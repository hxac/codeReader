# 图优化 Pass 体系

## 1. 本讲目标

在上一讲（u6-l1）里，我们把 `Optimizer` 看作一条「总装配线」，看到它依次走过预处理、图改写 Pass、合轴、任务生成、AutoSchedule 等阶段。本讲要把这条装配线里的「图改写 Pass」这一段单独拆开放大看。

读完本讲，你应当能够：

- 说清 Autofuse 的图优化 Pass 是**怎样被调度执行**的——入口在哪、谁来决定跑哪些 Pass、按什么顺序跑。
- 认识若干个**真实存在的典型改写 Pass**（如 CSE、广播优化、Pow 等价替换、MaskedFill 改写等），能用自己的话说出每个 Pass 想消除的低效模式。
- 理解为什么 **Pass 的顺序必须固定**、不同芯片平台为什么会有不同的 Pass 序列，以及这背后「图改写确定性」这一条工程红线。

本讲只聚焦「Pass 体系本身」，不会深入每个 Pass 的全部细节，也不涉及 ATT/codegen，这些留给后续讲义。

## 2. 前置知识

本讲默认你已经掌握以下概念（来自前序讲义）：

- **ASCIR / `af::AscGraph`**：Autofuse 内部的图视图。全链路只有一份图数据，optimize / att / codegen 都通过带调度语义的视图访问同一张图（见 u4-l2、u6-l1）。本讲里 `graph` 参数基本都是 `af::AscGraph &`。
- **Node / Anchor**：图的节点与连接边。Pass 的本质就是「读图上的节点 → 改边或增删节点」，这部分数据结构在 u4-l1 已建立。
- **Optimizer 流水线**：u6-l1 讲过 `Optimizer::OptimizeForHintGraph` 的阶段顺序，本讲的 Pass 就是其中「图改写」这一站。

需要补充的两个小术语：

- **Pass（图改写遍）**：对整张图做一次「扫描 + 改写」的最小单元。每个 Pass 只关心一类低效模式（比如「重复的减法」「多余的标量广播」），把它消除掉。一个 Pass 改完，图就变得更适合后续调度。
- **Pass Runner（Pass 调度器）**：把多个 Pass 按固定顺序依次跑起来的调度器。你可以把它理解成一个「播放列表」，Pass 是里面的「曲目」，顺序是写死的。

一句话直觉：**Pass 是「给图做减法/换法的清洁工」，Runner 是「排班表」。**

## 3. 本讲源码地图

本讲涉及的源码文件分布在 `autofuse/optimize/` 下三个子目录里：

| 文件 | 作用 |
|------|------|
| [graph_pass/base_graph_pass.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/graph_pass/base_graph_pass.h) | 所有 Pass 的抽象基类 `BaseGraphPass`，定义纯虚 `RunPass`。 |
| [graph_pass/pass_runner_handler.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/graph_pass/pass_runner_handler.h) / [.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/graph_pass/pass_runner_handler.cpp) | 静态入口 `PassRunnerHandler`，把请求桥接到平台特定的 runner。 |
| [platform/common/pass_runner.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/platform/common/pass_runner.h) | `BasePassRunner`：持有 Pass 列表并顺序执行的通用基类。 |
| [platform/v1/pass_runner_v1.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/platform/v1/pass_runner_v1.h) | `PassRunnerV1`：昇腾 2201（v1）平台注册的 7 个 Pass。 |
| [v35/optimize/pass_runner_v2.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/v35/optimize/pass_runner_v2.h) | `PassRunnerV2`：昇腾 950（v35）平台注册的 11 个 Pass。 |
| [graph_pass/pass_utils.h](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/graph_pass/pass_utils.h) | `PassUtils`：Pass 之间共享的改写工具（重连边、剪枝、判等）。 |
| [optimize/optimize.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/optimize.cpp) | `Optimizer::GraphPass` 的调用点，标明 Pass 在总流水线中的位置。 |
| `graph_pass/*.cpp` | 各具体 Pass 的实现（Pow、Broadcast、CSE 等）。 |

一个重要观察：**「Pass 定义」和「Pass 注册（排顺序）」是分离的。** `graph_pass/` 目录里只放「Pass 是什么」，而「跑哪些、什么顺序」由 `platform/` 下的 runner 类决定。这种分离让同一批 Pass 可以在不同芯片平台上按不同顺序组合。

