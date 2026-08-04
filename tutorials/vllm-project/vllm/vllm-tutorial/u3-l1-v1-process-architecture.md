# V1 多进程架构总览

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 vLLM V1 架构中 **API Server / EngineCore / GPU Worker / DP Coordinator** 这四类进程各自的职责边界。
- 根据 `TP`（张量并行）、`PP`（流水线并行）、`DP`（数据并行）三个参数，计算一次部署会拉起多少个进程。
- 描述进程之间如何通过 **ZMQ** 通信，以及「多对多」拓扑是怎么连起来的。
- 理解「为什么要把调度和执行拆到不同进程」这一设计动机。

本讲是进阶层的第一讲，承接 u1-l3（仓库目录结构）建立的方位感，开始进入 V1 运行时的内部世界。

## 2. 前置知识

在正式开始前，先建立几个直觉。

### 2.1 「进程」vs「线程」

- **进程**：操作系统里独立的运行单元，各自拥有独立的内存地址空间。进程之间不能直接共享变量，必须通过显式通信（管道、队列、网络 socket 等）。
- **线程**：进程内的执行单元，多个线程共享同一块内存。

vLLM V1 用的是**多进程**而非多线程。原因后面会展开，核心一句话：**把相互独立的职责放进相互隔离的进程，让它们各自独占 CPU/GPU 资源、互不抢占，从而最大化吞吐。**

### 2.2 ZMQ（ZeroMQ）是什么

ZMQ 是一个高性能的消息库，可以理解为「加强版的 socket」。它提供了多种 socket 模式：

- `DEALER` / `ROUTER`：异步请求-应答，支持多对多路由。
- `PUSH` / `PULL`：单向流水线，发送端 PUSH、接收端 PULL，自动做负载均衡。
- `PUB` / `XSUB`：发布-订阅，一个发布者广播给多个订阅者。

vLLM 用 ZMQ 在进程之间传递「请求」「输出」「统计」「控制信号」。它相比 Python 内置 `multiprocessing.Queue` 的优势在于：**能跨进程、跨机器、跨语言（vLLM 还有 Rust 写的前端）**，并且**在 socket 收发时会释放 GIL**，从而让 CPU 与 GPU 计算重叠。

### 2.3 回顾并行度参数

承接前面讲义建立的术语：

| 参数 | 含义 |
| - | - |
| `TP`（tensor_parallel_size） | 把**一个模型的权重**切到多张卡上一起算。 |
| `PP`（pipeline_parallel_size） | 把**模型的不同层**放到不同卡上，按流水线推进。 |
| `DP`（data_parallel_size） | 复制**多个完整的模型副本**，各自处理不同的请求，互不通信。 |

一个关键等式：每张 GPU 上跑一个 worker，所以

\[
N_{\text{GPU}} = DP \times PP \times TP
\]

### 2.4 离线推理 vs 在线服务

- **离线推理**：用 `vllm.LLM` 类，在脚本里一次性塞入 prompt 拿结果，**进程内**完成。
- **在线服务**：用 `vllm serve` 起 HTTP 服务，常驻等待请求。

多进程架构在这两种场景下的「进程数」略有不同（离线模式每个 DP rank 一个 LLM 实例），但**核心拓扑一致**。本讲以在线服务为主线讲解。

---

## 3. 本讲源码地图

本讲涉及的关键源码文件：

| 文件 | 作用 |
| - | - |
| [docs/design/arch_overview.md](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/docs/design/arch_overview.md) | 官方架构总览文档，定义了各类进程、进程数量汇总表。 |
| [vllm/v1/engine/core.py](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/core.py) | `EngineCore`（调度+执行内循环）与 `EngineCoreProc`（ZMQ 外壳、busy loop）。 |
| [vllm/v1/engine/utils.py](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/utils.py) | 前端启动逻辑：分配 ZMQ 地址、拉起 engine 进程、握手。 |
| [vllm/v1/engine/coordinator.py](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/coordinator.py) | `DPCoordinator`，DP 部署下的负载均衡与 MoE wave 协调。 |
| [vllm/v1/engine/async_llm.py](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/async_llm.py) | `AsyncLLM`，API Server 侧连接 EngineCore 的异步客户端。 |

---

## 4. 核心概念与源码讲解

vLLM 官方文档开宗明义：

