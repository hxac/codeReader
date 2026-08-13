# rms_norm 样例：表达式级联

## 1. 本讲目标

本讲是专家篇的综合案例讲义。前面我们已经分别掌握了「表达式模板」（u2-l1）、「归约与广播算子」（u2-l4）、「求值器系统」（u3-l1）、「DAG 构建」（u3-l3）以及「Reduce 模块的图分析」（u3-l6）。本讲把这些知识**串成一条完整的端到端执行链**：以 `examples/rms_norm/rms_norm.cpp` 为对象，看用户写下的一个**多步级联表达式**如何从数学公式，落到一行 ATVOSS 表达式，再经 DAG、Tile 求值器，最终变成昇腾硬件上的 ReduceSum / Broadcast / Sqrt / Div 指令。

学完本讲，你应当能够：

1. 把 RMSNorm 数学公式**逐项分解**为「平方 → 行内归约 → 求均值 → 开方 → 缩放」的计算图。
2. 用 ATVOSS 表达式把上述步骤写成 `ReduceSum<AR> → Broadcast<AB> → Divs<WIDTH> → Sqrt` 的级联。
3. 理解**二维 TileShape `Shape<HEIGHT, WIDTH>`** 在归约/广播算子中的关键作用，以及「归约轴必须完整落在一个 Tile 内」的约束。
4. 说清这条级联在 **DAG 层** 与 **Tile 求值层** 各自经历了什么，并理解为何本样例实际走的是 Elewise `FullAutoDag` 而非 `reduce/graph` 的专用归约图分析。

---

## 2. 前置知识

本讲默认你已经读过大纲里的以下讲义，下面只做最简回顾，不再重复细节：

- **u2-l4 归约与广播算子**：`ReduceSum<Pattern>` 与 `Broadcast<Pattern>` 是带编译期 `Pattern` 参数的 `UnaryOp`。约定 `axis0`=行（HEIGHT）、`axis1`=列（WIDTH）；`AR` 沿列归约（结果 `[axis0,1]`），`AB` 沿列广播（结果 `[axis0,axis1]`），两者互逆。
- **u2-l5 数据模型**：`Shape<int...>` 是编译期形状，`TileShape` 的底座；`OperationShape{axis0, axis1}` 是求值器运行时取到的二维形状。
- **u3-l1 求值器系统**：`Evaluator<T>` 用「主模板 + 偏特化」递归求值表达式树，每个 `OpAssign<T, OpXxx<...>>` 特化最终落到一条 Ascend C 指令。
- **u3-l3 DAG 构建**：`FullAutoDag` 把表达式列表转成带依赖的有序算子序列，自动插入 CopyIn/CopyOut、Alloc/Free。
- **u3-l6 Reduce 模块**：`reduce/graph/` 提供了 `ReduceAutoDag` 等专用归约图分析，但**当前 HEAD 下未被公共入口接入**，rms_norm 实际走 Elewise `FullAutoDag`。

> 一个贯穿全讲的直觉：RMSNorm 之所以是「综合案例」，是因为它一次性用到了**改变形状的算子**（ReduceSum/Broadcast）、**标量模板参数算子**（`Divs<WIDTH>`）、**逐元素一元算子**（Sqrt）和**逐元素二元算子**（乘、除），它们必须在一个 Tile 内按依赖顺序协同执行。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [examples/rms_norm/rms_norm.cpp](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/rms_norm/rms_norm.cpp) | 本讲主角。包含 `RmsNormConfig`（`TileShape`/`blockPolicy`/`kernelPolicy`/三级 Builder）与 `RmsNormCompute`（级联表达式），以及 `Run` 里的 ACL 调用样板与精度校验。 |
| [examples/rms_norm/README.md](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/rms_norm/README.md) | 给出 RMSNorm 数学公式、算子规格（`in1`/`in2`/`out`）、形状约束（`n=N`、`N` 需 32 对齐）与编译运行命令。 |
| [include/operators/transcendental_expression.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/transcendental_expression.h) | 声明 `OpReduceSum<Pattern,T>` 与 `OpBroadcast<Pattern,T>` 两个表达式节点及其构造函数 `ReduceSum`/`Broadcast`。 |
| [include/operators/transcendental_evaluator.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/transcendental_evaluator.h) | 为 `OpReduceSum`/`OpBroadcast` 提供 `Evaluator` 特化，映射到 `AscendC::ReduceSum` / `AscendC::Broadcast`。 |
| [include/operators/math_expression.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_expression.h) | 声明 `OpDivs<scalarValue,T>`（`Divs`）与 `DeclareUnaryOp(Sqrt)`，以及 `OpDiv`/`OpMul`。 |
| [include/operators/math_evaluator.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h) | 为 `OpDivs`/`OpSqrt`/`OpDiv` 提供 `Evaluator` 特化，映射到 `DivsAssign`/`SqrtAssign`/`DivAssign`。 |
| [include/utils/patterns.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/patterns.h) | 定义 `enum class Pattern { AR, RA, AB, BA }`。 |
| [include/elewise/block/schedule.h](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h) | `DagSelector` 在 `MemMngPolicy::AUTO` 时选择 `FullAutoDag`，是本样例 DAG 路由的依据。 |

