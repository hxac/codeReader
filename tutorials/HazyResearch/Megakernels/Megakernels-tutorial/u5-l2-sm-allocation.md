# U5-L2: SM 分配算法

在 Megakernels 中，当大量指令需要在多个 Streaming Multiprocessor (SM) 上执行时，如何高效地分配这些指令是一个关键问题。不同的分配策略会显著影响 GPU 的利用率和整体性能。

本讲义将分析 Megakernels 中的五种 SM 分配策略：轮询、Wave、DAG 感知、Pool 模式，以及指令序列化机制。

---

## 最小模块 1：轮询分配

### 概念说明

轮询分配是最简单的指令分配策略。当有 \(N\) 个 SM 和 \(M\) 条指令时，第 \(i\) 条指令被分配到 SM \((i \mod N)\) 上。

**为什么需要它：**
- 实现简单，易于理解和调试
- 提供基本的负载均衡
- 适用于指令执行时间相近的场景

### 伪代码或流程

```python
def round_robin_assign(instructions, sm_count):
    sm_queues = [[] for _ in range(sm_count)]
    for i, instruction in enumerate(instructions):
        sm_queues[i % sm_count].append(instruction)
    return sm_queues
```

**流程：**
1. 为每个 SM 创建一个空队列
2. 按顺序遍历所有指令
3. 将第 \(i\) 条指令放入第 \((i \mod N)\) 个 SM 的队列

### 原理分析

轮询分配的核心思想是**均匀分布**。假设有 \(N\) 个 SM 和 \(M\) 条指令：

- 每个 SM 分配到 \(\lfloor M/N \rfloor\) 或 \(\lceil M/N \rceil\) 条指令
- 最大队列长度与最小队列长度的差不超过 1

**数学描述：**
设 \(q_i\) 为第 \(i\) 个 SM 的队列长度，则：
\[
|q_i - q_j| \leq 1, \quad \forall i, j \in \{0, 1, \ldots, N-1\}
\]

**局限性：**
- 不考虑指令的执行时间差异
- 不考虑指令之间的依赖关系
- 可能导致某些 SM 过载而其他 SM 空闲

### 代码实践

轮询分配的实现位于 `megakernels/scheduler.py:154-161`：

[https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L154-L161](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L154-L161)

```python
def round_robin_assign_to_sms(
    instructions: list[Instruction], sm_count: int
) -> list[list[Instruction]]:
    sm_queues = [[] for _ in range(sm_count)]
    for i, instruction in enumerate(instructions):
        sm_queues[i % sm_count].append(instruction)

    return sm_queues
```

**关键代码说明：**
- 第 157 行：为每个 SM 创建一个空队列
- 第 159 行：使用模运算 `i % sm_count` 实现轮询分配
- 返回一个列表的列表，每个内部列表代表一个 SM 的指令队列

### 练习题

1. 假设有 4 个 SM 和 10 条指令，使用轮询分配后每个 SM 的队列长度是多少？

2. 轮询分配在什么情况下会表现良好？什么情况下会表现不佳？

3. 如何修改轮询分配算法，使其支持反向轮询（即先从最后一个 SM 开始分配）？

4. 轮询分配是否保证了指令的原始顺序在每个 SM 队列中？为什么？

### 答案

1. **答案：**
   - SM 0: 3 条指令（索引 0, 4, 8）
   - SM 1: 3 条指令（索引 1, 5, 9）
   - SM 2: 2 条指令（索引 2, 6）
   - SM 3: 2 条指令（索引 3, 7）

2. **答案：**
   - **表现良好：** 当所有指令的执行时间相近时，轮询分配能提供良好的负载均衡
   - **表现不佳：** 当指令执行时间差异很大时，某些 SM 可能会过载而其他 SM 空闲

3. **答案：**
   ```python
   def reverse_round_robin_assign(instructions, sm_count):
       sm_queues = [[] for _ in range(sm_count)]
       for i, instruction in enumerate(instructions):
           sm_idx = (sm_count - 1 - (i % sm_count))  # 反向分配
           sm_queues[sm_idx].append(instruction)
       return sm_queues
   ```

