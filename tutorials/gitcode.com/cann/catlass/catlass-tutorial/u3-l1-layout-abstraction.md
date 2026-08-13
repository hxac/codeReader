# Layout 布局抽象

## 1. 本讲目标

本讲是「数据模型与类型系统」单元的第一讲。学完后你应当能够：

- 说出 CATLASS 的 Layout 到底「抽象」了什么，以及它为什么是后续所有 GM 偏移计算的地基。
- 区分 `RowMajor`、`ColumnMajor` 与 `nZ`/`zN`/`zZ`/`nN`/`L0C` 等「分形（fractal）」布局的物理排布含义，以及 `PaddingRowMajor` 这类重排布局的存在意义。
- 读懂 `MakeLayout<Element>(rows, cols)` 的构造过程，理解它为什么必须是模板、且依赖元素类型。
- 手算 `GetOffset(MatrixCoord)` 把一个逻辑坐标 `(row, col)` 映射到线性偏移（并分清「元素偏移」与「字节偏移」），与源码公式逐项对照。

## 2. 前置知识

本讲承接 u2-l2（四层组装范式）。在那里我们已经看到 `basic_matmul.cpp` 里这样一行：

```cpp
using LayoutA = layout::RowMajor;
LayoutA layoutA = LayoutA::template MakeLayout<ElementA>(m, k);
```

并且这串 `layoutA` 会一路传到 Kernel 层，用于在 SPMD 循环里把「第几个 C 基本块」换算成 GM 上的真实地址。本讲就回答：这个 `layoutA` 里装了什么、它是怎么造出来的、它怎么算偏移。

需要先理解两个朴素概念：

- **线性内存**：无论 GM、L1 还是 L0C，物理上都是一段一维字节序列。一个二维矩阵 \(M \times N\) 必须按某种规则「摊平」存进去。
- **逻辑坐标与物理排布**：算子代码里我们习惯用 `(row, col)` 这样的逻辑下标去取一个元素；但内存里这个元素到底落在第几个字节，取决于「排布规则（layout）」。Layout 就是把「逻辑坐标 → 线性偏移」这条映射显式化的对象。

> 一个关键直觉：**Layout 不持有数据，只持有「形状 + 步长（+ 原始形状）」这套映射参数**。真正的数据指针由 Host 通过 ACL 申请并保存在 `Arguments`/`Params` 里，Layout 只负责「给定坐标算偏移」。

