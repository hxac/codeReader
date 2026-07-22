# 服务实现总览：SglangServiceImpl 与三类 RPC

## 1. 本讲目标

在 u2-l1 里，我们走通了「`.proto` 契约 → `tonic-build` 生成 Rust 代码 → `include_proto!` 引入」这条链路，知道了 `service SglangService` 会被生成成一个 Rust trait `proto::sglang_service_server::SglangService`，里面有 25 个待实现的方法。但**谁来实现这个 trait、实现出来长什么样、25 个方法是怎么组织的**——这些 u2-l1 故意没讲，正是本讲的主题。

本讲只做一件事：**给你一张 `server.rs` 的「全景地图」**。读完本讲，你应当能够：

- 说出 `SglangServiceImpl` 这个结构体持有哪些字段、每个字段从哪里来、起什么作用。
- 看懂 `#[tonic::async_trait] impl ... SglangService for SglangServiceImpl` 这块大代码的整体骨架，并能区分**流式 RPC** 与**一元 RPC** 两种截然不同的返回类型写法。
- 认识三类 RPC（SGLang 原生 / OpenAI 透传 / Admin）背后共享的三种**公共提交模式**（`submit_request` / `submit_json` / `submit_openai`）。

本讲**只看结构，不看细节**。流式响应的逐分支匹配留给 u2-l3，一元 RPC 的 JSON 解析留给 u2-l4，桥接通道的内部机制留给 u2-l5。本讲结束时，你脑子里应该有一张「方法清单 + 分组 + 入口」的表，后续精读任何一个方法时都能在这张表里快速定位。

## 2. 前置知识

进入源码前，先用通俗语言把四个 Rust/tonic 概念讲清楚。它们是看懂 `server.rs` 整块代码的钥匙。

### 2.1 trait 与 tonic 生成的服务 trait

Rust 的 **`trait`** 类似其他语言里的「接口」：它声明一组方法签名，由具体的类型去实现（`impl Trait for Type`）。

tonic 在 `build.rs` 里为 proto 里的每个 `service` 生成一个对应的 trait。对 `service SglangService`，生成出来的就是 `proto::sglang_service_server::SglangService`。**我们要做的，就是写一个类型 `SglangServiceImpl`，为它实现这个 trait**——这样 tonic 就能把网络进来的 gRPC 调用，分发到我们写的方法上。

### 2.2 为什么需要 `#[tonic::async_trait]`

这个 trait 里的方法几乎都是 `async fn`。在较早的 Rust 里，「trait 里写 `async fn`」有限制，需要借助 `async_trait` 宏把 `async fn foo(...)` 改写成「返回一个 `Pin<Box<dyn Future ...>>`」的普通方法。`#[tonic::async_trait]` 就是 tonic 自带的这个宏。你只要记住一句话：**看到 `#[tonic::async_trait]`，就知道下面这块 `impl` 里每个方法都是异步的、且能被 tonic 当作 RPC handler 调度。**

### 2.3 关联类型（associated type）与流式 RPC

proto 里带 `stream` 关键字的服务端流式 RPC，在生成的 trait 里会变成一个**关联类型**加上一个返回它的方法，大致长这样：

```rust
// tonic 生成的 trait（示意）
trait SglangService {
    type TextGenerateStream: Stream<Item = Result<TextGenerateResponse, Status>> + Send + 'static;
    async fn text_generate(&self, req: Request<TextGenerateRequest>)
        -> Result<Response<Self::TextGenerateStream>, Status>;
    // ...
}
```

`type TextGenerateStream` 是关联类型：实现者要指明「这个流到底用什么具体类型来承载」。我们的实现里统一把它指向 `StreamResult<...>`（下一节精读）。**`Self::TextGenerateStream` 这种写法就是「引用我自己声明的那个关联类型」。** 一元 RPC 没有关联类型，直接返回单个消息即可。

### 2.4 `Response<T>`、`Status` 与 `Stream`

- **`tonic::Response<T>`**：把「成功结果」包一层，附带 gRPC 的响应头/元数据。`T` 就是真正要发给客户端的东西。
- **`tonic::Status`**：gRPC 的「错误」，由一个状态码（`Code`）和一段消息组成。`Result<Response<T>, Status>` 是每个 RPC 方法的统一返回类型。
- **`tokio_stream::Stream`**：异步版的「迭代器」，代表「未来会陆续产出若干个 `Item`」的数据流。服务端流式 RPC 的 `Response` 里装的就是它。