4. **答案：**
   是的，轮询分配保证了指令的原始顺序在每个 SM 队列中。因为指令是按顺序遍历的，并且每条指令只被添加到一个队列中，所以每个队列内的指令保持原始顺序。

---

## 最小模块 2：Wave 分配

### 概念说明

Wave 分配是一种基于指令类型的分组策略。它将连续的、具有相同操作码（opcode）的指令组织成一个 "wave"，然后将每个 wave 内的指令按代价排序并分配给最空闲的 SM。

**为什么需要它：**
- 相同操作码的指令通常有相似的执行特性
- 通过分组可以减少 SM 间的上下文切换
- 按代价排序可以优化负载均衡

### 伪代码或流程

```python
def wave_assign(schedule):
    instructions = schedule.get_linear_instructions()
    waves = collect_into_waves(instructions)
    sm_count = schedule.globs.sm_count()
    sm_queues = [[] for _ in range(sm_count)]
    sm_heap = [(0, i) for i in range(sm_count)]  # (可用时间, SM索引)
    heapq.heapify(sm_heap)

    for wave in waves:
        # 按代价降序排序
        sorted_wave = sorted(wave, key=lambda x: x.cost(), reverse=True)
        for ins in sorted_wave:
            # 获取最空闲的 SM
            sm_time, sm_idx = heapq.heappop(sm_heap)
            sm_time += ins.cost()
            heapq.heappush(sm_heap, (sm_time, sm_idx))
            sm_queues[sm_idx].append(ins)

    return sm_queues
```

**流程：**
1. 将指令序列按操作码分组形成 waves
2. 为每个 SM 创建一个最小堆，记录其可用时间
3. 对每个 wave：
   - 按代价降序排序指令
   - 将每条指令分配给当前最空闲的 SM

### 原理分析

Wave 分包含两个关键优化：

**1. Wave 分组：**
将连续的相同操作码指令分组，利用了指令的局部性原理：
\[
\text{if } \text{opcode}(ins_i) = \text{opcode}(ins_{i+1}) \Rightarrow ins_i, ins_{i+1} \in \text{same\_wave}
\]

**2. 代价感知的负载均衡：**
使用最小堆维护 SM 的可用时间，每次选择最空闲的 SM：
\[
\text{sm\_selected} = \arg\min_{i} \{\text{available\_time}_i\}
\]

然后将指令的执行时间加到该 SM 的可用时间上：
\[
\text{available\_time}_{\text{sm\_selected}} \leftarrow \text{available\_time}_{\text{sm\_selected}} + \text{cost}(ins)
\]

**优势：**
- 相比轮询，考虑了指令的执行时间
- 通过 wave 分组减少了上下文切换
- 使用贪心策略保证局部最优

### 代码实践

Wave 分配的实现位于 `megakernels/scheduler.py:178-217`：

**Wave 收集：**
[https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L178-L192](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L178-L192)

```python
def collect_into_waves(instructions: list[Instruction]):
    waves: list[list[Instruction]] = []
    cur = []
    for instruction in instructions:
        if cur == [] or cur[-1].opcode() == instruction.opcode():
            cur.append(instruction)
        else:
            waves.append(cur)
            cur = [instruction]

    if len(cur) > 0:
        waves.append(cur)

    return waves
```

**Wave 分配：**
[https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L194-L217](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L194-L217)

```python
def wave_assign_to_sms(
    schedule: Schedule,
) -> list[list[Instruction]]:
    instructions = schedule.get_linear_instructions()
    globs = schedule.globs
    sm_count = globs.sm_count()

    waves = collect_into_waves(instructions)

    sm_queues = [[] for _ in range(sm_count)]

    sm_heap = [(0, i) for i in range(sm_count)]
    heapq.heapify(sm_heap)

    for wave in waves:
        sorted_by_biggest_cost = sorted(wave, key=lambda x: x.cost(globs), reverse=True)

        for ins in sorted_by_biggest_cost:
            sm_cost, sm_idx = heapq.heappop(sm_heap)
            sm_cost += ins.cost(globs)
            heapq.heappush(sm_heap, (sm_cost, sm_idx))
            sm_queues[sm_idx].append(ins)

    return sm_queues
```

