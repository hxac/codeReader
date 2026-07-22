# Tokio 运行时与 GIL 协作模型

## 1. 本讲目标

本讲是专家层（u3）的第五篇，聚焦 `sglang-grpc` 最底层的一条线索：**一个 Rust 异步服务（Tokio + tonic）和一个 CPython 解释器（持有 GIL）同处一个进程，它们如何并发协作而不互相卡死**。读完本讲，你应当能够：

- 说清 `start_server` 为什么要把 Tokio 运行时建在一条独立的 OS 线程里，以及「运行时（Runtime）」「句柄（Handle）」「承载线程」三者之间的关系。
- 理解 `tokio_handle` 为什么被 `clone` 出来、装进 `PyBridge`、再装进每一个 chunk 回调，并追踪到它在 `try_send_chunk` 通道满分支里的关键用途。
- 在 `server.rs` 里枚举出所有 `tokio::task::spawn_blocking` 调用点，并能解释「为什么不能直接在 `async fn` 里用 `Python::with_gil` 同步阻塞」。
- 区分 `Python::with_gil` 的三种使用时机（启动期一次性、`spawn_blocking` 闭包内、被 `spawn` 出来的异步任务内），并理解 `submit_request` 这种「内联拿 GIL」的取舍。

本讲只讲**并发模型与线程/GIL 协作**，不展开具体 RPC 业务（u2 主题）、背压停泊细节（u3-l1）、中止传播（u3-l2）、错误码映射（u3-l3）和服务引导/关停（u3-l4）。

## 2. 前置知识

本讲承接 **u1-l4**（`start_server` 与 `GrpcServerHandle` 生命周期，那里已点出「Python 主线程 / `sglang-grpc` 服务线程 / `sglang-grpc-tokio` worker 池」三线程分工）与 **u2-l5**（`PyBridge` 与请求通道架构）。本讲只补充几个前序讲义未细讲、但本讲必备的概念。

- **Tokio 运行时（Runtime）**：Tokio 是 Rust 生态最主流的异步运行时。一个 `Runtime` 对象内部包含线程池、IO 多路复用驱动、定时器驱动等。只有当某个线程「驱动」它（典型方式是调用 `rt.block_on(future)`）时，跑在上面的异步任务才会推进。`Runtime` 是**拥有型**句柄——它被 drop 时运行时会关闭。
- **多线程运行时 vs 当前线程运行时**：`new_multi_thread()` 建出的运行时有一组 worker 线程并行轮询任务（真正的并发）；`new_current_thread()` 只在调用线程上轮询（并发靠 `spawn`，但同一时刻只跑一个任务）。gRPC 服务要并发处理大量连接，故本 crate 选 `new_multi_thread`。
- **运行时句柄 `Handle`**：`rt.handle()` 返回一个**引用计数**的轻量句柄，指向运行时。`Handle::clone` 只是增加引用计数，**不会**创建新运行时。持有 `Handle` 就能从任意线程向运行时提交任务（`spawn` / `spawn_blocking`）。
- **GIL（全局解释器锁）**：CPython 用一把全局锁保证同一时刻只有一个 OS 线程执行 Python 字节码。Rust 代码要调用 Python 对象（`runtime_handle.submit_request(...)` 等），必须先「拿 GIL」。PyO3 提供 `Python::with_gil(|py| { ... })`：它**同步阻塞**当前线程直到拿到 GIL，再执行闭包，最后释放。`with_gil` 是一个**可能长时间阻塞**的同步调用。
- **`spawn_blocking`**：Tokio 提供的「把同步阻塞活儿挪到专用阻塞线程池」的接口。调用 `spawn_blocking(closure)` 会把 `closure` 扔到一个独立于异步 worker 的线程池上跑，立刻返回一个 `JoinHandle`；`.await` 它只在阻塞线程跑完时才就绪。这样异步 worker 线程就不会被阻塞调用卡住。

> 一句话直觉：**异步 worker 线程是「贵」且数量少的并发资源，绝不能让它站着等 GIL；GIL 是「一把全局锁」，跨进/出 Python 时要快进快出。** 本讲全部设计都围绕这条直觉。

## 3. 本讲源码地图

本讲涉及三个源文件：

| 文件 | 作用 |
| --- | --- |
| `rust/sglang-grpc/src/lib.rs` | `start_server`：构建 Tokio 运行时、`clone` 句柄、在独立线程 `block_on` 启动服务；`extract_tokenizer_info` 是启动期一次性拿 GIL 的代表。 |
| `rust/sglang-grpc/src/server.rs` | RPC 实现：6 处 `tokio::task::spawn_blocking` 调用点、`spawn_abort` 的 `Handle::try_current` 防御式写法、`pyerr_to_status` 的内联 GIL。 |
| `rust/sglang-grpc/src/bridge.rs` | `PyBridge`（持有 `tokio_handle`）、两个 chunk 回调（各持一份 `tokio_handle`）、`try_send_chunk` 在通道满时 `tokio_handle.spawn` 异步重发。 |

线程与运行时的总体关系（本讲的核心图）：

```text
Python 进程
│
├─ Python 主线程 ──start_server()──▶ 必须很快返回 GrpcServerHandle
│                                        （所以运行时要交给后台线程）
│
├─ OS 线程 "sglang-grpc"            ── 拥有 Runtime rt，rt.block_on(run_grpc_server(...))
│                                        （驱动顶层 accept future）
│
└─ Runtime rt（多线程）
     ├─ worker 池 "sglang-grpc-tokio"（worker_threads 个，默认 4）
     │      └─ 跑 tonic 为每条连接/RPC spawn 出来的任务、async_stream 流
     │
     └─ 阻塞线程池（默认上限 512）
            └─ 跑 spawn_blocking 闭包 ← 绝大多数「拿 GIL 调 Python」的活儿在这里
```

