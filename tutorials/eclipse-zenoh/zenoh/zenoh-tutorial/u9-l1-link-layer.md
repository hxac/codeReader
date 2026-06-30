# Link 层抽象与各链路实现

## 1. 本讲目标

本讲进入 Zenoh 内核的「最底层」——**Link 层**。它在协议栈中的位置是：

```
公开 API (zenoh::Session)
   ↓
Runtime / 路由 (Gateway / Face / HAT)        ← u7 / u8 讲过
   ↓
传输层 TransportManager (unicast / multicast) ← u9-l2 要讲
   ↓
Link 层 (Link / LinkManager)                  ← 本讲
   ↓
真实字节 (TCP / UDP / TLS / QUIC / WS ...)
```

学完本讲，你应该能够：

1. 说清楚 `Link` 与 `LinkManager`（unicast / multicast）这对抽象各自承担什么职责。
2. 掌握 `LocatorInspector` 如何仅凭一条 locator（如 `tcp/127.0.0.1:7447`）判断该链路**是否可靠**、**是否支持多播**。
3. 读懂 TCP / UDP 链路的实现，知道每种链路的可靠性、多播能力、默认 MTU 差异，并理解 locator 协议串（`tcp/`、`udp/`）是如何被分派到对应 `LinkManagerBuilder` 的。
4. 理解本次（io_uring 支持）为 `LinkUnicastTrait` 新增的 `get_fd` 能力：它为何要把底层 socket 的原始文件描述符暴露给上层，以及为何不同链路有的能给出 fd、有的必须 `bail!` 并回退到 tokio 读取路径。

## 2. 前置知识

在进入本讲前，你需要先具备以下直觉（前置讲义已建立）：

- **Runtime 是会话的运行时核心**（u7-l1）：`zenoh::open` 的执行链是 `RuntimeBuilder::build → Session::init → Runtime::start`，而真正「打开一条传输」的动作（`open_transport_unicast` / `multicast`）发生在 Runtime 内部，由 `TransportManager` 完成。本讲讲的 Link 层，正是 `TransportManager` 往下要用的零件。
- **locator 与 endpoint**（u2、u7-l3）：Zenoh 用形如 `tcp/127.0.0.1:7447` 的字符串描述一个网络端点，斜杠前是**协议串**（`tcp`/`udp`/`tls`/`quic`/`ws`…），斜杠后是地址。Link 层的关键工作之一，就是看协议串决定用哪条链路实现。
- **可靠性与 QoS**（u3-l3）：一条消息是否「可丢弃」由 `droppable = !reliable || (cc==Drop)` 决定。本讲会看到「链路本身是否可靠」这个更底层的概念——它是 `Reliable`/`BestEffort` 在线路层的根。

两个术语先澄清：

- **unicast（单播）**：点对点，一条链路只连两个端点（TCP 是典型）。
- **multicast（多播）**：一对多，一条链路向一组节点同时投递（UDP 多播是典型，用于 scouting 的节点发现）。

关于第 4 个目标，再补一个术语：**raw file descriptor（RawFd）** 是 Linux/Unix 内核给每个打开的「文件」（socket 也是文件）分配的一个整数编号（如 `3`、`7`）。tokio 把 socket 封装成 `TcpStream`/`UdpSocket` 后，这个编号藏在内部；而 Linux 的 io_uring 异步 I/O 接口需要直接拿这个编号来注册缓冲、提交读请求。`get_fd` 就是把藏起来的 fd 重新交出来的通道。

## 3. 本讲源码地图

本讲涉及的关键文件（按「从抽象到具体」排序）：

| 文件 | 作用 |
|------|------|
| [io/zenoh-link/src/lib.rs](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-link/src/lib.rs) | **门面/分派层**：定义 `LinkKind`、聚合 `LocatorInspector`、`LinkManagerBuilderUnicast/Multicast`，把协议串分派到具体链路实现；并聚合贯穿各链路的 `uring` feature |
| [io/zenoh-link-commons/src/lib.rs](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-link-commons/src/lib.rs) | 抽象骨架：`Link` 结构体、`LocatorInspector` trait、`ConfigurationInspector` trait |
| [io/zenoh-link-commons/src/unicast.rs](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-link-commons/src/unicast.rs) | 单播抽象：`LinkManagerUnicastTrait`、`LinkUnicastTrait`（含本次新增的 `get_fd`）、`NewLink` |
| [io/zenoh-link-commons/src/multicast.rs](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-link-commons/src/multicast.rs) | 多播抽象：`LinkManagerMulticastTrait`、`LinkMulticast` 及其 `send/recv`（内含 Zenoh080 编解码） |
| [io/zenoh-links/zenoh-link-tcp/src/lib.rs](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-links/zenoh-link-tcp/src/lib.rs) | TCP 链路：协议常量、`TcpLocatorInspector`、MTU/可调参数 |
| [io/zenoh-links/zenoh-link-tcp/src/unicast.rs](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-links/zenoh-link-tcp/src/unicast.rs) | TCP 链路实现：`LinkUnicastTcp`、`LinkManagerUnicastTcp`、accept 循环、`get_fd` |
| [io/zenoh-links/zenoh-link-udp/src/lib.rs](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-links/zenoh-link-udp/src/lib.rs) | UDP 链路：协议常量、`UdpLocatorInspector`、按 OS 区分的 MTU |
| [io/zenoh-links/zenoh-link-udp/src/unicast.rs](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-links/zenoh-link-udp/src/unicast.rs) | UDP 单播实现：`LinkUnicastUdp`（三种 variant）、按 variant 区分的 `get_fd` 回退语义 |
| [io/zenoh-links/zenoh-link-udp/src/multicast.rs](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-links/zenoh-link-udp/src/multicast.rs) | UDP 多播实现：`LinkMulticastUdp`（双 socket：写用单播、读用多播） |

> 这两个 crate 都是**内部 crate**（文件头都有 `⚠️ This crate is intended for Zenoh's internal use.`）。写应用不应直接依赖，但理解内核原理时它们是主角。

## 4. 核心概念与源码讲解

### 4.1 Link / LinkManager 抽象（unicast 与 multicast）

#### 4.1.1 概念说明

Link 层要回答的问题是：**「给定一个端点，如何把它变成一个可以读写字节的连接？」**

Zenoh 把这件事拆成两个角色，单播和多播各有一套：