**关键代码说明：**
- 第 182-183 行：如果当前 wave 为空或指令操作码相同，则加入当前 wave
- 第 185-186 行：否则，结束当前 wave 并开始新 wave
- 第 209 行：按代价降序排序指令
- 第 212-215 行：使用最小堆选择最空闲的 SM，并更新其可用时间

### 练习题

1. 给定指令序列：\[A(opcode=1, cost=5), B(opcode=1, cost=3), C(opcode=2, cost=8), D(opcode=2, cost=2)\]，使用 wave 分配和 2 个 SM，最终的分配结果是什么？

2. Wave 分配中为什么使用代价降序排序而不是升序？

3. 如果指令序列中没有连续的相同操作码，wave 分配的效果如何？

4. Wave 分配和轮询分配的主要区别是什么？

### 答案

1. **答案：**
   - Waves: \[\[A, B\], \[C, D\]\]
   - Wave 1 按代价排序: \[A(5), B(3)\]
   - Wave 2 按代价排序: \[C(8), D(2)\]
   - 分配过程：
     - A(5) → SM0 (time=5)
     - B(3) → SM1 (time=3)
     - C(8) → SM1 (time=3+8=11)
     - D(2) → SM0 (time=5+2=7)
   - 最终：SM0: \[A, D\], SM1: \[B, C\]

2. **答案：**
   使用代价降序排序可以优先处理高代价指令。这样可以更早地将高代价指令分配给最空闲的 SM，避免后续高代价指令集中在已经繁忙的 SM 上，从而改善负载均衡。

3. **答案：**
   如果没有连续的相同操作码，每个 wave 只包含一条指令。此时 wave 分配退化为一种代价感知的轮询分配，仍然能通过最小堆优化负载均衡，但失去了 wave 分组带来的优势。

4. **答案：**
   - Wave 分配考虑了指令的执行时间和类型（操作码），使用最小堆进行代价感知的分配
   - 轮询分配只考虑指令的顺序，不考虑执行时间和类型
   - Wave 分配通常能提供更好的负载均衡和更少的上下文切换

---

## 最小模块 3：DAG 感知分配

### 概念说明

DAG 感知分配是最复杂的分配策略，它考虑指令之间的依赖关系。指令被组织成有向无环图（DAG），其中边表示依赖关系。调度器确保只有当指令的所有依赖都完成后，才开始执行该指令。

**为什么需要它：**
- 许多计算任务中，指令之间存在依赖关系
- 通过并行执行无依赖的指令，可以提高整体性能
- 避免执行未准备好（依赖未满足）的指令

### 伪代码或流程

```python
def assign_dag_to_sms(schedule):
    nodes = schedule.dag_nodes  # DAG 节点列表
    sm_count = schedule.globs.sm_count()
    sm_queues = [[] for _ in range(sm_count)]
    sm_heap = [(0, i) for i in range(sm_count)]  # (可用时间, SM索引)
    heapq.heapify(sm_heap)

    # 初始化依赖关系
    for node in nodes:
        node.register_with_parents()
        node.remaining_dependencies = set(node.dependencies)

    # 找到所有无依赖的节点
    ready_nodes = [n for n in nodes if len(n.dependencies) == 0]
    ready_heap = []
    for node in ready_nodes:
        idx = node_to_idx[node]
        heapq.heappush(ready_heap, (-node.cost(), idx))  # 按代价排序，高代价优先

    # 调度循环
    while ready_heap:
        _, node_idx = heapq.heappop(ready_heap)
        node = idx_to_node[node_idx]

        # 获取最空闲的 SM
        sm_time, sm_idx = heapq.heappop(sm_heap)
        start_time = sm_time
        end_time = start_time + node.cost()

        node.start_time = start_time
        node.end_time = end_time
        sm_queues[sm_idx].append(node.instruction)
        heapq.heappush(sm_heap, (end_time, sm_idx))

        # 更新子节点的依赖状态
        for child in node.children:
            child.remaining_dependencies.remove(node)
            if len(child.remaining_dependencies) == 0:
                idx = node_to_idx[child]
                heapq.heappush(ready_heap, (-child.cost(), idx))

    return sm_queues
```

