# UDP 工作线程：入站消息分用

## 1. 本讲目标

本讲紧接 [u3-l1（WireGuard 胶水层）](u3-l1-wireguard-glue.md) 与 [u3-l2（TUN 工作线程）](u3-l2-tun-worker.md)，把视角从「出站」切换到「入站」。

学完后你应当能够：

- 说清 `udp_worker` 从 UDP 套接字读到一个字节流后，如何按首部 4 字节的 `type` 字段把它**分用（de-multiplex）**成两类：握手消息与传输消息。
- 掌握四个消息类型常量 `TYPE_INITIATION / TYPE_RESPONSE / TYPE_COOKIE_REPLY / TYPE_TRANSPORT` 的取值与含义区别。
- 看懂握手分支里 `wg.pending.fetch_add` 原子计数与 `wg.queue.send` 入队的配合，以及它如何驱动「under-load（过载）」检测。
- 理解传输分支为何直接调用 `wg.router.recv`，以及它与握手分支走不同子系统的原因。
- 能够解释 `mtu == 0`（设备 down）时为何用 `continue` 而非 `return`。

## 2. 前置知识

- **入站方向**：对端通过 UDP 发来的密文报文到达本机 → `udp_worker` 读取 → 分用。这是相对于 u3-l2 中「本机 IP 包 → TUN → 加密 → UDP 发出」的**反方向**数据流。
- **WireGuard 的两类报文**：所有 WireGuard 报文（无论握手还是数据）都跑在同一条 UDP 隧道上，靠报文最前面的 4 字节小端 `type` 字段区分种类。这正是 `udp_worker` 做分用的依据。
- **握手 vs 传输**：
  - **握手报文**（type 1/2/3）：用于 Noise IK 协议协商对称会话密钥，处理代价高（涉及 DH、HKDF、AEAD），需要抗 DoS。
  - **传输报文**（type 4）：用已协商好的会话密钥做对称加解密，承载真实用户数据（IP 包），吞吐敏感。
- **原子计数与有界通道**：`AtomicUsize` 用于无锁计数；`crossbeam_channel::bounded` 创建有界通道，满时 `send` 会阻塞，从而对生产者形成**背压（back-pressure）**。
- 本讲把 `handshake::Device` 与 `router::Device` 当作黑盒，只关心 `udp_worker` 如何把报文交给它们；内部细节分别在 u4、u5 单元展开。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/wireguard/workers.rs` | 本讲主角。`udp_worker` 函数、`HandshakeJob` 枚举，以及作为对照的 `tun_worker`/`handshake_worker`。 |
| `src/wireguard/router/mod.rs` | 导出路由器的常量（`TYPE_TRANSPORT`、`SIZE_MESSAGE_PREFIX`、`CAPACITY_MESSAGE_POSTFIX`）与 `Device` 类型别名。 |
| `src/wireguard/router/messages.rs` | 定义传输报文头 `TransportHeader` 与 `TYPE_TRANSPORT = 4`。 |
| `src/wireguard/handshake/messages.rs` | 定义三类握手消息结构、`TYPE_INITIATION/RESPONSE/COOKIE_REPLY` 与 `MAX_HANDSHAKE_MSG_SIZE`。 |
| `src/wireguard/wireguard.rs` | `WireguardInner` 的 `pending`、`queue`、`mtu` 字段，以及 `add_udp_reader` 如何 spawn `udp_worker`。 |
| `src/wireguard/queue.rs` | `ParallelQueue`（单发送端、多接收端的有界通道），即 `wg.queue` 的实现。 |
| `src/wireguard/router/device.rs` | `Device::recv` 入口，传输分支最终落到这里。 |

## 4. 核心概念与源码讲解

### 4.1 UDP 工作线程的整体循环与缓冲区管理

#### 4.1.1 概念说明

`udp_worker` 是入站方向的第一个处理者。它是一条阻塞常驻的线程：每个 UDP reader 对应一条（Linux 上通常是 IPv4、IPv6各一个）。它的职责只有两件事——**从 UDP 套接字读出一个报文**，然后**根据报文类型把它转交给正确的子系统**。它本身不做任何密码学运算，这一点和 `tun_worker` 一样，是个纯粹的「搬运 + 分流」角色。

理解 `udp_worker` 的关键，是把它和 `tun_worker` 对照着看：

- `tun_worker`（u3-l2）处理**出站明文**：TUN 读 IP 包 → 路由器加密发出。
- `udp_worker` 处理**入站密文**：UDP 读密文 → 分用为握手或传输。

二者结构高度对称，但有几个重要差异，本讲会逐一指出。

#### 4.1.2 核心流程

`udp_worker` 的主循环伪代码如下：

```
loop:
    mtu = 原子读取 wg.mtu                      # 当前 MTU（down 时为 0）
    分配缓冲区 msg，大小 = mtu + MAX_HANDSHAKE_MSG_SIZE
    (size, src) = reader.read(msg)             # 阻塞读一个 UDP 报文
        读出错 → return（线程退出）            # socket 被关闭，见 4.1.3
    msg.truncate(size)                         # 裁剪到实际读取的字节数
    若 mtu == 0 → continue                     # 设备 down，丢弃但保持线程存活
    若 msg 不足 4 字节 → continue              # 连 type 字段都没有，丢弃
    根据 msg 前 4 字节（小端 u32）分用：
        握手类型 → 见 4.3
        传输类型 → 见 4.4
        未知类型 → 丢弃（_ => ()）
