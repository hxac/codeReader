# TFLite 委托机制 delegates

> 本讲承接 u8-l2（FlatBuffer 模型格式与 OpResolver）。上一讲我们讲清楚了「模型里的算子如何经 OpResolver 找到 CPU kernel」，本讲回答它的下一个自然问题：**如果设备上有 GPU、NPU、DSP 等更强的算力，TFLite 怎么把计算交给它们？** 答案就是 **delegate（委托）**。

---

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清 **delegate 是什么**：它不是一个 kernel，而是一套「把一段子图整体替换成一个宏算子、交给加速后端执行」的协议。
2. 画出 **图分区（graph partitioning）** 的过程：为什么一张图会被切成「可加速子图 + 不可加速 CPU 子图」交替的若干段。
3. 区分 **两种失败回退**：apply 期（委托生效时）失败会整图回滚，运行期（Invoke 时）失败会自动退回 CPU 再跑一遍。
4. 对比 **GPU / NNAPI / XNNPACK** 三类常见委托后端的适用场景与编程入口。

---

## 2. 前置知识

本讲假设你已经掌握（详见 u8-l1、u8-l2）：

- **Interpreter 的执行计划 `execution_plan_`**：一张图加载后被展开成一条按序执行的算子序列，`Invoke` 就是逐个调用每个算子的 `invoke` 函数指针。
- **`TfLiteRegistration`**：一个算子的「身份证 + 函数指针集合」（`init/prepare/invoke/free`），由 OpResolver 从 FlatBuffer 算子码翻译而来。
- **`TfLiteContext`**：运行时传给算子的「工具箱」，算子通过它读写张量、上报错误。

补充两个本讲要用的新概念：

- **宏算子（macro-op / DELEGATE op）**：一个代表「整段被委托子图」的特殊算子，它的 `builtin_code` 被标记为 `BuiltinOperator_DELEGATE`。对运行时而言，它和普通算子一样排在执行计划里、同样有 `invoke`，只是它的 `invoke` 内部跑的是整段子图而非单个 op。
- **后端（backend）**：真正干活的硬件/库，如 GPU（OpenCL/Vulkan）、NNAPI（Android 厂商加速器）、XNNPACK（高度优化的 CPU 库）、Hexagon（高通 DSP）、CoreML（Apple）。Delegate 是「TFLite 运行时 ↔ 后端」之间的适配层。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `tensorflow/lite/core/c/common.h` | 定义委托协议核心：`TfLiteDelegate` 结构体、`TfLiteDelegateFlags` 位标志、`TfLiteDelegateParams`（一段被委托子图的输入输出清单）。 |
| `tensorflow/lite/core/interpreter.{h,cc}` | `Interpreter::ModifyGraphWithDelegate` / `RemoveAllDelegates` 的对外入口与跨子图分发。 |
| `tensorflow/lite/core/subgraph.cc` | 委托的真正主战场：`ModifyGraphWithDelegateImpl`（应用委托三步走）、`ReplaceNodeSubsetsWithDelegateKernels`（执行计划改写）、`SwitchToDelegateContext`（上下文切换）、`RemoveAllDelegates`（回滚）。 |
| `tensorflow/lite/graph_info.{h,cc}` | 图分区算法 `PartitionGraphIntoIndependentNodeSubsets`：把执行计划切成 `NodeSubset` 序列。 |
| `tensorflow/lite/delegates/interpreter_utils.{h,cc}` | 运行期自动 CPU 回退工具 `InvokeWithCPUFallback`。 |
| `tensorflow/lite/delegates/gpu/common/model_builder.cc` | GPU 委托的 `DelegatePrepare`，是「委托如何声明支持哪些节点」的范本。 |
| `tensorflow/lite/delegates/gpu/delegate.h` | GPU 委托的 C API（`TfLiteGpuDelegateV2Create`）。 |
| `tensorflow/lite/delegates/nnapi/nnapi_delegate.h` | NNAPI 委托的 C++ 接口 `StatefulNnApiDelegate` 及其 `Options`。 |
| `tensorflow/lite/delegates/xnnpack/xnnpack_delegate.cc` | XNNPACK 委托的 `DelegatePrepare`（常作为默认委托被自动应用）。 |
| `tensorflow/lite/delegates/utils/simple_delegate.h` | 写自定义委托的高层脚手架 `SimpleDelegateInterface`。 |
| `tensorflow/lite/python/interpreter.py` | Python 侧加载外部委托 `load_delegate` / `Delegate`。 |

---

## 4. 核心概念与源码讲解

### 4.1 委托是什么：把子图卸载到加速后端（lite.delegates）

#### 4.1.1 概念说明

TFLite 默认在 CPU 上逐算子解释执行。但很多设备有更强的算力：手机 GPU、Android 的 NPU/DSP、Apple 的 Neural Engine。问题是——这些后端各有各的 API、各自只支持一部分算子、而且通常**只在大段连续计算时才有收益**（单算子卸载的开销反而大于收益）。

Delegate（委托）就是为解决这件事而设计的协议。它的核心思想可以一句话概括：

> **delegate 告诉运行时「我能接管这些节点」，运行时就把这些连续节点打包成一个宏算子，由 delegate 自己负责这一整段的编译与执行；其余节点照旧在 CPU 上跑。**

所以 delegate 不是「替换某一个 kernel」，而是「替换一整段子图」。这带来三个关键设计后果：

