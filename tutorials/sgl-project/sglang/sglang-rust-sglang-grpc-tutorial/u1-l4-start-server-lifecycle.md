# 启动入口全景：start_server 与 GrpcServerHandle 生命周期

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 Python 端调用 `_core.start_server(...)` 后，Rust 侧按什么顺序做了哪些事（绑定端口、提取分词器、起 Tokio 运行时、起 OS 线程跑 tonic server）。
- 解释 `worker_threads` / `response_channel_capacity` / `response_timeout_secs` 这三个参数的「下限归一化」规则，以及为什么 0 不被允许。
- 读懂 `extract_tokenizer_info` 如何借一次性 GIL 从 Python `RuntimeHandle` 上摘出 `tokenizer_path` / `tokenizer_mode` / `context_len`。
- 理解返回给 Python 的 `GrpcServerHandle` 是如何用 `tokio::sync::Notify` + `std::thread::JoinHandle` 控制 gRPC 服务的「优雅关停」与「存活探测」的。

本讲是后续所有服务端讲义（u2、u3）的「地基」：服务的构造、桥接通道的容量、响应超时的默认值，全部在这一讲里定型。

## 2. 前置知识

在进入源码前，先用大白话把三个概念讲清楚，它们贯穿整篇：

- **PyO3 的 `#[pyfunction]` 与 `#[pyo3(signature = ...)]`**：PyO3 是 Rust ↔ Python 的绑定库。被 `#[pyfunction]` 标注的 Rust 函数会被导出成 Python 可调用对象；`signature` 宏参数则声明了「参数名 + 默认值」，让 Python 侧可以 `start_server(host, port, rt)` 这样省略带默认值的参数。本讲的 `start_server` 就是这样一个被导出的函数。
- **Tokio 运行时（multi-thread）**：Tokio 是 Rust 的异步运行时。`new_multi_thread()` 会建一个拥有多个工作线程的线程池，负责驱动所有 `async fn`（比如 tonic 的 gRPC handler）。`rt.handle()` 拿到的是一个「运行时句柄」，可以 clone 出来到处传，让别人能在任意线程上 `handle.spawn(...)` 往这个运行时提交异步任务。
- **Python GIL（全局解释器锁）**：任何对 Python 对象（`PyObject`）的访问都必须持有 GIL。在 Rust 里用 `Python::with_gil(|py| { ... })` 获取 GIL。`start_server` 主线程本身就在 Python 调用栈里、天然持有 GIL，但服务跑在独立 OS 线程上，后续访问 Python 时要主动 `with_gil`。

> 还有一个关键概念 **Notify**：Tokio 提供的「一次性信号」。`notify_one()` 唤醒等待方，`notified().await` 阻塞直到被唤醒。本讲用它做关停信号。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件，并少量引用另外两个文件的常量与函数签名：

| 文件 | 作用 | 本讲引用什么 |
| --- | --- | --- |
| `rust/sglang-grpc/src/lib.rs` | crate 根，含 PyO3 模块导出、`start_server`、`GrpcServerHandle`、分词器信息提取 | 全部核心代码 |
| `rust/sglang-grpc/src/bridge.rs` | Python 桥接器 `PyBridge` 与通道默认容量 | `PyBridge::new` 签名、`DEFAULT_RESPONSE_CHANNEL_CAPACITY` |
| `rust/sglang-grpc/src/server.rs` | gRPC 服务实现与服务引导 | `DEFAULT_RESPONSE_TIMEOUT_SECS`、`run_grpc_server` 签名 |
| `rust/sglang-grpc/src/tokenizers.rs` | Rust 原生分词器 | `RustTokenizer::from_tokenizer_path` 签名 |

一句话总结：`lib.rs` 是「装配车间」，它把 Python 的 `runtime_handle`、Rust 的分词器、Tokio 运行时、tonic server 串成一条启动流水线，最后交还给 Python 一个 `GrpcServerHandle` 遥控器。

## 4. 核心概念与源码讲解

### 4.1 返回给 Python 的遥控器：GrpcServerHandle

#### 4.1.1 概念说明

`start_server` 启动服务后不能「一直阻塞」到服务结束，否则 Python 调用方就拿不到控制权了。因此 Rust 把服务放进后台线程，并返回一个**句柄（handle）**给 Python。这个句柄就是 `GrpcServerHandle`，它对外只暴露两个能力：

1. **优雅关停**：`shutdown()`，通知服务停止并等待后台线程结束。
2. **存活探测**：`is_alive()`，判断服务线程是否还在跑。

这两个能力背后是两件不同的「武器」：`Notify` 负责「通知服务该停了」，`JoinHandle` 负责「等待线程真的退出」。把这两者组合，才能做到「先发起关停、再确认线程已死」。

#### 4.1.2 核心流程

