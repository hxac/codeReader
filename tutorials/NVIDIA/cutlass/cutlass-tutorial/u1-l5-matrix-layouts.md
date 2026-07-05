# 矩阵布局基础

## 1. 本讲目标

CUTLASS 要算矩阵乘法，但「矩阵」在内存里只是一条一维的字节流。同一段内存，可以被解读成「按行排」或「按列排」，乘法内核访问元素的方式完全不同。本讲解决的就是这个「逻辑坐标 → 物理地址」的翻译问题。

学完本讲，你应当能够：

- 说出 CUTLASS 2.x 中 `layout::RowMajor` / `layout::ColumnMajor` 这类 **layout tag** 到底是什么（提示：它们是「函数对象类」，不是枚举）。
- 解释 **leading dimension（ldm）/ stride** 的含义，并能手算 `packed` 紧密排布时的 ldm。
- 读懂 **pitch-linear（线性跨步）坐标** 的 `(contiguous, strided)` 约定，明白为什么 Tensor Core 内部更爱用它。
- 用 `TensorRef`（指针 + 布局）和 `TensorView`（指针 + 布局 + 范围）包装一段裸内存，并按逻辑坐标安全地读写元素。

本讲是理解 CUTLASS 2.x `device::Gemm`（下一讲 u1-l6）参数传递的必要前置——`Gemm` 的 args 对象里到处都是 `TensorRef` 和 `ldm`。

## 2. 前置知识

- **行主序 / 列主序**：行主序（row-major）把同一行的元素连续存放，列主序（column-major）把同一列的元素连续存放。C 语言的二维数组默认是行主序；Fortran、BLAS、cuBLAS 默认是列主序。
- **leading dimension（前导维 / ldm）**：相邻两条行（或列）在内存里隔了多少个元素。BLAS 文档里常写成 `lda`、`ldb`、`ldc`。
- **C++ 函数对象（functor）**：重载了 `operator()` 的类，对象可以像函数一样被「调用」。CUTLASS 的布局就是 functor：`layout(coord)` 返回该坐标在内存里的偏移。
- 承接 [u1-l4](u1-l4-numeric-types.md)：`sizeof_bits<T>`、子字节类型（如 `int4b_t`）等概念会在 `TensorRef` 的「引用类型」里再次出现。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `include/cutlass/layout/matrix.h` | 定义矩阵的布局映射函数：`RowMajor`、`ColumnMajor`、交错布局、块线性布局等。本讲主角。 |
| `include/cutlass/layout/pitch_linear.h` | 定义 `PitchLinear` 布局——硬件最自然的二维视图。 |
| `include/cutlass/pitch_linear_coord.h` | 定义 `PitchLinearShape` 与 `PitchLinearCoord(contiguous, strided)` 坐标。 |
| `include/cutlass/matrix_coord.h` | 定义 `MatrixCoord(row, column)`，矩阵的二维逻辑坐标。 |
| `include/cutlass/layout/tensor.h` | 定义 4-D/5-D 张量布局（NHWC 等），可投影到 pitch-linear。 |
| `include/cutlass/tensor_ref.h` | 定义 `TensorRef`（指针 + 布局）与布局接口规范 `IdentityTensorLayout`。 |
| `include/cutlass/tensor_view.h` | 定义 `TensorView`（在 `TensorRef` 上增加范围 `extent`）。 |

记住一个判断（承接 [u1-l3](u1-l3-directory-structure.md)）：`cutlass::layout::*` 命名空间下的类都是「纯函数」式的映射器，它们自己不持有数据，只持有「步长」这种描述几何的信息。

## 4. 核心概念与源码讲解

### 4.1 layout tag 概念

#### 4.1.1 概念说明

很多人第一次看到 `cutlass::gemm::device::Gemm<..., layout::RowMajor, layout::ColumnMajor, ...>` 时，会以为 `RowMajor` 只是一个「说明存放大方向」的枚举标签。其实不是——CUTLASS 把布局做成了**函数对象类**。

一个布局类的核心职责只有一个：**给定一个逻辑坐标，返回它在内存中的线性偏移**。即它实现了一个函数：

\[
\text{offset} : \text{逻辑坐标} \longrightarrow \mathbb{Z}_{\ge 0}
\]

