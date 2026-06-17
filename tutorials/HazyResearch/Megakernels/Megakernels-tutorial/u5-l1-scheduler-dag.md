# 第五单元第一讲：调度器基础与 DAG 构建

> 本讲深入理解 Megakernels 调度器如何构建指令 DAG（有向无环图），包括节点设计、依赖关系管理、优先级计算和 ScheduleBuilder 模式。掌握这些内容是理解 GPU 指令并行调度的关键。

## 最小模块 1：DAG 节点设计

### 概念说明

在 GPU 上执行神经网络推理时，我们需要将大量计算任务分配给多个 SM（Streaming Multiprocessor）并行执行。这些任务之间往往存在依赖关系——某些任务必须等待其他任务完成后才能开始。例如，在 Transformer 的注意力层中，O_Proj 必须等待 PartialAttention 完成才能开始。

DAG 节点设计的核心问题是：如何表示单个可调度指令，并追踪其依赖关系和时间信息？

### 伪代码或流程

```
class DAG_Node:
    instruction: Instruction           # 要执行的指令
    dependencies: List[DAG_Node]      # 依赖的父节点列表
    children: Set[DAG_Node]           # 依赖此节点的子节点集合
    start_time: Float                  # 调度开始时间
    end_time: Float                    # 调度结束时间
    remaining_dependencies: Set        # 剩余未完成的依赖（用于调度算法）
    priority: Float                    # 节点优先级

    def earliest_ready_time():
        if 无依赖:
            return 0
        return max(dep.end_time for dep in dependencies)

    def register_with_parents():
        for dep in dependencies:
            dep.children.add(self)
```

### 原理分析

DAG 节点采用**数据流图**的设计思想：

1. **指令封装**：每个节点包装一个 `Instruction`，表示一个可在 GPU 上执行的操作（如矩阵乘法、LayerNorm 等）。

2. **双向依赖链**：
   - `dependencies`：前向依赖，记录此节点依赖哪些父节点（只读）
   - `children`：反向依赖，记录哪些子节点依赖此节点（动态构建）
   - 双向设计使得调度算法可以高效地向前遍历（寻找可执行节点）和向后遍历（更新子节点状态）。

3. **时间槽管理**：
   - `start_time` / `end_time`：记录节点被调度到 SM 的时间段
   - `earliest_ready_time()`：计算节点最早可执行时间 = 所有依赖节点的最大结束时间
   - 初始为 `inf`，调度时由 `assign_dag_to_sms` 填充。

4. **哈希与等价**：`__hash__` 基于 `instruction.serialize()` 的元组，确保相同指令的节点哈希值相同——这在依赖索引（如 `qkv_deps` 字典）中用于快速查找。

5. **remaining_dependencies**：调度算法的**工作状态副本**。初始时复制 `dependencies`，每当一个依赖完成时移除；当集合为空时，节点变为可执行（ready）。

### 代码实践

DAG 节点的完整实现在 `megakernels/scheduler.py` 中：

```python
# https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L17-L47

@dataclass
class DAG_Node:
    def __hash__(self):
        return hash(tuple(self.instruction.serialize()))

    instruction: Instruction
    dependencies: list["DAG_Node"]

    children: set["DAG_Node"] = field(default_factory=set)
    start_time: float = float("inf")
    end_time: float = float("inf")
    remaining_dependencies: set["DAG_Node"] = field(default_factory=set)
    priority: float = 0

    def earliest_ready_time(self, globs: BaseGlobals):
        if len(self.dependencies) == 0:
            return 0
        return max(dep.end_time for dep in self.dependencies)

    def register_with_parents(self):
        for dep in self.dependencies:
            dep.children.add(self)

    def calc_priority(self, globs: BaseGlobals):
        cur_cost = self.priority
        for dep in self.dependencies:
            pri = cur_cost + dep.instruction.cost(globs)
            dep.priority = max(pri, dep.priority)
            dep.calc_priority(globs)
```

**关键行解析**：

- **第 19-20 行**：`__hash__` 基于指令序列化的元组，确保相同内容指令的节点哈希相同。这使得可以在字典中用 `qkv_deps[(layer_idx, opcode, block_idx)] = node` 形式快速查找依赖节点。

- **第 23 行**：`dependencies` 是前向依赖列表，在节点创建时通过构造函数传入。例如在 `make_dag_layer` 中创建 O_Proj 节点时：
  ```python
  o_proj_nodes.append(DAG_Node(ins, partial_nodes))  # partial_nodes 是依赖列表
  ```

- **第 25 行**：`children` 是反向依赖集合，由 `register_with_parents()` 动态填充。调用后，每个父节点都会在其 `children` 集合中记录当前节点。

