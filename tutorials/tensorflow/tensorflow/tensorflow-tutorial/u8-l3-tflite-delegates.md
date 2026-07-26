# TFLite 委托机制 delegates

> 本讲是「边缘部署 TFLite」单元的第三讲。前置讲义 u8-l1 讲清了 Interpreter 的四件套执行模型（FlatBufferModel→InterpreterBuilder→AllocateTensors→Invoke），u8-l2 讲清了 `.tflite` 的 FlatBuffer 格式与 OpResolver 如何把算子码解析成 `TfLiteRegistration`。本讲回答一个进阶问题：**既然 CPU kernel 已经能让模型跑起来，TFLite 还用什么机制去榨取 GPU/NNAPI/专用 NPU 的算力？** 答案就是 **delegate（委托）**。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 **delegate 是什么**：它不是一个新的 Interpreter，而是一种「在执行前把图里一部分节点整体替换成一个『宏节点』，并把这部分计算交给后端」的契约。
- 画出 **图分区（partition）** 的完整流程：delegate 的 `Prepare` 回调如何声明「我支持哪些节点」，运行时如何用 `PartitionGraph` 把这些节点切成若干 `NodeSubset`，再用一个 delegate 宏节点替换每个子集。
- 对照 **GPU delegate、NNAPI delegate、XNNPACK delegate** 三个真实实现，理解它们各自的适用场景与差异。
- 解释 **三级回退（fallback）策略**：加载期回退、运行期 `InvokeWithCPUFallback`、`UndoAllDelegates`/`RemoveAllDelegates` 各自在什么情况下触发。

## 2. 前置知识

本讲默认你已经掌握 u8-l1、u8-l2 的内容。下面补充两个本讲会用到的关键概念。

### 2.1 执行计划 execution_plan 与「宏节点」

回顾 u8-l1：Interpreter 实际把状态存在它持有的 `Subgraph` 里，`Invoke()` 时只是按 `execution_plan_`（一个 `std::vector<int>` 节点下标序列）顺序遍历每个节点，取出它的 `TfLiteRegistration` 调用 `invoke` 函数指针。

delegate 的核心技巧是：**允许某个 `TfLiteRegistration` 的 `invoke` 不做单个算子的计算，而是「代为执行一整段被替换掉的子图」**。这种节点被称为 delegate 宏节点（macro node），它的 `builtin_code` 是特殊的 `kTfLiteBuiltinDelegate`。对 `Invoke()` 来说，它和普通节点没区别——遍历到它、调它的 `invoke` 就行；真正的差异藏在 `invoke` 内部。

### 2.2 TfLiteRegistration 的 init/prepare/invoke 三件套（回顾）

u8-l2 已介绍 `TfLiteRegistration` 含 `init / prepare / invoke / free` 等函数指针。delegate 宏节点也是一个 `TfLiteRegistration`，同样遵守这套契约：`init` 时拿到子图描述并编译后端模型，`prepare` 时处理形状/内存，`invoke` 时真正跑一次推理。所以「一个 delegate kernel」在结构上和「一个普通算子 kernel」是同构的，这正是 TFLite 能用同一套 `Invoke` 机制兼容二者的原因。

> 术语提示：本讲的 **delegate** 指委托对象（`TfLiteDelegate`）；**delegate kernel** 指替换子图后产生的那个宏节点（一个 `TfLiteRegistration`）；**后端（backend）** 指 GPU/NNAPI/XNNPACK 等真实算力提供者。三者别混淆。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`tensorflow/lite/core/c/common.h`](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/c/common.h) | 定义 `TfLiteDelegate` 结构、`TfLiteDelegateFlags`、`TfLiteDelegateParams` 与 context 上的 `ReplaceNodeSubsetsWithDelegateKernels` 函数指针——delegate 的「C 语言契约」。 |
| [`tensorflow/lite/core/subgraph.cc`](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/subgraph.cc) | `ModifyGraphWithDelegateImpl`（委托入口）、`ReplaceNodeSubsetsWithDelegateKernels`（建宏节点）、`InvokeImpl`（运行时遍历）、`UndoAllDelegates`/`RemoveAllDelegates`（回退）。 |
| [`tensorflow/lite/graph_info.h`](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/graph_info.h) | `NodeSubset` 结构与 `PartitionGraphIntoIndependentNodeSubsets` 分区算法的声明。 |
| [`tensorflow/lite/delegates/interpreter_utils.h`](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/interpreter_utils.h) / [`.cc`](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/interpreter_utils.cc) | `InterpreterUtils::InvokeWithCPUFallback`——运行期回退的兜底实现。 |
| [`tensorflow/lite/delegates/utils/simple_delegate.h`](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/utils/simple_delegate.h) / [`simple_delegate.cc`](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/utils/simple_delegate.cc) | `SimpleDelegateInterface`——写一个新 delegate 的「模板基类」，浓缩了 Prepare 的标准骨架。 |
| [`tensorflow/lite/delegates/utils.h`](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/utils.h) | `GraphPartitionHelper`——通用的「逐节点判定支持性 + 取最大分区」工具类。 |
| [`tensorflow/lite/delegates/gpu/delegate.h`](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/gpu/delegate.h) / [`delegate.cc`](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/gpu/delegate.cc) / [`common/model_builder.cc`](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/gpu/common/model_builder.cc) | GPU delegate 的对外 C 接口与 `DelegatePrepare`/`GetOpsToReplace`。 |
| [`tensorflow/lite/delegates/nnapi/nnapi_delegate.h`](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/nnapi/nnapi_delegate.h) / [`nnapi_delegate.cc`](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/nnapi/nnapi_delegate.cc) | Android NNAPI delegate：把子图交给系统驱动模型。 |
| [`tensorflow/lite/delegates/xnnpack/xnnpack_delegate.cc`](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/xnnpack/xnnpack_delegate.cc) | XNNPACK delegate：高度优化的 CPU 后端，常作为默认 delegate。 |

## 4. 核心概念与源码讲解

### 4.1 delegate 的抽象与契约（lite.delegates）

#### 4.1.1 概念说明

「delegate」直译是「代表/委托」。在 TFLite 里，它的含义非常具体：**一个对象，它代表某个后端（GPU/NNAPI/...）向运行时声明「图里这些节点我能算」，并接管这些节点的实际计算。**

