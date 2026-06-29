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

## 2. 前置知识

在进入本讲前，你需要先具备以下直觉（前置讲义已建立）：

- **Runtime 是会话的运行时核心**（u7-l1）：`zenoh::open` 的执行链是 `RuntimeBuilder::build → Session::init → Runtime::start`，而真正「打开一条传输」的动作（`open_transport_unicast` / `multicast`）发生在 Runtime 内部，由 `TransportManager` 完成。本讲讲的 Link 层，正是 `TransportManager` 往下要用的零件。
- **locator 与 endpoint**（u2、u7-l3）：Zenoh 用形如 `tcp/127.0.0.1:7447` 的字符串描述一个网络端点，斜杠前是**协议串**（`tcp`/`udp`/`tls`/`quic`/`ws`…），斜杠后是地址。Link 层的关键工作之一，就是看协议串决定用哪条链路实现。
- **可靠性与 QoS**（u3-l3）：一条消息是否「可丢弃」由 `droppable = !reliable || (cc==Drop)` 决定。本讲会看到「链路本身是否可靠」这个更底层的概念——它是 `Reliable`/`BestEffort` 在线路层的根。

两个术语先澄清：

- **unicast（单播）**：点对点，一条链路只连两个端点（TCP 是典型）。
- **multicast（多播）**：一对多，一条链路向一组节点同时投递（UDP 多播是典型，用于 scouting 的节点发现）。

## 3. 本讲源码地图

本讲涉及的关键文件（按「从抽象到具体」排序）：

| 文件 | 作用 |
|------|------|
| [io/zenoh-link/src/lib.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-link/src/lib.rs) | **门面/分派层**：定义 `LinkKind`、聚合 `LocatorInspector`、`LinkManagerBuilderUnicast/Multicast`，把协议串分派到具体链路实现 |
| [io/zenoh-link-commons/src/lib.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-link-commons/src/lib.rs) | 抽象骨架：`Link` 结构体、`LocatorInspector` trait、`ConfigurationInspector` trait |
| [io/zenoh-link-commons/src/unicast.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-link-commons/src/unicast.rs) | 单播抽象：`LinkManagerUnicastTrait`、`LinkUnicastTrait`、`NewLink` |
| [io/zenoh-link-commons/src/multicast.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-link-commons/src/multicast.rs) | 多播抽象：`LinkManagerMulticastTrait`、`LinkMulticast` 及其 `send/recv`（内含 Zenoh080 编解码） |
| [io/zenoh-links/zenoh-link-tcp/src/lib.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-links/zenoh-link-tcp/src/lib.rs) | TCP 链路：协议常量、`TcpLocatorInspector`、MTU/可调参数 |
| [io/zenoh-links/zenoh-link-tcp/src/unicast.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-links/zenoh-link-tcp/src/unicast.rs) | TCP 链路实现：`LinkUnicastTcp`、`LinkManagerUnicastTcp`、accept 循环 |
| [io/zenoh-links/zenoh-link-udp/src/lib.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-links/zenoh-link-udp/src/lib.rs) | UDP 链路：协议常量、`UdpLocatorInspector`、按 OS 区分的 MTU |
| [io/zenoh-links/zenoh-link-udp/src/unicast.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-links/zenoh-link-udp/src/unicast.rs) | UDP 单播实现：`LinkUnicastUdp`（三种 variant） |
| [io/zenoh-links/zenoh-link-udp/src/multicast.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-links/zenoh-link-udp/src/multicast.rs) | UDP 多播实现：`LinkMulticastUdp`（双 socket：写用单播、读用多播） |

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

