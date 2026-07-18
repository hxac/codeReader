# PicoRV32 是什么：项目总览与 RISC-V 背景

## 1. 本讲目标

本讲是整个 PicoRV32 学习手册的第一篇，目标不是让你立刻读懂 Verilog，而是先建立「这个项目到底是什么、为什么存在、适合用来做什么」的整体认识。读完本讲后，你应当能够：

- 说清楚 PicoRV32 在 RISC-V 生态里的定位——它是一个**尺寸优先（size-optimized）**的 CPU 核，而不是高性能核。
- 理解它追求的核心指标是「小」和「高主频（f<sub>max</sub>）」，并明白这两个指标是**用性能（CPI）换来的**。
- 区分 RV32I / RV32E / RV32IC / RV32IM / RV32IMC 这几种指令集配置，以及 `picorv32`、`picorv32_axi`、`picorv32_wb` 这三种核心变体。
- 解释为什么 PicoRV32 的官方定位是「辅助处理器（auxiliary processor）」，而不是主处理器。
- 看懂 README 中给出的 LUT 数量、f<sub>max</sub>、CPI 三张表，并能据此判断它在某个 FPGA 上是否合适。

本讲不涉及任何 RTL 细节，主要阅读对象是 [README.md](README.md)。从下一讲开始，我们才会进入仓库结构、构建系统和源码。

---

## 2. 前置知识

本讲面向零基础读者，但有几个名词先解释清楚，后面会反复用到。

**RISC-V（发音 risk-five）** 是一种开源、开放的指令集架构（Instruction Set Architecture，ISA）。ISA 可以理解成「CPU 能听懂的机器语言规范」，它本身不是芯片，而是定义了「有哪些指令、寄存器、寻址方式」。RISC-V 的特点是模块化：以一个最基础的整数指令集 `RV32I`（32 个 32 位通用寄存器 + 约 40 条基本指令）为核心，可以按需叠加扩展。

**RV32 的常见变体**：

| 名称 | 含义 |
| ---- | ---- |
| `RV32I` | 基础整数指令集（32 位，37 条左右指令） |
| `RV32E` | 嵌入式精简版，只保留 `x0..x15` 共 16 个寄存器，面积更小 |
| `C` | Compressed 压缩指令扩展，允许用 16 位编码表示常用指令，省代码空间 |
| `M` | 乘除法扩展，新增 `MUL / MULH / DIV / REM` 等指令 |

把它们组合起来就是 `RV32IMC`（基础 + 乘除 + 压缩），这正是 PicoRV32 默认实现的 ISA。

**LUT 与 f<sub>max</sub>**：在 FPGA（现场可编程门阵列）里，逻辑由「查找表（Look-Up Table，LUT）」和「寄存器」构成。LUT 数量粗略反映了核的「面积/大小」；f<sub>max</sub> 是核能跑到的最高时钟频率。LUT 越少越省芯片面积，f<sub>max</sub> 越高越能塞进已有的高频设计里——这两个指标是 PicoRV32 的命根子。

**CPI（Cycles Per Instruction）**：执行一条指令平均需要的时钟周期数。CPI 越低性能越好。PicoRV32 的 CPI 大约是 4，这在 CPU 里属于偏高的，正是「小尺寸」的代价。

**主处理器 vs 辅助处理器**：主处理器负责跑操作系统和主要程序，性能要求高；辅助处理器（也叫协处理器/嵌入式控制核）通常只做控制、配置、低频任务，跑在已有设计旁边。理解这一区别是理解 PicoRV32 一切设计取舍的钥匙。

---

## 3. 本讲源码地图

本讲只涉及两个文件，且以 README 为主：

| 文件 | 在本讲中的作用 |
| ---- | ---- |
| [README.md](README.md) | PicoRV32 的官方说明文档，本讲几乎所有结论都来自这里。 |
| [picorv32.v](picorv32.v) | 整个 CPU 核的单一 Verilog 源文件。本讲只用它来**验证** README 里说的「三种核心变体」「内置协处理器」确实存在，并定位它们的行号。 |