1. **选择性卸载**：delegate 只需支持它能支持的算子，不支持的留在 CPU，两者能共存于同一张图。
2. **连续性收益**：相邻的支持算子会被合并成尽量大的段，最大化融合与减少跨后端拷贝。
3. **统一的「DELEGATE 宏算子」抽象**：无论后端是 GPU 还是 NPU，对运行时而言都是执行计划里一个 `invoke` 即可的宏算子，运行时主循环无需为每种后端改写。

#### 4.1.2 核心流程

一个 delegate 从「被创建」到「真正加速推理」要经过三幕：

```
第一幕：创建委托对象
   用户调用 TfLiteGpuDelegateV2Create() / StatefulNnApiDelegate() / load_delegate()
   得到一个 TfLiteDelegate*（含 data_、Prepare、flags）

第二幕：应用委托  ModifyGraphWithDelegate(delegate)
   1. 运行时校验：委托是否支持动态形状？图里有没有动态张量？
      不支持动态形状 + 图有动态张量 → 直接拒绝（kTfLiteApplicationError）
   2. 备份原始执行计划 → pre_delegation_execution_plan_（为回滚留底）
   3. SwitchToDelegateContext()：把 ReplaceNodeSubsetsWithDelegateKernels 等
      委托专用函数挂进 TfLiteContext，让委托能调用它们
   4. 调 delegate->Prepare(context, delegate)
      委托扫描全图，挑出「我支持的节点」nodes_to_replace，
      调 context->ReplaceNodeSubsetsWithDelegateKernels(...)
      → 运行时执行【图分区】，把执行计划改写成 CPU段 / DELEGATE宏算子 交替
   5. SwitchToKernelContext()：撤下委托专用函数（普通 kernel 不能再乱改图）
   6. 图进入 kStateInvokableAndImmutable（不可变）状态

第三幕：推理  Invoke()
   执行计划里逐个跑：CPU 算子照旧；遇到 DELEGATE 宏算子 → 调它的 invoke
   → 该段在 GPU/NPU 上跑完，结果写回张量
```

注意第二幕第 6 步的「不可变」：大多数委托在编译时就需要固定形状，因此应用委托后图会被冻结，不能再改输入尺寸。这就是为什么**委托必须在 `AllocateTensors()` 之前、且只能在输入形状确定后应用一次**。

#### 4.1.3 源码精读

委托协议的核心是 `common.h` 里的 `TfLiteDelegate` 结构体——它本质是一组函数指针 + 一份位标志：