> vLLM V1 uses a multi-process architecture to separate concerns and maximize throughput.（V1 采用多进程架构来分离关注点、最大化吞吐）

见 [docs/design/arch_overview.md:81-84](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/docs/design/arch_overview.md#L81-L84)。

下面按五个最小模块展开：API Server、EngineCore、GPU Worker、DP Coordinator、ZMQ 通信拓扑。

### 4.1 API Server 进程

#### 4.1.1 概念说明

API Server 是系统的「门面」。它做三件事：

1. **接 HTTP 请求**：接收 OpenAI 兼容的 `/v1/chat/completions` 等请求。
2. **做输入处理**：把文本 tokenize 成 token id，把图像/音频等多模态数据预处理成张量（这一步是 CPU 密集型，会占用多个线程）。
3. **把结果流式推回客户端**：边生成边返回。

它**不碰 GPU**，也**不跑模型 forward**。它只负责「翻译」HTTP 协议与内部协议，然后通过 ZMQ 把请求交给后端的 EngineCore。

为什么要把这层独立成进程？因为输入处理（尤其多模态图像解码）会消耗大量 CPU，如果不隔离，就会和「调度+执行」抢 CPU，拖慢 GPU 的每一步（step）。

#### 4.1.2 核心流程

```text
HTTP 请求
   │
   ▼
[API Server 进程]  ── tokenize / 多模态预处理（CPU 多线程）
   │
   ▼  通过 ZMQ 发送 EngineCoreRequest
[EngineCore 进程] （见 4.2）
   │
   ▲  通过 ZMQ 接收 EngineCoreOutputs
   │
[API Server 进程]  ── 反 tokenize、组装流式响应
   │
   ▼
HTTP 响应（逐 token 流式）
```

默认情况下只有 **1 个 API Server 进程**。但当启用数据并行（DP>1）时，它的数量会自动随 DP 扩展（也可用 `--api-server-count` 手动指定）。**每个 API Server 会连接到所有 EngineCore**，形成多对多拓扑——任何一台 API Server 都能把请求路由给任何一个 EngineCore。

#### 4.1.3 源码精读

文档对 API Server 进程的描述见 [docs/design/arch_overview.md:85-91](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/docs/design/arch_overview.md#L85-L91)，其中提到两点关键信息：

- 「Each API server connects to **all** engine cores via ZMQ in a **many-to-many** topology」——多对多拓扑。
- 多模态加载用多线程，数量由 `VLLM_MEDIA_LOADING_THREAD_COUNT`（默认 8）控制。

API Server 这边的「连接对象」是 `AsyncLLM`。它在构造时通过 `EngineCoreClient.make_async_mp_client` 建立到 EngineCore 的 ZMQ 通道：

[ vllm/v1/engine/async_llm.py:148-156 ](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/async_llm.py#L148-L156)：`AsyncLLM` 把请求处理拆成 `InputProcessor`（输入）、`OutputProcessor`（输出），再用 `EngineCoreClient` 当到 EngineCore 的代理。

> 说明：`AsyncLLM` 本身不是「进程」，而是 API Server 进程内代表「引擎客户端」的对象。真正和 EngineCore 进程对话的是它持有的 `EngineCoreClient`。

#### 4.1.4 代码实践

**实践目标**：在源码层面定位 API Server 的多线程输入处理配置项。

**操作步骤**：

1. 打开 [vllm/v1/engine/async_llm.py:72-156](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/async_llm.py#L72-L156)，确认 `AsyncLLM.__init__` 里构造了 `InputProcessor` 与 `EngineCoreClient`。
2. 在仓库内搜索 `VLLM_MEDIA_LOADING_THREAD_COUNT` 的定义。

**需要观察的现象**：找到这个环境变量的默认值是 8，并理解它对应「一个 API Server 进程内有 8 个线程做多模态加载」。

**预期结果**：能说出「API Server 进程是 CPU 侧的多线程输入处理器，独立于 GPU 侧」。

#### 4.1.5 小练习与答案

**练习 1**：如果一个请求里带一张大图，是 API Server 进程还是 EngineCore 进程负责解码图像？

**答案**：API Server 进程。输入处理（含多模态解码）发生在 API Server 侧的 `InputProcessor`，做完后才通过 ZMQ 把张量送给 EngineCore。

**练习 2**：为什么默认只有 1 个 API Server，而 DP>1 时会自动扩展到 DP 个？

**答案**：DP>1 时有多个 EngineCore，单个 API Server 容易成为 tokenize/多模态处理的 CPU 瓶颈；让 API Server 数与 DP 对齐，可并行处理输入，避免前端排队。

---

### 4.2 EngineCore 引擎核心进程

#### 4.2.1 概念说明

EngineCore 是 V1 的「大脑」。它把**调度**和**执行编排**放在一个进程里，但**不直接在 CPU 上跑模型计算**——真正的矩阵乘法在 GPU 上由 Worker 完成。EngineCore 负责：

- 跑**调度器**（Scheduler）：每一步决定「这一 step 处理哪些请求、哪些 token」。
- 管理 **KV 缓存**的显存预算（实际显存在 Worker 进程的 GPU 上，但分配策略在 EngineCore）。
- **协调 GPU Worker**：通过 executor 把 `SchedulerOutput` 派发给 worker，等它们算完 logits，再做采样和输出聚合。

数量上：**每个 DP rank 一个 EngineCore 进程**。例如 `--data-parallel-size 4` 就有 4 个 EngineCore。

> 提示：这里说的「EngineCore 进程」在源码里对应 `EngineCoreProc`（带 ZMQ 外壳的版本）；纯逻辑内循环是 `EngineCore` 类。两者是继承关系。

#### 4.2.2 核心流程

EngineCore 跑一个 **busy loop**（忙循环），不断重复三步：

```text
┌─────────────────────────────────────────────┐
│  while 运行中:                                │
│    1) _process_input_queue()   # 收新请求     │
│    2) _maybe_publish_request_counts()  # 上报 │
│    3) _process_engine_step()   # 调度+执行    │
└─────────────────────────────────────────────┘
```

其中第 3 步 `_process_engine_step` 调用的 `step()` 方法是核心，它把「调度 → 执行 → 回写」串起来：

```text
step():
  scheduler_output = scheduler.schedule()           # 决定本 step 处理什么
  future = executor.execute_model(scheduler_output) # 派发给 worker（异步）
  model_output = future.result()                    # 拿回 logits/采样结果
  engine_core_outputs = scheduler.update_from_output(...)  # 更新请求状态
  return engine_core_outputs
```

这个 `execute_model` 是**非阻塞**的（`non_block=True`），意味着 EngineCore 可以在 GPU 算的同时去做别的事（比如收下一个请求）。这正是「把调度和执行放一起但仍能高效」的关键。

#### 4.2.3 源码精读

[ vllm/v1/engine/core.py:103-113 ](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/core.py#L103-L113)：`EngineCore` 类的文档字符串与构造签名。注释明确写道它是「Inner loop of vLLM's Engine」。构造时依次创建 `model_executor`（executor）、初始化 KV 缓存、创建 scheduler（见 [core.py:143-168](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/core.py#L143-L168)）。

[ vllm/v1/engine/core.py:584-614 ](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/core.py#L584-L614)：`step()` 方法——调度、执行、回写的完整一轮。注意 `execute_model(..., non_block=True)`，这是「调度与执行重叠」的入口。

[ vllm/v1/engine/core.py:1008-1011 ](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/core.py#L1008-L1011)：`EngineCoreProc` 类，注释明确「ZMQ-wrapper for running EngineCore in **background process**」——这就是「进程」的体现。

[ vllm/v1/engine/core.py:1378-1389 ](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/core.py#L1378-L1389)：`run_busy_loop()`——EngineCore 进程的主循环，正是「收请求 → 上报 → step」三步。

#### 4.2.4 代码实践

**实践目标**：跟踪一轮 EngineCore `step()` 的调用链。

**操作步骤**：

1. 打开 [vllm/v1/engine/core.py:1378](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/core.py#L1378)，看 `run_busy_loop` 如何循环调用 `_process_engine_step`。
2. 跳到 [core.py:1435-1452](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/core.py#L1435-L1452) 的 `_process_engine_step`，确认它调用 `self.step_fn()`（即 `step()`）。
3. 再回到 [core.py:584-614](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/core.py#L584-L614) 的 `step()`，看清 `schedule → execute_model → update_from_output`。

**需要观察的现象**：整条链路上**没有任何 GPU kernel 调用代码**——GPU 计算被封装在 `executor.execute_model` 里，由 Worker 进程执行。

**预期结果**：能画出 `run_busy_loop → _process_engine_step → step → execute_model` 的调用链，并指出 EngineCore 与 Worker 的边界就在 `execute_model`。

#### 4.2.5 小练习与答案

**练习 1**：`EngineCore` 和 `EngineCoreProc` 是什么关系？

**答案**：`EngineCore` 是纯逻辑内循环类（调度+执行编排）；`EngineCoreProc` 继承它，额外加上 ZMQ 收发外壳和 busy loop，使其能在**独立进程**里运行。

**练习 2**：`execute_model(non_block=True)` 为什么对吞吐很重要？

**答案**：非阻塞返回一个 `Future`，EngineCore 在 GPU 算的同时可以收下一个请求或做其它 CPU 工作，让「调度/IO」与「GPU 计算」重叠，避免 CPU 空等 GPU。

---

### 4.3 GPU Worker 工作进程

#### 4.3.1 概念说明

遵循业界惯例：**一块 GPU 由一个专门的 worker 进程控制**。Worker 的职责是：

- 加载模型权重到自己的 GPU。
- 执行 **forward pass**（前向计算）。
- 管理本卡显存，包括 KV cache 的实际存储。

数量：**每个 DP rank 内有 \(PP \times TP\) 个 worker，全局共 \(N = DP \times PP \times TP\) 个**（即每个 GPU 一个）。

这些 worker 由 **Executor**（执行器）统一拉起和管理。一个 EngineCore 进程持有一个 executor，executor 再 fork 出属于它的若干 worker 进程。

#### 4.3.2 核心流程

```text
[EngineCore 进程]
   │  scheduler.schedule() → SchedulerOutput
   ▼
[Executor] ──派发──▶ [Worker0(GPU0)] ┐
                ──派发──▶ [Worker1(GPU1)] ├── 各自跑 forward，TP 时互做 all-reduce
                ──派发──▶ [Worker2(GPU2)] │   PP 时按流水线接力
                ──派发──▶ [Worker3(GPU3)] ┘
   │                       │
   │  ◀──── 聚合 logits ────┘
   ▼
采样、生成 token
```

关键点：worker 之间通过 **TP（all-reduce/all-gather）** 和 **PP（点对点传激活）** 通信，但这些都是 GPU 上的集合通信（NCCL 等），不走 ZMQ。ZMQ 只用于 EngineCore 与 API Server/Coordinator 之间。

#### 4.3.3 源码精读

文档对 worker 的定义见 [docs/design/arch_overview.md:101-107](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/docs/design/arch_overview.md#L101-L107)，明确：

- 「Each GPU is managed by a dedicated worker process.」
- 「total number of GPU worker processes equals `tensor_parallel_size x pipeline_parallel_size` per engine core.」

EngineCore 通过 `executor_class(vllm_config)` 创建执行器，见 [vllm/v1/engine/core.py:132](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/core.py#L132)（`self.model_executor = executor_class(vllm_config)`）。随后 executor 内部 fork 出 worker 进程。worker 的具体实现位于 `vllm/v1/worker/gpu_worker.py`、进程管理位于 `vllm/v1/executor/multiproc_executor.py`（本讲不深入，留待 u5-l2 详解）。

> 每个 worker 还有 `rank`（全局编排用）和 `local_rank`（分配 GPU 设备、访问本地文件/共享内存用）两个标识，文档在 [arch_overview.md:185-194](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/docs/design/arch_overview.md#L185-L194) 有说明。

#### 4.3.4 代码实践

**实践目标**：确认「EngineCore 通过 executor 派发计算，自己不跑 GPU kernel」。

**操作步骤**：

1. 打开 [vllm/v1/engine/core.py:132](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/core.py#L132)，看 `self.model_executor = executor_class(vllm_config)`。
2. 在 [core.py:596](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/core.py#L596) 的 `self.model_executor.execute_model(...)` 处停下，意识到这里就是「EngineCore ↔ Worker」的边界。

**需要观察的现象**：从 `EngineCore` 类内看不到任何 `torch` 张量运算或 CUDA kernel 调用——全部委托给 `model_executor`。

**预期结果**：能说清 EngineCore 与 Worker 的职责切分：EngineCore 管调度与编排，Worker 管实际 GPU 计算。

#### 4.3.5 小练习与答案

**练习 1**：4 卡 TP=4、DP=1 的部署有几个 worker 进程？

**答案**：\(N = DP \times PP \times TP = 1 \times 1 \times 4 = 4\) 个 worker 进程，正好一卡一个。

**练习 2**：worker 之间的集合通信（all-reduce）走 ZMQ 吗？

**答案**：不走。worker 间用 GPU 上的集合通信库（如 NCCL）直接通信。ZMQ 只用于「API Server/Coordinator ↔ EngineCore」这一层，跨进程、跨机器的控制流与数据流。

---

### 4.4 DP Coordinator 协调进程

#### 4.4.1 概念说明

DP Coordinator 是**有条件**才出现的进程：只有当数据并行 `DP > 1` 时才存在。它充当「多个 EngineCore 与一个或多个 API Server 之间的中间人」，做两件事：

1. **负载均衡统计**：收集各 EngineCore 的等待/运行队列长度，发布给所有 API Server，帮它们决定把请求路由给哪个 EngineCore（仅 internal/hybrid 负载均衡模式）。
2. **MoE wave 协调**：当模型是 MoE（混合专家）时，多个 DP rank 之间需要同步前向 pass，Coordinator 负责协调「request wave」（请求波次）的启动与暂停。

#### 4.4.2 核心流程

```text
       ┌─────────────────────┐
       │  DP Coordinator     │
       │  (仅 DP>1 时存在)    │
       └──┬───────────────▲──┘
   收集stats│               │ 广播 START_DP_WAVE
          ▼               │
[EngineCore0] [EngineCore1] [EngineCore2] [EngineCore3]
          ▲               ▲
          └─── 发布负载stats 给 ──┘
                 所有 API Server
```

对于 MoE 模型，各 DP rank 通过一个 all-reduce 操作判断「全局是否还有未完成请求」，达成一致后才集体进入暂停状态，等下一个 wave 启动。这个机制在源码里叫「wave」。

#### 4.4.3 源码精读

文档对 DP Coordinator 的定义见 [docs/design/arch_overview.md:109-115](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/docs/design/arch_overview.md#L109-L115)：当 `DP > 1` 时有且仅有 1 个 Coordinator 进程。

何时需要 Coordinator 由配置对象的 `needs_dp_coordinator` 属性决定，见 [vllm/config/vllm.py:661-673](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/config/vllm.py#L661-L673)。规则是：

- **MoE 模型且 DP>1**：总是需要（即使外部负载均衡也要做 wave 协调）。
- **非 MoE 模型且 internal/hybrid 负载均衡**：需要（做统计发布）。

`DPCoordinator` 类本身的注释把它的职责讲得很清楚，见 [vllm/v1/engine/coordinator.py:23-57](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/coordinator.py#L23-L57)：收集 stats、追踪 wave 状态、广播 START_DP_WAVE。

Coordinator 是在哪里被拉起的？在 [vllm/v1/engine/utils.py:1091-1108](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/utils.py#L1091-L1108) 的 `launch_core_engines` 里，当 `vllm_config.needs_dp_coordinator` 且是 rank 0 时，`DPCoordinator(...)` 被实例化（它内部 `multiprocessing.Process` 起一个独立进程）。

对于 MoE 的 DP 场景，EngineCore 用的是特殊子类 `DPEngineCoreProc`（见 [vllm/v1/engine/core.py:1918-1921](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/core.py#L1918-L1921)），它通过 all-reduce 同步各 rank 状态（[core.py:2171-2188](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/core.py#L2171-L2188) 的 `_has_global_unfinished_reqs`）。

#### 4.4.4 代码实践

**实践目标**：理解 Coordinator 的启动条件。

**操作步骤**：

1. 打开 [vllm/config/vllm.py:661-673](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/config/vllm.py#L661-L673)，读 `needs_dp_coordinator` 的判定逻辑。
2. 打开 [vllm/v1/engine/utils.py:1091-1108](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/utils.py#L1091-L1108)，看 `run_coordinator` 条件里同时要求 `dp_rank == 0`。

**需要观察的现象**：Coordinator 由 **rank 0 所在节点**启动，且只在 DP>1 时才进这个分支。

**预期结果**：能解释「DP=1 时根本没有 Coordinator 进程；DP>1 时恰好 1 个，且住在 rank 0」。

#### 4.4.5 小练习与答案

**练习 1**：一个非 MoE 模型、DP=4、外部负载均衡（external LB）的部署，会有 Coordinator 进程吗？

**答案**：不会有。`needs_dp_coordinator` 对非 MoE 模型仅在 internal/hybrid LB 下为真；external LB 时不做内部统计发布，所以不需要 Coordinator。（注：若改为 MoE 模型，即使 external LB 也会因为 wave 协调而需要 Coordinator。）

**练习 2**：Coordinator 与 EngineCore 之间传的是什么？

**答案**：EngineCore 把自己的请求队列统计和 wave 状态通过 ZMQ 发给 Coordinator；Coordinator 再发布给前端，并反向广播 `START_DP_WAVE` 控制信号启动下一波。

---

### 4.5 ZMQ 通信拓扑与进程数量计算

#### 4.5.1 概念说明

前三类进程之间靠 ZMQ 通信。理解拓扑的关键是一个数据结构 `EngineZmqAddresses`，它定义了一个 EngineCore 需要「连到哪些 socket」。同时，整个部署的进程总数由一个简单公式给出。

#### 4.5.2 核心流程

**ZMQ 地址包**：每个 EngineCore 持有一组地址，分别是

- `inputs`：接收请求的 socket 地址列表（每个前端一个）。
- `outputs`：发送响应的 socket 地址列表（每个前端一个）。
- `coordinator_input` / `coordinator_output`：与 Coordinator 通信的地址（可选）。

EngineCore 进程内开了**两条后台 IO 线程**，分别处理输入和输出：

```text
EngineCoreProc 内部：
  主线程:      run_busy_loop()  ← 调度+执行
  输入线程:    process_input_sockets()  ← DEALER socket 收请求 → input_queue
  输出线程:    process_output_sockets() ← output_queue → PUSH socket 发响应
```

引擎侧用 `DEALER`（输入）和 `PUSH`（输出）；前端侧是 `ROUTER`/`PULL`。一个 EngineCore 会为**每个前端**各建一个输入 socket、一个输出 socket——这正是「多对多」的实现：任何前端都能路由到任何 EngineCore，反之亦然。

**进程数量公式**：设 API Server 数为 \(A\)（默认 \(A = DP\)），则

\[
N_{\text{进程}} = A + DP + N_{\text{GPU}} + \begin{cases}1 & DP > 1 \\ 0 & DP = 1\end{cases}
\]

其中 \(N_{\text{GPU}} = DP \times PP \times TP\)。

#### 4.5.3 源码精读

[ vllm/v1/engine/utils.py:61-74 ](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/utils.py#L61-L74)：`EngineZmqAddresses` 数据类，`inputs`/`outputs` 是「每个前端一个地址」的列表，`coordinator_input/output` 可选。

[ vllm/v1/engine/utils.py:1005-1050 ](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/utils.py#L1005-L1050)：`get_engine_zmq_addresses`，按 `num_api_servers` 数量为 `inputs`/`outputs` 各分配对应数量的地址；同机用 IPC（`get_open_zmq_ipc_path`），跨机用 TCP。

[ vllm/v1/engine/core.py:1645-1741 ](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/core.py#L1645-L1741)：`process_input_sockets`——为 `input_addresses` 列表里**每一个前端地址**建一个 `zmq.DEALER` socket（[core.py:1661-1668](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/core.py#L1661-L1668)），即「连接到所有前端」。

[ vllm/v1/engine/core.py:1743-1811 ](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/core.py#L1743-L1811)：`process_output_sockets`——为每个前端建一个 `zmq.PUSH` socket，按 `client_index` 把输出推到对应前端。

[ vllm/v1/engine/utils.py:120-173 ](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/utils.py#L120-L173)：`CoreEngineProcManager`——用 `context.Process(target=EngineCoreProc.run_engine_core, ...)` fork 出 EngineCore 进程（[utils.py:164-171](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/utils.py#L164-L171)），进程名按 `EngineCore_DP{rank}` 命名。

[ vllm/v1/engine/utils.py:1206-1339 ](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/utils.py#L1206-L1339)：`wait_for_engine_startup`——前端与各 EngineCore 之间通过 `HELLO` → `READY` 两阶段握手，确认连接与配置一致性（[utils.py:1292-1333](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/utils.py#L1292-L1333)）。

**进程数量汇总表**见 [docs/design/arch_overview.md:117-127](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/docs/design/arch_overview.md#L117-L127)：

| 进程类型 | 数量 | 说明 |
| - | - | - |
| API Server | \(A\)（默认 \(DP\)） | HTTP 与输入处理 |
| Engine Core | \(DP\)（默认 1） | 调度与 KV 缓存管理 |
| GPU Worker | \(N\)（\(=DP \times PP \times TP\)） | 每卡一个，跑 forward |
| DP Coordinator | \(DP>1\) 时 1，否则 0 | 跨 DP rank 负载均衡/wave 协调 |
| **合计** | **\(A + DP + N\)（\(DP>1\) 再 +1）** | |

文档给出的两个标准例子（[arch_overview.md:129-143](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/docs/design/arch_overview.md#L129-L143)）：

- 4 卡 TP=4、DP=1：\(1 + 1 + 4 = 6\) 个进程。
- 8 卡 TP=2、DP=4：\(4 + 4 + 8 + 1 = 17\) 个进程。

#### 4.5.4 代码实践

**实践目标**：给定 TP=2、DP=4 的部署，列出各进程数量并画出通信拓扑。

**操作步骤**：

1. 用本讲公式计算：
   - API Server 数 \(A = DP = 4\)。
   - EngineCore 数 \(= DP = 4\)。
   - GPU 数 \(N = DP \times PP \times TP = 4 \times 1 \times 2 = 8\)。
   - Coordinator 数 \(= 1\)（因为 DP>1）。
   - 合计 \(4 + 4 + 8 + 1 = 17\)，与文档 [arch_overview.md:137-143](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/docs/design/arch_overview.md#L137-L143) 一致。
2. 画出拓扑（见下方）。
3. 用一句话回答「为什么要把调度和执行分到不同进程」。

**通信拓扑（ASCII）**：

```text
   API Server 0     API Server 1     API Server 2     API Server 3
       │ ZMQ DEALER/ROUTER 多对多 │           │                 │
       └────────────┬─────────────┴─────────────────┬───────────┘
                    │                               │
                ┌───▼──────────── DP Coordinator ───▼───┐   (1 个，做 LB/wave)
                │                                       │
   EngineCore0  EngineCore1   EngineCore2   EngineCore3   (各 1 个)
       │              │             │             │
     ┌─┴─┐          ┌─┴─┐         ┌─┴─┐         ┌─┴─┐
   W0   W1        W2   W3       W4   W5       W6   W7      (GPU worker，各 1/卡)
  GPU0 GPU1     GPU2 GPU3     GPU4 GPU5     GPU6 GPU7
```

要点：4 个 API Server 中的任何一个都能把请求路由给 4 个 EngineCore 中的任何一个（多对多）；4 个 EngineCore 与 Coordinator 互通统计与 wave 控制；每个 EngineCore 下挂 2 个 worker（TP=2）。

**需要观察的现象**：API Server 与 EngineCore 之间的连线是「全连接」的（4×4），不是一一对应。

**预期结果**：

- 进程数：API Server 4 + EngineCore 4 + Worker 8 + Coordinator 1 = **17**。
- 「为什么分进程」一句话答案：**让 CPU 密集的输入处理（API Server）、调度编排（EngineCore）和 GPU 计算（Worker）各自独占 CPU/进程，互不抢占 GIL 与 CPU 时间，从而让 GPU 每 step 都能被喂满，最大化吞吐；同时多进程便于按 TP/PP/DP 横向扩展到多机。**

#### 4.5.5 小练习与答案

**练习 1**：把上例改成 TP=4、DP=2、共 8 卡，进程总数是多少？

**答案**：\(A = DP = 2\)，EngineCore \(= DP = 2\)，Worker \(N = 2 \times 1 \times 4 = 8\)，Coordinator \(= 1\)。合计 \(2 + 2 + 8 + 1 = 13\) 个进程。

**练习 2**：为什么同机的 EngineCore 与前端用 IPC（如 `ipc:///tmp/xxx`）而不用 TCP？

**答案**：IPC（Unix 域 socket）在同机内延迟更低、不走网络栈；`get_engine_zmq_addresses` 在「所有 engine 都在本机」时（`client_local_only`）就用 `get_open_zmq_ipc_path()`，跨机才退化为 TCP（见 [utils.py:1042-1045](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/utils.py#L1042-L1045)）。

---

## 5. 综合实践

**任务**：自己推演一个 `--tensor-parallel-size 2 --pipeline-parallel-size 2 --data-parallel-size 2` 的单机部署（共 8 卡），完成下面三件事。

1. **算进程数**：写出 API Server、EngineCore、GPU Worker、DP Coordinator 各几个，给出总数，并与 [arch_overview.md:117-127](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/docs/design/arch_overview.md#L117-L127) 的汇总表逐项核对。

   参考解答：
   - API Server \(A = DP = 2\)
   - EngineCore \(= DP = 2\)
   - Worker \(N = DP \times PP \times TP = 2 \times 2 \times 2 = 8\)
   - Coordinator \(= 1\)
   - 合计 \(2 + 2 + 8 + 1 = 13\)

2. **画拓扑**：画出 2 个 API Server、2 个 EngineCore（每个 EngineCore 下挂 4 个 worker，其中 PP=2 级、TP=2 并）、1 个 Coordinator 的连接关系。标注哪条线是 ZMQ（API↔EngineCore、EngineCore↔Coordinator），哪条线是 GPU 集合通信（worker 间 TP all-reduce、PP 点对点）。

3. **追源码**：打开 [vllm/v1/engine/core.py:1378-1389](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/core.py#L1378-L1389) 的 `run_busy_loop`，在源码旁批注「这一行发生在哪个进程、跑在 CPU 还是 GPU 上」。预期你会发现：busy loop、schedule、execute_model 的编排都在 **EngineCore 进程的 CPU** 上，而真正的矩阵乘法在 **Worker 进程的 GPU** 上。

   > 提示：本实践为「源码阅读型实践」，无需 GPU 也能完成。若你后续有 GPU 环境，可在启动 vLLM 时用 `ps -ef | grep -E 'EngineCore|api_server|Worker|Coordinator'` 观察真实进程名，与本讲的命名（如 `EngineCore_DP{rank}`、`VLLM_DP_Coordinator`）对照。

---

## 6. 本讲小结

- V1 用**多进程架构**分离关注点：API Server（HTTP+输入处理）、EngineCore（调度+编排）、GPU Worker（GPU 计算）、DP Coordinator（DP 协调）。
- **进程数量**由公式 \(A + DP + N\)（\(DP>1\) 再 +1）给出，其中 \(N = DP \times PP \times TP\)；典型 TP=4/DP=1 是 6 个进程，TP=2/DP=4 是 17 个进程。
- **EngineCore** 跑 busy loop：`收请求 → step（schedule → execute_model → update_from_output）→ 上报`，其中 `execute_model` 非阻塞，使 CPU 调度与 GPU 计算重叠。
- **ZMQ 多对多拓扑**：每个 API Server 连到所有 EngineCore（DEALER/ROUTER 输入、PUSH/PULL 输出），同机用 IPC、跨机用 TCP。
- **DP Coordinator** 仅在 DP>1 时存在：非 MoE 做 LB 统计发布，MoE 额外做 wave 同步（`DPEngineCoreProc` 用 all-reduce 达成一致）。
- **设计动机**：让 CPU 输入处理、调度编排、GPU 计算各占独立进程，互不抢占 GIL/CPU，且便于 TP/PP/DP 横向扩展。

## 7. 下一步学习建议

本讲建立了「进程拓扑」的全景图。接下来建议沿着两条线深入：

- **沿请求流深入**：下一篇 u3-l2 讲 `VllmConfig`——这些并行参数（TP/PP/DP）和配置是如何被组织、传递的。之后 u3-l4 讲 `AsyncLLM` 这个前端客户端如何真正把请求发进 EngineCore。
- **沿执行链深入**：u5-l1 会精读 `EngineCore` 的 busy loop 与 `engine_step`，u5-l2 进入 GPU Worker，u5-l3 进入 ModelRunner。届时你会看到「execute_model 之后到底在 GPU 上发生了什么」。

建议阅读的源码顺序：先读 [docs/design/arch_overview.md](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/docs/design/arch_overview.md) 全文建立直觉，再读 [vllm/v1/engine/utils.py](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/utils.py) 看进程怎么被拉起，最后读 [vllm/v1/engine/core.py](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/core.py) 看 EngineCore 内循环。
