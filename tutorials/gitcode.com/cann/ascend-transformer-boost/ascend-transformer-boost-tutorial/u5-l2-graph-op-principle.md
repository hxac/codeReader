# 图算子原理：GraphParam、Node 与 GraphOperation

## 1. 本讲目标

本讲把视角从「单个算子」提升到「一张算子图」。学完后你应当能够：

- 说出**图算子（Graph Operation）**解决什么问题，以及它和「融合算子」「插件机制」的边界。
- 用 `Node`、`GraphParam`、`inTensorIds`、`outTensorIds` 这些结构，**手工描述一张由多个单算子组成的图**，并解释 `tensorId` 的编址规则。
- 理解 `GraphOperation` 如何把一张图「伪装」成一个普通 `Operation`，复用 `OperationBase` 的 `Setup`/`Execute` 两段式骨架。
- 跟踪 `GraphOperation → GraphRunner → 各节点 Runner` 这条调用链，理解中间张量内存复用与 workspace 复用的来源。

本讲承接 u3-l1（`OperationBase` 框架基类）与 u3-l2（Runner 执行单元体系），是 u5-l3（`GraphOpBuilder` 组图实战）和 u5-l4（图算子示例）的原理前置。

## 2. 前置知识

阅读本讲前，请确认你已经理解下列来自前序讲义的概念：

- **两段式执行**（u1-l6、u3-l1）：`Operation` 的 `Setup` 在 Host 侧做校验、形状推导、Tiling 并算出 `workspaceSize`；`Execute` 才异步下发到 Device。`OperationBase` 用模板方法把这两段冻结成骨架，子类只重写 `InferShapeImpl`、`CreateRunner` 等钩子。
- **Runner 是执行后端**（u3-l2）：`OperationBase` 从不直接 launch kernel，而是经纯虚钩子 `CreateRunner` 产出一个 `Runner`（如 `OpsRunner`、`AclnnRunner`），由 Runner 完成 kernel 下发。
- **VariantPack 与 Tensor**（u1-l4）：`VariantPack` 是「输入张量 + 输出张量」两个 `SVector<Tensor>` 组成的集装箱；`Tensor = TensorDesc + deviceData/hostData/dataSize`，描述与数据分离。
- **三大能力**（u1-l1）：融合算子（高性能单算子，含 kernel 融合）、图算子（把多个算子组合成图统一调度，**不做 kernel 融合**，但复用 workspace、统一调度缓解 Host Bound）、插件机制（自定义算子接入框架）。

> ⚠️ 一个最容易混淆的点：**图算子不等于融合算子**。融合算子的「融合」发生在 Kernel 层（多个运算合进一个 kernel）；图算子的「组合」发生在调度层（多个算子拼成一张图统一下发），图里的每个节点仍然是各自独立的 kernel。图算子的收益来自「少一次 Host 下发开销 + 中间张量/workspace 内存复用」，而非 kernel 内部融合。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/atb/types.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/types.h) | 定义 `Node`、`GraphParam`、`Chunk`、`ReshapeFunc`、`InferShapeFunc` 等图结构基础类型。 |
| [src/atb/operation/graph_operation.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/graph_operation.h) | `GraphOperation` 类声明：继承 `OperationBase`，把一张 `GraphParam` 包装成 `Operation`。 |
| [src/atb/operation/graph_operation.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/graph_operation.cpp) | `GraphOperation` 的实现：图校验、默认形状推导、`CreateRunner` 把逻辑图转成物理图。 |
| [src/atb/runner/graph_runner.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/graph_runner.h) | `GraphRunner` 类声明：图算子的执行后端，内部维护 `Graph`（实体张量 + 节点）。 |
| [src/atb/runner/graph_runner.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/graph_runner.cpp) | `GraphRunner` 的实现：逐节点 Setup/Execute、tiling 拼接、中间张量与 workspace 内存复用。 |
| [include/atb/graph_op_builder.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/graph_op_builder.h) | `GraphOpBuilder`：用「张量名称」自动推导 tensorId 的简化组图接口（本讲只做对照，详解见 u5-l3）。 |
| [example/graph_example.py](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/graph_example.py) | Python 端组图示例：`SelfAttention → ElewiseAdd → LayerNorm → Linear → Tanh → Linear`。 |

---

## 4. 核心概念与源码讲解

### 4.1 GraphParam 与 Node：用 tensorId 编址一张算子图

#### 4.1.1 概念说明

所谓「图算子」，就是把若干**已经创建好的 `Operation`**（可以是单算子，也可以是另一个图算子）按拓扑关系拼成一张有向无环图（DAG），再整体对外暴露为一个 `Operation`。对外看，它和普通算子没区别：有输入个数、输出个数、`Setup`、`Execute`；对内看，它是一次「多个算子的批量调度」。

表达这张图，ATB 没有显式定义「边（edge）」对象，而是采用一种更紧凑的方式——**给图中出现的每一个 Tensor 分配一个全局整数编号 `tensorId`，每个节点只声明自己「读哪些 tensorId、写哪些 tensorId」**。两个节点若共享同一个 tensorId，就自动连上了。这就是「用 tensorId 编址」。

