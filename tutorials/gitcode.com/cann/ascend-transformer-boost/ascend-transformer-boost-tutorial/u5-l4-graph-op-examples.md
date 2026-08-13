# 图算子 Python 与 C++ 示例

## 1. 本讲目标

本讲是「通信算子与图算子机制」单元的收尾篇。在 u5-l2（图算子原理）和 u5-l3（GraphOpBuilder 组图实战）里，我们已经搞清楚了图算子的「内部机制」与「C++ fluent 组图 API」。本讲换一个视角，回答一个更实用的问题：

> 在真实代码里，我到底要怎么把一个图算子**搭起来并跑通**？Python 怎么写？C++ 怎么写？多个图、多个流又怎么协同？

学完本讲，你应当能够：

1. 看懂 `example/graph_example.py`，并用 Python 的 `torch_atb.Builder` 把「注意力 → 残差相加 → LayerNorm → 两次 Linear」这种 Transformer 子结构搭成一个图算子并执行。
2. 区分 ATB 提供的**三种组图入口**（原生 `GraphParam+Node`、`GraphOperationBuilder`、Python `Builder`），知道它们各自的抽象层级与适用场景。
3. 读懂 C++ 多图多流示例 `example/multiStream/multiStream_multiGraph_demo.cpp`，理解「图内嵌图（graph-in-graph）」「事件（Event）做图间同步」「一个 Context 绑一个流」的写法。

## 2. 前置知识

本讲默认你已经掌握下面这些前置概念（来自前置讲义）：

- **图算子的本质**（u5-l2）：图算子在**调度层**把若干已存在的 `Operation` 拼成 DAG，整体对外暴露为一个 `Operation`，**不做 kernel 融合**，收益来自减少 Host 下发开销、中间张量与 workspace 的内存复用。
- **tensorId 三段编址**（u5-l2、u5-l3）：整图的所有张量按 `输入 → 输出 → 中间` 三段、id 从小到大连续编号；共享同一个 `tensorId` 即自动连线；中间张量由图内部托管，调用方不必分配内存。
- **GraphOpBuilder**（u5-l3）：用字符串名称指代张量、由 Builder 自动算出数字 id 的 fluent 组图工具，核心接口为 `Init/Reshape/AddOperation/Build`。
- **Python 调用算子**（u2-l2）：`Param → Operation(param) → forward([输入张量])` 三步走；一行 `forward` 等价于 C++ 的 `Setup + Execute + 流同步`；输出张量由 ATB 自动推导形状后在 NPU 上创建。
- **Context 与执行流**（u1-l5、u7-l1）：`Context` 托管执行流集合；`SetExecuteStream` 设单流，`SetExecuteStreams` 设多流；多流场景需用 `aclrtSynchronizeStream` 同步。

如果某个术语让你陌生，请先回看对应讲义。本讲的重点是「**把前面学的 API 串成一个能跑的端到端例子**」。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `example/graph_example.py` | **Python 端图算子示例**。用 `torch_atb.Builder` 拼出 6 个节点的 Transformer 子块图算子并 `forward` 执行。 |
| `example/multiStream/multiStream_multiGraph_demo.cpp` | **C++ 多图多流（图间同步）示例**。手工构造两个图算子，分别绑在两条流上，用 Event 做图间同步，并演示「图内嵌图」。 |
| `example/multiStream/multiStream_singleGraph_demo.cpp` | 配套的「图内多流并行」示例（README 中成对出现），本讲作为对照提及。 |
| `example/atb_aclnn/atb/atb_graph_op.cpp` | **C++ 最朴素的图算子示例**。用原生 `GraphParam + Node` 手工搭一个 `(a+b)+(c+d)` 的三 Add 图，是理解多图示例的基础。 |
| `example/atb_aclnn/atb/atb_graph_op.h` | 上述示例的头文件，**头部注释**精炼说明了 tensorId 三段编址与拓扑排序两条铁律。 |
| `src/torch_atb/enger_graph_builder.cpp` | Python `Builder` 的 C++ 实现（`TorchAtb::GraphBuilder`），本讲用于解释 `add_input/add_node/mark_output/build` 背后的行为。 |
| `src/torch_atb/enger_graph_builder.h` | 上者的头文件，列出 `Builder` 全部公开接口。 |
| `src/torch_atb/graph_operation_builder.cpp` | C++ 侧 `GraphOperationBuilder`（对应 u5-l3 的 `atb::GraphOpBuilder` 思路，经 torch_atb 包装），是另一种 fluent 组图入口。 |
| `src/torch_atb/graph_operation_builder.h` | 上者的头文件。 |
| `src/torch_atb/bindings.cpp` | pybind11 绑定，揭示「Python `Builder` ↔ C++ `GraphBuilder`」「Python `GraphBuilder` ↔ C++ `GraphOperationBuilder`」两套命名映射。 |

> 一个容易混淆的点：Python 里的 `torch_atb.Builder` 和 `torch_atb.GraphBuilder` 是**两个不同的类**，下文 4.1.3 会专门讲清楚。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 Python 端图算子**：以 `graph_example.py` 为主线，讲 `Builder` 的组图与执行。
- **4.2 C++ 端图算子**：以 `atb_graph_op.cpp` 讲最朴素的原生组图，再引出 `GraphOperationBuilder`。
- **4.3 多流多图与事件同步**：以 `multiStream_multiGraph_demo.cpp` 讲图间协同。

### 4.1 Python 端图算子：Builder 组图与执行

#### 4.1.1 概念说明

`graph_example.py` 想表达的是一个**典型 Transformer 子块**的计算：

```
SelfAttention → ＋(残差) → LayerNorm → Linear → Tanh → Linear
```

