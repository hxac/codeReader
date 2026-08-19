# JSON-RPC 消息模型与 serde 设计

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 LSP 中 request（请求）、notification（通知）、response（响应）三种消息的区别，以及它们在 `lsp` crate 中分别由哪些结构体建模。
2. 解释 `RequestId` 为什么是一个 `#[serde(untagged)]` 的 `Int/Str` 二态枚举，并准确预测一段 JSON 会被解析成哪个变体。
3. 理解 `NotificationOrRequest` 这个「粗解析中间形态」存在的原因：分帧时还不知道消息的具体类型，只能先把 `method` 和通用 JSON 的 `params` 提出来，再查表分发。
4. 掌握 `is_unit` / `deserialize_params` / `deserialize_result` 三个工具函数如何用 `TypeId` 识别 `()` 类型，从而优雅地解决「服务器对无参数/无返回值的请求发 `{}` 还是 `null`」这一现实世界的不一致问题。

## 2. 前置知识

### 2.1 JSON-RPC 2.0 在 30 秒内讲清楚

LSP 的底层是 JSON-RPC 2.0，一种非常简单的远程调用协议。双方通过传输层（LSP 通常用子进程的 stdin/stdout）互发 JSON 对象，只有三种消息：

| 消息种类 | 必有字段 | 可选字段 | 语义 |
| --- | --- | --- | --- |
| request（请求） | `jsonrpc`、`id`、`method` | `params` | 「请你做一件事，做完把结果回给我」 |
| notification（通知） | `jsonrpc`、`method` | `params` | 「告诉你一件事，不用回复」，没有 `id` |
| response（响应） | `jsonrpc`、`id` | `result` 或 `error`（恰含其一） | 对某个先前请求的答复 |

直观例子：

```json
{"jsonrpc":"2.0","id":1,"method":"textDocument/hover","params":{...}}   // 请求
{"jsonrpc":"2.0","method":"textDocument/didOpen","params":{...}}        // 通知
{"jsonrpc":"2.0","id":1,"result":{"contents":["hello"]}}                // 成功响应
{"jsonrpc":"2.0","id":1,"error":{"code":-32601,"message":"not found"}}  // 失败响应
```

关键点：**响应靠 `id` 与请求配对**；**响应中 `result` 与 `error` 必须恰含其一、绝不能同时出现**（这条规定正是本讲 4.2 节一整套 serde 设计的出发点）。

### 2.2 本讲用到的 serde 技巧速览

| 技巧 | 作用 |
| --- | --- |
| `#[derive(Serialize, Deserialize)]` | 自动生成 JSON 与结构体互转的代码 |
| `#[serde(untagged)]` | 枚举序列化时不写变体名，直接输出内层值；反序列化时按声明顺序逐个尝试变体 |
| `#[serde(skip_serializing_if = "...")]` | 某条件成立时该字段完全不出现在 JSON 里 |
| `#[serde(default)]` | 反序列化时字段缺失则取 `Default::default()` |
| `#[serde(flatten)]` | 把内层结构体的字段「摊平」到外层 JSON 对象 |
| `#[serde(borrow)]` + `&RawValue` | 反序列化时**零拷贝**借用原始 JSON 文本，而不是先解析成 `Value` 树 |
| `serde_json::value::RawValue` | 一段「还没解析的 JSON 文本」的包装类型，`get()` 返回 `&str` |

如果你对其中某项不熟悉，本讲会在真实源码里逐个演示它们的用法。

### 2.3 与上一讲的衔接

上一讲（u1-l1）我们确认了：`lsp` crate 只有两个源文件，`src/lsp.rs` 是库入口，`src/input_handler.rs` 负责从 stdout 字节流按 `Content-Length` 切出消息。本讲聚焦 `src/lsp.rs` 前三百多行里的「消息模型」——它们是后面所有收发机制的地基。

## 3. 本讲源码地图

| 文件 | 本讲关注的区域 | 作用 |
| --- | --- | --- |
| `src/lsp.rs` | L44-L45 | `JSON_RPC_VERSION`、`CONTENT_LEN_HEADER` 两个协议常量 |
| `src/lsp.rs` | L225-L233 | `RequestId`：请求 id 的 untagged 建模 |
| `src/lsp.rs` | L235-L253 | `is_unit` / `deserialize_params` / `deserialize_result` 三个工具函数 |
| `src/lsp.rs` | L258-L331 | `Request`、`AnyResponse`、`Response`、`LspResult`、`Notification`、`NotificationOrRequest`、`Error` 七个消息结构体 |
| `src/lsp.rs` | L1464-L1498、L1614-L1629 | 出站方向：请求与通知如何被序列化发送 |
| `src/lsp.rs` | L1230-L1315 | 入站方向：params 如何被反序列化、响应如何被构造 |
| `src/lsp.rs` | L2340-L2406 | 测试样板：id 反序列化与响应序列化的既有测试 |
| `src/input_handler.rs` | L108-L135 | 两分支分发：先试 `NotificationOrRequest`，再试 `AnyResponse` |

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：`RequestId` → 三种消息的建模（`Request` / `Notification` / `AnyResponse` / `Response` / `LspResult`）→ `NotificationOrRequest` → unit 类型特殊处理。

