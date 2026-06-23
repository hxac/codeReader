# 讲义标题：axum 是什么：定位、理念与生态

> 本讲是 axum 学习手册的第一篇。我们会从最基本的问题开始：axum 到底是什么？它解决了什么问题？它在整个 Rust Web 生态中处在什么位置？读完本讲你不必立刻看懂任何源码细节，但你会建立一个清晰的心智模型，知道后续每一篇讲义在整体地图中的位置。

## 1. 本讲目标

读完本讲，你应该能够：

- 用一句话说清 axum 是什么、它解决的核心问题是什么。
- 复述 axum 自我宣传的「四大特性」：路由（routing）、提取器（extractors）、错误模型（error handling）、响应生成（responses）。
- 画出 axum、hyper、tokio、tower、tower-http 这几个关键依赖之间的**分层关系**，并能解释「谁建在谁之上」。
- 理解 axum 最核心的设计取舍：**不自己造中间件系统，而是直接复用 `tower::Service`**，以及这样做带来的「免费能力」。
- 能够独立阅读 README 和 crate 顶层文档，并据此回答几个引导性问题。

本讲**不要求**你先学会 Rust 异步、tower、hyper 的任何细节。遇到不熟悉的术语，本讲都会在出现时简单解释。

## 2. 前置知识

在开始之前，请确保你对下面几个概念有最基本的印象（不需要精通）：

- **HTTP 请求与响应**：一个 HTTP 请求由方法（GET/POST/…）、路径（`/users/42`）、请求头（headers）和可选的请求体（body）组成；响应由状态码（200/404/…）、响应头和响应体组成。
- **Web 框架的作用**：把「进来的 HTTP 请求」分发给「你写的一段处理代码（handler）」，再把处理结果变成「发回去的 HTTP 响应」。axum 就是干这件事的。
- **Rust crate**：Rust 的「包」。一个项目（workspace）可以包含多个 crate。axum 仓库就是一个 workspace，里面有 4 个 crate。
- **Cargo feature**：Cargo 里的开关（feature flag），让你按需启用/关闭功能，从而减少编译体积和依赖。
- **trait**：Rust 里的「接口」概念。本讲会反复提到几个 trait 名字（`Service`、`FromRequest`、`IntoResponse`），现在只需知道它们是「约定好的能力契约」即可。

> 术语提示：下文反复出现的 **handler（处理器）** 指的是你写的那个 `async fn`，它接收请求、返回响应；**中间件（middleware）** 指的是在 handler 前后做通用处理（如鉴权、日志、超时）的组件。

## 3. 本讲源码地图

本讲涉及的关键文件如下。它们是「入口级」的文件，是你认识 axum 的第一站：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/README.md) | 项目的门面。用最短篇幅讲清 axum 是什么、有哪些特性、一段可运行的示例代码。 |
| [Cargo.toml](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/Cargo.toml)（workspace 根）| 整个仓库的 workspace 配置：包含哪些 crate、Rust 最低版本、全局 lint（如禁用 unsafe）。 |
| [axum/Cargo.toml](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/Cargo.toml) | `axum` 这个 crate 自身的配置：版本号、feature flags（`json`/`http1`/`tokio` 等）、依赖清单（hyper/tower/…）。**这是看懂 axum 依赖什么的关键文件。** |
| [axum/src/lib.rs](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/lib.rs) | `axum` crate 的顶层源文件，也是它的 crate 级文档（`//!` 开头的注释会出现在 docs.rs 的首页）。本讲的「顶层文档」精读主要看这里。 |

> 本讲是概念导入篇，所以我们重点读「文档」和「配置」，几乎不展开实现逻辑。具体的源码精读从下一讲（运行第一个程序）开始。

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：

- **4.1 axum 的定位**：它解决什么问题，一句话定义。
- **4.2 四大特性概览**：路由、提取器、错误模型、响应生成。
- **4.3 axum 在生态中的位置**：hyper / tokio / tower / tower-http 的分层关系。
- **4.4 为什么复用 `tower::Service` 而非自造中间件**：axum 最关键的设计取舍。

### 4.1 axum 的定位：它解决什么问题

#### 4.1.1 概念说明

我们先看 axum 自己对「我是什么」的一句话定义：

> `axum` is an HTTP routing and request-handling library that focuses on ergonomics and modularity.
> （axum 是一个专注于**人体工程学（好用）**和**模块化**的 HTTP **路由与请求处理**库。）

这句话出现在 README 的第一行，也几乎原样出现在 `lib.rs` 的第一行注释里。我们拆开看三个关键词：

1. **routing（路由）**：决定「一个进来的请求该交给哪个 handler 处理」。例如 `GET /users/42` 交给「查询用户」的处理函数。
2. **request-handling（请求处理）**：把请求里你需要的信息（路径参数、查询串、JSON body…）方便地「提取」出来交给你的函数，再把函数返回值变成响应。
3. **ergonomics and modularity（好用 + 模块化）**：axum 的 API 设计目标是让你少写样板代码（boilerplate），并且能像搭积木一样组合功能。

