# 控制流：循环与条件分支

## 1. 本讲目标

CUDA Tile IR 是一种「结构化」的 IR：循环和分支都是带 region（区域）的操作，而不是一串 `goto` 或跳转标签。本讲集中讲解 Control Flow（控制流）分组里的 8 个操作——`for` / `loop` / `if` / `break` / `continue` / `yield` / `return` / `assert`——它们一起构成了一个 Tile 内核内部的控制能力。

学完本讲，你应该能够：

1. 说清楚 `for`（范围循环）和 `loop`（无限循环 + `break` 退出）在 region 结构和终止符上的差异。
2. 用 `if` 的 `then` / `else` region 配合 `yield` 写出「按条件返回不同值」的分支，并理解为什么 `if` 分支还能被 `break` / `continue` / `return` 提前终止。
3. 理解 `ControlFlowImplicitTerminatorOpType` 这个 C++ 工具如何让一个 region 块「支持多种合法终止符、缺省时自动补一个」。
4. 在 `operationsTest.mlir` 这类可运行用例基础上，自己写出合法的循环 + 分支 MLIR，并用 `cuda-tile-opt` 验证。

本讲承接 [u4-l2 整数算术与比较操作](u4-l2-integer-arith.md) 中引入的 `tile<i1>` 掩码/条件类型——`if` 和 `assert` 的条件正是 `tile<i1>`。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**第一，什么叫「带 region 的操作」。** MLIR 里很多操作自带一个或多个「代码块区域」（region），region 里又装着若干 block，block 里是一串操作。`for`、`loop`、`if` 都是这样：它们的「循环体」「分支体」就是一个 region。嵌套关系是「操作里装 region、region 里装 block、block 里装操作」。

**第二，什么叫「终止符（terminator）」。** MLIR 规定每个 block 的最后一个操作必须是终止符，用来表明「这个块到这儿就结束了，控制流接下来去哪」。比如 `continue` 是循环体块的终止符、`yield` 是 `if` 分支块的终止符。终止符决定了控制流怎么「交接」给父操作。

**第三，什么叫「隐式终止符」。** 很多时候循环体或分支体写完，作者懒得显式写一个 `continue` / `yield`。MLIR 提供 `SingleBlockImplicitTerminator` 机制：如果块的末尾没有终止符，编译器会自动补一个「隐式」的。本讲的重点之一就是 CUDA Tile 把这个机制做成了「支持多种终止符」的增强版（`ControlFlowImplicitTerminatorOpType`）。

还有一个 MLIR 约定要先记住：**「父操作决定子终止符的语义」**。`yield` 在 `if` 里表示「把分支结果交还给 if」，在 `reduce`/`scan` 里表示「把一次运算结果交还给归约/扫描」；`break` 和 `continue` 在 `for` / `loop` 里才有意义。同一个 `continue`，父是 `for` 还是 `loop`，它的「下一步」不同。这正是「父操作决定语义」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `include/cuda_tile/Dialect/CudaTile/IR/Ops.td` | TableGen 定义。Control Flow 组的全部 8 个操作都声明在这里，包括各自接受的操作数、region 数量、trait（如 `SingleBlockImplicitTerminator`、`ParentOneOf`）。 |
| `include/cuda_tile/Dialect/CudaTile/IR/Ops.h` | C++ 工具头文件。核心是 `ControlFlowImplicitTerminatorOpType` 模板，以及 `IfOpImplicitTerminatorType` / `LoopOpImplicitTerminatorType` 两个别名。 |
| `include/cuda_tile/Dialect/CudaTile/IR/Dialect.td` | 方言骨架。`CudaTileControlFlowOpDef` 是本组操作的公共基类，把操作归入「Control Flow」分组。 |
| `lib/Dialect/CudaTile/IR/CudaTile.cpp` | C++ 实现。各操作的 `verify()` / `verifyRegions()` 在这里，负责检查「归纳变量类型对不对」「yield 的值类型和 if 结果类型一不一致」「break/continue 是不是真在一个循环里」等运行时不变式。 |
| `test/Bytecode/operationsTest.mlir` | 可运行测试。`for_op` 和 `if_else_op_test` 是本讲的标准参考写法，配合 `%round_trip_test` 跑字节码往返。 |

## 4. 核心概念与源码讲解

### 4.1 Control Flow 组总览：八大操作与终止符分工

#### 4.1.1 概念说明

Control Flow 组是 cuda_tile 方言 11 个操作分组之一（见 [u2-l2](u2-l2-dialect-definition.md) 的分组索引）。它包含 8 个操作，可以按「角色」分成三类：

- **构造控制流结构的「容器操作」**：`for`（范围循环）、`loop`（无限循环）、`if`（条件分支）。它们自带 region，是「大块头」。
- **结束某个块的「终止符操作」**：`yield`（向 `if`/`reduce`/`scan` 交还值）、`continue`（回到循环下一轮）、`break`（跳出循环）、`return`（从内核返回）。
- **诊断操作**：`assert`（条件为假时打印错误并向主机报错）。

