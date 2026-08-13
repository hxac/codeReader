# CATLASS 是什么：定位、分层与设计理念

## 1. 本讲目标

本讲是整本学习手册的第一篇。读完本讲，你应当能够：

- 说出 CATLASS 的全称、定位，以及它要解决的问题（让 GEMM 类算子**白盒化、可复用、可替换、可局部修改**）。
- 画出 CATLASS 的 **Device → Kernel → Block → Tile → Basic** 五层抽象，并说清楚每一层的职责。
- 看懂官方文档里那段「三层嵌套循环」的矩阵乘伪代码，知道五层抽象分别对应伪代码的哪一部分。
- 理解「用模板参数把组件拼装成一个算子」这种组装范式，相比直接手写一个完整算子好在哪里。

本讲不要求你懂昇腾硬件细节，也不要求你能编译运行代码——这些是后续讲义的内容。本讲只解决一个问题：**CATLASS 到底是个什么东西，它为什么这样设计。**

## 2. 前置知识

本讲面向零基础读者，但有几个概念先在脑子里有个印象会更顺畅：

- **GEMM（General Matrix Multiplication，通用矩阵乘）**：即 \( C = A \times B \)，其中 A、B、C 是矩阵。它是 Transformer、大模型推理/训练里占比最大的计算，所以「把矩阵乘写快」是性能优化的核心命题。
- **算子（Operator）**：在昇腾/深度学习语境下，算子就是一段能在硬件上执行的计算函数。矩阵乘、卷积、注意力都是算子。
- **模板（Template）**：C++ 的模板（`template <...>`）能让我们把「数据类型」「分块大小」等写成参数，由编译器生成具体代码。CATLASS 大量使用模板，所以你会看到很多 `using XXX = ...<参数1, 参数2, ...>` 的写法。
- **CANN**：昇腾计算架构（CANN，Compute Architecture for Neural Networks）是华为昇腾 NPU 的软件栈。CATLASS 里的 **CA** 就来自 CANN。
- **NPU / AICore**：NPU 是神经网络专用处理器；AICore 是昇腾 NPU 里负责密集计算的核心。这些会在 [u1-l2](u1-l2-ascend-hardware.md) 详细讲，本讲你只要知道「算子最终跑在这些核心上」即可。

如果你完全没接触过 C++ 模板，建议先把 `template` 和 `using 别名 = 模板类<参数>` 这两种语法大致看懂，否则后续每一篇讲义都会反复出现它们。

## 3. 本讲源码地图

本讲主要读文档与一个最小样例，不深入任何一层的实现。涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/README.md) | 项目门面：定位、软硬件要求、目录结构。本讲用它确认「CATLASS 是什么」。 |
| [docs/zh/2_Design/00_project_overview.md](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/2_Design/00_project_overview.md) | 项目设计总览：解释为什么用「分层模块化设计」，并给出完整目录树。 |
| [docs/zh/3_API/gemm_api.md](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/3_API/gemm_api.md) | GEMM API 文档：定义五层抽象、三层嵌套循环伪代码、组件对照表、五步组装。是本讲最重要的参考。 |
| [examples/00_basic_matmul/basic_matmul.cpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp) | 最简单的矩阵乘样例。本讲用它验证「五步组装」在真实代码里长什么样。 |

记住一句话：**本讲读的是「地图」，不是「地形」。** 每一层内部的源码细节（比如 BlockMmad 主循环、TileMmad 指令）会在后续讲义逐层拆开。

## 4. 核心概念与源码讲解

本讲对应三个最小模块：**项目简介与定位**、**分层模块化设计**、**Gemm 编程模型与组件一览**。

### 4.1 项目简介与定位

#### 4.1.1 概念说明

CATLASS 的全称是 **CANN Templates for Linear Algebra Subroutines**（昇腾算子模板库）。

如果你听过 NVIDIA 的 **CUTLASS**（CUDA Templates for Linear Algebra Subroutines），就能立刻理解 CATLASS 的定位：CUTLASS 把 GPU 上的高性能矩阵乘拆成可拼装的模板组件；CATLASS 做的是**同一件事，只不过目标是昇腾 NPU**。从命名就能看出这种类比关系——**CA**NN 对应 **CU**DA，两者都是「线性代数子程序的模板库」。但请注意：这是从命名与设计理念得出的对应关系，CATLASS 是针对昇腾硬件特点独立设计的，并非 CUTLASS 的移植。

