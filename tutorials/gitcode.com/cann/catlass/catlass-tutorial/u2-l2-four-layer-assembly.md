# 四层组装范式总览

## 1. 本讲目标

上一讲（u2-l1）我们把 `00_basic_matmul` 的 Host 侧流程切成了「初始化—分配—拷贝—执行—对比—释放」六阶段，并在「执行」阶段（代码第 106–115 行）只**指认**了一串 `using` 类型别名，没有解释它们是什么。本讲就来回答这一串别名：

- 掌握用 `DispatchPolicy` / `L1TileShape` / `L0TileShape` / `GemmType` 组装 `BlockMmad` 的写法，理解每个模板参数的来源与含义；
- 理解 `Kernel = BasicMatmul<BlockMmad, BlockEpilogue, BlockScheduler>` 的组合方式，以及 `BlockScheduler`、`BlockEpilogue` 在其中扮演的角色；
- 理解 `DeviceGemm` 如何作为 Host 侧适配器包装 Kernel，串起从 Host 类型组装到设备侧真正执行（`<<<blockDim>>>`）的整条调用链。

学完本讲，你应当能独立写出 CATLASS「五步组装」的 `using` 链条，并解释从 Host 一直下到设备侧 kernel 启动的完整路径。

## 2. 前置知识

本讲默认你已建立以下认知（来自 u1-l1、u1-l2、u2-l1）：

- **五层抽象**：Device → Kernel → Block → Tile → Basic。Device 是 Host 调用入口，Kernel 负责多核编排，Block 是单核主循环，Tile 是可组合微内核，Basic 封装硬件指令（如 `AscendC::Mmad`）。
- **三层嵌套循环**：矩阵乘被建模为三层 `for`。最外两层切 M/N（由 Kernel 层把不同块分给不同核，多核并行），中间层切 K（由 Block 层做累加主循环），最内层落到 Tile/Basic 的 `mmad` 指令。
- **SPMD 多核模型**：所有核跑同一份 kernel，靠 `GetBlockIdx()` / `GetBlockNum()` 的步长循环认领不同 C 基本块。
- **Host 执行阶段**：`Arguments` → `CanImplement` → `GetWorkspaceSize` → `Initialize` → `operator()(stream, aicCoreNum)`。

