# 回调机制：ChunkCallback 与 JsonChunkCallback

## 1. 本讲目标

在 u2-l5 里，我们看清了 `PyBridge` 这本「共享账本」是怎么开页（`create_channel`）、怎么撤页（`remove_channel`）的。但当时特意留下了一个关键对象没展开——它就是在 `submit_request` 里被组装、再作为 `chunk_callback` 参数传给 Python 的那个东西：

```rust
let callback = self.make_chunk_callback(py, rid_owned)?;
// ...
kwargs.set_item("chunk_callback", callback)?;
self.runtime_handle.call_method(py, "submit_request", (), Some(&kwargs))?;
```

这个 `callback` 是一个 **PyO3 对象**（Python 能直接「叫得出名字」、能 `()` 调用的对象）。Python 端的推理引擎每产出一个结果片段（chunk），就会**短暂持 GIL** 调一下它：`callback(chunk, finished=True)`。于是数据就从 Python 世界「流」回了 Rust 世界，再经 u2-l5 那条 mpsc 通道流向 tonic handler。

本讲要回答的核心问题是：**这个回调对象内部长什么样？Python 推过来的 `dict`（或 `bytes`）是怎么被翻译成 Rust 的 `ResponseData`、进而决定发 `Data`/`Finished`/`Error` 三种 chunk 的？`meta_info` 里的每个值又为什么都要套一层 JSON 编码？**

读完本讲，你应当能够：

- 说清 `make_chunk_callback` / `make_json_callback` 这两个「回调工厂」各自造出什么对象、把哪些「能力」（账本 `Arc`、`runtime_handle`、`tokio_handle`、`rid`）塞进了对象里，以及为什么是两类回调分别服务两类 RPC。
- 逐行读懂 `ChunkCallback.__call__`：如何从 Python `dict` 里取出 `text`/`output_ids`/`embedding`/`meta_info`、用 `finished` 决定变体、最终交给 `try_send_chunk`。
- 逐行读懂 `JsonChunkCallback.__call__`：它和前者形状几乎一样，但只收 `bytes` 和一个 `status_code`，产出 `json_bytes`——并能列出两者的差异表。
- 解释 `extract_meta_info` 为什么对 `meta_info` 里**每一个值**（包括字符串）都调 `py_value_to_json_string` 做 JSON 编码，从而适配 proto 的 `map<string, string>`。
- 说清 `set_on_ready` / `clear_on_ready` 这对回调方法如何操作账本里的 `ready_callbacks` / `ready_signals` 两张表，以及为什么「晚注册也能补发信号」是必须的（背压的完整停泊流程留待 u3-l1）。

本讲承接 u2-l5 的「账本骨架」，聚焦 **Python → Rust** 这一侧的数据流细节；u2-l3 的流式消费、u3-l1 的背压停泊、u3-l2 的中止传播仍是各自独立的后续主题。

## 2. 前置知识

进入源码前，先建立四块直觉。它们是看懂两个回调的钥匙。

### 2.1 PyO3 的 `#[pyclass]` 与 `__call__`

PyO3 用两个宏把 Rust 类型变成 Python 可见、可调用的对象：

- `#[pyclass]` 标在一个 **Rust 结构体**上，表示「这个结构体可以被包装成一个 Python 对象」。Python 端拿到的就是这个包装后的对象，但看不到字段细节，只能调用你在 `#[pymethods]` 里暴露的方法。
- `#[pymethods]` 标在一个 `impl` 块上，块里的 `fn` 会变成 Python 对象的方法。其中特殊命名的 `fn __call__` 会让这个对象**变成可调用对象**——Python 里写 `obj(...)` 就等于调 `obj.__call__(...)`。

这正是回调的设计：Python 端不需要知道对象叫什么、有什么字段，只要能 `chunk_callback(chunk, finished=True)` 把数据「推」进来即可。Rust 在 `__call__` 里接住，翻译成内部类型。

> 本讲里的两个回调 `ChunkCallback`、`JsonChunkCallback` 都是 `#[pyclass]`（见 [bridge.rs:610](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L610) 与 [bridge.rs:698](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L698)），但**不导出**给 Python 模块顶层——它们只在 Rust 内部 `Py::new` 创建、作为参数传给 `RuntimeHandle` 的方法。Python 看到的是一个「从天而降的可调用对象」。

### 2.2 `ResponseData` 与 `ResponseChunk`：回调翻译的目标类型

回调收到 Python 数据后，最终的产物是 `ResponseChunk`（u2-l5 已介绍，这里复习结构）。它是一个三变体枚举：

```rust
pub enum ResponseChunk {
    Data(ResponseData),      // 中间 chunk，流还在继续
    Finished(ResponseData),  // 正常收尾的终端 chunk
    Error(String),           // 异常收尾的终端 chunk
}
```

其中 `Data` 和 `Finished` 都携带一份 `ResponseData`——也就是「一条 chunk 的实际载荷」：

[`rust/sglang-grpc/src/bridge.rs:26-33`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L26-L33) —— `ResponseData` 的 5 个字段：`text`、`output_ids`、`embedding`、`json_bytes`、`meta_info`。

本讲的全部工作，本质就是「把 Python 推来的东西填进这 5 个字段，再决定套上 `Data` 还是 `Finished` 的壳」。注意 5 个字段里前 4 个都是 `Option`（这一条 chunk 可能只有文本、只有 token id、只有 embedding，或只有一段 JSON 字节），而 `meta_info` 恒为 `HashMap<String, String>`（可能为空）。

### 2.3 两类回调对应两类 RPC

SGLang 的 25 个 RPC（u2-l1/u2-l2 已分类）在「Python 回推的数据形态」上分两类：

- **SGLang 原生数据型 RPC**（`generate` / `text_generate` / `embed` / `classify` 等）：Python 推回的是**结构化 `dict`**，字段有明确含义（`text` 是生成文本、`output_ids` 是 token 序列、`embedding` 是向量、`meta_info` 是元数据）。这类用 **`ChunkCallback`**（dict 回调）。
- **OpenAI 透传型 + 控制型 RPC**（`submit_openai` 那一族，以及 `flush_cache` / `get_load` / `start_profile` 等）：Python 回推的是**一坨不透明的 JSON 字节**（OpenAI 协议原样透传，或一段控制命令的 JSON 结果），Rust 不解析其内部结构，只透传字节。这类用 **`JsonChunkCallback`**（bytes 回调）。

> 入口对应关系：`submit_request`（[bridge.rs:177-207](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L177-L207)）调 `make_chunk_callback`；`submit_json`（[bridge.rs:315-335](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L315-L335)）调 `make_json_callback`，而 `submit_openai` 和所有 `submit_*` 控制方法都是 `submit_json` 的薄封装（详见 u2-l5）。

### 2.4 `try_send_chunk` 是回调的「出口」

两个回调的 `__call__` 最后一行都是同一个调用：

```rust
try_send_chunk(py, &self.rid, &self.state, &self.runtime_handle,
                &self.tokio_handle, &sender, msg)
```

