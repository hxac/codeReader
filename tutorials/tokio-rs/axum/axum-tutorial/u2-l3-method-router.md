# 方法路由 MethodRouter

## 1. 本讲目标

上一篇讲义（u2-l1）我们建立了 `Router` 的心智模型：`Router::route(path, method_router)` 把一条「路径 → 方法路由」的映射注册进 `path_router`。当时我们把 `method_router`（比如 `get(handler)`）当成一个黑盒。本讲打开这个黑盒，学完后你应当能够：

- 说清 `MethodRouter` 内部为 9 个 HTTP 方法各保留了一个「槽位」，以及每个槽位装的是什么。
- 区分 `get` / `post` / `on` / `any` 四类入口的含义，并掌握 `.get().post()` 链式组合的用法。
- 解释为什么 `get` 注册的路由会自动响应 `HEAD` 请求，以及在源码的哪一行发生这一回退。
- 理解当请求方法未被注册时，axum 如何产生 `405 Method Not Allowed` 响应，并附带符合 RFC 9110 的 `Allow` 头。

本讲覆盖三个最小模块：**MethodRouter**（含其匹配语言 `MethodFilter`）、**MethodEndpoint**、**AllowHeader**。

## 2. 前置知识

阅读本讲前，你需要先具备上一篇讲义（u2-l1）建立的认知：

- `Router<S>` 是路由容器，`Router::route("/items", get(handler))` 中的第二个参数不是 handler 本身，而是一个 `MethodRouter`。
- handler 是返回 `IntoResponse` 的 `async fn`，由 `Handler` 的 blanket impl 被适配成 `tower::Service`。
- axum 复用 `tower::Service` 体系，错误类型 `E` 通常是 `Infallible`（永不失败）。

此外需要一点位运算常识：本讲的 `MethodFilter` 把每个 HTTP 方法编码成一个 `u16` 的某一位（bit），用按位与 `&` 判断「是否包含」，用按位或 `|` 合并多个方法。这与 u2-l2 里 matchit 的「基数树路径匹配」是两套独立机制——一个匹配 URL 路径，一个匹配 HTTP 方法。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `axum/src/routing/method_routing.rs` | `MethodRouter` 的定义、`get/post/on/any` 等顶层函数、链式方法、`on_endpoint` 分发逻辑、`call_with_state` 运行时匹配，以及 `MethodEndpoint`、`AllowHeader` 两个内部类型。 |
| `axum/src/routing/method_filter.rs` | `MethodFilter` 位域类型：为 9 个标准方法各定义一个常量位，提供 `contains` / `or` / `TryFrom<Method>`。 |
| `axum/src/routing/route.rs` | `RouteFuture` 在响应阶段补写 `Allow` 头、剥离 HEAD 响应体、处理 CONNECT 等特殊逻辑。 |
| `axum/src/routing/mod.rs` | `Fallback` 枚举（`Default`/`Service`/`BoxedHandler`），即 MethodRouter 未命中任何方法时的兜底处理器。 |

## 4. 核心概念与源码讲解

### 4.1 MethodRouter：九个方法槽位与匹配语言

#### 4.1.1 概念说明

回想上一篇：`Router::route("/items", ...)` 的第二个参数是一个「方法路由」。它的职责是——**当路径已经匹配上 `/items` 之后，再根据 HTTP 方法（GET/POST/PUT/…）决定把请求交给哪个 handler**。

最直觉的设计是一个 `HashMap<Method, Handler>`，但 axum 没有用哈希表，而是为 9 个标准方法各预留一个**固定字段**。这样做有两个好处：

1. **零分配、可预测**：每个方法槽位就是一个字段，分发时是一串固定顺序的比较，无需哈希与查找。
2. **HEAD 语义**：HTTP 规定 `HEAD` 请求的响应必须与 `GET` 一致但无响应体。axum 把「GET 自动响应 HEAD」做进了分发逻辑本身，需要 GET 与 HEAD 是两个可分别访问的槽位。

`MethodRouter` 还需要一个「兜底」机制：当请求方法没有任何注册的 handler 时（例如只注册了 GET/POST，却来了一个 DELETE），要返回 `405`。这个兜底由 `fallback` 字段承担，而 `Allow` 头内容由 `allow_header` 字段在注册时增量记录。

#### 4.1.2 核心流程

一个 `MethodRouter` 的生命周期分两个阶段：

**注册阶段（构建期，链式调用）**

```
get(handler)            // 调用顶层函数 get，产生一个 MethodRouter
  .post(handler2)       // 链式：再装填 POST 槽位
  .on(MethodFilter::DELETE, handler3)  // 用任意 MethodFilter 装填 DELETE
```

每次链式调用都会走 `on_endpoint`，它做两件事：