```

缓冲区大小取 `mtu + MAX_HANDSHAKE_MSG_SIZE` 是一个「一鱼两吃」的设计：

- 最大的握手报文是 `Initiation`，其长度即 `MAX_HANDSHAKE_MSG_SIZE = 148` 字节（推导见 4.2.3）。
- 最大的传输报文长度为 `SIZE_MESSAGE_PREFIX(16) + mtu + SIZE_TAG(16) = mtu + 32`。
- 由于 `148 > 32`，`mtu + 148 ≥ mtu + 32` 恒成立，所以这一个分配同时容纳「最大握手报文」与「最大传输报文」，无需在分用后再扩容。对传输报文而言多分配了一点空间，换取一次简单分配，值得。

#### 4.1.3 源码精读

函数签名与主循环开头：

[workers.rs:L103-L108](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L103-L108) — `udp_worker` 入口：每轮顶部 `wg.mtu.load(Ordering::Relaxed)` 原子重载 MTU，按 `mtu + MAX_HANDSHAKE_MSG_SIZE` 分配缓冲区。

读取与错误处理：

[workers.rs:L111-L118](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L111-L118) — `reader.read(&mut msg)` 返回 `(实际字节数, 源端点)`；**读出错时直接 `return`**（注意不是 `break`），日志写 "Bind reader closed"。`msg.truncate(size)` 把缓冲区裁剪到真实长度。

`reader.read` 的契约定义在平台抽象 trait 上：

[udp.rs:L4-L8](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/udp.rs#L4-L8) — `Reader::read` 把一个 UDP 报文写入 `buf`，返回 `(读到的字节数, 源 Endpoint)`。`src` 即对端地址，后续要用来回送响应、更新 peer 端点（支持漫游）。

`mtu == 0` 的处理：

[workers.rs:L120-L123](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L120-L123) — 注释 `TODO: start device down`，`mtu == 0` 表示设备处于 down 态，用 `continue` 丢弃报文但**不退出线程**。

为什么 down 时 mtu 是 0？看 `WireGuard::down`：

[wireguard.rs:L136-L137](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L136-L137) — `down()` 把 `mtu` 存为 0；相应地 `up(mtu)` 会把它存回真实值。

`udp_worker` 由 `add_udp_reader` 启动，且**不被 `WaitCounter` 计数**：

[wireguard.rs:L240-L245](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L240-L245) — `add_udp_reader` 只是 `thread::spawn` 一个 `udp_worker`，没有像 `add_tun_reader` 那样调用 `tun_readers.increase()/decrease()`。这意味着 `udp_worker` 的退出不影响 `wg.wait()`，可以自由 `return`。

#### 4.1.4 代码实践

**实践目标**：解释 `mtu == 0` 时为何用 `continue` 而非 `return`，并在源码上验证你的判断。

**操作步骤**：

1. 阅读 [workers.rs:L120-L123](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L120-L123) 与 [workers.rs:L111-L115](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L111-L115)，注意两处的不同：读出错 → `return`；`mtu == 0` → `continue`。
2. 阅读 [wireguard.rs:L240-L245](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L240-L245)（`add_udp_reader`）与 [wireguard.rs:L153-L175](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L153-L175)（`up`），确认 **`up()` 不会重新 spawn udp reader**。
3. 在 [workers.rs:L121](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L121) 的 `if mtu == 0` 分支里加一行（示例代码，非项目原有）：
   ```rust
   // 示例代码：仅用于观察 down 态下的丢包
   debug!("{} : udp_worker, device down (mtu=0), drain & drop {} bytes", wg, size);
   ```
4. 用 `RUST_LOG=debug` 运行（待本地验证），通过 UAPI 执行 `down` 再 `up`，观察 down 期间持续有该日志、`up` 后报文恢复正常处理。

**需要观察的现象**：down 期间 `udp_worker` 线程依然存活并持续读空/丢弃报文；up 之后无需重启进程即可恢复入站处理。

**预期结果 / 结论**：
- `return` 用于「socket 被永久关闭」的场景（重新绑定时旧 socket 关闭 → `read` 报错），此时线程理应退出，因为该 reader 已失效。
- `continue` 用于「设备管理性 down」的瞬态场景：socket 仍然有效，只是上层暂时不处理。如果改成 `return`，线程一旦退出就再也不会被重新创建（`up()` 不重起 reader），设备重新 up 后将永远收不到入站报文。此外，持续 `read` 还能**排空内核接收缓冲**，避免因缓冲堆积影响对端。所以必须 `continue`。

> 若无法本地运行，明确标注「待本地验证」，重点掌握上述因果推理。

#### 4.1.5 小练习与答案

**练习 1**：`udp_worker` 读出错时用 `return`，而 `tun_worker` 读出错时用 `break`（见 u3-l2）。为什么 `udp_worker` 不需要像 `tun_worker` 那样在退出时通知 `WaitCounter`？

**答案**：因为 `udp_worker` 不被 `WaitCounter` 计数。`add_udp_reader`（[wireguard.rs:L240-L245](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L240-L245)）只 spawn 线程，不调用 `tun_readers.increase()`；只有 `tun_worker` 被 `tun_readers` 计数，`wg.wait()` 只等 tun worker。所以 `udp_worker` 直接 `return` 即可，无需减少任何计数。

**练习 2**：缓冲区大小为何写成 `mtu + MAX_HANDSHAKE_MSG_SIZE`，而不是 `mtu + SIZE_MESSAGE_PREFIX + SIZE_TAG`？

**答案**：因为同一条 UDP 通道既收握手报文也收传输报文，缓冲区必须同时装得下二者。传输报文最大 `mtu + 32`，握手报文最大 `MAX_HANDSHAKE_MSG_SIZE = 148`；由于 `148 > 32`，`mtu + 148` 自动覆盖两种情况，是一种简单且安全的统一分配。

---

### 4.2 入站消息类型分用（de-multiplexer）

#### 4.2.1 概念说明

「分用」是本讲的核心概念。WireGuard 把所有报文都塞进一条 UDP 流，接收端必须把不同种类的报文送到不同的处理子系统。区分依据就是报文最前面的 **4 字节小端无符号整数 `type`**——它是每种消息结构体的第一个字段 `f_type`。

共有 4 个合法类型值：

| 类型常量 | 值 | 所属子系统 | 含义 |
| --- | --- | --- | --- |
| `TYPE_INITIATION` | 1 | 握手 | 发起握手 |
| `TYPE_RESPONSE` | 2 | 握手 | 响应握手 |
| `TYPE_COOKIE_REPLY` | 3 | 握手 | under-load 时返回的 cookie（抗 DoS） |
| `TYPE_TRANSPORT` | 4 | 路由器（数据面） | 承载加密用户数据的传输报文 |

注意 1/2/3 都是握手、4 是数据。这个划分决定了 `udp_worker` 的两条分流路径：握手走队列、传输走路由器。

#### 4.2.2 核心流程

```
读取并裁剪好 msg 之后：
    若 msg.len() < 4 → continue            # 太短，连 type 都读不出
    t = LittleEndian::read_u32(&msg[..])   # 读首 4 字节
    match t:
        1 | 2 | 3 → 握手分支（4.3）
        4        → 传输分支（4.4）
        _        → 丢弃（未知类型）
