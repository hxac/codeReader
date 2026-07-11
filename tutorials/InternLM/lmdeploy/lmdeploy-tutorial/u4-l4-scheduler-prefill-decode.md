# 调度器 Scheduler：prefill 与 decode

> 本讲属于「PyTorch 后端：引擎执行与调度」单元（u4），承接 u4-l2 异步推理循环 EngineLoop。
> EngineLoop 的 `main_loop` 每一步都要问一个问题：「下一步该让哪些序列跑、跑 prefill 还是 decode？」
> 回答这个问题的，就是本讲的 `Scheduler`。

## 1. 本讲目标

学完本讲，你应当能够：

- 用自己的话说清**持续批处理（continuous batching）**到底在持续什么，以及它为什么能大幅提升吞吐。
- 看懂 `Scheduler.schedule()` 如何根据 `is_prefill` 把工作分派给 `_schedule_prefill` 与 `_schedule_decoding` 两条分支。
- 读懂 `_schedule_decoding`：decode 阶段如何为在跑序列补块、必要时如何「抢占（preempt）」。
- 读懂 `_schedule_prefill`：新请求如何被「准入（admission）」进 batch，长上下文如何被切成 chunk，前缀缓存如何让一个原本超预算的请求被收编。
- 准确指出 `max_prefill_token_num` 在源码里被用作哪两件事的门槛，并解释它的作用。

## 2. 前置知识

本讲假设你已经理解（来自前置讲义）：

- **Prefill 与 Decode 两阶段**：Prefill 是「吃进整段 prompt、并行算每个位置的 KV」；Decode 是「一次只生成一个新 token」。Prefill 是计算密集，Decode 是访存密集。两者混在一个 batch 里跑，是持续批处理的物理基础。
- **Paged Attention / 分块 KV 缓存**：KV cache 不是一整块连续显存，而是被切成固定大小的 `block`（块），按需分配回收。本讲的调度器不停地「申请块、释放块」，但块本身的物理管理由 `block_manager` 负责（详见 u4-l5）。
- **SchedulerSequence 与 MessageStatus**：一条用户请求在引擎内部被抽象成一条「序列（seq）」，它有一个状态（`WAITING/READY/RUNNING/STOPPED` 等），状态决定了它此刻能不能被调度。注意：这些类型定义在**引擎面** `lmdeploy/pytorch/messages.py`，不是用户面的 `lmdeploy/messages.py`。
- **EngineLoop 主循环**：`main_loop` 每一步先取输入、再 forward、再收输出；「取输入」这一步正是调用 `InputsMaker`，而 `InputsMaker` 内部会调用本讲的 `scheduler.schedule(...)`（u4-l2 已说明 `scheduler.tick()` 在 `_send_next_inputs_impl` 里被调用）。

一个关键的认知：**调度器只做「决策」，不碰张量**。它决定「下一批跑哪些序列、各需要几块 KV、要不要驱逐别人」，但真正把 token 拼成张量喂给模型 forward 的是 `InputsMaker`。调度器是「交警」，`InputsMaker` 是「装车工」。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到哪些部分 |
| --- | --- | --- |
| `lmdeploy/pytorch/paging/scheduler.py` | **本讲主角**。请求调度与 KV/状态资源的准入决策。 | `Scheduler` 类、`schedule()`、`_schedule_prefill()`、`_schedule_decoding()`、长上下文分块与准入门槛 |
| `lmdeploy/pytorch/paging/state_manager.py` | SSM（状态空间模型）运行态/检查点态的槽位分配。 | `StateManager`、`build_state_manager`（理解 `state_manager.allocate(seq)` 这一步） |
| `lmdeploy/pytorch/paging/eviction_helper/` | 「驱逐」策略实现，按 `eviction_type` 选择。 | `RecomputeEvictionHelper.evict_for_seq`（理解 prefill/decode 里反复出现的「先驱逐再分配」） |
| `lmdeploy/pytorch/messages.py` | 序列、会话、状态枚举、`SequenceManager`。 | `MessageStatus`、`SequenceManager`（按状态分桶管理序列） |
| `lmdeploy/pytorch/config.py` | 引擎内部配置数据类。 | `SchedulerConfig`（`max_batches`/`prefill_interval`/`eviction_type`）、`CacheConfig.max_prefill_token_num` |
| `lmdeploy/pytorch/engine/inputs_maker.py` | 调度器结果 → forward 张量的翻译官，也是调度器的**调用方**。 | `do_prefill_default()`（决定该不该走 prefill）、`schedule(...)` 调用点 |

## 4. 核心概念与源码讲解

### 4.1 持续批处理与 Scheduler 的整体结构

#### 4.1.1 概念说明：持续批处理到底在「持续」什么？

最朴素的做法是「静态批处理」：凑齐 N 个请求组成一个 batch，等这 N 个请求**全部生成完**才一起退出，再凑下一批。它有两个致命浪费：