凡是要被 `TensorRef` 使用的布局类，都必须提供一组统一的公共接口（秩、步长秩、`Index`/`LongIndex` 类型、`TensorCoord`/`Stride` 类型、`operator()`、`stride()`、`capacity()`）。这套「接口契约」在 `tensor_ref.h` 里用一个示例类 `IdentityTensorLayout<Rank>` 写明，注释也明确指出「Layout functions must implement all members in the public interface of `IdentityTensorLayout<>`」。

#### 4.1.2 核心流程

以二维矩阵为例，逻辑坐标是 `(row, column)`。两种经典布局的映射规则：

- 列主序（column-major）：同一列连续，所以 `row` 是「连续维」，`column` 是「跨步维」。

\[
\text{offset}(r, c) = r + c \cdot \text{ldm}
\]

- 行主序（row-major）：同一行连续，所以 `column` 是「连续维」，`row` 是「跨步维」。

\[
\text{offset}(r, c) = r \cdot \text{ldm} + c
\]

注意两式的对偶性：它们就是「连续维 + 跨步维 × ldm」的同一条公式，只是把哪一维当成连续维交换了一下。这正是下一节 pitch-linear 的雏形。

#### 4.1.3 源码精读

`RowMajor` 的映射函数，把坐标 `(row, column)` 映射为 `row*ldm + column`：

