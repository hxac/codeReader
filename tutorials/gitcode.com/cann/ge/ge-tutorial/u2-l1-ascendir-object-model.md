# AscendIR 四层对象模型：Graph/Node/OpDesc/Tensor

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 AscendIR 作为 GE 核心中间表示（IR）的「四层对象模型」——`ComputeGraph`、`Node`、`OpDesc`、`GeTensorDesc` 各自是什么、彼此如何嵌套。
- 在真实头文件 `compute_graph.h` 中定位「添加节点 / 获取所有节点 / 拓扑排序」三类接口，并理解它们的返回类型与重载差异。
- 在 `node.h` 中定位 Node 的核心接口，并知道如何从一个节点走到它的算子描述与上下游邻居。
- 区分两个容易混淆的概念：**静态图**（图结构编译期固定）与**静态 Shape 图**（张量形状编译期已知），并能用 `GetGraphUnknownFlag()` 加以判断。

本讲是整个单元 2「AscendIR 图数据结构」的地基。后续讲义会在此基础上展开 Anchor 连边（u2-l2）、OpDesc 属性（u2-l3）、算子注册（u2-l4）。

## 2. 前置知识

在进入源码之前，先用通俗语言过一遍本讲会用到的几个基础概念。

### 2.1 什么是中间表示（IR）

把编译器想象成一名翻译：前端框架（PyTorch、TensorFlow、ONNX 文件）说的是「各种方言」，而翻译官需要一个**统一的中间语言**来思考。这个中间语言就是 **IR（Intermediate Representation，中间表示）**。GE 的 IR 叫 **AscendIR**——所有前端输入都被先翻译成 AscendIR，再统一做优化、编译、执行。

> 承接 u1-l2：我们已经知道 `graph_metadef` 目录定义了图基础结构与算子注册接口，是被全局共享的「全栈地基」。AscendIR 的数据结构就住在这个地基里。GE 仓**只维护图结构与注册机制**，算子的语义实现位于外部独立算子仓——本讲只看「图结构」这一半。

### 2.2 什么是计算图 / DAG

一个神经网络可以抽象成一张**有向无环图（DAG, Directed Acyclic Graph）**：

- **节点（Node）**：一个运算，比如矩阵乘、加法、卷积。
- **边（Edge）**：运算之间的数据流动方向。
- **无环**：数据只会往前流，不会绕回来形成死循环（控制流的循环另有机制，不在本讲展开）。

AscendIR 就是这样一张 DAG，但它的「边」不是独立对象——这一点很特别，4.1 节会细讲。

### 2.3 什么是张量（Tensor）

张量就是多维数组，是运算加工的「原料」。一个张量有两类信息：

- **元信息（描述）**：形状（shape，如 `[1, 3, 224, 224]`）、数据类型（dtype，如 FP32）、数据排布格式（format，如 NCHW）。这部分由 `GeTensorDesc` 承载。
- **实际数据**：具体的数值。这部分在编译期通常不存进 IR（除非是常量权重）。

本讲的「Tensor」层指的就是**张量描述** `GeTensorDesc`。

### 2.4 几个 C++ 用法提示

AscendIR 的头文件大量使用以下 C++ 惯用法，先有个印象即可，不必现在深究：

- **智能指针 `shared_ptr` / `weak_ptr`**：`shared_ptr` 是「共享所有权」的指针，多个持有者共同维护对象生命周期；`weak_ptr` 是「弱引用」，不增加引用计数，常用来避免循环引用（你持有我、我持有你导致内存永不释放）。
- **Pimpl 模式**：头文件只暴露一个 `ComputeGraph` / `Node`，真正的成员变量藏在一个 `ComputeGraphImpl` / `NodeImpl` 实现类里（用 `impl_` 指针持有）。好处是接口与实现解耦，改动实现不破坏二进制兼容。
- **`Vistor<T>` 返回类型**：这是 GE 自定义的 `RangeVistor`，你可以暂时把它当成「一种只读的容器视图」，可以用范围 for 循环遍历。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
|------|------|
| [docs/zh/design/modules/graph_metadef/ascend-ir.md](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/docs/zh/design/modules/graph_metadef/ascend-ir.md) | AscendIR 的官方设计文档，是本讲概念部分的主要依据。 |
| [inc/graph_metadef/graph/compute_graph.h](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/compute_graph.h) | `ComputeGraph` 类的对外接口——图的容器。 |
| [inc/graph_metadef/graph/node.h](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/node.h) | `Node` 类的对外接口——图中的算子节点。 |
| [inc/graph_metadef/graph/op_desc.h](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/op_desc.h) | `OpDesc` 类——算子描述符（四层模型的第三层，本讲只做定位，深入留到 u2-l3）。 |
| [inc/graph_metadef/graph/ge_tensor.h](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/ge_tensor.h) | `GeTensorDesc` / `GeShape` / `GeTensor`——张量描述（第四层）。 |

