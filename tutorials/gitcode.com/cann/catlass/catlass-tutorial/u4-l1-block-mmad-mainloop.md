# BlockMmad 主循环详解

## 1. 本讲目标

本讲是 U4「Block 层与主循环」的第一篇，进入五层抽象（Device → Kernel → Block → Tile → Basic）的第三层。

学完后你应该能够：

1. 说清楚 **Block 层对应三层嵌套循环中的哪一层**（k_tile 主循环），以及它和 Kernel 层、Tile 层的分工边界。
2. 读懂 `BlockMmad` 的 **模板参数表**（`DispatchPolicy` / `L1TileShape` / `L0TileShape` / `AType` / `BType` / `CType` / `TileCopy` / `TileMmad`），知道每个参数从哪来、控制什么。
3. 理解 **Pingpong 多缓冲**如何用 `STAGES` 片缓冲 + `SetFlag/WaitFlag` 事件同步，让「搬运」与「计算」两条流水并行起来。
4. 能在 `block_mmad_pingpong.hpp` 里准确指出「GM→L1 搬运」「L1→L0 搬运」「TileMmad 计算」「L0C→GM 搬出」四类操作的位置，并解释 ping/pong 是如何切换的。

---

## 2. 前置知识

本讲承接 u2-l4（Kernel 层 SPMD 分核）与 u3-l2（GemmType / TileShape），需要你已建立以下认知：

- **三层嵌套循环模型**：GEMM 被建模为三层 `for` 循环——外层切 M/N（Kernel 层跨核并行）、中间切 K（Block 层累加）、内层做 tile（Tile 层落到硬件指令）。本讲专门拆「中间这一层」。
- **存储层级与流水线**（u1-l2）：数据沿 GM → L1 → L0A/L0B → L0C → UB 内移；MTE2 管 GM→L1，MTE1 管 L1→L0，M 管矩阵乘累加，FIX（Fixpipe）管 L0C→GM。核内不同流水线异步并行，靠 `SetFlag/WaitFlag` 的 HardEvent 同步。
- **GemmType 与 TileShape**（u3-l2）：`GemmType` 把「元素类型 + 布局 + Position」打包成类型容器；`GemmShape<M,N,K>` 是编译期分块尺寸常量，且要同时满足各级存储容量上限与 32 字节对齐。
- **Kernel 如何调用 Block**（u2-l4）：`BasicMatmul::operator()<AIC>` 在 SPMD 步长循环里，为每个 C 基本块算出 GM 偏移后，调用 `blockMmad(gmA, layoutA, gmB, layoutB, gmC, layoutC, actualBlockShape)`。本讲就从这里接着往下读。

> 名词速查：
> - **k_tile / kPart / mPart / nPart**：K 维 L1 分块、L0 分块；M/N 维 L0 分块。
> - **Pingpong（乒乓）**：用两片（或多片）缓冲交替使用，让生产者填下一片的同时消费者用上一片。
> - **HardEvent**：昇腾提供的硬件事件同步原语，`SetFlag<A_B>` 放令牌、`WaitFlag<A_B>` 取令牌，`A_B` 表示「生产流水 A 通知消费流水 B」。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/catlass/gemm/block/block_mmad.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad.hpp) | `BlockMmad` 的**主模板声明**（仅声明、靠 `static_assert` 报未实现），并通过 `#include` 把所有 Block 层主循环实现（pingpong/preload/…）汇聚到一起。 |
| [include/catlass/gemm/block/block_mmad_pingpong.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_pingpong.hpp) | 本讲的主角：`BlockMmad` 针对 `MmadAtlasA2Pingpong` / `MmadPingpong` 的**偏特化实现**，含完整 k_tile 主循环与 pingpong 同步。 |
| [include/catlass/gemm/kernel/basic_matmul.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp) | Kernel 层 `BasicMatmul`，`operator()<AIC>` 里调用 `blockMmad(...)`——看 Block 层的调用上下文。 |
| [include/catlass/gemm/dispatch_policy.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/dispatch_policy.hpp) | `MmadAtlasA2Pingpong` 等 DispatchPolicy 的定义（`STAGES`、`ENABLE_UNIT_FLAG`）。 |
| [include/catlass/arch/resource.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/arch/resource.hpp) | `Arch::Resource<ArchTag>`：把 L1/L0A/L0B/L0C/UB 等片上缓冲统一成可按字节切分的 buffer 句柄。 |
| [include/catlass/gemm/tile/tile_mmad.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/tile/tile_mmad.hpp) | `TileMmad`：Block 层主循环内层调用的「微内核」，最终落到 `AscendC::Mmad`。 |
| docs/zh/3_API/gemm_api.md | 官方三层嵌套循环伪代码与 Block API 说明，是理解本讲的权威参照。 |

---

## 4. 核心概念与源码讲解

### 4.1 Block 层的定位：k_tile 主循环

#### 4.1.1 概念说明

