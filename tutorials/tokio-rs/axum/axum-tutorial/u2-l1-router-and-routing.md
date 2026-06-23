# Router 与路由注册

## 1. 本讲目标

本讲深入 axum 路由系统的「入口层」。学完后你应该能够：

1. 说清 `Router<S>` 这个对外类型为什么只持有一个 `Arc<RouterInner<S>>` 字段，以及为什么它的 `Clone` 是「廉价」的。
2. 画出 `RouterInner` 的三块拼图：`path_router`、`default_fallback`、`catch_all_fallback`，并解释一个请求在没有匹配路由时为什么会落到 `catch_all_fallback`。
3. 区分 `route()` 与 `route_service()` 两条注册入口：一个接收「方法路由 + handler」，另一个接收「任意 tower Service」，并知道它们各自的适用场景与陷阱。
4. 在真实源码中定位这些定义，并用 `curl` 触发自己注册的路由。

本讲是整个第 2 单元（路由系统）的地基。后续讲义（路径匹配、MethodRouter、nest/merge、fallback）都建立在本讲描述的 `Router` / `RouterInner` 结构之上。

## 2. 前置知识

在进入本讲前，你应该已经掌握（来自第 1 单元）：

- **axum 的定位**：它是 hyper 之上「相对薄的一层」，把 HTTP 请求交给 handler 处理。
- **tower::Service**：axum 不自造中间件，handler 与任意请求处理器最终都要适配成 `Service<Request>`。本讲会再次出现 `Service` 约束——`route_service` 只接收实现了 `Service` 的类型。
- **Handler 与 MethodRouter**：`async fn` 通过 `Handler` 的 blanket impl 适配为 Service；`get`/`post` 等方法路由函数会把一个 handler 包成 `MethodRouter`（某路径上「HTTP 方法 → handler」的映射）。本讲的 `route()` 接收的正是 `MethodRouter`。
- **Router<S> 的泛型 S**：表示「还缺少」的状态类型，只有 `Router<()>` 能交给 `axum::serve`。本讲默认讨论 `Router<()>`，状态机制留到提取器单元细讲。

两个本讲会用到的 Rust 概念，先做一句话解释：

- **`Arc<T>`（原子引用计数）**：让同一份堆数据被多个所有者共享，克隆一个 `Arc` 只是把引用计数 +1，非常便宜；真正复制数据只在「需要独占修改」时才发生。
- **Builder 模式（消费 self）**：`Router` 的方法大都写成 `fn route(self, ...) -> Self`，即「吃掉旧 Router、吐出新 Router」，从而支持 `Router::new().route(...).route(...)` 的链式调用。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `axum/src/routing/mod.rs` | `Router`、`RouterInner`、`route`/`route_service`、`Fallback`、`Endpoint` 的定义与 `tap_inner!`/`map_inner!` 宏，本讲的主战场。 |
| `axum/src/routing/path_router.rs` | `PathRouter`：真正存放路由表（`routes: Vec<Endpoint>` + `matchit` 路由树）的内部类型，`route`/`route_service` 最终都委托给它。 |
| `axum/src/routing/not_found.rs` | `NotFound`：默认 fallback 用的 Service，对所有请求返回 `404 Not Found`。 |
| `axum/src/docs/routing/route.md` | `Router::route` 的文档，描述路径语法与行为。 |
| `axum/src/docs/routing/route_service.md` | `Router::route_service` 的文档，含 `service_fn` 与 `ServeFile` 示例。 |
| `examples/hello-world/src/main.rs` | 最小可运行示例，本讲实践的起点。 |

> 说明：本讲聚焦 `Router`/`RouterInner`/`route`/`route_service` 这层「壳」。`PathRouter` 内部的 matchit 匹配树、`RouteId` 双向映射等细节属于专家层（见 u9-l1），本讲只在「它存了什么」的层面引用，不深入。

## 4. 核心概念与源码讲解

### 4.1 Router：对外类型与 Arc 共享

#### 4.1.1 概念说明

`Router` 是 axum 暴露给用户的「路由容器」类型。它本身非常薄——公开字段只有一个 `inner`。用户的绝大多数操作（`new`、`route`、`nest`、`layer`、`with_state`、`fallback`）都是 `Router` 上的方法，但这些方法并不直接操作数据，而是通过两个内部宏去「拆开 → 改 → 重新包好」内部的 `RouterInner`。

