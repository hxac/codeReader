# 路由骨架：Gateway / Face / Tables

> 本讲是「内部架构（二）：路由」单元的第一讲，承接《u7-l2 Primitives 与 Mux/DeMux》。
> 在那一讲里，你已经知道一条 `Put` 出入站都复用同一个 `route_data`，并且 `Mux`（出口）与 `DeMux`（入口）被缝在同一张 `Face` 上。
> 本讲要回答：**这张 Face 到底是什么？它是被谁创建、又注册到哪里的？路由用的「表」长什么样？**

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 `Gateway`、`Face`、`Tables` 三者各自的职责与关系：谁负责「总装」、谁是「路由单元」、谁是「共享状态」。
- 区分 `Face`（句柄）、`FaceState`（真实状态）、`WeakFace`（弱引用）三种形态，并解释 `Face` 为何同时扮演「入站 `Primitives`」与「出站 `EPrimitives` 持有者」两个角色。
- 读懂 `TablesData` 的关键字段（`faces`、`root_res`、`routes_version`、`hats`），并理解 `Resource` 与 `FaceContext` 如何把「key expression」与「face」关联起来。
- 完整追踪「一个新 subscriber 通过 Face 声明」时 `Tables` 内部发生的注册、失效与传播动作，并能标注涉及的关键数据结构名。

## 2. 前置知识

本讲默认你已经掌握以下内容（来自前置讲义）：

- **Session 与 Runtime**（u7-l1）：公开 `Session` 是 `Arc<SessionInner>` 薄壳；`Session::init` 经 `runtime.new_primitives → Gateway::new_session` 开一张 `Face`。
- **Primitives / EPrimitives / Mux / DeMux**（u7-l2）：`Primitives` 把消息交进路由织网，`EPrimitives` 把消息送出某张 face 并返回 `bool`；`DeMux` 处理入站、`Mux` 处理出站，二者缝在同一张 `Face` 上。
- **Key Expression 的集合匹配**（u2-l2）：路由匹配本质是 key expression 的 `intersects` / `includes` 判断。

两个需要先建立的术语直觉：

- **Face（会话面）**：可以理解成「路由器上的一个端口」。一条对端传输（TCP/UDP/QUIC…）对应一张 Face，一个本地公开 `Session` 也对应一张 Face。消息在 Face 之间被转发，就像以太网帧在网口之间被转发。
- **资源（Resource）**：key expression 在路由内部的树状表示。`Resource` 树是一棵以 `root_res` 为根、按 `/` 分层的 trie，每个节点可以挂多个 `FaceContext`，记录「哪些 face 在这个资源上声明了订阅/可查询/token」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [zenoh/src/net/routing/gateway.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/gateway.rs) | 路由总装车间：`GatewayBuilder` 构建 `Tables` 与各 HAT；`Gateway` 提供 `new_session` / `new_transport_unicast` / `new_transport_multicast` / `new_peer_multicast` 等工厂方法，把传输与路由表缝合。 |
| [zenoh/src/net/routing/dispatcher/face.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/face.rs) | 路由基本单元：`FaceState`（真实状态）、`Face` / `WeakFace`（句柄），以及 `impl Primitives for Face`（入站消息进入路由的入口）。 |
| [zenoh/src/net/routing/dispatcher/tables.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/tables.rs) | 路由表与共享状态：`TablesData`（全局状态）、`Tables`（data + hats）、`TablesLock`（三把锁的访问入口）、`InterRegionFilter`（跨 region 去重）。 |
| [zenoh/src/net/routing/mod.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/mod.rs) | routing 模块入口，定义 `RoutingContext<Msg>`（消息 + 全表达式信封），供 `SendDeclare` 携带出站声明。 |
| 辅助：[dispatcher/resource.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/resource.rs) 与 [dispatcher/pubsub.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/pubsub.rs) | `Resource` 树、`FaceContext`、`declare_subscriber` / `route_data` 等具体路由算法实现。 |

---

## 4. 核心概念与源码讲解

本讲拆三个最小模块：**Gateway（总装车间）**、**Face / FaceState（路由单元）**、**Tables / TablesData（共享状态）**。三者关系可以用一句话概括：

> `Gateway` 持有 `Arc<TablesLock>`；`TablesLock` 里锁着 `Tables{data, hats}`；`Face` 同时持有 `Arc<TablesLock>` 与 `Arc<FaceState>`，是「指针指回共享表」的可克隆句柄。

### 4.1 Gateway：路由总装车间

#### 4.1.1 概念说明

`Gateway` 是整个路由子系统的**入口结构**。它本身非常薄——只持有一个 `Arc<TablesLock>`：