## 4. 核心概念与源码讲解

### 4.1 模块一：Tokio 运行时的构建与承载线程

#### 4.1.1 概念说明

一个 Tokio 多线程运行时不是「自己会跑」的——它必须有**至少一个线程**去驱动它。最常见的驱动方式就是 `rt.block_on(future)`：该线程会持续轮询 `future`，直到它完成。同时，运行时内部还会另外 spawn 出 `worker_threads` 个 worker 线程，并行轮询通过 `tokio::spawn` 提交的任务。

为什么 `sglang-grpc` 要把运行时建在**一条独立 OS 线程**里，而不是直接在 Python 主线程上 `block_on`？三个原因：

1. **`block_on` 是阻塞调用**。Python 调用 `start_server` 后要尽快拿到 `GrpcServerHandle` 并继续往后走（启动 HTTP server、注册生命周期钩子等）。若在主线程 `block_on`，Python 就被永远卡住，永远返回不了。
2. **`Runtime` 是拥有型对象**。谁拥有 `rt`、谁活得久，运行时就活得久。把 `rt` move 进一条专门的后台线程，让那条线程「活着 = 运行时活着」，生命周期最清晰。
3. **隔离**。Tokio 的线程模型（worker 池 + 阻塞池）与 SGLang 自己的 Python 线程（调度器线程、TokenizerManager 线程等）互不干扰，便于在 `top`/`py-spy` 里按线程名区分。

#### 4.1.2 核心流程

`start_server` 里运行时相关的流程（地址解析、参数归一化等已在 u1-l4 讲过，这里只看运行时部分）：

```text
1. extract_tokenizer_info(&runtime_handle)        ← 启动期一次性拿 GIL（见 4.4）
2. 构造 RustTokenizer（best-effort，可能为 None）
3. let rt = Builder::new_multi_thread()
              .worker_threads(worker_threads)     ← 默认 4，已 max(1)
              .enable_all()                       ← 开启 IO / 定时器 / 信号驱动
              .thread_name("sglang-grpc-tokio")   ← 给 worker 线程命名
              .build()?;
4. let tokio_handle = rt.handle().clone();        ← 引用计数 clone，交给 PyBridge
5. let bridge = Arc::new(PyBridge::new(..., tokio_handle));
6. std::thread::Builder::new().name("sglang-grpc").spawn(move || {
       rt.block_on(run_grpc_server(...));         ← rt 被 move 进来，由本线程驱动
   });
7. 返回 GrpcServerHandle { shutdown, join_handle }
```

要点：第 4 步 `clone` 出来的 `Handle` 让 `PyBridge`（及其回调）即便在**运行时之外**的线程上（比如 Python 回调线程），也能向运行时提交任务。第 6 步把 `rt` move 进线程闭包，是「谁拥有运行时」的交代。

一个常被忽略的细节：`block_on` 驱动的是**顶层 future**（`run_grpc_server`，即 tonic 的 accept 主循环），它跑在 `sglang-grpc` 这条线程上；而 tonic 每接到一条新连接/RPC，会通过 `tokio::spawn` 把处理任务提交到 worker 池。也就是说，**具体的 RPC `async fn`（`text_generate`/`health_check` 等）绝大多数时刻是跑在 `sglang-grpc-tokio` worker 线程上的**，不是 `sglang-grpc` 那条线程。

#### 4.1.3 源码精读

构建多线程运行时（关键四参数：worker 数、全功能驱动、线程名、构建）：

[创建并配置 Tokio 多线程运行时 — `rust/sglang-grpc/src/lib.rs:213-217`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L213-L217)：`new_multi_thread()` + `.worker_threads(worker_threads)` + `.enable_all()` + `.thread_name("sglang-grpc-tokio")`。`.enable_all()` 至关重要——它同时开启 IO 驱动（tonic 的 TCP/HTTP2）、时间驱动（`server.rs` 里大量使用的 `tokio::time::timeout`）和信号驱动。

[`.build()` 并把可能的构造失败转成 `PyRuntimeError` — `rust/sglang-grpc/src/lib.rs:218-223`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L218-L223)：运行时构建失败（如系统资源不足）在启动期直接抛给 Python，避免带着半残状态继续。

[clone 句柄并交给 `PyBridge` — `rust/sglang-grpc/src/lib.rs:224-232`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L224-L232)：第 224 行 `rt.handle().clone()` 拿到轻量引用计数句柄；它和 `rt` 一起进入 `PyBridge::new`。注意 `rt` 本身还留在当前（Python 调用）线程的栈上，等下一步 move 进后台线程。

[在独立 OS 线程里 `block_on` 启动服务 — `rust/sglang-grpc/src/lib.rs:237-251`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L237-L251)：`std::thread::Builder::new().name("sglang-grpc")` spawn 一条线程，`rt` 被 `move` 进去并 `block_on(server::run_grpc_server(...))`。这条线程既「拥有」运行时，又驱动顶层 future。`start_server` 在 `spawn` 返回后立刻构造 `GrpcServerHandle` 返回给 Python，不再阻塞。

#### 4.1.4 代码实践

**实践目标**：从日志/源码层面确认「三组线程」的存在与命名，建立直观印象。

**操作步骤**：