**流程：**
1. 初始化每个节点的依赖关系
2. 找到所有无依赖的节点（就绪节点）
3. 使用两个堆：
   - 就绪堆：按代价排序的就绪节点
   - SM 堆：按可用时间排序的 SM
4. 循环调度：
   - 从就绪堆中取出最高代价的节点
   - 从 SM 堆中取出最空闲的 SM
   - 分配指令并更新时间
   - 检查子节点的依赖是否满足

### 原理分析

DAG 感知分配的核心是**拓扑排序 + 贪心调度**：

**1. DAG 结构：**
每个节点表示一个指令，边表示依赖关系：
\[
u \rightarrow v \iff v \text{ 依赖于 } u
\]

**2. 就绪条件：**
一个指令可以执行当且仅当其所有依赖都已完成：
\[
\text{ready}(v) \iff \forall u \in \text{dependencies}(v), \text{completed}(u) = \text{true}
\]

**3. 调度策略：**
使用贪心策略，每次选择：
\[
\text{node\_selected} = \arg\max_{n \in \text{ready}} \{\text{cost}(n)\}
\]
\[
\text{sm\_selected} = \arg\min_{i} \{\text{available\_time}_i\}
\]

**4. 时间计算：**
指令的开始时间和结束时间：
\[
\text{start\_time} = \max(\text{earliest\_ready\_time}, \text{sm\_available\_time})
\]
\[
\text{end\_time} = \text{start\_time} + \text{cost}(\text{instruction})
\]

**优势：**
- 充分利用指令级并行性
- 避免依赖冲突
- 通过优先执行高代价指令优化关键路径

### 代码实践

DAG 分配的实现位于 `megakernels/scheduler.py:94-151`：

[https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L94-L151](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L94-L151)

```python
def assign_dag_to_sms(schedule: Schedule) -> list[list[Instruction]]:
    nodes = schedule.dag_nodes
    globs = schedule.globs

    for node in nodes:
        node.register_with_parents()

    for node in nodes:
        node.remaining_dependencies = set(node.dependencies)

    sm_count = globs.sm_count()
    sm_queues = [[] for _ in range(sm_count)]

    sm_heap = [(0, i) for i in range(sm_count)]
    heapq.heapify(sm_heap)

    ready_nodes = [n for n in nodes if len(n.dependencies) == 0]

    idx_to_node = {i: n for i, n in enumerate(nodes)}
    node_to_idx = {n: i for i, n in enumerate(nodes)}

    ready_heap = []
    for node in ready_nodes:
        idx = node_to_idx[node]
        ready_heap.append((-node.instruction.cost(globs), idx))

    heapq.heapify(ready_heap)

    while ready_heap:
        ready_time, idx = heapq.heappop(ready_heap)
        node = idx_to_node[idx]

        sm_time, sm_idx = heapq.heappop(sm_heap)

        start_time = sm_time

        end_time = start_time + node.instruction.cost(globs)

        node.start_time = start_time
        node.end_time = end_time

        sm_queues[sm_idx].append(node.instruction)

        heapq.heappush(sm_heap, (end_time, sm_idx))

        for child in node.children:
            child.remaining_dependencies.remove(node)
            if len(child.remaining_dependencies) == 0:
                idx = node_to_idx[child]
                heapq.heappush(ready_heap, (-child.instruction.cost(globs), idx))

    return sm_queues
```

**关键代码说明：**
- 第 98-99 行：注册父子关系
- 第 101-102 行：初始化剩余依赖集合
- 第 109 行：创建 SM 可用时间最小堆
- 第 112 行：找到所有无依赖的节点（初始就绪节点）
- 第 121 行：创建就绪节点堆，按代价排序（负号实现最大堆）
- 第 125-150 行：主调度循环
- 第 145-149 行：更新子节点的依赖状态，将新就绪的节点加入堆

### 练习题

