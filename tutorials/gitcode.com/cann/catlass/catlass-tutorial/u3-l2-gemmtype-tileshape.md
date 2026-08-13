# GemmType 与 TileShape/Coord

## 1. 本讲目标

上一讲（u3-l1）我们建立了 Layout 抽象——它把逻辑坐标映射成线性偏移，是 GM 寻址的地基。但一个矩阵在 CATLASS 里不只有「怎么排布」，还有「元素是什么类型」「当前在哪一层存储」。本讲就把这三者绑在一起，学完后你应当能够：

- 读懂 `GemmType` 这个轻量类型绑定，理解它为什么把 `Element + Layout + Position` 打包成一个可传递的类型；
- 理解 `ElementAccumulatorSelector` 如何「根据 A、B 的输入类型自动决定累加精度」（例如 half 输入一定累加成 fp32）；
- 区分 `GemmShape`（编译期常量尺寸）与 `GemmCoord`（运行期坐标），掌握 `L1TileShape`/`L0TileShape` 的 `(M,N,K)` 含义；
- 能用矩阵乘理论模板的容量公式，手算验证一组 TileShape 是否放得下 L1/L0。

本讲是后续 Block 层（U4）、Tile 层（U5）的前置：BlockMmad 的模板参数里那一串 `AType/BType/CType` 与 `L1TileShape/L0TileShape`，正是本讲的主角。

## 2. 前置知识

- **C++ 模板特化（template specialization）**：CATLASS 大量用「主模板 + 针对特定类型的偏特化/全特化」来做编译期分发。例如给 `<half, half>` 写一个特化返回 `float`。看到 `static_assert(DEPENDENT_FALSE<...>, ...)` 的主模板，就表示「没有匹配的特化就报错」，这是 CATLASS 常见的「不支持的类型」守门写法。
- **类型即配置**：CATLASS 是纯头文件模板库，几乎所有「参数」都是编译期的 `using` 别名（类型），而不是运行期的变量。换类型 = 换行为。
- **昇腾存储层级（来自 u1-l2）**：数据沿 GM→L1→L0A/L0B→L0C→UB 内移，每层容量不同；L0C 按 fp32 累加是默认假设。
- **Layout（来自 u3-l1）**：`RowMajor`/`ColumnMajor` 等只描述排布、不持有数据。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/catlass/gemm/gemm_type.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/gemm_type.hpp) | 定义 `GemmType`——把 `Element + Layout + Position` 绑成一个结构体。全文件不到 30 行，却是整个 gemm 体系的「类型货币」。 |
| [include/catlass/gemm/helper.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/helper.hpp) | 类型选择器集合：`ElementAccumulatorSelector`（决定累加精度）、`L1ATypeSelector`/`L1BTypeSelector`（GM 布局→L1 布局）、`L1AndL0TypeSelectorGemm`、`TileShapeAlignChecker`（TileShape 对齐校验）等。 |
| [include/catlass/gemm_coord.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm_coord.hpp) | 定义 `GemmShape<M,N,K>`（编译期尺寸）与 `GemmCoord`（运行期三维坐标）。 |
| [include/catlass/arch/arch.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/arch/arch.hpp) | 各架构的存储容量常量（`L1_SIZE`/`L0A_SIZE`/`L0B_SIZE`/`L0C_SIZE`），是验证 TileShape 是否放得下的依据。 |
| [docs/zh/2_Design/01_kernel_design/04_matmul_summary.md](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/2_Design/01_kernel_design/04_matmul_summary.md) | 矩阵乘理论模板总结，给出 Common 模板的 Tiling 建模与各层容量约束公式。 |
| [examples/00_basic_matmul/basic_matmul.cpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp) | 第一个样例，展示了 `GemmType` 与 `GemmShape` 在真实组装中的用法。 |

## 4. 核心概念与源码讲解

### 4.1 GemmType 绑定：把「类型 + 布局 + 位置」打包

#### 4.1.1 概念说明

在 u3-l1 里，我们单独讲 Layout。但一个参与运算的矩阵，CATLASS 需要同时知道三件事：

