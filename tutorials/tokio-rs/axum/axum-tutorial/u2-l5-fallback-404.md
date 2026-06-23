# Fallback 与 404 处理

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 axum 里「路径没匹配」和「路径匹配了但方法没匹配」这两种情况分别返回什么默认响应。
- 区分 `Router::fallback`、`Router::fallback_service`、`Router::method_not_allowed_fallback` 三个 API 的适用场景，并能正确选用。
- 理解 `RouterInner` 内部 `catch_all_fallback` 字段、`default_fallback` 标记位、`NotFound` 服务三者如何协作产生默认 404。
- 掌握 `reset_fallback` 在合并路由（`merge`）时解决 fallback 冲突的用途。
- 能在源码里定位 `fallback_endpoint` 这个内部方法，解释它为什么要把 fallback 同时注册到 `/` 和 `/{*__private__axum_fallback}` 两条隐藏路径上。

## 2. 前置知识

本讲默认你已经读过前几讲，掌握了下面这些概念：

- **Router 与 RouterInner**：`Router<S>` 内部持有一个 `Arc<RouterInner<S>>`，`RouterInner` 由「正路由表 `path_router`」和「兜底处理器」组成（见 [u2-l1](u2-l1-router-and-routing.md)）。
- **路径匹配**：请求到来时，`PathRouter` 用 matchit 基数树对 URL path 做匹配；匹配不到时 matchit 返回 `MatchError::NotFound`（见 [u2-l2](u2-l2-path-matching-params.md)）。
- **方法路由**：同一条路径上可以注册多个 HTTP 方法的 handler，由 `MethodRouter` 持有 9 个方法槽位；方法没匹配时默认返回 `405 Method Not Allowed` 并附带 `Allow` 头（见 [u2-l3](u2-l3-method-router.md)）。
- **tower::Service**：axum 里的路由、handler、fallback 最终都被适配成 `Service<Request>`，`Service::call` 返回 `Future<Output = Result<Response, Error>>`。

一句话复习：一条请求进来后，先按**路径**匹配，路径命中后再按**方法**匹配。本讲关心的正是这两步「没命中」时 axum 的兜底行为——也就是用户看到的 404 和 405 是怎么来的、怎么自定义。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `axum/src/routing/mod.rs` | 定义 `Router`、`RouterInner`、`Fallback` 枚举，以及 `fallback` / `fallback_service` / `method_not_allowed_fallback` / `reset_fallback` 四个公共 API 和内部 `fallback_endpoint`。 |
| `axum/src/routing/not_found.rs` | 定义 `NotFound`——一个对所有请求都返回 `404 Not Found` 的最小 `Service`，是默认的 catch-all fallback。 |
| `axum/src/routing/path_router.rs` | `PathRouter::call_with_state` 在 matchit 匹配失败时返回 `Err`，把请求交给上层 fallback；`method_not_allowed_fallback` 在此遍历已注册的 `MethodRouter`。 |
| `axum/src/routing/method_routing.rs` | `MethodRouter` 内部也有一个 `fallback` 字段，默认返回 405；`default_fallback` 方法用于「仅在未自定义时」设置它。 |
| `axum/src/docs/routing/fallback.md` | `Router::fallback` 的用户文档，强调 fallback 只在「路径没匹配」时触发。 |
| `axum/src/docs/routing/method_not_allowed_fallback.md` | `Router::method_not_allowed_fallback` 的用户文档与示例。 |

## 4. 核心概念与源码讲解

### 4.1 默认的 404：NotFound 服务与 RouterInner 的 fallback 字段

#### 4.1.1 概念说明

当你写一个空的 `Router::new()` 而不加任何路由时，对任何请求 axum 都会返回 `404 Not Found`。这个行为不是「特殊判断」出来的，而是因为 axum 给每个 Router 都预置了一个**默认兜底服务**：`NotFound`。它是一个实现了 `Service` 的空结构体，`call` 时无条件返回 `StatusCode::NOT_FOUND`。

理解这一点很关键：**404 本身就是 fallback 的默认值**。所谓「自定义 404」，本质就是「换掉这个默认 fallback」。

#### 4.1.2 核心流程

默认 fallback 的产生流程：

1. `Router::new()` 构造 `RouterInner`，其中 `catch_all_fallback` 字段被初始化为 `Fallback::Default(Route::new(NotFound))`。
2. `default_fallback` 标记位被设为 `true`，表示「目前用的还是默认兜底」。
3. 请求到达后，路径匹配失败 → 进入 fallback 分支 → 调用 `NotFound` 的 `call` → 返回 404。

#### 4.1.3 源码精读

