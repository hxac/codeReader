# Axum 应用与路由表

## 1. 本讲目标

上一讲（u2-l3）我们跟踪了 `server::startup` 如何把各个子系统装配起来。本讲聚焦启动流水线的「最后一公里」：**`build_app` 如何把这些子系统捏合成一个可以被 `axum_server` 真正 `serve` 的 `axum::Router`**。

学完本讲，你应当能够：

- 说清 `build_app` 产出的 `Router` 由哪几部分组成（五大路由组 + 全局中间件链 + fallback + state）。
- 区分 `public / protected / admin / worker / mesh` 五个路由组的职责与认证保护范围。
- 理解 `protected` 组上 `concurrency_limit / auth / wasm` 三层 `route_layer` 的添加顺序与执行顺序，以及为什么认证要用 `route_layer` 而不是 `layer`。
- 掌握 `readiness` / `liveness` 的语义，以及 `/readiness` 按部署模式给出的就绪判定。
- 会往 `build_app` 里新增一个受认证保护的端点，并写测试验证「未带 key 返回 401」。

## 2. 前置知识

- **axum**：SGLang 网关使用的 Web 框架。它的核心是 `Router`，通过 `.route()` 注册路径与处理函数，通过 `.layer()` / `.route_layer()` 叠加中间件，最终用 `.with_state()` 注入共享状态后交给 `axum_server::serve`。
- **Tower 中间件**：axum 的中间件遵循 Tower 的 `Layer` / `Service` 模型。一个「层」包裹住内层服务，请求先经过外层、再到达内层。
- **控制面 vs 数据面**（u1-l1 已建立）：`/v1/chat/completions` 这类转发推理请求的端点属于数据面；`/workers`、`/flush_cache`、`/ha/*` 这类管理端点属于控制面。本讲的关键就是给这两类端点套上**不同强度**的认证保护。
- **AppState 与 AppContext**（u2-l4 已建立）：`AppContext` 是全程序共享底座；`AppState`（本讲会读）则在此基础上再打包一个 `Arc<dyn RouterTrait>`，作为 axum handler 的 `State`。

> 本讲只需读两个文件：`src/server.rs`（路由与 handler）和 `src/middleware.rs`（中间件实现）。`crate::auth` 实为重导出的姊妹 crate `smg_auth`（见 `src/lib.rs:2` 的 `pub use smg_auth as auth;`），本讲只用到它在 `server.rs` 中暴露的接口，不深入其内部。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [src/server.rs](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs) | 路由组装与 handler | `build_app`、五大路由组、`readiness`、`AppState` |
| [src/middleware.rs](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/middleware.rs) | 中间件实现 | `AuthConfig`、`auth_middleware`、`concurrency_limit_middleware`、`wasm_middleware`、全局日志/指标层 |

## 4. 核心概念与源码讲解

### 4.1 build_app：把路由与中间件组装成 Axum 应用

#### 4.1.1 概念说明

`build_app` 是一个**纯装配函数**：它不创建任何业务对象，只接收已经装配好的 `AppState` 和若干配置，把它们组织成一个 `axum::Router`。它是 `server::startup` 的收尾步骤——前面所有子系统（AppContext、RouterManager、Mesh、JobQueue……）都已就位，`build_app` 只负责「接线」。

接线的结果是一个 `Router<()>`（即不再缺任何 state、可直接 `serve` 的路由器），其结构可以用一句话概括：

> **五组子路由 merge 在一起 → 套一层全局中间件链 → 挂一个 fallback → 注入 state。**

#### 4.1.2 核心流程

```
build_app(app_state, auth_config, control_plane_auth_state, ...)
│
├─ 1. protected_routes：数据面推理端点
│      .route("/generate", ...)  .route("/v1/chat/completions", ...)  ...
│      .route_layer(concurrency_limit)   ┐ 只包裹上面这些 route
│      .route_layer(auth)                │ 三层中间件
│      .route_layer(wasm)                ┘
│
├─ 2. public_routes：探活/元信息（/liveness /readiness /v1/models ...）   无认证
├─ 3. admin_routes：/flush_cache /parse/* /wasm /v1/tokenizers ...         控制面认证
├─ 4. worker_routes：/workers CRUD                                        控制面认证
├─ 5. mesh_routes：/ha/*                                                  普通 API key 认证
│
├─ Router::new()
│      .merge(protected).merge(public).merge(admin).merge(worker).merge(mesh)
│      .layer(DefaultBodyLimit)        ┐
│      .layer(RequestBodyLimit)        │
│      .layer(create_logging_layer)    │ 全局中间件链（包裹所有路由）
│      .layer(HttpMetricsLayer)        │
│      .layer(RequestIdLayer)          │
│      .layer(CORS)                    ┘
│      .fallback(sink_handler)         # 未匹配路径 → 404
│      .with_state(app_state)          # 注入共享状态，得到 Router<()>
└─ 返回 Router
```

