# Diffusion 批处理：请求级与连续批处理

## 1. 本讲目标

本讲是专家层 U7（Diffusion 加速）的收尾篇，专门回答一个问题：**当扩散服务同时收到多个请求时，vLLM-Omni 怎样把它们合并成一次 GPU 前向，从而提升吞吐？**

学完本讲你应当能够：

1. 区分两种扩散批处理模式——**请求级批处理（request-level batching）** 与 **步级连续批处理（step-wise continuous batching）**——各自的前提、触发条件与适用场景。
2. 说清楚引擎如何根据 `step_execution`、`supports_request_batch`、`max_num_seqs` 三个开关，在初始化期一次性选定执行模式（`REQUEST_BATCH` / `STEP_BATCH`）并把对应的调度器（`RequestScheduler` / `StepScheduler`）与执行器入口（`execute_batch` / `execute_step`）绑定好。
3. 解释「兼容性键」为何是两种模式的共同安全闸：请求级用 `RequestBatchSamplingParamsKey`，步级用 `StepBatchSamplingParamsKey`，二者字段差异背后是「整条流水线一次跑完」与「按步推进」的根本区别。
4. 设计一个对比实验，用 `diffusion_benchmark_serving` 在 `max_num_seqs=1` 与 `max_num_seqs=4` 下度量吞吐差异，并解释兼容性约束如何影响实际批大小。

## 2. 前置知识

阅读本讲前，请先具备以下认知（前置讲义已建立）：

- **扩散请求与采样参数口袋**（u5-l4）：每条 `OmniDiffusionRequest` 只代表一个 prompt，多 prompt 的批处理由上层调度器合并；扩散采样参数装在 `OmniDiffusionSamplingParams` 里（尺寸、步数、seed、CFG 等）。
- **Diffusion 引擎外壳与三段式 busy loop**（u5-l1、u5-l2）：`DiffusionEngine` 是「编排者不下场」——它只做请求准入、调度/执行器协调、统一输出流投递，真正前向委托给多进程执行器的 worker；busy loop 的核心是 `schedule → execute → update_from_output → emit` 四段循环。
- **调度器与执行器分工**（u5-l2）：调度器只维护请求状态机与内存账本、完全不碰 GPU；执行器经 IPC（ZMQ/共享内存）把调度结果发给独立 worker 进程执行。`DiffusionRequestStatus` 是 `IntEnum`，终态判定可压缩为 `status >= FINISHED_COMPLETED`。
- **并行体系**（u7-l4）：扩散并行包含 TP/SP/DP/CFG/PP 等；本讲涉及的 DP 维度（`dp_concurrent`）会与请求级批处理产生一个特殊交叉，需略作了解。

几个本讲要反复用到的术语：

- **scheduler wave（调度波）**：调度器一次 `schedule()` 产出的 `DiffusionSchedulerOutput`，包含本批要跑的新请求与已运行请求。
- **co-batch（同批）**：多个请求被合并进同一次 GPU 前向。
- **head-of-line blocking（队头阻塞）**：FIFO 队列里，一个不兼容的队头请求会挡住后面兼容的请求进入当前批。
- **MFU（Model FLOPs Utilization）**：模型算力利用率；批处理的主要价值正是提升低 MFU 场景下的吞吐。

## 3. 本讲源码地图

本讲横跨「引擎模式选择 → 调度器准入 → 执行器分派 → worker 前向」四层，涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| `vllm_omni/diffusion/diffusion_engine.py` | 引擎：模式解析（`_resolve_execution_mode`）、调度器/执行函数绑定、busy loop 与准入等待 |
| `vllm_omni/diffusion/sched/base_scheduler.py` | 调度器基类：共享的 waiting/running 队列、FIFO 准入 `_can_schedule_waiting`、`schedule()` 主循环 |
| `vllm_omni/diffusion/sched/interface.py` | 调度接口：`DiffusionRequestStatus`、两个兼容性键、`SchedulerRequestState`、`DiffusionSchedulerOutput` |
| `vllm_omni/diffusion/sched/request_scheduler.py` | 请求级调度器：构建 `RequestBatchSamplingParamsKey`、一次执行即判终态 |
| `vllm_omni/diffusion/sched/step_scheduler.py` | 步级调度器：`_request_progress` 跨步推进、按 `step_index` 判完成 |
| `vllm_omni/diffusion/executor/abstract.py` | 执行器抽象：`execute_request` / `execute_batch` / `execute_step` 三个入口契约 |
| `vllm_omni/diffusion/executor/multiproc_executor.py` | 多进程执行器：三个入口的实际 RPC 投递，含「单请求回退」逻辑 |
| `vllm_omni/diffusion/worker/diffusion_model_runner.py` | worker 侧：`execute_model_batch`（请求级融合前向）与 `execute_stepwise`（步级批前向） |
| `vllm_omni/diffusion/worker/request_batch.py` | `DiffusionRequestBatch`：请求级批的运行时容器，面向 pipeline 的 `forward(batch)` |
| `vllm_omni/diffusion/worker/input_batch.py` | `InputBatch`（别名 `StepInputBatch`）：步级批的张量视图，`make_batch` / `scatter_latents` |
| `docs/design/feature/diffusion_request_level_batching.md` | 请求级批处理设计文档 |
| `docs/design/feature/diffusion_continuous_batching.md` | 步级连续批处理设计文档 |
| `docs/design/feature/diffusion_step_execution.md` | 步级执行契约（批处理的基础） |

