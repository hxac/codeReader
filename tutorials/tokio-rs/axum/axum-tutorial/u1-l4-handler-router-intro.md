# Handler 与 Router 初体验

## 1. 本讲目标

在前几讲里，你已经能跑起一个最小 axum 程序，也知道了 axum 是「hyper 之上薄薄一层」。本讲要把这层「薄纱」的编织方式讲清楚：**一条请求到底是怎么被 Router 接住、又怎么被交给一个 async 函数去处理的**。

读完本讲，你应该能做到：

1. 说清 `Router<S>` 里那个泛型 `S` 的含义，以及为什么只有 `Router<()>` 才能交给 `axum::serve`。
2. 说清 `Handler` trait 是如何把一个普通的 `async fn` 适配成 tower 的 `Service` 的，并解释**为什么 handler 必须写成 async**。
3. 区分两个容易混淆的概念：**handler**（你的业务函数）与 **method router**（`get`/`post` 这类函数产生的「方法 → handler」映射）。
4. 能写出带 `State`、`Path` 参数并返回 `Json` 的 handler，并能对照 `handlers_intro.md` 把每个参数归类为「提取器」或「返回值」。

## 2. 前置知识

本讲默认你已经掌握前三讲的内容。为了行文连贯，这里只做最简回顾，不展开：

- **请求生命周期**（来自 u1-l1）：一条请求从 TCP 进入，经 hyper 解析成 `http::Request`，由 axum 路由匹配到某个 handler，handler 的返回值经 `IntoResponse` 写回 TCP。
- **最小闭环**（来自 u1-l2）：`Router::new().route("/", get(handler))` 组装出一个 `Router`，再由 `axum::serve(listener, app)` 启动。其中 `get(handler)` 就是本讲要拆解的「方法路由」。
- **feature 开关**（来自 u1-l3）：`Json` 需要 `json` feature，`axum::serve` 需要 `tokio` 且（`http1` 或 `http2`）。本讲的代码实践会用到 `Json`，请确保开启了默认 feature。

还需要两个本讲会反复用到、但尚未细讲的概念（后续第 3、5 单元会深入）：

- **提取器（extractor）**：实现了 `FromRequest` 或 `FromRequestParts` 的类型，作为 handler 的参数出现。axum 会在调用 handler 之前，自动「提取」出这些值。`State`、`Path`、`Query`、`Json` 都是提取器。
- **`tower::Service`**：一个 `async fn(&mut Request) -> Result<Response, Error>` 的抽象。axum 的所有可调度单元（Router、handler、中间件）最终都是 `Service`。这是 axum 「不自造中间件」哲学的根基（u1-l1 已讲）。

> 术语约定：本讲把 `async fn` 形式的业务函数叫「handler 函数」，把 `Handler` trait 叫「`Handler` trait」，把 `get`/`post` 等返回的东西叫「方法路由（method router）」。三者关系是本讲的核心。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [axum/src/routing/mod.rs](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs) | 定义 `Router<S>` 及其 `route`/`with_state` 等公共 API，是路由系统的「外壳」。 |
| [axum/src/handler/mod.rs](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/handler/mod.rs) | 定义 `Handler<T, S>` trait 及其 blanket impl，是「async fn → Service」的适配层。 |
| [axum/src/routing/method_routing.rs](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs) | 定义 `MethodRouter`、`get`/`post`/`on` 等函数，是 handler 与 Router 之间的「方法层」桥梁。 |
| [axum/src/docs/handlers_intro.md](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/docs/handlers_intro.md) | handler 的官方一句话定义，被直接 `include_str!` 进模块文档。 |

本讲围绕三个最小模块展开：**Router**、**Handler**、**method_routing::get**。它们正好对应「请求被接住 → 被适配 → 被分发到正确方法」这条链路。

## 4. 核心概念与源码讲解

### 4.1 Router：路由容器与 `Router<S>` 的泛型 S

#### 4.1.1 概念说明