### 4.1 RequestId：untagged 的 Int/Str 二态

#### 4.1.1 概念说明

JSON-RPC 规定请求 id 可以是数字也可以是字符串。现实中两种服务器都存在：Zed 自己发起的请求总是用递增整数 id；而有些服务器（测试里出现的 Metals、真实世界的不少实现）会用字符串 id 甚至「长得像数字的字符串 id」（`"2"`）回请求。

因此 `RequestId` 被建模成一个二态枚举：

- `Int(i32)` —— Zed 出站请求使用，从 0 递增分配；
- `Str(String)` —— 兼容任何服务器发来的字符串 id。

它必须实现 `Eq + Hash`，因为它要当 `HashMap` 的键——响应到达时靠它反查「这是哪个请求的答复」（见 4.2.3 中 `response_handlers` 的用法）。

#### 4.1.2 核心流程

`#[serde(untagged)]` 的行为：

- **序列化**：不输出变体名，直接输出内层值。`Int(2)` → `2`，`Str("abc")` → `"abc"`。
- **反序列化**：按变体声明顺序逐一尝试：

```text
输入 JSON 的 id 字段
  ├─ 是数字且在 i32 范围内 → Int(数字)
  ├─ 是数字但超出 i32 → Int 失败，Str 也失败 → 整条消息解析失败
  └─ 是字符串 → Int 失败（字符串不是数字）→ Str(字符串)
```

注意第三条：`"2"` 是字符串，`i32` 变体无法匹配，于是落入 `Str("2")`——「数字样子的字符串 id」保持字符串身份，不会被悄悄转成数字。这正是既有测试 `test_deserialize_string_digit_id` 验证的行为。

#### 4.1.3 源码精读

定义在 [src/lsp.rs:L228-L233](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L228-L233)：`RequestId` 派生了 `Hash`、`Eq`、`Serialize`、`Deserialize`，并用 `#[serde(untagged)]` 标注，`Int(i32)` 与 `Str(String)` 两个变体按此顺序尝试匹配。

它的两个典型使用点：

- 出站分配 id：[src/lsp.rs:L1464-L1471](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1464-L1471) 中 `next_id.fetch_add(1, SeqCst)` 取出一个 `i32`，包成 `RequestId::Int(id)` 放进 `Request`。
- 入站配对：[src/input_handler.rs:L114-L119](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L114-L119) 从响应里取出 `id`，在 `response_handlers` 表中 `remove(&id)`，找到等待该答复的回调。

三个既有测试是本节最好的文档，位于 [src/lsp.rs:L2340-L2365](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L2340-L2365)：分别断言 `id:"2"` 解析为 `Str("2")`、`id:"anythingAtAll"` 解析为 `Str(...)`、`id:2` 解析为 `Int(2)`。

#### 4.1.4 代码实践

**实践目标**：亲手验证 untagged 解析规则，并把断言从「只查 id」扩展到「id + method + 嵌套 params」。

**操作步骤**（请在本地学习分支上做，改完不建议提交）：

1. 在 Zed 仓库根目录运行既有测试，确认环境可用：

   ```bash
   cargo test -p lsp test_deserialize
   ```

   应看到 `test_deserialize_string_digit_id`、`test_deserialize_string_id`、`test_deserialize_int_id` 三个测试通过。

2. 打开 `src/lsp.rs`，找到 [L2095](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L2095) 起的 `#[cfg(test)] mod tests` 模块，仿照 `test_deserialize_string_id` 新增一个测试（示例代码）：

   ```rust
   #[gpui::test]
   fn test_deserialize_workspace_configuration_request() {
       let json = r#"{"jsonrpc":"2.0","id":42,"method":"workspace/configuration","params":{"items":[{"scopeUri":"file:///demo/","section":"rust-analyzer"}]}}"#;
       let msg = serde_json::from_str::<NotificationOrRequest>(json)
           .expect("server-to-client request should be parsed");
       assert_eq!(msg.id, Some(RequestId::Int(42)));
       assert_eq!(msg.method, "workspace/configuration");
       assert_eq!(
           msg.params,
           Some(serde_json::json!({
               "items": [{"scopeUri": "file:///demo/", "section": "rust-analyzer"}]
           }))
       );
   }
   ```

   由于测试写在 crate 自己的 `tests` 子模块里，可以直接访问 crate 私有的 `NotificationOrRequest`。

3. 运行：

   ```bash
   cargo test -p lsp test_deserialize_workspace_configuration_request
   ```

**需要观察的现象**：数字 `42` 变成 `Int(42)`；嵌套的 `params` 被完整保留为通用 JSON 值，没有提前解析成任何具体类型。

**预期结果**：测试通过。若把 JSON 里的 `"id":42` 改成 `"id":"42"`，`assert_eq!(msg.id, Some(RequestId::Int(42)))` 会失败，实际值是 `Some(RequestId::Str("42".to_string()))`——这就是 untagged 的顺序匹配在起作用。（编译与运行结果待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `{"id": "2"}` 解析结果是 `Str("2")` 而不是 `Int(2)`？