之所以这样设计，关键在于 `Router` 必须实现 `Clone`（运行时每条 TCP 连接都需要一个 Router 的克隆去处理请求），但路由表可能很大、里面可能装着昂贵的 Service。如果每次 `clone` 都深拷贝整张表，开销会高得无法接受。因此 axum 让 `Router` 持有一个 `Arc<RouterInner<S>>`：

- **克隆 Router = 克隆 Arc = 引用计数 +1**，几纳秒级别，不动真正的数据。
- **修改 Router（注册新路由）= 写时复制（copy-on-write）**：只有当确实需要独占修改时，才把内部数据真正复制一份。

#### 4.1.2 核心流程

`Router` 的「注册路由」流程可以用下面的伪代码描述：

```
router.route("/users", get(handler))
  => self 被「消费」（按值传入）
  => tap_inner! 宏展开：
       1. 调用 into_inner() 拿到 RouterInner：
            - 若 Arc 只此一个引用 -> 直接 unwrap，零拷贝
            - 若 Arc 还有其他引用 -> clone 内部三块数据（写时复制）
       2. 对拿到的 path_router 调用 .route(path, method_router)  // 真正登记
       3. 用新的 RouterInner 重新包一个 Arc
  => 返回新的 Router（于是可以继续 .route(...).layer(...))
```

也就是说，链式调用 `Router::new().route(a).route(b)` 的每一步，都是一个「拆包-改-重包」的过程；只要中间没有把 Router clone 出多份，整个过程都是零拷贝的。

#### 4.1.3 源码精读

先看 `Router` 的定义，它只有一个字段：

```rust
#[must_use]
pub struct Router<S = ()> {
    inner: Arc<RouterInner<S>>,
}
```

这是 [axum/src/routing/mod.rs:86-88](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L86-L88)，说明 `Router` 把所有家当都放进了一个 `Arc` 里。`S = ()` 是状态类型的默认值，表示「不缺状态，可以直接 serve」。

`Clone` 是手写实现的，只做 `Arc::clone`，不做任何深拷贝：

```rust
impl<S> Clone for Router<S> {
    fn clone(&self) -> Self {
        Self {
            inner: Arc::clone(&self.inner),
        }
    }
}
```

见 [axum/src/routing/mod.rs:90-96](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L90-L96)。这就是「克隆 Router 很便宜」的源头——无论路由表多大，`clone` 永远只是一个原子计数加一。

写时复制的核心在 `into_inner`，它尝试独占 Arc 内部：

```rust
fn into_inner(self) -> RouterInner<S> {
    match Arc::try_unwrap(self.inner) {
        Ok(inner) => inner,                       // 只有我一个引用 -> 零拷贝拿到内部
        Err(arc) => RouterInner {                 // 还有别的引用 -> 逐字段 clone（写时复制）
            path_router: arc.path_router.clone(),
            default_fallback: arc.default_fallback,
            catch_all_fallback: arc.catch_all_fallback.clone(),
        },
    }
}
```

见 [axum/src/routing/mod.rs:172-181](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L172-L181)。`Arc::try_unwrap` 在引用计数为 1 时成功（直接拿走内部值），否则失败（说明别处还有克隆，只能复制）。

`route` 等方法并不直接调用 `into_inner`，而是通过 `tap_inner!` 宏，它封装了「拆开-改-重包」：

```rust
macro_rules! tap_inner {
    ( $self_:ident, mut $inner:ident => { $($stmt:stmt)* } ) => {
        {
            let mut $inner = $self_.into_inner();
            $($stmt)*;
            Router {
                inner: Arc::new($inner),
            }
        }
    };
}
```

见 [axum/src/routing/mod.rs:141-152](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L141-L152)。`Router::route`、`Router::fallback`、`Router::reset_fallback` 都用它。另一个类似的 `map_inner!`（[L129-L139](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L129-L139)）则用于「用表达式整体重建内部」，被 `layer`/`merge`/`with_state` 使用。两者构成了 axum builder 模式的统一骨架。

#### 4.1.4 代码实践

**目标**：亲手验证「Router 的 Clone 是廉价的，且修改已 Clone 的 Router 会触发写时复制」。

**操作步骤**：

1. 在本地新建一个依赖 axum 的项目（或复用 `examples/hello-world`）。
2. 阅读上面三段源码，确认你能在 [mod.rs](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L86-L96) 中找到 `Router` 的定义与 `Clone` 实现。
3. 在你的 `main.rs` 里写一段「源码阅读型」的对照实验（不需要运行，重点在理解）：

