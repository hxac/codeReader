# Reduce 模块：归约图分析

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 ATVOSS 为「带 `ReduceSum`/`Broadcast` 的表达式」准备了一套**与逐元素（Elewise）图分析不同**的专用图模块，并理解它要解决的核心矛盾：**归约会改变张量形状**。
- 看懂 `include/reduce/graph/` 三个文件（`dag.h` / `helper.h` / `buffer.h`）各自的职责，并能复述 `ReduceAutoDag` 把表达式切成 **PreReduce / Reduced 两段**的流程。
- 掌握 `InsertCopyIn` / `InsertCopyOut` 为什么要在归约边界**重新插入拷贝**，以及 `IsNodeUsedAfter` 这套**活跃性分析**如何决定一个节点的缓冲能否被复用/释放。
- 理解 `BufferIdGenerator` 这台「编译期寄存器分配器」如何为归约场景分配 ping/pong 双缓冲，特别是 `BUF_REDUCE_VAR` 这一档。

> 一个必须先建立的认知（本讲会在 §4.1 展开）：在本手册所基于的 HEAD（`9998053`）下，公共入口 `atvoss.h` 并未包含 `reduce/graph/*`，`rms_norm` 样例实际走的是 Elewise 的 `FullAutoDag`（见 u3-l3）。`reduce/graph` 是一套**更专门的归约图分析**，理解它既是读懂「ATVOSS 为归约算子预留的优化路径」的钥匙，也是一次极好的「编译期类型元编程做图分析」的练习。模块是否已在某条调度路径上启用，**待本地确认**。

## 2. 前置知识

本讲默认你已经读过（至少理解结论）：

- **u2-l4 归约与广播算子**：`ReduceSum<Pattern>` / `Broadcast<Pattern>` 是带编译期 `Pattern`（AR/RA/AB/BA）模板参数的 `UnaryOp`，归约/广播当前只面向**二维 Tile** `{axis0, axis1}`。
- **u3-l3 计算图构建：DAG 与 Bind**：Elewise 的 `FullAutoDag` 用「原地参数拆分 → 插 CopyIn/CopyOut → `OpAssign2Bind` → 依赖拓扑排序 → `DagNodeInfo` 存活分析」六步把表达式列表变成可调度序列；`Bind` 以 use→def 链编码依赖。
- **u3-l5 Buffer 管理与双缓冲**：UB 被切成 ping/pong 双槽，`bufferId` 粒度的 Mutex/PipeBarrier 让相邻 Tile 的搬入与计算重叠。

补两个本讲会用到的术语：

- **活跃性（liveness）**：一个变量/参数从「被定义」到「最后一次被使用」之间的这段区间。如果它在这区间之后还要被用到，就说它「仍然存活（still live）」，它的缓冲就不能给别人复用。
- **形状改变（shape-changing）算子**：逐元素算子（`Add`/`Mul`/`Sqrt` …）输入输出形状一致；而 `ReduceSum<AR>` 把 `[axis0, axis1]` 压成 `[axis0, 1]`，`Broadcast<AB>` 又把 `[axis0, 1]` 扩回 `[axis0, axis1]`。这种「中途换形状」正是归约图分析要特殊处理的根因。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲角色 |
| --- | --- | --- |
| [include/reduce/graph/dag.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/reduce/graph/dag.h) | 归约专用 DAG 的「编排器」：`InsertCopyIn`/`InsertCopyOut`、`ReduceAutoDag`（切 PreReduce/Reduced 两段） | **主角**：模块 4.1、4.2 |
| [include/reduce/graph/helper.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/reduce/graph/helper.h) | 图分析原语：`IsNodeUsedAfter` 活跃性分析、`IsReduceOp`/`ContainsReduceOp` 归约识别、`FindLocalVarReferences` 依赖闭包、`SortExprsByOutput` 排序 | **主角**：模块 4.3，并为 4.1 提供依赖分析 |
| [include/reduce/graph/buffer.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/reduce/graph/buffer.h) | 归约缓冲分配：`CanRelease`、`ReleaseCurrentInputs`、`GenerateBufferIdOrderAux` 编译期寄存器分配、`BufferIdGenerator` | **主角**：模块 4.4 |
| [include/graph/expr_operations.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/graph/expr_operations.h) | 公共原语：`ExtractInputs`（取一条语句的所有叶子输入）、`ContainsNodeInExpr`（判断节点是否出现在表达式里） | reduce 模块复用的「底层工具」 |
| [include/elewise/block/schedule.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h) | 当前公共管线里真正被实例化的 `DagSelector`/`PreProcessComputeExpr`，选 `FullAutoDag` 或 `ManualDag` | **对照**：证明当前 HEAD 的活路径是 Elewise DAG |
| [include/operators/transcendental_evaluator.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/transcendental_evaluator.h) | `Evaluator<OpAssign<T, OpReduceSum/OpBroadcast<...>>>`，把归约/广播表达式落到 `AscendC::ReduceSum`/`Broadcast` | **对照**：无论哪条图路径，最终执行都靠它 |

---

## 4. 核心概念与源码讲解

### 4.1 Reduce 专用 DAG 构建：为什么归约要单列一套图

#### 4.1.1 概念说明

回顾 u3-l3：Elewise 的 `FullAutoDag` 假设整条表达式都是**等形状**的逐元素运算，所以它能用一套统一的 ping/pong 缓冲、一条统一的依赖链把所有语句排成一条线性序列。

一旦表达式里出现 `ReduceSum`，这个假设就被打破：

- 归约之前的数据是「满形状」`[axis0, axis1]`，归约之后变成「缩减形状」`[axis0, 1]`。
- `AscendC::ReduceSum` 这类 API 通常要求**源数据独占一块连续 UB**，且归约结果要落到**另一块**缓冲里——它不能像逐元素算子那样「原地算完就地复用」。
- 归约之后，常常还有一串依赖归约结果的计算（如 `rms_norm` 里 `ReduceSum → Broadcast → Divs → Sqrt → 乘除`），它们消费的是缩减形状的数据。

