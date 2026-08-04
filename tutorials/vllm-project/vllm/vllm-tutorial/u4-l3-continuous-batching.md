# 连续批处理与请求队列

## 1. 本讲目标

本讲承接 u4-l2「调度器核心」，把目光从「调度器每步如何决策」收窄到**两个决定吞吐的关键机制**和**承载它们的队列结构**上。读完本讲，你应该能够：

- 说清**连续批处理（continuous batching）**相对静态批处理的吞吐优势，以及它在 V1 里是如何被「统一 token 预算」自然实现的；
- 说清**分块预填充（chunked prefill）**如何把一条超长 prefill 切成多步、并与正在 decode 的请求塞进同一个 step；
- 读懂 `request_queue.py` 中 `waiting` / `skipped_waiting` / `running` 三态队列的组织方式，以及 FCFS 与 PRIORITY 两种策略的差异；
- 用一个具体例子追踪「长 prefill + 多条短 decode 合并进同一 step」的全过程，并解释其吞吐收益。

本讲不碰 GPU、不碰模型权重，全部是纯 CPU 上的调度决策逻辑。

## 2. 前置知识

### 2.1 prefill 与 decode

一次生成任务在 GPU 上的计算天然分两段：

- **prefill（预填充）**：把整条 prompt 一次性喂进模型，算出每个位置的 KV 并写入缓存。这一步是**计算密集（compute-bound）**的——GPU 算力吃得满。
- **decode（解码）**：每步只新算 1 个 token，复用已有 KV 缓存。这一步是**显存带宽密集（memory-bound）**的——GPU 算力大量闲置。

这两段对 GPU 资源的需求完全不对称，这是后面所有优化的出发点。

### 2.2 静态批处理的天花板

朴素的批处理是「静态」的：凑齐 N 条请求组成一个 batch，等**最慢的那条**生成完才整体释放，再凑下一批。问题是：当 batch 内多数请求已结束、只剩 1 条还在 decode 时，GPU 每步只为这 1 条 token 工作，**算力几乎全空转**——这就是静态批处理的「尾部气泡（tail bubble）」。

连续批处理要消灭的就是这个气泡。

### 2.3 V1 的统一 token 预算模型（回顾 u4-l2）

V1 调度器里**没有独立的 prefill 阶段或 decode 阶段**，只有一句话的规则：

> 每一步，调度器从总预算 `token_budget` 里，给每个请求分配若干 token，让它「已计算 token 数」追上「目标 token 数」。

这把 prefill、decode、分块、前缀缓存、推测解码全部统一进了同一个循环。本讲要讲的连续批处理和分块预填充，正是这套统一模型在工程上的两个直接产物。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `vllm/v1/core/sched/scheduler.py` | 调度器主体。`schedule()` 是每步决策入口，串起「保 RUNNING → 接 WAITING → 组装 SchedulerOutput」三段。 |
| `vllm/v1/core/sched/request_queue.py` | 请求队列实现。定义抽象 `RequestQueue` 与两种策略 `FCFSRequestQueue`、`PriorityRequestQueue`。 |
| `vllm/v1/core/sched/output.py` | 调度产出 `SchedulerOutput`，含 `num_scheduled_tokens`（每请求本步算几个 token）等字段。 |
| `vllm/config/scheduler.py` | `SchedulerConfig`，声明 `max_num_batched_tokens`、`max_num_seqs`、`enable_chunked_prefill` 等控制本讲行为的参数。 |

## 4. 核心概念与源码讲解

### 4.1 连续批处理（continuous batching）

#### 4.1.1 概念说明

连续批处理的核心想法很简单：**请求可以随时进出 batch，不必等齐**。

- 有新请求到来 → 立刻在**下一个 step** 加入正在运行的 batch（无需等当前 batch 清空）；
- 某请求生成完 → 立刻在**下一个 step** 移出 batch（无需等其他请求）。

这样 batch 里始终同时有「正在 prefill 的新请求」和「正在 decode 的旧请求」，GPU 每 step 都有活干，尾部气泡被抹平。

