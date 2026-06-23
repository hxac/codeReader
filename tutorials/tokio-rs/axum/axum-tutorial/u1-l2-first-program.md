# 运行第一个 axum 程序

## 1. 本讲目标

上一篇（u1-l1）我们已经建立了对 axum 的整体认知：它是 hyper 之上的一层路由与请求处理库，最大的设计取舍是「不自造中间件，直接复用 `tower::Service`」。本讲把这套理念落到一个**能真正跑起来的最小程序**上。

读完本讲，你应当能够：

- 用 `Router::new().route(...)` 组装一个最小的 axum 服务，并说清每一行在做什么。
- 理解 `axum::serve(listener, app)` 是如何把一个 `Router` 接入 hyper 并开始监听 TCP 连接的。
- 掌握「handler 就是一个返回 `IntoResponse` 的 `async fn`」这一核心心智模型，并用 `Html` 之类内置响应类型返回内容。
- 在本地把官方 `hello-world` 示例跑起来，并自己加一条 `/health` 路由。

本讲只覆盖三个最小模块：**Router（路由注册）**、**axum::serve（启动服务）**、**handler 与 Html 响应**。关于路径匹配、提取器、状态、中间件等更深的内容，留给后续讲义。

## 2. 前置知识

在进入源码前，先用最通俗的方式建立几个概念。如果你已经熟悉 Web 框架，可以快速浏览本节。

- **HTTP 请求的生命周期**：客户端（如 curl、浏览器）向服务器的某个 IP:端口发起 TCP 连接，再在这个连接上发送一行形如 `GET / HTTP/1.1` 的请求；服务器读出「方法 + 路径」，决定由哪段代码处理，处理完把响应写回连接。axum 关心的就是「方法 + 路径 → 哪段代码」这一步，以及「这段代码的返回值 → HTTP 响应」这一步。
- **handler（处理函数）**：就是你自己写的一段处理请求的代码。在 axum 里它是一个 `async fn`，返回值必须能被转成 HTTP 响应（即实现 `IntoResponse`）。它「不用关心」连接是怎么来的，那是 `serve` 的职责。
- **路由（route）**：把「某个路径」绑定到「某个 handler」的对应关系。比如把 `/` 绑定到首页 handler。`Router` 就是存放这堆对应关系的容器。
- **监听器（listener）**：负责「监听端口、接受新 TCP 连接」的角色。axum 不自己实现网络层，而是直接用 tokio 的 `TcpListener`（上一篇提到的「复用生态」再次体现）。
- **`async`/`await` 与运行时**：axum 基于 tokio 异步运行时。`#[tokio::main]` 把 `main` 变成异步入口并在内部启动运行时，否则 `TcpListener::bind(...).await` 这类 `.await` 无法工作。

> 承接 u1-l1：我们已经知道 axum 的能力受 Cargo **feature flag** 控制。本讲的 `axum::serve` 只有在同时启用 `tokio` 和（`http1` 或 `http2`）feature 时才存在——这一点稍后在源码里会直接看到对应的 `#[cfg(...)]`。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `examples/hello-world/src/main.rs` | 官方最小示例，整篇讲义的「主样本」，只有 24 行。 |
| `examples/hello-world/Cargo.toml` | 示例的依赖声明，说明运行 axum 至少需要哪些 crate。 |
| `axum/src/lib.rs` | axum crate 的顶层文档与公开导出，能看到 `Router`、`serve` 是怎么被 re-export 的，以及 feature gate。 |
| `axum/src/routing/mod.rs` | `Router::new` 与 `Router::route` 的定义，路由注册的入口。 |
| `axum/src/routing/method_routing.rs` | `get` 等方法路由函数的定义（由宏生成），把 handler 包装成 `MethodRouter`。 |
| `axum/src/serve/mod.rs` | `axum::serve` 函数及其内部 accept 循环、`handle_connection` 的实现。 |
| `axum/src/response/mod.rs` | `Html` 类型及其 `IntoResponse` 实现。 |

## 4. 核心概念与源码讲解

### 4.1 Router：用 route 组装路由

#### 4.1.1 概念说明

`Router` 是 axum 的核心容器，存放「路径 → 处理单元」的映射。它的设计目标是让你用**链式调用、无宏**的 API 把应用一点点拼出来：

```rust
let app = Router::new()
    .route("/", get(handler))          // 把 GET / 交给 handler
    .route("/health", get(health));    // 再加一条 GET /health
```

这里有两个容易混淆的概念，先分清：