> 名词速查：本讲反复出现的 `rid` = **request id**，是每个请求的唯一标识（`uuid`），贯穿「Rust 提交 → Python 处理 → 回调推送 chunk」整条链路，是 u2-l5 桥接通道的键。本讲把它当成「每个请求的身份证号」即可。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件展开：

| 文件 | 作用 |
| --- | --- |
| [`rust/sglang-grpc/src/server.rs`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L21-L24) | 服务实现的全部家：`SglangServiceImpl` 结构体、trait 实现、辅助方法、服务引导 `run_grpc_server`、单元测试模块。 |

为了讲清「公共提交模式」，还要顺手看一眼桥接层暴露的三个提交方法（**只看签名，不深入内部**——内部是 u2-l5 的内容）：

| 文件 | 作用 |
| --- | --- |
| [`rust/sglang-grpc/src/bridge.rs`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L14-L18) | 桥接层：定义 `ResponseChunk`/`TerminalError`，以及 `submit_request`/`submit_json`/`submit_openai` 三个提交入口。 |

> 定位提示：`server.rs` 全文约 1010 行，结构很规整——顶部是结构体与常量（21–31）、一堆辅助函数（37–214）、中段是庞大的 trait 实现（216–847）、随后是辅助方法（850–951）与服务引导（978–1007），最后是测试模块（1010）。本讲主要在 21–31 和 216–951 这两个区段里活动。

## 4. 核心概念与源码讲解

本讲按三个最小模块拆分：先看「服务实现者」本身，再看它实现 trait 的「骨架」与流式/一元之分，最后看三类 RPC 背后的「三种公共提交模式」。

### 4.1 `SglangServiceImpl` 结构体与 `StreamResult` 类型别名

#### 4.1.1 概念说明

第一块要回答的问题是：**谁来替我们实现 `SglangService` 这个 trait？它需要记住哪些东西？**

答案是 `SglangServiceImpl`。它是「无状态的薄壳」——自身不保存任何请求级数据，只持有两个跨请求共享的、整个服务生命周期内不变的东西：

1. 一个指向 Python 运行时的**桥接句柄** `bridge`。
2. 一个统一的**响应超时时长** `response_timeout`。

所有真正「按请求」变化的状态（每个 rid 对应的通道、回调、错误等）都住在 `bridge` 内部，`SglangServiceImpl` 只负责把请求送进桥接、把结果送回客户端。

#### 4.1.2 核心流程

这个结构体的生命周期非常简单：

```
run_grpc_server(服务引导)
   │  构造 SglangServiceImpl { bridge, response_timeout }
   ▼
tonic 把每个进来的 RPC 调用 → 分发到对应 async fn
   │  方法内部几乎都以 &self.bridge.xxx(...) 的方式使用这两个字段
   ▼
方法返回 Result<Response<...>, Status> → tonic 回发给客户端
```

至于「流式响应到底用什么具体类型」，则交给紧挨着结构体的一个类型别名 `StreamResult<T>` 统一描述。

#### 4.1.3 源码精读

结构体定义只有两个字段（[server.rs:21-24](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L21-L24)）：

```rust
pub struct SglangServiceImpl {
    pub bridge: Arc<PyBridge>,
    pub response_timeout: Duration,
}
```

- `bridge: Arc<PyBridge>`：`Arc` 说明它被多个 owner 共享（每个并发请求都持有克隆）。`PyBridge` 就是 u2-l5 要精读的桥接器，内部持有 Python `RuntimeHandle` 和每请求通道。
- `response_timeout: Duration`：单个响应（或单个流式 chunk）的最长等待时间，超过就回 `DEADLINE_EXCEEDED`。它由启动入口 `start_server` 的 `response_timeout_secs` 参数归一化后传入（回顾 u1-l4：为 0 时回退默认 300 秒）。

紧随其后的两个常量与一个类型别名（[server.rs:26-31](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L26-L31)）：

```rust
type StreamResult<T> = Pin<Box<dyn Stream<Item = Result<T, Status>> + Send + 'static>>;
pub const DEFAULT_RESPONSE_TIMEOUT_SECS: u64 = 300;
pub const DEFAULT_GRPC_MAX_MESSAGE_SIZE: usize = 64 * 1024 * 1024;
```

`StreamResult<T>` 这一行是本模块的关键。把它从外往里读：

