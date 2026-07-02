# CUTLASS 项目总览与定位

## 1. 本讲目标

本讲是 CUTLASS 学习手册的第一篇。读完本讲，你应该能够：

- 用一句话说清楚 **CUTLASS 是什么、解决什么问题**，以及它为什么是「库」而不是「可执行程序」。
- 理解 **GEMM 层次化分解（hierarchical decomposition）** 的核心思想，明白 CUTLASS 为什么要把一次大矩阵乘法拆成多层。
- 区分 **CUTLASS 三代 API**：2.x（经典 `device::Gemm`）、3.x（基于 CuTe 的 `GemmUniversal`/`CollectiveBuilder`）、4.x（Python 的 CuTe DSL）。
- 说出 CUTLASS 支持的 **GPU 架构（compute capability）与数据类型**，并知道如何用 `CUTLASS_NVCC_ARCHS` 指定目标架构。

本讲不要求你懂 CUDA 的任何高级知识，但需要你大致知道「矩阵乘法 \(C = A \times B\)」是什么。

---

## 2. 前置知识

在进入源码之前，先建立几个最基础的概念。

### 2.1 什么是 GEMM

GEMM 是 **GEneral Matrix Multiply（通用矩阵乘法）** 的缩写。给定矩阵 \(A\)（\(M \times K\)）和矩阵 \(B\)（\(K \times N\)），计算：

\[
C_{M \times N} = \alpha \cdot A_{M \times K} \times B_{K \times N} + \beta \cdot C_{M \times N}
\]

其中 \(\alpha\)、\(\beta\) 是标量。深度学习、科学计算里大量的计算（全连接层、注意力、线性方程组求解等）最终都可以归约成 GEMM。所以「把 GEMM 写到极致快」几乎等价于「把一大类计算写到极致快」。

### 2.2 什么是 header-only 库

普通 C++ 库会编译成 `.so` / `.dll` 文件，你链接它来使用。而 **header-only（仅头文件）库** 把所有代码都写在 `.h` / `.hpp` 文件里，你只要在编译时把它的目录加入「头文件搜索路径」，`#include` 进来就能用，不需要单独「编译这个库」。CUTLASS 就是一个 header-only 库，这点直接决定了它的构建方式（见后续讲义 u1-l2）。

### 2.3 什么是 compute capability

NVIDIA 给每代 GPU 一个「计算能力（compute capability）」版本号，例如 `8.0` 对应 A100，`9.0` 对应 H100，`10.0` 对应 B200。版本号越高，支持的硬件指令越多。CUTLASS 需要知道你为哪一代 GPU 编译，才能选择正确的 Tensor Core 指令。

### 2.4 CUTLASS 与 cuBLAS 的关系（直觉版）

- **cuBLAS** 是 NVIDIA 随 CUDA Toolkit 一起发布的、**闭源**的高性能线性代数库，你只能调用、不能看实现。
- **CUTLASS** 是 NVIDIA 开源（BSD 协议）的 **C++ 模板库**，它把 cuBLAS 背后那些「怎么把 GEMM 写得很快」的技巧以可读、可组合、可定制的方式暴露出来。

一句话：cuBLAS 是「成品菜」，CUTLASS 是「菜谱 + 半成品食材」。在 CUTLASS 的 profiler 输出里，你经常能看到 CUTLASS 实现的结果与 cuBLAS 结果并排对比（详见后文 4.4 节）。

---

## 3. 本讲源码地图

本讲只读三个文件，它们都位于仓库根目录与 `include/` 下：

| 文件 | 作用 |
| --- | --- |
| `README.md` | 项目的「门面」，说明 CUTLASS 是什么、版本、支持架构/数据类型、构建方式、目录结构。本讲 80% 的结论来自这里。 |
| `CHANGELOG.md` | 版本变更记录，从顶部 `CUTLASS 4.x` 标题可看出当前处于第几代。 |
| `include/cutlass/cutlass.h` | 整个 `cutlass` 命名空间的「总入口」头文件，定义了 `cutlass::Status` 状态码、warp 相关常量等最基础的词汇类型。 |