```rust
// 示例代码：用于对照理解 Arc 共享，非项目原有代码
use axum::{routing::get, Router};

let app = Router::new().route("/", get(|| async {}));   // 此时 Arc 引用计数 = 1
let app2 = app.clone();                                 // Arc::clone，计数 = 2，零拷贝
// 若此刻调用 app.route("/x", get(|| async {}))：
//   into_inner -> Arc::try_unwrap 失败（app2 还持引用）
//   -> 走 Err 分支，clone path_router 等（写时复制）
```

**需要观察的现象**：理解链式调用 `Router::new().route(a).route(b)` 中，每一步 `self` 都是「当场消费、当场重建」，中间没有产生多余的克隆，因此 `try_unwrap` 一直成功，整条链零拷贝。

**预期结果**：你能用自己的话回答——「为什么 `Router` 用 `Arc` 而不是直接持有 `RouterInner`？」答案是：为了让运行时频繁的 `Router::clone`（每条连接一次）保持廉价，同时通过写时复制保留 builder 模式的可变性。

#### 4.1.5 小练习与答案

**练习 1**：如果删掉手写的 `impl Clone for Router`，让编译器自动 derive `Clone`，会发生什么？

**答案**：自动 derive 会要求 `RouterInner: Clone`，并且每次 clone 都会深拷贝 `RouterInner`（包括整张路由表）。这会让运行时每条连接的克隆都付出整表复制的代价，正是 axum 要避免的。手写 `Clone` 只克隆 `Arc`，才是正确做法。

**练习 2**：`Arc::try_unwrap` 在什么情况下会返回 `Err`？

**答案**：当该 `Arc` 的引用计数大于 1，即除了当前这个 `Router` 之外，还有别的 `Router` 克隆共享同一份 `RouterInner` 时。这时无法安全地「拿走」内部值，只能 clone。

---

### 4.2 RouterInner：路由的内部容器

#### 4.2.1 概念说明

`RouterInner` 是 `Router` 真正的家当。它把一个 axum 应用拆成两半：

- **`path_router`**：按 URL 路径组织的「正路由表」。你用 `route`/`route_service`/`nest`/`merge` 注册的所有路由都进这里，由它负责路径匹配并分发给对应的 Service。
- **`catch_all_fallback`**：兜底处理器。当 `path_router` 没有任何匹配（路径完全没登记）时，请求会落到这里。默认是一个永远返回 404 的 `NotFound` Service，但你可以用 `fallback`/`fallback_service` 自定义。

中间还有一个 `default_fallback: bool` 标记，记录「当前 catch_all_fallback 是否还是默认的那个」。它主要用于 `merge` 时判断两个 Router 是否都自定义了 fallback（不允许），这个细节在 u2-l4/u2-l5 才展开，本讲只需知道它的存在。

#### 4.2.2 核心流程

一个请求到来时，`Router` 内部的分发逻辑大致如下：

```
请求 req 进入 Router::call / call_with_state
  => 先问 path_router：你能不能匹配这个 req 的路径？
       - 能匹配  -> 返回 path_router 给出的 Future（命中正路由）
       - 不能匹配 -> 把 (req, state) 还回来
  => 拿回的 (req, state) 交给 catch_all_fallback 处理（命中兜底）
       - 默认 catch_all_fallback = NotFound -> 返回 404
```

这套「先正路由、后兜底」的两段式，是 axum 路由分发的基本节奏。

#### 4.2.3 源码精读

`RouterInner` 的定义只有三个字段：

```rust
struct RouterInner<S> {
    path_router: PathRouter<S>,
    default_fallback: bool,
    catch_all_fallback: Fallback<S>,
}
```

见 [axum/src/routing/mod.rs:98-102](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L98-L102)。注意它是私有类型（`struct` 前无 `pub`），用户只能通过 `Router` 的方法间接操作它——这是 axum 封装内部实现的方式。

`Router::new()` 给这三个字段填上初始值：

```rust
pub fn new() -> Self {
    Self {
        inner: Arc::new(RouterInner {
            path_router: Default::default(),
            default_fallback: true,
            catch_all_fallback: Fallback::Default(Route::new(NotFound)),
        }),
    }
}
```

见 [axum/src/routing/mod.rs:162-170](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L162-L170)。所以一个空 Router 不是「什么都没有」，而是「正路由为空 + 兜底返回 404」。这正是 `route.md` 文档里那句「Unless you add additional routes this will respond with 404 Not Found to all requests」的实现来源。

