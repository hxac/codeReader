# TFLite 委托机制 delegates

## 1. 本讲目标

在上一讲（u8-l2）里，我们讲清了 `.tflite` 模型如何被 FlatBuffer 存储、`OpResolver` 如何把模型里的算子解析到具体的 CPU kernel。这回答了「算子从哪来」，但留下了一个性能问题：**Interpreter 默认只在 CPU 上跑**，而移动设备上往往还有 GPU、NPU、DSP 等专用加速器。TFLite 用一套统一的「委托（delegate）」机制来桥接这些异构后端。

本讲学完后，你应当能够：

- 说清 **delegate 是什么**：它是一份「请把图中这些节点交给我，我用加速器算」的契约。
- 掌握 **图分区与卸载（partition & offload）**：用户给一个 delegate，运行时如何把连续可加速节点合并成若干个「宏节点」，并改写执行计划。
- 理解 **delegate kernel**：那个合并出来的宏节点本身也是一个 `TfLiteRegistration`，有自己的 `init/free/prepare/invoke`，它就是加速器进入 TFLite 执行循环的入口。
- 辨析 **GPU / NNAPI / XNNPACK 三大常见 delegate** 的适用场景与差异。
- 认识 **失败回退（fallback）策略**：委托在 Prepare 阶段失败、或在运行时 Invoke 失败时，运行时如何退回到纯 CPU 执行。

## 2. 前置知识

本讲建立在 u8-l1（TFLite 架构与 Interpreter）和 u8-l2（FlatBuffer 与 OpResolver）之上。开始前请确认你理解：

- **Interpreter 与 execution_plan**：Interpreter 持有一个 Subgraph，推理时按 `execution_plan_`（一个扁平的节点索引列表）顺序逐个执行算子。本讲的核心就是「这个列表如何被 delegate 改写」。
- **TfLiteRegistration**：在 u8-l2 里我们见过它——算子是一组 C 函数指针（`init/free/prepare/invoke`）。本讲的关键发现是：**delegate 卸载出的宏节点，用的就是同一套 `TfLiteRegistration`**，只是 `builtin_code` 被标成 `DELEGATE`。
- **TfLiteContext**：算子与运行时通信的「总线」，提供 `ReplaceNodeSubsetsWithDelegateKernels`、`ResizeTensor` 等能力。delegate 的 Prepare 正是通过它拿到图的视图。
- **（可选）C++ 静态全局对象自动注册**：见 u4-l1。delegate 不走这条，它是用户**显式**通过 `ModifyGraphWithDelegate` 接入的——这是它和普通 op 的根本区别。

一个直觉比喻：把 TFLite 的计算图想象成一条流水线，默认每个工位（节点）都用 CPU 这个「通用工人」。delegate 就像一个外包公司，它跑来巡视一遍流水线，说「第 3 到第 7 个工位、第 10 到第 15 个工位我都能干，而且更快」。运行时于是把这几段连续工位打包成两个「外包箱」，其余工位仍由 CPU 干。本讲要回答的就是：这个巡视、打包、改写流水线的过程，以及外包箱万一坏掉怎么退货。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [tensorflow/lite/core/c/common.h](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/core/c/common.h) | 定义 `TfLiteDelegate` 结构体、`TfLiteDelegateFlags`、`TfLiteRegistration`——delegate 契约的「法律文本」。 |
| [tensorflow/lite/core/subgraph.cc](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/core/subgraph.cc) | `ModifyGraphWithDelegateImpl`、`ReplaceNodeSubsetsWithDelegateKernels`——运行时执行改写的主调度逻辑。 |
| [tensorflow/lite/graph_info.h](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/graph_info.h) | `NodeSubset` 结构体与 `PartitionGraphIntoIndependentNodeSubsets`——分区算法的接口。 |
| [tensorflow/lite/delegates/utils.h](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/delegates/utils.h) | `GraphPartitionHelper`——写 delegate 时复用的分区助手，封装「判断哪些节点可加速」的样板。 |
| [tensorflow/lite/delegates/gpu/delegate.cc](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/delegates/gpu/delegate.cc) | GPU delegate 的实现：`DelegatePrepare`、`CreateRegistration`、`Delegate` 类。 |
| [tensorflow/lite/delegates/gpu/common/model_builder.cc](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/delegates/gpu/common/model_builder.cc) | `GetOpsToReplace`——GPU delegate 判定「我能加速哪些节点」。 |
| [tensorflow/lite/delegates/nnapi/nnapi_delegate.h](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/delegates/nnapi/nnapi_delegate.h) | `StatefulNnApiDelegate`——Android NNAPI delegate 的 C++ 接口。 |
| [tensorflow/lite/delegates/xnnpack/xnnpack_delegate.cc](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/delegates/xnnpack/xnnpack_delegate.cc) | XNNPACK delegate（CPU 上的高度优化算子库）的 `DelegatePrepare`。 |
| [tensorflow/lite/delegates/interpreter_utils.cc](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/delegates/interpreter_utils.cc) | `InvokeWithCPUFallback`——运行时失败的回退工具函数。 |
| [tensorflow/lite/python/interpreter.py](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/python/interpreter.py) | Python 侧 `load_delegate` 与 `experimental_delegates`——用户接入 delegate 的入口。 |

## 4. 核心概念与源码讲解

本讲把 delegate 机制拆成五个最小模块：契约、分区改写、宏节点的实现、常见 delegate 对比、失败回退。它们串起来就是「用户给一个 delegate → 运行时分区 → 生成宏节点 → 执行 → 万一失败就退回」的完整生命周期。

### 4.1 Delegate 接口契约：TfLiteDelegate 与 Prepare

#### 4.1.1 概念说明

**delegate（委托）** 是 TFLite 提供的一种插件机制：它让一个外部加速器（GPU、NPU、DSP，甚至高度优化的 CPU 库如 XNNPACK）能够「认领」计算图中的部分节点，由它自己来执行，而不是走 Interpreter 默认的 CPU kernel。

delegate 之所以重要，是因为它把「图的表示」和「执行后端」解耦了：模型本身不需要知道自己将来会跑在 GPU 还是 NPU 上，用户只需在创建 Interpreter 时挂上一个或多个 delegate，运行时就自动把能加速的子图交给对应后端。

所有 delegate 都必须实现一个 C 结构体 `TfLiteDelegate`，它就是 delegate 与运行时之间的「法律契约」。

#### 4.1.2 核心流程

`TfLiteDelegate` 的生命周期遵循「**用户创建 → 运行时调用 Prepare 改图 → 多次 Invoke → 用户销毁**」的模式：

