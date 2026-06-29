# Session 内部与 Runtime

> 本讲是「用法 → 原理」的过渡站。前面六个单元我们一直在用 `zenoh::open(...)`、`declare_subscriber(...)` 这些公开 API，本讲第一次把它们「翻过来」，看一眼 API 背后的内部结构：`Session` 里到底装了什么、它是怎么委托给 `net` 内部层的、而 `Runtime` 又是怎样承载真正网络连接状态的。

## 1. 本讲目标

学完本讲，你应该能够：

- 说出公开 `Session` 与内部 `SessionInner` / `SessionState` 三者的关系，以及 `Session` 为什么可以廉价克隆。
- 描述 `zenoh::open(config)` 从「拿到 Config」到「会话可用」中间经历的 `RuntimeBuilder::build → Session::init → Runtime::start` 三大步骤。
- 理解 `Runtime` / `RuntimeState` 作为「会话运行时核心」持有哪些重量级组件（`TransportManager`、`Gateway` 路由器、`HLC`、`TaskController`、`config`）。
- 看懂 `net` 模块入口及其稳定边界，知道 `Primitives` trait 是 API 层与网络/路由层之间的「契约接口」。
- 用自己的话追踪一条 `declare_subscriber` 调用链，说明它最终如何通过 `Primitives::send_declare` 把声明交给内部路由器，再由 `Gateway` 创建的 `Face` 转发到网络。

## 2. 前置知识

在进入源码之前，先用大白话建立三个直觉。它们是本讲的认知地基。

### 2.1 句柄（handle）与状态（state）分离

Zenoh 里很多公开类型都是「很薄的句柄」。所谓句柄，本质是一个指向真正状态的引用。比如 `Session` 其实就是一个 `Arc<SessionInner>`——它自己几乎不存数据，真正的数据都在 `SessionInner` 里。这样设计的好处是：克隆句柄很便宜（只是把引用计数 +1），多个线程可以各拿一份句柄共享同一份状态。这和《u2-l1 打开一个 Session》里讲的「Session 是可克隆的 Arc 句柄」完全对应，本讲我们要看清这个 Arc 里装的是什么。

### 2.2「声明（declare）」是 Zenoh 的核心动词

你在前面几讲反复看到 `declare_subscriber`、`declare_publisher`、`declare_queryable`、`declare_token`。在 Zenoh 的协议里，「声明」不仅仅是本地记一笔，它还会生成一条 **Declare** 网络消息，告诉路由器「我这边存在一个对 X 感兴趣的实体」。路由器据此决定后续数据该往哪些方向转发。所以「声明」是连接「应用本地状态」与「网络路由状态」的桥梁——本讲要追踪的正是这座桥。

### 2.3 trait object 是 Rust 里的「接口」

你会在源码里频繁看到 `Arc<dyn Primitives>` 这种写法。`Primitives` 是一个 trait（类似其他语言里的 interface），`dyn Primitives` 表示「某个实现了 Primitives 的具体类型，但具体是哪个我不关心」。`Arc<dyn Primitives>` 就是一个指向「某个 Primitives 实现」的线程安全引用。API 层只认 `Primitives` 这个接口，至于底层是真正的 `Face` 还是测试用的 `DummyPrimitives`，API 层不关心。这种「面向接口编程」正是 API 与内核解耦的关键。

> 名词速查：`Arc`（原子引用计数，跨线程共享所有权）、`RwLock`（读写锁，允许多读单写）、`HLC`（混合逻辑时钟，见《u6-l4 Timestamp》）、`Gateway`（路由网关，本讲先当黑盒，细节在《u8 路由》单元）。

## 3. 本讲源码地图

本讲涉及的关键文件，按「从外到内」的顺序：

| 文件 | 作用 |
| --- | --- |
| [`zenoh/src/api/session.rs`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs) | 公开 `Session` 的全部实现：`Session`/`SessionInner`/`SessionState` 三件套、`open`、`init`、`declare_subscriber` 及其 `inner` 版本。本讲的「主角」。 |
| [`zenoh/src/api/builders/session.rs`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/session.rs) | `OpenBuilder`：`zenoh::open` 返回的 builder，resolve 时调用 `Session::new`。还有供插件复用 Runtime 的 `init`/`InitBuilder`。 |
| [`zenoh/src/net/mod.rs`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/mod.rs) | 内部 `net` 模块的入口，声明 `codec/primitives/protocol/routing/runtime` 子模块。是「稳定 API」与「内部实现」的分界线。 |
| [`zenoh/src/net/primitives/mod.rs`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/primitives/mod.rs) | 定义 `Primitives` / `EPrimitives` 两个 trait——API 与网络层之间的契约接口。 |
| [`zenoh/src/net/runtime/mod.rs`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/mod.rs) | `Runtime`/`RuntimeState`/`RuntimeBuilder` 的定义：会话运行时核心，持有传输管理器、路由器、HLC、任务控制器等。 |
| [`zenoh/src/net/routing/dispatcher/face.rs`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/face.rs) | `Face`：实现了 `Primitives` 的「会话面」，是把 API 出站消息送进路由器的入口。 |
| [`zenoh/src/net/routing/gateway.rs`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/gateway.rs) | `Gateway`：路由网关，`new_session` 为每个 Session 创建一个 `Face`。 |
| [`zenoh/src/net/runtime/orchestrator.rs`](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/orchestrator.rs) | `Runtime::start`：按 client/peer/router 角色执行建连、scouting 的编排逻辑（细节留到《u7-l3》）。 |

记忆口诀：**`Session`（公开句柄）→ `SessionInner`（持有 Runtime 与 SessionState）→ `Primitives`（契约接口）→ `Face`（实现）→ `Gateway`（路由器）**。这条链就是本讲的全部主线。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 Session 与 SessionState**——公开会话内部装了什么。
- **4.2 net 模块入口与 Primitives 契约**——API 与内部之间的分界线长什么样。
- **4.3 Runtime 与 RuntimeState**——真正承载连接状态的核心。

