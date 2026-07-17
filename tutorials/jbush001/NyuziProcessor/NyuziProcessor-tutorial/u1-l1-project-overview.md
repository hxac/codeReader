# 项目定位与整体架构

## 1. 本讲目标

本讲是整本学习手册的第一篇，目标只有一个：**让你在进入任何一行具体源码之前，先在心里建起一张 Nyuzi 的全局地图**。

读完本讲，你应当能够：

- 用一句话说清楚 Nyuzi 是什么、为什么要造它；
- 列出 Nyuzi 的五大组成部分（硬件核心、指令集模拟器、LLVM 工具链、软件栈、测试），并指出每一部分在仓库里的位置与职责；
- 读懂默认配置文件，说出「每核几个线程、几条向量通道、各级缓存多大」这些关键数字；
- 理解整个项目是用哪些技术栈搭起来的，以及它们之间是如何协作的。

这一讲不要求你懂 SystemVerilog，也不要求你懂编译原理。我们只读三个文件：`README.md`、`hardware/core/config.svh`、`hardware/core/defines.svh`。

## 2. 前置知识

在开始之前，下面几个通俗概念会帮助你更顺地理解本讲。如果你已经熟悉，可以跳过。

- **处理器（Processor / CPU）**：执行程序的硬件。它从内存里一条条取出指令，做运算，再把结果写回去。
- **GPU / GPGPU**：GPU 原本是专门画图的芯片，特点是「成千上万个简单的计算单元同时干活」。GPGPU 指的是把这种「大规模并行」能力拿来做通用计算（科学计算、图形、AI），而不只是画图。Nyuzi 正是一个面向这种场景的处理器。
- **SIMD（Single Instruction Multiple Data）**：一条指令同时处理多份数据。比如一条「向量加法」指令可以同时算 16 组加法。Nyuzi 的向量通道数就是 16。
- **指令集（ISA, Instruction Set Architecture）**：处理器能听懂的「指令清单」和约定（有哪些寄存器、指令怎么编码）。它是软件和硬件之间的合同。
- **可综合（Synthesizable）**：硬件描述代码可以被工具转换成真实的逻辑电路（FPGA 或 ASIC）。Nyuzi 的硬件用 SystemVerilog 写成，且是可综合的。
- **模拟器（Emulator）**：用软件模拟硬件行为的程序。Nyuzi 提供一个 C 写的指令集模拟器，让你不用 FPGA 也能跑程序。
- **缓存（Cache）**：为了弥补「CPU 很快、内存很慢」的差距，处理器内部放的小容量高速存储。Nyuzi 有 L1（指令/数据各一层）和 L2 两级缓存。

## 3. 本讲源码地图

本讲只涉及三个文件，但它们是理解整个项目的钥匙：

| 文件 | 作用 |
| --- | --- |
| `README.md` | 项目门面。说明 Nyuzi 是什么、怎么装环境、怎么构建和运行。本讲从中提取「项目定位」与「组件构成」。 |
| `hardware/core/config.svh` | 硬件可配置参数表。核数、每核线程数、各级缓存的「路数/组数」、TLB 项数都在这里调。 |
| `hardware/core/defines.svh` | 全局类型与常量定义包（SystemVerilog 的 `package defines`）。寄存器位宽、向量通道数、指令编码、缓存行大小等「派生量」都在这里。 |

一句话概括三者的关系：`README.md` 告诉你「项目长什么样」，`config.svh` 让你「调参数」，`defines.svh` 把这些参数「翻译成硬件里到处复用的类型」。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**项目定位**、**组件构成**、**技术栈**。

### 4.1 项目定位

#### 4.1.1 概念说明

很多处理器项目要么是「教学用的简化 CPU」，要么是「工业级、庞大到难以读透的芯片」。Nyuzi 想占据一个中间地带：它是一个**实验性的 GPGPU 处理器**，目标是让人能拿来「试验微架构和指令集设计的取舍」。

也就是说，Nyuzi 不是一个要和 NVIDIA 竞争的产品，而是一块**可以亲手改、改完能立刻验证**的实验田。正因为如此，它把「硬件设计 + 模拟器 + 编译器 + 软件库 + 测试」整套都打包进了同一个仓库——改一处，整套都能跟上。