```text
Python 调 handle.shutdown()
        │
        ▼
shutdown.notify_one()  ──► 唤醒 run_grpc_server 里 shutdown.notified().await
        │                          (tonic 开始优雅关停，排空连接)
        ▼
join_handle.take().join()  ──► 阻塞等待 OS 线程退出
        │
        ▼
   shutdown() 返回，Python 侧确信服务已停止
```

`is_alive()` 则只看 `JoinHandle` 是否还在（未被 take 且线程未结束）。

#### 4.1.3 源码精读

句柄结构体只持有两个字段，[src/lib.rs:20-25](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L20-L25)：

```rust
#[pyclass]
struct GrpcServerHandle {
    shutdown: Arc<Notify>,
    join_handle: Option<std::thread::JoinHandle<()>>,
}
```

- `shutdown` 用 `Arc<Notify>` 是因为同一个 `Notify` 也要被 clone 进后台线程，交给 `run_grpc_server` 等待。
- `join_handle` 用 `Option` 是因为 `shutdown()` 里要 `take()` 它来 `join()`，take 后就变 `None`。

关停与探测的实现，[src/lib.rs:27-41](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L27-L41)：

```rust
#[pymethods]
impl GrpcServerHandle {
    /// Gracefully shut down the gRPC server.
    fn shutdown(&mut self) {
        self.shutdown.notify_one();
        if let Some(handle) = self.join_handle.take() {
            let _ = handle.join();
        }
    }

    /// Check if the server thread is still running.
    fn is_alive(&self) -> bool {
        self.join_handle.as_ref().is_some_and(|h| !h.is_finished())
    }
}
```

要点：`shutdown()` 先 `notify_one()` 再 `join()`——顺序很重要，否则会先死等一个「还没收到停机信号」的线程。`join()` 的返回值被 `let _ =` 丢弃，因为后台线程内部已经把错误用 `tracing::error!` 记了（见 4.5），这里不需要再向上抛。`is_alive()` 用 `is_some_and` 同时表达「句柄还在」且「线程未结束」。

> 后续在 [src/server.rs:998-1004](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L998-L1004) 你会看到 `notify_one()` 唤醒的那一侧：`serve_with_incoming_shutdown(..., async move { shutdown.notified().await; ... })`。本讲先建立「信号两侧」的整体感，u3-l4 会精读引导过程。

#### 4.1.4 代码实践

**实践目标**：从 Python 侧验证 `GrpcServerHandle` 的两个方法语义（不真正发 gRPC 请求，只看生命周期）。

**操作步骤**（属于「源码阅读型实践」，无需可运行的 Rust 环境；若有已编译好的 `sglang.srt.grpc._core` 可替换为真跑）：

1. 在 `src/lib.rs` 中确认 `_core` 模块确实导出了 `GrpcServerHandle` 类，[src/lib.rs:259-265](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L259-L265)。
2. 顺着 `shutdown()` 的 `notify_one()` 找到消费方 `run_grpc_server` 里的 `shutdown.notified().await`。
3. 假设你在 Python 里这样调用（伪代码，标注「示例代码」）：

   ```python
   # 示例代码：仅示意句柄用法，依赖真实编译产物
   from sglang.srt.grpc import _core
   handle = _core.start_server("127.0.0.1", 40000, runtime_handle)
   print(handle.is_alive())   # 预期 True：线程在跑
   handle.shutdown()          # 发起关停并阻塞到线程退出
   print(handle.is_alive())   # 预期 False：join_handle 已被 take
   ```

**需要观察的现象**：`shutdown()` 调用会**阻塞**直到后台线程真正退出（因为内部 `handle.join()`）；第二次 `is_alive()` 必为 `False`，因为 `join_handle` 已被 `take()` 成 `None`。

**预期结果**：先 `True` 后 `False`。若实际运行，待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `shutdown()` 里的 `notify_one()` 和 `join()` 调换顺序，会发生什么？

**答案**：`join()` 会先阻塞等待后台线程退出，但此时 `Notify` 还没被触发，`run_grpc_server` 里的 `shutdown.notified().await` 永远不会返回，服务不会主动停机——于是 `join()` 会**死锁式阻塞**。所以必须「先通知、后等待」。

**练习 2**：`is_alive()` 为什么用 `Option::is_some_and`，而不是直接 `self.join_handle.is_finished()`？

**答案**：因为 `join_handle` 可能已经被 `shutdown()` 里的 `take()` 变成 `None`（表示服务已被显式关停）。直接对一个不存在的句柄调 `is_finished()` 会编译不过；`is_some_and` 先确认「句柄还在」，再判断「线程未结束」，语义更准确。

---

### 4.2 Python 属性提取助手：try_get_attr 家族

#### 4.2.1 概念说明

`start_server` 需要从 Python 的 `RuntimeHandle` 上读取分词器路径等信息。但 Python 对象是「鸭子类型」的——某个属性可能存在、可能不存在，类型也可能不是你期望的。如果在 Rust 里直接 `obj.getattr(py, "xxx")?`，遇到任何一个缺失属性都会让整个启动失败。