`reduce/graph` 模块的核心设计思想因此是：**把整条表达式沿「归约」切成两段**。

- **PreReduce 段**：归约之前的满形状计算（如 `temp = in1 * in1`）。
- **Reduced 段**：归约算子本身，加上所有依赖归约结果的后续计算。

两段各自拥有**独立的缓冲分配**（两个 `BufferIdGenerator`），从而让满形状与缩减形状各自用各自的 UB 布局，互不串扰。这正是 `ReduceAutoDag` 这个名字的由来——它是「为归约量身定制的全自动 DAG」。

> ⚠️ 准确性提示：在本讲基于的 HEAD，`reduce/graph/dag.h` 没有被任何其他文件 `#include`（可用 `grep -r "reduce/graph" include/` 验证），公共 `atvoss.h` 只引入 `elewise/*`。因此 `ReduceAutoDag` 是一套**已实现但未接入公共活路径**的专用分析。把它当作「ATVOSS 为归约设计的优化路径 + 一次编译期图分析的范例」来读，是最稳妥的姿态。是否已在某条非默认调度上启用，**待本地确认**。

#### 4.1.2 核心流程

`ReduceAutoDag<ExprList, IsBinaryAcc>` 的三步流程（伪代码）：

```
输入：ExprList = [stmt₀, stmt₁, ..., stmtₙ₋₁]   # 一条条 OpAssign
     IsBinaryAcc                              # 是否二元累加（影响归约缓冲双缓冲）

# Step 1：识别归约算子
ReduceOpList   = ExprList 里「含 ReduceOp」的语句            # 如 out = ReduceSum<AR>(temp)
ReduceOpResList = 上面这些语句的输出                          # 如 {out}

# Step 2：找出所有「依赖归约输出」的语句（前向依赖闭包）
ReduceRefList  = 从 ReduceOpList 出发，递归找出所有 RHS 引用了
                 归约输出（或归约输出之派生量）的语句
Reduced 段     = ReduceOpList ∪ ReduceRefList                 # 归约 + 归约之后

# Step 3：剩下的是 PreReduce 段
PreReduce 段   = ExprList − ReduceRefList                     # 归约之前（仍含 ReduceOpList）

# 各段独立插入 CopyIn / CopyOut、独立做缓冲分配
PreReduceList  = InsertCopyOut(InsertCopyIn(PreReduce 段))
ReducedList    = InsertCopyOut(InsertCopyIn(Reduced 段))
PreReduceBuf   = BufferIdGenerator(PreReduce 段 去掉 ReduceOpList, ReduceOpList, IsBinaryAcc)
ReducedBuf     = BufferIdGenerator(Reduced 段,               ReduceOpList, false)
```

注意一个关键点：**PreReduce 段在 Step 3 里仍包含 `ReduceOpList`**（因为减去的是 `ReduceRefList`），但在做缓冲分配时又把它「去掉」了（`PreReduceWoReduceCopyInList`）。这是因为归约算子本身的缓冲要由 Reduced 段的 `BufferIdGenerator` 用 `BUF_REDUCE_VAR` 双缓冲专门管理，不能混进 PreReduce 的普通缓冲池。

#### 4.1.3 源码精读

`ReduceAutoDag` 的三步都写在它的 `private` 区，对应源码注释里的「Step 1/2/3」：