- 最内层 `Result<T, Status>`：流里每吐出一个条目，要么是一个成功消息 `T`，要么是一个 gRPC 错误 `Status`。
- `dyn Stream<Item = ...>`：一个能异步产出上述条目的流。
- `Box<dyn ...>`：把流装箱，让大小在编译期不确定的流也能被返回。
- `Pin<Box<...>>`：钉住这个 `Box`，保证「自引用」的流不会被移动（`Stream` 的内部常需要 `Pin` 保证安全）。
- `+ Send + 'static`：可以跨线程移动（`Send`），且不借用任何局部变量（`'static`），因此能被交给 tonic 的运行时。

一句话：**`StreamResult<T>` = 「一个可跨线程转移、自包含、会陆续产出 `Result<T, Status>` 的异步流」。** 后面所有流式 RPC 的关联类型都指向它。

> 这两个常量里，`DEFAULT_GRPC_MAX_MESSAGE_SIZE`（64 MiB）属于服务引导话题，本讲不展开，留给 u3-l4；这里你只需知道它和「消息大小上限」有关。

#### 4.1.4 代码实践

**实践目标**：确认 `SglangServiceImpl` 的两个字段在服务启动时如何被赋值。

**操作步骤**：

1. 打开 [server.rs:978-989](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L978-L989)，找到 `run_grpc_server` 函数。
2. 观察它的参数列表：`listener`、`bridge: Arc<PyBridge>`、`shutdown: Arc<Notify>`、`response_timeout: Duration`。
3. 看它如何用结构体字面量 `SglangServiceImpl { bridge, response_timeout }` 直接把入参搬进字段。

**需要观察的现象**：`bridge` 与 `response_timeout` 是 `run_grpc_server` 的**入参**，结构体本身不做任何计算，只是「转手」。这说明真正的运行时状态都不在 `SglangServiceImpl` 里。

**预期结果**：你能画出 `start_server`（lib.rs，构造 `PyBridge` 与超时）→ `run_grpc_server`（server.rs，构造 `SglangServiceImpl`）→ tonic 分发的链路。`start_server` 与 `run_grpc_server` 的衔接细节属于 u1-l4 与 u3-l4，这里只需确认「字段值从上游一路透传」。

> 待本地验证：若你已按 u1-l2 的方式本地构建过本 crate，可用 `cargo doc --no-deps -p sglang-grpc` 生成文档，在 `SglangServiceImpl` 的页面确认两个字段均为 `pub`（因为 `run_grpc_server` 与它不在同一 `impl` 块之外的作用域里也要能写）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `bridge` 用 `Arc<PyBridge>` 而不是直接 `PyBridge` 或 `&PyBridge`？

> **参考答案**：`SglangServiceImpl` 的方法签名是 `&self`，但每个请求处理时常常需要把 `bridge` 的克隆交给一个异步任务（例如 `RequestAbortGuard`、`spawn_blocking` 闭包），这些任务的生命周期长于单次方法调用。`&PyBridge` 借用活不过这些异步任务；`PyBridge` 直接持有则无法多处共享。`Arc` 提供「引用计数的共享所有权」，克隆廉价，正好满足「多处异步持有同一个桥接」的需求。

**练习 2**：把 `type StreamResult<T>` 里的 `+ Send + 'static` 两个约束删掉， tonic 会高兴吗？为什么？

> **参考答案**：不会。tonic 的传输层会把响应流交到多线程 Tokio 运行时上调度，要求流可以跨线程移动（`Send`）；同时流不能借用任何局部变量，否则方法返回后局部变量被释放、流就悬空了（`'static`）。这两个约束是 tonic 服务端流式响应的硬性要求。

---

### 4.2 trait 实现骨架：`async_trait` 与流式 / 一元返回类型

#### 4.2.1 概念说明

第二块要回答的问题是：**`impl SglangService for SglangServiceImpl` 这一大块代码长什么样？25 个方法是怎么挂上去的？**

从高空看，这块代码有一个非常整齐的**二分结构**：

- **流式 RPC**：proto 里带 `stream` 的方法。trait 里要求实现者声明一个关联类型（`type XStream = ...`），方法返回 `Result<Response<Self::XStream>, Status>`，也就是「Response 里装的是一条会持续吐条目的流」。
- **一元 RPC**：proto 里不带 `stream` 的方法。没有关联类型，方法直接返回 `Result<Response<具体消息>, Status>`，也就是「Response 里装的是一个完整的单条消息」。

记住这两种「形状」，你就能一眼分辨任意一个方法是流式还是一元。

#### 4.2.2 核心流程

整块 trait 实现的骨架可以用下面的伪代码概括（方法体省略）：