SGLang 的策略是「核心属性必须存在、子属性尽量容忍」，于是封装了三个**容错式取值**助手：

- `try_get_attr`：取任意属性，缺失返回 `None`（不报错）。
- `try_get_attr_str`：取属性并尝试转成 `String`。
- `try_get_attr_i32`：取属性并尝试转成 `i32`。

它们都用 `tracing::debug!` 记录失败原因，而不是 panic 或返回 `Err`。

#### 4.2.2 核心流程

```text
try_get_attr_str(obj, "tokenizer_path")
        │
        ├─ try_get_attr(obj, "tokenizer_path")  ──► getattr 失败？ → None（记 debug）
        │
        └─ 对拿到的值 value.extract::<String>()
                                ├─ 成功 → Some(String)
                                └─ 失败 → None（记 debug，类型不对）
```

#### 4.2.3 源码精读

最底层的 `try_get_attr`，[src/lib.rs:49-64](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L49-L64)：

```rust
fn try_get_attr(
    py: Python<'_>,
    obj: &PyObject,
    attr: &'static str,
    context: &'static str,
) -> Option<PyObject> {
    obj.getattr(py, attr).map(Some).unwrap_or_else(|err| {
        tracing::debug!("{}.{} is unavailable: {}", context, attr, err);
        None
    })
}
```

- `context` 是「这个属性来自哪个对象」的人类可读标签，只用于日志（如 `"tokenizer_manager"`、`"server_args"`）。
- `&'static str` 让调用方只能传字面量，避免构造临时 `String`。

`try_get_attr_str` 与 `try_get_attr_i32` 只是「在 `try_get_attr` 之上加一步类型提取」，[src/lib.rs:66-92](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L66-L92)：

```rust
fn try_get_attr_str(...) -> Option<String> {
    try_get_attr(py, obj, attr, context).and_then(|value| {
        value.extract(py).map(Some).unwrap_or_else(|err| {
            tracing::debug!("Could not extract {}.{} as string: {}", context, attr, err);
            None
        })
    })
}
// try_get_attr_i32 结构完全相同，只是目标类型是 i32
```

要点：`value.extract(py)` 是 PyO3 把任意 Python 对象转成目标 Rust 类型的操作；转换失败同样降级为 `None` 而非报错。这就是「best-effort」语义——拿不到就回退，不影响启动。

#### 4.2.4 代码实践

**实践目标**：理解 `context` 参数在排障时的价值。

**操作步骤**：

1. 设 `RUST_LOG=debug`（`start_server` 顶部 `EnvFilter::try_from_default_env()` 会读它，见 4.4）。
2. 在 [src/lib.rs:107-115](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L107-L115) 追踪 `tokenizer_path` 的三级回退链：先 `server_args.tokenizer_path`，再 `server_args.model_path`，最后 `tokenizer_manager.model_path`。
3. 假设三个都没有，你应该能在日志里看到三条 `... is unavailable` 的 debug 行，每条都带着不同的 `context`（`server_args` / `tokenizer_manager`），据此就能判断到底哪一层断了。

**需要观察的现象**：当传入一个「缺字段」的假 `runtime_handle` 时，日志能精确定位缺失发生在哪个对象上，而不是一句笼统的 AttributeError。

**预期结果**：日志按 `context.attr` 格式逐条输出缺失原因。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `try_get_attr` 用 `unwrap_or_else` 而不是 `?` 向上抛 `PyErr`？

**答案**：因为这组函数的设计目标就是「容错取值」。用 `?` 会让任何缺失属性直接终止 `start_server`，违背「子属性 best-effort」的策略；`unwrap_or_else` 把失败降级为 `None` + 一条 debug 日志，让上层自行决定 `None` 是否致命。

**练习 2**：`try_get_attr_i32` 在「属性存在但值是浮点数」时会怎样？

**答案**：`value.extract::<i32>(py)` 会失败（类型不匹配），于是走 `unwrap_or_else` 分支返回 `None` 并记 debug。调用方 `extract_tokenizer_info` 对 `context_len` 的 `None` 会兜底为 `0`（见 4.3）。

---

### 4.3 一次性 GIL 提取：extract_tokenizer_info

#### 4.3.1 概念说明

`start_server` 后续要把服务丢到独立 OS 线程里跑，那个线程不会天然持有 GIL。为减少跨线程抢 GIL 的次数，启动期就「一次性」把分词器相关的三样东西从 Python 对象里摘出来存成纯 Rust 结构 `TokenizerInfo`：

- `tokenizer_path`：分词器文件路径（用于加载 Rust 原生分词器）。
- `tokenizer_mode`：分词模式（如 `"slow"` 会禁用 Rust 分词器）。
- `context_len`：模型上下文长度（用于校验）。

其中 `tokenizer_manager` 是**必须存在**的（缺失即配置错误，应让启动失败），其余子属性都是 best-effort。

#### 4.3.2 核心流程

