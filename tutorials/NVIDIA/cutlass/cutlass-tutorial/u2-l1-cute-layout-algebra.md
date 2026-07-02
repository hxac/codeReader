# CuTe Layout 与布局代数

## 1. 本讲目标

上一讲 [u1-l5](u1-l5-matrix-layouts.md) 我们学过 CUTLASS 2.x 的布局：`RowMajor` / `ColumnMajor` 这些 **layout tag 是函数对象类**，给定一个逻辑坐标 `(row, col)`，返回它在内存里的偏移 `offset = r + c · ldm`。那是一种「二维、扁平、写死公式」的布局。

从 CUTLASS 3.x 起，整个库重构到了一套更强大、更本质的抽象上——**CuTe**（读作 *cute*）。CuTe 的第一块基石就是 **Layout**：它把「形状（Shape）」和「步长（Stride）」从二维公式解放出来，变成可以任意嵌套、可以做代数运算的「纯函数」。理解 Layout，是理解 3.x 一切（Tensor、Atom、collective MMA、TMA……）的前提。

学完本讲，你应当能够：

- 用一句话说清 **Layout = (Shape, Stride)** 的本质：它是一个从「逻辑坐标」到「线性下标」的函数。
- 看懂 CuTe 的 **层级化 int_tuple**：Shape / Stride 可以是整数，也可以是任意嵌套的元组，并用 `rank` / `depth` / `product` 描述它的结构。
- 解释 Layout 的 **代数运算**：`composition`（复合）、`complement`（补）、`coalesce`（合并）、`filter`（过滤），并理解「布局即函数」为什么让这些运算成为可能。
- 用 **`zipped_divide` / `local_tile`** 对一个布局做分块（tiling），并能预测分块后每个 tile 内元素到全局下标的映射。

本讲是 [u2-l2 CuTe Tensor](u2-l2-cute-tensor.md)、[u2-l4 CuTe Atoms](u2-l4-cute-atoms.md) 以及后面所有 3.x GEMM 讲义的共同地基。

## 2. 前置知识

- **把内存看成一条一维字节流**：任何多维数组在内存里都是线性的。「布局」就是「逻辑坐标 → 线性下标」的翻译规则。这一点在 [u1-l5](u1-l5-matrix-layouts.md) 已建立。
- **列主序 / 行主序与 leading dimension**：列主序下 `offset(r,c) = r + c·ldm`；行主序下 `offset(r,c) = c + r·ldm`。本讲例子默认列主序。
- **C++ 模板与 `constexpr`**：CuTe 大量使用模板元编程，把很多形状/步长在**编译期**算出来（用 `Int<N>` 这类「静态整数」）。你会看到 `Int<4>{}` 或简写 `_4` 这样的「编译期常量」。
- **函数复合**：若有两个函数 \(f\) 和 \(g\)，复合 \((f \circ g)(x) = f(g(x))\)。Layout 的代数核心就是函数复合。
- 承接 [u1-l5](u1-l5-matrix-layouts.md)：2.x 的 `layout::ColumnMajor` 是 CuTe Layout 的一个特例；本讲把它推广到任意维度与任意嵌套。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `include/cute/layout.hpp` | CuTe Layout 的核心：`Layout<Shape,Stride>` 类、`make_layout`、`composition`、`complement`、`coalesce`、`logical_divide`、`zipped_divide`、`tiled_divide` 等。本讲主角。 |
| `include/cute/int_tuple.hpp` | IntTuple 工具：`rank`、`depth`、`product`、`get<I>`、元组上的变换与折叠。Layout 的 Shape/Stride 就是 IntTuple。 |
| `include/cute/stride.hpp` | 坐标与下标互转的核心函数 `crd2idx`（坐标→下标）和 `idx2crd`（下标→坐标），以及它们的递归 divmod 规则。 |
| `include/cute/underscore.hpp` | 定义占位符 `_`（Underscore），用于在切片/分块时「保留某一维」。 |
| `include/cute/tensor_impl.hpp` | `local_tile` / `local_partition`——作用在 **Tensor** 上的分块/分区便捷函数；其内部就是调用 `zipped_divide`。 |

> 关键判断（承接 [u1-l3](u1-l3-directory-structure.md)）：`include/cute/` 是 3.x 的更底层地基，`cutlass::gemm` 反过来 `#include "cute/..."`。本讲读的全是 `cute::` 命名空间下的东西。

## 4. 核心概念与源码讲解

### 4.1 Shape 与 Stride：把布局看成函数

#### 4.1.1 概念说明

一个 CuTe **Layout** 就是两个东西的组合：

- **Shape（形状）**：每个维度的「长度」。
- **Stride（步长）**：沿每个维度走一步，下标增加多少。

它定义了一个函数

\[
L : \text{坐标} \longrightarrow \mathbb{Z}_{\ge 0}, \qquad L(\text{coord}) = \text{线性下标}.
\]