提示：PicoRV32 的一个显著工程特征是——**整个 CPU 核（以及它的所有变体、协处理器）都写在同一个 `picorv32.v` 文件里**。后续讲义会反复回到这个文件，本讲先用它确认高层结构。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **项目特性与典型应用**：PicoRV32 追求什么、牺牲什么、用在哪里。
2. **核心变体与 ISA 配置**：三种总线变体 + 多种 ISA 可选项。
3. **尺寸/频率评估概览**：README 给出的实测数据怎么看。

### 4.1 项目特性与典型应用

#### 4.1.1 概念说明

PicoRV32 的自我介绍只有一句话级别的精确——它是 **A Size-Optimized RISC-V CPU**（一个尺寸优化的 RISC-V CPU）。这句话定下了它的全部性格：

- **首要目标是小**：在 Xilinx 7 系列 FPGA 上只有 750–2000 个 LUT。
- **次目标是高主频**：250–450 MHz（7 系列），在更新的 UltraScale+ 上甚至能到 700 MHz 以上。
- **性能是可牺牲的**：为了小和高频，它愿意接受较高的 CPI（≈4），也比不上那些超标量、流水的「大核」。

它的「典型应用」也因此被明确限定。README 在 [Features 一节](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L34-L46) 里直说：

> This CPU is meant to be used as **auxiliary processor** in FPGA designs and ASICs.

翻译过来就是：它是给你当**助手**用的，不是当主角。这一点非常重要，因为它解释了为什么 PicoRV32 可以接受「性能不高」——辅助处理器不需要快，它需要的是「能塞得进去、不拖累主设计的时序」。

#### 4.1.2 核心流程：PicoRV32 的设计取舍三角形

可以把 PicoRV32 的设计哲学画成一个「取舍三角」。在芯片设计里有一个常识：**面积（小）、速度（快）、性能（强）三者不可兼得**。PicoRV32 的选择是把砝码压在前两项上：

```
            面积小 (LUT 少)
                 ▲
                / \
               /   \
              / Pico \
             /  RV32  \
            /  选择区  \
           /___________\
   主频高(fmax高) ──── 性能强(CPI低)
```

它的取舍流程可以总结为：

1. **目标**：要能嵌入既有 FPGA/ASIC 设计，当辅助核。
2. **约束**：嵌入要求「不破坏主设计的时序收敛（timing closure）」。这意味着要么主频极高、能跟上主时钟域；要么主频低、留有大量时序裕量（timing slack）。
3. **手段**：用极简的微架构（单文件、简单状态机、可选精简到 RV32E）换小面积和高 f<sub>max</sub>。
4. **代价**：接受较高的 CPI（平均约 4，见 [Cycles per Instruction 节](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L330-L373)）。

换句话说，PicoRV32 不是用来和 Cortex-A、Rocket Core 比跑分，而是用来在「你已经有一个大设计、只想加一个小小的可编程控制核」时，以最小的代价塞进去。

#### 4.1.3 源码精读

**特性清单**。README 在 [README.md:37-41](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L37-L41) 列出了四条核心特性（这里转述为中文）：

- Small：在 Xilinx 7 系列上 750–2000 LUT。
- High f<sub>max</sub>：在 7 系列上 250–450 MHz。
- Selectable native memory interface or AXI4-Lite master：可选原生内存接口或 AXI4-Lite 主接口。
- Optional IRQ support / Optional Co-Processor Interface：可选中断、可选协处理器接口。

**为什么能当辅助处理器**。README [README.md:43-46](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L43-L46) 给出了关键论证（中文转述）：

> 由于 f<sub>max</sub> 高，它可以不跨时钟域地集成进大多数既有设计；当它运行在较低频率时，会有大量时序裕量，因此加入一个设计不会破坏其时序收敛。