1. 打开 `rust/sglang-grpc/src/lib.rs`，确认第 216 行 worker 线程名为 `"sglang-grpc-tokio"`、第 238 行承载线程名为 `"sglang-grpc"`。
2. 设想在真实运行中，`run_grpc_server` 里的 `tracing::info!("gRPC server listening on {}", addr);`（见 u3-l4）会打印在 `sglang-grpc` 线程上；而每条 RPC 的 `async fn` 调用栈会落在 `sglang-grpc-tokio` 线程上。
3. 若本地可运行：启动一个挂了 sglang-grpc 的服务后，用 `top -H -p <pid>` 或 `py-spy dump --pid <pid>` 观察线程名，应能看到形如 `sglang-grpc`、`sglang-grpc-tokio`、`sglang-grpc-tokio`（多份）的线程。

**需要观察的现象**：是否存在一个 `sglang-grpc` 线程（承载 `block_on`）和多个 `sglang-grpc-tokio` 线程（worker 池，数量等于 `worker_threads`，默认 4）。

**预期结果**：线程组结构与「3. 本讲源码地图」里的图一致。若本地无法运行，此项**待本地验证**，但源码层面三组线程的来源已可确认。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `.enable_all()` 删掉，运行时还能正常驱动 tonic 服务吗？为什么？

> **答案**：不能正常工作。`.enable_all()` 等价于同时开启 IO、时间、信号驱动。不开 IO 驱动，tonic 的 TCP/HTTP2 收发无处推进；不开时间驱动，`server.rs` 里 `recv_chunk_with_timeout` 使用的 `tokio::time::timeout` 会 panic（「time driver 未启用」）。Tokio 会在调用这些功能时直接报错或 panic。

**练习 2**：为什么是 `rt.handle().clone()` 而不是把整个 `rt` 也 clone 一份给 `PyBridge`？

> **答案**：`Runtime` 没有「廉价 clone」语义——它是拥有型对象，只能有一个所有者。而 `Handle` 是引用计数的轻量句柄，`clone` 只增加引用计数，用于「从外部线程向运行时提交任务」。把 `rt` move 进后台线程保留唯一所有权，把 `Handle` clone 出去共享「提交能力」，是标准分工。

---

### 4.2 模块二：tokio_handle 的克隆与跨结构共享

#### 4.2.1 概念说明

回忆 u2-l6：每个 chunk 回调（`ChunkCallback` / `JsonChunkCallback`）是 PyO3 `#[pyclass]` 对象，被 Python 端**在持有 GIL 的 Python 线程上**反复 `__call__`，用来把生成结果一段段推回 Rust。也就是说——**回调执行时，它跑在 Python 线程上、持有 GIL，而不是跑在 Tokio worker 线程上**。

这就带来一个关键约束：回调内部**不能 `.await`**（它不是 async 函数，且持着 GIL 时更不能让出去等）。但 u3-l1 讲过，当客户端消费慢、mpsc 通道满时，`try_send_chunk` 需要把这块 chunk「异步重发」——这需要 `sender.send(msg).await`，一个异步操作。

如何「在一条没有运行时上下文的 Python 线程上，发起一个异步操作」？答案就是：**手里攥着运行时的 `Handle`，调用 `handle.spawn(async { ... })`**，把这个异步任务提交回运行时，由 worker 池去跑。回调自己立刻返回，把 GIL 还给 Python。

所以 `tokio_handle` 必须：

- 在 `start_server` 里 `clone` 出来；
- 装进 `PyBridge`；
- 在构造每个回调时**再 clone 一份**装进回调对象。

`Handle` 内部是引用计数，clone 极廉价，因此「每个回调一份」没有任何性能顾虑。

#### 4.2.2 核心流程

`tokio_handle` 的克隆与共享链路：

```text
lib.rs::start_server
   rt.handle().clone()  ──────────────────▶ PyBridge::new(tokio_handle)
                                                   │
                                                   │ 存为 PyBridge.tokio_handle: Handle
                                                   │
              构造回调时 make_chunk_callback/make_json_callback
                                                   │
                                 self.tokio_handle.clone() ──▶ ChunkCallback.tokio_handle
                                                              JsonChunkCallback.tokio_handle
                                                                   │
                 回调 __call__ → try_send_chunk(..., &self.tokio_handle, ...)
                                                                   │
                            通道满(TrySendError::Full) 分支
                                   tokio_handle.spawn(async move { sender.send(msg).await; ... })
```

要点：`Handle` 的所有权随用随 `clone`，三处 clone（`start_server` 一次、两类回调各一次）都是为了「让一个不在运行时上下文里的对象，仍能把异步活儿交回运行时」。

#### 4.2.3 源码精读

[`PyBridge` 结构体持有 `tokio_handle: Handle` — `rust/sglang-grpc/src/bridge.rs:85-92`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L85-L92)：`PyBridge` 不是 `#[pyclass]`（它不直接暴露给 Python），而是一个普通 struct，用 `Arc` 共享。`tokio_handle` 与 `runtime_handle`（Python 对象）、`state`（共享账本）并列存放。

[`PyBridge::new` 接收并保存 `tokio_handle` — `rust/sglang-grpc/src/bridge.rs:95-114`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L95-L114)：构造时带一个 `debug_assert!` 确保 `response_channel_capacity > 0`（归一化由 `start_server` 保证，见 u1-l4），然后逐字段赋值。

[构造 dict 回调时 clone 句柄 — `rust/sglang-grpc/src/bridge.rs:147-156`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L147-L156)：`make_chunk_callback` 把 `self.tokio_handle.clone()` 塞进 `ChunkCallback`，连同 `rid`、共享账本 `state` 的 `Arc`、Python 句柄的引用克隆（`clone_ref`），一起 `Py::new` 成 Python 可调用对象。`make_json_callback`（`bridge.rs:158-167`）结构完全相同。

`try_send_chunk` 是这一切的「用武之地」。看通道满分支里的 `spawn`：