#### 4.1.3 源码精读

**AppState**——axum handler 依赖的 `State`。它在 `AppContext` 之外再追加一个 `Arc<dyn RouterTrait>`（真正干活的数据面路由器）和几个 Mesh 相关字段：

```rust
// src/server.rs:70-78
pub struct AppState {
    pub router: Arc<dyn RouterTrait>,
    pub context: Arc<AppContext>,
    pub concurrency_queue_tx: Option<tokio::sync::mpsc::Sender<QueuedRequest>>,
    pub router_manager: Option<Arc<RouterManager>>,
    pub mesh_handler: Option<Arc<MeshServerHandler>>,
    pub mesh_sync_manager: Option<Arc<MeshSyncManager>>,
}
```

[server.rs:70-78](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L70-L78)：注意 `concurrency_queue_tx` / `mesh_*` 都是 `Option`——当限流或 Mesh 未启用时为 `None`，对应中间件会走「跳过」分支。

**build_app 签名**——它吃的是「已就绪的状态 + 配置」，吐的是 `Router`：

```rust
// src/server.rs:536-543
pub fn build_app(
    app_state: Arc<AppState>,
    auth_config: AuthConfig,
    control_plane_auth_state: Option<crate::auth::ControlPlaneAuthState>,
    max_payload_size: usize,
    request_id_headers: Vec<String>,
    cors_allowed_origins: Vec<String>,
) -> Router {
```

[server.rs:536-543](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L536-L543)：`control_plane_auth_state` 是 `Option`，决定 `admin`/`worker` 组用「控制面认证」还是退化为「普通 API key 认证」（详见 4.3）。

**protected_routes（数据面）**——所有推理转发端点都在这里，并且只对这一组套了三层 `route_layer`：

```rust
// src/server.rs:544-591（节选）
let protected_routes = Router::new()
    .route("/generate", post(generate))
    .route("/v1/chat/completions", post(v1_chat_completions))
    // … /v1/completions /v1/rerank /v1/responses /v1/embeddings /v1/classify
    // … /v1/conversations 系列 /v1/tokenize /v1/detokenize
    .route_layer(axum::middleware::from_fn_with_state(app_state.clone(), concurrency_limit_middleware))
    .route_layer(axum::middleware::from_fn_with_state(auth_config.clone(), auth_middleware))
    .route_layer(axum::middleware::from_fn_with_state(app_state.clone(), wasm_middleware));
```

[server.rs:544-591](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L544-L591)：三个 `.route_layer()` 全部写在所有 `.route()` 之后，因此它们包裹的是**上面这一整组推理端点**。

**public_routes（无认证）**——探活与元信息，任何客户端都能访问：

```rust
// src/server.rs:593-605
let public_routes = Router::new()
    .route("/liveness", get(liveness))
    .route("/readiness", get(readiness))
    .route("/health", get(health))
    .route("/health_generate", get(health_generate))
    .route("/engine_metrics", get(engine_metrics))
    .route("/v1/models", get(v1_models))
    .route("/model_info", get(get_model_info))
    .route("/get_model_info", get(get_model_info))   // 待移除的别名
    .route("/server_info", get(get_server_info))
    .route("/get_server_info", get(get_server_info)); // 待移除的别名
```

[server.rs:593-605](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L593-L605)：注意两处带 `// TODO: Remove ... alias after one release-cycle deprecation window` 的注释——`/get_model_info`、`/get_server_info` 是旧路径别名，与 `/model_info`、`/server_info` 指向同一个 handler，留一个发布周期的过渡期后删除。`admin_routes` 里的 `/get_loads` 也是同样的别名处理（[server.rs:611-612](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L611-L612)）。

**admin_routes / worker_routes（控制面）**——管理类端点，认证由 `apply_control_plane_auth` 统一加挂（见 4.3）：

```rust
// src/server.rs:640-655
let apply_control_plane_auth = |routes: Router<Arc<AppState>>| {
    if let Some(ref cp_state) = control_plane_auth_state {
        routes.route_layer(/* control_plane_auth_middleware */)
    } else {
        routes.route_layer(/* 普通 auth_middleware */)
    }
};
let admin_routes = apply_control_plane_auth(admin_routes);
let worker_routes = apply_control_plane_auth(worker_routes);
```