---

### 4.1 Session 与 SessionState：公开会话的内部结构

#### 4.1.1 概念说明

你在《u2-l1》已经会用 `zenoh::open(config).await` 拿到一个 `Session`，并在上面 `declare_subscriber`。但 `Session` 本身只是一个**透明包装**（`#[repr(transparent)]`），它内部只有一个字段：一个指向 `SessionInner` 的 `Arc`。

为什么要套两层？因为 Zenoh 要同时满足两个需求：

1. **句柄要能廉价克隆、跨线程共享**——所以用 `Arc`。
2. **最后一个句柄被 drop 时要自动关闭会话**——所以 `Session` 自定义了 `Clone` 和 `Drop`，用一个引用计数器 `strong_counter` 来判断「我是不是最后一个」。

`SessionInner` 又分两块：一块是「与 Runtime 的连接」（`runtime`、`face_id`），另一块是「本会话自己声明的所有实体」（`state: RwLock<SessionState>`）。`SessionState` 就是那个装满了 publishers / subscribers / queryables / resources 等映射表的大仓库——你在前面几讲声明的每一个实体，最终都登记在这里。

#### 4.1.2 核心流程

一次 `zenoh::open(config)` 的内部生命周期可以画成下面这条线（本模块只关注「构造 Session」这一段）：

```
zenoh::open(config)              // 返回 OpenBuilder（什么都没做）
        │  .await / .wait()
        ▼
OpenBuilder::wait
        │
        ▼
Session::new(config)             // 异步
        │
        ├──► RuntimeBuilder::new(config).build().await   // ① 造 Runtime（见 4.3）
        ├──► Session::init(runtime, ...)                 // ② 造 Session 并连上 Runtime（本节重点）
        └──► runtime.start().await                       // ③ 开始建连/scouting（见 4.3 / 《u7-l3》）
        │
        ▼
   返回 Session(Arc<SessionInner>)
```

关键在第 ② 步 `Session::init`：它做四件事——

1. 从 Runtime 的 config 读出 QoS 配置树，新建一个空的 `SessionState`。
2. 用 `Arc::new(SessionInner { ... })` 组装出 `Session`。
3. 向 Runtime 注册一个「连接性处理器」（`ConnectivityHandler`），让 Runtime 在传输事件发生时能回调本会话。
4. **最关键的一步**：调用 `runtime.new_primitives(...)`，拿到一个 `Arc<dyn Primitives>`，把它塞进 `SessionState.primitives`。从这一刻起，本会话就有了「向网络发消息」的出口。

#### 4.1.3 源码精读

**`Session` 的真身：一行 `Arc`**

[zenoh/src/api/session.rs:744-746](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L744-L746) —— 注意 `#[repr(transparent)]`，说明 `Session` 在内存里和 `Arc<SessionInner>` 完全等价，没有任何额外开销：

```rust
#[derive(Debug)]
#[repr(transparent)]
pub struct Session(Arc<SessionInner>);
```

**`SessionInner` 的字段**——这就是「公开会话」的全部内部状态：

[zenoh/src/api/session.rs:679-688](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L679-L688)

```rust
pub(crate) struct SessionInner {
    strong_counter: AtomicUsize,   // 自管理的引用计数，配合下面的 Clone/Drop
    runtime: GenericRuntime,        // 指向 Runtime（真正干活的人）
    state: RwLock<SessionState>,    // 本会话声明的所有实体
    id: EntityId,
    task_controller: TaskController,
    face_id: OnceCell<usize>,       // 在 Runtime 路由器里的「面孔 ID」
    pub(crate) callbacks_drop_sync_group: SyncGroup,
}
```

**自定义 `Clone` / `Drop` 实现自动关闭**——克隆时把计数 +1，drop 时若计数归 1（即自己是最后一个）就调用 `close()`：

[zenoh/src/api/session.rs:770-785](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L770-L785)

```rust
impl Clone for Session {
    fn clone(&self) -> Self {
        self.0.strong_counter.fetch_add(1, Ordering::Relaxed);
        Self(self.0.clone())
    }
}
impl Drop for Session {
    fn drop(&mut self) {
        if self.0.strong_counter.fetch_sub(1, Ordering::Relaxed) == 1 {
            if let Err(error) = self.close().wait() { tracing::error!(error) }
        }
    }
}
```

> 为什么不直接用 `Arc` 的 `strong_count`？源码注释里提到一个微妙原因：`WeakSession` 需要在会话内部建立「引用环」（primitive 实现会回指 session），而标准 `Weak` 在关闭流程里会引发问题，所以 Zenoh 自管计数 + `WeakSession`（`ManuallyDrop` 包裹）来打破环。初学不必深究，知道「最后一个 drop 自动 close」即可。

**`SessionState`——实体大仓库**：

[zenoh/src/api/session.rs:158-182](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L158-L182)

```rust
pub(crate) struct SessionState {
    pub(crate) primitives: Option<Arc<dyn Primitives>>,   // ★ 出站契约接口
    pub(crate) expr_id_counter: AtomicExprId,
    pub(crate) qid_counter: AtomicRequestId,
    pub(crate) local_resources: IntHashMap<ExprId, LocalResource>,
    pub(crate) remote_resources: IntHashMap<ExprId, Resource>,
    pub(crate) remote_subscribers: HashMap<SubscriberId, KeyExpr<'static>>,
    pub(crate) publishers: HashMap<Id, PublisherState>,
    pub(crate) queriers: HashMap<Id, QuerierState>,
    pub(crate) remote_tokens: HashMap<TokenId, KeyExpr<'static>>,
    pub(crate) subscribers: HashMap<Id, Arc<SubscriberState>>,
    pub(crate) liveliness_subscribers: HashMap<Id, Arc<SubscriberState>>,
    pub(crate) queryables: HashMap<Id, Arc<QueryableState>>,
    // ... 还有 queries / matching_listeners / QoS 树等
    span: tracing::span::Span,
}
```

