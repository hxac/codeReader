# Runtime 编排器：scouting 与建连

## 1. 本讲目标

在《u7-l1 Session 内部与 Runtime》里我们看到，`zenoh::open` 的执行链是 `RuntimeBuilder::build → Session::init → Runtime::start`——「先接线，再通电」。本讲就专门拆解最后一步 **`Runtime::start`（编排器，orchestrator）**：它在「通电」瞬间到底做了什么、是如何把一个孤立的节点接入网络的。

读完本讲，你应当能够：

- 说清 `Runtime::start` 按 `WhatAmI` 分派后，`bind_listeners → connect_peers → start_scout` 这三幕的先后与差异；
- 解释多播 `scout`/`responder` 这对互逆循环如何让节点彼此发现，以及发送周期的指数退避；
- 掌握 `AutoConnect` 的「matcher + 策略」模型，理解为什么两个节点不会重复互连；
- 区分「连配置里写死的端点」与「连 scouting 发现的端点」两条独立建连路径，以及 gossip scouting 与 multicast scouting 的差别。

## 2. 前置知识

本讲假定你已建立以下认知（来自前置讲义）：

- **WhatAmI 三种角色**（Router/Peer/Client，由配置 `mode` 决定，见《u2-l3》）：本讲会反复看到 `start()` 按角色分派。
- **Config 是键值树**（见《u2-l3》）：编排器大量读取 `listen/endpoints`、`connect/endpoints`、`scouting/multicast/*`、`scouting/gossip/*` 等键。
- **Scouting 协议消息 Scout/Hello**（见《u6-l1》）：本讲讲的是它们的**网络层实际收发实现**，而非公开 `zenoh::scout` API。
- **Runtime 与 RuntimeState**（见《u7-l1》）：`Runtime` 是 `Arc<RuntimeState>` 的薄壳，`RuntimeState` 持有 `TransportManager`、`Gateway`、`config`、`task_controller` 等。本讲的 `start`/`scout`/`responder` 都是 `impl Runtime` 上的方法。
- **TransportManager 是建连的真正执行者**（见《u9》系列预备）：编排器只负责「决定连谁、何时连」，真正打开传输（`open_transport_unicast`/`open_transport_multicast`）交给 `manager()`。

两个名词先澄清：

- **listener（监听端）**：本节点绑定一个端点（如 `tcp/0.0.0.0:7447`），等别人来连。对应 `config.listen.endpoints`。
- **peer（对端，配置意义上的）**：本节点主动去连的端点。对应 `config.connect.endpoints`。注意这里 `peer` 是「连接目标」之意，与 `WhatAmI::Peer`（角色）同名但含义不同，下文用「配置端点」避免混淆。

## 3. 本讲源码地图

本讲聚焦三个文件：

| 文件 | 作用 |
| --- | --- |
| [zenoh/src/net/runtime/mod.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/mod.rs) | `Runtime`/`RuntimeState` 定义、`spawn`/`spawn_abortable` 任务派发、`RuntimeBuilder::build`（接线阶段）。编排器方法大多定义在它 `impl Runtime` 块里委托到 state。 |
| [zenoh/src/net/runtime/orchestrator.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/orchestrator.rs) | **本讲主角**。`Runtime::start` 及其全部建连/发现逻辑：`start_client/peer/router`、`scout`、`responder`、`connect`、`connect_peer`、`autoconnect_all`、重试与 `StartConditions`。 |
| [zenoh/src/net/common.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/common.rs) | `AutoConnect` 结构：把配置里的 matcher 与 autoconnect 策略收口成一个小对象。 |

另有两个文件作为「调用方」与「对照」被引用：

- [zenoh/src/api/session.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs)：`zenoh::open` 的入口，确认 `start()` 的调用时机。
- [zenoh/src/net/protocol/gossip.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/gossip.rs)：gossip 发现的真正实现（跑在已建连的链路上），与 multicast scouting 形成对照。

---

## 4. 核心概念与源码讲解

### 4.1 Runtime::start：启动与建连总流程

#### 4.1.1 概念说明

`Runtime::start` 是节点「通电」的那一刻。在它之前，`RuntimeBuilder::build` 只是把所有结构体分配好——`TransportManager`、`Gateway`（路由器）、`HLC`、`config`、插件——但还没有绑定任何监听端口，也没有连任何对端，节点是一个「孤岛」。`start` 要做的就是让孤岛接上网。

