# AR 调度器：OmniARScheduler 与 OmniGenerationScheduler

## 1. 本讲目标

上一讲（u4-l1）我们已经建立了「每个 stage 是独立 EngineCore 子进程，AR 模块沿 vLLM 的 Scheduler/Worker/ModelRunner 四层派生子类」的全局认知。本讲把镜头拉近，**专门拆解调度器这一层**：

- 理解 vLLM-Omni 如何用一个 `execution_type`（执行类型）在两种 AR 调度器之间做选择。
- 精读 `OmniARScheduler.schedule()` 如何把 vLLM 原生的 `NewRequestData` **重包装**成 `OmniNewRequestData`，并附加 `prompt_embeds` / `additional_information` / `model_intermediate_buffer` 三类载荷。
- 精读 `OmniGenerationScheduler` 的**单步一次性快路径**：一次 `schedule()` 分配全部输入 token，一次 `update_from_output()` 立即判定请求完成。
- 掌握 AR 模块请求流：`InputProcessor → Scheduler → Worker → ModelRunner → OutputProcessor`。
- 通过对比实践，写出两种调度器在「token 分配」与「请求何时 finished」上的差异表。

学完后，你应能回答：**为什么 Thinker/Talker（标准自回归）用 `OmniARScheduler`，而 Code2wav（卷积/LSTM 等基础异构结构）用 `OmniGenerationScheduler`？** 答案就藏在这两个调度器对 token 的分配与结束判定方式里。

## 2. 前置知识

阅读本讲前，请确保理解以下概念（若不熟悉，可先回顾 u3-l3、u4-l1）：

- **stage（阶段）**：vLLM-Omni 把一个全模态请求拆成的顺序子任务，每个 stage 是一个独立的 EngineCore 子进程（如 Qwen3-Omni 的 Thinker → Talker → Code2wav 三阶段）。
- **调度器（Scheduler）**：vLLM v1 的核心组件，负责决定「这一步调度哪些请求、每条请求算多少 token」，并产出 `SchedulerOutput` 交给 Worker 执行。vLLM v1 的基类是 `vllm.v1.core.sched.scheduler.Scheduler`（下文记作 `VLLMScheduler`）。
- **`NewRequestData` / `SchedulerOutput`**：vLLM 描述「本步新纳入请求」与「本步调度结果」的数据结构。本讲会频繁看到前者被 omni「重包装」。
- **monkey-patch（猴子补丁）**：vLLM-Omni 在不修改 vLLM 源码的前提下，运行时替换其方法/属性（见 u2-l1）。
- **两阶段 execute/sample**：AR ModelRunner 的 `execute_model()` 只跑前向返回 `None`，再由 `sample_tokens()` 采样（见 u4-l1）。
- **chunked prefill / decode**：vLLM 把长 prompt 分块预填（prefill）、生成阶段逐 token 解码（decode）的机制。

一个关键直觉：vLLM 的调度器是为「逐 token 生成」设计的——它每步只给每条请求分配少量 token，请求要经历很多步才结束。但 omni 的某些 stage（如 Code2wav 把音频 latent 一次性转成波形）是**单步前向就完成**的「基础异构结构」。用标准调度器跑这种 stage 会很别扭，于是 omni 专门写了 `OmniGenerationScheduler` 提供一条「一次喂入全部 token、一步完成」的快路径。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [vllm_omni/config/stage_config.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/stage_config.py) | 用 `_resolve_scheduler(execution_type, async_scheduling)` 决定一个 stage 用哪个调度器类；定义 `StageExecutionType` 枚举。 |
| [vllm_omni/core/sched/omni_ar_scheduler.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/core/sched/omni_ar_scheduler.py) | `OmniARScheduler`（标准 AR）与 `OmniARAsyncScheduler`（异步变体），核心是 `schedule()` 的重包装逻辑与 KV 迁移管理。 |
| [vllm_omni/core/sched/omni_generation_scheduler.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/core/sched/omni_generation_scheduler.py) | `OmniGenerationScheduler`：单步一次性快路径调度器，`update_from_output()` 一步判定完成。 |
| [vllm_omni/core/sched/output.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/core/sched/output.py) | `OmniNewRequestData` / `OmniCachedRequestData` / `OmniSchedulerOutput`：被富化的调度输出数据结构。 |
| [vllm_omni/core/sched/omni_scheduler_mixin.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/core/sched/omni_scheduler_mixin.py) | `OmniSchedulerMixin`：两个调度器共享的工具方法（包裹输出、消费 connector 信号、状态对齐等）。 |
| [vllm_omni/core/sched/omni_scheduling_coordinator.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/core/sched/omni_scheduling_coordinator.py) | `OmniSchedulingCoordinator`：管理下游 stage「等上游载荷到达」的 `WAITING_FOR_INPUT` 状态，被两个调度器复用。 |