- **第 31-35 行**：`earliest_ready_time()` 实现关键路径的起点计算。如果节点无依赖，立即就绪（时间 0）；否则需要等待所有依赖的最大结束时间。

- **第 37-39 行**：`register_with_parents()` 遍历所有依赖，将当前节点注册到父节点的 `children` 集合中。这使得调度算法可以从叶子节点向前扫描时，高效地找到待更新的子节点。

- **第 41-46 行**：`calc_priority()` 递归计算优先级（本模块中仅展示接口，实际优先级计算在最小模块 3 中详解）。

### 练习题

1. **基础理解**：为什么 `children` 使用 `set` 而 `dependencies` 使用 `list`？提示：考虑操作类型和数据结构特性。

2. **时间计算**：假设节点 A 有两个依赖：B（结束时间 10）和 C（结束时间 15）。A 的 `earliest_ready_time()` 返回值是多少？如果 B 的结束时间更新为 20 呢？

3. **哈希设计**：如果两个 DAG 节点的 `instruction.serialize()` 返回相同列表，它们的哈希值是否相同？这对字典查找有何影响？

4. **工作状态副本**：为什么需要 `remaining_dependencies` 而不是直接使用 `dependencies`？提示：考虑调度算法的执行过程。

### 答案

1. **答案**：`children` 使用 `set` 是因为需要高效地**添加、查找和删除**子节点，且不关心顺序；`dependencies` 使用 `list` 是因为在节点创建时依赖列表已固定，主要按顺序遍历，无需频繁查找删除。

2. **答案**：返回 `max(10, 15) = 15`。如果 B 更新为 20，则返回 `max(20, 15) = 20`。`earliest_ready_time()` 始终返回所有依赖的最大结束时间。

3. **答案**：相同。哈希值基于 `serialize()` 返回的元组，相同内容 → 相同哈希。这使得可以在字典中用 `qkv_deps[(layer_idx, opcode, block_idx)]` 快速找到对应节点。

4. **答案**：`dependencies` 是只读的原始依赖列表，用于 DAG 的静态结构；`remaining_dependencies` 是调度算法的**可变工作状态**，每当一个依赖完成时从集合中移除。当 `remaining_dependencies` 为空时，表示所有依赖已完成，节点变为可执行。如果直接修改 `dependencies`，会破坏 DAG 的结构信息。

---

## 最小模块 2：依赖关系管理

### 概念说明

在构建 DAG 时，我们需要建立节点之间的依赖关系，确保指令按正确顺序执行。依赖关系管理要解决两个核心问题：

1. **构建时**：如何根据模型计算图的语义，正确地设置每个节点的 `dependencies` 列表？
2. **调度时**：如何高效地追踪依赖完成状态，及时将节点从"等待"转为"就绪"？

例如，在 Llama 层中，QKV → PartialAttention → O_Proj 形成一条链，每步都依赖前一步的输出。

### 伪代码或流程

```
# 构建阶段的依赖设置
def build_dag_layer(prev_layer_outputs):
    # QKV 节点：依赖上一层的输出
    qkv_nodes = [DAG_Node(qkv_ins, prev_layer_outputs) for ins in qkv_instructions]

    # PartialAttention 节点：依赖特定的 QKV 块
    partial_nodes = []
    for kv_head_idx in range(num_kv_heads):
        for partial_idx in range(num_partitions):
            deps = find_qkv_deps(kv_head_idx, partial_idx)  # 找到相关 QKV 节点
            partial_nodes.append(DAG_Node(partial_attn_ins, deps))

    # O_Proj 节点：依赖所有 PartialAttention
    o_proj_nodes = [DAG_Node(o_proj_ins, partial_nodes) for ...]

    return qkv_nodes + partial_nodes + o_proj_nodes, o_proj_nodes

# 调度阶段的依赖追踪
def assign_dag_to_sms(dag_nodes):
    # 步骤 1：注册子节点关系
    for node in dag_nodes:
        node.register_with_parents()

    # 步骤 2：初始化剩余依赖
    for node in dag_nodes:
        node.remaining_dependencies = set(node.dependencies)

    # 步骤 3：找出初始就绪节点
    ready_nodes = [n for n in dag_nodes if len(n.dependencies) == 0]

    # 步骤 4：调度循环
    while ready_nodes is not empty:
        node = select_highest_priority(ready_nodes)

        # 执行节点，更新时间
        node.start_time = sm_ready_time
        node.end_time = node.start_time + node.cost

        # 更新子节点状态
        for child in node.children:
            child.remaining_dependencies.remove(node)
            if child.remaining_dependencies is empty:
                ready_nodes.add(child)
```

