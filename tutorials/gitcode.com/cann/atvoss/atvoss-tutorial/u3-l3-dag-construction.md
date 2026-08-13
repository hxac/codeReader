# 计算图构建：DAG 与 Bind

## 1. 本讲目标

本讲打开 ATVOSS 编译期流水线的「图构建」环节。学完后你应该能够：

1. 说清楚用户在 `Compute()` 里写下的声明式表达式（如 `out = in1 * in2`），是如何被 `FullAutoDag` 加工成一条**带依赖、带内存搬运、带缓冲分配**的有序操作序列的。
2. 描述这条加工流水线的六个步骤：原地参数拆分 → 插入 CopyIn/CopyOut → 转 Bind 节点 → 取输出 → 依赖排序 → 内存分析。
3. 解释 `Bind` 节点如何用「操作 ↔ 赋值目标」的绑定关系编码数据依赖，从而支撑拓扑排序。
4. 理解 `DagNodeInfo` 如何做存活分析（liveness），推出一次 Tile 执行需要多少个 UB 缓冲。

本讲承接 u3-l1（求值器系统）：求值器需要一条**线性、有序、带缓冲**的操作序列才能驱动 `Tile::Evaluate`；本讲回答的正是「这条序列从哪来」。

## 2. 前置知识

本讲是专家篇，默认你已掌握以下前置认知（来自前置讲义）：

- **表达式树与叶子节点**（u2-l1、u2-l2）：`Compute()` 里的 `return (out = in1 * in2)` 不是运行时赋值，而是构造一棵编译期表达式树。叶子是 `Param<N,T,Usage>`（外部入参/出参）与 `LocalVar<N,T,Like>`（内部临时量），两者各自从 1 起的独立序号空间。
- **ParamUsage**（u2-l2）：`IN`/`OUT`/`IN_OUT` 表达数据流方向，是 GM 搬运的开关。
- **OpAssign / OpAndThen**（u2-l1）：`=` 生成 `OpAssign<Lhs, Rhs>` 表示「一条赋值语句」；逗号 `,` 生成 `OpAndThen<L, R>` 表示「先 L 后 R 的顺序」。逗号串接的表达式可被拍平成 `TypeList<OpAssign, OpAssign, ...>`。
- **张量算子 OpCopyIn/OpCopyOut/OpCopy/OpAlloc/OpFree**（u2-l3）：框架自动插入的内存搬运与缓冲管理节点，最终落到 `DataCopyPad`/`DataCopy` 等 Ascend C 指令。
- **求值器递归求值**（u3-l1）：`Tile::Evaluate<Expr>` 由 `Evaluator<Expr>` 递归特化驱动，`OpAndThen` 作为语句边界。

本讲还会用到两个编译期基础设施（来自 `utils/utility.h`）：`ForEach(list, func, init)` 按顺序对列表每个元素调用 `func` 折叠累积；`Filter_t`/`Unique_t`/`Difference_t`/`Concatenate_t` 等是类型列表的集合运算。这些是图构建的「手脚」，本讲聚焦图的语义，不展开它们。

> 一个贯穿全讲的直觉：ATVOSS 的图构建本质是**在类型层面做一次 SSA 风格的数据流分析**——每个 `Bind` 是一次定义（definition），它的输入指向产生这些输入的 `Bind`（use→def），从而隐式地连出一张有向无环图（DAG）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [include/elewise/graph/dag.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/dag.h) | 本讲主角。定义 `FullAutoDag`/`ManualDag`/`DagBase` 三套 DAG 构建器、`InplaceParamsProcessor` 原地参数拆分、`AddCopyX`/`OpAssign2Bind` 等流水线步骤、`Bind2OpAssign` 缓冲回填。 |
| [include/elewise/graph/node.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/node.h) | `DagNodeInfo`：对有序操作序列做存活分析与缓冲数量估算，输出 LEVEL_0/1/2 三档缓冲需求。 |
| [include/elewise/graph/bind.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/bind.h) | `Bind` 节点：把「操作」与「赋值目标」绑定，并从中抽取 `InNonScalarOps`（依赖）、`DependOps`（传递依赖闭包）等元信息。 |
| [include/operators/tensor_expression.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/tensor_expression.h) | `OpCopyIn`/`OpCopyOut`/`OpCopy`/`OpAlloc`/`OpFree` 的表达式节点定义。 |
| [include/elewise/block/schedule.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h) | DAG 的调用方：`PreProcessComputeExpr` 把用户表达式拍平、选 DAG、再做 Alloc/Free 插入。 |
| [include/expression/expr_template.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h) | `Param`/`LocalVar`/`OpAssign`/`OpAndThen` 与 `IsInVar`/`IsOutVar`/`IsInplaceVar` 萃取 trait 的定义。 |

## 4. 核心概念与源码讲解

### 4.1 全景：从用户表达式到可调度 DAG

#### 4.1.1 概念说明

先建立全局认知。用户在 `Compute()` 里写下的，是一棵**表达式树**（可能用逗号串成多条语句）。它只描述「算什么」，完全没有回答三个调度必需的问题：

1. **数据从哪来、到哪去？** 输入在 GM（Device 显存），计算在 UB（片上 Unified Buffer），需要 `CopyIn`/`CopyOut` 搬运。
2. **谁先执行谁后执行？** 一个临时变量在被使用前必须先被计算出来——需要拓扑排序。
3. **一次 Tile 同时要开多少块 UB 缓冲？** 需要做存活分析。

DAG 构建就是**在编译期、在类型层面**回答这三个问题。它把一棵表达式树变换成一个 `Bind` 节点序列：每个 `Bind` 绑定「一个赋值目标 + 一个操作」，并显式记录它依赖哪些前置 `Bind`，从而连出有向无环图。最后再依图分析内存。

> 名词解释：**DAG**（Directed Acyclic Graph，有向无环图）——一种没有环路的依赖图，A 依赖 B 就画一条 B→A 的边；拓扑排序给出一个「被依赖者先执行」的线性序列。**SSA**（Static Single Assignment）——每个变量只被赋值一次，每次赋值产生一个新版本，依赖关系因此天然清晰。ATVOSS 的 `Bind` 序列近似 SSA：每条 `OpAssign` 把结果写入一个目标，后续使用者通过引用「产生该目标的 Bind」来表达依赖。

#### 4.1.2 核心流程

