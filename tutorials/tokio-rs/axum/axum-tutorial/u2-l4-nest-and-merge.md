# 嵌套 nest 与合并 merge

## 1. 本讲目标

到目前为止，我们注册路由的方式都是「扁平」的：在一个 `Router` 上不断调用 `.route()`，每条路由写出完整路径（如 `/api/users/{id}`、`/api/orders/{id}`）。当路由数量一多，这种写法会变得又长又难维护，不同业务模块（用户、订单、团队……）的路由也混在了一起。

本讲解决的就是「**如何把多个 `Router` 组装成一棵可复用的路由树**」。学完后你应当能够：

- 用 `Router::nest("/api", child)` 给一组子路由统一加上前缀，把一个大应用拆成可独立维护的小 `Router`。
- 说清为什么嵌套路由里的 handler「看不到」完整的原始 URI——即 `StripPrefix` 在请求进入子路由前做了什么改写。
- 用 `Router::merge` 把两个 `Router` 扁平地合并成一个，并知道当双方都设置了 `fallback` 时会 panic、该如何用 `reset_fallback` 解决。
- 精确区分 `layer` 与 `route_layer`：前者连 fallback 一起包裹，后者只包裹「已经注册的路由」、常用于鉴权中间件，且在没有路由时调用会 panic。

本讲覆盖三个核心最小模块：**Router::nest**（含 `nest_service`）、**StripPrefix**（嵌套的 URI 改写层）、**Router::merge**；并在最后一节补充贯穿实践所需的 **layer 与 route_layer 的区别**。

## 2. 前置知识

阅读本讲前，你需要已经掌握前几讲建立的认知：

- `Router<S>` 是路由容器，内部由 `RouterInner` 组成，而 `RouterInner` 含三块：`path_router`（正路由表）、`catch_all_fallback`（兜底处理器，默认返回 404 的 `NotFound`）、`default_fallback`（标记位）。见 u2-l1。
- `Router::route(path, method_router)` 把一条「路径 → 方法路由」注册进 `path_router`，最终在 matchit 基数树里占一个节点。见 u2-l1、u2-l2。
- `path_router` 内部同时维护一份 `routes` 向量（按下标存 `Endpoint`）和一棵 matchit 树（`path → RouteId`），二者靠 `RouteId`（即向量下标）关联。见 u2-l2。
- 每条路由最终都被包装成实现了 `tower::Service` 的 `Route`；handler、方法路由都经由 `Endpoint` 枚举（`MethodRouter` 或 `Route` 两种变体）登记。见 u2-l1、u2-l3。
- `tower::Layer` 是「生产 `Service` 的工厂」：`layer.layer(service)` 返回一个被包裹后的新 `Service`。本讲会把 `Layer` 当作现成工具使用，深入原理留到第 5 单元。

一个关键的运行时事实（来自 u2-l1）：`Router` 内部用 `Arc` 共享，所有修改方法（`route`/`nest`/`merge`/`layer`…）都走「写时复制」——拆开 `Arc<RouterInner>`、改字段、重新包回去。本讲的 `tap_inner!` / `map_inner!` 两个宏就是这套骨架的统一封装，理解了这一点，下面的源码读起来会很顺。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `axum/src/routing/mod.rs` | `Router::nest` / `nest_service` / `merge` / `layer` / `route_layer` / `reset_fallback` 的公共 API；`RouterInner` 拆装；`Fallback::merge` 的合并规则；`Endpoint` 枚举与其 `layer` 方法。 |
| `axum/src/routing/path_router.rs` | `PathRouter` 上对应的方法：`nest` / `nest_service` / `merge` / `layer` / `route_layer` 的真正实现，以及 `path_for_nested_route`（前缀拼接）和 `validate_nest_path`（路径校验）。 |
| `axum/src/routing/strip_prefix.rs` | `StripPrefix` 层：在请求进入子路由前剥掉已匹配的前缀，改写 `Request` 的 URI。 |
| `axum/src/docs/routing/nest.md` | `nest` 的官方文档，含 URI 改写、外部捕获、与通配路由的差异、fallback 继承、panic 条件等示例。 |
| `axum/src/docs/routing/layer.md` / `route_layer.md` | `layer` 与 `route_layer` 的官方文档，说明生效范围与「中间件在路由之后运行」的特性。 |

## 4. 核心概念与源码讲解

### 4.1 Router::nest：给子路由加前缀

#### 4.1.1 概念说明

想象一个电商后台，用户相关的路由有一打，订单相关的也有一打。你不想把 `/api/users/...` 和 `/api/orders/...` 的完整路径散落在一个巨大的 `Router::new()` 链里，而是希望：

- 把用户路由封装成一个 `user_routes: Router`，里面的路径写成 `/{id}`、`/` 这样**相对**的样子。
- 把订单路由封装成 `order_routes: Router`。
- 最后用一个「父 Router」把它们分别挂到 `/api/users` 与 `/api/orders` 前缀下。

`nest` 就是干这件事的：**把一个子 `Router` 整体挂到某个路径前缀下**。挂载之后，子路由里写的相对路径会自动拼上父前缀，对外暴露成完整路径。