`Router` 是 axum 暴露给用户的「路由容器」。你在前两讲已经用过它：`Router::new().route(路径, 方法路由)`。但有两个细节我们一直没有展开，而它们正是理解 axum 的关键：

1. **`Router` 用 `Arc` 包裹内部数据**，因此 `Router` 是 `Clone` 的，且克隆代价极低（只增加一次引用计数）。这让一个 `Router` 可以被同时分发给多个 hyper 连接任务。
2. **`Router` 带一个泛型 `S`**，写作 `Router<S>`。这个 `S` 不是「路由持有的状态值」，而是「**路由还缺少的状态类型**」。这是 axum 最巧妙的设计之一，本讲先建立直觉，精确机制留到第 9 单元（with_state 类型状态模式）讲。

#### 4.1.2 核心流程

一个 `Router<S>` 的内部结构可以用下面的伪代码描述：

```
Router<S> {
    inner: Arc<RouterInner<S>>   // 共享指针，Clone 廉价
}

RouterInner<S> {
    path_router:        PathRouter<S>,   // 「路径 → 方法路由」的匹配树（本讲先当成黑盒）
    default_fallback:   bool,            // 是否还在用内置默认 404 fallback
    catch_all_fallback: Fallback<S>,     // 所有路径都匹配失败时调用的兜底服务
}
```

注册一条路由时，`Router::route(path, method_router)` 把「某条路径」与「一个方法路由」记录到 `path_router` 里。处理一条请求时，`Router` 先用请求的 URL 去 `path_router` 里查；查不到就走 `catch_all_fallback`（默认是返回 404 的 `NotFound`）。

> 本讲聚焦 `route` 与 `with_state` 两个 API。`path_router` 的路径匹配机制（matchit 树、`{id}` 捕获）是下一讲（u2-l2）的主题；fallback 是 u2-l5 的主题。

#### 4.1.3 源码精读

`Router` 的定义只有三行，但信息量很大：

[axum/src/routing/mod.rs:L78-L88](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L78-L88) — 这段文档和结构定义说明了两件事：第一，`Router<S>` 表示「**缺少**一个类型为 `S` 的状态才能处理请求」，因此**只有 `Router<()>`（即不缺状态）才能传给 `serve`**；第二，`inner: Arc<RouterInner<S>>` 解释了为什么 `Router` 克隆廉价。

`RouterInner` 的三个字段：

[axum/src/routing/mod.rs:L98-L102](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L98-L102) — `path_router` 是真正的匹配树，`catch_all_fallback` 是兜底逻辑。

`Router::new()` 创建一个空 Router，并预置默认 404 fallback：

[axum/src/routing/mod.rs:L162-L170](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L162-L170) — 注意 `catch_all_fallback: Fallback::Default(Route::new(NotFound))`，这就是「空 Router 对所有请求返回 404」的来源。

`route` 方法本身极其简短——它把真正的注册工作委托给 `path_router`：

[axum/src/routing/mod.rs:L190-L196](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L190-L196) — `panic_on_err!` 表示如果新路由与已有路由冲突（比如重复注册同一个静态路径），会在构造 Router 时直接 panic，而不是把错误推迟到运行时。这与 `route.md` 文档里「Panics if the route overlaps」的说明一致。

而把 `Router<S>` 变成 `Router<()>` 的关键方法是 `with_state`：

[axum/src/routing/mod.rs:L443-L450](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L443-L450) — 它把一份 `state: S` 灌进 `path_router` 和 `catch_all_fallback`，并返回一个 `Router<S2>`（通常 `S2 = ()`）。这就是「填充缺失状态」的动作。注意它用了 `map_inner!` 宏，本质是「取出独占内部 → 改 → 重新包成 `Arc`」，所以 `with_state` 会产生一个新的 `Router`，不破坏旧的。

#### 4.1.4 代码实践

