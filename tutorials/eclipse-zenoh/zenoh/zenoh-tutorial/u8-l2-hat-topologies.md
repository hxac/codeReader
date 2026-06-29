# HAT：四种拓扑的路由策略

## 1. 本讲目标

本讲承接《u8-l1 路由骨架：Gateway / Face / Tables》。在上一讲里，我们知道了 Gateway 负责把传输「缝合」成 Face、Tables 负责维护资源树与路由表。但有一个关键问题被刻意回避了：**当一条 `Put` 消息进来，Gateway 到底该把它转发给哪些下游 face？**

答案就藏在本讲的主角——**HAT（Hierarchical Adaptive Topology，分层自适应拓扑）** 里。HAT 是 Zenoh 路由层的「策略接口」：同样一个路由骨架，换上不同的 HAT 实现，就会展现出截然不同的转发行为。

学完本讲，你应当能够：

1. 说出 `HatTrait` 是什么、为什么要把路由策略抽象成一个 trait，以及它由哪五个子 trait 组成。
2. 解释 Gateway 是如何根据「region 朝向 × 节点角色」为每个 region 挑选一种 HAT 实现的。
3. **区分 router HAT 与 peer HAT 的根本差异**：router 用全网链路状态图 + 最短路径树做「树形转发」，peer 用「点对点洪泛 + 兴趣剪枝」。
4. 说出 client HAT 与 broker HAT 各自的「最简转发」语义，以及 `TreesComputationWorker` 的作用。

---

## 2. 前置知识

在进入 HAT 之前，请确保你已经理解以下概念（它们都来自前置讲义）：

- **Gateway / Face / Tables**（u8-l1）：Gateway 是路由总装车间，Face 是消息的路由单元（既实现入站 `Primitives`，又持有出站 `EPrimitives`），Tables 维护资源树 `Resource`（一棵 key expression 的 trie）与全局状态。本讲的 HAT 就「挂」在 Tables 上，按 region 存放。
- **Resource 与 face_ctxs**（u8-l1）：每个 `Resource` 有一个 `face_ctxs: IntHashMap<FaceId, Arc<FaceContext>>`，记录「哪些 face 对这个 key 感兴趣」，其中 `FaceContext.subs` 表示该 face 上有订阅者。这是本讲所有转发判断的数据基础。
- **WhatAmI 三种角色**（u2-l3）：Router / Peer / Client。HAT 的四种实现正好对应这三种角色（外加一个 broker）。
- **Primitives / EPrimitives / DeMux / Mux**（u7-l2）：HAT 计算出的「下游 face 集合」最终通过 `EPrimitives::send_push` 等方法真正发送出去。

还有一个新概念需要先建立直觉——**region（区域）与 bound（朝向）**。一个 Zenoh 节点可以同时属于多个 region，每个 region 有一个朝向：

- **North（北向）**：朝向「上层」网络（router 之间、peer 之间，或 client 朝向它的 router）。
- **South（南向）**：朝向「下层」客户端（router/peer 朝向挂在自己下面的 client）。

`Region::Local`（本地 Session 所在的区域）始终是 North 朝向。本讲会看到，「同一个 Client 角色，在 North region 用 client HAT，在 South region 用 broker HAT」——朝向决定了策略。

> 提示：HAT 全部位于 `zenoh/src/net/routing/hat/` 下，模块文档都标注了「⚠️ intended for Zenoh's internal use ⚠️」。也就是说，**HAT 是内部实现细节，不是稳定 API**。我们读它是为了理解原理，写应用时不应直接依赖。

---

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|---|---|
| [zenoh/src/net/routing/hat/mod.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/mod.rs) | 定义 `HatTrait` 复合 trait 及其五个子 trait（`HatBaseTrait` / `HatInterestTrait` / `HatPubSubTrait` / `HatQueriesTrait` / `HatTokenTrait`），是所有 HAT 实现的统一接口。 |
| [zenoh/src/net/routing/gateway.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/gateway.rs) | Gateway 构建时按 `(region.bound(), mode)` 为每个 region 实例化一种 HAT（第 150–176 行的 `match`）。 |
| [zenoh/src/net/routing/hat/router/mod.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/router/mod.rs) | router HAT 实现：维护全网链路状态图 `Network`，后台 `TreesComputationWorker` 周期重算最短路径树。 |
| [zenoh/src/net/routing/hat/router/pubsub.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/router/pubsub.rs) | router HAT 的 Pub/Sub 部分，核心是树形 `compute_data_route`。 |
| [zenoh/src/net/routing/hat/peer/mod.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/peer/mod.rs) | peer HAT 实现：用 `Gossip` 或多跳 `Network` 维护拓扑，但**不算树**，按兴趣洪泛。 |
| [zenoh/src/net/routing/hat/peer/pubsub.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/peer/pubsub.rs) | peer HAT 的 Pub/Sub 部分，核心是「向所有带订阅者的直连 face 洪泛」的 `compute_data_route`。 |
| [zenoh/src/net/routing/hat/client/mod.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/client/mod.rs) | client HAT 实现：北向最简转发，只对着自己唯一的上游。 |
| [zenoh/src/net/routing/hat/broker/mod.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/broker/mod.rs) | broker HAT 实现：南向最简转发，router/peer 为挂在自己下面的 client 做代理。 |