- **LinkManager（链路管理器）**：负责「创建/监听/删除」连接。它是**有状态、长生命周期**的——它持有监听 socket 和 accept 循环。同一种协议的多个连接通常共享同一个 manager。
- **Link（链路）**：代表一条**已经建立的、可读写**的连接，是 transport 层直接操作的句柄。

为什么单播和多播要分开？因为二者的「连接」语义完全不同：

| | unicast 单播 | multicast 多播 |
|---|---|---|
| 连接模型 | 点对点，先 listen 再 accept 出一条 link | 没有「连接」，直接 join 一个多播组就开始收发 |
| 写入 | 写给那一个对端 | 写给整个组（一份数据，所有成员都收） |
| 读取来源 | 已知，就是连接的对端 | 未知，每条数据可能来自组内任意成员 → read 要**返回来源 locator** |
| 接口方法 | `new_link / new_listener / del_listener / get_listeners / get_locators` | 只有 `new_link` |

#### 4.1.2 核心流程

单播建连流程（伪代码）：

```
TransportManager 想连 endpoint (如 tcp/1.2.3.4:7447)
  → LinkManagerBuilderUnicast::make(sender, endpoint)   // 按协议串选实现
  → 得到 LinkManagerUnicastTcp (Arc<dyn LinkManagerUnicastTrait>)
  → manager.new_link(endpoint)                           // 主动连出去
  → 内部 accept_task 把新连接经 sender 通知 transport 层
  → transport 层拿到 LinkUnicast，开始读写
```

多播建连流程：

```
TransportManager 想 join 一个多播 endpoint (如 udp/224.0.0.1:7447)
  → LinkManagerBuilderMulticast::make(LinkKind::Udp)     // 只有 UDP 支持多播
  → LinkManagerMulticastUdp
  → manager.new_link(endpoint)                            // join 多播组、建读 socket
  → 得到 LinkMulticast，即可 send/recv
```

#### 4.1.3 源码精读

**单播抽象**——`LinkManagerUnicast` 是个 trait object 别名，trait 定义了创建/监听/删除/列举五件事：