**答案**：untagged 反序列化按变体声明顺序尝试。`"2"` 是 JSON 字符串，无法匹配 `Int(i32)`（要求数字），于是落到第二个变体 `Str(String)`，成功匹配。字符串身份被保留。

**练习 2**：如果某个服务器发来 `"id": 5000000000`（超出 i32 范围），会发生什么？

**答案**：`Int(i32)` 匹配失败（溢出），`Str(String)` 也匹配失败（这是数字不是字符串），`RequestId` 整体反序列化失败，进而整条消息在 [src/input_handler.rs:L129-L134](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L129-L134) 落入 `warn!("failed to deserialize LSP message")` 分支被丢弃。这是可以按 serde 语义推出的结论，可用一个临时测试本地验证。

**练习 3**：`RequestId` 为什么需要派生 `Hash` 和 `Eq`？

**答案**：它被用作 `HashMap<RequestId, ResponseHandler>`（响应配对表）和 `HashMap<RequestId, Task<()>>`（待完成响应任务表）的键，见 [src/lsp.rs:L522-L524](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L522-L524)。`HashMap` 的键类型必须实现 `Eq + Hash`。

### 4.2 三种消息的建模：Request、Notification 与响应家族

#### 4.2.1 概念说明

先建立一个总览表，后面逐个精读：

| 结构体 | 可见性 | 建模哪种消息 | 泛型参数 | 使用方向 |
| --- | --- | --- | --- | --- |
| `Request<'a, T>` | `pub` | 出站请求 | `T` = params 类型 | 客户端 → 服务器 |
| `Notification<'a, T>` | crate 私有 | 出站通知 | `T` = params 类型 | 客户端 → 服务器 |
| `AnyResponse<'a>` | crate 私有 | 入站响应（未知具体类型时）与少数出站错误响应 | 无 | 双向 |
| `Response<T>` | crate 私有 | 出站响应（已知具体类型） | `T` = result 类型 | 客户端 → 服务器 |
| `LspResult<T>` | crate 私有 | `Response` 的内层，强制 result/error 互斥 | `T` = result 类型 | 随 `Response` |
| `Error` | crate 私有 | JSON-RPC 错误对象 | 无 | 双向 |

一个容易混淆的点：**服务器也会向客户端发请求**（如 `workspace/configuration`、`client/registerCapability`），此时 Zed 是「服务端」，要用 `Response` 答复。所以「出站响应」是真实存在的需求。

另外注意「强类型」与「弱类型」两条路线的分工：

- 发送时类型已知（调用方写了 `request::<T>` / `notify::<T>`），所以 `Request` / `Notification` 直接泛型持有强类型 params，一次序列化到位。
- 接收时类型未知（要先看 `method` 才知道该用哪个 handler），所以入站走 `NotificationOrRequest` + `AnyResponse` 这两个弱类型形态，params 保持通用 JSON，等查表后再反序列化。

#### 4.2.2 核心流程

一条消息在 crate 内的完整旅程（本讲只看序列化/反序列化环节，通道与任务在 u2-l2 展开）：

```text
出站请求:
  request::<T>(params)
    → 分配 Int id                    (lsp.rs L1464)
    → Request { jsonrpc, id, method: T::METHOD, params } 序列化成 JSON 字符串
    → 送入 outbound 通道

出站通知:
  notify::<T>(params)
    → Notification { jsonrpc, method: T::METHOD, params } 序列化
    → 送入通知序列化通道               (lsp.rs L1618-L1625)

入站响应:
  stdout 字节 → 分帧 → 尝试解析为 AnyResponse
    → 按 id 查 response_handlers 表   (input_handler.rs L110-L119)
    → result 以原始 JSON 文本(&str)传给回调
    → 回调里 deserialize_result::<T::Result> 转成强类型   (lsp.rs L1486)

入站请求/通知:
  分帧 → 解析为 NotificationOrRequest（见 4.3）
    → 按 method 查 notification_handlers 表
    → deserialize_params::<P> 转成强类型                  (lsp.rs L1233/L1262)
    → 若是请求: 构造 Response { LspResult::Ok/Error } 回写  (lsp.rs L1271-L1285)
```

#### 4.2.3 源码精读

**（1）出站请求 `Request`**：[src/lsp.rs:L258-L268](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L258-L268) 定义了请求消息：`jsonrpc` 固定为 `&'static str` 常量、`id` 是 `RequestId`、`method` 借用调用方字符串、`params` 是泛型 `T` 并标注 `#[serde(default, skip_serializing_if = "is_unit")]`——当 `T = ()` 时整个 `params` 字段不会出现在 JSON 里（例如 `Shutdown` 请求的 params 类型是 `()`，发出的报文就没有 `params` 键）。

真实序列化点在 [src/lsp.rs:L1464-L1471](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1464-L1471)：`request_internal_with_timer` 先用原子计数器 `fetch_add` 分配 id，再把 `Request` 序列化成字符串，`.expect(...)` 表明序列化失败被视为不可恢复的编程错误。