### 原理分析

依赖关系管理分为**构建阶段**和**调度阶段**：

#### 构建阶段：静态依赖建立

在 `make_dag_layer` 中，根据模型计算图的语义手动设置依赖：

1. **层间依赖**：`qkv_nodes` 的 `dependencies` 设置为 `prev_layer_outputs`（上一层的输出节点），确保层间顺序。

2. **操作间依赖**：`partial_nodes` 的依赖通过查找字典获得：
   ```python
   qkv_deps = {}
   for node in qkv_nodes:
       for block_idx in node.instruction.block_indices():
           qkv_deps[(layer_idx, opcode, block_idx)] = node

   # 后续查找
   dep_set = {qkv_deps[(layer_idx, prev_opcode, block_idx)] for block_idx in block_indices}
   ```

3. **汇聚依赖**：`o_proj_nodes` 的 `dependencies` 设置为整个 `partial_nodes` 列表，表示必须等待所有部分注意力完成。

关键设计：依赖是**节点引用**，而非索引或 ID。这使得后续调度可以直接操作节点对象，无需查找。

#### 调度阶段：动态依赖追踪

在 `assign_dag_to_sms` 中，通过 `remaining_dependencies` 和 `children` 双向链实现高效更新：

1. **初始化**：
   - `register_with_parents()`：填充反向依赖链 `children`
   - `remaining_dependencies = set(dependencies)`：创建工作副本

2. **调度循环**：
   - 每当节点完成时，遍历其 `children` 集合
   - 对每个子节点，从 `remaining_dependencies` 中移除当前节点
   - 如果 `remaining_dependencies` 变空，将子节点加入就绪队列

**数据流示例**：

假设 A → B → C 的链：

```
初始：
A.remaining_dependencies = {}
B.remaining_dependencies = {A}
C.remaining_dependencies = {B}

A 完成后：
→ 遍历 A.children = {B}
→ B.remaining_dependencies.remove(A) → {}
→ B 变为就绪，加入 ready_queue

B 完成后：
→ 遍历 B.children = {C}
→ C.remaining_dependencies.remove(B) → {}
→ C 变为就绪
```

这种设计避免了每次重新扫描整个 DAG，而是通过**增量更新**快速推进调度状态。

### 代码实践

#### 依赖建立（构建阶段）

```python
# https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L282-L294

# qkv
qkv_instructions = schedule_qkv(globs, layer_idx)
qkv_nodes: list[DAG_Node] = []
for ins in qkv_instructions:
    qkv_nodes.append(DAG_Node(ins, prev_layer_outputs))  # ← 依赖上一层的输出

qkv_deps = {}
for node in qkv_nodes:
    ins: LayerNorm_QKV_MatVecRopeAppend = node.instruction
    for block_idx in ins.block_indices():
        qkv_deps[(layer_idx, ins.opcode(), block_idx)] = node  # ← 建立索引
```

**关键行解析**：

- **第 285 行**：创建 QKV 节点时，将 `prev_layer_outputs` 作为依赖列表传入，建立层间依赖链。

- **第 287-292 行**：构建 `qkv_deps` 字典，键为 `(layer_idx, opcode, block_idx)`，值为对应节点。这使得后续 PartialAttention 节点可以通过查找字典快速找到其依赖的 QKV 节点。

```python
# https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L330-L336

# PartialAttention 节点的依赖设置
dep_set = {
    qkv_deps[(layer_idx, PartialAttention.prev_opcode(), block_idx)]
    for block_idx in block_indices
}
deps = list(dep_set)
partial_nodes.append(DAG_Node(ins, deps))  # ← 依赖集合转为列表传入
```

- **第 330-334 行**：通过集合推导式收集所有相关 QKV 节点，转为列表后传入 `DAG_Node` 构造函数。这建立了操作间依赖。

```python
# https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L343-L354

# oproj
num_o_blocks = assert_div(globs.hidden_size, globs.o_proj_block_size)
o_proj_nodes: list[DAG_Node] = []
for o_block_idx in range(num_o_blocks):
    ins = O_ProjResidual(...)
    o_proj_nodes.append(DAG_Node(ins, partial_nodes))  # ← 依赖所有 PartialAttention
```

- **第 354 行**：O_Proj 节点的依赖设置为整个 `partial_nodes` 列表，建立汇聚依赖。

#### 依赖追踪（调度阶段）

```python
# https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L98-L102

for node in nodes:
    node.register_with_parents()  # ← 填充 children 集合

for node in nodes:
    node.remaining_dependencies = set(node.dependencies)  # ← 初始化工作状态
```

