# u3-l1 发送通知与延迟序列化

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `LanguageServer::notify::<T>` 的完整链路：泛型参数 `T` 如何同时决定 JSON-RPC 消息的 `method` 字符串和 `params` 的 Rust 类型。
2. 解释 `NotificationSerializer` 为什么装的是「序列化闭包」而不是序列化好的字符串，以及这个设计如何避免在调用线程（通常是 UI 前台线程）上做重量级 JSON 序列化。
3. 画出 `notification` 通道与 `outbound` 通道两级串联的数据流，并推导出它们的级联关闭顺序：`notification_tx.close()` → 排空 → `outbound_tx.close()` → 排空 → barrier 点亮 → kill 子进程。
4. 独立完成一个实践：仿照 `register_buffer` 封装一个发送 `workspace/didChangeConfiguration` 通知的 helper，并用 `FakeLanguageServer` 断言消息内容正确送达。

## 2. 前置知识

本讲建立在 u1-l2（消息模型）和 u2-l2（IO 任务管线）之上，先快速回顾四个概念。

**通知（notification）与请求（request）的区别。** LSP 基于 JSON-RPC 2.0，只有三种消息。通知是「发出去就不管」的那种：有 `jsonrpc`、`method`、`params` 三个字段，**没有 `id`**，也永远不会有响应。所以发送通知天然是一次性动作，不需要 oneshot 通道、不需要超时、不需要取消——这决定了 `notify` 可以是一个普通的同步方法。

**前台线程与后台线程。** GPUI 把所有 UI 渲染和实体更新放在单一前台线程（u2-l2 讲过 `cx.spawn` 与 `cx.background_spawn` 的分工）。`LanguageServer` 的方法大量被 editor、project 等上层代码在前台调用，因此「调用方线程上做多少工作」是一个真实的设计约束。

**通道（channel）与关闭语义。** 本讲涉及两条 `async_channel` 的无界（unbounded）通道：`outbound` 通道传 `String`，`notification` 通道传 `NotificationSerializer`。无界通道的 `send` 永不阻塞；`recv` 在通道**关闭且已排空**之后才返回 `Err`——这个「先排空、再退出」的语义正是级联关闭的基石。向已关闭的通道 `send` 会立即失败，这也是 `notify` 返回 `Result<()>` 的唯一现实原因。