README 开头一句话就把这层意思说清楚了：

[NyuziProcessor/README.md:L6-L10](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/README.md#L6-L10)

> 这段话说明：Nyuzi 是面向计算密集任务的实验性 GPGPU 处理器，包含可综合的 SystemVerilog 硬件、指令集模拟器、基于 LLVM 的 C/C++ 编译器、软件库和测试，可用于试验微架构与指令集设计的取舍。

注意三个关键词：

- **experimental（实验性）**：它允许不够「工业级」的简化，例如浮点不完全遵循 IEEE 754（后续讲义会详谈）。
- **GPGPU**：它的并行模型是「多线程 + 向量 SIMD」，而非单线程极速标量。
- **tradeoffs（取舍）**：它的存在意义就是让你比较不同设计选择的影响。

#### 4.1.2 核心流程

理解 Nyuzi 的定位，最好把它看成一个**「设计—验证」闭环**。这个闭环正是项目五大组件互相配合的产物：

```text
   提出一个 ISA / 微架构 改动
              │
              ▼
   ┌──────────────────────┐
   │ 硬件核心 (SystemVerilog) │  ← 改 RTL
   │ 指令集模拟器 (C)         │  ← 改行为模型
   └──────────────────────┘
              │  （两边实现同一套 ISA）
              ▼
   用 LLVM 工具链编译测试程序 → ELF/hex
              │
              ▼
   模拟器 / Verilator 加载运行
              │
              ▼
   测试框架校验：结果对不对？性能计数器怎么说？
              │
              ▼
   观察取舍 → 回到第一步，再提一个改动
```

这个闭环解释了为什么 Nyuzi 必须把硬件、模拟器、编译器、软件、测试都放在一起：少任何一环，这个「改了就能立刻看到效果」的实验循环就断了。

#### 4.1.3 源码精读

我们只精读 README 里和「定位」最相关的两小段。

第一段是项目定义本身，已在上面引用（[README.md:L6-L10](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/README.md#L6-L10)）。它点明了五大组件。

第二段说明：仓库同时提供「模拟器」和「周期精确的硬件仿真器」，让你**没有 FPGA 也能做软硬协同开发**，同时也提供上 FPGA 的脚本与组件：

[NyuziProcessor/README.md:L14-L17](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/README.md#L14-L17)

> 这段话的含义是：环境里包含 emulator（指令集模拟器，快但不周期精确）和 cycle-accurate hardware simulator（基于 Verilator 的周期精确仿真），两者都能在没有 FPGA 的情况下开发硬件与软件；此外还提供上板（FPGA）所需的脚本与组件。

「模拟器」和「周期精确仿真器」是两个不同层次的工具，这是 Nyuzi 一个重要的设计取舍，我们在「组件构成」里会再展开。

#### 4.1.4 代码实践

> **实践目标**：用自己的话把 Nyuzi 的定位写下来，避免只背名词。

**操作步骤**：

1. 打开 [README.md](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/README.md)，只读第 6 到 17 行。
2. 准备一张纸或一个文本文件，回答下面三个问题（不要照抄原文）：
   - Nyuzi 是什么类型的处理器？它主要面向哪类任务？
   - 它为什么说自己是「实验性」的？它存在的意义是什么？
   - 不买 FPGA 能不能开发 Nyuzi 的软硬件？靠的是什么？

**需要观察的现象**：你会发现自己其实不需要看任何代码，只靠 README 开头就能回答。这正说明 Nyuzi 的「定位」是清晰且自洽的。

**预期结果**：你的回答应包含「GPGPU / 计算密集 / 试验微架构与指令集取舍 / 用模拟器和 Verilator 仿真，无需 FPGA」这些要点。

**待本地验证**：本实践为阅读理解型，无需运行命令；若你想顺手验证「无 FPGA 也能跑」，可在完成环境搭建（下一讲）后运行 `make tests` 观察。

#### 4.1.5 小练习与答案

**练习 1**：Nyuzi 说自己是 GPGPU。它和「教学用单核 CPU」最大的区别是什么？

> **参考答案**：GPGPU 强调大规模数据并行——Nyuzi 通过「每核多线程 + 16 通道向量 SIMD」来同时处理大量数据；而教学单核 CPU 通常一次只处理一个数据，重在讲清流水线原理而非并行吞吐。

**练习 2**：README 为什么特意强调「无需 FPGA 也能开发」？

> **参考答案**：因为硬件迭代成本高、上板慢。Nyuzi 提供指令集模拟器和 Verilator 周期精确仿真，让绝大多数软硬改动可以在普通电脑上快速验证，FPGA 只作为最终的真实硬件验证手段。

---

### 4.2 组件构成

#### 4.2.1 概念说明

Nyuzi 不是一个单一代码库，而是**五大组件的集合**。理解组件构成，就是搞清楚「仓库里的每个大目录，到底扮演什么角色」。这五部分是：

1. **硬件核心**：SystemVerilog 写的可综合处理器 RTL，是 Nyuzi 的「真身」。
2. **指令集模拟器**：C 写的行为模型，速度快，用于软件开发与功能验证。
3. **LLVM 工具链**：基于 LLVM 的 C/C++ 编译器，把高层语言编译成 Nyuzi 指令。
4. **软件栈**：运行在 Nyuzi 上的库（libc/libos/librender）、内核、应用、基准测试。
5. **测试**：从单元测试到整机程序、随机协同仿真的多层次验证体系。

这五部分不是平行无关的：硬件和模拟器**实现同一套 ISA**，工具链**产出**二者都能运行的程序，软件栈**运行**在二者之上，测试**校验**二者行为一致。

#### 4.2.2 核心流程

把五大组件按「从源码到验证」的顺序串起来，就是 Nyuzi 的工作流：

```text
  C/C++/汇编 源码 (software/)
        │  ① LLVM 工具链 (tools/NyuziToolchain)
        ▼
   ELF 可执行文件
        │  ② 转成 hex 内存镜像
        ▼
  ┌───────────────┐        ③ 加载到地址 0 启动
  │ 指令集模拟器   │  或  │ Verilator 周期精确仿真 │
  └───────────────┘        └──────────────────────┘
        │  运行时依赖 software/libs (libc/libos/librender)
        ▼
  tests/ 框架比对输出 / 协同仿真锁步比对 → 判断硬件与模拟器是否一致
```

这里有两条运行路径值得记住：

- **模拟器路径**：快，用于日常开发软件、跑应用。
- **Verilator 路径**：慢但周期精确，用于验证硬件 RTL 本身的正确性。
- **协同仿真**：让硬件仿真和模拟器同时跑同一段程序，逐条比对副作用，是发现 RTL bug 的利器（后续专讲）。

#### 4.2.3 源码精读

我们从三个角度印证「五大组件」的构成：根 `CMakeLists.txt` 的子项目划分、`.gitmodules` 引入的外部工具链、以及仓库顶层目录。

**① 四个子项目**。根 CMake 文件用四个 `add_subdirectory` 把仓库拆成四大块：

[NyuziProcessor/CMakeLists.txt:L24-L27](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/CMakeLists.txt#L24-L27)

> 这四行分别构建 `tools`（模拟器等工具）、`software`（软件栈）、`hardware`（RTL 与仿真）、`tests`（测试）。注意工具链本身（LLVM）不在这里编译，而是由安装脚本单独装好，见下。

**② 两个 Git 子模块**。`.gitmodules` 显示仓库引入了两个外部工具：

[NyuziProcessor/.gitmodules:L1-L6](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/.gitmodules#L1-L6)

> `tools/verilator` 是 Verilog 仿真器（用来做周期精确仿真）；`tools/NyuziToolchain` 是基于 LLVM 的 Nyuzi C/C++ 编译器。它们以子模块形式引入，由 `scripts/setup_tools.sh` 下载并安装。

**③ 顶层目录与组件的对应关系**：

| 仓库目录 | 所属组件 | 说明 |
| --- | --- | --- |
| `hardware/core/` | 硬件核心 | 处理器 RTL（本讲读的 `config.svh`、`defines.svh` 就在这里） |
| `hardware/fpga/` | 硬件核心 | FPGA 顶层与外设控制器（DE2-115 板） |
| `hardware/testbench/` | 硬件核心 | 仿真 testbench |
| `tools/emulator/` | 指令集模拟器 | C 写的指令集模拟器 |
| `tools/NyuziToolchain/` | LLVM 工具链 | 编译器（子模块） |
| `tools/verilator/` | 工具 | Verilog 仿真器（子模块） |
| `software/libs/` | 软件栈 | libc / libos / librender 等运行库 |
| `software/kernel/` | 软件栈 | 小型内核（虚拟内存、线程、系统调用） |
| `software/apps/` | 软件栈 | 示例应用（hello_world、sceneview 等） |
| `tests/` | 测试 | 单元 / ISA / 协同仿真 / 整机 / 渲染等多层次测试 |

这张表是后续所有讲义的「导航仪」——后面每次讲某个机制，你都能在这张表里定位它属于哪个组件、在哪个目录。

#### 4.2.4 代码实践

> **实践目标**：把「组件构成」从抽象描述变成你能亲手确认的事实。

**操作步骤**：

1. 在仓库根目录浏览顶层目录（对照上表）。
2. 打开 [CMakeLists.txt:L24-L27](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/CMakeLists.txt#L24-L27)，确认四个 `add_subdirectory`。
3. 打开 [.gitmodules](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/.gitmodules)，确认 `verilator` 与 `NyuziToolchain` 两个子模块。
4. 写一段话（3–5 句）说明 Nyuzi 由哪几部分组成，并指出「编译器」和「模拟器」分别在哪个目录。

**需要观察的现象**：你会发现仓库根目录的一级文件夹，恰好和「五大组件」基本一一对应；唯一「藏起来」的是编译器，它以子模块形式放在 `tools/` 下。

**预期结果**：你的描述应点出——硬件（`hardware/`）、模拟器（`tools/emulator/`）、编译器（`tools/NyuziToolchain/`，子模块）、软件栈（`software/`）、测试（`tests/`）。

**待本地验证**：若想确认子模块已拉取，可执行 `git submodule status`（只读命令），应能看到 verilator 与 NyuziToolchain 两条记录。

#### 4.2.5 小练习与答案

**练习 1**：为什么 LLVM 编译器不在根 `CMakeLists.txt` 的四个 `add_subdirectory` 里？

> **参考答案**：因为编译器是一个独立的大型项目（基于 LLVM），由 `scripts/setup_tools.sh` 单独下载、编译并安装到系统（默认 `/usr/local/llvm-nyuzi/`，见 `CMakeLists.txt` 第 21 行的 `NYUZI_COMPILER_ROOT`）。根 CMake 只通过 `NYUZI_COMPILER_BIN` 引用它，避免每次构建仓库都重编一遍 LLVM。

**练习 2**：指令集模拟器和 Verilator 仿真器都「跑 Nyuzi 程序」，它们分工有何不同？

> **参考答案**：模拟器是 C 写的行为模型，速度快但不周期精确，适合日常开发软件和快速验证功能；Verilator 把 SystemVerilog RTL 编译成 C++ 来周期精确仿真，速度慢但能验证真实硬件逻辑。二者实现同一套 ISA，可用协同仿真互相校验。

---

### 4.3 技术栈

#### 4.3.1 概念说明

「技术栈」回答的是：Nyuzi 用哪些语言和工具搭起来？以及，这些技术是如何被**配置参数**串起来的？

Nyuzi 的技术栈可以分成三层：

- **硬件层**：SystemVerilog（RTL）+ Verilator（仿真）+ Emacs verilog-mode（AUTO 宏展开）。
- **软件层**：C / C++（模拟器、软件库、内核、应用）+ LLVM（编译器）+ 汇编（启动代码 crt0.S 等）。
- **构建与验证层**：CMake / Make（构建）+ Python（测试框架 `tests/test_harness.py`、协同仿真脚本）。

更重要的是，Nyuzi 的硬件是**高度参数化**的：核数、线程数、缓存大小都不是写死的，而是由 `config.svh` 里的一组宏控制，再由 `defines.svh` 派生出贯穿全项目的类型与常量。理解这套「参数 → 派生量」的链条，是读懂后续所有硬件讲义的前提。

#### 4.3.2 核心流程

配置参数如何影响整个硬件设计？流程如下：

```text
  config.svh 里的宏（如 THREADS_PER_CORE、L1D_WAYS、L1D_SETS）
        │
        ▼  `include "config.svh"
  defines.svh：用宏派生出类型与常量
        │   例如 TOTAL_THREADS = THREADS_PER_CORE * NUM_CORES
        │        vector_t = scalar_t × 16
        │        CACHE_LINE_BYTES = NUM_VECTOR_LANES × 4
        ▼
  各 .sv 模块引用这些类型/常量 → 综合出不同规模的电路
```

缓存的容量就是一个典型派生量。`config.svh` 顶部注释给出了缓存大小的计算公式：

\[ \text{CacheSize} = \text{SETS} \times \text{WAYS} \times \text{CACHE\_LINE\_BYTES} \]

而 `CACHE_LINE_BYTES` 又来自向量通道数：

\[ \text{CACHE\_LINE\_BYTES} = \text{NUM\_VECTOR\_LANES} \times 4 = 16 \times 4 = 64 \text{ 字节} \]

这个「缓存行 = 一个向量宽度」的设计不是巧合，而是为了让一条向量访存指令恰好搬走一整行缓存——后续讲义会深入。现在你只需记住：**改一个参数，会牵动一连串派生量**。

#### 4.3.3 源码精读

**① 默认配置参数**。这是本讲最需要记住的一张表：

[NyuziProcessor/hardware/core/config.svh:L40-L51](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/config.svh#L40-L51)

> 这段定义了默认配置：单核（`NUM_CORES 1`）、每核 4 线程（`THREADS_PER_CORE 4`）、L1 数据缓存 4 路 64 组（注释标 16k）、L1 指令缓存同样 4 路 64 组（16k）、L2 缓存 8 路 256 组（注释标 128k）、AXI 数据宽度 32 位、ITLB/DTLB 各 64 项、TLB 4 路。

我们可以用上面的公式验算注释里的「16k」和「128k」：

- L1D = `L1D_SETS(64)` × `L1D_WAYS(4)` × `CACHE_LINE_BYTES(64)` = 16384 字节 = **16 KiB** ✓
- L2  = `L2_SETS(256)` × `L2_WAYS(8)` × `CACHE_LINE_BYTES(64)` = 131072 字节 = **128 KiB** ✓

**② 参数取值的硬约束**。`config.svh` 顶部的注释列出了改参数时必须遵守的规则，这是「技术栈」里容易被忽视却极重要的一部分：

[NyuziProcessor/hardware/core/config.svh:L20-L38](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/config.svh#L20-L38)

> 这段注释说明：`THREADS_PER_CORE` 必须 ≥ 2；缓存路数必须是 1/2/4/8（TLB 例外）；`L1D_WAYS`/`L1I_WAYS` 必须 ≥ `THREADS_PER_CORE`，否则缓存缺失时会 livelock（活锁）；缓存组数必须是 2 的幂；`L1D_SETS` 必须 ≤ 64（受页大小/缓存行大小限制，避免虚拟索引/物理标签缓存的别名问题）；缓存大小 = 组数 × 路数 × 64 字节。

其中「`L1D_WAYS` 必须 ≥ `THREADS_PER_CORE`」是一条非常 Nyuzi 特色的约束：因为多线程共享 L1，若路数少于线程数，所有线程同时缺失时可能谁也拿不到缓存行，陷入活锁。这类「参数之间的隐性耦合」正是本讲要建立的心智模型。

**③ 派生类型与常量**。`defines.svh` 在 `include config.svh` 之后，用这些宏派生出贯穿全项目的基础类型：

[NyuziProcessor/hardware/core/defines.svh:L42-L52](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L42-L52)

> 这段定义了：`NUM_VECTOR_LANES = 16`（16 条向量通道）；`NUM_REGISTERS = 32`（32 个寄存器）；`TOTAL_THREADS = THREADS_PER_CORE * NUM_CORES`（默认 4×1=4）；`scalar_t` 是 32 位标量；`vector_t` 是 16 个 `scalar_t` 拼成的向量（共 512 位）；`local_thread_bitmap_t` 是「每线程一比特」的位图；`subcycle_t` 和 `vector_mask_t` 服务于向量子周期与掩码执行。

这里能看到「参数 → 派生量」的真实链条：`TOTAL_THREADS` 直接由 `config.svh` 的两个宏相乘得到；`vector_t` 的位宽则由 `NUM_VECTOR_LANES` 决定。

**④ 缓存行大小 = 向量宽度**。最后看一处把「向量」和「缓存」绑在一起的派生：

[NyuziProcessor/hardware/core/defines.svh:L293-L298](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L293-L298)

> 这段定义了页大小 `PAGE_SIZE = 'h1000`（4096 字节，4 KiB），并令 `CACHE_LINE_BYTES = NUM_VECTOR_LANES * 4 = 64`，注释明确写道「必须与向量宽度相同」。这正是上面缓存容量公式里那个 64 的来源。

把这几段串起来，你就拿到了本讲实践任务所需的全部数字。

#### 4.3.4 代码实践

> **实践目标**：阅读 `README.md` 与 `config.svh`，用一段话说明 Nyuzi 由哪几部分组成，并指出默认配置下每核线程数、向量通道数与缓存容量各是多少。

**操作步骤**：

1. 读 [README.md:L6-L10](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/README.md#L6-L10)，写下 Nyuzi 的五大组成部分。
2. 读 [config.svh:L40-L51](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/config.svh#L40-L51)，记录默认值：`NUM_CORES`、`THREADS_PER_CORE`、`L1D_WAYS`、`L1D_SETS`、`L2_WAYS`、`L2_SETS`。
3. 读 [defines.svh:L42-L52](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L42-L52)，记录 `NUM_VECTOR_LANES`。
4. 读 [defines.svh:L293-L298](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L293-L298)，确认 `CACHE_LINE_BYTES = 64`。
5. 用缓存容量公式 \(\text{CacheSize} = \text{SETS} \times \text{WAYS} \times 64\) 手算 L1D 与 L2 容量，与注释里的「16k」「128k」对照。
6. 把以上结果写成一段话。

**需要观察的现象**：你会看到 `config.svh` 的注释（16k / 128k）和你手算的结果完全一致；这证明「参数 → 派生量 → 实际规模」的链条是自洽的。

**预期结果**（可直接对照）：

- Nyuzi 由五部分组成：可综合 SystemVerilog 硬件、指令集模拟器、基于 LLVM 的 C/C++ 编译器、软件库、测试。
- 默认配置：**1 核**，**每核 4 线程**，**16 条向量通道**（每通道 32 位，向量共 512 位）。
- L1 数据缓存 = L1 指令缓存 = 64 × 4 × 64 = **16 KiB**；L2 缓存 = 256 × 8 × 64 = **128 KiB**。
- 页大小 4 KiB；ITLB/DTLB 各 64 项、4 路；AXI 数据宽度 32 位。

**待本地验证**：以上数字均来自源码静态阅读，已可确认；若想验证「改参数后缓存容量变化」，可在学完下一讲搭建环境后，临时改 `L2_SETS` 并重新构建，观察综合报告（本讲不要求执行）。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `THREADS_PER_CORE` 改成 8，根据 `config.svh` 的约束，`L1D_WAYS` 至少要改成多少？为什么？

> **参考答案**：至少改成 8（且必须是 1/2/4/8 之一，8 合法）。因为约束规定 `L1D_WAYS` 必须 ≥ `THREADS_PER_CORE`，否则多线程同时发生缓存缺失时可能 livelock——所有线程都拿不到缓存行，互相挤占，谁也无法前进。

**练习 2**：`CACHE_LINE_BYTES` 为什么被定义成 `NUM_VECTOR_LANES * 4` 而不是随便取个数？

> **参考答案**：这样一整条缓存行恰好等于一个向量的位宽（16 通道 × 4 字节 = 64 字节）。于是向量 block 访存指令可以一次性搬走或写回一整行缓存，让 SIMD 数据搬运与缓存行对齐，既省带宽也简化了 fill/evict 逻辑。这是 Nyuzi「向量与缓存协同设计」的体现。

**练习 3**：默认配置下 `TOTAL_THREADS` 等于多少？它是怎么算出来的？

> **参考答案**：等于 4。由 `defines.svh` 中 `TOTAL_THREADS = THREADS_PER_CORE * NUM_CORES = 4 * 1` 派生而来。这也说明「核数」和「每核线程数」是两个独立的并行度维度。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个综合小任务：

> **任务**：假设你要向一位完全没听过 Nyuzi 的同事用 5 分钟介绍这个项目。请基于本讲读过的源码，完成一份「一页纸简介」，必须包含：
>
> 1. **一句话定位**：Nyuzi 是什么（用 README 的原意，不要照抄）。
> 2. **五大组件清单**：每一部分对应仓库里的哪个目录。
> 3. **技术栈三句话**：硬件用什么语言、软件用什么语言、构建/测试用什么。
> 4. **默认配置表**：核数、每核线程数、向量通道数、L1D/L1I/L2 容量、页大小、TLB 项数。
> 5. **一个有意思的设计取舍**：从本讲里挑一个（例如「缓存行 = 向量宽度」或「L1D_WAYS ≥ 线程数」），用一两句解释它背后的考量。

**操作建议**：

- 第 1–3 项参考 [README.md:L6-L17](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/README.md#L6-L17) 与本讲「组件构成」表。
- 第 4 项参考 [config.svh:L40-L51](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/config.svh#L40-L51) 与 [defines.svh:L42-L52](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L42-L52)，容量用公式手算。
- 第 5 项参考 [config.svh:L20-L38](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/config.svh#L20-L38) 的约束注释或 [defines.svh:L296](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L296) 的缓存行定义。

**预期结果**：你能不查任何外部资料、只凭这三个文件写出这份简介。写得出，就说明你已经在心里建起了 Nyuzi 的全局地图——这正是本讲的全部目的。

**待本地验证**：本实践为阅读与写作型，无需运行命令。

## 6. 本讲小结

- Nyuzi 是一个**实验性 GPGPU 处理器**，面向计算密集任务，目的是让人试验微架构与指令集设计的取舍。
- 它由**五大组件**构成：可综合 SystemVerilog 硬件、C 指令集模拟器、基于 LLVM 的 C/C++ 工具链、软件栈（libc/libos/librender/内核/应用）、多层次测试。
- 组件之间构成一个「改 ISA/微架构 → 改硬件与模拟器 → 编译 → 运行 → 测试校验」的**闭环**；硬件和模拟器实现同一套 ISA，可用协同仿真互验。
- 技术栈分三层：硬件（SystemVerilog + Verilator）、软件（C/C++ + LLVM + 汇编）、构建验证（CMake/Make + Python）。
- 硬件是**参数化**的：`config.svh` 给宏，`defines.svh` 派生类型与常量；默认配置为 1 核、每核 4 线程、16 向量通道、L1D/L1I 各 16 KiB、L2 为 128 KiB。
- 参数之间存在**隐性耦合**，例如 `L1D_WAYS ≥ THREADS_PER_CORE`（防 livelock）、`L1D_SETS ≤ 64`（防缓存别名）、`CACHE_LINE_BYTES = 向量宽度`（让向量访存与缓存行对齐）。

## 7. 下一步学习建议

本讲只搭了「全局地图」，还没有真正跑过任何东西。建议按以下顺序继续：

1. **下一讲 `u1-l2` 构建与运行环境搭建**：动手执行 `scripts/setup_tools.sh` 与 `cmake . && make`，把模拟器和 Verilator 装好、构建出可执行文件。这是后续一切实践的前提。
2. **然后读 `u1-l3` 目录结构与源码地图**：把本讲的「五大组件」细化到具体子目录，学会快速定位功能。
3. **再读 `u1-l4` 运行第一个程序**：用 `run_emulator` 跑通 `hello_world`，亲眼看到 Nyuzi 程序的输出。
4. 在阅读后续讲义时，**经常回看本讲的默认配置表**：每核 4 线程、16 通道、16K/128K 缓存这些数字会反复出现在流水线、缓存、调度等讲义里，记住它们能省很多回头翻找的时间。

如果你想在进入下一讲前再多读一点源码，推荐先扫一眼 `hardware/core/defines.svh` 的指令编码部分（`alu_op_t`、`memory_op_t`、`branch_type_t`），那是 ISA 讲义（第 2 单元）的预习材料。
