# 连接建立与握手：SYN cookie

## 1. 本讲目标

学完本讲，读者应该能够：

- 完整复述一条 UDT 连接是如何从「客户端调用 `connect`」走到「双方都进入 `Connected`」的，能画出四个握手包的时序与 `connection_type` 的取值变化。
- 说清楚 SYN cookie 的计算公式、它为什么按分钟轮换、为什么校验时要同时接受「当前分钟」和「上一分钟」两份 cookie。
- 说清楚握手期间的 MSS（最大分片大小）协商为什么是「取较小值」，以及版本号/ socket 类型不匹配时 listener 如何用 `1002` 拒绝。
- 把握手逻辑定位到 `src/socket.rs`、`src/control_packet.rs`、`src/udt.rs` 这三个文件的具体行号上，并能解释 `Udt::new_connection` 如何把新连接登记进 `peers` 注册表并唤醒 `accept`。

本讲是专家层「连接生命周期」的第一篇，前置是 [u3-l1 全局单例 Udt 与注册表](u3-l1-global-udt-registry.md) 和 [u4-l3 控制包格式](u4-l3-control-packet.md)：你需要已经知道全局 `Udt` 持有 `sockets`/`multiplexers`/`peers` 三张表，以及握手控制包的 `HandShakeInfo` 字段布局。

## 2. 前置知识

### 2.1 为什么要握手

UDT 跑在不可靠的 UDP 之上。两个端点在传数据前，必须先互相交换一些参数（彼此的 socket id、初始序列号、MSS、流控窗口），并确认对方「确实在线、确实能收到我的包」。这套参数交换就是握手（handshake）。

UDP 没有内核帮我们做握手（不像 TCP 的三次握手固化在协议栈里），所以 UDT 把握手完全放在应用层，用一种叫 **Handshake 的控制包**（控制包类型码 `0x0000`，见 [u4-l3](u4-l3-control-packet.md)）来承载。

### 2.2 什么是 SYN cookie，为什么要它

经典的 **SYN flood 攻击**：攻击者用伪造的源 IP 大量发送连接请求（SYN）。被攻击的服务端每收到一个请求就分配一条「半开连接」的资源，最终资源耗尽、无法服务真实用户。

**SYN cookie** 的防御思路：服务端在握手完成前**不分配任何 per-connection 状态**，而是把「这个客户端是否合法」编码进一个无状态的 cookie。流程是：

1. 客户端发来连接请求。
2. 服务端不存任何东西，只用一个**只有自己知道的密钥** + 客户端地址 + 当前时间，算出一个 cookie，连同握手包回给客户端。
3. 客户端必须把这个 cookie **原样回带**。
4. 服务端用同样的公式重算一遍 cookie 做比对：只有当客户端真能在这个地址收发包（即不是伪造源 IP）时，cookie 才对得上。校验通过后，服务端才真正创建连接。

因为伪造源 IP 的攻击者收不到服务端回包，自然回不出正确的 cookie，所以无法耗尽服务端资源。UDT 沿用了这一思想。

### 2.3 关键术语速查

| 术语 | 含义 |
|---|---|
| `connection_type` | 握手包里的一个 `i32` 字段，标记当前握手处于哪个阶段：`1` = 首轮请求、`-1` = 回带/最终响应、`1002` = 拒绝 |
| SYN cookie | 服务端无状态计算的一段验证码，要求客户端回带以证明其源地址真实 |
| ISN（initial seq number） | 初始序列号，连接双方的序列号起点 |
| MSS | 最大分片大小，单个数据包（含包头）的上限，默认 1500 |
| `peers` 注册表 | 全局 `Udt` 里 `(对端 socket id, isn) → 本地 socket id 集合` 的映射，用于握手去重 |
| `dest_socket_id` | 控制包头里的「目标 socket id」字段；握手阶段为 `0`，表示「发给这个端口上的 listener」 |

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `src/socket.rs` | 握手三阶段的核心逻辑：客户端 `connect`、服务端 `listen_on_handshake`、服务端最终响应 `connect_on_handshake`，以及 `compute_cookie` 和客户端侧 `process_ctrl` 的 Handshake 分支 |
| `src/control_packet.rs` | 握手包的线上格式 `HandShakeInfo`，含 `serialize`/`deserialize`（IPv4/IPv6、`connection_type` 解析）和 `new_handshake` 构造函数 |
| `src/udt.rs` | 全局引擎在握手里的职责：`new_connection` 创建新连接、登记 `peers` 注册表、唤醒 `accept`；`get_peer_socket` 做握手去重 |
| `src/queue/rcv_queue.rs` | 接收 worker 如何把 `dest_socket_id == 0` 的握手包路由给 listener |
| `src/configuration.rs` | `udt_version()`（恒为 4）与 `accept_queue_size`，分别用于拒绝判定与积压上限 |

## 4. 核心概念与源码讲解

### 4.1 三阶段握手主流程：connect → listen_on_handshake → connect_on_handshake

#### 4.1.1 概念说明

UDT 的常规连接握手需要 **4 个握手包**，分布在三个函数里。我们把客户端发起连接的那个 socket 记为 **C**，服务端 listener socket 记为 **L**，服务端在握手成功后新创建的 socket 记为 **S**：

| 序号 | 方向 | 触发函数 | `connection_type` | `syn_cookie` | 说明 |
|---|---|---|---|---|---|
| ① | C → L | `UdtSocket::connect` | `1` | `0` | 客户端发起首轮请求 |
| ② | L → C | `listen_on_handshake`（`ct==1` 分支） | `1`（不变） | 由 L 计算 | L 算出 cookie 回给客户端 |
| ③ | C → L | `process_ctrl` 的 Handshake 分支（`ct>0` 分支） | `-1` | 回带 ②里的 cookie | 客户端把 cookie 原样回带 |
| ④ | S → C | `connect_on_handshake` | `-1`（不变） | 沿用 | S 完成参数协商，给最终配置 |