把一次完整的矩阵乘 \(C_{M\times N} = A_{M\times K} B_{K\times N}\) 写成三层嵌套循环，CATLASS 的官方伪代码（见 [docs/zh/3_API/gemm_api.md:12-34](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/3_API/gemm_api.md#L12-L34)）是这样的：

```c++
for (block_m ...) {        // ① Kernel 层：跨 AICore 并行（SPMD 步长循环）
  for (block_n ...) {
    for (k_tile ...) {     // ② Block 层：K 维累加主循环  ← 本讲
      for (tile_mma_m ...) // ③ Tile 层：L0 上的块粒度，完全展开
        for (tile_mma_n ...)
          for (tile_mma_k ...)
            mmad(c, a, b);
}}}
```

三层各司其职：

- **① Kernel 层**（u2-l4 已讲）：不写成显式 `for`，而是用 `GetBlockIdx()` 步长循环把 C 的基本块分给各核，一个核负责若干个 \((M_{L1}\times N_{L1})\) 大小的块。
- **② Block 层**（本讲）：拿到**一个** C 基本块后，沿 K 维切成若干 k_tile，逐块把 A、B 的对应分片搬进片上、做乘累加，最终把这一块 C 算完。**Block 层不并行多核，它就是单核内的 K 维累加循环。**
- **③ Tile 层**（U5 讲）：在 L0 上对一个 k_tile 内部再细分成更小的 \((m,n,k)\) 微块，逐个调用 `AscendC::Mmad`。

一句话记忆：**Kernel 管「分块给谁」，Block 管「一块怎么累加」，Tile 管「一条指令算多大」。**

#### 4.1.2 核心流程

Block 层处理一个 C 基本块 \(C_{m\times n}\)（\(m=M_{L1}, n=N_{L1}\)）的过程：

1. **外层 k_tile 循环**：把 K 维切成 \(K_{tile}\) 份，每份 \(K_{L1}\) 列。
   \[
   kTileCount = \left\lceil \frac{K}{K_{L1}} \right\rceil
   \]
2. **每个 k_tile**：把 A 的 \((m\times K_{L1})\)、B 的 \((K_{L1}\times n)\) 分片从 GM 搬到 L1。
3. **内层 part 循环**：在 L0 上把这一片再细分为 \((M_{L0}\times K_{L0})\)、\((K_{L0}\times N_{L0})\) 的小块，逐块 `Mmad` 累加到 L0C。part 循环次数：
   \[
   mPartLoop=\left\lceil \frac{m}{M_{L0}} \right\rceil,\quad
   kPartLoop=\left\lceil \frac{K_{L1}}{K_{L0}} \right\rceil,\quad
   nPartLoop=\left\lceil \frac{n}{N_{L0}} \right\rceil
   \]
4. **k_tile 循环结束后**：把累加完成的 L0C 经 Fixpipe 搬回 GM 的对应位置。

需要特别注意：**K 维累加发生在 L0C 上**——第一个 k_tile 的首个 kPart 要把累加器清零（`initC=true`），后续都叠加（`initC=false`），直到整块 C 算完才搬出。

#### 4.1.3 源码精读

先看调用上下文。Kernel 层 `operator()<AIC>` 在 SPMD 循环里为每个 C 块算好偏移，然后一次调用 `blockMmad`：

```cpp
// include/catlass/gemm/kernel/basic_matmul.hpp:121-138
for (uint32_t loopIdx = AscendC::GetBlockIdx(); loopIdx < coreLoops; loopIdx += AscendC::GetBlockNum()) {
    GemmCoord blockCoord = matmulBlockScheduler.GetBlockCoord(loopIdx);
    GemmCoord actualBlockShape = matmulBlockScheduler.GetActualBlockShape(blockCoord);
    // 块坐标 × L1TileShape 得逻辑偏移 → layout.GetOffset 得 GM 偏移
    ...
    // 计算一个 C 基本块：Block 层主循环就藏在这个调用里
    blockMmad(gmA[gmOffsetA], params.layoutA, gmB[gmOffsetB], params.layoutB,
              gmC[gmOffsetC], params.layoutC, actualBlockShape);
}
```

参见 [basic_matmul.hpp:121-138](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp#L121-L138)。注意传给 Block 层的 `actualBlockShape` 是「触边裁剪后的真实块尺寸」——靠它 Block 层才知道最后一个不完整的 k_tile / part 有多大。

进入 `block_mmad_pingpong.hpp` 的 `operator()`，k_tile 主循环的真身在这里：

```cpp
// include/catlass/gemm/block/block_mmad_pingpong.hpp:222-223
uint32_t kTileCount = CeilDiv<L1TileShape::K>(actualShape.k());   // K 维要切几片
for (uint32_t kLoopIdx = 0; kLoopIdx < kTileCount; kLoopIdx++) {  // ← Block 层主循环
    ...
}
```

参见 [block_mmad_pingpong.hpp:218-223](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_pingpong.hpp#L218-L223)。`CeilDiv<L1TileShape::K>(actualShape.k())` 正是上面公式里的 \(kTileCount\)。循环体内的 part 三重循环就是 Tile 层工作的位置（见 4.3）。

> 关键结论：Block 层 = `operator()` 里那一个 `for (kLoopIdx ...)` 循环 + 它对 Tile 组件的调度。它对上承接 Kernel 给的一个 C 块，对下驱动 Tile 完成 k_tile 内的微块计算。

#### 4.1.4 代码实践

**实践目标**：确认 Block 层「只切 K、不切 M/N」的边界，理解它与 Kernel 层的分工。

**操作步骤**：

1. 打开 [basic_matmul.hpp:104-141](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp#L104-L141)，找到 Kernel 层用 `GetBlockIdx()`/`GetBlockNum()` 步长分配 C 块的循环（M/N 的并行就在这里隐式表达）。
2. 打开 [block_mmad_pingpong.hpp:222-223](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_pingpong.hpp#L222-L223)，确认 Block 层唯一的「维度假」循环变量是 `kLoopIdx`，上界 `kTileCount` 只与 K 维有关。
3. 在脑中（或纸面上）为 `M=256, N=512, K=1024, L1TileShape=128×256×256` 的情况填表：
   - Kernel 层要处理几个 C 块？\(\lceil256/128\rceil \times \lceil512/256\rceil = 2\times2=4\) 个。
   - Block 层对每个 C 块循环几次？\(\lceil1024/256\rceil = 4\) 次。

**需要观察的现象**：M/N 维的分块数由 Kernel 层的核数与 Swizzle 决定，Block 层完全不关心；Block 层只盯着 K 维累加。

**预期结果**：你会清楚看到「M/N 并行 = Kernel」「K 累加 = Block」这条边界。运行命令需真实 NPU，**待本地验证**（无 NPU 可用 `--simulator`，见 u1-l4）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `L1TileShape::K` 调大一倍（假设容量仍放得下），`kTileCount` 会怎么变？Block 层主循环迭代次数与单次搬运量分别如何变化？

> **答案**：`kTileCount` 减半，主循环迭代次数减半；但单次 GM→L1 搬运的 A/B 分片变大（K 维翻倍）。这是「循环次数」与「单次搬运量」的权衡，是调优的基本旋钮之一。

**练习 2**：Block 层主循环结束后，结果在哪个存储层？要怎样才到 GM？

> **答案**：在 L0C（按累加类型存放，如 fp16 输入按 fp32 累加）。需经 Fixpipe（FIX 流水）从 L0C 搬到 GM，对应代码里 `copyL0CToGm`（见 4.3.3 末尾）。

---

### 4.2 BlockMmad 模板参数与组装

#### 4.2.1 概念说明

`BlockMmad` 是 Block 层的主接口，但它**没有通用实现**——主模板只有一句 `static_assert(DEPENDENT_FALSE<...>)`，逼着使用者必须选一个匹配的 `DispatchPolicy` 来触发某个偏特化。这种「主模板报错 + 偏特化落地」的写法是 CATLASS 实现「基于标签的调度」的标准手法：

```cpp
// include/catlass/gemm/block/block_mmad.hpp:20-28
template <
    class DispatchPolicy, class L1TileShape, class L0TileShape, class AType, class BType, class CType,
    class BiasType = void,
    class TileCopy = Gemm::Tile::TileCopy<typename DispatchPolicy::ArchTag, AType, BType, CType, BiasType>,
    class TileMmad = Gemm::Tile::TileMmad<typename DispatchPolicy::ArchTag, AType, BType, BiasType>,
    class Enable = void>
struct BlockMmad {
    static_assert(DEPENDENT_FALSE<DispatchPolicy>, "BlockMmad is not implemented for this DispatchPolicy");
};
```

参见 [block_mmad.hpp:20-28](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad.hpp#L20-L28)。注意最后那个 `class Enable = void`——它正是偏特化用的 SFINAE「挂钩」。本讲的 pingpong 实现就是用 `std::enable_if_t<MmadPingpongDispatchChecker<DispatchPolicy_>::value>` 去匹配这个 `Enable`。

#### 4.2.2 核心流程

`BlockMmad` 的 8 个有效模板参数按职责分四组：

| 分组 | 参数 | 含义 / 来源 |
| --- | --- | --- |
| **调度策略** | `DispatchPolicy` | 标签类型（如 `MmadAtlasA2Pingpong<true>`），决定走哪个偏特化、`STAGES`、`ENABLE_UNIT_FLAG` 等。内含 `ArchTag`。 |
| **分块尺寸** | `L1TileShape` / `L0TileShape` | `GemmShape<M,N,K>` 编译期常量；L1 与 L0 的 M/N 必须相等，K 满足 \(K_{L0}\le K_{L1}\)。 |
| **数据类型** | `AType` / `BType` / `CType`（+ `BiasType`） | `GemmType` 实例（元素类型 + 布局 + Position），由 u3-l2 讲过。 |
| **Tile 组件** | `TileCopy` / `TileMmad` | 默认由 `ArchTag` 自动挑选；`TileCopy` 内含 GM→L1、L1→L0、L0C→GM 等搬运子组件。 |

组装顺序在 [docs/zh/3_API/gemm_api.md:62-91](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/3_API/gemm_api.md#L62-L91) 有经典示例，即「五步组装」的第一步：

```cpp
using DispatchPolicy = Gemm::MmadAtlasA2Pingpong<true>;       // STAGES=2, ENABLE_UNIT_FLAG=true
using L1TileShape    = GemmShape<128, 256, 256>;
using L0TileShape    = GemmShape<128, 256, 64>;
using AType = Gemm::GemmType<ElementA, LayoutA>;
using BType = Gemm::GemmType<ElementB, LayoutB>;
using CType = Gemm::GemmType<ElementC, LayoutC>;

using BlockMmad = Gemm::Block::BlockMmad<DispatchPolicy, L1TileShape, L0TileShape, AType, BType, CType>;
```

这里只填了前 6 个参数，`BiasType/TileCopy/TileMmad` 走默认值——`TileCopy` 和 `TileMmad` 会用 `DispatchPolicy::ArchTag` 自动选到对应架构的实现，这就是「换 `ArchTag` 即换底层 Tile 组件」的链路（详见 U5）。

`DispatchPolicy` 自己长什么样？看 `MmadAtlasA2Pingpong`：

```cpp
// include/catlass/gemm/dispatch_policy.hpp:31-35
template <bool ENABLE_UNIT_FLAG_ = false>
struct MmadAtlasA2Pingpong : public MmadAtlasA2 {
    static constexpr uint32_t STAGES = 2;                       // 固定双缓冲
    static constexpr bool ENABLE_UNIT_FLAG = ENABLE_UNIT_FLAG_; // Mmad 与 L0C→GM 细粒度并行开关
};
```

参见 [dispatch_policy.hpp:31-35](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/dispatch_policy.hpp#L31-L35)。它继承 `MmadAtlasA2`（带 `ArchTag = Arch::AtlasA2`），所以 `BlockMmad` 能从 `DispatchPolicy::ArchTag` 拿到架构标签。

#### 4.2.3 源码精读

**(a) 偏特化的 enable_if 匹配**

pingpong 偏特化通过两个 helper 守门。先看「是不是 pingpong 类策略」的检查器：

```cpp
// include/catlass/gemm/block/block_mmad_pingpong.hpp:26-43
template <class DispatchPolicy>
struct MmadPingpongDispatchChecker { static constexpr bool value = false; };

template <bool ENABLE_UNIT_FLAG>
struct MmadPingpongDispatchChecker<MmadAtlasA2Pingpong<ENABLE_UNIT_FLAG>> {
    static constexpr bool value = true;   // 匹配 MmadAtlasA2Pingpong<任意>
};
// （另有匹配 MmadPingpong<...> 的全参数特化）
```

参见 [block_mmad_pingpong.hpp:26-43](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_pingpong.hpp#L26-L43)。然后用它当 `Enable` 的条件，落地真正的偏特化：

```cpp
// include/catlass/gemm/block/block_mmad_pingpong.hpp:68-73
template <class DispatchPolicy_, ..., class TileMmad_>
struct BlockMmad<DispatchPolicy_, ..., TileMmad_,
    std::enable_if_t<MmadPingpongDispatchChecker<DispatchPolicy_>::value>> {
    ...
};
```

参见 [block_mmad_pingpong.hpp:68-73](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_pingpong.hpp#L68-L73)。这就是「基于标签的调度」：传 `MmadAtlasA2Pingpong` 就走 pingpong 实现，传 `MmadAtlasA2Preload` 就走另一个文件里的 preload 实现，主类名 `BlockMmad` 始终不变。

**(b) 从 TileCopy 拆出五条搬运子组件**

偏特化一上来就把 `TileCopy_` 里的子组件一个个取出来当成员类型：

```cpp
// include/catlass/gemm/block/block_mmad_pingpong.hpp:87-91
using CopyGmToL1A = typename TileCopy_::CopyGmToL1A;
using CopyGmToL1B = typename TileCopy_::CopyGmToL1B;
using CopyL1ToL0A = typename TileCopy_::CopyL1ToL0A;
using CopyL1ToL0B = typename TileCopy_::CopyL1ToL0B;
using CopyL0CToGm = typename TileCopy_::CopyL0CToGm;
```

参见 [block_mmad_pingpong.hpp:87-91](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_pingpong.hpp#L87-L91)。这五条正好对应本讲关心的四类搬运（GM→L1 的 A/B、L1→L0 的 A/B、L0C→GM）。它们在 [tile_copy.hpp:53-57](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/tile/tile_copy.hpp#L53-L57) 里由 `TileCopy` 模板统一组装，底层按 `ArchTag`/布局/类型特化到 `AscendC::DataCopy`/`LoadData`/`Fixpipe`（U5 详讲）。

**(c) 编译期容量与对齐校验**

偏特化用一连串 `static_assert` 把 u3-l2 讲的「容量 + 对齐」约束在编译期卡死，典型几条：

```cpp
// include/catlass/gemm/block/block_mmad_pingpong.hpp:122 / 128-130 / 133-135
static_assert((L1A_SIZE * STAGES + L1B_SIZE * STAGES) <= ArchTag::L1_SIZE,
              "L1TileShape exceeding the L1 space!");            // L1 要同时放下 STAGES 片 A 和 B
static_assert((L0A_TILE_SIZE * STAGES) <= L0A_SIZE, "...");      // L0A 放得下 STAGES 片
static_assert((L0B_TILE_SIZE * STAGES) <= L0B_SIZE, "...");      // L0B 放得下 STAGES 片
static_assert(L0C_TILE_SIZE <= L0C_SIZE, "...");                 // L0C 单片即可
static_assert(L1TileShape::M == L0TileShape::M && L1TileShape::N == L0TileShape::N,
              "...L1 和 L0 在 m/n 轴不同的情况暂不支持");           // M/N 维 L1=L0
static_assert(L0TileShape::K <= L1TileShape::K, "L0TileShape::K cannot exceed L1TileShape::K");
```

参见 [block_mmad_pingpong.hpp:114-141](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_pingpong.hpp#L114-L141)。注意 L1/L0A/L0B 的容量约束都乘了 `STAGES`——因为 pingpong 要同时留出多片缓冲；L0C 只乘 1，因为本实现里 L0C 单缓冲。这些断言就是你调 TileShape 时编译报错的直接来源。

#### 4.2.4 代码实践

**实践目标**：体会「换 DispatchPolicy 就是换实现」的调度机制，并定位一条编译期校验。

**操作步骤**：

1. 在样例 [examples/00_basic_matmul/basic_matmul.cpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp) 里找到 `using DispatchPolicy` 与 `using BlockMmad` 两行。
2. 对照 [block_mmad.hpp:20-28](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad.hpp#L20-L28) 与 [block_mmad_pingpong.hpp:68-73](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_pingpong.hpp#L68-L73)，确认：样例填了 `MmadAtlasA2Pingpong` → `MmadPingpongDispatchChecker::value == true` → 匹配到 pingpong 偏特化。
3. 尝试在脑中做反例：若把 `DispatchPolicy` 换成一个没有任何偏特化认领它的类型，`BlockMmad` 会落到主模板，触发 `static_assert(DEPENDENT_FALSE<...>)` 报错。

**需要观察的现象**：`BlockMmad` 这个类名在样例里始终不变，变的只是模板实参；底层实现（主循环怎么写、用几片缓冲）完全由 `DispatchPolicy` 决定。

**预期结果**：你应当能解释「为什么 CATLASS 说 `BlockMmad` 提供了一个清晰、单一的扩展点」——要加新主循环策略，只需新增一个 DispatchPolicy 标签 + 一个偏特化，使用者代码无需改类名。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `TileCopy` 和 `TileMmad` 有默认值，使用时通常不显式写？

> **答案**：因为它们都能从 `DispatchPolicy::ArchTag`（加上 AType/BType/CType）自动推导出对应架构的正确实现。使用者只在需要自定义搬运/计算组件时才显式覆盖，默认值降低了组装门槛。

**练习 2**：`L1TileShape::M` 和 `L0TileShape::M` 必须相等（见上述 `static_assert`），但 `K` 维允许 \(K_{L0}\le K_{L1}\)。请解释这条约束的物理含义。

> **答案**：M/N 维的 L1 块和 L0 块一一对应（一个 L1 块内的 M/N 区域正好被 L0 块铺满），实现上不处理两者 M/N 不一致的情况；而 K 维是「L1 搬进来一大片 \(K_{L1}\)，L0 再细分成多个 \(K_{L0}\) 小块逐块算」，所以只要求 L0 的 K 不超过 L1 的 K。

---

### 4.3 Pingpong 多缓冲与流水同步

#### 4.3.1 概念说明

光有 k_tile 循环还不够快。朴素做法是「串行」：搬一片 A/B → 算 → 搬下一片。这样 MTE2（搬运）和 M（计算）两条流水互相等待，大量时间浪费在空泡上。

**Pingpong（乒乓缓冲）** 的核心思想：**给每个存储层准备 `STAGES` 片缓冲，让生产者填「下一片」的同时消费者用「上一片」。** 对 `MmadAtlasA2Pingpong`，`STAGES=2`，即双缓冲：

```
时刻 t:   MTE2 在填 L1_buf[1]    MTE1/M 在用 L1_buf[0]
时刻 t+1: MTE2 在填 L1_buf[0]    MTE1/M 在用 L1_buf[1]
```

要安全地切换，必须保证：**消费者还没用完 buf[0] 时，生产者不能覆盖它；生产者还没填好 buf[1] 时，消费者不能去读它。** 这靠昇腾的 **HardEvent 事件同步**完成——`SetFlag<A_B>` 由生产流水放令牌，`WaitFlag<A_B>` 由消费流水取令牌，每个缓冲片配一个独立 `eventID`，互不干扰。

涉及的 HardEvent 类型（命名约定 `生产_消费`）：

| 事件 | 生产者 → 消费者 | 语义 |
| --- | --- | --- |
| `MTE2_MTE1` | GM→L1 完成 → L1→L0 可读 | L1 数据就绪 |
| `MTE1_MTE2` | L1→L0 读完 → GM→L1 可覆盖 | L1 缓冲可回收（pingpong 关键） |
| `MTE1_M` | L1→L0 完成 → Mmad 可算 | L0 数据就绪 |
| `M_MTE1` | Mmad 用完 → L1→L0 可覆盖 | L0 缓冲可回收 |
| `M_FIX` / `FIX_M` | Mmad 写完 L0C → Fixpipe 可搬出 / Fixpipe 搬完 → L0C 可重用 | 输出通路 |

#### 4.3.2 核心流程

pingpong 实现的 `operator()` 可拆成 **「首块预热 → 主循环（含预取）→ 收尾搬出」** 三段：

1. **预热**（循环前）：先把第 0 个 k_tile 的 A、B 从 GM 搬到 `L1_buf[0]`，并放置初始令牌，让主循环第一次迭代能立刻开始 L1→L0。
2. **主循环** `for (kLoopIdx ...)`：
   - **预取（preload）**：如果不是最后一个 k_tile，就提前把「下一个 k_tile」的 A、B 搬到 `L1_buf[next]`（即另一片缓冲），与当前片的计算重叠。
   - **part 三重循环**（`mPart → kPart → nPart`）：把当前 L1 片细分成 L0 微块，逐块 `L1→L0A`、`L1→L0B`、`tileMmad`，全程用事件同步保证不踩缓冲。
   - 循环末尾切换 `l1ListId = next`，进入下一片。
3. **收尾搬出**：把累加完成的 L0C 经 `copyL0CToGm` 写回 GM（是否需要显式同步取决于 `ENABLE_UNIT_FLAG`）。

**ping/pong 切换的三个游标**：

- `l1ListId`：当前用哪片 L1 缓冲，**每个 k_tile 切一次**（`l1ListId = l1ListIdNext`）。
- `l0AListId`：当前用哪片 L0A 缓冲，**每个 (mPart,kPart) 切一次**。
- `l0BListId`：当前用哪片 L0B 缓冲，**每个 (mPart,kPart,nPart) 切一次**。

切换写法统一是环形递增：`(id + 1 < STAGES) ? (id + 1) : 0`。

#### 4.3.3 源码精读（四类操作 + ping 切换）

**(a) 构造期：分配 STAGES 片缓冲 + 初始化事件令牌**

```cpp
// include/catlass/gemm/block/block_mmad_pingpong.hpp:150-170
for (uint32_t i = 0; i < STAGES; i++) {
    // 给每个 stage 分配 L1/L0A/L0B 缓冲片
    l1ATensorList[i] = resource.l1Buf.template GetBufferByByte<ElementA>(l1AOffset + L1A_SIZE * i);
    l1BTensorList[i] = resource.l1Buf.template GetBufferByByte<ElementB>(l1BOffset + L1B_SIZE * i);
    l0ATensorList[i] = resource.l0ABuf.template GetBufferByByte<ElementA>(L0A_PINGPONG_BUF_SIZE * i);
    l0BTensorList[i] = resource.l0BBuf.template GetBufferByByte<ElementB>(L0B_PINGPONG_BUF_SIZE * i);
    // 每个 stage 配独立 eventID
    l1AEventList[i] = i;  l1BEventList[i] = i + STAGES;
    l0AEventList[i] = i;  l0BEventList[i] = i + STAGES;
    // 预放令牌：假装 MTE1/M 已经「读完/算完」，让首次 MTE2 写入不被阻塞
    AscendC::SetFlag<AscendC::HardEvent::MTE1_MTE2>(l1AEventList[i]);
    AscendC::SetFlag<AscendC::HardEvent::MTE1_MTE2>(l1BEventList[i]);
    AscendC::SetFlag<AscendC::HardEvent::M_MTE1>(l0AEventList[i]);
    AscendC::SetFlag<AscendC::HardEvent::M_MTE1>(l0BEventList[i]);
}
```

参见 [block_mmad_pingpong.hpp:144-171](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_pingpong.hpp#L144-L171)。`resource`（[resource.hpp:19-39](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/arch/resource.hpp#L19-L39)）把 L1/L0A/L0B 等片上缓冲抽象成可按字节偏移切分的句柄。预放的 `SetFlag<MTE1_MTE2>`/`SetFlag<M_MTE1>` 是「初值令牌」——因为首次写入时根本没有前序的读/算，必须手动放一个令牌让 `WaitFlag` 不死等。

**(b) 操作一：GM→L1 搬运（MTE2 流水）**

主循环之前的「首片」搬运：

```cpp
// include/catlass/gemm/block/block_mmad_pingpong.hpp:203-212
AscendC::WaitFlag<HardEvent::MTE1_MTE2>(l1AEventList[l1ListId]); // 等 MTE1 读完这片 L1（首次靠初值令牌放行）
auto layoutTileA = layoutA.GetTileLayout(MakeCoord(actualShape.m(), kActual));
copyGmToL1A(l1ATensorList[l1ListId], gmA, layoutAInL1, layoutTileA);  // GM → L1
AscendC::SetFlag<HardEvent::MTE2_MTE1>(l1AEventList[l1ListId]);   // 通知 MTE1：这片 L1 数据就绪
```

主循环内的「预取下一片」搬运（这是 pingpong 的精髓——**算当前片的同时搬下一片**）：

```cpp
// include/catlass/gemm/block/block_mmad_pingpong.hpp:232-251（节选）
auto l1ATensor = l1ATensorList[l1ListIdNext];          // 用「另一片」L1 缓冲
auto gmTileA = gmA[layoutA.GetOffset(gmTileAOffset)];
AscendC::WaitFlag<HardEvent::MTE1_MTE2>(l1AEventList[l1ListIdNext]); // 等下一片可写
copyGmToL1A(l1ATensor, gmTileA, layoutAInL1, layoutTileA);           // GM → L1[next]
AscendC::SetFlag<HardEvent::MTE2_MTE1>(l1AEventList[l1ListIdNext]);  // 通知 MTE1：下一片就绪
```

参见 [block_mmad_pingpong.hpp:202-252](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_pingpong.hpp#L202-L252)。B 矩阵的搬运同理（`copyGmToL1B`）。

**(c) 操作二：L1→L0 搬运（MTE1 流水）**

part 循环里把 L1 上的微块搬到 L0A/L0B：

```cpp
// include/catlass/gemm/block/block_mmad_pingpong.hpp:276-282（A：L1→L0A）
AscendC::WaitFlag<HardEvent::M_MTE1>(l0AEventList[l0AListId]);     // 等 M 算完这片 L0A
if ((mPartIdx == 0) && (kPartIdx == 0)) {
    AscendC::WaitFlag<HardEvent::MTE2_MTE1>(l1AEventList[l1ListId]); // 首个 part 还要等 GM→L1 完成
}
copyL1ToL0A(l0ATile, l1ATile, layoutAInL0, layoutAInL1);           // L1 → L0A
```

B 的 `copyL1ToL0B` 在 [block_mmad_pingpong.hpp:300-307](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_pingpong.hpp#L300-L307)。

**(d) 操作三：TileMmad 计算（M 流水）**

这是 part 循环最内层，也是 K 维累加真正发生的地方：

```cpp
// include/catlass/gemm/block/block_mmad_pingpong.hpp:316-337（节选）
auto l0CTile = l0CTensor[layoutInL0C.GetOffset(l0COffset)];   // 定位 L0C 上的输出微块
AscendC::WaitFlag<HardEvent::MTE1_M>(EVENT_ID0);              // 等 L1→L0 完成

bool initC = ((kLoopIdx == 0) && (kPartIdx == 0));            // 首个 k_tile 首个 kPart：清零累加器
uint8_t unitFlag = 0b00;
if constexpr (ENABLE_UNIT_FLAG) {
    if ((kLoopIdx == kTileCount - 1) && (mPartIdx == mPartLoop - 1) &&
        (kPartIdx == kPartLoop - 1) && (nPartIdx == nPartLoop - 1)) {
        unitFlag = 0b11;   // 最后一块：触发 L0C→GM 随路搬出
    } else {
        unitFlag = 0b10;   // 中间块：继续在 L0C 累加
    }
}
tileMmad(l0CTile, l0ATile, l0BTile, mPartActual, nPartActual, kPartActual, initC, unitFlag);
```

参见 [block_mmad_pingpong.hpp:316-337](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_pingpong.hpp#L316-L337)。两个控制位很关键：

- **`initC`**：只在「第一个 k_tile 的第一个 kPart」为 `true`，把 L0C 清零；之后全为 `false`，沿 K 维持续叠加。这正是 Block 层「K 维累加」的体现。它最终填进 `MmadParams.cmatrixInitVal`（见 [tile_mmad.hpp:48-53](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/tile/tile_mmad.hpp#L48-L53)）。
- **`unitFlag`**：`ENABLE_UNIT_FLAG=true` 时，最后一块给 `0b11` 让 Mmad 指令随路触发 Fixpipe（L0C→GM），其余给 `0b10` 只累加不搬出。这样 Mmad 与 L0C→GM 形成**细粒度并行**——计算还没全结束时输出就已开始搬走。

**(e) 操作四：L0C→GM 搬出（FIX 流水）**

```cpp
// include/catlass/gemm/block/block_mmad_pingpong.hpp:351-361
if constexpr (!ENABLE_UNIT_FLAG) {
    AscendC::SetFlag<HardEvent::M_FIX>(EVENT_ID0);    // 通知 FIX：L0C 写完了
    AscendC::WaitFlag<HardEvent::M_FIX>(EVENT_ID0);   // FIX 排上队
    copyL0CToGm(gmC, l0CTensor, layoutBlock, layoutInL0C);
    AscendC::SetFlag<HardEvent::FIX_M>(EVENT_ID0);    // 通知 M：L0C 可重用
} else {
    copyL0CToGm(gmC, l0CTensor, layoutBlock, layoutInL0C, 0b11); // unitFlag 路径：搬出已随路完成
}
```

参见 [block_mmad_pingpong.hpp:351-361](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_pingpong.hpp#L351-L361)。两种搬出方式对比：

- `!ENABLE_UNIT_FLAG`：主循环结束后，**显式**用 `M_FIX`/`FIX_M` 同步再搬出（串行收尾）。
- `ENABLE_UNIT_FLAG`：最后一块 Mmad 的 `unitFlag=0b11` 已**随路**触发 Fixpipe，这里只需再补一次搬出调用，整体更并行。

**(f) ping/pong 切换点**

三个游标的环形递增散落在 part 循环里：

```cpp
// L0B：每个 nPart 切一次   [block_mmad_pingpong.hpp:340-341]
AscendC::SetFlag<HardEvent::M_MTE1>(l0BEventList[l0BListId]);   // 通知 MTE1：这片 L0B 可回收
l0BListId = (l0BListId + 1 < STAGES) ? (l0BListId + 1) : 0;     // ← ping 切换

// L0A：每个 (mPart,kPart) 切一次   [block_mmad_pingpong.hpp:343-344]
AscendC::SetFlag<HardEvent::M_MTE1>(l0AEventList[l0AListId]);
l0AListId = (l0AListId + 1 < STAGES) ? (l0AListId + 1) : 0;     // ← ping 切换

// L1：每个 kTile 切一次   [block_mmad_pingpong.hpp:347-348]
l1ListId = l1ListIdNext;
kActual = kActualNext;
```

参见 [block_mmad_pingpong.hpp:339-348](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_pingpong.hpp#L339-L348)。`l1ListIdNext` 在循环开头就算好了（[L224](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_pingpong.hpp#L224)），整个循环内「当前片用 `l1ListId`、预取片用 `l1ListIdNext`」严格分离，互不踩踏——这就是 pingpong 能成立的根本。

> 速记表：四类操作在哪几行
> | 操作 | 流水 | 代码位置 |
> | --- | --- | --- |
> | GM→L1（首片+预取） | MTE2 | L202-252 |
> | L1→L0A / L1→L0B | MTE1 | L276-282 / L300-307 |
> | TileMmad | M | L316-337 |
> | L0C→GM | FIX | L351-361 |

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：在 `block_mmad_pingpong.hpp` 的 `operator()` 里标注四类操作，并指出 ping 是如何切换的。这是本讲规格里指定的实践任务。

**操作步骤**：

1. 打开 [block_mmad_pingpong.hpp:186-362](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_pingpong.hpp#L186-L362)（整个 `operator()`）。
2. 用四种颜色/标记分别圈出：
   - **GM→L1 搬运**：所有 `copyGmToL1A` / `copyGmToL1B` 调用（首片在 L205、L211；预取在 L244、L250）。
   - **L1→L0 搬运**：`copyL1ToL0A`（L282）、`copyL1ToL0B`（L307）。
   - **TileMmad 计算**：`tileMmad(...)`（L337）。
   - **L0C→GM 搬出**：`copyL0CToGm(...)`（L357 或 L360）。
3. 圈出三个 ping 切换点：`l0BListId`（L341）、`l0AListId`（L344）、`l1ListId = l1ListIdNext`（L347）。
4. 回答两个问题：
   - 为什么预取片用 `l1ListIdNext` 而当前片用 `l1ListId`？（答：让 MTE2 填下一片的同时 MTE1/M 用当前片，两者物理上是不同的 L1 缓冲片，靠独立 eventID 同步，互不覆盖。）
   - `initC=true` 出现在什么条件？为什么只在那里？（答：`(kLoopIdx==0) && (kPartIdx==0)`，即整块 C 的第一次累加，需把 L0C 清零；之后沿 K 叠加。）

**需要观察的现象**：四类操作在代码里并非「先全部搬完再全部算」，而是**交错**排布——GM→L1(下一片) 排在 L1→L0(当前片) 之前，形成搬运与计算的重叠；事件同步（`WaitFlag`/`SetFlag`）保证这种重叠是安全的。

**预期结果**：你能对着代码画出一轮 k_tile 的时间线——MTE2 填 `L1[1]`、MTE1 把 `L1[0]` 拆进 L0、M 做 Mmad——三者并行推进，这正是 pingpong 相对单缓冲省时的原因。运行验证需真实 NPU，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：构造函数里为什么要预先 `SetFlag<MTE1_MTE2>` 和 `SetFlag<M_MTE1>`（[L164-167](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_pingpong.hpp#L164-L167)）？

> **答案**：因为 `operator()` 一进来就 `WaitFlag<MTE1_MTE2>`（等「MTE1 读完 L1」）和 `WaitFlag<M_MTE1>`（等「M 算完 L0」）。首次执行时根本没有前序读/算，若不预放令牌，`WaitFlag` 会永远等不到而挂死。预放的令牌代表「缓冲初始为空闲，可以写入」。

**练习 2**：`ENABLE_UNIT_FLAG=true` 时，为什么最后一块 Mmad 给 `unitFlag=0b11`、其余给 `0b10`？

> **答案**：`0b11` 让 Mmad 指令在累加的同时**随路触发 Fixpipe**（L0C→GM），把这一块的最终结果搬走；中间块给 `0b10` 只累加不搬出（因为还要继续叠加后续 k_tile）。这样输出搬运不必等到整个主循环结束，与计算细粒度并行，缩短收尾时间。

**练习 3**：析构函数（[L174-184](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_pingpong.hpp#L174-L184)）为什么要把所有 `WaitFlag` 都取干净？

> **答案**：构造时预放了令牌，运行中又 `Set/Wait` 多轮。析构时统一 `WaitFlag` 是为了「配平」事件计数——确保所有异步搬运/计算确实完成、缓冲可安全释放，避免令牌残留影响下一次调用或资源回收。

---

## 5. 综合实践

**任务**：给一个具体的 GEMM 配置，**完整跟踪一次 Block 层主循环的执行**，把「分块计数—四类操作—ping 切换—同步事件」串起来。

**配置**：`M=256, N=256, K=512`，`L1TileShape=128×128×256`，`L0TileShape=128×128×64`，`DispatchPolicy=MmadAtlasA2Pingpong<true>`（`STAGES=2`）。

**步骤**：

1. **算循环次数**（用本讲公式，手算）：
   - `kTileCount` = \(\lceil 512/256\rceil = 2\)
   - `mPartLoop` = \(\lceil 128/128\rceil = 1\)，`nPartLoop` = 1，`kPartLoop` = \(\lceil 256/64\rceil = 4\)
2. **列出第一个 k_tile（kLoopIdx=0）的事件序列**：参照 4.3.3，按代码顺序写出
   - 预热 GM→L1：`WaitFlag<MTE1_MTE2>[0]` → `copyGmToL1A/B` → `SetFlag<MTE2_MTE1>[0]`
   - 预取下一片 GM→L1[1]（因为 kLoopIdx < kTileCount-1）
   - part 循环（mPart=0,kPart=0..3,nPart=0）：每次 `L1→L0A` → `L1→L0B` → `tileMmad`，注意 `initC` 只在 (kLoopIdx=0,kPart=0) 为 true
   - 末尾 `l1ListId` 切到 1
3. **标注第二个 k_tile（kLoopIdx=1，最后一片）**：此时不再预取；最后的 `tileMmad` 因 `ENABLE_UNIT_FLAG=true` 且是最后一块，`unitFlag=0b11`。
4. **画出 L1 两片缓冲的占用时间线**：`L1[0]` 在 kLoopIdx=0 被消费、kLoopIdx=1 被（预取）复用；体会「消费 buf[0] 时填 buf[1]」的乒乓。
5. **对照源码核验**：每个事件名、每个 `copyXxx`、每个游标切换都能在 [block_mmad_pingpong.hpp:186-362](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_pingpong.hpp#L186-L362) 找到对应行。

**交付物**：一张表，列出两轮 k_tile 中每一步的操作类型、流水、用到的 eventID、ping 游标取值。

**预期结果**：你能向别人讲清楚「一个 C 基本块从进 Block 层到写出 GM，中间到底发生了什么、为什么这么排」。运行验证需真实 NPU，**待本地验证**；若想观察事件时序，可结合 `--msdebug`（见 u11-l3）。

---

## 6. 本讲小结

- **Block 层 = k_tile 主循环**：它对应三层嵌套循环的中间层，承接 Kernel 给的一个 C 基本块，沿 K 维累加；M/N 并行归 Kernel，K 累加归 Block，微块指令归 Tile。
- **`BlockMmad` 靠标签调度**：主模板只有 `static_assert` 报错，真正的实现是各 DispatchPolicy 对应的偏特化；换 `DispatchPolicy` 即换主循环实现，类名不变。
- **模板参数四组**：调度策略（`DispatchPolicy`）、分块尺寸（`L1/L0TileShape`）、数据类型（`AType/BType/CType`）、Tile 组件（`TileCopy/TileMmad`，默认由 `ArchTag` 自动选）。容量与对齐约束被编译期 `static_assert` 卡死。
- **Pingpong = STAGES 片缓冲 + HardEvent 同步**：L1/L0A/L0B 各留 `STAGES` 片，生产者填下一片、消费者用当前片；用 `MTE2_MTE1`/`MTE1_MTE2`/`MTE1_M`/`M_MTE1`/`M_FIX`/`FIX_M` 等事件配对保证安全。
- **四类操作的位置**：GM→L1（`copyGmToL1A/B`）、L1→L0（`copyL1ToL0A/B`）、TileMmad（`tileMmad`）、L0C→GM（`copyL0CToGm`）；ping 切换靠 `l1ListId/l0AListId/l0BListId` 环形递增。
- **两个关键控制位**：`initC` 控制 K 维累加的首次清零；`unitFlag`（`ENABLE_UNIT_FLAG`）让 Mmad 与 L0C→GM 随路并行，缩短收尾。

---

## 7. 下一步学习建议

- **横向对比其他主循环策略**：本讲只读了 `MmadAtlasA2Pingpong`。建议接着读 [block_mmad_preload.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_preload.hpp) 与 `block_mmad_preload_async.hpp`，对比它们如何用更多 stage 消除 GM→L1 空泡（对应 u4-l2/u4-l3）。
- **下到 Tile 层**：本讲把 `tileMmad`/`copyGmToL1A` 等当黑盒调用。U5 会拆开它们，看 `TileMmad` 如何填 `MmadParams` 调 `AscendC::Mmad`、`TileCopy` 如何按架构特化到 `DataCopy`/`LoadData`/`Fixpipe`（u5-l1、u5-l2）。
- **补全调度策略地图**：读 [dispatch_policy.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/dispatch_policy.hpp) 全文，了解 `Preload`/`PreloadAsync`/`PreloadAsyncWithCallback` 及其 `STAGES`/`ENABLE_SHUFFLE_K` 参数，为 u4-l2 打底。
- **Scheduler 侧**：本讲的 `actualShape`（触边裁剪尺寸）来自 `BlockScheduler`，可结合 u4-l4（Swizzle）与 u2-l4 理解「块坐标 → 真实块尺寸」的完整链路。
