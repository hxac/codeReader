# Scheduler 核心与事件循环

> 单元 U3 · 调度器与连续批处理 · 第 1 讲
> 依赖讲义：u2-l3（请求端到端流转）

## 1. 本讲目标

在 u2 系列里，我们把一条请求在 `TokenizerManager → Scheduler → DetokenizerManager` 三进程环上的流转走了一遍，但当时把 **Scheduler 当作黑盒**——只知道它「拿到 token、吐出 token」。本讲打开这个黑盒。

读完本讲，你应当能够：

1. 说清 **Scheduler 进程在 SGLang 中的定位**：它是一个独立的 Python 进程，持有 GPU worker、KV 缓存、调度策略，靠一个**事件循环（event loop）**反复迭代，把请求编排成「批（batch）」交给 GPU 前向。
2. 理解调度器维护的 **三大运行时状态**：`waiting_queue`（等待队列）、`running_batch`（正在 decode 的批）、`last_batch`（上一轮跑过的批），以及它们在一轮迭代中如何被读/写。
3. 读懂 **`run_event_loop` 如何分派到两种模式**：`event_loop_normal`（经典串行）与 `event_loop_overlap`（CPU 调度与 GPU 计算**重叠**，即所谓的「零开销调度器」）。
4. 定位 **prefill / decode 两类批的构造入口**：`get_new_batch_prefill`（从等待队列挑请求组新批做 prefill）与 `update_running_batch`（维护正在 decode 的批，必要时回缩/淘汰），并理解一条请求从 `waiting_queue` 进入 `running_batch` 的生命周期。

本讲的视角是**调度编排（orchestration）**：谁在一轮迭代里被调用、以什么顺序、各自输出什么。至于「如何挑请求」（调度策略 LPM/FCFS/LOF）、「批的底层数据结构」（`Req`/`ScheduleBatch`）、「overlap 的工程细节」分别在 u3-l3、u3-l2、u3-l4 展开，本讲只在必要处点到。

---

## 2. 前置知识

在进入源码前，先用三段大白话建立直觉。

### 2.1 为什么需要「调度器」这个角色

大模型推理有两个截然不同的计算阶段：

- **Prefill（预填充）**：把整段 prompt 一次性喂给模型，算出这段 prompt 对应的 KV 缓存。这是一次「重计算」，特点是**计算量大、并行度高**（一次处理很多 token）。
- **Decode（解码）**：基于已有 KV 缓存，**一个 token 一个 token**地往后生成。特点是**计算量小、显存带宽敏感**（每步只处理 1 个 token，但要反复读写 KV）。

如果把请求一条一条排队跑，GPU 在 decode 阶段会大量闲置（算力用不满）。所以现代推理框架都用**连续批处理（continuous batching / iteration-level batching）**：每个「推理步」都重新组批——新请求的 prefill 可以和旧请求的 decode 拼到一起跑，请求完成了就立刻从批里摘掉。**Scheduler 就是负责「每一步到底把哪些请求、以什么模式（prefill/decode）拼成一个批交给 GPU」的那个角色。**

### 2.2 事件循环：调度器的「心跳」

Scheduler 是一个独立进程，它的工作就是一个**死循环**：

> 收请求 → 决定这一步跑什么批 → 把批交给 GPU 前向 → 处理前向结果（采样、写 KV、把 token 发回给 Detokenizer）→ 回到第一步。

这个死循环就是 **event loop（事件循环）**。每一圈叫**一轮迭代（iteration）**或一个**forward step**。SGLang 的吞吐和延迟，本质上取决于这个循环转得多快、多稳。

### 2.3 两种循环模式：normal 与 overlap

朴素的事件循环是**串行**的：

```
[收请求 + 组批 + 处理上一批结果] ──(CPU)──▶ [GPU 前向] ──(GPU)──▶ 等它跑完 ──▶ 下一轮
```

CPU 在干调度活的时候 GPU 闲着，GPU 在跑前向的时候 CPU 闲着——两头都浪费。

SGLang 的杀手锏是 **overlap（重叠）模式**：用一条独立的 **CUDA stream（schedule_stream）**跑 CPU 调度逻辑，让「处理上一批结果 + 为下一批组批」与「GPU 跑当前批」**同时进行**：

```
CPU (schedule_stream): [处理批N的结果] [为批N+1组批] ...
GPU (forward_stream):            [跑批N的前向]        [跑批N+1的前向] ...
```

只要 CPU 调度的耗时**短于** GPU 前向的耗时，CPU 开销就被完全藏起来了——这就是 README 里「零开销调度器（zero-overhead scheduler）」的字面含义。代价是代码复杂度：结果处理被**延迟一轮**，需要用 `result_queue` 缓存上一批的结果。两种模式的差异是本讲的重头戏。

> 术语提示：CUDA stream 是 GPU 上的「任务队列」，同一 stream 内按序执行，不同 stream 间可并行。SGLang 用 `forward_stream`（跑模型前向）和 `schedule_stream`（跑 CPU 调度）这两条流来实现重叠。

---

## 3. 本讲源码地图

本讲涉及三个核心文件，职责如下：

| 文件 | 作用 |
| --- | --- |
| [scheduler.py](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py) | `Scheduler` 类本体：事件循环、批构造、前向触发、结果分派。是本讲的绝对主角，4600+ 行。 |
| [scheduler_components/request_receiver.py](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler_components/request_receiver.py) | `SchedulerRequestReceiver`：每轮迭代开头负责**从 TokenizerManager 经 ZMQ 拉取新请求**，并在 TP/PP 多卡间广播。 |
| [scheduler_components/batch_result_processor.py](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler_components/batch_result_processor.py) | `SchedulerBatchResultProcessor`：每轮迭代结尾负责**消化前向结果**（采样 token、更新请求状态、把文本流式发回）。 |
| [schedule_batch.py](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/schedule_batch.py) | `ScheduleBatch` / `Req` / `NextBatchPlan`：批与请求的数据结构。本讲只用到 `NextBatchPlan`（批构造的返回类型）。 |