> 阅读建议：先读 `hat/mod.rs` 的 trait 定义（看接口），再看 `gateway.rs` 的选择逻辑（看装配），最后对照 router 与 peer 的 `compute_data_route`（看差异）。

---

## 4. 核心概念与源码讲解

### 4.1 HatTrait：路由策略的统一接口

#### 4.1.1 概念说明

「HAT」这个名字里，**Topology（拓扑）** 是关键词。Zenoh 部署的拓扑形态差别巨大：

- **Router 拓扑**：成百上千台 router 组成的骨干网，需要高效、无重复地转发，否则会形成广播风暴。
- **Peer 拓扑**：少量对等节点组成的 mesh，简单可靠即可，不必维护全网视图。
- **Client 拓扑**：一个 client 只连一台 router/peer，转发逻辑就是「转发给我的上游」。
- **Broker 拓扑**：一台 router 为自己下面的一群 client 做集中代理，转发逻辑就是「在直连的 client 之间转发」。

如果把这些策略硬编码进 Gateway，代码会变成一团 `if mode == router ... else if mode == peer ...` 的面条。Zenoh 的做法是**把「路由策略」抽象成一个 trait**，Gateway 只负责装配，具体怎么转发由 HAT 实现自己决定。这就是经典的**策略模式（Strategy Pattern）**：同一个骨架（Gateway/Face/Tables），换不同的策略（HAT），得到不同的行为。

`HatTrait` 就是这个策略接口。它本身是一个空的标记 trait，真正的契约拆在五个子 trait 里，覆盖了 Zenoh 路由的方方面面。

#### 4.1.2 核心流程

`HatTrait` 的组合关系：

```
HatTrait = HatBaseTrait      // 生命周期：init / new_face / new_resource / close_face / owns …
         + HatInterestTrait  // 声明式兴趣（Interest）的路由
         + HatPubSubTrait    // Pub/Sub：compute_data_route / register_subscriber / propagate_subscriber …
         + HatQueriesTrait   // Query/Reply：compute_query_route / register_queryable …
         + HatTokenTrait     // Liveliness token：register_token …
```

每个子 trait 对应一类消息的处理。其中与本讲最相关的是 `HatPubSubTrait::compute_data_route`——它就是「一条 `Put` 该发给谁」的决策函数。它的契约（见源码）说明：

- **它对来源 face 无感知**：调用方（dispatcher）负责保证不会把消息回发给来源，HAT 只管「算出所有目标 face」。
- **返回的 `Route` 是带缓存语义的**：路由结果依赖「消息属性 + HAT 状态」，一旦 HAT 状态变化（如拓扑变动），缓存的路由就要作废。这正是后面 router HAT 调用 `disable_all_routes` 的原因。

四种 HAT 实现的差异，最集中的体现就是它们各自如何实现 `compute_data_route`。

#### 4.1.3 源码精读

`HatTrait` 是五个子 trait 的聚合，本身只是一个空 trait：

