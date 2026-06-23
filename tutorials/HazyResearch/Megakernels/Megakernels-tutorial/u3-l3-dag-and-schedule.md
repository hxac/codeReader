# DAG、Schedule 与 ScheduleBuilder

## 1. 本讲目标

上一讲（u3-l2）我们认识了 latency 模式的 7 个 opcode，理解了**一条指令是什么**。但 GPU 上一次 decode 要执行成百上千条指令，这些指令并不是随便排成一队的——它们之间有严格的先后依赖（算 QKV 之后才能做 attention，做完 attention 才能做 o_proj……）。

本讲要回答：**这些指令是如何被组织成一张「依赖图（DAG）」的，又是如何被封装成一个可调度的 Schedule 的？**

学完本讲你应当能够：

1. 说清 `DAG_Node` 的 `dependencies` / `children` / `remaining_dependencies` 三个字段分别在什么时刻起作用。
2. 读懂 `make_dag_layer` 如何把单层 5 个阶段的指令**按数据流**连成依赖边（`qkv→partial→oproj→upgate→downproj`），尤其是 `qkv→partial` 这条**细粒度**边。
3. 理解 `Schedule` 与 `ScheduleBuilder` 的接口，知道 `build()` 三步走（`make_globals → make_dag → Schedule`）以及为什么用一个 `NoOp` 的 `end_node` 收尾。

---

## 2. 前置知识

### 2.1 什么是 DAG

DAG 是「有向无环图」（Directed Acyclic Graph）：

- **有向**：节点之间有方向明确的边，A→B 表示「A 必须先完成，B 才能开始」。
- **无环**：不存在 A 依赖 B、B 又依赖 A 的循环，否则就死锁了。

在调度领域，DAG 的节点是「任务」，边是「数据依赖」。只要一个任务的所有入边（它的所有依赖）都完成，它就「就绪（ready）」可以被执行。

### 2.2 单层 Llama 的数据流（回顾 u3-l2）

一个 Llama decoder 层在 latency 模式下被拆成这样的流水线：

```
hidden_states
    │
    ▼
[ qkv ]  RMSNorm + Q/K/V 投影 + RoPE + 追加 KV cache
    │
    ▼
[ partial ]  每个 kv head 上做 flash attention 的部分和
    │
    ▼
[ oproj ]  attention 输出做 O 投影 + 残差相加
    │
    ▼
[ upgate ]  MLP 的 RMSNorm + gate/up 投影 + SiLU
    │
    ▼
[ downproj ]  MLP 的 down 投影 + 残差相加  → 下一层的 hidden_states
```

每一阶段都会被切成多条指令（按 SM / 输出 block 切分）。本讲的任务就是**把这些指令按上图的方向连上依赖边**。

> 小提示：u3-l2 讲了 7 个 opcode（含 `AttentionReduction`）。但在 latency 的 DAG 里，`skip_attn_reduction=True`、`num_attention_partitions=1`，所以 **`AttentionReduction` 不出现在 DAG 中**，单层实际只用到 `qkv / partial / oproj / upgate / downproj` 这 5 类，外加最后全局一次 `rms_lm_head`。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [megakernels/scheduler.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py) | 定义 DAG 的「通用骨架」：`DAG_Node`（节点）、`Schedule`（封装）、`ScheduleBuilder`（抽象基类），以及后续 SM 调度用的工具函数。本讲关注前三个。 |
| [megakernels/demos/latency/scheduler.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py) | latency 模式专属：`make_dag` / `make_dag_layer` 真正「逐层连依赖」的地方，以及 `LatencyScheduleBuilder` 子类。 |
| [megakernels/instructions.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py) | `Instruction.serialize()` 被 `DAG_Node.__hash__` 复用；`NoOp` 用作 `end_node`。 |

一句话区分两个文件：**`scheduler.py` 定义「怎么表示 DAG」**，**`latency/scheduler.py` 定义「latency 这套指令怎么连成 DAG」**。

---

## 4. 核心概念与源码讲解

### 4.1 DAG_Node：依赖图里的一个节点

#### 4.1.1 概念说明

`DAG_Node` 是依赖图的一个节点，它「包裹」一条指令，并记录这条指令与其他指令的依赖关系。你可以把它理解为一个「带依赖信息的指令信封」。

它需要回答三类问题：

1. **构建期**：我依赖谁？谁又依赖我？（`dependencies` / `children`）
2. **调度期**：我的依赖还剩几个没完成？我是不是可以开始跑了？（`remaining_dependencies`）
3. **执行期**：我什么时候开始、什么时候结束？优先级多少？（`start_time` / `end_time` / `priority`）

#### 4.1.2 核心流程