例如最经典的列主序 4×4 矩阵，写作 `(4,4):(1,4)`（Shape 是 `(4,4)`，Stride 是 `(1,4)`）：

\[
L(i,j) = i\cdot 1 + j\cdot 4 = i + 4j.
\]

- `L(0,0)=0`，`L(1,0)=1`，`L(2,0)=2`，`L(3,0)=3` —— 第 0 列占下标 0..3；
- `L(0,1)=4` —— 第 1 列从下标 4 开始。

这正是 [u1-l5](u1-l5-matrix-layouts.md) 里 `ColumnMajor` 的 `offset = r + c·ldm`（这里 ldm=4）。区别在于：2.x 把它写死成一个 functor 类，而 CuTe 把它拆成「Shape + Stride」两个可独立操作的数据。

> 一个极其重要的直觉：**Layout 不持有任何数据指针**。它只是一段「坐标→下标」的纯函数。数据指针要等到下一讲 [u2-l2](u2-l2-cute-tensor.md) 的 `Tensor = (Engine, Layout)` 才登场。所以本讲所有运算都不需要真实内存，可以在编译期或 CPU 上完成。

#### 4.1.2 核心流程

把坐标翻译成下标的核心函数叫 **`crd2idx`**（coordinate to index）。它对三种输入形态有三种行为（见源码注释）：

\[
\begin{aligned}
\text{op}(c, s, d) &= c\cdot d && \text{三者都是整数：直接乘} \\
\text{op}(c, (s,S), (d,D)) &= \text{op}(c \bmod \Pi s,\ s,\ d) + \text{op}(c \div \Pi s,\ (S),(D)) && \text{坐标是整数、形状/步长是元组：逐维 divmod} \\
\text{op}((c,C),(s,S),(d,D)) &= \text{op}(c,s,d) + \text{op}((C),(S),(D)) && \text{三者都是元组：逐维独立求和}
\end{aligned}
\]

第二种情况最关键：**当你给 `Layout` 传一个一维的「扁平下标」时，它会自动 divmod 拆成多维坐标**。这就是为什么 `L(7)`（传一个整数给二维布局）也能工作——它把 7 拆成 (3,1)，得到 `L(3,1)=7`。

`Layout` 类的 `operator()` 正是调用 `crd2idx`：如果坐标里没有占位符 `_`，就返回一个整数下标；如果有 `_`，则返回一个子布局（slice，见 4.4）。

#### 4.1.3 源码精读

**Layout 类本体**——私有继承 `cute::tuple<Shape, Stride>`（用空基类优化 EBO 让纯静态布局零开销），默认步长是 `LayoutLeft::Apply<Shape>`（即列主序紧凑排布）：

