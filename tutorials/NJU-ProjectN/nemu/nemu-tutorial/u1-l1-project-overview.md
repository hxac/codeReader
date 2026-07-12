# NEMU 是什么——教学全系统模拟器的定位与能力

## 1. 本讲目标

本讲是整本 NEMU 学习手册的第一篇，目标是帮你建立一个「全局认知」，而不是深入任何一处实现细节。读完本讲后，你应该能够：

- 说清楚 NEMU 是什么、为什么存在，以及它与南京大学 ICS 课程 PA 作业、AM 项目之间的关系。
- 列举 NEMU 的核心能力（监视器、CPU、内存、分页、中断、设备），同时也能说出它**明确不支持**的特性（如保护模式、浮点指令）。
- 知道 NEMU 每个子系统大致对应哪一个源码目录，拿到源码后能快速定位「我想看的东西在哪里」。
- 看懂顶层 `Kconfig` 中的几组关键选项（ISA、执行引擎、运行模式、构建目标），并能找到默认值。

本讲不要求你写任何 C 代码，重点在于「读懂 + 画图 + 找配置」。

## 2. 前置知识

在开始之前，最好对下面几个概念有一点印象。如果没有也没关系，本讲会用通俗语言再解释一遍。

- **模拟器（Emulator）**：用软件模拟出一台「假」的计算机，让原本为某种 CPU 写的程序，能在另一种机器上跑起来。和「虚拟机」相比，模拟器通常不依赖硬件虚拟化指令，纯靠软件解释执行，速度慢但跨平台、可观测。
- **ISA（Instruction Set Architecture，指令集架构）**：CPU 能理解的指令集合与编程模型，例如 x86、MIPS、RISC-V。同一个 ISA 下的程序，理论上可以在任何实现该 ISA 的 CPU（或模拟器）上运行。
- **全系统模拟（Full-system Emulation）**：模拟完整的硬件，包括 CPU、内存、外设，从而能运行一个完整的操作系统或裸机程序。与之相对的是「用户态模拟」，只模拟某一条用户程序的指令，把系统调用转发给宿主机。
- **Kconfig / menuconfig**：源自 Linux 内核的一套配置系统，用图形菜单选择功能开关，生成 `.config` 文件，再由构建系统转成 C 宏定义。NEMU 借用了这套机制。
- **ICS 与 PA**：ICS（计算机系统基础）是南京大学的系统类入门课程；PA（Programming Assignment）是该课程的贯穿性大作业，要求学生从零实现一个模拟器——也就是 NEMU。

> 关键直觉：NEMU 不是为了「跑得快」而存在的，而是为了「让人看清楚一台计算机是怎么工作的」。所以它刻意保持简单、可读，并在源码里留下大量 `TODO` 留给学生实现。

## 3. 本讲源码地图

本讲只涉及两个顶层文件，但它们是理解整个项目的「入口地图」。

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `README.md` | 项目自述文件，用一段话 + 一个特性清单说明 NEMU 是什么、能做什么 | 项目定位、核心能力、不支持特性、子系统划分 |
| `Kconfig` | 顶层配置菜单，定义 ISA、引擎、运行模式、构建目标等开关 | 默认 ISA、默认运行模式、各选项的依赖关系 |

后续讲义会逐层进入 `src/` 下的子目录。这里先给一张「子系统 → 源码目录」对照表，作为本讲实践任务的参考（基于当前仓库实际目录结构）：

| 子系统 | 对应源码目录 | 说明 |
| --- | --- | --- |
| 监视器 / 简单调试器 SDB | `src/monitor/`、`src/monitor/sdb/` | 命令行交互、单步、寄存器/内存查看、表达式求值、监视点 |
| CPU 执行引擎 | `src/cpu/`、`src/engine/interpreter/` | 取指译码执行主循环、状态机 |
| 内存 | `src/memory/` | 物理内存 `paddr`、虚拟内存 `vaddr` |
| 分页 / MMU | `src/isa/<isa>/system/` | ISA 相关的页表翻译、中断异常 |
| 中断与异常 | `src/isa/<isa>/system/`、`src/device/intr.c` | 中断挂起、查询、响应 |
| 设备与 I/O | `src/device/`、`src/device/io/` | 串口/定时器/键盘/VGA/audio，以及 mmio/pio 映射 |
| ISA 实现 | `src/isa/<isa>/` | 寄存器、指令译码、init（如 `riscv32`、`x86`） |
| 工具与公共头 | `src/utils/`、`include/` | 日志、反汇编、宏、公共类型 |

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：

