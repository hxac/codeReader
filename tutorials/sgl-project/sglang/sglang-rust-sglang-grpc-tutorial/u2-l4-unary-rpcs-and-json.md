# 一元 RPC 与 JSON 响应解析：embed / classify / list_models 等

> 本讲是进阶层第 4 篇。阅读前请确认你已经学过 **u2-l2（服务实现总览）**，知道 `SglangServiceImpl` 是一个薄壳、25 个 RPC 分为「流式 / 一元」两类，以及 `submit_request` / `submit_json` 两种公共提交模式。本讲不再重复这些结论，而是把放大镜对准**一元（unary）RPC**：它如何只收一个「终止 chunk」、如何把 Python 返回的 JSON 字符串解析成 proto 响应、如何在字段缺失时做默认值兜底。

---

## 1. 本讲目标

学完本讲，你应当能够：

1. 说出**一元 RPC 的终止 chunk 契约**：为什么一元响应只期望 `Finished`（或 `Error`），而中途收到 `Data` 会被当作「协议违例」直接报错。
2. 读懂 `recv_terminal_chunk_for_request` 这个「一元收银台」：它如何用超时、`RequestAbortGuard`、`closed_stream_status` 把一个 mpsc 通道收尾成一个确定的结果。
3. 掌握「Python JSON 字符串 → proto 响应 + 默认值兜底」的两种写法：通道型（`recv_json_response`）与同步直调型（`list_models`）。
4. 解释 **`classify` 为什么复用 `embed` 的提交路径**（`req_type="embed"`），并把它和 proto 注释、`EmbeddingReqInput`、`build_classify_dict` 串起来。
5. 看懂 `list_models` 从 JSON 数组到 `proto::ModelCard` 的字段映射，并理解「必填字段用 `unwrap_or`、`optional` 字段用 `.get().and_then()`」这一对应关系。

---

## 2. 前置知识

本讲用到的几个概念，先用大白话过一遍：

- **一元 RPC（unary RPC）**：客户端发一个请求、服务端回**一个**响应就结束，中间没有流。对应 proto 里 `returns (FooResponse)`（没有 `stream` 关键字）。本讲关注的 `TextEmbed` / `Embed` / `Classify` / `ListModels` / `FlushCache` 等都是一元。流式 RPC（`returns (stream ...)`）是上一讲 u2-l3 的内容。
- **ResponseChunk**：Rust 桥接层（`bridge.rs`）里通道传递的「响应片段」，有三个变体：`Data(ResponseData)`（中间数据）、`Finished(ResponseData)`（正常终结）、`Error(String)`（出错终结）。其中只有 `Finished` 和 `Error` 是「终止 chunk」。
- **rid**：每个请求的唯一标识（字符串），用来在桥接层里找到对应的 mpsc 通道。
- **serde_json**：Rust 里最常用的 JSON 库。本讲会反复用到 `serde_json::from_str` 把字符串解析成 `serde_json::Value`，再用 `value["key"].as_str()` / `value.get("key").and_then(|v| v.as_i64())` 之类的方式取字段。
- **proto3 的 `optional`**：在 proto3 里，普通 `string`/`int32` 字段即便没赋值也有默认值（空串 / 0），无法区分「没传」和「传了空」。加了 `optional` 关键字后，生成的 Rust 类型会变成 `Option<String>` / `Option<i32>`，于是「缺失」和「有值」就能区分了。这个细节在 `ModelCard` 的字段映射里会用到。

> 关键术语回顾（来自 u2-l2）：`SglangServiceImpl` 只持有 `bridge: Arc<PyBridge>` 和 `response_timeout: Duration` 两个字段；请求级状态全在 `bridge` 内部。本讲讲的所有一元方法，都是「调 bridge 提交 → 收一个终止 chunk → 解析」三步走。

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到的地方 |
| --- | --- | --- |
| `rust/sglang-grpc/src/server.rs` | gRPC 服务实现主体 | 一元 RPC 方法、`recv_terminal_chunk_for_request`、`recv_json_response`、`closed_stream_status`、`RequestAbortGuard` |
| `rust/sglang-grpc/src/bridge.rs` | Python 桥接层 | `ResponseChunk` / `ResponseData` / `TerminalError` 定义、`submit_request`、`list_models()` |
| `rust/sglang-grpc/src/utils/request_utils.rs` | proto → Python dict 构造 | `build_text_embed_dict`、`build_embed_dict`、`build_classify_dict` |
| `proto/sglang/runtime/v1/sglang.proto` | gRPC 契约 | `ClassifyRequest` / `ClassifyResponse` / `ModelCard` 及 classify 的关键注释 |

本讲的所有行号与永久链接均基于 HEAD `977ea336cd3e960141c4c6e746b4efc24fdf312e`。

---

## 4. 核心概念与源码讲解

本讲按「先讲共用的收尾协议，再讲三类一元方法」的顺序展开，共四个最小模块：

- **4.1** 一元收银台：`recv_terminal_chunk_for_request`（所有结构化/通道型一元 RPC 的共用收尾）
- **4.2** 结构化一元：`text_embed` / `embed`（embedding 透传）
- **4.3** `classify`：复用 `embed` 内部路径（`req_type="embed"` 的来龙去脉）
- **4.4** JSON 控制型一元：`recv_json_response` 与 `list_models`（JSON 解析 + 默认值兜底）