```

用一张分流图表示：

```
                 ┌──→ TYPE_INITIATION(1) ─┐
   UDP 报文 ──►  read_u32 ─┼──→ TYPE_RESPONSE(2) ──┼──► 握手队列 (4.3)
                 │     └──→ TYPE_COOKIE_REPLY(3)─┘
                 │
                 ├──→ TYPE_TRANSPORT(4) ──────────► 路由器 recv (4.4)
                 │
                 └──→ 其它 ───────────────────────► 丢弃（_ => ()）
```

`LittleEndian::read_u32` 之所以用小端，是因为 WireGuard 协议规定所有整数字段都是**小端序**（little-endian）——这也是为什么消息结构体里的 `f_type` 等字段都是 `U32<LittleEndian>`。

#### 4.2.3 源码精读

分用主体：

[workers.rs:L125-L144](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L125-L144) — 先做长度保护 `msg.len() < size_of::<u32>()` 丢弃过短报文，再用 `LittleEndian::read_u32(&msg[..])` 读取类型，`match` 分流到三个分支。

四个类型常量分布在两个模块（这是历史分层造成的，握手常量在 `handshake/`，传输常量在 `router/`）：

[handshake/messages.rs:L22-L24](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/messages.rs#L22-L24) — `TYPE_INITIATION = 1`、`TYPE_RESPONSE = 2`、`TYPE_COOKIE_REPLY = 3`。

[messages.rs:L5](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/messages.rs#L5) — `TYPE_TRANSPORT = 4`，并由 [router/mod.rs:L35](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/mod.rs#L35) `pub use messages::TYPE_TRANSPORT;` 在路由器模块根再导出。`workers.rs` 顶部的 `use` 同时从两个模块把它们引进来（[workers.rs:L25-L26](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L25-L26)）。

`MAX_HANDSHAKE_MSG_SIZE` 的推导（用于 4.1 的缓冲区计算）：

[handshake/messages.rs:L31-L34](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/handshake/messages.rs#L31-L34) — 取 `Response`、`Initiation`、`CookieReply` 三者 `size_of` 的最大值。各结构体均为 `#[repr(packed)]`，逐字段求和：