一张图的全部 Tensor 分三类，三类一起占据一段连续的 `tensorId` 区间：

- **输入张量（input）**：图的对外输入，由调用方在 `VariantPack.inTensors` 里提供。
- **输出张量（output）**：图的对外输出，对应 `VariantPack.outTensors`。
- **中间张量（internal）**：图内部节点之间传递、不对外暴露的张量。这是图算子区别于「逐个单算子调用」的关键——中间结果由图内部托管，调用方无需为其分配内存。

围绕这套编址，types.h 提供了两个核心结构：`Node`（一个算子节点）和 `GraphParam`（整张图）。另外两个辅助结构 `Chunk` 与 `ReshapeFunc` 提供对单个输入的「切分」和「形状改写」能力。

#### 4.1.2 核心流程：tensorId 编址规则与组图三步

**tensorId 编址布局**（三类张量按固定顺序排布，三段区间无缝拼接）：

```
tensorId 取值范围:  [0, inTensorNum + outTensorNum + internalTensorNum)

  [0 .................................. inTensorNum)                          -> 输入张量
  [inTensorNum ......................... inTensorNum + outTensorNum)        -> 输出张量
  [inTensorNum + outTensorNum .......... inTensorNum + outTensorNum + internalTensorNum) -> 中间张量
```

这个「先输入、再输出、最后中间」的顺序不是文档约定，而是代码里硬编码的——后文 `BuildFullTensorPtrs` 与 `InferShapeImplDefault` 都依赖它。

**组图三步**：

1. 创建若干个单算子 `Operation`（用 `CreateOperation<XxxParam>` 工厂，见 u1-l6）。
2. 为每个 `Node` 填三个关键字段：`operation`（指向第 1 步创建的算子）、`inTensorIds`（该算子各输入对应的 tensorId）、`outTensorIds`（各输出对应的 tensorId）。
3. 把所有 `Node` 放进 `GraphParam.nodes`，并设置 `inTensorNum`、`outTensorNum`、`internalTensorNum` 三个数量字段。

**三条硬约束**（违反会在建图时被 `CheckGraphParam` 拒绝，详见 4.2）：

- `nodes` 数组顺序必须满足**拓扑序**：一个 tensorId 必须先被某节点「写」出来，才能被后续节点「读」。即先产出后消费。
- 每个 `Node` 的 `inTensorIds.size()` 必须等于 `node.operation->GetInputNum()`；`outTensorIds.size()` 必须等于 `GetOutputNum()`。且元素顺序要与该算子定义的输入/输出顺序一致。
- 所有 `tensorId` 取值必须落在 `[0, 三类数量之和)` 内。

**Chunk 与 ReshapeFunc 的作用**（同一份输入，喂给不同算子时的两种「视图」变换，均不拷贝真实数据）：

- `Chunk`：在 Host 侧对某个输入做 split 切分，取均分后的第 `chunkIndex` 份。常用于把一个拼接好的张量按 `chunkNum` 等分后取一段喂给算子。
- `ReshapeFunc`：在该节点使用某个输入前，临时改写它的 `shape`（视图），不改 deviceData。常用于让同一个张量以不同形状参与不同算子的形状推导与 Tiling。

#### 4.1.3 源码精读

