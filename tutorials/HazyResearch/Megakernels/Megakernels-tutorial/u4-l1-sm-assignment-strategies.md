# SM 分配策略 rr / zz / wave / dag / pool

> 本讲对应手册单元 **U4·L1**，承接 [U3·L3：DAG、Schedule 与 ScheduleBuilder]（`u3-l3-dag-and-schedule.md`）。建议你已经理解 `DAG_Node` 的 `dependencies / children / remaining_dependencies` 三件套，以及 `Schedule` 如何把指令封装成一个可调度的整体，再进入本讲。

---

## 1. 本讲目标

学完本讲后，你应当能够：

1. 说出「把指令分配到各 SM 队列」这一步在整个 `mk`/`pyvm` 流程里的位置，以及它**为什么重要**（和 `NoOp` 填充、整层延迟的直接关系）。
2. 区分 5 种分配模式 `rr` / `zz` / `wave` / `dag` / `pool` 各自的分配逻辑，知道谁需要 `schedule`、谁需要 `memory_fraction`。
3. 读懂 `assign_dag_to_sms` 用**双堆栈**（就绪节点大顶堆 + SM 小顶堆）实现的**列表调度（list scheduling）**，并理解它「最长处理时间优先（LPT）」的贪心直觉。
4. 理解 `wave_assign_to_sms` 为什么要先按 `opcode()` 把指令聚类成「波次（wave）」再分。
5. 用一段**无需 GPU** 的最小脚本，对同一份 schedule 分别用 `mode=rr` 与 `mode=dag` 调度，量化对比各 SM 队列长度与 cost 均衡度（min/max/mean）。

---

## 2. 前置知识

### 2.1 SM（Streaming Multiprocessor）与「每 SM 一个指令队列」

GPU 里有多个 SM，Megakernels 把每层的计算拆成很多条 `Instruction`，再**静态**地把它们分配到若干个 SM 上。最终每条指令被放进「某一个 SM 的队列」里，硬件按队列顺序执行。本讲的全部内容就是：**这一步分配，应该怎么做？**

### 2.2 队列会被 NoOp 填充到等长（这是理解全部策略的钥匙）

在分配之后，`tensorize_instructions` 会把所有 SM 队列补齐到**最长那一条**的长度，缺的位置用 `NoOp` 填上：

```python
max_queue_len = max(len(queue) for queue in instruction_queues)
for queue in instruction_queues:
    queue.extend([NoOp()] * (max_queue_len - len(queue)))
```

这段来自 [megakernels/scheduler.py:287-289](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L287-L289) —— 因为指令被张量化成一个 `[num_sms, max_queue_len, 32]` 的规整张量（见下文 4.0），所以**队列必须等长**。

由此得到一个贯穿全讲的结论：

> **最长的那个 SM 队列，决定了所有 SM 要「空转」多少个 `NoOp` 槽位。** 因此分配策略的目标，不只是让「每条队列里的指令条数」尽量一样，还要让「每条队列的总 cost」尽量一样——两者共同决定整层延迟。

### 2.3 指令的三种关键属性