[通道满时用 `tokio_handle.spawn` 异步重发 — `rust/sglang-grpc/src/bridge.rs:562-592`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L562-L592)：在 `TrySendError::Full(msg)` 分支里，先 `register_pending_send` 占住「单停泊位」（详见 u3-l1），然后把 `sender`、`msg`、`state`、`runtime_handle` 全部 move 进一个 `async move { ... }`，交 `tokio_handle.spawn` 跑。闭包内 `sender.send(msg).await` 这一步会真正阻塞地等待通道有空位——但这是在 **worker 线程**上阻塞，而**不是**在持有 GIL 的回调线程上。回调线程在第 594 行 `return Ok(ChunkSendStatus::Pending)` 立刻脱手，Python 端拿到 `Pending` 后暂停生产，GIL 随即释放。

这就是 `PyBridge` 保存 `tokio_handle` 的根本用途：**让「跑在 Python 线程、持有 GIL」的回调，能把唯一一个必须 `.await` 的操作（慢通道发送）甩给运行时，自己立即返回。** 若没有这份 `tokio_handle`，回调要么死等（卡死 GIL），要么无处 spawn。

> 补充：`spawn` 出来的异步任务在 `sender.send().await` 成功后，会再 `Python::with_gil` 去 `mark_send_ready`/`notify_ready`（`bridge.rs:572-576`）或失败时 `close_channel_with_error`（`bridge.rs:579-589`）。这是「在异步任务里拿 GIL」的模式，详见 4.4 的「Pattern D」。

#### 4.2.4 代码实践

**实践目标**：把 `tokio_handle` 的 clone 链路走一遍，并解释它存在的必要性（对应规格里的「解释 PyBridge 构造时保存 tokio_handle 的用途」）。

**操作步骤**：

1. 在 `lib.rs:224` 确认 `tokio_handle` 从运行时 `clone` 出来。
2. 在 `bridge.rs:152` 与 `bridge.rs:163` 确认构造两类回调时各 `clone` 一次。
3. 在 `bridge.rs:562` 确认回调最终在通道满时用它 `spawn` 异步任务。
4. 追问：回调的 `__call__`（`bridge.rs:631` / `bridge.rs:719`）是不是 `async fn`？它执行时是否持有 GIL？

**需要观察的现象**：回调 `__call__` 是普通同步 `fn`，由 Python 在持 GIL 线程上调用；它内部调用同步的 `try_send_chunk`，而 `try_send_chunk` 在通道满时**没有**自己 `.await`，而是把 `.await` 交给 `tokio_handle.spawn` 出去的异步任务。

**预期结果**：能复述这条因果链——**因为回调在 Python 线程上持 GIL、不能 `.await`，所以它必须攥着 `Handle` 把异步发送甩给运行时；这正是 `PyBridge` 与每个回调都要存一份 `tokio_handle` 的原因。**

#### 4.2.5 小练习与答案

**练习 1**：`Handle::clone` 会不会复制整个运行时？多次 clone 会不会拖慢服务？

> **答案**：不会。`Handle` 内部是 `Arc` 引用计数，`clone` 只是原子地 +1，开销可忽略。无论 clone 多少份，指向的都是同一个运行时。

**练习 2**：假如把回调里的 `tokio_handle.spawn(...)` 改成直接 `sender.send(msg).await`，会发生什么？

> **答案**：编译就过不了——回调 `__call__` 不是 `async fn`，不能 `.await`。即便强行改成 async，它也是被 Python 同步调用的，`.await` 期间会一直霸占 GIL，等客户端慢慢消费，等于用一把全局 Python 锁去等一个网络慢客户端，整个进程的 Python 执行都被拖住。这正是「必须有 Handle 来 spawn」的根本动机。

---

### 4.3 模块三：spawn_blocking 与 GIL 阻塞调用的隔离

#### 4.3.1 概念说明

`Python::with_gil(|py| { ... })` 是一个**同步阻塞**调用：当前线程会站着等，直到拿到 GIL 并把闭包跑完。如果这段 Python 代码本身又做了点实事（比如 `health_check` 要真去查调度器状态、`get_model_info` 要序列化模型配置），那这次 `with_gil` 可能阻塞「毫秒级到秒级」。

问题来了：如果在一个 tonic 的 `async fn`（跑在 `sglang-grpc-tokio` worker 线程上）里**直接**写 `Python::with_gil(...)`，会发生什么？

1. **白白占用一个异步 worker**。worker 池默认只有 `worker_threads`（4）个，一个被 GIL 卡住，服务整体的异步并发能力就少一份。设 \(W\) 为 worker 数、\(b\) 为当前被同步阻塞占住的 worker 数，则有效异步并发约为：

   \[
   W_{\text{eff}} \approx W - b
   \]

   若多个 RPC 同时这么干，\(b\) 上升，\(W_{\text{eff}}\) 趋近 0，服务对其它请求的响应就开始抖动。

2. **潜在死锁**。SGLang 的 Python 侧（调度器等）可能在某些路径上反过来等待一个运行在 Tokio 上的任务完成。如果那个任务恰好排在「现在正被 GIL 阻塞的 worker」上，就形成循环等待——死锁。

3. **饿死定时器/IO 轮询**。worker 线程同时也是 IO 与定时器的轮询者之一；被阻塞久了，超时（`tokio::time::timeout`）和连接读写会延迟触发。

解药就是 `tokio::task::spawn_blocking`：它把闭包扔到 Tokio 的**独立阻塞线程池**（默认最多 512 个线程，带 keep-alive）上执行，`async fn` 这边只 `.await` 一个轻量 `JoinHandle`，worker 线程立刻被释放去服务别的 RPC。因为阻塞活在专门的池子里跑，\(b\) 守在 0 附近，\(W_{\text{eff}} \approx W\)。

#### 4.3.2 核心流程

