# 项目概览：ZipCPU 是什么

## 1. 本讲目标

本讲是整本《ZipCPU 学习手册》的第一篇。读完本讲后，你应该能够：

- 用一句话说清楚 **ZipCPU 是什么**：它是一个什么样的处理器、解决什么问题。
- 复述 ZipCPU 的 **核心设计目标**（32 位、RISC、load/store、流水线、双模式、开源等），并能指出它 **为何不是 RISC-V**。
- 说出这个仓库 **包含哪几个主要部分**（RTL、工具链、规范、仿真器、测试），以及每一部分各自负责什么。

本讲几乎不涉及具体的 Verilog 代码细节，重点是建立一张“项目全貌地图”，为后面读 ISA 规范（第 2 单元）和 RTL 实现（第 3 单元）打下基础。

## 2. 前置知识

阅读本讲不需要你已经懂硬件设计，但下面几个名词会反复出现，先建立一个最朴素的印象即可：

- **软核（soft core）CPU**：用硬件描述语言（这里是 Verilog）写成的 CPU“源代码”。它不是一块买来的物理芯片，而是一段可以“综合（synthesize）”进 FPGA 或生成 ASIC 的逻辑设计。你可以把它理解成“用代码写出来的 CPU”。
- **RTL（Register Transfer Level）**：一种硬件描述的抽象层次，ZipCPU 的 `.v` 文件就是 RTL。
- **ISA（Instruction Set Architecture，指令集架构）**：CPU 能“看懂”的指令集合与编程模型（有哪些寄存器、指令长什么样）。它是“软件和硬件之间的合同”。
- **RISC（Reduced Instruction Set Computer）**：精简指令集计算机，强调指令简单、定长、尽量单周期完成。
- **FPGA**：现场可编程门阵列，一种可以被重新配置逻辑的芯片，软核 CPU 通常跑在它上面。
- **总线（bus）**：CPU 和内存、外设之间传输数据的“通道协议”，本项目中会出现 Wishbone、AXI4、AXI4-Lite 三种。
- **load/store 架构**：一种设计原则——只有专门的 load（读内存）和 store（写内存）指令才能访问内存，普通运算指令只在寄存器之间干活。

> 名词会在后续讲义中反复深化，现在只要“知道有这个东西”就够了。

## 3. 本讲源码地图

本讲只看“项目入口级”的文档文件，不深入任何具体模块代码：

| 文件 | 作用 |
| --- | --- |
| [README.md](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/README.md) | 项目的“门面”：一段话讲清 ZipCPU 的定位、设计目标、独特之处、如何上手、当前状态。 |
| [INSTALL.md](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/INSTALL.md) | 安装与构建说明：仓库里每个目录放什么、构建依赖、如何 `make`。 |
| [doc/src/spec.tex](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex) | 规范文档的 LaTeX 源文件（编译后得到 `doc/spec.pdf`）。它是整个项目最权威的说明，包含 Introduction、Key Features、ISA、流水线、外设、调试等全部章节。 |

此外，了解仓库的顶层目录结构对建立“全貌地图”很关键，本讲也会一并梳理：`rtl/`、`sw/`、`sim/`、`bench/`、`doc/`。

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：

- **4.1 README 中的设计目标清单** —— 直接读 README，看作者自己怎么定义这台 CPU。
- **4.2 spec.tex 的 Introduction / Key Features 章节** —— 看规范文档里更正式、更完整的能力清单。

### 4.1 README 中的设计目标清单

#### 4.1.1 概念说明

README 的开头一句话就给 ZipCPU 定了性：

> The Zip CPU is a small, light-weight, RISC CPU.

也就是说，ZipCPU 的第一定位是 **“小而轻的 RISC CPU”**。紧接着作者用一串 bullet 列出了“具体设计目标（Specific design goals）”。这份清单是理解整个项目的钥匙——后面所有的 RTL 实现取舍、工具链设计、外设安排，都可以回溯到这几条目标。

为什么要先读它？因为软核 CPU 的世界里有 ARM、Microblaze、Nios、OpenRISC、RISC-V 等很多选择，ZipCPU 之所以“另起炉灶”，正是因为它有一组 **不同于这些既有方案的目标**。看懂目标，你才能看懂它后续所有的设计决策。