```text
Python::with_gil(|py| {
   ① runtime_handle.tokenizer_manager   ——必须存在，否则 PyValueError
   ② tm.server_args                     ——best-effort
   ③ tokenizer_path ← server_args.tokenizer_path
                     ↘ server_args.model_path
                     ↘ tm.model_path      (三级回退)
   ④ tokenizer_mode ← server_args.tokenizer_mode
   ⑤ context_len   ← tm.model_config.context_len  (失败兜底 0)
})
返回 TokenizerInfo（纯 Rust，不再依赖 Python 对象）
```

#### 4.3.3 源码精读

`TokenizerInfo` 是个普通结构体，[src/lib.rs:43-47](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L43-L47)：

```rust
struct TokenizerInfo {
    tokenizer_path: Option<String>,
    tokenizer_mode: Option<String>,
    context_len: i32,
}
```

提取逻辑整体包在 `Python::with_gil` 里（一次性持锁），[src/lib.rs:94-139](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L94-L139)。关键三段：

第一，`tokenizer_manager` 必须存在（这是唯一会主动报错的地方），[src/lib.rs:95-103](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L95-L103)：

```rust
let tm = runtime_handle
    .getattr(py, "tokenizer_manager")
    .map_err(|err| {
        pyo3::exceptions::PyValueError::new_err(format!(
            "runtime_handle.tokenizer_manager is required: {}", err
        ))
    })?;
```

第二，`tokenizer_path` 的三级回退，[src/lib.rs:107-118](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L107-L118)：

```rust
let tokenizer_path = server_args
    .as_ref()
    .and_then(|args| try_get_attr_str(py, args, "tokenizer_path", "server_args"))
    .or_else(|| server_args.as_ref()
        .and_then(|args| try_get_attr_str(py, args, "model_path", "server_args")))
    .or_else(|| try_get_attr_str(py, &tm, "model_path", "tokenizer_manager"));
if tokenizer_path.is_none() {
    tracing::warn!("Could not extract tokenizer path; Rust tokenizer disabled");
}
```

`None` 时只是 `warn`（禁用 Rust 分词器，回退 Python 分词），不致命。

第三，`context_len` 失败兜底 `0`，[src/lib.rs:124-131](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L124-L131)：

```rust
let context_len = try_get_attr(py, &tm, "model_config", "tokenizer_manager")
    .and_then(|model_config| {
        try_get_attr_i32(py, &model_config, "context_len", "model_config")
    })
    .unwrap_or_else(|| {
        tracing::warn!("Could not extract model_config.context_len; defaulting to 0");
        0
    });
```

> 三段合起来体现了「致命 vs 容忍」的分界：`tokenizer_manager` 是骨架，缺了就启动失败；其余字段缺了就降级，保证服务能起来。这与 `try_get_attr` 家族的 best-effort 设计一脉相承。

#### 4.3.4 代码实践

**实践目标**：用一张表把 `TokenizerInfo` 三个字段的「来源 → 回退链 → 失败处理」整理清楚。

**操作步骤**：

1. 通读 [src/lib.rs:94-139](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L94-L139)。
2. 填出下表（答案见下方）：

| 字段 | 来源对象 | 回退链 | 取不到时的处理 |
| --- | --- | --- | --- |
| `tokenizer_path` | ? | ? | ? |
| `tokenizer_mode` | ? | ? | ? |
| `context_len` | ? | ? | ? |

**参考答案**：

| 字段 | 来源 | 回退链 | 失败处理 |
| --- | --- | --- | --- |
| `tokenizer_path` | `server_args` / `tm` | `server_args.tokenizer_path` → `server_args.model_path` → `tm.model_path` | `warn`，置 `None`，禁用 Rust 分词器 |
| `tokenizer_mode` | `server_args` | 仅 `server_args.tokenizer_mode`（无回退） | 静默置 `None` |
| `context_len` | `tm.model_config` | 仅 `model_config.context_len`（无回退） | `warn`，兜底 `0` |

#### 4.3.5 小练习与答案

**练习 1**：为什么 `extract_tokenizer_info` 要在 `start_server` 里**先于**「起 OS 线程」调用？

**答案**：因为它需要持 GIL 访问 Python 对象，而 `start_server` 调用栈本身就在 Python 里、天然有 GIL；一旦把服务丢进独立 OS 线程，再想访问 Python 对象就得跨线程 `Python::with_gil` 重新抢锁，既慢又容易和 Python 主线程争用。所以把「只读一次」的信息提前摘成纯 Rust 结构 `TokenizerInfo`。

**练习 2**：如果 `runtime_handle` 根本没有 `tokenizer_manager` 属性，`start_server` 返回什么？

**答案**：返回 `Err(PyValueError)`，消息为 `"runtime_handle.tokenizer_manager is required: ..."`，整个 `start_server` 以 Python 异常的形式失败——这是有意为之，让「运行时句柄配置错误」尽早暴露在启动阶段。

---

### 4.4 start_server 总体流程与参数归一化