1. **实践目标**：亲手验证「只有 `Router<()>` 能编译」，并体会 `with_state` 的类型变换。
2. **操作步骤**：
   - 在你本地依赖 axum 的项目（沿用 u1-l2 的工程）里，写下两段代码并对比编译结果。

   ```rust
   // 代码 A：缺少状态，编译不过
   #[derive(Clone)]
   struct AppState;
   let app: Router<AppState> = Router::new().route("/", axum::routing::get(|| async {}));
   // axum::serve(listener, app).await.unwrap(); // ❌ 编译错误：期望 Router<()>，得到 Router<AppState>
   ```

   ```rust
   // 代码 B：调用 with_state 填充状态后，类型变成 Router<()>，可以通过
   let app: Router<AppState> = Router::new().route("/", axum::routing::get(|| async {}));
   let app_ready: Router<()> = app.with_state(AppState);
   // axum::serve(listener, app_ready).await.unwrap(); // ✅
   ```

3. **需要观察的现象**：去掉 `with_state` 直接把 `Router<AppState>` 传给 `serve` 时，编译器会报一个类型不匹配的错误，提示期望 `Router<()>`。加上 `with_state(AppState)` 后错误消失。
4. **预期结果**：你能用一句话概括——`Router<S>` 里的 `S` 是「**待填充**」的状态类型；`with_state` 把它「填满」成 `()`。
5. 如本地未配置 Rust 工具链，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`Router` 为什么用 `Arc<RouterInner>` 包裹，而不是直接持有 `RouterInner`？

> **参考答案**：因为 `tower::Service::call` 要求 `&self`（共享引用）来处理请求，而 hyper 会为每条 TCP 连接 spawn 一个独立任务，每个任务都需要访问同一个 Router。用 `Arc` 共享内部数据，使得 `Router` 的 `Clone` 只是增加一次引用计数，廉价且线程安全。

**练习 2**：`Router::new()` 之后不做任何 `.route(...)`，对一个空 Router 发请求会发生什么？

> **参考答案**：会命中默认的 `catch_all_fallback`，即 `Fallback::Default(Route::new(NotFound))`，返回 `404 Not Found`。这正是 `Router::new` 里设置默认 fallback 的作用。

---

### 4.2 Handler：把 `async fn` 适配为 Service

#### 4.2.1 概念说明