**关键行解析**：

- **第 99 行**：调用 `register_with_parents()` 后，每个父节点的 `children` 集合都包含了所有依赖它的子节点。例如，QKV 节点的 `children` 会包含所有 PartialAttention 节点。

- **第 102 行**：将 `dependencies` 列表转为集合，赋值给 `remaining_dependencies`。这是调度算法的可变工作状态。

```python
# https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L125-L150

while ready_heap:
    ready_time, idx = heapq.heappop(ready_heap)
    node = idx_to_node[idx]

    # ... 调度逻辑 ...

    node.start_time = start_time
    node.end_time = end_time

    # 更新子节点状态
    for child in node.children:  # ← 遍历反向依赖链
        child.remaining_dependencies.remove(node)  # ← 移除已完成依赖
        if len(child.remaining_dependencies) == 0:  # ← 检查是否就绪
            idx = node_to_idx[child]
            heapq.heappush(ready_heap, (-child.instruction.cost(globs), idx))
```

- **第 145-149 行**：每当节点完成调度后，遍历其 `children` 集合。对每个子节点，从 `remaining_dependencies` 中移除当前节点；如果集合变空，将子节点加入就绪堆。这实现了依赖状态的增量更新。

### 练习题

1. **依赖查找**：假设 `qkv_deps` 字典中有键 `(0, 1, 5)` 对应节点 N。如果某 PartialAttention 节点需要依赖块索引为 [3, 5, 7] 的 QKV 节点，如何构建其 `dep_set`？

2. **状态转换**：节点 C 的 `dependencies = [A, B]`，`remaining_dependencies` 初始为 `{A, B}`。当 A 完成调度后，`remaining_dependencies` 变成什么？当 B 也完成后呢？

3. **链式更新**：假设 A → B → C → D 的链。A 完成后，哪些节点的 `remaining_dependencies` 会被更新？B 完成后呢？

4. **反向依赖效率**：为什么使用 `children` 反向依赖链，而不是在每次调度时重新扫描整个 DAG 查找哪些节点依赖当前节点？提示：考虑 DAG 规模。

### 答案

1. **答案**：
   ```python
   dep_set = {
       qkv_deps[(layer_idx, PartialAttention.prev_opcode(), block_idx)]
       for block_idx in [3, 5, 7]
   }
   ```
   通过集合推导式，遍历块索引列表，从字典中查找对应节点并收集到集合中。

2. **答案**：A 完成后，`remaining_dependencies = {B}`；B 完成后，`remaining_dependencies = {}`，节点 C 变为就绪（可加入 `ready_heap`）。

3. **答案**：
   - A 完成后：只有 B 的 `remaining_dependencies` 被更新（移除 A），因为 `A.children = {B}`。
   - B 完成后：只有 C 的 `remaining_dependencies` 被更新（移除 B），因为 `B.children = {C}`。
   - 通过反向依赖链，每次只更新直接子节点，避免全图扫描。

4. **答案**：使用 `children` 反向链的时间复杂度是 \(O(\text{出度})\)，只需更新直接子节点；而全图扫描的时间复杂度是 \(O(|V| + |E|)\)。在大规模 DAG 中（如 32 层 Llama，数千节点），反向链能显著提升效率。

---

## 最小模块 3：优先级计算

### 概念说明

在调度 DAG 时，当多个节点同时就绪（`remaining_dependencies` 为空）时，应该先调度哪个节点？优先级计算要解决这个问题。

Megakernels 使用**关键路径优先**（Critical Path First）策略：优先调度那些位于最长依赖链上的节点，因为这些节点决定了整体完成时间。如果拖延关键路径上的节点，会延长整个调度的 makespan（总完成时间）。

每个指令有 `cost(globs)` 方法，返回其估算执行时间（如矩阵乘法的浮点运算数）。优先级应该反映节点到终点的最长路径长度。

### 伪代码或流程

```
def calc_priority(node, globs):
    cur_cost = node.priority
    for dep in node.dependencies:
        pri = cur_cost + dep.instruction.cost(globs)
        dep.priority = max(pri, dep.priority)
        dep.calc_priority(globs)

# 调度时使用优先级
ready_heap = []
for node in ready_nodes:
    heapq.heappush(ready_heap, (-node.priority, node.idx))

while ready_heap:
    neg_priority, idx = heapq.heappop(ready_heap)
    node = nodes[idx]
    # 调度 node...
```

### 原理分析

#### 关键路径优先（Critical Path First, CPF）

在 DAG 调度中，**关键路径**是从源点到汇点的最长路径（按节点 cost 加权和）。关键路径上的节点决定了整体完成时间，因为它们必须串行执行且 cost 最大。