1. 用「用户的 filter 是否包含某方法」逐个检查 9 个槽位，命中则把 handler 写进对应槽位，并把方法名追加到 `allow_header`。
2. 若某槽位已被占用又重复装填，直接 `panic`（编译期/启动期错误，而非运行时）。

**分发阶段（运行期，每条请求）**

```
call_with_state(req, state):
  按 HEAD → GET → POST → … → CONNECT 的固定顺序，
  逐个用 call! 宏检查「请求方法是否等于该槽位的方法」：
    - 命中且槽位非空  → 调用该端点的 Service
    - 命中但槽位为空  → 继续下一个（None 分支什么也不做）
  全部未命中          → 调用 fallback（默认返回 405）
                       并把 allow_header 附加到 RouteFuture
```

注意分发顺序里 **HEAD 被检查了两次**：先查 `head` 槽位，再查 `get` 槽位。这就是 HEAD 回退到 GET 的关键（见 4.1.3）。

匹配的「语言」是 `MethodFilter`——一个 `u16` 位域。用户极少直接写位运算，而是通过 `get`/`post` 等便捷函数，它们内部都转成 `on(MethodFilter::GET, handler)` 这样的统一形式。

#### 4.1.3 源码精读

先看 `MethodRouter` 的结构体定义——9 个方法槽位 + `fallback` + `allow_header`：

```rust
// 为每个 HTTP 方法保存一个端点
#[must_use]
pub struct MethodRouter<S = (), E = Infallible> {
    get: MethodEndpoint<S, E>,
    head: MethodEndpoint<S, E>,
    delete: MethodEndpoint<S, E>,
    options: MethodEndpoint<S, E>,
    patch: MethodEndpoint<S, E>,
    post: MethodEndpoint<S, E>,
    put: MethodEndpoint<S, E>,
    trace: MethodEndpoint<S, E>,
    connect: MethodEndpoint<S, E>,
    fallback: Fallback<S, E>,
    allow_header: AllowHeader,
}
```

> 结构体定义，9 个方法各占一个字段，外加 `fallback` 兜底与 `allow_header`。[axum/src/routing/method_routing.rs:547-559](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L547-L559)

泛型 `S` 是状态类型（与 `Router<S>` 对应，详见 u3-l5），`E` 是错误类型，绝大多数情况下是 `Infallible`。

**匹配语言 `MethodFilter`**。每个标准方法对应 `u16` 的一位：

```rust
#[derive(Debug, Copy, Clone, PartialEq)]
pub struct MethodFilter(u16);

impl MethodFilter {
    pub const CONNECT: Self = Self::from_bits(0b0_0000_0001);
    pub const DELETE:  Self = Self::from_bits(0b0_0000_0010);
    pub const GET:     Self = Self::from_bits(0b0_0000_0100);
    pub const HEAD:    Self = Self::from_bits(0b0_0000_1000);
    // ... OPTIONS / PATCH / POST / PUT / TRACE
    pub(crate) const fn contains(self, other: Self) -> bool {
        self.bits() & other.bits() == other.bits()
    }
    pub const fn or(self, other: Self) -> Self {
        Self(self.0 | other.0)
    }
}
```

