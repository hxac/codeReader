# 表达式线性化与图优化 Pass

> 本讲是专家篇的第 4 讲（u3-l4），承接 u3-l3「计算图构建：DAG 与 Bind」。u3-l3 讲的是「线性化之后的语句列表如何变成带依赖的 DAG」；本讲往**上游**走一步，回答：用户在 `Compute()` 里写下的那棵嵌套表达式树，是怎么先被「拉平成一条语句序列」、再被若干个**编译期优化 Pass** 打磨，最后才交给 DAG 的。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 **后序遍历（post-order）** 如何把一棵嵌套表达式树拆成一条「操作列表」（`TypeList<Op...>`）。
- 区分两条「中间结果落地」路径：`OptimizeWithLocalVars`（用户没写临时变量时，框架自动引入 `LocalVar`）与 `OptimizeBindBuffExpr`（用户已写临时变量时，化简嵌套赋值）。
- 描述 `Simplify` 如何把「只被直接透传一次」的 `LocalVar` 内联回去，减少无谓中间量。
- 掌握 **冗余 Cast 消除**（`RemoveRedundantCast`）的四条判定条件与「解包 + 符号替换」两步改写。
- 掌握 **Alloc/Free 插入 Pass**（`AllocInserter`/`FreeInserter`）如何在一个 `Param`/`LocalVar` 的「首次使用前」插 `OpAlloc`、「末次使用后」插 `OpFree`。
- 把上述步骤串成一条完整的编译期流水线，并明确「线性化」与「DAG + 内存插入」的先后关系。

## 2. 前置知识

本讲默认你已掌握以下概念（均在前面讲义建立）：

- **表达式模板与编译期 AST**（u2-l1）：`Expression<T>` 把计算结构编码在**类型**里，对象是空壳；`OpAssign`（`=` 触发）表示「一条赋值语句」，`OpAndThen`（逗号 `,` 触发）表示「顺序串联」。
- **叶子节点 Param / LocalVar**（u2-l2）：`Param<N>` 对应外部入参/出参，`LocalVar<N>` 是内部临时量，两套序号空间各自从 1 起独立连续。
- **张量算子 OpAlloc/OpFree/OpCopy/OpCopyIn/OpCopyOut**（u2-l3、u3-l2）：`OpAlloc`/`OpFree` 管理 UB 缓冲的申请/释放，`OpCopy` 是 UB→UB 的搬运。
- **DAG 与 Bind**（u3-l3）：`FullAutoDag` 会把语句列表转成带依赖的 `Bind` 序列，并算出每个 `Param`/`LocalVar` 的首用/末用位置（`ParamUseList`/`LocalVarUseList`）。
- **C++ 模板元编程基础**：`TypeList`、偏特化、`if constexpr`、`std::conditional_t`、`std::integral_constant`。

补充一个本讲反复用到的工具——`ForEach`（左折叠）：它把一个函数依次作用到 `TypeList` 的每个元素上，并把上一次的返回值作为下一次的输入，像一条「流水线」一样把一个累积器（`Data`）一路传递下去：

