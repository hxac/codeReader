# EngineCore 引擎核心主循环

## 1. 本讲目标

本讲聚焦 vLLM V1 架构中**最核心的运行时组件**——`EngineCore`。学完本讲后，你应该能够：

- 理解 `EngineCore` 为什么被称为「引擎的内层循环」，以及它持有哪些关键对象（scheduler、executor、队列）。
- 理解 `EngineCoreProc` 如何把 `EngineCore` 包装成一个**独立进程**，并用 ZMQ 线程 + Python `queue.Queue` 与外界通信。
- 看懂 `run_busy_loop` 这个「忙等待循环」的三段式结构（取输入 → 跑一步 → 发输出），以及空闲（idle）时如何阻塞。
- 用源码追踪一轮 `engine step` 的完整流程：`schedule()` → `execute_model(non_block=True)` → `update_from_output()`，并理解为什么执行过程要返回 `Future`。

本讲是 u5「模型执行链路」单元的起点，承接 u3-l1（V1 多进程架构），向下衔接 u5-l2（GPU Worker）与 u5-l3（ModelRunner）。

## 2. 前置知识

阅读本讲前，你需要先掌握以下概念（前面讲义已建立）：

- **V1 多进程架构**：vLLM V1 把系统拆成 API Server、EngineCore、GPU Worker、DP Coordinator 等多个进程，进程间用 ZMQ 通信（见 u3-l1）。
- **进程 vs 线程**：进程有独立的内存空间和 Python 解释器（独立 GIL）；线程共享内存但受同一个 GIL 约束。vLLM 把 EngineCore 放进**独立进程**，正是为了让它的 CPU 调度不被 API Server 的请求处理打断。
- **ZMQ**：一种高性能消息队列库，支持 `DEALER/ROUTER`、`PUSH/PULL` 等 socket 模式，用于进程间收发二进制消息帧。
- **`Future`**：Python `concurrent.futures.Future` 表示「一个尚未完成的异步结果」，调用 `.result()` 会阻塞直到结果就绪。这是 EngineCore 让「GPU 计算」与「CPU 工作」重叠的关键工具。
- **msgspec**：一个高性能的序列化库，vLLM 用它把请求/输出在进程边界上编码为字节流。
- **Scheduler 与 KV 缓存**：调度器决定「这一步算哪些请求、各算几个 token」（见 u4-l2）；KV 缓存按 block 管理（见 u4-l4）。本讲不深入调度算法本身，只关心 EngineCore **如何驱动**调度器和执行器。

一句话直觉：**EngineCore 就像一个不知疲倦的调度员，不停地重复「看有没有新任务 → 决定这批算什么 → 交给 GPU 算 → 把结果收回来发出去」。**

## 3. 本讲源码地图

| 文件 | 关键内容 | 本讲作用 |
|------|----------|----------|
| `vllm/v1/engine/core.py` | `EngineCore`、`EngineCoreProc`、`run_busy_loop`、`step`、`run_engine_core` | 主战场，全部最小模块都来自这里 |
| `vllm/v1/engine/utils.py` | `EngineCoreProcManager`、`run_engine_core` 的调用方、握手协议 | 解释「谁在什么时候把 EngineCore 拉成进程」 |
| `vllm/v1/engine/__init__.py` | `EngineCoreRequestType`、`EngineCoreOutputs` | 解释在队列里流动的消息类型 |
| `vllm/v1/executor/abstract.py` | `execute_model` 的抽象签名 | 解释 `non_block=True` 返回 `Future` 的契约 |

阅读建议：先建立 `EngineCore`（内层逻辑）与 `EngineCoreProc`（进程包装）的分层意识，再沿着 `run_busy_loop → _process_input_queue → _process_engine_step → step` 这条调用链自上而下读源码。

## 4. 核心概念与源码讲解

### 4.1 EngineCore：引擎的内层循环

#### 4.1.1 概念说明

`EngineCore` 是 vLLM V1 引擎的**内层逻辑核心**。它的类文档只有一句话：

> "Inner loop of vLLM's Engine."（vLLM 引擎的内层循环。）

之所以叫「内层」，是因为它只关心**推理本身**——调度、执行、产出输出，而不管「请求从哪来、结果发到哪去」。HTTP 接入、tokenize、多模态预处理等「外层」工作都在 API Server 进程完成（见 u3-l1、u5-l5）。

`EngineCore` 解决的核心问题是：**在一个进程里，把「纯 CPU 的调度决策」和「GPU 上的模型前向」粘合在一起，形成一个可被反复调用的 `step()`。** 它持有三样最重要的东西：

1. **`scheduler`**：决策者，决定这一步算什么（见 u4-l2）。
2. **`model_executor`**：执行者，负责把决策变成 GPU 上的前向（见 u5-l2）。
3. **若干队列**：承载「待处理的客户端请求」与「要发回的输出」。

#### 4.1.2 核心流程

`EngineCore.__init__` 的初始化流程可以概括为五步：

1. **加载插件**：`load_general_plugins()`，让插件有机会干预引擎（见 u10-l3）。
2. **建执行器**：`self.model_executor = executor_class(vllm_config)`，这里把模型权重、worker 进程拉起来。
3. **初始化 KV 缓存**：`_initialize_kv_caches()` 会先做一次显存 profiling，算出能分给 KV 缓存多少显存，再回填 `num_gpu_blocks`。
4. **建调度器**：`Scheduler = vllm_config.scheduler_config.get_scheduler_cls()`，按配置选具体调度器实现。
5. **选 step 函数**：根据是否启用 batch queue（流水线并行才用），决定 `self.step_fn` 是 `step` 还是 `step_with_batch_queue`。