#### 4.4.1 概念说明

`start_server` 是整个 crate 对 Python 暴露的「总开关」。它做四件事：解析与绑定地址、归一化三个容量/超时参数、起 Tokio 运行时并构造 `PyBridge`、在独立 OS 线程里 `block_on` tonic 服务。本节先看它的**签名与默认值**、**参数归一化**和**端口绑定**三块；运行时与线程留在 4.5。

#### 4.4.2 核心流程

```text
start_server(host, port, runtime_handle, worker_threads=4,
             response_channel_capacity=64, response_timeout_secs=300)
   │
   ① 初始化 tracing（best-effort，失败不管）
   ② 解析 "host:port" → SocketAddr
   ③ 归一化：
        worker_threads = max(worker_threads, 1)
        response_channel_capacity: 0 → 默认 64（并 warn）
        response_timeout_secs:     0 → 默认 300（并 warn）
   ④ 标准库 TcpListener::bind(addr)  ← 端口占用错误在这里尽早暴露
      listener.set_nonblocking(true)
   ⑤ extract_tokenizer_info(runtime_handle)  (见 4.3)
   ⑥ RustTokenizer::from_tokenizer_path(...)  (可选)
   ⑦ 建 Tokio multi-thread 运行时，clone handle
   ⑧ 构造 PyBridge、构造 Notify、spawn OS 线程 block_on(run_grpc_server)
   ⑨ 返回 GrpcServerHandle
```

#### 4.4.3 源码精读

签名与默认值由 `#[pyo3(signature = ...)]` 声明，[src/lib.rs:150-159](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L150-L159)：

```rust
#[pyfunction]
#[pyo3(signature = (host, port, runtime_handle, worker_threads=4, response_channel_capacity=64, response_timeout_secs=300))]
fn start_server(
    host: String,
    port: u16,
    runtime_handle: PyObject,
    worker_threads: usize,
    response_channel_capacity: usize,
    response_timeout_secs: u64,
) -> PyResult<GrpcServerHandle> {
```

Python 侧因此可以 `_core.start_server("0.0.0.0", 40000, rt)`，后三个参数自动取 4 / 64 / 300。返回 `PyResult<GrpcServerHandle>` 表示「成功返回句柄，失败抛 Python 异常」。

参数归一化（下限保护），[src/lib.rs:170-189](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L170-L189)：

```rust
let worker_threads = worker_threads.max(1);
let response_channel_capacity = if response_channel_capacity == 0 {
    tracing::warn!(default = DEFAULT_RESPONSE_CHANNEL_CAPACITY,
        "response_channel_capacity must be positive; using default");
    DEFAULT_RESPONSE_CHANNEL_CAPACITY
} else {
    response_channel_capacity
};
let response_timeout_secs = if response_timeout_secs == 0 {
    tracing::warn!(default = server::DEFAULT_RESPONSE_TIMEOUT_SECS,
        "response_timeout_secs must be positive; using default");
    server::DEFAULT_RESPONSE_TIMEOUT_SECS
} else {
    response_timeout_secs
};
let response_timeout = Duration::from_secs(response_timeout_secs);
```

三个归一化规则各有理由：

- `worker_threads.max(1)`：运行时至少要有一个 worker，否则 `async` 任务无人执行。
- `response_channel_capacity == 0` 不允许：通道容量为 0 意味着每个 chunk 都要同步握手，背压逻辑（u3-l1）会失效，于是回退到 `DEFAULT_RESPONSE_CHANNEL_CAPACITY`（[src/bridge.rs:35](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L35) 定义为 `64`）。
- `response_timeout_secs == 0` 不允许：0 秒超时会让所有请求立刻超时，回退到 `DEFAULT_RESPONSE_TIMEOUT_SECS`（[src/server.rs:27](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L27) 定义为 `300`）。

> 注意 `PyBridge::new` 里有一条 `debug_assert!(response_channel_capacity > 0, ...)`（[src/bridge.rs:102-105](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L102-L105)），注释明说「由 start_server 归一化」。这就是 4.4 与 4.5 之间的契约：归一化在前，构造在后。

端口绑定用标准库 `TcpListener`，[src/lib.rs:190-201](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L190-L201)：

```rust
let listener = TcpListener::bind(addr).map_err(|e| {
    pyo3::exceptions::PyRuntimeError::new_err(format!(
        "Failed to bind gRPC server to {}: {}", addr, e))
})?;
listener.set_nonblocking(true).map_err(|e| {
    pyo3::exceptions::PyRuntimeError::new_err(format!(
        "Failed to configure gRPC listener for {}: {}", addr, e))
})?;
```

> **为什么用标准库 `TcpListener` 而不是 Tokio 的？** 见本节代码实践。

#### 4.4.4 代码实践

**实践目标**：讲清「标准库 `TcpListener::bind` + `set_nonblocking(true)`，再交给 Tokio」这一设计选择的两层动机。

**操作步骤**：