- `Router`：整棵路由树的根，最终交给 `serve` 的就是这个东西。
- `MethodRouter`（方法路由）：绑定在**某一个具体路径**上、描述「不同 HTTP 方法各由谁处理」的对象。`get(handler)` 不会直接产生路由，它产生的是一个「针对 GET 方法的 `MethodRouter`」；真正把它挂到路径上的是 `Router::route("/", ...)`。

所以 `Router::new().route("/", get(handler))` 读作：「新建一棵路由树，在 `/` 这个路径上，把 GET 方法交给 `handler`」。

#### 4.1.2 核心流程

`Router` 内部用一个叫 `RouterInner` 的结构保存数据（本讲只看注册阶段，匹配阶段留给 u2）：

1. `Router::new()` 创建一个空的 `Router`，内部 `path_router` 为空，并预置一个默认的「404 兜底」fallback。
2. 调用 `.route(path, method_router)` 时，`Router` 把 `method_router` 注册到 `path_router` 里对应路径上。
3. 因为 `route` 返回 `Self`，所以可以链式 `.route(...).route(...)`。
4. 所有路由注册完成后，把这个 `Router` 交给 `serve`。

注册阶段的简化伪代码：

```text
Router::new()
  └─ RouterInner { path_router: 空, catch_all_fallback: 默认404 }
.route("/", get(handler))
  └─ path_router.route("/", MethodRouter{ get: handler })
.route("/health", get(health))
  └─ path_router.route("/health", MethodRouter{ get: health })
返回的 Router 即可交给 serve
```

> 注意：`Router<S>` 带一个泛型参数 `S`（状态类型）。本讲不使用状态，所以它是默认的 `Router<()>`（等价于「不缺状态」）。状态相关内容在 u3-l5。

#### 4.1.3 源码精读

先看示例里这行核心代码：