> 注意：`connection_type` 的取值是 `1 → 1 → -1 → -1`。这正是讲义规格里说的「client connect 发 `connection_type=1`、listener 回 SYN cookie、client 回 `-1`、`connect_on_handshake` 完成协商」。

握手成功后，C 收到 ④进入 `Connected`（客户端侧），S 在发出 ④之前就已经把自己置为 `Connected`（服务端侧）。于是双方都进入 `Connected`，可以开始传数据。

#### 4.1.2 核心流程

用伪代码描述三阶段（省略错误处理与锁）：

```
# 阶段 A：客户端发起（socket.rs::connect）
C.status == Init?  否则报错
C.open()                       # Init -> Opened
Udt.update_mux(C, bind_addr)   # 客户端 bind_addr=None，新建临时端口 mux
C.status = Connecting
C.set_peer_addr(peer)
构造 hs { connection_type: 1, syn_cookie: 0, socket_id: C.id, isn: C.isn, mss, window }
C.send_to(peer, hs, dest=0)    # dest=0 表示「发给 listener」
return Ok                      # 握手往返由后台 worker 异步完成

# 阶段 B：listener 处理首请求与回 cookie（socket.rs::listen_on_handshake）
收到 hs（由 rcv_queue worker 路由过来，dest_socket_id==0）
if hs.connection_type == 1:
    hs_response = hs.clone()
    hs_response.syn_cookie = compute_cookie(peer_addr, offset=None)   # 当前分钟
    发送 hs_response 给 peer，dest = hs.socket_id（客户端 id）
    return Ok
# 详见 4.2 / 4.3 对 ct==-1 的校验与协商

# 阶段 C：服务端创建新连接并给最终配置（socket.rs::connect_on_handshake，由 Udt::new_connection 调用）
S = UdtSocket::new(..., isn=hs.isn, config=listener.config)
        .with_peer(peer, hs.socket_id)
        .with_listen_socket(listener.id, listener.mux)   # 复用 listener 的 mux
S.open()
# MSS 协商：取较小值（见 4.3）
S.flow.window = hs.window
hs.window = min(rcv_buf_size, flight_flag_size)
hs.ip = peer.ip(); hs.socket_id = S.id
rate_control.init(...)
S.status = Connected
发送 hs（ct=-1）给 peer，dest = peer_socket_id
mux.rcv_queue.push_back(S.id)   # 把新 socket 加入定时器轮转
return Arc<S>

# 阶段 C'：客户端收最终响应（socket.rs::process_ctrl 的 Handshake 分支）
收到 hs，C.status 必须是 Connecting，否则报错
if hs.connection_type > 0:      # 即 ②，cookie 响应
    hs.connection_type = -1
    hs.socket_id = C.id
    发送 hs 给 dest=0            # ③：把 cookie 回带
else:                           # 即 ④，最终配置（ct=-1）
    config.mss = hs.mss
    config.flight_flag_size = hs.window
    state 游标用 hs.isn 初始化
    peer_socket_id = hs.socket_id
    rate_control.init(...)
    C.status = Connected
    connect_notify.notify_waiters()   # 唤醒 wait_for_connection
```

关键观察：

- **客户端的 `connect` 是「发射后不管」的**：它只发出 ① 就返回 `Ok`，真正的握手往返由 multiplexer 的接收/发送 worker 在后台异步完成。客户端上层用 `wait_for_connection` 轮询状态直到离开 `Connecting`（见 [u2-l1](u2-l1-connection-client.md)）。
- **`dest_socket_id` 在握手前两包里是 `0`**：因为客户端还不知道（也不需要知道）listener 的 socket id，只需知道「这个 UDP 端口上有个 listener」。接收 worker 看到 `dest==0` 且是握手包，就路由给该 mux 唯一的 listener。

#### 4.1.3 源码精读

**客户端发起①**——构造并发送首轮握手包：

[src/socket.rs:1098-1143](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1098-L1143) ——`UdtSocket::connect`：校验必须是 `Init` 态，`open()` 后调用全局 `Udt::update_mux` 建好 multiplexer，置 `Connecting`，构造 `connection_type: 1`、`syn_cookie: 0` 的握手包并以 `dest_socket_id = 0` 发出。

注意 [src/socket.rs:1122-1139](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1122-L1139) 里的 `max_window_size`：

```rust
max_window_size: std::cmp::min(
    self.flow.read().unwrap().flow_window_size,
    self.rcv_buffer().get_available_buf_size(),
),
```

客户端在首轮就把自己「能通告给对端的接收窗口」算好（取流控窗口与接收缓冲可用量的较小值），随握手带过去。

**握手包如何到达 listener**——接收 worker 的路由：

