# 重写后的 SeparateMemoryFromCompute：访存/计算解耦、SplitDataflow 与 AutoBlockify

## 1. 本讲目标

本讲是「Cube-Vector 融合与流水线优化」单元的第三讲，承接 u8-l2 对 DynamicCVPipeline 前半段（分析规划型子 pass）的讲解，进入流水线的**重构/落地阶段**：把已经规划好的计算块真正拆成可在 Cube 核与 Vector 核之间重叠执行的双流水线。

学完后你应当能够：

- 说清**重写后的 `SeparateMemoryFromCompute`** 为什么退化成一个只跑 `MarkGMLoadPass` 的薄壳，以及它如何用「标记 GM load → 交给下游多缓冲」的思路解耦访存与计算。
- 复述 `SplitDataflow` 的完整子 pass 编排，重点掌握 `DataDependencyAnalysis` 如何识别 V→C / C→V / 内存 / iterArg 四类跨核依赖，以及 `InterCoreTransferAndSync` 如何插入跨核数据搬运与 `SyncBlockSet/Wait` 同步。
- 区分两套「多缓冲」机制：CV 流水线内部的 `AllocMultiCache`（MLIR 层 Inner/Outer 多缓冲）与 `multibuffer` 选项驱动的 BiSheng `--enable-auto-multi-buffer`。
- 理解 `AutoBlockify`（`TRITON_ALL_BLOCKS_PARALLEL`）如何把「逻辑 block 数」映射到有限的物理核上：编译期包 `scf.for`、运行期裁剪到核数，并对顺序敏感算子做黑名单保护。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**直觉一：为什么要「访存/计算解耦」。** 昇腾 NPU 的 AI 核里，Cube（矩阵）单元和 Vector（向量）单元是**独立**的硬件流水线。若一段计算里「先从全局内存（GM）搬数据到片上、再做矩阵乘」，搬数和计算是串行的——搬的时候 Cube 空闲，算的时候搬运空闲。把「搬」和「算」拆开、用多缓冲让二者错峰重叠，就能近似做到「搬第 N+1 块的同时算第 N 块」，吞吐翻倍。这正是 `SeparateMemoryFromCompute` 与 `AllocMultiCache` 的共同目标。

**直觉二：跨核依赖需要显式同步。** 当 Cube 块和 Vector 块被拆到不同核上并行执行后，原本靠「源程序顺序」隐含保证的「生产者先于消费者」就失效了——两块在不同核上同时跑，消费者可能在生产者写出之前就去读。因此必须在两者之间插入**数据搬运**（把生产者的结果送到消费者能看到的缓冲）和**同步**（`set`/`wait` 一个 flag，保证消费者在读之前等到生产者写完）。这是 `SplitDataflow` 的核心职责。

**直觉三：「纯增益 + 失败回退」的设计哲学。** 整条 DynamicCVPipeline 是可选优化：它先把整个 module 克隆备份，任何一个子 pass 分析不出来或遇到不支持的模式，就设置一个 fallback 属性 `triton_ascend.dynamic_cv_pipeline.rc`，最后用备份还原、回到「不开 CV 流水线」的编译路径。因此本讲大量出现 `hasFallbackAttr` 早退与 `setFallbackAttr` 兜底——它们是这套机制「永远不比不开更差」的保证。