---

### 4.1 一元收银台：recv_terminal_chunk_for_request

#### 4.1.1 概念说明

流式 RPC 会从通道里收**一连串** `Data` 再收一个 `Finished`；而一元 RPC 只想要**一个最终结果**。如果在「只想收一个」的语义下，通道却吐出一个非终结的 `Data`，说明生产端（Python 那边）的输出协议和我们预期不一致——这是一个**协议违例**，应当报错而不是默默吞掉。

`recv_terminal_chunk_for_request` 就是「一元收银台」：它从通道里收**一个** chunk，并强制执行「一元只接受终止 chunk」的契约。它还顺带处理三类异常：超时、通道提前关闭、出错终结，并在任何「客户端不再消费」的情形下通过 `RequestAbortGuard` 把取消信号传回 Python。

> 为什么需要一个独立的收尾函数？因为 `text_embed` / `embed` / `classify` / `openai_unary_rpc` 这些一元方法的「收尾」逻辑完全一样（超时、abort、关闭判定）。抽出来既避免重复，也保证语义一致——尤其是「`Data` 即违例」这一条在每个一元方法里都成立。

#### 4.1.2 核心流程

设终止 chunk 集合为：

\[
T = \{\,\text{Finished},\ \text{Error}\,\}
\]

一元收银台从通道收一个 chunk 后，分四种结果处置：

```
recv_chunk_with_timeout(receiver, timeout)
        │
        ├─ Ok(Some(Data(_)))        → 协议违例：abort 后返回 INTERNAL
        ├─ Ok(Some(Finished|Error)) → 正常：disarm guard，把 chunk 返还给调用方
        ├─ Ok(None)                 → 通道关闭：查 closed_stream_status 决定是否 abort
        └─ Err(status)              → 超时(DEADLINE_EXCEEDED) 则 abort，否则 disarm
```

两条关键不变量：

1. **成功返回的 chunk 一定是 `Finished` 或 `Error`，绝不可能是 `Data`。** 这一点直接决定了 4.2 里调用方 `match` 中的 `Data` 分支是「写出来满足穷尽性、但实际走不到」的死代码。
2. **任何「客户端不再消费」的结局都会触发 `abort`**（通过 `RequestAbortGuard`），保证 Python 侧的生成不会因为 gRPC 客户端断开而空转。

#### 4.1.3 源码精读

收银台本体（[src/server.rs:141-186](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L141-L186)）：

```rust
async fn recv_terminal_chunk_for_request(
    bridge: &Arc<PyBridge>,
    rid: &str,
    receiver: &mut Receiver<ResponseChunk>,
    response_timeout: Duration,
) -> Result<ResponseChunk, Status> {
    let mut abort_guard = RequestAbortGuard::new(bridge.clone(), rid.to_string());

    match recv_chunk_with_timeout(receiver, response_timeout, || {
        format!("Request timed out after {}s", response_timeout.as_secs())
    }).await {
        Ok(Some(ResponseChunk::Data(_))) => {
            // 一元响应不该收到中间 Data：报协议违例
            abort_guard.abort_now();
            Err(Status::internal(
                "Unary response protocol violation: expected Finished, got Data",
            ))
        }
        Ok(Some(chunk @ (ResponseChunk::Finished(_) | ResponseChunk::Error(_)))) => {
            abort_guard.disarm();   // 正常终结，不再需要 abort
            Ok(chunk)
        }
        Ok(None) => { /* 通道关闭，见 closed_stream_status */ }
        Err(status) => { /* 超时或其它 */ }
    }
}
```

配套的几个小函数也在此文件内：

- 底层超时收取（[src/server.rs:78-86](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L78-L86)）：用 `tokio::time::timeout` 包住 `receiver.recv()`，超时返回 `DEADLINE_EXCEEDED`。
- 通道关闭判定（[src/server.rs:188-197](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L188-L197)）：`Ok(None)` 时先看 bridge 里有没有预先存好的 `TerminalError`（比如 `ChannelFull` / `Aborted`）；有就映射成对应状态码、且**不需要**再 abort；没有则视为「流异常关闭」，需要 abort。
- `RequestAbortGuard`（[src/server.rs:88-123](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L88-L123)）：RAII 守卫，`disarm()` 表示「正常结束、别 abort」；`abort_now()` 主动触发；`Drop` 时若仍 armed 则触发——保证流提前结束（比如客户端断开）时取消能传回 Python。

> 关于 `ResponseChunk` 三个变体的定义见 [src/bridge.rs:13-24](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L13-L24)，其中 `is_terminal()` 只对 `Finished` / `Error` 返回 true。`TerminalError` 的三变体（`ChannelFull` / `ClientDisconnected` / `Aborted`）见 [src/bridge.rs:48-67](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L48-L67)，它们到 gRPC 状态码的映射是 u3-l3（错误映射）的主题，本讲只在「通道关闭」时顺带用到。

#### 4.1.4 代码实践

**实践目标**：用「源码阅读」验证「一元成功路径只可能返回 `Finished` / `Error`」这条不变量，并确认 `Data` 分支在调用方是死代码。

**操作步骤**：