> 9 个方法各占一位；`contains` 用按位与判断「self 是否包含 other」，`or` 用按位或合并。[axum/src/routing/method_filter.rs:7-9](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_filter.rs#L7-L9)（常量定义 [L29-L45](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_filter.rs#L29-L45)；`contains` [L56-L58](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_filter.rs#L56-L58)；`or` [L61-L64](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_filter.rs#L61-L64)）

`MethodFilter` 还实现了 `TryFrom<Method>`，把运行时的 `http::Method` 转成位域（自定义方法如 `CUSTOM` 会转换失败，见 [method_filter.rs:88-105](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_filter.rs#L88-L105)）。

**顶层函数 `get`/`post`/`on`**。这些便捷函数都由宏批量生成，最终落到统一的 `on`：

```rust
// 宏为每个方法生成一个顶层函数，签名形如：
pub fn get<H, T, S>(handler: H) -> MethodRouter<S, Infallible>
where H: Handler<T, S>, ... {
    on(MethodFilter::GET, handler)
}

// on 接受任意 MethodFilter
pub fn on<H, T, S>(filter: MethodFilter, handler: H) -> MethodRouter<S, Infallible>
where H: Handler<T, S>, ... {
    MethodRouter::new().on(filter, handler)
}
```

> `get` 等顶层函数（[L439-L447](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L439-L447)）由 `top_level_handler_fn!` 宏生成（[L105-L174](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L105-L174)）；`on` 在 [L466-L473](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L466-L473)。

`any` 则不同——它不装填任何方法槽位，而是设置 `fallback` 并跳过 `Allow` 头：

```rust
pub fn any<H, T, S>(handler: H) -> MethodRouter<S, Infallible>
where H: Handler<T, S>, ... {
    MethodRouter::new().fallback(handler).skip_allow_header()
}
```

> `any` 把 handler 放进 fallback，并调用 `skip_allow_header`。[axum/src/routing/method_routing.rs:508-515](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L508-L515)

这解释了一个重要区别：`any(handler)` 意味着「**所有方法**都走这个 handler」，因此不存在「未匹配方法」，自然也不需要 `Allow` 头；而 `get(handler)` 只覆盖 GET（与 HEAD），其它方法会落到 fallback 产生 405。

**注册核心 `on_endpoint`**。这是理解 `get`/`post`/`on`/链式调用如何写进槽位的关键：

```rust
fn on_endpoint(mut self, filter: MethodFilter, endpoint: &MethodEndpoint<S, E>) -> Self {
    fn set_endpoint(/* ... */, endpoint_filter: MethodFilter, filter: MethodFilter,
                    allow_header: &mut AllowHeader, methods: &[&'static str]) {
        if endpoint_filter.contains(filter) {          // 用户的 filter 是否包含此方法？
            if out.is_some() {
                panic!("Overlapping method route. Cannot add two method routes that both handle `{method_name}`");
            }
            *out = endpoint.clone();
            for method in methods {
                append_allow_header(allow_header, method);  // 记录到 Allow 头
            }
        }
    }

    set_endpoint("GET", &mut self.get, endpoint, filter, MethodFilter::GET,
                 &mut self.allow_header, &["GET", "HEAD"]);   // 注意 GET 追加了 GET,HEAD
    set_endpoint("HEAD", &mut self.head, endpoint, filter, MethodFilter::HEAD,
                 &mut self.allow_header, &["HEAD"]);
    // ... 其余 7 个方法
}
```

> `on_endpoint` 逐方法检查并装填；`set_endpoint` 内部的 `contains` 判断与 overlap panic。[axum/src/routing/method_routing.rs:869-990](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L869-L990)（overlap panic 在 [L885-L891](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L885-L891)；GET 追加 `["GET","HEAD"]` 在 [L899-L907](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L899-L907)）

这里有一个精妙之处：注册 `get` 时，`methods` 参数是 `&["GET", "HEAD"]`——也就是说**只要注册了 GET，`Allow` 头里就同时声明 GET 和 HEAD**。这与分发阶段「HEAD 回退到 GET」是一体两面：既然 GET 路由会响应 HEAD，那么对客户端声明的「允许方法」自然要包含 HEAD。

**分发核心 `call_with_state`**。运行时匹配就发生在这里：

```rust
pub(crate) fn call_with_state(&self, req: Request, state: S) -> RouteFuture<E> {
    macro_rules! call {
        ($req:expr, $method_variant:ident, $svc:expr) => {
            if *req.method() == Method::$method_variant {
                match $svc {
                    MethodEndpoint::None => {}                              // 空槽位：继续下一个
                    MethodEndpoint::Route(route) => return route.clone().oneshot_inner_owned($req),
                    MethodEndpoint::BoxedHandler(handler) => {
                        let route = handler.clone().into_route(state);
                        return route.oneshot_inner_owned($req);
                    }
                }
            }
        };
    }
    // ... 解构出 get/head/post/.../fallback/allow_header

    call!(req, HEAD, head);   // ① HEAD 请求先查 head 槽位
    call!(req, HEAD, get);    // ② HEAD 请求再查 get 槽位 ← HEAD 回退到 GET 在这一行
    call!(req, GET, get);     // ③ GET 请求查 get 槽位
    call!(req, POST, post);
    call!(req, OPTIONS, options);
    call!(req, PATCH, patch);
    call!(req, PUT, put);
    call!(req, DELETE, delete);
    call!(req, TRACE, trace);
    call!(req, CONNECT, connect);

    let future = fallback.clone().call_with_state(req, state);   // 全部未命中 → fallback
    match allow_header {
        AllowHeader::None => future.allow_header(Bytes::new()),
        AllowHeader::Skip => future,
        AllowHeader::Bytes(allow_header) => future.allow_header(allow_header.clone().freeze()),
    }
}
```

> `call!` 宏做「请求方法 == 槽位方法」的固定顺序匹配；第 ② 行 `call!(req, HEAD, get)` 就是 HEAD 回退到 GET 的源码位置；全部未命中则走 `fallback` 并按 `allow_header` 附加 Allow 头。[axum/src/routing/method_routing.rs:1167-1222](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L1167-L1222)（HEAD→GET 回退在 [L1204-L1206](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L1204-L1206)；fallback+allow_header 在 [L1215-L1221](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L1215-L1221)）

理解了第 ② 行，就能解释一个常见现象：只注册 `get(handler)` 时，发 `HEAD` 请求会得到 `200 OK` 但响应体为空——它走的是 `get` 槽位的 handler，随后由 `RouteFuture`（见 4.3）把响应体剥离。如果同时注册了 `.head(handler_b)`，则第 ① 行先命中 `head` 槽位，handler_b 优先（这一点有测试 `head_takes_precedence_over_get` 守护，见 [L1438-L1444](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L1438-L1444)）。

#### 4.1.4 代码实践

**实践目标**：亲手观察 HEAD 回退到 GET 的现象，并在源码中定位回退行。

**操作步骤**：

1. 复制官方 `hello-world` 示例到本地（或在依赖 axum 的项目里新建一个 binary）。
2. 把 `main.rs` 改成下面这样（示例代码）：

```rust
// 示例代码：观察 HEAD 回退与 405
use axum::{routing::get, Router};

async fn items() -> &'static str {
    "items list"
}

#[tokio::main]
async fn main() {
    let app = Router::new().route("/items", get(items));
    let listener = tokio::net::TcpListener::bind("127.0.0.1:3000").await.unwrap();
    axum::serve(listener, app).await.unwrap();
}
```

3. `cargo run` 启动。
4. 在另一个终端分别用 curl 触发三种请求：

```bash
curl -i http://127.0.0.1:3000/items          # GET，应得 200 + "items list"
curl -i -X HEAD http://127.0.0.1:3000/items   # HEAD，应得 200，但无 body
curl -i -X DELETE http://127.0.0.1:3000/items # 未注册，应得 405 + Allow 头
```

**需要观察的现象**：

- GET 与 HEAD 都返回 `200`，但 HEAD 的响应体为空（`curl` 可能警告 `Warning: --no-progress-meter`，且看不到正文）。
- DELETE 返回 `405 Method Not Allowed`，且响应头里有一行 `allow: GET,HEAD`。

**预期结果**：HEAD 命中的是 `get` 槽位（对应源码第 ② 行 `call!(req, HEAD, get)`），由 `RouteFuture` 剥离 body；DELETE 全部未命中走 fallback 返回 405，`allow` 头为 `GET,HEAD`（因为注册 GET 时 `on_endpoint` 用了 `&["GET","HEAD"]`）。

> 若本地无法运行，标注「待本地验证」：上述行为有单元测试 `get_accepts_head`（[L1430-L1436](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L1430-L1436)）与 `method_not_allowed_by_default`（[L1401-L1407](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L1401-L1407)）守护。

#### 4.1.5 小练习与答案

**练习 1**：`get(handler).get(handler2)` 会发生什么？为什么这是 panic 而不是运行时错误？

**答案**：会 panic，信息为 `Overlapping method route. Cannot add two method routes that both handle GET`（对应测试 `handler_overlaps`，[L1607-L1613](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L1607-L1613)）。因为这是配置错误（程序员写错了路由），应当在启动时尽早暴露，而非变成运行时的 500。

**练习 2**：`any(handler)` 与 `get(handler).fallback(handler)` 行为上有何差异？

**答案**：两者都会让所有方法都走 `handler`。差异在 `Allow` 头：`any` 内部调用了 `skip_allow_header()`，永不产生 `Allow` 头（因为「全部方法都允许」时 `Allow` 无意义）；而 `get(handler).fallback(handler)` 仍会保留 GET/HEAD 的 `Allow` 信息。可对照测试 `allow_header_any`（[L1548-L1555](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L1548-L1555)）与 `allow_header_with_fallback`（[L1557-L1566](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L1557-L1566)）。

---

### 4.2 MethodEndpoint：端点的三种形态

#### 4.2.1 概念说明

`MethodRouter` 的 9 个槽位类型不是 handler 本身，而是 `MethodEndpoint<S, E>`。为什么要再包一层？因为同一个槽位可能装入两种来源不同的东西：

- **`Route`**：已经是一个完整的 `tower::Service`（由 `get_service(svc)` / `on_service` / `route_service` 注册，或 handler 经 `with_state` 物化后得到）。
- **`BoxedHandler`**：一个「还没拿到状态、尚未物化成 `Route`」的 handler。axum 把 handler 装进 `BoxedIntoRoute`（一种类型擦除的闭包），延迟到运行时拿到状态后才 `into_route(state)` 变成 `Route`。

第三种是 `None`——槽位为空。这层包装让 MethodRouter 能统一处理「有状态 handler」与「无状态 Service」两种注册方式，并把「handler → Route」的物化推迟到真正需要时（性能考量：见 u9-l3 的惰性转换）。

#### 4.2.2 核心流程

```text
注册 handler (get/post/...)      →  装入 BoxedHandler
注册 service (get_service/...)   →  装入 Route
空槽位                            →  None

运行时 call_with_state 命中某槽位：
  None          → 跳过，继续下一个槽位
  Route         → 直接 clone 后 oneshot
  BoxedHandler  → 先 into_route(state) 物化，再 oneshot

with_state 阶段（Router 装填状态时）：
  None          → 仍是 None
  Route         → 仍是 Route（无需状态）
  BoxedHandler  → 物化成 Route
```

#### 4.2.3 源码精读

`MethodEndpoint` 是个三变体枚举：

```rust
enum MethodEndpoint<S, E> {
    None,
    Route(Route<E>),
    BoxedHandler(BoxedIntoRoute<S, E>),
}
```

> 三种端点形态。[axum/src/routing/method_routing.rs:1272-1276](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L1272-L1276)

`with_state` 展示了「handler 物化」的时机——`BoxedHandler` 借状态变成 `Route`，`Route` 与 `None` 保持不变：

```rust
fn with_state<S2>(self, state: &S) -> MethodEndpoint<S2, E> {
    match self {
        Self::None => MethodEndpoint::None,
        Self::Route(route) => MethodEndpoint::Route(route),
        Self::BoxedHandler(handler) => MethodEndpoint::Route(handler.into_route(state.clone())),
    }
}
```

> `with_state` 把 BoxedHandler 物化为 Route。[axum/src/routing/method_routing.rs:1304-1310](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L1304-L1310)

而 `call_with_state` 里 `call!` 宏对 `BoxedHandler` 的处理（运行时物化）已在 4.1.3 引用：`let route = handler.clone().into_route(state); return route.oneshot_inner_owned($req);`。注意两条路径：注册 handler 走 `BoxedHandler`，注册 service 直接走 `Route`。

`Fallback` 枚举与之结构同构，只是它表示「兜底端点」而非「某方法的端点」：

```rust
enum Fallback<S, E = Infallible> {
    Default(Route<E>),                  // 默认 405 兜底
    Service(Route<E>),                  // 用户用 fallback_service 设置
    BoxedHandler(BoxedIntoRoute<S, E>), // 用户用 fallback(handler) 设置
}
```

> Fallback 三变体；`Default` 就是 `MethodRouter::new` 里那个返回 405 的 service_fn。[axum/src/routing/mod.rs:710-714](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L710-L714)（`call_with_state` 在 [L751-L759](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L751-L759)）

#### 4.2.4 代码实践

**实践目标**：用源码阅读理解「handler 走 BoxedHandler、service 走 Route」两条路径的区别。

**操作步骤**：

1. 阅读顶层函数 `get`（[L466-L473](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L466-L473) 的 `on`）与 `get_service`（由 `top_level_service_fn!` 生成，[L27-L103](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L27-L103) 的 `on_service`）。
2. 在 `on`（[L630-L640](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L630-L640)）里确认：handler 被包成 `MethodEndpoint::BoxedHandler(BoxedIntoRoute::from_handler(handler))`。
3. 在 `on_service`（[L860-L867](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L860-L867)）里确认：service 被包成 `MethodEndpoint::Route(Route::new(svc))`。

**需要观察的现象**：两条注册路径最终都进入同一个 `on_endpoint`，但装进槽位的变体不同。

**预期结果**：你能用自己的话讲清——handler 版需要等 `with_state` 物化，service 版注册时就已经是 `Route`。这是 u9-l3「惰性转 Route」性能优化的前提。

> 本实践为源码阅读型，无需运行命令。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `MethodEndpoint::with_state` 对 `Route` 变体原样返回，而 `BoxedHandler` 必须物化？

**答案**：`Route` 是已经成型的 `Service`，注册它时就不依赖状态（`get_service` 接受的是 `Service<Request>`，不要求 `S`）；而 `BoxedHandler` 封装的是 `Handler<T, S>`，handler 只有拿到具体状态 `S` 才能被 `Handler::with_state` 转成 `Service`。物化就是把「带状态参数的 handler」变成「无状态 Service」。

**练习 2**：`Fallback::Default` 与 `Fallback::Service` 都装的是 `Route`，它们的区别在哪？

**答案**：仅在于「是否用户主动设置」。`Default` 是 `MethodRouter::new` 内置的 405 service（见 [L799-L817](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L799-L817)），用户调用 `fallback_service(svc)` 后变为 `Service`。`merge` 时若两个 Fallback 都非 Default 会报错（[mod.rs:720-L727](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L720-L727)），Default 则会被对方的覆盖。

---

### 4.3 AllowHeader：405 响应与 Allow 头的生成

#### 4.3.1 概念说明

[RFC 9110](https://httpwg.org/specs/rfc9110.html#field.allow) 规定：服务器返回 `405 Method Not Allowed` 时**必须**附带 `Allow` 头，列出该资源支持的方法。axum 的 `MethodRouter` 在三个层面协作实现这一要求：

1. **构建期累积**：每次 `on_endpoint` 装填一个方法，就把方法名追加进 `allow_header`（`append_allow_header`）。
2. **分发期传递**：`call_with_state` 在走 fallback 时，把累积的 `allow_header` 交给 `RouteFuture`。
3. **响应期落盘**：`RouteFuture::poll` 发现响应状态是 405 且是顶层响应时，才把 `Allow` 头写进 `HeaderMap`。

`allow_header` 字段的类型 `AllowHeader` 有三个变体，分别对应三种语义：`None`（还没装填任何方法，Allow 为空字符串）、`Bytes`（累积的方法名，如 `GET,HEAD,POST`）、`Skip`（用 `any` 注册，永远不输出 Allow 头）。

#### 4.3.2 核心流程

```text
构建：on_endpoint 装填方法 → append_allow_header 累积到 AllowHeader::Bytes
      any() / any_service() → skip_allow_header → AllowHeader::Skip
      空路由                 → AllowHeader::None

分发：call_with_state 全部未命中 → fallback（返回 405）
      match allow_header:
        None  → future.allow_header(Bytes::new())   // 空字符串
        Skip  → future                              // 不设 Allow
        Bytes → future.allow_header(frozen bytes)   // 如 "GET,HEAD"

响应：RouteFuture::poll
      若 status == 405 且 top_level:
         set_allow_header(headers, allow_header)
         （仅当 headers 中尚无 Allow 时才写入，不覆盖 handler 自设的）
```

#### 4.3.3 源码精读

**默认 fallback 返回 405**。`MethodRouter::new` 把 fallback 设成一个永远返回 `METHOD_NOT_ALLOWED` 的 service：

```rust
pub fn new() -> Self {
    let fallback = Route::new(service_fn(|_: Request| async {
        Ok(StatusCode::METHOD_NOT_ALLOWED)
    }));
    Self {
        get: MethodEndpoint::None,
        // ... 其余 8 个槽位都是 None
        allow_header: AllowHeader::None,
        fallback: Fallback::Default(fallback),
    }
}
```

> 默认 fallback 返回 405，初始 `allow_header` 为 `None`。[axum/src/routing/method_routing.rs:799-817](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L799-L817)

**`AllowHeader` 三变体与 merge**：

```rust
enum AllowHeader {
    None,                // 尚无任何方法
    Skip,                // any/any_service，永不输出 Allow
    Bytes(BytesMut),     // 累积的方法名，如 "PUT,PATCH"
}

fn merge(self, other: Self) -> Self {
    match (self, other) {
        (Self::Skip, _) | (_, Self::Skip) => Self::Skip,         // 任一 Skip → Skip
        (Self::None, Self::None) => Self::None,
        (Self::None, Self::Bytes(p)) | (Self::Bytes(p), Self::None) => Self::Bytes(p),
        (Self::Bytes(mut a), Self::Bytes(b)) => {                 // 拼接两个列表
            a.extend_from_slice(b",");
            a.extend_from_slice(&b);
            Self::Bytes(a)
        }
    }
}
```

> 三变体；merge 在两个 MethodRouter 合并时拼接 Allow 列表。[axum/src/routing/method_routing.rs:561-583](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L561-L583)

`append_allow_header` 在装填每个方法时被调用，负责把方法名拼进 `Bytes`（含去重与 utf-8 校验）：

```rust
fn append_allow_header(allow_header: &mut AllowHeader, method: &'static str) {
    match allow_header {
        AllowHeader::None => *allow_header = AllowHeader::Bytes(BytesMut::from(method)),
        AllowHeader::Skip => {}
        AllowHeader::Bytes(allow_header) => {
            if let Ok(s) = std::str::from_utf8(allow_header) {
                if !s.contains(method) {                        // 去重
                    allow_header.extend_from_slice(b",");
                    allow_header.extend_from_slice(method.as_bytes());
                }
            } else { /* debug 模式 panic */ }
        }
    }
}
```

> 增量追加方法名，含 `contains` 去重。[axum/src/routing/method_routing.rs:1225-1243](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L1225-L1243)

**`RouteFuture` 落盘 Allow 头**。`MethodRouter` 只是把字节交给 `RouteFuture`，真正的写入发生在响应 Future 的 `poll` 里：

```rust
impl<E> Future for RouteFuture<E> {
    fn poll(self: Pin<&mut Self>, cx: &mut Context<'_>) -> Poll<Self::Output> {
        let this = self.project();
        let mut res = ready!(this.inner.poll(cx))?;

        // ... CONNECT 处理略
        } else if *this.top_level {
            if res.status() == http::StatusCode::METHOD_NOT_ALLOWED {
                // RFC 9110: 405 响应必须生成 Allow 头
                set_allow_header(res.headers_mut(), this.allow_header);
            }
            set_content_length(&res.size_hint(), res.headers_mut());
            if *this.method == Method::HEAD {
                *res.body_mut() = Body::empty();                // HEAD 剥离响应体
            }
        }
        Poll::Ready(Ok(res))
    }
}

fn set_allow_header(headers: &mut HeaderMap, allow_header: &mut Option<Bytes>) {
    match allow_header.take() {
        // 仅当响应里尚无 Allow 头时才写入——尊重 handler 自设的 Allow
        Some(allow_header) if !headers.contains_key(header::ALLOW) => {
            headers.insert(header::ALLOW, HeaderValue::from_maybe_shared(allow_header).expect(...));
        }
        _ => {}
    }
}
```

> 405 时补写 Allow 头、HEAD 剥离 body、CONNECT 去掉 Content-Length，都在 `RouteFuture::poll`。[axum/src/routing/route.rs:143-180](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/route.rs#L143-L180)（`set_allow_header` 在 [L182-L192](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/route.rs#L182-L192)）

注意 `set_allow_header` 的 `!headers.contains_key(header::ALLOW)` 守卫：如果 handler/fallback 自己已经设了 `Allow` 头（例如自定义一个返回 `GET,POST` 的 fallback），axum 不会覆盖它——对应测试 `allow_header_with_fallback_that_sets_allow`（[L1568-L1594](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L1568-L1594)）。

**`top_level` 标志**。`RouteFuture` 有个 `top_level` 字段（[route.rs:114-L116](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/route.rs#L114-L116)）。只有「顶层」路由（直接挂在 `Router` 上的那个 MethodRouter）才补 Allow 头/Content-Length/剥离 HEAD body；嵌套或 `oneshot_inner`（非顶层）走 `not_top_level()`，避免重复处理。这解释了为什么 `Allow` 头只在最外层生成一次。

#### 4.3.4 代码实践

**实践目标**：观察 405 与 `Allow` 头如何随注册的方法变化，以及 `any` 为何不产生 `Allow` 头。

**操作步骤**：在 4.1.4 的程序基础上做三组对照实验（示例代码）。

实验 A：注册 GET + POST

```rust
let app = Router::new().route(
    "/items",
    get(|| async { "list" }).post(|| async { "created" }),
);
```

实验 B：改用 `any`

```rust
let app = Router::new().route("/items", any(|| async { "all methods" }));
```

实验 C：用 `on` 注册 GET+DELETE 合并

```rust
let app = Router::new().route(
    "/items",
    get(|| async { "list" }).merge(delete(|| async { "deleted" })),
);
```

分别 `curl -i -X PUT http://127.0.0.1:3000/items`（用未注册的方法触发 405）。

**需要观察的现象**：

- 实验 A：`Allow: GET,HEAD,POST`（GET 带了 HEAD）。
- 实验 B：PUT 也能命中（`200`），且**没有** `Allow` 头。
- 实验 C：`Allow: GET,HEAD,DELETE`（merge 把两个 MethodRouter 的 allow_header 拼接）。

**预期结果**：与对应单元测试一致——`sets_allow_header` 期望 `PUT,PATCH`（[L1513-L1519](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L1513-L1519)）、`allow_header_when_merging` 期望 `PUT,PATCH,GET,HEAD`（[L1538-L1546](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L1538-L1546)）、`allow_header_any` 期望无 Allow 头（[L1548-L1555](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L1548-L1555)）。

> 若本地无法运行，标注「待本地验证」，可改为阅读上述三个测试断言理解行为。

#### 4.3.5 小练习与答案

**练习 1**：为什么空 `MethodRouter::new()` 对任何方法都返回 405，但 `Allow` 头是空字符串而不是不发送？

**答案**：因为 `new` 的 `allow_header` 是 `AllowHeader::None`，分发时 `call_with_state` 走 `AllowHeader::None => future.allow_header(Bytes::new())` 分支，传入空字节。`RouteFuture` 在 405 时仍会 `set_allow_header` 写入一个空的 `Allow:` 头。对应测试 `empty_allow_header_by_default`（[L1529-L1535](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L1529-L1535)）断言 `headers[ALLOW] == ""`。这与 RFC「405 必须有 Allow 头」一致，即使内容为空。

**练习 2**：如果 handler 自己在响应里设了 `Allow: GET,POST`，axum 覆盖它吗？

**答案**：不覆盖。`set_allow_header` 有守卫 `!headers.contains_key(header::ALLOW)`，handler 自设的 Allow 优先。这是为了让用户能用自定义 fallback 实现更复杂的「允许方法」逻辑（见 `allow_header_with_fallback_that_sets_allow` 测试，[L1568-L1594](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L1568-L1594)）。

## 5. 综合实践

把本讲三个最小模块串起来，完成一个「方法齐全」的资源端点（示例代码）：

```rust
// 示例代码：综合实践——一个 /items 资源端点
use axum::{
    http::StatusCode,
    response::IntoResponse,
    routing::{get, post},
    Json, Router,
};
use serde_json::json;

async fn list() -> impl IntoResponse { Json(json!([{ "id": 1 }])) }
async fn create() -> impl IntoResponse { (StatusCode::CREATED, Json(json!({ "id": 2 }))) }
async fn update() -> impl IntoResponse { (StatusCode::OK, "updated") }

#[tokio::main]
async fn main() {
    // 用链式调用 + merge 组织方法；观察 Allow 头
    let app = Router::new().route(
        "/items",
        get(list).post(create).merge(
            axum::routing::patch(update), // merge 进 PATCH
        ),
    );
    let listener = tokio::net::TcpListener::bind("127.0.0.1:3000").await.unwrap();
    axum::serve(listener, app).await.unwrap();
}
```

任务清单：

1. 启动后用 `curl -i -X PUT http://127.0.0.1:3000/items` 触发 405，确认 `Allow` 头为 `GET,HEAD,POST,PATCH`。
2. 用 `curl -i -X HEAD http://127.0.0.1:3000/items` 确认命中 `get` 槽位且响应体为空（验证 4.1 的 HEAD 回退）。
3. 在源码 [method_routing.rs:1204-L1206](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L1204-L1206) 定位 HEAD→GET 回退行，在 [L1131](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L1131) 定位 merge 拼接 `allow_header` 的语句，在 [route.rs:163-L168](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/route.rs#L163-L168) 定位 405 补写 Allow 头。
4. （进阶）把 `.merge(patch(update))` 换成 `.patch(update)`，对比 `Allow` 头是否变化，并解释链式 `.patch` 与 `merge` 在生成 Allow 头上的等价性。

> 若本地无法运行，至少完成第 3 步的源码定位，并阅读 `allow_header_when_merging` 测试（[L1538-L1546](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L1538-L1546)）验证合并后的 Allow 头拼接顺序。

## 6. 本讲小结

- `MethodRouter` 为 9 个标准 HTTP 方法各保留一个 `MethodEndpoint` 槽位，外加 `fallback`（默认返回 405）与 `allow_header`（累积 Allow 头内容）。
- `get`/`post` 等便捷函数最终都归一到 `on(MethodFilter, handler)`；`MethodFilter` 是 `u16` 位域，用 `contains`/`or` 完成方法匹配。
- 分发是固定顺序的方法比较：HEAD 被检查两次（先 `head` 槽位、再 `get` 槽位），这就是 HEAD 自动回退到 GET 的源码根因，位于 `call_with_state` 的 `call!(req, HEAD, get)`。
- `MethodEndpoint` 有三种形态：`None`（空）、`Route`（已物化的 Service）、`BoxedHandler`（待物化的 handler），后者在 `with_state` 时才变成 `Route`。
- `Allow` 头由三个阶段协作生成：构建期 `append_allow_header` 累积、分发期 `call_with_state` 传递、响应期 `RouteFuture::poll` 在 405 时落盘；`any`/`any_service` 用 `AllowHeader::Skip` 永不输出 Allow 头。
- 重复注册同一方法会在启动时 panic（配置错误尽早暴露），而 handler 自设的 `Allow` 头不会被 axum 覆盖。

## 7. 下一步学习建议

- 下一篇 **u2-l4（nest 与 merge）** 会把 `MethodRouter::merge` 放大到 `Router` 层面，讲解 `nest`/`merge`/`layer`/`route_layer` 如何组合路由树——本讲的 `merge_for_path`（[L1084-L1134](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/method_routing.rs#L1084-L1134)）是它的微观预演。
- 若想深入「handler 如何变成 Service」与 `BoxedIntoRoute` 的惰性物化，可提前跳读 **u8（Handler 实现原理）** 与 **u9-l3（with_state 类型状态模式）**。
- 继续阅读源码建议：通读 `method_routing.rs` 顶部的四个宏（`top_level_handler_fn!` / `chained_handler_fn!` / `top_level_service_fn!` / `chained_service_fn!`），理解 axum 如何用宏消除 9 个方法的样板代码。
