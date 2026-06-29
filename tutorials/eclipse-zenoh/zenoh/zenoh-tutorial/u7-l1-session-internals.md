# Session 内部与 Runtime

## 1. 本讲目标

前面六讲里，我们一直在用公开 API（`zenoh::open`、`Session`、`declare_subscriber`、`put`、`get`……）写应用。本讲开始「向下挖一层」：看看你写的 `session.declare_subscriber("a/b")` 这一行代码，在 Zenoh 内部到底走了哪些组件、最终把「订阅兴趣」注册到了哪里。

读完本讲，你应当能够：

- 画出公开 API（`api/`）与内部实现（`net/`）之间的边界，知道哪些是稳定的、哪些是内部的；
- 说清 `Session` 与 `SessionState` 各自存了什么、职责如何分工；
- 说清 `Runtime` 与 `RuntimeState` 存了什么、它为什么是「会话运行时的核心」；
- 沿着一条 `declare_subscriber` 调用链，从 `api/session.rs` 一路追到 `net/runtime/mod.rs` 与 `net/routing/`，解释公开 API 是如何「委托」到内部 net 层的。

本讲是后续 `u7-l2 Primitives 与 Mux/DeMux`、`u7-l3 Runtime 编排器` 以及整个第 8 单元（路由 / HAT）的入口。

## 2. 前置知识

本讲假设你已经学完：

- **u1-l4 公开 API 地图**：知道 `lib.rs` 是公开门面，`Resolvable/Resolve/Wait` 三 trait 支撑 builder 模式，以及 `unstable`/`internal` feature 门控。
- **u2-l1 打开一个 Session**：知道 `zenoh::open(config)` 返回 `Session`（本质是 `Arc`），克隆便宜、最后一个 drop 时自动关闭。

两个本讲会用到的关键背景：

1. **「委托」（delegation）**：公开 API 往往只是个外壳，真正的活儿交给内部对象去做。`Session` 就是这样的外壳——它持有状态和一把指向 `Runtime` 的句柄，几乎所有公开方法最终都「委托」给 `SessionState` 或 `Runtime`。
2. **trait object（`Arc<dyn Trait>`）**：当一个类型实现了某个 trait，我们就可以用 `Arc<dyn Trait>` 持有它的「动态分发」版本——调用者只知道它「满足这个接口」，不关心具体类型。本讲里 `Arc<dyn Primitives>`、`Arc<dyn IRuntime>` 都是这个套路：用 trait 把公开层和内部层解耦，让两边可以独立演进。

> 术语约定：本讲里「公开 API 层」指 `zenoh/src/api/`，「net 层」指 `zenoh/src/net/`。`api/` 是稳定的，`net/` 是内部实现、不保证稳定。

## 3. 本讲源码地图

本讲涉及的关键文件与各自职责：

| 文件 | 职责 |
|------|------|
| `zenoh/src/net/mod.rs` | net 层的总入口，声明 `primitives` / `routing` / `runtime` 等子模块，是公开 API 与内部实现之间的「分界线」。 |
| `zenoh/src/api/session.rs` | 公开 `Session` 的全部实现：`Session`、`SessionInner`、`SessionState`、`open`、`declare_subscriber` 等。本讲最重的文件。 |
| `zenoh/src/api/builders/subscriber.rs` | `SubscriberBuilder`：`declare_subscriber` 返回的 builder，`.await`/`.wait()` 时真正注册订阅。 |
| `zenoh/src/net/runtime/mod.rs` | `Runtime`、`RuntimeState`、`IRuntime`、`DynamicRuntime`、`GenericRuntime`、`RuntimeBuilder`。会话运行时核心。 |
| `zenoh/src/net/runtime/orchestrator.rs` | `Runtime::start`：根据 `WhatAmI` 角色（client/peer/router）启动 scouting 与建连。 |
| `zenoh/src/net/routing/gateway.rs` | `Gateway::new_session`：为本 Session 在路由表里创建一个 `Face`（消息路由面）。 |
| `zenoh/src/net/routing/dispatcher/face.rs` | `Face` 实现 `Primitives`：把出站 `Declare` 消息分发到正确的下游 face。 |
| `zenoh/src/net/primitives/mod.rs` | `Primitives` / `EPrimitives` trait：API 与网络层之间的抽象接口。 |

## 4. 核心概念与源码讲解

本讲拆三个最小模块：**net 模块入口**、**Session / SessionState**、**Runtime / RuntimeState**。它们正好对应「边界 → 公开层内部 → 运行时核心」三个层次。

### 4.1 net 模块入口：公开 API 与内部实现之间的边界

#### 4.1.1 概念说明

你在前几讲调用 `zenoh::open`、`Session::declare_subscriber`，这些类型都来自 `zenoh/src/api/`。但 Zenoh 真正收发网络消息、维护路由、调度传输的代码，在 `zenoh/src/net/` 里。

`net/mod.rs` 就是这两层之间的「门」：它把内部实现切成几个子模块，并明确标注哪些是内部用的、哪些（很少）允许对外暴露。理解它的 `mod` 声明，等于拿到了进入 Zenoh 内核的目录索引。

#### 4.1.2 核心流程

`net/` 下分五块，本讲先建立全景、后续单元逐个深入：

