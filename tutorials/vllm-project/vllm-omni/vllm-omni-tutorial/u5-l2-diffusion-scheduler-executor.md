# Diffusion 调度器与执行器

## 1. 本讲目标

本讲承接 [u5-l1](./u5-l1-diffusion-engine.md)，把 `DiffusionEngine` 这个「编排者」拆开，深入它最核心的两个内部子系统：**调度器（Scheduler）** 与 **执行器（Executor）**。

学完本讲，你应该能够：

1. 画出 `DiffusionRequestStatus` 的完整生命周期（WAITING → RUNNING → PREEMPTED / FINISHED_*），并说出每种终态的触发条件。
2. 理解 `BaseScheduler` 的共享账本结构，以及 `schedule()` / `update_from_output()` 这对调度契约如何把「谁该跑」和「跑完了没有」分开。
3. 区分两种调度策略：`RequestScheduler`（请求级批处理 + 兼容性准入，一次前向即终态）与 `StepScheduler`（逐步推进，跨多个去噪步才终态）。
4. 理解 `MultiprocDiffusionExecutor` 如何用 ZMQ/共享内存（SHM）管理多个 worker 进程，并把调度器的输出翻译成对 worker 的 RPC 调用。
5. 在一次 step_batch 推理中，跟踪一条请求从 `add_request` 到 `FINISHED_COMPLETED` 的状态迁移，并准确标注每一步由调度器还是执行器推进。

## 2. 前置知识

在进入源码前，先建立几个直觉。

**调度（Scheduling）与执行（Execution）为什么要分开？**

扩散模型（Diffusion）的一次完整推理可能要跑几十步去噪（denoise）。如果只有一张卡、却同时来了多条 prompt，就需要决定：

- **谁先跑？谁可以一起跑？** —— 这是调度器的工作。
- **真正调用 GPU 算 forward 的是谁？** —— 这是执行器（以及它身后的 worker 进程）的工作。

vLLM-Omni 把这两件事解耦：调度器只维护「内存中的请求账本」和状态机，**完全不碰 GPU**；执行器负责把调度结果通过进程间通信（IPC）发给真正的 worker 进程去算。这和 u5-l1 讲的「编排者不下场」是一致的——引擎只负责把它们串成一个循环。

**两种执行模式（回顾 u5-l1）**

| 模式 | 配置 | 调度器 | 一次执行调用做了什么 |
|------|------|--------|----------------------|
| `REQUEST_BATCH` | `step_execution=False` | `RequestScheduler` | 一次跑完整条 `pipeline.forward`（所有去噪步） |
| `STEP_BATCH` | `step_execution=True` | `StepScheduler` | 只推进一个去噪步 |

这个映射由引擎在初始化时确定（见后文 4.6），本讲要讲清两种调度器各自的状态推进逻辑。

**关键术语**

- **FIFO 队列**：先到先服务（First-In-First-Out），用 `collections.deque` 实现。
- **兼容性（compatibility）**：两条请求能否合并进同一次 `pipeline.forward(batch)`，取决于它们的形状、CFG、步数等参数是否相同。
- **采样参数键（sampling params key）**：把影响「能否批处理」的字段冻结成一个可哈希、可比较的键（`@dataclass(frozen=True, eq=True)`）。
- **wave_id / rpc_id**：执行器在 IPC 通道里给每次 RPC 调用打的编号，用来在多 worker 并发应答时把「请求」和「响应」配对。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [vllm_omni/diffusion/sched/interface.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/sched/interface.py) | 定义请求状态枚举、兼容性键、调度器输出等数据结构（调度层的「词汇表」） |
| [vllm_omni/diffusion/sched/base_scheduler.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/sched/base_scheduler.py) | `BaseScheduler`：共享账本与调度契约 |
| [vllm_omni/diffusion/sched/request_scheduler.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/sched/request_scheduler.py) | `RequestScheduler`：请求级批处理与兼容性准入 |
| [vllm_omni/diffusion/sched/step_scheduler.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/sched/step_scheduler.py) | `StepScheduler`：逐步推进与跨步进度 |
| [vllm_omni/diffusion/executor/abstract.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/executor/abstract.py) | `DiffusionExecutor`：执行器抽象基类与工厂 |
| [vllm_omni/diffusion/executor/multiproc_executor.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/executor/multiproc_executor.py) | `MultiprocDiffusionExecutor`：多进程执行器（本讲重点） |
| [vllm_omni/diffusion/diffusion_engine.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py) | `DiffusionEngine`：在 `_busy_loop` 中把调度器与执行器串成循环 |
| [vllm_omni/diffusion/worker/diffusion_worker.py](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/worker/diffusion_worker.py) | worker 进程入口与 RPC 分发（执行器的对端） |

## 4. 核心概念与源码讲解

### 4.1 请求状态机与批处理兼容性键

#### 4.1.1 概念说明

调度器的全部逻辑，本质上是「维护一组请求的状态，并在每一步决定哪些请求可以进 GPU」。要理解调度器，必须先掌握两件词汇表层面的事：