- [include/utils/utility.h:879-889](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/utility.h#L879-L889) —— `ForEach` 的折叠语义：`ForEach(list, f, d) = f(last, ... f(2nd, f(1st, d))...)`。Alloc/Free 插入 Pass 正是靠它把「整个语句列表」一路改写。

## 3. 本讲源码地图

本讲全部围绕 `include/graph/` 目录，这是 ATVOSS 的「编译期图优化管线」所在地：

| 文件 | 作用 | 本讲定位 |
|---|---|---|
| [include/graph/expr_linearizer.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/graph/expr_linearizer.h) | 线性化主入口：后序遍历 → 中间结果落地 → 冗余 Cast 消除 → Simplify | **核心**，串联全流程 |
| [include/graph/expr_cast_eliminate.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/graph/expr_cast_eliminate.h) | 冗余 Cast 消除 Pass | 关键 Pass |
| [include/graph/compute_preproc.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/graph/compute_preproc.h) | Alloc/Free 插入 Pass（`AllocInserter`/`FreeInserter`） | 关键 Pass |
| [include/graph/expr_flatten.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/graph/expr_flatten.h) | 另一种「操作数映射表」式展平实现（配套设计） | 对照理解 |
| [include/graph/expr_operations.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/graph/expr_operations.h) | `ExtractInputs`/`ContainsNodeInExpr` 等表达式分析工具 | 支撑工具（被 DAG 的使用分析复用） |
| [include/elewise/block/schedule.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h) | 把线性化结果接入 DAG 并执行 Alloc/Free 插入的「接线点」 | 流水线落地点 |

## 4. 核心概念与源码讲解

先给一张总览，看清本讲在整条编译链里的位置。注意一个关键事实：**线性化（含 Cast 消除）发生在 DAG 之前，而 Alloc/Free 插入发生在 DAG 之后**。

```
用户 Compute() 写出的嵌套表达式树
        │  ToLinearizerExpr()          ← 本讲 4.1 ~ 4.3（expr_linearizer.h + expr_cast_eliminate.h）
        ▼
优化后的「OpAssign 语句序列」（仍是一棵 OpAndThen 树）
        │  PreProcessComputeExpr()     ← block/schedule.h，进入 u3-l3 的 DAG
        │   ├─ FlattenAtOpAndThen       （把 OpAndThen 树重新拆成 TypeList）
        │   ├─ FullAutoDag              （u3-l3：插 CopyIn/CopyOut、转 Bind、拓扑排序、缓冲分析）
        │   └─ Alloc/Free 插入          ← 本讲 4.4（compute_preproc.h）
        ▼
带 OpAlloc/OpFree 的最终语句序列 → 交给 u3-l1 求值器执行
```

整条流水线里，「谁先谁后」非常重要：**冗余 Cast 消除」必须在「DAG 与内存插入」之前**——因为消除 Cast 改变了语句内容，会连带改变首用/末用位置；而 Alloc/Free 又依赖 DAG 算出的首用/末用。所以顺序只能是「线性化 → DAG → Alloc/Free」。

---

### 4.1 后序遍历提取操作列表

#### 4.1.1 概念说明

用户在 `Compute()` 里可以写得非常「数学化」，例如：

```cpp
return (out = in2 * (in1 / (Sqrt(in1*in1 + ...) + in1)), ...);
```

这是一棵**深度嵌套**的表达式树（`OpMul` 套 `OpDiv` 套 `OpAdd` 套 `OpSqrt`……）。但底层硬件（Vector 计算单元）一次只能执行一条指令，缓冲也只能逐个分配。所以我们第一步必须把这棵树**线性化**（linearize）成一条「先算 A，再算 B，最后算 C」的有序操作列表。

经典做法是 **后序遍历（post-order traversal）**：对每个运算节点，**先递归处理它的子表达式，最后再把自身追加到列表末尾**。这天然保证了「被依赖的计算排在前面」，正好是顺序执行所需的顺序。

为什么叶子（`Param`/`LocalVar`）不进列表？因为它们是「已经存在的数据来源」，本身不是一条需要执行的计算指令——它们只作为操作数的身份出现在后续 `OpAssign` 里。

#### 4.1.2 核心流程

后序遍历的递归规则（对一棵表达式 `Expr`）：

```
postOrder(Param | LocalVar)        →  空列表 []            # 叶子不展开
postOrder(OpAssign<L, R>)          →  postOrder(R) ++ postOrder(L) ++ [OpAssign<L,R>]
                                       # 注意：赋值是「先算右值、再写给左值」，所以 R 在 L 前
postOrder(OpAndThen<A, B>)         →  postOrder(A) ++ postOrder(B)
                                       # OpAndThen 只是顺序胶水，自身不入列表
postOrder(Unary<Inner>)            →  postOrder(Inner) ++ [Unary<Inner>]
postOrder(Binary<L, R>)            →  postOrder(L) ++ postOrder(R) ++ [Binary<L,R>]
```

关键细节有两个：

1. **`OpAssign` 的子树顺序是「先右后左」**：因为 `out = expr` 必须先求出 `expr`（右值），才能写入 `out`（左值），所以右值列表排在左值列表前面。
2. **`OpAndThen` 不产生自己的节点**：它只是「逗号」，负责把左右两段子表达式串起来；真正入列表的是它的子节点。最后整个 `OpAndThen` 树会被收集成一条平铺的 `TypeList`。

提取后还要做一次 `Unique_t` 去重：当某个子表达式在树里**字面重复**出现（同类型），后序遍历会产生重复节点，去重能避免后续重复落地（例如 `(in1+in2)*(in1+in2)` 里的 `OpAdd<in1,in2>`）。

#### 4.1.3 源码精读

后序遍历引擎是 `ExtractTypeListPostOrder`，靠一组偏特化分别处理叶子、二元、一元、带模式参数的算子：

- [include/graph/expr_linearizer.h:88-136](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/graph/expr_linearizer.h#L88-L136) —— 后序遍历的全部特化。其中二元算子的特化是核心。

重点看二元算子特化（含「赋值先右后左」与「OpAndThen 不入列」两条规则）：

- [include/graph/expr_linearizer.h:103-117](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/graph/expr_linearizer.h#L103-L117) —— 用 `IsSpecializationOf_v<OpAssign, ...>` 判断当前是不是赋值：若是，子列表拼成 `rhsList + lhsList`（先右后左）；若是 `OpAndThen`，则末尾追加空 `TypeList`（自身不入列）；其余二元算子按 `lhsList + rhsList` 正常后序。

随后由 `ExprLinearizer` 主结构把后序列表去重，并交给后续 Pass：

- [include/graph/expr_linearizer.h:446-453](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/graph/expr_linearizer.h#L446-L453) —— `ExprLinearizer` 三步：`postOrderList = Unique_t<ExtractTypeListPostOrder<Expr>>`（后序 + 去重），再据用户是否写临时变量选择 `OptimizeWithLocalVars` 或 `OptimizeBindBuffExpr`，接着 `RemoveRedundantCast`，最后 `Simplify`。
- [include/graph/expr_linearizer.h:457-460](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/graph/expr_linearizer.h#L457-L460) —— 对外入口 `ToLinearizerExpr`：把优化后的 `TypeList` 用 `ToOpAndThenExpr` 重新拼成一棵 `OpAndThen` 树返回。

> **配套设计：`expr_flatten.h` 的另一种展平思路**
>
> 同目录下还有一个 `FlattenExprRecursively`（[include/graph/expr_flatten.h:186-198](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/graph/expr_flatten.h#L186-L198)），它用的是「**操作数映射表**」策略：维护一张 `map`，遇到每个非 `Param` 的操作数就给它分配一个新的 `LocalVar`，把原表达式改写成 `OpAssign<新LocalVar, 该操作数>` 追加到结果里，并把后续对该操作数的引用替换成新 `LocalVar`（见 [include/graph/expr_flatten.h:142-169](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/graph/expr_flatten.h#L142-L169) 的 `FlattenOpAssign`）。这是一种「边遍历边物化临时变量」的展平法，与后序遍历「先收集再落地」互补。当前生产链路 `ToLinearizerExpr` 走的是后序遍历方案；`expr_flatten.h` 作为同目录下的配套实现存在，便于对照理解「展平」的本质：**把嵌套的运算树，改写成「每个中间结果都显式赋给一个临时变量」的平铺语句序列**。

#### 4.1.4 代码实践

**实践目标**：用一个现成的单测亲眼确认「后序遍历 + 自动引入临时变量」的效果。

**操作步骤**：

1. 打开 [tests/ut/host/test_expr_linearizer.cpp](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/ut/host/test_expr_linearizer.cpp)。这个测试把同一组计算写成两种形态：
   - `xx`：纯嵌套写法，中间结果用 `_1, _2, ...` 直接内联（[第 37-44 行](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/ut/host/test_expr_linearizer.cpp#L37-L44)）。
   - `xx1`：手动把每个中间结果赋给一个 `temp` 临时变量，串成显式的语句序列（[第 46-50 行](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/ut/host/test_expr_linearizer.cpp#L46-L50)）。
2. 关注最后的断言（[第 52-53 行](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/ut/host/test_expr_linearizer.cpp#L52-L53)）：`std::is_same_v<decltype(xx1), decltype(ToLinearizerExpr(xx))>`。

**需要观察的现象**：断言为真——即 `ToLinearizerExpr(嵌套写法)` 产出的类型，和「手写线性化」的 `xx1` **完全相同**。这证明线性化器能把嵌套表达式自动改写成「显式 `temp = ...` 序列」。

**预期结果**：编译并运行该 host 单测应通过（host 单测不依赖 NPU，可在主机侧验证编译期类型逻辑）。若环境不具备，则把这一步视为「源码阅读型实践」——通过断言读懂「后序遍历 + `OptimizeWithLocalVars` 自动落地」的等价性即可。**待本地验证**（运行命令参见 u1-l2 的 `bash scripts/build.sh -DSOC=ascend950 --host_ut`）。

#### 4.1.5 小练习与答案

**练习 1**：表达式 `out = (in1 + in2) * in1`，写出 `ExtractTypeListPostOrder` 得到的操作列表（去重前）。

**参考答案**：`OpAdd<in1, in2>` 是左子树，后序得 `[OpAdd]`；`in1` 是右子树（叶子，空）；最后追加 `OpMul`。由于最外层是 `OpAssign<out, OpMul<...>>`，按「先右后左」：右值 `OpMul` 的后序 `[OpAdd, OpMul]`，再追加 `OpAssign`。最终：`[OpAdd<in1,in2>, OpMul<OpAdd<in1,in2>, in1>, OpAssign<out, OpMul<...>>]`。去重后 `OpAdd` 只保留一次。

**练习 2**：为什么 `OpAssign` 的后序要先访问右值再访问左值，而普通二元算子（如 `OpMul`）是先左后右？

**参考答案**：`OpAssign<L, R>` 表示「把 R 的结果写入 L」，语义上必须**先求值右值 R**、**再执行写入左值 L**，所以后序顺序是 R 在前、L 在后。普通 `OpMul<L, R>` 只是「同时需要 L 和 R 两个操作数」，没有先后依赖，按默认先左后右即可。

---

### 4.2 Simplify：内联单用临时变量（与中间结果落地）

#### 4.2.1 概念说明

后序遍历得到的列表里，有些中间结果「只被直接透传一次」，例如：

```
localVar1 = in1 * in1
out       = localVar1          # localVar1 只在这里被原样使用
```

这里的 `localVar1` 纯属多余——完全可以直接 `out = in1 * in1`。`Simplify` 就是干这件事的：把「只作为直接透传目标」的 `LocalVar` 内联掉，减少无谓的中间量和缓冲分配。

但要注意一个前提：列表里的「操作」最初可能并不是规整的 `OpAssign` 形式（后序遍历会产生裸的 `OpMul`、`OpCast` 等节点）。所以 `Simplify` 之前还有一步「**中间结果落地**」，分两条路：

- 用户**没写**临时变量（纯嵌套表达式）：走 `OptimizeWithLocalVars`，框架**自动**给每个中间结果分配一个 `LocalVar`，包成 `OpAssign`。
- 用户**已写**临时变量（像 `xx1` 那样）：走 `OptimizeBindBuffExpr`，把可能嵌套的 `OpAssign`（赋值套在另一个运算里）化简平整。

两条路的目的都是得到「一条干净的 `OpAssign` 语句序列」，好让后续 Cast 消除、Simplify、DAG 统一处理。

#### 4.2.2 核心流程

`OptimizeWithLocalVars` 的逐元素处理逻辑（伪代码）：

```
对列表中的每个元素 First（带序号 LocalVarNumber）：
  若 First 已经是 OpAssign → 原样保留，序号不变
  否则（裸的运算节点）：
    构造 localVarType = LocalVar<LocalVarNumber, First::RetType>
    把 First 包成 OpAssign<localVarType, First>
    把「后续列表」里所有与 First 相同的子树，替换成 localVarType
    LocalVarNumber += 1
```

`Simplify` 的内联逻辑（伪代码）：

```
扫描语句序列，维护一张「LocalVar序号 → 其定义表达式」的表 DefList：
  遇到 OpAssign<LocalVar<ID>, expr>：
    登记定义，并检查「后面的语句里，是否存在 OpAssign<Param, LocalVar<ID>>」
    （即 ID 是否只被直接透传给某个出参/入参 Param）
    若是 → 这条定义语句被标记删除（待删除标记）
  遇到 OpAssign<Param, LocalVar<ID>>：
    若 DefList 里有 ID 的定义 → 替换成 OpAssign<Param, 那个定义表达式>（内联）
    同时把「后续语句」里残留的 LocalVar<ID> 替换成对应的 Param
```

核心判据是 `IsLocalVarReferencedByParam`：只有当一个 `LocalVar` 在后续**仅以 `Param = LocalVar` 的形式被引用**（纯透传），它才会被内联消除；如果它参与了真正的运算（如 `Param = LocalVar * x`），则不会被内联，因为内联会改变语句结构。

#### 4.2.3 源码精读

`OptimizeWithLocalVars`（用户无临时变量时的自动落地）：

- [include/graph/expr_linearizer.h:309-355](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/graph/expr_linearizer.h#L309-L355) —— 对非 `Param` 的中间结果用 `localVarType = LocalVar<LocalVarNumber, First::RetType, ...>` 包成 `OpAssign`，并用 `ReplaceOne` 把后续列表中对 `First` 的引用替换为 `localVarType`。`shouldCache = !IsParam_v<First>` 确保 `Param`（已是数据源）不再被包。

`OptimizeBindBuffExpr`（用户已写临时变量时化简嵌套赋值）：

- [include/graph/expr_linearizer.h:357-443](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/graph/expr_linearizer.h#L357-L443) —— 配合 `SimplifyAssign`（[第 376-412 行](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/graph/expr_linearizer.h#L376-L412)）把「运算里嵌套着赋值」的结构（如 `OpAdd(LocalVar2, OpAssign(LocalVar1, ...))`）化简成 `OpAdd(LocalVar2, LocalVar1)`，把内层赋值的左值提出来。

`Simplify` 与其判据：

- [include/graph/expr_linearizer.h:193-307](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/graph/expr_linearizer.h#L193-L307) —— `SimplifyImpl` 用「删除标记」`std::integral_constant<int,0>` 标记待删语句，用 `VarDef<ID,Expr>` 登记定义，递归处理。
- [include/graph/expr_linearizer.h:163-191](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/graph/expr_linearizer.h#L163-L191) —— `IsLocalVarReferencedByParam`：逐条检查后续语句是否形如 `OpAssign<Param, LocalVar<ID>>`，决定 `LocalVar<ID>` 能否被内联消除。

支撑工具——`ReplaceExpr`（编译期类型替换引擎，被多处复用来「把表达式里的某个子类型换成另一个」）：

- [include/graph/expr_linearizer.h:27-83](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/graph/expr_linearizer.h#L27-L83) —— `ReplaceExpr`/`ReplaceRecursive`：对 `Param` 停止递归，对一元/二元/带模式参数的算子递归下降，把匹配 `From` 的子树替换成 `To`。

#### 4.2.4 代码实践

**实践目标**：手工模拟 `OptimizeWithLocalVars`，理解「自动引入 `LocalVar`」的编号规则。

**操作步骤**：

1. 取 4.1.4 里 `xx` 的最简单一维子表达式：`auto s = Sqrt(in1 * in1 + in1)`（`in1` 是 `PlaceHolder<1>`）。
2. 手工列出后序遍历结果：`[OpMul<in1,in1>, OpAdd<OpMul<in1,in1>, in1>, OpSqrt<OpAdd<...>>]`。
3. 按 `OptimizeWithLocalVars` 规则给每个中间结果分配 `LocalVar`：`OpMul` → `LocalVar<1>`，`OpAdd` → `LocalVar<2>`，`OpSqrt` → `LocalVar<3>`。

**需要观察的现象**：`LocalVar` 的序号从 1 起递增，且与 `Param`（这里是 `in1=Param<1>`）**共享 1 起点但属于独立序号空间**（详见 u2-l2）。所以 `LocalVar<1>` 与 `Param<1>` 并不冲突。

**预期结果**：得到序列 `LocalVar<1> = in1*in1; LocalVar<2> = LocalVar<1> + in1; LocalVar<3> = Sqrt(LocalVar<2>)`。这正是「框架替用户把中间结果物化成显式临时变量」的过程。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `OptimizeWithLocalVars` 对 `Param` 不做包装（`shouldCache = !IsParam_v<First>`）？

**参考答案**：`Param` 是外部传入的数据源（已有 GM/UB 存储），它本身不是「需要执行的计算」，也不需要分配新的 `LocalVar` 缓冲去接结果。把它包成 `OpAssign<LocalVar, Param>` 只是无意义的搬运。所以只对「真正的运算节点」落地。

**练习 2**：若 `LocalVar<2>` 同时出现在 `out1 = LocalVar<2> + x` 和 `out2 = LocalVar<2>` 两处，`Simplify` 会内联它吗？

**参考答案**：不会。`Simplify` 的内联条件是「后续**仅以** `Param = LocalVar` 的纯透传形式被引用」。这里 `LocalVar<2>` 在 `out1 = LocalVar<2> + x` 里参与了运算，不是纯透传，所以不会被内联消除。只有当 `LocalVar` 仅被原样赋给某个 `Param` 时才内联。

---

### 4.3 冗余 Cast 消除（RemoveRedundantCast）

#### 4.3.1 概念说明

`Cast` 用于类型转换（如 `int32 → float`），但当**转换前后类型相同**时（如 `float → float`），这次 Cast 是纯粹的冗余——既不改变数值，却会白白占用一次 Vector 指令和一个临时缓冲。

这种冗余在真实算子里很常见。看 `test_cast_elimination.cpp` 里的 RMSNorm 变体（[tests/st/test_cast_elimination.cpp:43-51](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/st/test_cast_elimination.cpp#L43-L51)）：当三个数据类型 `T1=T2=T3=float` 时，表达式里那一连串 `Cast<CAST_NONE, float>(...)`（如 `temp = Cast<...,float>(in1)` 而 `in1` 本就是 `float`）全是冗余的。`RemoveRedundantCast` Pass 的任务就是在编译期把它们识别并消除掉，让最终生成的指令更少、缓冲更省。

#### 4.3.2 核心流程

整个 Pass 分「识别」和「改写」两阶段：

```
阶段一：识别（RedundantCastFinder 逐条扫描语句列表）
  对每条 OpAssign<L, R>：
    条件1: 它是 OpAssign
    条件2: 右值 R 是 OpCast<Mode, TargetT, Src>
    条件3: 被转换的源 Src 是 LocalVar 或 Param（不是又一个嵌套运算）
    条件4: Src 的数据类型 == L 的数据类型（转换前后同类型 = 冗余）
    四条全满足 → 记录一条 (位置i, 被赋值目标L, 源Src)

阶段二：改写（BuildOptimizedList 重建列表）
  对列表里每条语句（带索引 I）：
    Step A「解包」: 若本条正是冗余 Cast 赋值 L = Cast(Src)
                    → 改写成 L = Copy(Src)（同类型无需真正转换，退化为拷贝/绑定）
    Step B「符号替换」: 在「安全区间」[i+1, 下次重定义L) 内，
                       把所有对 L 的引用替换成 Src（因为这段时间 L 的值就等于 Src）
```

「安全区间」是这个 Pass 的精髓：替换不能无限制延伸。`SafeReplaceRange` 会算出从冗余 Cast 的下一条开始、到 **`L` 被再次赋值（重定义）之前**为止——只有在这段区间内，`L` 的值才确定等于 `Src`，引用 `L` 才能安全换成 `Src`。

净效果：冗余的 `float→float` Cast 不再产生真正的转换指令，下游直接读源数据；专门的 Cast 临时缓冲也不再需要。

#### 4.3.3 源码精读

识别器 `RedundantCastFinder`（四条判定条件）：

- [include/graph/expr_cast_eliminate.h:82-116](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/graph/expr_cast_eliminate.h#L82-L116) —— 用 `ForEach` 折叠整个列表，靠 `IndexedData` 带上当前索引。第 90 行判 `OpAssign`、第 94 行判右值是 `OpCast`、第 98 行判源是 `LocalVar`/`Param`、第 100 行 `std::is_same_v<typename CastSourceType::Type, typename TargetType::Type>` 判同类型。命中即追加 `TypeList<索引, L, Src>` 到结果。

安全区间计算 `SafeReplaceRange`：

- [include/graph/expr_cast_eliminate.h:42-56](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/graph/expr_cast_eliminate.h#L42-L56) —— `startPos = 索引+1`；`writePos = startPos + Find_v<「下一条赋值给 L 的语句」, 从 startPos 起的子列表>`。`CheckForAssign`/`IsAssignedTo`（[第 30-40 行](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/graph/expr_cast_eliminate.h#L30-L40)）用来识别「赋值给 L」的语句。

解包 `UnwrapOrNot` / `TryUnwrapRedundantCastInAssign`：

- [include/graph/expr_cast_eliminate.h:185-221](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/graph/expr_cast_eliminate.h#L185-L221) —— `IsRedundantCast` 判定 `(IsLocalVar_v<Src> || IsParam_v<Src>) && Src::Type==Lhs::Type`；命中后 `UnwrapOrNot` 把 `OpAssign<L, OpCast<Mode,Reg,Src>>` 改写成 `OpAssign<L, OpCopy<Src>>`。

符号替换 `ReplaceSymbol` / `ApplyReplacements`：

- [include/graph/expr_cast_eliminate.h:146-183](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/graph/expr_cast_eliminate.h#L146-L183) —— `ApplyReplacements` 判断当前索引是否落在某条记录的 `[Start, End)` 区间内，若是则用 `ReplaceSymbol` 把 `OldSym`(=L) 换成 `NewSym`(=Src)。

重建与总入口：

- [include/graph/expr_cast_eliminate.h:223-243](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/graph/expr_cast_eliminate.h#L223-L243) —— `BuildOptimizedList` 逐索引重建：先 Step A 解包、再 Step B 替换。
- [include/graph/expr_cast_eliminate.h:245-261](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/graph/expr_cast_eliminate.h#L245-L261) —— `RemoveRedundantCast` 总入口：若没识别到任何冗余 Cast，原样返回；否则算出各记录的安全区间并重建列表。

#### 4.3.4 代码实践

**实践目标**：对照一个真实的「全冗余 Cast」用例，理解 Pass 的输入输出。

**操作步骤**：

1. 打开 [tests/st/test_cast_elimination.cpp:43-51](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/st/test_cast_elimination.cpp#L43-L51)，观察 `RmsNormCompute` 里大量 `Cast<CAST_NONE, DtypeX>(...)`。
2. 注意 `main` 里调用的是 `Run<float, float, float>()`（[第 143 行](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/st/test_cast_elimination.cpp#L143)）：三个类型都是 `float`，所以这些 `Cast<...,float>(float量)` 全部满足「同类型」冗余条件。
3. 想象 `RemoveRedundantCast` 跑完后的效果：这些 `Cast` 要么被解包成 `Copy`、要么其下游引用被替换为源，从而不再生成真正的类型转换指令。

**需要观察的现象**：即便表达式里写了十几次 `Cast`，最终下发到 NPU 的真实计算里不应包含任何「float 转 float」的冗余转换指令；缓冲分配也会因此更省。

**预期结果**：该 ST 用例的 golden 为全 2.0（[第 132 行](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/tests/st/test_cast_elimination.cpp#L132)），运行应输出 `Accuracy verification passed.`，间接证明消除 Cast 不改变计算正确性。**待本地验证**（需 NPU 或 cannsim 环境）。

#### 4.3.5 小练习与答案

**练习 1**：如果源 `Src` 本身又是一个运算节点（如 `Cast<float>( Sqrt(x) )`），`RedundantCastFinder` 会把它当冗余吗？

**参考答案**：不会。条件 3（[第 98 行](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/graph/expr_cast_eliminate.h#L98)）要求 `Src` 是 `LocalVar` 或 `Param`。若 `Src` 是嵌套运算（既非 `LocalVar` 也非 `Param`），直接走第 98 行的「不处理」分支返回空。这样设计是因为「替换下游对 L 的引用为 Src」要求 Src 是一个**可直接命名的变量**，嵌套表达式无法整体替换。

**练习 2**：「安全区间」为什么必须以「`L` 的下次重定义」为右端点？

**参考答案**：在区间 `[i+1, 重定义L)` 内，`L` 没有被再次赋值，所以 `L` 的值恒等于那次冗余 Cast 的源 `Src`，引用 `L` 可以安全换成 `Src`。一旦 `L` 被重新赋值（承载了新值），`L` 就不再等于 `Src`，再替换就会改变语义。所以右端点必须卡在「下次重定义」处。

---

### 4.4 Alloc/Free 插入 Pass（compute_preproc.h）

#### 4.4.1 概念说明

经过 4.1～4.3，我们得到一条「优化好的 `OpAssign` 语句序列」。但这条序列里只有**计算和搬运**，还缺少**缓冲的生命周期管理**：每个在 UB 里用的 `Param`/`LocalVar`，都需要在某处 `OpAlloc`（申请 UB 切片）、用完 `OpFree`（释放）。u3-l2 讲过，`OpAlloc`/`OpFree` 最终落到 `bufPools.AllocTensor`，是 UB 内存复用的关键。

插入的原则很自然——**最小化缓冲占用**：

- `OpAlloc` 插在「首次使用」**之前**（越晚申请越好，缩短存活期）。
- `OpFree` 插在「末次使用」**之后**（越早释放越好，给后续腾出空间）。

「首用/末用位置」不是本 Pass 自己算的，而是 u3-l3 的 `FullAutoDag` 在做依赖与存活分析时一并产出的（`ParamUseList`、`LocalVarUseList`）。本 Pass 只负责「拿着这些位置，把 `OpAlloc`/`OpFree` 插进去」。

#### 4.4.2 核心流程

每个 `Param`/`LocalVar` 在使用列表里是一条三元组 `<变量, 首用索引, 末用索引>`。两个插入器的工作：

```
AllocInserter（对某变量 v，首用索引 f）：
  取列表第 f 条语句 oldStmt
  改写成 OpAndThen<OpAlloc<v>, oldStmt>   # 把 Alloc 前置到首用语句之前
  用新语句替换第 f 位

FreeInserter（对某变量 v，末用索引 l）：
  取列表第 l 条语句 oldStmt
  改写成 OpAndThen<oldStmt, OpFree<v>>    # 把 Free 后置到末用语句之后
  用新语句替换第 l 位
```

两个工程细节：

1. **标量不插**：`if constexpr (!std::is_scalar_v<...>)`——标量（如 `muls` 里的乘数）不占 UB 张量缓冲，无需 Alloc/Free。
2. **插入顺序**：Alloc 按 `ParamUseList` 的**逆序** `Reverse_t` 插入，Free 按**正序**插入。因为可能有多个变量共享同一索引（同一条语句里首用多个变量），`ForEach` 是顺序折叠并逐个 `Set_t` 替换，逆序/正序的配对能保证「同一索引上的多重插入」以确定、不互相覆盖的方式叠加。

完整的插入位置可以用一条时间轴表示。设 `in` 的首用是第 2 条、末用是第 4 条：

```
语句序列:  [ s0, s1, s2, s3, s4, s5 ]
                   ↑首用        ↑末用
插 Alloc:  ... s1, (OpAlloc<in> ; s2), s3, s4, s5 ...      # Alloc 紧贴 s2 之前
插 Free:   ... s1, OpAlloc<in>; s2, s3, s4( ; OpFree<in>), s5 ...   # Free 紧贴 s4 之后
```

#### 4.4.3 源码精读

`AllocInserter`（首用前插 Alloc）：

- [include/graph/compute_preproc.h:29-44](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/graph/compute_preproc.h#L29-L44) —— 从三元组取 `Param`（`Get_t<ParamUse,0>`）与首用索引 `firstUse`（`Get_t<ParamUse,1>`）；标量直接返回原列表；否则把第 `firstUse` 条语句改写为 `OpAndThen<OpAlloc<Param>, OldItem>` 并 `Set_t` 替换。

`FreeInserter`（末用后插 Free）：

- [include/graph/compute_preproc.h:46-61](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/graph/compute_preproc.h#L46-L61) —— 取末用索引 `lastUse`（`Get_t<ParamUse,2>`）；把第 `lastUse` 条语句改写为 `OpAndThen<OldItem, OpFree<Param>>`。

接线点——`PreProcessComputeExpr` 把「线性化结果 → DAG → Alloc/Free 插入」串起来：

- [include/elewise/block/schedule.h:52-72](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h#L52-L72) —— 关键四步：
  - `OriExprList = FlattenAtOpAndThen<Expr>`：把 `ToLinearizerExpr` 产出的 `OpAndThen` 树重新拆回 `TypeList`（[第 56 行](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h#L56)）。
  - `DagX = FullAutoDag<OriExprList>`：u3-l3 的 DAG 构建，产出 `ExprListWithCopyX`（已插 CopyIn/CopyOut）与首用/末用列表（[第 60-64 行](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h#L60-L64)）。
  - 四次 `ForEach`：Param 逆序 Alloc、Param 正序 Free、LocalVar 逆序 Alloc、LocalVar 正序 Free（[第 65-68 行](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h#L65-L68)）。
  - `BuildExpression<result4>`：把插完内存操作的列表重新拼成表达式（[第 70 行](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h#L70)）。

而这一切的起点，是 Block 层先用 `ToLinearizerExpr` 把 `Compute()` 线性化（注意先后：**先线性化、后进 DAG**）：

- [include/elewise/block/schedule.h:122-123](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h#L122-L123) —— `computeRes = ToLinearizerExpr(Compute{}.Compute<BlockTensorTile>())`，再 `PreProcessComputeExpr<memPolicy>(computeRes)`。

同样的 `ToLinearizerExpr` 也出现在 Kernel 层与 Device 层（说明三级 Builder 都会先线性化表达式）：

- [include/elewise/kernel/schedule.h:38](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/kernel/schedule.h#L38) —— Kernel 层线性化入口。
- [include/elewise/device/device_adapter.h:100](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/device/device_adapter.h#L100) —— Device 层线性化入口。

支撑工具——`expr_operations.h`（被 DAG 的使用分析复用，是首用/末用统计的地基）：

- [include/graph/expr_operations.h:22-62](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/graph/expr_operations.h#L22-L62) —— `ExtractInputs`：递归收集一条表达式里的所有 `Param`/`LocalVar` 叶子（用来判断某语句「用到哪些变量」）。
- [include/graph/expr_operations.h:64-98](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/graph/expr_operations.h#L64-L98) —— `ContainsNodeInExpr`：判断某个目标节点是否出现在表达式里（用来判断「某条语句是否用到了某个变量」）。DAG 正是用这类工具逐条扫描、统计出每个变量的首用/末用索引，喂给本讲的 `AllocInserter`/`FreeInserter`。

#### 4.4.4 代码实践（本讲主任务）

**实践目标**：把 4.3（Cast 消除）和 4.4（Alloc/Free 插入）两件事在一道题里串起来——这正是本讲规格里要求的核心实践。

**题目**：给定表达式（`in` 本身就是 `float`）：

```cpp
tmp = Cast<float>(in);   // 冗余：float → float
out = tmp * in;
```

请回答两问：(1) `RemoveRedundantCast` 如何识别并消除这次冗余 Cast？(2) 用 `AllocInserter`/`FreeInserter` 的逻辑，说明 `in` 的 `OpAlloc` 与 `OpFree` 会被插在哪一步的前后。

**操作步骤与参考分析**：

**第(1)问——冗余 Cast 的识别与消除**：

a. 经 4.1～4.2 后，语句序列大致为（`tmp` 是 `LocalVar`，`in` 是输入 `Param`）：

```
[0] OpAssign<tmp, OpCast<CAST_NONE, float, in>>
[1] OpAssign<out, OpMul<tmp, in>>
```

b. `RedundantCastFinder` 扫描第 0 条，逐条核对四条件：① 是 `OpAssign` ✓；② 右值是 `OpCast` ✓；③ 源 `in` 是 `Param` ✓（[第 98 行](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/graph/expr_cast_eliminate.h#L98)）；④ `in::Type`(float) == `tmp::Type`(float) ✓（[第 100 行](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/graph/expr_cast_eliminate.h#L100)）。四条全满足，记录 `<0, tmp, in>`。

c. `SafeReplaceRange` 算安全区间：`startPos = 1`；从第 1 条往后找「下次赋值给 `tmp`」的语句——没有，故区间延伸到列表末尾，即 `[1, 末尾)`。

d. `BuildOptimizedList` 重建：第 0 条经 `TryUnwrapRedundantCastInAssign` 解包成 `tmp = Copy(in)`；第 1 条落在区间 `[1, 末尾)` 内，经 `ApplyReplacements` 把 `tmp` 替换为 `in`，变成 `out = in * in`。净效果：冗余的 `float→float` Cast 不再产生真正转换指令，`out` 直接读 `in`，无需为这次同类型转换单独分配缓冲。

**第(2)问——`in` 的 Alloc/Free 插入位置**：

a. Cast 消除发生在**线性化阶段（DAG 之前）**；Alloc/Free 插入发生在**DAG 之后**（`PreProcessComputeExpr`，[block/schedule.h:65-68](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h#L65-L68)）。所以插入针对的是「Cast 消除后 + DAG 插完 CopyIn/CopyOut」的那份列表。

b. `in` 是输入 `Param`。DAG 会先给它插 `CopyIn`（把 GM 搬进 UB），并统计 `in` 的首用索引 `f` 与末用索引 `l`（`ParamUseList` 里的一条 `<in, f, l>`）。

c. `AllocInserter`（[compute_preproc.h:29-44](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/graph/compute_preproc.h#L29-L44)）：在第 `f` 条语句**之前**插入 `OpAlloc<in>`，即把第 `f` 条改写为 `OpAndThen<OpAlloc<in>, 原第f条>`。含义——`in` 的 UB 缓冲「用到才申请」。

d. `FreeInserter`（[compute_preproc.h:46-61](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/graph/compute_preproc.h#L46-L61)）：在第 `l` 条语句**之后**插入 `OpFree<in>`，即把第 `l` 条改写为 `OpAndThen<原第l条, OpFree<in>>`。含义——`in` 用完立即释放，UB 空间让给后续变量。

**需要观察的现象 / 预期结果**：

- Cast 消除让 `in` 的「使用范围」可能缩短（`tmp` 不再是独立中间环节），这会反过来影响 DAG 算出的首用/末用索引——这正是「先消除 Cast、再算使用范围、最后插 Alloc/Free」这一顺序的意义。
- 最终 `in` 的生命周期被 `OpAlloc` / `OpFree` 精确夹在「首用前 … 末用后」，UB 占用最小化。若想亲见，可在 `AllocInserter`/`FreeInserter` 处加一行编译期 `static_assert` 或类型打印（见综合实践）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `AllocInserter` 对标量（`std::is_scalar_v` 为真）直接返回原列表、什么都不插？

**参考答案**：标量（如 `muls` 的乘数 `in3`，见 4.1.4 测试里的 `PlaceHolder<3, float, IN>`）不占用 UB 的张量缓冲，它通过 Vector 指令的立即数/标量寄存器参与运算。`OpAlloc`/`OpFree` 管理的是 `LocalTensor` 切片，对标量无意义，故跳过（[compute_preproc.h:35](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/graph/compute_preproc.h#L35) 与 [:52](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/graph/compute_preproc.h#L52)）。

**练习 2**：若把 `AllocInserter` 也改成按 `ParamUseList` 正序插入（而非逆序），在「多个变量首用索引相同」时可能出什么问题？

**参考答案**：`ForEach` 是顺序折叠，每步用 `Set_t` 替换同一索引处的语句。若多个变量共享同一首用索引 `f`，正序插入会让「先处理的变量」的 `OpAndThen<OpAlloc<v1>, stmt>` 被后处理的变量再次 `Set_t` 时，需要正确地把 `OpAlloc<v2>` 叠在已改写过的语句外层。逆序插入是为了给出确定的叠加顺序、避免覆盖错位。总之顺序变了，多重 Alloc 的嵌套层级会随之改变，可能打乱预期的申请次序。

---

## 5. 综合实践

**任务**：在 `abs` 这类最简单的算子上，跟踪一条完整的「表达式 → 线性化 → Cast 消除 → DAG → Alloc/Free → 求值」路径，并用编译期手段「看见」Alloc/Free 被插进了哪里。

**操作步骤**：

1. **准备一个简单 Compute**：参照 [examples/abs/abs.cpp](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/abs/abs.cpp)（u1-l4 已详述），它的核心就是 `return (out = Abs(in));`。
2. **跟踪线性化**：在脑中（或纸上）走一遍 `ToLinearizerExpr`：
   - 后序遍历 `OpAssign<out, OpAbs<in>>` → 得到含 `OpAbs`、`OpAssign` 的列表。
   - `Abs` 不涉及 Cast，`RemoveRedundantCast` 原样返回。
   - 无单用 `LocalVar`，`Simplify` 不变。
3. **定位 Alloc/Free**：`in`（输入 Param）与 `out`（输出 Param）会被 DAG 插上 CopyIn/CopyOut，并由 `AllocInserter`/`FreeInserter` 在首用前/末用后插 `OpAlloc`/`OpFree`。
4. **加一个编译期「探针」**（**示例代码**，非项目原有代码）：在 `include/graph/compute_preproc.h` 的 `AllocInserter::operator()` 里临时加一行，让编译器报错时打印类型：

   ```cpp
   // 示例代码：仅用于观察，验证后请删除
   static_assert(sizeof(Param) == 0, "ATVOSS_ALLOC_FOR: " /* 借报错信息看 Param 类型 */);
   ```

   编译时报错信息会暴露当前正在被插 Alloc 的 `Param` 类型，从而「看见」`in`/`out` 各被处理了一次。

5. **观察现象**：
   - 未加探针时，算子正常编译并可在仿真/真机运行。
   - 加探针后，编译器会在 `AllocInserter` 对每个非标量 Param 触发处报错，报错次数 ≈ 输入输出张量数，证明 Alloc 被插在了「每个张量首用前」。

**预期结果**：你能复述出 `abs` 的整条编译期变换链，并确认 `OpAlloc<in>` 在 `Abs(in)` 之前、`OpFree<in>` 在其后；`out` 同理。**待本地验证**（探针改动仅用于学习，验证后务必还原，不可提交）。

> ⚠️ 提醒：本实践要求「只读源码」原则下的**临时**探针。worker 规则禁止修改源码入库；若你只是本地学习，加探针后请务必还原。

## 6. 本讲小结

- **后序遍历**（`ExtractTypeListPostOrder`）把嵌套表达式树拆成有序操作列表；`OpAssign` 的子序是「先右后左」，`OpAndThen` 自身不入列，最后 `Unique_t` 去重。
- **中间结果落地**有两条路：用户无临时变量时 `OptimizeWithLocalVars` 自动引入 `LocalVar`；用户已写时 `OptimizeBindBuffExpr` 化简嵌套赋值。二者都产出「干净的 `OpAssign` 序列」。
- **`Simplify`** 把「仅以纯透传形式被引用一次」的 `LocalVar` 内联回去（判据是 `IsLocalVarReferencedByParam`），减少无谓中间量。
- **冗余 Cast 消除**（`RemoveRedundantCast`）靠四条件（`OpAssign` + 右值是 `OpCast` + 源是 `LocalVar`/`Param` + 同类型）识别，用「解包成 `Copy` + 安全区间内符号替换」两步消除；它发生在 DAG **之前**。
- **Alloc/Free 插入**（`AllocInserter`/`FreeInserter`）在 DAG **之后**，据首用/末用索引把 `OpAlloc` 插在首用前、`OpFree` 插在末用后，标量跳过；Alloc 逆序、Free 正序以稳定多重插入。
- **顺序铁律**：线性化（含 Cast 消除）→ DAG（含 CopyIn/CopyOut 与首用/末用统计）→ Alloc/Free 插入 → 求值器执行。这一顺序的根因是：Cast 消除改变语句内容、进而改变使用范围，而 Alloc/Free 又依赖使用范围。

## 7. 下一步学习建议

- **u3-l5（Buffer 管理与双缓冲）**：本讲的 `OpAlloc`/`OpFree` 只是「在语句序列里插入申请/释放标记」；真正把这些标记映射到 UB 物理切片、决定 ping/pong 双缓冲的是 `buffer.h` 与 `buf_pool`。建议接着读 `include/elewise/graph/buffer.h`，理解「 Alloc/Free 标记」如何变成「具体 bufferId 的双缓冲分配」。
- **u3-l6（Reduce 模块：归约图分析）**：本讲的 Cast 消除是「通用」Pass；含 `ReduceSum`/`Broadcast` 的表达式在图层会被**特殊处理**（边界插额外 Copy、划分 PreReduce/Reduced 段），那是另一套图分析逻辑。
- **回看 u3-l1（求值器系统）**：本讲产出的「带 Alloc/Free 的语句序列」最终由 `Evaluator<OpAndThen<...>>` 递归求值落地为 Ascend C 指令。带着本讲的语句序列去重读 u3-l1，能看清「优化后的语句」如何一条条变成硬件指令。
- **拓展阅读**：对照 `expr_flatten.h` 的「操作数映射表」式展平与本讲的后序遍历式展平，体会「同一目标（线性化）的两种编译期实现策略」的取舍。
