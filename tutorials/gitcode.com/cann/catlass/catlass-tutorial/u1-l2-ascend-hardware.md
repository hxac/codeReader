# 昇腾硬件与算子编程模型

## 1. 本讲目标

本讲承接 [u1-l1](./u1-l1-project-overview.md) 建立的「五层抽象」认知，往下走一层：**先认识算子跑在什么样的硬件上**。

读完本讲，你应当能够：

- 画出昇腾 AtlasA2 芯片上一颗 AI Core 内部的**存储层级**（GM → L1 → L0A/L0B → L0C → UB），并说清每一层放什么、怎么搬。
- 区分 **AICore（Cube 矩阵核）** 与 **AIVector（Vector 向量核）**，知道 GEMM 的乘累加在哪、后处理激活在哪。
- 列举核内并行的 **8 条 PIPE 流水线**（MTE1/MTE2/MTE3/V/M/FIX/S/ALL），理解它们为什么会互相等待、需要同步。
- 理解 **SPMD 多核编程模型**：为什么一段 kernel 代码能同时驱动几十个核，`GetBlockIdx()`/`GetBlockNum()` 在分核循环里起什么作用。

这些概念是后续所有源码拆解（Tile 搬运、Pingpong 流水、BlockScheduler 分核）的物理基础。看不懂硬件，就看不懂 CATLASS 为什么要把数据在 L1/L0/UB 之间来回搬运。

## 2. 前置知识

在进入正文前，先用大白话对齐几个概念：

- **NPU（神经网络处理单元）**：专门做 AI 计算的芯片，昇腾 AtlasA2 就是华为的一类 NPU。和 CPU 不同，它里面有大量并行的计算核。
- **AI Core**：NPU 里的「计算小队」。一颗 AtlasA2 芯片上有几十颗 AI Core，每颗 Core 内部又分 **AICore（擅长矩阵乘，叫 Cube）** 和 **AIVector（擅长逐元素/向量运算，叫 Vector）** 两类子核。
- **片上存储 vs 片外存储**：芯片内部的存储（L1、L0、UB）容量小但速度极快；芯片外接的显存（GM/HBM）容量大但慢。算子优化的核心矛盾就是「怎么把数据尽量留在快的存储里、减少访问慢的 GM」。
- **流水线（Pipeline）**：把一条指令的执行拆成多个阶段，让不同阶段并行起来。昇腾核内有 8 条独立流水，搬数据的、算数据的、写结果的各自一条，能并行就并行，互相有依赖时才同步。
- **SPMD（Single Program, Multiple Data）**：所有核跑**同一份代码**，但靠各自的「工号」`GetBlockIdx()` 处理不同的数据块。下文会详讲。

> 类比：把一颗 AI Core 想象成一个厨房，GM 是远处的中央仓库（大但远），L1/L0/UB 是厨房里从大到小的操作台（越靠灶台越小越快）。AICore 是炒锅（做矩阵乘），AIVector 是料理机（做逐元素操作）。CATLASS 的工作，就是安排好「食材怎么从仓库一层层搬到灶台、做完再搬出去」的流程。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [include/catlass/arch/arch.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/arch/arch.hpp) | 架构抽象层：用 `constexpr` 常量记录 AtlasA2/Ascend950 各存储层的**字节数容量**，并用 `PositionXxx` 标签给每一层存储起一个可传递的类型名字。 |
| [docs/zh/2_Design/01_kernel_design/00_basics/atlasA2_hardware_info.md](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/2_Design/01_kernel_design/00_basics/atlasA2_hardware_info.md) | AtlasA2 硬件基础信息：内存单元容量表、逻辑位置↔物理位置↔搬入指令↔支持格式的对应表。 |
| [docs/zh/2_Design/01_kernel_design/00_basics/atlasA2_gemm_instruction_set.md](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/2_Design/01_kernel_design/00_basics/atlasA2_gemm_instruction_set.md) | GEMM 类硬件指令集：`DataCopy`/`LoadData`/`Mmad`/`Fixpipe` 的使用方式、对齐要求，以及核内 8 条 PIPE、核间同步说明。 |
| [include/catlass/catlass.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/catlass.hpp) | 全局基础常量（对齐块、`STRIDE_LIMIT`）以及 `CATLASS_ARCH` 宏如何按架构（2201/3510）切换。 |
| [include/catlass/gemm/kernel/basic_matmul.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp) | Kernel 层示例：`operator()<AIC>` 里那段经典的 SPMD 分核 `for` 循环，是本讲「编程模型」最真实的代码佐证。 |

---

