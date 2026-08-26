# Tile 编程模型深度剖析：容量、有效区域与布局

> 前置讲义：u2-l2（GlobalTensor：全局内存上的形状与步长抽象）。本讲把视角从 GM 侧切换到片上侧，精读 `include/pto/common/pto_tile.hpp` 中的 `Tile` 模板。

## 1. 本讲目标

学完本讲，你应该能够：

1. **区分两套形状概念**：Tile 的「容量形状 Rows×Cols（编译期常量）」与「有效区域 validRow×validCol（静态或运行期）」，并说清楚为什么 PTO 要把它们拆开。
2. **看懂 Tile 的模板参数表**：`TileType`（Vec/Mat/Left/Right/Acc…）、`BLayout`（RowMajor/ColMajor）、`SLayout`（NoneBox/RowMajor/ColMajor）、`SFractalSize` 各自控制什么。
3. **解释分形（fractal）布局**：为什么 Cube 指令要求 512 字节/1024 字节的「基块」，`TileLeft`/`TileRight`/`TileAcc` 别名分别对应 Nz/Zn/Zz 中的哪一种。
4. **掌握对齐约束**：未盒化 Tile 的 32 字节对齐 `static_assert` 在哪里、如何触发、怎样修改形状才能通过。
5. **亲手做一个掩码实验**：在 CPU 模拟器上用动态 validRows/validCols 只写 10x100 的有效区域，验证 GM 中有效区外的数据不被 TSTORE 触碰。

## 2. 前置知识

- **容量 vs 有效区域（本讲核心直觉）**：Tile 是一块「固定容量的二维格子」，容量在编译期定死，硬件按容量预留寄存器/UB 空间。但一次具体计算往往用不满容量——比如 4096 列的矩阵按 256 列切块，最后一块只有 100 列是真的。PTO 把「格子多大」和「这次用多少」拆成两组参数：前者 `Rows_/Cols_`，后者 `RowValid_/ColValid_`。
- **掩码（mask）**：有效区域是一个「连续前缀」——左上角对齐的矩形，行号 \( 0 \le i < \text{validRow} \)，列号 \( 0 \le j < \text{validCol} \)。它不是任意形状的位掩码，而是一对整数，因此传递开销极小，且可以直接映射到硬件的边界处理参数。
- **`DYNAMIC = -1`**：与 Shape/Stride 一致的约定（见 u2-l2）。模板参数填 `-1` 表示「这个值留到运行期」，构造函数实参个数必须与之匹配。
- **行主/列主（RowMajor/ColMajor）**：同一个二维逻辑坐标 (r, c) 在一维存储里的排布方式。行主按「一行接一行」存放，列主按「一列接一列」存放。
- **盒化/分形（boxed/fractal）**：把大矩阵切成固定大小的小块（基块，base tile），块内一种排布、块间另一种排布。这是昇腾 Cube 单元（矩阵乘硬件）期望的数据组织方式。
- **CPU 模拟器**：`__CPU_SIM` 宏下的编译路径（见 u1-l5）。Tile 在 CPU 上退化为「指向 host 内存的指针 + 必要的元数据」，但**形状与布局规则与 NPU 完全一致**，因此布局错误在 CPU 上就会以编译错误暴露。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/pto/common/pto_tile.hpp](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp) | 本讲主角：`Tile` 模板、有效区域、布局常量、别名、偏移计算 |
| [include/pto/common/type.hpp](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/type.hpp) | `TileType`/`BLayout`/`SLayout`/`Layout` 枚举定义 |
| [include/pto/common/memory.hpp](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/memory.hpp) | `MemoryQualifier`：TileType → 物理存储限定符（`__ubuf__`/`__ca__`…） |
| [include/pto/common/constants.hpp](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/constants.hpp) | `C0_SIZE_BYTE=32`、`FRACTAL_NZ_ROW=16`、MX/HIF4 分形常量 |
| [include/pto/cpu/tile_offsets.hpp](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/cpu/tile_offsets.hpp) | CPU 侧坐标→偏移的落地实现（行主/列主/Nz/Zn/Zz 分支） |
| [include/pto/cpu/TLoad.hpp](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/cpu/TLoad.hpp) | TLOAD 如何按有效区域搬运并填充 pad 值 |
| [include/pto/cpu/TStore.hpp](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/cpu/TStore.hpp) | TSTORE 如何只写有效区域（本讲实践的验证点） |
| [include/pto/cpu/TAdd.hpp](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/cpu/TAdd.hpp) | 计算指令如何消费有效区域 |
| [docs/coding/Tile_zh.md](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/docs/coding/Tile_zh.md) / [docs/coding/Tile.md](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/docs/coding/Tile.md) | Tile 编程模型官方文档（中/英） |
| [tests/cpu/st/testcase/tadd/tadd_kernel.cpp](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/tadd/tadd_kernel.cpp) | 动态掩码的真实用例（`Tile<..., -1, -1>`） |

## 4. 核心概念与源码讲解

### 4.1 Tile 模板参数全景

#### 4.1.1 概念说明

一个 `Tile` 由「位置、元素类型、容量形状、基础布局、有效区域、分形布局、pad 策略」共同定义。它与上一讲的 `GlobalTensor` 互为镜像：

| | GlobalTensor（GM 侧） | Tile（片上侧） |
| --- | --- | --- |
| 描述对象 | 全局内存上的数据窗口 | 片上存储中的一块容量 |
| 形状 | 五维 Shape，可静可动 | 二维 Rows×Cols，**必须静态** |
| 有效区域 | 无（窗口形状即有效形状） | 独立的 validRow/validCol |
| 地址 | 拥有 `__gm__` 指针 | 由 TASSIGN 绑定，自身不拥有内存 |
| 消费者 | TLOAD/TSTORE 等搬运指令 | 计算指令（TADD/TMATMUL…） |