`server.rs` 里所有「可能慢的 Python 调用」都套了同一个模板：

```text
async fn some_rpc(&self, req) -> Result<...> {
    let result = tokio::task::spawn_blocking({
        let bridge = self.bridge.clone();   // Arc，廉价 clone 进闭包
        move || bridge.some_py_method(...)   // 内部 Python::with_gil(...)
    })
    .await                                    // ① 等 JoinHandle（阻塞池线程跑完）
    .map_err(|e| Status::internal(...))?      // ① 处理 JoinError（panic/取消）
    .map_err(|e| pyerr_to_status(e, ...))?;   // ② 处理内部 PyErr（→ gRPC Status）
    // 解析 result（多为 JSON 字符串）并组装 proto 响应
}
```

两处要点：

- **`.await??` 双问号**：第一个 `?` 处理 `JoinError`（阻塞任务 panic 或被取消），映射成 `Status::internal("Task join error: ...")`；第二个 `?` 处理闭包返回的 `PyErr`，经 `pyerr_to_status` 映射成 `INVALID_ARGUMENT` 或 `INTERNAL`（详见 u3-l3）。
- **`bridge.clone()`**：`Arc<PyBridge>` 的 clone 只增加引用计数，把 `bridge` move 进阻塞线程，不违反 `Send`。

`server.rs` 的 6 个 `tokio::task::spawn_blocking` 调用点（全部是「可能慢的 Python 调用」）：

| RPC | spawn_blocking 位置 | 回调的 bridge 方法 |
| --- | --- | --- |
| `tokenize`（Python 回退） | `server.rs:496` | `bridge.tokenize_py(...)` |
| `detokenize`（Python 回退） | `server.rs:541` | `bridge.detokenize_py(...)` |
| `health_check` | `server.rs:563` | `bridge.health_check()` |
| `get_model_info` | `server.rs:578` | `bridge.get_model_info()` |
| `get_server_info` | `server.rs:596` | `bridge.get_server_info()` |
| `list_models` | `server.rs:611` | `bridge.list_models()` |

此外还有一处「同族但写法略不同」的阻塞调用——`spawn_abort`：它没有用 `tokio::task::spawn_blocking`，而是用 `Handle::try_current().spawn_blocking(...)`。原因见 4.3.3 最后一段。

#### 4.3.3 源码精读

[`tokenize` 的 Python 回退用 `spawn_blocking` — `rust/sglang-grpc/src/server.rs:496-503`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L496-L503)：当 `self.bridge.rust_tokenizer()` 为 `None`（原生分词器不可用，详见 u2-l8）时走 Python 回退。`spawn_blocking` 闭包内 `bridge.tokenize_py(&text, add_special)` 会 `Python::with_gil` 调 Python；`.await??` 分别处理 `JoinError` 与 `PyErr`。`detokenize` 的回退（`server.rs:541-548`）结构相同。

[`health_check` — `rust/sglang-grpc/src/server.rs:559-572`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L559-L572)：`spawn_blocking(move || bridge.health_check())` 把「跨 GIL 查询 Python 调度器健康度」的活儿挪到阻塞池，`.await??` 后组装 `HealthCheckResponse`。`get_model_info`（`server.rs:574-590`）、`get_server_info`（`server.rs:592-605`）、`list_models`（`server.rs:607-617`）是同一个模板的复刻，区别只在拿到 JSON 串后如何解析。

对照 `bridge.rs` 里这些方法内部确实在拿 GIL：[例：`health_check` — `rust/sglang-grpc/src/bridge.rs:281-286`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L281-L286) 内部 `Python::with_gil(|py| runtime_handle.call_method0(py, "health_check")...)`。正因为方法体内是同步拿 GIL 调 Python，外层才必须用 `spawn_blocking` 隔离。

最后看 `spawn_abort` 的「防御式」写法：

[`spawn_abort` 用 `Handle::try_current` 而非直接 `spawn_blocking` — `rust/sglang-grpc/src/server.rs:125-139`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L125-L139)：`spawn_abort` 由 `RequestAbortGuard::drop` / `abort_now` 调用（详见 u3-l2）。`Drop` 可能在**没有 Tokio 运行时上下文**的场景下触发（例如运行时正在关闭、或 future 被从非运行时线程 drop）。直接 `tokio::task::spawn_blocking` 会 panic（「no reactor」/「must be called from runtime」），所以这里先用 `Handle::try_current()` 试探：拿得到 handle 就 `spawn_blocking` 去 abort，拿不到就记一条 warn 跳过——宁可漏一次 abort，也不能在 drop 里 panic 把进程拖崩。这是「在不确定的上下文里提交阻塞任务」的稳健写法。

#### 4.3.4 代码实践

**实践目标**：枚举 `server.rs` 中所有 `spawn_blocking` 调用，说明它们为何不能直接在 `async fn` 里用 `Python::with_gil`（对应规格里的实践任务第一问）。

**操作步骤**：

1. 在 `server.rs` 内搜索 `spawn_blocking`，逐一记录行号与所在 RPC（参考 4.3.2 的表格）。
2. 对每一处，跳进 `bridge.rs` 看对应方法体，确认它内部确实有 `Python::with_gil`。
3. 思考反例：如果把 `health_check` 改成

   ```rust
   // 示例代码（不要照抄，仅用于分析）
   async fn health_check(&self, _req) -> Result<...> {
       let healthy = Python::with_gil(|py| self.bridge.health_check_inner(py)); // ❌ 直接内联
       ...
   }
   ```

   会引入哪三个问题？

**需要观察的现象**：6 处 `spawn_blocking` 全部包裹「跨 GIL 调 Python」的 bridge 方法；`spawn_abort` 用 `Handle::try_current` 是为了容忍 drop 时无运行时上下文。