那么它解决的核心问题是什么？如果没有 axum，你想用 Rust 写一个 HTTP 服务，底层有 `hyper`（一个高性能、但偏底层的 HTTP 实现）。`hyper` 只给你「收到一个连接、读到一段字节、发回一段字节」的能力，**它不懂路由、不懂怎么把 JSON 变成结构体、不懂中间件**。你需要自己写一大堆胶水代码。axum 就是这层「胶水」，把 `hyper` 之上常见的活儿都替你做好了。

axum 自己在性能一节里也点明了这层关系：

> `axum` is a relatively thin layer on top of `hyper` and adds very little overhead.

也就是说：axum 是「hyper 之上的一层薄薄的封装」，几乎不带来性能损耗。

#### 4.1.2 核心流程

虽然本讲不展开实现，但你可以先建立一个高层「请求生命周期」的直觉，这能帮你看懂后续所有讲义：

```
TCP 连接进入
   │   hyper（底层 HTTP 协议解析）把字节流解析成 http::Request
   ▼
axum::serve 接管连接
   │   把 Request 交给 Router
   ▼
Router 匹配路径 + 方法 ── 找到对应的 handler
   │   （中间件层在此前后介入：日志/鉴权/超时…）
   ▼
提取器（Extractors）把 Request 拆成 handler 需要的参数
   │   例如 Path<u32>、Json<Body>、State<AppState>
   ▼
你的 async handler 被调用，返回一个值
   ▼
该返回值通过 IntoResponse 转成 http::Response
   ▼
hyper 把 Response 序列化成字节流发回客户端
```

注意最上面和最下面是 `hyper` 干的活，中间从「Router 匹配」到「IntoResponse」都是 axum 负责的。这就是「axum 在 hyper 之上」的具体含义。

#### 4.1.3 源码精读

**axum 的自我定义**（README 与 lib.rs 第一行完全一致）：

