# Scheduler 调度器核心

## 1. 本讲目标

本讲是 vLLM V1 调度单元的核心篇。`EngineCore` 每跑一轮（一个 step）都要先问调度器一个问题：「这一步到底算哪些请求、各算几个 token？」`Scheduler` 就是负责回答这个问题的对象。

学完本讲，你应当能够：

- 说清 `Scheduler` 在一次 step 中扮演的角色，以及它如何被 `EngineCore` 调用。
- 解释「统一 token 预算」调度模型——为什么 V1 里没有独立的 prefill 阶段和 decode 阶段。
- 描述 `schedule()` 的三大步：选 RUNNING 请求、选 WAITING 请求、组装 `SchedulerOutput`。
- 理解当 KV 缓存显存不足时，调度器为什么要「抢占」（preempt）请求以及如何抢占。
- 读懂 `SchedulerOutput` 这个数据结构携带了哪些信息给下游 worker。

## 2. 前置知识

在进入调度器之前，请确认你已掌握以下概念（前序讲义已建立）：

- **Request 与状态机**（u4-l1）：每个请求有 `num_computed_tokens`（已算多少 token）、`num_tokens`（共需算多少）、以及 `WAITING → RUNNING → FINISHED` 等状态。调度器围绕这些字段做决策。
- **KV 缓存按 block 管理**（u4-l4 将深入，本讲只需知道大致概念）：模型的中间状态（KV）不是一段连续内存，而是被切成固定大小的 block。一个请求要前进，就必须能申请新 block 来存放新 token 的 KV。显存不够 = block 不够。
- **prefill 与 decode**：处理 prompt 的第一阶段叫 prefill（一次性算很多 token），逐 token 生成叫 decode（每步算 1 个 token）。
- **EngineCore 的 busy loop**（u5-l1 将深入）：`EngineCore` 不断循环 `schedule() → execute_model() → update_from_output()`，其中 `schedule()` 就是本讲的主角。

一个关键直觉：调度器自己**不算模型**、**不碰 GPU**。它只做 CPU 上的「排座位」决策——决定这一步哪些请求上车、各带几个 token，并把决策打包成 `SchedulerOutput` 交给 worker 去真正执行。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [vllm/v1/core/sched/interface.py](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/interface.py) | 定义 `SchedulerInterface` 抽象基类与 `PauseState`，是调度器对外的「接口契约」。 |
| [vllm/v1/core/sched/scheduler.py](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py) | `Scheduler` 的实现，本讲主角，`schedule()` 主流程全在这里。 |
| [vllm/v1/core/sched/output.py](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/output.py) | 定义 `SchedulerOutput`、`NewRequestData`、`CachedRequestData`，即调度决策的「产物」。 |
| [vllm/v1/core/sched/request_queue.py](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/request_queue.py) | 请求队列抽象（FCFS / Priority），是调度器挑选请求的容器。 |
| [vllm/v1/engine/core.py](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/core.py) | `EngineCore.step()`，展示 `schedule()` 在真实 busy loop 中的调用位置。 |

---

## 4. 核心概念与源码讲解

### 4.1 SchedulerInterface：调度器的接口契约

#### 4.1.1 概念说明

vLLM 希望上层（`EngineCore`）不关心调度器到底怎么实现，只关心「我能调用哪些方法」。于是抽象出一个接口 `SchedulerInterface`，它用 `@abstractmethod` 声明了调度器必须提供的能力。这是一种典型的**依赖倒置**：上层依赖抽象，不依赖具体实现。事实上 V1 还有一个 `AsyncScheduler`（异步调度）也实现了同一接口，`EngineCore` 可以无感替换。

`SchedulerInterface` 关心的不是「怎么调度」，而是「调度器必须会做什么」，主要方法有：

- `add_request`：往队列里加一个新请求。
- `schedule`：本讲核心——产出本轮的 `SchedulerOutput`。
- `update_from_output`：本轮模型执行完后，用结果更新调度器内部状态、判断哪些请求完成了。
- `has_unfinished_requests` / `get_num_unfinished_requests`：还有没有活儿要干，驱动 busy loop 是否继续。
- `finish_requests`：从外部把某些请求标记为结束（例如客户端断开连接时中止请求）。

#### 4.1.2 核心流程