- [include/cute/layout.hpp:98-109](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/layout.hpp#L98-L109) —— `struct Layout<Shape, Stride>`，构造函数接收 `(shape, stride)`，并把 `rank` 作为编译期常量暴露。

**坐标→下标映射**——`operator()` 的核心分支：

- [include/cute/layout.hpp:164-175](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/layout.hpp#L164-L175) —— 无 `_` 时返回 `crd2idx(coord, shape(), stride())`（一个下标）；有 `_` 时调用 `slice` 返回子布局。
- [include/cute/layout.hpp:178-183](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/layout.hpp#L178-L183) —— 多参数便捷重载：`layout(a, b, c)` 等价于 `layout(make_coord(a,b,c))`。

**`crd2idx` 的实现**——三种形态的分派：

- [include/cute/stride.hpp:47-56](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/stride.hpp#L47-L56) —— 注释精确描述了上面三条递归规则，是理解 CuTe 坐标语义的最佳入口。
- [include/cute/stride.hpp:99-124](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/stride.hpp#L99-L124) —— `crd2idx` 主体：元组×元组走 `crd2idx_ttt`（逐维求和），整数×元组走 `crd2idx_itt`（divmod 拆解），整数×整数走最末的 `coord * stride`（[stride.hpp:119](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/stride.hpp#L119)）。

**构造 Layout**——`make_shape` / `make_stride` / `make_layout`：

- [include/cute/layout.hpp:47-60](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/layout.hpp#L47-L60) —— 类型别名 `Shape<...>` / `Stride<...>` 本质就是 `cute::tuple<...>`。
- [include/cute/layout.hpp:332-349](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/layout.hpp#L332-L349) —— `make_layout(shape, stride)` 双参版本，以及只传 `shape` 时自动用 `compact_major<LayoutLeft>` 生成列主序紧凑步长的版本。

**打印与相等**：

- [include/cute/layout.hpp:1917-1921](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/layout.hpp#L1917-L1921) —— `print(Layout)` 输出形如 `(4,4):(1,4)`；这是本讲观察布局最常用的手段。
- [include/cute/layout.hpp:314-321](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/layout.hpp#L314-L321) —— `operator==` 比较 shape 和 stride 是否都相等。

#### 4.1.4 代码实践

**实践目标**：亲手构造 `(4,4):(1,4)`，验证它确实实现了 `L(i,j)=i+4j`。

**操作步骤**（这是「源码阅读 + 手算」型实践，无需 GPU）：

1. 阅读上面的 [layout.hpp:164-175](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/layout.hpp#L164-L175)，确认 `operator()` 在无 `_` 时返回 `crd2idx` 的整数结果。
2. 在纸上对 `(4,4):(1,4)` 手算下表：

| 坐标 (i,j) | L(i,j) = i+4j |
| --- | --- |
| (0,0) | 0 |
| (3,0) | 3 |
| (0,1) | 4 |
| (3,3) | 15 |

3. 再手算「扁平下标→多维坐标」的反向过程：给 `L` 传整数 7，它应 divmod 成 (3,1)，返回 7。

**需要观察的现象**：列主序下，**同一列**的元素下标连续（第 0 列 = 0,1,2,3），**同一行**的元素下标每隔 ldm=4 出现一次。

**预期结果**：上表中所有值都应与 `i+4j` 吻合；`L(7)` 返回 7。

> 若想真正运行，可写一个仅含 host 代码的 `.cu`：`auto L = cute::make_layout(cute::make_shape(_4,_4), cute::make_stride(_1,_4)); printf("%d\n", int(L(cute::make_coord(3,3))));`（编译与运行方式见第 5 节）。具体输出**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：行主序 4×4 矩阵的 CuTe Layout 写法是什么？即满足 `L(i,j)=j+4i` 的 (Shape, Stride)。

> **答案**：`(4,4):(4,1)`。连续维（步长 1）是列 j，跨步维（步长 4）是行 i。

**练习 2**：布局 `(8,8):(1,8)` 上，`L(13)`（传扁平整数）返回多少？

> **答案**：13。因为 13 = 13 mod 8 行坐标 5、列坐标 1，`L(5,1)=5+8=13`。任何「紧凑列主序」布局上，扁平下标 k 都映射回 k 自身。

---

### 4.2 层级化 int_tuple：多维与嵌套

#### 4.2.1 概念说明

CuTe 的 Shape / Stride 不限于「扁平的一维整数列表」。它们是 **IntTuple**——递归定义的结构：

> **IntTuple 是一个整数，或者一个由 IntTuple 组成的元组。**

也就是说，Shape 可以嵌套。比如一个 GEMM 里常见的 MMA 输出布局，shape 可能写成 `((2,2),(4,4))` 而不是扁平的 `(4,16)`。嵌套表达的是**逻辑分组**：外层两个模式（mode）分别对应「MMA 指令的两个维度」，每个模式内部又被进一步分成两个子模式。

为了描述一个 IntTuple 的结构，CuTe 提供两个关键量：

- **`rank`（秩）**：最外层元组有几个元素（一个整数算 rank 1）。
- **`depth`（深度）**：嵌套了几层（一个整数 depth 0；扁平元组 `(a,b,c)` depth 1；`((a,b),(c))` depth 2）。
- **`product`（乘积）**：所有叶节点整数的乘积，即这个 IntTuple 代表的「总元素数」。

例如：

| IntTuple | rank | depth | product |
| --- | --- | --- | --- |
| `4` | 1 | 0 | 4 |
| `(4,4)` | 2 | 1 | 16 |
| `((2,2),(4,4))` | 2 | 2 | 64 |

> 直觉：`rank` 是「这一层有几个抽屉」，`depth` 是「抽屉嵌套了几层」，`product` 是「最里面一共有多少件东西」。

#### 4.2.2 核心流程

CuTe 处理 IntTuple 的方式是**递归地对每个模式独立操作**。回头看 4.1.2 的第三条规则：

\[
\text{op}((c,C),(s,S),(d,D)) = \text{op}(c,s,d) + \text{op}((C),(S),(D)).
\]

当 Shape / Stride / Coord 都是同构（congruent，即结构一致）的元组时，`crd2idx` 只是把每个模式独立计算后求和。这意味着**嵌套结构不影响最终下标，只影响「我们如何理解分组」**。

例如 `(4,4):(1,4)` 与 `((4,4)):((1,4))`（多套一层）的 `product` 都是 16，作为「坐标→下标」的函数也等价；区别仅在于前者是扁平二维，后者强调「这是一个整体」。CuTe 用 `flatten`（拍平到 depth≤1）、`coalesce`（合并可合并的模式）等工具在不同表示间转换。

#### 4.2.3 源码精读

**IntTuple 的定义与 `get`**：

- [include/cute/int_tuple.hpp:40-43](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/int_tuple.hpp#L40-L43) —— IntTuple 的递归定义（「整数或整数的元组的元组」）。
- [include/cute/int_tuple.hpp:51-67](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/int_tuple.hpp#L51-L67) —— 对整数也定义 `get<0>(int) == int`，并支持 `get<0,1,...>` 的递归多层取值，使得「整数」与「单元素元组」在很多算法里可统一处理。

**`rank` / `depth` / `product`**：

- [include/cute/int_tuple.hpp:73-89](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/int_tuple.hpp#L73-L89) —— `rank`：元组返回元素个数，整数返回 1。
- [include/cute/int_tuple.hpp:193-209](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/int_tuple.hpp#L193-L209) —— `depth`：元组返回 `1 + max(各子元素 depth)`，整数返回 0。
- [include/cute/int_tuple.hpp:218-243](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/int_tuple.hpp#L218-L243) —— `product`：递归地把各模式乘起来。

**Layout 上的 `rank` / `size` / `depth`**（转发到 Shape）：

- [include/cute/layout.hpp:612-628](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/layout.hpp#L612-L628) —— `rank(layout)`、`size(layout)`、`depth(layout)` 都是对 `layout.shape()` 调用对应的 IntTuple 函数。

**静态整数字面量**（让嵌套 shape 写起来不啰嗦）：

- [include/cute/numeric/integral_constant.hpp:147](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/numeric/integral_constant.hpp#L147) —— `using _4 = Int<4>;`。CuTe 预定义了一大批 `_1 _2 _4 _8 …`，于是 `make_shape(_4,_4)` 比 `make_shape(Int<4>{},Int<4>{})` 简洁得多。

#### 4.2.4 代码实践

**实践目标**：用源码注释验证 `rank` / `depth` / `product` 在三种 IntTuple 上的取值。

**操作步骤**：

1. 打开 [int_tuple.hpp:73-89](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/int_tuple.hpp#L73-L89)（`rank`）和 [int_tuple.hpp:193-209](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/int_tuple.hpp#L193-L209)（`depth`）。
2. 对 `(4,4)`：`rank` 走「元组→元素个数」分支得 2；`depth` 走 `1 + max(depth(4),depth(4)) = 1+0 = 1`。
3. 对 `((2,2),(4,4))`：`rank` 得 2（最外层两个元素）；`depth` 得 `1 + max(depth((2,2)), depth((4,4))) = 1 + 1 = 2`。

**需要观察的现象**：`rank` 只看最外层；`depth` 会向下递归取最大值。

**预期结果**：与 4.2.1 表格一致。具体数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`((2,2),(2,2,2))` 的 `rank`、`depth`、`product` 各是多少？

> **答案**：`rank=2`（最外层两个元组），`depth=2`，`product=2·2·2·2·2=32`。

**练习 2**：为什么 CuTe 要允许 Shape 嵌套，而不是强制全部拍平成 `(32)`？

> **答案**：嵌套用来表达「逻辑分组」——比如哪几个维度属于同一条 MMA 指令、哪几个维度属于线程划分。拍平后虽然 `product` 和下标函数不变，但丢失了分组语义，后续做 `composition` / `partition`（按线程、按指令切分）时就无法对齐。

---

### 4.3 Layout 的代数运算：composition / complement / coalesce

#### 4.3.1 概念说明

把 Layout 看成函数后，函数能做的事它都能做，而且**结果还是 Layout**。这就是「布局代数」。最常用的三种运算：

**① 复合 composition：`L = L1 ∘ L2`**

\[
L(c) = L_1\big(L_2(c)\big).
\]

物理含义：先用 `L2` 把「新坐标」映射到「中间下标」，再用 `L1` 把「中间下标」映射到「最终下标」。这是 CuTe 最强大的工具——**用它可以把一个粗粒度布局「贴」到细粒度布局上**。例如把一个「线程布局」（哪个线程负责哪个元素）复合到「数据布局」上，就得到「每个线程实际访问的下标」。

**② 补 complement：`complement(L, M)`**

给定一个布局 `L` 和目标大小 `M`，返回一个「补布局」`C`，使得 `C` 覆盖 `L` 没覆盖到的那些「等距空位」，且 `size(C) · size(filter(L)) ≈ M`。直觉：`L` 描述了「块内」的相对位置，`complement` 给出「块与块之间」的步长。它是分块运算的另一半。

**③ 合并 coalesce / 过滤 filter**

- `coalesce(L)`：把能合并的模式合并掉，得到一个 depth≤1 的等价布局。例如 `(2,2):(1,2)` 合并成 `(4):(1)`（因为第二维步长 2 = 第一维大小 2，二者连续）。它**不改变「坐标→下标」的函数**（在定义域内），只简化表示。
- `filter(L)`：丢掉步长为 0 或大小为 1 的「无效模式」。

> 这三种运算是 `zipped_divide`（下一节的主角）的「原料」：分块 = `composition(原布局, make_layout(tiler, complement(tiler)))`。

#### 4.3.2 核心流程

分块的数学本质，用一个公式概括（即 `logical_divide` 的定义）：

\[
\texttt{logical\_divide}(L, T) \;=\; L \circ \big(T,\ \texttt{complement}(T, \texttt{size}(L))\big).
\]

- 左半 `T`：tile **内部**的布局（小块里的相对位置）。
- 右半 `complement(T, size(L))`：tile **网格**的布局（第几个小块、它的基准下标）。

复合后得到一个**两模式**的布局：`((tile 内部), (tile 网格))`。`zipped_divide` 只是在此基础上把这两个模式「拉到最外层」方便索引。

举个可手算的例子：`L=(4,4):(1,4)`，`T=(2,2):(1,2)`（即 tile 形状 `(2,2)` 的列主序紧凑布局）。

1. `coalesce(L) = (16):(1)`，故 `size(L)=16`。
2. `complement(T, 16)`：`T` 的 filter 形式是 `(4):(1)`（`(2,2):(1,2)` 合并），其补为 `(4):(4)`（大小 4，步长 4）。
3. `logical_divide = L ∘ (T, (4):(4))`。
   - 左模式 `L ∘ T`：`T(i,j)=i+2j∈{0,1,2,3}`，`L` 在 [0,4) 上是恒等，故结果 `(2,2):(1,2)`。
   - 右模式 `L ∘ (4):(4)`：`(4):(4)` 给 `0,4,8,12`，`L` 映射回 `0,4,8,12`，故结果 `(4):(4)`。
4. 所以 **`zipped_divide((4,4):(1,4), (2,2)) = ((2,2):(1,2), (4):(4))`**。

含义：分成 4 个 tile，每个 tile 内部是 `(2,2):(1,2)`（4 个连续下标 0,1,2,3），第 g 个 tile 的基准下标是 `4g`（g=0,1,2,3）。

> 注意一个反直觉的点：我们说的「2×2 分块」在这里**不是**空间上的 2×2 小方块网格！因为列主序 `(4,4):(1,4)` 的连续维大小 4 恰好等于 tile 连续段长，`complement` 把网格合并成了秩 1 的 `(4)`。于是 4 个 tile 各自覆盖 4 个**连续全局下标**：tile0={0,1,2,3}、tile1={4,5,6,7}、tile2={8,9,10,11}、tile3={12,13,14,15}。这正是 CuTe「合并一切可合并模式」哲学的体现——它追求最简表示，而非保留你脑中的几何形状。

#### 4.3.3 源码精读

**composition**：

- [include/cute/layout.hpp:1020-1024](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/layout.hpp#L1020-L1024) —— 复合的数学契约：`result(c) = lhs(rhs(c))`。
- [include/cute/layout.hpp:1132-1141](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/layout.hpp#L1132-L1141) —— `composition` 入口：先把 `lhs` 按 `rhs` 的 coprofile 做 `coalesce_x`，再进入 `composition_impl`。`Layout` 类还提供了便捷方法 `compose`（[layout.hpp:189-201](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/layout.hpp#L189-L201)）。
- [include/cute/layout.hpp:1036-1040](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/layout.hpp#L1036-L1040) —— `composition_impl` 的右分配律：当 RHS 是元组时，对每个模式独立复合（这就是上一节「左右两模式分别复合」的实现）。

**complement**：

- [include/cute/layout.hpp:1163-1172](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/layout.hpp#L1163-L1172) —— 补的后置条件：`size(C) ≥ cosize_hi / size(filter(L))`，且 `C(i)` 单调、避开 `L` 的像。
- [include/cute/layout.hpp:1231-1247](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/layout.hpp#L1231-L1247) —— `complement(layout, cotarget)` 入口：先 `filter`（拍平去废），再调 `detail::complement`。

**coalesce / filter**：

- [include/cute/layout.hpp:859-874](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/layout.hpp#L859-L874) —— `coalesce` 的契约与实现：保证 `size` 不变、`depth≤1`、且在定义域内函数不变；核心是 [layout.hpp:781-814](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/layout.hpp#L781-L814) 的 `bw_coalesce`（用形状×步长是否等于下一维步长来判断能否合并）。
- [include/cute/layout.hpp:933-939](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/layout.hpp#L933-L939) —— `filter`：`coalesce(filter_zeros(layout))`，丢掉 0 步长 / 1 大小的模式。

#### 4.3.4 代码实践

**实践目标**：在源码层面跟踪 `zipped_divide((4,4):(1,4), (2,2))` 的计算链，确认它确实等于 `((2,2):(1,2),(4):(4))`。

**操作步骤**（源码阅读型）：

1. 从 [layout.hpp:1610-1614](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/layout.hpp#L1610-L1614)（`zipped_divide`）进入，看到它调用 `tile_unzip(logical_divide(...), tiler)`。
2. 读 [layout.hpp:1555-1563](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/layout.hpp#L1555-L1563)（`logical_divide`）：`composition(layout, make_layout(tiler, complement(tiler, shape(coalesce(layout)))))`。
3. 按 4.3.2 的步骤手算 `complement` 与两模式复合的结果。

**需要观察的现象**：分块不是「新发明」的操作，而是 `composition + complement` 的组合；`coalesce` 在中途把网格模式合并成了 `(4)`。

**预期结果**：手算结果为 `((2,2):(1,2),(4):(4))`，与 4.3.2 一致。运行确认**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`coalesce((2,2,4):(1,2,8))` 的结果是什么？

> **答案**：`(4,4):(1,8)`。第一、二模式：`2·1==2`（下一维步长），合并成 `(4):(1)`；第三模式步长 8 ≠ 4·1，无法继续合并，保留为 `(4):(8)`。

**练习 2**：用一句话解释 `composition(L1, L2)` 为什么「结果仍是 Layout」而不是别的数据结构。

> **答案**：因为两个「坐标→下标」函数的复合仍是「坐标→下标」函数，而 Layout 恰好就是这类函数的有限表示；CuTe 的 `composition_impl` 会重新推导出一组合法的 (Shape, Stride) 来精确表示这个复合函数。

---

### 4.4 分块（tile）与分区（partition）：zipped_divide 与 local_tile

#### 4.4.1 概念说明

4.3 算出了 `zipped_divide` 的结果。这一节讲怎么**用它**。CuTe 提供一族命名清晰的分块函数，它们都建立在 `logical_divide` 之上，区别只是「结果怎么排列」：

| 函数 | 结果形态 | 典型用途 |
| --- | --- | --- |
| `logical_divide(L, T)` | 两模式，但保持原分组 | 代数中间表示 |
| `zipped_divide(L, T)` | `((tile内部), (网格))`，两模式拉到外层 | 最常用，便于用坐标取 tile |
| `tiled_divide(L, T)` | `((tile内部), 网格各维...)`，网格拆开 | 比 zipped 更平 |
| `flat_divide(L, T)` | `(tile内部各维..., 网格各维...)`，全平 | 完全扁平 |

对于 4.3 的例子，`zipped_divide((4,4):(1,4),(2,2)) = ((2,2):(1,2),(4):(4))`。第一个模式是「tile 内部布局」`(2,2):(1,2)`，第二个模式是「网格布局」`(4):(4)`。

**索引一个 tile**：给第二个模式（网格）传一个具体坐标 `g`，给第一个模式传占位符 `_`（保留整个 tile），就「切出」第 g 个 tile。占位符 `_` 的语义是「这一维全保留」——它在 [underscore.hpp](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/underscore.hpp) 里定义，`Layout::operator()` 一旦发现坐标含 `_` 就走 `slice` 分支返回子布局（见 [layout.hpp:168-170](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/layout.hpp#L168-L170)）。

**从 Layout 到 Tensor：`local_tile`**。上面这些都是 **Layout** 上的纯函数。但在真实内核里，我们分块的是「带数据的张量」。CuTe 把这个常用模式封装成 **`local_tile(tensor, tiler, coord)`**：它先用 `zipped_divide` 切分张量的布局，再用 `coord` 选出某个 tile，返回那个 tile 对应的小张量。下一讲 [u2-l2](u2-l2-cute-tensor.md) 会正式讲 Tensor；这里你只需记住：**`local_tile` 的内核就是 `zipped_divide`**，本节学的 Layout 分块就是它的全部原理。

#### 4.4.2 核心流程

`local_tile` 的执行链（从下往上看调用关系）：

```
local_tile(tensor, tiler, coord)            // 用户接口，作用在 Tensor 上
   └─> inner_partition(tensor, tiler, coord)
         └─> zipped_divide(tensor, tiler)   // 得到 ((tile内部),(网格))
               └─> logical_divide ─> composition + complement   （4.3 的全部代数）
         然后用 coord 切第二个模式（网格），保留第一个模式（tile）
```

切完之后，第 g 个 tile 是一个 `(2,2)` 的小张量；访问它的第 k 个元素就得到对应的**全局下标**（如果这个张量的「数据」恰好存的是下标本身）。

#### 4.4.3 源码精读

**Layout 层的分块函数**：

- [include/cute/layout.hpp:1555-1563](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/layout.hpp#L1555-L1563) —— `logical_divide`：分块的代数定义 `composition(layout, make_layout(tiler, complement(tiler, shape(coalesce(layout)))))`。
- [include/cute/layout.hpp:1606-1614](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/layout.hpp#L1606-L1614) —— `zipped_divide`：`tile_unzip(logical_divide(...))`，把结果整理成 `((tile内部),(网格))`。
- [include/cute/layout.hpp:1617-1628](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/layout.hpp#L1617-L1628) —— `tiled_divide`：在 `zipped_divide` 基础上把网格模式拆开（用 `result(_, repeat<R1>(_))` 切片）。
- [include/cute/layout.hpp:1541-1549](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/layout.hpp#L1541-L1549) —— `tile_unzip`：按 tiler 的轮廓把 `logical_divide` 的两两模式「拉到外层」。
- `Layout` 类还提供便捷方法 `tile(...)`，直接转调 `tiled_divide`（[layout.hpp:221-233](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/layout.hpp#L221-L233)）。

**切片与占位符**：

- [include/cute/layout.hpp:687-703](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/layout.hpp#L687-L703) —— `slice`（按 `_` 保留、按整数取定）与 `slice_and_offset`（同时返回子布局和基准偏移）。
- [include/cute/underscore.hpp:43](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/underscore.hpp#L43) —— `CUTE_INLINE_CONSTANT Underscore _;`，全局占位符对象。

**Tensor 层的 `local_tile`**：

- [include/cute/tensor_impl.hpp:1029-1044](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/tensor_impl.hpp#L1029-L1044) —— `local_tile(tensor, tiler, coord)` 的定义与典型用法注释（CTA 级取 tile）。
- [include/cute/tensor_impl.hpp:984-1000](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/tensor_impl.hpp#L984-L1000) —— `inner_partition`：[第 988 行](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/tensor_impl.hpp#L988) 调用 `zipped_divide`，随后用 `coord` 切「网格」模式、保留「tile」模式——这就是 `local_tile` 的全部内核。

#### 4.4.4 代码实践

**实践目标**：用 `zipped_divide` 把 `(4,4):(1,4)` 做「2×2 分块」，打印每个 tile 的局部坐标到全局下标的映射（这是第 5 节综合实践的 Layout 版预热）。

**操作步骤**（源码阅读型，可用第 5 节的程序运行确认）：

1. 构造 `L = (4,4):(1,4)`，`tiler = make_shape(_2,_2)`。
2. `auto z = zipped_divide(L, tiler);` 期望打印出 `((2,2):(1,2),(4):(4))`。
3. 对网格坐标 g=0..3、tile 内坐标 (i,j)∈{0,1}²，计算 `z(make_coord(i,j), g)` = `i + 2j + 4g`。

**需要观察的现象**：4 个 tile 各覆盖 4 个**连续**全局下标（tile0→0..3，tile1→4..7，…），印证 4.3.2 关于「complement 把网格合并成秩 1」的结论。

**预期结果**：

| tile g | 全局下标（按 tile 内 (i,j) 顺序） |
| --- | --- |
| 0 | 0, 1, 2, 3 |
| 1 | 4, 5, 6, 7 |
| 2 | 8, 9, 10, 11 |
| 3 | 12, 13, 14, 15 |

运行确认**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`local_tile` 与 `zipped_divide` 是什么关系？

> **答案**：`local_tile` 是作用在 **Tensor** 上的便捷封装；它内部第一步就是 `zipped_divide(tensor, tiler)`，再用 `coord` 切出指定 tile（见 [tensor_impl.hpp:988](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cute/tensor_impl.hpp#L988)）。所以本节学的 Layout 分块就是 `local_tile` 的全部原理。

**练习 2**：为什么 `zipped_divide` 结果的第一个模式（tile 内部）总是「`tiler` 的形状」？

> **答案**：因为 `logical_divide = L ∘ (tiler, complement(...))`，复合的左模式就是 `tiler` 本身（经 `L` 限制后），它决定了「一个 tile 里有几个元素、怎么排布」。`size(tiler)` 即每个 tile 的元素数。

---

## 5. 综合实践

把本讲串起来：构造 `(4,4):(1,4)` 的布局，**用 `cute::local_tile` 对它做 2×2 分块，打印每个 tile 的局部坐标到全局下标的映射**。我们用一个「数据 = 下标本身」的小张量，让分块结果一目了然。

> 说明：`local_tile` 作用在 **Tensor** 上（Tensor 是下一讲 [u2-l2](u2-l2-cute-tensor.md) 的主角）。这里我们只用最简形式 `make_tensor(指针, 布局)` 把布局包成张量，目的是调用 `local_tile`——它的原理你已在 4.4 完全掌握。

下面这段是**示例代码**（非项目原有代码），只含 host 逻辑，无需 GPU：

```cpp
// 文件名建议：u2l1_layout_tiling.cu
#include <cute/tensor.hpp>          // 含 layout.hpp + local_tile + make_tensor
#include <cstdio>

using namespace cute;

int main() {
  // 16 个元素，令 data[k] = k，这样「值」就是「全局下标」
  int data[16];
  for (int k = 0; k < 16; ++k) data[k] = k;

  // (4,4):(1,4) 列主序布局，并包成 host 张量
  auto layout  = make_layout(make_shape(_4, _4), make_stride(_1, _4));
  auto gtensor = make_tensor(data, layout);

  auto tiler = make_shape(_2, _2);   // 「2x2 分块」的 tiler

  printf("global layout: "); print(layout); printf("\n\n");

  // local_tile 内部 = zipped_divide(gtensor, tiler)，再按 g 切网格、保留 tile
  // 因为 complement 把网格合并成秩-1 的 (4)，所以用「扁平」网格坐标 g 遍历 4 个 tile
  for (int g = 0; g < 4; ++g) {
    auto tile = local_tile(gtensor, tiler, g);   // 第 g 个 tile，形状 (2,2)
    printf("tile %d  layout: ", g); print(tile.layout());
    printf("   全局下标: ");
    for (int k = 0; k < int(size(tile)); ++k) printf("%d ", int(tile(k)));
    printf("\n");
  }
  return 0;
}
```

**操作步骤**：

1. 把上面代码保存为 `u2l1_layout_tiling.cu`（放在仓库任意位置均可）。
2. 编译（CUTLASS 是 CUDA 项目，用 nvcc；本程序纯 host，不需要 GPU）：

   ```bash
   nvcc -std=c++17 -I include u2l1_layout_tiling.cu -o u2l1_layout_tiling
   ./u2l1_layout_tiling
   ```

   若本机无 CUDA 工具链，可尝试用支持 C++17 的编译器配合 `-I include` 直接当 host 代码编译；无法编译时标记**待本地验证**。
3. 阅读输出，对照 4.4.4 的预期表格。

**需要观察的现象**：

- `global layout: (4,4):(1,4)`。
- 每个 tile 的 `layout` 都是 `(2,2):(1,2)`（切片只固定网格坐标，保留 tile 内部步长）。
- 每个 tile 的 4 个「全局下标」分别是 `4g, 4g+1, 4g+2, 4g+3`。

**预期结果**：

```
global layout: (4,4):(1,4)

tile 0  layout: (2,2):(1,2)   全局下标: 0 1 2 3
tile 1  layout: (2,2):(1,2)   全局下标: 4 5 6 7
tile 2  layout: (2,2):(1,2)   全局下标: 8 9 10 11
tile 3  layout: (2,2):(1,2)   全局下标: 12 13 14 15
```

（确切字符串格式以本地 `print` 输出为准；下标集合应与上表完全一致。）

**延伸思考**：把 `layout` 改成行主序 `(4,4):(4,1)`，重跑。你会发现 tile 内部布局与全局下标分布都变了——体会「Shape/Stride 改变 → 分块结果改变」，这正是 Layout 代数可组合性的力量。

## 6. 本讲小结

- **Layout = (Shape, Stride)**：一个从逻辑坐标到线性下标的纯函数，**不持有数据**。`L(i,j)=i·d₀+j·d₁`。
- **`crd2idx`** 是坐标→下标的核心，支持「整数×整数」「整数×元组（divmod）」「元组×元组（逐维）」三种递归形态；传扁平整数会自动 divmod 成多维坐标。
- **IntTuple 可嵌套**：`rank` 看最外层、`depth` 看嵌套层数、`product` 看总元素数；嵌套表达「逻辑分组」，不影响下标函数本身。
- **布局代数**让 Layout 可组合：`composition`（复合 \(L₁∘L₂\)）、`complement`（补，给出块间步长）、`coalesce`/`filter`（简化表示且不改变函数）。
- **分块** `zipped_divide(L,T) = ((tile内部),(网格))`，本质是 `L ∘ (T, complement(T,size(L)))`；`local_tile` 是它在 Tensor 层的封装，内部第一步就是 `zipped_divide`。
- **反直觉但重要**：`(4,4):(1,4)` 用 `(2,2)` 分块会得到 4 个「连续下标段」的 tile（网格被 `complement` 合并成秩 1），而非空间上的 2×2 小方块网格——CuTe 总是追求最简表示。

## 7. 下一步学习建议

- 下一讲 [u2-l2 CuTe Tensor 与引擎](u2-l2-cute-tensor.md)：把本讲的 Layout 接上数据指针，正式学习 `Tensor = (Engine, Layout)`、`make_tensor`、smem/rmem/gmem 不同内存空间的张量。本讲综合实践里偷用的 `make_tensor` 会在那里讲透。
- 想巩固 Layout 代数，可继续读 [u2-l3 CuTe 算法：copy 与 gemm](u2-l3-cute-algorithms.md)，看 `cute::copy`/`cute::gemm` 如何**完全通过 Layout** 来决定数据搬运与乘加的访问模式。
- 进阶阅读：`include/cute/layout.hpp` 里 `right_inverse` / `left_inverse`（布局的左/右逆）、`upcast` / `downcast`（按因子重塑布局），它们在精度转换与 TMA 描述符构造中大量使用。
- 官方社区资源：NVIDIA 的 *A CuTe Layout and Algebra Tutorial*（GTC talk 与配套 notebook）是用具体例子直觉化理解 complement / zipped_divide 的最佳补充材料，与本讲源码对照阅读效果最好。