1. 给定 DAG：A → C, B → C, C → D，代价分别为 A=5, B=3, C=8, D=2，使用 DAG 感知分配和 2 个 SM，求每个指令的开始时间和结束时间？

2. DAG 感知分配中，为什么在就绪堆中使用负号（`-node.instruction.cost(globs)`）？

3. 如果 DAG 中存在环（循环依赖），DAG 感知分配会是什么情况？如何检测环？

4. DAG 感知分配和 Wave 分配的主要区别是什么？

### 答案

1. **答案：**
   - 初始就绪节点：A(5), B(3)
   - 调度过程：
     - A(5) → SM0 (start=0, end=5)
     - B(3) → SM1 (start=0, end=3)
     - C 的依赖满足（A,B 完成），C 就绪
     - C(8) → SM1 (start=3, end=11)（SM1 更早空闲）
     - D 的依赖满足（C 完成），D 就绪
     - D(2) → SM0 (start=5, end=7)
   - 最终时间：A(0-5), B(0-3), C(3-11), D(5-7)

2. **答案：**
   Python 的 `heapq` 模块只实现最小堆。通过使用负号，可以将最大堆问题转换为最小堆问题：`heapq.heappop(ready_heap)` 会返回代价最大的节点（因为 `-cost` 最小）。

3. **答案：**
   如果 DAG 中存在环，部分节点永远不会就绪（因为其依赖永远无法满足），导致算法陷入死循环或无法完成调度。可以通过以下方式检测环：
   - 维护一个计数器，记录已调度的节点数
   - 如果计数器超过节点总数，则存在环
   - 或使用拓扑排序算法检测环（如 Kahn 算法）

4. **答案：**
   - DAG 感知分配考虑指令之间的依赖关系，只调度依赖已满足的指令
   - Wave 分配不考虑依赖关系，按指令序列的顺序和操作码分组调度
   - DAG 感知分配更适用于有依赖关系的任务，Wave 分配更适用于独立的任务序列

---

## 最小模块 4：Pool 分配

### 概念说明

Pool 分配是一种基于资源类型的分配策略。它将指令分为不同的 "pool"（如内存池和计算池），然后为每个 pool 分配固定比例的 SM。每个 pool 内部使用轮询分配。

**为什么需要它：**
- 不同类型的指令（如内存操作和计算操作）有不同的资源需求
- 通过资源隔离可以减少资源竞争
- 便于针对性优化不同类型的指令

### 伪代码或流程

```python
def pool_assign_to_sms(instructions, sm_count, memory_fraction):
    memory_instructions = []
    compute_instructions = []

    # 按标签分类指令
    for ins in instructions:
        pool = ins.tags()["pool"]
        match pool:
            case "memory":
                memory_instructions.append(ins)
            case "compute":
                compute_instructions.append(ins)
            case _:
                raise ValueError(f"Unknown pool: {pool}")

    # 计算每个 pool 的 SM 数量
    mem_sms = round(sm_count * memory_fraction)
    compute_sms = sm_count - mem_sms

    # 每个 pool 内部使用轮询分配
    memory_queues = round_robin_assign_to_sms(memory_instructions, mem_sms)
    compute_queues = round_robin_assign_to_sms(compute_instructions, compute_sms)

    return memory_queues + compute_queues
```

**流程：**
1. 按指令标签（tags）分类指令
2. 根据比例计算每个 pool 分配的 SM 数量
3. 每个 pool 内部使用轮询分配
4. 合并所有 pool 的队列

### 原理分析

Pool 分配的核心思想是**资源隔离和分类管理**：

**1. Pool 分类：**
根据指令的标签（`tags()` 方法）将指令分配到不同的 pool：
\[
\text{pool}(ins) = \text{ins.tags()}["\text{pool}"]
\]

常见的 pool 类型：
- `"memory"`：内存密集型指令
- `"compute"`：计算密集型指令

**2. SM 分配：**
假设总 SM 数为 \(N\)，内存 pool 的比例为 \(f\)，则：
\[
N_{\text{memory}} = \lfloor N \cdot f \rceil
\]
\[
N_{\text{compute}} = N - N_{\text{memory}}
\]