- **尾巴浪费**：batch 里有的请求 10 个 token 就结束了，有的要 500 个，先结束的 GPU 算力空转，陪着没结束的等到最后。
- **准入延迟**：第 N+1 个请求来了，必须等当前整批结束才能进。

**持续批处理（continuous batching，又叫 iteration-level / dynamic batching）** 把「加入/退出」的粒度从「整批」降到「每一步 forward」：每生成一个 token（即每一个 decode step），调度器都重新决定一次——谁继续跑、谁结束了该退出、谁排着队可以现在加进来。于是：

- 一个请求生成完，立刻从 batch 里摘掉，腾出的 KV 块给下一位。
- 新请求不必等，下一个 step 就能以 prefill 形式插进正在 decode 的 batch。
- 同一个 batch 里可以**同时**混着 prefill（吃 prompt）和 decode（蹦单 token）。

这正是 lmdeploy（以及 vLLM 等）高吞吐的根源。调度器要解决的核心矛盾就是：「在 KV 显存有限、且每一步都有新请求到来的动态环境里，如何最大化每一步 forward 的有效工作量。」

#### 4.1.2 核心流程：Scheduler 持有哪些「家当」

`Scheduler` 在构造期就把调度所需的全部资源管理器装配好，自己则是一个「协调者」：

```
Scheduler.__init__
  ├─ sessions: OrderedDict        # 会话表（session_id -> SchedulerSession）
  ├─ state_manager               # SSM 运行态/检查点态槽位分配器
  ├─ block_manager               # KV 块的分配/释放（Paged Attention 的物理层）
  ├─ block_trie                  # 前缀缓存字典树（命中已有 KV、记录统计）
  ├─ eviction_helper             # 驱逐策略（默认 recompute）
  ├─ seq_manager: SequenceManager# 按 MessageStatus 分桶管理所有序列
  └─ scheduler_tick = 0          # 调度步计数（每 forward 一次 +1）
```

#### 4.1.3 源码精读

构造函数装配资源管理器（注意三件 KV 相关家当：`block_manager`、`block_trie`、`state_manager`）：