```rust
#[tonic::async_trait]
impl proto::sglang_service_server::SglangService for SglangServiceImpl {
    // —— 流式 RPC：先声明关联类型，方法返回 Response<Self::XStream> ——
    type TextGenerateStream = StreamResult<proto::TextGenerateResponse>;
    async fn text_generate(...) -> Result<Response<Self::TextGenerateStream>, Status> { ... }

    type GenerateStream = StreamResult<proto::GenerateResponse>;
    async fn generate(...) -> Result<Response<Self::GenerateStream>, Status> { ... }

    // OpenAI 流式同理，只是关联类型指向 OpenAiStreamChunk
    type ChatCompleteStream = StreamResult<proto::OpenAiStreamChunk>;
    async fn chat_complete(...) -> Result<Response<Self::ChatCompleteStream>, Status> { ... }

    // —— 一元 RPC：无关联类型，Response 里直接装单条消息 ——
    async fn text_embed(...) -> Result<Response<proto::TextEmbedResponse>, Status> { ... }
    async fn list_models(...) -> Result<Response<proto::ListModelsResponse>, Status> { ... }
    // ... 其余一元方法 ...
}
```

注意一个**极易踩的坑**：`Self::TextGenerateStream` 里的 `Self` 指的是「当前正在实现的类型」`SglangServiceImpl`，而 `TextGenerateStream` 是我们上面刚声明的那一行关联类型——两者是配对的。改了关联类型的指向，方法返回的具体流类型也随之改变。

#### 4.2.3 源码精读

整块实现的起始两行（[server.rs:216-217](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L216-L217)）：

```rust
#[tonic::async_trait]
impl proto::sglang_service_server::SglangService for SglangServiceImpl {
```

**流式 RPC 的声明 + 方法头**（以 `text_generate` 为例，[server.rs:220-225](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L220-L225)）：

```rust
type TextGenerateStream = StreamResult<proto::TextGenerateResponse>;

async fn text_generate(
    &self,
    request: Request<proto::TextGenerateRequest>,
) -> Result<Response<Self::TextGenerateStream>, Status> {
```

注意返回类型 `Response<Self::TextGenerateStream>`——外层 `Response` 里装的不是单条消息，而是一整条 `StreamResult<...>` 流。方法体最后会用 `Ok(Response::new(Box::pin(stream)))` 把这条流交回 tonic（具体怎么构造这条流是 u2-l3 的主题）。

全文件一共声明了 **4 个流式关联类型**，正好对应 4 个服务端流式 RPC：

| 关联类型声明 | 所属 RPC | 内层消息 | 行号 |
| --- | --- | --- | --- |
| `type TextGenerateStream = StreamResult<proto::TextGenerateResponse>` | `text_generate` | 原生 | [server.rs:220](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L220) |
| `type GenerateStream = StreamResult<proto::GenerateResponse>` | `generate` | 原生 | [server.rs:289](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L289) |
| `type ChatCompleteStream = StreamResult<proto::OpenAiStreamChunk>` | `chat_complete` | OpenAI | [server.rs:737](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L737) |
| `type CompleteStream = StreamResult<proto::OpenAiStreamChunk>` | `complete` | OpenAI | [server.rs:747](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L747) |

**一元 RPC 的方法头**（以 `text_embed` 为例，[server.rs:360-363](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L360-L363)）：

```rust
async fn text_embed(
    &self,
    request: Request<proto::TextEmbedRequest>,
) -> Result<Response<proto::TextEmbedResponse>, Status> {
```

对比流式：没有 `type ...Stream`，`Response<>` 的尖括号里直接是 `proto::TextEmbedResponse` 这个**单条消息结构体**，而不是 `Self::XStream` 流。这就是一眼分辨流式 / 一元的诀窍。

> 还有一类「委托型」一元方法，自己不写方法体，直接转交给一个辅助方法。例如 [`chat_complete`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L739-L745) 的整个方法体只有一行 `self.openai_streaming_rpc(request, "submit_openai_chat").await`。这种委托把「OpenAI 流式」与「OpenAI 一元」的共用逻辑抽到了 4.3 节要讲的辅助方法里。

#### 4.2.4 代码实践

**实践目标**：在本讲的源码地图上亲手清点一遍，把「25 个方法」数清楚、分好组，并验证流式与一元的返回类型差异。

**操作步骤**：