注意第一行 `primitives: Option<Arc<dyn Primitives>>`——这就是本会话「向网络发声」的出口。`Option` 是因为：会话关闭时（`close_inner`）会把它 `take()` 走，之后任何声明都会因找不到 primitives 而报 `SessionClosedError`。访问它的统一入口是：

[zenoh/src/api/session.rs:240-251](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L240-L251)

```rust
pub(crate) fn primitives(&self) -> ZResult<SpannedPrimitives> {
    let primitives = self.primitives.as_ref().cloned().ok_or(SessionClosedError)?;
    Ok(SpannedPrimitives { inner: primitives, _span: self.span.clone().entered() })
}
```

**`Session::init`——把 Session 与 Runtime 缝合起来的关键函数**：

[zenoh/src/api/session.rs:851-891](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L851-L891)

```rust
pub(crate) fn init(
    runtime: GenericRuntime,
    aggregated_subscribers: Vec<OwnedKeyExpr>,
    aggregated_publishers: Vec<OwnedKeyExpr>,
) -> impl Resolve<Session> {
    ResolveClosure::new(move || {
        let publisher_qos = runtime.get_config().get_typed::<PublisherQoSConfList>("qos/publication").unwrap();
        let state = RwLock::new(SessionState::new(
            aggregated_subscribers, aggregated_publishers, publisher_qos.into(), &runtime,
        ));
        let session = Session(Arc::new(SessionInner {
            strong_counter: AtomicUsize::new(1),
            runtime: runtime.clone(),
            state,
            id: runtime.next_id(),
            task_controller: TaskController::default(),
            face_id: OnceCell::new(),
            callbacks_drop_sync_group: SyncGroup::default(),
        }));
        // 注册连接性处理器
        runtime.new_handler(Arc::new(connectivity::ConnectivityHandler::new(session.downgrade())));
        // ★ 拿到出站 primitives，塞进 state
        let (_face_id, primitives) = runtime.new_primitives(Arc::new(session.downgrade()));
        zwrite!(session.0.state).primitives = Some(primitives);
        session.0.face_id.set(_face_id).unwrap();
        admin::init(session.downgrade());
        session
    })
}
```

把这段读穿，你就理解了「Session 怎么连上 Runtime」：`init` 收到一个 `GenericRuntime`，用它造 `SessionState` 和 `SessionInner`，再调 `runtime.new_primitives(...)` 拿到出站接口。`new_primitives` 返回的 `_face_id` 就是本会话在路由器里的面孔编号，存进 `face_id` 备用。

而 `init` 的调用方是 `Session::new`，它才是 `open` 真正的执行者：

[zenoh/src/api/session.rs:1431-1456](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L1431-L1456)

```rust
pub(super) fn new(config: Config, ...) -> impl Resolve<ZResult<Session>> {
    ResolveFuture::new(async move {
        let aggregated_subscribers = config.0.aggregation().subscribers().clone();
        let aggregated_publishers = config.0.aggregation().publishers().clone();
        let mut runtime = RuntimeBuilder::new(config);       // ① 准备 Runtime builder
        // ...
        let mut runtime = runtime.build().await?;            // ② 造出 Runtime
        let session = Self::init(                            // ③ 用 Runtime 造 Session
            runtime.clone().into(), aggregated_subscribers, aggregated_publishers,
        ).await;
        runtime.start().await?;                              // ④ 开始建连
        Ok(session)
    })
}
```

四个步骤清晰可数：build Runtime → init Session → start Runtime。顺序很重要：**必须先 init（把 primitives 接好）再 start（开始收发）**，否则网络消息到达时还没有人处理。

#### 4.1.4 代码实践

**实践目标**：用源码阅读的方式，确认「`Session` 是 `Arc<SessionInner>` 的薄壳」以及「最后一个 clone drop 会自动 close」。

**操作步骤**：

1. 打开 [zenoh/src/api/session.rs:746](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L746)，确认 `pub struct Session(Arc<SessionInner>);`。
2. 跳到 [zenoh/src/api/session.rs:770](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L770) 看 `Clone`，再到 :777 看 `Drop`。
3. 写一个最小示例（**示例代码，非项目原文件**）验证克隆与自动关闭：

```rust
// 示例代码：演示 Session 的 Arc 语义
#[tokio::main]
async fn main() {
    let session = zenoh::open(zenoh::Config::default()).await.unwrap();
    let session2 = session.clone();   // 克隆只是 strong_counter += 1，不重新建连
    assert_eq!(session.zid(), session2.zid());  // 两个句柄指向同一个 SessionInner
    drop(session);                    // 不会关闭，因为还有 session2
    println!("session2 仍可用，zid = {}", session2.zid());
    drop(session2);                   // 最后一个，触发 close()
    println!("会话已关闭");
}
```

**需要观察的现象**：`drop(session)` 之后 `session2` 仍能正常调用 `zid()`，说明克隆共享同一份 `SessionInner`；只有最后一个 drop 才会真正关闭。

**预期结果**：程序依次打印三个 zid（其中两个相同）和「会话已关闭」，中间不会 panic。运行命令为 `cargo run --example <你的示例名>`（需把上面的代码放进 `examples/` 目录，或在测试里验证；若不便编译，本实践可改为纯阅读型——直接在源码里确认 `Drop::drop` 的 `fetch_sub(...) == 1` 判断即可）。

#### 4.1.5 小练习与答案

**练习 1**：`SessionState.primitives` 为什么是 `Option<Arc<dyn Primitives>>` 而不是直接 `Arc<dyn Primitives>`？

