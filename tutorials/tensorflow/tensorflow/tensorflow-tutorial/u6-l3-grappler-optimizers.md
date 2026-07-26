# Grappler 图优化器

## 1. 本讲目标

在前面的学习中，我们已经知道：

- u3-l1 讲清了运行时如何用 `Graph`/`Node`/`Edge` 表示一张计算图，以及它如何被序列化成扁平的 `GraphDef`（`NodeDef` 列表）。
- u3-l2 讲清了 `DirectSession::Run` 在首次执行时，会依次完成 **剪枝（Pruning）→ 放置（Placement）→ 优化（Optimization）→ 分区（Partition）** 几个阶段。

本讲就钻进其中的「优化」阶段，回答一个核心问题：

> **在图真正被设备执行之前，TensorFlow 是怎么把它「改写得更快」的？**

学完本讲，你应当掌握：

1. 理解 **Grappler** 是 TensorFlow 的图优化子系统，知道它在执行链路中的确切位置。
2. 理解 `GraphOptimizer` 抽象基类与 `MetaOptimizer` 调度器构成的「优化器流水线」架构。
3. 掌握**常量折叠（constant folding）**这一最典型的图优化：它的判定条件、核心算法，以及一个关键事实——**它通过真正在 CPU 上跑一遍 OpKernel 来求值**。
4. 区分仓库里两处同名但职责不同的常量折叠：`grappler` 的 `ConstantFolding`（基于 `GraphDef`）与 `common_runtime` 的 `ConstantFold`（基于 `Graph`）。

---

## 2. 前置知识

本讲需要你已经具备以下认知（来自前置讲义），这里用通俗的话再点一遍：

- **GraphDef 与 NodeDef**（u3-l1）：图在磁盘/传输时是一串 `NodeDef`，每个节点用 `input` 字符串列表表达「我从谁拿数据」。比如 `"add:0"` 表示拿 `add` 节点的第 0 个输出，`"^ctrl"` 表示控制依赖。Grappler 工作在 `GraphDef` 这一层。
- **OpKernel 与 Compute**（u4-l2）：每个 op 在特定设备上有真正的计算实现 `OpKernel`，核心是 `Compute(OpKernelContext*)` 方法，输入输出都经 `OpKernelContext` 这条总线传递。
- **DirectSession 执行链路**（u3-l2）：`DirectSession::Run` 首次执行时，会用 `GraphExecutionState` 把图「打磨」好。Grappler 就是在这个打磨过程中被调用的。
- **为什么需要优化**：用户写出来的图往往不是最优的。比如 `tf.constant(2) + tf.constant(3)` 在每次推理时都会去真正算一次加法，但它其实可以在编译期就算成 `tf.constant(5)`。这类「可以在运行前就算掉」的工作，就是图优化要做的。

一个贯穿全讲的直觉：

> **图优化 = 把用户写的图，在执行前等价改写成一张「算得更少、算得更快」的图。**

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `tensorflow/core/grappler/optimizers/graph_optimizer.h` | 所有图优化器的抽象基类 `GraphOptimizer`，定义统一的 `Optimize()` 接口与超时（deadline）机制。 |
| `tensorflow/core/grappler/optimizers/meta_optimizer.h` / `.cc` | `MetaOptimizer`：根据 `RewriterConfig` 把一堆优化器组装成流水线并依次运行；是 Grappler 的「总调度」。 |
| `tensorflow/core/grappler/grappler_item.h` | `GrapplerItem`：一次优化的输入——待优化的图 + feed + fetch + 必须保留的节点。 |
| `tensorflow/core/grappler/utils.h` | `NodeMap`：在扁平 `GraphDef` 上建立的「名字→节点」「节点→消费者」索引，是图导航的基础工具。 |
| `tensorflow/core/grappler/optimizers/constant_folding.h` / `.cc` | 本讲主角 `ConstantFolding`：基于 `GraphDef` 的常量折叠优化器，约 4000 行，是单个优化器中最庞大的之一。 |
| `tensorflow/core/grappler/optimizers/evaluation_utils.cc` | 自由函数 `EvaluateNode()`：常量折叠时用来「真正求值」的工具——在 CPU 上建一个 `OpKernel` 并跑一遍。 |
| `tensorflow/core/common_runtime/constant_folding.h` | 另一套、更底层的 `ConstantFold()`：基于运行时 `Graph*` 的常量折叠，与 grappler 版分工不同。 |
| `tensorflow/core/common_runtime/graph_execution_state.cc` | 执行链路接入点：`DirectSession` 在优化阶段调用 `RunMetaOptimizer` 的地方。 |

---

## 4. 核心概念与源码讲解

### 4.1 优化器架构：GraphOptimizer 抽象与 MetaOptimizer 调度

#### 4.1.1 概念说明

Grappler 里优化器有几十个：常量折叠、算术化简、布局优化、算子融合（remap）、循环优化、依赖化简、自动并行、自动混合精度……如果让上层调用方（Session、tf.function）直接去逐个认识它们，会非常混乱。

所以 Grappler 用了一个经典的**策略模式 + 流水线**设计：

- 抽象出一个**统一接口** `GraphOptimizer`，规定「给一个图，吐出一个优化后的图」。
- 每个具体优化器（`ConstantFolding`、`ArithmeticOptimizer`……）都实现这个接口。
- 一个**总调度器 `MetaOptimizer`** 根据用户配置（`RewriterConfig`）决定启用哪些优化器、按什么顺序运行。

这样新增一个优化器只需写一个子类并注册到 `MetaOptimizer`，上层完全不用改。

#### 4.1.2 核心流程

