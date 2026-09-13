# 读懂 CuTe：本项目用到的布局、swizzle 与 GMMA atom

> 单元二 · 第 4 讲（u2-l4，intermediate）。前置：u1-l4（双 kernel 架构与 workspace 布局）。

## 1. 本讲目标

学完本讲，你应当能够：

1. 读懂 `K1Layouts` / `K2Layouts` 里每一个 `using` 类型定义，并能**手算**它们的 `cosize`（共享内存元素数）。
2. 区分两类 GMMA atom——`Layout_K_INTER_Atom`（K 方向连续，行主视图）与 `Layout_MN_INTER_Atom`（M/N 方向连续，转置视图）——并说出本项目分别在什么地方用它们。
3. 解释 `tile_to_shape` 如何用一个小 atom「铺地砖」一样铺满 `(CHUNK, D)` 等目标形状。
4. 解释 TMA 共享内存布局为什么通过 `prepend` + `composition` 派生，以及为什么 **K1 写 workspace 与 K2 读 workspace 必须使用完全相同的布局**（K1 写下的比特就是 K2 读到的比特）。

本讲只讲「布局」，不讲计算——但布局决定了每一个字节往哪里放，是读懂后续流水线与 MMA 两讲的地基。

## 2. 前置知识

### 2.1 Layout：从坐标到偏移的整数函数

CuTe（CUTLASS 的布局代数库）里，一个 `Layout` 就是函数

\[ \text{offset}(c_0, c_1, \dots) = c_0 \cdot s_0 + c_1 \cdot s_1 + \dots \]

其中 \((c_0, c_1, \dots)\) 是**逻辑坐标**（第几行、第几列），\((s_0, s_1, \dots)\) 是**步长（stride）**。CuTe 的书写习惯是 `shape:stride` 冒号对，例如：

```text
(16, 128):(128, 1)     -- 行主序：行 m 列 n 的偏移 = m*128 + n
(16, 128):(1, 16)      -- 列主序：偏移 = m + n*16
```

三个最常用的工厂函数：

- `make_shape(...)`：构造形状（每个维度的大小）；
- `make_stride(...)`：构造步长；
- `make_layout(shape, stride)`：把两者绑成一个 Layout。

`make_layout(shape, LayoutRight{})` / `LayoutLeft{}` 是偷懒写法：用「行主 / 列主」标签自动生成步长（`LayoutRight` 让最后一个模式步长为 1）。

### 2.2 静态整数与嵌套模式

`Int<16>{}`（也写作 `_16{}`）是**编译期整数**。整个布局全部由静态整数构成时，地址计算在编译期就能完成，这正是 `decltype(...)` 能在 `using` 里「算出」一个类型的原因——`K1Layouts` 里的每一行都是一次编译期布局代数运算。

Shape 可以嵌套：`(8, (8, 2))` 表示第一个模式是 8，第二个模式再拆成 8×2。嵌套只是分组记法，`cosize` 照常计算。

### 2.3 size 与 cosize

- `size(layout)`：逻辑元素总数（各维乘积）；
- `cosize(layout)`：布局覆盖的**最大偏移 + 1**，即真正需要开的元素个数。对「双射」（无重叠、无空洞）布局，`cosize == size`；本讲出现的所有布局都是双射的，后面会用 workspace 字节数交叉验证这一点。

### 2.4 swizzle 与 ComposedLayout

共享内存以 32 个 bank 组织，朴素行主的访存很容易撞 bank。解决办法是在偏移上加一个**位异或重排**（swizzle）：`Swizzle<B, M, S>` 把偏移的第 \([S, S+B)\) 位与第 \([M, M+B)\) 位异或。带 swizzle 的布局类型是 `ComposedLayout`，语义上是三层复合：

```text
ComposedLayout = Swizzle ∘ smem_ptr_flag ∘ Layout
                 （位重排）  （标记这是共享内存指针） （整数线性布局）
```

项目源码里的注释（[csrc/smxx/utils.cuh:L371-L377](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L371-L377)）和 CUTLASS 子模块中的 `cutlass/include/cute/atom/mma_traits_sm90_gmma.hpp` 都确认：GMMA 的全部 smem atom 都以这种 ComposedLayout 形式、先以**比特**为单位定义，再 `upcast` 成元素单位。其中 INTER 家族 `Swizzle` 的异或位数 \(B=0\)（实际不做任何异或），SW32 家族 \(B=1\)。

### 2.5 TMA 一句话