#### 4.1.2 核心流程

声明一个 Tile 时，编译器依次完成：

1. 解析 11 个模板参数（含默认值），把 `Rows_/Cols_/RowValid_/ColValid_` 提升为编译期常量 `Rows/Cols/ValidRow/ValidCol`；
2. 由 `BFractal_` 推导 `RowStride/ColStride`（行主：行跨一步 = Cols；列主：列跨一步 = Rows）；
3. 由 `SFractal_ + SFractalSize_` 推导内层基块尺寸 `InnerRows/InnerCols`；
4. 执行一组 `static_assert`：容量合法性、可整除性、32 字节对齐、SFractalSize 合法性；
5. 按 `Loc_` 通过 `MemoryQualifier` 决定 `data_` 成员的类型（NPU 上是带地址限定符的指针，CPU 上是普通指针）。

#### 4.1.3 源码精读

Tile 模板的完整参数表（注意后 7 个参数都有默认值，最简声明只需 4 个）：

[include/pto/common/pto_tile.hpp:1390-1395](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L1390-L1395) 声明 `Tile<Loc_, Element_, Rows_, Cols_, BFractal_, RowValid_, ColValid_, SFractal_, SFractalSize_, PadVal_, Compact_>`，其中 `RowValid_` 默认等于 `Rows_`、`ColValid_` 默认等于 `Cols_`——也就是说**不写有效区域时，整块容量都有效**。

[include/pto/common/pto_tile.hpp:1425-1433](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L1425-L1433) 把模板参数固化为常量成员，并由 `BFractal_` 推导出行/列步长，同时断言容量不小于有效区域：

```cpp
static constexpr int RowStride = BFractal_ == BLayout::RowMajor ? Cols : 1;
static constexpr int ColStride = BFractal_ == BLayout::RowMajor ? 1 : Rows;
static constexpr int ValidRow = RowValid_;
static constexpr int ValidCol = ColValid_;
static_assert(Rows > 0 && ValidRow <= Rows && Cols > 0 && ValidCol <= Cols, "Invalid Tile Layout.");
```

这段是「容量 ≥ 有效」的总闸门：`ValidRow > Rows` 直接编译失败。