1. **用户创建**：调用某个 delegate 的 `Create` 函数（如 `TfLiteGpuDelegateV2Create`），拿到一个 `TfLiteDelegate*`。这一步只做选项配置，不碰图。
2. **改图（关键）**：用户调 `interpreter->ModifyGraphWithDelegate(delegate)`。运行时会回调 delegate 的 `Prepare` 函数指针，把当前图（通过 `TfLiteContext*`）展示给 delegate。
3. **Prepare 内部**：delegate 检查每个节点自己能否加速，把「能加速的节点列表」交给运行时的 `ReplaceNodeSubsetsWithDelegateKernels`，请求把这些节点替换成 delegate 宏节点。
4. **执行**：之后每次 `Invoke`，被替换出的宏节点会调用 delegate 提供的 `invoke` 回调，在加速器上跑。
5. **用户销毁**：delegate 对象由用户拥有，必须比 Interpreter 活得更长，最后用对应的 `Delete` 函数释放。

> ⚠️ 注意：和普通 op（u8-l2 里 `REGISTER_OP`/`OpResolver` 那套，在启动期靠 C++ 静态全局对象自动注册）不同，**delegate 是用户在运行期显式挂载的**。这是理解两者区别的关键。

#### 4.1.3 源码精读

`TfLiteDelegate` 结构体定义在 `tensorflow/lite/core/c/common.h`：

