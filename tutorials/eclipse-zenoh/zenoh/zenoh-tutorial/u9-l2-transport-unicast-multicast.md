# Transport 层：unicast 与 multicast 管理

## 1. 本讲目标

本讲进入 Zenoh 协议栈的「传输层（Transport）」。学完之后，你应该能够：

- 说清 `TransportManager` 是什么、为什么用一个统一对象同时管理 unicast（点对点）与 multicast（组播）两类传输。
- 认识 `TransportManagerConfigUnicast` / `TransportManagerConfigMulticast` 里的关键参数：`lease`、`keep_alive`、`open_timeout`、`accept_timeout`、`accept_pending`、`max_sessions`、`join_interval`。
- 解释 `lease` 与 `keep_alive` 如何配合实现「连接保活」与「超时断开」，并掌握实际的 KeepAlive 发送间隔公式。
- 区分 `TransportUnicast::schedule` 与 `TransportMulticast::schedule` 在职责与状态管理上的差异（按 zid 索引 vs 按 Locator 索引）。
- 读懂 `TransportManager` 的 builder 构造链，并能据此调参观察连接的保活与断线行为。

本讲只讲「管理」这一层：谁负责建连、谁负责保活、参数从哪来。具体的建连握手（Open/Close）、认证、批处理/分片/优先级管道分别留给后续讲义（u9-l3、u9-l4）。

## 2. 前置知识

阅读本讲前，请先确认你理解以下概念（均来自前置讲义）：

- **Link 层**（u9-l1）：`Link` 与 `LinkManager` 把一个 endpoint（如 `tcp/127.0.0.1:7447`）变成可读写的字节连接；`LocatorInspector` 能凭协议串判断「是否可靠」「是否多播」。本讲的 `TransportManager` 就建立在 Link 层之上。
- **WhatAmI 角色**（u2-l3）：router / peer / client，决定建连拓扑。
- **ZenohId（zid）**（u2-l1）：节点的全局唯一标识；unicast 传输「一个对端 zid 对应一条传输」。
- **QoS 与优先级**（u3-l3）：`Reliability`、`CongestionControl`、`Priority`。传输层会为每个优先级各维护一条发送队列。
- **Primitives / Face**（u7-l2）：路由层的 `Face` 持有一个出站 `EPrimitives`（即 `Mux`），它最终调用 `TransportUnicast::schedule` 把消息送上网。本讲是这个调用链的「最末端」。
- **Runtime**（u7-l1、u7-l3）：`zenoh::open` 的执行链 `RuntimeBuilder::build → Session::init → Runtime::start`；`TransportManager` 是在 `Runtime` 构造阶段就建好的。

一句话定位：Link 层回答「字节怎么收发」，**Transport 层回答「两条 Zenoh 节点之间的逻辑连接怎么建立、怎么保持、怎么断开、怎么并发收发协议消息」**。它把裸字节连接「升级」成符合 Zenoh 传输协议的逻辑传输（带序列号、保活、分片、QoS）。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `io/zenoh-transport/` 这个**内部 crate** 下（属内部实现，不保证稳定，应用不应直接依赖，但读源码理解原理时它是主角）：

| 文件 | 作用 |
| --- | --- |
| [io/zenoh-transport/src/lib.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/lib.rs) | crate 入口。定义传输层的回调契约：`TransportEventHandler`、`TransportPeerEventHandler`、`TransportMulticastEventHandler`，以及 `TransportPeer` 数据结构。 |
| [io/zenoh-transport/src/manager.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/manager.rs) | **本讲主角**。`TransportManager`、`TransportManagerConfig`、`TransportManagerBuilder` 的定义；统一 builder、`from_config`、`build`、`close`、`add_listener` 分派。 |
| [io/zenoh-transport/src/unicast/manager.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/manager.rs) | unicast 传输的管理器扩展：`TransportManagerConfigUnicast`、状态表、`open_transport_unicast`、`handle_new_link_unicast`、`max_sessions` 校验。 |
| [io/zenoh-transport/src/unicast/mod.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/mod.rs) | `TransportUnicast` 句柄定义：`schedule`、`close`、`get_peer` 等公开方法。 |
| [io/zenoh-transport/src/multicast/manager.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/multicast/manager.rs) | multicast 传输的管理器扩展：`TransportManagerConfigMulticast`、状态表（按 Locator 索引）、`open_transport_multicast`、`join_interval`。 |
| [io/zenoh-transport/src/multicast/mod.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/multicast/mod.rs) | `TransportMulticast` 句柄定义：`schedule`、`get_peers` 等。 |
| [io/zenoh-transport/src/unicast/universal/transport.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/universal/transport.rs) | unicast 通用传输实现：计算 `keep_alive = lease / keep_alive`，启动 TX/RX 循环。 |
| [io/zenoh-transport/src/unicast/universal/link.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/universal/link.rs) | TX/RX 任务：KeepAlive 的发送逻辑（出站）与 lease 超时判定（入站）。 |
| [commons/zenoh-config/src/defaults.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-config/src/defaults.rs) | 各配置项的默认值（`lease=10000ms`、`keep_alive=4`、`max_sessions=1000` 等）。 |
| [zenoh/src/net/runtime/mod.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/mod.rs) | 上游：`Runtime` 构造 `TransportManager` 的真实调用点，把本讲与更高层接起来。 |