那么它到底要解决什么问题？官方文档把动机说得很直白：Transformer 里矩阵乘（GEMM）计算占比极大，性能优化至关重要；但 GEMM 类算子的实现变种极多，场景一变就要定制，**直接基于硬件手写算子开发难度大、周期长**。CATLASS 的解法是：

> 通过抽象分层的方式将矩阵类算子代码模板化，从而实现算子计算逻辑的**白盒化组装**，让算子代码**可复用、可替换、可局部修改**。

这段话里有四个关键词，构成了 CATLASS 的全部价值主张：

- **白盒化**：算子不是一团黑盒的二进制，而是你能逐层读、逐行改的源码模板。
- **可复用**：同一个搬运组件、同一个微内核，能被矩阵乘、分组矩阵乘、卷积等不同算子反复用。
- **可替换**：想要不同的流水策略？换一个 DispatchPolicy 即可，其余代码不动。
- **可局部修改**：只想改后处理（比如加个激活函数）？只动 Epilogue 那一层，不必重写整个算子。

此外，CATLASS 强调「上层代码逻辑共享的同时，支持底层硬件差异特化」——同一份上层组装代码，底层可以特化到 AtlasA2、Ascend950 等不同硬件。性能方面，README 给出的目标是：在定制 shape 下，性能能达到相应算子标杆性能的 0.98~1.2 倍。

#### 4.1.2 核心流程

从「需求」到「跑通一个 CATLASS 算子」的整体流程可以概括为：

1. **明确计算需求**：我要算的是 \( C = A \times B \)，还是带 bias、带激活、带反量化？
2. **分层组装**：用模板参数把 Block / Kernel / Device 这些组件像搭积木一样拼起来（这就是本讲 4.3 要讲的「五步组装」）。
3. **底层特化**：选择 `Arch::AtlasA2` 或 `Arch::Ascend950`，底层组件自动路由到对应硬件实现。
4. **Host 调用**：用 ACL 运行时分配显存、拷数据、启动算子、对比精度。

本讲你只需要建立第 2 步的直觉，第 3、4 步在后续讲义展开。

#### 4.1.3 源码精读

先看 README 对 CATLASS 的一句话定义：

> CATLASS(**CA**NN **T**emplates for **L**inear **A**lgebra **S**ubroutine**s**)，中文名为昇腾算子模板库，是一个聚焦于提供高性能矩阵乘类算子基础模板的代码库。