`TileType` 枚举共 10 个值，[include/pto/common/type.hpp:123-134](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/type.hpp#L123-L134) 定义了 `Vec/Mat/Left/Right/Acc/Bias/Scaling/ScaleLeft/ScaleRight/Ctrl`。它不只是标签——[include/pto/common/memory.hpp:26-33](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/memory.hpp#L26-L33) 起的 `MemoryQualifier` 特化把每个位置映射到真实物理存储：`Vec → __ubuf__`（UB 统一缓冲）、`Mat → __cbuf__`（L1）、`Left → __ca__`（L0A）、`Right → __cb__`（L0B）、`Acc → __cc__`（L0C）。

CPU 模拟器下 Tile 的存储退化，[include/pto/common/pto_tile.hpp:1540-1555](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L1540-L1555)：`__CPU_SIM`/`__COSTMODEL` 分支中 `TileDType` 是普通 `DType*`，注释明确说明「CPU Sim 下 data_ 是一个可被 TASSIGN 重定向到共享 NPU 内存的指针」。

#### 4.1.4 代码实践

1. **实践目标**：建立「模板参数 → 编译期常量」的直观感受。
2. **操作步骤**：写一个只包含 `<pto/pto-inst.hpp>` 的 `.cpp`（标注 `__CPU_SIM` 编译，方法见 u1-l5 的 `g++ -E` 实验），用 `static_assert` 打印 Tile 的推导结果：

```cpp
// 示例代码：验证模板参数推导（非项目原有代码）
#include <pto/pto-inst.hpp>
using namespace pto;
using T = Tile<TileType::Vec, float, 16, 256>;          // 全默认
static_assert(T::Rows == 16 && T::Cols == 256);
static_assert(T::ValidRow == 16 && T::ValidCol == 256); // 默认有效=容量
static_assert(T::RowStride == 256 && T::ColStride == 1);// 行主推导
static_assert(T::Numel == 16 * 256);
static_assert(std::is_same_v<T::TileDType, float*>);    // CPU Sim 下是指针
int main() {}
```

3. **需要观察的现象**：把 `ValidRow == 16` 改成 `== 15` 会编译失败；把模板改成 `Tile<..., 16, 8, BLayout::RowMajor>`（8 列 float 只有 32 字节，恰好对齐）能通过，改成 6 列则触发 4.5 节的对齐断言。
4. **预期结果**：全部断言通过；错误实验给出指向 `pto_tile.hpp` 的 `static_assert` 消息。**待本地验证**（编译器版本不同，报错措辞可能有差异）。

#### 4.1.5 小练习与答案

- **练习 1**：`Tile<TileType::Vec, half, 16, 16>` 与 `Tile<TileType::Left, half, 16, 16>` 在 CPU 模拟器下 `TileDType` 有区别吗？在 NPU 下呢？
  **答案**：CPU 下都是 `half*`（[pto_tile.hpp:1540-1543](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L1540-L1543) 统一走指针分支）；NPU 下 `Vec` 是 `__ubuf__ half*`、`Left` 是 `__ca__ half*`（[memory.hpp:26-59](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/memory.hpp#L26-L59)），指向不同的物理存储。
- **练习 2**：为什么 `RowStride` 在行主下等于 `Cols` 而不是 `Cols * sizeof(T)`？
  **答案**：PTO 的步长单位是**元素**而不是字节（u2-l2 已确立的约定），字节换算推迟到指令实现层按 dtype 完成。
- **练习 3**：`Numel` 与 CPU 侧 `GetSizeInUnits()` 什么时候不相等？
  **答案**：对 twin 类型（一个存储单元装两个元素，如 int4b 打包）时 `GetSizeInUnits() = Numel / 2`，见 [pto_tile.hpp:1666-1677](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L1666-L1677)。

### 4.2 布局系统：BLayout 与坐标到偏移的映射

#### 4.2.1 概念说明

`BLayout` 回答一个问题：**逻辑坐标 (r, c) 在 Tile 的一维存储里排在第几位？** 行主（RowMajor）按行扫描，列主（ColMajor）按列扫描。同一个 2x3 矩阵，两种布局的内存顺序完全不同。GPU 出身的读者可以把它类比为 row-major vs column-major 的 cuBLAS 布局参数——但 PTO 把它写进类型，编译器因此能在编译期算好每个下标。

#### 4.2.2 核心流程

未盒化（`SFractal == NoneBox`）布局下，坐标到偏移的映射是线性的：

\[
\text{offset}(r, c) =
\begin{cases}
r \times \text{Cols} + c & \text{RowMajor} \\
c \times \text{Rows} + r & \text{ColMajor}
\end{cases}
\]

硬件视角的含义：行主 Tile 的「一行」在内存里连续，适合按行流式处理；列主 Tile 的「一列」连续，适合按列访问（例如规约按列累加时对向量化更友好）。

#### 4.2.3 源码精读

[include/pto/cpu/tile_offsets.hpp:61-69](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/cpu/tile_offsets.hpp#L61-L69) 是 CPU 模拟器的落地实现，用 `if constexpr` 在编译期二选一：

```cpp
template <typename TileData>
size_t inline GetTileElementOffsetPlain(size_t r, size_t c)
{
    if constexpr (TileData::isRowMajor) {
        return r * TileData::Cols + c;
    } else {
        return c * TileData::Rows + r;
    }
}
```

`isRowMajor` 这个便捷常量来自 [include/pto/common/pto_tile.hpp:1438](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L1438)（`BFractal_ == BLayout::RowMajor`），全仓库的指令实现都用它做编译期分支，而不是运行期 if。

`BLayout` 枚举本身只有两个值，[include/pto/common/type.hpp:136-139](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/type.hpp#L136-L139)。

此外，`BLayout` 与 `SLayout` 组合会得到一个两字母布局名，[include/pto/common/memory.hpp:132-144](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/memory.hpp#L132-L144) 的 `GetLayoutName` 给出映射表：`NoneBox+RowMajor → "ND"`、`NoneBox+ColMajor → "DN"`、`RowMajor+RowMajor → "Zz"`、`ColMajor+RowMajor → "Nz"`、`RowMajor+ColMajor → "Zn"`、`ColMajor+ColMajor → "Nn"`。这组两字母名字是读懂昇腾文档的「黑话」，下一小节展开。

#### 4.2.4 代码实践

1. **实践目标**：确认同一个逻辑矩阵在两种 BLayout 下的内存排布差异。
2. **操作步骤**：在 CPU 模拟器下写两块 2x3 Tile（行主/列主各一），TASSIGN 到同一片 UB，用 `SetElement(r, c, v)` 逐格写入 1..6，再按 `data()` 的线性顺序打印（`SetElement` 定义在 [pto_tile.hpp:1684-1688](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L1684-L1688)，内部正是走 `GetTileElementOffset`）。
3. **需要观察的现象**：行主打印 `1 2 3 4 5 6`；列主打印 `1 4 2 5 3 6`。
4. **预期结果**：与手工按公式 \(\text{offset}=rC+c\) / \(cR+r\) 计算一致。注意 2x3 的 float 行主 Tile（3 列 × 4 字节 = 12 字节）不满足 32 字节对齐，会编译失败——请改用 2x8 或 4x8 做实验。**待本地验证**。

#### 4.2.5 小练习与答案

- **练习 1**：`Tile<Vec, float, 16, 64, BLayout::ColMajor>` 的 `RowStride`/`ColStride` 是多少？
  **答案**：`RowStride=1`、`ColStride=16`（[pto_tile.hpp:1428-1429](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L1428-L1429) 的三目表达式取另一分支）。
- **练习 2**：为什么 TADD 的 CPU 实现在行主下把 `validRow` 作为并行维度、列主下把 `validCol` 作为并行维度？
  **答案**：见 [include/pto/cpu/TAdd.hpp:25-43](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/cpu/TAdd.hpp#L25-L43)——`parallel_for_rows` 的第一个参数是「连续方向的长度」，多线程按不连续方向切任务、线程内沿连续方向向量化，两种布局各取对自己有利的方向。

### 4.3 分形布局：SLayout、SFractalSize 与基块

#### 4.3.1 概念说明

Cube（矩阵乘）硬件不是拿「一整块大矩阵」做乘法的，它每次吞一个固定大小的小矩阵块。以昇腾为例，矩阵引擎的一个典型操作粒度是 512 字节（A/B 操作数）或 1024 字节（累加器）。**分形布局就是把大矩阵在存储里直接按这些基块排好**：块内一种排布（由 `SLayout` 决定），块间另一种排布（由 `BLayout` 决定）。

- `SLayout::NoneBox`：不分块，普通二维矩阵（向量指令都用这种）。
- `SLayout::RowMajor / ColMajor`：分块，且声明**块内**是行主/列主。

基块大小不是任意值：`SFractalSize_` 只允许 `fractalABSize=512`、`fractalCSize=1024`、`fractalMxSize=32` 三档（[pto_tile.hpp:1535-1538](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L1535-L1538) 有专门断言）。给定 dtype 后基块形状随之确定：512 字节基块 + 内层行主时，fp32 是 16×8（16×8×4B=512B）、fp16 是 16×16、int8 是 16×32；内层列主则是它们的转置。

#### 4.3.2 核心流程

Tile 用两个 constexpr 函数从 `(SLayout, SFractalSize, sizeof(DType), BLayout)` 推导基块尺寸 `InnerRows × InnerCols`，然后要求：

\[
\text{Rows} \equiv 0 \pmod{\text{InnerRows}}, \qquad
\text{Cols} \equiv 0 \pmod{\text{InnerCols}}
\]

分形布局下的坐标映射变成两级：先算出元素落在哪个基块 `(BlockRow, BlockCol)` 与块内坐标 `(InnerRow, InnerCol)`，再按「块间排布 + 块内排布」合成线性偏移。以 Zz（外层行主 + 内层行主）为例：

\[
\text{offset} = (\text{BlockNumCol} \times \text{BlockRow} + \text{BlockCol}) \times \text{InnerNumel} + \text{InnerRow} \times \text{InnerCols} + \text{InnerCol}
\]

其中 `BlockNumCol = Cols / InnerCols`——即块间按「一行基块接一行基块」排。

#### 4.3.3 源码精读

基块尺寸推导，[include/pto/common/pto_tile.hpp:1399-1423](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L1399-L1423) 的 `getInnerRow()/getInnerCol()`：累加器档（1024B）固定 16×16；MX 档（32B）固定 16×2 或 2×16；AB 档（512B）按 `isInnerRowMajor` 取 \(16 \times 32/\text{sizeof}(T)\) 或 \(32/\text{sizeof}(T) \times 16\)。结果固化在 [pto_tile.hpp:1510-1513](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L1510-L1513)（`InnerRows/InnerCols/InnerNumel`）。

三类布局判别式，[include/pto/common/pto_tile.hpp:1806-1819](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L1806-L1819)：`is_Nz_layout`（外列主+内行主）、`is_Zn_layout`（外行主+内列主）、`is_Zz_layout`（外行主+内行主）。三者都是编译期 bool，供偏移函数 `if constexpr` 分派。

CPU 侧分形偏移计算，[include/pto/cpu/tile_offsets.hpp:37-59](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/cpu/tile_offsets.hpp#L37-L59) 的 `GetTileElementOffsetSubfractals`，四个分支分别对应 Nz/Zn/Zz/Nn，每支都是「块间一项 + 块内一项」的两级求和；入口在 [tile_offsets.hpp:71-82](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/cpu/tile_offsets.hpp#L71-L82)，`NoneBox` 走 4.2 节的朴素公式，否则先除模出块坐标再进分形函数。

常用别名（写 Cube 内核时几乎不会手写全模板参数，而是用别名）。CPU/非 a2a3 路径下 [pto_tile.hpp:1726-1736](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L1726-L1736) 的 `TileLeft` 是**外层列主 + 内层行主 + 512B**（即 Nz）；[pto_tile.hpp:1738-1741](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L1738-L1741) 的 `TileRight` 是**外层行主 + 内层列主 + 512B**（Zn）；[pto_tile.hpp:1768-1771](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L1768-L1771) 的 `TileAcc` 是**外层列主 + 内层行主 + 1024B**。注意 [pto_tile.hpp:1714-1724](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L1714-L1724) 在 `PTO_NPU_ARCH_A2A3`/Kirin 下另有一版 `TileLeft`（外层**行主**），所以同一别名在不同后端的物理布局不同——这正是「别名隔离平台差异」的设计意图。

文档佐证：[docs/coding/Tile_zh.md:129-133](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/docs/coding/Tile_zh.md#L129-L133) 明确写出 CPU 仿真后端上 TileLeft=Nz、TileRight=Zn、TileAcc=fractalCSize。

#### 4.3.4 代码实践

1. **实践目标**：用 `static_assert` 验证 fp16 的 AB 基块是 16×16，并验证别名组合。
2. **操作步骤**：

```cpp
// 示例代码（非项目原有代码）
#include <pto/pto-inst.hpp>
using namespace pto;
using A = TileLeft<half, 16, 16>;    // 容量恰为一个基块
static_assert(A::InnerRows == 16 && A::InnerCols == 16);
static_assert(!A::isRowMajor && A::isInnerRowMajor); // 外列主+内行主 = Nz
static_assert(is_Nz_layout<A>::value);
using B = TileRight<half, 16, 16>;
static_assert(B::isRowMajor && B::isInnerColMajor);  // Zn
static_assert(is_Zn_layout<B>::value);
int main() {}
```

3. **需要观察的现象**：把 `TileLeft<half, 8, 16>`（行数不是 16 的倍数）写进编译单元，观察报错。
4. **预期结果**：断言全部通过；非法形状触发 4.5 节引用的可整除性 `static_assert`。**待本地验证**。

#### 4.3.5 小练习与答案

- **练习 1**：512 字节基块 + fp32 + 内层行主，`InnerRows×InnerCols` 是多少？
  **答案**：16×8：内层行主时 `InnerRows = fixedRowSize = 16`、`InnerCols = alignedSize/sizeof(float) = 32/4 = 8`（[pto_tile.hpp:1406-1408](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L1406-L1408) 与 [pto_tile.hpp:1419-1421](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L1419-L1421)），乘积 128 元素 × 4B = 512B。
- **练习 2**：`TileAcc` 为什么用 `fractalCSize=1024` 而不是 512？
  **答案**：累加器 L0C 的硬件基块是 16×16；fp32 时 16×16×4B=1024B。`getInnerRow()/getInnerCol()` 里 `SFractalSize_ == fractalCSize` 的分支直接返回固定 `fixedRowSize/fixedColSize`（即 16×16，[pto_tile.hpp:1401-1402](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L1401-L1402) 与 [pto_tile.hpp:1414-1415](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L1414-L1415)），与 dtype 无关，因此 fp16 的 TileAcc 基块也是 16×16（实际占 512B，但按 1024B 档的形状规则）。
- **练习 3**：向量指令（TADD）的 Tile 能用 `SLayout::RowMajor` 吗？
  **答案**：能编译（盒化断言对 `Loc == Vec` 有豁免，见 [pto_tile.hpp:1522-1533](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L1522-L1533) 的第三分支 `(Loc == TileType::Vec) || ...`），但常规做法是向量用 NoneBox、Cube 用分形——指令页会声明支持范围。

### 4.4 有效区域掩码：容量与有效的分离

#### 4.4.1 概念说明

有效区域回答「**这次操作真正关心左上角多少行 × 多少列**」。它解决的是边界问题：真实矩阵维度（比如 4096×100）不会恰好是 tile 尺寸（16×256）的整数倍。若没有有效区域，程序员只能为边界单独写一套「小 tile」代码或冒险越界；有了它，**一份内核 + 运行期两个整数**就能处理任意尾块。

关键规则（来自 [docs/coding/Tile_zh.md:61-66](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/docs/coding/Tile_zh.md#L61-L66)）：

- 有效区域是**连续前缀**：\(0 \le i < \text{validRow}\)、\(0 \le j < \text{validCol}\)，不是任意掩码；
- 区域外元素**未指定**（unspecified），除非指令显式定义 pad 行为；
- 指令语义一般按「对有效区域内每个元素」解释。

#### 4.4.2 核心流程

有效区域的「静态/动态」二态由模板参数是否为 `DYNAMIC` 决定，两条路径在编译期分岔：

```text
RowValid_ 是正整数？
 ├─ 是 → GetValidRow() 是 static constexpr，直接返回常量
 │        构造函数只能用无参版本
 └─ 否(-1) → 值存进成员 RowMaskInternal
          → 构造函数用 SFINAE 提供有参版本（单参/双参，取决于几个维度是动态）
          → 运行期还可用 SetValidRow/SetValidCol/SetValidShape 修改
```

指令侧的消费模式（以 CPU 后端为例）统一为「取 dst 的有效区域 → 双重循环只扫有效区」：

- `TADD_IMPL` 取 `dst.GetValidRow()/GetValidCol()`，并断言三个 tile 的有效区域一致（[TAdd.hpp:63-75](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/cpu/TAdd.hpp#L63-L75)）；
- `TLOAD` 先把**整个容量**填成 pad 值，再只搬有效区（[TLoad.hpp:134-159](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/cpu/TLoad.hpp#L134-L159)）；
- `TSTORE` 只写有效区，区域外一个字节都不碰（[TStore.hpp:51-78](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/cpu/TStore.hpp#L51-L78)）。

这就构成了「**掩码即边界保护**」的完整闭环。

#### 4.4.3 源码精读

存储与查询，[include/pto/common/pto_tile.hpp:1588-1613](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L1588-L1613)：两个 `unsigned` 成员 `RowMaskInternal/ColMaskInternal`；`GetValidRow()` 有两个重载——静态版 `enable_if_t<RowMask > 0>` 返回编译期常量，动态版 `enable_if_t<RowMask == DYNAMIC>` 读成员。调用方代码完全一致，重载决议在编译期完成。

运行期修改接口（注释提醒调用需要 PIPE_S 等待，属于标量流水线操作）：

[include/pto/common/pto_tile.hpp:1615-1638](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L1615-L1638) 提供 `SetValidRow/SetValidCol/SetValidShape`，每个都用 `static_assert(ValidRow == DYNAMIC, ...)` 拦截「对静态掩码赋值」的误用，并用 `PTO_ASSERT(rowMask <= Rows)` 拦截运行期越界。

构造函数族（SFINAE 精选重载）：

[include/pto/common/pto_tile.hpp:1469-1499](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L1469-L1499) 依次给出「双动态 `Tile(VR, VC)`」「行动态 `Tile(VR)`」「列动态 `Tile(VC)`」三个构造，`enable_if_t` 条件保证只有对应的维度组合才暴露该重载——传错个数直接编译失败而不是留下未初始化的掩码。

真实工程用法，[tests/cpu/st/testcase/tadd/tadd_kernel.cpp:22-25](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L22-L25)：

```cpp
using TileData = Tile<TileType::Vec, T, kTRows_, kTCols_, BLayout::RowMajor, -1, -1>;
TileData src0Tile(kTRows_, kTCols_);
```

模板里写 `-1, -1`（即 DYNAMIC），构造时把容量值填成运行期有效值——tadd 用例里两者恰好相等，但机制上已经允许不同，这正是下一节实践要利用的自由度。

TSTORE 的掩码消费（本讲实践的验证点），[include/pto/cpu/TStore.hpp:51-78](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/cpu/TStore.hpp#L51-L78)：

```cpp
const size_t validRow = src.GetValidRow();
const size_t validCol = src.GetValidCol();
...
for (size_t row = 0; row < validRow; ++row) {
    for (size_t col = 0; col < validCol; ++col) {
        ...
        dst.SetElement(dstOffset, dstVal);
    }
}
```

循环上界是有效区域而非 `Rows/Cols`——「GM 中有效区外的数据不受影响」的直接实现位置就在这里。TLOAD 的对应逻辑在 [TLoad.hpp:154-159](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/cpu/TLoad.hpp#L154-L159)，且它在此之前先用 `getPadValue()`（[TLoad.hpp:20-50](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/cpu/TLoad.hpp#L20-L50)，`PadValue::Null/Zero` 都映射为 0）把整个容量填成 pad。

#### 4.4.4 代码实践（本讲主实践）

**任务**：定义 RowMajor 16x256 的 float Tile，用动态 validRows/validCols 只填充 10x100 的有效区域，TSTORE 后检查 GM 中有效区外的数据不受影响。

思路：GM 侧开一块 16x256 的缓冲并预填哨兵值；`GlobalTensor` 的**窗口形状**设为 (10,100)、行步长 256（沿用 u2-l2 的「窗口 Shape 描述搬运块、Stride 描述走法」）；Tile 侧容量 16x256、掩码动态。由于 TLOAD/TSTORE 的 ND 断言要求「GM 窗口形状 == Tile 有效区域」（[TLoad.hpp:71-81](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/cpu/TLoad.hpp#L71-L81)、[TStore.hpp:22-39](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/cpu/TStore.hpp#L22-L39)），两侧自然对齐。

1. **实践目标**：亲眼看到 TSTORE 只写 10x100 的窗口；把「掩码」从概念变成可观测行为。

2. **操作步骤**（在自己的工作副本里做练习，不要提交到仓库）：

   a. 复制一个新 ST 用例目录并注册（ST 框架见 u1-l4，四件套结构）：

   ```bash
   cp -r tests/cpu/st/testcase/tadd tests/cpu/st/testcase/tadd_mask
   mv tests/cpu/st/testcase/tadd_mask/tadd_kernel.cpp tests/cpu/st/testcase/tadd_mask/tadd_mask_kernel.cpp
   ```

   然后在 [tests/cpu/st/testcase/CMakeLists.txt:39](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/CMakeLists.txt#L39) 的 `ALL_TESTCASES` 列表里 `tadds` 之后加一行 `tadd_mask`（`pto_cpu_sim_st` 函数按「目录名自动拼 `<name>_kernel.cpp`」查找源文件，见 [CMakeLists.txt:11-15](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/CMakeLists.txt#L11-L15)）。

   b. 重写内核（`tadd_mask_kernel.cpp`，**示例代码**）：

   ```cpp
   #include <pto/pto-inst.hpp>
   using namespace pto;

   // 大缓冲 16x256；本次只处理左上角 10x100 的窗口
   template <typename T, int kBufRows_, int kBufCols_, int kValidRows_, int kValidCols_>
   AICORE void runTAddMask(__gm__ T __out__* out, __gm__ T __in__* src0, __gm__ T __in__* src1)
   {
       using BufShape  = Shape<1, 1, 1, kValidRows_, kValidCols_>;   // 窗口形状 = 有效区域
       using BufStride = Stride<1, 1, 1, kBufCols_, 1>;              // 行步长 = 大缓冲列数
       using GlobalData = GlobalTensor<T, BufShape, BufStride>;
       using TileData = Tile<TileType::Vec, T, 16, 256, BLayout::RowMajor, -1, -1>;
       static_assert(TileData::Cols * sizeof(T) % 32 == 0);          // 256*4=1024B，满足对齐

       TileData src0Tile(kValidRows_, kValidCols_);                  // 动态掩码 = (10,100)
       TileData src1Tile(kValidRows_, kValidCols_);
       TileData dstTile(kValidRows_, kValidCols_);
       TASSIGN(src0Tile, 0x0);
       TASSIGN(src1Tile, 0x4000);
       TASSIGN(dstTile, 0x8000);

       GlobalData src0Global(src0);
       GlobalData src1Global(src1);
       GlobalData dstGlobal(out);

       TLOAD(src0Tile, src0Global);
       TLOAD(src1Tile, src1Global);
       set_flag(PIPE_MTE2, PIPE_V, EVENT_ID0);
       wait_flag(PIPE_MTE2, PIPE_V, EVENT_ID0);
       TADD(dstTile, src0Tile, src1Tile);
       set_flag(PIPE_V, PIPE_MTE3, EVENT_ID0);
       wait_flag(PIPE_V, PIPE_MTE3, EVENT_ID0);
       TSTORE(dstGlobal, dstTile);
   }

   template void LaunchTAddMask<float, 16, 256, 10, 100>(float* out, float* src0, float* src1, void* stream);
   ```

   （`LaunchTAddMask` 的声明/转发照抄 tadd 的 [tadd_kernel.cpp:45-52](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/tadd/tadd_kernel.cpp#L45-L52) 写一个壳即可。）

   c. 修改 `main.cpp`（关键一步）：原版在 [tadd/main.cpp:59](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/tadd/main.cpp#L59) 把 dst 清零。清零会让实验失真——若 TSTORE 真的越界写了 pad 值，pad 恰好也是 0，你分辨不出来。改为**预填非零哨兵**：malloc 一块 host 缓冲填 `-12345.0f`，`aclrtMemcpy` 到 dstDevice 替代那行 `aclrtMemset`。TEST_F 用例写成 `case_float_mask_16x256_10x100`（注意「TEST_F 用例名 = 数据目录名」的约定，见 u1-l4）。

   d. 修改 `gen_data.py`：input1/input2 仍是 16x256 随机数（有效区外随便填，反正不会被读进有效计算）；golden 在 10x100 窗口内 = input1+input2，窗口外 = `-12345.0f`。

   e. 运行：`python3 tests/run_cpu.py -t tadd_mask --verbose`（构建细节：`-D__CPU_SIM` 由 [tests/cpu/st/CMakeLists.txt:32](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/CMakeLists.txt#L32) 统一定义）。

3. **需要观察的现象**：用例通过。再做一个反事实实验：把内核里 `TileData src0Tile(kValidRows_, kValidCols_)` 的构造参数换成 `(16, 256)`（掩码=容量），重新生成 golden（此时 TADD 会把 pad 相加、TSTORE 会写满 16x256），用例**应当失败**——这证明上一步的通过确实来自掩码，而非巧合。

4. **预期结果**：掩码版通过、满容量版失败，两者对照说明 TSTORE 的写入范围由 `GetValidRow()/GetValidCol()` 决定（实现位于 [TStore.hpp:64-78](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/cpu/TStore.hpp#L64-L78)）。**本实践的运行结果待本地验证**（我未执行编译运行；若 `CheckTileData` 断言报形状不匹配，请核对 GM 窗口形状是否与 Tile 有效区域完全一致）。

#### 4.4.5 小练习与答案

- **练习 1**：`Tile<Vec, float, 16, 256, RowMajor, DYNAMIC, DYNAMIC>` 分别能用 `Tile t(10)`、`Tile t(10, 100)`、`Tile t()`、`SetValidCol(100)` 中的哪些？
  **答案**：`Tile(10, 100)` 可以（双动态构造，[pto_tile.hpp:1469-1479](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L1469-L1479)）；`Tile()` 可以（掩码成员未设初值，随后必须 Set）；`Tile(10)` 不行（单参构造要求恰好一个动态维度）；`SetValidCol(100)` 可以（[pto_tile.hpp:1623-1629](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L1623-L1629)）。
- **练习 2**：为什么 `SetValidRow` 的注释写着「Call this function need PIPE_S wait」？
  **答案**：掩码是 Tile 对象的**运行期成员**，写它走标量流水线（PIPE_S）。若向量/搬运流水线同时在读旧掩码，需要事件保证顺序——CPU 模拟器上事件是空操作（u1-l5），所以这类时序错误只有上 NPU 才暴露。
- **练习 3**：TLOAD 为什么先把整个容量填 pad 再搬有效区，而不是只搬有效区？
  **答案**：让「有效区外的元素」有确定值而非残留旧数据——后续按容量扫描的指令（或被错误地满容量处理）读到的是可预期的 pad（`PadValue::Null/Zero → 0`，[TLoad.hpp:23-31](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/cpu/TLoad.hpp#L23-L31)），这同时是 `PadValue::Min/Max`（softmax 里减最大值前的填充）能用同一机制实现的基础。

### 4.5 对齐与分形约束：编译期 static_assert 防线

#### 4.5.1 概念说明

昇腾硬件的 DMA 与向量单元按 32 字节为最小搬运/对齐单位（u2-l1 讲过 `C0_SIZE_BYTE=32`、每块 16 元素等「度量衡」）。如果允许 `Tile<Vec, float, 16, 6>` 这种 24 字节一行的 Tile，运行时每次搬运都要硬件做非对齐修正，慢且部分代际根本不支持。PTO 的选择是：**把硬件约束翻译成 `static_assert`，让非法形状在编译期就死掉**。这就是「形状静态化把检查前移」在布局维度的体现。

#### 4.5.2 核心流程

Tile 上的约束按布局类型分三道闸门（全部在 [pto_tile.hpp:1515-1538](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L1515-L1538)）：

1. **容量合法性**：`Rows > 0`、`Cols > 0`、`ValidRow ≤ Rows`、`ValidCol ≤ Cols`；
2. **未盒化行主**：\( \text{Cols} \times \text{sizeof}(T) \equiv 0 \pmod{32} \)；**未盒化列主**：\( \text{Rows} \times \text{sizeof}(T) \equiv 0 \pmod{32} \)；
3. **盒化**：`Rows % InnerRows == 0` 且 `Cols % InnerCols == 0`（Vec 位置有豁免）；
4. **SFractalSize 只能是 512/1024/32 三档**。

对最小列数的直觉：float（4B）至少 8 列（行主）、half（2B）至少 16 列、int8 至少 32 列。

#### 4.5.3 源码精读

对齐常量与基块档位，[include/pto/common/pto_tile.hpp:1078-1087](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L1078-L1087)：

```cpp
namespace TileConfig {
static constexpr int alignedSize = 32;      // 32 字节对齐
static constexpr int fixedRowSize = 16;
static constexpr int fixedColSize = 16;
static constexpr int fractalABSize = 512;   // A/B 操作数基块
static constexpr int fractalCSize = 1024;   // 累加器基块
static constexpr int fractalMxSize = 32;    // MX scale 基块
}
```

核心对齐断言，[include/pto/common/pto_tile.hpp:1522-1533](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L1522-L1533)——错误消息里直接写明三种情况各自的合法条件：

```cpp
static_assert(
    (BFractal_ == BLayout::RowMajor && SFractal_ == SLayout::NoneBox &&
     Cols * sizeof(DType) % TileConfig::alignedSize == 0) ||
        (BFractal_ == BLayout::ColMajor && SFractal_ == SLayout::NoneBox &&
         Rows * sizeof(DType) % TileConfig::alignedSize == 0) ||
        (SFractal_ != SLayout::NoneBox) && (...),
    "BFractal_ is RowMajor and SFractal_ is NoneBox: Rows must be 32 bytes align, ...");
```

可整除断言在 [pto_tile.hpp:1515-1520](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L1515-L1520)。与之呼应，GM 侧的 NZ 布局在 [pto_tile.hpp:683-699](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L683-L699) 的 `TileShape2D<NZ>` 也断言 `rows % FRACTAL_NZ_ROW == 0`（16）与 `cols % C0Size == 0`——两个常量来自 [constants.hpp:34-35](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/constants.hpp#L34-L35)。低精度扩展同理：[constants.hpp:39-45](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/constants.hpp#L39-L45) 定义 MX（16×2，基块 32B）与 HIF4（16×4，基块 64B）的分形参数，`TileShape2D<BaseShape2D` 的各 MX/HIF4 特化（[pto_tile.hpp:823-1076](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/common/pto_tile.hpp#L823-L1076)）逐一带 `cols % C0Size == 0` 之类断言。

#### 4.5.4 代码实践

1. **实践目标**：亲手触发各类断言并会读错误消息。
2. **操作步骤**：在一个 `__CPU_SIM` 编译单元里逐个试（每次只留一个）：

   | 写法 | 预期触发 |
   | --- | --- |
   | `Tile<Vec, float, 16, 6>` | 32 字节对齐断言（1522 行） |
   | `Tile<Vec, float, 16, 256, RowMajor, 20, 100>` | `ValidRow <= Rows` 断言（1433 行） |
   | `TileLeft<half, 8, 16>` | 基块可整除断言（1516 行） |
   | 第 9 个模板参数填 `128` | `SFractalSize_ illegal`（1535 行） |
   | `Tile<Vec, float, 16, 256, RowMajor, 16, 256>` 静态掩码 + `SetValidRow(10)` | `Only Dynamic Valid Row Support Set Value.`（1618 行） |

3. **需要观察的现象**：每条错误消息都能在上述行号找到原文，且消息内容直接描述修法。
4. **预期结果**：5 条全部编译失败，报错定位与表格一致。**待本地验证**。

#### 4.5.5 小练习与答案

- **练习 1**：`Tile<Vec, int8_t, 16, 24>` 合法吗？怎么改？
  **答案**：不合法。行主下列字节数 24×1=24 不是 32 的倍数。改成 32 列（或任何 32 的倍数）即可。
- **练习 2**：`Tile<Vec, half, 16, 16>` 与 `Tile<Vec, half, 16, 16, BLayout::ColMajor>` 哪个合法？
  **答案**：都合法：行主看 Cols（16×2B=32B ✓），列主看 Rows（16×2B=32B ✓）。这解释了 tadd 的 half 用例为什么恰好是 16×256/16×16 这类尺寸。
- **练习 3**：为什么 32 字节对齐检查放在 Tile（片上侧）而不是 GlobalTensor（GM 侧）？
  **答案**：GM 侧数据布局由框架决定，可以是任意物理排布（PTO 用 Shape/Stride 描述走法即可）；Tile 是 PTO 自己规划的一等公民，其存储直接喂给向量/Cube 单元，必须满足硬件对齐。两侧约束的不对称是「GM 灵活、片上严格」设计的自然结果。

## 5. 综合实践

**任务：给 4.4 节的掩码内核补一张「布局+掩码」全景说明图，并做一次跨布局迁移实验。**

1. 画出你的 `tadd_mask` 内核中三块 Tile 的存储示意图：标出 16×256 容量、10×100 有效区、每行 1024 字节对齐、GM 缓冲里窗口外的哨兵区；在图上用箭头标出 TLOAD 读的范围、TADD 算的范围、TSTORE 写的范围（三者都应是同一个 10×100 前缀）。
2. 迁移实验：把内核改成 `half`（`Tile<Vec, half, 16, 256>`，half 时 256×2B=512B 仍满足对齐），有效区保持 10×100，重新生成 golden 并跑通；记录 float 与 half 两版的输出误差（main.cpp 的 `ResultCmp` 容差是 0.001，见 [tadd/main.cpp:89](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/tests/cpu/st/testcase/tadd/main.cpp#L89)——half 下可能需要放宽，说明为什么）。
3. 进阶（选做）：把有效区域改成「行动态、列静态」（模板 `RowValid_ = -1, ColValid_ = 256`，构造 `Tile(10)`），验证单参构造可用，并对比 golden 是否与双动态版完全一致。

预期：三步都完成后，你就同时用到了本讲的五个模块——模板参数、BLayout、（half 下的）对齐换算、动态掩码的两种粒度、以及掩码在 TLOAD/TADD/TSTORE 三处的消费链路。**待本地验证。**

## 6. 本讲小结

- Tile 的 11 个模板参数分四组：**位置**（TileType→物理存储）、**容量**（Rows/Cols，必须静态）、**布局**（BLayout 外层 + SLayout/SFractalSize 分形）、**有效区域**（RowValid/ColValid，静态或 DYNAMIC）；不写有效区域时默认等于容量。
- **容量与有效区域是两套概念**：容量决定硬件预留和地址规划，有效区域决定本次操作的真实边界；有效区域是左上角连续前缀，不是位掩码。
- 动态掩码通过 `enable_if_t` 构造函数族或 `SetValidRow/SetValidCol/SetValidShape` 注入，存进 `RowMaskInternal/ColMaskInternal`；`GetValidRow/GetValidCol` 用重载在编译期分流静态/动态两条路径。
- 布局是两层的：`BLayout` 决定块间（或未盒化时的元素间）排布，`SLayout+SFractalSize` 决定基块（512B/1024B/32B 档）与块内排布；`TileLeft/TileRight/TileAcc` 是把平台差异藏起来的别名（CPU 上 Nz/Zn/fractalCSize）。
- **约束全部前移到编译期**：32 字节对齐、基块可整除、容量≥有效、SFractalSize 三档——非法 Tile 直接编译失败，错误消息即修法。
- 掩码的消费闭环在 CPU 后端清晰可见：TLOAD 填 pad 后只搬有效区 → 计算指令只扫有效区 → TSTORE 只写有效区（[TStore.hpp:64-78](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/cpu/TStore.hpp#L64-L78)）。

## 7. 下一步学习建议

- **下一讲 u2-l4（TASSIGN 与片上内存规划）**：本讲的 Tile 还悬空着（`data_` 未绑定），下一讲讲 `TASSIGN` 如何把 Tile 绑到 UB 偏移地址、`buffer_limits.hpp` 的容量约束，以及多 tile 的手工 UB 布局与乒乓规划——正好承接 4.4 实践里 `0x0/0x4000/0x8000` 三个魔数。
- **顺流而下（同步）**：u3-l1 事件模型。你会开始理解为什么 `SetValidRow` 需要走 PIPE_S。
- **顺流而下（Cube）**：u4-l5 Cube 指令与分形布局。本讲的 Nz/Zn/Zz、`TileLeft/TileRight/TileAcc` 将在 TMATMUL 的真实调用中复现。
- **延伸阅读**：[docs/coding/Tile_zh.md](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/docs/coding/Tile_zh.md) 的「示例」三节（基本 Tile/静态掩码/动态掩码）与本讲 4.4 实践互为印证；[include/pto/cpu/TFillPad.hpp](https://github.com/hw-native-sys/pto-isa/blob/0dbecbe7fc26631b615e843ee77d4745b70cee43/include/pto/cpu/TFillPad.hpp) 展示了「有效区域 + pad」更复杂的组合玩法。