> 前置术语回顾（来自 u8-l1/u8-l2）：`core_type`（CUBE/VECTOR）、`block_id`（计算块编号）、`scope`（BiSheng 的作用域容器，承载核类型与地址空间）、PIPE（hivm 流水线类型，如 `PIPE_MTE2/MTE3/FIX`）、LCA（最近公共祖先）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [third_party/ascend/lib/DynamicCVPipeline/AddDynamicCVPipeline.cpp](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/AddDynamicCVPipeline.cpp#L85-L97) | DynamicCVPipeline 总入口：克隆备份、按序跑 10 个子 pass、失败则还原。本讲的 `SplitDataflow`(L92)、`AllocMultiCache`(L94)、`SeparateMemoryFromCompute`(L96) 都在这里被串联。 |
| [third_party/ascend/lib/DynamicCVPipeline/SeparateMemoryFromComputePass.cpp](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/SeparateMemoryFromComputePass.cpp#L36-L57) | 重写后的薄壳 pass：建一个 `OpPassManager`，只装入 `MarkGMLoadPass` 并运行。 |
| [third_party/ascend/lib/DynamicCVPipeline/SeparateMemoryFromCompute/MarkGMLoadPass.cpp](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/SeparateMemoryFromCompute/MarkGMLoadPass.cpp#L250-L285) | 本讲主角之一：识别「从 GM 搬到片上 alloc」的 `memref.copy`，给目标 alloc 打 `multi_buffer` 标记。 |
| [third_party/ascend/lib/DynamicCVPipeline/SplitDataflow.cpp](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/SplitDataflow.cpp#L44-L84) | `SplitDataflow` 的 7 步子 pass 编排。 |
| [third_party/ascend/lib/DynamicCVPipeline/SplitDataflow/DataDependencyAnalysis.cpp](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/SplitDataflow/DataDependencyAnalysis.cpp#L830-L865) | 跨核数据依赖分析，产出 v2c / c2v / memory / iterArg 四类依赖。 |
| [third_party/ascend/lib/DynamicCVPipeline/SplitDataflow/InterCoreTransferAndSync.cpp](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/SplitDataflow/InterCoreTransferAndSync.cpp#L1502-L1524) | 消费依赖列表，插入跨核搬运与 `SyncBlockSet/Wait` 同步。 |
| [third_party/ascend/lib/DynamicCVPipeline/AllocMultiCache.cpp](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/AllocMultiCache.cpp#L44-L70) | CV 流水线内部的多缓冲插入（Inner/Outer scope）。 |
| [third_party/ascend/lib/AutoBlockify/AutoBlockify.cpp](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/AutoBlockify/AutoBlockify.cpp#L286-L358) | AutoBlockify V2 参考实现：把 `program_id` 展平成 `blockifiedId` 并把区域算子包进 `scf.for`。 |
| [third_party/ascend/backend/compiler.py](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L157-L266) | `ttir_to_linalg`：把 `add_dynamic_cv_pipeline` 接进 pass 流水线，并控制 `multibuffer`、`auto-blockify-loop` 等编译开关。 |
| [third_party/ascend/backend/utils.py](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/utils.py#L349-L364) | `TRITON_ALL_BLOCKS_PARALLEL` 开关与 AutoBlockify 黑名单规则。 |

## 4. 核心概念与源码讲解

### 4.1 重写后的 SeparateMemoryFromCompute：MarkGMLoadPass 驱动的访存/计算解耦

#### 4.1.1 概念说明

`SeparateMemoryFromCompute`（把访存从计算中分离）这个名字暗示它做「重活」。但在当前 HEAD，它被重写成一个**薄壳 pass**：自身不再做 IR 变换，而是建一个内部 `OpPassManager`，只装入一个子 pass —— `MarkGMLoadPass` —— 然后运行它。

这种「重写」的思路是**把直接改写 IR 的硬逻辑，换成「打标记 + 让下游工具链据此解耦」的软逻辑**：`MarkGMLoadPass` 只负责**识别**哪些 `memref.copy` 是「从全局内存搬入片上缓冲」的 GM load，并在其目标 alloc 上打一个 `hivm.multi_buffer` 标记；真正的「异步 load 上提 / 访存计算解耦」由下游 BiSheng 工具链读到这个标记后完成。这样 Triton 侧只承担「识别 + 标注」，解耦的具体策略留给更了解硬件的 BiSheng，职责更清晰、回退更安全。

#### 4.1.2 核心流程

`SeparateMemoryFromComputePass::runOnOperation` 的流程极简：

1. 若 module 已带 fallback 属性，直接返回（前序 pass 已判定不可优化）。
2. 新建 `OpPassManager`，`pm.addPass(createMarkGMLoadPass())`。
3. `runPipeline(pm, module)`；失败则 `setFallbackAttr(module, ERRCODE_FAILED)`。

`MarkGMLoadPass` 内部三阶段：

- **Phase 1（只读收集）**：遍历所有 `memref::CopyOp`，用三条规则筛出「GM load 候选」。
- **Phase 2（解析缓冲份数）**：对每个候选，按其所在 `scope` 的核类型解析多缓冲份数 N。
- **Phase 3（打标记）**：若 N > 1，在目标 alloc 上插/更新一个 `annotation::MarkOp`，附 `multi_buffer = N` 属性。

三条筛选规则（这是本模块的算法核心）：

- **Rule 1（源是 GM）**：`copy` 的 source 必须能穿透 view-like / `extract_slice` / `scf.for` 与 `scf.while` 的 iter_arg，最终追溯到**入口函数的 BlockArgument**（即由宿主传入的 GM 指针）。
- **Rule 2（目标是片上 alloc）**：`copy` 的 target 穿透同样的 view-like 链后，必须终止于一个 `memref::AllocOp`（片上分配）。
- **Rule 3（份数来自 scope）**：由所在 `scope` 的核类型决定份数。

#### 4.1.3 源码精读

先看薄壳本体——重写后它几乎只剩调度逻辑：

[SeparateMemoryFromComputePass.cpp:36-57](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/SeparateMemoryFromComputePass.cpp#L36-L57) 建内部 PM、装入 `MarkGMLoadPass`、运行；失败即回退。注意它与所有 CV 子 pass 一样，进入即检查 `hasFallbackAttr`。

`MarkGMLoadPass` 的 Rule 1 是一段递归「use-def 追溯」。[MarkGMLoadPass.cpp:96-152](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/SeparateMemoryFromCompute/MarkGMLoadPass.cpp#L96-L152) 中 `traceSourceToFuncArg` 反复穿透 `ViewLikeOpInterface` 与 `tensor::ExtractSliceOp`，遇到 `func::FuncOp` 的 BlockArgument 时用 `resolveFuncBlockArg` 判断：若该函数无外部调用（`symbolKnownUseEmpty`）即为入口函数，参数即 GM 源；否则继续沿调用方对应 operand 往上追。遇到 `scf.for`/`scf.while` 的 iter_arg 则追到其 init 值。这是把「一维指针算术 + 循环携带」还原成「这是从函数参数进来的 GM 指针」的关键。

Rule 2 在 [MarkGMLoadPass.cpp:157-170](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/SeparateMemoryFromCompute/MarkGMLoadPass.cpp#L157-L170)：同样的穿透，但终点必须是 `memref::AllocOp`。

Rule 3 与打标记：

[MarkGMLoadPass.cpp:175-192](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/SeparateMemoryFromCompute/MarkGMLoadPass.cpp#L175-L192) `resolveBufferCount` 用 `getScopeType` 判定 scope 是 VECTOR 还是 CUBE，分别返回 `kDefaultVBufferCount` / `kDefaultCBufferCount`。

[MarkGMLoadPass.cpp:214-240](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/SeparateMemoryFromCompute/MarkGMLoadPass.cpp#L214-L240) `markGMLoadCandidate`：若 `bufferCount <= 1` 直接跳过；否则在 alloc 后插一个 `annotation::MarkOp` 并设 `hivm::MultiBufferAttr`。

最后看三阶段总调度：

[MarkGMLoadPass.cpp:250-285](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/SeparateMemoryFromCompute/MarkGMLoadPass.cpp#L250-L285) Phase 1 用 `module.walk([&](memref::CopyOp copyOp){...})` 收集候选；Phase 2 逐个 `resolveBufferCount`，得 -1 即 `setFallbackAttr` 回退；Phase 3 调 `markGMLoadCandidate`。

> **诚实的源码说明（重要）**：当前 HEAD 下 [MarkGMLoadPass.cpp:54-55](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/SeparateMemoryFromCompute/MarkGMLoadPass.cpp#L54-L55) 把 `kDefaultVBufferCount` 与 `kDefaultCBufferCount` 都设为 **1**，于是 `resolveBufferCount` 永远返回 1，`markGMLoadCandidate` 因 `bufferCount <= 1` 恒跳过。也就是说，这套「标记 GM load」的脚手架目前已完整就位（Rule 1/2 的候选识别是活跃的），但**多缓冲份数策略被刻意保守地置为 1，实际不打标记**。这正是「纯增益」设计的体现：结构先行、策略后调，永远不会让流水线变得更差。把它理解成「为 GM load 多缓冲预留的、保守关闭的接入点」最为准确。

#### 4.1.4 代码实践

**实践目标**：确认 `SeparateMemoryFromCompute` 在 CV 流水线中的位置，并验证当前它「只跑 MarkGMLoadPass」。

**操作步骤**：

1. 在 950 平台开启 CV 流水线编译（`enable_dynamic_cv_pipeline` 默认在 950 上为 True，见 [compiler.py:1228-1229](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L1228-L1229)），并对一个含 `tl.dot` 的 kernel 设置 `debug=True` 或导出 `TRITON_DEBUG=1`。
2. 在 [AddDynamicCVPipeline.cpp:88-97](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/AddDynamicCVPipeline.cpp#L88-L97) 确认 `createSeparateMemoryFromComputePass()` 是第 9 个子 pass（L96）。
3. 用 `MLIR_ENABLE_DUMP=1` 跑编译，找到 `MarkGMLoad` 前后的 dump。

**需要观察的现象**：由于当前缓冲份数为 1，`MarkGMLoad` 前后的 IR 应**无差异**（不打任何 `annotation.mark ... {multi_buffer}`）。这本身就是一个有意义的验证结论。

**预期结果**：dump 中看不到新增的 `multi_buffer` 标记。若你看到标记，说明缓冲份数常量已被调大（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Rule 1 要穿透 `scf.for` 的 iter_arg？  
**答案**：因为 `tl.dot`/reduce 等会被 lowering 成主循环，GM 指针常作为循环携带值（iter_arg）逐轮传递。不穿透 iter_arg，就会把「循环里的 GM load」误判为非 GM 来源。

**练习 2**：`MarkGMLoadPass` 在 `bufferCount == -1` 时为什么选择回退而不是跳过？  
**答案**：`-1` 表示 `getScopeType` 失败、scope 核类型异常——这是「分析不出来」的信号，属于不可安全继续的情况，故 `setFallbackAttr` 让整条 CV 流水线回退；而 `bufferCount == 1` 是「分析出来了，只是不需要多缓冲」，属于可安全继续的正常情形，故仅跳过标记。

---

### 4.2 SplitDataflow：数据依赖分析与跨核传输同步（InterCoreTransferAndSync）

#### 4.2.1 概念说明

`SplitDataflow` 是把「规划好的计算块」真正**拆分**成 Cube 核与 Vector 核各自执行的代码、并在它们之间补齐数据搬运与同步的 pass。它是 `SplitDataflowPass` 的容器，内部按固定顺序跑 7 个子 pass，本讲聚焦其中最关键的两个：

- `DataDependencyAnalysisPass`（分析）：算出哪些数据需要跨核流动。
- `InterCoreTransferAndSyncPass`（改写）：为每条跨核依赖插入搬运 + `set/wait` 同步。

#### 4.2.2 核心流程

**`SplitDataflow` 的 7 步**（见 [SplitDataflow.cpp:54-73](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/SplitDataflow.cpp#L54-L73)）：

1. `AddBlockIdForControlOps` — 给控制流算子补 `block_id`。
2. `DataDependencyAnalysis` — 本节主角（分析）。
3. `InterCoreTransferAndSync` — 本节主角（改写）。
4. `MarkMainLoop` — 标注主计算循环。
5. `SeparateCVScope` — 把 Cube/Vector 代码分进各自 scope。
6. `PreserveControlAttrsCanonicalize` — 在规范化时保留控制流属性。
7. `RefineArgsBlockId` — 精化主循环迭代变量的 `block_id`。

**`DataDependencyAnalysis` 的四类依赖**：它先调用 `createBlockInfoMap` 把每个 `block_id` 聚合成 `BlockInfo`（含 `isCube`、外部 inputs/outputs），再分四路收集依赖，结果存入一个 `DataDependencyInfo` 分析对象：

| 依赖类型 | 来源函数 | 含义 |
| --- | --- | --- |
| V→C（VectorToCube） | `analyzeExternalInputs` | Cube 块的某个输入由 Vector 算子产生 |
| C→V（CubeToVector） | `analyzeExternalOutputs` | Cube 块的某个输出被 Vector 算子消费 |
| 内存依赖 | `analyzeMemoryEffect` | 经内存（别名）的跨核 RAW/WAW，用 `MemoryDependenceGraph` + `AliasAnalysis` 发现 |
| iterArg 依赖 | `processIterArgDependencies` | `scf.for` 的 iter_arg 在循环内被不同核类型的算子读写 |

**对齐层级（LCA）**：生产者块和消费者块可能嵌套在不同深度的 scope/循环里。`findCommonLevelBlockIds` 沿祖先链找到两者的**最近公共祖先（LCA）**，返回「恰好在公共祖先之下的那一层」的生产者/消费者 block_id；找不到公共祖先则返回 `{-1,-1}` 触发回退。这决定了搬运与同步该插在哪一层。

**`InterCoreTransferAndSync` 的改写**：对每条依赖，在合适层级插入搬运 buffer（`createTransferAllocs`）、数据拷贝，以及一对 `SyncBlockSetOp`/`SyncBlockWaitOp`，并用 `FlagIdManager` 分配 flag id、用 `FlagIdReuseManager` 复用。PIPE 标注遵循硬件流水线约定（见 4.2.3）。

#### 4.2.3 源码精读

依赖的数据结构定义在头文件里。[DataDependencyAnalysis.h:40-66](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/include/DynamicCVPipeline/SplitDataflow/DataDependencyAnalysis.h#L40-L66) 定义 `DependencyType{VectorToCube, CubeToVector}`、`BlockInfo`、`DependencyInfo`（含 `producerBlockId/consumerBlockId` 及初始 id、是否标量/1D 张量/全转置等标志）。

块信息聚合在 [DataDependencyAnalysis.cpp:182-261](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/SplitDataflow/DataDependencyAnalysis.cpp#L182-L261)：`collectBlockInfo` 把同一 `block_id` 的 op 收为一组，标记 `isCube`（`ssbuffer.core_type` 含 "CUBE" 即为 Cube 块），并把「定义不在本组」的 operand 记为 input、「有组外 user」的 result 记为 output。

V→C 分析在 [DataDependencyAnalysis.cpp:495-548](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/SplitDataflow/DataDependencyAnalysis.cpp#L495-L548)：遍历每个 Cube 块的 input，若其定义算子的 `core_type` 是 VECTOR，则记一条 V→C 依赖。C→V 分析在 [DataDependencyAnalysis.cpp:551-628](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/SplitDataflow/DataDependencyAnalysis.cpp#L551-L628) 对称地处理 Cube 块 output 被 VECTOR 算子消费的情形，并额外判断 `isAllTransposedInVector`（C→V 的值若经 transpose 后只被 vector 用，可走 fixpipe 优化）。

内存依赖分析较复杂，在 [DataDependencyAnalysis.cpp:653-748](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/SplitDataflow/DataDependencyAnalysis.cpp#L653-L748)：借助 `MemoryDependenceGraph`（基于 `AliasAnalysis`）取每个 op「在此之前执行过」的前驱 `getExecBefore`，对每个核类型不同的真实前驱，经 `findCommonLevelBlockIds` 对齐层级后记一条内存依赖。

LCA 对齐算法在 [DataDependencyAnalysis.cpp:751-828](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/SplitDataflow/DataDependencyAnalysis.cpp#L751-L828)：同 MLIR Block 直接返回；否则收集生产者祖先链，再沿消费者祖先上溯，命中公共祖先时返回「公共祖先下一层」两侧的 block_id。

总调度在 [DataDependencyAnalysis.cpp:830-865](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/SplitDataflow/DataDependencyAnalysis.cpp#L830-L865)，四步依次执行并打印三类依赖计数。

改写侧的同步插入最能体现 PIPE 约定。[InterCoreTransferAndSync.cpp:927-974](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/SplitDataflow/InterCoreTransferAndSync.cpp#L927-L974) `insertMemDepSync` 规定：

- C→V：生产者核 CUBE、`srcPipe = PIPE_FIX`；消费者核 VECTOR、`dstPipe = PIPE_MTE2`。
- V→C：生产者核 VECTOR、`srcPipe = PIPE_MTE3`；消费者核 CUBE、`dstPipe = PIPE_MTE2`。

然后在生产者之后插 `SyncBlockSetOp`、在消费者之前插 `SyncBlockWaitOp`，两者共用同一个 `flagId`。PIPE 的含义可粗略理解为：`PIPE_FIX`（Cube 的 L0A/L0C 等固定流水）、`PIPE_MTE3`（Cube 的内存搬运入）、`PIPE_MTE2`（Cube/Vector 的内存搬出入）——它们告诉硬件调度器这两步之间的数据依赖类型，以便正确排队。

总入口在 [InterCoreTransferAndSync.cpp:1502-1524](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/SplitDataflow/InterCoreTransferAndSync.cpp#L1502-L1524)：建 `FlagIdManager`/`FlagIdReuseManager`，调 `processDependencies`，失败即回退。

#### 4.2.4 代码实践

**实践目标**：在 IR 中看到 `SplitDataflow` 插入的跨核搬运与同步算子。

**操作步骤**：

1. 选一个 Cube 与 Vector 混合的 kernel（如 matmul + elementwise 后处理），950 平台、`MLIR_ENABLE_DUMP=1` 编译。
2. 在 dump 序列里定位 `inter-core-transfer-and-sync` 相关的输出（按 pass 名分段）。
3. 搜索 `SyncBlockSetOp`/`SyncBlockWaitOp`（IR 中形如 `hivm.hir.sync_block_set/wait` 之类），观察其 `flagId`、`tcore_type`、PIPE 属性。

**需要观察的现象**：相邻的 Cube 块与 Vector 块之间出现成对的 set/wait，且 flag id 一致；C→V 与 V→C 的 PIPE 取值符合上表。

**预期结果**：能找到至少一对匹配的 set/wait；若 kernel 完全无跨核依赖则不会出现。**待本地验证**具体 IR 文本形态。

#### 4.2.5 小练习与答案

**练习 1**：为什么需要 LCA 对齐，不能直接用原始的 producer/consumer block_id？  
**答案**：生产者和消费者可能分别嵌套在不同的 scope/循环里，直接在各自层级插搬运会导致 buffer 作用域或同步点错位（例如 buffer 分配在内层而跨核需要在外层可见）。LCA 把两者对齐到「公共祖先下一层」，保证搬运 buffer 与 set/wait 落在能同时看见两边的正确层级。

**练习 2**：内存依赖分析与 V→C/C→V 分析的输入来源有何不同？  
**答案**：V→C/C→V 分析基于 `BlockInfo` 的 inputs/outputs（数据流 use-def）；内存依赖分析基于 `MemoryDependenceGraph` + `AliasAnalysis`（发现经内存别名、而非直接 use-def 边的跨核读写），用于处理那些没有显式 value 传递但通过共享内存产生的依赖。

---

### 4.3 AllocMultiCache 与 multibuffer 多缓冲

#### 4.3.1 概念说明

「多缓冲（multibuffer / multi-cache）」是用多份片上缓冲实现「搬第 N+1 块的同时算第 N 块」的经典手段。本仓库里有**两套**相关机制，初学者极易混淆，务必分清：

1. **CV 流水线内部的 `AllocMultiCache`**：一个 MLIR pass，在 `AddDynamicCVPipeline` 的第 7 步（[AddDynamicCVPipeline.cpp:94](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/AddDynamicCVPipeline.cpp#L94)）运行，包含 `AddMultiBufferInnerScope`（内层多缓冲）与 `AddMultiBufferOuterScope`（外层多缓冲）两个子 pass。它的产物**会出现在 `ttadapter.mlir` dump 里**。
2. **`multibuffer` 编译选项**：`NPUOptions.multibuffer`（默认 True，见 [compiler.py:1031](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L1031)），在 BiSheng 编译阶段翻译成 `--enable-auto-multi-buffer=...`（见 [compiler.py:524-535](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L524-L535)），作用于 SIMD/linalg 主线（不一定走 CV 流水线）。

此外，4.1 节的 `MarkGMLoadPass` 是「GM load 多缓冲」的标记入口（当前保守关闭）。三者职责不同：`AllocMultiCache` 是 CV 流水线内部的 IR 级多缓冲插入；`multibuffer` 选项是 BiSheng 的自动多缓冲开关；`MarkGMLoadPass` 是给特定 GM load 打多缓冲标记的预留接入点。

#### 4.3.2 核心流程

`AllocMultiCachePass::runOnOperation`（[AllocMultiCache.cpp:44-70](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/AllocMultiCache.cpp#L44-L70)）：

1. fallback 早退。
2. 建 PM，依次 `addPass(createAddMultiBufferInnerScopePass())` 与 `addPass(createAddMultiBufferOuterScopePass())`。
3. `runPipeline`，失败即回退。

「Inner」与「Outer」分别指在计算块的**内层循环**与**外层循环**边界插入多份缓存与轮转，从而让相邻迭代的访存与计算错峰。

`multibuffer` 选项到编译 flag 的映射逻辑（[compiler.py:524-535](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L524-L535)）：当 `multibuffer` 非 None 或 `num_stages` 非 None 时介入；`multibuffer=False` 或 `num_stages==1` 会让 `multi_buffer_value=False`，其余为 True，拼成 `--enable-auto-multi-buffer=<bool>`。

#### 4.3.3 源码精读

CV 内部多缓冲：[AllocMultiCache.cpp:44-70](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/DynamicCVPipeline/AllocMultiCache.cpp#L44-L70) 如上所述。注意它和 4.1 的 `SeparateMemoryFromCompute` 一样，都是「容器 pass + 子 pass」结构，且都遵循 fallback 早退约定。

选项侧字段：[compiler.py:1031](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L1031) `multibuffer: bool = True`，以及 `num_stages`、`set_workspace_multibuffer` 等相关字段（[compiler.py:1001](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L1001) 与 [compiler.py:1057](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L1057)）。当 CV 流水线开启时，[compiler.py:217-218](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L217-L218) 会把 `set_workspace_multibuffer` 置 0。

#### 4.3.4 代码实践

**实践目标**：区分并观察两套多缓冲机制。

**操作步骤**：

1. **CV 内部 `AllocMultiCache`**：950 平台开 CV 流水线，`MLIR_ENABLE_DUMP=1`，在 dump 中找到 `AllocMultiCache`/`AddMultiBufferInnerScope`/`AddMultiBufferOuterScope` 段，对比其前后 IR 是否新增了多份缓存分配与轮转。
2. **`multibuffer` 选项**：用同一 kernel，分别在 `multibuffer=True`（默认）与 `multibuffer=False`（或 `num_stages=1`）下编译，开 `debug=True`，在 [compiler.py:524-535](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L524-L535) 打印的命令行里确认 `--enable-auto-multi-buffer` 取值翻转。

**需要观察的现象**：(1) CV 路径下 `AllocMultiCache` 前后出现 buffer 份数变化；(2) `multibuffer` 开关翻转时编译命令里对应 flag 翻转。

**预期结果**：两套机制各自可观测、互不混淆。**待本地验证** IR 中多缓冲的具体形态（buffer 数量、轮转算子）。

#### 4.3.5 小练习与答案

**练习 1**：`AllocMultiCache` 与 `multibuffer` 选项作用在哪两个不同阶段？  
**答案**：`AllocMultiCache` 作用在 Triton 侧的 MLIR pass 阶段（CV 流水线内部，产物进 `ttadapter.mlir`）；`multibuffer` 选项作用在下游 BiSheng 编译阶段（通过 `--enable-auto-multi-buffer`）。

**练习 2**：为什么 CV 流水线开启时要把 `set_workspace_multibuffer` 置 0？  
**答案**：CV 流水线已经用 `AllocMultiCache` 在 IR 层做了多缓冲并自行管理缓冲，若 BiSheng 再对 workspace 做一次自动多缓冲会重复占用、可能冲突，故显式关闭以避免双重多缓冲。

---

### 4.4 AutoBlockify：TRITON_ALL_BLOCKS_PARALLEL 并行块映射

#### 4.4.1 概念说明

回顾 u2-l2/u2-l3：NPU 的 grid 直接对应物理核占用，且 `coreDim` 受 65535 上限约束。当逻辑上需要启动的 program（逻辑 block）数远多于物理核时，`AutoBlockify`（由环境变量 `TRITON_ALL_BLOCKS_PARALLEL` 控制，默认开启）让**一个物理核串行跑多个逻辑 block**：把逻辑 block id 重新映射，让每个物理核在一个 `scf.for` 循环里迭代若干个逻辑 block，运行期再把实际派发的物理 block 数裁剪到核数。这样既绕开了 coreDim 上限，又把大量逻辑块摊到有限核上。

> 注意：含 `atomic`/`volatile`/`inline_asm`/`cache` 修饰的访存等**顺序敏感**算子会被黑名单禁用 AutoBlockify（见 4.4.3），此时 coreDim 上限重新生效。

#### 4.4.2 核心流程

AutoBlockify 的核心数学是「三维 program id 展平 + 分块」。设三维 grid 为 \((X, Y, Z)\)，当前 program 的三维 id 为 \((x, y, z)\)：

\[ \text{logicalBlockId} = x \cdot (Y\!\cdot\! Z) + y\cdot Z + z,\qquad \text{logicalBlockNum} = X\cdot Y\cdot Z \]

引入分块大小 \(S\)（`autoBlockifySize`），把每个物理核要负责的逻辑块表示为：

\[ \text{blockifiedId}_k = \text{logicalBlockId} + k,\quad k=0,\dots,S-1 \]

并对越界位置（`blockifiedId >= logicalBlockNum`）用 mask 屏蔽。之后把原本依赖 `program_id` 的区域算子包进一个以 \(k\) 为迭代变量的 `scf.for`，使一个物理核依次处理 \(S\) 个逻辑块；非分块的 `program_id` 则改用 `blockifiedId` 经 `div/rem` 反推。

**接线方式（重要，易错）**：在当前生产编译流里，AutoBlockify 这一**特性**并非由 Python 直接调用 in-tree 的 `AutoBlockify.cpp`，而是通过给 BiSheng 传 `--enable-auto-blockify-loop` 开关实现；in-tree 的 `AutoBlockify.cpp`（"AutoBlockify V2"）是该机制的**Triton 方言级参考实现**，可经 `triton-opt -auto-blockify` 单独复现，其 Python 接入点 `add_auto_blockify` 当前被注释掉（[compiler.py:194](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L194)）。运行期裁剪到核数则在 `driver.py` 完成。

#### 4.4.3 源码精读

开关与黑名单：[utils.py:349-350](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/utils.py#L349-L350) `_is_auto_map_parallel_blocks_enabled` 读 `TRITON_ALL_BLOCKS_PARALLEL`（默认 "true"）。[utils.py:53-64](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/utils.py#L53-L64) `AUTO_BLOCKIFY_BLACKLIST_RULES` 列出四类不安全算子：`tt.atomic_rmw/cas`、`tt.elementwise_inline_asm`、`isVolatile=true` 的 `tt.load`、带 `cacheModifier` 的 `tt.load/store`。命中即把 `has_auto_blockify_blacklist_op` 置真并告警（[compiler.py:163-172](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L163-L172)）。

生产流接线：[compiler.py:190-191](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L190-L191) 在黑名单命中或总开关关闭时把 `auto_blockify_size` 置 1（等价于不分块）；[compiler.py:692-693](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L692-L693) 在 linalg→二进制路径上，当开关开且无黑名单时追加 `--enable-auto-blockify-loop`；simt_only 路径同理（[compiler.py:1186-1190](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L1186-L1190)）。

参考实现 `AutoBlockify.cpp` 的三步：

[AutoBlockify.cpp:198-220](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/AutoBlockify/AutoBlockify.cpp#L198-L220) 用 `GetNumProgramsOp`/`GetProgramIdOp` 取三维 grid 与 id，按上面公式算出 `logicalBlockId`/`logicalBlockNum`。

[AutoBlockify.cpp:222-249](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/AutoBlockify/AutoBlockify.cpp#L222-L249) 用 `MakeRangeOp(0, S)` + `SplatOp` + `AddIOp` 造出 `blockifiedId`，再用两次 `CmpIOp`（上界 slt、下界 sge）+ `OrIOp` 造越界 mask，包进一个 `UnrealizedConversionCastOp`。

[AutoBlockify.cpp:252-271](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/AutoBlockify/AutoBlockify.cpp#L252-L271) 把其余 `GetProgramIdOp` 用 `DivSI/RemSI` 从 `blockifiedId` 反推替换；[AutoBlockify.cpp:273-283](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/AutoBlockify/AutoBlockify.cpp#L273-L283) 对带 `autoBlockifyRegionOpAttr` 的区域算子调 `createBlockifyLoop` 包成 `scf.for`。

安全性检查在 [AutoBlockify.cpp:136-191](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/lib/AutoBlockify/AutoBlockify.cpp#L136-L191) `checkBlockifiable`：递归沿 use 链确认 value 可安全分块，遇到 `CondBranchOp/IntToPtrOp/WhileOp/DotOp` 或张量指针类型即拒绝（与 Python 侧黑名单互补）。

运行期裁剪在 [driver.py:545-547](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/driver.py#L545-L547)：`enable_auto_map_parallel_blocks = (开关开 and 无黑名单)`，`num_physical_blocks` 按 `mix_mode` 取 Vector 核数（aiv）或 AI 核数，启动时据此裁剪。

#### 4.4.4 代码实践

**实践目标**：观察 AutoBlockify 开关对编译命令与（若用 triton-opt）IR 的影响。

**操作步骤**：

1. 对同一 kernel，分别以 `TRITON_ALL_BLOCKS_PARALLEL=true`（默认）与 `=false` 编译，`debug=True`，对比打印的编译命令里是否含 `--enable-auto-blockify-loop`（参考 [compiler.py:692-693](https://github.com/triton-lang/triton-ascend/blob/0c3b1f6c32ff1e08bdde97597983a0937be8ae51/third_party/ascend/backend/compiler.py#L692-L693)）。
2. 阅读型实践：在 `AutoBlockify.cpp` 的 `preProcess` 中，手算一个 \((X,Y,Z)=(2,2,2), S=2\) 的例子，写出 `logicalBlockId`、`blockifiedId` 与 mask。
3. （可选）用 `triton-opt -auto-blockify -auto-blockify-size=2` 在一份 ttir 上离线复现分块 IR。

**需要观察的现象**：开关翻转时 `--enable-auto-blockify-loop` 出现/消失；含 atomic 的 kernel 会触发黑名单告警并强制关闭。

**预期结果**：命令行 flag 随开关翻转；手算结果符合公式。IR 级 triton-opt 复现**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `tt.atomic_rmw` 必须进黑名单？  
**答案**：AutoBlockify 把多个逻辑块串进同一物理核的 `scf.for`、并把 grid 映射到核数，改变了原本「每个逻辑块对应一个并发核」的执行模型；原子操作的语义依赖于所有块并发可见的跨核原子性，重映射后会破坏正确性，故禁用。

**练习 2**：`autoBlockifySize` 与物理核数是什么关系？  
**答案**：`autoBlockifySize` 是「每个物理核串行处理几个逻辑块」的编译期分块因子；物理核数是运行期 `driver.py` 探测的硬件核数。逻辑块总数 ≈ 物理核数 × `autoBlockifySize`，运行期再裁剪到实际核数。

---

## 5. 综合实践

把本讲四个模块串起来，做一次「CV 流水线落地全景」追踪。

**任务**：选一个 `tl.dot` 后接若干 elementwise 的 kernel（如 `tutorials/03-matrix-multiplication.py` 加一个逐元素后处理），在 950 平台以默认 `compile_mode=unstructured_in_simt`、`enable_dynamic_cv_pipeline=True`、`TRITON_ALL_BLOCKS_PARALLEL=true` 编译，完成下表（用 `MLIR_ENABLE_DUMP=1` + `debug=True`）：

| 观察项 | 对应 pass / 代码 | 你看到的证据 |
| --- | --- | --- |
| 计算块与核类型标注 | PlanComputeBlock（u8-l2） | `ssbuffer.core_type=CUBE/VECTOR`、`block_id` |
| 跨核依赖与同步 | SplitDataflow / InterCoreTransferAndSync | 成对的 `sync_block_set/wait`、flag id、PIPE |
| CV 内部多缓冲 | AllocMultiCache | buffer 份数变化 |
| GM load 标记 | MarkGMLoadPass（当前保守关闭） | 无 `multi_buffer` 标记（符合预期） |
| AutoBlockify 开关 | `--enable-auto-blockify-loop` | 编译命令中是否出现 |

**进阶**：把 `multibuffer` 设为 `False` 重编，对比 CV 内部 `AllocMultiCache` 的 IR 变化与编译命令中 `--enable-auto-multi-buffer` 的翻转；再用 `TRITON_ALL_BLOCKS_PARALLEL=false` 关掉 AutoBlockify，观察启动行为差异（注意 coreDim 是否触顶）。

> 凡涉及实际运行结果，若本地环境未就绪，请如实标注「待本地验证」，不要臆造 IR 文本。

## 6. 本讲小结

- 重写后的 `SeparateMemoryFromCompute` 退化为薄壳，只调度 `MarkGMLoadPass`；后者用 Rule 1/2（源追溯 GM 函数参数、目标追溯片上 alloc）识别 GM load 候选，并用 Rule 3 解析多缓冲份数——当前份数常量为 1，标记步骤保守关闭，体现「纯增益」设计。
- `SplitDataflow` 按 7 步串联，核心是 `DataDependencyAnalysis`（产出 V→C / C→V / 内存 / iterArg 四类依赖，并用 LCA 对齐层级）与 `InterCoreTransferAndSync`（按 PIPE 约定插入搬运与 `SyncBlockSet/Wait` 同步、用 flag id 配对）。
- 「多缓冲」有三套易混机制：CV 内部的 `AllocMultiCache`（MLIR 层 Inner/Outer）、`multibuffer` 选项（→BiSheng `--enable-auto-multi-buffer`）、`MarkGMLoadPass` 的 GM load 标记（预留接入点）。
- `AutoBlockify`（`TRITON_ALL_BLOCKS_PARALLEL`）用「展平 program id + 分块 + `scf.for` 包裹 + 运行期裁剪核数」把大量逻辑块映射到有限物理核；生产流经 `--enable-auto-blockify-loop` 触发，in-tree `AutoBlockify.cpp` 为参考实现；atomic/volatile/inline_asm/cache 修饰算子被黑名单禁用。
- 整条流水线贯彻「克隆备份 + fallback 还原」，任何子 pass 分析失败都安全回退到「不开 CV 流水线」。

## 7. 下一步学习建议

- 若想看清「搬运 buffer 与 set/wait 到底长什么样」，建议结合 u10-l4 的 `DynamicCVPipeline_ut` 手写 MLIR 测试套件，挑一个 `single_cube_single_vector` 用例，用 `triton-mlir-opt --pass-pipeline=...` 离线跑 `SplitDataflow`，对照本讲 4.2 验证你的理解。
- 性能向：学完本讲后可进入 u9（自动调优体系），重点看 u9-l3 代价模型如何在不跑设备的情况下评估 CV 流水线带来的收益，以及 u9-l2 的 CV 自动调优如何为 `tl.dot` 选 tile。
- 调试向：若 CV 流水线意外回退，用 `MLIR_ENABLE_DUMP=1` 定位是哪个子 pass 设置了 `triton_ascend.dynamic_cv_pipeline.rc`，再回到本讲对应章节排查（常见于 `findCommonLevelBlockIds` 返回 -1 或 iterArg 核类型冲突）。