---

## 4. 核心概念与源码讲解

### 4.1 批处理全景与 executor 路径选择

#### 4.1.1 概念说明

扩散生成一次图像/视频要跑几十步去噪，单条请求往往喂不饱 GPU（低 MFU）。若能同时跑多条兼容请求，就能把空闲算力填满，提升吞吐。vLLM-Omni 提供了**两条互斥的批处理路径**，二者的根本差异在于「合并发生在哪一层」：

- **请求级批处理（request-level batching）**：在 `step_execution=False`（默认）时启用。它把若干兼容请求**整条流水线**合并，对 pipeline 调用一次 `forward(batch)`，一次跑完全部去噪步。批在整条生成过程中是**静态**的——一旦这一波开始，中途不增不减请求。它需要 pipeline 显式声明 `supports_request_batch = True`。

- **步级连续批处理（step-wise continuous batching）**：在 `step_execution=True` 时启用。它把长去噪循环拆成调度器可见的「步」，**在去噪步之间**允许新的兼容请求加入、已完成的请求移除。批是**动态**的，每个 scheduler tick 成员都可能变化。它需要 pipeline 实现四段式步级契约（`prepare_encode` / `denoise_step` / `step_scheduler` / `post_decode`）。

一句话区分：**请求级「一批跑到底」，步级「批随步变」**。两者用同一个 `max_num_seqs` 控制容量上限，但含义不同——请求级是「一波里最多几个」，步级是「同时在跑的最多几个」。

#### 4.1.2 核心流程：引擎初始化期一次性绑定模式

引擎 `__init__` 按固定顺序完成模式选择与组件绑定：

```text
_resolve_execution_mode(od_config)   # ① 解析模式 + 校验 supports_request_batch
        │
        ├─ step_execution=True  ──→ STEP_BATCH  (supports_request_batch=False)
        └─ step_execution=False ──→ REQUEST_BATCH
                  └─ 若 max_num_seqs>1 但 pipeline 不支持 request_batch → 抛错（fail-fast）
_init_executor(od_config)            # ② 起多进程执行器
_init_scheduler(od_config)           # ③ 按模式选 RequestScheduler / StepScheduler
_init_execute_fn()                   # ④ 绑定 self.execute_fn = execute_step / execute_batch
```

运行期 busy loop 始终调用同一个 `self.execute_fn(sched_output)`，**分支在初始化期就定死**，不在每次请求时重判。这是一个典型的「把策略选择前置到构造期」设计，避免热路径里反复判分支。

需要注意一个**串行回退**：请求级模式下，若某次 `schedule()` 只产出一个请求（`max_num_seqs=1` 的常态，或一波里只有 1 个），`execute_batch` 会回退到 `execute_request`（逐请求 RPC）。也就是说 `max_num_seqs=1` 的请求级路径，与不开启批处理时的串行执行**完全等价**。

#### 4.1.3 源码精读

执行模式枚举与模式解析：[vllm_omni/diffusion/diffusion_engine.py:L143-L145](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L143-L145) 定义 `DiffusionExecutionMode`。真正的解析逻辑在 [vllm_omni/diffusion/diffusion_engine.py:L193-L211](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L193-L211)：`step_execution=True` 直接走 `STEP_BATCH`；否则检测 pipeline 的 `supports_request_batch`，若用户设了 `max_num_seqs>1` 但 pipeline 不支持，**在初始化期就抛 `ValueError`**——这正是文档所说的「fails early during engine initialization」。