[hat/mod.rs:110-113](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/mod.rs#L110-L113) —— `HatTrait` 把五类路由契约组合成一个接口，任何 HAT 实现都必须同时实现这五类。

其中 `HatPubSubTrait::compute_data_route` 是本讲的「决策核心」：

[hat/mod.rs:521-541](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/mod.rs#L521-L541) —— 这个方法签名（`compute_data_route(&self, tables, src_region, expr, node_id) -> Arc<Route>`）和它的文档注释，明确说明了两件事：(1) 它对来源 face 无感知，去重由 dispatcher 负责；(2) 返回的路由带缓存版本语义，HAT 状态一变就得作废。

`HatBaseTrait` 里还有几个贯穿四种实现的关键方法：`owns`（判断一张 face 是否归本 HAT 管）、`mode`（报告本 HAT 的角色）、`disable_all_routes`（令本 region 的缓存路由全部失效）：

[hat/mod.rs:262-270](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/mod.rs#L262-L270) —— `owns` 用 `region` 相等来判断归属（并带一组 `debug_assert` 校验 face 的朝向/角色一致性）。

[hat/mod.rs:307-312](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/mod.rs#L307-L312) —— `disable_all_routes` 把本 region 的 `routes_version` 加一，并令全局所有资源路由失效；这是「拓扑变了 → 缓存作废」的标准动作。

#### 4.1.4 代码实践

**实践目标**：亲手确认 `HatTrait` 的「五合一」结构，并找出每种 HAT 实现各自声明 `impl HatTrait for Hat` 的位置。

**操作步骤**：

1. 打开 `hat/mod.rs`，定位第 110–113 行，记下 `HatTrait` 聚合了哪五个子 trait。
2. 用编辑器全局搜索 `impl HatTrait for Hat`，分别在 `router/mod.rs`、`peer/mod.rs`、`client/mod.rs`、`broker/mod.rs` 找到四处（例如 [router/mod.rs:586](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/router/mod.rs#L586)、[peer/mod.rs:515](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/peer/mod.rs#L515)、[client/mod.rs:274](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/client/mod.rs#L274)、[broker/mod.rs:281](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/broker/mod.rs#L281)）。注意这四处都是空实现 `impl HatTrait for Hat {}`——真正的逻辑在各自的 `impl HatBaseTrait` / `impl HatPubSubTrait` 等块里。

**需要观察的现象**：四处 `impl HatTrait for Hat {}` 都是空的，说明「策略的真正差异不在 `HatTrait` 这一层，而在它要求的子 trait 方法里」。

**预期结果**：你会得到一张「四种子 crate × 五个子 trait」的对照表雏形——四种 HAT 各自独立实现同一套接口，这就是策略模式的落点。

#### 4.1.5 小练习与答案

**练习 1**：`HatTrait` 为什么设计成「五个子 trait 聚合」的空 trait，而不是把所有方法塞进一个大 trait？

**参考答案**：按关注点分离——Interest / PubSub / Queries / Token 是四类正交的消息处理逻辑，Base 是生命周期。拆开后，每个子 trait 可以独立演进、独立测试，阅读时也能按「我现在关心哪类消息」去定位代码。聚合 trait 则保证类型层面的「要么全实现，要么不实现」。

**练习 2**：`compute_data_route` 的文档说「它对来源 face 无感知」。那么「不把消息发回给来源」这件事由谁负责？

**参考答案**：由 dispatcher（调用方）负责。HAT 只负责算出所有匹配的下游 face，dispatcher 在实际投递时会过滤掉来源 face。这样 HAT 的逻辑可以保持纯粹（只看订阅关系），去重规则集中在 dispatcher 一处。

---

### 4.2 Gateway 如何按 region × 角色实例化 HAT

#### 4.2.1 概念说明

策略模式有两个动作：**定义策略接口**（上一节的 `HatTrait`）和**选择具体策略**（本节）。在 Zenoh 里，选择动作发生在 Gateway 构建时。

Gateway 会被赋予一组 region（至少包含 `Region::Local`）。对每一个 region，它根据两个量挑一种 HAT：

1. **`region.bound()`**：这个 region 是 North 还是 South 朝向。
2. **角色**：`region.mode().unwrap_or(mode)`，即这个 region 的角色（优先取 region 自带的，没有就用节点整体的 `mode`）。

这两个量的组合，正好对应四种 HAT。理解这张「选择表」是理解整个 HAT 体系的钥匙。

#### 4.2.2 核心流程

Gateway 的 HAT 选择表（直接对应源码里的 `match`）：

| `(region.bound(), 角色)` | 选用的 HAT | 含义 |
|---|---|---|
| `(North, Client)` | **client HAT** | 本节点是 client，朝北找它的上游 router/peer |
| `(South, Client)` | **broker HAT** | 本节点是 router/peer，朝南为自己下面的 client 做代理 |
| `(_, Peer)` | **peer HAT** | peer mesh，北向/南向都用同一套点对点策略 |
| `(_, Router)` | **router HAT** | router 骨干网，用链路状态 + 树计算 |

要点：

- **broker 与 client 是「一对镜像」**：都面向 client 角色，区别只在朝向。client HAT 朝北（自己当 client），broker HAT 朝南（伺候自己的 client）。所以 broker HAT 在 `new` 时断言「必须是南向」。
- **peer 与 router 不区分朝向**（`_`）：peer 在两个朝向都用洪泛策略，router 在两个朝向都用树策略。
- 一个 router 节点通常会同时拥有多个 HAT：一个 router HAT（朝北连骨干网）+ 一个 broker HAT（朝南伺候 client）。它们共存于同一张 Tables 里，按 region 索引。

#### 4.2.3 源码精读

Gateway 在 `build` 时为每个 region 装配 HAT：

[gateway.rs:150-176](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/gateway.rs#L150-L176) —— 这段 `regions.iter().map(...)` 遍历所有 region，用 `match (region.bound(), region.mode().unwrap_or(mode))` 装箱出对应的 `Box<dyn HatTrait>`，最终收集成 `RegionMap`。注意 router 分支在 `#[cfg(test)]` 下还会调用 `set_disable_async_tree_computation`，方便测试同步算树。

装配之后，Gateway 还要逐个调用 `hat.init(...)` 完成各 HAT 的初始化（router HAT 在这里创建 `Network`）：

[gateway.rs:207-218](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/gateway.rs#L207-L218) —— `init_hats` 持有 `ctrl_lock`，遍历所有 HAT 调用 `init`，把 runtime 传进去。

每种 HAT 的 `mode()` 方法报告自己的身份，可用于运行期区分：

- [router/mod.rs:514-516](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/router/mod.rs#L514-L516) 返回 `WhatAmI::Router`。
- [peer/mod.rs:414-416](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/peer/mod.rs#L414-L416) 返回 `WhatAmI::Peer`。
- [client/mod.rs:215-217](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/client/mod.rs#L215-L217) 与 [broker/mod.rs:222-224](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/broker/mod.rs#L222-L224) **都返回 `WhatAmI::Client`**——这印证了「broker 与 client 是面向 client 角色的一对镜像」。

#### 4.2.4 代码实践

**实践目标**：验证「同一个节点可以同时持有多个 HAT」。

**操作步骤**：

1. 阅读 [gateway.rs:150-176](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/gateway.rs#L150-L176)，确认 `hats` 是一个 `RegionMap`（每个 region 一个 HAT）。
2. 假设一台 router 节点配置了两个 region：一个朝北（router 骨干）、一个朝南（client）。根据选择表推断：它会同时拥有一个 **router HAT**（`(_, Router)`）和一个 **broker HAT**（`(South, Client)`）。
3. 在脑中（或纸上）画出：`Tables.hats = { north_region → router::Hat, south_region → broker::Hat, local → … }`。

**需要观察的现象**：多个 HAT 共享同一份 `TablesData`（同一棵资源树、同一张 faces 表），但各自维护自己的 per-face / per-resource 状态（`HatFace` / `HatContext`）。

**预期结果**：你能解释「为什么一条消息进来后，dispatcher 要按 region 逐个询问相关 HAT 的 `compute_data_route`，再把结果合并」——因为不同 region 用不同策略，需要分别计算。

#### 4.2.5 小练习与答案

**练习 1**：为什么 broker HAT 和 client HAT 的 `mode()` 都返回 `WhatAmI::Client`，而 Gateway 仍能区分它们？

**参考答案**：Gateway 区分它们靠的是 `region.bound()`（South → broker，North → client），不是 `mode()`。`mode()` 只是 HAT 对自己身份的自述（都面向 client 角色），真正决定装配的是 `(bound, 角色)` 这个组合。

**练习 2**：如果一个 peer 节点同时朝北和朝南都有连接，它会用到几种 HAT？

**参考答案**：只会用到 peer HAT 一种。因为选择表里 peer 是 `(_, WhatAmI::Peer)`——无论朝向，peer 角色都用 peer HAT。peer HAT 内部用同一个 `Gossip`/`Network` 处理两个朝向的连接。

---

### 4.3 router HAT：链路状态拓扑与树计算转发

#### 4.3.1 概念说明

router HAT 是四种实现里最复杂、也最能体现 Zenoh 路由「可扩展性」的一个。它的核心问题是：**在成百上千台 router 组成的骨干网里，怎么保证一条数据既不丢、又不重复地送达？**

如果用 peer 那种「洪泛给所有邻居」的策略，骨干网会立刻被广播风暴淹没。router HAT 的解法分两步：

1. **维护全网链路状态图**：每台 router 通过 linkstate 协议（u8-l4 会详讲）把自己「和谁相连」广播给全网，于是每台 router 都握有一张完整的骨干网拓扑图 `Network`。
2. **为每个源节点计算最短路径树**：以每个 router 为根，在拓扑图上跑一次最短路径（Dijkstra 类）算法，得到一棵生成树。转发时，**沿着树的方向走**——因为树是无环的，每个目的地在树上有且仅有一条路径，天然不重复。

这就把「全网路由」退化成了「查树」：知道目的地 router 是谁，在源 router 的树里查它的「下一跳方向（direction）」，把消息交给那个方向的 face 即可。

数学上，设源 router 为 \(s\)，目的 router 为 \(v\)，最短路径树给出从 \(s\) 到 \(v\) 的第一跳邻居 \(\text{dir}(s,v)\)。转发规则即：

\[
\text{next\_face}(s, v) = \text{face}\big(\text{dir}(s, v)\big)
\]

由于树无环，对任意 \(v\)，\(\text{dir}(s,v)\) 唯一，故不会重复投递。

#### 4.3.2 核心流程

router HAT 的工作流程可以拆成「维护」与「转发」两条线：

**维护线（拓扑变动时）**：

```
linkstate 消息到达 (handle_oam, OAM_LINKSTATE)
  → net.link_states(...) 更新拓扑图，得到 removed_nodes
  → 注销消失节点上的 subscribers/queryables/tokens
  → compute_trees()：调度 TreesComputationWorker
        │（异步，每 TREES_COMPUTATION_DELAY_MS=100ms 触发一次）
        ▼
  → do_compute_trees：
        net.compute_trees()          // 重算所有最短路径树
        pubsub_tree_change(...)      // 把订阅传播给树的新子节点
        queries_tree_change(...)
        token_tree_change(...)
        disable_all_routes(...)      // 缓存路由全部失效（routes_version++）
```

**转发线（数据到达时，compute_data_route）**：

```
对每个匹配资源 mres：
    取出 res_hat(mres).router_subs  // 哪些 router 上有订阅者（一组 zid）
    router_source = 源 router 在图中的索引
    对每个 sub ∈ router_subs：
        direction = net.trees[router_source].directions[sub_idx]  // 查树：下一跳节点
        face = face_of(net.graph[direction].zid)                  // 下一跳对应的 face
        route.insert(face)                                        // 加入转发目标
```

关键点：**router_subs 按 zid（哪台 router）记录，而不是按 face**。转发时通过查树把「目的 router」翻译成「下一跳 face」，所以哪怕目的 router 隔了好几跳，也只发给直接的下一跳邻居。

#### 4.3.3 源码精读

router HAT 的 `Hat` 结构体持有全网拓扑与三套「按 router zid 索引」的实体集合：

[router/mod.rs:100-110](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/router/mod.rs#L100-L110) —— `routers_net: Option<Network>` 是全网链路状态图；`router_subs` / `router_qabls` / `router_tokens` 分别记录「哪些 router 声明了订阅/可查询/token」；`routers_trees_worker` 是后台树计算器。

`TreesComputationWorker` 是一个后台任务，周期性重算树：

[router/mod.rs:69-98](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/router/mod.rs#L69-L98) —— 它在 `ZRuntime::Net` 上 spawn 一个循环：每隔 `TREES_COMPUTATION_DELAY_MS`（默认 100ms，见 [hat/mod.rs:64-66](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/mod.rs#L64-L66)）从 channel 取一次 `tables_ref`，然后持有写锁调用 `do_compute_trees` 重算。这是一个典型的「**把昂贵计算与数据快路径解耦**」的设计：拓扑变化只标记「需要重算」，真正的全图最短路径计算被延迟、合并到后台定时器里做，避免阻塞每次 `compute_data_route`。

`do_compute_trees` 是重算的入口：

[router/mod.rs:199-207](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/router/mod.rs#L199-L207) —— 调 `net.compute_trees()` 重算所有树，再分别处理 pubsub/queries/token 的树变更，最后 `disable_all_routes` 让缓存失效。

`init` 时创建 `Network`（含 gossip/autoconnect 配置）：

[router/mod.rs:237-275](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/router/mod.rs#L237-L275) —— 读 gossip 配置与链路权重，`Network::new(...)` 建图；注意第 245–247 行禁止把 client 当作 gossip 目标。

**转发核心**——`compute_data_route` 用预计算好的树查下一跳：

[router/pubsub.rs:316-397](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/router/pubsub.rs#L316-L397) —— 内部辅助函数 `insert_faces_for_subs`（第 324–362 行）是精髓：对每个订阅者 router `sub`，查 `net.trees[source].directions[sub_idx]` 得到下一跳节点 `direction`，再找到该节点的 face 插入路由。第 376–394 行的主循环遍历所有匹配资源，调用这个辅助函数。**因为查的是树，每个目的 router 只会产生一个下一跳 face，绝不重复。**

订阅声明时，router HAT 也只把订阅传播给「声明者在树中的子节点」，而不是广播：

[router/pubsub.rs:129-164](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/router/pubsub.rs#L129-L164) —— `propagate_sourced_subscriber` 找到声明者 router 的树索引，只向 `net.trees[tree_sid].children`（树上的子节点）发送 `DeclareSubscriber`。这是树形策略在「控制面」的对应物。

#### 4.3.4 代码实践

**实践目标**：追踪 router HAT「拓扑变动 → 重算树 → 缓存失效」的完整链路，理解 `TreesComputationWorker` 的去耦作用。

**操作步骤**：

1. 从 [router/mod.rs:320-450](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/router/mod.rs#L320-L450)（`handle_oam` 处理 `OAM_LINKSTATE`）出发：注意它先 `net.link_states(...)` 更新拓扑、注销消失节点上的实体，最后在第 441–445 行调用 `compute_trees(ctx)`。
2. 跟到 [router/mod.rs:185-197](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/router/mod.rs#L185-L197) 的 `compute_trees`：非测试构建下它调用 `compute_trees_async`（第 178–183 行），即「往 worker 的 channel 里塞一个 `tables_ref` 就返回」——**不立即重算**。
3. 再看 worker 循环 [router/mod.rs:77-95](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/router/mod.rs#L77-L95)：休眠 100ms 后才真正 `do_compute_trees`。

**需要观察的现象**：拓扑变动后，路由并不会立刻按新树走——在 worker 重算完成、`disable_all_routes` 之前的这段时间，`compute_data_route` 仍可能读到旧树或失效前的缓存。这是一个**最终一致**的设计：用 100ms 的延迟换取数据快路径不被全图 Dijkstra 阻塞。

**预期结果**：你能用一句话说清 `TreesComputationWorker` 的作用——**把昂贵的全网最短路径树计算异步化、批量化、定时化，与高频的数据转发快路径解耦**；拓扑变化只触发「标记 + 调度」，真正的重算在后台定时器里完成。

#### 4.3.5 小练习与答案

**练习 1**：为什么 router HAT 的 `router_subs` 按「router 的 zid」记录，而不是像 peer HAT 那样按「face」记录？

**参考答案**：因为 router 转发要跨多跳——目的订阅者可能隔了好几台 router。按 zid 记录「哪台 router 上有订阅者」，转发时再用最短路径树把 zid 翻译成「下一跳 face」，才能做到跨多跳的无重复投递。如果按 face 记录，就只能表达「直连邻居」，无法支撑骨干网。

**练习 2**：把 `TREES_COMPUTATION_DELAY_MS` 从 100ms 调大到 1000ms，会对系统产生什么影响？

**参考答案**：拓扑变化后路由收敛变慢（最多多等近 1 秒才按新拓扑转发，期间可能短暂走旧路径或丢投递），但数据快路径被阻塞的概率更低、CPU 在频繁拓扑抖动时更省（多次拓扑变化会被合并成一次重算）。这是「收敛速度 vs 转发稳定性/CPU」的权衡。

---

### 4.4 peer / client / broker HAT：点对点洪泛与最简转发

#### 4.4.1 概念说明

剩下三种 HAT 都**不维护全网视图、也不算树**，转发逻辑都建立在「直连的 face」上，但策略各有不同：

- **peer HAT**：维护一个轻量的 `Gossip`（单跳发现）或 `Network`（多跳 gossip）拓扑，但**只用它做去重判断，不用它算转发树**。转发时，对**所有**「带订阅者的直连 face」洪泛，靠兴趣（Interest）剪枝避免无谓发送。这是「点对点（point-to-point）」mesh 的典型做法：简单、无需全局视图，但每条消息可能被多个邻居转发，靠下游的兴趣剪枝与去重来收敛。

- **client HAT**：一个 client 只连一个上游（router/peer），所以它的转发就是「交给那个唯一的上游」或「交给本地带订阅者的 face」。代码里直接 `debug_assert` 断言「owned_faces 数量 == 1」。

- **broker HAT**：一台 router 为挂在自己下面的 client 做代理。它的转发就是「在直连的 client face 之间转发」——哪个 client 订阅了，就发给哪个 client face。这是「最简的 broker」语义。

这三者的共同点是：**路由决策只看 `Resource.face_ctxs`（哪些 face 对这个 key 感兴趣），不查任何全局树**。它们之间的差别，只是「面向哪些 face、要不要跨 region」的细节。

#### 4.4.2 核心流程

**peer HAT 的 `compute_data_route`（点对点洪泛 + 兴趣剪枝）**：

```
对每个匹配资源 mres：
    对每个 owned_face_context ctx（本 HAT 直管的 face）：
        if ctx.subs.is_some() && src_region != self.region():
            route.insert(ctx.face)        // 洪泛给所有带订阅者的直连 face
// 额外：若本 region 朝北、来源朝南，还要考虑
//   - 未 finalized 的订阅兴趣 face
//   - 未 finalized 的 initial interest face
//   - 多播组
```

**client HAT 的 `compute_data_route`（最简：单一上游 + 本地）**：

```
对每个匹配资源 mres：
    对每个 owned_face_context ctx：
        if ctx.subs.is_some() && src_region != self.region():
            route.insert(ctx.face)
if src_region 朝南：                      // 数据来自南（本地 client 之间）
    把唯一的北向 face 也加入（若其订阅兴趣未 finalized）
```

**broker HAT 的 `compute_data_route`（最简：直连 client 之间）**：

```
对每个匹配资源 mres：
    对每个 owned_face_context ctx：
        if ctx.subs.is_some():            // 注意：没有 src_region 检查
            route.insert(ctx.face)
```

#### 4.4.3 源码精读

**peer HAT** 的 `Hat` 是一个枚举，按 gossip 配置区分三种形态：

[peer/mod.rs:77-94](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/peer/mod.rs#L77-L94) —— `Uninit`（未初始化）/ `Gossip`（北向、单跳或不开 gossip）/ `Network`（北向多跳 gossip 或南向 gateway）。注释指出 `Gossip` 与 `Network` 底层都用同一套 linkstate 协议，只是 `Gossip` 不把信息反映到自己的节点结构里。

peer HAT 的转发——向所有带订阅者的直连 face 洪泛：

[peer/pubsub.rs:251-267](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/peer/pubsub.rs#L251-L267) —— `compute_data_route` 的核心循环：遍历 `owned_face_contexts`，凡是 `ctx.subs.is_some()` 且 `self.region() != *src_region` 的 face，都加入路由。**没有查任何树——只要直连邻居订阅了，就发**。后面第 269–316 行还处理「北向 region 收到南向来源」时的兴趣剪枝和多播组（标注为 `HACK(regions)`）。

**client HAT** 的 `Hat` 极简，只有一个 `region` 字段：

[client/mod.rs:61-77](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/client/mod.rs#L61-L77) —— `new` 时 `debug_assert!(region.bound().is_north())`，确认 client HAT 只用于北向。`init` 是空操作（第 121–123 行），因为它没有拓扑要维护。

client HAT 的关键断言——「只连一个上游」：

[client/mod.rs:145-163](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/client/mod.rs#L145-L163) —— `new_transport_unicast_face` 里第 155 行 `debug_assert_eq!(self.owned_faces(ctx.tables).count(), 1)`，硬性要求本 HAT 只管一张 face（唯一的上游）。

client HAT 的转发：

[client/pubsub.rs:146-188](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/client/pubsub.rs#L146-L188) —— 第 149–162 行同样向带订阅者的 face 转发；第 164–185 行处理「来源朝南」时把唯一北向 face 也算上（前提是其订阅兴趣未 finalized）。

**broker HAT** 的 `Hat` 同样极简：

[broker/mod.rs:61-76](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/broker/mod.rs#L61-L76) —— `new` 时 `debug_assert!(region.bound().is_south())`，确认 broker HAT 只用于南向（伺候 client）。

broker HAT 的建连几乎什么都不做——它不在 client 之间传播实体：

[broker/mod.rs:155-172](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/broker/mod.rs#L155-L172) —— `new_transport_unicast_face` 直接 `disable_all_routes`。注释（第 165–168 行）说得很直白：**「broker HAT 永远不是北向 HAT，因此没有兴趣要重新传播；broker HAT 不在 client 之间重新传播实体。」**

broker HAT 的转发：

[broker/pubsub.rs:242-280](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/broker/pubsub.rs#L242-L280) —— 第 264–276 行：对每个带订阅者的直连 face 都加入路由。**注意它没有 `src_region != self.region()` 的检查**（对比 peer/client）——因为 broker 面向的都是南向 client face，不存在「回发给来源 region」的跨 region 问题，dispatcher 会负责过滤来源 face。

#### 4.4.4 代码实践

**实践目标**：通过对比 `compute_data_route` 的「过滤条件」，体会三种 HAT 转发语义的差异。

**操作步骤**：

1. 打开三个文件并排对比它们的 `compute_data_route` 主体循环：
   - peer：[peer/pubsub.rs:251-267](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/peer/pubsub.rs#L251-L267)
   - client：[client/pubsub.rs:149-162](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/client/pubsub.rs#L149-L162)
   - broker：[broker/pubsub.rs:264-276](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/broker/pubsub.rs#L264-L276)
2. 观察三者进入 `route.insert` 的条件有何不同：peer 和 client 都有 `src_region != self.region()`（避免跨 region 回流），broker 没有；peer 还额外处理多播组和未 finalized 的兴趣，client 额外处理单一北向 face，broker 最「干净」。

**需要观察的现象**：三种 HAT 的转发循环**都不查任何全局树**，只看 `owned_face_contexts` 里 `ctx.subs.is_some()`。它们与 router HAT 的本质区别就在于此——router 跨多跳查树，其余三者只在直连 face 上判断。

**预期结果**：你能填出这张表：

| HAT | 转发依据 | 是否查树 | 典型规模 |
|---|---|---|---|
| router | 最短路径树 `trees[src].directions[sub]` | 是 | 大型骨干网 |
| peer | 所有带订阅者的直连 face（洪泛 + 兴趣剪枝） | 否 | 中小 mesh |
| client | 唯一上游 face + 本地带订阅者 face | 否 | 单 client |
| broker | 所有带订阅者的直连 client face | 否 | 单 router 的 client 群 |

#### 4.4.5 小练习与答案

**练习 1**：broker HAT 的 `compute_data_route` 为什么不需要 `src_region != self.region()` 检查，而 peer 和 client 需要？

**参考答案**：broker HAT 只用于南向 region，它管的 face 全是挂在同一台 router 下的 client，不存在「跨 region 回流」的语义；来源 face 的过滤由 dispatcher 统一负责。peer/client 则可能同时存在北向与南向连接，需要用 `src_region` 判断避免把消息错误地回流到来源 region。

**练习 2**：peer HAT 用 `Gossip`/`Network` 维护了拓扑，却没有用它来算转发树。那这个拓扑在 peer HAT 里起什么作用？

**参考答案**：主要用于**节点发现与去重**——比如判断一个 face 是北向还是南向、在多跳 gossip 下识别远端节点、为 `gateways`/`route_successor` 等接口提供信息。但数据转发本身只依赖直连 face 的订阅状态（洪泛 + 兴趣剪枝），不需要全网树。这就是 peer 与 router 在「拓扑用途」上的根本分野。

---

## 5. 综合实践

**任务**：对比 router HAT 与 peer HAT 在「发布一条消息」时的转发路径差异，并说明 `TreesComputationWorker` 的作用。这是本讲规格指定的核心实践。

**步骤 1：建立场景**

设想两种部署：

- **场景 A（router 骨干网）**：3 台 router `R1—R2—R3` 串联，订阅者挂在 `R3` 上，发布者挂在 `R1` 上。
- **场景 B（peer mesh）**：3 台 peer `P1—P2—P3` 同样串联，订阅者挂在 `P3` 上，发布者挂在 `P1` 上。

**步骤 2：分别追踪转发路径（源码阅读型）**

- **router HAT 路径**：从 [router/pubsub.rs:316-397](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/router/pubsub.rs#L316-L397) 出发。`R1` 收到发布后调 `compute_data_route`，源是 `R1`。对订阅者 router `R3`，查 `net.trees[R1].directions[R3]`——树给出的下一跳是 `R2`。于是 `R1` 只把消息发给 `R2` 一个 face；`R2` 同理查自己的树，下一跳是 `R3`，转发给 `R3`。**整条链路每跳只发给一个下游 face，无重复、无洪泛。** 关键代码是 `insert_faces_for_subs` 里 `net.trees[source as usize].directions[sub_idx.index()]` 这一次查表。

- **peer HAT 路径**：从 [peer/pubsub.rs:251-267](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/peer/pubsub.rs#L251-L267) 出发。`P1` 收到发布后，遍历自己直连的 face——只要 `P2` 这个 face 上有相关订阅/兴趣（或处于未 finalized 的兴趣态），就把消息发给 `P2`；`P2` 再发给 `P3`。peer **不查树**，靠每个节点独立看自己的直连邻居与兴趣来逐跳推进。

**步骤 3：写出对比说明（中文，建议 200–400 字）**

请按下述结构自己写一段：

1. **router HAT 如何选下游 face**：以源 router 为根查最短路径树，把「目的订阅者 router」翻译成「唯一下一跳 face」；跨多跳也只发一份数据给直接邻居。适合大规模骨干网，靠树的无环性天然去重。
2. **peer HAT 如何选下游 face**：不维护转发树，对所有「带订阅者（或未 finalized 兴趣）的直连 face」洪泛；每个 peer 独立决策，靠下游兴趣剪枝收敛。适合中小 mesh，简单但流量随邻居数增长。
3. **`TreesComputationWorker` 的作用**：把昂贵的全网最短路径树计算异步化——拓扑变动（linkstate）只触发「标记 + 调度」，真正的重算在 `ZRuntime::Net` 的后台任务里每 100ms 批量执行一次，随后 `disable_all_routes` 让缓存路由失效。这样数据快路径（`compute_data_route`）只需读预计算好的 `trees`，绝不会被全图 Dijkstra 阻塞。

**步骤 4（可选，运行验证）**：若本地已编译 zenohd，可起 3 个 router 互联（`mode=router`，互相 `connect`），开 `RUST_LOG=zenoh::net::routing=trace`，观察 `Compute trees` / `Schedule trees computation` 日志（[router/mod.rs:179](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/router/mod.rs#L179)、[router/mod.rs:200](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/router/mod.rs#L200)），验证树计算的延迟与批处理特性。具体命令与输出**待本地验证**。

**验收标准**：你能不看源码，用自己的话讲清楚「为什么 1000 台 router 不会广播风暴（树），而 10 台 peer 的 mesh 可以容忍洪泛（兴趣剪枝）」。

---

## 6. 本讲小结

- **`HatTrait` 是路由策略接口**：它是 `HatBaseTrait + HatInterestTrait + HatPubSubTrait + HatQueriesTrait + HatTokenTrait` 五合一的空 trait，把「怎么转发」从 Gateway 骨架中解耦出来，是策略模式的落点。
- **Gateway 按 `(region.bound(), 角色)` 装 HAT**：North+Client → client HAT，South+Client → broker HAT，Peer → peer HAT，Router → router HAT。一个节点可同时持有多个 HAT（如 router 同时有 router HAT 和 broker HAT）。
- **router HAT = 全网链路状态图 + 最短路径树**：订阅按 router zid 记录，转发时查 `trees[src].directions[sub]` 得到唯一下一跳 face，靠树的无环性跨多跳无重复投递，是骨干网可扩展的关键。
- **peer HAT = 点对点洪泛 + 兴趣剪枝**：不查树，对所有带订阅者/未 finalized 兴趣的直连 face 洪泛，每个节点独立决策，适合中小 mesh。
- **client HAT 与 broker HAT 是面向 client 的一对最简镜像**：client 朝北转发给唯一上游，broker 朝南在直连 client face 之间转发；二者都不维护拓扑、都不算树。
- **`TreesComputationWorker` 把树计算异步化**：拓扑变动只触发「标记 + 调度」，全图重算在后台每 100ms 批量执行，避免阻塞数据快路径；代价是路由收敛有约 100ms 的最终一致延迟。

---

## 7. 下一步学习建议

本讲把 HAT 的「策略差异」讲透了，但有几个相邻主题值得继续深入：

1. **dispatcher 子模块（u8-l3）**：本讲反复提到「dispatcher 调用 `compute_data_route` 再合并结果」「dispatcher 负责过滤来源 face」。下一讲会打开 `dispatcher/resource.rs`、`pubsub.rs`、`queries.rs`、`interests.rs`，看清 `compute_data_route` 的结果是如何被消费、合并、投递的，以及兴趣（Interest）如何驱动按需路由。
2. **路由协议：链路状态与 gossip（u8-l4）**：router HAT 依赖的 `Network`、`link_states`、`OAM_LINKSTATE` 消息、gossip 发现都来自 `zenoh/src/net/protocol/`。那里会讲清「全网拓扑图是怎么通过协议消息一点点建立起来的」。
3. **继续精读 router HAT 的 queries/token 子模块**：本讲只看了 `pubsub.rs`，`router/queries.rs`（`compute_query_route`）和 `router/token.rs`（liveliness）的树形传播逻辑与本讲同构，可作为自练。
4. **对照 Zenoh080Routing codec**：`handle_oam` 里解码 `LinkStateList` 用的就是 `Zenoh080Routing`（[router/mod.rs:336-340](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/router/mod.rs#L336-L340)），可在 u10-l2 线编码讲义里看到它的字节级细节。