[common.h:1408-1448](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/c/common.h#L1408-L1448) — 定义委托的「身份证 + 能力声明」。关键字段：

- `data_`：委托自用的不透明状态指针（如 GPU 编译出的模型、NNAPI 的 `ANeuralNetworksCompilation`），生命周期由委托自己管理。
- `Prepare(TfLiteContext*, TfLiteDelegate*)`：**最核心的方法**。被 `ModifyGraphWithDelegate` 调用，委托在这里浏览全图并调用 `ReplaceNodeSubsetsWithDelegateKernels()` 把自己能接管的节点替换成宏算子。
- `CopyFromBufferHandle` / `CopyToBufferHandle` / `FreeBufferHandle`：当委托使用自己的硬件缓冲区（如 OpenGL 纹理）时，这三者负责在「硬件缓冲 ↔ 普通 CPU 张量」之间搬运数据。不用硬件缓冲的委托可置空。
- `flags`：位掩码能力声明，见 `TfLiteDelegateFlags`。

位标志的含义见 [common.h:1356-1405](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/c/common.h#L1356-L1405)：

| 标志 | 值 | 含义 |
|------|----|------|
| `kTfLiteDelegateFlagsNone` | 0 | 无特殊能力 |
| `kTfLiteDelegateFlagsAllowDynamicTensors` | 1 | 委托能处理动态尺寸张量（否则图被冻结为不可变） |
| `kTfLiteDelegateFlagsRequirePropagatedShapes` | 2 | 要求运行时在张量 resize 时自动传播形状到委托 kernel 的 I/O 张量（依赖标志 1） |
| `kTfLiteDelegateFlagsPerOperatorProfiling` | 4 | 按 op 粒度而非「整段委托」粒度做 profiling |
| `kTfLiteDelegateFlagsHintFullyDelegatedToSingleDelegate` | 8 | 提示整图会被单一委托全包，可跳过部分分配 |

而 `ReplaceNodeSubsetsWithDelegateKernels` 改写执行计划时，会为每一段被委托的子图构造一个 `TfLiteDelegateParams` 作为该宏算子的「参数包」：

[common.h:835-840](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/c/common.h#L835-L840) — 一段被委托子图的清单：它由哪些节点组成（`nodes_to_replace`）、跨段边界的输入张量（`input_tensors`）、输出张量（`output_tensors`）。这份清单会被原样塞进 DELEGATE 宏算子的 `builtin_data`，让委托 kernel 在 `invoke` 时知道该算什么。

#### 4.1.4 代码实践

**实践目标**：在源码中亲手确认「委托的本质是一组函数指针 + 一段被替换的子图」，并理解三个回调各自的职责。

**操作步骤**：

1. 打开 [common.h:1408-1448](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/c/common.h#L1408-L1448)，找到 `struct TfLiteDelegate`，数一数它有几个函数指针字段（`Prepare`、`CopyFromBufferHandle`、`CopyToBufferHandle`、`FreeBufferHandle`）。
2. 打开 [simple_delegate.h:77-117](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/delegates/utils/simple_delegate.h#L77-L117)，对照高层封装 `SimpleDelegateInterface`：注意它要求实现的 `IsNodeSupportedByDelegate`、`Name`、`CreateDelegateKernelInterface`、`DelegateOptions` 四个方法，正好对应「声明支持谁 / 我叫什么 / 给我一个 kernel / 分区参数」。
3. 思考：`Prepare` 与 `SimpleDelegateKernelInterface::Init/Prepare/Eval` 的关系——前者负责「圈地」（声明支持哪些节点、触发分区），后者负责「在这块地上干活」（编译 + 执行）。

**需要观察的现象 / 预期结果**：你会确认委托的「协议」非常薄——运行时只认 `Prepare` 这一个入口，其余全靠委托自己回调 `context->ReplaceNodeSubsetsWithDelegateKernels`。这正是它能跨「GPU/NNAPI/XNNPACK」统一的原因。

> 待本地验证：若你有 Android/桌面 GPU 环境，可用本讲 4.4.4 的 Python 示例实际创建一个 GPU 委托并打印分区结果；本 CI 环境无 GPU，故此处为源码阅读型实践。

#### 4.1.5 小练习与答案

**练习 1**：委托的 `flags` 标志位 `kTfLiteDelegateFlagsAllowDynamicTensors` 没设（为 0），会对应用流程产生什么影响？
**答案**：运行时会在应用委托前先 `PrepareOpsStartingAt` 探测图是否含动态张量；若含动态张量则直接拒绝委托并返回 `kTfLiteApplicationError`；若不含，则应用后把图置为 `kStateInvokableAndImmutable`（不可变），后续不能再 resize 张量。

**练习 2**：为什么 `ReplaceNodeSubsetsWithDelegateKernels` 要把整段子图打包成「一个」宏算子，而不是给每个被支持的节点各换一个委托 kernel？
**答案**：因为整段连续计算在后端上可以融合、复用缓冲区，收益远大于逐算子卸载；逐算子卸载还会引入大量 CPU↔后端 的边界拷贝，得不偿失。分区算法（4.2）正是为「尽量合并连续支持节点」而设计。

---

### 4.2 图分区：从执行计划到 NodeSubset

#### 4.2.1 概念说明

委托说「我支持节点 {2,3,5,6,7}」，但执行计划是线性的 `{0,1,2,3,4,5,6,7}`，其中 0/1/4 不被支持。运行时要把它改写成可执行的形式：

```
原始:  [0][1][2][3][4][5][6][7]      （[]内是节点 id）
支持集:        [2][3]   [5][6][7]
改写后执行计划:
  ┌─── CPU 段 ───┐┌─ 委托段 ─┐┌CPU┐┌─── 委托段 ───┐
  [0][1]           [2,3](宏)   [4]  [5,6,7](宏)
```

也就是说，运行时把执行计划切成一串 **NodeSubset（节点子集）**，每个子集要么全是 CPU 节点（`kTfNonPartition`，原样保留），要么全是被委托节点（`kTfPartition`，合并成一个 DELEGATE 宏算子）。两种子集交替出现，数据在边界处通过张量传递（可能伴随 CPU↔后端 拷贝）。

#### 4.2.2 核心流程

分区由 `ReplaceNodeSubsetsWithDelegateKernels` 触发，核心算法是 `graph_info.cc` 的 **epoch（纪元）遍历**：

```
为每个节点打标记 node_type_[i]:
   在 nodes_to_replace 中 → kTfPartition；否则 → kTfNonPartition

按执行顺序遍历，维护「当前 subset」与 tensor 的产出纪元 tensor_epochs_[t]:
   每轮循环 = 一个 epoch = 一个新 NodeSubset:
     尽量把「所有输入都已就绪 且 类型与当前 subset 相同」的节点并入当前 subset
     遇到类型不同的节点 → 当前 subset 收尾，开下一个 subset（类型翻转）

结果: 一串类型交替的 NodeSubset
```

用形式化的说法，张量 \( t \) 的产出纪元记为 \( \text{epoch}(t) \)，则一个跨段边界张量必须满足：

\[ \text{epoch}(t_{\text{产出}}) \neq \text{epoch}(t_{\text{消费}}) \]

这类张量既是上一段的输出、又是下一段的输入，会被同时登记进两个相邻 subset 的 `output_tensors` / `input_tensors`，正是 4.1.3 里 `TfLiteDelegateParams` 的来源。

#### 4.2.3 源码精读

改写执行计划的主入口在 `Subgraph::ReplaceNodeSubsetsWithDelegateKernels`：

[subgraph.cc:521-615](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/subgraph.cc#L521-L615) — 这段代码做了四件事：

1. 给传入的 `registration` 打上 `builtin_code = BuiltinOperator_DELEGATE` 标记（L525），宣告「这是个宏算子，不是普通 op」。
2. 空集直接返回（L564-L566）——委托声明「一个节点都不支持」是合法的，此时图原样不动。
3. 调 `PartitionGraph(nodes_to_replace, &node_subsets)` 做分区（L570-L573）。
4. **清空并重建 `execution_plan_`**（L586 起）：遍历每个 subset，`kTfNonPartition` 的节点原样 `push_back` 回执行计划；`kTfPartition` 的节点用 `AddNodeWithParameters` 新增**一个** DELEGATE 宏算子（其参数即 4.1.3 的 `TfLiteDelegateParams`）。

`NodeSubset` 的定义很简洁：

[graph_info.h:76-93](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/graph_info.h#L76-L93) — 三类 `Type`（`kTfUnexplored` 仅构造期用、`kTfPartition`、`kTfNonPartition`），外加 `nodes`（成员节点）、`input_tensors`（来自别的 subset 或全局输入的跨段张量）、`output_tensors`（被别的 subset 消费或作为全局输出的张量；既非输入又非输出的中间张量可被省略）。

分区算法本体在 `PartitionGraphIntoIndependentNodeSubsetsImpl`：

[graph_info.cc:90-123](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/graph_info.cc#L90-L123) — `Partition()` 的主循环「每轮造一个 subset，直到造出空 subset 为止」。构造期先初始化两个特殊纪元：`kEpochAlwaysReady`（−2，表示常量/全局输入，永远就绪）和 `kEpochNotReady`（−1，表示尚未产出）。`UpdateNode`（[graph_info.cc:171 起](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/graph_info.cc#L171)）负责判断一个节点能否并入当前 subset——要求其输入张量全部已就绪，且节点类型与当前 subset 一致；不一致就触发新 subset。

把委托挂载到图上的「三步走」在 `Subgraph::ModifyGraphWithDelegateImpl`：

[subgraph.cc:2485-2558](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/subgraph.cc#L2485-L2558) — 对应 4.1.2 的三幕：

- **STEP 1（校验准备，L2507-L2548）**：若委托不支持动态形状，先 `PrepareOpsStartingAt` 探测；有动态张量则返回 `kTfLiteApplicationError`。首次应用委托时把 `execution_plan_` 备份到 `pre_delegation_execution_plan_`（L2547）——这是回滚的「存档点」。
- **STEP 2（委托接管，L2554-L2558）**：`SwitchToDelegateContext()` 把 `ReplaceNodeSubsetsWithDelegateKernels` 挂进 context，调 `TfLiteDelegatePrepareInternal(&context_, delegate)`（最终触发 `delegate->Prepare`），再 `SwitchToKernelContext()` 撤下。

`SwitchToDelegateContext` / `SwitchToKernelContext` 的「挂上/摘下」机制值得注意：

[subgraph.cc:2127-2158](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/subgraph.cc#L2127-L2158) — 委托上下文开启时，context 上的 `ReplaceNodeSubsetsWithDelegateKernels`、`GetExecutionPlan` 等函数指针指向真实实现；切换回 kernel 上下文后，这些指针被替换成 `ForbiddenContextFunction`（一调就报错）。这是一种**能力收窄**设计：普通算子 kernel 在 `Compute`/`invoke` 期间根本不该再去改图，所以直接把它禁掉。

#### 4.2.4 代码实践

**实践目标**：用一个最小的人造图，手动模拟分区算法的输出，从而真正理解 `NodeSubset` 的交替。

**操作步骤**：

1. 假设有执行计划节点 `[A,B,C,D,E,F]`，其中委托声明支持 `{B, C, E}`（不支持 A、D、F）。
2. 按 4.2.2 的算法手算：
   - subset 0（CPU）：A
   - subset 1（委托）：B, C
   - subset 2（CPU）：D
   - subset 3（委托）：E
   - subset 4（CPU）：F
3. 写出每个 subset 的 `type`，以及 B 的输入（来自 A，故 A→B 的张量是 subset0 的输出、subset1 的输入）这类边界张量。
4. 对照 [delegate_test.cc](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/delegates/delegate_test.cc) 与 [graph_info_test.cc:127](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/graph_info_test.cc#L127) 中 `PartitionGraphIntoIndependentNodeSubsets` 的断言，验证你的手算结果与测试预期一致。

**预期结果**：你会得到 5 个交替的 subset（CPU/委托/CPU/委托/CPU），共 2 个 DELEGATE 宏算子。关键观察是——**委托段的数量 = 支持集被「不支持节点」切成的连续块数**，这正是「连续性收益」的几何体现。

> 待本地验证：可改 `graph_info_test.cc` 的输入构造一个相同的人造图，编译运行该测试观察 `node_subsets` 的实际内容。

#### 4.2.5 小练习与答案

**练习 1**：如果一张图里所有节点都被委托支持，分区结果是什么？执行计划变成几个宏算子？
**答案**：只有一个 `kTfPartition` subset，整张图被合并成**一个** DELEGATE 宏算子。这也是 `kTfLiteDelegateFlagsHintFullyDelegatedToSingleDelegate`（4.1.3）成立的前提——运行时在 [subgraph.cc:2560](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/subgraph.cc#L2560) 会用 `IsFullyDelegated()` 校验。

**练习 2**：为什么分区算法强调「independent（独立）」node subsets？
**答案**：每个 subset 只通过显式的 `input_tensors`/`output_tensors` 与相邻 subset 交换数据，子集之间没有隐式耦合。这样每个 DELEGATE 宏算子都能被独立编译/执行，跨段边界只需处理边界张量，保证委托 kernel 的自洽性。

---

### 4.3 失败回退：apply 期回滚与运行期 CPU fallback（lite.delegates）

#### 4.3.1 概念说明

委托不是万能的，失败可能发生在两个截然不同的时刻，TFLite 对它们的处理策略也不同：

1. **应用期失败（apply-time）**：委托在 `Prepare` 阶段就发现自己搞不定（比如编译失败、显存不足）。此时推理还没开始，运行时可以**整图回滚**到委托应用前的状态，就像委托从没来过——用户最多看到「委托未生效」，不会得到错误结果。
2. **运行期失败（runtime/invoke-time）**：委托在应用时一切正常，但在某次 `Invoke` 时后端出错（GPU 驱动崩溃、NPU 超时）。此时用户已经调用了 `Invoke` 并期待结果。TFLite 提供可选的 **CPU 自动回退**：把委托撤掉、用 CPU 把这一帧重算一遍，把正确结果交还给用户。

理解两者的区别是本模块的核心：「**应用期回滚是运行时内置的、无条件的；运行期 CPU 回退是可选的、有代价的、有前提的**」。

#### 4.3.2 核心流程

**应用期回滚**（无条件，内置）：

```
Interpreter::ModifyGraphWithDelegateImpl 对每个子图调 Subgraph::ModifyGraphWithDelegate
   若返回 kTfLiteDelegateError → 调 RemoveAllDelegates() 恢复原图 → 整体返回该错误
Subgraph 内部还有更细的 reset_delegation_if_not_ok：
   任何一步失败 → RemoveAllDelegates + 报错 → 返回 kTfLiteDelegateError
```

**运行期 CPU 回退**（可选，需显式调用 `InvokeWithCPUFallback`）：

```
InterpreterUtils::InvokeWithCPUFallback(interpreter):
   status = interpreter->Invoke()
   if (status==OK || 已取消 || 根本没委托)  → 直接返回 status
   否则（委托相关的运行期失败）:
      1. 把当前输入张量数据拷到一块临时 buffer（CPU 回退要重算，输入得留着）
      2. interpreter->RemoveAllDelegates()  （撤掉所有委托，恢复 CPU 执行计划）
      3. 把输入数据从 buffer 拷回张量
      4. interpreter->Invoke()  （纯 CPU 再跑一遍，拿到正确结果）
      5. 返回 kTfLiteDelegateError  ← 注意：不是 OK，提醒调用方「这次是 CPU 兜底的」
```

#### 4.3.3 源码精读

应用期回滚的入口在 `Interpreter::ModifyGraphWithDelegateImpl`：

[interpreter.cc:401-423](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/interpreter.cc#L401-L423) — 逐子图应用委托；一旦某子图返回 `kTfLiteDelegateError`（L419），立即 `RemoveAllDelegates()` 把所有子图都恢复到委托前的执行计划，再把这个错误向上抛。注意它**跳过**校验子图（`IsValidationSubgraph`）和可跳过的子图（L405-L411）——这些子图不该被委托。

`RemoveAllDelegates` 的真正实现：

[subgraph.cc:2282-2288](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/subgraph.cc#L2282-L2288) — 三步：`UndoAllDelegates()`（把执行计划从 `pre_delegation_execution_plan_` 还原，正是 4.2.3 STEP1 留的存档点）、清空 `delegates_applied_`、`EnsureMemoryAllocations()` 重新规划内存。回滚之所以可行，全靠 4.2.3 中那句 `pre_delegation_execution_plan_ = execution_plan_` 的备份。

运行期 CPU 回退的实现在 `delegates/interpreter_utils.cc`：

[interpreter_utils.cc:29-71](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/delegates/interpreter_utils.cc#L29-L71) — 逐行对应 4.3.2 的伪代码。两个细节值得注意：

- L31-L34：只有「失败且图上确实有委托」才需要回退；没委托的失败是真正的运行时错误，不该重试。
- L47-L56：先把所有输入张量的字节流拷进 `buf`。注释点明输入数据是安全的，因为 `ArenaPlanner` 用了 `preserve_inputs=true` 不会覆盖输入区——这保证了回退后重算时输入仍然正确。
- L70：返回 `kTfLiteDelegateError` 而非 `kTfLiteOk`，**结果有效但调用方应知晓发生过回退**。

这个工具的契约与限制写在头文件里：

[interpreter_utils.h:25-46](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/delegates/interpreter_utils.h#L25-L46) — 明确警告两条前提：（1）调用方不能跨 `Invoke` 缓存张量数据指针（因为回退会重算、指针会变）；（2）模型最好是无状态的（无变量、无 LSTM），否则状态在批次间会不一致。这正是 XNNPACK 默认委托失败时能安全回退到内置 CPU kernel 的基础——见 [interpreter.cc:339-348](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/interpreter.cc#L339-L348) 的注释「the execution will fall back to default implementation if the XNNPACK delegate fails」。

#### 4.3.4 代码实践

**实践目标**：在源码层面追踪一次「运行期委托失败 → CPU 兜底」的完整数据流，确认输入数据在回退过程中不会丢失。

**操作步骤**：

1. 打开 [interpreter_utils.cc:29-71](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/delegates/interpreter_utils.cc#L29-L71)。
2. 在 `InvokeWithCPUFallback` 中标记三个关键点：
   - **点 A**（L47-L56）：备份输入到 `buf`；
   - **点 B**（L58）：`RemoveAllDelegates()` 撤销委托；
   - **点 C**（L62-L66）：从 `buf` 恢复输入、再 `Invoke()`。
3. 思考：为什么必须先备份再 `RemoveAllDelegates`？因为 `RemoveAllDelegates` 会调 `EnsureMemoryAllocations` 重新规划内存（见 [subgraph.cc:2286](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/subgraph.cc#L2286)），张量缓冲区地址可能改变，不备份就会丢输入。
4. 对照 [interpreter_utils.h:33-36](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/delegates/interpreter_utils.h#L33-L36) 的两条限制，回答：一个用 `tf.Variable` 维护 LSTM 隐藏状态的模型，为什么不适合用这个自动回退？

**预期结果**：你会确认 CPU 回退的正确性完全建立在「输入可备份 + 图无状态」两个前提上；对有状态模型，回退会让批次间状态错乱，因此这类模型应依赖应用期回滚而非运行期回退。

> 待本地验证：可参考 [interpreter_utils_test.cc](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/delegates/interpreter_utils_test.cc)，它用一个故意在 N 次调用后失败的测试委托来验证回退行为。

#### 4.3.5 小练习与答案

**练习 1**：应用期回滚和运行期 CPU 回退，哪个是无条件发生的？
**答案**：应用期回滚无条件发生——只要委托 `Prepare` 返回 `kTfLiteDelegateError`，运行时就自动 `RemoveAllDelegates`。运行期 CPU 回退是**可选**的，必须显式调用 `InterpreterUtils::InvokeWithCPUFallback` 而非普通 `Invoke`，普通 `Invoke` 遇到委托运行期失败只会原样返回错误码。

**练习 2**：`InvokeWithCPUFallback` 最后为什么返回 `kTfLiteDelegateError` 而不是 `kTfLiteOk`？结果不是已经算对了吗？
**答案**：结果确实有效，但返回 `kTfLiteDelegateError` 是为了**告知调用方「这次是 CPU 兜底的、性能可能不达标」**，让上层（如默认 XNNPACK 委托的回退逻辑）能据此决定是否禁用该委托、避免后续每次都触发回退的开销。

---

### 4.4 常见委托后端：GPU / NNAPI / XNNPACK

#### 4.4.1 概念说明

仓库 `tensorflow/lite/delegates/` 下有多个具体后端，它们的适用场景各异：

| 委托 | 目录 | 典型后端 | 适用场景 |
|------|------|----------|----------|
| **GPU** | `delegates/gpu/` | OpenCL / OpenGL / Vulkan / Metal | 手机/桌面 GPU，浮点密集型 CNN，延迟敏感的实时推理 |
| **NNAPI** | `delegates/nnapi/` | Android 厂商 NPU/DSP/GPU | Android 8.1+，走系统抽象层访问芯片专用加速器 |
| **XNNPACK** | `delegates/xnnpack/` | 高度优化 CPU 库 | 几乎所有平台，常作为**默认委托**自动应用，无需 GPU/NPU 也能提速 |
| Hexagon | `delegates/hexagon/` | 高通 DSP（Hexagon） | 高通芯片的极致低功耗推理 |
| CoreML | `delegates/coreml/` | Apple Neural Engine | iOS/macOS |
| Flex | `delegates/flex/` | TF 选择注册的 op | 在 TFLite 里调用尚未移植到 Lite 的 TF op |

它们对外接口各异（C API、C++ 类、外部动态库），但**内部都遵循同一个 Prepare 模板**——这正是 4.1 所讲协议的威力。

#### 4.4.2 核心流程

所有委托的 `Prepare` 几乎都是同一个套路（「委托 Prepare 三段式」）：

```
DelegatePrepare(context, delegate):
   1. 构造一个 TfLiteRegistration（含 init/prepare/free/invoke，代表 DELEGATE 宏算子）
   2. 用「节点支持判定函数」扫描全图，得到 ops_to_replace（委托能接管的节点 id 列表）
   3. context->ReplaceNodeSubsetsWithDelegateKernels(context, registration, ops_to_replace, delegate)
      —— 把 4.2 的分区+改写交给运行时
```

第 2 步的「判定函数」是各委托的差异化所在：GPU 判定能否转成它的算子图、NNAPI 查 `ANeuralNetworks` 支持表、XNNPACK 查自己的 op 支持表。判定函数返回 `false` 的节点就会被分到 `kTfNonPartition` 段留在 CPU（4.3 的回退也由此自然成立）。

#### 4.4.3 源码精读

**GPU 委托**是最完整的范本。[model_builder.cc:3599-3627](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/delegates/gpu/common/model_builder.cc#L3599-L3627) 的 `DelegatePrepare` 严格对应三段式：先内联构造 `registration`（`init` 建 `DelegateContext`、`free` 释放、`prepare` 检查 `user_data`），再 `GetOpsToReplace(context, ...)` 拿到支持节点，最后 `context->ReplaceNodeSubsetsWithDelegateKernels(...)`。其判定函数见 [model_builder.cc:3291-3300](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/delegates/gpu/common/model_builder.cc#L3291-L3300)：`IsNodeSupportedFn` 调 `IsSupported(...)` 并限定输入输出类型为 `{kTfLiteFloat32, kTfLiteFloat16}`。对外的 C API 在 [delegate.h:30-49](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/delegates/gpu/delegate.h#L30-L49)（`TfLiteGpuDelegateV2Create` / `Delete`），内部封装多种图形 API 自动择优。

**NNAPI 委托**面向 Android。[nnapi_delegate.h:46-97](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/delegates/nnapi/nnapi_delegate.h#L46-L97) 的 `StatefulNnApiDelegate : public TfLiteDelegate` 直接继承委托结构体，其 `Options` 暴露了若干重要旋钮：`execution_preference`（低功耗/单次最快/持续高速）、`accelerator_name`（指定厂商加速器）、`cache_dir`+`model_token`（**编译缓存**，避免每次启动重新编译）、`disallow_nnapi_cpu`（默认 true，因 NNAPI CPU 常比 TFLite 内置 kernel 还慢）、`max_number_delegated_partitions`（默认 3，限制分段数以免跨段拷贝开销超过加速收益）、`allow_fp16`。注意头文件 L28-L29 标注 NNAPI 已 **DEPRECATED**，官方建议迁移到厂商插件。

**XNNPACK 委托**特殊在「常被默认应用」。[xnnpack_delegate.cc:7637-7654](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/delegates/xnnpack/xnnpack_delegate.cc#L7637-L7654) 的 `DelegatePrepare` 同样三段式：`PrepareOpsToDelegate` 得到 `ops_to_replace`，再 `ReplaceNodeSubsetsWithDelegateKernels`。[interpreter.cc:339-348](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/interpreter.cc#L339-L348) 表明 InterpreterBuilder 会遍历 `delegate_providers`（目前仅 XNNPACK 可能默认开启），对返回非空的委托自动 `ModifyGraphWithDelegateImpl`；若 XNNPACK 应用失败则回退默认实现。所以即使用户什么委托都不显式加，TFLite 也可能已悄悄用 XNNPACK 加速了你的 CPU 推理。

**自定义委托的捷径**：若你要写自己的委托，`SimpleDelegateInterface`（[simple_delegate.h:77-117](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/delegates/utils/simple_delegate.h#L77-L117)）把三段式封装好了，你只需实现 `IsNodeSupportedByDelegate`（判定函数）+ `CreateDelegateKernelInterface`（给一个 `Init/Prepare/Eval` 的 kernel）+ `DelegateOptions`（`max_delegated_partitions`、`min_nodes_per_partition`），再用 `TfLiteDelegateFactory::CreateSimpleDelegate` 换成 `TfLiteDelegate*`。参考实现见 [simple_delegate_test.cc](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/delegates/utils/simple_delegate_test.cc)。

#### 4.4.4 代码实践

**实践目标**：用 Python 加载一个 GPU（或 XNNPACK）委托并应用，观察分区与不可加速段的回退；并在源码侧印证「不可加速部分回退到 CPU kernel」。

**操作步骤**（Python，示例代码）：

```python
# 示例代码：加载并应用 GPU 委托（需有对应平台的 GPU 委托动态库）
import tensorflow as tf

# 1) 通过 load_delegate 从动态库加载委托（CTypes 调 tflite_plugin_create_delegate）
gpu_delegate = tf.lite.experimental.load_delegate(
    "libdelegate.so",            # GPU 委托共享库路径，依平台而异
    options={"precision_loss_allowed": "1"})

# 2) 把委托传给 Interpreter；构造期会触发 ModifyGraphWithDelegate
interp = tf.lite.Interpreter(
    model_path="model.tflite",
    experimental_delegates=[gpu_delegate])
interp.allocate_tensors()

# 3) 推理；被委托段在 GPU 跑，不支持段在 CPU 跑，运行时自动衔接
interp.invoke()
```

> 注意：`tf.lite.experimental.load_delegate` 的实现见 [interpreter.py:137](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/python/interpreter.py#L137) 与 `Delegate` 类 [interpreter.py:56-134](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/python/interpreter.py#L56-L134)：它用 `ctypes` 加载动态库、调用约定的 `tflite_plugin_create_delegate` 拿到原生 `TfLiteDelegate*` 指针，再交给 C++ 侧 `ModifyGraphWithDelegate`。

**源码印证（不可加速段如何回退 CPU）**：

1. 在 [model_builder.cc:3291-3300](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/delegates/gpu/common/model_builder.cc#L3291-L3300) 看到 GPU 的判定函数对不支持的算子返回 `false` → 这些节点**不进** `ops_to_replace`。
2. 在 [subgraph.cc:564-615](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/subgraph.cc#L564-L615) 看到 `PartitionGraph` 把它们归入 `kTfNonPartition` subset，在重建执行计划时（L593-L598）**原样 push 回 `execution_plan_`**，仍由原 OpResolver 解析出的 CPU kernel 执行。
3. 于是「GPU 段 + CPU 段」在同一执行计划里交替，`Invoke` 时各自走自己的 `invoke`——这就是「不可加速部分回退到 CPU kernel」的真正含义（它不是失败回退，而是分区时就规划好的混合执行）。

**需要观察的现象**：开启 `TFLITE_LOG` 后，`ReplaceNodeSubsetsWithDelegateKernels` 的日志（[subgraph.cc:578-584](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/subgraph.cc#L578-L584)）会打印形如「Replacing N out of M node(s) with delegate ... yielding P partitions」——`P` 即分区数，`M - N` 即留在 CPU 的节点数。

> 待本地验证：上述 Python 代码需在有 GPU 委托动态库的真实设备（Android/桌面 GPU）运行；本 CI 无 GPU。在无 GPU 环境可改用 XNNPACK（通常已默认启用）并观察同一日志。

#### 4.4.5 小练习与答案

**练习 1**：为什么 NNAPI 的 `Options::disallow_nnapi_cpu` 默认为 `true`？
**答案**：因为 NNAPI 的 CPU 实现通常比 TFLite 自带的 CPU kernel 还慢。设为 true 意味着「只有当整图都能被硬件加速器接管时才用 NNAPI」，避免出现「一部分在慢速 NNAPI CPU 上跑」反而拖慢整体的情况（注释见 [nnapi_delegate.h:84-88](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/delegates/nnapi/nnapi_delegate.h#L84-L88)）。

**练习 2**：XNNPACK 委托和 GPU/NNAPI 委托在「是否需要专用硬件」上有何根本区别？这对默认开启策略有什么影响？
**答案**：XNNPACK 跑在 CPU 上，不需要专用硬件，几乎所有平台都能受益，所以它适合作为**默认委托**自动应用（[interpreter.cc:339-348](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/interpreter.cc#L339-L348)）；而 GPU/NNAPI 依赖特定硬件与驱动，必须由用户或应用显式启用，且失败时需回退。

---

## 5. 综合实践

**任务**：把本讲四个模块串起来，完整复述「一次带 GPU 委托的推理」从创建到（可能的）回退的全过程，并标注每一步对应的源码位置。

请按下列提示写出一份「带行号引用的执行轨迹」：

1. **创建**：用户调 `TfLiteGpuDelegateV2Create`（[delegate.h:41](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/delegates/gpu/delegate.h#L41)），得到含 `Prepare=DelegatePrepare`、`flags=0`（不支持动态张量）的 `TfLiteDelegate*`。
2. **应用**：`Interpreter::ModifyGraphWithDelegate` → [interpreter.cc:401](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/interpreter.cc#L401) → `Subgraph::ModifyGraphWithDelegateImpl`（[subgraph.cc:2485](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/subgraph.cc#L2485)）。写出 STEP1（备份 `pre_delegation_execution_plan_`）、STEP2（`SwitchToDelegateContext` + `Prepare` + `SwitchToKernelContext`）各自行号。
3. **圈地**：委托的 `DelegatePrepare`（[model_builder.cc:3599](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/delegates/gpu/common/model_builder.cc#L3599)）用 `GetOpsToReplace` 得到支持集，调 `ReplaceNodeSubsetsWithDelegateKernels`（[subgraph.cc:521](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/subgraph.cc#L521)）。
4. **分区**：`PartitionGraph`（[subgraph.cc:509](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/subgraph.cc#L509)）→ `PartitionGraphIntoIndependentNodeSubsets`（[graph_info.cc:90](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/graph_info.cc#L90)），产出交替的 `NodeSubset`，重建 `execution_plan_`。
5. **执行**：`Invoke` 按新执行计划跑，DELEGATE 宏算子在 GPU 执行，CPU 段走原 kernel。
6. **失败分支**：若应用期失败 → [interpreter.cc:419](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/interpreter.cc#L419) 调 `RemoveAllDelegates`（[subgraph.cc:2282](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/core/subgraph.cc#L2282)）回滚；若运行期失败且用了 `InvokeWithCPUFallback`（[interpreter_utils.cc:29](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/delegates/interpreter_utils.cc#L29)）则 CPU 兜底重算。

完成后，你应当能用一张图把「协议结构体 → 三步应用 → 图分区 → 混合执行 → 两级回退」整条链路一次性讲清。

---

## 6. 本讲小结

- **Delegate 是「子图卸载协议」**：本质是 `TfLiteDelegate` 这组函数指针（`Prepare` + 缓冲回调 + `flags`），它把一段连续节点合并成一个 `BuiltinOperator_DELEGATE` 宏算子交给加速后端。
- **图分区是核心机制**：运行时把执行计划按「委托支持/不支持」切成交替的 `NodeSubset`，支持段合并成宏算子、不支持段原样留 CPU，二者在同一条 `execution_plan_` 里混合执行。
- **分区靠 epoch 遍历**：`PartitionGraphIntoIndependentNodeSubsets` 用纪元遍历，相邻同类型节点尽量合并，边界张量成为各 subset 的输入输出。
- **上下文能力收窄**：`SwitchToDelegateContext`/`SwitchToKernelContext` 让只有委托 `Prepare` 期间才能调用改图函数，普通 kernel 期间调用即报错。
- **两级失败回退**：应用期失败无条件整图回滚（靠 `pre_delegation_execution_plan_` 存档）；运行期失败可选 CPU 兜底（`InvokeWithCPUFallback`，前提是无状态模型且不缓存张量指针）。
- **各后端遵循同一 Prepare 模板**：GPU/NNAPI/XNNPACK 的 `DelegatePrepare` 都是「构造 registration + 判定支持集 + `ReplaceNodeSubsetsWithDelegateKernels`」三段式；XNNPACK 常作为默认委托自动应用。

---

## 7. 下一步学习建议

- **动手写一个最小自定义委托**：参照 [simple_delegate.h](https://github.com/tensorflow/tensorflow/blob/6a6ae5d9d29b922f3f16ef297b33de06ba0c5131/tensorflow/lite/delegates/utils/simple_delegate.h) 与 `simple_delegate_test.cc`，实现一个只接管 `ADD` 算子的玩具委托，亲手跑通「判定 → 分区 → 宏算子 invoke」全链路。这是巩固本讲的最佳方式。
- **深入 GPU 委托的编译细节**：阅读 `delegates/gpu/common/model_builder.cc` 中 `IsSupported` 与 GPU 算子图（`GraphFloat32`）的构造，理解委托如何在 `init/prepare` 阶段把 TFLite 子图编译成 GPU shader。
- **回看运行时主线**：本讲的「执行计划改写」与 u8-l1 的「`Invoke` 按计划逐算子执行」是一枚硬币的两面；建议重读 `Subgraph::InvokeImpl`，体会加入 DELEGATE 宏算子后主循环其实无需任何特判。
- **关注 Stable Delegate / 外部委托**：若对跨进程、可热更新的委托感兴趣，可读 `delegates/external/` 与 `delegates/utils/experimental/stable_delegate/`，它们代表了委托 ABI 稳定化的方向。
