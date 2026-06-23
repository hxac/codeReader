# 路径匹配与路径参数

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚一条请求的 URL 路径是如何被匹配到某条路由的，匹配发生在哪个数据结构里。
- 写出 axum 路径模板的三种写法：`{id}`（命名捕获）、`{*tail}`（通配捕获）、`{{literal}}`（字面量转义），并知道它们各自的匹配范围。
- 解释 `PathRouter` 为什么同时维护一个 `routes` 向量和一棵 `matchit` 路由树，以及 `RouteId` 如何把两者关联起来。
- 描述匹配命中的参数是如何被写入请求的 `Extensions`，再被 `Path<T>` 提取器用 serde 反序列化为任意类型的。
- 动手写出 `/users/{id}` 与 `/files/{*path}` 两类路由，并用 `Path` 取值。

本讲承接 [u2-l1](./u2-l1-router-and-routing.md)：上一讲我们知道了 `Router` 内部由 `path_router`（正路由表）和 `catch_all_fallback`（兜底）组成，本讲就钻进 `path_router` 这一层，看清「路径 → 路由」这一步到底怎么发生。

## 2. 前置知识

阅读本讲前，你最好已经了解：

- **Router 与 route 注册**：`Router::new().route("/users/{id}", get(handler))` 的基本用法（见 [u2-l1](./u2-l1-router-and-routing.md)）。
- **提取器（Extractor）的直觉**：handler 参数会被某种类型「填进去」，这类类型实现了 `FromRequestParts` 或 `FromRequest`（见 [u1-l4](./u1-l4-handler-router-intro.md)）。本讲的 `Path<T>` 就是一个只读 parts 的提取器。
- **serde 反序列化**：`#[derive(Deserialize)]` 能把一个「键值集合」变成 struct。本讲会看到 `Path` 是如何用一个自定义的 `Deserializer` 把路径参数喂给 serde 的。
- **请求的 Extensions**：axum 的 `Request` 里有一个类型擦除的 `Extensions` 容器（类似 `HashMap<TypeId, Box<dyn Any>>`），路由阶段会把匹配到的参数塞进这里，提取器阶段再取出来。

一个关键直觉：**路径匹配和参数提取是两个分离的阶段**。第一阶段只关心「这个 URL 该交给哪条路由」，顺便把捕获到的字符串记下来；第二阶段才把这些字符串反序列化成 handler 想要的类型。理解了这点，后面所有源码都会变得顺理成章。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [axum/src/routing/path_router.rs](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/path_router.rs) | `PathRouter<S>` 的全部实现：注册路由、维护 matchit 树、请求匹配与分发。本讲的主战场。 |
| [axum/src/routing/mod.rs](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs) | 定义 `NEST_TAIL_PARAM`、`FALLBACK_PARAM` 等内部常量，以及 fallback 如何借助通配参数挂到路由树上。 |
| [axum/src/routing/url_params.rs](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/url_params.rs) | `UrlParams` 枚举与 `insert_url_params`：把 matchit 的匹配结果过滤、percent-decode 后写入 `Extensions`。 |
| [axum/src/extract/path/mod.rs](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/extract/path/mod.rs) | `Path<T>` 提取器与各种 Rejection/ErrorKind，从 `Extensions` 读 `UrlParams` 并反序列化。 |
| [axum/src/extract/path/de.rs](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/extract/path/de.rs) | `PathDeserializer`：实现 serde 的 `Deserializer` trait，把参数键值对适配成 struct/tuple/map。 |

---

## 4. 核心概念与源码讲解

### 4.1 PathRouter：路径路由的内部容器

#### 4.1.1 概念说明

上一讲我们看到 `RouterInner` 里有一个 `path_router: PathRouter<S>` 字段。`PathRouter` 才是真正存放「路径 → 处理器」映射的地方。它要同时回答两个问题：

1. **注册时**：给一段路径模板（如 `/users/{id}`）和一个处理器，怎么存起来？
2. **请求时**：给一个真实 URL（如 `/users/42`），怎么快速找到对应处理器，并把 `42` 这个捕获值交出去？

`PathRouter` 的设计要点是：它**不是**自己手写一个字符串匹配引擎，而是把「按模板匹配 URL」这件事外包给一个专门的库 `matchit`，自己只负责「把 axum 的概念（处理器、状态）和 matchit 的概念（路由树）粘起来」。

#### 4.1.2 核心流程

`PathRouter` 的内部其实只有三个字段：

```text
PathRouter<S> {
    routes: Vec<Endpoint<S>>,   // 按注册顺序存所有处理器
    node:   Arc<Node>,          // matchit 路由树 + 两个反向映射
    v7_checks: bool,            // 是否拒绝旧版 ":param" / "*param" 写法
}
```

注册一条路由的流程：