关键设计原则是**「父操作决定子终止符语义」**：同一个 `continue`，写在 `for` 里表示「进入下一次范围迭代」，写在 `loop` 里表示「重新执行循环体」；同一个 `yield`，写在 `if` 里交还分支结果，写在 `reduce` 里交还一次归约结果。TableGen 用 `ParentOneOf<...>` 这个 trait 强制「这个终止符只能出现在某些父操作里」。

#### 4.1.2 核心流程

把 8 个操作画成一张「谁能装在谁里面」的图：

```
entry（内核）────── 必须以 return 收尾（entry 不能返回值，所以 return 无操作数）
  └─ for（范围循环）── 体块以 continue 收尾（continue 可带循环携带值）
  │    └─ if ── then/else 块以 yield 收尾；也可被 break/continue/return 提前终止
  └─ loop（无限循环）── 体块以 continue 或 break 收尾
       └─ if ── 同上
```

一句话：**`for` 只认 `continue`；`loop` 认 `continue` 和 `break`；`if` 认 `yield`，但还「放行」`break`/`continue`/`return` 让它们穿透到外层循环或函数**。这个「放行/穿透」正是 4.5 节那个工具的由来。

#### 4.1.3 源码精读

公共基类 `CudaTileControlFlowOpDef` 把所有这些操作统一归入「Control Flow」分组，并带上 `sinceVersion`（本组操作基本都是 13.1 引入）：