> **参考答案**：因为会话关闭时（`close_inner`）需要把这个出口「拿走」（`primitives.take()`），让后续所有声明/发送操作拿不到 primitives、从而返回 `SessionClosedError`。用 `Option` 才能表达「可能已经被取空」这一状态，强制调用方处理关闭态。

**练习 2**：为什么 `Session::new` 里必须 `init` 之后才 `runtime.start()`，不能反过来？

> **参考答案**：`start()` 会开始建连、scouting，一旦链路建好就可能收到对端发来的网络消息，这些消息要由 `DeMux` 经路由器派发到 Session。如果还没 `init`，Session 的 primitives / face 还没就绪，入站消息就无人处理。所以「先接线、再通电」。

---

### 4.2 net 模块入口与 Primitives 契约：API 与内核的分界线

#### 4.2.1 概念说明

回忆《u1-l4》讲过：`zenoh/src/lib.rs` 是公开 API 门面，它把内部 `api/` 与 `net/` 里的类型挑挑拣拣后 re-export 给用户。本模块我们就站在这条分界线上，看一眼 `net/` 模块的入口长什么样，以及最重要的「契约接口」——`Primitives` trait。

为什么需要一个契约 trait？因为 API 层（`api/session.rs`）想发消息时，它**不应该、也不需要**知道底层是 Face、是 Namespace 包装、还是测试桩。它只需要知道「我有一个实现了 `send_declare / send_push / send_request ...` 的东西」。`Primitives` 就是这份合同。这种设计让 API 与路由/传输实现彻底解耦：你完全可以换掉底层路由器，只要它实现了 `Primitives`，API 层一行都不用改。

`net/mod.rs` 顶部的醒目警告也印证了这条边界：

> ⚠️ WARNING ⚠️ This module is intended for Zenoh's internal use.

#### 4.2.2 核心流程

`net` 模块下挂着五个子模块，各司其职：

```
net/
├── codec/       // 协议编解码（《u10-l2》）
├── common/
├── primitives/  // Primitives/EPrimitives 契约 + Mux(出站)/DeMux(入站)（《u7-l2》详讲）
├── protocol/    // 路由协议：linkstate/network/gossip（《u8-l4》）
├── routing/     // 路由核心：Gateway/Face/HAT/dispatcher（《u8》整单元）
└── runtime/     // Runtime 运行时核心（本讲 4.3）
```

API 层与 `net` 层之间的「双向数据流」可以用这张图概括（细节在《u7-l2 Primitives 与 Mux/DeMux》展开）：

```
        出站（应用 → 网络）                      入站（网络 → 应用）
   ┌─────────────────────────┐            ┌─────────────────────────┐
   │  Session.put/declare... │            │  传输层收到一帧          │
   │          │              │            │          │              │
   │          ▼              │            │          ▼              │
   │  state.primitives       │            │  DeMux.handle_message   │
   │  .send_declare(...)     │            │  (按消息类型分派)        │
   │          │              │            │          │              │
   │          ▼              │            │          ▼              │
   │  Face(实现 Primitives)  │            │  Face → Gateway 路由    │
   │  → Gateway 路由表        │            │  → 回调到 Session       │
   │  → Transport 发送        │            │                          │
   └─────────────────────────┘            └─────────────────────────┘
```

本模块我们只盯住「出站契约」这一头：`Primitives` trait。

#### 4.2.3 源码精读

**`net/mod.rs`——分界线本身**：

