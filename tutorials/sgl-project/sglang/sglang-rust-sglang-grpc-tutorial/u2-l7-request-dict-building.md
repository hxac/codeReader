# 请求字典构建：proto 消息到 Python dict

## 1. 本讲目标

在 u2-l5 里，我们看清了 `PyBridge::submit_request` 的「三段式」骨架：建通道 → 跨 GIL 调 Python `submit_request` → 失败回滚。当时为了聚焦账本与通道，刻意跳过了它吞进去的第二个参数 `req_dict`：

```rust
let py_req_dict = json_map_to_pydict(py, &req_dict)?;
// ...
kwargs.set_item("req_type", req_type)?;
kwargs.set_item("req_dict", py_req_dict)?;
kwargs.set_item("chunk_callback", callback)?;
self.runtime_handle.call_method(py, "submit_request", (), Some(&kwargs))?;
```

`req_dict` 是一个 `HashMap<String, serde_json::Value>`，最终以 Python `dict` 的身份交给 Python 端的推理引擎（匹配 `GenerateReqInput` / `EmbeddingReqInput`）。而 u2-l6 讲的是「**Python → Rust**」这一侧——回调把 Python 推回的数据翻译成 `ResponseChunk`。本讲补上另一半：**Rust → Python** 这一侧，proto 请求消息是怎么被翻译成那个 Python `dict` 的。

本讲要回答的核心问题是：**给定一个 proto `TextGenerateRequest`（或 `GenerateRequest` / `TextEmbedRequest` 等），Rust 如何把它逐字段拆开、拼成一个 Python 能直接当 `GenerateReqInput` 用的字典？`optional` 字段、`repeated` 字段、可选的整块参数（如 `DisaggregatedParams`）各用什么策略插入？中间为什么非要绕一层 `serde_json`？**

读完本讲，你应当能够：

- 画出「**proto 消息 → `serde_json` map → Python `dict`**」这条三段式管道，并说清为什么用 `serde_json` 当中间层（解耦 proto 类型与 Python、让构建函数纯 Rust 无 GIL、可单测）。
- 逐字段读懂 `sampling_params_to_map`：对 `optional` 标量用 `if let Some(v)`、对 `repeated` 字段用 `!x.is_empty()`、对 `optional string` 用 `if let Some(ref v)`，并能解释这三类条件判断各自的成因。
- 对比 `build_text_generate_dict` 与 `build_generate_dict`，列出共有字段与各自独有字段（`text` + `return_text_in_logprobs` vs `input_ids`），并说清 `received_time` 这类「服务端盖戳」字段的来源。
- 理解 `insert_disaggregated_params` 与 `trace_headers_to_json` 的「整块条件插入」策略：要么三把钥匙一起塞、要么一个不塞；trace 头非空才塞。
- 读懂 `json_map_to_pydict` / `json_value_to_py` 的递归转换，特别是 `Number` 分支如何区分 `i64` / `f64` / 兜底 `None`，并把它和 u2-l6 的 `py_value_to_json_string`（反方向）配成一对。

本讲与 u2-l4（一元 RPC）、u2-l5（桥接通道）是同一条数据链上相邻的三段：u2-l5 讲通道怎么开、u2-l6 讲数据怎么回流，本讲则讲请求怎么「出去」。三讲合起来，就拼出了 `submit_request` 一条请求的完整往返。

## 2. 前置知识

进入源码前，先建立四块直觉。它们是看懂「请求字典构建」的钥匙。

### 2.1 为什么是「三段式」而不是直接 proto → Python

最朴素的实现，是拿着 proto 消息、直接持 GIL 一个字段一个字段地 `py_dict.set_item(...)` 往 Python dict 里塞。本项目没有这么做，而是先在**纯 Rust、无 GIL** 的环境里把字段塞进一个 `HashMap<String, serde_json::Value>`，最后一刻再用一个通用的递归转换器 `json_map_to_pydict` 一次性转成 Python dict。

这样做有三个好处：