1. **README 概述**——NEMU 是什么、能做什么、不做什么。
2. **Kconfig 顶层选项**——项目怎么配置，默认值是什么。

### 4.1 README 概述：NEMU 的定位与能力

#### 4.1.1 概念说明

NEMU 的全称是 **NJU Emulator**，由南京大学 Zihao Yu 等人开发，专门用于教学。它是一个「简单但完整」的全系统模拟器：

- **简单**：代码量可控、风格统一、刻意省略了真实硬件里很多复杂细节（如保护模式、浮点）。
- **完整**：它模拟了一台计算机该有的全部关键部件——CPU、内存、外设、中断，能跑起一个最小的操作系统或裸机程序。

它的教学定位决定了三件事：

1. 它是 ICS 课程 PA 作业的「脚手架」：仓库里大量函数只给出签名和注释，真正的实现由学生完成。
2. 它和 [AM（Abstract Machine）项目](https://github.com/NJU-ProjectN/abstract-machine) 配套使用。AM 提供一层抽象的运行环境，让同一份程序能跑在 NEMU、QEMU 甚至真实硬件上；NEMU 则是这套抽象之下的一个「参考实现」。
3. 它支持多种 ISA（x86、mips32、riscv32、riscv64、loongarch32r），但每种 ISA 都只实现一个教学够用的子集。

#### 4.1.2 核心流程

理解 NEMU 的能力，最直接的方式是把它看成一个「由若干子系统拼装出来的虚拟机」。它的特性清单可以归纳成下面这张分层图（用文字表示）：

```
┌─────────────────────────────────────────────┐
│  监视器 monitor + 简单调试器 SDB              │  ← 人机交互层
│    单步 / 查看寄存器内存 / 表达式求值 /       │
│    监视点 / 差分测试 / 快照                   │
├─────────────────────────────────────────────┤
│  CPU 核心（解释执行引擎）                     │  ← 执行层
│    取指 → 译码 → 执行（INSTPAT 模式匹配）    │
├──────────────┬──────────────────────────────┤
│  内存 memory  │  分页 paging（MMU 翻译）      │  ← 存储层
│   paddr/vaddr │  TLB 可选，不支持保护         │
├──────────────┴──────────────────────────────┤
│  中断与异常（不支持保护）                     │  ← 控制流层
├─────────────────────────────────────────────┤
│  设备 device：serial/timer/keyboard/VGA/audio│  ← I/O 层
│  两类 I/O：port-mapped / memory-mapped        │
└─────────────────────────────────────────────┘
```

这张图对应 README 里「The main features of NEMU include」之后的全部条目。后面每一篇讲义，基本都是在深入这张图的某一层。

需要特别记住的是 NEMU **明确不做**的事，因为「不做什么」和「做什么」一样重要：

- x86：不支持实模式（real mode），不支持 x87 浮点。
- mips32：不支持 CP1 浮点。
- riscv32 / riscv64：只实现 RV32IM / RV64IM（整数 + 乘除法），不含浮点等扩展。
- 分页：TLB 可选（mips32 必须有），但不支持保护机制。
- 中断异常：不支持保护机制。

这些「不做」是教学取舍：保留足以理解计算机工作原理的核心机制，砍掉让学生迷失在细节里的复杂部分。

#### 4.1.3 源码精读

README 开头一句话定义了 NEMU 的本质：

[README.md:3-5](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/README.md#L3-L5) —— 说明 NEMU 是为教学设计的「简单但完整」的全系统模拟器，目前支持 x86、mips32、riscv32、riscv64，并指向 AM 项目用于构建在其上运行的程序。

接下来是核心特性清单。首先是监视器与简单调试器部分：

[README.md:7-14](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/README.md#L7-L14) —— 列出 monitor + SDB 的能力：单步、寄存器/内存查看、无符号表达式求值、监视点、差分测试、快照。

然后是 CPU 与 ISA 子集说明：

[README.md:15-24](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/README.md#L15-L24) —— CPU 支持常用指令，并逐 ISA 列出限制（x86 不支持实模式与 x87 浮点；mips32 不支持 CP1 浮点；riscv32/64 仅 IM 扩展）。

最后是内存、分页、中断、设备与 I/O：

[README.md:25-35](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/README.md#L25-L35) —— 内存、分页（TLB 可选但不支持保护）、中断异常（不支持保护）、5 个设备（serial/timer/keyboard/VGA/audio，多为简化且不可编程）、两类 I/O（端口映射与内存映射）。

#### 4.1.4 代码实践

**实践目标**：用一张图把 NEMU 的子系统组成固化下来，并标注每个子系统对应的源码目录，建立「特性 ↔ 代码位置」的直觉。

**操作步骤**：

1. 重新阅读 [README.md:7-35](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/README.md#L7-L35) 的特性清单。
2. 对照本讲第 3 节的「子系统 → 源码目录」对照表，也可以用下面的命令自行核对目录是否存在（只读命令，不修改任何文件）：
   ```bash
   ls src/monitor src/cpu src/memory src/device src/isa/riscv32
   ```
3. 在纸上或文本编辑器里画一张分层图（可参考 4.1.2 的文字版），每一层写上：子系统名、README 中的关键特性、对应的 `src/` 目录。

**需要观察的现象**：

- README 列出的 6 大类特性（monitor、CPU、memory、paging、interrupt、devices/I/O），每一类都能在 `src/` 下找到一个对应的目录。
- 设备相关的 `.c` 文件名（`serial.c`、`timer.c`、`keyboard.c`、`vga.c`、`audio.c`）正好和 README 列出的 5 个设备一一对应。

**预期结果**：得到一张同时包含「特性」和「源码目录」的组成图，例如：

| 层 | 特性（来自 README） | 源码目录 |
| --- | --- | --- |
| 人机交互 | monitor + SDB（单步/查看/表达式/监视点/差分/快照） | `src/monitor/`, `src/monitor/sdb/` |
| 执行 | CPU 核心，解释执行 | `src/cpu/`, `src/engine/interpreter/` |
| 存储 | memory + paging | `src/memory/`, `src/isa/riscv32/system/` |
| 控制流 | interrupt and exception | `src/isa/riscv32/system/`, `src/device/intr.c` |
| I/O | 5 设备 + mmio/pio | `src/device/`, `src/device/io/` |

> 如果无法在本地运行命令核对目录，可标注「待本地验证」，但本表已基于当前仓库目录结构给出。

#### 4.1.5 小练习与答案

**练习 1**：README 说 NEMU 是「full-system emulator」。请用一句话解释「全系统模拟」和「用户态模拟」的区别。

**参考答案**：全系统模拟模拟的是一台完整的计算机（含 CPU、内存、外设、中断），可以运行裸机程序或操作系统；用户态模拟只模拟单条用户程序的指令，把系统调用直接转发给宿主机，不需要模拟外设和内核态。

**练习 2**：riscv32 在 NEMU 中「只支持 RV32IM」。这里的 I、M 分别指什么？为什么说这符合 NEMU 的教学定位？

**参考答案**：I 是整数基础指令集（Integer），M 是乘除法扩展（Multiplication）。只实现 IM 砍掉了浮点（F/D）、原子（A）、压缩指令（C）等扩展，既能让一个最小的 RISC-V 程序跑起来，又避免了把学生淹没在过多指令细节里，符合「简单但完整」的教学取舍。

### 4.2 Kconfig 顶层选项

#### 4.2.1 概念说明

NEMU 的构建系统借鉴了 Linux 内核的 **Kconfig + menuconfig** 机制。它的核心思想是：

- 在 `Kconfig` 文件里用一种声明式语法描述「有哪些可配置选项、各自的默认值、相互之间的依赖关系」。
- 用户运行 `make menuconfig` 进入一个文本菜单，勾选想要的选项。
- 构建系统把选择结果写进 `.config`，并生成 `include/generated/autoconf.h`（一堆 `#define CONFIG_xxx` 宏）和 `scripts/auto.conf`（Makefile 可 include 的变量）。
- 源码和 `Makefile` 通过 `#ifdef CONFIG_xxx` 或 `$(CONFIG_xxx)` 来做条件编译，从而用一套源码适配多种 ISA、多种运行模式、多种构建目标。

> 关键直觉：`Kconfig` 是 NEMU 的「控制面板」。改一个选项，就可能换一种 ISA、换一种运行模式，甚至把 NEMU 从一个可执行程序变成一个动态库。

#### 4.2.2 核心流程

顶层 `Kconfig` 里最重要的几组选项构成了一个「四维配置空间」：

```
1. Base ISA        →  x86 / mips32 / riscv / loongarch32r   （riscv 默认）
                       └─ riscv 再由 RV64 决定 32 还是 64 位
2. 执行引擎 engine  →  Interpreter                            （默认）
3. 运行模式 mode    →  System mode（全系统）                   （默认）
4. 构建目标 target  →  Native ELF / Shared object / AM         （Native ELF 默认）
```

它们之间的依赖关系大致是：

- 只有 `MODE_SYSTEM`（全系统模式）才会引入 `src/memory/Kconfig` 和 `src/device/Kconfig`（即才有内存和设备的配置）。
- 只有 `ISA_riscv` 才会 source `src/isa/riscv32/Kconfig`（才有 RV64 / RVE 选项）。
- 差分测试 `DIFFTEST` 依赖 `TARGET_NATIVE_ELF`；指令追踪 `ITRACE` 依赖 `TRACE` 且只在 native + interpreter 下生效。

这套依赖关系解释了为什么「换一个 ISA」或「切到 AM 模式」时，编译进来的源文件集合会发生变化——这正是下一篇讲义（构建系统）要展开的内容。

#### 4.2.3 源码精读

第一组是 **Base ISA** 选择，默认是 riscv：

[Kconfig:3-14](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Kconfig#L3-L14) —— 用 `choice` 给出 x86 / mips32 / riscv / loongarch32r 四选一，`default ISA_riscv`，所以默认 ISA 是 riscv。

注意 riscv 还会进一步细分 32/64 位：

[Kconfig:16-28](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Kconfig#L16-L28) —— 把字符串 `ISA` 解析成具体值：`riscv32`（默认，`!RV64`）或 `riscv64`（`RV64` 时）。因此默认运行的是 **riscv32**。`ISA64` 仅在 `ISA_riscv && RV64` 时为 y。

接下来是执行引擎与运行模式：

[Kconfig:35-58](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Kconfig#L35-L58) —— 引擎默认 `ENGINE_INTERPRETER`（解释执行）；运行模式默认 `MODE_SYSTEM`（全系统模式，支持特权指令、MMU、设备）。所以 NEMU 的默认形态是「全系统 + 解释执行」。

构建目标决定了产物形态：

[Kconfig:60-69](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Kconfig#L60-L69) —— 默认 `TARGET_NATIVE_ELF`（在 Linux 上生成可执行文件）；`TARGET_SHARE` 把 NEMU 编译成动态库，用作差分测试的 REF；`TARGET_AM` 则把 NEMU 本身当成一个 AM 程序（教学用途，标注 DON'T CHOOSE）。

最后是模式与设备/内存配置的依赖关系：

[Kconfig:196-199](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Kconfig#L196-L199) —— 只有 `MODE_SYSTEM` 时才 source `src/memory/Kconfig` 和 `src/device/Kconfig`，印证了「全系统模式才有内存和设备」这条依赖。

#### 4.2.4 代码实践

**实践目标**：在 `Kconfig` 中找到默认 ISA 与默认运行模式，并理解「换 ISA」会连带影响哪些配置。

**操作步骤**：

1. 打开 `Kconfig`，定位到 [Kconfig:3-14](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Kconfig#L3-L14)，确认 `default ISA_riscv`。
2. 跟到 [Kconfig:16-23](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Kconfig#L16-L23)，确认默认 `!RV64`，所以字符串 `ISA` 的值是 `riscv32`。
3. 跟到 [Kconfig:50-58](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Kconfig#L50-L58)，确认默认运行模式是 `MODE_SYSTEM`。
4. （可选，待本地验证）如果你已有可用的构建环境，可以运行 `make menuconfig`，在菜单里把 Base ISA 从 riscv 改成 x86，保存后查看 `.config` 里 `CONFIG_ISA_x86=y`、`CONFIG_ISA_riscv` 是否被取消，以及 `CONFIG_ISA` 字符串是否变成 `x86`。本讲不要求实际编译，下一篇讲义会专门讲构建流程。

**需要观察的现象**：

- 默认配置下，NEMU 模拟的是一台 **riscv32** 的**全系统**机器，用**解释执行**引擎，产物是 **Linux 上的可执行文件**。
- 改 ISA 是一个「开关」，它会改变 `CONFIG_ISA` 字符串，进而（通过下一篇要讲的 `filelist.mk`）改变编译进来的 `src/isa/` 子目录。

**预期结果**：能用一句话回答「NEMU 默认模拟的是什么机器、什么模式」——答：riscv32、System mode、解释执行、native ELF。

> 如果你无法运行 `make menuconfig`，可只做「读 Kconfig 找默认值」这一步，并标注「实际编译验证待下一篇讲义」。

#### 4.2.5 小练习与答案

**练习 1**：`Kconfig` 里 `choice` 和普通 `config` 有什么区别？为什么 Base ISA 要用 `choice`？

**参考答案**：`choice` 表示一组「多选一」的互斥选项，同一时刻只能有一个被选中；普通 `config` 是独立开关，可以同时打开多个。Base ISA 必须是唯一的（一台机器不能同时是 x86 和 riscv），所以用 `choice` 来保证互斥。

**练习 2**：为什么 `src/memory/Kconfig` 和 `src/device/Kconfig` 被包在 `if MODE_SYSTEM ... endif` 里？

**参考答案**：因为只有在全系统模式下，NEMU 才需要模拟完整的内存和设备；非系统模式下没有这些概念，自然也不该出现相关配置项。用 `if MODE_SYSTEM` 把它们条件化 source，能让菜单在非系统模式下保持简洁，也防止用户配置出互相矛盾的组合。

**练习 3**：默认情况下 `CONFIG_ISA` 的值是 `riscv32` 还是 `riscv64`？依据是哪几行？

**参考答案**：是 `riscv32`。依据是 [Kconfig:20](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Kconfig#L20) —— `default "riscv32" if ISA_riscv && !RV64`，而 `RV64` 默认为 n（见 `src/isa/riscv32/Kconfig` 的 `config RV64 ... default n`），所以落到 riscv32 分支。

## 5. 综合实践

把本讲两个最小模块串起来，完成下面这个贯穿任务：

**任务**：为 NEMU 写一份「一页纸项目速览」。

要求包含三部分：

1. **定位**：用 2–3 句话说明 NEMU 是什么、为谁服务、和 AM 的关系（依据 [README.md:3-5](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/README.md#L3-L5)）。
2. **能力与边界**：画一张表，左列是 6 大子系统（monitor/CPU/memory/paging/interrupt/device），中列是「能做什么」，右列是「明确不做什么」（依据 [README.md:7-35](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/README.md#L7-L35)），并标注每个子系统对应的 `src/` 目录。
3. **默认配置**：写明默认 ISA、位数、运行模式、引擎、构建目标，并各给出一行 Kconfig 依据（依据 [Kconfig:3-69](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Kconfig#L3-L69)）。

完成后，你应该能不看源码、只凭这张速览，向别人讲清楚「NEMU 是一台什么样的虚拟机、它的代码大致放在哪里」。

## 6. 本讲小结

- NEMU 是南京大学 ICS 课程的教学用全系统模拟器，目标是「让人看懂计算机怎么工作」，而非追求性能。
- 它配套 AM 项目使用；AM 提供抽象运行环境，NEMU 是其下的一种参考实现。
- 核心能力分 6 层：监视器/SDB、CPU 执行引擎、内存、分页、中断异常、设备与 I/O；每层都能在 `src/` 下找到对应目录。
- 它刻意「不做什么」：不支持 x86 实模式与浮点、mips32 CP1 浮点、riscv 仅 IM、分页与中断均不支持保护——这些都是教学取舍。
- 顶层 `Kconfig` 用 `choice` 定义 ISA、引擎、模式、目标四组选项；默认是 riscv32 + System mode + Interpreter + Native ELF。
- 「换 ISA / 换模式」本质是改 `CONFIG_xxx` 开关，会连带改变编译进来的源文件集合（下一篇讲义详述）。

## 7. 下一步学习建议

本讲只是建立了「地图」。要真正动手，建议按下面的顺序继续：

1. **下一篇 `u1-l2-build-system-kconfig.md`**：深入 Makefile + Kconfig + `filelist.mk`，搞清楚一次 `make` 到底编译了哪些文件、产物是什么。这是所有后续实践的前提。
2. 之后再读 `u1-l3-startup-flow.md`，跟踪 `main()` → `init_monitor()` → `engine_start()` 的启动链路。
3. 如果你想先对「NEMU 跑起来长什么样」有感性认识，可以先跳到 SDB（u2 单元）看命令行交互，但编译运行的细节仍依赖 u1-l2。

> 阅读源码时，记得善用本讲给出的永久链接：它们指向当前 HEAD 的精确行号，方便你随时回到「权威出处」对照。
