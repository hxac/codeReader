# Graph 数据结构与 GraphDef

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 TensorFlow 在 C++ 内核里用什么数据结构表示「计算图」——即 `Graph`、`Node`、`Edge` 三件套构成的**有向无环图（DAG）**。
- 区分两种「图」：**运行时的 `Graph`**（带指针、可增删、服务于执行）与**序列化的 `GraphDef`**（一段可落盘、可跨进程传输的 protobuf 文本），并解释二者如何互转。
- 理解 Python 侧的 `FuncGraph` 在 `ops.Graph` 之上加了什么——`inputs/outputs/captures/outer_graph`——以及它为何是 `tf.function` 追踪（tracing）的产物。

本讲是「计算图与执行模型」单元的第一讲，承上（u2-l4 的 Operation/Tensor 对象关系）启下（u3-l2 的 Session 执行链路）。请记住一句话：**图是 TensorFlow 的「中间语言」，几乎所有优化、序列化、分布式、编译（XLA/MLIR）都建立在它之上。**

## 2. 前置知识

- **有向无环图（DAG）**：一组节点用带方向的边连起来，沿边的方向走不会回到起点。你可以把它想象成一张「菜谱依赖图」——每道工序（节点）要等它依赖的工序（入边）做完才能开工。
- **Operation 与 Tensor 的关系**（来自 u2-l4）：一个 op 消费若干输入 Tensor、产出若干输出 Tensor。本讲把它们提升到 C++ 层，称为 **Node**（节点）和带「输入/输出端口编号」的 **Edge**（边）。
- **protobuf（协议缓冲）**：Google 的跨语言序列化格式。TensorFlow 用 `.proto` 文件定义消息结构，编译出 C++/Python 等语言的类。`GraphDef`、`NodeDef` 都是 protobuf 消息。
- **Python 的 `tf.function` 与 ConcreteFunction**（将在 u3-l4 详讲，这里只需直觉）：被 `@tf.function` 装饰的 Python 函数，在被调用时会「追踪」出一张图，封装成 `ConcreteFunction`，其 `.graph` 就是一个 `FuncGraph`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `tensorflow/core/graph/graph.h` | 定义 C++ 运行时图的三件套：`Node`、`Edge`、`Graph` 类及其内联方法。 |
| `tensorflow/core/graph/graph.cc` | 上述类的实现：图的构造（自动建 `_SOURCE`/`_SINK`）、`AddNode`/`AddEdge`/`AddControlEdge`、序列化 `ToGraphDef`。 |
| `tensorflow/core/framework/graph.proto` | 序列化图的消息定义：`GraphDef`（`repeated NodeDef node` + `VersionDef` + 函数库）。 |
| `tensorflow/core/framework/node_def.proto` | 单个节点的序列化定义：`NodeDef`（`name`/`op`/`input`/`device`/`attr`），重点看 `input` 字符串格式。 |
| `tensorflow/python/framework/ops.py` | Python 侧 `Graph` 类（继承自 `pywrap_tf_session.PyGraph`，直接桥接 C++ 图）。 |
| `tensorflow/python/framework/func_graph.py` | `FuncGraph(ops.Graph)`：表示「函数体」的图，`tf.function` 追踪的核心产物。 |

记忆要点：**C++ 的 `Graph` 是「活的」运行时对象；`GraphDef` 是它「躺平」后的序列化形态；Python 的 `FuncGraph` 是给 `tf.function` 用的、带输入输出与捕获语义的 `Graph` 子类。**

---

## 4. 核心概念与源码讲解

### 4.1 运行时有向图模型：Graph / Node / Edge

#### 4.1.1 概念说明

在 C++ 内核里，一张计算图就是一个 DAG：

- **Node（节点）**：表示一次计算（一个 op 的实例），如 `Add`、`MatMul`、`Const`。它持有自己的名字、类型、属性（`NodeDef`）、输入输出类型，以及与它相连的边集合。
- **Edge（边）**：表示依赖。一条边既可能是**数据依赖**（A 的某个输出流到 B 的某个输入），也可能是**控制依赖**（B 必须等 A 执行完，但不传数据）。
- **Graph（图）**：拥有并管理所有 Node 与 Edge 的容器，提供增删节点、增删边、序列化等能力。

文件开头的一大段注释把模型讲得很清楚——DAG、source/sink、用「输入端口/输出端口」编号标注数据流向：