**直觉理解**：想象一个项目，有多个并行任务链。最长的链决定项目何时完成，应该优先保障这条链上的任务不被延误。

**数学定义**：

对于节点 \(v\)，定义其**优先级** \(p(v)\) 为从 \(v\) 到 DAG 终点的最长路径长度：

\[
p(v) = \max_{\text{path } v \to v_1 \to \cdots \to v_k \to \text{sink}} \left( \sum_{i=1}^{k} \text{cost}(v_i) \right)
\]

对于汇点（无子节点的节点），\(p(\text{sink}) = 0\)。

**递归关系**：

\[
p(v) = \max_{u \in \text{children}(v)} \left( \text{cost}(u) + p(u) \right)
\]

这个递归关系表明：节点 \(v\) 的优先级是其子节点中最大的（cost + 子节点优先级）。

#### 当前实现的简化版本

在当前代码中，`calc_priority` 的实现是一个**简化版本**，不完全符合上述递归关系：

```python
def calc_priority(self, globs: BaseGlobals):
    cur_cost = self.priority
    for dep in self.dependencies:
        pri = cur_cost + dep.instruction.cost(globs)
        dep.priority = max(pri, dep.priority)
        dep.calc_priority(globs)
```

这个函数实际上是**反向遍历依赖链**（从当前节点向父节点传播），而不是向子节点计算。它将当前节点的 priority 加上父节点的 cost，更新父节点的 priority。

**注意**：在 `assign_dag_to_sms` 中，`calc_priority` 被注释掉了（第 104 行），实际调度使用的是**指令 cost 直接作为优先级**：

```python
ready_heap.append((-node.instruction.cost(globs), idx))
```

这是一种启发式策略：优先调度 cost 大的指令，以减少 SM 空闲时间。

#### 优先级与堆调度

调度时使用**最大堆**（Python 的 `heapq` 是最小堆，用负数模拟最大堆）：

```python
heapq.heappush(ready_heap, (-node.priority, node.idx))
```

每次弹出优先级最高的节点（`neg_priority` 最小，即 `priority` 最大）。

### 代码实践

#### 指令 cost 计算

每条指令都实现了 `cost(globs)` 方法，返回其估算执行时间（浮点运算数的代理）：

```python
# https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/instructions.py#L53-L58

class LayerNorm_QKV_MatVecRopeAppend(Instruction):
    def cost(self, globs: Globals):
        return (
            (self.end_output_block_idx - self.start_output_block_idx)
            * globs.qkv_block_size
            * globs.hidden_size
        )
```

**关键行解析**：

- **第 54-58 行**：QKV 指令的 cost = 输出块大小 × 块大小 × hidden_size。这近似于矩阵乘法的 FLOPs（浮点运算次数）：\(\text{FLOPs} \approx \text{输出元素数} \times \text{输入维度}\)。

```python
# https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/instructions.py#L76-L81

class PartialAttention(Instruction):
    def cost(self, globs: Globals):
        seq_len = globs.pos_id + 1
        loaded_seq_len = seq_len / self.num_partials
        # num loaded elements from kv cache
        return loaded_seq_len * globs.head_dim * 2
```

- **第 77-81 行**：PartialAttention 的 cost = 加载序列长度 × head_dim × 2（K 和 V）。这反映了从 KV Cache 加载数据的内存访问量。

#### 优先级计算接口

```python
# https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L41-L46

def calc_priority(self, globs: BaseGlobals):
    cur_cost = self.priority
    for dep in self.dependencies:
        pri = cur_cost + dep.instruction.cost(globs)
        dep.priority = max(pri, dep.priority)
        dep.calc_priority(globs)
```

**关键行解析**：

- **第 42 行**：读取当前节点的 `priority`（初始为 0）。
- **第 43-46 行**：遍历所有依赖，对每个依赖：
  - 计算 `pri = cur_cost + dep.instruction.cost(globs)`
  - 更新 `dep.priority = max(pri, dep.priority)`（取最大值）
  - 递归调用 `dep.calc_priority(globs)`，继续向上传播

这个函数从调用节点开始，**向上遍历 DAG**（从子节点向父节点），将 cost 累积到父节点的 priority 中。

#### 调度中的优先级使用

```python
# https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L112-L123

ready_nodes = [n for n in nodes if len(n.dependencies) == 0]

idx_to_node = {i: n for i, n in enumerate(nodes)}
node_to_idx = {n: i for i, n in enumerate(nodes)}

ready_heap = []
for node in ready_nodes:
    idx = node_to_idx[node]
    # max cost first
    ready_heap.append((-node.instruction.cost(globs), idx))  # ← 直接使用 cost

heapq.heapify(ready_heap)
```