```
zenoh/src/net/
├── mod.rs          ← 总入口，声明下面 5 个子模块
├── primitives/     ← Primitives/EPrimitives 接口 + Mux/DeMux（u7-l2 详解）
├── routing/        ← 路由核心：Gateway/Face/Tables/HAT（第 8 单元详解）
├── protocol/       ← 路由协议：linkstate/network/gossip（u8-l4 详解）
├── codec/          ← net 层消息编解码（第 10 单元详解）
└── runtime/        ← Runtime 运行时核心（本讲重点）
```

可见性是关键设计：除了 `runtime` 被标成 `pub`（但仍 `#[doc(hidden)]`），其余子模块几乎都是 `pub(crate)`——即「同一个 crate 内可见、对外部用户不可见」。这正是 Zenoh 保护内部实现、只把稳定 API 经 `lib.rs` 暴露的策略的延续。

#### 4.1.3 源码精读

[`zenoh/src/net/mod.rs:20-29`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/mod.rs#L20-L29) 是整个 net 层的总入口：

```rust
//! ⚠️ WARNING ⚠️
//! This module is intended for Zenoh's internal use.
pub(crate) mod codec;
mod common;
pub(crate) mod primitives;
pub(crate) mod protocol;
pub(crate) mod routing;
#[doc(hidden)]
pub mod runtime;
```

解读几个要点：

- 文件顶部的 WARNING 注释（第 15-19 行）开宗明义：net 模块仅供 Zenoh 内部使用，外部应看 `docs.rs/zenoh`。
- `pub(crate) mod primitives / protocol / routing`：这三个核心子模块对 crate 内部公开、对外部用户隐藏，所以普通应用代码看不到也调不到。
- `#[doc(hidden)] pub mod runtime`：`runtime` 是唯一对外 `pub` 的，但 `#[doc(hidden)]` 让它不出现在文档里。它是「内部 + 供插件/binding 使用」的灰色地带（回忆 u1-l4 的 `internal` feature 门控），插件要拿到 `DynamicRuntime` 就靠它。

所以 net 层的边界很清晰：**公开应用代码不该直接碰 `net/`，只有插件（通过 `runtime`）和 Zenoh 自身内部才进入。**

#### 4.1.4 代码实践

**实践目标**：亲手确认 net 层的可见性边界。

**操作步骤**：

1. 打开 `zenoh/src/net/mod.rs`，数一数：`pub(crate)` 的子模块有几个？`#[doc(hidden)] pub` 的有几个？
2. 用全局搜索确认 `primitives`、`routing`、`runtime` 这三个名字，分别是从 `api/session.rs` 的哪些行被引入的（看 `use crate::net::...`）。

**需要观察的现象**：`api/session.rs` 里只有少数几处 `use crate::net::{...}`，说明公开层只「挑着用」net 层的少数类型，而不是整体依赖。

**预期结果**：你会看到 `api/session.rs` 顶部 [`use crate::net::{primitives::Primitives, runtime::{GenericRuntime, RuntimeBuilder}}`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L115-L118) 这样精挑细选的导入——公开层只把 `Primitives`（接口）和 `GenericRuntime`/`RuntimeBuilder`（运行时）这两类东西拿上来用，而不是把整个 `net` 翻出来。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `runtime` 子模块要 `pub`，而 `routing` 要 `pub(crate)`？

**参考答案**：`routing` 是纯内部实现（路由表、HAT 拓扑），外部代码（包括插件）不需要也不应该直接操作它。而 `runtime` 里的 `Runtime`/`DynamicRuntime` 是「插件启动参数」（`PluginStartArgs`），插件必须拿到它才能接入 zenohd 的共享运行时，所以需要对外 `pub`；又因为它是内部细节、不希望出现在公开文档里，所以再加 `#[doc(hidden)]`。

**练习 2**：如果你在应用代码（依赖 `zenoh` crate）里写 `use zenoh::net::routing::gateway::Gateway;`，会发生什么？

**参考答案**：编译失败。`net` 模块在 `lib.rs` 里并没有作为公开 `pub mod` 导出（它内部用了 `pub(crate)`/`#[doc(hidden)]`），`routing` 更是 `pub(crate)`，外部根本访问不到。这正是 Zenoh 的稳定边界在起作用。

---

### 4.2 Session 与 SessionState：声明实体的本地仓库

#### 4.2.1 概念说明

你已经很熟悉 `Session`：它由 `zenoh::open` 返回、可克隆、声明各种实体。但「`Session` 是什么」其实是两层：

- **`Session`** 是一个对外句柄，本质是 `Arc<SessionInner>`。它很轻、克隆便宜，生命周期靠引用计数管理（最后一个 drop 时自动 `close`，见 u2-l1）。
- **`SessionInner`** 才是真正的字段集合：持有指向 `Runtime` 的句柄、一个 `RwLock<SessionState>`、本 Session 的 `id`、任务控制器等。
- **`SessionState`** 是「本会话所有声明实体的本地账本」：所有 subscriber、publisher、queryable、query、本地/远端资源（key expression）都登记在这里。

「委托」在这里很直观：你调 `Session::declare_subscriber`，`Session` 只是把调用转给 `SessionInner`，真正的登记和发消息逻辑都在 `SessionState` 及其方法里。

#### 4.2.2 核心流程

Session 的「一生」可以概括为四步：

```
zenoh::open(config)
   │
   ├─ 1. RuntimeBuilder::new(config).build()   ← 构建 Runtime（见 4.3）
   ├─ 2. Session::init(runtime)                ← 建 SessionInner + SessionState，
   │                                              并向 Runtime 要 primitives（拿到 face_id）
   ├─ 3. runtime.start()                       ← 启动 scouting / 建连
   └─ 4. 返回 Session
```

之后每次 `declare_*` 的通用套路是：

```
Session::declare_xxx(key)            ← 公开入口，返回 Builder
   └─ Builder.wait()/await           ← 真正执行
        └─ Session::declare_xxx_inner(...)
             ├─ SessionState::register_xxx(...)   ← ① 在本地账本登记
             └─ primitives.send_declare(...)      ← ② 委托给 net 层（Face）
```

`primitives` 是什么、从哪来？它在 `Session::init` 时由 `runtime.new_primitives(...)` 创建（见 4.2.3 与 4.3）。它实现了 `Primitives` trait，是公开层通往 net 层的「电话线」。

#### 4.2.3 源码精读

**`Session` 是 `Arc<SessionInner>` 的透明包装**：[`zenoh/src/api/session.rs:744-746`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L744-L746)

```rust
#[derive(Debug)]
#[repr(transparent)]
pub struct Session(Arc<SessionInner>);
```

`#[repr(transparent)]` 说明 `Session` 在内存里就等同于 `Arc<SessionInner>`，没有额外开销。`Clone`/`Drop` 的实现（第 770-785 行）就是「引用计数 +1」和「计数归零时 `close()`」。

**`SessionInner` 的字段**：[`zenoh/src/api/session.rs:679-688`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L679-L688)

```rust
pub(crate) struct SessionInner {
    strong_counter: AtomicUsize,            // 引用计数，用于 Drop 时判断是否最后一个
    runtime: GenericRuntime,                // 指向运行时核心的句柄（委托目标）
    state: RwLock<SessionState>,            // 本地实体账本（读写锁保护）
    id: EntityId,                           // 本 Session 在 Runtime 内的实体 id
    task_controller: TaskController,        // 本 Session 派生的任务管理
    face_id: OnceCell<usize>,               // 在路由表里对应的 face id（只写一次）
    pub(crate) callbacks_drop_sync_group: SyncGroup,
}
```

最关键的两个字段是 `runtime`（委托目标）和 `state`（本地账本）。`face_id` 是「本 Session 在路由层是哪个 face」的唯一编号，由 `init` 写入、之后只读。

**`SessionState` 是本地实体仓库**：[`zenoh/src/api/session.rs:158-182`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L158-L182)

```rust
pub(crate) struct SessionState {
    pub(crate) primitives: Option<Arc<dyn Primitives>>,  // 委托给 net 层的「电话线」
    pub(crate) expr_id_counter: AtomicExprId,            // 本地 key expression 编号
    pub(crate) qid_counter: AtomicRequestId,             // 查询请求编号
    pub(crate) local_resources: IntHashMap<ExprId, LocalResource>,   // 本地声明的 key 资源
    pub(crate) remote_resources: IntHashMap<ExprId, Resource>,       // 对端发来的 key 资源
    pub(crate) remote_subscribers: HashMap<SubscriberId, KeyExpr<'static>>,
    pub(crate) publishers: HashMap<Id, PublisherState>,
    pub(crate) queriers: HashMap<Id, QuerierState>,
    pub(crate) subscribers: HashMap<Id, Arc<SubscriberState>>,       // 本地订阅者账本
    pub(crate) liveliness_subscribers: HashMap<Id, Arc<SubscriberState>>,
    pub(crate) queryables: HashMap<Id, Arc<QueryableState>>,
    // ... queries / matching_listeners / aggregated_* / publisher_qos_tree 等
    span: tracing::span::Span,
}
```

注意 `primitives: Option<Arc<dyn Primitives>>`：它是 `Option`，会话 `close` 时被 `take()` 成 `None`，之后所有操作都会因「primitives 为空」而报 `SessionClosedError`——这就是 `is_closed()` 的判定依据（第 962-964 行：`zread!(self.0.state).primitives.is_none()`）。

**`init` 把三者连起来**：[`zenoh/src/api/session.rs:851-891`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L851-L891)

```rust
pub(crate) fn init(
    runtime: GenericRuntime,
    aggregated_subscribers: Vec<OwnedKeyExpr>,
    aggregated_publishers: Vec<OwnedKeyExpr>,
) -> impl Resolve<Session> {
    ResolveClosure::new(move || {
        let state = RwLock::new(SessionState::new(/* ... */, &runtime));
        let session = Session(Arc::new(SessionInner { /* ... */ }));

        // 注册连通性事件处理器
        runtime.new_handler(Arc::new(connectivity::ConnectivityHandler::new(/* ... */)));

        // 关键：向 Runtime 要一对 (face_id, primitives)
        let (_face_id, primitives) = runtime.new_primitives(Arc::new(session.downgrade()));

        zwrite!(session.0.state).primitives = Some(primitives);   // 存进 SessionState
        session.0.face_id.set(_face_id).unwrap();                 // 存进 SessionInner

        admin::init(session.downgrade());
        session
    })
}
```

注意 `new_primitives` 的参数是 `session.downgrade()`（一个 `WeakSession`）。这一步在公开层和 net 层之间建立了一条**双向链路**：

- 出站：`SessionState.primitives`（即 `Face`）→ net 层 / 网络；
- 入站：net 层收到数据时，通过 `downgrade()` 给的弱引用回调到 `Session`（具体回调实现是 `Session` 对 `Primitives`/`EPrimitives` 的实现，在 u7-l2 详讲）。

而触发这一切的 `Session::new`（即 `open` 的内部实现）：[`zenoh/src/api/session.rs:1429-1456`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L1429-L1456)

```rust
pub(super) fn new(config: Config, /* shm_clients */) -> impl Resolve<ZResult<Session>> {
    ResolveFuture::new(async move {
        let aggregated_subscribers = config.0.aggregation().subscribers().clone();
        let aggregated_publishers  = config.0.aggregation().publishers().clone();
        let mut runtime = RuntimeBuilder::new(config);          // ① 构建 Runtime
        /* ... shm ... */
        let mut runtime = runtime.build().await?;               // ① build

        let session = Self::init(                              // ② 建 Session
            runtime.clone().into(),
            aggregated_subscribers,
            aggregated_publishers,
        ).await;
        runtime.start().await?;                                // ③ 启动建连
        Ok(session)                                            // ④ 返回
    })
}
```

这就是 4.2.2 那张「Session 的一生」流程图的源码对应。

#### 4.2.4 代码实践

**实践目标**：亲眼看到 `primitives` 是 `Option`，并理解它为 `None` 时为何等于「会话已关闭」。

**操作步骤**：

1. 阅读 [`Session::is_closed`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L962-L964)，确认它只判断 `primitives.is_none()`。
2. 阅读 [`WeakSession::close_inner`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L3560-L3561) 的第一行 `let primitives = zwrite!(self.0.state).primitives.take();`——`close` 的第一件事就是把 `primitives` 拿走。
3. 再看 [`SessionState::primitives()`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L239-L251)：任何 `declare_*_inner` 在发消息前都要先拿到 primitives，拿不到就 `Err(SessionClosedError)`。

**需要观察的现象**：`close` 之后，`primitives` 变 `None`；此时再做任何 `declare_publisher`/`put`/`get`，都会在 `state.primitives()?` 这一步提前返回「session closed」错误，根本走不到 net 层。

**预期结果**：你能在脑中确认这条「短路」逻辑——`primitives` 既是通往 net 层的电话线，也是会话是否还活着的开关。

#### 4.2.5 小练习与答案

**练习 1**：`Session` 用了 `#[repr(transparent)]`，去掉它会影响行为吗？

**参考答案**：不影响正确性，但会丢失「`Session` 与 `Arc<SessionInner>` 内存布局完全一致」的保证。保留它，编译器可以更好地优化，也明确表达了「这就是个 Arc 句柄、没有额外字段」的意图。

**练习 2**：为什么 `SessionState` 要放在 `RwLock` 里，而 `primitives` 取出来后却到处用 `Arc` 拷贝？

**参考答案**：`SessionState` 字段众多、读写频繁且需要一致性，所以用一把 `RwLock` 整体保护（读多写少故用 `RwLock` 而非 `Mutex`）。而 `primitives` 是 `Arc<dyn Primitives>`，克隆 `Arc` 极便宜；在持有写锁的短暂窗口里把它 `clone()` 出来（如 [`declare_subscriber_inner` 第 1756 行](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L1756)），之后就可以在不持锁的情况下调用 `primitives.send_declare(...)`，避免「持锁等网络」的死锁风险。

**练习 3**：`face_id` 为什么用 `OnceCell<usize>` 而不是普通 `usize`？

**参考答案**：`OnceCell` 表达「只写一次、之后只读」的语义。`face_id` 在 `init` 里由 `runtime.new_primitives` 返回并写入，之后再也不会变。用 `OnceCell` 既能在构造时延迟赋值（`SessionInner` 的字段初始化时还没有 face_id），又能保证后续读取不会遇到「还没赋值」的中间状态。

---

### 4.3 Runtime 与 RuntimeState：会话运行时核心

#### 4.3.1 概念说明

`Session` 负责登记「本会话的实体」，但 Zenoh 节点要联网，还需要一些更全局的东西：节点身份（`ZenohId`、`WhatAmI`）、传输管理器、路由核心（`Gateway`）、HLC 时钟、任务调度器、scouting/建连编排……这些不属于「某一个会话」，而属于「这个 Zenoh 节点本身」。`Runtime` 就是承载这一切的「节点运行时核心」。

把 `Session` 和 `Runtime` 摆在一起理解：

- **Session = 应用的一次接入 + 它声明的实体账本**；
- **Runtime = 节点本身的网络能力**（身份、传输、路由、时钟）。

文档里有一段说得很清楚（[`session.rs:724-734`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L724-L734)）：通常每个 Session 有自己的 Runtime；但插件场景下，多个 Session 会共享 zenohd 的同一个 Runtime，于是它们拥有相同的 `zid`（网络身份）——这就是为什么「一个 zenohd 里所有插件看到的是同一个节点」。

#### 4.3.2 核心流程

Runtime 这一侧的类型关系（本讲只读、不展开子模块）：

```
            ┌─────────────── Runtime (state: Arc<RuntimeState>) ───────────────┐
            │  pub(crate) 供 zenohd / 内部用                                   │
            │                                                                  │
            │  实现了 IRuntime trait（new_primitives / next_id / zid / ...）   │
            └────────────────────────────┬─────────────────────────────────────┘
                                         │ From<Runtime> for DynamicRuntime
                                         ▼
            ┌─────────────── DynamicRuntime (Arc<dyn IRuntime>) ───────────────┐
            │  「动态分发」版本，作插件的 PluginStartArgs                        │
            └────────────────────────────┬─────────────────────────────────────┘
                                         │ From<DynamicRuntime> for GenericRuntime
                                         ▼
            ┌─────────────── GenericRuntime ───────────────────────────────────┐
            │  dynamic_runtime + 可选 static_runtime                           │
            │  ← SessionInner.runtime 就是它                                    │
            └─────────────────────────────────────────────────────────────────-┘
```

要点：

- `RuntimeState` 是真正的字段集合，`Runtime` 包了一层 `Arc<RuntimeState>`。
- `IRuntime` 是 trait，把 `RuntimeState` 的能力抽象成接口；`DynamicRuntime` 用 `Arc<dyn IRuntime>` 实现动态分发，给插件用（插件不知道具体是静态 Runtime 还是别的实现）。
- `GenericRuntime` 是 `SessionInner` 实际持有的类型，它能装「静态 Runtime」（普通应用、zenohd）或「纯动态 Runtime」（插件拿到的就是动态的）。`Session` 通过 `Deref` 到 `DynamicRuntime` 再到 `IRuntime` 调用方法。

Runtime 的「一生」对应 `RuntimeBuilder`：

```
RuntimeBuilder::new(config)
   └─ .build().await?
        ├─ 解析 zid / whatami / hlc（是否盖时间戳，见 u6-l4）
        ├─ GatewayBuilder::new(&config).build()        ← 路由核心
        ├─ TransportManager::builder()...build()       ← 传输管理器
        └─ 装配出 Runtime { state: Arc<RuntimeState> }
   └─ runtime.start().await?                            ← 按 whatami 跑 scouting/建连
```

#### 4.3.3 源码精读

**`RuntimeState` 的字段**：[`zenoh/src/net/runtime/mod.rs:164-183`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/mod.rs#L164-L183)

```rust
pub(crate) struct RuntimeState {
    zid: ZenohId,                              // 节点身份
    whatami: WhatAmI,                          // router / peer / client
    next_id: AtomicU32,                        // 实体/face 编号分配器
    router: Arc<Gateway>,                      // 路由核心（Gateway）
    config: Notifier<ExpandedConfig>,          // 可通知的配置（运行时可改部分 key）
    manager: TransportManager,                 // 传输管理器（管 unicast/multicast 链路）
    transport_handlers: RwLock<Vec<Arc<dyn TransportEventHandler>>>,
    locators: RwLock<Vec<Locator>>,            // 本节点监听的 locator
    hlc: Option<Arc<HLC>>,                     // 混合逻辑时钟（u6-l4）
    task_controller: TaskController,           // 任务管理（u10-l4）
    #[cfg(feature = "plugins")]
    plugins_manager: Mutex<PluginsManager>,
    start_conditions: Arc<StartConditions>,
    pending_connections: tokio::sync::Mutex<HashSet<ZenohIdProto>>,
    namespace: Option<OwnedNonWildKeyExpr>,
    #[cfg(feature = "stats")]
    stats: zenoh_stats::StatsRegistry,
    span: tracing::Span,
}
```

可以看到 RuntimeState 几乎集成了一个 Zenoh 节点「活下去」需要的所有东西：身份（`zid`/`whatami`）、路由（`router`）、传输（`manager`）、配置（`config`）、时钟（`hlc`）、任务（`task_controller`）、插件（`plugins_manager`）。

**`IRuntime` trait 把能力抽象成接口**：[`zenoh/src/net/runtime/mod.rs:185-224`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/mod.rs#L185-L224)

```rust
#[allow(private_interfaces)]
pub trait IRuntime: Send + Sync {
    fn hlc(&self) -> Option<&HLC>;
    fn zid(&self) -> ZenohId;
    fn whatami(&self) -> WhatAmI;
    fn next_id(&self) -> u32;
    fn is_closed(&self) -> bool;
    fn new_timestamp(&self) -> Option<uhlc::Timestamp>;
    fn get_locators(&self) -> Vec<Locator>;
    fn get_zids(&self, whatami: WhatAmI) -> Box<dyn Iterator<Item = ZenohId> + Send + Sync>;
    fn new_handler(&self, handler: Arc<dyn TransportEventHandler>);
    fn get_transports(&self) -> Box<dyn Iterator<Item = Transport> + Send + Sync>;
    // ...
    fn new_primitives(
        &self,
        e_primitives: Arc<dyn EPrimitives + Send + Sync>,
    ) -> (usize, Arc<dyn Primitives>);
    fn matching_status_remote(/* ... */) -> crate::matching::MatchingStatus;
    fn get_config(&self) -> GenericConfig;
}
```

这个 trait 是「Session 与 Runtime 之间的契约」。注意 `new_primitives`——它接收一个 `EPrimitives`（入站回调，即「数据来了怎么交给 Session」），返回一个 `(usize, Arc<dyn Primitives>)`：那个 `usize` 是 `face_id`，那个 `Primitives` 就是出站「电话线」。这正是 4.2.3 里 `init` 调用的方法。

**`new_primitives` 的实现：在路由表里建 Face**：[`zenoh/src/net/runtime/mod.rs:399-415`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/mod.rs#L399-L415)

```rust
fn new_primitives(
    &self,
    e_primitives: Arc<dyn EPrimitives + Send + Sync>,
) -> (usize, Arc<dyn Primitives>) {
    match &self.namespace {
        Some(ns) => {
            let face = self.router.new_session(Arc::new(ENamespace::new(ns.clone(), e_primitives)));
            (face.state.id, Arc::new(Namespace::new(ns.clone(), face)))
        }
        None => {
            let face = self.router.new_session(e_primitives);   // 关键：在 Gateway 里建一个 Face
            (face.state.id, face)                                // face 本身就实现了 Primitives
        }
    }
}
```

读到这里，4.2 里悬而未决的「`primitives` 从哪来」就有了答案：它就是 `Gateway::new_session` 返回的 `Face`（命名空间场景会包一层 `Namespace`）。`Face` 同时实现了 `Primitives`（出站）和承载 `EPrimitives`（入站回调）。

进入 `Gateway::new_session` 看一眼：[`zenoh/src/net/routing/gateway.rs:220-262`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/gateway.rs#L220-L262)

```rust
pub(crate) fn new_session(&self, primitives: Arc<dyn EPrimitives + Send + Sync>) -> Arc<Face> {
    let ctrl_lock = zlock!(self.tables.ctrl_lock);
    let mut wtables = zwrite!(self.tables.tables);

    let newface = Arc::new(
        FaceStateBuilder::new(
            tables.data.new_face_id(),     // 分配 face id
            tables.data.zid,
            Region::Local,
            Bound::North,
            primitives.clone(),            // 存下入站回调
            tables.hats.map_ref(|hat| hat.new_face()),
        )
        .whatami(WhatAmI::Client)
        .local(true)
        .build(),
    );
    tables.data.faces.insert(newface.id, newface.clone());  // 登记到路由表
    // ... 通知 HAT 有新 local face ...
    Arc::new(face)
}
```

每个公开 Session 在路由表（`Tables`）里都对应一个 `Face`。`Face` 是「消息路由面」——路由层一切转发都以 face 为单位（第 8 单元详解）。

**Runtime 三层包装**：[`Runtime`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/mod.rs#L803-L806) / [`DynamicRuntime`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/mod.rs#L818-L819) / [`GenericRuntime`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/mod.rs#L1252-L1256)

```rust
#[derive(Clone)]
pub struct Runtime { state: Arc<RuntimeState> }            // 静态、强类型

#[derive(Clone)]
pub struct DynamicRuntime(Arc<dyn IRuntime>);              // 动态分发，插件用

#[derive(Clone)]
pub(crate) struct GenericRuntime {
    dynamic_runtime: DynamicRuntime,
    static_runtime: Option<Runtime>,   // 普通应用/zenohd 为 Some；插件为 None
}
```

`GenericRuntime` 的设计很巧妙：它对内可以拿到 `static_runtime()`（仅 zenohd/应用有，用于 `close` 时关闭整个 Runtime，见 [`close_inner` 第 3592-3601 行](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L3592-L3601)）；对外又统一表现为 `DynamicRuntime`，让插件代码无需区分。`From<Runtime> for GenericRuntime`（第 1272-1280 行）在普通应用启动时把静态 Runtime 包成 Generic。

**Runtime 的构造与启动**：[`RuntimeBuilder::new`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/mod.rs#L634-L647) 把 `Config` 转成 `ExpandedConfig`；[`build`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/mod.rs#L675) 里依次构造 zid、whatami、HLC、`Gateway`、`TransportManager`，最终装配 `Runtime`；而 [`Runtime::start`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/orchestrator.rs#L140-L146) 按 whatami 分派到 `start_client`/`start_peer`/`start_router`：

```rust
pub async fn start(&mut self) -> ZResult<()> {
    match self.whatami() {
        WhatAmI::Client => self.start_client().await,
        WhatAmI::Peer => self.start_peer().await,
        WhatAmI::Router => self.start_router().await,
    }
}
```

这些 `start_*` 方法读 `listen`/`connect`/`scouting` 配置，绑定监听端口、发起连接、必要时启动 multicast scouting（细节留给 u7-l3）。

#### 4.3.4 代码实践

**实践目标**：在源码里确认「普通应用的 Session 持有的是静态 Runtime，插件持有的是动态 Runtime」。

**操作步骤**：

1. 打开 [`GenericRuntime`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/mod.rs#L1252-L1289)，读 `From<Runtime> for GenericRuntime`（第 1272 行）：它同时存了 `dynamic_runtime` 和 `static_runtime: Some(...)`。
2. 再读 `From<DynamicRuntime> for GenericRuntime`（第 1282 行）：它只存 `dynamic_runtime`，`static_runtime` 为 `None`。
3. 在 [`WeakSession::close_inner`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L3592-L3601) 里看 `if let Some(r) = self.0.runtime.static_runtime()` 分支，注释明确写着「session created by plugins never have a copy of static_runtime, so the code below will run only inside zenohd」。

**需要观察的现象**：只有持有静态 Runtime 的 Session（zenohd / 普通应用）会走到「关闭整个 Runtime」的分支；插件 Session 没有静态 Runtime，`close` 时只 `primitives.send_close()` 而不会关掉共享的 zenohd Runtime。

**预期结果**：你能解释「为什么一个插件的 Session 关闭不会拖垮 zenohd」——因为它拿不到、也关不掉共享的静态 Runtime。**待本地验证**：可阅读 `plugins/zenoh-plugin-example` 确认插件入口收到的 `DynamicRuntime` 不含 `static_runtime`（因 `GenericRuntime` 字段是 `pub(crate)`，外部代码拿不到 `static_runtime()`，天然无法关闭）。

#### 4.3.5 小练习与答案

**练习 1**：`SessionInner.runtime` 的类型是 `GenericRuntime` 而不是 `Runtime`，这样做有什么好处？

**参考答案**：`GenericRuntime` 同时支持「自带静态 Runtime 的应用」（static_runtime = Some）和「借用 zenohd 运行时的插件」（static_runtime = None）两种场景，用同一个字段类型统一表达。它通过 `Deref` 到 `DynamicRuntime` 再到 `Arc<dyn IRuntime>`，使得两路代码在调用 `IRuntime` 方法时完全一致；仅在需要区分（如 close）时才用 `static_runtime()` 取值。

**练习 2**：`new_primitives` 返回的「电话线」具体是哪个类型？它住在哪里？

**参考答案**：是 `Gateway::new_session` 返回的 `Face`（命名空间场景包一层 `Namespace`），它实现了 `Primitives` trait。它住在路由层的 `Tables` 里（`tables.data.faces` 这个 map 以 `face_id` 为键），所以公开 Session 的每一次出站声明，最终都落到路由表里的一个 face 上。

**练习 3**：为什么 `RuntimeState` 里 `hlc` 是 `Option<Arc<HLC>>` 而不是直接 `HLC`？

**参考答案**：是否启用时间戳取决于配置 `timestamping.enabled`，且默认 client/peer 不盖时间戳（见 u6-l4）。`build` 时按配置决定要不要建 HLC，没有就 `None`。用 `Option` 表达「可能没有时钟」；用 `Arc` 是因为多处（盖戳、吸收对端时间戳）要共享同一个 HLC。

---

## 5. 综合实践：追踪一条 `declare_subscriber` 的下沉路径

本任务把三个最小模块串起来，完整走一遍「公开 API → net 层」的调用链。这是本讲的核心实践，请边读源码边画图。

**实践目标**：用一段中文文字（或一张标注了函数名、文件、行号的流程图）描述：调用 `session.declare_subscriber("a/b").await.unwrap()` 后，本地的订阅是如何被登记、又是如何「注册到网络」的。

**操作步骤**（请逐站打开对应源码）：

1. **公开入口**：[`Session::declare_subscriber`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L1108-L1122)。

   它什么「实事」都没做，只是把 `key_expr` 解析成 `KeyExpr`，连同默认 `origin`、默认 handler 包成一个 `SubscriberBuilder` 返回。**关键认知：builder 模式把「构造」和「执行」分离，真正执行发生在 `.await`/`.wait()`。**

2. **Builder 真正执行**：打开 `zenoh/src/api/builders/subscriber.rs` 的 [`SubscriberBuilder::wait`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/subscriber.rs#L213-L237)。

   它做三件事：① `declare_nonwild_prefix`（把 key 的非通配前缀登记成资源，省带宽）；② `handler.into_handler()` 把取数姿势（callback/channel）拆成 `(Callback, Handler)`；③ 调 `session.declare_subscriber_inner(...)`。注意它把 `session` downgrade 成弱引用存进返回的 `Subscriber`。

3. **本地账本登记 + 委托 net 层**：[`Session::declare_subscriber_inner`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L1747-L1777)。

   ```rust
   let mut state = zwrite!(self.0.state);
   let primitives = state.primitives()?;                        // 取出「电话线」（可能是 None→关闭错误）
   let id = self.0.runtime.next_id();                           // 向 Runtime 要一个实体 id
   let (sub_state, declared_sub) =
       state.register_subscriber(id, key_expr, origin, callback);  // ① 写本地账本
   if let Some(key_expr) = declared_sub {
       drop(state);                                             // 先放锁
       let wire_expr = key_expr.to_wire(self).to_owned();
       primitives.send_declare(&mut Declare {                   // ② 委托给 net 层
           // ...
           body: DeclareBody::DeclareSubscriber(DeclareSubscriber { id, wire_expr }),
       });
       // ...
   }
   Ok(sub_state)
   ```

   这里出现两个关键判断：
   - `register_subscriber`（[`第 393-474 行`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L393-L474)）会判断该 key 是否需要真正向网络声明（例如已被 `aggregated_subscribers` 聚合、或已有相同 key 的「孪生订阅」可复用），返回 `Option<KeyExpr>`：`Some` 表示要发声明、`None` 表示本地登记即可。这是 Zenoh 省「声明消息」的一种优化。
   - 只有 `origin != SessionLocal` 且确实需要声明时，才会 `primitives.send_declare(...)`。

4. **「电话线」接到了哪**：回忆 4.3.3，`primitives` 就是 `init` 时 `runtime.new_primitives(...)` 返回的 `Face`。看 [`Face::send_declare`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/face.rs#L559-L581)：

   ```rust
   fn send_declare(&self, msg: &mut zenoh_protocol::network::Declare) {
       let ctrl_lock = zlock!(self.tables.ctrl_lock);
       match &mut msg.body {
           // ...
           zenoh_protocol::network::DeclareBody::DeclareSubscriber(m) => {
               let mut declares = vec![];
               self.declare_subscriber(
                   m.id, &m.wire_expr, &SubscriberInfo, msg.ext_nodeid.node_id,
                   &mut |p, m| declares.push((p.clone(), m)),   // 收集「要转发给哪些下游 face」
               );
               drop(ctrl_lock);
               for (p, m) in declares {
                   m.with_mut(|m| p.send_declare(m));            // 实际转发
               }
           }
           // ...
       }
   }
   ```

   **这是路由层的核心动作**：`Face::send_declare` 不直接把声明塞进网络，而是先在路由表里计算「这个新订阅应该让哪些下游 face 知道」，收集成 `declares`，再逐个 `p.send_declare(m)` 转发。下游 face 才是真正连着传输链路的（u7-l2 / 第 8 单元详解）。

**需要观察的现象**：调用链清晰地分成「公开层登记（api/session.rs）→ 跨界委托（primitives.send_declare）→ 路由层计算与转发（dispatcher/face.rs）」三段。公开层完全不知道「下游有几个 face、用什么链路」，这些都被 `Primitives` trait 封装在 net 层内部。

**预期结果**：你能写出类似下面这段文字描述（这是本实践要求交付的产物）：

> `session.declare_subscriber("a/b").await` 触发 `SubscriberBuilder::wait`，它调用 `Session::declare_subscriber_inner`（session.rs:1747）。该方法先在 `SessionState::register_subscriber`（session.rs:393）里把订阅写进 `SessionState.subscribers` 账本并挂到相交的本地/远端资源上；若需要向网络声明，则取出 `SessionState.primitives`（即 `init` 时由 `runtime.new_primitives` 返回的 `Face`），调用 `primitives.send_declare(DeclareSubscriber)`（session.rs:1762）。该 `Face` 实现了 `Primitives`（dispatcher/face.rs:559），它不直接发网络，而是在路由表 `Tables` 里计算下游 face，再逐个转发 `p.send_declare(m)`，最终由传输层真正发出去。整条链路里，公开层只通过 `Arc<dyn Primitives>` 这条「电话线」与 net 层解耦。

**待本地验证**：可选地，在 `declare_subscriber_inner` 的 `tracing::trace!("declare_subscriber({:?})", key_expr)`（第 1754 行）处用 `RUST_LOG=zenoh::api::session=trace` 观察一次订阅的实际日志；在 `Face::send_declare` 加一行临时 `trace!` 可看到「转发了 N 条下游声明」。观察后请移除调试日志，不要提交对源码的改动。

## 6. 本讲小结

- **net 层是内部实现的总称**：`net/mod.rs` 是公开 API 与内部实现之间的门，除 `runtime`（`#[doc(hidden)] pub`）外，`primitives`/`routing`/`protocol`/`codec` 都是 `pub(crate)`，外部应用不该也不易触碰。
- **`Session` 是轻量句柄，`SessionState` 是本地账本**：`Session = Arc<SessionInner>`，`SessionInner` 持有 `runtime`（委托目标）与 `RwLock<SessionState>`；`SessionState` 登记本会话所有 subscriber/publisher/queryable/resource，并用 `Option<Arc<dyn Primitives>>` 这条「电话线」通往 net 层、兼作会话存活开关。
- **`Session` 的诞生四步**：`RuntimeBuilder::build()` → `Session::init(runtime)`（含 `runtime.new_primitives` 拿到 face_id 与 primitives）→ `runtime.start()`（按 whatami 建连）→ 返回 `Session`。
- **`Runtime` 是节点运行时核心**：`RuntimeState` 集成了 zid/whatami/Gateway/TransportManager/config/HLC/task_controller 等；经 `IRuntime` trait 抽象，再由 `Runtime → DynamicRuntime → GenericRuntime` 三层包装，统一服务应用与插件两种接入方式。
- **公开 API 与 net 层的边界是 `Primitives` trait**：`new_primitives` 返回的 `Face` 同时是出站 `Primitives` 和入站 `EPrimitives` 的载体；公开层只认 `Arc<dyn Primitives>`，不关心其背后是路由表里的哪个 face、走哪条链路。
- **声明实体的下沉路径**：`declare_subscriber` → `SubscriberBuilder::wait` → `declare_subscriber_inner`（`SessionState::register_subscriber` 本地登记 + `primitives.send_declare`）→ `Face::send_declare`（路由表计算下游 face 并转发）→ 传输层。

## 7. 下一步学习建议

本讲把公开 `Session` 下钻到了 `Runtime` 与路由层的 `Face` 边缘，但有两个关键环节被故意留白：

- **`Primitives` / `EPrimitives` / `Mux` / `DeMux`**：本讲里 `Face` 只是「电话线」的一端，入站数据怎么从网络回到 `Session`、出站消息怎么多路复用上链路，要看 `net/primitives/`。→ 下一讲 **u7-l2《Primitives 与 Mux/DeMux》**。
- **`Runtime::start` 与建连细节**：本讲只点到「按 whatami 分派到 `start_client/peer/router`」，scouting 周期、PeerConnector、AutoConnect 等留给 **u7-l3《Runtime 编排器：scouting 与建连》**。

若你对路由层的 `Face`/`Tables` 更感兴趣，可以直接跳到第 8 单元 **u8-l1《路由骨架：Gateway / Face / Tables》**，但建议先完成 u7-l2，把出入站全链路补齐再深入路由。