**`notification::Notification` trait。** lsp.rs 顶部整体再导出了 lsp-types（Zed fork，锁定在 workspace `Cargo.toml` 的 [rev f4dfa89a](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/Cargo.toml#L678)，见 [src/lsp.rs:L3-L4](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L3-L4)，这两行把 `lsp_types::request::*` 和整个 `lsp_types` 引入本 crate 命名空间）。其中 `notification` 模块为每种通知定义了一个标记类型，实现 `notification::Notification` trait。从 crate 内的使用处可以直接读出这个 trait 的形状：它有关联常量 `METHOD`（`&'static str`，如 `"workspace/didChangeConfiguration"`）和关联类型 `Params`（参数的 Rust 类型，出站时被序列化、测试侧会被反序列化回来）。使用证据贯穿全文，例如 [src/lsp.rs:L674](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L674) 的 `<notification::Cancel as notification::Notification>::METHOD` 和 [src/lsp.rs:L1987](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1987) 的 `if method == T::METHOD`。

## 3. 本讲源码地图

| 文件 | 行号区间 | 作用 |
| --- | --- | --- |
| `src/lsp.rs` | L112 | `NotificationSerializer` 定义：一个装着「序列化闭包」的新类型 |
| `src/lsp.rs` | L118-L119 | `LanguageServer` 的两个出站通道字段：`outbound_tx` 与 `notification_tx` |
| `src/lsp.rs` | L304-L313 | 出站通知消息结构体 `Notification<'a, T>` |
| `src/lsp.rs` | L497-L640 | `new_internal`：创建通道、spawn 三个 IO 任务与一个序列化后台任务 |
| `src/lsp.rs` | L742-L776 | `handle_outgoing_messages`：唯一写出点（u2-l2 已精读，本讲只取其关闭行为） |
| `src/lsp.rs` | L1110-L1167 | `shutdown`：触发级联关闭的入口 |
| `src/lsp.rs` | L1408-L1526 | `request`：对照样本，展示「直发 outbound」与「取消通知走序列化通道」两条路径 |
| `src/lsp.rs` | L1606-L1629 | `notify` / `notify_internal`：本讲主角 |
| `src/lsp.rs` | L1729-L1747 | `register_buffer` / `unregister_buffer`：notify 的典型便捷封装 |
| `src/lsp.rs` | L1956-L1993 | `FakeLanguageServer::notify` / `receive_notification`：测试侧的收发 |
| `src/lsp.rs` | L2105-L2189 | `test_fake`：本讲实践的样板测试 |
| `crates/project/src/lsp_store.rs` | L622-L624 | 上游真实调用方：initialize 后补发 didChangeConfiguration |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：`notify` / `notify_internal`（类型化发送口）→ `NotificationSerializer`（延迟序列化）→ `new_internal` 中的序列化后台任务（保序与两级关闭）。

### 4.1 notify / notify_internal：泛型于 METHOD 的类型化发送口

#### 4.1.1 概念说明

调用方想给语言服务器发一条通知时，需要提供两样东西：**方法名**（如 `"workspace/didChangeConfiguration"`）和**参数**（一个 JSON 对象）。最朴素的 API 设计是 `notify(method: &str, params: Value)`——字符串加通用 JSON。lsp crate 没有这样设计，而是：

```rust
server.notify::<notification::DidChangeConfiguration>(DidChangeConfigurationParams { settings })
```

泛型参数 `T` 是 lsp-types 里为该通知定义的标记类型，它同时绑定两件事：`T::METHOD` 决定线上的方法字符串，`T::Params` 决定参数的 Rust 类型。方法名和参数类型在**编译期**锁死为一对，拼错方法名或传错参数类型都无法编译。收发两侧也对齐：入站注册 handler 用的 `on_notification::<T>` 查表键同样是 `T::METHOD`。

另外注意 `notify` 是**同步方法**：没有 `async`，没有 `.await`，不接收 `cx` 参数，返回 `Result<()>`。通知没有响应可等，所以它不需要像 `request` 那样返回 future。这个「同步、可从前台随手调用」的形态正是下一节延迟序列化设计的出发点。

#### 4.1.2 核心流程

一次 `notify::<T>(params)` 调用的时序：

```text
调用线程（通常是前台）
  ├─ notify::<T>(params)                      L1609
  │    └─ clone notification_tx，转调 notify_internal
  ├─ notify_internal::<T>(tx, params)         L1614
  │    ├─ 构造闭包：move || to_string(&Notification {
  │    │        jsonrpc: "2.0", method: T::METHOD, params })   ← 此刻不执行！
  │    └─ tx.send_blocking(serializer)        ← 仅入队，立即返回
  └─ notify 返回 Ok(())                        ← 调用线程全程零序列化开销

后台序列化任务（稍后，见 4.3）
  └─ (serializer.0)()                         ← 这里才真正执行 serde_json::to_string
       └─ 得到 JSON 字符串 → 转发进 outbound 通道
```

要点：**序列化被推迟到了另一个线程的另一个时刻**，调用线程只付出「构造闭包 + 入队」的代价。

#### 4.1.3 源码精读

公开入口带一句文档注释，指回 LSP 规范的 notificationMessage 一节，函数体只有两行——克隆通道、转调关联函数：

- [src/lsp.rs:L1606-L1612](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1606-L1612)：`notify` 把 `self.notification_tx` 克隆一份后转调 `notify_internal`。克隆 sender 使得关联函数不借用 `self`，从而能在 `shutdown` 这种已经不能安全借用 `self` 的场景里复用（见 4.3.3）。

关联函数是全部逻辑所在，短短十五行：

- [src/lsp.rs:L1614-L1629](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1614-L1629)：`notify_internal` 构造一个 `NotificationSerializer`，闭包按值捕获 `params` 与 `T::METHOD`，体内才调用 `serde_json::to_string`；随后 `outbound_tx.send_blocking(serializer)?` 入队并返回。`?` 只会在通道已关闭（即 shutdown 已开始）时触发，这就是 `notify` 会失败的唯一现实情形。

闭包里构造的 `Notification` 是 crate 私有的出站消息结构体（u1-l2 精读过它的 serde 细节）：

- [src/lsp.rs:L304-L313](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L304-L313)：三个字段 `jsonrpc: &'static str`、`method: &'a str`、`params: T`。`method` 是**借用**的 `&str`——因为闭包里传入的是 `T::METHOD` 这个 `&'static str`，零拷贝；`params` 带 `skip_serializing_if = "is_unit"`（判定函数在 [src/lsp.rs:L235-L237](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L235-L237)，用 `TypeId` 识别 `()`），所以 `notify::<notification::Exit>(())` 这种无参通知（[src/lsp.rs:L1159](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1159)）序列化出来不会有多余的 `"params":null`。

crate 里对 `notify` 最典型的封装是文档同步这对方法，本讲综合实践就要仿照它：

- [src/lsp.rs:L1729-L1740](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1729-L1740)：`register_buffer` 把 `uri`、`language_id`、`version`、`initial_text` 四个裸参数组装成 `DidOpenTextDocumentParams`，一次 `notify::<notification::DidOpenTextDocument>` 发出 `textDocument/didOpen`。注意结尾的 `.ok()`：这是便捷封装的「尽力而为」语义——失败只意味着服务器已在关闭，不值得向上传播。
- [src/lsp.rs:L1742-L1747](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1742-L1747)：`unregister_buffer` 与之对称，发 `textDocument/didClose`。

上游真实调用方的写法可以印证这条 API 的使用形态：Zed 的 project crate 在 `initialize` 完成后立刻补发配置通知——

- [crates/project/src/lsp_store.rs:L622-L624](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/project/src/lsp_store.rs#L622-L624)：`language_server.notify::<lsp::notification::DidChangeConfiguration>(did_change_configuration_params)?`。注意这里用的是 `?` 而不是 `.ok()`——服务器启动主流程愿意把这条失败当错误上报，与 `register_buffer` 的取舍不同。

顺带一提，握手收尾也走 `notify`：[src/lsp.rs:L1105](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1105) 在 `initialize` 拿到响应后补发 `notification::Initialized`（参数是单元结构体 `InitializedParams {}`）。

#### 4.1.4 代码实践：盘点 notify 的调用面

这是一个源码阅读型实践，目标是建立「谁在发通知、发的是哪些」的全局印象。

1. **实践目标**：列出 `notify::<` 在整个 Zed 仓库中的调用点，并按用途分类。
2. **操作步骤**：
   - 在仓库根目录执行 `rg '\.notify::<' crates --glob '*.rs'`。
   - 把结果分成三类：文档同步（didOpen/didClose/didChange…）、配置推送（DidChangeConfiguration）、生命周期（Initialized/Exit/Cancel）。
   - 运行 crate 自带的端到端测试：`cargo test -p lsp test_fake`。
3. **需要观察的现象**：`test_fake` 中 [src/lsp.rs:L2150-L2167](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L2150-L2167) 这一段：测试先 `server.notify::<notification::DidOpenTextDocument>(...)`，紧接着 `fake.receive_notification::<notification::DidOpenTextDocument>().await` 拿回参数并断言 `uri`。发与收之间没有任何 `run_until_parked` 之类的推进语句——因为 `receive_notification` 本身是 async 的，挂起等待即完成了让步。
4. **预期结果**：测试通过；`rg` 至少能找到 `crates/project/src/lsp_store.rs` 与 `crates/copilot/src/copilot.rs` 中的 `DidChangeConfiguration` 调用。本环境未实际执行，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：对比 `notify` 与 `request` 的函数签名（[src/lsp.rs:L1609](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1609) 与 [src/lsp.rs:L1408-L1412](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1408-L1412)），说出三处本质差异。

**答案**：① `request` 需要分配 id 并在 `response_handlers` 表里注册回调、返回 future 供 `.await` 等响应，`notify` 无 id 无响应，是同步方法；② `request` 需要额外的 `request_timeout: Duration` 参数，`notify` 没有——没有响应也就没有「等太久」的问题；③ `request` 返回 `impl LspRequestFuture<T::Result>`（结果延迟到 future 完成），`notify` 返回 `Result<()>` 且只反映「是否成功入队」。

**练习 2**：`register_buffer` 为什么对 `notify` 的返回值只 `.ok()`，而 `lsp_store.rs` 里同样的 `notify` 用 `?`？

**答案**：`register_buffer` 返回 `()`，是文档同步的便捷封装；其唯一失败模式是序列化通道已关闭（服务器正在关闭），此时发不出 didOpen 无需上报——buffer 状态与服务器一起消失。`lsp_store.rs` 处在服务器启动主流程中，配置推送失败值得让上层感知并把服务器标记为启动失败。两者都是对同一 `Result` 的合理但不同的取舍。

**练习 3**：为什么方法名用 `T::METHOD` 关联常量而不是 `notify(method: &str, params: Value)` 这样的字符串参数？

**答案**：关联常量把「方法名字符串」与「参数 Rust 类型」绑定成一个编译期整体。写 `notify::<notification::DidOpenTextDocument>` 时，编译器保证 params 一定是 `DidOpenTextDocumentParams`，方法名不可能拼错；字符串 API 则把这两件事交给运行时的 JSON 拼装，错误要等到服务器回报 `MethodNotFound` 才暴露。此外入站分发 `on_notification::<T>` 用同一个 `T::METHOD` 做查表键，收发两侧天然对齐。

### 4.2 NotificationSerializer：把 serde_json::to_string 搬到后台线程

#### 4.2.1 概念说明

`NotificationSerializer` 的定义只有一行：

- [src/lsp.rs:L112](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L112)：`struct NotificationSerializer(Box<dyn FnOnce() -> String + Send + Sync>);`——一个装着「无参、返回 String 的闭包」的新类型（newtype）。

它解决的问题是：**序列化发生在哪个线程**。`notify` 是同步方法，最常见的调用方是 GPUI 前台线程（editor、project 在 UI 事件里直接调它）。而 `serde_json::to_string` 的耗时与 params 大小成正比——结构体字段 `configuration` 的文档注释（[src/lsp.rs:L125-L128](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L125-L128)）专门提到「给 json 服务器发 schemas」这类配置可以非常大。如果在调用线程上序列化，一次大配置推送就足以造成 UI 掉帧。

于是 crate 把「要做的事」（序列化）和「做这件事的时机/地点」（后台线程、稍后）分开：`notify` 只把闭包装箱入队，真正的 `to_string` 发生在后台序列化任务里（4.3 节）。这就是标题里「发送序列化闭包而非序列化好的字符串」的含义。

三个 trait 约束各有来由：

- `FnOnce`：闭包按值捕获 `params`，消费它产出一个 String，只能调用一次。
- `Send`：闭包要从调用线程跨越到后台任务。
- `Sync`：满足 `Box<dyn ... + Send + Sync>` 的默认 `'static` 装箱要求，同时让通道两端随意克隆传递。

#### 4.2.2 核心流程

```text
调用线程                          后台序列化任务
──────────                        ──────────────
params: T::Params
   │ move 进闭包
   ▼
NotificationSerializer(
  Box::new(move || to_string(     notification_rx.recv()
      &Notification { ... }))  ──►      │
)                                      ▼
   │ send_blocking                (serializer.0)()
   ▼                                     │ 产出 String
notification 通道（无界 FIFO）            ▼
                                   outbound 通道 ──► handle_outgoing_messages 写 stdin
```

对比假想的设计「调用线程先 `to_string` 再把 `String` 发进通道」：功能等价，但序列化的 CPU 开销落在调用线程上。当前设计把它整体挪到了后台。

#### 4.2.3 源码精读

构造点就是 4.1.3 读过的 `notify_internal`：

- [src/lsp.rs:L1618-L1627](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1618-L1627)：闭包体内才出现 `serde_json::to_string`，且以 `.unwrap()` 收尾。序列化一个 `Serialize` 类型失败属于类型定义层面的编程错误（此处也无法把错误传回——返回值类型就是 `String`），与运行时 IO 故障不同类，这是该 `unwrap` 的边界。入队用 `send_blocking`：无界通道上这个调用立即完成，失败即「通道已关闭」。

消费点在 `new_internal` 里那个常驻后台任务（下一节精读）：`(serializer.0)()` 一行就是闭包的执行现场。序列化产物随后进入 `outbound` 通道，由 u2-l2 精读过的 [src/lsp.rs:L742-L776](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L742-L776) `handle_outgoing_messages` 统一加 `Content-Length` 帧写出并 flush——对写出端而言，来自序列化任务的字符串和 `request` 直发的字符串没有任何区别。

值得注意的还有一处隐蔽的复用：请求的取消通知也走这条序列化路径。[src/lsp.rs:L1516-L1526](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1516-L1526) 中，`request_internal_with_timer` 为每个请求装了一个 `cancel_on_drop` 的 defer：future 被 drop（超时或调用方放弃）时，通过 `Weak` 升级拿到序列化通道的 sender，调 `notify_internal::<notification::Cancel>` 发出 `$/cancelRequest`。也就是说，**请求的序列化消息走直发路径，请求的取消却走延迟序列化路径**——两台机器并存在同一个 `request` 机制里。

#### 4.2.4 代码实践：验证「调用线程零序列化」

阅读型 + 本地实验。

1. **实践目标**：亲手确认序列化不在 `notify` 调用线程上执行。
2. **操作步骤**：
   - 在本地分支给 [src/lsp.rs:L1618](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1618) 的闭包内部和 [src/lsp.rs:L1614](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1614) 的 `notify_internal` 入口各加一行 `log::info!`（带上 `std::thread::current().id()`），运行 `cargo test -p lsp test_fake`。
   - 观察两行日志的线程 id 与先后关系。完成后还原改动。
3. **需要观察的现象**：入口日志出现在调用线程（测试里是 GPUI 测试执行线程），闭包日志出现在另一线程且时间上晚于 `notify` 返回之后。
4. **预期结果**：两条日志线程 id 不同；无论 params 多大，`notify` 调用本身耗时与 params 规模无关。**待本地验证**（本环境不修改源码）。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `NotificationSerializer` 改成直接装 `String`（调用线程序列化好再入队），会失去什么？

**答案**：失去「序列化挪到后台」的能力。`notify` 的调用方大量位于前台线程，params 大时（如携带 schemas 的配置、大文档全文）`serde_json::to_string` 的 CPU 时间会直接计入 UI 线程帧预算，造成卡顿。消息语义不变，变的是性能归属。

**练习 2**：为什么是 `FnOnce` 而不是 `Fn`？

**答案**：闭包 `move` 捕获了 `params: T::Params` 的所有权，调用时要消费它来构造 `Notification` 并序列化——同一份 params 无法重复消费，因此闭包只能执行一次，对应 `FnOnce`。消费端也只有一处 `(serializer.0)()`。

**练习 3**：`Notification` 的 `method` 字段为什么可以设计成借用 `&'a str`（[src/lsp.rs:L310](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L310)）？

**答案**：因为写入闭包的 method 永远是 `T::METHOD`——一个编译期内嵌的 `&'static str` 关联常量，无需拥有所有权；serde 序列化 `&str` 与 `String` 产出完全相同的 JSON。借用形式让这个只出不进的结构体零分配（params 之外）。

### 4.3 new_internal 中的序列化后台任务：保序与两级关闭

#### 4.3.1 概念说明

[状态搭建] `new_internal`（u2-l2 精读过它的三路 IO 泛型）在构造 `LanguageServer` 时会额外创建第四个长寿命任务：**通知序列化任务**。它是一条单消费者流水线，负责把 `notification` 通道里的闭包逐条执行、把产出的字符串转投 `outbound` 通道。两个通道字段见 [src/lsp.rs:L118-L119](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L118-L119)。

它带来两个性质：

**消息顺序。** 无界 FIFO 通道 + 单一消费者意味着：同一线程先后两次 `notify`，两条 JSON 必然按发送顺序到达 `outbound`、按顺序写进 stdin。LSP 的文档同步语义（先 didOpen 再 didChange）依赖这一点。需要如实指出边界：`request` 的消息是在调用点同步直发 `outbound` 的（见 4.3.3），不经过这一跳，所以「请求与随后的通知」之间的相对顺序没有通道层面的硬保证——这是用「请求要立刻出门等响应」换来的取舍。

**级联关闭。** 两个通道首尾相接，关闭必须从源头开始逐级传播：关 `notification` → 序列化任务排空退出时顺手关 `outbound` → 写出任务排空退出时点亮 barrier → `shutdown` 的 future 在 barrier 上醒来后才 kill 子进程。u2-l4 从 shutdown 的视角走过这条链；本讲从通道的角度把同一件事补全。

#### 4.3.2 核心流程

数据流（实线为消息，虚线为关闭信号）：

```text
notify() ──► notification_tx ──► [序列化任务] ──► outbound_tx ──► [handle_outgoing_messages] ──► stdin
                (闭包)            recv→执行→send      (String)         加帧→写→flush

shutdown() 触发的级联关闭：
  ① notify_internal::<Exit> 把 Exit 闭包入队           (L1159)
  ② notification_serializers.close()                   (L1160)
        └─► 序列化任务 recv 排空后返回 Err → 退出循环
              └─► outbound_tx.close()                  (L609)
                    └─► 写出任务把 Exit 写出并 flush 后 recv 返回 Err → 退出循环
                          └─► drop(output_done_tx) 点亮 barrier    (L774)
  ③ output_done.recv().await 返回 → child.kill()       (L1161-L1162)
```

关键不变量：**关闭先于排队中的最后一条消息到达出口是不可能的**——`async_channel` 的 `recv` 只在「关闭且排空」后报错，所以 ② 的每一级都会先把手头积压处理完。这就保证了 Exit 一定先落盘、子进程后被杀（u2-l4 的结论在这里得到了通道语义层面的解释）。

#### 4.3.3 源码精读

通道与任务的创建集中在 `new_internal` 尾部：

- [src/lsp.rs:L518-L519](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L518-L519)：先建 `outbound` 通道（传 `String`）和 `output_done` barrier——barrier 是写出任务完成后的「竣工信号」。
- [src/lsp.rs:L598-L612](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L598-L612)：本讲核心。建 `notification` 通道后 `cx.background_spawn` 一个任务：`while let Ok(serializer) = notification_rx.recv().await` 循环里 `(serializer.0)()` 执行序列化，`outbound_tx.send(serialized).await` 转发；**任何一次转发失败（`outbound` 已关）就 return**；循环自然结束（`notification` 关闭且排空）后执行 `outbound_tx.close()`——这就是级联的中间一环。任务以 `.detach()` 结尾：它是常驻管家，不随某次调用存活，也不进 `io_tasks`。
- [src/lsp.rs:L613-L639](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L613-L639)：组装结构体，`notification_tx`（L616）与 `outbound_tx`（L632）都存为字段，供 `notify` / `request` / `shutdown` 克隆使用。

对照：请求路径**不走**序列化通道。[src/lsp.rs:L1464-L1471](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1464-L1471) 在 `request_internal_with_timer` 的函数体内（注意：是同步执行体，不在 async 块里）就完成 id 分配与 `serde_json::to_string(&Request {...})`，随后 [src/lsp.rs:L1501-L1503](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1501-L1503) `outbound_tx.try_send(message)` 直发。请求需要「立刻出门、马上等响应」，多一跳后台任务只会增加延迟；而 `request` 本身就是 async 上下文可用的 API，调用方对序列化成本的预期也不同。

级联的触发端在 `shutdown`：

- [src/lsp.rs:L1158-L1163](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1158-L1163)：五步收尾——清空响应表；`notify_internal::<notification::Exit>(&notification_serializers, ())` 把 Exit 闭包入队；`notification_serializers.close()` 关掉源头；`output_done.recv().await` 等 barrier；最后 `child.kill()`。注意顺序铁律：**必须先入队 Exit 再 close**——反过来的话 `send_blocking` 直接失败，Exit 永远发不出去。还要注意 `notification_serializers` 是从 `self.notification_tx` 克隆出来的（[src/lsp.rs:L1116-L1118](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1116-L1118)），关闭只影响这对克隆的 sender 与通道本身——这正是 4.1.3 说「`notify` 先克隆通道再转调关联函数」的意义：`shutdown` 构造的关闭 future 不借用 `self`。
- [src/lsp.rs:L760-L774](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L760-L774)：写出任务的收尾侧。`while let Ok(message) = outbound_rx.recv().await` 在通道关闭且排空（Exit 已写出并 flush）后退出，`drop(output_done_tx)` 释放 barrier。

测试侧的收发镜像也值得一看——`FakeLanguageServer` 的两个方向用的是同一套机器：

- [src/lsp.rs:L1959-L1961](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1959-L1961)：fake 发通知（server→client 方向）就是转调内部那个 `LanguageServer` 的 `notify`。
- [src/lsp.rs:L1982-L1993](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1982-L1993)：`try_receive_notification` 从通道里逐条取 `(method, params)` 字符串对，`method == T::METHOD` 匹配则把 params 反序列化回 `T::Params`，不匹配就跳过——综合实践将用它在 fake 侧验收 didChangeConfiguration。

#### 4.3.4 代码实践：跟踪一次完整的级联关闭

源码阅读型实践，把 4.3.2 的流程图落到行号。

1. **实践目标**：能不看讲义、只看源码复述关闭级联的每一跳。
2. **操作步骤**：
   - 从 [src/lsp.rs:L1110](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1110) 的 `shutdown` 入口读起，依次追 L1159 → L1160 → L603-L609 → L760 → L774 → L1161-L1162。
   - 在每一步旁边标注：这一跳之前**必须已完成**什么、这一跳**保证**了什么。
   - 对照 `test_fake` 的结尾（[src/lsp.rs:L2184-L2188](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L2184-L2188)）：`drop(server)` + `cx.run_until_parked()` 之后 `receive_notification::<notification::Exit>` 能等到 Exit，说明整条链在测试调度下真实走通了。
3. **需要观察的现象**：`run_until_parked` 驱动期间，Drop 触发的 shutdown future 在后台完成「入队 Exit → 双通道排空 → barrier → kill（fake 无子进程，此步为空操作）」全流程。
4. **预期结果**：能画出与 4.3.2 一致的链路图；这是纯阅读任务，结论可直接从源码验证。

#### 4.3.5 小练习与答案

**练习 1**：如果把 L1160 的 `notification_serializers.close()` 删掉，会发生什么？

**答案**：级联在第一环就断掉。`notification` 通道永不关闭 → 序列化任务停在 `recv().await`，`outbound_tx.close()`（L609）永不执行 → 写出任务停在 `outbound_rx.recv().await`，`drop(output_done_tx)` 永不发生 → barrier 不亮 → `output_done.recv().await`（L1161）永久挂起 → shutdown future 永不完成、子进程不被 kill（只剩 `kill_on_drop` 这道 u2-l1 讲过的保险）。

**练习 2**：为什么 Exit 必须走 `notify_internal`（序列化通道），而不是像请求那样直接 `to_string` 后 `outbound_tx.try_send`？

**答案**：因为 Exit 必须是「最后一条消息」且必须与关闭动作绑定。走序列化通道后，紧随其后的 `notification_serializers.close()` 借助「排空后才退出」的通道语义，天然保证：所有先前入队的通知（包括 Exit）按序序列化、写出、flush，然后才轮到 `outbound` 关闭。若直发 `outbound`，还得另外保证「没有更晚的通知插到 Exit 之后」，而序列化通道里可能还有积压——把 Exit 放进同一队列，顺序问题自动消解。

**练习 3**：同一线程先后 `notify::<A>`、`notify::<B>`，两条消息在 stdin 上的顺序一定保持吗？`request::<R>` 之后再 `notify::<B>` 呢？

**答案**：前者一定保持：两次入队先后确定，单一序列化任务按 FIFO 逐条处理，转投 `outbound` 与写出都保序。后者没有通道层面的保证：`request` 的消息在调用点已直发 `outbound`（L1501-L1503），而 `notify::<B>` 还要等后台任务一跳，理论上可能先于请求消息落进 `outbound`——实践上请求早已发出等响应，这种交错不破坏协议正确性，属于设计取舍。

## 5. 综合实践

**任务**：仿照 `register_buffer` 的封装写法，实现一个发送 `workspace/didChangeConfiguration` 通知的 helper，并用 `FakeLanguageServer::receive_notification` 断言序列化后的 `settings` 内容端到端正确送达。这个任务贯穿本讲全部三个模块：泛型发送口（4.1）、经由延迟序列化的送达（4.2）、fake 侧的按序验收（4.3）。

**背景**：真实代码里这条通知由 project crate 发出（[crates/project/src/lsp_store.rs:L622-L624](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/project/src/lsp_store.rs#L622-L624)）；`LanguageServer` 自己只在 [src/lsp.rs:L593-L596](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L593-L596) 预存了一份 `settings: Value::Null` 的默认配置、在 [src/lsp.rs:L1386-L1388](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1386-L1388) 暴露只读视图。我们要做的是把「发送」这一半补成自己的 helper。

**操作步骤**：

1. 在本地克隆的 Zed 仓库中打开 `crates/lsp/src/lsp.rs`，滚到文件底部的 `mod tests`（[src/lsp.rs:L2095](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L2095)）。以下代码均为**示例代码**（仓库中不存在，验证后请删除，不要提交）：

   ```rust
   // 示例代码：写在 mod tests 内。仿照 register_buffer（L1729-L1740）的封装写法。
   fn send_settings(server: &LanguageServer, settings: serde_json::Value) -> anyhow::Result<()> {
       server.notify::<notification::DidChangeConfiguration>(DidChangeConfigurationParams {
           settings,
       })
   }

   #[gpui::test]
   async fn test_notify_did_change_configuration(cx: &mut TestAppContext) {
       cx.update(|cx| {
           release_channel::init(semver::Version::new(0, 0, 0), cx);
       });
       let (server, mut fake) = FakeLanguageServer::new(
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

       // 连发两条，顺带验证 4.3 的保序性质。
       let first = serde_json::json!({ "rust-analyzer": { "check.command": "clippy" } });
       let second = serde_json::json!({ "rust-analyzer": { "check.command": "check" } });
       send_settings(&server, first.clone()).unwrap();
       send_settings(&server, second.clone()).unwrap();

       let params = fake
           .receive_notification::<notification::DidChangeConfiguration>()
           .await;
       assert_eq!(params.settings, first);
       let params = fake
           .receive_notification::<notification::DidChangeConfiguration>()
           .await;
       assert_eq!(params.settings, second);

       // 善后：drop 触发 shutdown 级联，fake 应收到 Exit（同 test_fake 结尾）。
       drop(server);
       cx.run_until_parked();
       fake.receive_notification::<notification::Exit>().await;
   }
   ```

2. 测试样板逐行对照 `test_fake`（[src/lsp.rs:L2105-L2120](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L2105-L2120)）：`release_channel::init`、`FakeLanguageServer::new` 的五个参数（id、binary、名称、capabilities、`&mut cx.to_async()`）都照抄；`ctor` 初始化的 zlog（[src/lsp.rs:L2100-L2103](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L2100-L2103)）模块内已就绪，无需重复。
3. 在仓库根目录运行：`cargo test -p lsp test_notify_did_change_configuration`。
4. 观察后删除新增代码，恢复源码原状。

**需要观察的现象**：

- `receive_notification::<notification::DidChangeConfiguration>` 能拿到消息——证明闭包经序列化任务转成了正确的 JSON（`method` 为 `workspace/didChangeConfiguration`，`params.settings` 与发送值深度相等）。fake 侧的匹配逻辑在 [src/lsp.rs:L1982-L1993](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1982-L1993)，它会跳过路上其他 method 的消息（本测试中只有 drop 之后的 `shutdown` 请求与 `exit` 通知需要跳过）。
- 两次 `assert_eq` 依序通过——验证同线程两次 `notify` 的顺序保持（4.3 练习 3 的前半问落到实证）。
- 结尾对 `notification::Exit` 的等待能在 `run_until_parked` 之后立刻返回——级联关闭真实完成。

**预期结果**：测试通过，输出 `test test_notify_did_change_configuration ... ok`。若把 `second` 的断言改成与 `first` 比较则应失败（顺序错误会被抓到）。本环境未执行编译与测试，**待本地验证**。

**失败排查提示**：若 `receive_notification` 永久挂起，优先检查是否漏了 `&mut cx.to_async()`（fake 组装需要 `AsyncApp`）；若结尾 Exit 等不到，检查是否漏了 `cx.run_until_parked()`——Drop 发射的 shutdown future 需要调度机会（u2-l4）。

## 6. 本讲小结

- `notify::<T>` 用 lsp-types 的 `notification::Notification` trait 把「方法名字符串」与「参数 Rust 类型」在编译期绑成一对，`T::METHOD` 进 JSON 的 `method` 字段，`T::Params` 进 `params`；入站 `on_notification::<T>` 用同一常量查表，收发对齐。
- `notify` 是同步方法：无 id、无响应、无超时，返回的 `Result<()>` 只反映「是否成功入队」，失败唯一现实原因是通道已关闭（服务器正在关闭）。
- `NotificationSerializer`（L112）装的是 `FnOnce() -> String` 闭包而非字符串：`serde_json::to_string` 被推迟到后台线程执行，调用线程（通常是 UI 前台）只付出入队代价，大 params 推送不会卡界面。
- `new_internal` 里的序列化后台任务（L598-L612）是单消费者流水线：notification 通道（闭包）→ 执行序列化 → outbound 通道（String）→ `handle_outgoing_messages` 加帧写出。
- 单消费者 + FIFO 保证同线程多次 `notify` 严格保序；`request` 的消息在调用点直发 outbound、其 `$/cancelRequest` 取消却走序列化通道——两条出站路径并存。
- 关闭级联：`notify_internal::<Exit>` 入队 → `notification_tx.close()` → 序列化任务排空后 `outbound_tx.close()` → 写出任务排空（Exit 已 flush）后 `drop(output_done_tx)` 点亮 barrier → `shutdown` 醒来 kill 子进程。「先入队 Exit 再 close」的顺序不可颠倒。

## 7. 下一步学习建议

本讲只解决了「发出去就不管」的那一半 RPC；「发出并等回应」的另一半是下一讲 u3-l2《请求机制：id、handler 表与 oneshot》的主题：`AtomicI32` 如何分配请求 id、`response_handlers` 如何以 `RequestId` 为键挂起回调、oneshot 通道如何把响应送回等待者，以及 `LspRequestFuture` 为什么要对外暴露请求 id。阅读本讲时你已经两次撞见它的边角——`request_internal_with_timer` 的直发路径（L1464-L1503）和 `cancel_on_drop` 的取消通知（L1516-L1526）——下一讲会把它们串成完整链路。再往后，u3-l3 将深入超时与 `ConnectionResult` 三态，u3-l4 转向入站分发。若想先巩固本讲，建议通读 [src/lsp.rs:L1408-L1526](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1408-L1526)，对照体会「请求直发、通知延迟序列化」这对设计孪生子。