[include/reduce/graph/dag.h:183-198](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/reduce/graph/dag.h#L183-L198) —— `ReduceAutoDag` 的 Step 1~3：

- L187 `ReduceOpList = Filter_t<ContainsReduceOp, ExprList>`：用 `ContainsReduceOp`（见 4.3.3）筛出所有归约语句。
- L190 `ReduceRefListRaw = FindLocalVarReferences<ReduceOpList, ExprList, ReduceOpResList>`：从归约语句出发，在整条 `ExprList` 里做**依赖闭包**查找，找出所有「直接或间接用到归约输出」的语句（`FindLocalVarReferences` 的细节见 4.1.3 末尾）。
- L191 `ReduceRefList = SortExprsByOutput<ReduceRefListRaw>`：把这些语句按「LocalVar 在前（按序号）、Param 在后」排序，让缓冲分配有一个稳定的顺序。
- L196 `PreReduceWithReduceList = Difference_t<ExprList, ReduceRefList>`：整段减去「依赖归约的语句」= 归约之前的语句（仍含归约算子本身）。
- L198 `PreReduceWoReduceCopyInList = Difference_t<PreReduceWithReduceCopyInList, ReduceOpList>`：再做一次差集，**把归约算子从 PreReduce 缓冲分配里剔除**。

两段的结果与缓冲生成器在 `public` 区暴露：

[include/reduce/graph/dag.h:200-206](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/reduce/graph/dag.h#L200-L206) —— 分别为 PreReduce 与 Reduced 各产出一份「带 CopyIn/CopyOut 的语句序列」与一份「缓冲 ID 表」，且 PreReduce 的 `BufferIdGenerator` 传 `IsBinaryAcc`、Reduced 的传 `false`（归约缓冲的双缓冲只在 PreReduce 端按需开启，见 4.4）。

随后是一组 `GetMTE2Num<IsPre>` / `GetMTE3Num<IsPre>` / `GetTempBufNum<IsPre>` / `GetMaxDTypeSize<IsPre>` 等访问器：

[include/reduce/graph/dag.h:220-296](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/reduce/graph/dag.h#L220-L296) —— 用 `if constexpr (IsPre)` 在两段之间二选一，把 MTE2（搬入）/MTE3（搬出）/临时缓冲的数量、最大最小数据类型字节数分别报给上层，用于在编译期算出每段该占多少 UB。

支撑 Step 2 的「依赖闭包」查找是本模块最复杂的元函数之一：

[include/reduce/graph/dag.h:140-181](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/reduce/graph/dag.h#L140-L181) —— `FindLocalVarReferences`：给定一组「起点语句」（这里是归约语句），它反复用 `FilterRefLocalVar`（谁引用了我的输出？）做**前向**扩散，再用 `FindAllUnhandledInputs`（我的输入是谁定义的？）做**后向**扩散，递归地把整片「与归约有数据依赖」的语句收集起来。它同时维护一个 `ExcludeList` 避免重复访问，最终 `Unique_t` 去重。这正是「Reduced 段」的来源。

#### 4.1.4 代码实践

**实践目标**：用 `rms_norm` 的真实表达式，手工把语句分到 PreReduce / Reduced 两段，验证你对 `ReduceAutoDag` Step 1~3 的理解。

**操作步骤**：

1. 打开 [tests/st/test_tile_rms_norm_3.cpp:46-51](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/st/test_tile_rms_norm_3.cpp#L46-L51)，它的 `Compute()` 写成一条逗号表达式（会被 `FlattenAtOpAndThen` 拍成 `OpAssign` 列表）：

   ```
   temp = in1 * in1,                         # S0
   out  = ReduceSum<Atvoss::Pattern::AR>(temp),  # S1（归约）
   out  = Broadcast<Atvoss::Pattern::AB>(out),   # S2
   temp = Divs<TileLen>(out),                # S3
   out  = Sqrt(temp),                        # S4
   temp = in1 / out,                         # S5
   out  = in2 * temp                         # S6
   ```

2. 按 §4.1.2 的规则手工切分：
   - **Step 1**：`ReduceOpList = {S1}`，`ReduceOpResList = {out}`（注意 S1 的 LHS 是 `out`）。
   - **Step 2**：从 `out` 出发做依赖闭包。S2 的 RHS 用了 `out` → 收入；S3 用了 `out`(经 S2)→ 收入；S4 用了 `temp`(S3 产出)→ 收入；S5 用了 `out`(S4 产出)和 `in1`→ 收入；S6 用了 `temp`(S5 产出)→ 收入。故 `ReduceRefList = {S2,S3,S4,S5,S6}`。
   - **Step 3**：`PreReduce = ExprList − ReduceRefList = {S0, S1}`；做缓冲分配时再剔除 `S1`，得 PreReduce 缓冲段 `{S0}`。

**需要观察的现象**：`in1` 同时出现在 PreReduce 段（S0）和 Reduced 段（S5）。这正是下一节「为什么要在边界重新插 CopyIn」的直接原因。

**预期结果**：你应当得到「PreReduce 段只含 `temp = in1*in1`，Reduced 段含归约及其后所有 5 条语句」的结论，与 §4.1.2 的伪代码完全对应。

**待本地验证**：因 `ReduceAutoDag` 当前未接入公共管线，无法直接打印其切分结果；如需验证，可写一个最小 host 程序，手动用 `using D = Atvoss::Reduce::Graph::ReduceAutoDag<...>` 实例化并触发一个 `static_assert`/编译期错误来观察类型（参考 u3-l10 的 `compile_perf` 测试思路）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `rms_norm` 改成 `out = in2 * Broadcast<AB>(Sqrt(Divs<WIDTH>(ReduceSum<AR>(in1*in1))))`（不引入任何 `LocalVar`/`temp`，全部内联成一棵嵌套表达式），`ReduceAutoDag` 的 Step 1 还能找到归约算子吗？

**答案**：能。表达式先会被线性化（u3-l4）拆成一串 `OpAssign`（框架会自动引入 `LocalVar` 接中间结果），所以到达 `ReduceAutoDag` 时它仍是「一条条语句」的列表，`Filter_t<ContainsReduceOp, ExprList>` 照样能命中那条 `out = ...OpReduceSum...`。`ContainsReduceOp` 是沿 `OpAssign` 递归判断 RHS 是否含 `ReduceOp` 的（见 4.3.3），与是否手写 `temp` 无关。

**练习 2**：为什么 PreReduce 段在 Step 3 里要「先保留 `ReduceOpList`、做缓冲分配时再剔除」，而不是一开始就剔除？

**答案**：因为 Step 2 的依赖闭包查找需要把「归约算子的输出」当作起点；如果把归约算子提前剔除，就找不到「谁依赖归约结果」了。所以归约算子在「图结构分析」阶段必须留下，只在「PreReduce 缓冲分配」阶段（它由 Reduced 段的 `BUF_REDUCE_VAR` 专门管理）才剔除。

---

### 4.2 InsertCopyIn / InsertCopyOut：归约边界为什么要把拷贝「再做一遍」

#### 4.2.1 概念说明

回忆 u3-l3：Elewise 的 `FullAutoDag` 也插 CopyIn/CopyOut，但它是**整条表达式只插一次**——每个 IN 参数在首次使用前插一次 CopyIn，每个 OUT 参数在末次使用后插一次 CopyOut。

归约模块的 `InsertCopyIn` 做的是**同一件事，但作用域是「一段」**。因为 PreReduce 和 Reduced 是两段**独立的缓冲分配**，一个在两段里都用到的 IN 参数（如 `rms_norm` 的 `in1`）必须在**每一段的首次使用处各插一次 CopyIn**。从整条表达式看，这就像是「在归约边界把 `in1` 又搬了一次」。

为什么要这么「浪费」？因为两段的 UB 布局完全独立：PreReduce 段按满形状 `[axis0, axis1]` 分配缓冲，Reduced 段按缩减形状分配缓冲；`in1` 在两段的缓冲槽、ping/pong 编号都不一样。唯一的办法就是在段的入口重新 `DataCopyPad` 一次，让两段各自从自己的缓冲起步。这是用一次额外搬运换「两段缓冲管理彻底解耦」的刻意取舍。

#### 4.2.2 核心流程

`InsertCopyIn` 是一个左折叠的编译期递归，状态是「已经处理过哪些 IN 参数」：

```
InsertCopyIn(ExprList, ProcessedParams = {}, pos = 0):
    if pos == len(ExprList): return []
    stmt = ExprList[pos]
    inputs = Unique(ExtractInputs(stmt.RHS))        # 这条语句用到的所有 IN 叶子
    toCopy = [p ∈ inputs | IsInParam(p) ∧ p ∉ ProcessedParams]   # 还没搬过的 IN 参数
    inserted = [ (p = OpCopyIn(p))  for p in toCopy ]            # 为每个首次使用的 IN 生成一条搬入语句
    return inserted + [stmt]                                      # 搬入语句插在本条语句【之前】
         + InsertCopyIn(ExprList, ProcessedParams ∪ inputs, pos+1)
```

要点：

- **去重**：用 `ProcessedParams` 记账，同一个 IN 参数在**本段内**只搬一次（首次使用处）。
- **插在前**：搬入语句排在 `stmt` 之前，保证 `stmt` 执行时数据已在 UB。
- **逐段调用**：`ReduceAutoDag` 对 PreReduce 段和 Reduced 段**各调用一次** `InsertCopyIn`，于是跨段的 IN 参数被搬两次。

`InsertCopyOut` 更简单：对每条 LHS 是 OUT 参数的语句，在其**之后**追加一条 `OpCopyOut`。

#### 4.2.3 源码精读

[include/reduce/graph/dag.h:36-100](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/reduce/graph/dag.h#L36-L100) —— `InsertCopyIn`：

- L42 `CurrentInputs = Unique_t<ExtractInputs<InputsType>::Type>`：用公共原语 `ExtractInputs`（见 4.3.3 同名小节）抽出当前语句 RHS 的全部叶子输入，再 `Unique_t` 去重。
- L54-57 `NeedInsertCopyIn`：判定条件是「是 IN 参数」且「尚未处理」（`IsInParam` ∧ `!IsProcessed`）。
- L64-69 `InsertCopyInIfNeeded`：对需要搬入的参数，生成 `OpAssign<Param, OpCopyIn<Param>>` 这条语句。
- L88-94：把生成的搬入语句 `InsertedOps` 拼到当前语句**前面**，再把 `CurrentInputs` 并入 `NewProcessedParams` 传给下一位递归。终止条件在 L97-100（`pos == Size` 返回空表）。

[include/reduce/graph/dag.h:102-119](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/reduce/graph/dag.h#L102-L119) —— `InsertCopyOut`：L108-109 用 `IsOutParam<Output>` 判定 LHS 是否为 OUT 参数，是则在语句后追加 `OpAssign<Output, OpCopyOut<Output>>`。

[include/reduce/graph/dag.h:121-125](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/reduce/graph/dag.h#L121-L125) —— `InsertCopyInOut`：先 `InsertCopyIn` 再 `InsertCopyOut` 的组合。

回到 `ReduceAutoDag`，注意 L192 与 L197 **分别**对两段调用了 `InsertCopyIn`：

[include/reduce/graph/dag.h:192-197](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/reduce/graph/dag.h#L192-L197) —— `ReduceRefCopyInList` 给 Reduced 段插 CopyIn，`PreReduceWithReduceCopyInList` 给 PreReduce 段插 CopyIn。这就是「跨段 IN 参数被搬两次」的代码落点。

#### 4.2.4 代码实践

**实践目标**：以 `_1 = ReduceSum<AR>(in1*in1)` 这一「平方后整行求和」为例，说明为何归约前后会出现额外 CopyIn。

**操作步骤**：

1. 用 §4.1.4 切好的两段：PreReduce `{S0: temp = in1*in1}`、Reduced `{S1..S6}`。
2. 对 PreReduce 段跑 `InsertCopyIn`：S0 的输入是 `{in1}`，`ProcessedParams` 为空 → 在 S0 前插入 `in1 = OpCopyIn(in1)`。
3. 对 Reduced 段跑 `InsertCopyIn`：S5 `temp = in1/out` 又用到 `in1`，但这是**另一段**，`ProcessedParams` 重新从空开始 → 在 S5 前再次插入 `in1 = OpCopyIn(in1)`。

**需要观察的现象**：整条表达式里，`in1 = OpCopyIn(in1)` 出现了**两次**（一次在 S0 前，一次在 S5 前）。这就是「归约边界的额外拷贝」。

**预期结果**：你能解释——因为 PreReduce 段按 `[axis0, axis1]` 满形状分配 UB，Reduced 段按缩减形状分配 UB，`in1` 在两段的物理缓冲槽不同，所以必须在段入口各 `DataCopyPad` 一次。额外的一次 GM→UB 搬运，换来的是两段缓冲管理的完全解耦与归约所需的独占缓冲。

**待本地验证**：同 4.1.4，模块未接入活路径，结论基于源码逻辑推导。

#### 4.2.5 小练习与答案

**练习 1**：`InsertCopyIn` 为什么用 `ProcessedParams` 做去重，而不是简单地「每条语句的每个 IN 输入都插一次」？

**答案**：同一段内，一个 IN 参数可能被多条语句使用（如 `in1` 在 S0 和假设的别的满形状语句里都用）。如果每条都插一次 CopyIn，同一个参数会被重复从 GM 搬到 UB，既浪费带宽也容易和上一份缓冲产生竞争。`ProcessedParams` 保证「本段首次使用处搬一次，之后复用同一块 UB」。

**练习 2**：标量参数（scalar）会被 `InsertCopyIn` 搬运吗？

**答案**：不会。`IsInParam`/`NeedInsertCopyIn` 只针对张量类型的 IN 参数；标量在 u2-l6/u3-l2 里是直接透传进 Device 的，不占 GM、不需 CopyIn。（对照 Elewise 的 `AddCopyX` 也有 `is_scalar_v` 跳过分支。）

---

### 4.3 IsNodeUsedAfter：节点使用分析与活跃性

#### 4.3.1 概念说明

`IsNodeUsedAfter` 回答一个极其重要的问题：**从第 `start` 条语句往后，某个节点还会被用到吗？**

这是一个典型的**活跃性查询**。它的输出直接驱动两个决策：

1. **缓冲能否释放**：如果一个节点的最后一次使用已经过去（`IsNodeUsedAfter == false`），它占的 UB 缓冲就可以归还给「空闲池」给别人复用（见 4.4）。
2. **归约边界是否需要保活**：如果一个 IN 参数在归约之后还要被用到（`IsNodeUsedAfter == true`），它的缓冲就不能在归约前释放——这正是 §4.2 要靠「重新 CopyIn」来解耦的根因。

#### 4.3.2 核心流程

```
IsNodeUsedAfter(ExprList, TargetNode, start, current = 0):
    if ExprList 为空: return false
    First, Rest = ExprList 的头与尾
    needCheck   = (current >= start)                       # 还没走到 start 之前不查
    currentCheck = needCheck ∧ ContainsNodeInExpr(First.RHS, TargetNode)
    return currentCheck ∨ IsNodeUsedAfter(Rest, start, current+1)
```

关键三点：

- `start` 是「从哪条语句开始往后看」，通常是「当前语句的下一条」`pos+1`。
- 只检查每条语句的 **RHS**（即「被使用」的一侧），不看 LHS（LHS 是「被定义/赋值」，不算「使用」）。
- `ContainsNodeInExpr` 沿表达式树递归判断 `TargetNode` 是否出现（见 4.3.3）。

#### 4.3.3 源码精读

[include/reduce/graph/helper.h:31-49](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/reduce/graph/helper.h#L31-L49) —— `IsNodeUsedAfter`：

- L34-37 空表特化 → `false`（基线：走到头还没命中，就是没人用）。
- L42 `needCheck = (current >= start)`：只从 `start` 起才真正检查。
- L43-44 `currentCheck = needCheck ? ContainsNodeInExpr<First::RhsType, TargetNode> : false`：核心判断，复用公共原语。
- L45-48 递归尾 `Rest`，`current` 自增，结果取「或」。

`ContainsNodeInExpr` 与 `ExtractInputs` 都来自公共头 `include/graph/expr_operations.h`（reduce 模块 `using` 引入）：

[include/graph/expr_operations.h:22-62](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/graph/expr_operations.h#L22-L62) —— `ExtractInputs`：把一条 `OpAssign` 的 RHS 递归拆成全部叶子（`Param`/`LocalVar`）列表，对 `Op<Inner>`、`Op<Pattern,Inner>`、`Op<T,U>`、`Op<T,U,V>` 都有特化，把子树输入拼起来。

[include/graph/expr_operations.h:64-98](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/graph/expr_operations.h#L64-L98) —— `ContainsNodeInExpr`：沿同样的表达式骨架递归，只要任一子树命中 `TargetNode`（同类型即命中，L70-72）就返回 `true`；对 `OpAssign` 只看 RHS（L75-77），与「使用」语义一致。

模块内还有一组配套的图分析原语，值得知道它们各自管什么：

[include/reduce/graph/helper.h:56-69](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/reduce/graph/helper.h#L56-L69) —— `IsReduceOp`/`ContainsReduceOp`：用 `std::is_base_of_v<ReduceOp<typename T::DataType>, T>` 判断一个节点是不是归约算子，并能穿透 `OpAssign`/模板包装递归判定（驱动 §4.1 的 `ReduceOpList` 筛选）。

[include/reduce/graph/helper.h:79-88](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/reduce/graph/helper.h#L79-L88) —— `CheckReduceInput`/`IsDirectConnectToReduce`：判断某条语句的输出是否「直接喂给」某个归约算子。它在 4.4 里用来识别「紧贴归约的那个 LocalVar」是否需要 `BUF_REDUCE_VAR` 双缓冲。

[include/reduce/graph/helper.h:185-199](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/reduce/graph/helper.h#L185-L199) —— `SortExprsByOutput`：把语句按「LocalVar 在前（按 `number` 排）、Param 在后」排序，给缓冲分配一个稳定且「先算中间量、最后写输出」的顺序。

#### 4.3.4 代码实践

**实践目标**：用 `IsNodeUsedAfter` 的逻辑，判断 `in1` 在 `_1 = ReduceSum<AR>(in1*in1)` 这条归约语句之后是否还被使用，并据此推断它的缓冲释放时机。

**操作步骤**：

1. 沿用 §4.1.4 的 7 条语句 S0..S6，假设它们排成一条完整 `ExprList`。
2. 在 S0（`temp = in1*in1`）处查询 `IsNodeUsedAfter<ExprList, in1, start=1, current=0>`：从 S1 起往后看，`in1` 是否还出现在某条语句的 RHS？
3. 逐条核对：S1 RHS=`temp`（不含 in1）；S2=`out`；S3=`out`；S4=`temp`；**S5=`in1/out`（含 in1！）**；S6=`in2*temp`。

**需要观察的现象**：`current` 走到 S5 时，`ContainsNodeInExpr<S5.RHS, in1>` 为 `true`，整个递归立即返回 `true`。

**预期结果**：`IsNodeUsedAfter<ExprList, in1, 1>` == **true**。含义：`in1` 在归约（S1）之后**仍然存活**（S5 还要用），所以：

- 在 PreReduce 段里，`in1` 的缓冲**不能**在 S0 之后立刻释放；
- 这也解释了为什么 `reduce/graph` 要把表达式切段、并在 Reduced 段重新 CopyIn(in1)——因为 `in1` 跨越了形状改变边界仍然存活，单段缓冲无法既装满形状又装缩减形状，只能靠「重新搬入」让两段各自独立。

**待本地验证**：可用一个仅含这 7 条语句类型的最小 host 程序，`static_assert(IsNodeUsedAfter<...>::value)` 来编译期确认返回值。

#### 4.3.5 小练习与答案

**练习 1**：`IsNodeUsedAfter` 只查 RHS，不查 LHS。如果把「LHS 也算使用」会怎样？

**答案**：会得出过于乐观（保守）的存活结论。LHS 是「定义/赋值」，一个节点出现在 LHS 表示它在此处被**写**，而不是被**读**。缓冲能否释放只取决于「之后还有没有人读它」，所以只看 RHS 是正确的。若把 LHS 也算上，会让一些其实已无人读的节点被判为「仍存活」，导致缓冲迟迟不能复用，浪费 UB。

**练习 2**：`start` 参数为什么默认传 `pos+1`（下一条语句）而不是 `pos`（当前语句）？

**答案**：因为查询的语义是「**当前语句之后**还有没有人用」。当前语句本身已经在使用该节点（它出现在当前 RHS 里才触发查询），我们要知道的是「当前语句执行完、缓冲能否释放」，所以必须从下一条开始往后看。

---

### 4.4 归约缓冲复用判定：CanRelease、ReleaseCurrentInputs 与 BufferIdGenerator

#### 4.4.1 概念说明

知道了「谁还活着」（§4.3），下一步就是「据此怎么分配/回收 UB 缓冲」。`buffer.h` 实现了一台**编译期寄存器分配器**：逐条语句扫描，给每个新出现的节点分配一个 `bufferId`，把不再使用的节点的 `bufferId` 归还到「空闲池」供后续复用。

它对三类节点区别对待（`buffer.h` 顶部的三个位标志）：

| 标志 | 值 | 含义 | 缓冲策略 |
| --- | --- | --- | --- |
| `BUF_PARAM` | `0b001` | 入参/出参 `Param` | **独占双缓冲**（ping/pong 各一槽） |
| `BUF_LOCAL_VAR` | `0b010` | 普通临时变量 `LocalVar` | **可复用**（用空闲池里的旧槽） |
| `BUF_REDUCE_VAR` | `0b100` | 紧贴归约的 `LocalVar`（`IsBinaryAcc` 时） | **独占双缓冲**（像 Param 一样 ping/pong） |

为什么归约变量要享「双缓冲」待遇？因为归约是**累加型**计算，相邻 Tile 的归约输入需要在一块缓冲里就绪、归约结果落到另一块，用 ping/pong 才能隐藏搬运延迟——和入参的动机一致。而普通中间量算完即可丢弃，复用旧槽最省 UB。

#### 4.4.2 核心流程

`GenerateBufferIdOrderAux` 是核心循环（伪代码）：

```
for opPos, stmt in enumerate(ExprList):
    LHS = stmt.LHS; inputs = ExtractInputs(stmt.RHS)

    # 1) 这条语句的输出该用哪种缓冲？
    isLocalVar  = (LHS 是 LocalVar)
    isReduceVar = IsBinaryAcc ∧ isLocalVar ∧ IsDirectConnectToReduce(stmt)
    needDouble  = (not isLocalVar) ∨ isReduceVar      # Param 或归约变量 → 双缓冲
    canReuse    = isLocalVar ∧ (not isReduceVar)      # 普通临时量 → 可复用

    # 2) 选一个 bufferId
    if needDouble:                bufId = nextParamBufId        # 独占新槽
    elif canReuse ∧ freePool非空: bufId = freePool[0]           # 复用旧槽
    else:                         bufId = nextLocalBufId        # 新开临时槽

    # 3) 记账：把 (LHS → bufId, usage) 写进 BufferMap，更新各类 dtype 字节数极值

    # 4) 回收：对本条语句的每个输入，若 IsNodeUsedAfter(此后)==false 且是 LocalVar，
    #         把它的 bufId 还给 freePool
    for p in inputs:
        if CanRelease(p, opPos) ∧ 是LocalVar: freePool.add(bufId(p))

return AllocList（每个节点分配的 ping bufferId）,
       BufferMap（节点 → bufferId/usage 的映射）,
       各类 dtype min/max
```

`CanRelease<Node>` 的定义就一行：`IsNodeUsedAfter<ExprList, Node, opPos+1>::value == false`——把 §4.3 的活跃性查询直接接进来。

最终 `BufferIdGenerator` 把 `AllocList` 整理成一个 `int32_t[2][N]` 的 **ping/pong 二维 ID 表**：第 0 行是 ping ID，第 1 行是 pong ID。对 `BUF_LOCAL_VAR`，pong ID 就是它自己（单缓冲，无需双槽）；对 Param/ReduceVar，pong ID = ping ID + offset（双槽）。

#### 4.4.3 源码精读

[include/reduce/graph/buffer.h:54-59](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/reduce/graph/buffer.h#L54-L59) —— 三个 usage 位标志与 dtype 极值初值。注意初值的「反直觉」设定：`INIT_MAX_DTYPE_SIZE=1`、`INIT_MIN_DTYPE_SIZE=8`，目的是让后续 `Update*` 的比较从「最不利」起步，保证第一次遇到任何类型都能把它收进来。

[include/reduce/graph/buffer.h:61-67](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/reduce/graph/buffer.h#L61-L67) —— `CanRelease`：把「不再被后续使用」直接定义为 `IsNodeUsedAfter<ExprList, Node, pos+1, 0>::value == false`。

[include/reduce/graph/buffer.h:126-150](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/reduce/graph/buffer.h#L126-L150) —— `ReleaseCurrentInputs`：遍历当前语句的输入，仅当 `canRelease ∧ isLocalVar ∧ bufferId≥0` 时把该 `bufferId` 追加进空闲池（`Append_t<..., integral_constant<bufferId>>`）。注意它**只回收 LocalVar**——Param 的缓冲是独占的，不进空闲池。

[include/reduce/graph/buffer.h:169-187](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/reduce/graph/buffer.h#L169-L187) —— 三类缓冲的判定：`NeedsDoubleBuffer`（Param 或归约变量）、`CanReuseBuffer`（普通 LocalVar）。其中 [include/reduce/graph/buffer.h:174-177](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/reduce/graph/buffer.h#L174-L177) 的 `IsReduceVarBuffer()` 正是「`IsBinaryAcc ∧ 是LocalVar ∧ 紧贴归约`」三者同时成立才置位。

[include/reduce/graph/buffer.h:271-313](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/reduce/graph/buffer.h#L271-L313) —— `AllocBufferId`/`GetNextParamBufId`/`GetNextLocalBufId`：分配与游标推进逻辑。双缓冲走 `nextParamBufId` 且 +1；可复用且有空闲则取 `FreeBufferPool[0]` 且 `nextLocalBufId` 不动（复用不耗新槽）；否则取 `nextLocalBufId` 且 +1。

[include/reduce/graph/buffer.h:315-335](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/reduce/graph/buffer.h#L315-L335) —— 每轮的收尾：`UpdateFreePoolAfterAlloc`（复用时从空闲池移除刚分配的槽）、`NestBufferMap`（把新映射拼进 BufferMap）、`UpdatedFreePool`（调用 `ReleaseCurrentInputs` 回收本轮已不再使用的输入），然后把所有更新过的状态递归传给下一条语句。

[include/reduce/graph/buffer.h:425-515](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/reduce/graph/buffer.h#L425-L515) —— `BufferIdGenerator`：对外入口。[L429-433](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/reduce/graph/buffer.h#L429-L433) 算 `StartBufIdForLocalVar`：二元累加时，临时变量的起始 ID 要预留出「结果 Param 数 + 归约唯一输入数」的空间（`ResultParamCount + UniqueReduceInputCount_v<ReduceOpList>`），把前面的 ID 段留给 Param 与归约专用双缓冲。[L472-479](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/reduce/graph/buffer.h#L472-L479) `GetBufferIds()` 返回二维 ping/pong 表。

#### 4.4.4 代码实践

**实践目标**：把 `BUF_REDUCE_VAR` 的判定与 `CanRelease` 的回收串起来，预测 `_1 = ReduceSum<AR>(in1*in1)` 里「紧贴归约的 `temp`」会拿到哪种缓冲，以及 `in1` 何时能被回收。

**操作步骤**：

1. 在 PreReduce 段 `{S0: temp = in1*in1}` 后紧跟归约 `S1: out = ReduceSum<AR>(temp)`（归约算子在缓冲分析里属于 Reduced 段，但 `temp` 是它唯一的输入）。
2. 对 `temp`（LocalVar）查 `IsDirectConnectToReduce<ReduceOpList, S0>`：S0 的输出 `temp` 正是归约 S1 的输入 → 命中。若 `IsBinaryAcc=true`，则 `IsReduceVarBuffer()` 为真 → `temp` 被打上 `BUF_REDUCE_VAR`，走**双缓冲**（独占 ping/pong 两槽），保证归约累加时搬入与计算能重叠。
3. 对 `in1`（Param，BUF_PARAM）查 `CanRelease`：在 S0 处 `IsNodeUsedAfter<..., in1, S0之后>` —— 若只看 PreReduce 段，S0 之后无语句 → `false` → 可释放；但若看整条表达式，S5 还用 in1 → `true` → **不可释放**。

**需要观察的现象**：

- `temp` 因为「紧贴归约」从普通可复用临时量**升级**为双缓冲的归约变量。
- `in1` 是否能释放，**完全取决于查询作用域**（PreReduce 段内 vs 整条表达式）。`reduce/graph` 通过「切段 + 重新 CopyIn」让两段的回收判定互不干扰。

**预期结果**：你能说清——归约变量的双缓冲由 `IsReduceVarBuffer()` + `IsBinaryAcc` 控制；普通节点能否回收由 `CanRelease`（即 `IsNodeUsedAfter`）控制；Param 永不进空闲池。三者合起来就是这台编译期寄存器分配器的全部判据。

**待本地验证**：可写一个最小程序实例化 `BufferIdGenerator<...>` 并打印 `GetBufferIds()` 返回的二维数组（需借助编译期 `static_assert` 或模板实例化诊断）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `ReleaseCurrentInputs` 只回收 `LocalVar`，不回收 `Param`？

**答案**：Param 是入参/出参，它的缓冲在整个段内都可能被多次使用（且 ping/pong 双缓冲是为流水线重叠准备的固定槽），回收它会破坏双缓冲的稳定性，也容易和后续 CopyIn/CopyOut 冲突。LocalVar 是「算完即弃」的中间量，回收复用能显著省 UB。所以策略是「Param 独占、LocalVar 流通」。

**练习 2**：`IsReduceVarBuffer()` 要求 `IsBinaryAcc` 为真才把归约变量升级为双缓冲。若 `IsBinaryAcc=false`（`ReduceAutoDag` 给 Reduced 段的 `BufferIdGenerator` 正是传 `false`），会怎样？

**答案**：Reduced 段里的归约变量不会被判为 `BUF_REDUCE_VAR`，而是当作普通 `LocalVar` 走可复用路径。这与 §4.1 的设计一致：归约「累加」所需的双缓冲只在 PreReduce 端（`IsBinaryAcc` 可为真）按需开启；Reduced 段已是归约之后的后续计算，不再需要归约专用双缓冲，按普通临时量处理更省 UB。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「**给一段含归约的表达式，预测 reduce/graph 模块会对它做什么**」的端到端推演。

**任务**：给定表达式（已线性化为语句列表）

```
S0: t1 = in1 * in1
S1: t2 = ReduceSum<AR>(t1)
S2: t3 = Broadcast<AB>(t2)
S3: out = in1 / Sqrt(Divs<WIDTH>(t3))
```

请按下列顺序产出结论（全部基于源码逻辑，无需运行）：

1. **切段**（模块 4.1）：写出 `ReduceOpList`、`ReduceRefList`、`PreReduce 段`、`Reduced 段`。
2. **边界拷贝**（模块 4.2）：分别对两段跑 `InsertCopyIn`，标出 `in1 = OpCopyIn(in1)` 会出现几次、各在哪条语句前。
3. **活跃性**（模块 4.3）：用 `IsNodeUsedAfter` 判断 `in1` 在 S0 之后、S1 之后是否仍存活，并解释为何 `in1` 的缓冲在 PreReduce 段不能随便释放。
4. **缓冲分配**（模块 4.4）：判断 `t1`（紧贴归约 S1）在 `IsBinaryAcc=true` 时的 usage 类别与缓冲策略；判断 `in1` 会不会进空闲池。

**参考答案要点**：

1. `ReduceOpList={S1}`；从 `t2` 出发闭包 → S2 用 t2、S3 用 t3（S2 产出）→ `ReduceRefList={S2,S3}`；`PreReduce={S0,S1}`（缓冲段去掉 S1 得 `{S0}`）；`Reduced={S1,S2,S3}`。
2. PreReduce 段：S0 前插 `in1=CopyIn`；Reduced 段：S3 前再插 `in1=CopyIn`（S3 用到 in1）。共 2 次，分属两段。
3. `IsNodeUsedAfter<整条, in1, S0之后>`：S3 的 RHS 含 in1 → **true**（仍存活）；故 PreReduce 段里 in1 缓冲不可在 S0 后释放。这正是要靠 Reduced 段「重新 CopyIn」解耦的原因。
4. `t1` 是 S0 输出、紧贴归约 S1 → `IsDirectConnectToReduce` 命中 → `IsBinaryAcc=true` 下判为 `BUF_REDUCE_VAR`，走双缓冲。`in1` 是 Param（`BUF_PARAM`），**不进空闲池**，由 `CanRelease` 决定何时回收（在段内若不再使用可回收其槽，但 Param 槽按设计不被复用给别的 Param）。

**待本地验证**：如条件允许，参照 `tests/ut/compile_perf/` 的写法，写一个仅实例化 `ReduceAutoDag`/`BufferIdGenerator` 类型、不依赖 NPU 的 host 程序，用编译期 `static_assert` 把上述结论逐条断言出来——这是目前验证本模块行为最现实的方式。

## 6. 本讲小结

- `include/reduce/graph/` 是 ATVOSS 为**含 `ReduceSum`/`Broadcast` 的表达式**准备的专用图分析模块，核心矛盾是「归约会改变张量形状」，故必须与逐元素的 Elewise 图分析区别对待。
- `ReduceAutoDag` 把整条表达式沿归约切成 **PreReduce / Reduced 两段**，两段各自独立做 CopyIn/CopyOut 插入与缓冲分配（`FindLocalVarReferences` 算依赖闭包决定段边界）。
- `InsertCopyIn` 在**每一段的首次使用处**为 IN 参数插搬入语句，于是跨段的 IN 参数（如 rms_norm 的 `in1`）会被 CopyIn **两次**——这是用一次额外搬运换两段缓冲管理彻底解耦的刻意取舍。
- `IsNodeUsedAfter` 是一套编译期**活跃性分析**：从某条语句往后递归查 RHS 是否还含目标节点；它直接驱动缓冲能否释放（`CanRelease`）。
- `BufferIdGenerator` 是一台**编译期寄存器分配器**，用 `BUF_PARAM`/`BUF_LOCAL_VAR`/`BUF_REDUCE_VAR` 三档区分独占双缓冲、可复用、归约专用双缓冲；`ReleaseCurrentInputs` 据 `CanRelease` 回收 LocalVar 缓冲。
- **准确性提示**：本 HEAD 下 `reduce/graph/*` 未被 `atvoss.h` 及任何其他文件 include，`rms_norm` 实际走 Elewise `FullAutoDag` + `transcendental_evaluator`；本模块是「已实现的专用归约优化路径」，接入状态**待本地确认**。读懂它既能掌握 ATVOSS 为归约预留的设计，也是一次高强度的编译期图分析训练。

## 7. 下一步学习建议

- **横向对照**：回头重读 u3-l3 的 `FullAutoDag` 与 u3-l5 的 `GenerateBufferIdOrder`，把它和本讲的 `ReduceAutoDag`/`BufferIdGenerator` 做一张对比表（CopyIn 策略、缓冲分档、是否有 PreReduce/Reduced 切段）。你会更清楚地看到「通用逐元素路径 vs 专用归约路径」的设计分工。
- **纵向追踪执行**：本讲只到「图分析与缓冲分配」。归约算子最终如何落到硬件，看 [include/operators/transcendental_evaluator.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/transcendental_evaluator.h)（u3-l1 求值器系统的归约特化），结合 u3-l2 的 Tile 层同步链（MTE2→V→MTE3）理解归约前后的 PipeBarrier。
- **综合样例**：接着读 u3-l7（rms_norm 级联），把本讲的「切段/CopyIn/缓冲分档」与 rms_norm 端到端的 `ReduceSum→Broadcast→Divs→Sqrt` 串起来，巩固「为什么二维 TileShape + AR/AB 配对」的整体认知。
- **验证实践**：若想实证本模块，参考 u3-l10 的 `compile_perf`/host_ut 思路，写一个仅做类型实例化的 host 程序，用 `static_assert` 断言 `ReduceAutoDag`/`BufferIdGenerator` 的切分与缓冲结论。