> 提示：本讲引用的永久链接基线为 commit `900a7f08`，行号以此为准。

## 4. 核心概念与源码讲解

### 4.1 调度器选型与请求流总览

#### 4.1.1 概念说明

vLLM-Omni 不只有一个 AR 调度器，而是**按 stage 的执行类型（execution_type）分派**。这承接 u2-l2 的「配置体系」与 u3-l3 的「stage 进程」：每个 stage 在构建 `StageConfig` 时就决定了它属于哪种执行类型，进而决定用哪个调度器、哪个 worker、哪个 model runner。

执行类型由枚举 `StageExecutionType` 给出，只有三种：

[vllm_omni/config/stage_config.py:176-181](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/stage_config.py#L176-L181) —— 定义三个执行类型：`LLM_AR`（标准自回归）、`LLM_GENERATION`（单步生成的基础异构结构）、`DIFFUSION`（扩散，本讲不涉及）。

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

见 [vllm_omni/config/stage_config.py:184-201](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/stage_config.py#L184-L201)。

注意三个要点：

1. **`LLM_AR` 有同步/异步两个调度器**：`OmniARScheduler`（同步）与 `OmniARAsyncScheduler`（异步，后者继承前者并再继承 vLLM 的 `AsyncScheduler`）。选哪个由 `async_scheduling` 标志决定。在 stage 配置装配阶段，代码还会根据选中的类**反向回写** `engine_args["async_scheduling"]`，保证一致性（见 [vllm_omni/config/stage_config.py:943-944](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/stage_config.py#L943-L944)）。
2. **`LLM_GENERATION` 永远是 `OmniGenerationScheduler`**，不分同步异步。
3. **`DIFFUSION` 返回 `None`**——扩散 stage 不走 vLLM 调度器，它有自己的 `DiffusionEngine` 与 diffusion scheduler（见 U5）。

`StageExecutionType` 同时也映射出 `worker_type`（`ar` / `generation`），即调度器、worker、model runner 三者由同一个 `execution_type` 串起来：

[vllm_omni/config/stage_config.py:770-774](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/stage_config.py#L770-L774) —— `LLM_AR → ("llm", "ar")`、`LLM_GENERATION → ("llm", "generation")`、`DIFFUSION → ("diffusion", None)`。

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

#### 4.1.3 源码精读

`OmniARAsyncScheduler` 的定义极简——它只是把 `OmniARScheduler` 与 vLLM 的 `AsyncScheduler` 多继承拼到一起，所有 AR 调度逻辑都集中在 `OmniARScheduler`：

[vllm_omni/core/sched/omni_ar_scheduler.py:1022-1024](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/core/sched/omni_ar_scheduler.py#L1022-L1024) —— `class OmniARAsyncScheduler(OmniARScheduler, AsyncVLLMScheduler)`，异步变体复用同步版的全部逻辑。

两个调度器都先继承 `OmniSchedulerMixin` 再继承 `VLLMScheduler`，从而获得共享工具：

[vllm_omni/core/sched/omni_ar_scheduler.py:85-89](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/core/sched/omni_ar_scheduler.py#L85-L89) 与 [vllm_omni/core/sched/omni_generation_scheduler.py:43](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/core/sched/omni_generation_scheduler.py#L43) —— 两者类头都是 `class XxxScheduler(OmniSchedulerMixin, VLLMScheduler)`。

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

### 4.2 OmniARScheduler.schedule 的重包装逻辑

#### 4.2.1 概念说明

`OmniARScheduler` 服务于**标准自回归 stage**（Thinker、Talker 这类逐 token 生成）。它的设计哲学是「**最小改动**」——**不重写 vLLM 的调度算法**（分块 prefill、decode、连续批处理、抢占等全部沿用），只在两件事上做增量：

1. **`schedule()` 的尾部重包装**：把 vLLM 产出的原生 `NewRequestData` 重新包成 `OmniNewRequestData`，附加上 omni 的三类载荷，让下游 model runner 能拿到跨阶段数据。
2. **`update_from_output()` 的多步生命周期 + KV 迁移管理**：在标准停止判定之外，叠加「KV cache 迁移到下一 stage」的触发与等待逻辑。

为什么要重包装？因为 vLLM 的 `NewRequestData` 不认识 omni 字段，`additional_information` 这类载荷在 vLLM 内部会被丢弃。调度器是「vLLM 边界」的最后一站，必须在这里把载荷从「活的 `Request` 对象」捡回来，挂到调度输出上，才能跨越进程边界传到 worker/model runner。

#### 4.2.2 核心流程

`OmniARScheduler.schedule()` 的执行过程：

```
schedule(throttle_prefills):
  1. 先清理 waiting/running 中 FINISHED_ABORTED 的请求（omni 允许异步 abort 暂留）
  2. _consume_pending_connector_output("ar")        # 消费 connector 信号、处理 input 等待
  3. _process_pending_input_timeouts()              # 超时强制失败
  4. chunk_transfer_adapter.process_pending_chunks()# async_chunk 流式分块
  5. scheduler_output = super().schedule()          # ★ 交给 vLLM 原生调度
  6. restore 阶段：把 chunk-waiting / input-waiting 请求放回队列
  7. ★ 重包装：for nr in scheduled_new_reqs:
        用「活的 Request」的 prompt_embeds / additional_information /
           model_intermediate_buffer 重建 OmniNewRequestData
  8. 收集「需要 KV 迁移的 finished 请求」
  9. return _wrap_omni_scheduler_output(scheduler_output, ...)
```

注意第 5 步：**AR 调度器自己不决定 token 分配**，完全委托 `super().schedule()`。它只负责「包裹载荷」与「善后」。

`update_from_output()` 则负责多步推进：每步调用 `_update_request_with_output()` 应用新生成的 token、检查停止条件（EOS/max_tokens/停止串），命中后由 `_handle_stopped_request()` 判定是否真正结束。

#### 4.2.3 源码精读

**重包装的核心代码**（`schedule()` 尾部）。注意它从 `self.requests`（活的 `Request` 对象）里用 `getattr` 把 omni 字段捡回来——这正是 u2-l1/u2-l3 反复强调的「载荷在多边界被丢弃、需反复捡回」的体现：

[vllm_omni/core/sched/omni_ar_scheduler.py:284-315](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/core/sched/omni_ar_scheduler.py#L284-L315) —— 遍历 `scheduled_new_reqs`，逐个重建 `OmniNewRequestData`，保留所有基类字段（`prompt_token_ids`、`mm_features`、`sampling_params`、`block_ids` 等），并用 `getattr(request, "prompt_embeds" / "additional_information" / "model_intermediate_buffer", None)` 补齐 omni 载荷。重包装失败时用 `try/except` 兜底，保留原始输出不变（[L320-323](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/core/sched/omni_ar_scheduler.py#L320-L323)）。

**停止判定与结束**（`update_from_output()` 内）。AR 的「finished」由三股力量共同决定：

- 标准停止条件：[L475](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/core/sched/omni_ar_scheduler.py#L475) `_update_request_with_output()` 应用新 token 并返回 `stopped`（含 EOS/max_tokens/停止串）。
- Pooling 短路：[L483-486](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/core/sched/omni_ar_scheduler.py#L483-L486) 若有 pooling 参数且有输出，立即 `FINISHED_STOPPED`。
- KV 迁移触发停止：[L491](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/core/sched/omni_ar_scheduler.py#L491) `_process_kv_transfer_trigger()` 返回 True 表示「必须停止以触发 KV 迁移」。

命中 `stopped` 后，是否真正结束由 `_handle_stopped_request()` 决定（[L515](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/core/sched/omni_ar_scheduler.py#L515)）。它返回 `finished=True` 才真正调用 `_free_request()` 释放资源（[L535-536](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/core/sched/omni_ar_scheduler.py#L535-L536)）；流式输入场景下它可能返回 `False`，让请求继续等待下一个分段。

**`__init__` 的 KV 迁移账本**：`OmniARScheduler` 在构造时初始化了大量 KV 迁移相关状态，这是它相对 Generation 调度器更「重」的地方：

[vllm_omni/core/sched/omni_ar_scheduler.py:91-129](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/core/sched/omni_ar_scheduler.py#L91-L129) —— 初始化 `requests_needing_kv_transfer`、`waiting_for_transfer_free`、`active_kv_transfers`、`pending_stop_after_extraction` 等账本，并按需创建 `chunk_transfer_adapter` 与 `input_coordinator`。

#### 4.2.4 代码实践

**实践目标**：跟踪一次重包装，看清「哪些载荷从哪个对象被捡回」。

**操作步骤**：

1. 在 [vllm_omni/core/sched/omni_ar_scheduler.py:290](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/core/sched/omni_ar_scheduler.py#L290) 的 `for nr in scheduler_output.scheduled_new_reqs:` 循环内，记录三件事：
   - `nr`（原生 `NewRequestData`）从哪儿来？→ `super().schedule()` 产出。
   - `request = self.requests.get(req_id)` 的 `request` 是什么？→ 活的、未被 vLLM 剥离字段的 `OmniRequest` 对象。
   - 三个 `getattr(request, ...)` 分别取了什么？→ `prompt_embeds`、`additional_information`、`model_intermediate_buffer`。
2. 思考：如果不做这一步重包装，model runner 在 `execute_model()` 里还能拿到 `additional_information` 吗？

**需要观察的现象 / 预期结果**：明确「调度器重包装 = 把 omni 载荷从 `Request` 桥接到 `SchedulerOutput` 的唯一关口」。若不重包装，载荷会在 `NewRequestData` 这层丢失，下游 model runner 拿不到跨阶段数据。**待本地验证**：可在该行加一行 `logger.info(f"rewrap {req_id}: add_info={request.additional_information is not None}")`，运行一次多阶段离线推理观察日志。

#### 4.2.5 小练习与答案

**练习 1**：`OmniARScheduler.schedule()` 为什么要把重包装放进 `try/except`，且失败时「保留原始输出不变」？
**参考答案**：重包装是 omni 的增量逻辑，属于「锦上添花」。若它因字段缺失等异常失败，不应让整个 stage 崩溃；退回到原生 `NewRequestData` 至少能保证文本推理继续，符合「最小改动、最大兼容」哲学。

**练习 2**：AR 调度器自身有没有「决定每条请求算几个 token」的逻辑？
**参考答案**：没有。token 分配完全委托 `super().schedule()`（vLLM 原生 chunked prefill/decode）。`OmniARScheduler` 只做重包装与善后，这是它与 `OmniGenerationScheduler` 的根本区别之一。

---

### 4.3 OmniGenerationScheduler 的单步快路径

#### 4.3.1 概念说明

`OmniGenerationScheduler` 服务于**基础异构结构**（Convolution、LSTM 等）的 stage，例如 Qwen3-Omni 的 Code2wav（把音频 latent 一次性转成波形）。这类结构的共同点是：**一次前向就吃下全部输入、一次前向就产出结果**，没有「逐 token 解码」的概念。

用标准 AR 调度器跑这种 stage 会有两个尴尬：
- 标准调度器每步只分配少量 token，但这类 stage 需要一次喂入全部 token；
- 标准结束判定靠 EOS/max_tokens，但这类 stage 没有「生成 token」的概念，结束条件应是「输入全部算完」。

于是 `OmniGenerationScheduler` 提供了一条**快路径（fast path）**：

1. `schedule()` **一次性**为一个请求分配其全部输入 token（若为 0 则分配 1 个占位 token）。
2. `update_from_output()` 在「当前输入单元已全部计算完成」时**立即**判定请求 `FINISHED_STOPPED`。

#### 4.3.2 核心流程

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
# D. 组装 SchedulerOutput（新请求也包成 OmniNewRequestData）
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

#### 4.3.3 源码精读

**一次性全量分配**（快路径核心）。注意 `required_tokens = max(len(request.prompt_token_ids), 1)`——这是「全量 + 零输入占位」的来源：

[vllm_omni/core/sched/omni_generation_scheduler.py:215-242](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/core/sched/omni_generation_scheduler.py#L215-L242) —— 从 `waiting` 取请求，`required_tokens = max(len(request.prompt_token_ids), 1)`，`allocate_slots` 分配后 `pop_request()` 移入 `running`，扣减 `token_budget`。分配失败（显存压力）时 `break` 触发回退。

**回退机制**：当快路径「一个 token 都没调度到」时，回退到 vLLM 原生调度（除非启用了 async_chunk，那时原生调度处理不了空 prompt，必须保留在 adapter 内部）：

[vllm_omni/core/sched/omni_generation_scheduler.py:249-262](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/core/sched/omni_generation_scheduler.py#L249-L262) —— `if not num_scheduled_tokens:` 时，无 adapter 则 `super().schedule()`，有 adapter 则仅 `restore_queues`。

**一步结束判定**（`update_from_output`）。这是 Generation 调度器的「心脏」——三个 `or` 条件覆盖「已被外部标记 / 无分块的全量算完 / 有分块的分块收完且算完」：

[vllm_omni/core/sched/omni_generation_scheduler.py:540-554](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/core/sched/omni_generation_scheduler.py#L540-L554) —— 命中即 `request.status = RequestStatus.FINISHED_STOPPED; stopped = True`，随后进入 `_handle_stopped_request → _free_request` 释放资源。

**停止处理器的覆盖**：Generation 调度器还覆盖了 `_handle_stopped_request`，让下游 async-chunk stage 在收到下一分段前不被错误地「真正结束」：

[vllm_omni/core/sched/omni_generation_scheduler.py:77-90](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/core/sched/omni_generation_scheduler.py#L77-L90) —— 若是「跨块保留状态」的下游 stage 且还会收下一分段，则置 `WAITING` 重新入队并返回 `False`（未真正结束），否则交给 `super()`。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：对比 `OmniARScheduler` 与 `OmniGenerationScheduler` 的 `schedule()` / `update_from_output()`，亲手写出两者在「token 分配」与「请求何时 finished」上的差异表。这是本讲规格指定的核心实践任务。

**操作步骤**：

1. **读 token 分配**：
   - 打开 [omni_ar_scheduler.py:246-329](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/core/sched/omni_ar_scheduler.py#L246-L329)，确认 AR 的 `schedule()` 把 token 分配委托给 `super().schedule()`（搜索 `super().schedule(throttle_prefills)`）。
   - 打开 [omni_generation_scheduler.py:184-242](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/core/sched/omni_generation_scheduler.py#L184-L242)，确认 Generation 用 `required_tokens = max(len(prompt_token_ids), 1)` 一次性分配。
2. **读结束判定**：
   - AR：在 [omni_ar_scheduler.py:475-516](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/core/sched/omni_ar_scheduler.py#L475-L516) 找到 `_update_request_with_output` + `_handle_stopped_request` 的多步链路。
   - Generation：在 [omni_generation_scheduler.py:540-554](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/core/sched/omni_generation_scheduler.py#L540-L554) 找到一步判完的条件。
3. **填差异表**：

| 维度 | OmniARScheduler | OmniGenerationScheduler |
| --- | --- | --- |
| **每步 token 分配** | 委托 `super().schedule()`，按 chunked prefill/decode 增量分配 | 快路径一次性分配 `max(len(prompt), 1)` 个 token |
| **谁决定算多少 token** | vLLM 原生调度算法 | 自定义快路径（全量），失败则回退 `super()` |
| **请求何时 finished** | 多步累积，命中 EOS/max_tokens/停止串 或 pooling/KV 触发后，由 `_handle_stopped_request` 判定 | 一步判完：`num_computed_tokens >= num_prompt_tokens`（或分块收完）立即 `FINISHED_STOPPED` |
| **结束判定的代码位置** | `update_from_output` 内 L475/L483/L491/L515 | `update_from_output` 内 L540-554 |
| **典型 stage** | Thinker / Talker（逐 token 自回归） | Code2wav（latent→波形，单步前向） |
| **`_handle_stopped_request` 是否覆盖** | 否（用 vLLM 原生） | 是（L77-90，处理跨块续段） |

**需要观察的现象**：两者「何时结束」的语义完全不同——AR 是「生成结束」，Generation 是「输入消费完」。

**预期结果**：能脱稿说出「AR 增量分配、多步结束；Generation 全量分配、一步结束」。**待本地验证**：可参考测试 [tests/core/sched/test_omni_generation_scheduler_update_session.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/tests/core/sched/test_omni_generation_scheduler_update_session.py) 与 [tests/core/sched/test_omni_ar_scheduler_logprobs.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/tests/core/sched/test_omni_ar_scheduler_logprobs.py) 验证各调度器的行为契约。

#### 4.3.5 小练习与答案

**练习 1**：`OmniGenerationScheduler.schedule()` 何时会回退到 `super().schedule()`？
**参考答案**：当快路径「一个 token 都没分配」（`not num_scheduled_tokens`）时——例如显存压力导致 `allocate_slots` 返回 `None`，或所有请求都在等首块。回退让 vLLM 原生调度兜底（但启用 async_chunk 时不回退，因原生调度无法处理空 prompt）。

**练习 2**：Generation 调度器在 `max(len(prompt_token_ids), 1)` 里为什么要有 `max(..., 1)`？
**参考答案**：某些异构结构可能用零长度的「占位 prompt」（真正的输入来自 `prompt_embeds` 或 connector 载荷）。为保证 vLLM 至少分配一个 KV 槽位、驱动一次前向，对零长度 prompt 分配 1 个占位 token。

**练习 3**：为什么 Code2wav 不能直接用 `OmniARScheduler`？
**参考答案**：Code2wav 是单步前向的卷积解码器，没有逐 token 生成的停止条件（EOS）。若用 AR 调度器，请求会因「永远不命中 EOS」而无法结束；且 AR 每步只分配少量 token，无法一次喂入全部 latent。Generation 调度器的「全量分配 + 输入算完即结束」正好匹配。

---

### 4.4 OmniNewRequestData 富化字段

#### 4.4.1 概念说明

`OmniNewRequestData` 是两个调度器共同产出的「富化请求数据」。它继承 vLLM 的 `NewRequestData`，**保留全部基类字段**（`req_id`、`prompt_token_ids`、`mm_features`、`sampling_params`、`block_ids`、`num_computed_tokens`、`lora_request`、`prompt_embeds` 等），并新增三个 omni 专有字段，用于跨阶段数据传递：

| 新增字段 | 类型 | 用途 |
| --- | --- | --- |
| `external_req_id` | `str \| None` | 外部（用户/上游）请求 ID，用于跨 stage 追踪同一条逻辑请求 |
| `additional_information` | `AdditionalInformationPayload \| None` | 序列化的「额外信息」载荷，可含张量/列表/标量，是跨阶段元数据通道 |
| `model_intermediate_buffer` | `dict \| None` | runner 拥有的载荷，传给 `GPUModelRunner.model_intermediate_buffer` |

> 注意：`prompt_embeds`（预计算 prompt 嵌入）是**从基类 `NewRequestData` 继承**的（见 dataclass 文档字符串说明），并非 omni 新增；omni 的贡献是保证它在重包装时不丢失。`additional_information` 与 `model_intermediate_buffer` 的语义演进见 u2-l3：前者是 legacy 请求级通道，后者是新 runner 拥有载荷，两者并存逐步迁移。

#### 4.4.2 核心流程

`OmniNewRequestData` 的两种构造路径：

1. **`from_request` 类方法**（Generation 调度器用）：直接从一个 `Request` 对象构造，字段全部用 `getattr(request, ...)` 取，缺省为 `None`。
2. **逐字段重建**（AR 调度器用）：先有原生 `NewRequestData`（`nr`），保留其基类字段，再叠加 omni 载荷。

两种路径都依赖「活的 `Request` 对象」携带 omni 字段——这又回到 u2-l3 的链路：`OmniRequest` 在 stage 端把 `additional_information` payload 解码回张量。

#### 4.4.3 源码精读

**`OmniNewRequestData` dataclass 与三个新字段**：

[vllm_omni/core/sched/output.py:9-29](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/core/sched/output.py#L9-L29) —— 继承 `NewRequestData`，文档注释明确「prompt_embeds 继承自基类」，新增 `external_req_id` / `additional_information` / `model_intermediate_buffer`。

**`from_request` 类方法**（Generation 调度器路径用它批量构造）：

[vllm_omni/core/sched/output.py:31-63](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/core/sched/output.py#L31-L63) —— 全部字段来自 `request`，`prompt_embeds`/`additional_information`/`model_intermediate_buffer` 用 `getattr` 容错。

**配套的 `OmniSchedulerOutput`**：调度输出本身也被富化，多带两个字段——「需要 KV 迁移的 finished 请求」与「待注册的 chunk 接收句柄」：

[vllm_omni/core/sched/output.py:95-100](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/core/sched/output.py#L95-L100) —— `OmniSchedulerOutput(SchedulerOutput)` 新增 `finished_requests_needing_kv_transfer` 与 `pending_input_registrations`。这两个字段由 `OmniSchedulerMixin._wrap_omni_scheduler_output`（[omni_scheduler_mixin.py:161-182](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/core/sched/omni_scheduler_mixin.py#L161-L182)）统一注入，两个调度器都复用。

#### 4.4.4 代码实践

**实践目标**：用源码确认三个 omni 字段的「来源对象」与「消费者」。

**操作步骤**：

1. 在 [output.py:31-63](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/core/sched/output.py#L31-L63) 的 `from_request` 里，标记三个新字段的来源都是 `getattr(request, ..., None)`。
2. 用 `Grep` 在 `vllm_omni/worker/` 下搜索 `additional_information` 与 `model_intermediate_buffer`，找到它们的消费者（model runner 解码载荷、传给模型 forward）。

**需要观察的现象 / 预期结果**：明确「调度器只是载荷的中转站——它不产生也不消费这些字段，只负责把它们从 `Request` 桥接到 `SchedulerOutput`」。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`prompt_embeds` 是 omni 新增的字段吗？
**参考答案**：不是。按 dataclass 文档注释，`prompt_embeds` 继承自基类 `NewRequestData`。omni 的贡献是在重包装时用 `getattr` 保证它不丢失，并在 model runner 里做 overlay（见 u4-l1）。

**练习 2**：`additional_information` 与 `model_intermediate_buffer` 有何区别？
**参考答案**：`additional_information` 是 legacy 的请求级元数据通道（可含张量/列表/标量，跨多边界传输）；`model_intermediate_buffer` 是较新的、runner 拥有的载荷，直接传给 `GPUModelRunner.model_intermediate_buffer`。两者并存，项目正逐步迁移（详见 u2-l3）。

---

## 5. 综合实践

**任务**：为 Qwen3-Omni 的三阶段流水线，画出「请求 → stage → 调度器 → 结束方式」的完整映射，并解释为什么 Code2wav 必须用快路径。

**步骤**：

1. 假设一条「文本 → 音频」请求进入 vLLM-Omni，依次流经 Thinker（Stage 0）、Talker（Stage 1）、Code2wav（Stage 2）。
2. 为每个 stage 标注：
   - `execution_type`（`LLM_AR` 还是 `LLM_GENERATION`）；
   - 选中的调度器类（用 `_resolve_scheduler` 推断）；
   - `schedule()` 的 token 分配方式（增量委托 / 全量快路径）；
   - `update_from_output()` 的结束条件（EOS 多步 / 输入算完一步）；
   - 重包装时附加的载荷（`prompt_embeds` / `additional_information` / `model_intermediate_buffer`）。
3. 回答：如果强行把 Code2wav 改成 `LLM_AR`，会发生什么？（提示：永不停下的解码 + 无法一次喂入全部 latent）。

**预期产出**：一张三行表格 + 一段解释，能把本讲三个最小模块（重包装、快路径、富化字段）串成一条完整链路。**待本地验证**：可对照 `examples/offline_inference/qwen3_omni/end2end.py` 与 Qwen3-Omni 的 pipeline 注册确认 stage 划分。

## 6. 本讲小结

- vLLM-Omni 按 `StageExecutionType` 分派调度器：`LLM_AR` → `OmniARScheduler`（同步）/`OmniARAsyncScheduler`（异步），`LLM_GENERATION` → `OmniGenerationScheduler`，`DIFFUSION` → 不走这套（返回 `None`）。分派逻辑在 [stage_config.py 的 `_resolve_scheduler`](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/config/stage_config.py#L184-L201)。
- `OmniARScheduler` 走「最小改动」路线：`schedule()` 完全委托 `super().schedule()` 分配 token，只在尾部把原生 `NewRequestData` **重包装**成 `OmniNewRequestData`，附加 `prompt_embeds`/`additional_information`/`model_intermediate_buffer`，并在 `update_from_output()` 叠加 KV 迁移管理。
- `OmniGenerationScheduler` 走「单步快路径」：`schedule()` 用 `max(len(prompt), 1)` **一次性全量分配** token，`update_from_output()` 在「输入全部算完」时**一步判完** `FINISHED_STOPPED`，专为 Code2wav 这类基础异构结构服务。
- 两者的根本差异在于「token 分配」与「结束判定」：AR 增量分配 + 多步停止条件（EOS/max_tokens），Generation 全量分配 + 输入消费完即结束。
- `OmniNewRequestData` 继承 `NewRequestData`，新增 `external_req_id`/`additional_information`/`model_intermediate_buffer` 三个跨阶段载荷字段；调度器只是这些载荷从 `Request` 到 `SchedulerOutput` 的「中转站」。
- 两个调度器都继承 `OmniSchedulerMixin`，复用 `_wrap_omni_scheduler_output`、`_consume_pending_connector_output`、状态对齐等共享逻辑；下游 stage 的「等上游载荷」由 `OmniSchedulingCoordinator` 的 `WAITING_FOR_INPUT` 状态统一管理。

## 7. 下一步学习建议

- **u4-l3 多模态输出处理**：本讲停在 `update_from_output()` 产出 `EngineCoreOutputs`，下一步看 `MultimodalOutputProcessor` 如何按 `output_type` 路由、`OmniRequestState` 如何跨步累积张量——这是 AR stage 把隐藏态/多模态输出交给下游 stage 的收尾环节。
- **u3-l2 Orchestrator**：调度器决定的是「单 stage 内部如何调度」，而 Orchestrator 决定「stage 之间如何前推」。两者衔接处正是本讲的 `additional_information` 载荷被跨阶段搬运的起点。
- **回看 u2-l3**：若对 `additional_information` payload 的序列化（`PromptEmbedsPayload`/`AdditionalInformationEntry`）与 `upgrade_to_omni_request` 的细节还不清晰，建议复习，它是本讲重包装逻辑的上游。
- **阅读测试**：`tests/core/sched/` 下的 `test_omni_ar_scheduler_*.py` 与 `test_omni_generation_scheduler_*.py` 用最小 stub 锁定了各调度器的行为契约，是验证你理解是否正确的最好参照。
