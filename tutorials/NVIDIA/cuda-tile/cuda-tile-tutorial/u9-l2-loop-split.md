# LoopSplit 循环分裂

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚「循环分裂（loop splitting）」这种优化在 GPU 内核里解决什么问题，以及它为什么能带来收益。
- 读懂 `LoopSplit.cpp` 中的判定总流程 `isSplittableCondition`，并能描述它依次检查哪些条件。
- 精读 `normalizeForOpCmp`：理解它如何把任意方向的比较统一规范化成「归纳变量在左、循环不变量在右」的形式，并校验符号一致性。
- 精读 `isSplitProfitable`：理解阈值启发式（默认 1）与「重操作」判定如何共同决定要不要分裂。
- 掌握 `performLoopSplit` 如何计算分裂点、处理非 1 步长的对齐、并克隆出两段循环。
- 理解 `--split-threshold` 命令行选项与 `cuda_tile.loop_split` 属性的优先级交互，并能对照 `loop_split.mlir` 解释分裂前后的 IR 差异。

## 2. 前置知识

本讲是「专家·优化器与变换 Pass」单元的第二篇，默认你已经学过：

- **u5-l4 控制流**：`for` 是带归纳变量 `iv`、左闭右开区间 `[lb, ub)`、正步长 `step` 的结构化范围循环，支持 `iter_values` 循环携带值，循环体以 `continue` 收尾；`if` 接受 `tile<i1>` 条件，分支用 `yield` 交还值。本讲大量出现 `ForOp`/`IfOp`/`CmpIOp`/`YieldOp`/`ContinueOp`，这些都来自那一讲。
- **u4-l2 整数算术与比较**：`cmpi` 用六种谓词（`equal`/`not_equal`/`less_than`/`less_than_or_equal`/`greater_than`/`greater_than_or_equal`）逐元素比较，**必须带 `signed`/`unsigned` 符号性**，输出同形状的 `tile<i1>`。符号性是本讲判定的关键之一。
- **u9-l1 Pass 框架与 FuseFMA**：MLIR 的 Pass 通过 `Passes.td` 经 TableGen 声明，由 `Passes.h` 的 `GEN_PASS_REGISTRATION` 宏生成 `registerCudaTilePasses`，再被 `cuda-tile-opt` 调起；`InterfacePass` 会绑定到某个 OpInterface（如 `mlir::FunctionOpInterface`）而非具体 Op。`LoopSplitPass` 正是一个 `InterfacePass`。

补充两个本讲会用到的 MLIR 概念：

- **归纳变量（induction variable, iv）**：`for` 循环每次迭代取值的变量，即 `for %arg1 in (...)` 里的 `%arg1`。
- **循环不变量（loop invariant）**：在循环整个执行过程中值都不变的量，比如循环外定义的常量 `%3 = constant <i64: 32>`。把「iv 与不变量」比较，是循环分裂能识别的核心模式。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [lib/Dialect/CudaTile/Transforms/LoopSplit.cpp](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/LoopSplit.cpp) | Pass 的全部实现：判定、收益评估、分裂点计算、循环克隆、属性读取。 |
| [include/cuda_tile/Dialect/CudaTile/Transforms/Passes.td](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/Transforms/Passes.td) | `LoopSplitPass` 的 TableGen 声明，含前后对照示例与 `--split-threshold` 选项定义。 |
| [include/cuda_tile/Dialect/CudaTile/Transforms/Passes.h](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/Transforms/Passes.h) | `kLoopSplitThresholdAttrName`（即 `cuda_tile.loop_split`）属性名常量与默认优化选项。 |
| [test/Transforms/loop_split.mlir](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Transforms/loop_split.mlir) | 用 FileCheck 钉死 Pass 行为的权威测试，覆盖了所有支持/不支持/属性交互场景。 |

## 4. 核心概念与源码讲解

### 4.1 循环分裂的动机与整体判定流程

#### 4.1.1 概念说明

考虑这样一类很常见的内核：一个 `for` 循环里有一个 `if`，而 `if` 的条件是「归纳变量 `iv` 与某个循环不变量 `v` 的比较」，例如 `if (iv < 32)`。在循环的整个区间里，这个条件只在某一次「翻转点」前后发生改变：当 `iv` 还小于 32 时走 then 分支，之后就走 else 分支。