1. 打开 [server.rs:216-847](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L216-L847)，定位 `impl ... SglangService for SglangServiceImpl { ... }` 这个块（它从第 217 行开始，到第 847 行结束）。
2. 在这个块里数 `async fn` 的数量，按注释分块（`// --- SGLang-native RPCs ... ---`、`// --- OpenAI-compatible RPCs ... ---`、`// --- Admin RPCs ---`）分组。
3. 另起一处看 [server.rs:850-951](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L850-L951)，这是**另一个** `impl SglangServiceImpl { ... }` 块（inherent impl，不是 trait 实现）。

**需要观察的现象**：

- trait 实现块（216–847）里有 **25 个** `async fn`。
- 流式方法有 **4 个**（`text_generate`、`generate`、`chat_complete`、`complete`），它们每个上方都有一行 `type XStream = StreamResult<...>`。
- 其余 **21 个**是一元方法，返回 `Result<Response<proto::具体消息>, Status>`，没有关联类型。
- 第二个 `impl` 块（850–951）里的 `openai_streaming_rpc`、`openai_unary_rpc` **不计入这 25 个**——它们是辅助方法，不是 trait 方法。

**预期结果**（参考答案见下方表格）：

| 分组 | 数量 | 成员 |
| --- | --- | --- |
| SGLang 原生（16） | 16 | `text_generate`※、`generate`※、`text_embed`、`embed`、`classify`、`tokenize`、`detokenize`、`health_check`、`get_model_info`、`get_server_info`、`list_models`、`get_load`、`abort`、`flush_cache`、`pause_generation`、`continue_generation` |
| OpenAI 透传（6） | 6 | `chat_complete`※、`complete`※、`open_ai_embed`、`open_ai_classify`、`score`、`rerank` |
| Admin/Ops（3） | 3 | `start_profile`、`stop_profile`、`update_weights_from_disk` |

> 带 ※ 的为流式 RPC，共 4 个。16 + 6 + 3 = 25，与 u2-l1 统计的 proto rpc 总数一致。

**返回类型差异小结**（本实践的第二个要求）：

- 流式：`Result<Response<Self::XStream>, Status>`，其中 `Self::XStream = StreamResult<proto::FooResponse>`。Response 里装的是「会陆续产出多个 `Result<proto::FooResponse, Status>` 的异步流」。
- 一元：`Result<Response<proto::FooResponse>, Status>`。Response 里直接装「一个完整的 `proto::FooResponse` 消息」。

#### 4.2.5 小练习与答案

**练习 1**：如果有人误把 `type TextGenerateStream` 这一行删掉，但保留 `text_generate` 方法，编译会报什么错？

> **参考答案**：trait `SglangService` 声明了关联类型 `TextGenerateStream`，实现者必须提供它。删掉这行会让编译器报「缺失 trait 关联类型的赋值」类错误；同时方法返回类型里的 `Self::TextGenerateStream` 也会找不到该名字而报「cannot find type」错。两者都说明关联类型声明与方法是**配对**的。

**练习 2**：`chat_complete` 与 `complete` 的关联类型都指向 `StreamResult<proto::OpenAiStreamChunk>`，这意味着什么？

> **参考答案**：意味着这两个流式 RPC 对外吐出的「每个条目」都是同一种消息 `OpenAiStreamChunk`（一段 OpenAI 格式的 JSON 片段）。两者的区别不在「流的条目类型」，而在方法体里调用 Python 端的哪个方法（`submit_openai_chat` vs `submit_openai_complete`）——这是 4.3 节要讲的「方法名透传」。

---

### 4.3 三类 RPC 的布局与三种公共提交模式

#### 4.3.1 概念说明

第三块要回答的问题是：**25 个方法的方法体虽然各不相同，但它们是不是有一些「公共套路」可以归纳？**

答案是有的。绝大多数「数据型」RPC 在方法体开头都会做三件事：

1. `request.into_inner()`：从 tonic 的 `Request` 里剥出 proto 消息。
2. 生成或取出 `rid`（多数用 `uuid::Uuid::new_v4()`，原生 RPC 也允许客户端在 proto 里带 `rid`）。
3. 调用一个**桥接提交方法**，拿到一个 `Receiver<ResponseChunk>`，再从中读取结果。

关键就在第 3 步。`PyBridge` 暴露了三种「把请求送进 Python」的入口，正好对应三类不同的请求形状。看懂这三种入口，就抓住了整块代码的主干。

> 名词速查：[`ResponseChunk`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L14-L18) 是桥接通道里流转的「响应片段」，有三个变体：`Data(ResponseData)`（中间产物）、`Finished(ResponseData)`（正常终结）、`Error(String)`（出错终结）。本讲只把它理解成「通道里的一条消息」即可，逐分支处理留给 u2-l3/u2-l4。