**预期结果**：能口头复述「直接内联 `Python::with_gil` 会 ① 占用 worker 削弱并发 \(W_{\text{eff}}=W-b\)、② 有死锁风险、③ 连累定时器/IO 轮询；`spawn_blocking` 把阻塞挪到独立阻塞池，worker 立即释放」。这就是本模块的结论。

#### 4.3.5 小练习与答案

**练习 1**：`tokenize` 和 `detokenize` 为什么不是无条件走 `spawn_blocking`？

> **答案**：它们先尝试 `self.bridge.rust_tokenizer()`（纯 Rust、不拿 GIL、不阻塞），只有为 `None` 时才回退到 `spawn_blocking` + Python（见 u2-l8）。原生路径直接在 worker 上同步算完即可，无需进阻塞池；只有「必须拿 GIL 调 Python」的回退路径才需要 `spawn_blocking` 隔离。

**练习 2**：`.await??` 里的两个 `?` 各处理什么错误？为什么顺序不能反？

> **答案**：第一个 `?` 处理 `spawn_blocking` 返回的 `Result<T, JoinError>`——即阻塞任务 panic 或被取消，映射成 `Status::internal("Task join error: ...")`；只有它通过后，才拿到闭包的返回值 `Result<T, PyErr>`，第二个 `?` 处理这个 `PyErr`，经 `pyerr_to_status` 映射。顺序不能反：必须先确认阻塞任务「成功跑完」（无 `JoinError`），才谈得上「它的返回值是 Ok 还是 Err」。

---

### 4.4 模块四：GIL 获取时机与 submit_request 的「内联」取舍

#### 4.4.1 概念说明

通读三个源文件后会发现，`Python::with_gil` 在本 crate 里出现在**好几种不同的上下文**里，并非全都套了 `spawn_blocking`。把它们归类，能看清「什么时候该内联、什么时候必须隔离」的判断准则。

可以归纳为四种模式：

- **Pattern A — 启动期一次性 GIL**：在运行时还不存在时，于 Python 调用线程上拿一次 GIL，摘取配置。只发生一次、很 brief。
- **Pattern B — `async fn` 内联 GIL（brief 操作）**：在 worker 线程上直接 `Python::with_gil`，但只做「投递即返回」的极短操作（如把请求塞进 Python 队列、给异常分类）。属于「可接受的小阻塞」。
- **Pattern C — `spawn_blocking` 闭包内 GIL（可能慢的操作）**：把 GIL 调用整体扔进阻塞池，worker 不被占。对应 4.3 的 6 个调用点。
- **Pattern D — 被 `spawn` 出来的异步任务内 GIL**：先用 `tokio_handle.spawn` 开一个异步任务，任务里 `.await` 一个真正可能阻塞的异步操作（如慢通道发送），**之后**才 `Python::with_gil` 做一点点 brief 收尾（如发个就绪通知）。

判断准则（心法）：

- 若 Python 调用是「投递/查询队列、异常分类」这类微秒级、且 Python 侧立即返回 → **内联即可（Pattern B）**。
- 若 Python 调用会做真活儿（序列化、查状态、跑分词器），可能毫秒级以上 → **必须 `spawn_blocking`（Pattern C）**。
- 若你身处一条**没有运行时上下文**的线程（Python 回调线程）、却需要发起异步操作 → **用 `Handle::spawn` 把异步部分交回运行时，GIL 部分尽量短（Pattern D，见 4.2）**。

`submit_request` 正是 Pattern B 的典型，也是本模块要解释的「内联取舍」。

#### 4.4.2 核心流程

四种模式的判定与落点：

```text
要在 Rust 里调 Python：
│
├─ 处于启动期、运行时尚未建立？
│     └─ Pattern A：直接 with_gil（例：extract_tokenizer_info）
│
├─ 处于 tonic async fn、且 Python 调用「投递即返回」？
│     └─ Pattern B：内联 with_gil（例：submit_request / abort / pyerr_to_status）
│
├─ 处于 tonic async fn、但 Python 调用会做真活儿？
│     └─ Pattern C：spawn_blocking(move || { with_gil(...) })（例：health_check 等 6 处）
│
└─ 处于 Python 回调线程（持 GIL、无运行时上下文）、却需要 .await？
      └─ Pattern D：tokio_handle.spawn(async { .await; with_gil(收尾) })（例：try_send_chunk 满分支）
```

为什么 `submit_request` 选了 Pattern B 而不是 Pattern C？关键在于 Python 侧 `runtime_handle.submit_request` 的语义：它**只是把请求登记进内部队列并立刻返回**，并不等模型推理完成（推理结果是后续通过 chunk 回调异步推回的）。也就是说这次 GIL 调用是「微秒级、纯入队」，把它扔进 `spawn_blocking` 反而要多付出一次线程切换和 `JoinHandle` 调度的代价，得不偿失。于是作者选择「在 worker 上内联拿 GIL，快进快出」。

而 `health_check`/`get_model_info` 等，Python 侧要做真实的查询与序列化，耗时不可控，必须走 Pattern C。

#### 4.4.3 源码精读

**Pattern A** —— 启动期一次性 GIL：