> 小提示：GE 头文件按 `inc/graph_metadef/graph/` 组织，是「图元数据定义」。`ComputeGraph` 是顶层入口，几乎所有图操作都从拿到一个 `ComputeGraphPtr`（即 `std::shared_ptr<ComputeGraph>`）开始。

## 4. 核心概念与源码讲解

### 4.1 四层对象模型

#### 4.1.1 概念说明

AscendIR 的核心对象模型由**四个层次**自上而下嵌套构成。一句话概括它们的职责：

- **`ComputeGraph`**：图的容器，管理节点集合、输入/输出节点、子图、拓扑排序。
- **`Node`**：图中的算子节点，持有一个 `OpDesc` 和一组连接锚点（Anchor）。
- **`OpDesc`**：算子描述符，定义算子的名称、类型、输入/输出张量描述、属性、推导函数。
- **`GeTensorDesc`**：张量描述，包含形状、数据类型、格式、内存布局等元信息。

这四层是**组合关系（has-a）**，而不是继承：图里装着很多节点，节点里装着一个算子描述，算子描述里描述着若干张量。

这里有一个 AscendIR 最关键、也最容易让初学者意外的设计：**图中不存在独立的 Edge（边）对象**。节点之间的连接关系，完全由节点自身携带的「锚点（Anchor）」互相引用来表达。这意味着图的结构 = 节点 + 节点内嵌的锚点引用，没有单独的边表。（Anchor 的细节是 u2-l2 的主题，本讲只需记住这个设计前提。）

AscendIR 的另一条根本原则是「**静态图 + 属性扩展**」：核心拓扑是一个编译期就固定的静态 DAG，所有「动态」信息（编译状态、融合标记、内存偏移……）都通过**属性（Attribute）**附加到这些静态对象上，而不会改变图的核心结构。这让「频繁改图」的编译优化（融合、死代码消除）变得很轻量。

#### 4.1.2 核心流程

四层模型的嵌套关系可以用下面这行伪结构表示（取自设计文档）：

```
ComputeGraph  →  Node  →  OpDesc  →  GeTensorDesc
                   ↕
                Anchor（内嵌于 Node，表达连边）
```

用包含关系画出来（这也是本讲实践任务要你亲手画的那张图）：

```
ComputeGraph（图容器）
├── nodes_：节点列表
│   └── Node（算子节点）
│        ├── op_desc_：OpDesc（算子描述）
│        │    ├── inputs_desc_：[GeTensorDesc, ...]（输入张量描述）
│        │    └── outputs_desc_：[GeTensorDesc, ...]（输出张量描述）
│        └── in/out 锚点集合（Anchor，表达连边）
├── input_nodes_：输入节点集合
└── output_nodes_info_：输出节点及其输出索引
```

注意：连边信息**不**单独存储，而是分散在每个 Node 的锚点里。所以「遍历邻居」本质上是「读某个节点某个锚点的对端引用」。

#### 4.1.3 源码精读

设计文档用一张 mermaid 类图把四层关系画得很清楚：