1. 打开 [src/server.rs:141-186](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L141-L186)，逐分支写下 `recv_terminal_chunk_for_request` 在四种输入下分别返回什么。
2. 跳到 [src/server.rs:384-392](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L384-L392)（`text_embed` 的 `match chunk`），观察它同时列出了 `Data` 与 `Finished` 两个 arm。
3. 反问自己：既然收银台绝不可能返回 `Ok(Data)`，那 `text_embed` 里的 `ResponseChunk::Data(data)` arm 何时会被命中？

**需要观察的现象**：在 `recv_terminal_chunk_for_request` 的成功分支里，只有 `Ok(Some(chunk @ (Finished | Error)))` 会 `Ok(chunk)`；`Data` 在更上面就被转成了 `Err`。

**预期结果**：`text_embed` 的 `ResponseChunk::Data(data) | ResponseChunk::Finished(data)` 合并 arm 中，`Data` 那一半在运行期**永远不会命中**，它存在的唯一原因是 Rust 要求 `match` 对 `ResponseChunk` 的所有变体穷尽。如果你想亲眼看编译器确认，可以在该 arm 的 `Data` 分支里临时写一行 `unreachable!("unary never returns Data")`（**仅本地实验，勿提交**），再 `cargo check`——能编过即说明类型上确实可能走到（因为 `match` 值是 `ResponseChunk`），但运行期不会到。

> 待本地验证：`cargo check -p sglang-grpc` 是否通过（取决于你是否真的加了 `unreachable!`；不加则一定通过）。

#### 4.1.5 小练习与答案

**练习 1**：`recv_terminal_chunk_for_request` 在 `Ok(None)`（通道关闭）时，依据什么决定要不要 `abort_now()`？

**参考答案**：依据 `closed_stream_status` 返回的 `should_abort` 布尔位（[src/server.rs:188-197](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L188-L197)）。如果 bridge 里已经存了该 rid 的 `TerminalError`（说明是桥接层主动关闭，如 `ChannelFull` / `Aborted`），则 `should_abort=false`、只 `disarm`；否则视为「流异常关闭」，`should_abort=true`、调用 `abort_now()` 把取消传回 Python。

**练习 2**：为什么 `recv_terminal_chunk_for_request` 在超时（`DeadlineExceeded`）时要 `abort_now()`，而在某些 `Err` 情况下却 `disarm`？

**参考答案**：超时意味着「客户端等够了时间不再要这个结果」，属于「客户端不再消费」，需要取消 Python 侧的生成，故 `abort_now()`。分支里对其它 `Err`（理论上目前只有超时这一种 `Err` 来源，因为 `recv_chunk_with_timeout` 只会因超时报错）做了防御性的 `disarm`，避免在已经终结的情况下重复 abort。整个设计的核心是：**只有「客户端不再消费」才 abort**。

---

### 4.2 结构化一元 RPC：text_embed / embed

#### 4.2.1 概念说明

`TextEmbed`（文本进、向量出）和 `Embed`（token ids 进、向量出）是最典型的「结构化一元 RPC」：输入是强类型 proto 消息，输出也是强类型 proto 消息（`repeated float embedding` + `map<string,string> meta_info`），全程不碰 JSON。

这两个方法几乎一模一样，唯一区别是输入承载（`text` 字符串 vs `input_ids` 整数数组）和对应的字典构造函数。它们共同示范了**一元结构化方法的标准三段式**：

1. 构造请求字典（`build_text_embed_dict` / `build_embed_dict`）；
2. `bridge.submit_request(rid, "embed", req_dict)` 拿到 mpsc receiver；
3. `recv_terminal_chunk_for_request` 收一个终止 chunk，从 `ResponseData` 里取出 `embedding` 和 `meta_info`。

#### 4.2.2 核心流程

```
TextEmbedRequest ──build_text_embed_dict──▶ Python dict
                                                  │ submit_request(rid, "embed", dict)
                                                  ▼
                            mpsc::Receiver<ResponseChunk>  ◀── ChunkCallback 推送 ── Python 生成
                                                  │ recv_terminal_chunk_for_request
                                                  ▼
                                  ResponseChunk::Finished(ResponseData)
                                                  │ 取 embedding / meta_info
                                                  ▼
                                         TextEmbedResponse
```

`embed`（tokenized）的流程把第一步换成 `build_embed_dict`、输入换成 `input_ids`，其余完全相同。

#### 4.2.3 源码精读

`text_embed`（[src/server.rs:360-393](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L360-L393)）：

```rust
async fn text_embed(
    &self,
    request: Request<proto::TextEmbedRequest>,
) -> Result<Response<proto::TextEmbedResponse>, Status> {
    let req = request.into_inner();
    let rid = req.rid.clone().unwrap_or_else(|| uuid::Uuid::new_v4().to_string());
    let req_dict = build_text_embed_dict(&rid, &req);

    let mut receiver = self.bridge
        .submit_request(&rid, "embed", req_dict)                       // ① 提交
        .map_err(|e| pyerr_to_status(e, "Failed to submit request"))?;

    let chunk = recv_terminal_chunk_for_request(                       // ② 收一个终止 chunk
        &self.bridge, &rid, &mut receiver, self.response_timeout,
    ).await?;

    match chunk {                                                       // ③ 取字段
        ResponseChunk::Data(data) | ResponseChunk::Finished(data) => {
            Ok(Response::new(proto::TextEmbedResponse {
                embedding: data.embedding.unwrap_or_default(),          // None → 空 vec
                meta_info: data.meta_info,
            }))
        }
        ResponseChunk::Error(msg) => Err(Status::internal(msg)),
    }
}
```