分配策略大量依赖指令对象上的三个方法（定义在基类 [megakernels/instructions.py:84-119](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py#L84-L119)）：

| 方法 | 含义 | 谁用它 |
| --- | --- | --- |
| `cost(globs)` | 这条指令的「执行代价」估计（时间量纲） | `dag` / `wave` 做负载均衡 |
| `opcode()` | 指令的操作码（整数） | `wave` 用它判断「是否同一类」 |
| `tags()` | 一个字典，当前只有 `"pool"` 键（`"memory"` / `"compute"`） | `pool` 用它分池 |

注意 `tags()` 默认返回空字典（[instructions.py:94-95](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py#L94-L95)），只有 **throughput** 指令集才通过 `ComputeInstruction` / `MemoryInstruction` 给出 `pool` 标签（[throughput/instructions.py:43-54](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/throughput/instructions.py#L43-L54)）。这决定了 `pool` 模式只能配 `setting=throughput` 使用（详见 4.2）。

### 2.4 Python 的 `heapq`

本讲的 `dag` / `wave` 策略都把 SM 队列组织成**最小堆**，每次取出「当前累计 cost 最小的那个 SM」。`heapq` 在 Python 里是最小堆；要模拟「最大堆」（比如让代价最大的指令优先被调度），就把 key 取负。这个技巧后面会反复出现。

### 2.5 列表调度（list scheduling）

「列表调度」是经典调度算法：维护一个「就绪集合」（所有依赖都已完成的节点），每次从中挑一个节点，放到「最早能开始执行」的资源上。挑节点的顺序（优先级）和放资源的策略，决定了调度的质量。本讲 `assign_dag_to_sms` 就是一种实现：用「最长处理时间优先（LPT, Longest Processing Time first）」作为优先级，用「最闲的 SM」作为资源选择。

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注 |
| --- | --- | --- |
| [megakernels/scheduler.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py) | **本讲主角**。定义 5 种分配函数 + 统一入口 `assign_to_sms` + 张量化 `tensorize_instructions` | 全文 |
| [megakernels/scripts/generate.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py) | 基准生成脚本 | 配置默认值 `sched="rr"` / `memory_fraction=None`，以及调用 `assign_to_sms` 的位置 |
| [megakernels/instructions.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py) | `Instruction` 基类 | `opcode()` / `tags()` / `cost()` 抽象接口 |
| [megakernels/demos/throughput/instructions.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/throughput/instructions.py) | throughput 指令集 | `ComputeInstruction` / `MemoryInstruction` 提供 `pool` 标签 |

### 3.1 `assign_to_sms` 在流程中的位置

`generate.py` 里，无论你最终用 `torch` / `pyvm` / `mk` 哪种 mode，分配都发生在分支之前：

```python
schedule = schedule_builder.build(model)
assigned_to_sms = assign_to_sms(
    config.sched, schedule=schedule, memory_fraction=config.memory_fraction
)
tensorize_instructions(schedule.globs, assigned_to_sms)
```

来自 [generate.py:139-144](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L139-L144)。而默认 `sched="rr"`、`memory_fraction=None`，定义在 [generate.py:47-49](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L47-L49)。也就是说：**不显式指定时，项目默认用最朴素的 round_robin**。

---

## 4. 核心概念与源码讲解

先放一张「五种模式一图流」的对照表，后面三节再分别展开：

| 模式 | 输入 | 分配依据 | 是否看 cost | 是否看依赖 | 典型特点 |
| --- | --- | --- | --- | --- | --- |
| `rr` | `instructions` | `i % sm_count` | 否 | 否 | 计数最均匀，cost 完全不管 |
| `zz` | `instructions` | 锯齿波 `i % (2*sm_count)` | 否 | 否 | 计数均匀，避免「相隔 n 条」落到同一 SM |
| `wave` | `schedule` | 先按 `opcode()` 聚类，再组内 LPT | 是 | 否 | 同类指令成「波次」并均衡 cost |
| `dag` | `schedule` | 双堆栈列表调度 + LPT | 是 | **是** | 唯一尊重 DAG 依赖的策略 |
| `pool` | `instructions` + `memory_fraction` | 按 `tags()["pool"]` 分池后各池 rr | 否 | 否 | 内存/计算指令分到不同 SM 子集 |

注意接口不对称：`rr`/`zz`/`pool` 只需要扁平的 `instructions` 列表；而 `wave`/`dag` 需要 `schedule`（因为要用 `globs` 算 cost、或读 DAG 结构）。这一点直接体现在统一入口 `assign_to_sms` 的 `match` 分支里（[scheduler.py:256-271](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L256-L271)）。

### 4.0 统一入口 `assign_to_sms`：模式路由

#### 4.0.1 概念说明

五种策略对应五个独立函数，`assign_to_sms` 只是一个**路由器**：根据 `mode` 字符串把调用转发给具体函数。它的价值在于：上层（`generate.py`）只和 `assign_to_sms(mode, ...)` 打交道，切换策略只需改一个字符串。

#### 4.0.2 核心流程

```text
assign_to_sms(mode, schedule=None, instructions=None, sm_count=None, memory_fraction=None)
  │
  ├─ 若给了 schedule：从中抽出 instructions 和 sm_count（说明上层可以只传 schedule）
  │
  └─ match mode:
       "rr"   → round_robin_assign_to_sms(instructions, sm_count)
       "zz"   → zig_zag_assign_to_sms(instructions, sm_count)
       "wave" → wave_assign_to_sms(schedule)          # 必须 schedule
       "dag"  → assign_dag_to_sms(schedule)           # 必须 schedule
       "pool" → pool_assign_to_sms(instructions, sm_count, memory_fraction)  # 必须 memory_fraction
       其它   → raise ValueError
```

#### 4.0.3 源码精读

[scheduler.py:245-271](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L245-L271) —— `match mode` 路由。两个要点：

1. 第 266 行 `assert memory_fraction is not None`：`pool` 是唯一**必须**额外提供 `memory_fraction` 的模式；因为默认配置里 `memory_fraction=None`（[generate.py:49](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L49)），所以直接 `sched=pool` 而不传 `memory_fraction=0.5` 之类，会在这里断言失败。
2. `wave` 和 `dag` 传的是整个 `schedule` 而非 `instructions`，因为它们要读 `globs`（算 cost）或 DAG 结构。

---

### 4.1 最朴素的两种分配：round_robin 与 zig_zag

#### 4.1.1 概念说明

最自然的想法：把指令按顺序轮流发给各 SM，第 `i` 条给第 `i mod n` 个 SM（`n` 为 SM 数）。这就是 **round_robin（轮询，`rr`）**。它只关心「让每条队列的**指令条数**尽量一样多」，完全不看 cost、不看依赖。

**zig_zag（锯齿，`zz`）** 是 round_robin 的小变体：不是一直向前轮询，而是「去而复返」地锯齿状分配。两者都能让**计数**完美均衡，但对「cost 均衡」都无能为力——这是后面三种更聪明策略要解决的问题。

#### 4.1.2 核心流程

**round_robin**：设 SM 数为 \(n\)，第 \(i\) 条指令（下标从 0 开始）落到

\[
\text{sm}(i) = i \bmod n
\]

这是一个周期为 \(n\) 的锯齿（前向）。每 \(n\) 条指令里每个 SM 恰好分到 1 条，所以**条数**严格均衡（误差最多 1）。

**zig_zag**：先算 `base_id = i mod (2n)`，再用「前一半正序、后一半倒序」映射：

\[
\text{sm}(i) =
\begin{cases}
i \bmod 2n, & i \bmod 2n < n \\
n - 1 - \big((i \bmod 2n) - n\big), & i \bmod 2n \ge n
\end{cases}
\]

以 \(n=4\) 为例，连续 8 条指令的 SM 序列是 `0,1,2,3, 3,2,1,0`，像三角波一样「上去再下来」。关键差别：在 round_robin 里，第 `i` 条和第 `i+n` 条落在**同一个** SM（`0,1,2,3,0,1,2,3`）；而 zig_zag 里它们落在**相邻但不同**的 SM。换句话说，zig_zag 让「相隔 n 条的指令」也尽量分散开，避免 wrap-around 处的重复命中。

> 诚实说明：在实际的 latency/throughput schedule 上，rr 与 zz 的**条数**都已经是均衡的，差异主要体现在「cost 不均时谁更扛得住」。真正能改善 cost 不均的是 `wave`/`dag`，所以本节我们把 rr/zz 当作「baseline」，把精力留给后面。

#### 4.1.3 源码精读

**round_robin_assign_to_sms** —— [scheduler.py:154-161](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L154-L161)：

```python
def round_robin_assign_to_sms(instructions, sm_count):
    sm_queues = [[] for _ in range(sm_count)]
    for i, instruction in enumerate(instructions):
        sm_queues[i % sm_count].append(instruction)
    return sm_queues
```

核心就一行 `sm_queues[i % sm_count].append(...)`，教科书式的轮询。

**zig_zag_assign_to_sms** —— [scheduler.py:164-175](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L164-L175)：

```python
base_id = i % (sm_count * 2)
if base_id < sm_count:
    sm_queues[base_id].append(instruction)
else:
    sm_queues[sm_count - 1 - (base_id - sm_count)].append(instruction)
```

`base_id` 在 \([0, 2n)\) 上循环；`< n` 时正序，`≥ n` 时用 `sm_count - 1 - (base_id - sm_count)` 倒序回扫，正好对应上面的三角波公式。

另外，`Schedule` 类上有两个便捷方法：[scheduler.py:59-64](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L59-L64) —— `smart_assign_to_sms()` 直接调 `assign_dag_to_sms`，`round_robin_assign_to_sms()` 调 `round_robin_assign_to_sms` 函数。命名上「smart」= dag，可窥见作者把 dag 视为「更聪明」的默认。

#### 4.1.4 代码实践：手算 rr 与 zz 的 SM 序列

**目标**：亲手验证两种映射，建立直觉。

**操作步骤**（纯纸笔，无需运行）：

1. 取 `sm_count = 4`，列出 8 条指令的下标 `i = 0..7`。
2. 对每条分别按 rr（`i % 4`）和 zz（上面的公式）算出目标 SM。
3. 检查「每 4 条里每个 SM 是否各出现 1 次」。

**预期结果**：

| i | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| rr | 0 | 1 | 2 | 3 | 0 | 1 | 2 | 3 |
| zz | 0 | 1 | 2 | 3 | 3 | 2 | 1 | 0 |

注意 `i=3` 和 `i=4`：rr 分别给 SM 3、SM 0（相邻不同）；zz 都给 SM 3（锯齿在顶点处连续命中同一 SM，这是三角波的自然结果）。两种映射都保证 8 条里每个 SM 恰好 2 条。

#### 4.1.5 小练习与答案

**Q1**：`sm_count=6`、共 13 条指令时，round_robin 下各 SM 队列长度分别是多少？

**答**：\(13 = 6 \times 2 + 1\)，所以前 \(13 \bmod 6 = 1\) 个 SM（即 SM 0）分到 3 条，其余 5 个 SM 各 2 条。条数差最大为 1。

**Q2**：把 `zig_zag` 的 `sm_count - 1 - (base_id - sm_count)` 改成 `2*sm_count - 1 - base_id` 是否等价？

**答**：等价。因为 `2n - 1 - base_id = (n-1) - (base_id - n)`，两者都是把 `base_id ∈ [n, 2n)` 反射回 `[0, n)` 的倒序映射，只是写法不同。

**Q3**：为什么说 rr/zz 对「cost 不均」束手无策？

**答**：它们的分配只依赖下标 `i` 和 `sm_count`，完全不读取 `instruction.cost(globs)`。所以若指令代价差异大（例如一条 attention 远贵于一条 norm），rr 会把这些贵指令均匀「摊」到下标上而非按 cost 均摊，导致某些 SM 队列总 cost 远高于其他。

---

### 4.2 按 opcode 聚类与按池分离：wave 与 pool

这两种策略的共同点是「先给指令分组，再在组内分配」，但分组依据完全不同：`wave` 按 `opcode()`（同类操作），`pool` 按 `tags()["pool"]`（内存型 vs 计算型）。

#### 4.2.1 概念说明

**wave（波次）** 的动机：在实际 schedule 里，指令往往是「一段相同 opcode 连在一起」出现的——比如先一串 `PreAttnLayerNorm`，再一串 `QKV`，再一串 `Attention`……同 opcode 的指令通常**共享同一个底层 megakernel、且 cost 量级相近**。`wave` 把「同 opcode 的一段连续指令」称为一个**波次（wave）**，然后在**每个波次内部**用「最闲的 SM」贪心分配。这样既保持了同类的局部性，又让每个波次内的 cost 被摊平。

**pool（分池）** 的动机：另一类不均衡来自**资源类型**。有些指令是**内存带宽受限（memory-bound）**，有些是**算力受限（compute-bound）**；如果它们挤在同一批 SM 上互相抢，效率会下降。`pool` 的做法是把 SM 切成两堆——一部分专门跑 `memory` 池指令，一部分专门跑 `compute` 池指令——让两类工作**物理隔离**，互不争抢。两堆的大小由 `memory_fraction` 控制。

#### 4.2.2 核心流程

**wave**：

```text
1. instructions = schedule.get_linear_instructions()   # 拓扑序的扁平列表
2. waves = collect_into_waves(instructions)
       遍历列表：相邻 opcode 相同就并入当前 wave，否则开新 wave
3. 对每个 wave：
     a. 按 cost 降序排序（LPT：最贵的先安排）
     b. 逐条取「累计 cost 最小的 SM」（sm_heap 小顶堆），塞进去
```

注意：wave **不跨波次**做负载均衡——它只保证「每个波次内部」cost 均衡。因为波次是按 opcode 切的，跨波次的 cost 天然不同，强行均衡反而打乱局部性。

**pool**：

```text
1. 遍历 instructions，按 tags()["pool"] 分成 memory_instructions / compute_instructions
2. mem_sms  = round(sm_count * memory_fraction)   # 内存池分到这么多 SM
   compute_sms = sm_count - mem_sms
3. 两个池各自用 round_robin 分到自己的 SM 子集
4. 返回 memory_queues + compute_queues（前 mem_sms 条是内存池，后面是计算池）
```

这里有个**重要的前置条件**：`pool` 要读 `tags()["pool"]`，但只有 throughput 指令集定义了这个标签。所以 `pool` 模式实际上只能配 `setting=throughput`；在 `setting=latency` 下用 `pool` 会在 `ins.tags()["pool"]` 处抛 `KeyError`（因为基类 `tags()` 返回空字典，[instructions.py:94-95](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py#L94-L95)）。

#### 4.2.3 源码精读

**collect_into_waves** —— [scheduler.py:178-191](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L178-L191)：

```python
for instruction in instructions:
    if cur == [] or cur[-1].opcode() == instruction.opcode():
        cur.append(instruction)
    else:
        waves.append(cur)
        cur = [instruction]
```

关键判据是 `cur[-1].opcode() == instruction.opcode()`：**只和当前波次最后一条比 opcode**。所以它是「连续同 opcode 的游程（run-length）切分」——一旦 opcode 变了就立刻开新波次，哪怕后面又出现同样的 opcode，也算新的波次。

**wave_assign_to_sms** —— [scheduler.py:194-217](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L194-L217)：

```python
sm_heap = [(0, i) for i in range(sm_count)]
heapq.heapify(sm_heap)

for wave in waves:
    sorted_by_biggest_cost = sorted(wave, key=lambda x: x.cost(globs), reverse=True)
    for ins in sorted_by_biggest_cost:
        sm_cost, sm_idx = heapq.heappop(sm_heap)   # 取最闲的 SM
        sm_cost += ins.cost(globs)
        heapq.heappush(sm_heap, (sm_cost, sm_idx)) # 放回，累计 cost 更新
        sm_queues[sm_idx].append(ins)
```

注意 `sm_heap` 是**跨波次复用**的：它一直累积每个 SM 的总 cost，所以越往后，被频繁塞过指令的 SM 越不容易再被选中。这隐式地实现了「全局」层面的均衡——哪怕每个波次只做局部 LPT。

**pool_assign_to_sms** —— [scheduler.py:220-242](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L220-L242)：

```python
for ins in instructions:
    pool = ins.tags()["pool"]
    match pool:
        case "memory":  memory_instructions.append(ins)
        case "compute": compute_instructions.append(ins)
        case _: raise ValueError(...)

mem_sms = round(sm_count * memory_fraction)
compute_sms = sm_count - mem_sms
memory_queues  = round_robin_assign_to_sms(memory_instructions, mem_sms)
compute_queues = round_robin_assign_to_sms(compute_instructions, compute_sms)
return memory_queues + compute_queues
```

两个细节：(1) 分池后**两池各自独立做 round_robin**，所以 `pool` 内部其实复用了 4.1 的 `round_robin_assign_to_sms`；(2) 返回时 `memory_queues + compute_queues` 把内存池队列接在前面、计算池接在后面，**SM 的下标含义因此改变**——前 `mem_sms` 个 SM 是内存池，后面是计算池。这一点对下游 `tensorize_instructions` 透明（它只看队列数量），但在理解硬件行为时要记得这个分界。

#### 4.2.4 代码实践：观察 wave 的切分边界

**目标**：直观看到 `collect_into_waves` 如何把一串指令切成波次。

**操作步骤**（这是**示例代码**，无需 GPU，可直接运行）：

```python
# 示例代码：模拟一组 opcode 序列，观察 wave 切分
from megakernels.scheduler import collect_into_waves
from dataclasses import dataclass

@dataclass
class FakeIns:
    op: int
    def opcode(self): return self.op
    def __repr__(self): return f"op{self.op}"

# opcode 序列：1,1,1, 2,2, 1, 3,3,3  ——注意中间又出现了 op1
seq = [FakeIns(o) for o in [1,1,1,2,2,1,3,3,3]]
waves = collect_into_waves(seq)
for i, w in enumerate(waves):
    print(f"wave {i}: {w}")
```

**需要观察的现象**：op1 出现了两次，但被切成**两个不同的波次**（wave 0 和 wave 4），因为切分只看「与上一条是否相同」。

**预期结果**：

```text
wave 0: [op1, op1, op1]
wave 1: [op2, op2]
wave 2: [op1]
wave 3: [op3, op3, op3]
```

（共 4 个波次，而不是「按 opcode 去重」后的 3 类。）若你的运行结果与此不符，请核对 `collect_into_waves` 的判据是「连续游程」而非「全局聚合」。

#### 4.2.5 小练习与答案

**Q1**：若 `memory_fraction=0.5`、`sm_count=8`，内存池和计算池各分到几个 SM？返回的 `sm_queues` 中第 3 个和第 6 个分别属于哪个池？

**答**：`mem_sms = round(8*0.5) = 4`，`compute_sms = 4`。返回列表前 4 个是内存池，后 4 个是计算池；所以第 3 个（下标 2）属内存池，第 6 个（下标 5）属计算池。

**Q2**：为什么 `wave` 只在「波次内部」做 LPT，而不对全部指令排序？

**答**：波次由 opcode 连续游程定义，对应同类、可共用 megakernel 的指令段。全局排序会打散这种同类局部性，让同一 opcode 的指令散落到各 SM、各时段，丧失 wave 想保持的「同类聚集」特性；而波次内 LPT 既保住了局部性，又均衡了该段的 cost。

**Q3**：`pool` 模式在 `setting=latency` 下直接跑会怎样？为什么？

**答**：会在 `ins.tags()["pool"]` 处抛 `KeyError`。因为 latency 指令集没有覆盖 `tags()`，基类返回空字典（`{}`），没有 `"pool"` 键；只有 throughput 指令集的 `ComputeInstruction`/`MemoryInstruction` 提供了该标签。

---

### 4.3 `assign_dag_to_sms`：双堆栈列表调度

这是 5 种策略里**唯一尊重 DAG 依赖**、也最接近「真正调度」的一个。`Schedule.smart_assign_to_sms()` 走的就是它（[scheduler.py:59-60](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L59-L60)）。

#### 4.3.1 概念说明

前四种策略都有一个共同弱点：它们把指令当成「一条条独立的活儿」往 SM 上摊，**完全忽略指令之间的依赖**。但在真实的 DAG 里，指令 B 可能依赖指令 A 的输出（比如 attention 依赖 qkv 的结果），B 在 A 完成前不能开始。

`assign_dag_to_sms` 用**列表调度（list scheduling）**解决这个问题：

- 维护一个「**就绪堆**」：当前所有依赖都已满足、可以分配的节点。从中**优先取出 cost 最大的**（LPT：先把最耗时的活儿安排掉，避免它最后才被发现、拖垮整体）。
- 维护一个「**SM 堆**」：所有 SM 的当前累计完成时间。每次**优先把节点给累计时间最小的 SM**（最闲的先干活）。
- 节点分配后，更新它子节点（children）的剩余依赖；某子节点一旦依赖清空，就进入就绪堆。

直觉上：这是「最长任务优先 + 最闲资源优先」的双向贪心，目标是最小化 **makespan**（最后一个节点完成的时刻）。makespan 有一个理论下界：

\[
\text{makespan} \ge \max\!\left(\max_{j} p_j,\ \frac{1}{n}\sum_{j} p_j\right)
\]

即「最长的单任务」和「总工作量均摊到 n 个 SM」两者取大。LPT 贪心能逼近这个下界（经典 Graham 结果：LPT 的 makespan 不超过最优解的 \(\tfrac{4}{3}-\tfrac{1}{3n}\) 倍）。

#### 4.3.2 核心流程

```text
assign_dag_to_sms(schedule):
  1. 建立反向边：每个 node register_with_parents()，让父节点知道自己的 children
  2. remaining_dependencies = 当前未完成的依赖集合（初始 = dependencies）
  3. sm_heap   = [(0, i) for i in range(sm_count)]   # 小顶堆：累计时间最小者优先
  4. ready_heap= [(-cost, idx) for 无依赖节点]        # 取负 → 大顶堆：cost 最大者优先
  5. while ready_heap 非空:
       a. 弹出就绪堆里 cost 最大的节点 node
       b. 弹出 sm_heap 里最闲的 SM（sm_time, sm_idx）
       c. start_time = sm_time      # ⚠ 见 4.3.3 的诚实说明
          end_time   = start_time + node.cost
       d. 把 node.instruction 追加到 sm_queues[sm_idx]
       e. 把 (end_time, sm_idx) 推回 sm_heap
       f. 遍历 node.children：从它们的 remaining_dependencies 里移除 node；
          若某 child 依赖清空 → 把 (-child.cost, idx) 推入 ready_heap
  6. 返回 sm_queues
```

#### 4.3.3 源码精读（含两处需要「读得仔细」的地方）

**初始化两个堆** —— [scheduler.py:109-123](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L109-L123)：

```python
sm_heap = [(0, i) for i in range(sm_count)]      # 累计完成时间最小堆
heapq.heapify(sm_heap)

ready_nodes = [n for n in nodes if len(n.dependencies) == 0]
ready_heap = []
for node in ready_nodes:
    idx = node_to_idx[node]
    ready_heap.append((-node.instruction.cost(globs), idx))  # 取负 → 最大堆
heapq.heapify(ready_heap)
```

就绪堆用 `-cost` 当 key 来模拟「最大堆」，这是 Python `heapq`（最小堆）实现 LPT 的标准手法。

**主循环** —— [scheduler.py:125-150](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L125-L150)：

```python
while ready_heap:
    ready_time, idx = heapq.heappop(ready_heap)   # 取 cost 最大的就绪节点
    node = idx_to_node[idx]
    sm_time, sm_idx = heapq.heappop(sm_heap)      # 取最闲的 SM

    # start_time = max(ready_time, sm_time)       # ← 被注释掉了！
    start_time = sm_time

    end_time = start_time + node.instruction.cost(globs)
    node.start_time = start_time
    node.end_time = end_time
    sm_queues[sm_idx].append(node.instruction)
    heapq.heappush(sm_heap, (end_time, sm_idx))

    for child in node.children:
        child.remaining_dependencies.remove(node)
        if len(child.remaining_dependencies) == 0:
            heapq.heappush(ready_heap, (-child.instruction.cost(globs), node_to_idx[child]))
```

**这里有两处必须「读得仔细」的细节，值得专门指出（不夸大、不掩饰）：**

1. **`start_time = sm_time`（第 134 行），而非 `max(ready_time, sm_time)`。** 上一行被注释掉的 `start_time = max(ready_time, sm_time)` 显示作者**曾打算**用「节点就绪时间与 SM 空闲时间的较大者」作为开始时间——这才是教科书列表调度的正确写法（要尊重「依赖完成才能开始」）。但当前生效的是 `start_time = sm_time`，**直接用 SM 的累计时间，忽略了节点本身的就绪时间**。结合就绪堆弹出的 `ready_time` 变量其实只是 `-cost`（用于排序），并不是真正的时间戳。所以在**当前代码版本**下，`dag` 模式更接近「按 cost 做 LPT 负载均衡 + 用 DAG 拓扑约束就绪顺序」，但 `end_time` 里并未严格嵌入「等依赖完成」的延迟。读源码时不要被变量名 `ready_time` 误导——它存的是 `-cost`。

2. **`priority` / `calc_priority` 机制存在但当前未启用。** `DAG_Node` 有 `priority` 字段和 `calc_priority`（[scheduler.py:29](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L29)、[scheduler.py:41-46](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L41-L46)），它能沿依赖链累加 cost 得到「关键路径优先级」。但在 `assign_dag_to_sms` 里，调用它的那行 `# schedule.end_node.calc_priority(globs)` 被**注释掉了**（[scheduler.py:104](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L104)），且就绪堆的 key 用的是 `-cost` 而非 `-priority`。也就是说，**当前版本实际用的是「单条指令 cost」做 LPT，而不是「关键路径优先级」**。`calc_priority` 是为更精细调度预留的脚手架。

把这两点和「理想列表调度」对照着读，会比单纯相信函数名更接近真相——这正是源码精读的价值。

#### 4.3.4 代码实践：rr vs dag，量化对比队列长度与 cost 均衡度

这是本讲的核心实践（对应规格里的练习任务）。我们构造一个**无需 GPU、无需模型**的最小 `Schedule`：指令 cost 故意不均，从而放大 `rr`（只看条数）与 `dag`（看 cost 做 LPT）的差异。

**目标**：对同一份 schedule 分别用 `mode=rr` 与 `mode=dag` 调度，打印各 SM 队列长度、总 cost 的 min/max/mean，直观看到 dag 在 cost 均衡上的优势。

**操作步骤**（**示例代码**，可在仓库根目录直接 `python` 运行，不需要 GPU）：

```python
# 示例代码：rr vs dag 的负载均衡对比（无 GPU 依赖）
import statistics
from dataclasses import dataclass, field
from megakernels.scheduler import (
    Schedule, DAG_Node,
    round_robin_assign_to_sms, assign_dag_to_sms,
)

class FakeGlobs:
    def sm_count(self):           # 假设 4 个 SM
        return 4

@dataclass
class FakeIns:
    _cost: float
    _opcode: int = 1
    def cost(self, globs):        # dag/wave 靠它做负载均衡
        return self._cost
    def opcode(self):
        return self._opcode
    def serialize(self):          # DAG_Node.__hash__ 会用到，必须提供
        return [self._opcode]

def build_schedule(costs):
    # 扁平 DAG：每个节点 dependencies 为空（都可立即就绪）
    nodes = [DAG_Node(instruction=FakeIns(c), dependencies=[]) for c in costs]
    return Schedule(globs=FakeGlobs(), dag_nodes=nodes, end_node=nodes[-1])

# cost 故意不均：有 1 也有 9，放大 rr 的弱点
costs = [1, 9, 2, 8, 3, 7, 4, 6, 5, 5, 1, 9]

def report(name, queues):
    lens  = [len(q) for q in queues]
    costs = [sum(ins.cost(None) for ins in q) for q in queues]
    print(f"--- {name} ---")
    print("  队列长度 :", lens, " max-min =", max(lens) - min(lens))
    print("  各队列cost:", costs)
    print(f"  cost min/max/mean = {min(costs)}/{max(costs)}/{round(statistics.mean(costs),2)}"
          f"  不均衡(max-min) = {max(costs)-min(costs)}")

# rr 只需要扁平 instructions 列表
instructions = [FakeIns(c) for c in costs]
report("rr",  round_robin_assign_to_sms(instructions, 4))
# dag 需要 schedule（每次都建新 schedule，避免节点状态被复用污染）
report("dag", assign_dag_to_sms(build_schedule(costs)))
```

**需要观察的现象**：

- 两种模式下**队列长度的 max−min**：rr 通常很小（条数被取模天然摊匀）；dag 也接近，但可能因 LPT 重排略有差异。
- 两种模式下**各队列总 cost 的 max−min（不均衡度）**：这是关键。rr 因为完全不看 cost，把 `9` 和 `1` 随下标摊，某些 SM 总 cost 会明显偏高；dag 用 LPT 把大 cost 优先喂给最闲的 SM，cost 不均衡度应显著更小。

**预期结果**（数值会随堆的 tie-break 略有不同，关键是**趋势**）：

- `rr`：各队列 cost 差异较大，max−min 可能在个位数到十位数之间（取决于 cost 分布与 SM 数）。
- `dag`：各队列 cost 非常接近，max−min 通常为 0 或 1（LPT 几乎完美均摊）。

> **待本地验证**：上面脚本的精确数字取决于 `heapq` 在 `(cost, idx)` 相等时的 tie-break 与 SM 数。请在你的机器上实际运行，记录两组「cost max−min」并填入下表对照。重点是验证 **dag 的 cost 不均衡度 ≤ rr 的 cost 不均衡度** 这一趋势。

**延伸（可选）**：把 `report` 也用在 `zig_zag_assign_to_sms(instructions, 4)` 上，观察 zz 的 cost 不均衡度是否和 rr 接近（预期：是，因为 zz 同样不看 cost）。

#### 4.3.5 小练习与答案

**Q1**：为什么就绪堆要用 `-cost` 作 key，而不是直接 `cost`？

**答**：Python `heapq` 是最小堆，直接用 `cost` 会变成「最短任务优先（SPT）」。LPT（最长处理时间优先）要求最贵的任务先调度，所以取负把最小堆「反用」成最大堆。LPT 的好处是先把耗时任务铺开，避免它们堆积在末尾拖长 makespan。

**Q2**：在当前代码里，把第 134 行的 `start_time = sm_time` 换回被注释的 `start_time = max(ready_time, sm_time)` 会正确吗？

**答**：**不会直接正确**，因为弹出的 `ready_time` 实际存的是 `-cost`（一个排序用的负数），并不是节点的真实就绪时间戳。要正确实现 `max(就绪时间, SM空闲时间)`，需要先把就绪堆的 key 改成真正的「最早可开始时间」（即 `max(所有依赖的 end_time)`，可由 `DAG_Node.earliest_ready_time` 算出，见 [scheduler.py:31-35](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L31-L35)），再与 `sm_time` 取大。这是从「LPT 负载均衡」升级到「严格时间感知列表调度」需要补的一块。

**Q3**：若所有指令 cost 完全相同，`dag` 相对 `rr` 还有优势吗？

**答**：在 cost 均匀时，LPT 退化为普通列表调度，`dag` 的 cost 均衡优势消失，结果会接近 rr（但 `dag` 仍保留「按 DAG 就绪顺序」的约束，rr 则无视依赖）。所以 `dag` 的**主要卖点**在 cost 不均 + 有依赖的场景。

---

## 5. 综合实践：五种模式同台对比

把本讲 5 种策略放到同一份 schedule 上跑一遍，汇总成一张表，串起全部知识点。

**目标**：构造一份带「不同 opcode、不同 pool、cost 不均」的合成 schedule，分别用 `rr`/`zz`/`wave`/`dag`/`pool` 调度，对每种模式报告「队列长度 max−min」和「各队列 cost max−min」，并解释数字背后的策略差异。

**操作步骤**（**示例代码**，无需 GPU）：

```python
# 示例代码：5 种 SM 分配策略同台对比
import statistics
from dataclasses import dataclass
from megakernels.scheduler import (
    Schedule, DAG_Node,
    round_robin_assign_to_sms, zig_zag_assign_to_sms,
    wave_assign_to_sms, pool_assign_to_sms, assign_dag_to_sms,
)

N_SMS = 4

class FakeGlobs:
    def sm_count(self): return N_SMS

@dataclass
class FakeIns:
    _cost: float
    _opcode: int = 1
    _pool: str = "compute"
    def cost(self, globs): return self._cost
    def opcode(self):      return self._opcode
    def tags(self):        return {"pool": self._pool}
    def serialize(self):   return [self._opcode]

def build_schedule(items):
    # items: list of (cost, opcode, pool)
    nodes = [DAG_Node(instruction=FakeIns(c, op, p), dependencies=[]) for (c, op, p) in items]
    return Schedule(globs=FakeGlobs(), dag_nodes=nodes, end_node=nodes[-1])

# opcode 在 1/2 间交替 → wave 会切成多个小波次；pool 交替 → 两个池都有活
items = []
for c, op, p in [(9,1,"compute"),(1,2,"memory"),(8,1,"compute"),(2,2,"memory"),
                 (7,1,"compute"),(3,2,"memory"),(6,1,"compute"),(4,2,"memory"),
                 (5,1,"compute"),(5,2,"memory"),(1,1,"compute"),(9,2,"memory")]:
    items.append((c, op, p))

schedule   = build_schedule(items)
instr_list = [n.instruction for n in schedule.dag_nodes]

def report(name, queues):
    lens  = [len(q) for q in queues]
    costs = [sum(ins.cost(None) for ins in q) for q in queues]
    print(f"{name:5s} | 长度={lens} 长度差={max(lens)-min(lens)}"
          f" | cost={costs} cost差={max(costs)-min(costs)}")

print(f"SM 数 = {N_SMS}, 共 {len(instr_list)} 条指令\n")
report("rr",   round_robin_assign_to_sms(instr_list, N_SMS))
report("zz",   zig_zag_assign_to_sms(instr_list, N_SMS))
report("wave", wave_assign_to_sms(schedule))
# dag 会改写节点状态，所以每次都重建一份全新的 schedule
report("dag",  assign_dag_to_sms(build_schedule(items)))
report("pool", pool_assign_to_sms(instr_list, N_SMS, memory_fraction=0.5))
```

> 注意：`dag` 那一行特意 `build_schedule(items)` **重建一份全新的 schedule**，而不是复用上面的 `schedule` 变量——因为 `assign_dag_to_sms` 会改写节点的 `start_time/end_time/remaining_dependencies`，复用同一批节点会得到错误结果。

**需要观察的现象与预期**：

| 模式 | 长度差（条数） | cost 差 | 解读 |
| --- | --- | --- | --- |
| `rr` | 小（≈0–1） | **较大** | 条数均匀，但 cost 完全不均 |
| `zz` | 小（≈0–1） | 较大 | 同 rr，不看 cost |
| `wave` | 中等 | 较小 | 每个波次内 LPT 均衡，跨波次靠 sm_heap 累积隐式均衡 |
| `dag` | 小 | **很小（≈0–1）** | 全局 LPT，cost 几乎完美均摊 |
| `pool` | 取决于两池数量比 | 两池内部各自 rr | 内存/计算隔离；注意它只返回 `mem_sms+compute_sms` 条队列 |

**思考题**（综合）：

1. 为什么 `wave` 的长度差可能比 `rr` 大？（提示：波次内 LPT 可能把多条塞给同一 SM，导致条数不再严格取模均衡。）
2. `pool` 在 `memory_fraction=0.5` 时返回几条队列？为什么它的「队列数」可能和其他模式不同？（答：`mem_sms + compute_sms = sm_count` 条，数量一致；但前半和后半的 SM 含义不同。）
3. 如果你的目标是「最小化最长队列造成的 NoOp 空转」，你会优先选哪种模式？为什么？（提示：综合「条数均衡」与「cost 均衡」，`dag` 通常是最优候选，但 `wave` 在保留 opcode 局部性上有额外好处。）

**待本地验证**：请在你的机器上运行该脚本，把 5 行真实数字填入上表，并据此回答思考题。重点是理解**每种策略均衡的是「条数」还是「cost」，以及是否尊重依赖**这三条轴。

---

## 6. 本讲小结

- Megakernels 把指令**静态**分配到各 SM 队列，随后 `tensorize_instructions` 用 `NoOp` 把所有队列补齐到最长那一条（[scheduler.py:287-289](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L287-L289)）——**最长的队列决定空转开销**，所以分配策略要同时尽量均衡「条数」和「cost」。
- 5 种模式由 `assign_to_sms`（[scheduler.py:245-271](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L245-L271)）路由；默认 `sched="rr"`（[generate.py:47](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scripts/generate.py#L47)）。`rr`/`zz`/`pool` 只吃扁平 `instructions`，`wave`/`dag` 要 `schedule`，`pool` 还必须传 `memory_fraction`。
- **rr / zz**：只看下标，计数均衡但不看 cost；zz 用三角波避免「相隔 n 条」命中同一 SM。
- **wave**：先用 `collect_into_waves` 按「连续同 opcode」切成波次，再在每个波次内 LPT 均衡 cost，`sm_heap` 跨波次累积以隐式全局均衡。
- **pool**：按 `tags()["pool"]` 把指令分成 memory/compute 两池，各池 round_robin 到按 `memory_fraction` 切分的 SM 子集；**仅 throughput 指令集可用**。
- **dag**：唯一的「真调度」——双堆栈列表调度，就绪堆按 `-cost`（LPT）选节点、SM 堆按累计时间选最闲资源，并按 `remaining_dependencies` 尊重 DAG 拓扑。读源码时注意：当前版本 `start_time = sm_time`（未用就绪时间），且 `calc_priority`/`priority` 关键路径机制被注释、未启用——实际就绪顺序由单条指令 cost 决定。

---

## 7. 下一步学习建议

- **下一讲 U4·L2**（`u4-l2-...`）会讲分配之后的 `tensorize_instructions` 与 `serialize_and_pad`：理解指令如何被序列化成 `[num_sms, queue_len, 32]` 的张量、`NoOp` 如何填充、`timings` 张量为何按 `[num_sms, max_queue_len, 128]` 分配。本讲的「队列长度/cost 均衡」会直接对应到那里的「填充开销」与「时序槽位」。
- **继续读源码**：
  - 把 `assign_dag_to_sms` 的两处「读得仔细」（`start_time`、`calc_priority`）当作小练习——尝试把就绪堆 key 改成真正的 `earliest_ready_time`（[scheduler.py:31-35](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L31-L35)），观察 makespan 变化。
  - 对照 [megakernels/demos/throughput/scheduler.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/throughput/scheduler.py) 与 [megakernels/demos/latency/scheduler.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py)，看两个 setting 各自的 DAG 结构如何影响 `dag` 模式的就绪顺序。
- **动手实验**（需 GPU）：用 `generate.py mode=mk sched=rr` 与 `sched=dag` 跑同一 prompt，对比 `Tokens per second`，验证「cost 更均衡 → 整层延迟更低」的因果关系（无 GPU 则记为「待本地验证」，并把重心放在本讲的无 GPU 脚本对比上）。
