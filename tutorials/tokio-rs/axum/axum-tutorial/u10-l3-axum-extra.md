# axum-extra：类型化路由与扩展提取器/响应

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 `axum-extra` 在 axum 生态中的定位——它是「可选增强包」，每一类增强都用独立的 Cargo feature 按需开启。
- 理解 **类型化路由（typed routing）**：用 `TypedPath` + `RouterExt::typed_get/typed_post` 把「路径字符串」变成编译期可校验的类型，并在反方向上用 `Display` 安全地构造出链接。
- 彻底搞懂 `TypedPath::with_query_params` **最新的查询参数序列化行为**：为什么它现在输出 `/users?page=1&per_page=10` 而不再是 `/users?&page=1`，以及当路径本身已经含有 `?` 时会发生什么。
- 掌握 `WithRejection` 提取器：把任意提取器的 `Rejection`「翻译」成你自己的错误类型。
- 掌握 `CookieJar` 提取器：它如何从请求头读 cookie、又如何作为响应的一部分把改动写回 `Set-Cookie`。
- 速览 `Resource`（约定式 CRUD 路由）、`Protobuf`、`JsonLines`、`Cached` 等其他扩展，建立「遇到需求先去 axum-extra 找」的习惯。

> 本讲对应的一次源码增量（`485c603d..600e762b`）只改动了 [`axum-extra/src/routing/typed.rs`](https://github.com/tokio-rs/axum/blob/600e762b30dba5d1b9e253fabd36d0d23333e5d5/axum-extra/src/routing/typed.rs)，修复了 `TypedPath` 带查询参数序列化时多余的 `?&` 前缀。本讲会重点讲清这处修复。

## 2. 前置知识

阅读本讲前，建议你已经掌握（见前置讲义）：

- **Router 与路由注册**（u2-l1）：知道 `Router::route(path, method_router)` 怎么注册一条路由。
- **路径参数与 Path 提取器**（u2-l2、u3-l2）：知道 `{id}` 这种捕获组怎么从 URL 取出来。
- **IntoResponse 与响应构建**（u4-l1、u4-l2）：知道 handler 返回值如何变成 `Response`，以及响应头怎么追加。
- **提取器双 trait**（u3-l1）：`FromRequestParts`（只读 parts）与 `FromRequest`（消费 body）的区别。
- **Rejection 模型**（u7-l1）：提取失败会返回一个实现了 `IntoResponse` 的 `Rejection`。

下面两个概念是本讲反复用到的「地基」，先一句话回顾：

- **提取器（Extractor）**：任何实现了 `FromRequest`/`FromRequestParts` 的类型，写成 handler 参数就会被 axum 自动「从请求里取值」。
- **Cargo feature（条件编译开关）**：axum-extra 几乎每个增强都是独立 feature（如 `typed-routing`、`with-rejection`、`cookie`、`protobuf`）。不开 feature，对应的类型根本不会被编译进来。

## 3. 本讲源码地图

本讲涉及的关键文件（均在 `axum-extra` crate 内）：

| 文件 | 作用 | 关联的最小模块 |
| --- | --- | --- |
| [`axum-extra/src/routing/typed.rs`](https://github.com/tokio-rs/axum/blob/600e762b30dba5d1b9e253fabd36d0d23333e5d5/axum-extra/src/routing/typed.rs) | `TypedPath` trait、`WithQueryParams`、查询参数序列化（本次更新的核心） | TypedPath |
| [`axum-extra/src/routing/mod.rs`](https://github.com/tokio-rs/axum/blob/600e762b30dba5d1b9e253fabd36d0d23333e5d5/axum-extra/src/routing/mod.rs) | `RouterExt` 扩展 trait，提供 `typed_get/typed_post/...` 与 `route_with_tsr` | TypedPath |
| [`axum-extra/src/routing/resource.rs`](https://github.com/tokio-rs/axum/blob/600e762b30dba5d1b9e253fabd36d0d23333e5d5/axum-extra/src/routing/resource.rs) | `Resource` 约定式 CRUD 路由构建器 | 扩展速览 |
| [`axum-extra/src/extract/with_rejection.rs`](https://github.com/tokio-rs/axum/blob/600e762b30dba5d1b9e253fabd36d0d23333e5d5/axum-extra/src/extract/with_rejection.rs) | `WithRejection<E, R>` 提取器，转换 Rejection | WithRejection |
| [`axum-extra/src/extract/cookie/mod.rs`](https://github.com/tokio-rs/axum/blob/600e762b30dba5d1b9e253fabd36d0d23333e5d5/axum-extra/src/extract/cookie/mod.rs) | `CookieJar`（及 `SignedCookieJar`/`PrivateCookieJar`） | CookieJar |
| [`axum-extra/src/protobuf.rs`](https://github.com/tokio-rs/axum/blob/600e762b30dba5d1b9e253fabd36d0d23333e5d5/axum-extra/src/protobuf.rs) | `Protobuf<T>` 提取器兼响应 | 扩展速览 |

> 小提示：`axum-extra` 用 feature 控制编译。下面凡是涉及某类型的代码，`Cargo.toml` 里都要开启对应 feature，例如 `typed-routing`、`with-rejection`、`cookie`、`protobuf`。

## 4. 核心概念与源码讲解

### 4.1 TypedPath：把路径字符串升级成类型

#### 4.1.1 概念说明

普通的 `Router::route("/users/{id}", get(handler))` 有一个长期痛点：**路径字符串是「散落的字符串字面量」**。如果你在 handler 参数里写 `Path(id)`，编译器并不会检查 `/users/{id}` 和 `id` 是否真的对得上；如果你想在别处构造一个指向 `/users/42` 的链接，你只能手写字符串拼接，既容易拼错也容易忘做 URL 编码。

`TypedPath` 就是来解决这个问题的。它的核心思想是：**把「一个路径」定义成一个类型**，让这个类型同时承担三个职责：

1. **声明路径模板**：`const PATH: &'static str = "/users/{id}"`。
2. **作为提取器**：请求进来时，axum 把捕获到的 `{id}` 反序列化进这个结构体的字段。
3. **作为链接构造器**：实现 `Display`，把结构体的字段填回模板，安全地拼出真实 URL（并自动 percent-encode）。

这样，「路径模板」「提取出来的参数」「生成的链接」三者被同一个类型**绑定**在一起，编译器帮你检查一致性。

#### 4.1.2 核心流程

类型化路由的注册流程非常简洁，背后靠的是「类型推断」：

```
定义 #[derive(TypedPath)] 的结构体（带 #[typed_path("/users/{id}")]）
        │  派生宏生成：TypedPath impl + FromRequest impl + Display impl
        ▼
在 handler 第一个参数位置写上该结构体
        │  派生宏要求：TypedPath 必须是 handler 的第一个参数
        ▼
调用 Router::new().typed_get(handler)
        │  typed_get 内部读 P::PATH 作为路径字符串，等价于 .route(P::PATH, get(handler))
        ▼
axum 用 P::PATH 注册路由；请求到来时用生成的 FromRequest 从路径取参数
```

关键点：**路径不再由你手写在 `typed_get` 里**，而是从 `P::PATH` 推断出来。`P` 就是 handler 第一个参数的类型。这就是「typed」的含义——路径来自类型，而不是字符串。

#### 4.1.3 源码精读

先看 `TypedPath` trait 本身。它非常小，只要求实现者提供 `PATH` 常量和一个 `Display`：

[`axum-extra/src/routing/typed.rs:217-276`](https://github.com/tokio-rs/axum/blob/600e762b30dba5d1b9e253fabd36d0d23333e5d5/axum-extra/src/routing/typed.rs#L217-L276) 定义了 `TypedPath` trait。其中：

- `const PATH: &'static str`（L219）：关联的路径模板，如 `/users/{id}`。
- `fn to_uri(&self) -> Uri`（L232-L234）：默认实现是 `self.to_string().parse().unwrap()`——也就是先走 `Display` 拼出字符串，再解析成 `http::Uri`。因为派生宏生成的 `Display` 会做 percent-encode，所以这里解析不会失败。
- `fn with_query_params<T>(self, params: T) -> WithQueryParams<Self, T>`（L269-L275）：把查询参数挂到路径上，返回一个 `WithQueryParams` 包装类型（见 4.1.5）。

再看 `typed_get` 是怎么把「类型」变成「路由」的。实现极其简短：

[`axum-extra/src/routing/mod.rs:282-289`](https://github.com/tokio-rs/axum/blob/600e762b30dba5d1b9e253fabd36d0d23333e5d5/axum-extra/src/routing/mod.rs#L282-L289)：

```rust
fn typed_get<H, T, P>(self, handler: H) -> Self
where
    H: axum::handler::Handler<T, S>,
    T: SecondElementIs<P> + 'static,
    P: TypedPath,
{
    self.route(P::PATH, axum::routing::get(handler))
}
```

看最后一句：`self.route(P::PATH, get(handler))`。它和你手写 `.route("/users/{id}", get(handler))` 完全等价，区别只在于路径来自 `P::PATH`。其余 `typed_post/typed_put/...` 都是同一个套路（见 [`axum-extra/src/routing/mod.rs:291-369`](https://github.com/tokio-rs/axum/blob/600e762b30dba5d1b9e253fabd36d0d23333e5d5/axum-extra/src/routing/mod.rs#L291-L369)）。

那 `T: SecondElementIs<P>` 这个奇怪的约束是干什么的？它是一个**编译期「类型证明」**：保证 handler 的「第二个元素」（即 handler 的第一个参数，因为 `Handler<T, S>` 的 `T` 是参数元组）确实是 `P`。这样如果你忘了把 `TypedPath` 类型放在第一个参数，编译器会用一个相对友好的提示告诉你。它的定义见 [`axum-extra/src/routing/typed.rs:333`](https://github.com/tokio-rs/axum/blob/600e762b30dba5d1b9e253fabd36d0d23333e5d5/axum-extra/src/routing/typed.rs#L333)，并通过宏 [`impl_second_element_is!`](https://github.com/tokio-rs/axum/blob/600e762b30dba5d1b9e253fabd36d0d23333e5d5/axum-extra/src/routing/typed.rs#L335-L367) 为 0~16 个参数的元组、`Option<P>`、`Result<P, E>` 三种形态都生成了实现。

> 派生宏 `#[derive(TypedPath)]` 的展开细节（生成 `TypedPath`/`FromRequest`/`Display` 三套 impl，并校验路径捕获与结构体字段一一对应）属于 `axum-macros` 的范畴，已在 u10-l2 讲过，本讲直接使用其产物。

#### 4.1.4 代码实践

**实践目标**：亲手把一条普通路由改造成类型化路由，体会「路径来自类型」的好处。

**操作步骤**：

1. 在 `Cargo.toml` 中开启 feature：

   ```toml
   [dependencies]
   axum = "0.8"
   axum-extra = { version = "0.10", features = ["typed-routing"] }
   serde = { version = "1", features = ["derive"] }
   tokio = { version = "1", features = ["full"] }
   ```

2. 编写最小示例（示例代码）：

   ```rust
   use axum::{Router, routing::get};
   use axum_extra::routing::{RouterExt, TypedPath}; // RouterExt 引入 typed_get
   use serde::Deserialize;

   // 把 /users/{id} 这个路径「类型化」
   #[derive(TypedPath, Deserialize)]
   #[typed_path("/users/{id}")]
   struct UserMember { id: u32 }

   // handler 的第一个参数就是这条路径的类型
   async fn users_show(UserMember { id }: UserMember) -> String {
       format!("user #{id}")
   }

   // 注意：typed_get 里没有手写路径字符串
   let app: Router = Router::new().typed_get(users_show);
   ```

3. 在终端编译：`cargo check`。

**需要观察的现象 / 预期结果**：

- 编译通过。`typed_get` 内部读取了 `UserMember::PATH`（即 `/users/{id}`）来注册路由。
- 如果故意把结构体字段改成 `user_id: u32`（与路径里的 `{id}` 不匹配），派生宏会在编译期报错——这就是「类型化」带来的安全保证。
- 如果你想反向构造一个链接，`UserMember { id: 42 }.to_string()` 会得到 `"/users/42"`，无需手拼字符串。

> 待本地验证：以上为示例代码，未在当前会话执行；请在本地 `cargo run` 后用 `curl localhost:3000/users/42` 确认返回 `user #42`。

#### 4.1.5 小练习与答案

**练习 1**：`typed_get` 与 `route(P::PATH, get(handler))` 有什么本质区别？

> **答案**：没有运行时区别，`typed_get` 的函数体就是后者。区别在编译期：`typed_get` 通过 `SecondElementIs<P>` 约束强制 handler 第一个参数必须是实现了 `TypedPath` 的类型 `P`，并把路径字符串收敛到 `P::PATH` 这一处定义，避免路径字符串散落多处。

**练习 2**：为什么 `TypedPath` trait 要求实现 `std::fmt::Display`，而不是单独提供一个 `fn to_uri()`？

> **答案**：`Display` 同时服务于两个目的：(1) `to_uri()` 的默认实现就是 `self.to_string().parse()`；(2) 让你能在任何需要字符串的地方（模板、日志、重定向链接）直接 `format!("{}", path)` 安全地拿到带 percent-encode 的 URL。一个 trait 解决「提取」「链接构造」两个方向。

---

### 4.1.6（重点）with_query_params 与查询参数序列化

> 这是本讲（也是本次源码更新）的核心。请仔细读。

#### 概念说明

`TypedPath` 描述的是**路径部分**（path + 捕获组）。但真实世界的 URL 往往还带**查询参数**（query string），例如 `/users?page=1&per_page=10`。`TypedPath` 本身不知道查询参数的存在，于是提供了 `with_query_params` 方法：它把「一个已序列化好的路径」和「一组查询参数」组合成一个新类型 `WithQueryParams<P, T>`，后者同样实现了 `Display`，因此在 `to_uri()` 时会把查询串正确地拼到路径后面。

这里有一个看似简单实则容易出错的细节：**查询串要以单个 `?` 起始、参数之间用 `&` 连接**，即正确的形式是 `?page=1&per_page=10`，而不是 `?&page=1`（多了一个 `&`）或 `page=1&per_page=10`（漏了 `?`）。本次源码更新修的正是这个 bug。

#### 核心流程

`WithQueryParams` 的 `Display` 实现需要处理两种情况：

```
情况 A：路径里还没有 ?  （如 "/users"）
    → 推入一个 '?'
    → 记录 '?' 之后的位置作为 params_start
    → 在该位置之后追加查询参数

情况 B：路径里已经有 ?  （如 "/test?"，或上一次 with_query_params 已拼好的串）
    → 不再推入新的 '?'
    → 找到现有 '?' 之后的位置作为 params_start
    → 在该位置之后追加查询参数（若该位置已有内容，自动加 '&' 分隔）
```

关键是：序列化器必须知道「查询内容从哪个字节开始已经存在」，才能决定第一个参数前要不要加 `&`。这正是 `form_urlencoded::Serializer::for_suffix(buf, start)` 这个 API 的用途——它表示「`buf` 里已经有一段查询内容、起点在 `start`，请在正确位置追加」。

#### 源码精读（含本次修复）

`WithQueryParams` 结构体本身很朴素，只是把路径和参数包在一起：

[`axum-extra/src/routing/typed.rs:281-285`](https://github.com/tokio-rs/axum/blob/600e762b30dba5d1b9e253fabd36d0d23333e5d5/axum-extra/src/routing/typed.rs#L281-L285)：

```rust
#[derive(Debug, Clone, Copy)]
pub struct WithQueryParams<P, T> {
    path: P,
    params: T,
}
```

它还转发实现了 `TypedPath`（`PATH` 就是内部 `P::PATH`），见 [`axum-extra/src/routing/typed.rs:314-320`](https://github.com/tokio-rs/axum/blob/600e762b30dba5d1b9e253fabd36d0d23333e5d5/axum-extra/src/routing/typed.rs#L314-L320)，所以 `WithQueryParams` 也能继续被 `typed_*` 使用、也能继续 `to_uri()`。

真正干活的 `Display` 实现（**本次修复就在这里**）：

[`axum-extra/src/routing/typed.rs:292-311`](https://github.com/tokio-rs/axum/blob/600e762b30dba5d1b9e253fabd36d0d23333e5d5/axum-extra/src/routing/typed.rs#L292-L311)：

```rust
fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
    let mut out = self.path.to_string();

    let params_start = out.find('?').map(|i| i + 1).unwrap_or_else(|| {
        out.push('?');
        out.len()
    });
    let mut urlencoder = form_urlencoded::Serializer::for_suffix(&mut out, params_start);
    self.params
        .serialize(serde_html_form::ser::Serializer::new(&mut urlencoder))
        .unwrap_or_else(|err| {
            panic!("failed to URL encode value of type `{}`: {err}", type_name::<T>())
        });
    f.write_str(&out)?;
    Ok(())
}
```

逐行拆解这段修复后的逻辑：

1. `let mut out = self.path.to_string();`——先把内部路径序列化成字符串（比如 `/users`，或者 `/users/1?foo=foo` 如果之前已经拼过参数）。
2. `out.find('?')`——查找路径里**是否已经含有 `?`**：
   - **找到**（情况 B）：`params_start = 该位置 + 1`，即 `?` 之后那个字节的位置。**不**再 push 新的 `?`。
   - **没找到**（情况 A）：在闭包里 `out.push('?')`，并令 `params_start = out.len()`（push 之后的新长度，正好是 `?` 之后的位置）。
3. `Serializer::for_suffix(&mut out, params_start)`——告诉序列化器：「`out` 中已经存在一段查询内容，起点在 `params_start`，请相对它正确地插入分隔符」。这是本次修复的关键改动。
4. 用 `serde_html_form` 把 `self.params` 序列化进这个 urlencoder；序列化失败会 `panic!`（因为生成无效 URL 基本是程序员传了无法序列化为查询参数的类型，属于不可恢复错误）。

**对比旧实现（本次更新前的版本）**：

```rust
// 旧代码（已删除）
if !out.contains('?') {
    out.push('?');
}
let mut urlencoder = form_urlencoded::Serializer::new(&mut out);
```

旧代码用 `Serializer::new(&mut out)`。问题在于：`Serializer::new` 是为「从空字符串开始全新构造查询串」设计的，它不知道 `out` 已经以 `?` 结尾。结果它给第一个参数前也插了一个 `&` 分隔符，于是产出 `/users?&page=1&per_page=10`——多出来的那个 `?&`。虽然 URL 规范允许 `?&`（空参数段会被忽略），但它不美观，也容易让下游严格解析器困惑。改用 `for_suffix` 并传入精确的字节偏移后，序列化器能判断「`params_start` 处是否已有内容」，从而只在确有内容时才加 `&`，产出干净的 `/users?page=1&per_page=10`。

测试代码也同步更新了断言，明确记录了正确行为：

[`axum-extra/src/routing/typed.rs:405-431`](https://github.com/tokio-rs/axum/blob/600e762b30dba5d1b9e253fabd36d0d23333e5d5/axum-extra/src/routing/typed.rs#L405-L431) 里的 `with_params` 与 `with_params_called_multiple_times` 两个测试，断言分别是：

- `/users/1?foo=foo&bar=123&baz=true`（单次拼接）
- `/users/1?foo=foo&bar=123&baz=true&qux=1337`（链式两次拼接，第二次自动补 `&`）

并且新增了一个针对「路径本身已含 `?`」的测试 [`with_params_question_mark_no_params`](https://github.com/tokio-rs/axum/blob/600e762b30dba5d1b9e253fabd36d0d23333e5d5/axum-extra/src/routing/typed.rs#L433-L450)：

```rust
#[derive(TypedPath)]
#[typed_path("/test?")]
struct EndsWithQuestionMark;

assert_eq!(EndsWithQuestionMark.to_uri(), "/test?");      // 无参数时原样保留 ?
// 加参数后，参数接在已有的 ? 之后，不再加新 ?
assert_eq!(path.to_uri(), "/test?foo=foo&bar=123&baz=true");
```

这正是「情况 B」的体现：`out.find('?')` 命中了模板自带的 `?`，于是 `params_start` 指向它之后，不再 push 新的 `?`。

#### 代码实践

**实践目标**：亲手验证查询参数序列化的最新正确行为，确认没有多余的 `?&`。

**操作步骤**（示例代码，可用 `#[test]` 或 `fn main` 跑）：

```rust
use axum_extra::routing::TypedPath;
use serde::Serialize;

#[derive(TypedPath)]
#[typed_path("/users")]
struct Users;

#[derive(Serialize)]
struct Pagination { page: u32, per_page: u32 }

fn main() {
    let path = Users.with_query_params(Pagination { page: 1, per_page: 10 });
    println!("{}", path.to_uri());
}
```

**预期结果**：输出 `/users?page=1&per_page=10`。

**需要观察的现象**：

- 必须是**单个** `?`，且 `?` 后**直接**跟第一个参数（`?page=1`），而不是 `?&page=1`。
- 多个参数之间用 `&` 连接：`page=1&per_page=10`。
- 再尝试链式调用 `.with_query_params([("sort","desc")])`，应得到 `/users?page=1&per_page=10&sort=desc`（第二次自动补 `&`）。
- 把 `#[typed_path("/users")]` 改成 `#[typed_path("/users?")]`（路径自带 `?`），输出仍应是 `/users?page=1&per_page=10`，不会变成 `/users??page=1`。

> 待本地验证：以上为示例代码，未在当前会话执行；请在本地运行确认。如果你拿到的输出含 `?&`，说明依赖的 axum-extra 版本早于本次修复。

#### 小练习与答案

**练习 1**：为什么修复要改用 `Serializer::for_suffix(buf, start)` 而不是继续用 `Serializer::new(buf)`？

> **答案**：`Serializer::new` 假定 buffer 是「全新的查询串」，它会在第一个参数前也插入分隔符，导致 `?` 之后多出 `&`。`for_suffix(buf, start)` 显式告诉序列化器「buffer 中 `start` 位置开始已有查询内容」，它据此判断首个参数前是否需要 `&`，从而产出干净的 `?k=v&k2=v2`。

**练习 2**：当 `#[typed_path("/a?b")]`（路径中间就有 `?`）调用 `with_query_params` 时，`params_start` 等于多少？

> **答案**：`out.find('?')` 返回 `?` 的字节下标（设为 `i`），`params_start = i + 1`，即 `?` 之后那一位。后续参数从该位置追加。因此不会重复加 `?`。

**练习 3**：`WithQueryParams<P, T>` 为什么自己也要实现 `TypedPath`？

> **答案**：为了让「带查询参数的路径」仍然能被 `typed_*` 系列方法使用、也能继续调用 `to_uri()`。它的 `PATH` 直接转发为内部 `P::PATH`（见 typed.rs L314-L320），保持与原始路径模板一致。

---

### 4.2 WithRejection：把提取器的拒绝「翻译」成你的错误类型

#### 4.2.1 概念说明

每个提取器都有自己的 `Rejection` 类型（如 `JsonRejection`、`PathRejection`）。这些内置 Rejection 实现了 `IntoResponse`，所以提取失败时 axum 会自动把它变成一个 HTTP 响应。但它们的响应格式是 axum 内定的（通常是纯文本 + 固定状态码），往往不符合你项目的统一错误格式（比如你想要统一的 JSON 错误体 `{ "code": ..., "message": ... }`）。

`WithRejection<E, R>` 就是一个**适配器提取器**：它包住内层提取器 `E`，把 `E::Rejection` 通过 `From<E::Rejection>` 转换成你自定义的 `R`，再以 `R` 作为自己的 `Rejection`。于是你可以在「转换」这一步把内置错误信息改造成项目统一格式，而 handler 侧仍然拿到的是 `E` 提取出来的值。

#### 4.2.2 核心流程

```
WithRejection<E, R> 作为 handler 参数
        │  axum 调用 E::from_request(...)
        ▼
   ┌──── E 提取成功 ────┐    ┌──── E 提取失败 ────┐
   │ Ok(E 的值)         │    │ Err(E::Rejection) │
   │ 包成 WithRejection │    │ 通过 From 转成 R    │
   │ 返回给 handler     │    │ 作为 WithRejection │
   └────────────────────┘    │ 的 Rejection 返回  │
                              └────────────────────┘
```

两个关键约束（编译期）：

- `R: From<E::Rejection>`：你必须能从内层拒绝构造出 `R`。
- `R: IntoResponse`：和所有 Rejection 一样，`R` 必须能变成响应。

#### 4.2.3 源码精读

`WithRejection` 是个透明的元组 newtype：

[`axum-extra/src/extract/with_rejection.rs:60`](https://github.com/tokio-rs/axum/blob/600e762b30dba5d1b9e253fabd36d0d23333e5d5/axum-extra/src/extract/with_rejection.rs#L60)：

```rust
pub struct WithRejection<E, R>(pub E, pub PhantomData<R>);
```

第一个字段就是内层提取器的值（`pub`，所以可以直接模式匹配解构，如 `WithRejection(Json(p), _): WithRejection<Json<P>, MyErr>`）；第二个是幻影类型，只用来「携带」`R` 这个类型参数。

`FromRequest` 实现（消费 body 的版本）：

[`axum-extra/src/extract/with_rejection.rs:112-124`](https://github.com/tokio-rs/axum/blob/600e762b30dba5d1b9e253fabd36d0d23333e5d5/axum-extra/src/extract/with_rejection.rs#L112-L124)：

```rust
impl<E, R, S> FromRequest<S> for WithRejection<E, R>
where
    S: Send + Sync,
    E: FromRequest<S>,
    R: From<E::Rejection> + IntoResponse,
{
    type Rejection = R;

    async fn from_request(req: Request, state: &S) -> Result<Self, Self::Rejection> {
        let extractor = E::from_request(req, state).await?;
        Ok(Self(extractor, PhantomData))
    }
}
```

注意三点：

1. `type Rejection = R;`（L118）——它的拒绝类型不再是 `E::Rejection`，而是**你的** `R`。这就是「翻译」的本质。
2. 约束 `R: From<E::Rejection> + IntoResponse`（L116）——翻译能力与可响应能力的来源。
3. `E::from_request(req, state).await?` 里的 `?`：当内层失败时，`E::Rejection` 通过 `From` 自动转成 `R` 并作为 `Err` 返回。`FromRequestParts` 版本（[L126-L138](https://github.com/tokio-rs/axum/blob/600e762b30dba5d1b9e253fabd36d0d23333e5d5/axum-extra/src/extract/with_rejection.rs#L126-L138)）逻辑完全一致，只是只读 parts。

此外，当开启 `typed-routing` feature 时，`WithRejection<E, R>` 还会为 `E: TypedPath` 转发实现 `TypedPath`（[L140-L146](https://github.com/tokio-rs/axum/blob/600e762b30dba5d1b9e253fabd36d0d23333e5d5/axum-extra/src/extract/with_rejection.rs#L140-L146)），并转发 `Display`（[L148-L155](https://github.com/tokio-rs/axum/blob/600e762b30dba5d1b9e253fabd36d0d23333e5d5/axum-extra/src/extract/with_rejection.rs#L148-L155)）。这意味着你可以在类型化路由里直接用 `WithRejection<MyTypedPath, MyErr>` 作为 handler 第一个参数——既享受类型安全路由，又自定义拒绝格式。

#### 4.2.4 代码实践

**实践目标**：把 `Path` 提取器的内置 `PathRejection` 翻译成自定义 JSON 错误。

**操作步骤**（示例代码）：

```rust
use axum::{extract::rejection::PathRejection, response::{IntoResponse, Response}, Json};
use axum_extra::extract::WithRejection;
use serde::Deserialize;
use serde_json::json;

// 自定义统一错误
struct ApiError(String);
impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        Json(json!({ "code": 400, "message": self.0 })).into_response()
    }
}
// 关键：从内置 PathRejection 转换
impl From<PathRejection> for ApiError {
    fn from(r: PathRejection) -> Self { ApiError(r.to_string()) }
}

#[derive(Deserialize)]
struct UserId { id: u32 }

async fn handler(
    WithRejection(id, _): WithRejection<axum::extract::Path<UserId>, ApiError>,
) -> String {
    format!("user {}", id.id)
}
```

**需要观察的现象 / 预期结果**：

- 正常请求 `/users/5` → `id.id = 5`，返回 `user 5`。
- 非法请求 `/users/abc`（类型不匹配）→ 内层 `Path` 返回 `PathRejection`，被翻译成 `ApiError`，最终响应体是统一 JSON 格式 `{"code":400,"message":"..."}`，而不是 axum 默认的纯文本。

> 待本地验证：以上为示例代码，请在本地用 `curl` 触发合法与非法两种请求，对照响应体确认翻译生效。

#### 4.2.5 小练习与答案

**练习 1**：`WithRejection<E, R>` 的 `Rejection` 关联类型是什么？为什么不是 `E::Rejection`？

> **答案**：是 `R`。因为整个适配器的目的就是把内层拒绝翻译出去；如果还用 `E::Rejection`，就没有「翻译」这一步了。

**练习 2**：如果 `R` 只实现了 `From<E::Rejection>` 而忘了实现 `IntoResponse`，会发生什么？

> **答案**：编译失败。`WithRejection` 的 trait 约束同时要求 `R: From<E::Rejection> + IntoResponse`（见 with_rejection.rs L116）。Rejection 必须能变成响应，这是 axum 错误模型的硬性要求（见 u7-l1）。

---

### 4.3 CookieJar：既是提取器又是响应 Part

#### 4.3.1 概念说明

HTTP cookie 是「请求带进来（`Cookie` 头）、响应带出去（`Set-Cookie` 头）」的双向机制。`CookieJar` 把这件事封装成一个极其顺手的类型：它**既是一个提取器**（从请求的 `Cookie` 头读出所有 cookie），**又是一个 `IntoResponseParts`**（把对 jar 的修改写回 `Set-Cookie` 头）。

这种「提取 + 修改 + 返回」一气呵成的设计意味着：handler 里拿到 `CookieJar`，调用 `.add(...)`/`.remove(...)` 修改后，**必须把 jar 作为返回值的一部分返回**，改动才会真正落到响应上。这正是源码里 `#[must_use]` 注释提醒的。

#### 4.3.2 核心流程

```
请求进入，handler 参数声明了 CookieJar
        │  FromRequestParts：扫描所有 Cookie 头，解析成 jar 的「原始 cookie」
        ▼
handler 调用 jar.add(cookie) / jar.remove(cookie)
        │  这些修改只记在 jar 的「delta」里
        ▼
handler 返回 (jar, 其他响应内容)
        │  IntoResponseParts：遍历 delta，每个改动 append 一条 Set-Cookie 头
        ▼
浏览器据此更新本地 cookie
```

理解一个关键区分：`cookie` crate 的 `CookieJar` 把 cookie 分成「原始的（original，来自请求）」和「增量（delta，本次新增/删除）」两类。响应阶段只把 **delta** 写回，避免把请求里所有 cookie 又原样塞回响应。

#### 4.3.3 源码精读

`CookieJar` 是对底层 `cookie::CookieJar` 的薄包装：

[`axum-extra/src/extract/cookie/mod.rs:98-102`](https://github.com/tokio-rs/axum/blob/600e762b30dba5d1b9e253fabd36d0d23333e5d5/axum-extra/src/extract/cookie/mod.rs#L98-L102)：

```rust
#[must_use = "`CookieJar` should be returned as part of a `Response`, otherwise it does nothing."]
pub struct CookieJar {
    jar: cookie::CookieJar,
}
```

注意 `#[must_use]`：如果你忘了把 jar 放进返回值，编译器会警告「它什么都不会做」。

作为提取器，它的 `Rejection` 是 `Infallible`（永不失败——解析不到 cookie 也不是错，只是空 jar）：

[`axum-extra/src/extract/cookie/mod.rs:104-113`](https://github.com/tokio-rs/axum/blob/600e762b30dba5d1b9e253fabd36d0d23333e5d5/axum-extra/src/extract/cookie/mod.rs#L104-L113)。具体解析逻辑在 [`cookies_from_request`](https://github.com/tokio-rs/axum/blob/600e762b30dba5d1b9e253fabd36d0d23333e5d5/axum-extra/src/extract/cookie/mod.rs#L115-L122)：取所有 `COOKIE` 头，按 `;` 切分，逐个 `Cookie::parse_encoded`。然后把它们作为 `add_original` 装进 jar（[`from_headers`](https://github.com/tokio-rs/axum/blob/600e762b30dba5d1b9e253fabd36d0d23333e5d5/axum-extra/src/extract/cookie/mod.rs#L133-L139)）。

读写的便捷方法：

- [`get`](https://github.com/tokio-rs/axum/blob/600e762b30dba5d1b9e253fabd36d0d23333e5d5/axum-extra/src/extract/cookie/mod.rs#L169-L171)：按名取 cookie。
- [`add`](https://github.com/tokio-rs/axum/blob/600e762b30dba5d1b9e253fabd36d0d23333e5d5/axum-extra/src/extract/cookie/mod.rs#L205-L208)：**消费** `self` 后返回新的 jar（builder 风格），值会自动 percent-encode。
- [`remove`](https://github.com/tokio-rs/axum/blob/600e762b30dba5d1b9e253fabd36d0d23333e5d5/axum-extra/src/extract/cookie/mod.rs#L185-L188)：同样消费并返回。

把改动写回响应的是 `IntoResponseParts`：

[`axum-extra/src/extract/cookie/mod.rs:216-223`](https://github.com/tokio-rs/axum/blob/600e762b30dba5d1b9e253fabd36d0d23333e5d5/axum-extra/src/extract/cookie/mod.rs#L216-L223)：

```rust
impl IntoResponseParts for CookieJar {
    type Error = Infallible;
    fn into_response_parts(self, mut res: ResponseParts) -> Result<ResponseParts, Self::Error> {
        set_cookies(&self.jar, res.headers_mut());
        Ok(res)
    }
}
```

[`set_cookies`](https://github.com/tokio-rs/axum/blob/600e762b30dba5d1b9e253fabd36d0d23333e5d5/axum-extra/src/extract/cookie/mod.rs#L231-L240) 遍历 `jar.delta()`，把每个改动 `append` 成一条 `Set-Cookie` 头。注释里特意说明：因为 `into_response_parts` 会消费 jar，所以不需要 `reset_delta`。

> `CookieJar` 是明文版本。如果需要**签名**（防篡改，可读但不可改）或**加密**（不可读），分别用 `SignedCookieJar`/`PrivateCookieJar`（开启 `cookie-signed`/`cookie-private` feature），它们额外需要一个 `Key`（通常通过 `FromRef` 从 State 取，见 u3-l5）。

#### 4.3.4 代码实践

**实践目标**：实现一个「设置 cookie」和「读取 cookie」的最小闭环。

**操作步骤**（示例代码，开启 `cookie` feature）：

```rust
use axum::{routing::get, Router};
use axum_extra::extract::cookie::{Cookie, CookieJar};

async fn set_cookie(jar: CookieJar) -> CookieJar {
    jar.add(Cookie::new(("name", "claude"))) // 注意：必须返回 jar
}

async fn read_cookie(jar: CookieJar) -> String {
    jar.get("name").map(|c| c.value().to_owned()).unwrap_or_else(|| "<none>".into())
}

let app: Router = Router::new()
    .route("/set", get(set_cookie))
    .route("/read", get(read_cookie));
```

**需要观察的现象 / 预期结果**：

- `curl -i localhost:3000/set` → 响应头里出现 `set-cookie: name=claude`。
- `curl -i -H "Cookie: name=claude" localhost:3000/read` → 响应体 `claude`。
- 故意把 `set_cookie` 改成不返回 jar（比如返回 `()`），重新请求 `/set` 会发现响应里**没有** `set-cookie` 头——印证「jar 必须返回才生效」。

> 待本地验证：以上为示例代码，未在当前会话执行。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `CookieJar::add` 的签名是 `fn add(mut self, cookie) -> Self`（消费 self），而不是 `&mut self`？

> **答案**：builder 风格，便于在 handler 里链式写出 `jar.add(c1).add(c2)` 并直接作为返回值。同时「消费 self」也意味着返回的 jar 就是改动后的版本，避免忘记把改动带回去。

**练习 2**：响应阶段 `set_cookies` 遍历的是 `jar.delta()` 而不是全部 cookie，为什么？

> **答案**：请求里带来的「原始 cookie」不该再被 `Set-Cookie` 回显给浏览器（那会无意义地放大响应、还可能覆盖浏览器已有值）。只有本次 handler 新增/删除的「增量」才需要通知浏览器。`delta()` 正是只包含这些改动的迭代器。

---

### 4.4 扩展全家桶速览：Resource / Protobuf / 其他

除了上面三个最小模块，`axum-extra` 还有许多「即取即用」的增强。这里不逐行精读，只帮你建立索引——遇到对应需求时，知道去哪个文件、开哪个 feature。

#### 4.4.1 概念说明

- **`Resource`（约定式 CRUD 路由）**：Rails 风格的资源路由。`Resource::named("users").index(...).create(...).show(...).update(...).destroy(...)` 一气呵成地生成 `/users`、`/users/{id}`、`/users/{id}/edit` 等一整套 RESTful 路由，省去手写路径。
- **`Protobuf<T>`（提取器兼响应）**：把 `prost::Message` 类型在请求/响应里与 Protocol Buffers 二进制互转。提取时 `from_request` 把 body 缓冲后 `T::decode`；响应时 `into_response` 调 `T::encode`，默认 `Content-Type: application/octet-stream`。
- **`JsonLines`**：以「每行一个 JSON」的 [JSON Lines](https://jsonlines.org) 格式做流式提取与响应，适合日志/事件流。
- **`Cached<T>`**：把另一个提取器的结果缓存到**当前请求**的 extensions 里，避免一次请求中重复执行昂贵提取（如多次加载 Session）。
- **`ErasedJson` / `InternalServerError`**：`ErasedJson` 让你返回「类型被擦除」的 `serde_json::Value`；`InternalServerError` 提供统一的 500 响应。
- **`Attachment` / `FileStream` / `AsyncReadBody`**：文件下载（带 `Content-Disposition`）、文件流、把 `tokio::io::AsyncRead` 当响应体。

#### 4.4.2 源码指针

- **Resource**：[`axum-extra/src/routing/resource.rs:36-148`](https://github.com/tokio-rs/axum/blob/600e762b30dba5d1b9e253fabd36d0d23333e5d5/axum-extra/src/routing/resource.rs#L36-L148)。`Resource` 内部就持一个 `Router<S>`（[L36-L39](https://github.com/tokio-rs/axum/blob/600e762b30dba5d1b9e253fabd36d0d23333e5d5/axum-extra/src/routing/resource.rs#L36-L39)），各方法（`index`/`create`/`show`/`update`/`destroy`，[L56-L134](https://github.com/tokio-rs/axum/blob/600e762b30dba5d1b9e253fabd36d0d23333e5d5/axum-extra/src/routing/resource.rs#L56-L134)）本质是按约定拼路径再 `.route()`。例如 `update` 用 `on(PUT.or(PATCH), handler)` 同时接受 PUT 和 PATCH（[L109-L122](https://github.com/tokio-rs/axum/blob/600e762b30dba5d1b9e253fabd36d0d23333e5d5/axum-extra/src/routing/resource.rs#L109-L122)）。`Resource` 可以 `From` 成 `Router` 直接 merge（[L150-L154](https://github.com/tokio-rs/axum/blob/600e762b30dba5d1b9e253fabd36d0d23333e5d5/axum-extra/src/routing/resource.rs#L150-L154)）。
- **Protobuf**：[`axum-extra/src/protobuf.rs:94-142`](https://github.com/tokio-rs/axum/blob/600e762b30dba5d1b9e253fabd36d0d23333e5d5/axum-extra/src/protobuf.rs#L94-L142)。`FromRequest` 缓冲 body 再 `T::decode`（[L96-L116](https://github.com/tokio-rs/axum/blob/600e762b30dba5d1b9e253fabd36d0d23333e5d5/axum-extra/src/protobuf.rs#L96-L116)）；`IntoResponse` 用 `T::encode`（[L126-L142](https://github.com/tokio-rs/axum/blob/600e762b30dba5d1b9e253fabd36d0d23333e5d5/axum-extra/src/protobuf.rs#L126-L142)）。失败由宏生成的 `ProtobufRejection`（[L153-L162](https://github.com/tokio-rs/axum/blob/600e762b30dba5d1b9e253fabd36d0d23333e5d5/axum-extra/src/protobuf.rs#L153-L162)）描述。
- **Cached**：[`axum-extra/src/extract/cached.rs:1-75`](https://github.com/tokio-rs/axum/blob/600e762b30dba5d1b9e253fabd36d0d23333e5d5/axum-extra/src/extract/cached.rs#L1-L75)，把结果存进请求 extensions，按类型缓存（同一类型每请求只缓存一个值）。

#### 4.4.3 代码实践（源码阅读型）

**实践目标**：通过阅读测试，验证 `Resource` 生成的路径与 HTTP 方法是否符合 REST 约定。

**操作步骤**：

1. 打开 [`axum-extra/src/routing/resource.rs`](https://github.com/tokio-rs/axum/blob/600e762b30dba5d1b9e253fabd36d0d23333e5d5/axum-extra/src/routing/resource.rs) 的 `works` 测试（[L165-L214](https://github.com/tokio-rs/axum/blob/600e762b30dba5d1b9e253fabd36d0d23333e5d5/axum-extra/src/routing/resource.rs#L165-L214)）。
2. 对照断言，整理出 `Resource::named("users")` 生成的路由表。

**预期结果**（应得出类似下表）：

| 方法 | 路径 | 对应 Resource 方法 |
| --- | --- | --- |
| GET | `/users` | `index` |
| POST | `/users` | `create` |
| GET | `/users/new` | `new` |
| GET | `/users/{users_id}` | `show` |
| GET | `/users/{users_id}/edit` | `edit` |
| PUT / PATCH | `/users/{users_id}` | `update` |
| DELETE | `/users/{users_id}` | `destroy` |

> 待本地验证：可在 `axum-extra` 目录下 `cargo test --features routing works` 运行该测试。

## 5. 综合实践

把本讲的三个最小模块串起来，构建一个「类型安全的用户分页接口」：

**任务**：实现如下接口，并满足全部约束。

1. 用 `TypedPath` 定义两条类型化路由：
   - `UserCollection`：`#[typed_path("/users")]`，支持 `GET`（列表）和 `POST`（创建）。
   - `UserMember`：`#[typed_path("/users/{id}")]`，支持 `GET`（详情）。
2. 用 `typed_get` / `typed_post` 注册它们（不要手写路径字符串）。
3. 给 `UserMember` 的 `GET` 加上查询参数支持：返回一个指向「下一页」的链接，通过 `with_query_params` 构造，**确认输出的 URL 是 `/users/{id}?page=2` 形态，没有多余的 `?&`**。
4. 在 `UserMember` 的 handler 里用 `WithRejection<axum::extract::Path<UserMember>, ApiError>` 替换默认拒绝，使得 `id` 非法时返回你的统一 JSON 错误格式。
5. 用 `CookieJar` 记录「最近访问的用户 id」：每次访问详情接口都更新一个名为 `last_user` 的 cookie 并在响应里下发。

**验收清单**：

- [ ] `typed_get`/`typed_post` 注册的路径正确（`curl /users`、`curl /users/7` 都能命中）。
- [ ] `with_query_params` 产出的链接为 `/users/7?page=2`（而非 `/users/7?&page=2`）。
- [ ] `curl /users/abc` 返回你的自定义 JSON 错误（而非 axum 默认文本）。
- [ ] `curl -i /users/7` 响应头含 `set-cookie: last_user=7`。
- [ ] 全部开启的 feature：`typed-routing`、`with-rejection`、`cookie`。

> 这是综合练习，无标准答案文件；建议在本地新建一个二进制 crate 完成，并用 `curl` 逐项验证。

## 6. 本讲小结

- `axum-extra` 是 axum 的「可选增强包」，每个增强由独立 Cargo feature 控制（`typed-routing`、`with-rejection`、`cookie`、`protobuf` 等）。
- `TypedPath` 把路径模板、参数提取、链接构造三者绑定到同一类型，`RouterExt::typed_get/typed_post` 通过 `P::PATH` 注册路由，路径来自类型而非手写字符串。
- `TypedPath::with_query_params` 返回 `WithQueryParams<P,T>`，其 `Display` 实现（typed.rs L292-L311）**本次已修复**：改用 `form_urlencoded::Serializer::for_suffix(&mut out, params_start)`，正确产出 `/users?page=1&per_page=10`，不再有多余的 `?&`；当路径已含 `?` 时参数直接接在其后。
- `WithRejection<E, R>` 是 Rejection 翻译适配器：约束 `R: From<E::Rejection> + IntoResponse`，把内置拒绝改造成你项目的统一错误格式。
- `CookieJar` 同时是提取器（`Rejection = Infallible`）与 `IntoResponseParts`（写回 `Set-Cookie`），修改后**必须返回 jar** 才生效；响应阶段只回显 `delta()`。
- `Resource` 提供约定式 CRUD 路由，`Protobuf`/`JsonLines`/`Cached`/`ErasedJson`/`Attachment` 等覆盖二进制协议、流式、缓存、文件下载等场景——遇需求先查 axum-extra。

## 7. 下一步学习建议

- **学测试（u11-l1）**：用 `TestClient` / `oneshot` 为本讲的类型化路由和 cookie 接口写自动化测试，把「手动 curl 验收」固化成回归测试。
- **学 WebSocket 与 ConnectInfo（u11-l2）**：`CookieJar` 常与会话结合，理解连接级信息（`ConnectInfo`）如何与 cookie/会话协同。
- **综合实战（u11-l3）**：把本讲的类型化路由、`WithRejection` 统一错误、`CookieJar` 会话整合进一个完整的 REST API（参考 `examples/todos`、`examples/jwt`）。
- **回看派生宏（u10-l2）**：如果想定制 `TypedPath` 的生成（例如自定义 rejection、字段与捕获的映射规则），深入 `axum-macros/src/typed_path.rs`。
- **继续阅读源码**：把 [`axum-extra/src/routing/typed.rs`](https://github.com/tokio-rs/axum/blob/600e762b30dba5d1b9e253fabd36d0d23333e5d5/axum-extra/src/routing/typed.rs) 的 `tests` 模块全部跑一遍，作为本次「`?&` 修复」的回归基线。