为什么需要它？因为 TFLite 的设计哲学是「**一个 CPU 解释器 + 可插拔加速后端**」。如果每加一个加速器都要 fork 一份 Interpreter，维护成本不可控。delegate 把「后端如何识别自己支持的算子」「后端如何编译并执行这些算子」这两件事抽象成一个统一的 C 结构体契约，运行时只需认这个契约，就能透明地把部分图「外包」给任意后端。

打个比方：Interpreter 像一家总承包商，CPU kernel 是它自己的施工队；delegate 像外包分包商。总承包商在动工前（`AllocateTensors`/`ModifyGraphWithDelegate` 之前）和分包商签合同——「二楼到五楼的混凝土工程归你」，然后把这部分从总进度表里替换成「分包商施工」这一条目。真正施工（`Invoke`）时，总进度表照常推进，只是走到那条目时打电话叫分包商来干。

#### 4.1.2 核心流程

delegate 契约的核心是一个 C 函数指针 `Prepare`，它的工作流程是：

```text
运行时调用 delegate->Prepare(context, delegate)
        │
        ├─ 1. 遍历 execution_plan，逐节点判定「我是否支持」
        │     → 收集出 supported_nodes（一个节点下标列表）
        │
        ├─ 2. 调用 context->ReplaceNodeSubsetsWithDelegateKernels(
        │        context, delegate_kernel_registration, supported_nodes, delegate)
        │
        └─ 3. 运行时把 supported_nodes 分区、替换成宏节点（见 4.2）
```

关键点：**`Prepare` 自己不替换节点，它只把「想替换哪些节点」通过 `ReplaceNodeSubsetsWithDelegateKernels` 告诉运行时，真正改图的是运行时**。这是一种「声明意图 + 运行时执行」的分工，好处是 delegate 作者不必关心图的内部数据结构怎么改。

#### 4.1.3 源码精读

**`TfLiteDelegate` 结构体**就是契约本身，定义在 `common.h`：