需要强调：连续批处理并非靠某个独立模块实现，而是**统一 token 预算 + 随时增删请求**的自然结果。调度器每步重新决定 batch 的组成，请求随到随算、随完随走。

#### 4.1.2 核心流程

每一步 `schedule()` 内部按固定顺序处理两类请求：

1. **保住 RUNNING**：遍历 `self.running`，给每个仍在运行的请求分配它能算的 token（decode 通常只需 1 个），从 `token_budget` 里扣除。
2. **接纳 WAITING**：若还有剩余预算且并发数没到上限，从 `self.waiting` 队列取新请求，分配其本步可算的 prefill token。

预算总量在每步开始时初始化：

\[ \text{token\_budget}_0 = \text{max\_num\_scheduled\_tokens} \]

每个被排进的请求消耗 \(\Delta\)：

\[ \text{token\_budget} \leftarrow \text{token\_budget} - \Delta \]

直到预算耗尽或没有可排请求。最终把「每请求本步算几个 token」打包进 `SchedulerOutput.num_scheduled_tokens`。

正因为 RUNNING 与 WAITING **共用同一池预算、同一轮循环**，prefill 和 decode 才能被自然地混进同一个 step——这就是连续批处理。

#### 4.1.3 源码精读

预算上限由 `SchedulerConfig` 决定，默认 `max_num_scheduled_tokens` 回落到 `max_num_batched_tokens`（默认 2048）：

[scheduler.py:109-114](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L109-L114) — 调度器把并发上限和单步 token 上限读入成员变量，前者来自 `max_num_seqs`（默认 128），后者来自 `max_num_batched_tokens`（默认 2048）。

每步预算在这里初始化，是整段调度的「钱包」：

[scheduler.py:459](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L459) — `token_budget = self.max_num_scheduled_tokens`。

`schedule()` 开头的这段注释，是理解整套机制的钥匙——它明确说「没有 prefill 阶段也没有 decode 阶段」：

[scheduler.py:441-450](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L441-L450) — 调度算法说明：每步只把 token 分配给请求，让其「已计算数」追上「目标数」，足以覆盖 chunked prefill、前缀缓存与推测解码。

接着是两段循环。**第一段处理 RUNNING**，计算每个运行中请求本步要算多少新 token：

[scheduler.py:516-523](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L516-L523) — 对 RUNNING 请求，`num_new_tokens = 目标token数 - 已计算token数`，再用 `min(num_new_tokens, token_budget)` 截断到剩余预算。

被排进本步的请求在这里扣预算：

[scheduler.py:631-638](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L631-L638) — 把请求加入 `scheduled_running_reqs`，记录其本步 token 数，并 `token_budget -= num_new_tokens`。

**第二段处理 WAITING**（见 4.2 详述），两段共用同一个 `token_budget`。最后组装输出并做总量校验：