先看 `RouterInner` 的三个关键字段——本讲几乎所有行为都围绕它们展开：

[axum/src/routing/mod.rs:98-102](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L98-L102) —— `path_router` 是正路由表；`default_fallback` 是一个 bool 标记位，记录「catch_all_fallback 是否仍是默认值」；`catch_all_fallback` 才是真正存放兜底处理器的地方。

```rust
struct RouterInner<S> {
    path_router: PathRouter<S>,
    default_fallback: bool,
    catch_all_fallback: Fallback<S>,
}
```

`Router::new()` 把默认 fallback 装进去——注意它包了一层 `Fallback::Default`，这个变体在后面 `merge` 逻辑里有特殊待遇：

[axum/src/routing/mod.rs:162-170](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L162-L170) —— 默认 `catch_all_fallback` 就是 `Route::new(NotFound)`，`default_fallback = true`。

再看 `NotFound` 本身，它是整个 404 行为的最底层实现：

[axum/src/routing/not_found.rs:11-34](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/not_found.rs#L11-L34) —— `NotFound` 是个零大小结构体（`#[derive(Clone, Copy, Debug)]`），它的 `call` 直接 `ready(Ok(StatusCode::NOT_FOUND.into_response()))`。`type Error = Infallible` 表示它永远不会失败——这呼应了 axum 中间件「返回 `Infallible` 错误」的设计。

```rust
pub(super) struct NotFound;

impl<B> Service<Request<B>> for NotFound { /* ... */
    fn call(&mut self, _req: Request<B>) -> Self::Future {
        ready(Ok(StatusCode::NOT_FOUND.into_response()))
    }
}
```

而 `catch_all_fallback` 字段的类型 `Fallback<S>` 是一个三态枚举，承载 fallback 的三种来源：

[axum/src/routing/mod.rs:710-714](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L710-L714) —— `Default`（默认的 NotFound）、`Service`（用户通过 `fallback_service` 传入的 Service）、`BoxedHandler`（用户通过 `fallback` 传入的 handler，尚未物化成 Route）。

```rust
enum Fallback<S, E = Infallible> {
    Default(Route<E>),
    Service(Route<E>),
    BoxedHandler(BoxedIntoRoute<S, E>),
}
```

#### 4.1.4 代码实践

**实践目标**：亲手验证「空 Router 对任何请求都返回 404」，并确认这个 404 来自 `NotFound`。

**操作步骤**：

1. 新建一个 binary crate，写如下最小程序：

   ```rust
   // 示例代码
   use axum::{Router, routing::get};

   #[tokio::main]
   async fn main() {
       let app: Router = Router::new(); // 完全空的路由
       let listener = tokio::net::TcpListener::bind("0.0.0.0:3000").await.unwrap();
       axum::serve(listener, app).await.unwrap();
   }
   ```

2. `cargo run` 启动。
3. 用 curl 请求任意路径：`curl -i http://localhost:3000/whatever`。

**需要观察的现象**：响应状态码为 `404 Not Found`，body 为空。

**预期结果**：无论请求什么路径、什么方法，都得到 404。这正是 `NotFound` 服务在被触发。

**说明**：本步骤的结果**待本地验证**（取决于你本地是否安装了 Rust 工具链与 tokio）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `NotFound` 的 `type Error` 是 `Infallible` 而不是某个具体错误类型？

**参考答案**：因为 axum 的 Router/Service 链路对外承诺错误类型为 `Infallible`（不会产生需要向上传播的错误）。`NotFound` 只是返回一个 404 响应，这本身是「正常的响应」而非「错误」，所以用 `Infallible` 表达「这个 Service 永远成功」。

**练习 2**：`RouterInner` 里的 `default_fallback: bool` 和 `catch_all_fallback: Fallback<S>` 看起来信息重复——既然 `Fallback::Default` 已经表示「默认」，为什么还要一个单独的 bool？

**参考答案**：这个 bool 主要服务于 `merge` 时的冲突检测与「默认 fallback 让位给自定义 fallback」的优先级判断（见 4.4 节）。它让代码可以用简单的 `match (this.default_fallback, default_fallback)` 表达「两个都是默认」「一个默认一个自定义」「两个都自定义」三种情况，而不必每次去比对 `Fallback` 变体。

---

### 4.2 自定义「路径未匹配」：fallback / fallback_service / fallback_endpoint

#### 4.2.1 概念说明

默认的 404 太单薄（空 body、无 JSON）。`Router::fallback(handler)` 让你换掉它：**当没有任何路由匹配请求路径时**，改调用你给的 handler。它和 `fallback_service` 的关系，就像 `route` 和 `route_service` 的关系——前者接 handler（`async fn`），后者接任意 `Service<Request>`。

需要特别强调的是 fallback 的**触发边界**（来自官方文档 [fallback.md](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/docs/routing/fallback.md)）：

- 只有「路径完全没匹配」才触发 fallback。
- 如果 handler 匹配了但**自己返回了 404**，fallback **不会**被调用——fallback 不是「404 响应的拦截器」。
- 如果路径匹配了但**方法没匹配**（405 场景），`Router::fallback` 也**不会**触发，那属于 4.3 节的 `method_not_allowed_fallback`。

#### 4.2.2 核心流程

设置 fallback 时，axum 做了两件事（这是本讲最容易被忽略、也最关键的内部细节）：

1. **更新 `catch_all_fallback` 字段**：把传入的 handler 存为 `Fallback::BoxedHandler`（或 `fallback_service` 存为 `Fallback::Service`），并把 `default_fallback` 标记位置为 `false`。
2. **通过 `fallback_endpoint` 注册两条隐藏路由**：在 matchit 树里同时注册 `/` 和 `/{*__private__axum_fallback}` 两个路径，都指向同一个 fallback 处理器。

为什么要注册成路由？因为 axum 希望复用 matchit 已经做好的高效路径匹配，而不是另写一套 fallback 查找。这样绝大多数「路径没匹配」的请求，其实是被 matchit 匹配到了那条隐藏的通配路由上。

请求分发时：

- `Router::call_with_state` 先调用 `path_router.call_with_state`。
- 若 matchit 命中（包括命中隐藏的 fallback 通配路由）→ 返回 `Ok(future)`，直接用。
- 若 matchit 真的返回 `MatchError::NotFound`（例如 CONNECT 请求带空路径等边缘情况）→ 返回 `Err`，再回退到 `catch_all_fallback` 字段。

#### 4.2.3 源码精读

先看两个公共 API。`fallback` 接 handler，`fallback_service` 接 Service：

[axum/src/routing/mod.rs:334-362](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L334-L362) —— `fallback` 把 handler 存进 `catch_all_fallback`（`Fallback::BoxedHandler`），然后调用 `fallback_endpoint` 注册隐藏路由。`fallback_service` 走 `Fallback::Service` 分支，逻辑对称。

```rust
pub fn fallback<H, T>(self, handler: H) -> Self
where H: Handler<T, S>, T: 'static,
{
    tap_inner!(self, mut this => {
        this.catch_all_fallback =
            Fallback::BoxedHandler(BoxedIntoRoute::from_handler(handler.clone()));
    })
    .fallback_endpoint(Endpoint::MethodRouter(any(handler))) // 注意：用 any() 包一层
}
```

注意 `fallback` 内部用 `any(handler)` 把 handler 包成了一个「接受所有方法」的 `MethodRouter`，再传给 `fallback_endpoint`。这保证 fallback 不受 HTTP 方法限制。

接下来是核心的 `fallback_endpoint`——这是本讲三个最小模块之一。它做了「注册两条隐藏路由 + 关闭默认标记」：

[axum/src/routing/mod.rs:391-441](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L391-L441) —— 分别向 `path_router` 注册 `/` 和 `FALLBACK_PARAM_PATH`（即 `/{*__private__axum_fallback}`）。每条都套了一个中间层：进入时移除 `MatchedPath` 扩展（fallback 不算「真正命中的路由」，不应上报 matched path），再用 `oneshot_inner_owned` 调用真正的 fallback Route。最后把 `default_fallback` 置为 `false`。

```rust
_ = this.path_router.route_endpoint("/", endpoint.clone().layer(...));
_ = this.path_router.route_endpoint(FALLBACK_PARAM_PATH, endpoint.layer(...));
this.default_fallback = false;
```

这里用到的 `FALLBACK_PARAM_PATH` 常量定义在这里：

[axum/src/routing/mod.rs:126-127](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L126-L127) —— 这就是上一篇 [u2-l2](u2-l2-path-matching-params.md) 提到的「内部隐藏通配参数」。fallback 借用它实现「匹配任意剩余路径」。

```rust
pub(crate) const FALLBACK_PARAM: &str = "__private__axum_fallback";
pub(crate) const FALLBACK_PARAM_PATH: &str = "/{*__private__axum_fallback}";
```

最后看请求分发时 fallback 字段如何兜底：

[axum/src/routing/mod.rs:452-462](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L452-L462) —— `path_router.call_with_state` 返回 `Ok` 就直接用；返回 `Err((req, state))`（matchit NotFound）才落到 `catch_all_fallback.call_with_state`。

```rust
pub(crate) fn call_with_state(&self, req: Request, state: S) -> RouteFuture<Infallible> {
    let (req, state) = match self.inner.path_router.call_with_state(req, state) {
        Ok(future) => return future,
        Err((req, state)) => (req, state),
    };
    self.inner.catch_all_fallback.clone().call_with_state(req, state)
}
```

而 `path_router.call_with_state` 在 matchit 返回 `NotFound` 时把请求「原样退还」给上层：

[axum/src/routing/path_router.rs:342-371](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/path_router.rs#L342-L371) —— `Err(MatchError::NotFound) => Err((Request::from_parts(parts, body), state))`，这正是把控制权交回 `Router::call_with_state` 去触发 `catch_all_fallback` 的地方。

> 小结这套设计的双重保险：**大部分未匹配请求**走 matchit 命中的隐藏通配路由（快路径）；**matchit 真正 NotFound 的边缘情况**（如空路径的 CONNECT 请求）才走 `catch_all_fallback` 字段（慢路径）。两者指向同一个 handler，所以行为一致。源码注释也印证了这一点——`nest` 时特意丢弃子路由的 `catch_all_fallback`，注释写道「它只用于 CONNECT 空路径请求」。

#### 4.2.4 代码实践

**实践目标**：给应用加一个返回 JSON 错误的全局 fallback，并验证「handler 自己返回 404 不会触发 fallback」。

**操作步骤**：

1. 编写如下程序（示例代码）：

   ```rust
   // 示例代码
   use axum::{http::StatusCode, response::IntoResponse, routing::get, Json, Router};
   use serde_json::json;

   async fn hello() -> &'static str { "hi" }

   // 这个 handler 自己返回 404
   async fn manual_404() -> (StatusCode, &'static str) {
       (StatusCode::NOT_FOUND, "I decided this is missing")
   }

   // 全局 fallback：返回 JSON
   async fn fallback() -> impl IntoResponse {
       (StatusCode::NOT_FOUND, Json(json!({"error": "route not found"})))
   }

   #[tokio::main]
   async fn main() {
       let app: Router = Router::new()
           .route("/hello", get(hello))
           .route("/missing", get(manual_404))
           .fallback(fallback);
       let listener = tokio::net::TcpListener::bind("0.0.0.0:3000").await.unwrap();
       axum::serve(listener, app).await.unwrap();
   }
   ```

2. `cargo run`，分别请求三个 URL：
   - `curl -i http://localhost:3000/hello`
   - `curl -i http://localhost:3000/does-not-exist`
   - `curl -i http://localhost:3000/missing`

**需要观察的现象**：
- `/hello` → 200。
- `/does-not-exist` → 404，body 是 `{"error":"route not found"}`（**来自 fallback**）。
- `/missing` → 404，body 是 `I decided this is missing`（**来自 handler 自己，不是 fallback**）。

**预期结果**：第三种情况能直观证明「fallback 只在路径没匹配时触发，handler 内部返回 404 不会被 fallback 接管」。

**说明**：以上运行结果**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `fallback_endpoint` 里注册 `/` 的那行删掉（只保留 `/{*__private__axum_fallback}`），哪个请求会出问题？

**参考答案**：访问根路径 `GET /` 可能出问题。matchit 的通配 `/{*x}` 主要匹配「斜杠后有内容」的路径；为了让根路径 `/` 也能被 fallback 兜住，axum 额外注册了精确的 `/`。所以两条注册互为补充，缺一不可。

**练习 2**：为什么 `fallback` 要用 `any(handler)` 把 handler 包成 `MethodRouter` 再注册，而不是直接注册成一个 `Route`？

**参考答案**：因为 fallback 必须对**所有 HTTP 方法**生效（不管是 GET、POST 还是 PUT）。`any()` 产生的 `MethodRouter` 不限方法，并且其 `AllowHeader` 为 `Skip`（见 [u2-l3](u2-l3-method-router.md)），不会在 fallback 响应里附加多余的 `Allow` 头。

---

### 4.3 「方法未匹配」：method_not_allowed_fallback 与 MethodRouter 的 405 兜底

#### 4.3.1 概念说明

「路径匹配了，但方法没匹配」是另一类常见情况。比如 `/items` 只注册了 `get(handler)`，你却发了一个 `POST /items`。axum 默认返回 `405 Method Not Allowed` 并附 `Allow` 头（见 [u2-l3](u2-l3-method-router.md)）。

这个 405 **不是** `Router::fallback` 管的（4.2 节强调过，路径已匹配就不会走 catch-all fallback）。它来自 `MethodRouter` **自身**的 `fallback` 字段——每个 `MethodRouter` 都自带一个方法级兜底，默认就是返回 405。

axum 提供两个层级来自定义它：

- **`Router::method_not_allowed_fallback(handler)`**：一次性给 Router 里**所有已注册的** `MethodRouter` 设置方法级 fallback。
- **`MethodRouter::fallback(handler)`**：只给某一条路径的 `MethodRouter` 单独设置。

#### 4.3.2 核心流程

默认 405 的产生流程：

1. 路径匹配成功，进入某个 `MethodRouter::call_with_state`。
2. 逐个方法槽位比较（GET、POST、PUT…），都没命中。
3. 落到 `MethodRouter` 的 `fallback` 字段——默认是一个返回 `StatusCode::METHOD_NOT_ALLOWED` 的 `service_fn`。
4. `RouteFuture::poll` 阶段补写 `Allow` 头（见 u2-l3）。

`Router::method_not_allowed_fallback` 的作用流程：

1. 调用 `PathRouter::method_not_allowed_fallback`。
2. 它遍历**当前已注册的所有** `Endpoint::MethodRouter`。
3. 对每个调用 `MethodRouter::default_fallback(handler)`——注意是 `default_fallback`，意思是「只有当该方法路由还没自定义 fallback 时才设置」，避免覆盖用户单独配置的 `MethodRouter::fallback`。

#### 4.3.3 源码精读

先看 `MethodRouter` 默认的 405 兜底是怎么造出来的：

[axum/src/routing/method_routing.rs:797-817](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L797-L817) —— `MethodRouter::new()` 的 `fallback` 字段是一个 `service_fn`，直接返回 `StatusCode::METHOD_NOT_ALLOWED`，包成 `Fallback::Default`。这就是 405 的源头。

```rust
pub fn new() -> Self {
    let fallback = Route::new(service_fn(|_: Request| async {
        Ok(StatusCode::METHOD_NOT_ALLOWED)
    }));
    Self { /* 9 个方法槽位全为 None */ fallback: Fallback::Default(fallback), ... }
}
```

方法没匹配时，分发逻辑会落到这个 `fallback`：

[axum/src/routing/method_routing.rs:1167-1222](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L1167-L1222) —— 先用 `call!` 宏逐个方法槽位尝试（注意 HEAD 会先试 head 槽位再试 get 槽位，这是 HEAD 回退 GET 的根因），全部落空后 `fallback.clone().call_with_state(req, state)`，再根据 `allow_header` 给响应补上 `Allow` 头。

```rust
let future = fallback.clone().call_with_state(req, state);
match allow_header {
    AllowHeader::None => future.allow_header(Bytes::new()),
    AllowHeader::Skip => future,
    AllowHeader::Bytes(allow_header) => future.allow_header(allow_header.clone().freeze()),
}
```

再看自定义入口。`Router::method_not_allowed_fallback` 只是转发：

[axum/src/routing/mod.rs:364-375](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L364-L375) —— 转给 `path_router.method_not_allowed_fallback(&handler)`。

真正的遍历逻辑在 `PathRouter`：

[axum/src/routing/path_router.rs:95-105](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/path_router.rs#L95-L105) —— `for endpoint in self.routes.iter_mut()` 遍历**当前所有已注册路由**，只对 `Endpoint::MethodRouter` 调用 `default_fallback`。

```rust
pub(super) fn method_not_allowed_fallback<H, T>(&mut self, handler: &H)
where H: Handler<T, S>, T: 'static,
{
    for endpoint in self.routes.iter_mut() {
        if let Endpoint::MethodRouter(rt) = endpoint {
            *rt = rt.clone().default_fallback(handler.clone());
        }
    }
}
```

⚠️ **两个关键细节**（直接影响你写代码的顺序）：

1. **只影响「已注册」的路由**：因为它遍历的是 `self.routes`。如果你在调用 `method_not_allowed_fallback` **之后**才 `.route(...)` 注册新路由，那些新路由不会自动获得这个 fallback。官方文档原话：「Sets a fallback on all **previously registered** `MethodRouter`s」。所以这个 API 通常放在所有 `.route(...)` 之后调用。

2. **不覆盖已有的自定义 fallback**：`default_fallback` 只在 `Fallback::Default` 时才替换：

[axum/src/routing/method_routing.rs:711-722](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L711-L722) —— `match self.fallback { Fallback::Default(_) => self.fallback(handler), _ => self }`。如果你已经对某个 `MethodRouter` 调过 `.fallback()`，全局的 `method_not_allowed_fallback` 会跳过它。

#### 4.3.4 代码实践

**实践目标**：让「方法未匹配」返回和「路径未匹配」**不同**的响应，验证两者互不干扰。

**操作步骤**：

1. 编写如下程序（示例代码，对应官方 [method_not_allowed_fallback.md](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/docs/routing/method_not_allowed_fallback.md) 的结构）：

   ```rust
   // 示例代码
   use axum::{http::StatusCode, response::IntoResponse, routing::get, Json, Router};
   use serde_json::json;

   async fn hello() -> &'static str { "hello\n" }

   async fn default_fallback() -> impl IntoResponse {
       (StatusCode::NOT_FOUND, Json(json!({"kind": "path_not_found"})))
   }

   async fn handle_405() -> impl IntoResponse {
       (StatusCode::METHOD_NOT_ALLOWED, Json(json!({"kind": "method_not_allowed"})))
   }

   #[tokio::main]
   async fn main() {
       let app: Router = Router::new()
           .route("/", get(hello))
           .fallback(default_fallback)
           .method_not_allowed_fallback(handle_405); // 注意：放在 .route 之后
       let listener = tokio::net::TcpListener::bind("0.0.0.0:3000").await.unwrap();
       axum::serve(listener, app).await.unwrap();
   }
   ```

2. `cargo run`，发三个请求：
   - `curl -i http://localhost:3000/`（正确方法 GET）
   - `curl -i -X POST http://localhost:3000/`（路径存在，方法错）
   - `curl -i http://localhost:3000/nope`（路径不存在）

**需要观察的现象**：
- `GET /` → 200，body `hello`。
- `POST /` → **405**，body `{"kind":"method_not_allowed"}`，并带 `Allow: GET` 头。
- `GET /nope` → **404**，body `{"kind":"path_not_found"}`。

**预期结果**：404 与 405 走两个完全不同的 fallback，互不混淆。这正是 `Router::fallback`（路径维度）与 `Router::method_not_allowed_fallback`（方法维度）的分界线。

**说明**：以上结果**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：把上面例子里 `.method_not_allowed_fallback(handle_405)` 移到 `.route("/", get(hello))` **之前**，会发生什么？

**参考答案**：`POST /` 不再返回 `handle_405` 的 JSON，而是回到默认的 405（空 body + Allow 头）。因为 `method_not_allowed_fallback` 只作用于调用时**已注册**的路由；放在 `.route` 之前，此时 `self.routes` 还是空的，没有任何 `MethodRouter` 被设置。这是初学者最容易踩的顺序坑。

**练习 2**：如果某条路由我想要一个**特殊**的方法未匹配响应，不想被全局 `method_not_allowed_fallback` 覆盖，怎么办？

**参考答案**：在该路由的 `MethodRouter` 上单独调用 `.fallback(your_handler)`，例如 `.route("/", get(hello).fallback(special))`。由于 `default_fallback` 只替换 `Fallback::Default`，单独设置过的会被跳过，从而保留你的自定义。

---

### 4.4 reset_fallback 与 merge 的 fallback 冲突

#### 4.4.1 概念说明

当你用 `Router::merge` 把两个 Router 合并时，fallback 会遇到一个自然的问题：两个 Router 可能**各自都设置了自定义 fallback**，合并后该用哪个？axum 的回答是——**直接 panic**，拒绝替你做选择。

这个设计逼迫你显式表态：用 `reset_fallback` 把其中一个 Router 的 fallback 重置回默认（NotFound），再合并。这就引出本节的核心：`reset_fallback` 存在的唯一目的，就是配合 `merge`。

#### 4.4.2 核心流程

`merge` 的 fallback 处理流程：

1. 解构出 `other` 的 `default_fallback` 标记和 `catch_all_fallback`。
2. 用 `(this.default_fallback, default_fallback)` 二元组判断：
   - other 是默认 → 保持 this 不变。
   - this 是默认、other 自定义 → 采用 other 的。
   - **两个都自定义 → panic**。
3. 再调 `Fallback::merge` 做同样的二次校验（双保险），若返回 `None` 同样 panic。

`reset_fallback` 的流程很简单：把 `default_fallback` 设回 `true`，`catch_all_fallback` 设回 `Fallback::Default(Route::new(NotFound))`。这样这个 Router 在 merge 时就会被当作「没有自定义 fallback」的一方。

#### 4.4.3 源码精读

`merge` 里的冲突检测：

[axum/src/routing/mod.rs:269-293](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L269-L293) —— 先按 `default_fallback` bool 做粗判，再用 `Fallback::merge` 做细判，两层都拒绝「两个都自定义」。

```rust
match (this.default_fallback, default_fallback) {
    (_, true) => {}                 // other 默认，保持 this
    (true, false) => { this.default_fallback = false; } // this 默认，采用 other
    (false, false) => panic!("Cannot merge two `Router`s that both have a fallback"),
};
this.catch_all_fallback = this.catch_all_fallback
    .merge(catch_all_fallback)
    .unwrap_or_else(|| panic!("Cannot merge two `Router`s that both have a fallback"));
```

配套的 `Fallback::merge` 语义——「只要有一方是 `Default`，就采用另一方」：

[axum/src/routing/mod.rs:720-727](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L720-L727) —— `(Self::Default(_), pick) | (pick, Self::Default(_)) => Some(pick)`，其余返回 `None`。

```rust
fn merge(self, other: Self) -> Option<Self> {
    match (self, other) {
        (Self::Default(_), pick) | (pick, Self::Default(_)) => Some(pick),
        _ => None,
    }
}
```

而 `reset_fallback` 就是「主动把一方变回 `Default`」的开关：

[axum/src/routing/mod.rs:377-389](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L377-L389) —— 注意它**只重置 `catch_all_fallback`**，不碰方法级的 `method_not_allowed_fallback`（后者存在各个 `MethodRouter` 内部，merge 时随路由一起搬过去）。

```rust
pub fn reset_fallback(self) -> Self {
    tap_inner!(self, mut this => {
        this.default_fallback = true;
        this.catch_all_fallback = Fallback::Default(Route::new(NotFound));
    })
}
```

> 补充对比：与 `merge` 不同，`nest` **不继承**子路由的 `catch_all_fallback`（见 [u2-l4](u2-l4-nest-and-merge.md)），所以嵌套时不会触发这个冲突——这也是 nest 与 merge 在 fallback 语义上的一个重要差异。

#### 4.4.4 代码实践

**实践目标**：复现「两个带自定义 fallback 的 Router 不能直接 merge」的 panic，并用 `reset_fallback` 解决。

**操作步骤**：

1. 先写一段**会 panic** 的代码（示例代码），运行观察：

   ```rust
   // 示例代码（运行会 panic）
   use axum::{routing::get, Router};

   async fn fa() -> &'static str { "A" }
   async fn fb() -> &'static str { "B" }

   fn main() {
       let a = Router::new().route("/a", get(fa)).fallback(fa);
       let b = Router::new().route("/b", get(fb)).fallback(fb);
       let _app = a.merge(b); // 💥 panic: Cannot merge two `Router`s that both have a fallback
   }
   ```

2. 修改为用 `reset_fallback` 丢弃 b 的 fallback：

   ```rust
   // 示例代码（修复后）
   let _app = a.merge(b.reset_fallback()); // b 的 fallback 被重置为默认 NotFound，merge 成功
   ```

**需要观察的现象**：
- 第 1 步运行时，程序在构造阶段直接 panic，打印 `Cannot merge two \`Router\`s that both have a fallback`。
- 第 2 步正常运行；此时未匹配路径会走 a 的 `fallback(fa)`（因为 b 被重置成了默认，merge 时 a 的自定义胜出）。

**预期结果**：理解「自定义 fallback 在 merge 时是互斥的」，且 `reset_fallback` 是解除互斥的官方手段。

**说明**：panic 信息**待本地验证**（具体措辞以源码常量为准）。

#### 4.4.5 小练习与答案

**练习 1**：`reset_fallback` 重置后，该 Router 上之前用 `method_not_allowed_fallback` 设置的方法级兜底还在吗？

**参考答案**：还在。`reset_fallback` 只重置 `catch_all_fallback`（路径维度），完全不触碰各 `MethodRouter` 内部的方法级 fallback。所以方法级自定义会随路由一起保留并参与 merge。

**练习 2**：为什么 `Fallback::merge` 用 `Option<Self>` 作返回值，而不是直接 panic？

**参考答案**：把「能否合并」表达成 `Option` 让调用方（`Router::merge`）可以先用 `default_fallback` bool 做一次粗判、再借 `unwrap_or_else(panic)` 给出统一且带上下文的 panic 信息。这种「数据层返回 Option、业务层决定如何报错」的分层，让 `Fallback::merge` 保持纯粹、可测试。

---

## 5. 综合实践

把本讲的三类 fallback 串起来，构建一个**带错误分级**的小应用。

**任务**：实现一个 Router，满足：

1. 注册 `GET /api/users` 与 `POST /api/users` 两个路由。
2. **路径未匹配**（如 `GET /api/orders`）→ 返回 `404` + `{"error": "not found", "path": "<实际路径>"}`。提示：fallback handler 可以用 `axum::http::Uri` 提取器拿到请求路径。
3. **方法未匹配**（如 `DELETE /api/users`）→ 返回 `405` + `{"error": "method not allowed"}`，并保留默认的 `Allow` 头。
4. 把 `users` 路由封装成一个子 Router，再 `nest` 到 `/api`，验证 nest 后全局 fallback 仍对 `/api/xxx` 之外的路径生效（回顾 [u2-l4](u2-l4-nest-and-merge.md)：nest 不继承子路由的 catch_all_fallback，所以全局 fallback 会冒泡）。

**参考实现骨架**（示例代码）：

```rust
// 示例代码
use axum::{http::{StatusCode, Uri}, response::IntoResponse, routing::{get, post}, Json, Router};
use serde_json::json;

async fn list_users() -> &'static str { "[]" }
async fn create_user() -> &'static str { "created" }

async fn path_fallback(uri: Uri) -> impl IntoResponse {
    (StatusCode::NOT_FOUND, Json(json!({"error":"not found","path": uri.path()})))
}
async fn method_fallback() -> impl IntoResponse {
    (StatusCode::METHOD_NOT_ALLOWED, Json(json!({"error":"method not allowed"})))
}

#[tokio::main]
async fn main() {
    let users = Router::new()
        .route("/users", get(list_users).post(create_user));

    let app: Router = Router::new()
        .nest("/api", users)
        .fallback(path_fallback)
        .method_not_allowed_fallback(method_fallback); // 必须在 .nest/.route 之后

    let listener = tokio::net::TcpListener::bind("0.0.0.0:3000").await.unwrap();
    axum::serve(listener, app).await.unwrap();
}
```

**自测清单**（**待本地验证**）：
- `GET /api/users` → 200。
- `DELETE /api/users` → 405 + method_fallback 的 JSON + `Allow` 头含 `GET, POST`。
- `GET /api/unknown` → 404 + path_fallback 的 JSON，`path` 字段为 `/api/unknown`。
- `GET /totally/missing` → 404 + path_fallback（证明全局 fallback 在 nest 之外也生效）。

## 6. 本讲小结

- **404 是 fallback 的默认值**：`Router::new()` 的 `catch_all_fallback` 默认是 `NotFound`——一个对所有请求返回 `StatusCode::NOT_FOUND` 的零大小 `Service`。
- **三类 fallback 分属两个维度**：`Router::fallback` / `fallback_service` 管「路径未匹配」（catch-all）；`Router::method_not_allowed_fallback` 与 `MethodRouter::fallback` 管「方法未匹配」（405）。
- **`fallback_endpoint` 的双注册技巧**：自定义 fallback 会同时注册到 `/` 和 `/{*__private__axum_fallback}` 两条隐藏路径，复用 matchit 的高效匹配；`catch_all_fallback` 字段只兜 matchit 真正 NotFound 的边缘情况（如空路径 CONNECT）。
- **`method_not_allowed_fallback` 有两个隐含规则**：只作用于调用时**已注册**的路由（要放在 `.route` 之后）；且不覆盖已单独 `.fallback()` 过的 `MethodRouter`（靠 `default_fallback` 的 `Default` 判断）。
- **merge 拒绝两个自定义 fallback**：直接 panic，需用 `reset_fallback` 把一方重置回默认 `NotFound` 后再合并；`reset_fallback` 只影响路径维度的 `catch_all_fallback`，不影响方法级 fallback。
- **`Fallback` 三态枚举**（`Default`/`Service`/`BoxedHandler`）是理解 merge 优先级与 `with_state` 物化的钥匙。

## 7. 下一步学习建议

本讲讲完了 Router 层的注册、匹配、嵌套与兜底（u2 全部）。接下来进入 **u3 提取器机制**，你将看到这些 fallback 路径上 handler 的参数（如综合实践里的 `Uri`）是如何被 `FromRequestParts` / `FromRequest` 自动填充的。建议：

- 先读 [u3-l1 FromRequestParts 与 FromRequest 双 trait 机制](u3-l1-fromrequest-traits.md)，建立提取器的总框架。
- 回头看本讲 `fallback(handler)` 里 handler 的 `Uri` 参数，它就是一个 `FromRequestParts` 提取器——本讲是它的第一个真实用例。
- 若想深入 fallback 在请求生命周期里和「匹配路径」`MatchedPath` 的交互（注意 `fallback_endpoint` 里特意移除了 `MatchedPath`），可在学完 u3 后回来重读 [mod.rs:391-441](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L391-L441) 这一节源码。