对应 [README.md:46](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/README.md#L46)。

紧接着第 48 行给出了核心设计理念，即「白盒化、可复用、可替换、可局部修改」和「底层硬件差异特化」，见 [README.md:48](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/README.md#L48)。性能定位（0.98~1.2 倍标杆）在 [README.md:50](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/README.md#L50)。

再看项目设计文档对「为什么需要它」的完整论证：

> 直接基于硬件能力定制开发 GEMM 类算子面临着开发难度大，开发周期长的问题。为此，昇腾 CANN 推出 CATLASS 算子模板库，采用分层模块化设计，将 GEMM 计算解耦为可灵活组合的数据分块策略和计算单元配置等组件……

这段对应 [docs/zh/2_Design/00_project_overview.md:3](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/2_Design/00_project_overview.md#L3)。

#### 4.1.4 代码实践

**实践目标**：建立「CATLASS = 昇腾版 GEMM 模板库」的第一印象，并亲手验证命名与定位。

**操作步骤**：

1. 打开 [README.md](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/README.md)，定位到第 46 行，把全称里加粗的字母 CA、T、L、A、S 抄下来，确认它们分别对应 CANN、Templates、Linear、Algebra、Subroutines。
2. 打开 [docs/zh/2_Design/00_project_overview.md](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/2_Design/00_project_overview.md)，通读前 3 段，用一句话写下「如果没有 CATLASS，开发者要怎么开发一个 GEMM 算子」。

**需要观察的现象**：你会发现全称的每个首字母都被加粗高亮，这是命名意图的直接证据。

**预期结果**：你能用自己的话回答「CATLASS 解决了什么问题」，并提到「白盒化、可复用、可替换、可局部修改」中的至少两个词。

#### 4.1.5 小练习与答案

**练习 1**：CATLASS 的全称是什么？「CA」代表什么？

> **答案**：CANN Templates for Linear Algebra Subroutines；CA 代表 CANN（昇腾计算架构）。

**练习 2**：为什么说 CATLASS 让算子「可局部修改」？请举一个只改一层就能完成的例子。

> **答案**：因为算子被拆成多层独立组件。例如只想给矩阵乘的输出加一个激活函数（如 ReLU），通常只需在 Epilogue（后处理）层换一个带激活的后处理组件，而不必重写 BlockMmad 主循环或 Kernel 分核逻辑。

**练习 3**：从命名看，CATLASS 与 NVIDIA 的哪个开源库存在设计理念上的对应关系？两者最核心的共同点是什么？

> **答案**：CUTLASS。共同点是：都把高性能矩阵乘（GEMM）拆解成可分层组装、可复用的模板组件；区别是目标硬件（CUDA GPU vs 昇腾 NPU）。

### 4.2 分层模块化设计

#### 4.2.1 概念说明

CATLASS 的核心设计是**分层抽象**：把一次完整的 GEMM 计算，从「Host 怎么调用」一直到「硬件指令怎么发」，切成五层。每一层只关心自己那一段逻辑，并通过模板参数与上下层对接。

这种思路的好处，文档里用了一句很精准的总结（这是「模板方法」设计模式的体现）：

> 算法框架中的特定步骤会延迟到子类实现，使得子类能够在不改变算法整体结构的情况下，灵活重定义其中的某些关键步骤。

翻译成大白话：**主流程（骨架）是固定的，但每一步具体怎么做可以替换。** 这正是「可替换」的实现机制。

五层抽象（由高到低）如下，本讲你只需要记住名字和一句话职责：

| 层级 | 代表类 | 一句话职责 |
| --- | --- | --- |
| **Device** | `Catlass::Gemm::Device::DeviceGemm` | Host 侧入口，屏蔽设备差异，把 Kernel 包成 Host 能调用的形态。 |
| **Kernel** | `Catlass::Gemm::Kernel::BasicMatmul` | 把多个 Block 组合起来，负责分核（Swizzle）、分片、同步。 |
| **Block** | `Catlass::Gemm::Block::BlockMmad`、`Catlass::Epilogue::Block::BlockEpilogue` | 一个逻辑核（Process）内的主循环：搬运 + 计算 + 后处理。 |
| **Tile** | `TileMmad`、`TileCopy` | 可组合的「微内核」，把硬件指令封装成块粒度操作。 |
| **Basic** | `AscendC::Mmad`、`AscendC::DataCopy` | 直接对应硬件指令的最底层 API。 |

> 说明：以上「代表类」来自 [gemm_api.md 的组件对照表](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/3_API/gemm_api.md#L44)。DeviceGemm 定义在 [device_gemm.hpp:22](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/device/device_gemm.hpp#L22)，BasicMatmul 定义在 [basic_matmul.hpp:24](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp#L24)。

#### 4.2.2 核心流程

五层之间的调用关系是**自上而下组装、自下而上执行**：

```
[组装阶段：Host 写代码时，自顶向下拼装]
Device (DeviceGemm)
   └── 包装 Kernel (BasicMatmul)
          └── 组合 Block (BlockMmad + BlockEpilogue)
                 └── 使用 Tile (TileMmad / TileCopy)
                        └── 调用 Basic (AscendC::Mmad / DataCopy)

[执行阶段：NPU 上运行时，Kernel 驱动 Block，Block 驱动 Tile，Tile 发指令]
```

一个关键直觉是：**上层管「编排」，下层管「干活」。** Kernel 管「哪些核算哪块 C、什么时候同步」；Block 管「一个核内怎么搬数据、怎么循环」；Tile 管「搬一块、算一块的具体动作」；Basic 则是真正发给硬件的指令。

这种分层还有一个直接收益——**跨架构复用**。上层 Device/Kernel/Block 的组装逻辑可以共享，只要把最底下的 ArchTag 从 `Arch::AtlasA2` 换成 `Arch::Ascend950`，Tile 层会自动路由到对应硬件的实现。两个架构标签分别定义在 [arch.hpp:18](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/arch/arch.hpp#L18)（AtlasA2）与 [arch.hpp:29](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/arch/arch.hpp#L29)（Ascend950）。

#### 4.2.3 源码精读

项目设计文档用一张图（`api_level.png`）和一个小节专门描述这个分层：

> 算子实现分层模块化设计

对应 [docs/zh/2_Design/00_project_overview.md:9](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/2_Design/00_project_overview.md#L9)。同一文档第 7 行也点出了「模板方法」思想，见 [00_project_overview.md:7](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/2_Design/00_project_overview.md#L7)。

分层在仓库目录上的映射也很清晰，`include/catlass/` 下按职责分了若干子目录：

```
include/catlass/
├── arch        # 硬件架构抽象层（AtlasA2 / Ascend950）
├── gemm        # GEMM 算子模板（含 device/kernel/block/tile）
├── gemv        # GEMV（向量-矩阵乘）算子模板
├── conv        # 卷积算子模板
├── epilogue    # 后处理模板（独立命名空间，可被多种算子复用）
├── layout      # 数据布局定义（RowMajor / ColumnMajor 等）
└── (../tla)    # TLA：新一代 Tile 级抽象框架
```

这段目录结构来自 [docs/zh/2_Design/00_project_overview.md:37-47](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/2_Design/00_project_overview.md#L37)。注意 `epilogue` 不在 `gemm/` 下、也不在 `Catlass::Gemm` 命名空间里——因为后处理（激活、bias、量化等）是通用的，卷积、GEMV 也可能用到。

#### 4.2.4 代码实践

**实践目标**：把「五层抽象」与「仓库目录」对应起来，建立空间导航感。

**操作步骤**：

1. 浏览 [include/catlass/](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass) 目录，找到 `gemm/device/`、`gemm/kernel/`、`gemm/block/`、`gemm/tile/` 这四个子目录。
2. 在每个子目录里随便点开一个 `.hpp`，找到它的 `namespace` 声明（例如 `namespace Catlass::Gemm::Device`），确认命名空间层级与目录层级一致。

**需要观察的现象**：目录路径 `gemm/device/device_gemm.hpp` 与命名空间 `Catlass::Gemm::Device::DeviceGemm` 一一对应；CATLASS 的命名是高度自洽的。

**预期结果**：你能说出「要找 Device 层入口，就去 `gemm/device/`；要找 Tile 层微内核，就去 `gemm/tile/`」。

#### 4.2.5 小练习与答案

**练习 1**：五层抽象从高到低的顺序是什么？

> **答案**：Device → Kernel → Block → Tile → Basic。

**练习 2**：为什么 Epilogue 后处理组件不放在 `Catlass::Gemm` 命名空间下？

> **答案**：因为后处理（如 bias、激活、反量化）是通用的，GEMM、卷积、GEMV 等多种算子都可能用到。把它独立出来才能跨算子复用，这正是「可复用」设计目标的体现。

**练习 3**：若要把一个样例从 AtlasA2 迁移到 Ascend950，从分层角度看，主要影响哪一层？

> **答案**：主要影响 Tile 层与 arch 层——Tile 组件会按 ArchTag 路由到不同硬件的实现，容量常量（L1/L0/UB 等）也来自 arch 层。而上层 Device/Kernel/Block 的组装逻辑通常可以共享（这正是「上层共享、底层特化」）。详细迁移步骤见 [u10-l1](u10-l1-a2-to-950-migration.md)。

### 4.3 Gemm 编程模型与组件一览

#### 4.3.1 概念说明

知道有五层之后，自然要问：**这五层是怎么协同算出一个矩阵乘的？** 答案藏在官方文档里那段著名的「三层嵌套循环」伪代码里。它把矩阵乘 \( C_{M \times N} = A_{M \times K} \times B_{K \times N} \) 拆成了三重循环，并标注了每一重对应哪一层抽象。

核心思想是「分块（Tiling）」：矩阵太大，硬件装不下，于是把 M、N、K 三个维度切成小块，逐块搬运、逐块计算。其中：

- 最外两重（block_m、block_n）循环负责把输出矩阵 C 切成大块，**由多个 AICore 并行处理**——对应 Kernel 层的分核。
- 中间一重（k_tile）循环负责沿 K 维累加，**在一个核（Block）内完成**——对应 Block 层的主循环。
- 最内三重（tile_mma_m/n/k）负责把一块数据真正算出来——对应 Tile 层的微内核，最终落到 Basic 层的 `AscendC::Mmad` 指令。

#### 4.3.2 核心流程

矩阵乘 \( C = A \times B \) 的分块计算量可以简单刻画如下。设外层分块为 BlockTile（M、N 向）和 K 向的 k-tile，则每个输出元素需要的乘加次数为 K，整矩阵的总浮点运算量（FLOPs，按乘加算 2 次）为：

\[
\text{FLOPs} = 2 \cdot M \cdot N \cdot K
\]

而搬运量取决于分块策略（这块会在 [u8-l1](u8-l1-matmul-theory-templates.md) 的理论模板里细讲）。本讲你只要建立一个直觉：**外层切 M/N 决定并行度，中间切 K 决定累加，内层切 tile 决定一次指令算多少。**

伪代码的执行流程（与五层抽象对应）：

```
Kernel 层（BasicMatmul）：在多个 AICore 上并行
  for block_m in [0, M) step BlockTileM:      # 外层循环 1
    for block_n in [0, N) step BlockTileN:    # 外层循环 2（实际用 BlockIdx 区分核，不写成 for）

Block 层（BlockMmad）：单核内的 k-tile 主循环
      for k_tile in [0, K):
        搬运 A、B 的分片 (GM → L1 → L0)
        Tile 层（TileMmad）：最内三重循环
          for tile_mma_m / n / k:
            AscendC::Mmad(c, a, b)            # Basic 层硬件指令
```

注意：外层那两重 `for` 在真实代码里**并不写成显式的嵌套 for**，而是通过 `BlockIdx`（核编号）来区分「我这个核算哪一块」。这是昇腾 SPMD 编程模型的特点，会在 [u2-l4](u2-l4-kernel-basic-matmul.md) 详讲。

#### 4.3.3 源码精读

GEMM API 文档一开篇就给出了五层对应关系：

> CATLASS 的 Gemm API 对应于以下分层，由高到低分别是：Device、Kernel、Block、Tile（MMAD and Copy）、Basic。

对应 [docs/zh/3_API/gemm_api.md:3](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/3_API/gemm_api.md#L3)。

紧接着是三层嵌套循环的伪代码，并在注释里标明了每一重属于哪一层，见 [docs/zh/3_API/gemm_api.md:12-L34](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/3_API/gemm_api.md#L12)。组件与层级的正式对照表（Device→Basic）在 [docs/zh/3_API/gemm_api.md:44-L51](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/3_API/gemm_api.md#L44)。

最关键的是文档给出的**五步组装顺序**（「先组装 Block，再组合成 Kernel，最后用 Device 包装」）。文档里的标准写法是：

```c++
// 第一步：组装 Block 主循环
using BlockMmad = Gemm::Block::BlockMmad<DispatchPolicy, L1TileShape, L0TileShape, AType, BType, CType>;
// 第二步：指定后处理（可选，这里不用）
using BlockEpilogue = void;
// 第三步：指定数据走位（Swizzle）
using BlockScheduler = typename Gemm::Block::GemmIdentityBlockSwizzle<>;
// 第四步：在 Kernel 层组合
using MatmulKernel = Gemm::Kernel::BasicMatmul<BlockMmad, BlockEpilogue, BlockScheduler>;
// 第五步：用 Device 适配器包装
using Matmul = Catlass::Gemm::Device::DeviceGemm<MatmulKernel>;
```

这段摘自 [docs/zh/3_API/gemm_api.md:62-L91](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/3_API/gemm_api.md#L62)。

这套组装在真实样例 [examples/00_basic_matmul/basic_matmul.cpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp) 里几乎一一对应，是验证你有没有看懂的最佳参照：

```c++
using ArchTag = Arch::AtlasA2;                          // 选硬件
using DispatchPolicy = Gemm::MmadAtlasA2Pingpong<true>; // 选流水策略
using L1TileShape = GemmShape<128, 256, 256>;           // 选 L1 分块
using L0TileShape = GemmShape<128, 256, 64>;            // 选 L0 分块

using AType = Gemm::GemmType<ElementA, LayoutA>;        // 数据类型+布局绑定
using BType = Gemm::GemmType<ElementB, LayoutB>;
using CType = Gemm::GemmType<ElementC, LayoutC>;

using BlockMmad = Gemm::Block::BlockMmad<DispatchPolicy, L1TileShape, L0TileShape, AType, BType, CType>;
using BlockEpilogue = void;
using BlockScheduler = typename Gemm::Block::GemmIdentityBlockSwizzle<3, 0>;

using MatmulKernel = Gemm::Kernel::BasicMatmul<BlockMmad, BlockEpilogue, BlockScheduler>;
using MatmulAdapter = Gemm::Device::DeviceGemm<MatmulKernel>;
```

对应 [basic_matmul.cpp:86-L106](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L86)。可以看到：样例代码与文档的「五步」完全吻合，只是把 `BlockScheduler` 的 Swizzle 参数从默认值改成了 `<3, 0>`。**这就是 CATLASS 的组装范式——改需求时只换对应那一步的参数。**

> 提示：上面出现的 `DispatchPolicy`、`GemmType`、`Swizzle`、`TileShape` 等概念，本讲只要求你「知道它出现在组装的哪一步」，它们的内部原理分别属于 [u3（类型系统）](u3-l2-gemmtype-tileshape.md)、[u4（Block 与调度）](u4-l2-dispatch-policy.md)、[u4-l4（Swizzle）](u4-l4-block-scheduler-swizzle.md)。先建立整体印象即可，不要在本讲深究。

#### 4.3.4 代码实践

**实践目标**：亲手把「五步组装」从文档抄到样例，建立「组装 = 拼模板参数」的肌肉记忆。

**操作步骤**：

1. 同时打开两份内容：[gemm_api.md 的五步组装](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/3_API/gemm_api.md#L62) 与 [basic_matmul.cpp:86-L106](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/examples/00_basic_matmul/basic_matmul.cpp#L86)。
2. 逐行对照：文档里的「第一步 BlockMmad」对应样例第几行？「第四步 Kernel」对应第几行？「第五步 Device」对应第几行？
3. 找出样例相对文档「多了」或「改了」的参数（提示：`ArchTag`、`L0TileShape`、`BlockScheduler<3,0>`），猜一猜它们分别影响什么。

**需要观察的现象**：你会确认样例代码就是文档五步组装的实例化；`using ... = ...` 这种「别名拼接」贯穿始终，是 CATLASS 的标志性写法。

**预期结果**：你能不查文档，默写出「BlockMmad → BlockEpilogue → BlockScheduler → BasicMatmul → DeviceGemm」这条组装链，并能指出每一步分别属于五层抽象里的哪一层。

#### 4.3.5 小练习与答案

**练习 1**：在三层嵌套循环伪代码里，「k_tile 主循环」对应五层抽象里的哪一层？

> **答案**：Block 层（`BlockMmad`）。它负责在一个逻辑核内沿 K 维迭代搬运与计算。

**练习 2**：伪代码最内层的 `mmad.call(c, a, b)` 最终落到哪个 Basic 层接口？

> **答案**：`AscendC::Mmad`。Tile 层的 `TileMmad` 是对它的封装。

**练习 3**：按 CATLASS 的组装顺序，下面三步的正确先后是？(a) `DeviceGemm<MatmulKernel>` (b) `BasicMatmul<BlockMmad, ...>` (c) `BlockMmad<DispatchPolicy, ...>`

> **答案**：c → b → a。即先组装 Block，再把 Block 组合成 Kernel，最后用 Device 包装 Kernel。

## 5. 综合实践

把本讲学到的「定位」「五层抽象」「组装范式」串起来，完成下面这个贯穿性小任务。

**任务：画出 CATLASS 五层抽象的职责对应关系图，并标注每层的一个代表类名。**

具体要求：

1. 在纸上或任意画图工具里，画出如下纵向栈（从上到下：Device → Kernel → Block → Tile → Basic）。
2. 在每一层旁边写上：
   - 该层的**一句话职责**（用你自己的话，不要照抄表格）。
   - 该层的**一个代表类名**（例如 Device 层写 `DeviceGemm`、Kernel 层写 `BasicMatmul`、Block 层写 `BlockMmad` 或 `BlockEpilogue`、Tile 层写 `TileMmad` 或 `TileCopy`、Basic 层写 `AscendC::Mmad` 或 `AscendC::DataCopy`）。
3. 在栈的右侧，画出「三层嵌套循环」与五层的对应关系：外层 block_m/block_n 连到 Kernel，中间 k_tile 连到 Block，内层 mmad 连到 Tile/Basic。
4. 最后在图的最上方标出 CATLASS 的全称，并在最下方标出「目标硬件：AtlasA2 / Ascend950」。

完成后，对照 [gemm_api.md:44-L51 的组件表](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/3_API/gemm_api.md#L44) 自查：你写的代表类名是否都在表里？

> 这张图建议保留下来——后续每一篇讲义都在拆这张图里的某一层，它会成为你阅读本手册的「导航总览」。

## 6. 本讲小结

- CATLASS（**C**ANN **T**emplates for **L**inear **A**lgebra **S**ubroutines）是面向昇腾 NPU 的高性能矩阵乘类算子模板库，设计理念类比 NVIDIA CUTLASS，但针对昇腾硬件独立设计。
- 它通过**分层模块化**把 GEMM 解耦成可拼装组件，目标是让算子**白盒化、可复用、可替换、可局部修改**，并在上层共享的同时支持底层硬件差异特化。
- 五层抽象由高到低是 **Device → Kernel → Block → Tile → Basic**，分别对应 Host 入口、多核编排、单核主循环、可组合微内核、硬件指令。
- 矩阵乘被建模为「三层嵌套循环」：外层切 M/N（Kernel 并行）、中间切 K（Block 累加）、内层算 tile（Tile/Basic 指令）。
- 组装范式是固定的「五步」：先 `BlockMmad`，再 `BlockEpilogue`/`BlockScheduler`，组合成 `BasicMatmul`，最后用 `DeviceGemm` 包装——换需求时只换对应那一步的模板参数。
- 仓库目录与命名空间高度自洽：`gemm/{device,kernel,block,tile}` 对应 Device/Kernel/Block/Tile，`epilogue` 独立在外以便跨算子复用。

## 7. 下一步学习建议

本讲只建立了「地图」。接下来建议按以下顺序深入：

1. **先理解硬件**：阅读 [u1-l2 昇腾硬件与算子编程模型](u1-l2-ascend-hardware.md)，搞清楚 GM/L1/L0/UB 的存储层级与 AICore 流水——这是后续理解「为什么要分块、为什么要多缓冲」的物理基础。
2. **再看工程组织**：阅读 [u1-l3 目录结构与工程组织](u1-l3-directory-structure.md)，把仓库导航地图补全。
3. **动手跑样例**：阅读 [u1-l4 环境搭建与编译运行首个样例](u1-l4-build-and-run.md)，亲手编译运行 `00_basic_matmul`，看到 `Compare success`。
4. **然后进入第二单元**：从 [u2-l1](u2-l1-host-acl-runtime.md) 开始逐层拆解 `00_basic_matmul` 这一个算子，从 Host 一路读到 Kernel，把本讲看到的「五步组装」在源码里彻底走通。

如果你只想先记住一件事，那就记住：**CATLASS = 把矩阵乘拆成五层可拼装的模板组件，按五步组装成一个能在昇腾 NPU 上跑的高性能算子。**