#### 4.3.2 核心流程

三种公共提交模式（它们的完整内部机制在 u2-l5，本讲只看**签名 + 谁在用**）：

```
┌─────────────────────────────────────────────────────────────────┐
│  模式 A：submit_request(rid, req_type, req_dict)                 │
│  把「结构化字段」拼成 dict + req_type 字符串交给 Python          │
│  → 返回 mpsc Receiver<ResponseChunk>                            │
│  使用者：text_generate / generate / text_embed / embed / classify│
└─────────────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────────────┐
│  模式 B：submit_openai(rid, method_name, json_body, trace_headers)│
│  把「整段 OpenAI JSON 字节」原样透传给 Python 的某个方法          │
│  → 返回 mpsc Receiver<ResponseChunk>                            │
│  使用者：chat_complete / complete / open_ai_embed /              │
│          open_ai_classify / score / rerank（经两个辅助方法）     │
└─────────────────────────────────────────────────────────────────┘
┌─────────────────────────────────────────────────────────────────┐
│  模式 C：submit_json(rid, 闭包)  —— 由各 submit_* 小方法包装     │
│  调用 Python 的某个控制/查询方法，结果以 JSON 字符串回传         │
│  → 返回 mpsc Receiver<ResponseChunk>，再用 recv_json_response 取 │
│  使用者：get_load / flush_cache / pause_generation /             │
│          continue_generation / start_profile / stop_profile /    │
│          update_weights_from_disk                                │
└─────────────────────────────────────────────────────────────────┘
```

还有少数 RPC **不走通道**，属于「直连」模式：`tokenize`/`detokenize`（先试 Rust 原生分词器，否则 `spawn_blocking` 调 `tokenize_py`/`detokenize_py`）、`health_check`/`get_model_info`/`get_server_info`/`list_models`（`spawn_blocking` 直调桥接方法拿返回值）、`abort`（直调 `bridge.abort`）。它们不创建 per-request 通道，所以没有 `Receiver`。

#### 4.3.3 源码精读

**模式 A —— `submit_request`**：以 `generate` 为例（[server.rs:302-305](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L302-L305)）：

```rust
let mut receiver = self
    .bridge
    .submit_request(&rid, "generate", req_dict)
    .map_err(|e| pyerr_to_status(e, "Failed to submit request"))?;
```

`submit_request` 的签名（[bridge.rs:177-182](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L177-L182)）：

```rust
pub fn submit_request(
    &self,
    rid: &str,
    req_type: &str,          // "generate" / "embed"
    req_dict: HashMap<String, serde_json::Value>,
) -> PyResult<Receiver<ResponseChunk>>
```

要点：第二个参数 `req_type` 是一个字符串，决定 Python 端走「生成」还是「嵌入」路径。`classify` 也复用这条路径，传的就是 `"embed"`（因为分类在 SGLang 内部复用了嵌入管线，详见 u2-l4）。`req_dict` 是把 proto 消息拍平后的字典，由 `utils::build_*_dict` 系列构造（详见 u2-l7）。

**模式 B —— `submit_openai`**：6 个 OpenAI RPC 都不直接调它，而是经由两个辅助方法。以 `rerank`（一元）为例（[server.rs:779-784](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L779-L784)）：

```rust
async fn rerank(&self, request: Request<proto::OpenAiRequest>)
    -> Result<Response<proto::OpenAiResponse>, Status> {
    self.openai_unary_rpc(request, "submit_openai_rerank").await
}
```

辅助方法 `openai_unary_rpc` 内部才真正调 `submit_openai`（[server.rs:922-925](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L922-L925)）：

```rust
let mut receiver = self
    .bridge
    .submit_openai(&rid, method_name, &req.json_body, &req.trace_headers)
    .map_err(|e| pyerr_to_status(e, "Failed to submit request"))?;
```

`submit_openai` 的签名（[bridge.rs:411-417](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L411-L417)）：

```rust
pub fn submit_openai(
    &self,
    rid: &str,
    method_name: &str,          // "submit_openai_chat" / "submit_openai_rerank" / ...
    json_body: &[u8],           // 整段 OpenAI JSON，原样透传
    trace_headers: &HashMap<String, String>,
) -> PyResult<Receiver<ResponseChunk>>
```

要点：OpenAI RPC 的请求体是一段**不透明的 JSON 字节**（`bytes json_body`），Rust 端不解析它，只把 `method_name` 当成 Python 端的方法名连同字节一起送过去。这正是 u2-l1 里说的「OpenAI 透传」的含义。