## 4. 核心概念与源码讲解

### 4.1 TransportManager：统一管理 unicast 与 multicast

#### 4.1.1 概念说明

`TransportManager` 是传输层的「总管」。它把上一讲的 Link 层（`Link` / `LinkManager`）和上层路由（`Face` / `Mux`）衔接起来：Link 层提供的是「一条裸字节连接」，而 `TransportManager` 在其上建立「符合 Zenoh 传输协议的逻辑传输」——负责建连握手、保活心跳、序列号、分片重组、按优先级分道收发。

关键设计：**它不强迫使用者区分 unicast 与 multicast**。对外，`add_listener` / `del_listener` / `get_listeners` 这些方法是统一的；内部由 `locator_inspector.is_multicast()` 自动分派到对应的 unicast 或 multicast 子系统。这样上层（`Runtime`）只持有一个 `TransportManager`，就能同时处理点对点连接和组播组。

`TransportManager` 内部持有：

- `config: Arc<TransportManagerConfig>` —— 所有不可变配置（zid、whatami、batch_size、queue_size、以及 unicast/multicast 子配置）。
- `state: Arc<TransportManagerState>` —— 可变状态，由 unicast 状态表与 multicast 状态表两部分组成。
- `prng` / `cipher` —— 随机数与加密原语（供建连握手与可选加密用）。
- `locator_inspector` —— 判断 endpoint 是否多播，用于分派。
- `new_unicast_link_sender` —— 一条 `flume` channel，Link 层 accept 到新连接时通过它上交给传输层。
- `task_controller` —— 统一管理传输层后台任务的生命周期（保活循环、accept 循环等），`close` 时统一终止。

#### 4.1.2 核心流程

`TransportManager` 的诞生与使用遵循固定的 builder 流程：

```text
TransportManager::builder()               // 1. 拿到一个默认 builder
    .from_config(&config).await?           // 2. 从用户 Config 填充各项
    .whatami(...)                          //    （可选）覆盖单个字段
    .bound_callback(...)                   //    （可选）设置 region 边界回调
    .build(handler).await?                 // 3. 组装成 TransportManager
                                          //    内部再分别 build unicast / multicast 子状态
→ TransportManager::new(...)               // 4. spawn 一个 Net 任务，循环接收新 incoming link
→ 运行期 add_listener(endpoint)            // 5. 按 is_multicast 分派到 unicast/multicast
→ 运行期 open_transport_unicast(...)       // 6. 主动建连（见 4.2）
→ 运行期 schedule(message)                 // 7. 发送协议消息（见 4.2/4.3）
→ TransportManager::close().await          // 8. 关闭：先关 unicast、再关 multicast、最后终止所有任务
```

特别注意第 4 步：`TransportManager::new` 不会阻塞，而是 spawn 一个长期运行的 Net 任务，从 `new_unicast_link_receiver` 这条 channel 里取 Link 层 accept 到的新连接，再交给 `handle_new_link_unicast` 处理。这就是「被动接入」的入口（与「主动建连」`open_transport_unicast` 相对）。

#### 4.1.3 源码精读

**TransportManagerConfig** —— 总配置，把 unicast 与 multicast 子配置打包在一起：