[output.py:193-208](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/output.py#L193-L208) — `SchedulerOutput` 数据类，其中 `num_scheduled_tokens`（每请求本步 token 数）与 `total_num_scheduled_tokens`（其总和）就是把调度决策交给执行层的关键载体。

[scheduler.py:1108-1110](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L1108-L1110) — 断言 `total_num_scheduled_tokens <= max_num_scheduled_tokens`，保证一步不超额。

#### 4.1.4 代码实践（源码阅读型）

**实践目标**：亲手确认「RUNNING 和 WAITING 共用同一预算」这一连续批处理的根基。

**操作步骤**：

1. 打开 [scheduler.py](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py)。
2. 搜索 `token_budget =`，确认它在 L459 只被初始化**一次**。
3. 分别在 RUNNING 循环（L516 附近）和 WAITING 循环（L879 附近）找到 `token_budget -= num_new_tokens`，确认两者减的是**同一个变量**。

**需要观察的现象**：两段循环读写的是同一个 `token_budget`，没有任何地方为 prefill 单独开一份预算。

**预期结果**：你会确信——只要预算还够，一个正在 decode 的请求和一个新到的 prefill 请求会在同一步里被排进去，这正是连续批处理的实现方式。

**待本地验证**：可在实际部署中观察 `/metrics` 里的 `vllm:num_requests_running` 在请求进出时如何平滑变化（而静态批处理则会阶梯式跳变）。

#### 4.1.5 小练习与答案

**练习 1**：连续批处理消灭了静态批处理的什么问题？

> 参考答案：消灭了「尾部气泡」——batch 内多数请求已结束、只剩少数 decode 时 GPU 空转的问题。新请求可随时补进空位，使每步都尽量满载。

**练习 2**：为什么说连续批处理在 V1 里「不是一个独立模块」？

> 参考答案：因为它由「统一 token 预算 + 每步重算 batch 组成」自然产生。RUNNING 与 WAITING 共享同一个 `token_budget`、跑在同一段调度循环里，请求随到随算、随完随走，无需专门代码。

---

### 4.2 分块预填充（chunked prefill）

#### 4.2.1 概念说明

「分块预填充」回答一个具体问题：**当一条 prompt 比 `max_num_batched_tokens` 还长时怎么办？**

朴素做法是「一步算完整条 prompt」。但这有两个后果：要么被迫把 `max_num_batched_tokens` 调到极大（挤占并发、撑爆显存），要么拒绝超长 prompt。

分块预填充的思路是：**长 prefill 也可以像 decode 一样，每步只算其中一段**。一条 3000 token 的 prompt 在 `max_num_batched_tokens=2048` 下，会被切成两步——第一步算 2048 个，第二步算剩下 1052 个——期间它一直留在 `running` 队列里，和别的 decode 请求一起排队算。

更进一步，V1 默认把这条长 prefill 的**第一个块**和别的 decode **塞进同一个 step**（只要预算够）。于是 GPU 同一步内既做计算密集的 prefill、又做带宽密集的 decode，两者资源需求互补，吞吐被推到最高。这正是本讲代码实践要追踪的场景。

#### 4.2.2 核心流程

设一条新 prefill 请求的 prompt 长度为 \(L\)、本步开始时已计算 \(\text{num\_computed\_tokens}=0\)、剩余预算为 \(B\)。则本步为它分配的 token 数为：

\[
\Delta = \min\bigl(L - \text{num\_computed\_tokens},\; B\bigr)
\]

- 若 \(\Delta = L\)：本步算完整条 prompt，prefill 当步完成；
- 若 \(\Delta < L\)：本步只算 \(\Delta\) 个，剩余 \(L-\Delta\) 留到后续 step。该请求状态保持 `RUNNING`，并被打上 `is_prefill_chunk = True`（表示「还在 prefill 中」）。

判定「是否仍在 prefill」的关键不变式（在每步调度后刷新）：

\[
\text{is\_prefill\_chunk} \iff \text{num\_computed\_tokens} < \text{num\_tokens} + \text{num\_output\_placeholders}
\]

即只要还有未算的输入 token，就还是 prefill。

这一机制的开关是 `SchedulerConfig.enable_chunked_prefill`，**V1 默认开启（True）**。关闭时，一旦某 prefill 超过剩余预算，调度器会直接 `break`，等下一步整块再算（见源码精读）。

#### 4.2.3 源码精读

默认配置项在这里，注意 `enable_chunked_prefill` 默认为 `True`：

[scheduler.py(config):70-80](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/config/scheduler.py#L70-L80) — `long_prefill_token_threshold`（默认 0，表示不设上限）与 `enable_chunked_prefill`（默认 True）。`long_prefill_token_threshold` 是可选的额外上限：若设为正数，单条 prefill 一步最多算这么多 token。

WAITING 请求被接纳时，本步 token 数的计算与截断在这里：

[scheduler.py:874-914](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L874-L914) — 这是分块预填充的核心。要点逐条对应：

- L879：`num_new_tokens = request.num_tokens - num_computed_tokens`，即这条 prompt 还差多少没算；
- L899-901：可选地用 `long_prefill_token_threshold` 截断；
- L903-911：**若关闭了 chunked prefill 且本步算不完**，直接 `break`（不分块）；
- L913：`num_new_tokens = min(num_new_tokens, token_budget)`，**把本步分配额截到剩余预算**——这就是「切块」发生的地方。

请求被成功排进本步后，状态流转在这里：

[scheduler.py:1055-1082](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L1055-L1082) — 把请求 `append` 进 `self.running`、状态置为 `RUNNING`、记录 `num_computed_tokens`。关键在 L1080-1082：如果 `num_computed_tokens + num_new_tokens < request.num_tokens`，说明这条 prefill **本步没算完**，把它加入 `_inflight_prefills` 集合——下一轮它会以 RUNNING 身份继续算剩余部分。

「是否仍在 prefill」的判定在每步调度结束后刷新：

[scheduler.py:1335-1343](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L1335-L1343) — `_update_after_schedule` 中，先推进 `num_computed_tokens += num_scheduled_token`，再用前述不变式设置 `is_prefill_chunk`。一旦某请求算完所有输入 token（`is_prefill_chunk` 变 False），就从 `_inflight_prefills` 移除，正式进入纯 decode 模式。

> 补充：`num_computed_tokens` 在调度阶段是「乐观推进」的（u4-l2 已讲）。`_update_after_schedule` 在每步结束后兑现这次推进，并据实刷新 `is_prefill_chunk`，使下一步调度看到正确的剩余量。

#### 4.2.4 代码实践（数字推演型）

**实践目标**：用具体数字算出一条长 prefill 如何被切成两步。

**场景**：`max_num_scheduled_tokens = 2048`，一条 prompt 长 `L = 3000`，初始无任何 RUNNING 请求，前缀缓存未命中。

**操作步骤 / 预期结果**：

- **第 1 步**：
  - RUNNING 循环空转（无运行请求）。
  - WAITING 循环取到这条 prefill，`num_new_tokens = 3000 - 0 = 3000`；`long_prefill_token_threshold=0` 不截断；chunked prefill 开启；`num_new_tokens = min(3000, 2048) = 2048`。
  - 本步算 2048 个 prefill token，预算耗尽。因 `0 + 2048 < 3000`，请求留在 `running`，加入 `_inflight_prefills`，`is_prefill_chunk = True`。
  - `_update_after_schedule` 后：`num_computed_tokens = 2048`。
- **第 2 步**：
  - 该请求以 RUNNING 身份进入第一段循环：`num_new_tokens = 3000 - 2048 = 1052`；`min(1052, 2048) = 1052`。
  - 本步算 1052 个 token，预算还剩 `2048 - 1052 = 996`（可继续接纳别的 decode 请求）。
  - `2048 + 1052 = 3000`，prefill 全部算完，`is_prefill_chunk` 变 False，进入 decode。

**需要观察的现象**：一条 3000 token 的 prefill 被切成「2048 + 1052」两步，且第二步还有预算空位可塞别的请求。

**待本地验证**：可在日志或 metrics 里观察 `vllm:request_prefill_tokens` 是否在两步内累计达到 prompt 长度。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `enable_chunked_prefill` 关掉，一条 3000 token 的 prompt 在 `max_num_batched_tokens=2048` 下会发生什么？

> 参考答案：触发 [scheduler.py:905-911](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L905-L911) 的分支——调度器发现 `num_new_tokens(3000) > token_budget(2048)` 且 chunked prefill 关闭，直接 `break`，本步**不调度**这条请求，留到某个预算空到 ≥3000 的 step 整块算（实际上需要调大 `max_num_batched_tokens` 才能放下）。

**练习 2**：`is_prefill_chunk` 何时从 True 变成 False？

> 参考答案：在每步 `_update_after_schedule` 里，当 `num_computed_tokens` 追平 `num_tokens + num_output_placeholders`（即所有输入 token 都已算完）时，`is_prefill_chunk` 置 False，请求从 prefill 模式进入纯 decode 模式。

---

### 4.3 请求队列管理（request_queue）

#### 4.3.1 概念说明

调度器要做的决策，本质上是在几个**请求容器**之间搬运请求：哪些在等、哪些在跑、哪些被临时跳过。`request_queue.py` 把这些容器的「怎么排」抽象出来，与「跑什么」解耦。

V1 调度器维护三个容器（定义在 `scheduler.py` 的 `__init__`）：

| 容器 | 类型 | 含义 |
| --- | --- | --- |
| `self.waiting` | `RequestQueue` | 新到、尚未被排进任何 step 的请求。 |
| `self.skipped_waiting` | `RequestQueue` | 本步因依赖未就绪等原因**临时跳过**的 WAITING 请求，下一步优先重试。 |
| `self.running` | `list[Request]` | 已被接纳、正在算的请求（含 prefill 中的和 decode 中的）。 |

「怎么排」由**调度策略（policy）**决定，目前有两种：

- **FCFS（默认）**：先来先服务，`waiting` 是一个 `deque`，从左取、从右追加。
- **PRIORITY**：按 `(priority, arrival_time)` 排序的堆，`priority` 值小的先算。

#### 4.3.2 核心流程

请求的生命周期与三个容器的关系：

```
add_request  ──►  self.waiting
                        │  schedule() 第二段接纳
                        ▼
                   self.running ──► （正常 decode 直到完成，移出）
                        ▲
                        │  抢占：_preempt_request 把它 prepend 回 waiting
                        │
              self.skipped_waiting  ◄── 本步临时跳过的 WAITING 请求
                  （下一步优先重试）
```

每步 `schedule()` 第二段里，调度器用 `_select_waiting_queue_for_scheduling()` 决定**先从哪个队列取**：

- FCFS 模式：优先 `skipped_waiting`（让跳过的请求尽快重试），其次 `waiting`；
- PRIORITY 模式：比较两个队首的 `(priority, arrival_time)`，取更小者。

被取出的请求若因依赖（如远程 KV 未到、LoRA 名额满、编码缓存未就绪）暂时不能算，会被 `pop` 后 `prepend` 进 `step_skipped_waiting`，循环结束后再统一搬回 `self.skipped_waiting`，确保下一步优先重试、且不饿死老请求。

#### 4.3.3 源码精读

两个调度策略的枚举：

[request_queue.py:13-18](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/request_queue.py#L13-L18) — `SchedulingPolicy` 枚举，`FCFS` 与 `PRIORITY` 两值，对应 `SchedulerConfig.policy`（默认 `"fcfs"`）。

FCFS 实现——本质就是一个带策略方法的 `deque`：

[request_queue.py:75-128](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/request_queue.py#L75-L128) — `FCFSRequestQueue(deque[Request])`。`add_request` 走 `append`（队尾入），`pop_request` 走 `popleft`（队首出），`prepend_request` 走 `appendleft`（插队首）。

PRIORITY 实现——基于堆：

[request_queue.py:131-198](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/request_queue.py#L131-L198) — `PriorityRequestQueue` 用 `heapq` 维护堆，排序键由 `Request` 类定义（`priority` 小者优先，相同则 `arrival_time` 早者优先）。注意它的 `prepend_request` 实际等同 `add_request`——堆里没有「插队首」的概念，一律按优先级入堆。

工厂函数，按策略造队列：

[request_queue.py:201-208](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/request_queue.py#L201-L208) — `create_request_queue(policy)` 把字符串/枚举策略映射到具体队列类。

调度器在 `__init__` 里造出三个容器：

[scheduler.py:186-190](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L186-L190) — `self.waiting` 与 `self.skipped_waiting` 都是 `create_request_queue(self.policy)` 造出的同策略队列；`self.running` 是普通 `list`。

新请求进入系统的唯一入口：

[scheduler.py:2213-2235](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L2213-L2235) — `add_request`：对全新请求调用 `_enqueue_waiting_request` 入 `waiting` 队，并登记进 `self.requests` 字典。请求从此进入「等待 → 运行 → 完成」的生命周期。

「先从哪个队列取」的决策：

[scheduler.py:2064-2074](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L2064-L2074) — `_select_waiting_queue_for_scheduling`。FCFS 下优先 `skipped_waiting`；PRIORITY 下比较两队首取小者。

被跳过的请求如何「插回队首优先重试」：

[scheduler.py:1099-1101](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L1099-L1101) — 本步收集在 `step_skipped_waiting` 里的请求，循环结束后用 `prepend_requests` 整体搬到 `self.skipped_waiting` 前面，确保下一步优先处理。

抢占时请求被送回 `waiting`：

[scheduler.py:1274-1315](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L1274-L1315) — `_preempt_request`：释放该请求的 KV block、`num_computed_tokens` 清零、状态置 `PREEMPTED`，最后 `self.waiting.prepend_request(request)` 把它**插到队首**，使它能在显存腾出后优先重算（详见 u4-l2）。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：看清「一条请求因依赖未就绪被跳过」后，如何在下一步被优先重试。

**操作步骤**：

1. 阅读 [scheduler.py:684-711](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L684-L711)，找到把请求 `pop` 后 `step_skipped_waiting.prepend_request(request)` 的分支（如 `WAITING_FOR_REMOTE_KVS` 未就绪、LoRA 名额已满等）。
2. 跳到 [scheduler.py:1099-1101](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L1099-L1101)，确认这些被跳过的请求被搬到 `self.skipped_waiting` 队首。
3. 再看 [scheduler.py:2064-2074](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L2064-L2074)，确认下一步 `_select_waiting_queue_for_scheduling` 会**优先**从 `skipped_waiting` 取。

**需要观察的现象**：被跳过的请求不会丢失，也不会排到队尾饿死，而是被「插队」优先重试。

**预期结果**：理解 `waiting` 与 `skipped_waiting` 双队列设计的目的——区分「从未尝试的新请求」与「尝试过但暂未就绪的请求」，让后者尽快推进。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `waiting` 和 `skipped_waiting` 要分成两个队列，而不是合在一个 `deque` 里？

> 参考答案：分开后可以精确表达「优先级」。被跳过的请求往往只是临时卡住（如远程 KV 在路上），一旦就绪应立刻推进；新请求则按 FCFS 排队。双队列让调度器用 `_select_waiting_queue_for_scheduling` 先服务跳过队列，避免老请求被新请求不断插队而饿死，同时新请求也不会因为某个长期未就绪的跳过请求而永远排在它后面（FCFS 下 `skipped_waiting or waiting` 的短路逻辑会在跳过队列空后才取新请求）。

**练习 2**：在 PRIORITY 模式下，`prepend_request` 和 `add_request` 有区别吗？为什么？

> 参考答案：没有区别。堆结构里不存在「队首」概念，所有请求都按 `(priority, arrival_time)` 入堆，`prepend_request` 直接调用 `add_request`。这是与 FCFS（`deque` 可 `appendleft`）的本质差异。

---

## 5. 综合实践

**任务**：用具体数字与源码引用，完整追踪「一条长 prefill + 多条短 decode 合并进同一 step」的过程，并解释吞吐收益。这是本讲三个最小模块的综合应用。

**场景设定**：

- `max_num_scheduled_tokens = 2048`，`max_num_seqs = 128`，`enable_chunked_prefill = True`，`long_prefill_token_threshold = 0`。
- 当前 `self.running` 里有 **100 条 decode 请求**，每条本步只需 1 个新 token。
- `self.waiting` 队首有一条 **长 prefill 请求**，prompt 长 **2500** token，无前缀缓存命中。

**请完成**：

1. **第一步预算分配**（对应 [scheduler.py:483-672](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L483-L672) RUNNING 段）：
   - 100 条 decode 各算 1 token，共消耗 \(100\)，预算剩 \(2048 - 100 = 1948\)。
2. **接纳长 prefill**（对应 [scheduler.py:874-914](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L874-L914) WAITING 段）：
   - `num_new_tokens = 2500 - 0 = 2500`；`min(2500, 1948) = 1948`。
   - 该 prefill 本步算 1948 个，预算耗尽。因 \(0 + 1948 < 2500\)，加入 `_inflight_prefills`，`is_prefill_chunk = True`。
   - **本 step 合计**：100 decode + 1948 prefill = 2048 token，正好打满预算。
3. **第二步收尾 prefill**：
   - 该 prefill 以 RUNNING 身份进第一段：`num_new_tokens = 2500 - 1948 = 552`，剩余预算 \(2048 - 552 = 1496\) 可继续接纳新的 decode 或新 prefill。
   - \(1948 + 552 = 2500\)，prefill 完成，进入 decode。

**解释吞吐收益**：

- **prefill 与 decode 资源互补**：prefill 计算密集、decode 带宽密集。把它们塞进同一 step，GPU 的算力单元和带宽单元同时被利用，相对「先算完所有 prefill、再统一 decode」的两阶段方式，单步资源利用率显著提升。
- **消除气泡**：长 prefill 被切块后，每步都有 decode 请求填充剩余预算，GPU 不会因为「只剩 prefill 在算」或「只剩 1 条 decode」而空转。
- **响应延迟更低**：新 decode 请求无需等长 prefill 整块算完，每步都能见缝插针地被接纳。

**验收**：把上述每一步的 `token_budget` 变化、`num_computed_tokens` 推进、`is_prefill_chunk` 取值列成表格，确认与源码逻辑一致。

## 6. 本讲小结

- **连续批处理**在 V1 里不是独立模块，而是「统一 token 预算 + 每步重算 batch」的自然结果——RUNNING 与 WAITING 共享同一个 `token_budget`，请求随到随算、随完随走，消灭了静态批处理的尾部气泡。
- **分块预填充**把超长 prefill 按 `token_budget` 切成多步，靠 [scheduler.py:913](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L913) 的 `min(num_new_tokens, token_budget)` 实现；`enable_chunked_prefill` 默认开启，关闭则拒绝分块。
- 一条 prefill 是否「还在 prefill 中」由 `is_prefill_chunk` 标记，每步在 `_update_after_schedule` 中据 `num_computed_tokens < num_tokens` 刷新。
- **请求队列**由 `request_queue.py` 抽象，调度器维护 `waiting` / `skipped_waiting` / `running` 三态：新请求入 `waiting`，被跳过的进 `skipped_waiting` 优先重试，被接纳的进 `running`，被抢占的 `prepend` 回 `waiting` 队首。
- 两种策略 **FCFS（`deque`）** 与 **PRIORITY（`heap`）** 由 `SchedulerConfig.policy`（默认 `fcfs`）选择，决定队首是谁。
- 长 prefill 与多条 decode 合并进同一 step，是连续批处理 + 分块预填充的合力，让 GPU 的算力与带宽同时被吃满，是 vLLM 高吞吐的根基。

## 7. 下一步学习建议

本讲只讲了「调度器如何决定算什么」，尚未讲「KV 缓存从哪来、放哪、怎么复用」。建议继续阅读：

- **u4-l4 PagedAttention 与 KV 缓存管理**：本讲反复提到的 `kv_cache_manager.allocate_slots`、block 分配与 `_inflight_prefills` 背后的物理存储，将在那里展开。
- **u4-l5 前缀缓存**：本讲 WAITING 段里 `get_computed_blocks` 的命中逻辑，决定了 `num_computed_tokens` 的起点，是理解 prefill 长度的另一半。
- 想从调度结果看到 GPU 实际执行，可衔接 **u5-l1 EngineCore 引擎核心主循环** 与 **u5-l3 ModelRunner**，看 `SchedulerOutput.num_scheduled_tokens` 如何被翻译成一次 forward。