模块化背景：`scheduler_components/` 目录是把原本臃肿的 `Scheduler` 拆出来的**协作组件**（receiver、result processor、metrics reporter、output streamer 等），`Scheduler` 在 `__init__` 里把它们作为字段持有并调用。这种「持有并调用」而非 mixin 的组合方式，正是项目代码规范（见 `.claude/rules/general-code-style.md`）推崇的写法。

---

## 4. 核心概念与源码讲解

### 4.1 Scheduler 的进程角色与构造

#### 4.1.1 概念说明

`Scheduler` 是一个**进程级单例**：每个 TP rank（每张 GPU）对应一个 Scheduler 进程。它由 `run_scheduler_process` 在子进程里构造（承接 u2-l1 讲过的多进程拓扑），构造完成后立即进入 `run_event_loop` 阻塞循环，直到收到 `ShutdownReq` 才优雅退出。

Scheduler 类声明里挂了一长串 mixin（`SchedulerDisaggregationDecodeMixin`、`SchedulerPPMixin` 等），这些 mixin 提供的是**特定部署形态**（PD 分离、流水线并行）的事件循环变体。本讲聚焦最常用的「单进程 + 普通推理」形态，即 `event_loop_normal` / `event_loop_overlap`。

#### 4.1.2 核心流程

`Scheduler` 的生命周期可以概括为：

```
run_scheduler_process()          # 进程入口（scheduler.py:4593）
  ├─ Scheduler(server_args, ...) # 构造：解析参数、建 GPU worker、建 KV 缓存、建组件   (scheduler.py:313)
  │    ├─ init_running_status()  # 初始化三大状态：waiting_queue / running_batch / last_batch
  │    ├─ request_receiver = SchedulerRequestReceiver(...)     # 持有接收组件
  │    └─ batch_result_processor = SchedulerBatchResultProcessor(...)  # 持有结果处理组件
  ├─ scheduler.run_event_loop()  # 进入事件循环（阻塞）
  └─ finally: release_host_resources()
```

#### 4.1.3 源码精读

进程入口 `run_scheduler_process` 构造 Scheduler 并启动循环——注意「构造完立刻 `run_event_loop`」这个顺序：

[python/sglang/srt/managers/scheduler.py:4642-4658](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L4642-L4658) —— 在子进程里 `Scheduler(...)` 构造完成后，立刻调用 `scheduler.run_event_loop()` 阻塞，直到 `ShutdownReq` 把 `gracefully_exit` 置 True 才跳出。

Scheduler 类通过多个 mixin 组合而成（这些 mixin 提供 PD 分离、PP 等变体的事件循环）：

[python/sglang/srt/managers/scheduler.py:303-311](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L303-L311) —— `class Scheduler(SchedulerDisaggregationDecodeMixin, ...)`，注释写明它「管理一个张量并行 GPU worker」。

`__init__` 的签名与最初几行，体现「构造参数即部署拓扑（tp_rank/pp_rank/dp_rank 等）+ 配置（server_args）」：

[python/sglang/srt/managers/scheduler.py:313-368](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L313-L368) —— 把 `server_args` 里的关键字段（`schedule_policy`、`enable_overlap`、`page_size`、`spec_algorithm` 等）「提取并缓存」到 `self` 属性上。其中最关键的一行是：

```python
self.enable_overlap = not server_args.disable_overlap_schedule and not use_mlx()
```

`enable_overlap` 这个布尔值**决定了走哪种事件循环**——它是 normal 与 overlap 两种模式的分水岭。

三大运行时状态在 `init_running_status` 里初始化：

[python/sglang/srt/managers/scheduler.py:962-971](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L962-L971) —— 这三个字段是理解事件循环的钥匙，下面 4.2 会展开：

```python
self.waiting_queue: List[Req] = []                              # 等待 prefill 的请求
self.running_batch: ScheduleBatch = ScheduleBatch(reqs=[], ...) # 正在 decode 的批
self.last_batch: Optional[ScheduleBatch] = None                 # 上一轮跑过的批
```

两个协作组件在 `__init__` 中被持有（组合而非继承）：

[python/sglang/srt/managers/scheduler.py:1754-1755](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L1754-L1755) —— `self.request_receiver = SchedulerRequestReceiver(recv_from_tokenizer=..., ...)`，把 ZMQ 接收 socket 等依赖注入进接收组件。

[python/sglang/srt/managers/scheduler.py:1877](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L1877) —— `self.batch_result_processor = SchedulerBatchResultProcessor(...)`，同理持有结果处理组件。

#### 4.1.4 代码实践

**实践目标**：用源码阅读的方式确认「Scheduler 是进程级单例、构造完即进循环」这一心智模型。

**操作步骤**：

1. 打开 [scheduler.py:4593](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L4593) 的 `run_scheduler_process`。
2. 顺着读：构造 Scheduler（4642 行）→ 发送 init 信息给父进程（4655 行）→ 调 `run_event_loop`（4658 行）→ `finally` 里 `release_host_resources`（4671 行）。
3. 在 `__init__`（313 行）里搜索 `self.enable_overlap =`，确认它来自 `server_args.disable_overlap_schedule`。

**需要观察的现象**：`run_event_loop` 之后没有任何代码会立即执行（它在阻塞），直到循环退出才走 `finally`。

**预期结果**：你会确认「构造 → 阻塞循环 → 退出清理」的三段式，并且明白 `enable_overlap` 是后续模式分发的开关。这一步**待本地验证**的是：用 `--disable-overlap-schedule` 启动服务时，`enable_overlap` 变为 `False`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Scheduler 要做成「每张 GPU 一个进程」，而不是一个进程管多卡？
**参考答案**：每卡一个进程可以让 CPU 调度与该卡的 GPU 前向在各自进程里独立重叠（overlap），避免 GIL 串行化；同时也天然对应张量并行（TP）下「每 rank 一份 worker」的拓扑，NCCL 通信在进程间完成。