- [examples/hello-world/src/main.rs:7-L12](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/examples/hello-world/src/main.rs#L7-L12) — 导入 `Router`、`get`、`Html`，并用 `Router::new().route("/", get(handler))` 拼出应用。

`Router::new()` 的真实定义：

- [axum/src/routing/mod.rs:158-L170](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L158-L170) — 创建一个空的 `Router`，内部用 `Arc` 包裹 `RouterInner`，并预置 `catch_all_fallback: Fallback::Default(Route::new(NotFound))`，这正是「没匹配到任何路由时返回 404」的来源。

`Router::route` 的定义：

- [axum/src/routing/mod.rs:190-L196](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L190-L196) — 接收路径字符串和一个 `MethodRouter<S>`，调用 `path_router.route(...)` 真正注册。`#[track_caller]` 让出错时能定位到调用点。

`get` 函数其实是由宏生成的。宏定义和实例化位置如下：

- [axum/src/routing/method_routing.rs:441-L441](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L441-L441) — `top_level_handler_fn!(get, GET);` 这一行「实例化」出名为 `get` 的函数。
- [axum/src/routing/method_routing.rs:160-L167](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L160-L167) — 宏展开后生成的函数签名：`pub fn get<H, T, S>(handler: H) -> MethodRouter<S, Infallible> where H: Handler<T, S>`。也就是说 `get(handler)` 接收任何实现了 `Handler` 的东西，返回一个「错误类型为 `Infallible`（不可能失败）」的 `MethodRouter`。

宏里还有一条对初学者很关键的注释：

- [axum/src/routing/method_routing.rs:127-L129](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L127-L129) — 说明用 `get` 注册的路由**也会响应 HEAD 请求**（只是会把响应体去掉）。这解释了为什么用 curl 发 HEAD 也能命中。

#### 4.1.4 代码实践

**实践目标**：亲手组装一个带两条路由的 `Router`，确认注册 API 的链式用法。

**操作步骤**：

1. 把官方示例复制到本地一个新目录（具体命令见「综合实践」），先不急着跑。
2. 找到 `let app = Router::new().route("/", get(handler));` 这一行。
3. 在它后面再加一条：`.route("/health", get(|| async { "ok" }))`。
4. 阅读上面给出的 `Router::route` 源码链接，确认你理解 `route` 的两个参数分别是什么。

**需要观察的现象**：代码能通过编译，说明 `route` 的返回类型确实是 `Self`（链式成立）。

**预期结果**：编译通过；`app` 现在持有两条路由。路由是否真的生效，留到综合实践用 curl 验证。

**待本地验证**：本步骤不运行，仅修改源码并编译。

#### 4.1.5 小练习与答案

**练习 1**：`Router::route` 的第二个参数类型是什么？为什么不是直接传 handler？

<details>
<summary>参考答案</summary>

第二个参数是 `MethodRouter<S>`。因为同一个路径可能要区分 GET/POST 等不同方法，axum 把「方法 → handler」的映射封装成 `MethodRouter`，由 `get`/`post` 等函数生成。直接传 handler 会丢失「这是哪个方法」的信息。
</details>

**练习 2**：`Router::new()` 创建出来的空 Router，如果直接交给 `serve` 跑起来，访问任意路径会发生什么？

<details>
<summary>参考答案</summary>

会返回 404。因为 `Router::new()` 里预置了 `catch_all_fallback: Fallback::Default(Route::new(NotFound))`，没有任何匹配路由时就走这个兜底，返回 `404 Not Found`。
</details>

### 4.2 axum::serve：把 Router 跑起来

#### 4.2.1 概念说明

`Router` 只是一个「描述了路由规则」的数据结构，它自己不会监听端口。真正把网络跑起来的是 `axum::serve(listener, app)`。它的职责可以概括为三件事：

1. 持有一个**监听器**（listener），不断接受新的 TCP 连接。
2. 对每条连接，把它适配成 hyper 能理解的 HTTP 连接。
3. 对这条连接上收到的每个请求，调用你的 `app`（一个 `tower::Service`）去处理，把响应写回去。

这里再次体现 axum 的设计哲学：网络 IO 和 HTTP 解析全部交给 tokio + hyper，axum 只在「请求/响应」这一层做适配。

#### 4.2.2 核心流程

`serve` 启动后进入一个无限 accept 循环，核心调用链（无优雅关闭的简单情形）如下：

```text
serve(listener, app)            // 构造 Serve 结构体，记录 listener + app
  └─ Serve::run()               // await 它时进入（通过 IntoFuture）
       └─ loop {
            listener.accept()                // 等待新 TCP 连接
            handle_connection(...)           // 处理这一条连接
              ├─ make_service.call(...)      // 由 app 产出处理该连接的 Service
              ├─ TowerToHyperService::new    // 把 tower Service 适配成 hyper Service
              └─ executor.execute(async {    // 为这条连接 spawn 一个任务
                   builder.serve_connection_with_upgrades(io, hyper_service)
                   // hyper 解析 HTTP，每来一个请求就调用 app
                 })
          }
```

要点：

- `serve` 返回的是一个 `Serve` **future**，必须 `.await` 才会真正运行（示例里 `axum::serve(listener, app).await;`）。
- accept 循环是 `loop { ... }`，正常情况下永不返回（文档明确说它「永远不会真正完成或返回错误」，TCP 错误时只是短暂休眠后重试）。
- 每条连接都被 `executor.execute(...)`（默认 `TokioExecutor`，即 `tokio::spawn`）独立 spawn 成一个任务，因此多连接之间是并发处理的。

#### 4.2.3 源码精读

先看示例里调用 `serve` 的两行：

- [examples/hello-world/src/main.rs:15-L19](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/examples/hello-world/src/main.rs#L15-L19) — 用 tokio 的 `TcpListener::bind` 绑定 `127.0.0.1:3000`，然后 `axum::serve(listener, app).await`。

`serve` 函数本身的定义和它的 feature gate：

- [axum/src/serve/mod.rs:102-L119](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/serve/mod.rs#L102-L119) — 注意最上面的 `#[cfg(all(feature = "tokio", any(feature = "http1", feature = "http2")))]`：只有同时启用 `tokio` 与（`http1` 或 `http2`）时，`serve` 才被编译出来。这正是上一篇提到的 feature 影响能力。函数签名 `pub fn serve<L, M, S, B>(listener: L, make_service: M)` 要求 `L: Listener`、`M` 是能对每条连接产出 `Service<Request>` 的「make service」——你的 `Router` 满足这个约束，所以可以直接传。

accept 循环（await 时执行）：

- [axum/src/serve/mod.rs:321-L344](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/serve/mod.rs#L321-L344) — `async fn run(self) -> !`，`loop { listener.accept().await; handle_connection(...).await; }`。返回类型 `-> !` 印证了「永不返回」。

单连接处理：

- [axum/src/serve/mod.rs:559-L633](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/serve/mod.rs#L559-L633) — `handle_connection`：把底层 IO 包成 `TokioIo`，调用 `make_service.call(...)` 得到处理本连接的 tower service，再用 `TowerToHyperService::new(...)` 适配成 hyper service，最后 `executor.execute(async move { builder.serve_connection_with_upgrades(io, hyper_service) ... })` 把整条连接交给 hyper。
- [axum/src/serve/mod.rs:587-L596](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/serve/mod.rs#L587-L596) — 这几行是「tower world ↔ hyper world」的桥接点：`make_service.call(IncomingStream {...})` 产出 service，`.map_request(|req| req.map(Body::new))` 把 hyper 的 `Incoming` body 转成 axum 的 `Body`，`TowerToHyperService::new(tower_service)` 完成反向适配。

`serve` 在 lib.rs 的导出同样是 feature gate 的：

- [axum/src/lib.rs:500-L501](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/lib.rs#L500-L501) — `#[cfg(all(feature = "tokio", any(feature = "http1", feature = "http2")))] pub mod serve;`
- [axum/src/lib.rs:529-L531](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/lib.rs#L529-L531) — `pub use self::serve::serve;`（同样带 cfg）。

#### 4.2.4 代码实践

**实践目标**：通过阅读源码，把「一条 TCP 连接 → 你的 handler」的调用链画清楚，而不是停留在「反正能跑」。

**操作步骤**：

1. 打开 [axum/src/serve/mod.rs:321-L344](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/serve/mod.rs#L321-L344)，确认 accept 循环结构。
2. 打开 [axum/src/serve/mod.rs:559-L633](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/serve/mod.rs#L559-L633)，找到三处关键代码：
   - `make_service.call(...)`：由 `app` 产生处理本连接的 service。
   - `TowerToHyperService::new(tower_service)`：tower → hyper 适配。
   - `executor.execute(async move { builder.serve_connection_with_upgrades(...) })`：把连接交给 hyper。
3. 在纸上画出从 `axum::serve(listener, app).await` 到「hyper 调用你的 `app` 处理某个请求」的完整调用链。

**需要观察的现象**：你能指出 tower 的 `Service`/`make_service` 与 hyper 的 `serve_connection` 在哪一行衔接。

**预期结果**：得到一张调用链图，说明 axum 在其中只做了「适配 + spawn 任务」，真正的 HTTP 解析和 socket 读写由 hyper/tokio 完成。

**待本地验证**：本步骤为源码阅读型实践，无需运行命令。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `axum::serve(...)` 后面必须加 `.await`？不加会怎样？

<details>
<summary>参考答案</summary>

`serve` 返回的是 `Serve` 这个 future（实现了 `IntoFuture`）。不 `.await` 它就不会被驱动，accept 循环不会运行，服务也就不会真正监听。而且编译器会因 `#[must_use]` 给出警告。
</details>

**练习 2**：如果编译时只关掉了 `http1` 和 `http2` 两个 feature，示例里的 `axum::serve` 还能编译通过吗？

<details>
<summary>参考答案</summary>

不能。`serve` 函数和 `pub mod serve` 都带 `#[cfg(all(feature = "tokio", any(feature = "http1", feature = "http2")))]`，两个 HTTP feature 都关掉时 `serve` 根本不存在，编译会报「找不到 `axum::serve`」。
</details>

### 4.3 handler 与 Html 响应

#### 4.3.1 概念说明

handler 是你自己写的业务代码。在 axum 里它有两个硬性要求：

1. 必须是 `async fn`（或闭包）。
2. 返回类型必须实现 `IntoResponse`——即「能被转成一个 HTTP 响应」。

满足这两条的函数，axum 会通过一个叫 `Handler` 的 blanket 实现（ blanket impl，即「为所有满足条件的类型自动实现的实现」，细节留到 u8-l1）自动把它适配成可被路由使用的处理单元。

`IntoResponse` 已经为大量常见类型实现了：`&'static str`、`String`、`StatusCode`、`()`（空响应）、元组、`Result<T, E>`、以及本讲的主角 `Html<T>`。`Html` 是一个「.newtype 包装」：它把任意已经是响应体的内容包一层，并自动加上 `Content-Type: text/html`。

#### 4.3.2 核心流程

一个请求被路由到 handler 后，流程是：

```text
hyper 解析出 Request
  └─ axum 路由匹配 → 找到 handler
       └─ 调用 handler()，得到返回值 R: IntoResponse
            └─ R::into_response() → Response（含状态码、headers、body）
                 └─ hyper 把 Response 写回连接
```

`Html` 这一层做的事情很轻：它只是把 `Content-Type: text/html; charset=utf-8` 这个响应头和内部内容组合成一个元组响应，再交给元组的 `IntoResponse` 实现去拼装最终响应。状态码默认是 `200 OK`（因为内部 `&'static str` 等类型的默认状态码是 200）。

#### 4.3.3 源码精读

示例里的 handler：

- [examples/hello-world/src/main.rs:22-L24](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/examples/hello-world/src/main.rs#L22-L24) — `async fn handler() -> Html<&'static str>`，返回 `Html("<h1>Hello, World!</h1>")`。它没有参数（不提取任何东西），返回值是 `Html`。

`Html` 的定义与 `IntoResponse` 实现：

- [axum/src/response/mod.rs:32-L37](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/response/mod.rs#L32-L37) — `pub struct Html<T>(pub T);`，一个元组结构体（newtype），字段 `pub` 所以可以用 `Html(...)` 直接构造。
- [axum/src/response/mod.rs:39-L53](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/response/mod.rs#L39-L53) — `impl<T: IntoResponse> IntoResponse for Html<T>`：它构造一个 `[(CONTENT_TYPE, "text/html; charset=utf-8")]` 的响应头数组，和内部内容 `self.0` 组成元组 `(headers, self.0).into_response()`。这行代码正好展示了 axum 「响应是可组合的」思想——`Html` 复用了元组的 `IntoResponse` 实现。

`Router` 与 `serve` 在 lib.rs 的导出：

- [axum/src/lib.rs:517-L517](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/lib.rs#L517-L517) — `pub use self::routing::Router;`，所以示例里能直接 `use axum::Router`。

lib.rs 顶部还给出了和示例几乎一致的官方「Hello, World」片段，可作为权威参考：

- [axum/src/lib.rs:23-L42](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/lib.rs#L23-L42) — crate 文档里的最小示例，用闭包 `get(|| async { "Hello, World!" })`，注意这里返回的是 `&'static str`（默认 `text/plain`），和 `Html`（`text/html`）形成对比。

#### 4.3.4 代码实践

**实践目标**：体会「handler 返回不同 `IntoResponse` 类型 → 不同响应头」，理解 `Html` 只是众多响应类型之一。

**操作步骤**：

1. 在综合实践的可运行项目里，写两个 handler：

   ```rust
   // 示例代码：展示不同 IntoResponse 类型
   async fn plain() -> &'static str {
       "plain text"   // 默认 Content-Type: text/plain; charset=utf-8
   }

   async fn html() -> Html<&'static str> {
       Html("<h1>html</h1>")  // Content-Type: text/html; charset=utf-8
   }
   ```

2. 分别用 `.route("/plain", get(plain))` 和 `.route("/html", get(html))` 注册。

**需要观察的现象**：用 `curl -i` 请求两个端点时，`Content-Type` 响应头不同。

**预期结果**：`/plain` 返回 `content-type: text/plain; charset=utf-8`；`/html` 返回 `content-type: text/html; charset=utf-8`。两者状态码都是 `200`。

**待本地验证**：具体响应头值以本地 `curl -i` 实际输出为准。

#### 4.3.5 小练习与答案

**练习 1**：把示例 handler 的返回类型从 `Html<&'static str>` 改成 `&'static str`（即直接返回字符串），响应会有什么变化？

<details>
<summary>参考答案</summary>

页面内容本身几乎不变（都是那段文字），但 `Content-Type` 会从 `text/html; charset=utf-8` 变成 `text/plain; charset=utf-8`，浏览器不会再把 `<h1>` 当作 HTML 标签渲染。
</details>

**练习 2**：handler 为什么必须是 `async fn`？普通 `fn` 行不行？

<details>
<summary>参考答案</summary>

`Handler` trait 的 `call` 返回的是一个 `Future`，要求处理函数能产出 future。axum 的 blanket impl 只对 `async fn`/返回 future 的闭包生效，普通同步 `fn` 不满足该约束，无法被 `get(...)` 接受（编译期报错）。
</details>

## 5. 综合实践

本任务把三个模块串起来：复制官方 `hello-world` 到本地、跑起来、再加一条 `/health` 路由并用 curl 验证。

**实践目标**：完整跑通一个真实的 axum 服务，并验证路由确实生效。

**操作步骤**：

1. 新建一个 cargo 项目（推荐方式，避免直接在 axum 仓库内编译示例）：

   ```bash
   cargo new my-axum-hello
   cd my-axum-hello
   ```

2. 在 `Cargo.toml` 的 `[dependencies]` 里加上 axum 与 tokio（依赖范围参考 [examples/hello-world/Cargo.toml:7-L9](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/examples/hello-world/Cargo.toml#L7-L9)）：

   ```toml
   [dependencies]
   axum = "<一个具体版本号>"
   tokio = { version = "1", features = ["full"] }
   ```

   > 版本号请用 `cargo add axum` 自动解析，或参考 axum 当时发布的最新版本，不要照抄占位符。

3. 把 `src/main.rs` 替换为官方示例内容（[examples/hello-world/src/main.rs:7-L24](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/examples/hello-world/src/main.rs#L7-L24)），并在 `Router::new()` 链上新增 `/health` 路由：

   ```rust
   use axum::{response::Html, routing::get, Router};

   #[tokio::main]
   async fn main() {
       let app = Router::new()
           .route("/", get(handler))
           .route("/health", get(|| async { "ok" })); // 新增：返回 200 + "ok"

       let listener = tokio::net::TcpListener::bind("127.0.0.1:3000")
           .await
           .unwrap();
       println!("listening on {}", listener.local_addr().unwrap());
       axum::serve(listener, app).await;
   }

   async fn handler() -> Html<&'static str> {
       Html("<h1>Hello, World!</h1>")
   }
   ```

4. 运行：`cargo run`。等编译完成、看到 `listening on 127.0.0.1:3000`。

5. 另开一个终端，用 curl 验证两条路由：

   ```bash
   curl -i http://127.0.0.1:3000/         # 期望 200，Content-Type: text/html
   curl -i http://127.0.0.1:3000/health   # 期望 200，body 为 ok
   ```

**需要观察的现象**：

- `GET /` 返回 `200`，响应头里有 `content-type: text/html; charset=utf-8`，body 是 `<h1>Hello, World!</h1>`。
- `GET /health` 返回 `200`，body 是 `ok`。
- 访问一个未注册的路径，如 `curl -i http://127.0.0.1:3000/nope`，应返回 `404`（来自 `Router::new()` 的默认 fallback）。
- 用 `curl -I`（HEAD）请求 `/`，由于 `get` 也会响应 HEAD（见 [method_routing.rs:127-L129](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L127-L129)），应得到 `200` 但没有 body。

**预期结果**：以上四点全部符合预期，即说明「Router 注册路由 + axum::serve 启动 + handler 返回响应」的最小闭环已经打通。

**待本地验证**：由于本环境无法实际启动服务，上述 curl 的状态码与响应头值请以本地真实输出为准；若 `cargo run` 报缺 feature，请确认 `tokio` 启用了 `full`（或至少 `macros`、`rt-multi-thread`、`net`）。

## 6. 本讲小结

- `Router` 是存放「路径 → 方法路由」映射的容器，用 `Router::new().route(path, get(handler))` 链式组装；空 Router 默认对任意请求返回 404。
- `get(handler)` 之类函数由宏生成，返回一个 `MethodRouter`，它把「某个 handler 绑定到某个 HTTP 方法」；真正挂到路径上的是 `Router::route`。
- `axum::serve(listener, app)` 把 `Router` 接入 hyper：内部是一个永不返回的 accept 循环，每条 TCP 连接被 spawn 成独立任务，由 hyper 解析 HTTP 并调用你的 `app`。
- `serve` 只在同时启用 `tokio` 与（`http1` 或 `http2`）feature 时才被编译出来——这是 feature 影响能力的直接体现。
- handler 是返回 `IntoResponse` 的 `async fn`；`Html<T>` 是给响应自动加上 `text/html` 的 newtype，复用了元组的 `IntoResponse` 实现。
- axum 在网络层只做「适配 + 任务 spawn」，真正的 socket 读写和 HTTP 解析由 tokio/hyper 完成，印证了「hyper 之上薄薄一层」的定位。

## 7. 下一步学习建议

- 想深入了解 `Router` 内部如何存储和匹配路由，进入 **u2-l1（Router 与路由注册）** 与 **u2-l2（路径匹配与路径参数）**。
- 想知道 handler 的多个参数（`Path`/`Query`/`Json` 等）从哪来，进入 **u3-l1（FromRequestParts 与 FromRequest 双 trait 机制）**，那是 axum 最有特色的部分。
- 想理解 `Router<S>` 的泛型 `S` 和 `.with_state`，进入 **u3-l5（State 提取器与 FromRef 子状态）**。
- 在继续之前，建议先把本讲的 `hello-world` 在本地彻底跑通，并对着源码把「一条请求从 TCP 进来、到 handler 返回、再写回 TCP」的调用链在脑子里走一遍——这条链是后面所有讲义的共同地基。