- `NoiseInitiation` = `f_type(4)+f_sender(4)+f_ephemeral(32)+f_static(48)+f_timestamp(28)` = 116 → `Initiation` = 116 + `MacsFooter(32)` = **148**
- `NoiseResponse` = `4+4+4+32+16` = 60 → `Response` = 60 + 32 = 92
- `CookieReply` = `f_type(4)+f_receiver(4)+f_nonce(24)+f_cookie(32)` = 64

故 `MAX_HANDSHAKE_MSG_SIZE = max(92, 148, 64) = 148`。

未知类型分支：

[workers.rs:L143](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L143) — `_ => ()` 静默丢弃任何非 1/2/3/4 的类型。这是本讲代码实践的改造点。

#### 4.2.4 代码实践

**实践目标**：对未知消息类型增加 debug 日志与计数，便于排查「收到奇怪报文」的情况（这正是本讲义规格指定的实践任务）。

**操作步骤**：

1. 在 `workers.rs` 顶部已有的 `use std::sync::atomic::Ordering;` 基础上，引入一个进程级原子计数器（示例代码，非项目原有）：
   ```rust
   // 示例代码
   use std::sync::atomic::AtomicU64;
   static UDP_UNKNOWN_TYPE_COUNT: AtomicU64 = AtomicU64::new(0);
   ```
2. 把 [workers.rs:L143](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L143) 的 `_ => ()` 改为：
   ```rust
   // 示例代码
   t => {
       UDP_UNKNOWN_TYPE_COUNT.fetch_add(1, Ordering::Relaxed);
       debug!("{} : udp_worker, dropped unknown message type {}", wg, t);
   }
   ```
   > 用 `AtomicU64` 而非线程局部（thread-local）的原因：`udp_worker` 通常有多条线程（IPv4/IPv6），我们想知道**全设备**累计收到多少未知类型，原子计数天然跨线程聚合。如果只关心单线程频率，可改用 `thread_local!`。