axum 的官方一句话定义写在 [axum/src/docs/handlers_intro.md:L1-L4](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/docs/handlers_intro.md#L1-L4)：

> handler 是一个 **async 函数**，接受零个或多个**提取器**作为参数，返回一个能被转换成**响应**的东西。

这段话被 `include_str!` 直接嵌入到 `axum::handler` 模块的文档里（见 [axum/src/handler/mod.rs:L1-L3](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/handler/mod.rs#L1-L3)），可见它是 axum 最核心的心智模型。

关键点在于：handler 是**你写的普通 `async fn`**，但 axum 内部一切调度都基于 `tower::Service`（签名是 `fn call(&mut self, req: Request) -> Future<Result<Response, Error>>`）。两者形状不同，需要一个适配层——这就是 `Handler` trait 的作用。

#### 4.2.2 核心流程

`Handler` trait 的本质是「**把 `async fn(提取器...) -> 返回值` 包装成一个能被路由调用的 `Service`**」。流程如下：

```
你写的：   async fn greet(State(s): State<S>, Path(id): Path<u64>) -> Json<...>
                                  │
                  Handler trait 的 blanket impl（由宏为 1~16 个参数生成）
                                  ▼
适配成：    impl Handler for F  // F 就是上面的 async fn
            fn call(self, req: Request, state: S) -> Future<Response>
                                  │
                  内部依次：对每个参数调用 FromRequestParts::from_request_parts
                           最后一个参数（若消费 body）调用 FromRequest::from_request
                           任何一个失败 → 把 Rejection 转成响应并提前返回
                           全部成功 → 调用真正的 async fn，把返回值 into_response
```

这里有两个设计要点：

1. **参数提取是依次 `await` 的**。每调用一次 `from_request_parts` / `from_request` 都是一个异步操作（比如读请求体是异步 IO）。这也是 handler 之所以必须是 `async` 的根本原因之一。
2. **消费请求体的提取器只能放在最后一个参数**。因为「读 body」是破坏性的——body 只能被消费一次。这个约束在宏的 blanket impl 里用类型系统表达（见源码精读），后续 u3-l1 会专门讲。

#### 4.2.3 源码精读

`Handler` trait 的定义：

[axum/src/handler/mod.rs:L145-L205](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/handler/mod.rs#L145-L205) — 注意三处：① trait 签名是 `Handler<T, S>`，`call` 接收 `req: Request` 和 `state: S` 两个参数（state 由路由透传，正是 `with_state` 填进去的那个值）；② `type Future: Future<Output = Response> + Send + 'static`，**调用 handler 返回的一定是一个 Future**；③ `with_state` 把 handler 变成 `HandlerService`（一个真正的 `Service`）。

> 顺带解释 trait 上的类型参数 `T`：它不是用户要关心的东西，文档 [axum/src/handler/mod.rs:L129-L144](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/handler/mod.rs#L129-L144) 说得很清楚——它是为了**绕过 Rust 的 trait coherence（一致性）规则**。否则编译器无法区分「一个函数 `F` 到底是 0 个参数的 handler 还是 1 个参数的 handler」（因为理论上同一个 `F` 可以同时实现 `Fn()` 和 `Fn(A)`）。`T` 充当一个「参数个数的标记类型」，让不同元数的 blanket impl 不会冲突。第 8 单元会深入展开。

0 参数 handler 的 blanket impl（最简单的一版，先看它建立直觉）：

[axum/src/handler/mod.rs:L207-L219](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/handler/mod.rs#L207-L219) — 约束是 `F: FnOnce() -> Fut` 且 `Fut: Future<Output = Res>`、`Res: IntoResponse`。`call` 的实现就是把请求 `req` 和状态 `state` 都丢弃，直接 `self().await.into_response()`。**这一段揭示了「为什么 handler 必须是 async」**：约束要求 `F` 的返回值 `Fut` 必须是一个 `Future`。普通同步 `fn() -> String` 返回的是 `String`，而 `String` 不实现 `Future`，所以不满足约束；只有 `async fn`（其返回值是 `impl Future`）才满足。

1~16 个参数的 blanket impl 由 `impl_handler!` 宏生成：

[axum/src/handler/mod.rs:L221-L262](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/handler/mod.rs#L221-L262) — 这是本讲最重要的一段。逐行解读：

- 第 233-234 行的约束：前面的每个参数 `$ty` 必须实现 `FromRequestParts<S>`（只读 parts、可重复），**最后一个参数 `$last` 必须实现 `FromRequest<S, M>`（可能消费 body）**。这正是「消费 body 的提取器只能放最后」的类型层依据。
- 第 239-256 行的 `call` 实现：先把 `req.into_parts()` 拆成 `parts` 和 `body`；对每个 `$ty` 依次 `await` 提取，失败就 `return rejection.into_response()`；再把 `parts` 和 `body` 重新拼成 `Request`，对 `$last` 调用 `from_request`；最后 `self($ty..., $last).await.into_response()` 调用你写的函数。
- 第 262 行 `all_the_tuples!(impl_handler);` 把这个宏对 1~16 元组各展开一次，于是任何 0~16 个提取器参数的 `async fn` 都自动成为 `Handler`。

还有一个常被忽略的 blanket impl——「任何 `IntoResponse` 的值本身也是 `Handler`」：

[axum/src/handler/mod.rs:L270-L280](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/handler/mod.rs#L270-L280) — 这就是为什么你可以写 `get("Hello, World!")` 或 `get((StatusCode::CREATED, Json(...)))`：字符串、元组这些「现成的响应值」也实现了 `Handler`，被当作「不带提取器、直接返回固定值」的 handler。

#### 4.2.4 代码实践

1. **实践目标**：通过故意写错，亲眼看到「handler 必须 async」的编译错误，从而理解约束的来源。
2. **操作步骤**：在本地工程里写两个版本的同名 handler，分别编译。

   ```rust
   // 版本 A（错误）：同步函数
   fn sync_handler() -> &'static str {
       "hello"
   }
   // let app = Router::new().route("/", axum::routing::get(sync_handler)); // ❌ 编译错误
   ```

   ```rust
   // 版本 B（正确）：async 函数
   async fn async_handler() -> &'static str {
       "hello"
   }
   // let app = Router::new().route("/", axum::routing::get(async_handler)); // ✅
   ```

3. **需要观察的现象**：版本 A 会报一个形如 `F: FnOnce() -> Fut, Fut: Future` 不满足的错误（且 Rust 会提示考虑 `#[axum::debug_handler]`，见 u10-l1）。版本 B 编译通过。
4. **预期结果**：你能解释——`get(sync_handler)` 失败是因为 `sync_handler()` 返回 `&str` 而非 `Future`，不满足 `Handler` 的 0 参数 blanket impl 中 `Fut: Future<Output = Res>` 的约束。
5. 如本地未配置工具链，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`Handler::call` 的签名是 `fn call(self, req: Request, state: S)`，其中 `state` 从哪里来？

> **参考答案**：来自 `Router::with_state(state)` 填充的值。路由在分发请求时会把它持有的 `state` 透传给 `Handler::call`，再由宏生成的实现把 `state` 传给每个提取器的 `from_request_parts(&mut parts, &state)`。所以 `State<T>` 提取器最终读到的就是这个值。

**练习 2**：为什么 `get((StatusCode::CREATED, Json(data)))` 这种「直接返回固定值」的写法能编译通过？

> **参考答案**：因为存在 `impl<T, S> Handler<private::IntoResponseHandler, S> for T where T: IntoResponse + ...`（[mod.rs:L270-L280](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/handler/mod.rs#L270-L280)）。任何实现了 `IntoResponse` 的类型（字符串、`StatusCode`、元组、`Json` 等）都自动成为一个「无提取器参数」的 handler。

---

### 4.3 method_routing::get：从 handler 到 MethodRouter

#### 4.3.1 概念说明

初学者最容易混淆的一对概念是 **handler** 和 **method router**。看下面两行：

```rust
async fn hello() -> &'static str { "hello" }   // 这是 handler（一个 async fn）
Router::new().route("/", get(hello));           // get(hello) 是 MethodRouter，不是 handler
```

- `hello` 本身是 handler。
- `get(hello)` 把 `hello` 包装成一个 **`MethodRouter`**，意思是「在 `GET` 方法上挂这个 handler」。
- `Router::route(path, method_router)` 接收的第二个参数**必须是 `MethodRouter`**，不能直接传 handler 函数。

为什么要中间多一层 `MethodRouter`？因为同一条路径可能要响应**多个 HTTP 方法**：`GET /users` 列表、`POST /users` 创建、`DELETE /users/{id}` 删除……`MethodRouter` 就是「同一路径上，方法 → handler」的映射表。`get`/`post`/`delete` 这些函数是构造 `MethodRouter` 的便捷入口，`.get().post()` 链式调用则是往这张表里继续添加方法。

#### 4.3.2 核心流程

```
get(handler)
   │  实际调用：on(MethodFilter::GET, handler)
   ▼
MethodRouter {
    get:    MethodEndpoint::BoxedHandler(handler),   // 仅 get 字段被填，其余为 None
    post:   None,
    delete: None,
    ...
    fallback: 默认,
    allow_header: 自动累积（用于生成 405 的 Allow 头）
}
   │
   │  .post(other) 链式调用 → 再次 on(MethodFilter::POST, other)
   ▼
MethodRouter { get: Some(...), post: Some(...), ... }
   │
   │  Router::route("/users", method_router)
   ▼
注册进 path_router 的匹配树
```

运行时，当一条 `POST /users` 请求匹配到这个 `MethodRouter` 时，`MethodRouter`（本身也是 `Service`）会按方法找到对应的 endpoint 去执行；如果方法未注册（比如对上面的注册发 `PUT`），就返回 `405 Method Not Allowed` 并附上 `Allow` 头告诉客户端允许哪些方法（这部分细节在 u2-l3 展开）。

#### 4.3.3 源码精读

`get` 是一个由 `top_level_handler_fn!` 宏生成的函数：

[axum/src/routing/method_routing.rs:L160-L173](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L160-L173) — 宏为 `get` 生成的签名是 `pub fn get<H, T, S>(handler: H) -> MethodRouter<S, Infallible> where H: Handler<T, S>`，函数体只有一行 `on(MethodFilter::GET, handler)`。注意 `H: Handler<T, S>` 这个约束——这正是 4.2 节讲的 trait，它保证传入的确实是合法 handler。该宏在 [method_routing.rs:L441](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L441) 处被实例化为 `top_level_handler_fn!(get, GET);`，`post`/`put`/`delete` 等同理。

顶层的 `on` 函数：

[axum/src/routing/method_routing.rs:L466-L473](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L466-L473) — `on(filter, handler)` 创建一个空 `MethodRouter::new()`，再调它的 `on` 方法。

`MethodRouter` 的结构定义是理解「方法层」的关键：

[axum/src/routing/method_routing.rs:L547-L559](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L547-L559) — 它为每个 HTTP 方法（`get`/`head`/`delete`/`options`/`patch`/`post`/`put`/`trace`/`connect`）各保留一个 `MethodEndpoint` 槽位，外加一个 `fallback` 和一个 `allow_header`。`get(handler)` 只填 `get` 这一个槽，其余为 `None`；`.post(h2)` 再填 `post` 槽。

`MethodRouter::on` 方法（链式调用的真正落点）：

[axum/src/routing/method_routing.rs:L629-L640](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L629-L640) — 它把 handler 包成 `MethodEndpoint::BoxedHandler(BoxedIntoRoute::from_handler(handler))`，再交给 `on_endpoint` 去落到对应方法的槽位上。注意 `BoxedIntoRoute` 是一个**类型擦除**的容器——它把「带具体提取器类型的 handler」统一存成一个统一的形状，等到 `with_state` 时再惰性地转成最终的 `Route`（这个优化在 u9-l3 讲）。

槽位里的值是 `MethodEndpoint` 枚举：

[axum/src/routing/method_routing.rs:L1272-L1276](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L1272-L1276) — 三种状态：`None`（该方法未注册）、`Route(...)`（已经是可执行的 Service）、`BoxedHandler(...)`（handler，待 `with_state` 后转成 `Route`）。

#### 4.3.4 代码实践

1. **实践目标**：用一条路径注册多个方法，区分 handler 与 method router。
2. **操作步骤**：

   ```rust
   use axum::{routing::{get, post}, Json, Router};
   use serde_json::json;

   async fn list() -> Json<serde_json::Value> { Json(json!([{"id": 1}])) }
   async fn create() -> Json<serde_json::Value> { Json(json!({"created": true})) }

   let app: Router = Router::new().route(
       "/items",
       get(list).post(create),  // 同一路径，GET→list，POST→create
   );
   ```

3. **需要观察的现象**：对 `/items` 发 `GET` 走 `list`，发 `POST` 走 `create`，发 `PUT` 得到 `405` 且响应头里有 `Allow: GET,POST`（`Allow` 头的生成细节见 u2-l3）。
4. **预期结果**：你能指出——`list` 和 `create` 是 handler（async fn），`get(list).post(create)` 这一整个表达式是**一个** `MethodRouter`，它被 `route` 注册到了 `/items` 这一条路径上。
5. 如本地未配置工具链，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`Router::route` 的第二个参数类型是什么？为什么不能直接传一个 `async fn`？

> **参考答案**：第二个参数类型是 `MethodRouter<S>`。不能直接传 `async fn`，是因为一条路径需要知道「这个 handler 对应哪个 HTTP 方法」。`get(h)`/`post(h)` 这层包装就是用来声明方法的；缺少这层，`route` 无从知道该把 handler 挂到哪个方法槽位上。

**练习 2**：`MethodRouter` 内部为 9 个 HTTP 方法各保留了一个槽位。如果对一条只注册了 `get` 的路径发 `PUT` 请求，会进入哪个槽位？

> **参考答案**：没有任何槽位匹配（`put` 槽是 `None`），此时 `MethodRouter` 的 `Service` 实现会返回 `405 Method Not Allowed`，并根据已注册的方法生成 `Allow` 头（由 `allow_header` 字段累积而来）。它**不会**走到外层 `Router` 的 `catch_all_fallback`，因为路径是匹配上的，只是方法不匹配。

---

## 5. 综合实践

把本讲三个最小模块串起来：写一个带 `State`、`Path` 参数、返回 `Json` 的完整 handler，对照 `handlers_intro.md` 把每个参数归类，并解释 handler 为什么必须 async。

### 5.1 目标

实现一个 `GET /greet/{id}` 端点：从应用状态里取问候语，从路径取用户 id，返回一段 JSON。

### 5.2 完整代码（示例代码）

新建一个二进制 crate，`Cargo.toml` 关键依赖：

```toml
[dependencies]
axum = "0.8"
tokio = { version = "1", features = ["full"] }
serde = { version = "1", features = ["derive"] }
serde_json = "1"
```

`src/main.rs`：

```rust
// 示例代码
use axum::{extract::{Path, State}, routing::get, Json, Router};
use serde::Serialize;

// 应用状态：会被 with_state 填充进 Router<S>，再透传给 Handler::call
#[derive(Clone)]
struct AppState {
    greeting: String,
}

// 返回值类型：必须实现 IntoResponse。Json<T> 实现了 IntoResponse。
#[derive(Serialize)]
struct GreetingResponse {
    message: String,
    user_id: u64,
}

// handler：async fn，参数都是提取器，返回值实现 IntoResponse
async fn greet(
    State(state): State<AppState>, // 提取器①：FromRequestParts，从透传的 state 取值
    Path(id): Path<u64>,           // 提取器②：FromRequestParts，从路径参数 {id} 取值
) -> Json<GreetingResponse> {      // 返回值：IntoResponse（不是提取器）
    Json(GreetingResponse {
        message: state.greeting.clone(),
        user_id: id,
    })
}

#[tokio::main]
async fn main() {
    // Router<AppState>：还缺少 AppState
    let app: Router<AppState> = Router::new().route("/greet/{id}", get(greet));

    // with_state 填充状态 → Router<()>：现在可以交给 serve 了
    let app: Router<()> = app.with_state(AppState {
        greeting: "Hello".into(),
    });

    let listener = tokio::net::TcpListener::bind("127.0.0.1:3000").await.unwrap();
    axum::serve(listener, app).await.unwrap();
}
```

### 5.3 操作步骤

1. 运行：`cargo run`。
2. 测试：`curl http://127.0.0.1:3000/greet/42`。

### 5.4 参数归类（对照 handlers_intro.md）

对照 [axum/src/docs/handlers_intro.md:L1-L4](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/docs/handlers_intro.md#L1-L4) 的定义（「handler 是 async 函数，接受零或多个提取器，返回可转成响应的东西」）：

| 位置 | 代码片段 | 归类 | 说明 |
| --- | --- | --- | --- |
| 参数 ① | `State(state): State<AppState>` | **提取器**（`FromRequestParts`） | 不消费 body，可放在任意非末尾位置。从 `Handler::call` 透传的 `state: S` 取值。 |
| 参数 ② | `Path(id): Path<u64>` | **提取器**（`FromRequestParts`） | 不消费 body，读 URL 里 `{id}` 捕获的值（u3-l2 详讲）。 |
| 返回值 | `Json<GreetingResponse>` | **返回值**（`IntoResponse`） | 不是提取器。它会被 `into_response` 转成带 `content-type: application/json` 的响应。 |

> 关键区分：**提取器是参数，`IntoResponse` 是返回值**。注意 `Json<T>` 同时实现了 `FromRequest`（可作提取器，读请求体）和 `IntoResponse`（可作返回值，写响应体），它扮演哪个角色完全取决于它出现在参数列表还是返回位置。如果本例还要接收请求体，应再加一个 `Json<RequestBody>` 作为**最后一个**参数（因为消费 body 的提取器必须放最后，见 4.2.3 的 `impl_handler!` 约束）。

### 5.5 为什么 handler 必须 async

结合本讲源码，给出三层理由：

1. **类型约束层面**：`Handler` 的 blanket impl（[mod.rs:L207-L219](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/handler/mod.rs#L207-L219) 与 [mod.rs:L221-L262](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/handler/mod.rs#L221-L262)）要求 `F: FnOnce(...) -> Fut` 且 `Fut: Future<Output = Res>`。只有 `async fn` 的返回值是 `Future`，同步函数的返回值不是，故不满足。
2. **提取器层面**：参数提取本身是异步的——`from_request_parts` / `from_request` 都返回 `Future`（读请求体是异步 IO）。宏生成的 `call` 内部要 `.await` 它们（见 [mod.rs:L242-L253](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/handler/mod.rs#L242-L253)）。这一步发生在 axum 内部，但要求整个调用链是 async 的。
3. **业务层面**：handler 体内通常要访问数据库、调用下游服务、读写文件——这些在 tokio 生态里都是 async 的。async 让 handler 能在不阻塞线程的前提下等待这些操作。

### 5.6 预期结果

`curl` 返回类似：

```json
{"message":"Hello","user_id":42}
```

如本地无法运行，标注「待本地验证」。

## 6. 本讲小结

- `Router<S>` 是路由容器，内部用 `Arc<RouterInner<S>>` 共享以廉价 `Clone`；泛型 `S` 表示「**还缺少**」的状态类型，**只有 `Router<()>` 能交给 `serve`**，`with_state` 负责把它「填满」。
- `Handler<T, S>` trait 是「`async fn` → `tower::Service`」的适配层。其 blanket impl 由 `impl_handler!` 宏为 0~16 个参数生成，内部依次 `await` 每个提取器，并把返回值 `into_response`。
- handler **必须是 async**，根因是 trait 约束要求其返回值是 `Future`，且提取器提取与业务 IO 都是异步的。
- **handler ≠ method router**：`async fn` 是 handler；`get(handler)` 产生的 `MethodRouter` 才是 `Router::route` 第二个参数所期望的类型。`MethodRouter` 内部为 9 个 HTTP 方法各保留一个槽位。
- 消费请求体的提取器（如 `Json` 作参数）**只能放最后一个参数**，这是 `impl_handler!` 宏在类型层面的硬约束。
- `Json<T>` 既是提取器（`FromRequest`，读 body）又是返回值（`IntoResponse`，写 body），角色由它出现的位置决定。

## 7. 下一步学习建议

本讲建立了 Router/Handler/MethodRouter 三者的静态心智模型，但路径**怎么匹配**、提取器**具体怎么取值**都还是黑盒。建议按以下顺序继续：

1. **下一讲 u2-l1（Router 与路由注册）**：深入 `route` / `route_service` 与 `RouterInner` 的关系，把本讲的「外壳」讲透。
2. **u2-l2（路径匹配与路径参数）**：拆开 `path_router` 的 matchit 匹配树，理解本讲 `Path(id)` 是怎么从 `{id}` 捕获里取到值的。
3. **u3-l1（FromRequestParts 与 FromRequest 双 trait 机制）**：系统讲解提取器两个 trait，并解释为什么本讲反复强调「消费 body 的提取器必须放最后」。
4. 想提前感受「handler 类型错误有多难读」的读者，可以跳读 u10-l1 的 `#[debug_handler]` 宏，它能让本讲 4.2.4 里的编译错误信息变得友好得多。

> 继续阅读建议源码：先重读 [axum/src/handler/mod.rs:L221-L262](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/handler/mod.rs#L221-L262) 的 `impl_handler!` 宏，再带着「参数怎么被依次提取」的问题进入第 3 单元。