```
DirectSession::Run (首次)
   └─ GraphExecutionState::OptimizeGraph
        └─ grappler::RunMetaOptimizer(item, config, ...)
             └─ MetaOptimizer::OptimizeGraph
                  ├─ InitializeOptimizers()   // 按 RewriterConfig 选出 enabled 的优化器列表
                  └─ 依次对每个 optimizer 调用 optimizer->Optimize(cluster, item, out_graph)
                       （上一个的输出 out_graph 作为下一个的输入，串成流水线）
```

关键点：**优化器是串成流水线依次跑的**——常量折叠先跑、把可算的常量算掉；随后算术优化器在更「干净」的图上再做化简；它们彼此配合。MetaOptimizer 还会**循环跑若干趟**，因为一次优化可能制造出新的可优化机会（例如折叠后又出现了新的全常量输入）。

#### 4.1.3 源码精读

**统一接口 `GraphOptimizer`**——三个抽象/虚方法 + 一个超时机制：

[graph_optimizer.h:36-60](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/grappler/optimizers/graph_optimizer.h#L36-L60) 定义抽象基类。核心是纯虚的 `Optimize()`：

```cpp
// Routine called to allow an algorithm to propose a rewritten graph
// for the graph, feeds and fetches in "item" to run more efficiently
// on "cluster".
virtual absl::Status Optimize(Cluster* cluster, const GrapplerItem& item,
                              GraphDef* optimized_graph) = 0;
```

签名里的三个参数就是优化器工作的全部上下文：`item`（输入图 + feed/fetch）、`cluster`（目标硬件的抽象描述，可用于代价估算）、`optimized_graph`（输出）。`name()` 返回优化器名字（如 `"constant_folding"`），`UsesFunctionLibrary()` 声明该优化器是否需要真实函数库。

`graph_optimizer.h:70-88` 还定义了一个**超时（deadline）机制**：优化器可以设一个截止时间，`DeadlineExceeded()` 判断是否超时，宏 `GRAPPLER_RETURN_IF_DEADLINE_EXCEEDED()` 用于在耗时优化器的循环里提前安全退出。这一点很重要——优化不能无限期拖延图的执行。

**总调度器 `MetaOptimizer`** 的注册逻辑。`meta_optimizer.cc:383-393` 是常量折叠被启用的判定与构造：

```cpp
if (BOTH_NOT_OFF(constant_folding)) {        // 配置不是 OFF 就启用
  if (USER_IS_EXPERIMENTAL_MLIR(constant_folding) || ...) {
    VLOG(2) << "constant_folding is not implemented in TFG yet";
  } else {
    optimizers->push_back(std::make_unique<ConstantFolding>(
        cfg_.constant_folding(), cpu_device_,
        cfg_.experimental_disable_compressed_tensor_optimization(),
        !cfg_.experimental_disable_folding_quantization_emulation()));
  }
}
```

`BOTH_NOT_OFF` 这类宏表示「用户的 `RewriterConfig` 和插件的配置都不是 `OFF`」时才启用。同一个 `InitializeOptimizers` 里，类似的 `MK_OPT` / `push_back` 还有几十处，把 `ArithmeticOptimizer`、`GenericLayoutOptimizer`、`Remapper`、`LoopOptimizer`、`DependencyOptimizer`、`MemoryOptimizer` 等依次加入列表（见 `meta_optimizer.cc:242-281` 的 `MakeNewOptimizer` 名字映射表）。

**接入执行链路**。`graph_execution_state.cc:802-825` 是 `DirectSession` 把图交给 Grappler 的现场：

```cpp
// Convert Graph to GraphDef and add it to the GrapplerItem.
graph.ToGraphDef(&item.graph);
...
// Construct a virtual cluster and find the cpu_device, which the
// ConstantFolding optimizer will use for partial evaluation of the graph.
grappler::VirtualCluster cluster(device_set_);
...
// Now we can run the MetaOptimizer on the constructed GrapplerItem.
GraphDef new_graph;
TF_RETURN_IF_ERROR(
    grappler::RunMetaOptimizer(std::move(item), session_options_->config,
                               cpu_device, &cluster, &new_graph));
```

注意三件事，它们正好对应 u3-l2 的「优化阶段」：

1. 运行时先把内存里的 `Graph*` 转回 `GraphDef`（u3-l1 讲过两者的区别），因为 Grappler 工作在 `GraphDef` 这一层。
2. 构造一个 `VirtualCluster`（虚拟集群，用于代价估算）和找到 `cpu_device`——注释明确说「常量折叠优化器会用它做部分求值」。这正是下一节要讲的核心。
3. 调 `RunMetaOptimizer`，返回优化后的 `new_graph`，之后再做分区。

> 小结：Grappler 在「放置之后、分区之前」运行，输入输出都是 `GraphDef`。这是它在执行链路中的确切坐标。

#### 4.1.4 代码实践

**实践目标**：从源码层面确认「哪些优化器默认开启、Grappler 在何处被调用」。

**操作步骤**：

1. 打开 [meta_optimizer.cc 的 InitializeOptimizers 区域](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/grappler/optimizers/meta_optimizer.cc#L383-L520)，数一下共有多少处 `BOTH_NOT_OFF(...)` 或 `MK_OPT(...)`，把对应的优化器名字列成一个表。
2. 阅读 `meta_optimizer.cc:1363-1389` 的 `MetaOptimizerEnabled()`：只要 `RewriterConfig` 里任意一个开关不是 `OFF`（或 `disable_model_pruning` 为假），`MetaOptimizer` 就会被启用。据此判断：在**默认配置**下，常量折叠是否默认开启？

**需要观察的现象**：

- 默认情况下，`RewriterConfig` 各项多为 `DEFAULT`（等价于 `ON`），所以 `MetaOptimizerEnabled` 返回 `true`，`BOTH_NOT_OFF(constant_folding)` 成立——常量折叠默认就是开着的。

**预期结果**：你能列出至少 8 个优化器（constant_folding、arithmetic_optimization、layout_optimizer、remapping、loop_optimization、dependency_optimization、shape_optimization、common_subgraph_elimination 等），并确认它们默认参与流水线。具体运行结果「待本地验证」（可写一段 C++ 单测调用 `RunMetaOptimizer` 打印 `MetaOptimizer::GetResultString()` 看每个优化器是否生效与各自耗时）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Grappler 选择工作在 `GraphDef`（扁平的 `NodeDef` 列表）而不是运行时的 `Graph`/`Node`/`Edge` 对象上？

> **参考答案**：`GraphDef` 是 protobuf，序列化友好、易于跨进程传输与做快照；优化器只需读写 `NodeDef` 的字符串 `input` 字段，增删节点就是增删数组元素，无需维护复杂的边对象与引用关系。这也让 Grappler 可以独立于运行时被复用（例如离线对 SavedModel 做优化）。代价是失去了运行时图的部分便利，所以 Grappler 自己用 `NodeMap`（见 4.2）重建索引。

**练习 2**：`GraphOptimizer` 基类里的 `UsesFunctionLibrary()` 返回 `false` 意味着什么？

> **参考答案**：见 [graph_optimizer.h:43-48](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/grappler/optimizers/graph_optimizer.h#L43-L48) 的注释：表示该优化器不需要有效的函数库就能工作，于是框架传给它的 `GrapplerItem` 里会用「桩（stub）」函数体替代真实函数体以省内存。`ConstantFolding` 恰好返回 `false`（见 constant_folding.h:60），因为它只评估图的常量部分，不实例化函数。

---

### 4.2 输入与导航：GrapplerItem 与 NodeMap

#### 4.2.1 概念说明

优化器要干活，得先有「原材料」和「工具」：

- **原材料 `GrapplerItem`**：封装「一张待优化的图 + 它的 feed（喂入点）/fetch（取回点）+ 必须保留的节点」。fetch/feed 决定了优化的**安全边界**——任何被 fetch 的节点都不能被随意删掉或改名，否则用户取不到结果。
- **工具 `NodeMap`**：`GraphDef` 只是节点的扁平数组，要回答「节点 X 被谁消费了？」「`"add:0"` 指向哪个 `NodeDef`？」这类问题，必须先建索引。`NodeMap` 就是这个索引。

#### 4.2.2 核心流程

`NodeMap` 在构造时**遍历一次 `GraphDef`**，建立两张表：

```
nodes_  : node_name          -> NodeDef*
outputs_: node_name          -> { 消费它的所有 NodeDef* }
```

构造算法（伪代码）：

```
对每个节点 n in graph.node:
    nodes_[n.name] = &n
    对每个输入字符串 input in n.input:
        src_name = 去掉 ":port" 和 "^" 的节点名
        outputs_[src_name].insert(&n)
```

于是：
- `GetNode("add")` → O(1) 拿到节点指针。
- `GetOutputs("add")` → O(1) 拿到所有消费 `add` 的节点集合（即 fanout）。

注意 `input` 字符串可能是 `"add:0"`（数据边，第 0 个输出）或 `"^ctrl"`（控制依赖）。`NodeMap` 内部用 `NodeName(input)` 剥掉端口和 `^` 前缀再索引，因此两种边都能正确建立「源→消费者」关系——这正对应 u3-l1 讲过的 GraphDef 边编码方式。

#### 4.2.3 源码精读

**`GrapplerItem` 的核心字段**——[grappler_item.h:38-84](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/grappler/grappler_item.h#L38-L84)：

```cpp
struct GrapplerItem {
  std::string id;
  GraphDef graph;                              // 待优化的图
  std::vector<std::pair<std::string, Tensor>> feed;  // 喂入点
  std::vector<std::string> fetch;             // 取回点
  ...
  std::vector<std::string> keep_ops;          // 必须保留的节点
  // 返回必须保留的节点名集合（feed + fetch + keep_ops + init_ops）
  std::unordered_set<std::string> NodesToPreserve() const;
};
```

`NodesToPreserve()` 是优化安全性的基石：常量折叠在判断一个节点能否折叠时，会先查它是否在「必须保留」集合里（见 4.3 的 `MaybeFoldable`）。

**`NodeMap` 的构造与查询**——[utils.h:108-182](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/grappler/utils.h#L108-L182)。构造函数遍历建表：

```cpp
for (int i = 0; i < graph->node_size(); i++) {
  NodeDefT* node = GetNodeDefFromGraph(graph, i);
  nodes_.emplace(node->name(), node);          // 名字 -> 节点
  for (const auto& input : node->input()) {
    outputs_[NodeName(input)].insert(canonical); // 源 -> 消费者
  }
}
```

查询接口：

```cpp
NodeDefT* GetNode(const std::string& name) const;          // utils.h:174
const absl::flat_hash_set<NodeDefT*>& GetOutputs(...) ...; // utils.h:133
std::vector<NodeDefT*> GetOutputsOrderedByNodeName(...) const; // utils.h:143（确定性排序版）
```

`NodeMap` 本身（[utils.h:252-255](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/grappler/utils.h#L252-L255)）只是模板基类 `NodeMapInternal` 的一层薄包装，目的是让同一套实现同时支持可变 `GraphDef` 与只读 `const GraphDef`（后者叫 `ImmutableNodeMap`）。

#### 4.2.4 代码实践

**实践目标**：用一个小图体会 `NodeMap` 如何回答「谁消费了 X」。

**操作步骤**：

1. 假设有图 `c1=Const(2)`，`c2=Const(3)`，`add=Add(c1, c2)`，`out=Identity(add)`。
2. 在纸上按上面伪代码模拟 `NodeMap` 构造，写出 `outputs_` 表：
   - `outputs_["c1"] = {add}`
   - `outputs_["c2"] = {add}`
   - `outputs_["add"] = {out}`
3. 思考：若常量折叠把 `add` 折叠成一个新的 `Const` 节点 `add_folded`，需要修改 `out` 的 `input` 吗？如何找到要修改的消费者？

**需要观察的现象**：

- 通过 `GetOutputs("add")` 立刻知道 `out` 是 `add` 的唯一消费者，所以折叠后必须把 `out` 的 `input` 从 `add` 改指向新常量，否则 `out` 会断链。

**预期结果**：你会直观体会到「优化器做任何改写，都必须同步维护 fanout 指向」——这正是 `NodeMap` 存在的意义。运行层面「待本地验证」（可阅读 `constant_folding_test.cc` 中构造小图并断言折叠结果的测试）。

#### 4.2.5 小练习与答案

**练习 1**：`NodeMap` 构造时若图里有两个同名节点会怎样？

> **参考答案**：见 [utils.h:119-123](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/grappler/utils.h#L119-L123)：先发现的节点成为「canonical（规范节点）」，后发现的会被忽略并打一条 `Duplicated node in the graph` 警告日志，不报错。

**练习 2**：为什么需要 `GetOutputsOrderedByNodeName` 这种「排序版」？

> **参考答案**：`GetOutputs` 返回的是哈希集合，遍历顺序不确定；而 Grappler 要求优化结果**可复现**（同一张图每次优化得到完全相同的输出图），所以在需要遍历 fanout 改写时（如 `FoldGraph`），用排序版保证顺序确定。

---

### 4.3 常量折叠算法精读

#### 4.3.1 概念说明

**常量折叠（constant folding）** 是最经典也最「值钱」的图优化。直觉定义：

> 如果一个节点的所有输入在**编译期**就能确定（都是常量），那么它的输出在编译期也能确定。与其每次推理都重新算一遍，不如**在优化阶段就把结果算出来，把该节点替换成一个常量节点**。

举例：

```
优化前：  a=Const(2)  b=Const(3)  c=Add(a,b)   → 每次推理都算 2+3
优化后：  c=Const(5)                              → 推理时直接取常量
```

更进一步，常量折叠还会做**常量传播**：一旦 `c` 变成常量，依赖 `c` 的下游节点如果其余输入也都是常量，就又可以折叠，像多米诺骨牌一样层层算下去。

这里有个让很多人意外的关键事实，也是本讲最重要的一句话：

> **Grappler 的常量折叠不是「手写一套算术解释器」，而是「在 CPU 上真正实例化一个 OpKernel 并跑一遍 `Compute`」来求值。** 也就是说，它复用了 u4-l2 讲的 OpKernel 机制。

这意味着任何注册了 CPU kernel、无副作用、输入全常量的 op，都能自动被折叠——无需为每个 op 单独写折叠逻辑。

#### 4.3.2 核心流程

`ConstantFolding::Optimize` 的整体策略是**不动点迭代（fixpoint iteration）**：

```
do:
    graph_modified_ = false
    RunOptimizationPass(item, optimized_graph)   // 内部若改动了图，置 graph_modified_=true
while (graph_modified_ 或 节点数仍变化)
```

因为一轮折叠会产生新的常量、新的可折叠节点，所以反复跑到「不再变化」为止，同时每轮都检查 deadline 防止跑飞。

`RunOptimizationPass` 单轮做三件事：

1. **`MaterializeShapes` / `MaterializeConstants`**：借助 `GraphProperties`（形状推导结果），把一些形状信息「物化」成常量（例如把 `Shape(x)` 折叠成具体的 `[2,3]`，把 `BroadcastGradientArgs`、`ReductionIndices` 等算成常量）。
2. **`FoldGraph`**：核心的常量折叠主体（BFS 遍历可折叠节点，逐一求值替换）。
3. **`SimplifyGraph`**：算术/结构化简（把 `x*1`→`x`、`Reshape` 改 `Identity` 等），与折叠相互配合。

`FoldGraph` 的核心算法是一个**BFS 工作队列**：

```
初始化：把所有 IsFoldable 的节点入队
while 队列非空:
    node = 出队
    fanout = NodeMap.GetOutputs(node)           // 先记录消费者（待会 node 会被改写）
    s = FoldNode(node, output_graph)            // 求值并替换成常量节点
    if 成功:
        for f in fanout:
            if IsFoldable(f): 把 f 入队          // 新常量可能让下游也变可折叠
    删除无人消费的新生成节点
```

`FoldNode` 对单个节点的处理简化为：

```
1. 收集 node 的所有常量输入，组装成 TensorValue 数组 inputs
2. 调 EvaluateNode(node, inputs, &output_tensors)   // 关键：在 CPU 上跑 kernel
3. 对每个输出张量，用 CreateNodeDef() 序列化成 Const 节点，加入 optimized_graph
4. 把消费者的 input 改指向新 Const 节点
```

**可折叠判定**（`IsFoldable`/`MaybeFoldable`）是安全性的核心，它必须保守——宁可少折叠也不能折错。判定要点见 4.3.3。

#### 4.3.3 源码精读

**入口 `Optimize`**——[constant_folding.cc:4073-4130](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/grappler/optimizers/constant_folding.cc#L4073-L4130)。注意开头两行很有意思：

```cpp
port::ScopedFlushDenormal flush;
port::ScopedSetRound round(FE_TONEAREST);
```

注释解释：TensorFlow 运行时会「把非规格化数冲刷为零、就近舍入」，所以常量折叠求值时必须设置**完全相同的浮点环境**，否则编译期算出的常量与运行期算出的会对不上——这是保证「折叠前后数值等价」的细节。随后用 `GraphProperties.InferStatically(...)` 推导全图形状与部分输出张量值（`include_output_tensor_values=true`），作为折叠的依据。

**不动点循环**——[constant_folding.cc:4118-4125](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/grappler/optimizers/constant_folding.cc#L4118-L4125)：

```cpp
do {
  GRAPPLER_RETURN_IF_DEADLINE_EXCEEDED();
  graph_modified_ = false;
  item_to_optimize.graph.Swap(optimized_graph);
  node_count = item_to_optimize.graph.node_size();
  TF_RETURN_IF_ERROR(RunOptimizationPass(cluster, &item_to_optimize,
                                        &properties, optimized_graph));
} while (graph_modified_ || optimized_graph->node_size() != node_count);
```

每轮先 `GRAPPLER_RETURN_IF_DEADLINE_EXCEEDED()` 检查超时，循环条件是「本轮改过图」或「节点数变了」。

**单轮 `RunOptimizationPass`**——[constant_folding.cc:4034-4070](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/grappler/optimizers/constant_folding.cc#L4034-L4070)：

```cpp
node_map_.reset(new NodeMap(graph_));                  // 建 NodeMap
...
for (const auto& fetch : item->fetch) {                // 单输出的 fetch 节点可折叠
  if (NumOutputs(*fetch_node, graph_) == 1) {
    nodes_allowlist_.insert(fetch_node->name());        // 放进允许折叠的白名单
  }
}
...
TF_RETURN_IF_ERROR(MaterializeShapes(*properties));
TF_RETURN_IF_ERROR(MaterializeConstants(*properties));
TF_RETURN_IF_ERROR(
    FoldGraph(*properties, optimized_graph, &nodes_to_not_simplify));
...
TF_RETURN_IF_ERROR(SimplifyGraph(optimized_graph, properties, &nodes_to_not_simplify));
```

这里有一个精巧的设计：fetch 节点本该「必须保留」不可折叠，但若它只有**单一输出**，折叠后会用同名常量替换，用户仍能用原名取回，于是放进 `nodes_allowlist_` 允许折叠；多输出节点则不行（折叠会改名导致用户取不到）。

**可折叠判定**——`MaybeFoldable`（[constant_folding.cc:1060-1125](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/grappler/optimizers/constant_folding.cc#L1060-L1125)）做「粗筛」：

```cpp
if (IsConstant(node)) return false;          // 常量本身已折叠，不用再折
if (!IsFreeOfSideEffect(node)) return false; // 有副作用（如随机数 TruncatedNormal）绝不能折
if (nodes_to_preserve_.count(node.name()) && !nodes_allowlist_.count(node.name()))
  return false;                              // 必须保留且不在白名单的，不折
if (ModifiesFrameInfo(node)) return false;   // 控制流节点（影响 frame）不折
if (IsPlaceholder(node)) return false;       // Placeholder 不折
...
```

`IsFreeOfSideEffect` 这个判定至关重要：像 `TruncatedNormal`、`RandomUniform` 这种每次调用都应产生不同随机值的 op，一旦被折叠成常量就「随机性死了」，所以必须排除——这正是「等价改写」的红线。

`IsFoldableUncached`（[constant_folding.cc:974-1058](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/grappler/optimizers/constant_folding.cc#L974-L1058)）做「细筛」：要求节点所有输入都是常量（`IsReallyConstant`），且输出不能过大——用形状信息估算输出字节数，超过 `kMaxConstantSize` 就不折，避免折叠出一个巨型常量拖慢甚至撑爆内存：

```cpp
if (num_bytes > input_size_bytes && num_bytes > kMaxConstantSize) {
  return false;   // 输出太大，不折叠
}
```

`kMaxConstantSize` 定义在 [constant_folding.h:36](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/grappler/optimizers/constant_folding.h#L36)（一个 `extern const int64_t`）。

**BFS 主体 `FoldGraph`**——[constant_folding.cc:1655-1700](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/grappler/optimizers/constant_folding.cc#L1655-L1700)：

```cpp
std::deque<NodeDef*> queue;
for (...) if (IsFoldable(node, &properties)) queue.push_back(...);  // 初始入队
while (!queue.empty()) {
  NodeDef* node = queue.front(); queue.pop_front();
  if (processed_nodes.count(node->name())) continue;
  std::vector<NodeDef*> fanout = node_map_->GetOutputsOrderedByNodeName(node->name());
  absl::Status s = FoldNode(node, optimized_graph, &result_too_large);
  processed_nodes.insert(node->name());
  if (s.ok()) {
    for (auto& fanout_node : fanout)
      if (IsFoldable(*fanout_node, &properties)) queue.push_back(fanout_node);
  }
}
```

注意「先记录 fanout 再 FoldNode」的注释——因为 `FoldNode` 会改写节点，必须提前快照消费者列表，顺序用 `GetOutputsOrderedByNodeName` 保证确定性。

**真正求值的 `EvaluateNode`**——这是常量折叠复用 OpKernel 的核心。先看 `ConstantFolding::EvaluateNode`（[constant_folding.cc:1347-1352](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/grappler/optimizers/constant_folding.cc#L1347-L1352）转调自由函数：

```cpp
return ::tensorflow::grappler::EvaluateNode(node, inputs, cpu_device_,
                                            resource_mgr_.get(), output);
```

自由函数 [evaluation_utils.cc:65-101](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/grappler/optimizers/evaluation_utils.cc#L65-L101) 才是真相揭晓处：

```cpp
std::unique_ptr<OpKernel> op_kernel(
    CreateOpKernel(DEVICE_CPU, cpu_device, cpu_device->GetAllocator({}), node,
                   TF_GRAPH_DEF_VERSION, &status));   // ① 建一个 CPU OpKernel
...
OpKernelContext::Params params;
params.device = cpu_device;
params.inputs = inputs;
params.op_kernel = op_kernel.get();                    // ② 组装上下文
...
OpKernelContext op_context(&params);
op_kernel->Compute(&op_context);                       // ③ 真正跑 Compute！
for (int i = 0; i < num_outputs; i++) {
  output->push_back(op_context.release_output(i));     // ④ 取出结果张量
}
```

四步与 u4-l2 讲的 OpKernel 执行模型完全一致：`CreateOpKernel` 按 `(op, DEVICE_CPU)` 查 kernel 注册表（回顾 u4-l2 的 `KernelRegistry` multimap），构造出 kernel 对象；填好 `OpKernelContext::Params`（注意输出分配器都设了 `set_on_host(true)`，因为折叠结果要留在主机内存里序列化）；调 `Compute`；释放输出。**这正是「图优化器在编译期复用了运行期的执行机制」的精妙之处。**

折叠得到的张量再由 [constant_folding.h:43-45](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/grappler/optimizers/constant_folding.h#L43-L45) 的静态方法 `CreateNodeDef(name, tensor, node)` 序列化成一个 `Const` 节点（值为 `TensorProto`，回顾 u2-l2 讲过的 `Const` 节点结构）。新生成的常量节点会带上专门前缀（[constant_folding.h:34-35](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/grappler/optimizers/constant_folding.h#L34-L35) 的 `kConstantFoldingConst` / `kConstantFoldingCtrl`），方便调试时辨认「这是折叠产物」。

#### 4.3.4 代码实践

**实践目标**：亲手构造一个含可折叠常量的小图，对照源码推断 Grappler 会把它变成什么样，并解释为何更快。

**操作步骤（源码阅读型 + 可选运行型）**：

1. **构造小图（示例代码，非项目原有代码）**：

   ```python
   import tensorflow as tf
   @tf.function
   def f(x):
       a = tf.constant(2.0)
       b = tf.constant(3.0)
       c = a + b          # 全常量输入，可折叠成 Const(5.0)
       return x * c       # 与输入 x 相乘，x 不是常量
   ```

2. **预测折叠结果**：根据 4.3 的算法，`a`、`b` 是常量，`c=Add(a,b)` 所有输入都是常量且 `Add` 无副作用 → `c` 会被折叠成 `Const(5.0)`。于是 `return x * c` 变成 `x * Const(5.0)`，`a`、`b`、原 `Add` 节点被删除。

3. **（可选）观察优化后的图**：用如下方式导出并查看图节点（「待本地验证」）：

   ```python
   cf = f.get_concrete_function(tf.TensorSpec([], tf.float32))
   for n in cf.graph.as_graph_def().node:
       print(n.op, n.name)
   ```

   预期看到只有一个乘法节点和一个名为 `.../ConstantFolding...` 的 `Const` 节点，看不到原始的 `Add`。

**需要观察的现象**：

- 优化后的图里，`a + b` 这条计算链消失了，取而代之的是一个值为 5.0 的 `Const` 节点。

**为何更快（这是本实践要回答的核心问题）**：

- 每次 `f(x)` 被调用，原本要先执行一次 `Add` kernel（分配输出张量、启动 kernel、做逐元素加法），现在变成零计算——`Const` 的值直接内嵌在节点里（`TensorProto`），运行时几乎只是读取。
- 还省下了 `a`、`b` 两个节点的执行开销与中间张量内存。
- 在大规模图里，这种可折叠的子图往往很多（尤其是形状计算、配置常量、梯度里的 `BroadcastGradientArgs` 等），累积收益非常可观。

**预期结果**：你能画出优化前后的图对比，并指出折叠消灭了哪些运行期 op。具体导出节点列表「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：假如把 `tf.constant(2.0)` 换成 `tf.random.uniform([], ...) _ * 0 + x`，常量折叠能把它折叠吗？为什么？

> **参考答案**：不能。随机数 op 有副作用（每次应产生不同值），`IsFreeOfSideEffect` 返回 `false`，`MaybeFoldable` 直接排除它。即便后面乘 0，优化器也不会把 `random * 0` 折成 0，因为那样会消除随机副作用，破坏语义。

**练习 2**：`IsFoldableUncached` 里为什么对输出大小设上限（`kMaxConstantSize`）？折叠出一个大常量有什么坏处？

> **参考答案**：折叠会把结果张量内嵌进 `GraphDef`（`TensorProto`）并在内存中常驻。若一个 op 的输出极大（例如 `Fill([10000,10000], 0)` 产生上亿元素），折叠会让 `GraphDef` 膨胀、加载变慢、内存占用居高不下，得不偿失。所以超过阈值就放弃折叠、保留原 op 在运行期即时生成。

**练习 3**：为什么 `FoldGraph` 要用「先记录 fanout、再 FoldNode、然后把新可折叠的 fanout 入队」的 BFS，而不是一次性扫描全图？

> **参考答案**：因为折叠会**级联**——折叠 `c` 后，依赖 `c` 的下游节点 `d` 可能从「输入非全常量」变成「输入全常量」，从而新变为可折叠。BFS 自然地捕捉这种级联：每折一个就把它新变得可折叠的消费者入队，直到收敛。这正是外层还要套不动点 `do/while` 的原因之一。

---

### 4.4 两套 ConstantFolding 的分工

#### 4.4.1 概念说明

细心的读者会发现：仓库里有**两个**都叫「常量折叠」的东西——

1. `tensorflow/core/grappler/optimizers/constant_folding.h` 里的 `class ConstantFolding`（本讲主角，基于 `GraphDef`）。
2. `tensorflow/core/common_runtime/constant_folding.h` 里的自由函数 `ConstantFold()`（基于运行时 `Graph*`）。

它们解决同一类问题，但工作层次不同、调用时机不同。混淆它们是初学常犯的错误，所以专门辨析。

#### 4.4.2 核心流程

| 维度 | grappler `ConstantFolding`（类） | common_runtime `ConstantFold`（函数） |
| --- | --- | --- |
| 工作对象 | `GraphDef`（protobuf，扁平 NodeDef 列表） | `Graph*`（运行时内存图，含 Node/Edge 对象） |
| 调用者 | `MetaOptimizer` 流水线，执行前优化 | 运行时更早期的图构造阶段（如 `graph_optimizer.cc`） |
| 输入封装 | `GrapplerItem`（graph + feed + fetch） | `ConstantFoldingOptions`（consider 谓词、shape_map、max_size） |
| 特点 | 功能丰富（物化形状、算术化简、不动点迭代） | 更精简、更低层，操作真实 `Graph` 对象 |

简单说：**grappler 版是主力、功能完备的「产品级」常量折叠；common_runtime 版是更早期的、与运行时图对象绑定的轻量入口**。在 TF2 默认（走 Grappler）的执行链路里，你接触到的主要是 grappler 版。

#### 4.4.3 源码精读

看 common_runtime 版的签名与说明——[common_runtime/constant_folding.h:54-66](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/common_runtime/constant_folding.h#L54-L66)：

```cpp
// Perform constant folding optimization on "graph".
// Looks for nodes in "graph" that can be completely evaluated statically, i.e.,
// that are only dependent on constants. Evaluates those nodes on a CPU device
// and replaces those nodes with the result of the evaluation.
absl::Status ConstantFold(const ConstantFoldingOptions& opts,
                          FunctionLibraryRuntime* function_library, Env* env,
                          const Device* partition_device, Graph* graph,
                          bool* was_mutated);
```

对比 grappler 版，几个显著差异：

- 入参是 `Graph* graph`（运行时图，u3-l1 讲的 `Graph`/`Node`/`Edge`），而不是 `GraphDef*`。
- 用 `ConstantFoldingOptions`（[constant_folding.h:35-52](https://github.com/tensorflow/tensorflow/blob/92725c7290ff3c4f1ca8125eebd69a70e02ae03b/tensorflow/core/common_runtime/constant_folding.h#L35-L52)）描述「哪些节点参与折叠（`consider` 谓词）」「已知的部分形状（`shape_map`）」「最大常量尺寸」。
- 也带 `FunctionLibraryRuntime*`，因为操作运行时图对象时需要解析函数。
- 注释同样点明「在 CPU 设备上求值并替换」——**两套在「用 CPU kernel 求值」这一点上是同源的**，区别只在操作对象与所处阶段。

文件顶部还有一行 TODO：`// TODO(skyewm): can this be combined with EvaluateConstantTensor?`，侧面说明历史上存在多套相近实现，社区一直在收敛它们。`ConstantFoldNameGenerator` 则用于给折叠产生的新节点按规则生成名字。

> 阅读建议：日常读 TF2 执行链路时，把注意力放在 grappler 版（`ConstantFolding` 类）；只有在研究运行时图对象层的早期优化（或读老代码）时才需要 common_runtime 版的 `ConstantFold`。

#### 4.4.4 代码实践

**实践目标**：在源码里定位并区分这两套常量折叠的调用者，避免混淆。

**操作步骤**：

1. 用 `grep` 在 `tensorflow/core/common_runtime/` 下搜索 `ConstantFold(`（注意是函数调用，带左括号）的调用点，确认它在运行时图构造流程中的位置。
2. 回顾 4.1.3：grappler 版的调用点是 `graph_execution_state.cc:824` 的 `RunMetaOptimizer`。
3. 写一句话对比：两个调用点分别处在图的什么形态（`GraphDef` vs `Graph*`）。

**需要观察的现象**：

- 两套常量折叠分别绑定不同的图表示，互不直接调用。

**预期结果**：你能清楚说出「grappler 版吃 `GraphDef`、由 `MetaOptimizer` 调度；common_runtime 版吃 `Graph*`、由运行时更早期调用」，从而在读代码时不会被同名迷惑。具体调用点「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 TF 会同时保留两套常量折叠实现，而不是只用一套？

> **参考答案**：历史演进与分层所致。运行时早期就有一套基于 `Graph*` 的折叠（common_runtime 版），用于在 `Graph` 对象层面做局部优化；后来 Grappler 引入了更强大、基于 `GraphDef`、可组合进流水线的版本（grappler 版），并成为 TF2 的主力。短期内两套共存以兼容不同路径（如某些非 Grappler 的图构造流程仍走旧版），社区通过 TODO 等方式逐步收敛。

**练习 2**：两套实现共享了哪条核心思想？

> **参考答案**：**「在 CPU 上真正跑 OpKernel 来对全常量输入的节点求值，再用结果常量替换原节点」**。无论操作 `GraphDef` 还是 `Graph*`，求值手段的本质一致，都复用了 u4-l2 的 `OpKernel`/`Compute`/`OpKernelContext` 机制。

---

## 5. 综合实践

把本讲的知识串起来，完成下面这个端到端的小任务：

**任务**：追踪一次 `tf.function` 从「用户写的图」到「Grappler 优化后的图」的全过程，定位常量折叠在其中发挥了什么作用。

**步骤**：

1. 写一个 `@tf.function`，内部故意包含一段「全常量」的计算（例如几个 `tf.constant` 做加减乘，再算个 `tf.shape` 之类的形状相关 op）和一个依赖输入 `x` 的真实计算。
2. 用 `get_concrete_function` 取到 `ConcreteFunction`，导出 `as_graph_def()` 的节点列表。
3. 对照 4.3.3 的算法，逐节点判断：
   - 哪些节点被折叠了（在优化后图中消失或变成 `ConstantFolding` 前缀的 `Const`）？
   - 为什么被折叠（满足 `MaybeFoldable` + `IsFoldableUncached` 的哪些条件）？
   - 为什么剩下的没被折叠（是输入非全常量？有副作用？太大？）？
4. 用 `tf.config.optimizer.set_experimental_options({'constant_folding': False})`（或通过 `RewriterConfig`）关闭常量折叠，重新导出图对比，验证你的判断。
5. 写一段话回答：常量折叠为这个图省下了哪些运行期 op？为什么这能提速？

**验收标准**：

- 你能画出优化前后的图对比图。
- 你能对每个节点用源码里的判定条件（`IsFreeOfSideEffect`、`IsReallyConstant`、`kMaxConstantSize` 等）解释它是否/为何被折叠。
- 你能指出 Grappler 在执行链路中的位置（`DirectSession::Run` → `GraphExecutionState::OptimizeGraph` → `RunMetaOptimizer`）。

> 运行结果「待本地验证」；若本地无法编译运行 TF，可改为纯源码阅读型：阅读 `tensorflow/core/grappler/optimizers/constant_folding_test.cc`，挑一个测试用例，对照本讲算法解释它构造的小图为何被折叠成断言中的结果。

---

## 6. 本讲小结

- **Grappler 是 TensorFlow 的图优化子系统**，在执行链路「放置之后、分区之前」运行（接入点 `graph_execution_state.cc` 的 `RunMetaOptimizer`），输入输出都是 `GraphDef`。
- 它采用**策略模式 + 流水线**架构：抽象基类 `GraphOptimizer` 定义统一 `Optimize()` 接口，`MetaOptimizer` 按 `RewriterConfig` 把数十个优化器（常量折叠、算术化简、布局优化、remap……）串成流水线依次运行，并支持不动点迭代与 deadline 超时保护。
- **常量折叠**是最典型的优化：把「所有输入都是常量、无副作用、输出不过大」的节点，**通过在 CPU 上真正实例化 OpKernel 并跑一遍 `Compute` 求值**（`evaluation_utils.cc`），替换成内嵌结果的 `Const` 节点，并以 BFS 级联传播，直到不动点。
- 折叠的**安全性**靠保守的判定函数保障：`MaybeFoldable` 排除有副作用（随机数）、控制流、placeholder、必须保留的节点；`IsFoldableUncached` 要求输入全常量且输出不超 `kMaxConstantSize`。
- `NodeMap` 是 Grappler 在扁平 `GraphDef` 上的导航工具，建立「名字→节点」「节点→消费者」两张索引，让增删改写能正确维护 fanout 指向；`GrapplerItem` 的 `NodesToPreserve()` 则划定优化的安全边界。
- 仓库有**两套常量折叠**：grappler 的 `ConstantFolding` 类（基于 `GraphDef`、功能完备、TF2 主力）与 common_runtime 的 `ConstantFold` 函数（基于 `Graph*`、更早期轻量），二者共享「用 CPU OpKernel 求值」的核心思想。

---

## 7. 下一步学习建议

- **横向认识其他优化器**：本讲只深挖了常量折叠。建议接着阅读 `tensorflow/core/grappler/optimizers/` 下的 `arithmetic_optimizer`（算术化简，如 `x*1→x`、强度削弱）、`remapper`（把多个 op 融合成一个，如 Conv+BiasAdd+Relu 融成单个 fused kernel）、`generic_layout_optimizer`（把 NHCW 改成对 GPU 更友好的 NHWC/NCHW），它们与常量折叠在同一个 `MetaOptimizer` 流水线里协作。
- **纵向深入 XLA/MLIR**（承接 u7 单元）：Grappler 是「图级」优化；XLA/MLIR 则是更激进的「编译级」优化，能把整个子图编译成高效设备代码。理解 Grappler 与 XLA auto-clustering（u7-l3）的分工与先后，能建立完整的「TF 图如何被加速」的全景。
- **实践自定义优化**：阅读 `tensorflow/core/grappler/optimizers/custom_graph_optimizer_registry.h`，了解如何把自己的 `GraphOptimizer` 子类注册进 `RewriterConfig.custom_optimizers`，亲手写一个最小图优化器（例如「把所有 `Identity` 节点短路掉」），这是检验你是否真懂 Grappler 架构的最好方式。
- **读测试**：`constant_folding_test.cc` 是一座金矿，里面有大量精心构造的微型图和断言，逐个对照本讲算法，能快速巩固对判定条件与折叠行为的理解。