[io/zenoh-link-commons/src/unicast.rs:30-38](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-link-commons/src/unicast.rs#L30-L38) —— 把 `LinkManagerUnicast` 定义为 `Arc<dyn LinkManagerUnicastTrait>`，并声明 `new_link`（主动连）、`new_listener`（被动监听）、`del_listener`、`get_listeners`、`get_locators` 五个异步方法。

`LinkUnicast` 内部用 `NewLink` 枚举区分两种可靠性形态：

[io/zenoh-link-commons/src/unicast.rs:48-55](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-link-commons/src/unicast.rs#L48-L55) —— `NewLink::Single` 是一条普通链路；`MixedReliability` 则把「可靠」和「尽力而为」两条底层链路打包在一起（例如 QUIC 的流 + 数据报混合，后面 4.3 会看到）。`LinkUnicast` 通过 `Deref` 透明暴露可靠的那条。

单播链路的读写契约在 `LinkUnicastTrait`：

[io/zenoh-link-commons/src/unicast.rs:76-93](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-link-commons/src/unicast.rs#L76-L93) —— 关键方法：`get_mtu`（单帧最大字节数）、`is_reliable`、`is_streamed`（是否字节流）、`write/write_all/read/read_exact/close`。注意每个读写都带一个 `priority: Option<Priority>` 参数——这是给支持多优先级流的链路（如 QUIC）用的，普通 TCP 链路会忽略它。

**多播抽象**——注意它和单播的两处关键差异：

[io/zenoh-link-commons/src/multicast.rs:35-59](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-link-commons/src/multicast.rs#L35-L59) —— manager 只有 `new_link`（没有 listen/accept 概念）；`read` 的返回类型是 `(usize, Cow<Locator>)`，第二个分量就是这条数据的**来源 locator**——这正是多播「读取来源未知」的体现。

更有意思的是，多播 `LinkMulticast` 自己实现了 `send/recv`，里面直接用 `Zenoh080` codec 编解码一个 `TransportMessage`：

[io/zenoh-link-commons/src/multicast.rs:61-94](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-link-commons/src/multicast.rs#L61-L94) —— `send` 把 `TransportMessage` 序列化进一个 `Vec` 再 `write_all`；`recv` 按 `get_mtu()` 申请缓冲、读出后反序列化。也就是说，**多播链路里「一个数据报 = 一个完整的传输消息」**（数据报天然有边界）。这与单播链路不同——单播链路只负责搬运字节，消息分帧由更上层的 batch/codec 负责（见 u9-l4）。

#### 4.1.4 代码实践

**实践目标**：通过源码阅读，确认单播 manager 持有哪些状态、accept 循环如何把新连接上交给 transport 层。

**操作步骤**：

1. 打开 [io/zenoh-links/zenoh-link-tcp/src/unicast.rs:231-252](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-links/zenoh-link-tcp/src/unicast.rs#L231-L252)，看 `LinkManagerUnicastTcp` 只有两个字段：`manager: NewLinkChannelSender` 和 `listeners: ListenersUnicastIP`。
2. 跳到 [io/zenoh-links/zenoh-link-tcp/src/unicast.rs:385-443](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-links/zenoh-link-tcp/src/unicast.rs#L385-L443) 的 `accept_task`，找到第 422 行 `manager.send_async(LinkUnicast::from(link))`。

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

[io/zenoh-link-commons/src/lib.rs:70-79](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-link-commons/src/lib.rs#L70-L79) —— `LocatorInspector: Default` 定义三个方法：`protocol()` 返回协议串（如 `"tcp"`），`is_multicast` 是 `async`（因为可能要 DNS 解析地址），`is_reliable` 是同步的。旁边还有 `ConfigurationInspector<C>`，用于把 `Config` 翻译成该链路需要的初始化字符串（如 TLS 的证书路径）。

**TCP 的具体实现**（最简单，作为基线）：

[io/zenoh-links/zenoh-link-tcp/src/lib.rs:48-72](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-links/zenoh-link-tcp/src/lib.rs#L48-L72) —— `TcpLocatorInspector::is_multicast` 永远返回 `Ok(false)`（TCP 不支持多播）；`is_reliable` 先查 locator metadata 的 `RELIABILITY` 字段，查不到就用常量 `IS_RELIABLE`（TCP 是 `true`）。

**UDP 的具体实现**（更复杂，能体现 metadata 覆盖和多播判定）：

[io/zenoh-links/zenoh-link-udp/src/lib.rs:80-107](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-links/zenoh-link-udp/src/lib.rs#L80-L107) —— `UdpLocatorInspector::is_multicast` 调用 `get_udp_addrs` 解析地址，再用 `any(|x| x.ip().is_multicast())` 判定；`is_reliable` 同样先查 metadata，否则用 `IS_RELIABLE`（UDP 默认 `false`）。注意 `const IS_RELIABLE: bool = false` 定义在第 70 行。

**聚合 dispatcher**：zenoh-link 门面层持有一组子质询官，按 `LinkKind` 分派：

[io/zenoh-link/src/lib.rs:252-280](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-link/src/lib.rs#L252-L280) —— 聚合 `LocatorInspector` 先 `LinkKind::try_from(locator)` 判定是哪种链路，再把 `is_reliable` 转给对应的子质询官（`is_multicast` 在 [L282-L309](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-link/src/lib.rs#L282-L309) 同理）。这就是「策略分发」的标准写法。

#### 4.2.4 代码实践

**实践目标**：通过对比 TCP / UDP 两个质询官，理解「metadata 覆盖」机制。

**操作步骤**：

1. 对照 [io/zenoh-links/zenoh-link-tcp/src/lib.rs:60-71](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-links/zenoh-link-tcp/src/lib.rs#L60-L71) 与 [io/zenoh-links/zenoh-link-udp/src/lib.rs:95-106](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-links/zenoh-link-udp/src/lib.rs#L95-L106)，两者的 `is_reliable` 逻辑结构**完全相同**：`if let Some(reliability) = locator.metadata().get(Metadata::RELIABILITY) ... { 判断值 } else { 用默认常量 }`。

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

几个本节要讲清的关键差异（**可靠性、多播、MTU、是否字节流**）：

| 链路 | 协议串 | 默认可靠 | 支持多播 | 默认 MTU | is_streamed |
|------|--------|:---:|:---:|------|:---:|
| TCP | `tcp/` | ✅ | ❌ | `65535`（`BatchSize::MAX`），生效值再扣 IP/TCP 头并取 MSS 整数倍 | ✅（字节流） |
| UDP 单播(裸) | `udp/` | ❌ | ❌ | `UDP_MTU_LIMIT`：Linux/Win=`65487`、macOS=`9216`、其它=`8192` | ❌（数据报） |
| UDP 单播(可靠) | `udp/`+`reliability=reliable` | ✅ | ❌ | 由 QUIC 传输决定 | ✅ |
| UDP 多播 | `udp/`(多播地址) | ❌ | ✅ | `UDP_DEFAULT_MTU` | —（数据报） |

> `BatchSize` 是 `u16`（见 [commons/zenoh-protocol/src/transport/mod.rs:41](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/transport/mod.rs#L41)），所以 `BatchSize::MAX = 65535`。

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

[io/zenoh-link/src/lib.rs:84-96](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-link/src/lib.rs#L84-L96) —— `LinkKind` 枚举列出所有支持的链路种类。`TryFrom<&Locator>` 在 [L136-L189](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-link/src/lib.rs#L136-L189) 里 `match locator.protocol().as_str()`，把协议串映射成 `LinkKind`（每条分支都被对应的 `#[cfg(feature = "transport_*")]` 门控）。注意 QUIC 那条分支还会调用 `QuicLocatorInspector.is_reliable` 来区分 `Quic` 与 `QuicDatagram`。

**(2) Unicast builder 工厂分派**：

[io/zenoh-link/src/lib.rs:387-423](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-link/src/lib.rs#L387-L423) —— `LinkManagerBuilderUnicast::make` 收到 endpoint，先 `LinkKind::try_from(endpoint)`，再 `match` 出对应的 `LinkManagerUnicast*::new(_manager)`，包成 `Arc` 返回。**这就是「协议串 → LinkManagerBuilder」映射的核心落点**：`tcp/` → `LinkManagerUnicastTcp`，`udp/` → `LinkManagerUnicastUdp`，依此类推。

**(3) Multicast builder 只认 UDP**：

[io/zenoh-link/src/lib.rs:432-440](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-link/src/lib.rs#L432-L440) —— `LinkManagerBuilderMulticast::make` 只匹配 `LinkKind::Udp` 返回 `LinkManagerMulticastUdp`，其它一律 `bail!("Multicast not supported for link ...")`。目前 Zenoh 的多播链路实现只有 UDP 一种。

**(4) TCP 链路实现与 MTU 计算**：

[io/zenoh-links/zenoh-link-tcp/src/lib.rs:36-46](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-links/zenoh-link-tcp/src/lib.rs#L36-L46) —— TCP 的 `TCP_MAX_MTU = BatchSize::MAX = 65535`、`TCP_LOCATOR_PREFIX = "tcp"`、`IS_RELIABLE = true`，注释解释了「字节流本无 MTU，因 Zenoh 用 16 位编码帧长而封顶 65535」。

[io/zenoh-links/zenoh-link-tcp/src/unicast.rs:77-109](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-links/zenoh-link-tcp/src/unicast.rs#L77-L109) —— `LinkUnicastTcp::new` 里计算 mtu：先按 IPv4/IPv6 扣 40/60 头，再在 unix 上读取 TCP MSS、把 mtu 收到 MSS 的整数倍（用 `mss/2` 做粒度）。这保证一帧能被整数个 TCP 段承载，避免半段浪费。

[io/zenoh-links/zenoh-link-tcp/src/unicast.rs:186-199](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-links/zenoh-link-tcp/src/unicast.rs#L186-L199) —— TCP 链路的自报属性：`is_reliable = true`、`is_streamed = true`（字节流，需要上层分帧）、`get_auth_id = LinkAuthId::Tcp`。

**(5) UDP 单播实现——三种 variant**（最有意思的部分）：

[io/zenoh-links/zenoh-link-udp/src/lib.rs:46-78](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-links/zenoh-link-udp/src/lib.rs#L46-L78) —— UDP 的 `UDP_MAX_MTU = u16::MAX - 8 - 40 = 65487`，`UDP_MTU_LIMIT` 按 OS 取值（Linux/Win=65487、macOS=9216、其它=8192），`IS_RELIABLE = false`。这些都用 `zconfigurable!` 标成**运行时可调静态量**（`UDP_DEFAULT_MTU`、`TCP_DEFAULT_MTU` 同理），可用环境变量调优。

[io/zenoh-links/zenoh-link-udp/src/unicast.rs:125-141](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-links/zenoh-link-udp/src/unicast.rs#L125-L141) —— `LinkUnicastUdpVariant` 有三个变体：
- `Connected`：socket `connect` 到对端，用 `recv/send`，最简单。
- `Unconnected`：socket 不连接，靠一个共享 accept-read 循环按 `(src,dst)` 分用（demux），用 `send_to`。
- `Reliable(Box<LinkUnicastQuicUnsecure>)`：**在 UDP 之上跑 QUIC**，把不可靠的 UDP「升级」成可靠+有序+字节流的链路。

[io/zenoh-links/zenoh-link-udp/src/unicast.rs:526-538](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-links/zenoh-link-udp/src/unicast.rs#L526-L538) —— `LinkManagerUnicastUdp::new_link` 会先问 `UdpLocatorInspector.is_reliable`：可靠就走 `LinkUnicastQuicUnsecure::connect`（QUIC over UDP），否则走普通 `new_udp_link`。这正是 4.2 所说「metadata 覆盖可靠性」的落地——在 UDP 这一层实现「按需可靠」。

QUIC-over-UDP 的实现可对照 [io/zenoh-links/zenoh-link-udp/src/reliability.rs:36-60](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-links/zenoh-link-udp/src/reliability.rs#L36-L60)，它复用 `zenoh-link-commons` 的 QUIC 客户端构建器（`QuicClientBuilder::security(false)`，即不强制 TLS）。

[io/zenoh-links/zenoh-link-udp/src/unicast.rs:228-267](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-links/zenoh-link-udp/src/unicast.rs#L228-L267) —— UDP 链路的属性随 variant 而变：裸 UDP `is_reliable=false`、`is_streamed=false`、`supports_priorities=false`；Reliable(QUIC) 变体三者都为 `true`。`get_mtu` 裸 UDP 用 `*UDP_DEFAULT_MTU`，Reliable 变体用 QUIC 自己的 mtu。

**(6) UDP 多播实现——双 socket 模型**：

[io/zenoh-links/zenoh-link-udp/src/multicast.rs:38-49](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-links/zenoh-link-udp/src/multicast.rs#L38-L49) —— `LinkMulticastUdp` 用**两个 socket**：`unicast_socket` 用来写（`send_to` 到多播组地址），`mcast_sock` 用来读（join 多播组后 `recv_from`）。

[io/zenoh-links/zenoh-link-udp/src/multicast.rs:87-138](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-links/zenoh-link-udp/src/multicast.rs#L87-L138) —— 写用单播 socket `send_to(multicast_addr)`；读用多播 socket `recv_from`，并且**跳过自己发出的回环消息**（`if self.unicast_addr == addr { continue }`）。`is_reliable` 恒为 `false`，`get_mtu` 用 `*UDP_DEFAULT_MTU`。

**(7) transport 层如何调用这些 builder**：

[io/zenoh-transport/src/unicast/manager.rs:397-401](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/manager.rs#L397-L401) —— `TransportManager` 用 `LinkManagerBuilderUnicast::make(self.new_unicast_link_sender.clone(), endpoint)` 拿到 manager，并按 `LinkKey` 缓存复用（同协议端点共享一个 manager）。

[io/zenoh-transport/src/multicast/manager.rs:211-229](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/multicast/manager.rs#L211-L229) —— 多播侧用 `LinkKind::try_from(endpoint)` 拿到 `link_kind`，校验是否在 `supported_links` 里，再 `LinkManagerBuilderMulticast::make(link_kind)`，并按 `link_kind` 缓存。

#### 4.3.4 代码实践

**实践目标**：阅读 link-tcp 与 link-udp 的实现，整理一张对比表，并解释协议串如何映射到 builder。

**操作步骤**：

1. 通读 [io/zenoh-links/zenoh-link-tcp/src/lib.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-links/zenoh-link-tcp/src/lib.rs) 与 [io/zenoh-links/zenoh-link-udp/src/lib.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-links/zenoh-link-udp/src/lib.rs)，找到每条链路的 `*_LOCATOR_PREFIX`、`IS_RELIABLE`、MTU 常量与 `*LocatorInspector`。
2. 整理出下面这张对比表（答案见「预期结果」）。
3. 追踪协议串映射：从 [io/zenoh-link/src/lib.rs:394](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-link/src/lib.rs#L394) 的 `LinkKind::try_from(endpoint)` 出发，跟着 `match` 走到对应的 `LinkManagerUnicast*::new`。

**需要观察的现象**：TCP 是「天然可靠 + 字节流」，所以 `is_reliable=true`、`is_streamed=true`、MTU 接近 65535；UDP 默认「不可靠 + 数据报」，`is_reliable=false`、`is_streamed=false`、MTU 受 OS 限制更小，但可通过 metadata 升级为可靠的 QUIC-over-UDP。

**预期结果**——对比表：

| 维度 | TCP (`tcp/`) | UDP 单播裸 (`udp/`) | UDP 单播可靠 (`udp/`+reliable) | UDP 多播 (`udp/`多播地址) |
|------|:---:|:---:|:---:|:---:|
| 可靠（默认） | 是 | 否 | 是 | 否 |
| 支持多播 | 否 | 否 | 否 | 是 |
| is_streamed | 是 | 否 | 是 | —（数据报） |
| 默认 MTU | 65535（生效再扣头+取 MSS 倍） | Linux/Win 65487 / macOS 9216 / 其它 8192 | 由 QUIC 决定 | UDP_DEFAULT_MTU |
| 映射的 manager | `LinkManagerUnicastTcp` | `LinkManagerUnicastUdp`(Connected/Unconnected) | `LinkManagerUnicastUdp`(Reliable=`LinkUnicastQuicUnsecure`) | `LinkManagerMulticastUdp` |

协议串映射一句话总结：`endpoint.protocol()` 经 `LinkKind::try_from` 变成 `LinkKind`，再经 `LinkManagerBuilderUnicast::make` / `LinkManagerBuilderMulticast::make` 的 `match` 选出具体 `LinkManager*` 实现。

> 说明：本实践为源码阅读型实践，不涉及运行命令；若想运行验证 MTU，可在 u9-l2 启用 transport 后用 tracing 日志或 adminspace 观察链路的 `mtu` 字段。

#### 4.3.5 小练习与答案

**练习 1**：为什么 TCP 链路的 `is_streamed = true`，而裸 UDP 单播 `is_streamed = false`？这对上层意味着什么？

**参考答案**：TCP 是字节流，单次 `read` 可能返回半条或多条粘在一起的消息，边界丢失，所以 `is_streamed=true`，上层必须自己用长度前缀给消息分帧（这正是 transport batch/codec 做的事，见 u9-l4）。UDP 是数据报，一次 `recv_from` 恰好返回一整个数据报，边界天然保留，`is_streamed=false`，上层无需分帧。

**练习 2**：如果想让一条 UDP 连接变得可靠，Zenoh 是怎么做到的？

**参考答案**：在 locator 的 metadata 里设置 `reliability=reliable`。`UdpLocatorInspector::is_reliable` 查到该 metadata 后返回 `true`，`LinkManagerUnicastUdp::new_link` 据此不建裸 UDP socket，而是调用 `LinkUnicastQuicUnsecure::connect`，在 UDP 之上跑一个 QUIC 传输（可靠、有序、字节流），把链路「升级」成可靠的。

**练习 3**：为什么 `LinkManagerBuilderMulticast::make` 只支持 `LinkKind::Udp`？

**参考答案**：多播需要底层协议本身支持「一份数据多端可达」的语义。在 Zenoh 支持的链路里，只有 UDP 具备 IP 多播能力（TCP/TLS/QUIC/WS 都是点对点连接，无法多播）。所以多播 manager 工厂只匹配 `Udp`，其它直接报错。

## 5. 综合实践

**任务**：画出一条 Zenoh 消息「从想要连接到真正收发字节」时，Link 层参与的完整决策与创建链路，并标注每一步用到的源码位置。

请按下面的引导完成一份文字版的「Link 层决策图」：

1. 假设 transport 层要连接 `tcp/192.0.2.1:7447`。
2. 写出从协议串到拿到 `LinkManagerUnicastTcp` 的分派路径，引用 [io/zenoh-link/src/lib.rs:394](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-link/src/lib.rs#L394)（`LinkKind::try_from`）与 [io/zenoh-link/src/lib.rs:396](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-link/src/lib.rs#L396)（`LinkManagerUnicastTcp::new`）。
3. 写出 transport 如何调它：[io/zenoh-transport/src/unicast/manager.rs:398](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/manager.rs#L398)。
4. 假设现在是 `udp/224.0.0.1:7447`（多播）。说明它为什么走的是 `LinkManagerBuilderMulticast::make` 而不是 unicast，引用 [io/zenoh-link/src/lib.rs:432-440](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-link/src/lib.rs#L432-L440)，并说明多播与单播在 `LinkManager` 接口上的差异（参考 4.1）。
5. 把两种情况各得到的 `Link` 的 `is_reliable` / `is_streamed` / `get_mtu` 列出来（参考 4.3 的对比表）。

**预期成果**：一张清晰的「协议串 → LinkKind → LinkManager → Link（含属性）」决策表，能解释清楚 Zenoh 是如何用同一套抽象驾驭 TCP / UDP（含多播与可靠变体）等多种链路的。

## 6. 本讲小结

- **Link 层是内核最底层**：它把「端点」变成「可读写的字节连接」，单播用 `LinkManagerUnicastTrait` + `LinkUnicastTrait`，多播用 `LinkManagerMulticastTrait` + `LinkMulticastTrait`，二者模型差异源于「连接 vs 无连接」。
- **LinkManager 管状态、Link 管收发**：manager 持监听 socket 和 accept 循环，通过 `flume` channel 把新链路上交给 transport 层；link 只暴露 `get_mtu/is_reliable/is_streamed/write/read/close`。
- **LocatorInspector 是建连前的质询官**：靠 locator 协议串 + 可选 metadata 判断可靠性与多播能力，**metadata 覆盖优先、协议默认值兜底**。
- **协议串经 LinkKind 分派到具体实现**：`LinkKind::try_from` 把协议串变成枚举 tag，`LinkManagerBuilderUnicast/Multicast::make` 用 `match` 选出具体 `LinkManager*`。
- **TCP 天然可靠+字节流（MTU≈65535）**，**UDP 默认不可靠+数据报（MTU 受 OS 限制）但可经 metadata 升级为 QUIC-over-UDP 的可靠链路**；多播目前只有 UDP 一种实现，用「单播 socket 写 + 多播 socket 读」的双 socket 模型。
- **MTU 等参数多为 `zconfigurable!` 运行时可调静态量**，可用环境变量调优。

## 7. 下一步学习建议

本讲只讲了「Link 层如何提供一条可读写的连接」，但还没有讲 transport 层如何**管理多条链路、建连握手、保活、批处理与分帧**。建议接着学：

- **u9-l2 传输层：unicast 与 multicast 管理**：看 `TransportManager` 如何用本讲的 `LinkManagerBuilder` 创建/复用 manager，以及 `lease/keep_alive/超时` 等配置如何影响连接生命周期。
- **u9-l3 传输建连、认证与内部状态机**：看 Open/Close 握手如何交换 zid/whatami/lease，以及认证（usrpwd/pubkey）如何接入。
- **u9-l4 批处理、分片与优先级管道**：理解 `is_streamed` 为何重要——字节流链路需要 batch/分帧，而大消息要在 MTU 之上分片、对端重组。

源码上，可以继续阅读其它链路实现（`io/zenoh-links/zenoh-link-tls`、`zenoh-link-quic`、`zenoh-link-ws`）对比它们如何实现同一套 `LinkUnicastTrait` 与 `LocatorInspector`，体会这套抽象的普适性。