这正是「辅助处理器」定位的技术依据。

**进一步缩小**。如果连 750 LUT 都嫌大，README [README.md:48-58](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L48-L58) 还给出了两个「再省一点」的开关：禁用 `x16..x31` 上半部分寄存器和计数器指令（变成 RV32E 核）、在单端口与双端口寄存器堆之间二选一。需要注意 README 的一个提醒：在用专用存储资源实现寄存器堆的架构（如很多 FPGA）上，这两个开关**未必**真能减小面积——这是后续讲义会展开的细节。

#### 4.1.4 代码实践（源码阅读型）

本讲是入门总览，实践以「读懂文档 + 验证仓库结构」为主。

**实践目标**：亲手确认 README 描述的特性，并把「核心卖点」翻译成自己的话。

**操作步骤**：

1. 打开 [README.md 的 Features 节](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L34-L46)，逐条对照本讲 4.1.3 的四条特性。
2. 用只读命令查看仓库到底有哪些顶层条目，确认这是一个「小而聚焦」的项目：

```bash
git ls-files | awk -F/ '{print $1}' | sort -u
```

3. 估一下核心源文件的规模，直观感受「单文件 CPU」是什么概念：

```bash
wc -l picorv32.v
```

**需要观察的现象**：

- `git ls-files` 的顶层应该出现 `picorv32.v`、`README.md`、`Makefile`、`firmware/`、`tests/`、`picosoc/`、`scripts/` 等少数条目，没有庞大的 `src/` 目录树——印证「核心逻辑集中在单文件」。
- `wc -l picorv32.v` 会显示约 2900 行左右（包含三种变体和多个协处理器模块），说明「整个 CPU 家族都在一个文件里」。

**预期结果**：你能用自己的话写出 PicoRV32 的三条核心卖点，例如：

1. **极小**：约 750–2000 LUT，可进一步精简到 RV32E。
2. **极快（主频）**：7 系列 250–450 MHz，能融入既有高频设计而不破坏时序。
3. **可裁剪**：总线接口、中断、协处理器、乘除、压缩指令全是可选项。

并解释：正因为「小 + 主频高」，它适合作为**辅助处理器**嵌入既有 FPGA/ASIC 设计——主频高就能跟主时钟域，主频低就有时序裕量，两种情况都不拖累主设计。

> 说明：本实践不产生仿真输出，属于文档阅读型实践；后续讲义（u1-l3）才会真正 `make test_ez` 把核跑起来。

#### 4.1.5 小练习与答案

**练习 1**：README 说 PicoRV32 在较低频率运行时「会有大量时序裕量（timing slack）」。请用一句话解释这对把它嵌入既有设计有什么好处。

**参考答案**：时序裕量大意味着信号在时钟周期内能更早稳定到达，几乎不会因为加入这个核而让主设计的关键路径违例，因此可以把 PicoRV32「无副作用」地加进一个已经收敛好的设计里。

**练习 2**：PicoRV32 的平均 CPI 约为 4，比许多现代 CPU 高。为什么它的作者仍然认为这是可接受的？

**参考答案**：因为 PicoRV32 的优化目标是尺寸和 f<sub>max</sub>，不是 IPC/性能。作为辅助处理器，它处理的多是低频控制任务，CPI 高带来的吞吐损失远小于「塞不进芯片」或「破坏时序」带来的工程代价。

---

### 4.2 核心变体与 ISA 配置

#### 4.2.1 概念说明

「PicoRV32」其实不是一个核，而是**一族核**。理解这一族要从两个维度入手：

**维度一：指令集配置（ISA）。** README 开篇 [README.md:6-8](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L6-L8) 写明，它实现 RV32IMC，并可配置为 RV32E、RV32I、RV32IC、RV32IM 或 RV32IMC。这些是通过 Verilog `parameter`（参数）在**综合时**决定的，不是运行时切换的。也就是说，同一份 `picorv32.v`，设不同的参数就综合出不同 ISA 的核。