1. 阅读绑定代码 [src/lib.rs:190-201](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L190-L201)。
2. 阅读消费侧 `run_grpc_server` 里把标准库 listener 转成 Tokio listener 的 `from_std`，[src/server.rs:984-985](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L984-L985)。
3. 列出全部参数及其默认值（来自 signature 宏）。

**需要观察/思考的现象**（两件事）：

- **端口占用错误时机**：`TcpListener::bind` 是**同步**调用，发生在 `start_server` 主流程里、**在** `std::thread::spawn` **之前**。所以「端口被占用」会立刻以 `PyRuntimeError` 抛回 Python，而不是等到后台线程起来才发现。如果改成在后台线程里用 Tokio 绑定，错误就只能 `tracing::error!` 记进日志、Python 侧拿到一个「看起来启动成功但其实没在监听」的句柄——非常难排查。
- **`set_nonblocking(true)` 与 `from_std` 的关系**：Tokio 的 `TcpListener::from_std` 要求传入的是一个**非阻塞** socket（否则会 panic / 行为异常）。标准库 `bind` 默认是阻塞的，所以必须显式 `set_nonblocking(true)` 再转交。

**预期结果（参数清单）**：

| 参数 | 类型 | 默认值 | 含义 |
| --- | --- | --- | --- |
| `host` | `String` | （必填） | 绑定地址，如 `"0.0.0.0"` |
| `port` | `u16` | （必填） | 端口，如 `40000` |
| `runtime_handle` | `PyObject` | （必填） | Python 运行时句柄 |
| `worker_threads` | `usize` | `4` | Tokio 工作线程数（下限 1） |
| `response_channel_capacity` | `usize` | `64` | 每请求响应通道容量（0 回退 64） |
| `response_timeout_secs` | `u64` | `300` | 单 chunk 等待超时秒数（0 回退 300） |

#### 4.4.5 小练习与答案

**练习 1**：把 `response_channel_capacity` 传成 `0`，运行时实际用的是哪个值？为什么？

**答案**：实际用 `DEFAULT_RESPONSE_CHANNEL_CAPACITY`，即 `64`（[src/bridge.rs:35](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L35)）。因为容量为 0 会让背压通道失去缓冲意义，代码显式把它视为非法并回退默认值，同时 `warn` 提示。

**练习 2**：`worker_threads` 传 `0` 会怎样？

**答案**：被 `worker_threads.max(1)` 归一化成 `1`。注意它**没有**像另外两个参数那样 `warn`——因为「至少 1 个线程」是静默保证，不算异常输入。

---

### 4.5 起 Tokio 运行时、构造 PyBridge、后台线程拉起服务

#### 4.5.1 概念说明

参数和端口都就绪后，进入启动的最后一段：建一个**多线程 Tokio 运行时**，把它的 `handle` clone 一份交给 `PyBridge`（桥接器需要在任意线程上 spawn 异步发送任务，见 u3-l1 背压）；然后构造一个 `Notify` 关停信号，**clone** 出 `shutdown` / `bridge` 两份，把它们 move 进一个**独立 OS 线程**，在线程里 `rt.block_on(run_grpc_server(...))`。

关键设计：Tokio 运行时 `rt` 必须在「持有它的那个线程」上 `block_on`。所以服务线程既拥有 `rt`，又负责驱动它。返回给 Python 的 `GrpcServerHandle` 不持有 `rt`，只持有 `Notify` 和该线程的 `JoinHandle`——通过信号而非运行时句柄来控制生命周期。

#### 4.5.2 核心流程

```text
       主线程（start_server，持 GIL）
            │
   建 rt = multi_thread(worker_threads)        ──┐
   tokio_handle = rt.handle().clone()             │ clone 后分两路
            │                                     │
   bridge = Arc::new(PyBridge::new(               │
       runtime_handle, rust_tokenizer,            │
       context_len, channel_cap, tokio_handle)) ◄─┘ bridge 持有 handle
            │
   shutdown = Arc::new(Notify::new())
            │
   std::thread::spawn("sglang-grpc", move || {
        rt.block_on(run_grpc_server(            ◄── rt 必须在拥有它的线程 block_on
            listener, bridge_clone,
            shutdown_clone, response_timeout))
   })  ── 返回 JoinHandle
            │
   返回 GrpcServerHandle { shutdown, join_handle }
```

#### 4.5.3 源码精读

建运行时并取 handle，[src/lib.rs:213-224](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L213-L224)：

```rust
let rt = tokio::runtime::Builder::new_multi_thread()
    .worker_threads(worker_threads)
    .enable_all()
    .thread_name("sglang-grpc-tokio")
    .build()
    .map_err(|err| /* PyRuntimeError */)?;
let tokio_handle = rt.handle().clone();
```

- `enable_all()` 开启 IO 与时间驱动（tonic 需要这两者）。
- `thread_name("sglang-grpc-tokio")` 让 Tokio worker 线程在调试器/日志里有清晰名字。
- `rt.handle().clone()` 得到一个可跨线程移动的句柄；`rt` 本身留在主线程，准备 move 进服务线程。