[io/zenoh-link-commons/src/unicast.rs:32-40](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-link-commons/src/unicast.rs#L32-L40) —— 把 `LinkManagerUnicast` 定义为 `Arc<dyn LinkManagerUnicastTrait>`，并声明 `new_link`（主动连）、`new_listener`（被动监听）、`del_listener`、`get_listeners`、`get_locators` 五个异步方法。

`LinkUnicast` 内部用 `NewLink` 枚举区分两种可靠性形态：

[io/zenoh-link-commons/src/unicast.rs:50-57](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-link-commons/src/unicast.rs#L50-L57) —— `NewLink::Single` 是一条普通链路；`MixedReliability` 则把「可靠」和「尽力而为」两条底层链路打包在一起（例如 QUIC 的流 + 数据报混合，后面 4.3 会看到）。`LinkUnicast` 通过 `Deref` 透明暴露可靠的那条。

单播链路的读写契约在 `LinkUnicastTrait`：

[io/zenoh-link-commons/src/unicast.rs:78-97](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-link-commons/src/unicast.rs#L78-L97) —— 关键方法：`get_mtu`（单帧最大字节数）、`is_reliable`、`is_streamed`（是否字节流）、`write/write_all/read/read_exact/close`。注意每个读写都带一个 `priority: Option<Priority>` 参数——这是给支持多优先级流的链路（如 QUIC）用的，普通 TCP 链路会忽略它。trait 末尾的 `get_fd`（第 95-96 行）是本次为 io_uring 新增的方法，先记住它在 `#[cfg(...)]` 门控下「可选存在」，4.4 节专门讲它。

**多播抽象**——注意它和单播的两处关键差异：

[io/zenoh-link-commons/src/multicast.rs:35-59](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-link-commons/src/multicast.rs#L35-L59) —— manager 只有 `new_link`（没有 listen/accept 概念）；`read` 的返回类型是 `(usize, Cow<Locator>)`，第二个分量就是这条数据的**来源 locator**——这正是多播「读取来源未知」的体现。

更有意思的是，多播 `LinkMulticast` 自己实现了 `send/recv`，里面直接用 `Zenoh080` codec 编解码一个 `TransportMessage`：

[io/zenoh-link-commons/src/multicast.rs:61-94](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-link-commons/src/multicast.rs#L61-L94) —— `send` 把 `TransportMessage` 序列化进一个 `Vec` 再 `write_all`；`recv` 按 `get_mtu()` 申请缓冲、读出后反序列化。也就是说，**多播链路里「一个数据报 = 一个完整的传输消息」**（数据报天然有边界）。这与单播链路不同——单播链路只负责搬运字节，消息分帧由更上层的 batch/codec 负责（见 u9-l4）。

#### 4.1.4 代码实践

**实践目标**：通过源码阅读，确认单播 manager 持有哪些状态、accept 循环如何把新连接上交给 transport 层。

**操作步骤**：

1. 打开 [io/zenoh-links/zenoh-link-tcp/src/unicast.rs:241-244](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-links/zenoh-link-tcp/src/unicast.rs#L241-L244)，看 `LinkManagerUnicastTcp` 只有两个字段：`manager: NewLinkChannelSender` 和 `listeners: ListenersUnicastIP`。
2. 跳到 [io/zenoh-links/zenoh-link-tcp/src/unicast.rs:395-440](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-links/zenoh-link-tcp/src/unicast.rs#L395-L440) 的 `accept_task`，找到第 432 行 `manager.send_async(LinkUnicast::from(link))`。

**需要观察的现象**：accept 循环 accept 到一条 TCP 连接后，构造 `LinkUnicastTcp`，**不是**直接返回给调用者，而是通过 `manager`（一个 flume sender）异步推给 transport 层。这就是「manager 负责监听、通过 channel 上交新链路」的设计。

**预期结果**：你能用一句话描述这条数据通路：`TcpListener::accept → LinkUnicastTcp → flume::Sender → TransportManager`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `LinkManagerMulticastTrait` 没有 `new_listener` / `del_listener` 方法，而单播有？

**参考答案**：多播没有「被动接受连接」的概念。加入一个多播组（`new_link` 内部完成 join）后，组成员之间的收发是对称的、无连接的，不存在「谁连进来」的事件，因此不需要 listen/accept 这一套。

**练习 2**：`LinkUnicastTrait::write` 为什么带一个 `priority` 参数？TCP 链路会用到它吗？

**参考答案**：这是给「一条物理连接内可分多个优先级流」的链路（如 QUIC 的多 stream）预留的，让上层把高优先级消息走独立流。TCP 是单字节流、不支持优先级，`LinkUnicastTcp::write` 形参名为 `_priority`（下划线开头），说明它**接收但忽略**该参数。

---

### 4.2 LocatorInspector：用 locator 判断可靠性与多播

#### 4.2.1 概念说明

在真正建连**之前**，transport 层需要先知道两件事：

1. 这条链路**是否可靠**（reliable）？——决定能不能承载要求可靠投递的消息（`Reliable` QoS）。
2. 这条 endpoint **是否多播**（multicast）？——决定该用 unicast 还是 multicast 的 transport 子系统。

`LocatorInspector` 就是回答这两个问题的「质询官」。它有两个层级：

- **抽象 trait**（在 commons）：`protocol()`、`is_multicast()`、`is_reliable()`。
- **每条链路的具体实现**：如 `TcpLocatorInspector`、`UdpLocatorInspector`……各自给出该协议的答案。
- **聚合 dispatcher**（在 zenoh-link 门面层）：持有一组子质询官，按 `LinkKind` 分派。

一个重要细节：**可靠性可以被 locator 的 metadata 覆盖**。也就是说，同是 UDP，默认不可靠，但如果在 locator 里显式标注 `reliability=reliable`，质询官就会报告它「可靠」——而 Zenoh 会在 UDP 之上跑一个可靠的 QUIC 传输来实现这一点（见 4.3）。

#### 4.2.2 核心流程

```
给定 locator (如 udp/224.0.0.1:7447)
  → LinkKind::try_from(&locator)        // 看协议串 udp/ → LinkKind::Udp
  → 聚合 LocatorInspector 按 LinkKind 分派到 UdpLocatorInspector
  → UdpLocatorInspector::is_reliable    // 先查 metadata，没有则用该协议默认值
  → UdpLocatorInspector::is_multicast   // 解析地址，看 IP 是否落在多播段
```

判断 IP 是否多播的依据是标准网络定义：IPv4 多播段是 `224.0.0.0/4`，IPv6 多播段是 `ff00::/8`。Rust 标准库的 `IpAddr::is_multicast()` 直接做这件事。

#### 4.2.3 源码精读

**抽象 trait**：

[io/zenoh-link-commons/src/lib.rs:70-79](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-link-commons/src/lib.rs#L70-L79) —— `LocatorInspector: Default` 定义三个方法：`protocol()` 返回协议串（如 `"tcp"`），`is_multicast` 是 `async`（因为可能要 DNS 解析地址），`is_reliable` 是同步的。旁边还有 `ConfigurationInspector<C>`，用于把 `Config` 翻译成该链路需要的初始化字符串（如 TLS 的证书路径）。

**TCP 的具体实现**（最简单，作为基线）：

[io/zenoh-links/zenoh-link-tcp/src/lib.rs:48-72](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-links/zenoh-link-tcp/src/lib.rs#L48-L72) —— `TcpLocatorInspector::is_multicast` 永远返回 `Ok(false)`（TCP 不支持多播）；`is_reliable` 先查 locator metadata 的 `RELIABILITY` 字段，查不到就用常量 `IS_RELIABLE`（TCP 是 `true`）。

**UDP 的具体实现**（更复杂，能体现 metadata 覆盖和多播判定）：

[io/zenoh-links/zenoh-link-udp/src/lib.rs:80-107](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-links/zenoh-link-udp/src/lib.rs#L80-L107) —— `UdpLocatorInspector::is_multicast` 调用 `get_udp_addrs` 解析地址，再用 `any(|x| x.ip().is_multicast())` 判定；`is_reliable` 同样先查 metadata，否则用 `IS_RELIABLE`（UDP 默认 `false`）。注意 `const IS_RELIABLE: bool = false` 定义在第 70 行。

**聚合 dispatcher**：zenoh-link 门面层持有一组子质询官，按 `LinkKind` 分派：

[io/zenoh-link/src/lib.rs:252-280](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-link/src/lib.rs#L252-L280) —— 聚合 `LocatorInspector` 先 `LinkKind::try_from(locator)` 判定是哪种链路，再把 `is_reliable` 转给对应的子质询官（`is_multicast` 在 [L282-L309](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-link/src/lib.rs#L282-L309) 同理）。这就是「策略分发」的标准写法。

#### 4.2.4 代码实践

**实践目标**：通过对比 TCP / UDP 两个质询官，理解「metadata 覆盖」机制。

**操作步骤**：

1. 对照 [io/zenoh-links/zenoh-link-tcp/src/lib.rs:60-71](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-links/zenoh-link-tcp/src/lib.rs#L60-L71) 与 [io/zenoh-links/zenoh-link-udp/src/lib.rs:95-106](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-links/zenoh-link-udp/src/lib.rs#L95-L106)，两者的 `is_reliable` 逻辑结构**完全相同**：`if let Some(reliability) = locator.metadata().get(Metadata::RELIABILITY) ... { 判断值 } else { 用默认常量 }`。

**需要观察的现象**：唯一区别是默认常量 `IS_RELIABLE`——TCP 是 `true`，UDP 是 `false`。

**预期结果**：你得出结论——可靠性有两层来源，**locator metadata 优先，协议默认值兜底**。这解释了为何同一个 `udp/` 协议既能跑不可靠的裸 UDP，也能在 metadata 标注后跑可靠的 QUIC-over-UDP。

> 说明：本实践为源码阅读型实践，无需运行命令；具体「如何往 locator metadata 写 reliability」属于 transport/endpoint 层的用法，可在 u9-l2/l3 进一步验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `is_multicast` 是 `async`，而 `is_reliable` 是同步的？

**参考答案**：判断多播需要把 locator 的地址字符串解析成 `SocketAddr`（可能触发 DNS 解析 `tokio::net::lookup_host`，这是异步 I/O），所以是 `async`。而可靠性只取决于 metadata 字段或协议默认常量，纯内存判断，无需 I/O，故同步。

**练习 2**：locator `udp/224.0.0.1:7447` 的 `is_multicast()` 和 `is_reliable()` 分别返回什么？

**参考答案**：`is_multicast()` 返回 `Ok(true)`（`224.0.0.1` 落在 `224.0.0.0/4` 多播段）；`is_reliable()` 返回 `Ok(false)`（无 metadata 覆盖，用 UDP 默认值 `false`）。

---

### 4.3 协议串分发与具体链路实现（TCP / UDP）

#### 4.3.1 概念说明

现在把前两节串起来：**locator 的协议串（`tcp`/`udp`/…）是如何被映射到具体的 `LinkManager` 实现的？**

答案是一个 `LinkKind` 枚举做中介，再加两个 builder 做工厂分派。整体是经典的「**用枚举做 tag，用 match 做分发**」模式。每种协议对应一个 `LinkKind` 变体、一个 `*LocatorInspector`、一个 `LinkManager*` 实现。

几个本节要讲清的关键差异（**可靠性、多播、MTU、是否字节流、能否给出 get_fd**）：

| 链路 | 协议串 | 默认可靠 | 支持多播 | 默认 MTU | is_streamed | get_fd（uring） |
|------|--------|:---:|:---:|------|:---:|:---:|
| TCP | `tcp/` | ✅ | ❌ | `65535`（`BatchSize::MAX`），生效值再扣 IP/TCP 头并取 MSS 整数倍 | ✅（字节流） | ✅ 返回 fd |
| UDP 单播(裸,Connected) | `udp/` | ❌ | ❌ | `UDP_MTU_LIMIT`：Linux/Win=`65487`、macOS=`9216`、其它=`8192` | ❌（数据报） | ✅ 返回 fd |
| UDP 单播(裸,Unconnected) | `udp/` | ❌ | ❌ | 同上 | ❌ | ❌ bail（共享 demux） |
| UDP 单播(可靠) | `udp/`+`reliability=reliable` | ✅ | ❌ | 由 QUIC 传输决定 | ✅ | ❌ bail（QUIC） |
| UDP 多播 | `udp/`(多播地址) | ❌ | ✅ | `UDP_DEFAULT_MTU` | —（数据报） | —（多播不经此 trait） |

> `BatchSize` 是 `u16`（见 [commons/zenoh-protocol/src/transport/mod.rs:41](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/commons/zenoh-protocol/src/transport/mod.rs#L41)），所以 `BatchSize::MAX = 65535`。`get_fd` 那一列的细节见 4.4 节。

#### 4.3.2 核心流程

**协议串 → LinkManager** 的完整分派链：

```
endpoint.protocol() == "tcp"
  → LinkKind::try_from(&locator)        // match protocol → LinkKind::Tcp
  → LinkManagerBuilderUnicast::make     // match LinkKind::Tcp → LinkManagerUnicastTcp::new(sender)
endpoint.protocol() == "udp" + 多播地址
  → LinkKind::try_from → LinkKind::Udp
  → LinkManagerBuilderMulticast::make   // match LinkKind::Udp → LinkManagerMulticastUdp（其它 b 错误）
```

**TCP MTU 的计算**：理论上 TCP 是字节流、无 MTU 上限，但 Zenoh 的批处理用 16 位编码帧长，所以封顶 `65535`；实际生效值还要扣掉 IP/TCP 头并取 TCP MSS 的整数倍：

\[
\text{MTU}_{\text{tcp}} = \min\bigl(\text{TCP\_DEFAULT\_MTU} - h,\ \lfloor \tfrac{\text{TCP\_DEFAULT\_MTU} - h}{\text{MSS}} \rfloor \cdot \text{MSS}\bigr)
\]

其中 \( h = 40 \)（IPv4：20 IP + 20 TCP）或 \( 60 \)（IPv6：40 IP + 20 TCP）。

**UDP MTU 的计算**：UDP 数据报最大 \( 2^{16} \) 字节，扣 8 字节 UDP 头与 40 字节 IPv6 头（最坏情况）：

\[
\text{UDP\_MAX\_MTU} = 2^{16} - 8 - 40 = 65487
\]

再按操作系统收紧（macOS 取 9212、其它取 8192）。

#### 4.3.3 源码精读

**(1) LinkKind 是分派中介**：

[io/zenoh-link/src/lib.rs:84-96](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-link/src/lib.rs#L84-L96) —— `LinkKind` 枚举列出所有支持的链路种类。`TryFrom<&Locator>` 在 [L136-L189](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-link/src/lib.rs#L136-L189) 里 `match locator.protocol().as_str()`，把协议串映射成 `LinkKind`（每条分支都被对应的 `#[cfg(feature = "transport_*")]` 门控）。注意 QUIC 那条分支还会调用 `QuicLocatorInspector.is_reliable` 来区分 `Quic` 与 `QuicDatagram`。

**(2) Unicast builder 工厂分派**：

[io/zenoh-link/src/lib.rs:387-423](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-link/src/lib.rs#L387-L423) —— `LinkManagerBuilderUnicast::make` 收到 endpoint，先 `LinkKind::try_from(endpoint)`，再 `match` 出对应的 `LinkManagerUnicast*::new(_manager)`，包成 `Arc` 返回。**这就是「协议串 → LinkManagerBuilder」映射的核心落点**：`tcp/` → `LinkManagerUnicastTcp`，`udp/` → `LinkManagerUnicastUdp`，依此类推。

**(3) Multicast builder 只认 UDP**：

[io/zenoh-link/src/lib.rs:432-440](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-link/src/lib.rs#L432-L440) —— `LinkManagerBuilderMulticast::make` 只匹配 `LinkKind::Udp` 返回 `LinkManagerMulticastUdp`，其它一律 `bail!("Multicast not supported for link ...")`。目前 Zenoh 的多播链路实现只有 UDP 一种。

**(4) TCP 链路实现与 MTU 计算**：

[io/zenoh-links/zenoh-link-tcp/src/lib.rs:36-46](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-links/zenoh-link-tcp/src/lib.rs#L36-L46) —— TCP 的 `TCP_MAX_MTU = BatchSize::MAX = 65535`、`TCP_LOCATOR_PREFIX = "tcp"`、`IS_RELIABLE = true`，注释解释了「字节流本无 MTU，因 Zenoh 用 16 位编码帧长而封顶 65535」。

[io/zenoh-links/zenoh-link-tcp/src/unicast.rs:79-100](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-links/zenoh-link-tcp/src/unicast.rs#L79-L100) —— `LinkUnicastTcp::new` 里计算 mtu：先按 IPv4/IPv6 扣 40/60 头，再在 unix 上读取 TCP MSS、把 mtu 收到 MSS 的整数倍（用 `mss/2` 做粒度）。这保证一帧能被整数个 TCP 段承载，避免半段浪费。

[io/zenoh-links/zenoh-link-tcp/src/unicast.rs:188-201](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-links/zenoh-link-tcp/src/unicast.rs#L188-L201) —— TCP 链路的自报属性：`is_reliable = true`、`is_streamed = true`（字节流，需要上层分帧）、`get_auth_id = LinkAuthId::Tcp`。

**(5) UDP 单播实现——三种 variant**（最有意思的部分）：

[io/zenoh-links/zenoh-link-udp/src/lib.rs:46-78](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-links/zenoh-link-udp/src/lib.rs#L46-L78) —— UDP 的 `UDP_MAX_MTU = u16::MAX - 8 - 40 = 65487`，`UDP_MTU_LIMIT` 按 OS 取值（Linux/Win=65487、macOS=9216、其它=8192），`IS_RELIABLE = false`。这些都用 `zconfigurable!` 标成**运行时可调静态量**（`UDP_DEFAULT_MTU`、`TCP_DEFAULT_MTU` 同理），可用环境变量调优。

[io/zenoh-links/zenoh-link-udp/src/unicast.rs:128-132](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-links/zenoh-link-udp/src/unicast.rs#L128-L132) —— `LinkUnicastUdpVariant` 有三个变体：
- `Connected`：socket `connect` 到对端，用 `recv/send`，最简单。
- `Unconnected`：socket 不连接，靠一个共享 accept-read 循环按 `(src,dst)` 分用（demux），用 `send_to`。
- `Reliable(Box<LinkUnicastQuicUnsecure>)`：**在 UDP 之上跑 QUIC**，把不可靠的 UDP「升级」成可靠+有序+字节流的链路。

[io/zenoh-links/zenoh-link-udp/src/unicast.rs:549-560](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-links/zenoh-link-udp/src/unicast.rs#L549-L560) —— `LinkManagerUnicastUdp::new_link` 会先问 `UdpLocatorInspector.is_reliable`：可靠就走 `LinkUnicastQuicUnsecure::connect`（QUIC over UDP），否则走普通 `new_udp_link`。这正是 4.2 所说「metadata 覆盖可靠性」的落地——在 UDP 这一层实现「按需可靠」。

QUIC-over-UDP 的实现可对照 [io/zenoh-links/zenoh-link-udp/src/reliability.rs:36-60](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-links/zenoh-link-udp/src/reliability.rs#L36-L60)，它复用 `zenoh-link-commons` 的 QUIC 客户端构建器（`QuicClientBuilder::security(false)`，即不强制 TLS）。

[io/zenoh-links/zenoh-link-udp/src/unicast.rs:230-269](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-links/zenoh-link-udp/src/unicast.rs#L230-L269) —— UDP 链路的属性随 variant 而变：裸 UDP `is_reliable=false`、`is_streamed=false`、`supports_priorities=false`；Reliable(QUIC) 变体三者都为 `true`。`get_mtu` 裸 UDP 用 `*UDP_DEFAULT_MTU`，Reliable 变体用 QUIC 自己的 mtu。

**(6) UDP 多播实现——双 socket 模型**：

[io/zenoh-links/zenoh-link-udp/src/multicast.rs:38-49](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-links/zenoh-link-udp/src/multicast.rs#L38-L49) —— `LinkMulticastUdp` 用**两个 socket**：`unicast_socket` 用来写（`send_to` 到多播组地址），`mcast_sock` 用来读（join 多播组后 `recv_from`）。

[io/zenoh-links/zenoh-link-udp/src/multicast.rs:87-138](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-links/zenoh-link-udp/src/multicast.rs#L87-L138) —— 写用单播 socket `send_to(multicast_addr)`；读用多播 socket `recv_from`，并且**跳过自己发出的回环消息**（`if self.unicast_addr == addr { continue }`）。`is_reliable` 恒为 `false`，`get_mtu` 用 `*UDP_DEFAULT_MTU`。

**(7) transport 层如何调用这些 builder**：

[io/zenoh-transport/src/unicast/manager.rs:397-401](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-transport/src/unicast/manager.rs#L397-L401) —— `TransportManager` 用 `LinkManagerBuilderUnicast::make(self.new_unicast_link_sender.clone(), endpoint)` 拿到 manager，并按 `LinkKey` 缓存复用（同协议端点共享一个 manager）。

[io/zenoh-transport/src/multicast/manager.rs:211-229](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-transport/src/multicast/manager.rs#L211-L229) —— 多播侧用 `LinkKind::try_from(endpoint)` 拿到 `link_kind`，校验是否在 `supported_links` 里，再 `LinkManagerBuilderMulticast::make(link_kind)`，并按 `link_kind` 缓存。

#### 4.3.4 代码实践

**实践目标**：阅读 link-tcp 与 link-udp 的实现，整理一张对比表（含「是否实现 get_fd」），并解释协议串如何映射到 builder。

**操作步骤**：

1. 通读 [io/zenoh-links/zenoh-link-tcp/src/lib.rs](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-links/zenoh-link-tcp/src/lib.rs) 与 [io/zenoh-links/zenoh-link-udp/src/lib.rs](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-links/zenoh-link-udp/src/lib.rs)，找到每条链路的 `*_LOCATOR_PREFIX`、`IS_RELIABLE`、MTU 常量与 `*LocatorInspector`。
2. 在两个 `unicast.rs` 里各自找到 `fn get_fd`（见 4.4），记下 TCP 与 UDP 各 variant 的取 fd / bail 行为。
3. 整理出下面这张对比表（答案见「预期结果」）。
4. 追踪协议串映射：从 [io/zenoh-link/src/lib.rs:394](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-link/src/lib.rs#L394) 的 `LinkKind::try_from(endpoint)` 出发，跟着 `match` 走到对应的 `LinkManagerUnicast*::new`。

**需要观察的现象**：TCP 是「天然可靠 + 字节流」，所以 `is_reliable=true`、`is_streamed=true`、MTU 接近 65535，且底层就是一个裸 TCP socket，能给出 fd；UDP 默认「不可靠 + 数据报」，`is_reliable=false`、`is_streamed=false`、MTU 受 OS 限制更小，只有 Connected 变体能给出 fd，Unconnected/Reliable 变体都 bail。

**预期结果**——对比表：

| 维度 | TCP (`tcp/`) | UDP 单播裸 (`udp/`) | UDP 单播可靠 (`udp/`+reliable) | UDP 多播 (`udp/`多播地址) |
|------|:---:|:---:|:---:|:---:|
| 可靠（默认） | 是 | 否 | 是 | 否 |
| 支持多播 | 否 | 否 | 否 | 是 |
| is_streamed | 是 | 否 | 是 | —（数据报） |
| 默认 MTU | 65535（生效再扣头+取 MSS 倍） | Linux/Win 65487 / macOS 9216 / 其它 8192 | 由 QUIC 决定 | UDP_DEFAULT_MTU |
| 实现 get_fd | 是（返回 socket fd） | Connected=是；Unconnected=否(bail) | 否（bail，QUIC） | —（多播不经 LinkUnicastTrait） |
| 映射的 manager | `LinkManagerUnicastTcp` | `LinkManagerUnicastUdp`(Connected/Unconnected) | `LinkManagerUnicastUdp`(Reliable=`LinkUnicastQuicUnsecure`) | `LinkManagerMulticastUdp` |

协议串映射一句话总结：`endpoint.protocol()` 经 `LinkKind::try_from` 变成 `LinkKind`，再经 `LinkManagerBuilderUnicast::make` / `LinkManagerBuilderMulticast::make` 的 `match` 选出具体 `LinkManager*` 实现。

> 说明：本实践为源码阅读型实践，不涉及运行命令；若想运行验证 MTU 或 get_fd 是否生效，可在 u9-l2/l5 启用 transport 与 uring feature 后用 tracing 日志或 adminspace 观察链路的 `mtu` 字段与接收路径分派。

#### 4.3.5 小练习与答案

**练习 1**：为什么 TCP 链路的 `is_streamed = true`，而裸 UDP 单播 `is_streamed = false`？这对上层意味着什么？

**参考答案**：TCP 是字节流，单次 `read` 可能返回半条或多条粘在一起的消息，边界丢失，所以 `is_streamed=true`，上层必须自己用长度前缀给消息分帧（这正是 transport batch/codec 做的事，见 u9-l4）。UDP 是数据报，一次 `recv_from` 恰好返回一整个数据报，边界天然保留，`is_streamed=false`，上层无需分帧。

**练习 2**：如果想让一条 UDP 连接变得可靠，Zenoh 是怎么做到的？

**参考答案**：在 locator 的 metadata 里设置 `reliability=reliable`。`UdpLocatorInspector::is_reliable` 查到该 metadata 后返回 `true`，`LinkManagerUnicastUdp::new_link` 据此不建裸 UDP socket，而是调用 `LinkUnicastQuicUnsecure::connect`，在 UDP 之上跑一个 QUIC 传输（可靠、有序、字节流），把链路「升级」成可靠的。

**练习 3**：为什么 `LinkManagerBuilderMulticast::make` 只支持 `LinkKind::Udp`？

**参考答案**：多播需要底层协议本身支持「一份数据多端可达」的语义。在 Zenoh 支持的链路里，只有 UDP 具备 IP 多播能力（TCP/TLS/QUIC/WS 都是点对点连接，无法多播）。所以多播 manager 工厂只匹配 `Udp`，其它直接报错。

---

### 4.4 get_fd：为 io_uring 暴露原始文件描述符（含回退语义）

#### 4.4.1 概念说明

本节讲本次代码变更（io_uring 支持）给 Link 层新增的一项能力：`LinkUnicastTrait::get_fd`。

**为什么需要它**：Zenoh 默认用 tokio 的异步 I/O 读取 socket。Linux 还提供了 io_uring——一种基于「提交队列 SQ + 完成队列 CQ」的异步 I/O 接口，能减少系统调用、实现更高吞吐的接收路径（完整接收路径见 u9-l5）。但 io_uring 要直接操作内核的**原始文件描述符（RawFd）**来注册缓冲、提交读请求，而 tokio 的 `TcpStream`/`UdpSocket` 把 fd 封装在内部、并不直接暴露。于是 Link 层新增 `get_fd()`，把底层 socket 的 fd 交给上层（传输层的 uring 接收路径）。

**关键设计是「尽力而为 + 优雅回退」**：每条链路自己决定能不能给出一个「有意义」的 fd：

- 给得出 → 返回 `Ok(RawFd)`，上层可用 io_uring 接管读取；
- 给不出 → 返回 `Err`，上层捕获后**回退到 tokio 的普通异步读路径**。

也就是说，io_uring 是加速器而非必需品：任何链路、任何时候都可以选择不用它。这是贯穿本次 io_uring 改动的核心语义。

**门控**：`get_fd` 被 `#[cfg(all(feature = "uring", target_os = "linux"))]` 双重门控——只有开启 `uring` feature 且在 Linux 上才编译进 trait；其它平台/配置下这个方法根本不存在，`LinkUnicastTrait` 也就退回原来的样子。配合这次还新增的 `uring` feature 聚合（在 [io/zenoh-link/Cargo.toml](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-link/Cargo.toml) 里把 `zenoh-link-commons/uring` 与十条链路的 `uring` 子特性一起拉进来），整个能力可以按需开关。

#### 4.4.2 核心流程

```
传输层想用 io_uring 读这条 link
  → link.get_fd()
  ├─ Ok(fd)  → 注册缓冲到 io_uring，走 uring 接收路径（rx_task_uring）
  └─ Err(_)  → 回退到 tokio 的 async read（rx_task_non_uring）
```

哪些链路给得出 fd、哪些给不出，取决于一个判据：**底层是否就是一个裸的 OS socket，且这个 fd 能代表「这条链路独有的、可直接读取的」数据流**。凡是在裸 socket 之上又套了一层用户态处理（TLS 加解密、HTTP/WebSocket 分帧、QUIC 用户态栈），或者 fd 被多个逻辑链路共享（未连接 UDP），都无法给出一个可直接读取的有意义 fd。

#### 4.4.3 源码精读

**(1) trait 上的新方法（双重 cfg 门控）**：

[io/zenoh-link-commons/src/unicast.rs:95-96](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-link-commons/src/unicast.rs#L95-L96) —— `LinkUnicastTrait` 末尾新增 `fn get_fd(&self) -> ZResult<RawFd>`，前面挂着 `#[cfg(all(feature = "uring", target_os = "linux"))]`。配合文件顶部同样 cfg 门控的 `use std::os::fd::RawFd`（[L21-L22](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-link-commons/src/unicast.rs#L21-L22)）。

**(2) 真正给出 fd 的链路**——都是「直接包了一个裸 socket」的实现，模式高度一致：取 `as_raw_fd()`，若 `< 0` 则 `bail!("FD unavailable")`：

- TCP：[io/zenoh-links/zenoh-link-tcp/src/unicast.rs:203-209](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-links/zenoh-link-tcp/src/unicast.rs#L203-L209) —— `unsafe { &*self.socket.get() }.as_raw_fd()`。
- unixsock_stream / vsock：同 `socket.get().as_raw_fd()` 模式。
- unixpipe：从内部 `pipe` 取 fd。
- UDP 的 **Connected** 变体：见下条。

**(3) UDP 的 `get_fd` 最能体现「按 variant 分派 + 回退」**：

[io/zenoh-links/zenoh-link-udp/src/unicast.rs:276-294](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-links/zenoh-link-udp/src/unicast.rs#L276-L294) —— `match &self.variant`：
- `Connected` → `socket.as_raw_fd()`，给出真 fd；
- `Unconnected` → `bail!("FD unavailable for unconnected UDP")`；
- `Reliable(_)` → `bail!("FD unavailable")`。

`Unconnected` 之所以必须 bail，源码注释写得很清楚：未连接的 UDP socket **被多个对端共享**，靠源地址在 tokio 读路径里分用（demux）把数据报派给正确的 link；把同一 fd 交给 io_uring 的 `RecvMulti` 会绕过这套 demux，把任意对端的数据报都投到这一条 link 上，造成串包。所以它**永远回退到 tokio**。

**(4) 显式 bail、给不出 fd 的链路**——各有原因，可在各自 `unicast.rs` 的 `fn get_fd` 处对照：
- QUIC / quic_datagram：`bail!("Not supported")`，注释 `//TODO: expose FD for quinn???`——quinn 库暂未暴露底层 UDP socket 的 fd。
- serial：`bail!("Not supported")`，`//TODO: expose FD for ZSerial`。
- TLS：`bail!("Correct FD unavailable for TLS extension")`——fd 上跑的是**加密后**的字节，io_uring 直接读会绕过 rustls 解密层，读出无意义密文，所以「正确的 fd 不可用」。
- WS：`bail!("Not supported")`——WebSocket 跑在 HTTP/TLS 之上、是用户态分帧，没有可直接读取的裸 fd。

#### 4.4.4 代码实践

**实践目标**：阅读各链路 `get_fd` 实现，整理「谁给得出 fd、谁回退到 tokio」的对比表，并解释每条 bail 的原因。

**操作步骤**：

1. 在 [io/zenoh-link-commons/src/unicast.rs:95-96](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-link-commons/src/unicast.rs#L95-L96) 确认 trait 定义与双重 cfg 门控，并注意它只在 `uring` feature + Linux 下存在。
2. 对照两种截然不同的实现：返回真 fd 的 [io/zenoh-links/zenoh-link-tcp/src/unicast.rs:203-209](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-links/zenoh-link-tcp/src/unicast.rs#L203-L209)，以及按 variant 分派的 [io/zenoh-links/zenoh-link-udp/src/unicast.rs:276-294](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-links/zenoh-link-udp/src/unicast.rs#L276-L294)。
3. 打开 `zenoh-link-tls`、`zenoh-link-quic`、`zenoh-link-ws`、`zenoh-link-serial` 各自的 `unicast.rs`，找到 `fn get_fd`，记录它们 bail 的不同理由。

**需要观察的现象**：能否给出 fd，与「链路是否包了一个裸 socket」严格对应；凡是上面还套了用户态编解码层（TLS/WS/QUIC/serial）或 fd 被多端共享（未连接 UDP），都选择 bail。

**预期结果**——get_fd 行为对比表：

| 链路 | `get_fd` 行为 | 原因 |
|---|---|---|
| TCP | 返回 fd | 直接包裸 TCP socket |
| UDP Connected | 返回 fd | socket 已 `connect` 到单一对端 |
| UDP Unconnected | bail | fd 被多端共享、靠源地址 demux |
| UDP Reliable(QUIC) | bail | 底层是 QUIC 用户态栈 |
| unixsock_stream / vsock / unixpipe | 返回 fd | 包裸 OS socket / pipe |
| QUIC / quic_datagram | bail | quinn 未暴露 fd（TODO） |
| serial | bail | 串口库未暴露 fd（TODO） |
| TLS | bail | fd 上是密文，绕过解密 |
| WS | bail | 用户态分帧 |

> 说明：本实践为源码阅读型实践。若要运行验证某条链路实际走 uring 还是回退 tokio，需在 Linux 上开启 `zenoh/uring` feature 编译，并用 tracing 日志观察接收路径分派——具体方法见 u9-l5。

#### 4.4.5 小练习与答案

**练习 1**：TLS 链路底层明明也是一个 TCP socket，为什么它的 `get_fd` 选择 bail？

**参考答案**：因为 io_uring 直接读 fd 拿到的是 TLS 加密后的密文，会绕过 rustls 的解密层，读出的字节无法被上层协议解析。源码里 `bail!("Correct FD unavailable for TLS extension")` 的「Correct」就是强调「能读到明文的那个 fd 并不存在」——fd 倒是拿得到，但拿到的不是正确的（可读的）数据流。

**练习 2**：未连接的 UDP（Unconnected）为何必须回退到 tokio，而不能用 io_uring？

**参考答案**：见 [unicast.rs:282-285](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-links/zenoh-link-udp/src/unicast.rs#L282-L285) 的注释：未连接 socket 被多个对端共享，tokio 读路径靠源地址把数据报 demux 到正确的 link；若把同一 fd 交给 io_uring 的 `RecvMulti`，会绕过 demux，把任意对端的数据报都投到这一条 link 上，造成串包。所以它无条件 bail、回退 tokio。

**练习 3**：在不开启 `uring` feature 的情况下，`LinkUnicastTrait` 上有 `get_fd` 方法吗？为什么这样设计？

**参考答案**：没有。`get_fd` 被 `#[cfg(all(feature = "uring", target_os = "linux"))]` 门控，关掉 feature 或非 Linux 平台时该方法根本不编译进 trait，trait 退回原貌。这样既保证了不开 uring 时零开销、不污染抽象，又让上层 transport 代码可以在同一处用 `#[cfg]` 决定是否走 uring 路径。

## 5. 综合实践

**任务**：画出一条 Zenoh 消息「从想要连接到真正收发字节」时，Link 层参与的完整决策与创建链路，并标注每一步用到的源码位置。

请按下面的引导完成一份文字版的「Link 层决策图」：

1. 假设 transport 层要连接 `tcp/192.0.2.1:7447`。
2. 写出从协议串到拿到 `LinkManagerUnicastTcp` 的分派路径，引用 [io/zenoh-link/src/lib.rs:394](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-link/src/lib.rs#L394)（`LinkKind::try_from`）与 [io/zenoh-link/src/lib.rs:396](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-link/src/lib.rs#L396)（`LinkManagerUnicastTcp::new`）。
3. 写出 transport 如何调它：[io/zenoh-transport/src/unicast/manager.rs:398](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-transport/src/unicast/manager.rs#L398)。
4. 假设现在是 `udp/224.0.0.1:7447`（多播）。说明它为什么走的是 `LinkManagerBuilderMulticast::make` 而不是 unicast，引用 [io/zenoh-link/src/lib.rs:432-440](https://github.com/eclipse-zenoh/zenoh/blob/5dd1a2f764b28a70ce6f12801e1d4dca2321bbc2/io/zenoh-link/src/lib.rs#L432-L440)，并说明多播与单播在 `LinkManager` 接口上的差异（参考 4.1）。
5. 把两种情况各得到的 `Link` 的 `is_reliable` / `is_streamed` / `get_mtu` 列出来（参考 4.3 的对比表）。
6. **进阶**：如果现在开启了 `uring` feature，分别说明上面 TCP link 与一条 Unconnected UDP link 在「上层想用 io_uring 读取」时会调用哪个方法、得到什么结果、最终走哪条读取路径（参考 4.4）。

**预期成果**：一张清晰的「协议串 → LinkKind → LinkManager → Link（含属性 + get_fd 可用性）」决策表，能解释清楚 Zenoh 是如何用同一套抽象驾驭 TCP / UDP（含多播与可靠变体）等多种链路，并在 io_uring 与 tokio 之间优雅切换的。

## 6. 本讲小结

- **Link 层是内核最底层**：它把「端点」变成「可读写的字节连接」，单播用 `LinkManagerUnicastTrait` + `LinkUnicastTrait`，多播用 `LinkManagerMulticastTrait` + `LinkMulticastTrait`，二者模型差异源于「连接 vs 无连接」。
- **LinkManager 管状态、Link 管收发**：manager 持监听 socket 和 accept 循环，通过 `flume` channel 把新链路上交给 transport 层；link 暴露 `get_mtu/is_reliable/is_streamed/write/read/close`（以及可选的 `get_fd`）。
- **LocatorInspector 是建连前的质询官**：靠 locator 协议串 + 可选 metadata 判断可靠性与多播能力，**metadata 覆盖优先、协议默认值兜底**。
- **协议串经 LinkKind 分派到具体实现**：`LinkKind::try_from` 把协议串变成枚举 tag，`LinkManagerBuilderUnicast/Multicast::make` 用 `match` 选出具体 `LinkManager*`。
- **TCP 天然可靠+字节流（MTU≈65535）**，**UDP 默认不可靠+数据报（MTU 受 OS 限制）但可经 metadata 升级为 QUIC-over-UDP 的可靠链路**；多播目前只有 UDP 一种实现，用「单播 socket 写 + 多播 socket 读」的双 socket 模型。
- **`get_fd` 是本次 io_uring 改动给 Link 层加的能力**：在 `#[cfg(uring + linux)]` 门控下，把底层 socket 的 RawFd 暴露给上层 uring 接收路径；裸 socket 链路（TCP / UDP Connected / unixsock / vsock / unixpipe）给出 fd，而未连接 UDP（共享 demux）、QUIC、TLS（密文）、WS、serial 选择 `bail!` 并**回退到 tokio**——io_uring 是加速器而非必需品。
- **MTU 等参数多为 `zconfigurable!` 运行时可调静态量**，可用环境变量调优。

## 7. 下一步学习建议

本讲只讲了「Link 层如何提供一条可读写的连接」，但还没有讲 transport 层如何**管理多条链路、建连握手、保活、批处理与分帧**，也还没讲 `get_fd` 暴露出的 fd 在上层如何被 io_uring 使用。建议接着学：

- **u9-l2 传输层：unicast 与 multicast 管理**：看 `TransportManager` 如何用本讲的 `LinkManagerBuilder` 创建/复用 manager，`lease/keep_alive/超时` 等配置如何影响连接生命周期，以及 uring feature 下 `TransportManagerState` 如何初始化 io_uring reactor 并在失败时回退。
- **u9-l3 传输建连、认证与内部状态机**：看 Open/Close 握手如何交换 zid/whatami/lease，以及认证（usrpwd/pubkey）如何接入。
- **u9-l4 批处理、分片与优先级管道**：理解 `is_streamed` 为何重要——字节流链路需要 batch/分帧，而大消息要在 MTU 之上分片、对端重组。
- **u9-l5 io_uring 接收路径**：本讲的 `get_fd` 在这里落地——看传输层如何在拿到 fd 后走 `rx_task_uring`、拿不到 fd 或未开启 uring 时回退 `rx_task_non_uring`，以及 `setup_read` / `setup_fragmented_read` 如何区分字节流与数据报链路。

源码上，可以继续阅读其它链路实现（`io/zenoh-links/zenoh-link-tls`、`zenoh-link-quic`、`zenoh-link-ws`、`zenoh-link-vsock`、`zenoh-link-unixsock_stream`）对比它们如何实现同一套 `LinkUnicastTrait`（含各自的 `get_fd` 回退策略）与 `LocatorInspector`，体会这套抽象的普适性。