**维度二：总线接口变体。** 同一个 CPU 核，对外提供三种「插头」：

| 变体 | 接口 | 适用场景 |
| ---- | ---- | ---- |
| `picorv32` | 原生简单 valid-ready 内存接口 | 简单环境，上手最容易 |
| `picorv32_axi` | AXI4-Lite 主接口 | 已有 AXI 总线的系统 |
| `picorv32_wb` | Wishbone 主接口 | 已有 Wishbone 总线的系统 |

此外还有一个独立的 `picorv32_axi_adapter`，专门负责把「原生接口」翻译成「AXI4-Lite」。它的用途见 [README.md:66-70](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L66-L70)：当你想做一个包含多个 PicoRV32 核 + 本地 RAM/ROM/外设的子系统，内部用简单的原生接口互连，对外统一用 AXI4 通信时，就用它来桥接。

**可选特性**（仍然通过参数开关）：中断（IRQ）、协处理器接口（PCPI）。其中 PCPI 自带三个内置协处理器实现 M 扩展的乘除法。

#### 4.2.2 核心流程：如何为项目挑选一个 PicoRV32

当你决定用 PicoRV32 时，实际上是在做一个三步选择流程：

```
[第 1 步] 选 ISA 基础
   RV32I（标准） / RV32E（16 寄存器更小） / +C（压缩） / +M（乘除）
        │
        ▼
[第 2 步] 选总线变体
   原生 picorv32 / picorv32_axi / picorv32_wb
   （自定义子系统时再加 picorv32_axi_adapter）
        │
        ▼
[第 3 步] 选可选特性
   ENABLE_IRQ（中断） / ENABLE_PCPI（协处理器） / ENABLE_MUL/DIV / COMPRESSED_ISA ...
        │
        ▼
   综合出唯一的、定制的 CPU 核
```

关键点：这三步都是**综合时（compile-time）**用 Verilog 参数定的，不是运行时切换。README 的「Verilog Module Parameters」一节（[README.md:145-327](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L145-L327)）列出了 20 多个这样的参数，本讲只需建立「可配置」的印象，具体参数留到 u3-l1 详讲。

#### 4.2.3 源码精读

**三种变体确实都在同一个文件里。** README 在 [README.md:89-103](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L89-L103) 用一张表列出了 `picorv32.v` 内含的全部模块。我们直接到源码里核实这些 `module` 声明：

- 基础核：[picorv32.v:62](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L62) —— `module picorv32 #(...`，这是带原生内存接口的标准 CPU。
- AXI 变体：[picorv32.v:2517](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2517) —— `module picorv32_axi #(...`。
- AXI 适配器：[picorv32.v:2731](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2731) —— `module picorv32_axi_adapter #(...`。
- Wishbone 变体：[picorv32.v:2815](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2815) —— `module picorv32_wb #(...`。

**可配置参数的入口**。打开 `module picorv32` 就能看到它接受一长串 `parameter`，这正是「ISA/特性可裁剪」的实现位置：[picorv32.v:62-88](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L62-L88)。例如其中的：

- `ENABLE_REGS_16_31 = 1`（[picorv32.v:65](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L65)）——设为 0 即裁掉 `x16..x31`，变成 RV32E 风格。
- `COMPRESSED_ISA = 0`（[picorv32.v:72](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L72)）——设为 1 开启 C 扩展。
- `ENABLE_IRQ = 0`（[picorv32.v:79](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L79)）、`ENABLE_PCPI = 0`（[picorv32.v:75](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L75)）——中断与协处理器的总开关。

**内置协处理器**。README [README.md:99-101](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L99-L101) 提到三个内置 PCPI 核，它们也都在同一文件中：`picorv32_pcpi_mul`（[picorv32.v:2197](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2197)）、`picorv32_pcpi_fast_mul`（[picorv32.v:2318](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2318)）、`picorv32_pcpi_div`（[picorv32.v:2420](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L2420)）。本讲只要知道它们存在即可，原理留到 u6-l1。

