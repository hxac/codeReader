# Tile 编程模型：静态形状、动态掩码与数据组织

## 1. 本讲目标

上一讲我们学习了 `GlobalTensor`——它只是全局内存（GM）数据的一个"零拷贝视图"，本身不存任何数据。本讲进入数据的"落脚点"：**Tile**。学完本讲，你应该能够：

1. 说清 Tile 是什么：片上（on-chip）的固定容量 2-D 缓冲抽象，是 PTO 指令计算和搬运的基本单位。
2. 掌握 Tile 的"容量形状"（静态 `Rows × Cols`）与"有效区"（valid region / 掩码，静态或动态）这两套互相配合的形状系统。
3. 理解 `TileType::Vec`、`Left`、`Right`、`Acc` 等 tile 类型如何对应硬件上不同的片上存储（UB / L0A / L0B / accumulator）。
4. 理解数据在 tile 存储中的组织方式：基础布局（`BLayout`）、分形布局（`SLayout` + `SFractalSize`），以及 `GetTileOffset` 的地址映射规则。
5. 能独立写出带动态掩码的 Tile 声明，并解释掩码如何在尾块（tail block）场景下防止越界写回。

## 2. 前置知识

- **片上存储层级**：昇腾 AI Core 内部有多级存储。与 PTO Tile 相关的主要是：UB（Unified Buffer，向量流水线使用）、L1（矩阵 tile）、L0A/L0B（矩阵乘操作数缓冲）、以及矩阵乘的累加器。它们都比 GM（Global Memory，DDR）小得多但快得多。你不需要记住每一级，只要记住："**不同 TileType 对应不同物理存储**"。
- **静态 vs 动态**：C++ 模板参数在编译期确定（静态），构造函数实参在运行期确定（动态）。PTO 的哲学是：**容量尽量静态**（便于编译期特化优化），**有效区允许动态**（应对运行时才知道的总长度）。
- **`DYNAMIC`（-1）**：上一讲已见过的哨兵值，表示"该维度编译期未知，运行时再填"。
- **尾块问题**：假设总数据 1000 行、每 tile 64 行，最后一个 tile 只有 1000 − 15×64 = 40 行是有效的。如果指令无视这一点把 64 行全部计算并写回 GM，就会把垃圾数据写进不属于你的内存区域——这就是掩码要解决的问题。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [docs/coding/Tile.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/Tile.md) | Tile 编程模型的官方文档：五族属性、有效区、布局约束的权威说明 |
| [include/pto/common/pto_tile.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp) | Tile 模板类的真实实现（也包含上一讲的 GlobalTensor / Shape / Stride） |
| [include/pto/common/type.hpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/type.hpp) | `TileType` / `BLayout` / `SLayout` / `PadValue` 等枚举定义 |
| [demos/baseline/add/csrc/kernel/add_custom.cpp](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/kernel/add_custom.cpp) | Add 算子 NPU 版，展示了"静态形状 + 动态掩码"Tile 的真实工程用法 |

## 4. 核心概念与源码讲解

### 4.1 Tile 抽象：一片固定容量的片上 2-D 缓冲

#### 4.1.1 概念说明

PTO 程序操作的基本单位是 **Tile**：一个固定容量的 2-D 缓冲。概念上它住在片上存储（类似寄存器堆或 SRAM）里，通过 `TLOAD` 从 GM 搬入、通过 `TSTORE` 写回 GM。在 CPU 仿真后端上 Tile 实际存放在宿主机内存中，但**形状与布局规则保持一致**，因此同一份代码可以先在 CPU 上验证逻辑。