**关键行解析**：

- **第 121 行**：直接使用 `node.instruction.cost(globs)` 作为优先级，用负数表示最大堆。这实现了**最大 cost 优先**的启发式策略。

```python
# https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L125-L150

while ready_heap:
    ready_time, idx = heapq.heappop(ready_heap)  # ← 弹出优先级最高（cost 最大）的节点
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
            heapq.heappush(ready_heap, (-child.instruction.cost(globs), idx))  # ← 新就绪节点按 cost 加入
```

- **第 126 行**：从 `ready_heap` 弹出优先级最高的节点（`ready_time` 最小，因为 cost 用负数表示）。
- **第 149 行**：当子节点变为就绪时，同样按 `instruction.cost` 加入堆。

### 练习题

1. **关键路径识别**：假设 DAG 有以下节点和 cost：
   - A (cost=5), B (cost=3), C (cost=7)
   - 依赖：B → A, C → A
   - 哪条是关键路径？A 的优先级应该是多少？

2. **优先级递归**：对于节点 D，其子节点为 E 和 F。E 的 priority=10，cost=5；F 的 priority=8， cost=6。根据递归关系，D 的 priority 应该是多少？

3. **堆调度**：假设就绪节点的 cost 分别为 [15, 8, 20, 12]。用最大堆调度，节点的处理顺序是什么？

4. **CPF vs Max-Cost**：当前代码使用 max-cost 启发式，而非完整的 CPF。在什么情况下这两种策略会给出不同的调度顺序？

### 答案

1. **答案**：
   - 路径 B → A：cost = 3 + 5 = 8
   - 路径 C → A：cost = 7 + 5 = 12
   - 关键路径是 C → A（最长路径）。
   - 根据递归关系，A 的 priority 应该是 `max(cost(C)+p(C), cost(B)+p(B)) = max(7+0, 3+0) = 7`（假设 C 和 B 的优先级都为 0，因为它们是叶子节点）。

2. **答案**：
   - 根据递归关系：\(p(D) = \max(\text{cost}(E) + p(E), \text{cost}(F) + p(F))\)
   - \(p(D) = \max(5 + 10, 6 + 8) = \max(15, 14) = 15\)

3. **答案**：处理顺序为 [20, 15, 12, 8]。最大堆保证每次弹出 cost 最大的节点。

4. **答案**：
   - **CPF（关键路径优先）**：优先级 = 节点到终点的最长路径。能准确识别关键路径，保证关键路径上的节点优先调度。
   - **Max-Cost 启发式**：优先级 = 节点自身的 cost。只考虑当前节点的执行时间，不考虑其在 DAG 中的位置。
   - **差异场景**：
     - 假设节点 A（cost=100，无子节点）和 B（cost=50，子节点 C 的 cost=60，总路径=110）同时就绪。
     - CPF 会优先调度 B（priority=110），因为 B 在关键路径上。
     - Max-Cost 会优先调度 A（cost=100），因为 A 的自身 cost 更大。
     - 在这种情况下，CPF 更优，因为拖延 B 会延长整体 makespan（110 vs 100）。

---

## 最小模块 4：ScheduleBuilder 模式

### 概念说明

不同的优化目标（如延迟优化 LatencyOptimized、吞吐量优化 ThroughputOptimized）需要不同的 DAG 构建策略。例如：
- **延迟优化**：尽量减少单次推理的延迟，可能需要更多的并行划分。
- **吞吐量优化**：尽量提高 batch 处理效率，可能需要更粗粒度的指令合并。

ScheduleBuilder 模式要解决的问题是：如何提供一个统一的接口来构建调度，同时允许不同优化策略有不同的实现？

### 伪代码或流程

```
abstract class ScheduleBuilder:
    @classmethod
    def make_globals(cls, model):
        raise NotImplementedError

    @classmethod
    def make_dag(cls, globs, stop_after_op, layer_limit):
        raise NotImplementedError

    @classmethod
    def build(cls, model, stop_after_op, layer_limit):
        globs = cls.make_globals(model)
        dag_nodes, end_node = cls.make_dag(globs, stop_after_op, layer_limit)
        return Schedule(globs, dag_nodes, end_node)

# 延迟优化实现
class LatencyScheduleBuilder(ScheduleBuilder):
    @classmethod
    def make_globals(cls, model):
        return make_latency_globals(model)

    @classmethod
    def make_dag(cls, globs, stop_after_op, layer_limit):
        return make_latency_dag(globs, stop_after_op, layer_limit)

# 吐量优化实现
class ThroughputScheduleBuilder(ScheduleBuilder):
    @classmethod
    def make_globals(cls, model):
        return make_throughput_globals(model)

    @classmethod
    def make_dag(cls, globs, stop_after_op, layer_limit):
        return make_throughput_dag(globs, stop_after_op, layer_limit)

# 使用
schedule = LatencyScheduleBuilder.build(model)
```