#### 4.2.4 代码实践（源码阅读型）

**实践目标**：用一条命令亲手列出 `picorv32.v` 里的全部 `module`，验证 README 表格没有夸大。

**操作步骤**：

```bash
grep -n '^module' picorv32.v
```

**需要观察的现象**：输出应包含 8 行左右的 `module` 声明，分别是 `picorv32`、`picorv32_regs`、`picorv32_pcpi_mul`、`picorv32_pcpi_fast_mul`、`picorv32_pcpi_div`、`picorv32_axi`、`picorv32_axi_adapter`、`picorv32_wb`，且行号与本讲 4.2.3 给出的一致（或非常接近，行号可能因后续提交略有漂移）。

**预期结果**：你会得到一张「行号 → 模块名」对照表，证明三种总线变体和三个协处理器确实集中在同一个文件里。这正是 PicoRV32 「拷贝一个文件就能用」的工程优势来源——README [README.md:103](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L103) 原话就是「Simply copy this file into your project」。

> 说明：本实践只读不写，不修改任何源码，行号若与文中略有出入以本地 `grep` 输出为准。

#### 4.2.5 小练习与答案

**练习 1**：如果你的系统已经用了 AXI 总线，你会选 `picorv32`、`picorv32_axi` 还是 `picorv32_wb`？为什么？

**参考答案**：选 `picorv32_axi`。它直接提供 AXI4-Lite 主接口，能无缝挂到现有 AXI 互连上，无需自己写桥接逻辑。`picorv32_axi_adapter` 主要用于「自定义子系统内部用原生接口、对外才转 AXI」的场景。

**练习 2**：`ENABLE_REGS_16_31`、`COMPRESSED_ISA`、`ENABLE_IRQ` 这三个参数分别对应 RV32 的哪一项裁剪/扩展？

**参考答案**：`ENABLE_REGS_16_31=0` 对应 RV32E（精简到 16 寄存器）；`COMPRESSED_ISA=1` 对应 C 扩展（16 位压缩指令）；`ENABLE_IRQ=1` 对应中断功能（注意：IRQ 不属于标准 RV32I/E/C/M 命名，而是 PicoRV32 自定义的可选特性）。

---

### 4.3 尺寸/频率评估概览

#### 4.3.1 概念说明

光说「小」「快」是主观的，PicoRV32 在 README 末尾的 [Evaluation 一节](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L691-L740) 给出了在 Xilinx 7 系列 FPGA 上的实测数据，分两部分：

- **Timing（时序/频率）**：把 `picorv32_axi` + `TWO_CYCLE_ALU` 在多款器件上做布局布线，用「二分查找最短时钟周期」的方法，找出还能满足时序（meet timing）的最高频率。
- **Utilization（资源占用）**：在面积优化综合下，比较 small / regular / large 三种配置各占多少 Slice LUT 与寄存器。

这三个配置的含义见 [README.md:725-732](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L725-L732)：

- **small**：关掉计数器、关掉两阶段移位、外部锁存 `mem_rdata`、不捕获非对齐访问和非法指令——能关的都关。
- **regular**：默认配置。
- **large**：开启 PCPI、IRQ、MUL、DIV、BARREL_SHIFTER、COMPRESSED_ISA——能开的都开。

#### 4.3.2 核心流程：这些数字是怎么测出来的

理解评估方法本身，比记住具体数字更重要。README 在 [README.md:696-703](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L696-L703) 和 [README.md:720-734](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L720-L734) 说明了流程，对应 `scripts/vivado/` 里的 `make table.txt` 与 `make area`：