[Dialect.td:166-168](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Dialect.td#L166-L168) —— 这是分组基类，第三参数 `"Control Flow"` 决定了规范文档与字节码里的分组归属。

各终止符的 `ParentOneOf` 约束是最值得看的「谁能装在谁里」的声明。以 `break` 为例：

[Ops.td:938-940](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L938-L940) —— `break` 声明 `ParentOneOf<["IfOp", "LoopOp"]>`。注意这里写的是「IfOp 或 LoopOp」，但实际语义是「`break` 可以写在 `if` 里、但必须最终落在 `loop` 里」——这个「穿透 if」的细节由 C++ verifier 进一步收紧（见 4.3 节）。`ReturnLike` + `Terminator` 两个 trait 标明它是终止符且像 return 一样「中断当前控制流」。

`continue` 的约束是 `ParentOneOf<["ForOp", "IfOp", "LoopOp"]>`，见 [Ops.td:1197-1198](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L1197-L1198)（注：此处链接 hash 已对齐当前 HEAD，真实路径以仓库为准），它既能回到 `for` 也能回到 `loop`。

`yield` 的约束是 `ParentOneOf<["IfOp", "ReduceOp", "ScanOp"]>`，见 [Ops.td:5001-5004](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L5001-L5004)——注意 `yield` **不**服务于循环控制（循环用 `break`/`continue`），这与标准 MLIR 的 `scf.yield` 不同，源码注释里专门强调过这一点。

`return` 的父约束包含 `EntryOp`（和测试用的 `Test_FuncOp`），见 [Ops.td:4085-4090](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L4085-L4090)。`assert` 则没有 region、不是终止符，只是一个普通副作用操作，见 [Ops.td:216-244](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L216-L244)。

#### 4.1.4 代码实践

**实践目标**：把 8 个操作按「容器 / 终止符 / 诊断」分类，并核对它们的 `ParentOneOf`。

**操作步骤**：

1. 打开 `include/cuda_tile/Dialect/CudaTile/IR/Ops.td`，搜索 `def CudaTile_AssertOp`、`def CudaTile_ForOp`、`def CudaTile_LoopOp`、`def CudaTile_IfOp`、`def CudaTile_BreakOp`、`def CudaTile_ContinueOp`、`def CudaTile_YieldOp`、`def CudaTile_ReturnOp`。
2. 逐一记录每个操作的 `ParentOneOf<...>` 列表（容器操作没有这个 trait）。

**预期结果**：你会得到一张「终止符 → 允许的父操作」的映射表，与本节 4.1.2 的图一致。

**需要观察的现象**：`yield` 不出现在任何循环的允许父列表里；`break` 不指向 `ForOp`（因为 `for` 不能提前退出，只能在范围耗尽时结束）。

> 待本地验证：如果你已构建项目，可用 `grep -n "ParentOneOf" include/cuda_tile/Dialect/CudaTile/IR/Ops.td` 快速核对。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `break` 的 `ParentOneOf` 写了 `IfOp`？`break` 真的能直接终止一个 `if` 吗？

**参考答案**：不能直接终止 `if`。`IfOp` 出现在列表里是因为 `break` 允许写在 `if` 分支块**内部**，用来穿透 `if` 去终止外层 `loop`。`ParentOneOf` 描述的是「允许的最近父操作类型」，C++ verifier 会进一步要求沿着祖先链一路向上必须是 `if`，直到碰到 `loop`（详见 4.3 节 `verifyEarlyExitOp`）。

**练习 2**：`yield` 能不能写在 `for` 循环体里？

**参考答案**：不能。`yield` 的 `ParentOneOf` 是 `["IfOp", "ReduceOp", "ScanOp"]`，不含任何循环操作。循环的「交还值」由 `continue` 完成。源码注释明确指出：与标准 MLIR 不同，CUDA Tile 的 `yield` 不用于循环控制流（见 [Ops.td:5014-5017](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L5014-L5017)）。

---

### 4.2 for 循环：归纳变量与 iter_values 循环携带值

#### 4.2.1 概念说明

`for` 是一个**结构化的范围循环**：给定 `lowerBound`、`upperBound`、`step` 三个标量整数 tile，归纳变量（induction variable，`%iv`）从下界出发，每次加 `step`，直到「大于等于上界」停止。它对应 C 语言里的 `for (iv = lb; iv < ub; iv += step)`。

除了遍历整数范围，`for` 还支持**循环携带值（loop-carried values）**：一组在迭代间传递的值，类似 CUDA C++ 里反复更新的累加器。在 MLIR 文本里用 `iter_values(%value = %init) -> (result_type)` 声明。每次迭代开始时 `%value` 是上一次迭代 `continue` 交还的值（第一次是 `%init`），循环结束后 `for` 的返回值就是最后一次迭代交还的值。

#### 4.2.2 核心流程

迭代空间是一个左闭右开区间（含下界、不含上界）：

\[
range(L_b, U_b, S) = \{\, L_b + i \cdot S \mid i \in \mathbb{Z},\ L_b + i \cdot S < U_b \,\}
\]

要点（来自操作的官方描述）：

- `step` 必须为正；上下界可以是负数或零。
- 三个界都是**有符号整数**解释；默认用有符号比较判断终止，可用 `unsigned` 属性改用无符号比较（13.2 引入）。
- region 的第一个 block 参数固定是归纳变量 `%iv`，其类型与三个界相同；之后的 block 参数是循环携带值的「本轮入口副本」。
- 体块必须以 `continue` 收尾，`continue` 带的操作数就是「交还给下一次迭代」的循环携带值。
- **`for` 不允许提前退出**：没有 `break`，必须老老实实跑完范围（或被外层机制打断）。

伪代码（带一个循环携带值 `acc`，做 `acc = acc + iv`）：

```
%result = for %iv in (%lb to %ub, step %step) : tile<i32>
          iter_values(%acc = %init) -> (tile<i32>) {
  %new = addi %acc, %iv          // 用本轮入口值 %acc 算新值
  continue %new : tile<i32>      // 把新值交还，成为下一轮的 %acc
}
// 循环结束后，%result == 最后一次交还的 %new
```

#### 4.2.3 源码精读

`for` 的 TableGen 定义是本组里最「重」的一个，重点看它的 trait 和参数：

[Ops.td:1891-1899](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L1891-L1899) —— 关键 trait解读：
- `AutomaticAllocationScope`：循环体是一个分配作用域，体里的 `alloca` 生命周期限于单次迭代（承接 [u5-l1 内存模型](u5-l1-memory-model-tokens.md) 里的 `alloca`）。
- `AllTypesMatch<["lowerBound", "upperBound", "step"]>`：三个界同类型。
- `AllTypesMatch<["initValues", "resultValues"]>`：初始值和最终返回值同类型。
- `SingleBlockImplicitTerminator<"ContinueOp">`：体块缺省终止符是 `continue`——这解释了为什么最简单的 `for` 体里可以什么都不写。

`for` 的操作数与 region 声明在 [Ops.td:1971-1979](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L1971-L1979)：下界/上界/步长都是 `CudaTile_ScalarTileOf<CudaTile_AnyInt>`（标量整数 tile），`initValues` 是变长（variadic）操作数，region 是 `SizedRegion<1>`（恰好一个块）。

C++ 侧的 region 校验 `ForOp::verifyRegions` 很简洁：

[CudaTile.cpp:2524-2537](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L2524-L2537) —— 做两件事：① region 至少有一个 block 参数（归纳变量），且归纳变量类型必须与下界相同；② 调 `verifyLoopIterValues` 校验「循环携带值的个数、类型、以及不能是 `tensor_view`/视图类型」。其中 `verifyLoopIterValues` 见 [CudaTile.cpp:2433-2450](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L2433-L2450)，它显式禁止把 `tensor_view` 或 TileView 当循环携带值（因为这些是全局显存的几何描述符，不能逐次迭代「传递」）。

#### 4.2.4 代码实践

**实践目标**：读懂标准 `for` 写法，并验证「循环携带值在迭代间传递」。

**操作步骤**：

1. 打开 [test/Bytecode/operationsTest.mlir:40-49](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Bytecode/operationsTest.mlir#L40-L49)，这是 `@for_op` 用例：

   ```mlir
   cuda_tile.entry @for_op(%a: !cuda_tile.tile<i32>) {
     %lower = cuda_tile.constant <i32: 0> : !cuda_tile.tile<i32>
     %upper = cuda_tile.constant <i32: 5> : !cuda_tile.tile<i32>
     %step  = cuda_tile.constant <i32: 1> : !cuda_tile.tile<i32>
     %result = cuda_tile.for %iv in (%lower to %upper, step %step) : tile<i32>
               iter_values(%value = %a) -> (tile<i32>) {
       %new_value = cuda_tile.addi %value, %iv : tile<i32>
       cuda_tile.continue %new_value : tile<i32>
     }
     cuda_tile.return
   }
   ```

2. 自己追踪：第一次迭代 `%value = %a`，`%iv = 0`；每次 `%value` 累加 `%iv`。范围是 `[0, 5)` 共 5 次，所以最终 `%result == %a + 0 + 1 + 2 + 3 + 4`。

**需要观察的现象**：归纳变量 `%iv` 的类型 `tile<i32>` 写在 `:` 后面；`continue` 的操作数类型 `tile<i32>` 必须和 `iter_values` 声明的返回类型一致。

**预期结果**：理解 `%value` 是「本轮入口」、`continue %new_value` 是「交还出口」的对应关系。

> 待本地验证：若已构建，运行 `cuda-tile-opt`（或 `cuda-tile-translate`）加载该文件，确认无验证错误；再故意把 `continue` 改成不传值，观察 verifier 报「循环携带值个数不匹配」。

#### 4.2.5 小练习与答案

**练习 1**：如果 `step` 给负数会怎样？

**参考答案**：会变成死循环或零次循环。规范要求 `step` 必须为正（见 [Ops.td:1919](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L1919-L1919)），给负值属于违反前置条件，行为未定义；想反向迭代应改写界或用 `loop` + `break`。

**练习 2**：为什么循环携带值不能是 `tensor_view`？

**参考答案**：`tensor_view` 和各类 TileView 是描述「全局显存某块几何」的视图句柄，其合法性依赖固定几何关系；在迭代间逐次「传递、改写」会破坏这些几何不变式。`verifyLoopIterValues` 因此直接拒绝（见 [CudaTile.cpp:2443-2450](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L2443-L2450)）。

---

### 4.3 loop 循环与 break/continue：无限循环的两条退出路径

#### 4.3.1 概念说明

`loop` 是一个**非结构化的无限循环**：没有归纳变量、没有范围，循环体一遍一遍执行，**直到动态执行到 `break` 才停止**。它对应 C 语言里的 `while (true) { ... }`，需要配合 `if` + `break` 表达 `while`/`do-while` 的退出条件。

`loop` 也支持循环携带值（`iter_values`），与 `for` 类似，但有两个关键不同：

- 体块的每条控制路径必须以 `continue`（进入下一轮，可交还携带值）**或** `break`（彻底退出，可交还最终值）收尾。
- 因为可能 `break`，`loop` 的返回值类型可以和循环携带值的入口类型**不同**——`break` 交还的是「最终返回值」，`continue` 交还的是「下一轮入口值」。

`break` 和 `continue` 都有一个重要性质：**总是回到最近的外层循环**，即使中间隔了 `if`。也就是说，`if` 对 `break`/`continue` 是「透明」的。

#### 4.3.2 核心流程

`loop` 的两种典型形态（都来自官方 `mlirExamples`）：

```
// while-do：先判条件，真则继续、假则 break
loop {
  %cond = constant <i1: 1> : tile<i1>
  if %cond { continue }      // 条件为真，进下一轮
  break                       // 条件为假，退出
}

// do-while：先做事，再判条件
loop { /*...body...*/
  %cond = constant <i1: 1> : tile<i1>
  if %cond { continue }
  break
}
```

带携带值且返回类型不同的形态（入口 `i32`、出口 `f32`）：

```
%results = loop iter_values(%value0 = %init_i32) : tile<i32> -> tile<f32> {
  %cond = constant <i1: 1> : tile<i1>
  if %cond {
    %new = constant <i32: 0> : tile<i32>
    continue %new : tile<i32>     // 下一轮入口仍是 i32
  }
  %final = constant <f32: 0.0> : tile<f32>
  break %final : tile<f32>        // 退出时交还 f32，作为 loop 结果
}
```

#### 4.3.3 源码精读

`loop` 的定义：

[Ops.td:2933-2939](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L2933-L2939) —— 注意它的 `SingleBlockImplicitTerminator<"impl::LoopOpImplicitTerminatorType">`，用的是 4.5 节那个「多终止符」工具，意味着体块可以用 `continue` 或 `break` 结束，缺省时补 `continue`。它的参数只有变长的 `initValues`（没有界、没有归纳变量）。

[Ops.td:2939-2968](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L2939-L2968) 是描述文字，明确「无限循环直到 `break`」和「循环体内不允许直接 `return` 退出整个内核」两条限制。

「穿透 `if`、回到最近循环」的校验由一个共享模板 `verifyEarlyExitOp` 完成：

[CudaTile.cpp:1797-1820](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L1797-L1820) —— 这段逻辑沿着祖先链向上走：每个祖先要么是允许的循环类型（`AllowedLoopOpsT`，对 `break` 是 `LoopOp`，对 `continue` 是 `ForOp`/`LoopOp`），要么是 `IfOp`（透明放行）；只要碰到别的操作就报错「只能嵌套在 … 或 cuda_tile.if 里」。

`BreakOp::verify` 在 `verifyEarlyExitOp` 之上还多做一件事：

[CudaTile.cpp:1822-1838](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L1822-L1838) —— 找到最近外层 `LoopOp`，强制 `break` 的操作数类型必须和该 `loop` 的结果类型一一对应。这保证了「`break` 交还的值」能正确成为 `loop` 的返回值。

#### 4.3.4 代码实践

**实践目标**：用 `loop` + `if` + `break` 写一个「while」语义的循环，并观察「穿透 if」。

**操作步骤**：

1. 参考 [Ops.td:2970-2982](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L2970-L2982) 的官方 while-do 示例，写出：

   ```mlir
   cuda_tile.entry @while_like() {
     loop {
       %cond = cuda_tile.constant <i1: 1> : !cuda_tile.tile<i1>
       cuda_tile.if %cond {
         cuda_tile.continue        // 写在 if 里，穿透到外层 loop
       }
       cuda_tile.break             // 写在 loop 体直接层
     }
     cuda_tile.return
   }
   ```

2. 故意把 `break` 改成 `return`，期望被拒：`return` 不能直接用在循环体内终止内核（见 [Ops.td:2965-2967](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L2965-L2967)）。

**需要观察的现象**：`continue` 在 `if` 里但能终止 `if` 块并把控制权交回 `loop`，这就是「穿透」。

**预期结果**：合法写法通过验证；把 `break` 误改成 `return` 后，`ReturnOp::verify` 会报「must be used within a … entry, or cuda_tile.if operation」（见 [CudaTile.cpp:5051-5055](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L5051-L5055)）。

> 待本地验证：实际报错文案以本地 `cuda-tile-opt` 输出为准。

#### 4.3.5 小练习与答案

**练习 1**：`loop` 的返回值类型可以和循环携带值入口类型不同，`for` 可以吗？

**参考答案**：`for` 不可以。`for` 用 `AllTypesMatch<["initValues", "resultValues"]>` 强制入口与返回同类型（见 [Ops.td:1894](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L1894-L1894)）。`loop` 之所以能不同，是因为「退出」由独立的 `break` 表达，`break` 交还的类型就是 `loop` 结果类型，与 `continue` 交还的「下一轮入口类型」可以解耦。

**练习 2**：`break` 写在 `if` 里、`if` 又嵌在 `for` 里（不是 `loop`），合法吗？

**参考答案**：不合法。`break` 的目标只能是 `LoopOp`（见 `BreakOp::verify` 调 `verifyEarlyExitOp<LoopOp>`，[CudaTile.cpp:1823](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L1823-L1823)）。`for` 不支持提前退出，所以 `break` 不能穿透到 `for`。

---

### 4.4 if 条件分支：then/else region 与 yield 传值

#### 4.4.1 概念说明

`if` 是结构化条件分支，对应 C 语言的 `cond ? x : y` 或 `if/else`。它接受一个 `tile<i1>` 的**标量条件**（注意是 `ScalarTileOf<Int1>`，即形状为标量的 i1 tile），带一个必填的 `thenRegion` 和一个可选的 `elseRegion`。

`if` 可以「带返回值」：两个分支各自用 `yield` 交还若干值，`if` 的结果就是被选中分支 yield 的值。这要求**两个分支的 yield 类型必须一致**，且若声明了返回类型则 `else` 分支不可省略。值得强调的是 `if` 的结果类型**不能是 `tensor_view` 或视图类型**（与循环携带值同样的限制）。

#### 4.4.2 核心流程

三种形态：

```
// 1. 无返回值，仅副作用（else 可省）
if %cond { /*...*/ }

// 2. 无返回值，带 else
if %cond { /*...*/ } else { /*...*/ }

// 3. 带返回值：两分支都 yield，类型一致
%x, %y = if %cond -> (tile<f32>, tile<i32>) {
  %xt = constant <f32: 1.0> : tile<f32>
  %yt = constant <i32: 2>   : tile<i32>
  yield %xt, %yt : tile<f32>, tile<i32>
} else {
  %xe = constant <f32: 1.0> : tile<f32>
  %ye = constant <i32: 42>  : tile<i32>
  yield %xe, %ye : tile<f32>, tile<i32>
}
```

校验要点（来自 `IfOp::verify`）：① `then` 必须存在；② 若 `if` 声明了返回类型，则每个有 `yield` 的分支其 yield 类型必须与返回类型逐一致；③ 没有返回类型时分支不得 yield 值；④ 有返回类型时 `else` 必须存在。

#### 4.4.3 源码精读

`if` 的定义：

[Ops.td:2413-2416](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L2413-L2416) —— 关键 trait：
- `NoRegionArguments`：`then`/`else` region 不带入口参数（条件和 `for` 不同，不作为 block 参数传入）。
- `SingleBlockImplicitTerminator<"impl::IfOpImplicitTerminatorType">`：用 4.5 节的多终止符工具，分支块缺省补 `yield`，但也允许 `break`/`continue`/`return` 提前终止。

参数与 region 见 [Ops.td:2471-2476](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L2471-L2476)：条件是 `CudaTile_ScalarTileOf<CudaTile_Int1>`；region 是 `SizedRegion<1>`（then，恰好一块）+ `MaxSizedRegion<1>`（else，至多一块）。

返回值与 yield 类型一致性的校验：

[CudaTile.cpp:2821-2874](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L2821-L2874) —— 用一个 lambda `checkRegionYieldTypes` 对 then/else 分别检查 yield 类型与 `if` 结果类型是否一致、是否「该 yield 没 yield / 不该 yield 却 yield」；并显式拒绝 `tensor_view` 与 TileView 结果类型（[CudaTile.cpp:2849-2856](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L2849-L2856)）。若声明了返回类型却没写 `else`，会在 [CudaTile.cpp:2866-2868](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L2866-L2868) 报「must define else branch」。

可运行的标准写法在 [operationsTest.mlir:79-86](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Bytecode/operationsTest.mlir#L79-L86)：

```mlir
cuda_tile.entry @if_else_op_test(%cond: !cuda_tile.tile<i1>,
                                 %a: !cuda_tile.tile<i32>,
                                 %b: !cuda_tile.tile<i32>) {
  %result = cuda_tile.if %cond -> (!cuda_tile.tile<i32>) {
    cuda_tile.yield %a : !cuda_tile.tile<i32>
  } else {
    cuda_tile.yield %b : !cuda_tile.tile<i32>
  }
  cuda_tile.return
}
```

#### 4.4.4 代码实践

**实践目标**：写一个带返回值的 `if`，让两分支 yield 不同来源的值。

**操作步骤**：

1. 在一个 `cuda_tile.entry` 里，声明条件 `%cond: tile<i1>` 和两个候选 `%a`、`%b: tile<i32>`。
2. 写 `if %cond -> (tile<i32>) { yield %a } else { yield %b }`，结构对齐上面的 `@if_else_op_test`。
3. 故意把 `else` 分支删掉但保留 `-> (tile<i32>)` 返回声明，期望报错。

**需要观察的现象**：返回类型声明 `-> (tile<i32>)` 决定了 `if` 结果 `%result` 的类型；两分支 yield 的类型必须逐一致。

**预期结果**：合法写法 `%result` 类型为 `tile<i32>`；删除 `else` 后 `IfOp::verify` 报「has non-empty return type, must define else branch」。

> 待本地验证：用 `cuda-tile-opt` 加载自写的 `.mlir`，确认上述两类输入的接受/拒绝行为。

#### 4.4.5 小练习与答案

**练习 1**：`if` 的条件为什么必须是 `tile<i1>` 而不是普通 `i1`？

**参考答案**：CUDA Tile IR 把所有数据都装进 tile（见 [u3-l1](u3-l1-tile-and-element-types.md)），连「标量条件」也是一个形状为标量的 i1 tile（`ScalarTileOf<Int1>`）。这保持了「一切皆 tile」的一致性，也让条件在需要时能随 tile 维度并行（尽管 `if` 这里要求标量形状）。

**练习 2**：`if` 的结果可以是 `tensor_view` 吗？为什么？

**参考答案**：不可以。`IfOp::verify` 显式拒绝 `tensor_view` 和 TileView 作为结果类型（[CudaTile.cpp:2849-2856](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L2849-L2856)），原因是视图是全局显存的几何句柄，从一个分支「产生」并跨分支传递会破坏几何一致性，与循环携带值的限制同源。

---

### 4.5 ControlFlowImplicitTerminatorOpType：多终止符隐式注入工具

#### 4.5.1 概念说明

本节是本讲的「工程彩蛋」，也是理解前面四个操作为何如此灵活的关键。

MLIR 自带 `SingleBlockImplicitTerminator<某Op>` 机制：如果一个单块 region 的块末尾没有终止符，自动补一个 `某Op`。但 CUDA Tile 的需求更强——**一个块可能合法地以多种终止符中的任意一种结束**。例如 `if` 的 `then` 块，正常情况以 `yield` 结束，但如果分支想「提前跳出外层循环」，也可以以 `break`/`continue` 结束；如果想「提前返回整个内核」，可以以 `return` 结束。这些情况下不该再强行补一个 `yield`。

`ControlFlowImplicitTerminatorOpType` 就是为此设计的「多合一」包装：它告诉 MLIR「这些终止符都算合法；但如果一个都没有，就补第一个（隐式默认）那个」。

#### 4.5.2 核心流程

该模板接受一个「隐式默认操作」和一串「其它合法终止符」：

```
ControlFlowImplicitTerminatorOpType<ImplicitOpT, OtherTerminatorOpTs...>
```

它提供三样东西，恰好满足 MLIR 的隐式终止符协议：

- `classof(op)`：判断 `op` 是不是这些终止符中的任意一个（用 `isa<ImplicitOpT, OtherTerminatorOpTs...>(op)`）。
- `build(...)`：构造隐式默认操作（委托给 `ImplicitOpT::build`）。
- `getOperationName()`：返回隐式默认操作的名字（`ImplicitOpT::getOperationName()`）。

CUDA Tile 据此定义了两个别名：

```
IfOpImplicitTerminatorType   = <YieldOp, BreakOp, ContinueOp, ReturnOp>
LoopOpImplicitTerminatorType = <ContinueOp, BreakOp>
```

含义：
- `if` 的分支块：合法终止符是 `yield`/`break`/`continue`/`return`；缺省补 `yield`。
- `loop` 的体块：合法终止符是 `continue`/`break`；缺省补 `continue`。
- `for` 的体块：直接用普通 `SingleBlockImplicitTerminator<"ContinueOp">`（只认 `continue`，缺省补 `continue`）。

#### 4.5.3 源码精读

模板本体很短，但设计很巧：

[Ops.h:74-95](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.h#L74-L95) —— 注释明确说明它「兼容 `SingleBlockImplicitTerminator` 的接口，但允许除单一终止符外的多种潜在终止符；若不存在终止符，则生成 `ImplicitOpT`」。`classof` 用变参 `isa` 同时匹配所有终止符类型；`build` 与 `getOperationName` 都转发给 `ImplicitOpT`，即列表里的第一个。

两个具体别名：

[Ops.h:96-102](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.h#L96-L102) —— `IfOpImplicitTerminatorType` 以 `YieldOp` 为隐式默认，`LoopOpImplicitTerminatorType` 以 `ContinueOp` 为隐式默认。

它们在 TableGen 里被引用：

- `if`：[Ops.td:2416](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L2416-L2416) 用 `impl::IfOpImplicitTerminatorType`。
- `loop`：[Ops.td:2937](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.td#L2937-L2937) 用 `impl::LoopOpImplicitTerminatorType`。

效果联动：正因为 `if` 分支块的隐式终止符集合含 `break`/`continue`/`return`，4.3 节里「`continue` 写在 `if` 里穿透到 `loop`」「`break` 写在 `if` 里穿透到 `loop`」才在语法层合法；再由 `verifyEarlyExitOp` 在语义层收紧到「最终必须落在真正的循环 / 函数里」。这是一个「语法宽松 + 语义严格」的双层设计。

#### 4.5.4 代码实践

**实践目标**：通过「省略终止符」观察隐式补全，从而验证工具的实际效果。

**操作步骤**：

1. 写一个最简 `loop`，体里只放一句无关操作、**不写** `continue` 或 `break`：

   ```mlir
   cuda_tile.entry @implicit_continue() {
     cuda_tile.loop {
       // 故意什么都不写
     }
     cuda_tile.return
   }
   ```

2. 写一个最简 `if`，`then` 块**不写** `yield`：

   ```mlir
   cuda_tile.entry @implicit_yield(%cond: !cuda_tile.tile<i1>) {
     cuda_tile.if %cond {
       // 故意不写 yield
     }
     cuda_tile.return
   }
   ```

3. 用 `cuda-tile-opt`（或 `cuda-tile-translate`）加载并 round-trip 打印，观察自动补出的 `continue` / `yield`。

**需要观察的现象**：经过解析→打印，`loop` 体里应出现一个隐式 `continue`；`if` 的 `then` 块里应出现一个隐式 `yield`。这说明缺省终止符分别是 `ContinueOp` 和 `YieldOp`，与 [Ops.h:98-102](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.h#L98-L102) 一致。

**预期结果**：两个用例都能被接受，且 round-trip 后显式出现隐式终止符。

> 待本地验证：实际 round-trip 输出以本地工具为准；若解析器对完全空块有额外要求，可在块内放一个无副作用操作（如 `%x = constant <i1: 1> : tile<i1>`）再观察。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `if` 的隐式默认是 `YieldOp` 而不是 `BreakOp`？

**参考答案**：因为 `if` 最常见的用法是「正常结束分支、把控制权交还给 `if`」，对应的就是 `yield`。`break`/`continue`/`return` 是「提前跳出」的特殊语义，必须显式写。把最常见、最安全的 `yield` 设为缺省，能让简单分支写法更简洁。

**练习 2**：`ControlFlowImplicitTerminatorOpType` 与 MLIR 原生 `SingleBlockImplicitTerminator` 有何关系？

**参考答案**：它是后者接口的「多终止符增强版」。原生机制只认单一终止符类型；本模板用变参模板把 `classof` 扩展为「匹配多种终止符」，但 `build`/`getOperationName` 仍只对应隐式默认的那一个，从而在保持协议兼容的前提下，让一个块接受多种合法终止符（见 [Ops.h:74-95](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/IR/Ops.h#L74-L95)）。

---

## 5. 综合实践

把本讲四个核心操作（`for` / `if` / `yield` / `continue`）串成一个完整可验证的内核。任务：写一个带循环携带值的 `for` 循环，循环体内用一个 `if`，依据归纳变量是否小于某个阈值，让 `if` 的两个分支 yield 不同的值，再用 `continue` 把结果交还，进入下一轮。

**参考写法**（基于 `operationsTest.mlir` 的 `@for_op` 与 `@if_else_op_test` 改写，标注为示例代码）：

```mlir
// 示例代码：综合 for + if + yield + continue
cuda_tile.module @kernels {
  cuda_tile.entry @accumulate_with_branch(%init: !cuda_tile.tile<i32>) {
    %lb   = cuda_tile.constant <i32: 0> : !cuda_tile.tile<i32>
    %ub   = cuda_tile.constant <i32: 10> : !cuda_tile.tile<i32>
    %step = cuda_tile.constant <i32: 1> : !cuda_tile.tile<i32>
    %k    = cuda_tile.constant <i32: 5> : !cuda_tile.tile<i32>   // 阈值

    %result = cuda_tile.for %iv in (%lb to %ub, step %step) : tile<i32>
              iter_values(%acc = %init) -> (tile<i32>) {
      // 比较 %iv < %k，得到标量 i1 条件（cmpi 见 u4-l2）
      %cond = cuda_tile.cmpi slt %iv, %k : tile<i32> -> tile<i1>
      // 按条件选增量：%iv<5 时加 2，否则加 1
      %chosen = cuda_tile.if %cond -> (!cuda_tile.tile<i32>) {
        %two = cuda_tile.constant <i32: 2> : !cuda_tile.tile<i32>
        cuda_tile.yield %two : !cuda_tile.tile<i32>
      } else {
        %one = cuda_tile.constant <i32: 1> : !cuda_tile.tile<i32>
        cuda_tile.yield %one : !cuda_tile.tile<i32>
      }
      %new = cuda_tile.addi %acc, %chosen : tile<i32>
      cuda_tile.continue %new : tile<i32>
    }
    cuda_tile.return
  }
}
```

**操作步骤**：

1. 把上述内容存为 `cf_demo.mlir`。
2. 用 `cuda-tile-opt`（或 `cuda-tile-translate`）加载，确认通过验证（region 结构、终止符、yield/continue 类型一致性）。
3. 对照本讲源码，逐行解释：`%iv` 是归纳变量（block 参数 0）；`%acc` 是循环携带值的本轮入口（block 参数 1）；`%chosen` 是 `if` 的返回值，由两分支 `yield` 提供；`continue %new` 把新值交还，成为下一轮 `%acc`。
4. 故意制造三类错误各跑一次，核对报错来源：
   - 删掉 `if` 的 `else` 分支（保留 `-> (...)`）→ 应被 `IfOp::verify` 拒（[CudaTile.cpp:2866-2868](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L2866-L2868)）。
   - 把 `continue %new` 改成不传值 → 应被循环携带值个数校验拒。
   - 在 `if` 分支里加一个 `break` → 应被 `verifyEarlyExitOp` 拒，因为外层是 `for` 不是 `loop`（[CudaTile.cpp:1797-1820](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L1797-L1820)）。

**预期结果**：合法版本通过验证；三个错误版本分别被对应 verifier 拒绝，报错指向本讲引用的源码位置。

> 待本地验证：`cmpi slt` 的确切拼写、错误文案以本地构建的 `cuda-tile-opt` 输出为准（`cmpi` 的谓词写法见 [u4-l2](u4-l2-integer-arith.md)）。

## 6. 本讲小结

- Control Flow 组共 8 个操作，按角色分三类：容器（`for`/`loop`/`if`）、终止符（`yield`/`continue`/`break`/`return`）、诊断（`assert`）；核心设计是「父操作决定子终止符语义」，`ParentOneOf` 在语法层约束谁能装在谁里。
- `for` 是范围循环，归纳变量从 `[lb, ub)` 按 `step` 步进，体块以 `continue` 收尾；支持 `iter_values` 循环携带值，入口与返回同类型；`for` 不能提前退出。
- `loop` 是无限循环，靠 `break` 退出、`continue` 进入下一轮，二者都可带携带值；因为 `break` 独立交还最终值，`loop` 的返回类型可与入口类型不同。
- `if` 接受标量 `tile<i1>` 条件，带必填 `then` 和可选 `else`；带返回值时两分支 `yield` 类型必须一致、`else` 不可省；结果不能是视图类型。
- `ControlFlowImplicitTerminatorOpType` 是 MLIR 隐式终止符机制的「多终止符增强版」：`if` 块认 `yield`/`break`/`continue`/`return`、缺省补 `yield`；`loop` 块认 `continue`/`break`、缺省补 `continue`。
- 「穿透 `if` 回到最近循环」由 `verifyEarlyExitOp` 在语义层收紧，形成「语法宽松 + 语义严格」的双层校验。

## 7. 下一步学习建议

- **优化器视角**：本讲的 `for` 与 `if` 是 [u9-l1 Pass 框架与 FuseFMA 融合](u9-l1-passes-and-fusefma.md) 和 [u9-l2 LoopSplit 循环分裂](u9-l2-loop-split.md) 的操作对象。读完本讲后，建议接着看 `LoopSplit` 如何识别「`if` 条件比较归纳变量与循环不变量」的模式（正是 4.4 + 4.2 的组合），把循环按谓词分裂成两段。
- **归约/扫描的 yield**：`yield` 还服务于 `reduce`/`scan`（见 [u4-l6 归约、扫描与低精度打包](u4-l6-reduce-scan-pack.md)），可对比「`yield` 在 `if` 里交还分支结果」与「`yield` 在 `reduce` 里交还一次运算结果」的语义差异。
- **继续阅读源码**：想加深对终止符穿透的理解，可精读 [CudaTile.cpp:1844-1860](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L1844-L1860) 的 `ContinueOp::verify`（与 `BreakOp::verify` 对偶），以及 [CudaTile.cpp:5007-5059](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/IR/CudaTile.cpp#L5007-L5059) 的 `ReturnOp::verify`，看它们如何各自实现「向上找对父操作」。