**3. 内部调度：**
每个 pool 内部使用简单的轮询分配（或其它策略），实现简单高效。

**优势：**
- 资源隔离，减少不同类型指令间的干扰
- 可以针对不同 pool 优化（如内存池优化带宽，计算池优化算力）
- 易于扩展新的 pool 类型

**局限性：**
- 需要预先知道指令的类型特征
- 固定的 SM 比例可能不是最优的
- 如果某些 pool 的指令很少，可能导致 SM 空闲

### 代码实践

Pool 分配的实现位于 `megakernels/scheduler.py:220-242`：

[https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L220-L242](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L220-L242)

```python
def pool_assign_to_sms(
    instructions: list[Instruction], sm_count: int, memory_fraction: float
) -> list[list[Instruction]]:
    memory_instructions = []
    compute_instructions = []

    for ins in instructions:
        pool = ins.tags()["pool"]
        match pool:
            case "memory":
                memory_instructions.append(ins)
            case "compute":
                compute_instructions.append(ins)
            case _:
                raise ValueError(f"Unknown pool: {pool}")

    mem_sms = round(sm_count * memory_fraction)
    compute_sms = sm_count - mem_sms

    memory_queues = round_robin_assign_to_sms(memory_instructions, mem_sms)
    compute_queues = round_robin_assign_to_sms(compute_instructions, compute_sms)

    return memory_queues + compute_queues
```

**关键代码说明：**
- 第 223-234 行：按指令标签分类到 memory 或 compute pool
- 第 236-237 行：根据比例计算每个 pool 的 SM 数量
- 第 239-240 行：每个 pool 内部使用轮询分配
- 第 242 行：合并所有队列