**模式 C —— `submit_json` 家族**：以 `flush_cache` 为例（[server.rs:681-684](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L681-L684)）：

```rust
let rid = uuid::Uuid::new_v4().to_string();
let receiver = self
    .bridge
    .submit_flush_cache(&rid)
    .map_err(|e| pyerr_to_status(e, "Failed to flush cache"))?;
```

`submit_flush_cache` 这类小方法都委托给同一个私有泛型方法 `submit_json`（[bridge.rs:315-335](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L315-L335)），差别只在闭包里调用 Python 的哪个方法：

```rust
pub fn submit_flush_cache(&self, rid: &str) -> PyResult<Receiver<ResponseChunk>> {
    self.submit_json(rid, |py, runtime_handle, callback| {
        runtime_handle.call_method1(py, "flush_cache", (callback,))?;
        Ok(())
    })
}
```

它们的响应统一由 `recv_json_response` 收口——收一个终结 chunk，把里面的 `json_bytes` 解码成 UTF-8 字符串返回（[server.rs:954-971](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L954-L971)）；各方法再把这个字符串 `serde_json::from_str` 成自己的字段。

> 三种模式的桥梁内部（`create_channel` 去重、GIL 调用、失败清理）都在 u2-l5 精读；`recv_json_response` 与 `recv_terminal_chunk_for_request` 的终结语义在 u2-l4 精读。本讲到此为止，不要陷进去。

#### 4.3.4 代码实践

**实践目标**：用一张表把「25 个 RPC ↔ 提交模式」的对应关系亲手填出来，验证三种模式确实覆盖了绝大多数数据型 RPC。

**操作步骤**：

1. 在 [server.rs:216-847](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L216-L847) 里逐个方法找「它调用 `bridge.` 的哪个方法」。
2. 按下表归类（A=submit_request，B=submit_openai，C=submit_json 系列，D=不走通道的直连）。

**需要观察的现象 / 预期结果**：

| 模式 | 桥接方法 | 消费 receiver 的方式 | RPC 成员 | 数量 |
| --- | --- | --- | --- | --- |
| A 结构化 | `submit_request` | 流式 `stream!` 循环 或 `recv_terminal_chunk_for_request` | text_generate、generate、text_embed、embed、classify | 5 |
| B OpenAI 透传 | `submit_openai`（经 `openai_streaming_rpc`/`openai_unary_rpc`） | 同上 | chat_complete、complete、open_ai_embed、open_ai_classify、score、rerank | 6 |
| C JSON 控制 | `submit_*` → `submit_json` → `recv_json_response` | `recv_json_response` | get_load、flush_cache、pause_generation、continue_generation、start_profile、stop_profile、update_weights_from_disk | 7 |
| D 直连无通道 | `rust_tokenizer`/`tokenize_py`/`health_check`/... 或 `bridge.abort` | 直接返回值 / 无 receiver | tokenize、detokenize、health_check、get_model_info、get_server_info、list_models、abort | 7 |

> 5 + 6 + 7 + 7 = 25，全员到齐。

**思考题（动手验证）**：模式 B 的 6 个 RPC 里，哪些是流式、哪些是一元？提示——看它们调用的是 `openai_streaming_rpc` 还是 `openai_unary_rpc`（[server.rs:739-784](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L739-L784)）。

> **参考答案**：`chat_complete`、`complete` 调 `openai_streaming_rpc`（流式）；`open_ai_embed`、`open_ai_classify`、`score`、`rerank` 调 `openai_unary_rpc`（一元）。

#### 4.3.5 小练习与答案

**练习 1**：`classify` 在 proto 里是独立的 RPC，为什么它提交时传的 `req_type` 是 `"embed"` 而不是 `"classify"`？

> **参考答案**：因为 SGLang 内部的分类（reward / 评分模型）走的是嵌入管线（`EmbeddingReqInput`），并没有一条独立的 `classify` Python 提交路径。`submit_request` 的 `req_type` 参数最终对应 Python `RuntimeHandle.submit_request` 的 `req_type`，传 `"embed"` 就是复用嵌入路径；`build_classify_dict` 构造的字典字段也与嵌入一致。这是「上层 proto 分类清晰、底层复用嵌入管线」的设计取舍（详见 u2-l4）。

**练习 2**：模式 B 的 OpenAI RPC 为什么不像模式 A 那样用 `build_*_dict` 把 proto 拆成字典，而是直接传 `&req.json_body` 字节？