```
[频率评估 make table.txt]
  综合(Synthesis) → 布局布线(P&R)
       → 对目标时钟周期做二分查找
       → 找到「还能 meet timing」的最短周期
       → 换算成 fmax 列入表格

[面积评估 make area]
  对 small / regular / large 三套参数分别做面积优化综合
       → 统计 Slice LUT / LUT as Memory / Slice Registers
       → 列入对比表格
```

一个直觉公式：最高频率与最短时钟周期互为倒数，

\[
f_{\max} \;=\; \frac{1}{T_{\min}}
\]

例如 Kintex UltraScale+ 的最短周期 1.3 ns 对应约 769 MHz，与表格一致（[README.md:716](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L716)）。

#### 4.3.3 源码精读

**资源占用表**。这是最直观的「小」的证据，来自 [README.md:736-740](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L736-L740)：

| 核配置 | Slice LUTs | LUTs as Memory | Slice Registers |
| ------ | ---------: | -------------: | --------------: |
| PicoRV32 (small) | 761 | 48 | 442 |
| PicoRV32 (regular) | 917 | 48 | 583 |
| PicoRV32 (large) | 2019 | 88 | 1085 |

读法：small 配置只要 **761 个 LUT**，这就是 README 开篇「750–2000 LUT」下界的来源；large 把所有特性打开也才约 2000 LUT，对应上界。regular 默认配置不到 1000 LUT，对绝大多数 FPGA 而言都是「几乎免费」的。

**性能数据（CPI）**。作为「小」的代价，README [README.md:339-354](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L339-L354) 给出各类指令的 CPI，例如 ALU 立即数指令 3 周期、内存 load 5 周期、移位 4–14 周期，平均约 4。而开 `ENABLE_FAST_MUL + ENABLE_DIV + BARREL_SHIFTER` 后跑 Dhrystone 基准测试的结果是 **0.516 DMIPS/MHz**（[README.md:368](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L368)）。这个数字再次印证：PicoRV32 是尺寸/频率优先，性能只是「够用」。

> 小贴士：DMIPS/MHz 是「每兆赫兹每秒百万 Dhrystone 指令」的归一化性能指标，方便在不同主频的核之间横向比较。0.516 属于偏低的水平，符合「辅助核」定位。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：建立「配置 → 资源」的直觉，能根据表格为 hypothetical 项目选配置。

**操作步骤**：