[io/zenoh-transport/src/manager.rs:111-134](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/manager.rs#L111-L134) —— 注意其中 `unicast: TransportManagerConfigUnicast` 与 `multicast: TransportManagerConfigMulticast` 两个字段，以及 `queue_size: [usize; Priority::NUM]`（每个优先级一条队列的容量）、`batch_size`、`handler: Arc<dyn TransportEventHandler>`（建连成功后回调上层的钩子）。

**handler 回调契约** —— 传输层在「新建一条传输」时通过它通知上层：

[io/zenoh-transport/src/lib.rs:46-57](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/lib.rs#L46-L57) —— `TransportEventHandler` 只有两个方法：`new_unicast`（点对点传输建立时）和 `new_multicast`（加入组播组时）。它们返回的 `TransportPeerEventHandler` 就是入站消息的回调出口（路由层的 `DeMux` 实现了它，见 u7-l2）。

**from_config：用户 Config → builder** —— 这是「参数从哪来」的关键：

[io/zenoh-transport/src/manager.rs:390-444](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/manager.rs#L390-L444) —— 这里把 `ExpandedConfig` 里的 `transport/link/tx/...` 一一翻译成 builder 字段（`batch_size`、`defrag_buff_size`、`wait_before_drop`、`queue_size`、`tx_threads`、`protocols` 等），最后再分别调用 `TransportManagerBuilderUnicast::from_config` 和 `TransportManagerBuilderMulticast::from_config` 填充两个子配置。

**build：组装并初始化加密原语**：

[io/zenoh-transport/src/manager.rs:446-512](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/manager.rs#L446-L512) —— `build` 用 `PseudoRng::from_entropy()` 初始化 PRNG，再分别 `self.unicast.build()`、`self.multicast.build()` 构造两个子状态，把 `QueueSizeConf` 展开成 `[usize; Priority::NUM]` 数组，最后交给 `TransportManager::new`。

**TransportManager::new：spawn Net 任务接收入站连接**：

[io/zenoh-transport/src/manager.rs:596-642](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/manager.rs#L596-L642) —— 这里创建了 `new_unicast_link_sender/receiver` 这对 channel，并在 `ZRuntime::Net` 上 spawn 一个 `loop`：`select!` 监听 `new_unicast_link_receiver.recv_async()` 与 `cancellation_token.cancelled()`。每来一条新连接就调用 `this.handle_new_link_unicast(new_link).await`。`close` 时通过 `task_controller` 取消该任务。

**add_listener：按多播标志分派**：

[io/zenoh-transport/src/manager.rs:671-681](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/manager.rs#L671-L681) —— 这就是「统一入口、内部分派」的体现：`locator_inspector.is_multicast(&endpoint.to_locator())` 为真走 `add_listener_multicast`，否则走 `add_listener_unicast`。

**上游真实调用点（Runtime）**：

[zenoh/src/net/runtime/mod.rs:726-743](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/mod.rs#L726-L743) —— `Runtime` 正是用 `TransportManager::builder().from_config(&config).await?.whatami(...).bound_callback(...).build(handler)` 这条链构造出 `transport_manager` 的。这印证了「用户 Config → TransportManager」是唯一来源，没有第二条路。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：建立「用户配置键 → builder 方法 → 默认值」的映射直觉。

**操作步骤**：

1. 打开 [manager.rs 的 `from_config`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/manager.rs#L390-L444)，逐行对照每个 `self = self.xxx(...)` 调用，记下它读取的配置路径（如 `link.tx().batch_size()`）。
2. 对每个字段，去 [defaults.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-config/src/defaults.rs#L251-L264) 查默认值。
3. 整理成一张表（示例前两行）：

| 配置路径（Config 中的键） | builder 方法 | 默认值 |
| --- | --- | --- |
| `transport/link/tx/batch_size` | `batch_size` | `BatchSize::MAX` |
| `transport/link/tx/lease` | （unicast 子 builder）`lease` | `10_000` ms |

**需要观察的现象**：你会发现 `from_config` 里**没有**直接出现 `lease` / `keep_alive`——它们是在 unicast/multicast 子 builder 的 `from_config` 里读取的（见 4.2.3、4.3.3）。这正是「总 builder + 子 builder」的两层结构。

**预期结果**：完成一张涵盖 `batch_size`、`defrag_buff_size`、`link_rx_buffer_size`、`tx_threads`、`protocols`、`region_name` 的映射表，并理解 unicast/multicast 各自有独立的 `from_config`。

#### 4.1.5 小练习与答案

**练习 1**：`TransportManager` 为什么只用一个 `task_controller` 管理所有后台任务，而不是 unicast/multicast 各一个？

**参考答案**：因为 `close()` 需要一次性、有序地终止整个传输层（先关 unicast、再关 multicast、最后 `task_controller.terminate_all_async()`，见 [manager.rs:662-666](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/manager.rs#L662-L666)）。统一控制器保证 Session 关闭时所有保活循环、accept 循环都能被可靠取消，不会留下孤儿任务。

**练习 2**：`add_listener` 是怎么决定一个 endpoint 走 unicast 还是 multicast 的？

**参考答案**：调用 `locator_inspector.is_multicast(&endpoint.to_locator())`（[manager.rs:671-681](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/manager.rs#L671-L681)）。判定依据来自 u9-l1：地址落入多播地址段（IPv4 `224.0.0.0/4` 或 IPv6 `ff00::/8`）即为多播。

---

### 4.2 TransportUnicast：点对点传输管理

#### 4.2.1 概念说明

`TransportUnicast` 是两个 Zenoh 节点之间的「点对点逻辑传输」。它是 Zenoh 网络里最主要的承载通道——绝大多数 pub/sub、query/reply 数据都走它。一条 `TransportUnicast` 对应**一个对端 zid**，并可能在其上叠加多条物理 Link（multilink，需 feature）。

它承担的职责：

- **建连握手**：与对端交换 Open/Close 消息，协商 zid、whatami、lease、初始序列号等（细节留给 u9-l3）。
- **保活（KeepAlive）与租约（lease）**：周期性发送 KeepAlive 心跳，并在 lease 时间内收不到任何对端消息则判定连接已死。
- **并发收发**：把上层 `schedule(message)` 投来的协议消息按优先级分道发送；接收侧解码后回调 `TransportPeerEventHandler::handle_message`。
- **会话上限与防 DoS**：通过 `max_sessions`、`accept_pending`、`accept_timeout` 限制连接数量与建连速率。

`TransportUnicast` 本身是个**轻量句柄**：内部是 `Weak<dyn TransportUnicastTrait>`（弱引用），所以它克隆便宜、可安全持有，且当真正的传输被关闭时方法会返回 `Err("Transport unicast closed")`。

#### 4.2.2 核心流程

**主动建连**（我方发起）：

```text
open_transport_unicast(endpoint)
→ open_transport_unicast_inner(endpoint, expected_zid)
   → 校验 is_multicast==false
   → new_link_manager_unicast(endpoint)        // 按协议取/建 LinkManager
   → manager.new_link(endpoint)                // Link 层建立字节连接
   → establishment::open::open_link(...)        // 发起 Open 握手（受 open_timeout 约束）
   → init_transport_unicast(...)                // 注册进 transports 表（见下）
```

**被动接入**（对端发起，由 4.1 的 Net 任务驱动）：

```text
handle_new_link_unicast(link)                  // Link 层 accept 到新连接
→ 若 incoming >= accept_pending：直接 close，防 DoS
→ 否则 incoming +1，在 ZRuntime::Acceptor 上 spawn：
   → tokio::time::timeout(accept_timeout, accept_link(...))   // 受 accept_timeout 约束
   → init_transport_unicast(...)
```

**注册进表**（`init_transport_unicast`）：

```text
锁住 transports 表
→ 若该 zid 已存在：init_existing_transport_unicast（加一条 Link 到已有传输，multilink 场景）
→ 否则：init_new_transport_unicast
   → 校验 config.zid != self.zid()（禁止连自己）
   → 校验 guard.len() < max_sessions（会话上限）
   → 选择传输实现：lowlatency → TransportUnicastLowlatency；否则 TransportUnicastUniversal
   → add_link → send_open_ack → 插入表 → 通知 handler.new_unicast
```

**保活与租约的数学关系**（本模块的核心）。配置里的 `keep_alive` 字段**不是时长，而是份数**（`usize`）。真正的 KeepAlive 发送间隔为：

\[
\text{KeepAlive 间隔} = \frac{\text{lease}}{\text{keep\_alive}}
\]

默认 `lease = 10_000 ms`、`keep_alive = 4`，故默认每 `2500 ms` 发一次心跳。源码注释引用 ITU-T G.8013/Y.1731：当 3.5 倍目标间隔内收不到消息即视为链路故障；`keep_alive=4`（即 lease 的 1/4）正是为此预留余量。

双向监控：

- **出站（TX）**：跟踪「距上次发送消息的时间」；若一个 KeepAlive 间隔内没发出任何 control/data 消息，就主动发一个 `KeepAlive`，让对端知道我方还活着。
- **入站（RX）**：跟踪「距上次收到消息的时间」；若整个 `lease` 内没收到任何消息（含对端的心跳），则 `bail!("expired after N milliseconds")`，触发断连。

#### 4.2.3 源码精读

**TransportManagerConfigUnicast** —— unicast 的关键参数全部在这里：

[io/zenoh-transport/src/unicast/manager.rs:56-69](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/manager.rs#L56-L69) —— `lease`、`keep_alive`、`open_timeout`、`accept_timeout`、`accept_pending`、`max_sessions`、`is_qos`、`is_lowlatency`。注意 `keep_alive: usize`（份数，非时长）。

**keep_alive 语义的设计说明**（源码注释本身就在解释）：

[io/zenoh-transport/src/unicast/manager.rs:146-151](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/manager.rs#L146-L151) —— 注释明确写道：「set the actual keep_alive timeout to one fourth of the lease time」，并引用 ITU-T G.8013/Y.1731 的 3.5 倍判据。

**from_config（unicast 子 builder）** —— 参数从用户 Config 读取的真正位置：

[io/zenoh-transport/src/unicast/manager.rs:249-279](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/manager.rs#L249-L279) —— `lease` 与 `keep_alive` 来自 `transport/link/tx/lease` 和 `transport/link/tx/keep_alive`（与 multicast 共享同一份 link tx 配置）；`open_timeout`、`accept_timeout`、`accept_pending`、`max_sessions`、`qos`、`lowlatency` 来自 `transport/unicast/...`。`build` 时还会校验 `qos` 与 `lowlatency` 互斥（[L285-L287](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/manager.rs#L285-L287)）。

**会话上限校验**：

[io/zenoh-transport/src/unicast/manager.rs:619-634](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/manager.rs#L619-L634) —— `init_new_transport_unicast` 里，若 `guard.len() >= self.config.unicast.max_sessions`，则以 `close::reason::INVALID` 拒绝新连接。这就是 `max_sessions`（默认 1000）的强制力。

**主动建连与 open_timeout**：

[io/zenoh-transport/src/unicast/manager.rs:844-890](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/manager.rs#L844-L890) —— 整个「`new_link` + `open_link` 握手」被包在 `tokio::time::timeout(self.config.unicast.open_timeout, ...)` 里（[L880-L889](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/manager.rs#L880-L889)）。`open_timeout`（默认 10s）就是「主动建连最多等多久」。

**被动接入与 accept_pending / accept_timeout（防 DoS）**：

[io/zenoh-transport/src/unicast/manager.rs:926-961](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/manager.rs#L926-L961) —— 若 `incoming` 计数已达 `accept_pending`（默认 100），直接 `link.close()` 拒绝（注释直言这是为了防 DoS）；否则在 `ZRuntime::Acceptor` 上 spawn，用 `accept_timeout`（默认 10s）约束整个 `accept_link` 握手。

**保活间隔的计算**（本模块最关键的一行）：

[io/zenoh-transport/src/unicast/universal/transport.rs:329-331](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/universal/transport.rs#L329-L331) —— `let keep_alive = self.manager.config.unicast.lease / self.manager.config.unicast.keep_alive as u32;` 紧接着 `link.start_tx(..., keep_alive)`、`link.start_rx(..., other_lease)`。这一行就是公式 \(\text{lease}/\text{keep\_alive}\) 的落点。

**TX 任务的 KeepAlive 发送**：

[io/zenoh-transport/src/unicast/universal/link.rs:306-312](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/universal/link.rs#L306-L312) —— `keep_alive_tracker.wait_if(...)` 超时（即一个间隔内没发任何消息）时，构造 `KeepAlive` 并以 `Priority::Control` 发出。

**RX 任务的 lease 超时判定**：

[io/zenoh-transport/src/unicast/universal/link.rs:447-449](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/universal/link.rs#L447-L449) —— `lease_tracker.wait_if(...)` 超时（整个 lease 内没收到任何消息）时，`bail!("{link}: expired after {} milliseconds", ...)`，这正是「超时断开」的触发点。

**TransportUnicast 句柄与 schedule**：

[io/zenoh-transport/src/unicast/mod.rs:73-74](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/mod.rs#L73-L74) 定义句柄为 `Weak<dyn TransportUnicastTrait>`；[io/zenoh-transport/src/unicast/mod.rs:140-144](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/mod.rs#L140-L144) 的 `schedule` 先 `get_inner()` 把 `Weak` 升级为 `Arc`（传输已关则返回 `Err`），再委托给内部实现的 `schedule(message)`，返回 `ZResult<bool>`（是否成功送出）。

#### 4.2.4 代码实践（可运行型：调小 lease 观察断线）

**实践目标**：直观验证 `lease` 如何决定「对端失联后本端多久断开」。

**操作步骤**：

1. 准备两个终端。终端 A 启动一个 router 并把 lease 调小到 2 秒、开启 debug 日志：
   ```bash
   RUST_LOG=zenoh_transport=debug cargo run -p zenohd -- \
     --cfg 'transport/link/tx/lease:"2000"' --cfg 'transport/link/tx/keep_alive:"4"'
   ```
   （配置键名以本仓库 [DEFAULT_CONFIG.json5](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/DEFAULT_CONFIG.json5) 为准；`lease` 单位为毫秒。）
2. 终端 B 以 client 模式连上它：
   ```bash
   cargo run --example z_sub -- -e tcp/127.0.0.1:7447 -m client
   ```
3. 等 A、B 建连成功后，**强制杀掉终端 B**（例如 `Ctrl+\` 或直接关闭终端，模拟对端失联而非优雅关闭）。

**需要观察的现象**：

- 建连正常时，终端 A 的 debug 日志里应能周期性看到 KeepAlive 相关的收发痕迹（默认每 `lease/keep_alive = 2000/4 = 500ms` 一次心跳）。
- B 被强杀后，由于不再有心跳到达，A 端应在约 **2 秒（= lease）** 内触发 `expired after 2000 milliseconds`（对应 [link.rs:447-449](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/universal/link.rs#L447-L449)），并关闭该传输。

**预期结果**：A 在 lease 时长左右感知到 B 的失联并断开连接；对比默认 lease=10s，断开明显更快。**待本地验证**：确切的日志字符串可能因版本而异，请以实际 `RUST_LOG=zenoh_transport=debug` 输出为准。

#### 4.2.5 小练习与答案

**练习 1**：把 `keep_alive` 从 4 调成 1（lease 不变），KeepAlive 发送频率会如何变化？会更省带宽还是更费带宽？

**参考答案**：KeepAlive 间隔 = `lease / keep_alive`。`keep_alive=1` 时间隔 = `lease`（10s），心跳频率降为原来的 1/4，**更省带宽**，但代价是失联检测的余量变薄——对端一旦丢几个心跳就更易被判死。`keep_alive` 越大心跳越勤、越早发现故障、也越费带宽。

**练习 2**：为什么 `is_qos` 与 `is_lowlatency` 不能同时为真？

**参考答案**：QoS 模式为每个优先级维护独立队列与处理路径以支持分道与背压；lowlatency 模式则是为最低延迟裁剪掉的极简路径。二者在发送结构上互斥，故 `build` 里 `bail!("'qos' and 'lowlatency' options are incompatible")`（[unicast/manager.rs:285-287](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/manager.rs#L285-L287)）。

---

### 4.3 TransportMulticast：组播传输管理

#### 4.3.1 概念说明

`TransportMulticast` 是「一对多」的组通信传输，用一条 UDP 多播链路同时与多个 peer 通信。它适用于局域网内大量节点的发现与轻量广播（如 scouting 的多播发现就建立在它之上）。

与 unicast 最大的结构差异在于**索引方式**：

- unicast 的 `transports` 表按**对端 zid** 索引（`HashMap<ZenohIdProto, ...>`），一条传输对应一个对端。
- multicast 的 `transports` 表按**Locator（多播组地址）** 索引（`HashMap<Locator, ...>`），一条传输对应一个多播组，而一个组里可能有**多个 peer**。

因此 `TransportMulticast` 提供 `get_peers()` 返回该组内当前已知的所有 peer 列表，而 `get_transport_multicast(zid)` 需要遍历所有组、检查哪个组里有该 zid。

`TransportMulticast` 同样是弱引用句柄（`Weak<TransportMulticastInner>`），`schedule(message)` 把消息一次性发到整组。

#### 4.3.2 核心流程

```text
open_transport_multicast(endpoint)
→ 校验 supported_links、locator_inspector.is_multicast==true
→ new_link_manager_multicast(endpoint)        // 按 LinkKind 取/建 LinkManager
→ manager.new_link(endpoint)                  // Link 层建立多播 socket
→ establishment::open_link(...)               // 多播建连（加入组）

add_listener_multicast(endpoint)              // 「监听」==「加入组」
→ 直接调用 open_transport_multicast(endpoint)

保活：join_interval（默认 2500ms）            // 周期性发送加入消息，宣告本节点在组内
                                            // 相当于组播版的 KeepAlive
```

一个关键概念：**对组播来说，「监听（listen）」和「打开（open）」是同一件事**——加入一个多播组既是发送也是接收。所以 `add_listener_multicast` 的实现就是直接调用 `open_transport_multicast`（见源码）。这与 unicast「listener 被动 accept、open 主动拨号」的区分截然不同。

#### 4.3.3 源码精读

**TransportManagerConfigMulticast** —— multicast 的参数集，明显比 unicast 更精简：

[io/zenoh-transport/src/multicast/manager.rs:34-42](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/multicast/manager.rs#L34-L42) —— 只有 `lease`、`keep_alive`、`join_interval`、`max_sessions`、`is_qos`。**没有** `open_timeout` / `accept_timeout` / `accept_pending`——因为组播没有「点对点握手 + 防 DoS」的需求。

**状态表：按 Locator 索引**：

[io/zenoh-transport/src/multicast/manager.rs:69-74](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/multicast/manager.rs#L69-L74) —— `transports: Arc<Mutex<HashMap<Locator, Arc<TransportMulticastInner>>>>`。对比 unicast 的 [TransportManagerStateUnicast](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/manager.rs#L102-L115)（按 zid 索引），这一处差异是理解两种传输管理差异的钥匙。

**from_config（multicast 子 builder）**：

[io/zenoh-transport/src/multicast/manager.rs:131-143](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/multicast/manager.rs#L131-L143) —— `lease`、`keep_alive` 同样来自 `transport/link/tx/...`（与 unicast 共享）；`join_interval`、`max_sessions`、`qos` 来自 `transport/multicast/...`。注意 `join_interval` 与 `max_sessions` 用了 `.unwrap()`——因为 multicast 配置里这两项是 `Option`，此处取默认。

**open_transport_multicast**：

[io/zenoh-transport/src/multicast/manager.rs:243-289](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/multicast/manager.rs#L243-L289) —— 校验 `is_multicast==true`（非多播端点直接 `bail!`），取/建 `LinkManagerMulticast`，`new_link` 后进入 `establishment::open_link`。注意它没有 unicast 那种 `tokio::time::timeout` 包裹的握手。

**add_listener_multicast 就是 open**：

[io/zenoh-transport/src/multicast/manager.rs:339-343](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/multicast/manager.rs#L339-L343) —— 函数体只有两行：记录 locator、调用 `open_transport_multicast(endpoint)`。这就是「监听 == 加入组」的代码证据。

**默认值**：

[commons/zenoh-config/src/defaults.rs:213-222](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-config/src/defaults.rs#L213-L222) —— `join_interval = Some(2500)` ms、`max_sessions = Some(1000)`、`qos.enabled = false`（多播默认不开 QoS 分道）。对比 [TransportUnicastConf 默认值](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-config/src/defaults.rs#L198-L211)（`qos.enabled = true`），可见两类传输的默认 QoS 策略相反。

**TransportMulticast 句柄**：

[io/zenoh-transport/src/multicast/mod.rs:54-55](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/multicast/mod.rs#L54-L55) 定义 `TransportMulticast(Weak<TransportMulticastInner>)`；[schedule](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/multicast/mod.rs#L112-L116) 与 [get_peers](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/multicast/mod.rs#L96-L100) 是它的核心方法。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：理解「multicast 为什么不能像 unicast 那样按 zid 索引传输」。

**操作步骤**：

1. 阅读 [open_transport_multicast](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/multicast/manager.rs#L243-L289)，确认建连结果如何放入 `transports` 表（key 是什么）。
2. 阅读 [get_transport_multicast](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/multicast/manager.rs#L291-L298)（按 zid 查询）：它如何遍历所有组、用 `t.get_peers().iter().any(|p| p.zid == *zid)` 匹配。
3. 对比 unicast 的 [get_transport_unicast](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/manager.rs#L892-L896)（直接 `transports.get(peer)`，O(1)）。

**需要观察的现象**：unicast 按 zid 查是直接哈希查找；multicast 按 zid 查必须遍历所有组、再遍历组内 peer，复杂度更高。

**预期结果**：写出一句话结论——「unicast 一条传输 = 一个对端，故按 zid 索引；multicast 一条传输 = 一个多播组（含多个 peer），故按 Locator 索引，查 zid 需双重遍历」。

#### 4.3.5 小练习与答案

**练习 1**：multicast 配置里为什么没有 `accept_pending` 和 `accept_timeout`？

**参考答案**：这两个参数服务于 unicast 的「点对点握手 + 防并发接入/防 DoS」场景（[unicast/manager.rs:926-961](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/manager.rs#L926-L961)）。组播没有点对点握手，加入一个组就是打开一个多播 socket，不存在「大量半连接」的攻击面，因此不需要这两个限制。

**练习 2**：`join_interval`（默认 2500ms）在多播里扮演什么角色？

**参考答案**：它相当于组播版的 KeepAlive——周期性地向组里发送加入消息，宣告「本节点还在组内」。其他成员据此维护 `get_peers()` 列表，并在超过 `lease` 仍收不到某成员消息时把它剔除。

---

## 5. 综合实践

**任务**：梳理 `TransportManager` 的 builder 配置项，写一段说明它们如何共同决定「连接保活」与「超时断开」行为，并用一张对比表总结 unicast 与 multicast 的差异。

**操作步骤**：

1. **梳理参数来源**。从 [manager.rs 的 from_config](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/manager.rs#L390-L444) 出发，分三类整理：
   - 总配置（`batch_size`、`defrag_buff_size`、`link_rx_buffer_size`、`queue_size`、`tx_threads`、`protocols`）；
   - unicast 专属（[unicast from_config](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/manager.rs#L249-L279)：`lease`、`keep_alive`、`open_timeout`、`accept_timeout`、`accept_pending`、`max_sessions`、`qos`、`lowlatency`）；
   - multicast 专属（[multicast from_config](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/multicast/manager.rs#L131-L143)：`lease`、`keep_alive`、`join_interval`、`max_sessions`、`qos`）。
2. **画出保活/断线时序**。用文字描述一条 unicast 连接的全生命周期：建连（受 `open_timeout` / `accept_timeout` 约束）→ 运行期 TX 每 `lease/keep_alive` 发一次 KeepAlive、RX 用 `lease` 做死链判据 → 失联后在 `lease` 内断开。引用 [transport.rs:329-331](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/universal/transport.rs#L329-L331)、[link.rs:306-312](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/universal/link.rs#L306-L312)、[link.rs:447-449](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/unicast/universal/link.rs#L447-L449)。
3. **填出对比表**：

| 维度 | TransportUnicast | TransportMulticast |
| --- | --- | --- |
| 通信模型 | 点对点（一对一） | 组播（一对多） |
| `transports` 表 key | 对端 `ZenohIdProto` | 多播组 `Locator` |
| 建连方式 | 主动 `open` / 被动 `accept`（握手） | 加入组（`add_listener` == `open`） |
| 心跳 | KeepAlive，间隔 `lease/keep_alive` | `join_interval`（默认 2500ms） |
| 死链判据 | `lease` 内无任何入站消息 | `lease` 内无某 peer 消息 |
| 防 DoS / 并发接入 | `accept_pending` + `accept_timeout` | 无 |
| 会话上限 | `max_sessions`（默认 1000） | `max_sessions`（默认 1000） |
| 默认 QoS 分道 | 开启（`qos.enabled=true`） | 关闭（`qos.enabled=false`） |
| 建连超时 | `open_timeout`（默认 10s） | 无 |

**预期结果**：产出一份一页纸的「Transport 层参数与行为说明」，能回答「调小 `lease` 会怎样」「`keep_alive` 变大意味着什么」「为什么多播没有 accept_timeout」等问题。完成后再做 4.2.4 的 lease 调参实验，把理论与实测对照。

## 6. 本讲小结

- `TransportManager` 是传输层总管，用**一个统一 builder + 一个 handler** 同时管理 unicast 与 multicast；对外 `add_listener` 统一入口，内部靠 `locator_inspector.is_multicast()` 自动分派。
- 它由 `Runtime` 在 `zenoh::open` 阶段经 `builder().from_config(&config).build(handler)` 唯一构造，所有参数都来自用户 `Config`，没有第二条来源。
- unicast 的核心是「保活 + 租约」：KeepAlive 发送间隔 \(\text{lease}/\text{keep\_alive}\)（默认 10s/4 = 2.5s）；TX 主动发心跳、RX 用 `lease` 判死链，超时即 `expired` 断开。
- unicast 用 `max_sessions`（会话上限）、`accept_pending` + `accept_timeout`（防 DoS / 并发接入约束）、`open_timeout`（主动建连超时）三组参数把连接数量与速率约束在可控范围。
- `TransportUnicast` 是 `Weak<dyn TransportUnicastTrait>` 弱引用句柄，`schedule(message)` 是上层 `Mux` 把消息送上网的最终调用点；`is_qos` 与 `is_lowlatency` 互斥。
- multicast 状态表按 `Locator` 索引（一条传输 = 一个多播组，含多个 peer），「监听」即「加入组」，用 `join_interval` 做组播心跳，默认不开 QoS 分道。

## 7. 下一步学习建议

- **u9-l3 传输建连、认证与内部状态机**：本讲只说「`open_transport_unicast` 会发起握手」，但 Open/Close 消息如何交换 zid/whatami/lease、`send_open_ack` 的时序、`transport_unicast_inner` 的 `TransportStatus` 状态机（`Uninitialized → Alive → Closed`）都在下一讲展开。
- **u9-l4 批处理、分片与优先级管道**：本讲的 `schedule` 只说「投递消息」，但消息如何被 `BatchConfig` 打包成帧、按 `Priority` 分道排队、大消息如何分片并在对端 `defragmentation` 重组，是下一讲的专题。
- **回到 u7-l2 / u8-l1**：带着本讲的认识重读 `Mux::route_data` 与 `Gateway::new_transport_unicast`，你会看清「路由层 Face → `Mux` → `TransportUnicast::schedule`」这条出站链是怎么贯通的。
- **配置实战**：结合 [DEFAULT_CONFIG.json5](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/DEFAULT_CONFIG.json5) 与 [defaults.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-config/src/defaults.rs#L198-L264)，尝试用 `zenohd --cfg` 改 `transport/link/tx/lease`、`transport/unicast/max_sessions`，对照本讲的保活/断线实验加深印象。