每个迭代都要算一次比较、再做一次分支跳转，对 GPU 这种「靠海量线程掩盖延迟、但对分支发散（divergent branch）很敏感」的硬件来说并不划算。**循环分裂（loop splitting）** 的思路是：与其在每个迭代里反复判断，不如把原循环沿翻转点切成**两段**——第一段里条件恒为真、第二段里条件恒为假——于是每段循环体内的 `if` 都可以彻底删除，分支发散也随之消失。

Pass 文档给出了一份极具代表性的前后对照，先建立直观印象：

```
Before:
  %4 = for %arg1 in (%1 to %0, step %2) : tile<i32> iter_values(%7 = %1) -> (tile<i32>) {
    %5 = cmpi greater_than %arg1, %3, signed : tile<i32>
    %6 = if %5 -> (tile<i32>) {
      %9 = muli %arg1, %0 : tile<i32>
      yield %9 : tile<i32>
    } else {
      yield %arg1 : tile<i32>
    }
    ...
  }

After:
  %for   = for %loopIdx in (%cst_0_i32 to %0,  step %cst_1_i32) ... { /* 只剩 else 分支的体 */ }
  %for_0 = for %loopIdx in (%0 to %cst_128_i32, step %cst_1_i32) ... { /* 只剩 then 分支的体 */ }
```

注意「分裂点」就是 `%3`（不变量）所在的位置：第一段循环的上界收到分裂点，第二段循环的下界从分裂点开始，两段合起来等价于原循环。

#### 4.1.2 核心流程

整个 Pass 的调度入口是 `LoopSplitPass::runOnOperation`，它对每个 `ForOp` 内部的每个 `IfOp` 调用 `isSplittableCondition` 做判定；一旦找到一个可分裂且值得分裂的 `IfOp`，就立刻 `performLoopSplit` 并 `interrupt()` 中止本轮遍历。判定函数 `isSplittableCondition` 按顺序检查五道关：

1. **阈值关**：`threshold == 0` 直接返回 false（属性把分裂关掉了）。
2. **比较关**：`if` 的条件必须由 `CmpIOp` 定义，否则返回 false。
3. **规范化关**：调用 `normalizeForOpCmp`，要求比较的一侧是本循环的归纳变量，且符号性与循环一致（详见 4.2）。
4. **不变量关**：另一侧（rhs）必须是循环不变量——它的定义操作不能位于循环体内。
5. **谓词关 + 收益关**：规范化后的谓词必须是四种不等式之一；并要求至少有一个共享该比较的 `IfOp` 满足 `isSplitProfitable`（详见 4.3）。

全部通过后才把 `cmp`/规范化谓词/rhs 交给 `performLoopSplit` 执行真正的分裂。

#### 4.1.3 源码精读

