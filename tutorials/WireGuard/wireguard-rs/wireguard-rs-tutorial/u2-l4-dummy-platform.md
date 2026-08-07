# Dummy 纯测试平台

## 1. 本讲目标

本讲解决一个问题：**wireguard-rs 的协议核心不依赖任何真实操作系统，那么我们怎样在不创建真实 TUN 网卡、不绑定真实 UDP 端口的前提下，把整个 WireGuard 跑起来做测试？**

答案是 `platform/dummy` 模块提供的一组「纯软件」平台实现。读完本讲你应该能够：

1. 说清 dummy 平台在单元测试中扮演的角色——无副作用、可把两个 WireGuard 实例「背靠背」对接。
2. 看懂 `TunTest` 如何用 `std::sync::mpsc` 通道模拟一张 TUN 网卡的读写，以及 `TunFakeIO` 如何充当「内核那一端」。
3. 看懂 `PairBind::pair` 如何用两条交叉通道把两个实例的 UDP 收发对接，从而模拟「互联网」。
4. 理解 `UnitEndpoint` 为何可以是一个零字段的空类型，以及它如何满足 `Endpoint` trait。
5. 掌握用 dummy 编写纯软件握手 + 收发测试的方法，为后续（u7-l4）阅读 `test_pure_wireguard` 打基础。

## 2. 前置知识

本讲是 u2-l1（平台抽象 trait 设计）的直接落地，需要你已经了解：

- **平台抽象 trait**：`Tun` / `PlatformTun`、`UDP` / `PlatformUDP`、`Endpoint`、`Reader` / `Writer` / `Status` / `Owner` 这一族 trait 的方法签名与关联类型。dummy 模块就是这些 trait 的一套「测试用」实现。
- **关联类型 + 泛型**：协议核心写作 `WireGuard<T: Tun, B: UDP>`，编译期把具体平台类型钉死。dummy 平台就是把它实例化为 `WireGuard<dummy::TunTest, dummy::PairBind>`。
- **`std::sync::mpsc` 通道**：dummy 大量使用 `sync_channel`（有界同步通道）在内存里搬运报文。`send` 在通道满时阻塞，`recv` 在通道空时阻塞，这恰好模拟了真实 IO 的阻塞语义。

一个直觉性的对比：

| 真实平台（`platform/linux`） | 测试平台（`platform/dummy`） |
|---|---|
| `read`/`write` 走 `libc` 系统调用 | `read`/`write` 走 `mpsc` 通道 |
| TUN 网卡由 `/dev/net/tun` + ioctl 创建 | 「网卡」就是一对通道 |
| UDP 报文走内核网络栈 | 报文在两条交叉通道间倒手 |
| `Endpoint` 携带真实 IP/端口与 sticky 源 | `UnitEndpoint` 是零字段空类型 |
| 创建需要 root、有副作用 | 纯内存、无副作用、可重复 |

理解这张对照表，本讲剩下的事情就是「看 dummy 如何把右侧填出来」。

## 3. 本讲源码地图

本讲涉及的关键文件及其作用：

| 文件 | 作用 |
|---|---|
| [src/platform/dummy/mod.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/dummy/mod.rs) | dummy 平台入口，声明并统一导出 `endpoint`/`tun`/`udp` 三个子模块 |
| [src/platform/dummy/tun/mod.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/dummy/tun/mod.rs) | TUN 子模块入口，定义 `TunError` 并导出 `dummy`/`void` 子模块 |
| [src/platform/dummy/tun/dummy.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/dummy/tun/dummy.rs) | **`TunTest` 核心**：模拟 TUN 网卡，含 `TunFakeIO`/`TunReader`/`TunWriter`/`TunStatus` |
| [src/platform/dummy/udp.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/dummy/udp.rs) | **`PairBind` 核心**：用交叉通道把两个实例对接；另有 `VoidBind`/`VoidOwner` |
| [src/platform/dummy/endpoint.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/dummy/endpoint.rs) | **`UnitEndpoint`**：零字段空类型的 `Endpoint` 实现 |
| [src/wireguard/tests.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/tests.rs) | 用 dummy 搭两个实例做端到端回归的 `test_pure_wireguard` |