```rust
pub struct Gateway {
    pub tables: Arc<TablesLock>,
}
```
（[gateway.rs:201-204](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/gateway.rs#L201-L204)）

它的真正职责是「**工厂**」：负责把传输（`TransportUnicast` / `TransportMulticast`）或本地 Session 与路由表**缝合**成一张 `Face`。换句话说，传输层只管收发字节，不知道「订阅」「路由」为何物；Gateway 负责给每条传输套上一层 `Face`，让它参与到路由织网里。

名字叫「Gateway（网关）」，是因为它支持**多 region** 拓扑：节点可以同时面向 `North`（北向，连上游）和 `South`（南向，连下游子域）等多个方向，在方向之间转发。即便是最简单的 client/peer 节点，也持有一个 Gateway，只是它内置的 HAT（路由策略）不同。

#### 4.1.2 核心流程

Gateway 的生命周期分两步：**构建**（组装表与 HAT）和**开 Face**（按需创建路由单元）。

**构建（`GatewayBuilder::build`）**：

1. 读 `config.mode()`（router/peer/client），决定要创建哪些 `Region`（默认总有一个 `Region::North` 与一个 `Region::Local`）。
2. 按 region × 角色组合，为每个 region 实例化一个 `HatTrait`（路由策略）：北向 client 用 `hat::client`、南向 client 用 `hat::broker`、peer 用 `hat::peer`、router 用 `hat::router`。
3. 用这些 HAT 与全局参数构造 `TablesData`，包进 `TablesLock`。
4. 返回 `Gateway { tables: Arc<TablesLock> }`。

```text
GatewayBuilder::new(config)
   │  .hlc(hlc)            // 可选：注入 HLC（用于时间戳）
   │  .build()
   ▼
Gateway { tables: Arc<TablesLock> }   ←  之后 Runtime 调 init_hats / new_session
```

**开 Face（三种典型入口）**：

| 方法 | 触发场景 | 创建的 Face |
| --- | --- | --- |
| `new_session(primitives)` | 公开 `zenoh::open` 时，为本地 Session 开面 | 本地 Face：`Region::Local` / `Bound::North` / `whatami=Client` / `local=true` |
| `new_transport_unicast(transport, region, remote_bound)` | 一条单播传输建连成功 | 远端 Face：出口=`Mux`，入口=`DeMux`，二者缝在同一张 Face |
| `new_transport_multicast` / `new_peer_multicast` | 多播传输 | 多播 Face：出口=`McastMux` |

#### 4.1.3 源码精读

**Gateway 结构体**仅持有一把共享锁的句柄（[gateway.rs:201-204](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/gateway.rs#L201-L204)）。注意 `whatami` 字段被注释掉了——本端角色不存这里，而是由各 HAT 自带的 `mode` 表达。

**`build` 的核心**：根据 region 为每张「面方向」挑选 HAT，并构造 `TablesData`（[gateway.rs:150-198](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/gateway.rs#L150-L198)）。关键一句：

```rust
let data = TablesData::new(
    zid,
    self.hlc,
    self.config,
    regions.iter().copied().map(|b| (b, tables::HatTablesData::new())).collect(),
    ...
)?;
Ok(Gateway {
    tables: Arc::new(TablesLock {
        tables: RwLock::new(Tables { data, hats }),
        ctrl_lock: Mutex::new(()),
        queries_lock: RwLock::new(()),
    }),
})
```
（[gateway.rs:178-198](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/gateway.rs#L178-L198)）—— `Tables` 由 `data`（全局状态）与 `hats`（按 region 的策略集）两部分组成。

**`new_session`（为本地 Session 开面）** 用 `FaceStateBuilder` 构造一张本地 Face，插入 `tables.data.faces`，再调用所属 HAT 的 `new_local_face` 完成本地初始化（[gateway.rs:220-262](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/gateway.rs#L220-L262)）：

```rust
let newface = Arc::new(
    FaceStateBuilder::new(
        tables.data.new_face_id(),
        tables.data.zid,
        Region::Local,
        Bound::North,
        primitives.clone(),            // ← 本地 Session 的 EPrimitives（出口）
        tables.hats.map_ref(|hat| hat.new_face()),
    )
    .whatami(WhatAmI::Client)
    .local(true)
    .build(),
);
tables.data.faces.insert(newface.id, newface.clone());
```
注意第 251-253 行的 `send_declare` 闭包写着 `unreachable!("no declarations should be pushed to new session faces")`——本地 Session 面不会向自己回推声明，因为它就是声明的源头。

**`new_transport_unicast`（为一条单播传输开面）** 是最能体现「缝合」的方法（[gateway.rs:264-355](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/gateway.rs#L264-L355)）。它做了三件事：

1. 从传输读对端 `whatami` / `zid`，建出口 `Mux` 与入口拦截器链 `ingress`；
2. 用 `FaceStateBuilder` 建 `FaceState` 并塞进 `tables.data.faces`，再 `mux.face.set(Face::downgrade(&face))` 把出口指向这张 Face；
3. 调用所属 HAT 的 `new_transport_unicast_face`，把跨 region 的初始声明收集到 `declares` 里；**先 drop 锁**，再逐条 `send_declare` 发出。

这个「**收集声明 → 释放锁 → 再发送**」的模式（先在锁内构造，出锁后再走网络 I/O）是 Zenoh 路由层反复出现的纪律，目的是绝不持锁做网络收发（[gateway.rs:327-347](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/gateway.rs#L327-L347)）。

#### 4.1.4 代码实践

**实践目标**：对比 Gateway 创建「本地面」与「传输面」时的差异，理解 Face 的身份字段含义。

**操作步骤**：

1. 打开 [gateway.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/gateway.rs)。
2. 对照 `new_session`（[L220-L262](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/gateway.rs#L220-L262)）与 `new_transport_unicast`（[L264-L355](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/gateway.rs#L264-L355)）。
3. 在两个终端分别运行示例，并用环境变量打开路由层调试日志：

   ```bash
   # 终端 1
   RUST_LOG=zenoh::net::routing=debug cargo run --example z_sub -- --mode peer
   # 终端 2
   RUST_LOG=zenoh::net::routing=debug cargo run --example z_pub -- --mode peer
   ```

**需要观察的现象**：日志里应出现形如 `New North/xxxx:0`、`New North/yyyy:1` 的 `tracing::debug!("New {}", newface)` 行（[gateway.rs:239](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/gateway.rs#L239) 与 [gateway.rs:318](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/gateway.rs#L318)）。格式 `region/zid.short():id` 来自 `FaceState` 的 `Display`（见 [face.rs:348-352](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/face.rs#L348-L352)）。

**预期结果**：本地 Session 面是 `Local/...:0`（`Region::Local`），对端传输面是 `North/...:1`（`Region::North`）。若看不到日志，说明节点间未建连——检查 scouting 是否被关闭。**待本地验证**（日志输出取决于具体拓扑）。

#### 4.1.5 小练习与答案

**练习 1**：`Gateway` 为什么不直接持有 `Tables`，而要套一层 `Arc<TablesLock>`？
**答案**：因为多张 Face、Runtime、Session 都要并发访问同一份路由表。`Arc<TablesLock>` 让所有 Face 共享同一份表，而 `RwLock` / `Mutex` 提供并发安全。Gateway 本身是薄壳，真正的状态在共享的 `TablesLock` 里。

**练习 2**：`build` 时为什么会为同一个节点创建多个 HAT（多个 region）？
**答案**：一个节点可能同时面向多个拓扑方向（北向连上游 router、南向连下游 client/peer）。每个方向有自己的路由策略（HAT），所以按 `region × 角色` 实例化多套 HAT。即便简单节点也至少有 `Region::North` 与 `Region::Local`。

---

### 4.2 Face / FaceState：消息路由的基本单元

#### 4.2.1 概念说明

**Face 是路由的最小单元**。可以把路由器想象成一台多端口交换机：每个端口就是一张 Face——一条对端传输一张 Face，一个本地 Session 一张 Face。消息的「路由」本质就是在 Face 之间决定「从哪张 Face 进、转发给哪些 Face」。

需要区分三种形态：

- **`FaceState`**：Face 的**真实状态**，是个大结构体，存 id、zid、whatami、region、出口 `primitives`、各种映射表与待办查询。存在 `Arc` 里被共享。
- **`Face`**：可克隆的**句柄**，只持 `Arc<TablesLock>` + `Arc<FaceState>`，`#[derive(Clone)]`，克隆极其便宜。
- **`WeakFace`**：弱引用版本，`upgrade()` 得到 `Option<Face>`，用于避免循环引用（例如 `Mux` 反向持有 Face）。

最关键的一点：**`Face` 实现了 `Primitives` trait**（[face.rs:544](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/face.rs#L544)）。这就是 u7-l2 说的「Face 双向使用」的落点：

- **入站方向**：`DeMux` 收到对端消息后，调用 `face.send_declare(...)` / `face.send_push_consume(...)`，Face 作为 `Primitives` 把消息交进路由。
- **出站方向**：路由算出该往哪些 Face 发后，调用 `dst_face.primitives.send_push(...)`，这里的 `primitives` 是 Face 持有的 `EPrimitives`（`Mux` / `McastMux` / 本地 Session 引用）。

#### 4.2.2 核心流程

`Face` 作为 `Primitives` 的核心分派逻辑（伪代码）：

```text
入站 Declare 消息 → Face::send_declare(msg)
   ├── DeclareKeyExpr        → register_expr(...)            // 登记 key 表达式映射
   ├── DeclareSubscriber      → self.declare_subscriber(...)  // 进入 pubsub 路由（见 4.3）
   ├── DeclareQueryable       → self.declare_queryable(...)
   ├── DeclareToken           → self.declare_token(...)
   ├── DeclareFinal           → self.declare_final(...)
   └── Undeclare*             → 对应反注册

入站 Push 消息   → Face::send_push_consume(msg) → route_data(...)  // 数据投递（见 4.3）
入站 Request     → Face::send_request(msg)      → route_query(...)
入站 Response    → Face::send_response(msg)     → route_send_response(...)
Face 关闭        → Face::send_close()           // 反注册该 face 的全部实体并清理
```

每个方法都遵循同样的**锁纪律**：先取 `ctrl_lock`（结构性变更互斥锁），在写锁内收集需要回推的 `declares`，**出锁后**再逐条 `send_declare` 真正发送（见 [face.rs:559-668](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/face.rs#L559-L668) 的 `DeclareSubscriber` 分支）。这与 4.1.3 的「收集 → 释放 → 发送」模式一致。

**Face 关闭（`send_close`）** 是理解 Face 生命周期的钥匙：它要彻底从 `Tables` 里抹掉这张 Face 的所有痕迹——反注册该 face 上的所有 subscribers / queryables / tokens、清空它的 `local_mappings` / `remote_mappings` / `local_interests`、最后 `tables.data.faces.remove(&src_fid)`（[face.rs:704-826](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/face.rs#L704-L826)）。这解释了为什么断线后路由能自愈：旧 Face 被清理，新 Face 建连时重新声明。

#### 4.2.3 源码精读

**`FaceState` 字段全景**（[face.rs:115-143](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/face.rs#L115-L143)）：

```rust
pub struct FaceState {
    pub(crate) id: FaceId,            // = usize，全局唯一 face 编号
    pub(crate) zid: ZenohIdProto,     // 对端节点 id（本地面则是本机 zid）
    pub(crate) whatami: WhatAmI,      // 对端角色
    pub(crate) region: Region,        // 该 face 所属拓扑方向
    pub(crate) remote_bound: Bound,   // 北/南向
    pub(crate) primitives: Arc<dyn EPrimitives + Send + Sync>,  // ← 出口
    pub(crate) local_interests: HashMap<InterestId, InterestState>,
    pub(crate) remote_key_interests: HashMap<InterestId, Option<Arc<Resource>>>,
    pub(crate) local_mappings: IntHashMap<ExprId, Arc<Resource>>,   // 本端声明的 expr 映射
    pub(crate) remote_mappings: IntHashMap<ExprId, Arc<Resource>>,  // 对端声明的 expr 映射
    pub(crate) pending_queries: HashMap<RequestId, (Arc<Query>, CancellationToken)>, // 待办查询
    pub(crate) mcast_group: Option<TransportMulticast>,
    pub(crate) hats: RegionMap<Box<dyn Any + Send + Sync>>,   // 每 region 的 HatFace 状态
    pub(crate) task_controller: TaskController,
    pub(crate) is_local: bool,
    ...
}
```

注意 `local_mappings` / `remote_mappings`：Zenoh 为了省带宽，不会每次都传完整 key expression，而是用 `ExprId` 引用一个**已声明的前缀资源**。`get_mapping` 按 `Mapping::Sender`（看对端映射）或 `Mapping::Receiver`（看本端映射）取回对应的 `Resource`（[face.rs:220-242](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/face.rs#L220-L242)）。

**`Face` / `WeakFace` 句柄**（[face.rs:364-383](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/face.rs#L364-L383)）：

```rust
pub struct Face {
    pub(crate) tables: Arc<TablesLock>,   // 指回共享路由表
    pub(crate) state: Arc<FaceState>,     // 指向自己的状态
}
```
每张 Face 都「指回」同一份 `TablesLock`——这就是所有 Face 共享路由表的方式。

**`impl Primitives for Face::send_declare`** 的 `DeclareSubscriber` 分支（[face.rs:568-581](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/face.rs#L568-L581)）：

```rust
DeclareBody::DeclareSubscriber(m) => {
    let mut declares = vec![];
    self.declare_subscriber(
        m.id, &m.wire_expr, &SubscriberInfo, msg.ext_nodeid.node_id,
        &mut |p, m| declares.push((p.clone(), m)),
    );
    drop(ctrl_lock);
    for (p, m) in declares { m.with_mut(|m| p.send_declare(m)); }
}
```

**`send_push_consume`** 把数据直接交给 `route_data`（[face.rs:670-683](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/face.rs#L670-L683)），这是 4.3 要细看的数据投递入口。

#### 4.2.4 代码实践

**实践目标**：理解 `Face` 的双向角色——既是 `Primitives`（入口），又持有 `EPrimitives`（出口）。

**操作步骤**：

1. 在 [face.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/face.rs) 找到 `impl Primitives for Face`（[L544](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/face.rs#L544)），确认它处理的是「入站」。
2. 在 `FaceState` 里找到 `primitives: Arc<dyn EPrimitives>` 字段（[L121](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/face.rs#L121)），确认它被「出站」使用——在 pubsub.rs 的 `route_data` 里搜索 `dst_face.primitives.send_push`。
3. 回到 [gateway.rs 的 `new_transport_unicast`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/gateway.rs#L264-L355)，找到 `let mux = Arc::new(Mux::new(...))` 与 `mux.face.set(Face::downgrade(&face))`，确认：出口 `Mux` 反向弱引用了这张 Face，二者成环的「弱」一端由 `downgrade` 打断。

**需要观察的现象**：你会看到一张 Face 同时是 `Primitives`（被 DeMux 调用）和 `EPrimitives` 的持有者（`Mux`），这正是 u7-l2「Mux/DeMux 缝在同一张 Face 上」的实现机制。

**预期结果**：能用自己的话写清「消息从 DeMux 进入 Face 的 send_xxx，路由后再从另一张 Face 的 state.primitives（即 Mux）发出」这条往返链路。

#### 4.2.5 小练习与答案

**练习 1**：`Face` 为什么要 `Clone`？它克隆时拷贝了什么？
**答案**：路由计算结果常常是一个 `Vec<Direction>`，每个 `Direction` 持有目标 `Arc<FaceState>`；发送时要按 face 操作。`Face` 只持两个 `Arc`，克隆只是增加引用计数，不拷贝真实状态，所以可以放心克隆给每个路由目标。

**练习 2**：`Face::send_close` 为什么要在最后调用 `tables.data.faces.remove(&src_fid)`？
**答案**：一张 Face 关闭意味着这条连接/会话彻底消失，必须把它从全局 face 注册表里删掉，否则后续路由仍可能把消息算到这张已死的 Face 上（泄漏 + 错投）。删除后，由该 Face 声明的订阅/查询也会被一并反注册。

**练习 3**：`local_mappings` 与 `remote_mappings` 各存什么？为什么 `get_mapping` 要按 `Mapping::Sender` / `Mapping::Receiver` 区分？
**答案**：`local_mappings` 存「本端声明的 ExprId→Resource」，`remote_mappings` 存「对端声明的 ExprId→Resource」。同一条 `WireExpr` 的 `scope`（ExprId）对本端和对端可能指向不同前缀，所以解析时要按这条消息是「对端发来的」（看 remote）还是「要发给对端的」（看 local）来取正确映射。

---

### 4.3 Tables / TablesData：路由表与资源表

#### 4.3.1 概念说明

`Tables` 是 Gateway 持有的**共享状态**，由两部分组成：

```rust
pub struct Tables {
    pub data: TablesData,                              // 全局路由状态
    pub hats: RegionMap<Box<dyn HatTrait + Send + Sync>>,  // 按 region 的路由策略
}
```
（[tables.rs:291-294](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/tables.rs#L291-L294)）

- **`TablesData`**：所有 Face 共享的全局状态——本机 `zid`、资源树根 `root_res`、face 注册表 `faces: HashMap<FaceId, Arc<FaceState>>`、各种超时（`queries_default_timeout`、`interests_timeout`）、拦截器工厂、以及一个全局 `routes_version`（路由缓存版本号）。
- **`hats`**：按 region 的路由策略集合（HAT = Hierarchical Adaptive Topology，下一讲专题）。声明/投递时，Gateway 总是先定位「这张 Face 所属 region 的 HAT」，再把动作委派给它。

访问入口是 **`TablesLock`**，它把三把锁捆在一起（[tables.rs:265-269](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/tables.rs#L265-L269)）：

| 锁 | 类型 | 保护对象 |
| --- | --- | --- |
| `tables` | `RwLock<Tables>` | 路由表本体（读多写少） |
| `ctrl_lock` | `Mutex<()>` | 结构性变更（声明/反声明）互斥 |
| `queries_lock` | `RwLock<()>` | 各 face 的 `pending_queries` 字段 |

**`Resource`** 是 key expression 在路由内部的**树状节点**（[resource.rs:373-381](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/resource.rs#L373-L381)）：

```rust
pub struct Resource {
    pub(crate) parent: Option<Arc<Resource>>,          // 父节点（root 无父）
    pub(crate) expr: String,                            // 从根到这里的完整表达式
    pub(crate) suffix: usize,                          // 本段 chunk
    pub(crate) nonwild_prefix: Option<Arc<Resource>>,  // 非通配前缀（匹配优化）
    pub(crate) children: SingleOrBoxHashSet<Child>,    // 子节点
    pub(crate) ctx: Option<Box<ResourceContext>>,       // 路由上下文（路由缓存等）
    pub(crate) face_ctxs: IntHashMap<FaceId, Arc<FaceContext>>,  // ← 关键！face↔资源 关系
}
```

最关键字段是 **`face_ctxs: IntHashMap<FaceId, Arc<FaceContext>>`**——它记录「哪些 Face 在这个资源上声明了什么」。**`FaceContext`** 是「某 Face × 某 Resource」的二元关系记录（[resource.rs:185-196](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/resource.rs#L185-L196)）：

```rust
pub(crate) struct FaceContext {
    pub(crate) face: Arc<FaceState>,
    pub(crate) local_expr_id: Option<ExprId>,
    pub(crate) remote_expr_id: Option<ExprId>,
    pub(crate) subs: Option<SubscriberInfo>,       // ← 该 face 在此资源上是订阅者
    pub(crate) qabl: Option<QueryableInfoType>,    // ← 该 face 在此资源上是可查询者
    pub(crate) token: bool,                        // ← 该 face 在此资源上声明了 token
    pub(crate) subscriber_interest_finalized: bool,
    pub(crate) queryable_interest_finalized: bool,
    pub(crate) in_interceptor_cache: InterceptorCache,
    pub(crate) e_interceptor_cache: InterceptorCache,
}
```

可以把 `Resource.face_ctxs` 理解成一张「**谁对这个 key 感兴趣**」的登记表，路由投递时就是查这张表。

#### 4.3.2 核心流程：一个新 subscriber 声明时，Tables 里发生了什么

这是本讲的核心。完整调用链如下（以本地 Session 声明订阅为例）：

```text
用户: session.declare_subscriber("a/b")
   │  （经公开 API → 内部 net 层）
   ▼
本地 Face::send_declare(DeclareSubscriber)        [face.rs:568]
   │  取 ctrl_lock，声明收集到 declares vec
   ▼
Face::declare_subscriber(id, wire_expr, ...)      [pubsub.rs:49]
   │
   ├─① with_mapped_expr(expr): 把 WireExpr 解析/创建为 Resource（res）
   │     · get_mapping(scope) → prefix Resource
   │     · 若不存在：make_resource + match_resource（挂到 Resource 树，建立 matches 关系）
   │     · 拿到 tables 写锁
   │
   ├─② hats[region].register_subscriber(ctx, id, res, node_id, info)
   │     · 在所属 HAT 的本地结构登记（如 router 的 router_subs；并写入 res.face_ctxs[face].subs）
   │
   ├─③ hats[region].disable_data_routes(&mut res)
   │     · 让该资源上缓存的数据路由失效（bump version），下次 route_data 重新计算
   │
   └─④ 遍历所有 region dst：
        hats[dst].propagate_subscriber(ctx, res, other_info)
           · 向其他 HAT（尤其 North）传播该订阅
           · 若需要让对端知道，经 send_declare 回调把 Declare 推到 declares vec
   │
   ▼ （with_mapped_expr 返回，写锁释放）
drop(ctrl_lock);
for (p, m) in declares { p.send_declare(m); }     [face.rs:578-580]  真正发出网络声明
```

涉及的关键数据结构名（请务必标注）：

| 动作 | 数据结构 | 位置 |
| --- | --- | --- |
| 解析表达式为资源 | `Resource` 树（`root_res` 起的 trie） | `TablesData.root_res` |
| 登记该 face 的订阅 | `Resource.face_ctxs: IntHashMap<FaceId, Arc<FaceContext>>`，写入 `FaceContext.subs` | [resource.rs:380](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/resource.rs#L380) |
| HAT 本地登记 | `HatTablesData`（每 region 的路由数据） + HAT 自己的 `router_subs` 等 | [tables.rs:156-171](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/tables.rs#L156-L171) |
| 失效路由缓存 | `ResourceContext.data_routes`（清空） / `TablesData.routes_version` | [resource.rs:343-345](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/resource.rs#L343-L345) |

**反向（数据投递）流程**：当一条 `Put` 到来，`route_data`（[pubsub.rs:232](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/pubsub.rs#L232)）做三件事：

1. `get_mapping` 解析 `WireExpr` 得到 `prefix`，构造 `RoutingExpr`；
2. `get_data_route(...)` 算出目标 `Route`（一组 `Direction`，每个含 `dst_face`）；路由结果会**按 `routes_version` 缓存**在 `ResourceContext.data_routes`，版本不符则重算（[pubsub.rs:196-230](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/pubsub.rs#L196-L230)）；
3. 用 `egress_filter` 过滤目标 face（不回发源 face、同多播组不重复），再逐个 `dst_face.primitives.send_push(...)` 发出。

**`egress_filter`** 是一个简单但重要的去重规则（[tables.rs:384-388](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/tables.rs#L384-L388)）：

```rust
fn egress_filter(&self, src_face: &FaceState, out_face: &Arc<FaceState>) -> bool {
    src_face.id != out_face.id
        && (out_face.mcast_group.is_none() || src_face.mcast_group.is_none())
}
```

语义：① 不把消息发回给源 face；② 若源与目标在同一多播组，则不通过单播重复发（多播已经覆盖）。

**跨 region 去重（`InterRegionFilter`）**：当存在多个网关（gateway）连接同一对 region 时，为避免重复转发，Zenoh 用一个确定性规则选「主网关」：在所有已知网关 zid 集合 `gwys` 中取最大值作为 `primary`，仅当 `self.zid == primary` 时才允许转发（[tables.rs:511-513](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/tables.rs#L511-L513)）：

\[ \text{primary} = \max(\text{gwys}), \quad \text{allow} \iff \text{self.zid} = \text{primary} \]

「取最大 zid」是一个无需协调的分布式破局法（tie-breaker）：所有网关独立计算得到同一个 primary，从而只有一方真正转发，避免重复。详见 [tables.rs:450-516](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/tables.rs#L450-L516) 的文档注释与 ASCII 图。

#### 4.3.3 源码精读

**`TablesData` 全局状态**（[tables.rs:122-148](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/tables.rs#L122-L148)）关键字段：

```rust
pub(crate) struct TablesData {
    pub(crate) zid: ZenohIdProto,
    pub(crate) runtime: Option<WeakRuntime>,
    pub(crate) drop_future_timestamp: bool,
    pub(crate) queries_default_timeout: Duration,
    pub(crate) interests_timeout: Duration,
    pub(crate) root_res: Arc<Resource>,               // ← 资源树根
    pub(crate) face_counter: FaceId,                  // ← 分配 face id 的计数器
    pub(crate) faces: HashMap<FaceId, Arc<FaceState>>, // ← face 注册表
    pub(crate) hats: RegionMap<HatTablesData>,        // ← 每 region 的路由数据
    pub(crate) routes_version: RoutesVersion,         // ← 全局路由缓存版本号
    ...
}
```

`new_face_id` 就是对 `face_counter` 自增（[tables.rs:252-256](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/tables.rs#L252-L256)）。`disable_all_routes` 通过 `routes_version.saturating_add(1)` 让所有缓存路由失效（[tables.rs:259-262](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/tables.rs#L259-L262)）。

**`declare_subscriber` 全流程**（[pubsub.rs:49-82](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/pubsub.rs#L49-L82)），这是本讲最该逐行读的函数：

```rust
pub(crate) fn declare_subscriber(&self, id, expr, sub_info, node_id, send_declare) {
    self.with_mapped_expr(expr, |tables, mut res| {       // ① 解析/创建 Resource
        let hats = &mut tables.hats;
        let region = self.state.region;
        let mut ctx = DispatcherContext { /* tables, src_face, send_declare */ };

        hats[region].register_subscriber(ctx.reborrow(), id, res.clone(), node_id, sub_info); // ② 本地登记
        hats[region].disable_data_routes(&mut res);                                         // ③ 失效缓存

        for dst in hats.regions().collect_vec() {                                           // ④ 跨 region 传播
            let other_info = hats.values()
                .filter(|hat| hat.region() != dst)
                .flat_map(|hat| hat.remote_subscribers_of(ctx.tables, &res))
                .reduce(|_, _| SubscriberInfo);
            hats[dst].propagate_subscriber(ctx.reborrow(), res.clone(), other_info);
        }
    });
}
```

**`register_subscriber` 的 HAT 接口**（[hat/mod.rs:456-463](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/mod.rs#L456-L463)）以 trait 方法定义；router HAT 的实现把它登记进 `router_subs` 并写入资源的 `face_ctxs`（[hat/router/pubsub.rs:400-421](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/router/pubsub.rs#L400-L421)）：

```rust
fn register_subscriber(&mut self, ctx, _id, mut res, node_id, info) {
    let Some(router) = self.get_router(ctx.src_face, node_id) else { ... };
    self.res_hat_mut(&mut res).router_subs.insert(router);   // ← HAT 本地结构
    self.router_subs.insert(res.clone());
    self.propagate_sourced_subscriber(ctx.tables, &res, info, Some(ctx.src_face), &router);
}
```

**`DispatcherContext`** 是声明操作在 HAT 之间传递的「请求包」（[hat/mod.rs:115-120](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/mod.rs#L115-L120)），携带 `tables`、`src_face` 与 `send_declare` 回调；`reborrow()` 让它能在循环里反复借用而不 move。

**`RoutingContext<Msg>`** 是出站声明的「信封」，把消息与它的完整表达式绑在一起（[routing/mod.rs:33-61](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/mod.rs#L33-L61)），供 `SendDeclare` 闭包使用。

#### 4.3.4 代码实践（本讲主实践任务）

**实践目标**：亲手追踪「一个新 subscriber 通过 Face 声明」时 `Tables` 内部的注册动作，并标注关键数据结构名。

**操作步骤**：

1. 从入口顺读：[face.rs `send_declare` 的 DeclareSubscriber 分支](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/face.rs#L568-L581) → [pubsub.rs `declare_subscriber`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/pubsub.rs#L49-L82) → [hat/mod.rs `register_subscriber`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/mod.rs#L456-L463) → [hat/router/pubsub.rs 实现](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/router/pubsub.rs#L400-L421)。
2. 对照 [Resource 结构](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/resource.rs#L373-L381) 与 [FaceContext 结构](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/resource.rs#L185-L196)，回答：订阅信息最终落在 `Resource` 的哪个字段、哪个子字段？
3. 写一段不少于 8 行的中文说明，覆盖：①表达式如何变成 `Resource`；②该 face 的订阅登记到 `face_ctxs[face_id].subs`；③`disable_data_routes` 为何必要；④`propagate_subscriber` 把订阅向哪些 region 传播；⑤声明消息何时真正发出（出锁后）。
4. （可选运行验证）开两个终端跑 `z_sub` / `z_pub`，设 `RUST_LOG=zenoh::net::routing::dispatcher=trace`，观察声明期的 trace 日志，与你的说明对照。

**需要观察的现象**：trace 日志里能看到 `declare_subscriber` 的 instrument 输出（含 `expr`、`node_id` 字段，[pubsub.rs:43-48](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/pubsub.rs#L43-L48)），以及随后的 `propagate_subscriber` 痕迹。

**预期结果**：你能给出一份标注了 `root_res`、`Resource.face_ctxs`、`FaceContext.subs`、`HatTablesData`、`routes_version` 的说明，并解释「为何声明完成后数据路由会被重算」。若日志级别不够看不到 trace，属正常——**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `declare_subscriber` 在登记后要立刻 `disable_data_routes(&mut res)`？
**答案**：拓扑变了（多了一个订阅者），此前为该资源算好并缓存的数据路由（存在 `ResourceContext.data_routes`）可能已经过期（漏了新订阅者）。`disable_data_routes` 清空缓存，迫使下一次 `route_data` 用 `get_data_route` 重新计算，保证新订阅者能收到数据。

**练习 2**：`TablesData.faces` 与 `Resource.face_ctxs` 各自记录什么？为什么需要两张「face 索引」？
**答案**：`TablesData.faces: HashMap<FaceId, Arc<FaceState>>` 是全局 face 注册表（按 id 查状态，用于关闭/清理）；`Resource.face_ctxs: IntHashMap<FaceId, Arc<FaceContext>>` 是「某个资源上有哪些 face、各自声明了什么」的局部视图（用于路由匹配）。前者是「face 存在性」，后者是「face 对某 key 的兴趣」，二者维度不同，缺一不可。

**练习 3**：`routes_version` 这个版本号解决什么问题？
**答案**：路由计算相对昂贵，而同一资源在拓扑未变时路由结果不变，所以缓存。但拓扑会变（声明/反声明/断线）。`routes_version` 是一个单调递增的「代」：缓存路由记下自己算出来时的版本，读取时若与当前 `routes_version` 不符就视为失效、重算。`disable_all_routes` / `disable_data_routes` 就是「bump 版本让缓存失效」的开关。

---

## 5. 综合实践

**任务：画出「声明订阅 → 发布数据 → 关闭」全过程中 Gateway/Face/Tables 三者的协作图，并用日志佐证。**

请完成以下步骤：

1. **准备**：克隆本仓库，确保能 `cargo build --release --examples`（参考 u1-l2）。
2. **启动观测**：三个终端。

   ```bash
   # 终端 A：订阅端（开路由 trace）
   RUST_LOG=zenoh::net::routing=debug cargo run --release --example z_sub -- --mode peer
   # 终端 B：发布端
   RUST_LOG=zenoh::net::routing=debug cargo run --release --example z_pub -- --mode peer
   ```
3. **画图**：基于本讲源码，绘制如下时序（文字版即可）：
   - **建连期**：两端各自 `Gateway::new_session`（本地 Face）→ scouting 建连 → `Gateway::new_transport_unicast`（远端 Face，缝 Mux/DeMux）→ 日志出现 `New ...`。
   - **声明期**：`z_sub` 的 `declare_subscriber` → `register_subscriber`（写 `Resource.face_ctxs[face].subs`）→ `disable_data_routes` → `propagate_subscriber`（向对端推 Declare）。
   - **投递期**：`z_pub` 的 `put` → 对端 `Face::send_push_consume` → `route_data`（`get_data_route` 算路由 → `egress_filter` 过滤 → `send_push`）→ 本地 Face 送达 `z_sub`。
   - **关闭期**：进程退出 → `Face::send_close` → `unregister_face_subscribers` → 若无人持有则 `Resource::clean` → `faces.remove`。
4. **验证**：把你图里的每个箭头与一处日志或一处源码行号对应；无法在日志中确认的环节标注「源码确认」。
5. **输出**：提交一张图 + 一份不超过 200 字的说明，重点解释「Gateway 负责 Face 的诞生与缝合、Face 是路由的入站口与出站持有者、Tables 是二者共享并查改的状态」三者如何咬合。

> 提示：若日志中 `route_data` 的细节看不全，可把级别调到 `trace`，或聚焦看 `New ...`（Face 创建）与 declare 的 instrument 行即可证明骨架运转。

## 6. 本讲小结

- **Gateway 是总装车间**：它只持 `Arc<TablesLock>`，核心职责是 `new_session` / `new_transport_unicast` 等工厂方法，把传输或本地 Session 缝合成一张 `Face`；建连时遵循「锁内收集声明 → 出锁再发送」的纪律。
- **Face 是路由基本单元**：`FaceState` 存真实状态（id/zid/region/出口 `primitives`/各种映射/待办查询），`Face` 是持 `Arc<TablesLock>+Arc<FaceState>` 的廉价克隆句柄，`WeakFace` 防止循环引用。
- **Face 双向使用**：它实现 `Primitives`（入站入口，被 `DeMux` 调用），同时又持有 `EPrimitives`（出口，即 `Mux`/`McastMux`/本地 Session），这是 u7-l2「Mux/DeMux 缝在同一张 Face」的落点。
- **Tables 是共享状态**：`Tables{data, hats}`；`TablesData` 持全局 `root_res` 资源树、`faces` 注册表、`routes_version` 等；`TablesLock` 用三把锁（`tables`/`ctrl_lock`/`queries_lock`）管控并发。
- **资源树 + face_ctxs 是匹配核心**：`Resource.face_ctxs: IntHashMap<FaceId, Arc<FaceContext>>` 记录「谁对这个 key 感兴趣」，`FaceContext.subs/qabl/token` 是其具体形态；路由投递本质是查这张表。
- **声明 = 登记 + 失效 + 传播**：`declare_subscriber` 做四件事——解析/创建 `Resource`、`register_subscriber` 写本地结构、`disable_data_routes` 失效缓存、`propagate_subscriber` 跨 region 传播；拓扑变更靠 `routes_version` 让缓存失效，跨网关去重靠 `max(gwys)` 选主。

## 7. 下一步学习建议

本讲把路由**骨架**立起来了，但故意回避了一个关键问题：**不同 region 的 HAT 在 `register_subscriber` / `propagate_subscriber` / `compute_data_route` 里到底各自怎么做？** 这正是下一讲《u8-l2 HAT：四种拓扑的路由策略》的主题。建议：

1. 先读 [hat/mod.rs 的 `HatTrait` 定义](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/mod.rs#L110-L113)，看清它由 `HatBaseTrait + HatInterestTrait + HatPubSubTrait + HatQueriesTrait + HatTokenTrait` 五个 super-trait 组成。
2. 对比本讲引用的 [router HAT 的 `register_subscriber`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/router/pubsub.rs#L400-L421) 与 [peer HAT 的同名方法](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/hat/peer/pubsub.rs#L322)，体会「树计算链路状态」与「点对点」的差异。
3. 之后进入《u8-l3 Dispatcher：资源、pubsub、查询与 interest》，把 dispatcher 各子模块（resource/pubsub/queries/interests/token）逐个打通。