构造 `PyBridge`（用 `Arc` 包，因为要 clone 给服务线程），[src/lib.rs:226-232](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L226-L232)：

```rust
let bridge = Arc::new(PyBridge::new(
    runtime_handle,
    rust_tokenizer,
    tokenizer_info.context_len,
    response_channel_capacity,
    tokio_handle,    // ← 桥接器据此在通道满时 spawn 异步发送
));
```

`PyBridge` 持有的字段印证了它为何需要 `tokio_handle`，[src/bridge.rs:84-92](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L84-L92)（其中 `tokio_handle: Handle`）。

拉起服务线程，[src/lib.rs:233-256](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L233-L256)：

```rust
let shutdown = Arc::new(Notify::new());
let shutdown_clone = shutdown.clone();
let bridge_clone = bridge.clone();

let join_handle = std::thread::Builder::new()
    .name("sglang-grpc".to_string())
    .spawn(move || {
        if let Err(e) = rt.block_on(server::run_grpc_server(
            listener, bridge_clone, shutdown_clone, response_timeout,
        )) {
            tracing::error!("gRPC server exited with error: {}", e);
        }
    })
    .map_err(|e| /* PyRuntimeError */)?;

Ok(GrpcServerHandle { shutdown, join_handle: Some(join_handle) })
```

要点：

- 服务跑在**独立的 OS 线程**（名字 `sglang-grpc`），与 Python 主线程、Tokio worker 线程都区分开。这样 Python 主线程不会被 tonic 的 `block_on` 卡住。
- `rt` 被 `move` 进服务线程并在那里 `block_on`——这是 Tokio 的硬性要求：运行时要在创建它的所有权线程上驱动。
- `run_grpc_server` 的错误用 `tracing::error!` 记录后丢弃（对应 4.1 里 `shutdown()` 对 `join()` 返回值的 `let _ =`）。
- 返回的 `GrpcServerHandle` 持有原始的 `shutdown`（不是 clone）和 `join_handle`，于是 Python 侧 `shutdown()` 的 `notify_one()` 能唤醒服务线程里 `shutdown_clone.notified().await`（[src/server.rs:1000-1003](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L1000-L1003)）。

`run_grpc_server` 接收的就是这份标准库 listener 并就地转成 Tokio listener，[src/server.rs:978-989](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L978-L989)：

```rust
pub async fn run_grpc_server(
    listener: std::net::TcpListener,
    bridge: Arc<PyBridge>,
    shutdown: Arc<Notify>,
    response_timeout: Duration,
) -> Result<(), Box<dyn std::error::Error + Send + Sync>> {
    let addr = listener.local_addr()?;
    let listener = tokio::net::TcpListener::from_std(listener)?;   // ← 非阻塞 socket 转 Tokio
    let service = SglangServiceImpl { bridge, response_timeout };
    ...
}
```

#### 4.5.4 代码实践

**实践目标**：验证「Tokio 运行时必须由持有它的线程 block_on」这一约束，并理解为何把 `rt` move 进服务线程。

**操作步骤**：

1. 在 [src/lib.rs:213-224](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L213-L224) 找到 `rt` 的创建处，确认它**没有**被 clone，而是被整体 `move` 进 [src/lib.rs:239-248](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L239-L248) 的闭包。
2. 思考一个反例：假如不 spawn 新线程，直接在 `start_server` 主线程里 `rt.block_on(run_grpc_server(...))`，会发生什么？
3. 追踪 `tokio_handle` 的去向：它被 clone 给 `PyBridge`（[src/lib.rs:226-232](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L226-L232)），最终用在 `try_send_chunk` 的通道满分支（[src/bridge.rs:562](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L562) 的 `tokio_handle.spawn(...)`）。

**需要观察/思考的现象**：

- 反例答案：`start_server` 是被 Python 同步调用的，主线程持 GIL。若在主线程 `block_on`，会**同时**长时间占用 GIL 和主线程，Python 解释器在此期间无法继续执行任何代码（包括将来想调 `handle.shutdown()` 都进不来）。所以必须把 `block_on` 推进独立 OS 线程，立刻把控制权还给 Python。
- `tokio_handle` 的用途：桥接器在「通道满」时不能阻塞 Python 回调线程，于是用 `tokio_handle.spawn(async move { sender.send(msg).await ... })` 把这次发送挂到 Tokio 运行时上异步完成（背压停泊，u3-l1 详讲）。没有这份 handle，回调线程就无处 spawn。

**预期结果**：能说清「三个线程各司其职」——Python 主线程（发起 `start_server` 并持有 GIL）、`sglang-grpc` OS 线程（拥有并 `block_on` 运行时）、`sglang-grpc-tokio` worker 线程池（实际跑 tonic handler 与 spawn 的发送任务）。待本地验证（可在各线程名上用 `htop`/调试器观察）。