参考契约（已在 u2-l1 讲过，本讲只对照实现）：

| 契约文件 | dummy 实现它的类型 |
|---|---|
| [src/platform/tun.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/tun.rs) | `TunTest`(impl `Tun`/`PlatformTun`)、`TunReader`/`TunWriter`/`TunStatus` |
| [src/platform/udp.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/udp.rs) | `PairBind`(impl `UDP`/`PlatformUDP`)、`PairReader`/`PairWriter`/`VoidOwner` |
| [src/platform/endpoint.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/endpoint.rs) | `UnitEndpoint` |

## 4. 核心概念与源码讲解

### 4.1 为什么需要 dummy：无副作用的测试地基

#### 4.1.1 概念说明

协议核心 `src/wireguard/` 是纯逻辑：给它字节，它吐字节；给它密钥，它算握手。它本身不碰操作系统。但要把「握手 → 协商出会话密钥 → 加密发送 → 对端解密」这条完整链路跑一遍，总得有 IO 来源和去处。

`platform/linux` 提供了真实 IO，但它有三个测试不可接受的缺点：需要 root、会创建真实网卡和套接字（副作用）、依赖内核行为（难以控制时序）。dummy 平台的目标就是**用内存通道替换所有系统调用**，让整条链路在 `cargo test` 里可重复、可断言地跑起来。

模块头部的注释把意图说得很直白：