[core/graph/graph.h:16-35](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/graph/graph.h#L16-L35) —— 注释说明：内部节点表示要执行的计算，边表示「目标节点必须在源节点完成后才能执行」，且图中预置了唯一的 `source`（起点）与 `sink`（终点）。

这条「边带端口编号」的设计是理解全栈的关键。因为一个 op 可以有多个输出、多个输入，光说「A 连到 B」不够，必须说清「A 的第几个输出连到 B 的第几个输入」。所以一条边实际携带四个信息：

\[
\text{Edge} = (\text{src},\ \text{src\_output},\ \text{dst},\ \text{dst\_input})
\]

#### 4.1.2 核心流程

一张图的生命周期大致是：

1. **构造空图**：`new Graph(ops)` 时，自动创建两个特殊节点 `_SOURCE`（id 恒为 0）与 `_SINK`（id 恒为 1），并在它们之间连一条控制边。后续业务节点 id 从 2 开始递增。
2. **加节点**：调用 `AddNode(NodeDef)`，到 op 注册表里查 op 定义，推导输入输出类型，分配一个 `Node`。
3. **加边**：调用 `AddEdge(src, x, dst, y)`，把 `src` 的第 `x` 个输出连到 `dst` 的第 `y` 个输入；控制边用特殊值 `kControlSlot = -1`。
4. **优化/执行/序列化**：图可被 Grappler 优化（u6-l3）、被 Executor 执行（u3-l2）、或被 `ToGraphDef` 序列化。

伪代码描述加边：

```
AddEdge(src, x, dst, y):
    e = 分配一个 Edge（优先从 free_edges_ 复用，否则 arena 分配）
    e.src_, e.dst_ = src, dst
    e.src_output_, e.dst_input_ = x, y
    src.out_edges_.insert(e)      # 源节点记一条出边
    dst.in_edges_.insert(e)       # 目标节点记一条入边
    edges_.push_back(e)           # 图的全局边表也登记
```

#### 4.1.3 源码精读

**Graph 类本体**——注意它的私有成员：`nodes_`（按 id 索引的节点指针数组）、`edges_`（按 id 索引的边指针数组）、`arena_`（内存竞技场，集中分配 Node/Edge 以提升局部性）、`free_nodes_`/`free_edges_`（被删后可复用的空闲槽位）：

[core/graph/graph.h:531-631](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/graph/graph.h#L531-L631) —— `Graph` 类的公有接口：构造、`AddNode`/`CopyNode`/`RemoveNode`、`AddEdge`/`AddControlEdge`/`RemoveEdge`、`num_nodes`/`num_edges` 等核心方法。

**构造函数自动建 source/sink**：这是「每个图都有唯一起点和终点」不变量的来源。`_SOURCE` 和 `_SINK` 都是 `NoOp`（空操作），靠一条控制边相连：

[core/graph/graph.cc:440-466](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/graph/graph.cc#L440-L466) —— `Graph(ops)` 构造函数：先填版本号，再把 `_SOURCE`/`_SINK` 作为前两个节点加入，最后用 `AddControlEdge(source, sink)` 连起来。

**source/sink 的判定与 id 约定**：靠 id 区分（0=source，1=sink，>1 才是普通 op 节点）：

[core/graph/graph.h:159-162](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/graph/graph.h#L159-L162) —— `IsSource()`/`IsSink()`/`IsOp()` 三个判定方法；`num_op_nodes()` 因此等于「总节点数 − 2」（排除 source/sink）。

**Node 类的关键属性**：名字、类型、输入输出类型、入边出边集合，以及保存「用户原始 NodeDef + OpDef + 类型向量」的 `props_`：

[core/graph/graph.h:85-156](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/graph/graph.h#L85-L156) —— `Node` 类公有接口：`name()`/`type_string()`/`def()`/`num_inputs()`/`num_outputs()`、`in_edges()`/`out_edges()`、以及 `IsConstant()`/`IsVariable()`/`IsControlFlow()` 等按 `NodeClass` 分类的判定。

**Edge 类——整张图最「纯粹」的数据结构**：五个字段（源、源端口、目标、目标端口、id），没有任何方法做「计算」，只描述连接关系：

[core/graph/graph.h:435-468](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/graph/graph.h#L435-L468) —— `Edge` 类：`src()`/`dst()`/`src_output()`/`dst_input()` 四个取值器，`IsControlEdge()` 判定；私有字段 `src_`/`dst_`/`id_`/`src_output_`/`dst_input_`。

**控制边约定 `kControlSlot`**：用 `-1` 这个特殊端口值表示「这不是数据流，只是执行顺序约束」：

[core/graph/graph.h:555](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/graph/graph.h#L555) —— `static constexpr int kControlSlot = -1;`，数据端口的合法值是 `>=0`，`-1` 专门留给控制边。

**AddEdge 的实现**：注意它用「placement new + arena」分配 Edge，并把同一条边同时插入源节点的出边集与目标节点的入边集（双向链接）：

[core/graph/graph.cc:638-668](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/graph/graph.cc#L638-L668) —— `AddEdge(source, x, dest, y)`：从 `free_edges_` 复用或 arena 新建 Edge，填入四元组，分别 `insert` 进两端的边集合，并登记到全局 `edges_`。

**AddNode 的实现**：到 op 注册表查 op 定义 → 推导输入输出类型 → `AllocateNode` 分配节点：

[core/graph/graph.cc:549-593](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/graph/graph.cc#L549-L593) —— `AddNode`：`ops_.LookUp` 查 op、`InOutTypesForNode` 推类型、用 `NodeProperties`（含 `op_def`、`node_def`、输入输出类型向量）构造并 `AllocateNode`。

**节点的 arena 分配**：Node/Edge 不走普通 `new`，而走 `core::Arena`，被删后进 `free_nodes_`/`free_edges_` 供复用，id 不回收（因此 `nodes_` 数组里可能有空洞）：

[core/graph/graph.cc:967-983](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/graph/graph.cc#L967-L983) —— `AllocateNode`：`arena_.Alloc(sizeof(Node))` placement new，赋 `graph_` 回指针，`id = nodes_.size()`，`Initialize` 后 `push_back`。

> 小贴士：因为 id 不回收，`num_node_ids()`（数组长度）往往大于 `num_nodes()`（存活数）。要按 id 建索引数组时用前者，要数「当前有几个节点」用后者——这点在 [graph.h:659-679](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/graph/graph.h#L659-L679) 的注释里讲得很清楚。

#### 4.1.4 代码实践

**实践目标**：在不跑任何代码的前提下，纯靠读源码，把「一条边携带哪四个信息」「source/sink 的 id 是几」内化。

**操作步骤**：

1. 打开 [graph.h:435-468](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/graph/graph.h#L435-L468)，抄下 Edge 的五个私有字段。
2. 打开 [graph.cc:440-466](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/graph/graph.cc#L440-L466)，找到 `_SOURCE` 与 `_SINK` 是在第几、第几个被 `AddNode` 的。
3. 思考：如果我想表示「A 的第 2 个输出连到 B 的第 0 个输入」，调用该写成 `AddEdge(A, ?, B, ?)` 里的两个端口值分别是什么？

**需要观察的现象**：源节点只有「出边」概念会出现在 `AddEdge` 里，目标节点只有「入边」；同一条 Edge 对象同时存在于两个 `EdgeSet` 中。

**预期结果**：端口值依次是 `2` 和 `0`（即 `AddEdge(A, 2, B, 0)`）。`_SOURCE` 的 id 是 0、`_SINK` 的 id 是 1。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Graph` 要预先创建 `_SOURCE` 和 `_SINK` 两个特殊节点，而不是让用户图直接「裸跑」？

> **参考答案**：为了给整张图提供唯一的「起点」和「终点」。执行器（Executor）需要从没有数据依赖的节点开始调度、到没有任何消费者的节点结束；用两个固定哨兵节点统一处理「无入边的节点」和「无出边的节点」，能让调度、放置、图分区等算法的边界条件大大简化（`source` 永远是唯一不依赖任何东西的节点，`sink` 永远是唯一不被依赖的节点）。

**练习 2**：`Edge::IsControlEdge()` 是怎么判断一条边是不是控制边的？为什么只判断 `src_output_ == kControlSlot` 就够了？

> **参考答案**：见 [graph.h:1071-1075](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/graph/graph.h#L1071-L1075)。它判断 `src_output_ == Graph::kControlSlot`。注释指出：`AddEdge` 会保证「若 `src_output_` 或 `dst_input_` 之一是 `kControlSlot`，另一个也一定是」（见 [graph.cc:644-648](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/graph/graph.cc#L644-L648) 的 `DCHECK`），所以只看一个即可。

---

### 4.2 GraphDef：图的序列化形态

#### 4.2.1 概念说明

`Graph` 是内存里「活的」对象——节点是指针、边是对象、还能随时增删。但很多时候我们需要把图**保存到磁盘**（SavedModel，u5-l3）、**跨进程传输**（分布式训练，u6-l4）、或**喂给编译器**（XLA/MLIR，u7）。这就需要一个「躺平的、可序列化」的表示：**`GraphDef`**。

`GraphDef` 是一个 protobuf 消息，本质是**一张扁平的 `NodeDef` 列表**，外加版本号和函数库。它和运行时 `Graph` 的根本区别在于**边的表示方式**：

- 运行时 `Graph`：边是**显式的 `Edge` 对象**，带指针，`O(1)` 双向遍历。
- `GraphDef`：**没有独立的边对象**。每条边被「折叠」进目标节点的 `input` 字符串字段里。

#### 4.2.2 核心流程

`GraphDef` 的核心结构：

```
GraphDef {
    repeated NodeDef node      # 扁平的节点列表
    VersionDef versions        # 图格式版本（producer / min_consumer / bad_consumers）
    FunctionDefLibrary library # 函数库（tf.function 定义的子图）
    GraphDebugInfo debug_info  # 调试信息（节点对应的 Python 源码栈）
}
```

每个 `NodeDef` 里，`input` 字段是**字符串列表**，编码规则如下（见 proto 注释）：

| `input` 字符串 | 含义 |
| --- | --- |
| `"A"` | 取节点 `A` 的第 0 个输出（`:0` 可省略） |
| `"A:2"` | 取节点 `A` 的第 2 个输出 |
| `"^A"` | 对节点 `A` 的控制依赖（不传数据，只约束顺序） |

所以「运行时一条带端口的边」在 `GraphDef` 里被压成「目标节点 input 列表里的一行字符串」。

序列化方向（`Graph → GraphDef`）由 `Graph::ToGraphDef` 完成；反序列化方向（`GraphDef → Graph`）由图构造工具（如 `ConvertGraphDefToGraph`）完成，本讲只看前者。流程：

1. 写入版本号 `versions` 与函数库 `library`。
2. 遍历图中**每个普通 op 节点**（跳过 source/sink）。
3. 对每个节点，按端口顺序收集数据入边对应的 `src_name:src_output`，再追加控制入边对应的 `^src_name`，控制边还要排序以保证序列化稳定。
4. 把这些字符串写进 `NodeDef.input`。

#### 4.2.3 源码精读

**GraphDef 消息定义**：

[core/framework/graph.proto:32-75](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/framework/graph.proto#L32-L75) —— `message GraphDef`：`repeated NodeDef node`（节点列表）、`VersionDef versions`（版本）、`FunctionDefLibrary library`（函数库）、`GraphDebugInfo debug_info`。

**NodeDef 与 input 字符串格式**：

[core/framework/node_def.proto:29-66](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/framework/node_def.proto#L29-L66) —— `message NodeDef`：`name`/`op`/`repeated string input`/`device`/`map<string,AttrValue> attr`。`input` 的注释精确说明了 `"node:src_output"` 与控制输入 `"^node"` 两种格式。

**序列化主逻辑 ToGraphDefSubRange**——这是本模块最重要的一段代码，它演示了「边如何被折叠成字符串」：

[core/graph/graph.cc:843-916](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/graph/graph.cc#L843-L916) —— `ToGraphDefSubRange`：对每个 `IsOp()` 的节点，先用 `num_inputs()` 个槽位按 `dst_input` 放数据边，再把控制边 `push_back` 到末尾，最后对控制边按 `src->name()` 排序，逐条用 `AddInput` 写成 `"name:slot"` 或 `"^name"` 字符串。

注意其中三个细节：
- `if (node == nullptr || !node->IsOp()) continue;`（[L862](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/graph/graph.cc#L862)）——**source/sink 不进 GraphDef**，因为它们 `IsOp()` 为假（id ≤ 1）。
- 控制边放在数据边之后（[L876-879](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/graph/graph.cc#L876-L879)）。
- 控制边排序（[L894-897](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/graph/graph.cc#L894-L897)）保证同样的图每次序列化出的字符串顺序一致（可复现）。

**字符串拼装的辅助函数 AddInput**：

[core/graph/graph.cc:757-765](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/graph/graph.cc#L757-L765) —— `Graph::AddInput`：`kControlSlot` → `"^name"`，slot 0 → 省略 `":0"`，否则 → `"name:slot"`。这正是上表三条规则的代码出处。

**版本号 VersionDef**——区分「图格式版本」与「TF 发布版本」（u1-l5 已讲过双轨版本号）：

[core/framework/graph.proto:38](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/framework/graph.proto#L38) 与 [core/framework/versions.proto:39-47](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/framework/versions.proto#L39-L47) —— `VersionDef` 含 `producer`/`min_consumer`/`bad_consumers`，用于判断「这个 GraphDef 能否被当前运行时接受」。

#### 4.2.4 代码实践

**实践目标**：亲手验证「运行时 `Graph` 的边」如何变成「`GraphDef` 里的 input 字符串」，并确认 source/sink 不出现在序列化结果里。

**操作步骤**（运行环境：装好 `tensorflow` 的 Python）：

```python
# 示例代码
import tensorflow as tf

@tf.function
def small(x):
    a = tf.add(x, x)          # 节点 add
    b = tf.multiply(a, x)     # 节点 mul，依赖 add 的输出
    return b

cf = small.get_concrete_function(tf.TensorSpec([], tf.float32))

# 运行时形态：遍历 Operation（对应运行时 Node）
for op in cf.graph.get_operations():
    print(op.name, "|", op.type, "| inputs:",
          [f"{t.op.name}:{t.value_index}" for t in op.inputs])

# 序列化形态：导出 GraphDef
gd = cf.graph.as_graph_def()
for n in gd.node:
    print(n.name, "|", n.op, "| input:", list(n.input))
print("GraphDef 中节点数:", len(gd.node))
```

**需要观察的现象**：
- 运行时输出里，`mul` 这个 op 的 `inputs` 会包含形如 `add:0` 的张量引用——这正是一条「带端口的边」。
- 序列化输出里，`mul` 对应的 `NodeDef.input` 是一串字符串（如 `["add", "<占位符名>"]`），**看不到任何 Edge 对象**。
- `len(gd.node)` 里**不会**出现 `_SOURCE`/`_SINK`（因为序列化时被 `IsOp()` 过滤）。
- 节点 `name` 的具体取值依赖 TF 版本的自动命名策略，**待本地验证**。

**预期结果**：你会直观看到「同一条依赖」在两种形态下的不同表示：运行时是「对象指针 + 端口」，序列化是「目标节点 input 列表里的一行字符串」。

#### 4.2.5 小练习与答案

**练习 1**：`GraphDef` 为什么不单独定义一个 `Edge` 消息，而是把边折叠进 `NodeDef.input`？

> **参考答案**：因为 protobuf 适合「按节点遍历」的扁平表示，而 TF 的边天然「依附于目标节点的某个输入端口」。把边折叠成 `input` 字符串后，一个 `NodeDef` 就能自描述「我是谁、我要消费谁的哪个输出」，序列化/反序列化、跨语言、可视化都更简单。代价是：从 `GraphDef` 反查「某个输出被谁消费」需要扫全表，不如运行时 `Graph` 的 `out_edges_` 高效——所以**执行前通常会把 `GraphDef` 重建为运行时 `Graph`**。

**练习 2**：同一个运行时 `Graph`，连续两次调用 `ToGraphDef`，得到的 `GraphDef` 是否字节级一致？为什么控制边要排序？

> **参考答案**：基本一致。运行时 `in_edges_`/`out_edges_` 是 `EdgeSet`（集合），遍历顺序不保证稳定，若直接序列化会让 `input` 列表顺序随机波动。代码因此对控制边按 `src->name()` 排序（[graph.cc:894-897](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/core/graph/graph.cc#L894-L897)），目的是让序列化**可复现**，便于 diff、缓存命中与测试断言。

---

### 4.3 Python 侧的 FuncGraph

#### 4.3.1 概念说明

C++ 的 `Graph` 通用且强大，但 Python 用户不会直接 `new Graph()`。在 TF2 中，图几乎总是 `tf.function` 追踪的产物。追踪出的「函数体」需要一个比普通 `Graph` 更丰富的容器——**`FuncGraph`**。

`FuncGraph` 继承自 Python 的 `ops.Graph`（后者又继承自 `pywrap_tf_session.PyGraph`，即直接桥接 C++ 图）。所以一个 `FuncGraph` **既是** Python 对象，**底层也持有一张 C++ `Graph`**。它在普通图之上多记录了「函数」特有的一切：

- `inputs` / `outputs`：函数的输入占位符与输出张量（对应 C++ 函数的 `_Arg`/`_Retval` 语义，但在 Python 层用普通 Tensor 表达）。
- `control_outputs`：函数执行完毕前必须完成的 op。
- `captures`：**捕获**——把外部闭包变量映射成函数内部的占位符。
- `outer_graph`：外层图，支持函数嵌套（`tf.function` 里再套 `tf.function`）。
- `structured_input_signature` / `structured_outputs`：保留 Python 嵌套结构（list/dict）的输入输出签名。

其中最独特的是 **capture（捕获）**。Python 闭包可以引用外部变量：

```python
x = tf.constant(2.0)
@tf.function
def f(y):
    return y + x   # x 是外部闭包变量，被「捕获」进函数图
```

追踪时，`x` 属于外层图，不能直接放进 `f` 的图里。`FuncGraph.capture(x)` 会为它在函数内部创建一个占位符，并记录 `(外部 x → 内部占位符)` 的映射，调用时再把 `x` 的值喂给这个占位符。

#### 4.3.2 核心流程

`FuncGraph` 的典型诞生路径（`tf.function` 追踪，u3-l4 详讲）由 `func_graph_from_py_func` 驱动：

1. `new FuncGraph(name)`，`_building_function = True`，记录 `outer_graph`。
2. `with func_graph.as_default()`：把它设为当前默认图，使后续 `tf.add` 等 op 落进这张图。
3. 为函数参数创建占位符（`_create_placeholders`），填入 `func_graph.inputs`。
4. **执行用户 Python 函数**：函数体里每写一个 op，都会触发 `FuncGraph._create_op_internal`，它会把外部输入张量自动 `capture` 成内部占位符。
5. 收集函数返回值到 `func_graph.outputs`，并对闭包返回值再 `capture` 一次。
6. （可选）`AutomaticControlDependencies` 给有副作用的 op 补控制边，写入 `control_outputs`。

op 创建时的「捕获拦截」是 `FuncGraph` 区别于普通 `Graph` 的核心逻辑：每个外部输入 tensor 都被 `self.capture(inp)` 替换成内部占位符后再交给父类建 op。

#### 4.3.3 源码精读

**Python `Graph` 类的来源**——它直接继承 C 扩展类型 `PyGraph`，说明 Python 图与 C++ 图是「同一物」的薄包装：

[python/framework/ops.py:2030-2076](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/ops.py#L2030-L2076) —— `class Graph(pywrap_tf_session.PyGraph)`：文档说明图是「数据流图」，含一组 `Operation`（计算单元）与 `Tensor`（数据单元）；`__init__` 里维护名字表、设备栈、控制流上下文、collections、`_graph_def_versions` 等 Python 侧状态。

**FuncGraph 类定义与文档**——重点看它新增的属性 `inputs`/`outputs`/`control_outputs`/`captures`/`outer_graph`：

[python/framework/func_graph.py:133-161](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/func_graph.py#L133-L161) —— `class FuncGraph(ops.Graph)`，文档逐项解释属性：`inputs` 是代表函数输入的占位符（含被捕获的），`outputs` 是返回张量，`captures` 把外部 tensor 映射到内部占位符，`outer_graph` 是定义它的外层图。

**FuncGraph.__init__ 的关键字段初始化**：

[python/framework/func_graph.py:192-227](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/func_graph.py#L192-L227) —— 初始化 `self.inputs=[]`/`outputs=[]`/`control_outputs=[]`，用 `weakref` 持有 `outer_graph`（避免引用环），设 `self._building_function = True`，并用 `capture_container.FunctionCaptures()` 管理捕获。

**捕获入口 capture**——一行委托给 `_function_captures`：

[python/framework/func_graph.py:618-619](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/func_graph.py#L618-L619) —— `def capture(self, tensor, name=None, shape=None): return self._function_captures.capture_by_value(self, tensor, name)`。

**op 创建的捕获拦截 _create_op_internal**——这是理解「为何外部 tensor 能透明地出现在函数图里」的关键：遍历每个输入，先 `self.capture(inp)` 转成内部占位符，再调父类建 op：

[python/framework/func_graph.py:548-616](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/func_graph.py#L548-L616) —— `_create_op_internal`：对 `inputs` 逐个 `inp = self.capture(inp)`，把捕获后的 `captured_inputs` 交给 `super()._create_op_internal(...)`。

**追踪驱动 func_graph_from_py_func**——它把上面这些串起来：建图 → 设默认 → 建占位符 → 跑用户函数 → 收集输出：

[python/framework/func_graph.py:921-1003](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/func_graph.py#L921-L1003) —— `func_graph_from_py_func`：`with func_graph.as_default()` 内创建占位符、`func_graph.inputs = ...`、调用 `python_func(*func_args)` 触发追踪。

[python/framework/func_graph.py:1097-1107](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/func_graph.py#L1097-L1107) —— 追踪收尾：把「真实参数占位符 + 内部捕获 + 延迟捕获」拼成最终 `func_graph.inputs`，并对结构化输出里的每个 tensor 再 `capture` 后塞进 `func_graph.outputs`。

**三层关系一览**：

| 层 | 类 | 文件 | 职责 |
| --- | --- | --- | --- |
| C++ 运行时 | `Graph`/`Node`/`Edge` | `core/graph/graph.{h,cc}` | 真正存节点指针与边对象、执行 |
| Python 通用图 | `ops.Graph` | `python/framework/ops.py` | 桥接 C++ 图，加名字/设备/collections 等 Python 状态 |
| Python 函数图 | `FuncGraph` | `python/framework/func_graph.py` | 加 inputs/outputs/captures/outer_graph，服务 `tf.function` |

#### 4.3.4 代码实践

**实践目标**：构造一张小图，对照 `func_graph.py`，写出该图在运行时由哪些 Node、Edge 组成，并体会 `FuncGraph` 比 `Graph` 多了什么。

**操作步骤**：

```python
# 示例代码
import tensorflow as tf

external = tf.constant(3.0)   # 外部闭包变量，将被 capture

@tf.function
def body(a):
    s = tf.add(a, a)              # Node: add
    p = tf.multiply(s, external)  # Node: mul；external 来自外层，会被捕获
    return p

cf = body.get_concrete_function(tf.TensorSpec([], tf.float32, name="a"))
fg = cf.graph                      # 这是一个 FuncGraph

# (1) 运行时 Node：FuncGraph 里的 op（对照 func_graph.py 的 self.inputs/outputs）
print("inputs :", [t.op.name for t in fg.inputs])
print("outputs:", [t.op.name for t in fg.outputs])
for op in fg.get_operations():
    print(f"  Node {op.name}({op.type}) <- inputs:",
          [f"{t.op.name}:{t.value_index}" for t in op.inputs])

# (2) 运行时 Edge：由每个 op 的 inputs 反推（每对 op:idx -> op 是一条带端口的边）
# (3) 捕获：external 被映射成函数内部占位符
print("captures:", [(ext.op.name, ph.op.name) for ext, ph in fg.captures])
```

**需要观察的现象**：
- `fg.inputs` 既有真正的函数参数占位符（`a`），也有因 `external` 被捕获而产生的额外占位符——这正是 [func_graph.py:1097-1099](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/func_graph.py#L1097-L1099) 把「真实参数 + internal_captures」拼在一起的体现。
- `fg.captures` 里应能看到 `(external 的 op, 内部占位符)` 的映射。
- `fg` 的类型是 `FuncGraph`，而 `type(fg).__mro__` 会显示它继承自 `ops.Graph`。
- 具体节点名（占位符的自动命名）依赖 TF 版本，**待本地验证**。

**预期结果**：你能据此画出这张小图——
- 节点：参数占位符、被捕获的 `external` 占位符、`add`、`mul`（外加 C++ 层不可见的 `_SOURCE`/`_SINK`）。
- 边：`参数 → add(0)`、`参数 → add(1)`、`add(0) → mul(0)`、`external占位符 → mul(1)`。
- `FuncGraph` 比 `Graph` 多了 `inputs/outputs/captures/outer_graph`，这些正是它「作为函数体」的额外语义。

#### 4.3.5 小练习与答案

**练习 1**：`FuncGraph` 为什么要用 `weakref` 持有 `outer_graph`，而不是直接 `self.outer_graph = outer_graph`？

> **参考答案**：见 [func_graph.py:206-212](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/func_graph.py#L206-L212)。外层图通常持有内层 `FuncGraph`（或至少与它共同存活），若内层再强引用外层，会形成**引用环**，导致 Python 垃圾回收无法及时回收，造成内存泄漏（尤其在大量缓存 `ConcreteFunction` 的场景）。用弱引用打破环，并在外层图被回收后回退到 `_fallback_outer_graph`。

**练习 2**：如果在 `@tf.function` 的函数体里直接使用一个属于外层 eager 上下文的 tensor，运行时会发生什么？结合 `_create_op_internal` 说明。

> **参考答案**：该外部 tensor 会被自动捕获。在 [func_graph.py:607-613](https://github.com/tensorflow/tensorflow/blob/4efe77a0562d30d57b733ebb4adfa4ea1f930ecb/tensorflow/python/framework/func_graph.py#L607-L613)，`_create_op_internal` 遍历 `inputs`，对每个 `inp` 调 `self.capture(inp)`，把它替换为函数内部的占位符后再建 op；同时 `(外部 tensor, 内部占位符)` 被记进 `captures`。调用这个 ConcreteFunction 时，运行时会用捕获时记录的外部值去喂那个占位符。

---

## 5. 综合实践

**任务**：把本讲三个模块串起来——构造一张含「数据依赖 + 控制依赖 + 闭包捕获」的小图，分别从**运行时 `FuncGraph`**、**序列化 `GraphDef`**、**C++ 三件套**三个视角观察它，最后画成一张图。

**步骤**：

1. 写一个 `@tf.function`，函数体含：一次 `tf.add`（数据依赖）、一个外部常量（捕获）、一个用 `tf.control_dependencies` 加的控制依赖（或一个有副作用的 op 如 `tf.print`）。
2. 取 `cf = f.get_concrete_function(...)`，`fg = cf.graph`。
3. **运行时视角**：用 `fg.get_operations()` 列出所有 Node；用每个 op 的 `inputs` 列出所有带端口的数据 Edge；找出控制依赖对应的控制 Edge。
4. **序列化视角**：`gd = fg.as_graph_def()`，遍历 `gd.node`，核对：数据边表现为 `"name:idx"`、控制边表现为 `"^name"`、且 `_SOURCE`/`_SINK` 不在其中。
5. **C++ 视角**：回到本讲源码，回答——这些 Node 在 C++ `Graph` 里由 `AllocateNode` 分配、id 从 2 起；这些 Edge 由 `AddEdge` 同时插入两端的 `EdgeSet`；整张图可经 `ToGraphDefSubRange` 序列化成你第 4 步看到的 `gd`。
6. 画图：用纸笔或工具画出节点与边，标注每条边的 `(src_output, dst_input)` 端口，控制边用虚线。

**验收标准**：你能用一句话说清——**同一个 `add` 依赖，在 `FuncGraph` 里是 `mul.inputs[0]` 这个 Tensor 对象（指向 `add:0`），在 `GraphDef` 里是 `mul` 节点 `input` 字段里的字符串 `"add"`，在 C++ `Graph` 里是一条 `Edge(add, 0, mul, 0)` 对象**。三种表示，同一条依赖。

## 6. 本讲小结

- TensorFlow 在 C++ 内核用 **`Graph`/`Node`/`Edge`** 三件套表示计算图，本质是带「输入/输出端口编号」的 DAG；每条边携带 `(src, src_output, dst, dst_input)` 四元组。
- 每个 `Graph` 构造时自动建 `_SOURCE`（id 0）与 `_SINK`（id 1）两个哨兵节点并用控制边相连，作为唯一的起点与终点；普通 op 的 id 从 2 开始。
- **控制边**用特殊端口值 `kControlSlot = -1` 表示，只约束执行顺序、不传数据。
- **运行时 `Graph`** 是带指针、可增删的对象图；**序列化 `GraphDef`** 是扁平的 `NodeDef` 列表，边被「折叠」进每个 `NodeDef.input` 的字符串（`"name:idx"` 或 `"^name"`）。
- `Graph::ToGraphDef` 负责前者到后者：跳过 source/sink、按端口排数据边、控制边置末并排序以保证可复现。
- Python 侧 `ops.Graph` 直接桥接 C++ 图；`FuncGraph(ops.Graph)` 在其上加 `inputs/outputs/captures/outer_graph`，是 `tf.function` 追踪的产物，靠 `_create_op_internal` 里的 `capture` 把外部闭包变量透明地搬进函数图。

## 7. 下一步学习建议

- **u3-l2 会话执行链路 Session 与 DirectSession**：本讲只讲到「图怎么存」，下一讲讲「图怎么跑」——`DirectSession::Run` 如何把这张 `Graph` 切分、放置、调度、执行。届时你会看到 source/sink 哨兵在调度中的真正用途。
- **u3-l3 Eager 执行模式**：对比「不建图、立即执行」的路径，理解为何 TF2 默认 eager 却仍需要 `FuncGraph`。
- **u3-l4 tf.function 与 ConcreteFunction**：本讲把 `FuncGraph` 当作既成事实，下一讲深挖 `tf.function` 如何决定何时追踪、如何缓存 `ConcreteFunction`。
- 想深入序列化方向，可先读 `tensorflow/core/framework/graph.proto`、`node_def.proto` 全文，再看 `tensorflow/core/graph/graph_def_util.h` 里的 `ConvertGraphDefToGraph`（`GraphDef → Graph` 的反向过程）。