[`extract_tokenizer_info` 在运行时建立之前拿 GIL 摘配置 — `rust/sglang-grpc/src/lib.rs:94-95`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/lib.rs#L94-L95)：函数体 `Python::with_gil(|py| { ... })` 一次性从 `runtime_handle` 上摘出 `tokenizer_path`/`tokenizer_mode`/`context_len`（详见 u1-l4、u2-l8）。它在 `start_server` 第 203 行、**运行时 `rt` 构建之前**被调用，因此此刻根本没有 Tokio 上下文，谈不上 `spawn_blocking`——只能在这条 Python 调用线程上直接拿 GIL。因为是启动期一次性、且只是读几个属性，brief 且无害。

**Pattern B** —— `async fn` 内联 GIL：

[`submit_request` 内联拿 GIL 投递请求 — `rust/sglang-grpc/src/bridge.rs:177-207`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L177-L207)：第 186 行 `Python::with_gil(|py| { ... })` 在 `async fn` 的调用链上**直接**执行（注意 `submit_request` 本身是同步 `fn`，但它被 `text_generate` 等 `async fn` 同步调用，于是实际跑在 worker 线程上）。闭包里 `json_map_to_pydict` + 构造回调 + `runtime_handle.call_method(py, "submit_request", ...)`——Python 侧只入队即返回，故可接受这点内联阻塞。失败时第 203 行 `remove_channel(rid)` 回滚（详见 u2-l5）。

[`pyerr_to_status` 内联拿 GIL 给异常分类 — `rust/sglang-grpc/src/server.rs:66-76`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/server.rs#L66-L76)：第 67 行 `Python::with_gil(|py| err.is_instance_of::<PyValueError>(py) || ...)` 只为判断异常类型，极 brief；它常出现在 `async fn` 的 `.map_err(...)` 里，故也算 Pattern B。`bridge.rs` 的 `abort`（`bridge.rs:256-260`）同理——临界区里只改账本，GIL 段只发一次 `call_method1("abort", ...)`。

**Pattern C** —— `spawn_blocking` 闭包内 GIL：见 4.3.3（6 个调用点）。

**Pattern D** —— 被 `spawn` 出来的异步任务内 GIL：

[`try_send_chunk` 满分支：先 `.await` 再拿 GIL — `rust/sglang-grpc/src/bridge.rs:562-592`](https://github.com/sgl-project/sglang/blob/977ea336cd3e960141c4c6e746b4efc24fdf312e/rust/sglang-grpc/src/bridge.rs#L562-L592)：`tokio_handle.spawn(async move { ... })` 开出来的异步任务里，**先** `sender.send(msg).await`（这才是真正可能长时间的等待，但它发生在 worker 上、不持 GIL），**成功之后**才 `Python::with_gil` 做 brief 的 `mark_send_ready`/`notify_ready`（`bridge.rs:572-576`）或失败时 `close_channel_with_error`（`bridge.rs:579-589`）。也就是说：把「可能慢」的部分（channel send）放在 `.await`、把「拿 GIL」的部分压到最短并放在 `.await` 之后——既不卡 GIL，也不卡 worker。这是比 Pattern C 更精细的写法：连「拿到 GIL 之前的等待」都用异步 `.await` 而非同步阻塞来表达。

#### 4.4.4 代码实践

**实践目标**：把三个源文件里所有 `Python::with_gil` 调用点归类到 A/B/C/D 四种模式。

**操作步骤**：

1. 用搜索把 `with_gil` 在 `lib.rs`/`server.rs`/`bridge.rs` 的全部出现位置列出（`lib.rs:95`、`server.rs:67`、`bridge.rs` 多处、`bridge.rs:572/579` 等）。
2. 对每一处判断它属于哪种模式：
   - `lib.rs:95`（`extract_tokenizer_info`）→ Pattern A；
   - `server.rs:67`（`pyerr_to_status`）、`bridge.rs:186`（`submit_request`）、`bridge.rs:256`（`abort`）、`bridge.rs:268/275/282/290/300/309`（各 info/control 方法**体**）→ 这些方法体本身是 Pattern B 的「原料」，但其中 6 个是被 Pattern C 的 `spawn_blocking` 包起来调用的；
   - `bridge.rs:323`（`submit_json`）→ 同 `submit_request`，Pattern B；
   - `bridge.rs:572/579`（`try_send_chunk` 的 spawn 任务内）→ Pattern D。
3. 验证一条「内联 vs 隔离」的取舍：`submit_request`（B）与 `health_check`（C）都调 Python，为何一个内联、一个隔离？

**需要观察的现象**：能画出一张「调用点 → 模式」对照表，并指出区分 B 与 C 的唯一判据是「Python 侧是否立即返回」。

**预期结果**：复述判据——**Python 侧「投递即返回」可内联（B），「做真活儿」必须 `spawn_blocking`（C）；无运行时上下文却要 `.await` 时用 `Handle::spawn`（D）；启动期一次性用 A。**

#### 4.4.5 小练习与答案

**练习 1**：`submit_request` 内联拿 GIL（Pattern B）有没有可能反而拖慢服务？在什么前提下它是安全的？

> **答案**：有可能，前提是 Python 侧 `submit_request` 不再「立即返回」。它的安全性完全依赖 Python 端 `runtime_handle.submit_request` 只做「入队」而不做「等待推理」。一旦某天 Python 侧把它改成同步等到出结果再返回，这次内联 GIL 就会长时间占住 worker，退化为 Pattern B 被滥用的反例——届时必须改写成 Pattern C（`spawn_blocking`）。这也提示：跨语言调用的「内联 vs 隔离」取决于**被调方的耗时语义**，而非调用方。

**练习 2**：Pattern D（`try_send_chunk` 满分支）和 Pattern C（`spawn_blocking`）都把工作「挪出当前线程」，二者有何本质区别？

> **答案**：Pattern C 用 `spawn_blocking` 把**同步阻塞**活儿（`with_gil` 调 Python）挪到**阻塞线程池**；Pattern D 用 `tokio_handle.spawn` 把一个**异步任务**（含 `.await`）挪到 **worker 池**。前者解决「同步阻塞不能放在 async 线程」，后者解决「Python 回调线程没有运行时上下文、不能 `.await`」。两者动机不同、落点的线程池也不同。

---

## 5. 综合实践

把四个模块串起来，完成一张「**一条流式生成请求的线程/上下文流转图**」。以 `text_generate` 为例：

**实践目标**：用本讲授的并发模型，画出从「Python 调 `start_server`」到「一个 `text_generate` 流式响应完整结束」的过程中，代码分别在**哪条线程/哪个上下文**上执行、何时拿/放 GIL、何时 `spawn`/`spawn_blocking`。

**操作步骤**：

1. 起点设为 Python 主线程调用 `_core.start_server(...)`。标注：构建 `rt`、`clone handle`、spawn `sglang-grpc` 线程、`block_on`、返回 `GrpcServerHandle`——这些在**哪条线程**完成？运行时此刻被谁拥有？
2. 客户端发来一条 `text_generate`。标注：tonic accept 主循环在 `sglang-grpc` 线程，但 RPC 处理任务被 `tokio::spawn` 到 `sglang-grpc-tokio` worker 线程上跑 `async fn text_generate`。
3. 标注 `text_generate` 里 `self.bridge.submit_request(...)`：这是 Pattern B，在 worker 线程上**内联** `Python::with_gil` 入队，Python 侧返回后释放 GIL。
4. 标注 Python 侧异步产出 chunk、在 Python 线程上调用 `chunk_callback(chunk, ...)`（Pattern 之外的「回调线程」上下文）：回调持有 GIL、调用 `try_send_chunk`。
5. 假设客户端消费慢、通道满：标注 `try_send_chunk` 走 `Full` 分支，用**回调对象里的 `tokio_handle`** `spawn` 一个异步任务（Pattern D）到 worker 池，回调立即返回 `Pending`、释放 GIL；spawn 出的任务在 worker 线程上 `sender.send(msg).await`，成功后再 `Python::with_gil` 发就绪通知。
6. 假设客户端中途断开：标注 `RequestAbortGuard::drop` 触发 `spawn_abort`，用 `Handle::try_current().spawn_blocking`（防御式）把 `bridge.abort(...)` 扔进阻塞池，避免在 drop 里阻塞 worker。

**需要观察的现象**：整条链路上，**GIL 从不在「需要 `.await` 慢客户端」时被持有**；凡是可能慢的操作（慢通道 send、Python 真活儿、drop 里的 abort）都被挪到了 worker 池或阻塞池；Python 主线程与 GIL 始终保持「快进快出」。

**预期结果**：能产出一张含「线程名 / 上下文 / 是否持 GIL / 关键调用」四列的流转表，并口头解释每一处「挪线程」的动机都对应本讲的某条准则。若无法实地运行验证，请明确标注「待本地验证」，但源码层面的流转关系应可完整推导。

## 6. 本讲小结

- `start_server` 把 Tokio 多线程运行时建在一条独立的 `sglang-grpc` OS 线程里：该线程 `block_on` 驱动 tonic accept 主循环，而具体 RPC `async fn` 实际跑在 `sglang-grpc-tokio` worker 池上；`.enable_all()` 是 IO/定时器/信号驱动的前提。
- `tokio_handle` 经 `rt.handle().clone()` 取出，存入 `PyBridge`，并在构造每个 chunk 回调时再 clone 一份；它存在的根本原因是**回调跑在持有 GIL 的 Python 线程上、不能 `.await`，必须攥着 `Handle` 把异步发送甩给运行时**（`try_send_chunk` 通道满分支）。
- 直接在 `async fn` 里 `Python::with_gil` 会占用 worker、削弱并发（\(W_{\text{eff}}=W-b\)）、有死锁与饿死定时器风险；凡是「Python 做真活儿」的调用都用 `tokio::task::spawn_blocking` 隔离到独立阻塞池（`server.rs` 共 6 处）。
- `spawn_blocking` 模板的 `.await??` 双问号分别处理 `JoinError` 与 `PyErr`；`spawn_abort` 改用 `Handle::try_current().spawn_blocking` 是为容忍 `Drop` 时无运行时上下文。
- `Python::with_gil` 有四种使用时机（A 启动期一次性 / B 内联投递即返回 / C `spawn_blocking` 隔离 / D `spawn` 任务内先 `.await` 后拿 GIL）；判据是「Python 侧是否立即返回」与「当前线程是否有运行时上下文」。
- `submit_request` 选择 Pattern B 内联，是因为 Python 侧 `submit_request` 仅入队即返回；这一取舍的安全性完全依赖被调方的耗时语义。

## 7. 下一步学习建议

- **动手验证并发模型**：建议结合 u3-l1（背压停泊）与 u3-l2（中止传播）通读 `try_send_chunk` 的完整三分支，体会「Ready/Pending/Closed」三态与「回调线程 / worker 池 / 阻塞池」三种执行上下文的对应关系。
- **继续读源码**：精读 `bridge.rs` 的 `try_send_chunk`（524–607 行）与 `close_channel_with_error`（449–461 行），把本讲的 Pattern D 与 u3-l1 的「单停泊位不变量」、u3-l3 的「终端错误→gRPC Status」串成一条完整链。
- **下一讲 u3-l6**：将转向测试组织与扩展实践（新增 RPC、新增分词器后端），届时可回到本讲，思考「新增一个会调用 Python 的 RPC 时，应当套 Pattern B 还是 Pattern C」——这正是本讲心法的直接应用。
- **延伸阅读**：Tokio 官方文档关于 `spawn_blocking` 与阻塞线程池（默认 512 上限、keep-alive）的说明，以及 PyO3 关于 `Python::with_gil` 与 GIL 死锁 avoidance 的章节，能帮助把本讲的「心法」沉淀为可复用的设计直觉。
