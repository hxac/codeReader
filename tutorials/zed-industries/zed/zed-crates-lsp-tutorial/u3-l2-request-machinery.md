# u3-l2 请求机制：id、handler 表与 oneshot

## 1. 本讲目标

学完本讲，你应该能够：

1. 画出一次 `LanguageServer::request::<T>` 的完整链路：**分配 id → 注册 handler → 发送消息 → oneshot 等待响应**，并说清「哪些步骤发生在调用返回之前、哪些步骤发生在 await 之后」。
2. 解释 `response_handlers` 表为什么以 `RequestId` 为键、为什么由 `input_handler.rs` 的响应分发分支来触发，以及「一次性 remove」在其中的作用。
3. 理解 `LspRequestFuture::id()` 为什么存在——调用方在拿到 future 之后仍能读回请求 id，并用它做进度对账等事情。
4. 识别「server shut down」快速失败路径的三个触发点，理解为什么请求不会在服务器关闭后无限挂起。
5. 独立完成一个实践：用 `FakeLanguageServer::set_request_handler` 让 `request::HoverRequest` 先成功、再失败，观察错误如何变成 JSON-RPC error 响应、又如何被客户端还原为 anyhow 错误。

## 2. 前置知识

本讲建立在 u1-l2（消息模型）、u1-l3（stdout 分帧）与 u2-l2（IO 任务管线）之上，先回顾四个概念。

**请求与响应的配对问题。** JSON-RPC 2.0 里请求带 `id`，响应必须回带同一个 `id`。客户端可能同时有多个在途请求（悬停、格式化、补全……），所以「响应来了该唤醒谁」是一张查表问题：键是 `RequestId`（[src/lsp.rs:L225-L233](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L225-L233)，u1-l2 讲过它是 untagged 的 `Int(i32)` / `Str(String)` 二态枚举），值是「收到结果后该干什么」的回调。

**oneshot 通道。** `futures::channel::oneshot` 是一次性通道：`tx` 只能发一次，`rx` 收到（或发现 `tx` 被丢弃）后即完成。它正是「一个在途请求 ← 一个响应」这对一对多里的一对一关系，也是本讲等待方的核心原语。若 `tx` 在未发送前就被 drop，`rx` 会立刻得到 `Err(Canceled)`——这个信号在本讲里被翻译成「连接被重置」。

**同步部分与异步部分的分裂。** `request` 不是 `async fn`，而是一个返回 future 的普通方法。这意味着函数体内的代码（分配 id、注册 handler、入队消息）在**调用返回的那一刻**就全部执行完了；返回的 future 只负责「等响应 + 超时竞争」。这个分裂是理解本讲的钥匙：注册和发送永远不会被 `.await` 拖延，而等待可以随时被放弃（放弃即触发下一讲的 `$/cancelRequest`）。