**练习 2**：`request_receiver` 和 `batch_result_processor` 为什么被拆成独立类而不是写在 `Scheduler` 里？
**参考答案**：遵循「持有并调用」的组合式设计（见项目 `general-code-style.md`），把「拉取请求」「消化结果」这两个有独立输入输出契约的职责收口到小而聚焦的 frozen dataclass 里，降低 `Scheduler` 这个本就 4000+ 行的大类的复杂度，也方便单独测试。

---

### 4.2 事件循环总入口：run_event_loop 与模式分发

#### 4.2.1 概念说明

`run_event_loop` 是事件循环的**唯一入口**，但它本身不写循环逻辑。它只做两件事：① 创建并切到 `schedule_stream`（重叠模式所需的独立 CUDA 流）；② 调用 `dispatch_event_loop` 按「部署形态」分派到具体的循环实现。

#### 4.2.2 核心流程

分派决策树（简化版）：

```
run_event_loop()
  ├─ use_mlx()? ──yes──▶ dispatch_event_loop(self)   # MLX 走另一套
  ├─ 创建 schedule_stream（CUDA/HIP 时避开与 forward_stream 别名）
  └─ 在 schedule_stream 上下文里 dispatch_event_loop(self)

dispatch_event_loop(scheduler)
  ├─ disaggregation_mode == NULL（普通服务）
  │    ├─ enable_pdmux        ──▶ event_loop_pdmux
  │    ├─ pp_size > 1         ──▶ event_loop_pp
  │    ├─ enable_overlap_mlx  ──▶ event_loop_overlap_mlx
  │    ├─ enable_overlap      ──▶ event_loop_overlap   ◀── 默认走这里
  │    └─ else                ──▶ event_loop_normal
  ├─ disaggregation_mode == PREFILL ──▶ 各种 disagg_prefill 变体
  └─ disaggregation_mode == DECODE  ──▶ 各种 disagg_decode 变体
```

本讲的 `event_loop_normal` 与 `event_loop_overlap` 都属于 `disaggregation_mode == NULL` 分支。

#### 4.2.3 源码精读

`run_event_loop` 的主体——注意 schedule_stream 的创建与「避免与 forward_stream 别名」的保护逻辑：

[python/sglang/srt/managers/scheduler.py:1469-1500](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L1469-L1500) —— 创建 `schedule_stream`；在 CUDA/HIP 下，由于 stream 来自轮询池，可能和 `forward_stream` 指向同一个底层 handle（那样重叠就失效了），所以最多重试 64 次重新抽取；最后 `with StreamContext(schedule_stream)` 里调用 `dispatch_event_loop(self)`。注释点明 `schedule_stream` 上的写入必须排在 forward 对共享内存池的读取之后（WAR 屏障）。

模式分派函数：

[python/sglang/srt/managers/scheduler.py:4497-4525](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L4497-L4525) —— `dispatch_event_loop` 按 `disaggregation_mode` + `enable_overlap` + `pp_size` + `enable_pdmux` 组合选择具体循环。普通推理服务（`NULL` 模式、单 PP、`enable_overlap=True`）走 `event_loop_overlap`；若 `--disable-overlap-schedule` 则走 `event_loop_normal`。

两个关键变量决定了你会进入哪个分支：

- `self.enable_overlap`：在 `__init__` 里由 `disable_overlap_schedule` 决定（见 4.1.3）。
- `self.disaggregation_mode`：PD 分离相关，本讲默认 `NULL`。

#### 4.2.4 代码实践

**实践目标**：搞清「在我的启动参数下，到底进的是哪个事件循环」。

**操作步骤**：

1. 决定你的启动方式。默认 `sglang serve --model-path <m>` 不带 `--disable-overlap-schedule`，也不开 PD 分离。
2. 对照 [scheduler.py:4501-4511](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L4501-L4511) 的分支：`disaggregation_mode == NULL`、`pp_size == 1`、`enable_overlap == True` → 命中第 4508-4509 行的 `event_loop_overlap()`。
3. 若想强制走 normal 模式做对照，加 `--disable-overlap-schedule`，则 `enable_overlap` 变 `False`，命中第 4510-4511 行的 `event_loop_normal()`。

**需要观察的现象**：切换开关后，进程日志/行为在 decode 延迟上的差异（overlap 通常 decode 更快）。

**预期结果**：你能在源码分支表里精确定位自己当前使用的循环函数名。**待本地验证**：实际启动后用 profiler 或日志确认。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `run_event_loop` 要费力气保证 `schedule_stream` 与 `forward_stream` 不是同一个底层 handle？
**参考答案**：重叠的前提是两条流能被 GPU 并发执行；若两者别名同一 handle，则 schedule_stream 上的 CPU 调度写入会和 forward_stream 的前向串行化，重叠收益归零，与直接用 normal 模式无异。

**练习 2**：`dispatch_event_loop` 为什么不直接用 `if/else` 写在 `run_event_loop` 里？
**参考答案**：因为分派维度多（disaggregation_mode × pp_size × enable_overlap × enable_pdmux × mlx），且 PD 分离、PP 各自还有一整套循环变体。抽成独立函数让分派表清晰，也让 mixin 能注入各自的变体而不污染主流程。

---

### 4.3 event_loop_normal：经典串行循环的一轮迭代

#### 4.3.1 概念说明

`event_loop_normal` 是最直白的事件循环：**一圈迭代里，CPU 干完所有调度活，再交给 GPU 跑前向，GPU 跑完了 CPU 才开始下一圈**。它逻辑简单、易于理解，是阅读 SGLang 调度逻辑的最佳入口。它的缺点是 CPU 与 GPU 串行，有「互相等待」的空闲。

#### 4.3.2 核心流程

`event_loop_normal` 一轮迭代的步骤（这是本讲**最重要的流程**，请记牢）：

