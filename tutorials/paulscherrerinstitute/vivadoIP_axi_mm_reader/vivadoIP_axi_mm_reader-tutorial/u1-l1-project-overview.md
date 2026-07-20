# 项目概览：vivadoIP_axi_mm_reader 是什么

## 1. 本讲目标

本讲是整本学习手册的第一篇，目标是让读者在「不写一行代码、不打开 Vivado」的前提下，建立起对这个项目的整体认知。读完本讲，你应当能够：

- 用一句话说清 **vivadoIP_axi_mm_reader** 这个 IP 核是做什么的、解决什么问题；
- 说出它的两种**输出方式**（AXIS 与 AXIMM）以及它们的差别；
- 列出它在 Vivado 配置界面（GUI）里的几个关键**可配置项**；
- 说明它的**许可证**（PSI HDL Library License / LGPL）和它所依赖的**外部库**（psi_common、PsiSim、PsiIpPackage、psi_tb）；
- 知道后续每一篇讲义大概会讲什么，建立继续学习的信心。

本讲只做「定位与背景」，不深入 RTL 细节。具体的寄存器映射、状态机、AXI 主从机实现会在后续讲义中逐步展开。

## 2. 前置知识

如果你完全没接触过 FPGA，下面几个名词先了解一下即可，本讲不会用到很深的细节。

- **FPGA**：一种可以通过编程改变内部电路的芯片。开发者用硬件描述语言（HDL）写代码，再由工具「综合」成真实的电路。
- **IP 核（IP-Core）**：Intellectual Property Core，可以理解为「FPGA 里的一个可复用功能模块」，就像软件里的库。本项目的 `vivadoIP_axi_mm_reader` 就是一个 IP 核，可以被拖进 Vivado 的 Block Design 里使用。
- **VHDL**：本项目使用的硬件描述语言，源码文件后缀是 `.vhd`。
- **AXI4**：ARM 设计、Xilinx/AMD FPGA 广泛使用的总线协议。可以把它想象成芯片内部各模块之间的「高速公路」，数据沿着它传输。本项目里：
  - **AXI4 Memory-Mapped（AXI4-MM）**：带地址的总线，像读写内存一样读写某个地址的「寄存器」。
  - **AXI-Stream（AXI-S）**：不带地址、只管一路往外送数据的流式接口，像流水线传送带。
- **寄存器（Register）**：一个 32 位（本项目里）的小存储单元，软件或硬件通过它读写控制信息和状态。
- **FIFO**：先进先出队列，常用来在「产生数据的快模块」和「消费数据的慢模块」之间做缓冲，避免数据丢失。

> 没有硬件基础也没关系，本讲重点在「它解决什么问题」，把名词当成黑盒理解即可。

## 3. 本讲源码地图

本讲涉及的文件都是文档与元信息，不涉及 HDL 逻辑实现：

| 文件 | 作用 |
|------|------|
| `README.md` | 项目主页说明：维护者、许可证、**功能描述**、外部依赖、仿真运行方式 |
| `doc/Documentation.md` | 官方详细文档：功能概述、典型用途、接口、**配置 GUI 参数**、寄存器表、架构图 |
| `Changelog.md` | 版本变更记录（目前只有 1.0.0 首次发布） |
| `License.txt` | 许可证全文：PSI HDL Library License（基于 LGPL） |

其中最重要的是 `README.md` 和 `doc/Documentation.md`，它们共同回答了「这个项目是什么」。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **README 项目定位**：从主页一句话功能描述切入。
2. **Documentation 概述**：展开功能、典型用途、接口、可配置项、两种输出方式。
3. **依赖列表**：搞清楚要跑起来需要哪些外部库。

### 4.1 README 项目定位

#### 4.1.1 概念说明

打开任何一个开源项目，第一件事通常是读 `README.md`。它通常会告诉你三件事：**这个项目是谁做的、做什么的、怎么跑**。

对 `vivadoIP_axi_mm_reader` 来说，`README.md` 里的 **Description** 一段就是整个项目的「一句话定位」。这一句话是理解全部后续讲义的钥匙，所以本模块专门把它拎出来讲透。

#### 4.1.2 核心流程

从 README 的描述可以提炼出这个 IP 核的工作主线：