**（2）出站通知 `Notification`**：[src/lsp.rs:L303-L313](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L303-L313) 与 `Request` 几乎一致，唯一区别是**没有 `id` 字段**。`method` 上的 `#[serde(borrow)]` 允许从输入字符串零拷贝地借用。它的序列化发生在 [src/lsp.rs:L1618-L1625](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1618-L1625) 的 `notify_internal`：注意这里序列化被包进 `NotificationSerializer` 闭包**延迟执行**——发送的不是一个已序列化的字符串，而是「将来如何序列化」的指令（为什么要延迟，u3-l1 会专门讲）。

**（3）入站响应 `AnyResponse`**：[src/lsp.rs:L271-L279](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L271-L279) 是「还不知道具体类型」的响应形态：

- `result: Option<&'a RawValue>` 配合 `#[serde(borrow)]`：反序列化时**不解析** result 的内容，只是借用指向原始 JSON 文本的引用，零拷贝。
- `error` 与 `result` 都标了 `skip_serializing_if = "Option::is_none"`：序列化时为 `None` 的一方整个字段消失——这正是「result 与 error 不同时出现」的实现手段。

它在入站方向被使用于 [src/input_handler.rs:L110-L128](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L110-L128)：解析出 `id` 后从 `response_handlers` 表摘除回调，然后按三种情况调用——有 `error` 就传 `Err(error)`；有 `result` 就传 `Ok(result.get().into())`（`&RawValue::get()` 得到 `&str`，`.into()` 变成 `String`，回调签名见 [src/lsp.rs:L81](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L81)）；两者都没有就传 `Ok("null".into())`。

**（4）出站响应 `Response` 与 `LspResult`**：[src/lsp.rs:L284-L290](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L284-L290) 的 `Response<T>` 内层字段 `value` 标注 `#[serde(flatten)]`，把 [src/lsp.rs:L292-L298](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L292-L298) 的 `LspResult` 摊平到顶层：

- `LspResult::Ok(Option<T>)` 显式 `rename = "result"` → 序列化为 `"result": ...`
- `LspResult::Error(Option<Error>)`（snake_case）→ 序列化为 `"error": ...`

枚举只有这两个变体且 `flatten` 内联，所以**任意时刻恰好只有一个键出现**。

真实构造点在 [src/lsp.rs:L1270-L1290](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1270-L1290)（`on_custom_request` 的异步回写）：handler 成功返回就构造 `LspResult::Ok(Some(result))`；返回 `Err` 就构造 `LspResult::Error(Some(Error { code: REQUEST_FAILED, ... }))`。另有两处直接用 `AnyResponse` 发错误响应：参数解析失败回 `-32700`（Parse error，[src/lsp.rs:L1299-L1308](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1299-L1308)），未识别的方法回 `-32601`（MethodNotFound，[src/lsp.rs:L530-L547](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L530-L547)）。

**（5）`Error`**：[src/lsp.rs:L325-L331](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L325-L331) 就是 JSON-RPC 错误对象 `{code, message, data}`。注意一个精妙的对比：`data` 只有 `#[serde(default)]` 而**没有** `skip_serializing_if`，所以 `data: None` 会序列化出 `"data":null`（既有测试的期望串里能直接看到这一点）；而 `AnyResponse.result` 为 `None` 时整个字段消失。同一个文件里两种策略并存，各有用意——错误对象保持形状稳定，响应消息严格遵守「恰含其一」。

#### 4.2.4 代码实践

**实践目标**：亲手复刻并扩展两个序列化测试，验证「error 响应不含 result 字段」与「Ok(None) 不含 error 字段」。

**操作步骤**：

1. 先运行既有测试作为基准（这两个是纯 `#[test]`，不需要 gpui 测试环境）：

   ```bash
   cargo test -p lsp test_serialize
   ```

   应看到 `test_serialize_error_response_has_no_result` 与 `test_serialize_has_no_nulls` 通过。它们的源码在 [src/lsp.rs:L2367-L2406](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L2367-L2406)。

2. 在 `tests` 模块中新增自己的变体（示例代码）：

   ```rust
   #[test]
   fn test_error_response_omits_result_key() {
       let response = AnyResponse {
           jsonrpc: JSON_RPC_VERSION,
           id: RequestId::Int(7),
           error: Some(Error {
               code: -32601,
               message: "method not found".to_string(),
               data: None,
           }),
           result: None,
       };
       let text = serde_json::to_string(&response).unwrap();
       // error 响应中不能出现 "result" 键
       assert!(!text.contains("\"result\""));
       // 但 Error.data 没有 skip，会保留 "data":null
       assert!(text.contains("\"data\":null"));
       println!("{text}");
   }

   #[test]
   fn test_ok_response_omits_error_key() {
       let response = Response::<u32> {
           jsonrpc: "",
           id: RequestId::Int(0),
           value: LspResult::Ok(None),
       };
       let text = serde_json::to_string(&response).unwrap();
       assert_eq!(text, "{\"jsonrpc\":\"\",\"id\":0,\"result\":null}");
       assert!(!text.contains("\"error\""));
   }
   ```

3. 运行：

   ```bash
   cargo test -p lsp test_error_response_omits_result_key test_ok_response_omits_error_key
   ```

**需要观察的现象**：第一条测试的输出 JSON 里只有 `error` 键、没有 `result` 键，但 `data:null` 仍在；第二条输出 `"result":null` 且没有 `error` 键。