1. **一个请求有哪些状态？** —— 由 `DiffusionRequestStatus` 枚举定义。
2. **两个请求能否合并进同一个 batch？** —— 由「采样参数键」决定。

这两个数据结构都定义在 `interface.py` 里，是后续所有调度逻辑的地基。

#### 4.1.2 核心流程：请求状态生命周期

`DiffusionRequestStatus` 是一个 `IntEnum`，状态迁移如下：

```
        add_request              schedule() admit
WAITING ────────────► (WAITING) ─────────────────► RUNNING
                          │                           │
                          │ schedule() 抢占           │ update_from_output
                          ▼                           ▼
                      PREEMPTED ──► WAITING      FINISHED_COMPLETED
                      (回队首)                    FINISHED_ABORTED
                                                  FINISHED_ERROR
```

关键设计：`is_finished()` 用整数比较实现——「任何大于等于 `FINISHED_COMPLETED` 的状态都算终态」。这样新增终态只要排在 `FINISHED_COMPLETED` 之后即可，无需改判断逻辑。

#### 4.1.3 源码精读

状态枚举与终态判断：

```python
class DiffusionRequestStatus(enum.IntEnum):
    WAITING = enum.auto()
    RUNNING = enum.auto()
    PREEMPTED = enum.auto()
    # if any status is after or equal to FINISHED_COMPLETED, it is considered finished
    FINISHED_COMPLETED = enum.auto()
    FINISHED_ABORTED = enum.auto()
    FINISHED_ERROR = enum.auto()

    @staticmethod
    def is_finished(status: DiffusionRequestStatus) -> bool:
        return status >= DiffusionRequestStatus.FINISHED_COMPLETED
```