它把翻译好的 `msg: ResponseChunk` 塞进通道（u2-l5 的 `channels[rid]` 对应的 `Sender`），并返回一个 `ChunkSendStatus`（`Ready`/`Pending`/`Closed`）告诉 Python「继续推 / 等一下 / 别推了」。本讲只把 `try_send_chunk` 当作「出口黑盒」用——它内部的背压停泊（通道满时 `spawn` 异步发送）是 u3-l1 的主题。回调的职责止于「造出正确的 `ResponseChunk` 并交给它」。

## 3. 本讲源码地图

本讲几乎全部围绕 `bridge.rs` 的回调区段（610–801）展开，外加 `py_utils.rs` 里一个被 `extract_meta_info` 复用的转换函数：

| 文件 | 作用 |
| --- | --- |
| [`rust/sglang-grpc/src/bridge.rs`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L147-L167) | 回调工厂 `make_chunk_callback` / `make_json_callback`（147–167）、两个 `#[pyclass]` 回调 `ChunkCallback`（610–695）与 `JsonChunkCallback`（698–783）、`meta_info` 提取函数 `extract_meta_info`（785–801），以及就绪信号注册辅助函数 `set_on_ready_for_rid` / `clear_on_ready_for_rid`（498–522）。 |
| [`rust/sglang-grpc/src/utils/py_utils.rs`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/py_utils.rs#L48-L67) | `py_value_to_json_string`（48–63）与兜底的 `json_encode_string`（65–67）：把任意 Python 值编码成一个 JSON 字符串，是 `meta_info` 适配 `map<string,string>` 的核心。 |

> 定位提示：`bridge.rs` 顶部的 `use crate::utils::{json_map_to_pydict, py_value_to_json_string};`（[bridge.rs:11](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L11)）已经把这个函数引进来了——`extract_meta_info` 直接调用它，无需重新导入。

## 4. 核心概念与源码讲解

### 4.1 回调工厂：make_chunk_callback 与 make_json_callback

#### 4.1.1 概念说明

回调对象不能凭空出现——它必须由 `PyBridge` 在**持 GIL** 的上下文里「现场捏一个」出来，因为创建 PyO3 对象（`Py::new`）需要 Python token。这件事由两个工厂方法负责：

- `make_chunk_callback(py, rid)` → 造一个 `ChunkCallback`（给 `submit_request` 用）。
- `make_json_callback(py, rid)` → 造一个 `JsonChunkCallback`（给 `submit_json` / `submit_openai` / 控制型 RPC 用）。

工厂的核心动作不是「造对象」本身，而是**把回调需要的四样「能力」打包进对象**：

| 能力 | 字段 | 用途 |
| --- | --- | --- |
| 请求身份证 | `rid: String` | 回调被调时，靠它知道自己代表哪个请求、该往 `channels[rid]` 推。 |
| 共享账本 | `state: BridgeStateRef`（账本的 `Arc`） | 取出 `channels[rid]` 的 `Sender`；操作 `ready_callbacks`/`ready_signals`；记 `terminal_errors`。 |
| Python 句柄 | `runtime_handle: PyObject` | 通道出问题时回调要反向调 Python 的 `abort`（见 `try_send_chunk` → `close_channel_with_error`，u3-l1/u3-l2）。 |
| Tokio 句柄 | `tokio_handle: Handle` | 通道满时 `spawn` 异步发送任务（背压，u3-l1）。 |

一句话：回调虽然是个「被动接收数据」的对象，但它手里握着反向操作账本和 Python 的全部钥匙，所以能独立完成「取通道、发 chunk、必要时 abort」的闭环，而不必再回头问 `PyBridge`。

#### 4.1.2 核心流程

```
make_chunk_callback(py, rid):                # 必须持 GIL 才能调
    callback = ChunkCallback {
        rid,                                   # 所有权搬入（String）
        state = self.state.clone(),            # 账本 Arc，clone 仅增引用计数
        runtime_handle = self.runtime_handle.clone_ref(py),  # Python 对象 clone_ref
        tokio_handle = self.tokio_handle.clone(),            # Handle clone 廉价
    }
    py_callback = Py::new(py, callback)?       # 包成 Python 对象（需 GIL）
    return py_callback.into_any()              # 类型擦除成 PyObject，便于塞进 kwargs
```

`make_json_callback` 与之**逐行同构**，只是把 `ChunkCallback` 换成 `JsonChunkCallback`。

两个细节值得记住：

1. **`clone` / `clone_ref` 都很廉价**：`state` 是 `Arc`，`clone` 只增一个引用计数；`tokio_handle` 是 `Handle`，本质也是 `Arc`；`runtime_handle.clone_ref(py)` 是 PyO3 在 GIL 下增一个 Python 引用计数。所以「每个请求造一个回调」几乎零分配开销（除了回调结构体本身那点栈数据）。
2. **`into_any()` 的意义**：`Py::new` 返回的是强类型 `Py<ChunkCallback>`，但工厂的返回类型是 `PyObject`（`Py<PyAny>`）。`.into_any()` 做类型擦除，这样 `submit_request` 就能把回调作为 `kwargs["chunk_callback"]` 塞进去——Python 不关心它的具体 Rust 类型，只关心「它可调用」。

#### 4.1.3 源码精读

[`rust/sglang-grpc/src/bridge.rs:147-156`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L147-L156) —— `make_chunk_callback`：组装 `ChunkCallback`，`Py::new` 包成 Python 对象，`into_any()` 擦除类型。

[`rust/sglang-grpc/src/bridge.rs:158-167`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L158-L167) —— `make_json_callback`：与上面同构，只换了类型名。

调用点只有两处，恰好对应两条提交路径：

- `submit_request`（数据型 RPC）在 [bridge.rs:188](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L188) 调 `make_chunk_callback`，随后在第 193 行以 `chunk_callback` 为键塞进 kwargs。
- `submit_json`（控制型 / OpenAI 型 RPC）在 [bridge.rs:324](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L324) 调 `make_json_callback`，产出的回调作为闭包 `call` 的第三个参数传递（具体由各 `submit_*` 方法决定怎么把它传给 Python 方法，见 [bridge.rs:342-405](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L342-L405)）。

> 为什么不让回调持有 `&PyBridge` 而要拆出四个字段单独 clone？因为回调对象会被传给 Python、其生命周期可能超出单次 `submit_request` 调用（Python 可能把它存起来反复调用直到流结束）。持有引用会引入复杂的生命周期约束；而 `Arc`/`Handle`/`PyObject` 都是「可安全长期持有、可 clone」的所有权类型，拆出来 clone 进回调，既解耦了生命周期，又让回调自给自足。

#### 4.1.4 代码实践

1. **实践目标**：确认「两类回调与两类提交路径一一对应」。
2. **操作步骤**：
   - 在 `bridge.rs` 里搜索 `make_chunk_callback` 和 `make_json_callback`，各数一下调用点数量（预期各 1 处）。
   - 再搜索 `submit_request` 和 `submit_json`，确认前者只服务数据型、后者服务控制型/OpenAI 型。
3. **观察现象**：`make_chunk_callback` 唯一调用点是 `submit_request`（L188）；`make_json_callback` 唯一调用点是 `submit_json`（L324）。
4. **预期结果**：你能口头复述「`submit_request` → `ChunkCallback`（dict）；`submit_json` → `JsonChunkCallback`（bytes）」这条对应关系，并能解释为什么 `submit_openai` 也用 JSON 回调（它透传的是 OpenAI 协议字节，Rust 不解析结构）。

> 待本地验证：在 `make_chunk_callback` 与 `make_json_callback` 各加一行 `tracing::debug!(rid, "created chunk/json callback");`，发起一次 `generate` 和一次 OpenAI `/v1/chat/completions` 请求，观察日志里分别打出哪类回调。

#### 4.1.5 小练习与答案

**练习 1**：`make_chunk_callback` 返回 `PyResult<PyObject>`，为什么不是 `PyResult<Py<ChunkCallback>>`？

**答案**：因为调用方（`submit_request`）要把它塞进 `kwargs`（一个 `PyDict`，值为 `PyObject`），还要传给 Python 方法。`Py<ChunkCallback>` 是强类型，塞进 dict 前必须先擦除成 `Py<PyAny>`（即 `PyObject`）。工厂直接返回 `PyObject` 省得每个调用方都写一遍 `.into_any()`，也让「回调具体是什么 Rust 类型」成为工厂内部细节。

**练习 2**：回调对象持有的四样东西里，哪一样是「创建时必须持 GIL」的？为什么？

**答案**：`runtime_handle.clone_ref(py)` 必须在持 GIL 时进行（参数 `py` 就是 GIL token）。PyO3 规定增减 Python 对象的引用计数需要 GIL 保护；其余三样（`rid` 的 `String`、`state` 的 `Arc::clone`、`tokio_handle` 的 `Handle::clone`）都是纯 Rust 操作，无需 GIL。这也正是两个工厂都接收 `py: Python<'_>` 参数、且只在 `Python::with_gil` 闭包内被调用的原因。

---

### 4.2 ChunkCallback.\_\_call\_\_：dict chunk → ResponseData → ResponseChunk

#### 4.2.1 概念说明

`ChunkCallback.__call__` 是「Python → Rust」数据流的核心翻译器，服务 SGLang 原生数据型 RPC。每当 Python 端推理产出一个片段，就调用它，签名（经 `#[pyo3(signature = ...)]` 暴露给 Python）是：

```python
callback(chunk: dict, finished: bool = False, error: Optional[str] = None)
    -> int   # ChunkSendStatus: 0=Ready, 1=Pending, 2=Closed
```

它的职责分四步：

1. **定位通道**：从账本 `channels[rid]` 取出 `Sender`；若通道已不在（被 abort / 已收尾），直接返回 `Closed`，让 Python 停止生产。
2. **错误短路**：若 Python 传了 `error`，立刻包成 `ResponseChunk::Error(error)` 发出，不再提取任何字段。
3. **字段提取**：从 `chunk` 这个 `dict` 里逐个取 `text` / `output_ids` / `embedding`，再调 `extract_meta_info` 取 `meta_info`；`json_bytes` 这一型回调恒为 `None`。
4. **决定变体 + 发送**：按 `finished` 选 `Finished` 或 `Data`，交给 `try_send_chunk`。

#### 4.2.2 核心流程

```
__call__(chunk, finished, error):
    py = chunk.py()                                   # 从 bound 对象拿 GIL token
    state = lock_or_recover(self.state)               # 加锁账本
    sender = state.channels.get(rid):
        Some(s) => s.clone()                          # 拿到对应通道发送端
        None    => return Closed                      # 通道已不在，告诉 Python 别推了
    drop(state)                                       # 关键：立即放锁，避免跨 try_send_chunk 持锁

    if error 是 Some(msg):
        return try_send_chunk(... ResponseChunk::Error(msg))   # 错误短路

    text       = chunk.get("text").extract::<String>()         # Option，缺失即 None
    output_ids = chunk.get("output_ids").extract::<Vec<i32>>()
    embedding  = chunk.get("embedding").extract::<Vec<f32>>()
    meta_info  = extract_meta_info(chunk)                      # 4.4 节详讲

    data = ResponseData { text, output_ids, embedding, json_bytes: None, meta_info }

    msg = if finished { Finished(data) } else { Data(data) }

    return try_send_chunk(py, rid, state, runtime_handle, tokio_handle, sender, msg)
```

两个**极易看漏但极重要**的细节：

- **`drop(state)` 必须在调 `try_send_chunk` 之前**。`try_send_chunk` 内部会**再次** `lock_or_recover(self.state)`（见 u3-l1 的背压分支），如果这里还持着同一把 `std::sync::Mutex`，就会**死锁**（`std::Mutex`不可重入）。所以这里只做「读出 `Sender` 后立即放锁」，把后续发送交给无锁上下文。
- **每个字段都用 `and_then(|v| v.extract::<T>().ok())`**。`get_item` 返回 `Option<Bound>`（键不存在为 `None`），`.extract()` 失败（类型不对）也吞掉变成 `None`。这是一种**容错提取**：Python 那条 dict 里某个字段缺失或类型不符，不会让整个回调报错，只是这个字段为 `None`，流仍能继续。这很合理——一条 chunk 可能只有 `text`、没有 `output_ids`，反之亦然。

#### 4.2.3 源码精读

结构体与签名：

[`rust/sglang-grpc/src/bridge.rs:610-616`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L610-L616) —— `ChunkCallback` 的四个字段，正是 4.1 节工厂塞进来的那四样。

[`rust/sglang-grpc/src/bridge.rs:630-636`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L630-L636) —— `#[pyo3(signature = (chunk, finished=false, error=None))]` 暴露给 Python 的默认参数；`__call__` 接收 `chunk: &Bound<PyDict>`（一个 Python dict 的强类型绑定）、`finished: bool`、`error: Option<String>`，返回 `ChunkSendStatus`。

通道定位与放锁：

[`rust/sglang-grpc/src/bridge.rs:637-643`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L637-L643) —— `lock_or_recover` 取锁（u2-l5 讲过的防中毒锁），从 `state.channels` 按 `self.rid` 取 `Sender` 的 `clone`（`Sender` 可 clone，多生产者）；取不到就返回 `ChunkSendStatus::Closed`。注意 `drop(state)` 紧随其后。

错误短路：

[`rust/sglang-grpc/src/bridge.rs:645-655`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L645-L655) —— 若 `error` 为 `Some(err_msg)`，直接构造 `ResponseChunk::Error(err_msg)` 发送，**不提取任何字段**。这保证 Python 端报错时，handler 侧能收到一条 `Error` 终端 chunk 并以 gRPC 错误结束流。

字段提取与装配：

[`rust/sglang-grpc/src/bridge.rs:657-677`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L657-L677) —— 逐字段 `get_item` + `extract`，`json_bytes` 显式写 `None`（这一型回调永远不产生 JSON 字节）；`meta_info` 由 `extract_meta_info(chunk)` 计算（4.4 节）。

变体决定与发送：

[`rust/sglang-grpc/src/bridge.rs:679-694`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L679-L694) —— `finished` 为真套 `Finished`、否则套 `Data`；最后统一交 `try_send_chunk`。注意 `finished` **不影响 `ResponseData` 的内容**——同一份 `data` 既可能作为中间 `Data` 发出，也可能作为终端 `Finished` 发出；`finished` 只决定「这条之后流还要不要继续」。

#### 4.2.4 代码实践

1. **实践目标**：精确回答「`__call__` 把 Python dict 的哪些键映射到了 `ResponseData` 的哪些字段」。
2. **操作步骤**：
   - 打开 [`bridge.rs:657-677`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L657-L677)。
   - 列出每个 `get_item("...")` 的键名与对应的 `ResponseData` 字段、目标 Rust 类型。
3. **观察现象**：四个键 `text`→`Option<String>`、`output_ids`→`Option<Vec<i32>>`、`embedding`→`Option<Vec<f32>>`、`meta_info`→`HashMap<String,String>`（经 `extract_meta_info`）；`json_bytes` 不从 dict 取，恒为 `None`。
4. **预期结果**：你能画出这张映射表，并指出「Python 没传的键」会因 `get_item` 返回 `None` 而安全落到 `Option::None`，不会报错。
5. **延伸**：在 [bridge.rs:637-643](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L637-L643) 处确认 `drop(state)` 的存在，思考「若删掉这行会发生什么」——答案见练习 2。

> 待本地验证：临时把 `drop(state)` 注释掉，在 debug 构建里跑一次会触发背压的请求（让消费端故意慢于生产端），观察是否死锁挂起——这能直观验证「不可重入锁 + 跨 `try_send_chunk` 持锁 = 死锁」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `output_ids` 提取成 `Vec<i32>` 而不是 `Vec<i64>` 或 `Vec<u32>`？

**答案**：因为 proto 消息里 token id 字段的类型是 `repeated int32`（见 u2-l1 的 proto 定义），tonic 生成的 Rust 类型就是 `Vec<i32>`。回调这里必须与 proto 类型对齐，否则后续 `server.rs` 把 `ResponseData.output_ids` 填进 proto 响应时会类型不符。整条链路（proto → 生成代码 → `ResponseData` → 回调提取）的类型是一致的。

**练习 2**：删掉 [bridge.rs:643](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L643) 的 `drop(state)` 会怎样？

**答案**：`state`（`MutexGuard`）会一直活到 `__call__` 结束，也就是覆盖到末尾的 `try_send_chunk` 调用。而 `try_send_chunk` 在「通道满」分支里会再次 `lock_or_recover(self.state)`（u3-l1）。`std::sync::Mutex` 不可重入，同线程二次加锁会**死锁**（线程永久挂起）。所以 `drop(state)` 不是多余的，而是「先把读出来的 `sender` 拿走、立刻放锁」的刻意安排。

**练习 3**：`finished` 参数为 `true` 时，`text` / `output_ids` 等字段还可以有值吗？

**答案**：可以。`finished` 与字段内容**完全独立**——同一次调用里既可能 `finished=true` 且 `text="最后一段"`（正常流式收尾，把最后一段文本和「结束」信号一起发出），也可能 `finished=true` 且所有字段为 `None`（仅发一个「结束」标记）。变体选择（`Finished` vs `Data`）只看 `finished`，不看字段是否为空。

---

### 4.3 JsonChunkCallback.\_\_call\_\_：bytes chunk → ResponseData

#### 4.3.1 概念说明

`JsonChunkCallback.__call__` 服务 OpenAI 透传 + 控制型 RPC。它和 `ChunkCallback.__call__` **形状几乎一模一样**（同样的「定位通道 → 放锁 → 错误短路 → 装配 → 发送」骨架），区别只在于**收的是字节而不是 dict、且多收一个 `status_code`**。Python 端签名是：

```python
callback(chunk_bytes, finished=False, error=None, status_code=None) -> int
```

关键设计：Rust **不解析字节内容**。无论是 OpenAI 协议的一段 JSON、还是某条控制命令的结果，Rust 只把它当成 `json_bytes: Vec<u8>` 透传——具体语义留给消费端（u2-l3 的流式 handler 或 u2-l4 的一元 JSON 解析）去处理。这正是「OpenAI 透传」名字的由来：Rust 是一根不透明的管子。

#### 4.3.2 核心流程

```
__call__(chunk_bytes, finished, error, status_code):
    py = chunk_bytes.py()
    state = lock_or_recover(self.state)
    sender = state.channels.get(rid) 或 return Closed
    drop(state)

    if error 是 Some(msg):
        return try_send_chunk(... ResponseChunk::Error(msg))   # 与 ChunkCallback 完全相同

    # 字节提取：兼容 bytes / str / 其它（空）
    bytes_data = match chunk_bytes.extract::<Vec<u8>>() {
                    Ok(b) => b
                 } else match extract::<String>() {
                    Ok(s) => s.into_bytes()      # 允许 Python 传 str，按 UTF-8 落字节
                 } else {
                    vec![]                        # 既非 bytes 也非 str，给空（容错）
                 }

    meta_info = {}
    if status_code 是 Some(code):
        meta_info["status_code"] = code.to_string()    # 仅这一项，且是纯数字字符串

    data = ResponseData {
        text: None, output_ids: None, embedding: None,   # 这三型恒为 None
        json_bytes: Some(bytes_data),
        meta_info,
    }

    msg = if finished { Finished(data) } else { Data(data) }
    return try_send_chunk(...)
```

#### 4.3.3 源码精读

结构体与签名：

[`rust/sglang-grpc/src/bridge.rs:698-704`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L698-L704) —— `JsonChunkCallback` 的字段与 `ChunkCallback` **完全相同**（`rid` / `state` / `runtime_handle` / `tokio_handle`）。

[`rust/sglang-grpc/src/bridge.rs:718-725`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L718-L725) —— 签名 `(chunk_bytes, finished=false, error=None, status_code=None)`，第一参是 `&Bound<PyAny>`（弱类型——因为它既可能是 `bytes` 也可能是 `str`），多一个 `status_code: Option<i32>`。

通道定位、放锁、错误短路与 4.2 节逐字一致：

[`rust/sglang-grpc/src/bridge.rs:726-744`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L726-L744) —— 取 `Sender` → `drop(state)` → `error` 短路。

字节与 `meta_info` 装配（本节的独有部分）：

[`rust/sglang-grpc/src/bridge.rs:746-765`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L746-L765) —— `bytes_data` 用 `if let Ok / else if let Ok / else` 三段式容错：先试 `Vec<u8>`（Python `bytes`），再试 `String`（Python `str`，`into_bytes` 按 UTF-8 编码），都不行给空 `vec![]`。`meta_info` 只可能含一个键 `status_code`（来自参数，`code.to_string()` 是纯数字字符串，**不走 JSON 编码**——因为来源是 `i32`，类型已知）。

变体决定与发送：

[`rust/sglang-grpc/src/bridge.rs:767-782`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L767-L782) —— 与 4.2 节末尾同构：`finished` 决定 `Finished`/`Data`，交 `try_send_chunk`。

#### 4.3.4 代码实践（本讲核心实践之一：两回调差异对照）

1. **实践目标**：列出 `ChunkCallback.__call__` 与 `JsonChunkCallback.__call__` 在提取 `ResponseData` 字段上的全部差异。
2. **操作步骤**：对照 [`bridge.rs:630-694`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L630-L694) 与 [`bridge.rs:718-782`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L718-L782)，逐字段填表。
3. **观察现象 / 预期结果**：差异表如下——

| `ResponseData` 字段 | `ChunkCallback.__call__`（dict） | `JsonChunkCallback.__call__`（bytes） |
| --- | --- | --- |
| `text` | `chunk["text"]` → `Option<String>` | 恒为 `None` |
| `output_ids` | `chunk["output_ids"]` → `Option<Vec<i32>>` | 恒为 `None` |
| `embedding` | `chunk["embedding"]` → `Option<Vec<f32>>` | 恒为 `None` |
| `json_bytes` | 恒为 `None` | `chunk_bytes` → `Some(Vec<u8>)`（兼容 `bytes`/`str`/空） |
| `meta_info` | `extract_meta_info(chunk)`：遍历 `chunk["meta_info"]` dict，**逐值 JSON 编码** | 仅 `{"status_code": code.to_string()}`（当 `status_code` 非空），**不做 JSON 编码** |
| 第一参类型 | `&Bound<PyDict>`（强类型 dict） | `&Bound<PyAny>`（弱类型，bytes/str 皆可） |
| 额外参数 | `finished`, `error` | `finished`, `error`, `status_code` |
| 服务对象 | 数据型 RPC（`submit_request`） | OpenAI 透传 + 控制型（`submit_json`） |

4. **结论**：两个回调**骨架相同、提取策略互补**——`ChunkCallback` 把 dict 拆成若干强类型字段；`JsonChunkCallback` 把一切塞进一个 `json_bytes`，外加一个可选的 `status_code`。同一次 `ResponseData` 装配里，「强类型字段」与「`json_bytes`」是互斥的（前者三个非 None 时 json_bytes 必为 None，反之亦然）。

> 待本地验证：发一次 OpenAI 流式请求，在 [`bridge.rs:746`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L746) 处打印 `bytes_data.len()`，观察每条 chunk 的字节大小；再发一次 `generate` 请求，在 [`bridge.rs:657`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L657) 处打印 `text` 是否非空——验证两类回调确实走不同字段。

#### 4.3.5 小练习与答案

**练习 1**：`JsonChunkCallback` 为什么允许第一参既可以是 `bytes` 也可以是 `str`？

**答案**：OpenAI 透传场景里，Python 端拼装响应时可能用 `bytes`（已编码的 JSON）也可能用 `str`（待编码的 JSON 文本）。为了不让 Python 端为「转成 bytes 再传」费心，回调在 Rust 侧做了兼容：先试 `Vec<u8>`，失败再试 `String` 并 `into_bytes()`（按 UTF-8 落字节）。两种输入最终都变成同一份 `Vec<u8>`，下游无感知。这是「让 Python 端用着省心」的工程取舍。

**练习 2**：`status_code` 为什么不做 JSON 编码，直接 `code.to_string()`？

**答案**：因为它的来源类型是确定的 `i32`，语义就是「HTTP/gRPC 风格的状态码整数」，下游消费者约定按整数读。`code.to_string()` 得到 `"200"` 这样纯净的数字字符串，消费者直接解析即可，无需 JSON 解包的额外约定。而 `extract_meta_info` 里的值来源是任意 Python 对象（类型未知），才需要统一的 JSON 编码契约（见 4.4 节）。

**练习 3**：假如 Python 传来的 `chunk_bytes` 是一个既非 `bytes` 也非 `str` 的对象（比如一个 `int`），会发生什么？

**答案**：两段 `extract` 都失败，落到 `else { vec![] }`，`json_bytes = Some(vec![])`——一条空字节 chunk 仍会被发送，流不会因这一条坏数据中断。这是容错设计：坏一条 chunk 顶多让消费者收到空内容，不至于让整个请求失败。生产中 Python 端理应总是传 `bytes`/`str`，这里只是兜底。

---

### 4.4 extract_meta_info 与 `map<string,string>` 的 JSON 编码

#### 4.4.1 概念说明

`extract_meta_info` 是 `ChunkCallback.__call__` 专用的辅助函数（`JsonChunkCallback` 不用它，因为它没有结构化 `meta_info`）。它做的事看起来简单：把 Python `chunk["meta_info"]` 这个 dict 搬成 Rust 的 `HashMap<String, String>`。但里面藏着一个**至关重要的设计决策**——每个 value 都要先经过 JSON 编码。

为什么要 JSON 编码？根因在 proto 契约。`meta_info` 在 proto 里被声明为 `map<string, string>`（见 u2-l1）——**键和值都必须是字符串**。但 Python 端的 `meta_info` 是个普通 dict，值可以是任意类型：整数（`{"completion_tokens": 42}`）、浮点（`{"prompt_load": 0.35}`）、布尔（`{"is_final": True}`）、列表（`{"finish_reasons": ["stop", "length"]}`）、嵌套 dict，当然也有纯字符串。

如果直接对每个值调 Python 的 `str()` 来「字符串化」，会丢类型且不可逆：

| Python 值 | `str(value)` | 问题 |
| --- | --- | --- |
| `42`（int） | `"42"` | 和字符串 `"42"` 无法区分 |
| `True`（bool） | `"True"` | Python 字面量，不是 JSON 的 `true`，客户端 `json.loads("True")` 会失败 |
| `None` | `"None"` | 既不是 JSON 的 `null`，也无法还原 |
| `"hello"`（str） | `"hello"` | 和数字字符串化的结果一模一样，无法区分「原本是字符串」还是「原本是数字」 |
| `[1, 2]` | `"[1, 2]"` | 看起来像 JSON 数组，但类型存疑 |

解决办法：**对每个值调 `json.dumps`，把它编码成一个合法的 JSON 文档字符串**。于是：

| Python 值 | `json.dumps(value)`（map 里的存储形式） | 客户端 `json.loads(...)` 还原 |
| --- | --- | --- |
| `42` | `"42"` | `42`（int）✓ |
| `True` | `"true"` | `true`（bool）✓ |
| `None` | `"null"` | `null`（None）✓ |
| `"hello"` | `"\"hello\""`（带引号） | `"hello"`（str）✓ |
| `[1, 2]` | `"[1, 2]"` | `[1, 2]`（list）✓ |

关键在第 4 行：**字符串也要被编码**（套上引号）。这是「**统一契约**」的精髓——map 里每一个 value 都是一个合法的 JSON 文档，客户端对**每一个** entry 无脑 `json.loads` 就能无损还原原始 Python 值及其类型，不需要任何特判。如果唯独字符串不编码（直接存裸串 `"hello"`），那客户端拿到 `"hello"` 就分不清「这是字符串 hello」还是「这是 JSON 文档 hello」（碰巧也是个合法字符串），又得引入「先试 JSON 解析、失败当字符串」的特判逻辑——契约就不统一了。

#### 4.4.2 核心流程

```
extract_meta_info(chunk):
    meta = {}
    if chunk.get_item("meta_info") 成功且能 downcast 成 PyDict:
        for (k, v) in meta_dict:
            key = k.extract::<String>()       # 键必须是字符串，否则跳过
            val = py_value_to_json_string(v)  # 值 → JSON 文档字符串（含字符串也编码）
            meta.insert(key, val)
    return meta
```

```
py_value_to_json_string(value):                # 在 py_utils.rs
    s = python.json.dumps(value)               # 借 Python 的 json 模块编码
    成功 => s
    失败（不可序列化对象等） =>
        fallback = value.str()                 # 退而求其次：str() 再包成 JSON 字符串
        return json_encode_string(fallback)    #   => "\"<str 结果>\""
```

兜底分支 `json_encode_string` 用 `serde_json` 把 `str()` 结果再包一层 JSON 字符串引号，**保证即便 json.dumps 失败，map 里的 value 仍是一个合法 JSON 文档**（一个 JSON 字符串），契约不破。

#### 4.4.3 源码精读

`extract_meta_info`：

[`rust/sglang-grpc/src/bridge.rs:785-801`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L785-L801) —— 注意它用了 `let ... && let ...` 的链式 `let`（Rust 2024 edition 稳定的 `let-chains`）：先 `get_item("meta_info")` 成功且非 None，再 `downcast::<PyDict>` 成功，才进入遍历。循环里同样用 `if let Ok(key) && let Ok(val)` 保证「键能提取成 String、值能编码成 JSON」才插入——任一失败就跳过该 entry，不污染 map。注释（L791–792）把「为什么 JSON 编码」讲得很直白：proto 是 `map<string,string>`，编码每个值以便客户端无损还原数字、布尔、数组、对象。

`py_value_to_json_string`（在 `py_utils.rs`）：

[`rust/sglang-grpc/src/utils/py_utils.rs:48-63`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/py_utils.rs#L48-L63) —— 用 `value.py().import("json")` 拿到 Python 的 `json` 模块，`call_method1("dumps", (value,))` 调 `json.dumps(value)`，再 `extract::<String>()` 取回结果。失败时走 `value.str()?.to_string()` 取 `str()`，交给 `json_encode_string` 包成 JSON 字符串。注释（L49–50）再次强调「包括字符串在内都编码，以便客户端统一解码」。

[`rust/sglang-grpc/src/utils/py_utils.rs:65-67`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/py_utils.rs#L65-L67) —— `json_encode_string`：`serde_json::Value::String(s).to_string()`，即把任意 `&str` 序列化成一个 JSON 字符串（自动加引号、转义内部引号），保证返回值恒为合法 JSON。

> 为什么用 Python 的 `json.dumps` 而不是 Rust 的 `serde_json` 来编码？因为输入是 **Python 对象**（`Bound<PyAny>`），Rust 的 serde 不认识 Python 的 `dict`/`list`/`int`。最稳妥的方式是让 Python 自己序列化自己的对象——`json.dumps` 天然认识所有 Python 原生类型。只有兜底分支（已经 `str()` 成 Rust `String` 了）才轮到 Rust 的 `serde_json` 出场。

#### 4.4.4 代码实践（本讲核心实践之二：JSON 编码的必要性）

1. **实践目标**：解释「为什么 `extract_meta_info` 要对每个 value（包括字符串）做 JSON 编码」。
2. **操作步骤**：
   - 阅读 [`bridge.rs:785-801`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L785-L801) 与 [`py_utils.rs:48-63`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/utils/py_utils.rs#L48-L63)。
   - 假设 Python 推来 `meta_info = {"completion_tokens": 42, "is_final": True, "model": "qwen"}`，推演 map 里存的三个字符串值。
3. **观察现象 / 预期结果**：
   - `"completion_tokens"` → `"42"`（`json.dumps(42)`），客户端 `json.loads("42")` 得 `int 42`；
   - `"is_final"` → `"true"`（`json.dumps(True)`），客户端得 `bool True`；
   - `"model"` → `"\"qwen\""`（`json.dumps("qwen")`，注意带了引号），客户端 `json.loads("\"qwen\"")` 得 `str "qwen"`。
4. **结论**：三者都是合法 JSON 文档，客户端对三个 value 用**同一套** `json.loads` 即可还原出 int / bool / str 三种不同类型——这正是「逐值 JSON 编码（含字符串）」换来的统一解码契约。若字符串不编码（`"model"` 存成裸 `"qwen"`），客户端就得猜「这串到底是 JSON 文档还是裸字符串」，契约破裂。

> 待本地验证：在 [`bridge.rs:797`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L797) 处临时 `tracing::debug!(key, val, "meta_info entry");`，发一次 `generate` 请求，观察日志里字符串型 meta_info 的 `val` 是否带引号、数值型是否为裸数字。

#### 4.4.5 小练习与答案

**练习 1**：proto 为什么不把 `meta_info` 声明成 `map<string, google.protobuf.Value>`（一种能直接表达任意 JSON 的类型），而坚持用 `map<string, string>`？

**答案**：这是工程取舍。`map<string,string>` 在 proto 序列化上更简单、跨语言更省心（所有 gRPC 客户端都能直接读字符串 map），且 SGLang 选择把「值的类型语义」交给应用层用 JSON 约定来恢复，而不是依赖 proto 的 `Value` 类型（并非所有语言栈都方便处理 `Value`）。代价是值要多一层 `json.dumps`/`json.loads`。对 meta_info 这种「小体积、可读、客户端各自需要不同字段」的场景，这个取舍是划算的。

**练习 2**：`py_value_to_json_string` 的兜底分支（`json.dumps` 失败）什么时候会触发？触发后契约还成立吗？

**答案**：当 Python 值是 `json.dumps` 无法序列化的对象（比如自定义类实例、含循环引用的对象、`set` 等）时触发。触发后走 `value.str()` 取字符串描述，再 `json_encode_string` 包成 JSON 字符串——即 map 里存的是 `"\"<对象的可读描述>\""`，一个合法的 JSON 字符串文档。客户端 `json.loads` 会得到一个字符串（对象的 str 描述），类型是 str。契约（「每个 value 都是合法 JSON 文档」）依然成立，只是语义上从「原值」退化为「原值的字符串描述」，是有损但安全的降级。

**练习 3**：`extract_meta_info` 的循环里，键 `k` 如果不是字符串（比如 Python 用了 `int` 作 key），会怎样？

**答案**：`k.extract::<String>()` 会失败，落到 `if let Ok(key) && ...` 的短路，整个 entry 被跳过——该键值对不会进入 Rust 的 `meta` map。这是静默丢弃，符合「proto 键必须是 string」的约束。生产中 Python 的 meta_info 键理应都是字符串，这里只做防御。

---

### 4.5 set_on_ready / clear_on_ready：就绪信号注册

#### 4.5.1 概念说明

两个回调还各自暴露一对同名方法：`set_on_ready(on_ready)` 和 `clear_on_ready()`。它们是**背压反馈通道的注册端**，但本节只讲「注册」这一半——完整的「通道满 → 停泊 → 通知恢复」流程是 u3-l1 的主题，这里先把注册机制讲清，因为它直接操作 u2-l5 账本里的两张表。

背景：当 `try_send_chunk` 发现通道已满（`TrySendError::Full`），它会把 chunk「停泊」到一个异步任务里慢慢发，并通过返回 `ChunkSendStatus::Pending` 告诉 Python「先别推了」。问题是：**Python 怎么知道什么时候可以恢复推送？** 答案是 Rust 在停泊的 chunk 终于排空后，回调 Python 注册的 `on_ready` 函数。`set_on_ready` 就是 Python 用来「登记这个恢复回调」的入口。

`on_ready` 的注册必须解决一个时序难题：**如果 chunk 排空发生在 Python 注册 `on_ready` 之前（晚注册），信号会不会丢？** 答案是不能丢——否则 Python 会永远等不到「可以继续」的信号而卡死。为此账本里有 `ready_callbacks`（已注册的回调）和 `ready_signals`（已排空但还没人注册的「待补发」标记）两张表配合，实现「边沿触发不丢信号」。

#### 4.5.2 核心流程

```
set_on_ready(on_ready):                         # Python 在开始生产前注册
    加锁 state
    ready_callbacks[rid] = on_ready             # 记下回调
    had_signal = ready_signals.remove(rid)      # 顺手看：之前是否已排空但没人接？
    放锁
    if had_signal:
        on_ready()                              # 已排空过 → 立即补发，不丢边沿

clear_on_ready():                               # 流结束后注销
    加锁 state
    ready_callbacks.remove(rid)
    ready_signals.remove(rid)                   # 两者一起清，避免幽灵信号
    放锁
```

对应的「排空侧」（u3-l1 详讲）在 `try_send_chunk` 的停泊任务里：chunk 排空后调 `mark_send_ready`，它先删 `pending_sends[rid]`，再看 `ready_callbacks[rid]` 在不在——在就直接拿到回调去 `notify_ready`；不在就往 `ready_signals[rid]` 插一个标记，等 Python 将来 `set_on_ready` 时补发。两张表一前一后，保证了「信号产生」与「回调注册」无论谁先发生，最终都能配对成功。

> 终端 chunk 契约：一旦发了 `Finished`/`Error` 这类终端 chunk，`try_send_chunk` 会直接 `remove_channel_refs` 把该 `rid` 的所有表项清掉（含 `ready_callbacks`/`ready_signals`），**不再触发任何 `on_ready`**——因为流已结束，没有「继续生产」的必要了。这写在 [bridge.rs:565-570](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L565-L570) 的注释里。

#### 4.5.3 源码精读

回调侧的两个方法（两个回调类实现完全相同，都委托给自由函数）：

[`rust/sglang-grpc/src/bridge.rs:620-628`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L620-L628) —— `ChunkCallback` 的 `set_on_ready` / `clear_on_ready`，方法上的 doc 注释点明了「晚注册也能补发」的设计意图。

[`rust/sglang-grpc/src/bridge.rs:710-716`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L710-L716) —— `JsonChunkCallback` 的同名方法，逐字一致。

真正的逻辑在两个自由函数里：

[`rust/sglang-grpc/src/bridge.rs:498-515`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L498-L515) —— `set_on_ready_for_rid`：持锁把 `on_ready` 写进 `ready_callbacks[rid]`，同时 `ready_signals.remove(rid)` 取出可能存在的待补发标记；**放锁后**若确有信号，立即 `on_ready.call0(py)` 补发。注意「先放锁再调回调」——和 4.2 节 `drop(state)` 同理，避免在持锁状态下跨 Python 调用（`on_ready` 是 Python 对象，调用它可能反向争用这把锁或长时间持 GIL）。

[`rust/sglang-grpc/src/bridge.rs:517-522`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L517-L522) —— `clear_on_ready_for_rid`：同时删 `ready_callbacks[rid]` 与 `ready_signals[rid]`，注释明确「别再为同一 rid 调 set_on_ready」。

排空侧（仅供对照，细节留 u3-l1）：

[`rust/sglang-grpc/src/bridge.rs:481-496`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L481-L496) —— `mark_send_ready`（删 `pending_sends`、有回调则返回它、无则插 `ready_signals`）与 `notify_ready`（调用回调、失败只记 warn 不报错）。

#### 4.5.4 代码实践

1. **实践目标**：验证「`set_on_ready` 与排空侧通过 `ready_signals` 互为因果，不丢信号」。
2. **操作步骤**：
   - 对照 [`set_on_ready_for_rid`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L498-L515) 与 [`mark_send_ready`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L481-L490)，列出两种时序下各自的处理。
3. **观察现象**：
   - **先排空、后注册**：`mark_send_ready` 发现 `ready_callbacks` 里没回调 → 往 `ready_signals` 插标记；之后 `set_on_ready_for_rid` 注册回调时 `ready_signals.remove(rid)` 命中 → 立即 `on_ready()` 补发。
   - **先注册、后排空**：`set_on_ready_for_rid` 注册时 `ready_signals` 为空 → 不补发；之后 `mark_send_ready` 发现 `ready_callbacks` 有回调 → 返回回调 → `notify_ready` 正常触发。
4. **预期结果**：两种时序最终都恰好触发一次 `on_ready`，既不丢信号也不重复触发。这正是「注册端 + 排空端」两侧对账本读写配合的结果。

> 待本地验证：本节实践为「源码阅读型」，运行时复现需要构造背压（让消费端慢于生产端），完整跟踪留到 u3-l1 的背压实践。

#### 4.5.5 小练习与答案

**练习 1**：`set_on_ready_for_rid` 为什么「先放锁、再调 `on_ready`」？

**答案**：`on_ready` 是 Python 对象，`call0(py)` 会获取 GIL 并执行 Python 代码，可能耗时；若在持 `state` 锁期间调用，一方面长时间持锁会阻塞其他请求对账本的访问，另一方面若 `on_ready` 内部又间接触发账本操作就会死锁。先在锁内完成「写回调 + 取信号」的纯数据操作并放锁，再在锁外调 Python，临界区最短、无重入风险。这与 `ChunkCallback.__call__` 里 `drop(state)` 在 `try_send_chunk` 之前是同一类考量。

**练习 2**：`clear_on_ready` 为什么要把 `ready_signals[rid]` 也一起删掉，而不是只删 `ready_callbacks[rid]`？

**答案**：`clear_on_ready` 的语义是「这条流结束了，别再通知」。若只删回调、留下 `ready_signals`，则那条「待补发」标记会变成无人认领的幽灵信号，万一同一 `rid` 后来被复用（开新一轮请求），新一轮的 `set_on_ready` 会错误地命中旧信号而误触发。两表一起清，才能让该 `rid` 的就绪信号机制彻底复位。

---

## 5. 综合实践

把本讲的知识串起来，做一个「**完整追踪一条 chunk 从 Python dict 到 gRPC 字节的全过程**」的阅读型实践。

**任务**：以一次 `generate` 流式请求为例，从 Python 推来一个 dict chunk，到 Rust 把它变成 `ResponseChunk` 塞进通道，画出「字段级」的数据流图，并在每个环节标注源码位置与「字段此刻的取值」。

**操作步骤**：

1. **回调就位**：回到 u2-l5 的入口 `submit_request`（[bridge.rs:177-207](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L177-L207)），确认它在 [bridge.rs:188](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L188) 用 `make_chunk_callback` 造出回调，回调内绑定 `rid` + 账本 `Arc` + `runtime_handle` + `tokio_handle`（4.1 节）。
2. **Python 推数据**：假设 Python 调 `chunk_callback({"text": "你好", "output_ids": [1234], "meta_info": {"completion_tokens": 2}}, finished=False)`。
3. **通道定位**：进入 [`ChunkCallback.__call__`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L630-L694)（4.2 节），`state.channels.get(rid)` 拿到 `Sender`，`drop(state)` 放锁。
4. **字段提取**：按 [bridge.rs:657-669](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L657-L669) 提取：`text = Some("你好")`、`output_ids = Some([1234])`、`embedding = None`；`extract_meta_info`（[bridge.rs:785-801](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L785-L801)）把 `meta_info` 处理成 `{"completion_tokens": "2"}`（数值经 `py_value_to_json_string` → `json.dumps(2)` → `"2"`，4.4 节）。
5. **装配与变体**：[bridge.rs:671-683](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L671-L683) 组出 `ResponseData { text: Some("你好"), output_ids: Some([1234]), embedding: None, json_bytes: None, meta_info: {"completion_tokens":"2"} }`；`finished=false` → 套 `ResponseChunk::Data(data)`。
6. **发送**：交 `try_send_chunk`（[bridge.rs:685-693](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L685-L693)），返回 `ChunkSendStatus::Ready`，Python 继续推下一条。

**产出**：一张字段流转表，形如：

| 阶段 | `text` | `output_ids` | `embedding` | `json_bytes` | `meta_info` | 源码 |
| --- | --- | --- | --- | --- | --- | --- |
| Python dict | `"你好"` | `[1234]` | （无） | — | `{"completion_tokens": 2}`（int 值） | 调用方 |
| 提取后 `ResponseData` | `Some("你好")` | `Some([1234])` | `None` | `None` | `{"completion_tokens": "2"}`（JSON 编码后） | bridge.rs:657–677 |
| `ResponseChunk` | — | — | — | — | — | `Data(data)`（finished=false）→ bridge.rs:679–683 |
| 进通道 | — | — | — | — | — | try_send_chunk（bridge.rs:685–693） |

**对比扩展**：把同一练习对 `JsonChunkCallback` 重做一遍——假设 OpenAI 透传推 `chunk_callback(b'{"choices":[...]}', finished=True, status_code=200)`，按 [bridge.rs:746-771](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L746-L771) 得 `ResponseData { text:None, output_ids:None, embedding:None, json_bytes: Some(<那段字节>), meta_info: {"status_code":"200"} }`，套 `Finished(data)`。

**预期结果**：你能完整复述「Python dict/bytes → 字段提取 → meta_info JSON 编码 → 变体选择 → 进通道」这条链，并能指出 `ChunkCallback` 与 `JsonChunkCallback` 在每个阶段的字段差异——即本讲的全部要点。

## 6. 本讲小结

- **两类回调服务两类 RPC**：`ChunkCallback`（dict）服务 SGLang 原生数据型（`submit_request` → `make_chunk_callback`）；`JsonChunkCallback`（bytes）服务 OpenAI 透传 + 控制型（`submit_json` → `make_json_callback`）。
- **回调工厂打包四样能力**：`make_chunk_callback` / `make_json_callback` 把 `rid`、账本 `Arc`、`runtime_handle`（`clone_ref`，需 GIL）、`tokio_handle` 塞进对象，使回调能独立完成「取通道、发 chunk、必要时 abort/spawn」的闭环；`Py::new` + `into_any()` 包成 `PyObject` 塞进 kwargs。
- **`ChunkCallback.__call__` 四步**：定位通道（不在则返回 `Closed`）→ `drop(state)` 放锁 → `error` 短路成 `Error` chunk → 容错提取 `text`/`output_ids`/`embedding`/`meta_info`（`json_bytes` 恒 None），按 `finished` 选 `Data`/`Finished`，交 `try_send_chunk`。
- **`JsonChunkCallback.__call__` 同骨架异提取**：收 `bytes`（兼容 `bytes`/`str`/空）填 `json_bytes`，其余三字段恒 None；`meta_info` 只来自 `status_code`（纯数字字符串，不 JSON 编码）。
- **`extract_meta_info` 逐值 JSON 编码**：因 proto 是 `map<string,string>` 而 Python 值类型任意，对每个 value（含字符串）调 `py_value_to_json_string`（Python `json.dumps`，失败退 `str()` + `serde_json` 包字符串），换来客户端「无脑 `json.loads` 每个 value 即可无损还原类型」的统一契约。
- **`set_on_ready`/`clear_on_ready` 是背压注册端**：靠 `ready_callbacks` 与 `ready_signals` 两张表配合实现「晚注册也能补发」的边沿不丢信号；终端 chunk 后不再触发 `on_ready`。完整停泊/通知流程见 u3-l1。

## 7. 下一步学习建议

本讲讲清了「Python → Rust」这一侧的数据流细节，但还有几条线没收尾。建议按顺序继续：

1. **u2-l7 请求字典构建**：本讲讲的是「响应方向」（Python→Rust），u2-l7 讲「请求方向」（Rust→Python）——`utils/request_utils.rs` 的 `build_*_dict` 如何把 proto 消息变成 Python dict，再经 `json_map_to_pydict`（与本讲的 `py_value_to_json_string` 同属 `py_utils.rs`）交给 Python。两讲合起来就是一个完整的请求-响应往返。
2. **u3-l1 背压与 pending-send 停泊**：本讲多次把 `try_send_chunk` 当「出口黑盒」、把 `set_on_ready` 当「注册端」——u3-l1 会补上「通道满 → `register_pending_send` → `tokio_handle.spawn` 异步发送 → `mark_send_ready`/`notify_ready`」的完整停泊流程，是 `BridgeState` 后四张表真正发挥威力的地方。
3. **u3-l2 中止传播**：回调持有的 `runtime_handle` 在「通道关闭」时会被 `close_channel_with_error` 反向调用 `abort`（Python）；u3-l2 讲清 `RequestAbortGuard` 的 RAII 中止与 `abort`/`abort_all` 的批量清理。
4. **u2-l3 流式 RPC 消费**：本讲只讲 chunk 怎么进通道；u2-l3 讲 tonic handler 怎么用 `async_stream::stream!` 从 `Receiver` 把 `Data`/`Finished`/`Error` 一条条吐回客户端，是「Rust → gRPC 客户端」那一半。