调度与遍历入口在 [lib/Dialect/CudaTile/Transforms/LoopSplit.cpp:358-387](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/LoopSplit.cpp#L358-L387)：先读 entry 级别的 `cuda_tile.loop_split` 属性作为 `entryHint`，再用嵌套 `walk` 遍历每个 `ForOp`→`IfOp`，对每个 `IfOp` 算出当前生效阈值（4.4 节展开）后调用 `isSplittableCondition`。

判定总函数 `isSplittableCondition` 在 [lib/Dialect/CudaTile/Transforms/LoopSplit.cpp:108-181](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/LoopSplit.cpp#L108-L181)，其中前两关——「阈值非 0」与「条件是 CmpIOp」——见 [LoopSplit.cpp:113-120](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/LoopSplit.cpp#L113-L120)。这段说明：只要属性把阈值设为 0，或条件根本不是 `cmpi`（比如直接用一个 `tile<i1>` 值），就立刻判定不可分裂。

不变量关在 [LoopSplit.cpp:131-133](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/LoopSplit.cpp#L131-L133)：

```cpp
auto rhsOp = rhs.getDefiningOp();
if (rhsOp && forOp.getBody()->findAncestorOpInBlock(*rhsOp))
  return false;
```

它用 `findAncestorOpInBlock` 检查 rhs 的定义操作是否「住」在循环体里——若是，说明 rhs 随迭代变化，不是不变量，分裂不成立。测试里的 `unsupported_cmp_non_inv` 用例（`cmpi greater_than %arg1, %70`，其中 `%70 = addi %arg1, %arg1`）正是被这一关挡掉的。

#### 4.1.4 代码实践

**目标**：通过阅读测试，确认「rhs 非不变量」这一关确实会阻止分裂。

**步骤**：

1. 打开 [test/Transforms/loop_split.mlir:49-92](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Transforms/loop_split.mlir#L49-L92)（`unsupported_cmp_non_inv` 用例）。
2. 注意其中 `cmpi greater_than %arg1, %70` 的 rhs 是 `%70 = addi %arg1, %arg1`，它定义在循环体内。
3. 对照同文件 [loop_split.mlir:61-62](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Transforms/loop_split.mlir#L61-L62) 的 `CHECK`/`CHECK-NOT`：期望只出现一个 `for`、不出现第二个 `for`，即没有发生分裂。

**观察现象**：FileCheck 的 `CHECK-NOT: {{.*}} = for {{.*}}` 断言分裂后没有多出第二个循环。

**预期结果**：因为 rhs 依赖归纳变量，循环不被分裂，IR 保持原样。**待本地验证**：在你的构建里执行 `cmake --build build --target check-cuda-tile`，确认该用例通过。

#### 4.1.5 小练习与答案

**练习 1**：循环分裂和「循环展开（unroll）」是同一种优化吗？为什么 CUDA Tile 选择实现分裂而不是展开？

> **答案**：不是。展开把循环体复制多份以减少分支开销，但会让代码体积暴涨；分裂不减少迭代次数，只是沿翻转点把一个循环切成两段，消除每迭代一次的 `if` 分支判断。对张量核内核里常见的「分段处理（如前 N 个元素走快路径、其余走通用路径）」模式，分裂能在不爆代码体积的前提下消除分支发散。

**练习 2**：如果 `if` 的条件是一个直接传入的 `tile<i1>` 值（而非 `cmpi` 的结果），`isSplittableCondition` 会怎么处理？

> **答案**：在 [LoopSplit.cpp:118-120](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/LoopSplit.cpp#L118-L120)，`ifOp.getCondition().getDefiningOp<CmpIOp>()` 返回空，函数直接返回 false，不分裂。因为没有了「iv 与不变量的比较」，就找不到翻转点。

### 4.2 判定核心一：normalizeForOpCmp —— 比较规范化与符号一致性

#### 4.2.1 概念说明

即便 `if` 的条件是 `cmpi`，它的写法也可能五花八门：归纳变量既可能在左（`iv < 32`），也可能在右（`32 > iv`）。为了让后面的逻辑只处理一种统一形态，需要一个规范化步骤，把所有合法比较统一成 **「iv <op> value」**（归纳变量恒在左边）。同时，由于 `cmpi` 的符号性会影响比较结果（有符号 vs 无符号），还必须确认这次比较的符号性与所在 `for` 循环声明的符号性一致——否则分裂后的边界计算会出错。

这里出现一个关键事实：`for` 操作本身带一个 `unsignedCmp` 属性，它决定了「这个循环在语义上按有符号还是无符号解释整数比较」。这是循环级别的整体约定。

#### 4.2.2 核心流程

`normalizeForOpCmp` 的处理流程：

1. 由 `forOp.getUnsignedCmp()` 推出循环的符号性 `forOpSignedness`。
2. 若 `cmp` 的符号性 `cmp.getSignedness()` 与之不等，返回 false。
3. 若 `cmp` 的左操作数恰是归纳变量 `iv`：谓词保持不变，rhs 取右操作数。
4. 若 `cmp` 的右操作数是 `iv`（即写成了 `value op iv`）：把 rhs 取为左操作数，并把谓词**翻转**为等价的「iv 在左」形式（如 `LESS_THAN` ↔ `GREATER_THAN`）。
5. 若两侧都不是 `iv`，返回 false。
6. `equal`/`not_equal` 谓词在翻转分支的 `default` 里返回 false——它们没有方向但也不能用来找翻转点。

谓词翻转的对应关系（交换左右操作数时的等价变换）：

| 原谓词（value op iv） | 翻转后（iv op' value） |
| --- | --- |
| `LESS_THAN` | `GREATER_THAN` |
| `LESS_THAN_OR_EQUAL` | `GREATER_THAN_OR_EQUAL` |
| `GREATER_THAN` | `LESS_THAN` |
| `GREATER_THAN_OR_EQUAL` | `LESS_THAN_OR_EQUAL` |
| `equal` / `not_equal` | 不支持（返回 false） |

#### 4.2.3 源码精读

符号一致性检查见 [lib/Dialect/CudaTile/Transforms/LoopSplit.cpp:36-44](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/LoopSplit.cpp#L36-L44)：

```cpp
Signedness forOpSignedness =
    forOp.getUnsignedCmp() ? Signedness::Unsigned : Signedness::Signed;
if (cmp.getSignedness() != forOpSignedness)
  return false;
```

这两行是「符号关」。测试 `split_unsigned`（[loop_split.mlir:822-864](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Transforms/loop_split.mlir#L822-L864)）就因为 `cmpi less_than %arg1, %3, unsigned` 与循环默认的有符号约定不一致而被挡下。

「iv 在左」的直通分支在 [LoopSplit.cpp:48-51](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/LoopSplit.cpp#L48-L51)；「iv 在右」需要翻转谓词，逻辑在 [LoopSplit.cpp:52-71](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/LoopSplit.cpp#L52-L71)，其 `switch` 的 `default` 分支处理 `equal`/`not_equal`，返回 false。

#### 4.2.4 代码实践

**目标**：亲手验证「iv 在右」的写法也能被分裂，且谓词会被正确翻转。

**步骤**：

1. 看 [test/Transforms/loop_split.mlir:266-289](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Transforms/loop_split.mlir#L266-L289) 的 `split_continue` 用例，注意条件是 `cmpi less_than_or_equal %3, %arg1`——不变量 `%3` 在左、iv `%arg1` 在右。
2. 因为规范化后变成 `iv >= %3`（即 `GREATER_THAN_OR_EQUAL`），它属于「第二段走 then」的情形，故 `secondThen = true`。

**观察现象**：分裂后第一段循环走 else 分支体、第二段走 then 分支体。

**预期结果**：FileCheck 期望出现两段 `for`，且 `CHECK-NOT: if` 说明分支被消除。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么必须校验 `cmp` 的符号性与 `for` 的 `unsignedCmp` 一致？

> **答案**：分裂点的边界计算（`mini`/`maxi`/`divi` 等）会沿用循环的符号性（见 4.3 节 `performLoopSplit` 里反复传入的 `forOpSignedness`）。如果比较本身用了另一种符号性，那么「翻转点」在有符号/无符号下的语义不同，直接分裂会产生与原循环不等价的结果，所以必须拒绝。

**练习 2**：把 `cmpi equal %arg1, %3` 作为 `if` 条件，能触发分裂吗？

> **答案**：不能。即便 `normalizeForOpCmp` 能识别 iv，但等值比较没有「单调翻转」语义——条件可能在区间内多次为真为假，不存在单一翻转点。代码里 `equal`/`not_equal` 落入 `switch` 的 `default` 返回 false。

### 4.3 判定核心二：isSplitProfitable —— 阈值启发式与重操作判定

#### 4.3.1 概念说明

即便一个 `if` 满足可分裂的全部语法条件，也未必值得分裂——因为分裂本身有代价：要克隆整个循环体两次。如果 `if` 分支里只有一两条 trivial 指令，分裂带来的「消除分支」收益可能抵不上代码膨胀。所以需要一个**收益启发式** `isSplitProfitable` 来回答「这一处分裂划不划算」。

它综合两类信号：
- **操作数量**：then/else 分支体里的操作数是否达到阈值 `threshold`。
- **重操作**：分支体里是否含有「昂贵」的操作（访存、MMA、归约、嵌套 `if`/`for`）——只要有任何一个，就值得分裂。

默认阈值是 1，含义是「只要分支非空就分裂」，此时可短路返回。

#### 4.3.2 核心流程

`isSplitProfitable(forOp, ifOp, threshold)` 流程：

1. `threshold == 1` → 直接返回 true（短路，不再数操作）。
2. 用 `countOps` 统计 then 区块（以及可选的 else 区块）的操作数与「是否含重操作」。
3. 操作数计数 `opCount - 1`：减 1 是为了排除终止符（`yield`/`continue`）。
4. 只要满足「then 操作数 ≥ 阈值」**或**「else 操作数 ≥ 阈值」**或**「任一分支含重操作」之一，就返回 true。

被认定为「重操作」的类型集合是固定的九种：

| 类别 | 操作 |
| --- | --- |
| 访存 | `LoadPtrTkoOp`, `LoadViewTkoOp`, `StorePtrTkoOp`, `StoreViewTkoOp` |
| 张量核 | `MmaFOp`, `MmaIOp` |
| 跨元素 | `ReduceOp` |
| 嵌套结构 | `IfOp`, `ForOp` |

这套判定的直觉是：访存与张量核指令是 GPU 上延迟最高、最怕分支发散的；嵌套控制流意味着分支体本身很重。只要有它们，消除分支的收益几乎总是大于克隆代价。

#### 4.3.3 源码精读

短路逻辑在 [lib/Dialect/CudaTile/Transforms/LoopSplit.cpp:77-81](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/LoopSplit.cpp#L77-L81)：

```cpp
if (threshold == 1)
  return true;
```

`countOps` 与重操作集合在 [LoopSplit.cpp:82-92](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/LoopSplit.cpp#L82-L92)，其中 `hasHeavyOps |= isa<LoadPtrTkoOp, ...>` 一行就是重操作清单。最终的三选一判定见 [LoopSplit.cpp:101-103](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/LoopSplit.cpp#L101-L103)。

注意 `isSplitProfitable` 在 `isSplittableCondition` 里是按「同一个 `cmp` 结果的所有 `IfOp` 用户」聚合调用的——见 [LoopSplit.cpp:152-171](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/LoopSplit.cpp#L152-L171)：只要其中**任何一个** `IfOp` 收益达标（`isProfitable |= ...`），整个比较就被认定为值得分裂；同时把「直接嵌套在该 `for` 下」的 `IfOp` 收集进 `ifOps`（它们会被部分克隆，详见 4.4），并据此设置 `copyCmp`（比较是否还被别的用途使用、需要保留）。

#### 4.3.4 代码实践

**目标**：观察阈值如何影响分裂是否发生。

**步骤**：

1. 取 `split_sge` 用例 [test/Transforms/loop_split.mlir:97-143](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Transforms/loop_split.mlir#L97-L143) 的循环体作为输入（其中 then/else 含 `load_ptr_tko`，属于重操作）。
2. 用默认阈值（`--split-threshold=1`，或不传）跑一次，确认发生分裂。
3. 把同一输入用 `--split-threshold=10` 再跑一次。

**观察现象**：阈值 1 时短路直接分裂；阈值 10 时，由于分支含 `load_ptr_tko`（重操作），`hasHeavyOps` 为真，仍然分裂——说明重操作会「无视阈值」地触发分裂。

**预期结果**：两种阈值下都发生分裂。若你构造一个**只含纯算术、无重操作、且操作数很少**的分支，并把阈值调高，则应观察到不分裂。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `countOps` 返回 `opCount - 1` 而不是 `opCount`？

> **答案**：每个 region 的最后一条是终止符（`if` 的 `yield`、`for` 的 `continue`），它本身不是「有意义的工作」，统计分支「有多重」时应排除它，所以减 1。

**练习 2**：如果一个 `if` 的 then 分支只有一条 `muli`、else 分支也只有一条 `muli`，阈值设为 5，会分裂吗？

> **答案**：会。虽然操作数（1）远小于阈值 5，但判定是「操作数 ≥ 阈值 **或** 含重操作」的析取。不过 `muli` 不在重操作清单里，所以这种情况下其实**不会**分裂——除非把其中一条换成 `load_ptr_tko` 等重操作。关键在于理解「重操作」是独立于操作数计数的另一条触发路径。

### 4.4 执行分裂：performLoopSplit 与 LoopSplitPass 选项

#### 4.4.1 概念说明

判定通过后，`performLoopSplit` 负责真正的重写：计算分裂点、把它对齐到合法的循环边界、生成上下两段循环、并复制原循环体的内容（按段决定走 then 还是 else）。同时，分裂的「阈值」并不是只来自命令行 `--split-threshold`：源码还支持在 `entry`/`for`/`if` 三层操作上挂 `cuda_tile.loop_split` 属性来精确控制，越局部的属性优先级越高。这一节把「执行机制」和「选项/属性交互」一起讲，并用 `loop_split.mlir` 做前后对照。

#### 4.4.2 核心流程

**分裂点计算**（`performLoopSplit`）：

1. 取 `splitValue = rhs`（比较里的不变量）。
2. 对 `GREATER_THAN` 与 `LESS_THAN_OR_EQUAL` 谓词，分裂点要 `+1`（因为「严格大于 v」的翻转点在 `v+1`）。对此 `LESS_THAN` 与 `GREATER_THAN_OR_EQUAL` 不加。
3. 若步长不是常量 1，还要把分裂点**对齐**到 `lb + k*step` 网格上，公式为：

   \[
   \text{splitPoint} = \text{lb} + \left\lceil \frac{\text{splitPoint} - \text{lb}}{\text{step}} \right\rceil \times \text{step}
   \]

   其中除法用 `divi ... rounding<positive_inf>` 实现向上取整。
4. 用 `mini`/`maxi` 把分裂点夹到 `[lb, ub]` 区间内，得到 `minSplitPoint`（第一段上界）与 `maxSplitPoint`（第二段下界），防止翻转点落在循环区间之外时产生空循环或越界。

**两段克隆**（`copyLoop`）：

1. 用 `IRMapping` 建立原循环体参数到新循环体参数的映射。
2. 遍历要克隆的操作：普通操作直接 `clone`；要分裂的 `IfOp` 只克隆其中一个 region（第一段克隆条件恒假的一侧、第二段克隆条件恒真的一侧）；`CmpIOp` 则替换成常量 `true`/`false`（因为分裂后条件已经确定）。
3. 处理 `if` 内的 `continue`/`yield`：遇到 `continue` 就复制并停止克隆（因为 continue 会结束本轮循环体）。
4. 第一段循环的 `iter_args` 用原循环的 `init_values`，第二段用第一段的结果，从而把携带值串起来。

**阈值来源与优先级**（`getSplitThreshold`）：阈值按 `if` 属性 → `for` 属性 → `entry` 属性 → 命令行选项 的顺序取第一个存在的值。属性值为 0 表示「禁用」。

#### 4.4.3 源码精读

分裂点的 `+1` 调整与非 1 步长对齐见 [lib/Dialect/CudaTile/Transforms/LoopSplit.cpp:278-297](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/LoopSplit.cpp#L278-L297)。其中 [LoopSplit.cpp:292-296](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/LoopSplit.cpp#L292-L296) 正是上面公式的四条指令 `subi`/`divi`/`muli`/`addi`。`mini`/`maxi` 夹逼在 [LoopSplit.cpp:299-302](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/LoopSplit.cpp#L299-L302)。

两段循环的克隆与衔接见 [LoopSplit.cpp:314-322](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/LoopSplit.cpp#L314-L322)：第一段用 `lb`→`minSplitPoint`、克隆 `!secondThen` 一侧；第二段用 `maxSplitPoint`→`ub`、克隆 `secondThen` 一侧、`iter_args` 取 `firstLoop.getResults()`；最后 `replaceOp` 用第二段替换原循环。

`copyLoop` 把 `CmpIOp` 替换成常量见 [LoopSplit.cpp:202-210](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/LoopSplit.cpp#L202-L210)，只克隆单个 region 的逻辑见 [LoopSplit.cpp:216-237](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/LoopSplit.cpp#L216-L237)。

阈值优先级函数 `getSplitThreshold` 见 [LoopSplit.cpp:330-340](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/LoopSplit.cpp#L330-L340)；属性读取 `getLoopSplitThresholdAttr`（用 `getDiscardableAttr`）见 [LoopSplit.cpp:342-349](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/LoopSplit.cpp#L342-L349)。属性名常量 `cuda_tile.loop_split` 定义在 [include/cuda_tile/Dialect/CudaTile/Transforms/Passes.h:19-20](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/Transforms/Passes.h#L19-L20)。

Pass 的 TableGen 声明与 `--split-threshold` 选项见 [include/cuda_tile/Dialect/CudaTile/Transforms/Passes.td:65-102](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/Transforms/Passes.td#L65-L102)，其中选项默认值 `1` 见 [Passes.td:96-101](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/Transforms/Passes.td#L96-L101)。注意它声明为 `InterfacePass<"loop-split", "mlir::FunctionOpInterface">`，所以与 u9-l1 的 FuseFMA 一样对任意函数式操作（包括 `entry`）生效。

#### 4.4.4 代码实践

**目标**：跑一次完整的前后对照，并验证 `cuda_tile.loop_split` 属性的优先级。

**步骤**：

1. 复制 [test/Transforms/loop_split.mlir:721-767](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Transforms/loop_split.mlir#L721-L767) 的 `hint_disable_entry_enable_for` 用例到本地文件 `t.mlir`：它给 `entry` 挂了 `cuda_tile.loop_split = 0`、给内部 `for` 挂了 `cuda_tile.loop_split = 1`。
2. 用测试里的官方调用方式跑（`loop-split` 是嵌在 module→entry 管线里的 InterfacePass）：

   ```
   cuda-tile-opt t.mlir \
     --pass-pipeline='builtin.module(cuda_tile.module(cuda_tile.entry(loop-split)))' \
     --split-input-file
   ```
3. 对照同用例的 `CHECK`（[loop_split.mlir:732-737](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Transforms/loop_split.mlir#L732-L737)）：应出现 `mini`/`maxi` 与两段 `for`，且 `CHECK-NOT: if`。
4. 再看 [loop_split.mlir:772-818](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Transforms/loop_split.mlir#L772-L818) 的 `hint_disable_for_enable_if`：`for` 上是 0、某个 `if` 上是 1，确认 `if` 属性优先级高于 `for` 属性，该 `if` 仍被分裂。

**观察现象**：尽管 entry 层级禁用了分裂（0），但 for 层级的 `1` 覆盖了它，循环仍被分裂——验证了「越局部越优先」。

**预期结果**：两个用例的输出都符合各自 `CHECK`，证明优先级为 `if > for > entry > 命令行`。**待本地验证**：实际命令行输出以你本地构建的 `cuda-tile-opt` 为准。

#### 4.4.5 小练习与答案

**练习 1**：非 1 步长时为什么要做 `lb + ⌈(split-lb)/step⌉*step` 的对齐？

> **答案**：循环实际访问的归纳变量值是 `lb, lb+step, lb+2*step, ...`，分裂点必须落在这个网格上，两段循环的边界才能各自取到合法的迭代值。若直接用原始 `splitValue`（可能不落在网格上），第二段下界就不是任何一次真实迭代值，会破坏等价性。代码用 `divi ... rounding<positive_inf>`（向上取整）保证第二段从「不超过原翻转点的最近网格点」开始。

**练习 2**：`mini`/`maxi` 把分裂点夹到 `[lb, ub]` 是为了处理什么情况？

> **答案**：翻转点可能落在循环区间之外（比如 `ub < 32` 而 `cmpi iv < 32`，则条件全程为真）。此时 `mini(split, ub)` 让第一段上界收到 `ub`、`maxi(split, lb)` 让第二段下界收到 `lb`，结果是其中一段退化成空循环、另一段覆盖全程，从而安全地等价于原循环而不产生越界边界。

**练习 3**：命令行 `--split-threshold=5` 与在 `if` 上挂 `cuda_tile.loop_split = 1` 同时存在时，哪个生效？

> **答案**：`if` 上的属性生效（值为 1）。`getSplitThreshold` 按 `if` → `for` → `entry` → 命令行的顺序取第一个存在的值，属性优先级始终高于命令行默认值。

## 5. 综合实践

把本讲的知识串起来，完成一次「手工预测 + 工具验证」的对照实验。

**任务**：基于 [test/Transforms/loop_split.mlir:868-916](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Transforms/loop_split.mlir#L868-L916) 的 `split_step` 用例（步长 `%2 = constant <i64: 4>`、谓词 `greater_than`），完成以下步骤：

1. **先做纸面预测**：
   - 该比较能否通过符号关？（`signed`，与循环一致 → 能）
   - `greater_than` 属于哪类？分裂点要不要 `+1`？（要 `+1`）
   - 步长非 1，分裂点对齐会生成哪几条指令？（`addi`/`subi`/`divi ... rounding<positive_inf>`/`muli`/`addi`）
2. **写出你预期的 CHECK 序列**，然后对照 [loop_split.mlir:879-886](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/test/Transforms/loop_split.mlir#L879-L886) 的官方 `CHECK`，逐行核对。
3. **运行验证**：在本地构建目录执行 `cmake --build build --target check-cuda-tile`，确认 `split_step` 用例通过。
4. **改造实验**：把步长改回 `1`（`%2 = constant <i64: 1>`），重新跑 `cuda-tile-opt`，观察对齐用的 `subi/divi/muli` 是否消失（因为 `isConstOne` 为真，跳过对齐分支，见 [LoopSplit.cpp:288](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/LoopSplit.cpp#L288)）。

**预期结果**：你的纸面预测与官方 `CHECK` 一致；步长改 1 后对齐指令消失。这一步把「规范化 → 收益 → 分裂点计算 → 对齐 → 克隆」整条链路在你脑中打通。**待本地验证**：实际运行结果以本地工具链为准。

## 6. 本讲小结

- **动机**：循环分裂把「条件随归纳变量单调翻转」的 `if` 消除掉，方法是沿翻转点把一个循环切成两段，每段条件恒定、分支发散消失，代码体积膨胀有限。
- **判定五关**：`isSplittableCondition` 依次检查阈值非 0、条件是 `cmpi`、`normalizeForOpCmp` 通过、rhs 是循环不变量、谓词合法且收益达标。
- **规范化**：`normalizeForOpCmp` 把任意方向的比较统一成「iv <op> value」，翻转等价谓词，并强制 `cmp` 的符号性与 `for` 的 `unsignedCmp` 一致。
- **收益启发式**：`isSplitProfitable` 在阈值（默认 1）短路；否则看分支操作数是否达阈值，或是否含九类「重操作」（访存/MMA/归约/嵌套控制流）——重操作无视阈值触发分裂。
- **执行**：`performLoopSplit` 计算分裂点（部分谓词 `+1`、非 1 步长做 `lb+⌈(split-lb)/step⌉*step` 对齐、`mini/maxi` 夹逼），`copyLoop` 克隆两段并把 `cmpi` 替换成常量、只保留恒定一侧的分支体。
- **选项交互**：阈值来源优先级为 `if` 属性 > `for` 属性 > `entry` 属性 > `--split-threshold` 命令行（默认 1，0 表示禁用）；属性名 `cuda_tile.loop_split` 是 discardable 属性。

## 7. 下一步学习建议

- **下一步讲义 u9-l3**：`cuda-tile-optimize` 工具会把 `loop-split` 与 canonicalize/CSE/fuse-fma 组装成默认优化管线，建议接着学它，理解 `TileIROptimizationsOpts.loop_split_threshold`（见 [Passes.h:22-32](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/include/cuda_tile/Dialect/CudaTile/Transforms/Passes.h#L22-L32)）如何与命令行联动，以及设为 `-1` 完全禁用本 Pass 的用法。
- **回看 u9-l1**：对比 FuseFMA 与 LoopSplit 两类 Pass 的差异——前者是「非数值保持」的算术重写（改变位级结果），后者是「语义保持」的结构变换（不改计算结果、只改控制流结构），有助于建立「哪些变换安全、哪些需显式开启」的判断力。
- **继续阅读源码**：想深入可研究 [LoopSplit.cpp:184-243](https://github.com/NVIDIA/cuda-tile/blob/e01244d89cd38e81dde50d60fbfee07ac6d7be22/lib/Dialect/CudaTile/Transforms/LoopSplit.cpp#L184-L243) 的 `copyLoop` 如何用 `IRMapping` 处理嵌套 `if`/`for` 与 `continue`/`yield` 的克隆，并对照 `loop_split.mlir` 中 `supported_split_inner_if`、`supported_if_inside_nested_for`、`split_if_inside_while_loop` 三个嵌套场景的 CHECK 理解其行为边界。