**预期结果**：两个测试通过；`println!` 打出的字符串与 [src/lsp.rs:L2381](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L2381)、[src/lsp.rs:L2395](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L2395) 的既有断言一致。（运行结果待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：`AnyResponse.result` 为什么用 `Option<&'a RawValue>` 而不是 `Option<Value>`？

**答案**：两个原因。其一，分帧时刻不知道这条响应对应哪个请求、result 应该反序列化成什么类型，只能先「原样拿着」；`RawValue` 配合 `#[serde(borrow)]` 从输入字节零拷贝借用原始 JSON 文本，避免先解析成 `Value` 树再重新序列化的双重开销。其二，文本原样传递还能避免 `Value` 解析-再序列化可能带来的键序变化与浮点精度差异。

**练习 2**：`Error.data` 会序列化出 `"data":null`，而 `AnyResponse.result` 为 `None` 时整个键消失——为什么同一文件里两种策略并存？

**答案**：`Error.data`（[src/lsp.rs:L329-L330](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L329-L330)）只有 `#[serde(default)]`，字段形状保持稳定、消费端可以无条件读取；`AnyResponse.result`（[src/lsp.rs:L277-L278](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L277-L278)）额外有 `skip_serializing_if = "Option::is_none"`，因为 JSON-RPC 规定响应里 `result` 与 `error` 必须恰含其一——如果 `None` 时仍输出 `"result":null`，错误响应就会同时含两个键，部分服务器会因此出错（既有测试注释里提到的 ticket #10595 就是这一类问题）。

**练习 3**：`Response<T>` 为什么不直接写 `result: Option<T>` 和 `error: Option<Error>` 两个字段，而要引入 `LspResult` 枚举加 `flatten`？

**答案**：两个 `Option` 字段在类型上允许同时为 `Some` 或同时为 `None`，无法在类型层面表达「恰含其一」；而 `LspResult` 枚举只有 `Ok`/`Error` 两个变体，一次只能取其一，配合 `flatten` + `rename` 直接摊平成顶层 `"result"` 或 `"error"` 键。把约束从「约定」提升为「类型」，这是 Rust 建模的典型手法。

### 4.3 NotificationOrRequest：粗解析的中间形态

#### 4.3.1 概念说明

 stdout 上来的消息可能是通知、服务器请求或响应，分帧代码**事先无法知道**每条消息对应哪个具体 Rust 类型。crate 的做法是引入一个「粗解析」中间形态 `NotificationOrRequest`：

- `id: Option<RequestId>` —— `Some` 表示这是请求（需要答复），`None` 表示这是通知（不用答复）；
- `method: String` —— 分发的钥匙，用它去查 handler 表；
- `params: Option<Value>` —— 通用 JSON 值，等找到 handler 后再用 `deserialize_params` 转成具体类型。

它把「识别消息种类」和「解析具体参数」两个阶段解耦：前者在读取任务里完成，后者延迟到前台分发时完成。后续 `handle_incoming_messages` 主循环（u3-l4 精读）消费的正是这个类型。

#### 4.3.2 核心流程

[src/input_handler.rs:L108-L135](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L108-L135) 的分发逻辑是一个**按顺序尝试的两分支**结构：

```text
一段完整的消息字节（已按 Content-Length 切出）
  │
  ├─ 先尝试解析为 NotificationOrRequest
  │    成功条件：必须有 "method" 字段
  │    → send 进 incoming_messages 通道，交给前台分发
  │
  ├─ 失败则尝试解析为 AnyResponse
  │    成功条件：必须有 "id"（method 缺失导致上一分支失败）
  │    → 按 id 从 response_handlers 摘除回调并执行
  │
  └─ 都失败 → warn!("failed to deserialize LSP message")
```

**顺序不能颠倒**，这是本模块最关键的洞察：

- 请求 `{"jsonrpc","id","method","params"}` 两个分支都能解析成功（`AnyResponse` 会忽略它不认识的 `method` 键）——先试 `NotificationOrRequest` 才能保证请求被正确识别为请求；若反过来，请求会被误当响应处理。
- 响应 `{"jsonrpc","id","result"}` 没有 `method`，`NotificationOrRequest` 必然失败（`method: String` 无默认值），自然落到第二分支。

判别两种消息的真正依据因此是「**有没有 `method` 字段**」，而不是「有没有 `id`」。

#### 4.3.3 源码精读

结构体定义在 [src/lsp.rs:L316-L323](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L316-L323)：三个字段中 `id` 与 `params` 都标了 `#[serde(default)]`（缺失即 `None`），唯独 `method` 没有默认值——正是这一点让它充当「请求/通知 vs 响应」的判别字段。

分发实现在 [src/input_handler.rs:L108-L109](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L108-L109)：`serde_json::from_slice::<NotificationOrRequest>(&buffer)` 成功就 `send(msg).await` 进容量 128 的有界通道（[src/input_handler.rs:L62](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L62)，背压设计是 u4-l1 的主题）。