注意第 3、4 步的顺序：**必须先 profiling 算出 KV 缓存容量，调度器才知道自己有多少 block 可分配**。这是 EngineCore 把「显存预算」与「调度能力」串起来的地方。

#### 4.1.3 源码精读

`EngineCore` 类的定义与构造函数：[vllm/v1/engine/core.py:103-113](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/core.py#L103-L113)

```python
class EngineCore:
    """Inner loop of vLLM's Engine."""

    def __init__(
        self,
        vllm_config: VllmConfig,
        executor_class: type[Executor],
        log_stats: bool,
        executor_fail_callback: Callable | None = None,
        include_finished_set: bool = False,
    ):
```

构造函数里把 executor、KV 缓存、调度器依次建好的关键片段：[vllm/v1/engine/core.py:131-168](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/core.py#L131-L168)

```python
# Setup Model.
self.model_executor = executor_class(vllm_config)
...
# Setup KV Caches and update CacheConfig after profiling.
kv_cache_config = self._initialize_kv_caches(vllm_config)
self.structured_output_manager = StructuredOutputManager(vllm_config)

# Setup scheduler.
Scheduler = vllm_config.scheduler_config.get_scheduler_cls()
...
self.scheduler: SchedulerInterface = Scheduler(
    vllm_config=vllm_config,
    kv_cache_config=kv_cache_config,
    ...
)
```

最后一步——选择 `step_fn`，这是后续 `run_busy_loop` 真正会调用的方法：[vllm/v1/engine/core.py:231-233](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/core.py#L231-L233)

```python
self.step_fn = (
    self.step if self.batch_queue is None else self.step_with_batch_queue
)
```

> 说明：当 `batch_queue is None`（即没有流水线并行的 batch 队列）时，`step_fn` 指向朴素的 `step`。本讲 4.4 节以 `step` 为主线讲解；`step_with_batch_queue` 的思路类似但允许「调度下一批」与「等待上一批」重叠，用于流水线并行消气泡。

构造函数末尾还有两个有意思的优化：`freeze_gc_heap()`（把启动期分配的权重、KV 缓存标记为「静态」，让 Python GC 跳过它们以减少暂停）和 `enable_envs_cache()`（环境变量此后不再变化，缓存起来）。这两个调用都是为了**让 busy loop 跑得更稳、更快**。

#### 4.1.4 代码实践

**实践目标**：建立「`EngineCore` 把 executor、scheduler、step_fn 串起来」的方位感。

**操作步骤（源码阅读型）**：

1. 打开 `vllm/v1/engine/core.py`，定位 `class EngineCore`（约第 103 行）。
2. 在 `__init__` 中找到 `self.model_executor = executor_class(vllm_config)`（第 132 行），确认 executor 在调度器**之前**创建。
3. 继续向下找到 `self.scheduler = ...`（第 160 行）和 `self.step_fn = ...`（第 231 行）。
4. 用编辑器的「查找引用」功能，看 `self.step_fn` 在哪里被调用——你会找到 `_process_engine_step`（第 1439 行）。

**需要观察的现象**：构造函数里 executor → KV 缓存 → scheduler → step_fn 的**顺序是固定的且有依赖**：scheduler 需要 `kv_cache_config`，而 `kv_cache_config` 来自 profiling。

**预期结果**：你能用自己的话说出「为什么 scheduler 不能在 KV 缓存初始化之前创建」——因为调度器需要知道可用 block 数量。

**待本地验证**：本实践为纯源码阅读，无需运行。

#### 4.1.5 小练习与答案

**练习 1**：`EngineCore` 的类文档为什么写「Inner loop」而不是「Engine」？

> **参考答案**：因为它只负责引擎的**内层循环**（调度 + 执行 + 产出），不负责外层的请求接入、tokenize、HTTP 等。外层工作由 API Server 进程承担。把内外层分到不同进程，正是 V1 多进程架构的初衷（见 u3-l1）。

**练习 2**：构造函数里 `freeze_gc_heap()` 和 `enable_envs_cache()` 都放在 `__init__` 的**末尾**，这暗示了什么？

> **参考答案**：暗示「到此为止，所有启动期的重型对象（权重、KV 缓存）和环境变量都已定型，此后不再变化」。把它们在进入 busy loop 前固化，可以让后续循环少做无用功（GC 不必扫描静态堆，env 查询走缓存）。

---

### 4.2 EngineCoreProc：让 EngineCore 跑在独立进程里

#### 4.2.1 概念说明

`EngineCore` 本身只是一个「可被调用 `step()` 的对象」，它**不会自己循环**，也**不关心进程边界**。真正让它「活起来」的是它的子类 `EngineCoreProc`。

`EngineCoreProc` 的职责是给 `EngineCore` 套上一层**进程与通信外壳**：

- 用 ZMQ socket 与 API Server 进程收发消息；
- 用两个 Python `queue.Queue`（`input_queue` / `output_queue`）在「ZMQ 线程」与「主循环」之间解耦；
- 处理启动握手（handshake）、优雅关闭（shutdown）、心跳/容错。

为什么要这一层？因为 vLLM V1 让 EngineCore 跑在**独立进程**里。进程之间不能直接调用方法，只能通过消息传递。`EngineCoreProc` 就是「消息 ↔ 方法调用」的翻译层。

> 术语辨析：`EngineCore`（内层逻辑，无进程概念）⊂ `EngineCoreProc`（加上进程与 ZMQ 外壳）。在本讲的多数语境下，当我们说「EngineCore 进程」时，实际指的是 `EngineCoreProc` 实例。

#### 4.2.2 核心流程

`EngineCoreProc` 内部有三条并行的「数据通路」：

```
                  API Server 进程
                        │  (ZMQ DEALER/ROUTER, PUSH/PULL)
                        ▼
        ┌───────────────────────────────────┐
        │          EngineCoreProc 进程         │
        │                                     │
        │  ┌─────────────┐   ┌──────────────┐│
        │  │ input_thread │   │ output_thread ││   ← 守护线程，处理 ZMQ IO
        │  │ (收 ZMQ)     │   │ (发 ZMQ)      ││
        │  └──────┬──────┘   └──────▲───────┘│
        │         │ put             │ get      │
        │         ▼                 │          │
        │    input_queue        output_queue   │
        │         │                 ▲          │
        │         │ get             │ put      │
        │         ▼                 │          │
        │  ┌─────────────────────────────────┐│
        │  │     主线程：run_busy_loop        ││   ← 调度 + 执行
        │  │ _process_input_queue → step_fn  ││
        │  └─────────────────────────────────┘│
        └───────────────────────────────────┘
```

- **`input_thread`**：守护线程，在 `process_input_sockets` 里循环 `poll()` ZMQ 输入 socket，把收到的请求反序列化后 `put` 进 `input_queue`。
- **`output_thread`**：守护线程，在 `process_output_sockets` 里循环 `get()` `output_queue`，把输出序列化后通过 ZMQ 输出 socket 发出。
- **主线程**：跑 `run_busy_loop`，从 `input_queue` 取请求、调用 `step_fn`、把结果 `put` 进 `output_queue`。

为什么要把 ZMQ IO 放在**单独的守护线程**？源码注释说得很清楚：「这些线程能让 ZMQ socket IO 与 GPU **重叠**（因为它们会释放 GIL），并让部分序列化/反序列化与模型前向重叠。」也就是说，当主线程在等 GPU 时，IO 线程可以同时收新请求、发上一批结果。

#### 4.2.3 源码精读

`EngineCoreProc` 的类定义与两条队列的创建：[vllm/v1/engine/core.py:1008-1028](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/core.py#L1008-L1028)

```python
class EngineCoreProc(EngineCore):
    """ZMQ-wrapper for running EngineCore in background process."""

    ENGINE_CORE_DEAD = b"ENGINE_CORE_DEAD"
    addresses: EngineZmqAddresses

    @instrument(span_name="EngineCoreProc init")
    def __init__(self, vllm_config, local_client, handshake_address,
                 executor_class, log_stats, ...):
        self.input_queue = queue.Queue[tuple[EngineCoreRequestType, Any]]()
        self.output_queue = queue.Queue[tuple[int, EngineCoreOutputs] | bytes]()
```

> 说明：`input_queue` 里放的是 `(请求类型, 请求数据)` 元组；`output_queue` 里放的是 `(client_index, EngineCoreOutputs)` 元组（或一个表示引擎死亡的 `bytes` 哨兵）。请求类型来自 `EngineCoreRequestType` 枚举（见下方）。

`EngineCoreRequestType` 枚举——在 `input_queue` 里流动的消息类型：[vllm/v1/engine/__init__.py:261-274](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/__init__.py#L261-L274)

```python
class EngineCoreRequestType(enum.Enum):
    ADD = b"\x00"
    ABORT = b"\x01"
    START_DP_WAVE = b"\x02"
    UTILITY = b"\x03"
    EXECUTOR_FAILED = b"\x04"   # 进程内哨兵
    WAKEUP = b"\x05"            # 关闭时唤醒 input_queue.get()
```

> 说明：用单字节 `bytes` 而不是字符串，是为了「无需额外编码步骤就能通过 socket 发送」。

构造函数末尾启动两条守护线程：[vllm/v1/engine/core.py:1097-1119](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/core.py#L1097-L1119)

```python
# Background Threads and Queues for IO. These enable us to
# overlap ZMQ socket IO with GPU since they release the GIL,
# and to overlap some serialization/deserialization with the
# model forward pass.
input_thread = threading.Thread(
    target=self.process_input_sockets, ..., daemon=True)
input_thread.start()

self.output_thread = threading.Thread(
    target=self.process_output_sockets, ..., daemon=True)
self.output_thread.start()
```

输入线程把 ZMQ 帧反序列化后塞进 `input_queue` 的关键片段（位于 `process_input_sockets`）：[vllm/v1/engine/core.py:1702-1741](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/core.py#L1702-L1741)

```python
while True:
    for input_socket, _ in poller.poll():
        type_frame, *data_frames = input_socket.recv_multipart(copy=False)
        ...
        request_type = EngineCoreRequestType(bytes(type_frame.buffer))
        ...
        # Push to input queue for core busy loop.
        self.input_queue.put_nowait((request_type, request))
```

> 说明：这是「ZMQ → input_queue」的入口。注意 `ADD` 类型的请求会先经 `preprocess_add_request`（做语法编译、多模态特征更新等预处理）再放入队列，让预处理与模型前向并行。

#### 4.2.4 代码实践

**实践目标**：理解 `EngineCoreProc` 用「两条线程 + 两条队列」解耦 IO 与计算。

**操作步骤（源码阅读型）**：

1. 在 `core.py` 中定位 `process_input_sockets`（第 1645 行）和 `process_output_sockets`（第 1743 行）。
2. 在 `process_output_sockets` 中找到 `output = self.output_queue.get()`（第 1779 行），注意它会**阻塞**直到主线程往队列里放了东西。
3. 找到 `ENGINE_CORE_DEAD` 哨兵的处理（第 1780 行）：当输出线程收到这个哨兵，就给所有 socket 发死亡通知然后 `break`，整个进程随之退出。

**需要观察的现象**：输出线程是「消费者」，它只在主循环产出 `EngineCoreOutputs` 时才有活干；没有活干时它阻塞在 `get()` 上，几乎不消耗 CPU。

**预期结果**：你能画出「主线程 `step()` → `output_queue.put()` → 输出线程 `get()` → ZMQ `send`」这条输出数据通路。

**待本地验证**：纯源码阅读，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `input_thread` 和 `output_thread` 要设成 `daemon=True`（守护线程）？

> **参考答案**：守护线程不会阻止进程退出。当主线程的 busy loop 因 `SystemExit` 结束时，守护线程会被自动回收，不需要显式 join。这简化了关闭流程——只要主循环退出，整个进程就能干净地结束。

**练习 2**：`process_input_sockets` 里对 `ADD` 请求会先调用 `preprocess_add_request` 再入队。为什么不直接把原始请求入队，让主循环去预处理？

> **参考答案**：为了让**预处理（如结构化输出的语法编译）与主循环里的模型前向并行**。预处理是 CPU 密集且与具体请求无关的工作，放在输入线程做，主线程就能在等 GPU 的同时让下一个请求的预处理提前完成，缩短首 token 延迟（TTFT）。

---

### 4.3 run_busy_loop：引擎的驱动循环

#### 4.3.1 概念说明

有了 `EngineCoreProc` 的进程外壳和队列，还差一个**驱动器**——一个不停把「取输入 → 跑一步」重复下去的循环。这就是 `run_busy_loop`。

`run_busy_loop` 的名字暗示了它的本质：**忙等待循环**。它会**持续不停地跑**，只要还有工作或还有未完成的请求。当真的没事干时，它会在 `input_queue.get()` 上**阻塞**（而不是空转烧 CPU），一旦有新请求到来就被唤醒，继续循环。

这种设计的好处是**低延迟**：请求一来就能立刻被处理，不需要等固定的轮询间隔。

#### 4.3.2 核心流程

`run_busy_loop`（非 DP 版本）的主体非常简洁，是一个三段式循环：

```
while self._handle_shutdown():          # 还没收到关闭信号？
    self._process_input_queue()         # ① 取输入：处理 input_queue 里的请求
    self._maybe_publish_request_counts()# ② 发布 DP 负载统计（仅 DP 内部均衡时）
    self._process_engine_step()         # ③ 跑一步：调用 step_fn，把输出放入 output_queue
    self._maybe_publish_request_counts()# ② 再发一次，保证统计新鲜
raise SystemExit                         # 收到关闭信号 → 退出
```

其中最微妙的是 `_process_input_queue` 的「智能阻塞」逻辑：

- **有活干时**（`has_work()` 为真）：不阻塞，立刻把 `input_queue` 排干，然后返回去跑 `step`。
- **没活干时**（`has_work()` 为假）：在 `input_queue.get(block=True)` 上**阻塞**，直到新请求到来或收到 `WAKEUP` 哨兵。

`has_work()` 的判定是「引擎正在运行 / 调度器里还有请求 / batch 队列非空」三者之一：[vllm/v1/engine/core.py:1365-1371](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/core.py#L1365-L1371)

```python
def has_work(self) -> bool:
    """Returns true if the engine should be stepped."""
    return (
        self.engines_running
        or self.scheduler.has_requests()
        or bool(self.batch_queue)
    )
```

`run_busy_loop` 与 `step` 的关系：`run_busy_loop` 是**循环骨架**，`step`（经 `step_fn`）是**每一步的肉体**。骨架负责「什么时候跑、怎么取输入、怎么发输出」，肉体负责「这一步具体算什么」。

#### 4.3.3 源码精读

`run_busy_loop` 的完整实现（带容错装饰器）：[vllm/v1/engine/core.py:1377-1389](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/core.py#L1377-L1389)

```python
@fault_tolerant_wrapper
def run_busy_loop(self):
    """Core busy loop of the EngineCore."""
    while self._handle_shutdown():
        # 1) Poll the input queue until there is work to do.
        self._process_input_queue()
        # Publish request counts before and after GPU step to ensure freshness.
        self._maybe_publish_request_counts()
        # 2) Step the engine core and return the outputs.
        self._process_engine_step()
        self._maybe_publish_request_counts()

    raise SystemExit
```

> 说明：`@fault_tolerant_wrapper` 是容错包装；`_handle_shutdown()` 在每次循环开头检查关闭状态，返回 `False` 时退出循环。注意两次 `_maybe_publish_request_counts()` 夹住 `_process_engine_step()`，是为了在 GPU 步骤前后各发布一次请求计数，让 DP 协调器拿到尽可能新鲜的负载信息。

`_process_input_queue` 的「智能阻塞」核心：[vllm/v1/engine/core.py:1404-1425](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/core.py#L1404-L1425)

```python
def _process_input_queue(self):
    """Exits when an engine step needs to be performed."""

    waited = False
    while not self.has_work() and self.is_running():
        # Notify callbacks waiting for engine to become idle.
        self._notify_idle_state_callbacks()
        if self.input_queue.empty():
            ...
            with self.aborts_queue.mutex:
                self.aborts_queue.queue.clear()
            ...
        block = self.process_input_queue_block
        try:
            req = self.input_queue.get(block=block)
            self._handle_client_request(*req)
        except queue.Empty:
            break
        if not block:
            break
    ...
```

> 说明：外层 `while not self.has_work()` 表示「只要没活干就一直尝试取输入」。当 `block=True` 时，`input_queue.get(block=True)` 会**阻塞**直到有请求；一旦取到请求并交给 `_handle_client_request`，下一轮 `has_work()` 就可能为真，循环退出，转去跑 `step`。`process_input_queue_block` 在弹性 EP（elastic EP）扩缩容时会被设为 `False`，使取输入变为非阻塞，方便状态机推进。

请求的分发逻辑在 `_handle_client_request`：[vllm/v1/engine/core.py:1507-1520](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/core.py#L1507-L1520)

```python
def _handle_client_request(self, request_type, request):
    """Dispatch request from client."""
    if request_type == EngineCoreRequestType.WAKEUP:
        return
    elif request_type == EngineCoreRequestType.ADD:
        req, request_wave = request
        if self._reject_add_in_shutdown(req):
            return
        self.add_request(req, request_wave)
    elif request_type == EngineCoreRequestType.ABORT:
        self.abort_requests(request)
    elif request_type == EngineCoreRequestType.UTILITY:
        ...
```

> 说明：`ADD` 请求最终调到 `EngineCore.add_request()`，把请求交给**调度器**（`self.scheduler.add_request(request)`）。注意：这里**只是登记**，并不立即计算——真正的计算发生在随后的 `step` 里。

那么，是谁调用了 `run_busy_loop`？答案是 `run_engine_core` 这个静态方法，它就是子进程的入口函数：[vllm/v1/engine/core.py:1342](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/core.py#L1342)

```python
            engine_core.run_busy_loop()
```

而 `run_engine_core` 本身被 `utils.py` 里的 `CoreEngineProcManager` 当作子进程的 `target`：[vllm/v1/engine/utils.py:164-171](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/utils.py#L164-L171)

```python
self.processes.append(
    context.Process(
        target=EngineCoreProc.run_engine_core,
        name=f"EngineCore_DP{global_index}" if is_dp else "EngineCore",
        kwargs=common_kwargs
        | {"dp_rank": global_index, "local_dp_rank": local_index},
    )
)
```

> 说明：每个 DP rank 对应一个子进程，进程入口是 `run_engine_core`，它构造 `EngineCoreProc`（或 MoE 场景下的 `DPEngineCoreProc`），注册信号处理，然后调用 `run_busy_loop()`。收到 `SIGTERM`/`SIGINT` 时，信号处理函数把 `shutdown_state` 设为 `REQUESTED`，并通过 `WAKEUP` 哨兵唤醒可能阻塞的循环，让 `_handle_shutdown` 走关闭分支。

#### 4.3.4 代码实践

**实践目标**：追踪 `run_busy_loop` 的「取输入 → 跑一步」循环，并理解空闲阻塞。

**操作步骤（源码阅读型）**：

1. 在 `core.py` 定位 `run_busy_loop`（第 1378 行）。
2. 跟着 `_process_input_queue`（第 1404 行）看它如何用 `has_work()` + `input_queue.get(block=...)` 实现「有活就干，没活就睡」。
3. 跟着 `_process_engine_step`（第 1435 行）看它如何调 `self.step_fn()` 并把输出塞进 `output_queue`。
4. 在 `utils.py` 第 164 行，确认 `run_engine_core` 是子进程的 `target`。

**需要观察的现象**：当调度器里没有任何请求、batch 队列也为空时，`has_work()` 返回 `False`，循环会卡在 `input_queue.get(block=True)` 上阻塞，**CPU 占用接近零**。一旦 API Server 发来一个 `ADD` 请求，输入线程把它放进 `input_queue`，`get()` 立刻返回，循环被唤醒。

**预期结果**：你能解释「为什么 vLLM 在空闲时不会持续 100% 占用一个 CPU 核」——因为 `_process_input_queue` 用阻塞式 `get()` 而非忙等待轮询。

**待本地验证**：若本地有 GPU 环境，可用 `vllm serve` 启动一个小模型，在空闲时用 `top -H -p <EngineCore PID>` 观察主线程 CPU 占用应很低；无 GPU 环境则纯源码阅读。

#### 4.3.5 小练习与答案

**练习 1**：`run_busy_loop` 在每一轮都会调两次 `_maybe_publish_request_counts()`，分别在 `_process_engine_step()` 的前后。为什么要发两次？

> **参考答案**：为了让 DP 负载均衡器（DPCoordinator / 内部均衡）拿到**尽可能新鲜**的请求计数。GPU step 之前发一次反映「即将执行」的负载，之后发一次反映「执行完这一步后」的负载。两次之间请求状态可能变化（有请求完成、有新请求加入），双发能减少均衡决策的滞后。

**练习 2**：`_handle_shutdown()` 在 `while` 条件里被调用。如果某次循环中收到 `SIGTERM`，循环是如何**安全退出**而不是直接中断的？

> **参考答案**：信号处理函数只是把 `shutdown_state` 设为 `REQUESTED`，并往 `input_queue` 放一个 `WAKEUP` 哨兵唤醒阻塞的 `get()`。真正的退出发生在下一轮 `while self._handle_shutdown()` 检查时：它发现状态是 `REQUESTED`，根据 `shutdown_timeout` 决定「立即 abort 所有请求」还是「drain 排干」，进入 `SHUTTING_DOWN`，等所有工作完成后 `_handle_shutdown()` 返回 `False`，循环结束并 `raise SystemExit`。这是一种**协作式关闭**，避免在 step 执行中途被打断。

---

### 4.4 engine_step：一轮 step 的「调度 → 执行 → 回写」

#### 4.4.1 概念说明

前面三节讲的都是「外壳」——怎么拉进程、怎么收发消息、怎么循环。这一节进入**真正的推理核心**：一轮 `step` 到底做了什么。

`engine step`（即 `step_fn`，默认是 `EngineCore.step`）是整个引擎**每一步**的工作单元。它回答一个问题：**「这一步，调度器决定算哪些请求、各算几个 token；执行器把它们在 GPU 上算完；调度器再根据结果更新请求状态。」**

理解 `step` 的关键是抓住**三个阶段**和**一个 Future**：

- **调度（schedule）**：`scheduler.schedule()` 返回一个 `SchedulerOutput`，描述「这一步算什么」。
- **执行（execute）**：`executor.execute_model(scheduler_output, non_block=True)` **立刻返回一个 `Future`**，GPU 工作在后台进行。
- **回写（update_from_output）**：`future.result()` 拿到 GPU 结果后，`scheduler.update_from_output()` 用它更新请求状态、产出 `EngineCoreOutputs`。

#### 4.4.2 核心流程

一轮 `step` 的完整时序：

```
step():
  ① 若 scheduler 无请求          → 直接返回 {}（空步骤）
  ② scheduler_output = scheduler.schedule()
        ├─ 决定本轮 prefill/decode 的请求集合与 token 数
        └─ 产出 SchedulerOutput（含块表增量、新请求等）

  ③ future = executor.execute_model(scheduler_output, non_block=True)
        └─ 非阻塞！把工作派发给 worker 进程，立刻返回 Future
           （GPU forward 在 worker 进程里跑，与本进程的 CPU 工作重叠）

  ④ grammar_output = scheduler.get_grammar_bitmask(scheduler_output)
        └─ CPU 工作：计算结构化输出的语法掩码（与 ③ 的 GPU forward 重叠）

  ⑤ model_output = future.result()      ← 阻塞，等 GPU 结果回来
        └─ 得到 ModelRunnerOutput（含采样前的 logits / 已采样 token）

  ⑥ _process_aborts_queue()              ← 处理执行期间发生的 abort

  ⑦ engine_core_outputs = scheduler.update_from_output(scheduler_output, model_output)
        ├─ 把采样出的 token 追加到请求
        ├─ 判定哪些请求完成（EOS / 达到 max_tokens）
        └─ 产出要发回客户端的 EngineCoreOutputs

  return engine_core_outputs, 是否真的执行了模型
```

**为什么 ③ 用 `non_block=True` 返回 Future？** 这是 V1 提升吞吐的关键设计之一（见 u3-l1）。如果 ③ 阻塞等 GPU，那么 ④ 的语法掩码计算就必须等 GPU 算完才开始，白白浪费了 GPU 在算时 CPU 本可以干活的时间。用 Future 后，**④ 的 CPU 工作（语法掩码）与 ③ 的 GPU 前向并行**，等 ④ 做完，⑤ `future.result()` 时 GPU 往往也快好了。

在 `step_with_batch_queue`（流水线并行场景）里，这个重叠被进一步放大：可以**调度下一批**的同时**等待上一批**的结果，从而消除流水线气泡。

#### 4.4.3 源码精读

`step` 方法的完整实现：[vllm/v1/engine/core.py:584-614](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/core.py#L584-L614)

```python
def step(self) -> tuple[dict[int, EngineCoreOutputs], bool]:
    """Schedule, execute, and make output.

    Returns tuple of outputs and a flag indicating whether the model
    was executed.
    """
    if not self.scheduler.has_requests():
        return {}, False
    scheduler_output = self.scheduler.schedule(self._should_throttle_prefills())
    future = self.model_executor.execute_model(scheduler_output, non_block=True)
    grammar_output = self.scheduler.get_grammar_bitmask(scheduler_output)
    with (
        self.capture_iteration_details(scheduler_output) as iteration_details,
        self.log_error_detail(scheduler_output),
    ):
        model_output = future.result()
        if model_output is None:
            model_output = self.model_executor.sample_tokens(grammar_output)

    # Before processing the model output, process any aborts that happened
    # during the model execution.
    self._process_aborts_queue()
    engine_core_outputs = self.scheduler.update_from_output(
        scheduler_output, model_output
    )
    self._attach_iteration_details(engine_core_outputs, iteration_details)

    return engine_core_outputs, scheduler_output.total_num_scheduled_tokens > 0
```

逐行解读：

- **第 593-594 行**：若调度器里没有请求，直接返回空输出。这是「空步骤」的快速路径，避免无谓的调度开销。
- **第 595 行**：`scheduler.schedule()` 是调度的真正入口（详见 u4-l2）。`_should_throttle_prefills()` 在普通 `EngineCoreProc` 里恒返回 `False`（见第 579-582 行），只有 DP MoE 场景才会节流 prefill。
- **第 596 行**：`execute_model(..., non_block=True)` 返回 `Future[ModelRunnerOutput | None]`。
- **第 597 行**：在等 Future 期间，先算结构化输出的语法掩码（CPU 工作）。
- **第 602 行**：`future.result()` 阻塞取回 GPU 结果。
- **第 603-604 行**：若 `model_output is None`（表示执行器只算了 logits 没采样），用语法掩码做采样 `sample_tokens(grammar_output)`。
- **第 609-611 行**：`update_from_output` 是回写——把 token 追加到请求、判定完成、产出输出。

`execute_model` 的 `non_block` 契约（重载签名）：[vllm/v1/executor/abstract.py:215-227](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/executor/abstract.py#L215-L227)

```python
@overload
def execute_model(
    self, scheduler_output: SchedulerOutput, non_block: Literal[True] = True
) -> Future[ModelRunnerOutput | None]:
    pass

def execute_model(
    self, scheduler_output: SchedulerOutput, non_block: bool = False
) -> ModelRunnerOutput | None | Future[ModelRunnerOutput | None]:
    output = self.collective_rpc(
        "execute_model", args=(scheduler_output,), non_block=non_block
    )
    return output[0]
```

> 说明：`non_block=True` 时返回 `Future`；`non_block=False` 时直接返回结果。底层走 `collective_rpc` 把调用派发给 worker 进程。worker 进程内的 `execute_model` 真正跑 forward（见 u5-l2、u5-l3）。

`step_fn` 如何被 `_process_engine_step` 调用，并把输出塞进 `output_queue`：[vllm/v1/engine/core.py:1435-1452](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/core.py#L1435-L1452)

```python
def _process_engine_step(self) -> bool:
    """Called only when there are unfinished local requests."""

    # Step the engine core.
    outputs, model_executed = self.step_fn()
    # Put EngineCoreOutputs into the output queue.
    for output in outputs.items() if outputs else ():
        self.output_queue.put_nowait(output)
    # Post-step hook.
    self.post_step(model_executed)

    # If no model execution happened but there is still scheduler work
    # (e.g. WAITING_FOR_REMOTE_KVS or delayed KV connector frees), yield
    # the GIL briefly to allow background transfer threads to make progress.
    if not model_executed and self.scheduler.has_requests():
        time.sleep(0.001)

    return model_executed
```

> 说明：`step_fn()` 的返回是 `(outputs_dict, model_executed)`。`outputs.items()` 把「按 client_index 分组的输出」逐个 `put_nowait` 进 `output_queue`，随后输出线程会把它们序列化发回 API Server。`post_step` 是钩子，用于在非异步调度下取回推测解码的 draft token（见 u9-l3）。最后那个 `time.sleep(0.001)` 是一个细节：当某步没有真正执行模型、但调度器还有事（比如在等远程 KV 迁移），主动让出 GIL 一小会儿，让后台传输线程有机会推进。

#### 4.4.4 代码实践

**实践目标**：完整追踪「EngineCore 从收到新请求到调用 worker `execute_model`」的关键步骤，画出一轮 step 的处理时序。

**操作步骤（源码阅读型）**：

1. **请求入口**：从 `process_input_sockets`（第 1645 行）开始。当输入线程收到一个 `ADD` 帧，调用 `preprocess_add_request`（第 969 行）做预处理，再把 `(ADD, Request)` 放进 `input_queue`。
2. **主循环取请求**：`run_busy_loop` → `_process_input_queue`（第 1404 行）从 `input_queue.get()` 取出请求 → `_handle_client_request`（第 1507 行）按类型分发，`ADD` 走 `add_request`（第 439 行），最终 `self.scheduler.add_request(request)` 把请求**登记**到调度器。
3. **跑一步**：`_process_engine_step`（第 1435 行）调 `self.step_fn()`，即 `step`（第 584 行）。
4. **调度**：`step` 内 `scheduler.schedule()`（第 595 行）产出 `SchedulerOutput`。
5. **执行**：`executor.execute_model(scheduler_output, non_block=True)`（第 596 行）把工作派发给 worker 进程，返回 `Future`。
6. **回写**：`future.result()`（第 602 行）取回结果，`scheduler.update_from_output`（第 609 行）更新状态、产出 `EngineCoreOutputs`。
7. **发输出**：`_process_engine_step` 把输出 `put_nowait` 进 `output_queue`（第 1442 行），输出线程 `process_output_sockets`（第 1743 行）序列化后经 ZMQ 发回 API Server。

**需要观察的现象**：注意第 5 步 `execute_model` 与第 6 步 `future.result()` 之间，插入了第 597 行的 `get_grammar_bitmask`（语法掩码计算）。这说明「CPU 的语法计算」与「GPU 的前向」是**并行**的——这是 `non_block=True` 的直接收益。

**预期结果**：画出如下时序图（文字版）：

```
输入线程:   ZMQ.recv → preprocess → input_queue.put ──────┐
                                                          │
主线程:   get(req) → add_request(登记) ─┐                 │
                                         │                 │
        step: schedule() ──┐             │                 │
                            │             │                 │
        execute_model(non_block) ──┐     │                 │
                                   │Future                 │
        get_grammar_bitmask (CPU) ◄┤     │  ← CPU/GPU 并行 │
                                   │     │                 │
        future.result() ◄──────────┘     │                 │
        update_from_output ──────────────┘                 │
        output_queue.put ──────────────────────────────────┘
输出线程:   output_queue.get → ZMQ.send
```

**待本地验证**：本实践为源码追踪，无需运行。若本地有 GPU，可在 `step` 的第 596 行前后各加一行日志（注意：这会修改源码，仅用于本地学习，勿提交），打印时间戳，观察 `execute_model` 派发与 `future.result()` 返回之间的间隔，从而直观感受 GPU forward 的耗时与 CPU 并行的窗口。

#### 4.4.5 小练习与答案

**练习 1**：`step` 返回值的第二个元素（`model_executed`）是怎么算出来的？它为 `False` 时意味着什么？

> **参考答案**：`return engine_core_outputs, scheduler_output.total_num_scheduled_tokens > 0`——即「这一步实际调度的 token 数是否大于 0」。为 `False` 表示这一步没有真正执行模型（比如所有请求都在等远程 KV 迁移）。`_process_engine_step` 据此决定是否 `time.sleep(0.001)` 让出 GIL，让后台传输线程推进。

**练习 2**：如果把第 596 行的 `non_block=True` 改成 `non_block=False`（同步阻塞执行），会损失什么？输出会变吗？

> **参考答案**：**输出不会变**（最终算的东西一样），但会**损失性能**：同步阻塞会让第 597 行的 `get_grammar_bitmask` 必须等 GPU 算完才开始，CPU 与 GPU 的并行窗口被抹掉。在流水线并行的 `step_with_batch_queue` 里影响更大——失去 `non_block` 就无法「调度下一批」与「等待上一批」重叠，流水线气泡重新出现。这正是 V1 坚持用 Future 派发执行的核心理由。

**练习 3**：`step` 里为什么要在 `update_from_output` **之前**先调 `_process_aborts_queue()`（第 608 行）？

> **参考答案**：因为模型执行（GPU forward）期间，可能有客户端发来了 `ABORT`。这些 abort 被放进了 `aborts_queue`。在用模型输出去更新请求状态**之前**先处理 abort，可以确保「执行期间被取消的请求」不会被错误地当作正常完成来回写输出，保证 abort 的及时性与一致性。

## 5. 综合实践

**任务**：把本讲四个最小模块串起来，用自己的话写一份「一次推理请求在 EngineCore 进程内的完整生命周期」说明，并标注每一步对应的源码行号。

**要求覆盖的环节**：

1. 请求如何从 ZMQ 进入 `input_queue`（4.2）。
2. `run_busy_loop` 如何被唤醒并取走请求（4.3）。
3. 请求如何被登记到调度器（`add_request`）。
4. 一轮 `step` 如何调度、执行、回写（4.4）。
5. 输出如何经 `output_queue` 发回（4.2）。
6. 空闲时循环如何阻塞、收到关闭信号如何退出（4.3）。

**建议产出形式**：一张时序图（可以是手绘或文字版）+ 一段不超过 200 字的文字说明。重点体现两个「并行」：

- **进程间并行**：API Server 进程 ↔ EngineCore 进程（经 ZMQ）。
- **进程内并行**：ZMQ IO 线程 ↔ 主循环线程（经 `queue.Queue`）；CPU 语法计算 ↔ GPU 前向（经 `Future`）。

**自检问题**（做完后回答）：

- 如果一条请求的 prefill 很长，被调度器切成多个 chunk（见 u4-l3），它在 EngineCore 里是「一次 `add_request` + 多次 `step`」还是「多次 `add_request`」？
  > 提示：是前者。`add_request` 只登记一次，分块 prefill 体现在**多轮 `step`** 里，每轮 `schedule()` 分配一部分 token，直到整个 prompt 算完。

## 6. 本讲小结

- **`EngineCore` 是「引擎的内层循环」**：它持有 `scheduler`、`model_executor` 和若干队列，把「调度 + 执行 + 产出」封装成可反复调用的 `step`，但不关心进程边界与请求来源。
- **`EngineCoreProc` 给 `EngineCore` 套上进程外壳**：用两条守护线程（输入/输出）处理 ZMQ IO，用 `input_queue`/`output_queue` 在 IO 线程与主循环间解耦，让 ZMQ 序列化与 GPU 前向并行。
- **`run_busy_loop` 是驱动循环**：三段式「取输入 → 发布统计 → 跑一步」，用 `has_work()` + 阻塞式 `input_queue.get()` 实现「有活就干、没活就睡」的低延迟忙等待。
- **`run_engine_core` 是子进程入口**：被 `CoreEngineProcManager` 当作 `multiprocessing.Process` 的 `target`，构造 `EngineCoreProc`、注册信号、调用 `run_busy_loop`。
- **一轮 `step` = 调度 → 执行 → 回写**：`scheduler.schedule()` 产出 `SchedulerOutput`，`executor.execute_model(non_block=True)` 返回 `Future` 让 CPU/GPU 并行，`future.result()` 取回结果后 `scheduler.update_from_output()` 回写状态并产出 `EngineCoreOutputs`。
- **协作式关闭**：`SIGTERM`/`SIGINT` 只设标志位 + 唤醒哨兵，真正的退出在下一轮 `_handle_shutdown()` 协作完成，避免在 step 执行中途被打断。

## 7. 下一步学习建议

- **向下追执行器**：本讲的 `execute_model` 把工作派发给了 worker 进程。下一讲 **u5-l2（GPU Worker 工作进程）** 讲解 worker 如何初始化设备、加载模型、真正跑 forward。
- **深入 ModelRunner**：worker 内部的 forward 由 `GPUModelRunner` 准备输入张量并调用模型。见 **u5-l3（ModelRunner 模型运行器）**。
- **回顾调度细节**：本讲的 `schedule()` 与 `update_from_output()` 是调度的两端，其内部算法在 **u4-l2（Scheduler 调度器核心）** 已有讲解，可对照阅读，理解「乐观计数」如何在 `update_from_output` 中被兑现。
- **进阶——流水线并行**：本讲主线是朴素的 `step`。若关心流水线并行的 batch queue 重叠机制，可精读 `step_with_batch_queue`（第 625 行起），它体现了 `non_block` Future 在多步重叠中的更强威力。