一个 Tile 由五族属性完整定义（见文档 [docs/coding/Tile.md:L9-L17](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/Tile.md#L9-L17)，这段列出了 Location、元素类型、容量形状、布局、有效区五族属性）：

| 属性 | 含义 | 典型取值 |
| --- | --- | --- |
| Location（`TileType`） | 逻辑/物理存储类别 | `Vec` / `Mat` / `Left` / `Right` / `Acc` / `Bias` / `Scaling` |
| Element type | 标量元素类型 | `half`、`float`、`int8_t`… |
| Capacity shape | 编译期容量 `Rows × Cols` | 如 `64 × 64` |
| Layout | 基础布局 + 可选分形布局 | `BLayout::RowMajor` + `SLayout::NoneBox` |
| Valid region | 有效行/列数 | 静态值或 `DYNAMIC` |

#### 4.1.2 核心流程

一个 Tile 的生命周期：

```text
① 声明类型（编译期）：确定 Loc / 元素类型 / 容量 / 布局 / 掩码是否动态
② 构造对象（运行期）：若是动态掩码，传入 (vRows, vCols) 存入对象
③ TASSIGN 绑定地址：把 tile 绑到具体的片上缓冲地址（Manual 模式）
④ TLOAD 搬入 → 指令计算（只保证有效区内语义）→ TSTORE 写回
```

#### 4.1.3 源码精读

Tile 的模板声明在 [include/pto/common/pto_tile.hpp:L1389-L1394](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L1389-L1394)，这段定义了 `Tile` 的完整模板参数表：位置 `Loc_`、元素 `Element_`、容量 `Rows_`/`Cols_`、基础布局 `BFractal_`、有效区 `RowValid_`/`ColValid_`、分形布局 `SFractal_`/`SFractalSize_`、填充策略 `PadVal_` 与紧凑模式 `Compact_`。

`TileType` 枚举定义在 [include/pto/common/type.hpp:L121-L132](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/type.hpp#L121-L132)，这段列出了全部九种 tile 位置类型（比文档多出 `ScaleLeft`/`ScaleRight`/`Ctrl`，用于 MX 缩放因子和控制类 tile）。对照文档 [docs/coding/Tile.md:L38-L48](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/Tile.md#L38-L48) 的解释：

- `Vec`：向量 tile 存储（UB / 向量流水线），逐元素指令（TADD、TMUL…）的操作数。
- `Mat`：通用矩阵 tile 存储（Matrix L1）。
- `Left` / `Right`：矩阵乘操作数 tile（对应 L0A / L0B）。
- `Acc`：矩阵乘累加器 tile。
- `Bias` / `Scaling`：matmul/搬运路径的辅助 tile（偏置、MX 缩放因子）。

`TileType` 参与指令的**重载选择与编译期检查**——每条指令的 ISA 文档（`docs/isa/`）会写明它接受哪些位置。例如 [include/pto/common/pto_tile.hpp:L1443-L1455](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L1443-L1455) 的 `SetValue`/`GetValue` 就用 `static_assert(Loc == TileType::Vec, ...)` 强制只允许向量 tile 逐元素读写。

#### 4.1.4 代码实践

**实践目标**：确认"指令接受哪些 TileType"是可查、可验证的约束。

**操作步骤**：

1. 打开 [include/pto/common/type.hpp:L121-L132](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/type.hpp#L121-L132)，抄下九种 TileType。
2. 打开 [docs/isa/TADD.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TADD.md)，找到它对操作数 tile 位置的要求。
3. 再看 [docs/isa/TMATMUL.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TMATMUL.md) 的位置要求，与 TADD 对比。

**需要观察的现象**：TADD 这类逐元素指令只吃 `Vec` tile；TMATMUL 则要求 `Left`/`Right`/`Acc` 组合。

**预期结果**：你能列出一个"指令 × TileType"的小对照表。具体每条指令接受哪些位置以 `docs/isa/*` 为准（待本地验证：请在本地翻阅这两份 ISA 文档核对细节）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 PTO 不设计一个"万能 Tile"让所有指令通用？

**答案**：因为不同 TileType 对应不同物理存储与流水线（向量单元用 UB，矩阵乘用 L0A/L0B/累加器）。把位置编码进类型系统，编译器能在编译期通过重载和 `static_assert` 拒绝非法组合（比如把 `Acc` tile 喂给 TADD），而不是到真机上才出错。

**练习 2**：`TileType::Left` 和 `TileType::Right` 分别对应矩阵乘的哪个操作数？

**答案**：`Left` 对应矩阵乘左操作数 A（L0A 缓冲），`Right` 对应右操作数 B（L0B 缓冲），`Acc` 存结果累加。

---

### 4.2 tile 形状与掩码：容量与有效区的双轨制

#### 4.2.1 概念说明

Tile 有两套"形状"，初学者最容易混淆：

- **容量形状（capacity shape）**：`Rows_ × Cols_`，tile 缓冲的物理容量，**编译期静态**。指令实现按它做特化与优化。
- **有效区（valid region）**：`(valid_row, valid_col)`，本次操作中真正有意义的元素范围，**可以静态也可以动态**。

有效区永远是一个**连续前缀**（contiguous prefix）：合法下标满足 `0 <= i < valid_row` 且 `0 <= j < valid_col`（见 [docs/coding/Tile.md:L60-L66](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/Tile.md#L60-L66)，文档明确"指令语义一般按有效区内逐元素解释，有效区外的元素值未指定"）。也就是说掩码不是任意的 0/1 掩码矩阵，而是"前 valid_row 行 × 前 valid_col 列"的矩形。

双轨制的动机：

1. **性能**：容量静态 → 编译期特化，无需运行时判断缓冲多大。
2. **正确性**：真实问题里总长度（如 `totalLength`）往往运行时才知道，尾块大小随之变化 → 有效区必须支持动态。
3. **防越界**：指令只对有效区内的元素做计算与写回，尾块中"多出来的行/列"既不会污染计算结果，也不会经 TSTORE 写出到 GM，越界风险被挡在掩码这一层。

#### 4.2.2 核心流程

静态与动态两条路径（`DYNAMIC = -1` 定义于 [include/pto/common/pto_tile.hpp:L28](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L28)）：

```text
静态有效区：RowValid_ = 64（等于或不等于 Rows_ 的编译期常量）
    └─ GetValidRow() 是 constexpr，直接返回 RowMask 模板值

动态有效区：RowValid_ = -1 (DYNAMIC)
    ├─ 构造时：Tile t(vRows, vCols);  → 存入 RowMaskInternal / ColMaskInternal
    ├─ 运行中：t.SetValidRow(m) / t.SetValidCol(n) / t.SetValidShape(m, n)
    └─ 查询：t.GetValidRow() / t.GetValidCol() 读对象内的 unsigned 成员
```

查询接口与上一讲 GlobalTensor 一样是双轨的：静态时 `GetValidRow()` 走 `static constexpr` 重载，动态时走成员函数重载——两者同名，编译器按 `RowValid_` 是否为 `DYNAMIC` 自动选择（SFINAE 约束）。

#### 4.2.3 源码精读

**① 静态常量与合法性检查**。[include/pto/common/pto_tile.hpp:L1424-L1432](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L1424-L1432) 定义了 `Loc`、`Rows`、`Cols`、`ValidRow`、`ValidCol` 等静态常量，并用 `static_assert(Rows > 0 && ValidRow <= Rows && Cols > 0 && ValidCol <= Cols, "Invalid Tile Layout.")` 在编译期强制"有效区不得超过容量"——这是掩码安全性的第一道闸门。

**② 动态掩码的存储**。[include/pto/common/pto_tile.hpp:L1579-L1580](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L1579-L1580) 声明了两个 `unsigned` 成员 `RowMaskInternal` / `ColMaskInternal`，动态有效区的值就存在这里。

**③ 动态构造函数**。[include/pto/common/pto_tile.hpp:L1467-L1498](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L1467-L1498) 提供了三个运行期构造函数：双动态 `(VR, VC)`、仅行动态 `(VR)`、仅列动态 `(VC)`，均通过 `std::enable_if_t` 约束"对应模板参数确实为 DYNAMIC"才可用——如果你给静态有效区的 Tile 传运行时掩码，会直接编译失败。

**④ 查询与设置**。[include/pto/common/pto_tile.hpp:L1582-L1604](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L1582-L1604) 是 `GetValidRow`/`GetValidCol` 的静态/动态双版本；[include/pto/common/pto_tile.hpp:L1607-L1629](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L1607-L1629) 是 `SetValidRow`/`SetValidCol`/`SetValidShape`，它们用 `static_assert(ValidRow == DYNAMIC, ...)` 保证只有动态 tile 才能运行时改掩码，并用 `PTO_ASSERT(rowMask <= Rows, ...)` 做运行期兜底。注意源码注释"Call this function need PIPE_S wait"——在真机上改掩码要等标量流水线，避免与其他流水线竞争。

**⑤ 真实工程用法（Add 算子 NPU 版）**。[demos/baseline/add/csrc/kernel/add_custom.cpp:L54-L63](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/kernel/add_custom.cpp#L54-L63) 是"静态形状 + 动态掩码"的教科书式写法：

```cpp
// define TileData on UB buffer with static shape and dynamic mask
using TileData = Tile<TileType::Vec, T, tileSRows, tileSCols, BLayout::RowMajor, -1, -1>;
...
// valid mask(vRows, vCols) of each tile
unsigned vRows = tileRows / AscendC::GetBlockNum();
unsigned vCols = bLength / tileNum / BUFFER_NUM;
TileData xTiles[BUFFER_NUM] = {TileData(vRows, vCols), TileData(vRows, vCols)};
```

容量 `tileSRows × tileSCols` 编译期定死，而 `totalLength` 是 kernel 的运行期入参，由此算出的 `vRows`/`vCols` 作为动态掩码在构造时传入。随后 [demos/baseline/add/csrc/kernel/add_custom.cpp:L92-L106](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/kernel/add_custom.cpp#L92-L106) 中，TLOAD 只搬有效区、TADD 只算有效区、TSTORE 只写有效区——尾块越界问题就此消解。

**⑥ 掩码如何"保护越界写入"——机制拆解**：TSTORE 的语义是"把 tile 有效区内的元素写回 GM 视图"。设某 tile 容量为 \( R \times C \)，动态掩码为 \( (v_r, v_c) \)（\( v_r \le R,\ v_c \le C \)），则写回的元素集合是：

\[ \{\, (i, j) \mid 0 \le i < v_r,\ 0 \le j < v_c \,\} \]

第 \( i \) 行（\( i \ge v_r \)）的元素**根本不进入写回集合**，因此即使 GM 视图后面没有足够空间、或那片内存属于别的核，也不会被写坏。同理 TADD 等计算指令只在有效区内定义结果，无效区的"垃圾"不会扩散到有效结果里（个别指令显式定义了 padding 行为时除外，见 `PadValue`）。

#### 4.2.4 代码实践

**实践目标**：亲手写出一个 64×64 的 float16 Tile 声明（静态版与动态掩码版各一个），并解释掩码保护。

**操作步骤**（示例代码，非项目原有代码，仿照 add_custom.cpp 的写法）：

```cpp
#include <pto/pto-inst.hpp>
using namespace pto;

// 版本 A：全静态，有效区 = 容量 = 64x64
using TileStatic = Tile<TileType::Vec, half, 64, 64, BLayout::RowMajor>;
TileStatic a, b, c;          // GetValidRow() == 64，编译期常量

// 版本 B：容量 64x64 静态，有效区动态（-1 即 DYNAMIC）
using TileMasked = Tile<TileType::Vec, half, 64, 64, BLayout::RowMajor, -1, -1>;
unsigned m = /* 运行时剩余行数，例如尾块只剩 40 行 */ 40;
TileMasked t(m, 64);          // 构造时传入动态 RowMaskInternal=40
// t.GetValidRow() == 40；TSTORE(t) 只会把前 40 行写回 GM
```

把它放进任一 CPU 仿真测试的 kernel 里（例如仿照 u1-l4 的 Add 示例改造），或临时写个只声明不运行的小翻译单元验证能编译。

**需要观察的现象**：

1. 版本 A 里调用 `t.SetValidRow(40)` 会**编译失败**（`static_assert(ValidRow == DYNAMIC)`）。
2. 版本 B 里传 `m = 100`（> 64）会在运行期触发 `PTO_ASSERT(rowMask <= Rows)`（CPU 仿真下）。
3. 用 CPU 仿真跑 TADD + TSTORE 时，若 `m = 40`，golden 数据只有前 40 行需要比对一致。

**预期结果**：三种行为都符合上述描述；掩码把"计算/写回范围"限制在容量内的连续前缀上。运行结果待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：总数据 1000 行、tile 容量 64 行、按 1 tile 循环处理，最后一个 tile 的 `vRows` 是多少？容量不变时掩码设成多少才安全？

**答案**：\( 1000 - \lfloor 1000/64 \rfloor \times 64 = 1000 - 960 = 40 \)。掩码 `vRows = 40`，容量仍是 64；指令只处理前 40 行，后 24 行不参与计算也不写回。

**练习 2**：为什么有效区被限制为"连续前缀"而不是任意掩码矩阵？

**答案**：前缀矩形可以被两个整数 `(valid_row, valid_col)` 唯一描述，硬件与仿真实现都能用简单的边界比较控制循环范围（成本低、可向量化）；任意掩码则需要逐元素的掩码位图，代价高且大多数尾块场景用不上。文档 [docs/coding/Tile.md:L60-L66](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/Tile.md#L60-L66) 明确采用了这一模型。

**练习 3**：`GetValidRow()` 在静态 tile 和动态 tile 上分别何时确定？

**答案**：静态 tile 上它是 `static constexpr` 函数，编译期返回模板参数 `RowValid_`；动态 tile 上它是普通成员函数，运行期返回对象内的 `RowMaskInternal`（构造或 `SetValidRow` 时填入）。

---

### 4.3 tile 存储：布局、分形与地址映射

#### 4.3.1 概念说明

Tile 的数据在片上缓冲里**不是只有"行优先一个数组"一种摆法**。PTO 用三个旋钮描述存储组织：

- **`BLayout`（基础布局）**：外层矩阵按 `RowMajor` 还是 `ColMajor` 解释（枚举见 [include/pto/common/type.hpp:L134-L137](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/type.hpp#L134-L137)）。
- **`SLayout`（分形/盒装布局）**：`NoneBox`（不分块）或内部再按 `RowMajor`/`ColMajor` 分成固定大小的"基础 tile"（fractal，枚举见 [include/pto/common/type.hpp:L139-L143](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/type.hpp#L139-L143)）。
- **`SFractalSize`（基础 tile 字节数）**：`TileConfig` 里给出了三档（[include/pto/common/pto_tile.hpp:L1077-L1086](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L1077-L1086)）：`fractalABSize = 512`（A/B 操作数常用）、`fractalCSize = 1024`（累加器常用）、`fractalMxSize = 32`（MX 缩放因子用）。

为什么需要分形布局？因为矩阵引擎（Cube）有固定的最佳访问粒度——它按固定大小的基础 tile 喂数据。把这一要求显式写进类型（文档 [docs/coding/Tile.md:L78-L96](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/Tile.md#L78-L96) 解释了动机：编译器早选合法布局、运行时避免慢速"修正"路径、同一份源码映射到不同代硬件），例如 512 字节基础 tile 在 fp16 下是 `16 × 16`，在 fp32 下是 `16 × 8`。

另外，Tile 的**物理存储本体**因后端而异：CPU 仿真/CostModel 下 `data_` 是一个可被 TASSIGN 重定向到共享仿真内存的**指针**；真机 NPU 下则是带存储限定符（如 `__ubuf__`）的类型，Auto 模式还会附加 `tile_size(Rows*Cols)` 属性。这些差异被 `#if defined(__CPU_SIM)` 等宏封装在同一个模板里（见 [include/pto/common/pto_tile.hpp:L1539-L1554](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L1539-L1554)，这段按宏选择 `TileDType` 的真身）。

#### 4.3.2 核心流程

给定逻辑坐标 `(row, col)`，元素在 tile 存储中的线性偏移由 [include/pto/common/pto_tile.hpp:L1824-L1849](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L1824-L1849) 的 `GetTileOffset` 计算：

```text
非盒装（NoneBox）：
    offset = row * RowStride + col * ColStride
    （RowMajor: RowStride=Cols, ColStride=1；ColMajor 反之）

盒装（fractal）：
    BlockRow = row / InnerRows    BlockCol = col / InnerCols
    InnerRow = row % InnerRows    InnerCol = col % InnerCols
    Nz 布局（外列主 + 内行主）：块序 = BlockNumRow*BlockCol + BlockRow，块内 = InnerRow*InnerCols + InnerCol
    Zn 布局（外行主 + 内列主）：块序 = BlockNumCol*BlockRow + BlockCol，块内 = InnerCol*InnerRows + InnerRow
    Zz 布局（外行主 + 内行主）：块序 = BlockNumCol*BlockRow + BlockCol，块内 = InnerRow*InnerCols + InnerCol
```

直觉图示（fp16、512B 分形、16×16 基础 tile 的 Nz 摆放）：一个 32×32 的 tile 被切成 2×2 = 4 个基础 tile，存储顺序是**逐基础 tile 列方向优先**（先摆 (0,0)、(1,0) 两块，再摆 (0,1)、(1,1)），每个基础 tile 内部再按行主序连续存放。这与朴素行主序完全不同，正是 Cube 引擎期望的喂料方式。

编译期还有一组硬约束（[include/pto/common/pto_tile.hpp:L1514-L1537](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L1514-L1537)，对应文档 [docs/coding/Tile.md:L111-L119](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/Tile.md#L111-L119)）：

- 非盒装 RowMajor：`Cols * sizeof(Element)` 必须是 32 字节的倍数（`TileConfig::alignedSize`）。
- 非盒装 ColMajor：`Rows * sizeof(Element)` 必须是 32 字节的倍数。
- 盒装：`Rows`/`Cols` 必须被 `InnerRows`/`InnerCols` 整除（部分 Vec tile 有例外）。

#### 4.3.3 源码精读

**① 内盒尺寸推导**。[include/pto/common/pto_tile.hpp:L1398-L1422](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L1398-L1422) 的 `getInnerRow`/`getInnerCol` 按 `SFractalSize_` 推导基础 tile 的行列数：1024B 累加器固定 16×16；512B 操作数按内布局取 `16 × (32B/sizeof(DType))` 或其转置；32B MX 缩放固定 16×2。推导出的 `InnerRows`/`InnerCols` 是后面整除断言和地址映射的基础（`isBoxedLayout`/`isInnerRowMajor` 等标签定义于 [include/pto/common/pto_tile.hpp:L1505-L1512](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L1505-L1512)）。

**② CPU 仿真下的存储与逐元素访问**。[include/pto/common/pto_tile.hpp:L1657-L1679](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L1657-L1679) 提供了 CPU 仿真专用的 `GetSizeInUnits`/`GetElement`/`SetElement`：`GetElement(r, c)` 内部正是调用 `GetTileOffset` 把逻辑坐标翻译成存储偏移——这是你验证"布局到底怎么摆"的最直接入口。

**③ 便捷别名**。矩阵乘相关 tile 不必手写九个模板参数，[include/pto/common/pto_tile.hpp:L1705-L1727](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L1705-L1727)、[L1729-L1737](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L1729-L1737)、[L1759-L1767](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L1759-L1767) 分别定义了 `TileLeft`、`TileRight`、`TileAcc`。注意 `TileLeft` 在 A2A3 与其他架构下的默认外布局不同（A2A3 用 RowMajor 外布局，CPU 仿真/新架构用 ColMajor 外布局，即文档所说的"Nz"），这正是"别名屏蔽架构差异"的实例：同一句 `TileLeft<half, 128, 128>` 在不同目标上自动选对合法布局。

**④ 类型探测工具**。[include/pto/common/pto_tile.hpp:L1769-L1821](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L1769-L1821) 的 `is_tile`/`is_global`/`is_boxed_tile`/`is_Nz_layout` 等 traits 让指令实现能 generic 地识别"这是不是 tile、是什么盒装布局"，是后面单元里读懂指令模板泛型代码的钥匙。

#### 4.3.4 代码实践

**实践目标**：验证 64×64 fp16 行主序 Vec tile 满足对齐约束，并通过 CPU 仿真的 `GetElement` 亲手确认非盒装布局的偏移公式。

**操作步骤**：

1. 检查约束：`64 (Cols) × sizeof(half)=2 → 128 字节`，是 `alignedSize=32` 的倍数 → 非盒装 RowMajor 合法。
2. 写一个只含声明的翻译单元（示例代码）：

```cpp
#include <pto/pto-inst.hpp>
using namespace pto;
// 合法：128 字节行宽，32 字节对齐
using Ok = Tile<TileType::Vec, half, 64, 64, BLayout::RowMajor>;
// 非法（预期编译失败）：33 列 * 2B = 66 字节，不是 32 的倍数
using Bad = Tile<TileType::Vec, half, 64, 33, BLayout::RowMajor>;
Ok a;
```

3. 在 CPU 仿真环境下编译（可用 `tests/run_cpu.py` 的构建 flags，或任何 `g++ -std=c++20 -D__CPU_SIM` 加上 include 路径的最小编译，具体命令待本地验证）。先注释掉 `Bad` 行确认 `Ok` 通过，再放开 `Bad` 观察错误。
4. 进阶（阅读型）：在 [include/pto/common/pto_tile.hpp:L1824-L1849](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L1824-L1849) 中手动代入 `row=2, col=5`，`RowStride=64, ColStride=1`，得 `offset = 2*64 + 5 = 133`。

**需要观察的现象**：`Bad` 那行触发 `static_assert`，报错文案即 [include/pto/common/pto_tile.hpp:L1521-L1532](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L1521-L1532) 中的"Rows must be 32 bytes align…"提示。

**预期结果**：合法声明编译通过、非法声明被编译期拒绝；手算偏移 133 与 `GetTileOffset` 代码推导一致。编译命令与输出待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：fp32 的 512 字节基础 tile（内行主序）是多大？fp8 呢？

**答案**：fp32：`512 / 4 = 128` 个元素，按 16 行 × 8 列摆；fp8：512 个元素，16 × 32（依据 [docs/coding/Tile.md:L88-L94](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/Tile.md#L88-L94)；内列主序则为转置）。

**练习 2**：`TileLeft`、`TileRight`、`TileAcc` 各自默认使用哪个 `SFractalSize`？

**答案**：`TileLeft`/`TileRight` 用 `fractalABSize`（512B），`TileAcc` 用 `fractalCSize`（1024B），见 [include/pto/common/pto_tile.hpp:L1705-L1767](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L1705-L1767) 中别名展开。

**练习 3**：CPU 仿真下 Tile 的 `data_` 与真机 NPU 下有何不同？为什么 TASSIGN 在 CPU 上"重定向指针"就能工作？

**答案**：CPU 仿真/CostModel 下 `TileDType = DType*`（普通指针，见 [include/pto/common/pto_tile.hpp:L1539-L1541](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/include/pto/common/pto_tile.hpp#L1539-L1541)），TASSIGN 只需把指针改指向仿真共享内存中模拟 UB/L1 的区域；真机下 `data_` 是带 `__ubuf__` 等存储限定符的类型，绑定地址走硬件机制。两者共享同一套模板与掩码/布局逻辑。

---

## 5. 综合实践

**任务：给"分块乘 2"算子补上尾块掩码**（综合本讲全部三个模块）。

1. 仿照 [demos/baseline/add/csrc/kernel/add_custom.cpp:L47-L63](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/demos/baseline/add/csrc/kernel/add_custom.cpp#L47-L63) 的骨架，写一个 kernel：GM 输入长度 `totalLength` 可变，tile 容量取 64×64 fp16（`TileType::Vec`、`BLayout::RowMajor`）。
2. 声明 Tile 时把 `RowValid_`/`ColValid_` 置为 `-1`，构造时根据 `totalLength` 计算并传入 `vRows`/`vCols`。
3. 指令序列：`TLOAD` → `TMUL`（乘标量 2 的变体，或 TADD 自身）→ `TSTORE`。
4. 验证：取 `totalLength` 分别为"恰好整除"（如 64×64×n）和"留尾块"（如 64×64×n + 1000）两组值，在 CPU 仿真下比对 golden。
5. 写一段说明：尾块那次迭代中 `vRows`/`vCols` 是多少，哪些行不会被 TSTORE 写出，为什么这保护了 GM 越界；同时说明你的 64×64 fp16 声明如何满足 32 字节对齐的 `static_assert`。

（若暂时无法搭建运行环境，可只完成第 1、2、5 步的代码与文字推导，标注"待本地验证"。）

## 6. 本讲小结

- Tile 是 PTO 的计算与搬运基本单位：片上固定容量 2-D 缓冲，`TileType` 决定它落在哪级物理存储（Vec→UB、Left/Right→L0A/L0B、Acc→累加器）。
- 形状是双轨制：**容量 `Rows × Cols` 编译期静态**（供特化优化），**有效区（掩码）可静态可动态**（`DYNAMIC=-1`，运行期经构造函数或 `SetValidShape` 填入 `RowMaskInternal`/`ColMaskInternal`）。
- 有效区是"容量内的连续前缀"矩形；指令只对有效区定义语义，TSTORE 只写回有效区——这是尾块防越界的核心机制，并有编译期 `ValidRow <= Rows` 与运行期 `PTO_ASSERT` 双重保险。
- 存储组织由 `BLayout`（外层行/列主序）+ `SLayout`/`SFractalSize`（分形盒装，512B/1024B/32B 三档）三旋钮决定；`GetTileOffset` 负责逻辑坐标 → 存储偏移的映射。
- 编译期 `static_assert` 强制 32 字节对齐与整除约束，把非法布局挡在编译阶段。
- `TileLeft`/`TileRight`/`TileAcc` 别名屏蔽了架构间默认布局差异（如 A2A3 与其他后端的 `TileLeft` 外布局不同）。

## 7. 下一步学习建议

Tile 里的数据从哪来、算完到哪去？下一讲 **u2-l3「事件与同步」** 会先讲清多流水线（MTE/Vector/Cube）之间如何用 set/wait flag 表达依赖——这是理解"为什么 TLOAD 之后要等一等才能算"的关键。随后单元三将进入 `TLOAD`/`TSTORE` 的指令语义与双实现精读。建议同步阅读：

- [docs/coding/Event.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/Event.md)
- [docs/coding/Tile.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/Tile.md) 的 "Address binding (`TASSIGN`)" 一节（[L135-L139](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/coding/Tile.md#L135-L139)）
- 想提前看 tile 绑定地址的指令细节，可翻 [docs/isa/TASSIGN.md](https://github.com/gitcode.com/cann/pto-isa/blob/8aacb8e0a0c636d291f2e4219d231b52e8003a8a/docs/isa/TASSIGN.md)