```
while not gracefully_exit:
  1. recv_reqs = request_receiver.recv_requests()      # 拉取新请求
  2. process_input_requests(recv_reqs)                  # 把新 Req 放进 waiting_queue；处理控制类请求
  3. plan = get_next_batch_to_run(running_batch, last_batch)  # 决定这一步跑什么批
       self.running_batch = plan.running_batch          # 更新 running_batch（可能被合并/回缩）
       batch = plan.batch_to_run                        # 这一步真正要前向的批（可能为 None）
  4. if batch:
       result = run_batch(batch)                        # 触发 GPU 前向（同步拿回结果）
       process_batch_result(batch, result)              # 采样、更新 Req 状态、流式发回 token
     else:
       on_idle()                                        # 无人可跑时做自检/休眠
  5. self.last_batch = batch                            # 记下这一轮的批，供下一轮用
```

关键直觉：**第 3 步 `get_next_batch_to_run` 是真正的「调度大脑」**，它综合 `waiting_queue` 和 `running_batch` 决定这一步是「跑一个 prefill 批」「跑一个 decode 批」还是「无批可跑」。第 4 步的 `run_batch`/`process_batch_result` 分别是「执行」与「回写」。下一节（4.5）会专门拆开 `get_next_batch_to_run` 内部对 prefill/decode 的分流。

#### 4.3.3 源码精读

`event_loop_normal` 全文（带 `@DynamicGradMode()` 装饰器，统一管理梯度上下文）：

[python/sglang/srt/managers/scheduler.py:1519-1551](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L1519-L1551) —— 逐行对应上面流程图的 5 步。注意 `get_next_batch_to_run` 返回的 `plan` 同时携带了「更新后的 running_batch」和「这一步要跑的 batch」，调度器据此分别赋值给 `self.running_batch` 和局部变量 `batch`。`last_batch` 在末尾被刷新，下一轮迭代里 `get_next_batch_to_run` 会用到它（例如判断上一轮是不是 extend/prefill，决定要不要把上轮新完成的请求并入 running_batch）。

请求接收组件的入口——每轮迭代的第一步：