它的核心思想是**三幕剧**，顺序固定：

1. **绑定监听端**（`bind_listeners`）：把 `config.listen.endpoints` 里的端点逐个绑到 `TransportManager` 上，让本节点「可被连接」。
2. **连配置端点**（`connect_peers`）：对 `config.connect.endpoints` 里写死的目标，主动发起连接。
3. **启动发现**（`start_scout`，仅当 `scouting/multicast/enabled`）：开一对多播收发循环，动态发现并连接网络中的其它节点。

这三幕按角色（Client/Peer/Router）有微妙差异，差异集中在「要不要监听」「连一个还是连全部」「要不要等 scouting」。

#### 4.1.2 核心流程

入口 `start` 按 `whatami` 分派到三个分支：

```text
Runtime::start()
  ├─ WhatAmI::Client  → start_client()
  ├─ WhatAmI::Peer    → start_peer()
  └─ WhatAmI::Router  → start_router()
```

以最典型的 **Peer** 为例，`start_peer` 的流程是：

```text
start_peer()
  ├─ 一次性读出配置：listeners / peers / scouting / wait_scouting / listen / autoconnect / addr / ifaces / delay
  ├─ bind_listeners(&listeners)        // 第 1 幕：绑定监听端
  ├─ connect_peers(&peers, false)      // 第 2 幕：连配置端点（多链路）
  ├─ if scouting { start_scout(...) }  // 第 3 幕：多播发现
  └─ 若 wait_scouting，则在 delay 内等待 StartConditions 满足（否则仅告警）
```

关键差异点（读者可对照源码自行比较）：

- **Client**：默认不监听（`listen` 角色相关配置通常为空）；`connect_peers` 走 `single_link=true`（只连一个就够，见 4.1.3）；若既没配 peers 又关了 scouting，直接 `bail!` 报错——Client 必须有出路。
- **Peer / Router**：`connect_peers` 走 `single_link=false`（连全部配置端点）；都会 `start_scout`；Router 最后固定 `sleep(delay)` 再返回，Peer 则可能 `wait` 在 `StartConditions` 上。

#### 4.1.3 源码精读

先看分派入口。`start` 本身极简，复杂度全在三个分支里：