[docs/zh/design/modules/graph_metadef/ascend-ir.md:9-79](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/docs/zh/design/modules/graph_metadef/ascend-ir.md#L9-L79) —— AscendIR 四层对象模型的类图与包含关系总览，明确指出「图中不存在独立的 Edge 对象」。

落到代码上，四层类的声明分别位于：

[inc/graph_metadef/graph/compute_graph.h:46](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/compute_graph.h#L46) —— `ComputeGraph` 类声明。它继承自 `enable_shared_from_this` 与 `AttrHolder`（说明它自带属性能力）。

[inc/graph_metadef/graph/node.h:58](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/node.h#L58) —— `Node` 类声明，注释直言 `// Node is a component of ComputeGraph`。

[inc/graph_metadef/graph/op_desc.h:35](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/op_desc.h#L35) —— `OpDesc` 类声明，同样继承 `AttrHolder`。

[inc/graph_metadef/graph/ge_tensor.h:115](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/ge_tensor.h#L115) —— `GeTensorDesc` 类声明（第四层），描述张量元信息；同文件还有 [ge_tensor.h:41](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/ge_tensor.h#L41) 的 `GeShape`（形状）与 [ge_tensor.h:263](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/ge_tensor.h#L263) 的 `GeTensor`（带实际数据的张量）。

值得注意的设计共性：这四个类**都使用了 Pimpl 模式**——`ComputeGraph` 持有 `ComputeGraphImplPtr impl_`，`Node` 持有 `NodeImplPtr impl_`。真正的成员变量（如 `nodes_`、`peer_anchors_`）都藏在 `*Impl` 实现类里。这就是为什么你看头文件时，几乎看不到成员变量，只能看到一堆接口函数。

[docs/zh/design/modules/graph_metadef/ascend-ir.md:330-338](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/docs/zh/design/modules/graph_metadef/ascend-ir.md#L330-L338) —— 设计原则小结，其中「锚点优于边」「静态图 + 属性扩展」「Pimpl 隔离」「weak_ptr 防环」四条直接对应本节讲的设计。

#### 4.1.4 代码实践：画一张包含关系图

1. **实践目标**：亲手把四层模型的包含关系固化成一张图，建立空间记忆。
2. **操作步骤**：
   - 打开上面的四个头文件，分别确认四个类的声明行（46 / 58 / 35 / 115）。
   - 打开设计文档的 mermaid 类图段（9-79 行）作为参考。
   - 在纸上或任意画图工具里，按「外层包含内层」的方式画四个框：最外层 `ComputeGraph`，向内依次 `Node` → `OpDesc` → `GeTensorDesc`。
   - 在 `Node` 框旁补一个 `Anchor` 小框，用虚线连到 `Node`，并标注「连边由 Anchor 表达，无独立 Edge 对象」。
3. **需要观察的现象**：四个类都各自有 `Impl` 指针成员，头文件里看不到具体成员变量；`OpDesc` 与 `GeTensorDesc` 都继承 `AttrHolder`，说明它们都能挂属性。
4. **预期结果**：得到一张与 4.1.2 节伪结构一致的包含关系图。
5. **待本地验证**：无，本实践为纯源码阅读与画图，结论可由头文件直接确认。

#### 4.1.5 小练习与答案

**练习 1**：四层模型里，哪一层负责「张量的形状与数据类型」？哪一层负责「整个网络的拓扑结构」？

> **答案**：形状与数据类型由 `GeTensorDesc` 承载（其中形状进一步由 `GeShape` 表达）；整个网络拓扑由 `ComputeGraph` 承载——它管理节点列表、输入/输出节点与拓扑排序。

**练习 2**：为什么说 AscendIR 是「静态图 + 属性扩展」，而不是把动态信息直接塞进结构里？

> **答案**：让核心拓扑保持静态（编译期固定的 DAG），所有易变的编译状态（内存偏移、流分配、融合标记等）通过 `AttrHolder` 属性系统附加到现有对象上。这样图结构稳定、可序列化，而频繁的编译优化只需读写属性、局部改图，不必动核心结构。

**练习 3**：如果让你从源码判断一个类是不是「四层模型」的成员，你会看什么特征？

> **答案**：看它是否处于 `inc/graph_metadef/graph/` 目录、是否用 Pimpl 模式持有 `Impl` 指针、是否参与「图→节点→算子描述→张量描述」的包含链。`ComputeGraph`/`Node`/`OpDesc`/`GeTensorDesc` 同时满足这三点。

---

### 4.2 ComputeGraph 接口

#### 4.2.1 概念说明

`ComputeGraph` 是图的**顶层容器与门面**。你拿到一个模型，最终都会变成一个 `ComputeGraphPtr`。它对外提供三类能力：

- **节点管理**：增、删、插、融合节点。
- **图查询**：获取所有节点 / 输入节点 / 输出节点 / 子图。
- **拓扑排序**：把图里「乱序」的节点排成一个合法的执行顺序。

一个关键细节：节点列表 `nodes_` 在实现里用的是 `std::list`（链表）而不是 `std::vector`，因为编译优化会**频繁地在中间插入/删除节点**，链表在这类操作上更高效。设计文档对此有明确说明。

另一个本讲必须讲清的概念是**子图（subgraph）**。AscendIR 支持嵌套子图，这是表达控制流（If / While / Case）的基础。子图不是独立图，而是挂在某个父节点（如 `If`）上的附属结构：父节点的 `OpDesc` 记录了子图实例名称，`ComputeGraph` 通过名称映射找到对应子图。

#### 4.2.2 核心流程

**获取节点的流程**——注意区分「递归含子图」与「只取本层直接节点」：

```
GetAllNodes()        递归遍历当前图 + 所有子图节点      （通常用于需要全图的场景）
GetDirectNode()      只取当前图直接包含的节点          （不递归子图）
GetNodes(flag)       flag=false 等价 GetAllNodes
                     flag=true  等价 GetDirectNode     （按是否未知 shape 分派）
```

**添加 / 删除节点**：

```
AddNode(op)          用 OpDesc 创建 Node，Init 生成全部锚点，push_back 到 nodes_
RemoveNode(node)     复合操作：删常量输入 → 移出输入/输出表 → IsolateNode 旁路连边 → 从 nodes_ 移除
FuseNodeKeepTopo()   算子融合专用：把多个原始节点替换为融合算子，插在原 topo 序最小处
```

**拓扑排序流程**：

1. 从输入节点（入度为 0 的节点）出发。
2. 按 BFS / DFS 等策略逐步「消费」入度归零的节点，加入结果序列。
3. 每个节点排完后，给它的 `id` 赋值为排序结果中的位置索引。
4. 若排序后节点数 ≠ 总节点数，说明**图里存在环**（排序失败）。

四种策略的选择由配置项 `OPTION_TOPOSORTING_MODE` 控制：训练默认 BFS，推理默认 DFS。

#### 4.2.3 源码精读

先看类声明与拓扑排序策略枚举：

[inc/graph_metadef/graph/compute_graph.h:38-45](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/compute_graph.h#L38-L45) —— `TopoSortingMode` 枚举，列出 `kBFS / kDFS / kRDFS / kStableRDFS` 四种拓扑排序策略。

「获取所有节点」一族接口：

[inc/graph_metadef/graph/compute_graph.h:67-87](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/compute_graph.h#L67-L87) —— `GetAllNodesSize()` / `GetAllNodes()`（递归含子图）、`GetNodes(is_unknown_shape)`（带形状分派注释）、`GetDirectNode()`（只取本层）。注意 67 行注释明确：`is_unknown_shape:false → 同 GetAllNodes；true → 同 GetDirectNodes`。

「添加节点」一族接口：

[inc/graph_metadef/graph/compute_graph.h:94-96](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/compute_graph.h#L94-L96) —— `AddNode` 的三个重载：可传入已有的 `NodePtr`、由 `OpDescPtr` 创建、或带 `id`（用于反序列化）。

[inc/graph_metadef/graph/compute_graph.h:139-140](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/compute_graph.h#L139-L140) —— `FuseNodeKeepTopo`，算子融合专用插入接口，注释说它「性能更优，优先使用」，插在原节点集合中 topo 序最小的算子后面。

「删除节点」与「拓扑排序」：

[inc/graph_metadef/graph/compute_graph.h:148](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/compute_graph.h#L148) —— `RemoveNode`，注意它是一个复合操作（旁路被删节点的连边）。

[inc/graph_metadef/graph/compute_graph.h:185-191](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/compute_graph.h#L185-L191) —— `TopologicalSorting` 的三个重载：带自定义比较器、默认、指定 `TopoSortingMode`。

子图管理：

[inc/graph_metadef/graph/compute_graph.h:159-166](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/compute_graph.h#L159-L166) —— `AddSubgraph` / `GetSubgraph` / `GetAllSubgraphs`，注释强调子图必须先设置父图与父节点，且只能加到 root graph。

四种排序策略与适用场景，设计文档有对照表：

[docs/zh/design/modules/graph_metadef/ascend-ir.md:165-176](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/docs/zh/design/modules/graph_metadef/ascend-ir.md#L165-L176) —— 四种拓扑排序策略的算法与适用场景对照表（BFS 训练默认 / DFS 推理默认）。

#### 4.2.4 重点澄清：静态图 vs 静态 Shape 图

这是本讲的**易混淆点**，也是一个明确的学习目标。两个「静态」说的不是同一件事：

| 概念 | 含义 | 范围 |
|------|------|------|
| **静态图** | 图的**结构**（节点 + 连接拓扑）在编译期固定 | AscendIR 的根本属性，**所有**图都是静态图 |
| **静态 Shape 图（known shape）** | 不仅结构静态，张量**形状**也在编译期完全已知 | `GetGraphUnknownFlag() == false` |
| **动态 Shape 图（unknown shape）** | 结构静态，但形状要到**运行时**才知道 | `GetGraphUnknownFlag() == true` |

换句话说：AscendIR 永远是静态图；静态 Shape 图是静态图的一个**子集**（结构静态 + 形状已知）。源码里的判别开关是：

[inc/graph_metadef/graph/compute_graph.h:236-237](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/compute_graph.h#L236-L237) —— `GetGraphUnknownFlag()`，注释明确 `// false: known shape  true: unknow shape`。

这个标志位会影响节点获取的行为，也正是 `GetNodes(is_unknown_shape)` 存在的原因（known shape 用 `GetAllNodes` 递归全集；unknown shape 退化为 `GetDirectNode` 只取本层）。它也直接对应 u1-l2 提到的两套执行架构：known shape → v1 静态执行器；unknown shape → v2 动态执行器。

#### 4.2.5 代码实践：定位「添加节点 / 获取所有节点 / 拓扑排序」

1. **实践目标**：在不依赖本讲的前提下，自己在 `compute_graph.h` 里捞出三类核心接口。
2. **操作步骤**：
   - 打开 `inc/graph_metadef/graph/compute_graph.h`。
   - 用编辑器搜索 `AddNode`、`GetAllNodes`、`TopologicalSorting` 三个关键词。
   - 对每个命中，记下：函数签名、返回类型、是否重载、行号。
3. **需要观察的现象**：`AddNode` 有 3 个重载；`GetAllNodes` 返回 `Vistor<NodePtr>`；`TopologicalSorting` 有 3 个重载，其中 `graphStatus TopologicalSorting()` 是最常用的无参版本。
4. **预期结果**（应能找到并对应到行号）：

   | 类别 | 代表接口 | 行号 |
   |------|----------|------|
   | 添加节点 | `AddNode(const OpDescPtr op)` | 94-96 |
   | 获取所有节点 | `GetAllNodes() const` | 73 |
   | 拓扑排序 | `graphStatus TopologicalSorting()` | 185-191 |

5. **待本地验证**：无，纯头文件阅读。

#### 4.2.6 小练习与答案

**练习 1**：`GetAllNodes()` 和 `GetDirectNode()` 有什么区别？什么时候用哪个？

> **答案**：`GetAllNodes()` 递归包含所有子图的节点；`GetDirectNode()` 只返回当前图直接包含的节点，不进子图。需要处理整张图（含控制流子图）时用前者；只关心当前这一层结构时用后者。unknown shape 场景下 `GetNodes(true)` 会退化为 `GetDirectNode()`。

**练习 2**：为什么节点列表 `nodes_` 用 `std::list` 而不是 `std::vector`？

> **答案**：编译优化（融合、常量折叠、插入新算子）会频繁地在列表中间插入或删除节点。`std::list` 在任意位置增删是 O(1) 且不使其他迭代器失效，而 `std::vector` 中间增删是 O(n)。设计文档明确指出这是为了支持频繁的中间增删。

**练习 3**：如果一次 `TopologicalSorting()` 返回失败，最可能的原因是什么？

> **答案**：图中存在环（循环依赖）。排序算法会统计排好的节点数，若不等于总节点数，说明有节点始终无法入度归零——即存在环。AscendIR 要求合法的图是 DAG。

---

### 4.3 Node 接口

#### 4.3.1 概念说明

如果说 `ComputeGraph` 是「整张网」，`Node` 就是网上的「一个结」。每个 `Node` 表示一个算子实例，它主要持有两样东西：

- 一个 **`OpDesc`**：说明「我是什么算子、输入输出张量是什么、有哪些属性」。
- 一组 **Anchor（锚点）**：说明「我和谁连着、怎么连」。锚点是 Node 的一部分，所以连边信息内嵌在节点里。

要特别区分 `Node` 与 `OpDesc`：`OpDesc` 是**描述**（算子长什么样），`Node` 是**实例**（这个算子在图里的具体位置与连接）。同一个 `OpDesc` 描述可以对应图里不同的 `Node`。`Node` 还通过 `GetOwnerComputeGraph()` 反向知道自己属于哪张图。

#### 4.3.2 核心流程

从 `Node` 出发访问邻居的典型路径（锚点细节见 u2-l2，这里只看 Node 层封装）：

```
node->GetInDataNodes()      → 所有「向我喂数据」的上游节点
node->GetOutDataNodes()     → 所有「吃我数据」的下游节点
node->GetInControlNodes()   → 所有控制依赖上的上游节点
node->GetAllInDataAnchors() → 我的所有输入数据锚点（由此可进一步拿到对端）
node->GetOpDesc()           → 我的算子描述（读类型/属性/张量描述）
node->GetOwnerComputeGraph()→ 我所在的图
```

一个**项目约定的性能要点**（来自 `AGENTS.md` 代码风格）：GE 的 Node/Anchor 接口大多成对提供「返回智能指针」和「返回裸指针」两个版本。**只读遍历场景应优先用裸指针版本**（如 `GetOutDataNodesPtr()`、`GetAllInDataAnchorsPtr()`、`GetNamePtr()`），因为它不需要构造 `shared_ptr`，性能更好；只有「边遍历边改图」时才用智能指针版本。

#### 4.3.3 源码精读

类声明与初始化：

[inc/graph_metadef/graph/node.h:58](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/node.h#L58) —— `Node` 类声明，注释 `// Node is a component of ComputeGraph`，可见其与图的从属关系。

[inc/graph_metadef/graph/node.h:77](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/node.h#L77) —— `graphStatus Init()`，创建节点时据此按 `OpDesc` 的输入/输出数量生成全部 DataAnchor，并固定创建一对 ControlAnchor。

身份与归属：

[inc/graph_metadef/graph/node.h:79-82](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/node.h#L79-L82) —— `GetName()` / `GetType()`（以及性能更优的 `GetNamePtr()` / `GetTypePtr()`），分别取节点名称与算子类型。

[inc/graph_metadef/graph/node.h:84](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/node.h#L84) —— `GetOwnerComputeGraph()`，反向获取所属图。

锚点访问（成对的智能指针 / 裸指针版本）：

[inc/graph_metadef/graph/node.h:93-99](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/node.h#L93-L99) —— `GetAllInDataAnchors()`（智能指针，注释说「适用于边遍历边修改场景」）与 `GetAllInDataAnchorsPtr()`（裸指针，注释说「性能优于前者，适用于只读场景」）。这正是上面提到的项目性能约定。

[inc/graph_metadef/graph/node.h:105-111](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/node.h#L105-L111) —— `GetAllOutDataAnchors()` / `GetAllOutDataAnchorsPtr()`，输出锚点的成对版本。

邻居节点访问：

[inc/graph_metadef/graph/node.h:157-159](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/node.h#L157-L159) —— `GetInDataNodes()` / `GetInControlNodes()`，分别取上游数据节点与上游控制节点。

[inc/graph_metadef/graph/node.h:168-169](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/node.h#L168-L169) —— `GetOutDataNodes()` / `GetOutDataNodesPtr()`，下游数据节点的成对版本。

获取算子描述：

[inc/graph_metadef/graph/node.h:192-193](https://github.com/gitcode.com/cann/ge/blob/4ad52a6d6506af3a7f5be009b1fb6b0b3e8db0b5/inc/graph_metadef/graph/node.h#L192-L193) —— `GetOpDesc()` / `GetOpDescBarePtr()`，拿到节点持有的 `OpDesc`（同样有裸指针高性能版本）。

#### 4.3.4 代码实践：给定一个节点，列出它的下游消费者

1. **实践目标**：用 Node 接口写一段「从一个节点走到所有下游消费节点」的伪代码，理解连边信息如何从锚点聚合到节点层。
2. **操作步骤**：
   - 在 `node.h` 中确认 `GetOutDataNodesPtr()`（169 行）与 `GetOpDesc()`（192 行）的签名。
   - 阅读下面这段示例伪代码（**注意：以下为示例代码，不是项目原有代码**）：

```cpp
// 示例代码：给定一个 Node，打印它所有下游数据消费节点的名称
void DumpDownstream(const Node *node) {
  // 只读场景，优先用裸指针版本（项目性能约定）
  for (const Node *consumer : node->GetOutDataNodesPtr()) {
    // 从下游节点拿到其 OpDesc，从而读到算子类型
    auto op_desc = consumer->GetOpDescBarePtr();
    printf("downstream: %s (type=%s)\n",
           consumer->GetNamePtr(), op_desc->GetTypePtr());
  }
}
```

3. **需要观察的现象**：`GetOutDataNodesPtr()` 直接返回下游 `Node*` 列表——你**不需要**先去操作 OutDataAnchor 再跳到对端，Node 接口已经帮你聚合好了。这正是「锚点内嵌、O(1) 邻居访问」设计带来的便利。
4. **预期结果**：能把一个节点的全部下游消费者打印出来；若该节点是输出末端节点（没有下游），则列表为空。
5. **待本地验证**：本伪代码可在任何持有合法 `ComputeGraph` 的测试程序中验证；若暂无运行环境，可改为纯阅读 `node.h` 并口述调用链。

#### 4.3.5 小练习与答案

**练习 1**：`Node` 和 `OpDesc` 是什么关系？为什么要把它们分成两层？

> **答案**：`Node` **持有一个** `OpDesc`（has-a）。`OpDesc` 描述「算子长什么样」（类型、输入输出张量、属性、推导函数），`Node` 描述「这个算子在图里的具体实例」（位置、与邻居的连接、所属图）。分开后，`OpDesc` 可独立于图存在并复用，而图结构（拓扑、连边）只由 `Node` + Anchor 表达，职责清晰。

**练习 2**：`GetAllInDataAnchors()` 与 `GetAllInDataAnchorsPtr()` 该用哪个？

> **答案**：只读遍历用 `GetAllInDataAnchorsPtr()`（裸指针，不构造 `shared_ptr`，性能更好）；需要在遍历同时修改图结构（增删连边）时用 `GetAllInDataAnchors()`（智能指针版本，保证所有权安全）。这是 `AGENTS.md` 明确的项目约定，同类还有 `GetNamePtr()` / `GetPeerInDataAnchorsPtr()` 等。

**练习 3**：一个 `Node` 如何反向知道自己属于哪张图？

> **答案**：调用 `GetOwnerComputeGraph()`（返回所属 `ComputeGraphPtr`）或 `GetOwnerComputeGraphBarePtr()`（裸指针高性能版本）。这与 `ComputeGraph` 持有 `nodes_` 列表形成双向引用，但子图对父图的引用用 `weak_ptr` 以防循环引用（见设计原则「weak_ptr 防环」）。

---

## 5. 综合实践

把本讲的三块知识串起来：用 `ComputeGraph` + `Node` + `OpDesc` 接口，写出一段「建一个小图 → 排序 → 遍历打印」的伪代码。注意：本练习只用到本讲讲过的接口；**节点之间的连边（Anchor 连接）是 u2-l2 的内容，这里先用注释占位**。

**示例代码（非项目原有代码）**：

```cpp
// 示例代码：构建一个 3 节点小图并遍历（不含连边，连边见 u2-l2）
void BuildAndDump() {
  // 1) 创建图容器
  auto graph = std::make_shared<ComputeGraph>("my_graph");

  // 2) 用 OpDesc 添加 3 个节点（AddNode 见 compute_graph.h:94-96）
  auto n1 = graph->AddNode(std::make_shared<OpDesc>("x", "Data"));
  auto n2 = graph->AddNode(std::make_shared<OpDesc>("matmul", "MatMul"));
  auto n3 = graph->AddNode(std::make_shared<OpDesc>("y", "NetOutput"));

  // 3) 连边：n1 -> n2 -> n3（用 Anchor，留到 u2-l2 实践，此处略）

  // 4) 拓扑排序（TopologicalSorting 见 compute_graph.h:185-191）
  graph->TopologicalSorting();

  // 5) 遍历所有节点，打印名称、类型、上下游数量
  for (const Node *node : graph->GetAllNodesPtr()) {  // 只读用裸指针版本
    auto op = node->GetOpDescBarePtr();
    printf("node=%s type=%s in=%zu out=%zu\n",
           node->GetNamePtr(), op->GetTypePtr(),
           node->GetInDataNodesSize(), node->GetOutDataNodesSize());
  }
}
```

**完成检查清单**：

- [ ] 能解释为什么先 `AddNode` 再 `TopologicalSorting`（排序前节点顺序是插入序，排序后才得到合法执行序）。
- [ ] 能指出 `GetAllNodesPtr()` 与 `GetAllNodes()` 的取舍（只读用前者）。
- [ ] 能说清 `AddNode(OpDescPtr)` 内部会调用 `Node::Init()` 生成锚点（节点一创建就连边「插槽」就绪）。
- [ ] 能标注出连边为什么留到 u2-l2（AscendIR 没有独立 Edge，连边靠 Anchor 互相引用）。

> 提示：如果你想在真实环境运行，可以参考 GE 的 UT 测试（`tests/` 目录，u9-l5 会系统讲解）中构造 `ComputeGraph` 的写法；本讲聚焦数据结构本身，运行验证留到后续讲义。

## 6. 本讲小结

- AscendIR 用**四层对象模型** `ComputeGraph → Node → OpDesc → GeTensorDesc` 自上而下嵌套表达一张计算图，四层都是组合关系且都用 Pimpl 模式隔离实现。
- **没有独立的 Edge 对象**：连边由节点内嵌的 Anchor 互相引用表达——这是 AscendIR 最关键的设计特征，细节留待 u2-l2。
- `ComputeGraph` 是图容器，提供「节点管理 / 图查询 / 拓扑排序」三类接口；节点列表用 `std::list` 以支持频繁的中间增删；支持嵌套子图来表达控制流。
- 拓扑排序有 BFS / DFS / RDFS / StableRDFS 四种策略，训练默认 BFS、推理默认 DFS，排序失败意味着图中存在环。
- `Node` 持有 `OpDesc` 与一组 Anchor，并提供 `GetInDataNodes()` / `GetOutDataNodes()` 等「直接拿邻居」的便捷接口；项目约定只读遍历优先用裸指针版本（`*Ptr()`）以提升性能。
- **静态图** ≠ **静态 Shape 图**：所有 AscendIR 都是静态图（结构编译期固定）；是否「静态 Shape」由 `GetGraphUnknownFlag()` 区分，并对应 v1/v2 两套执行器。

## 7. 下一步学习建议

本讲建立了 AscendIR 的「骨架」——四层对象与 Graph/Node 接口。接下来按依赖顺序建议：

1. **u2-l2 Anchor 锚点：数据边与控制边的表达**——这是本讲反复提到的「连边」的真正实现。学完它，你才能把 4.3 节的「邻居访问」与 5 节综合实践里的连边补全。
2. **u2-l3 OpDesc 算子描述：输入输出与属性**——深入第三层 `OpDesc` 的属性存取与张量描述，承接本讲对 `OpDesc` 的初步定位。
3. **u2-l4 算子注册与原型体系**——理解 `OpDesc` 里的算子类型/输入输出从哪里来（外部算子仓经 `OpsProtoManager` 动态加载注册）。

如果想先看到「这些数据结构如何被消费」，可以跳到单元 4 的 u4-l1（编译器总览），那里会展示 `ComputeGraph` 如何进入编译四阶段。但建议先完成单元 2 的四篇，打好 IR 地基。