为什么不用一个超大的 `Router` 直接写完整路径？因为 `nest` 带来三个工程价值：

1. **模块化**：每个子 `Router` 可以放在单独的文件/模块里独立维护、独立测试。
2. **前缀统一管理**：改前缀只改 `nest("/api/v2", ...)` 一处，不用逐条路由改。
3. **中间件隔离**：可以在子 `Router` 上单独加 `layer`/`route_layer`，只对该模块生效（见 4.4）。

#### 4.1.2 核心流程

`nest` 的注册过程可以概括为「**逐条改写路径 + 逐条加两层包装**」：

```
outer.nest("/api", inner):
  1. 校验前缀：必须以 / 开头、不能是根、不能含通配 {*}。
  2. 把 inner 拆成 RouterInner，只取 path_router，
     丢弃 inner 的 catch_all_fallback 与 default_fallback。
  3. 遍历 inner.path_router.routes 里的每条端点：
     a. 查出它在 inner 里的相对路径（如 /{id}）。
     b. 用 path_for_nested_route 拼成完整路径（如 /api/{id}）。
     c. 给端点套上两层 Layer：
        - StripPrefix::layer("/api")   —— 运行时剥掉前缀
        - SetNestedPath::layer(...)     —— 记录嵌套路径用于日志/错误
     d. 用拼好的完整路径，把包装后的端点注册进 outer.path_router。
```

注意第 3 步的关键设计：**子路由里的每一条路由都被「摊平」注册进了父路由的 matchit 树**，最终对外只有一个扁平的路由表。`nest` 不是运行时嵌套查找，而是构建期就把路径全部拼好。这也解释了为什么 `nest` 会在路径冲突时 panic——它和 `route` 用的是同一张 matchit 树，重名节点会冲突。

#### 4.1.3 源码精读

先看 `Router::nest` 的公共入口，它只做拆装和委托：

> `nest` 入口：校验不能在根路径嵌套，然后只取子路由的 `path_router`（显式丢弃 `catch_all_fallback` 与 `default_fallback`），把真正的拼装工作交给 `path_router.nest`。[axum/src/routing/mod.rs:220-237](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L220-L237)

这里有两个值得注意的细节：

1. **在根路径嵌套会 panic**（`path == "/"` 或空）。文档明确建议改用 `merge`。根路径嵌套没有「前缀」可言，语义上等价于合并。
2. **显式丢弃子路由的 `catch_all_fallback`**。源码注释解释了原因：`catch_all_fallback` 只用于 CONNECT 请求的空路径场景，如果继承它，会错误地匹配 `/{path}/*` 却又不能匹配空路径。这个丢弃正是「嵌套路由未命中时会冒泡到外层 fallback」的根源（见 4.1.5）。

真正的拼装逻辑在 `PathRouter::nest`：