下游消费侧的类型签名也能看到它的中枢地位：`new_internal` 的 `on_unhandled_notification` 回调以 `&NotificationOrRequest` 为参数（[src/lsp.rs:L516](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L516)），主循环 `handle_incoming_messages` 的兜底回调同样以它为参数（[src/lsp.rs:L649](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L649)）。

#### 4.3.4 代码实践

**实践目标**：用三条真实形状的报文验证两分支分发的判别规则。

**操作步骤**：在 `tests` 模块新增（示例代码）：

```rust
#[test]
fn test_dispatch_branch_selection() {
    // 1) 服务器→客户端的请求：带 method 与 id
    let request = r#"{"jsonrpc":"2.0","id":1,"method":"workspace/configuration","params":{}}"#;
    let msg = serde_json::from_str::<NotificationOrRequest>(request).unwrap();
    assert_eq!(msg.id, Some(RequestId::Int(1)));
    assert_eq!(msg.method, "workspace/configuration");

    // 2) 通知：带 method 但没有 id
    let notification = r#"{"jsonrpc":"2.0","method":"textDocument/didOpen","params":{}}"#;
    let msg = serde_json::from_str::<NotificationOrRequest>(notification).unwrap();
    assert_eq!(msg.id, None);

    // 3) 响应：没有 method → 第一分支必然失败，落入 AnyResponse
    let response = r#"{"jsonrpc":"2.0","id":1,"result":{"capacity":8}}"#;
    assert!(serde_json::from_str::<NotificationOrRequest>(response).is_err());
    let parsed: AnyResponse = serde_json::from_str(response).unwrap();
    assert_eq!(parsed.id, RequestId::Int(1));
    assert!(parsed.error.is_none());
    // result 以原始 JSON 文本形式被借用保留
    assert_eq!(parsed.result.unwrap().get(), r#"{"capacity":8}"#);
}
```

运行：

```bash
cargo test -p lsp test_dispatch_branch_selection
```

**需要观察的现象**：第 3 组断言中 `from_str::<NotificationOrRequest>` 对响应报文返回 `Err`（缺 `method`），而 `AnyResponse` 解析成功且 `result.get()` 返回的是**未重新格式化的原始文本** `"{"capacity":8}"`。

**预期结果**：测试通过，三条判别规则全部成立。（运行结果待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：如果把 input_handler 里两个分支的顺序对调（先试 `AnyResponse` 再试 `NotificationOrRequest`），会发生什么？

**答案**：服务器发来的请求（带 `id` 和 `method`）会被 `AnyResponse` 成功解析——serde 默认忽略未知字段，`method` 会被丢弃——然后按 `id` 去 `response_handlers` 表里找回调。此时该 id 并非我方发出的请求 id，大概率找不到回调而被静默丢弃，即使碰巧撞上也只是触发了错误的处理逻辑。所以顺序是协议正确性的一部分，不能颠倒。

**练习 2**：一条既没有 `method` 也没有 `id` 的报文（例如某些服务器输出的杂质输出）命运如何？

**答案**：`NotificationOrRequest` 因缺 `method` 失败，`AnyResponse` 因缺 `id` 失败，最终落入 [src/input_handler.rs:L129-L134](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L129-L134) 的 `warn!("failed to deserialize LSP message")`，消息被丢弃但循环继续，不会导致读取任务崩溃。

**练习 3**：`NotificationOrRequest.params` 为什么是 `Option<Value>` 而不像 `Request` 那样做成泛型？

**答案**：泛型要求在编译期知道类型，而读取任务在分帧时刻只知道字节。`method` 字符串要等到前台查 `notification_handlers` 表之后才能确定 params 的具体类型，所以这里只能先保存通用 `Value`，把强类型化推迟到 `deserialize_params`（见 4.4）。这也是「粗解析」名字的由来。

### 4.4 is_unit、deserialize_params、deserialize_result：unit 类型的特殊处理

#### 4.4.1 概念说明

LSP 里有不少「没有参数」或「没有返回值」的方法，Rust 侧自然用 `()` 建模（例如 `Shutdown` 请求的 `Params = ()`、`Result = ()`）。但现实世界的服务器并不统一：

- 有的对无参数请求发 `"params": {}`（空对象），有的干脆不发 `params`；
- 有的对无返回值请求回 `"result": {}`，有的回 `"result": null`。

而 serde 的规则很严格：`()` 只能从 JSON `null` 反序列化，`{}` 会直接报错。如果不做处理，一个「用 `{}` 表示空」的服务器会让客户端解析失败。

crate 用三个小函数统一解决，核心手段是 `TypeId` 在运行时识别 `T` 是不是 `()`：

| 函数 | 用在哪一端 | 行为 |
| --- | --- | --- |
| `is_unit::<T>` | 序列化 | `T = ()` 时让 `skip_serializing_if` 省略整个 `params` 字段 |
| `deserialize_params::<T>` | 反序列化入站 params | `T = ()` 时无视收到的任何内容，一律从 `Value::Null` 解析成 `()` |
| `deserialize_result::<T>` | 反序列化入站 result | `T = ()` 时无视原始文本（哪怕是 `{}`），一律从 `"null"` 解析成 `()` |

#### 4.4.2 核心流程