---

## 4. 核心概念与源码讲解

### 4.1 RMSNorm 的数学分解

#### 4.1.1 概念说明

RMSNorm（Root Mean Square Normalization，均方根归一化）是大语言模型里常用的归一化层。与 LayerNorm 相比，它**去掉了「减均值」这一步**，只做「除以均方根」，因此计算更省、且只引入两个统计量中的一个（平方和）。README 给出的数学公式如下：

\[
\operatorname{RmsNorm}(x_i)=\frac{x_i}{\operatorname{Rms}(\mathbf{x})}\, g_i,\qquad
\operatorname{Rms}(\mathbf{x})=\sqrt{\frac{1}{n}\sum_{i=1}^{n} x_i^2}
\]

其中：

- \(x_i\) 是输入 `in1` 的第 \(i\) 个元素（沿「行内」即归约轴展开）。
- \(g_i\) 是缩放权重 `in2`，逐元素相乘。
- \(n\) 是归约轴的长度（本样例中即 `WIDTH`）。
- \(\operatorname{Rms}(\mathbf{x})\) 是该行所有元素的**均方根**。

由于本样例只处理**二维**输入（`MAX_DIM = 2`），归约是「按行」进行的：每一行各自算一个平方和，得到一个标量，再开方、回除、乘权重。

#### 4.1.2 核心流程

把上式拆成 6 个串行步骤，每一步对应一个张量形状变化（记行数 \(M\)=`axis0`，列数 \(N\)=`axis1`）：

| 步骤 | 运算 | 形状变化 | 数学含义 |
| --- | --- | --- | --- |
| ① 平方 | \(x_i^2\) | `[M, N] → [M, N]` | 准备求和的项 |
| ② 行内归约 | \(\sum_{i} x_i^2\) | `[M, N] → [M, 1]` | 每行的平方和 |
| ③ 求均值 | \(\frac{1}{n}\sum x_i^2\) | `[M, 1] → [M, 1]` | 平方和除以 \(n\) |
| ④ 开方 | \(\sqrt{\cdot}\) | `[M, 1] → [M, 1]` | 得到均方根 Rms |
| ⑤ 回除缩放 | \(x_i / \text{Rms}\) | 需要 Rms 从 `[M,1]` 扩回 `[M,N]` 后再逐元素除 | 归一化 |
| ⑥ 乘权重 | \(g_i \cdot (\cdot)\) | `[M, N]` | 得到最终输出 |

**关键难点**在第 ⑤ 步：归约后的 Rms 形状是 `[M, 1]`，而 \(x_i\) 是 `[M, N]`，两者形状不一致无法直接逐元素相除。必须先用**广播**把 `[M, 1]` 扩回 `[M, N]`（即每行的同一个标量复制 \(N\) 份），再做逐元素除法。这就是 RMSNorm 必须同时使用「归约」与「广播」的原因——二者在形状上是**互逆操作**。

#### 4.1.3 源码精读

数学公式在 README 中给出：

