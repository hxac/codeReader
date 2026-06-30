# Dispatcher：资源、pubsub、查询与 interest

## 1. 本讲目标

本讲深入 Zenoh 路由层的「管道层」——`dispatcher/` 子模块。上一讲《u8-l1 路由骨架》我们立起了 Gateway / Face / Tables 三大件，并指出 Gateway 把「转发给谁」这件事委托给了 HAT（路由策略）。但 HAT 要做决策，必须先有人替它：

- 把 key expression 组织成一棵可匹配的树；
- 记录「哪个 face 在哪个 key 上声明了 subscriber / queryable / token」；
- 在一条数据/查询真正到来时，把对应的「下游 face 列表」算出来并实际发送；
- 还要处理一种特殊的「声明式订阅」——interest，让网络只在有人关心时才传播声明。

这正是 `dispatcher/` 的职责。读完本讲，你应该能够：

1. 说出 `Resource` 这棵前缀树如何承载 key expression，以及 `get_matches` 如何完成相交匹配；
2. 跟踪一条 `Push`（pub/sub 数据）从 `route_data` 到对端 face 的转发路径，并指出 `compute_data_route` 的决策依据；
3. 跟踪一条 `Request`（查询）从 `route_query` 到 queryable 的转发路径，理解 `QueryTarget`（All / AllComplete / BestMatching）三种目标的差异；
4. 说清 `interest` 如何被 `interests.rs` 记录，以及它如何驱动「只把声明传播给关心的 face」的剪枝逻辑；
5. 理解 liveliness token 在 dispatcher 层的声明与跨 region 传播。

> ⚠️ 本讲属于内核（`net/routing`），全部代码都带 `pub(crate)` 且被标为 internal，**写应用时不应直接依赖**。我们读它是为了理解 Zenoh 内部「一条消息如何被路由」。

## 2. 前置知识

本讲假设你已经学完《u8-l1 路由骨架：Gateway / Face / Tables》，并熟悉以下概念（在前置讲义中已建立）：

- **Gateway / Face / Tables**：Gateway 是路由总装；Face 是路由单元（实现入站 `Primitives`、持有出站 `EPrimitives`）；Tables 是共享状态，用 `TablesLock`（`tables`/`ctrl_lock`/`queries_lock` 三把锁）管控并发。
- **HAT（Hierarchical Adaptive Topology）**：把「转发给谁」抽象为 `HatTrait`，按 `(region, 角色)` 装 router / peer / broker / client 之一。HAT 的核心方法是 `compute_data_route` / `compute_query_route`。
- **Resource 树**：key expression 的 trie，其 `face_ctxs` 记录「谁对该 key 感兴趣」。
- **Primitives / EPrimitives / Face 双向使用**：DeMux（入站）→ Face（`Primitives`）→ HAT；HAT → Face（`EPrimitives`，即 Mux）→ Transport。
- **Region（North / South）**：一张 face 朝向「上游（north）」或「下游（south）」，region 决定路由方向与 HAT 选择。

如果上面任何一项让你陌生，请先回到《u8-l1》复习。本讲会用到的、来自协议层（`zenoh-protocol`）的消息类型先列一张速查表：

| 协议消息 | 含义 | 在本讲出现于 |
| --- | --- | --- |
| `Push` | pub/sub 数据帧（Put/Delete） | `route_data` |
| `Request` / `Response` / `ResponseFinal` | 查询 / 应答 / 应答结束 | `route_query` / `route_send_response*` |
| `Declare` + `DeclareBody` | 声明实体（Subscriber/Queryable/Token/KeyExpr/Interest/Final） | 所有 `declare_*` |
| `Interest` | 声明式兴趣（Current/Future/Final） | `Face::interest` |

## 3. 本讲源码地图

本讲聚焦 `zenoh/src/net/routing/dispatcher/` 目录，并少量引用其调用方 HAT（因为 dispatcher 把决策委托给 HAT，必须一起看才能讲清「转发给谁」）。

| 文件 | 作用 |
| --- | --- |
| [dispatcher/resource.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/resource.rs) | `Resource` 前缀树、`FaceContext`、key 匹配引擎 `get_matches`、keyexpr 注册与短 id 优化 |
| [dispatcher/pubsub.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/pubsub.rs) | 声明/反声明 subscriber，`route_data` 把 `Push` 转发给下游 face |
| [dispatcher/queries.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/queries.rs) | 声明/反声明 queryable，`route_query` 把查询转发并按 `QueryTarget` 选目标、超时清理 |
| [dispatcher/interests.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/interests.rs) | 处理 `Interest`：记录远端兴趣（Future）、发送当前快照（Current）、清理 |
| [dispatcher/token.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/token.rs) | liveliness token 的声明/反声明与跨 region 传播 |
| [dispatcher/face.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/face.rs) | `FaceState`（face 的真实状态）、`with_mapped_expr`（把 WireExpr 解析成 Resource） |
| hat/peer/pubsub.rs / hat/peer/interests.rs / hat/peer/mod.rs | peer HAT 的具体路由策略，是 dispatcher 委托的对象，用来讲清剪枝 |

记住一条贯穿全讲的规律：**dispatcher 负责登记 + 失效 + 委托 + 实际发送，HAT 负责「算路由」**。dispatcher 文件里的 `declare_*` / `route_*` 几乎都是「先把消息里的 WireExpr 解析成 `Resource`，再调用 `tables.hats[region]` 上的方法，最后由 HAT 决定要不要 `send_declare` / `send_push`」。

## 4. 核心概念与源码讲解

### 4.1 Resource：key 表达式前缀树与匹配

#### 4.1.1 概念说明

`Resource` 是 Zenoh 路由层的「地址空间数据结构」。它把所有出现过的 key expression（如 `demo/example/a`、`demo/*`、`robot/**`）组织成一棵**前缀树（trie）**：