[server.rs:640-655](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L640-L655)：`admin`（`/flush_cache`、`/parse/*`、`/wasm`、`/v1/tokenizers`，[server.rs:608-630](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L608-L630)）与 `worker`（`/workers` CRUD，[server.rs:633-638](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L633-L638)）共享同一套认证策略。

**最终装配**——merge 五组、套全局链、挂 fallback、注入 state：

```rust
// src/server.rs:676-694
Router::new()
    .merge(protected_routes)
    .merge(public_routes)
    .merge(admin_routes)
    .merge(worker_routes)
    .merge(mesh_routes)
    .layer(DefaultBodyLimit::max(max_payload_size))
    .layer(RequestBodyLimitLayer::new(max_payload_size))
    .layer(create_logging_layer())
    .layer(HttpMetricsLayer::new(app_state.context.inflight_tracker.clone()))
    .layer(RequestIdLayer::new(request_id_headers))
    .layer(create_cors_layer(cors_allowed_origins))
    .fallback(sink_handler)
    .with_state(app_state)
```

[server.rs:676-694](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L676-L694)：

- `merge` 把五组路由表合并成一个；`.fallback(sink_handler)` 处理任何未匹配路径——`sink_handler` 直接返回 `404 NOT_FOUND`（[server.rs:94-96](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L94-L96)）。
- 六个 `.layer()` 是**横切所有路由**的全局关注点：请求体大小限制（两层）、结构化日志/Trace、HTTP 指标、请求 ID、CORS。
- `.with_state(app_state)` 把共享状态注入，类型由 `Router<Arc<AppState>>` 变为 `Router<()>`，随后即可 `serve`。

**全局层的执行顺序**：在 axum 中，`.layer()` 链里**先添加的层在外层、最先收到请求**。因此上面这条链的请求流方向是：

```
请求 → DefaultBodyLimit → RequestBodyLimit → logging(Trace) → HttpMetrics
     → RequestId → CORS → (命中的路由组及其 route_layer) → handler
```

