# FromRequestParts 与 FromRequest 双 trait 机制

## 1. 本讲目标

本讲是整个「提取器」单元（u3）的地基。读完本讲，你应当能够：

- 说清 `FromRequestParts` 和 `FromRequest` 这两个 trait 各自能拿到请求的哪一部分，以及为什么 axum 要把它们拆成两个。
- 读懂 `tuple.rs` 中宏为元组生成的两套实现，并能画出多个提取器的调用顺序图。
- 解释「消费请求体的提取器必须是 handler 最后一个参数」这条铁律在类型层面是如何被强制的——为什么把 `Json` 写在 `Path` 之前会直接编译失败。
- 理解 `Rejection` 为什么必须实现 `IntoResponse`，以及 `Result<T, T::Rejection>` 这种写法背后的 blanket impl。

本讲承接 [u1-l4](u1-l4-handler-router-intro.md)：在那里我们已经知道 handler 是一个 `async fn`，由 `Handler` 的 blanket impl 适配成 `tower::Service`，并且「依次 await 各提取器」。本讲就打开「依次 await」这个黑盒，看 axum 到底是如何把一个 `http::Request` 拆成多个函数参数的。

## 2. 前置知识

本讲会用到以下几个概念，先用最朴素的语言过一遍：

- **`http::Request` 的两段式结构**：`http` crate 把一个请求拆成 `Parts`（方法、URI、版本、headers、extensions 等元数据）和 `Body`（请求体）。`Parts` 是廉价的、可 `Clone` 的；`Body` 则是一个**只能被消费一次**的异步字节流。`Request::into_parts()` 把它俩拆开，`Request::from_parts(parts, body)` 再拼回去。
- **提取器（extractor）**：任何实现了 `FromRequest` 或 `FromRequestParts` 的类型。它在 handler 被调用前，由框架从请求里「提取」出一个值，作为 handler 的某个参数。`Path`、`Query`、`Json`、`State` 都是提取器。
- **`IntoResponse`**：把一个 Rust 值变成 HTTP 响应的 trait。handler 的返回值必须实现它（详见 [u4-l1](u4-l1-into-response.md)）。
- **`Rejection`**：提取失败时返回的「错误」。它的关键约束是必须能转成响应（`IntoResponse`），这样框架才能在 handler 根本没被调用的情况下，就把错误响应写回给客户端。
- **`Send` 约束与 `impl Future + Send`**：axum 把每条连接 spawn 成独立的 tokio 任务，因此提取过程返回的 Future 必须是 `Send` 的（可以在线程间转移）。
- **trait coherence（一致性规则）**：Rust 规定同一类型对同一 trait 只能有一个实现，否则编译器拒绝。本讲会看到 axum 用一个「标记类型 `M`」绕开这条规则，让 `FromRequestParts` 的类型也能被当作 `FromRequest` 用。

如果你对 `http::Request` 的 `Parts`/`Body` 拆分还不熟，记住下面这张对照表就够用了：

| 部分 | 内容 | 能否多次读 | 对应的 axum trait |
| --- | --- | --- | --- |
| `Parts` | method / uri / headers / extensions / 版本 | 可以（多为 `clone`） | `FromRequestParts` |
| `Body` | 请求体字节流 | **只能消费一次** | `FromRequest` |

## 3. 本讲源码地图

本讲涉及的关键文件都在 `axum-core` 里（核心 trait 必须放在最底层、依赖最少的 crate，这一点在 [u1-l3](u1-l3-workspace-and-features.md) 讲过）：

| 文件 | 作用 |
| --- | --- |
| [axum-core/src/extract/mod.rs](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum-core/src/extract/mod.rs) | 定义 `FromRequestParts`、`FromRequest` 两个核心 trait，以及连接两者的 `ViaParts` blanket impl 和 `Result` 提取器的 blanket impl。 |
| [axum-core/src/extract/tuple.rs](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum-core/src/extract/tuple.rs) | 用 `impl_from_request!` 宏为 1~16 元组生成提取实现，是理解「多提取器依次调用」的最佳样本。 |
| [axum-core/src/extract/request_parts.rs](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum-core/src/extract/request_parts.rs) | 为内置类型（`Method`/`Uri`/`HeaderMap`/`Bytes`/`String`/`Body` 等）实现这两个 trait，是「只读 parts」与「消费 body」两类提取器的真实范例。 |
| [axum-core/src/extract/rejection.rs](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum-core/src/extract/rejection.rs) | 用 `__define_rejection!` / `__composite_rejection!` 宏生成 Rejection 类型，演示 Rejection 如何实现 `IntoResponse`。 |
| [axum-core/src/macros.rs](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum-core/src/macros.rs) | 定义 `all_the_tuples!`、`__define_rejection!`、`__composite_rejection!` 等代码生成宏。 |
| [axum/src/handler/mod.rs](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/handler/mod.rs) | `Handler` trait 与 `impl_handler!` 宏——提取器机制在 handler 层的消费者，把「依次提取」真正接到 `async fn` 上。 |
| [axum/src/docs/extract.md](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/docs/extract.md) | axum 官方「提取器」文档，明确写了「提取器按参数从左到右执行」「消费 body 的提取器必须放最后」。 |

## 4. 核心概念与源码讲解

### 4.1 从「函数参数」到「提取器」：请求被一分为二

#### 4.1.1 概念说明

当你在 axum 里写下：