如果用单算子方式调用，你需要依次创建 6 个 `Operation`、手动准备每个中间张量、6 次 `forward`、6 次 Host→Device 下发。而用**图算子**，你把这 6 个算子拼成一个整体，对外只暴露一个 `Operation`，一次 `forward` 即可：

- 6 个算子统一调度，中间张量由图内部托管（你不用分配）；
- workspace 在图内复用（取各节点最大值，见 u5-l2）；
- 减少 Host 下发次数，缓解 Host Bound。

Python 端的组图工具是 `torch_atb.Builder`。它的设计哲学是**「用字符串名字指代张量」**——你给每个输入、每个节点输出取个名字，Builder 在 `build()` 时自动把这些名字翻译成数字 `tensorId`，你完全不用手算 id（这正是 u5-l3 解决的痛点，在 Python 里被进一步简化）。

#### 4.1.2 核心流程

`graph_example.py` 的组图与执行流程可以概括为「**四步走**」：

1. **建图（`graph_build`）**：`graph = torch_atb.Builder("Graph")` 创建一个空图容器。
2. **声明输入**：对每个外部输入张量调用 `graph.add_input("名字")`，得到一个字符串句柄。
3. **逐个加节点**：调用 `graph.add_node([输入名字...], param)`，它内部用 `CreateOperation(param)` 建算子并接线；用 `node.get_output(0)` 取出本节点输出名字，喂给下一个节点。最后 `graph.mark_output(输出名字)` 标记整图输出。
4. **编译并执行（`run`）**：`Graph = graph.build()` 把名字编译成 `tensorId` 并产出 `Operation`；`Graph.forward([输入张量列表])` 一次执行整图。

整个过程是**声明式**的：你只描述「谁连到谁」，不描述「id 是几」「中间张量内存在哪」。流程示意：

```
Builder("Graph")
  → add_input × N            (登记输入名 → 输入段 id)
  → add_node(param) × M      (内部 CreateOperation；产出名挂为中间段)
     └ get_output(0)          (取出节点输出名作下一节点输入)
  → mark_output(name)        (把某中间名提升为输出段 id)
  → build()                  (名字 → 数字 id；产出 Operation)
  → forward([tensors])       (Setup + Execute + 同步)
```

#### 4.1.3 源码精读

**(a) 组图主函数 `graph_build`**

整个图的结构都在这一个函数里，先声明 4 个注意力相关输入并加第一个节点 SelfAttention：