### 原理分析

ScheduleBuilder 采用**模板方法模式**（Template Method Pattern）：

1. **统一的构建流程**：`build` 方法定义了构建调度的标准步骤：
   - 步骤 1：调用 `make_globals(model)`，从模型参数中提取全局配置（权重、缓存、超参数等）
   - 步骤 2：调用 `make_dag(globs, ...)`，根据全局配置和优化目标构建 DAG 节点
   - 步骤 3：返回 `Schedule(globs, dag_nodes, end_node)`，封装调度结果

2. **策略可替换**：`make_globals` 和 `make_dag` 是抽象方法，由子类实现。不同的优化策略可以提供不同的实现，而 `build` 方法保持不变。

3. **扩展性**：新增优化策略只需继承 `ScheduleBuilder` 并实现两个方法，无需修改 `build` 的调用代码。

**依赖倒置原则**：高层模块（调用 `build` 的代码）依赖于抽象（`ScheduleBuilder` 接口），而非具体实现（`LatencyScheduleBuilder`）。这使得策略可以在运行时替换。

**组合模式的应用**：`Schedule` 对象组合了 `globs`（全局配置）和 `dag_nodes`（DAG 结构），封装了完整的调度信息。后续调度分配（如 `assign_dag_to_sms`）只依赖于 `Schedule` 对象，不关心其如何构建。

### 代码实践

#### ScheduleBuilder 抽象基类

```python
# https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L67-L92

class ScheduleBuilder:
    @classmethod
    def make_globals(cls, model):
        raise NotImplementedError

    @classmethod
    def make_dag(
        cls, globs, stop_after_op: str | None = None, layer_limit: int | None = None
    ):
        raise NotImplementedError

    @classmethod
    def build(
        cls,
        model: LlamaForCausalLM,
        stop_after_op: str | None = None,
        layer_limit: int | None = None,
    ):
        globs = cls.make_globals(model)
        dag_nodes, end_node = cls.make_dag(globs, stop_after_op, layer_limit)
        return Schedule(globs, dag_nodes, end_node)

    @classmethod
    def with_new_globals(cls, schedule: Schedule, model: LlamaForCausalLM):
        return replace(schedule, globs=cls.make_globals(model))
```

**关键行解析**：

- **第 68-70 行**：`make_globals` 抽象方法，要求子类从模型中提取全局配置。返回 `BaseGlobals` 或其子类。

- **第 72-76 行**：`make_dag` 抽象方法，要求子类根据全局配置构建 DAG。参数 `stop_after_op` 用于调试（限制只构建到某个操作），`layer_limit` 用于限制层数（增量调试）。

- **第 78-87 行**：`build` 模板方法，定义构建流程：
  1. 调用 `make_globals` 创建全局配置
  2. 调用 `make_dag` 构建 DAG，返回节点列表和结束节点
  3. 封装为 `Schedule` 对象返回

- **第 89-91 行**：`with_new_globals` 工具方法，用于替换现有调度的全局配置（如模型参数更新后重建 Globals）。

#### LatencyScheduleBuilder 实现

```python
# https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L389-L397

class LatencyScheduleBuilder(ScheduleBuilder):
    @classmethod
    def make_globals(cls, model):
        return make_globals(model)

    @classmethod
    def make_dag(
        cls, globs, stop_after_op: str | None = None, layer_limit: int | None = None
    ):
        return make_dag(globs, stop_after_op, layer_limit)
```

**关键行解析**：

- **第 391-392 行**：`make_globals` 委托给 `make_globals` 函数（定义在同一文件的第 38 行），创建延迟优化的全局配置（如设置 `skip_attn_reduction=True`）。

- **第 395-396 行**：`make_dag` 委托给 `make_dag` 函数（定义在第 235 行），构建延迟优化的 DAG（如减少部分注意力的划分）。

#### ThroughputScheduleBuilder 实现

```python
# https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/throughput/scheduler.py#L401-L409

class ThroughputScheduleBuilder(ScheduleBuilder):
    @classmethod
    def make_globals(cls, model):
        return make_globals(model)

    @classmethod
    def make_dag(
        cls, globs, stop_after_op: str | None = None, layer_limit: int | None = None
    ):
        return make_dag(globs, stop_after_op, layer_limit)
```