```rust
async fn handler(method: Method, headers: HeaderMap, body: String) { /* ... */ }
```

框架需要在这三个参数被传入函数之前，分别从请求里把它们「变」出来。这个「变」的过程就叫**提取（extraction）**。

关键观察是：前两个参数（`Method`、`HeaderMap`）只需要读请求的**元数据**，根本不碰请求体；而第三个参数（`String`）必须把整个请求体读出来、拼成字节、再校验是合法 UTF-8。这两类操作有本质区别：

- 读元数据可以重复做、顺序无所谓（`Method` 可以 clone、`HeaderMap` 可以 clone）。
- 读请求体是一次性消费——字节流被读走后就没了。

axum 把这个区别直接编码进类型系统，于是有了两个 trait 而不是一个：**只读 parts 的 `FromRequestParts`** 与 **消费 body 的 `FromRequest`**。这种「一个 trait 对应一种能力」的设计，是后续所有规则的根。

#### 4.1.2 核心流程

一个 handler 被调用前，框架大致经历这样的流程：

```text
http::Request 到达
        │
        ▼
Request::into_parts()  ──►  (Parts, Body)
        │                       │
        │  FromRequestParts 提取器们（可多次、可任意顺序）
        │  依次 await：Method → HeaderMap → State → Path → Query ...
        │  每个都只借 &mut Parts
        │
        ▼  剩下的 (Parts, Body) 重新拼成 Request
FromRequest 提取器（最多一个，必须是最后一个参数）
        │  以 by-value 的方式拿走整个 Request（含 body）
        ▼
得到所有参数 ──► 调用 async fn
```

注意中间那步「重新拼成 Request」：parts 在前几个提取器之间是共享借用的（`&mut`），等轮到消费 body 的提取器时，框架会把 `Parts` 和 `Body` 拼回完整的 `Request` 再交出去。这正是 4.3 节元组实现里会看到的 `Request::from_parts(parts, body)`。

### 4.2 FromRequestParts：只读 parts 的提取器

#### 4.2.1 概念说明

`FromRequestParts<S>` 表示「我能从一个请求的 `Parts`（不含 body）里提取出自己」。凡是只需要 method、uri、headers、extensions 的提取器都走这条路：`Method`、`Uri`、`HeaderMap`、`Extensions`、`Path`、`Query`、`State`……因为不碰 body，这类提取器**可以放任意位置、可以有好几个、顺序不影响正确性**（只影响执行先后）。

#### 4.2.2 核心流程

trait 定义只有两样东西：一个关联类型 `Rejection`（失败时返回的错误），一个异步方法 `from_request_parts`。

```text
from_request_parts(parts: &mut Parts, state: &S)
        │
        ▼  从 parts 里读/写所需信息
   Result<Self, Self::Rejection>
```

注意两个细节：

1. 参数是 `&mut Parts`（可变借用）。之所以给 `mut`，是因为有些提取器会**往 parts 里写东西**——例如路径匹配会把 `UrlParams` 写进 `parts.extensions` 供 `Path` 提取器读取（详见 [u2-l2](u2-l2-path-matching-params.md)）。
2. 第二个参数 `state: &S` 是应用状态的引用。`State` 提取器正是从这里取出共享状态（详见 [u3-l5](u3-l5-state-fromref.md)）。

#### 4.2.3 源码精读