- 树根是空字符串 `""`（`Resource::root`）；
- 每条边是一个以 `/` 起始的 chunk（如 `/demo`、`/example`、`/a`）；
- 每个节点保存自己从根到这里的完整表达式 `expr`（如 `demo/example/a`），以及一个 `suffix` 偏移，用来 O(1) 取出自己的最后一段。

为什么用前缀树？因为 Zenoh 的匹配是 key expression 的**集合相交**（`*` 单层、`**` 多层），前缀树能让「找所有与某 key 相交的已声明资源」这件事高效。这就是路由的核心查询：一条数据来了，我要知道哪些 face 在「相交的 key」上有 subscriber。

`Resource` 还承载每个 face 在该 key 上的**上下文** `FaceContext`——这正是「谁对这个 key 感兴趣」的落点。dispatcher 与 HAT 的所有路由判断，最终都在读 `FaceContext` 里的几个布尔/Option 位。

#### 4.1.2 核心流程

一棵 `Resource` 树的关键操作：

1. **建/找节点**：`make_resource(from, suffix)` 从某父节点开始，按 `/` 切 chunk 逐级下钻，缺失则创建；`get_resource` 只查找不创建。
2. **升级为「有上下文」**：`upgrade_resource` 给节点装上 `ResourceContext`（只有「被声明过的实体」对应的资源才有 context，根节点没有）。
3. **匹配**：`get_matches(tables, key_expr)` 从根做一次类 BFS 遍历，利用 chunk 级别的 `intersects`（集合相交）和 `**` 通配，收集所有与该 key 相交、且**有 context** 的资源，返回 `Vec<Weak<Resource>>`。
4. **登记互配关系**：`match_resource` 把双向「我匹配谁」的弱引用写进 `ResourceContext.matches`，加速后续 `res.matches(other)` 判断。
5. **per-face 上下文**：`face_ctxs: IntHashMap<FaceId, Arc<FaceContext>>` 记录「每个 face 在我这个 key 上是什么状态」。

匹配的直觉（详见 `get_matches`）：

\[
\text{route}(key) = \{\, res \in \text{tree} \;\big|\; res.ctx \text{ 存在} \;\land\; keyexpr(res) \cap key \neq \emptyset \,\}
\]

其中「`res.ctx` 存在」是关键过滤——只返回真正被某实体声明过的资源，纯前缀中间节点不会出现在匹配结果里。

#### 4.1.3 源码精读

**Resource 结构体**——注意它同时持有树结构字段和「有声明才有的」上下文：