#### 4.5.5 小练习与答案

**练习 1**：为什么返回的 `GrpcServerHandle` 不直接持有 `rt`（运行时本身），而是只持有 `Notify` 和 `JoinHandle`？

**答案**：因为 `rt` 已经被 `move` 进服务线程并在那里 `block_on`，所有权属于那个线程；Rust 的所有权规则也不允许同时让两处持有 `rt`。关停不靠「销毁运行时」，而靠 `Notify` 信号触发 tonic 的 `serve_with_incoming_shutdown` 优雅退出，再靠 `JoinHandle::join()` 等线程（连同它拥有的 `rt`）自然结束。这样既满足所有权约束，又能优雅排空连接。

**练习 2**：`bridge` 和 `shutdown` 为什么都要 `.clone()` 一份再 move 进闭包？

**答案**：因为原始的 `bridge`、`shutdown` 要留给 `GrpcServerHandle` 返回给 Python（Python 侧 `shutdown()` 要用同一份 `Notify` 来唤醒服务线程）。它们都是 `Arc`，clone 只是增加引用计数、共享同一份底层数据。闭包拿 clone、句柄拿原始，两端指向同一个 `PyBridge` 和同一个 `Notify`。

---

## 5. 综合实践

把本讲串起来，做一次「启动时序复盘」：

**任务**：假设你要给团队写一份「gRPC 服务启动 checklist」，请基于 `start_server` 的源码，按真实执行顺序写出每一步、它在哪个线程发生、是否持 GIL、失败会怎样。要求覆盖下列检查点：

1. tracing 初始化（best-effort）。
2. 地址解析失败 → 何种异常？
3. 三个参数的归一化（各自规则与默认值来源）。
4. 端口绑定失败的异常类型，以及为什么能在启动期就暴露。
5. `set_nonblocking(true)` 为何必要。
6. `extract_tokenizer_info` 中哪一步会致命、哪些步会降级。
7. Rust 分词器在什么条件下不会被创建（`tokenizer_path` 为 `None`）。
8. Tokio 运行时的线程名、worker 数下限。
9. `PyBridge` 持有 `tokio_handle` 的用途。
10. 服务线程的名字、它如何 `block_on`、错误如何被记录。
11. `GrpcServerHandle.shutdown()` 的两步动作顺序，以及 `is_alive()` 的判定依据。

**建议产出**：一张表格，列为「步骤 / 所在线程 / 是否持 GIL / 失败处理」。完成后，对照 [src/lib.rs:150-257](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L150-L257) 逐行校验。这份表会是你阅读 u2（服务实现与桥接）、u3（背压与关停）时的「定位地图」。

## 6. 本讲小结

- `start_server` 是 crate 对 Python 暴露的唯一启动入口，通过 `#[pyo3(signature)]` 提供 `worker_threads=4` / `response_channel_capacity=64` / `response_timeout_secs=300` 三个默认值。
- 三个容量/超时参数都有下限归一化：worker 至少 1；通道容量与超时秒数为 0 时分别回退到 `DEFAULT_RESPONSE_CHANNEL_CAPACITY`(64) 与 `DEFAULT_RESPONSE_TIMEOUT_SECS`(300)。
- 端口用标准库 `TcpListener::bind` **同步**绑定，使「端口占用」在启动期就抛 `PyRuntimeError`；随后 `set_nonblocking(true)` 以满足 Tokio `from_std` 的要求。
- `extract_tokenizer_info` 一次性持 GIL 把 `tokenizer_path`/`tokenizer_mode`/`context_len` 摘成纯 Rust `TokenizerInfo`，仅 `tokenizer_manager` 缺失会致命，其余字段 best-effort 降级。
- 服务跑在名为 `sglang-grpc` 的独立 OS 线程里 `block_on` 一个多线程 Tokio 运行时（worker 线程名 `sglang-grpc-tokio`），主线程立即把控制权还给 Python。
- `GrpcServerHandle` 用 `Arc<Notify>` + `Option<JoinHandle>` 实现生命周期控制：`shutdown()` 先 `notify_one()` 后 `join()`，`is_alive()` 看 `JoinHandle` 是否仍在且未结束。

## 7. 下一步学习建议

- 接下来进入 **u2-l1（proto 契约与 tonic 代码生成）**：本讲的 `run_grpc_server` 只是「拉起服务」，要看它服务的对象（`SglangServiceImpl` 实现 trait）长什么样，就得先读 proto 契约。
- 若想立刻看清 `bridge_clone` 与 `shutdown_clone` 的消费侧，可直接跳读 [src/server.rs:978-1007](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L978-L1007) 的 `run_grpc_server` 全文，作为 u2-l2 的预热。
- 想深入 `tokio_handle` 在背压里的用法，可提前扫一眼 [src/bridge.rs:562](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L562)，那是 **u3-l1（背压与停泊）** 的核心，本讲的 `tokio_handle.clone()` 正是为此埋下的伏笔。