[tensorflow/lite/core/c/common.h:1408-1459](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/core/c/common.h#L1408-L1459) — 这是 delegate 的核心结构体。中文逐字段说明：

- `data_`：delegate 自己的状态指针，由 delegate 拥有，运行时只透传不解释。
- `Prepare`：**最关键的字段**。被 `ModifyGraphWithDelegate` 调用，给 delegate 一份当前图的视图（`TfLiteContext*`）。它的典型实现是：遍历节点、判断哪些可加速，然后调 `ReplaceNodeSubsetsWithDelegateKernels` 请求运行时把可加速子图替换成宏节点。
- `CopyFromBufferHandle` / `CopyToBufferHandle` / `FreeBufferHandle`：用于「零拷贝」缓冲区管理。当 delegate 用自己的显存（如 GPU 纹理）存张量时，这三个回调负责在显存句柄和 CPU 内存之间搬运数据。不用自己缓冲区的 delegate 可以把它们置 `nullptr`。
- `flags`：位掩码，见 `TfLiteDelegateFlags`。
- `opaque_delegate_builder`：新一代「不透明」builder，优先级高于 `Prepare`，用于 ABI 稳定的扩展 API。

`flags` 的取值定义在同一文件，是理解 delegate 能力边界的钥匙：

[tensorflow/lite/core/c/common.h:1358-1405](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/core/c/common.h#L1358-L1405) — `TfLiteDelegateFlags` 枚举，要点：

- `kTfLiteDelegateFlagsAllowDynamicTensors = 1`：delegate 能否处理动态尺寸张量。**不能**的话，运行时会在挂载 delegate 前强制把所有形状确定下来，并把整个图冻结为不可变。
- `kTfLiteDelegateFlagsRequirePropagatedShapes = 2`：要求运行时在张量尺寸变化时自动传播形状。
- `kTfLiteDelegateFlagsPerOperatorProfiling = 4`：请求逐算子性能剖析。
- `kTfLiteDelegateFlagsHintFullyDelegatedToSingleDelegate = 8`：调用方提示「整图都会被这一个 delegate 吃下」，运行时可据此跳过部分分配。

一个具体例子——GPU delegate 的 `Delegate` 构造函数如何填写这份契约：

[tensorflow/lite/delegates/gpu/delegate.cc:231-237](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/delegates/gpu/delegate.cc#L231-L237) — GPU delegate 把 `data_` 指向自己、把 `Prepare` 接到自己的 `DelegatePrepare`、三个 buffer 回调置空（用 TFLite 默认张量）、`flags` 只设了逐算子剖析。这就是一份「我不做零拷贝、但我支持 profiling」的简明声明。

#### 4.1.4 代码实践

**实践目标**：在源码层面确认「写一个 delegate 需要填哪些字段」。

**操作步骤**：

1. 打开 [tensorflow/lite/delegates/gpu/delegate.cc:225](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/delegates/gpu/delegate.cc#L225)，阅读 `Delegate` 类构造函数。
2. 打开 [tensorflow/lite/delegates/nnapi/nnapi_delegate.h:46](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/delegates/nnapi/nnapi_delegate.h#L46)，看 `StatefulNnApiDelegate` 的声明。

**需要观察的现象**：GPU delegate 是「内嵌一个 `TfLiteDelegate` 成员、在构造函数里填字段」；而 NNAPI 的 `StatefulNnApiDelegate` 直接**继承** `TfLiteDelegate`（因为 `TfLiteDelegate` 是个 C struct，C++ 里可以继承）。两种写法殊途同归——最终都是要填好 `data_/Prepare/.../flags`。

**预期结果**：你能用一句话说出「`Prepare` 函数指针是 delegate 唯一不可省略的核心字段」。

#### 4.1.5 小练习与答案

**练习 1**：如果一个 delegate 既不实现 `CopyToBufferHandle` 也不实现 `CopyFromBufferHandle`（都置 `nullptr`），运行时如何处理它的输入输出张量？

**参考答案**：delegate 的宏节点仍然读写 TFLite 默认的 CPU 张量缓冲区。每次 Invoke 前运行时把输入从 CPU 张量拷进 delegate、Invoke 后再把输出拷回 CPU 张量——没有零拷贝，但兼容性最好。这正是 GPU delegate 默认配置的做法（见 delegate.cc:233-235 三个回调置空）。

**练习 2**：`kTfLiteDelegateFlagsAllowDynamicTensors` 不设（即不支持动态张量）时，运行时会有什么副作用？

**参考答案**：运行时会在挂载 delegate 前先 `PrepareOpsStartingAt` 跑一遍形状推导，若发现仍有动态尺寸张量就直接返回 `kTfLiteApplicationError` 拒绝挂载；挂载成功后会把图冻结为 `kStateInvokableAndImmutable`，此后张量尺寸不能再变（否则要 `RemoveAllDelegates` 重来）。

---

### 4.2 图分区与卸载：ModifyGraphWithDelegate 全链路

#### 4.2.1 概念说明

delegate 的 `Prepare` 只是声明「我要这些节点」，**真正把执行计划改写过来是运行时的职责**。这一步叫「图分区（graph partitioning）」：

- 运行时把 `execution_plan_`（扁平的节点列表）重新组织成若干段 **NodeSubset（节点子集）**。
- 每个子集要么是 **`kTfPartition`**（delegate 认领的连续段，将被合并成一个宏节点），要么是 **`kTfNonPartition`**（delegate 不认领、仍由 CPU 逐个执行的段）。
- 改写后的 `execution_plan_` 在「CPU 原生节点」和「delegate 宏节点」之间交替。

为什么需要分区而不是简单地把支持节点全替换？因为加速器不一定支持所有 op。假设一张图是 `Conv → Reshape → Conv → CustomOp → Conv`，GPU 支持 Conv 但不支持那个 CustomOp，那么运行时会切成 `[Conv,Reshape,Conv]`（给 GPU）、`[CustomOp]`（留 CPU）、`[Conv]`（再给 GPU）三段，产生两个 GPU 宏节点夹一个 CPU 节点。

#### 4.2.2 核心流程

`Subgraph::ModifyGraphWithDelegateImpl`（subgraph.cc:2485）是整个改写的总调度，分三步：

```text
用户调用 ModifyGraphWithDelegate(delegate)
        │
        ▼
STEP 1  验证 & 准备
        ├─ RedoAllDelegates()（若有之前被撤销的 delegate，先恢复）
        ├─ 读取 delegate->flags，判断是否支持动态张量
        ├─ 若不支持动态张量：先 PrepareOpsStartingAt 跑形状推导
        │   └─ 若仍有动态张量 → 返回 kTfLiteApplicationError 拒绝
        └─ 若是第一个 delegate：保存 pre_delegation_execution_plan_（回退用）
        │
        ▼
STEP 2  delegate 改图（核心）
        ├─ SwitchToDelegateContext()  // 把 ReplaceNodeSubsets... 挂到 context
        ├─ TfLiteDelegatePrepareInternal(context, delegate)
        │       └─ 回调 delegate->Prepare(context, delegate)
        │             └─ Prepare 内部：判定可加速节点 → 调
        │                context->ReplaceNodeSubsetsWithDelegateKernels(...)
        │                   └─ PartitionGraph → 重建 execution_plan_
        └─ SwitchToKernelContext()
        │
        ▼
STEP 3  收尾：保持图状态一致
        ├─ 不支持动态张量 → 标记 kStateInvokableAndImmutable（冻结）
        └─ delegates_applied_.push_back(delegate)
```

其中 `ReplaceNodeSubsetsWithDelegateKernels`（subgraph.cc:521）做的是真正的「重排执行计划」：

```text
输入：nodes_to_replace = delegate 认领的节点索引列表
        │
        ▼
1. 把传入的 registration 的 builtin_code 标成 DELEGATE
2. PartitionGraph(nodes_to_replace, &node_subsets)
     → 调 PartitionGraphIntoIndependentNodeSubsets（graph_info.h）
     → 把图切成交替的 kTfPartition / kTfNonPartition 子集
3. execution_plan_.clear()
4. for 每个 node_subset:
     ├─ kTfNonPartition：把里面的节点原样 push 回 execution_plan_
     └─ kTfPartition：创建一个 delegate 宏节点
           ├─ CreateDelegateParams(node_subset)  // 打包 nodes/inputs/outputs
           ├─ AddNodeWithParameters(...)          // 往图里加这个宏节点
           └─ 把子集输出张量的 delegate 字段、节点的 delegate 字段都记上
```

#### 4.2.3 源码精读

先看总调度的「三步走」结构：

[tensorflow/lite/core/subgraph.cc:2507-2608](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/core/subgraph.cc#L2507-L2608) — `ModifyGraphWithDelegateImpl`。注意三个关键设计：

1. **第 2544-2548 行**：第一个 delegate 挂载时保存原始执行计划 `pre_delegation_execution_plan_`。这是后面「回退」能成立的根本——原始 CPU 节点信息被完整留底。
2. **第 2554-2555 行**：`SwitchToDelegateContext()` 把 `context_.ReplaceNodeSubsetsWithDelegateKernels` 等函数指针挂上去（见下），随后 `TfLiteDelegatePrepareInternal` 才会真正回调 `delegate->Prepare`。
3. **第 2496-2505 行**的 `reset_delegation_if_not_ok` 闭包：若 STEP 2 失败，立即调 `RemoveAllDelegates()` 恢复原始计划并返回 `kTfLiteDelegateError`。这是「Prepare 阶段失败」的回退点。

delegate 是怎么拿到 `ReplaceNodeSubsetsWithDelegateKernels` 这个能力的？答案在 `SwitchToDelegateContext`：

[tensorflow/lite/core/subgraph.cc:2131-2134](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/core/subgraph.cc#L2131-L2134) — 切到 delegate context 时，运行时把自己的分区函数 `Subgraph::ReplaceNodeSubsetsWithDelegateKernels` 和 `PreviewDelegatePartitioning` 注册到 `context_` 上。于是 delegate 在自己的 `Prepare` 里就能调 `context->ReplaceNodeSubsetsWithDelegateKernels(...)` 请求改图。这是「运行时通过 context 把能力下放给 delegate」的典型 C 风格接口。

真正的「重排执行计划」逻辑：

[tensorflow/lite/core/subgraph.cc:521-634](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/core/subgraph.cc#L521-L634) — `Subgraph::ReplaceNodeSubsetsWithDelegateKernels`。重点看第 568-632 行的循环：

- 第 570-573 行调 `PartitionGraph` 得到 `node_subsets`。
- 第 593-598 行：`kTfNonPartition` 的节点直接原样放回 `execution_plan_`。
- 第 599-627 行：`kTfPartition` 的节点被**合并成一个**宏节点（`AddNodeWithParameters`），并把子集的输入/输出张量列表打包进 `TfLiteDelegateParams`。注意第 617-622 行：子集的输出张量被标记 `tensor->delegate = delegate`，第 626 行宏节点本身 `node->delegate = delegate`。这些标记是后续判断「这个张量/节点归谁管」的依据。

分区算法本身的核心数据结构在 `graph_info.h`：

[tensorflow/lite/graph_info.h:76-93](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/graph_info.h#L76-L93) — `NodeSubset` 结构体。`type` 区分 `kTfPartition`（被 delegate 认领）和 `kTfNonPartition`（留 CPU）；`input_tensors` / `output_tensors` 描述子集的边界——后者注释尤其重要：「不出现在 output_tensors 列表里的输出都是中间结果，可以省略」。

[tensorflow/lite/graph_info.h:101-158](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/graph_info.h#L101-L158) — `PartitionGraphIntoIndependentNodeSubsets` 的接口与详尽注释。第 107-116 行讲清了 `greedily` 参数：贪心模式下会尽量把可调度节点并入同一子集；非贪心则严格按原始执行序切分。第 124-145 行还用一个图例说明控制依赖如何影响分区结果——读这段注释是理解分区算法最快的方式。

> 💡 一句话总结：**分区算法把「哪些连续节点归 delegate」这个集合问题，转化为「按拓扑序切成交替子集」的图论问题**，并保证子集内部、子集之间都满足依赖顺序。

#### 4.2.4 代码实践

**实践目标**：把分区改写的过程「画」出来，验证你对 `NodeSubset` 的理解。

**操作步骤**：

1. 假设一张小图，execution_plan_ = `[0:Conv, 1:BiasAdd, 2:CustomOp, 3:Conv, 4:Relu]`。假设某个 delegate 只支持 Conv/BiasAdd/Relu，不支持 CustomOp。
2. 那么 delegate 在 Prepare 里会把 `nodes_to_replace = {0, 1, 3, 4}` 传给 `ReplaceNodeSubsetsWithDelegateKernels`。
3. 对照 [tensorflow/lite/graph_info.h:124-145](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/graph_info.h#L124-L145) 的算法逻辑，写出分区结果。

**需要观察的现象**：因为 CustomOp 把两段 Conv「断开」了，分区结果会是三个子集：

- `kTfPartition {0,1}` → 合并成宏节点 D1
- `kTfNonPartition {2}` → CustomOp 原样保留
- `kTfPartition {3,4}` → 合并成宏节点 D2

改写后 `execution_plan_ = [D1, 2(CustomOp), D2]`，推理时按 Conv 段（GPU）→ CustomOp（CPU）→ Conv+Relu 段（GPU）交替执行。

**预期结果**：你能解释「为什么 delegate 中间夹一个不支持的 op 会导致产生**两个** delegate 宏节点而不是一个」，并理解这会带来 CPU↔加速器之间的数据搬运开销。

#### 4.2.5 小练习与答案

**练习 1**：为什么 TFLite 的分区倾向于把不支持的 op 「夹断」成独立 CPU 段，而不是干脆放弃整个 delegate？

**参考答案**：为了最大化加速收益。即便只有一段连续的可加速子图，把它交给 GPU 也能省下那部分的 CPU 时间。代价是每多一段边界就多一次 CPU↔加速器的张量拷贝，所以 delegate 通常有 `max_delegated_partitions` 限制（如 GPU 默认 1、NNAPI 默认 3），避免「碎成太多小段、拷贝开销反超加速收益」。

**练习 2**：`pre_delegation_execution_plan_` 保存的是「挂第一个 delegate 之前」还是「挂当前这个 delegate 之前」的计划？

**参考答案**：是「挂**第一个** delegate 之前」的纯 CPU 原始计划（subgraph.cc:2544-2548 的 `if (delegates_applied_.empty())` 判断）。这样无论挂了多少个 delegate，回退时都能一键回到「没有任何 delegate」的干净状态。

---

### 4.3 delegate kernel：DELEGATE 宏节点的实现

#### 4.3.1 概念说明

上一模块我们看到，被 delegate 认领的连续节点会合并成一个「宏节点」。这个宏节点在运行时是什么？答案令人意外又合理：**它就是一个普通的算子节点，持有自己的 `TfLiteRegistration`**，只不过 `builtin_code` 被标成 `DELEGATE`。

回忆 u8-l2：每个算子都是一组 C 函数指针 `init/free/prepare/invoke`。delegate 宏节点也不例外——它的这四个回调由 delegate 提供，构成所谓的 **delegate kernel（委托内核）**：

- `init`：宏节点第一次创建时调用，传入打包好的 `TfLiteDelegateParams`（含子集的输入/输出张量与节点列表）。**这是加速器真正「编译」子图的地方**——例如 GPU delegate 在这里把子图编译成 OpenCL/OpenGL 计算图。
- `prepare`：输入尺寸变化时调用，让 delegate kernel 重新规划（如重新分配显存）。
- `invoke`：每次推理时调用，**让加速器真正执行这段子图**。
- `free`：宏节点销毁时释放 delegate kernel 资源。

推理时，Interpreter 仍按 `execution_plan_` 顺序逐节点执行；遇到 delegate 宏节点，就和遇到普通 op 一样去调它的 `invoke`——只不过这个 `invoke` 内部跑的是 GPU/NNAPI 而非 CPU kernel。这就是「delegate 对执行循环透明」的精髓。

#### 4.3.2 核心流程

以 GPU delegate 为例，宏节点的生命周期如下：

```text
挂载 delegate（ModifyGraphWithDelegate）
        │
        ▼
ReplaceNodeSubsetsWithDelegateKernels
  把 registration.builtin_code = DELEGATE
  对每个 kTfPartition 子集 AddNodeWithParameters(...)  // 创建宏节点
        │
        ▼  创建宏节点时触发 registration.init
registration.init(context, buffer=TfLiteDelegateParams):
  ├─ new DelegateKernel(gpu_delegate)
  └─ DelegateKernel::Prepare(context, params)
        └─ 把 TFLite 子图转成 GraphFloat32
        └─ InitializeOpenClApi / InitializeOpenGlApi  // 编译成 GPU 计算图
        └─ 返回 DelegateKernel* 作为 user_data
        │
        ▼  之后每次推理
registration.invoke(context, node):
  └─ GetDelegateKernel(node)->Invoke(context)  // 在 GPU 上跑这段子图
        │
        ▼  销毁
registration.free(context, buffer):
  └─ delete DelegateKernel*
```

#### 4.3.3 源码精读

先确认「宏节点和普通 op 共用同一套注册结构」：

[tensorflow/lite/core/c/common.h:1184-1228](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/core/c/common.h#L1184-L1228) — `TfLiteRegistration`。第 1210 行 `init`、第 1214 行 `free`、第 1222 行 `prepare`、第 1228 行 `invoke`——delegate 宏节点填的就是这同一组字段。注意第 1206-1210 行注释特别提到：「对 delegate kernel，init 失败时返回 `TfLiteKernelInitFailed()`，最终会让 `ModifyGraphWithDelegate` 返回错误」。

`builtin_code` 被强制改成 DELEGATE 的地方：

[tensorflow/lite/core/subgraph.cc:524-528](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/core/subgraph.cc#L524-L528) — `ReplaceNodeSubsetsWithDelegateKernels` 一进来就把传入 registration 的 `builtin_code` 标成 `BuiltinOperator_DELEGATE`。这是宏节点的「身份证」。

GPU delegate 如何构造这份 registration（即它的 delegate kernel）：

[tensorflow/lite/delegates/gpu/delegate.cc:1409-1468](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/delegates/gpu/delegate.cc#L1409-L1468) — `CreateRegistration()`。逐字段看这份 lambda 注册：

- **`.init`**（1412-1427 行）：从 `buffer` 反解出 `TfLiteDelegateParams`，`new` 一个 `DelegateKernel`，调它的 `Prepare` 把这段子图编译成 GPU 计算图；失败返回 `nullptr`（触发 `TfLiteKernelInitFailed`）。
- **`.free`**（1429-1431 行）：`delete` 掉 `DelegateKernel`。
- **`.prepare`**（1433-1452 行）：计算所需的临时张量 `temporaries`。
- **`.invoke`**（1454-1462 行）：调 `GetDelegateKernel(node)->Invoke(context)`——**这就是 GPU 真正执行推理的入口**。

`init` 里那段「把 TFLite 子图转成 GPU 内部表示、再编译」的工作量最重，在：

[tensorflow/lite/delegates/gpu/delegate.cc:442-482](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/delegates/gpu/delegate.cc#L442-L482) — `DelegateKernelCore` 的 Prepare。第 445-449 行先把 TFLite 子图转成 `GraphFloat32`；第 451-482 行按选项选后端——`CL_ONLY` 走 OpenCL、`GL_ONLY` 走 OpenGL、**默认先试 OpenCL，失败再回退 OpenGL**（第 463-482 行）。这段揭示了 GPU delegate 内部「编译期选后端」的策略。

> 💡 一个深刻的观察：**delegate kernel 的 init 把「昂贵的一次性编译」和「廉价的反复执行」分离了**——编译发生在挂载 delegate 时（只一次），之后每次 Invoke 只跑已编译好的计算图。这和 u7 讲的 XLA JIT「先编译后执行」思想一致，只不过编译产物存在 delegate kernel 的 `user_data` 里。

#### 4.3.4 代码实践

**实践目标**：跟踪一次「宏节点 invoke」的调用链，确认它和普通 op 走的是同一条执行路径。

**操作步骤**：

1. 打开 [tensorflow/lite/core/subgraph.cc:1451-1452](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/core/subgraph.cc#L1451-L1452)，看 Subgraph 执行单个节点时的分发：`return referenced_registration->invoke(&context_, node);`。
2. 对比 [tensorflow/lite/delegates/gpu/delegate.cc:1454-1462](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/delegates/gpu/delegate.cc#L1454-L1462) GPU delegate 的 `.invoke`。

**需要观察的现象**：subgraph.cc:1452 对**所有**节点（普通 op 和 delegate 宏节点）一视同仁，都是 `registration->invoke(...)`。区别只在于：普通 op 的 `invoke` 指向 CPU kernel，delegate 宏节点的 `invoke` 指向 `DelegateKernel::Invoke`。

**预期结果**：你能回答「为什么说 delegate 对 Interpreter 的执行循环是透明的」——因为执行循环根本不需要知道某节点是不是 delegate 节点，它只管调 `invoke`。

#### 4.3.5 小练习与答案

**练习 1**：delegate 宏节点的 `init` 是在 `ModifyGraphWithDelegate` 调用时立即执行，还是延迟到第一次 `Invoke`？

**参考答案**：在 `ModifyGraphWithDelegate` 调用过程中就执行了。`ReplaceNodeSubsetsWithDelegateKernels` → `AddNodeWithParameters` 创建宏节点时会立即触发 `registration.init`（见 delegate.cc:1412-1427 的 lambda）。所以 GPU 子图的编译发生在挂载 delegate 时，而非首次推理时——这正是挂载 delegate 比「不挂直接跑」慢、但之后每次推理更快的原因。

**练习 2**：如果一个 `kTfPartition` 子集里有 5 个原始节点，合并后图里多了几个节点、少了几个节点？

**参考答案**：5 个原始节点被替换成 1 个 delegate 宏节点，所以**少了 4 个节点**（净减 4）。宏节点的输入/输出张量就是子集的 input_tensors / output_tensors，中间的临时张量被「吸收」进 delegate kernel 内部。

---

### 4.4 常见 delegate：GPU / NNAPI / XNNPACK 对比

#### 4.4.1 概念说明

虽然底层机制统一，但三个最常用的 delegate 定位截然不同：

| delegate | 后端 | 典型平台 | 擅长 | 是否需独立设备 |
| --- | --- | --- | --- | --- |
| **GPU delegate** | GPU（OpenCL/OpenGL/Metal） | Android/iOS | 浮点卷积/矩阵乘，高吞吐 | 是（GPU） |
| **NNAPI delegate** | 系统抽象的 NPU/DSP/GPU | Android 10+ | 量化模型、专用加速器（EdgeTPU 等） | 是（厂商加速器） |
| **XNNPACK delegate** | **CPU**（高度优化算子库） | 全平台 | CPU 上的浮点/量化推理，常作为默认加速 | 否（仍是 CPU） |

XNNPACK 看起来「特立独行」——它不用 GPU/NPU，为什么也算 delegate？因为它同样通过「认领节点 → 替换成自己的 kernel」机制工作，只不过它的 kernel 跑在 CPU 上、但比 TFLite 自带的 reference kernel 快得多。这印证了 **delegate 是一种「替换执行后端」的通用框架，不限于异构硬件**。

#### 4.4.2 核心流程

三个 delegate 的 `Prepare` 都遵循同一模式：「**判定可加速节点 → 调 ReplaceNodeSubsetsWithDelegateKernels**」，差别只在「如何判定」和「提供什么 registration」：

```text
所有 delegate 的 Prepare 模板：
  1. 构造一个 IsNodeSupportedFn（判断某节点能否加速）
  2. 用 GraphPartitionHelper（utils.h）跑分区，拿到可加速节点列表
  3. 构造自己的 TfLiteRegistration（delegate kernel）
  4. context->ReplaceNodeSubsetsWithDelegateKernels(context, registration, ops_to_replace, delegate)
```

`GraphPartitionHelper` 是 TFLite 提供给 delegate 作者的「分区助手」，把「遍历节点、判断支持、调 PreviewDelegatePartitioning 拿子集」这套样板封装好了，delegate 只需提供一个 `IsNodeSupportedFn` 回调。

#### 4.4.3 源码精读

先看分区助手 `GraphPartitionHelper`：

[tensorflow/lite/delegates/utils.h:95-204](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/delegates/utils.h#L95-L204) — 要点：

- 第 95-97 行：`IsNodeSupportedFn` 是个函数对象，签名 `(context, node, registration, unsupported_details) -> bool`。每个 delegate 自己实现这个判断逻辑。
- 第 101-105 行：`GraphPartitionHelper` 构造时接收这个回调。
- 第 125-130 行：`Partition()` 跑分区。
- 第 145-150 行：`GetNodesOfFirstNLargestPartitions(n)` 返回「前 n 个最大子集」的节点——用于实现 `max_delegated_partitions` 限制。

GPU delegate 怎么用这套机制判定可加速节点：

[tensorflow/lite/delegates/gpu/common/model_builder.cc:3291-3371](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/delegates/gpu/common/model_builder.cc#L3291-L3371) — `GetOpsToReplace`。第 3295-3352 行定义了一个 `node_supported_fn` lambda：先调 `IsSupported` 判断这个 op GPU 是否支持，再检查输入输出张量类型是否在允许列表内（第 3308-3350 行，默认只允许 float32/float16，开启 quant 才加 int8/uint8）。第 3354 行用这个 lambda 构造 `FP16GraphPartitionHelper`，第 3358 行跑分区，第 3369-3371 行只取最大的若干分区。

GPU delegate 的 `DelegatePrepare` 把这些串起来：

[tensorflow/lite/delegates/gpu/delegate.cc:1526-1567](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/delegates/gpu/delegate.cc#L1526-L1567) — 第 1542-1551 行调 `GetOpsToReplace` 拿到可加速节点列表，第 1552-1553 行调 `context->ReplaceNodeSubsetsWithDelegateKernels` 把它们替换成 GPU delegate kernel。注意第 1536-1540 行：若当前设备 OpenCL 不支持，会把 `Split`/`SplitV` 排除——这是 delegate **按运行时能力动态调整支持范围**的例子。

NNAPI delegate 的接口与 flags：

[tensorflow/lite/delegates/nnapi/nnapi_delegate.h:46-97](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/delegates/nnapi/nnapi_delegate.h#L46-L97) — `StatefulNnApiDelegate` 直接继承 `TfLiteDelegate`。`Options` 里有几项值得注意：`disallow_nnapi_cpu`（默认 true，因为 NNAPI 的 CPU 实现往往比 TFLite 自带 kernel 还慢）、`max_number_delegated_partitions`（默认 3，比 GPU 的 1 大）、`cache_dir`/`model_token`（编译缓存）。

[tensorflow/lite/delegates/nnapi/nnapi_delegate.cc:6564-6570](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/delegates/nnapi/nnapi_delegate.cc#L6564-L6570) — NNAPI 的 flags 设置：若 `allow_dynamic_dimensions` 则开 `kTfLiteDelegateFlagsAllowDynamicTensors`（对比 GPU 默认不开）。这正呼应了 4.1 里 flags 的语义——NNAPI 可以处理动态维度，GPU 默认不行。

XNNPACK delegate 的 Prepare 与 registration：

[tensorflow/lite/delegates/xnnpack/xnnpack_delegate.cc:7637-7673](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/delegates/xnnpack/xnnpack_delegate.cc#L7637-L7673) — `DelegatePrepare`：第 7641-7643 行调 `PrepareOpsToDelegate` 拿可加速节点，第 7651-7652 行调 `ReplaceNodeSubsetsWithDelegateKernels` 用 `kSubgraphRegistration`。注意 XNNPACK 还单独处理了一类「MoE」算子（7659-7671 行），逐个替换——这是它对特定算子的特殊处理。

[tensorflow/lite/delegates/xnnpack/xnnpack_delegate.cc:7626-7635](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/delegates/xnnpack/xnnpack_delegate.cc#L7626-L7635) — `kSubgraphRegistration`，`custom_name = "TfLiteXNNPackDelegate"`。注意它的 `init/prepare/invoke` 内部用的是 XNNPACK 自己的 `Subgraph` 类（不是 TFLite 的 Subgraph）来跑优化后的 CPU 算子。

#### 4.4.4 代码实践

**实践目标**：对比三个 delegate 的「支持范围」与「flags」，学会按场景选型。

**操作步骤**：

1. 打开 [tensorflow/lite/delegates/gpu/README.md:26-45](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/delegates/gpu/README.md#L26-L45)，列出 GPU delegate 支持的算子。
2. 对比 [tensorflow/lite/delegates/nnapi/nnapi_delegate.h:84-88](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/delegates/nnapi/nnapi_delegate.h#L84-L88) 关于 NNAPI CPU 的注释。

**需要观察的现象**：GPU README 明确支持浮点算子（ADD/CONV_2D/DEPTHWISE_CONV_2D/...），且强调「GPU 用 16/32 位浮点，不需要量化」；NNAPI 注释则坦言「NNAPI 的 CPU 实现通常比 TFLite 内置 kernel 还慢」，所以默认 `disallow_nnapi_cpu=true`。

**预期结果**：你能给出选型口诀——**浮点模型优先 GPU delegate；量化模型在 Android 上用 NNAPI 打 NPU/DSP；纯 CPU 平台用 XNNPACK；端云同构测试时 XNNPACK 还能当跨平台基准**。

#### 4.4.5 小练习与答案

**练习 1**：XNNPACK 跑在 CPU 上，为什么还要走 delegate 机制而不是直接替换 TFLite 自带 kernel？

**参考答案**：因为 TFLite 自带的算子注册（reference kernel）是「正确但未优化」的兜底实现，必须保证在所有平台可用。XNNPACK 通过 delegate 机制**有选择地**替换其中它能做得更好的部分，遇到不支持的算子自动回退到自带 kernel——既拿到了性能，又不破坏兼容性。这种「优化层叠加在兜底层之上」正是 delegate 框架的价值。

**练习 2**：GPU delegate 默认只取最大 1 个分区（`max_delegated_partitions=1`），这个保守策略的利弊是什么？

**参考答案**：利：避免「碎成多段、每段都要 GPU↔CPU 数据拷贝」导致开销反超收益；弊：若模型中间有个不支持的小算子，可能让 GPU 只接管一小段、放弃后面本可加速的一大段。用户可显式调大这个值做权衡。

---

### 4.5 失败回退策略：从 InvokeWithCPUFallback 到 RemoveAllDelegates

#### 4.5.1 概念说明

delegate 可能在两个时机失败，需要两套回退策略：

1. **Prepare 阶段失败**：delegate 在 `Prepare` 里发现编译不了（如 GPU 不支持某 op 的组合）、或运行时检测到不兼容（如不支持动态张量却有动态张量）。这种失败是「确定性的」，回退方式是 **撤销 delegate、恢复原始 CPU 执行计划**。

2. **Invoke 阶段失败**：delegate 宏节点在运行时跑挂了（如 GPU 驱动返回错误、显存不足）。这种失败是「运行时的」，需要更复杂的处理：把输入数据抢救出来、撤销所有 delegate、再用纯 CPU 重跑。

TFLite 对失败定义了一组清晰的状态码，让调用方能区分「可回退的 delegate 错误」和「不可恢复的致命错误」。

#### 4.5.2 核心流程

Prepare 阶段的回退（自动）：

```text
ModifyGraphWithDelegateImpl
  └─ STEP 2 调 delegate->Prepare 失败
        └─ reset_delegation_if_not_ok(status)
              └─ RemoveAllDelegates()  // 用 pre_delegation_execution_plan_ 恢复
              └─ 返回 kTfLiteDelegateError
```

Invoke 阶段的回退（需调用方主动用 `InvokeWithCPUFallback`）：

```text
InterpreterUtils::InvokeWithCPUFallback(interpreter)
  ├─ status = interpreter->Invoke()
  ├─ 若 kTfLiteOk / 已取消 / 无 delegate → 直接返回
  ├─ 否则（delegate 运行时失败）：
  │     1. 把所有输入张量数据拷到临时 buffer（抢救输入）
  │     2. RemoveAllDelegates()        // 撤销全部 delegate
  │     3. 把输入数据从 buffer 拷回
  │     4. interpreter->Invoke()       // 纯 CPU 重跑
  │     └─ 返回 kTfLiteDelegateError（表示「delegate 挂了，但 CPU 兜底成功」）
```

状态码语义（来自 interpreter.h `ModifyGraphWithDelegate` 的文档）：

- `kTfLiteOk`：成功。
- `kTfLiteDelegateError`：delegate 自身出错（如编译失败），Interpreter 已**恢复到委托前状态**，可继续用 CPU 跑。
- `kTfLiteApplicationError`：与运行时不兼容（如图已冻结不可再委托），Interpreter 仍可调用但本次委托未生效。
- `kTfLiteError`：意外/致命错误，输出无效。

#### 4.5.3 源码精读

Prepare 阶段失败回退的代码：

[tensorflow/lite/core/subgraph.cc:2496-2505](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/core/subgraph.cc#L2496-L2505) — `reset_delegation_if_not_ok` 闭包：只要 STEP 2/3 的状态不是 `kTfLiteOk`，立即 `RemoveAllDelegates()` 恢复原始计划并报错「Restored original execution plan after delegate application failure.」。注意它「撤销后仍返回 `kTfLiteDelegateError` 而非 `kTfLiteOk`」——这样调用方能知道「虽然没崩，但 delegate 没生效」。

Invoke 阶段失败回退的工具函数：

[tensorflow/lite/delegates/interpreter_utils.cc:29-71](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/delegates/interpreter_utils.cc#L29-L71) — `InvokeWithCPUFallback`。逐段看：

- 第 30-34 行：先正常 `Invoke()`，成功或无 delegate 就直接返回。
- 第 41-56 行：**抢救输入数据**——把所有输入张量的字节拷进一个临时 `buf`。注释（第 42-43 行）解释了为什么安全：`ArenaPlanner` 用 `preserve_inputs=true`，输入数据在 `RemoveAllDelegates` 后仍可读。
- 第 58 行：`RemoveAllDelegates()`——撤销**全部** delegate（这是「all-or-nothing」的代价）。
- 第 60-66 行：把输入数据从 `buf` 拷回（因为撤销 delegate 可能重组了张量内存布局）。
- 第 69 行：纯 CPU 再 `Invoke()` 一次。
- 第 70 行：返回 `kTfLiteDelegateError`——明确告诉调用方「delegate 失败但已 CPU 兜底，输出有效」。

这个函数的头文件注释清楚说明了它的适用前提：

[tensorflow/lite/delegates/interpreter_utils.h:27-45](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/delegates/interpreter_utils.h#L27-L45) — 「允许回退仅适用于：调用方不在多次 Invoke 间缓存张量数据指针；且模型无状态（无变量、无 LSTM）或状态跨 batch 不需要」。这两条限制的原因是：`RemoveAllDelegates` 会重组内存、丢掉 delegate 内部积累的状态。

#### 4.5.4 代码实践

**实践目标**：理解两类失败的本质差异，掌握在应用层如何正确处理。

**操作步骤**：

1. 阅读上面的两个源码段，在笔记里画一张「失败时机 → 回退方式 → 返回码」对照表。
2. 思考一个具体场景：你给模型挂了 GPU delegate，但在某台老旧设备上 GPU 驱动有 bug，Invoke 时段错误被 delegate 内部捕获并返回失败。

**需要观察的现象**：Prepare 阶段失败是「自动回退」的（ModifyGraphWithDelegate 内部就处理了）；Invoke 阶段失败**不会自动回退**——`interpreter->Invoke()` 只会返回错误码，要不要 `InvokeWithCPUFallback` 由调用方决定。

**预期结果**：你能说出生产环境推荐模式——「挂载 delegate 后检查返回码，若是 `kTfLiteDelegateError` 就当没挂、纯 CPU 跑；若坚持要用且能接受有状态模型限制，则用 `InvokeWithCPUFallback` 包裹每次推理」。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `InvokeWithCPUFallback` 要在 `RemoveAllDelegates` **之前**先把输入数据拷到 `buf`？

**参考答案**：因为 `RemoveAllDelegates` 会恢复原始执行计划、重新规划张量内存（ArenaPlanner 重新分配），这会让原输入张量的数据指针失效或被覆盖。所以必须先把输入抢救到独立的 `buf`，撤销并重组内存后再拷回去。

**练习 2**：一个有 LSTM（带状态）的模型适合用 `InvokeWithCPUFallback` 吗？为什么？

**参考答案**：不适合。LSTM 在跨 batch 间维护隐藏状态，而 `RemoveAllDelegates` 会丢掉 delegate 内部积累的这些状态，回退后的纯 CPU 重跑相当于「状态归零」，结果会错。interpreter_utils.h:34-36 的文档明确把「模型无状态」列为使用前提之一。

---

## 5. 综合实践

把本讲五个模块串起来，完成一个「**用 Python 给 TFLite Interpreter 挂载外部 delegate 并观察分区与回退**」的综合任务。

### 背景

TFLite 的 Python API 通过 `tf.lite.Interpreter` 的 `experimental_delegates` 参数接入 delegate，`tf.lite.experimental.load_delegate` 负责从 `.so` 动态库加载一个 delegate。

### 步骤

1. **阅读用户接入入口**：打开 [tensorflow/lite/python/interpreter.py:138-182](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/python/interpreter.py#L138-L182)，读 `load_delegate` 的文档与示例。它把一个共享库（如 `libdelegate.so`）包装成 `Delegate` 对象。

2. **阅读挂载点**：在同一个文件搜 `experimental_delegates`（约 554-557 行），确认 Interpreter 构造时会遍历 delegates 并调 `self._interpreter.ModifyGraphWithDelegate(...)`——这正是本讲 4.2 讲的入口。

3. **写一段示例代码（示例代码）**：

   ```python
   # 示例代码：演示如何挂载一个外部 delegate
   import tensorflow as tf

   # 1. 尝试加载 delegate（这里用 GPU delegate 的 .so 名做示意）
   try:
       delegate = tf.lite.experimental.load_delegate('libGpuDelegate.so')
       interpreter = tf.lite.Interpreter(
           model_path='model.tflite',
           experimental_delegates=[delegate])
       print('delegate 挂载成功，将走加速后端')
   except (ValueError, RuntimeError):
       # 回退：加载失败就纯 CPU
       interpreter = tf.lite.Interpreter(model_path='model.tflite')
       print('delegate 不可用，回退纯 CPU')

   interpreter.allocate_tensors()
   # ... 填输入、invoke、取输出 ...
   ```

4. **把这段代码对照本讲源码画调用链**：`load_delegate` → `Interpreter.__init__(experimental_delegates=...)` → `_interpreter.ModifyGraphWithDelegate` →（C++ Subgraph）`ModifyGraphWithDelegateImpl` → `SwitchToDelegateContext` + `delegate->Prepare` → `ReplaceNodeSubsetsWithDelegateKernels` → `PartitionGraph` → 重建 `execution_plan_`。

5. **观察分区（待本地验证）**：在有 GPU 的 Android 设备上，用 `TfLiteGpuDelegateV2Create` 挂 GPU delegate 后，开启 verbose 日志（`TFLITE_LOG_VERBOSE`）。对照 [tensorflow/lite/core/subgraph.cc:578-584](https://github.com/tensorflow/tensorflow/blob/7749c6197a553299f76f3ca02bc92a4986de70f2/tensorflow/lite/core/subgraph.cc#L578-L584) 那条 `TFLITE_LOG_PROD` 日志，你会看到形如「Replacing X out of Y node(s) with delegate (...) yielding Z partitions」的输出——**X 是被认领的节点数、Y 是总节点数、Z 是分区数**。据此回答：你的模型有多少节点被 GPU 接管、切成了几段。

### 预期结果

你能完整复述：用户在 Python 传一个 `.so` → 它如何变成 `TfLiteDelegate*` → 运行时如何分区改图 → 推理时宏节点如何在加速器上跑 → 万一失败如何回退 CPU。这就是 delegate 机制的端到端闭环。

> ⚠️ 步骤 5 需要真实 Android 设备与 GPU delegate 库，在普通 Linux 开发机上无法运行，标注为「待本地验证」。即便无法运行，步骤 1-4 的源码阅读与调用链绘制也可独立完成。

## 6. 本讲小结

- **delegate 是「替换执行后端」的通用插件机制**：通过 `TfLiteDelegate` 结构体的 `Prepare` 函数指针，让 GPU/NNAPI/XNNPACK 等后端认领并执行图中的部分节点。它和普通 op 不同——是用户在运行期显式挂载，而非启动期自动注册。
- **改图分三步**：`ModifyGraphWithDelegateImpl` 先验证并留底原始计划（`pre_delegation_execution_plan_`），再回调 `delegate->Prepare`，最后保持图状态一致；任一步失败都用 `RemoveAllDelegates` 回滚。
- **分区是核心**：`ReplaceNodeSubsetsWithDelegateKernels` 调 `PartitionGraphIntoIndependentNodeSubsets` 把图切成交替的 `kTfPartition`（delegate 段）与 `kTfNonPartition`（CPU 段），把每个 delegate 段合并成一个宏节点。
- **宏节点仍是普通算子**：它持有一份 `TfLiteRegistration`（`init/free/prepare/invoke`），`builtin_code = DELEGATE`。`init` 里做昂贵的子图编译，`invoke` 里在加速器上执行——对 Interpreter 执行循环完全透明。
- **三大 delegate 定位不同**：GPU 擅长浮点卷积、NNAPI 打 Android NPU/DSP、XNNPACK 是优化的 CPU 库；都遵循「`IsNodeSupportedFn` + `GraphPartitionHelper` + `ReplaceNodeSubsetsWithDelegateKernels`」同一 Prepare 模板。
- **两类失败两套回退**：Prepare 失败自动 `RemoveAllDelegates` 恢复 CPU 计划（返回 `kTfLiteDelegateError`）；Invoke 失败需调用方主动用 `InvokeWithCPUFallback`——先抢救输入、撤销 delegate、纯 CPU 重跑，但有「无状态模型」前提。

## 7. 下一步学习建议

本讲是 u8「边缘部署 TFLite」单元的最后一篇，把 TFLite 的运行时机制（Interpreter → OpResolver → delegate）补全了。建议的后续方向：

- **阅读 `tensorflow/lite/delegates/gpu/common/model_builder.cc`**：完整看一遍 GPU delegate 如何把 TFLite 子图翻译成它的 `GraphFloat32` 内部表示，这是写 delegate 最有参考价值的范本。
- **通读 `tensorflow/lite/delegates/utils.h` 的 `GraphPartitionHelper` 实现**：如果你想自己写一个 delegate（如为某款自研 NPU），这个助手类是起点，配合 `simple_delegate`（在 `tensorflow/lite/tools/delegates/` 下的示例）能搭出最小可用 delegate。
- **回到主线对比 XLA/JIT**：u7-l3 讲的 XLA 自动聚类与本讲的 delegate 分区都是「把子图交给专用编译/执行后端」的思想，对照阅读能加深对「编译期与执行期分离」架构模式的理解。
- **进入 u9 扩展与二次开发**：若你想在 C++ 层做更深的定制（如自定义 op、profiler），u9 单元会从 `examples/adding_an_op` 等示例展开。