**关键行解析**：

- **第 403-404 行**：`make_globals` 委托给 `make_globals` 函数（定义在 throughput/scheduler.py 中），创建吞吐量优化的全局配置（如 batch 维度调整）。

- **第 407-408 行**：`make_dag` 委托给 `make_dag` 函数，构建吞吐量优化的 DAG（如更大的 block size）。

#### 使用示例

```python
# 构建延迟优化调度
schedule = LatencyScheduleBuilder.build(model, stop_after_op=None, layer_limit=None)

# 或者构建吞吐量优化调度
schedule = ThroughputScheduleBuilder.build(model)

# 分配到 SM
sm_queues = schedule.smart_assign_to_sms()  # 内部调用 assign_dag_to_sms

# 序列化指令
tensorize_instructions(globs, sm_queues)
```

### 练习题

1. **模式识别**：ScheduleBuilder 使用的是什么设计模式？它与"策略模式"（Strategy Pattern）有何异同？

2. **扩展性**：假设要新增一个"内存优化"策略（MemoryOptimized），应该如何扩展？需要修改哪些代码？

3. **调试支持**：`stop_after_op` 和 `layer_limit` 参数的作用是什么？它们如何帮助调试 DAG 构建过程？

4. **依赖替换**：`with_new_globals` 的应用场景是什么？为什么需要替换全局配置而不重新构建整个 DAG？

### 答案

1. **答案**：ScheduleBuilder 使用**模板方法模式**（Template Method Pattern）。它定义了算法的骨架（`build` 方法），将某些步骤（`make_globals` 和 `make_dag`）延迟到子类实现。
   - 与策略模式的异同：
     - 相同点：都允许在运行时替换算法的不同实现。
     - 不同点：策略模式通过组合实现（传入策略对象），模板方法通过继承实现（子类重写方法）。ScheduleBuilder 使用继承，属于模板方法模式。

2. **答案**：
   - 新增 `MemoryOptimizedScheduleBuilder` 类，继承 `ScheduleBuilder`。
   - 实现 `make_globals` 和 `make_dag` 方法，调用内存优化的专用函数。
   - **无需修改** `ScheduleBuilder` 基类或 `build` 方法，只需新增文件和类。这体现了开闭原则（对扩展开放，对修改封闭）。

3. **答案**：
   - `stop_after_op`：限制 DAG 构建到某个操作为止（如 `stop_after_op="qkv"` 只构建 QKV 节点）。用于分阶段调试，先验证前面部分正确。
   - `layer_limit`：限制只构建前 N 层（如 `layer_limit=1` 只构建第 0 层）。用于减少 DAG 规模，加速调试循环。
   - 两者可以组合使用：`stop_after_op="oproj", layer_limit=2` 只构建前 2 层的 O_Proj 节点。

4. **答案**：
   - **应用场景**：当模型参数更新（如 fine-tuning 后权重改变），但 DAG 结构不变时。可以用 `with_new_globals` 重新提取全局配置，而复用已有的 DAG 节点。
   - **为什么复用 DAG**：构建 DAG 是计算密集的过程（需要分析模型结构、计算依赖关系），而全局配置提取相对轻量。如果只改变权重，DAG 的依赖关系不变，重新构建是浪费。
   - **实现原理**：`replace(schedule, globs=...)` 使用 `dataclasses.replace` 创建 Schedule 的浅拷贝，只替换 `globs` 字段，保留 `dag_nodes` 和 `end_node`。

---

## 总结

本讲深入探讨了 Megakernels 调度器的四个核心模块：

1. **DAG 节点设计**：通过 `DAG_Node` 类封装指令、依赖关系、时间信息和优先级，建立了双向依赖链（前向 `dependencies` 和反向 `children`）。

2. **依赖关系管理**：在构建阶段通过手动设置 `dependencies` 建立静态依赖；在调度阶段通过 `remaining_dependencies` 和 `children` 实现增量更新，高效追踪节点状态。

3. **优先级计算**：介绍了关键路径优先（CPF）策略，分析了当前实现的 max-cost 启发式，解释了如何通过优先级堆实现就绪节点排序。

4. **ScheduleBuilder 模式**：采用模板方法模式，统一了调度构建流程（`make_globals` → `make_dag` → `Schedule`），同时支持多种优化策略的灵活扩展。

这些模块共同构成了一个高效的 GPU 指令调度框架，为后续的 SM 分配和指令执行奠定了基础。

**本讲义覆盖的最小模块**：DAG 节点设计、依赖关系管理、优先级计算、ScheduleBuilder 模式。