## 4. 核心概念与源码讲解

### 4.1 存储层级与数据通路

#### 4.1.1 概念说明

一颗 AtlasA2 AI Core 内部，从「最远最大」到「最近最小」，数据要经过一连串存储层。理解每一层的**容量、用途、访问它的指令**，是看懂 CATLASS 搬运代码的前提。

整体层级（从外到内）：

```
GM (HBM, 全核共享)            ← 所有矩阵 A/B/C 的家，容量大、慢
   │  DataCopy (MTE2)
   ▼
L1 Buffer (每核 512KB)         ← 暂存反复使用的 tile 块
   │  LoadData (MTE1)
   ▼
L0A / L0B (各 64KB)            ← Cube 核的输入 A / B
   │  Mmad (M, Cube 矩阵乘累加)
   ▼
L0C (128KB, AtlasA2)           ← Cube 核的输出（累加结果）
   │  Fixpipe (FIX)
   ▼
GM                             ← 结果写回（GEMM 主路径）

GM ──DataCopy(MTE2)──▶ UB (192KB) ──Vector(V)──▶ UB ──DataCopy(MTE3)──▶ GM
                              ↑ AIVector 后处理主战场（激活、量化、cast）
```

几点关键认知：

- **GEMM 的 Cube 主路径**走 GM→L1→L0A/L0B→L0C→GM；**AIVector 的后处理路径**走 GM→UB→GM。两条路径分别由不同子核（AIC / AIV）和不同 PIPE 负责。
- **容量越小越快**：L0A/L0B 只有 64KB，所以一次只能搬进去一小块 `m_0×k_0`；L1 有 512KB，能放稍大的 `m_1×k_1`；GM 是 HBM，所有核共享，慢但装得下整个矩阵。
- **L0C 用 fp32 累加**：即便输入是 fp16，Cube 在 L0C 上也按 fp32 累加以保精度，所以 L0C 的容量约束按 4 字节算（见 4.1.4）。

#### 4.1.2 核心流程

把 GEMM 中**一个 tile 块**的生命周期串起来：

1. **GM → L1**：用 `DataCopy`（流水 PIPE_MTE2），可随路把 ND 排布转成 NZ/nZ 分形排布。
2. **L1 → L0A / L0B**：用 `LoadData` 系列（流水 PIPE_MTE1），按需做小分形转置。
3. **L0A + L0B → L0C**：用 `Mmad`（流水 PIPE_M，即 Cube），完成矩阵乘累加。
4. **L0C → GM**：用 `Fixpipe`（流水 PIPE_FIX），把 fp32 结果转回 fp16（或其它输出类型）写出。
5. 若有后处理：`L0C → UB`（或 `GM → UB`），在 AIVector 上做激活/量化，再 `UB → GM`（流水 PIPE_MTE3）。

搬运在不同层之间用的**指令和排布格式都不一样**，这正是 CATLASS `TileCopy` 组件要封装的复杂度（后续 u5 会专门讲）。

#### 4.1.3 源码精读

**① 容量常量——一切 TileShape 约束的源头**