```text
出站（T = ()）:
  Request/Notification 序列化
    → skip_serializing_if = is_unit 命中
    → JSON 中完全没有 "params" 键

入站 params（P = ()）:
  服务器发来 {} 或 null 或任意值
    → deserialize_params: TypeId 判定 P 是 ()
    → from_value(Value::Null) → Ok(())      # 无视真实内容

入站 result（R = ()）:
  服务器发来 "result": {} 或 "result": null
    → deserialize_result: TypeId 判定 R 是 ()
    → from_str("null") → Ok(())             # 无视原始文本
```

三个函数的共同前提是 `'static` 约束——`TypeId::of::<T>()` 只对不含非静态生命周期的类型可用，所以你会看到 `where T: 'static` 出现在 `Request`、`Notification` 的泛型约束里。

#### 4.4.3 源码精读

三个函数集中在 [src/lsp.rs:L235-L253](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L235-L253)：`is_unit` 用 `TypeId` 相等性判断 `T` 是否为 `()`；`deserialize_params` 与 `deserialize_result` 在 `T = ()` 时分别改从 `Value::Null` 与字符串 `"null"` 解析。

使用点回看前文：

- 序列化端：`Request.params` 与 `Notification.params` 的 `skip_serializing_if = "is_unit"`（[src/lsp.rs:L266-L267](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L266-L267)、[src/lsp.rs:L311-L312](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L311-L312)）。
- 反序列化 params：通知 handler 里 [src/lsp.rs:L1233](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1233)，请求 handler 里 [src/lsp.rs:L1262](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1262)。
- 反序列化 result：响应回调里 [src/lsp.rs:L1486](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1486)。

两个集成测试直接验证了这套机制对「不规矩服务器」的容错：

- [src/lsp.rs:L2260-L2303](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L2260-L2303)：fake 服务器分别用 `{}` 和 `null` 作为 params 发送 `workspace/diagnostic/refresh`（其 `Params = ()`），客户端两种都能正确收到 `()` 并触发 handler。
- [src/lsp.rs:L2305-L2338](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L2305-L2338)：fake 对 `Shutdown`（其 `Result = ()`）回 `"result": {}`，客户端照样得到 `()`。

#### 4.4.4 代码实践

**实践目标**：通过运行两个既有集成测试，观察 unit 特殊处理如何吞掉服务器发来的 `{}`。

**操作步骤**：

1. 运行（两个测试名都以 `test_unit_` 开头，可用前缀一次匹配）：

   ```bash
   cargo test -p lsp test_unit_
   ```

2. 阅读 [src/lsp.rs:L2296-L2302](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L2296-L2302)：测试用 `serde_json::json!({})` 和 `Value::Null` 两种 params 循环两次发请求，断言都返回 `()`。

3. 做一个小实验，对照「serde 原生规则」与「crate 的 workaround」——在 `tests` 模块里加一个对照测试（示例代码）：

   ```rust
   #[test]
   fn test_unit_type_rejects_empty_object_without_workaround() {
       // serde 的原生规则：() 只接受 null，不接受 {}
       assert!(serde_json::from_value::<()>(serde_json::json!({})).is_err());
       assert!(serde_json::from_value::<()>(serde_json::json!(null)).is_ok());
       // crate 的 workaround：deserialize_params 无视输入，() 永远成功
       assert!(deserialize_params::<()>(serde_json::json!({})).is_ok());
   }
   ```

   运行：

   ```bash
   cargo test -p lsp test_unit_type_rejects_empty_object_without_workaround
   ```

**需要观察的现象**：原生 serde 规则下 `from_value::<()>(json!({}))` 是 `Err`；而 `deserialize_params::<()>` 对同样的 `{}` 返回 `Ok(())`——差异完全来自 [src/lsp.rs:L240-L242](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L240-L242) 的 `TypeId` 分支把输入换成了 `Value::Null`。

**预期结果**：`test_unit_` 前缀的两个集成测试通过；新增的对照测试也通过。（运行结果待本地验证。）

#### 4.4.5 小练习与答案

**练习 1**：`is_unit` 在序列化端解决了什么问题？举一个 Zed 中真实的例子。

**答案**：当 params 类型是 `()` 时，`skip_serializing_if = "is_unit"` 让 `params` 字段完全不出现，而不是输出 `"params":null`。部分服务器遇到 `null` params 会报错，省略字段是最保守的形状。真实例子：`Shutdown` 请求（`Params = ()`），发出的报文就是 `{"jsonrpc":"2.0","id":N,"method":"shutdown"}`，没有 `params` 键。

**练习 2**：`deserialize_params::<()>` 为什么要把输入替换成 `Value::Null` 再解析，而不是直接返回 `Ok(())`？

**答案**：功能上等价，但复用 `serde_json::from_value(Value::Null)` 保持了统一的代码路径——所有类型都走同一个 `from_value` 调用，只是输入在 `T = ()` 时被规约成 `Null`。这样实现最短，也不需要为 `()` 单写一个分支返回值。（同时 `from_value(Value::Null)` 对 `()` 是必然成功的，语义上就是「强制成功」。）

**练习 3**：服务器对 `Shutdown` 回了 `"result": {}`，从字节到 `()` 的完整链路是什么？