[src/queue/rcv_queue.rs:162-181](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/queue/rcv_queue.rs#L162-L181) ——`dest_socket_id == 0` 时，只接受握手包，取出 mux 唯一的 listener，调用 `listener.listen_on_handshake(addr, handshake)`；非握手包发到 `dest==0` 则视为非法。

**listener 回 cookie②**：

[src/socket.rs:376-433](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L376-L433) ——`listen_on_handshake`。其中 [src/socket.rs:385-393](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L385-L393) 是首轮分支：克隆 hs、写入 `syn_cookie = compute_cookie(&addr, None)`，以 `dest_socket_id = hs_response.socket_id`（客户端的 id）回发。

**客户端回带 cookie③**：

[src/socket.rs:450-491](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L450-L491) ——`process_ctrl` 的 `Handshake` 分支。状态必须是 `Connecting`；[src/socket.rs:462-467](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L462-L467) 处理 `connection_type > 0`（即收到的 ②）：把 `connection_type` 改成 `-1`、把 `socket_id` 改成自己的，**保留 listener 给的 `syn_cookie` 不动**，以 `dest_socket_id = 0` 发回，这就是 ③。

**服务端创建新连接 + 发最终配置④**：

握手校验通过后，`listen_on_handshake` 调用 `Udt::get().write().await.new_connection(self, addr, hs)`（[src/socket.rs:425-429](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L425-L429)）。`new_connection` 在 [src/udt.rs:94-175](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L94-L175) 里创建新 socket S（复用 listener 的 multiplexer），然后在 [src/udt.rs:164](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L164) 调用 `new_socket.connect_on_handshake(peer, hs.clone())` 发出 ④。`connect_on_handshake` 的主体见 [src/socket.rs:170-216](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L170-L216)。

**客户端收最终配置④进入 Connected**：

回到 [src/socket.rs:468-490](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L468-L490) 的 `else` 分支（post connect）：用对端协商后的 `mss`/`flight_flag_size` 更新配置，用 `hs.initial_seq_number` 初始化接收侧游标（`last_sent_ack`、`last_ack2_received`、`curr_rcv_seq_number = isn - 1`），记下 `peer_socket_id`，置 `Connected` 并 `connect_notify.notify_waiters()`——这一句唤醒了 [src/connection.rs:65-70](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/connection.rs#L65-L70) 里那个 `wait_for_connection` 的循环，让 `UdtConnection::connect` 真正返回。

#### 4.1.4 代码实践

**实践目标**：把四个握手包的来龙去脉在源码里「走」一遍，确认 `connection_type` 的取值流转。

**操作步骤（源码阅读型实践）**：

1. 打开 `src/socket.rs`，定位三处：
   - `connect`（约 1098 行）里 `connection_type: 1`；
   - `listen_on_handshake` 的 `if hs.connection_type == 1`（约 385 行）—— 注意它**没有**改 `connection_type`，所以 ②仍是 `1`；
   - `process_ctrl` 的 Handshake 分支里 `if hs.connection_type > 0`（约 462 行）—— 它把 `connection_type` 改成 `-1`。
2. 在 `src/udt.rs` 的 `new_connection`（约 94 行）里，确认它调用 `connect_on_handshake`（约 164 行），而后者也**没有**改 `connection_type`，所以 ④仍是 `-1`。
3. 画出下面这张时序图：

```
C(客户端 socket)                          L(listener)            S(新建服务端 socket)
   |  ① hs{ct=1, cookie=0, dest=0}            |                        |
   |----------------------------------------->>|                        |
   |  ② hs{ct=1, cookie=算, dest=C.id}        |                        |
   |<<-----------------------------------------|                        |
   |  ③ hs{ct=-1, cookie=算, dest=0}          |                        |
   |----------------------------------------->>|                       (此时 new_connection 创建 S)
   |                                           |-------connect_on_handshake----->|
   |  ④ hs{ct=-1, 协商后, dest=C.id}           |                        |  S.status=Connected
   |<<-------------------------------------------------------------------|
   |  C.status=Connected (process_ctrl 的 else 分支)                     |
```

**需要观察的现象**：四个包的 `connection_type` 依次是 `1, 1, -1, -1`；②和④的 `dest_socket_id` 是客户端 socket id，①和③是 `0`。

**预期结果**：你能不依赖讲义，口述出「哪个函数发出哪个包、`connection_type` 在哪一行被改成 `-1`」。

#### 4.1.5 小练习与答案

**练习 1**：为什么客户端的 `connect` 发完 ① 就立即返回 `Ok`，而不是等到收到 ④？

**参考答案**：因为握手往返需要经过网络和后台 worker，是异步的。`connect` 只负责「把状态推到 `Connecting` 并发出首个握手包」；后续由接收 worker 收到 ②/④后在 `process_ctrl` 里推进，最终 `connect_notify.notify_waiters()` 唤醒上层 `wait_for_connection` 的循环。这样 `connect` 本身不会阻塞在网络往返上，符合 Tokio 异步模型。

**练习 2**：如果客户端还没连上，又调用了一次 `connect`，会发生什么？

**参考答案**：[src/socket.rs:1103-1108](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1103-L1108) 检查到状态不是 `Init`（此时是 `Connecting`），直接返回 `Unsupported` 错误。

---

### 4.2 SYN cookie：compute_cookie 的防伪造与轮换

#### 4.2.1 概念说明

`compute_cookie` 是 SYN cookie 防御的核心。它的设计目标有三个：

1. **无状态**：listener 在发 cookie ②时**不存任何东西**，校验时用同一公式重算即可，因此伪造源 IP 的攻击者无法通过校验。
2. **绑定源地址**：cookie 依赖客户端的 IP 和端口，换一个源地址就算不出同样的值。
3. **限时有效**：cookie 里掺了「分钟级时间戳」，每过一分钟就变，攻击者即便窃听到一个 cookie，也只能在很短时间内重放。

这三个性质合起来，让 UDT 的 listener 在握手完成前几乎不为「半开连接」付出代价。

#### 4.2.2 核心流程

cookie 的计算公式（把字节串当输入做 SHA-256，取前 4 字节当 `u32`）：

\[
\text{cookie} = \text{u32BE}\bigl(\,\text{SHA-256}(\text{salt} : \text{ip} : \text{port} : \text{timestamp})[0..4]\bigr)
\]

其中：

- `salt` 是进程级随机串，30 个字母数字字符，进程启动后不变。
- `ip`、`port` 来自客户端的 `SocketAddr`。
- `timestamp = (listener.start_time.elapsed().as_secs() / 60) + offset`，即「以 listener socket 创建时刻为起点，每 60 秒一个桶」的时间分片。

校验时（`listen_on_handshake` 收到 ③），listener 重新算两份 cookie：

\[
\text{cookie}_0 = \text{compute}(\text{offset}=0),\qquad
\text{cookie}_{-1} = \text{compute}(\text{offset}=-1)
\]

只要客户端回带的 cookie 等于其中任意一个，就判合法。下面 4.2.3 会解释为什么是「当前分钟或上一分钟」。

#### 4.2.3 源码精读

**进程级 salt**：

[src/socket.rs:29-35](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L29-L35) ——`SALT` 是 `Lazy<String>`，首次访问时生成 30 位随机字母数字串，之后整个进程复用。它是 cookie 里「只有服务端知道」的密钥成分。

**cookie 计算**：

[src/socket.rs:356-366](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L356-L366) ——`compute_cookie`。关键点：

```rust
let timestamp = (self.start_time.elapsed().as_secs() / 60) + offset.unwrap_or(0) as u64;
```

- `self.start_time` 是 listener socket 的创建时刻（见 [u3-l2](u3-l2-udt-socket-state.md) 的 `UdtSocket` 字段），`.elapsed()` 是「自创建以来经过的秒数」。
- `/ 60` 把时间轴切成每分钟一个桶。
- `offset` 让调用方能算「相邻桶」的 cookie。

最后 `Sha256::digest(format!("{salt}:{host}:{port}:{timestamp}"))` 取前 4 字节大端还原成 `u32`。

**生成②（用当前桶）**：

[src/socket.rs:389](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L389) ——`hs_response.syn_cookie = self.compute_cookie(&addr, None);`，`offset=None` 即「当前分钟」。

**校验③（接受当前桶或上一桶）**：

[src/socket.rs:402-409](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L402-L409)：

```rust
let syn_cookie = hs.syn_cookie;
if syn_cookie != self.compute_cookie(&addr, None)
    && syn_cookie != self.compute_cookie(&addr, Some(-1))
{
    return Err(Error::new(ErrorKind::PermissionDenied, "invalid cookie"));
}
```

**为什么是「当前桶（offset=0）或上一桶（offset=-1）」两份都接受？** 因为 cookie 是 listener 在②时刻按「当时的当前桶」算的，而③到达时已经过了一段往返时间（RTT）。如果这段 RTT 跨过了一分钟的整点边界，那么③到达时 listener 的「当前桶」已经 `+1`，和②时算的不是同一个桶了。校验端同时算 `offset=0`（仍在本桶）和 `offset=-1`（退回上一桶），就**容错了一次分钟边界跨越**，给握手 RTT 一个最长约 2 分钟的容忍窗口：

- ②在桶 T 发出；
- ③若仍在桶 T 到达：`offset=0` 命中；
- ③若已进入桶 T+1 到达：`offset=-1` 算出桶 T，命中。

这恰好覆盖「最多跨一次边界」的情形，既容错又把重放窗口压在约 2 分钟内。

**为什么要按分钟轮换？** 因为 salt 在进程生命期内不变，唯一让 cookie 随时间变化的就是 `timestamp`。每分钟换桶，意味着同一个 `(ip, port)` 在不同分钟算出的 cookie 不同，攻击者窃听到的 cookie 最多有效约 2 分钟，且必须从同一 `ip:port` 重放——大幅缩小了重放攻击面。这与 TCP SYN cookie 取「分钟级」时间片的做法一致。

> 补充：cookie 只绑定了客户端的「IP+端口+时间」，并不绑定握手里的序列号或 socket id。这够用，因为它的职责只是「证明这个源地址能收包」，真正的连接参数协商在 `connect_on_handshake` 里做。

#### 4.2.4 代码实践

**实践目标**：亲手验证 cookie 的「分钟轮换」与「±1 容错」语义。

**操作步骤（源码阅读 + 推理型实践）**：

1. 读 [src/socket.rs:356-366](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L356-L366) 的 `compute_cookie`，确认它只依赖 `SALT`、`addr`、`offset` 三者（外加 `self.start_time`）。
2. 构造一个心智实验：假设 listener 在第 0 秒创建，握手 ②在第 30 秒发出（此时 `elapsed/60 = 0`，桶 0）。问：客户端在以下三个时刻回带 ③，校验能否通过？
   - 第 40 秒回带：`elapsed/60 = 0`，`offset=0` 算桶 0 → 通过。
   - 第 65 秒回带：`elapsed/60 = 1`，`offset=0` 算桶 1（不命中），`offset=-1` 算桶 0 → 通过。
   - 第 130 秒回带：`elapsed/60 = 2`，`offset=0` 算桶 2，`offset=-1` 算桶 1，都不是桶 0 → **不通过**，握手失败。
3. （可选，待本地验证）在 `compute_cookie` 入口加一行 `eprintln!` 打印 `timestamp` 与算出的 cookie，跑一次 `cargo run --bin udt_sender` / `udt_receiver`，观察②和③两次 `compute_cookie` 调用的 `timestamp` 是否落在同一桶或相邻桶。

**预期结果**：你能解释「为什么握手 RTT 必须小于约 2 分钟，否则合法客户端也会因为 cookie 过期而被拒绝」。

#### 4.2.5 小练习与答案

**练习 1**：如果两个不同的客户端恰好在同一分钟、同一 IP、同一端口先后发起连接（例如 NAT 复用），cookie 会一样吗？这会有问题吗？

**参考答案**：会一样——cookie 只由 `salt:ip:port:timestamp` 决定。但这不会直接出问题，因为后续 `new_connection` 会按 `(peer_socket_id, isn)` 在 `peers` 注册表里去重（见 4.1 的 `get_peer_socket`），真正的连接区分靠的是 socket id 与 ISN，而不是 cookie。cookie 只负责「证明源地址真实」。

**练习 2**：为什么 salt 用 `Lazy<String>` 而不是每次 `compute_cookie` 都新生成随机 salt？

**参考答案**：salt 是「服务端密钥」，必须在②生成和③校验之间保持不变，否则永远校验失败。`Lazy` 保证进程内只生成一次、之后恒定；同时进程重启会换 salt，使旧 cookie 失效，增强安全性。

---

### 4.3 MSS 协商与 1002 拒绝

#### 4.3.1 概念说明

握手不仅要验证身份（cookie），还要交换参数。最重要的两个是：

- **MSS（`max_packet_size`）**：单个数据包（含 16 字节包头）的最大长度。两端可能配置了不同的 MSS，UDT 的规则是**取较小值**——否则一端发出的大包会超过另一端的处理能力。
- **窗口（`max_window_size`）**：对端通告的流控窗口，约束在途未确认包数。

此外，握手包里还带着 `udt_version` 和 `socket_type`。如果两端协议版本或 socket 类型（流式 `Stream` vs 数据报 `Datagram`）对不上，listener 会用一个特殊的 `connection_type = 1002` 回绝请求。`1002` 是参考 C++ UDT 实现的「错误码」约定。

#### 4.3.2 核心流程

MSS 协商（在服务端 `connect_on_handshake` 里）：

\[
\text{MSS}_{\text{最终}} = \min(\text{MSS}_{\text{本地}},\ \text{MSS}_{\text{对端}})
\]

写成代码就是「谁小用谁」：

```
if 对端.max_packet_size > 本地.mss:
    对端.max_packet_size = 本地.mss      # 本地更小，用本地
else:
    本地.mss = 对端.max_packet_size      # 对端更小或相等，用对端
```

随后服务端把自己「能通告给对端的窗口」写成 `min(rcv_buf_size, flight_flag_size)`，连同协商后的 MSS 放进 ④发回。客户端在 ④的 `else` 分支里直接采用 `config.mss = hs.max_packet_size` 与 `config.flight_flag_size = hs.max_window_size`，于是双方收敛到同一组参数。

拒绝路径（在 `listen_on_handshake` 里，cookie 校验通过之后）：

```
if hs.udt_version != 本地.udt_version()  或  hs.socket_type != 本地.socket_type:
    hs.connection_type = 1002
    发回 hs
    return ConnectionRefused("configuration mismatch")
```

注意：`udt_version()` 在本实现里恒为常量 `4`（见下），所以版本检查实质是「要求对端也是版本 4」。

#### 4.3.3 源码精读

**MSS 协商（服务端）**：

[src/socket.rs:175-186](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L175-L186)：

```rust
if hs.max_packet_size > configuration.mss {
    hs.max_packet_size = configuration.mss;
} else {
    configuration.mss = hs.max_packet_size;
}
self.flow.write().unwrap().flow_window_size = hs.max_window_size;
hs.max_window_size =
    std::cmp::min(configuration.rcv_buf_size, configuration.flight_flag_size);
```

这里先取 MSS 较小值，再把「对端通告的窗口」存进本地 `flow`，然后把自己要通告的窗口写进 `hs`（取接收缓冲与 `flight_flag_size` 的较小值，与配置注释「`flight_flag_size` 不应小于 `rcv_buf_size`」呼应，见 [src/configuration.rs:14-17](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/configuration.rs#L14-L17)）。

**MSS 协商（客户端，采用对端值）**：

[src/socket.rs:470-472](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L470-L472)：

```rust
configuration.mss = hs.max_packet_size;
configuration.flight_flag_size = hs.max_window_size;
```

客户端直接信任服务端在 ④里回带的协商结果——因为服务端已经在上一段代码里把「双方较小值」算好放进了 `hs.max_packet_size`。

**版本与类型校验 + 1002 拒绝**：

[src/socket.rs:411-423](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L411-L423)：

```rust
let udt_version = self.configuration.read().unwrap().udt_version();
if hs.udt_version != udt_version || hs.socket_type != self.socket_type {
    let mut hs_response = hs.clone();
    hs_response.connection_type = 1002; // Error codes defined in C++ implementation
    let hs_packet = UdtControlPacket::new_handshake(hs_response, dest_socket_id);
    self.send_to(&addr, hs_packet.into()).await?;
    return Err(Error::new(ErrorKind::ConnectionRefused, "configuration mismatch"));
}
```

注意 `udt_version()` 的实现：

[src/configuration.rs:51-53](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/configuration.rs#L51-L53) 恒返回常量 `UDT_VERSION = 4`（[src/configuration.rs:6](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/configuration.rs#L6)）。所以版本检查目前等价于「对端也必须是 4」。`socket_type` 来自 `SocketType` 枚举（`Stream = 1`、`Datagram = 2`，见 [src/socket.rs:39-58](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L39-L58)），listener 与客户端都用 `SocketType::Stream` 建 socket（见 [src/connection.rs:41](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/connection.rs#L41) 与 [src/listener.rs:17](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/listener.rs#L17)），正常情况下必然匹配。

**握手包里这些字段的线上位置**——供你对照格式（详见 [u4-l3](u4-l3-control-packet.md)）：

[src/control_packet.rs:217-228](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L217-L228) 是 `HandShakeInfo` 结构；`connection_type` 在反序列化里从 [src/control_packet.rs:269](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/control_packet.rs#L269) 的 `i32::from_be_bytes(raw[20..24])` 解析（有符号 4 字节，所以能装下 `-1` 和 `1002`）。

> 一个值得留意的边角：客户端 `process_ctrl` 里 `if hs.connection_type > 0` 用的是「大于 0」而非「等于 1」。这意味着如果服务端回了一个 `1002`（也 `> 0`），客户端当前会把它当成「cookie 响应」走 ③的回带分支，而不是当作拒绝处理。这是本实现的一个已知粗糙点；完整的拒绝语义（让客户端收到 `1002` 后立即失败）目前**待本地验证/后续完善**。在做压测或对接其它 UDT 实现时，请把这一点记在心里。

#### 4.3.4 代码实践

**实践目标**：验证「MSS 取较小值」在两端配置不同时的行为。

**操作步骤（推理 + 待本地验证型实践）**：

1. 读 [src/socket.rs:175-181](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L175-L181) 的 if/else，确认无论本地与对端谁大，最终 `hs.max_packet_size` 与 `configuration.mss` 都会等于「较小的一方」。
2. 构造两个配置：
   - 客户端 `UdtConfiguration { mss: 1400, .. }`；
   - 服务端 `UdtConfiguration { mss: 1500, .. }`（默认）。
3. 推理：①里客户端带 `max_packet_size = 1400` 过去；服务端本地 `mss = 1500`，因 `1400 > 1500` 为假，走 `else` 把本地 `mss` 改成 `1400`，④回带 `max_packet_size = 1400`；客户端 ④分支把 `config.mss` 设为 `1400`。两端最终都是 1400。
4. （待本地验证）分别在 `src/bin/udt_sender.rs` 与 `udt_receiver.rs` 的配置构造处改成上述不同 MSS，跑一次收发，确认能正常连通且实际 payload 大小按 1400 走（可结合 [u4-l2](u4-l2-data-packet.md) 的 payload = MSS − 28 − header 计算）。

**预期结果**：无论谁配置得更小，连接成功后双方 `mss` 都收敛到那个较小值，避免任何一端发出超过对端承受力的包。

#### 4.3.5 小练习与答案

**练习 1**：MSS 协商为什么用「取较小值」而不是「取较大值」或「取平均值」？

**参考答案**：因为 MSS 是「单个包的最大尺寸」这一**硬上限**。如果取较大值，一端可能发出超过另一端 MSS（甚至超过路径 MTU）的包，导致对端无法处理或 IP 层分片，降低性能与可靠性。取较小值保证双方发出的包都不超过任意一端的能力。取平均值没有物理意义。

**练习 2**：listener 在哪一行检查 `accept_queue_size`？超了会怎样？

**参考答案**：在 [src/udt.rs:145-147](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L145-L147)，`new_connection` 里若 `queued_sockets.len() >= config.accept_queue_size`，返回 `Other` 错误 "Too many queued sockets"，本次握手不会创建新连接。

---

### 4.4 Udt::new_connection：登记 peers 与唤醒 accept

#### 4.4.1 概念说明

握手通过 cookie 与参数校验后，listener 还要真正「把这条连接生出来」并交给应用层的 `accept`。这件事不在 listener socket 上做，而是委托给全局引擎 `Udt::new_connection`。它要完成四件事：

1. **去重**：UDP 不可靠，握手包可能重复或重传。如果这条连接（同一对端 socket id + ISN）已经建过，不要重复建。
2. **创建新 socket S**：复用 listener 的 multiplexer（这样 S 和 listener 共享同一个 UDP socket 与收发 worker），并继承 listener 的配置。
3. **登记注册表**：把 S 写进全局 `sockets` 表，并把 `(对端 socket id, ISN) → S` 写进 `peers` 表，供以后去重。
4. **通知 accept**：把 S 的 id 塞进 listener 的 `queued_sockets`，并 `accept_notify.notify_one()` 唤醒可能在等待的 `accept`。

#### 4.4.2 核心流程

```
Udt::new_connection(listener_socket, peer, hs):
    # 1. 去重：是否已存在同 (peer socket id, isn) 的连接？
    if let Some(existing) = get_peer_socket(peer, hs.socket_id, hs.isn):
        if existing.status == Broken:
            从 listener.queued_sockets 移除 existing     # 旧连接已死，清理后继续建新
        else:
            用 existing 的配置重发一个 ct=-1 的握手        # 幂等回应重复握手
            return Ok                                    # 不建新连接
    # 2. 创建 S（复用 listener 的 mux、继承配置、用对端 isn）
    new_id = get_new_socket_id()
    if queued_sockets.len() >= accept_queue_size: return Err("Too many queued sockets")
    S = UdtSocket::new(new_id, hs.socket_type, isn=hs.isn, config=listener.config)
          .with_peer(peer, hs.socket_id)
          .with_listen_socket(listener.id, listener.mux)
    S.open()
    # 3. 发最终握手 ④（内部把 S 置 Connected）
    S_ref = S.connect_on_handshake(peer, hs)?
    # 4. 登记两张表 + 唤醒 accept
    peers[(hs.socket_id, S.isn)].insert(S.id)
    sockets[S.id] = S_ref
    listener.queued_sockets.insert(S.id)
    listener.accept_notify.notify_one()
```

#### 4.4.3 源码精读

**去重查询**：

[src/udt.rs:59-76](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L59-L76) ——`get_peer_socket`：在 `peers[(socket_id, initial_seq_number)]` 集合里找出 `peer_addr` 匹配的那个本地 socket。

**去重的两条分支**：

[src/udt.rs:100-132](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L100-L132)：

- [src/udt.rs:105-114](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L105-L114)：若已存在的连接是 `Broken`，把它从 `queued_sockets` 移除，然后**继续往下**走「建新连接」流程（这是「旧连接已断、客户端重连」的正常路径）。
- [src/udt.rs:115-131](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L115-L131)：否则用现有连接的配置重发一个 `connection_type = -1` 的握手给对端，`return Ok`——**不建新连接**。这处理「握手包重传/重复」：同一个客户端因为丢包重发的 ①或③，不会让服务端建出多条连接。

**创建 S 并复用 mux**：

[src/udt.rs:134-159](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L134-L159)。其中：

- [src/udt.rs:136-142](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L136-L142)：把 listener 的 `multiplexer`（`Weak`）`upgrade` 成 `Arc`，拿到共享的 UDP socket 与 worker。
- [src/udt.rs:145-147](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L145-L147)：`accept_queue_size` 积压上限检查。
- [src/udt.rs:149-156](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L149-L156)：用对端的 `socket_type`、**对端的 ISN**、listener 的配置克隆来 `new` 一个 socket，再 `.with_peer(...).with_listen_socket(...)`。这一步让 S 与 listener 共享同一个 multiplexer——这就是为什么「一个 listener 通常只对应一个 UDP socket」，却能为每个客户端服务（详见 [u3-l3](u3-l3-multiplexer.md)）。

**发 ④ + 登记表 + 唤醒 accept**：

[src/udt.rs:161-174](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L161-L174)：

```rust
let new_socket_ref = new_socket.connect_on_handshake(peer, hs.clone()).await?;
self.peers
    .entry((ns_peer_socket_id, ns_isn))
    .or_default()
    .insert(new_socket_ref.socket_id);
self.sockets.insert(ns_id, new_socket_ref);
listener_socket.queued_sockets.write().await.insert(ns_id);
listener_socket.accept_notify.notify_one();
```

- `connect_on_handshake` 发出 ④（见 4.1.3），并返回 `Arc<UdtSocket>`。
- `peers.entry((对端 id, 本地 isn))`：注意这里用的 `ns_isn` 是 S 自己的 ISN（[src/udt.rs:162](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L162)）。S 的 ISN 来自 `UdtSocket::new(..., Some(hs.initial_seq_number), ...)`，即**采用客户端在握手里报上来的 ISN**——这是本实现的一个设计选择，使得 `peers` 表的键 `(对端 socket id, isn)` 能稳定标识一条逻辑连接，用于上面的去重。
- `listener_socket.accept_notify.notify_one()`：这一句唤醒 [src/listener.rs:58-76](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/listener.rs#L58-L76) 里 `accept` 的 `notified().await`，让 `accept` 从 `queued_sockets` 取出 `ns_id` 并返回新连接。

#### 4.4.4 代码实践

**实践目标**：跟踪一次成功握手后，全局 `Udt` 三张表与 listener `queued_sockets` 的变化。

**操作步骤（源码阅读型实践）**：

1. 假设握手成功，按顺序列出 `new_connection` 末尾（[src/udt.rs:166-173](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L166-L173)）执行后各表的状态：
   - `peers[(peer_socket_id, S.isn)]` 新增 `S.id`；
   - `sockets[S.id] = Arc<S>`；
   - listener 的 `queued_sockets` 新增 `S.id`；
   - `accept_notify` 被 `notify_one()` 触发。
2. 回到 [src/listener.rs:58-76](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/listener.rs#L58-L76)，确认 `accept` 被唤醒后从 `queued_sockets.iter().next()` 取出最小 id、移除、再用 `Udt::get_socket` 换成 `SocketRef` 包成 `UdtConnection` 返回。
3. （可选）对照 [src/udt.rs:227-259](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L227-L259) 的 `garbage_collect_sockets`，思考：如果这条新连接之后变成 `Broken`，GC 会从 `queued_sockets` 里移除它（[src/udt.rs:233-241](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L233-L241)），此时若 `accept` 正好取出这个已 `Closed` 的 id 会怎样？（答：[src/listener.rs:79-84](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/listener.rs#L79-L84) 的 `get_socket` 因状态为 `Closed` 返回 `None`，`accept` 报 `Other` 错，需调用方处理。）

**预期结果**：你能解释「一次握手 = 在 `peers` 和 `sockets` 各加一条 + 在 listener 的 accept 队列加一条 + 唤醒一次 accept」。

#### 4.4.5 小练习与答案

**练习 1**：为什么新建的 S 要复用 listener 的 multiplexer，而不是自己开一个 UDP socket？

**参考答案**：因为客户端是冲着 listener 的固定 UDP 端口来的，握手包都落到那个端口对应的 UDP socket 上。如果 S 另开 UDP socket，它就收不到后续的数据包（对端还在往 listener 的端口发）。复用 mux 让 S 与 listener 共享同一个 UDP socket，由接收 worker 按包头里的 `dest_socket_id` 把包解复用分发到 S（`dest = S.id`）或 listener（`dest = 0`）。详见 [u3-l3](u3-l3-multiplexer.md)。

**练习 2**：`peers` 表的键为什么是 `(对端 socket id, ISN)` 而不是只用对端 socket id？

**参考答案**：同一台客户端（同一 socket id）可能先后发起多条逻辑连接，每条有不同的 ISN。用 `(socket id, ISN)` 作键能区分这些连接，避免把新连接误判为旧连接的重复握手。`get_peer_socket` 还会进一步用 `peer_addr` 匹配（[src/udt.rs:71](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L71)），双重确认。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个**源码阅读 + 推理型**综合任务：

**任务**：模拟一次完整的「客户端 A 连接 listener L」握手，回答以下问题，并把答案与源码行号对应起来。

1. **时序**：画出 4 个握手包的方向、`connection_type`、`syn_cookie`、`dest_socket_id`，并在每个箭头上标注触发它的函数名与文件:行号（参考 4.1.3）。
2. **cookie 容错**：假设 listener 在第 50 秒发出 ②（桶 0），客户端因网络抖动在第 95 秒才回带 ③。请用 [src/socket.rs:402-409](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L402-L409) 的判定式说明：此时 `offset=0` 算的是哪个桶？`offset=-1` 算的是哪个桶？cookie 能通过吗？如果把抖动改成 125 秒呢？
3. **参数协商**：客户端配置 `mss = 1450`，listener 用默认 `mss = 1500`。握手成功后，客户端 `config.mss` 最终是多少？请用 [src/socket.rs:175-181](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L175-L181) 与 [src/socket.rs:470-472](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L470-L472) 推导。
4. **注册表变化**：握手成功瞬间，全局 `Udt` 的 `peers`、`sockets` 两张表分别新增了什么？listener 的 `queued_sockets` 新增了什么？哪一行代码唤醒了 `accept`？（参考 4.4.3）
5. **去重场景**：如果客户端因为 ①丢包而重传了完全相同的 ①，listener 的 `listen_on_handshake` 会再算一次 cookie 并回 ②；但客户端随后补发的 ③到达时，`new_connection` 会走哪条分支避免重复建连？（参考 [src/udt.rs:115-131](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/udt.rs#L115-L131)）

**预期结果**：完成后，你应该能用一张图 + 一段话，向别人讲清楚「tokio-udt 的一条连接是如何安全、幂等、参数协商一致地建立起来的」。

> 提示：本任务全部基于静态源码阅读与推理即可完成；如需观察实际包流，可在 [src/socket.rs:389](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L389) 与 [src/socket.rs:403-405](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L403-L405) 加 `eprintln!` 打印 cookie，运行 `udt_sender`/`udt_receiver` 验证（待本地验证）。

## 6. 本讲小结

- UDT 的常规连接握手需要 **4 个握手包**，`connection_type` 取值依次为 `1 → 1 → -1 → -1`，分别由客户端 `connect`、listener `listen_on_handshake`（首轮）、客户端 `process_ctrl`（回带）、服务端 `connect_on_handshake`（最终配置）发出。
- **SYN cookie** 由 `compute_cookie` 用 `SHA-256(salt:ip:port:分钟级时间戳)` 取前 4 字节算出；salt 是进程级随机密钥，时间戳按分钟分桶轮换，使 cookie 大约每分钟变化、限制重放窗口。
- 校验时同时接受 `offset=0`（当前桶）和 `offset=-1`（上一桶），是为了**容错一次分钟边界跨越**，给握手 RTT 约 2 分钟的容忍窗口。
- **MSS 协商取较小值**（`connect_on_handshake` 的 if/else），客户端在 ④分支直接采用服务端回带的协商结果，双方收敛一致；窗口通告取 `min(rcv_buf_size, flight_flag_size)`。
- 版本号（恒为 4）或 socket 类型（`Stream`/`Datagram`）不匹配时，listener 回 `connection_type = 1002` 并返回 `ConnectionRefused`；注意客户端侧对 `1002` 的处理目前较粗糙（`>0` 即视为 cookie 响应），属已知边角。
- `Udt::new_connection` 负责握手成功后的收尾：去重（按 `(对端 id, ISN)` 查 `peers`，已存在则幂等重发、不重建）、创建复用 listener mux 的新 socket S、把 S 登记进 `sockets` 与 `peers`、塞进 `queued_sockets` 并 `accept_notify.notify_one()` 唤醒 `accept`。

## 7. 下一步学习建议

- 握手建立连接后，下一步自然是「如何关闭连接」。请继续学习 [u8-l2 关闭、linger 与垃圾回收](u8-l2-close-linger-gc.md)，看 `UdtSocket::close` 如何发 Shutdown 包、如何在 `linger_timeout` 内等待发送缓冲排空，以及 `garbage_collect_sockets` 如何回收 `Broken`/`Closing` 状态的 socket。
- 想深入理解「握手成功后双方如何开始收发数据」，可回顾 [u5-l1](u5-l1-send-queue-and-buffer.md) 与 [u5-l2](u5-l2-recv-queue-and-buffer.md) 的发送/接收通路，以及 [u6-l2](u6-l2-recv-and-ack.md) 的 ACK 生成——握手阶段初始化的那些游标（`last_sent_ack`、`curr_rcv_seq_number`）正是在那里被消费。
- 若对 rendezvous（会合）模式感兴趣，可在源码里搜索 `rendezvous` 关键字：本实现声明了该字段但尚未实现（`listen`/`accept` 会返回 `Unsupported`，见 [src/listener.rs:20-25](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/listener.rs#L20-L25) 与 [src/socket.rs:1119-1120](https://github.com/Distributed-EPFL/tokio-udt/blob/450cfcc04d7975c59eec8c0a3f130e0cb57b3285/src/socket.rs#L1119-L1120) 的 TODO），可作为二次开发的切入点。