[TfLiteDelegate 结构体定义:common.h#L1408-L1459](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/c/common.h#L1408-L1459) —— 注意它几乎全是函数指针：`Prepare` 是必填的委托入口；`CopyFromBufferHandle`/`CopyToBufferHandle`/`FreeBufferHandle` 三件套用于支持后端用「自己的硬件 buffer 句柄」存张量数据（如 GPU 纹理），需要时才填；`flags` 是行为位掩码；`data_` 是 delegate 自己的私有数据口袋。

`flags` 的取值定义在同一文件的 `TfLiteDelegateFlags` 枚举里：

[TfLiteDelegateFlags 枚举:common.h#L1358-L1405](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/c/common.h#L1358-L1405) —— 最重要的两个：`kTfLiteDelegateFlagsAllowDynamicTensors = 1`（声明本 delegate 能处理运行时才知形状的动态张量，否则图会被冻结成不可变）；`kTfLiteDelegateFlagsHintFullyDelegatedToSingleDelegate = 8`（调用方保证整图都被这一个 delegate 接管，可跳过部分内存分配）。这两个 flag 直接决定 4.2 节里 `ModifyGraphWithDelegateImpl` 走哪条分支。

**`TfLiteDelegateParams`** 是运行时回传给 delegate kernel 的「子图描述」，每个宏节点 init 时会拿到一份：

[TfLiteDelegateParams 结构:common.h#L835-L840](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/c/common.h#L835-L840) —— 含 `delegate`（指向自己）、`nodes_to_replace`（这个分区里包含哪些原始节点）、`input_tensors`/`output_tensors`（这个分区与外界的数据边界）。这正是分包商拿到的「合同附件」：你负责这些节点，输入从这几个张量来，输出写到那几个张量去。

而 context 上挂的 `ReplaceNodeSubsetsWithDelegateKernels` 函数指针，就是「声明意图」的入口：

[context->ReplaceNodeSubsetsWithDelegateKernels 声明:common.h#L948-L952](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/c/common.h#L948-L952) —— 参数是：一个 `TfLiteRegistration`（这就是 delegate kernel 的注册信息，运行时会用它造宏节点）、`nodes_to_replace`（要替换的节点列表）、`delegate`（自己）。同一个 context 上还有 `PreviewDelegatePartitioning`（[common.h#L1023-L1045](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/c/common.h#L1023-L1045)），允许 delegate 在真正替换前先「预演」分区结果，方便它决定要不要接单。

#### 4.1.4 代码实践

**实践目标**：在源码里数清「一个 delegate 至少要填 `TfLiteDelegate` 的哪些字段才能工作」。

**操作步骤**：

1. 打开 [`tensorflow/lite/delegates/utils/simple_delegate.cc`](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/utils/simple_delegate.cc)，看 `TfLiteDelegateFactory::CreateSimpleDelegate`（[第 119-153 行](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/utils/simple_delegate.cc#L119-L153)）如何用 `new TfLiteDelegate{}` 构造一个空 delegate，然后只设了 `Prepare`、`flags`、`data_` 以及三个 buffer 句柄回调。
2. 对照 4.1.3 的结构体定义，标记哪些字段被赋值、哪些留空。

**需要观察的现象**：除了 `Prepare`，其余函数指针都允许为 `null`；`data_` 被塞进了 `SimpleDelegateInterface*`（delegate 作者的业务对象），这是「C 结构体 + C++ 对象」的常见粘合手法。

**预期结果**：你会得出结论——**一个最小可用的 delegate 只需提供 `Prepare` 和 `data_`**，其余都是可选能力。这正是为什么写一个新 delegate 门槛不高。

#### 4.1.5 小练习与答案

**练习 1**：如果一个 delegate 不设置 `kTfLiteDelegateFlagsAllowDynamicTensors`，模型里却存在动态形状张量，会发生什么？

**参考答案**：在 `ModifyGraphWithDelegateImpl` 里（见 4.2.3），运行时会先 `PrepareOpsStartingAt` 探测，一旦 `has_dynamic_tensors_` 为真，就返回 `kTfLiteApplicationError`，整个委托失败。即「不支持动态张量的 delegate 遇到动态图」会直接被拒绝。

**练习 2**：`TfLiteDelegateParams` 里为什么没有「边的连接关系」字段，只有 `nodes_to_replace` / `input_tensors` / `output_tensors`？

**参考答案**：因为分区后，**分区内部的连接对运行时不可见**——整个分区被替换成一个宏节点，内部如何连线是 delegate kernel 自己的事。运行时只关心这个宏节点对外暴露哪些输入、哪些输出张量（边界），所以 `TfLiteDelegateParams` 只描述边界。

---

### 4.2 图分区与子图卸载：ModifyGraphWithDelegate（lite.delegates 核心）

#### 4.2.1 概念说明

4.1 讲了「契约」，本节讲「运行时如何履约」。一个 delegate 通常只支持模型里的**一部分**算子（比如 GPU 支持 Conv2D、DepthwiseConv2D，但不支持某个冷门 custom op）。那么被支持的节点之间，如果夹杂着不支持的节点，运行时怎么处理？

答案是 **图分区（graph partition）**：运行时把 `execution_plan` 切成若干个 `NodeSubset`（节点子集），每个子集要么是「连续的支持节点」（交给 delegate），要么是「不支持节点」（留在 CPU）。最终 `execution_plan` 变成交替排列的 `[CPU 段] → [delegate 宏节点] → [CPU 段] → [delegate 宏节点] → ...`。

> 注意：分区不保证「支持节点一定被连续合并」。如果支持节点被一个不支持节点隔开，它们会被切成**两个独立的 delegate 宏节点**——也就是说，一次委托可能产生多个宏节点，每个宏节点对应 `TfLiteDelegateParams` 里一份独立的分区。

#### 4.2.2 核心流程

完整的「启用一个 delegate」从用户调用 `interpreter.ModifyGraphWithDelegate(delegate)` 开始：

```text
ModifyGraphWithDelegate(delegate)
  │
  └─ ModifyGraphWithDelegateImpl(delegate)               [subgraph.cc:2485]
       │
       ├─ STEP 1 准备：RedoAllDelegates；按 flags 处理动态张量
       │              首次委托时保存 pre_delegation_execution_plan_（原始计划）
       │
       ├─ STEP 2 委托：SwitchToDelegateContext()
       │              → TfLiteDelegatePrepareInternal()  即调 delegate->Prepare
       │                  │
       │                  └─ delegate 的 Prepare 内部：
       │                       逐节点判定支持性 → 收集 supported_nodes
       │                       → context->ReplaceNodeSubsetsWithDelegateKernels(...)
       │                           │
       │                           └─ Subgraph::ReplaceNodeSubsetsWithDelegateKernels
       │                                 ├─ PartitionGraph(nodes_to_replace)   [分区]
       │                                 │    → NodeSubset 列表（kTfPartition/kTfNonPartition）
       │                                 └─ 清空 execution_plan_，按 NodeSubset 重建：
       │                                      kTfNonPartition  → 原节点逐个加回
       │                                      kTfPartition     → AddNodeWithParameters
       │                                                         建一个宏节点
       │
       └─ STEP 3 收尾：按 flags 决定 state_（kStateInvokableAndImmutable 等）
                       EnsureMemoryAllocations()；delegates_applied_.push_back(delegate)
                       若任何一步失败 → reset_delegation_if_not_ok → RemoveAllDelegates
```

分区算法本身（`PartitionGraphIntoIndependentNodeSubsets`）的逻辑：**按拓扑序扫描 `nodes_to_replace`，把「连续且都被支持」的节点贪心合并成一个 `kTfPartition` 子集，遇到不支持的节点就归入 `kTfNonPartition` 子集**。它保证：子集内部、子集之间都保持依赖顺序（即按子集出现的顺序执行就是合法拓扑序）。

#### 4.2.3 源码精读

**委托总入口** `Subgraph::ModifyGraphWithDelegateImpl`：

[ModifyGraphWithDelegateImpl:subgraph.cc#L2485-L2608](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/subgraph.cc#L2485-L2608) —— 这段是理解整个机制的关键，重点看三处：

- [STEP 1 准备与动态张量检查:subgraph.cc#L2507-L2548](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/subgraph.cc#L2507-L2548)：`delegates_applied_.empty()` 时把当前 `execution_plan_` 备份到 `pre_delegation_execution_plan_`——这就是回退时恢复原图的「底片」。
- [STEP 2 调 delegate->Prepare:subgraph.cc#L2553-L2558](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/subgraph.cc#L2553-L2558)：`SwitchToDelegateContext()` 切换 context 让 delegate 能用扩展接口，`TfLiteDelegatePrepareInternal` 真正触发 `delegate->Prepare(context, delegate)`，之后立刻 `SwitchToKernelContext()` 切回。
- [失败即回退的 lambda:subgraph.cc#L2496-L2505](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/subgraph.cc#L2496-L2505)：`reset_delegation_if_not_ok` 在 status 非 OK 时调 `RemoveAllDelegates()` 还原原图。这是**加载期回退**的核心。

**真正改图的地方** `Subgraph::ReplaceNodeSubsetsWithDelegateKernels`：

[ReplaceNodeSubsetsWithDelegateKernels 主体:subgraph.cc#L521-L634](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/subgraph.cc#L521-L634) —— 重点三段：

- [先分区:subgraph.cc#L568-L573](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/subgraph.cc#L568-L573)：调 `PartitionGraph(nodes_to_replace, &node_subsets)` 得到 `NodeSubset` 列表。
- [清空并重建 execution_plan:subgraph.cc#L586-L633](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/subgraph.cc#L586-L633)：对每个 `NodeSubset` 走 switch——`kTfNonPartition` 把节点原样加回；`kTfPartition` 用 `AddNodeWithParameters(...)` 造一个宏节点。
- [给宏节点的输出张量打上 delegate 标记:subgraph.cc#L616-L626](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/subgraph.cc#L616-L626)：`tensor->delegate = delegate;` 与 `node->delegate = delegate;`。这一步很重要——它让运行时知道「这个张量/节点的数据可能在 delegate 的私有 buffer 里」，跨边界时需要 `CopyFromBufferHandle`。

**分区数据结构** `NodeSubset`：

[NodeSubset 结构:graph_info.h#L76-L93](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/graph_info.h#L76-L93) —— 三种类型 `kTfUnexplored`（算法内部临时态）/`kTfPartition`（交给 delegate）/`kTfNonPartition`（留在 CPU）；字段 `nodes`/`input_tensors`/`output_tensors` 与 `TfLiteDelegateParams` 一一对应。分区算法的语义在 [graph_info.h#L101-L122](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/graph_info.h#L101-L122) 有详细注释：贪心模式下会把同类节点尽量合并，非贪心模式则严格保持原始执行顺序。

**标准 Prepare 骨架**（以 `SimpleDelegateInterface` 为模板）：

[DelegatePrepare 标准 skeleton:simple_delegate.cc#L82-L116](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/utils/simple_delegate.cc#L82-L116) —— 这是写新 delegate 时 `Prepare` 应有的样子，浓缩为四步：`Initialize` → 用 `GraphPartitionHelper` + `IsNodeSupportedByDelegate` 判定 → `GetNodesOfFirstNLargestPartitions`（按 `Options::max_delegated_partitions` / `min_nodes_per_partition` 取最大若干分区）→ [ReplaceNodeSubsetsWithDelegateKernels: simple_delegate.cc#L113-L115](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/utils/simple_delegate.cc#L113-L115)。`SimpleDelegateInterface` 抽象本身见 [simple_delegate.h#L77-L117](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/utils/simple_delegate.h#L77-L117)，子类只需实现 `IsNodeSupportedByDelegate`（决定支持性）与 `CreateDelegateKernelInterface`（造每个分区的 kernel）。

**通用分区工具** `GraphPartitionHelper`：

[GraphPartitionHelper:utils.h#L101-L154](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/utils.h#L101-L154) —— 提供 `Partition()`、`GetNodesOfFirstNLargestPartitions()`、`num_partitions()` 等，是 GPU/XNNPACK/NNAPI 共用的「逐节点问支持性 + 取大分区」工具。注意 `num_partitions()` 直接反映「本次委托会建几个宏节点」。

#### 4.2.4 代码实践

**实践目标**：在源码里跟踪「一次 `ReplaceNodeSubsetsWithDelegateKernels` 调用，`execution_plan_` 的长度发生了什么变化」。

**操作步骤**：

1. 打开 [subgraph.cc 的 ReplaceNodeSubsetsWithDelegateKernels:subgraph.cc#L521-L634](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/subgraph.cc#L521-L634)。
2. 假设原图 `execution_plan_` 有 10 个节点，其中节点 {2,3,4,5,6} 被 delegate 支持，其余不支持，且 {2..6} 连续。
3. 在纸上推演 `PartitionGraph` 产出的 `NodeSubset` 序列，再推演重建后的 `execution_plan_`。

**需要观察的现象**：分区结果应是三个子集：`kTfNonPartition{0,1}` → `kTfPartition{2,3,4,5,6}` → `kTfNonPartition{7,8,9}`；重建后 `execution_plan_` 长度从 10 变成 **8**（5 个支持节点被压缩成 1 个宏节点）。

**预期结果**：`execution_plan_.size()` 与宏节点个数的关系是 `新长度 = 原长度 - Σ(每个分区节点数 - 1)`。如果支持节点 {2,3,4} 和 {6} 被 {5}（假设 5 其实不支持，仅举例）隔开，则会变成两个 `kTfPartition` 子集、两个宏节点。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ModifyGraphWithDelegateImpl` 要先 `RedoAllDelegates()`？

**参考答案**：因为如果之前曾经调用过 `ResizeInputTensor` 等导致委托被「撤销但保留」（`delegates_undone_=true`，见 4.5），在叠加新 delegate 之前必须先把之前「暂时撤销」的 delegate 重新应用，否则图状态不一致。`RedoAllDelegates` 就是把 `delegates_applied_` 里的 delegate 逐个重新 `ModifyGraphWithDelegateImpl` 一遍。

**练习 2**：`kTfNonPartition` 子集里的节点会被改写吗？

**参考答案**：不会。代码里 [kTfNonPartition 分支:subgraph.cc#L593-L598](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/subgraph.cc#L593-L598) 只是把节点原样 `push_back` 进新 `execution_plan_`，它们的 `TfLiteRegistration` 还是原来的 CPU kernel，依然由 OpResolver 提供（承接 u8-l2）。

---

### 4.3 GPU delegate 实例剖析（lite.delegates.gpu）

#### 4.3.1 概念说明

GPU delegate（`TfLiteGpuDelegateV2`）把支持的计算卸载到设备 GPU。它最大的特点是「**多后端合一**」：同一个对外接口 `TfLiteGpuDelegateV2Create`，内部根据设备能力在 OpenCL、OpenGL、Metal（iOS）等 API 之间选最快的。它适合浮点（FP32/FP16）的密集计算，尤其是卷积、矩阵乘、各类激活——这类算子在 GPU 上并行度极高，往往比 CPU 快一个数量级。

它的局限：通常只支持浮点（可选地支持量化），且某些算子（如特定形状的 Split）在 OpenCL 不支持时会被排除——这些被排除的节点会自然留在 CPU，由分区机制处理。

#### 4.3.2 核心流程

GPU delegate 的 `Prepare` 完全遵循 4.2 的标准骨架，只是把「判定支持性」换成了自己复杂的 `GetOpsToReplace`：

```text
DelegatePrepare(context, delegate)                    [gpu/delegate.cc:1526]
  │
  ├─ kRegistration = CreateRegistration()             # 构造 GPU 宏节点的 TfLiteRegistration
  │
  ├─ ops_to_replace = GetOpsToReplace(context, ...)   # 逐节点问「GPU 支持吗」
  │     └─ 内部用 FP16GraphPartitionHelper + IsSupported()
  │
  └─ context->ReplaceNodeSubsetsWithDelegateKernels(   # 交给运行时分区+建宏节点
         context, kRegistration, ops_to_replace, delegate)
```

#### 4.3.3 源码精读

**对外 C 接口**（应用层调用它创建/销毁 delegate）：

[TfLiteGpuDelegateV2Create/Delete:gpu/delegate.h#L41-L49](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/gpu/delegate.h#L41-L49) —— 返回的是一个裸 `TfLiteDelegate*`，调用方负责 `TfLiteGpuDelegateV2Delete` 释放（或交给 Interpreter 接管所有权）。具体实现在 [delegate.cc#L1573-L1578](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/gpu/delegate.cc#L1573-L1578)：`new tflite::gpu::Delegate(options, /*async=*/false)` 后返回其内部的 `tflite_delegate()`。

**GPU 的 Prepare**：

[DelegatePrepare:gpu/delegate.cc#L1526-L1567](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/gpu/delegate.cc#L1526-L1567) —— 先根据平台选 sync 还是 async 的 `kRegistration`（[L1529-L1534](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/gpu/delegate.cc#L1529-L1534)），再 `GetOpsToReplace` 拿到支持节点列表，最后 [ReplaceNodeSubsetsWithDelegateKernels: delegate.cc#L1552-L1553](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/gpu/delegate.cc#L1552-L1553)。注意它还做遥测上报（`TelemetryReportDelegateSettings`）和可选的 per-op profiling。

**支持性判定** `GetOpsToReplace`：

[GetOpsToReplace:model_builder.cc#L3291-L3365](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/gpu/common/model_builder.cc#L3291-L3365) —— 这里的判定分两层：先调 `IsSupported(...)` 看算子本身和参数是否被 GPU 接受（[L3299-L3307](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/gpu/common/model_builder.cc#L3299-L3307)），再额外检查张量 dtype 是否在允许清单内（[L3308-L3350](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/gpu/common/model_builder.cc#L3308-L3350)），最后用 `FP16GraphPartitionHelper.Partition()` 汇总（[L3354-L3362](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/gpu/common/model_builder.cc#L3354-L3362)）。`FP16GraphPartitionHelper` 是 `GraphPartitionHelper` 的子类，额外处理「把支持的 FP32 节点输入重映射到 FP16 版本」这种 delegate 特有优化。

> 设计要点：`GetOpsToReplace` 把「GPU 是否支持某算子」这件事完全封装在 GPU delegate 内部，运行时对此一无所知。这正是 delegate 架构「可插拔」的体现——换一个后端，只需换一份 `IsSupported`。

#### 4.3.4 代码实践

**实践目标**：对比 GPU delegate 与 4.2 的标准 Prepare 骨架，确认 GPU delegate 完全是「标准骨架 + 自定义支持性判定」。

**操作步骤**：

1. 并排打开 [simple_delegate.cc 的 DelegatePrepare（L82-L116）](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/utils/simple_delegate.cc#L82-L116) 与 [gpu/delegate.cc 的 DelegatePrepare（L1526-L1567）](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/gpu/delegate.cc#L1526-L1567)。
2. 用一张表对齐两边的「构造 Registration / 取支持节点 / 调 ReplaceNodeSubsetsWithDelegateKernels」三步。

**需要观察的现象**：两者结构完全同构，唯一差异是「取支持节点」这一步——SimpleDelegate 用 `GraphPartitionHelper` + `IsNodeSupportedByDelegate`，GPU 用 `GetOpsToReplace`（内部也是 partition helper，但判定逻辑复杂得多）。

**预期结果**：你会得出本节最重要的结论——**所有 delegate 的 Prepare 都是同一个三段式模板，差异只集中在「判定哪些节点支持」这一步**。理解了这一点，看任何一个新 delegate 都能快速找到它的核心逻辑。

#### 4.3.5 小练习与答案

**练习 1**：如果模型里有一个 GPU 不支持的 custom op 夹在两个大卷积块之间，会建几个 GPU 宏节点？

**参考答案**：建 **2 个** GPU 宏节点。分区算法会把前一段连续支持的卷积切成一个 `kTfPartition`，中间的 custom op 单独成 `kTfNonPartition`，后一段卷积再成第二个 `kTfPartition`。运行时执行顺序是 `[GPU 宏节点1] → [CPU custom op] → [GPU 宏节点2]`，跨边界张量会经 `CopyFromBufferHandle`/`CopyToBufferHandle` 在 GPU buffer 与 CPU 内存间搬运。

**练习 2**：`TfLiteGpuDelegateV2Create` 返回的 `TfLiteDelegate*` 与 `tflite::gpu::Delegate` 是什么关系？

**参考答案**：`tflite::gpu::Delegate` 是 C++ 实现类，持有选项、后端选择、遥测等状态；它内部持有一个 `TfLiteDelegate` 成员（其 `Prepare` 指向 `DelegatePrepare`，`data_` 指向自己）。对外暴露的裸指针就是这个内部成员的地址。这是「C 接口 + C++ 实现」的典型粘合，和 u8-l1 里 TfLiteContext 与 Subgraph 的关系同构。

---

### 4.4 NNAPI delegate 与硬件抽象（lite.delegates.nnapi）

#### 4.4.1 概念说明

NNAPI（Neural Networks API）是 Android 系统层提供的统一推理抽象。Android 手机上有形形色色的 AI 加速器（高通 Hexagon DSP、联发科 APU、华为 NPU 等），每家驱动都不一样；NNAPI 在它们之上提供一套统一 C API，应用只需对接 NNAPI，系统会分派到本机实际可用的加速器。

所以 **NNAPI delegate 的角色是「TFLite 图 ↔ Android NNAPI」的翻译器**：它把 TFLite 子图翻译成 NNAPI 模型（`ANeuralNetworksModel`），交给系统编译执行。与 GPU delegate 不同，NNAPI delegate 自己不含任何计算实现，它完全依赖设备厂商的驱动。它的适用场景是：你想用一个「不挑后端、让系统选最快加速器」的方案，且主要在 Android 上部署。

#### 4.4.2 核心流程

NNAPI delegate 在结构上更特殊：它的 `StatefulNnApiDelegate` **本身直接继承自 `TfLiteDelegate`**（而不是像 SimpleDelegate 那样把 `TfLiteDelegate` 当成员），并把 `Prepare` 设成自己的 `DoPrepare`：

```text
StatefulNnApiDelegate（是一个 TfLiteDelegate）           [nnapi_delegate.h:46]
  └─ Prepare = DoPrepare                                  [nnapi_delegate.cc:6559]
       └─ DoPrepare(context, delegate)                    [nnapi_delegate.cc:6825]
            ├─ 用 NnapiDelegateKernel 逐节点判定支持性
            └─ context->ReplaceNodeSubsetsWithDelegateKernels(
                  context, nnapi_delegate_kernel, ops_to_replace, delegate)
```

注意替换用的 `nnapi_delegate_kernel`（一个 `TfLiteRegistration`）的 `init` 会真正把子图翻译成 NNAPI 模型并编译；`invoke` 则触发一次 NNAPI 执行。

#### 4.4.3 源码精读

**delegate 类声明**：

[StatefulNnApiDelegate:nnapi_delegate.h#L46](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/nnapi/nnapi_delegate.h#L46) —— `class StatefulNnApiDelegate : public TfLiteDelegate`。它的构造函数（[nnapi_delegate.h#L188-L237](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/nnapi/nnapi_delegate.h#L188-L237)）接受 `Options`（加速器类型、缓存目录、分区数等），构造时把基类的 `Prepare` 指向 `DoPrepare`。

**宏节点注册信息**：

[nnapi_delegate_kernel:nnapi_delegate.cc#L6945-L6989](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/nnapi/nnapi_delegate.cc#L6945-L6989) —— 这就是 NNAPI delegate 宏节点的 `TfLiteRegistration`：`init` 里从 `TfLiteDelegateParams*` 拿到子图，构造/复用一个 `NNAPIDelegateKernel` 并 `Init`（这一步把 TFLite 算子翻译成 NNAPI op 并编译）；`prepare`/`invoke` 转发给 kernel state；注意 `builtin_code = kTfLiteBuiltinDelegate`，确认它就是一个 delegate 宏节点。运行时真正触发 NNAPI 编译与执行的是 [ReplaceNodeSubsetsWithDelegateKernels: nnapi_delegate.cc#L7009 与 L7089](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/nnapi/nnapi_delegate.cc#L7009)。

**DoPrepare 与支持性判定**：

[DoPrepare 入口:nnapi_delegate.cc#L6825](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/nnapi/nnapi_delegate.cc#L6825) 与节点支持性函数 [IsNodeSupportedFn:nnapi_delegate.cc#L6797](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/nnapi/nnapi_delegate.cc#L6797)。和 GPU delegate 一样，这里把「某算子 NNAPI 是否支持」封装在 delegate 内部，运行时不感知。

> 对比要点：GPU delegate 把 `TfLiteDelegate` 当**成员**（`new tflite::gpu::Delegate` 后取其内部指针），NNAPI delegate 直接**继承** `TfLiteDelegate`。两种粘合方式都合法，体现了 C 结构体契约的灵活性。

#### 4.4.4 代码实践

**实践目标**：理解 NNAPI delegate 与 GPU delegate 在「`TfLiteDelegate` 如何被持有」上的差异。

**操作步骤**：

1. 打开 [nnapi_delegate.h 的 StatefulNnApiDelegate 类继承（L46）](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/nnapi/nnapi_delegate.h#L46)。
2. 对比 4.3 节 GPU delegate 的 `TfLiteGpuDelegateV2Create`（返回一个由 `tflite::gpu::Delegate` 拥有的成员指针）。
3. 在源码里搜 `StatefulNnApiDelegate` 如何被传给 `ModifyGraphWithDelegate`（它自身就是 `TfLiteDelegate`，直接传 `this`/对象地址即可）。

**需要观察的现象**：NNAPI delegate 的对象本身就能当 `TfLiteDelegate*` 用；而 GPU delegate 要先 `new` 一个 C++ 对象再取其内部 `TfLiteDelegate` 成员。

**预期结果**：你会总结出两种「C 结构体 + C++ 类」粘合范式——**继承式**（NNAPI）与**组合式**（GPU/SimpleDelegate）。组合式更解耦，继承式更省一次指针跳转，各有利弊。

#### 4.4.5 小练习与答案

**练习 1**：为什么 NNAPI delegate 的宏节点 `init` 里要做「翻译」而不是在 `Prepare` 里？

**参考答案**：因为 `init` 是在 `ReplaceNodeSubsetsWithDelegateKernels` 建宏节点时（[subgraph.cc#L612-L614 的 AddNodeWithParameters](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/subgraph.cc#L612-L614)）被运行时调用的，此时分区已确定、`TfLiteDelegateParams` 已就绪，正好把这块子图翻译成 NNAPI 模型并编译；`Prepare`（kernel 的 prepare）则处理后续的形状/张量准备。这种「init 编译、prepare 调整、invoke 执行」的分工与 OpKernel 生命周期一致（承接 u4-l2、u8-l1）。

**练习 2**：如果一台 Android 手机既没有 NPU、GPU 驱动也挂了，NNAPI delegate 还能加速吗？

**参考答案**：NNAPI 标准要求所有 Android 设备至少提供 CPU 参考实现（`nnapi-reference`），所以即便没有专用加速器，NNAPI 仍会在 CPU 上跑（不一定比 TFLite 原生 CPU kernel 快）。 delegate 本身不保证加速，只保证「把活转交给 NNAPI」。

---

### 4.5 卸载失败的回退策略（lite.delegates + interpreter_utils）

#### 4.5.1 概念说明

delegate 把计算外包出去能加速，但也带来风险：万一后端在**加载期**或**运行期**失败怎么办？比如 GPU 不支持某个组合、NNAPI 驱动崩溃、XNNPACK 在某输入形状上崩溃。TFLite 设计了**三级回退**来保证「最坏情况下也能用 CPU 跑出正确结果」：

1. **加载期回退（prepare-time fallback）**：`ModifyGraphWithDelegateImpl` 任何一步失败 → 立刻 `RemoveAllDelegates()` 还原原图，整个 delegate 等于从未应用。
2. **显式撤销/重做（undo/redo）**：`UndoAllDelegates` 暂存原始计划、`RedoAllDelegates` 重新应用，用于输入形状变化时优雅处理。
3. **运行期回退（runtime fallback）**：`Invoke()` 时宏节点报错 → `InterpreterUtils::InvokeWithCPUFallback` 自动撤销所有 delegate，用 CPU 重跑一次。

#### 4.5.2 核心流程

```text
=== 第一级：加载期回退 ===
ModifyGraphWithDelegateImpl
  └─ reset_delegation_if_not_ok(status)
       └─ 若 status != kTfLiteOk → RemoveAllDelegates() → 返回 kTfLiteDelegateError

=== 第二级：撤销/重做（输入形状变化时）===
ResizeInputTensor() 等触发 → UndoAllDelegates（暂存原图、释放宏节点）
  ... 用户再次 Invoke 前 ...
  → RedoAllDelegates（用 delegates_applied_ 重新委托）

=== 第三级：运行期回退 ===
Invoke() → 某宏节点 invoke 返回错误
  └─ 调用方用 InterpreterUtils::InvokeWithCPUFallback：
       1. 先 Invoke()，失败且 HasDelegates()
       2. 备份输入张量数据到 buf
       3. RemoveAllDelegates()
       4. 还原输入、再次 Invoke()
       5. 返回 kTfLiteDelegateError（提示「已回退，结果有效」）
```

#### 4.5.3 源码精读

**第一级——加载期回退**：

[reset_delegation_if_not_ok lambda:subgraph.cc#L2496-L2505](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/subgraph.cc#L2496-L2505) —— 这是 `ModifyGraphWithDelegateImpl` 里的局部 lambda，凡 `status != kTfLiteOk` 就 `RemoveAllDelegates()` 并报告「Restored original execution plan after delegate application failure」。它保证：**delegate 哪怕只在最后一步失败，图也回到委托前的干净状态**，不会留下半残的混合图。

**第二级——撤销/重做**：

[UndoAllDelegates 释放宏节点并恢复计划:subgraph.cc#L2183-L2198](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/subgraph.cc#L2183-L2198) —— 遍历 `execution_plan_`，对所有 `node.delegate != nullptr` 的宏节点 `CleanupNode`（释放后端资源），再把 `execution_plan_` 恢复成 `pre_delegation_execution_plan_`。注意它不删除 `delegates_applied_`，只是把 `delegates_undone_` 置真，因此稍后可以 `RedoAllDelegates` 重新应用（[subgraph.cc#L2267-L2279](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/subgraph.cc#L2267-L2279)）。彻底移除则用 [RemoveAllDelegates: subgraph.cc#L2282-L2288](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/subgraph.cc#L2282-L2288)（`UndoAllDelegates` + 清空 `delegates_applied_` + 重分配内存）。

**第三级——运行期回退**：

[InvokeWithCPUFallback:interpreter_utils.cc#L29-L71](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/interpreter_utils.cc#L29-L71) —— 这是回退机制最巧妙的部分，分四步：

- [先试一次：L30-L34](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/interpreter_utils.cc#L30-L34)：正常 `Invoke()`，成功/已取消/根本没 delegate 就直接返回。
- [备份输入：L44-L56](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/interpreter_utils.cc#L44-L56)：把所有输入张量的裸字节拷进 `buf`。注释解释了为何安全——`ArenaPlanner` 用 `preserve_inputs=true`，输入张量地址在 `RemoveAllDelegates` 后不变。
- [撤销所有 delegate：L58](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/interpreter_utils.cc#L58)：`RemoveAllDelegates()`。
- [还原输入并重跑：L61-L69](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/interpreter_utils.cc#L61-L69)：从 `buf` 拷回输入，再 `Invoke()` 一次。返回 `kTfLiteDelegateError` 表示「委托失败但已用 CPU 兜底，输出有效」。

其声明与使用约束在 [interpreter_utils.h#L25-L46](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/interpreter_utils.h#L25-L46) —— 注释特别强调：允许回退的前提是「调用方不在跨 `Invoke()` 之间缓存张量数据指针」且「模型无状态或状态在 batch 间不需要」。

> 运行期回退的代价：第一次 `Invoke` 失败 + `RemoveAllDelegates`（重建计划与内存）+ 第二次 `Invoke`，单次推理延迟会有一次明显尖峰。但换来的是「结果永远正确」。生产环境里它是兜底，不应频繁触发——频繁触发说明 delegate 选型有问题。

#### 4.5.4 代码实践

**实践目标**：用一次 `InvokeWithCPUFallback` 的「备份-撤销-重跑」流程，理解运行期回退为何能保证结果正确。

**操作步骤**：

1. 阅读 [interpreter_utils.cc#L29-L71](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/interpreter_utils.cc#L29-L71) 的完整实现。
2. 回答：如果第 58 行 `RemoveAllDelegates()` 会改变输入张量的内存地址，第 61-66 行「从 buf 拷回输入」还能工作吗？源码注释（L42-L43）是怎么保证地址不变的？

**需要观察的现象**：源码注释明确依赖 `ArenaPlanner` 的 `preserve_inputs=true` 这一上游保证。这是典型的「跨模块契约」——回退逻辑能正确工作，是因为内存规划器承诺了输入张量地址稳定。

**预期结果（待本地验证）**：在一个真实带 delegate 的模型上，若故意让 delegate kernel 在 `invoke` 里返回 `kTfLiteError`，并改用 `InvokeWithCPUFallback`，应能观察到：第一次 Invoke 报错 → 日志打印 "Invoke() failed in the presence of delegation. Retrying without." → 第二次 Invoke 成功 → 返回 `kTfLiteDelegateError` 且输出与纯 CPU 一致。

#### 4.5.5 小练习与答案

**练习 1**：`UndoAllDelegates` 和 `RemoveAllDelegates` 的区别是什么？

**参考答案**：`UndoAllDelegates` 是「暂时撤销」：释放宏节点、恢复原始 `execution_plan_`，但**保留 `delegates_applied_` 列表**，并设 `delegates_undone_=true`，之后可用 `RedoAllDelegates` 重新委托。`RemoveAllDelegates` 是「彻底移除」：在 `UndoAllDelegates` 基础上**清空 `delegates_applied_`**、重置 `delegates_undone_` 并重分配内存，delegate 不再可恢复。前者用于输入形状变化后重委托，后者用于彻底回退到纯 CPU。

**练习 2**：为什么 `InvokeWithCPUFallback` 的注释要求「模型无状态（无 variables、无 LSTM）或状态在 batch 间不需要」？

**参考答案**：因为回退流程是「第一次 Invoke 失败后丢弃那次的所有副作用，再重跑」。如果模型带状态（如 variable 的累加、LSTM 的隐状态），第一次失败 Invoke 可能已经部分改写了状态，第二次 CPU Invoke 会基于被污染的状态继续，结果就错了。无状态模型没有这种「部分副作用」问题，重跑等价于从头跑，所以安全。

---

## 5. 综合实践

**任务**：把本讲的三块知识（图分区、GPU delegate、运行期回退）串起来，在源码层面完整还原「用户启用一个 GPU delegate 后，Interpreter 如何把可加速子图交给它，不可加速部分如何回退到 CPU kernel」这一全过程，并给出一份带行号引用的「调用链说明文档」。

**要求完成的工作**：

1. **分区链路**：从 [gpu/delegate.cc 的 DelegatePrepare（L1526）](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/gpu/delegate.cc#L1526) 出发，依次标注 `GetOpsToReplace` → `ReplaceNodeSubsetsWithDelegateKernels` → `PartitionGraph` → `NodeSubset` 重建 这四步各自所在的文件与行号，用箭头画出调用链。
2. **运行链路**：标注 [subgraph.cc 的 InvokeImpl（L1662）](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/subgraph.cc#L1662) 如何遍历新 `execution_plan_`，遇到 `kTfNonPartition` 节点走 CPU kernel、遇到 delegate 宏节点走其 `invoke`。
3. **回退链路**：标注 [interpreter_utils.cc 的 InvokeWithCPUFallback（L29）](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/interpreter_utils.cc#L29) 在宏节点 invoke 失败时的「备份→RemoveAllDelegates→重跑」三步。

**可选的运行验证（待本地验证，需 Android/Linux 真机或带 GPU 的环境）**：用下面的示例代码加载一个 `.tflite` 模型并启用 GPU delegate（示例代码，非项目原有）：

```python
# 示例代码：仅供参考，运行需要 tflite_runtime 与对应平台的 GPU delegate 共享库
import tflite_runtime.interpreter as tflite

interpreter = tflite.Interpreter(
    model_path="model.tflite",
    experimental_delegates=[
        tflite.load_delegate("lib delegate.so")  # 替换为实际 GPU delegate 路径
    ],
)
interpreter.allocate_tensors()
# ... 填输入 ...
interpreter.invoke()
```

**需要观察的现象**：开启 verbose 日志后（如 `TFLITE_LOG_VERBOSE`），应能看到类似 `Replacing N out of M node(s) with delegate (TfLiteGpuDelegateV2) node, yielding K partitions` 的日志（来自 [subgraph.cc#L578-L584](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/core/subgraph.cc#L578-L584)），它直接告诉你「这次委托建了几个分区、替换了几个节点」。把日志里的 N/M/K 与你手算的分区结果对照。

**预期结果**：你能用一张图说清——**分区是「加载期一次性」的，回退是「运行期按需」的；前者用 `pre_delegation_execution_plan_` 作底片，后者用 `buf` 暂存输入再 `RemoveAllDelegates`**。两者共同保证「加速优先、正确兜底」。

## 6. 本讲小结

- **delegate 是契约不是实现**：`TfLiteDelegate` 这个 C 结构体用函数指针（核心是 `Prepare`）定义了「后端如何声明支持的节点并接管计算」，运行时只认契约，从而可插拔任意后端。
- **图分区是委托的核心机制**：delegate 的 `Prepare` 提供 `nodes_to_replace`，运行时用 `PartitionGraph` 切出 `kTfPartition`/`kTfNonPartition` 子集，把每个支持分区替换成一个 `kTfLiteBuiltinDelegate` 宏节点，重建后的 `execution_plan_` 由 CPU 段与宏节点交替构成。
- **所有 delegate 的 Prepare 都是同一个三段式模板**：构造 `TfLiteRegistration` → 判定支持节点 → `ReplaceNodeSubsetsWithDelegateKernels`。GPU（`GetOpsToReplace`）、NNAPI（`DoPrepare`）、XNNPACK（`PrepareOpsToDelegate`）的差异只集中在「判定支持性」这一步。
- **GPU 与 NNAPI 代表两类粘合范式**：GPU 把 `TfLiteDelegate` 当**组合成员**且多后端合一；NNAPI 直接**继承** `TfLiteDelegate` 并把图翻译成系统 NNAPI 模型，自身不含计算。
- **三级回退保证正确性**：加载期 `reset_delegation_if_not_ok`→`RemoveAllDelegates`；形状变化用 `UndoAllDelegates`/`RedoAllDelegates` 暂存重做；运行期 `InvokeWithCPUFallback` 备份输入、撤销 delegate、CPU 重跑。
- **回退依赖跨模块契约**：`InvokeWithCPUFallback` 之所以能正确还原输入，依赖 `ArenaPlanner` 的 `preserve_inputs=true`；这正是「为什么回退要求模型无状态」的根因。

## 7. 下一步学习建议

- **亲手写一个最小 delegate**：精读 [`tensorflow/lite/delegates/utils/simple_delegate.h`](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/utils/simple_delegate.h) 与仓库自带的示例 `tensorflow/lite/delegates/utils/dummy_delegate/`，实现一个 `SimpleDelegateInterface` 子类，跑通「自己声明支持某几个 op 并把它们替换成一个空操作宏节点」。这是巩固本讲最有效的练习。
- **深入 XNNPACK delegate**：它是生产环境里**默认开启**的 CPU 后端，逻辑相对 GPU/NNAPI 更易读，建议精读 [xnnpack_delegate.cc 的 DelegatePrepare（L7637）](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/xnnpack/xnnpack_delegate.cc#L7637) 与其 `PrepareOpsToDelegate`，理解「为什么连纯 CPU 也要走 delegate 机制」（答案：XNNPACK 的算子融合与高度手写优化比 TFLite reference kernel 快很多）。
- **承接 TFLite 序列化与 telemetry**：本讲多处出现 `TelemetryReportDelegateSettings`。可继续阅读 [`tensorflow/lite/delegates/serialization.h`](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/serialization.h) 与 [`telemetry.h`](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/telemetry.h)，理解 delegate 的编译结果（如 GPU 编译出的 kernel）如何被缓存到磁盘、遥测数据如何上报，这是生产部署的关键一环。
- **回到执行模型主线**：本讲是「边缘部署」单元的收尾。若想继续追 TFLite 的边界，可研究 control flow op（`WHILE`/`IF`）如何跨子图委托（涉及 `MarkSubgraphAsDelegationSkippable`，见 [utils.h#L62-L93](https://github.com/tensorflow/tensorflow/blob/44d7a2dcda991165f62cdd9633b7c6421610cdda/tensorflow/lite/delegates/utils.h#L62-L93)），那是 delegate 机制里最复杂的一块。