1. **触发**：周期性（定时）或在某个事件到来时启动一次「读周期」。
2. **读取**：经 AXI4 总线，从一个预先配好的地址表里，把一批 32 位寄存器的值依次读出来。
3. **输出**：把读到的值交给软件——要么通过一个 FIFO（软件排队取走），要么通过 AXI-Stream 接口（直接流出去）。

用一句话概括：**它是一个「自动批量读寄存器，再交给软件」的搬运工**。

#### 4.1.3 源码精读

**项目维护者与许可证（一句话级）**：

[README.md:3-7](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/README.md#L3-L7) 给出维护者是 PSI 的 Oliver Bründler，并说明许可证是 PSI HDL Library License（即 LGPL 加上针对固件/FPGA 的一些额外例外条款）。

**功能描述（本模块核心）**：

[README.md:42-43](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/README.md#L42-L43) 原文是：

> This IP-core reads a number of registers automatically (periodic or upon an event) and makes them available to SW through a FIFO or transmits them through an AXI-Stream interface.

这句话同时交代了：① 自动读多个寄存器；② 触发方式是周期或事件；③ 输出方式是 FIFO（给软件）或 AXI-Stream。

**仿真运行方式**：

[README.md:45-57](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/README.md#L45-L57) 说明如何在 Modelsim 或 GHDL 下跑回归仿真（具体流程在第 u1-l3 讲详解，这里只需知道入口是 `sim/run.tcl` 和 `sim/runGhdl.tcl`）。

#### 4.1.4 代码实践

这是一个**源码阅读型实践**，目标是把「一句话定位」内化成自己的理解。

1. **实践目标**：不看讲义，能复述这个 IP 核是做什么的。
2. **操作步骤**：
   - 打开 `README.md`，只读第 42–43 行（Description）。
   - 用中文把这句话翻译并改写成「面向完全外行」的一句话。
3. **需要观察的现象**：注意原文里的两组关键词——`(periodic or upon an event)` 和 `(FIFO or AXI-Stream)`，它们分别对应「触发方式」和「输出方式」两个维度。
4. **预期结果**：写出类似「这个 IP 核能定时或按事件触发，自动通过 AXI4 把一批寄存器读出来，再经 FIFO 或 AXI-Stream 交给软件」的句子。
5. 如果想确认自己理解对不对，可以继续读 `doc/Documentation.md` 的 Overview 对照。

#### 4.1.5 小练习与答案

**练习 1**：README 的 Description 里提到了几种「输出数据给软件」的方式？分别是什么？

**答案**：两种。一是通过 FIFO（软件从 FIFO 里取），二是通过 AXI-Stream 接口。

**练习 2**：README 里触发读取的方式有哪两种？

**答案**：周期性（periodic）自动触发，或在某个事件（upon an event）到来时触发。

### 4.2 Documentation 概述

#### 4.2.1 概念说明

`README.md` 是「电梯演讲」，`doc/Documentation.md` 则是「完整说明书」。它把 README 里那句话展开成了：典型用途、对外接口、可配置参数、寄存器表和架构图。本模块带你看懂这份说明书的「骨架」，细节（寄存器位含义、状态机）留给后续讲义。

#### 4.2.2 核心流程

Documentation 描述的完整数据通路可以粗略画成：

```
软件配置(s00_axi) ──► 地址表(RegTable)
                            │
  触发(Trig)/超时(Timeout) ──┤
                            ▼
                  核心逻辑发起读周期
                            │
                  m00_axi 按表逐个读寄存器
                            │
                            ▼
                  读回值进入内部 FIFO 缓冲
                            │
                ┌───────────┴───────────┐
                ▼                       ▼
        AXI-Stream 输出(m_axis)   软件从 FIFO 读取(AXIMM)
```

两种输出方式（AXIS / AXIMM）共享前半段「读寄存器 + 进 FIFO」的逻辑，只在「如何把数据交出去」这一步分叉。

#### 4.2.3 源码精读

**功能概述**：

[doc/Documentation.md:3-7](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/doc/Documentation.md#L3-L7) 说明：本 IP 核周期性或按触发事件经 AXI-4 读取一批 32 位寄存器，读回的值要么由软件从内部 FIFO 读取，要么经 AXI-Stream 接口送出，二者在 GUI 里可选；待读的寄存器地址存放在一个小 RAM（RegTable）里，每个读周期遍历整张表。

**两个典型用途（理解「为什么要造这个 IP」的关键）**：

[doc/Documentation.md:9-11](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/doc/Documentation.md#L9-L11) 给出两种意图：

- 把「只能通过寄存器访问」的值（例如 System-Monitor 的温度/电压）转换成 AXI-Stream；
- 在「精确时间点」抓取状态寄存器的值——因为软件读寄存器通常有较大抖动（jitter），而硬件定时读取抖动极小。

**对外接口**：

[doc/Documentation.md:24-33](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/doc/Documentation.md#L24-L33) 列出接口要点：

- 所有接口共用一个时钟和复位；如需跨时钟域，要在外部加时钟穿越组件。
- `Trig` 来一个单拍脉冲就启动一次读周期；读周期进行中的触发会被忽略；必须用「恰好高一拍」的脉冲。
- `s00_axi`（AXI 从机）：软件用来**配置**本 IP。
- `m00_axi`（AXI 主机）：本 IP 用来**读取**目标寄存器。
- `DoneIrq`：读周期完成时输出一个单拍脉冲（中断）。
- `m_axis` 是否存在取决于配置：AXIS 模式下存在（送 AXI-Stream），AXIMM 模式下不存在（改由软件读 FIFO）。

**可配置参数（GUI）**：

[doc/Documentation.md:42-54](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/doc/Documentation.md#L42-L54) 列出 GUI 参数，整理成表：

| 参数 | 含义 |
|------|------|
| s00_axi 地址宽度 | 至少为 \( \lceil \log_2(\text{MaxRegisters} \times 4 + 32) \rceil \) |
| 时钟频率（Hz） | 用于计算超时 |
| 超时（µs） | 使能后若一直无触发，经过该时间自动启动一次读周期；不接触发即可实现周期读取 |
| 每周期最多读取寄存器数 | 配置 ROM（RegTable）的条目数；运行时可设更小 |
| 缓冲的读周期数 | 内部 FIFO 能缓存多少个完整读周期，缓冲不足会丢数据 |
| 输出类型 | **AXIS**（AXI-Stream 送出）或 **AXIMM**（软件经寄存器空间里的 FIFO 读取） |

> 关于地址宽度公式里的 `+32`：配置寄存器区占据 `0x00`–`0x1C` 共 \(8 \times 4 = 32\) 字节，地址表 `Addr[]` 从 `0x20` 开始，所以总地址空间是「配置区 32 字节 + 地址表 `MaxRegisters×4` 字节」，地址宽度必须能覆盖这个范围。这个细节会在 u2-l2（寄存器映射）讲透。

**两种输出方式**：

[doc/Documentation.md:52-54](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/doc/Documentation.md#L52-L54) 明确：

- **AXIS**：读回值经 AXI-S 接口送出（此时有 `m_axis` 端口）。
- **AXIMM**：读回值放在一个映射到寄存器空间的 FIFO 里，由软件读取（此时无 `m_axis` 端口）。

**寄存器表（先建立印象，细节后续讲）**：

[doc/Documentation.md:70-80](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/doc/Documentation.md#L70-L80) 列出寄存器：`Ena`(0x00)、`RegCnt`(0x04)、`RdData`(0x08,仅 AXIMM)、`RdLast`(0x0C,仅 AXIMM)、`Level`(0x10)、`Addr[0..]`(0x20 起)。本讲只要知道「有这么一张表」即可。

#### 4.2.4 代码实践

1. **实践目标**：把「可配置项」和「两种输出方式」整理成自己的速查表。
2. **操作步骤**：
   - 打开 `doc/Documentation.md`，阅读第 42–54 行的 GUI 参数清单。
   - 做一张三列表格：**参数名 / 作用 / 取值或约束**。
   - 单独列出 AXIS 与 AXIMM 的差别（端口是否出现、谁取数据）。
3. **需要观察的现象**：注意 `RdData`/`RdLast` 两个寄存器标注了「Only present for Output type = AXIMM」——这印证了「输出方式决定端口和寄存器是否存在」。
4. **预期结果**：得到一张能随时翻阅的速查表，并能口述「AXIS 有 m_axis 端口、AXIMM 没有」。
5. 想加深印象可以看一下 Documentation 里 `Architecture` 一节附带的架构图（[doc/Documentation.md:84-88](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/doc/Documentation.md#L84-L88)）。

#### 4.2.5 小练习与答案

**练习 1**：如果不接任何外部触发信号，但希望 IP 核每隔固定时间读一次寄存器，应该怎么配置？

**答案**：使能 IP 核，并设置「超时（Timeout in µs）」参数；超时到来会自动启动一次读周期，循环即实现周期读取。

**练习 2**：为什么 Documentation 说软件读寄存器「有较大抖动」，而本 IP 核适合在精确时间点抓状态？

**答案**：软件读取受操作系统调度、中断、总线竞争等影响，发起时刻不确定（抖动大）；而硬件 IP 核由时钟驱动、定时触发，读取时刻精确、抖动极小。

**练习 3**：AXIS 和 AXIMM 两种模式下，`m_axis` 端口的去留有什么不同？

**答案**：AXIS 模式下 `m_axis` 端口存在（数据经 AXI-Stream 送出）；AXIMM 模式下 `m_axis` 端口不存在（数据进入映射到寄存器空间的 FIFO，由软件读取）。

### 4.3 依赖列表

#### 4.3.1 概念说明

PSI 的 FPGA 项目不是孤立的，它们共享一套公共库（统称 psi_* 系列）。要编译、仿真、打包这个 IP 核，需要先把这些依赖准备好。README 里有一段被特殊标记、可被脚本自动解析的「Dependencies」区块，本模块就是读懂它。

#### 4.3.2 核心流程

依赖按用途分成三类，其中标注「for development only」的是只在**开发/仿真/打包**阶段需要，最终用户在 Vivado 里使用打包好的 IP 时不一定需要：

```
TCL 工具：  PsiSim(仿真)        PsiIpPackage(打包)        ← 仅开发
VHDL 库：   psi_common(运行必需) psi_tb(测试台)            ← psi_tb 仅开发
本项目：    vivadoIP_axi_mm_reader
```

此外，README 还提供了一个一站式方案：`psi_fpga_all` 仓库把所有 FPGA 相关仓库以正确目录结构作为子模块包含进去；也可以用 `scripts/dependencies.py` 脚本自动拉取依赖。

#### 4.3.3 源码精读

**依赖区块（脚本可解析）**：

[README.md:17-32](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/README.md#L17-L32) 列出全部依赖。注意开头的注释 `<!-- DO NOT CHANGE FORMAT: this section is parsed to resolve dependencies -->`（[README.md:15](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/README.md#L15)）——这一段格式是固定的，因为脚本会解析它。整理成表：

| 类别 | 依赖 | 版本要求 | 是否仅开发用 |
|------|------|---------|--------------|
| TCL | PsiSim | ≥ 2.4.0 | 是（仿真） |
| TCL | PsiIpPackage | 2.2.0 | 是（打包） |
| VHDL | psi_common | ≥ 2.10.0 | **否（运行必需）** |
| VHDL | psi_tb | ≥ 2.5.0 | 是（测试台） |

> 关键点：`psi_common` 是这个 IP 核运行时真正依赖的 VHDL 公共库（提供 AXI 主/从机、FIFO、双口 RAM 等基础组件），其余三个（PsiSim、PsiIpPackage、psi_tb）只在开发阶段需要。

**两种获取依赖的方式**：

[README.md:19-21](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/README.md#L19-L21) 说明可以直接克隆 `psi_fpga_all`（包含全部依赖、目录结构已就绪）；[README.md:34-40](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/README.md#L34-L40) 说明也可用 `python dependencies.py -help` 查看自动拉取脚本（需先安装 PsiFpgaLibDependencies 包）。

#### 4.3.4 代码实践

1. **实践目标**：分清「运行必需」和「仅开发用」的依赖。
2. **操作步骤**：
   - 打开 `README.md` 第 17–32 行。
   - 把四个依赖分别标注为「运行必需」或「仅开发用」。
   - 想象两种场景：①「我只是想在 Vivado 里用这个 IP」；②「我想改它的代码并跑仿真」。分别列出各自需要的依赖。
3. **需要观察的现象**：注意「for development only」这个短语出现在 PsiSim、PsiIpPackage、psi_tb 三者后面，唯独 psi_common 没有——这正说明它是运行必需。
4. **预期结果**：得出「场景①只需 psi_common（打包时已含），场景②需要全部四个」的结论。
5. 待本地验证：若本地有 `psi_fpga_all`，可检查其子模块是否包含上述四个仓库。

#### 4.3.5 小练习与答案

**练习 1**：四个依赖里，哪一个是「运行 IP 核时必需、不能省」的？

**答案**：`psi_common`（≥ 2.10.0）。它是 IP 核综合实现所依赖的 VHDL 公共库；其余三个只用于开发阶段的仿真与打包。

**练习 2**：`psi_fpga_all` 仓库的作用是什么？

**答案**：它把 PSI 所有 FPGA 相关仓库以正确的目录结构作为子模块集合在一起，克隆它即可一次性获得全部依赖，免去手动维护目录结构。

## 5. 综合实践

本讲的综合实践是一道**纯阅读理解题**，对应大纲里的实践任务。完成它，你就真正建立了对这个项目的整体认知。

**任务**：阅读 `README.md` 与 `doc/Documentation.md`，用自己的话写一份「项目速览」，要求包含以下四部分：

1. **它解决什么问题**：用 2–3 句中文说清，并至少举一个典型应用场景（提示：System-Monitor、低抖动状态采集）。
2. **可配置项清单**：列出 GUI 里的可配置参数及其作用（可做成表格）。
3. **两种输出方式**：对比 AXIS 与 AXIMM 的差别——谁取数据、`m_axis` 端口是否存在、相关寄存器（`RdData`/`RdLast`）是否出现。
4. **外部依赖**：分别列出「跑仿真」和「打包 IP」需要的外部库，并指出哪个是运行时必需。

**参考做法骨架（请先自己写，再对照）**：

```text
[1] 解决问题：定时/按事件经 AXI4 自动读一批 32 位寄存器，再交给软件。
    典型场景：把 System-Monitor 温度/电压转成 AXI-Stream；
              在精确时间点抓取状态寄存器（硬件读取抖动远小于软件）。

[2] 可配置项：s00_axi 地址宽度、时钟频率、超时(µs)、每周期最多读寄存器数、
              缓冲读周期数、输出类型(AXIS/AXIMM)。

[3] 两种输出：
    - AXIS：数据经 AXI-Stream(m_axis) 送出，m_axis 端口存在。
    - AXIMM：数据进入映射到寄存器空间的 FIFO，由软件读 RdData/RdLast，
             m_axis 端口不存在，RdData/RdLast 仅此模式存在。

[4] 依赖：
    - 跑仿真：PsiSim、psi_common、psi_tb。
    - 打包 IP：PsiIpPackage、psi_common。
    - 运行必需：psi_common。
```

> 提示：写完后回头检查——如果有人只读你这份速览就能回答「这 IP 是干嘛的、怎么配置、怎么集成」，那本讲的目标就达成了。

## 6. 本讲小结

- **定位**：`vivadoIP_axi_mm_reader` 是一个 AXI4 IP 核，能周期性或按事件自动读取一批 32 位寄存器，再交给软件。
- **两种输出**：AXIS（AXI-Stream 直出，有 `m_axis` 端口）与 AXIMM（软件从映射到寄存器空间的 FIFO 读取，无 `m_axis` 端口）。
- **典型用途**：把只能寄存器访问的量（如 System-Monitor 温度/电压）转成 AXI-Stream；在精确时间点低抖动抓取状态寄存器。
- **触发方式**：`Trig` 单拍脉冲触发，或使能后靠超时自动触发（可实现周期读取）。
- **许可证**：PSI HDL Library License（LGPL 加固件场景例外条款），由 PSI 的 Oliver Bründler 维护。
- **依赖**：运行必需 `psi_common`；开发用 `PsiSim`、`PsiIpPackage`、`psi_tb`；可用 `psi_fpga_all` 或 `scripts/dependencies.py` 获取。

## 7. 下一步学习建议

本讲只建立了「项目是什么」的认知。接下来建议：

- **u1-l2（仓库目录结构速览）**：动手把仓库目录画成树，定位核心 RTL（`hdl/`）、测试台（`tb/`）、仿真脚本（`sim/`）、C 驱动（`drivers/`），为后续读源码做准备。
- **u1-l3（如何运行仿真）**：搞清 PsiSim 的 `config.tcl` / `run.tcl`，亲手跑一次回归仿真。
- 暂时不想看代码的读者，可以先把本讲的「速览」写扎实，再进入第二单元（进阶）自顶向下拆解架构。