**答案**：分帧后先在 input_handler 尝试 `NotificationOrRequest`——因缺 `method` 失败；改用 `AnyResponse` 解析成功，`result` 以 `&RawValue` 借用保留原始文本 `"{}"`（[src/input_handler.rs:L110-L124](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L110-L124)）；按 `id` 摘除回调并以 `Ok("{}")` 调用；回调内 `deserialize_result::<()>`（[src/lsp.rs:L1486](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/lsp.rs#L1486)）判定 `T = ()`，改从 `"null"` 解析得到 `Ok(())`，经 oneshot 通道送回请求 future。

## 5. 综合实践

**任务：搭建你的「迷你协议实验室」**——用一个测试把本讲四个模块串起来：解析一条服务器请求 → 判别种类 → 构造强类型响应 → 验证输出形状。

在 `src/lsp.rs` 的 `tests` 模块中新增（示例代码）：

```rust
#[gpui::test]
fn test_message_lab_roundtrip() {
    // ── 第一步：解析一条 server→client 的 workspace/configuration 请求 ──
    let incoming = r#"{"jsonrpc":"2.0","id":"cfg-1","method":"workspace/configuration","params":{"items":[{"section":"rust-analyzer"}]}}"#;
    let msg = serde_json::from_str::<NotificationOrRequest>(incoming).unwrap();

    // ── 第二步：用 id 判别「请求」还是「通知」──
    let id = msg.id.expect("message with id is a request, not a notification");
    assert_eq!(id, RequestId::Str("cfg-1".to_string()));

    // ── 第三步：模拟 handler 返回配置，构造强类型响应 ──
    let items = vec![serde_json::json!({"hover": true})];
    let ok = Response {
        jsonrpc: JSON_RPC_VERSION,
        id: id.clone(),
        value: LspResult::Ok(Some(items)),
    };
    let ok_text = serde_json::to_string(&ok).unwrap();
    assert!(ok_text.contains("\"result\""));
    assert!(!ok_text.contains("\"error\""));

    // ── 第四步：构造失败路径，验证互斥性 ──
    let err = Response {
        jsonrpc: JSON_RPC_VERSION,
        id,
        value: LspResult::Error(Some(Error {
            code: -32601,
            message: "no such section".to_string(),
            data: None,
        })),
    };
    let err_text = serde_json::to_string(&err).unwrap();
    assert!(err_text.contains("\"error\""));
    assert!(!err_text.contains("\"result\""));
}
```

完成后：

1. 运行 `cargo test -p lsp test_message_lab_roundtrip`，确认通过。
2. 在每个 `assert!` 旁边用注释写清它对应本讲的哪条规则（untagged 顺序匹配 / method 判别 / flatten 互斥 / ……）。
3. 把 `id` 换成数字、把 params 换成别的嵌套结构再跑一遍，观察哪些断言需要跟着变——这能检验你是否真的理解了每条规则。
4. 完成后建议在本地分支还原 `src/lsp.rs` 的改动（或用 `git checkout -p` 摘除测试），不要把练习代码带入正式提交。

**预期结果**：测试通过；两段序列化文本分别只含 `result` / `error` 之一。（运行结果待本地验证。）

## 6. 本讲小结

- LSP 只有三种消息：请求（有 `id` 有 `method`）、通知（无 `id`）、响应（有 `id`、`result`/`error` 恰含其一）；分别由 `Request`/`Notification`/`Response`+`LspResult` 等结构体建模。
- `RequestId` 是 `#[serde(untagged)]` 的 `Int(i32)/Str(String)`：序列化直接输出内层值，反序列化按声明顺序尝试，`"2"` 保持字符串身份。
- 入站采用「粗解析」策略：`NotificationOrRequest`（带 `method` 的消息）与 `AnyResponse`（响应，`result` 以 `&RawValue` 零拷贝借用）两个弱类型形态，把强类型化推迟到查表分发之后。
- `LspResult` 枚举 + `flatten` + `rename` 在类型层面保证响应中 `result` 与 `error` 互斥；`skip_serializing_if` 则让 `AnyResponse` 的空字段彻底消失。
- `is_unit` / `deserialize_params` / `deserialize_result` 用 `TypeId` 识别 `()`，统一了各服务器对「空」的不同表达（`{}` / `null` / 省略字段）。
- 既有测试 `test_deserialize_*`、`test_serialize_*`、`test_unit_*` 是这些行为的活文档，改动消息模型前先读它们。

## 7. 下一步学习建议

本讲解决了「消息长什么样」；下一讲 **u1-l3「stdout 分帧读取：从字节流到消息」**解决「消息怎么从字节流里切出来」：精读 `src/input_handler.rs` 的 `read_headers` 增量读取循环、`Content-Length` 头解析，以及解析后如何进入本讲分析过的两分支分发。建议提前浏览 [src/input_handler.rs:L35-L50](https://github.com/zed-industries/zed/blob/28c0f4aef89d298a08176977d9839671827f5528/crates/lsp/src/input_handler.rs#L35-L50) 的 `read_headers`，并思考一个问题：为什么它要循环地 `read_until(b'\n')` 而不是一次性读固定长度？答案下一讲揭晓。
