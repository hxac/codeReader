# GraphOpBuilder 组图实战

## 1. 本讲目标

上一讲（u5-l2）我们已经知道：图算子（Graph Operation）在**调度层**把若干个 `Operation` 拼成一张 DAG，整体对外暴露成一个新的 `Operation`。但当时我们是在「手工指定 `tensorId`」的层面理解 `GraphParam`/`Node` 的——每加一个中间张量都要自己算它排在第几号、每条边都要手写一串数字。当算子一多，这套手工编号极易出错。

本讲就来解决这个问题。学完后你应该能够：

1. 说出 `GraphOpBuilder` 相比「手动 tensorId 组图」解决了什么痛点。
2. 掌握 `GraphOpBuilder` 的四个 fluent 接口：`Init` / `Reshape` / `AddOperation` / `Build`，以及工厂函数 `CreateGraphOpBuilder`。
3. 理解「基于 tensor 名称自动组图」的实现原理：Builder 如何用名称查到 `tensorId`、如何自动分配中间张量编号。
4. 理解 `Reshape` 的名称编址——它为什么不产生新张量、而是给输入边挂一个形状变换函数。
5. 用 `GraphOpBuilder` 从零写出一段「输入 → SelfAttention → Linear」的图算子构建伪代码。

## 2. 前置知识

本讲建立在你已经掌握以下概念之上（若不熟，请先看对应讲义）：

- **Operation 与 CreateOperation 工厂**（u1-l6）：每个算子是 `Operation` 抽象类的实例，由模板工厂 `CreateOperation<OpParam>` 创建、`DestroyOperation` 销毁。本讲的 `AddOperation` 模板重载内部就是调它。
- **SVector**（u1-l4）：ATB 自研的动态数组，`SVector<std::string>` 在本讲里用来装「tensor 名称列表」。
- **图算子的 GraphParam / Node**（u5-l2，**本讲的硬前置**）：图算子用 `GraphParam` 描述，里面有 `inTensorNum`/`outTensorNum`/`internalTensorNum` 和一张 `nodes` 列表；每个 `Node` 用 `inTensorIds`/`outTensorIds`（数字 id）引用全局张量，`node.operation` 指向真正的算子。理解「共享同一个 `tensorId` 就自动连线」是本讲的基础。

一句话回顾 u5-l2 的核心：图算子里所有张量被分成**输入段、输出段、中间段**三段，全局从 0 开始编号；`Node` 之间靠相同的 `tensorId` 自动连边。本讲要回答的是：**这些 id 谁来算？** 答案就是 `GraphOpBuilder`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/atb/graph_op_builder.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/graph_op_builder.h) | `GraphOpBuilder` 抽象类的公共接口：`Init`/`Reshape`/`AddOperation`/`Build` 与模板 `AddOperation`，以及工厂 `CreateGraphOpBuilder`/`DestroyGraphOpBuilder`。 |
| [src/atb/operation/graph_op_builder_impl.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/graph_op_builder_impl.cpp) | 唯一实现 `GraphOpBuilderImpl`：用「名称 → tensorId」的多张 map 完成自动编号与组图。 |
| [include/atb/types.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/types.h) | 定义 `ReshapeFunc`、`InferShapeFunc`、`Node`、`GraphParam`、`Chunk` 等本讲涉及的数据结构。 |
| [tests/framework/c++/layer_ops/llama65b/layer/llama65b_layer_mlp_graph_builder.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/framework/c++/layer_ops/llama65b/layer/llama65b_layer_mlp_graph_builder.cpp) | 一份**真实**的 C++ 组图样例：用 `GraphOpBuilder` 拼出 Llama MLP 图算子，含 `Reshape` + 多个 `AddOperation`。 |
| [example/graph_example.py](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/graph_example.py) | Python 端等价的 `Builder` 组图样例（SelfAttention→Linear 链路），本讲综合实践对照参考。 |
| [docs/ATB_mechanisms.md](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/docs/ATB_mechanisms.md) | 官方对「轻量图构建」与 GraphOpBuilder 样例的说明。 |

---

## 4. 核心概念与源码讲解

### 4.1 从手动 tensorId 到名称编址：GraphOpBuilder 解决的问题

#### 4.1.1 概念说明

在 u5-l2 里，`GraphParam` 描述一张图，`Node` 用**数字 `tensorId`** 引用张量。如果直接手填 `GraphParam`，你会遇到三个痛点：

1. **编号易错**：每加一个中间张量，你要在心里记住「现在排到第几号了」，输入、输出、中间三段的边界容易算错。
2. **改图痛苦**：在中间插入一个算子、或换一个输出，往往要批量改一连串 `tensorId`。
3. **可读性差**：`node.inTensorIds = {0, 3, 5}` 这样的数字，读代码时根本看不出 0/3/5 分别是什么。

`GraphOpBuilder` 用一个很自然的思想化解了它们：**让你用「字符串名字」指代张量，由 Builder 帮你算数字 id**。它的类注释写得很清楚——它「通过 Operation 的输入输出关系组建出算子的拓扑图……优化了之前手动定义 tensor id 的组图方式」。