如果你还不清楚「五层抽象对应目录里的哪个子目录」，建议先回看 u1-l3 的「目录路径即分层抽象」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [examples/00_basic_matmul/basic_matmul.cpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L86-L115) | 本讲主战场：第 86–115 行写出了完整的五步组装与执行调用。 |
| [include/catlass/gemm/block/block_mmad.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad.hpp#L20-L28) | `BlockMmad` 的主模板声明：定义了组装 Block 主循环所需的全部模板参数。 |
| [include/catlass/gemm/kernel/basic_matmul.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp#L23-L141) | `BasicMatmul` 内核：`Params`/`Arguments`、`ToUnderlyingArguments`、SPMD 主循环 `operator()<AIC>`。 |
| [include/catlass/gemm/device/device_gemm.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/device/device_gemm.hpp#L21-L98) | `DeviceGemm`：Host 侧适配器，封装 `CanImplement`/`GetWorkspaceSize`/`Initialize`/`operator()` 并启动 kernel。 |
| [include/catlass/gemm/block/block_swizzle.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_swizzle.hpp#L24-L114) | `GemmIdentityBlockSwizzle`：决定 C 基本块的遍历顺序（Swizzle）。 |
| [include/catlass/gemm/dispatch_policy.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/dispatch_policy.hpp#L21-L35) | `MmadAtlasA2Pingpong` 等调度策略：用标签驱动 Block 层特化。 |
| [include/catlass/gemm/gemm_type.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/gemm_type.hpp#L20-L25) | `GemmType`：把「元素类型 + 布局」绑定成一个可传递的类型。 |
| [include/catlass/gemm_coord.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm_coord.hpp#L20-L37) | `GemmShape`：用 `(M, N, K)` 表达分块尺寸。 |

## 4. 核心概念与源码讲解

本讲按「最小模块」拆成三块：**4.1 BlockMmad 组装**、**4.2 Kernel 组合**、**4.3 DeviceGemm 包装**。它们正好对应五步组装中的「第一步」「第二到第四步」「第五步」。

在开始前，先把五步组装的全貌画出来（来自 `basic_matmul.cpp` 的真实代码）：

```
第一步  BlockMmad       = BlockMmad<DispatchPolicy, L1TileShape, L0TileShape, AType, BType, CType>
第二步  BlockEpilogue   = void                                          // 本例不做后处理
第三步  BlockScheduler  = GemmIdentityBlockSwizzle<3, 0>                // C 块遍历顺序
第四步  MatmulKernel    = BasicMatmul<BlockMmad, BlockEpilogue, BlockScheduler>
第五步  MatmulAdapter   = DeviceGemm<MatmulKernel>                       // Host 适配器
```

这五步是**自底向上**的：先造最底层的计算砖块（BlockMmad），再一层层往上包。下面逐步拆解。

### 4.1 BlockMmad 组装：把数据类型、布局、分块、调度策略绑成一个主循环

#### 4.1.1 概念说明

`BlockMmad` 是 Block 层的接口，对应三层嵌套循环里的**中间层 `k_tile` 主循环**——它在一个核内，把分配给本核的某块 C（尺寸为 `L1TileShape`）算出来：从 GM 把 A、B 的分片搬到 L1、L0，反复执行 `mmad` 累加，最后把 L0C 的结果写回 GM。

为什么要「组装」它？因为一个主循环的行为由很多**正交**的选择共同决定：

- 用哪种调度策略（pingpong？preload？）；
- L1 和 L0 上的基本块多大；
- A/B/C 各是什么元素类型、什么排布（行优先 / 列优先 / NZ）。

CATLASS 的做法是把这些选择作为**模板参数**一次性传给 `BlockMmad`，编译期就生成对应的特化实现。这样「换需求」就退化成「换参数」，而不用改主循环代码。

#### 4.1.2 核心流程

组装 `BlockMmad` 的流程：

1. 选 **`DispatchPolicy`**：决定架构（`ArchTag`，如 `AtlasA2`）与缓冲策略（`STAGES`、`ENABLE_UNIT_FLAG`）。
2. 选 **`L1TileShape`** 与 **`L0TileShape`**：用 `GemmShape<M,N,K>` 表达 L1/L0 上的基本块尺寸，需满足硬件容量约束。
3. 用 **`GemmType`** 把 A/B/C 的「元素类型 + 布局」各绑成一个类型。
4. 把以上 6 个参数传给 `BlockMmad` 模板，得到一个具体的 Block 主循环类型。

`BlockMmad` 内部还会根据 `DispatchPolicy::ArchTag` 自动挑出配套的 `TileCopy`（搬运族）与 `TileMmad`（微内核）——这两个是模板默认参数，本讲先用默认值，其内部留待 U5 拆解。

#### 4.1.3 源码精读

先看 Host 侧的组装（第一步）：

[examples/00_basic_matmul/basic_matmul.cpp:86-100](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L86-L100) — 这 15 行就是 BlockMmad 的全部组装：

```cpp
using ArchTag = Arch::AtlasA2;                          // 架构标签
using DispatchPolicy = Gemm::MmadAtlasA2Pingpong<true>; // true = ENABLE_UNIT_FLAG
using L1TileShape = GemmShape<128, 256, 256>;           // L1 基本块 (M,N,K)
using L0TileShape = GemmShape<128, 256, 64>;            // L0 基本块 (M,N,K)

using AType = Gemm::GemmType<ElementA, LayoutA>;        // half + RowMajor
using BType = Gemm::GemmType<ElementB, LayoutB>;
using CType = Gemm::GemmType<ElementC, LayoutC>;

using BlockMmad = Gemm::Block::BlockMmad<
    DispatchPolicy, L1TileShape, L0TileShape, AType, BType, CType>;
```

逐个参数看来源：

- **`DispatchPolicy = MmadAtlasA2Pingpong<true>`**：定义在 [dispatch_policy.hpp:31-35](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/dispatch_policy.hpp#L31-L35)。它继承自 `MmadBase<Arch::AtlasA2>`（[dispatch_policy.hpp:21-25](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/dispatch_policy.hpp#L21-L25)），因此携带 `ArchTag = AtlasA2`；并给出两个常量：`STAGES = 2`（L1 双缓冲 / pingpong）和 `ENABLE_UNIT_FLAG = true`（开启 `mmad` 与 L0C→GM 搬出的细粒度并行）。注意它**没传** `ArchTag`，架构是继承来的——这就是后面 `DispatchPolicy::ArchTag` 能取到 `AtlasA2` 的原因。

- **`L1TileShape = GemmShape<128, 256, 256>`**：`GemmShape` 是个纯编译期常量容器，定义在 [gemm_coord.hpp:20-37](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm_coord.hpp#L20-L37)，只有 `static constexpr M/N/K`。这里表示 L1 上的基本块是 128(M)×256(N)×256(K)。

- **`GemmType<half, RowMajor>`**：`GemmType` 只是把「元素类型 + 布局 + 默认位置 GM」绑成一个结构体，见 [gemm_type.hpp:20-25](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/gemm_type.hpp#L20-L25)。本例 `ElementA/B/C` 都是 `half`（[basic_matmul.cpp:48-50](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L48-L50)），`LayoutA/B/C` 都是 `RowMajor`（[basic_matmul.cpp:60-62](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L60-L62)）。

再看 `BlockMmad` 的主模板声明，确认这些参数怎么被接收：

[include/catlass/gemm/block/block_mmad.hpp:20-28](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad.hpp#L20-L28) — 这是**主模板（未特化）**，故意 `static_assert` 报错，提示「该 DispatchPolicy 未实现」：

```cpp
template <
    class DispatchPolicy, class L1TileShape, class L0TileShape,
    class AType, class BType, class CType,
    class BiasType = void,
    class TileCopy = Gemm::Tile::TileCopy<typename DispatchPolicy::ArchTag, AType, BType, CType, BiasType>,
    class TileMmad = Gemm::Tile::TileMmad<typename DispatchPolicy::ArchTag, AType, BType, BiasType>,
    class Enable = void>
struct BlockMmad {
    static_assert(DEPENDENT_FALSE<DispatchPolicy>, "BlockMmad is not implemented for this DispatchPolicy");
};
```

关键有两点：

1. 我们传的 6 个参数对应前 6 个模板形参；`BiasType`/`TileCopy`/`TileMmad`/`Enable` 都有默认值，本例都没显式传。
2. 注意默认值里的 `typename DispatchPolicy::ArchTag`——`TileCopy` 和 `TileMmad` 是**根据你选的架构自动挑选**的。这就是「换 `DispatchPolicy` → 换架构 → 自动换底层 Tile 组件」的机制。真正的实现是针对每个 `DispatchPolicy` 的**偏特化**（如 pingpong 版本在 `block_mmad_pingpong.hpp`，本讲不深入其内部循环）。

#### 4.1.4 代码实践

**实践目标**：确认 L1TileShape 是否放得下 L1，理解「换参数」对组装的影响。

**操作步骤**：

1. 打开 [basic_matmul.cpp:86-100](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L86-L100)，把 6 个组装参数逐一对应到 [block_mmad.hpp:20-28](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad.hpp#L20-L28) 的形参，写成一张表（参数名 → 取值 → 来自哪一行）。
2. 手算一个 L1 分片的体积（fp16，每元素 2 字节）：
   - A 分片：`L1TileShape.M × L1TileShape.K × 2` 字节；
   - B 分片：`L1TileShape.K × L1TileShape.N × 2` 字节。
3. 把 `L1TileShape` 从 `<128, 256, 256>` 改大（例如 `<128, 256, 512>`），重新编译，观察是否仍能通过（**待本地验证**：能否编译取决于是否超出 L1 容量与 pingpong 双缓冲后的可用空间）。

**需要观察的现象**：

- 手算每组分片体积，乘以 `STAGES=2`（pingpong 双缓冲），与 AtlasA2 的 `L1_SIZE`（512KB，见 u1-l2 / arch.hpp）比较，判断是否放得下。
- 改大 K 后，若超容量，编译期或运行期应有报错提示（**待本地验证**报错形式）。

**预期结果**：能写出 6 个参数的对应表；能定性判断「K 翻倍会让 L1 占用翻倍，可能超容」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `DispatchPolicy` 换成 `MmadAtlasA2Preload`（[dispatch_policy.hpp:59-64](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/dispatch_policy.hpp#L59-L64)），`BlockMmad` 还需要改哪些参数？

> **答案**：不用改 `BlockMmad` 的其他参数。`MmadAtlasA2Preload` 同样继承自 `MmadBase<Arch::AtlasA2>`，`ArchTag` 仍是 `AtlasA2`，因此 `TileCopy`/`TileMmad` 的自动挑选不受影响；只是它走的是「Preload 预加载」这条偏特化主循环，会额外用到 `ENABLE_SHUFFLE_K` 等参数（其内部差异留待 U4）。

**练习 2**：本例为什么没传 `BiasType`？

> **答案**：`BiasType` 在主模板里有默认值 `void`（[block_mmad.hpp:22](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad.hpp#L22)）。本例 `C = A*B`，没有 bias，所以用默认 `void` 即可。要做带 bias 的算子时再显式传一个 `GemmType`。

### 4.2 Kernel 组合：用 BasicMatmul 把 Block、后处理、调度器拼成无状态内核

#### 4.2.1 概念说明

有了 `BlockMmad`（一个核内怎么算一块 C）之后，还需要回答两个跨核的问题：

- **谁来算哪一块**？——由 `BlockScheduler`（含 Swizzle）决定，它把 `loopIdx` 映射成 C 基本块的 (m, n) 坐标。
- **算完之后要不要做后处理**？——由 `BlockEpilogue` 决定，例如 `C = beta*C + alpha*A*B` 里的 `beta*C`、bias、激活等。

`BasicMatmul` 就是把它们组合起来的 Kernel 层入口。它是**无状态**的：调用者（即 Device 层）负责管理状态，Kernel 只接收 `Params` 描述输入输出。

#### 4.2.2 核心流程

Kernel 组合（第二到第四步）：

```
第二步  BlockEpilogue  = void                      // 不做后处理
第三步  BlockScheduler = GemmIdentityBlockSwizzle<3,0>
第四步  MatmulKernel   = BasicMatmul<BlockMmad, BlockEpilogue, BlockScheduler>
```

Kernel 内部的运行期职责（设备侧）：

1. 用 `BlockScheduler` + `L1TileShape` 算出总任务数 `coreLoops`；
2. 进入 SPMD 步长循环：`for (loopIdx = GetBlockIdx(); loopIdx < coreLoops; loopIdx += GetBlockNum())`；
3. 每轮用 `GetBlockCoord(loopIdx)` 得到本块坐标，换算成 A/B/C 在 GM 上的字节偏移；
4. 调用 `blockMmad(...)` 执行单块计算（进入 4.1 那个 Block 主循环）。

#### 4.2.3 源码精读

先看 Host 侧组合（[basic_matmul.cpp:97-103](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L97-L103)）：

```cpp
using BlockEpilogue = void;                                          // 第二步：无后处理
using BlockScheduler = typename Gemm::Block::GemmIdentityBlockSwizzle<3, 0>;  // 第三步
using MatmulKernel = Gemm::Kernel::BasicMatmul<
    BlockMmad, BlockEpilogue, BlockScheduler>;                       // 第四步
```

- **`BlockEpilogue = void`**：本例 `C = A*B`，没有后处理，所以直接用 `void`。后续需要激活/bias 时，会换成一个真实的 `BlockEpilogue<...>`（留待 U6）。
- **`BlockScheduler = GemmIdentityBlockSwizzle<3, 0>`**：模板参数是 `SwizzleOffset=3, SwizzleDirection=0`。它决定 C 基本块的遍历顺序：`direction=0` 表示按 Z 字形（Zn）遍历，`offset=3` 是 Z 形折返的块数。其定义见 [block_swizzle.hpp:24-25](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_swizzle.hpp#L24-L25)（细节留待 u4-l4）。

再看 `BasicMatmul` 本体（[basic_matmul.hpp:23-24](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp#L23-L24)）：

```cpp
template <class BlockMmad_, class BlockEpilogue_, class BlockScheduler_>
class BasicMatmul;
```

它从 `BlockMmad` 里**反向推导**出一堆公共类型（[basic_matmul.hpp:26-37](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp#L26-L37)），如 `ElementA`、`LayoutA`、`L1TileShape` 等——这就是「组装范式」的好处：上层不用再重复声明这些类型，直接从底层砖块继承。

Kernel 的两个关键结构体：

- **`Arguments`**（[basic_matmul.hpp:69-74](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp#L69-L74)）：**用户 API**，只装最朴素的信息——`problemShape` 和三个 GM 指针 `ptrA/ptrB/ptrC`。注意它**不装 layout**。

- **`Params`**（[basic_matmul.hpp:40-67](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp#L40-L67)）：**Kernel API**，除了指针还带三个 `layoutA/layoutB/layoutC`——这是设备侧算偏移要用的。

两者靠 **`ToUnderlyingArguments`** 桥接（[basic_matmul.hpp:86-93](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp#L86-L93)）：它根据 `problemShape` 现场构造三个布局，把 `Arguments` 补全成 `Params`：

```cpp
static Params ToUnderlyingArguments(const Arguments& args, uint8_t* workspace) {
    LayoutA layoutA = LayoutA::template MakeLayout<ElementA>(args.problemShape.m(), args.problemShape.k());
    LayoutB layoutB = LayoutB::template MakeLayout<ElementB>(args.problemShape.k(), args.problemShape.n());
    LayoutC layoutC = LayoutC::template MakeLayout<ElementC>(args.problemShape.m(), args.problemShape.n());
    Params params{args.problemShape, args.ptrA, layoutA, args.ptrB, layoutB, args.ptrC, layoutC};
    return params;
}
```

最后看 SPMD 主循环（[basic_matmul.hpp:104-141](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp#L104-L141)），这是 u1-l2「SPMD 步长循环」的真实落地：

```cpp
template <> CATLASS_DEVICE void operator()<AscendC::AIC>(Params const& params) {
    BlockScheduler matmulBlockScheduler(params.problemShape, MakeCoord(L1TileShape::M, L1TileShape::N));
    uint32_t coreLoops = matmulBlockScheduler.GetCoreLoops();   // 总任务数
    ...
    for (uint32_t loopIdx = AscendC::GetBlockIdx(); loopIdx < coreLoops; loopIdx += AscendC::GetBlockNum()) {
        GemmCoord blockCoord = matmulBlockScheduler.GetBlockCoord(loopIdx);   // loopIdx -> (m,n) 块号
        ...
        int64_t gmOffsetA = params.layoutA.GetOffset(offsetA);                 // 块号 -> GM 偏移
        ...
        blockMmad(gmA[gmOffsetA], params.layoutA, gmB[gmOffsetB], ..., actualBlockShape); // 进 4.1 的主循环
    }
}
```

注意几个承接点：

- `operator()<AIC>` 是**给 Cube 核（AICore）用的特化**；下面还有一个空的 `operator()<AIV>`（[basic_matmul.hpp:143-145](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp#L143-L145)），对应 u1-l2 讲过的「Cube 算乘累加、Vector 做后处理」分工——本例没有后处理，所以 `AIV` 是空的。
- `GetCoreLoops()` 返回 `loopsMN.row() * loopsMN.column()`（[block_swizzle.hpp:67-71](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_swizzle.hpp#L67-L71)），即 C 矩阵在 `L1TileShape` 粒度下被切成多少块。
- `GetBlockCoord` 把线性任务号换算成 (m, n) 块号（[block_swizzle.hpp:80-114](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_swizzle.hpp#L80-L114)），并按 `SwizzleDirection` 做 Z 字形重排。

#### 4.2.4 代码实践

**实践目标**：把「SPMD 分核」与「`Arguments`/`Params` 转换」两条链路看清楚。

**操作步骤**：

1. 在 [basic_matmul.hpp:121](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp#L121) 的 `for` 循环里，标注三件事：起始值 `GetBlockIdx()`、终止值 `coreLoops`、步长 `GetBlockNum()`——这正是 u1-l2 的 SPMD 步长循环。
2. 追踪一个 `loopIdx`（假设 `loopIdx=0`）：调用 `GetBlockCoord(0)` 得到 `blockCoord`，再算 `offsetC = {blockCoord.m()*128, blockCoord.n()*256}`，最后 `gmOffsetC = layoutC.GetOffset(offsetC)`。手算当 `loopIdx=0` 时 `blockCoord` 与 `gmOffsetC` 的值。
3. 对比 `Arguments`（[basic_matmul.hpp:69-74](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp#L69-L74)）和 `Params`（[basic_matmul.hpp:40-67](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp#L40-L67)），列出 `Params` 比 `Arguments` 多出的字段。

**需要观察的现象**：

- 当核数 `GetBlockNum()` 大于 `coreLoops` 时，多余的核心不会进入循环（天然空转），这就是负载是否均衡的判断依据。
- `Params` 多出三个 layout，而 layout 完全由 `problemShape` + 元素类型决定，因此可在 Host 侧提前算好。

**预期结果**：能讲清「`loopIdx` → `blockCoord` → `gmOffsetC`」三步换算；能说出 `Params` 比 `Arguments` 多了 `layoutA/layoutB/layoutC`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `operator()<AIC>` 里要用 `params.layoutA.GetOffset(offsetA)` 而不是直接 `offsetA.m() * K + offsetA.k()`？

> **答案**：因为 A 的排布可能是 `RowMajor`，也可能是 `ColumnMajor` 或 NZ 分形（见 u3-l1）。`GetOffset` 把「逻辑坐标」映射成「物理字节偏移」，屏蔽了排布差异；硬编码 `m*K+k` 只对 `RowMajor` 正确，换布局就错。

**练习 2**：本例 `BlockEpilogue = void`，那么 `C = beta*C + alpha*A*B` 里的 `beta*C` 在哪里做？

> **答案**：本例 `beta=0`（纯 `C = A*B`，C 是只写输出），所以不需要 `beta*C`，故 `BlockEpilogue` 用 `void`。如果要做 `beta*C`、bias 或激活，就需要在第四步传入一个真实的 `BlockEpilogue<...>` 类型（U6 专题）。

### 4.3 DeviceGemm 包装：Host 侧适配器屏蔽设备差异

#### 4.3.1 概念说明

Kernel（`BasicMatmul`）是**设备侧**代码，用 `AscendC::` 接口、跑在 NPU 上。但 Host 侧（x86 CPU）不能直接 `new BasicMatmul()` 去跑——它需要：

- 校验参数能否被该 Kernel 接受（`CanImplement`）；
- 算出需要多大 workspace（`GetWorkspaceSize`）；
- 把 `Arguments` 转成设备侧能用的 `Params`（`Initialize`）；
- 用类似 CUDA 启动核的语法 `<<<blockDim>>>` 把 Kernel 派发到 NPU（`operator()`）。

`DeviceGemm` 就是这层「Host 侧适配器」。它对 Host 暴露统一的 4 个接口，对内把 Kernel 的静态能力转发出来——**上层逻辑共享、底层差异屏蔽**。

#### 4.3.2 核心流程

第五步 + 执行：

```
第五步  MatmulAdapter = DeviceGemm<MatmulKernel>

运行期：
  Arguments args{problemShape, deviceA, deviceB, deviceC};
  MatmulAdapter matmulOp;
  matmulOp.CanImplement(args);                         // 校验
  sizeWorkspace = matmulOp.GetWorkspaceSize(args);     // 算 workspace
  matmulOp.Initialize(args, deviceWorkspace);          // 内部调 ToUnderlyingArguments
  matmulOp(stream, aicCoreNum);                        // 启动 kernel <<<aicCoreNum>>>
```

#### 4.3.3 源码精读

`DeviceGemm` 整体很薄，定义在 [device_gemm.hpp:21-98](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/device/device_gemm.hpp#L21-L98)，核心是把请求转发给 Kernel。逐接口看：

- **类型透传**（[device_gemm.hpp:24-28](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/device/device_gemm.hpp#L24-L28)）：

  ```cpp
  using Kernel = GemmKernel;
  using Arguments = typename GemmKernel::Arguments;   // 直接复用 Kernel 的 Arguments
  using Params = typename GemmKernel::Params;
  ```

  即 `MatmulAdapter::Arguments` 就是 `BasicMatmul::Arguments`——所以 Host 侧才写得出 `MatmulKernel::Arguments arguments{...}`（[basic_matmul.cpp:106](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L106)）。

- **`CanImplement`**（[device_gemm.hpp:47-54](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/device/device_gemm.hpp#L47-L54)）：转发 `GemmKernel::CanImplement(args)`，返回 `Status::kSuccess` 或 `kInvalid`。本例 `BasicMatmul::CanImplement` 恒返回 `true`（[basic_matmul.hpp:76-79](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp#L76-L79)），更复杂的 Kernel 会在这里校验对齐、尺寸等约束。

- **`GetWorkspaceSize`**（[device_gemm.hpp:57-62](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/device/device_gemm.hpp#L57-L62)）：转发 `GemmKernel::GetWorkspaceSize`。本例返回 0（[basic_matmul.hpp:81-84](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp#L81-L84)），所以 Host 侧 `sizeWorkspace == 0`、不分配 workspace（[basic_matmul.cpp:110-113](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L110-L113)）。SplitK 等场景会返回非 0。

- **`Initialize`**（[device_gemm.hpp:65-70](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/device/device_gemm.hpp#L65-L70)）：调 `GemmKernel::ToUnderlyingArguments(args, workspace)` 把 `Arguments` 转成 `Params`，存进成员 `params_`。这正是 4.2 讲的那次「补全 layout」转换发生的地方。

- **`operator()(stream, blockDim)`**（[device_gemm.hpp:89-92](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/device/device_gemm.hpp#L89-L92)）→ `Run`（[device_gemm.hpp:74-86](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/device/device_gemm.hpp#L74-L86)）：真正的设备侧启动。注意它按 `CATLASS_ARCH` 分了两条路（2201/3510），但都调用 `Catlass::KernelAdapter<GemmKernel><<<blockDim, nullptr, stream>>>(params_)`——这个 `<<<blockDim>>>` 就是把 `blockDim`（=Host 传进来的 `aicCoreNum`，[basic_matmul.cpp:115](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L115)）作为派发到 NPU 的核数，与 Kernel 里 `GetBlockNum()` 的取值一致。

由此整条链路闭环：**Host 组装类型 → `Arguments` → `Initialize` 转 `Params` → `<<<aicCoreNum>>>` 派发 → 设备侧 `operator()<AIC>` 的 SPMD 循环 → `blockMmad(...)` → Tile/Basic 的 `mmad` 指令**。

#### 4.3.4 代码实践

**实践目标**：把 Host 执行阶段（u2-l1 的阶段④）与 DeviceGemm 的四个接口对应起来。

**操作步骤**：

1. 读 [basic_matmul.cpp:106-115](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L106-L115)，把每一行对应到 `device_gemm.hpp` 里的某个接口（`CanImplement` / `GetWorkspaceSize` / `Initialize` / `operator()`）。
2. 追踪 `aicCoreNum` 的来源：[basic_matmul.cpp:84](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L84) 通过 `PlatformAscendCManager::GetInstance()->GetCoreNumAic()` 取硬件 Cube 核数，第 115 行作为 `blockDim` 传给 `matmulOp(stream, aicCoreNum)`，最终成为 `<<<aicCoreNum>>>` 与设备侧 `GetBlockNum()` 的值。
3. 说明当 `GetWorkspaceSize` 返回非 0 时 Host 应该怎么处理（参考 [basic_matmul.cpp:110-119](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L110-L119) 的 `aclrtMalloc` + 用完 `aclrtFree` 模式）。

**需要观察的现象**：

- 本例 `sizeWorkspace == 0`，所以 workspace 分配被 `if` 跳过；但代码仍保留完整的「申请—使用—释放」骨架，说明这是通用模式。
- `Initialize` 是把 Host 信息「下沉」到 `Params` 的唯一时机；之后 `operator()` 只传 `stream`/`blockDim`，不再传 `Arguments`。

**预期结果**：能画出「Host 调用 → DeviceGemm 接口 → Kernel 静态方法 / KernelAdapter 启动」的调用时序；能解释 workspace 的生命周期。

#### 4.3.5 小练习与答案

**练习 1**：`DeviceGemm` 为什么要把 `CanImplement` / `GetWorkspaceSize` 声明成 `static`，而 `Initialize` / `operator()` 不是？

> **答案**：`CanImplement` / `GetWorkspaceSize` 只依赖 `args`、不需要任何实例状态，所以做成 `static`，可不经实例化直接调用（也便于 Host 在分配 workspace 前先用 `GetWorkspaceSize` 询大小）。`Initialize` 要把结果写进成员 `params_`、`operator()` 要读 `params_` 来启动 kernel，依赖实例状态，故为非静态成员（见 [device_gemm.hpp:32-34](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/device/device_gemm.hpp#L32-L34)）。

**练习 2**：把 `<<<aicCoreNum>>>` 改成 `<<<1>>>`（单核），程序结果会变吗？性能呢？

> **答案**：结果不变（只要 `coreLoops > 0`，单核也能在步长循环里串行算完所有 C 块），但性能会大幅下降——因为所有 `coreLoops` 个任务全压到一个核上，丢掉了多核并行。这也反向印证了 SPMD 模型：核数只决定并行度，不改变正确性。

## 5. 综合实践

**任务**：在不改变正确性的前提下，给 `00_basic_matmul` 换一组「配置」跑通，验证你对五步组装的理解。

要求完成以下三步并记录结果：

1. **写全 using 链条**：对照 [basic_matmul.cpp:86-105](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L86-L105)，在笔记里画出从 `ElementA/B/C`、`LayoutA/B/C` → `GemmType` → `BlockMmad` → `BasicMatmul` → `DeviceGemm` 的完整依赖图，并在每条边上标注「该参数来自哪一行 / 哪个常量」。

2. **改 TileShape 观察编译产物**：把 `L1TileShape` 从 `<128, 256, 256>` 改成 `<128, 128, 128>`，`L0TileShape` 相应改成 `<128, 128, 64>`，重新用 `scripts/build.sh` 编译。观察：
   - 是否仍能编译通过、运行后是否 `Compare success`（**待本地验证**）；
   - 在 SPMD 主循环里，`coreLoops` 会变大（因为 C 被切得更细），思考这对多核负载均衡的影响。

3. **追踪一次完整派发**：从 `matmulOp(stream, aicCoreNum)`（[basic_matmul.cpp:115](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L115)）出发，沿 `operator()` → `Run` → `KernelAdapter<<<aicCoreNum>>>` → `BasicMatmul::operator()<AIC>` → `blockMmad(...)` 走一遍，确认你已经能把五层抽象里的 Device / Kernel / Block 三层「串」起来（Tile / Basic 留待 U5）。

**预期产出**：一张 using 依赖图 + 一份「改 TileShape 前后 coreLoops 变化」的说明 + 一条能讲清楚的派发时序。

## 6. 本讲小结

- **五步组装**是 CATLASS 的核心范式：`BlockMmad`（计算砖块）→ `BlockEpilogue`（后处理，可 `void`）→ `BlockScheduler`（分核 + Swizzle）→ `BasicMatmul`（Kernel 组合）→ `DeviceGemm`（Host 适配器）。组装顺序自底向上。
- `BlockMmad` 的 6 个参数（`DispatchPolicy`/`L1TileShape`/`L0TileShape`/`AType`/`BType`/`CType`）各自独立，换需求只换对应参数；`DispatchPolicy::ArchTag` 还会自动驱动底层 `TileCopy`/`TileMmad` 的挑选。
- `GemmType` 把「元素类型 + 布局」绑成可传递类型，`GemmShape` 是纯编译期 `(M,N,K)` 常量——两者是组装的基本零件。
- `BasicMatmul` 提供 `Arguments`（用户 API，仅 problemShape + 指针）与 `Params`（Kernel API，含 layout），靠 `ToUnderlyingArguments` 桥接；设备侧 `operator()<AIC>` 用 SPMD 步长循环把 C 块分给各核，`AIV` 特化负责后处理（本例为空）。
- `DeviceGemm` 是薄适配器，四个接口（`CanImplement`/`GetWorkspaceSize`/`Initialize`/`operator()`）转发 Kernel 能力，`operator()` 最终以 `<<<aicCoreNum>>>` 把 Kernel 派发到 NPU。
- 整条链路闭环：**Host 类型组装 → `Arguments` → `Initialize` 转 `Params` → `<<<aicCoreNum>>>` 派发 → SPMD 循环 → `blockMmad` → Tile/Basic 的 `mmad`**。本讲覆盖了 Device/Kernel/Block 三层，Tile/Basic 留给 U5。

## 7. 下一步学习建议

- **U3（数据模型与类型系统）**：本讲把 `Layout`、`GemmType`、`GemmShape`、`ArchTag` 当作零件用了，U3 会拆开它们的内部（`RowMajor` 怎么算偏移、`GemmShape` 的硬件约束、`Arch` 容量常量）。
- **U4（Block 层与主循环）**：本讲只点了 `BlockMmad` 的「组装」与 `DispatchPolicy` 的标签机制，U4 会进入 `block_mmad_pingpong.hpp` 的主循环内部，讲清 pingpong 双缓冲、`DispatchPolicy` 的四种变体与 Swizzle 细节。
- **u2-l3（DeviceGemm 深入）**：如果先想看清 Device 层四个接口的 Host 侧时序与 workspace 全流程，可读紧邻的 u2-l3。
- 阅读建议：对照 `docs/zh/3_API/gemm_api.md` 的「CATLASS Gemm 组件」表格，把本讲的类名逐一对到 Device/Kernel/Block/Tile/Basic 五层，巩固「目录即分层」的地图。