[example/graph_example.py:42-53](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/graph_example.py#L42-L53) — 创建 `Builder`、`add_input` 登记输入、用 `SelfAttentionParam` 加第一个节点。

注意几个细节：

- `add_input("query")` 返回的就是字符串 `"query"`，后续直接拿字符串当张量句柄用。
- `add_node([query, key, value, seqLen], self_attention_param)` 的第一个参数是**输入名字列表**，顺序必须与算子定义的输入顺序一致（SelfAttention 的前 4 个输入是 query/key/value/seqLen）。
- `self_attention.get_output(0)` 取出该节点第 0 个输出的名字，作为下游节点的输入。这里 `0` 是输出下标，`get_output` 在 C++ 端返回的是一个**自动生成的字符串名**。

接着是残差相加 + LayerNorm + 两次 Linear（中间夹一个 Tanh），全部用同样的「`add_node` → `get_output`」模式串联：

[example/graph_example.py:55-83](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/graph_example.py#L55-L83) — ElewiseAdd(残差) → LayerNorm → Linear → ElewiseTanh → Linear，最后 `mark_output` 标记整图唯一输出。

末尾两行收尾：

[example/graph_example.py:83-85](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/graph_example.py#L83-L85) — `mark_output(linear_1_out)` 把最后一个 Linear 的输出提升为整图输出；`graph.build()` 编译产出图算子。

**(b) 执行：一次 forward 跑通整图**

[example/graph_example.py:88-92](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/graph_example.py#L88-L92) — `run()` 先建图、再准备输入列表、最后 `Graph.forward(inputs)` 执行。

输入列表的**顺序必须与 `add_input` 的调用顺序一致**。本例 `add_input` 顺序是 query、key、value、seqLen、input_0、gamma、beta、weight_0、bias_0、weight_1、bias_1（共 11 个）：

[example/graph_example.py:38-39](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/graph_example.py#L38-L39) — `get_inputs()` 把 11 个张量按固定顺序装进列表返回。

`forward` 内部就是 u2-l2 讲过的桥接：把 `torch::Tensor` 零拷贝转成 `atb::Tensor`，自动取 `thread_local` 的 Context 与当前流，自动管理 workspace，Setup+Execute+同步一条龙。

**(c) `add_input / add_node / mark_output / build` 背后做了什么**

要看懂 Builder 的行为，得读它的 C++ 实现 `enger_graph_builder.cpp`。注意 pybind11 绑定里的命名映射（容易踩坑）：

[src/torch_atb/bindings.cpp:104-177](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/bindings.cpp#L104-L177) — Python `torch_atb.Builder` 绑定到 C++ 类 `TorchAtb::GraphBuilder`，暴露 `add_input/add_node/reshape/mark_output/set_execute_streams/build`。

> 另一个绑定在 [src/torch_atb/bindings.cpp:179-184](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/bindings.cpp#L179-L184)：Python `torch_atb.GraphBuilder` 绑定到 C++ `TorchAtb::GraphOperationBuilder`（见 4.2.3）。**两者不同，别搞混**：`Builder` 是高层、名字驱动的；`GraphBuilder` 是低层、显式 `set_input_output/add_operation` 的。

`AddInput` 把名字登记进输入段，id 自增：

[src/torch_atb/enger_graph_builder.cpp:28-32](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/enger_graph_builder.cpp#L28-L32) — `AddInput` 把名字映射到输入段 id（`inTensorNum` 自增），返回名字本身。

`AddNode` 内部先用 `CreateOperation(param)` 建算子，并**自动为它的每个输出生成一个名字**，计入中间张量计数：

[src/torch_atb/enger_graph_builder.cpp:34-52](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/enger_graph_builder.cpp#L34-L52) — `AddNode` 设置算子、保存输入名字列表、按输出个数生成形如 `node{k}_outTensor{i}_{opName}` 的输出名字，`internalTensorNum++`。

> 这就解释了为什么你在 Python 里调 `self_attention.get_output(0)` 能拿到一个字符串——它是这里自动生成的 `node0_outTensor0_SelfAttention` 之类的名字。`get_output` 只是根据下标返回这个已生成的名字。

`MarkOutput` 的作用是把某个「原本算作中间张量」的名字**提升为整图输出**：它把该名字从中间段挪到输出段：

[src/torch_atb/enger_graph_builder.cpp:246-261](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/enger_graph_builder.cpp#L246-L261) — `MarkOutput` 先校验该名字确实是某个节点的输出，再给它分配输出段 id（`inTensorNum + outTensorNum++`），同时 `internalTensorNum--`（从中间段扣除）。

`Build` 是编译时刻：遍历所有节点，把字符串名字经 `GetTensorId` 翻译成数字 id，组装出最终的 `atb::GraphParam`：

[src/torch_atb/enger_graph_builder.cpp:263-299](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/enger_graph_builder.cpp#L263-L299) — `Build` 遍历 `graphNodes_`，逐节点把输入/输出名字翻译成 `tensorId`，处理 Reshape 挂载，最终 `ExecuteStreamsAssign()` 配流后返回 `OperationWrapper(graphParam_)`。

`GetTensorId` 是「名字→id」的查表核心，查找顺序正是 u5-l3 讲过的四段：输入、输出、reshape 视图、中间，首次出现则登记为中间段：

[src/torch_atb/enger_graph_builder.cpp:332-347](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/enger_graph_builder.cpp#L332-L347) — `GetTensorId` 按输入/输出/reshape/中间四段查找，未登记的名字就地新增为中间张量 id。

> 注意：Python `Builder` 与 u5-l3 的 C++ `atb::GraphOpBuilder` 在「自动编址」思想上完全一致，区别在于 `Builder` 还顺手帮你 `CreateOperation`（你只传 `param`），并且节点输出名字由它自动生成，比 C++ 版更省事。

#### 4.1.4 代码实践

**实践目标**：亲手读懂 `graph_example.py` 的组图拓扑，并能改动它。

**操作步骤**：

1. 打开 [example/graph_example.py:42-85](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/graph_example.py#L42-L85)。
2. 在纸上画出 6 个节点与它们的输入输出连线，标出哪些是外部输入（`add_input`）、哪些是中间张量（`get_output` 产物）、哪个是整图输出（`mark_output`）。
3. 对照 [example/graph_example.py:38-39](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/graph_example.py#L38-L39) 的输入列表，确认 11 个张量的顺序与 `add_input` 顺序一一对应。
4. **改图实验**：把第一个 Linear 之后的 `ELEWISE_TANH` 换成 `ELEWISE_GELU`（参照 u4-l3 的 `ElewiseParam.ElewiseType`），即在 [L74-75](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/graph_example.py#L74-L75) 处修改枚举值。这是纯组图改动，不涉及形状。

**需要观察的现象**：

- 改动只发生在「算子参数」层面，组图骨架（add_node/get_output 连线）不变，说明图算子把「拓扑」与「节点语义」解耦了。
- 若环境可用，运行 `python graph_example.py`，日志会打印 `forward` 的输出张量。

**预期结果**：

- 能正确画出 6 节点 DAG：`SelfAttention → ElewiseAdd(+input_0) → LayerNorm → Linear → ElewiseTanh → Linear`，整图 11 输入 1 输出。
- 改激活类型后，只要新枚举值合法，图仍能 `build()` 成功。

> 若本地无 NPU/CANN 环境，无法实际运行，此为「**待本地验证**」项；但组图分析与改图步骤可在纯阅读层面完成。

#### 4.1.5 小练习与答案

**练习 1**：`graph_example.py` 里为什么没有出现任何数字 `tensorId`？这些 id 在什么时候、由谁算出来？

**答案**：因为 Python `Builder` 用字符串名字指代张量，把数字 id 的管理完全隐藏。id 在调用 `graph.build()` 时，由 `Build()` 遍历各节点、经 `GetTensorId`（[enger_graph_builder.cpp:332-347](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/enger_graph_builder.cpp#L332-L347)）查表/登记后写进 `atb::GraphParam`。

**练习 2**：如果把 [L83](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/graph_example.py#L83) 的 `mark_output(linear_1_out)` 删掉，会发生什么？

**答案**：整图将没有任何输出张量（`outTensorNum` 为 0），`linear_1_out` 仍被算作中间张量。`Build` 虽可能不报错，但产出的图算子没有对外输出，`forward` 拿不到结果——这违背了「整图必须有输出」的语义，`MarkOutput` 的作用正是把中间名提升为输出段（见 [enger_graph_builder.cpp:246-261](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/enger_graph_builder.cpp#L246-L261)）。

---

### 4.2 C++ 端图算子：原生 GraphParam + Node 与 GraphOperationBuilder

#### 4.2.1 概念说明

Python `Builder` 很方便，但在 C++ 侧，ATB 提供了**两条**组图路径，抽象层级不同：

1. **原生 `atb::GraphParam + atb::Node`（最底层）**：你自己填 `inTensorNum/outTensorNum/internalTensorNum`、自己填每个 `Node` 的 `operation`、`inTensorIds`、`outTensorIds`（直接写数字 id），最后 `CreateOperation(graphParam, &op)`。代表示例是 `atb_graph_op.cpp` 和多图 demo。
2. **`GraphOperationBuilder` / `atb::GraphOpBuilder`（fluent 层）**：用字符串名字 + `SetInputOutput/AddOperation/Build`，Builder 帮你算 id。这正是 u5-l3 讲过的 `atb::GraphOpBuilder` 的 torch_atb 包装版，实现在 `graph_operation_builder.cpp`。

为什么要了解底层？因为多图多流示例（4.3）必须在原生层操作——它要用到「图节点里嵌一个子图算子」「节点是 Event 同步算子」等高级特性，这些在 fluent Builder 里并不直接暴露。**先懂原生，才能读懂 4.3**。

#### 4.2.2 核心流程

原生组图有两条铁律，`atb_graph_op.h` 的头部注释说得最清楚：

[example/atb_aclnn/atb/atb_graph_op.h:10-12](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/atb_aclnn/atb/atb_graph_op.h#L10-L12) — 注释明确两条规则：① tensorId 从小到大必须是「输入、输出、中间」顺序，且每段个数与参数一致；② Node 必须按计算图的**拓扑序**排成有序队列。

也就是说，原生组图的流程是：

1. 规划张量编号：先数有几个输入、几个输出、几个中间，按 `输入段 → 输出段 → 中间段` 分配连续 id。
2. 对每个 `Node`：用 `CreateOperation(param, &node.operation)` 建算子，再填 `inTensorIds`/`outTensorIds`（数字数组）。
3. 把节点按拓扑序塞进 `graphParam.nodes`，填好三个 `*TensorNum`。
4. `CreateOperation(graphParam, &operation)` 把整图编译成一个 `Operation`。

#### 4.2.3 源码精读

**(a) 最朴素示例：`(a+b)+(c+d)`**

`atb_graph_op.cpp` 用 3 个 ElewiseAdd 拼出 `(a+b)+(c+d)`，是理解原生组图的最佳起点：

[example/atb_aclnn/atb/atb_graph_op.cpp:10-65](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/atb_aclnn/atb/atb_graph_op.cpp#L10-L65) — 整图 4 输入(a,b,c,d)、1 输出(ADD3_OUT)、2 中间(ADD1_OUT,ADD2_OUT)、3 个 Add 节点。

逐行拆解关键部分。先声明三个数量字段与节点数组：

[example/atb_aclnn/atb/atb_graph_op.cpp:16-21](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/atb_aclnn/atb/atb_graph_op.cpp#L16-L21) — `inTensorNum=4, outTensorNum=1, internalTensorNum=2, nodes.resize(3)`。

为了不写「魔法数字」id，示例用一个 `enum class InTensorId` 给 7 个张量（4 输入 + 1 输出 + 2 中间）取了可读名字，并用 `toU` 把枚举转成底层 `int` 作为 id：

[example/atb_aclnn/atb/atb_graph_op.cpp:23-32](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/atb_aclnn/atb/atb_graph_op.cpp#L23-L32) — 枚举值天然满足 `输入(0-3) → 输出(4) → 中间(5,6)` 的升序，与铁律①吻合。

> 注意：枚举里 `ADD3_OUT=4`（输出，排第一段输出）、`ADD1_OUT=5`、`ADD2_OUT=6`（中间）。这正是「输出段在中间段之前」的体现。

然后每个节点都是「建算子 + 填 id」两步。第一个 Add：输入 a,b → 输出 ADD1_OUT：

[example/atb_aclnn/atb/atb_graph_op.cpp:39-45](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/atb_aclnn/atb/atb_graph_op.cpp#L39-L45) — `CreateOperation(addParam, &addNode.operation)` 建算子；`inTensorIds={A,B}`、`outTensorIds={ADD1_OUT}`。

第三個 Add 把两个中间结果相加，输出整图结果：

[example/atb_aclnn/atb/atb_graph_op.cpp:54-59](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/atb_aclnn/atb/atb_graph_op.cpp#L54-L59) — `inTensorIds={ADD1_OUT, ADD2_OUT}`、`outTensorIds={ADD3_OUT}`，即 `(a+b)+(c+d)`。

最后用 `GraphParam` 把整图编译成一个 `Operation`：

[example/atb_aclnn/atb/atb_graph_op.cpp:61-62](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/atb_aclnn/atb/atb_graph_op.cpp#L61-L62) — `CreateOperation(opGraph, operation)` 产出图算子，返回 `atb::NO_ERROR` 表示成功。

> 由此你看到原生组图与 Python `Builder` 的根本差异：**这里 id 是你手写的数字（或枚举），节点输出名字不存在，节点顺序就是数组顺序**。Python `Builder` 把这些全自动化了。

**(b) fluent 层：`GraphOperationBuilder`**

`graph_operation_builder.cpp` 是 C++ 侧的 fluent Builder，思路与 u5-l3 的 `atb::GraphOpBuilder` 一致，只是放在 `TorchAtb` 命名空间下、经 pybind11 暴露为 Python `torch_atb.GraphBuilder`。它的接口更接近 u5-l3：先 `SetInputOutput` 声明输入输出名，再逐个 `AddOperation`，最后 `Build`。

`SetInputOutput` 把输入输出名字分别登记到输入段、输出段（输出段 id 紧接输入段）：

[src/torch_atb/graph_operation_builder.cpp:25-46](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/graph_operation_builder.cpp#L25-L46) — 遍历 `inTensorNames` 登记输入段 id（自增）、再遍历 `outTensorNames` 登记输出段 id，并写入 `graphParam_.inTensorNum/outTensorNum`，全程校验张量数 ≤ 256。

`AddOperation` 把一个已建好的 `Operation`（经 `OperationWrapper` 传入）加进图，输入输出名字经 `GetTensorId` 翻译，并按需挂 Reshape 函数：

[src/torch_atb/graph_operation_builder.cpp:48-82](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/graph_operation_builder.cpp#L48-L82) — 从 `opWrapper` 取出裸 `Operation*`，构造 `Node`，对每个输入名查 `GetTensorId` 并挂 reshape，对每个输出名查 `GetTensorId`，最后 `push_back` 进 `graphParam_.nodes`。

`GetTensorId` 与 `enger_graph_builder.cpp` 里的版本同构——四段查找，首次出现登记为中间段：

[src/torch_atb/graph_operation_builder.cpp:110-125](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/graph_operation_builder.cpp#L110-L125) — 输入/输出/reshape/中间四段查表，未登记则 `internalTensorId = inIds.size()+outIds.size()+internalTensorNum++`。

`Build` 收尾，填上 `internalTensorNum` 并产出 `OperationWrapper`：

[src/torch_atb/graph_operation_builder.cpp:104-108](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/graph_operation_builder.cpp#L104-L108) — `Build` 写入 `graphParam_.internalTensorNum`，返回包裹了 `GraphParam` 的 `OperationWrapper`。

> 三个组图入口的关系，一张表收束：
>
> | 入口 | id 管理 | 是否自动建算子 | 代表文件 |
> | --- | --- | --- | --- |
> | 原生 `GraphParam+Node` | 手写数字 id | 否（自己 `CreateOperation`） | `atb_graph_op.cpp`、`multiStream_multiGraph_demo.cpp` |
> | `atb::GraphOpBuilder`（u5-l3）/ `GraphOperationBuilder` | 名字→自动 id | 否（传 `Operation*`） | `graph_operation_builder.cpp` |
> | Python `torch_atb.Builder` | 名字→自动 id | **是**（只传 `param`） | `enger_graph_builder.cpp`、`graph_example.py` |

#### 4.2.4 代码实践

**实践目标**：用原生 `GraphParam+Node` 在脑中（或纸上）复现一个最小图，理解每个字段的来源。

**操作步骤**：

1. 阅读 [example/atb_aclnn/atb/atb_graph_op.cpp:16-62](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/atb_aclnn/atb/atb_graph_op.cpp#L16-L62)。
2. 列一张表：7 个张量（a,b,c,d,ADD3_OUT,ADD1_OUT,ADD2_OUT）各自属于输入段/输出段/中间段，id 是几。
3. 列出 3 个节点各自的 `inTensorIds` 与 `outTensorIds`。
4. **对比练习**：把同一张 `(a+b)+(c+d)` 图，改用 Python `Builder` 写出伪代码（`add_input` × 4 → `add_node(ADD)` × 3 → `mark_output` → `build`），体会「不用手写 id」的差异。

**需要观察的现象**：

- 原生写法里，节点输出 id（如 `ADD1_OUT=5`）必须落在中间段（≥ 输入数+输出数 = 4+1 = 5），与铁律①一致。
- Python 版本里，你完全不需要知道这些数字。

**预期结果**：

- 张量表：a=0,b=1,c=2,d=3（输入）；ADD3_OUT=4（输出）；ADD1_OUT=5,ADD2_OUT=6（中间）。
- 节点表：node1 {in:[0,1], out:[5]}、node2 {in:[2,3], out:[6]}、node3 {in:[5,6], out:[4]}。

> 此为「源码阅读型实践」，无需运行即可完成。

#### 4.2.5 小练习与答案

**练习 1**：`atb_graph_op.cpp` 为什么把 `ADD3_OUT`（整图输出）的枚举值定义为 4，排在两个中间张量（5、6）之前？

**答案**：因为 tensorId 三段编址铁律要求「输入段 → 输出段 → 中间段」升序排列。4 个输入占 0-3，输出段必须紧随其后从 4 开始，中间段再排在输出段之后（5、6）。把输出枚举值定为 4 正好满足这一顺序。

**练习 2**：`graph_operation_builder.cpp` 的 `AddOperation` 接收的是 `OperationWrapper&`（已建好的算子），而 Python `Builder` 的 `add_node` 接收的是 `param`。这两者谁更「省事」？为什么？

**答案**：Python `Builder` 更省事。因为 `Builder.add_node` 内部会调用 `CreateOperation(param, &op)` 自动建算子（见 [enger_graph_builder.cpp:54-63](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/enger_graph_builder.cpp#L54-L63) 的 `AddNodeByParamType`），用户只传参数对象；而 `GraphOperationBuilder.AddOperation` 要求用户先自己把算子建好、包成 `OperationWrapper` 再传入。后者更灵活（可以传入任意已存在的 `Operation*`，包括另一个图算子），但写法更繁琐。

---

### 4.3 多流多图与事件同步

#### 4.3.1 概念说明

前面两节都是「**一个**图算子」。真实推理里，你常常需要**多个图算子并行**——比如一个图跑在 stream1、另一个图跑在 stream2，让 NPU 上两段计算重叠，进一步压榨吞吐。这就引出两个新问题：

1. **怎么让两个图绑在不同的流上？** —— 每个图算子在 `Execute` 时需要一个 `Context`，而 `Context` 绑定执行流（u1-l5）。所以「多图多流」的常见写法是：**为每条流建一个 `Context`，把图算子在对应 Context 上 Setup/Execute**。
2. **两个图如果共享某个张量（有数据依赖），怎么在流之间同步？** —— 用 ACL 的 **Event（事件）** 机制：在一个流的图里插一个「RECORD 节点」记录事件，在另一个流的图里插一个「WAIT 节点」等待该事件。ATB 把 Event 也做成了 `Operation`（`atb::common::EventParam`），可以像普通节点一样嵌进图里。

`multiStream_multiGraph_demo.cpp` 正是同时演示这两点的「图间同步」示例（README 里与之成对的是「图内多流并行」的 singleGraph demo）。它还顺带演示了一个高级特性：**图内嵌图（graph-in-graph）**——一个 `Node` 的 `operation` 本身可以是另一个 `GraphOperation`。

#### 4.3.2 核心流程

`main` 的整体骨架（去掉资源准备后）是：

```
aclInit
 ├─ aclrtSetDevice
 ├─ aclrtCreateStream × 2          (stream1, stream2)
 ├─ aclrtCreateEventWithFlag        (一个共享 event)
 ├─ CreateContext × 2               (contextWR 绑 stream1, contextRW 绑 stream2)
 ├─ 构造 operationWR (Wait-Record 图)
 ├─ 构造 operationRW (Record-Wait 图)
 ├─ 准备两个 VariantPack (WR / RW)
 ├─ operationWR->Setup / Execute   (在 contextWR=stream1)
 ├─ operationRW->Setup / Execute   (在 contextRW=stream2)
 ├─ aclrtSynchronizeStream × 2
 └─ 资源释放（算子→Context→张量→workspace→event→stream→device→aclFinalize）
```

两个图的节点序列刻意设计成「互补」：

- **operationWR**（先 Wait 再 Record）：`mul → WAIT(event) → add → 子图(mini graph) → RECORD(event)`
- **operationRW**（先 Record 再 Wait）：`mul → RECORD(event) → add → 子图(mini graph) → WAIT(event)`

两者共享同一个 `event`。这样 stream1 上的图 Record 之后，stream2 上的图 Wait 才能继续（反之亦然），实现了两条流之间的回合制同步。

#### 4.3.3 源码精读

**(a) 图内嵌图：`CreateMiniGraphOperation`**

多图示例里，主图的某个节点本身是一个**子图算子**。这个子图由 `CreateMiniGraphOperation` 构造——它就是 4.2 那种原生组图：2 输入、1 输出、2 中间、3 个 Add 节点：

[example/multiStream/multiStream_multiGraph_demo.cpp:64-96](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/multiStream/multiStream_multiGraph_demo.cpp#L64-L96) — 构造一个三连 Add 的子图：`addNode{0,1→3} → addNode2{3,1→4} → addNode3{4,1→2}`，最后 `CreateOperation(opGraph, operation)` 把它编译成一个 `Operation*`。

> 关键认知：`Node::operation` 的类型是 `atb::Operation*`，它既可以是普通单算子（如 ElewiseAdd），也可以是**另一个图算子**。图算子可以无限嵌套，对外仍是一个 `Operation`。这就是「图内嵌图」。

**(b) 把 Event 当节点：`CreateGraphOperationWithWREvent`**

主图 operationWR 有 5 个节点：mul、WAIT、add、子图、RECORD。注意中间两个节点不是计算算子，而是 `EventParam` 算子：

[example/multiStream/multiStream_multiGraph_demo.cpp:98-140](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/multiStream/multiStream_multiGraph_demo.cpp#L98-L140) — `CreateGraphOperationWithWREvent` 构造 5 节点图：`mul{0,1→3}` → `WAIT(event)` → `add{0,1→4}` → 子图`{3,4→2}` → `RECORD(event)`。

其中 WAIT 与 RECORD 节点用 `atb::common::EventParam` 创建，区别只在 `operatorType`：

[example/multiStream/multiStream_multiGraph_demo.cpp:118-121](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/multiStream/multiStream_multiGraph_demo.cpp#L118-L121) — WAIT 节点：`EventParam.event = event; operatorType = WAIT`，`CreateOperation` 建成一个等待事件的算子。

[example/multiStream/multiStream_multiGraph_demo.cpp:134-137](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/multiStream/multiStream_multiGraph_demo.cpp#L134-L137) — RECORD 节点：同一个 `event`，但 `operatorType = RECORD`，建成一个记录事件的算子。

> 这种「把同步原语做成图节点」的设计，让你可以在**组图阶段**就声明好流间依赖，而不必在执行时手动插 `aclrtRecordEvent/aclrtWaitEvent`。图算子的统一抽象（一切皆 `Operation`）在这里发挥了价值。

配套的 `CreateGraphOperationWithRWEvent` 结构对称，只是 RECORD/WAIT 顺序对调：

[example/multiStream/multiStream_multiGraph_demo.cpp:142-184](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/multiStream/multiStream_multiGraph_demo.cpp#L142-L184) — `CreateGraphOperationWithRWEvent`：`mul → RECORD → add → 子图 → WAIT`，与 WR 版本共享同一 event，形成回合制同步。

**(c) 双 Context 绑双流 + 双图执行**

`main` 里为两条流各建一个 Context，这是「多图多流」的核心写法：

[example/multiStream/multiStream_multiGraph_demo.cpp:201-206](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/multiStream/multiStream_multiGraph_demo.cpp#L201-L206) — 建两个 Context：`contextWR->SetExecuteStream(stream1)`、`contextRW->SetExecuteStream(stream2)`。

两个图分别在各自 Context 上 Setup（拿 workspaceSize）和 Execute（异步下发）：

[example/multiStream/multiStream_multiGraph_demo.cpp:265-266](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/multiStream/multiStream_multiGraph_demo.cpp#L265-L266) — `operationWR->Setup(packWR, ..., contextWR)` 与 `operationRW->Setup(packRW, ..., contextRW)`，分别在自己的 Context（流）上规划。

[example/multiStream/multiStream_multiGraph_demo.cpp:281-283](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/multiStream/multiStream_multiGraph_demo.cpp#L281-L283) — `operationWR->Execute(..., contextWR)` 与 `operationRW->Execute(..., contextRW)`，两个图分别在 stream1、stream2 上异步下发。

> 两个 `Execute` 都是**异步**的（仅把任务压到各自流上，立即返回）。因此图间的真实执行顺序由 event 同步保证，而非由这两行代码的先后决定。

最后分别同步两条流，确保所有 NPU 任务完成：

[example/multiStream/multiStream_multiGraph_demo.cpp:286-296](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/multiStream/multiStream_multiGraph_demo.cpp#L286-L296) — `aclrtSynchronizeStream(stream1)` 与 `aclrtSynchronizeStream(stream2)`，等待两条流都跑完。

资源释放严格按 u2-l1 讲过的「创建逆序」：先销毁图算子、再销毁 Context、再释放张量/workspace、最后 event/stream/device/aclFinalize：

[example/multiStream/multiStream_multiGraph_demo.cpp:298-321](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/multiStream/multiStream_multiGraph_demo.cpp#L298-L321) — `DestroyOperation` → `DestroyContext` → `aclrtFree`(张量/workspace) → `aclrtDestroyEvent` → `aclrtDestroyStream` × 2 → `aclrtResetDevice` → `aclFinalize`。

> **图内多流 vs 图间同步**：本 demo 是「图间同步」（两个独立图、两个 Context、两条流）。README 里成对的 `multiStream_singleGraph_demo.cpp` 是「图内多流并行」（一个图内不同节点跑在不同 streamId 上，通过 `Context::SetExecuteStreams` + 节点级 `SetExecuteStreamId` 路由，详见 u7-l1）。两者是不同维度的并行，别混淆。

#### 4.3.4 代码实践

**实践目标**：理清多图示例里「两个图 + 两条流 + 一个 event」的同步时序。

**操作步骤**：

1. 阅读 [example/multiStream/multiStream_multiGraph_demo.cpp:64-184](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/multiStream/multiStream_multiGraph_demo.cpp#L64-L184) 的三个 `Create*` 函数。
2. 画出两个图（WR、RW）的节点序列，标出每个节点的类型（计算 / WAIT / RECORD / 子图）与共享 `event`。
3. 推演时序：假设 stream1 跑 WR、stream2 跑 RW。WR 先到 WAIT 会阻塞，直到 RW 执行完它的 RECORD；反之亦然。画出「回合制」依赖。
4. 若要实际编译运行，按 README 步骤：把 `CMakeLists.txt` 的 `add_executable` 指向 `multiStream_multiGraph_demo.cpp`，`mkdir build && cd build && cmake .. -DUSE_CXX11_ABI=OFF`（或 ON，需与 CANN/PyTorch ABI 对齐，见 u1-l3），`cmake --build .`，`./multiStreamDemo`。

**需要观察的现象**：

- 两个图共享同一个 `event` 对象（[L198-199](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/multiStream/multiStream_multiGraph_demo.cpp#L198-L199) 创建，分别传给两个 `Create*` 函数）。
- 两个图的 `Execute` 调用先后（[L281-283](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/multiStream/multiStream_multiGraph_demo.cpp#L281-L283)）并不决定真实执行顺序——真实顺序由 event 同步决定。

**预期结果**：

- 能正确画出两条流的时序图，标出 WAIT/RECORD 的等待关系。
- 运行输出 `multi graph multi-stream demo start`（[L263](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/multiStream/multiStream_multiGraph_demo.cpp#L263)）后正常退出（返回码 0）。

> 编译运行依赖 CANN/nnal 环境与 NPU 设备，若本地不具备，时序推演部分仍可完成，实际运行为「**待本地验证**」。

#### 4.3.5 小练习与答案

**练习 1**：`multiStream_multiGraph_demo.cpp` 为什么要建**两个** `Context`，而不是共用一个？

**答案**：因为一个 `Context` 通过 `SetExecuteStream` 绑定**一条**执行流（[L203-206](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/multiStream/multiStream_multiGraph_demo.cpp#L203-L206)）。要让两个图算子分别跑在 stream1、stream2 上，就需要两个各自绑流的 Context，并在 `Execute` 时传入对应 Context。Context 还托管 TilingBufferPool、RunnerPool等资源（u3-l5），两个图用独立 Context 也避免资源争用。

**练习 2**：如果把 `operationWR` 与 `operationRW` 共享的 `event` 改成**两个不同的** event（各自一个），会发生什么？

**答案**：两个图将失去同步耦合——WR 的 WAIT 等的是 event_A，而 RW 的 RECORD 记录的是 event_B，两者互不相关。于是两条流各跑各的，原本的「回合制」依赖被破坏，可能出现数据竞争（如果两图真有共享张量依赖）。这正说明共享同一个 event 是建立流间依赖的关键。

**练习 3**：本 demo 中 `CreateMiniGraphOperation` 构造的子图，被当作主图的一个节点塞进去（`graphNode.operation`）。这体现了 ATB 图算子的什么特性？

**答案**：体现了「图算子也是普通 `Operation`，可被嵌套（graph-in-graph）」的特性（[L123-124](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/multiStream/multiStream_multiGraph_demo.cpp#L123-L124)）。`Node::operation` 是 `atb::Operation*`，既可以是单算子也可以是另一个图算子，于是可以把复杂模型按模块拆成多层图，再组合成更大的图，对外始终是一个 `Operation`。

## 5. 综合实践

**任务：用三种方式表达同一个图，并对比它们的抽象层级。**

选取一个简单计算：`y = LayerNorm(x + Linear(x))`（即残差相加后做 LayerNorm），输入 `x`、权重 `w`、偏置 `b`、`gamma`、`beta` 共 5 个，输出 1 个。

请完成：

1. **Python `Builder` 版**：参照 [graph_example.py](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/graph_example.py#L42-L85)，写出 `add_input` × 5 → `add_node(Linear)` → `add_node(ElewiseAdd 残差)` → `add_node(LayerNorm)` → `mark_output` → `build` 的伪代码。注意：残差相加需要把 `x` 与 Linear 输出都作为 ElewiseAdd 的输入，所以 `x` 会被两个节点引用——这正是「同一个名字在多处复用」的典型场景。

2. **原生 `GraphParam+Node` 版**：参照 [atb_graph_op.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/atb_aclnn/atb/atb_graph_op.cpp#L16-L62)，手写 5 个输入、1 个输出、2 个中间（Linear 输出、Add 输出）的 id 分配表，列出 3 个节点的 `inTensorIds/outTensorIds`。

3. **对比**：用一个表格总结三种入口（原生 / `GraphOperationBuilder` / Python `Builder`）在「id 管理、是否自动建算子、能否嵌套子图、能否插 Event 节点」四个维度上的差异，并说明各自适合的场景。

**预期产出**：

- Python 版伪代码（约 10 行）。
- 原生版 id 表与节点表。
- 三入口对比表，结论大致是：Python `Builder` 最适合快速搭模型；原生 `GraphParam+Node` 最适合需要精细控制（多流、Event、嵌套子图）的场景；`GraphOperationBuilder`/`atb::GraphOpBuilder` 介于两者之间，适合 C++ 侧中等复杂度的组图。

> 本实践为「设计与阅读型」，无需运行环境即可完成；若要运行 Python 版，需具备 torch_atb 与 NPU 环境（**待本地验证**）。

## 6. 本讲小结

- **Python 图算子**用 `torch_atb.Builder`（C++ 类 `TorchAtb::GraphBuilder`）以「字符串名字」组图：`add_input` 登记输入、`add_node(param)` 自动建算子并接线、`get_output` 取输出名、`mark_output` 提升整图输出、`build` 编译成 `Operation`、`forward` 一次执行。`graph_example.py` 把 SelfAttention→残差→LayerNorm→Linear→Tanh→Linear 拼成了一个 6 节点 Transformer 子块。
- **名字到 id 的翻译**发生在 `build()` 时刻，由 `GetTensorId` 按「输入/输出/reshape/中间」四段查表完成；`mark_output` 把中间名挪到输出段。Python 用户从头到尾不碰数字 id。
- **C++ 有三条组图入口**，抽象层级递增：原生 `GraphParam+Node`（手写 id、自己建算子，最灵活）→ `GraphOperationBuilder`/`atb::GraphOpBuilder`（名字→自动 id，传入已建算子）→ Python `Builder`（名字→自动 id 且自动建算子，最省事）。
- **tensorId 三段编址铁律**（输入→输出→中间，升序）与 **Node 拓扑序**是所有原生组图的共同约束，`atb_graph_op.h` 头注释把它们点破。
- **多图多流**的写法是「每条流一个 Context、每图一个图算子」，两个 `Execute` 都是异步下发；**图间同步**靠把 ACL Event 做成 `EventParam` 节点（RECORD/WAIT）嵌进图里，两个图共享同一 event 形成回合制依赖。
- **图内嵌图（graph-in-graph）**：`Node::operation` 是 `atb::Operation*`，既可装单算子也可装另一个图算子，于是图可以按模块层层嵌套，对外始终是一个 `Operation`。

## 7. 下一步学习建议

本讲是「通信算子与图算子机制」单元（u5）的结束。到这里，你已经具备了用 ATB 拼装和执行图算子的完整能力。接下来的学习建议：

1. **进入自定义算子开发（u6 单元）**：图算子是「组合已有算子」，u6 教你「造新算子」——从 AscendC Kernel、Tiling，到 Operation/Runner 集成、注册与交付件。推荐从 [u6-l1 插件机制](#) 开始，理解 `OperationInfra/PluginOperation` 如何把用户算子嵌入框架。
2. **深入多流与 Tiling 调度（u7-l1）**：本讲的「图间同步」是粗粒度并行；u7-l1 会讲「图内多流并行」（`SetExecuteStreams` + 节点级 `streamId` 路由，即 singleGraph demo 的机制）、异步 Tiling 拷贝与两段式下发，是性能调优的关键。
3. **性能与调试（u7-l2）**：想验证你的图算子是否真的减少了 Host 下发、是否并行起来了，学会用 `ProfStats` 与 ATB 日志环境变量采集运行时数据。
4. **源码延伸阅读**：回头对照 [enger_graph_builder.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/enger_graph_builder.cpp) 的 `ExecuteStreamsAssign`（[L313-330](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/torch_atb/enger_graph_builder.cpp#L313-L330)），它会自动为多 streamId 的图创建额外流并 `SetExecuteStreams`——这是连接「Python Builder」与「图内多流」的桥梁，读懂它就打通了 u5 与 u7。