`DAG_Node` 的关键字段与其生命周期：

| 字段 | 何时被设置 | 含义 |
| --- | --- | --- |
| `instruction` | 创建时 | 这个节点对应的指令（如 `LayerNorm_QKV_MatVecRopeAppend`） |
| `dependencies` | 创建时（构造参数） | 我依赖哪些节点（必须先完成） |
| `children` | `register_with_parents()` 时反向填充 | 谁依赖我（便于通知后继） |
| `remaining_dependencies` | 调度开始时复制自 `dependencies`，随调度递减 | 还有多少依赖没完成，为空即「就绪」 |
| `start_time` / `end_time` | 调度器分配后 | 模拟出的起止时刻（用于排时间表） |
| `priority` | `calc_priority()` 反向传播 | 关键路径优先级 |

三个辅助方法刻画了「就绪」与「优先级」的语义：

- **就绪时刻**：一个节点能开始的最早时间 = 它所有依赖结束时间的最大值。

\[
  \text{earliest\_ready}(n) = \max_{d \in \text{dependencies}(n)} \text{end\_time}(d)
\]

（无依赖时为 0。）这正是 `earliest_ready_time` 的实现。

- **反向建孩子指针**：依赖是「孩子→父亲」方向写的（我声明我依赖谁），但通知时需要「父亲→孩子」方向（我完成后要通知谁）。`register_with_parents()` 把每个父亲节点的 `children` 集合加上自己，补齐这个反向边。

- **关键路径优先级**：`calc_priority()` 从当前节点向依赖反向传播，`dep.priority = max(dep.priority, cur_cost + dep.cost)`。直觉是：**越处在「最耗时链路」上的节点优先级越高**，调度时应优先安排。这套优先级在 u4 的 DAG 列表调度里才会真正用到。

#### 4.1.3 源码精读

`DAG_Node` 的完整定义：

```python
# DAG_Node 用 dataclass 定义，instruction + dependencies 是必填项
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
```

见 [megakernels/scheduler.py:17-29](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L17-L29)——定义了上面表中的全部字段。