`catch_all_fallback` 的类型 `Fallback<S>` 是一个枚举，区分三种来源：

```rust
enum Fallback<S, E = Infallible> {
    Default(Route<E>),
    Service(Route<E>),
    BoxedHandler(BoxedIntoRoute<S, E>),
}
```

见 [axum/src/routing/mod.rs:710-714](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L710-L714)。`Default` 是默认 404；`Service` 是 `fallback_service` 注册的原生 Service；`BoxedHandler` 是 `fallback(handler)` 注册的 handler（还带着状态 S，等待 `with_state` 时才转成 Route）。本讲只关心 `Default` 这一分支。

默认 fallback 用的 `NotFound` 是一个极简 Service，对所有请求返回 404：

```rust
pub(super) struct NotFound;

impl<B> Service<Request<B>> for NotFound
where
    B: Send + 'static,
{
    type Response = Response;
    type Error = Infallible;
    // ...
    fn call(&mut self, _req: Request<B>) -> Self::Future {
        ready(Ok(StatusCode::NOT_FOUND.into_response()))
    }
}
```

见 [axum/src/routing/not_found.rs:16-34](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/not_found.rs#L16-L34)。它永远 `Poll::Ready(Ok(()))`，永远返回 `StatusCode::NOT_FOUND`，是整条分发链的最底端。

最后看「先正路由、后兜底」在代码里是怎么落地的——`call_with_state`：

```rust
pub(crate) fn call_with_state(&self, req: Request, state: S) -> RouteFuture<Infallible> {
    let (req, state) = match self.inner.path_router.call_with_state(req, state) {
        Ok(future) => return future,                      // 命中正路由，直接返回
        Err((req, state)) => (req, state),                // 没命中，连请求带状态还回来
    };

    self.inner
        .catch_all_fallback
        .clone()
        .call_with_state(req, state)                      // 交给兜底
}
```

见 [axum/src/routing/mod.rs:452-462](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L452-L462)。`path_router.call_with_state` 返回 `Result<RouteFuture, (Request, S)>`：成功表示匹配到了路由，失败时把「没被消费的 Request 和 State」原样归还，供 fallback 继续使用。这正是兜底机制能拿到完整请求的原因。

#### 4.2.4 代码实践

**目标**：观察「空 Router 对所有请求返回 404」这一行为，并定位 `RouterInner` 的定义行号。

**操作步骤**：

1. 把 `examples/hello-world/src/main.rs` 复制到本地（见 [examples/hello-world/src/main.rs:1-25](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/examples/hello-world/src/main.rs#L1-L25)）。
2. 运行：`cargo run -p example-hello-world`。
3. 在另一个终端用 curl 请求一个**未注册**的路径：`curl -i http://127.0.0.1:3000/nope`。
4. 在源码中确认 `RouterInner` 定义于 [mod.rs:98-102](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L98-L102)，并记录该行号。

**需要观察的现象**：`curl -i` 输出的状态行是 `HTTP/1.1 404 Not Found`，响应体为空。这说明 `/nope` 没有命中 `path_router`（只登记了 `/`），于是落到了 `catch_all_fallback`（默认 `NotFound`）。

**预期结果**：你能把「curl 看到的 404」与「源码里 `NotFound::call` 返回 `StatusCode::NOT_FOUND`」对应起来，并指出 `RouterInner` 在 `axum/src/routing/mod.rs` 第 98–102 行。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `RouterInner` 是私有的，而 `Router` 是公开的？

**答案**：`RouterInner` 是实现细节（字段布局、`PathRouter` 内部结构都可能随版本变化），axum 不希望用户直接构造或修改它；`Router` 是稳定的公共 API，通过方法暴露受控的操作。把内部类型藏起来，axum 才能在未来重构 `RouterInner` 而不破坏用户代码。

**练习 2**：如果把 `catch_all_fallback` 从 `RouterInner` 里去掉，会有什么后果？

**答案**：那么当请求路径未匹配任何正路由时，`Router` 将没有东西可以处理它。`call_with_state` 里 `path_router` 返回 `Err((req, state))` 后将无路可走，要么 panic，要么返回某种错误响应——无法再提供「统一 404」和「可自定义 fallback」的能力。

---

### 4.3 route 与 route_service：两条注册入口

#### 4.3.1 概念说明

`Router` 提供两条注册路由的入口，分别面向两种「被路由的东西」：

- **`route(path, method_router)`**：接收一个 `MethodRouter`（由 `get`/`post` 等产生，内含一个或多个 handler）。它是日常开发的主力——handler 自动适配成 Service，还能按 HTTP 方法分发、合并同路径的多个方法。
- **`route_service(path, service)`**：接收一个**任意的 tower Service**（需满足 `Service<Request, Error = Infallible>`）。当你想直接挂一个现成的 Service（如 `tower_http::services::ServeFile`、自定义 `service_fn`），或想对某个路径「所有方法都走同一个 Service」时用它。

两者的核心区别可以归纳成一张表：

| 维度 | `route` | `route_service` |
| --- | --- | --- |
| 接收类型 | `MethodRouter<S>`（含 handler） | 任意 `Service<Request, Error=Infallible>` |
| 方法感知 | 按方法分发，未匹配方法返回 405 | 方法无关，所有方法都走同一 Service |
| 同路径多次注册 | 合并（`route("/", get(_)).route("/", post(_))` 合并） | 覆盖（按 matchit 重复插入会 panic） |
| 传 Router 进去 | 允许（但通常用 `get` 等） | **panic**，提示改用 `nest` |

#### 4.3.2 核心流程

两条入口的执行路径高度相似，都是「消费 self → 拆开 RouterInner → 委托给 path_router → 重新包好」：

```
route("/users", get(handler))
  => tap_inner!(self, mut this => { path_router.route(path, method_router) })
  => path_router.route:
       1. validate_path: 路径必须以 `/` 开头，v7 校验禁止 `:`/`*` 前缀
       2. 若该路径已有 MethodRouter -> 合并（merge_for_path）
       3. 否则 -> new_route: 分配 RouteId，插入 matchit 树，push Endpoint::MethodRouter

route_service("/file", ServeFile::new(...))
  => tap_inner!(self, mut this => { path_router.route_service(path, service) })
  => 先 try_downcast: 若 service 其实是 Router -> panic（提示用 nest）
  => path_router.route_service:
       -> route_endpoint(path, Endpoint::Route(Route::new(service)))
       -> new_route: 分配 RouteId，插入 matchit 树，push Endpoint::Route
```

可以看到，最终所有路由都变成 `Endpoint`（见下方源码），存进 `PathRouter` 的 `routes: Vec<Endpoint>`。两条入口的区别，本质上只是往 `Endpoint` 的哪个变体里装：

- `route` → `Endpoint::MethodRouter(MethodRouter<S>)`
- `route_service` → `Endpoint::Route(Route)`（`Route::new` 把 Service 类型擦除打包）

#### 4.3.3 源码精读

先看两条入口本身。`route` 委托给 `path_router.route`：

```rust
#[doc = include_str!("../docs/routing/route.md")]
#[track_caller]
pub fn route(self, path: &str, method_router: MethodRouter<S>) -> Self {
    tap_inner!(self, mut this => {
        panic_on_err!(this.path_router.route(path, method_router));
    })
}
```

见 [axum/src/routing/mod.rs:190-196](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L190-L196)。文档来自 [axum/src/docs/routing/route.md](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/docs/routing/route.md)。注意 `#[track_caller]`：当 `path_router.route` 内部 panic（如路径重叠）时，错误信息会指向**用户调用 `.route(...)` 的那一行**，而不是 axum 内部，便于定位。

`route_service` 多一道「防呆」检查，并且接收任意 `Service`：

```rust
#[doc = include_str!("../docs/routing/route_service.md")]
pub fn route_service<T>(self, path: &str, service: T) -> Self
where
    T: Service<Request, Error = Infallible> + Clone + Send + Sync + 'static,
    T::Response: IntoResponse,
    T::Future: Send + 'static,
{
    let Err(service) = try_downcast::<Self, _>(service) else {
        panic!(
            "Invalid route: `Router::route_service` cannot be used with `Router`s. \
            Use `Router::nest` instead"
        );
    };

    tap_inner!(self, mut this => {
        panic_on_err!(this.path_router.route_service(path, service));
    })
}
```

见 [axum/src/routing/mod.rs:198-215](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L198-L215)。约束 `T: Service<Request, Error = Infallible>` 很关键：axum 的路由层不允许 Service 返回错误，所有问题要么在 Service 内部转成响应、要么用 `HandleErrorLayer` 兜（见 u7-l2）。`try_downcast` 那行是个有趣的防呆——如果你不小心把一个 `Router` 当 Service 传进来，它会 panic 并提示「用 `nest` 代替」，因为把 Router 嵌进路径树是 `nest` 的职责（见 route_service.md 第 53-68 行的 panic 示例）。

再看委托对象 `PathRouter`。它的结构揭示了「路由到底以什么形式被存储」：

```rust
pub(super) struct PathRouter<S> {
    routes: Vec<Endpoint<S>>,
    node: Arc<Node>,
    v7_checks: bool,
}
```

见 [axum/src/routing/path_router.rs:16-20](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/path_router.rs#L16-L20)。`routes` 是按 `RouteId` 顺序排列的 Endpoint 列表；`node` 是 matchit 路由树（路径 → RouteId 的映射）；`v7_checks` 控制是否做 v0.7 风格的路径校验。`Endpoint` 是个二选一枚举：

```rust
#[allow(clippy::large_enum_variant)]
enum Endpoint<S> {
    MethodRouter(MethodRouter<S>),
    Route(Route),
}
```

见 [axum/src/routing/mod.rs:786-790](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L786-L790)。这正对应两条入口：`route` 存 `MethodRouter` 变体，`route_service` 存 `Route` 变体。

`PathRouter::route` 的关键逻辑是「同路径合并」：

```rust
pub(super) fn route(
    &mut self,
    path: &str,
    method_router: MethodRouter<S>,
) -> Result<(), Cow<'static, str>> {
    validate_path(self.v7_checks, path)?;

    if let Some((route_id, Endpoint::MethodRouter(prev_method_router))) = self
        .node
        .path_to_route_id
        .get(path)
        .and_then(|route_id| self.routes.get(route_id.0).map(|svc| (*route_id, svc)))
    {
        // 如果该路径已有 MethodRouter，就合并：这让 .route("/", get(_)).route("/", post(_)) 生效
        let service = Endpoint::MethodRouter(
            prev_method_router
                .clone()
                .merge_for_path(Some(path), method_router)?,
        );
        self.routes[route_id.0] = service;
    } else {
        let endpoint = Endpoint::MethodRouter(method_router);
        self.new_route(path, endpoint)?;
    }

    Ok(())
}
```

见 [axum/src/routing/path_router.rs:66-93](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/path_router.rs#L66-L93)。这段解释了 [route.md 第 120-135 行](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/docs/routing/route.md#L120-L135) 那个「逐个添加方法」的示例为什么能工作——同一路径多次 `route` 会被 merge 成一个含多方法的 MethodRouter。

而 `route_service` 直接走 `route_endpoint`，不合并：

```rust
pub(super) fn route_service<T>(
    &mut self,
    path: &str,
    service: T,
) -> Result<(), Cow<'static, str>>
where
    T: Service<Request, Error = Infallible> + Clone + Send + Sync + 'static,
    T::Response: IntoResponse,
    T::Future: Send + 'static,
{
    self.route_endpoint(path, Endpoint::Route(Route::new(service)))
}
```

见 [axum/src/routing/path_router.rs:107-118](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/path_router.rs#L107-L118)。`Route::new(service)` 把任意 Service 类型擦除（`BoxCloneSyncService`）打包成统一的 `Route`，所以 `Endpoint::Route` 永远装的是同一种类型，不再带泛型。

最后，两条入口最终都汇到 `new_route` 完成登记：

```rust
fn new_route(&mut self, path: &str, endpoint: Endpoint<S>) -> Result<(), String> {
    let id = RouteId(self.routes.len());
    self.set_node(path, id)?;          // 插入 matchit 树：path -> RouteId
    self.routes.push(endpoint);        // 追加到 Vec：RouteId -> Endpoint
    Ok(())
}
```

见 [axum/src/routing/path_router.rs:139-144](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/path_router.rs#L139-L144)。`RouteId` 就是 `routes` Vec 里的下标（见 [mod.rs:75-76](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L75-L76) 的 `pub(crate) struct RouteId(usize);`）。matchit 树负责「URL → RouteId」，Vec 负责「RouteId → Endpoint」，两者配合完成查找。

#### 4.3.4 代码实践

**目标**：同时用 `route`（普通 handler）和 `route_service`（自定义 Service）注册两条路由，分别 curl 触发，理解两者行为差异。

**操作步骤**：

1. 新建一个项目，`Cargo.toml` 依赖（这些是示例代码，请按你本地项目结构调整）：

```toml
# 示例代码
[dependencies]
axum = "0.8"
tokio = { version = "1", features = ["full"] }
tower = "0.5"
```

2. 编写 `src/main.rs`（示例代码，参考自 [route_service.md](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/docs/routing/route_service.md#L1-L47) 与 hello-world 示例）：

```rust
use axum::{extract::Request, response::Html, routing::get, Router};
use std::convert::Infallible;
use tower::service_fn;

#[tokio::main]
async fn main() {
    // route：注册一个普通 handler
    // route_service：注册一个自定义 tower Service
    let app = Router::new()
        .route("/", get(handler))
        .route_service("/svc", service_fn(my_service));

    let listener = tokio::net::TcpListener::bind("127.0.0.1:3000")
        .await
        .unwrap();
    println!("listening on {}", listener.local_addr().unwrap());
    axum::serve(listener, app).await;
}

// 普通 handler：由 Handler blanket impl 自动适配为 Service
async fn handler() -> Html<&'static str> {
    Html("<h1>Hello from handler</h1>")
}

// 自定义 Service：手动返回 Result<_, Infallible>
async fn my_service(req: Request) -> Result<String, Infallible> {
    Ok(format!("service saw {} {}", req.method(), req.uri().path()))
}
```

3. 运行 `cargo run`。
4. 在另一终端分别触发：
   - `curl -i http://127.0.0.1:3000/`
   - `curl -i http://127.0.0.1:3000/svc`
   - `curl -i -X POST http://127.0.0.1:3000/`（用 POST 请求 `/`）
   - `curl -i -X POST http://127.0.0.1:3000/svc`（用 POST 请求 `/svc`）

**需要观察的现象**：

- `/` 用 GET 返回 200 + HTML；用 POST 返回 **405 Method Not Allowed**（因为 `/` 只注册了 `get`，由 MethodRouter 决定）。
- `/svc` 无论 GET 还是 POST 都返回 200 + `"service saw METHOD /svc"`——因为 `route_service` 是方法无关的，所有方法都走同一个 Service。

**预期结果**：你能清楚看到 `route`（方法感知、未匹配方法 405）与 `route_service`（方法无关、来者不拒）的区别，并在源码中把这一区别归因到「`Endpoint::MethodRouter` vs `Endpoint::Route`」两个变体（[mod.rs:786-790](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L786-L790)）。

5. **记录行号**：`RouterInner` 定义在 [axum/src/routing/mod.rs:98-102](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L98-L102)。

> 若本地环境无法运行，明确标注「待本地验证」并只完成源码阅读部分（对照上面的「需要观察的现象」推断结果）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `route_service` 要约束 `T: Service<Request, Error = Infallible>`，即不允许 Service 返回错误？

**答案**：axum 的 `Router` 自身实现的 `Service` 其 `Error` 类型就是 `Infallible`（见 [mod.rs:599-618](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L599-L618) 的 `type Error = Infallible;`）。这意味着「路由层保证不会产生错误」，所有失败都必须被转成某个 HTTP 响应。所以挂进来的 Service 也必须自己把错误吃掉（转成响应），或在上游用 `HandleErrorLayer` 兜底——不能把错误冒泡到 Router。

**练习 2**：下面的代码会怎样？为什么？

```rust
// 示例代码
Router::new().route_service("/", Router::new().route("/foo", get(|| async {})));
```

**答案**：它会 **panic**。`route_service` 开头有 `try_downcast::<Self, _>(service)` 检查（[mod.rs:205-210](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L205-L210)），发现传入的其实是一个 `Router` 时，会 panic 并提示「`route_service` 不能用于 `Router`，请改用 `nest`」。把一个子 Router 挂到某前缀下，是 `Router::nest` 的职责（见 u2-l4）。

**练习 3**：`route("/", get(a)).route("/", post(b))` 和 `route("/", get(a).post(b))` 效果一样吗？

**答案**：一样。前者两次 `route` 命中同一路径，在 `PathRouter::route` 里走「合并」分支（[path_router.rs:73-86](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/path_router.rs#L73-L86)），通过 `merge_for_path` 把 `post(b)` 合进已有 MethodRouter；后者是直接构造一个含两个方法的 MethodRouter。最终得到的 `Endpoint::MethodRouter` 等价，都能对 `GET /` 调 a、对 `POST /` 调 b。

## 5. 综合实践

把本讲三个最小模块串起来，做一个「带登记表」的小练习。

**任务**：

1. 新建一个 axum 项目，构造一个 `Router<()>`，满足：
   - 用 `route` 注册 `/` 和 `/users/{id}` 两条普通路由（`{id}` 的路径语法先照抄，细节在 u2-l2 讲）。
   - 用 `route_service` 注册 `/health`，挂一个 `service_fn` 返回 `"ok"`。
2. 在 `main` 里**克隆**一份 `app`（`let app2 = app.clone();`），再在**原 `app`** 上继续 `.route("/extra", get(...))`。
3. 运行后用 curl 验证：
   - 原服务的 `/`、`/users/42`、`/health`、`/extra` 都能正确响应。
   - 思考（不必运行验证）：若把 `app2` 也 `serve` 起来，它的 `/extra` 是否存在？为什么？

**验收要点**：

- 你能画出本应用请求的分发路径：`req → call_with_state → path_router 匹配成功 → Endpoint`；若都不匹配 → `catch_all_fallback(NotFound) → 404`。
- 你能用一句话解释第 2 步里「在 clone 后继续修改原 app」为什么是写时复制：`Arc::try_unwrap` 此时失败（`app2` 还持引用），`into_inner` 走 `Err` 分支 clone 内部。
- 你能在源码里指出 `RouterInner`（mod.rs:98-102）、`Endpoint`（mod.rs:786-790）、`new_route`（path_router.rs:139-144）三个关键位置。

> 提示：第 3 步的「思考题」答案是——`/extra` 只存在于 clone 之后被修改的那一份 Router 里；因为 `Arc::try_unwrap` 失败触发写时复制，`app2` 仍指向旧的、不含 `/extra` 的 `RouterInner`。这正是 Arc 共享 + 写时复制的直接体现。

## 6. 本讲小结

- `Router<S>` 是 axum 的对外路由类型，内部只有一个 `Arc<RouterInner<S>>` 字段；手写的 `Clone` 只克隆 Arc，所以运行时每条连接 clone Router 都极廉价。
- 修改 Router 走「写时复制」：`into_inner` 用 `Arc::try_unwrap` 尝试独占，独占失败才 clone 内部三块数据。`tap_inner!`/`map_inner!` 两个宏统一封装了「拆开-改-重包」的 builder 骨架。
- `RouterInner` 由三块组成：`path_router`（正路由表）、`default_fallback`（标记是否还是默认兜底）、`catch_all_fallback`（兜底处理器，默认是返回 404 的 `NotFound`）。
- 请求分发是「先正路由、后兜底」两段式：`call_with_state` 先问 `path_router`，未命中再把 `(req, state)` 交给 `catch_all_fallback`。
- `route()` 接收 `MethodRouter`，按方法分发、支持同路径合并；`route_service()` 接收任意 `Service<Request, Error=Infallible>`，方法无关、来者不拒，且会拒绝传入 `Router`（提示改用 `nest`）。
- 两条入口最终都把路由存成 `Endpoint`（`MethodRouter` 或 `Route` 变体），经 `PathRouter::new_route` 登记进 `routes: Vec` 与 matchit 树（`path → RouteId`）。

## 7. 下一步学习建议

本讲建立了 `Router` / `RouterInner` / 两条注册入口的心智模型。接下来按依赖顺序建议：

- **u2-l2 路径匹配与路径参数**：本讲反复提到的 `path_router` 内部的 matchit 树、`{id}`/`{*path}` 语法、`UrlParams` 如何写入 extensions，是下一讲的正题。
- **u2-l3 MethodRouter**：本讲把 `route` 的接收类型一笔带过为「MethodRouter」，下一讲拆解它如何为同一路径按方法分发、以及 405 与 `Allow` 头如何生成。
- **u2-l4 nest 与 merge**：本讲提到 `route_service` 拒绝 `Router` 并提示用 `nest`，以及 `Endpoint::Route` 的存在——这些在 nest/merge 讲义里展开。
- **u2-l5 Fallback 与 404**：本讲只讲了默认 `NotFound` 与 `Fallback::Default` 分支，自定义 fallback、`method_not_allowed_fallback` 等留到该讲。

> 阅读建议：在进入下一讲前，确保你能凭记忆画出 `Router → Arc<RouterInner> → {path_router, default_fallback, catch_all_fallback}` 这张结构图，并说清一个 404 请求是经过了哪几步。