3. 编译（`cargo build`，待本地验证），确认无类型错误。

**需要观察的现象**：当对端发送了不符合 WireGuard 格式的 UDP 报文（例如用 `nc -u` 向端口发垃圾数据）时，日志会出现 `dropped unknown message type <t>`，且计数器递增。

**预期结果**：合法的 1/2/3/4 不受影响；只有真正未知的 type 才进入新分支。注意 `t` 此时绑定的是 `match` 的值（一个 `u32`），可直接打印。

> 若无法本地构造该流量，明确标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：长度保护 `msg.len() < std::mem::size_of::<u32>()` 写在 `read_u32` 之前。如果删掉它会怎样？

**答案**：`LittleEndian::read_u32(&msg[..])` 在缓冲区不足 4 字节时会 panic（切片越界）或返回错误（取决于 `byteorder` 版本）。该保护确保任何短于 4 字节的报文（如探测包、残包）被安全丢弃，而不至于让 `udp_worker` 线程崩溃。

**练习 2**：为什么传输报文类型常量 `TYPE_TRANSPORT` 定义在 `router/` 而不是和其它三个一起定义在 `handshake/`？

**答案**：因为类型常量与其处理子系统对应：1/2/3 由握手模块处理，4 由路由器（数据面）模块处理。把 `TYPE_TRANSPORT` 放在 `router/messages.rs` 让数据面自包含；`udp_worker` 作为胶水层，分别从两个模块 `use` 它们（[workers.rs:L25-L26](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L25-L26)），体现了「胶水层连接两个独立子系统」的架构分层。

---

### 4.3 握手分支：`pending` 原子计数与入队

#### 4.3.1 概念说明

当 `type ∈ {1,2,3}` 时，报文是握手消息。`udp_worker` 不自己处理它（不做密码学），而是把它**包装成 `HandshakeJob::Message` 丢进握手队列**，由一组 `handshake_worker` 线程（数量 = CPU 核数，见 u3-l1）并行消费。

这里有两个关键设计：

1. **`wg.pending` 原子计数**：在入队前先 `fetch_add(1)`，记录「目前有多少握手任务在队列里/在途」。这个计数是 **under-load（过载）检测**的输入——当 pending 超过阈值，设备认为自己正遭受可能的 DoS，从而触发 cookie 机制（详见 u4-l4）。
2. **有界通道背压**：`wg.queue` 是容量 128 的有界通道；队列满时 `send` 会阻塞 `udp_worker`，自动限制入站握手速率，保护系统不被打爆。

#### 4.3.2 核心流程

```
握手分支：
    debug 日志
    wg.pending.fetch_add(1, SeqCst)              # 先计数
    wg.queue.send(HandshakeJob::Message(msg, src))  # 再入队（满则阻塞）

# 对应在 handshake_worker（u4-l6 详讲）：
    job = rx.recv()                              # 取出任务
    pending = wg.pending.fetch_sub(1, SeqCst)    # 计数减一，返回旧值
    if pending > THRESHOLD_UNDER_LOAD:           # 过载检测
        under_load = true                        # 触发 cookie / DoS 防御
```

注意 `fetch_add` 在 `send` **之前**：即使 `send` 阻塞，该任务也已被计数；当它最终被 `handshake_worker` 取走时 `fetch_sub`，差值就是「队列中 + 在途」的握手任务数。

#### 4.3.3 源码精读

握手分支本体：

[workers.rs:L130-L133](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L130-L133) — 三个握手类型共用一个 `match` 臂：`fetch_add(1, Ordering::SeqCst)` 后 `wg.queue.send(HandshakeJob::Message(msg, src))`。

`HandshakeJob` 枚举：

[workers.rs:L30-L33](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L30-L33) — `HandshakeJob::Message(Vec<u8>, E)` 携带报文字节与源端点；另一个变体 `New(PublicKey)` 表示「本地主动请求与某 peer 握手」，由定时器注入，不经 `udp_worker`。

`pending` 与 `queue` 字段定义：

[wireguard.rs:L59-L60](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L59-L60) — `pending: AtomicUsize`（注释 "number of pending handshake packets in queue"）与 `queue: ParallelQueue<HandshakeJob<B::Endpoint>>`。