> **关于 `__hash__`**：节点显式定义了 `__hash__`，它把指令序列化后的整数列表当哈希源（`hash(tuple(self.instruction.serialize()))`，见 [megakernels/scheduler.py:19-20](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L19-L20)）。这一点很关键：节点要被放进 `children` / `remaining_dependencies` 两个 `set`，还要当 dict 的 key，**必须是可哈希的**。它复用 `Instruction.serialize()`（详见 [instructions.py:97-119](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/instructions.py#L97-L119)），保证「指令内容相同→哈希相同」。

三个方法：

```python
def earliest_ready_time(self, globs):
    if len(self.dependencies) == 0:
        return 0
    return max(dep.end_time for dep in self.dependencies)

def register_with_parents(self):
    for dep in self.dependencies:
        dep.children.add(self)

def calc_priority(self, globs):
    cur_cost = self.priority
    for dep in self.dependencies:
        pri = cur_cost + dep.instruction.cost(globs)
        dep.priority = max(pri, dep.priority)
        dep.calc_priority(globs)
```

见 [megakernels/scheduler.py:31-46](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L31-L46)。

- `earliest_ready_time` 直接实现了上面的 max 公式。
- `register_with_parents` 把「我」加入每个依赖的 `children` 集合（反向边）。
- `calc_priority` 是递归的反向传播：把「我到末尾的累计代价」加到每个依赖上，并取 max。

#### 4.1.4 代码实践

**目标**：亲手建几个 `DAG_Node`，观察 `dependencies` 与 `register_with_parents()` 后 `children` 的变化。

**操作步骤**（纯 Python，无需 GPU）：

```python
# 示例代码：手搓 3 个节点，连成 a -> b -> c
from megakernels.instructions import NoOp
from megakernels.scheduler import DAG_Node

a = DAG_Node(NoOp(), [])
b = DAG_Node(NoOp(), [a])
c = DAG_Node(NoOp(), [a, b])   # c 同时依赖 a 和 b

for n in (a, b, c):
    n.register_with_parents()

print("a.children:", len(a.children))   # a 被 b 和 c 依赖 -> 2
print("b.children:", len(b.children))   # b 被 c 依赖       -> 1
print("c.children:", len(c.children))   # c 是终点          -> 0
print("c.dependencies:", len(c.dependencies))  # 2 (a, b)
print("c.earliest_ready_time(0):", c.earliest_ready_time(None))  # 依赖未调度，end_time=inf
```

**需要观察的现象**：

- `register_with_parents()` 之后，`children` 是依赖关系的**反向投影**：入度（被依赖次数）= `len(children)`。
- 因为 `c` 的依赖 `end_time` 仍是 `inf`（没经过调度），`earliest_ready_time` 返回 `inf`——它要等真正的调度器（u4）填好 `end_time` 才有意义。

**预期结果**：`a.children=2, b.children=1, c.children=0`。其余「待本地验证」实际数值。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `dependencies` 用 `list`，而 `children` / `remaining_dependencies` 用 `set`？

> **参考答案**：`dependencies` 在构造时按确定顺序传入，保留顺序便于稳定遍历；`children` 和 `remaining_dependencies` 都涉及「成员关系判断」和「去重」（一个 partial 可能从同一条 qkv 读多个 block，去重后只算一次依赖），用 `set` 的 O(1) 查找/删除更合适。

**练习 2**：如果两个 `DAG_Node` 包含**完全相同的指令**（同 opcode、同参数），它们的 `__hash__` 会相同吗？这会带来什么隐患？

> **参考答案**：会相同，因为 `__hash__` 只看 `instruction.serialize()`。但它们是不同的节点对象。在 `set` 里，Python 先比哈希、再用 `==`（dataclass 默认 `__eq__` 比较全部字段，含可变对象）判等，不同对象通常判为不等，所以能共存。隐患是：**不能把两个内容相同的节点当成「逻辑上同一个」**去重，否则调度时会漏掉其中一个。

---

### 4.2 make_dag / make_dag_layer：逐层连依赖

#### 4.2.1 概念说明

有了 `DAG_Node`，下一步是「把指令真的连起来」。`make_dag` 负责遍历所有层（再加最后的 lm_head），`make_dag_layer` 负责把**单层**的 5 个阶段按数据流连上依赖。

关键设计点有两个：

1. **依赖不是「全连接」**：粗看是「qkv 阶段 → partial 阶段」，但代码并没有让每个 partial 依赖所有 qkv，而是让每个 partial **只依赖写出它所需 K/V block 的那几条 qkv**。这是细粒度的数据依赖，能让更多指令提前就绪。
2. **`stop_after_op` 早停开关**：可以要求构建到某个阶段就停（返回当前阶段的节点作为「层输出」）。这是给性能剖析/调试用的——只看某一阶段的 DAG。

#### 4.2.2 核心流程

`make_dag` 的整体结构（伪代码）：

```
nodes = []
last_outputs = 上一层的输出节点列表   # 第一层为空 []
for layer_idx in range(nlayers):
    new_nodes, new_outputs = make_dag_layer(globs, layer_idx, last_outputs, stop_after_op)
    nodes.extend(new_nodes)
    last_outputs = new_outputs         # 串成层间依赖

if 构建了全部层(nlayers == num_hidden_layers):
    追加 rms_lm_head 节点（依赖 last_outputs）
    last_outputs = lm_head_nodes

end_node = DAG_Node(NoOp(), last_outputs)   # 一个 NoOp 终点，依赖最后所有输出
return nodes, end_node
```

`make_dag_layer` 的单层连线（每一段都是「先 schedule 出本阶段指令 → 用上一阶段节点作为 dependencies 建节点 → 检查 stop_after_op 决定是否提前返回」）：

```
prev = 上一层输出
# qkv：依赖 prev（首层为空，即无依赖）
qkv_nodes      = build(schedule_qkv,            deps=prev)
if stop_after_op == "qkv": return ..., qkv_nodes

# partial：依赖【特定的 qkv 节点】（见 4.2.3 细粒度边）
partial_nodes  = build(PartialAttention,        deps=按 block 反查 qkv_deps)
if stop_after_op == "partial": return ..., partial_nodes

# oproj：依赖【全部】partial 节点
o_proj_nodes   = build(O_ProjResidual,          deps=partial_nodes)
if stop_after_op == "oproj": return ..., o_proj_nodes

# upgate：依赖【全部】o_proj 节点
upgate_nodes   = build(schedule_upgate,         deps=o_proj_nodes)
if stop_after_op == "upgate": return ..., upgate_nodes

# downproj：依赖【全部】upgate 节点
downproj_nodes = build(schedule_downproj,       deps=upgate_nodes)
if stop_after_op == "downproj": return ..., downproj_nodes
return ..., downproj_nodes
```

注意一个不变量：每一阶段的输出节点列表，正是下一阶段的「层输出」（`new_outputs`），从而天然形成**层内串行依赖**和**层间串行依赖**（`make_dag` 里把 `last_outputs` 传给下一层 qkv 的 `prev`）。

#### 4.2.3 源码精读

先看 `make_dag` 的骨架与 `end_node` 收尾：

```python
def make_dag(globs, stop_after_op=None, layer_limit=None):
    nodes = []
    nlayers = layer_limit if layer_limit is not None else globs.num_hidden_layers

    last_outputs = []
    for layer_idx in range(nlayers):
        new_nodes, new_outputs = make_dag_layer(
            globs=globs, layer_idx=layer_idx,
            prev_layer_outputs=last_outputs, stop_after_op=stop_after_op,
        )
        nodes.extend(new_nodes)
        last_outputs = new_outputs

    if nlayers == globs.num_hidden_layers:   # 只有完整构建时才加 lm_head
        lm_head_nodes = [DAG_Node(ins, last_outputs) for ins in schedule_lm_head(globs)]
        nodes.extend(lm_head_nodes)
        last_outputs = lm_head_nodes

    end_node = DAG_Node(NoOp(), last_outputs)   # NoOp 终点收尾
    return nodes, end_node
```

见 [megakernels/demos/latency/scheduler.py:235-267](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L235-L267)。`end_node` 见 [L265](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L265)：它是一个 `NoOp`，依赖最后一阶段的所有输出——**图有且仅有一个汇点**，调度器可以把它当作「整张图结束」的标志。

再看 `make_dag_layer` 里最精妙的 **qkv→partial 细粒度边**。

qkv 阶段先建节点，并登记一张「哪个 block 由哪个 qkv 节点写出」的反查表：

```python
# qkv
qkv_instructions = schedule_qkv(globs, layer_idx)
qkv_nodes = [DAG_Node(ins, prev_layer_outputs) for ins in qkv_instructions]

qkv_deps = {}
for node in qkv_nodes:
    ins = node.instruction
    for block_idx in ins.block_indices():            # 该 qkv 节点写出的所有 block
        qkv_deps[(layer_idx, ins.opcode(), block_idx)] = node
```

见 [megakernels/demos/latency/scheduler.py:281-294](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L281-L294)。`qkv_deps` 的 key 是 `(层号, qkv 的 opcode, 输出 block 号)`，value 是写出该 block 的那个 qkv 节点。

partial 阶段则根据「我这条 partial 需要读哪些 K/V block」去反查，只依赖写这些 block 的 qkv 节点：

```python
for kv_head_idx in range(globs.num_kv_heads):
    for partial_idx in range(num_attention_partitions):   # latency 里 =1
        ins = PartialAttention(layer_idx=layer_idx, kv_head_idx=kv_head_idx,
                               num_partials=num_attention_partitions, partial_idx=partial_idx)
        block_indices = [ ...该 kv head 的 K block + V block... ]
        dep_set = {
            qkv_deps[(layer_idx, PartialAttention.prev_opcode(), block_idx)]
            for block_idx in block_indices
        }
        partial_nodes.append(DAG_Node(ins, list(dep_set)))
```

见 [megakernels/demos/latency/scheduler.py:300-336](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L300-L336)，其中反查建依赖的核心是 [L330-L334](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L330-L334)。

> **`PartialAttention.prev_opcode()` 是什么？** 它返回「上一阶段（生产者）的 opcode」，即 `LayerNorm_QKV_MatVecRopeAppend.opcode()`（见 [latency/instructions.py:73-74](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/instructions.py#L73-L74)）。这正是 u3-l2 讲过的「opcode / prev_opcode 成对关系」——partial 用 `prev_opcode()` 去 qkv_deps 表里查 qkv 写出的 block，保证生产者与消费者口径一致。用 `set` 去重后，**一条 partial 通常只依赖 2 个 qkv 节点**（一个写它的 K block、一个写它的 V block），而不是全部 qkv。

后面的 oproj / upgate / downproj 则是「依赖上一阶段全部节点」的粗粒度连接：

```python
# oproj：每个 o_proj block 依赖所有 partial 节点（attention 完整输出）
o_proj_nodes = [DAG_Node(O_ProjResidual(...), partial_nodes) for o_block_idx in range(num_o_blocks)]
# upgate：依赖所有 o_proj 节点
upgate_nodes = [DAG_Node(ins, o_proj_nodes) for ins in schedule_upgate(globs, layer_idx)]
# downproj：依赖所有 upgate 节点
downproj_nodes = [DAG_Node(ins, upgate_nodes) for ins in schedule_downproj(globs, layer_idx)]
```

见 [megakernels/demos/latency/scheduler.py:343-354](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L343-L354)（oproj）、[L361-L365](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L361-L365)（upgate）、[L374-L377](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L374-L377)（downproj）。每段后面都紧跟一个 `if stop_after_op == "...": return ...`（见 [L296-297](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L296-L297)、[L340-L341](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L340-L341)、[L358-L359](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L358-L359)、[L369-L370](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L369-L370)、[L381-L382](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L381-L382)）。

把单层依赖边画出来大致是（粗线 = 全连接，细线 = 按 block 细粒度）：

```
  prev_layer_outputs
         │ (首层为空)
         ▼
      ┌──── qkv_nodes (按 SM 切，每个写一段 block) ────┐
      │  细粒度：partial 只连写出自己 K/V block 的 qkv   │
      ▼                                                 ▼
   partial_nodes  ──────全连接──────►  o_proj_nodes
                                          │ 全连接
                                          ▼
                                    upgate_nodes
                                          │ 全连接
                                          ▼
                                   downproj_nodes  ──► 下一层 / end_node
```

#### 4.2.4 代码实践（本讲主实践）

**目标**：对单层使用 `stop_after_op`（`qkv/partial/oproj/upgate/downproj`），观察每阶段累计节点数；再画出 `qkv→partial→oproj` 的依赖边，验证「partial 只依赖少数 qkv」。

> **技巧**：`make_dag_layer` 只读取 globs 的**标量字段**（`num_kv_heads`、`head_dim`、各种 `block_size`、`sm_count()` 等），不碰任何张量。因此我们造一个**只含标量的最小 globs 桩**，就能在**纯 CPU** 上跑通 DAG 构建（绕开真实 `make_globals` 里需要 GPU 的 `get_sm_count`）。

**操作步骤**：

```python
# 示例代码：纯 CPU 跑 latency 单层 DAG 构建
import types
from megakernels.demos.latency.scheduler import make_dag

# 最小 globs 桩：只给 make_dag_layer 用到的标量字段
globs = types.SimpleNamespace(
    skip_attn_reduction=True,
    num_attention_heads=16,
    num_kv_heads=4,
    head_dim=128,
    hidden_size=2048,
    intermediate_size=8192,
    num_hidden_layers=4,     # 注意：要 > layer_limit，否则会触发 lm_head 分支
    vocab_size=128256,
    qkv_block_size=16,
    o_proj_block_size=16,
    up_gate_proj_block_size=16,
    down_proj_block_size=16,
    lm_head_block_size=16,
)
globs.sm_count = lambda: 8   # make_dag_layer 只需要这个方法返回 int

# 步骤 1：逐阶段累计节点数
for stop in ["qkv", "partial", "oproj", "upgate", "downproj", None]:
    nodes, end = make_dag(globs, stop_after_op=stop, layer_limit=1)
    print(f"stop_after_op={stop!s:>10}  layer 节点数 = {len(nodes):>4}  end_node 类型 = {type(end.instruction).__name__}")

# 步骤 2：画出 qkv -> partial -> oproj 的依赖边（停到 oproj）
nodes, end = make_dag(globs, stop_after_op="oproj", layer_limit=1)
idx = {id(n): i for i, n in enumerate(nodes)}     # 用 id 建索引，避免依赖 __eq__
for n in nodes:
    op = type(n.instruction).__name__
    deps = [idx[id(d)] for d in n.dependencies]
    print(f"node#{idx[id(n)]:>3} {op:<32} deps={deps}")
```

**需要观察的现象**：

- 步骤 1 中，`stop_after_op` 每往后推进一个阶段，节点数**单调增加**，且 `end_node` 始终是 `NoOp`。
- 步骤 2 中：
  - `qkv` 节点数应等于 `sm_count`（=8）。
  - 每个 `PartialAttention` 节点的 `deps` **远少于** qkv 总数（通常只有 2 个），证明是细粒度依赖。
  - 每个 `O_ProjResidual` 节点的 `deps` 包含**全部** partial 节点（=4 个），证明 oproj 是全连接 fan-in。

**预期结果**（基于上述桩配置，由代码算术推出，待本地验证）：

| `stop_after_op` | 累计节点数 | 增量阶段 |
| --- | --- | --- |
| `qkv` | 8 | +8 qkv（= sm_count） |
| `partial` | 12 | +4 partial（= num_kv_heads × 1） |
| `oproj` | 140 | +128 oproj（= hidden_size / o_proj_block_size = 2048/16） |
| `upgate` | 148 | +8 upgate（= sm_count） |
| `downproj` | 156 | +8 downproj（= sm_count） |
| `None`（完整层） | 156 | 同 downproj；注意 `layer_limit=1 < num_hidden_layers`，**不会**加 lm_head，但会附带一个独立的 `end_node`(NoOp) |

推导依据：节点数 = `sm_count` 或 `维度 / block_size`，见 [schedule_qkv](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L118-L142)、[schedule_upgate](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L145-L164)、[schedule_downproj](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L167-L213) 与 oproj 的 [L343-L354](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L343-L354)。

**亲手画依赖边**：把步骤 2 的 `PartialAttention` 行挑出来，例如会看到形如 `node#8 PartialAttention deps=[5, 6]`、`node#9 PartialAttention deps=[5, 7]`…… 把这些 `(qkv 节点编号 → partial 节点编号)` 连线，你就得到了 `qkv→partial` 的依赖图。再连上「所有 partial → 每个 oproj」的全连接边，即得到 `qkv→partial→oproj` 完整依赖图。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `layer_limit=1` 而把桩的 `num_hidden_layers` 也设成 1，会发生什么？

> **参考答案**：此时 `nlayers == globs.num_hidden_layers` 成立，`make_dag` 会进入 lm_head 分支（[L256](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L256)），额外追加 `rms_lm_head` 节点，并要求桩提供 `vocab_size`、`lm_head_block_size`。这就是实践里特意把 `num_hidden_layers` 设为 4 的原因——**只剖析单层时不想触发 lm_head**。

**练习 2**：`dep_set` 用集合推导后为什么还要 `list(dep_set)`？

> **参考答案**：`DAG_Node.dependencies` 字段类型是 `list`。集合推导已经完成了「按 block 去重」（同一条 qkv 写多个 block 时只保留一次），转成 list 只是为了匹配字段类型，并不改变依赖语义。

**练习 3**：oproj 依赖全部 partial、upgate 依赖全部 oproj，这种「全连接」相比 qkv→partial 的细粒度连接，代价是什么？

> **参考答案**：全连接意味着 fan-in/fan-out 很大（如 128 个 oproj 每个都依赖 4 个 partial → 512 条边），调度时这些节点必须等**全部**前驱完成才能就绪，并行度被压缩。而 qkv→partial 的细粒度连接让每个 partial 只要等 2 个 qkv，能更早开始。代码注释也提到（[L373](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L373)）downproj 还可以做得更细，目前是简化版。

---

### 4.3 Schedule 与 ScheduleBuilder：把 DAG 封装成可调度对象

#### 4.3.1 概念说明

`make_dag` 返回的 `(nodes, end_node)` 还很「裸」。`Schedule` 把它和全局状态 `globs` 打包成一个完整对象，并提供几种「拍平成线性指令流 / 分配到 SM」的方法。

`ScheduleBuilder` 则是一个**抽象基类**，定义了「构造一个 Schedule 的统一三步接口」：

```
build(model) = make_globals(model)        # 第 1 步：从模型造 globals
             → make_dag(globs, ...)        # 第 2 步：造 DAG
             → Schedule(globs, dag, end)   # 第 3 步：封装
```

不同模式（latency / throughput）各自继承 `ScheduleBuilder`，实现自己的 `make_globals` 和 `make_dag`，但共用同一套 `build()` 流程。这是典型的**模板方法 / 工厂模式**。

#### 4.3.2 核心流程

`Schedule` 持有三样东西：`globs`（全局状态）、`dag_nodes`（节点列表）、`end_node`（汇点）。它的方法分两类：

- **拍平**：`get_linear_instructions()` 把 DAG 节点按（拓扑）顺序取出指令列表——**注意它假设 `dag_nodes` 已经是拓扑序**（见下方注释）。
- **分配到 SM**：`smart_assign_to_sms()`（= DAG 列表调度）、`round_robin_assign_to_sms()`（轮询）。这些属于 u4 的内容，本讲只了解接口存在。

`ScheduleBuilder` 的 `build()` 是模板方法；`with_new_globals()` 则是一个便利方法：**保留旧 DAG 结构，只换一份新的 globals**（如换了 batch 或设备时复用 DAG）。

#### 4.3.3 源码精读

`Schedule` 定义：

```python
@dataclass
class Schedule:
    globs: BaseGlobals
    dag_nodes: list[DAG_Node]
    end_node: DAG_Node

    def get_linear_instructions(self):
        # NOTE: assumes this is in topological order
        return [node.instruction for node in self.dag_nodes]

    def smart_assign_to_sms(self):
        return assign_dag_to_sms(self)

    def round_robin_assign_to_sms(self):
        instructions = self.get_linear_instructions()
        return round_robin_assign_to_sms(instructions, self.globs.sm_count())
```

见 [megakernels/scheduler.py:49-64](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L49-L64)。重点是 [L55-L57](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L55-L57) 那行注释：`get_linear_instructions` **不做**拓扑排序，只是把节点按列表顺序的指令取出来。之所以能这样，是因为 `make_dag` 是**按层、按阶段顺序 append** 节点的，天然满足「依赖在前、被依赖在后」的拓扑序。

`ScheduleBuilder` 抽象基类：

```python
class ScheduleBuilder:
    @classmethod
    def make_globals(cls, model):          # 子类实现
        raise NotImplementedError
    @classmethod
    def make_dag(cls, globs, stop_after_op=None, layer_limit=None):  # 子类实现
        raise NotImplementedError

    @classmethod
    def build(cls, model, stop_after_op=None, layer_limit=None):
        globs = cls.make_globals(model)
        dag_nodes, end_node = cls.make_dag(globs, stop_after_op, layer_limit)
        return Schedule(globs, dag_nodes, end_node)

    @classmethod
    def with_new_globals(cls, schedule, model):
        return replace(schedule, globs=cls.make_globals(model))
```

见 [megakernels/scheduler.py:67-91](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L67-L91)。`build()` 见 [L78-L87](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L78-L87)，`with_new_globals` 见 [L89-L91](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L89-L91)（用 `dataclasses.replace` 生成一个只替换 `globs` 的新 Schedule）。

latency 的具体实现：

```python
class LatencyScheduleBuilder(ScheduleBuilder):
    @classmethod
    def make_globals(cls, model):
        return make_globals(model)
    @classmethod
    def make_dag(cls, globs, stop_after_op=None, layer_limit=None):
        return make_dag(globs, stop_after_op, layer_limit)
```

见 [megakernels/demos/latency/scheduler.py:389-398](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/latency/scheduler.py#L389-L398)。它把抽象方法分别委托给本模块的 `make_globals` 和 `make_dag`，`build()` 直接复用基类。`dispatch.py` 通过 `BUILDER_MAP = {"latency": LatencyScheduleBuilder, "throughput": ThroughputScheduleBuilder}` 在运行时挑选具体子类（见 [dispatch.py:17-20](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/dispatch.py#L17-L20)）。

#### 4.3.4 代码实践

**目标**：用 `LatencyScheduleBuilder.make_dag` 复用本讲主实践的桩，观察 `Schedule` 的三个字段与 `get_linear_instructions` 的「拓扑序假设」。

**操作步骤**：

```python
# 示例代码：复用 4.2.4 的 globs 桩，组装一个 Schedule
import types
from megakernels.scheduler import Schedule
from megakernels.demos.latency.scheduler import LatencyScheduleBuilder

globs = types.SimpleNamespace(
    skip_attn_reduction=True, num_attention_heads=16, num_kv_heads=4,
    head_dim=128, hidden_size=2048, intermediate_size=8192,
    num_hidden_layers=4, vocab_size=128256,
    qkv_block_size=16, o_proj_block_size=16, up_gate_proj_block_size=16,
    down_proj_block_size=16, lm_head_block_size=16,
)
globs.sm_count = lambda: 8

# 直接用 make_dag（build 会顺带 make_globals，这里用桩绕开）
dag_nodes, end_node = LatencyScheduleBuilder.make_dag(globs, layer_limit=1)
sched = Schedule(globs=globs, dag_nodes=dag_nodes, end_node=end_node)

print("dag_nodes:", len(sched.dag_nodes), "end_node:", type(sched.end_node.instruction).__name__)
opcodes = [type(n.instruction).opcode() for n in sched.dag_nodes]
print("指令 opcode 出现的种类:", sorted(set(opcodes)))     # 期望 {1(qkv),2(partial),4(oproj),5(upgate),6(downproj)}

# 验证拓扑序假设：每个节点的依赖都出现在它之前
idx = {id(n): i for i, n in enumerate(sched.dag_nodes)}
ok = all(idx[id(d)] < idx[id(n)] for n in sched.dag_nodes for d in n.dependencies)
print("dag_nodes 是否满足拓扑序:", ok)
```

**需要观察的现象**：

- `end_node` 是 `NoOp`。
- 单层（`layer_limit=1`）的 opcode 种类是 `{1, 2, 4, 5, 6}`——**没有 3（AttentionReduction）**，印证了 latency 跳过 reduction 的设计。
- 拓扑序检查输出 `True`，印证 `get_linear_instructions` 「假设已拓扑序」的前提成立。

**预期结果**：上述三项均符合描述（具体计数待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么不直接在 `get_linear_instructions` 里做一次拓扑排序，而是要求调用方保证 `dag_nodes` 已是拓扑序？

> **参考答案**：`make_dag` 本就是按层、按阶段顺序 append 的，建出来的列表天然是拓扑序。每次取指令都重排是浪费。用注释把这条不变量显式记下来（[L56](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py#L56)），是「信任构造、不在热路径上重复校验」的工程取舍。

**练习 2**：`with_new_globals` 为什么有用？它复用了什么、没有复用什么？

> **参考答案**：DAG 的**结构**（哪些节点、怎么连边）只依赖模型形状，与具体权重/buffer 无关；而 `globs` 里的张量会随 batch、位置、设备变化。`with_new_globals` 用 `dataclasses.replace` 只换 `globs`，**复用了整张 DAG**（省去重新连边的开销），适合「同一模型反复 decode 不同 token」的场景——每步 decode 几乎只需更新 `pos_id`、KV cache 等少量字段。

---

## 5. 综合实践

**任务**：用本讲学到的全部知识，手工「审计」一个单层 DAG，并回答三个问题。

复用 4.2.4 / 4.3.4 的 globs 桩（`num_hidden_layers=4, layer_limit=1, sm_count=8`），完成：

1. **节点计数审计**：依次用 `stop_after_op = qkv → partial → oproj → upgate → downproj` 构建，记录每步累计节点数，确认每步增量等于「该阶段切分数」（qkv/upgate/downproj = sm_count=8；partial = num_kv_heads=4；oproj = 2048/16=128）。

2. **依赖边审计**：构建到 `oproj`，写出每条 `PartialAttention` 的依赖 qkv 节点编号。验证「每条 partial 恰好依赖 2 个 qkv 节点（一个写 K、一个写 V）」。再任取一个 `O_ProjResidual`，验证它依赖**全部 4 个** partial。

3. **封装审计**：用 `Schedule` 封装，验证 (a) `end_node` 是 `NoOp`；(b) opcode 种类不含 3（AttentionReduction）；(c) `dag_nodes` 满足拓扑序。

**进阶思考**：如果把 `sm_count` 改大（比如 16），qkv→partial 的依赖边数量会怎么变？partial 的依赖个数会变吗？（提示：partial 依赖的是「写出特定 block 的 qkv 节点」，qkv 切得更细只会让 block→节点的映射更细，但一条 partial 仍只读固定的 K/V block，依赖个数通常仍为 2，只是被依赖的节点编号会变。）

**预期结果**：三项审计全部通过；进阶问题中 partial 依赖个数基本不变（待本地验证）。

---

## 6. 本讲小结

- `DAG_Node` 是「带依赖信息的指令信封」：`dependencies`（我依赖谁，构建期）、`children`（谁依赖我，反向投影）、`remaining_dependencies`（调度期递减、为空即就绪）；它靠 `instruction.serialize()` 实现哈希，因而能进 set、当 dict key。
- `make_dag_layer` 按 `qkv→partial→oproj→upgate→downproj` 的数据流连依赖：其中 **qkv→partial 是按 block 反查的细粒度边**（partial 只依赖写出自己 K/V block 的 qkv），其余三段是「依赖上一阶段全部节点」的全连接。
- `PartialAttention.prev_opcode()` 复用 u3-l2 的 opcode/prev_opcode 配对，保证 partial 用 qkv 的 opcode 去 `qkv_deps` 表里查生产者。
- `stop_after_op` 是逐阶段早停开关，配合 `layer_limit` 可只剖析单层（注意 `layer_limit < num_hidden_layers` 才不会触发 lm_head 分支）。
- `make_dag` 用一个 `NoOp` 的 `end_node` 收尾，给整张图一个唯一汇点。
- `Schedule(globs, dag_nodes, end_node)` 封装 DAG；`get_linear_instructions` 依赖「dag_nodes 已是拓扑序」这一构造保证；`ScheduleBuilder.build()` 是模板方法（`make_globals → make_dag → Schedule`），`LatencyScheduleBuilder` 子类只实现两个 make 方法。

---

## 7. 下一步学习建议

本讲我们得到了一个**结构完整、已封装的 `Schedule`**，但还没说「这些节点怎么排到各 SM 的队列里」。下一讲 **u4-l1「SM 分配策略 rr / zz / wave / dag / pool」** 会回到 [scheduler.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/scheduler.py) 后半部分，重点读：

- `assign_dag_to_sms`——它正是用到本讲的 `remaining_dependencies` / `children` / `register_with_parents` / `calc_priority` 的那个「DAG 列表调度器」，用双堆把就绪节点按 cost 分配到最先空闲的 SM。建议先把本讲的 `remaining_dependencies` 生命周期理顺，再去读它的主循环。
- 再之后 **u4-l2** 讲 `tensorize_instructions`，把分配好的 SM 队列补 `NoOp`、`serialize_and_pad` 成 `[num_sms, queue_len, 32]` 的张量——你会再次看到 `NoOp` 的妙用。

延伸阅读：可对照 [megakernels/demos/throughput/scheduler.py](https://github.com/HazyResearch/Megakernels/blob/7309cec801537b61fea3b50d7dfe454a6cde578e/megakernels/demos/throughput/scheduler.py) 里的 `ThroughputScheduleBuilder`，看另一套指令如何复用同一个 `ScheduleBuilder` / `Schedule` 骨架。