[src/platform/dummy/mod.rs:5-9](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/dummy/mod.rs#L5-L9) —— 注释说明 dummy 平台用于单元测试整个 WireGuard、配置接口和 UAPI 解析器。

#### 4.1.2 核心流程

dummy 平台为四个抽象各提供一个测试替身（test double）：

```
Tun 抽象      →  TunTest       (一张「通道网卡」+ 一个「内核端」TunFakeIO)
UDP 抽象      →  PairBind      (两条交叉通道，把两个实例的 UDP 对接)
Endpoint 抽象 →  UnitEndpoint  (零字段空类型，无视地址)
Status 抽象   →  TunStatus     (首次返回 Up(1420)，之后永久阻塞)
```

测试的典型拓扑：两个 `WireGuard<dummy::TunTest, dummy::PairBind>` 实例，各自的 `PairBind` 互相连接，各自的 `TunTest` 由测试代码用 `TunFakeIO` 喂数据 / 取数据。这样就在一个进程内搭出了「两个对等方 + 一条虚拟网络」。

#### 4.1.3 源码精读

dummy 通过 `pub use ... ::*` 把三个子模块的符号一次性拍平导出：

[src/platform/dummy/mod.rs:11-13](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/dummy/mod.rs#L11-L13) —— `pub use endpoint::*; pub use tun::*; pub use udp::*;`，使得测试代码只需 `use super::dummy;` 即可拿到 `dummy::TunTest`、`dummy::PairBind`、`dummy::UnitEndpoint`。

注意一个工程细节：dummy 只在测试场景下使用，它**不实现** `PlatformTun::create` / `PlatformUDP::bind` 的真实创建逻辑（这俩在 dummy 里直接返回错误，见 4.2.4 与 4.3.4），因为测试是用 `TunTest::create` 和 `PairBind::pair` 这两个**自定义构造函数**手动装配的，不走 UAPI 触发的 `create`/`bind` 路径。

### 4.2 TunTest：用通道模拟一张 TUN 网卡

#### 4.2.1 概念说明

真实的 TUN 网卡有两侧：一侧是用户态进程（WireGuard），另一侧是内核网络栈。进程 `write` 到 TUN 的报文，相当于「内核收到一个本机发出的明文 IP 包」；进程从 TUN `read` 出来的，是「内核要交给本机处理的明文 IP 包」。

dummy 用 `TunFakeIO` 扮演「内核那一端」：测试代码调用 `TunFakeIO::write` 把一个明文 IP 包塞进网卡（模拟「内核收到了一个要去对端的包」），调用 `TunFakeIO::read` 把网卡里解密好的包取出来（模拟「内核收到了一个发给本机的包」）。WireGuard 这一侧则用 `TunReader`/`TunWriter` 与之通过通道交互。

四个类型的关系：

| 类型 | 角色 | 对应真实一侧 |
|---|---|---|
| `TunTest` | trait 锚点（impl `Tun`/`PlatformTun`），本身是空结构体 | 「网卡类型」 |
| `TunReader` | WireGuard 侧读（明文 IP 包入站） | 进程读 TUN |
| `TunWriter` | WireGuard 侧写（明文 IP 包出站） | 进程写 TUN |
| `TunFakeIO` | 测试侧「内核端」，喂数据 / 取数据 | 内核网络栈 |
| `TunStatus` | 报告 Up/Down 事件 | netlink 事件 |

#### 4.2.2 核心流程

`TunTest::create` 一次性建好两条通道，把四个对象绑定到同一个 `id`：

```
           (tx1, rx1) 通道 A            (tx2, rx2) 通道 B
TunFakeIO ───────► ───────────────► TunReader        (测试→WG：测试 write → rx1 → TunReader.read)
TunWriter  ◄────── ───────────────◄ TunFakeIO.rx?    (WG→测试：TunWriter.write → tx2 → TunFakeIO.read)
```

精确的通道走向是（对照 4.2.3 的字段赋值）：

- `TunFakeIO.tx = tx1`，`TunReader.rx = rx1`：测试调 `fake.write(p)` 把包从 `tx1` 送入，WireGuard 的 `tun_worker` 在 `TunReader::read` 里从 `rx1` 取出。这模拟**入站明文**。
- `TunWriter.tx = tx2`，`TunFakeIO.rx = rx2`：WireGuard 解密后调 `TunWriter::write` 把包从 `tx2` 送出，测试在 `TunFakeIO::read` 里从 `rx2` 取出。这模拟**出站明文交给本机**。

注意上面用「入站/出站」是相对测试视角而言；`TunReader` 在协议核心里其实对应「从 TUN 读出明文 IP 包进入加密管道」的方向（见 u3-l2 的 `tun_worker`）。

#### 4.2.3 源码精读

先看四个结构体的字段，它们都用同一个 `id: u32` 做调试日志标识：

[src/platform/dummy/tun/dummy.rs:25-46](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/dummy/tun/dummy.rs#L25-L46) —— `TunTest`（空）、`TunFakeIO{ id, store, tx, rx }`、`TunReader{ id, rx }`、`TunWriter{ id, store, tx: Mutex<SyncSender> }`。`store` 标志决定写出去的包是否真的送进通道（关闭时可模拟「丢弃」，见 `VoidTun` 的设计意图）。

`TunReader::read` 实现了 u2-l1 中 `Reader::read` 的契约，`offset` 参数用于就地预留传输头前缀（与 Linux 实现一致）：

[src/platform/dummy/tun/dummy.rs:86-105](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/dummy/tun/dummy.rs#L86-L105) —— 从 `self.rx.recv()` 阻塞收一个 `Vec<u8>`，按 `min(buf.len() - offset, msg.len())` 截断，拷到 `buf[offset..]`，返回长度。通道断开时返回 `TunError::Disconnected`。

`TunWriter::write` 实现 `Writer::write`，把明文 IP 包送出网卡：

[src/platform/dummy/tun/dummy.rs:107-127](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/dummy/tun/dummy.rs#L107-L127) —— 当 `self.store` 为真时，把 `src` 转成 `Vec<u8>` 经 `tx` 通道发出（模拟「内核收到了发给本机的明文包」）；`store` 为假时直接丢弃返回 `Ok(())`。`store` 关闭的语义对应基准测试中「只测入站、不关心出站」的场景。

`TunStatus::event` 是 dummy 里最「取巧」的一处——它模拟网卡 Up 事件：

[src/platform/dummy/tun/dummy.rs:129-142](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/dummy/tun/dummy.rs#L129-L142) —— 首次调用返回 `TunEvent::Up(1420)`（硬编码 MTU 1420），之后进入 `loop { thread::sleep(一小时) }` 永久阻塞，模拟「再无状态变化」。真实平台用 netlink 被动收事件（见 u2-l2），dummy 则用「先吐一次 Up，再睡死」来近似。

`TunTest::create` 是 dummy 自定义的构造函数（**不是** trait 方法），返回 4 元组：

[src/platform/dummy/tun/dummy.rs:162-192](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/dummy/tun/dummy.rs#L162-L192) —— 建两条 `sync_channel`：`store` 为真时容量 32，为假时容量 1；随机生成 `id`；分别装配 `TunFakeIO`（持 `tx1`+`rx2`）、`TunReader`（持 `rx1`）、`TunWriter`（持 `tx2`）、`TunStatus`（`first=true`）。通道容量的设计：开启 `store`（要收发数据）时给 32 足够缓冲；关闭时退化为容量 1 的「一进一出」。

#### 4.2.4 代码实践

**实践目标**：理解 dummy 为何把 `PlatformTun::create` 留空（返回错误）。

**操作步骤**：

1. 打开 [src/platform/dummy/tun/dummy.rs:194-200](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/dummy/tun/dummy.rs#L194-L200)，确认 `impl PlatformTun for TunTest` 的 `create` 直接返回 `Err(TunError::Disconnected)`。
2. 对比 [src/platform/linux/tun.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/linux/tun.rs) 中 Linux 实现的 `create`（走 `/dev/net/tun` + ioctl）。
3. 在 `TunTest::create`（自定义构造函数，L162）旁边写一行注释，说明它与 trait 方法 `PlatformTun::create`（L197）的区别。

**需要观察的现象**：dummy 的 `PlatformTun::create` 永远不会在测试中被调用——因为测试直接调 `TunTest::create(true)` 拿到四元组手动装配，而非走 UAPI 触发的 `plt::Tun::create` 路径。

**预期结果**：你能在注释里写清「测试用 `TunTest::create` 装配，trait 的 `create` 仅满足编译、运行时不调用」。

**待本地验证**：如果你尝试在测试里调用 `<TunTest as PlatformTun>::create("wg0")`，应直接得到 `TunError::Disconnected`。

#### 4.2.5 小练习与答案

**练习 1**：`TunStatus::event` 为什么首次返回 `Up(1420)` 后就 `loop { sleep }`？如果它像真实 netlink 那样阻塞等待，会发生什么？

**参考答案**：协议核心需要一个初始的 `Up` 事件来设置 MTU 并启动路由器；dummy 用硬编码 1420 模拟「网卡已就绪」。之后没有真实事件可等，于是用一个超长 `sleep` 模拟「阻塞但永不返回新事件」。如果它直接 `return Err`，会触发上游错误处理；如果它每秒都返回 Up，又会反复重置 MTU——所以「先 Up 一次再睡死」是最贴近真实行为的近似。

**练习 2**：`TunReader::read` 里 `min(buf.len() - offset, msg.len())` 这一行，`buf.len() - offset` 何时会比 `msg.len()` 小？这意味着什么？

**参考答案**：当传入的 `buf` 总长减去 `offset` 前缀后，剩余空间不足以容纳整个 `msg` 时。这意味着对端写进通道的包比预留缓冲区还大，dummy 选择**截断**而非报错。这是一种宽松的测试行为；在真实 Linux 实现里，`read` 的缓冲区按 MTU 分配，正常不会出现截断。

### 4.3 PairBind：用交叉通道把两个实例对接

#### 4.3.1 概念说明

握手和传输报文都跑在 UDP 上。要测试「实例 A 发给实例 B」，就需要把 A 的发送端连到 B 的接收端、B 的发送端连到 A 的接收端。

`PairBind::pair()` 就是干这件事的：它返回**两组** reader/writer，把它们用两条通道**交叉**连接——A 的 writer 写进通道 2，B 的 reader 从通道 2 读；B 的 writer 写进通道 1，A 的 reader 从通道 1 读。于是两个 WireGuard 实例就像被一根虚拟网线连了起来。

dummy 里 UDP 侧还有两个配角：

- `VoidBind`：一个「黑洞」实现，`read` 永远返回 0 字节、`write` 永远丢弃。源码注释提到它用于基准测试/剖析入站路径（类似 TUN 侧被注释掉的 `VoidTun`）。
- `VoidOwner`：`PairBind` 的 `PlatformUDP::Owner` 关联类型，`set_fwmark` 空实现、`get_port` 返回 0，仅满足编译。

#### 4.3.2 核心流程

`PairBind::pair` 的连接拓扑（关键在于 writer 的 `send` 指向**对方**的 receiver）：

```
           tx1 ──► rx1                tx2 ──► rx2
实例A:  PairWriter.send=tx2       PairReader.recv=rx1
实例B:  PairWriter.send=tx1       PairReader.recv=rx2

即：A.write ──tx2──► rx2──► B.read     （A 发，B 收）
     B.write ──tx1──► rx1──► A.read     （B 发，A 收）
```

注意通道是**交叉**的：A 的 writer 持有的是 `tx2`（对应 B 的 reader），这正是「网线」的接法。两条 `sync_channel(128)` 提供有界缓冲，缓冲满时 `send` 阻塞，模拟真实 socket 的反压。

#### 4.3.3 源码精读

`PairReader` 与 `PairWriter` 的字段结构：

[src/platform/dummy/udp.rs:78-83](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/dummy/udp.rs#L78-L83) 和 [src/platform/dummy/udp.rs:123-128](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/dummy/udp.rs#L123-L128) —— `PairReader{ id, recv: Arc<Mutex<Receiver<Vec<u8>>>> }`、`PairWriter{ id, send: Arc<Mutex<SyncSender<Vec<u8>>>> }`。`Reader`/`Writer` 都派生 `Clone`，因为协议核心可能为每个 UDP reader 起一个工作线程（见 u3-l3 `add_udp_reader`），需要多份克隆。`Arc<Mutex<…>>` 包裹让多个克隆共享同一个通道端点且线程安全。

`PairReader::read` 实现 `udp::Reader::read`，返回 `(读到的字节数, Endpoint)`：

[src/platform/dummy/udp.rs:85-104](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/dummy/udp.rs#L85-L104) —— `recv` 拿到一个 `Vec<u8>`，拷进 `buf`，返回 `(len, UnitEndpoint{})`。注意它**永远返回同一个 `UnitEndpoint`**，因为 dummy 里所有报文的「来源」都是同一个虚拟对端。

`PairWriter::write` 实现 `udp::Writer::write`，签名要求 `dst: &mut E`（u2-l1 提到的 sticky 源支持）：

[src/platform/dummy/udp.rs:106-121](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/dummy/udp.rs#L106-L121) —— 把 `buf` 转成 `Vec<u8>`（获得所有权，因为要送进 `Vec<u8>` 通道），经 `self.send` 发出，无视 `dst`。`dst` 是 `&mut` 仅是为了满足 Linux 实现里「粘连源失效时清源重发」的契约，dummy 无真实地址，直接忽略。

`PairBind::pair` 的核心——两条交叉通道：

[src/platform/dummy/udp.rs:133-169](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/dummy/udp.rs#L133-L169) —— 建 `(tx1,rx1)` 与 `(tx2,rx2)` 两条 `sync_channel(128)`；返回的两个端点里，第一个的 writer 持 `tx2`、reader 持 `rx1`，第二个的 writer 持 `tx1`、reader 持 `rx2`。两个随机 `id`（`OsRng.gen()`）仅用于日志区分。这就是「背靠背」连接的全部魔法。

`impl UDP for PairBind` 把关联类型接到 `PairReader`/`PairWriter`/`UnitEndpoint`：

[src/platform/dummy/udp.rs:172-177](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/dummy/udp.rs#L172-L177) —— `type Endpoint = UnitEndpoint; type Reader = PairReader<Self::Endpoint>; type Writer = PairWriter<Self::Endpoint>;`，让 `WireGuard<_, PairBind>` 能编译通过。

#### 4.3.4 代码实践

**实践目标**：理解 dummy 为何把 `PlatformUDP::bind` 也留空。

**操作步骤**：

1. 阅读 [src/platform/dummy/udp.rs:191-196](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/dummy/udp.rs#L191-L196)，确认 `impl PlatformUDP for PairBind` 的 `bind` 直接返回 `Err(BindError::Disconnected)`，而 `Owner` 关联类型指向空实现的 `VoidOwner`（L179-189）。
2. 在 `PairBind::pair` 上方写注释：为什么测试用 `pair()` 手动装配，而不用 `bind(port)`？

**需要观察的现象**：真实平台 `bind` 返回 `(readers, writer, owner)` 三元组并由配置层装配；dummy 的 `pair` 返回两个**独立**的 `(reader, writer)` 二元组，由测试代码分别喂给两个实例的 `set_writer` / `add_udp_reader`。

**预期结果**：注释能写清「`pair()` 表达的是『两个实例互连』这一测试意图，而 `bind(port)` 表达的是『单实例绑一个真实端口』，二者语义不同，故 dummy 不复用 `bind`」。

#### 4.3.5 小练习与答案

**练习 1**：`PairReader` 和 `PairWriter` 都派生了 `Clone`，且内部用 `Arc<Mutex<…>>`。为什么要 `Clone` 又为什么要 `Mutex`？

**参考答案**：`Clone` 是因为协议核心在 `add_udp_reader` 里会为每个 reader 起一个 `udp_worker` 线程，可能需要多份 reader 克隆（同理 writer 也可能被多处持有）。`Mutex` 是因为底层 `SyncSender`/`Receiver` 不是 `Sync` 的，要在线程间共享就必须用 `Mutex` 包一层；`Arc` 则让所有克隆共享同一个通道端点。

**练习 2**：如果把 `PairBind::pair` 里两条通道的容量从 128 改成 1，`test_pure_wireguard` 还能跑通吗？

**参考答案**：大概率会出现阻塞或显著变慢，但不一定失败——取决于生产/消费速度。容量 128 给了足够缓冲，让发送方在接收方忙于加密时不被立刻卡住；改成 1 会让 `send` 频繁阻塞，把并发加密的吞吐压低。这是一个观察「通道反压」如何模拟 socket 行为的好切入点。**待本地验证**具体是否仍通过。

### 4.4 UnitEndpoint：零字段的对端地址

#### 4.4.1 概念说明

真实平台里 `Endpoint` 携带大量信息：对端 IP、端口，以及 Linux 的 sticky 源辅助数据（见 u2-l3）。这些在 dummy 里都没有意义——因为「对端」永远是同一个内存里的兄弟实例，没有地址概念。

于是 `UnitEndpoint` 退化成一个**零字段**的空结构体。它的存在纯粹是为了满足 `Endpoint` trait 的类型约束，让 `WireGuard<_, PairBind>` 的关联类型链能闭合。

#### 4.4.2 核心流程

`Endpoint` trait（u2-l1）要求三个方法：`from_address`、`into_address`、`clear_src`。`UnitEndpoint` 的实现策略是：

| 方法 | 实现 | 含义 |
|---|---|---|
| `from_address(_)` | 丢弃参数，返回新的 `UnitEndpoint{}` | 无视真实地址 |
| `into_address(&self)` | 返回硬编码 `"127.0.0.1:8080"` | 占位，仅供日志/调试 |
| `clear_src(&mut self)` | 空函数体 | 无 sticky 源可清 |

#### 4.4.3 源码精读

整个类型只有 24 行：

[src/platform/dummy/endpoint.rs:5-24](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/dummy/endpoint.rs#L5-L24) —— `UnitEndpoint{}` 是零字段结构体，派生 `Clone, Copy`；`from_address` 丢弃入参返回空值，`into_address` 解析返回一个固定的 `127.0.0.1:8080`，`clear_src` 为空。`new()` 是个便捷构造函数。

注意 `into_address` 的 `127.0.0.1:8080` **不是**真实使用的地址——它只在调试输出（如日志、`Display`）里出现，确保协议核心某处若调用 `into_address` 不会 panic。这也是为什么 `test_pure_wireguard` 里设置对端端点用的是 `dummy::UnitEndpoint::new()`（见 4.5.3）。

#### 4.4.4 代码实践

**实践目标**：验证 `UnitEndpoint` 的「无地址」特性不会破坏协议核心的端点管理。

**操作步骤**：

1. 在 [src/platform/dummy/endpoint.rs:13](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/platform/dummy/endpoint.rs#L13) 的 `into_address` 旁加注释，说明这个 `127.0.0.1:8080` 仅为占位。
2. 用 `Grep` 在 `src/wireguard/` 里搜索 `into_address` 的调用点，确认它在数据面热路径上是否被调用。

**需要观察的现象**：握手成功后，对端端点是从**收到的报文**里学到的（`from_address`），而 dummy 里 `from_address` 永远返回同一个空 `UnitEndpoint`，所以「学到端点」这件事在 dummy 里其实是无操作。

**预期结果**：能解释「dummy 用同一个空 Endpoint 共享给所有 peer，因此 `PairBind` 才能把报文无差别地投递到对端实例」。

#### 4.4.5 小练习与答案

**练习 1**：`UnitEndpoint` 派生了 `Copy`。这对它在协议核心里的使用（比如 `set_endpoint(UnitEndpoint)` 按值传入）有什么好处？

**参考答案**：`Copy` 使得 `UnitEndpoint` 可以无脑按值复制，无需 `Clone()` 调用，也无需 `&self` 借用。因为它是零字段，复制代价为零。协议核心里端点经常被传来传去（如路由器里缓存对端端点），`Copy` 让这些代码写起来就像在传整数。

**练习 2**：真实 Linux 平台的 `LinuxEndpoint` 有 `dst`（目的地址）和 `info`（sticky 源）两个字段（见 u2-l3）。dummy 的 `clear_src` 为空，这意味着 `PairWriter::write` 里 `dst` 失效时不会真的「清源重发」。这在测试里会带来偏差吗？

**参考答案**：不会影响正确性测试。sticky 源失效重发是 Linux 特有的「对端漫游」场景优化，dummy 用一条确定性的内存通道，永远不存在「源地址失效」，所以 `clear_src` 无操作是自洽的。代价是 dummy 无法测试漫游这一路径——这是 dummy 抽象的固有边界。

## 5. 综合实践

本实践要求你**完整复刻** `test_pure_wireguard` 的「装配」部分（但不发送数据），把本讲三个最小模块串起来：用 `TunTest` 建两张网卡、用 `PairBind::pair` 连接两个实例、用 `UnitEndpoint` 设置对端，最后断言双方 peer 都被成功添加。

下面是参照 [src/wireguard/tests.rs:71-137](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/tests.rs#L71-L137) 抽取的最小装配代码（**示例代码**，可直接放进一个新的 `#[test]`）：

```rust
// 示例代码：仅装配两个互连实例并配置密钥，不发送任何数据
use super::dummy;
use super::wireguard::WireGuard;
use x25519_dalek::{PublicKey, StaticSecret};

#[test]
fn test_dummy_pair_setup_only() {
    // 1) 用 TunTest 建两张「通道网卡」
    let (_, tun_reader1, tun_writer1, _) = dummy::TunTest::create(true);
    let (_, tun_reader2, tun_writer2, _) = dummy::TunTest::create(true);

    // 2) 构造两个 WireGuard 实例，类型钉死为 dummy 平台
    let wg1: WireGuard<dummy::TunTest, dummy::PairBind> = WireGuard::new(tun_writer1);
    wg1.add_tun_reader(tun_reader1);
    wg1.up(1500);

    let wg2: WireGuard<dummy::TunTest, dummy::PairBind> = WireGuard::new(tun_writer2);
    wg2.add_tun_reader(tun_reader2);
    wg2.up(1500);

    // 3) 用 PairBind::pair 把两个实例的 UDP 「背靠背」对接
    let ((bind_reader1, bind_writer1), (bind_reader2, bind_writer2)) = dummy::PairBind::pair();
    wg1.set_writer(bind_writer1);
    wg2.set_writer(bind_writer2);
    wg1.add_udp_reader(bind_reader1);
    wg2.add_udp_reader(bind_reader2);

    // 4) 生成两对 (sk, pk)
    let sk1 = StaticSecret::from([0u8; 32]); // 示例里用全零，真实测试用随机值
    let sk2 = StaticSecret::from([0u8; 32]);
    let pk1 = PublicKey::from(&sk1);
    let pk2 = PublicKey::from(&sk2);

    // 5) 互相添加对端公钥，再设置本端私钥
    assert!(wg1.add_peer(pk2), "wg1 应成功添加 pk2");
    assert!(wg2.add_peer(pk1), "wg2 应成功添加 pk1");
    wg1.set_key(Some(sk1));
    wg2.set_key(Some(sk2));

    // 6) 断言双方 peer 表里确实有对方
    {
        let peers1 = wg1.peers.read();
        let peers2 = wg2.peers.read();
        assert!(peers1.get(&pk2).is_some(), "wg1 的 peer 表里应能查到 pk2");
        assert!(peers2.get(&pk1).is_some(), "wg2 的 peer 表里应能查到 pk1");
    }
}
```

**操作步骤**：

1. 把上面的代码放进 `src/wireguard/tests.rs`（与 `test_pure_wireguard` 并列）。
2. 注意 `add_peer` 在 peer 已存在时会返回 `false`（参见 [src/wireguard/wireguard.rs:205](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/wireguard.rs#L205)），所以示例里两个公钥必须不同；上面为简洁用了全零，**实际请替换成两个不同的随机私钥**（可照搬 `test_pure_wireguard` 里那两个硬编码 `StaticSecret::from([...])`）。
3. 运行 `cargo test test_dummy_pair_setup_only`。

**需要观察的现象**：测试应快速通过——因为没有发送数据，不会触发真实握手。`add_peer` 返回 `true`，`peers.get(&pk)` 返回 `Some`。

**预期结果**：你得到一个最小可运行的「双实例装配」测试，证明三个 dummy 组件（`TunTest`/`PairBind`/`UnitEndpoint`）足以让协议核心进入「就绪、可收发」状态，而无需任何 root 权限或真实 IO。

**待本地验证**：若使用全零私钥，`add_peer` 的断言可能仍通过（peer 添加不依赖私钥有效性），但握手阶段会因零共享密钥被拒（见 u4-l3）。所以本实践「只配置不发送」是有意为之。

## 6. 本讲小结

- dummy 平台是 `platform/` trait 的一套**纯内存测试替身**，让整个 WireGuard 协议核心能在 `cargo test` 里无副作用地端到端跑通。
- `TunTest` 用两条 `sync_channel` 模拟一张 TUN 网卡：`TunReader`/`TunWriter` 是 WireGuard 侧，`TunFakeIO` 是测试侧「内核端」；`TunStatus` 首次返回 `Up(1420)` 后永久阻塞。
- `PairBind::pair` 用两条**交叉**的 `sync_channel(128)` 把两个实例的 UDP 收发对接，模拟一根虚拟网线；`PairReader`/`PairWriter` 用 `Arc<Mutex<…>>` 共享通道端点并派生 `Clone`。
- `UnitEndpoint` 是零字段空类型，`from_address` 无视地址、`into_address` 返回占位 `127.0.0.1:8080`、`clear_src` 为空——dummy 没有真实地址概念。
- dummy 故意把 `PlatformTun::create` / `PlatformUDP::bind` 留空（返回错误），因为测试用自定义的 `TunTest::create` / `PairBind::pair` 手动装配，不走 UAPI 触发的创建路径。
- dummy 的固有边界：无法测试真实 IO 行为（如 sticky 源漫游、socket 反压细节），这些归 Linux 平台实现。

## 7. 下一步学习建议

- **下一步进入协议核心的 IO 工作线程**：本讲只装配了实例，还没让数据真正流动。建议进入 u3-l1（WireGuard 胶水层与设备状态）和 u3-l2/u3-l3（TUN/UDP 工作线程），看 `tun_worker`/`udp_worker` 如何消费 dummy 提供的 reader。
- **回到完整端到端测试**：学完 u3 后重读 [src/wireguard/tests.rs](https://github.com/WireGuard/wireguard-rs/blob/7d84ef9064559a29b23ab86036f7ef62b450f90c/src/wireguard/tests.rs) 的 `test_pure_wireguard`，结合 `make_packet`（L15-56）看测试如何用 `pnet` 构造 IP 报文、用 `TunFakeIO::write/read` 收发并断言「按序、不改」。
- **测试体系全景**：u7-l4 会系统讲解项目测试策略，包括 proptest 属性测试与 Queue 并发模糊测试，届时你会更清楚 dummy 在整个测试体系中的位置。