`ParallelQueue` 的有界通道实现：

[queue.rs:L15-L33](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/queue.rs#L15-L33) — `ParallelQueue::new(queues, capacity)` 用 `crossbeam_channel::bounded(capacity)` 创建**一个**发送端、克隆出 `queues` 个接收端（见 u3-l1 的「单发多收」）；`send` 在通道满时阻塞。注意创建时传的容量是 `128`（[wireguard.rs:L273](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L273)）。

消费端的 `fetch_sub` 与过载阈值：

[workers.rs:L159-L167](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L159-L167) — `handshake_worker` 取出任务后立即 `wg.pending.fetch_sub(1, SeqCst)`，若返回的旧值 `> THRESHOLD_UNDER_LOAD` 则置 `under_load = true` 并刷新 `last_under_load` 时间戳。

过载相关常量：

[constants.rs:L19-L29](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/constants.rs#L19-L29) — `MAX_QUEUED_INCOMING_HANDSHAKES = 4096`、`THRESHOLD_UNDER_LOAD = 4096/8 = 512`、`DURATION_UNDER_LOAD = 1s`。即 pending 超过 512 即判定过载，且一旦过载至少维持 1 秒。

> 关于内存序：`fetch_add` 与 `fetch_sub` 都用 `SeqCst`，为的是给 under-load 判定提供一个跨线程的全局一致顺序，避免计数与判断之间出现松散内存序导致的误判。

#### 4.3.4 代码实践

**实践目标**：跟踪 `pending` 计数的完整生命周期（增/减/判定），理解它如何驱动 under-load。

**操作步骤**：

1. 阅读 [workers.rs:L130-L133](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L130-L133)（增）与 [workers.rs:L159-L167](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L159-L167)（减 + 判定）。
2. 在两处各加一行日志（示例代码，非项目原有）：
   ```rust
   // 入队前（udp_worker）
   debug!("{} : pending inc -> {}", wg,
          wg.pending.fetch_add(1, Ordering::SeqCst) + 1);
   // 取出后（handshake_worker）
   debug!("{} : pending dec -> {}", wg,
          wg.pending.fetch_sub(1, Ordering::SeqCst) - 1);
   ```
3. 对照 [constants.rs:L19-L29](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/constants.rs#L19-L29)，记下阈值 512。

**需要观察的现象**：轻载时 pending 在 0 附近波动；模拟握手洪泛时 pending 上升，越过 512 后日志出现 `under load (above threshold)`。

**预期结果**：`pending` 是「队列内 + 在途」握手任务的近似计数；它单调围绕真实负载波动，是 under-load/cookie 机制的触发信号。若无法本地验证，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`pending` 的容量上限到底是 4096（`MAX_QUEUED_INCOMING_HANDSHAKES`）还是 128（通道容量）？二者关系是什么？

**答案**：实际队列容量是 **128**（`ParallelQueue::new(cpus, 128)`）。`MAX_QUEUED_INCOMING_HANDSHAKES = 4096` 是一个**协议级常量**，主要用于派生过载阈值 `THRESHOLD_UNDER_LOAD = 512` 以及在 `handshake_worker` 里做一个宽松的 sanity 断言（[workers.rs:L160](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L160)）。由于有界通道满时 `send` 会阻塞，`pending` 通常被限制在 128 + 在途任务数附近；4096 是一个远高于实际容量的安全上界。

**练习 2**：为什么 `fetch_add` 要放在 `queue.send` 之前，而不是之后？

**答案**：`queue.send` 在通道满时会阻塞。若先 `send` 后计数，则阻塞期间该任务「已入队但未计数」，`pending` 会低估在途任务数，under-load 检测会失准（漏报过载）。先计数后入队，则「计数 ≥ 实际在途任务」始终成立，过载判定偏保守、更安全。

---

### 4.4 传输分支：交给路由器解密

#### 4.4.1 概念说明

当 `type == 4`（`TYPE_TRANSPORT`）时，报文是承载用户数据的传输报文。`udp_worker` 把它直接交给 **路由器（router）** 的 `recv` 方法，由路由器内部的工作线程池做 AEAD 解密、防回放、cryptokey 路由校验、写入 TUN 等处理（详见 u5 单元）。

注意它**不经过握手队列、不触碰 `pending` 计数**——传输报文有自己的并行/有序管道，不需要 under-load/cookie 这套抗 DoS 机制（因为传输报文必须先完成握手、用合法会话密钥才能解密成功，攻击者无法用未授权的传输报文浪费太多服务器资源）。

`wg.router.recv` 返回 `Result<(), RouterError>`；失败时 `udp_worker` 仅打一条 debug 日志后继续，不退出。

#### 4.4.2 核心流程

```
传输分支：
    debug 日志
    let _ = wg.router.recv(src, msg)        # 交给路由器
        .map_err(|e| debug!(...));          # 失败仅记录，不退出
```

路由器 `recv` 内部（[device.rs:L211-L250](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L211-L250)，u5 详讲）会：

1. 用 `LayoutVerified::new_from_prefix` 零拷贝解析 `TransportHeader`，断言 `f_type == TYPE_TRANSPORT`。
2. 按 `f_receiver`（接收方 id）在 `recv` 表查到对应的 `DecryptionState`（解密状态，含会话密钥与防回放窗口）。
3. 构造 `ReceiveJob` 丢进路由器自己的工作队列，由 worker 线程并行解密、串行按序写 TUN。

#### 4.4.3 源码精读

传输分支本体：

[workers.rs:L135-L142](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L135-L142) — `TYPE_TRANSPORT` 臂调用 `wg.router.recv(src, msg)`，用 `map_err` 把错误降级为 debug 日志，`let _ =` 丢弃返回值。

`router` 字段定义：

[wireguard.rs:L54-L55](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L54-L55) — `router: router::Device<...>`，类型别名 `Device` 实际是 `DeviceHandle`（[router/mod.rs:L34](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/mod.rs#L34)）。

`recv` 入口：

[device.rs:L211-L250](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L211-L250) — 解析头、按 `f_receiver` 查 `DecryptionState`、构造 `ReceiveJob` 并入队。其中 [device.rs:L224-L227](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L224-L227) 用 `debug_assert!` 断言 `f_type == TYPE_TRANSPORT`，注释明确写 "this should be checked by the message type multiplexer"——也就是说，`udp_worker` 的分用就是这条断言的前提保证。

#### 4.4.4 代码实践

**实践目标**：通过阅读 `Device::recv`，确认传输分支与握手分支走完全不同的子系统，并理解 `debug_assert` 对分用器的依赖。

**操作步骤**：

1. 阅读 [device.rs:L211-L250](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L211-L250)。
2. 找到 [device.rs:L224-L227](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L224-L227) 的 `debug_assert!(header.f_type.get() == TYPE_TRANSPORT as u32, ...)`。
3. 思考：假如 `udp_worker` 的分用器有 bug，把一个握手报文误送进 `wg.router.recv`，会在哪里失败？

**需要观察的现象（源码阅读型）**：`recv` 第一步 `LayoutVerified::new_from_prefix` 会成功（因为握手报文也以 4 字节 type 开头），但随后按 `f_receiver` 查 `recv` 表大概率查不到（握手报文的字段语义不同），返回 `RouterError::UnknownReceiverId`；即便侥幸查到，解密也会失败。

**预期结果 / 结论**：`debug_assert` 把「分用器必须保证 type == 4」这一不变量显式化。在 debug 构建下，分用器 bug 会立即触发断言 panic；release 构建下则靠解密失败兜底。这说明了 `udp_worker` 分用正确性的重要性。

#### 4.4.5 小练习与答案

**练习 1**：传输分支为何不像握手分支那样维护一个类似 `pending` 的计数？

**答案**：因为传输报文必须先用合法会话密钥才能解密成功，未授权的传输报文会在解密阶段被廉价地丢弃，难以构成 DoS；且路由器已有自己的有界工作队列与背压。握手则不同——initiation 报文在认证前就需要做较重的密码学处理，容易被滥用，因此需要 `pending` 计数 + under-load + cookie 机制专门保护（见 u4-l4）。

**练习 2**：`wg.router.recv(src, msg)` 的第一个参数 `src`（源端点）最终被用在什么地方？

**答案**：`src` 随 `ReceiveJob` 一路传递，在传输报文成功解密、通过防回放校验后，用于**更新该 peer 的对端端点**（支持对端漫游：当对端换了个地址发来合法报文，本端就把回复地址更新为 `src`）。这与握手分支里 `HandshakeJob::Message(msg, src)` 携带 `src` 的目的是一致的。

---

## 5. 综合实践

设计一个贯穿本讲的源码阅读 + 画图任务，把四个模块串起来：

**任务**：画出一张「入站报文完整流转图」，并对照源码标注每一步的代码位置。

要求在图中至少包含以下节点与判断，并给每个节点标上对应的永久链接行号：

1. UDP 套接字收到一个字节流 → `reader.read`（[workers.rs:L111](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L111)）。
2. 读出错 → `return`（[workers.rs:L112-L115](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L112-L115)）。
3. `mtu == 0` → `continue`（[workers.rs:L120-L123](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L120-L123)）。
4. 长度 < 4 → `continue`（[workers.rs:L126-L128](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L126-L128)）。
5. `read_u32` 分用（[workers.rs:L129](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L129)），分三条路径：
   - 握手（1/2/3）：`pending.fetch_add` → `queue.send`（[workers.rs:L132-L133](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L132-L133)）→ `handshake_worker` 消费并 `fetch_sub` 判过载（[workers.rs:L159-L167](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L159-L167)）。
   - 传输（4）：`wg.router.recv`（[workers.rs:L139](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L139)）→ `Device::recv` 解析头 + 查 `DecryptionState` + 入队（[device.rs:L211-L250](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/router/device.rs#L211-L250)）。
   - 未知：`_ => ()`（[workers.rs:L143](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/workers.rs#L143)）。

**进阶**：在图上用颜色/标记区分「会改变线程归属」的边（握手入队后由 `handshake_worker` 线程处理、传输入队后由 router worker 线程处理），体会 `udp_worker` 作为「分流器」把工作分发给两个独立线程池的角色。

完成后，你应该能用自己的话回答：一个入站字节流从进入 `udp_worker` 到被实际处理，最多会跨越几个线程？

## 6. 本讲小结

- `udp_worker` 是入站方向的第一站，职责单一：**读 UDP 报文 → 按 `type` 分用**，自身不做密码学。
- 分用依据是报文首 4 字节小端 `u32`：`1/2/3`（`INITIATION/RESPONSE/COOKIE_REPLY`）是握手，`4`（`TRANSPORT`）是数据，其余丢弃。
- 握手分支：先 `wg.pending.fetch_add(1, SeqCst)` 计数，再 `wg.queue.send(HandshakeJob::Message(...))` 入有界通道（容量 128，满则背压）；`pending` 是 under-load/cookie 抗 DoS 机制的输入。
- 传输分支：直接 `wg.router.recv(src, msg)`，交路由器工作线程池解密；不经队列、不触碰 `pending`。
- 缓冲区 `mtu + MAX_HANDSHAKE_MSG_SIZE`（= `mtu + 148`）一份分配同时容纳最大握手报文与最大传输报文。
- 错误处理三态：读出错 → `return`（socket 关闭/重绑，线程退出）；`mtu == 0` → `continue`（设备 down 是瞬态，必须保活线程排空缓冲）；过短/未知类型 → `continue`/丢弃。`udp_worker` 不被 `WaitCounter` 计数，故可自由 `return`。

## 7. 下一步学习建议

- **横向对照**：回到 [u3-l2](u3-l2-tun-worker.md) 把 `tun_worker`（出站）与 `udp_worker`（入站）并排重读，巩固「胶水层两个对称搬运工」的整体印象。
- **纵向深入握手**：进入 u4 单元，尤其是 **u4-l6（握手工作线程）**——那里会讲清 `handshake_worker` 如何消费本讲入队的 `HandshakeJob::Message`、如何用 `pending` 判定 under-load、如何处理 cookie 与密钥派生。
- **纵向深入数据面**：进入 u5 单元，尤其是 **u5-l1（路由器总览）** 与 **u5-l3（接收管道 ReceiveJob）**——那里会讲清 `wg.router.recv` 之后报文如何被并行解密、防回放、按序写 TUN。
- **抗 DoS 全貌**：本讲只触及 `pending` 计数；完整的 cookie/mac 机制在 **u4-l4**，建议结合阅读。