> 遍历子路由的每条端点，用 `path_for_nested_route(prefix, inner_path)` 拼出完整路径，给端点套上 `(StripPrefix::layer(prefix), SetNestedPath::layer(path_to_nest_at))` 两层，再注册进父路由。[axum/src/routing/path_router.rs:172-210](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/path_router.rs#L172-L210)

前缀拼接由 `path_for_nested_route` 完成，它处理了三种边界情况（前缀是否以 `/` 结尾、子路径是否就是 `/`）：

> 拼接规则：前缀以 `/` 结尾就直接拼接并去掉重复斜杠；子路径是 `/` 就只取前缀；否则正常拼接。[axum/src/routing/path_router.rs:466-477](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/path_router.rs#L466-L477)

举几个例子帮助理解这张表（设子路由里的路径为 `inner_path`，挂载前缀为 `prefix`）：

| `prefix` | `inner_path` | 拼接结果 |
| --- | --- | --- |
| `/api` | `/{id}` | `/api/{id}` |
| `/api/` | `/{id}` | `/api/{id}`（`trim_start_matches('/')` 去掉重复斜杠） |
| `/api` | `/` | `/api` |
| `/api` | `/list` | `/api/list` |

路径校验在 `validate_nest_path`，它禁止三件事：不以 `/` 开头、是根 `/`、包含通配 `{*...}`：

> 嵌套路径必须以 `/` 开头、长度 ≥ 2（即不能是根），且不允许出现通配捕获段。[axum/src/routing/path_router.rs:445-464](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/path_router.rs#L445-L464)

#### 4.1.4 nest_service：挂任意 Service

`nest` 只接受另一个 `Router`。如果你想挂的是一个普通的 `tower::Service`（比如一个静态文件服务、一个 upstream 代理），用 `nest_service`：

> `nest_service` 接受任意 `Service<Request, Error = Infallible>`，把它当作「整段前缀下的单一服务」挂载。[axum/src/routing/mod.rs:241-254](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L241-L254)

和 `nest` 不同，`nest_service` 没有「子路由列表」可以遍历，它只有一个 Service。于是它用 matchit 的**通配捕获** `{*...}` 来兜住前缀下的所有子路径。实现里会把服务注册到三个路径上，确保前缀本身也能命中：

> 用 `{*NEST_TAIL_PARAM}` 通配兜住前缀下的所有子路径，并额外在前缀本身（`/foo`）和带尾斜杠的形式（`/foo/`）各注册一次，否则 `/foo`、`/foo/` 自身不会命中。[axum/src/routing/path_router.rs:212-249](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/path_router.rs#L212-L249)

这正是官方文档里那句「嵌套在 `/foo` 的 router 会匹配 `/foo`（但不匹配 `/foo/`）；嵌套在 `/foo/` 的会匹配 `/foo/`（但不匹配 `/foo`）」的源码来源——见 [nest.md 第 85-88 行](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/docs/routing/nest.md#L85-L88)。

#### 4.1.5 代码实践：用 nest 组织模块化路由

**实践目标**：把用户和订单两组路由各自封装成子 `Router`，用 `nest` 挂到统一前缀下，验证完整路径能正确命中。

**操作步骤**：

1. 新建一个 binary（参考 `examples/hello-world` 的 `Cargo.toml` 依赖：`axum`、`tokio`）。
2. 写入下面的代码（**示例代码**，仿照 [nest.md 第 8-28 行](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/docs/routing/nest.md#L8-L28) 的官方示例）：

```rust
use axum::{routing::get, Router};

async fn user_by_id() -> &'static str { "user" }
async fn order_by_id() -> &'static str { "order" }

#[tokio::main]
async fn main() {
    let user_routes = Router::new().route("/{id}", get(user_by_id));
    let order_routes = Router::new().route("/{id}", get(order_by_id));

    let api = Router::new()
        .nest("/users", user_routes)
        .nest("/orders", order_routes);

    let app = Router::new().nest("/api", api);

    let listener = tokio::net::TcpListener::bind("127.0.0.1:3000").await.unwrap();
    axum::serve(listener, app).await.unwrap();
}
```

3. `cargo run` 后，用 curl 验证：

```bash
curl http://127.0.0.1:3000/api/users/42   # 期望返回 user
curl http://127.0.0.1:3000/api/orders/7   # 期望返回 order
```

**需要观察的现象**：两条完整路径 `/api/users/{id}`、`/api/orders/{id}` 都能命中各自 handler；而 `/api/teams/1`（未挂载）应返回 404。

**预期结果**：`user` 与 `order` 文本分别返回，证明 `nest` 在构建期把相对路径 `/{id}` 拼成了 `/api/users/{id}` 与 `/api/orders/{id}`。若把 `nest("/api", api)` 改成 `nest("/", api)`，程序会 panic，提示「Nesting at the root is no longer supported. Use merge instead.」。

> ⚠️ 待本地验证：以上 curl 输出依赖你本地实际运行，作者未在此环境执行。

#### 4.1.6 小练习与答案

**练习 1**：如果把上面 `let app = Router::new().nest("/api", api);` 改成 `Router::new().nest("api", api);`（少了前导斜杠），会发生什么？

**参考答案**：程序在启动时 panic。`validate_nest_path` 检查到 `path` 不以 `/` 开头，返回 `Err("Nesting paths must start with a `/`.")`，`panic_on_err!` 把它转成 panic。

**练习 2**：为什么 `nest("/api", inner)` 之后，访问 `/api/users/{id}` 时，handler 里用 `Uri` 提取器拿到的是 `/users/{id}` 而不是 `/api/users/{id}`？

**参考答案**：因为 `nest` 给每条子路由套了 `StripPrefix::layer("/api")`，它在请求进入子路由前就把 URI 里已匹配的 `/api` 前缀剥掉了。若需要原始 URI，应使用 `OriginalUri` 提取器（见 4.2）。

### 4.2 StripPrefix：嵌套路由的 URI 改写

#### 4.2.1 概念说明

上一节反复提到 `StripPrefix`。它是一个 `tower::Layer`，作用只有一个：**在请求被交给子路由之前，把 URI 里「已经匹配上的前缀」剥掉**。

为什么必须剥？因为子路由里的路径是相对的（`/{id}`，而不是 `/api/users/{id}`）。如果请求进来时 URI 还是完整的 `/api/users/42`，子路由的 matchit 树里只有 `/{id}`，根本匹配不上。所以需要在两者之间插一层「翻译」：把 `/api/users/42` 改写成 `/42`（针对最内层的 users 路由），这样相对路径才能命中。

这也是官方文档强调的那句：**「嵌套路由不会看到原始请求 URI，而是看到剥掉前缀后的 URI」**。对静态文件服务这类「只认相对路径」的 Service 尤其重要——它需要看到 `/index.html` 而不是 `/static/index.html`。

#### 4.2.2 核心流程

`StripPrefix` 作为一个 `Service<Request>`，在 `call` 时做一次 URI 改写，再转发给内层 Service：

```
StripPrefix::call(req, prefix="/api"):
  new_uri = strip_prefix(req.uri(), "/api")
  if 能剥掉前缀:
      req.uri = new_uri        // 用剥掉后的 URI 覆盖
  inner.call(req)              // 交给子路由处理
```

注意「**剥不掉就不改**」这个细节：如果当前请求的 URI 根本不以该前缀开头，`strip_prefix` 返回 `None`，此时 URI 原样保留。这保证了 `StripPrefix` 在「不匹配」时不会破坏请求。

`strip_prefix` 内部采用「**逐段比较**」而非简单的字符串前缀切割，因为前缀里可能含捕获参数（如 `/api/{version}`），需要按路径段（segment）一段段匹配：

```
strip_prefix(uri, prefix):
  按 / 把 uri.path 和 prefix 各自切成段
  用 zip_longest 逐段配对：
    - 段都存在且相等（或前缀段是 {参数} 通配）→ 计入匹配长度
    - 前缀段为空（前缀以 / 结尾）→ 视作匹配完成
    - 其它情况 → 不匹配，返回 None
  在匹配长度处切开 uri.path，保留剩余部分作为新 path
  重新拼上 query，组装成新 Uri 返回
```

#### 4.2.3 源码精读

`StripPrefix` 结构体本身非常简单——持有内层 Service 和一个 `Arc<str>` 前缀：

> `StripPrefix<S>` 是个包装层，`prefix` 用 `Arc` 共享以支持廉价克隆。[axum/src/routing/strip_prefix.rs:10-14](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/strip_prefix.rs#L10-L14)

它的 `layer` 工厂方法用 `tower_layer::layer_fn` 把一个闭包变成 `Layer`：

> `StripPrefix::layer` 返回一个 `Layer`，每次 `layer(service)` 都产出新的 `StripPrefix`，并克隆一份 `Arc<str>` 前缀。[axum/src/routing/strip_prefix.rs:16-24](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/strip_prefix.rs#L16-L24)

`Service` 实现是改写的核心——`poll_ready` 直接委托，`call` 改写 URI 后转发：

> `call` 中先尝试 `strip_prefix`，成功则覆盖 `req.uri_mut()`，最后调用内层 Service；失败（不匹配）则 URI 原样保留。[axum/src/routing/strip_prefix.rs:26-45](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/strip_prefix.rs#L26-L45)

逐段匹配的主循环位于 `strip_prefix` 函数内，用 `zip_longest` 把路径段和前缀段配对，累加 `matching_prefix_length`：

> 逐段比较并累加匹配长度；前缀段为空（前缀以 `/` 结尾）或路径更长都算匹配，前缀更长则不匹配。[axum/src/routing/strip_prefix.rs:62-104](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/strip_prefix.rs#L62-L104)

匹配长度确定后，函数在边界处切开 path 并重组 query：

> 在匹配长度处 `split_at`，保留剩余 path；若剩余不以 `/` 开头则补一个前导 `/`，再按是否有 query 拼出新的 `path_and_query`，组装成新 `Uri`。[axum/src/routing/strip_prefix.rs:111-121](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/strip_prefix.rs#L111-L121)

这里有个精巧之处：匹配长度永远落在 `/` 边界上（因为是一段段匹配的），所以 `split_at` 不会切到半个字符，不会 panic。源码注释也专门说明了这一点。

#### 4.2.4 代码实践：对比 Uri 与 OriginalUri

**实践目标**：直观看到 `StripPrefix` 对 handler 可见 URI 的影响，并学会用 `OriginalUri` 取回原始 URI。

**操作步骤**：

1. 在 4.1.5 的程序基础上，把 `user_by_id` 改成同时打印两种 URI（**示例代码**）：

```rust
use axum::extract::OriginalUri;
use http::Uri;

async fn user_by_id(OriginalUri(original): OriginalUri, uri: Uri) -> String {
    format!("stripped={uri} | original={original}")
}
```

2. 注册成 `let user_routes = Router::new().route("/{id}", get(user_by_id));`，其余不变。
3. `cargo run`，请求 `curl http://127.0.0.1:3000/api/users/42`。

**需要观察的现象**：响应里 `stripped` 与 `original` 两部分不同。

**预期结果**：输出形如 `stripped=/42 | original=/api/users/42`。这证明 `Uri` 提取器拿到的是 `StripPrefix` 改写后的 `/42`，而 `OriginalUri` 保留了改写前 axum 在最外层存进 extensions 的完整 URI。

> ⚠️ 待本地验证：实际输出文本依赖本地运行结果。

#### 4.2.5 小练习与答案

**练习 1**：`strip_prefix` 函数对前缀 `/api` 和路径 `/api`（完全相等、无剩余段）会返回什么？

**参考答案**：返回 `Some("/")`。参考 [strip_prefix.rs 测试 `single_segment`（第 246-251 行）](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/strip_prefix.rs#L246-L251)：`uri="/a", prefix="/a"` 期望 `Some("/")`。完全匹配时剩余路径为空，会被补成 `/`。

**练习 2**：为什么 `StripPrefix` 的 `Service` 实现里，剥不掉前缀时选择「不改 URI」而不是返回 404？

**参考答案**：因为 `StripPrefix` 是 nest 内部用的层，到达它的请求已经经过了外层 matchit 匹配（外层节点就是按完整路径注册的），理论上前缀一定匹配。`strip_prefix` 返回 `None` 只是防御性的回退（例如前缀含异常捕获段），不改 URI 让请求继续流转比直接 404 更安全。源码在 `capture_prefix_suffix` 异常时也特意避免 production 里 panic。

### 4.3 Router::merge：扁平合并与 fallback 冲突

#### 4.3.1 概念说明

`merge` 和 `nest` 都是「把两个 `Router` 合成一个」，但语义完全不同：

- `nest(prefix, child)`：给子路由**加前缀**，子路由保持「相对路径」的独立性。
- `merge(other)`：**不加前缀**，把另一个 `Router` 的路由「原样」搬进当前 `Router`，两条路由表扁平地合在一起。

`merge` 最常见的用途是配合 `layer`：你想给「一部分路由」加某种中间件、给「另一部分路由」加另一种中间件，就分别建两个子 `Router`、各自 `.layer(...)`，最后 `merge` 到一起。官方 [layer.md 第 31-50 行](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/docs/routing/layer.md#L31-L50) 给的就是这个模式：一个子 Router 加 `TraceLayer`，另一个加 `CompressionLayer`，最后 merge。

#### 4.3.2 核心流程

`merge` 的实现分两层：`Router::merge` 处理 `RouterInner` 级别的合并（含 fallback 冲突检测），`PathRouter::merge` 处理路由表本身的合并。

```
Router::merge(other):
  把 other 拆成 RouterInner (path_router, default_fallback, catch_all_fallback)
  fallback 冲突检测（三选一）:
    - other 用默认 fallback     → 保留 this 的 fallback
    - this 用默认、other 自定义   → 采用 other 的（关掉 default 标记）
    - 双方都自定义               → panic！
  path_router.merge(other.path_router)   // 真正搬路由表
  catch_all_fallback.merge(...)          // 同样有冲突则 panic
```

`PathRouter::merge` 做的是「遍历 other 的每条路由，查出它的路径，用 `route`/`route_service` 重新注册进 self」：

```
PathRouter::merge(other):
  v7_checks |= other.v7_checks   // 任一方禁用旧语法则合并后也禁用
  for (id, route) in other.routes:
      path = other.node.route_id_to_path[id]
      match route:
        MethodRouter(mr) → self.route(path, mr)     // 可能触发路径冲突错误
        Route(r)         → self.route_service(path, r)
```

关键点：**合并是按「原始路径」搬运的**。如果两个 Router 有相同路径，会在 `self.route` 里触发 matchit 的节点冲突，返回错误（被 `panic_on_err!` 转成 panic）。

#### 4.3.3 源码精读

先看 `Router::merge` 的入口，重点在三段 fallback 冲突检测：

> `merge` 入口：拆出 `other` 的三块，用 `match (this.default_fallback, default_fallback)` 判定 fallback 归属——other 是默认则保留 this，this 是默认则采用 other，双方都自定义则 panic；随后合并 `path_router` 与 `catch_all_fallback`。[axum/src/routing/mod.rs:258-293](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L258-L293)

`catch_all_fallback` 的合并规则在 `Fallback::merge`，逻辑很简洁：「只要有一方是 `Default`，就返回另一方；否则返回 `None`（即冲突）」：

> `Fallback::merge`：任一方为 `Default` 时采用另一方；双方都是自定义时返回 `None`，外层据此 panic。[axum/src/routing/mod.rs:720-727](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L720-L727)

> 这意味着「两个都设了 `fallback` 的 Router 不能 merge」是双重保险：`default_fallback` 标记位检查一次，`catch_all_fallback.merge` 返回 `None` 再检查一次。

真正搬路由表的 `PathRouter::merge`：

> 遍历 `other.routes`，用 `route_id_to_path` 反查每条路由的原始路径，按 `Endpoint` 变体分别调用 `self.route` 或 `self.route_service` 重新注册；同时合并 `v7_checks`。[axum/src/routing/path_router.rs:146-170](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/path_router.rs#L146-L170)

当你确实需要合并两个都带 `fallback` 的 Router 时，`reset_fallback` 派上用场——它把一方的 fallback 重置为默认，绕开冲突：

> `reset_fallback` 把 `default_fallback` 标记位复原为 `true`、`catch_all_fallback` 重置为默认的 `NotFound`，专门为 merge 前丢弃一方的 fallback 而设。[axum/src/routing/mod.rs:377-389](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L377-L389)

#### 4.3.4 代码实践：体会 merge 的 fallback 冲突

**实践目标**：复现「两个带 fallback 的 Router 不能 merge」的 panic，并用 `reset_fallback` 解决。

**操作步骤**：

1. 写一段**会 panic 的示例代码**（故意触发冲突）：

```rust
use axum::{routing::get, Router};

async fn fb_a() -> &'static str { "A" }
async fn fb_b() -> &'static str { "B" }

let a = Router::new().route("/a", get(|| async "a")).fallback(fb_a);
let b = Router::new().route("/b", get(|| async "b")).fallback(fb_b);

// 下一步会 panic: "Cannot merge two `Router`s that both have a fallback"
let app = a.merge(b);
```

2. 运行 `cargo run`，确认看到 panic 信息。
3. 在合并前对 `b` 调用 `reset_fallback`，丢弃它的 fallback：

```rust
let app = a.merge(b.reset_fallback());  // 现在能编译并运行
```

**需要观察的现象**：第 1 步启动即 panic；第 3 步正常运行。

**预期结果**：第 3 步中，访问 `/a`、`/b` 正常返回；访问不存在的路径（如 `/c`）返回 A 的 fallback（因为 `b` 的 fallback 被重置，合并后保留 `a` 的）。这印证了 `merge` 的 fallback 归属规则——「采用自定义的那一方」。

> ⚠️ 待本地验证：panic 文本与 fallback 返回值依赖本地运行。

#### 4.3.5 小练习与答案

**练习 1**：`Router::new().merge(Router::new())`（合并两个空 Router）会 panic 吗？

**参考答案**：不会。两个空 Router 都没有自定义 fallback（`default_fallback = true`、`catch_all_fallback = Default`），命中 `(_, true) => {}` 分支保留 this；`path_router.merge` 遍历空列表也不做事。结果是一个仍为空的 Router。

**练习 2**：`merge` 时如果两个 Router 都注册了 `/health` 路由（一个 GET、一个 POST），会成功吗？

**参考答案**：会成功。`PathRouter::merge` 对 `/health` 的 `MethodRouter`（来自 b）调用 `self.route("/health", mr)`，而 `route` 对同一路径会**按方法合并**到已有的 `MethodRouter`（见 u2-l1、u2-l3），不冲突。最终 `/health` 同时支持 GET 和 POST。只有「路径模板在 matchit 树里冲突」（如 `/health` 与 `/health` 字面相同但已被当成不同模板）才会报错；同字面路径的方法合并是允许的。

### 4.4 layer 与 route_layer：中间件生效范围

> 说明：`layer` 与 `route_layer` 本身属于第 5 单元（中间件）的主题，但因为本讲的实践任务要用 `route_layer` 给子路由加日志、并与 `layer` 对比生效范围，这里先讲清两者的区别。深入的 `tower::Service/Layer` 原理留到 u5-l1。

#### 4.4.1 概念说明

`layer` 和 `route_layer` 都是把一个 `tower::Layer` 应用到 Router 上，让中间件包裹住路由产生的 `Service`。它们的共同点是：**只影响「已经注册」的路由**——之后再用 `route` 新加的路由不会被包裹。

两者的关键区别在「**生效范围**」：

- `Router::layer(layer)`：包裹**所有已注册路由**，**外加 fallback**。即使请求最终走到了 fallback（404），中间件也会先跑一遍。
- `Router::route_layer(layer)`：**只包裹已注册的路由**，**不碰 fallback**。请求若没命中任何路由、走向 fallback，中间件不会执行。此外，在没有路由时调用 `route_layer` 会 **panic**（因为这是无意义的 no-op，通常是 bug）。

为什么需要这两种？官方 [route_layer.md 第 9-12 行](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/docs/routing/route_layer.md#L9-L12) 给了最典型的例子：**鉴权中间件应该用 `route_layer`**。设想一个校验 token 的中间件，失败时返回 401。如果用 `layer`，那么访问一个根本不存在的路径 `/not-found` 时，中间件也会先跑、把本该是 404 的响应变成 401——这会泄露「该路径是否存在」的信息。用 `route_layer` 则保证：只有命中真实路由的请求才会被鉴权，不存在的路径照常返回 404。

#### 4.4.2 核心流程

两者都委托给 `PathRouter`，但传递的字段不同：

```
Router::layer(layer):
  path_router.layer(layer.clone())            // 包裹所有路由
  catch_all_fallback.map(|r| r.layer(layer))  // 也包裹 fallback
  // default_fallback 标记位保持不变

Router::route_layer(layer):
  path_router.route_layer(layer)              // 仅包裹路由（无路由则 panic）
  // catch_all_fallback 原样不动！
```

`PathRouter::layer` 和 `route_layer` 的路由表处理几乎一样——都是遍历 `self.routes`，对每个 `Endpoint` 调用 `endpoint.layer(layer.clone())`。差异在两点：

1. `route_layer` 多了一段「routes 为空就 panic」的检查。
2. `route_layer` 在 `Router` 这一层不触碰 `catch_all_fallback`。

`Endpoint::layer` 则按变体分发：`MethodRouter` 变体交给 `MethodRouter::layer`，`Route` 变体交给 `Route::layer`。

#### 4.4.3 源码精读

先看 `Router` 这一层两者的对照——`layer` 会 `.map(|route| route.layer(layer))` 处理 fallback，`route_layer` 不会：

> `Router::layer`：`path_router.layer(layer.clone())` 外加 `catch_all_fallback.map(|route| route.layer(layer))`，fallback 也被包裹。[axum/src/routing/mod.rs:296-309](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L296-L309)

> `Router::route_layer`：只调用 `path_router.route_layer(layer)`，`catch_all_fallback` 原样保留。[axum/src/routing/mod.rs:313-326](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L313-L326)

再看 `PathRouter` 这一层，`route_layer` 多了空检查：

> `PathRouter::route_layer`：若 `self.routes.is_empty()` 直接 panic（「Adding a route_layer before any routes is a no-op」），否则遍历所有端点套 layer。[axum/src/routing/path_router.rs:273-299](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/path_router.rs#L273-L299)

> `PathRouter::layer`：与 `route_layer` 的遍历逻辑相同，但没有空检查（因为 `layer` 即使包裹空路由表也无害——它还会包裹 fallback）。[axum/src/routing/path_router.rs:251-270](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/path_router.rs#L251-L270)

`Endpoint::layer` 的分发，解释了为什么「方法路由」和「service 路由」都能被同一层包裹：

> `Endpoint::layer` 按变体分发：`MethodRouter` 走 `MethodRouter::layer`，`Route` 走 `Route::layer`。[axum/src/routing/mod.rs:796-808](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L796-L808)

最后，官方 [layer.md 第 57-62 行](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/docs/routing/layer.md#L57-L62) 还提醒一个运行时特性：**用 `layer`/`route_layer` 加的中间件运行在路由匹配之后**，因此不能用它来改写请求 URI 影响路由决策（改写 URI 的需求要用专门的 `axum::middleware` 方案，见第 5 单元）。

#### 4.4.4 代码实践：对比 layer 与 route_layer 的生效范围

**实践目标**：用一个「打印日志」的中间件，分别用 `layer` 和 `route_layer` 应用，观察对 404 请求的差别。

**操作步骤**：

1. 用 `axum::middleware::from_fn` 写一个最简日志中间件（**示例代码**，`from_fn` 的深入原理见 u5-l2）：

```rust
use axum::{middleware::Next, extract::Request};

async fn log_middleware(req: Request, next: Next) -> String {
    println!("[log] handling {}", req.uri());
    next.run(req).await.into_response() // 简化：这里只关心是否被调用
}
```

> 注：上面的签名仅为示意；`from_fn` 中间件必须返回 `impl IntoResponse`。最稳妥的写法见 u5-l2。这里若编译不通过，可改用现成的 `tower_http::trace::TraceLayer` 替代，重点不在中间件本身，而在它「是否被调用」。

2. 准备两个子 Router，各注册一个路由；对其中一个用 `route_layer`，对父 Router 用 `layer`：

```rust
use axum::{routing::get, Router, middleware};

async fn handler() -> &'static str { "ok" }

let users = Router::new()
    .route("/list", get(handler))
    .route_layer(middleware::from_fn(log_middleware)); // 仅命中 /list 才跑

let app = Router::new()
    .nest("/api/users", users);
// 等价地，也可在父级用 layer 对比：
// let app = Router::new().nest("/api/users", users).layer(TraceLayer::new_for_http());
```

3. 分别请求命中与未命中的路径：

```bash
curl http://127.0.0.1:3000/api/users/list   # 命中路由
curl http://127.0.0.1:3000/api/users/nope   # 未命中，走 fallback
```

**需要观察的现象**：用 `route_layer` 时，只有第一条请求会打印 `[log] ...`；第二条（未命中）不会打印日志，直接返回 404。

**预期结果**：把 `route_layer` 换成 `layer` 后，两条请求都会打印日志（因为 `layer` 也包裹 fallback）。这就是「鉴权中间件要用 `route_layer`」的直观验证。

> ⚠️ 待本地验证：日志是否打印依赖本地运行；若 `from_fn` 签名报错，先用 `TraceLayer` 观察同样的「是否包裹 fallback」差异。

#### 4.4.5 小练习与答案

**练习 1**：`Router::new().route_layer(some_layer)`（先 layer 后 route）会怎样？

**参考答案**：启动时 panic。`PathRouter::route_layer` 检查到 `self.routes.is_empty()`，输出「Adding a route_layer before any routes is a no-op」。正确顺序是先 `route` 再 `route_layer`。注意 `Router::new().layer(some_layer)` 不会 panic，因为 `layer` 允许空路由表（它还会包裹 fallback）。

**练习 2**：你已经用 `route_layer` 给 `users` 子 Router 加了鉴权。现在想再给「整个应用」加一个全局的 `TraceLayer`，应该用 `layer` 还是 `route_layer`？写在哪一层？

**参考答案**：用 `layer`，写在最外层父 Router 上：`let app = Router::new().nest("/api/users", users).layer(TraceLayer::new_for_http());`。`TraceLayer` 的目的是追踪所有请求（包括 404），所以需要覆盖 fallback，用 `layer` 而非 `route_layer`；写在外层可以同时覆盖 `users` 子树和其它后续挂载的路由。顺序上，外层 `layer` 包裹的是「已经 nest 好的整棵树」，因此对子路由也生效。

## 5. 综合实践

把本讲四个最小模块串起来，完成一个模块化的小型 API 骨架。**综合实践任务**（即本讲规格里的 practice_task）：

> 把「用户相关路由」和「订单相关路由」分别封装成两个子 `Router`，用 `nest` 挂到 `/api/users` 与 `/api/orders`；再对其中一个子路由用 `route_layer` 加上日志中间件；最后对外层用 `layer` 加一个全局中间件，验证 `layer` 与 `route_layer` 的生效范围差异。

建议步骤：

1. **建两个子 Router**（**示例代码**，可基于 4.1.5 扩展）：

```rust
use axum::{routing::{get, post}, Router, extract::Path, Json, middleware};

async fn list_users() -> &'static str { "[]" }
async fn get_user(Path(id): Path<u32>) -> String { format!("user {id}") }
async fn create_user() -> &'static str { "created" }

async fn list_orders() -> &'static str { "[]" }
async fn get_order(Path(id): Path<u32>) -> String { format!("order {id}") }

// 用户子路由：额外加一个 route_layer 日志（仅命中这些路由时才打印）
let users = Router::new()
    .route("/", get(list_users).post(create_user))
    .route("/{id}", get(get_user))
    .route_layer(middleware::from_fn(log_middleware));

// 订单子路由：不加 route_layer
let orders = Router::new()
    .route("/", get(list_orders))
    .route("/{id}", get(get_order));

let app = Router::new()
    .nest("/api/users", users)
    .nest("/api/orders", orders)
    .layer(/* 全局中间件，例如 TraceLayer，覆盖包括 404 在内的所有请求 */);
```

2. `cargo run` 后做四组验证（**预期结果**）：

| 请求 | 命中情况 | 子级 `route_layer` 日志 | 外层 `layer` |
| --- | --- | --- | --- |
| `GET /api/users/` | 命中 users 路由 | ✅ 打印 | ✅ 运行 |
| `GET /api/users/99` | 命中 users 路由 | ✅ 打印 | ✅ 运行 |
| `GET /api/orders/3` | 命中 orders 路由 | ❌ 不打印（orders 没加） | ✅ 运行 |
| `GET /api/users/missing` | 未命中，走 fallback | ❌ 不打印（route_layer 不覆盖 fallback） | ✅ 运行（layer 覆盖 fallback） |

3. **思考题（自验）**：把 `users` 上的 `route_layer` 换成 `layer`，第 4 行（`/api/users/missing`）的日志会不会出现？为什么？（答案：会，因为 `layer` 包裹了 users 子树的 fallback。）

> ⚠️ 待本地验证：表格中的日志/中间件行为依赖本地运行结果。若 `from_fn` 中间件签名有疑问，先用 `tower_http::trace::TraceLayer` 等现成中间件替代，验证「是否覆盖 fallback」这一核心差异即可。

## 6. 本讲小结

- **`nest(prefix, child)`** 在构建期把子路由的相对路径全部拼上前缀、摊平注册进父路由的 matchit 树；它只取子路由的 `path_router`，丢弃其 `catch_all_fallback` 与 `default_fallback`，这是嵌套未命中时「冒泡到外层 fallback」的根源。
- **`nest_service`** 用于挂任意 `tower::Service`，靠 `{*NEST_TAIL_PARAM}` 通配兜住前缀下的所有子路径，并额外注册前缀本身与带尾斜杠的形式，让 `/foo`、`/foo/` 都能命中。
- **`StripPrefix`** 是 nest 给每条子路由套的层，在请求进入子路由前剥掉已匹配的前缀、改写 URI；因此子路由 handler 看到的是相对 URI，需要原始 URI 时用 `OriginalUri`。
- **`merge`** 不加前缀、把另一个 Router 的路由表按原路径搬进来；两个都带自定义 `fallback` 的 Router 不能 merge，用 `reset_fallback` 丢弃一方即可。
- **`layer` 包裹所有路由外加 fallback**，**`route_layer` 只包裹已注册路由、不碰 fallback** 且在无路由时 panic；鉴权类中间件应选 `route_layer`，全局追踪类应选 `layer` 并写在外层。
- 两者都只影响「已注册」的路由，且中间件运行在路由匹配**之后**，无法用它改写 URI 影响路由决策。

## 7. 下一步学习建议

至此，第 2 单元（路由系统）的五个最小主题已全部讲完：路由注册（u2-l1）、路径匹配与参数（u2-l2）、方法路由（u2-l3）、嵌套与合并（本讲）、fallback 与 404（u2-l5，下一篇）。建议：

- **紧接着读 u2-l5（Fallback 与 404 处理）**：本讲多次提到「嵌套未命中会冒泡到外层 fallback」「merge 的 fallback 冲突」，下一篇会从 `catch_all_fallback`、`NotFound`、`method_not_allowed_fallback` 的角度把这些兜底机制讲透，与本讲紧密咬合。
- **回顾性阅读**：回看 u2-l1 里 `RouterInner` 的三块结构（`path_router` / `catch_all_fallback` / `default_fallback`），结合本讲的 `nest`/`merge` 源码，你会对这三个字段如何被各 API 增删改有完整画面。
- **为第 5 单元做铺垫**：本讲把 `Layer` 当黑盒用了（`StripPrefix`、`route_layer`）。第 5 单元（u5-l1）会从 `tower::Service`/`Layer` 的基本概念讲起，解释「中间件如何包裹 Service 形成管线」，届时再回头读 `StripPrefix` 的 `Service` 实现与 `Endpoint::layer` 的分发，会有更深的理解。