1. **Element**：元素是什么类型？`half`、`float`、`int8_t`……不同类型位宽不同，影响「512 字节能装几个元素」「对齐粒度」。
2. **Layout**：逻辑坐标怎么排布？`RowMajor`、`ColumnMajor`、`nZ`、`zN`……
3. **Position**：这个矩阵现在位于哪一层存储？GM、L1、L0A、L0B、L0C、UB。同一份数据从 GM 搬到 L1 后，排布可能从 `RowMajor` 变成分形 `zN`、Position 也从 `GM` 变成 `A1`。

如果这三个属性各传一个模板参数，那么 `BlockMmad`、`TileCopy` 的参数列表会变得又长又容易写错。`GemmType` 的作用就是**把三者打包成一个结构体**，作为一个整体在模板链路里传递——它本身没有任何运行期数据、没有逻辑，纯粹是一个「类型容器」（type carrier）。

#### 4.1.2 核心流程

`GemmType` 的设计可以用一句话概括：**声明三个公开的类型/常量别名，让下游用 `typename T::Element` 这样取用**。

```
GemmType<Element_, Layout_, POSITION_ = GM>
   ├─ using Element  = Element_
   ├─ using Layout   = Layout_
   └─ static constexpr POSITION = POSITION_   // 默认 GM
```

- 上游（样例 host 代码）写 `using AType = Gemm::GemmType<half, layout::RowMajor>;`，Position 取默认值 GM。
- 下游组件用 `AType::Element` 取到 `half`、`AType::Layout` 取到 `RowMajor`。
- 当数据搬入 L1 时，类型选择器（4.1.3 中的 `L1ATypeSelector`）会「消费」一个 GM 上的 GemmType，产出一个新的、Position 为 `A1` 的 L1 版 GemmType——这就是「同一份数据在不同层有不同 GemmType」的体现。

#### 4.1.3 源码精读