- [examples/rms_norm/README.md:15-18](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/rms_norm/README.md#L15-L18)：算子数学计算公式，`Rms(x) = sqrt(1/n * Σ x²)`。

样例在 `Run` 里用一组**特别构造的取值**来验证正确性：`in1`（即 \(x\)）全填 `1.0F`，`in2`（即 \(g\)）全填 `2.0F`，期望输出 `golden` 全为 `2.0F`：

- [examples/rms_norm/rms_norm.cpp:198](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/rms_norm/rms_norm.cpp#L198)：`hostInput1` 全 `1.0F`（\(x\)）。
- [examples/rms_norm/rms_norm.cpp:201](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/rms_norm/rms_norm.cpp#L201)：`hostInput2` 全 `2.0F`（\(g\)）。
- [examples/rms_norm/rms_norm.cpp:222-226](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/rms_norm/rms_norm.cpp#L222-L226)：`golden` 全 `2.0F`，用 `VerifyResults` 校验。

代入公式（设 `WIDTH = n = 32`，每行 32 个 1.0）：\(\sum x_i^2 = 32\)，均值 \(= 32/32 = 1\)，\(\text{Rms} = \sqrt{1} = 1\)，\(\text{out} = g \cdot x / \text{Rms} = 2 \cdot 1 / 1 = 2.0\)，与 `golden` 完全吻合。

#### 4.1.4 代码实践

1. **实践目标**：手工验证 golden，确认对数学公式的理解。
2. **操作步骤**：
   - 假设把 `hostInput1` 改成全 `2.0F`（其余不变：`in2=2.0F`、`WIDTH=32`）。
   - 用本节的 6 步流程手工计算新的 `golden`。
3. **需要观察的现象**：每行平方和变为 \(32 \times 2^2 = 128\)；均值 \(128/32 = 4\)；\(\text{Rms}=\sqrt{4}=2\)；\(\text{out}=g \cdot x/\text{Rms}=2 \cdot 2/2=2.0\)。
4. **预期结果**：输出仍为 `2.0F`。这是一个有趣的「不动点」：当 \(x\) 全相等时，RMSNorm 的输出恒等于权重 \(g\)（因为 \(x/\text{Rms} = x/(|x|) = \text{sign}(x)\)，\(x>0\) 时为 1）。
5. **运行结论**：上述为纯数学推导，可自行改源码后用 `output/bin/rms_norm --shape=1,32` 验证，**具体数值待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 RMSNorm 比 LayerNorm 少算一个统计量？
**答案**：LayerNorm 需要先减去均值（\(\mu\)）再除以标准差，统计量是「均值 + 方差」两个；RMSNorm 直接除以均方根，**省去了均值**这一步，只算平方和。

**练习 2**：若把 `WIDTH` 从 32 改成 64（输入也相应变成 64 列），golden 是否还是 2.0？
**答案**：仍是 2.0。因为只要 \(x\) 全相等，\(\text{Rms}=|x|\)，输出恒为 \(g\)，与列数 \(n\) 无关。

---

### 4.2 表达式级联：ReduceSum→Broadcast→Divs→Sqrt→乘法

#### 4.2.1 概念说明

4.1 节的 6 个数学步骤，在 ATVOSS 里被写成 `Compute()` 中**一行级联表达式**。核心思想：每一步用一个表达式节点描述，节点之间通过「`auto` 中间变量」串联，最终 `return out = ...` 触发整棵表达式树的组装。

涉及的算子（均在前面讲义中介绍过）：

- `ReduceSum<Pattern::AR>(in1 * in1)`：步骤①+②，先逐元素平方（`in1*in1` → `OpMul`），再沿列归约。
- `Broadcast<Pattern::AB>(_1)`：步骤⑤的前置，把 `[M,1]` 扩回 `[M,N]`。
- `Divs<WIDTH>(_2)`：步骤③，除以编译期常量 `WIDTH`（即 \(n\)），把平方和变成均值。
- `Sqrt(...)`：步骤④。
- `in1 / Sqrt(...)`：步骤⑤的逐元素除法（`/` → `OpDiv`）。
- `in2 * _3`：步骤⑥，乘权重（`*` → `OpMul`）。

#### 4.2.2 核心流程

`Compute()` 中的级联（伪代码，形状用 `[M,N]` 标注）：

```text
in1:  Param<1> [M, N]  (x)
in2:  Param<2> [M, N]  (g)
out:  Param<3> [M, N]

_1 = ReduceSum<AR>(in1 * in1)        // [M,N] → [M,1]   平方和
_2 = Broadcast<AB>(_1)               // [M,1] → [M,N]   广播回原形状
_3 = in1 / Sqrt( Divs<WIDTH>(_2) )   // [M,N]           x / sqrt(mean)
return out = in2 * _3                // [M,N]           g * (x/Rms)
```

逐节点形状追踪（设 `HEIGHT=M=1`、`WIDTH=N=32`）：

| 中间量 | 表达式 | 形状 | 对应数学 |
| --- | --- | --- | --- |
| `in1*in1` | `OpMul` | `[1,32]` | \(x_i^2\) |
| `_1` | `OpReduceSum<AR>` | `[1,1]` | \(\sum x_i^2\) |
| `_2` | `OpBroadcast<AB>` | `[1,32]` | 广播后的平方和 |
| `Divs<32>(_2)` | `OpDivs<32>` | `[1,32]` | \(\frac{1}{32}\sum x_i^2\)（均值） |
| `Sqrt(...)` | `OpSqrt` | `[1,32]` | \(\text{Rms}\) |
| `in1 / Sqrt(...)` | `OpDiv` | `[1,32]` | \(x_i/\text{Rms}\) |
| `out = in2 * _3` | `OpMul` | `[1,32]` | \(g_i \cdot x_i/\text{Rms}\) |

> 注意：`AR`（沿列归约）与 `AB`（沿列广播）恰好互逆，这是「归约结果能精确还原成输入形状」的根本原因，详见 4.3 节。

#### 4.2.3 源码精读

`Compute()` 的级联表达式（本讲最核心的 4 行）：

- [examples/rms_norm/rms_norm.cpp:42-45](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/rms_norm/rms_norm.cpp#L42-L45)：`_1 = ReduceSum<AR>(in1*in1)`、`_2 = Broadcast<AB>(_1)`、`_3 = in1 / Sqrt(Divs<WIDTH>(_2))`、`return out = in2 * _3`。中文说明：这 4 行把 RMSNorm 数学公式逐项翻译成表达式节点，每个节点是一个编译期类型化的 `Expression<Op...>` 空壳对象。

`ReduceSum`/`Broadcast` 表达式节点的声明：

- [include/operators/transcendental_expression.h:17-23](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/transcendental_expression.h#L17-L23)：`OpReduceSum<pattern, T>` 继承 `UnaryOp<T>`，把 `Pattern` 编码进类型。
- [include/operators/transcendental_expression.h:26-29](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/transcendental_expression.h#L26-L29)：`ReduceSum(Expression<T>)` 工厂函数，返回 `Expression<OpReduceSum<pattern,T>>`。这正对应 `_1 = ReduceSum<AR>(in1*in1)` 的构造过程。
- [include/operators/transcendental_expression.h:37-49](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/transcendental_expression.h#L37-L49)：`OpBroadcast` 结构体与 `Broadcast` 工厂函数，结构与 `OpReduceSum` 完全对称。

`Divs` 与 `Sqrt` 表达式节点：

- [include/operators/math_expression.h:145-157](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_expression.h#L145-L157)：`OpDivs<scalarValue,T>` 把常量 `scalarValue`（这里是 `WIDTH`）作为模板参数 `auto scalarValue` 编码进类型；`Divs` 工厂返回 `Expression<OpDivs<scalarValue,T>>`。说明：`Divs` 与普通逐元素除法 `OpDiv` 不同，它的除数是**编译期常量**，求值时可优化为「乘以倒数」。
- [include/operators/math_expression.h:165](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_expression.h#L165)：`DeclareUnaryOp(Sqrt)` 宏展开为 `OpSqrt` 结构体与 `operator`/工厂，是一元算子。

`Pattern` 枚举定义：

- [include/utils/patterns.h:14-20](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/patterns.h#L14-L20)：`enum class Pattern { AR, RA, AB, BA }`。本样例用到 `AR`（归约）与 `AB`（广播）。

#### 4.2.4 代码实践

1. **实践目标**：理解 `Divs<WIDTH>` 与「除以标量常数」的差异，为第 5 节的综合实践做准备。
2. **操作步骤**：阅读 [include/operators/math_expression.h:145-163](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_expression.h#L145-L163)，注意 `Divs` 的模板参数是 `auto scalarValue`（编译期常量）。
3. **需要观察的现象**：`Divs<WIDTH>` 的除数必须是**编译期常量整型**（`WIDTH` 是 `constexpr int32_t`），不能是运行时变量。
4. **预期结果**：理解为何 `Divs` 能优化为「乘倒数」——除数在编译期已知，编译器可预先算出 \(1/n\)。
5. **运行结论**：本步为源码阅读型实践，无需运行。

#### 4.2.5 小练习与答案

**练习 1**：把级联里 `Divs<WIDTH>(_2)` 换成 `_2 / WIDTH`（即用 `/` 运算符），结果一样吗？
**答案**：不一样。`Divs<WIDTH>` 的除数是编译期常量，会被 `OpDivs` 求值器特化优化为乘倒数；而 `_2 / WIDTH` 走的是 `OpDiv` 路径，`WIDTH` 作为右操作数会被当标量处理，分派到标量除法 `DivsAssign`（带标量分支），路径不同但数值结果相同。前者更贴近「求均值」的语义且更高效。

**练习 2**：为什么 `_1`（归约结果）不能直接用 `in1 / Sqrt(Divs<WIDTH>(_1))`，而必须先 `Broadcast` 成 `_2`？
**答案**：因为 `_1` 形状是 `[M,1]`，而 `in1` 形状是 `[M,N]`，两者形状不一致，逐元素除法 `OpDiv` 要求左右形状一致。必须先用 `Broadcast<AB>` 把 `_1` 扩成 `[M,N]` 才能相除。

---

### 4.3 二维 TileShape 与归约对齐约束

#### 4.3.1 概念说明

ATVOSS 的 `TileShape` 决定了 Block 层「一次 Tile 处理多少数据」。对于逐元素算子，一维 `TileShape` 就够了；但对于**归约/广播**，必须用**二维 `TileShape`**，因为归约方向（沿哪条轴、归约多长）必须能从 TileShape 中读出来。

本样例用 `Atvoss::Shape<HEIGHT, WIDTH>`，其中 `HEIGHT` 对应 `axis0`（行），`WIDTH` 对应 `axis1`（列，即归约轴）：

- `HEIGHT = 1`：一个 Tile 处理 1 行。
- `WIDTH = 32`：一个 Tile 处理 32 列。

这里有一个**硬约束**：Tile 的列数 `n` 必须等于输入的总列数 `N`。也就是说，**归约轴必须完整地落在一个 Tile 内**。理由很直接：`ReduceSum<AR>` 是在一个 Tile 内、对 UB 里的数据做行内归约；如果同一行的元素被切到两个 Tile，每个 Tile 只能看到「半行」，算出的就不是完整的平方和，结果就错了。

#### 4.3.2 核心流程

二维 TileShape 如何被消费：

1. **编译期**：`Shape<HEIGHT, WIDTH>` 的两个整数经累乘得到 `BASIC_BLOCK = HEIGHT * WIDTH`（u2-l9），决定单核一次 Tile 处理的总元素数。
2. **运行时（Tile 求值）**：求值器从 `ContextData` 取出 `OperationShape`，其 `axis0`/`axis1` 由 TileShape 的两维填充（u2-l5）。归约/广播求值器正是读 `operationShape.axis0`、`operationShape.axis1` 来确定「沿多长的轴做归约/广播」。
3. **形状约束链**：`TileShape.axis1 = WIDTH = N`（归约轴完整）→ 一个 Tile 内 `ReduceSum<AR>` 能算出完整行平方和 → `Broadcast<AB>` 能精确还原。

#### 4.3.3 源码精读

样例的常量与 TileShape 定义：

- [examples/rms_norm/rms_norm.cpp:24-26](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/rms_norm/rms_norm.cpp#L24-L26)：`HEIGHT=1`、`WIDTH=32`、`MAX_DIM=2`。
- [examples/rms_norm/rms_norm.cpp:33](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/rms_norm/rms_norm.cpp#L33)：`using TileShape = Atvoss::Shape<HEIGHT, WIDTH>;`——二维形状编码进类型，`axis0=HEIGHT`、`axis1=WIDTH`。

README 明确给出的形状约束（关键：`n = N`）：

- [examples/rms_norm/README.md:61-66](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/rms_norm/README.md#L61-L66)：只支持二维输入；总输入 `(M,N)` 需满足 `M<8160`、`N<=7168`、`N` 需 32 对齐；**Tile 块 `(m,n)` 需满足 `n = N`、`m*n <= 7168`**。中文说明：`n = N` 正是「归约轴必须完整落在一个 Tile 内」的明文体现；`m*n <= 7168` 是 UB 容量约束。

`OperationShape` 的定义（求值器运行时读到的二维形状）：

- [include/utils/layout/layout.h:15-19](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/utils/layout/layout.h#L15-L19)：`struct OperationShape { axis0, axis1, axis2 }`。归约/广播求值器只认 `axis0`/`axis1`（即二维）。

#### 4.3.4 代码实践

1. **实践目标**：体会「归约轴必须对齐」的约束。
2. **操作步骤**：把 [rms_norm.cpp:33](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/rms_norm/rms_norm.cpp#L33) 的 `TileShape` 改成 `Shape<1, 16>`（即 `WIDTH` 改为 16），同时保持运行参数 `--shape=1,32`（总列数仍是 32）。
3. **需要观察的现象**：此时 Tile 的列数（16）小于总列数（32），同一行的 32 个元素被切成两个 Tile。
4. **预期结果**：归约结果将出错——每个 Tile 只算了「半行」的平方和，`Rms` 不再正确。这是**待本地验证**的行为，预期精度校验失败；也可能触发框架的形状对齐断言。
5. **正确做法**：`WIDTH` 必须与运行时 `--shape` 的列数一致，且满足 32 对齐与 `n <= 7168`。

#### 4.3.5 小练习与答案

**练习 1**：为什么本样例的 `TileShape` 用二维 `Shape<1,32>` 而不是一维 `Shape<32>`？
**答案**：因为算子里有归约（`ReduceSum<AR>`）和广播（`Broadcast<AB>`），它们需要知道「沿哪条轴、轴长多少」才能正确执行。一维 `Shape` 只能编码总元素数，无法表达归约方向；二维 `Shape<HEIGHT,WIDTH>` 让 `axis0`=行、`axis1`=列，`AR` 即「沿 axis1 归约」有了明确语义。

**练习 2**：若输入是 `(M=4, N=32)`，`TileShape` 设 `Shape<2,32>`，一次 Tile 处理哪些数据？需要几个 Tile？
**答案**：一次 Tile 处理 2 行 × 32 列 = 64 个元素（2 个完整行）。共 4 行，需要 2 个 Tile。每个 Tile 内独立做 2 行的行内归约，正确。

---

### 4.4 端到端执行：DAG 与 Tile 求值层的协同

#### 4.4.1 概念说明

前面 3 个模块讲了「算什么」（数学）、「怎么写」（表达式）、「形状如何对齐」（TileShape）。本模块回答最后一个问题：**这条级联表达式，从用户写下到变成硬件指令，到底经历了哪几层处理？**

这里有一个**必须澄清的事实**（与 u3-l6 一致）：`reduce/graph/` 下的 `ReduceAutoDag` 等**专用归约图分析模块，在当前 HEAD 下并未被公共入口接入**。rms_norm 走的是标准的 Elewise 路径：Block 层的 `DagSelector` 在 `MemMngPolicy::AUTO`（默认）时选择 `FullAutoDag`。也就是说，本样例里的 `ReduceSum`/`Broadcast` 是作为**普通 Op 节点**，被与逐元素算子**同一套** DAG 与求值器管线处理的——它们之所以能工作，靠的是「归约轴完整落在单 Tile 内」这个约束，而不是专门的归约图分析。

> 提示：`reduce/graph/ReduceAutoDag` 的接入状态为「待确认」，如果你后续看到它被挂进 `DagSelector` 或 Block 调度，需要重新评估本节结论。

#### 4.4.2 核心流程

端到端执行链（承接 u3-l4 → u3-l3 → u3-l1 → u3-l2）：

```text
用户表达式 (out = in2 * (in1 / Sqrt(Divs<32>(Broadcast<AB>(ReduceSum<AR>(in1*in1))))))
   │
   ① 线性化 ToLinearizerExpr：后序遍历，把嵌套树拉平成 OpAssign 语句序列
   │   （含 LocalVar 引入、冗余 Cast 消除）—— u3-l4
   │
   ② FullAutoDag：把语句序列转成带依赖的有序算子列表
   │   - 自动插入 OpCopyIn（GM→UB）、OpCopyOut（UB→GM）
   │   - 依赖拓扑排序（数据依赖决定执行顺序）
   │   - DagNodeInfo 估算缓冲，后续插 OpAlloc/OpFree —— u3-l3
   │
   ③ Block 层循环：单核任务切成多个 Tile，每个 Tile 构造 ContextData —— u2-l9
   │
   ④ Tile::Evaluate：对有序算子列表逐条递归求值 —— u3-l1 / u3-l2
   │   每条 OpAssign<T, OpXxx<...>> 命中一个 Evaluator 特化：
   │     OpCopyIn  → DataCopyPad (GM→UB)
   │     OpReduceSum<AR> → AscendC::ReduceSum<AR>     （形状 [axis0, axis1] → [axis0,1]）
   │     OpBroadcast<AB> → AscendC::Broadcast          （形状 [axis0,1] → [axis0,axis1]）
   │     OpDivs<32> → DivsAssign（乘倒数）
   │     OpSqrt    → SqrtAssign
   │     OpDiv     → DivAssign（逐元素除）
   │     OpMul     → MulAssign（逐元素乘，含标量分派）
   │     OpCopyOut → DataCopyPad (UB→GM)
   │
   ⑤ 流水同步：MTE2（搬入）→ V（Vector 计算）→ MTE3（搬出），配合双缓冲 —— u3-l2/u3-l5
```

**关键点**：归约/广播求值器特化与逐元素求值器特化**结构完全一致**，都是「对左右子表达式递归构造 `Evaluator`，再调一个 `XxxAssign` 辅助函数」。差别只在 `XxxAssign` 内部调用的是 `AscendC::ReduceSum`/`AscendC::Broadcast` 还是 `AscendC::Sqrt`/`AscendC::Div`。这种「同构」正是 rms_norm 能把归约与逐元素算子无缝级联的原因。

#### 4.4.3 源码精读

**DAG 路由**：本样例 `blockPolicy` 用默认的 `MemMngPolicy`（`DefaultBlockPolicy<TileShape>` 默认 `AUTO`），因此：

- [include/elewise/block/schedule.h:41-44](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h#L41-L44)：`DagSelector` 在 `memMngPolicy == MemMngPolicy::AUTO` 时，`using Type = FullAutoDag<ExprList>`。中文说明：这正是 rms_norm 实际走 Elewise `FullAutoDag`、而非 `reduce/graph/ReduceAutoDag` 的直接证据。

样例中 `blockPolicy`/`kernelPolicy` 的定义：

- [examples/rms_norm/rms_norm.cpp:49-54](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/rms_norm/rms_norm.cpp#L49-L54)：`blockPolicy` 用 `DefaultBlockPolicy<TileShape>`（默认 `AUTO`），`kernelPolicy` 用 `UniformSegment` 均匀切分。

**归约/广播求值器特化**（最直接的「表达式→硬件」映射）：

- [include/operators/transcendental_evaluator.h:44-55](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/transcendental_evaluator.h#L44-L55)：`ReduceSumAssign`——`AR` 调 `AscendC::ReduceSum<T, AscendC::Pattern::Reduce::AR, true>(dst, src, shape, true)`，`shape = {axis0, axis1}`。说明：ATVOSS 的 `Pattern::AR` 直接复用了 AscendC 的 `Pattern::Reduce::AR`，二者语义一致。
- [include/operators/transcendental_evaluator.h:24-37](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/transcendental_evaluator.h#L24-L37)：`BroadcastAssign`——`AB` 时 `srcShape = {axis0, 1}`、`dstShape = {axis0, axis1}`，调 `AscendC::Broadcast<T, 2, 1>`（沿轴 1 广播）。说明：这正是「把 `[M,1]` 扩回 `[M,N]`」的实现，与 `ReduceSum<AR>` 互逆。
- [include/operators/transcendental_evaluator.h:86-99](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/transcendental_evaluator.h#L86-L99)：`Evaluator<OpAssign<T, OpReduceSum<pattern,U>>>` 特化。说明：它对左右子表达式分别构造 `Evaluator`（递归），取 `GetUbTensor()`，再调 `ReduceSumAssign`；`OperationShape` 由 `GetShape<Operation::Binary>(context.argsTensors)` 取得（即用二维形状）。

**逐元素算子求值器特化**（级联的后半段）：

- [include/operators/math_evaluator.h:436-448](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L436-L448)：`Evaluator<OpAssign<T, OpDivs<scalarValue,U>>>` → `DivsAssign`，除数 `scalarValue=WIDTH` 编译期已知。
- [include/operators/math_evaluator.h:502-514](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L502-L514)：`Evaluator<OpAssign<T, OpSqrt<U>>>` → `SqrtAssign`。
- [include/operators/math_evaluator.h:420-425](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L420-L425)：`Evaluator<OpAssign<T, OpDiv<U,V>>>` 在**非 arch35**（即 ascend950 默认）分支走 `DivAssign`（逐元素除）。说明：`in1 / Sqrt(...)` 两边都是张量，故落到逐元素 `DivAssign`，不走标量分支。

#### 4.4.4 代码实践

1. **实践目标**：追踪一条 `OpAssign` 的求值器分派链，巩固 u3-l1/u3-l2 的理解。
2. **操作步骤**：聚焦级联中的 `Divs<WIDTH>(_2)` 这一步，画出它在 `Tile::Evaluate` 中被求值的过程。
3. **需要观察的现象**：
   - 表达式 `Divs<WIDTH>(_2)` 经线性化后变成形如 `OpAssign<某LocalVar, OpDivs<WIDTH, OpBroadcast<AB, OpReduceSum<AR, OpMul<Param1,Param1>>>>>` 的语句（嵌套结构会被拉平，此处仅为示意）。
   - 求值时命中 [math_evaluator.h:436](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/math_evaluator.h#L436) 的 `OpDivs` 特化。
   - 该特化内部对右子表达式（`_2`，即 `OpBroadcast`）递归构造 `Evaluator`，最终取到 UB 中的 `LocalTensor`，再调 `DivsAssign` 完成「除以 32」。
4. **预期结果**：理解「每个 `Op` 节点 = 一个 `Evaluator` 特化 = 一条 Ascend C 指令」，归约/广播与逐元素算子在求值机制上**完全同构**。
5. **运行结论**：本步为源码阅读型实践，无需运行；具体线性化后语句的确切形态可结合 u3-l4 的 `test_expr_linearizer` 单测进一步确认。

#### 4.4.5 小练习与答案

**练习 1**：rms_norm 的 `ReduceSum`/`Broadcast` 走的是 `reduce/graph/ReduceAutoDag` 还是 Elewise `FullAutoDag`？依据是什么？
**答案**：走 Elewise `FullAutoDag`。依据是 [block/schedule.h:41-44](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/elewise/block/schedule.h#L41-L44) 的 `DagSelector`：`AUTO` 策略下选 `FullAutoDag`；而 `reduce/graph/ReduceAutoDag` 当前未被 `DagSelector` 或 Block 调度引用。`ReduceAutoDag` 的接入状态待确认。

**练习 2**：归约求值器与逐元素 Sqrt 求值器在结构上有什么共性？
**答案**：二者都是「`Evaluator<OpAssign<T, OpXxx<...>>>` 偏特化 + 对子表达式递归构造 `Evaluator` + 调一个 `XxxAssign` 辅助函数落到 Ascend C 指令」的同构模板。差别只在 `XxxAssign` 调用的是 `ReduceSum`/`Broadcast` 还是 `Sqrt`。

---

## 5. 综合实践

**任务**：把 RMSNorm 表达式里的 `Divs<WIDTH>(_2)` 改为除以一个**标量常数**，观察并解释这一改动对 `ReduceSum→Broadcast` 之后计算语义的影响；再说明为何 `Broadcast<AB>` 能把归约结果还原成与输入同形状。

### 操作步骤

1. 打开 [examples/rms_norm/rms_norm.cpp:44](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/examples/rms_norm/rms_norm.cpp#L44)，把
   ```cpp
   auto _3 = in1 / Atvoss::Sqrt(Atvoss::Divs<WIDTH>(_2));
   ```
   改为除以另一个编译期常数，例如 `64`：
   ```cpp
   auto _3 = in1 / Atvoss::Sqrt(Atvoss::Divs<64>(_2));
   ```
   注意：`Divs` 的模板参数必须是编译期常量整型（`auto scalarValue`），所以只能填 `constexpr` 整数。
2. 重新编译：
   ```bash
   bash scripts/build.sh -DSOC=ascend950 rms_norm
   ```
3. 运行（保持默认输入 `in1` 全 1、`in2` 全 2、`--shape=1,32`）：
   ```bash
   output/bin/rms_norm --shape=1,32
   ```

### 需要观察与解释的现象

- **计算语义的变化**：原本 `Divs<WIDTH>`（`WIDTH=32`）把平方和除以列数 \(n\) 得到**均值** \(\frac{1}{n}\sum x_i^2\)，这是 RMSNorm 的正确语义。改成 `Divs<64>` 后，除数不再等于归约长度 \(n=32\)，算出的是 \(\frac{1}{64}\sum x_i^2\) 而非均值。对 `in1` 全 1 的情况：平方和 = 32，`Divs<64>` 后 = 0.5，`Sqrt` = \(\sqrt{0.5}\)，`out = 2 * 1 / \sqrt{0.5} ≈ 2.828`。这不再等于 golden `2.0`，精度校验将**失败**——证明了 `Divs` 的除数必须与归约轴长度一致才能保持 RMSNorm 语义。具体数值**待本地验证**。
- **结论**：`Divs<WIDTH>` 中的 `WIDTH` 不是一个「可随意调」的参数，它承载了「除以 \(n\) 求均值」的数学语义；改动它会破坏归一化含义。

### 第二问：为何 `Broadcast<AB>` 能把归约结果还原成与输入同形状

- `ReduceSum<AR>` 沿 `axis1`（列）归约，把 `[axis0, axis1]` 压成 `[axis0, 1]`——每一行塌缩成一个标量。
- `Broadcast<AB>` 沿 `axis1`（列）广播，把 `[axis0, 1]` 扩回 `[axis0, axis1]`——把那一个标量沿列复制 `axis1` 份。
- 由 [transcendental_evaluator.h:29-31](https://github.com/gitcode.com/cann/atvoss/blob/999805358318db9288c6e9e8c2e49e098ad4f8db/include/operators/transcendental_evaluator.h#L29-L31) 可见，`AB` 的 `srcShape = {axis0, 1}`、`dstShape = {axis0, axis1}`，恰好是 `AR` 的逆：`AR` 把 axis1 从 `axis1` 压成 1，`AB` 把 axis1 从 1 扩回 `axis1`。轴与方向都相反，故能精确还原输入形状。

---

## 6. 本讲小结

- RMSNorm 的数学本质是「平方 → 行内归约 → 求均值 → 开方 → 回除缩放」，其中**归约**（`ReduceSum<AR>`，`[M,N]→[M,1]`）与**广播**（`Broadcast<AB>`，`[M,1]→[M,N]`）互为逆操作，是形状不匹配时的桥梁。
- ATVOSS 用 `Divs<WIDTH>` 把「除以列数 \(n\)」的均值语义编码为编译期常量除法（可优化为乘倒数），`Divs` 的除数**必须等于归约轴长度**才正确。
- 二维 `TileShape = Shape<HEIGHT, WIDTH>` 是归约/广播算子的前提：`axis0`=行、`axis1`=列（归约轴）；README 的 `n = N` 约束保证「归约轴完整落在一个 Tile 内」。
- 本样例实际走 Elewise `FullAutoDag`（由 `DagSelector` 在 `AUTO` 策略下选定），`reduce/graph/ReduceAutoDag` 当前未被公共入口接入，接入状态待确认。
- 归约/广播求值器特化与逐元素算子求值器特化**结构同构**：都是「递归求值子表达式 + 调一个 `XxxAssign` 落到 Ascend C 指令」，这是级联能无缝工作的根本原因。
- 端到端链路为：表达式 → 线性化 → `FullAutoDag`（插 CopyIn/CopyOut、依赖排序、插 Alloc/Free）→ Block 切 Tile 循环 → `Tile::Evaluate` 逐条递归求值 → MTE2/V/MTE3 流水同步。

---

## 7. 下一步学习建议

- **回看 u3-l6 Reduce 模块**：本讲指出 `reduce/graph/ReduceAutoDag` 当前未接入。建议跟踪 `DagSelector` 与 Block 调度的演进，一旦 `ReduceAutoDag` 被挂入，归约/广播的图处理（边界插 Copy、PreReduce/Reduced 分段、`IsNodeUsedAfter` 缓冲复用）就会取代当前的「单 Tile 内同构求值」，需重新理解。
- **阅读测试**：`tests/st/` 下有 `test_tile_rms_norm_*` 类用例（见 u3-l10），可对照本讲理解 RMSNorm 在 ST 层的覆盖方式；`tests/ut/host/test_expr_linearizer.cpp` 能帮你看到本讲级联表达式**线性化后**的确切语句序列。
- **扩展实践**：尝试用同样的级联思路实现 **LayerNorm**（在 RMSNorm 基础上增加「减均值」步骤，需要额外一次 `ReduceSum` 和一次减法），体会「归约→广播→逐元素」这一通用模式如何复用。
- **继续专家篇**：下一篇 u3-l8「策略与配置定制」将讲解如何通过自定义 `TileShape`、`MemMngPolicy`、`Schedule` 模板形参与 `platform_info` 做硬件感知调优——可结合本讲的 `n*N <= 7168`、UB 容量约束一起实践。