TMA（Tensor Memory Accelerator）是 Hopper 起的硬件异步拷贝引擎：主机侧/内核侧用 `make_tma_copy(SM90_TMA_LOAD{}, gmem张量, smem布局)` 构造一个**描述符**，之后一条指令就能把全局内存的一个「盒子」搬进共享内存（或反向 store）。描述符里编码了 gmem 侧排布与 smem 侧排布的映射，所以**两边布局类型必须严格一致地成对出现**——这是本讲第 3 个模块的主线。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| [csrc/smxx/fwd_kernel1.cuh](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L5-L42) | K1（prepare）内核 | `K1Layouts`（L5-42）、`SharedStorageK1`（L44-84）、TMA 读入（L200-255）、TMA 写出 workspace（L515-583） |
| [csrc/smxx/fwd_kernel2.cuh](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L9-L70) | K2（recurrence）内核 | `K2Layouts`（L9-70）、`SharedStorageK2`（L72-112）、状态 TMA 读写（L239-304、L786-835） |
| [csrc/smxx/utils.cuh](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L61-L77) | 公共工具 | cute 头引入（L9-28）、`WorkspaceSizes`（L63-77）、fp32↔bf16 状态转换及其注释（L371-437） |
| [csrc/smxx/fwd_launch.cu](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L86-L144) | 启动器 | `make_tma_copy` 如何成对消费这些布局（L87-144） |
| `cutlass/include/cute/atom/mma_traits_sm90_gmma.hpp` | CUTLASS 子模块（见 [.gitmodules](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/.gitmodules#L1-L3)） | `Layout_K_INTER_Atom` 等 atom 的定义；本讲引用其结论，具体行号请在本地 `git submodule update --init cutlass` 后确认 |

## 4. 核心概念与源码讲解

本讲的三个最小模块：**4.1 CuTe Layout 基础** → **4.2 GMMA atom 与 tile_to_shape** → **4.3 TMA smem 布局（prepend/composition）**。

回顾 u1-l4 已建立的事实：CHUNK=16、D=128；K1 网格 `(total_tiles, H)` 负责准备，K2 网格 `(N, H)` 负责递推；两者通过每 tile 13824 字节的 6 段 workspace 交接。本讲回答：这 6 段数据在共享内存里**按什么排布**、为什么这样排。

### 4.1 模块一：CuTe Layout 基础——朴素布局三件套

#### 4.1.1 概念说明

并不是所有共享内存都需要 swizzle 和 atom。一个缓冲区如果只被「朴素访存」使用（按行列坐标逐元素读写、或作为协作 GEMM 的输入输出），最简单的行主/列主 Layout 就够了。`K1Layouts` 里的 `QKLayout`、`GLayout`、`BetaSmemLayout`、`GTotalLayout`、`LF32Layout` 就是这一类。它们回答的问题只有一个：**给定逻辑坐标，元素放在缓冲区第几个位置**。理解它们是理解后续复杂布局的入口，因为复杂布局只是在这个基础上加了「分块 + 位重排」两层的同一个函数。

#### 4.1.2 核心流程

读一个朴素布局的固定套路：

1. 看 shape：逻辑形状是几维、每维多大；
2. 看 stride：哪个方向步长为 1（哪个方向内存连续）；
3. 乘出 `cosize`：开多大的 `ArrayEngine`；
4. 乘上元素大小：得到字节数，与 `WorkspaceSizes` 对照。

以 `QKLayout` 为例，取坐标 \((m, n)\)（m 为 chunk 内行号 0..15，n 为通道号 0..127）：

\[ \text{offset}(m, n) = m \times 128 + n \in [0, 2048) \]

#### 4.1.3 源码精读

**QKLayout 与 GLayout：q/k 块与门控累加块的行主布局。**

[文件 csrc/smxx/fwd_kernel1.cuh:L7-L8](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L7-L8)（K1Layouts 开头两行）：

```cpp
using QKLayout = decltype(make_layout(make_shape(Int<CHUNK>{}, Int<D>{}), LayoutRight{}));
using GLayout  = decltype(make_layout(make_shape(Int<CHUNK>{}, Int<D>{}), LayoutRight{}));
```

这两行用 `LayoutRight{}`（行主）生成 `(16, 128):(128, 1)`：D 方向连续。TMA 从 gmem 加载 q/k/g 的 16×128 tile 时，smem 目标就用它（见 4.3）。`cosize = 16 × 128 = 2048` 个 bf16 = 4096 字节——与 [utils.cuh:L70](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L70) 的 `kKDecayed = CHUNK * D * 2 = 4096` 互相印证（k_decayed 缓冲与 q/k 同尺寸，见 4.2）。

**BetaSmemLayout 与 GTotalLayout：两个一维布局。**

[csrc/smxx/fwd_kernel1.cuh:L14-L15](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L14-L15)：

```cpp
using BetaSmemLayout = Layout<Shape<Int<32>>, Stride<Int<1>>>;
using GTotalLayout   = Layout<Shape<Int<D>>, Stride<Int<1>>>;
```

直接手写 `Shape/Stride`：`(32):(1)` 与 `(128):(1)`，都是一维连续。beta 为什么是 32 而不是 16？看内核里的加载方式（[csrc/smxx/fwd_kernel1.cuh:L222-L225](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L222-L225)）：`beta_aligned = beta_linear & ~7`——TMA 盒子起点被对齐到 8 的倍数，随后消费时带最多 7 的偏移再取 16 个元素（[L345](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L345)），所以至少要 \(7 + 16 = 23\) 个，取整到 32。`GTotalLayout` 装的是每通道的累计门控 \(g_{\text{total}}\)（fp32），`cosize=128`，512 字节 = `kGTotal`。

**LF32Layout：全朴素的最小方阵布局。**

[csrc/smxx/fwd_kernel1.cuh:L21](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L21)：

```cpp
using LF32Layout = decltype(make_layout(make_shape(Int<CHUNK>{}, Int<CHUNK>{}), LayoutRight{}));
```

`(16,16):(16,1)` 的 fp32 方阵，存放 (I+L) 的种子矩阵 L。它只被协作 GEMM（[L483](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L483)）和前代消元逐元素读写（[utils.cuh:L240](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L240)），既不进 TMA 也不进 LDSM，所以**保持最朴素的行主序**。这是一个重要对照：本项目只给「确实需要 TMA / ldmatrix 直读」的缓冲区套 atom 布局，其余一律朴素。

**用 cosize 开缓冲区：ArrayEngine。**

[csrc/smxx/fwd_kernel1.cuh:L44-L84](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L44-L84) 的 `SharedStorageK1` 全部缓冲区都是 `cute::ArrayEngine<T, cute::cosize_v<某Layout>>`——布局类型直接决定字节数。q/k（Phase A）与 k_decayed/q_decayed/k_inv（Phase B）能放进同一个 [union（L57-L71）](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L57-L71)，正是因为 `cosize_v<QKLayout> == cosize_v<MMALayout> == 2048`。

#### 4.1.4 代码实践（源码阅读 + 手算，无需 GPU）

1. **实践目标**：能手算朴素布局的 cosize，并与 workspace 字节常量对上。
2. **操作步骤**：
   - 打开 [csrc/smxx/utils.cuh:L63-L77](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L63-L77)，抄下 6 个常量；
   - 对 `QKLayout`、`GLayout`、`BetaSmemLayout`、`GTotalLayout`、`LF32Layout` 逐一写出 `(shape):(stride)` 与 cosize；
   - 换算成字节（bf16=2、fp32=4），填入下表。

   | 布局 | shape:stride | cosize | 类型 | 字节 |
   |---|---|---|---|---|
   | QKLayout | 待填 | 待填 | bf16 | ？ |
   | BetaSmemLayout | 待填 | 待填 | bf16 | ？ |
   | GTotalLayout | 待填 | 待填 | fp32 | ？ |
   | LF32Layout | 待填 | 待填 | fp32 | ？ |

3. **观察现象**：GTotalLayout 的字节数应与 `kGTotal` 相等；LF32Layout 的字节数应等于 1024（它没有对应的 workspace 段，只在 K1 内部用）。
4. **预期结果**：QKLayout=(16,128):(128,1)、cosize=2048、4096 字节；BetaSmemLayout=(32):(1)、64 字节；GTotalLayout=(128):(1)、512 字节；LF32Layout=(16,16):(16,1)、1024 字节。

#### 4.1.5 小练习与答案

**练习 1**：`QKLayout` 下坐标 (3, 127) 的偏移是多少？(15, 0) 呢？
答案：\(3 \times 128 + 127 = 511\)；(15, 0) = \(15 \times 128 = 1920\)。

**练习 2**：把 `QKLayout` 的 `LayoutRight{}` 换成 `LayoutLeft{}`，得到什么布局？D 方向还连续吗？
答案：得到 `(16,128):(1,16)`，行方向步长 1、D 方向步长 16——D 不再连续。TMA 与后续按行访存的效率都会改变（本项目所有 (CHUNK,D) 朴素布局都用行主，保证 D 连续，与 gmem 的行主张量一致）。

**练习 3**：`BetaSmemLayout` 为什么不是 16 或 23，而是 32？
答案：消费端最多从对齐起点偏移 7 再读 16 个（\(7+16=23\)），缓冲区必须 ≥23；TMA 盒与对齐习惯上取 2 的幂，故取 32。

### 4.2 模块二：GMMA atom 与 tile_to_shape——用「积木」铺满整块

#### 4.2.1 概念说明

`GMMA::Layout_K_INTER_Atom<T>` 这类名字来自 SM90 的 warpgroup MMA（wgmma）：Tensor Core 不再从寄存器取 A/B 操作数，而是直接从共享内存取，因此硬件规定（并让编译器静态检查）smem 必须排布成它认识的**规范形式**。CUTLASS 把最小规范单元做成「atom」——一个带 `Swizzle ∘ smem_ptr ∘ Layout` 三层结构的 `ComposedLayout` 模板。要点有三：

1. **K_INTER（K-major interleave）**：K 维（对 A/B 操作数来说就是收缩维）方向连续，即「行主」视角；`Swizzle` 异或位数 \(B=0\)，不做位交换。
2. **MN_INTER（MN-major interleave）**：转置视角——M/N 方向连续（每 8 个元素即 128 比特为一段），K 方向按行跳跃。对同一块物理内存，它给出「转置后」的坐标映射，供需要转置操作数的 MMA/copy 使用。
3. atom 以**比特**为单位定义、再 `upcast` 成元素单位；所有 GMMA atom 的 `Swizzle` 参数都是 \(\langle B, 4, 3 \rangle\)，INTER 家族 \(B=0\)，SW32 家族 \(B=1\)（见 CUTLASS 子模块 `cutlass/include/cute/atom/mma_traits_sm90_gmma.hpp`；本仓库自己的注释也如此描述——[utils.cuh:L371-L373](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L371-L373) 与 [fwd_kernel2.cuh:L59](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L59)）。

一个必须诚实说明的事实：**本项目并没有真正发射 wgmma 指令**——K2 的矩阵乘用的是 SM80 HMMA + `ldmatrix`（见 [fwd_kernel2.cuh:L468-L472](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L468-L472)）。GMMA atom 布局在这里的价值是当「规范 smem 排布模板」用：8 行一组、最内 128 比特连续、无位交换，这使得同一块 smem 既能被 **TMA 直接写/读**，又能被 **LDSM_N / LDSM_T（普通/转置 ldmatrix）直接读**（内核注释明说了这层对应：[fwd_kernel2.cuh:L480-L490](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L480-L490)——"A copy: K_INTER → LDSM_N"、"A copy: MN_INTER → LDSM_T"）。

`tile_to_shape(atom, shape, order)` 则是铺地砖：把 atom 当一块砖，沿 `shape` 反复平铺直到铺满；`order`（LayoutLeft/LayoutRight）决定砖块排列顺序（列主/行主）。铺完得到的仍是双射布局，`cosize` 就等于 `size(shape)`。

#### 4.2.2 核心流程

```text
atom (8 行 × 若干 128bit 段, Swizzle<B,4,3>, B=0)
        │
        ├── LayoutLeft 逐块平铺 ──►  tile_to_shape(atom, (CHUNK, D))   = MMALayout    (K1/K2 的 A/B 操作数排布)
        │                           tile_to_shape(atom, (CHUNK, CHUNK)) = LMLayout     (INV/Mqk 方阵)
        │                           tile_to_shape(atom, (D, D))         = StateSmemLayout (K2 状态矩阵)
        │
        └── MN_INTER atom 同样平铺 ──► Transposed* 系列（同一内存的转置视图，配 LDSM_T）
```

选择规则（从代码归纳）：

| 数据 | atom | 目标形状 | 为什么 |
|---|---|---|---|
| k_decayed/q_decayed/k_restored、v、out | K_INTER, bf16 | (16, 128) | TMA 可写可读；LDSM_N 直读做 A/B 操作数 |
| INV、Mqk | K_INTER, bf16 | (16, 16) | 同上，方阵版 |
| state（初始/最终状态） | K_INTER, bf16 | (128, 128) | K1/K2 之间经 gmem 交接 + K2 内当累加器 |
| k_restored 的转置、s_acc 的转置 | MN_INTER, bf16 | (128, 16)/(128,128) | Phase 6 状态更新需要按列取（LDSM_T） |
| fp32 状态 | K_SW32, float | (128, 128) | 仅 TMA + 标量转换访问，见 4.3 |

#### 4.2.3 源码精读

**K1 的三个 K_INTER 布局与一个 MN_INTER 布局。**

[csrc/smxx/fwd_kernel1.cuh:L9-L26](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L9-L26)：

```cpp
using MMALayout = decltype(tile_to_shape(
    GMMA::Layout_K_INTER_Atom<cute::bfloat16_t>{},
    make_shape(Int<CHUNK>{}, Int<D>{}),
    LayoutLeft{}));
// ...
using LMLayout = decltype(tile_to_shape(
    GMMA::Layout_K_INTER_Atom<cute::bfloat16_t>{},
    make_shape(Int<CHUNK>{}, Int<CHUNK>{}),
    LayoutLeft{}));
// ...
using TransposedLMLayout = decltype(tile_to_shape(
    GMMA::Layout_MN_INTER_Atom<cute::bfloat16_t>{},
    make_shape(Int<CHUNK>{}, Int<CHUNK>{}),
    LayoutRight{}));
```

- `MMALayout`：K_INTER atom 铺满 (16,128)。**cosize = 2048**（双射），这就是 `SharedStorageK1` 里 k_decayed/q_decayed/k_inv/k_restored 缓冲的大小，也解释了 workspace 每段 4096 字节。
- `LMLayout`：同一个 atom 铺满 (16,16)，cosize = 256（512 字节 = `kINV`/`kMqk`）。
- `TransposedLMLayout`：MN_INTER 版的 (16,16)——**同一块物理内存、另一个坐标映射**。注意它是独立类型：谁的 `begin()` 指向哪块内存，转置视图才作用在哪块上。

**K2 的布局族（含状态矩阵与 fp32 变体）。**

[csrc/smxx/fwd_kernel2.cuh:L11-L39](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L11-L39)：

```cpp
using MMALayout = decltype(tile_to_shape(
    GMMA::Layout_K_INTER_Atom<cute::bfloat16_t>{},
    make_shape(Int<CHUNK>{}, Int<D>{}), LayoutLeft{}));          // 与 K1 完全相同
using TransposedMMALayout = decltype(tile_to_shape(
    GMMA::Layout_MN_INTER_Atom<cute::bfloat16_t>{},
    make_shape(Int<D>{}, Int<CHUNK>{}), LayoutRight{}));          // (128,16) 转置视图
using StateSmemLayout = decltype(tile_to_shape(
    GMMA::Layout_K_INTER_Atom<cute::bfloat16_t>{},
    make_shape(Int<D>{}, Int<D>{}), LayoutLeft{}));               // 128x128, cosize=16384
using LMLayout = decltype(tile_to_shape(..., (CHUNK,CHUNK), ...)); // 与 K1 相同
```

K2 的 `MMALayout`/`LMLayout` 与 K1 **逐字符相同**——这不是巧合，而是 workspace 契约的类型层体现（4.3 详述）。`StateSmemLayout` cosize = \(128 \times 128 = 16384\) 个 bf16（32 KB），K2 用它当跨 tile 的状态累加器 `state_acc`（[fwd_kernel2.cuh:L82](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L82)）；其 MN_INTER 版 `TransposedStateSmemLayout` 服务于 Phase 6 的按列状态更新（[L29-L33](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L29-L33)，内核内 `s_acc_T` 视图见 [L457-L458](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L457-L458)）。

**fp32 状态：K_SW32 atom。**

[csrc/smxx/fwd_kernel2.cuh:L59-L69](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L59-L69)：

```cpp
// FP32 state layout (K_SW32 atom, same 8x8 atom structure as K_INTER bf16)
using FP32StateSmemLayout = decltype(tile_to_shape(
    GMMA::Layout_K_SW32_Atom<float>{},
    make_shape(Int<D>{}, Int<D>{}), LayoutLeft{}));
```

注释里的 "same 8x8 atom structure" 是理解 [utils.cuh:L378-L437](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L378-L437) 两个转换函数的钥匙：bf16 状态（K_INTER，\(B=0\)）与 fp32 状态（K_SW32，\(B=1\)）都按「8 行 × 128bit 段」的逻辑块组织，因此 `smem_cvt_fp32_to_bf16` 可以按 8×8 逻辑块逐块搬移——两个 `make_tensor(..., FP32Layout{})` / `make_tensor(..., BF16Layout{})` 视图各自套用各自布局完成坐标换算，每线程转 2 个元素（[utils.cuh:L390-L405](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L390-L405)）。注意 cosize 同为 16384，但单位分别是 float 与 bf16，所以 `SharedStorageK2` 的 fp32 缓冲是 `cosize_v<StateSmemLayout> * sizeof(float)` = 64 KB（[fwd_kernel2.cuh:L106](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L106)）。

**字节层面的交叉验证。**

把 cosize 与 [WorkspaceSizes（utils.cuh:L70-L75）](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/utils.cuh#L70-L75) 对表：

| 段 | 常量 | 推导 | 一致？ |
|---|---|---|---|
| kKDecayed/kQDecayed/kKRestored | 4096 B | cosize(MMALayout)=2048 × 2 B | ✓ |
| kGTotal | 512 B | cosize(GTotalLayout)=128 × 4 B | ✓ |
| kINV/kMqk | 512 B | cosize(LMLayout)=256 × 2 B | ✓ |
| 合计 kPerTile | 13824 B | 3×4096+512+512+512 | ✓ |

这张表同时证明了 `tile_to_shape` 的结果是双射（cosize==size）——否则字节账算不平。

#### 4.2.4 代码实践：cute_probe.cu 探针程序

这是本讲的主实践：写一个独立小程序，用 `cute::print` 亲手「看见」这些布局类型。

1. **实践目标**：打印 atom 与 tile_to_shape 结果的真实 shape/stride/cosize，并在笔记里标注 swizzle 参数。
2. **操作步骤**：

   先初始化 CUTLASS 子模块（编译包含路径要用，见 [setup.py:L62-L67](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/setup.py#L62-L67)）：

   ```bash
   git submodule update --init cutlass
   ```

   新建 `cute_probe.cu`（**示例代码**，独立小工具，不属于 FlashKDA 工程）：

   ```cpp
   // cute_probe.cu —— 打印本讲涉及的 CuTe 布局（示例代码）
   #include <cstdio>
   #include <cutlass/bfloat16.h>
   #include <cute/tensor.hpp>
   #include <cute/atom/mma_traits_sm90_gmma.hpp>

   using namespace cute;

   template <class L>
   void probe(char const* name) {
     printf("== %s ==\n  layout: ", name);
     print(L{});                       // 打印 ComposedLayout/Layout 的规范形式
     printf("\n  cosize: %d\n\n", int(cosize_v<L>));
   }

   int main() {
     probe<GMMA::Layout_K_INTER_Atom<cute::bfloat16_t>>("K_INTER atom (bf16)");
     probe<GMMA::Layout_MN_INTER_Atom<cute::bfloat16_t>>("MN_INTER atom (bf16)");
     probe<GMMA::Layout_K_SW32_Atom<float>>("K_SW32 atom (fp32)");

     using MMALayout = decltype(tile_to_shape(
         GMMA::Layout_K_INTER_Atom<cute::bfloat16_t>{},
         make_shape(Int<16>{}, Int<128>{}), LayoutLeft{}));
     probe<MMALayout>("MMALayout (16x128)");

     using LMLayout = decltype(tile_to_shape(
         GMMA::Layout_K_INTER_Atom<cute::bfloat16_t>{},
         make_shape(Int<16>{}, Int<16>{}), LayoutLeft{}));
     probe<LMLayout>("LMLayout (16x16)");

     using TransposedLMLayout = decltype(tile_to_shape(
         GMMA::Layout_MN_INTER_Atom<cute::bfloat16_t>{},
         make_shape(Int<16>{}, Int<16>{}), LayoutRight{}));
     probe<TransposedLMLayout>("TransposedLMLayout (16x16)");
     return 0;
   }
   ```

   编译运行（布局代数全部是编译期/host 侧计算，不需要 GPU 即可运行；`--expt-relaxed-constexpr` 与仓库 [setup.py:L76-L77](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/setup.py#L76-L77) 的做法一致）：

   ```bash
   nvcc -std=c++17 --expt-relaxed-constexpr -I cutlass/include \
        -o cute_probe cute_probe.cu
   ./cute_probe > probe_out.txt
   ```

3. **观察现象**：每行输出形如 `Sw<B,M,S> o smem_ptr o (shape):(stride)`（atom 与 tile_to_shape 结果都会带 `Sw<...>` 前缀，因为它们都是 ComposedLayout）；比较 K_INTER 与 K_SW32 的 `Sw<B,...` 中 B 的差异；比较 K_INTER 与 MN_INTER 内层 stride 中 `_1` 的位置（哪个方向连续）；确认 MMALayout 的 cosize 是 2048、LMLayout 是 256。
4. **预期结果**（按 CUTLASS 源码的规范形式推得；**具体打印细节待本地验证**，尤其 `upcast` 到元素单位后 swizzle 参数可能被换算）：
   - 三个 atom 都以 `Sw<0,...`（INTER，无异或位）或 `Sw<1,...`（SW32，1 个异或位）开头；
   - K_INTER 内层 K 方向出现步长 `_1` 的连续段，MN_INTER 则是 M/N 方向步长 `_1`；
   - `MMALayout` cosize = 2048，`LMLayout`/`TransposedLMLayout` cosize = 256。
   把输出贴进笔记，用红笔圈出每个 `Sw<B,M,S>` 的三个数字并注明含义（异或位数 / 基底位宽起点 / 移位量）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 workspace 的 6 段全部选 K_INTER（\(B=0\)，无异或），而不是 bank-conflict 特性更好的 SW64/SW128？
答案：这些段要被 K1 用 TMA store 写、被 K2 用 TMA load 读，还要被 K2 的 LDSM 直读。INTER 是三家共用、且完全无位交换的最简规范排布——无 swizzle 的布局 TMA 描述最朴素、逐字节可预测，K1 写下的每个比特 K2 原样读回（4.3 的契约）。bank conflict 的代价由访问模式（LDSM 的行组读取）吸收，项目以吞吐实测做了取舍（本讲只陈述代码事实：所有 bf16 交接缓冲均为 K_INTER）。

**练习 2**：`TransposedMMALayout` 的形状是 `(128, 16)` 而不是 `(16, 128)`，为什么？
答案：它是「同一块 (16,128) 内存的转置**视图**」：以 (D, CHUNK) 逻辑坐标访问 k_restored，等价于按列取原矩阵。类型形状必须写成访问者眼中的形状 (128,16)，内核里也正是 `make_tensor(...k_restored.begin(), TransposedMMALayout{})`（[fwd_kernel2.cuh:L464](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L464)）。

**练习 3**：`StateSmemLayout` 的 cosize 是多少？fp32 状态缓冲为什么是它的 4 倍字节数？
答案：cosize = 128×128 = 16384。`FP32StateSmemLayout` 与它逻辑形状相同、cosize 相同，但元素是 float（4 字节），所以 `state_fp32_buf` 开 `cosize_v<StateSmemLayout> * sizeof(float)` = 65536 字节（[fwd_kernel2.cuh:L106](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L106)）。

### 4.3 模块三：TMA smem 布局——prepend 与 composition 派生，以及 K1↔K2 的位一致契约

#### 4.3.1 概念说明

`make_tma_copy(SM90_TMA_LOAD{}, gmem张量, smem布局)` 构造描述符时，要求 **gmem 张量的模式与 smem 布局的模式一一对应（秩相同）**。本项目内核里，每个 CTA 取的 gmem 切片都被构造成带一个长度 1 的前导维，例如 q/k 块是 `(1, CHUNK, D)`（[fwd_kernel1.cuh:L216-L219](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L216-L219)），workspace 切片是 `(1, CHUNK, D)`（[L519-L522](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L519-L522)）。于是 smem 布局也要在最前面补一个长度 1 的「哑模式」，得到 `((1), M, N)`——这就是 `prepend` 的作用。一维的 beta 盒子本来就是一维，所以 `TMABetaSmemLayout` 直接等于 `BetaSmemLayout`（[fwd_kernel1.cuh:L28](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L28) 的注释："1D TMA, no dummy dim"）。

问题在于：`QKLayout` 是普通 Layout，`prepend` 直接可用；而 `MMALayout` 是 **ComposedLayout**（Swizzle 三件套），不能整体套 `prepend`。解法是把它拆开重组：

```cpp
composition(MMALayout{}.layout_a(),      // 外层映射（Swizzle 部分）
            MMALayout{}.offset(),        // 中间偏移
            prepend(MMALayout{}.layout_b()))  // 内层 Layout 先补哑模式
```

`layout_a() / offset() / layout_b()` 取出三件套的三个成员，`composition(a, offset, b)` 按同样的结构组装回去——语义上等于「保持 swizzle 不动，只对最内层的线性布局做 `prepend`」。（三个访问器与 `composition` 的精确定义在子模块 `cutlass/include/cute/layout.hpp`，建议本地用 IDE 跳转确认。）

最后是本讲的核心认知——**workspace 位一致契约**：K1 用 TMA store 把 k_decayed 等缓冲写进 gmem workspace，K2 再用 TMA load 读回来。TMA 是「按布局搬运」的：**store 侧 smem 布局决定比特怎么排进 gmem，load 侧布局决定比特怎么排回 smem**。两头布局若不完全一致，gmem 里的字节模式就对不上，读出来的就是打乱的矩阵。所以 fwd_launch.cu 里每一对 store/load 描述符都用**同一个 gmem 张量 + 同一个 TMA 布局类型**构造。

#### 4.3.2 核心流程

```text
【布局派生】
plain Layout L        ──prepend──►  ((1), ...)        （秩 +1，补哑模式）
ComposedLayout C      ──layout_a/offset/layout_b 拆开──►
                      ──prepend(layout_b) 后 composition 重组──►  TMA 可用的 ComposedLayout

【K1 → workspace → K2 一条数据的旅程】
K1 smem 缓冲 (MMALayout 排布)
   │ TMA store，smem 视图 = TMAVOLayout            [fwd_kernel1.cuh L517-527]
   ▼
gmem workspace（朴素行主 (n_ht, CHUNK, D)）           [fwd_launch.cu L73]
   │ TMA load，smem 视图 = TMAVOLayout（同一类型！）   [fwd_kernel2.cuh L370-377]
   ▼
K2 smem input[stage].k_decayed (MMALayout 排布) → LDSM_N → HMMA
```

#### 4.3.3 源码精读

**派生定义（K1 侧）。**

[csrc/smxx/fwd_kernel1.cuh:L28-L41](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L28-L41)：

```cpp
using TMABetaSmemLayout = BetaSmemLayout;  // 1D TMA, no dummy dim
using TMAQKLayout = decltype(prepend(QKLayout{}));
using TMAVOLayout = decltype(composition(
    MMALayout{}.layout_a(), MMALayout{}.offset(),
    prepend(MMALayout{}.layout_b())));
using TMAGLayout = decltype(prepend(GLayout{}));
using TMALMLayout = decltype(composition(
    LMLayout{}.layout_a(), LMLayout{}.offset(),
    prepend(LMLayout{}.layout_b())));
using TMAGTotalSmemLayout = decltype(prepend(GTotalLayout{}));
```

规则一目了然：**普通布局 → 直接 `prepend`；ComposedLayout → 三件套拆装**。K2 侧完全同构（[fwd_kernel2.cuh:L41-L57](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L41-L57)），另有 fp32 状态的 `TMAFP32StateSmemLayout` 用同一手法从 `FP32StateSmemLayout` 派生（[L65-L69](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L65-L69)）。

**契约的证据：描述符成对构造。**

[fwd_launch.cu:L98-L114](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L98-L114)：

```cpp
// K1 写出：
auto tma_store_ws_kd  = make_tma_copy(SM90_TMA_STORE{}, m_ws_kd, TMAVOLayout{});
// ...
// K2 读回：
auto tma_load_ws_kd   = make_tma_copy(SM90_TMA_LOAD{},  m_ws_kd, TMAVOLayout{});
```

同一个 `m_ws_kd`、同一个 `TMAVOLayout`——六段 workspace 全部如此（gtotal 用 `TMAGTotalSmemLayout`，INV/Mqk 用 `TMALMLayout`）。gmem 侧布局本身是朴素的（[fwd_launch.cu:L73-L77](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L73-L77)，`(n_ht, CHUNK, D)` 行主），「swizzled 排布」只存在于 smem 两侧；TMA 描述符负责两边的翻译。

**K1 内核里的用法（写）。**

[csrc/smxx/fwd_kernel1.cuh:L515-L527](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L515-L527)（其余五段同构，至 [L582](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L582)）：

```cpp
Tensor s_kd = make_tensor(make_smem_ptr(shared_storage.k_decayed.begin()), TMAVOLayout{});
auto cta_tma = tma_store_ws_kd.get_slice(Int<0>{});
cute::copy(tma_store_ws_kd, cta_tma.partition_S(s_kd), cta_tma.partition_D(g_ws_tile));
tma_store_arrive();
...
tma_store_wait<0>();   // [L584]
```

注意：**计算时**这块缓冲用的是 `MMALayout` 视图（[L348](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L348)），**搬运时**换成 `TMAVOLayout` 视图——同一块内存、两个布局类型，这正是「布局与数据分离」的日常用法。

**K2 内核里的用法（读）。**

[csrc/smxx/fwd_kernel2.cuh:L370-L377](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L370-L377)：LOAD warp 用 `TMAVOLayout` 视图把 k_decayed 拉进流水线缓冲；MMA warp 消费时换 `MMALayout` 视图（[L450](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L450)），再经 LDSM_N 进寄存器片段。状态的 TMA 装载同理用 `TMAStateSmemLayout`（bf16，[L255-L262](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L255-L262)）或 `TMAFP32StateSmemLayout` + 转换函数（fp32，[L284-L304](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L284-L304)）。

**事务字节数：cosize 的又一次出场。**

K1（[fwd_kernel1.cuh:L158-L161](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L158-L161)）：

```cpp
constexpr uint32_t kTmaTransactionBytes =
    uint32_t(cute::cosize_v<QKLayout>) * uint32_t(3 * sizeof(BF16)) +  // q + k + g_bf16
    uint32_t(32) * uint32_t(sizeof(BF16)) +                             // beta
    uint32_t(D) * uint32_t(sizeof(float));                              // dt_bias
```

= 2048×6 + 32×2 + 128×4 = 12864 字节，供 barrier 的事务计数核对（[L206](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L206)）。K2 的对应公式（[fwd_kernel2.cuh:L172-L181](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L172-L181)）= 4096+64+12288+512+1024 = **17984 字节 = 恰好一个 InputStorage 流水级**（可由 4.2 的 cosize 表逐项算出），这是「布局→缓冲区大小→流水线事务量」一条线的闭环。

**union 再省一刀。**

[SharedStorageK2（fwd_kernel2.cuh:L101-L107）](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L101-L107) 把流水线缓冲（3 级输入 + 2 级输出，约 62 KB）与 fp32 状态转换缓冲（64 KB）union：fp32 状态只在流水线循环之前/之后使用，互不重叠。K1 的 Phase A/B union（[fwd_kernel1.cuh:L54-L71](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L54-L71)）同理。字节账全部由 cosize 决定——布局代数直接决定了 SM 占用率。

#### 4.3.4 代码实践（源码阅读型：跟踪一条数据的完整链路）

1. **实践目标**：以 k_decayed 为例，把「K1 产出 → K2 消费」整条链路上每一步用到的布局类型名抄录成表，亲眼验证契约。
2. **操作步骤**：依次打开下列位置，填表（不给答案的格留空自己填）：

   | 步骤 | 位置 | 布局类型 | 用途 |
   |---|---|---|---|
   | 1. K1 decay_apply 写入 | fwd_kernel1.cuh L348 | `MMALayout` | 计算视图 |
   | 2. K1 TMA store smem 视图 | fwd_kernel1.cuh L523 | ？ | 搬运视图 |
   | 3. store 描述符 | fwd_launch.cu L98 | ？ | 契约一半 |
   | 4. workspace gmem 布局 | fwd_launch.cu L73 | 朴素 `(n_ht,16,128)` 行主 | 中转 |
   | 5. load 描述符 | fwd_launch.cu L109 | ？ | 契约另一半 |
   | 6. K2 TMA load smem 视图 | fwd_kernel2.cuh L375 | ？ | 搬运视图 |
   | 7. K2 MMA 读取 | fwd_kernel2.cuh L450 | `MMALayout` | LDSM_N 输入 |

3. **观察现象**：第 2/3/5/6 步填的是**同一个类型名**；第 1 步与第 7 步也是同一个类型名（但与前者不同——一个带哑模式、一个不带）。
4. **预期结果**：`TMAVOLayout` 出现 4 次成对，`MMALayout` 出现 2 次成对；没有任何一步「擅自」换成其他布局。若做思想实验把第 5 步换成别的布局类型，K2 读到的矩阵行列会错乱、输出错误——这就是契约的意义。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `TMABetaSmemLayout` 不需要 `prepend`，而 `TMAGTotalSmemLayout` 需要？
答案：是否补哑模式取决于 gmem 切片的秩：beta 的 gmem 张量与切片是真一维（`beta_gmem_layout = (H*T)`，[fwd_launch.cu:L52-L53](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L52-L53)；内核里切片也是一维，[fwd_kernel1.cuh:L225](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L225)），秩已匹配；g_total/dt_bias 的切片是 `(1, D)`（[fwd_kernel1.cuh:L250-L251](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L250-L251)），smem 布局就要 `prepend` 成 `((1),128)` 与之同秩。

**练习 2**：`TMAVOLayout` 能不能直接写成 `prepend(MMALayout{})`？
答案：不能。`MMALayout` 是 ComposedLayout（swizzle 三件套），`prepend` 作用在普通 Layout 上；直接套会丢掉/无法穿过 swizzle 层。必须 `layout_a()/offset()/layout_b()` 拆开、对 `layout_b()` 做 prepend、再用 `composition` 重组——这正是源码写三行而不是一行的原因（[fwd_kernel1.cuh:L30-L34](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L30-L34)）。

**练习 3**：核对 K2 的 `kTmaTransactionBytes` 公式（[fwd_kernel2.cuh:L173-L181](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L173-L181)）逐项算出总和。
答案：v: 2048×2=4096；beta: 32×2=64；kd/qd/kr: 2048×2×3=12288；g_total: 128×4=512；INV+Mqk: 256×2×2=1024。合计 17984 字节，恰等于一个 `InputStorage` 流水级的字节数（SharedStorageK2 L84-L93 逐成员求和可得同值）。

## 5. 综合实践

**任务：给 `cute_probe.cu` 扩展出「全布局清单 + 字节对账表」。**

1. 把 `K1Layouts<D=128>` 的定义原样复制进探针程序（源码 [fwd_kernel1.cuh:L5-L42](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel1.cuh#L5-L42)），对全部 14 个 `using` 成员逐个 `probe<...>()` 打印布局与 cosize。
2. 对 6 个会进 workspace 的缓冲（k_decayed/q_decayed/k_restored/g_total/INV/Mqk），用 `cosize × sizeof(元素)` 算出字节数，与 `WorkspaceSizes` 常量（utils.cuh:L70-L75）并排成表。
3. 在表下回答两个问题：
   - 哪些 cosize 与 size 不相等？（预期：全部相等——所有布局都是双射；若发现例外，说明该布局有空洞，需解释来源。）
   - `TMAVOLayout` 与 `MMALayout` 的打印差异在哪里？（预期：前者多一个前导 `_1` 模式，swizzle 部分相同。）
4. 产出物：一份 `probe_out.txt` + 一张手写/Markdown 对账表 + 两段结论，归入你的学习笔记。

预期结果：对账表 6 行全部吻合（4096/4096/4096/512/512/512，合计 13824）；打印细节以本机输出为准（**待本地验证**）。

## 6. 本讲小结

- CuTe Layout 是「坐标→偏移」的编译期整数函数：`(shape):(stride)`、`cosize` 决定缓冲区大小；朴素缓冲（q/k/g/beta/g_total/L）用行主 `make_layout`，`cosize` 与 `WorkspaceSizes` 的字节数逐项对得上。
- GMMA atom（`Layout_K_INTER_Atom` / `Layout_MN_INTER_Atom` / `Layout_K_SW32_Atom`）是「swizzle ∘ smem 标记 ∘ 线性布局」的规范 smem 排布积木；K_INTER 是无异或（B=0）的行主形式，MN_INTER 是它的转置视图；`tile_to_shape` 把积木铺满 (16,128)/(16,16)/(128,128)。
- 本项目把 GMMA atom 布局当「三栖」模板用：同一块 smem 同时满足 TMA 写入、TMA 读出、LDSM_N/LDSM_T 直读（实际 MMA 是 SM80 HMMA）。
- TMA smem 布局用 `prepend`（普通布局）或 `layout_a/offset/layout_b + composition`（ComposedLayout）补出秩匹配的哑模式；beta 一维盒是唯一例外。
- **workspace 位一致契约**：K1 store 与 K2 load 用同一 gmem 张量 + 同一 TMA 布局类型构造描述符（fwd_launch.cu L98-103 与 L109-114 成对出现），保证 K1 写下的比特就是 K2 读到的比特；事务字节数公式是 cosize 的下游应用。

## 7. 下一步学习建议

布局已就位，接下来两块拼图建议按此顺序补齐：

1. **多级流水线与 warp 分工**：重读 [fwd_kernel2.cuh:L188-L213](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_kernel2.cuh#L188-L213)（MMA/LOAD/STORE 三种 warp 角色与 `PipelineTmaAsync`/`PipelineAsync`），理解 `InputStorage input[InputStages]` 是如何被本讲的 TMA 布局逐级填充/释放的。
2. **TMA 描述符构造细节**：通读 [fwd_launch.cu:L86-L144](https://github.com/MoonshotAI/FlashKDA/blob/7afb9f454f160a6c4bbc0999beca0a8c40a38934/csrc/smxx/fwd_launch.cu#L86-L144)，并在本地子模块里跳转 `cutlass/include/cute/atom/copy_traits_sm90_tma.hpp` 中的 `make_tma_copy`，确认「gmem 模式与 smem 布局同秩」这条规则在 CuTe 里的静态检查。
3. 随手验证：编译运行第 5 节的综合实践探针，把 `Sw<B,M,S>` 的三个参数抄进笔记——之后任何一讲再遇到 swizzle 布局，你都能秒读它的含义。