- [include/cutlass/layout/matrix.h:108-110](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/layout/matrix.h#L108-L110) —— `RowMajor::operator()`，行主序偏移公式。

`ColumnMajor` 的映射函数，把坐标 `(row, column)` 映射为 `column*ldm + row`：

- [include/cutlass/layout/matrix.h:201-203](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/layout/matrix.h#L201-L203) —— `ColumnMajor::operator()`，列主序偏移公式。

注意 `RowMajor` / `ColumnMajor` 的 `kStrideRank = 1`（[matrix.h:64](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/layout/matrix.h#L64)、[matrix.h:155](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/layout/matrix.h#L155)），即它们的步长向量只有一个分量——就是 leading dimension。类型别名 `Stride = Coord<1, LongIndex>`（[matrix.h:76](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/layout/matrix.h#L76)）。

「布局契约」由这个示例类规定，任何布局类都得提供这些成员：

- [include/cutlass/tensor_ref.h:50-113](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/tensor_ref.h#L50-L113) —— `IdentityTensorLayout<Rank>`，定义了布局类必须满足的公共接口。

仓库里还有运行期才决定行/列主序的 `ContiguousMatrix`（带一个 `Matrix layout_` 枚举成员），以及编译期表达的 `GeneralMatrix`，它们都遵守同一套接口。`Matrix` 枚举本身定义在：

- [include/cutlass/layout/matrix.h:456-459](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/layout/matrix.h#L456-L459) —— `enum class Matrix { kColumnMajor, kRowMajor }`。

#### 4.1.4 代码实践

**目标**：亲手验证「布局就是把坐标翻译成偏移」这件事。

**步骤**：

1. 打开 [include/cutlass/layout/matrix.h:91-103](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/layout/matrix.h#L91-L103)，看 `RowMajor` 的构造函数与 `packed` 静态方法。
2. 在脑海里走一遍：对一个紧密排布的 3 行 5 列矩阵，`RowMajor::packed({3,5})` 返回的 ldm 是多少？（答案：`extent.column() = 5`，见 [matrix.h:101-103](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/layout/matrix.h#L101-L103)）。
3. 用纸笔（或计算器）算 `(row=2, col=3)` 在该布局下的偏移：`2*5 + 3 = 13`。
4. 再换成 `ColumnMajor::packed({3,5})`（[matrix.h:194-196](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/layout/matrix.h#L194-L196)），ldm = `extent.row() = 3`，同一坐标偏移 = `3*3 + 2 = 11`。

**需要观察的现象**：同一个逻辑坐标 `(2,3)`，行主序和列主序算出的物理偏移不同（13 vs 11）。这正是「同一段内存、不同解读」的本质。

**预期结果**：行主序偏移 13，列主序偏移 11。如本地实际调用 `layout({2,3})` 结果与此不符，请核对 `packed` 返回的 ldm 是否理解正确。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `RowMajor` / `ColumnMajor` 的 `kStrideRank` 是 1 而不是 2？

**答案**：因为在这两种「规范」布局里，必定有一个维度的步长是 1（连续维），只需要用一个 `ldm` 来描述另一个（跨步维）的步长就够了。等学到 `AffineRank2ColumnMajor`（[matrix.h:730-834](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/layout/matrix.h#L730-L834)）才会看到 `kStrideRank = 2`——那是两个维度都不连续、各有独立步长的情形。

**练习 2**：`LayoutTranspose<RowMajor>::type` 是什么？在哪里定义？

**答案**：是 `ColumnMajor`，定义在 [include/cutlass/layout/matrix.h:1334-1344](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/layout/matrix.h#L1334-L1344)。行、列主序互为转置。

---

### 4.2 leading dimension 与 stride

#### 4.2.1 概念说明

**leading dimension（ldm）**就是「沿着非连续方向，跨到下一条线要跳几个元素」。对列主序矩阵，连续方向是「向下换行」，非连续方向是「向右换列」，所以 ldm 表示相邻两列起点之间的元素数——通常等于行数（紧密排布时），但也可能更大（带 padding 时）。

**stride（步长）**是更一般的概念：任意一个维度上相邻两个逻辑元素在内存里相隔的元素数。对 `RowMajor`/`ColumnMajor` 而言，连续维的 stride 恒为 1，跨步维的 stride 就是 ldm，所以 `kStrideRank = 1`，只存 ldm 这一个数。

#### 4.2.2 核心流程

紧密排布（packed）时 ldm 怎么来：

| 布局 | 连续维 | 跨步维 | `packed` 返回的 ldm | 容量 `capacity(extent)` |
| --- | --- | --- | --- | --- |
| `ColumnMajor` | row | column | `extent.row()` | `extent.column() * ldm` |
| `RowMajor` | column | row | `extent.column()` | `extent.row() * ldm` |

`capacity()` 返回「存放这个范围至少需要的连续元素数」，对带 padding 的 ldm 也能正确计算（行数 × ldm 或列数 × ldm）。这跟 BLAS 里「分配 buffer 至少 `ldm × 另一维` 个元素」是一回事。

#### 4.2.3 源码精读

`RowMajor::packed` 与 `capacity`：

- [include/cutlass/layout/matrix.h:99-103](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/layout/matrix.h#L99-L103) —— 紧密排布时 ldm = 列数。
- [include/cutlass/layout/matrix.h:142-146](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/layout/matrix.h#L142-L146) —— `capacity = row * ldm`。

`ColumnMajor::packed` 与 `capacity`：

- [include/cutlass/layout/matrix.h:192-196](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/layout/matrix.h#L192-L196) —— 紧密排布时 ldm = 行数。
- [include/cutlass/layout/matrix.h:235-239](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/layout/matrix.h#L235-L239) —— `capacity = column * ldm`。

`TensorRef` 暴露 stride 的几个重载（注意它只是转发给内部 `layout_`）：

- [include/cutlass/tensor_ref.h:290-306](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/tensor_ref.h#L290-L306) —— `stride()` / `stride(dim)`，读取布局的步长。

#### 4.2.4 代码实践

**目标**：体会 ldm 可以大于「紧密值」，从而支持带 padding 的矩阵（例如为了对齐 16 字节边界）。

**步骤**：

1. 设想一个 3 行、3 列但 ldm = 4 的列主序矩阵。物理存储顺序为：第 0 列的 3 个元素 + 1 个 padding，再第 1 列……共需 `capacity = column * ldm = 3 * 4 = 12` 个元素。
2. 手算 `(row=2, col=2)` 的偏移：`2 + 2*4 = 10`。
3. 到 [include/cutlass/layout/matrix.h:201-203](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/layout/matrix.h#L201-L203) 对照公式，确认一致。

**需要观察的现象**：ldm 比「紧密值」大 1 时，同一逻辑坐标 `(2,2)` 的偏移从 8（紧密，ldm=3：`2 + 2*3`）变成 10（ldm=4）。说明 ldm 直接控制了内存跳步。

**预期结果**：偏移 10。这种「ldm > 紧密值」的能力，正是 cuBLAS 里 `lda` 可以大于 `M` 的原因——为了对齐和向量化。

#### 4.2.5 小练习与答案

**练习 1**：一个列主序 4×4 矩阵，希望每列起点都对齐到 8 个 float（32 字节）边界，ldm 至少应设为多少？

**答案**：至少 8。紧密值是 4，但每列要占 8 个 float 才能保证下一列起点也对齐到 32 字节，所以 ldm = 8，`capacity = 4 * 8 = 32` 个 float（其中一半是 padding）。

**练习 2**：`TensorRef_aligned(ref, alignment)` 这个工具函数（[tensor_ref.h:398-415](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/tensor_ref.h#L398-L415)）检查对齐时，除了指针地址，还检查了什么？

**答案**：还逐个检查了每条 stride 是否能被 `alignment` 整除。因为只有「指针对齐 + 每条 stride 也对齐」才能保证后续每个 tile 的起点都对齐，这对向量化加载至关重要。

---

### 4.3 pitch_linear 坐标

#### 4.3.1 概念说明

**pitch-linear（线性跨步）**是 CUTLASS 内部描述二维 tile 的「通用语」。它不再区分 row/column，而是用两个更贴近硬件的维度名：

- **contiguous（连续维）**：在内存里步长为 1 的那一维——「挨个排」。
- **strided（跨步维）**：每跨一步要跳 ldm 个元素的那一维——「隔 ldm 排」。

对应坐标 `PitchLinearCoord(contiguous, strided)`。于是统一的二维映射函数就是：

\[
\text{offset}(k, s) = k + s \cdot \text{ldm},\quad k=\text{contiguous},\ s=\text{strided}
\]

为什么 Tensor Core 代码爱用它？因为 GPU 共享内存里的 tile、MMA 指令的输入分片，本质上都是「一个维度连续、另一个维度按 ldm 跨步」的二维块。用 pitch-linear 抽象后，行/列主序只是「把谁映射成 contiguous」的两种特例，复用同一套 tile 拷贝/计算代码。

#### 4.3.2 核心流程

把规范布局投影到 pitch-linear：

- `ColumnMajor`：contiguous = row，strided = column → `offset = row + column*ldm`（与 [matrix.h:201-203](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/layout/matrix.h#L201-L203) 完全一致）。
- `RowMajor`：contiguous = column，strided = row → `offset = column + row*ldm`（与 [matrix.h:108-110](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/layout/matrix.h#L108-L110) 完全一致）。

更高维的张量布局（如卷积用的 NHWC）也提供 `operator()(PitchLinearCoord)` 重载，把 4-D 坐标先压成 pitch-linear 二维，再喂给同一套 tile 机制。

#### 4.3.3 源码精读

坐标与静态形状：

- [include/cutlass/pitch_linear_coord.h:44-52](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/pitch_linear_coord.h#L44-L52) —— `PitchLinearShape<Contiguous, Strided>`，编译期二维形状。
- [include/cutlass/pitch_linear_coord.h:57-114](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/pitch_linear_coord.h#L57-L114) —— `PitchLinearCoord`，提供 `.contiguous()` / `.strided()` 命名访问（`kContiguous=0`，`kStrided=1`）。

`PitchLinear` 布局本身就是上一节公式的直接实现：

- [include/cutlass/layout/pitch_linear.h:98-103](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/layout/pitch_linear.h#L98-L103) —— `offset = contiguous + strided * ldm`。
- [include/cutlass/layout/pitch_linear.h:92-96](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/layout/pitch_linear.h#L92-L96) —— `packed` 时 ldm = `extent.contiguous()`。

卷积张量布局如何投影到 pitch-linear（注意它多了一个 `operator()(PitchLinearCoord)` 重载）：

- [include/cutlass/layout/tensor.h:150-154](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/layout/tensor.h#L150-L154) —— `TensorNHWC` 把 pitch-linear 坐标映射为 `contiguous + strided * stride_n`，让 4-D 的卷积张量也能用同一套 tile 代码。

#### 4.3.4 代码实践

**目标**：确认「`ColumnMajor` 就是 pitch-linear 的一种特例」。

**步骤**：

1. 对照两段公式：
   - 列主序：`offset(r, c) = c*ldm + r`（[matrix.h:201-203](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/layout/matrix.h#L201-L203)）
   - pitch-linear：`offset(k, s) = k + s*ldm`（[pitch_linear.h:101-103](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/layout/pitch_linear.h#L101-L103)）
2. 代入 `k = r, s = c`，两者完全相同。
3. 同理验证 `RowMajor` 对应 `k = c, s = r`。

**需要观察的现象**：三种布局（行主序、列主序、纯 pitch-linear）共用同一条偏移公式，差别只在于「逻辑的 row/column 哪个挂到 contiguous、哪个挂到 strided」。

**预期结果**：代数上完全一致。这一节是「源码阅读型实践」，无需运行。

#### 4.3.5 小练习与答案

**练习**：`TensorNHWC` 的逻辑坐标是 4-D `(n, h, w, c)`，它的连续维是哪个字母？依据是什么？

**答案**：连续维是 `c`（channel）。依据是 [include/cutlass/layout/tensor.h:142-148](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/layout/tensor.h#L142-L148)：偏移公式里 `coord.c()` 的系数是 1（没有乘 stride），而 `n` 的系数是最大的 `stride_[2]`，所以 NHWC 的 channel 维在内存里连续——这也是它名字里把 C 放最后的原因。

---

### 4.4 TensorRef 与 TensorView

#### 4.4.1 概念说明

布局只管「坐标 → 偏移」，但还不涉及真实数据。把「一根指针 + 一个布局」拼起来，就是 **`TensorRef`**；再补上「范围 extent」，就是 **`TensorView`**。

- `TensorRef<Element, Layout>`：**指针 + 布局**。轻量、不持有所有权，知道怎么把坐标翻译成偏移，但不知道这片张量有多大。常用于「把矩阵 A 的起点和 ldm 传给 kernel」。
- `TensorView<Element, Layout>`：在 `TensorRef` 基础上**多记一个 `extent_`**（逻辑范围）。因为有了范围，它是个「完整的数学对象」，可以做越界判断 `contains()`、取子视图 `subview()`、算总容量 `capacity()`。

这俩都是「视图（view）」而非「容器」——它们不分配/释放内存，只借用外部指针。这点和 `std::span`、`std::string_view` 的设计哲学一致。

#### 4.4.2 核心流程

`TensorRef` 访问一个元素的链路：

```text
view.at({row, col})
   └─> offset({row, col})   // 调 layout_(coord)，返回线性偏移
          └─> data(offset)  // ptr_ + 偏移，返回引用
```

`TensorView` 在此基础上多了：

```text
view.contains({row, col})  // 用 extent_ 判断是否在范围内
view.subview(extent, origin) // 返回一个偏移过的子视图
```

关键点：`operator[]` 和 `at()` 等价，都走 `offset → data` 这条路；越界检查只在 `contains()` 里显式做，普通的 `[]` 不会自动检查（性能优先）。

#### 4.4.3 源码精读

`TensorRef` 的数据成员与构造（注释里就给了列主序/行主序的用法示例）：

- [include/cutlass/tensor_ref.h:117-145](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/tensor_ref.h#L117-L145) —— 注释示例：`TensorRef<float, layout::ColumnMajor> A(ptr, ldm)`。
- [include/cutlass/tensor_ref.h:199-225](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/tensor_ref.h#L199-L225) —— 两个私有成员 `ptr_`、`layout_`，以及构造函数。

访问与偏移（注意 `offset()` 直接调用 `layout_(coord)`，把映射委托给布局）：

- [include/cutlass/tensor_ref.h:314-330](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/tensor_ref.h#L314-L330) —— `offset()` / `at()` / `operator[]`，这是「坐标 → 元素」的核心路径。

子字节元素的特殊处理（承接 [u1-l4](u1-l4-numeric-types.md)）：当元素不足 8 位时，`at()` 不能返回普通引用，而要返回 `SubbyteReference`：

- [include/cutlass/tensor_ref.h:161-165](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/tensor_ref.h#L161-L165) —— `Reference` 类型按 `sizeof_bits` 分派。

便捷工厂函数：

- [include/cutlass/tensor_ref.h:383-386](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/tensor_ref.h#L383-L386) —— `make_TensorRef(ptr, layout)`。

`TensorView` 继承自 `TensorRef` 并增加 `extent_`：

- [include/cutlass/tensor_view.h:56-118](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/tensor_view.h#L56-L118) —— 类声明与私有成员 `extent_`。
- [include/cutlass/tensor_view.h:128-137](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/tensor_view.h#L128-L137) —— 构造函数 `(ptr, layout, extent)`。
- [include/cutlass/tensor_view.h:189-199](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/tensor_view.h#L189-L199) —— `contains()`，用 `extent_` 做范围判断。
- [include/cutlass/tensor_view.h:219-229](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/tensor_view.h#L219-L229) —— `subview()`，返回带偏移的子视图。
- [include/cutlass/tensor_view.h:287-293](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/tensor_view.h#L287-L293) —— `make_TensorView(ptr, layout, extent)`。

#### 4.4.4 代码实践

**目标**：用 `layout::ColumnMajor` 构造 `TensorRef` 指向一个 4×4 列主序矩阵，再用 `TensorView` 按逻辑 `(row, col)` 访问并打印，验证「列主序存储 + 逻辑坐标访问」能正确还原矩阵。

> 说明：下面的程序是**示例代码**（非仓库自带文件）。`layout`/`TensorRef`/`TensorView` 都是纯 C++ 模板、标注了 `CUTLASS_HOST_DEVICE`，不含 CUDA 内建函数，因此可以用普通主机编译器（`g++`/`clang++`）直接编译运行，无需 nvcc。

```cpp
// 示例代码：文件名 demo_layout.cpp
#include <iostream>
#include "cutlass/cutlass.h"
#include "cutlass/layout/matrix.h"
#include "cutlass/matrix_coord.h"
#include "cutlass/tensor_ref.h"
#include "cutlass/tensor_view.h"

int main() {
  constexpr int M = 4, N = 4;          // 4x4 矩阵
  float storage[M * N];

  // 以「列主序」写入：第 col 列的 M 个元素连续存放
  // 元素值取 row*10 + col，便于肉眼核对
  for (int col = 0; col < N; ++col)
    for (int row = 0; row < M; ++row)
      storage[col * M + row] = float(row * 10 + col);

  // 1) 布局：列主序，ldm = M（相邻两列间距 = 行数）
  cutlass::layout::ColumnMajor layout(M);

  // 2) TensorRef：指针 + 布局（不含范围）
  cutlass::TensorRef<float, cutlass::layout::ColumnMajor> ref(storage, layout);

  // 3) TensorView：再附加范围 extent，得到「完整」张量对象
  cutlass::TensorView<float, cutlass::layout::ColumnMajor> view(
      storage, layout, cutlass::MatrixCoord(M, N));

  // 4) 用逻辑坐标 (row, col) 读取——at() 内部走 offset()->data() 链路
  std::cout << "通过 TensorView 按逻辑坐标打印：\n";
  for (int row = 0; row < M; ++row) {
    for (int col = 0; col < N; ++col) {
      std::cout << view.at(cutlass::MatrixCoord(row, col)) << "\t";
    }
    std::cout << "\n";
  }
  return 0;
}
```

**操作步骤**：

1. 在仓库根目录把上面的内容存为 `demo_layout.cpp`（放在仓库外或临时目录均可，**不要**写进 `include/`）。
2. 编译（只需把 `include/` 加入头文件搜索路径，C++17）：

   ```bash
   g++ -std=c++17 -Iinclude demo_layout.cpp -o demo_layout
   ./demo_layout
   ```

**需要观察的现象**：尽管 `storage` 是按列连续写入的（`col * M + row`），但用 `TensorView` 以 `(row, col)` 逻辑坐标访问时，打印出来是一个规整的矩阵，且 `(row, col)` 处的值正好是 `row*10 + col`。这说明布局 functor 把逻辑坐标正确翻译回了物理偏移。

**预期结果**（由偏移公式 `row + col*M` 推得）：

```text
通过 TensorView 按逻辑坐标打印：
0       1       2       3
10      11      12      13
20      21      22      23
30      31      32      33
```

如果本地实际运行结果与此不符，请核对：写入顺序是否真的是 `col*M + row`（列主序）、以及构造 `ColumnMajor` 时 ldm 是否等于行数 `M`。**精确的编译/运行输出待本地验证。**

#### 4.4.5 小练习与答案

**练习 1**：把上面示例中的布局换成 `RowMajor`（保持 `storage` 仍是列主序写入不变），打印结果会变成什么样？为什么？

**答案**：会变成「错位」的矩阵——因为存储是列主序，却用行主序公式去解释，相当于对矩阵做了转置。打印的 `(row, col)` 实际取到的是原矩阵的 `(col, row)`，整张表会沿对角线翻转。这正好印证 `LayoutTranspose<RowMajor>::type = ColumnMajor`。

**练习 2**：`TensorView` 比 `TensorRef` 多了 `contains()` 和 `subview()`，但它俩都有 `at()` / `operator[]`。为什么 `TensorView` 不在 `operator[]` 里自动做越界检查？

**答案**：为了性能。GEMM 内核里 `operator[]` 处于最内层循环，每次访问都判断范围会拖慢计算。`TensorView` 把范围信息保留下来，需要安全检查时由调用方显式用 `contains()`；这与 `std::vector::operator[]` 不做边界检查、`at()` 才检查的设计哲学一致。

---

## 5. 综合实践

把本讲四个最小模块串起来：**布局是函数 → ldm 控制跳步 → pitch-linear 是通用语 → TensorRef/View 包装真实内存**。

任务：**用同一片 8×8 内存，分别以列主序和行主序两种视图访问，验证它们互为转置。**

要求：

1. 分配 `float buf[64]`，按下标 `i` 写入 `buf[i] = float(i)`（即物理内存里依次是 0,1,2,…,63）。
2. 构造 `TensorView<float, layout::ColumnMajor>`，`extent = {8,8}`，ldm = 8。按 `(row, col)` 打印这个矩阵。
3. 再构造 `TensorView<float, layout::RowMajor>`，指向**同一片** `buf`，`extent = {8,8}`，ldm = 8。同样按 `(row, col)` 打印。
4. 观察：两份打印互为转置（列主序视图第 `(r,c)` 个值，等于行主序视图第 `(c,r)` 个值）。
5. 进阶：用 `subview()`（[tensor_view.h:219-229](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/tensor_view.h#L219-L229)）从列主序视图里取出左上角 4×4 子块并打印，确认子块元素与完整矩阵对应位置一致。

提示：列主序视图 `offset(r,c) = r + c*8`，所以 `view_col.at({r,c}) = buf[r + 8c] = r + 8c`；行主序视图 `offset(r,c) = r*8 + c`，所以 `view_row.at({r,c}) = buf[8r + c] = 8r + c`。把 `view_col.at({c,r})` 代入得 `c + 8r = 8r + c = view_row.at({r,c})`，二者互为转置得证。这一步帮助你建立「同一物理内存、不同布局 = 不同逻辑矩阵」的直觉，正是下一讲 `device::Gemm` 同时接受 A/B/C 三种独立布局参数的基础。

## 6. 本讲小结

- **布局是函数对象类**：`layout::RowMajor` / `layout::ColumnMajor` 不是枚举标签，而是实现了 `operator()(coord) → offset` 的映射器，必须满足 `IdentityTensorLayout` 规定的接口契约。
- **leading dimension = 唯一的步长**：对规范布局而言 `kStrideRank = 1`，连续维步长恒为 1，跨步维步长就是 ldm；紧密排布时 `ColumnMajor` 的 ldm = 行数，`RowMajor` 的 ldm = 列数。
- **pitch-linear 是 CUTLASS 的通用二维语**：`(contiguous, strided)` + `offset = k + s*ldm`，行/列主序只是它的两种特例，卷积等高维布局也会投影到它上面复用 tile 代码。
- **`TensorRef` = 指针 + 布局**：轻量、用于把矩阵起点和 ldm 传给内核；**`TensorView` = `TensorRef` + extent**：完整数学对象，支持 `contains()` / `subview()` / `capacity()`。
- 两者都是「视图」而非「容器」，不管理内存；普通 `operator[]` 不做越界检查，性能优先。
- 子字节类型（如 `int4b_t`）下，`TensorRef::at()` 返回 `SubbyteReference` 而非普通引用（承接 [u1-l4](u1-l4-numeric-types.md)）。

## 7. 下一步学习建议

- **紧接着学 [u1-l6 第一个 GEMM：2.x device API](u1-l6-first-gemm.md)**：`device::Gemm` 的 args 对象里，A、B、C 三个矩阵就是用 `TensorRef<Element, Layout>` + `ldm` 传进去的，本讲的 `TensorRef` / `ColumnMajor` / `ldm` 会立刻用上。
- 想了解布局如何被 threadblock/warp 层的 tile 代码使用，可先扫一眼 `include/cutlass/gemm/threadblock/default_mma.h`（u2-l6 会精读），那里大量出现 `PitchLinearShape`。
- 若你对 3.x 的 `cute::Layout`（用 `(Shape, Stride)` 表达任意层级布局）感兴趣，可跳到 u2-l1，但建议先掌握本讲的「布局 = 映射函数」直觉，再去看 CuTe 的代数化表达会更顺。