[README.md:3](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/README.md#L3) 与 [axum/src/lib.rs:1](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/lib.rs#L1) —— 这两处给出了 axum 的一句话定位：HTTP 路由与请求处理库，强调好用与模块化。

**「薄薄一层」的性能说明**：

[README.md:102](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/README.md#L102) —— 明确说明 axum 是 hyper 之上相对薄的一层，开销很小。这正是它能「接近 hyper 性能」的原因。

**与 hyper/tokio 强绑定**：

[axum/src/lib.rs:18-L21](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/lib.rs#L18-L21) —— `# Compatibility` 一节写明 axum 是为 tokio 和 hyper 设计的，「运行时和传输层无关（runtime/transport independence）目前不是目标」。也就是说，axum 不打算兼容其它异步运行时或 HTTP 底层实现，它**主动选择**绑定 tokio + hyper 以换取简洁和性能。

#### 4.1.4 代码实践

**实践目标**：亲手感受「没有 axum 时你大概要写多少胶水」，从而理解 axum 这一层封装的价值。

**操作步骤**：

1. 打开 [README.md](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/README.md) 的「Usage example」一节（约第 33–93 行），阅读那段约 30 行的示例代码。
2. 注意这段代码做了几件事：`Router::new().route(...)` 注册路由、`axum::serve(listener, app)` 启动服务、两个 `async fn` 作为 handler。
3. 想象一下：如果只用裸 `hyper`，你要自己实现「把 `/users` 这种字符串路径匹配到函数」「把请求体字节反序列化成 `CreateUser` 结构体」「把返回的结构体序列化成 JSON 并设置 `Content-Type`」——这些 axum 示例里「看不见」的活儿，全是 axum 帮你做的。

**需要观察的现象**：示例代码里**没有任何**手写字符串匹配、字节解析、状态码拼接的代码，handler 几乎只写「业务逻辑」。

**预期结果**：你会体会到 axum「好用（ergonomic）」这个词的分量——同样功能，裸 hyper 的代码量通常是 axum 的好几倍。

> 本实践为源码阅读型实践，不需要运行（运行留到下一讲）。如果你现在就想跑，可以直接执行下一讲的内容。

#### 4.1.5 小练习与答案

**练习 1**：用一句话（不超过 30 字）描述 axum 是什么。
> **参考答案**：axum 是建在 hyper 之上的 HTTP 路由与请求处理库，强调好用与模块化。

**练习 2**：README 说 axum 是「hyper 之上相对薄的一层」。请猜测：axum 为什么能保持「薄」而不需要重新实现 HTTP 协议解析？
> **参考答案**：因为底层 HTTP 协议解析（连接管理、字节流 ↔ http::Request/Response）已经由 hyper 负责，axum 只在「拿到 Request 之后、发出 Response 之前」这中间一段做路由和请求处理，所以它不需要重造 HTTP 协议这一层，自然就「薄」。

---

### 4.2 四大特性概览

#### 4.2.1 概念说明

axum 在 README 和 lib.rs 里都用同一个无序列表宣告自己的「高级特性（High-level features）」。我们把它视为 axum 的「四大支柱」：

1. **路由（Routing）**：`Route requests to handlers with a macro free API.` —— 用**不用宏**的 API 把请求路由到 handler。
2. **提取器（Extractors）**：`Declaratively parse requests using extractors.` —— 用「声明式」的方式从请求里解析出你想要的数据。
3. **错误模型（Error handling model）**：`Simple and predictable error handling model.` —— 简单且可预测的错误处理模型。
4. **响应生成（Responses）**：`Generate responses with minimal boilerplate.` —— 用最少的样板代码生成响应。

外加一条「贯穿一切」的生态特性：

5. **复用 tower/tower-http 生态**：`Take full advantage of the tower and tower-http ecosystem...` —— 充分利用 tower 生态的中间件、服务和工具。

我们逐一建立初步印象（深入实现留给后续讲义）：

- **「无宏路由 API」是什么意思？** 很多 Web 框架用宏来注册路由，例如 `#[get("/users")]`。axum 不走这条路，而是用普通的方法调用：`Router::new().route("/users", get(handler))`。这意味着路由注册就是普通的 Rust 代码，编辑器的自动补全、重构、类型检查都能正常工作。`get`、`post` 这些是普通函数，不是宏。

- **「提取器」是什么？** 你写 handler 时，参数不是手写从 request 里取数据的代码，而是声明类型，axum 自动帮你提取。例如：

  ```rust
  async fn handler(Path(id): Path<u32>, Json(body): Json<Payload>) { ... }
  ```

  这里 `Path<u32>` 和 `Json<Payload>` 就是「提取器」，axum 会自动从路径参数里取出 `id`、把请求体反序列化成 `Payload`。这是 axum 最具特色的部分（第 3 单元会专讲）。

- **「可预测的错误模型」是什么？** axum 保证所有错误最终都会被处理、变成一个 HTTP 响应，不会出现「错误被吞掉」「程序因为一个未处理错误崩溃」这类情况（第 7 单元专讲）。

- **「最少样板生成响应」是什么？** 你只要让返回值实现 `IntoResponse` trait，axum 就能把它转成响应。`&str` 会自动变成 `200 OK` + `text/plain`，`Json(x)` 会自动变成 `application/json`，元组 `(StatusCode, Json(x))` 还能同时指定状态码和 body（第 4 单元专讲）。

#### 4.2.2 核心流程

可以用一张「四大特性 + 一个生态」的关系图来记忆：

```
                 ┌─────────────────────────────────────┐
请求  ─────────▶ │  路由 Routing：把请求交给哪个 handler  │
                 └────────────────┬────────────────────┘
                                  ▼
                 ┌─────────────────────────────────────┐
                 │ 提取器 Extractors：把请求拆成参数       │
                 └────────────────┬────────────────────┘
                                  ▼
                            你的 async handler
                                  ▼
                 ┌─────────────────────────────────────┐
                 │ 响应 Responses：返回值 → IntoResponse  │
                 └────────────────┬────────────────────┘
                                  ▼
                 ┌─────────────────────────────────────┐
                 │ 错误模型：任何错误都 → 一个 HTTP 响应   │  （贯穿全程）
                 └─────────────────────────────────────┘

   左右两侧贯穿一切：tower / tower-http 生态的中间件与服务
```

错误模型（第 4 项）实际上是「贯穿全程」的：路由匹配不到 → 404 响应；提取失败 → 4xx 响应；handler 出错 → 错误响应。所以它是横切关注点，而不是链路里的某一环。

#### 4.2.3 源码精读

**README 列出的高级特性**（正是「四大特性 + 生态」的出处）：

[README.md:11-L18](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/README.md#L11-L18) —— 这 5 个 bullet 就是 axum 自述的核心特性。

`lib.rs` 的 crate 级文档几乎逐字重复了这段（因为 docs.rs 首页要从源码注释生成）：

[axum/src/lib.rs:3-L10](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/lib.rs#L3-L10) —— crate 级文档里的同一份特性列表。

**「Hello, World!」示例（无宏路由 + 提取器 + 响应生成一图流）**：

[axum/src/lib.rs:23-L42](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/lib.rs#L23-L42) —— 注意 `Router::new().route("/", get(...))` 里，`get` 是普通函数（无宏），`|| async { "Hello, World!" }` 这个闭包的返回值 `&'static str` 会自动通过 `IntoResponse` 变成响应。这一小段同时演示了「路由」「无宏 API」「响应生成」三个特性。

**提取器与响应的官方示例片段**（出现在 lib.rs 中段）：

[axum/src/lib.rs:78-L79](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/lib.rs#L78-L79) —— 给出提取器的定义：「一个实现了 `FromRequest` 或 `FromRequestParts` 的类型」，并用 `Path`/`Query`/`Json` 三例展示。

[axum/src/lib.rs:101-L101](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/lib.rs#L101) —— 给出响应的入口：任何实现了 `IntoResponse` 的类型都能从 handler 返回。

#### 4.2.4 代码实践

**实践目标**：在源码里逐条对号入座，把「四大特性」落实成具体的代码符号。

**操作步骤**：

1. 打开 [axum/src/lib.rs](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/lib.rs)。
2. 找到第 23–42 行的 Hello World 示例，回答：
   - 哪一行体现了「路由」？（`Router::new().route("/", get(...))`）
   - `get(...)` 体现了「无宏 API」还是「宏」？（无宏，它是普通函数）
   - 闭包返回的 `"Hello, World!"` 靠哪个 trait 变成响应？（`IntoResponse`）
3. 往下翻到 `# Extractors` 一节（约 76–97 行）和 `# Responses` 一节（约 99–129 行），记下提取器依赖的两个 trait（`FromRequest` / `FromRequestParts`）和响应依赖的一个 trait（`IntoResponse`）。

**需要观察的现象**：四大特性在示例代码里都能找到对应的「代码符号」，而不是停留在文字描述。

**预期结果**：你能在不查资料的情况下，说出「路由用 `Router`/`get`，提取器实现 `FromRequest(Parts)`，响应用 `IntoResponse`」。

#### 4.2.5 小练习与答案

**练习 1**：「macro free API（无宏路由 API）」的好处是什么？
> **参考答案**：路由注册是普通的 Rust 方法调用（如 `.route("/", get(h))`），能被编辑器自动补全、被编译器完整类型检查，重构时也更安全；不像 `#[get("/")]` 宏那样有时会绕过部分静态检查。

**练习 2**：四大特性里，哪一个不是请求处理链路上的「某一环」，而是横切贯穿全程的？
> **参考答案**：错误模型。路由匹配失败、提取失败、handler 出错都会产生错误响应，它贯穿全程，而不是链路上独立的一步。

**练习 3**：提取器要实现哪两个 trait 之一？响应返回值要实现哪个 trait？
> **参考答案**：提取器实现 `FromRequest` 或 `FromRequestParts`；响应返回值实现 `IntoResponse`。

---

### 4.3 axum 在生态中的位置：hyper / tokio / tower / tower-http

#### 4.3.1 概念说明

要真正理解 axum，必须搞清它和几个「邻居」的关系。这几个名字经常一起出现，初学者很容易混淆。我们用一句话给每个邻居定位：

- **tokio**：Rust 的异步运行时（async runtime）。它提供「异步任务调度、网络 IO、定时器」等最底层能力。几乎所有 Rust 异步网络程序都跑在 tokio 上。
- **hyper**：建在 tokio 之上的 HTTP 协议实现。它把「TCP 字节流」解析成结构化的 `http::Request` / `http::Response`，处理 HTTP/1、HTTP/2 协议细节。它很快，但 API 偏底层，不懂「路由」「业务逻辑」。
- **tower**：一个定义「服务（Service）」抽象的生态。核心是 `tower::Service` trait，它把「接收一个请求、返回一个响应」抽象成一个统一的接口。基于此，tower 提供了大量可组合的中间件（超时、限流、重试、负载均衡…）。
- **tower-http**：专门服务 HTTP 场景的 tower 中间件集合（CORS、压缩、请求追踪、静态文件、鉴权…），是 tower 生态在 Web 领域的「现成轮子库」。
- **axum**：建在 hyper 之上的「路由 + 请求处理」层。它**不是** HTTP 协议解析器（那是 hyper），**不是**运行时（那是 tokio），而是把请求「路由、提取、处理、响应」这一段业务逻辑封装好。

用一个比喻：如果搭一个 Web 服务是「开一家餐厅」：

| 角色 | 比喻 | 干什么 |
| --- | --- | --- |
| tokio | 餐厅的「水电煤气」基础设施 | 提供最底层的运行能力 |
| hyper | 「厨房设备」：烤箱、灶台 | 真正处理「生的请求字节」→「熟的结构化请求」 |
| tower / tower-http | 「标准化的厨房流程套件」：定时器、清洁、安检 | 通用的、可组合的中间件 |
| **axum** | 「前厅经理」：安排客人入座、点单、上菜 | **路由 + 请求处理**，把请求交给合适的处理者 |

axum 处在「最靠近你业务代码」的那一层，但它**向下完全依赖** hyper/tokio/tower。

#### 4.3.2 核心流程

分层关系可以用「调用方向」画清楚。一个请求从「进来到被处理」经过的层次，从下（最底层）往上（最贴近业务）是：

```
   （你的业务代码：async handler）
              ▲  axum 把请求交给你，把你的返回值变成响应
   ┌──────────┴──────────┐
   │        axum          │  路由 / 提取器 / 响应 / 错误模型
   └──────────┬──────────┘
              ▲  axum 用 tower::Service 把各层串起来
   ┌──────────┴──────────┐
   │  tower / tower-http  │  通用中间件（超时/压缩/鉴权/…）
   └──────────┬──────────┘
              ▲  请求以 http::Request 形式向上传递
   ┌──────────┴──────────┐
   │        hyper         │  HTTP 协议解析、连接管理
   └──────────┬──────────┘
              ▲  字节流 ↔ 结构化请求/响应
   ┌──────────┴──────────┐
   │        tokio         │  异步运行时、网络 IO
   └─────────────────────┘
```

关键认知：

1. **请求对象 `http::Request` 来自 `http` crate**，由 hyper 构造，被 axum、tower 层层处理。axum 在 `lib.rs` 里直接重新导出了它，方便你用。
2. **每一层只做自己擅长的事**：tokio 不管 HTTP，hyper 不管业务逻辑，axum 不管协议解析。这种分层让每层都能被单独替换或复用。
3. **axum 是「粘合剂」**：它把 hyper 给的 `Request`、tower 给的中间件能力、你的 handler，用 `tower::Service` 这一套抽象缝在一起。

#### 4.3.3 源码精读

**axum 直接重导出 `http` crate**（说明请求/响应类型来自标准 `http` 库，由 hyper 构造）：

[axum/src/lib.rs:508-L509](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/lib.rs#L508-L509) —— `#[doc(no_inline)] pub use http;`，把 `http`（包含 `http::Request`、`http::StatusCode`、`http::HeaderMap` 等）暴露给 axum 用户，意味着你日常用的这些类型其实来自 `http` crate，hyper 和 axum 共享它们。

**axum 的依赖清单（看它依赖谁，就知道它建在谁之上）**：

[axum/Cargo.toml:110-L128](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/Cargo.toml#L110-L128) —— 这里列出了 axum 的非可选依赖。可以看到几条关键证据：

- `axum-core = { path = "../axum-core", ... }`（[L111](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/Cargo.toml#L111)）：axum 建在更底层的 axum-core 之上。
- `http = "1.0.0"`、`http-body = "1.0.0"`（[L115-L116](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/Cargo.toml#L115-L116)）：使用标准 http 抽象。
- `matchit = "=0.9.2"`（[L119](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/Cargo.toml#L119)）：路径匹配树（路由的核心数据结构）由第三方 crate `matchit` 提供——又一个「不自造、复用生态」的例子。
- `tower = { version = "0.5.2", ... }`、`tower-layer = "0.3.2"`、`tower-service = "0.3"`（[L126-L128](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/Cargo.toml#L126-L128)）：**这三行是「axum 复用 tower」最直接的物证**。axum 把 `Service` 和 `Layer` 这套抽象当作一等公民。

**hyper 是「可选依赖」**（说明协议层按需启用）：

[axum/Cargo.toml:135-L136](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/Cargo.toml#L135-L136) —— `hyper` 和 `hyper-util` 都是 `optional = true`，分别由 `http1` / `http2` 等 feature 开启。这解释了为什么关掉 `http1` feature 后 `axum::serve` 会不可用（见下一模块）。

**tower-http 也被 axum 引入**（虽然主要用于文档与测试）：

[axum/Cargo.toml:149-L152](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/Cargo.toml#L149-L152) —— axum 的 `[dependencies.tower-http]` 段，开启了一大堆 tower-http 的功能特性。这印证了 README 里「充分利用 tower-http 生态」不是空话。

#### 4.3.4 代码实践

**实践目标**：通过「读依赖清单」亲手验证 axum 的分层关系。

**操作步骤**：

1. 打开 [axum/Cargo.toml](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/Cargo.toml) 的 `[dependencies]` 段（第 110 行起）。
2. 找到这几行，并判断它们各自对应「哪一层」：
   - `tokio`（在 `[features]` 和 target 段，可选）→ 运行时层
   - `hyper` / `hyper-util`（可选）→ HTTP 协议层
   - `tower` / `tower-layer` / `tower-service` → 服务/中间件抽象层
   - `tower-http` → HTTP 专用中间件层
   - `matchit` → 路由匹配层
3. 注意哪些是 `optional = true`（按需），哪些不是（必需）。

**需要观察的现象**：`tower`/`tower-layer`/`tower-service` 是**必需依赖**（不带 `optional`），而 `hyper`/`tokio` 是**可选依赖**。这说明 axum 把「tower 抽象」视为不可分割的核心，而「具体运行时/协议实现」是可插拔的。

**预期结果**：你能口述出「axum 必需 tower 三件套，可选 hyper/tokio，路由用 matchit」。

> 待本地验证：如果你想亲眼确认 `tower` 是必需依赖，可以在本地克隆仓库后用 `cargo tree -p axum`（默认 feature）观察依赖树，再对比 `cargo tree -p axum --no-default-features` 的差异。

#### 4.3.5 小练习与答案

**练习 1**：把 `tokio`、`hyper`、`axum`、`tower` 按从底到顶排序。
> **参考答案**：tokio（运行时）→ hyper（HTTP 协议）→ tower（服务/中间件抽象）→ axum（路由 + 请求处理）。

**练习 2**：为什么 axum 重导出了 `http` crate（`pub use http;`），而不是自己定义 `Request`/`Response` 类型？
> **参考答案**：因为 `http::Request` / `http::Response` 是 hyper、tower、axum 共享的标准类型。复用它们意味着 hyper 构造的请求可以无缝交给 tower 中间件和 axum handler，无需类型转换。如果 axum 自定义一套类型，就会在每一层之间产生转换开销和生态割裂。

**练习 3**：axum 的路径匹配（路由树）是自己实现的吗？
> **参考答案**：不是。它依赖第三方 crate `matchit`（版本固定为 `=0.9.2`，见 axum/Cargo.toml 第 119 行）。这是 axum「复用生态而非自造」哲学的又一个体现。

---

### 4.4 为什么复用 `tower::Service` 而非自造中间件

#### 4.4.1 概念说明

这是 axum **最关键**的设计取舍，也是 README 里明确说「让 axum 与众不同（sets axum apart）」的那一点。我们原文照引 README 最核心的一段：

> In particular the last point is what sets `axum` apart from other libraries / frameworks. `axum` doesn't have its own middleware system but instead uses `tower::Service`. This means `axum` gets timeouts, tracing, compression, authorization, and more, for free. It also enables you to share middleware with applications written using `hyper` or `tonic`.

翻译要点：

- axum **没有自己的中间件系统**，而是直接使用 `tower::Service`。
- 因此 axum 可以「免费（for free）」获得超时、链路追踪、压缩、鉴权等能力。
- 而且可以让 axum 应用和用 `hyper` 或 `tonic`（gRPC 框架）写的应用**共享同一套中间件**。

这是什么意思？很多 Web 框架会自己设计一套「中间件」机制，结果就是：每个框架的中间件互不兼容，你为框架 A 写的鉴权中间件，没法用到框架 B。axum 走了一条不同的路：它把「请求处理」这件事用 tower 社区早已定义好的 `tower::Service` trait 来表达。

**`tower::Service` 是什么？**（先给一个直觉，深入留给第 5 单元）它是一个极简的 trait，核心思想是：

```rust
// 概念示意（简化），不是 axum 真实代码
trait Service<Request> {
    type Response;
    type Error;
    fn call(&mut self, req: Request) -> Future<Output=Result<Self::Response, Self::Error>>;
}
```

也就是说，一个「Service」就是「给一个请求，还你一个（异步的）结果」。一个 handler 是 Service，一个中间件（包着另一个 Service）也是 Service，整个 Router 也是 Service。因为大家都是同一个 trait，所以它们可以**任意嵌套组合**——这正是 tower 生态的中间件能直接用到 axum 上的根本原因。

> 上面这段是**示例代码（概念示意）**，目的是让你建立直觉，不是 axum 的真实 trait 定义。`tower::Service` 的真实定义（含 `poll_ready`）会在第 5 单元讲解。

#### 4.4.2 核心流程

复用 `tower::Service` 带来的「组合性」可以用一个嵌套结构来表达。每个方框都是一个 `Service`，外层包内层：

```
                       一个请求进来
                            │
        ┌───────────────────▼───────────────────┐
        │  TraceLayer（tower-http，链路追踪）       │  ← 免费中间件
        └───────────────────┬───────────────────┘
        ┌───────────────────▼───────────────────┐
        │  TimeoutLayer（tower，超时控制）          │  ← 免费中间件
        └───────────────────┬───────────────────┘
        ┌───────────────────▼───────────────────┐
        │  CompressionLayer（tower-http，压缩）     │  ← 免费中间件
        └───────────────────┬───────────────────┘
        ┌───────────────────▼───────────────────┐
        │  Router（axum，路由分发）                 │  ← axum 核心
        │     ┌─────────────────────────────┐    │
        │     │  handler A   handler B  ... │    │  ← 每个也是 Service
        │     └─────────────────────────────┘    │
        └────────────────────────────────────────┘
```

因为每一层都是 `Service<http::Request>`，所以「trace + timeout + compression + router」可以像积木一样叠起来。你**不需要**为 axum 专门学一套新的中间件写法——你为任何 tower 应用写的中间件，这里都能直接用。

「免费」获得的典型能力（来自 README/tower/tower-http）：

| 能力 | 来源 | 作用 |
| --- | --- | --- |
| 超时（timeout） | `tower::timeout` | 请求超过时限自动取消 |
| 链路追踪（tracing） | `tower-http::trace` | 记录每个请求的方法、路径、耗时、状态码 |
| 压缩（compression） | `tower-http::compression` | 自动 gzip/br/deflate 压缩响应 |
| 鉴权（authorization） | `tower-http::auth` | 校验 Authorization 头等 |
| 限流（concurrency limit） | `tower::limit` | 限制并发请求数 |
| 静态文件（serve dir） | `tower-http::fs` | 提供静态资源服务 |

#### 4.4.3 源码精读

**axum 最核心的「设计宣言」**（README）：

[README.md:20-L24](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/README.md#L20-L24) —— 明确指出「没有自己的中间件系统，改用 `tower::Service`」，并列出免费获得的能力：timeouts、tracing、compression、authorization。`lib.rs` 中也有几乎逐字相同的一段：

[axum/src/lib.rs:12-L16](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/lib.rs#L12-L16) —— crate 级文档里同一句宣言。

**`tower` 系列是必需依赖（物证）**：

[axum/Cargo.toml:126-L128](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/Cargo.toml#L126-L128) —— `tower`、`tower-layer`、`tower-service` 三者都是**非可选**依赖。这意味着无论你开哪些 feature，tower 这套抽象始终在场——它是 axum 的地基，不是可选插件。

**`serve` 的导出取决于 tokio + http 协议 feature**（说明运行时层是「按需接入」的）：

[axum/src/lib.rs:500-L501](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/lib.rs#L500-L501) 与 [axum/src/lib.rs:529-L531](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/lib.rs#L529-L531) —— `pub mod serve` 和 `pub use ... serve` 都被 `#[cfg(all(feature = "tokio", any(feature = "http1", feature = "http2")))]` 守卫。也就是说，只有同时启用 `tokio` 和至少一个 HTTP 协议 feature，`axum::serve` 才存在。这正是「运行时/协议层可插拔」的体现：axum 的核心抽象（Router/handler/extractor）不依赖具体运行时，只有真正「跑起来」时才需要 tokio+hyper。

**Feature flags 按需裁剪协议层与能力**：

[axum/Cargo.toml:40-L88](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/Cargo.toml#L40-L88) —— `[features]` 段。注意几个要点：

- 默认 feature（`default`，[L41-L51](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/Cargo.toml#L41-L51)）包含 `http1`、`json`、`tokio` 等，开箱即用。
- `http1` / `http2`（[L59-L60](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/Cargo.toml#L59-L60)）分别开启 hyper 的对应协议支持；关掉它们，`axum::serve` 就没了。
- `json`（[L61](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/Cargo.toml#L61)）开启 `Json` 类型；关掉它，`Json` 提取器/响应就不可用（因为 `lib.rs` 里 `pub use self::json::Json` 被 `#[cfg(feature = "json")]` 守卫，见 [axum/src/lib.rs:513-L515](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/lib.rs#L513-L515)）。
- `macros`（[L62](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/Cargo.toml#L62)）开启 `debug_handler` 等调试宏（注意：这和「无宏路由 API」不矛盾——路由本身无宏，宏只是辅助开发）。

这套 feature 体系让 axum 可以被裁剪到「只保留你需要的能力」，对编译体积和依赖很友好。

#### 4.4.4 代码实践

**实践目标**：理解「关闭某个 feature 会让什么消失」，从而体会 axum 的模块化设计。

**操作步骤**：

1. 打开 [axum/src/lib.rs](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/lib.rs) 的第 513–515 行，确认 `Json` 的导出被 `#[cfg(feature = "json")]` 守卫。
2. 打开 [axum/Cargo.toml:61](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/Cargo.toml#L61)，确认 `json = ["dep:serde_json", "dep:serde_path_to_error"]`——关闭它就不会引入 serde_json。
3. 思考：如果在一个 `Cargo.toml` 里写 `axum = { version = "0.8", default-features = false, features = ["http1", "tokio"] }`（即不启用 `json`），那么代码里用 `use axum::Json;` 会发生什么？

**需要观察的现象**：`Json` 类型的存在与否完全由 `json` feature 决定。

**预期结果**：关闭 `json` feature 后，`use axum::Json;` 会编译报错（找不到 `Json`），因为它的 `pub use` 在该配置下被条件编译移除了。

> 待本地验证：在本地新建一个最小项目，分别用「默认 feature」和 `--no-default-features --features http1,tokio` 两次 `cargo build`，对比能否使用 `axum::Json`、以及编译出的二进制体积差异。这正是本系列下一单元第 3 讲（workspace 与 feature）要做的实践。

#### 4.4.5 小练习与答案

**练习 1**：axum 的 README 说它能「免费」获得超时、追踪、压缩、鉴权。这里的「免费」是什么意思？真的不付出任何代价吗？
> **参考答案**：「免费」指**不需要自己实现**这些中间件，直接拿 tower/tower-http 现成的来用，因为大家共享 `tower::Service` 抽象。代价不是没有：你需要理解 tower 的 `Service`/`Layer` 抽象，且引入了 tower 生态的依赖。但相比「自己造一套中间件且和别人不兼容」，这个代价小得多。

**练习 2**：关掉 axum 的 `http1` 和 `http2` feature 后，`axum::serve` 为什么就不可用了？这和「axum 复用 hyper」有什么关系？
> **参考答案**：`axum::serve` 依赖 hyper 来做实际的 HTTP 协议监听，而 hyper 的 `http1`/`http2` 协议支持正是由 axum 的 `http1`/`http2` feature 间接开启的（见 axum/Cargo.toml 第 59–60 行）。两个协议 feature 都关闭，就没有底层协议实现可用，`serve` 自然无法存在——这从源码上印证了「axum 把协议层完全交给 hyper」。

**练习 3**：既然 axum 强调「无宏路由 API」，为什么又提供 `macros` feature（如 `debug_handler`）？两者矛盾吗？
> **参考答案**：不矛盾。「无宏路由 API」指的是**路由注册**用普通函数/方法（`get(...)`、`.route(...)`），不依赖宏；而 `macros` feature 提供的是**辅助开发的派生/属性宏**（如 `#[debug_handler]` 帮你得到更友好的编译错误），它不是路由的必需品，可完全不用。

---

## 5. 综合实践

本次综合实践把本讲的全部内容串起来，做一个「文档阅读 + 归纳表达」的练习。它对应本讲规格里的核心实践任务。

**实践目标**：通过独立阅读官方文档，用自己的话讲清三件事——(a) axum 在 hyper 之上提供了什么；(b) 为什么 axum 选择 tower 而非自造中间件；(c) 由 tower 生态「免费」获得的 3 个能力。

**操作步骤**：

1. 完整阅读 [README.md](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/README.md)（重点第 1–24 行、第 100–106 行）。
2. 完整阅读 [axum/src/lib.rs](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/src/lib.rs) 的 crate 级文档（第 1–21 行），以及 `# Routing`/`# Handlers`/`# Extractors`/`# Responses`/`# Error handling`/`# Middleware` 各节标题（约第 47–143 行）。
3. 翻阅 [axum/Cargo.toml](https://github.com/tokio-rs/axum/blob/485c603dddcee45bb4bc40aab492b47576e2a2f8/axum/Cargo.toml) 的依赖清单，找到 `tower`/`tower-http`/`hyper`/`matchit` 这几行。
4. 用**一段话**（约 150–250 字）回答下面三个问题：
   - **(a)** axum 在 hyper 之上提供了什么？（提示：路由、提取器、响应、错误模型）
   - **(b)** 为什么 axum 选择 `tower::Service` 而不是自造一套中间件系统？（提示：组合性、生态共享、和 hyper/tonic 复用中间件）
   - **(c)** 列举 **3 个**由 tower 生态「免费」获得的能力，并各用一句话说明作用。

**需要观察的现象**：你应该能用「薄薄一层」「Service/Layer」「免费中间件」这几个关键词，把三问串成一段连贯的叙述，而不是机械复制原文。

**预期结果（参考范文，你的答案可以不同）**：

> axum 是建在 hyper 之上的「HTTP 路由与请求处理」库。hyper 只负责把字节流解析成结构化的 `http::Request`/`Response`，而 axum 在这之上补齐了 Web 框架必需的四件事：把请求路由到 handler（路由）、用提取器把请求拆成参数（提取器）、把返回值变成响应（`IntoResponse`）、以及一套可预测的错误模型。axum 之所以不自己造中间件，是因为它直接采用 `tower::Service` 这套社区标准抽象：handler、中间件、Router 都是同一个 `Service` trait，因此可以任意嵌套组合，还能和用 hyper、tonic 写的应用共享同一套中间件。由此 axum 「免费」获得了：①`tower::timeout` 的请求超时控制；②`tower-http::trace` 的请求链路追踪（记录方法/路径/耗时/状态码）；③`tower-http::compression` 的响应自动压缩（gzip/br/deflate）。

> 评分自检：如果你的回答里出现了「`tower::Service`」「`IntoResponse`」「免费 / for free」「hyper 之上」这些本讲的关键词，且没有把 axum 和 hyper 的职责搞混，就算过关。

## 6. 本讲小结

- axum 是一个**建在 hyper 之上**的 HTTP **路由与请求处理**库，强调「好用」与「模块化」，本身是「相对薄的一层」，性能接近 hyper。
- axum 自述的**四大特性**：路由（无宏 API）、提取器（声明式解析请求）、错误模型（简单可预测）、响应生成（最少样板），外加一条贯穿始终的「复用 tower/tower-http 生态」。
- 分层关系从底到顶是：**tokio（运行时）→ hyper（HTTP 协议）→ tower/tower-http（服务/中间件抽象）→ axum（路由+请求处理）**；请求/响应类型共享标准的 `http` crate（`pub use http;`）。
- axum 最关键的设计取舍是**不自造中间件，直接用 `tower::Service`**：`tower`/`tower-layer`/`tower-service` 是 axum 的**必需依赖**，这是它能「免费」获得超时/追踪/压缩/鉴权的根本原因。
- axum 通过 **Cargo feature** 按需裁剪能力：`http1`/`http2` 决定协议层（影响 `axum::serve` 是否存在），`json`/`form`/`query` 决定对应提取器是否可用，`tokio` 决定运行时是否接入。
- 当前 `main` 分支版本为 `axum 0.8.9`（见 axum/Cargo.toml 第 3 行），仓库正在向 0.9 演进（见 README 第 26–29 行），本手册基于 `main` 分支的 HEAD 讲解。

## 7. 下一步学习建议

本讲建立的是「地图」，下一步应该「上路」——亲手跑起一个 axum 程序。建议按顺序：

1. **下一讲（u1-l2 运行第一个 axum 程序）**：照着 `examples/hello-world` 真正 `cargo run` 一个 axum 服务，把本讲讲的「路由 + handler + `axum::serve`」最小闭环在终端里跑通。这是从「读文档」到「写代码」的关键一步。
2. **随后读 u1-l3（workspace 与 feature）**：在跑通程序后，回头看 axum 的 workspace 结构和 feature 体系，理解 4 个 crate 的分工，亲手做一次「关掉 feature」的对比实验。
3. **再读 u1-l4（Handler 与 Router 初体验）**：建立 `Router<S>`、`Handler` trait 的心智模型，为第 2 单元（路由系统深入）打基础。

> 阅读建议：在进入下一讲前，确保你能不看资料回答「axum 的四大特性是什么」以及「axum 为什么用 tower」。如果还含糊，重读本讲 4.2 和 4.4 两个模块。后续每一篇讲义都会假设你已经建立了本讲这套「分层 + 四大特性」的心智模型。