```text
route("/users/{id}", method_router)
  │
  ├─ validate_path: 校验必须以 '/' 开头、不得用旧的 ":" / "*" 写法
  ├─ 若该 path 已有 MethodRouter  → 合并（.route("/", get).route("/", post) 生效）
  └─ 否则 new_route:
        ├─ id = RouteId(routes.len())      // 用当前向量长度当 id
        ├─ node.insert(path, id)           // 同时写入 matchit 树和两个 HashMap
        └─ routes.push(endpoint)           // 处理器追加到向量末尾
```

注意 `RouteId` 是一个「位置下标」，它是 `routes` 向量与 `node` 路由树之间的桥梁：matchit 树存的是 `RouteId`，匹配命中后用 `RouteId.0` 当下标去 `routes` 里取出真正的处理器。

#### 4.1.3 源码精读

先看 `PathRouter` 的字段定义：

```rust
pub(super) struct PathRouter<S> {
    routes: Vec<Endpoint<S>>,
    node: Arc<Node>,
    v7_checks: bool,
}
```

[axum/src/routing/path_router.rs:16-20](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/path_router.rs#L16-L20) — 三个字段，`routes` 是「处理器数组」，`node` 是「路由树 + 反查表」，`v7_checks` 控制旧语法校验。

再看注册新路由的核心 `new_route`：

```rust
fn new_route(&mut self, path: &str, endpoint: Endpoint<S>) -> Result<(), String> {
    let id = RouteId(self.routes.len());
    self.set_node(path, id)?;
    self.routes.push(endpoint);
    Ok(())
}
```

[axum/src/routing/path_router.rs:139-144](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/path_router.rs#L139-L144) — 用 `routes.len()` 当 `RouteId`，再把 `(path, id)` 写进 `node`，最后把处理器 push 进向量。三者靠同一个 `id` 串起来。

而 `set_node` 最终调用 `Node::insert`：

```rust
fn insert(&mut self, path: impl Into<String>, val: RouteId) -> Result<(), matchit::InsertError> {
    let path = path.into();
    self.inner.insert(&path, val)?;            // ① 写入 matchit 树
    let shared_path: Arc<str> = path.into();
    self.route_id_to_path.insert(val, shared_path.clone());  // ② id → path
    self.path_to_route_id.insert(shared_path, val);          // ③ path → id
    Ok(())
}
```

[axum/src/routing/path_router.rs:413-427](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/path_router.rs#L413-L427) — 一次插入写三处：matchit 树（用于匹配）、`route_id_to_path`（用于 `matched-path` feature 回填匹配到的模板）、`path_to_route_id`（用于 `.route()` 时判断「这个 path 是不是已经注册过」以便合并 MethodRouter）。

> **为什么 `routes` 向量和 `node` 两份数据都要保留？**
> 因为它们职责不同：`node`（matchit 树）擅长「按 URL 模板快速匹配」，但它存不下 axum 的 `Endpoint`（带泛型 `S`、可能是 `MethodRouter` 或 `Route`）；`routes` 向量擅长「按下标 O(1) 取处理器」，但不擅长模板匹配。两者通过 `RouteId` 这个下标关联，各取所长。

#### 4.1.4 代码实践

1. **实践目标**：在源码层面确认 `RouteId` 是「下标」这一事实。
2. **操作步骤**：
   - 打开 [path_router.rs](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/path_router.rs)，定位 `new_route`（L139）。
   - 找到 `merge`（L146）里 `for (id, route) in routes.into_iter().enumerate()` 这段，观察合并时是如何用 `RouteId(id)` 反查到 `path`，再调用 `self.route(path, ...)` 的。
3. **观察现象**：合并两个 `PathRouter` 时，并不会直接拷贝 matchit 树节点，而是「用 id 反查出 path，再走一遍正常的 `route()` 注册」。
4. **预期结果**：你会理解为什么 `merge` 比想象的「memcpy」慢——它本质上是把另一个路由器的所有路由重新注册一遍。

#### 4.1.5 小练习与答案

**练习 1**：如果连续调用 `.route("/a", get(_)).route("/a", post(_))`，会新建两个 `RouteId` 吗？

> **答案**：不会。`route()` 在 [L73-90](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/path_router.rs#L73-L90) 先查 `path_to_route_id`，发现 `/a` 已存在且是 `MethodRouter`，就走 `merge_for_path` 合并，原地替换 `routes[route_id.0]`，不会调用 `new_route`。

**练习 2**：`RouteId` 为什么用 `routes.len()` 而不是一个自增计数器？

> **答案**：因为 `RouteId` 的本质就是「在 `routes` 向量里的下标」。用 `routes.len()` 保证 push 后下标恰好等于 id，二者永远一致，取处理器只需 `self.routes.get(id.0)`，无需额外的查找结构。

---

### 4.2 matchit 路由树与三种路径语法

#### 4.2.1 概念说明

axum 自己不实现 URL 匹配算法，而是依赖 [`matchit`](https://docs.rs/matchit/=0.9.2) 这个 crate（在 [axum/Cargo.toml](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/Cargo.toml) 中锁定为 `matchit = "=0.9.2"`）。matchit 内部用一棵**基数树（radix tree）**按路径前缀组织所有模板，匹配一条 URL 的时间复杂度与路径长度成正比，而与注册的路由总数无关。这正是 axum 路由「快」的根源——这部分性能几乎完全来自 matchit。

在 matchit 的模板语法里，有三种我们最常用的写法：

| 写法 | 名称 | 匹配范围 | 例子 |
| --- | --- | --- | --- |
| `{name}` | 命名捕获 | 单个路径段（不含 `/`） | `/users/{id}` 匹配 `/users/42`，`id = "42"` |
| `{*name}` | 通配捕获 | 剩余整段路径（含 `/`） | `/files/{*path}` 匹配 `/files/a/b/c`，`path = "a/b/c"` |
| `{{` / `}}` | 字面量转义 | 匹配一个真正的 `{` 或 `}` | `/foo/{{x}}` 匹配字面量 `/foo/{x}` |

> 说明：`{name}` 和 `{*name}` 两种写法在 axum 自带的测试里有直接验证（见下方源码精读）；`{{` / `}}` 字面量转义是 matchit 0.9.x 模板语法的一部分（`{` `}` 被保留给捕获组，要匹配字面花括号就必须双写）。如果你需要在路由里匹配字面花括号，建议本地用一个小例子确认 matchit 0.9.2 的具体行为。

另外，命名捕获还可以**嵌在字面文本中间**——axum 的测试里就有 `/f{o}o/b{a}r` 这样的模板，`{o}` 和 `{a}` 仍是被捕获的参数，周围的 `f`/`o`、`b`/`r` 是字面前缀后缀。

#### 4.2.2 核心流程

一次匹配的流程（请求阶段，发生在 `call_with_state` 里）：

```text
node.at("/users/42")
  │
  ├─ Ok(match_)   → match_.value 是 &RouteId，match_.params 是捕获键值对
  │                   例如 [("id", "42")]
  └─ Err(NotFound) → 返回 Err((req, state))，交给上层 fallback 处理
```

matchit 在匹配成功时会同时给出「命中的值」（这里是 `RouteId`）和「捕获到的参数」（一个 `Params` 迭代器）。axum 拿到这两样东西后，前者用来定位处理器，后者用来填充 `Path` 提取器。

#### 4.2.3 源码精读

`Node` 是 matchit 路由树的薄包装：

```rust
struct Node {
    inner: matchit::Router<RouteId>,
    route_id_to_path: HashMap<RouteId, Arc<str>>,
    path_to_route_id: HashMap<Arc<str>, RouteId>,
}
```

[axum/src/routing/path_router.rs:405-410](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/path_router.rs#L405-L410) — `inner` 才是真正的 matchit 树，两个 HashMap 是为了支持「按 id 查 path」和「按 path 查 id」。

匹配就一行：

```rust
fn at<'n, 'p>(&'n self, path: &'p str) -> Result<matchit::Match<'n, 'p, &'n RouteId>, MatchError> {
    self.inner.at(path)
}
```

[axum/src/routing/path_router.rs:429-434](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/path_router.rs#L429-L434) — 直接转发给 matchit。axum 不参与具体的字符串匹配算法。

通配捕获 `{*rest}` 的行为，axum 有专门测试覆盖：

```rust
.route("/foo/{*rest}", get(|Path(param): Path<String>| async move { param }))
// ...
let res = client.get("/foo/bar/baz").await;
assert_eq!(res.text().await, "bar/baz");
```

[axum/src/extract/path/mod.rs:678-698](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/extract/path/mod.rs#L678-L698) — `{*rest}` 一次吞掉 `/bar/baz` 整段（去掉前导 `/` 后得到 `bar/baz`）。

「捕获嵌在字面文本中间」同样有测试：

```rust
.route("/f{o}o/b{a}r", get(|Path(params): Path<Vec<(String, String)>>| async move { /* ... */ }));
let res = client.get("/f0o/b4r").await;  // o => "0", a => "4"
```

[axum/src/extract/path/mod.rs:864-883](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/extract/path/mod.rs#L864-L883) — 印证了 `{o}`、`{a}` 是参数，前后字面字符照常匹配。

另外，axum 默认会拒绝旧版（v0.7）的 `:param` / `*param` 写法，给出明确提示：

```rust
if segment.starts_with(':') {
    Some(Err("Path segments must not start with `:`. For capture groups, use `{capture}`..."))
} else if segment.starts_with('*') {
    Some(Err("Path segments must not start with `*`. For wildcard capture, use `{*wildcard}`..."))
}
```

[axum/src/routing/path_router.rs:36-56](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/path_router.rs#L36-L56) — 这是 `v7_checks` 的核心，避免用户混用新旧语法。如果确实要匹配以 `:` 或 `*` 开头的字面段，可以调用 `without_v07_checks` 关闭这个校验。

#### 4.2.4 代码实践

1. **实践目标**：观察 `{id}` 与 `{*tail}` 两种捕获在 matchit 层面的差异。
2. **操作步骤**：
   - 写一个最小程序（依赖 `axum`、`tokio`）：

     ```rust
     // 示例代码：最小验证程序
     use axum::{extract::Path, routing::get, Router};

     async fn user(Path(id): Path<String>) -> String { format!("user={id}") }
     async fn file(Path(p): Path<String>) -> String { format!("file={p}") }

     #[tokio::main]
     async fn main() {
         let app = Router::new()
             .route("/users/{id}", get(user))
             .route("/files/{*tail}", get(file));
         let l = tokio::net::TcpListener::bind("127.0.0.1:3000").await.unwrap();
         axum::serve(l, app).await.unwrap();
     }
     ```

   - `cargo run` 后分别请求 `curl http://127.0.0.1:3000/users/42` 和 `curl http://127.0.0.1:3000/files/a/b/c`。
3. **观察现象**：`/users/42` 返回 `user=42`；`/files/a/b/c` 返回 `file=a/b/c`（通配捕获吞掉了多段）。
4. **预期结果**：`{id}` 只取一段，`{*tail}` 取剩余全部。若请求 `/users/a/b`，由于 `{id}` 不跨段，会命中 fallback 返回 404。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `/users/{id}` 不能匹配 `/users/`（空 id）？

> **答案**：命名捕获 `{id}` 要求匹配一个非空的路径段。axum 测试 `captures_dont_match_empty_path`（[L700-711](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/extract/path/mod.rs#L700-L711)）验证了 `/` 不会命中 `/{key}`，返回 404。

**练习 2**：模板 `/foo/{*rest}` 和 `/foo/{rest}` 有什么本质区别？

> **答案**：`{*rest}` 是通配捕获，能跨 `/` 匹配多段（如 `/foo/a/b` → `rest="a/b"`）；`{rest}` 是命名捕获，只匹配单段（`/foo/a` → `rest="a"`，但 `/foo/a/b` 不匹配）。

---

### 4.3 UrlParams：从匹配结果到请求扩展

#### 4.3.1 概念说明

matchit 匹配成功后返回的 `Params` 是一个带生命周期的临时迭代器，它不能直接塞进请求里供后续的 `Path` 提取器使用（提取器运行时匹配结果早已销毁）。于是 axum 设计了一个中间数据结构 `UrlParams`，在匹配阶段把捕获结果「物化」成一个 owned 的 `Vec`，存进请求的 `Extensions`，供提取器阶段读取。

`UrlParams` 是个内部枚举（`pub(crate)`），有两种状态：

- `Params(Vec<(Arc<str>, PercentDecodedStr)>)`：正常的捕获键值对，已完成 percent-decode。
- `InvalidUtf8InPathParam { key }`：某个参数 percent-decode 后不是合法 UTF-8，记下出错的 key。

#### 4.3.2 核心流程

把 matchit 的 `Params` 转成 `UrlParams` 并写入 `Extensions` 的流程（`insert_url_params`）：

```text
for (key, value) in matchit_params:
    过滤掉 key 以 NEST_TAIL_PARAM 或 FALLBACK_PARAM 开头的项   # ① 内部隐藏参数
    对 value 做 percent-decode:
        成功且是合法 UTF-8 → 保留 (key, decoded)
        失败                → 整体标记为 InvalidUtf8InPathParam { key }

把结果合并进 Extensions:
    已有 Params  → extend 追加（嵌套路由会多次调用）
    没有         → 插入新的 Params
    有 InvalidUtf8 → 保持错误状态，不再覆盖
```

关键点有两个：

1. **过滤内部参数**：`NEST_TAIL_PARAM` 和 `FALLBACK_PARAM` 是 axum 自己用来「挂载 nest 兜底和 404 兜底」的隐藏捕获组（见 4.3.3），它们不应该暴露给用户的 `Path` 提取器，所以在写入 `Extensions` 前被过滤掉。
2. **percent-decode**：URL 里的 `%20` 之类编码会被解码；如果解码后不是合法 UTF-8（例如某些非法字节序列），就转成 `InvalidUtf8InPathParam`，最终由 `Path` 提取器报 400。

#### 4.3.3 源码精读

`UrlParams` 枚举定义：

```rust
#[derive(Clone)]
pub(crate) enum UrlParams {
    Params(Vec<(Arc<str>, PercentDecodedStr)>),
    InvalidUtf8InPathParam { key: Arc<str> },
}
```

[axum/src/routing/url_params.rs:6-10](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/url_params.rs#L6-L10) — 两种状态：正常参数列表，或「某个参数 UTF-8 非法」。

写入逻辑的核心是两个 `filter`：

```rust
let params = params
    .iter()
    .filter(|(key, _)| !key.starts_with(super::NEST_TAIL_PARAM))
    .filter(|(key, _)| !key.starts_with(super::FALLBACK_PARAM))
    .map(|(k, v)| {
        if let Some(decoded) = PercentDecodedStr::new(v) {
            Ok((Arc::from(k), decoded))
        } else {
            Err(Arc::from(k))   // percent-decode 后非合法 UTF-8
        }
    })
    .collect::<Result<Vec<_>, _>>();
```

[axum/src/routing/url_params.rs:20-31](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/url_params.rs#L20-L31) — 两层过滤把内部隐藏参数剔除，再对每个值尝试 percent-decode。

那两个被过滤的常量长什么样？它们定义在 routing 模块根部：

```rust
pub(crate) const NEST_TAIL_PARAM: &str = "__private__axum_nest_tail_param";
pub(crate) const FALLBACK_PARAM: &str = "__private__axum_fallback";
pub(crate) const FALLBACK_PARAM_PATH: &str = "/{*__private__axum_fallback}";
```

[axum/src/routing/mod.rs:123-127](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L123-L127) — 这就是本讲实践任务要找的两个常量。

**它们的作用是什么？**

- **`NEST_TAIL_PARAM`（`__private__axum_nest_tail_param`）**：当用 `nest_service` 把一个 Service 挂到某个前缀时，axum 需要让「该前缀下的所有子路径」都命中这个 Service。matchit 没有原生的「前缀匹配」，于是 axum 用一个通配捕获 `{*__private__axum_nest_tail_param}` 来吞掉前缀后面的剩余路径。见 [path_router.rs:212-249](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/path_router.rs#L212-L249) 的 `nest_service`：

  ```rust
  let path = if path.ends_with('/') {
      format!("{path}{{*{NEST_TAIL_PARAM}}}")
  } else {
      format!("{path}/{{*{NEST_TAIL_PARAM}}}")
  };
  ```

  （这里的 `{{*...}}` 是 Rust 格式化字符串里转义出的字面 `{*...}`，传给 matchit 作为通配模板。）这个隐藏捕获只是用来「吃掉剩余路径」，不是用户参数，所以写入 `Extensions` 前要过滤掉。

- **`FALLBACK_PARAM`（`__private__axum_fallback`）**：全局 fallback（处理所有未命中的请求）也是通过把它注册成一个通配路由 `/{*__private__axum_fallback}` 来实现的。见 [mod.rs:419-437](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L419-L437)，fallback 的 endpoint 被挂到 `FALLBACK_PARAM_PATH` 上。同样，这个捕获不应暴露给用户的 `Path`，所以被过滤。

> 一句话总结：这两个常量是 axum 「用通配捕获模拟前缀匹配 / 兜底匹配」的内部机关，对用户透明——透明的方式就是在 `insert_url_params` 里把它们过滤掉。

最后看合并逻辑：

```rust
match (current_params, params) {
    (_, Err(invalid_key)) => {
        extensions.insert(UrlParams::InvalidUtf8InPathParam { key: invalid_key });
    }
    (Some(UrlParams::Params(current)), Ok(params)) => {
        current.extend(params);
    }
    (None, Ok(params)) => {
        extensions.insert(UrlParams::Params(params));
    }
}
```

[axum/src/routing/url_params.rs:33-46](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/url_params.rs#L33-L46) — 注意 `extend`：嵌套路由（nest）会让内层路由在外层之后再次匹配并再次调用 `insert_url_params`，所以这里用 `extend` 把内外两层的参数拼起来，而不是覆盖。

#### 4.3.4 代码实践

1. **实践目标**：验证 percent-decode 行为，并亲手找出两个内部常量。
2. **操作步骤**：
   - 在上面的最小程序基础上，把 `/users/{id}` 的 handler 改成返回原值：`async fn user(Path(id): Path<String>) -> String { id }`。
   - 请求 `curl http://127.0.0.1:3000/users/one%20two`（`%20` 是空格）。
   - 用编辑器打开 [url_params.rs](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/url_params.rs)，在 L22-23 找到两个 `filter`，再跳到 [mod.rs:123-127](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L123-L127) 看常量定义。
3. **观察现象**：`%20` 被解码成空格，响应体是 `one two`（对应测试 `percent_decoding`，[L642-654](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/extract/path/mod.rs#L642-L654)）。
4. **预期结果**：你能复述 `NEST_TAIL_PARAM` 用于 `nest_service` 的通配捕获、`FALLBACK_PARAM` 用于全局兜底的通配捕获，二者都被 `insert_url_params` 过滤掉不暴露给用户。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `insert_url_params` 用 `extend` 而不是 `insert`（覆盖）？

> **答案**：因为嵌套路由（nest）会让请求先后经过外层和内层两次匹配，每层都可能贡献各自的路径参数。用 `extend` 才能把内外层的参数合并到一个 `UrlParams::Params` 里，供 `Path` 一次性提取。

**练习 2**：如果某个用户参数的值是 `%E4%B8%AD`（合法 UTF-8，即「中」），和 `%ff`（非法 UTF-8）分别会发生什么？

> **答案**：前者被 `PercentDecodedStr::new` 成功解码，进入 `Params`；后者解码失败，整个 `UrlParams` 变成 `InvalidUtf8InPathParam { key }`，后续 `Path` 提取器返回 `FailedToDeserializePathParams`（状态码 400）。

---

### 4.4 Path 提取器：把参数反序列化成类型

#### 4.4.1 概念说明

走到这一步，请求的 `Extensions` 里已经有了一个 `UrlParams`，里面是 `[(key, value), ...]` 的键值对。但 handler 想要的往往是一个具体类型：`Path<i32>`、`Path<(String, Uuid)>`、`Path<MyStruct>`。把「字符串键值对」变成「强类型值」这一步，由 `Path<T>` 提取器完成，它借助 serde 的反序列化机制实现。

`Path<T>` 是个简单的 newtype：

```rust
#[derive(Debug)]
pub struct Path<T>(pub T);
```

它实现 `FromRequestParts`（只读 parts，不消费 body），这正是路径参数「不占用 body」的体现。它的 `Rejection` 是 `PathRejection`，能区分「参数数量不对」「类型解析失败」「UTF-8 非法」「根本没有参数」等不同情况，并映射到不同的 HTTP 状态码。

#### 4.4.2 核心流程

`Path<T>::from_request_parts` 的流程：

```text
1. get_params(parts): 从 parts.extensions 取出 UrlParams
     ├─ Params(vec)              → 返回 &[(key, value)]
     ├─ InvalidUtf8InPathParam   → 报 FailedToDeserializePathParams (400)
     └─ None                     → 报 MissingPathParams (500)
2. T::deserialize(PathDeserializer::new(vec))
     ├─ T 是单值类型(i32/String) → parse_single_value：要求恰好 1 个参数，再 .parse()
     ├─ T 是 tuple/struct        → 按位置/字段名逐个取值
     └─ 失败                     → 对应 ErrorKind（WrongNumberOfParameters / ParseError / ...）
3. 成功 → Path(value)；失败 → PathRejection
```

错误被归类为 `ErrorKind`，再决定状态码：

- 参数个数不对 / 不支持的类型 → `500 INTERNAL_SERVER_ERROR`（被视为程序员错误）。
- 解析失败 / UTF-8 非法 / 缺参数 → `400 BAD_REQUEST`（被视为客户端错误）。

#### 4.4.3 源码精读

`Path` 的提取实现：

```rust
async fn from_request_parts(parts: &mut Parts, _state: &S) -> Result<Self, Self::Rejection> {
    fn get_params(parts: &Parts) -> Result<&[(Arc<str>, PercentDecodedStr)], PathRejection> {
        match parts.extensions.get::<UrlParams>() {
            Some(UrlParams::Params(params)) => Ok(params),
            Some(UrlParams::InvalidUtf8InPathParam { key }) => { /* → 400 */ }
            None => Err(MissingPathParams.into()),
        }
    }
    match T::deserialize(de::PathDeserializer::new(get_params(parts)?)) {
        Ok(val) => Ok(Self(val)),
        Err(e) => Err(failed_to_deserialize_path_params(e)),
    }
}
```

[axum/src/extract/path/mod.rs:164-189](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/extract/path/mod.rs#L164-L189) — 先从 extensions 取出参数切片，再用 serde 反序列化。注意 `get_params` 被提取成独立函数，这样对所有 `T` 只编译一次。

`PathDeserializer` 是个非常薄的 serde Deserializer：

```rust
pub(crate) struct PathDeserializer<'de> {
    url_params: &'de [(Arc<str>, PercentDecodedStr)],
}
```

[axum/src/extract/path/de.rs:45-47](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/extract/path/de.rs#L45-L47) — 它只持有参数切片的引用。

单值类型的反序列化由一个宏批量生成，例如 `i32`、`String`、`Uuid`：

```rust
macro_rules! parse_single_value {
    ($trait_fn:ident, $visit_fn:ident, $ty:literal) => {
        fn $trait_fn<V>(self, visitor: V) -> Result<V::Value, Self::Error> {
            if self.url_params.len() != 1 {
                return Err(...wrong_number_of_parameters().got(self.url_params.len()).expected(1));
            }
            let value = self.url_params[0].1.parse().map_err(|_| {
                PathDeserializationError::new(ErrorKind::ParseError { value: ..., expected_type: $ty })
            })?;
            visitor.$visit_fn(value)
        }
    };
}
```

[axum/src/extract/path/de.rs:22-43](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/extract/path/de.rs#L22-L43) — 这就是为什么 `Path<i32>` 要求「恰好 1 个路径参数」，多了少了都会报 `WrongNumberOfParameters`。

tuple 类型则按位置匹配，并校验长度：

```rust
fn deserialize_tuple<V>(self, len: usize, visitor: V) -> Result<V::Value, Self::Error> {
    if self.url_params.len() != len {
        return Err(...wrong_number_of_parameters().got(self.url_params.len()).expected(len));
    }
    visitor.visit_seq(SeqDeserializer { params: self.url_params, idx: 0 })
}
```

[axum/src/extract/path/de.rs:153-166](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/extract/path/de.rs#L153-L166) — `Path<(String, String)>` 会要求恰好 2 个参数，按顺序填进元组。

错误如何映射到状态码？看 `FailedToDeserializePathParams::status`：

```rust
pub fn status(&self) -> StatusCode {
    match self.0.kind {
        ErrorKind::Message(_) | ErrorKind::DeserializeError { .. }
        | ErrorKind::InvalidUtf8InPathParam { .. } | ErrorKind::ParseError { .. }
        | ErrorKind::ParseErrorAtIndex { .. } | ErrorKind::ParseErrorAtKey { .. } =>
            StatusCode::BAD_REQUEST,
        ErrorKind::WrongNumberOfParameters { .. } | ErrorKind::UnsupportedType { .. } =>
            StatusCode::INTERNAL_SERVER_ERROR,
    }
}
```

[axum/src/extract/path/mod.rs:438-450](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/extract/path/mod.rs#L438-L450) — 客户端传错值（解析失败）算 400；但参数个数不对或不支持的类型，被认定是「程序员把 handler 签名写错了」，算 500。

完整的 `ErrorKind` 枚举列出了所有错误情形：

[axum/src/extract/path/mod.rs:285-355](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/extract/path/mod.rs#L285-L355) — 包括 `WrongNumberOfParameters`、`ParseErrorAtKey`（struct 字段解析失败）、`ParseErrorAtIndex`（tuple 元素解析失败）、`InvalidUtf8InPathParam`、`UnsupportedType` 等。

> **设计要点**：axum 把路径参数的反序列化做得和 serde JSON 一样灵活——可以反序列化成单值、tuple、struct（按字段名匹配）、`HashMap`、`Vec<(K,V)>`。但底层不是 JSON，而是一个自定义的 `PathDeserializer`，它把「扁平的路径参数列表」适配成 serde 能理解的 map/seq 模型。

#### 4.4.4 代码实践

1. **实践目标**：体验 `Path` 反序列化成 struct，并观察不同错误的响应。
2. **操作步骤**：把最小程序的 handler 换成：

   ```rust
   // 示例代码：用 struct 提取 + serde rename
   use serde::Deserialize;

   #[derive(Deserialize)]
   struct UserId { #[serde(rename = "id")] user_id: i32 }

   async fn user(Path(u): Path<UserId>) -> String { format!("user={}", u.user_id) }
   // 路由保持 /users/{id}
   ```

   分别请求：
   - `curl -i http://127.0.0.1:3000/users/42`
   - `curl -i http://127.0.0.1:3000/users/abc`（类型不匹配）
3. **观察现象**：前者返回 `user=42`（200）；后者返回 `Invalid URL: Cannot parse "id" with value "abc" to a "i32"`（400），对应 `ErrorKind::ParseErrorAtKey`。
4. **预期结果**：字段通过 `serde(rename)` 与路径捕获名 `id` 对应；类型解析失败时给出精确的「哪个 key、什么值、期望什么类型」错误。

#### 4.4.5 小练习与答案

**练习 1**：为什么 handler 写成 `async fn h(Path(a): Path<i32>, Path(b): Path<i32>)`（两个 `Path`）会得到 500 而不是 400？

> **答案**：每个 `Path<i32>` 都要求「恰好 1 个参数」，但实际 URL 里有两个捕获。第一个 `Path` 就会因为 `WrongNumberOfParameters { got: 2, expected: 1 }` 失败，这被视为程序员错误，返回 500。测试 `two_path_extractors`（[L798-811](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/extract/path/mod.rs#L798-L811)）验证了这一点，错误信息还会提示「多个参数请用 tuple 或 struct」。

**练习 2**：`Path<HashMap<String, String>>` 是如何工作的？

> **答案**：`PathDeserializer` 把参数列表当作一个 map 来反序列化（`deserialize_map`，[de.rs:188-197](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/extract/path/de.rs#L188-L197)），每个捕获键值对成为 HashMap 的一项。测试 `extracting_url_params`（[L611-630](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/extract/path/mod.rs#L611-L630)）里就用了 `Path<HashMap<String, i32>>`。

---

## 5. 综合实践

把本讲的三个最小模块（PathRouter / matchit / UrlParams）串起来，完成下面这个综合任务（即规格中的代码实践任务）：

> 设计 `/users/{id}` 和 `/files/{*path}` 两个路由，用 `Path` 提取器分别取值并打印，再阅读 `url_params.rs` 找出 `NEST_TAIL_PARAM` 和 `FALLBACK_PARAM` 这两个内部常量的作用。

**步骤**：

1. 新建一个 crate，`Cargo.toml` 依赖 `axum`（默认 feature）、`tokio`（开启 `macros`、`net`、`rt-multi-thread`）、`serde`（开启 `derive`）。
2. 编写 `main.rs`：

   ```rust
   // 示例代码：综合实践
   use axum::{extract::Path, routing::get, Router};
   use serde::Deserialize;

   // 用 struct 提取，演示字段名匹配
   #[derive(Deserialize, Debug)]
   struct UserId { id: u64 }

   async fn users(Path(u): Path<UserId>) -> String {
       println!("users: {u:?}");
       format!("user id = {}", u.id)
   }

   // 用通配捕获 {*} 提取多段路径
   async fn files(Path(path): Path<String>) -> String {
       println!("files: {path}");
       format!("file path = {path}")
   }

   #[tokio::main]
   async fn main() {
       let app: Router = Router::new()
           .route("/users/{id}", get(users))
           .route("/files/{*path}", get(files));
       let l = tokio::net::TcpListener::bind("127.0.0.1:3000").await.unwrap();
       axum::serve(l, app).await.unwrap();
   }
   ```

3. `cargo run`，依次请求并观察终端日志与响应：

   | 请求 | 预期响应 | 命中的捕获 |
   | --- | --- | --- |
   | `curl localhost:3000/users/42` | `user id = 42` | `id = "42"` → `u64` |
   | `curl localhost:3000/users/abc` | 400，`Cannot parse "id" with value "abc" to a "u64"` | 解析失败 |
   | `curl localhost:3000/files/a/b/c` | `file path = a/b/c` | `path = "a/b/c"`（通配） |
   | `curl localhost:3000/users/42/extra` | 404 | `{id}` 不跨段，命中 fallback |

4. 验证 percent-decode：`curl localhost:3000/files/a%20b` 应返回 `file path = a b`。
5. **源码阅读部分**：打开 [url_params.rs:20-23](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/url_params.rs#L20-L23)，找到两个 `filter`；再跳到 [mod.rs:123-127](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/mod.rs#L123-L127) 看常量定义。用自己的话写下：为什么这两个常量必须被过滤？（参考 4.3.3 的解释。）

**如果无法本地运行**：可改为纯源码阅读型实践——对照 [path_router.rs 的 `call_with_state`](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/routing/path_router.rs#L324-L372)，画出「`/users/42` → `node.at` → `insert_url_params` → `MethodRouter::call_with_state` → handler 里 `Path::from_request_parts` 读 `UrlParams`」的完整调用链。

---

## 6. 本讲小结

- 路径匹配发生在外部 crate **matchit** 的基数树上，axum 用 `Node` 包装它，并额外维护 `route_id_to_path` / `path_to_route_id` 两个 HashMap 支持反查与 MethodRouter 合并。
- `PathRouter` 同时维护 `routes: Vec<Endpoint>` 和 `node: Node`，靠 `RouteId`（即向量下标）把两者关联；这是「注册」与「匹配」职责分离的结果。
- 路径模板有三种写法：`{id}`（命名捕获，单段）、`{*tail}`（通配捕获，跨段）、`{{` / `}}`（字面花括号转义）；默认还会拒绝旧版 `:param` / `*param`。
- 匹配与提取是**两阶段**：先在 `call_with_state` 里 `node.at` 匹配并把参数 `insert_url_params` 写进 `Extensions`（`UrlParams`），再由 `Path<T>` 提取器用 serde 反序列化。
- `UrlParams` 在写入时会过滤掉 `NEST_TAIL_PARAM`（nest_service 的内部通配）和 `FALLBACK_PARAM`（全局兜底的内部通配）两个隐藏捕获，并对每个值做 percent-decode。
- `Path<T>` 的错误被归类为 `ErrorKind`：客户端传错值 → 400；参数个数不对 / 不支持的类型（程序员错误）→ 500。

## 7. 下一步学习建议

- 下一讲 [u2-l3 方法路由 MethodRouter](./u2-l3-method-router.md) 会讲同一路径如何按 HTTP 方法分发——你会看到 `PathRouter::call_with_state` 最后调用的 `method_router.call_with_state` 内部到底做了什么。
- 想深入「参数如何变成类型」的读者，可以继续精读 [de.rs](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/extract/path/de.rs) 里 `SeqDeserializer` / `MapDeserializer` 的完整实现，理解 struct 与 HashMap 反序列化的细节。
- 想理解 fallback 全貌的读者，预习 [u2-l5 Fallback 与 404 处理](./u2-l5-fallback-404.md)，那里会讲 `FALLBACK_PARAM_PATH` 是如何与用户自定义 fallback 配合的。
- 对 matchit 本身感兴趣的话，可以直接阅读 [matchit 0.9.2 文档](https://docs.rs/matchit/=0.9.2)，了解基数树匹配的复杂度与冲突检测规则。