**指令标签示例：**
在 `megakernels/demos/throughput/instructions.py` 中：
[https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/throughput/instructions.py#L43-L54](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/throughput/instructions.py#L43-L54)

```python
@dataclass
class ComputeInstruction(Instruction):
    @classmethod
    def tags(cls):
        return {"pool": "compute"}

@dataclass
class MemoryInstruction(Instruction):
    @classmethod
    def tags(cls):
        return {"pool": "memory"}
```

### 练习题

1. 假设有 8 个 SM，`memory_fraction=0.3`，30 条内存指令，50 条计算指令，使用 pool 分配后每个 SM 的队列长度是多少？

2. Pool 分配中，为什么使用 `round()` 而不是 `int()` 或 `math.floor()` 来计算 SM 数量？

3. 如果指令的 `tags()` 方法没有返回 `"pool"` 键，会发生什么？如何改进代码使其更健壮？

4. Pool 分配和 DAG 感知分配能否结合使用？如果可以，应该如何设计？

### 答案

1. **答案：**
   - Memory SM 数：`round(8 * 0.3) = round(2.4) = 2`
   - Compute SM 数：`8 - 2 = 6`
   - Memory 队列长度：30 / 2 = 15（每个 SM 15 条）
   - Compute 队列长度：50 / 6 ≈ 8.33（SM 0-2: 9 条，SM 3-5: 8 条）

2. **答案：**
   使用 `round()` 可以四舍五入，比 `int()` 或 `math.floor()` 更接近目标比例。例如，8 个 SM 的 30% 应该是 2.4，`round(2.4) = 2` 比 `int(2.4) = 2` 更接近 30%（虽然在这个例子中结果相同，但在其他情况下会有差异）。

3. **答案：**
   如果 `tags()` 方法没有返回 `"pool"` 键，`ins.tags()["pool"]` 会抛出 `KeyError`。改进方法：
   ```python
   pool = ins.tags().get("pool", "compute")  # 默认为 compute
   # 或
   if "pool" not in ins.tags():
       raise ValueError(f"Instruction {ins} missing pool tag")
   ```

4. **答案：**
   可以结合使用。设计思路：
   - 先使用 DAG 感知调度确定指令的执行顺序和时间
   - 然后根据指令的 pool 类型，在 DAG 调度结果的基础上进行 pool 分配
   - 或者在 DAG 调度时，考虑 pool 限制，只在对应 pool 的 SM 上调度指令
   - 这样既能保证依赖关系，又能实现资源隔离

---

## 最小模块 5：指令序列化

### 概念说明

指令序列化是将指令对象转换为固定大小的整数数组的过程，以便在 GPU 上执行。每条指令被序列化为一个整数数组，不足的部分用 0 填充。

**为什么需要它：**
- GPU 需要统一格式的指令表示
- 固定大小的指令便于内存管理和索引
- 填充确保所有指令占用相同的空间，简化硬件设计

### 伪代码或流程

```python
def serialize_and_pad(instruction):
    serialized = instruction.serialize()  # 指令自序列化
    num_padding = INTS_PER_INSTRUCTION - len(serialized)
    assert num_padding >= 0
    return serialized + [0] * num_padding

def tensorize_instructions(globs, instruction_queues):
    num_sms = globs.sm_count()

    # 填充 NoOp 使所有队列长度相等
    max_queue_len = max(len(queue) for queue in instruction_queues)
    for queue in instruction_queues:
        queue.extend([NoOp()] * (max_queue_len - len(queue)))

    # 序列化并展平
    flattened = []
    for queue in instruction_queues:
        for instruction in queue:
            flattened.extend(serialize_and_pad(instruction))

    # 转换为张量
    device = globs.device
    serialized = torch.tensor(flattened, dtype=torch.int32, device=device).view(
        num_sms, -1, INTS_PER_INSTRUCTION
    )

    # 创建时序张量
    timings = torch.zeros(
        [num_sms, max_queue_len, TIMING_SLOTS],
        dtype=torch.int32,
        device=device,
    )

    globs.instructions = serialized
    globs.timings = timings
```

**流程：**
1. 序列化每条指令并填充到固定长度
2. 用 NoOp 填充使所有 SM 的队列长度相等
3. 将所有指令序列展平为一维数组
4. 转换为 3D 张量：`[num_sms, max_queue_len, INTS_PER_INSTRUCTION]`
5. 创建时序张量用于记录执行时间

### 原理分析

指令序列化的核心是**统一表示和内存布局**：

**1. 指令序列化：**
每条指令通过 `serialize()` 方法转换为整数数组：
\[
\text{serialized} = \text{instruction.serialize()} = [opcode, arg_1, arg_2, \ldots]
\]

**2. 填充：**
将指令数组填充到固定长度 `INTS_PER_INSTRUCTION`：
\[
\text{padded} = \text{serialized} \cup \{0\}^{(\text{INTS\_PER\_INSTRUCTION} - |\text{serialized}|)}
\]

**3. 张量化：**
将所有指令组织成 3D 张量：
\[
\text{instructions}[i][j][k] = \text{第 } i \text{ 个 SM 的第 } j \text{ 条指令的第 } k \text{ 个整数}
\]

**4. 时序管理：**
创建时序张量记录每条指令在每个 SM 上的执行时间：
\[
\text{timings}[i][j][t] = \text{第 } i \text{ 个 SM 的第 } j \text{ 条指令在时序槽 } t \text{ 的状态}
\]

**优势：**
- 统一的指令格式便于 GPU 解析和执行
- 固定大小简化内存管理和索引计算
- 时序张量支持精确的时间控制和同步

### 代码实践

指令序列化的实现位于 `megakernels/scheduler.py:274-308`：

**序列化和填充：**
[https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L274-L278](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L274-L278)

```python
def serialize_and_pad(instruction: Instruction):
    serialized = instruction.serialize()
    num_padding = INTS_PER_INSTRUCTION - len(serialized)
    assert num_padding >= 0
    return serialized + [0] * num_padding
```

**张量化：**
[https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L281-L308](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L281-L308)

```python
def tensorize_instructions(
    globs: BaseGlobals,
    instruction_queues: list[list[Instruction]],
):
    num_sms = globs.sm_count()

    max_queue_len = max(len(queue) for queue in instruction_queues)
    for queue in instruction_queues:
        queue.extend([NoOp()] * (max_queue_len - len(queue)))

    flattened = []
    for queue in instruction_queues:
        flattened.extend(serialize_and_pad(instruction) for instruction in queue)

    device = globs.device

    serialized = torch.tensor(flattened, dtype=torch.int32, device=device).view(
        num_sms, -1, INTS_PER_INSTRUCTION
    )

    timings = torch.zeros(
        [num_sms, max_queue_len, TIMING_SLOTS],
        dtype=torch.int32,
        device=device,
    )

    globs.instructions = serialized
    globs.timings = timings
```

**指令序列化示例：**
在 `megakernels/instructions.py` 中：
[https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py#L97-L119](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py#L97-L119)

```python
def serialize(self):
    words = [self.opcode()]
    for field in fields(self):
        name = field.name
        if name == "global_idx":
            continue
        attr = getattr(self, name)

        if isinstance(attr, int):
            words.append(attr)
        elif isinstance(attr, tuple):
            words.append(len(attr))
            words.extend(attr)
        elif isinstance(attr, list):
            words.append(len(attr))
            words.extend(attr)
        elif attr is None:
            words.append(0)
        else:
            raise ValueError(f"Unsupported field type: {attr}")

    return words
```

**关键代码说明：**
- 第 275 行：调用指令的 `serialize()` 方法获取整数数组
- 第 276-278 行：计算需要填充的 0 的数量，并填充
- 第 287-289 行：用 NoOp 填充使所有队列长度相等
- 第 291-293 行：序列化所有指令并展平为一维数组
- 第 297-299 行：转换为 3D 张量，形状为 `[num_sms, max_queue_len, INTS_PER_INSTRUCTION]`
- 第 301-305 行：创建时序张量，形状为 `[num_sms, max_queue_len, TIMING_SLOTS]`

### 练习题

1. 假设 `INTS_PER_INSTRUCTION=32`，某条指令序列化后得到 `[1, 5, 10, 3]`，填充后的结果是什么？

2. 为什么要用 NoOp 填充使所有队列长度相等？如果填充 0 会怎样？

3. 指令张量的形状是 `[num_sms, max_queue_len, INTS_PER_INSTRUCTION]`，为什么是 3D 而不是 2D？

4. 时序张量（`timings`）的作用是什么？为什么需要它？

### 答案

1. **答案：**
   填充后的结果：`[1, 5, 10, 3, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0, 0]`（原 4 个整数 + 28 个 0）

2. **答案：**
   使用 NoOp 填充而不是 0，是因为 NoOp 是一个合法的指令（opcode=0），代表"空操作"。如果填充 0，会导致序列化时缺少指令对象，无法调用 `serialize()` 方法。NoOp 确保所有槽位都有有效的指令对象，保持队列结构完整。

3. **答案：**
   3D 张量的每一维都有明确的含义：
   - 第 1 维（num_sms）：区分不同的 SM
   - 第 2 维（max_queue_len）：区分同一 SM 上的不同指令
   - 第 3 维（INTS_PER_INSTRUCTION）：区分同一条指令的不同字段
   2D 张量无法同时表示这三个维度的信息。

4. **答案：**
   时序张量用于记录和控制每条指令在每个 SM 上的执行时间。它的作用包括：
   - 记录指令的开始时间和结束时间
   - 支持指令间的同步和依赖管理
   - 提供精确的时间控制和性能分析
   - 支持动态调度和资源管理

---

## 总结

本讲义介绍了 Megakernels 中的五种 SM 分配策略和指令序列化机制：

1. **轮询分配**：简单高效，适用于指令执行时间相近的场景
2. **Wave 分配**：基于指令类型的分组，优化负载均衡和上下文切换
3. **DAG 感知分配**：考虑指令依赖关系，充分利用指令级并行性
4. **Pool 分配**：基于资源类型的隔离，减少不同类型指令间的干扰
5. **指令序列化**：统一指令格式，便于 GPU 执行和管理

这些策略各有优劣，选择合适的策略需要考虑具体的场景、指令特征和性能目标。在实际应用中，可能需要结合多种策略或进行定制化优化。

---

**本讲义覆盖的最小模块：** 轮询分配、Wave 分配、DAG 感知分配、Pool 分配、指令序列化