CATLASS 把硬件容量用 `constexpr` 常量固化在 [include/catlass/arch/arch.hpp:18-26](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/arch/arch.hpp#L18-L26)：

```cpp
struct AtlasA2 {
    static constexpr uint32_t BIAS_SIZE  = 1024;
    static constexpr uint32_t FIXBUF_SIZE = 7 * 1024;   // FB：存放量化/relu 参数，7KB
    static constexpr uint32_t UB_SIZE    = 192 * 1024;  // Unified Buffer，192KB
    static constexpr uint32_t L1_SIZE    = 512 * 1024;  // L1，512KB
    static constexpr uint32_t L0A_SIZE   = 64 * 1024;   // Cube 输入 A，64KB
    static constexpr uint32_t L0B_SIZE   = 64 * 1024;   // Cube 输入 B，64KB
    static constexpr uint32_t L0C_SIZE   = 128 * 1024;  // Cube 输出，128KB
};
```

这段代码就是「AtlasA2 存储容量」的**唯一可信来源**：单位是字节（Byte）。所有 TileShape 能不能选某个值，最终都要拿这些常量去算。`Ascend950`（下一代架构）定义在同文件 [arch.hpp:29-37](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/arch/arch.hpp#L29-L37)，主要差异是 L0C 升到 256KB、UB 升到 248KB——容量变了，Tile 的选择范围也会变（迁移相关在 u10 讲）。

**② Position 标签——给每一层存储起个类型名字**

光有容量常量还不够。CATLASS 在代码里要能**用类型表达「这个 tensor 在哪一层」**，于是有了 [arch.hpp:42-48](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/arch/arch.hpp#L42-L48)：

```cpp
using PositionGM   = PositionType<AscendC::TPosition::GM>;        // GM
using PositionL1   = PositionType<AscendC::TPosition::A1>;        // L1
using PositionL0A  = PositionType<AscendC::TPosition::A2>;        // L0A
using PositionL0B  = PositionType<AscendC::TPosition::B2>;        // L0B
using PositionL0C  = PositionType<AscendC::TPosition::CO1>;       // L0C
using PositionUB   = PositionType<AscendC::TPosition::VECCALC>;   // UB
```

这里有个**昇腾命名容易绕的点**：硬件逻辑位置 `A1/B1/C1` 对应物理 L1，`A2/B2` 对应 L0A/L0B，`CO1` 对应 L0C，`VECCALC` 对应 UB。CATLASS 用 `PositionL1/PositionL0A/...` 给它们起了更直观的别名，后续 Tile 组件模板参数里的「这块数据在哪一层」就是靠这些标签区分的（u3-l3 会展开）。

**③ 存储与搬运指令的官方对应表**

文档 [atlasA2_hardware_info.md:24-36](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/2_Design/01_kernel_design/00_basics/atlasA2_hardware_info.md#L24-L36) 把「逻辑位置 → 物理位置 → 搬入指令 → 支持格式」列成一张表，摘录关键几行：

| 逻辑位置 | 物理位置 | 搬入指令 | 支持格式 |
| --- | --- | --- | --- |
| GM | GM | DataCopy | RowMajor/ColumnMajor/nZ/zN |
| A1/B1 | L1 | DataCopy | zN/nZ/ND(m=1) |
| A2 | L0A | LoadData | zZ |
| B2 | L0B | LoadData | nZ |
| CO1 | L0C | —（Mmad 产出） | zN |
| tbufVECIN/OUT/CALC | UB | DataCopy | ND/NZ |

这张表回答了一个高频疑问：**为什么 GM→L1 用 `DataCopy`，而 L1→L0A/L0B 要换成 `LoadData`？** 因为不同层支持的排布格式和搬入指令不同，CATLASS 的 `TileCopy` 组件正是按「源层+目的层+数据类型+排布」做特化路由（u5 讲）。

#### 4.1.4 代码实践

> **实践目标**：亲手算一遍「一个 fp16 的 L1Tile 是否放得下 L1」，建立「TileShape 不是随便选的、要受容量约束」的直觉。

**操作步骤**：

1. 打开 [arch.hpp:18-26](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/arch/arch.hpp#L18-L26)，记下 `L1_SIZE = 512*1024 = 524288` 字节。
2. 打开 [matmul_summary.md 的 Common 模板约束](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/2_Design/01_kernel_design/04_matmul_summary.md#L172-L179)，找到 L1 的容量约束式。

   L1 容量约束（fp16，2 字节）：

   \[
   m_1 k_1 \cdot \text{L1Stage}_A + n_1 k_1 \cdot \text{L1Stage}_B \;\le\; \text{L1Size} / 2\,\text{Byte}
   \]

   其中 \(m_1, n_1, k_1\) 是搬入 L1 的 TileShape，`L1Stage_A/L1Stage_B` 是 A/B 在 L1 上的缓冲份数（单缓冲=1，pingpong=2）。

3. 给定 L1TileShape 为 \(m_1=128,\; n_1=256,\; k_1=256\)，元素类型 fp16（2 字节），分别算 A 和 B 的占用：

   - A tile \(= m_1 \times k_1 = 128 \times 256\)，占用 \(128 \times 256 \times 2 = 65536\) 字节 \(=64\text{KB}\)。
   - B tile \(= n_1 \times k_1 = 256 \times 256\)，占用 \(256 \times 256 \times 2 = 131072\) 字节 \(=128\text{KB}\)。

**需要观察/计算的现象与结果**：

- **单缓冲**（`L1Stage_A = L1Stage_B = 1`）：总占用 \(64 + 128 = 192\text{KB} \le 512\text{KB}\) ✓ 放得下，还剩 320KB。
- **Pingpong 双缓冲**（`L1Stage_A = L1Stage_B = 2`）：总占用 \(64\times2 + 128\times2 = 384\text{KB} \le 512\text{KB}\) ✓ 也放得下，但只剩 128KB 余量。

**结论**：在 AtlasA2 上，`128×256×256` 的 fp16 L1Tile 无论单缓冲还是 pingpong 双缓冲都能放下，是一个**合法**的 TileShape 选择。（文档 [atlasA2_hardware_info.md:17](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/2_Design/01_kernel_design/00_basics/atlasA2_hardware_info.md#L17) 还提到 L1 通常切成 4 个 128KB，L1A pingpong + L1B pingpong，B 的 pingpong（256KB）正好把 L1B 区域填满，比较紧。）

> 注：这里全是纯算术，可直接手算确认，无需在 NPU 上运行。

#### 4.1.5 小练习与答案

**练习 1**：如果把上面 L1Tile 的元素类型从 fp16 换成 int8（1 字节），单缓冲下 A+B 共占多少 KB？是否还放得下？

**答案**：int8 下 A\(=128×256×1=32\text{KB}\)，B\(=256×256×1=64\text{KB}\)，共 96KB，远小于 512KB，放得下（且 int8 时 L0A/L0B 的约束反而更关键，因为小分形形状不同，见 u5）。

**练习 2**：为什么 L0C 的容量约束要除以 4 字节，而不是像 L0A/L0B 那样除以 2 字节？

**答案**：因为即便输入是 fp16，Cube 核在 L0C 上也按 **fp32（4 字节）累加**以避免精度损失，所以 L0C 占用按 4 字节算（见 [matmul_summary.md:177](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/2_Design/01_kernel_design/04_matmul_summary.md#L177) 的 `L0CSize / 4Byte`）。

---

### 4.2 AICore/AIVector 与流水线

#### 4.2.1 概念说明

一颗 AtlasA2 AI Core 内部其实有**两类子核**：

- **AICore（Cube）**：专门做矩阵乘累加（`Mmad`），输入来自 L0A/L0B，结果写 L0C。GEMM 的「主力计算」在这里。
- **AIVector（Vector）**：专门做逐元素/向量运算（加 bias、激活、cast、量化反量化等），数据在 UB 上来回。后处理（Epilogue）很多动作在这里。

为什么分成两类核？因为矩阵乘和逐元素运算的硬件最优实现完全不同：Cube 是脉动阵列式的乘加流水，Vector 是宽 SIMD 式的向量单元。把它们分开，各自跑在最擅长的硬件上。

而为了让 Cube 和 Vector「同时干活」，核内设计了 **8 条并行的 PIPE 流水线**。它们各自独立推进，只有在读写同一块存储、存在数据依赖时，才需要同步。

#### 4.2.2 核心流程

核内 8 条 PIPE 及职责（来自 [atlasA2_gemm_instruction_set.md:513-523](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/2_Design/01_kernel_design/00_basics/atlasA2_gemm_instruction_set.md#L513-L523)）：

| 流水类型 | 含义 |
| --- | --- |
| `PIPE_MTE2` | 从 GM 出发的搬运：GM→L1、GM→L0A、GM→L0B、GM→UB |
| `PIPE_MTE1` | 从 L1 出发的搬运：L1→L0A、L1→L0B、L1→BT |
| `PIPE_M`    | Cube 计算流水（执行 Mmad） |
| `PIPE_FIX`  | Fixpipe 相关：L0C→GM、L0C→L1、L1→FP buffer |
| `PIPE_MTE3` | 回 GM 的搬运：UB→GM、L1→GM |
| `PIPE_V`    | Vector 计算流水（UB 上的逐元素运算） |
| `PIPE_S`    | 标量流水（含 Tensor GetValue） |
| `PIPE_ALL`  | 所有流水（用于全局屏障） |

一次 GEMM + 后处理里，典型的依赖链：

```
PIPE_MTE2 (GM→L1)  →  依赖  →  PIPE_MTE1 (L1→L0)  →  依赖  →  PIPE_M (Mmad)
                                                                      │
PIPE_MTE3 (UB→GM)  ←  依赖  ←  PIPE_V (Vector后处理)  ←  依赖  ←  PIPE_FIX (L0C→UB/GM)
```

- **依赖必须同步**：例如 `PIPE_MTE1` 要从 L1 读数据，必须等 `PIPE_MTE2` 把数据从 GM 写进 L1，否则读到的是旧数据。CATLASS 用 `SetFlag/WaitFlag`（事件同步）或 `PipeBarrier` 来管这些依赖。
- **不同 PIPE 之间能并行**：当 `PIPE_M` 在算第 N 块时，`PIPE_MTE2` 可以同时搬第 N+1 块——这正是 Pingpong/Multi Buffer 流水优化的物理基础（u4 讲）。

#### 4.2.3 源码精读

文档对核内同步的动机说得很直白（[atlasA2_gemm_instruction_set.md:507](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/2_Design/01_kernel_design/00_basics/atlasA2_gemm_instruction_set.md#L507)）：

> AIC 核/AIV 核内部的执行单元（如 MTE2 搬运单元、Vector 计算单元等）以**异步并行**的方式运行，在读写同一存储资源时可能存在数据依赖关系。为确保数据一致性及计算正确性，需通过**同步控制**协调操作时序。

核间同步（`SyncAll`、`CrossCoreSetFlag`/`CrossCoreWaitFlag`）则用于 **AIC+AIV 混合（Mix）算子**，让 AIC 和 AIV 互相等待——例如 Cube 算完通知 Vector 做后处理（[atlasA2_gemm_instruction_set.md:526-541](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/2_Design/01_kernel_design/00_basics/atlasA2_gemm_instruction_set.md#L526-L541)）。

回到 CATLASS 代码，`Mmad` 指令最终在 Tile 层通过 `AscendC::Mmad` 调用（u5-l1 会精读 [tile_mmad.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/tile/tile_mmad.hpp)）。本讲只要知道：`Mmad` 跑在 `PIPE_M`，它的输入必须由 `PIPE_MTE1` 提前就位、输出要等 `PIPE_FIX` 写出，三类流水靠同步串起来。

#### 4.2.4 代码实践

> **实践目标**：源码阅读型——在 GEMM 主路径里给每一步标上对应的 PIPE，把「抽象流水」和「真实指令」对上号。

**操作步骤**：

1. 打开 [atlasA2_gemm_instruction_set.md:513-523](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/2_Design/01_kernel_design/00_basics/atlasA2_gemm_instruction_set.md#L513-L523) 的 PIPE 表。
2. 在一张纸上画一条 GEMM tile 处理链，为每一步标注指令与所属 PIPE：

   | 步骤 | 指令 | 所属 PIPE | 子核 |
   | --- | --- | --- | --- |
   | GM→L1 | `DataCopy` | `PIPE_MTE2` | AIC |
   | L1→L0A/L0B | `LoadData` | `PIPE_MTE1` | AIC |
   | 矩阵乘累加 | `Mmad` | `PIPE_M` | AICore(Cube) |
   | L0C→GM | `Fixpipe` | `PIPE_FIX` | AIC |
   | （后处理）GM→UB→GM | `DataCopy`+Vector | `PIPE_MTE2`/`PIPE_V`/`PIPE_MTE3` | AIVector |

**需要观察的现象**：注意 `Mmad` 之前必须有 `PIPE_MTE1` 把 L0A/L0B 填好；`Fixpipe` 之前必须有 `Mmad` 把 L0C 写好——这就是后文 Pingpong 流水要插入同步点的地方。

**预期结果**：你得到一张「GEMM tile 全流程 × 指令 × PIPE × 子核」对照表，这正是读懂 [block_mmad_pingpong.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/block/block_mmad_pingpong.hpp)（u4-l1）的钥匙。

#### 4.2.5 小练习与答案

**练习 1**：一个带 ReLU 激活后处理的 GEMM，Cube 算完之后，激活这个动作大概在哪条 PIPE、哪个子核上发生？

**答案**：激活（逐元素）跑在 `PIPE_V`（Vector 流水），由 **AIVector** 子核在 **UB** 上完成。所以结果要先从 L0C（经 Fixpipe）弄到 UB，Vector 处理后再写回 GM。

**练习 2**：为什么说「`PIPE_MTE2` 搬第 N+1 块、`PIPE_M` 算第 N 块」能并行就是 Pingpong 的收益来源？

**答案**：因为这两条流水操作的是**不同的 buffer 副本**（ping 和 pong），不读写同一存储，没有数据依赖，所以可以真正并行推进，把搬运空泡用计算填满，详见 u4-l3 的流水图。

---

### 4.3 SPMD 多核编程模型

#### 4.3.1 概念说明

AtlasA2 芯片上有**几十颗 AI Core**，GEMM 这种大计算必须靠多核并行才能跑得快。昇腾采用 **SPMD（Single Program, Multiple Data）** 编程模型：

- 所有核执行**同一份 kernel 代码**（Single Program）。
- 但每个核通过自己的**「工号」**`AscendC::GetBlockIdx()` 知道自己是第几号核，从而处理**不同的数据块**（Multiple Data）。
- 核总数由 `AscendC::GetBlockNum()` 给出。

> 类比：一个包工头（Host）把一面墙（C 矩阵）切成很多砖（基本块），同时把同一份《砌墙手册》（kernel）发给 40 个工人（核）。每个工人看自己的工牌号（`GetBlockIdx`），按手册算出自己该砌第 0、40、80… 号砖，干完下班。手册只有一份，活儿各干各的。

理解 SPMD 的关键：**你写的 kernel 代码只描述「一个核怎么处理一个基本块」，分核的任务划分（哪个核干哪些块）藏在那段经典的 stride 循环里**。

#### 4.3.2 核心流程

标准的 SPMD 分核循环模式（伪代码）：

```
coreLoops = 把 C 矩阵切出的基本块总数
for (loopIdx = GetBlockIdx();  loopIdx < coreLoops;  loopIdx += GetBlockNum()) {
    blockCoord = 由 loopIdx 还原出 (m块号, n块号)      // 我负责哪个块
    blockMmad(... 处理这个块 ...)                       // 计算+搬运这个块
}
```

- 第 0 号核处理 loopIdx = 0, GetBlockNum(), 2×GetBlockNum(), …
- 第 1 号核处理 loopIdx = 1, 1+GetBlockNum(), …
- 依此类推，所有核合起来正好覆盖全部 `coreLoops` 个基本块。

这里还藏着两个 CATLASS 概念（后续 u2-l4/u4-l4 展开）：

- **`coreLoops` 怎么来**：由 `BlockScheduler` 根据 C 矩阵形状和 TileShape 算出基本块总数。
- **`loopIdx` 怎么映射到 (m,n) 块号**：由 `BlockScheduler.GetBlockCoord(loopIdx)` 完成，背后还可能套一层 **Swizzle**（调整遍历顺序，u4-l4 讲）。

#### 4.3.3 源码精读

这段循环真实存在于 CATLASS 最基础的 Kernel [basic_matmul.hpp:121-138](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp#L121-L138)，是 SPMD 模型最直接的代码佐证：

```cpp
BlockScheduler matmulBlockScheduler(params.problemShape,
                                    MakeCoord(L1TileShape::M, L1TileShape::N));
uint32_t coreLoops = matmulBlockScheduler.GetCoreLoops();   // 基本块总数

for (uint32_t loopIdx = AscendC::GetBlockIdx();             // 从自己的工号开始
     loopIdx < coreLoops;
     loopIdx += AscendC::GetBlockNum()) {                    // 步长=核总数
    GemmCoord blockCoord = matmulBlockScheduler.GetBlockCoord(loopIdx);      // 我负责的块号
    GemmCoord actualBlockShape = matmulBlockScheduler.GetActualBlockShape(blockCoord);
    MatrixCoord offsetA{blockCoord.m() * L1TileShape::M, blockCoord.k() * L1TileShape::K};
    MatrixCoord offsetB{blockCoord.k() * L1TileShape::K, blockCoord.n() * L1TileShape::N};
    MatrixCoord offsetC{blockCoord.m() * L1TileShape::M, blockCoord.n() * L1TileShape::N};
    int64_t gmOffsetA = params.layoutA.GetOffset(offsetA);                   // 算出 GM 偏移
    // ...
    blockMmad(gmA[gmOffsetA], params.layoutA, gmB[gmOffsetB], params.layoutB,
              gmC[gmOffsetC], params.layoutC, actualBlockShape);              // 处理这个块
}
AscendC::PipeBarrier<PIPE_ALL>();   // 收尾全局屏障
```

逐句对应 SPMD 概念：

- `GetBlockIdx()`/`GetBlockNum()`：**SPMD 的灵魂**——同一个 `for`，每个核因为工号不同而走上不同的迭代序列。
- `GetCoreLoops()`：基本块总数，决定了总工作量。
- `GetBlockCoord(loopIdx)`：把一维的 `loopIdx` 还原成二维块号 `(m, n)`。
- `blockCoord.m() * L1TileShape::M`：由块号算出在 GM 上的行列偏移，再经 `layoutA.GetOffset` 映射成字节地址，最后 `gmA[gmOffsetA]` 定位到这块 A 数据。
- 末尾 `PipeBarrier<PIPE_ALL>()`：保证本核所有流水（MTE/M/FIX…）都干完再退出。

另外注意 Kernel 用模板特化区分子核入口（[basic_matmul.hpp:104-145](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp#L104-L145)）：`operator()<AscendC::AIC>` 装着上面的 SPMD 计算，而 `operator()<AscendC::AIV>` 是空的——`00_basic_matmul` 这种纯 Cube 算子把所有活儿都放在 AIC 上，AIV 什么都不做。带后处理的算子才会在 AIV 里干活。

#### 4.3.4 代码实践

> **实践目标**：源码阅读型——亲手走一遍「一个核的一个 loopIdx」是如何变成 GM 地址并触发计算的，把 SPMD 循环的每一段都标注出来。

**操作步骤**：

1. 打开 [basic_matmul.hpp:121-138](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp#L121-L138)。
2. 假设有 `GetBlockNum() = 40` 个核，`L1TileShape = (128, 256, ...)`，问题形状 `C: M=512, N=512`：
   - 基本块总数 `coreLoops = (512/128) × (512/256) = 4 × 2 = 8` 个。
   - 第 `GetBlockIdx()=2` 号核会处理 `loopIdx = 2` 这一块（因为 8 < 40，每个核最多处理一块）。
3. 在代码旁标注：哪一行算 `blockCoord`、哪一行算 `offsetA`、哪一行算 `gmOffsetA`、哪一行真正调用 `blockMmad`。

**需要观察的现象**：

- 注意循环条件 `loopIdx < coreLoops`：当 `coreLoops`(8) < `GetBlockNum()`(40) 时，只有 0~7 号核有活干，其余核一进循环就退出（这正是 u8-l4 要讲的「Small 场景负载不均、需要 Small/SplitK 模板」的起因）。
- 注意 `actualBlockShape`：当 C 的边长不是 TileShape 整数倍时，边缘块形状要被 `GetActualBlockShape` 裁小。

**预期结果**：你能在 `for` 循环里清晰地指出——`GetBlockIdx` 决定起点、`GetBlockNum` 决定步长、`GetBlockCoord` 把序号变坐标、`GetOffset` 把坐标变 GM 字节地址、`blockMmad(...)` 才是真正干活。**待本地验证**：若能在 NPU 上跑 `./00_basic_matmul 512 512 1024 0` 并加打印 `GetBlockIdx()`，会看到不同核打印出不同的 `loopIdx`（真机操作）。

#### 4.3.5 小练习与答案

**练习 1**：问题形状 `M=1024, N=1024`，`L1TileShape::M=128, L1TileShape::N=256`，AIC 核数 `GetBlockNum()=20`。`coreLoops` 是多少？每个核平均处理几块？是否有核空闲？

**答案**：`coreLoops = (1024/128) × (1024/256) = 8 × 4 = 32` 块。20 个核分 32 块，平均每核 32/20 ≈ 1.6 块：前 12 号核各处理 2 块（loopIdx = i, i+20），后 8 号核各处理 1 块（loopIdx = i+20 已超出？实际 0~11 号核处理两块 0..31，12~19 号核处理一块）。无核完全空闲，但负载略不均。

**练习 2**：如果把 `for` 循环的步长 `GetBlockNum()` 误写成 `1`，会发生什么？

**答案**：每个核都会从自己的 `GetBlockIdx()` 开始、步长 1 地遍历**几乎所有**基本块，导致 40 个核重复计算同一批块，结果可能因重复累加出错、性能也会崩溃。步长必须是 `GetBlockNum()` 才能保证块之间互不重叠地被各核认领。

**练习 3**：为什么 `00_basic_matmul` 的 `operator()<AscendC::AIV>` 是空函数体？

**答案**：因为 `00_basic_matmul` 是**纯 Cube 算子**，矩阵乘全部在 AIC 上完成、没有 AIVector 后处理，所以 AIV 入口无事可做（[basic_matmul.hpp:143-145](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp#L143-L145)）。带激活/量化的算子才会填这个 AIV 分支。

---

## 5. 综合实践

**任务：跟踪「一块 C 基本块」从 GM 出发、历经各存储层、最终写回 GM 的完整旅程，并标注每一步的硬件要素。**

把本讲三个模块串起来，完成下面这张「全旅程表」（参考答案见后，建议先自己填）：

| 阶段 | 数据动作 | 使用的指令 | 所属 PIPE | 子核 | 涉及存储层 | 容量约束（fp16） |
| --- | --- | --- | --- | --- | --- | --- |
| 0. 分核 | 由 SPMD 循环决定哪个核处理本块 | `GetBlockIdx/GetBlockNum` | —（标量） | AIC | GM（寻址） | — |
| 1. 搬入 A | GM→L1 | ? | ? | ? | GM→L1 | \(m_1k_1\cdot\)Stage ≤ L1/2B |
| 2. 搬入 B | GM→L1 | ? | ? | ? | GM→L1 | \(n_1k_1\cdot\)Stage ≤ L1/2B |
| 3. 进 Cube 输入 | L1→L0A/L0B | ? | ? | AIC | L1→L0A/L0B | L0A/L0B 各 64KB |
| 4. 乘累加 | L0A·L0B→L0C | ? | ? | ? | L0C | L0C 按 4B 算 |
| 5. 写回 | L0C→GM | ? | ? | AIC | L0C→GM | — |

**参考答案**（关键填空）：

- 阶段 1/2 指令 `DataCopy`，PIPE `PIPE_MTE2`，子核 AIC。
- 阶段 3 指令 `LoadData`，PIPE `PIPE_MTE1`，子核 AIC。
- 阶段 4 指令 `Mmad`，PIPE `PIPE_M`，子核 **AICore(Cube)**。
- 阶段 5 指令 `Fixpipe`，PIPE `PIPE_FIX`，子核 AIC。

完成这张表后，再回到 [basic_matmul.hpp:121-138](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp#L121-L138) 的 SPMD 循环：你会看到 Kernel 只负责「阶段 0 分核 + 调用 `blockMmad`」，而阶段 1~5 全部封装在 `blockMmad`（即 Block 层）里——这正是 u1-l1 所说「Kernel 管多核编排、Block 管单核主循环」的物理体现。

> 拓展（可选）：若把后处理 ReLU 也纳入旅程，则阶段 5 之后还要加 `L0C→UB`（Fixpipe）、`UB` 上 Vector 计算 ReLU（PIPE_V，AIVector）、`UB→GM`（PIPE_MTE3）。这会用到核间同步（AIC 通知 AIV），对应 [atlasA2_gemm_instruction_set.md:526-541](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/docs/zh/2_Design/01_kernel_design/00_basics/atlasA2_gemm_instruction_set.md#L526-L541)。

## 6. 本讲小结

- 昇腾 AtlasA2 一颗 AI Core 内有 **GM → L1 → L0A/L0B → L0C → UB** 的存储层级，越内层容量越小、速度越快；GEMM 走 Cube 主路径，后处理走 AIVector 的 UB 路径。
- 各层容量固化在 [arch.hpp](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/arch/arch.hpp)：L1=512KB、L0A/L0B=64KB、L0C=128KB（AtlasA2）、UB=192KB；TileShape 必须满足对应的容量约束，且 L0C 按 fp32(4B) 算。
- 搬运用不同指令：GM→L1 用 `DataCopy`，L1→L0 用 `LoadData`，乘累加用 `Mmad`，L0C→GM 用 `Fixpipe`；每类对应一条 PIPE。
- AI Core 分 **AICore(Cube)** 与 **AIVector(Vector)** 两类子核；核内 8 条 PIPE（MTE2/MTE1/M/MTE3/V/FIX/S/ALL）异步并行，读写同一存储时靠 `SetFlag/WaitFlag`、`PipeBarrier` 同步。
- 昇腾采用 **SPMD** 编程模型：所有核跑同一份 kernel，靠 `GetBlockIdx()`/`GetBlockNum()` 的 stride 循环认领不同的 C 基本块，真实代码见 [basic_matmul.hpp:121-138](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/gemm/kernel/basic_matmul.hpp#L121-L138)。
- 本讲建立的「存储层 + 流水 + 分核」三件套，是后续读懂 Tile 搬运（u5）、Pingpong 流水（u4）、BlockScheduler 分核（u2-l4/u4-l4）的物理基础。

## 7. 下一步学习建议

- **紧接着**去学 [u1-l3 目录结构](./u1-l3-directory-structure.md) 与 [u1-l4 编译运行](./u1-l4-build-and-run.md)，亲手把 `00_basic_matmul` 跑起来，在真机上感受本讲的「Compare success」。
- **想深入容量常量与 Position 标签**：进阶讲 [u3-l3 硬件架构抽象层 Arch 与 Position](./u3-l3-arch-position.md)，会展开 `CATLASS_ARCH` 宏（见 [catlass.hpp:38-42](https://github.com/gitcode.com/cann/catlass/blob/53a42be2b5850cca67db196a9a50c11d8e87b4a0/include/catlass/catlass.hpp#L38-L42)）如何驱动 AtlasA2/Ascend950 特化。
- **想看搬运指令怎么被封装**：跳到 u5《Tile 层与硬件指令》，看 `TileCopy` 如何按「源层+目的层+类型+排布」把 `DataCopy`/`LoadData`/`Fixpipe` 路由到不同实现。
- **想看流水如何并行优化**：u4《Block 层与主循环》的 Pingpong/Multi Buffer/Preload，正是把本讲的「不同 PIPE 并行」用到极致的工程实践。