#### 4.1.2 核心流程

README 的“设计目标清单”可以归纳成下面这张逻辑图（目标 → 对应的设计决策）：

```text
小而轻的 RISC CPU
   │
   ├── 32-bit            → 所有寄存器/地址/指令都是 32 位，简单统一
   ├── 单周期为主         → 大多数指令一个时钟周期完成（乘除/访存/浮点例外）
   ├── load/store        → 只有 load/store 指令能碰内存
   ├── 多种总线           → 提供 Wishbone / AXI4-Lite / AXI4 三种封装
   ├── (准)Von-Neumann   → 指令和数据共享同一地址空间
   ├── 流水线             → 取指/译码/读操作数/(ALU+访存+除法)/写回
   ├── 双模式             → supervisor / user 两种权限级别
   └── 完全开源 GPLv3     → 可自由仿真、综合、学习
```

随后 README 还有一节 **Unique features（独特之处）**，列出几条“别人一般没有、而 ZipCPU 有”的特点：指令只有 29 条、几乎全部指令可条件执行、重度使用流水线、用双寄存器组代替中断向量。

#### 4.1.3 源码精读

先看 README 开头的设计目标清单：

[README.md:L3-L24](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/README.md#L3-L24) — 作者把 ZipCPU 的设计目标逐条列出。要点摘录：

- **32-bit**：寄存器、地址、指令全都是 32 位。
- **A RISC CPU**：指令名义上单周期完成，乘法、除法、访存、（未来的）浮点是例外。
- **明确声明“不是 RISC-V”**：`(Note that the ZipCPU is *not* a RISC-V CPU, nor does it copy from any other instruction set but its own.)` —— 这条对本讲尤其重要。
- **load/store 架构**：只有 load/store 指令可以访问内存。
- **三种总线**：包含 Wishbone、AXI4-Lite、AXI4 三种内存接口选项。
- **(最小化的)Von-Neumann 架构**：Wishbone 封装里指令和数据共享总线；AXI4-Lite / AXI4 封装则是指令、数据各走一条总线，但地址空间本身是共用的。
- **流水线架构**：包含 prefetch（取指）、decode（译码）、read-operand（读操作数）、一个合并级（ALU + memory + divide + 浮点）、以及最后的 write-back（写回）。
- **双模式机器**：supervisor（监管）和 user（用户）两种模式，权限级别不同。
- **完全开源**，采用 GPLv3 协议。

再看 README 的“独特之处”一节：

[README.md:L26-L34](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/README.md#L26-L34) — 列出了几条 ZipCPU 区别于其它 CPU 的地方：

- 目前只实现了 **29 条指令**，另外预留 6 条给“将来要做但还没做”的浮点单元。
- **（几乎）所有指令都可以条件执行**。例外是 LDI、BREAK、LOCK、SIM、NOOP；汇编器会把“带条件的 LDI”悄悄改写成两条指令 `BREV` + `LDILO` 的等价组合。
- **重度使用流水线**：用流水线式内存核时，连续两次 load 可能只比一次 load 多花一个时钟。
- **没有中断向量，而是两套寄存器组**：中断发生时，CPU 直接从用户寄存器组切到监管寄存器组，自动完成上下文保存与恢复；可选的 [icontrol](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/icontrol.v) 外设可以把多路外部中断合并成一条中断线。

最后，README 的 “Not yet integrated” 一节，对理解“项目当前状态”也很有用：

[README.md:L99-L106](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/README.md#L99-L106) — 作者指出 [zipmmu](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/zipmmu.v)（内存管理单元）需要为新的 ZipCore 重写，计划放在“ZipCore 与各种总线封装之间”，让缓存基于物理地址。这说明：**ZipCPU 仍在演进**，某些组件尚未完全整合。

#### 4.1.4 代码实践

这是一个“源码阅读 + 归纳”型实践，无需运行任何命令。

1. **实践目标**：从 README 的设计目标清单里，提炼出你认为最关键的 **三条** 设计目标，并说明理由。
2. **操作步骤**：
   - 打开 [README.md:L3-L24](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/README.md#L3-L24)。
   - 逐条阅读这 9 个 bullet，思考“哪几条最能解释 ZipCPU 为什么长成现在这样”。
3. **需要观察的现象**：你会发现，很多目标之间是“联动”的。例如“load/store”和“没有 I/O 指令、外设全靠内存映射”是配套的；“双模式”和“没有中断向量、用双寄存器组”是配套的。
4. **预期结果**（参考答案， yours may vary）：三条最关键的目标可归纳为——
   - **“小而轻的 RISC”**（决定它只有 29 条指令、面积小、面向 FPGA）。
   - **“load/store + 内存映射外设”**（决定它的编程模型与系统拓扑）。
   - **“流水线 + 可条件执行 + 双寄存器组”**（决定它与 RISC-V 等主流 ISA 在微架构和中断模型上的根本差异）。
5. 本实践不涉及命令执行，因此不涉及“运行结果待验证”的问题。

#### 4.1.5 小练习与答案

**练习 1**：README 说 ZipCPU 是“（最小化的）Von-Neumann 架构”，又说 AXI4-Lite / AXI4 封装里指令和数据各有独立总线。这两句话矛盾吗？为什么？

> **参考答案**：不矛盾。Von-Neumann 在这里指的是 **地址空间统一**（指令和数据共享同一套地址空间），而不是“物理上必须共用同一条总线”。Wishbone 封装确实把指令和数据合并在一条总线上；AXI4-Lite / AXI4 封装则把它们拆成两条独立总线，但两者访问的是同一个地址空间。

**练习 2**：README 提到“汇编器会把带条件的 LDI 改写成 BREV + LDILO”。请猜一下：为什么要做这种“派生指令”转换？（提示：和 LD  I 本身不能条件执行有关。）

> **参考答案**：因为 LDI（load immediate，装载立即数）属于“不能条件执行”的少数指令之一。当程序员写出“带条件的装立即数”时，汇编器无法用单条 LDI 实现，于是用两条无条件/组合指令 `BREV`（按位反转）+ `LDILO`（装载低半字）拼出等价效果。这正是 spec 里所说的“派生指令（Derived Instructions）”。

**练习 3**：根据 README，ZipCPU 为什么“没有中断向量”？它用什么替代？

> **参考答案**：它用 **两套寄存器组（supervisor / user）** 替代中断向量。中断发生时，CPU 直接从用户寄存器组切换到监管寄存器组，监管态的上下文在两次中断之间被自动保存/保留/恢复，因此不需要向量表也能快速响应中断。

---

### 4.2 spec.tex 的 Introduction / Key Features 章节

#### 4.2.1 概念说明

README 是“面向用户的口语化介绍”，而 `doc/src/spec.tex` 是 **正式的规范文档（Specification）**。编译它得到 `doc/spec.pdf`，是整个项目最权威的说明。spec 的作者（Dan Gisselquist）在文件开头明确写道：spec “取代（supersedes）仓库中其它任何关于指令集或 CPU 的信息”。

本模块只读 spec 的两个章节：

- **Introduction（引言）**：讲 ZipCPU 的基本哲学——为什么要“最小化逻辑”。
- **Key Features（关键特性）**：一份比 README 更正式、更完整的能力清单。

读这一节的意义在于：**很多 README 没强调的细节（如 Big Endian、压缩指令子集、总线宽度可配置、可选调试接口等）都在这里**。这些细节会在后续讲义反复出现，先在这里建立印象。

#### 4.2.2 核心流程

spec 的“Key Features”可以视作 README 设计目标的“扩展版”，补充了若干工程化能力。归纳如下：

```text
Key Features（相对 README 的补充点用 ★ 标注）
   │
   ├── 32-bit / RISC / load-store          （与 README 一致）
   ├── 没有 I/O 指令，外设全部内存映射      （README 隐含，这里明确）
   ├── ★ Big Endian（大端）                 （README 未提，重要！）
   ├── ★ 压缩指令子集 CIS                  （对常用指令做压缩）
   ├── Von-Neumann / 流水线                 （与 README 一致）
   ├── 开源 GPLv3                           （与 README 一致）
   ├── 多种内存控制器 + 可配置大小缓存
   ├── ★ 总线宽度可配置（≥32 位即可）
   ├── ★ 可选的外部调试接口（启停/读写寄存器/单步）
   └── ★ 高度可配置（时钟门控/跟踪端口/性能剖析/多任务/多种乘法/除法/早分支…）
```

其中标 ★ 的几条，是后续讲义（第 4、5 单元）会重点展开的内容，本讲只需“知道有这些能力”。

#### 4.2.3 源码精读

先看 spec 的 **Introduction** 章节：

[doc/src/spec.tex:L255-L271](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L255-L271) — 这一段给出 ZipCPU 的核心哲学。要点：

- ZipCPU 是一个 **软核 CPU**，设计目标是 **面积小、指令集最小化**。
- 基本哲学是：**在保持“全流水线、32 位、能跑现代操作系统（暂不含 MMU）”的前提下，尽量减少逻辑**，并兼容多种 FPGA 架构。
- 作者把 ZipCPU 比作“**穷人版的大架构替代品**（a poor man's alternative to the larger architectures）”——这句俏皮话很好地概括了它的定位：不追求最强，追求“够用、可控、可学”。

再看 spec 的 **Key Features** 章节：

[doc/src/spec.tex:L272-L326](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L272-L326) — 这是正式的能力清单。除与 README 重叠的条目外，**特别值得记下的几条**：

- **Big Endian（大端序）**（第 290 行附近）——这是 README 没有强调、但很重要的架构属性。它会影响你如何看待内存中的字节顺序，后续读访存模块（`memops.v` 等）时要记得这一点。
- **压缩指令子集（compressed instruction subset）**——对最常用指令提供压缩表示，省指令存储。
- **没有 I/O 指令、外设内存映射**——CPU 不直接“读写端口”，所有外设都映射到地址空间。
- **多种内存控制器 + 可配置缓存**——支持 Wishbone / AXI4-Lite / AXI4，缓存大小用户可配。
- **总线宽度可配置（bus-width agile）**——只要总线宽度 ≥ 32 位即可，宽度可参数化。
- **可选外部调试接口**——允许外部调试器启动/停止 CPU、读写寄存器、单步执行。
- **高度可配置**——可选时钟门控、外部跟踪端口、性能剖析（profiler）、多任务、多种乘法配置、除法支持、早分支（early branching）等。

如果你想了解“作者为什么要另造一个 CPU、而不是用 RISC-V”，可以读 spec 的 **前言（Preface）**：

[doc/src/spec.tex:L174-L228](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L174-L228) — 作者给出了几条理由：

- **“因为我能做到”**（Because I can）。
- **开源与厂商无关**：希望能同时仿真、又能放进 FPGA，生成的 Verilog 能在 Xilinx / Altera / Lattice 上等价运行；选 Verilator 作为仿真工具意味着必须用纯 Verilog、不能用任何专有核——所以 ARM、Microblaze、Nios 都被排除。
- **比 OpenRISC 更轻量**：OpenRISC 目标是“全功能 CPU”、定义了 200 多条指令；ZipCPU 目标是“简单、省资源”，只有少量指令（浮点除外）已全部实现。
- **作为学习项目**：作者借此深入理解 CPU 微架构、验证与回归测试。

这几条解释了 ZipCPU 与 RISC-V / OpenRISC / ARM 的根本差异。

> 顺带一提，spec 在 [第 149-167 行的修订历史](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L149-L167) 中记录了演进：例如 Rev 3.0 引入了“新 ZipDMA、调试接口、多总线支持和总线宽度无关”；这说明当前版本（Rev 3.0+）已经是一个支持 AXI4 / AXI4-Lite、总线宽度可配置的成熟软核。

#### 4.2.4 代码实践

这也是一个“源码阅读 + 对比”型实践，帮助你看清 spec 比 README 多了什么。

1. **实践目标**：找出 spec 的 Key Features 比 README 设计目标清单 **多出来** 的至少 3 条能力，并思考它们分别会影响哪一篇后续讲义。
2. **操作步骤**：
   - 打开 [doc/src/spec.tex:L272-L326](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L272-L326)。
   - 把它的每一条与 [README.md:L3-L24](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/README.md#L3-L24) 对照。
3. **需要观察的现象**：spec 里会出现 README 没有的术语，例如 Big Endian、compressed instruction subset、bus-width agile、debug interface、profiler 等。
4. **预期结果**（参考）：
   - **Big Endian** —— 影响第 2 单元（ISA）与第 3 单元访存模块（`memops.v`）。
   - **压缩指令子集 CIS** —— 影响第 3 单元译码（`idecode.v`）。
   - **可选调试接口** —— 影响第 5 单元调试讲义。
   - **高度可配置（OPT_*）** —— 影响第 5 单元“构建参数与集成选项”。
5. 本实践无需运行命令。

#### 4.2.5 小练习与答案

**练习 1**：spec 说 ZipCPU 是 Big Endian，这意味着一个 32 位整数 `0x12345678` 存到内存地址 `0x1000` 后，地址 `0x1000` 这个字节里放的是什么？

> **参考答案**：大端序下，**最高有效字节存在最低地址**。所以 `0x1000` 处放 `0x12`，`0x1001` 放 `0x34`，`0x1002` 放 `0x56`，`0x1003` 放 `0x78`。（这与 x86 的小端序相反，写跨平台代码或看内存转储时要注意。）

**练习 2**：spec 的前言里，作者为什么排除 ARM、Microblaze、Nios？

> **参考答案**：因为作者选择 Verilator 作为仿真工具，而 Verilator 要求使用纯 Verilog、不允许使用任何专有（proprietary）核。ARM、Microblaze、Nios 都带有厂商专有成分或无法自由仿真/综合，因此被排除。这也直接催生了“用一个开源、厂商无关的软核”的需求。

**练习 3**：spec 的 Introduction 说 ZipCPU 的哲学是“最小化逻辑的同时保持全流水线、32 位、可跑现代 OS（暂不含 MMU）”。请结合 README 的 “Not yet integrated” 说明：这里的“暂不含 MMU”是什么意思？

> **参考答案**：意思是 ZipCPU **目前还没有把 MMU 完整集成进新 ZipCore**。README 第 99-106 行明确说 [zipmmu.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/peripherals/zipmmu.v) 需要重写，计划放在 ZipCore 与总线封装之间。因此“跑现代 OS”目前还缺少完整的虚拟内存支持。

## 5. 综合实践

本讲的综合实践是规格里指定的“阅读 + 写作”任务，它把上面两个最小模块串起来，并加上对仓库目录结构的梳理。**全程无需运行任何命令**（如果你本地装好了全部依赖，可以选做第 5 步的构建，但那属于后续讲义范畴，结果待本地验证）。

**任务**：阅读 README 与 INSTALL，写一段话说明 ZipCPU 的三条最关键设计目标，并指出它为何不是 RISC-V；列出仓库顶层 5 个目录各自的作用。

**操作步骤**：

1. 读 [README.md:L3-L24](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/README.md#L3-L24) 与 [doc/src/spec.tex:L255-L271](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L255-L271)，提炼三条最关键设计目标。
2. 读 [README.md:L9](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/README.md#L9)（“不是 RISC-V”）与 [doc/src/spec.tex:L174-L228](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex#L174-L228)（为何另造 CPU），写一段话解释“为何不是 RISC-V”。
3. 读 [INSTALL.md:L4-L18](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/INSTALL.md#L4-L18)，了解仓库里每个顶层目录放什么。
4. 用下面这张表对照你梳理出的 5 个顶层目录（可作为参考答案）：

| 顶层目录 | 作用（依据 INSTALL.md / 仓库内容） |
| --- | --- |
| `rtl/` | **Verilog RTL**，即 CPU 的硬件实现源码。包括核心 `rtl/core/`（如 [zipcore.v](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/rtl/core/zipcore.v)）、总线支持 `rtl/ex/`、外设 `rtl/peripherals/`、AXI DMA `rtl/zipdma/`，以及四种顶层封装 `zipsystem.v` / `zipbones.v` / `zipaxi.v` / `zipaxil.v`。 |
| `sw/` | **软件工具链**：带 ZipCPU 后端的 GCC、binutils、newlib 的补丁与构建脚本（`gcc-zippatch.patch`、`gas-zippatch.patch`、`nlib-zippatch.patch` 等），以及汇编级调试器 `zipdbg`。 |
| `sim/` | **两套仿真器**：`sim/cpp/` 是独立于 RTL 的 C++ 指令级模拟器（ISS）；`sim/verilator/` 是基于 RTL 的 Verilator 仿真器与各种测试台。 |
| `bench/` | **基准与测试**：`bench/asm/`（汇编测试程序）、`bench/cpp/`（C++ 基准）、`bench/formal/`（基于 SymbiYosys 的形式化验证 `.sby` 配置）。 |
| `doc/` | **规范文档**：`doc/src/spec.tex` 编译生成 `doc/spec.pdf`，是项目最权威的说明。 |

5. （选做）若你想尝试构建，可读 [INSTALL.md:L20-L34](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/INSTALL.md#L20-L34) 了解依赖（texinfo、g++、flex、bison、Verilator v5.007+、libgmp/libmpfr/libmpc 等）与“在主目录 `make`”的流程。注意：作者坦言该流程会“半路失败一次”，需要把 `sw/install/cross-tools/bin` 加入 `PATH` 后重新 `make` 才能跑完。**本步涉及实际构建，结果待本地验证。**

**预期结果**：

- 你应该能写出一段类似下面的话——
  > ZipCPU 的三条最关键设计目标是：① **小而轻的 32 位 RISC 软核**（面积小、指令少、面向 FPGA）；② **load/store + 内存映射外设** 的简洁系统模型；③ **流水线 + 几乎全指令可条件执行 + 双寄存器组替代中断向量** 的微架构取舍。它 **不是 RISC-V**：README 明确声明它不复制任何既有 ISA，且 spec 前言说明作者追求开源、厂商无关、比 OpenRISC 更轻量，并以此作为学习项目——因此 ZipCPU 拥有自己的指令集、自己的条件执行模型和自己的双寄存器组中断模型，与 RISC-V 的标准化、分支为主、单寄存器组 + 向量中断的设计路径根本不同。

## 6. 本讲小结

- ZipCPU 是一个 **小而轻的 32 位 RISC 软核 CPU**，用 Verilog 写成，面向 FPGA，开源（GPLv3）。
- 它的核心设计目标是：32 位、单周期为主的 RISC、load/store 架构、流水线、supervisor/user 双模式、支持 Wishbone/AXI4-Lite/AXI4 三种总线。
- 它的 **独特之处** 在于：只有 29 条指令、几乎所有指令可条件执行、用 **双寄存器组** 替代中断向量、重度依赖流水线。
- README 明确声明 **它不是 RISC-V**，也不复制任何其它 ISA；spec 前言解释了作者为何要在 RISC-V/OpenRISC/ARM 之外另造一个开源、厂商无关、更轻量的 CPU。
- spec 比 README 多出几个重要细节：**Big Endian**、压缩指令子集 CIS、总线宽度可配置、可选调试接口、高度可配置（OPT_*）。
- 仓库由 5 个顶层目录构成：`rtl/`（硬件实现）、`sw/`（工具链）、`sim/`（仿真器）、`bench/`（测试与形式化验证）、`doc/`（规范文档）。

## 7. 下一步学习建议

本讲建立了“项目全貌地图”，接下来建议：

1. **继续第 1 单元**：先读 [u1-l2 仓库目录结构与顶层构建系统](u1-l2-repo-layout-and-build.md)，弄清顶层 `Makefile` 的各个目标和如何构建；再读 u1-l3 了解四种顶层封装（zipsystem/zipbones/zipaxi/zipaxil）；最后用 u1-l4 在模拟器里跑通第一个程序。
2. **进入第 2 单元 ISA 规范**：如果你更想先理解“CPU 到底能做什么”，可以直接读 u2-l1，从 spec 的寄存器组与状态寄存器 CC 开始。ISA 是后续所有 RTL 讲义的“合同”，越早建立印象越有利。
3. **建议同步翻阅** `doc/spec.pdf`（若已编译）或直接读 [doc/src/spec.tex](https://github.com/ZipCPU/zipcpu/blob/42606d2d6ef55df313772232977621b2d72f0159/doc/src/spec.tex) 的目录结构，把它作为贯穿全书的“权威字典”。