[resource.rs:373-381](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/resource.rs#L373-L381)：`Resource` 主体。`parent`/`children` 构成树；`ctx: Option<Box<ResourceContext>>` 标记「是否被声明过」；`face_ctxs` 记录 per-face 状态。`PartialEq`/`Hash` 都按 `expr()` 比较，所以「字符串相等即同一资源」。

**FaceContext**——per-face 的路由位，是后续路由判断的真正依据：

[resource.rs:185-213](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/resource.rs#L185-L213)：`FaceContext` 含 `subs: Option<SubscriberInfo>`（该 face 在此 key 上是否有订阅）、`qabl: Option<QueryableInfoType>`（是否有可查询端）、`token: bool`（是否有存活令牌）、`subscriber_interest_finalized` / `queryable_interest_finalized`（interest 握手是否完成）。这几个字段就是 `compute_data_route` / `compute_query_route` 的判据。

**匹配引擎**——路由的核心查询：

[resource.rs:810-894](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/resource.rs#L810-L894)：`get_matches` 用一个 `VecDeque` 做迭代式遍历（注释明确「不用递归，因为树可能有任意深度」）。对每个节点，它把 key 按第一个 `/` 切成 `(chunk, rest)`，与节点 suffix 做 `intersects` 判断；只有 `ctx.is_some()` 的节点才会被 `push` 进结果。末尾按指针 `sort + dedup` 去重。

**登记互配关系**：

[resource.rs:896-913](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/resource.rs#L896-L913)：`match_resource` 在节点有 context 时，把双向弱引用写入 `ctx.matches`。之后 `Resource::matches`（[resource.rs:494-504](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/resource.rs#L494-L504)）只需查这张预计算表，而不必每次重算相交。

> 设计要点：`matches` 用 `Weak<Resource>`，且 `clean`（[resource.rs:535-562](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/resource.rs#L535-L562)）会在引用计数降到阈值且无子节点时把资源从树里摘除并清理互配表。这是「声明消失 → 树自动瘦身」的回收机制。

#### 4.1.4 代码实践

**实践目标**：理解 `get_matches` 的匹配语义，亲手验证「相交即匹配」。

**操作步骤**（源码阅读型实践）：

1. 打开 [resource.rs:810-894](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/resource.rs#L810-L894)，对照阅读 `get_matches_from` 内部的两个分支 `None`（key 已走到最后一段）与 `Some(rest)`（key 还有后续 chunk）。
2. 假设树里已声明了三个有 context 的资源：`a/b`、`a/*`、`a/**`。回答：用 `get_matches(tables, "a/b")` 会返回哪几个？用 `get_matches(tables, "a/c")` 又会返回哪几个？
3. 阅读 `ke_chunk_intersects_suffix`、`ke_chunk_is_wild`、`suffix_is_wild` 三个局部布尔，说明 `**` 是如何让遍历「跨层下钻」的。

**预期结果**：

- `get_matches("a/b")`：`a/b`、`a/*`、`a/**` 三者都与 `a/b` 相交，应全部返回。
- `get_matches("a/c")`：`a/*`（单层通配含 `c`）、`a/**`（多层通配）返回；`a/b` 不与 `a/c` 相交，不返回。

**待本地验证**：上述结论基于对 `intersects` 语义的推理；若想实测，可参考 `local_resources.rs` 末尾的 `#[cfg(test)] mod tests`（[local_resources.rs:335-452](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/local_resources.rs#L335-L452)）的写法，构造 `Resource::root()` 后调用 `make_resource` + `match_resource`，再断言 `get_matches` 结果。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Resource` 的 `PartialEq`/`Hash` 只用 `expr()`，而不用指针？

> **答案**：因为同一 key expression 在全局只应存在一个 `Resource` 节点（由 `get_resource`/`make_resource` 保证），「字符串相等即同一资源」让 `Resource` 可以放进 `HashSet`/作为 `HashMap` 键去重，而不受 `Arc` 克隆产生的不同指针影响。

**练习 2**：`Resource::clean` 为什么要检查 `Arc::strong_count(res) <= 3`？

> **答案**：注释（[resource.rs:540-541](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/resource.rs#L540-L541)）解释：`+1` 是本次调用临时克隆的 `resclone`，`+1` 是父节点的 `children` 引用，所以「只有当外部仅剩一个持有者且无子节点」时（共 3）才安全回收，避免误删仍被别处引用的资源。

---

### 4.2 Pub/Sub 路由：declare_subscriber 与 route_data

#### 4.2.1 概念说明

`pubsub.rs` 处理 pub/sub 链路在 dispatcher 层的两件事：

1. **声明管理**：`declare_subscriber` / `undeclare_subscriber`——当某 face 声明（或撤销）一个 subscriber 时，登记进 HAT、让缓存路由失效、并把声明跨 region 传播出去。
2. **数据转发**：`route_data`——当一条 `Push`（Put/Delete 数据）到达时，算出该转发给哪些下游 face，并实际发送。

它的核心抽象是 **`Route = Vec<Direction>`**（[resource.rs:82](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/resource.rs#L82)），其中每个 `Direction` 指明「目标 face + 在该 face 上使用的 wire_expr + node_id」。`RouteBuilder`（[resource.rs:119-155](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/resource.rs#L119-L155)）负责按 face id 去重地构建这条路由，保证同一 face 只出现一次。

> 与公开 API 的关系：你在应用层 `declare_subscriber` 时（见《u3-l1》），最终在路由层就是调用这里的 `Face::declare_subscriber`；`Session::put` 发出的 `Push` 到达路由器后，就是走 `route_data`。

#### 4.2.2 核心流程

**声明一个 subscriber**（`declare_subscriber`）：

```
declare_subscriber(id, expr, ...)
├─ with_mapped_expr(expr)  → 把 WireExpr 解析/创建成 Resource res
├─ hats[region].register_subscriber(ctx, id, res, ...)   # 在 HAT 登记本 face 的订阅
│     └─ 设置 res.face_ctxs[src_face].subs = Some(SubscriberInfo)
├─ hats[region].disable_data_routes(&mut res)            # 让该 res 上缓存的数据路由失效
└─ for dst in 所有 region:
       hats[dst].propagate_subscriber(ctx, res, other_info)  # 跨 region 传播声明
```

**转发一条数据**（`route_data`）：

```
route_data(tables, src_face, Push, reliability, consume)
├─ 解析 wire_expr.scope 得到 prefix → 组成 RoutingExpr
├─ ingress_filter(src_face)  # 入站拦截器过滤
├─ route = get_data_route(tables, src_face, expr, node_id)
│     └─ 遍历每个 region：hats[region].compute_data_route(...)  # 委托 HAT 算路由
├─ treat_timestamp!(...)  # 必要时补/校验时间戳
├─ 用 inter_region_filter + egress_filter 过滤候选方向
└─ for dir in route:
       send_push(dir.dst_face, 改写 wire_expr/node_id 后的 Push)  # 实际发送
```

注意「登记 + 失效 + 传播」三段式，以及「锁内读路由、出锁再发送」的并发纪律（与《u8-l1》一致）。

#### 4.2.3 源码精读

**声明 subscriber**：

[pubsub.rs:49-82](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/pubsub.rs#L49-L82)：`declare_subscriber`。先 `with_mapped_expr` 把 WireExpr 解析成 Resource；然后 `register_subscriber`（HAT 内部会把 `subs` 位写成 `Some`）；接着 `disable_data_routes` 让缓存的 `Routes` 失效（这样下次 `route_data` 会重算）；最后对所有 region 调 `propagate_subscriber`，`other_info` 用 `reduce` 聚合「其它 region 是否已有人订阅同 key」（用于去重/聚合）。

**反声明与清理**：

[pubsub.rs:90-128](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/pubsub.rs#L90-L128)：`undeclare_subscriber`。若移除后所有 region 都不再有该 subscriber（`remaining` 为空），就向所有 region `unpropagate_subscriber` 并 `Resource::clean` 回收；若只剩一个「非本 region」的 owner，则只对该 owner 做特殊反传播（`unpropagate_last_non_owned_subscriber`）。

**数据路由的缓存与重算**：

[pubsub.rs:196-230](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/pubsub.rs#L196-L230)：`get_data_route`。它对每个 region 调 `get_hat_data_route`，而后者通过 `get_or_set_route`（[resource.rs:300-319](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/resource.rs#L300-L319)）实现「读时缓存、版本不匹配则重算」：

\[ \text{cached?} \equiv (version_{stored} == version_{tables}) \]

`RoutesVersion`（[resource.rs:217](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/resource.rs#L217)）是 64 位全局版本号；任何 `declare_*` 都会 `disable_*_routes`（即 `Routes::clear`），等价于让版本失效，从而触发下次 `route_data` 重算。这就是「声明变化 → 缓存失效 → 路由自动更新」的机制。

**实际转发**：

[pubsub.rs:232-349](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/pubsub.rs#L232-L349)：`route_data`。重点几处：

- [L279](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/pubsub.rs#L279) 算出 `route`；
- [L290-303](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/pubsub.rs#L290-L303) 定义 `inter_region_filter`，用 `InterRegionFilter { src, dst, src_zid, fwd_zid, dst_zid }.resolve(tables)` 做跨 region 去重（避免环路/重复投递）；
- [L305-322](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/pubsub.rs#L305-L322) 对单目标路由做优化（不必 clone payload），改写 `wire_expr`/`node_id` 后 `send_push`；多目标则 clone payload 逐个发送。

**HAT 如何算路由（以 peer 为例）**：

[hat/peer/pubsub.rs:232-319](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/peer/pubsub.rs#L232-L319)：`compute_data_route`。这是 dispatcher 委托的核心。它遍历 `matches`，对每个匹配资源取 `owned_face_contexts`，仅当 `ctx.subs.is_some() && self.region() != src_region` 时才把该 face 插入路由。**`ctx.subs.is_some()` 就是「这个 face 在相交的 key 上确实有订阅」的判据**——它正是 4.1 里 `FaceContext.subs` 那一位。

#### 4.2.4 代码实践

**实践目标**：跟踪一条 `Push` 从入站到转发，确认「只有 `subs.is_some()` 的 face 才被转发」。

**操作步骤**（源码阅读 + tracing 实践）：

1. 在 `compute_data_route`（[hat/peer/pubsub.rs:232-319](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/peer/pubsub.rs#L232-L319)）里找到 `ctx.subs.is_some()` 这个判断，记下行号。
2. 顺藤摸瓜：`subs` 是在哪被写成 `Some` 的？→ peer HAT 的 `register_subscriber`（[hat/peer/pubsub.rs:322-353](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/peer/pubsub.rs#L322-L353)），它在 `res.face_ctxs[src_face]` 上设 `subs = Some(*info)`。
3. 用环境变量 `RUST_LOG="zenoh::net::routing::dispatcher::pubsub=debug,zenoh::net::routing::hat::peer=debug"` 运行任意 pub/sub 示例（如 `cargo run --example z_sub` + `cargo run --example z_pub`），观察 `route_data` 与 `compute_data_route` 的 tracing 日志。

**需要观察的现象**：

- 发布端发送后，路由器日志应出现 `Route data for res ...`（`route_data` 的 trace，[pubsub.rs:254-259](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/pubsub.rs#L254-L259)）。
- `compute_data_route` 的 span 会在 `ret` 字段里打印算出的路由（face id 列表）。

**预期结果**：有 subscriber 时路由非空、订阅端收到；撤销 subscriber 后再次发布，`compute_data_route` 返回空路由、订阅端不再收到。**待本地验证**（取决于你用单进程还是带 zenohd 的拓扑）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `declare_subscriber` 里要调用 `disable_data_routes`？不调会怎样？

> **答案**：`get_data_route` 会缓存上次算的路由（按 `RoutesVersion`）。新增 subscriber 后若不失效缓存，`route_data` 还会用「没有这个 subscriber」的旧路由，导致新订阅者收不到数据。`disable_data_routes` 把缓存清空，强制下次重算。

**练习 2**：`route_data` 在路由只有 1 个目标 vs 多个目标时，对 payload 的处理有何不同？为什么？

> **答案**：单目标时（[pubsub.rs:305-322](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/pubsub.rs#L305-L322)）若 `consume=true` 可直接移动原消息、避免 clone；多目标时（[pubsub.rs:323-347](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/pubsub.rs#L323-L347)）必须为每个方向 clone payload。这是「常见单目标场景」的性能优化。

---

### 4.3 Query 路由：declare_queryable 与 route_query

#### 4.3.1 概念说明

`queries.rs` 是 pub/sub 的「拉模型」镜像。区别在于：

- pub/sub 转发的是**数据**（`Push`），目标是「有 subscriber 的 face」；
- query 转发的是**请求**（`Request`），目标是「有 queryable 的 face」，并且要按 **`QueryTarget`** 选目标，还要处理**应答回流**与**超时**。

三种 `QueryTarget`（协议层定义，见《u4-l2》）决定了选谁：

| `QueryTarget` | 语义 | `compute_final_route` 行为 |
| --- | --- | --- |
| `All` | 问所有匹配的 queryable | 全部插入路由 |
| `AllComplete` | 问所有 complete 的 queryable | 仅 `info.complete==true` 的 |
| `BestMatching` | 找一个最近的 complete，否则退化为 All | 先找一个 complete，找不到再 `All` |

另一个独有点：query 是**请求-应答**，必须为每个转发出去的查询登记一个「待应答」状态（`pending_queries`），以便把对端的 `Response` / `ResponseFinal` 正确路由回发起方，并在超时未应答时回一个 `Err("Timeout")`。

#### 4.3.2 核心流程

**路由一条查询**（`route_query`）：

```
route_query(Request)
├─ 解析 prefix → RoutingExpr；ingress_filter
├─ for region in regions:
│     qabls = get_query_route(...)           # 该 region 下匹配的 queryable 集合
│     compute_final_route(target, builder, query, qabls, filter)
│           └─ 按 All / AllComplete / BestMatching 选目标
├─ 若 builder 为空 → 直接 send_response_final（无人可答）
└─ else for QueryDirection{dir, rid} in builder:
       spawn_query_clean_up_task(dst_face, rid, timeout)   # 超时守护
       send_request(Request { id: rid, wire_expr: dir.wire_expr, ... })
```

**应答回流**（`route_send_response` / `route_send_response_final`）：用 `face.pending_queries[rid]` 反查原始 `(src_face, src_qid)`，把 `msg.rid` 改回 `src_qid` 后 `send_response` 给发起方；收到 `ResponseFinal` 时从 `pending_queries` 移除并取消超时任务。

#### 4.3.3 源码精读

**按目标选路由**：

[queries.rs:352-401](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/queries.rs#L352-L401)：`compute_final_route`。三个分支用 `route.insert(face_id, || …)`（带 face 去重）。注意 `BestMatching`（[queries.rs:384-398](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/queries.rs#L384-L398)）：先 `find` 第一个 `complete` 的 queryable，找到就只发它；找不到就递归调 `All`。这与《u4-l2》讲的公开 API 默认 `BestMatching` 行为一致。

**待应答登记与 id 分配**：

[queries.rs:404-417](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/queries.rs#L404-L417)：`insert_pending_query`。每转发一次查询，就在出站 face 上生成一个新的本地 `qid`（`next_qid.wrapping_add(1)`），并把 `(Arc<Query>, cancellation_token)` 存进 `pending_queries`。注释解释了为何用 `wrapping_add`（qid 用 varint 编码，增量 id 并非最优，但冲突概率极低）。

**超时守护**：

[queries.rs:419-503](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/queries.rs#L419-L503)：`QueryCleanup`。它 `tokio::select!` 在「`sleep(timeout)`」与「`cancellation_token.cancelled()`」之间二选一：要么超时，走 `run()` 回一个 `Err("Timeout")` 应答并清理；要么对端正常 `ResponseFinal` 取消它。超时时长取 `msg.ext_timeout`，缺省用 `tables.queries_default_timeout`（[queries.rs:267-269](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/queries.rs#L267-L269)）。

**应答改写回流**：

[queries.rs:533-605](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/queries.rs#L533-L605)：`route_send_response`。关键是 [L586-587](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/queries.rs#L586-L587)：把入站 `msg.rid`（本 face 的本地 qid）**改回** `query.src_qid`（发起方的原始 qid），再把 QoS 还原成 `query.src_qos`，然后发给 `query.src_face`。这就是「跨多跳查询时 id 翻译」的落点。

**queryable 信息聚合**：

[queries.rs:655-700](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/queries.rs#L655-L700)：`merge_qabl_infos` 与 `QueryableInfoType::aggregate`。当多个 region 都有 queryable 时，要把 `(complete, distance)` 聚合成一个对外可见的信息——`complete` 取或，`distance` 在同 complete 下取 `min`。这决定了 `BestMatching` 能否找到一个 complete 且更近的目标。

#### 4.3.4 代码实践

**实践目标**：观察 `QueryTarget` 如何改变路由结果。

**操作步骤**：

1. 准备两个 queryable 进程，都监听同一 key（如 `q/example`），都设为 `complete=true`；再准备一个设为 `complete=false`。
2. 用 `z_get`（默认 `BestMatching`）查询，观察收到几条应答。
3. 改用代码把 `target` 显式设为 `All`（参考《u4-l2》关于 `QueryTarget` 的说明），再查一次。

**需要观察的现象**：

- `BestMatching`：应只收到 1 条（找最近的 complete）。
- `All`：应收到全部 queryable 的应答。

**预期结果**：与《u4-l2》公开 API 行为一致——`BestMatching` 在有 complete queryable 时只取一个；`All` 全取。**待本地验证**（具体如何设置 queryable 的 complete 标志，参见 `z_queryable` 示例与 `QueryableInfoType`）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `route_query` 在 `dirs.is_empty()` 时要主动 `send_response_final`？

> **答案**：查询是请求-应答，发起方在等 `ResponseFinal` 才会结束 `get`（见《u4-l2》的 `nb_final` 归零逻辑）。若路由器找不到任何 queryable 又不发 final，发起方会一直挂死到超时。主动发 final 让发起方立即知道「无应答」。

**练习 2**：`BestMatching` 找不到 complete 的 queryable 时会怎样？

> **答案**：退化为 `All`（[queries.rs:395-397](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/queries.rs#L395-L397)），即把查询发给所有匹配的（即便非 complete 的）queryable，保证至少有应答机会。

---

### 4.4 Interest：声明式兴趣与兴趣驱动剪枝

#### 4.4.1 概念说明

`interest` 是 Zenoh 路由协议里最巧妙的一环，理解它是本讲的重点。直觉上：

> **interest = 「请把将来出现的、与某个 key 相关的声明（subscriber/queryable/token）告诉我」**。

它是一种**元声明**——声明的不是数据实体，而是「对声明的兴趣」。它的价值在于**按需传播**：默认情况下，Zenoh 不会把全网每个 subscriber/queryable 都洪泛给每个节点；只有当某节点显式声明了 interest，路由器才会把匹配的声明转发给它。这就是「兴趣驱动的剪枝」。

`Interest` 有三个维度（协议层 `Interest` 消息）：

- **mode**：`Current`（只要当前快照，问完即止）、`Future`（从现在起持续监听将来的变化）、`Final`（撤销一个已注册的 Future 兴趣）。
- **options**：位掩码，指明关心哪类实体——`subscribers()` / `queryables()` / `tokens()` / `keyexprs()` / `aggregate()`。
- **wire_expr**：兴趣的 key 范围（可为空，表示「全 key」）。

`interests.rs` 里两组数据结构分别承载两种用途：

- `RemoteInterest { res, options, mode }`（[interests.rs:57-78](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/interests.rs#L57-L78)）：一个远端 Future 兴趣，存进 HAT 的 `face_hat(face).remote_interests`，用于**剪枝声明传播**（路由决策）。
- `face.remote_key_interests: HashMap<InterestId, Option<Arc<Resource>>>`（[face.rs:123](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/face.rs#L123)）：keyexpr 兴趣，用于**短 keyexpr 优化**（带宽决策）。

这两者都要看，但分工不同。

#### 4.4.2 核心流程

`Face::interest`（`interests.rs` 的入口）先做一连串**合法性校验**（拒绝非法组合，如 peer 不能发 aggregate、Current 只能问 tokens 等），然后分三路：

```
Face::interest(msg)
├─ 校验：region 朝向、角色、options 合法性、Current 仅限 tokens
├─ if options.keyexprs() && mode != Current:
│     register_expr_interest(...)        # 记 keyexpr 兴趣 → face.remote_key_interests
├─ with_mapped_optional_expr(wire_expr, |tables, res| {
│     route_interest_res = hats[North].route_interest(ctx, msg, res, &src)   # 把兴趣向 north 传播
│     match msg.mode {
│       Current  => send_current_subscribers/queryables/tokens(...)  # 回当前快照
│       Future   => hats[region].register_interest(ctx, msg, res)     # 登记 Future 兴趣
│     }
│     if ResolvedCurrentInterest => send_declare_final(...)
│   })
```

**兴趣如何驱动剪枝**（核心，对应实践任务）：

```
某 face A 声明 Future 兴趣：subscribers() on "foo/*"
  → hats[region].register_interest(...)
  → face_hat(A).remote_interests[id] = RemoteInterest{ res:"foo/*", subs }

之后，另一个 region 有 face B 声明 subscriber on "foo/bar"
  → pubsub.rs declare_subscriber → propagate_subscriber → maybe_propagate_subscriber(B, "foo/bar")
  → 检查 face_hat(A).remote_interests.iter()
        .filter(|(_, i)| i.options.subscribers() && i.matches("foo/bar"))
    "foo/*" 与 "foo/bar" 相交 → matches() == true → should_notify = true
  → 仅向 A 发 DeclareSubscriber("foo/bar")，并登记进 face_hat(A).local_subs

再之后，有人发布 "foo/bar"
  → route_data → compute_data_route
  → 读 FaceContext.subs（A 在 "foo/bar" 上有 subs，因为上面 propagate 过）
  → 把数据转发给 A
```

> 关键洞察：**interest 从不出现在数据转发路径上**。它在「声明传播」时充当门禁——只有匹配兴趣的 face 才会收到 `DeclareSubscriber`，从而才会在对应 `Resource` 的 `FaceContext.subs` 上留下 `Some`；而 `compute_data_route` 只读 `subs`。换句话说，interest 在声明时刻「塑造」了订阅表，数据时刻只是查这张表。

资源树（`resource.rs`）在这里扮演的角色是：提供 `FaceContext`（承载 `subs`/`subscriber_interest_finalized` 等位）和 `RemoteInterest::matches`（[interests.rs:75-78](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/interests.rs#L75-L78)）所依赖的 `Resource::matches`（[resource.rs:494-504](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/resource.rs#L494-L504)）。

#### 4.4.3 源码精读

**入口与校验**：

[interests.rs:204-235](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/interests.rs#L204-L235)：`Face::interest` 的开头。注意几条硬约束：north-bound 非 peer face 的 interest 非法（L207）；router 的 interest 暂不支持（L215）；`aggregate + tokens` 非法（L225）；`Current` 只能问 tokens（L230）。这些校验保护了协议不变量。

**Current vs Future 分流**：

[interests.rs:273-333](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/interests.rs#L273-L333)：`Current` 模式调 `send_current_subscribers/queryables/tokens`（回当前快照，配合 `other_*_matches` 跨 region 收集）；`Future` 模式调 `hats[region].register_interest`。`route_interest` 的返回值若是 `ResolvedCurrentInterest`，说明这条 Current 兴趣在本节点就已解决、无需继续上传，于是回 `DeclareFinal`（L331-333）。

**peer HAT 登记 Future 兴趣**：

[hat/peer/interests.rs:665-688](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/peer/interests.rs#L665-L688)：`register_interest`。非常简洁——把 `RemoteInterest { res, options, mode }` 插入 `face_hat(src_face).remote_interests[msg.id]`。注意「Interest ids cannot be re-used」的保护。

**剪枝门禁（核心）**：

[hat/peer/pubsub.rs:71-145](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/peer/pubsub.rs#L71-L145)：`maybe_propagate_subscriber`。看 [L95-111](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/peer/pubsub.rs#L95-L111)：遍历 `face_hat(dst_face).remote_interests`，`filter(|(_, i)| i.options.subscribers() && i.matches(res))`——**只有存在匹配的 subscriber 兴趣时 `should_notify` 才为 true**；[L113-115](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/peer/pubsub.rs#L113-L115) 处 `if !should_notify { return; }` 直接剪掉。这就是「不感兴趣的 face 不会被通知有 subscriber」的代码落点。

**数据路径上的 interest 残留处理（安全网）**：

[hat/peer/pubsub.rs:269-304](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/peer/pubsub.rs#L269-L304)：`compute_data_route` 里的特殊分支。当 north hat 处理来自 south 的数据时，除了转发给 `subs.is_some()` 的 face，还会转发给那些 **`subscriber_interest_finalized == false`** 的 south face（[L275-288](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/peer/pubsub.rs#L275-L288)）。原因：interest 握手未完成时，subscriber 声明可能还没传到，先「兜底转发」避免丢数据。`subscriber_interest_finalized` 正是 `FaceContext`（resource.rs）里的位，由 `InterestState::set_finalized`（[face.rs:89-104](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/face.rs#L89-L104)）在收到 `DeclareFinal` 时置位。

**keyexpr 兴趣（带宽优化）**：

[resource.rs:1043-1094](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/resource.rs#L1043-L1094)：`register_expr_interest`，把 keyexpr 兴趣写入 `face.remote_key_interests[id]`。它在 [resource.rs:688-693](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/resource.rs#L688-L693)（`decl_key`）里被查询：只有当 face 对某前缀有兴趣（或兴趣为 `None`=全 key），路由器才会向它发 `DeclareKeyExpr` 注册一个短 id，此后该 key 走短 id 而非全字符串，省带宽。

#### 4.4.4 代码实践（本讲核心实践任务）

**实践目标**：追踪一条 `declare_interest`（Future, subscribers）消息，画出「兴趣记录 → 声明传播剪枝 → 数据按表转发」的完整链路。

**操作步骤**（源码阅读型，画图为主）：

1. **记录兴趣**：从 [interests.rs:327-329](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/interests.rs#L327-L329)（Future 分支）进入 [hat/peer/interests.rs:665-688](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/peer/interests.rs#L665-L688) `register_interest`，确认兴趣被存入 `face_hat(A).remote_interests`。记下数据结构名：`remote_interests: HashMap<InterestId, RemoteInterest>`（[hat/peer/mod.rs:489-498](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/peer/mod.rs#L489-L498)）。
2. **剪枝传播**：假设别处新声明了 subscriber `foo/bar`。从 [pubsub.rs:72-80](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/pubsub.rs#L72-L80) `propagate_subscriber` 进入 [hat/peer/pubsub.rs:95-115](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/peer/pubsub.rs#L95-L115)，确认 `i.matches(res)` 决定了 `should_notify`。画出：兴趣 `foo/*` 与 subscriber `foo/bar` 经 `RemoteInterest::matches`（[interests.rs:75-78](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/interests.rs#L75-L78)）→ `Resource::matches` 判定相交 → 仅向 A 发 `DeclareSubscriber`。
3. **数据按表转发**：发布 `foo/bar` 时，[hat/peer/pubsub.rs:251-267](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/peer/pubsub.rs#L251-L267) `compute_data_route` 读 `ctx.subs.is_some()`——A 因上一步被 propagate，其 `FaceContext.subs` 已是 `Some`，故被加入路由。

**需要观察的现象 / 产出**：画一张文字流程图，标注每一步经过的文件:行号、用到的数据结构（`remote_interests` / `FaceContext.subs` / `local_subs`）、以及「若没有兴趣会发生什么」（`should_notify=false` → 不 propagate → `subs` 保持 `None` → `compute_data_route` 不路由给 A → 数据不送达 A）。

**预期结果**：你应当得出结论——**interest 是声明传播的开关，`FaceContext.subs` 是数据转发的判据；二者通过 `maybe_propagate_subscriber` 这一环衔接，interest 本身不参与数据快路径**。

> ⚠️ 说明：源码中大量 `NOTE(regions)` / `FIXME(regions)` 注释（如 [hat/peer/pubsub.rs:66-67](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/peer/pubsub.rs#L66-L67)、[L86-88](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/peer/pubsub.rs#L86-L88)）坦承 regions 机制仍在演进、部分命名有误导性。本讲只描述当前代码「确定在做的事」，不展开尚未稳定的 regions 细节。

#### 4.4.5 小练习与答案

**练习 1**：`Current` 模式的 interest 与 `Future` 模式有何本质区别？分别用在什么场景？

> **答案**：`Current` 是一次性快照——「把此刻已存在的 subscriber/queryable/token 告诉我」，问完即止，靠 `send_current_*` 实现；`Future` 是持续订阅——「从此刻起把将来的变化推给我」，靠 `register_interest` 存表、由后续 `propagate_*` 持续推送。liveliness 的 `get`（取当前存活 token）走 Current，`declare_subscriber(history=true)` 走 CurrentFuture 组合。

**练习 2**：`remote_interests`（HAT 内）和 `remote_key_interests`（FaceState 内）分别服务于什么目的？

> **答案**：`remote_interests` 服务于**路由剪枝**——决定 `DeclareSubscriber/Queryable/Token` 要不要传播给某 face（`maybe_propagate_*` 查它）。`remote_key_interests` 服务于**带宽优化**——决定要不要向某 face 发 `DeclareKeyExpr` 注册短 id（`decl_key` 查它），让后续消息用短 id 而非全字符串。

---

### 4.5 Token：liveliness 令牌的声明与传播

#### 4.5.1 概念说明

`token.rs` 是 liveliness（见《u6-l2》）在 dispatcher 层的落点。liveliness token 是一种「以 key 表示的存在感」——声明一个 token 等于宣告「我在」，撤销（或进程掉线）等于「我不在了」。它的路由机制与 subscriber 类似（声明、跨 region 传播、按兴趣剪枝），但更简单：token 没有附加 info，只是一个布尔性的存在。

#### 4.5.2 核心流程

```
declare_token(id, expr, node_id, interest_id, ...)
├─ with_mapped_expr(expr) → res
├─ 若带 interest_id 且来自 south → 报错（只允许下游流）
├─ match interest_id:
│   Some → route_current_token(...)   # 应答一个 Current token 兴趣
│   None → register_token(...)        # 登记 token
│          for dst in regions: 若 InterRegionFilter.resolve 通过 → propagate_token(dst)
```

撤销（`undeclare_token`）则反向清理：`unregister_token` + 跨 region `unpropagate_token`，无人再持有时 `Resource::clean`。

#### 4.5.3 源码精读

[token.rs:38-117](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/token.rs#L38-L117)：`declare_token`。注意 [L46-54](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/token.rs#L46-L54) 的方向约束：带 `interest_id` 的 token 只能「下游流」（south→north），反向是非法的。主分支（[L90-114](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/token.rs#L90-L114)）先 `register_token`，再遍历每个 region 用 `InterRegionFilter { src, dst, src_zid, fwd_zid, dst_zid:None }.resolve(tables)` 决定是否向该 region `propagate_token`——与 `route_data` 的跨 region 过滤同构。

[token.rs:119-178](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/token.rs#L119-L178)：`undeclare_token`。`unregister_token` 返回后，统计还有哪些 region 持有该 token（`remote_tokens_of`），据此决定是全网 `unpropagate_token`+`clean`，还是只对最后那个非 owner region 做特殊处理。

> 与 interest 的关系：token 的「按需传播」同样受 interest（`tokens()` 选项）驱动——见 4.4 里 `send_current_tokens` 与 `register_interest` 对 tokens 的处理。换句话说，token 也走「兴趣剪枝 → 数据/声明转发」同一套范式。

#### 4.5.4 代码实践

**实践目标**：验证 token 的传播方向约束。

**操作步骤**：

1. 阅读 [token.rs:46-54](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/token.rs#L46-L54) 的报错分支，说明它防止了什么。
2. 结合《u6-l2》的 `z_liveliness` 示例：声明 token 后，在另一节点用 liveliness `get` 查询，应能查到；令声明进程退出，应收到 Delete。

**预期结果**：token 声明 → 全网可见；进程退出 → 自动撤销。**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：为什么带 `interest_id` 的 token 不允许来自 south-bound face？

> **答案**：带 `interest_id` 的 token 是对一个 Current token 兴趣的**应答**，协议规定这种应答只能「往回流」给提问方（下游方向）。若来自 south-bound face，说明方向反了，是协议违规，故直接报错拒绝。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个「全链路阅读 + 画图」任务：

**场景**：拓扑为 `Subscriber S — Router R — Publisher P`（R 是 peer 或 router，S/P 是 R 的两个 face）。S 订阅 `demo/temp/*`，P 发布 `demo/temp/room1`。

**任务**：

1. **声明阶段**：画出 S 的 `declare_subscriber` 在 R 内的完整路径——
   - `with_mapped_expr` 把 `demo/temp/*` 解析成 `Resource`（resource.rs）；
   - `register_subscriber` 在 `FaceContext.subs` 写 `Some`（hat/peer/pubsub.rs）；
   - `disable_data_routes` 失效缓存（pubsub.rs）；
   - 若 P 所在 region 需要，经 interest 剪枝后 `propagate_subscriber` 把声明传给 P 侧。
2. **数据阶段**：画出 P 发布 `demo/temp/room1` 时 `route_data` 的路径——`get_matches("demo/temp/room1")` 命中 `demo/temp/*`（resource.rs `get_matches`）→ `compute_data_route` 见 S 的 `subs.is_some()` → 转发给 S。
3. **撤销阶段**：S 撤销订阅，画出 `undeclare_subscriber` 如何清 `subs`、`disable_data_routes`、并在无人订阅时 `Resource::clean`。

**产出**：一张包含「文件:行号 + 数据结构名 + 消息方向」的端到端时序图。完成后，你应能用一句话回答：「为什么 P 发布的数据能精确地只送到 S，而不会洪泛给无关 face？」——因为 `get_matches` 限定了相交资源，`compute_data_route` 只选 `subs.is_some()` 的 face，而 interest 又在声明传播阶段就剪掉了不关心的分支。

## 6. 本讲小结

- **`Resource`（resource.rs）** 是 key expression 的前缀树，`get_matches` 用 chunk 级 `intersects` + `**` 通配做相交匹配，只返回「有 context（即被声明过）」的节点；`FaceContext` 的 `subs/qabl/token/*_interest_finalized` 位是所有路由判断的依据。
- **`route_data`（pubsub.rs）** 把 `Push` 委托给 HAT 的 `compute_data_route`，后者只向 `ctx.subs.is_some()` 且异 region 的 face 转发；路由结果按 `RoutesVersion` 缓存，`declare_*` 用 `disable_*_routes` 失效缓存。
- **`route_query`（queries.rs）** 按 `QueryTarget`（All/AllComplete/BestMatching）选 queryable，用 `pending_queries` + `QueryCleanup` 管理「待应答 + 超时」，应答时把本地 qid 翻译回发起方 src_qid。
- **`interest`（interests.rs）** 是「对声明的兴趣」：Future 兴趣存进 `face_hat.remote_interests`，在 `maybe_propagate_subscriber` 等「传播门禁」处被查询——只有匹配兴趣的 face 才会收到声明，从而其 `FaceContext.subs` 才会被置位。interest **不参与数据快路径**，它在声明时刻塑造订阅表。
- **`remote_key_interests`（face.rs）** 是 interest 的另一个用途——keyexpr 短 id 优化，决定是否向某 face 发 `DeclareKeyExpr`，让后续消息用短 id 省带宽。
- **`token`（token.rs）** 是 liveliness 的路由落点，机制与 subscriber 同构（声明、按 `InterRegionFilter` 跨 region 传播、按兴趣剪枝），但更轻（无 info，仅存在性）。
- **贯穿规律**：dispatcher 负责「登记 + 失效 + 委托 HAT + 实际发送」，HAT 负责「算路由」；并发上遵循「锁内收集/读路由、出锁再发送」。

## 7. 下一步学习建议

- **下一讲《u8-l4 路由协议：链路状态、网络与 gossip》**：本讲的 `InterRegionFilter`、`propagate_*` 都依赖「跨 region/跨 router 的拓扑视图」——下一讲讲清 router 间如何用 linkstate/gossip 协议同步拓扑，你才能完整理解 `InterRegionFilter.resolve` 背后的「谁连着谁」。
- **继续阅读源码**：
  - 想看清「声明如何跨多跳 router 一路传播」，读 `zenoh/src/net/routing/hat/router/`（router HAT 的 `propagate_subscriber` 与树计算）。
  - 想看清 interest 在协议线上的样子，读 `commons/zenoh-protocol/src/network/interest.rs` 与 `commons/zenoh-codec/src/network/`。
  - 想理解 `InterRegionFilter` 的判定细节，读 `zenoh/src/net/routing/dispatcher/tables.rs` 中 `InterRegionFilter::resolve`。
- **回顾对照**：学完下一讲后，回来重读本讲 4.4 的剪枝实践，你会发现「兴趣驱动剪枝」与「链路状态拓扑」正是 Zenoh 路由可扩展的两根支柱。