> **参考答案**：OpenAI 兼容接口的请求体本身就是一段标准 JSON，SGLang 想原样保留它（包括未来新增的任意字段），所以 proto 里就用 `bytes json_body` 承载，Rust 端不解析、不重新拼装，直接把字节透传给 Python 端对应的 `submit_openai_*` 方法。这是「强类型（原生 RPC）」与「不透明透传（OpenAI RPC）」两种设计哲学的分水岭（呼应 u2-l1）。

---

## 5. 综合实践

把本讲三个模块串起来的小任务：**为 `SglangServiceImpl` 画一张「方法速查卡」**。

要求：

1. 在一张表里列出全部 25 个 trait 方法，每行包含：方法名、所属类别（原生/OpenAI/Admin）、流式还是一元、使用的提交模式（A/B/C/D）。
2. 在表下方用一段话回答：流式 RPC 与一元 RPC 在「返回类型」与「receiver 消费方式」这两个维度上分别有什么不同？
3. 最后，挑一个你感兴趣的流式方法（如 `text_generate`）和一个一元方法（如 `list_models`），分别用一句话概括它们的方法体在做什么——为后续精读 u2-l3（流式细节）和 u2-l4（一元 JSON 细节）做铺垫。

**检查清单**（自查）：

- [ ] 表里正好 25 行，分组合计 16 + 6 + 3。
- [ ] 流式恰有 4 个，且都标了关联类型 `type XStream`。
- [ ] 提交模式 A/B/C/D 的数量分别为 5/6/7/7。
- [ ] 你能指出 `openai_streaming_rpc`/`openai_unary_rpc` 这两个辅助方法**不在** 25 个之内、但被其中 6 个调用。

> 这个任务不需要改任何源码，纯阅读 + 归纳。完成后建议把这张速查卡保存下来——它会是阅读 u2-l3 ~ u2-l8 时最好的「索引页」。

## 6. 本讲小结

- `SglangServiceImpl` 是实现 `SglangService` trait 的「薄壳」，只持有 `bridge: Arc<PyBridge>` 和 `response_timeout: Duration` 两个字段，自身不保存任何请求级状态——真正的状态都在 `bridge` 内部。
- `type StreamResult<T> = Pin<Box<dyn Stream<Item = Result<T, Status>> + Send + 'static>>` 统一描述了所有服务端流式响应的具体类型：可跨线程转移、自包含、会陆续产出 `Result<T, Status>` 的异步流。
- trait 实现块（[server.rs:216-847](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L216-L847)）共 **25 个** `async fn`，分成 SGLang 原生（16）/ OpenAI 透传（6）/ Admin（3）三类，与 u2-l1 的 proto rpc 总数一一对应。
- **流式 RPC**（4 个）先声明 `type XStream = StreamResult<...>`，返回 `Result<Response<Self::XStream>, Status>`；**一元 RPC**（21 个）无关联类型，返回 `Result<Response<proto::具体消息>, Status>`——这是分辨两者的诀窍。
- 数据型 RPC 共享三种**公共提交模式**：`submit_request`（结构化 dict）、`submit_openai`（OpenAI JSON 字节透传）、`submit_json` 系列（控制/查询，结果回 JSON 字符串）；另有 7 个 RPC 走「直连」、不创建通道。
- `openai_streaming_rpc`/`openai_unary_rpc` 是住在**另一个** `impl SglangServiceImpl` 块（[server.rs:850-951](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L850-L951)）里的辅助方法，不是 trait 方法，容易被误数。

## 7. 下一步学习建议

本讲只给了「骨架与地图」，方法体里的细节都还没展开。建议按下面顺序继续：

1. **u2-l3 流式 RPC 与 async_stream**：精读 `text_generate`/`generate`/`openai_streaming_rpc` 里 `async_stream::stream!` 宏构造的响应流，搞清 `ResponseChunk::Data/Finished/Error` 与 `Ok(None)` 四个分支各自的产出与终止语义。
2. **u2-l4 一元 RPC 与 JSON 响应解析**：精读 `recv_terminal_chunk_for_request`/`recv_json_response` 的终结语义，以及 `list_models`、`classify` 等如何把 JSON 字符串解析成 proto 字段并兜底默认值。
3. **u2-l5 PyBridge 与请求通道架构**：深入本讲反复出现的 `submit_request`/`submit_json`/`submit_openai` 的内部——`create_channel` 去重、GIL 调用 Python、失败时 `remove_channel` 清理。
4. 想先验证「整块代码能编过」的同学，可回到 u1-l2 的构建方式，用 `cargo check -p sglang-grpc` 做一次类型层面的自检。