[scheduler.py:L116-L141](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/scheduler.py#L116-L141) — `Scheduler.__init__`：建立会话表、三个资源管理器与驱逐助手；`is_ssm` 由 `states_shapes` 是否非空判定（SSM = 状态空间模型，如 Mamba 类）。

调度步计数器——u4-l2 提过「派发即计步」，计数就发生在这里：

[scheduler.py:L143-L146](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/scheduler.py#L143-L146) — `tick()`：每被 `InputsMaker` 调用一次（对应一次 forward 派发），`scheduler_tick` 自增。

#### 4.1.4 代码实践：从外部看调度步

实践目标：理解 `scheduler_tick` 随 forward 增长。

操作步骤：

1. 打开 `lmdeploy/pytorch/engine/inputs_maker.py`，全局搜 `scheduler.tick()` 与 `scheduler_tick`，确认它出现在 `_send_next_inputs_impl` 的每次 forward 派发处。
2. 打开 `lmdeploy/pytorch/paging/scheduler.py`，定位 `schedule_metrics` 属性（文件末尾），看它把 `scheduler_tick` 暴露进 `ScheduleMetrics`。

需要观察的现象 / 预期结果：`scheduler_tick` 是一个**单调递增、与 forward 次数一一对应**的计数，用于 metrics 上报与诊断。它**不是** batch 内序列计数，而是「调度器被推进了多少步」。

#### 4.1.5 小练习与答案

**练习 1**：`Scheduler` 自己直接持有「KV 显存张量」吗？
**答**：不持有。`Scheduler` 持有的是**管理器**（`block_manager`/`block_trie`/`state_manager`），由它们去操作底层的块表与槽位；张量本身在更底层。调度器只做决策。

**练习 2**：为什么 `block_trie` 既要给 `block_manager` 又要给 `state_manager` 传引用？
**答**：`block_trie`（前缀缓存）需要联动两者——命中前缀时复用 `block_manager` 的块，SSM 场景下还要 pin/释放 `state_manager` 的检查点槽位（见 scheduler.py 顶部模块文档第 5 点）。

### 4.2 序列状态机：调度器如何「分桶」看待请求

#### 4.2.1 概念说明

调度器对序列的管理完全是**状态驱动**的。`SequenceManager` 用一个 `dict[MessageStatus, SeqMap]` 把所有序列按状态分桶，调度器在不同分支里只关心某一桶：

| 状态 | 含义 | 调度器何时看它 |
| --- | --- | --- |
| `WAITING` | 新到、尚未 prefill | `_schedule_prefill` 从这里捞人 |
| `READY` | 已 prefill、待 decode（一步） | `_schedule_decoding` 把它当 running |
| `RUNNING` | 正在 batch 中跑 | 用来算剩余名额 `max_batches - num_ready - num_running` |
| `STOPPED` | 已停止、占着 KV 可被驱逐 | 当作「可驱逐者（hanging）」 |

> 提示：调度器源码里大量出现的 `self.waiting`、`self.running`、`self.ready`、`self.hanging` **不是普通字段**，而是用 `create_status_list_property` 动态生成的属性，每次访问都现去 `seq_manager` 查对应状态桶。这样状态转移（由 `seq.state.activate/free/evict` 触发）与「能查到哪些序列」自动保持一致。

#### 4.2.2 核心流程

状态转移由 `SchedulerSequence.state` 上的方法驱动（这些方法会改 `seq_manager` 的分桶）：

```
新请求进引擎  → WAITING
prefill 准入成功 → state.activate() → READY/RUNNING
decode 每步    → 维持 RUNNING
显存不足被驱逐 → state.evict()  → 退回 WAITING（recompute 模式重算）
生成结束      → state.stop()   → STOPPED（KV 可被别人驱逐）
```

#### 4.2.3 源码精读

`MessageStatus` 枚举（注意后半段是 PD 分离迁移专用状态，本讲只看前四个）：

[messages.py:L247-L264](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/messages.py#L247-L264) — `WAITING/READY/STOPPED/RUNNING` 是基础四态；`TO_BE_MIGRATED…MIGRATION_DONE` 服务于 PD 分离（u9-l5）。

「按状态分桶」的核心数据结构与查询接口：

[messages.py:L279-L304](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/messages.py#L279-L304) — `SequenceManager`：`_status_seq_map` 是「状态 → 序列字典」的映射；`get_sequences(status)` / `num_sequences(status)` 即按桶取。

动态生成属性——这就是为什么 `self.waiting` 永远是「当前处于 WAITING 的序列列表」：

[scheduler.py:L388-L435](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/scheduler.py#L388-L435) — `create_status_list_property` / `create_num_status_method` / `create_has_status_method` 三个工厂方法，把 `waiting/ready/hanging/running` 等名字绑定到「查 seq_manager 的对应桶」。

#### 4.2.4 代码实践：看懂「动态属性」

实践目标：确认 `self.waiting` 不是缓存值。

操作步骤：

1. 在 `scheduler.py` 搜 `self.waiting =`，你会发现它**没有**出现在 `__init__` 里，而是出现在第 417 行的类体赋值（`waiting = create_status_list_property(...)`）。
2. 再搜 `num_ready` / `num_running`，确认它们同样是绑定到 `create_num_status_method` 的方法。

预期结果：每次写 `self.waiting` 都触发一次 `self.seq_manager.get_sequences(MessageStatus.WAITING)`。这意味着任何 `state.activate/evict/free` 一旦改了分桶，下一次读 `self.waiting` 立刻反映新状态——无需手动同步。

#### 4.2.5 小练习与答案

**练习**：`hanging` 对应哪个 `MessageStatus`？为什么 decode/prefill 的驱逐逻辑都把它当首选驱逐对象？
**答**：`hanging = STOPPED`（见 scheduler.py:L419）。`STOPPED` 的序列已经生成结束、KV 不再被需要（最多留给前缀缓存复用），驱逐它代价最低；而驱逐 `WAITING` 只是把还没跑的请求再往后排，也不损失已算的 KV。两者都是「驱逐它最不疼」。

### 4.3 schedule()：prefill 与 decode 的总分发

#### 4.3.1 概念说明

`schedule()` 是调度器的**唯一公开入口**。它只做一件事：根据「这一步该跑 prefill 还是 decode」把工作分派下去，再把结果统一包装成 `SchedulerOutput`。

「这一步该跑 prefill 还是 decode」这个**决策本身不在调度器里**，而在调用方 `InputsMaker`（见 `do_prefill_default()`，本讲稍后实践会用到）。调度器只接收一个已经决定好的 `is_prefill` 布尔。

#### 4.3.2 核心流程

```
schedule(is_prefill, prealloc_size, allow_long_prefill, prefer_long_prefill)
  ├─ if is_prefill:  output = _schedule_prefill(...)      # 准入新请求 / 长上下文分块
  ├─ else:           output = _schedule_decoding(...)     # 为在跑序列补块
  └─ return SchedulerOutput(running, swap_in_map, swap_out_map, copy_map)
```

`SchedulerOutput.running` 是「这一步真正要 forward 的序列列表」，是后续 `InputsMaker` 装车的依据。`swap_in/swap_out/copy_map` 服务于 KV 在 CPU/GPU 间的换入换出（本讲不展开）。

#### 4.3.3 源码精读

输出数据类——四个字段：

[scheduler.py:L67-L75](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/scheduler.py#L67-L75) — `SchedulerOutput`：`running`（本步要跑的序列）+ 三个 map。

总分发入口——本讲一切的起点：

[scheduler.py:L784-L796](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/scheduler.py#L784-L796) — `schedule()`：一行 `if is_prefill` 决定走 prefill 还是 decode，最后包成 `SchedulerOutput`。

#### 4.3.4 代码实践：定位「谁在调用 schedule」

实践目标：找到 `schedule()` 的调用方，理解决策与执行的分离。

操作步骤：

1. 全局搜 `scheduler.schedule(` 或 `.schedule(is_prefill`，定位到 `inputs_maker.py` 第 878 行附近的 `__create_inputs_prefill`。
2. 在同一文件搜 `do_prefill_default`（约 1108 行），阅读它返回 `True/False` 的几个条件。

预期结果：你会看到 `do_prefill_default()` 依据「有没有 WAITING、连续 decode 是否过多（`prefill_interval`）、等待 token 是否累计超 `max_prefill_token_num`、running 是否过少」来**决定**该不该 prefill，然后把 `is_prefill=True/False` 传进 `schedule()`。调度器只负责「执行」这个决定。

#### 4.3.5 小练习与答案

**练习**：`schedule()` 里为什么没有「是 prefill 还是 decode」的判断逻辑？
**答**：这是**关注点分离**。该不该 prefill 是「策略」（依赖工作负载、等待队列、连续 decode 次数等运行时统计），由 `InputsMaker` 的策略方法（`do_prefill_default`/`do_prefill_chunked`）决定；`schedule()` 是「机制」，只接受已定好的 `is_prefill` 并忠实分派。

### 4.4 _schedule_decoding：decode 阶段的调度

#### 4.4.1 概念说明

Decode 调度相对简单：此时 batch 里都是已经 prefill 完、正在一个 token 一个 token 蹦的序列。每个序列每步最多新增 1 个 token，因此每步只需为它**补 0 或 1 个 KV 块**（取决于有没有跨块边界）。调度器要做的是：

1. 把 `READY` 序列（=「上一步 forward 完、本步可以继续」的序列）按到达时间排序。
2. 逐个为它们申请新块；空闲块不够时，驱逐 `hanging`/`waiting`。
3. 如果驱逐后仍不够，就从 batch 尾部**抢占（preempt）**一个 running 序列——把它打回 WAITING，等下次 prefill 重算（recompute 驱逐策略）。

#### 4.4.2 核心流程

```
_schedule_decoding(prealloc_size)
  running = sorted(self.ready, by arrive_time)        # 本步要 decode 的序列
  while running:
      seq = running.pop(0)
      num_required_blocks = block_manager.num_required_blocks(seq, prealloc_size)
      while 不能驱逐到足够空闲块:
          preempt running.pop(-1)                      # 抢占最后进入的
      if 空闲块仍 < num_required_blocks:
          seq.state.evict(); continue                  # 自己被踢回 WAITING
      block_manager.allocate(seq)                      # 补块
      block_trie.allocate(seq)                         # 发布到前缀缓存
  return self.ready[:max_batches], ...                 # 最多 max_batches 条
```

#### 4.4.3 源码精读

decode 主循环（注意「先驱逐别人、不行就抢占自己人」的两层策略）：

[scheduler.py:L761-L782](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/scheduler.py#L761-L782) — `_schedule_decoding` 收尾：先尝试驱逐 `hanging+waiting` 凑块；仍不够则从 batch 末尾抢占；自己分不到块就 `state.evict()` 退回 WAITING；最后截断到 `max_batches`。

decode 的「驱逐门槛」内联函数（与 prefill 不同：它先看空闲块是否已够，够则不驱逐）：

[scheduler.py:L746-L759](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/scheduler.py#L746-L759) — `__evict_for_seq`：`num_required_blocks==0` 直接放行；小于空闲块也放行；否则才去驱逐。

「抢占」的具体动作（注意是 `pop(-1)`，即牺牲最后加入者，保住早到的）：

[scheduler.py:L769-L777](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/scheduler.py#L769-L777) — 驱逐失败时 `seq_preempted = running.pop(-1); seq_preempted.state.evict()`，把末尾序列踢出 batch。

#### 4.4.4 代码实践：追踪一次 decode 的资源需求

实践目标：理解 decode 每步「需要几个块」。

操作步骤：

1. 在 `paging/block_manager/` 下找到 `num_required_blocks(seq, num_tokens)` 的实现，确认它根据 `seq` 当前已占块数与新 token 数推算「还差几块」。
2. 在 `scheduler.py:L765` 看到调用 `self.block_manager.num_required_blocks(seq, prealloc_size)`，其中 decode 时 `prealloc_size` 通常对应「为下一步预留」。

预期结果：对一条 decode 中的序列，多数 step `num_required_blocks == 0`（新 token 还落在当前块内），偶尔 `== 1`（跨块边界）。这就是 decode 对显存的「细水长流」式需求，与 prefill 的一次性大量申请截然不同。

> 待本地验证：实际 `prealloc_size` 取决于 `engine_strategy.get_prealloc_size(...)`，可在调试时打印确认。

#### 4.4.5 小练习与答案

**练习 1**：decode 时为什么优先驱逐 `hanging+waiting`，而不是直接抢占 running？
**答**：驱逐 `hanging`（已结束、KV 可弃）和 `waiting`（还没开跑、无 KV 损失）代价最低；抢占 running 意味着把人家算了一半的 KV 扔掉、下次重算（recompute），是「最后手段」。

**练习 2**：`_schedule_decoding` 返回的 `running` 为什么是 `self.ready[:max_batches]` 而不是局部变量 `running`？
**答**：循环里对每条序列调 `block_manager.allocate` 并在成功时让序列留在 `READY` 状态；末尾再从 `self.ready` 取前 `max_batches` 条，保证既「只返回成功补块的序列」又「不超过并发上限」。

### 4.5 _schedule_prefill：准入循环与长上下文分块

#### 4.5.1 概念说明

`_schedule_prefill` 是调度器最复杂的方法，因为 prefill 要把「一整段 prompt 一次性灌进来」，资源消耗大、还要做一堆准入判断。它的核心是一个**准入循环（admission loop）**：从 `WAITING` 里按策略挑序列，逐一过「门槛」，过了就分配资源、放进 batch；过不了就跳过或停。

两个核心门槛：

1. **批容量门槛**：本批还能再装几条 = `max_batches - num_ready - num_running`。
2. **token 预算门槛**：本批累计的 prefill token 数不能超过 `max_prefill_token_num`（默认 8192）。这是实践任务的重点，下一节专门讲。

还有一条特殊路径：**长上下文分块（long context chunking）**。当某条 prompt 比一个 chunk 的上限还长时，它不能一次 prefill 完，会被切成多块，跨多个 forward step 完成。调度器用 `kv_token_limit` 标记「这次只算到第几个 token」，并且**一旦本步接了一个未完成的长 prefill，就 `break` 出循环**（不再往本批塞别的请求），因为长 prefill 已经占满预算。

#### 4.5.2 核心流程

```
_schedule_prefill(prealloc_size, allow_long_prefill, prefer_long_prefill)
  max_batches = max_batches - num_ready - num_running      # 本批剩余名额
  waiting = _reorder_waiting()                             # 按策略排序（含长 prefill 策略）
  while waiting 且 running < max_batches:
      seq = waiting.pop(0)
      gate = _check_prefill_admission_gates(seq, token_count, ...)  # ① token 预算 + 长 prefill 门槛
      若 gate 拒绝: skip（让位）或 break（停止本轮）
      block_trie.match(seq)                                # ② 前缀缓存命中（可能缩短 prompt）
      __prepare_and_evict(seq)                             # ③ 应用 chunk 限制 + 驱逐凑块
      block_manager.allocate(seq, alloc_prealloc_size)     # ④ 分配 KV 块
      block_trie.allocate(seq)                             # ⑤ 发布新块到前缀缓存
      state_manager.allocate(seq)  (SSM only)              # ⑥ 分配运行态槽位
      _finish_prefix_cache_schedule(seq)                   # ⑦ 结算命中统计
      _to_running(seq, prefill_token_count)                # ⑧ 激活、计入 token_count
      seq.record_event(SCHEDULED)
      if seq.kv_token_limit is not None: break             # 未完成的长 prefill → 独占本轮
  return running, ...
```

这个顺序与文件顶部模块文档（scheduler.py:L12-L19）描述的「成功 prefill 调度的固定顺序」一一对应：先 match、再检查驱逐与状态、再 allocate 块、再发布、最后才（SSM 由下游）恢复/保存检查点。

#### 4.5.3 源码精读

准入循环的整体框架——名额、排序、循环骨架：

[scheduler.py:L488-L525](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/scheduler.py#L488-L525) — `_schedule_prefill` 开头：算 `max_batches` 剩余名额；定义 `_to_running`、`__evict_for_seq`、`__prepare_and_evict` 三个内嵌帮手。

排序策略——长 prefill 与普通 prefill 的分流：

[scheduler.py:L603-L623](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/scheduler.py#L603-L623) — `_reorder_waiting`：默认按 `arrive_time`（FIFO）；`prefer_long_prefill` 时「挑一个长等候者 + 填满普通 prefill」；`allow_long_prefill=False`（短轮）时优先普通 prefill、长等候者靠后。

准入循环主体——七步资源决策：

[scheduler.py:L625-L728](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/scheduler.py#L625-L728) — 主 `while`：过门槛 → match 前缀 → 驱逐 → `block_manager.allocate` → `block_trie.allocate` → (SSM) `state_manager.allocate` → `_finish_prefix_cache_schedule` → `_to_running`；末尾 `if seq.kv_token_limit is not None: break` 让未完成的长 prefill 独占。

关键：`_to_running` 既激活序列、又累加 `token_count`（供下一条的预算门槛判断）：

[scheduler.py:L503-L508](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/scheduler.py#L503-L508) — `_to_running`：`seq.state.activate()` 改状态分桶，`token_count += prefill_token_count`。

长上下文分块：算「下一个 chunk 到第几个 token 结束」：

[scheduler.py:L311-L336](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/scheduler.py#L311-L336) — `_next_long_context_chunk_end`：chunk 大小 = `min(num_token_ids, max_prefill_num)`，并保证不切断多模态（图像）数据。

判断「这条 prompt 是否需要分块」：

[scheduler.py:L338-L350](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/scheduler.py#L338-L350) — `_prefill_kv_token_limit`：若 `num_token_ids <= chunk_limit` 返回 `None`（无需分块）；否则返回下一个 chunk 的结束位置。`_prefill_admission_token_count` 据此算「这条进入 batch 的真实 token 成本」。

#### 4.5.4 代码实践：手动跑通准入循环的心智模型

实践目标：用纸笔走一遍准入循环，理解 `token_count` 如何逐条累加。

操作步骤：

1. 假设 `max_prefill_token_num = 8192`，三条 WAITING 请求的 `num_token_ids` 分别为 3000、3000、4000，按 `arrive_time` 排序为 A、B、C。
2. 走循环：
   - A：`token_count = 3000 ≤ 8192`，准入。`token_count` → 3000。
   - B：`3000 + 3000 = 6000 ≤ 8192`，准入。`token_count` → 6000。
   - C：`6000 + 4000 = 10000 > 8192`，触发 token 预算门槛 → 被 skip（让位）或 break。
3. 对照源码 [scheduler.py:L625-L728](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/scheduler.py#L625-L728)，确认 C 的命运取决于 `allow_long_prefill`：短轮 skip（C 留在 WAITING，下轮再试），其它情况 break（停止本轮接纳）。

预期结果：本批只跑了 A、B，C 等下一轮。这正是 token 预算门槛把「一个超大 prompt」挡在门外、保护其余请求 TTFT 的机制。

#### 4.5.5 小练习与答案

**练习 1**：为什么接了一个「未完成的长 prefill」后要 `break`，而不是继续往 batch 里塞短请求？
**答**：一个长 prefill 的 chunk 已经占满（或接近占满）`max_prefill_token_num` 预算；继续塞会超预算。而且长 prefill 需要跨多步连续推进（`LongContextChunker` 维护状态），让它独占本轮更简单、更可预测。

**练习 2**：`_schedule_prefill` 里的 `block_trie.match(seq)` 如果失败了（没命中前缀），会怎样？
**答**：`match` 不会「失败」——它只是「命中 0 个块」的退化情况，照常推进。真正的回滚（rollback）发生在**后续资源申请失败**时（驱逐不到块、SSM 抢不到运行态），那时 `_rollback_unscheduled_prefix_match` 会撤销 match 对序列状态的副作用（见 scheduler.py:L166-L190），让序列干干净净地留到下一轮。

### 4.6 前缀缓存准入与 max_prefill_token_num（实践重点）

#### 4.6.1 概念说明

`max_prefill_token_num`（`CacheConfig` 字段，默认 8192）是 prefill 调度的**核心预算旋钮**，它在源码里同时扮演两个角色：

1. **单批 prefill 的 token 预算上限**——准入循环里，已接纳序列的 `token_count` 加上候选序列的 prefill token 数一旦超过它，候选就被挡（[scheduler.py:L254-L273](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/scheduler.py#L254-L273)）。用公式表达：

\[ \text{token\_count} + \text{prefill\_token\_count} \;\leq\; \text{max\_prefill\_token\_num} \]

2. **长上下文分块的单块上限**——`_long_context_chunk_limit` 直接返回 `max_prefill_token_num` 作为一块的大小（[scheduler.py:L301-L309](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/scheduler.py#L301-L309)）。超长 prompt 按此切块。

它的作用是**保护 TTFT（首 token 延迟）与显存**：一批 prefill 的 token 总量有上限，就不会出现「一个巨无霸 prompt 把整块 GPU 占住、其余请求干等」的局面；同时把一次 forward 的 prefill 计算量限制在可预测的范围，便于 CUDA Graph 捕获与显存规划。

注意一个精妙之处：**前缀缓存命中可以「救活」一个超预算的请求**。准入门槛在判断超预算时，会先尝试 `block_trie.match(seq)`——如果命中了一段前缀，候选的 `prefill_token_count` 会随之缩小，可能就从「超预算」变成「刚好够」。这也是为什么 match 是「试探性（tentative）」的：命中了但最终资源不够，还得能回滚。

#### 4.6.2 核心流程：门槛判断的两种结果

```
_check_prefill_admission_gates(seq, token_count, has_admitted, allow_long_prefill)
  ① 若是「未完成的长 prefill」且本短轮不允许长 prefill:
        试 match 前缀 → 仍是非末块 → reject_action = SKIP
  ② token 预算: token_count + prefill_token_count > max_prefill_token_num ?
        若超: 试 match 前缀看能否缩到预算内
              仍超 → reject_action = SKIP(短轮) / BREAK(长轮)
  返回 _PrefillGateCheck(prefix_match?, rollback_action?, reject_action?)
```

`reject_action` 取两个值：`_PREFILL_GATE_SKIP`（让位，候选回 WAITING，继续看下一位）与 `_PREFILL_GATE_BREAK`（停止本轮接纳）。短轮倾向 SKIP（尽量多塞短请求），长轮倾向 BREAK（保住当前候选）。

#### 4.6.3 源码精读

门槛检查——本节的核心：

[scheduler.py:L229-L276](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/scheduler.py#L229-L276) — `_check_prefill_admission_gates`：先处理「非末块长 prefill 在短轮」；再处理 token 预算；两处都先试 match 前缀看能否救活；仍不行才返回 `reject_action`。

两个动作常量：

[scheduler.py:L77-L78](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/scheduler.py#L77-L78) — `_PREFILL_GATE_SKIP` / `_PREFILL_GATE_BREAK`：SKIP = 让位给下一位，BREAK = 停止本轮。

长上下文分块的单块上限就是它：

[scheduler.py:L301-L309](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/scheduler.py#L301-L309) — `_long_context_chunk_limit`：`max_prefill_num = self.cache_config.max_prefill_token_num`，并保证不小于任一多模态数据的尺寸（避免切碎图像）。

默认值与配置来源：

[config.py:L118](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/config.py#L118) — `max_prefill_token_num: int = 8192`：默认 8192；实际值会被 `ExecutorBase._get_runtime_size` 按可用显存**向下调整**（见 `executor/base.py` 的运行时测算，必要时折半重试）。

SSM 运行态槽位的分配（准入循环第 ⑥ 步）：

[state_manager.py:L67-L81](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/state_manager.py#L67-L81) — `StateManager.allocate_state`/`free_state`：从同一槽位池里给「运行态」与「检查点态」分槽；运行态有独立配额上限，避免大量前缀检查点饿死活跃请求。

#### 4.6.4 代码实践：找出并解释 max_prefill_token_num 的两处作用

实践目标：完成规格要求的「阅读 `_schedule_prefill` 找出 max_prefill token 限制相关逻辑并解释其作用」。

操作步骤：

1. 在 `lmdeploy/pytorch/paging/scheduler.py` 全文搜 `max_prefill_token_num`，你会得到三处命中：
   - L255、L261、L267：都在 `_check_prefill_admission_gates`，对应**角色 1（单批 token 预算）**。
   - L303：在 `_long_context_chunk_limit`，对应**角色 2（长上下文单块上限）**。
2. 再在 `lmdeploy/pytorch/config.py` 确认默认值 `8192`（L118）。
3. （可选）在 `lmdeploy/pytorch/engine/executor/base.py` 搜 `max_prefill_token_num`，看 `_get_runtime_size` 如何在显存不足时把它折半下调（L216-L237），并打印告警。

需要观察的现象 / 预期结果：

- **角色 1（单批预算）**：`_check_prefill_admission_gates` 在「`has_admitted`（本批已有人）且累加后超 `max_prefill_token_num`」时触发，目的是**限制单次 prefill forward 的总计算量与中间 logits 显存**（`base.py` 的 runtime_cache_size 正比于 `max_prefill_token_num × vocab_size`）。它保护 TTFT——避免一个新来的大 prompt 把在跑的 decode 挤掉、让所有用户苦等。
- **角色 2（单块上限）**：`_long_context_chunk_limit` 把它当作长 prompt 的切块刀，目的是**让超出单块上限的 prompt 跨多步完成**，单步 prefill 的 token 数受控。

> 待本地验证：如果你手头有可运行的 lmdeploy，可用 `LMDEPLOY_LOG_LEVEL=DEBUG` 跑一段长 prompt（>8192 token），在日志里观察「一条 prefill 被切成多个 chunk、跨多步推进」的现象；并用 `pipeline(...).engine._engine.scheduler.scheduler_tick` 之类的调试入口（具体属性链以本地版本为准）观察 tick 增长。若无法本地运行，本实践的「源码阅读 + 心智推演」部分已自洽。

#### 4.6.5 小练习与答案

**练习 1**：把 `max_prefill_token_num` 调大（比如 32768），吞吐和 TTFT 会怎样？
**答**：单批能容纳的 prefill token 更多，长 prompt 不必切那么多块，**吞吐可能上升**；但单步 forward 计算量与中间 logits 显存（∝ `max_prefill_token_num × vocab_size`）变大，**显存压力增大、单步延迟变长**，若并发请求多，新到请求的 **TTFT 可能变差**（被大 prefill 阻塞）。这是个需要权衡的旋钮。

**练习 2**：前缀缓存命中如何让一个「超预算」的请求被收编？请用源码行为说明。
**答**：在 `_check_prefill_admission_gates` 判断超预算时（L256-L265），会调 `_try_prefix_match_for_prefill_gate` 试探性 match；命中后 `match.prefill_token_count` 缩小，若满足 `token_count + match.prefill_token_count ≤ max_prefill_token_num`（L260-L261），请求即「被救活」、带着缩水后的成本进入正常准入路径；若最终资源分配失败，则按 `rollback_action` 回滚这次试探性命中。

## 5. 综合实践：画出一次「混合 prefill + decode」的调度时序

把本讲知识串起来，完成下面这个端到端的小任务（源码阅读型 + 心智推演）。

**任务背景**：假设引擎当前 `max_batches=4`、`max_prefill_token_num=8192`、`enable_prefix_caching=True`。初始时刻有 2 条序列正在 decode（RUNNING，各占若干块），此时新到 3 条 WAITING 请求 P1/P2/P3，prompt 长度分别为 2000 / 5000 / 20000 token，且 P2 与某条历史序列共享一段 3000 token 的前缀。

**操作步骤**：

1. **决定 prefill 还是 decode**：阅读 `inputs_maker.py` 的 `do_prefill_default()`（[inputs_maker.py:L1108-L1141](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/engine/inputs_maker.py#L1108-L1141)）。因 `scheduler.has_waiting()` 为真，会返回 True，进入 prefill 分支。
2. **算剩余名额**：`max_batches - num_ready - num_running = 4 - 0 - 2 = 2`。即本轮 prefill 最多再接纳 2 条。
3. **排序**：按 `_reorder_waiting`（默认 `arrive_time` FIFO，假设 P1<P2<P3）。
4. **逐条准入**（对照 [scheduler.py:L625-L728](https://github.com/InternLM/lmdeploy/blob/b56ddfb634f069b600f0fe3f4730fe289ac7fafe/lmdeploy/pytorch/paging/scheduler.py#L625-L728)）：
   - P1：`token_count=2000 ≤ 8192`，match 无命中，驱逐凑块，allocate → 准入。`token_count=2000`。`running` 已达 2 条名额上限？注意名额是 2，目前 2 条 RUNNING decode 不算 prefill 的 running，需自己核对：本轮 prefill running 列表长度到 2 即停。
   - P2：命中 3000 前缀，实际 prefill 成本 ≈ 2000，`2000+2000=4000 ≤ 8192`，准入。
   - P3：20000 > 8192 → 触发长上下文分块，`_prefill_kv_token_limit` 返回第一个 chunk 结束位（≈ 8192）。若 `allow_long_prefill=True`，它可作为长 prefill 被接纳并 `break` 独占；但本轮名额已用完，P3 留待下一轮。
5. **画出时序图**：在纸上画出「step N：decode 2 条 + prefill P1,P2」「step N+1：decode … + P3 的第一个 chunk」「step N+2…：P3 后续 chunk」的推进过程，标注每步的 `scheduler_tick` 自增。

**预期结果**：你能用本讲术语（WAITING/READY/RUNNING、token 预算、chunk、match/rollback、preempt）解释每一条请求在每个 step 的处境。这等价于你真正读懂了 `Scheduler`。

> 待本地验证：名额计算中「decode 的 2 条 RUNNING 是否占用 prefill 名额」取决于 `num_ready + num_running` 的实时取值；请以本地调试时打印的 `scheduler.num_running()` / `num_ready()` 为准。

## 6. 本讲小结

- **持续批处理**把「加入/退出」的粒度降到每一步 forward：每步重新决定谁跑、谁退、谁加入，同一 batch 可混跑 prefill 与 decode，这是高吞吐的根源。
- **`Scheduler`** 是「决策者而非执行者」：它持 `block_manager`/`block_trie`/`state_manager` 三件 KV 家当与按状态分桶的 `seq_manager`，只产出 `SchedulerOutput.running`，不碰张量。
- **`schedule()`** 是唯一入口，按已决定好的 `is_prefill` 分派到 `_schedule_prefill` / `_schedule_decoding`；「该不该 prefill」的策略在调用方 `InputsMaker.do_prefill_*`。
- **`_schedule_decoding`** 较简单：为 READY 序列补块，先驱逐 `hanging+waiting`，仍不够则抢占 batch 末尾的 running（recompute 模式打回 WAITING 重算）。
- **`_schedule_prefill`** 是重头戏：一个准入循环，过「批容量 + token 预算 + 长 prefill」三道门槛，依固定顺序 match→驱逐→allocate→publish→（SSM）allocate state→结算命中；前缀缓存命中可缩水请求成本、救活超预算候选，但 match 是试探性的、可回滚。
- **`max_prefill_token_num`**（默认 8192）身兼二职：既是**单批 prefill 的 token 预算上限**（保护 TTFT 与显存），又是**长上下文分块的单块上限**；实际值会被 `ExecutorBase` 按显存向下测算调整。

## 7. 下一步学习建议

- **u4-l5 分块 KV 缓存与 BlockManager**：本讲反复出现的 `block_manager.allocate` / `num_required_blocks` / `num_gpu_blocks` 的物理实现就在那里，建议紧接着读，把「块」从抽象符号落到数据结构。
- **u9-l3 Prefix 缓存与 BlockTrie**：本讲的 `block_trie.match` / `allocate` / 回滚只是前缀缓存的调度侧调用，完整的命中、LRU 驱逐、命中率统计在 `block_trie.py`。
- **u9-l5 PD 分离部署**：本讲刻意略过了 `MIGRATION_*` 系列状态与 `_schedule_migration`，它们服务 prefill/decode 分离架构，等学完调度主干再去读会更顺。
- 想再深一层，可对照 vLLM 的调度器（本文件头部注明 `modify from: vllm-project/vllm`），比较两者在「抢占策略、长上下文分块、前缀缓存准入」上的取舍差异。