接口本身不包含流程，但它的 `schedule` 文档字符串把调度模型讲得非常清楚，值得逐字理解：

> 「本质上，调度器产出一个 `{req_id: num_tokens}` 的字典，说明本轮每个请求要处理多少 token。」
>
> ——引自 [interface.py:54-82](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/interface.py#L54-L82)

`num_tokens` 可以是：

- 新请求的整段 prompt 长度（完整 prefill）；
- `1`（一个请求在做 decode，每步生成一个 token）；
- 介于两者之间（分块 prefill、前缀缓存命中后只补一段、推测解码的草稿 token 等）。

这就是 V1 调度模型的灵魂：**一切都被统一成「本轮给某个请求分配多少个 token」**。

#### 4.1.3 源码精读

接口类定义和 `schedule` 抽象方法签名：

[interface.py:38-83 — SchedulerInterface 类与 schedule 抽象方法](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/interface.py#L38-L83) 声明了所有调度器必须实现的方法。注意 `schedule` 接受一个 `throttle_prefills: bool` 参数，这是 DP（数据并行）场景下用来对齐各 rank 的 prefill 节奏的，普通单引擎调用时恒为 `False`。

同样重要的是 `update_from_output` 的契约：

[interface.py:91-109 — update_from_output 抽象方法](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/interface.py#L91-L109) 它在模型执行后被调用，读取 `ModelRunnerOutput`（采样出的 token 等），更新请求状态，并返回按客户端分组的输出。本讲会在 4.3 节看到 `EngineCore.step()` 是如何把 `schedule` 与 `update_from_output` 配对使用的。

另外，接口里还有一个 `PauseState` 枚举：

[interface.py:24-35 — PauseState 暂停状态](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/interface.py#L24-L35) 分 `UNPAUSED / PAUSED_NEW / PAUSED_ALL` 三档，用于热更新权重等场景临时停止调度。本讲不展开。

#### 4.1.4 代码实践

**目标**：确认 `Scheduler` 与 `SchedulerInterface` 的继承关系，并看清接口规定了哪些方法。

**操作步骤**（源码阅读型）：

1. 打开 [vllm/v1/core/sched/scheduler.py:69](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L69)，确认 `class Scheduler(SchedulerInterface)`。
2. 在 [interface.py](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/interface.py) 中数一下有多少个 `@abstractmethod`。
3. 注意 `has_unfinished_requests`、`has_requests` 这类方法在接口里有**默认实现**（不是 abstract），它们调用抽象方法 `get_num_unfinished_requests`。

**需要观察的现象**：接口里既有纯抽象方法，也有带默认实现的辅助方法（例如 [interface.py:173-176](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/interface.py#L173-L176) 的 `has_unfinished_requests`）。

**预期结果**：理解「接口=契约」，`Scheduler` 必须实现所有 abstract 方法，但可复用接口提供的默认方法。

#### 4.1.5 小练习与答案

**练习 1**：`has_unfinished_requests()` 在接口里有默认实现，但它依赖哪个抽象方法？
**答案**：依赖 `get_num_unfinished_requests()`，见 [interface.py:168-176](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/interface.py#L168-L176)。

**练习 2**：`has_finished_requests()` 的文档特别强调「它和 `not has_unfinished_requests()` 不一样」，为什么？
**答案**：调度器内部维护了一个「上一步刚结束、但还没在下一次 `schedule()` 里上报给 worker 清理」的请求列表（`finished_req_ids`）。`has_finished_requests()` 检查的是这个待清理列表是否非空，用于 DP attention 等场景。

---

### 4.2 Scheduler 的构造：调度约束与请求队列

#### 4.2.1 概念说明

调度器并不是「想调度谁就调度谁」，它受三类硬约束（来自 `SchedulerConfig` / `CacheConfig`）：

1. **token 预算**（`max_num_scheduled_tokens`，通常等于 `max_num_batched_tokens`，默认 2048）：一轮里所有请求分到的 token 总数不能超过它。
2. **并发数上限**（`max_num_seqs`，默认 128）：同时在跑（RUNNING）的请求数不能超过它。
3. **KV 缓存可用 block 数**：每个新 token 都要落进一个 block，block 池见底就没法再分。这是动态的，取决于当前有多少请求在占着显存。

调度器维护两个核心容器：

- `waiting` 队列：还没开始算（或被抢占后重新排队）的请求，按调度策略（FCFS 或 Priority）排序。
- `running` 列表：已经被接纳、正在推进的请求。

另外还有一个 `skipped_waiting` 队列，专门存放「本轮因为依赖未满足（如等待远程 KV、等待 grammar 编译）而被跳过」的请求——本讲先建立主框架，细节留到扩展篇。

#### 4.2.2 核心流程

构造阶段的伪代码：

```
解析 vllm_config 中的各子配置
读取约束：max_num_running_reqs, max_num_scheduled_tokens, max_model_len
根据 policy 创建 waiting / skipped_waiting 队列（FCFS 或 Priority）
创建 running 列表（空）
创建 KVCacheManager（管 block 分配，下一讲深入）
创建 finished_req_ids / reset_preempted_req_ids 等簿记集合
```

调度策略（[request_queue.py:13-17](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/request_queue.py#L13-L17)）只有两种：

- `FCFS`：先来先服务，用双端队列 `deque` 实现（[request_queue.py:75](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/request_queue.py#L75)）。
- `PRIORITY`：按 `(priority, arrival_time)` 排序的最小堆（[request_queue.py:131-142](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/request_queue.py#L131-L142)）。

#### 4.2.3 源码精读

约束字段的初始化：

[scheduler.py:108-115 — 调度约束](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L108-L115) 设置了 `max_num_running_reqs = max_num_seqs`，并把 `max_num_scheduled_tokens` 设为配置值或回退到 `max_num_batched_tokens`。

队列的创建：

[scheduler.py:178-196 — 请求容器](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L178-L196) 注意 `waiting` 与 `skipped_waiting` 都由 `create_request_queue(self.policy)` 生成，`running` 只是一个普通 `list`。

KV 管理器的创建（本讲只需知道它存在）：

[scheduler.py:276-290 — 创建 KVCacheManager](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L276-L290) 它把显存预算、watermark、是否启用前缀缓存等都交给 block 池管理。下一讲（u4-l4）会专门拆解它。

#### 4.2.4 代码实践

**目标**：用测试辅助函数直接构造一个 `Scheduler`，观察它的内部容器。

**操作步骤**（源码阅读型，无需 GPU）：

1. 打开测试辅助文件 [tests/v1/core/utils.py:49-202](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/tests/v1/core/utils.py#L49-L202)，看 `create_scheduler` 如何拼装 `VllmConfig` 并实例化 `Scheduler`。
2. 注意它设置了 `watermark=0.0`、`num_blocks=10000`，目的是让抢占行为在测试里**确定且可预测**。

**需要观察的现象**：`create_requests` 生成的请求每个默认 `num_tokens=10`、`max_tokens=16`，是构造「短请求」来跑调度逻辑的标准做法。

**预期结果**：理解在真实部署里 `Scheduler` 是由 `EngineCore` 在初始化时构造的，而在单元测试里则用 `create_scheduler` 隔离地构造。

#### 4.2.5 小练习与答案

**练习 1**：`max_num_scheduled_tokens` 与 `max_num_batched_tokens` 是什么关系？
**答案**：前者优先取配置中的 `max_num_scheduled_tokens`；若为 `None`，则回退到 `max_num_batched_tokens`（默认 2048），见 [scheduler.py:110-114](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L110-L114)。

**练习 2**：FCFS 队列和 Priority 队列在「抢占」语义上有什么微妙差别？
**答案**：FCFS 用 `deque`，`prepend_request` 能把请求插回队首；Priority 用最小堆，没有「队首」概念，`prepend_request` 实际等价于重新 `add_request` 按优先级入堆，见 [request_queue.py:160-165](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/request_queue.py#L160-L165)。这会影响抢占后请求被重新调度的顺序。

---

### 4.3 schedule() 主流程：选请求 → 分配 block → 生成输出

#### 4.3.1 概念说明

这是本讲最重要的部分。先抛出 V1 调度最核心的设计理念（来自源码注释）：

> 「调度器里没有所谓的『decode 阶段』或『prefill 阶段』。每个请求只有一个 `num_computed_tokens`（已算）和一个 `num_tokens`（目标）。每一步，调度器只是尽量给各请求分配 token，让它们的 `num_computed_tokens` 追上目标。这个模型足够通用，能同时覆盖分块 prefill、前缀缓存、推测解码。」

换句话说，**prefill 和 decode 不是两种模式，而是「分配的 token 数不同」的两种情形**。这种统一视角让一套代码处理所有情况。

#### 4.3.2 核心流程

`schedule()` 的大致步骤：

```
1. token_budget = max_num_scheduled_tokens          # 本轮可用 token 预算
   kv_cache_manager.new_step_starts()               # block 池进入新的一轮

2. 遍历 running 列表（已在跑的请求）:
     计算它本轮还需要算多少 token: num_new_tokens
     让 KVCacheManager 给它分配 block: allocate_slots()
       └─ 如果 block 不够 → 触发抢占（见 4.4）
     记录到 num_scheduled_tokens[req_id]
     token_budget -= num_new_tokens

3. 如果没有发生抢占 且 处于 UNPAUSED:
     遍历 waiting 队列（新请求）:
       先查前缀缓存命中（已算的 token 可免算）
       计算需要补算的 num_new_tokens
       分配 block、接纳进 running
       token_budget -= num_new_tokens

4. 断言检查（总 token 数不超预算、并发数不超上限等）

5. 组装 SchedulerOutput（见 4.5）
   _update_after_schedule(): 把 num_computed_tokens 提前推进
```

两个关键细节：

- **先 running 后 waiting**：已经在 decode 的请求优先保住，剩余预算再接纳新请求。这避免了一个长 prefill 反复抢占已生成到一半的 decode。
- **`num_computed_tokens` 在调度时「乐观推进」**：调度阶段就把它加上本轮分配的 token 数（在 `_update_after_schedule` 里），这样下一步就能继续推进，不必等模型真正跑完。如果后续有 token 被拒绝（如推测解码草稿被拒），`update_from_output` 会回退修正。

#### 4.3.3 源码精读

`schedule()` 入口与算法说明：

[scheduler.py:439-459 — schedule() 开头与调度哲学注释](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L439-L459) 这段注释是理解整个调度器的钥匙。注意 `token_budget = self.max_num_scheduled_tokens` 这一行就是预算的起点。

**第一步：调度 RUNNING 请求**

[scheduler.py:483-532 — 调度 running 请求并计算 num_new_tokens](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L483-L532) 这里 `num_new_tokens` 由 `num_tokens_with_spec - num_computed_tokens` 算出，再用 `token_budget` 和 `max_model_len` 两次裁剪。对一个普通 decode 请求，结果是 1；对一个刚进来的请求，结果是它的 prompt 长度（或被切块后的剩余量）。

**分配 block + 抢占循环**

[scheduler.py:575-629 — allocate_slots 与抢占](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L575-L629) 重点看这个 `while True`：调用 `kv_cache_manager.allocate_slots(...)` 尝试给请求要 block；如果返回 `None`（block 不够），就抢占一个优先级最低的 running 请求腾出空间，再重试。抢占细节见 4.4 节。

**第二步：调度 WAITING 请求**

[scheduler.py:683-766 — 调度 waiting 请求与查前缀缓存](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L683-L766) 这里先检查并发数上限（`num_running >= max_num_running_reqs` 就停），然后对队首请求查 `kv_cache_manager.get_computed_blocks(request)`——这一步会返回前缀缓存命中的 block 数，让请求不必重算已经缓存的前缀。前缀缓存机制在 u4-l5 详讲。

分块 prefill 的关键判断：

[scheduler.py:905-911 — 关闭 chunked prefill 时停止调度](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L905-L911) 如果没开分块 prefill 且一个新请求的 token 数超过剩余预算，就直接 `break`——它必须整段 prefill，不能切。开启分块 prefill（默认）时，超长 prompt 会被切成多步，每步算一段。

**调度结束后推进计数**

[scheduler.py:1317-1343 — _update_after_schedule 推进 num_computed_tokens](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L1317-L1343) 注意注释里的三点说明：当前 step 的 `SchedulerOutput` 仍要带「原始」的 scheduled token 数（供 worker 准备输入），而调度器内部已经把 `num_computed_tokens` 推进，使得下一步能立即继续。这就是 V1 异步调度的「乐观计数」机制。

**`EngineCore.step()` 中的调用位置**

[core.py:584-614 — EngineCore.step 串起 schedule → execute → update](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/engine/core.py#L584-L614) 真实调用链：`has_requests()` 判断有活 → `schedule()` 出决策 → `execute_model(non_block=True)` 提交 GPU（返回 Future，不阻塞）→ `future.result()` 拿结果 → `update_from_output()` 更新状态。`schedule()` 就是这条链的第一环。

#### 4.3.4 代码实践

**目标**：跟踪一条「长 prefill + 短 decode」共存的 step，体会统一预算模型如何把它们塞进同一轮。

**操作步骤**（源码阅读 + 跑测试）：

1. 在 [tests/v1/core/test_scheduler.py](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/tests/v1/core/test_scheduler.py) 找到涉及混合 prefill/decode 的测试（如 `test_mixed_prefill_decode`、`test_chunked_prefill` 等）。
2. 尝试运行（环境允许时）：

   ```bash
   .venv/bin/python -m pytest tests/v1/core/test_scheduler.py -v
   ```

3. 若无法运行，则做源码跟踪：在 [scheduler.py:485](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L485) 处设想一个 `running` 列表里有 3 个 decode 请求（各需 1 token），`waiting` 里有 1 个 1000-token 的 prefill 请求，`token_budget=2048`。手动演算每步扣减。

**需要观察的现象**：3 个 decode 共扣 3，剩 2045；接着 1000-token prefill 被整段接纳（若开了分块且 prompt 超过预算则会被切）。同一 step 内 prefill 和 decode 共存——这就是「连续批处理」的由来。

**预期结果**：理解 `num_scheduled_tokens` 字典里，decode 请求值为 1、prefill 请求值为其 token 数，二者相加不超过 2048。

> 若本地无法运行测试，明确标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么调度器要先处理 running 再处理 waiting，而不是反过来？
**答案**：优先保障已经在生成的请求（避免一个 decode 生成到一半被长 prefill 反复打断），把剩余预算才分给新请求，提升整体延迟稳定性。

**练习 2**：`num_computed_tokens` 为什么要在 `schedule()` 阶段就乐观推进，而不是等模型执行完？
**答案**：因为 V1 的异步调度会让「下一步的调度」与「当前步的执行」重叠。若等执行完再推进，下一步就无法提前决策；乐观推进 + 执行后用 `update_from_output` 修正（如回退被拒的 spec token），能最大化 CPU 调度与 GPU 计算的重叠。

**练习 3**：若关闭了 chunked prefill，一个 5000-token 的 prompt 在 `max_num_batched_tokens=2048` 下会怎样？
**答案**：见 [scheduler.py:905-911](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L905-L911)，它会 `break` 暂不调度，直到能独占足够预算。开启 chunked prefill（默认）则会把它切成多段逐步算。

---

### 4.4 抢占（Preemption）：显存不足时的换出

#### 4.4.1 概念说明

KV 缓存是有限的显存资源。当一个新的 RUNNING 请求需要 block，而 block 池没有空闲块时，调度器有两条路：

1. **不接纳新请求**（waiting 里的请求继续等）。
2. **抢占一个正在跑的请求**，把它的 KV block 释放，腾出空间。

V1 的策略是：对**已在 running 列表里的请求**，如果它自己分配不到 block，就抢占「优先级最低」的另一个 running 请求。被抢占的请求会被放回 `waiting` 队列，状态变为 `PREEMPTED`，`num_computed_tokens` 清零——**它要从头重算**（recalculate 模式）。这是一种用「计算换显存」的权衡：宁可重算，也要保证当前高优先级的请求能跑。

> 说明：vLLM 历史上有「重算（recalculate）」和「换出（swap）」两种抢占方式。V1 调度器核心实现的是重算式抢占：释放 block、清零进度、重新排队。

#### 4.4.2 核心流程

抢占的决策点在「分配 block 失败」时：

```
attempt allocate_slots(request)
  if 失败 (返回 None):
     选一个优先级最低的 running 请求 (FCFS: 列表末尾; Priority: priority 最大者)
     调用 _preempt_request(它):
        释放它的 block (归还 block 池)
        encoder_cache 也释放
        status = PREEMPTED
        num_computed_tokens = 0          # 进度清零
        把它 prepend 回 waiting 队列
     重试 allocate_slots(request)
     if 被抢占的就是自己 → 彻底没法调度, break
```

关键：`_preempt_request` 不会把请求从 `running` 移除，**移除动作在调用方完成**（注释明确要求），因为它要在不同策略下选择不同的移除方式。

#### 4.4.3 源码精读

抢占触发点（在 4.3 已贴过的循环里）：

[scheduler.py:587-625 — 分配失败时抢占最低优先级请求](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L587-L625) 注意两种策略选受害者的方式不同：

- `PRIORITY`：`max(self.running, key=lambda r: (r.priority, r.arrival_time))`——选优先级数值最大（最低优先级）的。
- `FCFS`：`self.running.pop()`——直接弹末尾（最近才加入的）。

而且如果受害者在本 step 里已被安排进 `scheduled_running_reqs`，还要把它已占的预算和 block 记录**回滚**（`token_budget += ...`、`req_to_new_blocks.pop(...)`）。

`_preempt_request` 的实现：

[scheduler.py:1274-1315 — _preempt_request 换出请求](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L1274-L1315) 核心动作：`self._free_request_blocks(request)` 释放 block、`request.num_computed_tokens = 0` 清零进度、`self.waiting.prepend_request(request)` 插回队首。`num_preemptions += 1` 用于统计和前缀缓存命中的判断。

为什么 `waiting` 队列在抢占后还会被调度？看这段条件：

[scheduler.py:684 — 没发生抢占才调度 waiting](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L684) `if not preempted_reqs and ...` ——一旦本 step 发生了抢占，就不再接纳新 waiting 请求，避免连锁抢占。

#### 4.4.4 代码实践

**目标**：构造显存极度紧张的场景，触发并观察抢占。

**操作步骤**（源码阅读型）：

1. 打开 [tests/v1/core/test_scheduler.py](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/tests/v1/core/test_scheduler.py)，搜索 `preempt` 相关测试（如 `test_preemption`、`test_priority_preemption`）。
2. 关注这些测试如何用很小的 `num_blocks`（如几块）和多个请求制造「挤不下」的局面。
3. 在源码 [scheduler.py:617-622](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L617-L622) 处确认：被抢占请求会出现在 `preempted_reqs` 列表，其 id 最终进入 `SchedulerOutput.preempted_req_ids`（见 4.5）。

**需要观察的现象**：被抢占请求的 `num_preemptions` 自增、`num_computed_tokens` 归零、状态变 `PREEMPTED`，并被 `prepend` 回 `waiting` 队首，下一步会被当作「恢复中的请求」重新调度。

**预期结果**：理解「抢占 = 计算换显存」的代价是重算，因此 watermark（预留一块空闲 block 缓冲）能减少抢占频率。

#### 4.4.5 小练习与答案

**练习 1**：为什么抢占发生后，本 step 不再接纳新的 waiting 请求？
**答案**：见 [scheduler.py:684](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L684)。显存已紧张到要换出别人，再接纳新请求只会引发连锁抢占，毫无收益。

**练习 2**：被抢占请求的进度为什么直接清零而不是保留？
**答案**：V1 用重算式抢占，释放的 block 会被别的请求复用，原 KV 内容已不可信，只能从 prompt 头重算（[scheduler.py:1290-1294](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L1290-L1294)）。代价是浪费之前的计算，所以 watermark 默认预留缓冲来尽量避免。

---

### 4.5 SchedulerOutput：一次调度的产物

#### 4.5.1 概念说明

`schedule()` 的返回值是 `SchedulerOutput`。它是调度器与 worker 之间的**数据契约**：worker（`ModelRunner`）拿到它，就知道该怎么准备输入张量、跑 forward。

它最核心的字段是 `num_scheduled_tokens: dict[str, int]`——正是 4.1 提到的「每个请求算几个 token」。围绕它还有：

- `scheduled_new_reqs`：本步**首次**被调度的请求（worker 还没缓存过它们的数据，需要完整发送）。
- `scheduled_cached_reqs`：之前已调度过的请求（worker 已缓存，只发 diff，省通信）。
- `finished_req_ids`：上一步结束后刚完成的请求 id（通知 worker 清理它们的状态）。
- `preempted_req_ids`：本步被抢占的请求 id（v2 model runner 用）。
- `num_common_prefix_blocks`：所有 running 请求的最长公共前缀 block 数，供 cascade attention 优化使用。

#### 4.5.2 核心流程

`SchedulerOutput` 的组装发生在 `schedule()` 末尾：

```
汇总 num_scheduled_tokens, total_num_scheduled_tokens
为 new_reqs 构造 NewRequestData（含 block_ids）
为 running/resumed 构造 CachedRequestData（只含 diff）
计算公共前缀 block 数
构造 SchedulerOutput(...) 并返回
```

worker 侧的消费逻辑（本讲只点一下）：拿到 `num_scheduled_tokens` 后，按其中的 req_id 和 token 数组织输入张量；遇到 `scheduled_new_reqs` 就为新请求分配 model runner 内部槽位。

#### 4.5.3 源码精读

`SchedulerOutput` 数据类定义：

[output.py:192-269 — SchedulerOutput 字段定义](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/output.py#L192-L269) 重点字段：`num_scheduled_tokens`（[output.py:205](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/output.py#L205)）、`scheduled_new_reqs` / `scheduled_cached_reqs`（[output.py:197-201](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/output.py#L197-L201)）、`finished_req_ids`（[output.py:224](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/output.py#L224)）、`preempted_req_ids`（[output.py:233](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/output.py#L233)）。

构造 SchedulerOutput 的代码：

[scheduler.py:1208-1229 — 组装 SchedulerOutput](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L1208-L1229) 把前面几步累积的字典（`num_scheduled_tokens`、`req_to_new_blocks`、`scheduled_new_reqs` 等）打包进 `SchedulerOutput`。注意 `finished_req_ids=self.finished_req_ids`——调度器把上一步累积的「待清理」集合直接转交给 worker。

`NewRequestData` 与 `CachedRequestData` 的分工：

[output.py:34-69 — NewRequestData](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/output.py#L34-L69) 携带请求的完整信息（prompt_token_ids、block_ids、sampling_params 等）。
[output.py:115-148 — CachedRequestData](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/output.py#L115-L148) 只携带增量（新分配的 block_ids、新的 num_computed_tokens 等）。这种「新请求发全量、旧请求发增量」的设计显著降低了多进程间通信量。

断言检查（保证调度不变量）：

[scheduler.py:1108-1119 — 调度不变量断言](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L1108-L1119) 确保：总 token 数 ≤ 预算、并发数 ≤ 上限、本轮被调度的请求数 ≤ running 总数。

#### 4.5.4 代码实践

**目标**：实际调用一次 `schedule()`，打印 `SchedulerOutput` 的关键字段。

**操作步骤**（源码阅读 + 可选运行）：

1. 在 `create_scheduler()`（[tests/v1/core/utils.py:49](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/tests/v1/core/utils.py#L49)）构造的调度器上，用 `create_requests(num_requests=3)` 加 3 个请求。
2. 调用 `scheduler.schedule()`，打印 `output.num_scheduled_tokens`、`output.scheduled_new_reqs`、`output.total_num_scheduled_tokens`。

   ```python
   scheduler = create_scheduler()
   for r in create_requests(num_requests=3):
       scheduler.add_request(r)
   out = scheduler.schedule()
   print(out.num_scheduled_tokens)        # {'0': 10, '1': 10, '2': 10}
   print(out.total_num_scheduled_tokens)  # 30
   print(len(out.scheduled_new_reqs))     # 3
   ```

   （以上为示例代码，具体数值取决于 `create_requests` 的 `num_tokens` 默认 10；待本地验证。）

**需要观察的现象**：3 个新请求各分到 10 个 token（完整 prefill），都被列入 `scheduled_new_reqs`；再调一次 `schedule()`，它们会进入 `scheduled_cached_reqs`，`num_scheduled_tokens` 变为各 1（decode）。

**预期结果**：亲眼看到「同一请求首次出现是 new、之后是 cached」，理解增量通信设计。

#### 4.5.5 小练习与答案

**练习 1**：`scheduled_new_reqs` 与 `scheduled_cached_reqs` 为什么要分开？
**答案**：新请求 worker 从没见过，必须发完整数据（prompt、block 表、采样参数等）；旧请求 worker 已缓存，只需发增量 diff。分开能最小化跨进程通信，见 [output.py:197-201](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/output.py#L197-L201)。

**练习 2**：`num_common_prefix_blocks` 这个字段服务什么优化？
**答案**：服务于 cascade attention——若多个请求共享相同的前缀 KV block，可以让它们共享一次前缀 attention 计算，省算力。调度器在 [scheduler.py:1123-1129](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L1123-L1129) 算出这个公共长度并随 `SchedulerOutput` 下发。

---

## 5. 综合实践

**任务**：手画一轮 step 的「调度账本」，验证统一预算模型与抢占逻辑。

设定（可在纸上或表格里推演）：

- 配置：`max_num_scheduled_tokens = 100`，`max_num_seqs = 4`，开启 chunked prefill，block 池足够大。
- 时刻 T：`running` 里有 A、B 两个 decode 请求（各已生成若干 token，本轮各需 1 token）；`waiting` 里有 C（prompt 150 token）。

请完成：

1. **推演 schedule() 的第一步（处理 running）**：A、B 各扣 1，`token_budget` 从 100 变 98。
2. **推演第二步（处理 waiting）**：C 有 150 token，但剩余预算只有 98。因为开了 chunked prefill，C 本轮只算 98 个 token（成为 prefill chunk），剩余 52 留到下一步。把 C 记入 `num_scheduled_tokens = {'A':1, 'B':1, 'C':98}`。
3. **写出 SchedulerOutput**：A、B 在 `scheduled_cached_reqs`（已调度过），C 在 `scheduled_new_reqs`（首次）。`total_num_scheduled_tokens = 100`。
4. **抢占情景**：若把 block 池缩到刚好只够 A、B，C 申请新 block 时 `allocate_slots` 返回 `None`。问：FCFS 下谁会被抢占？答：`running.pop()` 弹末尾（假设是 B），B 的进度清零回 `waiting`，C 仍无法调度（因为抢占后不再接纳 waiting，见 [scheduler.py:684](https://github.com/vllm-project/vllm/blob/f0de1a604cad003379e5bb4dfc3cc5d2a1f25fa8/vllm/v1/core/sched/scheduler.py#L684)），下一步再尝试。

**验收**：你能用一句话解释「为什么 prefill 和 decode 能进同一轮」——因为它们都只是 `num_scheduled_tokens` 字典里大小不同的条目，统一受 `token_budget` 约束。

---

## 6. 本讲小结

- `Scheduler` 实现 `SchedulerInterface`，是 CPU 上的「排座位」决策者，自己不碰 GPU；它的产出 `SchedulerOutput` 才是 worker 执行的依据。
- V1 用**统一 token 预算模型**：没有独立的 prefill/decode 阶段，只有「本轮给每个请求分配几个 token」，一套逻辑覆盖分块 prefill、前缀缓存、推测解码。
- `schedule()` 三步走：先保 running（每步扣减 `token_budget`），再在剩余预算内接纳 waiting（查前缀缓存命中），最后组装 `SchedulerOutput`。
- `num_computed_tokens` 在调度阶段**乐观推进**，由 `update_from_output` 在执行后修正，支撑 CPU 调度与 GPU 计算重叠。
- 当 KV block 不足时触发**抢占**：换出优先级最低的 running 请求、释放 block、进度清零、回 `waiting` 队列重算（计算换显存）；watermark 用于缓冲、减少抢占。
- `SchedulerOutput` 区分 `scheduled_new_reqs`（发全量）与 `scheduled_cached_reqs`（发增量），最小化跨进程通信。

## 7. 下一步学习建议

- **u4-l3 连续批处理与请求队列**：深入 `request_queue.py` 的 FCFS/Priority 实现，以及 chunked prefill 如何把长 prompt 切成多步。
- **u4-l4 PagedAttention 与 KV 缓存管理**：拆解 `KVCacheManager` / `BlockPool`，看 `allocate_slots` 到底怎么管 block 分配与释放——本讲里它是个黑盒。
- **u4-l5 前缀缓存**：理解 `get_computed_blocks` 如何命中共享前缀、让请求免算已缓存 token。
- **u5-l1 EngineCore 引擎核心主循环**：把本讲的 `schedule()` 放回完整的 `step()` 循环里，看 `EngineCore` 如何驱动调度器与 worker 协作。