`supports_request_batch` 的判定函数 [vllm_omni/diffusion/diffusion_engine.py:L103-L109](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L103-L109) 解析（含 custom pipeline）模型类后，读取类属性 `supports_request_batch`。目前声明为 `True` 的有 Qwen-Image、Flux、SD3、LTX2 等，例如 [vllm_omni/diffusion/models/qwen_image/pipeline_qwen_image.py:L269](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/models/qwen_image/pipeline_qwen_image.py#L269)。

调度器与执行函数的绑定：[vllm_omni/diffusion/diffusion_engine.py:L217-L228](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L217-L228) 按模式选 `StepScheduler()` 或 `RequestScheduler()`；[vllm_omni/diffusion/diffusion_engine.py:L271-L275](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L271-L275) 把 `execute_fn` 绑到 `execute_step` 或 `execute_batch`。

执行器抽象定义了三个入口契约：[vllm_omni/diffusion/executor/abstract.py:L77-L90](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/executor/abstract.py#L77-L90)（`execute_request` / `execute_batch` / `execute_step`）。多进程执行器的 `execute_batch` 是关键——它在 [vllm_omni/diffusion/executor/multiproc_executor.py:L521-L529](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/executor/multiproc_executor.py#L521-L529) 实现「单请求回退到 `execute_request`、多请求走融合 `execute_model_batch` RPC」；`execute_step` 在 [vllm_omni/diffusion/executor/multiproc_executor.py:L555-L565](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/executor/multiproc_executor.py#L555-L565) 直接把调度输出转发给 worker 的 `execute_stepwise` RPC。

最后看 busy loop 里的执行分派与输出流分支：[vllm_omni/diffusion/diffusion_engine.py:L442-L462](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L442-L462) 是统一的「执行 → update → emit」；其中 `_emit_outputs` 在 [vllm_omni/diffusion/diffusion_engine.py:L598-L600](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L598-L600) 区分：非 STEP_BATCH 模式只在 `finished` 时投递整块输出；STEP_BATCH 模式会逐步投递中间块（流式）。

#### 4.1.4 代码实践

**实践目标**：验证「模式在初始化期绑定」与「`max_num_seqs>1` 但 pipeline 不支持 request_batch 会 fail-fast」。

**操作步骤（源码阅读型，无需 GPU）**：

1. 打开 [vllm_omni/diffusion/diffusion_engine.py:L193-L211](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L193-L211)，分别在脑中代入两组配置：
   - A：`step_execution=True, max_num_seqs=8` → 走哪条分支？`supports_request_batch` 被设成什么？
   - B：`step_execution=False`，模型是 `z_image`（`supports_request_batch=False`，见 `vllm_omni/diffusion/models/z_image/pipeline_z_image.py:165`），`max_num_seqs=4` → 会发生什么？
2. 用 Grep 在 `vllm_omni/diffusion/models` 下搜索 `supports_request_batch = True`，列出全部支持请求级批处理的 pipeline 类。

**预期结果**：A 走 `STEP_BATCH` 且 `supports_request_batch=False`；B 在初始化期抛出以 `does not support request-level batching` 开头的 `ValueError`，**进程不会等到第一条请求才崩**。这是 vLLM-Omni「能力检查前置」工程风格的体现。

#### 4.1.5 小练习与答案

**练习 1**：若一个 pipeline 同时声明了 `supports_request_batch = True` 且实现了步级契约，用户同时设了 `--step-execution --max-num-seqs 4`，会发生请求级批处理吗？

**参考答案**：不会。`_resolve_execution_mode` 中 `step_execution=True` 优先级最高，直接返回 `STEP_BATCH` 并把 `supports_request_batch` 置为 `False`（[diffusion_engine.py:L200-L202](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L200-L202)）。两种模式互斥，步级胜出。

**练习 2**：`execute_fn` 为什么不在 busy loop 里每次用 `if step_execution` 动态选分支？

**参考答案**：热路径里反复判分支会带来可读性与微弱性能开销；更深层原因是模式决定了调度器类型、执行函数、输出流语义（整块 vs 流式块）这一整套耦合，把它们在构造期一次性钉死，能保证运行期「同一组组件协作」，避免半路切换导致状态不一致。

---

### 4.2 请求级批处理：兼容性键与准入等待策略

#### 4.2.1 概念说明

请求级批处理的核心承诺是：**保持每条请求的身份与元数据独立**（各自的 `request_id`、seed、采样参数、输出、错误、中止状态），只把「兼容」的请求融合进一次 `pipeline.forward(batch)`。这样既拿到批处理的吞吐红利，又不让 abort/错误处理变得含糊。

它依赖两个机制：

- **兼容性键 `RequestBatchSamplingParamsKey`**：从采样参数里抽取「形状敏感 + 引导敏感」的字段拼成一个可哈希的 key。只有 key 相同的请求才能同批。注意它**刻意排除** seed、generator、latent 张量、timesteps 这些「请求局部」字段——这些值在批内仍按请求读取。
- **准入等待（admission wait）`request_batch_max_wait_ms`**：面对突发 HTTP 流量，引擎可以在新一波的第一次 `schedule()` 前**短暂等待**，让相近的兼容请求凑齐再一起跑，从而增大融合批的大小。默认 `0` 表示不等待、零额外延迟。

准入策略是**保守的**：FIFO 保序，且队头若不兼容就阻塞后续——宁可少批，不可错批。

#### 4.2.2 核心流程

请求级一波的生命周期：

```text
新请求 add_request → 进 waiting 队列
        │
busy_loop 拿到 _cv 锁
        ├─ 若 supports_request_batch（或 dp_concurrent）：
        │     _wait_for_request_batch_admission_locked()   # 等兼容请求凑齐
        ├─ schedule()：
        │     先排 running，再按容量(max_num_seqs)与兼容性从 waiting 取
        │     _can_schedule_waiting：队头 key 必须等于当前批 key，否则 break
        ├─ execute_batch → 单请求回退 execute_request / 多请求 execute_model_batch
        └─ update_from_output → 一次执行即判终态（COMPLETED/ABORTED/ERROR）
```

准入等待的退出条件有四个（任一满足即停）：

1. waiting 队列达到 `max_num_seqs`（凑满了）；
2. 队列在「稳定窗口」内不再增长（来齐了，`stable_window_s` 默认 50ms）；
3. 到达 `deadline`（`request_batch_max_wait_ms` 上限）；
4. 引擎停止。

注意：准入等待**只在「当前没有 running 请求」时生效**——一波已经开始就不会中途插队。

#### 4.2.3 源码精读

兼容性键定义：[vllm_omni/diffusion/sched/interface.py:L68-L113](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/sched/interface.py#L68-L113) 是 `RequestBatchSamplingParamsKey`。它**包含** `num_inference_steps`、`sigmas`、`output_type`、`strength` 等字段——因为整条流水线一次跑完，这些决定执行轨迹的参数必须一致；而 seed/generator/latent 不在其中。`RequestScheduler` 用 [vllm_omni/diffusion/sched/request_scheduler.py:L30-L37](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/sched/request_scheduler.py#L30-L37) 构建该 key（注意 LoRA identity 需从 `sampling.lora_request` 单独解析，见文件顶部注释）。

FIFO 准入与队头阻塞：核心在基类 [vllm_omni/diffusion/sched/base_scheduler.py:L80-L109](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/sched/base_scheduler.py#L80-L109)。其中 `while self._waiting and len(self._running) < self.max_num_running_reqs` 循环里，[L97-L98](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/sched/base_scheduler.py#L97-L98) 调 `_can_schedule_waiting(state)`，不通过就 `break`——这就是队头阻塞。`_can_schedule_waiting` 实现在 [L260-L265](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/sched/base_scheduler.py#L260-L265)：批空则放行，否则要求「当前批 key == 该请求 key」。容量来自 `max_num_seqs`，在 [L59-L63](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/sched/base_scheduler.py#L59-L63) 写入 `max_num_running_reqs`。

准入等待逻辑：[vllm_omni/diffusion/diffusion_engine.py:L467-L523](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion_engine.py#L467-L523) 是 `_wait_for_request_batch_admission_locked`。[L472-L477](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L472-L477) 先排除步级与不支持批处理的情形；[L483-L484](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L483-L484) 「running>0 则不等」；四个退出条件见 [L497-L513](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L497-L513)。调用点在 busy loop [L432-L435](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L432-L435)，仅当 `supports_request_batch or dp_concurrent` 才调用。

worker 侧融合前向：[vllm_omni/diffusion/worker/diffusion_model_runner.py:L576-L594](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_model_runner.py#L576-L594) 的 `execute_model_batch` 把调度到的新请求包成列表，调用 `_execute_request_list(..., require_request_batch_support=True)`，最终对 pipeline 调用 `forward(batch)`。批的运行时容器是 `DiffusionRequestBatch`：[vllm_omni/diffusion/worker/request_batch.py:L60-L131](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/worker/request_batch.py#L60-L131)，它暴露 `prompts`、`sampling_params_list`、`kv_sender_info` 等属性，让迁移自上游的 pipeline 代码尽量少改。批内每请求的局部值（seed、generator、latent）从 `sampling_params_list` 按请求读取，`collate_*` 系列静态方法负责把「每请求张量」拼成「批张量」。

终态判定：请求级一次执行就判完成，见 [vllm_omni/diffusion/sched/request_scheduler.py:L39-L71](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/sched/request_scheduler.py#L39-L71) 的 `update_from_output`：有结果且无 error/abort 即 `FINISHED_COMPLETED`；异步模式下 `result=None` 但带 `async_output_id` 视为「算完待取」。

#### 4.2.4 代码实践

**实践目标**：体会准入等待如何提升融合批大小，并量化队头阻塞的影响。

**操作步骤（源码阅读型）**：

1. 阅读设计文档 [docs/design/feature/diffusion_request_level_batching.md:L112-L120](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_request_level_batching.md#L112-L120)，确认准入等待的四个触发前提（request batch 支持、`step_execution=False`、`request_batch_max_wait_ms>0`、当前无 running）。
2. 在脑中模拟一条到达序列（假设 `max_num_seqs=4`、`request_batch_max_wait_ms=20`、Qwen-Image 全 1024×1024 同参数）：
   - t=0ms：请求 A 到达，running 空 → 进入准入等待。
   - t=5ms：B、C 相继到达。
   - t=20ms：deadline 到，D 仍未到 → 触发 `schedule()`，批={A,B,C}。
3. 改一下序列：A 是 512×512，B/C/D 是 1024×1024。A 在队头会怎样？

**预期结果**：第 3 步中，A 的 key（含 height/width）与 B/C/D 不同，`_can_schedule_waiting` 对 A 返回 True（批空放行 A），但随后 B/C/D 因 key≠A 的 key 而 break，批只能={A}，B/C/D 被阻塞直到 A 跑完。这就是 FIFO 队头阻塞——**文档明确把它列为当前限制**。若想让 B/C/D 先批，需要业务侧按尺寸分桶提交。

**待本地验证**：若你有 GPU，可起服务 `vllm serve Qwen/Qwen-Image --omni --port 8091 --max-num-seqs 4 --request-batch-max-wait-ms 20`，并发投递 4 条同尺寸 prompt，观察日志中的 `[RequestBatch] admission wait done waiting=4 ...` 行，确认融合批大小。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `RequestBatchSamplingParamsKey` 包含 `num_inference_steps`，而 `seed` 不包含？

**参考答案**：请求级是「整条流水线一次跑完」，`num_inference_steps` 决定了去噪循环长度与 latent 张量的时间维形状，不同步数的请求无法在同一个 `forward(batch)` 里共享张量布局，必须分批。而 `seed`/generator 只影响每请求的随机噪声采样，是请求局部值，批内可各自持有，故不进 key。

**练习 2**：`request_batch_max_wait_ms` 设成 1000ms 会让每个请求都多等 1 秒吗？

**参考答案**：不会。等待有四个提前退出条件（凑满、稳定窗口、deadline、停止）。最常见的是「队列稳定 50ms 不增长」就提前触发（`stable_window_s=min(0.05, max_wait_s/5)`）。只有持续有请求零散到达时才可能拖到 deadline。文档建议延迟敏感场景取 10–50ms。

---

### 4.3 步级连续批处理：步间准入与移除

#### 4.3.1 概念说明

步级连续批处理建立在**步级执行契约**之上：把原本一次跑完的去噪循环，拆成 pipeline 的四个方法——`prepare_encode`（每请求一次性准备）、`denoise_step`（算当前步噪声预测）、`step_scheduler`（推进 latent 与 step_index）、`post_decode`（最终解码）。这个拆分给运行时留出了一个「步间窗口」：**每一步去噪之间，调度器都可以让新的兼容请求加入、让已完成的请求离开**。

它的价值场景是低 MFU 或突发流量：一条请求的单步去噪可能喂不饱 GPU，多条兼容请求共享同一次 `denoise_step` 前向就能提升利用率。文档明确强调：**这通常不是单请求延迟的胜利，而是吞吐与利用率的胜利**。

它当前是**实验特性**，且策略保守：只同批兼容请求；每请求的进度与完成彼此独立；`cache_backend`、KV 迁移等请求级附加能力尚未接入步级批路径。

#### 4.3.2 核心流程

步级一个 scheduler tick 的 worker 侧流程（`execute_stepwise`）：

```text
每 tick（一次 schedule + 一次 execute_step）：
  1. _update_states：
       - 清理上一 tick finished 的请求（移出 state_cache）   ← 步间「移除」
       - 为新请求建 StepRequestState、receive KV             ← 步间「加入」
       - 已运行请求复用其持久 state
  2. _prepare_batch_inputs：
       - 新请求跑 prepare_encode（一次性编码/建 latent/建调度器副本）
       - InputBatch.make_batch(states, cached_batch=上一 tick 的批)
           · 成员不变 → _repack_dynamic_fields（只刷新动态张量）
           · 成员变   → _rebuild（重建批视图）
  3. denoise_step(input_batch)        ← 唯一被批处理的前向
  4. 按请求切分 noise_pred → 逐请求 step_scheduler / 条件 post_decode
  5. _update_states_after：gather latents → scatter_latents 回写到各 state
```

调度器侧 `StepScheduler` 用 `_request_progress` 跟踪每请求的 `current_step/total_steps`，每次 `update_from_output` 按返回的 `step_index` 推进，`finished` 为真才判 `FINISHED_COMPLETED`。

与请求级的两个关键差异：

1. **兼容性键不含 `num_inference_steps`**：步级只共享「当前这一步的去噪张量契约」，总步数不同的请求可以同批，各自走完自己的步数后独立结束。
2. **没有准入等待**：步级靠 `schedule()` 在每个 tick 自然地「按容量 + 兼容性」吸纳 waiting 请求，不需要时间窗口凑批。

#### 4.3.3 源码精读

步级兼容性键：[vllm_omni/diffusion/sched/interface.py:L31-L65](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/sched/interface.py#L31-L65) 是 `StepBatchSamplingParamsKey`。对比请求级，它**没有** `num_inference_steps`、`sigmas`、`output_type` 等字段——这与设计文档「requests with different total step counts can still share a batch」「requests also do not need to be at the same current denoise progress」的描述完全对应。形状（height/width/num_frames）、CFG、`num_outputs_per_prompt`、LoRA identity 仍保留。FIFO 准入与队头阻塞逻辑与请求级共用基类 `_can_schedule_waiting`，无需重写。

调度器步推进：[vllm_omni/diffusion/sched/step_scheduler.py:L40-L63](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/sched/step_scheduler.py#L40-L63) 的 `add_request` 在入队时算出 `total_steps`（来自 timesteps/sigmas/num_inference_steps，见 [L121-L128](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/sched/step_scheduler.py#L121-L128)）并初始化 `_request_progress`。[L65-L116](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/sched/step_scheduler.py#L65-L116) 的 `update_from_output` 把 `req_output.step_index` 回写到 progress 与 `sampling_params.step_index`，只有 `req_output.finished` 才判终态——这正是「跨步推进、各请求独立结束」的实现。

worker 侧步级批前向：[vllm_omni/diffusion/worker/diffusion_model_runner.py:L691-L819](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_model_runner.py#L691-L819) 是 `execute_stepwise`。其中 `_update_states`（[L604-L645](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_model_runner.py#L604-L645)）做「步间移除（清理 finished）+ 步间加入（建新 state）」；`_prepare_batch_inputs`（[L647-L665](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_model_runner.py#L647-L665)）对新请求跑 `prepare_encode` 并构建 `InputBatch`。批前向在 [L719](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_model_runner.py#L719) `self.pipeline.denoise_step(input_batch, states=states)`——这是唯一被批化的前向；之后 [L740-L777](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_model_runner.py#L740-L777) 按请求切片 `noise_pred`、逐请求 `step_scheduler`、条件性 `post_decode`，证明「共享仅限去噪前向，请求级状态与输出各自独立」。

批视图的复用与重建：[vllm_omni/diffusion/worker/input_batch.py:L696-L718](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/worker/input_batch.py#L696-L718) 的 `InputBatch.make_batch`：成员不变时走 `_repack_dynamic_fields`（只刷新每步动态张量如 latents/timesteps），成员变化时走 `_rebuild`。文件末尾 [L771-L772](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/worker/input_batch.py#L771-L772) 给出别名 `StepInputBatch = InputBatch`（即设计文档里的 `StepInputBatch`）。步后回写靠 `scatter_latents`（[L751](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/worker/input_batch.py#L751) 起），它由 `idx_mapping` 驱动，把批张量切片写回各请求的持久 state。

步级执行契约与参考实现：四段式契约见 [docs/design/feature/diffusion_step_execution.md:L40-L74](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion_step_execution.md#L40-L74)；当前仅 `QwenImagePipeline` 支持（见该文档「Current Support Scope」表）。`execute_stepwise` 起始处 [L698-L700](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_model_runner.py#L698-L700) 还显式拒绝 `cache_backend`，印证「步级尚未接入缓存加速」这一限制。

#### 4.3.4 代码实践

**实践目标**：跟踪一个请求在步级路径下从「加入 → 多步推进 → 独立结束」的状态迁移，定位「步间准入/移除」的确切代码点。

**操作步骤（源码阅读型）**：

1. 设想 `max_num_seqs=2`，两条请求 R1（50 步）、R2（30 步），同尺寸同 CFG，几乎同时到达。
2. 在 [step_scheduler.py:L40-L63](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/sched/step_scheduler.py#L40-L63) 确认二者 `total_steps` 不同但仍可同批（key 不含步数）。
3. 在 [diffusion_model_runner.py:L604-L645](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_model_runner.py#L604-L645) 标注：第 1 tick 二者都是 new → 各自 `prepare_encode`；第 2..30 tick 二者都是 cached → 复用 state；第 31 tick 起 R2 已 finished → 被 `_update_states` 顶部清理移出，批只剩 R1。
4. 在 [L108-L112](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/sched/step_scheduler.py#L108-L112) 确认：每 tick `step_index` 被回写，只有 `req_output.finished` 为真（如 R2 在第 30 步、R1 在第 50 步）才判 `FINISHED_COMPLETED`。

**预期现象**：R1、R2 在第 1–30 步共享 `denoise_step` 前向（批大小=2）；第 31 步起 R2 离开，R1 独自跑完剩余 20 步（批大小=1）。两条请求**各自按自己的步数结束**，互不拖累。这演示了连续批处理相对请求级的核心优势：**批成员随步动态变化**。

**待本地验证**：若有支持步级的模型与 GPU，可对比「R1、R2 串行（max_num_seqs=1，先 R1 后 R2）」与「R1、R2 同批（max_num_seqs=2）」的总耗时。预期同批更快（前提是单步未喂饱 GPU）。

#### 4.3.5 小练习与答案

**练习 1**：步级连续批处理为什么不需要 `request_batch_max_wait_ms` 这种准入等待？

**参考答案**：步级的「凑批」机会天然存在于每个 scheduler tick——`schedule()` 每步都会按容量与兼容性从 waiting 队列吸纳请求。只要请求还在去噪循环里，后续 tick 都能把它加进来，因此不需要用一个时间窗口去「等」请求凑齐。请求级则因为「一批跑到底」，必须在波开始前凑齐，才需要准入等待。

**练习 2**：步级路径下，若一个请求在第 10 步被用户 abort，会怎样？

**参考答案**：abort 会让该请求在 `update_from_output` 中被判为终态（参考 [step_scheduler.py:L88-L91](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/sched/step_scheduler.py#L88-L91) 的 abort 分支）。下一个 tick 的 `_update_states` 顶部会把它从 `state_cache` 清理掉（[diffusion_model_runner.py:L606-L607](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_model_runner.py#L606-L607)），批成员随之收缩，其余请求不受影响——这正是「每请求进度与完成彼此独立」的体现。

---

## 5. 综合实践：`max_num_seqs=1` 与 `max_num_seqs=4` 的吞吐对比实验

本任务把本讲三个最小模块（executor 路径选择、请求级兼容性键与等待、两种模式差异）串成一个可量化实验。建议用支持请求级批处理的 Qwen-Image 模型（`supports_request_batch = True`）。

**实践目标**：度量批处理对吞吐的影响，并解释兼容性约束如何决定实际批大小。

**操作步骤**：

1. **启动服务（串行基线）**：

   ```bash
   vllm serve Qwen/Qwen-Image --omni --port 8099 --max-num-seqs 1
   ```

2. **压测（4 条同尺寸 prompt）**，使用项目自带的在线服务压测脚本：

   ```bash
   python3 benchmarks/diffusion/diffusion_benchmark_serving.py \
       --base-url http://localhost:8099 \
       --model Qwen/Qwen-Image \
       --task t2i \
       --dataset vbench \
       --num-prompts 4 \
       --width 1024 --height 1024 \
       --num-inference_steps 30
   ```

   记录吞吐（requests/s）与延迟百分位。

3. **重启服务（批处理）**：

   ```bash
   vllm serve Qwen/Qwen-Image --omni --port 8099 \
       --max-num-seqs 4 --request-batch-max-wait-ms 20
   ```

4. **重复步骤 2 的压测**，记录吞吐与延迟。

5. **对照源码解释结果**：
   - 在 [diffusion_engine.py:L193-L211](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L193-L211) 确认两次启动都解析为 `REQUEST_BATCH`（`step_execution` 未开）。
   - 在 [multiproc_executor.py:L521-L529](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/executor/multiproc_executor.py#L521-L529) 解释：`max_num_seqs=1` 时每波只 1 个请求 → 回退 `execute_request`（4 次独立前向）；`max_num_seqs=4` 时若 4 条 prompt 同尺寸同参数 → key 相同 → 融合成 1 次 `execute_model_batch`。
   - 在 [interface.py:L68-L113](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/sched/interface.py#L68-L113) 解释兼容性约束：4 条 prompt 必须在 height/width/num_inference_steps/CFG 等 key 字段上完全一致才能同批；若混入一条 512×512，它会被 key 隔离成单独一波。

**需要观察的现象**：

- 吞吐：`max_num_seqs=4` 应明显高于 `max_num_seqs=1`（待本地验证具体倍数，理论上限是单请求未喂饱 GPU 的程度）。
- 首请求延迟：`request_batch_max_wait_ms=20` 会给新一波首请求增加最多约 20ms 等待，但通常因「稳定窗口」提前退出。
- 日志：`max_num_seqs=4` 启动时应有 `[RequestBatch] engine init max_num_seqs=4 ...`（[diffusion_engine.py:L278-L283](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L278-L283)）；运行期应有 `[RequestBatch] admission wait done waiting=4 ...`（[L518-L523](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L518-L523)）。

**预期结果与解释**：批处理通过把 4 次独立去噪循环合并为 1 次更大的批前向，提升了 GPU 利用率从而提升吞吐。但收益受兼容性约束限制——只有 key 一致的请求才能合并；异构流量（不同分辨率/步数）会因队头阻塞或 key 隔离而无法充分批处理。这正是文档把「FIFO 队头阻塞」「仅同质批」列为当前限制的原因。

**说明**：若无 GPU 或无 Qwen-Image 权重，可将本实践降级为纯源码阅读——手动模拟 4 条 prompt 的 key 构造（用 [request_scheduler.py:L30-L37](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/sched/request_scheduler.py#L30-L37) 的逻辑），判断它们能否同批，并画出两次启动下的 executor 路径分派图。

## 6. 本讲小结

- **两种模式互斥且在初始化期定死**：`step_execution=True` 走 `STEP_BATCH` + `StepScheduler` + `execute_step`；否则走 `REQUEST_BATCH` + `RequestScheduler` + `execute_batch`。`max_num_seqs>1` 但 pipeline 不支持 `supports_request_batch` 会在引擎构造期 fail-fast。
- **请求级批处理**是「一批跑到底」的静态批：靠 `RequestBatchSamplingParamsKey`（含步数/形状/CFG/LoRA）筛选兼容请求，靠 `request_batch_max_wait_ms` 在波前短暂凑批；`max_num_seqs=1` 时回退到逐请求串行路径。
- **步级连续批处理**是「批随步变」的动态批：靠 `StepBatchSamplingParamsKey`（**不含**步数）筛选兼容请求，在每个 scheduler tick 自然吸纳/移除请求；唯一被批化的是 `denoise_step` 前向，请求级状态与输出始终独立。
- **兼容性键是共同安全闸**：两种模式都用 frozen dataclass 作 key，把形状与引导敏感字段纳入、把请求局部值（seed/generator/latent）排除；key 不同则分批，宁可少批不可错批。
- **FIFO 队头阻塞是共同约束**：`_can_schedule_waiting` 要求队头 key 等于当前批 key，否则 break，异构队头会挡住后续兼容请求。
- **步级尚是实验特性**：`cache_backend`、KV 迁移等请求级附加能力尚未接入步级批路径；当前仅 `QwenImagePipeline` 支持步级契约。

## 7. 下一步学习建议

- **若关心「如何让我的 pipeline 支持批处理」**：阅读 `docs/contributing/model/adding_diffusion_model.md` 与 `docs/design/feature/diffusion_step_execution.md` 的「Rules For New Pipelines」「Validation Checklist」，了解如何声明 `supports_request_batch` 或实现四段式步级契约。
- **若关心批处理与并行的交叉**：回顾 u7-l4（并行策略），理解 `dp_concurrent`（DLO+AllGather）如何让 `max_num_running_reqs=dp_size` 并对每 rank 投递不同请求，这是请求级批处理在 DP 维度的特殊变体（见 `diffusion_engine.py` 的 `_init_runtime_state`）。
- **若关心批处理下的性能剖析**：结合 u8-l3（基准测试与性能剖析），用 `diffusion_benchmark_serving` 的 `trace` 数据集投递异构请求，观察兼容性约束如何影响实际批大小与 MFU。
- **继续阅读源码**：从 `DiffusionEngine._busy_loop`（[diffusion_engine.py:L432-L462](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L432-L462)）出发，跟踪一次多请求波从「准入等待 → schedule → execute_batch/execute_step → update_from_output → emit」的完整闭环，把本讲的三条路径在脑中跑通。