`GemmType` 的完整定义只有几行（[gemm_type.hpp:20-25](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/gemm_type.hpp#L20-L25)）：

```cpp
template <class Element_, class Layout_,
          AscendC::TPosition POSITION_ = AscendC::TPosition::GM>
struct GemmType {
    using Element = Element_;
    using Layout = Layout_;
    static constexpr AscendC::TPosition POSITION = POSITION_;
};
```

要点：

- `POSITION_` 带默认值 `GM`，所以 host 侧写 `GemmType<half, RowMajor>` 时隐含「这块数据在 GM 上」。
- 它没有任何成员变量、没有构造函数——纯粹是编译期类型别名容器，运行期零开销。

在第一个样例里，三个矩阵就是这样绑定的（[basic_matmul.cpp:92-94](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L92-L94)）：

```cpp
using AType = Gemm::GemmType<ElementA, LayoutA>;   // half + RowMajor, 默认 GM
using BType = Gemm::GemmType<ElementB, LayoutB>;
using CType = Gemm::GemmType<ElementC, LayoutC>;
```

「消费 GemmType、产出新 GemmType」的典型例子是 `L1ATypeSelector`。它的主模板是「不支持就报错」（[helper.hpp:177-180](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/helper.hpp#L177-L180)），而对 `GemmType<Element, layout::RowMajor>` 的特化（[helper.hpp:187-190](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/helper.hpp#L187-L190)）则把 GM 上的 RowMajor 翻译成 L1 上的 zN：

```cpp
template <class Element>
struct L1ATypeSelector<Gemm::GemmType<Element, layout::RowMajor>> {
    using L1AType = Gemm::GemmType<Element, layout::zN, AscendC::TPosition::A1>;
};
```

这里就能直观看到：进入 L1 后，Layout 从 `RowMajor` 变成分形 `zN`、Position 从 `GM` 变成 `A1`，但 Element 保持不变。**GemmType 是承载这种「跨层变换」的标准载体**。

#### 4.1.4 代码实践

**实践目标**：体会「GemmType 是可传递的类型货币」。

**操作步骤（源码阅读型）**：

1. 打开 [basic_matmul.cpp:86-96](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L86-L96)。
2. 追踪 `AType` 这个类型别名的去向：它作为 `BlockMmad` 的第 4 个模板参数传入。
3. 在 `include/catlass/gemm/helper.hpp` 中搜索 `AType::Element` 或 `typename ...::Element` 的使用（例如 `L1ATypeSelector`），观察下游如何从 GemmType 里「拆」出 Element 与 Layout。

**需要观察的现象**：`GemmType` 自身没有任何 `.cpp` 实现逻辑，它在源码里只以「类型别名」的形式出现；所有的「行为」都体现在「谁消费了它的 `Element`/`Layout`/`POSITION`」。

**预期结果**：你能说清楚「为什么 CATLASS 不直接把 Element、Layout 当两个参数传，而要打包成 GemmType」——因为跨层搬运时三者要一起变，打包后只要换一个类型选择器就能整体替换。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `GemmType` 的 `POSITION_` 默认值是 `GM`，而不是要求显式写出？

**参考答案**：因为绝大多数用户在 host 侧描述的输入/输出矩阵都在 GM 上；让 GM 成为默认值，可以让人体感上只写「类型 + 布局」两个参数，减少样板代码。只有数据搬进内层存储时，才由类型选择器显式产出带 `A1`/`A2` 等 Position 的新 GemmType。

**练习 2**：下面这段省略了 Position，请说明它等价于什么：

```cpp
using AType = Gemm::GemmType<half, layout::RowMajor>;
```

**参考答案**：等价于 `Gemm::GemmType<half, layout::RowMajor, AscendC::TPosition::GM>`，即「位于 GM、半精度、行主序」的矩阵类型。

---

### 4.2 累加类型选择：ElementAccumulatorSelector

#### 4.2.1 概念说明

矩阵乘 \(C = A \times B\) 在最内层是不断做「乘加」：\(c \mathrel{+}= a \times b\)。这个累加值 \(c\) 用什么类型存？这就是「累加类型（ElementAccumulator）」。

为什么不能直接用输入类型累加？因为：

- **half（fp16）的精度只有 ~3 位有效十进制**，做上千次相加后会快速丢精度。昇腾硬件的 L0C 默认就是按 **fp32** 累加，所以 half 输入必须升到 fp32 累加。
- **int8_t** 乘积最大到 \(127 \times 127 \approx 1.6\times 10^4\)，再累加会立刻溢出 int16，必须用 **int32** 累加。

于是 CATLASS 用 `ElementAccumulatorSelector<ElementA, ElementB>` 这个模板，**在编译期根据 A、B 的类型自动推导出累加类型**。你不必手写「half 就用 float」，selector 帮你查表。

#### 4.2.2 核心流程

selector 是一张「输入类型对 → 累加类型」的编译期映射表：

```
ElementAccumulatorSelector<ElementA, ElementB>
   ├── 主模板：DEPENDENT_FALSE（不支持的组合直接编译报错）
   └── 特化表（节选）：
         <half, half>            → float
         <float, float>          → float
         <bfloat16_t, bfloat16_t>→ float
         <int8_t, int8_t>        → int32_t
         <int4b_t, int4b_t>      → int32_t
         <int32_t, int32_t>      → int32_t
         FP8 / FP4 系列（仅 3510）→ float
```

下游（如 BlockMmad、TileMmad）写 `using ElementAccumulator = typename helper::ElementAccumulatorSelector<ElementA, ElementB>::ElementAccumulator;` 就拿到了累加类型，无需手动指定。

#### 4.2.3 源码精读

主模板是「守门」写法，匹配不到特化就报错（[helper.hpp:88-92](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/helper.hpp#L88-L92)）：

```cpp
template <class ElementA, class ElementB>
struct ElementAccumulatorSelector {
    static_assert(DEPENDENT_FALSE<ElementA>,
                  "Unsupported element accumulator selector, can not find the specialization.");
};
```

最常用的几条特化（[helper.hpp:94-112](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/helper.hpp#L94-L112)）：

```cpp
template <> struct ElementAccumulatorSelector<half, half>            { using ElementAccumulator = float; };
template <> struct ElementAccumulatorSelector<float, float>          { using ElementAccumulator = float; };
template <> struct ElementAccumulatorSelector<int8_t, int8_t>        { using ElementAccumulator = int32_t; };
template <> struct ElementAccumulatorSelector<bfloat16_t, bfloat16_t>{ using ElementAccumulator = float; };
```

可以看到规律：**所有浮点类（half/bf16/fp8）都升到 float 累加；整型类（int8/int4）都升到 int32 累加**。这与昇腾 L0C「浮点按 fp32、整型按 int32」的硬件行为一致。

注意 FP8 与 FP4（微缩放）相关特化被 `#if (CATLASS_ARCH == 3510)` 包裹（[helper.hpp:114-154](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/helper.hpp#L114-L154)），即它们只在 Ascend950 架构下编译——这是「底层硬件差异特化」的典型体现。int4b_t 的特化（[helper.hpp:172-175](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/helper.hpp#L172-L175)）放在宏外面，两代架构通用。

> 小贴士：为什么是「主模板报错 + 一堆特化」而不是 `if constexpr`？因为这是 C++ 模板，编译期分发；不写特化的类型组合会在编译期被 `DEPENDENT_FALSE` 拦住，给出清晰错误信息，而不是生成错误的代码。

#### 4.2.4 代码实践

**实践目标**：验证「fp16 输入 → fp32 累加」这条规则。

**操作步骤（源码阅读型 + 推导）**：

1. 给定 `ElementA = half`、`ElementB = half`，查表 [helper.hpp:94-97](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/helper.hpp#L94-L97) 推导 `ElementAccumulator = float`。
2. 打开 `include/catlass/gemm/tile/tile_mmad.hpp` 或 `block_mmad*.hpp`，搜索 `ElementAccumulatorSelector`，观察它如何被用来定义 L0C 上的累加类型。
3. 对照 [04_matmul_summary.md:166](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/2_Design/01_kernel_design/04_matmul_summary.md#L166) 中那句「L0C 上按照 fp32 累加」，确认推导与文档一致。

**需要观察的现象**：累加类型完全由输入类型编译期决定，运行期没有任何「选类型」的开销；L0C 的容量按 fp32（4 字节）估算正是因为累加类型是 float。

**预期结果**：你能口头复述「half→fp32、int8→int32、bf16→fp32」三条规则，并解释为什么 half 不能直接用 half 累加。

#### 4.2.5 小练习与答案

**练习 1**：如果有人写 `ElementAccumulatorSelector<half, int8_t>`，会发生什么？

**参考答案**：会编译失败。因为 selector 没有为 `<half, int8_t>` 这种「跨精度混合」提供特化，会落到主模板，触发 `DEPENDENT_FALSE` 的 `static_assert` 报错。CATLASS 目前要求 A、B 元素类型一致（或属于同一族）。

**练习 2**：为什么 int8 矩阵乘要用 int32 累加，而不能用 int16？

**参考答案**：两个 int8 相乘最大 \(127 \times 127 = 16129\)，已经在 int16（最大 32767）的可表示范围内但余量极小；再做若干次累加必然溢出。int32 提供足够余量，且与昇腾 L0C 的整型累加硬件行为一致。

---

### 4.3 TileShape 与硬件约束：GemmShape 与 GemmCoord

#### 4.3.1 概念说明

矩阵乘被 CATLASS 切成一层层的「块（tile）」。要描述一个块有多大、在哪，需要两类工具：

- **GemmShape**：描述一个块的**尺寸**，是编译期常量。`L1TileShape = GemmShape<128, 256, 256>` 表示「L1 上 A/B 块的 M=128、N=256、K=256」。编译期常量意味着编译器能据此展开循环、分配缓冲。
- **GemmCoord**：描述一个块在问题空间里的**坐标**（位置），是运行期值。例如基本任务块 (mBlock=2, nBlock=3, kBlock=0)。

两者长相相似但语义不同：**Shape 是「多大」，Coord 是「在第几个」**。

为什么 TileShape 要受硬件约束？因为 L1、L0A、L0B、L0C 的容量是有限的（来自 u1-l2）。把块切太大放不下，切太小则流水效率低。所以选 TileShape 本质是「在容量上限内尽量填满各级存储」。

#### 4.3.2 核心流程

**GemmShape 的结构**（[gemm_coord.hpp:19-62](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm_coord.hpp#L19-L62)）：

```
GemmShape<M=1, N=1, K=1>
   ├─ static constexpr M, N, K          // 三个尺寸
   ├─ static constexpr MN, MK, KN, MNK  // 两两/三者乘积（方便算元素数）
   └─ ToCoord()/ToCoordMN()/...         // 转成 Coord（编译期值→可当运行期用）
```

**GemmCoord 的结构**（[gemm_coord.hpp:66-156](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm_coord.hpp#L66-L156)）：继承自 `Coord<3, uint32_t>`，带 `m()/n()/k()` 访问器，是运行期可变的三维坐标。

**容量约束流程**（Common 模板，来自 [04_matmul_summary.md:174-179](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/2_Design/01_kernel_design/04_matmul_summary.md#L174-L179)）。设 L1 块尺寸为 \(m_1,n_1,k_1\)，L0 块尺寸为 \(m_0,n_0,k_0\)，则：

\[
m_1 k_1 \cdot \text{L1Stage}_A + n_1 k_1 \cdot \text{L1Stage}_B \le \text{L1Size} / 2\text{Byte}
\]

\[
m_0 k_0 \cdot \text{L0AStage} \le \text{L0ASize} / 2\text{Byte}, \quad
n_0 k_0 \cdot \text{L0BStage} \le \text{L0BSize} / 2\text{Byte}
\]

\[
m_0 n_0 \cdot \text{L0CStage} \le \text{L0CSize} / 4\text{Byte}, \quad
m_0 = m_1, \quad n_0 = n_1
\]

其中 `/ 2Byte` 是因为 fp16 每元素 2 字节（把字节容量换算成「元素个数」），`/ 4Byte` 是因为 L0C 按 fp32（4 字节）累加。`Stage` 是多缓冲数（pingpong = 2）。除 TileShape 外，还有一个**对齐约束**由 `TileShapeAlignChecker` 在编译期强制（见 4.3.3）。

#### 4.3.3 源码精读

`GemmShape` 把 M/N/K 和常用乘积都做成 `static constexpr`（[gemm_coord.hpp:26-36](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm_coord.hpp#L26-L36)）：

```cpp
struct GemmShape {
    static constexpr uint32_t M = M_;
    static constexpr uint32_t N = N_;
    static constexpr uint32_t K = K_;
    static constexpr int64_t MN = M * N;
    static constexpr int64_t MK = M * K;
    // ...
};
```

第一个样例里 L1 与 L0 的块尺寸是这样设的（[basic_matmul.cpp:88-90](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L88-L90)）：

```cpp
using L1TileShape = GemmShape<128, 256, 256>;   // m1=128, n1=256, k1=256
using L0TileShape = GemmShape<128, 256, 64>;    // m0=128, n0=256, k0=64
```

注意这里满足 \(m_0 = m_1 = 128\)、\(n_0 = n_1 = 256\)，与文档约束一致。

容量常量来自架构抽象（[arch.hpp:18-26](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/arch/arch.hpp#L18-L26)）：

```cpp
struct AtlasA2 {
    static constexpr uint32_t L1_SIZE  = 512 * 1024;   // 512 KB
    static constexpr uint32_t L0A_SIZE = 64 * 1024;    // 64 KB
    static constexpr uint32_t L0B_SIZE = 64 * 1024;    // 64 KB
    static constexpr uint32_t L0C_SIZE = 128 * 1024;   // 128 KB
};
```

此外，`TileShapeAlignChecker` 在编译期校验「TileShape 的每个维度 × 元素位宽」是否对齐 32 字节（`_aligned = 32`），不齐就直接 `static_assert` 报错（[helper.hpp:402-414](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/helper.hpp#L402-L414)）：

```cpp
template <class L1TileShape, class L0TileShape, class ElementA, class ElementB,
          uint32_t _aligned = 32>
struct TileShapeAlignChecker {
    static constexpr uint32_t _ALIGN = _aligned * 8;   // 256 bit
    static_assert(L1TileShape::K * SizeOfBits<ElementA>::value % _ALIGN == 0,
                  "L1TileShape::K is not aligned.");
    // ... M/N/K 各方向逐一校验
};
```

> 小贴士：`SizeOfBits<T>::value` 对普通类型是 `sizeof(T)*8`（half→16），但对 int4/fp4 这种子字节类型会返回逻辑位宽 4（见 [numeric_size.hpp:35-39](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/numeric_size.hpp#L35-L39)）。所以 `K * 16 % 256 == 0` 要求 K 是 16 的倍数（fp16），这正是样例里 K 取 256、64 这类值的原因。

#### 4.3.4 代码实践

**实践目标**：手算验证样例里的 `L1TileShape(128, 256, 256)` 在 AtlasA2 上确实放得下 L1（含 pingpong 双缓冲）。

**操作步骤（推导型）**：

1. 确认输入为 fp16（`ElementA = ElementB = half`），故每元素 2 字节；累加为 fp32。
2. L1 容量换算成「fp16 元素个数」：`L1_SIZE / 2 = 512*1024 / 2 = 262144` 个元素。
3. 计算单缓冲下 A、B 各占：A = \(m_1 \times k_1 = 128 \times 256 = 32768\)；B = \(n_1 \times k_1 = 256 \times 256 = 65536\)。
4. 样例用 `MmadAtlasA2Pingpong`，即 pingpong 双缓冲（Stage = 2），故总占用 = \(32768 \times 2 + 65536 \times 2 = 196608\) 个元素。
5. 比较：\(196608 \le 262144\) ✓ 放得下。

**需要观察的现象**：把 K 维从 256 加大到 512（其它不变），A+B 双缓冲会变成 \(128\times512\times2 + 256\times512\times2 = 393216 > 262144\)，超出 L1——这就解释了为什么样例不会无脑把 k1 拉满。

**预期结果**：你能用公式独立判断「一组 TileShape 是否合法」，并理解 `m_1k_1*Stage_A + n_1k_1*Stage_B ≤ L1Size/2Byte` 中每一项的来历。

> 说明：本实践为「源码阅读 + 手算推导」，未在真实 NPU 上运行；Stage 数以样例所用 pingpong=2 为准，若你选用 SingleBuffer 或 Preload 不同的 Stage 数，需相应替换。

#### 4.3.5 小练习与答案

**练习 1**：`GemmShape` 和 `GemmCoord` 都有 M/N/K，它们的根本区别是什么？

**参考答案**：`GemmShape<M,N,K>` 是编译期 `static constexpr` 常量，描述「一个块有多大」，编译器据此分配缓冲、展开循环；`GemmCoord` 继承 `Coord<3, uint32_t>`，是运行期可变的值，描述「当前这个块在整个问题里的第几个位置」。前者是尺寸，后者是坐标。

**练习 2**：样例的 `L0TileShape = GemmShape<128, 256, 64>`，请验证 L0B 约束（单缓冲即可）。

**参考答案**：L0B 容量换算成 fp16 元素数 = `64*1024/2 = 32768`；B 占 \(n_0 \times k_0 = 256 \times 64 = 16384\)，单缓冲已满足 \(16384 \le 32768\) ✓。即使按双缓冲 \(16384 \times 2 = 32768 \le 32768\) 也刚好放得下。

**练习 3**：为什么 `TileShapeAlignChecker` 默认按 32 字节对齐？half 下 K 维最小合法取值是多少？

**参考答案**：32 字节是昇腾搬运指令（DataCopy/LoadData）的对齐粒度，不对齐会损失带宽或报错。half 位宽 16 bit = 2 字节，`K * 2 % 32 == 0` 要求 K 是 16 的倍数；故 half 下 K 维最小合法值为 16（如 64、128、256 都满足）。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成一次「从输入类型到 TileShape 合法性」的完整判定：

**任务**：假设你要为 AtlasA2 写一个 fp16 的矩阵乘，A/B 都是 `RowMajor`、C 是 `RowMajor`，打算用 `L1TileShape = GemmShape<128, 256, 256>`、`L0TileShape = GemmShape<128, 256, 64>`、pingpong 双缓冲。请完成：

1. **类型绑定**：写出 `AType/BType/CType` 三个 `using`（参考 [basic_matmul.cpp:92-94](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L92-L94)），指出它们各自的 Position 默认值。
2. **累加类型**：用 [helper.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/helper.hpp) 的 selector 推导 `ElementAccumulator`，并说明 L0C 应按几位宽估算容量。
3. **容量验证**：用 [04_matmul_summary.md](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/2_Design/01_kernel_design/04_matmul_summary.md) 的 Common 模板公式，分别验证 L1（A+B 双缓冲）、L0A、L0B、L0C（单缓冲）是否放得下。
4. **对齐检查**：用 `TileShapeAlignChecker` 的口径，确认 L1/L0 各维度 × 16 bit 都能被 256 bit（32 字节）整除。

**参考要点**：

1. `using AType = Gemm::GemmType<half, layout::RowMajor>;`（Position 默认 GM），B/C 同理。
2. `ElementAccumulatorSelector<half, half>::ElementAccumulator = float`；L0C 按 fp32（4 字节）估算。
3. L1：\(128\times256\times2 + 256\times256\times2 = 196608 \le 262144\) ✓；L0A：\(128\times64 = 8192 \le 32768\) ✓；L0B：\(256\times64 = 16384 \le 32768\) ✓；L0C：\(128\times256 = 32768\) 个 fp32 = 131072 字节，而 L0C 容量换算成 fp32 元素 = \(128\times1024/4 = 32768\)，\(32768 \le 32768\) ✓（刚好）。
4. half 位宽 16：128/256/64/256 均 ×16 后是 2048/4096/1024/4096 bit，都能被 256 整除 ✓。

> 若你的环境有 NPU，可进一步把 `L1TileShape` 改成不合法值（如 K=512）重新编译，观察编译期 `static_assert` 或运行期容量报错——这是验证你理解的最直接方式。

## 6. 本讲小结

- `GemmType` 是把 `Element + Layout + Position` 打包的轻量类型容器，本身无逻辑，作为「类型货币」在模板链路里传递；跨层搬运时由类型选择器产出带新 Position/Layout 的 GemmType。
- `ElementAccumulatorSelector` 在编译期据 A/B 输入类型查表决定累加精度：浮点类（half/bf16/fp8）→ `float`，整型类（int8/int4）→ `int32`；不支持的组合由主模板 `DEPENDENT_FALSE` 拦下报错。
- `GemmShape` 是编译期尺寸常量（M/N/K 及乘积），`GemmCoord` 是运行期三维坐标；二者一个回答「多大」、一个回答「第几个」。
- TileShape 必须满足各级存储容量约束（L1/L0A/L0B 按 fp16 元素数、L0C 按 fp32 元素数），并对齐 32 字节；样例 `L1TileShape(128,256,256)` + pingpong 在 AtlasA2 上占用 196608 ≤ 262144 个 fp16 元素，合法。
- 容量常量与累加位宽都来自架构抽象（`arch.hpp`）与类型选择器（`helper.hpp`），换架构（如 Ascend950 的 L0C=256KB）需重新核算。

## 7. 下一步学习建议

- **进入 Block 层**：本讲的 `GemmType` 与 `L1TileShape/L0TileShape` 正是 `BlockMmad` 的模板参数。建议接着读 u4-l1（BlockMmad 主循环），看它们如何被组装进 K 维主循环。
- **看搬运如何变换 GemmType**：读 `include/catlass/gemm/tile/copy_gm_to_l1.hpp` 等 Tile 组件（U5），观察「GM 上的 RowMajor GemmType」是如何一步步变成「L1 上的 zN GemmType」的。
- **量化场景的累加类型**：若对 int8/fp8 感兴趣，可先跳读 [helper.hpp:104-154](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/helper.hpp#L104-L154) 中 int4b_t 与 FP8/FP4 的特化，再结合 u9-l2（量化矩阵乘）系统学习。
- **架构差异**：对比 `AtlasA2` 与 `Ascend950` 的 `L0C_SIZE`（128KB vs 256KB），思考它会如何放宽 L0TileShape 的选择——这是 u10（跨架构迁移）的伏笔。
