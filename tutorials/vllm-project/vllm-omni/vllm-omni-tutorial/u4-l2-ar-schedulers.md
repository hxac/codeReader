# AR 调度器：OmniARScheduler 与 OmniGenerationScheduler

## 1. 本讲目标

上一讲（u4-l1）我们已经建立了「每个 stage 是独立 EngineCore 子进程，AR 模块沿 vLLM 的 Scheduler/Worker/ModelRunner 四层派生子类」的全局认知。本讲把镜头拉近，**专门拆解调度器这一层**：

- 理解 vLLM-Omni 如何用一个 `execution_type`（执行类型）在两种 AR 调度器之间做选择。
- 精读「[1/N] Scheduler 重构」（PR #5461）如何把 AR 与 Generation 调度器里**重复的调度管道（plumbing）上提到共享基类 `OmniSchedulerMixin`**，建立显式的输入/输出生命周期契约。
- 精读 `OmniARScheduler.schedule()` 如何把 vLLM 原生的 `NewRequestData` **重包装**成 `OmniNewRequestData`（现在走 mixin 的 `_postprocess_omni_schedule_output` + `from_base`），并附加 `prompt_embeds` / `additional_information` / `model_intermediate_buffer` 三类载荷。
- 精读 `OmniGenerationScheduler` 的**单步一次性快路径**：一次 `schedule()` 分配全部输入 token，一次 `update_from_output()` 立即判定请求完成。
- 掌握 AR 模块请求流：`InputProcessor → Scheduler → Worker → ModelRunner → OutputProcessor`。
- 通过对比实践，写出两种调度器在「token 分配」与「请求何时 finished」上的差异表，并指出各自复用了 `OmniSchedulerMixin` 的哪些共享 helper。

学完后，你应能回答：**为什么 Thinker/Talker（标准自回归）用 `OmniARScheduler`，而 Code2wav（卷积/LSTM 等基础异构结构）用 `OmniGenerationScheduler`？以及重构后两者共享了哪些「调度管道」、各自又保留了哪些差异？** 答案就藏在两个调度器对 token 的分配、结束判定方式，以及共享基类 `OmniSchedulerMixin` 提供的 helper 契约里。

## 2. 前置知识

阅读本讲前，请确保理解以下概念（若不熟悉，可先回顾 u3-l3、u4-l1）：

- **stage（阶段）**：vLLM-Omni 把一个全模态请求拆成的顺序子任务，每个 stage 是一个独立的 EngineCore 子进程（如 Qwen3-Omni 的 Thinker → Talker → Code2wav 三阶段）。
- **调度器（Scheduler）**：vLLM v1 的核心组件，负责决定「这一步调度哪些请求、每条请求算多少 token」，并产出 `SchedulerOutput` 交给 Worker 执行。vLLM v1 的基类是 `vllm.v1.core.sched.scheduler.Scheduler`（下文记作 `VLLMScheduler`）。
- **`NewRequestData` / `SchedulerOutput`**：vLLM 描述「本步新纳入请求」与「本步调度结果」的数据结构。本讲会频繁看到前者被 omni「重包装」。
- **monkey-patch（猴子补丁）**：vLLM-Omni 在不修改 vLLM 源码的前提下，运行时替换其方法/属性（见 u2-l1）。
- **两阶段 execute/sample**：AR ModelRunner 的 `execute_model()` 只跑前向返回 `None`，再由 `sample_tokens()` 采样（见 u4-l1）。
- **chunked prefill / decode**：vLLM 把长 prompt 分块预填（prefill）、生成阶段逐 token 解码（decode）的机制。
- **Mixin 模式**：Python 的多继承协作技巧——把一组可复用方法放进一个「混入类」，让两个本无父子关系的类各自继承它以共享实现。本讲的 `OmniSchedulerMixin` 就是这种混入类。

一个关键直觉：vLLM 的调度器是为「逐 token 生成」设计的——它每步只给每条请求分配少量 token，请求要经历很多步才结束。但 omni 的某些 stage（如 Code2wav 把音频 latent 一次性转成波形）是**单步前向就完成**的「基础异构结构」。用标准调度器跑这种 stage 会很别扭，于是 omni 专门写了 `OmniGenerationScheduler` 提供一条「一次喂入全部 token、一步完成」的快路径。而这两条路径又有大量重复的「善后」逻辑（消费 connector 信号、重包装输出、拼装 EngineCoreOutput……），重构便把这些重复部分收敛进了 `OmniSchedulerMixin`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [vllm_omni/config/stage_config.py](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/config/stage_config.py) | 用 `_resolve_scheduler(execution_type, async_scheduling)` 决定一个 stage 用哪个调度器类；定义 `StageExecutionType` 枚举。 |
| [vllm_omni/core/sched/omni_scheduler_mixin.py](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/omni_scheduler_mixin.py) | **`OmniSchedulerMixin`（[1/N] 重构的主战场）**：两个调度器共享的「调度管道」——状态初始化、消费 connector 信号、超时兜底、重包装输出、拼装 EngineCoreOutput、收尾统计等，并显式定义了 full-payload input_coordinator 与 async-chunk 两套输入等待的生命周期契约。 |
| [vllm_omni/core/sched/omni_ar_scheduler.py](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/omni_ar_scheduler.py) | `OmniARScheduler`（标准 AR）与 `OmniARAsyncScheduler`（异步变体），核心是 `schedule()` 经 mixin helper 重包装、`update_from_output()` 的多步生命周期与 KV 迁移管理。 |
| [vllm_omni/core/sched/omni_generation_scheduler.py](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/omni_generation_scheduler.py) | `OmniGenerationScheduler`：单步一次性快路径调度器，`update_from_output()` 一步判定完成。 |
| [vllm_omni/core/sched/output.py](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/output.py) | `OmniNewRequestData`（含重构新增的 `from_base` 类方法）/ `OmniCachedRequestData` / `OmniChunkRecvHandle`（重构新增）/ `OmniSchedulerOutput`：被富化的调度输出数据结构。 |
| [vllm_omni/core/sched/omni_scheduling_coordinator.py](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/omni_scheduling_coordinator.py) | `OmniSchedulingCoordinator`：管理下游 stage「等上游 full-payload 载荷到达」的 `WAITING_FOR_INPUT` 状态，被两个调度器经 mixin 的 `input_coordinator` 复用。 |

> 提示：本讲引用的永久链接基线为当前 HEAD `5215e03a`（即 [1/N] 重构 PR #5461 合入后的版本），行号以此为准。

## 4. 核心概念与源码讲解

### 4.1 调度器选型与请求流总览

#### 4.1.1 概念说明

vLLM-Omni 不只有一个 AR 调度器，而是**按 stage 的执行类型（execution_type）分派**。这承接 u2-l2 的「配置体系」与 u3-l3 的「stage 进程」：每个 stage 在构建 `StageConfig` 时就决定了它属于哪种执行类型，进而决定用哪个调度器、哪个 worker、哪个 model runner。

执行类型由枚举 `StageExecutionType` 给出，只有三种：