还需要一组硬件常量（定义在 [include/catlass/catlass.hpp:26-33](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/catlass.hpp#L26-L33)），本讲会反复用到：

| 常量 | 值 | 含义 |
|---|---|---|
| `BYTE_PER_C0` | 32 字节 | 一个 C0 块的字节数（Cube 单元的最小数据块） |
| `C0_NUM_PER_FRACTAL` | 16 | 一个分形里沿某一方向的 C0 个数 |
| `BYTE_PER_FRACTAL` | 512 字节 | 一个分形的字节数 = `BYTE_PER_C0 * C0_NUM_PER_FRACTAL` |
| `BYTE_PER_BLK` | 32 字节 | UB/搬运对齐块字节数 |
| `STRIDE_LIMIT` | 65536 | 单步 stride 不能超过该值（Padding 布局的诱因之一） |

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [include/catlass/layout/matrix.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/layout/matrix.hpp) | **本讲主角**。定义全部矩阵布局类型：`RowMajor`/`ColumnMajor`（2 维）、`nZ`/`zN`/`zZ`/`nN`/`L0C`/`Weight4BitnZ`（4 维分形）、`PaddingRowMajor`/`PaddingColumnMajor`（重排）、以及卷积用的 `NDC1HWC0`/`KDC1KHKWN1N0C0`。每个类型都提供 `MakeLayout`/`GetOffset`/`shape`/`stride`/`Capacity` 接口。 |
| [include/catlass/layout/layout.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/layout/layout.hpp) | 一站式聚合头：`#include` 了 `matrix.hpp`/`vector.hpp`/`tensor.hpp`。样例里写 `#include "catlass/layout/layout.hpp"` 即可拿到全部布局。 |
| [include/catlass/matrix_coord.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/matrix_coord.hpp) | `MatrixCoord`：对 `Coord<2, uint32_t>` 的包装，提供具名 `.row()`/`.column()` 访问。`GetOffset` 的入参就是它。 |
| [include/catlass/coord.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/coord.hpp#L349-L395) | `Coord<RANK>` 与 `MakeCoord(...)` 构造助手；布局内部的 `shape_`/`stride_` 都是 `Coord`。 |
| [include/catlass/detail/alignment.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/detail/alignment.hpp) | `RoundUp`/`CeilDiv` 等对齐工具，`MakeLayout` 用它们把尺寸向上取整到分形倍数。 |
| [include/catlass/numeric_size.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/numeric_size.hpp) | `SizeOfBits<T>`、`BytesToBits`，用于「字节数 → 元素个数」的换算。 |
| [include/catlass/gemm/kernel/basic_matmul.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp) | Kernel 层调用 `MakeLayout` 构造 layout、并在 SPMD 循环里调用 `GetOffset` 算 GM 偏移，是 Layout 真正被「用起来」的地方。 |
| [examples/00_basic_matmul/basic_matmul.cpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp) | 入门样例，演示 Host 侧如何 `MakeLayout`。 |

---

## 4. 核心概念与源码讲解

### 4.1 布局类型：从逻辑坐标到物理排布

#### 4.1.1 概念说明

不同存储层对数据的「摆放方式」要求不同：

- **GM（全局显存）**：用户喂进来的 A/B/C 矩阵通常是朴素二维排布——行主序（一行存完再存下一行）或列主序。对应 `RowMajor` / `ColumnMajor`。
- **L0C / L1（Cube 核内）**：硬件的 Mmad 指令按「分形（fractal）」为单位吞吐数据。一个分形是 \(16\times16\) 个元素（对 fp16）的小块，块内、块间各有独立的排布方向，于是衍生出 `nZ`/`zN`/`zZ`/`nN`/`L0C` 等 4 维布局。
- **非对齐场景**：当矩阵 stride 不是 512 字节对齐、或超过 `STRIDE_LIMIT=65536` 时，直接朴素排布会让搬运带宽暴跌，于是有 `PaddingRowMajor`/`PaddingColumnMajor` 把矩阵重新切块重排（U8-l3 会专门讲）。
- **卷积**：特征图 / 权重有自己专属的 5/6 维排布 `NDC1HWC0`、`KDC1KHKWN1N0C0`（U9-l5 讲）。

CATLASS 把这些排布方式抽象成一组**行为一致的类型**：无论哪种布局，都对外暴露同样的方法签名——`MakeLayout<Element>(rows, cols)` 构造、`GetOffset(coord)` 算偏移、`shape()`/`stride()`/`Capacity()` 查询。这样上层 Kernel 代码就能用同一套「坐标乘 TileShape → 调 `GetOffset`」逻辑，无视底层排布差异。

#### 4.1.2 核心流程

每个布局类型内部只存「映射参数」，最典型的两类结构如下：

```text
# 朴素二维布局（RowMajor / ColumnMajor），RANK = 2
Layout {
    Shape   shape_   = (rows, cols)        # 逻辑形状
    Stride  stride_  = (stride0, stride1)  # 每个维度相邻元素的步长
}

# 分形布局（nZ/zN/zZ/nN/L0C），RANK = 4，额外记住原始 2 维形状
Layout {
    OrgShape orgShape_ = (orgRows, orgCols)  # 原始逻辑形状（触边裁剪要用）
    Shape    shape_    = (inFractalR, byFractalR, inFractalC, byFractalC)
    Stride   stride_   = (对应 4 个维度的步长)
}
```

布局类型的统一接口契约（以 `RowMajor` 为例）：

- `MakeLayout<Element>(rows, cols)` —— 静态工厂，根据元素位宽算出正确的 shape/stride，返回一个布局对象。
- `GetOffset(MatrixCoord) -> LongIndex` —— **核心方法**，把 `(row, col)` 映射成线性偏移（单位：元素个数）。
- `shape()` / `shape(i)` / `stride()` / `stride(i)` —— 查询形状与步长。
- `Capacity() -> LongIndex` —— 该布局占用的元素总数。
- `GetTileLayout(tileShape)` —— 取出某个子 tile 的布局（视图像，不搬运数据）。

#### 4.1.3 源码精读

`RowMajor` 的完整定义，注意它的 `stride_` 默认就是 `(cols, 1)`——这正是「行主序」的数学体现：跨一行要跳 `cols` 个元素，跨一列只跳 1 个。

[include/catlass/layout/matrix.hpp:23-90](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/layout/matrix.hpp#L23-L90) 定义了 `RowMajor`，其中构造与偏移函数为：

```cpp
// 行主序：第 0 维（行）步长 = ldm，第 1 维（列）步长 = 1
RowMajor(Index rows, Index cols)
    : shape_(MakeCoord(rows, cols)), stride_(MakeCoord(LongIndex(cols), LongIndex(1))) {}

// 行主序偏移：行 * 行步长 + 列
LongIndex GetOffset(MatrixCoord const& coord) const {
    return LongIndex(coord.row()) * stride_[0] + LongIndex(coord.column());
}
```

`ColumnMajor` 把这两个方向对调（[include/catlass/layout/matrix.hpp:189-222](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/layout/matrix.hpp#L189-L222)）：

```cpp
ColumnMajor(Index rows, Index cols)
    : shape_(MakeCoord(rows, cols)), stride_(MakeCoord(LongIndex(1), LongIndex(rows))) {}

LongIndex GetOffset(MatrixCoord const& coord) const {
    return LongIndex(coord.row()) + LongIndex(coord.column()) * stride_[1];
}
```

分形布局 `nZ` 的注释直接点明它的排布语义——**块内列主序、块间行主序**（[include/catlass/layout/matrix.hpp:306-307](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/layout/matrix.hpp#L306-L307)）：

```cpp
/// Mapping function for nZ matrices which is col-major inside fractal and row-major between fractal
struct nZ {
```

`zN` 则相反——块内行主序、块间列主序（[include/catlass/layout/matrix.hpp:474-475](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/layout/matrix.hpp#L474-L475)）。`L0C`（[include/catlass/layout/matrix.hpp:802-805](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/layout/matrix.hpp#L802-L805)）是累加器专用排布，结构与 zN 接近但常数不同。

> 把布局类型按「维度/用途」分成三族，便于记忆：

| 族 | 类型 | RANK | 典型用途 |
|---|---|---|---|
| 朴素二维 | `RowMajor` / `ColumnMajor` | 2 | GM 上用户输入的 A/B/C |
| 分形 | `nZ` / `zN` / `zZ` / `nN` / `L0C` / `Weight4BitnZ` | 4 | L1 / L0C 上 Cube 吞吐的分形数据 |
| 重排 / 卷积 | `PaddingRowMajor` / `PaddingColumnMajor` / `NDC1HWC0` / `KDC1KHKWN1N0C0` | 4–6 | 对齐优化、卷积特征图/权重 |

#### 4.1.4 代码实践

**实践目标**：把 `matrix.hpp` 里的布局类型归族，建立「看到名字就能猜用途」的直觉。

**操作步骤**：

1. 打开 [include/catlass/layout/matrix.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/layout/matrix.hpp)，定位每个 `struct` 的定义起始行。
2. 为每个类型记录它的 `static constexpr int RANK`（朴素布局为 2，分形/重排为 4，卷积为 5–6）。
3. 读它的文档注释（每个 `struct` 上方一行 `/// Mapping function for ...`）。

**需要观察的现象**：所有分形布局都有 `ORG_SHAPE_RANK = 2` 与 `orgShape_` 成员；朴素二维布局则没有 `orgShape_`。这说明分形布局在「分形排布」之外，还额外记住了「用户视角的原始二维形状」，以便触边裁剪（见 u2-l4 的 `GetActualBlockShape`）。

**预期结果**：你能复述上表三族分类，并能解释「为什么分形布局需要 `orgShape_` 而朴素布局不需要」。

#### 4.1.5 小练习与答案

**练习 1**：`RowMajor` 与 `ColumnMajor` 的 `GetOffset` 公式有何对称关系？

> **答**：两者都只有一个「大步长」维度。`RowMajor` 行方向步长大：`offset = row*cols + col`；`ColumnMajor` 列方向步长大：`offset = row + col*rows`。互换「行/列」与「`stride_[0]`/`stride_[1]`」即得到对方。

**练习 2**：分形布局为什么要把 `RANK` 设成 4 而不是 2？

> **答**：因为分形排布把一个二维坐标拆成了「块间坐标（第几个分形）」和「块内坐标（分形内第几个元素）」两层，行、列各拆一次，共 4 个维度 `(inFractalR, byFractalR, inFractalC, byFractalC)`。用 `div/mod` 在这 4 维之间换算，正是 `GetOffset` 里除法和取模的由来。

---

### 4.2 MakeLayout 构造：为什么依赖元素类型

#### 4.2.1 概念说明

`MakeLayout` 是个模板方法：`MakeLayout<Element>(rows, cols)`。**它必须知道元素类型 `Element`**，原因只在分形布局里才真正显现——一个分形固定是 512 字节（`BYTE_PER_FRACTAL`），但「512 字节能装几个元素」取决于每个元素占几位：

\[
\text{ELE\_NUM\_PER\_C0} = \frac{\text{BytesToBits}(\text{BYTE\_PER\_C0})}{\text{SizeOfBits<Element>}}
\qquad
\text{ELE\_NUM\_PER\_FRACTAL} = \frac{\text{BytesToBits}(\text{BYTE\_PER\_FRACTAL})}{\text{SizeOfBits<Element>}}
\]

例如 `BYTE_PER_C0=32` 字节：

- `half`（16 位）：\(256/16 = 16\) 个元素/C0；
- `int8`（8 位）：\(256/8 = 32\) 个元素/C0；
- `float`（32 位）：\(256/32 = 8\) 个元素/C0。

元素位宽不同，分形内「一行多少个元素」「一共多少个 C0」就不同，shape 和 stride 也随之不同——这就是 `MakeLayout` 必须吃 `Element` 的根本原因。

> 朴素二维布局的 `MakeLayout` 其实「几乎不关心」`Element`：`RowMajor::MakeLayout` 对绝大多数类型直接 `return RowMajor(rows, cols);`，只有 Ascend950（`CATLASS_ARCH==3510`）下的 4 位浮点类型才会把列向上取整到 2 的倍数（[include/catlass/layout/matrix.hpp:59-68](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/layout/matrix.hpp#L59-L68)）。它保留 `Element` 模板参数，是为了和分形布局保持**统一签名**，让上层能用同一句 `LayoutA::MakeLayout<ElementA>(m, k)` 不加区分地调用。

#### 4.2.2 核心流程

分形 `MakeLayout` 的通用三步：

1. 由 `Element` 算出每 C0 元素数、每分形元素数两个 `constexpr`。
2. 用 `RoundUp` 把 `orgRows` / `orgCols` 向上取整到分形倍数（块不能装半个）。
3. 把 `orgShape / shape / stride` 三组参数填进构造函数返回。

以 `nZ::MakeLayout<half>(orgRows, orgCols)` 为例（fp16）：

```text
ELE_NUM_PER_C0      = 16          # 一个 C0 装 16 个 half
ELE_NUM_PER_FRACTAL = 256         # 一个分形装 256 个 half (=16x16)
rowsRound = RoundUp<16>(orgRows)  # 行向上对齐到 16
colsRound = RoundUp<16>(orgCols)  # 列向上对齐到 16

shape  = (16, rowsRound/16, 16, colsRound/16)   # (inR, byR, inC, byC)
stride = (1,  colsRound*16, 16, 256)            # 对应四维步长
```

#### 4.2.3 源码精读

`RowMajor::MakeLayout`（朴素布局，对普通类型退化为直接构造）：

[include/catlass/layout/matrix.hpp:59-68](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/layout/matrix.hpp#L59-L68)

```cpp
template <class Element>
CATLASS_HOST_DEVICE static RowMajor MakeLayout(Index rows, Index cols)
{
#if (defined(CATLASS_ARCH) && CATLASS_ARCH == 3510)
    if constexpr (std::is_same_v<Element, float4_e2m1x2_t> || ...) {
        return RowMajor(rows, cols, RoundUp<2>(cols));
    }
#endif
    return RowMajor(rows, cols);   // half/int8/float 等都走这里
}
```

`nZ::MakeLayout`（分形布局，真正用到了 `Element`）：

[include/catlass/layout/matrix.hpp:356-366](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/layout/matrix.hpp#L356-L366)

```cpp
template <class Element>
CATLASS_HOST_DEVICE constexpr static nZ MakeLayout(Index orgRows, Index orgCols)
{
    constexpr uint32_t ELE_NUM_PER_C0 = BytesToBits(BYTE_PER_C0) / SizeOfBits<Element>::value;
    constexpr uint32_t ELE_NUM_PER_FRACTAL = BytesToBits(BYTE_PER_FRACTAL) / SizeOfBits<Element>::value;
    Index rowsRound = RoundUp<ELE_NUM_PER_C0>(orgRows);
    Index colsRound = RoundUp<C0_NUM_PER_FRACTAL>(orgCols);
    return nZ(orgRows, orgCols,
              ELE_NUM_PER_C0, rowsRound / ELE_NUM_PER_C0, C0_NUM_PER_FRACTAL, colsRound / C0_NUM_PER_FRACTAL,
              1, colsRound * ELE_NUM_PER_C0, ELE_NUM_PER_C0, ELE_NUM_PER_FRACTAL);
}
```

Host 侧的实际调用就在样例里（[examples/00_basic_matmul/basic_matmul.cpp:60-65](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L60-L65)）：

```cpp
using LayoutA = layout::RowMajor;
LayoutA layoutA = LayoutA::template MakeLayout<ElementA>(m, k);  // ElementA = half
```

Kernel 层 `BasicMatmul::ToUnderlyingArguments` 里也用同一套写法把 problem shape 补成 layout（[include/catlass/gemm/kernel/basic_matmul.hpp:86-91](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp#L86-L91)）：

```cpp
LayoutA layoutA = LayoutA::template MakeLayout<ElementA>(args.problemShape.m(), args.problemShape.k());
LayoutB layoutB = LayoutB::template MakeLayout<ElementB>(args.problemShape.k(), args.problemShape.n());
LayoutC layoutC = LayoutC::template MakeLayout<ElementC>(args.problemShape.m(), args.problemShape.n());
```

#### 4.2.4 代码实践

**实践目标**：体会「元素位宽不同 → 分形形状不同」。

**操作步骤**（源码阅读型实践）：

1. 取 `orgRows = 48, orgCols = 48`。
2. 分别用 `half`（16 位）和 `int8`（8 位）套用 `nZ::MakeLayout` 的公式，手算 `ELE_NUM_PER_C0`、`ELE_NUM_PER_FRACTAL`、`rowsRound`、`colsRound` 与最终 `shape`。

**需要观察的现象**：`int8` 的 `ELE_NUM_PER_C0`（32）正好是 `half`（16）的两倍；因此 `int8` 下「分形内行数」翻倍、「分形行数」减半。

**预期结果**：

| Element | ELE_NUM_PER_C0 | ELE_NUM_PER_FRACTAL | nZ shape (inR, byR, inC, byC) |
|---|---|---|---|
| `half` | 16 | 256 | (16, 3, 16, 3) |
| `int8` | 32 | 512 | (32, 2, 16, 3) |

> 说明：`int8` 的 `ELE_NUM_PER_FRACTAL = BytesToBits(512)/8 = 512`；分形仍占 512 字节，只是塞进了更多元素，故块内行数从 16 变 32。`orgCols=48` 经 `RoundUp<16>` 仍为 48，`byC = 48/16 = 3`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `RowMajor::MakeLayout` 对 `half` 不做任何对齐，而 `nZ::MakeLayout` 必须 `RoundUp`？

> **答**：`RowMajor` 描述的是朴素连续内存，元素逐个紧排，没有「块」的概念，不需要对齐。`nZ` 描述的是分形内存，硬件按整个分形（16×16）为单位搬运/计算，矩阵尺寸不是分形倍数时必须向上补齐到倍数（padding），否则无法填满一个分形。

**练习 2**：如果把 `ElementA` 从 `half` 改成 `float`，`basic_matmul.cpp` 里 `layoutA` 的 `stride_` 会变成什么？

> **答**：朴素 `RowMajor::MakeLayout<float>` 仍返回 `RowMajor(m, k)`，`stride_ = (k, 1)`，**与元素类型无关**——因为朴素布局的步长单位是「元素个数」而非字节。元素类型只影响后续「元素偏移 → 字节偏移」时的 `sizeof(float)`。

---

### 4.3 坐标到偏移映射：GetOffset 的数学

#### 4.3.1 概念说明

`GetOffset(MatrixCoord)` 是 Layout 的「心脏」——它把一个逻辑二维坐标 `(row, col)` 换算成线性偏移。**务必分清两种偏移**：

- `GetOffset` 返回的是 **元素偏移**（`LongIndex`，单位：元素个数）；
- 真正在 GM 上的 **字节地址** = 基地址 + 元素偏移 \(\times\) `sizeof(Element)`。

在 Kernel 里这一点体现得很清楚（[include/catlass/gemm/kernel/basic_matmul.hpp:130-136](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp#L130-L136)）：`gmOffsetA` 是 `GetOffset` 返回的元素偏移，`gmA` 是 `GlobalTensor<half>`，`gmA[gmOffsetA]` 这个下标运算会自动乘上 `sizeof(half)` 得到字节地址。

朴素布局的 `GetOffset` 是线性公式；分形布局的 `GetOffset` 则是「除法定位块 + 取模定位块内」的二层分解。

#### 4.3.2 核心流程

**朴素二维（RowMajor）**：

\[
\text{offset}(r, c) = r \cdot \text{stride}_0 + c,\qquad \text{stride}_0 = \text{cols}
\]

**分形（nZ，块内列主序、块间行主序）**：设分形内行数 \(R_f=\text{shape}_0\)、分形内列数 \(C_f=\text{shape}_2\)，

\[
\text{offset}(r, c) = \underbrace{\lfloor r/R_f \rfloor \cdot \text{stride}_1}_{\text{跳到第几个「分形行」}} + \underbrace{\lfloor c/C_f \rfloor \cdot \text{stride}_3}_{\text{跳到第几个「分形列」}} + \underbrace{(r \bmod R_f)\cdot \text{stride}_0}_{\text{分形内行位置}} + \underbrace{(c \bmod C_f)\cdot \text{stride}_2}_{\text{分形内列位置}}
\]

直觉：分形布局把一个全局 `(r, c)` 先除以分形尺寸定位到「第 `(r/R_f, c/C_f)` 个分形」，再取模定位到「分形内的第 `(r%R_f, c%C_f)` 个元素」。块内/块间的主序方向由各 `stride` 的相对大小决定。

#### 4.3.3 源码精读

`RowMajor::GetOffset`（[include/catlass/layout/matrix.hpp:79-83](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/layout/matrix.hpp#L79-L83)）：

```cpp
LongIndex GetOffset(MatrixCoord const& coord) const {
    return LongIndex(coord.row()) * stride_[0] + LongIndex(coord.column());
}
```

`nZ::GetOffset`（[include/catlass/layout/matrix.hpp:370-375](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/layout/matrix.hpp#L370-L375)），四项分别对应上面的四个下划线项：

```cpp
LongIndex GetOffset(MatrixCoord const& coord) const {
    return LongIndex(coord.row()) / shape_[0] * stride_[1]      // 哪个分形行
         + LongIndex(coord.column()) / shape_[2] * stride_[3]   // 哪个分形列
         + (LongIndex(coord.row()) % shape_[0]) * stride_[0]    // 分形内行
         + (LongIndex(coord.column()) % shape_[2]) * stride_[2];// 分形内列
}
```

Kernel 层的实战调用——先把「块坐标乘 L1TileShape」得到逻辑坐标 `offsetA`，再交给 `GetOffset` 转成 GM 元素偏移（[include/catlass/gemm/kernel/basic_matmul.hpp:126-137](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp#L126-L137)）：

```cpp
MatrixCoord offsetA{blockCoord.m() * L1TileShape::M, blockCoord.k() * L1TileShape::K};
int64_t gmOffsetA = params.layoutA.GetOffset(offsetA);   // 元素偏移
...
blockMmad(gmA[gmOffsetA], params.layoutA, ...);          // gmA[...] 内部 ×sizeof(half) 得字节地址
```

`MatrixCoord` 本身只是 `Coord<2, uint32_t>` 的具名包装（[include/catlass/matrix_coord.hpp:34-63](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/matrix_coord.hpp#L34-L63)），`.row()` 取第 0 维、`.column()` 取第 1 维：

```cpp
struct MatrixCoord : public Coord<2, uint32_t> {
    MatrixCoord(Index row, Index column) : Base(MakeCoord(row, column)) {}
    Index const& row() const { return this->At(ROW_INDEX); }      // ROW_INDEX = 0
    Index const& column() const { return this->At(COLUMN_INDEX); }// COLUMN_INDEX = 1
};
```

#### 4.3.4 代码实践

**实践目标**：用 `LayoutA::MakeLayout<half>(m, k)` 构造一个布局，手算 `offsetA` 在某个 `(row, col)` 下的偏移，并与源码 `GetOffset` 公式逐项对照（本讲的核心实践）。

**场景取值**：沿用 `00_basic_matmul` 的默认参数 `m=256, k=1024`，`ElementA = half`（`sizeof(half)=2` 字节）。

**操作步骤**：

1. 构造布局：`RowMajor::MakeLayout<half>(256, 1024)`，得 `shape_=(256,1024)`、`stride_=(1024, 1)`。
2. 任取一个坐标，例如 `MatrixCoord offsetA{2, 5}`（即第 2 行第 5 列，对应 Kernel 里 `blockCoord.m()*128 = 2` 这种逻辑起点）。
3. 套用 `RowMajor::GetOffset` 公式手算：
   \[
   \text{offset} = 2 \times 1024 + 5 = 2053 \text{（元素）}
   \]
4. 换算字节偏移：
   \[
   \text{字节偏移} = 2053 \times \text{sizeof(half)} = 2053 \times 2 = 4106 \text{ 字节}
   \]
5. 打开 [include/catlass/layout/matrix.hpp:79-83](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/layout/matrix.hpp#L79-L83)，确认源码实现正是 `row()*stride_[0] + column()`，与手算一致。

**需要观察的现象**：

- `GetOffset` 返回 **2053**（元素个数），不是字节。要得到字节地址必须再乘 `sizeof(half)`。
- 若把 `ElementA` 换成 `float`，同一坐标的 `GetOffset` 仍是 2053（朴素布局步长与元素类型无关），但字节偏移变为 `2053*4 = 8212`。

**预期结果**：手算 `GetOffset(2,5)=2053` 与源码公式吻合；字节偏移 4106 由「元素偏移 × sizeof」得到，对应 Kernel 里 `gmA[2053]` 的真实寻址。

> **进阶（选做，待本地验证）**：对同一 `(256,1024)` 构造一个 `nZ::MakeLayout<half>(256,1024)`，手算 `GetOffset(20, 18)`。提示：`R_f=C_f=16`，`20/16=1, 20%16=4`，`18/16=1, 18%16=2`，stride 由 `colsRound*16=1024*16`、`256`、`1`、`16` 四项构成；逐项相加后与 `nZ::GetOffset` 源码对照。（本项涉及分形排布的完整推导，可在 U5「Tile 层」再回来验证。）

#### 4.3.5 小练习与答案

**练习 1**：`RowMajor(256,1024)` 下，坐标 `(0,0)` 与 `(1,0)` 的元素偏移差是多少？它等于什么？

> **答**：`(1,0)` 的偏移 \(=1\times1024+0=1024\)，与 `(0,0)` 的 \(0\) 相差 1024，正好等于一行的元素数 `stride_[0]=cols=1024`。这正是「行主序：相邻行相差一整行」的体现。

**练习 2**：为什么说 `GetOffset` 返回的是「元素偏移」而非「字节偏移」？Kernel 是怎么把它变成字节地址的？

> **答**：因为 `GetOffset` 的步长 `stride_` 单位是「元素个数」（`RowMajor(rows,cols)` 里 `stride=(cols,1)`，`cols` 是列数而非字节数）。Kernel 用 `GlobalTensor<ElementA> gmA; gmA[gmOffsetA]` 做下标，`GlobalTensor` 知道 `ElementA` 的位宽，内部自动完成 `元素偏移 × sizeof(ElementA)` 的字节换算。

**练习 3**：`nZ::GetOffset` 里为什么会出现除法 `/` 和取模 `%`，而 `RowMajor::GetOffset` 没有？

> **答**：`nZ` 是分形排布，一个全局坐标要先除以分形尺寸定位到「第几个分形」（块间，用 `/`），再取模定位到「分形内的位置」（块内，用 `%`）。`RowMajor` 是朴素连续排布，没有「块」的概念，坐标与偏移是纯线性关系，只需乘加。

---

## 5. 综合实践

把三个最小模块串起来，完成一次「从样例参数到 GM 寻址」的完整追踪。

**任务**：给定 `00_basic_matmul` 的运行参数 `./00_basic_matmul 256 512 1024 0`（即 \(M=256, N=512, K=1024\)），回答下列问题并画出一张 A/B/C 三个矩阵的寻址示意图。

1. **归类**：样例里 `LayoutA/LayoutB/LayoutC` 都用了哪个布局类型？它属于哪一族、`RANK` 是多少？（提示：[basic_matmul.cpp:60-65](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L60-L65)）
2. **构造**：写出 `LayoutA`、`LayoutB`、`LayoutC` 经 `MakeLayout<half>` 后的 `shape_` 与 `stride_`。
   - A（M×K = 256×1024）：`shape=(256,1024)`，`stride=(1024,1)`
   - B（K×N = 1024×512）：`shape=(1024,512)`，`stride=(512,1)`
   - C（M×N = 256×512）：`shape=(256,512)`，`stride=(512,1)`
3. **寻址**：假设 SPMD 循环里某核拿到块坐标 `blockCoord = (m=1, n=2, k=0)`，`L1TileShape=(128,256,256)`（见 [basic_matmul.cpp:88](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L88)），按 [basic_matmul.hpp:127-132](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp#L127-L132) 计算：
   - `offsetA = (1*128, 0*256) = (128, 0)` → `gmOffsetA = 128*1024 + 0 = 131072`（元素）
   - `offsetB = (0*256, 2*256) = (0, 512)` → `gmOffsetB = 0*512 + 512 = 512`（元素）
   - `offsetC = (1*128, 2*256) = (128, 512)` → `gmOffsetC = 128*512 + 512 = 66048`（元素）
4. **换算**：把上面三个元素偏移换算成字节偏移（`half` → ×2），并说明它们分别落在 A/B/C 缓冲区的什么位置。

**验收标准**：你能不看答案独立从「块坐标 → `MatrixCoord` → `GetOffset` → 字节偏移」走完一遍，并能解释「为什么 A 的行步长是 1024 而 C 的行步长是 512」（因为 A 沿 K 维展开、C 沿 N 维展开，列数不同）。

> 说明：以上数值为依据源码公式的手算结果，运行时真实块坐标由 `BlockScheduler.GetBlockCoord(loopIdx)` 决定（见 u2-l4），不同 `loopIdx` 会得到不同 `(m,n)`；本实践聚焦「给定块坐标如何寻址」，分核逻辑不在本讲范围。

---

## 6. 本讲小结

- **Layout 是纯映射**：它只存 `shape_`/`stride_`（分形布局额外存 `orgShape_`），不持有数据；职责是把逻辑坐标 `(row, col)` 换算成线性偏移。
- **三族布局**：朴素二维（`RowMajor`/`ColumnMajor`，RANK=2，用于 GM）、分形（`nZ`/`zN`/`zZ`/`nN`/`L0C`/`Weight4BitnZ`，RANK=4，用于 L1/L0C）、重排与卷积（`Padding*`、`NDC1HWC0` 等）。
- **`MakeLayout<Element>` 必须吃元素类型**：因为分形里「512 字节装几个元素」取决于元素位宽；朴素布局虽不关心 `Element`，但为统一签名也保留该模板参数。
- **`GetOffset` 返回元素偏移**：朴素布局是线性公式 `row*stride + col`；分形布局是「除法定位块 + 取模定位块内」的二层分解。字节偏移需再乘 `sizeof(Element)`，由 `GlobalTensor<T>` 的下标运算完成。
- **它是 GM 寻址的地基**：Kernel 层「块坐标 × L1TileShape → `MatrixCoord` → `GetOffset` → `gmA[offset]」这条链路，正是 Layout 真正发挥作用的地方。

---

## 7. 下一步学习建议

- **横向**：本讲只讲了 `Layout` 这一块拼图。u3-l2 会把「元素类型 + 布局」绑成 `GemmType`，并引入 `ElementAccumulatorSelector`（如 half 输入 → fp32 累加）和 `GemmShape`/`TileShape`，把数据模型补全。
- **纵向（推荐）**：本讲的分形布局（`nZ`/`zN`/`L0C`）在 GM 上用得少，真正大量出现在 L1/L0C 搬运里。学到 U5「Tile 层与硬件指令」（u5-l2 TileCopy 数据搬运族）时，你会看到 `copy_gm_to_l1` 如何把 GM 的 `RowMajor` 转成 L1 的分形布局——那时再回头看本讲的 `GetOffset` 分形公式，会有「打通任督二脉」的感觉。
- **延伸阅读**：U8-l3 会讲 `PaddingRowMajor` 这类重排布局如何解决 stride 非 512 字节对齐 / 超 `STRIDE_LIMIT` 的带宽问题，可作为本讲「重排族」的深入入口。