1. 打开 [Evaluation 一节](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/README.md#L691-L740)，阅读 Timing 表与 Utilization 表。
2. 做一个纸上推演：假设你的 FPGA 还剩约 1000 个 LUT，你需要一个能跑 C 压缩指令、带中断、带乘法的小控制核。对照 large 配置（2019 LUT）判断是否放得下；如果放不下，回退到 regular（917 LUT）会牺牲哪些特性？
3. （可选）核验频率换算：任取 Timing 表一行，用 \(\,f_{\max}=1/T_{\min}\,\) 验证周期与频率一致。

**需要观察的现象**：

- small 与 large 的 LUT 差距接近 **2.6 倍**（761 → 2019），说明「能开的都开」会显著变大。
- 寄存器数量（442 → 1085）同样翻倍以上，主要来自启用的功能单元。

**预期结果**：你能用一句话总结——「PicoRV32 的面积几乎完全由你开了多少可选特性决定」。这正好呼应 4.2 节：ISA/特性裁剪不仅是功能选择，更是面积选择。

> 说明：本实践为纸上分析，无需运行综合工具；若想真正复现数字，需要安装 Xilinx Vivado 并进入 `scripts/vivado/` 运行 `make area`，属于「待本地验证」的高阶内容。

#### 4.3.5 小练习与答案

**练习 1**：small 配置为什么比 regular 少了约 150 个 LUT？它牺牲了什么？

**参考答案**：small 关掉了计数器指令（`ENABLE_COUNTERS=0`）、两阶段移位（`TWO_STAGE_SHIFT=0`）、对非对齐访问和非法指令的捕获（`CATCH_MISALIGN=0`、`CATCH_ILLINSN=0`），并假定外部锁存 `mem_rdata`。这些省下来的检测/运算电路就是那约 150 个 LUT 的来源，代价是失去部分异常捕获能力和移位速度。

**练习 2**：Dhrystone 结果 0.516 DMIPS/MHz 意味着 PicoRV32 性能很差、不能用吗？请结合它的定位反驳。

**参考答案**：不能这样下结论。DMIPS/MHz 衡量的是「性能密度」，而 PicoRV32 优化的不是性能，是面积和 f<sub>max</sub>。作为辅助处理器，它处理的是低频控制任务，0.516 DMIPS/MHz 配合高达数百 MHz 的主频，对于配置寄存器、轮询外设、实现状态机等任务已经绑绑有余。评价一个核要看它是否达成了设计目标，而不是单看某一项指标。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个「为 PicoRV32 写一段电梯演讲」的小任务：

**任务**：假设你要向团队提议在一个已有的 FPGA 设计中加入一个 PicoRV32 作为辅助控制核，请准备一份不超过 200 字的中文说明，必须覆盖以下三点，并附上 README 中的依据：

1. **它是什么**：一句话定位（尺寸优化的 RISC-V 核，可配 RV32I/E/C/M）。
2. **为什么选它**：结合 4.1 的取舍三角和 4.3 的数据，说明它够小（≤1000 LUT，regular 配置）、主频够高（能跟主时钟域、不破坏时序）、可裁剪。
3. **怎么集成**：结合 4.2 说明你会选哪个总线变体（原生 / AXI / Wishbone），以及需要开哪些可选特性。

**建议产出格式**（示例框架，请用你自己的话填充）：

> 我们计划加入一个 PicoRV32 作为辅助控制核。它是……（定位）。选择它的理由是……（小、快、可裁剪，引用 LUT/fmax 数据）。集成方案是采用 `picorv32_axi` 变体挂到现有 AXI 总线，开启 IRQ 与……等特性。

完成后，对照本讲小结检查你是否真正理解了 PicoRV32 的定位与取舍。

---

## 6. 本讲小结

- PicoRV32 是一个**尺寸优先**的 RISC-V CPU，核心目标是「小」和「高 f<sub>max</sub>」，明确以「辅助处理器」为定位。
- 它的设计取舍是：用较高的 CPI（≈4）换取小面积（750–2000 LUT）和高主频（7 系列 250–450 MHz）。
- 它实现 RV32IMC，并可配置为 RV32E/I/IC/IM/IMC；ISA 与特性全部通过 Verilog `parameter` 在综合时决定。
- 它有三种总线变体——`picorv32`（原生接口）、`picorv32_axi`（AXI4-Lite）、`picorv32_wb`（Wishbone），外加 `picorv32_axi_adapter` 桥接器。
- 三种变体和全部内置协处理器都集中在**同一个 `picorv32.v`** 文件里，拷贝一个文件即可使用。
- README 的 Evaluation 表显示，面积几乎完全由「开了多少可选特性」决定：small 761 LUT、regular 917 LUT、large 2019 LUT。

---

## 7. 下一步学习建议

本讲只读了文档，还没有真正碰过代码的运行。建议按手册顺序继续：

- **下一讲 u1-l2《仓库结构与构建系统》**：先弄清 `Makefile` 的 `test_*` 目标族、目录划分和 FuseSoC 的 `.core` 打包，建立「项目怎么组织、怎么构建」的整体观。
- **再下一讲 u1-l3《跑起来：最小测试台 testbench_ez》**：这是你第一次**真正运行** PicoRV32（`make test_ez`），不依赖工具链，直观感受时钟、复位与取指。
- 如果你想先睹为快源码全景，可以打开 [picorv32.v:62](https://github.com/YosysHQ/picorv32/blob/87c89acc18994c8cf9a2311e871818e87d304568/picorv32.v#L62) 浏览 `module picorv32` 的参数与端口列表，但具体讲解会从 u3 单元开始。