[vllm_omni/config/stage_config.py:176-181](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/config/stage_config.py#L176-L181) —— 定义三个执行类型：`LLM_AR`（标准自回归）、`LLM_GENERATION`（单步生成的基础异构结构）、`DIFFUSION`（扩散，本讲不涉及）。

调度器的分派逻辑在一个纯函数 `_resolve_scheduler` 里，逻辑非常直观：

```python
def _resolve_scheduler(execution_type, async_scheduling=True):
    if execution_type == StageExecutionType.LLM_AR:
        if not async_scheduling:
            return OmniARScheduler
        return OmniARAsyncScheduler
    if execution_type == StageExecutionType.LLM_GENERATION:
        return OmniGenerationScheduler
    return None  # Diffusion 不走这套
```

见 [vllm_omni/config/stage_config.py:184-201](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/config/stage_config.py#L184-L201)。

注意三个要点：

1. **`LLM_AR` 有同步/异步两个调度器**：`OmniARScheduler`（同步）与 `OmniARAsyncScheduler`（异步，后者继承前者并再继承 vLLM 的 `AsyncScheduler`）。选哪个由 `async_scheduling` 标志决定。在 stage 配置装配阶段，代码还会根据选中的类**反向回写** `engine_args["async_scheduling"]`，保证一致性（见 [vllm_omni/config/stage_config.py:943-944](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/config/stage_config.py#L943-L944)）。
2. **`LLM_GENERATION` 永远是 `OmniGenerationScheduler`**，不分同步异步。
3. **`DIFFUSION` 返回 `None`**——扩散 stage 不走 vLLM 调度器，它有自己的 `DiffusionEngine` 与 diffusion scheduler（见 U5）。

`StageExecutionType` 同时也映射出 `worker_type`（`ar` / `generation`），即调度器、worker、model runner 三者由同一个 `execution_type` 串起来：

[vllm_omni/config/stage_config.py:769-773](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/config/stage_config.py#L769-L773) —— `LLM_AR → (LLM, "ar")`、`LLM_GENERATION → (LLM, "generation")`、`DIFFUSION → (DIFFUSION, None)`。

#### 4.1.2 核心流程：AR 模块请求流

两种调度器都嵌在同一个请求流里，差别只在「调度」与「结束判定」两环。完整的 AR 请求流（来自设计文档）如下：

```
InputProcessor(stage-0, 在 AsyncOmniEngine 中)
   │  产出 EngineCoreRequest，再被 upgrade 成 OmniEngineCoreRequest
   ▼
OmniARScheduler / OmniGenerationScheduler
   │  schedule() → SchedulerOutput（新请求被重包装为 OmniNewRequestData）
   ▼
GPUARWorker / GPUGenerationWorker
   │  把 SchedulerOutput 交给 model runner
   ▼
GPUARModelRunner / GPUGenerationModelRunner
   │  execute_model()（AR 还会 sample_tokens()）
   │  → 产出 OmniModelRunnerOutput（含 pooler_output / multimodal_outputs）
   ▼
OmniARScheduler / OmniGenerationScheduler
   │  update_from_output() → 决定请求是否 finished、产出 EngineCoreOutputs
   ▼
MultimodalOutputProcessor
   │  按模态路由、累积张量，产出 RequestOutput
   ▼
下游 stage 或客户端
```

关键洞察：

- **stage-0 的 `InputProcessor` 用的是 vLLM 原生的**，`AsyncOmniEngine` 在它之后用 `_upgrade_to_omni_request()` 把被丢弃的 omni 字段（`additional_information`、`prompt_embeds`）捡回来（见 u2-l3）。
- **调度器不直接产生推理结果**，它只决定「调度谁、算多少 token、何时结束」，并把 omni 载荷塞进 `SchedulerOutput`。真正的前向计算在 model runner。
- **结束判定**是两种调度器最大的分野：AR 靠标准的多步停止条件（EOS / max_tokens / 停止串），Generation 靠「全部 prompt token 算完」一步判完。
- **两者的「调度管道」高度重叠**：消费 connector 信号、重包装 `NewRequestData`、拼装 `EngineCoreOutputs`、收尾统计——重构后这些都进了 `OmniSchedulerMixin`（见 4.2）。

#### 4.1.3 源码精读

两个调度器都先继承 `OmniSchedulerMixin` 再继承 `VLLMScheduler`，从而获得共享工具：

[vllm_omni/core/sched/omni_ar_scheduler.py:73](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/omni_ar_scheduler.py#L73) 与 [vllm_omni/core/sched/omni_generation_scheduler.py:29](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/omni_generation_scheduler.py#L29) —— 两者类头都是 `class XxxScheduler(OmniSchedulerMixin, VLLMScheduler)`。

`OmniARAsyncScheduler` 的定义极简——它只是把 `OmniARScheduler` 与 vLLM 的 `AsyncScheduler` 多继承拼到一起，所有 AR 调度逻辑都集中在 `OmniARScheduler`：

[vllm_omni/core/sched/omni_ar_scheduler.py:815](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/omni_ar_scheduler.py#L815) —— `class OmniARAsyncScheduler(OmniARScheduler, AsyncVLLMScheduler)`，异步变体复用同步版的全部逻辑。

#### 4.1.4 代码实践

**实践目标**：在真实源码里验证「执行类型 → 调度器类」的映射，确认一个具体模型的每个 stage 落到哪个调度器。

**操作步骤**：

1. 打开 `vllm_omni/config/stage_config.py`，定位 `_resolve_scheduler`（L184）。
2. 在 `examples/offline_inference/qwen3_omni/end2end.py`（或 Qwen3-Omni 的 pipeline 注册处）找到 Thinker / Talker / Code2wav 三个 stage 的 `execution_type` 声明。
3. 对照 `_resolve_scheduler`，把每个 stage 映射到调度器类，填入下表（待本地确认具体声明位置）：

| stage | execution_type（推断） | 调度器类 |
| --- | --- | --- |
| Thinker（文本/COT 自回归） | `LLM_AR` | `OmniARScheduler`/`OmniARAsyncScheduler` |
| Talker（音频 latent 自回归） | `LLM_AR` | `OmniARScheduler`/`OmniARAsyncScheduler` |
| Code2wav（latent→波形） | `LLM_GENERATION` | `OmniGenerationScheduler` |

**需要观察的现象**：Thinker/Talker 走标准 AR 调度，Code2wav 走快路径——这正解释了「为什么同是 AR 模块却需要两个调度器」。

**预期结果**：能清晰说出每个 stage 的调度器选型依据是 `execution_type`，而非模型名。

#### 4.1.5 小练习与答案

**练习 1**：如果新增一个纯文本 LLM stage（如 BAGEL 的 thinker），它会用哪个调度器？为什么？
**参考答案**：`OmniARScheduler`（或异步版）。因为纯文本逐 token 生成属于标准自回归，`execution_type=LLM_AR`。

**练习 2**：为什么 `_resolve_scheduler` 对 `DIFFUSION` 返回 `None`？
**参考答案**：扩散 stage 不复用 vLLM 的请求/调度抽象，它有独立的 `DiffusionEngine` 与 diffusion scheduler（U5），因此不返回 vLLM 调度器类。

---

### 4.2 OmniSchedulerMixin：[1/N] 重构的共享基座

#### 4.2.1 概念说明

在 [1/N] 重构（PR #5461「Remove duplicated AR/generation scheduler plumbing and establish explicit shared lifecycle contracts」）之前，`OmniARScheduler` 与 `OmniGenerationScheduler` 各自写了一份几乎相同的「调度管道」：在 `schedule()` 开头消费 connector 信号、在尾部把原生 `NewRequestData` 重包装成 `OmniNewRequestData`、在 `update_from_output()` 末尾拼装 `EngineCoreOutputs` 并附统计……这部分代码是「复制粘贴」的，改一处要同步改两处，极易漂移。

重构把这些重复逻辑**上提到共享混入类 `OmniSchedulerMixin`**。它不是 `VLLMScheduler` 的子类，而是一个独立工具集，被两个调度器通过多继承「拌」进来。重构后，`OmniSchedulerMixin` 承担三类职责：

1. **I/O 调度状态的统一初始化**（`_init_omni_io_scheduling_state`）——两个调度器的 `__init__` 都调用它。
2. **共享的调度/输出 helper**——重包装、超时兜底、输出拼装、收尾统计等，两个调度器复用。
3. **显式的输入/输出生命周期契约**——full-payload input_coordinator 与 async-chunk 两套输入等待机制，以及「输出先暂存、下一轮再消费」的 staging 约定。

> 为什么用 Mixin 而不是让两个调度器继承同一个 omni 中间基类？因为它们**最终都必须继承 vLLM 的 `VLLMScheduler`**（MRO 与上游校验依赖它），再加一层 omni 基类会与 vLLM 的继承体系打架。Mixin 只贡献方法、不进入 `VLLMScheduler` 的继承链，是最干净的协作方式。

#### 4.2.2 核心流程

`OmniSchedulerMixin` 的方法可以按「调度周期」的四个时机归类，串起来正好是一个完整的 `schedule()` → `update_from_output()` 轮次：

```
┌─ schedule() 开头（消费上一轮暂存的输入信号）──────────────┐
│  _process_pending_omni_inputs(model_mode):               │
│    ├ _consume_pending_connector_output(model_mode)       │  ← 消费 _latest_omni_connector_output
│    ├ _process_pending_input_timeouts()                   │  ← full-payload 等待超时兜底
│    └ chunk_transfer_adapter.process_pending_chunks()     │  ← async-chunk 流式分块
└──────────────────────────────────────────────────────────┘
┌─ schedule() 主体（各自实现：AR 委托 super，Generation 快路径）┐
└──────────────────────────────────────────────────────────┘
┌─ schedule() 尾部（重包装 + 包成 OmniSchedulerOutput）────────┐
│  _postprocess_omni_schedule_output(scheduler_output):    │
│    ├ _rewrap_scheduled_new_reqs()  ← NewRequestData → OmniNewRequestData
│    └ chunk_transfer_adapter.postprocess_scheduler_output()
│  _restore_omni_wait_queues()        ← 把临时停车（WAITING_FOR_INPUT/CHUNK）的请求放回
│  _wrap_omni_scheduler_output()      ← 包成 OmniSchedulerOutput，附 KV 迁移/chunk 注册元数据
└──────────────────────────────────────────────────────────┘
┌─ update_from_output() 末尾（收尾统计 + 暂存下一轮输入信号）────┐
│  _make_omni_engine_output / _append_request_output        │  ← 拼装单请求 EngineCoreOutput
│  _attach_finished_request_sets(synthesize_abort_outputs)  │  ← AR 合成 abort 输出，Generation 不合成
│  _attach_scheduler_stats / _aggregate_kv_connector_stats  │  ← 统计
│  _capture_omni_connector_output(model_runner_output)      │  ← 仅暂存，下一轮 schedule 才消费
└──────────────────────────────────────────────────────────┘
```

两个细节值得强调：

- **「暂存而非立即消费」的生命周期约定**：`_capture_omni_connector_output`（`update_from_output` 末尾）只把 model runner 的 `omni_connector_output` 暂存到 `self._latest_omni_connector_output`，真正的 `update_request_metadata` 推迟到**下一轮** `_consume_pending_connector_output`（`schedule` 开头）执行。注释明确解释：在 generation 模式下 `update_request_metadata` 会重置 `prompt_token_ids`/`_output_token_ids`/`num_computed_tokens`，若在 `update_from_output` 里立即消费会「踩掉」两轮之间刚刚推进的进度。
- **full-payload 与 async-chunk 两套输入等待**：下游 stage 可能要等上游 stage 把一整块载荷送达后才能开始。omni 用两条独立路径管理这种「等输入」状态——`input_coordinator`（`OmniSchedulingCoordinator`，整块 full-payload，对应 `WAITING_FOR_INPUT`）与 `chunk_transfer_adapter`（流式分块，对应 `WAITING_FOR_CHUNK`）。两者的初始化、消费、恢复都在 mixin 里统一。

#### 4.2.3 源码精读

**I/O 调度状态初始化**（两个调度器的 `__init__` 都调用）：

[vllm_omni/core/sched/omni_scheduler_mixin.py:71-82](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/omni_scheduler_mixin.py#L71-L82) —— `_init_omni_io_scheduling_state` 按 `async_chunk` 与 `uses_full_payload_input_coordinator(model_config)` 两个开关，分别创建 `chunk_transfer_adapter`（async-chunk 路径）与 `input_coordinator`（full-payload 路径），并初始化暂存槽 `_latest_omni_connector_output`。这正是「谁需要哪条等待路径」在调度器侧的唯一落点。

- AR 调度器在 `__init__` 末尾调用它：[omni_ar_scheduler.py:107](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/omni_ar_scheduler.py#L107)。
- Generation 调度器同样在 `__init__` 调用：[omni_generation_scheduler.py:33](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/omni_generation_scheduler.py#L33)。

**`schedule` 开头统一消费输入信号**：

[vllm_omni/core/sched/omni_scheduler_mixin.py:141-150](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/omni_scheduler_mixin.py#L141-L150) —— `_process_pending_omni_inputs(model_mode)` 串起三件事：消费上一轮暂存的 connector 输出、对 full-payload 等待做超时兜底、推进 async-chunk 分块。`model_mode` 参数（`"ar"` / `"generation"`）是 AR 与 Generation 之间唯一的差异点——它被透传给 `update_request_metadata`。

[vllm_omni/core/sched/omni_scheduler_mixin.py:120-139](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/omni_scheduler_mixin.py#L120-L139) —— `_consume_pending_connector_output` 把暂存的 `omni_connector_output` 的 `request_metadata` 喂给 `input_coordinator.update_request_metadata`，并调用 `process_pending_full_payload_inputs` 推进 full-payload 等待。

**超时兜底**（full-payload 路径的安全网）：

[vllm_omni/core/sched/omni_scheduler_mixin.py:163-199](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/omni_scheduler_mixin.py#L163-L199) —— `_process_pending_input_timeouts` 读取 coordinator 的 `_waiting_since` 时间戳，把等待超过 `DEFAULT_INPUT_WAIT_TIMEOUT_S`（默认 600 秒，可由环境变量 `VLLM_OMNI_INPUT_WAIT_TIMEOUT_S` 覆盖）的请求强制 `FINISHED_ERROR`，防止「生产者丢弃载荷、消费者永远卡死」。注释明确标注它的作用域**只覆盖 `input_coordinator`（full-payload），不覆盖 async-chunk 路径**。

**重包装与输出包装**（`schedule` 尾部）：

[vllm_omni/core/sched/omni_scheduler_mixin.py:242-249](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/omni_scheduler_mixin.py#L242-L249) —— `_rewrap_scheduled_new_reqs` 遍历 `scheduler_output.scheduled_new_reqs`，已经是 `OmniNewRequestData` 的原样保留，否则用 `OmniNewRequestData.from_base(data, self.requests.get(data.req_id))` 重建（见 4.5 的 `from_base`）。

[vllm_omni/core/sched/omni_scheduler_mixin.py:251-267](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/omni_scheduler_mixin.py#L251-L267) —— `_postprocess_omni_schedule_output` 把「重包装」与「chunk adapter 后处理」串起来，参数 `include_cached_payloads` 让 AR（带 cached 载荷）与 Generation 快路径（不带）共用一段代码。

[vllm_omni/core/sched/omni_scheduler_mixin.py:219-240](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/omni_scheduler_mixin.py#L219-L240) —— `_wrap_omni_scheduler_output` 用 `getattr` 逐字段把基类 `SchedulerOutput` 的数据搬进 `OmniSchedulerOutput`，并注入 `finished_requests_needing_kv_transfer` 与 `pending_input_registrations` 两个 omni 字段。注释自嘲「lifted from 4 separate copy-pastes between AR (1) and generation (3) schedulers」——这正是重构动机的写照。

**`update_from_output` 末尾的收尾与暂存**：

[vllm_omni/core/sched/omni_scheduler_mixin.py:201-217](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/omni_scheduler_mixin.py#L201-L217) —— `_capture_omni_connector_output` 只暂存、不消费（理由见 4.2.2）。

[vllm_omni/core/sched/omni_scheduler_mixin.py:307-319](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/omni_scheduler_mixin.py#L307-L319) —— `_append_request_output` 把一个请求的各字段打包成 `OmniEngineCoreOutput`（经 `_make_omni_engine_output`，L269-305）并按 `client_index` 归桶。

[vllm_omni/core/sched/omni_scheduler_mixin.py:341-364](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/omni_scheduler_mixin.py#L341-L364) —— `_attach_finished_request_sets(synthesize_abort_outputs=...)`：这是 AR 与 Generation 的一个**显式差异契约**——AR 传 `True`（为 finished 请求合成 abort 输出，保证客户端收到收尾信号），Generation 传 `False`。

#### 4.2.4 代码实践

**实践目标**：在源码里逐条核对本模块列出的 mixin helper，确认 AR 与 Generation 调用的是**同一个**方法。

**操作步骤**：

1. 打开 [omni_scheduler_mixin.py](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/omni_scheduler_mixin.py)，对以下方法各记一行：`_init_omni_io_scheduling_state`、`_process_pending_omni_inputs`、`_postprocess_omni_schedule_output`、`_wrap_omni_scheduler_output`、`_append_request_output`、`_capture_omni_connector_output`、`_attach_finished_request_sets`。
2. 用 `Grep` 在 `omni_ar_scheduler.py` 与 `omni_generation_scheduler.py` 中分别搜这些方法名，确认**两者都在调用**。

**需要观察的现象 / 预期结果**：你会看到 AR 与 Generation 的 `schedule()` / `update_from_output()` 末尾几乎是「镜像」的——同样调 `_postprocess_omni_schedule_output`、`_wrap_omni_scheduler_output`、`_attach_finished_request_sets`、`_capture_omni_connector_output`。差异只在那几个「开关参数」（`model_mode`、`include_cached_payloads`、`synthesize_abort_outputs`）上。**待本地验证**：可用 `grep -n "_postprocess_omni_schedule_output\|_wrap_omni_scheduler_output\|_attach_finished_request_sets" vllm_omni/core/sched/omni_ar_scheduler.py vllm_omni/core/sched/omni_generation_scheduler.py` 一目了然地看到「镜像调用」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_capture_omni_connector_output` 只暂存、不在 `update_from_output` 里立即消费？
**参考答案**：因为在 generation 模式下，`update_request_metadata` 会重置 `prompt_token_ids`/`_output_token_ids`/`num_computed_tokens`。若在本轮 `update_from_output` 立即消费，会「踩掉」两轮之间刚刚推进的进度。推迟到下一轮 `schedule` 开头消费才安全。

**练习 2**：`_attach_finished_request_sets` 的 `synthesize_abort_outputs` 参数为什么 AR 传 `True`、Generation 传 `False`？
**参考答案**：AR 调度器需要为 finished 请求合成 abort 输出，保证客户端/上游收到明确的收尾信号；Generation 快路径的语义不同，不合成 abort 输出。这是两个调度器在「显式生命周期契约」上的一个受控差异点。

---

### 4.3 OmniARScheduler.schedule 的重包装逻辑

#### 4.3.1 概念说明

`OmniARScheduler` 服务于**标准自回归 stage**（Thinker、Talker 这类逐 token 生成）。它的设计哲学是「**最小改动**」——**不重写 vLLM 的调度算法**（分块 prefill、decode、连续批处理、抢占等全部沿用），只在两件事上做增量：

1. **`schedule()` 的尾部重包装**：把 vLLM 产出的原生 `NewRequestData` 重新包成 `OmniNewRequestData`，附加上 omni 的三类载荷，让下游 model runner 能拿到跨阶段数据。重构后这一步由 mixin 的 `_postprocess_omni_schedule_output`（内部调 `_rewrap_scheduled_new_reqs` + `from_base`）完成。
2. **`update_from_output()` 的多步生命周期 + KV 迁移管理**：在标准停止判定之外，叠加「KV cache 迁移到下一 stage」的触发与等待逻辑。这部分是 AR 独有的，仍在 `OmniARScheduler` 自身。

为什么要重包装？因为 vLLM 的 `NewRequestData` 不认识 omni 字段，`additional_information` 这类载荷在 vLLM 内部会被丢弃。调度器是「vLLM 边界」的最后一站，必须在这里把载荷从「活的 `Request` 对象」捡回来，挂到调度输出上，才能跨越进程边界传到 worker/model runner。

#### 4.3.2 核心流程

重构后 `OmniARScheduler.schedule()` 极为简洁，几乎全是 mixin 调用：

```
schedule(throttle_prefills):
  1. 清理 waiting/running 中 FINISHED_ABORTED 的请求（omni 允许异步 abort 暂留）
  2. _process_pending_omni_inputs("ar")              # ★ mixin：消费 connector 信号 + 超时 + chunk
  3. （可选）推迟 waiting 准入：_should_defer_waiting_admission()
  4. scheduler_output = super().schedule()           # ★ 交给 vLLM 原生调度（决定 token 分配）
     finally: _restore_omni_wait_queues()            # ★ mixin：恢复被 input/chunk 门停车的请求
  5. _postprocess_omni_schedule_output(
        scheduler_output, include_cached_payloads=True)   # ★ mixin：重包装 + chunk 后处理
  6. finished_reqs = get_finished_requests_needing_kv_transfer()
  7. return _wrap_omni_scheduler_output(             # ★ mixin：包成 OmniSchedulerOutput
        scheduler_output, finished_requests_needing_kv_transfer=finished_reqs)
```

注意第 4 步：**AR 调度器自己不决定 token 分配**，完全委托 `super().schedule()`。它只负责「包裹载荷」与「善后」——而善后工作现在都委托给了 mixin。

`update_from_output()` 仍较重（多步推进 + KV 迁移），但末尾的输出拼装、统计、暂存等也走 mixin（`_append_request_output` / `_attach_finished_request_sets(synthesize_abort_outputs=True)` / `_capture_omni_connector_output`）。

#### 4.3.3 源码精读

**`schedule()` 全貌**（重构后非常薄）：

[vllm_omni/core/sched/omni_ar_scheduler.py:221-257](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/omni_ar_scheduler.py#L221-L257) —— 清理 aborted（L226-229）→ `_process_pending_omni_inputs("ar")`（L230）→ `super().schedule()`（L238）配 `finally` 里 `_restore_omni_wait_queues()`（L245）→ `_postprocess_omni_schedule_output(..., include_cached_payloads=True)`（L247-250）→ 收集 KV 迁移请求（L251）→ `_wrap_omni_scheduler_output(...)`（L254-257）。

**重包装的真正实现**（已搬到 mixin）。`_rewrap_scheduled_new_reqs` 用 `from_base` 重建，保留所有基类字段并补齐 omni 载荷，不再像旧版那样在 AR 文件里手写一大段字段拷贝：

[vllm_omni/core/sched/omni_scheduler_mixin.py:242-249](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/omni_scheduler_mixin.py#L242-L249) —— 已是 `OmniNewRequestData` 的原样保留，否则 `OmniNewRequestData.from_base(data, self.requests.get(data.req_id))` 重建。这正是 u2-l1/u2-l3 反复强调的「载荷在多边界被丢弃、需反复捡回」的体现：用 `getattr` 从「活的 `Request`」捡回 `prompt_embeds`/`additional_information`/`model_intermediate_buffer`。

**停止判定与结束**（`update_from_output()` 内，仍在 AR 自身）。AR 的「finished」由三股力量共同决定：

- 标准停止条件：[L403](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/omni_ar_scheduler.py#L403) `_update_request_with_output()` 应用新 token 并返回 `stopped`（含 EOS/max_tokens/停止串）。
- Pooling 短路：[L411-414](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/omni_ar_scheduler.py#L411-L414) 若有 pooling 参数且有输出，立即 `FINISHED_STOPPED`。
- KV 迁移触发停止：[L419](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/omni_ar_scheduler.py#L419) `_process_kv_transfer_trigger()` 返回 True 表示「必须停止以触发 KV 迁移」。

命中 `stopped` 后，是否真正结束由 `_handle_stopped_request()` 决定（[L443](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/omni_ar_scheduler.py#L443)）。它返回 `finished=True` 才真正调用 `_free_request()` 释放资源（[L463-464](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/omni_ar_scheduler.py#L463-L464)）；流式输入场景下它可能返回 `False`，让请求继续等待下一个分段。`update_from_output` 末尾的输出归桶走 mixin 的 `_append_request_output`（[L481](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/omni_ar_scheduler.py#L481)），收尾统计与暂存走 `_attach_finished_request_sets(synthesize_abort_outputs=True)`（[L538-541](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/omni_ar_scheduler.py#L538-L541)）、`_capture_omni_connector_output`（[L551](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/omni_ar_scheduler.py#L551)）。

**`__init__` 的 KV 迁移账本 + mixin 初始化**：`OmniARScheduler` 在构造时初始化了大量 KV 迁移相关状态（它相对 Generation 调度器更「重」的原因），并在末尾调用 mixin 的 `_init_omni_io_scheduling_state()`：

[vllm_omni/core/sched/omni_ar_scheduler.py:79-109](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/omni_ar_scheduler.py#L79-L109) —— 初始化 `requests_needing_kv_transfer`、`waiting_for_transfer_free`、`active_kv_transfers`、`pending_stop_after_extraction` 等账本（L83-96），并在 L107 调 `_init_omni_io_scheduling_state()` 创建 `chunk_transfer_adapter` / `input_coordinator`。

#### 4.3.4 代码实践

**实践目标**：跟踪一次重包装，看清「哪些载荷从哪个对象被捡回」，并确认重包装逻辑已不在 AR 文件内。

**操作步骤**：

1. 在 [omni_scheduler_mixin.py:242-249](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/omni_scheduler_mixin.py#L242-L249) 的 `_rewrap_scheduled_new_reqs` 里，确认：`data`（原生 `NewRequestData`）来自 `super().schedule()` 产出；`self.requests.get(data.req_id)` 是「活的、未被 vLLM 剥离字段的 `OmniRequest` 对象」；`from_base`（见 4.5）从后者用 `getattr` 取回三个 omni 字段。
2. 思考：如果不做这一步重包装，model runner 在 `execute_model()` 里还能拿到 `additional_information` 吗？

**需要观察的现象 / 预期结果**：明确「调度器重包装 = 把 omni 载荷从 `Request` 桥接到 `SchedulerOutput` 的唯一关口」。若不重包装，载荷会在 `NewRequestData` 这层丢失，下游 model runner 拿不到跨阶段数据。**待本地验证**：可在 `_rewrap_scheduled_new_reqs` 内加一行 `logger.info(f"rewrap {data.req_id}: add_info={request.additional_information is not None}")`，运行一次多阶段离线推理观察日志。

#### 4.3.5 小练习与答案

**练习 1**：AR 调度器自身有没有「决定每条请求算几个 token」的逻辑？
**参考答案**：没有。token 分配完全委托 `super().schedule()`（vLLM 原生 chunked prefill/decode）。`OmniARScheduler` 只做善后——而善后本身也已大部分委托给 `OmniSchedulerMixin`。这是它与 `OmniGenerationScheduler` 的根本区别之一。

**练习 2**：重构后，`OmniARScheduler.schedule()` 里的「重包装 for 循环」去哪了？
**参考答案**：被上提到 `OmniSchedulerMixin._rewrap_scheduled_new_reqs`，并被 `_postprocess_omni_schedule_output` 调用。AR 的 `schedule()` 只需一行 `_postprocess_omni_schedule_output(...)` 即可完成重包装，不再手写字段拷贝。这是 [1/N] 重构「消除重复管道」的典型成果。

---

### 4.4 OmniGenerationScheduler 的单步快路径

#### 4.4.1 概念说明

`OmniGenerationScheduler` 服务于**基础异构结构**（Convolution、LSTM 等）的 stage，例如 Qwen3-Omni 的 Code2wav（把音频 latent 一次性转成波形）。这类结构的共同点是：**一次前向就吃下全部输入、一次前向就产出结果**，没有「逐 token 解码」的概念。

用标准 AR 调度器跑这种 stage 会有两个尴尬：
- 标准调度器每步只分配少量 token，但这类 stage 需要一次喂入全部 token；
- 标准结束判定靠 EOS/max_tokens，但这类 stage 没有「生成 token」的概念，结束条件应是「输入全部算完」。

于是 `OmniGenerationScheduler` 提供了一条**快路径（fast path）**：

1. `schedule()` **一次性**为一个请求分配其全部输入 token（若为 0 则分配 1 个占位 token）。
2. `update_from_output()` 在「当前输入单元已全部计算完成」时**立即**判定请求 `FINISHED_STOPPED`。

它与 AR 调度器共享 `OmniSchedulerMixin` 的全部善后逻辑（`_process_pending_omni_inputs("generation")`、`_postprocess_omni_schedule_output`、`_wrap_omni_scheduler_output`、`_append_request_output`、`_capture_omni_connector_output`），但 `_attach_finished_request_sets` 传 `synthesize_abort_outputs=False`——这是两者在共享契约上的受控差异。

#### 4.4.2 核心流程

`schedule()` 快路径（核心 token 预算循环）：

```
token_budget = max_num_scheduled_tokens

# A. 先处理已在 running 中的请求（async_chunk 续段）
while running 有请求 且 token_budget > 0:
    required = len(prompt_token_ids) - num_computed_tokens
    分配 min(required, token_budget) 个 token
    若 allocate_slots 失败 → break（回退）

# B. 再从 waiting 取新请求，一次性喂入全部输入
while waiting 有请求 且 token_budget > 0 且 未暂停:
    控制并发：num_running < max_num_running_reqs
    跳过已 finished / 等首块的请求
    required_tokens = max(len(prompt_token_ids), 1)   # ★ 全量，0 则占 1
    分配 min(required_tokens, token_budget) 个 token
    若失败 → break（回退）

# C. 若快路径一个都没调度到 → 回退 super().schedule()
# D. 组装 SchedulerOutput（新请求包成 OmniNewRequestData）
# E. _postprocess_omni_schedule_output + _restore_omni_wait_queues + _wrap_omni_scheduler_output
```

token 预算的算术很简单：每纳入一个新请求，就从 `token_budget` 扣掉它一次性吃掉的量：

\[
\text{token\_budget} \leftarrow \text{token\_budget} - \min(\max(\text{len}(\text{prompt}),\;1),\;\text{token\_budget})
\]

`update_from_output()` 的结束判定（一步判完）：

```
for 每个本步调度的 req:
    若满足任一：
      - 已被标记 FINISHED_STOPPED
      - 无 chunk adapter 且 num_computed_tokens >= num_prompt_tokens
      - 有 chunk adapter 且分块已收完 且 num_computed_tokens >= len(prompt_token_ids)
    则 request.status = FINISHED_STOPPED; stopped = True
    → _handle_stopped_request → _free_request
```

#### 4.4.3 源码精读

**一次性全量分配**（快路径核心）。注意 `required_tokens = max(len(request.prompt_token_ids), 1)`——这是「全量 + 零输入占位」的来源：

[vllm_omni/core/sched/omni_generation_scheduler.py:188-191](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/omni_generation_scheduler.py#L188-L191) 与 [L204-214](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/omni_generation_scheduler.py#L204-L214) —— 从 `waiting` 取请求，`required_tokens = max(len(request.prompt_token_ids), 1)`（L190），`allocate_slots` 分配后 `pop_request()` 移入 `running`（L205-206），扣减 `token_budget`（L214）。分配失败（显存压力）时 `break` 触发回退（L197-202）。

**回退机制**：当快路径「一个 token 都没调度到」时，回退到 vLLM 原生调度（除非启用了 async_chunk，那时原生调度处理不了空 prompt，必须保留在 adapter 内部）：

[vllm_omni/core/sched/omni_generation_scheduler.py:222-231](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/omni_generation_scheduler.py#L222-L231) —— `if not num_scheduled_tokens:` 时，无 adapter 则 `super().schedule()` 再经 `_postprocess_omni_schedule_output` + `_wrap_omni_scheduler_output`（L228-231），有 adapter 则仅 `_restore_omni_wait_queues()`（L226）。

**一步结束判定**（`update_from_output`）。这是 Generation 调度器的「心脏」——三个 `or` 条件覆盖「已被外部标记 / 无分块的全量算完 / 有分块的分块收完且算完」：

[vllm_omni/core/sched/omni_generation_scheduler.py:429-441](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/omni_generation_scheduler.py#L429-L441) —— 命中即 `request.status = RequestStatus.FINISHED_STOPPED; stopped = True`（L438-441），随后进入 `_handle_stopped_request → _free_request`（L447-454）释放资源。末尾的输出归桶、统计与暂存同样走 mixin（`_append_request_output` L485、`_attach_finished_request_sets(synthesize_abort_outputs=False)` L563-566、`_capture_omni_connector_output` L576）。

**停止处理器的覆盖**：Generation 调度器覆盖了 `_handle_stopped_request`，让下游 async-chunk stage 在收到下一分段前不被错误地「真正结束」：

[vllm_omni/core/sched/omni_generation_scheduler.py:55-68](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/omni_generation_scheduler.py#L55-L68) —— 若是「跨块保留状态」的下游 stage（`chunk_transfer_adapter.receives_chunks`）且还会收下一分段，则置 `WAITING` 重新入队并返回 `False`（未真正结束），否则交给 `super()`。

#### 4.4.4 代码实践（本讲主实践）

**实践目标**：对比 `OmniARScheduler` 与 `OmniGenerationScheduler` 的 `schedule()` / `update_from_output()`，亲手写出两者在「token 分配」与「请求何时 finished」上的差异表，并指出各自复用了 `OmniSchedulerMixin` 的哪些共享 helper。这是本讲规格指定的核心实践任务。

**操作步骤**：

1. **读 token 分配**：
   - 打开 [omni_ar_scheduler.py:238](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/omni_ar_scheduler.py#L238)，确认 AR 的 `schedule()` 把 token 分配委托给 `super().schedule()`。
   - 打开 [omni_generation_scheduler.py:190](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/omni_generation_scheduler.py#L190)，确认 Generation 用 `required_tokens = max(len(prompt_token_ids), 1)` 一次性分配。
2. **读结束判定**：
   - AR：在 [omni_ar_scheduler.py:403-443](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/omni_ar_scheduler.py#L403-L443) 找到 `_update_request_with_output` + pooling 短路 + KV 触发 + `_handle_stopped_request` 的多步链路。
   - Generation：在 [omni_generation_scheduler.py:429-441](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/omni_generation_scheduler.py#L429-L441) 找到一步判完的条件。
3. **填差异表**：

| 维度 | OmniARScheduler | OmniGenerationScheduler |
| --- | --- | --- |
| **每步 token 分配** | 委托 `super().schedule()`，按 chunked prefill/decode 增量分配 | 快路径一次性分配 `max(len(prompt), 1)` 个 token |
| **谁决定算多少 token** | vLLM 原生调度算法 | 自定义快路径（全量），失败则回退 `super()` |
| **请求何时 finished** | 多步累积，命中 EOS/max_tokens/停止串 或 pooling/KV 触发后，由 `_handle_stopped_request` 判定 | 一步判完：`num_computed_tokens >= num_prompt_tokens`（或分块收完）立即 `FINISHED_STOPPED` |
| **结束判定的代码位置** | `update_from_output` 内 L403/L411/L419/L443 | `update_from_output` 内 L429-441 |
| **典型 stage** | Thinker / Talker（逐 token 自回归） | Code2wav（latent→波形，单步前向） |
| **`_handle_stopped_request` 是否覆盖** | 否（用 vLLM 原生） | 是（L55-68，处理跨块续段） |
| **`_attach_finished_request_sets` 的 `synthesize_abort_outputs`** | `True` | `False` |

4. **列各自复用的 mixin helper**（两边都调用下列方法，差异仅在参数）：

| 共享 helper（都在 `OmniSchedulerMixin`） | AR 调用点 | Generation 调用点 |
| --- | --- | --- |
| `_init_omni_io_scheduling_state` | `__init__` L107 | `__init__` L33 |
| `_process_pending_omni_inputs` | `schedule` L230（`model_mode="ar"`） | `schedule` L98（`model_mode="generation"`） |
| `_restore_omni_wait_queues` | `schedule` L245 | `schedule` L226/L229/L316 |
| `_postprocess_omni_schedule_output` | `schedule` L247（`include_cached_payloads=True`） | `schedule` L230/L314（快路径自身已组装，此处只做重包装） |
| `_wrap_omni_scheduler_output` | `schedule` L254 | `schedule` L231/L318 |
| `_append_request_output` | `update_from_output` L481 | `update_from_output` L485/L523 |
| `_attach_finished_request_sets` | `update_from_output` L538（`synthesize_abort_outputs=True`） | `update_from_output` L563（`synthesize_abort_outputs=False`） |
| `_capture_omni_connector_output` | `update_from_output` L551 | `update_from_output` L576 |

**需要观察的现象**：两者「何时结束」的语义完全不同——AR 是「生成结束」，Generation 是「输入消费完」；而两者的「调度管道」高度镜像，差异只落在几个开关参数上。

**预期结果**：能脱稿说出「AR 增量分配、多步结束；Generation 全量分配、一步结束」，以及「两者的善后管道共用 `OmniSchedulerMixin`，差异集中在 `model_mode`、`include_cached_payloads`、`synthesize_abort_outputs` 三个开关」。**待本地验证**：可参考测试 [tests/core/sched/test_omni_ar_scheduler_logprobs.py](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/tests/core/sched/test_omni_ar_scheduler_logprobs.py)、[test_omni_generation_scheduler_update_session.py](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/tests/core/sched/test_omni_generation_scheduler_update_session.py) 与重构新增的 [test_omni_scheduler_mixin_shared.py](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/tests/core/sched/test_omni_scheduler_mixin_shared.py) 验证各调度器与共享 mixin 的行为契约。

#### 4.4.5 小练习与答案

**练习 1**：`OmniGenerationScheduler.schedule()` 何时会回退到 `super().schedule()`？
**参考答案**：当快路径「一个 token 都没分配」（`not num_scheduled_tokens`）时——例如显存压力导致 `allocate_slots` 返回 `None`，或所有请求都在等首块。回退让 vLLM 原生调度兜底（但启用 async_chunk 时不回退，因原生调度无法处理空 prompt）。

**练习 2**：Generation 调度器在 `max(len(prompt_token_ids), 1)` 里为什么要有 `max(..., 1)`？
**参考答案**：某些异构结构可能用零长度的「占位 prompt」（真正的输入来自 `prompt_embeds` 或 connector 载荷）。为保证 vLLM 至少分配一个 KV 槽位、驱动一次前向，对零长度 prompt 分配 1 个占位 token。

**练习 3**：为什么 Code2wav 不能直接用 `OmniARScheduler`？
**参考答案**：Code2wav 是单步前向的卷积解码器，没有逐 token 生成的停止条件（EOS）。若用 AR 调度器，请求会因「永远不命中 EOS」而无法结束；且 AR 每步只分配少量 token，无法一次喂入全部 latent。Generation 调度器的「全量分配 + 输入算完即结束」正好匹配。

---

### 4.5 富化字段：OmniNewRequestData / OmniSchedulerOutput / OmniChunkRecvHandle

#### 4.5.1 概念说明

`OmniNewRequestData` 是两个调度器共同产出的「富化请求数据」。它继承 vLLM 的 `NewRequestData`，**保留全部基类字段**（`req_id`、`prompt_token_ids`、`mm_features`、`sampling_params`、`block_ids`、`num_computed_tokens`、`lora_request`、`prompt_embeds` 等），并新增三个 omni 专有字段，用于跨阶段数据传递：

| 新增字段 | 类型 | 用途 |
| --- | --- | --- |
| `external_req_id` | `str \| None` | 外部（用户/上游）请求 ID，用于跨 stage 追踪同一条逻辑请求 |
| `additional_information` | `AdditionalInformationPayload \| None` | 序列化的「额外信息」载荷，可含张量/列表/标量，是跨阶段元数据通道 |
| `model_intermediate_buffer` | `dict \| None` | runner 拥有的载荷，传给 `GPUModelRunner.model_intermediate_buffer` |

> 注意：`prompt_embeds`（预计算 prompt 嵌入）是**从基类 `NewRequestData` 继承**的（见 dataclass 文档字符串说明），并非 omni 新增；omni 的贡献是保证它在重包装时不丢失。`additional_information` 与 `model_intermediate_buffer` 的语义演进见 u2-l3：前者是 legacy 请求级通道，后者是新 runner 拥有载荷，两者并存逐步迁移。

[1/N] 重构还在这个文件里新增了两个东西：`OmniNewRequestData.from_base` 类方法（让 mixin 的重包装代码不再手写字段拷贝）与 `OmniChunkRecvHandle`（把「待注册的 chunk 接收」从整张 Request 收窄成两个字段，方便 IPC 序列化）。

#### 4.5.2 核心流程

`OmniNewRequestData` 现在有**三种构造路径**：

1. **`from_base` 类方法**（重构新增，AR/mixin 重包装用）：从一个原生 `NewRequestData` + 一个 `Request` 构造，保留全部基类字段，再叠加 omni 载荷。取代了旧版手写的字段逐个拷贝。
2. **`from_request` 类方法**（Generation 调度器用）：直接从一个 `Request` 对象构造，字段全部用 `getattr(request, ...)` 取，缺省为 `None`。
3. **逐字段构造**（Generation 快路径的 cached reqs）。

配套的 `OmniSchedulerOutput` 同样被富化，多带两个字段——「需要 KV 迁移的 finished 请求」与「待注册的 chunk 接收句柄」；后者用 `OmniChunkRecvHandle` 表达。

#### 4.5.3 源码精读

**`OmniNewRequestData` dataclass 与三个新字段**：

[vllm_omni/core/sched/output.py:9-29](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/output.py#L9-L29) —— 继承 `NewRequestData`，文档注释明确「prompt_embeds 继承自基类」，新增 `external_req_id` / `additional_information` / `model_intermediate_buffer`。

**`from_base` 类方法**（重构新增，mixin 重包装路径用它）：

[vllm_omni/core/sched/output.py:31-44](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/output.py#L31-L44) —— 用 `fields(NewRequestData)` 自动枚举基类字段（避免手写、避免漏字段），三个 omni 字段全部来自 `getattr(request, ..., None)`。

**`from_request` 类方法**（Generation 调度器路径用它批量构造）：

[vllm_omni/core/sched/output.py:46-78](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/output.py#L46-L78) —— 全部字段来自 `request`，`prompt_embeds`/`additional_information`/`model_intermediate_buffer` 用 `getattr` 容错。

**`OmniChunkRecvHandle`（重构新增）**：

[vllm_omni/core/sched/output.py:93-107](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/output.py#L93-L107) —— 只带 `request_id` 与 `external_req_id` 两个字段。注释说明：runner 的 `register_chunk_recv` 只消费这两个字段，所以无需把整张 `Request` 跨进程搬运；具体类型也让 msgspec 序列化在 default/PD-disagg/multi-node executor 各 IPC 路径上保持确定性。

**配套的 `OmniSchedulerOutput`**：

[vllm_omni/core/sched/output.py:110-115](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/output.py#L110-L115) —— `OmniSchedulerOutput(SchedulerOutput)` 新增 `finished_requests_needing_kv_transfer` 与 `pending_input_registrations`（`list[OmniChunkRecvHandle]`）。这两个字段由 `OmniSchedulerMixin._wrap_omni_scheduler_output`（[omni_scheduler_mixin.py:219-240](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/omni_scheduler_mixin.py#L219-L240)）统一注入，`pending_input_registrations` 直接取自 `input_coordinator.pending_input_registrations`，两个调度器都复用。

#### 4.5.4 代码实践

**实践目标**：用源码确认三个 omni 字段的「来源对象」与「消费者」，并对比 `from_base` 与 `from_request` 两条构造路径。

**操作步骤**：

1. 在 [output.py:31-44](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/output.py#L31-L44) 的 `from_base` 与 [output.py:46-78](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/core/sched/output.py#L46-L78) 的 `from_request` 里，标记三个新字段的来源都是 `getattr(request, ..., None)`。
2. 对比两者：`from_base` 是「已有原生 `NewRequestData`，补 omni 字段」（AR 重包装场景）；`from_request` 是「从零按 `Request` 构造」（Generation 新请求场景）。
3. 用 `Grep` 在 `vllm_omni/worker/` 下搜索 `additional_information` 与 `model_intermediate_buffer`，找到它们的消费者（model runner 解码载荷、传给模型 forward）。

**需要观察的现象 / 预期结果**：明确「调度器只是载荷的中转站——它不产生也不消费这些字段，只负责把它们从 `Request` 桥接到 `SchedulerOutput`」。**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：`prompt_embeds` 是 omni 新增的字段吗？
**参考答案**：不是。按 dataclass 文档注释，`prompt_embeds` 继承自基类 `NewRequestData`。omni 的贡献是在重包装时用 `getattr` 保证它不丢失，并在 model runner 里做 overlay（见 u4-l1）。

**练习 2**：`additional_information` 与 `model_intermediate_buffer` 有何区别？
**参考答案**：`additional_information` 是 legacy 的请求级元数据通道（可含张量/列表/标量，跨多边界传输）；`model_intermediate_buffer` 是较新的、runner 拥有的载荷，直接传给 `GPUModelRunner.model_intermediate_buffer`。两者并存，项目正逐步迁移（详见 u2-l3）。

**练习 3**：重构为什么新增 `OmniChunkRecvHandle`，而不是直接把 `Request` 放进 `OmniSchedulerOutput.pending_input_registrations`？
**参考答案**：runner 的 `register_chunk_recv` 只需要 `request_id` 与 `external_req_id`。直接搬 `Request` 既重（带大量字段/张量）又会让 msgspec 序列化在多 executor IPC 路径上走 `list[Any]` 兜底分支、行为不确定。收窄成两个字段的 dataclass，让序列化确定性、跨进程开销都更可控。

---

## 5. 综合实践

**任务**：为 Qwen3-Omni 的三阶段流水线，画出「请求 → stage → 调度器 → 善后管道 → 结束方式」的完整映射，并解释为什么 Code2wav 必须用快路径、而三者又共享同一套 mixin 善后管道。

**步骤**：

1. 假设一条「文本 → 音频」请求进入 vLLM-Omni，依次流经 Thinker（Stage 0）、Talker（Stage 1）、Code2wav（Stage 2）。
2. 为每个 stage 标注：
   - `execution_type`（`LLM_AR` 还是 `LLM_GENERATION`）；
   - 选中的调度器类（用 `_resolve_scheduler` 推断）；
   - `schedule()` 的 token 分配方式（增量委托 / 全量快路径）；
   - `update_from_output()` 的结束条件（EOS 多步 / 输入算完一步）；
   - 重包装时附加的载荷（`prompt_embeds` / `additional_information` / `model_intermediate_buffer`）；
   - **复用了哪些 mixin helper**（`_process_pending_omni_inputs` / `_postprocess_omni_schedule_output` / `_wrap_omni_scheduler_output` / …），差异落在哪个开关参数。
3. 回答：如果强行把 Code2wav 改成 `LLM_AR`，会发生什么？（提示：永不停下的解码 + 无法一次喂入全部 latent）。

**预期产出**：一张三行表格 + 一段解释，能把本讲四个最小模块（共享 mixin、AR 重包装、Generation 快路径、富化字段）串成一条完整链路。**待本地验证**：可对照 `examples/offline_inference/qwen3_omni/end2end.py` 与 Qwen3-Omni 的 pipeline 注册确认 stage 划分。

## 6. 本讲小结

- vLLM-Omni 按 `StageExecutionType` 分派调度器：`LLM_AR` → `OmniARScheduler`（同步）/`OmniARAsyncScheduler`（异步），`LLM_GENERATION` → `OmniGenerationScheduler`，`DIFFUSION` → 不走这套（返回 `None`）。分派逻辑在 [stage_config.py 的 `_resolve_scheduler`](https://github.com/vllm-project/vllm-omni/blob/5215e03a91adecbb5ffece29aa74360a7569d0c5/vllm_omni/config/stage_config.py#L184-L201)。
- **[1/N] 重构把两个调度器重复的「调度管道」上提到 `OmniSchedulerMixin`**：统一了 I/O 调度状态初始化（`_init_omni_io_scheduling_state`）、`schedule` 开头消费输入信号（`_process_pending_omni_inputs`）、尾部重包装与输出包装（`_postprocess_omni_schedule_output` / `_wrap_omni_scheduler_output`）、`update_from_output` 末尾的输出拼装与统计（`_append_request_output` / `_attach_scheduler_stats` / `_capture_omni_connector_output`）。
- 两者在共享 mixin 上有**显式的、受控的差异**，靠几个开关参数表达：`model_mode`（`"ar"`/`"generation"`）、`include_cached_payloads`、`_attach_finished_request_sets` 的 `synthesize_abort_outputs`（AR=`True`、Generation=`False`）。
- `OmniARScheduler` 走「最小改动」路线：`schedule()` 完全委托 `super().schedule()` 分配 token，重包装已委托给 mixin；`update_from_output()` 仍是它最重之处（多步停止判定 + KV 迁移管理）。
- `OmniGenerationScheduler` 走「单步快路径」：`schedule()` 用 `max(len(prompt), 1)` **一次性全量分配**，`update_from_output()` 在「输入全部算完」时**一步判完** `FINISHED_STOPPED`，专为 Code2wav 这类基础异构结构服务。
- 两者的根本差异在于「token 分配」与「结束判定」：AR 增量分配 + 多步停止条件（EOS/max_tokens），Generation 全量分配 + 输入消费完即结束。
- `OmniNewRequestData` 继承 `NewRequestData`，新增 `external_req_id`/`additional_information`/`model_intermediate_buffer` 三个跨阶段载荷字段；重构新增 `from_base` 类方法（让重包装不再手写字段拷贝）与 `OmniChunkRecvHandle`（收窄 chunk 注册句柄）；调度器只是这些载荷从 `Request` 到 `SchedulerOutput` 的「中转站」。
- 下游 stage 的「等上游 full-payload 载荷」由 `OmniSchedulingCoordinator`（`input_coordinator`）的 `WAITING_FOR_INPUT` 状态统一管理，并被 mixin 的超时兜底（`_process_pending_input_timeouts`）保护；async-chunk 流式分块走独立的 `chunk_transfer_adapter`。

## 7. 下一步学习建议

- **u4-l3 多模态输出处理**：本讲停在 `update_from_output()` 产出 `EngineCoreOutputs`，下一步看 `MultimodalOutputProcessor` 如何按 `output_type` 路由、`OmniRequestState` 如何跨步累积张量——这是 AR stage 把隐藏态/多模态输出交给下游 stage 的收尾环节。
- **u4-l4 异步输出实例化**：当本讲的 `sample_tokens` 关键路径需要加速时，Async Output Materialization 把 `OmniModelRunnerOutput` 的 CPU 侧构造推迟到后台线程，与本讲讨论的调度器/runner 边界紧密相关。
- **u3-l2 Orchestrator**：调度器决定的是「单 stage 内部如何调度」，而 Orchestrator 决定「stage 之间如何前推」。两者衔接处正是本讲的 `additional_information` 载荷被跨阶段搬运的起点，而 `input_coordinator` 的 `WAITING_FOR_INPUT` 正是下游 stage 等待这一搬运的体现。
- **回看 u2-l3**：若对 `additional_information` payload 的序列化（`PromptEmbedsPayload`/`AdditionalInformationEntry`）与 `upgrade_to_omni_request` 的细节还不清晰，建议复习，它是本讲重包装逻辑的上游。
- **阅读测试**：`tests/core/sched/` 下的 `test_omni_ar_scheduler_*.py`、`test_omni_generation_scheduler_*.py` 与重构新增的 `test_omni_scheduler_mixin_shared.py` 用最小 stub 锁定了各调度器与共享 mixin 的行为契约，是验证你理解是否正确的最好参照。