> 参见 [graph_op_builder.h:30-34](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/graph_op_builder.h#L30-L34) 的类说明。

于是你只要做两件事：给图的输入输出起名字、告诉 Builder「这个算子的输入叫什么、输出叫什么」。Builder 会自动推导出哪些是新出现的中间张量、该分到第几号。

#### 4.1.2 核心流程

Builder 的总体用法是一条 fluent（流式）调用链：

```text
CreateGraphOpBuilder(&builder)        // 1. 建一个 builder
   ↓
builder->Init(name, inferShapeFunc,   // 2. 声明图名 + 输入输出名称列表 + 整图形状推导规则
              inNames, outNames)
   ↓
builder->Reshape(srcName, func, viewName)   // 3.（可选）给某个张量挂一个形状视图
builder->AddOperation(op, inNames, outNames)//    逐个往图里加算子，用名称接线
   ...                                    //    可重复 Reshape / AddOperation
   ↓
op = builder->Build()                 // 4. 收尾，产出可执行的图算子 Operation*
DestroyGraphOpBuilder(builder)        // 5. 销毁 builder（注意：产出的 op 不受影响）
```

关键点：**第 3 步里出现的、既不是输入也不是输出的名字，会被自动登记为「中间张量」**——这正是「自动组图」的核心，4.3 节会拆解其实现。

#### 4.1.3 源码精读

`GraphOpBuilder` 在头文件里是一个**纯抽象类**（所有方法都是纯虚 `= 0`），真正的逻辑在实现类的派生类里。这样设计是为了把接口与实现分离、便于后续替换实现。

类声明与四个纯虚接口：

```cpp
// graph_op_builder.h:36-95（节选）
class GraphOpBuilder {
public:
    virtual Status Init(const std::string &opName, const InferShapeFunc &inferShapeFunc,
                        const SVector<std::string> &inTensorNames,
                        const SVector<std::string> &outTensorNames) = 0;
    virtual Status Reshape(const std::string &srcTensorName, const ReshapeFunc &reshapeFunc,
                           const std::string &viewTensorName) = 0;
    virtual Status AddOperation(Operation *operation, const SVector<std::string> &inTensorNames,
                                const SVector<std::string> &outTensorNames) = 0;
    virtual Operation *Build() = 0;
    ...
};
```

- [graph_op_builder.h:59-60](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/graph_op_builder.h#L59-L60)：`Init` 声明，接收图名、整图形状推导函数、输入名称列表、输出名称列表。
- [graph_op_builder.h:73-74](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/graph_op_builder.h#L73-L74)：`Reshape` 声明，给源张量挂一个形状变换，产生一个「视图名字」。
- [graph_op_builder.h:87-88](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/graph_op_builder.h#L87-L88)：`AddOperation`（接收已建好的 `Operation*`）。
- [graph_op_builder.h:95](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/graph_op_builder.h#L95)：`Build` 返回最终的图算子，失败返回空指针。

工厂函数负责创建/销毁 builder，用户拿不到具体实现类：

- [graph_op_builder.h:134](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/graph_op_builder.h#L134) `CreateGraphOpBuilder`、[graph_op_builder.h:143](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/graph_op_builder.h#L143) `DestroyGraphOpBuilder`。

#### 4.1.4 代码实践

**实践目标**：用眼睛走一遍 Builder 的 fluent 时序，建立「名字驱动」的直觉。

**操作步骤**：打开真实样例 [llama65b_layer_mlp_graph_builder.cpp:83-101](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/framework/c++/layer_ops/llama65b/layer/llama65b_layer_mlp_graph_builder.cpp#L83-L101)，逐行对照上面的「核心流程」五步。

**需要观察的现象**：
- 第 84 行 `CreateGraphOpBuilder(&graphOpBuilder)` 建出 builder。
- 第 86-91 行 `Init` 只声明了输入 `{"hidden_states", "weight"}` 和输出 `{"mlp_out"}`，**没提任何中间张量**。
- 第 93-98 行的 `Reshape`/`AddOperation` 里却冒出 `linear_out`、`gate_out`、`up_out`、`swish_out` 这些新名字——它们没在 `Init` 里登记。
- 第 100 行 `Build()` 产出的 `*operation`，在第 101 行 `DestroyGraphOpBuilder` 销毁 builder 后**仍然有效**。

**预期结果**：你能用自己的话解释「`Init` 里没列出的名字，最后怎么会变成合法的中间张量」。答案在 4.3 节。（本实践为源码阅读型，无需运行设备。）

#### 4.1.5 小练习与答案

**练习 1**：如果不使用 `GraphOpBuilder`，直接手填 `GraphParam`，把第 N 个中间张量的 `tensorId` 写错一位，会在什么时候被发现？
**答案**：在最坏情况下可能要到 `GraphOperation` 的 `CheckGraphParam` 校验拓扑时（u5-l2 提到的严格校验）才报错，甚至跑到 `Setup`/`Execute` 才崩。Builder 把这种「人脑算编号」的错误在组图阶段就消除了。

**练习 2**：`GraphOpBuilder` 在头文件里为什么设计成纯抽象类、再用工厂创建？
**答案**：分离接口与实现，隐藏 `GraphOpBuilderImpl` 这一具体类型，便于以后替换组图策略而不破坏用户代码（面向接口编程）。

---

### 4.2 fluent 四接口精讲：Init / Reshape / AddOperation / Build

> 本节对应最小模块「GraphOpBuilder 接口」。

#### 4.2.1 概念说明

`GraphOpBuilder` 的公开能力就四个动作加一个模板糖。理解它们的关键是分清两类重载：

- **`AddOperation(Operation*, ...)`**：你先把算子 `Operation*` 建好（自己调 `CreateOperation`），再交给 Builder 接线。
- **模板 `AddOperation(OpParam, ...)`**：一个语法糖，你只传 `Param`，它内部帮你 `CreateOperation` 建算子、再调上面那个重载接线；失败还会自动 `DestroyOperation` 清理。

`Reshape` 是这套接口里最特别的一个：它**不产生新的图节点**，而是在「边」上挂一个形状变换函数。这点很容易误解，4.4 节会专门讲。

#### 4.2.2 核心流程

四个接口的职责分工：

```text
Init      → 登记图名、整图形状推导函数、输入名列表、输出名列表；
            给输入输出分配 [0, in+out-1] 的 tensorId。
Reshape   → 把一个 (源名, 形状函数) 注册成「视图名」；视图名与源名共享同一个 tensorId。
AddOperation → 取每个名字查 tensorId 组成一个 Node（输入名若命中视图则附带 reshape 函数），
            把 Node 追加到 graphParam_.nodes；遇到没见过的名字就自动登记为中间张量。
Build     → 把 internalTensorNum 回填进 GraphParam，调 CreateOperation(graphParam) 产出图算子；
            失败时逐个 DestroyOperation 清理已加入的子算子。
```

#### 4.2.3 源码精读

**模板 `AddOperation`——最常用的入口**。它把「建算子 + 接线 + 失败清理」打包，是 fluent 写法的关键：

```cpp
// graph_op_builder.h:108-124
template <class OpParam>
Status AddOperation(const OpParam &opParam, const SVector<std::string> &inTensorNames,
                    const SVector<std::string> &outTensorNames)
{
    Operation *operation = nullptr;
    Status st = CreateOperation(opParam, &operation);   // 用 Param 建算子
    if (st != NO_ERROR) { return st; }
    st = AddOperation(operation, inTensorNames, outTensorNames);  // 再接线
    if (st != NO_ERROR) {
        if (operation != nullptr) { DestroyOperation(operation); }  // 接线失败就回收算子
    }
    return st;
}
```

- [graph_op_builder.h:108-124](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/graph_op_builder.h#L108-L124)：注意它在 `CreateOperation` 成功但 `AddOperation` 失败时，会 `DestroyOperation(operation)` 防止内存泄漏。这就是为什么用模板重载比「自己建算子再 Add」更省心。

**`Init` 实现——分配输入输出 id**。它用 `id++` 给输入输出顺序编号：

```cpp
// graph_op_builder_impl.cpp:48-73（节选）
graphParam_.name = opName;
graphParam_.inferShapeFunc = inferShapeFunc;
uint32_t id = 0;
for (const std::string &inTensorName : inTensorNames) {
    inTensorIds_[inTensorName] = id++;        // 输入: 0 .. inTensorNum-1
}
for (const std::string &outTensorName : outTensorNames) {
    outTensorIds_[outTensorName] = id++;      // 输出: inTensorNum .. in+out-1
}
graphParam_.inTensorNum  = static_cast<uint32_t>(inTensorNum);
graphParam_.outTensorNum = static_cast<uint32_t>(outTensorNum);
```

- [graph_op_builder_impl.cpp:59-69](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/graph_op_builder_impl.cpp#L59-L69)：注意 `Init` 里还做了 `> MAX_TENSOR_NUM(256)` 的校验（[L55-58](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/graph_op_builder_impl.cpp#L55-L58) 与 L62-66），超限返回 `ERROR_INVALID_IN_TENSOR_NUM`。这就把 `GraphParam` 文档里「输入/输出/中间张量数均 ≤ 256」的约束落实了。

**`Build` 实现——收尾产图算子**：

```cpp
// graph_op_builder_impl.cpp:117-132
Operation *GraphOpBuilderImpl::Build() {
    graphParam_.internalTensorNum = internalTensorNum_;   // 回填中间张量数
    Operation *graphOp = nullptr;
    Status st = CreateOperation(graphParam_, &graphOp);   // 用 GraphParam 建图算子
    if (st != NO_ERROR) {
        for (size_t i = 0; i < graphParam_.nodes.size(); i++) {   // 失败: 逐个销毁子算子
            if (graphParam_.nodes.at(i).operation != nullptr) {
                DestroyOperation(graphParam_.nodes.at(i).operation);
                graphParam_.nodes.at(i).operation = nullptr;
            }
        }
        ATB_LOG(ERROR) << "create graph op fail";
    }
    return graphOp;
}
```

- [graph_op_builder_impl.cpp:117-132](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/graph_op_builder_impl.cpp#L117-L132)：`Build` 把累积的 `graphParam_` 一次性交给 `CreateOperation(graphParam_, ...)`（因为 `GraphParam` 本身也是一种 Param，会触发 `GraphOperation` 的构造，见 u5-l2）。失败时遍历 `nodes` 把已加入的子算子逐个 `DestroyOperation`——这与模板 `AddOperation` 的清理思路一致：**资源谁持有谁负责回收**。

#### 4.2.4 代码实践

**实践目标**：对比两种 `AddOperation` 写法的差异，体会模板糖的省心之处。

**操作步骤**：

1. 阅读 [llama65b_layer_mlp_graph_builder.cpp:94](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/framework/c++/layer_ops/llama65b/layer/llama65b_layer_mlp_graph_builder.cpp#L94)：`graphOpBuilder->AddOperation(Linear(param), {"hidden_states_", "weight"}, {"linear_out"});`。这里 `Linear(param)` 是一个返回 `Operation*` 的辅助函数（见 [L17-25](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/framework/c++/layer_ops/llama65b/layer/llama65b_layer_mlp_graph_builder.cpp#L17-L25)），所以这里走的是**非模板**重载。
2. 想象把它改写成模板重载：`graphOpBuilder->AddOperation(linearParam, {"hidden_states_", "weight"}, {"linear_out"});`。

**需要观察的现象**：非模板重载要求你自己保证 `Linear(param)` 返回的指针有效；而模板重载内部已封装「建算子 + 失败销毁」。

**预期结果**：能用一句话说出「当你手里只有 Param 时用模板重载更安全；当你想复用一个已建好的 `Operation*` 时用非模板重载」。

#### 4.2.5 小练习与答案

**练习 1**：`Init` 的第二个参数 `inferShapeFunc` 和 `Node.operation` 自己的 `InferShape` 是什么关系？为什么图算子还需要一个整图级别的 `inferShapeFunc`？
**答案**：`Node.operation` 的 `InferShape` 只推导单个算子的输出形状；而整图的 `inferShapeFunc` 负责「给定图的输入 TensorDesc，推导图的输出 TensorDesc」，因为中间张量的形状由内部节点链路决定、对外不可见，调用方只需关心图级输入→输出。两者层次不同，互补。

**练习 2**：为什么 `Build()` 失败时要手动遍历 `nodes` 销毁子算子，而不是让 `GraphOperation` 析构去管？
**答案**：因为 `Build` 失败说明 `CreateOperation(graphParam_)` 没有成功构造出 `GraphOperation`，子算子的所有权没有被图算子接管，此时 Builder 仍持有这些 `Operation*`，必须由 Builder 自己回收，否则泄漏。

---

### 4.3 名称自动编址与组图实现：GetTensorId

> 本节对应最小模块「组图实现」，是本讲的核心。

#### 4.3.1 概念说明

「基于 tensor 名称自动组图」的全部魔法，集中在实现类私有的一个函数 `GetTensorId(tensorName)` 上。它的工作是：**给我一个名字，还你一个 `tensorId`；如果这个名字从没见过，就当场给它分配一个新 id 并登记为中间张量。**

这个函数决定了三件事：

1. 输入、输出张量拿到 u5-l2 说的「前三段」id；
2. 任何在 `AddOperation` 里新冒出来的名字，会被自动判为**中间张量**并分配 id；
3. `Reshape` 产生的「视图名」**不会**分配新 id，而是复用源张量的 id（4.4 节详解）。

#### 4.3.2 核心流程

`GetTensorId` 是一个「四段查找 + 兜底分配」的过程，按固定优先级查四张 map：

```text
GetTensorId(name):
  if name ∈ inTensorIds_      → return 输入段 id            （输入张量）
  elif name ∈ outTensorIds_   → return 输出段 id            （输出张量）
  elif name ∈ viewTensorIds_  → return 视图记录里的源 id     （Reshape 视图，复用源 id）
  elif name ∈ internalTensorIds_ → return 已分配的中间 id    （之前见过的中间张量）
  else                        → 分配新中间 id 并登记         （首次出现的中间张量）
```

新中间张量的 id 计算方式（来自 [graph_op_builder_impl.cpp:145](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/graph_op_builder_impl.cpp#L145)）：

\[
\text{internalTensorId} = \text{inTensorIds\_.size()} + \text{outTensorIds\_.size()} + \text{internalTensorNum\_}
\]

即中间张量从 `inTensorNum + outTensorNum` 开始递增，与 u5-l2 描述的「输入段 | 输出段 | 中间段」全局布局完全吻合。第 k 个被登记的中间张量其 id 为：

\[
\text{tensorId} = \text{inTensorNum} + \text{outTensorNum} + k,\quad k = 0,1,2,\dots
\]

#### 4.3.3 源码精读

四张 map 是实现类的成员，分别记录四类名字到 id 的映射，外加一张视图表（值是「源 id + 形状函数」的 pair）：

```cpp
// graph_op_builder_impl.cpp:40-45
GraphParam graphParam_;
uint32_t internalTensorNum_ = 0;
std::map<std::string, uint32_t> inTensorIds_;
std::map<std::string, uint32_t> outTensorIds_;
std::map<std::string, uint32_t> internalTensorIds_;
std::map<std::string, std::pair<uint32_t, ReshapeFunc>> viewTensorIds_; // key: viewTensorName
```

`GetTensorId` 的完整实现：

```cpp
// graph_op_builder_impl.cpp:134-149
uint32_t GraphOpBuilderImpl::GetTensorId(const std::string &tensorName) {
    if (inTensorIds_.find(tensorName) != inTensorIds_.end()) {
        return inTensorIds_[tensorName];                       // 输入
    } else if (outTensorIds_.find(tensorName) != outTensorIds_.end()) {
        return outTensorIds_[tensorName];                      // 输出
    } else if (viewTensorIds_.find(tensorName) != viewTensorIds_.end()) {
        return viewTensorIds_[tensorName].first;               // 视图 → 复用源 id
    } else if (internalTensorIds_.find(tensorName) != internalTensorIds_.end()) {
        return internalTensorIds_[tensorName];                 // 已登记中间张量
    } else {
        uint32_t internalTensorId = inTensorIds_.size() + outTensorIds_.size() + internalTensorNum_++;
        internalTensorIds_[tensorName] = internalTensorId;     // 首次出现 → 新中间张量
        return internalTensorId;
    }
}
```

- [graph_op_builder_impl.cpp:134-149](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/graph_op_builder_impl.cpp#L134-L149)：注意视图分支返回的是 `viewTensorIds_[name].first`（源 id），`.second`（形状函数）在 `AddOperation` 里另取。这就是「视图不占新 id」的实现根源。

`AddOperation` 如何用 `GetTensorId` 组装一个 `Node`：

```cpp
// graph_op_builder_impl.cpp:82-115（节选）
Node node;
node.operation = operation;
for (const std::string &inTensorName : inTensorNames) {
    node.inTensorIds.push_back(GetTensorId(inTensorName));          // 查 id
    if (viewTensorIds_.find(inTensorName) != viewTensorIds_.end()) {
        node.inTensorReshapeFuncs.push_back(viewTensorIds_[inTensorName].second); // 输入是视图 → 挂 reshape
    } else {
        node.inTensorReshapeFuncs.push_back(nullptr);               // 普通输入 → 挂空
    }
}
for (const std::string &outTensorName : outTensorNames) {
    node.outTensorIds.push_back(GetTensorId(outTensorName));
}
graphParam_.nodes.push_back(node);
```

- [graph_op_builder_impl.cpp:89-113](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/graph_op_builder_impl.cpp#L89-L113)：每条输入边都会在 `node.inTensorReshapeFuncs` 里对应一个槽位——是视图就放形状函数、否则放 `nullptr`。这正是 [types.h:177-178](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/types.h#L177-L178) 里 `Node::inTensorReshapeFuncs` 的由来：它与 `inTensorIds` **等长、同序**。

#### 4.3.4 代码实践

**实践目标**：手工模拟一遍 `GetTensorId`，验证「自动编号」结果。

**操作步骤**：以 [llama65b_layer_mlp_graph_builder.cpp:86-98](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/framework/c++/layer_ops/llama65b/layer/llama65b_layer_mlp_graph_builder.cpp#L86-L98) 的真实图为例，用纸笔推演每个名字最终拿到的 `tensorId`。

**需要观察的现象**：按代码顺序推演（`Init` 输入 `hidden_states`/`weight`、输出 `mlp_out`，随后 Reshape 产生视图，再 AddOperation）：

| 名字 | 类别 | tensorId |
| --- | --- | --- |
| `hidden_states` | 输入 | 0 |
| `weight` | 输入 | 1 |
| `mlp_out` | 输出 | 2 |
| `hidden_states_` | 视图（源=0） | 0（复用） |
| `linear_out` | 中间（首次） | 3 |
| `linear_out_` | 视图（源=3） | 3（复用） |
| `gate_out` | 中间（首次） | 4 |
| `up_out` | 中间（首次） | 5 |
| `swish_out` | 中间（首次） | 6 |

**预期结果**：`internalTensorNum_` 最终为 4（`linear_out`、`gate_out`、`up_out`、`swish_out`），与 `Build` 回填的 `graphParam_.internalTensorNum` 一致；视图名（`hidden_states_`、`linear_out_`）不进入中间张量计数。如果你推演出的表和上面一致，说明你已掌握自动编址。（本实践为纸笔推演型，无需运行。）

#### 4.3.5 小练习与答案

**练习 1**：如果两个不同名字的中间张量，会被分配到同一个 `tensorId` 吗？
**答案**：不会。每个首次出现的名字都会触发 `else` 分支，`internalTensorNum_` 自增一次，分配唯一递增 id。名字与 id 是一一对应的（视图除外，视图刻意复用源 id）。

**练习 2**：为什么 `GetTensorId` 用四张独立的 `std::map` 而不是一张「名字→id」总表？
**答案**：因为视图（`viewTensorIds_`）不仅要存 id（且是复用的源 id），还要存形状函数；输入/输出/中间三类语义不同、生命周期不同（输入输出在 `Init` 一次性确定，中间是逐步登记）。分类存放让「视图复用源 id」「中间按需分配」这两种特殊语义能清晰表达。

---

### 4.4 Reshape 的名称编址：不产生新张量的形状视图

#### 4.4.1 概念说明

`Reshape(srcTensorName, reshapeFunc, viewTensorName)` 是最容易误解的接口。直觉上你可能以为它「像 PyTorch 的 `view()` 一样产生一个新张量节点」。**实际上不是**——它产生的 `viewTensorName` 只是一个**逻辑别名**：

- 它与源张量共享**同一个 `tensorId`**（同一段底层存储，零拷贝）；
- 它额外携带一个 `ReshapeFunc`，在「某个算子把这个视图当作输入」时，被挂到那条输入边的 `inTensorReshapeFuncs` 上。

换句话说，`Reshape` 不是在图里加一个节点，而是在描述「下一站消费这个张量时，请先把它的形状理解成新样子」。形状变换发生在 Host 侧的形状推导/分片（Tiling）阶段，不搬运数据。

`ReshapeFunc` 的类型签名（[types.h:156](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/types.h#L156)）：

```cpp
using ReshapeFunc = std::function<void(const Dims &oldShape, Dims &newShape)>;
```

它接收旧形状、改写新形状，纯描述性、无副作用。

#### 4.4.2 核心流程

`Reshape` + `AddOperation` 的协作流程：

```text
Reshape(src, func, view):
  viewTensorIds_[view] = ( GetTensorId(src),  func )     # 登记视图：源 id + 形状函数
  # 注意：此时并没有调用 GetTensorId(view)，view 不进 internalTensorIds_

AddOperation(op, {view, ...}, {out}):
  对输入 view 调 GetTensorId(view) → 命中 viewTensorIds_ 分支 → 返回源 id
  node.inTensorReshapeFuncs 对应槽位 ← func              # 把形状函数挂到这条输入边
```

#### 4.4.3 源码精读

`Reshape` 的实现只有一行，但它定义了视图的语义：

```cpp
// graph_op_builder_impl.cpp:75-80
Status GraphOpBuilderImpl::Reshape(const std::string &srcTensorName, const ReshapeFunc &reshapeFunc,
                                   const std::string &viewTensorName) {
    viewTensorIds_[viewTensorName] = {GetTensorId(srcTensorName), reshapeFunc};
    return NO_ERROR;
}
```

- [graph_op_builder_impl.cpp:75-80](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/graph_op_builder_impl.cpp#L75-L80)：`viewTensorName` 作为 key 存进 `viewTensorIds_`，值是 `(源 id, func)`。这就是为什么 4.3 节里视图名 `hidden_states_`、`linear_out_` 不计入 `internalTensorNum_`。

真实样例里的两个 `ReshapeFunc`，展示「把 3 维折叠成 2 维」和「把 2 维展开成 3 维」这两种典型用法：

```cpp
// llama65b_layer_mlp_graph_builder.cpp:72-82
atb::ReshapeFunc reshape_01_2 = [](const atb::Dims &oldShape, atb::Dims &newShape) {
    newShape.dimNum = 2;                                   // 3 维 → 2 维
    newShape.dims[0] = oldShape.dims[0] * oldShape.dims[1]; // 合并前两维
    newShape.dims[1] = oldShape.dims[2];
};
atb::ReshapeFunc unsqueueze_0 = [](const atb::Dims &oldShape, atb::Dims &newShape) {
    newShape.dimNum = 3;                                   // 2 维 → 3 维
    newShape.dims[0] = 1;                                  // 前面插一个大小为 1 的维
    newShape.dims[1] = oldShape.dims[0];
    newShape.dims[2] = oldShape.dims[1];
};
```

- [llama65b_layer_mlp_graph_builder.cpp:72-82](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/framework/c++/layer_ops/llama65b/layer/llama65b_layer_mlp_graph_builder.cpp#L72-L82)：`reshape_01_2` 用于把 `[B, S, H]` 折叠成 `[B*S, H]` 喂给 `Linear`（矩阵乘习惯 2 维输入）；`unsqueueze_0` 把 `Linear` 的 2 维输出 `[B*S, H]` 再展开回 3 维 `[1, B*S, H]` 给后续 `Split`。配合 [L93-98](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/framework/c++/layer_ops/llama65b/layer/llama65b_layer_mlp_graph_builder.cpp#L93-L98) 的 Reshape+AddOperation 交替使用，可以看出 Reshape 是「为下一个算子的形状要求做适配」的工具。

#### 4.4.4 代码实践

**实践目标**：理解 Reshape「不搬运数据、只在边上加形状函数」的设计。

**操作步骤**：

1. 在 [llama65b_layer_mlp_graph_builder.cpp:93](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/framework/c++/layer_ops/llama65b/layer/llama65b_layer_mlp_graph_builder.cpp#L93) 处，`Reshape("hidden_states", reshape_01_2, "hidden_states_")` 之后，第 94 行 `AddOperation(Linear(param), {"hidden_states_", "weight"}, {"linear_out"})` 把 `hidden_states_` 作为输入。
2. 对照 4.3 节的 `AddOperation` 实现（[L100-104](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/graph_op_builder_impl.cpp#L100-L104)）：`hidden_states_` 命中 `viewTensorIds_`，于是这个 Linear 节点的 `inTensorReshapeFuncs[0] = reshape_01_2`，而 `inTensorIds[0] = 0`（与 `hidden_states` 同一个 id）。

**需要观察的现象**：`hidden_states_` 既没有出现在 `Init` 的输入输出列表里，也不在中间张量计数里，却能让 Linear 正确拿到一个「被 reshape 过的」输入。

**预期结果**：你能解释「Reshape 不产生新张量、不占新 id，它只是在消费算子的输入边上挂一个形状函数」。这就是 `Node::inTensorReshapeFuncs` 存在的全部意义。

#### 4.4.5 小练习与答案

**练习 1**：假如一个视图名被**两个**不同的 `AddOperation` 当作输入，会怎样？
**答案**：两个 `Node` 的对应输入边都会拿到相同的源 `tensorId`，且各自的 `inTensorReshapeFuncs` 槽位都挂上同一个 `ReshapeFunc`。视图是「按名查找」的，可被多次复用，每次接线都独立挂函数。

**练习 2**：能不能用 `Reshape` 把一个 `[4, 8]` 的张量 reshape 成 `[3, 3]`（元素总数不一致）？
**答案**：不能。`ReshapeFunc` 只描述「形状怎么看」，底层存储不变，元素总数必须一致（4×8 = 32 ≠ 3×3 = 9）。这种错误描述会在下游形状推导/校验阶段暴露，Builder 本身不拦截元素总数。

---

### 4.5 完整组图实战：解析真实的 Llama MLP 图算子

#### 4.5.1 概念说明

把前四节串起来，看一份**真实可用**的完整组图代码。这段代码出自 ATB 测试框架，把 Llama 风格的 MLP（`Linear → Split → Swish/Silu → Mul`）拼成一个图算子。它同时用到了：`InferShapeFunc`（整图形状推导）、两个 `ReshapeFunc`（形状适配）、`Init`/`Reshape`/`AddOperation`/`Build` 全套接口。

#### 4.5.2 核心流程

MLP 的计算语义（图内拓扑）：

```text
hidden_states ──Reshape→ [B*S,H]──┐
weight ───────────────────────────┤
                                  ├─→ Linear → linear_out ──Reshape→ [1,B*S,H] ──→ Split → {gate_out, up_out}
                                  │                                                    │
                                  │                                          gate_out ──→ Swish → swish_out ──┐
                                  │                                                               └─→ Mul → mlp_out
                                  │                                                          up_out ─┘
```

#### 4.5.3 源码精读

整图形状推导函数 `inferShapeFunc`：输入是 `hidden_states`（in[0]）和 `weight`（in[1]），输出形状取决于 weight 的排布（是否转置）：

```cpp
// llama65b_layer_mlp_graph_builder.cpp:55-70（节选）
atb::InferShapeFunc inferShapeFunc = [=](const atb::SVector<atb::TensorDesc> &inTensorDescs,
                            atb::SVector<atb::TensorDesc> &outTensorDescs) {
    outTensorDescs.at(0) = inTensorDescs.at(0);
    outTensorDescs.at(0).shape.dimNum = DIM3;                 // 输出固定 3 维
    outTensorDescs.at(0).shape.dims[0] = inTensorDescs.at(0).shape.dims[0];
    outTensorDescs.at(0).shape.dims[1] = inTensorDescs.at(0).shape.dims[1];
    // 最后一维 = 隐藏维度的一半（Split 把 gate/up 各取一半后 Mul，恢复成一半宽度）
    outTensorDescs.at(0).shape.dims[2] = param.transpose
        ? inTensorDescs.at(1).shape.dims[0] / 2    // weight 转置: [out, in] → 取 dims[0]/2
        : inTensorDescs.at(1).shape.dims[1] / 2;   // weight 不转置: [in, out] → 取 dims[1]/2
    return atb::NO_ERROR;
};
```

- [llama65b_layer_mlp_graph_builder.cpp:55-70](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/framework/c++/layer_ops/llama65b/layer/llama65b_layer_mlp_graph_builder.cpp#L55-L70)：注意它捕获了 `param`（`[=]`），说明整图形状推导可以依赖外部配置；`/2` 正对应图内 `Split` 把升维后的隐藏维对半切成 gate/up 两路。

随后就是 4.1.4 已展示的 [L83-101](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/framework/c++/layer_ops/llama65b/layer/llama65b_layer_mlp_graph_builder.cpp#L83-L101)：建 builder → `Init` → `Reshape`/`AddOperation` 链 → `Build` → `DestroyGraphOpBuilder`。产出的 `*operation` 就是一个普通 `Operation*`，之后按 u1-l6 / u2-l1 的两段式 `Setup`+`Execute` 使用即可，对调用方完全透明——这正是图算子的价值。

#### 4.5.4 代码实践

**实践目标**：把整段样例「拆开重组」，验证你对全套接口的理解。

**操作步骤**：

1. 完整阅读 [llama65b_layer_mlp_graph_builder.cpp:53-103](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/framework/c++/layer_ops/llama65b/layer/llama65b_layer_mlp_graph_builder.cpp#L53-L103)。
2. 把 L93-98 这 6 行组图语句，**改写**成「不用 Builder、直接手填 `GraphParam`」的等价伪代码：自己算出每个 `Node` 的 `inTensorIds`/`outTensorIds`（可借助 4.3.4 的表）、自己填 `internalTensorNum=4`、自己给两个 Linear 的输入边填 `inTensorReshapeFuncs`。

**需要观察的现象**：手填版会出现大量裸数字（`{0,1}`、`{3}`、`{4,5}`…），且一旦想在 `Split` 前再插一个算子，几乎所有 id 和 `internalTensorNum` 都要重算。

**预期结果**：你直观感受到 Builder 把「易错的数字管理」自动化后，组图代码变得多么可读、可改。这是本讲最直接的 takeaway。

#### 4.5.5 小练习与答案

**练习 1**：这份样例最终对外暴露几个输入、几个输出？调用方需要准备几个中间张量的内存？
**答案**：2 个输入（`hidden_states`、`weight`）、1 个输出（`mlp_out`）。中间张量（`linear_out`、`gate_out`、`up_out`、`swish_out`）由图内部托管，调用方**无需**为其分配内存（回顾 u5-l2 的「中间张量由图托管」）。

**练习 2**：如果想在 `Linear` 之后、`Split` 之前插入一个 `Activation`，用 Builder 改需要动几处？
**答案**：只需在 `Build` 之前加一行 `graphOpBuilder->AddOperation(Activation(...), {"linear_out_"}, {"act_out"});`，并把 `Split` 的输入名从 `linear_out_` 改成 `act_out`。id 重排、`internalTensorNum` 更新全部由 Builder 自动处理——这正是名称编址的可维护性收益。

---

## 5. 综合实践

**任务**：用 `GraphOpBuilder` 写出一段「输入 → SelfAttention → Linear」的图算子构建伪代码，把本讲的 `Init`/`Reshape`（可选）/`AddOperation`/`Build` 全套接口用一遍。

**参考依据**：

- C++ 接口与套路：本讲的 [llama65b_layer_mlp_graph_builder.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/framework/c++/layer_ops/llama65b/layer/llama65b_layer_mlp_graph_builder.cpp)。
- SelfAttention + Linear 的算子参数：`SelfAttentionParam`、`LinearParam`（见 u2-l3、u4-l1、u4-4），以及 [example/graph_example.py](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/graph_example.py) 里 Python 版的同款链路（SelfAttention→…→Linear）。

**示例代码**（伪代码，基于真实接口编写，未在本机运行；参数取值请按你的模型调整）：

```cpp
// 示例代码：用 GraphOpBuilder 构建 query→SelfAttention→Linear 的图算子
#include "atb/atb_infer.h"

atb::Operation* BuildAttnLinearGraph() {
    // 1) 整图形状推导：输出 = Linear(attn_out) 的形状
    atb::InferShapeFunc inferShapeFunc = [](const atb::SVector<atb::TensorDesc> &in,
                                            atb::SVector<atb::TensorDesc> &out) {
        // in: 0=query 1=key 2=value 3=seqLen 4=weight(Linear) 5=bias(Linear)
        // SelfAttention 输出末维 = headNum * vHeadSize；Linear 再把它映射到 weight 的 out 维
        out.at(0) = in.at(0);
        out.at(0).shape.dims.back() = in.at(4).shape.dims[0]; // transposeB 时 weight[out,in]
        return atb::NO_ERROR;
    };

    atb::GraphOpBuilder *builder = nullptr;
    atb::CreateGraphOpBuilder(&builder);

    // 2) Init：声明图名、形状推导、输入名、输出名
    builder->Init(
        "AttnLinearGraphOp",
        inferShapeFunc,
        {"query", "key", "value", "seqLen", "weight", "bias"},  // 6 个输入
        {"attn_linear_out"}                                      // 1 个输出
    );

    // 3) 加 SelfAttention 节点：输出 attn_out 是新中间张量（自动编号）
    atb::infer::SelfAttentionParam attnParam;
    attnParam.headNum     = 16;
    attnParam.kvHeadNum   = 16;
    attnParam.qkScale     = 1.0f / std::sqrt(64.0f);
    builder->AddOperation(attnParam,
        {"query", "key", "value", "seqLen"},   // 模板重载：传 Param，内部 CreateOperation
        {"attn_out"});                         // attn_out 自动成为中间张量

    // 4) 加 Linear 节点：吃 attn_out，产图输出 attn_linear_out
    atb::infer::LinearParam linearParam;
    linearParam.hasBias       = true;
    linearParam.outDataType   = ACL_DT_UNDEFINED;  // 浮点路径（见 u4-l1）
    builder->AddOperation(linearParam,
        {"attn_out", "weight", "bias"},
        {"attn_linear_out"});

    // 5) Build 产出图算子，销毁 builder（产出的 op 仍有效）
    atb::Operation *graphOp = builder->Build();
    atb::DestroyGraphOpBuilder(builder);
    return graphOp;  // 之后按 Setup+Execute 两段式使用
}
```

**自检要点**（不运行，靠阅读核对）：

1. `attn_out` 没在 `Init` 里出现，它会被 `GetTensorId` 自动分配为 id = `inTensorNum(6) + outTensorNum(1) + 0 = 7`，`internalTensorNum` 最终为 1。
2. `attn_linear_out` 在 `Init` 里登记为输出，id = 6（输出段从 `inTensorNum` 起步，只占一个槽位）。结合 4.3 节回答：它会和 `attn_out` 冲突吗？（提示：中间张量从 `inTensorNum + outTensorNum = 7` 起步。）
3. Python 端等价写法见 [graph_example.py:42-85](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/graph_example.py#L42-L85)：`Builder` → `add_input` → `add_node(SelfAttention)` → `add_node(Linear)` → `mark_output` → `build()`，对应关系一目了然。

> 关于自检要点 2 的说明（待本地验证）：在本文样例的输入输出数量下，输出 `attn_linear_out` 的 id 由 `Init` 在「输出段」分配（= `inTensorNum`，即 6），而中间张量 `attn_out` 的 id 从 `inTensorNum + outTensorNum = 7` 起步——二者落在不同的 id 段，不会冲突。建议你在自己的参数下重新推演一遍以确认。

---

## 6. 本讲小结

- `GraphOpBuilder` 把 u5-l2 里「手动管理 `tensorId`」的痛点，改造成「**用字符串名字指代张量，由 Builder 自动算 id**」的 fluent API。
- 四个核心接口：`Init`（声明图名/形状推导/输入输出名）、`Reshape`（给张量挂形状视图）、`AddOperation`（按名字加算子并自动接线）、`Build`（产出可执行图算子）。另有模板 `AddOperation` 重载封装了 `CreateOperation` + 失败清理。
- 自动编址的核心是私有函数 `GetTensorId`：按「输入 → 输出 → 视图 → 已登记中间 → 首次出现」四段查找，首次出现的名字被自动登记为中间张量，id 从 `inTensorNum + outTensorNum` 递增，与 `GraphParam` 的三段布局吻合。
- `Reshape` **不产生新张量、不占新 id**：视图名复用源张量的 id，形状函数被挂到消费算子输入边的 `Node::inTensorReshapeFuncs` 上——这就是该字段「与 `inTensorIds` 等长同序」的由来。
- `Build` 失败时会逐个 `DestroyOperation` 回收已加入的子算子，体现了「资源谁持有谁回收」的一致风格；产出的图算子是一个普通 `Operation*`，对调用方完全透明。
- 真实样例 [llama65b_layer_mlp_graph_builder.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/tests/framework/c++/layer_ops/llama65b/layer/llama65b_layer_mlp_graph_builder.cpp) 把 `InferShapeFunc` + 两个 `ReshapeFunc` + 全套接口用在一个 Llama MLP 图算子上，是最佳学习样板。

## 7. 下一步学习建议

- **u5-l4（图算子 Python 与 C++ 示例）**：本讲聚焦 C++ `GraphOpBuilder` 的接口与实现，u5-l4 会带你跑通 Python 端 `Builder`（`add_input`/`add_node`/`mark_output`/`build`）与 C++ 多流多图 demo，把组图能力在端到端例子里用起来。
- **回顾 u5-l2**：如果你对 `GraphOperation` 如何消费 Builder 产出的 `GraphParam`（拓扑校验、沿拓扑序传播形状、`GraphRunner` 的中间张量显存复用）还不够清晰，建议重读 u5-l2 的 `GraphRunner` 部分——Builder 负责「把图画出来」，`GraphOperation`/`GraphRunner` 负责「把图跑起来」。
- **延伸阅读**：想看 Builder 在真实大模型 layer 里的更多用法，可浏览 `tests/framework/c++/layer_ops/` 下其它 `*_graph_builder.cpp` 文件，观察不同模型如何用 Reshape + AddOperation 表达各异的子图。