这段定义了五种状态与统一的终态判定：[vllm_omni/diffusion/sched/interface.py:14-28](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/sched/interface.py#L14-L28)。

批处理兼容性键（以 step 级为例）：只有「形状 / CFG / 输出数量 / LoRA 身份」完全相同的请求才能放进同一个去噪 batch，其余字段（seed、latent 张量等）是每请求独立的：

[vllm_omni/diffusion/sched/interface.py:31-65](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/sched/interface.py#L31-L65)。注意类装饰器 `@dataclass(frozen=True, eq=True)`：冻结使实例可哈希、可作 dict key，`eq=True` 让相同字段的两个键判等——这正是「兼容性比较」的基础。

请求级的键 `RequestBatchSamplingParamsKey` 字段更多（额外包含 `num_inference_steps`、`sigmas`、`output_type`、`strength` 等），因为它要求整条流水线的配置都对齐：[vllm_omni/diffusion/sched/interface.py:68-113](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/sched/interface.py#L68-L113)。

#### 4.1.4 代码实践

**实践目标**：亲手验证「兼容性键」如何决定两条请求能否批处理。

**操作步骤**：
1. 打开 [vllm_omni/diffusion/sched/interface.py:31-65](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/sched/interface.py#L31-L65)。
2. 在项目根目录启动 `python`，构造两个 `StepBatchSamplingParamsKey` 实例，分别只差一个字段（例如 `height`），用 `==` 比较。

**示例代码**（非项目原有代码，仅供演示）：

```python
from vllm_omni.diffusion.sched.interface import StepBatchSamplingParamsKey

a = StepBatchSamplingParamsKey(height=1024, width=1024, guidance_scale=4.0)
b = StepBatchSamplingParamsKey(height=1024, width=1024, guidance_scale=4.0)
c = StepBatchSamplingParamsKey(height=512,  width=1024, guidance_scale=4.0)

print(a == b)  # True  —— 可批处理
print(a == c)  # False —— 尺寸不同，不能批处理
print(hash(a) == hash(b))  # True —— frozen 所以可哈希
```

**预期结果**：`a == b` 为 `True`，`a == c` 为 `False`。这正解释了为什么高分辨率与低分辨率请求必须分两批去噪。

> 若本地没有 GPU/模型权重，此实践仅依赖纯 Python 数据类，可直接运行验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `DiffusionRequestStatus` 用 `IntEnum` 而非普通 `Enum`？

**参考答案**：因为终态判定用 `status >= FINISHED_COMPLETED` 做整数比较。`IntEnum` 保证成员可比较大小，从而把「是否终态」压缩成一行；普通 `Enum` 不支持 `<`/`>=`。

**练习 2**：`StepBatchSamplingParamsKey` 故意不包含 `seed`。如果把 `seed` 加进键里，会发生什么？

**参考答案**：几乎每条请求的 seed 都不同，会导致兼容性键几乎永远不相等，批处理永远退化成串行（`max_num_seqs` 形同虚设），吞吐大幅下降。所以 seed 必须是「请求级」字段，不参与批处理兼容性。

---

### 4.2 BaseScheduler：共享账本与调度契约

#### 4.2.1 概念说明

`BaseScheduler` 是两个具体调度器的共同基类。它封装了所有「与具体执行模式无关」的账本逻辑：维护等待队列、运行列表、已完成集合，以及一个调度周期里「先保 RUNNING，再按容量与兼容性准入 WAITING」的通用 `schedule()`。

它定义了一对**调度契约**：

- `schedule()` —— 产出 `DiffusionSchedulerOutput`（这批该跑谁），**不碰 GPU**。
- `update_from_output(sched_output, runner_output)` —— 拿到执行器返回的结果后，更新请求状态，返回本批刚变成终态的请求 id 集合。这一步是**抽象方法**，留给子类按执行模式实现「何时算完成」。

这种「调度产出 → 执行器执行 → 回灌结果更新状态」的三段式，正是引擎 busy loop 的骨架（见 4.6）。

#### 4.2.2 核心流程：一个调度周期

`schedule()` 的逻辑可以分成三步：

1. **保活 RUNNING**：当前所有 RUNNING 请求直接列入「缓存请求」（`scheduled_cached_reqs`），它们跨步继续跑。
2. **准入 WAITING**：只要还有空位（`len(_running) < max_num_running_reqs`）且队首请求与当前运行批「兼容」，就把它从 WAITING 升级为 RUNNING。**一旦遇到不兼容的请求就 `break`**——这是 FIFO 下的「队头阻塞」：队头不兼容，后面的请求即便兼容也只能等。
3. **KV 预取提示**：若开启了 KV 异步预取，把队尾下一个待跑请求的 `kv_sender_info` 作为 `KVPrefetchJob` 暴露给 runner，让它在本次前向时顺便预取下一条请求的 KV。

准入的兼容性判据 `_can_schedule_waiting` 可形式化为：

\[
\text{can\_schedule}(s) =
\begin{cases}
\text{True} & \text{若运行队列为空（开新批）} \\
\text{key}(s) = K_{\text{running}} & \text{否则（必须与当前批同键）}
\end{cases}
\]

其中 \(K_{\text{running}}\) 是当前运行批的采样参数键，\(s\) 是待准入请求。

#### 4.2.3 源码精读

**共享账本（构造函数）**：等待队列用 `deque`（FIFO），运行列表用 `list`，`max_num_running_reqs` 默认 1（串行）：

[vllm_omni/diffusion/sched/base_scheduler.py:40-49](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/sched/base_scheduler.py#L40-L49)

**initialize 读配置**：从 `OmniDiffusionConfig` 读 `max_num_seqs`（→ `max_num_running_reqs`）与 KV 预取开关：

[vllm_omni/diffusion/sched/base_scheduler.py:51-66](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/sched/base_scheduler.py#L51-L66)

**schedule 主循环**（本讲最核心的一段）：先调度 RUNNING，再在容量与兼容性约束下准入 WAITING，最后组装 `DiffusionSchedulerOutput`：

[vllm_omni/diffusion/sched/base_scheduler.py:80-140](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/sched/base_scheduler.py#L80-L140)

注意第 97-98 行的 `break`：这就是 FIFO 队头阻塞——遇到不兼容请求立即停止准入。

**兼容性判据**：

```python
def _can_schedule_waiting(self, state: SchedulerRequestState) -> bool:
    if not self._running:
        return True
    current_key = self._current_sampling_params_key()
    return current_key is not None and current_key == state.sampling_params_key
```

[vllm_omni/diffusion/sched/base_scheduler.py:260-265](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/sched/base_scheduler.py#L260-L265)

**调度契约（抽象）**：`update_from_output` 留给子类：

[vllm_omni/diffusion/sched/base_scheduler.py:142-144](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/sched/base_scheduler.py#L142-L144)

**共享的收尾工具**：子类算出「每个请求该变什么终态」后，统一交给 `_finalize_update_from_output` 真正改账本并返回 finished id 集合。它还会把「在 schedule 之后、update 之前被 abort 的请求」也补进返回集，保证引擎能观测到终态：

[vllm_omni/diffusion/sched/base_scheduler.py:231-245](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/sched/base_scheduler.py#L231-L245)

#### 4.2.4 代码实践

**实践目标**：理解 `_can_schedule_waiting` 的队头阻塞行为。

**操作步骤**：
1. 阅读 [vllm_omni/diffusion/sched/base_scheduler.py:80-109](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/sched/base_scheduler.py#L80-L109)。
2. 假设三条请求按到达顺序进入等待队列：A(1024×1024)、B(1024×1024)、C(512×512)，`max_num_seqs=2`。

**需要观察的现象**：第一次 `schedule()` 后谁在 RUNNING？A、B、C 各自状态如何？

**预期结果**：A 升级为 RUNNING，B 与 A 同键且有空位 → 也升级为 RUNNING；C 与运行批不同键 → 被 `break` 拦下，仍是 WAITING。运行列表 = [A, B]，等待队列 = [C]。这就是「队头阻塞」：C 必须等 A 或 B 跑完腾出空位（且届时运行批为空）才能开新批。

#### 4.2.5 小练习与答案

**练习 1**：`schedule()` 先调度 RUNNING、再准入 WAITING。如果把顺序反过来（先准入 WAITING 直到满），会有什么问题？

**参考答案**：正在跑的请求会先被「挤出」运行列表，下一周期又得重新准入，导致同一条请求反复进出运行态，状态机抖动且 KV/中间态无法跨步复用。先保 RUNNING 保证了「已经在跑的请求持续跑到结束」这一不变式。

**练习 2**：`_finalize_update_from_output` 为什么要特意检查「sched_output 中已 finished 的请求」？

**参考答案**：请求可能在 `schedule()` 之后、`update_from_output()` 之前被 `abort()` 标记为终态（如 FINISHED_ABORTED）。若不在此补登，引擎这一轮就观测不到它的终态，输出流会漏掉这条请求，造成调用方挂起。

---

### 4.3 两种调度策略：RequestScheduler 与 StepScheduler

#### 4.3.1 概念说明

`BaseScheduler` 只定义了「怎么排队、怎么准入」，但**没有定义「一条请求什么时候算完成」**——这正是两种执行模式的分水岭：

- **`RequestScheduler`（请求级）**：一次 `execute_model` 就跑完整条流水线（全部去噪步 + VAE 解码）。所以拿到执行结果后，请求**立即**进入终态（COMPLETED / ABORTED / ERROR）。它用更严格的 `RequestBatchSamplingParamsKey`，因为要把多条请求融进同一次 `pipeline.forward(batch)`。

- **`StepScheduler`（步级）**：一次 `execute_stepwise` 只推进**一个**去噪步。请求会跨很多步保持 RUNNING，直到 worker 报告 `finished=True`（通常是去噪步耗尽 + 解码完成）。它额外维护一个 `_request_progress` 字典，记录每条请求「当前步 / 总步数」。

二者的区别可概括为：**RequestScheduler 的终态由「结果类型」决定；StepScheduler 的终态由「步进度」决定**。

#### 4.3.2 核心流程

**RequestScheduler.update_from_output**（一次执行即终态）：

```
对每个被调度的请求:
    result = 该请求的执行结果
    if result is None 且有 async_output_id:  → FINISHED_COMPLETED（异步：算完等拷贝）
    elif result is None:                      → FINISHED_ERROR（无结果）
    elif result.aborted:                      → FINISHED_ABORTED
    elif result.error:                        → FINISHED_ERROR
    else:                                     → FINISHED_COMPLETED
```

**StepScheduler.update_from_output**（逐步推进）：

```
对每个被调度的请求:
    读取 req_output.step_index
    更新 progress.current_step = step_index
    更新 req.sampling_params.step_index = step_index   # 回写给请求，供下一步用
    if req_output.finished:                   → FINISHED_COMPLETED
    else:                                     → 保持 RUNNING（继续下一步）
```

注意 StepScheduler 在出错（aborted / error / 缺 step_index）时也会直接判终态，但「正常未完」时**不**进终态，而是原地等待下一轮调度。

#### 4.3.3 源码精读

**RequestScheduler**：构建请求级键 + 一次执行即判终态：

[vllm_omni/diffusion/sched/request_scheduler.py:27-71](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/sched/request_scheduler.py#L27-L71)

注意第 55-57 行：`result is None` 但带 `async_output_id` 时判 `FINISHED_COMPLETED`——这是 u5-l1 提到的异步输出（COMPUTE_DONE / OUTPUT_READY 分离）：计算已完成，最终张量稍后通过 `wait_output_ready` 到达。

**StepScheduler**：额外的 `_request_progress` 账本与逐步推进：

[vllm_omni/diffusion/sched/step_scheduler.py:30-63](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/sched/step_scheduler.py#L30-L63)（构造与 `add_request`，`add_request` 会校验 `total_steps > 0` 并初始化进度）

[vllm_omni/diffusion/sched/step_scheduler.py:65-116](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/sched/step_scheduler.py#L65-L116)（`update_from_output`：第 108-109 行把 `step_index` 同时写回进度账本和请求本身；第 110-112 行仅当 `finished` 才进终态）

总步数的计算（优先 timesteps 序列长度，其次 sigmas 长度，最后 `num_inference_steps`）：

[vllm_omni/diffusion/sched/step_scheduler.py:121-128](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/sched/step_scheduler.py#L121-L128)

#### 4.3.4 代码实践

**实践目标**：对比两种调度器在「token/步分配」与「何时 finished」上的差异。

**操作步骤**：
1. 打开 [request_scheduler.py:39-71](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/sched/request_scheduler.py#L39-L71) 与 [step_scheduler.py:65-116](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/sched/step_scheduler.py#L65-L116)。
2. 填写下面这张对照表。

**需要观察的现象 / 预期结果**（待本地验证后补全你的判断）：

| 维度 | RequestScheduler | StepScheduler |
|------|------------------|---------------|
| 每次执行调用做了多少去噪步 | 全部步（整条 pipeline.forward） | 1 步 |
| `update_from_output` 中「正常未完」时的状态 | 不存在（一定进终态） | 保持 RUNNING |
| 终态判据 | 结果类型（completed/aborted/error/async） | `req_output.finished == True` |
| 额外账本 | 无 | `_request_progress`（current/total step） |
| 跨步状态回写 | 无 | 把 `step_index` 写回 `req.sampling_params` |

#### 4.3.5 小练习与答案

**练习 1**：`StepScheduler` 为什么要 `request.sampling_params.step_index = req_output.step_index` 这一行回写？

**参考答案**：去噪是迭代过程，下一步要用到上一步推进后的时间步索引。把 `step_index` 写回请求对象，使得下一个调度周期 worker 从 `sampling_params.step_index` 就能读到正确起点，无需调度器额外传参。

**练习 2**：一个模型同时声明了 `supports_request_batch=True`，但用户设了 `streaming_output=True`。会走哪个调度器？

**参考答案**：走 `StepScheduler`。因为引擎的 `_resolve_execution_mode` 规定：`streaming_output=True` 会强制 `step_execution=True`（流式必须按步出块），而 `step_execution=True` 一律选 `StepScheduler`，`supports_request_batch` 在 step 模式下被置为 `False`。

---

### 4.4 MultiprocDiffusionExecutor：多进程执行器与 ZMQ/SHM 通信

#### 4.4.1 概念说明

调度器决定「谁跑」，但真正调用 GPU 的是**独立进程**里的 worker。`MultiprocDiffusionExecutor` 就是引擎与 worker 之间的「翻译+快递员」：

- 它**不**做推理，只负责：启动 worker 进程、把调度输出翻译成对 worker 的 RPC、收集 worker 的应答、监控进程存活、优雅关闭。
- 通信走两条共享内存消息队列（基于 ZMQ）：**broadcast_mq**（引擎 → 所有 worker，下发命令）和 **result_mq**（worker → 引擎，回传结果）。
- 它是 `DiffusionExecutor` 抽象基类的实现，通过工厂 `DiffusionExecutor.get_class(od_config)` 按 `distributed_executor_backend`（默认 `"mp"`）选中。

> 备注：vLLM-Omni 当前只实现了 `mp`（多进程）后端，`ray` / `external_launcher` 会抛 `NotImplementedError`。

#### 4.4.2 核心流程：进程拓扑与一次 RPC 往返

**进程拓扑（初始化时建立）**：

```
   引擎进程                          worker 进程们（每 GPU 一个）
 ┌───────────────┐                ┌──────────────────────┐
 │ DiffusionEngine│                │ DiffusionWorker(rank)│
 │  ├ scheduler   │  broadcast_mq  │   └ model_runner     │
 │  └ executor ──┼───────────────►│   (busy_loop 读命令) │
 │       ▲        │                │                      │
 │       │        │   result_mq    │                      │
 │       └────────┼◄───────────────│   (写回结果)         │
 └───────────────┘                └──────────────────────┘
        每个进程独立 GPU、独立 NCCL/HCCL 通信组
```

**一次 RPC 往返（以 step 模式的 `execute_step` 为例）**：

1. 引擎 busy loop 调 `executor.execute_step(sched_output)`。
2. 执行器调 `collective_rpc("execute_stepwise", args=(sched_output,), unique_reply_rank=0)`：把方法名+参数打包成 `rpc_request`，打上 `wave_id`，`broadcast_mq.enqueue(...)` 广播。
3. 每个 worker 在 `_worker_busy_loop` 里 `mq.dequeue()` 收到命令，`_execute_rpc` 调用 `worker.execute_stepwise(...)` 真正算一步。
4. 只有 rank 0（`unique_reply_rank=0`）把结果 `result_mq.enqueue(...)` 写回。
5. 执行器 `_dequeue_one_with_failure_polling` 从 `result_mq` 收应答，用 `wave_id` 校验是不是本次调用的回应（丢弃过期应答），解包后返回。

#### 4.4.3 源码精读

**抽象基类与工厂**：`get_class` 按后端字符串选中具体执行器类：

[vllm_omni/diffusion/executor/abstract.py:21-60](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/executor/abstract.py#L21-L60)

抽象契约定义了执行器必须实现的方法：`execute_request` / `execute_batch` / `execute_step` / `collective_rpc` / `check_health` / `shutdown`：

[vllm_omni/diffusion/executor/abstract.py:66-122](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/executor/abstract.py#L66-L122)

**进程初始化**：创建 broadcast/result 两条 `MessageQueue`、`wake_events`、启动 result pump 与 worker 监控：

[vllm_omni/diffusion/executor/multiproc_executor.py:72-121](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/executor/multiproc_executor.py#L72-L121)

**启动 worker 进程**：`mp.set_start_method("spawn")`，每个 rank 一个 `mp.Process(target=WorkerProc.worker_main, ...)`，通过 pipe 等待 `{"status":"ready","result_handle":...}`：

[vllm_omni/diffusion/executor/multiproc_executor.py:248-313](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/executor/multiproc_executor.py#L248-L313)

**三个 execute 方法（引擎按模式选择）**：

- `execute_request`：单请求路径，逐个新请求发 `execute_model` RPC，rank 0 回复；含一个 DP 多并发的特殊分支：

[vllm_omni/diffusion/executor/multiproc_executor.py:379-505](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/executor/multiproc_executor.py#L379-L505)

- `execute_batch`：≤1 个新请求时回退到 `execute_request`（保守串行）；否则发融合的 `execute_model_batch` RPC（要求 pipeline 支持 request-batch）：

[vllm_omni/diffusion/executor/multiproc_executor.py:507-553](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/executor/multiproc_executor.py#L507-L553)

- `execute_step`：step 模式，发 `execute_stepwise` RPC：

[vllm_omni/diffusion/executor/multiproc_executor.py:555-569](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/executor/multiproc_executor.py#L555-L569)

**collective_rpc 的两条路径**：Path 1（请求模式的 `execute_model`/`execute_model_batch`）用 `rpc_id` + Future 走异步输出分离；Path 2（step 模式或其他 RPC）用 `wave_id` 同步收应答：

[vllm_omni/diffusion/executor/multiproc_executor.py:571-678](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/executor/multiproc_executor.py#L571-L678)

**result pump（异步输出的唯一读者）**：当请求模式启用异步输出时，pump 线程独占 `result_mq`，按 `AsyncDiffusionOutput.kind` 路由——`COMPUTE_DONE`/`RPC_RESULT` 唤醒对应 RPC 的 Future，`OUTPUT_READY` 唤醒对应输出的 Future；非异步消息塞进 `_sync_result_buffer` 供 Path 2 消费：

[vllm_omni/diffusion/executor/multiproc_executor.py:691-780](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/executor/multiproc_executor.py#L691-L780)

**worker 端对端**（执行器的对端，便于理解 IPC 全貌）：`worker_main` 构造 `WorkerProc` 并跑 `_worker_busy_loop`；`_execute_rpc` 决定本 rank 是否执行、是否回复；worker 的 `execute_stepwise` 委托给 model runner：

[vllm_omni/diffusion/worker/diffusion_worker.py:1141-1208](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/worker/diffusion_worker.py#L1141-L1208)

[vllm_omni/diffusion/worker/diffusion_worker.py:940-1027](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/worker/diffusion_worker.py#L940-L1027)

[vllm_omni/diffusion/worker/diffusion_worker.py:493-503](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/worker/diffusion_worker.py#L493-L503)

#### 4.4.4 代码实践

**实践目标**：理解 broadcast/result 两条队列的「单写多读 / 多写单读」拓扑。

**操作步骤**：
1. 阅读 [multiproc_executor.py:123-133](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/executor/multiproc_executor.py#L123-L133)（两条队列如何创建）。
2. 回答：广播队列有几个 reader？结果队列有几个 reader？为什么？

**需要观察的现象 / 预期结果**：
- `broadcast_mq`：`n_reader=num_workers`（每个 worker 都要收到同一条命令）→ 「1 写 N 读」。
- `result_mq`：`n_reader=1`（只有引擎进程读结果）→ 「N 写 1 读」。
- 当启用异步输出时，`result_mq` 的那「1 个 reader」是 result pump 线程，它再把非异步消息转投 `_sync_result_buffer`——否则 pump 与 `collective_rpc` 的 Path 2 会争抢同一队列。

> 此实践为源码阅读型，无需 GPU。若想观察运行时行为，可在 `result_pump` 的 `msg = self._result_mq.dequeue(...)` 处加一行日志，但这会修改源码，仅建议在本地实验分支进行。

#### 4.4.5 小练习与答案

**练习 1**：`collective_rpc` 里 `unique_reply_rank=0` 与 `exec_all_ranks=True` 同时出现意味着什么？

**参考答案**：所有 rank 都执行该方法（`exec_all_ranks=True`，保证 TP/SP/PP/CFG 各 rank 都参与集合通信），但只有 rank 0 把结果写回 `result_mq`（`unique_reply_rank=0`）。这避免了非主 rank 重复回复导致引擎收到多余应答、与 `num_responses` 对不上。

**练习 2**：worker 监控线程（`_start_worker_monitor`）发现某 worker 进程意外死亡后做了什么？

**参考答案**：记录带 exitcode/signal 的错误日志（负 exitcode 翻译成信号名，如 -9 → SIGKILL/OOM），把 `_is_failed` 置真，调用 `shutdown()`，再逐个调用已注册的 `_failure_callbacks`（引擎借此感知到执行器已死，向上抛 `EngineDeadError`）。

---

### 4.5 引擎如何把调度器与执行器粘合：_busy_loop

#### 4.5.1 概念说明

前面四节分别讲了「调度器怎么排队」和「执行器怎么调度 worker」。本节用引擎的 `_busy_loop` 把它们缝起来，这是理解「一条请求如何一步步走到终态」的总览。

引擎在初始化时做两个关键绑定（本节不讲 AR/Generation，只看 diffusion）：

- **选执行模式与调度器**：`_resolve_execution_mode` 根据 `step_execution` / `streaming_output` / `supports_request_batch` / `max_num_seqs` 决定 `REQUEST_BATCH` 还是 `STEP_BATCH`；`_init_scheduler` 据此选 `RequestScheduler` 或 `StepScheduler`。
- **选执行函数**：`_init_execute_fn` 把 `execute_fn` 绑到 `executor.execute_step`（step 模式）或 `executor.execute_batch`（request 模式）。

#### 4.5.2 核心流程：busy loop 的三段式循环

```
while 未停止:
    处理 abort 队列 / RPC 队列
    等待条件变量（直到有待跑请求、或有 RPC、或有 abort）
    若是 request-batch 模式：等待兼容请求积累（_wait_for_request_batch_admission_locked）
    sched_output  = scheduler.schedule()          # ① 调度：决定谁跑
    runner_output = execute_fn(sched_output)       # ② 执行：交给 worker 算
    finished      = scheduler.update_from_output(  # ③ 更新：回灌结果改状态
                        sched_output, runner_output)
    emit_outputs(finished, ..., runner_output)     # 把终态/中间块投递给输出流
```

三段式的责任划分非常清晰：**调度器决定 WHO，执行器决定 HOW，update 把结果翻译回状态**。

#### 4.5.3 源码精读

**执行模式解析**（注意 `streaming_output` 会强制 `step_execution`，且不支持 request-batch 时 `max_num_seqs>1` 会直接报错）：

[vllm_omni/diffusion/diffusion_engine.py:193-211](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L193-L211)

**调度器选择**：

[vllm_omni/diffusion/diffusion_engine.py:217-228](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L217-L228)

**执行函数绑定**：

[vllm_omni/diffusion/diffusion_engine.py:271-275](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L271-L275)

**busy loop 主体**（schedule → execute → update_from_output → emit）：

[vllm_omni/diffusion/diffusion_engine.py:411-465](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L411-L465)

#### 4.5.4 代码实践

**实践目标**：把调度器、执行器、busy loop 三者在一次 step 推理中的协作画成时序。

**操作步骤**：
1. 阅读 [diffusion_engine.py:411-465](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L411-L465)。
2. 标注每一行属于「调度器职责」「执行器职责」还是「引擎粘合」。

**预期结果**（示例标注）：
- `self.scheduler.schedule()` → 调度器
- `self.execute_fn(sched_output)` → 执行器（背后是 worker）
- `self.scheduler.update_from_output(...)` → 调度器
- `self._emit_outputs(...)` → 引擎粘合

#### 4.5.5 小练习与答案

**练习**：busy loop 在调用 `execute_fn` 失败（抛异常）时，如何保证请求不会卡死在 RUNNING？

**参考答案**：except 分支用 `DiffusionOutput.from_exception(exc)` 给本批每个被调度的请求构造一个错误结果，包成 `BatchRunnerOutput`，再照常走 `update_from_output`。于是 `RequestScheduler`/`StepScheduler` 的 `update_from_output` 会把这些请求判成 `FINISHED_ERROR`，引擎随即通过 `_emit_outputs` 把错误投递给输出流，请求不会滞留。

## 5. 综合实践

**任务**：在 `STEP_BATCH` 场景下，完整跟踪一条请求从 `add_request` 到 `FINISHED_COMPLETED` 的状态迁移，并标注每一步由**调度器**还是**执行器**推进。

**背景设定**：设 `step_execution=True`，模型总去噪步 `num_inference_steps=4`，单卡，`max_num_seqs=1`。

**操作步骤**：

1. **入口**：调用方经 `DiffusionEngine.add_request(request)`（[diffusion_engine.py:689-698](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/diffusion_engine.py#L689-L698)）→ 内部调 `scheduler.add_request`（[step_scheduler.py:40-63](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/sched/step_scheduler.py#L40-L63)）。请求状态 = **WAITING**，`_request_progress = (0, 4)`。**【调度器】**

2. **第 1 个 busy loop 周期**：
   - `schedule()`（[base_scheduler.py:80-140](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/sched/base_scheduler.py#L80-L140)）：运行列表为空 → 该请求兼容准入 → 状态变 **RUNNING**，进入 `scheduled_new_reqs`。**【调度器】**
   - `execute_fn = executor.execute_step`（[multiproc_executor.py:555-569](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/executor/multiproc_executor.py#L555-L569)）→ `collective_rpc("execute_stepwise", ...)` → worker `execute_stepwise`（[diffusion_worker.py:493-503](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/worker/diffusion_worker.py#L493-L503)）算第 1 步，返回 `RunnerOutput(step_index=1, finished=False)`。**【执行器 + worker】**
   - `update_from_output`（[step_scheduler.py:65-116](https://github.com/vllm-project/vllm-omni/blob/900a7f0813d0482811b0e4dfd3cf7deabbe2429f/vllm_omni/diffusion/sched/step_scheduler.py#L65-L116)）：`finished=False` → 状态保持 **RUNNING**，`progress=(1,4)`，`step_index` 回写。**【调度器】**

3. **第 2、3 个周期**：重复「调度器保活 RUNNING → 执行器算一步 → 调度器更新进度」。`progress` 推进到 `(2,4)`、`(3,4)`，状态始终 RUNNING。

4. **第 4 个周期**：worker 算完最后一步，返回 `RunnerOutput(step_index=4, finished=True)`。`update_from_output` 命中第 110-112 行 → 状态变 **FINISHED_COMPLETED**，返回的 `finished_req_ids` 含该请求。引擎 `_emit_outputs` 把最终 `DiffusionOutput` 投递到该请求的输出流，调用方在 `get_output_stream` 收到 `output.finished=True` 后结束。**【调度器判终态 + 引擎投递】**

**最终交付物**：一张状态迁移表，形如：

| 周期 | schedule() | execute (worker step) | update_from_output | 状态 | 推进方 |
|------|-----------|----------------------|--------------------|------|--------|
| 入口 | — | — | — | WAITING | 调度器(add) |
| 1 | 升级为 RUNNING | step 1 → idx=1, fin=False | progress=(1,4) | RUNNING | 调度器→执行器→调度器 |
| 2 | 保活 RUNNING | step 2 → idx=2, fin=False | progress=(2,4) | RUNNING | 同上 |
| 3 | 保活 RUNNING | step 3 → idx=3, fin=False | progress=(3,4) | RUNNING | 同上 |
| 4 | 保活 RUNNING | step 4 → idx=4, fin=**True** | → FINISHED_COMPLETED | **FINISHED_COMPLETED** | 同上 |

**需要观察的现象**：状态在 WAITING→RUNNING 之间只迁移一次；RUNNING 持续 4 个周期；终态只在第 4 周期出现。每一步的「计算」由执行器/worker 完成，「状态变更」一律由调度器完成。

> 若本地无 GPU，本实践为纯源码跟踪型。可在 `StepScheduler.update_from_output` 入口加临时日志（仅本地实验分支）打印 `request_id / progress / finished`，对照上表验证。

## 6. 本讲小结

- **状态机**：`DiffusionRequestStatus` 用 `IntEnum` 把「是否终态」压缩成 `status >= FINISHED_COMPLETED`；`WAITING→RUNNING→(PREEMPTED)/FINISHED_*` 是核心迁移。
- **兼容性键**：`StepBatchSamplingParamsKey` / `RequestBatchSamplingParamsKey` 是冻结可哈希的 dataclass，决定哪些请求能合并进同一 batch；不参与批处理的字段（如 seed）刻意排除。
- **BaseScheduler 契约**：`schedule()`（决定谁跑，含 FIFO + 兼容性准入 + 队头阻塞）与抽象的 `update_from_output()`（决定何时算完），`_finalize_update_from_output` 是共享收尾工具。
- **两种策略**：`RequestScheduler` 一次执行即终态（按结果类型判），`StepScheduler` 跨多步推进（按 `finished` 判，并用 `_request_progress` 跟踪步进度、回写 `step_index`）。
- **多进程执行器**：`MultiprocDiffusionExecutor` 用 broadcast_mq（1 写 N 读）下发命令、result_mq（N 写 1 读）收结果；`collective_rpc` 区分异步（rpc_id+Future）与同步（wave_id）两条路径；result pump 独占异步模式下的结果队列。
- **三段式循环**：引擎 `_busy_loop` 用「schedule → execute → update_from_output → emit」把调度器（定 WHO）与执行器（定 HOW）缝合成一条不断推进请求状态的流水线。

## 7. 下一步学习建议

- **进入 worker 内部**：本讲到 `execute_stepwise` / `execute_model` 就停在了 worker 边界。下一讲 [u5-l3](./u5-l3-diffusion-worker-loader.md) 拆解 `DiffusionWorker` 的设备初始化、模型加载与消息循环，以及 `DiffusionModelRunner` 如何真正调用 pipeline.forward。
- **看完整数据流**：[u5-l4](./u5-l4-diffusion-pipeline.md) 讲 pipeline 的去噪循环（CFG 双前向、scheduler.step），把本讲的「一步」展开成张量级的细节。
- **批处理深入**：本讲提到 request-level batching 的兼容性准入，[u7-l5](./u7-l5-diffusion-batching.md) 会系统对比 request-level 与 step-wise continuous batching 的设计与吞吐取舍。
- **建议配套阅读**：`tests/diffusion` 下有针对调度器的单元测试（镜像 `vllm_omni/diffusion/sched/` 结构），可用断言佐证本讲对状态迁移的描述。