**request::Request trait。** 与 u3-l1 讲过的 `notification::Notification` 对称，lsp-types（Zed fork，[Cargo.toml:L678](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/Cargo.toml#L678)）为每种请求定义了标记类型，实现 `request::Request` trait。从 crate 内使用处可直接读出其形状：关联常量 `METHOD`（`&'static str`，如 `"textDocument/hover"`）、关联类型 `Params` 与 `Result`——例如 [src/lsp.rs:L1964-L1974](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1964-L1974) 的 `FakeLanguageServer::request<T>` 同时用到了 `T::Params` 和 `T::Result`。方法名、参数类型、返回类型在编译期锁死为一对。

## 3. 本讲源码地图

| 文件 | 行号区间 | 作用 |
| --- | --- | --- |
| `src/lsp.rs` | L81 | `ResponseHandler` 类型别名：响应回调的形状 |
| `src/lsp.rs` | L115-L143 | `LanguageServer` 结构体：本讲涉及 `next_id`、`outbound_tx`、`response_handlers` 三个字段 |
| `src/lsp.rs` | L258-L268 | 出站请求结构体 `Request<'a, T>` |
| `src/lsp.rs` | L333-L365 | `LspRequestFuture` trait 与 `LspRequest` 包装器 |
| `src/lsp.rs` | L518-L523 | `new_internal`：`response_handlers` 以 `Some(HashMap)` 起始 |
| `src/lsp.rs` | L659-L671 | 入站任务退出时作废响应表（触发点之一） |
| `src/lsp.rs` | L752-L758 | 写出任务退出时作废响应表（触发点之二） |
| `src/lsp.rs` | L1111-L1128 | `shutdown` 快照计数器并复用 `request_internal` |
| `src/lsp.rs` | L1158 | `shutdown` 显式作废响应表（触发点之三） |
| `src/lsp.rs` | L1405-L1448 | `request` / `request_with_timer`：公开入口 |
| `src/lsp.rs` | L1450-L1558 | `request_internal_with_timer`：本讲主角 |
| `src/lsp.rs` | L1560-L1599 | `request_internal` 与 `request_timeout_future`：默认超时封装 |
| `src/lsp.rs` | L1249-L1292 | `on_custom_request`：fake 端把 handler 的 `Err` 变成 JSON-RPC error 响应 |
| `src/lsp.rs` | L1995-L2026 | `FakeLanguageServer::set_request_handler`：实践的注册口 |
| `src/lsp.rs` | L2105-L2189 | `test_fake`：实践的样板测试 |
| `src/input_handler.rs` | L53-L68 | `LspStdoutHandler::new`：接收 `response_handlers` 表 |
| `src/input_handler.rs` | L108-L135 | 两分支分发中的响应分支（本讲 4.3 的主角） |
| `crates/util/src/util.rs` | L771-L792 | `ConnectionResult` 三态终局与 `into_response` |
| `crates/project/src/lsp_store.rs` | L5624-L5664 | 上游真实调用方：`id()` 的实际用途 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：`request` 类型化入口（4.1）→ `request_internal_with_timer` 核心链路（4.2）→ `input_handler.rs` 的响应分发分支（4.3）→ `LspRequest` / `LspRequestFuture` 包装器（4.4）。

### 4.1 request：类型化的请求发送口

#### 4.1.1 概念说明

调用方想要一个「发出去、等回来」的回答时，用 `request`：

```rust
let hover = server
    .request::<request::HoverRequest>(hover_params, request_timeout)
    .await;
```

与 `notify` 一样，泛型参数 `T` 同时决定三件事：`T::METHOD` 决定线上的方法字符串，`T::Params` 决定参数类型，`T::Result` 决定响应反序列化成什么类型。与 `notify` 不同的是：请求需要**超时**参数（`Duration`），且返回的是一个 future 而不是 `Result<()>`——因为有响应要等。

注意 `request` 只接收 `&self`。它要改的所有可变状态都不需要 `&mut`：请求 id 计数器是原子量，handler 表在 `Arc<Mutex<...>>` 里。这意味着任意多个调用方可以在不同线程同时发请求，互不阻塞。

#### 4.1.2 核心流程

公开入口只是薄薄的转发层：

```text
LanguageServer::request::<T>(params, timeout)              L1408
  └─ Self::request_internal::<T>(                          L1416
       &self.next_id,            ← 原子计数器（id 的唯一来源）
       &self.response_handlers,  ← 响应回调表（Arc 共享给 input_handler）
       &self.outbound_tx,        ← 直发写出通道（不经过序列化后台任务！）
       &self.notification_tx,    ← 序列化通道（只给 cancel_on_drop 用）
       &self.executor, timeout, params)

LanguageServer::request_with_timer::<T, U>(params, timer)  L1431
  └─ 同上，但把「定时器 future」也作为参数传入
```

两个入口最终都汇入 `request_internal_with_timer`（4.2）。区别只在超时的形态：`request` 收 `Duration`，由 `request_timeout_future` 把它变成一个 `Future<Output = String>`（[src/lsp.rs:L1586-L1599](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1586-L1599)；`Duration::MAX` 与零都映射为永不完成的 `pending`，细节留给 u3-l3）；`request_with_timer` 干脆让调用方自带任意定时器。

#### 4.1.3 源码精读

- [src/lsp.rs:L1405-L1425](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1405-L1425)：`request` 的函数体只有一次转调。文档注释链接到 LSP 规范的 requestMessage 一节。返回类型 `impl LspRequestFuture<T::Result> + use<T>`——`use<T>` 是 Rust 的返回类型捕获语法，表示这个 future 只捕获类型参数 `T`（连同按值移入的 params），**不借用 `self`**，所以它可以被随意移动、存进结构体、跨 `await` 边界传递。
- [src/lsp.rs:L1427-L1448](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1427-L1448)：`request_with_timer` 形状相同，多一个 `timer: U where U: Future<Output = String>`——定时器到期时产出的字符串会直接进错误日志（见 4.2.3 的 `message` 变量），所以它其实是「超时描述文本」而不只是闹钟。
- [src/lsp.rs:L258-L268](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L258-L268)：序列化目标 `Request<'a, T>` 四个字段：`jsonrpc`、`id: RequestId`、`method: &'a str`（借用 `T::METHOD` 的 `&'static str`，零拷贝）、`params: T`（`skip_serializing_if = "is_unit"`，无参请求不会多出 `"params":null`）。

为什么底层函数要收一堆散装引用而不收 `&self`？`shutdown` 给出了答案：

- [src/lsp.rs:L1111-L1128](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1111-L1128)：`shutdown` 需要在 `self` 已经不能被安全借用的 `'static` future 里发 Shutdown 请求，于是它把所需状态逐个克隆出来（L1114-L1119），其中**计数器是快照**：`AtomicI32::new(self.next_id.load(SeqCst))`（L1115）——新建一个从当前值起步的计数器，而不是带走原计数器。随后以散装参数调用 `request_internal`（L1120-L1128）。u2-l4 讲过这段的关闭语义，本讲只需要记住：**松散参数让请求机制可以被任何持有这几样东西的代码复用**。

#### 4.1.4 代码实践：盘点 request 的调用面

这是一个源码阅读型实践。

1. **实践目标**：建立「上游怎么用 request、超时从哪来」的直观印象。
2. **操作步骤**：
   - 在仓库根目录执行 `rg '\.request::<' crates --glob '*.rs' | head -30`。
   - 挑出 `crates/project/src/lsp_store.rs` 中的调用，观察 `request_timeout` 的来源：它来自 `ProjectSettings::get_global(cx).global_lsp_settings.get_request_timeout()`（[crates/project/src/lsp_store.rs:L5620-L5622](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/project/src/lsp_store.rs#L5620-L5622)）——也就是用户设置里的请求超时，最终汇入 `DEFAULT_LSP_REQUEST_TIMEOUT`（[src/lsp.rs:L50-L55](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L50-L55)，120 秒）作为兜底。
   - 对比同一文件里 `notify` 的调用点，确认「等待答案的用 request、告知事件的用 notify」。
3. **需要观察的现象**：几乎所有调用点拿到 future 后的第一件事要么是 `.await`，要么先 `.id()` 再 `await`（见 4.4）。
4. **预期结果**：`project`、`language`、`copilot` 等 crate 中能找到数十处调用。本环境未实际执行，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`request` 有三个泛型来源：方法签名的 `T: request::Request`、参数 `params: T::Params`、超时 `request_timeout: Duration`。为什么超时不是 `T` 的一部分，而要调用方每次传？

**答案**：方法名与参数类型、结果类型是**协议属性**，由 LSP 规范固定，放进标记类型合理；超时是**调用方策略**——同一个 `Shutdown` 请求在 `shutdown()` 里用 5 秒（`SERVER_SHUTDOWN_TIMEOUT`，[src/lsp.rs:L58](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L58)），普通交互请求用用户配置的 120 秒。把策略塞进协议标记类型会让「同一方法、不同时限」无法表达。

**练习 2**：`request` 的消息走 `outbound_tx` 直发，而 `notify` 的消息走 `notification_tx` 序列化通道。回看 u3-l1，说出为什么请求可以「直发」。

**答案**：u3-l1 讲过，`notification_tx` 通道装的是「序列化闭包」，目的是把 `serde_json::to_string` 从前台线程挪到后台。而 `request` 的序列化发生在**调用点**（4.2.3 的 L1465），调用方本来就可能不在前台（很多请求在 `cx.spawn` 的异步上下文里发起）；更重要的是请求必须**同步地**在返回前完成注册与入队，否则「先注册 handler 后发送」的顺序无法保证，所以它选择在调用点一次性付清序列化代价，直接投递序列化好的 `String`。

### 4.2 request_internal_with_timer：分配 id、注册 handler、发送与等待

#### 4.2.1 概念说明

这是本讲的核心：一个约 110 行的关联函数，把「一次请求」拆成时间上分离的两段。**同步段**在 `request()` 返回前跑完——分配 id、序列化、注册回调、入队发送；**异步段**封装进返回的 future——等 oneshot 或定时器先到。两段之间靠三样东西连接：`RequestId::Int(id)` 是查表键，oneshot 通道是唤醒管道，`Instant::now()` 记录起点用于延迟日志。

理解这段代码的另一个关键是 `ResponseHandler` 的形状（[src/lsp.rs:L81](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L81)）：

```rust
type ResponseHandler = Box<dyn Send + FnOnce(Result<String, Error>) -> Task<()>>;
```

它吃 `Result<String, Error>`——**还没反序列化的 JSON 文本或错误结构**，返回一个 `Task<()>`。也就是说，响应到达时的反序列化也不在调用线程做，而是被丢给后台执行器（见 4.2.3）。`FnOnce` 意味着回调只能被调用一次，与「一次性 remove」的表语义互相印证。

#### 4.2.2 核心流程

```text
【同步段】调用线程，request() 返回前完成
  ├─ next_id.fetch_add(1, SeqCst)                            L1464  原子分配请求 id
  ├─ serde_json::to_string(&Request { id, method, params })  L1465  此刻序列化完整请求
  ├─ oneshot::channel()                                      L1473  创建一次性回传通道 (tx, rx)
  ├─ response_handlers.lock().as_mut()                       L1474
  │    ├─ 表为 None → Err("server shut down")                L1477  快速失败①
  │    └─ 表为 Some → handlers.insert(RequestId::Int(id), 闭包) L1480
  │                闭包：收到 String/Error → 后台反序列化 → tx.send(...)
  ├─ outbound_tx.try_send(message)                           L1501  直发写出通道
  │    └─ 失败 → Err("failed to write to language server's stdin")  快速失败②
  └─ 返回 LspRequest::new(id, async move { … })              L1508

【异步段】调用方 await 时才执行
  ├─ 注册/发送已失败？→ 立即 ConnectionResult::Result(Err)    L1509-L1514
  ├─ 安装 cancel_on_drop：future 被 drop 时发 $/cancelRequest  L1516-L1526（u3-l3 详述）
  └─ select!                                                 L1529
       ├─ rx（oneshot）先到 → ConnectionResult::Result(响应)  L1530-L1541
       │     └─ rx 得到 Err(Canceled) → ConnectionResult::ConnectionReset
       └─ timer 先到 → 从表中 remove 该 id → ConnectionResult::Timeout  L1543-L1555
```

「server shut down」快速失败的触发条件是 `response_handlers` 表变成了 `None`。这张表的定义在 [src/lsp.rs:L131](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L131)：`Arc<Mutex<Option<HashMap<RequestId, ResponseHandler>>>>`——外层 `Option` 就是「会话是否还活着」的开关。`new_internal` 把它初始化为 `Some(空表)`（[src/lsp.rs:L522-L523](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L522-L523)），之后有**三个地方**会把它 `take()` 成 `None`：

| 触发点 | 位置 | 含义 |
| --- | --- | --- |
| 入站任务退出 | [src/lsp.rs:L660-L665](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L660-L665) | stdout 断了，响应永远不会再到达 |
| 写出任务退出 | [src/lsp.rs:L753-L758](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L753-L758) | stdin 断了，请求根本发不出去 |
| `shutdown()` 显式作废 | [src/lsp.rs:L1158](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1158) | 主动关闭，拒绝新请求 |

任何一个先发生，后续所有 `request` 调用都会在同步段拿到 `Err("server shut down")` 并立刻以 `ConnectionResult::Result(Err)` 结束——这就是「快速失败路径」：**不发送、不注册、不等待**。

#### 4.2.3 源码精读

**同步段四步。**

- [src/lsp.rs:L1464-L1471](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1464-L1471)：第一步 `fetch_add` 返回旧值并自增，所以第一个请求的 id 是 `0`（计数器在 [src/lsp.rs:L631](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L631) 以 `Default::default()` 起步）；`SeqCst` 顺序保证多线程分配的 id 严格唯一。第二步立刻把完整请求序列化成 `String`，`id` 字段包成 `RequestId::Int(id)`——**客户端发出的请求 id 永远是整数**，字符串 id 只出现在对接第三方服务器响应时。
- [src/lsp.rs:L1473-L1499](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1473-L1499)：创建 oneshot，然后注册回调。`.context("server shut down")` 作用在 `Option` 上：`None`（表已作废）被转成携带该消息的 `Err`。注意整个 `lock()` 守卫的生命周期很短——插入完成即释放，绝不在持锁状态下做任何 IO。注册的闭包是响应的「落地动作」：
  - `Ok(response)` → `deserialize_result::<T::Result>(&response)`（辅助函数在 [src/lsp.rs:L247-L253](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L247-L253)，u1-l2 讲过它对 unit 类型的特殊处理）；反序列化失败会 `log::error!` 记下完整原文再返回错误，**不会静默吞掉**。
  - `Err(error)` → `Err(anyhow!("{}", error.message))`——**JSON-RPC error 的 message 字段原样变成 anyhow 错误字符串**，这正是综合实践中「错误还原」的落点。
  - 最后 `tx.send(response).ok()`：`.ok()` 忽略发送失败——失败只意味着等待方已经放弃（future 被 drop），无需处理。
  - 整段被包在 `executor.spawn(...)` 里：这个 `executor` 是 `BackgroundExecutor`（[src/lsp.rs:L136](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L136)），所以响应反序列化在**后台线程**执行——调用回调的是 stdout 读取路径，不该在它身上扛大响应的解析开销。
- [src/lsp.rs:L1501-L1503](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1501-L1503)：`try_send` 而非 `send().await`——`request` 不是 async 函数，没有 await 点可用；`outbound` 是无界通道，`try_send` 唯一的失败情形就是通道已关闭（服务器在关闭）。
- [src/lsp.rs:L1505-L1507](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1505-L1507)：克隆 `Arc`、把序列化通道 sender **降级为 `Weak`**（供 cancel_on_drop 用，避免 future 存活就拖住整个服务器）、记录 `started` 时刻。

**异步段。**

- [src/lsp.rs:L1508-L1514](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1508-L1514)：future 体内最先检查同步段的两笔「欠账」。若有失败，直接返回 `ConnectionResult::Result(Err)`——快速失败路径的出口。
- [src/lsp.rs:L1516-L1526](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1516-L1526)：`gpui_util::defer` 安装的 `cancel_on_drop` 只在这段 async 块**被 drop 且未完成**时触发，通过升级 `Weak` sender 发送 `$/cancelRequest`（参数 `CancelParams { id: NumberOrString::Number(id) }`）。机制细节留给 u3-l3。
- [src/lsp.rs:L1529-L1556](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1529-L1556)：`select!` 让响应与定时器竞争。响应分支先 `cancel_on_drop.abort()`（收到答案就不再需要取消），然后 `log::trace!` 记录 ` Took {elapsed:?} to receive response to {method:?} id {id}`——`started` 在这里兑现为延迟观测；`Err(Canceled)` 分支对应 oneshot 发送端被 drop（等待方之后的整条响应链路没了），翻译成 `ConnectionResult::ConnectionReset`。定时器分支先从表里 `remove` 该 id（超时的请求不再接收迟到的响应），再返回 `ConnectionResult::Timeout`；`remove` 之前同样要过 `context("server shut down")` 这道关。`ConnectionResult` 的三种终局定义在 [crates/util/src/util.rs:L771-L786](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/util/src/util.rs#L771-L786)，`into_response()` 把 `Timeout` / `ConnectionReset` 分别转成 `"Request timed out"` / `"Server reset the connection"` 的 anyhow 错误。

#### 4.2.4 代码实践：观察 id 序列与延迟日志

1. **实践目标**：亲眼看到「id 从 0 递增」「响应延迟被记录」两件事。
2. **操作步骤**：
   - 在本地 checkout 中给 `test_fake`（[src/lsp.rs:L2105](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L2105)）临时加上对 initialize 的观察即可（无需改产品代码，也可直接用环境变量）：
     ```bash
     RUST_LOG=trace cargo test -p lsp test_fake -- --nocapture
     ```
   - 在输出里找两类行：`outgoing message:{"jsonrpc":"2.0","id":0,"method":"initialize",...}`（来自 [src/lsp.rs:L761](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L761) 的写出日志）与 `Took … to receive response to "initialize" id 0`（来自 L1532 的响应日志）。
3. **需要观察的现象**：`initialize` 请求的 id 是 `0`；测试结尾 shutdown 期间的 Shutdown 请求 id 会**从计数器快照处**继续（对照 4.1.3 的 L1115），而不是重新从 0 开始。
4. **预期结果**：如果 trace 日志被测试初始化器（`init_logger`，[src/lsp.rs:L2100-L2103](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L2100-L2103) 的 `zlog::init_test()`）过滤掉，则看不到这些行——此时可改用 `cargo test -p lsp test_fake -- --nocapture` 配合 `ZLOG` 相关环境变量，具体过滤策略**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：假设两个线程同时调用 `request`，id 分配分别为 5 和 6。响应以 `{"id":6,...}` 先回来。请描述此时 `response_handlers` 表与两个等待 future 的状态。

**答案**：id 5 与 6 各有一条表项，键分别为 `RequestId::Int(5)`、`RequestId::Int(6)`，值是各自捕获了自己 oneshot `tx` 的 `FnOnce` 闭包。id 6 的响应到达时，input_handler 分支执行 `handlers.remove(&RequestId::Int(6))`——id 6 的表项被**一次性摘除**，其闭包在后台反序列化后 `tx.send`，唤醒 id 6 的等待方；id 5 的表项原封不动，它的 future 继续在 `select!` 里挂起，直到自己的响应到来或定时器先响。两个请求互不干扰，这正是「以 id 为键的表 + 每请求一个 oneshot」设计要达到的效果。

**练习 2**：如果注册 handler 成功、`try_send` 却失败了（快速失败②），表里那条刚插入的表项怎么办？会泄漏吗？

**答案**：不会永久泄漏。这条表项会随会话终结被清理——`try_send` 失败说明 outbound 通道已关闭，即写出任务已在退出路径上，其 `defer`（L753-L758）会把整张表 `take()` 成 `None`，旧 `HashMap` 连同闭包一起被丢弃。代价只是「失败到清理之间」的短暂窗口内多了一条死表项；调用方本身立即拿到 `ConnectionResult::Result(Err("failed to write to language server's stdin"))`，不会挂起等待。

### 4.3 input_handler.rs 中的响应分发分支

#### 4.3.1 概念说明

响应从服务器 stdout 回到客户端的「入口」不在 `handle_incoming_messages` 主循环，而在它**下游一层**的 `LspStdoutHandler::handler` 里。u1-l3 讲过两分支分发：先试严格的 `NotificationOrRequest`（必有 `method`），失败再试宽松的 `AnyResponse`（必有 `id`）。本讲聚焦第二个分支——它就是 4.2 注册的回调被触发的地方。

这里有个容易忽略的结构性事实：`response_handlers` 这张表被 `Arc` 共享给了 `LspStdoutHandler`（构造于 [src/lsp.rs:L666-L671](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L666-L671)，传入 [src/input_handler.rs:L53-L68](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L53-L68) 的 `new`）。所以响应根本**不经过** `incoming_messages` 通道、不经过前台分发循环——后台读帧线程查到表项就直接调用，回调再把活儿丢给后台执行器。通知走「后台读 → 有界通道 → 前台分发」，响应走「后台读 → 直接回调」，两条路径的分叉点就是那个两分支。

#### 4.3.2 核心流程

```text
服务器 stdout 字节流
  └─ LspStdoutHandler::handler（后台线程循环）        input_handler.rs L83
       ├─ read_headers → 解析 Content-Length → read_exact 读定长体   L86-L99
       ├─ io_handlers 记录原始文本（IoKind::StdOut）  L101-L106
       ├─ 分支一：能解析成 NotificationOrRequest？    L108-L109
       │    └─ incoming_messages.send(msg).await（有界通道，u4-l1 的背压点）
       └─ 分支二：能解析成 AnyResponse？              L110-L128
            ├─ handlers.remove(&id)                   L114-L119  ← 一次性摘除
            │    （表为 None 或无此 id → 静默丢弃该响应）
            ├─ error 字段存在 → handler(Err(error)).await          L121-L122
            ├─ result 字段存在 → handler(Ok(result.get().into()))  L123-L124
            └─ 两者皆缺     → handler(Ok("null".into()))           L125-L127
                 ↓ 唤醒 4.2 注册的闭包（后台反序列化 → oneshot → select!）
```

`remove` 是关键动词：查表与摘除是**同一个原子动作**（单次 `HashMap::remove`），迟到的重复响应、超时后才到的响应都查不到表项，自然被丢弃。`handler(...)` 的三种入参对应 JSON-RPC 响应的三种形态；`result.get()` 取出 `RawValue` 内的原始 JSON 文本（u1-l2 讲过的零拷贝借用），`.into()` 转成 `String` 交给回调——**此刻仍不反序列化成具体类型**，类型化推迟到回调内部的后台任务。

#### 4.3.3 源码精读

- [src/input_handler.rs:L108-L109](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L108-L109)：分支一。`serde_json::from_slice::<NotificationOrRequest>` 只在有 `method` 字段时成功（u1-l3 讲过判别依据），成功的消息进入有界通道。
- [src/input_handler.rs:L110-L119](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L110-L119)：分支二。解构出 `AnyResponse { id, error, result, .. }` 后，先在**独立的块**里完成 `remove`——这样锁守卫在调用 handler 之前就释放了，回调执行期间绝不持锁。`response_handlers.lock()` 得到的是 `Mutex<Option<HashMap>>`，所以链式是 `.as_mut().and_then(|handlers| handlers.remove(&id))`：表为 `None`（会话已关闭）或 `id` 不在表中，都得到 `None`。
- [src/input_handler.rs:L120-L128](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L120-L128)：`if let Some(handler)` 有表项才继续。三个 case 依次判断 `error`、`result`、兜底 `"null"`——u1-l3 讲过这个归一化：某些服务器对空结果既不给 `result` 也不给 `error`，客户端统一当成 `Ok("null")`，让 `deserialize_result` 的 unit 类型特殊处理去消化它。注意 `handler(...)` 的返回值 `Task<()>` 被直接丢弃——`Task` 未存储也不 detach 会发生什么？这里返回的正是 4.2 闭包里 `executor.spawn` 的产物，而实际工作在闭包被调用时已经开始；调用点对它的丢弃在本代码路径下是无害的（后台 spawn 的任务在 GPUI 的后台执行器上运行至完成）。此外两个分支都失败时只 `warn!` 不终止循环（L129-L134）——坏一条消息不拆掉整个会话。
- 对照客户端这侧的闭包（[src/lsp.rs:L1482-L1497](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1482-L1497)）：`Ok(String)` → `deserialize_result::<T::Result>`；`Err(Error)` → `anyhow!("{}", error.message)`。**错误在 fake 侧的生成点**则要看 `on_custom_request`：[src/lsp.rs:L1276-L1284](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1276-L1284) 把 handler 的 `Err(error)` 包装成 `LspResult::Error(Some(Error { code: REQUEST_FAILED, message: error.to_string(), .. }))` 序列化回写。于是综合实践里的错误旅程是：`Err(anyhow!("…"))` → JSON-RPC error（code -32803）→ 客户端 `Err(error.message)` → `anyhow!("{}", message)`。
- [src/input_handler.rs:L53-L68](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L53-L68)：`LspStdoutHandler::new` 用 `cx.spawn`（`cx` 是 `BackgroundExecutor`）把 handler 循环放到后台——这是「响应路径不过前台」的落点。

#### 4.3.4 代码实践：手工喂一条响应帧，验证表项被触发

在 `src/input_handler.rs` 底部的 `tests` 模块里仿照 [test_backpressure_when_messages_are_not_consumed](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L146-L188) 写一个测试（**示例代码**，需在本地 checkout 中添加）：

```rust
#[gpui::test]
async fn test_response_dispatch(cx: &mut TestAppContext) {
    use futures::channel::oneshot;

    let (tx, rx) = oneshot::channel();
    let executor = cx.background_executor().clone();
    let mut handlers = HashMap::default();
    handlers.insert(
        RequestId::Int(7),
        Box::new(move |result| {
            let tx = tx.clone(); // 闭包是 FnOnce，直接 move 亦可
            executor.spawn(async move { tx.send(result).ok(); })
        }) as ResponseHandler,
    );

    let (mut writer, reader) = async_pipe::pipe();
    let _handler = LspStdoutHandler::new(
        reader,
        Arc::new(Mutex::new(Some(handlers))),
        Arc::new(Mutex::new(HashMap::default())),
        cx.background_executor().clone(),
    );

    let payload = r#"{"jsonrpc":"2.0","id":7,"result":{"contents":"hi"}}"#;
    let message = format!("Content-Length: {}\r\n\r\n{}", payload.len(), payload);
    cx.background_executor()
        .spawn(async move {
            use futures::AsyncWriteExt;
            writer.write_all(message.as_bytes()).await.unwrap();
        })
        .detach();

    cx.run_until_parked();
    let result = rx.await.unwrap();
    assert!(matches!(result, Ok(ref s) if s.contains(r#""hi""#)));
}
```

1. **实践目标**：不经任何真实/fake 服务器，直接验证「id 为 7 的响应帧 → 表项被一次性调用 → oneshot 收到原始 JSON 文本」。
2. **操作步骤**：把测试加进 `mod tests`，运行 `cargo test -p lsp test_response_dispatch`。
3. **需要观察的现象**：`result` 是 `Ok(String)`，内容正是 `result` 字段的原始 JSON（`{"contents":"hi"}`），**不含** `id`/`jsonrpc` 等信封字段——因为 `AnyResponse` 只把 `RawValue` 的内芯交出来。
4. **预期结果**：断言通过。可再扩展：把帧改成 `{"jsonrpc":"2.0","id":8,"error":{"code":-32603,"message":"boom"}}`（换一个不存在的键或同一个键），观察「表里没有的 id」如何被静默丢弃、`Error` 结构如何走 `Err` 分支。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么响应不设计成「也进 `incoming_messages` 通道，由前台循环统一分发」？

**答案**：三个理由。其一，通道容量只有 128（`INCOMING_MESSAGE_QUEUE_CAPACITY`，[src/input_handler.rs:L27](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L27)），响应混进去会与通知争抢背压名额，前台卡顿时连请求响应一起堵；其二，响应回调不需要 `&mut AsyncApp`（通知 handler 才需要），没有必须上前台的理由；其三，响应的消费者是已经存在的「等待中的 future」，直接回调是最短路径，绕经通道再查一次表纯属浪费。

**练习 2**：`handler(Ok("null".into()))` 这个兜底分支，最终会走到 4.2 闭包的哪一行？结果是什么？

**答案**：走到 L1486 的 `Ok(response) => deserialize_result::<T::Result>(&response)`，`response` 为 `"null"`。若 `T::Result` 是 `()`（如 `request::Shutdown`），`deserialize_result` 的 unit 特殊处理（[src/lsp.rs:L247-L253](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L247-L253)）直接从字面量 `"null"` 反序列化出 `Ok(())`；若是普通类型，`Option<Hover>` 这类能从 `null` 反序列化出 `None` 的类型也得 `Ok(None)`，不能消化 `null` 的类型则报「failed to deserialize response」错误并记日志。

### 4.4 LspRequest / LspRequestFuture：暴露请求 id 的 future 包装器

#### 4.4.1 概念说明**

`request` 返回的不是裸 future，而是一个「记得自己 id」的 future。这套设计由两个类型组成：

- `LspRequestFuture<O>`（trait）：既是 `Future<Output = ConnectionResult<O>>`，又多一个 `id(&self) -> i32` 方法——**在 await 之前**就能读出这次请求分到的 id。
- `LspRequest<F>`（结构体）：任意 future `F` 外面套一层 `id` 字段的包装器，实现上述 trait。

为什么要对外暴露 id？请求 id 在 crate 内部的用途是响应配对，那对调用方是透明的；但 LSP 生态里 id 还有第二个身份——**进度令牌（progress token）**等其他协议字段经常直接复用请求 id（服务器端用同一个数字回报与该请求相关的工作进度）。调用方若想在发起请求的同时登记「这个 id 对应的进度」，就必须在 await 之前拿到 id。此外 id 也能用于日志对账、超时后手动发 `$/cancelRequest` 等场景。

#### 4.4.2 核心流程

```text
LanguageServer::request::<T>(params, timeout)
  └─ 返回 LspRequest { id, request: async { … } }       L1508
       │
       ├─ 调用方先取 id：lsp_request.id()  → i32        （不消费 future）
       └─ 调用方再 await：lsp_request.await → ConnectionResult<T::Result>
```

包装器对 future 的行为完全透明：`poll` 只是把唤醒转投给内层 future。

#### 4.4.3 源码精读

- [src/lsp.rs:L333-L335](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L333-L335)：trait 定义只有两行——继承 `Future<Output = ConnectionResult<O>>`，加上 `fn id(&self) -> i32`。注意 `id` 取 `&self`：读 id 不需要、也不会消费 future，之后照常 await。
- [src/lsp.rs:L337-L346](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L337-L346)：包装器就是 `{ id: i32, request: F }` 加一个构造函数。泛型 `F` 无约束——任何 future 都能被包。
- [src/lsp.rs:L348-L356](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L348-L356)：`Future` 实现。这里出现了 crate 里少见的 `unsafe`：标准的手写 pin 投影（`Pin::new_unchecked(&mut self.get_unchecked_mut().request)`），注释说明依据——外层已 pinned 则字段必然 pinned。这是「包装 future 而不改变其行为」的惯用写法，Zed 通常用 `pin-project` 类工具规避手写，此处选择了手写并给出 SAFETY 注释。
- [src/lsp.rs:L358-L365](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L358-L365)：只要内层 `F: Future<Output = ConnectionResult<O>>`，`LspRequest<F>` 就实现 `LspRequestFuture<O>`，`id()` 原样返回字段。

**真实调用方**（仓库内能找到的 `id()` 消费点）在 project crate 的请求总入口：

- [crates/project/src/lsp_store.rs:L5624-L5627](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/project/src/lsp_store.rs#L5624-L5627)：先拿到 `lsp_request`，紧接着 `let id = lsp_request.id();`——**在 await 之前**取 id。
- [crates/project/src/lsp_store.rs:L5628-L5644](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/project/src/lsp_store.rs#L5628-L5644)：用这个 id 构造 `ProgressToken::Number(id)` 调 `on_lsp_work_start`——把请求 id 当作进度令牌登记「服务器正在为此请求工作」的状态栏提示；请求结束后（L5648-L5659 的 `defer`）再以同一 token 调 `on_lsp_work_end`。
- [crates/project/src/lsp_store.rs:L5664](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/project/src/lsp_store.rs#L5664)：最后 `lsp_request.await.into_response()` 把三态终局压平成 `anyhow::Result`。

顺带一提，fake 侧也有同名转发口：[src/lsp.rs:L1964-L1974](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1964-L1974) 的 `FakeLanguageServer::request`，让测试能从假服务器一侧发起 server→client 请求（方向与 `LanguageServer::request` 相反，机制走的是 u3-l4 的 `on_request` 路径）。

#### 4.4.4 代码实践：追踪 id() 的真实用途

1. **实践目标**：亲手验证「请求 id 被复用为进度令牌」这条链路。
2. **操作步骤**：
   - 精读 [crates/project/src/lsp_store.rs:L5619-L5664](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/project/src/lsp_store.rs#L5619-L5664)：注意 `status`（L5619）非空才登记进度——没有状态文案的请求不会显示进度。
   - 在仓库根目录执行 `rg 'ProgressToken::Number' crates/project/src --glob '*.rs'`，观察还有哪些地方以数字令牌匹配进度事件。
3. **需要观察的现象**：`id()` 的返回值与 `on_lsp_work_start` 收到的 token 是同一个数字；请求完成（无论成败）后 `defer` 保证 `on_lsp_work_end` 必然被调用。
4. **预期结果**：能说出「若 `LspRequestFuture` 不暴露 id，project 就得自己另造一套 token 并维护 token↔请求 的映射」——这正是该 trait 存在的价值。本环境未实际执行 rg，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`LspRequestFuture::id` 返回 `i32` 而不是 `RequestId`，为什么？

**答案**：因为本客户端发出的请求 id **只会是整数**——4.2 里分配 id 用的是 `AtomicI32` 的 `fetch_add`，序列化时包成 `RequestId::Int(id)`。`RequestId::Str` 变体只为**接收**第三方服务器发来的字符串 id 而存在（作为响应表与 server→client 请求的键）。返回 `i32` 让调用方（如 `ProgressToken::Number(id)`）无需匹配枚举。

**练习 2**：`LspRequest` 的 `Future` 实现为什么必须手写 `unsafe` pin 投影？如果去掉 `unsafe`、直接 `self.request.poll(cx)` 会怎样？

**答案**：`Pin<&mut Self>` 不能直接给出 `&mut F`——`F` 可能是自引用 future（内含指向自身的指针），把它当可移动对象取出会破坏其不变量，这正是 `Pin` 存在的意义。手写投影通过 `get_unchecked_mut` 承诺「外层 pinned ⇒ 字段 pinned」来安全化这个转换（SAFETY 注释写明了依据）。去掉 unsafe 就根本无法从 `Pin<&mut LspRequest<F>>` 拿到 `Pin<&mut F>`，编译不过；换成普通引用则会在 future 被移动后悬垂。

## 5. 综合实践：用 FakeLanguageServer 走一遍 HoverRequest 的成功与失败

这是本讲的收尾实践：不启动任何真实进程，用 `FakeLanguageServer` 端到端验证 4.2 → 4.3 的整条链路，并亲眼看到「handler 返回 `Err` → JSON-RPC error → anyhow 错误」的还原过程。

**任务**：在 `src/lsp.rs` 底部 `mod tests` 中新增一个测试（**示例代码**，需在本地 checkout 中添加），仿照 [test_fake](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L2105-L2189) 的搭建方式：

```rust
#[gpui::test]
async fn test_request_hover_with_fake(cx: &mut TestAppContext) {
    use std::str::FromStr;

    let (server, _fake) = FakeLanguageServer::new(
        LanguageServerId(0),
        LanguageServerBinary {
            path: "path/to/language-server".into(),
            arguments: vec![],
            env: None,
        },
        "the-lsp".to_string(),
        Default::default(),
        &mut cx.to_async(),
    );

    // 第一幕：handler 返回 Ok(Some(Hover))
    let _hover_handled = _fake.set_request_handler::<request::HoverRequest, _, _>(|_, _| {
        async move {
            Ok(Some(Hover {
                contents: HoverContents::Scalar(MarkedString::String("fake hover".into())),
                range: None,
            }))
        }
    });

    let params = HoverParams {
        text_document_position_params: TextDocumentPositionParams {
            text_document: TextDocumentIdentifier::new(Uri::from_str("file:///a/b").unwrap()),
            position: Position::new(0, 1),
        },
        work_done_progress_params: Default::default(),
    };

    let response = server
        .request::<request::HoverRequest>(params, DEFAULT_LSP_REQUEST_TIMEOUT)
        .await;
    match response {
        ConnectionResult::Result(Ok(Some(hover))) => match hover.contents {
            HoverContents::Scalar(MarkedString::String(text)) => assert_eq!(text, "fake hover"),
            _ => panic!("unexpected hover contents"),
        },
        other => panic!("expected Ok(Some(Hover)), got {other:?}"),
    }

    // 第二幕：重新注册（set_request_handler 会先移除旧 handler），返回 Err
    let _hover_failed = _fake.set_request_handler::<request::HoverRequest, _, _>(|_, _| {
        async move { Err(anyhow!("fake hover failure")) }
    });

    let response = server
        .request::<request::HoverRequest>(params, DEFAULT_LSP_REQUEST_TIMEOUT)
        .await;
    match response {
        ConnectionResult::Result(Err(err)) => {
            assert_eq!(err.to_string(), "fake hover failure");
        }
        other => panic!("expected Err, got {other:?}"),
    }

    drop(server);
    cx.run_until_parked();
}
```

**操作步骤**：

1. 把测试加入 `mod tests`（需要 `anyhow!`，tests 模块已通过 `use super::*` 拿到其余类型）。
2. 运行 `cargo test -p lsp test_request_hover_with_fake`。

**需要观察的现象与对应源码**：

- 第一幕的成功链路：`request`（L1408）同步段分配 id 0 之后的第一个可用 id（若 `FakeLanguageServer::new` 内部未发过请求，本次即为其计数器首个 id）、注册表项、直发 outbound；fake 侧 `on_custom_request` 调用 handler（L1264），`Ok` 分支（L1271-L1275）序列化 `LspResult::Ok` 回写；客户端 input_handler 分支二 `remove` 表项、回调以 `Ok(String)` 触发；后台 `deserialize_result` 还原 `Option<Hover>`；oneshot 唤醒 `select!`，返回 `ConnectionResult::Result(Ok(Some(hover)))`。
- 第二幕的失败链路：`Err` 分支（L1276-L1284）把 `error.to_string()` 放进 `Error { code: REQUEST_FAILED, message }` 序列化回写；客户端 `handler(Err(error))`（[src/input_handler.rs:L121-L122](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L121-L122)）触发 4.2 闭包的 `Err(error) => Err(anyhow!("{}", error.message))`（L1493）——所以断言 `err.to_string() == "fake hover failure"` 能通过：**错误消息穿越了整个 JSON-RPC 往返而保持原文**。
- `set_request_handler` 之所以能重复注册，是因为它先 `remove_request_handler::<T>()`（[src/lsp.rs:L2007](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L2007)）再 `on_request`，避免触发「registered multiple handlers」断言（L1317-L1320）。

**预期结果**：两个断言分支依次命中；结尾 `drop(server)` 触发 Drop 自动善后（u2-l4）。若想进一步验证 `id()`，可在两次 request 之间插入 `let id = server.request::<request::HoverRequest>(…).id();` 观察 id 递增。本测试未在本环境执行，**待本地验证**（`request::HoverRequest` 是 hover 请求在 lsp-types 中的真实类型名——manifest 中写作 `request::Hover`，实际类型为 `HoverRequest`，见 [crates/project/tests/integration/project_tests.rs:L9519-L9531](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/project/tests/integration/project_tests.rs#L9519-L9531) 的既有用法）。

## 6. 本讲小结

- 一次 `request` 分两段：**同步段**在调用返回前完成「`fetch_add` 分配 id → 序列化完整请求 → 向 `response_handlers` 插入 `RequestId::Int(id)` 表项 → `try_send` 直发 outbound」；**异步段**封装在返回的 future 里，`select!` 让 oneshot 响应与定时器竞争。
- `response_handlers` 是 `Arc<Mutex<Option<HashMap<RequestId, ResponseHandler>>>>`：以 `RequestId` 为键、`FnOnce` 回调为值；由 `input_handler.rs` 的响应分支在**后台读帧线程**上 `remove` 触发——查表与摘除是一次原子动作，重复/迟到响应自然丢弃。响应路径不经过 `incoming_messages` 通道与前台循环。
- 回调收到的仍是**原始 JSON 文本**，`T::Result` 的反序列化被推迟到 `BackgroundExecutor` 上的任务里完成；JSON-RPC error 的 `message` 原样变成 anyhow 错误字符串。
- 「server shut down」快速失败有三个触发点（入站任务退出、写出任务退出、`shutdown` 显式作废），任一发生后新请求在同步段即以 `ConnectionResult::Result(Err("server shut down"))` 失败，不发送、不等待。
- `LspRequest` 是给任意 future 套 `id` 字段的透明包装器（含一处带 SAFETY 注释的手写 pin 投影）；`LspRequestFuture::id()` 让调用方在 await 前拿到请求 id——project crate 用它把请求 id 直接复用为进度令牌。
- 三种终局 `Timeout` / `ConnectionReset` / `Result` 由 `ConnectionResult` 统一表达，`into_response()` 可压平为 `anyhow::Result`。

## 7. 下一步学习建议

本讲刻意绕开了 `request_internal_with_timer` 里两块「深水区」：`request_timeout_future` 对 `Duration::MAX` 与零的特殊处理、`select!` 定时器分支的完整语义，以及 `cancel_on_drop` 如何在 future 被 drop 时发出 `$/cancelRequest`。这些正是下一讲 **u3-l3「超时、取消与 ConnectionResult」** 的主题，建议先做完本讲综合实践再继续。之后 **u3-l4「入站分发」** 会补上 `handle_incoming_messages` 主循环与 `on_request` 的另一半视角（server→client 请求，即本讲 4.3 分支一的去向）；若想深入「有界通道 + 背压」，可跳读 **u4-l1**。源码方面，建议把 [crates/util/src/util.rs:L771-L792](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/util/src/util.rs#L771-L792) 的 `ConnectionResult` 和 [crates/project/src/lsp_store.rs:L5619-L5664](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/project/src/lsp_store.rs#L5619-L5664) 的真实调用方对照本讲再读一遍。