---

## 4. 核心概念与源码讲解

### 4.1 Pass Runner 调度机制

#### 4.1.1 概念说明

很多人第一次看 `pass_runner_handler.h` 会感到意外——这个名叫「PassRunnerHandler」的类，**自己一个 Pass 都不持有**。它只是一个「前台」，真正干活的是从「平台（Platform）」里取出来的 runner。

为什么要这样绕一层？因为不同芯片平台（昇腾 2201 与昇腾 950）需要跑的 Pass 集合不一样。如果调度逻辑写死在 handler 里，每加一个平台就得改公共代码；现在的设计里，handler 只说「请把活交给当前平台的 runner」，至于 runner 里装了哪些 Pass，由各平台自己决定。这和 u6-l1 里 Optimizer「按平台分叉」的思路是一致的。

三层结构如下：

- **`BaseGraphPass`**：Pass 的抽象基类，规定「每个 Pass 必须实现 `RunPass(graph)`」。
- **`BasePassRunner`**：runner 的通用基类，持有一个 `vector<BaseGraphPass>`，并提供「按顺序逐个跑」的 `RunPasses`。
- **`PassRunnerV1` / `PassRunnerV2`**：各平台的具体 runner，在构造函数里决定「装哪些 Pass、按什么顺序装」。

#### 4.1.2 核心流程

从 Optimizer 调用到某个具体 Pass 被执行，调用链是：

```text
Optimizer::GraphPass(impl_graph)            # optimize.cpp:682，总流水线在这里调用
  └─ PassRunnerHandler::RunPasses(graph)    # pass_runner_handler.cpp:14，静态入口
       ├─ PlatformFactory::GetInstance().GetPlatform()   # 取当前芯片平台
       └─ platform->GetPassRunner()         # 向平台要它的 runner
            └─ BasePassRunner::RunPasses(graph)   # pass_runner.h:28，顺序迭代
                 └─ for (pass : passes_) pass->RunPass(graph)  # 逐个执行
```

关键点：handler 到 runner 之间有一次「平台分发」，runner 内部则是一个**朴素的顺序 for 循环**——没有动态调度、没有依赖分析、没有并行。顺序就是 `passes_` 这个向量里的顺序，而这个顺序在 runner 构造时就定死了。

#### 4.1.3 源码精读

**① Pass 的抽象基类**——只有一个纯虚方法：