> 说明：本讲不深入任何算法实现，只是让你「站在高处俯瞰项目」。真正的源码精读从第二讲（u1-l2）开始。

---

## 4. 核心概念与源码讲解

### 4.1 CUTLASS 是什么与适用场景

#### 4.1.1 概念说明

CUTLASS（**CU**da **T**emplates for **L**inear Algebra **S**ubroutines and **S**olvers）是 NVIDIA 官方开源的 CUDA C++ 模板库，用来**实现高性能的矩阵乘法（GEMM）及相关计算**。它的定位可以用 README 开头一句话概括：

[README.md:L8-L11](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/README.md#L8-L11) —— 这段明确写道，CUTLASS 是「一组用于在各层级实现高性能 GEMM 的抽象」，并且包含「层次化分解与数据搬运策略」，把「可动的零件」拆成可复用、可组合的软件组件。

简单理解，CUTLASS 的适用场景是：

- 你需要 **比 cuBLAS 更灵活** 的矩阵乘法（自定义 tile 大小、自定义 epilogue、混合精度、特殊布局）。
- 你要 **研究/复现** 高性能 GEMM 的实现原理。
- 你要 **为新的硬件特性**（如 FP8、TMA、warp specialization）写自定义内核，而不愿从零开始。

#### 4.1.2 核心流程

CUTLASS 不是一个程序，所以没有「运行流程」。但作为库，它的「被使用流程」是：

1. 用户在自己的 `.cu` 文件里 `#include "cutlass/..."` 相应头文件。
2. 用模板参数（数据类型、布局、tile 大小）实例化一个 GEMM 类型。
3. 构造参数对象（`args`），传入矩阵指针、尺寸、步长。
4. 调用 `initialize()` → `run()` 启动 CUDA kernel。
5. （可选）用 CUTLASS 自带的 reference 实现（在 `tools/util` 里）对比验证。

其中第 2、3 步的「结果」会被 CUTLASS 内部组装成多个层次的组件（见 4.2 节）。

#### 4.1.3 源码精读

库的总入口头文件 `include/cutlass/cutlass.h` 顶部说明了它的角色：

[include/cutlass/cutlass.h:L32-L38](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/cutlass.h#L32-L38) —— 注释写明这是「CUTLASS 的基础 include」；文件用 `#pragma once` 保护，并引入 `helper_macros.hpp`。这是一个纯头文件，没有 `.cpp`，印证了 header-only 的性质。

该文件里定义了贯穿全库的状态码枚举 `cutlass::Status`，它是几乎所有 CUTLASS 操作的返回类型：

[include/cutlass/cutlass.h:L47-L60](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/cutlass.h#L47-L60) —— 例如 `kSuccess`、`kErrorMisalignedOperand`（操作数未对齐）、`kErrorInvalidDataType`（数据类型不支持）、`kErrorArchMismatch`（在错误的架构上运行）等。记住这几个状态码，后面排错时你会反复见到它们。

同一个文件还定义了 GPU 线程层级相关的基础常量：

[include/cutlass/cutlass.h:L96-L101](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/cutlass.h#L96-L101) —— 例如 `NumThreadsPerWarp = 32`、`NumThreadsPerWarpGroup = 128`。这些数字是理解后面「warp / warp group 划分」的前提（Hopper 内核会用到 warp group）。

> 关于「库不是程序」的另一个佐证：README 明确写道 CUTLASS 是 header-only，无需单独构建即可被其他项目使用——

[README.md:L245-L249](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/README.md#L245-L249)。

#### 4.1.4 代码实践

**实践类型：源码阅读型。**

1. 实践目标：通过阅读，确认 CUTLASS 是「header-only 模板库」而非可执行程序，并认识 `cutlass::Status`。
2. 操作步骤：
   - 打开 [include/cutlass/cutlass.h](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/include/cutlass/cutlass.h)，确认该文件里没有任何 `int main()`。
   - 数一数 `enum class Status` 里共有多少个状态码。
3. 需要观察的现象：整个文件只有声明与 inline 实现，没有定义需要链接的符号。
4. 预期结果：你会看到 12 个状态码（含 `kInvalid`），全文件无 `main` 函数。

#### 4.1.5 小练习与答案

**练习 1**：为什么说 CUTLASS 是「header-only」库？这对使用者意味着什么？

**参考答案**：因为 CUTLASS 的代码全部写在 `.h`/`.hpp` 头文件里，用户只需把 `include/` 加入头文件搜索路径并 `#include` 即可，不必先编译成独立的库文件再链接。好处是无需维护二进制依赖、跨平台方便、模板可充分实例化；代价是编译时间较长。

**练习 2**：`cutlass::Status::kErrorArchMismatch` 通常在什么情况下出现？

**参考答案**：当你编译时用的目标架构（如 `sm_80`）与实际运行的 GPU 架构不匹配，或使用了某架构专属指令（如 Hopper 的 `sm_90a` 指令）却在普通 `sm_90` 目标下运行时，会触发此错误（README 在 Target Architecture 一节也强调了这点）。

---

### 4.2 GEMM 层次化分解概览

#### 4.2.1 概念说明

一次 GEMM 动辄计算上百亿次乘加。如果只用「一个线程算一个元素」的朴素方法，性能会极差。现代 GPU 的 GEMM 实现都采用 **层次化分解（hierarchical decomposition）**：把大矩阵切成小块，让 GPU 的不同硬件层级（线程 / 线程束 warp / 线程块 threadblock / 整个设备 device）各司其职，并利用 Tensor Core 等专用指令。README 开头那句「It incorporates strategies for hierarchical decomposition and data movement」说的就是这个。

CUTLASS 2.x 把这个层次明确组织为四层：

```
device        ← 一次完整的 GEMM（用户直接接触的层）
  └─ threadblock  ← 一个 CTA（线程块）负责的输出子矩阵块
       └─ warp        ← 一个线程束负责的更小子块
            └─ thread / mma 指令  ← 单条硬件乘加指令
```

CUTLASS 3.x 在此基础上重组为「kernel + collective MMA + epilogue」三段式（进阶层讲义会详讲），但「分层」这个核心思想没有变。

#### 4.2.2 核心流程

以「计算 \(C = A \times B\)」为例，层次化分解的执行直觉是：

1. **device 层**：把输出矩阵 \(C\)（\(M \times N\)）切成若干个 tile（例如 \(128 \times 128\)），每个 tile 由一个 threadblock 负责。
2. **threadblock 层**：在自己的 tile 内，沿 \(K\) 维再分段（例如每次处理 32 个 \(K\)），把数据从全局显存（gmem）搬到共享内存（smem）。
3. **warp 层**：threadblock 内的多个 warp 从共享内存读取数据，调用 Tensor Core 指令（mma）完成片段乘加。
4. **thread/mma 指令层**：最终落到硬件指令（如 Ampere 的 `mma.m16n8k16`、Hopper 的 `wgmma`）。

公式上，整体计算是把 \(K\) 维累加拆成多层循环之和：

\[
C_{mn} = \sum_{k=0}^{K-1} A_{mk} B_{kn}
\quad\Longrightarrow\quad
\text{分块累加：}\ C_{tile} = \sum_{k\_step} A_{tile,k\_step}\, B_{k\_step,tile}
\]

CUTLASS 的价值，就是把上面每一层的「数据怎么搬、tile 怎么切、指令怎么调」都抽象成可复用、可替换的组件。

#### 4.2.3 源码精读

README 最顶部的配图标题就点明了主题：

[README.md:L1-L2](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/README.md#L1-L2) —— 图片名为 `gemm-hierarchy-with-epilogue`，标题「Complete CUDA GEMM decomposition」，直观展示了「带 epilogue 的完整 CUDA GEMM 分解」。

README 进一步解释了分层与可调参数的意义：

[README.md:L13-L15](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/README.md#L13-L15) —— 这段说：概念并行层级里的原语可以通过「自定义 tile 大小、数据类型和算法策略」来特化和调优。

而每一层在源码里对应不同目录。看 README 的目录树：

[README.md:L301-L340](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/README.md#L301-L340) —— 例如 `arch/`（指令级 GEMM）、`gemm/`（通用矩阵乘）、`thread/`（单线程级）、以及 `cute/`（3.x 的 Layout/Atom 抽象）。你可以把这张目录树和上面四层分解对应起来：`thread/` ↔ thread 层，`arch/` ↔ 指令层，`gemm/threadblock/`、`gemm/device/` ↔ 更上层。

#### 4.2.4 代码实践

**实践类型：源码阅读型（目录映射）。**

1. 实践目标：把「四层分解」和仓库的物理目录对应起来。
2. 操作步骤：
   - 在本地仓库（或 GitHub）打开 `include/cutlass/` 目录。
   - 分别找到 `thread/`、`gemm/threadblock/`、`gemm/device/`、`arch/` 四个子目录。
   - 画一张表，把 4.2.1 节的四层与这四个目录一一对应。
3. 需要观察的现象：每个目录的文件名（如 `mma.h`、`default_gemm_configuration.h`）能反映其所属层级。
4. 预期结果：你会得到类似 `device 层 → gemm/device/gemm.h`、`threadblock 层 → gemm/threadblock/`、`thread 层 → thread/mma.h`、`指令层 → arch/mma*.h` 的映射。

#### 4.2.5 小练习与答案

**练习 1**：为什么不能直接用「每个线程算一个 \(C\) 元素」的朴素方法？请结合 GPU 存储层级说明。

**参考答案**：朴素方法会导致每个线程频繁访问全局显存（gmem），带宽极高且延迟极大；而层次化分解通过共享内存（smem）重用数据、用 Tensor Core 做高吞吐乘加，把访存次数摊薄到每次乘加上，从而逼近峰值算力。

**练习 2**：CUTLASS 2.x 的「device / threadblock / warp / thread」四层，分别对应 README 目录树里的哪些目录？

**参考答案**：device → `gemm/device/`，threadblock → `gemm/threadblock/`，warp → `gemm/warp/`（与 `gemm/threadblock` 相邻），thread → `thread/`，硬件指令 → `arch/`。

---

### 4.3 三代 API 演进时间线

#### 4.3.1 概念说明

CUTLASS 发展到今天有「三代」并存的 API，初学者最容易在这里迷路。理解它们的关系，是后续选对学习路径的关键：

- **CUTLASS 2.x（经典 API）**：以 `cutlass::gemm::device::Gemm` 为代表，把 GEMM 严格分成 device/threadblock/warp/thread 四层模板。稳定、文档齐全，是入门 GEMM 内部实现的最佳起点。
- **CUTLASS 3.x（CuTe API）**：从 3.0 起引入了全新核心库 **CuTe**（读作 cute），用 `Layout`/`Tensor`/`Atom` 等抽象统一描述「线程与数据的层级化多维布局」。GEMM 被重组为 **kernel + collective MMA + epilogue** 三段式，并提供了 `CollectiveBuilder` 自动组装。性能、可组合性、对新硬件（Hopper/Blackwell）的支持都远超 2.x。
- **CUTLASS 4.x（CuTe DSL）**：在 C++ 之上，新增了 **Python 原生的 CuTe DSL**，让你可以用 Python 写出与 C++ CuTe 概念一致的高性能内核，编译更快、上手更平滑，目前处于公开 beta。

> 注意：版本号「4.x」不代表 2.x/3.x 被废弃，三代 API 在当前仓库里**同时存在**，你可以按需选择。

#### 4.3.2 核心流程

三代 API 的演进时间线（依据 README 与 CHANGELOG 中的明确表述）：

```
2017 起   CUTLASS 作为 CUDA C++ 模板库诞生（2.x 体系的起点）
   │
   │   device::Gemm 经典四层 GEMM
   ▼
3.0       引入 CuTe（Layout/Tensor/Atom），3.x 三段式 GEMM
   │
   │   CollectiveBuilder、Hopper/Blackwell 支持
   ▼
4.0       引入 CuTe DSL（Python 原生 DSL）
   │
   ▼
4.6.0（2026-06）  当前版本（README 与 CHANGELOG 顶部）
```

各代之间不是替换关系，而是「叠加」：3.x 在 2.x 之上增加了 CuTe 与三段式模型；4.x 在 3.x 之上增加了 Python DSL。三代 API 共享同一套对新硬件（如 Tensor Core 指令）的支持。

#### 4.3.3 源码精读

**当前版本号**写在 README 顶部：

[README.md:L4-L6](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/README.md#L4-L6) —— `CUTLASS 4.6.0 - June 2026`。

**「自 2017 年起」的历史定位**：

[README.md:L17-L18](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/README.md#L17-L18) —— 明确说 CUTLASS「自 2017 年起」就在提供高性能线性代数的 C++ 模板抽象，这对应 2.x 体系的起点。

**3.x 引入 CuTe**：

[README.md:L103-L106](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/README.md#L103-L106) —— 「CUTLASS 3.0 introduced a new core library, CuTe」，并说明 CuTe 提供 `Layout` 和 `Tensor` 对象来紧凑地打包数据的类型、形状、内存空间与布局。

**4.x 引入 CuTe DSL**：

[README.md:L28-L31](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/README.md#L28-L31) —— 这段说「To this rich ecosystem of C++ based kernel programming abstractions, CUTLASS 4 adds CUTLASS DSLs」，并指出 CuTe DSL 是第一个发布的 DSL，完全与 CuTe C++ 抽象一致。

**CHANGELOG 顶部**也印证当前处于 4.x 大版本：

[CHANGELOG.md:L1-L5](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/CHANGELOG.md#L1-L5) —— 标题 `# CUTLASS 4.x`，最新条目 `[4.6.0] (2026-06-11)`，下设 `### CuTe DSL` 与 `### CUTLASS C++` 两个子板块，正好对应「Python DSL」与「C++」两条并行的产出路径。

#### 4.3.4 代码实践

**实践类型：源码阅读型（时间线梳理）。**

1. 实践目标：在仓库里找到「三代 API 各自的代表目录/文件」。
2. 操作步骤：
   - 2.x：找到 `include/cutlass/gemm/device/gemm.h`（经典 device 层 GEMM）。
   - 3.x：找到 `include/cutlass/gemm/kernel/gemm_universal.hpp`（三段式 kernel）与 `include/cutlass/gemm/collective/collective_builder.hpp`（自动组装）。
   - 4.x：找到 `python/CuTeDSL/` 目录与 `examples/python/CuTeDSL/`。
3. 需要观察的现象：三代 API 的入口分别位于不同顶层目录（`include/cutlass/gemm/device`、`include/cutlass/gemm/collective`、`python/CuTeDSL`），彼此独立共存。
4. 预期结果：你能列出三代 API 的「入口文件/目录」各一个，确认它们在仓库中同时存在。

#### 4.3.5 小练习与答案

**练习 1**：CuTe 是哪一代引入的？它解决了什么问题？

**参考答案**：CuTe 在 CUTLASS 3.0 引入。它用 `Layout`（= 形状+步长）和 `Tensor`（= 引擎+布局）等统一抽象来描述「数据与线程的层级化多维布局」，替程序员完成繁琐的索引计算，从而让 GEMM 等线性代数内核的设计更可组合、更易读，并更好地支持新硬件。

**练习 2**：CuTe DSL（4.x）与 CuTe C++（3.x）是什么关系？

**参考答案**：CuTe DSL 是 CUTLASS 4 引入的 Python 原生接口，它在概念上「与 CuTe C++ 抽象完全一致」（同样的 layout/tensor/atom 概念），但用 Python 表达，编译更快、学习曲线更平。两者是同一套思想在两种语言上的实现，并不互相替代。

---

### 4.4 支持的架构与数据类型

#### 4.4.1 概念说明

CUTLASS 的「广度」体现在两点：**支持的 GPU 架构很多**，**支持的数据类型很丰富**。这背后是因为 CUTLASS 要让每一代硬件的 Tensor Core 都能被充分榨干。

README 在概述里一口气列出了支持的精度与架构：

[README.md:L18-L26](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/README.md#L18-L26) —— 数据类型包括 FP64、FP32、TF32、FP16、BF16、8 位浮点（e5m2 / e4m3）、block scaled 类型（NVFP4、MXFP4/MXFP6/MXFP8）、窄整数（4/8 位有符号与无符号）、二值 1 位；架构覆盖 Volta、Turing、Ampere、Ada、Hopper、Blackwell。

#### 4.4.2 核心流程

要使用 CUTLASS，关键是「告诉它目标架构」。流程是：

1. 确认你的 GPU 的 compute capability（如 A100=8.0、H100=9.0、B200=10.0）。
2. 在 CMake 配置时用 `CUTLASS_NVCC_ARCHS` 指定目标架构。
3. **重要细节**：CUDA 12.0 引入了「架构加速特性（architecture-accelerated features）」，Hopper/Blackwell 的某些 PTX 指令没有前向兼容保证，因此需要带 `a` 后缀的目标，如 `90a`、`100a`。
4. 如果你在普通 `sm_90` 目标下运行使用了 `sm_90a` 指令（如 Hopper Tensor Core）的内核，会得到运行时错误（对应 `Status::kErrorArchMismatch`）。

#### 4.4.3 源码精读

**最低环境要求**：

[README.md:L127-L131](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/README.md#L127-L131) —— 最低架构 Volta（compute capability 7.0）、编译器需支持 C++17、CUDA Toolkit 至少 11.4；最佳性能推荐 CUDA 12.8。

**硬件支持表**（节选，列出了各 GPU 的 compute capability 与所需最低 Toolkit）：

[README.md:L156-L173](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/README.md#L156-L173) —— 例如 V100=7.0、T4/RTX20x0=7.5、A100=8.0、A10/RTX30x0=8.6、RTX40x0/L40=8.9、H100/H200=9.0、B200=10.0、RTX50x0=12.0 等。注意 README 还特别提醒：Blackwell 数据中心（SM100）与 RTX 50 系列（SM120）是不同的 compute capability，互相不兼容。

**目标架构与 `90a` 后缀的解释**：

[README.md:L175-L201](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/README.md#L175-L201) —— 这段详细解释了「架构加速特性」为何需要 `sm_90a`/`sm100a`，并给出 cmake 示例 `cmake .. -DCUTLASS_NVCC_ARCHS="90a"`。它还强调：要在 Hopper GH100 上发挥最大性能，必须用 `90a` 编译；否则内核会在运行时报错。

**CUTLASS 与 cuBLAS 的关系**也能在 profiler 输出里看到实证：

[README.md:L424-L427](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/README.md#L424-L427) —— `Disposition: Passed`，且同时有 `reference_device: Passed` 与 `cuBLAS: Passed`。这说明 CUTLASS 的 profiler 会把 CUTLASS 实现的结果同时与「CUTLASS 自带的参考实现」和「cuBLAS」对比验证。可见 CUTLASS 的目标就是达到、甚至在定制场景下超过 cuBLAS 的性能，同时把实现完全开源、可定制。

#### 4.4.4 代码实践

**实践类型：源码阅读型 + 本地可选验证。**

1. 实践目标：从 README 的硬件表里整理出 compute capability 清单，并理解 `90a` 的含义。
2. 操作步骤：
   - 打开 [README.md 的 Hardware 表](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/README.md#L156-L173)。
   - 列出至少 6 个 GPU 及其 compute capability。
   - 找到 `90a` 的解释段落，用自己的话写一句「为什么要加 a」。
3. 需要观察的现象：硬件表中 compute capability 跨度从 7.0 到 12.1；`a` 后缀专门用于「架构加速特性」。
4. 预期结果：例如 V100=7.0、T4=7.5、A100=8.0、H100=9.0、B200=10.0、RTX50x0=12.0；`90a` 是因为 Hopper 的部分 PTX 指令无前向兼容保证，必须显式指定才能启用。

> 若本地有 CUDA 环境，可在第 2 步后追加：在 build 目录运行 `cmake .. -DCUTLASS_NVCC_ARCHS=80` 并观察 cmake 输出里出现的目标架构字符串（**待本地验证**，本讲不强制运行）。

#### 4.4.5 小练习与答案

**练习 1**：A100、H100、B200 的 compute capability 分别是多少？哪一个需要带 `a` 后缀？

**参考答案**：A100=8.0，H100=9.0，B200=10.0。H100 在使用其 Tensor Core 等「架构加速特性」时需用 `90a`（而不是 `90`）编译；Blackwell 数据中心卡对应 `100a`。

**练习 2**：CUTLASS 默认会为哪些架构编译内核？想缩短编译时间应该怎么做？

**参考答案**：默认会为 5.0、6.0、6.1、7.0、7.5、8.0、8.6、8.9、9.0 等多个架构编译（见 README「Building CUTLASS」一节）。缩短编译时间的方法是改 `CUTLASS_NVCC_ARCHS`，只指定你真正需要的那一两个架构，例如 `-DCUTLASS_NVCC_ARCHS=80`。

---

## 5. 综合实践

把本讲四个最小模块串起来，完成下面这一份「一页纸 CUTLASS 速览」：

1. **架构清单**：阅读 [README.md 的 Hardware 表](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/README.md#L156-L173)，列出至少 6 个 GPU 及其 compute capability，并标注其中哪些需要带 `a` 后缀。
2. **数据类型清单**：阅读 [README.md:L18-L26](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/README.md#L18-L26)，列出至少 4 种 CUTLASS 支持的数据类型（建议覆盖浮点与整数两大类）。
3. **三代 API 对照**：用一张三列表格写出 2.x / 3.x / 4.x（CuTe DSL）各自的「代表入口」与「核心抽象」（参考 4.3 节）。
4. **写一段话（150 字以内）**：用自己的话说明 **CUTLASS 与 cuBLAS 的关系**，要求结合 [README.md:L424-L427](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/README.md#L424-L427) 的 profiler 输出作为佐证（CUTLASS 结果与 cuBLAS 结果均 Passed）。

> 提交物可以是一份 markdown 文档。本题不涉及编译运行，是纯阅读理解型综合任务，确保你建立了对项目的整体认知，再进入第二讲的动手构建。

---

## 6. 本讲小结

- **CUTLASS 是 NVIDIA 开源的 header-only CUDA C++ 模板库**，用于实现高性能 GEMM 及相关计算，定位是「比 cuBLAS 更灵活、可读、可定制的菜谱」。
- 它的核心思想是 **GEMM 的层次化分解**：device → threadblock → warp → thread/指令，每一层在源码里都有对应目录。
- 仓库里 **三代 API 并存**：2.x（`device::Gemm` 经典四层）、3.x（CuTe 的 kernel/collective/epilogue 三段式）、4.x（Python 的 CuTe DSL）。
- 支持广泛的 **数据类型**（FP64/FP32/TF32/FP16/BF16/FP8/FP4/整数/二值）与 **架构**（Volta→Blackwell，compute capability 7.0~12.1）。
- 目标架构通过 **`CUTLASS_NVCC_ARCHS`** 指定，Hopper/Blackwell 的「架构加速特性」需带 **`a` 后缀**（如 `90a`）。
- `include/cutlass/cutlass.h` 是库的总入口，定义了贯穿全库的 `cutlass::Status` 状态码与线程层级常量。

---

## 7. 下一步学习建议

本讲只建立了「俯瞰图」，还没有动手。建议按下面的顺序继续：

- **u1-l2（构建与运行）**：学会用 CMake 构建 CUTLASS，设置 `CUTLASS_NVCC_ARCHS`，编译并运行 `examples/00_basic_gemm`，跑通你的第一个 CUTLASS GEMM。
- **u1-l3（目录结构与源码组织）**：深入 `include/cutlass` 各子目录的职责，把本讲 4.2 节的「层级 ↔ 目录」对应关系落到具体文件。
- **u1-l4（数值类型与基础容器）**与 **u1-l5（矩阵布局基础）**：学习 `half_t`/`Array`/`TensorRef` 等基础类型，为第 6 讲写第一个 GEMM 做准备。
- 想提前感受 CuTe 的，可以先跳读 [README.md 的 CuTe 一节（L103-L123）](https://github.com/NVIDIA/cutlass/blob/e8ecfad75b44d1ad56264f5001d877e9e47fe080/README.md#L103-L123)，但真正的源码精读要等到第二单元（u2-l1 起）。