1. **构建函数不碰 GIL，纯逻辑、可单测。** `build_text_generate_dict` 只吃 `&proto::TextGenerateRequest`、吐 `HashMap`，输入输出都是普通 Rust 类型。所以 `request_utils.rs` 末尾能用 `#[test]` 直接断言「字典里有没有 `session_id`」「`bootstrap_host` 等于多少」，完全不用起 Python 解释器（见 [request_utils.rs:269-342](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/request_utils.rs#L269-L342)）。
2. **proto 类型被「挡」在 serde_json 这一层之外。** 上游（`server.rs`）只管把不同 proto 消息喂给各自的 `build_*_dict`，下游（`bridge.rs`）只认 `HashMap<String, serde_json::Value>` 这一个统一类型。新增一个 RPC 时，proto 的细节不会泄漏到桥接层。
3. **转换器只写一遍。** `json_value_to_py` 递归处理 `serde_json::Value` 的全部六种变体（Null/Bool/Number/String/Array/Object），所有 `build_*_dict` 共用这一个转换器，行为一致、维护点单一。

> 一句话记忆：**proto 是「合同」，serde_json map 是「草稿」，Python dict 是「成品」**。构建函数负责把合同誊抄成草稿，`json_map_to_pydict` 负责把草稿印成成品。

### 2.2 prost 把 proto 三种字段类型映射成什么

`build.rs` 用 `tonic_build` + `prost` 把 `.proto` 编译成 Rust。三类字段的 Rust 形态决定了后面 `if let` 怎么写，必须先理清：

- **`optional T field = n;`**（proto3 显式 optional，本项目用 `--experimental_allow_proto3_optional` 开启）→ Rust 里是 `Option<T>`。例如 `optional float temperature` → `temperature: Option<f32>`。判断「有没有设」用 `if let Some(v) = p.temperature`。
- **`repeated T field = n;`** → Rust 里是 `Vec<T>`，**永远存在**，默认空。例如 `repeated string stop` → `stop: Vec<String>`。prost 不给它包 `Option`，所以「没设」和「设成空」在 Rust 侧无法区分，判断只能用 `!p.stop.is_empty()`。
- **`map<string, string> field = n;`** → Rust 里是 `HashMap<String, String>`。例如 `map<string, string> trace_headers` → `trace_headers: HashMap<String, String>`。判空用 `is_empty()`。

以 `SamplingParams` 为例，它 15 个字段全是前两类（见 proto 定义 [`proto/sglang/runtime/v1/sglang.proto:51-68`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/proto/sglang/runtime/v1/sglang.proto#L51-L68)）：13 个 `optional` 标量 + 2 个 `repeated`（`stop`、`stop_token_ids`）。这正是 `sampling_params_to_map` 里两种 `if` 写法的来源。

### 2.3 `optional string` 与 `optional float` 在 `if let` 上的细微差别

这点容易绊倒初学者。看 `sampling_params_to_map`：

```rust
if let Some(v) = p.temperature {           // float：Copy 类型
    map.insert("temperature".into(), serde_json::json!(v));
}
// ...
if let Some(ref v) = p.json_schema {       // string：非 Copy
    map.insert("json_schema".into(), serde_json::json!(v));
}
```

`p` 是 `&proto::SamplingParams`（一个引用）。`p.temperature` 是 `Option<f32>`，`f32` 是 `Copy` 类型，`if let Some(v)` 会**拷贝**出那个 `f32`，不移动任何东西，没问题。但 `p.json_schema` 是 `Option<String>`，`String` 不是 `Copy`，若直接 `if let Some(v)` 就会试图**移动** `String` 出一个借用的结构体——编译器拒绝。所以这里写 `if let Some(ref v)`，`v` 是 `&String`，只借用不移动。（Rust 2021/2024 edition 的「默认绑定模式」其实能让你省略 `ref`，但本项目显式写了 `ref`，意图清晰，照抄即可。）

> 规则记法：**标量（数字/布尔）直接 `Some(v)`；字符串/消息要 `Some(ref v)` 或 `Some(v)` 配合所有权转移（当 `p` 是 owned 时）。**

### 2.4 这一对函数是「双向桥」的两个方向

`py_utils.rs` 里有两个转换函数，方向相反，恰好配成桥的两端：

| 函数 | 方向 | 用在哪 | 输入 → 输出 |
| --- | --- | --- | --- |
| `json_map_to_pydict` | **Rust → Python** | 构建请求字典（本讲） | `HashMap<String, serde_json::Value>` → Python `dict` |
| `py_value_to_json_string` | **Python → Rust** | 编码回调里的 `meta_info`（u2-l6） | Python 任意值 → JSON 字符串 |

请求要「出去」（Rust→Python），用前者；响应的 `meta_info` 要「回来」（Python→Rust），用后者。两者都住在 `py_utils.rs`，由 `utils/mod.rs` 统一再导出（[`src/utils/mod.rs:1-8`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/mod.rs#L1-L8)）。本讲聚焦前者，后者在 u2-l6 已展开。

## 3. 本讲源码地图

本讲围绕 `utils/` 子模块展开，并向上接到 `server.rs`（调用点）与 `bridge.rs`（消费点）：

| 文件 | 作用 |
| --- | --- |
| [`rust/sglang-grpc/src/utils/request_utils.rs`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/request_utils.rs#L1-L342) | 本讲主战场。5 个公开的 `build_*_dict`（111–267）、采样参数映射 `sampling_params_to_map`（6–59）、可选整块插入 `insert_disaggregated_params`（69–87）、trace 头 `trace_headers_to_json`（61–67）、时间戳 `now_timestamp`（89–94）、以及 `extract_model_path`（96–108，服务 `get_model_info`，非本讲重点）。末尾 `#[cfg(test)]`（269–342）是现成的单测范本。 |
| [`rust/sglang-grpc/src/utils/py_utils.rs`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/py_utils.rs#L1-L67) | 递归转换器 `json_value_to_py`（5–35）与其顶层封装 `json_map_to_pydict`（37–46）；以及反方向的 `py_value_to_json_string`（48–63，u2-l6 主角）。 |
| [`rust/sglang-grpc/src/utils/mod.rs`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/mod.rs#L1-L8) | 8 行的「再导出关卡」：把 `py_utils` / `request_utils` 里 `pub(crate)` 的符号汇集成 `crate::utils::` 下的统一入口。 |
| [`rust/sglang-grpc/src/server.rs`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L231) | 调用点：`text_generate`（231）、`generate`（300）、`text_embed`（369）、`embed`（404）、`classify`（446）各自调对应的 `build_*_dict`。 |
| [`rust/sglang-grpc/src/bridge.rs`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L177-L207) | 消费点：`submit_request` 在第 187 行 `json_map_to_pydict(py, &req_dict)?` 把草稿印成 Python dict。 |
| [`proto/sglang/runtime/v1/sglang.proto`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/proto/sglang/runtime/v1/sglang.proto#L45-L111) | 契约：`SamplingParams`（51–68）、`DisaggregatedParams`（45–49）、`TextGenerateRequest`（72–87）、`GenerateRequest`（97–111）。 |

> 可见性提示：`utils` 模块在 `lib.rs` 里声明为 `pub(crate) mod utils`（crate 私有），所以这些 `build_*_dict` / `json_map_to_pydict` 都标 `pub(crate)`——它们只服务于本 crate 内部的请求翻译，不对外（连 Python 都看不到，Python 看到的是翻译好的 dict）。

## 4. 核心概念与源码讲解

### 4.1 三段式管道：proto → serde_json map → PyDict

#### 4.1.1 概念说明

这是本讲的总纲。一条请求从 tonic handler 进来，到被 Python 引擎接收，要穿过三个「形相」：

1. **proto 消息**：强类型 Rust 结构体（如 `proto::TextGenerateRequest`），字段类型由 `.proto` 决定，`optional` 是 `Option<T>`、`repeated` 是 `Vec<T>`。
2. **serde_json map**：一个 `HashMap<String, serde_json::Value>`，键是字符串、值是动态类型（`Value` 的六变体之一）。这是「类型抹平」后的草稿。
3. **Python dict**：PyO3 的 `Bound<PyDict>`，键值都是 Python 对象。这是交给 `runtime_handle.submit_request` 的成品。

中间这层 serde_json 不是多余的——它是「翻译台」。proto 那侧每种请求结构体长得不一样（`TextGenerateRequest` 有 `text`、`GenerateRequest` 有 `input_ids`），但抹平成 `HashMap<String, serde_json::Value>` 后，下游只剩一个统一类型，于是 `json_map_to_pydict` 这一个递归函数就能处理所有请求。

#### 4.1.2 核心流程

以 `text_generate` 为例的端到端时序：

```text
proto::TextGenerateRequest  (server.rs:226  req = request.into_inner())
            │
            │  build_text_generate_dict(&rid, &req)        ← 纯 Rust，无 GIL
            ▼
HashMap<String, serde_json::Value>  (request_utils.rs:111)
            │
            │  bridge.submit_request(&rid, "generate", req_dict)
            ▼
   json_map_to_pydict(py, &req_dict)  (bridge.rs:187)      ← 持 GIL
            │
            ▼
        Python dict  →  runtime_handle.submit_request(req_type=, req_dict=, chunk_callback=)
```

关键点：`build_*_dict` 全程**不需要 GIL**（它只摆弄 Rust 类型），所以可以在 tonic 的 async handler 里随意调用；真正持 GIL 的只有最后一步 `json_map_to_pydict`，它发生在 `submit_request` 的 `Python::with_gil(|py| ...)` 闭包内（[bridge.rs:186-198](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L186-L198)）。

#### 4.1.3 源码精读

调用点（server.rs，建立 rid 后立即构建字典）：

[`rust/sglang-grpc/src/server.rs:226-236`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L226-L236) —— `text_generate`：取出 proto 消息、用 `uuid` 兜底生成 `rid`、调 `build_text_generate_dict` 得到 `req_dict`，再交给 `bridge.submit_request`。

消费点（bridge.rs，把草稿印成成品）：

[`rust/sglang-grpc/src/bridge.rs:186-198`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L186-L198) —— `submit_request` 闭包内：`json_map_to_pydict(py, &req_dict)?` 把 HashMap 转成 `py_req_dict`，连同 `req_type`、`chunk_callback` 一起塞进 kwargs，调 `runtime_handle.call_method(py, "submit_request", ...)`。

注意 `req_dict` 是按值传入 `submit_request`（`req_dict: HashMap<...>`，[bridge.rs:181](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L181)），所有权一路交到 `json_map_to_pydict`，用完即弃——这条 dict 是一次性的，不缓存。

#### 4.1.4 代码实践

**实践目标**：亲眼看到三段式管道在测试里跑通，确认 proto 默认值 → 字典键的映射。

**操作步骤**：

1. 在 crate 根目录（`rust/sglang-grpc/`）运行现成的单测：
   ```bash
   cargo test --lib utils::request_utils::tests
   ```
2. 阅读三个测试（[request_utils.rs:273-341](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/request_utils.rs#L273-L341)），观察它们如何**只用 Rust 类型**（`proto::TextGenerateRequest { ..Default::default() }`）造请求、再断言字典内容——全程无 Python。

**需要观察的现象**：

- 测试能编译通过并全绿。这说明 `build_*_dict` 确实是纯 Rust、无 GIL 的，否则它进不了普通 `#[test]`（那需要嵌入 Python 解释器）。
- `generate_dicts_include_session_id` 里，proto 的 `session_id: Some("session-1")` 在字典里变成了 `serde_json::json!("session-1")`——这正是「proto 的 `Option<String>` → serde_json 的 `Value::String`」一步。

**预期结果**：3 个测试通过；你能在断言里直接看到「proto 字段值 → `serde_json::Value`」的对应。若本地无 Rust 工具链或编译耗时，此结果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `json_map_to_pydict` 删掉、改成在 `build_text_generate_dict` 内部直接持 GIL 调 `py_dict.set_item`，会破坏哪条性质？

**答案**：会破坏「构建函数纯 Rust、无 GIL、可单测」。`build_*_dict` 一旦依赖 `Python<'py>`，就无法用普通 `#[test]` 直接断言字典内容（要么得起嵌入式 Python，要么改成集成测试），而且 tonic async handler 里随意持 GIL 也更易和 `spawn_blocking` 模式冲突。serde_json 中间层正是为了把「翻译逻辑」和「Python 互操作」切开。

**练习 2**：`req_dict` 的所有权在 `submit_request` 里是怎么流动的？

**答案**：按值传入（`req_dict: HashMap<...>`），所有权交给 `submit_request`；在闭包里被 `json_map_to_pydict(&req_dict)`（借用）读取转换成 Python dict 后，原始 HashMap 在 `submit_request` 返回时被 drop。这条 dict 是一次性的请求草稿，不进入任何缓存。

---

### 4.2 `sampling_params_to_map`：optional / repeated 字段的映射范本

#### 4.2.1 概念说明

`SamplingParams` 是生成类请求（`text_generate` / `generate`）共用的采样参数子结构，15 个字段。它被 proto 定义成一个独立 message（[`proto:51-68`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/proto/sglang/runtime/v1/sglang.proto#L51-L68)），在 proto 消息里以 `optional SamplingParams sampling_params = 2` 的形式嵌套。`sampling_params_to_map` 把它（可能是 `None`）摊平成一个 `serde_json::Value`（一个 JSON 对象），作为请求字典里 `sampling_params` 这个键的值。

这个函数之所以是「范本」，是因为它**集中展示了三类字段的插入策略**，后面的 `build_*_dict` 只是反复套用同样套路。

#### 4.2.2 核心流程

```text
sampling_params_to_map(&Option<SamplingParams>)
  ├─ None  → 返回空对象 {}                     （整个采样参数缺省）
  └─ Some(p) → 新建空 Map，逐字段判断：
        optional 标量（temperature/top_p/...）  → if let Some(v) = p.x { insert }
        repeated（stop / stop_token_ids）       → if !p.x.is_empty() { insert }
        optional string（json_schema / regex）  → if let Some(ref v) = p.x { insert }
     最后包成 Value::Object(map) 返回
```

核心思想是「**只插入被显式设置的字段，缺省的不进字典**」。这样 Python 侧的 `SamplingParams` 能用自己的默认值填补——而不是被 Rust 塞进去的 `0` / `false` / `[]` 覆盖。这对 `temperature`（默认 1.0 而非 0）这类「默认值非零」的参数尤其重要：如果 Rust 无脑插入 `temperature=0`，Python 的默认 1.0 就被冲掉了。

#### 4.2.3 源码精读

[`rust/sglang-grpc/src/utils/request_utils.rs:6-59`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/request_utils.rs#L6-L59) —— `sampling_params_to_map` 全文。三类写法集中在此。

`repeated` 字段处理（`stop` / `stop_token_ids`）：

[`rust/sglang-grpc/src/utils/request_utils.rs:37-42`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/request_utils.rs#L37-L42) —— 用 `!p.stop.is_empty()` 判断。`p.stop` 是 `Vec<String>`（prost 把 `repeated string` 映射成 `Vec<String>`），永远存在、默认空。只有非空时才 `serde_json::json!(p.stop)`（把整个 Vec 序列化成 JSON 数组）插入。空 Vec 不插入，让 Python 用默认的空停止词表。

`optional string` 处理（`json_schema` / `regex`）：

[`rust/sglang-grpc/src/utils/request_utils.rs:49-54`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/request_utils.rs#L49-L54) —— 用 `if let Some(ref v)`。`v` 是 `&String`，`serde_json::json!(v)` 借用序列化，不移动。标量字段（如第 10–15 行的 `temperature`）则是 `if let Some(v)`，因为 `f32`/`i32`/`bool` 都是 `Copy`。

`None` 兜底：

[`rust/sglang-grpc/src/utils/request_utils.rs:57`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/request_utils.rs#L57) —— 当整个 `sampling_params` 字段是 `None`（请求没带采样参数）时，返回一个空 JSON 对象 `{}`，而非 `Null`。这样下游 `build_*_dict` 无条件把 `sampling_params` 键设成这个值即可，Python 拿到一个空 dict 自然全用默认。

#### 4.2.4 代码实践

**实践目标**：验证「`repeated` 字段空时不进字典」这条规则，亲手跑一次断言。

**操作步骤**：

1. 在 `request_utils.rs` 的 `#[cfg(test)] mod tests` 里（第 269 行之后）追加一个测试（**示例代码**，非项目原有）：
   ```rust
   #[test]
   fn sampling_params_omit_empty_repeated() {
       // 带 stop、不带 stop_token_ids
       let p = proto::SamplingParams {
           stop: vec!["\n".to_string()],
           ..Default::default()
       };
       let req = proto::TextGenerateRequest {
           sampling_params: Some(p),
           ..Default::default()
       };
       let d = build_text_generate_dict("r1", &req);
       let sp = d.get("sampling_params").and_then(|v| v.as_object()).unwrap();
       assert!(sp.contains_key("stop"));              // 非空 → 进字典
       assert!(!sp.contains_key("stop_token_ids"));   // 空 Vec → 不进字典
       assert!(!sp.contains_key("temperature"));      // None → 不进字典
   }
   ```
2. 运行 `cargo test --lib sampling_params_omit_empty_repeated`。

**需要观察的现象**：测试通过，证明 `stop`（非空）进了字典、`stop_token_ids`（空 Vec）和 `temperature`（None）都没进。

**预期结果**：3 个断言全过。**待本地验证**（若本地无 Rust 工具链）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `stop` 用 `!p.stop.is_empty()` 判断，而 `temperature` 用 `if let Some(v)`？

**答案**：因为 prost 对两类字段映射不同。`repeated string stop` → `stop: Vec<String>`，prost 不给它包 `Option`，永远是 `Vec`（默认空），所以只能用 `is_empty()` 区分「有没有内容」；而 `optional float temperature` → `temperature: Option<f32>`，有 `Option` 外壳，用 `if let Some(v)` 区分「设没设」。

**练习 2**：如果改成无条件 `map.insert("temperature", json!(p.temperature.unwrap_or(0.0)))`，会造成什么实际问题？

**答案**：会把「没设 temperature」错误地当成 `0.0` 塞进字典，覆盖掉 Python `SamplingParams` 默认的 `1.0`，导致用户没指定温度时反而以贪婪解码（temperature≈0）运行。「只插显式设置的字段」这条原则正是为了避免这种默认值污染。

---

### 4.3 `build_text_generate_dict` 与 `build_generate_dict`：两个生成请求字典

#### 4.3.1 概念说明

这两个函数分别服务 `text_generate`（文本进、文本出）和 `generate`（token id 进、token id 出）两个流式 RPC。它们是 `request_utils.rs` 里最长、也最具代表性的两个 builder，字段布局高度相似，只在「输入载体」和个别字段上有差异。

- `build_text_generate_dict`：吃 `proto::TextGenerateRequest`（带 `string text`），产出匹配 Python `GenerateReqInput` 的字典。
- `build_generate_dict`：吃 `proto::GenerateRequest`（带 `repeated int32 input_ids`），同样产出匹配 `GenerateReqInput` 的字典。

两者产出的字典**目标 Python 类型相同**（都是 `GenerateReqInput`），区别只在输入侧——一个是原始文本（Python 端再分词），一个是已经分好的 token id。

#### 4.3.2 核心流程

两个函数的骨架完全一样，可拆成五段：

```text
build_*_dict(rid, req):
  1. 必填核心字段：rid、输入载体（text 或 input_ids）、sampling_params
  2. 生成控制字段（带默认值兜底）：stream、return_logprob、top_logprobs_num、logprob_start_len
     （text 版多一个 return_text_in_logprobs）
  3. 可选字段（条件插入）：lora_path、routing_key、routed_dp_rank、session_id
  4. 可选整块：insert_disaggregated_params(...)、trace_headers_to_json(...)
  5. 服务端盖戳：received_time = now_timestamp()
```

「带默认值兜底」与「条件插入」的区别是本模块的重点：

- 第 2 段用 `req.x.unwrap_or(default)`——**无论 proto 设没设，这个键一定出现在字典里**，只是没设时用 Rust 给的默认值（如 `stream` 默认 `false`、`logprob_start_len` 默认 `-1`）。
- 第 3 段用 `if let Some(...)`——**只有 proto 显式设了，键才出现**；没设则整个键缺席，让 Python 用自己的默认。

这两种策略对应 proto 字段的两种语义：「有合理默认、永远要给」vs「可选增强、缺省即不启用」。

#### 4.3.3 源码精读

`build_text_generate_dict` 全文：

[`rust/sglang-grpc/src/utils/request_utils.rs:111-160`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/request_utils.rs#L111-L160) —— 注意第 117 行 `text`、第 138-141 行独有的 `return_text_in_logprobs`、第 158 行 `received_time`。

`build_generate_dict` 全文：

[`rust/sglang-grpc/src/utils/request_utils.rs:163-208`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/request_utils.rs#L163-L208) —— 注意第 169 行 `input_ids`，且**没有** `return_text_in_logprobs`（因为输入是 token id，logprobs 里没有「文本」可言）。

服务端盖戳字段：

[`rust/sglang-grpc/src/utils/request_utils.rs:89-94`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/request_utils.rs#L89-L94) —— `now_timestamp` 取 `SystemTime` 距 UNIX_EPOCH 的秒数（`f64`）。每个请求字典末尾都盖一个 `received_time`，供 Python 端统计排队/推理耗时。注意这是 wall-clock，不是单调时钟，仅用于时延统计而非排序基准。

#### 4.3.4 代码实践

**实践目标**：完成规格要求的对比——列出两个函数的共有字段与各自独有字段。

**操作步骤**：对照 [`request_utils.rs:111-160`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/request_utils.rs#L111-L160) 与 [`request_utils.rs:163-208`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/request_utils.rs#L163-L208) 逐行比对，填写下表。

**需要观察的现象 / 预期结果**——字段差异表：

| 类别 | 字段 | text_generate | generate | 说明 |
| --- | --- | --- | --- | --- |
| 输入载体 | `text` | ✅ 必填 | ❌ | text 版用原始文本 |
| 输入载体 | `input_ids` | ❌ | ✅ 必填 | token 版用已分词 id |
| 生成控制 | `return_text_in_logprobs` | ✅ `unwrap_or(false)` | ❌ | text 版独有：logprobs 里是否带文本 |
| 生成控制 | `stream` / `return_logprob` / `top_logprobs_num` / `logprob_start_len` | ✅ | ✅ | 共有，均 `unwrap_or` 兜底 |
| 采样 | `sampling_params` | ✅ | ✅ | 共有，调 `sampling_params_to_map` |
| 可选 | `lora_path` / `routing_key` / `routed_dp_rank` / `session_id` | ✅ 条件 | ✅ 条件 | 共有，`if let Some` 插入 |
| 可选整块 | `bootstrap_host/port/room`（disaggregated） | ✅ 条件 | ✅ 条件 | 共有，整块插入 |
| 可选整块 | `external_trace_header` | ✅ 条件 | ✅ 条件 | 共有，trace 非空才插 |
| 服务端 | `rid` / `received_time` | ✅ | ✅ | 共有 |

**核心差异**只有两处：输入载体（`text` ↔ `input_ids`），以及 text 版多一个 `return_text_in_logprobs`。其余完全对称——这正反映了两个 RPC 共享 `GenerateReqInput` 这条 Python 管线，只是输入形式不同。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `stream` 用 `unwrap_or(false)`（永远插键），而 `session_id` 用 `if let Some`（条件插键）？

**答案**：`stream` 是「永远有意义的开关」，Python `GenerateReqInput` 需要一个确定的布尔值，所以 Rust 给默认 `false`、保证键存在；`session_id` 是「可选的路由/复用标识」，不设时 Python 端应走「无 session」路径，硬塞一个空串或默认值反而可能误触会话逻辑，所以缺省时整键缺席。

**练习 2**：`logprob_start_len` 的默认值是 `-1`（[request_utils.rs:134-137](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/request_utils.rs#L134-L137)）。为什么用负数默认值而不是 `0`？

**答案**：`-1` 在 SGLang 里是「自动/全量」的约定哨兵值（从头开始算 logprob），而 `0` 是一个合法的具体起始位置。若默认成 `0`，会与「用户显式要从位置 0 开始」混淆；用 `-1` 能让 Python 端区分「没指定（自动）」与「指定从 0 开始」。

---

### 4.4 `insert_disaggregated_params` 与 `trace_headers_to_json`：可选整块的条件插入

#### 4.4.1 概念说明

请求字典里有两组字段不是「单字段条件插入」，而是「**整块条件插入**」——要么一组键一起出现，要么一个都不出现。它们对应 proto 里两个嵌套结构：

- **`DisaggregatedParams`**（ disaggregated / PD 分离部署的引导参数）：含 `bootstrap_host`、`bootstrap_port`、`bootstrap_room` 三个字段（[`proto:45-49`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/proto/sglang/runtime/v1/sglang.proto#L45-L49)）。这三者在语义上是一个整体（指向某个 KV 缓存引导房间），要么一起带、要么不带。
- **`trace_headers`**（链路追踪头）：proto 的 `map<string, string>`，整张表要么非空透传、要么不出现。

#### 4.4.2 核心流程

```text
insert_disaggregated_params(&mut request, &Option<DisaggregatedParams>):
  if let Some(params) = params:
      request["bootstrap_host"]  = params.bootstrap_host
      request["bootstrap_port"]  = params.bootstrap_port
      request["bootstrap_room"]  = params.bootstrap_room
  // else: 三个键都不插

trace_headers_to_json(&HashMap<String,String>) -> Option<Value>:
  if headers.is_empty():  None          → 调用方不插 external_trace_header
  else:                   Some(json)     → 调用方插 external_trace_header
```

注意两者实现风格不同：`insert_disaggregated_params` 直接 `&mut request` 原地修改（因为要插多个键）；`trace_headers_to_json` 返回 `Option<Value>`，由调用方决定插不插（因为只插一个键，用 `if let Some(trace) = ... { d.insert("external_trace_header", trace) }` 更简洁）。这是「插多键」与「插单键」两种场景的自然分野。

#### 4.4.3 源码精读

`insert_disaggregated_params`：

[`rust/sglang-grpc/src/utils/request_utils.rs:69-87`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/request_utils.rs#L69-L87) —— `if let Some(params)` 守卫，三把钥匙（`bootstrap_host`/`port`/`room`）一起 `insert`。注意键名是 `bootstrap_*`（与 proto 字段名相同），不是 `disaggregated_*`——Python `GenerateReqInput` 直接认这个名字。

`trace_headers_to_json`：

[`rust/sglang-grpc/src/utils/request_utils.rs:61-67`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/request_utils.rs#L61-L67) —— 空表返回 `None`、非空表 `serde_json::json!(headers)` 整张序列化。调用点（如 [request_utils.rs:155-157](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/request_utils.rs#L155-L157)）据此决定是否插 `external_trace_header`。

现成单测覆盖了「有/无 disaggregated」两种情形：

[`rust/sglang-grpc/src/utils/request_utils.rs:295-341`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/request_utils.rs#L295-L341) —— `generate_dicts_include_disaggregated_params`（带参 → 三键都在，连 `bootstrap_room = i64::MAX` 这种边界值都断言）与 `generate_dicts_omit_disaggregated_params_when_absent`（不带参 → 三键都 `!contains_key`）。

#### 4.4.4 代码实践

**实践目标**：确认「整块插入」的原子性——三把钥匙要么全在、要么全无。

**操作步骤**：阅读 [`request_utils.rs:330-341`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/request_utils.rs#L330-L341) 的 `generate_dicts_omit_disaggregated_params_when_absent`，看它如何用 `..Default::default()` 造一个「不带 disaggregated_params」的请求，再对 `bootstrap_host`/`bootstrap_port`/`bootstrap_room` 三个键分别 `assert!(!request.contains_key(...))`。

**需要观察的现象**：三个断言并列，缺一不可——这正反映了「整块」语义。若实现里漏插一个键（比如只插了 host 没插 port），这个测试不会发现，但 Python 端会因为 `bootstrap_port` 缺失而拿不到完整的引导地址。**观察重点**：思考如果将来 `DisaggregatedParams` 加了第 4 个字段，这个「整块插入」函数和它的测试各需要怎么同步改。

**预期结果**：默认请求（`Default::default()`）的字典里完全不含三个 `bootstrap_*` 键；带参请求则三键齐全且值正确（含 `i64::MAX` 边界）。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `insert_disaggregated_params` 接收 `&mut HashMap`，而 `trace_headers_to_json` 返回 `Option<Value>` 让调用方自己插？

**答案**：因为 disaggregated 要插三个键，封装成「原地改 map」能保证三键作为一个原子单元一起插（不会漏一个）；trace 头只插一个键（`external_trace_header`），返回 `Option` 让调用方用一行 `if let Some(...) = ... { d.insert(...) }` 更轻量，不必传 `&mut`。

**练习 2**：`bootstrap_room` 在测试里被设成 `i64::MAX`（[request_utils.rs:300](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/request_utils.rs#L300)）。这能说明什么？

**答案**：proto 里 `bootstrap_room` 是 `int64`（[proto:48](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/proto/sglang/runtime/v1/sglang.proto#L48)），prost 映射成 `i64`。测试用 `i64::MAX` 是在验证「无截断地穿透」——从 proto 的 `int64` 到 `serde_json::json!(...)` 再到 Python，这个大整数不应被悄悄降精度（`serde_json` 用 `as_i64` 能完整容纳，见下一模块 `json_value_to_py`）。

---

### 4.5 `json_map_to_pydict`：serde_json 到 Python dict 的递归桥

#### 4.5.1 概念说明

前面四个模块都在「造草稿」（`HashMap<String, serde_json::Value>`）。本模块讲最后一步——把草稿「印成成品」的通用转换器 `json_map_to_pydict`。它不关心字典里装的是 generate 请求还是 embed 请求，只认 `serde_json::Value` 这一种动态类型，递归地把它翻成对应的 Python 对象。

它是 `py_utils.rs` 里和 u2-l6 `py_value_to_json_string`（反方向）配对的那个函数，是「Rust → Python」方向的唯一通道。

#### 4.5.2 核心流程

```text
json_map_to_pydict(py, &HashMap<String, Value>) -> Bound<PyDict>:
  新建空 PyDict
  for (k, v) in map:
      py_dict.set_item(k, json_value_to_py(py, v)?)    ← 每个值递归
  返回 py_dict

json_value_to_py(py, &Value) -> PyObject:     ← 六变体递归
  Null    → py.None()
  Bool(b) → Python bool
  Number  → as_i64()? i64 : (as_f64()? f64 : None)    ← 整数/浮点/兜底
  String  → Python str
  Array   → 递归每个元素 → PyList
  Object  → 递归每个值 → PyDict
```

`json_map_to_pydict` 只负责**顶层**那层（一个 `HashMap` → 一个 `PyDict`），真正的脏活在 `json_value_to_py`——它对 `serde_json::Value` 的六种变体各写一个分支，遇到嵌套就递归。

#### 4.5.3 源码精读

递归核心 `json_value_to_py`：

[`rust/sglang-grpc/src/utils/py_utils.rs:5-35`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/py_utils.rs#L5-L35) —— 六变体逐一处理。重点看 `Number` 分支（第 9-17 行）：先 `n.as_i64()`，能容纳就当 Python int；否则 `n.as_f64()` 当 Python float；都失败（理论上只有 NaN/Infinity 这类 serde_json 默认不产生的值）兜底 `py.None()`。

顶层封装 `json_map_to_pydict`：

[`rust/sglang-grpc/src/utils/py_utils.rs:37-46`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/py_utils.rs#L37-L46) —— 新建 `PyDict`、遍历 `HashMap`、每个值经 `json_value_to_py` 转换后 `set_item`。返回 `Bound<'py, PyDict>`，正是 `submit_request` 闭包里 `kwargs.set_item("req_dict", py_req_dict)?` 所需的形态。

为什么这能正确处理前面造的字典？举两个对应例子：

- `serde_json::json!(req.input_ids)` 把 `Vec<i32>` 序列化成 `Value::Array([Value::Number(...)])`，`json_value_to_py` 递归成 Python `list[int]`——正确。
- `sampling_params_to_map` 产出的 `Value::Object`，递归成 Python `dict`，作为请求字典里 `sampling_params` 键的嵌套 dict——正确。

#### 4.5.4 代码实践

**实践目标**：追踪一个具体值从 proto 字段到 Python 对象的完整转换路径，理解 `Number` 分支的「先 int 后 float」逻辑。

**操作步骤**：

1. 追踪 `sampling_params` 里 `temperature: Some(0.7)`（proto `optional float`）的旅程：
   - proto `f32` 0.7 → `sampling_params_to_map` 里 `serde_json::json!(v)` → `Value::Number(0.7)`（serde_json 会把它存成浮点 Number）。
   - → `json_value_to_py` 的 `Number` 分支：`n.as_i64()` 返回 `None`（0.7 不是整数）→ 走 `n.as_f64()` 得 `0.7` → Python `float`。
2. 追踪 `bootstrap_room: i64::MAX`（上一模块提到的边界值）：
   - → `serde_json::json!(i64::MAX)` → `Value::Number(i64::MAX)`。
   - → `json_value_to_py`：`n.as_i64()` 成功 → Python `int`，**无精度损失**。
3. 思考：如果某个值是 `1e400`（超过 f64 范围的「数」），`as_i64()` 和 `as_f64()` 都返回 `None`，会落到兜底 `py.None()`——但 `serde_json` 默认配置下根本不会产生这种值，所以这条兜底几乎是死代码（防御性编程）。

**需要观察的现象**：整数走 `as_i64` 分支成 Python `int`、浮点走 `as_f64` 分支成 Python `float`，类型不串。`bootstrap_room` 这种 `i64::MAX` 不会被降级成 float。

**预期结果**：纯源码阅读型实践，无需运行。结论是 `json_value_to_py` 的 `Number` 分支用「先 i64 后 f64 后 None」的三段式，保证整数不丢精度、浮点不被强转成 int。若要机器验证，可在 `py_utils/tests.rs` 里构造一个含 `Value::from(i64::MAX)` 的 map，`json_map_to_pydict` 后断言 Python 端 `== 9223372036854775807`（**待本地验证**）。

#### 4.5.5 小练习与答案

**练习 1**：`json_value_to_py` 为什么先试 `as_i64` 再试 `as_f64`，而不是反过来？

**答案**：因为「能当整数就当整数」能避免精度损失。`serde_json::Number` 对 `5` 这种值既能 `as_i64` 也能 `as_f64`；若先试 `as_f64`，`5` 会变成 Python `float` 5.0，而下游 SGLang 期望 `max_new_tokens` 这类是 `int`。先试 `as_i64` 保证整数语义的字段（token 数、id）以 Python `int` 送达。

**练习 2**：`json_map_to_pydict` 和 u2-l6 的 `py_value_to_json_string` 都在 `py_utils.rs`，它们的角色如何配对？

**答案**：二者是双向桥的两端。`json_map_to_pydict`（Rust→Python）把请求字典翻成 Python dict，用于**发请求**；`py_value_to_json_string`（Python→Rust）把回调里的任意 Python 值编码成 JSON 字符串，用于**收响应的 meta_info**。一个出、一个进，正好覆盖 `submit_request` 的完整往返。两者都由 [`utils/mod.rs:4`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/mod.rs#L4) 统一再导出为 `crate::utils::`。

---

## 5. 综合实践

把本讲的知识串起来，完成一个「**新增一个最简一元 RPC 的请求字典**」的纸上设计任务。假设我们要给 SGLang 加一个 `text_count` RPC：输入一段文本，输出 token 计数（仅做字典构建，不实现推理）。

**任务**：

1. **proto 侧**：在 `sglang.proto` 新增一个 message，至少包含 `string text = 1`、`optional string rid = 2`、`optional string routing_key = 3`、`map<string,string> trace_headers = 4`（可参照 `TextEmbedRequest` 的字段集，[`proto:121-126`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/proto/sglang/runtime/v1/sglang.proto#L121-L126)）。
2. **Rust 侧**：在 `request_utils.rs` 新增 `build_text_count_dict(rid, &proto::TextCountRequest) -> HashMap<String, serde_json::Value>`。要求：
   - 必填 `rid`、`text`。
   - 条件插入 `routing_key`（`if let Some`）。
   - 用 `trace_headers_to_json` 条件插入 `external_trace_header`。
   - 末尾盖 `received_time` 戳。
3. **测试侧**：仿照 [`request_utils.rs:273-293`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/request_utils.rs#L273-L293) 的风格写一个单测，断言 `routing_key` 被正确写入、空 `trace_headers` 时字典**不含** `external_trace_header` 键。
4. **接线侧**（口述）：说明在 `utils/mod.rs`（[`mod.rs:5-8`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/mod.rs#L5-L8)）和 `server.rs` 的 trait 实现里各需要加一行什么，才能让这个新 RPC 走通「proto → dict → `submit_request`」管道（提示：参照 `text_embed` 的 [`server.rs:369`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L369)）。

**预期成果**：

- 一段 `build_text_count_dict` 的 Rust 代码（结构应与 [`build_text_embed_dict` request_utils.rs:211-226](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/request_utils.rs#L211-L226) 几乎同构）。
- 一个能 `cargo test` 通过的单测。
- 一句关于 `mod.rs` 加 `pub(crate) use ...::build_text_count_dict`、`server.rs` 加 `let req_dict = build_text_count_dict(&rid, &req);` 的说明。

这个任务把「三段式管道」「optional/repeated 条件插入」「整块条件插入」「服务端盖戳」「可单测」「接线点」六个知识点全部用上。**整体待本地验证**（需要能编译 crate 的 Rust 工具链）。

## 6. 本讲小结

- 请求字典构建走 **三段式管道**：proto 消息 → `HashMap<String, serde_json::Value>`（纯 Rust、无 GIL、可单测）→ Python `dict`（`json_map_to_pydict` 持 GIL 印成品）。serde_json 中间层把 proto 类型细节挡在桥接层之外。
- `sampling_params_to_map` 是字段插入的**三策略范本**：`optional` 标量用 `if let Some(v)`、`repeated` 用 `!x.is_empty()`、`optional string` 用 `if let Some(ref v)`；核心原则是「只插显式设置的字段」，避免污染 Python 默认值。
- `build_text_generate_dict` 与 `build_generate_dict` **高度同构**：差异仅在输入载体（`text` ↔ `input_ids`）与 text 版独有的 `return_text_in_logprobs`；共有字段里，「永远插键带 `unwrap_or` 兜底」与「`if let Some` 条件插键」两种策略对应两种 proto 语义。
- `insert_disaggregated_params`（三键整块）与 `trace_headers_to_json`（单键 `Option`）展示「整块条件插入」的两种实现形态，分别对应插多键与插单键。
- `json_map_to_pydict` / `json_value_to_py` 是 Rust→Python 的**唯一通用通道**，`Number` 分支「先 `i64` 后 `f64` 后 `None`」保证整数不丢精度；它与 u2-l6 的 `py_value_to_json_string`（反方向）配成桥的两端。
- 所有 `build_*_dict` 与转换器都是 `pub(crate)`、经 `utils/mod.rs` 再导出；Python 永远只看到翻译好的成品 dict，看不到这些函数。

## 7. 下一步学习建议

本讲讲完了请求「出去」的方向。接下来可以：

- **横向读完其余三个 builder**：`build_text_embed_dict` / `build_embed_dict` / `build_classify_dict`（[`request_utils.rs:211-267`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/request_utils.rs#L211-L267)）。它们比两个 generate 版本更精简（无 `sampling_params`、无 disaggregated），是巩固本讲套路的好练习——尤其 `build_classify_dict` 同时接受 `text` 和 `input_ids` 且都用 `!x.is_empty()` 判断，值得对照。
- **进入 u2-l8（原生分词器）**：理解为什么 `text_generate` 带的是 `text` 而 `generate` 带的是 `input_ids`——后者正是前者经过 Rust 原生分词器（或 Python 回退）分词后的产物，u2-l8 讲这条分词链。
- **回头对照 u2-l5 / u2-l6**：现在再看 `submit_request`（[bridge.rs:177-207](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L177-L207)）的三段式，应能一眼看清「`req_dict`（本讲产物）→ `json_map_to_pydict` → Python dict → `chunk_callback`（u2-l6 主角）回流」的完整闭环。
- **专家层铺垫**：本讲所有函数都在请求**正常路径**上。后续 u3-l1（背压停泊）、u3-l2（中止传播）、u3-l3（错误映射）会讲这条路径上的异常分支——届时你会发现，请求字典构建是整条链路里最「干净」的一段，因为它完全在数据平面的 happy path 上。