> [zenoh/src/net/runtime/orchestrator.rs:140-146](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/orchestrator.rs#L140-L146) —— 按 `whatami()` 分派到 `start_client/start_peer/start_router`。

再看调用时机，确认它在 `build` 与 `init` **之后**才执行（即「先接线再通电」）：

> [zenoh/src/api/session.rs:1445-1453](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L1445-L1453) —— `runtime.build().await?` 之后 `Self::init(...)`，最后才 `runtime.start().await?`。

`start_peer` 是三幕剧的范本。它先把所需配置一次性锁出来（避免长时间持锁），再依次走三幕：

> [zenoh/src/net/runtime/orchestrator.rs:219-258](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/orchestrator.rs#L219-L258) —— `start_peer`：读配置 → `bind_listeners` → `connect_peers(peers, false)` → 条件 `start_scout` → 可选等待 `start_conditions.notified()`。

注意第 231 行的 `wait_scouting`，它读的是 `open.return_conditions.connect_scouted`——「open 调用是否要等到至少连上一个 scouted peer 才返回」。第 248-256 行用 `tokio::time::timeout(delay, notified())` 等待，超时只打告警不报错（只要还有配置端点 `!peers.is_empty()`），体现了「scouting 是尽力而为的便利机制」这一设计取向。

第 2 幕 `connect_peers` 带一个全局连接超时，超时则报错：

> [zenoh/src/net/runtime/orchestrator.rs:348-366](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/orchestrator.rs#L348-L366) —— `connect_peers`：用 `get_global_connect_timeout` 包一层超时，超时返回 `"Unable to connect to any of {:?}. Timeout!"`。

它按 `single_link` 标志二选一：Client 走「连上一个就成功」的 `connect_peers_single_link`，Peer/Router 走「连全部」的 `connect_peers_multiply_links`：

> [zenoh/src/net/runtime/orchestrator.rs:376-423](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/orchestrator.rs#L376-L423) —— `connect_peers_single_link`：对每个「端点组」逐个尝试，一组里任一端点连上即标记成功并 `break`，全部失败才报错。（端点组 `EndPoints` 支持 allOf/oneOf 策略，但 `warn_if_oneof` 提示 oneOf 尚未实现，回退到 allOf 行为。）

> [zenoh/src/net/runtime/orchestrator.rs:425-457](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/orchestrator.rs#L425-L457) —— `connect_peers_multiply_links`：对每个端点都尝试连接；依重试配置分别走「立即连」「带退避重试」「后台异步连」三条路。

真正打开传输的动作在 `peer_connector`——它调 `manager().open_transport_unicast(peer)`，并把成功建立的 endpoint 记进 `RuntimeSession.endpoints`（用于日后断线重连）：

> [zenoh/src/net/runtime/orchestrator.rs:459-480](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/orchestrator.rs#L459-L480) —— `peer_connector`：`open_transport_unicast` 建连，取 transport 回调，downcast 成 `RuntimeSession`，把 endpoint 写入其 `endpoints` 集合。

第 1 幕 `bind_listeners` 与之对称，把 listener 注册到 manager，并把实际可被访问的 locator 打印出来（你启动 zenohd 时看到的 `Zenoh can be reached at: ...` 就来自这里）：

> [zenoh/src/net/runtime/orchestrator.rs:525-547](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/orchestrator.rs#L525-L547) —— `bind_listeners_impl`：逐个 `add_listener`，按重试配置决定立即/重试/后台，最后 `print_locators()`。

#### 4.1.4 代码实践

**实践目标**：让一个 Peer 角色的节点主动连接到一个写死的端点，并用 `tracing` 日志观察「通电」三幕。

**操作步骤**：

1. 先起一个被连目标。最简单的方式是启动官方示例 `z_sub`（它默认是 Peer、会监听并开启 multicast scouting）：

   ```bash
   # 终端 A：被连目标，开启调试日志
   RUST_LOG=debug cargo run --example z_sub -- --mode peer --listen tcp/127.0.0.1:77477
   ```

2. 再起一个 Peer 节点显式连它（关掉 multicast scouting，强迫走「连配置端点」这条路径）：

   ```bash
   # 终端 B
   RUST_LOG=debug cargo run --example z_pub -- --mode peer \
     --connect tcp/127.0.0.1:77477 --no-multicast-scouting
   ```

   > 说明：`z_pub`/`z_sub` 共用 `CommonArgs`（见《u1-l2》），`--connect`/`--listen`/`--mode`/`--no-multicast-scouting` 会经 `From<CommonArgs> for Config` 翻译成 `connect/endpoints`、`listen/endpoints`、`mode`、`scouting/multicast/enabled=false`。

**需要观察的现象**：在终端 B 的日志里，按时间顺序应能看到编排器三幕的痕迹，大致为：

- `Using ZID: ...`（来自 `RuntimeBuilder::build`，接线阶段）
- `Try to add listener: ...` 或 `Starting with no listener endpoints!`（第 1 幕，取决于是否配了 listen）
- `Try to connect: ...: global timeout: ..., retry: ...`（第 2 幕，[orchestrator.rs:386](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/orchestrator.rs#L386)）
- `Successfully connected to configured peer ...`（[orchestrator.rs:855](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/orchestrator.rs#L855)）

**预期结果**：两个节点建立 unicast 传输，`z_sub` 收到 `z_pub` 发布的 `demo/example/zenoh-rs-pub` 样本。若把终端 A 关掉，终端 B 会因 `--no-multicast-scouting` 且无其它出路而无法自动恢复（自动重连机制见 4.3）。

> 「能否看到精确的某条日志」「退避具体多久」属于运行时行为，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Client 角色用 `single_link=true`，而 Peer/Router 用 `single_link=false`？

**参考答案**：Client 在 Zenoh 拓扑里是「叶子」，只需一条上行的链路连到 router/peer 即可拿到全网数据，多连无益还浪费资源；Peer/Router 则是 mesh 里的对等节点，需要与多个邻居分别建链以构成网状拓扑，故连全部配置端点。

**练习 2**：`start_peer` 里第 248-256 行的 `timeout(delay, notified())` 超时后为什么只告警而不报错？

**参考答案**：因为只要 `!peers.is_empty()`（已配了写死的端点并连上），节点就已经有出路、可以正常工作；scouting 只是「锦上添花」的动态发现，没发现到也不应让 `open` 失败。

---

### 4.2 scout 与 responder：多播发现的双向循环

#### 4.2.1 概念说明

第 2 幕只能连「你在配置里写死的目标」。但很多时候你并不知道对端的地址——比如同一局域网里有多少个 Zenoh 节点。**scouting** 就是用来动态发现它们的机制（公开 API 侧见《u6-l1》，本讲讲网络层实现）。

multicast scouting 基于 UDP 多播，每个参与节点同时扮演两个角色：

- **提问方（scout）**：周期性地向多播组地址（默认 `224.0.0.224:7446`）发送一个 `Scout` 消息，问「组里有哪些节点？」。
- **应答方（responder）**：监听多播组，收到 `Scout` 后，**单播**回一个 `Hello` 消息，附上自己的 `zid`、`whatami` 和可被连接的 `locators`。

注意这个非对称设计：提问是多播（一次问所有人），应答是单播（只回给提问者）。这避免了应答风暴。

一个节点通常**同时**运行这两个循环——既提问又应答，从而既能发现别人、也能被别人发现。

#### 4.2.2 核心流程

`Runtime::scout` 是一个静态方法，内部用 `tokio::select!` 并发跑「发送」与「接收」两半：

```text
Runtime::scout(sockets, matcher, mcast_addr, f)
  ├─ send 半：循环向 mcast_addr 发 Scout，sleep(delay)，delay 指数增长（1s→2s→4s→8s 封顶）
  └─ recv 半：每个 socket 并发 recv_from，解出 ScoutingMessage；
              若是 Hello 且 whatami 命中 matcher，调回调 f(hello)，返回 Loop::Break 则停
```

发送周期的退避用三个常量刻画，是理解「scouting 流量随时间衰减」的关键：

- 初始周期 \( T_0 = 1000\,\text{ms} \)
- 增长因子 \( k = 2 \)
- 上限 \( T_{\max} = 8000\,\text{ms} \)

每次发送后 `delay *= k`，直到 \( k\cdot\text{delay} > T_{\max} \) 就不再增长：

\[
T_{n+1} = \min(k\cdot T_n,\ T_{\max})
\]

于是序列为 \( 1000, 2000, 4000, 8000, 8000, \dots \)（ms）。刚启动时密集提问以快速发现邻居，稳定后退避到 8s 一次以省带宽。

`responder` 则是一个纯接收循环：

```text
responder(mcast_socket, ucast_sockets)
  └─ loop:
       recv_from(buf)              // 收到 Scout（多播）
       若来源是自己 → 忽略（避免自激）
       解码出 Scout{what, ...}
       若 what.matches(self.whatami()) → 构造 HelloProto{version, whatami, zid, locators}
                                      → 用「与提问者地址最匹配」的 socket 单播回 Hello
```

`start_scout` 是这两者的「调度器」，根据本节点是否 `listen`（应答）和是否 `autoconnect`（提问后连）开四种组合：

> [zenoh/src/net/runtime/orchestrator.rs:297-346](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/orchestrator.rs#L297-L346) —— `start_scout`：按 `(listen, autoconnect.is_enabled())` 四种组合分别 `spawn_abortable` 跑 `responder`/`autoconnect_all`，或两者 `select!` 并行。

#### 4.2.3 源码精读

先看三个退避常量：

> [zenoh/src/net/runtime/orchestrator.rs:52-54](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/orchestrator.rs#L52-L54) —— `SCOUT_INITIAL_PERIOD=1s`、`SCOUT_MAX_PERIOD=8s`、`SCOUT_PERIOD_INCREASE_FACTOR=2`。

`Runtime::scout` 的发送半构造 `Scout` 消息（带版本、`what` matcher、`zid=None`），用 `Zenoh080` codec 编码后多播出去，并按上面的公式退避：

> [zenoh/src/net/runtime/orchestrator.rs:902-946](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/orchestrator.rs#L902-L946) —— `scout` 的 `send` 半：编码 `Scout` → 对每个 socket `send_to(mcast_addr)` → `sleep(delay)` → `delay *= 2`（封顶 8s）。

接收半用 `select_all` 让所有 socket 并发接收，解出 `ScoutingMessage`，只处理 `Hello` 且 `matcher.matches(hello.whatami)` 的，调回调：

> [zenoh/src/net/runtime/orchestrator.rs:947-986](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/orchestrator.rs#L947-L986) —— `scout` 的 `recv` 半：`select_all` 并发 recv，解码后过滤 `Hello`，命中即调 `f(hello)`，返回 `Loop::Break` 则该 socket 退出。

`responder` 收到 `Scout` 后构造 `HelloProto`。注意它填的是**自己的真实信息**，并选择与提问者 IP 「octet 前缀匹配最长」的 socket 回复（多网卡场景下选最合适的出口）：

> [zenoh/src/net/runtime/orchestrator.rs:1221-1251](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/orchestrator.rs#L1221-L1251) —— `responder` 核心：`what.matches(self.whatami())` 判定要不要回，构造 `HelloProto{version, whatami, zid, locators}`，经 `get_best_match` 选出口单播发回。

`HelloProto` 的字段 `locators` 来自 `self.get_locators()`，即第 1 幕 `bind_listeners` 后 `print_locators` 写入的那些实际可连地址——这正是发现方后续 `connect` 所需的「门牌号」。

> 补充：`scout`/`responder` 用到的 socket 绑定在 `bind_mcast_port`/`bind_ucast_port`（[orchestrator.rs:629-757](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/orchestrator.rs#L629-L757)），它们设置了 `SO_REUSEADDR`、加入多播组、设 `multicast_ttl`，是标准的多播 socket 编程，此处不展开。

#### 4.2.4 代码实践

**实践目标**：观察 multicast scouting 的「提问—应答」往返，验证退避周期。

**操作步骤**：

1. 终端 A 起一个会应答的节点（默认 `listen=true`）：

   ```bash
   RUST_LOG=trace cargo run --example z_sub
   ```

2. 终端 B 起另一个节点，用 `trace` 级别看多播收发：

   ```bash
   RUST_LOG=trace cargo run --example z_pub
   ```

**需要观察的现象**：在 `trace` 级别下，能看到（关键字对照源码）：

- 终端 B：周期性 `Send Scout ... to 224.0.0.224:7446 on interface ...`（[orchestrator.rs:918](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/orchestrator.rs#L918)），且间隔从 ~1s 逐渐拉长到 ~8s。
- 终端 A：`Listening scout messages on 224.0.0.224:7446`（[orchestrator.rs:706](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/orchestrator.rs#L706)）、`Received Scout ... from ...`、随后 `Send Hello ...` 单播回 B。
- 终端 B：`Received Hello ... from ...`，并最终 `Successfully connected to newly scouted peer`（见 4.3）。

**预期结果**：两节点通过 multicast scouting 互相发现并自动建连。多播需操作系统支持（某些 CI/容器环境禁用多播，此时应改用 gossip，见综合实践）。

> 退避的精确时间点、`trace` 日志是否齐全属运行时行为，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `responder` 用单播回 `Hello`，而不是也用多播？

**参考答案**：多播应答会导致组内每个节点都收到所有人的应答，形成 N×N 的应答风暴，且提问方要重复去重。单播只把「自己的名片」发给提问者，开销最小。

**练习 2**：发送周期为什么用指数退避而不是固定间隔？

**参考答案**：启动初期网络拓扑变化最大、最需要快速发现邻居，故密集提问；稳定后拓扑少变，退避到较长间隔（8s）可显著降低稳态下的多播流量，兼顾「快速发现」与「低开销」。

---

### 4.3 AutoConnect 策略：连谁、谁连、何时重试

#### 4.3.1 概念说明

发现到对端（拿到它的 `Hello`）不等于要连它。**AutoConnect** 回答两个问题：

1. **要不要连这个发现到的节点？**——由 `matcher`（一个 `WhatAmIMatcher` 位掩码）决定：只连角色匹配的（如 Peer 只 autoconnect 到 router/peer/client）。
2. **该我连，还是该它连？**——由 `strategy` 决定。两个节点若同时发现对方、又都去连，就会建立两条冗余链路。策略 `GreaterZid` 的规则是「只连 zid 比自己小的」，于是双方比较 zid 后只有一方主动，避免重复。

这是 multicast scouting（`autoconnect_all`）和 gossip（见 4.3 末尾）共用的决策核心，被收口在 `common.rs` 的小结构 `AutoConnect` 里。

与之并行的还有一条**重连**路径：配置端点断线后，编排器会按 `connection_retry` 策略自动重连。这与 AutoConnect 不同——AutoConnect 处理「scouting 发现的新节点」，重连处理「已配置但断开的端点」。

#### 4.3.2 核心流程

multicast 发现后的建连由 `autoconnect_all` 驱动，它复用 4.2 的 `scout` 循环，只是回调换成「发现即连」：

```text
autoconnect_all(sockets, autoconnect, addr)
  └─ Runtime::scout(..., |hello| {
       if hello.locators 非空 && autoconnect.should_autoconnect(hello.zid, hello.whatami) {
           self.connect_peer(&hello.zid, &hello.locators).await;  // 连发现到的节点
       }
       Loop::Continue   // 持续发现，不 Break
     })
```

`should_autoconnect` 的判定是本模块的数学核心：

\[
\text{should} = \text{matcher.matches}(\text{whatami}) \;\land\; \text{strategy}()
\]

其中策略函数为：

\[
\text{strategy}() =
\begin{cases}
\text{true}, & \text{Always} \\
(\text{self.zid} > \text{to.zid}), & \text{GreaterZid}
\end{cases}
\]

`connect_peer` 再做一层去重：若与该 zid 已有 unicast 或 multicast 传输，就跳过；否则调 `connect` 真正打开传输。`connect` 还会过滤掉「已在配置端点里」的 locator（避免与第 2 幕重复），并用 `pending_connections` 集合防止对同一 zid 并发重复建连。

重连则由传输回调触发：当某条已建链关闭，`RuntimeSession::closed`/`del_link` 会调 `closed_session`/`closed_link`，后者从配置里取回该端点，重新跑 `peers_connector_retry`。

#### 4.3.3 源码精读

`AutoConnect` 把 matcher 与 strategy 打包成一个小对象，提供 multicast/gossip 两个构造函数与一个 `disabled()`：

> [zenoh/src/net/common.rs:8-49](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/common.rs#L8-L49) —— `AutoConnect` 结构与三个构造函数：`multicast`/`gossip` 从对应配置构造，`disabled()` 产出永远返回 false 的实例。

决策逻辑在 `should_autoconnect`，正是上面的合取公式：

> [zenoh/src/net/common.rs:61-71](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/common.rs#L61-L71) —— `should_autoconnect`：`matcher.matches(what) && strategy()`，`Always` 恒真、`GreaterZid` 比较 `self.zid > to`。注释明确写出设计意图：避免双方都尝试连接造成资源浪费。

`autoconnect_all` 把 scout 与「发现即连」缝合：

> [zenoh/src/net/runtime/orchestrator.rs:1159-1179](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/orchestrator.rs#L1159-L1179) —— `autoconnect_all`：复用 `Runtime::scout`，回调里判 `should_autoconnect` 后调 `connect_peer`，并返回 `Loop::Continue` 持续发现。

`connect_peer` 做去重（已连则跳过），再委托 `connect`：

> [zenoh/src/net/runtime/orchestrator.rs:1099-1125](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/orchestrator.rs#L1099-L1125) —— `connect_peer`：若与该 zid 既无 unicast 也无 multicast 传输，才调 `connect`；否则记 `trace`「已连」。

`connect` 负责去重 locator、防并发、按 multicast/unicast 分派打开传输：

> [zenoh/src/net/runtime/orchestrator.rs:990-1096](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/orchestrator.rs#L990-L1096) —— `connect`：`insert_pending_connection` 防并发；过滤掉已在配置端点的 locator；按 `is_multicast` 选 `open_transport_multicast` 或 `open_transport_unicast_with_zid`。

**重连**路径由传输事件回调进入编排器（`RuntimeSession` 在《u7-l1》/《u7-l2》已介绍）：

> [zenoh/src/net/runtime/orchestrator.rs:1263-1331](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/orchestrator.rs#L1263-L1331) —— `closed_session`/`closed_link`：链路断开后，从配置取回端点，重新 `peers_connector_retry` 自动重连。

**gossip scouting 的对照**：gossip 不走多播，而是**在已建连的 unicast 链路上**传播「我知道的节点列表」，从而把发现范围扩展到多播可达之外。它复用同一套 `AutoConnect` 决策（注意是 `AutoConnect::gossip` 构造）和 `connect_peer`，只是发现消息的载体从 UDP 多播变成了协议层的 gossip 消息：

> [zenoh/src/net/protocol/gossip.rs:385-409](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/protocol/gossip.rs#L385-L409) —— gossip 收到对端通告的节点后，用 `should_autoconnect` 判定，再 `connect_peer` 建连，并与 `StartConditions` 联动。

**StartConditions 与 PeerConnector**：Peer 角色在 `start` 时可能等待「至少连上一个 scouted/gossip 节点」才返回。`StartConditions` 用一个 `Vec<PeerConnector>` 登记每个待完成的连接槽，全部 `terminated` 后 `notify_one` 唤醒等待者：

> [zenoh/src/net/runtime/orchestrator.rs:62-126](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/orchestrator.rs#L62-L126) —— `PeerConnector`/`StartConditions`：登记/终止连接槽，全部终止时 `notify_one`。

`spawn_peer_connector` 在后台连配置端点时，会按 gossip/wait_declares 决定是否把该槽加入 `StartConditions`：

> [zenoh/src/net/runtime/orchestrator.rs:759-789](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/orchestrator.rs#L759-L789) —— `spawn_peer_connector`：后台重试连接，成功后登记 zid；若非 gossip 且（非 wait_declares 或非 Peer），立即终止该槽。

#### 4.3.4 代码实践

**实践目标**：对比 multicast scouting 与 gossip scouting，理解 gossip 如何在多播不可用时仍能发现节点。

**操作步骤**：

1. 准备三个节点 A、B、C，使 A 连 B、B 连 C，但 A 与 C 不直接配置连接、且彼此不在多播范围（最简单：三者都加 `--no-multicast-scouting`，仅靠 gossip 传播）。

   ```bash
   # A：连 B
   RUST_LOG=debug cargo run --example z_sub -- --no-multicast-scouting --connect tcp/127.0.0.1:7447
   # B：监听 7447，连 C
   RUST_LOG=debug cargo run --example z_pub -- --no-multicast-scouting --listen tcp/127.0.0.1:7447 --connect tcp/127.0.0.1:7448
   # C：监听 7448
   RUST_LOG=debug cargo run --example z_get -- --no-multicast-scouting --listen tcp/127.0.0.1:7448
   ```

   > 说明：gossip 默认 `enabled: true`（见 `DEFAULT_CONFIG.json5`），即便关了 multicast scouting，gossip 仍会在已建链上传播邻居信息。

2. 关键对比：把第 1 步的 `--no-multicast-scouting` 去掉重跑一遍，观察 multicast 模式下的日志差异。

**需要观察的现象**：

- gossip 模式（步骤 1）：A、B、C 的日志里**不会**出现 `Send Scout ... to 224.0.0.224:7446`，但应出现 gossip 相关的协议交互，且 A 最终能通过 B 的通告发现并连上 C（`Successfully connected to newly scouted peer`）。
- multicast 模式（步骤 2）：能看到 4.2 描述的 `Send Scout`/`Send Hello` 多播往返。

**预期结果**：gossip 让 A—C 在没有直接配置、没有多播的情况下也能互联，代价是发现依赖中间节点 B 的转发、且只到下一跳（除非开 `scouting/gossip/multihop`）。

> 三节点能否如预期互联、具体日志行属运行时行为，**待本地验证**。如果你在单机试验，「不在多播范围」的假设并不成立（回环仍可多播），所以严格对照需在多机或禁用多播的环境下进行。

#### 4.3.5 小练习与答案

**练习 1**：两个 Peer 都用默认 `autoconnect_strategy`（`to_peer: "always"`），发现对方后会建立几条链路？若都改成 `greater-zid` 呢？

**参考答案**：默认 `always` 策略下，双方都会主动连对方，结果是**两条**冗余链路（A→B 与 B→A）；若都改成 `greater-zid`，则只有 zid 较大的一方主动，建立**一条**链路，另一方因 `self.zid > to` 为假而不连。这就是策略存在的意义。

**练习 2**：`connect` 里为什么要先 `insert_pending_connection(zid)`，最后再 `remove`？

**参考答案**：scout 回调和 gossip 可能近乎同时报告同一个节点，若不加保护会并发地对同一 zid 发起多次 `open_transport_*`，浪费资源还可能产生半开连接。`pending_connections` 是一个 `HashSet`，`insert` 返回布尔值表示「是否由我拿下这次建连权」，已在建的后续报告直接跳过（`Already connecting to {}. Ignore.`）。

---

## 5. 综合实践

把三幕串起来：构建一个最小「发现 + 建连 + 重连」观察实验。

**任务**：在同一台机器上跑两个 Peer 节点（一个发布、一个订阅），分别用三种方式让它们互联，对比 `open` 返回前的日志与建连路径：

1. **方式一（显式连接）**：发布端 `--no-multicast-scouting --connect <订阅端 listen>`。预期走第 2 幕 `connect_peers`，日志见 `Try to connect`/`Successfully connected to configured peer`。
2. **方式二（multicast 自动发现）**：两端都不传 `--connect`、不开 `--no-multicast-scouting`。预期走第 3 幕 `start_scout`，日志见 `Send Scout`/`Send Hello`/`Received Hello`，并通过 `autoconnect_all`→`connect_peer` 自动建连。
3. **方式三（gossip 透传）**：引入第三个中间节点，两端分别显式连中间节点并都加 `--no-multicast-scouting`，验证 gossip 能让两端经由中间节点互相发现。

对每种方式，记录：

- `open`（即 `cargo run` 启动）到「两端能收发数据」之间出现的关键日志行；
- 哪一幕（bind / connect / scout）在其中起作用；
- 关掉其中一端再重启，是否自动重连（验证 4.3 的 `closed_session`/`closed_link` 重连路径）。

**延伸思考（不必动手）**：把 `scouting/multicast/autoconnect` 的 `peer` 值改成 `[]`（关掉 multicast autoconnect），方式二还能自动建连吗？（答案：不能，只会发现不连，因为 `autoconnect.is_enabled()` 为 false，`start_scout` 走 `(listen=true, autoconnect=false)` 分支只跑 `responder`。）这正好对应 `start_scout` 的四组合调度逻辑。

## 6. 本讲小结

- `Runtime::start` 是节点「通电」时刻，按 `WhatAmI` 分派，固定走「绑定监听 → 连配置端点 → 启动发现」三幕；Client 只连一条（`single_link`），Peer/Router 连全部。
- 第 2 幕 `connect_peers` 处理配置里写死的端点，真正建连由 `peer_connector`→`manager().open_transport_unicast` 完成，受全局超时与 `connection_retry` 重试策略约束。
- multicast scouting 是一对互逆循环：`scout` 多播提问（1s→8s 指数退避）、`responder` 单播回 `Hello`（含 zid/whatami/locators）；二者由 `start_scout` 按 `(listen, autoconnect)` 四组合调度。
- `AutoConnect` 用「matcher + 策略」决策发现到的节点连不连、该谁连；`Always` 可能冗余双连，`GreaterZid` 保证只一方主动。
- `connect`/`connect_peer` 负责去重（已有传输跳过、已配置 locator 过滤）与防并发（`pending_connections`）；链路断开由 `closed_session`/`closed_link` 触发自动重连。
- gossip scouting 复用同一套 `AutoConnect`/`connect_peer`，但载体是已建链路上的协议消息，能把发现范围扩展到多播之外；`StartConditions`/`PeerConnector` 让 Peer 角色的 `open` 可等待「连上至少一个 scouted 节点」。

## 7. 下一步学习建议

本讲讲清了「连谁、何时连」，但「连接握手本身怎么完成」——`open_transport_unicast` 内部的 Open/Close 握手、认证、序列号协商——属于传输层。建议接下来：

- **《u9-l1 链路层抽象》**与**《u9-l2 传输层 unicast/multicast 管理》**：`TransportManager` 的 builder、`open_transport_unicast`/`open_transport_multicast` 的实现，本讲里反复出现的 `manager()` 在那里展开。
- **《u9-l3 传输建连、认证与内部状态机》**：本讲 `connect` 调到的 `establishment`（Open 消息交换 zid/whatami/lease）与 `authentication`，是建连的最后一公里。
- **《u8-l4 路由协议：链路状态、网络与 gossip》**：本讲只讲了 gossip 的「发现」一面，gossip 还承担 router 间拓扑传播，其协议编解码与 linkstate 在该讲深入。
- **《u6-l1 Scouting（公开 API 侧）》**（若尚未读）：把本讲的网络层实现与用户可见的 `zenoh::scout`/`Hello` API 对应起来。