把「请求体超限拦截」放在最外层是合理的：超大请求在进入日志/指标/认证之前就被拒掉（413）。这条顺序有一处**代码内证据**：`RequestIdLayer` 加在 `create_logging_layer()`（TraceLayer）之后，而 `RequestSpan::make_span` 的注释明确写道「`RequestIdLayer` 在 `TraceLayer` 创建 span 之后才运行」「此时 request_id 还不可用」（[middleware.rs:273-282](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/middleware.rs#L273-L282)）——这说明「后添加 = 更内层 = 更晚执行」。

> 提示：axum 的 `Router::layer`/`route_layer` 顺序与 Tower 的 `ServiceBuilder` **相反**：axum 中先添加的是外层（最先执行）。这是社区常见的混淆点，请以本仓库代码注释为准。

#### 4.1.4 代码实践

> 实践目标：往 `build_app` 的 `protected` 组新增一个端点 `/v1/echo`，让它自动继承三层中间件（含认证）；然后写一个测试，验证「未带 Authorization 头 → 401」「带正确 Bearer key → 非 401」。

**操作步骤**

1. 在 `src/server.rs` 增加一个最小 handler（示例代码，非项目原有）：

   ```rust
   // 示例代码：一个受认证保护的最小端点
   async fn v1_echo(State(state): State<Arc<AppState>>, Json(body): Json<Value>) -> Response {
       (StatusCode::OK, Json(json!({ "echo": body }))).into_response()
   }
   ```

2. 把它注册进 `protected_routes`，**务必放在三个 `.route_layer(...)` 之前**，否则中间件不会包裹它：

   ```rust
   // src/server.rs，protected_routes 内，在 .route_layer 之前添加
   .route("/v1/echo", post(v1_echo))
   ```

   > 关键点：`route_layer` 只包裹「在它之前注册的路由」。这是 axum 文档明确的行为——「middleware 只作用于已存在的路由，之后新增的路由不会被加上」（见 [axum Router::route_layer 文档](https://docs.rs/axum/0.8.6/axum/routing/struct.Router.html#method.route_layer)）。

3. 编写测试。仓库已有现成的测试脚手架 `AppTestContext` + `TestRouterConfig`（见 `tests/common/mod.rs` 与 `tests/common/test_config.rs`）。`RouterConfig` 的 `api_key` 是公开字段（`src/config/types.rs:29`），把它设上即可让 `protected` 组强制认证（因为 `auth_middleware` 仅在 `api_key` 为 `Some` 时校验）。在 `tests/security/auth_test.rs` 风格下新增：

   ```rust
   // 示例代码：验证未带 key 返回 401
   #[tokio::test]
   async fn test_new_protected_endpoint_requires_api_key() {
       let mut config = TestRouterConfig::round_robin(4310);
       config.api_key = Some("secret-key".into()); // 开启 protected 组认证

       let ctx = AppTestContext::new_with_config(
           config,
           vec![TestWorkerConfig::healthy(20310)],
       )
       .await;
       let app = ctx.create_app().await;

       // 1) 不带 Authorization → 期望 401
       let req = Request::builder()
           .method("POST")
           .uri("/v1/echo")
           .header(CONTENT_TYPE, "application/json")
           .body(Body::from(serde_json::to_string(&json!({"text":"hi"})).unwrap()))
           .unwrap();
       let resp = app.clone().oneshot(req).await.unwrap();
       assert_eq!(resp.status(), StatusCode::UNAUTHORIZED);

       // 2) 带正确 Bearer key → 不再是 401（应为 200）
       let req = Request::builder()
           .method("POST")
           .uri("/v1/echo")
           .header(CONTENT_TYPE, "application/json")
           .header("Authorization", "Bearer secret-key")
           .body(Body::from(serde_json::to_string(&json!({"text":"hi"})).unwrap()))
           .unwrap();
       let resp = app.oneshot(req).await.unwrap();
       assert_ne!(resp.status(), StatusCode::UNAUTHORIZED);

       ctx.shutdown().await;
   }
   ```

**需要观察的现象**

- 不带 header 时返回 `401 Unauthorized`（被 `auth_middleware` 拦下）。
- 带 `Authorization: Bearer secret-key` 时返回 `200`（`v1_echo` 的实现）。
- 若你把 `.route("/v1/echo", ...)` 误放到三个 `.route_layer(...)` **之后**，则该端点**不会**被认证保护——不带 key 也会 200。这恰好验证了「route_layer 只作用于之前的路由」。

**预期结果**

```
cargo test --test security_tests test_new_protected_endpoint_requires_api_key
```

测试通过。若失败，先用 `cargo run -- --help` 确认 `api_key` 配置链路，再检查路由是否放在了 `route_layer` 之前。

> 说明：本实践需要修改 `src/server.rs`。若你只想阅读而不改源码，可改为「源码阅读型」：追踪 `/generate` 端点（[server.rs:545](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L545)）→ `generate` handler（[server.rs:172-182](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L172-L182)）→ `route_chat` 的调用链，确认它受三层中间件保护。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `.fallback(sink_handler)` 删掉，访问一个不存在的路径会发生什么？

> **答案**：axum 默认 fallback 返回 `404 Not Found`。删掉自定义的 `sink_handler` 后行为基本一致（仍是 404），区别在于 `sink_handler` 是项目显式定义的统一出口（[server.rs:94-96](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L94-L96)），便于将来统一加日志或自定义错误体。

**练习 2**：`/get_model_info` 和 `/model_info` 为什么同时存在？

> **答案**：前者是旧路径别名，正在废弃过渡期内（注释 `// TODO: Remove ... alias after one release-cycle deprecation window`，[server.rs:601-602](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L601-L602)）。两者指向同一 handler `get_model_info`，保证旧客户端在一个发布周期内不被破坏。

---

### 4.2 三层 route_layer 中间件与执行顺序

#### 4.2.1 概念说明

`protected_routes` 是唯一套了三层 `route_layer` 的路由组。这三层分别负责：

- **`wasm_middleware`**：WASM 插件层，可在请求/响应阶段执行用户上传的 WASM 模块（鉴权、限流、改写等）。未启用时近乎零成本放行。
- **`auth_middleware`**：Bearer token 校验。
- **`concurrency_limit_middleware`**：令牌桶限流 + 排队；命中 mesh 全局限流时直接 429。

#### 4.2.2 核心流程

代码里的添加顺序是（[server.rs:580-591](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L580-L591)）：

```
.route_layer(concurrency_limit_middleware)   // 1️⃣ 先添加
.route_layer(auth_middleware)                 // 2️⃣
.route_layer(wasm_middleware)                 // 3️⃣ 后添加
```

由于 axum 中「先添加 = 外层 = 最先收到请求」，这三层在**请求进入**时的执行顺序为：

```
请求 → concurrency_limit → auth → wasm → handler
响应 ← concurrency_limit ← auth ← wasm ← handler   （响应沿原路返回）
```

也就是说，`concurrency_limit` 最先看到请求（最先尝试获取令牌），`wasm` 离 handler 最近。

#### 4.2.3 源码精读

三层 `route_layer` 的注册（含 `from_fn_with_state` 注入各自所需 state）：

```rust
// src/server.rs:580-591
.route_layer(axum::middleware::from_fn_with_state(
    app_state.clone(),
    middleware::concurrency_limit_middleware,
))
.route_layer(axum::middleware::from_fn_with_state(
    auth_config.clone(),
    middleware::auth_middleware,
))
.route_layer(axum::middleware::from_fn_with_state(
    app_state.clone(),
    middleware::wasm_middleware,
));
```

[server.rs:580-591](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L580-L591)：注意 `auth_middleware` 的 state 是 `AuthConfig`（仅含 `api_key`），而另外两层用的是 `Arc<AppState>`。

**为什么认证用 `route_layer` 而不是 `layer`？** axum 官方文档对此有专门说明：`route_layer` 与 `layer` 的区别在于它**只在请求匹配到某条路由时才运行**，「这对会提前返回的中间件（如鉴权）很有用，否则会把 `404 Not Found` 变成 `401 Unauthorized`」（见 [axum 文档](https://docs.rs/axum/0.8.6/axum/routing/struct.Router.html#method.route_layer)）。换句话说：

- 用 `route_layer(auth)`：访问**不存在**的路径 → 命中 fallback → `404`；访问**存在但未授权**的 protected 端点 → `401`。语义正确。
- 若误用全局 `layer(auth)`：访问不存在的路径也会先被 auth 拦成 `401`，泄露「该路径是否存在」的信息，语义错误。

**关于「令牌在 auth 之前获取」是否合理**：当 `api_key` 未配置时（默认情况），`auth_middleware` 直接放行（[middleware.rs:119](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/middleware.rs#L119) 的 `if let Some(expected_key)` 为假时走 `next.run`），此时顺序几乎无成本；当配置了 `api_key` 时，未授权请求会先获取令牌、随后被 auth 拒成 401，响应回程时 `concurrency_limit` 的 `TokenGuardBody` 会在响应体耗尽时归还令牌（[middleware.rs:49-105](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/middleware.rs#L49-L105)），令牌只被极短暂占用。

#### 4.2.4 代码实践

> 实践目标：通过「阅读 + 观察」验证三层中间件的存在与短路行为。

1. 打开 `src/middleware.rs`，分别定位三层的「提前返回」分支：
   - `wasm_middleware`：`enable_wasm` 关闭时 `return Ok(next.run(request).await)`（[middleware.rs:767-769](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/middleware.rs#L767-L769)）。
   - `auth_middleware`：`api_key` 为 `None` 时直接放行（[middleware.rs:119](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/middleware.rs#L119)）。
   - `concurrency_limit_middleware`：`rate_limiter` 为 `None` 时直接放行（[middleware.rs:519-525](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/middleware.rs#L519-L525)）；mesh 全局限流超限返回 429（[middleware.rs:500-517](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/middleware.rs#L500-L517)）。
2. 推理一次完整请求的穿越路径：`concurrency_limit`（尝试取令牌）→ `auth`（校验 Bearer）→ `wasm`（执行插件）→ `route_chat` → worker。

**预期结果**：你能口述出「未配 api_key 时，auth 层形同虚设；未开 wasm 时，wasm 层零成本放行；只有 concurrency_limit 在限流配置开启时才真正介入」。

> 待本地验证：若你本地启用了 mesh（`--enable-mesh`）并设置了全局限流，可在超限时观察到 `concurrency_limit_middleware` 返回的 429 JSON 体。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `auth_middleware` 用 `route_layer` 而不是全局 `layer`？

> **答案**：`route_layer` 只在请求命中已注册路由时运行，避免把对「不存在路径」的访问也变成 `401`，从而不泄露路由表信息，并让 404/401 语义正确。

**练习 2**：三层中哪一层离 handler 最近？请求最先经过哪一层？

> **答案**：`wasm_middleware` 最后添加、最内层，离 handler 最近；`concurrency_limit_middleware` 最先添加、最外层，请求最先经过它。

---

### 4.3 AuthConfig 与认证保护范围

#### 4.3.1 概念说明

网关有两套认证强度：

- **普通认证（`AuthConfig` + `auth_middleware`）**：单一共享 API key，校验 `Authorization: Bearer <key>`。保护 `protected`（数据面）和 `mesh`（`/ha/*`）路由组。
- **控制面认证（`crate::auth::ControlPlaneAuthConfig`，来自 `smg_auth`）**：更丰富，支持 API key 列表 + JWT/OIDC + RBAC 角色。保护 `admin` 和 `worker` 路由组。若未配置控制面认证，则**降级**为普通 `auth_middleware`。

`AuthConfig` 本身极其简单：

```rust
// src/middleware.rs:107-110
#[derive(Clone)]
pub struct AuthConfig {
    pub api_key: Option<String>,
}
```

[middleware.rs:107-110](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/middleware.rs#L107-L110)：`api_key` 为 `None` 表示「不开启认证」。

#### 4.3.2 核心流程

```
请求命中 protected/mesh 端点
   └─ auth_middleware:
        if api_key.is_none()  → 直接放行（next.run）
        else 取 Authorization 头:
            不是 "Bearer xxx"  → 401
            长度不等            → 401
            常量时间比较不等     → 401
            否则                → 放行
```

控制面端点（admin/worker）则由 `apply_control_plane_auth` 决定走哪条认证。

#### 4.3.3 源码精读

**auth_middleware 实现**——只在配置了 `api_key` 时校验，且用常量时间比较防时序攻击：

```rust
// src/middleware.rs:114-148（节选）
pub async fn auth_middleware(
    State(auth_config): State<AuthConfig>,
    request: Request<Body>,
    next: Next,
) -> Result<Response, StatusCode> {
    if let Some(expected_key) = &auth_config.api_key {
        let auth_header = request.headers().get(header::AUTHORIZATION) /* … */;
        match auth_header {
            Some(v) if v.starts_with("Bearer ") => {
                let token = &v[7..];
                // 先比长度，再常量时间比较，防止时序侧信道
                if token.as_bytes().len() != expected_key.as_bytes().len()
                    || token.as_bytes().ct_eq(expected_key.as_bytes()).unwrap_u8() != 1
                {
                    return Err(StatusCode::UNAUTHORIZED);
                }
            }
            _ => return Err(StatusCode::UNAUTHORIZED),
        }
    }
    Ok(next.run(request).await)
}
```

[middleware.rs:114-148](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/middleware.rs#L114-L148)：两个要点——(1) `api_key` 为 `None` 时整段 `if` 不进入，直接放行；(2) 用 `subtle::ConstantTimeEq`（`ct_eq`）比较，避免因比较耗时差异泄露 key 长度/前缀信息。注意它**只认 `Authorization: Bearer`**，不校验 `X-API-Key` 之类的头。

**控制面认证的分流**——`apply_control_plane_auth` 闭包：

```rust
// src/server.rs:641-655
let apply_control_plane_auth = |routes: Router<Arc<AppState>>| {
    if let Some(ref cp_state) = control_plane_auth_state {
        routes.route_layer(axum::middleware::from_fn_with_state(
            cp_state.clone(),
            crate::auth::control_plane_auth_middleware,
        ))
    } else {
        routes.route_layer(axum::middleware::from_fn_with_state(
            auth_config.clone(),
            middleware::auth_middleware,
        ))
    }
};
```

[server.rs:641-655](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L641-L655)：控制面认证状态 `control_plane_auth_state` 在 `startup` 中由 `ControlPlaneAuthState::try_init` 初始化（[server.rs:1037-1039](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L1037-L1039)），再传入 `build_app`。有则用强控制面认证，无则退化为普通 API key。

把五大路由组的认证范围汇总成表：

| 路由组 | 代表端点 | 认证方式 |
| --- | --- | --- |
| `public` | `/liveness` `/readiness` `/v1/models` | 无 |
| `protected` | `/generate` `/v1/chat/completions` `/v1/responses` | 普通 `auth_middleware`（`AuthConfig.api_key`） |
| `admin` | `/flush_cache` `/parse/*` `/wasm` `/v1/tokenizers` | 控制面认证，未配置则降级为 `auth_middleware` |
| `worker` | `/workers` CRUD | 同 admin |
| `mesh` | `/ha/*` | 普通 `auth_middleware` |

#### 4.3.4 代码实践

> 实践目标：用 curl 直观验证 `protected` 组的认证开关。

1. 启动一个 mock worker（例如用 `python3 -m http.server` 或任意返回 200 的服务）。
2. 不带 key 启动网关（默认 `api_key` 为 `None`）：
   ```bash
   smg --worker-urls http://127.0.0.1:<worker_port> --port 3001
   ```
   发请求（不带 Authorization）：
   ```bash
   curl -s -o /dev/null -w "%{http_code}\n" \
     -X POST http://127.0.0.1:3001/generate \
     -H "Content-Type: application/json" -d '{"text":"hi"}'
   ```
   **预期**：`200`（未开启认证，放行）。
3. 带 key 启动：
   ```bash
   smg --api-key secret-key --worker-urls http://127.0.0.1:<worker_port> --port 3001
   ```
   - 不带 header：预期 `401`。
   - 带 `-H "Authorization: Bearer secret-key"`：预期 `200`。
   - 访问不存在的路径 `/nope`（不带 header）：预期 `404`（证明 auth 用了 `route_layer`，未把 404 变成 401）。

> 待本地验证：具体 mock worker 的搭建方式与请求体 schema 请以本地可用的最小服务为准；重点是观察 401/200/404 的差异，而非具体的转发结果。

#### 4.3.5 小练习与答案

**练习 1**：`api_key` 未配置时，`auth_middleware` 会做什么？

> **答案**：`if let Some(expected_key)` 不命中，直接执行 `Ok(next.run(request).await)` 放行（[middleware.rs:119,147](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/middleware.rs#L119)）。即「不配置 = 不开启认证」。

**练习 2**：`admin` 组在什么情况下会用普通 `auth_middleware` 而不是控制面认证？

> **答案**：当 `control_plane_auth_state` 为 `None` 时——即启动时未配置任何 `ControlPlaneAuthConfig`。此时 `apply_control_plane_auth` 走 `else` 分支，退化为单一 API key 校验（[server.rs:647-652](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L647-L652)）。

---

### 4.4 readiness 与 liveness：就绪判定

#### 4.4.1 概念说明

Kubernetes 等编排系统通常区分两种探针：

- **liveness（存活）**：进程是否还活着。只要能回 200 就算活。失败通常触发**重启**。
- **readiness（就绪）**：是否准备好接流量。失败只会把 Pod 从负载均衡里**摘除**，不重启。

网关把 `/liveness`、`/health` 都实现为「无条件 200」，把真正的业务就绪判定放在 `/readiness`。这与 u2-l3 讲过的「网关在 worker 尚未全部注册时就已经 bind 对外」相呼应——bind 不代表就绪，就绪由 `/readiness` 说了算。

#### 4.4.2 核心流程

```
/liveness、/health        → 恒 200 "OK"
/readiness:
   取所有 worker，筛出 healthy
   按 mode 判定 is_ready:
      enable_igw            → 至少 1 个健康 worker
      PrefillDecode          → 至少 1 个健康 prefill 且 至少 1 个健康 decode
      Regular / OpenAI       → 至少 1 个健康 worker
   is_ready  → 200 {status:"ready", healthy_workers, total_workers}
   否则      → 503 {status:"not ready", reason:"insufficient healthy workers"}
```

#### 4.4.3 源码精读

**liveness / health**——极简，永远 200：

```rust
// src/server.rs:98-100
async fn liveness() -> Response {
    (StatusCode::OK, "OK").into_response()
}
// src/server.rs:146-148
async fn health(_state: State<Arc<AppState>>) -> Response {
    liveness().await
}
```

[server.rs:98-100](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L98-L100)、[server.rs:146-148](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L146-L148)：`/health` 只是 `/liveness` 的别名。

**readiness**——按部署模式给出不同判定：

```rust
// src/server.rs:102-122（节选）
let is_ready = if state.context.router_config.enable_igw {
    !healthy_workers.is_empty()
} else {
    match &state.context.router_config.mode {
        RoutingMode::PrefillDecode { .. } => {
            let has_prefill = healthy_workers
                .iter().any(|w| matches!(w.worker_type(), WorkerType::Prefill { .. }));
            let has_decode = healthy_workers
                .iter().any(|w| matches!(w.worker_type(), WorkerType::Decode));
            has_prefill && has_decode
        }
        RoutingMode::Regular { .. } => !healthy_workers.is_empty(),
        RoutingMode::OpenAI { .. } => !healthy_workers.is_empty(),
    }
};
```

[server.rs:102-122](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L102-L122)：PD（PrefillDecode）模式要求 prefill 与 decode **两类** worker 各至少一个健康才算就绪——这正是 PD 拓扑能正常工作的最低条件（缺任一类都无法完成「前置填 + 解码」流水线）。其余模式只要有任意健康 worker 即可。

判定后返回结构化 JSON（[server.rs:124-143](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L124-L143)）：就绪时 `200 {status:"ready", healthy_workers, total_workers}`，未就绪时 `503 {status:"not ready", reason:"insufficient healthy workers"}`。

#### 4.4.4 代码实践

> 实践目标：观察 `/readiness` 随 worker 健康状态翻转。

1. 启动网关但**不注册任何健康 worker**（例如不给 `--worker-urls`，或指向一个未启动的地址）：
   ```bash
   smg --port 3001
   curl -s http://127.0.0.1:3001/liveness   # 200 OK
   curl -s -o /dev/null -w "%{http_code}\n" http://127.0.0.1:3001/readiness   # 预期 503
   ```
2. 通过 `POST /workers` 注册一个健康的 worker（或在启动时 `--worker-urls` 指向一个已就绪的服务），等待健康检查通过后：
   ```bash
   curl -s http://127.0.0.1:3001/readiness   # 预期 200，并显示 healthy_workers/total_workers
   ```

**预期结果**：`/liveness` 始终 200；`/readiness` 在无健康 worker 时 503、有健康 worker 时 200。这正对应编排系统「存活探针用 /liveness、就绪探针用 /readiness」的最佳实践。

> 待本地验证：worker 注册与健康检查的耗时不定；若 503 持续，可轮询 `/readiness` 或查看 `/engine_metrics` 确认 worker 状态。

#### 4.4.5 小练习与答案

**练习 1**：PD 模式下，只有 prefill worker 健康、decode worker 全部不健康时，`/readiness` 返回什么？

> **答案**：`503 not ready`。PD 模式要求 `has_prefill && has_decode` 同时为真（[server.rs:111-118](https://github.com/sgl-project/sglang/blob/40b2119b23e49be767da1f9f73746ac8e158dae5/sgl-model-gateway/src/server.rs#L111-L118)），缺 decode 即不就绪，此时 Pod 会被从负载均衡摘除，避免接收无法完成的请求。

**练习 2**：为什么把存活判定（liveness）和就绪判定（readiness）分成两个端点？

> **答案**：存活探针失败会触发重启，就绪探针失败只摘流量不重启。网关启动时 worker 可能尚未注册完，此时进程是活的（liveness=200）但还没准备好接流量（readiness=503），分开后编排系统不会误杀进程、只是暂时不导流，等 worker 就绪后自动恢复。

---

## 5. 综合实践

把本讲内容串起来，完成一个「新增受保护端点 + 验证就绪与认证」的小任务：

1. **新增端点**：按 4.1.4 在 `protected_routes` 增加 `POST /v1/echo`（放在三个 `route_layer` 之前）。
2. **写两类测试**（参考 `tests/security/auth_test.rs`）：
   - 未配置 `api_key` 时，`/v1/echo` 不带 header 返回 200。
   - 配置 `config.api_key = Some("secret".into())` 后，不带 header 返回 401、带 `Bearer secret` 返回 200。
3. **验证 404 语义**：在配置了 `api_key` 的情况下，访问不存在的 `/v1/nope`（不带 header）应返回 **404** 而非 401——这正是 `route_layer`（而非全局 `layer`）用于认证的体现。
4. **验证就绪翻转**：在测试里用 `AppTestContext` 起一个无 worker 的网关，断言 `GET /readiness` 返回 503；注册健康 worker 后断言变为 200（可对照 `readiness` handler 的 mode 分支）。

完成后，你应当能清晰说出：一个请求从进入 `axum_server`，到穿过全局层 → 命中路由组 → 穿过 `route_layer` 三层 → 到达 handler，再到 `/readiness` 如何决定它能不能被外部负载均衡调用。

## 6. 本讲小结

- `build_app` 是纯装配函数：把 `protected/public/admin/worker/mesh` 五组路由 `merge` 后套六层全局中间件、挂 `sink_handler` fallback、`.with_state()` 注入 `AppState`，产出可 `serve` 的 `Router<()>`。
- 认证按强度分层：`public` 无认证；`protected`/`mesh` 走普通 `auth_middleware`（单一 `AuthConfig.api_key`）；`admin`/`worker` 走控制面认证（`smg_auth`），未配置则降级为 `auth_middleware`。
- `protected` 组的三层 `route_layer`（`concurrency_limit` → `auth` → `wasm`）按「先添加为外层」执行；认证用 `route_layer` 是为了保证 404/401 语义正确。
- `auth_middleware` 仅在 `api_key` 为 `Some` 时校验 `Authorization: Bearer`，并用常量时间比较防时序攻击。
- `/liveness`、`/health` 恒 200；`/readiness` 按模式判定（PD 需 prefill+decode 双类健康），就绪 200、未就绪 503，分别对应存活探针与就绪探针。

## 7. 下一步学习建议

- 下一讲 u3（控制面：Worker 生命周期）将进入 `/workers` 这组路由背后的 `WorkerService`、`JobQueue` 与 `steps` 工作流，本讲的 `admin`/`worker` 路由组是它们的 HTTP 入口。
- 想深入可靠性，可继续阅读 `src/middleware.rs` 的 `concurrency_limit_middleware` / `TokenGuardBody`（u6-l1、u6-l3）与 `src/core/circuit_breaker.rs`（u6-l2）。
- 想了解控制面认证的完整能力（API key 列表、JWT/OIDC、RBAC 角色），可阅读 `tests/security/auth_integration_test.rs` 中对 `ControlPlaneAuthConfig` 的用法，这是 `smg_auth` crate 在本仓库的集成侧入口。