几个要点：

- `rid` 缺省时用 `uuid::Uuid::new_v4()` 现场生成（与流式 RPC 一致）。
- `data.embedding.unwrap_or_default()`：`embedding` 是 `Option<Vec<f32>>`，若 Python 没给（极少见）就兜底成空 `Vec`，避免 proto 的 `repeated float` 字段拿到 `None`。
- 第 ③ 步的 `match` 里 `Data` arm 如 4.1 所述是死代码，`Finished` 才是正常路径；`Error(msg)` 转成 `INTERNAL`。

`embed`（tokenized，[src/server.rs:395-428](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L395-L428)）结构完全一致，只是 `build_embed_dict`、`proto::EmbedRequest` / `proto::EmbedResponse`。

字典构造以 `build_text_embed_dict` 为例（[src/utils/request_utils.rs:211-226](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/request_utils.rs#L211-L226)）：写入 `rid` / `text`，按需写入 `routing_key` / `external_trace_header`（仅当非空），最后写入 `received_time`。这些键对应 Python 侧的 `EmbeddingReqInput`。

#### 4.2.4 代码实践

**实践目标**：对比 `text_embed` 与 `embed` 两个方法，确认它们「同构」，并定位所有差异点。

**操作步骤**：

1. 并排打开 [src/server.rs:360-393](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L360-L393) 与 [src/server.rs:395-428](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L395-L428)。
2. 逐行比对，列出「输入 proto 类型、字典构造函数、输出 proto 类型」三处的差异。
3. 确认 `submit_request` 的第二个参数两者都是 `"embed"`。

**需要观察的现象**：除上述三处命名差异外，方法体几乎逐字相同。

**预期结果**：差异表为——`text_embed`：`TextEmbedRequest` / `build_text_embed_dict` / `TextEmbedResponse`（输入是 `text`）；`embed`：`EmbedRequest` / `build_embed_dict` / `EmbedResponse`（输入是 `input_ids`）。两者 `req_type` 都是 `"embed"`，且 `embedding` / `meta_info` 的取法完全一致。

#### 4.2.5 小练习与答案

**练习 1**：`text_embed` 把 `PyErr`（提交失败）映射成什么 gRPC 状态码？依据是哪个函数？

**参考答案**：由 `pyerr_to_status`（[src/server.rs:66-76](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L66-L76)）决定：若 Python 抛的是 `PyValueError` / `PyTypeError`（客户端传错参数）映射为 `INVALID_ARGUMENT`，其余（如 `PyRuntimeError`）映射为 `INTERNAL`。这是 u3-l3 错误映射的主题，这里只需知道「提交失败的错误码由它决定」。

**练习 2**：为什么 `data.embedding` 要用 `unwrap_or_default()`，而 `data.meta_info` 直接用？

**参考答案**：`embedding` 的类型是 `Option<Vec<f32>>`，proto 的 `repeated float embedding` 需要一个 `Vec<f32>`，故用 `unwrap_or_default()` 把 `None` 变成空 `Vec`。`meta_info` 本身就是 `HashMap<String,String>`（非 `Option`），可以直接移交给 proto 的 `map<string,string>` 字段。

---

### 4.3 classify：复用 embed 内部路径

#### 4.3.1 概念说明

`Classify` 表面上是一个独立的 RPC（分类 / 打分），但它在 Rust 这一侧**几乎完全复用了 `embed` 的提交与收尾逻辑**：同样的 `submit_request(..., "embed", ...)`、同样的 `recv_terminal_chunk_for_request`、同样从 `ResponseData.embedding` 取结果。这不是偷懒，而是刻意为之——在 SGLang 的 Python 运行时里，分类/奖励模型与 embedding 模型走的是**同一条 `EmbeddingReqInput` 管线**，模型产出的是「每个输入对应一个向量」，对分类模型而言这个向量就是各类别分数。

proto 文件对此有明确注释（见 4.3.3），`ClassifyResponse` 的输出字段也叫 `repeated float embedding`，与 `EmbedResponse` 同构。

`classify` 相对 `embed` 只多两件事：① 输入校验（`text` 和 `input_ids` 不能同时为空）；② 字典构造用 `build_classify_dict`（它同时支持 `text` 和 `input_ids` 两种承载）。

#### 4.3.2 核心流程

```
ClassifyRequest
   │
   ├─ text 与 input_ids 同时为空？ ──是──▶ INVALID_ARGUMENT（提前返回）
   │否
   ▼
build_classify_dict ──▶ Python dict（含 text 或 input_ids 之一）
   │ submit_request(rid, "embed", dict)        ← 注意 req_type 仍是 "embed"
   ▼
recv_terminal_chunk_for_request
   │ 取 embedding / meta_info
   ▼
ClassifyResponse { embedding, meta_info }
```

#### 4.3.3 源码精读

proto 契约与关键注释（[proto/sglang/runtime/v1/sglang.proto:183](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/proto/sglang/runtime/v1/sglang.proto#L183)）：

```proto
// ---- Classify (same internal path as embed, uses EmbeddingReqInput) ----

message ClassifyRequest {
  string text = 1;
  repeated int32 input_ids = 2;
  optional string rid = 3;
  optional string routing_key = 4;
  map<string, string> trace_headers = 5;
}

message ClassifyResponse {
  repeated float embedding = 1;      // 与 EmbedResponse 同构
  map<string, string> meta_info = 2;
}
```

`classify` 实现（[src/server.rs:432-470](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L432-L470)）：

```rust
async fn classify(
    &self,
    request: Request<proto::ClassifyRequest>,
) -> Result<Response<proto::ClassifyResponse>, Status> {
    let req = request.into_inner();
    if req.text.is_empty() && req.input_ids.is_empty() {              // ① 输入校验
        return Err(Status::invalid_argument(
            "Classify requires either text or input_ids",
        ));
    }
    let rid = req.rid.clone().unwrap_or_else(|| uuid::Uuid::new_v4().to_string());
    let req_dict = build_classify_dict(&rid, &req);

    let mut receiver = self.bridge
        .submit_request(&rid, "embed", req_dict)                      // ② req_type 仍是 "embed"
        .map_err(|e| pyerr_to_status(e, "Failed to submit request"))?;

    let chunk = recv_terminal_chunk_for_request(
        &self.bridge, &rid, &mut receiver, self.response_timeout,
    ).await?;

    match chunk {
        ResponseChunk::Data(data) | ResponseChunk::Finished(data) => {
            Ok(Response::new(proto::ClassifyResponse {
                embedding: data.embedding.unwrap_or_default(),
                meta_info: data.meta_info,
            }))
        }
        ResponseChunk::Error(msg) => Err(Status::internal(msg)),
    }
}
```

`build_classify_dict`（[src/utils/request_utils.rs:247-267](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/request_utils.rs#L247-L267)）的文档注释直接写明 `Build a request dict for EmbeddingReqInput from proto ClassifyRequest`，并且**条件地**写入 `text` 或 `input_ids`（哪个非空写哪个）：

```rust
if !req.text.is_empty() {
    d.insert("text".into(), serde_json::json!(req.text));
}
if !req.input_ids.is_empty() {
    d.insert("input_ids".into(), serde_json::json!(req.input_ids));
}
```

这与 `build_text_embed_dict`（只写 `text`）/ `build_embed_dict`（只写 `input_ids`）不同——classify 允许二选一，所以两处都用 `if !...is_empty()` 守卫。

#### 4.3.4 代码实践

**实践目标**：回答练习任务的核心问题——`classify` 为什么传 `"embed"` 作为 `req_type`。

**操作步骤**：

1. 读 proto 注释 [proto/sglang/runtime/v1/sglang.proto:183](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/proto/sglang/runtime/v1/sglang.proto#L183)，抄下那句注释。
2. 读 `build_classify_dict` 的文档注释 [src/utils/request_utils.rs:246-250](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/request_utils.rs#L246-L250)，确认它构造的是 `EmbeddingReqInput`。
3. 对照 `submit_request` 的文档（[src/bridge.rs:173-182](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L173-L182)），看 `req_type` 取值的说明。

**需要观察的现象**：三处证据指向同一个事实——classify 在 Python 内部走的是 embedding 管线。

**预期结果**（参考答案）：`req_type` 是 Rust 传给 Python `runtime_handle.submit_request` 的一个字符串参数，Python 依据它把请求分发到 `GenerateReqInput`（`"generate"`）或 `EmbeddingReqInput`（`"embed"`）两条管线之一。classify 的输出（类别分数 / 奖励分）与 embedding 一样是「每个输入一个向量」，因此 SGLang 复用 `EmbeddingReqInput` 管线来跑分类/奖励模型，proto 注释「same internal path as embed, uses EmbeddingReqInput」正是此意。于是 Rust 端必须传 `"embed"`，让 Python 把它当 embedding 请求处理；结果向量经 `ChunkCallback` 回到 `ResponseData.embedding`，再原样放进 `ClassifyResponse.embedding`。这就是为什么三个方法（`text_embed` / `embed` / `classify`）的 `submit_request` 第二参数都是 `"embed"`，而唯独 `classify` 的输入同时支持 `text` 和 `input_ids`。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `classify` 的 `submit_request` 第二个参数从 `"embed"` 改成 `"classify"`，会发生什么？

**参考答案**：Python 侧的 `runtime_handle.submit_request` 并没有 `"classify"` 这个分发分支（它的 `req_type` 只接受 `"generate"` / `"embed"` / `"classify"`——注意 bridge.rs 的文档其实列出了三者，但 Rust 端对 classify 仍传 `"embed"`）。即便 Python 认识 `"classify"`，由于 classify 与 embed 共用 `EmbeddingReqInput` 管线，传 `"embed"` 才能确保命中正确的内部路径与回调协议。改错最直接的后果是 Python 抛错（未知 `req_type` 或字段不匹配），在 Rust 端经 `pyerr_to_status` 变成 `INVALID_ARGUMENT` 或 `INTERNAL`。**结论：这是被 proto 注释和 `build_classify_dict` 文档共同约束的契约，不能随意改。**（待本地验证 Python 端的确切报错文案。）

**练习 2**：`classify` 的输入校验为什么放在 `build_classify_dict` 之前？

**参考答案**：因为 `build_classify_dict` 用 `if !...is_empty()` 守卫 `text` 和 `input_ids`，若两者都为空，构造出的字典里两者都没有，等于给 Python 发了一个「空请求」，错误会被推迟到 Python 侧、且语义模糊。在 Rust 端提前用 `Status::invalid_argument` 拦截，既快（不进 GIL、不建通道），又能给客户端一个明确的 `INVALID_ARGUMENT` 错误码，而不是含糊的 `INTERNAL`。

---

### 4.4 JSON 控制型一元 RPC：recv_json_response 与 list_models

#### 4.4.1 概念说明

并不是所有一元 RPC 都像 `text_embed` 那样有强类型 proto 响应。有一批「控制 / 查询」类 RPC（`FlushCache` / `GetLoad` / `PauseGeneration` / `ContinueGeneration` / `StartProfile` / `StopProfile` / `UpdateWeightsFromDisk`）的响应本质上是 Python 返回的一段 JSON。对它们，Rust 不为每个字段建 proto 类型，而是**直接把 JSON 字符串拿来，用 `serde_json` 解析出关心的几个字段，缺失就用默认值兜底**。

这里有两种「拿 JSON 字符串」的方式，本讲各看一个代表：

1. **通道型**：请求经 `submit_json` 走 mpsc 通道，用共用函数 `recv_json_response` 从终止 chunk 的 `json_bytes` 里取出 UTF-8 字符串。代表：`flush_cache`。
2. **同步直调型**：不走通道，直接在 `spawn_blocking` 里调 bridge 的同步方法（如 `bridge.list_models()`），拿到 Python 返回的 JSON 字符串。代表：`list_models`、`get_model_info`。

两种方式的**收尾不同**（一个走收银台+取消语义，一个是普通同步调用），但**JSON 解析手法相同**：`serde_json::from_str` + `.as_str()/.as_bool()/.as_i64()` + `unwrap_or(...)`。

#### 4.4.2 核心流程

通道型（以 `flush_cache` 为例）：

```
flush_cache ──submit_flush_cache(rid)──▶ mpsc receiver
                                              │ recv_json_response
                                              │   └ recv_terminal_chunk_for_request
                                              │       └ Finished(ResponseData{json_bytes})
                                              ▼
                                     String（UTF-8 解码自 json_bytes）
                                              │ serde_json::from_str
                                              ▼
                          { success: v["success"].as_bool().unwrap_or(false),
                            message: v["message"].as_str().unwrap_or("") }
```

同步直调型（以 `list_models` 为例）：

```
list_models ──spawn_blocking(bridge.list_models())──▶ String（JSON 数组）
                                                              │ serde_json::from_str::<Vec<Value>>
                                                              ▼
                              每个 Value ──map──▶ proto::ModelCard { id, root, parent, max_model_len }
```

#### 4.4.3 源码精读

共用收尾 `recv_json_response`（[src/server.rs:954-971](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L954-L971)）：内部复用 4.1 的收银台，成功后从 `json_bytes` 解出字符串：

```rust
async fn recv_json_response(
    bridge: &Arc<PyBridge>,
    rid: &str,
    mut receiver: Receiver<ResponseChunk>,
    response_timeout: Duration,
) -> Result<String, Status> {
    let chunk =
        recv_terminal_chunk_for_request(bridge, rid, &mut receiver, response_timeout).await?;

    match chunk {
        ResponseChunk::Data(data) | ResponseChunk::Finished(data) => {
            let bytes = data.json_bytes.unwrap_or_default();
            String::from_utf8(bytes)
                .map_err(|e| Status::internal(format!("Invalid UTF-8 in response: {}", e)))
        }
        ResponseChunk::Error(msg) => Err(Status::internal(msg)),
    }
}
```

`flush_cache` 的典型用法（[src/server.rs:676-694](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L676-L694)）：拿到字符串后 `serde_json::from_str`，再用 `.as_bool().unwrap_or(false)` / `.as_str().unwrap_or("")` 兜底：

```rust
let json_str = recv_json_response(&self.bridge, &rid, receiver, self.response_timeout).await?;
let v: serde_json::Value = serde_json::from_str(&json_str)
    .map_err(|e| Status::internal(format!("Failed to parse JSON response: {}", e)))?;
Ok(Response::new(proto::FlushCacheResponse {
    success: v["success"].as_bool().unwrap_or(false),
    message: v["message"].as_str().unwrap_or("").to_string(),
}))
```

同步直调型 `list_models`（[src/server.rs:607-636](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L607-L636)）：注意它**没有**用 `recv_json_response`，而是直接调 `bridge.list_models()`（[src/bridge.rs:308-313](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L308-L313)），后者同步调用 Python `runtime_handle.list_models()` 并 `extract::<String>`：

```rust
let json_str = tokio::task::spawn_blocking({
    let bridge = self.bridge.clone();
    move || bridge.list_models()
}).await
.map_err(|e| Status::internal(format!("Task join error: {}", e)))?
.map_err(|e| pyerr_to_status(e, "Failed to list models"))?;

let models_arr: Vec<serde_json::Value> = serde_json::from_str(&json_str)
    .map_err(|e| Status::internal(format!("Failed to parse models JSON: {}", e)))?;

let models = models_arr.iter().map(|m| proto::ModelCard {
    id: m["id"].as_str().unwrap_or("").to_string(),
    root: m["root"].as_str().unwrap_or("").to_string(),
    parent: m.get("parent").and_then(|v| v.as_str()).map(String::from),
    max_model_len: m.get("max_model_len").and_then(|v| v.as_i64()).map(|n| n as i32),
}).collect();

Ok(Response::new(proto::ListModelsResponse { models }))
```

`ModelCard` 的 proto 定义（[proto/sglang/runtime/v1/sglang.proto:228-233](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/proto/sglang/runtime/v1/sglang.proto#L228-L233)）：

```proto
message ModelCard {
  string id = 1;
  string root = 2;
  optional string parent = 3;
  optional int32 max_model_len = 4;
}
```

> **关键细节**：`id` / `root` 用 `m["id"]` 索引 + `unwrap_or("")`，永远得到一个字符串（对应 proto3 普通字段，缺省即空串）；`parent` / `max_model_len` 用 `m.get(...).and_then(...).map(...)`，缺失或类型不符时得到 `None`（对应 proto3 `optional` 字段，能区分「没传」与「传了空/0」）。**JSON 取值策略与 proto 字段的可选性严格对应**，这是本节最重要的模式。

#### 4.4.4 代码实践

**实践目标**：画出 `list_models` 从 JSON 数组到 `proto::ModelCard` 的字段映射表，并标注每个字段的兜底行为。

**操作步骤**：

1. 打开 [src/server.rs:622-633](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L622-L633)。
2. 对照 proto [proto/sglang/runtime/v1/sglang.proto:228-233](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/proto/sglang/runtime/v1/sglang.proto#L228-L233)，逐字段填表。
3. 用一段示例 JSON（如下）手工推演每个字段的取值。

示例输入（Python 返回的 JSON 数组，**示例数据**）：

```json
[
  {"id": "Qwen2.5-Embed", "root": "/models/qwen", "max_model_len": 32768},
  {"id": "BGE-rerank", "root": "/models/bge", "parent": "BGE-base", "max_model_len": 512}
]
```

**需要观察的现象**：第一条缺 `parent`；第二条四个字段齐全；注意 `max_model_len` 是数字、`parent` 是字符串。

**预期结果**——字段映射表：

| proto 字段 | 类型 | 取值代码 | 第一条结果 | 第二条结果 | 缺失/类型不符时的兜底 |
| --- | --- | --- | --- | --- | --- |
| `id` | `string` | `m["id"].as_str().unwrap_or("")` | `"Qwen2.5-Embed"` | `"BGE-rerank"` | 空串 `""` |
| `root` | `string` | `m["root"].as_str().unwrap_or("")` | `"/models/qwen"` | `"/models/bge"` | 空串 `""` |
| `parent` | `optional string` | `m.get("parent").and_then(\|v\| v.as_str()).map(String::from)` | `None`（字段缺失） | `Some("BGE-base")` | `None` |
| `max_model_len` | `optional int32` | `m.get("max_model_len").and_then(\|v\| v.as_i64()).map(\|n\| n as i32)` | `Some(32768)` | `Some(512)` | `None` |

推演结论：第一条的 `parent` 为 `None`（proto `optional` 字段不出现）；`id`/`root` 即便缺失也只是空串而非 `None`。这正解释了「必填字段用 `unwrap_or`，`optional` 字段用 `.get().and_then()`」的对应关系。

> 待本地验证：实际运行 `list_models` 时 Python 返回的 JSON 字段名是否与上表一致（字段名取决于 Python `runtime_handle.list_models()` 的实现，本讲只保证 Rust 端解析逻辑准确）。

#### 4.4.5 小练习与答案

**练习 1**：`list_models` 解析失败（`serde_json::from_str` 报错）时返回什么？为什么不像 `text_embed` 那样依赖 `recv_terminal_chunk_for_request`？

**参考答案**：返回 `Status::internal(format!("Failed to parse models JSON: {}", e))`。它不依赖收银台，是因为数据来源不同——`list_models` 走的是同步直调 `bridge.list_models()`（不建 mpsc 通道、没有 ChunkCallback、没有取消语义），收银台是「通道型」收尾专用。两者只是恰好都用 `serde_json` 解析 JSON 而已。

**练习 2**：`recv_json_response` 为什么要对 `json_bytes` 做 `String::from_utf8`？失败映射成什么状态码？

**参考答案**：`json_bytes` 是 `Vec<u8>`（来自 `JsonChunkCallback` 推送的字节），而后续 `serde_json::from_str` 需要 `&str`。如果 Python 那边推回来的字节不是合法 UTF-8，`String::from_utf8` 会失败，映射为 `Status::internal(format!("Invalid UTF-8 in response: {}", e))`，避免把非法字节喂给 JSON 解析器产生更含糊的报错。

**练习 3**：`flush_cache` 的 `success` 字段用 `unwrap_or(false)`，而 `message` 用 `unwrap_or("")`。如果 Python 返回的 JSON 里 `success` 是字符串 `"true"`，会发生什么？

**参考答案**：`v["success"].as_bool()` 对字符串 `"true"` 返回 `None`（`as_bool` 只认 JSON 的 `true`/`false` 字面量），于是 `unwrap_or(false)` 兜底为 `false`——即「看起来成功，但被误判为失败」。这正是「松散解析 + 默认值兜底」的代价：它对类型不匹配是「静默降级」而非报错。生产中应保证 Python 端返回正确类型。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个「新增一个最简一元 RPC」的设计练习（**纸面设计，不改源码**）。

假设要新增一元 RPC `GetVocabSize`：输入空，输出一个 `int32 vocab_size`。请按下列步骤产出设计稿：

1. **proto 契约**：在 `proto/sglang/runtime/v1/sglang.proto` 的 `service SglangService` 里加一行 `rpc GetVocabSize(GetVocabSizeRequest) returns (GetVocabSizeResponse);`（一元，无 `stream`），并定义两个消息。参考现有 [proto/sglang/runtime/v1/sglang.proto:220-226](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/proto/sglang/runtime/v1/sglang.proto#L220-L226) 的 `ListModels` 写法。
2. **选择数据来源**：词汇表大小是一次性查询、无需取消语义，应仿 `list_models` 走「同步直调 + `spawn_blocking`」还是仿 `flush_cache` 走「通道 + `recv_json_response`」？写出你的选择和理由。（提示：参考 4.4.1 两种方式的区别。）
3. **JSON 解析与兜底**：假设 Python 返回 `{"vocab_size": 151643}`，写出 Rust 端把 `vocab_size` 解析出来并兜底为 `0` 的那一行代码（仿 [src/server.rs:628-631](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L628-L631) 的 `max_model_len` 写法）。
4. **收尾**：说明你这个方法**不需要** `recv_terminal_chunk_for_request`（若选同步直调）或**需要**它（若选通道型），并指出对应的风险（同步直调无法在客户端断开时取消；通道型更重但语义完整）。

> 参考方向：词汇表大小是纯本地查询（可由 `bridge.context_len()` 同量级的同步方法取得），最自然的是同步直调，仿 `list_models`。若未来需要支持「查询期间可取消」，再迁移到通道型 + `recv_json_response`。本练习重在判断「哪种一元收尾模式更合适」，而非写出完整代码。

---

## 6. 本讲小结

- **一元终止 chunk 契约**：一元 RPC 只期望 `Finished`（或 `Error`）一个终止 chunk；中途收到 `Data` 会被 `recv_terminal_chunk_for_request` 当作协议违例，转成 `INTERNAL` 并 abort。
- **收银台复用**：`recv_terminal_chunk_for_request` 是 `text_embed` / `embed` / `classify` / `openai_unary_rpc` 共用的收尾，统一处理超时、通道关闭、abort 传播；调用方 `match` 里的 `Data` 分支因此是死代码（仅满足穷尽性）。
- **结构化三段式**：`text_embed` / `embed` = 构造字典 → `submit_request("embed")` → 收终止 chunk → 取 `embedding` + `meta_info`；`embedding` 用 `unwrap_or_default()` 兜底。
- **classify 复用 embed 管线**：`classify` 传 `req_type="embed"`、用 `build_classify_dict`（条件写 `text` 或 `input_ids`），因为 Python 侧分类/奖励模型与 embedding 共用 `EmbeddingReqInput` 管线，proto 注释与字典构造函数文档共同确认这一点。
- **JSON 一元两型**：通道型（`recv_json_response`，从终止 chunk 的 `json_bytes` 取 UTF-8 串）用于 `flush_cache` 等控制 RPC；同步直调型（`bridge.list_models()`）用于 `list_models` 等纯查询。两者都用 `serde_json` + 默认值兜底。
- **取值策略 ↔ proto 可选性**：必填字段用 `m["k"].as_x().unwrap_or(默认)`，`optional` 字段用 `m.get("k").and_then(...).map(...)`，使「缺失」能映射到 `None`——`ModelCard` 的四字段正是这一对应关系的范本。

---

## 7. 下一步学习建议

- **下一讲 u2-l5（PyBridge 与请求通道架构）**：本讲反复出现的 `submit_request` / `submit_json` / `create_channel` 都定义在 `bridge.rs`。读完本讲你已知道「怎么用」它们，下一讲讲「它们内部如何用 `Mutex<BridgeState>` 管理 rid→通道映射、重复 rid 如何报错、Python 调用失败如何清理」。
- **u2-l6（回调机制）**：本讲里 `ResponseData.embedding` / `json_bytes` 的数据是从哪来的？答案在 `ChunkCallback` / `JsonChunkCallback` 的 `__call__`——Python 通过它把 chunk 推回 Rust。想彻底打通「Python→Rust」这一段，就去读 u2-l6。
- **u3-l1（背压与 pending-send 停泊）**：本讲的 `recv_terminal_chunk_for_request` 只管「收」，而通道满时「发」端的背压（`try_send_chunk`、`register_pending_send`、`ChannelFull` 终止错误）是另一个维度。`ChannelFull` 正是本讲 4.1 里 `closed_stream_status` 可能取到的 `TerminalError` 之一，它的来龙去脉在 u3-l1。
- **u3-l3（错误映射）**：本讲顺带提到的 `pyerr_to_status`、`terminal_error_status`、`closed_stream_status` 的完整映射表（`PyValueError`→`INVALID_ARGUMENT`、`ChannelFull`→`RESOURCE_EXHAUSTED`、`Aborted`→`CANCELLED`、超时→`DEADLINE_EXCEEDED`）在 u3-l3 系统讲解。