[base_graph_pass.h:17-22](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/graph_pass/base_graph_pass.h#L17-L22)：定义 `BaseGraphPass`，规定每个 Pass 必须实现 `virtual Status RunPass(af::AscGraph &graph) = 0`，输入是整张图，返回执行状态。这是一个典型的「策略接口」。

**② 静态入口（前台）**——自己不持有 Pass，只做平台分发：

[pass_runner_handler.cpp:14-20](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/graph_pass/pass_runner_handler.cpp#L14-L20)：`RunPasses` 先通过 `PlatformFactory` 取到当前平台，再调 `platform->GetPassRunner()` 拿到该平台的 runner，最后 `pass_runner->RunPasses(graph)` 把活交出去。注意这里没有任何 Pass 名字——它是平台无关的。

**③ 通用 runner 基类**——持有列表并顺序执行：

[pass_runner.h:18-34](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/platform/common/pass_runner.h#L18-L34)：
- `passes_`（L20）是 `std::vector<std::unique_ptr<BaseGraphPass>>`，Pass 的顺序就是向量下标顺序。
- `RegisterPass<PassT>()`（L22-25）是一个模板，把一个 Pass 实例 `emplace_back` 进向量。
- `RunPasses`（L28-33）就是一个 `for` 循环，依次对每个 Pass 调 `RunPass(graph)`，跑完返回 `SUCCESS`。

**④ 总流水线的调用点**——Pass 站在 PreProcess 之后、合轴之前：

[optimize.cpp:917](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/optimize.cpp#L917)：`GE_CHK_STATUS_RET(GraphPass(optimize_graph), "Run graph passes failed");`。结合 [optimize.cpp:905-921](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/optimize.cpp#L905-L921) 可见阶段顺序：PreProcess（L905）→ DtypeConsistency（L914）→ **GraphPass（L917）** → 重新补全 API 信息与拓扑排序（L919-920）→ 合轴（L923-928）。`GraphPass` 本体只有一行，见 [optimize.cpp:682-684](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/optimize.cpp#L682-L684)。

#### 4.1.4 代码实践

**实践目标**：亲眼看一遍「调用从 Optimizer 一路传到某个 Pass 的 `RunPass`」的完整链路。

**操作步骤**：

1. 打开 [optimize.cpp:682](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/optimize.cpp#L682)，确认 `Optimizer::GraphPass` 内部只调用了 `PassRunnerHandler::RunPasses`。
2. 跟进到 [pass_runner_handler.cpp:14](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/graph_pass/pass_runner_handler.cpp#L14)，确认它通过 `PlatformFactory` + `GetPassRunner()` 拿到 runner。
3. 跟进到 [pass_runner.h:28](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/platform/common/pass_runner.h#L28)，确认 `RunPasses` 是一个顺序 for 循环。

**需要观察的现象**：整条链路上，**没有任何一处出现具体的 Pass 类名**（如 CSE、Pow）。具体类名只出现在 `pass_runner_v1.h` / `pass_runner_v2.h` 这两个平台 runner 的构造函数里。

**预期结果**：你会清楚地看到「调度机制（handler + BasePassRunner）」与「具体 Pass（各平台 runner）」被物理分离在不同文件里。

#### 4.1.5 小练习与答案

**练习 1**：如果把某个平台的 `GetPassRunner()` 返回 `nullptr`，会发生什么？
**答案**：在 [pass_runner_handler.cpp:18](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/graph_pass/pass_runner_handler.cpp#L18) 有 `GE_CHECK_NOTNULL(pass_runner, ...)`，会直接返回错误码并打印 "Get pass runner by platform failed."，整个 GraphPass 阶段失败。

**练习 2**：`BasePassRunner::RunPasses` 是 `const` 方法且返回固定的 `af::SUCCESS`，这说明设计上对「单个 Pass 失败」做了什么假设？
**答案**：当前实现里 `for` 循环并未检查每个 `RunPass` 的返回值（见 [pass_runner.h:29-31](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/platform/common/pass_runner.h#L29-L31)），即假设各 Pass 内部会自己处理异常、不会让整批 Pass 中断。这是「尽力改写」的宽松策略。

---

### 4.2 典型改写 Pass 逐个讲解

#### 4.2.1 概念说明

这一节我们沿着 `PassRunnerV1` 的注册顺序（见 4.3），逐个认识 7 个公共 Pass。先建立一条心智模型：

> 框架 lowering 出来的子图「正确但啰嗦」——存在大量「可以用更便宜的算子表达」「重复计算」「多余的搬运」。Pass 的职责就是在调度之前，把这些低效模式规整成「调度器更好处理」的形态。

注意：有一个 `SoftmaxPatternFusionPass` 虽然也继承 `BaseGraphPass`、也放在 `graph_pass/` 目录里，但它**并没有被任何 runner 注册**。它是在 reduce 任务生成阶段被直接实例化调用的（见 4.2.3 末尾）。这说明 `BaseGraphPass` 这个抽象既能被 runner 统一调度，也能被其它流程单独借用。

#### 4.2.2 核心流程

下表先给一张「速查表」，7 个公共 Pass 按 V1 注册顺序排列：

| 顺序 | Pass 类 | 一句话目的 |
|------|---------|-----------|
| 1 | `PowEquivSubstitutionPass` | 把特殊指数的 `Pow` 换成更便宜的算子（sqrt、倒数、乘法等）。 |
| 2 | `BroadcastConstToStorePass` | 给「存常量」的 `Store` 显式补一个 `Broadcast` 节点。 |
| 3 | `ScalarTo1DTensorPass` | 给没有调度轴的标量节点补一个大小为 1 的一维轴。 |
| 4 | `ScalarBroadcastOptimizationPass` | 删除多余的「标量广播」链。 |
| 5 | `MaskedFillInputReorderPass` | 把 `MaskedFill` 改写成 `Select` 并重排输入。 |
| 6 | `ExpandDimsForAllReducePass` | 给「全归约」图在最前面插一根大小为 1 的轴。 |
| 7 | `DuplicateElewiseCsePass` | 公共子表达式消除：合并重复的同源 `Sub` 节点。 |

总体改写套路是：「遍历 `graph.GetAllNodes()` → 用类型/属性判断是否命中模式 → 改边 / 换类型 / 删节点 / 加节点」。

#### 4.2.3 源码精读

**① `PowEquivSubstitutionPass`：把 Pow 换成更便宜的算子**

`Pow(x, n)` 在 n 是一些特殊值时，可以用更轻的算子等价替换。替换规则枚举见 [pow_equiv_substitution_pass.h:18-29](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/graph_pass/pow_equiv_substitution_pass.h#L18-L29)，每条注释都点明了等价关系，例如：

- `kHalf`（0.5）：`pow(x, 0.5) = sqrt(x)`
- `kNegHalf`（-0.5）：`pow(x, -0.5) = 1/sqrt(x)`
- `kNegOne`（-1）：`pow(x, -1) = reciprocal(x)`
- `kTwo`（2）：`pow(x, 2) = mul(x, x)`
- `kZero`（0）：`pow(x, 0) = brc(1)`（广播全 1）
- `kOne`（1）：`pow(x, 1)` 直接删节点

替换比通用 `Pow` 更快，是因为 `Pow` 通常要走更重的库函数（如 `exp(n*log(x))`），而 `sqrt`/`mul`/`reciprocal` 是单条 Vector 指令。这个 Pass 是 `PowEquivSubstitutionPass` 类（[L31](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/graph_pass/pow_equiv_substitution_pass.h#L31)），内部用一张 `PatternType → SubstitutionFunc` 的映射来分发。

**② `BroadcastConstToStorePass`：给常量 Store 补广播**

[broadcast_const_to_store.cpp:25-52](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/graph_pass/broadcast_const_to_store.cpp#L25-L52)：遍历图中所有 `Store` 节点，如果它的输入是个常量张量（`IsConstTensor`），就在常量与 Store 之间插入一个新的 `Broadcast` 节点（L34-41），并把 Store 输出的 axis/repeats/strides/dtype 等信息复制给这个新广播节点（L42-49）。这样「把一个标量常量铺满成张量再存」的语义就被显式表达成一个广播算子，方便后续统一调度。

**③ `ScalarTo1DTensorPass`：补一根一维轴**

[scalar_to_1d_tensor.cpp:18-44](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/graph_pass/scalar_to_1d_tensor.cpp#L18-L44)：调度器要求每个非 buffer 节点都至少有一根调度轴（`node->attr.sched.axis`）。如果发现某些节点 `axis` 为空（L21），就统一创建一根名为 `"axis_1d"`、大小为 1 的轴（L30），并把它赋给这些节点及其输出（L31-42）。这把「纯标量」规整成「一维张量」，是后续 tiling 的前置条件。

**④ `ScalarBroadcastOptimizationPass`：删除多余标量广播**

[scalar_broadcast_optimization.cpp:144-195](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/graph_pass/scalar_broadcast_optimization.cpp#L144-L195)：当一串标量 `Broadcast` 后面的消费算子本身就支持标量输入时，这一串广播就是纯开销。Pass 先找到连续的标量广播链（L156），再用 `IsNextNodeSupportScalarInput`（L77-142）逐个判断下游算子是否支持标量输入、必要时交换输入顺序（L136）。若全部支持，就把广播链的首个输入直接重连给下游（L171-184），然后删除整条广播链（L187-192）。

**⑤ `MaskedFillInputReorderPass`：MaskedFill → Select**

[masked_fill_input_reorder_pass.cpp:101-109](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/graph_pass/masked_fill_input_reorder_pass.cpp#L101-L109) 找出所有 `MaskedFill` 节点。`ReorderMaskedFillInput`（[L71-98](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/graph_pass/masked_fill_input_reorder_pass.cpp#L71-L98)）做两件事：一是两次交换输入下标（L85-86），把输入从 `(x, mask, value)` 调整成 Select 期望的 `(mask, value, x)`；二是把节点类型从 `MaskedFill` 改写成 `Select`（L90-95）。原因是底层 Vector 指令集里 `Select` 更直接，`MaskedFill` 只是一个语义别名。

**⑥ `ExpandDimsForAllReducePass`：给全归约图补一根轴**

[expand_dims_for_all_reduce.cpp:69-115](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/graph_pass/expand_dims_for_all_reduce.cpp#L69-L115)：如果图里存在「全归约」（所有轴都被归约，见 `IsAllReduce`，L18-37），调度上会出问题（没有保留轴）。Pass 在图的最前面插一根大小为 1 的轴 `"axis_1d"`（L77），并把所有非 IO 节点的 axis/strides/repeats 同步扩一维（L88-113），相当于做了一次 `ExpandDims(0)`。

**⑦ `DuplicateElewiseCsePass`：公共子表达式消除（CSE）**

[duplicate_elewise_cse_pass.cpp:135-148](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/graph_pass/duplicate_elewise_cse_pass.cpp#L135-L148) 是 Pass 入口。核心思路：如果两个**同类型**算子的**两个输入都来自同一个源输出端口**，且输出的 view（dtype/axis/repeats/strides）和调度轴完全一致（`IsOutputEquivalent`，[L54-59](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/graph_pass/duplicate_elewise_cse_pass.cpp#L54-L59)），那它们算的是同一件事，可以合并掉一个。当前支持类型表 `kCseSupportedTypes` 只含 `Sub`（[L30](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/graph_pass/duplicate_elewise_cse_pass.cpp#L30)）。合并时选拓扑序最早（OpDesc ID 最小）的节点作为「正本」（`SelectCanonical`，L62-66），把冗余节点的下游边重连到正本，再删除冗余节点（`MergeElewise`，L80-93）。`CanMerge`（L69-77）保证合并不会让正本引用到尚未计算的数据。

**补充：未被 runner 注册的 `SoftmaxPatternFusionPass`**

[reduce_schedule_case_generator.cpp:219-222](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/task_generator/reduce_schedule_case_generator.cpp#L219-L222)：在 reduce 任务生成阶段，若 ASCIR 注册了 `Softmax` 算子（`GetAscIrCodegenImpl("Softmax") != nullptr`），就直接 `SoftmaxPatternFusionPass softmax_pass; softmax_pass.RunPass(graph);`。它复用了 `BaseGraphPass` 接口，但不走 runner 的统一调度——这是「Pass 抽象被单独借用」的一个真实例子。

#### 4.2.4 代码实践

**实践目标**：从 `graph_pass/` 目录中挑出至少三个具体 Pass，写出它们的改写目的（对应本讲规格里的实践任务前半部分）。

**操作步骤**：

1. 列出 `graph_pass/` 下所有 `*.h`，挑出三个你感兴趣的（建议选 `pow_equiv_substitution_pass`、`masked_fill_input_reorder_pass`、`duplicate_elewise_cse_pass`，因为它们改写效果最直观）。
2. 对每个 Pass，打开它的 `.cpp`，定位 `RunPass` 的入口 `for` 循环，回答三个问题：
   - 它**遍历**图里什么样的节点（用什么 `if` 判断）？
   - 它**改写**成了什么（新增/删除/换类型）？
   - 它**消除**了哪种低效？
3. 填写下面这张表（示例答案见「预期结果」）：

| Pass | 命中条件 | 改写动作 | 消除的低效 |
|------|---------|---------|-----------|
| PowEquivSubstitution | Pow 节点且指数为特殊值 | 换成 sqrt/mul/reciprocal 等 | 昂贵的通用 Pow 指令 |
| MaskedFillInputReorder | MaskedFill 节点 | 重排输入 + 类型改为 Select | 语义别名带来的额外开销 |
| DuplicateElewiseCse | 同源、等价输出的 Sub | 合并冗余节点 | 重复计算 |

**需要观察的现象**：三个 Pass 的 `RunPass` 都以 `for (node : graph.GetAllNodes())` 开头，都只改命中模式的那一小撮节点，对其它节点零副作用。

**预期结果**：你会确认「每个 Pass 只做一件小事、只命中一类模式」这一设计原则。

**待本地验证**：具体命中哪些节点取决于你构造的输入图；可以用 4.3 节的 UT 方式构造图来观察。

#### 4.2.5 小练习与答案

**练习 1**：为什么 CSE Pass 要用 `std::map` 以「输入源锚点对」为 key 来分组（见 [duplicate_elewise_cse_pass.cpp:29](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/graph_pass/duplicate_elewise_cse_pass.cpp#L29)），而不是按算子类型分组？
**答案**：两个算子要算「同一件事」，前提不仅是类型相同，更重要的是**输入来自同一个源输出端口**（否则值不同）。按源锚点对分组，才能保证同组的节点真的吃同一份数据；按类型分组会把不相关的节点混在一起。

**练习 2**：`SoftmaxPatternFusionPass` 为什么不放进 runner，而要在 reduce 任务生成阶段单独调？
**答案**：它只在「图里存在 reduce 且注册了 Softmax 算子」时才有意义（见 [reduce_schedule_case_generator.cpp:219](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/task_generator/reduce_schedule_case_generator.cpp#L219) 的条件判断），把它放进通用 runner 会在不相关的图上空跑甚至误改。它在更窄的上下文里被有条件地调用，复用的只是 `BaseGraphPass::RunPass` 这个接口形态。

---

### 4.3 Pass 顺序与确定性

#### 4.3.1 概念说明

Pass 不是随便排的。「谁先跑、谁后跑」直接决定了图的最终形态，因为**后一个 Pass 看到的是前一个 Pass 改完的图**。举两个真实依赖：

- `ScalarTo1DTensorPass`（顺序 3）会往图里**新增**调度轴。如果它晚于 `ExpandDimsForAllReducePass`（顺序 6），后者对「轴为空」的判断就会失效。
- `ScalarBroadcastOptimizationPass`（顺序 4）会**删除**标量广播节点。如果它晚于 `DuplicateElewiseCsePass`（顺序 7），CSE 看到的就是「还有冗余广播」的图，可能错过合并机会或合并到马上要被删的节点上。

因此 Autofuse 把 Pass 顺序**硬编码在 runner 的构造函数里**，运行时不允许重排。这背后是项目的一条工程红线——**图改写确定性**：同样的输入图，无论跑多少次、在什么环境，都必须得到完全相同的改写结果。这条红线在项目的编码规范（`docs/guidelines/编码红线.md`）里有明确要求。

同时，**不同平台允许有不同的 Pass 序列**。昇腾 2201 与昇腾 950（v35）指令集不同、支持的优化不同，所以有两个 runner，各自排自己的顺序。

#### 4.3.2 核心流程

两个平台的 Pass 序列对比（顺序即执行顺序）：

| 顺序 | PassRunnerV1（2201） | PassRunnerV2（v35/950） | 说明 |
|------|---------------------|------------------------|------|
| 1 | PowEquivSubstitution | PowEquivSubstitution | 相同 |
| 2 | BroadcastConstToStore | BroadcastConstToStore | 相同 |
| 3 | ScalarTo1DTensor | ScalarTo1DTensor | 相同 |
| 4 | ScalarBroadcastOptimization | ScalarBroadcastOptimization | 相同 |
| 5 | MaskedFillInputReorder | MaskedFillInputReorder | 相同 |
| 6 | ExpandDimsForAllReduce | ExpandDimsForAllReduce | 相同 |
| 7 | —（无） | **ContinuesBroadcastOptimization** | v35 专属：连续广播优化 |
| 8 | —（无） | **SameSourceBroadcastCse** | v35 专属：同源广播 CSE |
| 9 | DuplicateElewiseCse | DuplicateElewiseCse | 相同（V1 在 7，V2 在 9） |
| 10 | —（无） | **GatherToLoad** | v35 专属：Gather 转 Load |
| 11 | —（无） | **SplitConcatOptimization** | v35 专属：Split/Concat 优化 |

可以看到设计上的两点：① 前 6 个公共 Pass 的**相对顺序**在两个平台完全一致；② v35 平台额外插入了 4 个专属 Pass，其中两个（ContinuesBroadcastOptimization、SameSourceBroadcastCse）插在 CSE **之前**，两个（GatherToLoad、SplitConcatOptimization）插在 CSE **之后**。这些插入位置都是经过依赖分析的有意安排。

#### 4.3.3 源码精读

**① V1 平台（2201）的固定序列**：

[pass_runner_v1.h:26-34](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/platform/v1/pass_runner_v1.h#L26-L34)：构造函数里依次 `RegisterPass` 了 7 个 Pass，顺序就是上文表格的顺序。注意这是 `final` 类，继承自 `BasePassRunner`，构造时就一次性把「播放列表」填好。

**② V2 平台（v35）的固定序列**：

[pass_runner_v2.h:30-42](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/v35/optimize/pass_runner_v2.h#L30-L42)：前 6 个 `RegisterPass` 调用与 V1 **逐字相同**（L31-36），随后插入 v35 专属的 4 个（L37-38、L40-41），CSE 夹在中间（L39）。这印证了「公共 Pass 顺序跨平台一致，平台 Pass 按需插入」的设计。

**③ 平台如何被选用**：runner 是通过 `platform->GetPassRunner()` 取得的（见 4.1）。`PlatformV1` 与 `PlatformV2` 各自 override 了 `GetPassRunner()`，返回自己的 runner 类型，所以「跑哪套 Pass」最终由 `PlatformFactory` 选出的平台决定。

#### 4.3.4 代码实践

**实践目标**：解释为什么 Pass 顺序需要固定（对应本讲规格里实践任务的后半部分）。

**操作步骤**：

1. 打开 [pass_runner_v1.h:26-34](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/platform/v1/pass_runner_v1.h#L26-L34)。
2. 做一个**思想实验**：假设把 `ScalarBroadcastOptimizationPass`（顺序 4）和 `DuplicateElewiseCsePass`（顺序 7）对调。思考：
   - 如果 CSE 先跑，它看到的图里还留着标量广播节点，会不会把「广播 + 某算子」误判为可合并？
   - 等到广播优化 Pass 后跑、删掉广播节点时，CSE 刚建的重连关系是否还自洽？
3. 写出你的结论：顺序固定的根本原因是什么。

**需要观察的现象**：两个 Pass 的改写动作（删节点 vs 合并节点）都会**改图的拓扑**，谁先谁后会让另一方看到不同的图。

**预期结果（参考答案）**：顺序必须固定，因为 Pass 之间存在**数据依赖**——后一个 Pass 依赖前一个 Pass 把图规整成它假设的形态。一旦重排，某个 Pass 的命中条件（如「轴为空」「无冗余广播」）就可能不再成立，导致改写结果不一致，破坏「图改写确定性」这条红线。

#### 4.3.5 小练习与答案

**练习 1**：为什么 V2 把 `ContinuesBroadcastOptimization`、`SameSourceBroadcastCse` 放在 `DuplicateElewiseCse` **之前**（见 [pass_runner_v2.h:37-39](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/v35/optimize/pass_runner_v2.h#L37-L39)）？
**答案**：这两个 v35 专属 Pass 会先把广播链规整/合并掉，让图里「广播相关」的冗余在进入通用 CSE 之前就被消解。若反过来，通用 CSE 可能基于「还有广播节点」的旧形态做合并，等广播 Pass 再改图时就产生了不一致。先特化、后通用，能保证 CSE 在更干净的图上工作。

**练习 2**：项目要求「图改写确定性」。从代码层面看，runner 用了哪些手段来保证这一点？
**答案**：① Pass 顺序硬编码在构造函数的 `RegisterPass` 调用序列里，运行期不可变；② `BasePassRunner::RunPasses` 是确定性的顺序 for 循环，无随机、无并发；③ CSE 等 Pass 内部用 `std::map`（有序）和「选 OpDesc ID 最小节点作正本」这类确定性规则（见 [duplicate_elewise_cse_pass.cpp:62-66](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/graph_pass/duplicate_elewise_cse_pass.cpp#L62-L66)），避免遍历顺序影响结果。

---

## 5. 综合实践

**任务**：跑一个真实的 Pass 单测，用 BEFORE/AFTER 两张图来印证「Pass 把 Pow 换成了更便宜的算子」，并把这一过程串起本讲的三个最小模块（调度机制、典型 Pass、顺序确定性）。

**背景**：项目里已有针对 Pass 的单测，位于 [tests/ut/optimize/test_optimizer.cpp](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/tests/ut/optimize/test_optimizer.cpp)。其中 `PowEqivCase` 系列构造了一张含多个不同指数 `Pow` 的图，直接调用 `PassRunnerHandler().RunPasses(graph)`，并断言改写后某些 `pow` 节点消失了（见 [test_optimizer.cpp:5788-5802](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/tests/ut/optimize/test_optimizer.cpp#L5788-L5802)）。

**操作步骤**：

1. **编译并运行这个 UT**（参照 u1-l3 与 AGENTS.md，注意限并行度防 OOM）：

   ```bash
   sh build.sh -u --module=autofuse_framework --impl=cpp -j 8
   ```

   再用 ctest/gtest 过滤跑 `PowEqiv*` 用例（具体过滤命令以本机 gtest 用法为准，**待本地验证**）。

2. **打开 DFX 观察 BEFORE/AFTER**：测试代码里调了 `DumpGraph(graph, "BEFORE")` 和 `DumpGraph(graph, "AFTER")`（[test_optimizer.cpp:5787](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/tests/ut/optimize/test_optimizer.cpp#L5787) 与 [L5790](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/tests/ut/optimize/test_optimizer.cpp#L5790)）。在 dump 目录里对比这两张图。

3. **回答串联三个模块的问题**：
   - **调度机制**：`PassRunnerHandler().RunPasses(graph)` 这一调用，是怎么最终落到 `PowEquivSubstitutionPass::RunPass` 的？（提示：回顾 4.1 的调用链，并注意这里没指定平台，平台由环境决定。）
   - **典型 Pass**：BEFORE 里的 `pow0..pow4`（指数分别为 2、0.5、-0.5、3、4 等）在 AFTER 里分别变成了什么算子？对应 [pow_equiv_substitution_pass.h:18-29](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/optimize/graph_pass/pow_equiv_substitution_pass.h#L18-L29) 的哪条规则？
   - **顺序确定性**：为什么 `PowEquivSubstitution` 必须排在第一个？如果它排在 `ScalarTo1DTensorPass` 之后，Pow 换出来的 sqrt/mul 节点会不会因为「没有轴」而被后续 Pass 误处理？

**需要观察的现象**：AFTER 图里 `pow*` 节点数量减少甚至归零（[test_optimizer.cpp:5791-5800](https://github.com/gitcode.com/cann/graph-autofusion/blob/00627d97bf898d8331ec5189f93a7621294f9121/autofuse/tests/ut/optimize/test_optimizer.cpp#L5791-L5800) 断言它们为 `nullptr`），取而代之的是 `sqrt`/`mul`/`reciprocal` 等更轻的算子。

**预期结果**：你能用一张「调用链 + BEFORE/AFTER diff + 顺序理由」的图，把本讲三个模块串成一条完整的故事。

> 说明：本实践需要真实编译环境与昇腾工具链，若本机不具备，可退化为「源码阅读型实践」——只做步骤 3 的源码分析与断言阅读，不实际编译。

## 6. 本讲小结

- **Pass 是对整张图做一次扫描+改写的最小单元**，全部继承 `BaseGraphPass`、实现纯虚 `RunPass(graph)`。
- **调度机制分三层**：静态入口 `PassRunnerHandler`（平台无关）→ 平台 `GetPassRunner()` 分发 → `BasePassRunner` 用顺序 for 循环逐个执行；具体装哪些 Pass 由各平台 runner 的构造函数决定。
- **「Pass 定义」与「Pass 注册」分离**：`graph_pass/` 只定义 Pass 是什么，`platform/` 的 runner 决定跑哪些、什么顺序。`SoftmaxPatternFusionPass` 还示范了「Pass 接口被 runner 之外单独借用」的用法。
- **7 个公共 Pass 各管一类低效**：Pow 等价替换、常量 Store 补广播、标量补一维轴、删多余标量广播、MaskedFill→Select、全归约补轴、Sub 的 CSE。
- **Pass 顺序硬编码、不可重排**，因为 Pass 之间存在数据依赖；这是项目「图改写确定性」红线的落地。
- **两个平台有两套序列**：V1（2201）7 个，V2（v35）11 个（前 6 个相同，额外插入 4 个 v35 专属 Pass）。

## 7. 下一步学习建议

- **下一讲 u6-l3（AutoSchedule 自动调度与 tiling 生成）**：Pass 把图规整干净后，就要交给 AutoSchedule 做轴分组与 tiling。建议先理解本讲「Pass 输出的图形态」如何影响下游调度。
- **若想扩展 Pass**：阅读 `base_graph_pass.h` 与任意一个现有 Pass 的 `.cpp`，仿照其 `RunPass` 结构新写一个；然后在 `pass_runner_v1.h` / `pass_runner_v2.h` 里决定它的插入顺序——务必想清它与前后 Pass 的依赖。
- **若关心平台扩展**：对照本讲的 V1/V2 runner，去看 u11-l1（v35 平台扩展机制），理解 v35 那 4 个专属 Pass 是如何随平台启用的。
- **精度与确定性**：本讲多次提到「图改写确定性」，可在 u12-l3（编码红线、贡献规范）里读到这条红线的完整定义与检查流程。