[zenoh/src/net/mod.rs:15-26](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/mod.rs#L15-L26)

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

注意可见性差异：`codec/primitives/protocol/routing` 都是 `pub(crate)`（只在 zenoh crate 内可见，外部用户摸不到）；唯独 `runtime` 是 `pub mod`（外加 `#[doc(hidden)]`），因为插件需要通过 `DynamicRuntime` 与它交互（见《u11-l2 插件系统》）。这条可见性差别本身就是「稳定边界」的体现。

**`Primitives` trait——出站契约**：

[zenoh/src/net/primitives/mod.rs:28-50](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/primitives/mod.rs#L28-L50)

```rust
pub trait Primitives: Send + Sync {
    fn send_interest(&self, msg: &mut Interest);
    fn send_declare(&self, msg: &mut Declare);
    fn send_push_consume(&self, msg: &mut Push, reliability: Reliability, consume: bool);
    #[inline(always)]
    fn send_push(&self, msg: &mut Push, reliability: Reliability) {
        self.send_push_consume(msg, reliability, true)
    }
    fn send_request(&self, msg: &mut Request);
    fn send_response(&self, msg: &mut Response);
    fn send_response_final(&self, msg: &mut ResponseFinal);
    fn send_close(&self);
    fn as_any(&self) -> &dyn Any;
}
```

这一个 trait 涵盖了 Zenoh 全部出站网络动作：声明（declare）、推送数据（push）、查询请求/应答（request/response）、兴趣声明（interest）、关闭（close）。你在 API 层做的一切——`put`、`get`、`declare_subscriber`、`declare_token`——最终都会归约到这几个方法的一次调用。`send_push` 是 `send_push_consume` 的默认包装，体现了 trait 的默认方法用法。

**`EPrimitives`——带路由上下文的「内核侧」契约**：紧随其后还有一个 trait，方法签名带 `RoutingContext`，专供路由器内部使用，返回 `bool` 表示「这条消息是否还有下游需要继续转发」。API 层用不到它，本讲只需知道它存在：

[zenoh/src/net/primitives/mod.rs:52-66](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/primitives/mod.rs#L52-L66)

**`DummyPrimitives`——测试/空实现**：同一个文件里还提供了一个「全空」实现，所有方法什么都不做。它的意义是：在没有真实网络时（比如单元测试、或会话尚未连上）提供一个不会出错的占位实现，这正是「面向接口」带来的可替换性：

[zenoh/src/net/primitives/mod.rs:68-89](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/primitives/mod.rs#L68-L89)

**谁真正实现了 `Primitives`？**——是 `Face`（路由器为每个 Session 创建的「面孔」）。下面是 `Face::send_declare` 的开头，它就是 API 出站声明的真正落点：

[zenoh/src/net/routing/dispatcher/face.rs:559-581](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/face.rs#L559-L581)

```rust
fn send_declare(&self, msg: &mut zenoh_protocol::network::Declare) {
    let ctrl_lock = zlock!(self.tables.ctrl_lock);
    match &mut msg.body {
        zenoh_protocol::network::DeclareBody::DeclareSubscriber(m) => {
            let mut declares = vec![];
            self.declare_subscriber(
                m.id, &m.wire_expr, &SubscriberInfo, msg.ext_nodeid.node_id,
                &mut |p, m| declares.push((p.clone(), m)),
            );
            drop(ctrl_lock);
            for (p, m) in declares {
                m.with_mut(|m| p.send_declare(m));   // 把声明转发给下游 face
            }
        }
        // ... 其余 Declare 变体
    }
}
```

读这段你就能看到契约的「落地」：`Face` 收到一条 `Declare`，根据 body 类型（声明订阅者 / 可查询者 / key expr ……）调用自己的路由方法（如 `declare_subscriber`），这些方法会算出「该把这条声明转发给哪些下游 face」，再用回调 `p.send_declare(m)` 一一送出。至此，API 的一条声明就走完了从「应用」到「路由器」再到「下游链路」的全过程。

#### 4.2.4 代码实践

**实践目标**：通过阅读，建立「API 的每个动作 = `Primitives` 的一次方法调用」的对应关系。

**操作步骤**：

1. 打开 [zenoh/src/net/primitives/mod.rs:28](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/primitives/mod.rs#L28)，把 `Primitives` 的 7 个方法抄下来。
2. 在 `zenoh/src/api/session.rs` 里分别搜索这几个方法名（`send_declare`、`send_push`、`send_request`、`send_interest`、`send_close`），看它们各自被哪些 API 操作调用。例如 `declare_subscriber_inner` 调 `send_declare`（见 4.3 节），`close_inner` 调 `send_close`。
3. 整理一张「API 操作 → Primitives 方法」对照表。

**需要观察的现象**：你会发现所有出站 API 最终都收口到这 7 个方法之一，没有任何 API 直接调用传输层。

**预期结果**（参考对照表，可作为答案核对）：

| 公开 API 操作 | 最终调用的 Primitives 方法 |
| --- | --- |
| `declare_subscriber` / `declare_publisher` / `declare_queryable` / `declare_token` | `send_declare` |
| `publisher.put` / `delete`、`session.put` | `send_push` |
| `session.get` / `querier.get` | `send_request` |
| `queryable` 的 `reply` | `send_response` / `send_response_final` |
| `declare_querier`（持久兴趣）、liveliness `declare_subscriber` | `send_interest` |
| `session.close()` | `send_close` |

#### 4.2.5 小练习与答案

**练习 1**：`Primitives` 和 `EPrimitives` 都叫 primitives，它们最本质的区别是什么？

> **参考答案**：`Primitives` 是给 API 层用的「无路由上下文」出站接口，方法无返回值（发出去就完了）；`EPrimitives` 是路由器内部用的「带 `RoutingContext`」接口，方法返回 `bool` 表示「这条消息是否还需要继续向下游转发」，用于路由层的逐跳传播控制。

**练习 2**：为什么 `net/mod.rs` 里把 `runtime` 设成 `pub mod`（尽管加了 `#[doc(hidden)]`），而 `routing` 只是 `pub(crate)`？

> **参考答案**：插件（`zenohd` 加载的 `.so/.dll`）运行在 zenoh crate 之外，必须通过 `DynamicRuntime`（它 `Deref` 到 `Arc<dyn IRuntime>`）与运行时交互，所以 `runtime` 需要对外可见。而 `routing` 是纯内部实现细节，外部（包括插件）不应直接触碰路由表，故只 `pub(crate)`。这正是「按需开放最小接口」的稳定边界设计。

---

### 4.3 Runtime 与 RuntimeState：承载连接状态的核心

#### 4.3.1 概念说明

如果说 `Session` 是「用户视角的会话」，那么 `Runtime` 就是「机器视角的会话」。`Session` 关心的是「我声明了哪些 entity」；`Runtime` 关心的是「我这个 Zenoh 节点拥有什么」——它的 ZenohId、它的角色（router/peer/client）、它的传输管理器（管所有 TCP/UDP/… 链路）、它的路由器（`Gateway`，管所有路由表与 face）、它的 HLC 时钟、它的任务控制器、它的配置。

一个关键事实：**通常一个 Session 拥有自己独占的 Runtime，但 zenohd 这种路由器场景下，多个插件 Session 会共享同一个 Runtime**（源码注释在 `Session` 的 doc comment 里讲得很清楚）。所以 `SessionInner.runtime` 的类型不是 `Runtime`，而是 `GenericRuntime`——它既能装一个「真实 Runtime」（独占场景），也能装一个「动态 Runtime」（共享场景，只有 `DynamicRuntime`、没有静态副本）。

#### 4.3.2 核心流程

Runtime 的生命周期同样分三步：**build → 被 Session init 引用 → start**。

- **build**（`RuntimeBuilder::build`）：读 config，解析 zid / whatami，按需创建 HLC，构建 `Gateway` 路由器和 `TransportManager`，加载插件，组装出 `RuntimeState` 并包进 `Arc`。
- **init**（已在 4.1 讲）：Session 调 `runtime.new_primitives(...)`，路由器为此 Session 新建一个 `Face`，把它作为 `Primitives` 实现交还给 Session。
- **start**（`Runtime::start`，定义在 orchestrator.rs）：按 whatami 角色分支——`start_client` / `start_peer` / `start_router`，负责绑定监听端口、发起连接、跑 scouting。这部分细节留到《u7-l3 Runtime 编排器》。

`new_primitives` 是连接 4.1 与 4.3 的「铆钉」，值得单独看清：

```
Session::init
    │  runtime.new_primitives(Arc<dyn EPrimitives>)   // EPrimitives 实现是 session 自身（处理入站）
    ▼
RuntimeState::new_primitives
    │  self.router.new_session(e_primitives)           // 路由器为这个 session 开一张「面孔」
    ▼
Gateway::new_session
    │  新建 FaceState（带 face_id）+ Face
    │  把 Face 注册进 Tables.data.faces
    ▼
返回 (face_id, Arc<Face>)   // Face 实现了 Primitives
    │
    ▼
Session 把 Arc<Face> 当作 Arc<dyn Primitives> 存进 state.primitives
```

也就是说：**Session 拿到的「出站 primitives」其实就是路由器给它开的 Face**。Session 通过它「发」（`Face::send_declare` 走路由器出站），路由器又通过同一个 Face 反向「收」（Face 持有的 `e_primitives` 回调到 Session 处理入站）。一个 Face，双向使用。

#### 4.3.3 源码精读

**`RuntimeState`——节点的全部「家当」**：

[zenoh/src/net/runtime/mod.rs:164-183](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/mod.rs#L164-L183)

```rust
pub(crate) struct RuntimeState {
    zid: ZenohId,                              // 本节点唯一标识
    whatami: WhatAmI,                          // 角色：router/peer/client
    next_id: AtomicU32,                        // 给 entity 分配 id 的计数器
    router: Arc<Gateway>,                      // ★ 路由网关（所有路由表 + face）
    config: Notifier<ExpandedConfig>,          // 可热更新的配置
    manager: TransportManager,                 // ★ 管理所有 unicast/multicast 链路
    transport_handlers: std::sync::RwLock<Vec<Arc<dyn TransportEventHandler>>>,
    locators: std::sync::RwLock<Vec<Locator>>, // 本节点对外可达的端点
    hlc: Option<Arc<HLC>>,                     // 混合逻辑时钟（《u6-l4》）
    task_controller: TaskController,           // 管理所有 spawn 出来的任务
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

这一坨字段就是「一个 Zenoh 节点的全部运行时状态」。重点抓三个：`router`（路由核心，本讲当黑盒）、`manager`（传输核心）、`config`（带 `Notifier` 包装，支持运行时改部分配置）。

**`IRuntime` trait——Runtime 的「对外接口」**。Runtime 的能力通过这个 trait 暴露给 Session / 插件。注意 `new_primitives` 方法就定义在这里：

[zenoh/src/net/runtime/mod.rs:210-213](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/mod.rs#L210-L213)

```rust
fn new_primitives(
    &self,
    e_primitives: Arc<dyn EPrimitives + Send + Sync>,
) -> (usize, Arc<dyn Primitives>);
```

**`RuntimeState` 如何实现 `new_primitives`**——核心就一行 `self.router.new_session(...)`：

[zenoh/src/net/runtime/mod.rs:399-415](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/mod.rs#L399-L415)

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
            let face = self.router.new_session(e_primitives);
            (face.state.id, face)        // ★ face 就是 Arc<Face>，Face 实现了 Primitives
        }
    }
}
```

无 namespace 的常见路径下，直接 `router.new_session(e_primitives)` 返回一个 `Face`，它既实现了 `Primitives`（出站），又持有 `e_primitives`（入站回调）。返回的 `face.state.id` 就是 4.1 里看到的 `_face_id`。

**`Gateway::new_session`——开一张面孔**：路由器内部为这个 session 新建 `FaceState`（分配 face_id、绑定到本地 Region、记下 e_primitives、让 HAT 也新建对应 face），并插入 `Tables.data.faces` 映射表：

[zenoh/src/net/routing/gateway.rs:220-238](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/gateway.rs#L220-L238)

```rust
pub(crate) fn new_session(&self, primitives: Arc<dyn EPrimitives + Send + Sync>) -> Arc<Face> {
    let ctrl_lock = zlock!(self.tables.ctrl_lock);
    let mut wtables = zwrite!(self.tables.tables);
    let tables = &mut *wtables;
    let newface = Arc::new(
        FaceStateBuilder::new(
            tables.data.new_face_id(), tables.data.zid, Region::Local, Bound::North,
            primitives.clone(), tables.hats.map_ref(|hat| hat.new_face()),
        )
        .whatami(WhatAmI::Client).local(true).build(),
    );
    tables.data.faces.insert(newface.id, newface.clone());
    // ...
}
```

**`RuntimeBuilder::build`——Runtime 的「装配车间」**：这是把 config 变成可用 Runtime 的地方，重点看它如何造 `Gateway` 和 `TransportManager`：

[zenoh/src/net/runtime/mod.rs:689-779](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/mod.rs#L689-L779)（节选关键行）

```rust
let zid = ZenohIdProto::from(config.id());
let whatami = config.mode();
let hlc = (*unwrap_or_default!(config.timestamping().enabled().get(whatami)))
    .then(|| Arc::new(HLCBuilder::new().with_id(uhlc::ID::from(&zid)).build()));
let mut gateway_builder = GatewayBuilder::new(&config);
// ...
let gateway = Arc::new(gateway_builder.build()?);
let transport_manager = TransportManager::builder()
    .from_config(&config).await?
    .whatami(whatami)
    .bound_callback(/* ... */)
    .build(handler.clone(), /* stats */)?;
// ...
let runtime = Runtime {
    state: Arc::new(RuntimeState {
        zid: zid.into(), whatami,
        next_id: AtomicU32::new(1),   // 0 预留给路由核心
        router: gateway,
        config, manager: transport_manager,
        // ...
    }),
};
get_mut_unchecked(&mut runtime.state.router.clone()).init_hats(runtime.clone())?;
if start_admin_space { AdminSpace::start(&runtime).await; }
#[cfg(feature = "plugins")] start_plugins(&runtime);
```

读这段要注意三件事：① HLC 是否创建取决于 `timestamping.enabled`（router 默认开、peer/client 默认关，呼应《u6-l4》）；② `TransportManager::builder().from_config(&config)` 把所有传输相关配置（链路类型、lease、QoS 等）一次性灌进去；③ `init_hats` 给路由器装上当前角色对应的 HAT（路由拓扑策略，见《u8-l2》）。

**`Runtime` 与 `RuntimeState`、`GenericRuntime`、`DynamicRuntime` 的关系**：

[zenoh/src/net/runtime/mod.rs:803-806](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/mod.rs#L803-L806)

```rust
#[derive(Clone)]
pub struct Runtime {
    state: Arc<RuntimeState>,
}
```

`Runtime` 同样是 `Arc<RuntimeState>` 的薄壳、可克隆。而 `GenericRuntime`（[zenoh/src/net/runtime/mod.rs:1252-1289](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/mod.rs#L1252-L1289)）同时装着 `DynamicRuntime`（总是有，`Deref` 到 `Arc<dyn IRuntime>`，插件共享场景用）和一个可选的 `static_runtime: Option<Runtime>`（独占场景才有）。`close_inner` 里那句 `if let Some(r) = self.0.runtime.static_runtime()` 就是据此判断「我是不是 zenohd 那个持有真实 Runtime 的 session」——只有它才有权真正关闭 Runtime。

**`Runtime::start`——按角色编排建连**（定义在 orchestrator.rs，细节留到《u7-l3》）：

[zenoh/src/net/runtime/orchestrator.rs:140-146](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/orchestrator.rs#L140-L146)

```rust
pub async fn start(&mut self) -> ZResult<()> {
    match self.whatami() {
        WhatAmI::Client => self.start_client().await,
        WhatAmI::Peer => self.start_peer().await,
        WhatAmI::Router => self.start_router().await,
    }
}
```

至此整条启动链闭环：`open` → `Session::new` → `RuntimeBuilder::build`（造 Runtime）→ `Session::init`（用 `new_primitives` 接上 Face）→ `Runtime::start`（开始建连）。

#### 4.3.4 代码实践

**实践目标**：追踪一次 `declare_subscriber` 调用，写出「从公开 API 到 Runtime/Face」的完整调用路径描述（这也是本讲义规格里指定的实践任务）。

**操作步骤**：

1. 从 [zenoh/src/api/session.rs:1108](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L1108) 的 `declare_subscriber` 看 builder 构造（它只是把 session、key_expr、handler 打包成 `SubscriberBuilder`，**此时尚未真正声明**）。
2. builder 被 `.await` resolve 后，会进入 [zenoh/src/api/session.rs:1747](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L1747) 的 `declare_subscriber_inner`。精读它的三步：
   - `let primitives = state.primitives()?;`——取出 4.1 里塞进去的 `Arc<dyn Primitives>`（其实是 Face）。
   - `state.register_subscriber(id, key_expr, origin, callback)`——在 `SessionState` 本地登记（见 [zenoh/src/api/session.rs:393](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L393)），它会把订阅挂到所有 `intersects` 的 resource 节点上，并决定是否需要对外声明。
   - `primitives.send_declare(&mut Declare { ... body: DeclareSubscriber { id, wire_expr } })`——把声明交给 Face（[zenoh/src/api/session.rs:1763](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L1763)）。
3. 跟着 `send_declare` 进 [zenoh/src/net/routing/dispatcher/face.rs:559](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/face.rs#L559)，看 `Face::send_declare` 如何调 `self.declare_subscriber(...)` 算出下游，再逐个 `p.send_declare(m)` 转发。

**需要观察的现象**：你应该能看到「本地登记」与「网络声明」是两件事——`register_subscriber` 只改 `SessionState`，`send_declare` 才真正触网；且 `register_subscriber` 内部会用 key expression 的 `intersects` 关系（呼应《u2-l2》）决定把订阅挂到哪些 resource 上。

**预期结果**：写出的调用路径应类似如下（可作为你的实践报告模板）：

> `session.declare_subscriber(ke)` 构造一个 `SubscriberBuilder`；`.await` 触发 `Session::declare_subscriber_inner`：它先 `state.primitives()` 拿到 `Arc<dyn Primitives>`（实为 Runtime 在 `init` 时通过 `new_primitives → Gateway::new_session` 创建的 `Face`），再 `state.register_subscriber(...)` 在本地 `SessionState` 登记（按 `intersects` 挂到对应 resource 节点），最后 `primitives.send_declare(Declare{ body: DeclareSubscriber{id, wire_expr} })`。`Face::send_declare` 在路由器的 `Tables` 里调用 `declare_subscriber`，算出该把这条声明转发给哪些下游 face，逐个 `p.send_declare(m)` 送出，最终经 TransportManager 发到对端。整条链没有一处直接操作 socket，全部经过 `Primitives` 契约与路由器抽象。

#### 4.3.5 小练习与答案

**练习 1**：`SessionInner.runtime` 的类型为什么是 `GenericRuntime` 而非 `Runtime`？

> **参考答案**：因为要兼容「共享 Runtime」场景。普通应用独占一个 Runtime（`GenericRuntime` 内含 `static_runtime: Some(Runtime)`）；而 zenohd 里每个插件 Session 共用同一个路由器 Runtime，这时只有 `DynamicRuntime`（`Arc<dyn IRuntime>`）、没有静态副本。`GenericRuntime` 用枚举同时承载两种情况，并通过 `Deref` 暴露统一的 `IRuntime` 接口。

**练习 2**：`Session` 拿到的 `Arc<dyn Primitives>` 与 `Gateway::new_session` 收到的 `e_primitives` 参数，分别承担什么方向的数据流？

> **参考答案**：`Arc<dyn Primitives>`（实为 `Face`）承担**出站**：Session 调 `send_declare/send_push/...` 把应用消息送进路由器。`e_primitives` 承担**入站**：它是 Session 提供给路由器的回调（`Arc<dyn EPrimitives>`），当路由器有消息要投递给本 Session 时，通过它回调到 `SessionState`，最终触发 subscriber 的 callback 或 queryable 的 handler。一个 Face 对象把这两个方向缝在一起。

---

## 5. 综合实践

**任务**：画出一张「`zenoh::open` 到一次 `subscriber.recv_async()` 收到对端 `put`」的完整端到端流转图，并配文字说明，把本讲三个模块的知识串起来。

**要求**：

1. **启动段**（用 4.1 + 4.3）：标出 `open → Session::new → RuntimeBuilder::build → Session::init → Runtime::start` 五个节点，注明 `init` 内 `new_primitives` 创建 Face 这一步。
2. **出站段**（用 4.2 + 4.3）：假设对端有一个 publisher 调 `put`，画出 `Session.put → state.primitives().send_push → Face::send_push → Gateway 路由 → TransportManager → 链路`。
3. **入站段**：本端收到数据后，反向画出 `链路 → TransportManager → DeMux.handle_message → Face/Gateway 路由 → e_primitives 回调 → SessionState 找到匹配的 subscriber → handler.recv_async 返回 Sample`。

**验收标准**：

- 图里必须出现这些关键词：`Session`、`SessionInner`、`SessionState`、`Primitives`/`Face`、`Runtime`/`RuntimeState`、`Gateway`、`TransportManager`。
- 文字说明里要明确指出「出站走 `Primitives` 契约、入站走 `EPrimitives` 回调」，体现你对双向数据流的理解。
- 标出哪一步对应《u7-l2》将要详讲的 `Mux`/`DeMux`（出站多路复用 / 入站分用），为下一讲埋好接口。

> 这个任务不需要你写代码，重点是**用源码行号佐证你画的每一条箭头**。例如出站 `send_push` 这一步，应能指到 [face.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/face.rs) 里 `impl Primitives for Face` 的位置。完成后，你就拥有了后续《u7-l2》《u8》《u9》全部内核讲义的「地图」。

## 6. 本讲小结

- `Session` 是 `#[repr(transparent)]` 的 `Arc<SessionInner>` 薄壳；`SessionInner` 持有 `runtime` 与 `state: RwLock<SessionState>`；`SessionState` 是登记所有 publishers/subscribers/queryables/resources 的「实体大仓库」，并用 `Option<Arc<dyn Primitives>>` 持有出站出口。
- `zenoh::open` 的真正执行链是 `Session::new`：`RuntimeBuilder::build`（造 Runtime）→ `Session::init`（造 Session 并接上 Runtime）→ `Runtime::start`（开始建连）。顺序必须是「先接线再通电」。
- `Session::init` 的核心是 `runtime.new_primitives(...)`：它让路由器为本会话开一个 `Face`，把 `Arc<Face>` 当作 `Arc<dyn Primitives>` 塞进 `SessionState.primitives`，从而打通出站通道。
- `net` 模块是「稳定 API」与「内部实现」的分界线；`Primitives` trait 是 API 与路由/传输层之间的出站契约，把 `send_declare/send_push/send_request/...` 七个动作收口，API 层从不直接碰 socket。
- `Runtime`/`RuntimeState` 是承载节点全部连接状态的核心：持有 `zid`、`whatami`、`Gateway` 路由器、`TransportManager`、`HLC`、`TaskController`、可热更新的 `config`。通常一 Session 独占一 Runtime，zenohd 多插件则共享同一 Runtime（靠 `GenericRuntime` 兼容）。
- 一个 `Face` 双向使用：对 Session 是出站的 `Primitives`，对路由器又持有入站的 `e_primitives` 回调；`declare_subscriber` 的完整链路是 `declare_subscriber_inner → register_subscriber(本地) + primitives.send_declare → Face::send_declare → Gateway 路由 → 下游 face → TransportManager`。

## 7. 下一步学习建议

本讲建立了「Session → Primitives → Face → Gateway → Runtime」的骨架，但有两处故意留了黑盒，正好是后续讲义的主题：

- **《u7-l2 Primitives 与 Mux/DeMux》**：本讲多次提到「出站 send / 入站回调」，下一讲会把 `Primitives`/`EPrimitives` 与 `Mux`（出站多路复用）、`DeMux`（入站分用）的分工彻底讲透，补全数据流图里的中间环节。
- **《u7-l3 Runtime 编排器》**：本讲只点了 `Runtime::start` 按 client/peer/router 分支，下一讲深入 `orchestrator.rs`，讲清 scouting 周期、`PeerConnector` 建连、AutoConnect 策略。
- **《u8 路由（HAT 拓扑）》单元**：本讲把 `Gateway`/`Face`/`Tables` 当黑盒，那里会逐一拆开——`Gateway` 如何管 face、`Tables` 如何存资源与路由、四种 HAT（router/peer/client/broker）如何决定转发。
- **《u9 传输与链路层》单元**：本讲的 `TransportManager` 在那里展开成 unicast/multicast、各链路（tcp/udp/quic/ws）、批处理与分片。

建议你带着本讲的「流转图」继续往下读：每读到一个新组件，就把它填进 4.2.2 那张双向数据流图里，逐步把黑盒换成白盒。