[python/sglang/srt/managers/scheduler_components/request_receiver.py:72-99](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler_components/request_receiver.py#L72-L99) —— `SchedulerRequestReceiver.recv_requests()`：在 `tp_rank=0` 那张卡上用 `zmq.NOBLOCK` 非阻塞地从 TokenizerManager 拉取已分词请求，拉到的列表再广播给其余 TP rank。注意它是**非阻塞**的——有就拉、没有就立刻返回空列表，绝不卡住事件循环。

底层拉取逻辑：

[python/sglang/srt/managers/scheduler_components/request_receiver.py:101-139](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler_components/request_receiver.py#L101-L139) —— `_pull_raw_reqs`：两个 `while True` 分别从 `recv_from_tokenizer`（普通请求）和 `recv_from_rpc`（RPC 控制请求）非阻塞收消息，受 `max_recv_per_poll` 限制（防止单轮收太多拖慢调度）。`zmq.NOBLOCK` + 捕获 `ZMQError` 是「拉空即停」的标准写法。

第二步 `process_input_requests` 的职责：

[python/sglang/srt/managers/scheduler.py:1677-1701](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L1677-L1701) —— 遍历收到的请求，通过 `_request_dispatcher` 分发：生成类请求会被加入 `waiting_queue`，控制类请求（如 abort、load weights）就地处理并可能立即回吐 output 给 TokenizerManager。

#### 4.3.4 代码实践

**实践目标**：把 `event_loop_normal` 的 5 步流程对照源码「标注」一遍，建立一轮迭代的精确心智图。

**操作步骤**：

1. 打开 [scheduler.py:1519-1551](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L1519-L1551)。
2. 准备一张表，列为：`行号 | 代码 | 这一步在做什么 | 读哪个状态 | 写哪个状态`。
3. 逐行填写，例如：
   - 1527 行 `recv_reqs = self.request_receiver.recv_requests()` → 拉请求；读：ZMQ socket；写：无（返回局部变量）。
   - 1528 行 `process_input_requests(recv_reqs)` → 入队；读：recv_reqs；写：`waiting_queue`。
   - 1533-1537 行 `get_next_batch_to_run(...)` → 决策；读：`running_batch`、`last_batch`、`waiting_queue`；写：`self.running_batch`、`batch`。
   - 1542-1543 行 `run_batch` + `process_batch_result` → 执行+回写；读：`batch`；写：各 Req 的 `output_ids`、TokenizerManager 收到的 token。
   - 1549 行 `self.last_batch = batch` → 读 `batch`；写 `self.last_batch`。

**需要观察的现象**：填完表后你会清楚看到——一轮迭代里 `waiting_queue` 只被 `process_input_requests` 和 `get_new_batch_prefill`（在 `get_next_batch_to_run` 内）改动，`running_batch` 只被 `get_next_batch_to_run` 改动。

**预期结果**：得到一张完整的状态读写表。这是后续理解 overlap 模式「为什么要把结果处理延后」的基础。此为源码阅读型实践，**待本地验证**的是实际运行时各状态的快照。

#### 4.3.5 小练习与答案

**练习 1**：在 `event_loop_normal` 里，`batch` 为 `None`（无批可跑）时会发生什么？为什么？
**参考答案**：走 `else` 分支调用 `on_idle()`。`on_idle` 只在「整盘皆空」（`waiting_queue` 和 `running_batch` 都空）时才真正做自检/重置/休眠，否则直接返回（说明还有活要干，只是这一轮没凑出批）。这样可以避免空转浪费 CPU。

**练习 2**：`recv_requests` 用 `zmq.NOBLOCK` 而不是阻塞接收，有什么好处？
**参考答案**：事件循环必须保持高频率轮转，阻塞接收会让循环卡在「等请求」上，无法及时推进 `running_batch` 的 decode（正在生成的请求每一步都依赖循环转一圈）。非阻塞保证「有请求就收、没请求立刻去管正在跑的批」。

---

### 4.4 event_loop_overlap：CPU/GPU 重叠调度

#### 4.4.1 概念说明

`event_loop_overlap` 是 SGLang 默认的、也是性能最优的事件循环。它的核心思想在 2.3 节已经讲过：用 `schedule_stream` 跑 CPU 调度，让它与 `forward_stream` 上的 GPU 前向**并行**。

实现重叠的关键技巧是**把结果处理延后一轮**：当 GPU 正在跑「批 N」时，CPU 同时在「处理批 N-1 的结果 + 为批 N+1 组批」。因此需要一个 `result_queue` 暂存上一批的 `(batch, result)`，等下一轮再处理。

#### 4.4.2 核心流程

`event_loop_overlap` 一轮迭代的步骤（与 normal 对照，多了一个 result_queue）：

```
event_loop_overlap():
  self.result_queue = deque()   # 暂存「上一批」的 (batch, result)

  while not gracefully_exit:
    1. recv_requests() + process_input_requests()        # 同 normal
    2. plan = get_next_batch_to_run(running_batch, last_batch)
       batch = plan.batch_to_run
       disable_overlap_for_batch = is_disable_overlap_for_batch(batch, last_batch)
    3. if disable_overlap_for_batch:                      # 某些情况必须禁用重叠
         pop_and_process()  # 立刻处理上一批结果（同步点）
    4. if batch:
         batch_result = run_batch(batch)                  # 在 forward_stream 上发起前向
         _apply_war_barrier()                             # 排好「本轮写」与「上一轮读」的顺序
         result_queue.append( (batch.copy(), batch_result) )  # 暂存，下一轮再处理
    5. if last_batch and not disable_overlap_for_batch:   # 处理上一批的结果（延后一轮）
         pop_and_process()
       elif batch is None:
         on_idle()
    6. launch_batch_sample_if_needed(...)                 # 采样（依赖上一批结果，故排在后面）
    7. self.last_batch = batch
```

`pop_and_process()` 就是 `result_queue.popleft()` 后调 `process_batch_result`——即把 normal 里第 4 步的「回写」挪到了**下一轮迭代的第 5 步**。

什么时候必须**禁用重叠**（`disable_overlap_for_batch`）？两种典型情况：
- **连续两个 prefill 批**：为了降低首个批的 TTFT（首 token 延迟），不让它等上一批的结果处理。
- **某些投机解码 + 文法约束**组合：FSM（有限状态机）必须在下一个批的掩码生成前推进，必须同步。

#### 4.4.3 源码精读

`event_loop_overlap` 主体——注意 `result_queue` 与「延后一轮处理」的对应关系：

[python/sglang/srt/managers/scheduler.py:1553-1625](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L1553-L1625) —— `result_queue` 是 `Deque[Tuple[ScheduleBatch, GenerationBatchResult|EmbeddingBatchResult]]`。第 1601 行 `run_batch(batch)` 发起前向，第 1604 行把 `(batch.copy(), batch_result)` 入队——**注意是 `batch.copy()`**，因为下一轮处理时 `batch` 这个局部变量早已被覆盖，必须快照。第 1609-1611 行在 `last_batch` 存在且允许重叠时，才 `pop_and_process()` 处理上一批。第 1603 行 `_apply_war_barrier()` 是个内存序屏障：保证本轮 schedule_stream 上的写入（组批时写共享 buffer）排在上一轮 forward_stream 对共享内存池的读取之后（写后读的逆序 WAR）。

WAR 屏障的实现：

[python/sglang/srt/managers/scheduler.py:1502-1517](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L1502-L1517) —— `_apply_war_barrier`：快路径是等待上一轮前向发布过的 `read_done` 事件（细粒度），慢路径才整体 `wait_stream(forward_stream)`（粗粒度，可由 `SGLANG_FORCE_COARSE_WAR_BARRIER` 强制）。

禁用重叠的判定：

[python/sglang/srt/managers/scheduler.py:1627-1665](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L1627-L1665) —— `is_disable_overlap_for_batch`：判定两个连续 prefill 批（受 `SGLANG_DISABLE_CONSECUTIVE_PREFILL_OVERLAP` 控制）以及投机解码+文法的同步需求。注释解释了 DP attention 下要用全局同步的 `is_extend_in_batch` 判断，避免各 rank 决策不一致导致死锁。

结果处理组件——`pop_and_process` 最终调到的就是它：

[python/sglang/srt/managers/scheduler_components/batch_result_processor.py:724-834](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler_components/batch_result_processor.py#L724-L834) —— `process_batch_result_decode`：先 `result.copy_done.synchronize()` 等 GPU→CPU 拷贝完成，然后遍历批里的每个 `Req`，把 `next_token_id` 追加进 `req.output_ids`、更新完成状态、处理 logprob/hidden_states/grammar，最后 `output_streamer.stream_output(...)` 把 token 流式发回 Detokenizer。这正是「延后一轮」最终要兑现的工作。

调度器对 decode/prefill/idle 的分派：

[python/sglang/srt/managers/scheduler.py:3581-3601](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L3581-L3601) —— `process_batch_result` 按 `batch.forward_mode`（decode / extend / prebuilt / idle）路由到结果处理组件的不同方法。它被 normal 与 overlap 两种循环的 `pop_and_process` 共同调用。

#### 4.4.4 代码实践

**实践目标**：亲手对比 normal 与 overlap 两种循环在一轮迭代里的 CPU/GPU 时间线，理解「延后一轮」的含义。

**操作步骤**：

1. 准备一张甘特图草稿，横轴为时间，两条泳道：`CPU (schedule_stream)` 与 `GPU (forward_stream)`。
2. **normal 模式**（参考 4.3.3 的 5 步）：画出 3 轮迭代，你会看到 CPU 与 GPU 严格交替（CPU 段、GPU 段、CPU 段……），中间有空隙。
3. **overlap 模式**（参考 4.4.3）：画出 3 轮迭代，CPU 在「处理批 N-1 结果 + 为批 N+1 组批」时，GPU 同时在跑「批 N」。理想情况下两条泳道几乎满载重叠。
4. 在 [scheduler.py:1604](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L1604) 标注「这里把结果入队，下一轮才处理」，在 [scheduler.py:1611](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L1611) 标注「这里出队处理上一批」。

**需要观察的现象**：overlap 模式下，`process_batch_result(batch_N)` 实际发生在「第 N+1 轮迭代」里，而非第 N 轮。

**预期结果**：你画出的 overlap 甘特图里，CPU 段与 GPU 段高度重叠，这正是吞吐提升的来源；而代价是「结果延迟一轮」带来的一拍输出延迟（通常可忽略）。**待本地验证**：用 `--disable-overlap-schedule` 跑同一负载，对比 decode 吞吐。

> 小提示：当 CPU 调度耗时 \( T_{\text{cpu}} \) 小于 GPU 前向耗时 \( T_{\text{gpu}} \) 时，overlap 把每轮墙钟时间从 \( T_{\text{cpu}} + T_{\text{gpu}} \) 压到约 \( \max(T_{\text{cpu}}, T_{\text{gpu}}) \approx T_{\text{gpu}} \)，即 CPU 开销近乎「免费」。

#### 4.4.5 小练习与答案

**练习 1**：overlap 模式里，为什么入队的是 `batch.copy()` 而不是 `batch` 本身？
**参考答案**：因为局部变量 `batch` 在下一轮迭代开头会被 `get_next_batch_to_run` 重新赋值，而 `result_queue` 里的那一项要到下一轮才处理。若不快照，处理时拿到的就是「下一轮的新批」，张量引用、请求列表都会错乱。这也是项目 `schedule-batch-out-of-place-mutation.md` 规则强调「ScheduleBatch 字段不可原地改」的现实动机——快照依赖旧对象保持冻结。

**练习 2**：什么场景下 overlap 会自动退化为「同步处理上一批」？
**参考答案**：当 `is_disable_overlap_for_batch` 返回 True 时——典型是两个连续的 prefill/extend 批（为了首 token 延迟），或某些投机解码+文法约束组合（FSM 必须先推进）。此时第 1588-1589 行的 `pop_and_process()` 会立即处理上一批，相当于在两轮之间插入一个同步点。

---

### 4.5 prefill/decode 批的构造：get_new_batch_prefill 与 update_running_batch

#### 4.5.1 概念说明

前面两节反复出现的 `get_next_batch_to_run` 是调度大脑，它内部把决策分流为两条路径：

- **有新请求可 prefill** → 调 `get_new_batch_prefill`，从 `waiting_queue` 挑请求组一个 prefill 批；
- **没有新 prefill、但 `running_batch` 非空** → 调 `update_running_batch`，让正在 decode 的批前进一步。

`get_new_batch_prefill` 负责「**把请求从等待队列搬进批，并分配 KV 内存、做前缀缓存匹配**」；`update_running_batch` 负责「**维护 decode 批的健康**：清理已完成的请求、检查 KV 是否够用、不够就回缩（retract）一部分请求回等待队列」。两者共同决定了「一条请求的生命周期」。

一条 `Req` 的生命周期视角：

```
到达 → waiting_queue
     →（被 get_new_batch_prefill 选中）→ prefill 批（首次前向，建 KV）
     →（prefill 完成后合并进 running_batch）
     → running_batch（每轮 update_running_batch 推进一个 decode 步）
     →（KV 不够时被 retract 回 waiting_queue，等下次重新 prefill）
     →（生成完毕）→ 从批里移除，结果发回
```

#### 4.5.2 核心流程

`get_next_batch_to_run` 的核心分流（简化）：

```
get_next_batch_to_run(running_batch, last_batch):
  1. 处理超时/abort、chunked abort 等清理
  2. 若 last_batch 是 extend（prefill）且非空：
       last_batch.filter_batch()           # 摘掉已完成/被排除的请求
       running_batch.merge_batch(last_batch)  # 把上轮新 prefill 完的请求并入 running_batch
  3. prefill_plan = get_new_batch_prefill(running_batch)   # 尝试组 prefill 批
     new_batch = prefill_plan.batch_to_run
  4. if new_batch is not None:
       ret = new_batch                       # 这一轮跑 prefill
     else:
       if running_batch 非空:
         running_batch = update_running_batch(running_batch)  # 这一轮跑 decode
         ret = running_batch
       else:
         ret = None                          # 空闲
  5. return NextBatchPlan(batch_to_run=ret, running_batch=running_batch)
```

注意第 2 步：**prefill 与 decode 的衔接**就发生在 `merge_batch`——上一轮新 prefill 出来的请求，这一轮被并进 `running_batch`，从此开始它的 decode 生涯。这就是「连续批处理」的实现机制：新请求随时 prefill 加入，老请求继续 decode。

#### 4.5.3 源码精读

`get_next_batch_to_run` 的核心分流段：

[python/sglang/srt/managers/scheduler.py:2754-2820](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L2754-L2820) —— 第 2754-2779 行：若 `last_batch` 是 extend 且非空，先 `filter_batch` 再 `merge_batch` 进 `running_batch`（连续批处理的衔接点）。第 2794-2796 行调 `get_new_batch_prefill` 尝试组 prefill 批。第 2811-2820 行的 if/else 是 prefill vs decode 的最终分流：`new_batch` 非空就 prefill，否则用 `update_running_batch` 推进 decode，都没有就返回 `None`。

返回类型 `NextBatchPlan`——把「这一步要跑什么」和「更新后的 running_batch」打包：

[python/sglang/srt/managers/schedule_batch.py:3171-3173](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/schedule_batch.py#L3171-L3173) —— `class NextBatchPlan(msgspec.Struct): batch_to_run: Optional[ScheduleBatch]; running_batch: ScheduleBatch`。事件循环据此分别赋值（见 4.3.3 的 1533-1537 行）。

`get_new_batch_prefill` 的外壳（含 prefill delayer 预算评估）：

[python/sglang/srt/managers/scheduler.py:2844-2863](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L2844-L2863) —— 真正干活的是 `_get_new_batch_prefill_raw`。若启用了 `prefill_delayer`（内存压力大时延迟 prefill），先评估当前显存池用量再决定。

`_get_new_batch_prefill_raw` 的核心循环——从 `waiting_queue` 挑请求，用 `PrefillAdder` 评估预算并组批：

[python/sglang/srt/managers/scheduler.py:2883-2914](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L2883-L2914) —— 前置短路检查：若 `running_batch.batch_is_full` 或 `waiting_queue` 为空（且无 chunked_req），直接返回 `None`（这一步不 prefill）。否则检查 `get_num_allocatable_reqs` 是否还有可分配槽位，没有则把 `batch_is_full` 置 True 并返回。

[python/sglang/srt/managers/scheduler.py:2970-3047](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L2970-L3047) —— 遍历 `waiting_queue`，对每个 `req` 调 `adder.add_one_req(...)` 尝试加入批（`PrefillAdder` 在此评估 token/内存预算、做前缀缓存匹配——具体策略在 u3-l3 讲）。被选中的请求进入 `adder.can_run_list`；第 3047 行把 `waiting_queue` 过滤掉已选中的请求。这里体现了「请求从 waiting_queue 出发」的生命周期第一步。

[python/sglang/srt/managers/scheduler.py:3062-3085](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L3062-L3085) —— 用选中的 `can_run_list` 构造新批 `ScheduleBatch.init_new(...)`，传入 `req_to_token_pool`（请求→token 槽池）、`token_to_kv_pool_allocator`（token→KV 内存分配器）、`tree_cache`（RadixCache 前缀缓存），再 `prepare_for_extend()` 准备 prefill 所需张量。至此 prefill 批成型。

`update_running_batch`——维护 decode 批，必要时回缩：

[python/sglang/srt/managers/scheduler.py:3155-3243](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L3155-L3243) —— 先 `batch.filter_batch()` 清理已完成请求。第 3170 行 `check_decode_mem()` 检查剩余 KV 是否够这一步 decode；不够（`kv_full_retract_flag`）就 `batch.retract_decode(...)` 把一部分请求**回缩**（释放它们的 KV、降低 `new_token_ratio`），回缩的请求被重新放回 `waiting_queue`（第 3230-3231 行）——这就是生命周期里「KV 不够时退回 waiting_queue」的那一步。一切正常则 `prepare_for_decode()` 准备 decode 张量。

#### 4.5.4 代码实践

**实践目标**：跟踪一条请求从 `waiting_queue` 到 `running_batch` 再到完成的完整路径，标注每一步发生在哪个函数里。

**操作步骤**：

1. 假设一条请求 `R` 刚被 `process_input_requests` 放进 `self.waiting_queue`（[scheduler.py:1677](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L1677)）。
2. 下一轮迭代，`get_new_batch_prefill` 选中 `R`：在 [scheduler.py:3001](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L3001)（`adder.add_one_req`）和 [scheduler.py:3047](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L3047)（从 waiting_queue 移除）处标注「R 离开等待队列」。此时 `R` 在一个 prefill 批里，`batch_to_run` 非空。
3. 这一轮 `run_batch` 跑完 R 的 prefill，`process_batch_result_prefill` 给 R 采样首 token。
4. 再下一轮，`get_next_batch_to_run` 在 [scheduler.py:2774-2779](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L2774-L2779) 处把含 R 的上轮批 `merge_batch` 进 `running_batch`——标注「R 正式成为 decode 成员」。
5. 之后每轮，若没有新 prefill，`update_running_batch`（[scheduler.py:3242](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L3242) 的 `prepare_for_decode`）推进 R 一个 decode 步。
6. 若某轮 KV 不够，[scheduler.py:3183](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L3183) 的 `retract_decode` 可能把 R 退回 `waiting_queue`（[scheduler.py:3231](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L3231)）。
7. R 生成完毕时，`filter_batch` 把它从批里摘掉，`process_batch_result_decode` 把最后 token 发回。

**需要观察的现象**：你会看到一条请求在 `waiting_queue` ↔ `running_batch` 之间最多往返多次（retract），最终在 `filter_batch` 被清理。

**预期结果**：画出 R 的状态流转图，每个箭头标注触发的函数与行号。这是理解连续批处理与 KV 内存管理的最直接方式。**待本地验证**：在高负载下触发 retract 时观察日志里的 `"KV cache pool is full. Retract requests."` 警告（[scheduler.py:3217-3228](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L3217-L3228)）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 prefill 批和 decode 批要分开构造（`get_new_batch_prefill` vs `update_running_batch`），而不是合成一个统一函数？
**参考答案**：两者的计算模式、内存需求、张量形状完全不同。prefill 要为新 token 分配 KV、做前缀匹配、处理长 prompt（可能还要 chunked prefill 切块）；decode 只是给每个已存在请求往前走 1 个 token，关心的是「KV 还够不够」。分开构造让各自逻辑聚焦，也让 `get_next_batch_to_run` 的分流清晰。

**练习 2**：`retract_decode` 把请求退回 `waiting_queue` 后，这条请求会重新走 prefill 吗？这会不会浪费算力？
**参考答案**：会重新走 prefill，但**不是从零开始**——RadixAttention 的前缀缓存（`tree_cache`）会命中它已经算过的前缀 KV，所以重 prefill 的实际计算量只是「被回缩掉的那一段尾巴」。这正是 RadixAttention（u4-l1）与 retract 机制配合的价值：在显存压力下回缩是低成本的。`new_token_ratio` 也会被调低以更保守地预估未来需求。

**练习 3**：`NextBatchPlan` 为什么要把 `running_batch` 和 `batch_to_run` 一起返回，而不是只返回要跑的批？
**参考答案**：因为 `get_next_batch_to_run` 在决策过程中**会改写 `running_batch`**（合并上轮 prefill 请求、retract、filter 已完成请求）。事件循环需要拿到这个更新后的 `running_batch` 赋值给 `self.running_batch`，否则状态会失步。把两者打包返回，是让「这一步跑什么」与「批状态如何演化」原子地传达。

---

## 5. 综合实践

把本讲的知识串起来，做一个**「一轮迭代的完整剧本」**标注任务。这是本讲最值得动手的综合练习。

**任务**：假设你用默认参数（overlap 模式）启动了 SGLang，服务里既有新请求在到达，又有一批请求正在 decode。请写一份「一轮 `event_loop_overlap` 迭代的剧本」，要求：

1. **画出这一轮涉及的函数调用链**（按真实调用顺序），至少包含：`recv_requests` → `process_input_requests` → `get_next_batch_to_run` →（`get_new_batch_prefill` 或 `update_running_batch`）→ `run_batch` → `_apply_war_barrier` → `result_queue.append` → `pop_and_process`（处理上一批）→ `process_batch_result` → `process_batch_result_decode/prefill` → `output_streamer.stream_output`。
2. **在每一个函数旁标注**：它读 / 写了三大状态（`waiting_queue`、`running_batch`、`last_batch`）中的哪一个，以及它运行在 `schedule_stream` 还是 `forward_stream` 上。
3. **解释**：为什么 `process_batch_result` 处理的是「上一轮的批」而不是「这一轮的批」？如果上一轮是 prefill、这一轮也是 prefill，会发生什么（提示：`is_disable_overlap_for_batch`）？
4. **延伸**：把 `enable_overlap` 设为 False（即 `event_loop_normal`），重写这份剧本，对比两份剧本里 `process_batch_result` 出现的时机差异。

**验收标准**：

- 调用链顺序与本讲 4.3、4.4、4.5 的源码一致；
- 状态读写标注正确（可对照 4.3.4 的实践表格）；
- 能清楚说出「延后一轮」的因果；
- 能指出连续 prefill 时 overlap 退化为同步的代码位置（[scheduler.py:1643-1647](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/scheduler.py#L1643-L1647)）。

完成后，你已经把 Scheduler 的「心跳」彻底走通——后续 u3-l2（批数据结构）、u3-l3（调度策略）、u3-l4（overlap 工程细节）都是在这个骨架上往里填肉。

---

## 6. 本讲小结

- **Scheduler 是每张 GPU 一个的进程级单例**，由 `run_scheduler_process` 构造后立即进入 `run_event_loop` 阻塞循环，直到 `ShutdownReq` 触发优雅退出。
- **三大运行时状态**——`waiting_queue`（待 prefill）、`running_batch`（正在 decode）、`last_batch`（上一轮的批）——是理解一切调度逻辑的钥匙，在 `init_running_status` 里初始化。
- **`run_event_loop` 只负责建 `schedule_stream` 并调 `dispatch_event_loop`**，后者按 `disaggregation_mode × enable_overlap × pp_size` 把循环分派到具体实现；普通服务默认走 `event_loop_overlap`。
- **`event_loop_normal`** 是串行循环：收请求 → `get_next_batch_to_run` 决策 → `run_batch` 执行 → `process_batch_result` 回写 → 刷 `last_batch`，CPU 与 GPU 交替有空闲。
- **`event_loop_overlap`** 用 `result_queue` 把结果处理**延后一轮**，让 CPU 调度与 GPU 前向重叠，实现「零开销调度器」；代价是代码复杂度与连续 prefill 等场景下需要 `is_disable_overlap_for_batch` 主动退化为同步。
- **`get_next_batch_to_run` 是调度大脑**：内部先 `merge_batch` 衔接 prefill→decode，再用 `get_new_batch_prefill`（从等待队列组 prefill 批，经 `PrefillAdder` 评估预算+前缀匹配）与 `update_running_batch`（维护 decode 批，KV 不够时 `retract_decode` 回缩）分流，最后用 `NextBatchPlan` 打包返回。

---

## 7. 下一步学习建议

本讲只刻画了事件循环的**骨架**与 prefill/decode 的**分流**，刻意回避了三块细节，它们正是 U3 后续三讲的主题：

1. **u3-l2（请求与批数据模型）**：本讲反复出现的 `Req`、`ScheduleBatch` 到底有哪些字段？`filter_batch`、`merge_batch`、`prepare_for_extend/decode`、`retract_decode` 是怎么操作这些字段的？读 [schedule_batch.py](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/schedule_batch.py)。
2. **u3-l3（调度策略）**：本讲里 `PrefillAdder.add_one_req` 和 `policy.calc_priority` 是黑盒——`waiting_queue` 里的请求按什么顺序被挑（LPM 最长前缀匹配 / FCFS / LOF）？为什么缓存感知调度能提升命中率？读 [schedule_policy.py](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/schedule_policy.py)。
3. **u3-l4（调度组件与 CPU-GPU 重叠）**：本讲对 overlap 只讲了 `result_queue` 与 WAR 屏障的机制，没展开 `scheduler_components/` 的全部组件与 overlap 工程细节。读 [overlap_utils.py](https://github.com/sgl-project/sglang/blob/4a55fdba0b7e5ac8e8dee0233064d34468b16a06/python/sglang/srt/managers/overlap_utils.py) 与各 scheduler_components。

补一句关于跨单元衔接：本讲的 `run_batch` 会调到 `model_worker.forward_batch_generation`，那是 **U5（模型执行层）** 的入口；而 `get_new_batch_prefill` 里用到的 `tree_cache`（RadixCache）和 `token_to_kv_pool_allocator`，则是 **U4（KV 缓存与 RadixAttention）** 的主角。所以当你想往下钻「前向到底跑了什么」或「KV 内存怎么分配」时，U4、U5 是下一站。