**`Node` 结构** —— 图的一个算子节点（[include/atb/types.h:170-181](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/types.h#L170-L181)）：

```cpp
struct Node {
    Operation *operation = nullptr;              // 该节点对应的算子（单算子或图算子）
    SVector<uint32_t> inTensorIds;               // 输入 tensorId 列表，顺序对齐 operation 的输入顺序
    SVector<uint32_t> outTensorIds;              // 输出 tensorId 列表，顺序对齐 operation 的输出顺序
    SVector<ReshapeFunc> inTensorReshapeFuncs;   // 每个输入的 reshape 函数（可空）
    SVector<Chunk> inTensorChunks;               // 每个输入的 chunk 切分参数
};
```

注意 `inTensorReshapeFuncs` 与 `inTensorChunks` 都是 `SVector`，**长度与 `inTensorIds` 对齐**：第 `i` 个 reshape/chunk 作用于第 `i` 个输入。

**`GraphParam` 结构** —— 整张图（[include/atb/types.h:188-207](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/types.h#L188-L207)）：

```cpp
struct GraphParam {
    std::string name;                 // 图名，仅字母/数字/下划线，长度<=128
    uint32_t inTensorNum = 0;         // 输入张量数，<=256
    uint32_t outTensorNum = 0;        // 输出张量数，<=256
    uint32_t internalTensorNum = 0;   // 中间张量数，<=256
    std::vector<Node> nodes;          // 节点列表，长度<=1024，需满足拓扑序
    InferShapeFunc inferShapeFunc = nullptr;  // 可选：用户自定义形状推导
};
```

`nodes` 的注释明确写出两条规则：节点顺序需满足执行依赖；若三类张量数之和为 `S`，则所有 `inTensorIds`/`outTensorIds` 的取值都要落在 `[0, S-1]`。

**`Chunk` 与两个函数别名**（[include/atb/types.h:144-159](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/types.h#L144-L159)）：

```cpp
struct Chunk {
    uint32_t chunkNum = 1;     // 切分数量
    uint32_t chunkIndex = 0;   // 取均分后的第几份
};
using ReshapeFunc = std::function<void(const Dims &oldShape, Dims &newShape)>;
using InferShapeFunc =
    std::function<Status(const SVector<TensorDesc> &inTensorDescs, SVector<TensorDesc> &outTensorDescs)>;
```

`InferShapeFunc` 是整图的形状推导回调：接收图的全部输入 `TensorDesc`，产出全部输出 `TensorDesc`。如果用户没填，`GraphOperation` 会用默认的「拓扑序逐节点传播」（见 4.2）。

#### 4.1.4 代码实践：手工组一张「两个 Linear 串联」的最小图

这是本讲的主实践任务。目标：用 `Node` + `tensorId` 描述 `X → Linear1 → (中间) → Linear2 → Y` 这条链。

**实践目标**：通过亲手编址，理解 tensorId 的三段布局与「中间张量」的作用。

**前置事实**（来自 [src/ops/ops_infer/linear/linear_operation.cpp:365-382](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/ops/ops_infer/linear/linear_operation.cpp#L365-L382)）：浮点 Linear（`outDataType == ACL_DT_UNDEFINED`）在 `hasBias=true` 时为 **3 输入（x, weight, bias）1 输出**；`hasBias=false` 时为 2 输入 1 输出。下面的示例代码采用 `hasBias=true`。

**步骤 1：盘点张量并编号**

两个 Linear 串联，需要的张量有：

| 张量 | 角色 | tensorId |
| --- | --- | --- |
| `X` | 图输入（Linear1 的 x） | 0 |
| `W1` | 图输入（Linear1 的 weight） | 1 |
| `B1` | 图输入（Linear1 的 bias） | 2 |
| `W2` | 图输入（Linear2 的 weight） | 3 |
| `B2` | 图输入（Linear2 的 bias） | 4 |
| `Y` | 图输出（Linear2 的结果） | 5 |
| `M` | 中间张量（Linear1 的结果，喂给 Linear2） | 6 |

因此：`inTensorNum = 5`（id 0~4），`outTensorNum = 1`（id 5），`internalTensorNum = 1`（id 6），总数 `S = 7`。

**步骤 2：写出两个 Node（拓扑序：Linear1 在前，Linear2 在后）**

```cpp
// 示例代码：仅展示 Node/tensorId 结构，省略 Param 构造与 CreateOperation 细节
atb::Node node0;  // Linear1: X -> M
node0.operation = linearOp1;                 // CreateOperation<LinearParam> 得到，hasBias=true
node0.inTensorIds  = { 0, 1, 2 };            // X, W1, B1
node0.outTensorIds = { 6 };                  // M（中间张量）

atb::Node node1;  // Linear2: M -> Y
node1.operation = linearOp2;                 // 另一个 Linear，hasBias=true
node1.inTensorIds  = { 6, 3, 4 };            // M, W2, B2
node1.outTensorIds = { 5 };                  // Y（图输出）
```

**步骤 3：装填 GraphParam**

```cpp
// 示例代码
atb::GraphParam graph;
graph.name = "two_linear";
graph.inTensorNum = 5;
graph.outTensorNum = 1;
graph.internalTensorNum = 1;
graph.nodes = { node0, node1 };
graph.inferShapeFunc = nullptr;              // 用默认拓扑序形状推导
```

**需要观察的现象 / 预期结果**：

1. `node0` 必须排在 `node1` 前面——因为 `node1` 要读 tensorId 6（`M`），而 6 由 `node0` 产出。若调换顺序，`CheckGraphParam` 会报「tensorId 6 is not assigned value yet」。
2. 中间张量 `M`（id 6）不出现在调用方的 `VariantPack` 里——调用方只需准备 5 个输入、1 个输出，中间内存由 `GraphRunner` 内部分配（详见 4.3）。
3. `node1.outTensorIds = {5}` 直接写到图输出 `Y`，这是允许且常见的：最后一个节点的输出往往就是图输出。

> 待本地验证：在一个已编译 ATB 环境里，把上述 `GraphParam` 交给 `CreateOperation<GraphParam>` 建图，再用 5 入 1 出的 `VariantPack` 执行，确认结果与「逐个调用两个 Linear」一致。

#### 4.1.5 小练习与答案

**练习 1**：若把上例中 `internalTensorNum` 误设为 0（其余不变），建图会在哪一步失败？为什么？

> **答案**：在 `CheckGraphParam` 的 `CheckNode` 阶段失败。因为 `node0.outTensorIds = {6}`，而三类张量总数变成 `5+1+0=6`，合法 tensorId 范围是 `[0,5]`，`6` 越界，报「tensorId 6 is invalid, need less than 6」。

**练习 2**：`ReshapeFunc` 和直接调用一个 `Reshape` 算子相比，本质区别是什么？

> **答案**：`ReshapeFunc` 只在**该节点使用这个输入时**临时改写 `shape`（视图），不产生新张量、不拷贝 deviceData、不影响其他节点对该 tensorId 的使用；而独立的 Reshape 算子会产出一个新的 tensorId 张量，占据一个图张量槽位。前者是「局部视图」，后者是「图里的真实节点」。

---

### 4.2 GraphOperation：把图当算子（校验 + 形状推导 + 建 Runner）

#### 4.2.1 概念说明

`GraphOperation` 继承自 `OperationBase`（见 u3-l1），它用一个 `GraphParam` 作为内部参数，**对外就是一个普通 `Operation`**：

- `GetInputNum()` 返回 `GraphParam.inTensorNum`；
- `GetOutputNum()` 返回 `GraphParam.outTensorNum`；
- `Setup`/`Execute` 沿用 `OperationBase` 的两段式骨架，只是把工作下放给图执行后端 `GraphRunner`。

它在「图」这一层额外做了三件普通算子不做的事：

1. **建图校验**：在 `CreateOperation<GraphParam>` 入口对 `GraphParam` 做严格合法性检查，把拓扑错误、数量不匹配等挡在建图之前。
2. **整图形状推导**：实现 `InferShapeImpl` 钩子，支持两条路径——用户自定义 `inferShapeFunc`，或默认沿拓扑序逐节点传播形状。
3. **建执行图**：实现 `CreateRunner` 钩子，产出 `GraphRunner`，并把「逻辑 tensorId 图」翻译成「物理 Tensor* 指针图」。

它对外暴露的依然是 `Setup → Execute` 两段式，因此可作为一个更大的图的**节点**（`Node.operation` 可以指向 `GraphOperation`），实现图的嵌套组合。

#### 4.2.2 核心流程

**校验流程**（`CheckGraphParam`，建图入口）：

```
1. 名称合法（字母/数字/下划线，<=128）
2. in/out/internal 数量各 <=256；nodes 数量 <=1024
3. 初始化 tensorIsValued[]：所有输入张量(id<inTensorNum)标记为"已赋值"
4. 按数组顺序逐个 CheckNode：
   a. node.operation 非空，且 inTensorIds/outTensorIds 数量 == 该 operation 的输入/输出数
   b. 每个 inTensorId：不越界，且必须"已赋值"（先产出后消费）；标记为"已使用"
   c. 每个 outTensorId：不越界；若已赋值，检查是否同时是本节点输入(原地写)，否则报"重复赋值"；
      否则标记为"已赋值"
5. 收尾：所有非输入张量必须被赋值；输入/中间张量未被使用给 WARN
```

**形状推导**（`InferShapeImpl`）两条路径：

- 用户填了 `inferShapeFunc`：直接调用，并对返回的每个输出 `TensorDesc` 做非空校验（`dimNum != 0`、`dtype != UNDEFINED`、`format != UNDEFINED`）。
- 未填：走 `InferShapeImplDefault`，按拓扑序逐节点传播：

```
totalTensorDescs[inTensorNum + outTensorNum + internalTensorNum]   // 全图张量描述表
把图输入 desc 填入 totalTensorDescs[0 .. inTensorNum)
for each node（拓扑序）:
    按 inTensorIds 取出各输入 desc（途中套用 ReshapeFunc 改形状）
    调 node.operation->InferShape(opInTensorDescs, opOutTensorDescs)
    把 opOutTensorDescs 按 outTensorIds 散写回 totalTensorDescs
输出 = totalTensorDescs[inTensorNum .. inTensorNum + outTensorNum)
```

**建 Runner**（`CreateRunner`）：创建 `GraphRunner`，用 `BuildFullTensorPtrs` 建立「tensorId → `Tensor*`」映射表，再逐节点 `CreateRunnerNode`：把每个 `Node` 翻译成 `GraphRunner::Node`（持有该算子自己的 `Runner`），并用映射表把 `inTensorIds`/`outTensorIds` 落成具体的 `Tensor*` 指针接线。

#### 4.2.3 源码精读

**`GraphOperation` 类声明**（[src/atb/operation/graph_operation.h:20-52](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/graph_operation.h#L20-L52)）：可见它只重写了 `InferShapeImpl` 与 `CreateRunner` 两个钩子（外加若干 `Empty*Perms`、`GetGraphInfoImpl` 等），其余全靠 `OperationBase` 骨架。成员 `opGraph_` 即持有的 `GraphParam`。

**校验的核心：`CheckNode`**（[src/atb/operation/graph_operation.cpp:40-90](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/graph_operation.cpp#L40-L90)）实现了上面流程第 4 步的 a/b/c。关键是这段「输入必须先赋值」的逻辑（节选）：

```cpp
for (size_t i = 0; i < node.inTensorIds.size(); ++i) {
    uint32_t tensorId = node.inTensorIds.at(i);
    if (!tensorIsValued.at(tensorId)) {        // 尚未被任何前置节点产出
        ATB_LOG(ERROR) << "... is not assigned value yet, please check your graph.";
        return false;
    }
    tensorIsUsed.at(tensorId) = true;
}
```

而 `CheckGraphParam`（[src/atb/operation/graph_operation.cpp:131-170](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/graph_operation.cpp#L131-L170)）先把所有输入张量标记为「已赋值」（第 152-154 行），再驱动逐节点检查。这套「`tensorIsValued`/`tensorIsUsed` 双标记 + 拓扑序扫描」就是 ATB 判定图合法性的算法。

**建图入口 `CreateOperation<GraphParam>` 特化**（[src/atb/operation/graph_operation.cpp:198-219](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/graph_operation.cpp#L198-L219)）：先 `CheckGraphParam`，失败返回 `ERROR_INVALID_GRAPH`，通过才 `new GraphOperation(name, opGraph)`。注意 `GraphOperation` 析构（[src/atb/operation/graph_operation.cpp:236-244](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/graph_operation.cpp#L236-L244)）会逐节点 `DestroyOperation`，所以放进 `Node.operation` 的算子由图算子统一回收，调用方不要再单独销毁。

**形状推导 `InferShapeImpl`**（[src/atb/operation/graph_operation.cpp:256-283](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/graph_operation.cpp#L256-L283)）：有 `inferShapeFunc` 走用户函数并对结果做非空校验；否则调 `InferShapeImplDefault`。默认实现（[src/atb/operation/graph_operation.cpp:402-446](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/graph_operation.cpp#L402-L446)）就是上面流程描述的「全图描述表 + 拓扑序传播」，输出取自 `totalTensorDescs[inTensorNum + i]`，正好印证了 4.1 的 tensorId 三段布局。

**`CreateRunner` 与 `BuildFullTensorPtrs`**（[src/atb/operation/graph_operation.cpp:285-319](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/graph_operation.cpp#L285-L319) 与 [src/atb/operation/graph_operation.cpp:448-460](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/graph_operation.cpp#L448-L460)）：

```cpp
// BuildFullTensorPtrs：按「输入、输出、中间」顺序把 Tensor* 填进一张扁平表
for (inTensors)  fullTensorPtrs.at(offset++) = &runnerGraph.inTensors.at(i);
for (outTensors) fullTensorPtrs.at(offset++) = &runnerGraph.outTensors.at(i);
for (internalTensors) fullTensorPtrs.at(offset++) = &runnerGraph.internalTensors.at(i);
```

这张 `fullTensorPtrs[tensorId]` 就是「tensorId → `Tensor*`」映射。随后 `CreateRunnerNode`（[src/atb/operation/graph_operation.cpp:321-360](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/graph_operation.cpp#L321-L360)）为每个节点做三件事：① `opBase->CreateRunner(context)` 给该算子建它自己的 Runner；② 把 `inTensorReshapeFuncs`/`inTensorChunks` 透传给 `GraphRunner::Node`；③ 用映射表把 `inTensorIds`/`outTensorIds` 落成 `Tensor*` 指针：

```cpp
runnerNode.inTensors.at(j)  = fullTensorPtrs.at(opNode.inTensorIds.at(j));   // tensorId -> Tensor*
runnerNode.outTensors.at(k) = fullTensorPtrs.at(opNode.outTensorIds.at(k));
```

至此，「逻辑 tensorId 图」彻底翻译为「物理 `Tensor*` 指针图」，后续交给 `GraphRunner` 执行。

#### 4.2.4 代码实践：源码阅读型——预测哪些图能通过校验

**实践目标**：通过阅读 `CheckNode`/`CheckGraphParam`，在不跑代码的前提下判断图的合法性，固化对拓扑序与 tensorId 编址的理解。

**操作步骤**：阅读 [src/atb/operation/graph_operation.cpp:40-170](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/graph_operation.cpp#L40-L170)，然后判断下面三张图（假设每个算子的输入/输出数都已对齐）能否通过校验、若不能在哪一步失败：

- **图 A**：`inTensorNum=2, outTensorNum=1, internalTensorNum=0`。`node0: in={0}, out={2}`；`node1: in={2,1}, out={3}`。（注意 `out={3}` 但输出张量 id 应为 2）
- **图 B**：`inTensorNum=1, outTensorNum=1, internalTensorNum=1`（id 0 输入，id 1 输出，id 2 中间）。`node0: in={0}, out={2}`；`node1: in={2}, out={1}`。
- **图 C**：把图 B 的两个 node 顺序对调（`node0: in={2}, out={1}` 在前）。

**需要观察的现象 / 预期结果**：

- **图 A 失败**：输出张量 id 应落在 `[inTensorNum, inTensorNum+outTensorNum) = [2,3)`，即只能是 2；`node1.out={3}` 越界（总数为 3，合法范围 `[0,2]`）。报「tensorId 3 is invalid」。
- **图 B 通过**：拓扑序正确，id 2 由 node0 产出再被 node1 消费，id 1 作为图输出被 node1 写入。
- **图 C 失败**：`node0` 先读 id 2，但 id 2 此时未被任何节点产出（`tensorIsValued[2]=false`），报「tensorId 2 is not assigned value yet」。

> 待本地验证：可在测试里用这三组 `GraphParam` 调 `CreateOperation<GraphParam>`，对照返回的 `Status`/`ErrorType` 与日志。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `GraphOperation` 的析构要逐节点 `DestroyOperation`，而 `CreateRunnerNode` 里又用了一个「空删除器」的 `shared_ptr` 包裹 `op`？

> **答案**：所有权归 `GraphOperation`——它在析构时负责销毁各节点算子。`GraphRunner::Node` 里用 `shared_ptr`（空删除器，见 [graph_operation.cpp:332](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/graph_operation.cpp#L332)）只是为了在 Runner 侧持有一个引用、方便传递，但不抢所有权，避免 double-free。

**练习 2**：默认形状推导 `InferShapeImplDefault` 在什么情况下会比用户自定义 `inferShapeFunc` 更省事？又在什么情况下必须自己写？

> **答案**：当图里每个节点的 `InferShape` 已能正确反映输出形状、且最终图输出就是「沿拓扑序自然传到的张量」时，默认推导就够，无需自写。当图输出形状与节点自然传播结果不一致（例如做了某种全局 reshape、或输出取自非末位节点的某种组合），或想让形状推导跳过某些节点逻辑时，才需要自写 `inferShapeFunc`。

---

### 4.3 GraphRunner：图的统一调度与中间张量内存复用

#### 4.3.1 概念说明

`GraphRunner` 是 `GraphOperation::CreateRunner` 产出的执行后端（承接 u3-l2 的 Runner 体系）。它继承 `Runner`，内部维护一张「物理图」`GraphRunner::Graph`——包含**实体 `Tensor`**（输入/输出/中间各一组）与**节点列表**，每个节点持有一个指向子算子的 `Runner`。

它的职责是把图级的 `Setup`/`Execute` **展开成对每个子节点的 `Setup`/`Execute`**，串行下发到同一条 stream。在此过程中带来两个图算子独有的收益：

1. **中间张量内存复用**：中间张量由 `GraphRunner` 统一分配，并基于「张量活跃性」让生命周期不重叠的中间张量复用同一块显存，而不是为每个中间结果各开一块。
2. **workspace 复用**：整图的 workspace 不是各节点 workspace 之和，而是按 stream 取各节点的**最大值**——因为节点串行执行，同一时刻只有一个节点在用 workspace。

这两个收益正是 u1-l1 所说的「图算子复用 workspace、缓解 Host Bound」的具体落地。

#### 4.3.2 核心流程

**SetupImpl 流程**（[src/atb/runner/graph_runner.cpp:304-347](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/graph_runner.cpp#L304-L347)）：

```
1. 取全局 MemAllocationSolver（中间张量内存分配/复用求解器）
2. 为每个 node 预留 SVector 容量
3. InitTensorFromRunnerVariantPack：把用户 in/out 张量拷进物理图，
   记录每个 inTensor 是否可释放、每个 outTensor 是否需内部 malloc，
   首次进入时调 runnerGraph_.Init() 做活跃性分析
4. Reset / FreeUselessInTensor
5. SetupNodes：按顺序逐节点
   - PreparseNodeVariantPack：为该节点准备 RunnerVariantPack（含中间张量分配）
   - SetupNodeRunners：调 node.runner->Setup(...)，得到该节点 tiling/workspace
   - 累加各节点 tiling 大小
6. CalcTilingBufferSize / CalcIntermediateBufferSize：汇总全图 tiling 与中间内存
```

**ExecuteImpl 流程**（[src/atb/runner/graph_runner.cpp:390-400](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/graph_runner.cpp#L390-L400) → `ExecuteAllRunner` [src/atb/runner/graph_runner.cpp:946-987](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/graph_runner.cpp#L946-L987)）：

```
for each node（数组顺序 = 拓扑序）:
    把 context 注入 node.runnerVariantPack
    node.runner->Execute(node.runnerVariantPack)   // 下发该节点 kernel 到 stream
```

即「按拓扑序逐节点执行」。图级 workspace 复用体现在 `GetWorkspaceBufferSize`：

```
单算子逐个调用:   W_total = Σ W_i          （每个算子各占一块 workspace）
图算子（同 stream）: W_total = max_i W_i     （节点串行，取最大值复用同一块）
```

#### 4.3.3 源码精读

**`GraphRunner::Graph` 与 `GraphRunner::Node`**（[src/atb/runner/graph_runner.h:29-63](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/graph_runner.h#L29-L63)）：`Graph` 持有三组实体 `Tensor`（`inTensors`/`outTensors`/`internalTensors`）与节点列表，外加一组用于内存复用的映射表：

```cpp
struct Graph {
    SVector<Tensor> inTensors, outTensors, internalTensors;
    std::vector<Node> nodes;
    std::map<Tensor*, uint64_t> tensorMaxNodeIdMap;        // 张量 -> 最后使用它的节点 id（活跃性）
    std::map<uint64_t, std::set<Tensor*>> maxNodeIdTensorMap; // 反向：节点 id -> 在此结束的张量集
    std::map<Tensor*, bool> isInTensorCanFree;             // 输入张量是否可在某节点后释放
    std::map<Tensor*, bool> isOutTensorNeedMalloc;         // 输出张量是否需内部 malloc
    ...
};
```

`Node`（[graph_runner.h:29-41](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/graph_runner.h#L29-L41)）持有子算子的 `op`、其 `runner`、`inTensors`/`outTensors`（`Tensor*` 指针）、`inTensorReshapeFuncs`、该节点专属的 `runnerVariantPack`，以及标记每个 in/out 张量是否为中间张量的 `inTensorTypes`/`outTensorTypes`。

**逐节点 Setup：`SetupNodes`**（[src/atb/runner/graph_runner.cpp:275-302](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/graph_runner.cpp#L275-L302)）按顺序对每个节点执行 `PreparseNodeVariantPack` + `SetupNodeRunners`，并把节点 tiling 大小累加进一块连续的 host tiling buffer（`nodeHostTilingBuffer += nodeTilingSize`）。整图 tiling 拼接由 `FillHostTilingBufferImpl`（[src/atb/runner/graph_runner.cpp:354-370](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/graph_runner.cpp#L354-L370)）完成：每个节点的 tiling 段顺序写入，Device 侧再按相同偏移切分读取。

**workspace 取最大值复用：`GetWorkspaceBufferSize`**（[src/atb/runner/graph_runner.cpp:372-383](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/graph_runner.cpp#L372-L383)）：

```cpp
for (each node) {
    const auto &runnerWorkspaceBufferSize = node.runner->GetWorkspaceBufferSize();
    for (each stream i)
        multiStreamWorkspaceSizes_.at(i) = std::max(multiStreamWorkspaceSizes_.at(i),
                                                    runnerWorkspaceBufferSize.at(i));
}
```

每个 stream 的图级 workspace 取所有节点的最大值——这就是「复用而非累加」。

**逐节点 Execute：`ExecuteAllRunner`**（[src/atb/runner/graph_runner.cpp:946-987](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/graph_runner.cpp#L946-L987)）核心就是循环里那句 `node.runner->Execute(node.runnerVariantPack)`，按拓扑序把每个节点的 kernel 下发到 stream；中间还做 mstx 张量地址注册（profiling 用），对非 GraphRunner 类型的子 runner 才注册。

#### 4.3.4 代码实践：跟踪中间张量的内存复用

**实践目标**：理解 `tensorMaxNodeIdMap` 如何支撑中间张量复用，从而解释「为什么图算子比逐个调用省显存」。

**操作步骤**：

1. 阅读 `Graph::Init` 调用的 `InitTensorMaxNodeMap`（[src/atb/runner/graph_runner.cpp:58-64](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/graph_runner.cpp#L58-L64)）以及 `SearchTensorInNodeInTensor`/`SearchTensorInNodeOutTensor`（[src/atb/runner/graph_runner.cpp:86-112](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/graph_runner.cpp#L86-L112)）。
2. 回到 4.1 的「两个 Linear 串联」例子：中间张量 `M`（id 6）由 `node0` 产出、被 `node1` 消费。
3. 推断 `tensorMaxNodeIdMap[M]` 的值，并据此说明 `M` 何时可被释放、能否与其它中间张量复用同一块显存。

**需要观察的现象 / 预期结果**：

- `SearchTensorInNodeInTensor` 会把 `M` 的 `maxNodeId` 更新为**最后一个读取它的节点 id**，即 `node1`（id=1）。因此 `tensorMaxNodeIdMap[M] = 1`。
- 含义：`M` 在 `node1` 执行完之后就不再被任何节点使用，其显存可在 `node1` 之后释放/复用。若图里还有另一个中间张量 `N`，其 `tensorMaxNodeIdMap[N] = 0`（仅在 node0 阶段存活），则 `M` 与 `N` 生命周期不重叠，`MemAllocationSolver` 可让它们复用同一块显存。
- 对比「逐个调用两个 Linear」：调用方必须为中间结果 `M` 单独分配一块显存并自行管理；而图算子由 `GraphRunner` 统一托管并复用，调用方完全无感。

> 待本地验证：在 ATB_LOG=INFO 下运行一张含多个中间张量的图，观察日志中 `malloc size` / `real size`（见 [graph_runner.cpp:340-341](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/graph_runner.cpp#L340-L341)），对比「逐个调用」时的中间显存占用。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `GetWorkspaceBufferSize` 对每个 stream 取的是 `max` 而不是 `sum`？

> **答案**：图内节点在同一 stream 上**串行**执行（见 `ExecuteAllRunner` 的 for 循环），任一时刻只有一个节点在用 workspace，前一个用完即可让后一个复用同一块，故取最大值即可覆盖所有节点；若取 sum 会造成无谓的显存浪费。注意这依赖「同 stream 串行」这一前提。

**练习 2**：`GraphRunner::Node` 里的 `runnerVariantPack` 与图级 `RunnerVariantPack` 是什么关系？

> **答案**：图级 `RunnerVariantPack` 承载整图的输入/输出与一块拼接好的 host tiling；`SetupNodes` 的 `PreparseNodeVariantPack` 会从图级 pack 里**切出**该节点需要的一段 tiling、组装好该节点的输入/输出 `Tensor*`，填进节点专属的 `node.runnerVariantPack`，再交给 `node.runner->Setup/Execute`。即「图级 pack 被逐节点切片复用」，节点 runner 拿到的是为自己定制的局部 pack。

---

## 5. 综合实践

把本讲三个模块串起来：用本讲学到的 `GraphParam`/`Node` 手工组图思路，**读懂** [example/graph_example.py](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/example/graph_example.py) 这张真实图，并把它「翻译」成等价的 C++ `GraphParam` 结构。

该图的数据流为：

```
query,key,value,seqLen -> SelfAttention(PA_ENCODER) -> out0
out0, input_0 -> ElewiseAdd -> out1
out1, gamma, beta -> LayerNorm -> out2
out2, weight_0, bias_0 -> Linear -> out3
out3 -> ElewiseTanh -> out4
out4, weight_1, bias_1 -> Linear -> final_out  (mark_output)
```

请你完成：

1. **数张量**：这张图有几个输入、几个输出、几个中间张量？分别给出它们的 tensorId（按「输入、输出、中间」三段布局）。提示：Python 端的 `add_input`/`mark_output` 分别对应图的输入与输出，`get_output(0)` 产生的、既非 `add_input` 又非 `mark_output` 的就是中间张量。
2. **写 Node**：为上述 6 个算子节点各写一个 C++ `Node`，填好 `operation`、`inTensorIds`、`outTensorIds`。注意 `SelfAttention`/`LayerNorm`/`Linear`/`Elewise` 的输入个数要与各自 `GetInputNum()` 对齐（参考 graph_example.py 里 `add_node` 传入的输入列表长度）。
3. **自检**：用 `CheckNode`/`CheckGraphParam` 的规则，验证你写的 `nodes` 顺序满足拓扑序、每个 tensorId 先产出后消费。
4. **对比**：说明这张图若不用图算子、而用「逐个 `forward` 单算子」的方式实现，调用方需要手动管理几个中间张量、几次 `aclrtSynchronizeStream`。

> 待本地验证：把你写的 `GraphParam` 交给 `CreateOperation<GraphParam>`，确认能成功建图（返回 `NO_ERROR`），并与 Python 端 `Graph.forward` 的结果对照。

通过这个练习，你会真切体会到：图算子把「N 个算子的拓扑、中间内存、workspace、同步」全部收口到一个 `Operation` 里，调用方只面对「输入 → forward → 输出」，这正是它相对逐个单算子调用的核心价值。

## 6. 本讲小结

- **图算子 = 调度层的组合**：把多个已存在的 `Operation` 按 DAG 拼成一张图，整体对外是一个 `Operation`；它的「组合」发生在调度层，不做 kernel 融合。
- **用 tensorId 编址**：图中所有张量按「输入 → 输出 → 中间」三段连续编号；`Node` 用 `inTensorIds`/`outTensorIds` 引用 tensorId，共享同一 id 即自动连线，无需显式边对象。
- **三类张量是关键**：输入/输出对外暴露，**中间张量由图内部托管**，调用方无需为其分配内存——这是图算子区别于逐个调用的核心。
- **`GraphOperation` 把图当算子**：继承 `OperationBase`，重写 `InferShapeImpl`（默认沿拓扑序传播形状）与 `CreateRunner`（产出 `GraphRunner`），并用 `CheckGraphParam` 在建图时严格校验拓扑合法性。
- **`GraphRunner` 统一调度**：把图级 Setup/Execute 展开成逐节点 Setup/Execute 串行下发，带来两个收益——中间张量按活跃性复用显存、workspace 按 stream 取最大值复用。
- **逻辑图 → 物理图**：`BuildFullTensorPtrs` 建立 tensorId→`Tensor*` 映射，`CreateRunnerNode` 据此把逻辑 tensorId 图翻译成物理指针图，是连接 `GraphOperation` 与 `GraphRunner` 的桥梁。

## 7. 下一步学习建议

- **u5-l3 GraphOpBuilder 组图实战**：本讲要求手工维护 tensorId，节点一多就易错。下一篇讲 `GraphOpBuilder`（[include/atb/graph_op_builder.h](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/include/atb/graph_op_builder.h)）如何用「张量名称」自动推导 tensorId，把组图从「数 id」简化为「连名字」。
- **u5-l4 图算子 Python 与 C++ 示例**：结合本讲的 `graph_example.py` 与 `multiStream` 多图 demo，跑通端到端图算子，并理解多流多图场景。
- **回看 u3-l2/u3-l1**：若对 `RunnerVariantPack`、`OperationBase` 骨架的细节仍有模糊，建议重温，因为本讲的 `GraphRunner` 完全建立在其上。
- **延伸阅读**：通读 [src/atb/operation/graph_operation.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/operation/graph_operation.cpp) 与 [src/atb/runner/graph_runner.cpp](https://github.com/gitcode.com/cann/ascend-transformer-boost/blob/578daaec1e5cf238ecfa8607a24ccbb606b46b6a/src/atb/runner/graph_runner.cpp)，重点关注 `MemAllocationSolver` 的接口与 `Graph::Init` 的活跃性分析，理解中间张量内存复用的完整实现。