trait 的定义在 [axum-core/src/extract/mod.rs:L53-L63](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum-core/src/extract/mod.rs#L53-L63)：

```rust
pub trait FromRequestParts<S>: Sized {
    type Rejection: IntoResponse;

    fn from_request_parts(
        parts: &mut Parts,
        state: &S,
    ) -> impl Future<Output = Result<Self, Self::Rejection>> + Send;
}
```

上面这段代码做了三件事：声明 `Rejection` 必须实现 `IntoResponse`（保证失败时能变成响应）；签名里用 `&mut Parts` 而非 by-value；返回 `impl Future + Send`（提取是异步的，且 Future 跨线程安全）。

> 一个常被忽略的点：方法返回的是 `impl Future` 而非 `async fn`。这是 axum-core 的刻意选择——用 `-> impl Future` 可以让实现者按需选择 `async` 块或手写 Future，同时把 `+ Send` 直接写进 trait 契约。

最直观的真实实现是内置类型那一批，全部在 [axum-core/src/extract/request_parts.rs](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum-core/src/extract/request_parts.rs) 里。以 `Method` 为例（[L19-L28](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum-core/src/extract/request_parts.rs#L19-L28)）：

```rust
impl<S> FromRequestParts<S> for Method
where S: Send + Sync,
{
    type Rejection = Infallible;

    async fn from_request_parts(parts: &mut Parts, _: &S) -> Result<Self, Self::Rejection> {
        Ok(parts.method.clone())
    }
}
```

`Method` 的 `Rejection` 是 `Infallible`——它永远不可能失败（每个请求都有方法）。`Uri`（[L30-L39](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum-core/src/extract/request_parts.rs#L30-L39)）、`HeaderMap`（[L57-L66](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum-core/src/extract/request_parts.rs#L57-L66)）、`Extensions`（[L153-L162](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum-core/src/extract/request_parts.rs#L153-L162)）都是同一个套路：从 `parts` 里 `clone` 一份出来。这些就是「只读 parts」提取器的标准写法。

#### 4.2.4 代码实践

这是一个**源码阅读型实践**，目标是建立「哪些类型走 FromRequestParts」的直觉。

1. **实践目标**：能一眼判断一个内置类型是「只读 parts」还是「消费 body」。
2. **操作步骤**：打开 [axum-core/src/extract/request_parts.rs](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum-core/src/extract/request_parts.rs)，用编辑器搜索 `impl<S> FromRequestParts`，列出所有命中的类型。
3. **需要观察的现象**：`Method`、`Uri`、`Version`、`HeaderMap`、`Parts`、`Extensions` 命中 `FromRequestParts`；而 `Bytes`、`String`、`BytesMut`、`Body`、`Request` 命中的是 `FromRequest`。
4. **预期结果**：你会得到一张清晰的「两类提取器」清单——凡是命中 `FromRequestParts` 的，都能在 handler 里任意排位；凡是命中 `FromRequest` 的，都只能放最后。
5. 待本地验证：如果你想确认结论，可以在一个示例项目里写 `async fn h(m: Method, b: String)`（合法）与 `async fn h2(b: String, m: Method)`（编译失败），亲手感受这条约束。

#### 4.2.5 小练习与答案

**练习 1**：`HeaderMap` 的 `Rejection` 是 `Infallible`，但 `Path` 的 `Rejection` 是 `PathRejection`（不是 `Infallible`）。为什么两者不同？

**参考答案**：`HeaderMap` 只是把已有数据 clone 出来，请求不可能「没有 headers」，所以不会失败。而 `Path` 要把路径参数反序列化成目标类型（如 `Path<u32>`），客户端传了非数字、或类型不匹配都会失败，因此需要一个真正的错误类型。

**练习 2**：`from_request_parts` 拿到的是 `&mut Parts` 而不是 `&Parts`。请举一个「提取器需要往 parts 里写数据」的真实场景。

**参考答案**：路径匹配阶段，axum 把解析出的 `UrlParams` 写进 `parts.extensions`，随后 `Path` 提取器再从 extensions 里把它读出来做反序列化（见 [u2-l2](u2-l2-path-matching-params.md)）。没有 `&mut`，这条「先写后读」的链路就无法成立。

### 4.3 FromRequest：消费 body 的提取器 与 ViaParts 桥梁

#### 4.3.1 概念说明

`FromRequest<S, M>` 表示「我需要整个请求（含 body）才能提取出自己」。它的签名以 by-value 接收 `Request`——这意味着一旦某个提取器实现了 `FromRequest`，它就**独占**了请求体，别人再也读不到。`Bytes`、`String`、`Json`、`Form`、`Body`、`Request` 本体都走这条路。

这里有个看起来奇怪的设计：`FromRequest` 有**两个**类型参数 `<S, M = private::ViaRequest>`，第二个 `M` 是个默认值标记类型。它存在的唯一目的，是绕开 Rust 的 trait coherence 规则，让「实现了 `FromRequestParts` 的类型，自动也算实现了 `FromRequest`」这件事成为可能。这条「自动也算」的桥梁，是整个提取器体系能优雅运转的关键，下一小节专门讲。

#### 4.3.2 核心流程

```text
from_request(req: Request, state: &S)   // req 是 by-value，含 body
        │
        ▼  消费 body（缓冲、反序列化、校验...）
   Result<Self, Self::Rejection>
```

因为 `req` 是 by-value，调用 `from_request` 之后原 `Request` 就不存在了——这正是「只能消费一次」在类型上的体现。

#### 4.3.3 源码精读

trait 定义在 [axum-core/src/extract/mod.rs:L79-L89](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum-core/src/extract/mod.rs#L79-L89)：

```rust
pub trait FromRequest<S, M = private::ViaRequest>: Sized {
    type Rejection: IntoResponse;

    fn from_request(
        req: Request,
        state: &S,
    ) -> impl Future<Output = Result<Self, Self::Rejection>> + Send;
}
```

与 `FromRequestParts` 对照，差异只有两处：方法签名是 `req: Request`（by-value、含 body）；多了一个标记参数 `M`。

真实实现可以看 `Bytes`（[axum-core/src/extract/request_parts.rs:L100-L116](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum-core/src/extract/request_parts.rs#L100-L116)）：

```rust
impl<S> FromRequest<S> for Bytes
where S: Send + Sync,
{
    type Rejection = BytesRejection;

    async fn from_request(req: Request, _: &S) -> Result<Self, Self::Rejection> {
        let bytes = req
            .into_limited_body()               // 取出带大小限制的 body
            .collect().await                   // 把整个流缓冲成连续字节
            .map_err(FailedToBufferBody::from_err)?
            .to_bytes();
        Ok(bytes)
    }
}
```

注意 `.into_limited_body()` 与默认 2MB 限制的关系——这背后是 `DefaultBodyLimit`（[u3-l4](u3-l4-json-extractor.md) 会细讲）。`String`（[L118-L138](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum-core/src/extract/request_parts.rs#L118-L138)）则复用 `Bytes::from_request` 再做 UTF-8 校验，`Rejection` 因此多了个 `InvalidUtf8` 变体。还有一个极端例子是 `Body` 本体（[L164-L173](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum-core/src/extract/request_parts.rs#L164-L173)）：它直接 `Ok(req.into_body())` 把流原样交出，不做任何缓冲——这就是「最大自由度」的 body 提取器。

现在解释那个神秘的标记 `M`。看 [axum-core/src/extract/mod.rs:L91-L105](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum-core/src/extract/mod.rs#L91-L105) 的 blanket impl：

```rust
impl<S, T> FromRequest<S, private::ViaParts> for T
where
    S: Send + Sync,
    T: FromRequestParts<S>,
{
    type Rejection = <Self as FromRequestParts<S>>::Rejection;

    fn from_request(req: Request, state: &S) -> impl Future<Output = Result<Self, Self::Rejection>> {
        let (mut parts, _) = req.into_parts();   // 注意：body 被丢弃
        async move { Self::from_request_parts(&mut parts, state).await }
    }
}
```

这段代码说的是：**任何实现了 `FromRequestParts` 的类型 `T`，都自动实现 `FromRequest<S, ViaParts>`**，实现方式就是把请求拆开、丢掉 body、只调用 `from_request_parts`。两个标记类型 `ViaParts` / `ViaRequest` 定义在 [mod.rs:L31-L37](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum-core/src/extract/mod.rs#L31-L37)，它们是空的枚举（zero-sized），纯粹用于在类型层面区分「这个 `FromRequest` 是真的要读 body，还是只是 `FromRequestParts` 的别名」。

为什么要绕这一圈？因为如果没有 `M`，axum 想同时提供：

- `impl FromRequest for T where T: FromRequestParts`（让 `Method` 也能当「最后一个参数」用）；
- `impl FromRequest for Bytes`（真正消费 body 的实现）。

对同一个 `T`，这两条 impl 在编译器眼里会冲突（coherence 冲突）。引入标记 `M` 后，前者实现的是 `FromRequest<S, ViaParts>`，后者实现的是 `FromRequest<S, ViaRequest>`（即默认的 `FromRequest<S>`），二者是不同的 trait 参数，冲突消失。这就是 4.4 节「`Path` 为何能当最后一个参数」的底层原因。

#### 4.3.4 代码实践

1. **实践目标**：验证「消费 body 的提取器只能放最后」这条规则，并理解 `ViaParts` 桥梁的作用。
2. **操作步骤**：
   - 在一个依赖 axum 的项目里写两个 handler：
     ```rust
     // 合法：String（FromRequest）放最后
     async fn ok(m: Method, b: String) {}
     ```
   - 再试着把 `String` 提到前面：
     ```rust
     // 非法：String 必须是最后一个参数
     async fn bad(b: String, m: Method) {}
     ```
   - 分别用 `axum::routing::get(ok)` / `get(bad)` 注册，观察后者是否编译失败。
3. **需要观察的现象**：`bad` 会触发编译错误，且错误信息会指向 `$last: FromRequest<S, M>` 与 `$ty: FromRequestParts<S>` 的约束不满足。
4. **预期结果**：`ok` 编译通过；`bad` 编译失败。错误根因见下一节 4.4 的约束展开。
5. 待本地验证：不同 rustc 版本给出的错误措辞可能不同；若想看更友好的错误，可在 handler 上加 `#[axum::debug_handler]`（见 [u10-l1](u10-l1-debug-handler-macro.md)）。

#### 4.3.5 小练习与答案

**练习 1**：`Request`（整个请求）同时实现了 `FromRequest` 和 `FromRequestParts` 吗？分别在哪？

**参考答案**：`Request` 实现了 `FromRequest`（[request_parts.rs:L8-L17](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum-core/src/extract/request_parts.rs#L8-L17)，直接 `Ok(req)`，`Rejection = Infallible`）。而 `http::request::Parts`（不是 `Request`）实现了 `FromRequestParts`（[L141-L150](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum-core/src/extract/request_parts.rs#L141-L150)）。两者是不同类型，所以并不冲突。

**练习 2**：`ViaParts` blanket impl 里有一行 `let (mut parts, _) = req.into_parts();`，下划线丢掉的是什么？这意味着什么？

**参考答案**：丢掉的是 `Body`。这意味着当一个「只读 parts」的提取器被放在 handler 最后一个参数位置时（经由 `ViaParts` 桥梁），请求体会被**静默丢弃**——因为这个提取器根本不需要 body。这也是为什么 `async fn h(_: Method)` 合法：`Method` 经 `ViaParts` 实现 `FromRequest`，body 被丢弃但没人需要它。

### 4.4 tuple impl：handler 多参数如何依次提取

#### 4.4.1 概念说明

前面两节讲的是「单个提取器如何提取」。但 handler 通常有多个参数，框架需要按从左到右的顺序把它们一个个提取出来。axum 用一个宏 `impl_from_request!` 为 1~16 元组各生成一份实现，把「多提取器依次调用」的逻辑写得清清楚楚。这一节是本讲的重心，也是官方文档「[The order of extractors](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/docs/extract.md#L120-L153)」那段话的源码依据。

需要先厘清一个容易混淆的点：handler 里多个参数的提取，**并不是**通过「元组的 `FromRequest` impl」完成的，而是通过 [axum/src/handler/mod.rs](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/handler/mod.rs) 里 `impl_handler!` 宏为每个参数单独调用提取器完成的。但**两者的逻辑结构完全一致**（都是「前面几个走 `from_request_parts`，最后一个走 `from_request`」）。元组 impl 是更自包含、更易读的版本，所以我们用它做样本，最后再点出 handler 宏如何复刻同一套逻辑。

#### 4.4.2 核心流程

`impl_from_request!` 宏为每个元组长度生成**两套**实现：

```text
实现 A：impl FromRequestParts<S> for (T1,...,Tn)
        要求：所有 Ti 都是 FromRequestParts
        用途：让「元组」本身能作为一个非末尾的提取器（嵌套元组）

实现 B：impl FromRequest<S> for (T1,...,Tn)
        要求：T1..T(n-1) 是 FromRequestParts；Tn 是 FromRequest
        用途：让「元组」作为一个消费 body 的提取器（末位）
```

实现 B 的提取流程（也是 handler 多参数的标准流程）：

```text
req.into_parts()  ──►  (parts, body)
        │
        │  for T1..T(n-1):  T_i::from_request_parts(&mut parts, state).await?
        │                    （共享借用 parts，失败则 into_response 后提前返回）
        │
        ▼  Request::from_parts(parts, body)   把剩下的 parts 与 body 拼回完整请求
Tn::from_request(req, state).await?           （独占消费 body）
        │
        ▼
Ok((T1, ..., Tn))
```

两阶段的关键：前 n-1 个提取器共享 `&mut parts`，互不干扰；只有最后一个拿到完整 `Request` 并消费 body。这正好对应「消费 body 的提取器只能有一个、且必须是最后一个」。

#### 4.4.3 源码精读

宏定义在 [axum-core/src/extract/tuple.rs:L18-L75](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum-core/src/extract/tuple.rs#L18-L75)。先看实现 B（`FromRequest` for 元组），这是最关键的一段（[L50-L73](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum-core/src/extract/tuple.rs#L50-L73)）：

```rust
impl<S, $($ty,)* $last> FromRequest<S> for ($($ty,)* $last,)
where
    $( $ty: FromRequestParts<S> + Send, )*     // 前面的：只能 FromRequestParts
    $last: FromRequest<S> + Send,              // 最后一个：必须 FromRequest
    S: Send + Sync,
{
    type Rejection = Response;

    fn from_request(req: Request, state: &S) -> impl Future<Output = Result<Self, Self::Rejection>> {
        let (mut parts, body) = req.into_parts();
        async move {
            $(
                let $ty = $ty::from_request_parts(&mut parts, state).await
                    .map_err(|err| err.into_response())?;
            )*
            let req = Request::from_parts(parts, body);          // 拼回完整请求
            let $last = $last::from_request(req, state).await    // 最后一个消费 body
                .map_err(|err| err.into_response())?;
            Ok(($($ty,)* $last,))
        }
    }
}
```

读这段宏时把它想象成「对每个非末位参数展开一次 `let $ty = ...`」。注意三个细节：

1. **where 子句就是铁律本身**：`$ty: FromRequestParts<S>` 和 `$last: FromRequest<S>`。这条约束直接回答了本讲标题里的问题——为什么 `Json` 不能放在 `Path` 之前。
2. **`type Rejection = Response`**：元组里每个提取器的 `Rejection` 类型可能不同（`Method` 是 `Infallible`，`Path` 是 `PathRejection`，`Json` 是 `JsonRejection`……），没法用一个统一的关联类型表达。所以每个失败都被 `.map_err(|err| err.into_response())` 归一化成 `Response`，元组整体的 `Rejection` 就是 `Response`。
3. **`Request::from_parts(parts, body)`**：前 n-1 个提取器用完 parts 后，把 parts 和未被触碰的 body 拼回完整请求交给最后一个。这就是「body 在最后才被消费」的物证。

宏通过 [tuple.rs:L77](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum-core/src/extract/tuple.rs#L77) 的 `all_the_tuples!(impl_from_request);` 对 1~16 元组各展开一次。`all_the_tuples!` 定义在 [axum-core/src/macros.rs:L235-L254](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum-core/src/macros.rs#L235-L254)，它把 `[$($ty),*], $last` 这个模式从 `[], T1` 一直铺到 `[T1..T15], T16`。所以最大 16 个参数的 handler 都被覆盖。

现在回答本讲的核心问题：**为什么把 `Json` 放在 `Path` 之前会编译失败？**

考虑 `async fn handler(json: Json<T>, id: Path<u32>)`。`impl_handler!` 宏（[axum/src/handler/mod.rs:L221-L260](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/handler/mod.rs#L221-L260)）会按「最后一个参数是 `$last`，其余是 `$ty`」展开 where 子句：

- `id: Path<u32>` 是 `$last`，要求 `Path<u32>: FromRequest<S, M>`。`Path` 实现了 `FromRequestParts`，再经 4.3 节的 `ViaParts` 桥梁自动获得 `FromRequest<S, ViaParts>`，**满足**。
- `json: Json<T>` 是 `$ty`（非末位），要求 `Json<T>: FromRequestParts<S>`。但 `Json` **只实现了 `FromRequest`，没有实现 `FromRequestParts`**（因为它必须消费 body），**不满足** → 编译失败。

换句话说，编译失败不是「运行时可能出问题」的警告，而是 where 子句里 `Json<T>: FromRequestParts<S>` 找不到 impl 的硬性类型错误。axum 用类型系统把「body 提取器不能放非末位」这条规则**编译期强制**了。

补充一句关于 handler 宏与元组宏的关系。`impl_handler!`（[L238-L257](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/handler/mod.rs#L238-L257)）里的 `call` 方法把上面这套两阶段逻辑逐参数展开：

```rust
fn call(self, req: Request, state: S) -> Self::Future {
    let (mut parts, body) = req.into_parts();
    Box::pin(async move {
        $( let $ty = match $ty::from_request_parts(&mut parts, &state).await {
               Ok(v) => v,
               Err(rejection) => return rejection.into_response(),   // 失败提前返回
           }; )*
        let req = Request::from_parts(parts, body);
        let $last = match $last::from_request(req, &state).await {
            Ok(v) => v,
            Err(rejection) => return rejection.into_response(),
        };
        self($($ty,)* $last,).await.into_response()                  // 真正调用 async fn
    })
}
```

可以看到它和元组实现 B 几乎逐行对应，差别只是失败时 `return rejection.into_response()`（直接把错误响应写回），成功后 `self(...)` 真正调用你的 handler 函数。元组 impl 的存在，主要是为了让「一个元组参数」本身也能作为一个提取器（见 tuple.rs 里 [nested_tuple 测试](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum-core/src/extract/tuple.rs#L114-L118)）。

官方文档把这一切总结成一句话，见 [axum/src/docs/extract.md:L192-L193](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/docs/extract.md#L192-L193)：「axum enforces this by requiring the last extractor implements `FromRequest` and all others implement `FromRequestParts`.」

#### 4.4.4 代码实践（对应本讲指定的实践任务）

这是本讲的主实践，目标是把宏展开后的调用顺序画出来，并亲手验证「Json 放 Path 前」会编译失败。

1. **实践目标**：读懂宏展开，画出 3 个提取器的调用顺序图；解释 `Json` 写在 `Path` 之前的编译失败原因。
2. **操作步骤**：
   - 打开 [axum-core/src/extract/tuple.rs:L50-L73](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum-core/src/extract/tuple.rs#L50-L73)，把宏参数 `[$($ty),*], $last` 想象成具体代入 `($ty = T1, T2; $last = T3)`，手动把宏展开成普通 Rust 代码。
   - 假设 handler 是 `async fn h(m: Method, headers: HeaderMap, body: String)`，标注 `Method`/`HeaderMap` 走哪一行、`String` 走哪一行、`Request::from_parts` 在哪一行。
   - 写一个故意写反的小程序验证：
     ```rust
     use axum::{Json, extract::Path, routing::post, Router};
     use serde::Deserialize;
     #[derive(Deserialize)] struct P { }
     // 这一行会编译失败
     async fn bad(Json(_b): Json<P>, Path(_id): Path<u32>) {}
     fn main() { let _: Router = Router::new().route("/", post(bad)); }
     ```
   - `cargo build`，记录编译错误信息。
3. **需要观察的现象**：
   - 三提取器调用顺序图为（以 `h(m, headers, body)` 为例）：
     ```text
     req.into_parts() ─► (parts, body)
            │
            ├─ Method::from_request_parts(&mut parts, state)   // 宏里第 1 个 $ty
            ├─ HeaderMap::from_request_parts(&mut parts, state) // 宏里第 2 个 $ty
            │       （二者共享同一个 &mut parts）
            ▼
     Request::from_parts(parts, body)                           // 宏中拼接那行
            │
            └─ String::from_request(req, state)                 // 宏里 $last，消费 body
            ▼
     Ok((Method, HeaderMap, String)) → 调用 h(...)
     ```
   - `bad` 的编译错误会提到 `Json<P>` 不满足 `FromRequestParts<_>`（或类似措辞）。
4. **预期结果**：调用顺序图如上；`bad` 无法编译，根因是 `Json<P>: FromRequestParts<S>` 无 impl（见 4.4.3 的约束分析）。
5. 待本地验证：编译错误的具体措辞依赖 rustc 与 axum 版本；上面是基于当前 HEAD 源码的预期。

#### 4.4.5 小练习与答案

**练习 1**：元组的 `FromRequest` impl 里 `type Rejection = Response;`，而不是某个具体的 `XxxRejection`。为什么？

**参考答案**：元组里每个成员的 `Rejection` 类型不同（`Infallible`、`PathRejection`、`JsonRejection`……），无法选一个统一的具体类型。于是每个成员失败时都 `.map_err(|err| err.into_response())` 把自己的 Rejection 转成 `Response`，元组整体就以 `Response` 作为 Rejection。这也呼应了「Rejection 必须实现 `IntoResponse`」的设计目的——任何 Rejection 都能统一坍缩成响应。

**练习 2**：handler 最多能有多少个参数？为什么是这个数？

**参考答案**：16 个。因为 `all_the_tuples!`（[macros.rs:L235-L254](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum-core/src/macros.rs#L235-L254)）只展开到 16 元组，`impl_handler!` 经由同一个宏覆盖 1~16 个参数（加上 [handler/mod.rs:L208-L219](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/handler/mod.rs#L208-L219) 的零参数 impl，合计 0~16）。这是宏展开上限的工程选择，而非语言限制。

**练习 3**：`async fn h((a, b): (Method, HeaderMap), body: String)` 合法吗？它用的是哪一套 impl？

**参考答案**：合法。`(Method, HeaderMap)` 是一个**非末位**参数，它作为整体实现 `FromRequestParts`（元组实现 A，[tuple.rs:L24-L44](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum-core/src/extract/tuple.rs#L24-L44)，要求所有成员都是 `FromRequestParts`，`Method`/`HeaderMap` 都满足）。`String` 仍是末位的 `FromRequest`。这就是「嵌套元组」提取。

### 4.5 Rejection 必须实现 IntoResponse 的设计

#### 4.5.1 概念说明

两个 trait 都把 `Rejection` 约束为 `type Rejection: IntoResponse;`。这不是随手加的约束，而是整个错误模型的核心：**当某个提取器失败时，handler 根本不会被调用**，框架必须能直接把失败变成一个 HTTP 响应写回客户端。让 Rejection 自己实现 `IntoResponse`，就把「失败 → 响应」这件事完全交给提取器自己决定（返回什么状态码、什么 body 文本），框架只负责调用它。

#### 4.5.2 核心流程

```text
提取器 T::from_request_parts(...).await
        │
        ├─ Ok(value)  ──►  继续下一个提取器 / 调用 handler
        └─ Err(rejection)
                │
                ▼  rejection.into_response()   // Rejection: IntoResponse
              提前返回该响应，handler 不执行
```

这条流程在 4.4 节的 `impl_handler!` 里就是那两处 `Err(rejection) => return rejection.into_response()`。

#### 4.5.3 源码精读

约束声明在 trait 定义里：`FromRequestParts` 的 [mod.rs:L56](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum-core/src/extract/mod.rs#L56) 与 `FromRequest` 的 [mod.rs:L82](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum-core/src/extract/mod.rs#L82)，都是同一行 `type Rejection: IntoResponse;`。

那么具体的 Rejection 类型是怎么「自动」实现 `IntoResponse` 的？答案是两个代码生成宏，定义在 [axum-core/src/macros.rs](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum-core/src/macros.rs)：

- `__define_rejection!`（[L38-L149](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum-core/src/macros.rs#L38-L149)）：生成一个带 `#[status = ...]` 和 `#[body = ...]` 的结构体，并自动为它实现 `IntoResponse`（把状态码与 body 文本组成响应）、`Display`、`std::error::Error`。
- `__composite_rejection!`（[L154-L232](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum-core/src/macros.rs#L154-L232)）：把多个 rejection 组合成一个枚举，`IntoResponse` 实现里 `match` 每个变体委托给内层的 `into_response`。

以 `StringRejection` 为例，它在 [axum-core/src/extract/rejection.rs:L75-L83](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum-core/src/extract/rejection.rs#L75-L83) 由 `composite_rejection!` 生成，组合了 `FailedToBufferBody` 与 `InvalidUtf8` 两种失败。每个变体自身又由 `define_rejection!` 生成并自带 `IntoResponse`（例如 `InvalidUtf8` 在 [rejection.rs:L57-L63](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum-core/src/extract/rejection.rs#L57-L63)，状态码 `BAD_REQUEST`，body `"Request body didn't contain valid UTF-8"`）。这样一层套一层，最终保证「任何 Rejection 都能变成响应」。

还有一组与 Rejection 紧密相关的 blanket impl：`Result<T, T::Rejection>` 自身也能作为提取器，见 [mod.rs:L107-L129](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum-core/src/extract/mod.rs#L107-L129)：

```rust
impl<S, T> FromRequestParts<S> for Result<T, T::Rejection>
where T: FromRequestParts<S>, S: Send + Sync,
{
    type Rejection = Infallible;   // 注意：框架层面永不失败
    async fn from_request_parts(parts: &mut Parts, state: &S) -> Result<Self, Self::Rejection> {
        Ok(T::from_request_parts(parts, state).await)   // 把内层 Ok/Err 包进 Ok
    }
}
```

这段代码实现了官方文档「[Handling extractor rejections](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/docs/extract.md#L196-L236)」的能力：当你写 `Result<Json<T>, JsonRejection>` 作参数时，框架层面的 Rejection 是 `Infallible`（永远成功），内层的 `Ok`/`Err` 被原样塞进 `Ok(...)` 交给你，于是你可以在 handler 里 `match` 它，自己决定如何响应，而不会被框架直接短路。

#### 4.5.4 代码实践

1. **实践目标**：观察「提取失败时 handler 不执行、Rejection 自动变响应」的真实行为。
2. **操作步骤**：参考 [axum-core/src/extract/request_parts.rs:L175-L198](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum-core/src/extract/request_parts.rs#L175-L198) 里 `extract_request_parts` 测试的写法，用 `axum::test_helpers::TestClient` 起一个最小服务，handler 签名为 `async fn h(body: String)`。
   - 发送一个合法 UTF-8 body，预期 200。
   - 发送一个非法 UTF-8 body（raw bytes），预期 400（`InvalidUtf8` 的状态码）。
3. **需要观察的现象**：第二种请求返回 400，且 handler 体不会被执行（可在 handler 第一行加一句 `println!`，会看到它不打印）。
4. **预期结果**：400 响应由 `StringRejection::InvalidUtf8` 经 `into_response()` 自动生成，对应 [rejection.rs:L57-L63](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum-core/src/extract/rejection.rs#L57-L63) 的 `BAD_REQUEST` 与 body 文本。
5. 待本地验证：具体测试基础设施（`TestClient`）在 `axum` crate 的 `test_helpers` 模块下，需启用相应 feature（见 [u11-l1](u11-l1-testing.md)）。

#### 4.5.5 小练习与答案

**练习 1**：为什么把 `Rejection` 约束为 `IntoResponse`，而不是让所有 Rejection 都实现 `std::error::Error` 然后由框架统一格式化？

**参考答案**：让 Rejection 自己实现 `IntoResponse`，是为了把「失败时返回什么响应」的决定权交给提取器。`JsonRejection::MissingJsonContentType` 想返回 415（Unsupported Media Type），`InvalidUtf8` 想返回 400，`LengthLimitError` 想返回 413——这些状态码差异是业务语义的一部分，只有提取器自己知道最合适。如果统一用 `Error`，就丢失了这种细粒度控制。

**练习 2**：`Result<T, T::Rejection>` 作为提取器时，它自身的 `Rejection` 是 `Infallible`。这说明什么？

**参考答案**：说明「框架不会因为内层提取失败而短路」。内层的成功/失败被包成 `Ok(Ok(v))` 或 `Ok(Err(rejection))` 交给 handler，由 handler 自己 `match`。换句话说，`Result<T, _>` 是一种「我要在 handler 内部处理提取失败」的信号，对应文档里的「Handling extractor rejections」用法。

## 5. 综合实践

把本讲的三条主线（两类 trait、元组依次提取、Rejection→响应）串成一个可运行的小任务。

**任务**：写一个 `POST /echo` 服务，handler 签名为：

```rust
async fn echo(method: Method, headers: HeaderMap, payload: Result<Json<MyBody>, JsonRejection>)
-> Result<Json<MyBody>, (StatusCode, String)>
```

要求：

1. `Method` 与 `HeaderMap` 是「只读 parts」提取器，放在前面——它们对应 4.2 节。
2. `Result<Json<MyBody>, JsonRejection>` 是「消费 body」的末位提取器（`Json` 走 `FromRequest`），并经由 4.5 节的 `Result` blanket impl 让你在 handler 内部处理提取失败——对应 4.3 与 4.5 节。
3. 在 handler 里 `match` 这个 `Result`：成功则原样回显（`Ok(Json(payload))`），失败则按 [extract.md:L299-L330](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/docs/extract.md#L299-L330) 的示例，把 `JsonDataError` / `JsonSyntaxError` / `MissingJsonContentType` 分别映射成不同的 `(StatusCode, String)`。
4. 用 `curl` 分别发送：合法 JSON、缺 `Content-Type` 头、语法错误 JSON、字段类型错误的 JSON，观察四种不同的响应，并把每一种响应与你从源码读到的 Rejection 变体一一对应（`JsonRejection` 的细节在 [u3-l4](u3-l4-json-extractor.md) 会深入）。

**自检要点**：

- 你能否解释为什么 `Method` 可以放在 `Json` 前面，而反过来不行？（答：`Method: FromRequestParts`，可放任意非末位；`Json` 只有 `FromRequest`，必须末位。）
- 你的 handler 有几个参数就对应 `impl_handler!` 里几次提取调用；末位那次走 `from_request`，前面的走 `from_request_parts`——能否在 [handler/mod.rs:L238-L257](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/handler/mod.rs#L238-L257) 指出对应行？

## 6. 本讲小结

- axum 把「提取」拆成两个 trait：`FromRequestParts` 只读 `&mut Parts`（method/uri/headers/extensions），可多次、可任意顺序；`FromRequest` 以 by-value 拿走整个 `Request`（含 body），只能消费一次。
- 两者用 `private::ViaParts` / `private::ViaRequest` 标记类型 + 一个 blanket impl 桥接：任何 `FromRequestParts` 类型自动也是 `FromRequest<S, ViaParts>`，从而可以出现在 handler 末位（此时 body 被静默丢弃）。
- 「消费 body 的提取器必须是最后一个参数」是**编译期**强制的：`impl_handler!` 的 where 子句要求非末位参数 `FromRequestParts`、末位参数 `FromRequest`。`Json` 只有 `FromRequest`，故写在 `Path` 前会因找不到 `FromRequestParts` impl 而编译失败。
- 元组与 handler 的多参数提取都遵循同一套「先依次 `from_request_parts`、再 `Request::from_parts` 拼回、最后 `from_request` 消费 body」的两阶段流程，由 `all_the_tuples!` 宏对 0~16 元组展开覆盖。
- `Rejection: IntoResponse` 是错误模型的基石：提取失败时 handler 不执行，Rejection 自己变成响应；`Result<T, T::Rejection>` 提取器则把失败包成 `Ok(Err(..))` 交给 handler 自行处理。

## 7. 下一步学习建议

- 本讲只讲了「提取的两个 trait 与多参数机制」。下一篇 [u3-l2](u3-l2-path-extractor.md) 会进入第一个具体提取器 `Path`：看它如何从 `parts.extensions` 读出 `UrlParams`、用自定义 `PathDeserializer` 反序列化，以及它的 `PathRejection` 各变体。建议先把本讲的「`FromRequestParts` 签名」与「Rejection→响应」记牢，再去读 `Path` 的实现就非常顺畅。
- 想提前了解 body 提取的细节（Content-Type 校验、`serde_path_to_error`、大小限制），可直接跳到 [u3-l4](u3-l4-json-extractor.md) 的 `Json` 提取器，它是 `FromRequest` 最完整的范例。
- 如果你对本讲里「宏为元组/参数展开」感兴趣，[u8-l1](u8-l1-handler-trait-macro.md) 会从专家层视角完整拆解 `Handler` trait 与 `impl_handler!` 宏，包括 `Handler<T, S>` 里那个 `T` 参数如何用于 trait coherence。