DAG 的**输入**不是用户原始表达式，而是它被「拍平」后的结果。调用方在 [schedule.h:56](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h#L56) 先做一步：

```cpp
using OriExprList = typename Atvoss::FlattenAtOpAndThen<ExprT>::Type;
```

`FlattenAtOpAndThen`（定义见 [expr_template.h:491-511](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L491-L511)）把逗号串接的 `OpAndThen` 树展开成一个 `TypeList<OpAssign, OpAssign, ...>`。所以喂给 DAG 的 `ExprList` 已经是「语句列表」，每条语句是一个 `OpAssign<Lhs, Rhs>`。

随后选择用哪种 DAG 构建器（[schedule.h:36-44](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h#L36-L44)）：

```cpp
// MemMngPolicy::AUTO  → FullAutoDag（框架全自动插 CopyIn/CopyOut、分缓冲）
// 其它（MANUAL）       → ManualDag（用户自己管缓冲）
```

`FullAutoDag` 内部是一条六步流水线，下面 4.1.3 给出骨架，4.2–4.5 展开每个关键部件。

#### 4.1.3 源码精读

`FullAutoDag` 的类型成员就是流水线本身，自上而下读即可（[dag.h:442-458](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/dag.h#L442-L458)）：

```cpp
template <typename ExprList, MemLevel memOpt = MemLevel::LEVEL_0>
struct FullAutoDag {
    using NewExprList = typename InplaceParamsProcessor<ExprList>::Type; // ① 拆分 IN_OUT
    using Base = DagBase<NewExprList>;                                   // ② 收集 In/Out 参数
    using OriExprListWithCopyX = decltype(                               // ③ 插 CopyIn/CopyOut
        ForEach(typename Base::AllParams{}, AddCopyX{}, NewExprList{}));
    using BindList = decltype(                                           // ④ 转 Bind 节点
        ForEach(OriExprListWithCopyX{}, OpAssign2Bind{}, TypeList<>{}));
    using OutList = GetLastN_t<BindList, Size_v<typename Base::OutParams>>; // ⑤ 取尾部 CopyOut Bind
    using OrderdOps = Unique_t<decltype(                                 // ⑥ 依赖拓扑排序
        ForEach(OutList{}, ExtractDependOps{}, TypeList<>{}));
    using FullNodeInfo = DagNodeInfo<OrderdOps, OutList>;                // ⑦ 内存/缓冲分析
    ...
};
```

每行右侧注释对应一个最小模块：①对应 4.3（InplaceParamsProcessor），③④对应 4.2（流水线细节），⑤⑥的依赖机制藏在 `Bind` 里见 4.4，⑦见 4.5。注意原文 `OrderdOps` 是源码中的拼写（Ordered Ops），本讲沿用。

> 流水线里最反直觉的一点：步骤③的 `AddCopyX` **并不在意插入位置**——它把所有 `OpCopyIn` 塞到列表最前面、所有 `OpCopyOut` 塞到最后面。真正的执行顺序完全由步骤⑥的依赖拓扑排序决定。这意味着「原始列表顺序不重要，数据依赖才重要」。这是 DAG 设计的核心收益。

#### 4.1.4 代码实践

**实践目标**：在脑子里跑通 `out = in1 * in2` 的前两步变换，确认对输入格式的理解。

**操作步骤**：

1. 设 `in1 = Param<1,float,IN>`、`in2 = Param<2,float,IN>`、`out = Param<3,float,OUT>`。
2. 用户表达式 `out = in1 * in2` 经 `FlattenAtOpAndThen` 得到 `OriExprList = [OpAssign<out, OpMul<in1, in2>>]`（只有一条语句）。
3. `InplaceParamsProcessor` 无 IN_OUT 参数，故 `NewExprList` 不变。
4. `DagBase` 收集：`AllParams = [in1, in2, out]`、`InParams = [in1, in2]`、`OutParams = [out]`。

**需要观察的现象**：`OriExprList` 是一个**长度为 1** 的 `TypeList`，里面只有一个 `OpAssign`，说明 `FlattenAtOpAndThen` 只是「拆逗号」，不会拆解单条赋值的右值。

**预期结果**：确认输入是「语句列表」而非「表达式树」，后续所有步骤都在这个列表上做原地变换。

（完整的六步追踪留给第 5 节综合实践。）

#### 4.1.5 小练习与答案

**练习 1**：muls 的 `MulsComputePromtIn` 返回 `(inTmp = Cast(in), out = inTmp * scalar)`。`FlattenAtOpAndThen` 会把它变成多长的 `TypeList`？

**答案**：长度为 2：`[OpAssign<inTmp, OpCast<in>>, OpAssign<out, OpMul<inTmp, scalar>>]`。逗号表达式 `OpAndThen` 被拆成两条独立语句。

**练习 2**：为什么 `FullAutoDag` 的默认模板参数是 `MemLevel::LEVEL_0`，但步骤⑦之后还有 `ChooseBufferLevel` 去重新选档位？

**答案**：`LEVEL_0` 在这里表示「由框架自动选最优档」，而非「固定用 LEVEL_0」。`ChooseBufferLevel`（见 4.5）会根据缓冲数量在 LEVEL_2/1/0 之间挑一个能装得下的最激进档位。`LEVEL_0` 是最保守（缓冲最省、复用最少）的兜底。

---

### 4.2 FullAutoDag 构建流水线：CopyIn/CopyOut 插入与转 Bind

#### 4.2.1 概念说明

本模块拆开流水线中最实质的两步：**插入内存搬运**与**把语句转成 Bind 节点**。

输入参数的值起初在 GM，而计算发生在 UB。所以每个 `IN`/`IN_OUT` 参数在被计算使用前，必须有一次 `OpCopyIn`（GM→UB）；每个 `OUT`/`IN_OUT` 参数在计算完成后，必须有一次 `OpCopyOut`（UB→GM）。标量参数（如 muls 的 `scalar`）是按值传入的，不需要搬运——这是 `AddCopyX` 里要特别处理的边界。

转成 `Bind` 节点是图构建的关键一跃：`OpAssign` 只说「把右值赋给左值」，是平铺的；`Bind` 则把每个操作的使用者显式指向「产生该输入的那个 Bind」，依赖关系因此被编码进类型里。

#### 4.2.2 核心流程

**步骤③：插入 CopyIn/CopyOut** —— `AddCopyX`（[dag.h:89-104](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/dag.h#L89-L104)）是一个 `ForEach` 仿函数，对 `AllParams` 里每个参数按下表处置：

| 参数性质 | 处置 | 效果 |
|---------|------|------|
| 标量 + `IN` | 不动（且断言标量只能 `IN`） | 标量不搬运 |
| 张量 + `IN` | 在列表**头部**插入 `OpCopyIn<Param>` | 搬入 |
| 张量 + `OUT` | 在列表**尾部**插入 `OpCopyOut<Param>` | 搬出 |
| 张量 + `IN_OUT` | 头插 `OpCopyIn` + 尾插 `OpCopyOut` | 先搬入再搬出 |

对 `out = in1 * in2`（`AllParams=[in1,in2,out]`），依次处理后得到：

```
[ OpCopyIn<in2>, OpCopyIn<in1>, OpAssign<out, OpMul<in1, in2>>, OpCopyOut<out> ]
```

注意 `in2` 的 CopyIn 排在 `in1` 前面——因为逐个头插，后插的更靠前。这无所谓，顺序后面会被拓扑排序重排。

> 对比：`ManualDag` 用的是更精细的 `CopyInInserter`/`CopyOutInserter`（[dag.h:52-87](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/dag.h#L52-L87)），它借助 `FirstAndLastUse` 把 CopyIn 插到「首次使用之前」、CopyOut 插到「末次使用之后」，并保留原始语句顺序。`FullAutoDag` 不需要这种精细，因为它完全依赖依赖排序。

**步骤④：转 Bind** —— `OpAssign2Bind`（[dag.h:106-128](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/dag.h#L106-L128)）遍历带搬运的列表，把每一项转成 `Bind`：

- `OpCopyIn<P>` → `Bind<P, OpCopyIn<P>>`（赋值目标就是它自己）；
- `OpAssign<Lhs, Rhs>` → `Bind<Lhs, Rhs'>`，其中 `Rhs'` 是把 `Rhs` 里每个参数引用**替换成「最近一次给它赋值的 Bind」**；
- `OpCopyOut<P>` → `Bind<P, OpCopyOut<P>'`，同样做参数→Bind 的替换。

这里最关键的一行是 [dag.h:123](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/dag.h#L123)：

```cpp
using RealOpX = ReplaceOpArgs_t<OpX, LastAssignRhsFinder<BindList>::template Type>;
```

`LastAssignRhsFinder` 对操作里的每个参数 `P`，查 `LastAssignRhs<BindList, P>`（[bind.h:150-174](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/bind.h#L150-L174)）：找到列表中**最后一次**把值赋给 `P` 的那个 `Bind`（跳过 CopyOut），用那个 `Bind` 替换 `P`。于是 `OpMul<in1, in2>` 变成 `OpMul<Bind<in1,OpCopyIn<in1>>, Bind<in2,OpCopyIn<in2>>>`——操作的入参不再是裸参数，而是「产生该参数的那个 Bind」。**依赖边就是这样建出来的。**

#### 4.2.3 源码精读

`AddCopyX` 的三分支（[dag.h:89-104](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/dag.h#L89-L104)）：

```cpp
struct AddCopyX {
    template <typename Param, typename ExprList>
    constexpr auto operator()(TypeWrapper<Param>, ExprList) const {
        if constexpr (std::is_scalar_v<typename Param::Type>) {
            static_assert(Param::usage == ParamUsage::IN, "Scalar Tensor is only supported in ParamUsage::IN");
            return ExprList{};                                          // 标量：原样返回
        } else if constexpr (Param::usage == ParamUsage::IN) {
            return Concatenate_t<TypeList<OpCopyIn<Param>>, ExprList>{}; // 头插 CopyIn
        } else if constexpr (Param::usage == ParamUsage::OUT) {
            return Concatenate_t<ExprList, TypeList<OpCopyOut<Param>>>{}; // 尾插 CopyOut
        } else { // in_out
            return Concatenate_t<TypeList<OpCopyIn<Param>>, ExprList, TypeList<OpCopyOut<Param>>>{};
        }
    }
};
```

`OpAssign2Bind` 的三分支（[dag.h:106-128](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/dag.h#L106-L128)），省略号处即「参数替换为产生它的 Bind」：

```cpp
struct OpAssign2Bind {
    template <typename T, typename BindList>
    constexpr auto operator()(TypeWrapper<T>, BindList) const {
        if constexpr (IsSpecializationOf_v<OpCopyIn, T>) {
            using CopyInParam = typename T::DataType;
            return Append_t<BindList, Bind<CopyInParam, T>>{};          // CopyIn → Bind<P,OpCopyIn<P>>
        } else if constexpr (IsSpecializationOf_v<OpCopyOut, T>) {
            using RealOpX = ReplaceOpArgs_t<T, LastAssignRhsFinder<BindList>::template Type>;
            return Append_t<BindList, Bind<CopyOutParam, RealOpX>>{};
        } else { // OpAssign
            using AssignedTo = typename T::LhsType;
            using OpX = typename T::RhsType;
            using RealOpX = ReplaceOpArgs_t<OpX, LastAssignRhsFinder<BindList>::template Type>;
            return Append_t<BindList, Bind<AssignedTo, RealOpX>>{};      // 赋值目标 + 替换入参后的操作
        }
    }
};
```

替换所用的 `LastAssignRhs`（[bind.h:155-169](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/bind.h#L155-L169)）——在已建好的 `BindList` 里倒序找「最后一次赋给该参数、且不是 CopyOut」的 Bind：

```cpp
template <typename ToCheck>
struct ParamEquals
    : std::bool_constant<!IsBindOfOp_v<OpCopyOut, ToCheck> && std::is_same_v<Param, typename ToCheck::AssignedTo>> {};
static constexpr std::size_t lastPos = FindLast_v<ParamEquals, BindList>;
using Type = Get_t<BindList, lastPos>;
```

这正是 SSA 的 use→def 链：使用一个变量，等于引用它最近的那个定义点。

#### 4.2.4 代码实践

**实践目标**：手算 `out = in1 * in2` 经步骤③④后的 `BindList`，验证依赖边是否正确建出。

**操作步骤**：

1. 步骤③结果（见 4.2.2）：`[OpCopyIn<in2>, OpCopyIn<in1>, OpAssign<out,OpMul<in1,in2>>, OpCopyOut<out>]`。
2. 逐项走 `OpAssign2Bind`，维护一个不断增长的 `BindList`：
   - `OpCopyIn<in2>` → 追加 `Bind<in2, OpCopyIn<in2>>`（记作 `B_cin2`）；
   - `OpCopyIn<in1>` → 追加 `Bind<in1, OpCopyIn<in1>>`（`B_cin1`）；
   - `OpAssign<out, OpMul<in1,in2>>`：把 `in1` 替换成 `B_cin1`、`in2` 替换成 `B_cin2`，追加 `Bind<out, OpMul<B_cin1, B_cin2>>`（`B_mul`）；
   - `OpCopyOut<out>`：把 `out` 替换成最近的赋值者 `B_mul`，追加 `Bind<out, OpCopyOut<...>>`（`B_cout`）。

**需要观察的现象**：`B_mul` 的操作入参不再是裸 `in1`/`in2`，而是两个 CopyIn Bind。这就是「`B_mul` 依赖 `B_cin1` 和 `B_cin2`」的类型编码。

**预期结果**：得到 4 个 `Bind` 的列表，其中 `B_mul.Operation` 的入参类型正是 `B_cin1`、`B_cin2`。后续 4.4 的 `DependOps` 就是顺着这些入参提取依赖的。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `in2` 也声明成 `IN_OUT`（即 `out = in1 * in2` 且 `in2` 同时是输出），`AddCopyX` 会怎样处理 `in2`？

**答案**：`AddCopyX` 走 `in_out` 分支，对 `in2` 既头插 `OpCopyIn<in2>` 又尾插 `OpCopyOut<in2>`。但注意：`IN_OUT` 在进入 `FullAutoDag` 前会被 `InplaceParamsProcessor`（4.3）改写，所以实际进到 `AddCopyX` 时通常已没有 `IN_OUT` 了。

**练习 2**：为什么标量参数的 `CopyIn` 被显式禁止（还加了 `static_assert`）？

**答案**：标量（如 `float`、`ScalarDtype`）在 `ArgumentsBuilder` 里是按值传入、随 kernel 参数直接进 NPU 的，不占用 GM、不需要 GM→UB 搬运。给它插 `OpCopyIn` 既无意义也会导致类型错误，故断言拦截。

---

### 4.3 InplaceParamsProcessor：拆分 IN_OUT 参数

#### 4.3.1 概念说明

`ParamUsage::IN_OUT` 表示一个参数「既读又写、复用同一块 GM」（即原地算子，in-place）。这种参数对 DAG 是个麻烦：

- 它同时属于输入列表和输出列表，框架会想给它既插 CopyIn 又插 CopyOut；
- 但在 SSA 视角下，「读它」和「写它」应是两个不同的版本，否则依赖边会自环、拓扑排序会出问题。

`InplaceParamsProcessor` 的策略是**把原地参数拆成两个**：一个只读的 `IN` 版本（承载输入语义），一个新编号的 `OUT` 版本（承载输出语义），并在表达式中把对它的引用重定向到正确的版本。拆完后，列表里只剩纯 `IN` 和纯 `OUT`，`AddCopyX` 就能无歧义地工作。

#### 4.3.2 核心流程

`InplaceParamsProcessor`（[dag.h:261-275](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/dag.h#L261-L275)）三步走：

1. **识别**：从所有参数里筛出 `IN_OUT` 的（`InplaceParams`），再筛出「真的被 `OpAssign` 赋过值」的（`AssignedInplaceParams`）——只读的 `IN_OUT`（比如只参与右值）不需要拆。
2. **建替换表**：为每个待拆原地参数造一个新 `Param`，序号接在现有参数总数之后，`Usage=OUT`，`inplaceNumber` 仍指向原参数（保证运行时复用同一块 GM）。
3. **改写表达式**：倒序扫描语句列表，把赋值左值的原地参数换成新 `OUT` 参数，把右值里对原地参数的引用在合适时机换成 `IN` 版本；最后把所有 `IN_OUT` 的 `usage` 统一改成 `IN`。

关键约束（[expr_template.h:98-106](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/expression/expr_template.h#L98-L106)）：`Param` 有一个 `inplaceNumber` 字段，默认等于自身序号 `N`；两个 `Param` 若 `inplaceNumber` 相同，运行时就共享同一块 GM。这正是拆分后「逻辑上是两个参数、物理上是一块内存」的桥梁。

#### 4.3.3 源码精读

`InplaceParamsProcessor` 主体（[dag.h:261-275](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/dag.h#L261-L275)）：

```cpp
template <typename ExprList>
struct InplaceParamsProcessor {
private:
    using OriAllParams = Params_t<ExprList>;                              // 全部参数
    using InplaceParams = Filter_t<IsInplaceVar, OriAllParams>;           // 筛 IN_OUT
    using AssignedInplaceParams = Unique_t<decltype(                      // 筛「被赋过值的」IN_OUT
        ForEach(ExprList{}, ExtractAssignedParams<ParamUsage::IN_OUT>{}, TypeList<>{}))>;
    using ReplaceParamsMap = decltype(                                    // 造新 OUT 参数的替换表
        CreateReplacerForInplaceParams<AssignedInplaceParams, Size_v<OriAllParams> + 1>());
    using NewExprListTmp = decltype(                                      // 倒序改写表达式
        ReplaceInplaceParam<AssignedInplaceParams, ReplaceParamsMap, ExprList, Size_v<OriAllParams> + 1>());
public:
    using Type = decltype(ChangeInplaceParamUsageToIn<InplaceParams, NewExprListTmp>()); // 把 IN_OUT 统一改成 IN
};
```

造新参数的 `CreateReplacerForInplaceParams`（[dag.h:151-162](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/dag.h#L151-L162)），新 `Param` 的 `inplaceNumber` 设为原参数序号，`Usage=OUT`：

```cpp
using NewParams = Param<nextParamNum, typename OldParams::Type, ParamUsage::OUT, OldParams::number>;
//                                     ^^^新序号               ^^^OUT          ^^^沿用原 inplaceNumber
```

倒序改写的 `ReplaceInplaceParam`（[dag.h:193-223](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/dag.h#L193-L223)）从最后一条语句往前扫：当遇到某原地参数**首次作为左值出现**（即倒序最先碰到它的定义点）时，把这条赋值的左值换成新 `OUT` 参数；此前的引用保持 `IN`。这保证了「定义点之前用输入版本、定义点起用输出版本」的 SSA 语义。

最后 `ChangeInplaceParamUsageToIn`（[dag.h:241-259](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/dag.h#L241-L259)）把残留的 `IN_OUT` 全部改成 `IN`，让 `AddCopyX` 不再触发 `in_out` 分支。

#### 4.3.4 代码实践

**实践目标**：用一个假想的原地算子理解拆分效果。

**操作步骤**：假设用户写 `out` 声明为 `Param<1, float, IN_OUT>`，表达式为 `out = Sqrt(out)`（对自身开方）。

1. `InplaceParamsProcessor` 识别 `out` 是被赋值的 `IN_OUT`；
2. 设原参数总数为 1，造新参数 `Param<2, float, OUT, 1>`（`inplaceNumber=1`，与 `out` 共享 GM）；
3. 改写后表达式变为 `Param<2> = Sqrt(Param<1>)`，且 `Param<1>` 的 `usage` 改成 `IN`、`Param<2>` 是 `OUT`。

**需要观察的现象**：改写后 `Param<1>` 是纯输入、`Param<2>` 是纯输出，但两者 `inplaceNumber` 都等于 1，运行时仍指向同一块 GM。

**预期结果**：`AddCopyX` 随后会给 `Param<1>` 插 CopyIn、给 `Param<2>` 插 CopyOut，逻辑上读旧值写新值，物理上读写同一块显存。这正是原地算子的正确语义。

> 待本地验证：ATVOSS 自带样例目前未直接演示 `IN_OUT`（abs/muls 都用纯 IN/OUT）。可参考 `tests/` 下相关用例确认原地参数的端到端行为。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `InplaceParamsProcessor` 要区分「所有 IN_OUT 参数」和「被赋值的 IN_OUT 参数」？

**答案**：一个 `IN_OUT` 参数若只出现在右值（只读未被赋值），它实质上就是个 `IN`，没有歧义、无需拆分。只有既读又写（被 `OpAssign` 赋值）的才需要拆成 IN/OUT 两个版本以消除自环依赖。

**练习 2**：拆分后两个 `Param` 序号不同但 `inplaceNumber` 相同，这个信息在哪里被消费？

**答案**：在运行时的 GM 内存分配/寻址阶段（Device 层 `ArgumentsBuilder` 与 GM 偏移计算）。`inplaceNumber` 相同意味着它们映射到同一块 GM，从而实现原地写回。在编译期图构建层面，框架只关心它们逻辑上的数据流方向（IN vs OUT）。

---

### 4.4 Bind 节点结构：操作 ↔ 赋值目标的绑定与依赖抽取

#### 4.4.1 概念说明

`Bind` 是 DAG 的节点类型，定义在 [bind.h:239-280](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/bind.h#L239-L280)。它的核心思想：**一个 Bind = 一个赋值目标 + 一个操作**。但和普通 `OpAssign` 不同，`Bind` 的操作入参是「产生该入参的 Bind」（由 4.2 的 `LastAssignRhs` 替换而来），因此 `Bind` 天然携带「我依赖谁」的边。

`Bind` 还提供一组类型成员，把操作的各种性质预先算好，供排序、内存分析复用：入参里的 GM 参数（`AllParams`/`InParams`/`OutParams`）、入参里的非标量操作（`InNonScalarOps`，即依赖的前置 Bind）、传递依赖闭包（`DependOps`）等。

#### 4.4.2 核心流程

给定一个 `Bind<V, OpX>`，它的关键字段如何派生（[bind.h:246-280](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/bind.h#L246-L280)）：

1. `OpPattern<OpX>`（[bind.h:49-78](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/bind.h#L49-L78)）先把 `OpX` 的「类型参数」与「非类型参数」分离。例如 `OpReduceSum<Pattern::AR, T>` 的 `OpArgs = [T]`（`AR` 被当作 `NoneType` 剥掉）。这样后续的集合运算只作用于真正的类型入参。
2. `AllParams = OpArgs 里属于 Param 的、去重`；再按 `usage` 分出 `InParams`/`OutParams`。
3. `InOps` = `OpArgs` 里**既不是 Param 也不是标量**的部分——也就是 4.2 替换进来的「产生者 Bind」。
4. `InNonScalarOps` = `InOps` 去掉标量 Bind 后的剩余，即本节点**直接依赖的前置 Bind 列表**。
5. `DependOps` = 把每个直接依赖的 `DependOps` 递归摊平、去重，再并上自己——即本节点的**全部前置依赖（含自己）**，已拓扑排好序。
6. `isCopyXOp`/`isCopyInOp` 等布尔标签，标记本节点是否是搬运类节点。

依赖闭包 `DependOps` 是拓扑排序的引擎：从输出节点出发，取它的 `DependOps` 再去重，就得到整张图的合法执行顺序。

#### 4.4.3 源码精读

`Bind` 的核心派生（[bind.h:246-280](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/bind.h#L246-L280)），注释标出每个字段含义：

```cpp
template <typename V, typename OpX>
struct Bind {
    using AssignedTo = V;                              // 赋值目标（Param 或 LocalVar）
    using Operation = typename Pattern::OpTpl;         // 操作本体
    using OpArgs = typename Pattern::OpArgs;           // 操作的类型入参（去掉 Pattern 等非类型参数）
    using AllParams = Unique_t<Filter_t<IsParam, OpArgs>>; // 入参中的 GM 参数
    using InParams = Filter_t<IsInParam, AllParams>;
    using OutParams = Filter_t<IsOutParam, AllParams>;
private:
    using InOpsTmp1 = Unique_t<Difference_t<OpArgs, AllParams>>; // 入参中去掉 GM 参数
    using InOpsTmp2 = Filter_t<std::is_scalar, InOpsTmp1>;
public:
    using InOps = Difference_t<InOpsTmp1, InOpsTmp2>;            // = 产生者 Bind（非标量）
    using InScalarOps = Filter_t<IsScalarBind, InOps>;
    using InNonScalarOps = Difference_t<InOps, InScalarOps>;     // 直接依赖的前置 Bind
    using DependOps = Append_t<Unique_t<decltype(
        ForEach(InOps{}, ExtractDependOps{}, TypeList<>{}))>, BindType>; // 传递依赖闭包 + 自身
    constexpr static bool isCopyXOp = (IsSpecializationOf_v<OpCopyIn, Operation>
                                       || IsSpecializationOf_v<OpCopyOut, Operation>);
    constexpr static bool isCopyInOp = IsSpecializationOf_v<OpCopyIn, Operation>;
};
```

`ExtractDependOps`（[bind.h:80-87](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/bind.h#L80-L87)）极其简单——它只是把每个依赖 Bind 的 `DependOps` 拼接起来：

```cpp
struct ExtractDependOps {
    template <typename T, typename ResultList>
    constexpr auto operator()(T, ResultList) const {
        using B = typename T::Type;
        return Concatenate_t<ResultList, typename B::DependOps>{};
    }
};
```

因为每个 `Bind::DependOps` 已经是「我的全部前置 + 我自己」的闭包，所以顶层只需从输出节点调用一次 `ExtractDependOps`、再 `Unique_t` 去重，就得到整图拓扑序——这就是 `FullAutoDag::OrderdOps` 那一行（[dag.h:456](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/dag.h#L456)）的全部魔法。

> 这是一种**记忆化递归**：依赖闭包在 `Bind` 构造时就已算好缓存进类型，排序时只是取用，不重复计算。运行时零开销。

辅助分析器 `IsAbleToFree`（[bind.h:222-237](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/bind.h#L222-L237)）与 `ConnectToAny`（[bind.h:125-132](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/bind.h#L125-L132)）分别支撑后续的缓冲释放判定与「是否连到输出」判定，本讲在 4.5 会用到它们的结果。

#### 4.4.4 代码实践

**实践目标**：用 4.2 得到的 `BindList` 推导每个 `Bind` 的 `InNonScalarOps` 与 `DependOps`，验证 `OrderdOps` 的排序结果。

**操作步骤**（`out = in1 * in2`，沿用 4.2.4 的命名 `B_cin2, B_cin1, B_mul, B_cout`）：

1. `B_cin1`/`B_cin2`：操作是 `OpCopyIn<P>`，`OpArgs=[P]`，`AllParams=[P]`，故 `InOps=[]`、`DependOps=[自身]`。它们是叶子（从 GM 读，不依赖任何 Bind）。
2. `B_mul`：`OpArgs=[B_cin1, B_cin2]`，`AllParams=[]`（这俩是 Bind 不是 Param），`InOps=[B_cin1,B_cin2]`，`InNonScalarOps=[B_cin1,B_cin2]`，`DependOps=[B_cin1, B_cin2, B_mul]`。
3. `B_cout`：`InNonScalarOps=[B_mul]`，`DependOps=[B_cin1, B_cin2, B_mul, B_cout]`。
4. `OutList = [B_cout]`（尾部 1 个 CopyOut Bind）。`OrderdOps = Unique(B_cout.DependOps) = [B_cin1, B_cin2, B_mul, B_cout]`。

**需要观察的现象**：`OrderdOps` 的顺序是 `CopyIn → 计算 → CopyOut`，被依赖者在前——这正是合法的执行顺序，与步骤③里「CopyIn 都塞最前」的粗插无关。

**预期结果**：最终 `OrderdOps` 长度为 4，顺序为搬入、搬入、乘法、搬出，完全由数据依赖决定。

#### 4.4.5 小练习与答案

**练习 1**：`OpPattern` 为什么要从 `OpReduceSum<Pattern::AR, T>` 里剥掉 `AR`？

**答案**：`AR` 是一个编译期枚举值（非类型模板参数），不是类型。集合运算（`Filter`/`Difference` 等）只对类型有意义，若不剥掉，`AR` 会混入 `OpArgs` 干扰 Param/Bind 的识别。`OpPattern` 的两个特化（[bind.h:52-78](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/bind.h#L52-L78)）分别处理「带非类型参数」与「纯类型参数」两种操作形式。

**练习 2**：若 `B_mul` 同时是多个后续节点的输入，它的 `DependOps` 会被重复展开吗？结果会重复吗？

**答案**：展开过程中可能多次出现，但 `DependOps` 自身在构造时已 `Unique_t` 去重，`OrderdOps` 顶层再 `Unique_t` 一次，所以最终序列无重复。这是 DAG（无环）性质保证的——每个节点只有一个定义点。

---

### 4.5 DagNodeInfo：依赖排序与内存/缓冲分析

#### 4.5.1 概念说明

拿到有序的 `Bind` 序列后，`DagNodeInfo`（[node.h:143-274](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/node.h#L143-L274)）回答最后一个问题：**执行这条序列，UB 里同时要开多少块缓冲？**

这本质上是一次**存活分析（liveness analysis）**，思路和编译器的寄存器分配一样：一个节点从「被定义」起存活，到「最后一次被使用」止死亡；任意时刻存活的节点数，就是该时刻需要并存的缓冲数。峰值存活数决定了缓冲池容量。

`DagNodeInfo` 把分析结果封装成一族 `GetBufferNumLevelX()` 静态方法，对应 `MemLevel` 的三档策略（LEVEL_0 最省缓冲、复用最保守；LEVEL_2 最激进、缓冲最多）。`FullAutoDag::ChooseBufferLevel`（[dag.h:462-476](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/dag.h#L462-L476)）再据此挑一个「缓冲数 ≤ 10 且最激进」的档位。

#### 4.5.2 核心流程

存活分析由 `MaxAliveNode`（[node.h:108-138](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/node.h#L108-L138)）完成，它沿有序序列从前向后扫描，维护一个「当前存活集合」`Acc`：

1. 对第 `i` 个节点，它的存活相关集合 `InOutNodes` = 该节点的输入（`InNonScalarOps`）并上自身（`CopyOut` 例外，只算输入，因为它不产生新数据）；
2. `AliveNodes = (Acc ∪ InOutNodes) − RsvList`（缓存节点除外）；
3. 用 `AliveNodes` 的大小更新峰值 `aliveNode`；
4. `DelVar` = 该节点那些「此后再也不会被用到」的输入（用 `IsAbleToFree` 判定，[bind.h:222-237](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/bind.h#L222-L237)）；
5. `Next = AliveNodes − DelVar`，即删除已死的，传给下一轮。

峰值存活数 `maxAliveNodeInfo.aliveNode` 即并存缓冲的上界。`tempCalcNode` 进一步统计「既不是 CopyIn、也没直连输出」的临时计算节点数，用于临时缓冲估算。

随后 `GetBufferNumLevel0/1/2`（[node.h:252-273](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/node.h#L252-L273)）把存活数、输入数、输出数按各档公式（含 ping-pong ×2 因子）换算成总缓冲槽数。`GetCopyInCountBeforeFirstCalcNode`（[node.h:51-67](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/node.h#L51-L67)）专门统计「第一个计算节点之前」要先搬入多少个 GM，因为它们必须同时驻留 UB。

#### 4.5.3 源码精读

`MaxAliveNode` 的核心循环（[node.h:108-138](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/node.h#L108-L138)）：

```cpp
template <typename OpList, typename OutList, typename RsvList = TypeList<>,
          std::size_t start = 0, typename Acc = TypeList<>>
constexpr DagMaxAliveInfo MaxAliveNode(DagMaxAliveInfo info)
{
    if constexpr (start < Size_v<OpList>) {
        using Op = Get_t<OpList, start>;
        // 当前节点的依赖：输入（CopyOut 只算输入）并上自身
        using InOutNodes = std::conditional_t<
            IsBindOfOp_v<OpCopyOut, Op>, typename Op::InNonScalarOps,
            Append_t<typename Op::InNonScalarOps, typename Op::BindType>>;
        using AliveNodes = Difference_t<Unique_t<Concatenate_t<Acc, InOutNodes>>, RsvList>;
        info.aliveNode = Max<std::size_t>(Size_v<AliveNodes>, info.aliveNode); // 更新峰值
        // 找出此后不再使用的输入，从存活集中删除
        using DelVar = Filter_t<WillNotUsed<OpList, start + 1>::template Type, typename Op::InNonScalarOps>;
        using Next = Difference_t<AliveNodes, DelVar>;
        return MaxAliveNode<OpList, OutList, RsvList, start + 1, Next>(info);
    }
    return info;
};
```

`DagNodeInfo` 把它汇总（[node.h:143-163](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/node.h#L143-L163)）：

```cpp
template <typename OpList, typename OutList>
struct DagNodeInfo {
    using AllParams = Unique_t<Concatenate_t<ExtractBindParams_t<OpList>, Map_t<ExtractBindAssignTo, OutList>>>;
    using InParams = Filter_t<IsInParam, AllParams>;
    using OutParams = Filter_t<IsOutParam, AllParams>;
    constexpr static std::size_t inSize = Size_v<InParams>;
    constexpr static std::size_t outSize = Size_v<OutParams>;
    constexpr static auto maxAliveNodeInfo = MaxAliveNode<OpList, OutList>(DagMaxAliveInfo()); // 峰值存活
    ...
    constexpr static std::size_t GetBufferNumLevel0() { /* ping-pong ×2 等公式 */ }
    constexpr static std::size_t GetBufferNumLevel1() { ... }
    constexpr static std::size_t GetBufferNumLevel2() { ... }
};
```

`FullAutoDag` 用它来选档（[dag.h:462-476](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/dag.h#L462-L476)）：`LEVEL_2` 缓冲数 ≤ `MAX_BUFFER_NUMBER`(10) 就选 LEVEL_2，否则退 LEVEL_1，再不行退 LEVEL_0；并在 [dag.h:542-544](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/dag.h#L542-L544) 用 `static_assert` 拦截「缓冲数超过 32」的非法情况。

最后，`FullAutoDag` 用 `Bind2OpAssign`（[dag.h:343-401](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/dag.h#L343-L401)）把已排序的 `Bind` 序列连同算好的缓冲 ID 回填成最终的 `ExprListWithCopyX`（CopyIn/OpAssign/CopyOut 列表）与 `BufMap`（Param→bufferId 映射），输出给调用方做 Alloc/Free 插入。缓冲分配细节属于 u3-l5（Buffer 双缓冲），本讲只指出它消费 `DagNodeInfo` 的结果。

#### 4.5.4 代码实践

**实践目标**：手算 `out = in1 * in2` 的存活曲线与峰值，体会缓冲数量的由来。

**操作步骤**（沿 `OrderdOps = [B_cin1, B_cin2, B_mul, B_cout]` 扫描，`Acc` 初始为空）：

| 步骤 | 节点 | InOutNodes | AliveNodes (Acc∪InOut) | 峰值 | DelVar（此后不再用） | Next（删除后） |
|------|------|-----------|------------------------|------|--------------------|----------------|
| 0 | `B_cin1` | {B_cin1} | {B_cin1} | 1 | ∅ | {B_cin1} |
| 1 | `B_cin2` | {B_cin2} | {B_cin1, B_cin2} | **2** | ∅ | {B_cin1, B_cin2} |
| 2 | `B_mul` | {B_cin1,B_cin2,B_mul} | {B_cin1,B_cin2,B_mul} | **3** | {B_cin1,B_cin2}（之后不再用） | {B_mul} |
| 3 | `B_cout` | {B_mul}（CopyOut 只算输入） | {B_mul} | 3 | {B_mul} | ∅ |

**需要观察的现象**：峰值存活出现在步骤 2（`aliveNode=3`），即两个 CopyIn 的结果加上正在计算的 `B_mul` 同时驻留 UB。

**预期结果**：`maxAliveNodeInfo.aliveNode = 3`。再叠加 ping-pong（×2）、MTE3 输出缓冲等因素，`GetBufferNumLevelX` 会给出各档的总缓冲槽数，`ChooseBufferLevel` 据此选档。

> 待本地验证：上表是「逻辑存活」推演，实际缓冲数还含 ping-pong、缓存复用等修正（见 `GetBufferNumLevel0/1/2` 公式），完整数值需结合具体 MemLevel 在编译期确认。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `CopyOut` 节点的 `InOutNodes` 只算输入、不算自身？

**答案**：`CopyOut` 是把 UB 数据搬回 GM，它**不产生新的存活数据**（结果直接进了 GM，不再占用 UB 缓冲）。若把它自己也计入存活，会虚增缓冲需求。`MaxAliveNode` 用 `std::conditional_t` 对 `OpCopyOut` 做了特判（[node.h:116-118](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/node.h#L116-L118)）。

**练习 2**：`MAX_BUFFER_NUMBER = 10`（[dag.h:36](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/dag.h#L36)）和 `BUF_MAX_COUNT = 32`（buffer.h）分别约束什么？

**答案**：`MAX_BUFFER_NUMBER=10` 是 `ChooseBufferLevel` 选档的阈值——某档缓冲数 ≤ 10 才被视为「装得下」并优先选更激进档。`BUF_MAX_COUNT=32` 是硬件 TPipe 缓冲池的硬上限，`GetBufferIds` 在 [dag.h:542-544](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/graph/dag.h#L542-L544) 用 `static_assert` 强制 totalCount ≤ 32，超过则编译失败并提示切换到更省缓冲的 MemLevel。

---

## 5. 综合实践

**任务**：完整追踪 `out = in1 * in2` 从用户表达式到 `OrderdOps` 的全过程，画出 DAG 并解释 CopyIn 的必要性。

**步骤**：

1. **输入**：用户 `Compute()` 返回 `out = in1 * in2`，`in1=Param<1,float,IN>`、`in2=Param<2,float,IN>`、`out=Param<3,float,OUT>`。经 `FlattenAtOpAndThen` 得 `OriExprList = [OpAssign<out, OpMul<in1, in2>>]`。

2. **InplaceParamsProcessor**：无 `IN_OUT`，列表不变。

3. **AddCopyX 插入搬运**（标量不在此例）：
   ```
   [OpCopyIn<in2>, OpCopyIn<in1>, OpAssign<out, OpMul<in1,in2>>, OpCopyOut<out>]
   ```
   对 `in1`/`in2` 头插 CopyIn，对 `out` 尾插 CopyOut。

4. **OpAssign2Bind 转 Bind**（参数替换为产生者 Bind）：
   - `B_cin2 = Bind<in2, OpCopyIn<in2>>`
   - `B_cin1 = Bind<in1, OpCopyIn<in1>>`
   - `B_mul = Bind<out, OpMul<B_cin1, B_cin2>>`
   - `B_cout = Bind<out, OpCopyOut<B_mul>>`

5. **依赖 DAG（有向图）**：
   ```
   B_cin1 ──┐
            ├──> B_mul ──> B_cout
   B_cin2 ──┘
   ```
   边的方向是「被依赖 → 依赖者」。`B_mul` 同时依赖两个搬入节点；`B_cout` 依赖 `B_mul`。两个 `B_cin*` 是叶子（指向 GM，不依赖任何 Bind）。

6. **拓扑排序**：`OutList=[B_cout]`，`OrderdOps = Unique(B_cout.DependOps) = [B_cin1, B_cin2, B_mul, B_cout]`。

**回答关键问题——为何要为每个首次使用的入参插 CopyIn？**

因为 `in1`、`in2` 的初始值在 **GM**，而 `OpMul` 在 **UB** 上计算。若不插 CopyIn，`B_mul` 执行时 UB 里根本没有 `in1`/`in2` 的数据。CopyIn 是「把 GM 数据搬进 UB」的唯一手段（最终落到 Ascend C 的 `DataCopyPad`），是跨内存层级计算的硬性前置。框架通过 `ParamUsage::IN` 自动识别需要搬入的参数并插入，让用户完全不用手写搬运代码——这正是 ATVOSS「声明式编程」承诺的落地。

**延伸**：把 `out = in1 * in2` 改成两步 `tmp = in1 * in2; out = Sqrt(tmp)`（`tmp` 用 `LocalVar`），重做第 4–6 步。观察 `B_sqrt` 如何依赖 `B_mul`、`B_mul` 如何依赖两个 `B_cin`，DAG 变成三层链。这正是 rms_norm 级联表达式（u3-l7）的雏形。

## 6. 本讲小结

- **DAG 构建是把表达式树变成有序操作序列**：`FullAutoDag` 用一条六步流水线（拆原地参数 → 插搬运 → 转 Bind → 取输出 → 依赖排序 → 内存分析）完成变换，输入是拍平的 `OpAssign` 列表，输出是带缓冲的有序 `Bind` 序列。
- **`AddCopyX` 只管「该插就插」，顺序交给依赖排序**：CopyIn 头插、CopyOut 尾插是粗粒度的，真正的执行顺序由 `Bind` 的依赖闭包拓扑排序（`OrderdOps`）决定。
- **`InplaceParamsProcessor` 把 `IN_OUT` 拆成纯 IN + 纯 OUT 两个版本**，靠 `inplaceNumber` 保持物理上共用同一块 GM，消除原地算子的自环依赖。
- **`Bind` 用「操作入参 = 产生者 Bind」编码数据依赖**：`InNonScalarOps` 是直接依赖，`DependOps` 是记忆化的传递依赖闭包，排序时只需取用、零重复计算。
- **`DagNodeInfo` 做存活分析估缓冲**：沿有序序列扫描存活集合取峰值，结合 ping-pong 等因子给出 LEVEL_0/1/2 三档缓冲数，`ChooseBufferLevel` 选最激进的可装档位。
- **整条流水线运行在编译期类型层面**，零运行时开销，最终由 `Bind2OpAssign` 回填缓冲 ID，交给调用方（schedule.h）做 Alloc/Free 插入后再交给求值器执行。

## 7. 下一步学习建议

- **u3-l4（表达式线性化与图优化 Pass）**：本讲的输入 `OriExprList` 来自 `FlattenAtOpAndThen` 的简单拍平；更完整的线性化（后序遍历 + Simplify 内联、Cast 消除）在 `include/graph/` 下，建议接着读 `expr_linearizer.h`/`expr_flatten.h`，理解复杂表达式如何被「展平 + 优化」成喂给 DAG 的语句列表。
- **u3-l5（Buffer 管理与双缓冲）**：本讲只到「缓冲数量估算」与 `BufMap` 输出，具体的 ping/pong 双缓冲 ID 分配、`GenerateBufferIdOrder` 的内存层级调度、`BlockBufferEx` 的 TPipe 物理分配是下一讲的专题，建议结合 `include/elewise/graph/buffer.h` 与 `include/utils/buf_pool/` 阅读。
- **u3-l6（Reduce 模块图分析）**：含归约/广播的表达式走的是 `include/reduce/graph/` 的另一套 DAG 构建（`InsertCopyIn` 在归约边界插额外拷贝、`IsNodeUsedAfter` 决定缓冲复用），与本章的 `FullAutoDag` 对照阅读，能看清「逐元素」与「归约」两条图处理路径的差异。
- **回看 schedule.h**：读 [schedule.h:52-72](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h#L52-L72) 的 `PreProcessComputeExpr`，把本讲的 DAG 输出与紧随其后的 `AllocInserter`/`FreeInserter`（`compute_preproc.h`）串起来，理解「DAG → Alloc/Free 插入 → 重建表达式 → 求值」的完整 Block 层链路。
