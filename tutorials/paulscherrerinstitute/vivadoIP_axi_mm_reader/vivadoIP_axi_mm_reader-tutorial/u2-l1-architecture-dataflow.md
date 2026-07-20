# 整体架构与数据流

## 1. 本讲目标

本讲是进阶单元的第一讲。在前一单元你已经知道 `vivadoIP_axi_mm_reader` 是「周期性或触发式地经 AXI4 读一批寄存器再输出」的 IP 核，但还不知道它**内部是怎么把这件事做完的**。本讲要帮你建立一张「自顶向下」的架构心智地图，学完后你应当能够：

- 说出该 IP 核对外暴露的全部接口（`s00_axi`、`m00_axi`、`m_axis`、`Trig`、`DoneIrq`）以及各自的职责。
- 用一句话描述一次完整「读周期」的数据通路：配置表 → 核心 FSM → AXI 主机 → FIFO → 输出。
- 区分 **AXIS** 与 **AXIMM** 两种输出模式在数据流上的差异，并解释为什么默认打包出来的 IP **没有** `m_axis` 端口。

> 本讲只画「大图」，故意不展开 FSM 状态机的细节、寄存器位字段、握手时序——这些分别在 u2-l3、u2-l2、u2-l6 里讲。本讲负责把后续讲义串起来。

## 2. 前置知识

### 2.1 AXI4 是什么

AXI4 是 ARM AMBA 总线家族里最常用的一类接口。你可以把它想成「一根有多条独立车道的高速公路」：

- **AR 通道**（Read Address）：先发「我要读哪个地址」。
- **R 通道**（Read Data）：对方再把数据送回来。
- **AW/W/B 通道**：写地址、写数据、写响应（本 IP 在主机侧**不写**，所以只用读通道）。

每个通道都用 **valid/ready 握手**：发送方拉高 `valid`，接收方拉高 `ready`，两边同时高那一拍才算成交。

### 2.2 「主机」与「从机」

- **主机（Master）**：主动发起读写。本 IP 的 `m00_axi` 是主机——它去读**别人**的寄存器。
- **从机（Slave）**：被动响应别人的读写。本 IP 的 `s00_axi` 是从机——软件（CPU）通过它来**配置本 IP**。

### 2.3 AXI-Stream 是什么

AXI-Stream（AXI-S）是 AXI 家族里的「数据流」接口，比完整 AXI4 轻得多：只有 `tvalid`/`tready`/`tdata`，外加一个 `tlast` 标记「这一拍是一个包的最后一拍」。它适合连续不断地传数据流，而不是随机地址访问。

### 2.4 核心 + wrapper 分层

复习 u1-l2 的结论：`hdl/` 采用**核心 + wrapper** 分层。

- **核心** `axi_mm_reader.vhd`：纯逻辑，只懂一套简化的内部握手（**IPIC 接口**），完全看不到 AXI4。
- **wrapper** `axi_mm_reader_wrp.vhd`：真正的 AXI4 接口边界，里面实例化了三个 `psi_common` 组件来翻译 AXI4 ↔ IPIC。

本讲的「数据流」几乎都画在 **wrapper** 里——因为它才是把外部 AXI4 接口、核心、FIFO、输出端口连起来的那层。

## 3. 本讲源码地图

| 文件 | 作用 |
|:-----|:-----|
| `doc/Documentation.md` | 官方文档，含接口说明、参数说明、寄存器表和一张整体架构图，是本讲「大图」的权威来源。 |
| `hdl/axi_mm_reader_wrp.vhd` | wrapper，本讲的主战场。里面实例化了解码从机、AXI 主机、核心，并用 `generate` 块切换两种输出模式。 |
| `hdl/axi_mm_reader.vhd` | 核心逻辑。本讲只看它的「对外端口」和「内部由哪几个组件拼成」，不展开 FSM。 |
| `hdl/definitions_pkg.vhd` | 共享常量包，定义寄存器索引等，本讲偶尔引用。 |

## 4. 核心概念与源码讲解

### 4.1 接口总览

#### 4.1.1 概念说明

先把 IP 核当成一个「黑盒」，只看它的引脚。这个黑盒有 **三类外部打交道的方式**：

1. **被人配置**（`s00_axi`，从机）：软件告诉它「要读哪些寄存器、读几个、使能与否」。
2. **去读别人**（`m00_axi`，主机）：它主动发起 AXI 读，把配置好的那些寄存器一个个读回来。
3. **把读回的值送出去**（`m_axis` 或寄存器 `RdData`）：按用户选的输出方式交还给软件或下游流。

此外还有两个「事件」引脚：

- `Trig`（输入）：一个时钟周期的脉冲，命令「现在就读一轮」。
- `DoneIrq`（输出）：一个时钟周期的脉冲，表示「一轮读完了」。

所有接口共用同一个 `Clk`/`Rst`——本 IP **不做时钟域穿越**，需要不同时钟时由用户在外部加 CDC 组件。

#### 4.1.2 核心流程

文档用一张图概括了这个黑盒（见源码精读里的架构图）。用文字描述接口关系：

```text
            软件 (CPU)
               │  配置/读结果
               ▼
          ┌─────┐  Trig ───► (启动读周期)
软件 ◄──── │ IP  │
读 RdData  │ 核  │
或收 AXIS  │     │ ───► DoneIrq (一轮完成脉冲)
          └──┬──┘
             │ m00_axi (主机，主动读)
             ▼
        外部寄存器（如 System-Monitor）
```

关键约束（来自官方文档）：

- `Trig` 只能用**单拍脉冲**；读周期进行中来的 `Trig` 会被忽略。
- `DoneIrq` 在一轮读完成时输出**一个高电平周期**。
- `m_axis` 端口**是否存在**取决于配置：AXIS 输出时存在，AXIMM 输出时不存在。

#### 4.1.3 源码精读

接口定义全部集中在 wrapper 的 entity 里。先看 generics（综合时定死的参数）：

[hdl/axi_mm_reader_wrp.vhd:24-36](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L24-L36) —— 定义了时钟频率 `ClkFrequencyHz`、超时 `TimeoutUs_g`、最大寄存器数 `MaxRegCount_g`、缓冲周期数 `MinBuffers_g`、**输出类型 `Output_g`**（默认 `"AXIMM"`）和从机地址宽度。注意默认 `Output_g = "AXIMM"`，这正是默认打包出来的 IP 没有 `m_axis` 端口的根本原因。

接着是三类事件与接口端口：

[hdl/axi_mm_reader_wrp.vhd:42-49](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L42-L49) —— `Clk`/`Rst` 与事件 `Trig`（输入）、`DoneIrq`（输出）。

[hdl/axi_mm_reader_wrp.vhd:54-93](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L54-L93) —— `s00_axi` **从机**接口（AR/R 读通道 + AW/W/B 写通道）。这是软件配置 IP 的入口。

[hdl/axi_mm_reader_wrp.vhd:98-113](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L98-L113) —— `m00_axi` **主机**接口，只有 AR/R 两个读通道（没有写通道），印证「本 IP 只读不写」。

[hdl/axi_mm_reader_wrp.vhd:118-121](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L118-L121) —— `m_axis` AXI-Stream 输出端口（`tdata`/`tvalid`/`tready`/`tlast`）。

文档对外部行为的描述（与本讲直接相关的几条）：

[doc/Documentation.md:24-33](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/doc/Documentation.md#L24-L33) —— 官方对接口行为的说明：共用时钟复位、`Trig` 脉冲启动读周期（进行中被忽略，且必须是单拍）、`s00_axi` 配置、`m00_axi` 读寄存器、`DoneIrq` 完成脉冲、`m_axis` 是否存在取决于配置。

[doc/Documentation.md:84-88](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/doc/Documentation.md#L84-L88) —— 官方整体架构图（`pics/architecture.png`，源文件 `Architecture.vsdx`）。本讲的框图就是这张图的文字化版本。

#### 4.1.4 代码实践

**实践目标**：不依赖任何工具，只用眼睛把接口「点」一遍，确认黑盒引脚与文档一致。

**操作步骤**：

1. 打开 [hdl/axi_mm_reader_wrp.vhd:37-124](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L37-L124) 的 `port (...)` 段。
2. 画一张表格，左列填端口名，右列填它的方向（`in`/`out`）和所属接口（控制 / s00_axi / m00_axi / m_axis）。
3. 对照 [doc/Documentation.md:24-33](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/doc/Documentation.md#L24-L33)，核对每一个文档声明的接口是否都能在 entity 里找到对应端口。

**需要观察的现象**：`m00_axi` 一侧**只有 `ar*`/`r*` 信号，没有任何 `aw*`/`w*`/`b*` 信号**；而 `s00_axi` 一侧读、写通道齐全。

**预期结果**：你应当得出结论——主机侧只读、从机侧可读可写。这正是「IP 去读别人，别人（软件）来配置它」的方向性体现。

> 待本地验证项：如果你在 Vivado 里打开打包好的 IP（Output_g=AXIMM），应能看到 `m_axis` 端口**不出现**；切到 AXIS 再打包则出现。本讲只做源码阅读，不强制要求运行 Vivado。

#### 4.1.5 小练习与答案

**练习 1**：`Trig` 为什么必须是「恰好一个高电平周期」的脉冲，而不能持续拉高？

**参考答案**：因为 `Trig` 是边沿式启动信号，持续拉高会在每个时钟沿都被识别为一次触发请求；而读周期进行中的触发会被忽略，所以持续高电平语义混乱。文档 [doc/Documentation.md:25-27](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/doc/Documentation.md#L25-L27) 明确要求「exactly one high-cycle」。

**练习 2**：`DoneIrq` 和 `Trig` 在方向上分别是 in 还是 out？

**参考答案**：`Trig` 是 `in`（外部命令 IP 开始一轮），`DoneIrq` 是 `out`（IP 通知外部一轮结束）。见 [hdl/axi_mm_reader_wrp.vhd:48-49](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L48-L49)。

---

### 4.2 读周期数据流

#### 4.2.1 概念说明

「读周期」（read cycle）是本 IP 的核心动作：**把 RegTable 里登记的所有寄存器，一个个读回来，塞进 FIFO**。你可以把它理解成一个「照着购物清单（RegTable）逐项取货（读寄存器）、最后统一装箱（FIFO）」的过程。

要讲清楚数据流，需要认识 wrapper 内部连起来的**五个角色**：

1. **AXI 从机解码器**（`psi_common_axi_slave_ipif`）：把软件的 AXI4 读写翻译成简单的「寄存器读/写」和「内存读/写」信号（IPIC）。
2. **RegTable RAM**（`psi_common_tdp_ram`）：双端口 RAM，A 端口给软件写地址表，B 端口给核心读地址表。
3. **核心 FSM**（`axi_mm_reader.vhd`）：照着 RAM 里的地址表，逐个命令「读这个地址」。
4. **AXI 主机**（`psi_common_axi_master_simple`）：把核心的简化命令翻译成真正的 AXI4 读事务，发到 `m00_axi`，再把读回的数据交还核心。
5. **读数据 FIFO**（`psi_common_sync_fifo`）：暂存读回的值（带一个 Last 位），再按输出模式交出去。

#### 4.2.2 核心流程

一次完整读周期的数据流（自顶向下）：

```text
  软件 ──s00_axi──► [从机解码器] ──reg_wr/mem_wr──► ┐
                                                   │ 配置
                                          [RegTable RAM] ◄── 核心按 RamAddr 读地址
                                                   │ 地址 (B 端口)
                                                   ▼
  Trig/超时 ──► [核心 FSM] ──AxiM_CmdRd_*──► [AXI 主机] ──m00_axi──► 外部寄存器
                                                   ▲                     │
                                                   │                  读回值
                                                   └──── AxiM_RdDat_* ◄─┘
                                                          │
                                                          ▼
                                                   [读数据 FIFO] ──AxiS_*──► 输出
```

用一句话概括这条通路：

> 软件把「要读哪些地址」写进 RegTable RAM；核心 FSM 在 `Trig`/超时驱动下，逐个从 RAM 取出地址，经 AXI 主机发到 `m00_axi` 读回数据，结果连同 Last 位一起压入 FIFO，最后由输出端取走。

这里有几个「数据流上的关键设计点」先记住，细节留给后续讲义：

- **配置和读址共用同一块 RAM 的两个端口**：软件写 A 端口、核心读 B 端口，互不阻塞。
- **核心只发单拍读命令**：`CmdRd_Size => "1"`、`AxiMaxBeats_g => 1`，每个寄存器一次独立的 AXI 读事务。
- **读回值先进 FIFO 再输出**：FIFO 让「读得快」和「输出/软件取得慢」解耦，这是本 IP 能缓冲多个读周期的关键。
- **`DoneIrq` 在一轮所有寄存器都读完时拉高一拍**。

#### 4.2.3 源码精读

**（a）AXI 从机解码器**——软件配置入口。它把 `s00_axi` 解码成 `reg_rd/reg_wr/reg_wdata/reg_rdata`（寄存器侧）和 `mem_addr/mem_wr/mem_wdata/mem_rdata`（内存侧，即 RegTable）：

[hdl/axi_mm_reader_wrp.vhd:191-263](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L191-L263) —— `psi_common_axi_slave_ipif` 实例。`NumReg_g => USER_SLV_NUM_REG`（寄存器侧数量），`UseMem_g => true`（启用内存侧，承载 RegTable）。紧接着 [hdl/axi_mm_reader_wrp.vhd:264](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L264) 把 4 字节写使能 `mem_wr` 压成一个写脉冲 `mem_wrena`。

**（b）AXI 主机**——核心发命令、收回数据的中介。它把核心的简化命令接口翻译成 `m00_axi` 的真 AXI4：

[hdl/axi_mm_reader_wrp.vhd:271-308](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L271-L308) —— `psi_common_axi_master_simple` 实例。注意几个对「数据流形状」有决定意义的参数：`ImplRead_g => true`、`ImplWrite_g => false`（只读不写），`AxiMaxBeats_g => 1`（单拍事务），`CmdRd_Size => "1"`（命令固定请求 1 拍数据），`AxiMaxOpenTrasactions_g => 4`（最多 4 个在途事务），`DataFifoDepth_g => 16`（主机内部还有一层 16 深的数据 FIFO）。

**（c）核心实例**——把解码器、RAM、FIFO 与主机串起来的中枢。注意端口映射里「谁连谁」就是数据流本身：

[hdl/axi_mm_reader_wrp.vhd:313-343](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L313-L343) —— `axi_mm_reader` 核心实例。几条关键连线对照数据流：
- `RegCount <= reg_wdata(RegIdx_RegCnt_c)(...)`：软件写的 RegCnt 寄存器位字段喂给核心。
- `Enable <= reg_wdata(RegIdx_Ctrl_c)(BitIdx_Ctrl_Ena_c)`：软件写的使能位喂给核心。
- `RegCfg_Idx <= mem_addr(...)`、`RegCfg_WrReg <= mem_wdata`、`RegCfg_Wr <= mem_wrena`：软件对 RegTable 的写经解码器直达核心内部的 RAM（A 端口）。
- `AxiM_CmdRd_*` / `AxiM_RdDat_*`：核心 ↔ AXI 主机的命令/数据通道。
- `AxiS_*`：核心 FIFO 的输出，准备交给输出模式选择（见 4.3）。

**（d）核心内部的三个组件**——再看一眼核心 `axi_mm_reader.vhd` 内部，确认「RAM + FIFO + FSM」三件套：

[hdl/axi_mm_reader.vhd:206-223](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L206-L223) —— RegTable 用 `psi_common_tdp_ram` 实现：A 端口是 `RegCfg_*`（软件配置），B 端口用核心的 `r.RamAddr` 读、输出 `RamRegAddr`（核心要读的寄存器地址）。

[hdl/axi_mm_reader.vhd:226-247](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L226-L247) —— 读数据 FIFO 用 `psi_common_sync_fifo` 实现：`Width_g => 32+1`（32 位数据 + 1 位 Last），`InData(31:0) <= AxiM_RdDat_Data`（读回值），`InData(32) <= Last`（这一拍是不是本周期最后一个），`OutData(31:0) => AxiS_Data`、`OutData(32) => AxiS_Last`。这就是「读回值连同 Last 位一起压栈」的实现。

FSM 本身（[hdl/axi_mm_reader.vhd:74](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L74) 定义了五个状态 `Idle_s/ReadAddr_s/SetCmd_s/ApplyCmd_s/WaitDone_s`）只在本讲点到为止——状态转移细节是 u2-l3 的主题。这里你只需知道：FSM 在 `Idle_s` 收到启动后，循环走「取地址 → 发命令 → 等成交 → 取下一个地址」，直到 RegTable 读完，再到 `WaitDone_s` 等所有数据落进 FIFO，最后拉一拍 `DoneIrq`。

**（e）触发与超时**——启动这一整条通路的两种方式：

[doc/Documentation.md:46-47](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/doc/Documentation.md#L46-L47) —— 官方对超时的说明：使能后若没有 `Trig`，经过 `Timeout in us` 也会自动开始一轮；这等价于「不接 `Trig`、只设超时」即可实现周期性读取。

超时换算成时钟周期数的常量在 wrapper 里：

[hdl/axi_mm_reader_wrp.vhd:134](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L134) —— `TimeoutCkCycles_c := integer(real(ClkFrequencyHz)*real(TimeoutUs_g)/1.0e6)`，即把「微秒」换算成「时钟周期数」。取默认 `ClkFrequencyHz = 100_000_000`、`TimeoutUs_g = 100`，代入得

\[
\text{TimeoutCkCycles\_c} = \left\lfloor \frac{100\times10^{6}\ \times\ 100}{10^{6}} \right\rfloor = 10\,000
\]

即 100 MHz 下每 100 µs（= 10 000 拍）自动触发一轮。触发/超时/使能门控的精确逻辑见 u2-l4。

#### 4.2.4 代码实践

**实践目标**：在源码里把 4.2.2 的框图「一条线一条线」地找出来，验证数据流不是凭空画的。

**操作步骤**：

1. 从软件写一个 RegTable 项出发：在 [hdl/axi_mm_reader_wrp.vhd:259-262](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L259-L262) 找到解码器输出的 `o_mem_addr/o_mem_wr/o_mem_wdata`。
2. 跟到 [hdl/axi_mm_reader_wrp.vhd:264](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L264) 的 `mem_wrena`，再跟到 [hdl/axi_mm_reader_wrp.vhd:328-331](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L328-L331) 核心的 `RegCfg_*` 端口，最后落到 [hdl/axi_mm_reader.vhd:212-217](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L212-L217) RAM 的 A 端口。
3. 再从核心读址出发：[hdl/axi_mm_reader.vhd:219-222](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L219-L222) 的 B 端口 `DoutB => RamRegAddr`，经 FSM 的 `SetCmd_s`（[hdl/axi_mm_reader.vhd:137-140](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L137-L140)）变成 `AxiM_CmdRd_Addr`。
4. 跟到 [hdl/axi_mm_reader_wrp.vhd:286-293](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L286-L293) AXI 主机的 `CmdRd_*`/`RdDat_*`，确认命令出去、数据回来。
5. 最后数据落进 FIFO：[hdl/axi_mm_reader.vhd:238-241](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L238-L241)。

**需要观察的现象**：你应当能画出一条**没有断点**的链路：`s00_axi → 解码器 → mem_* → RegCfg_* → RAM(A) … RAM(B) → RamRegAddr → FSM → AxiM_CmdRd → 主机 → m00_axi → 读回 → AxiM_RdDat → FIFO → AxiS_*`。

**预期结果**：理解「软件只负责填表和使能，真正驱动读周期的是核心 FSM 与 AXI 主机」。

#### 4.2.5 小练习与答案

**练习 1**：核心发一条读命令，对应 `m00_axi` 上几个数据拍（beat）？

**参考答案**：1 个。因为 [hdl/axi_mm_reader_wrp.vhd:275](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L275) `AxiMaxBeats_g => 1` 且 [hdl/axi_mm_reader_wrp.vhd:287](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L287) `CmdRd_Size => "1"`，每个寄存器一次单拍读事务。

**练习 2**：RegTable RAM 为什么用「双端口」？

**参考答案**：因为软件配置（A 端口写）和核心读址（B 端口读）是两个独立的使用者，双端口让二者能同时操作同一块 RAM 互不阻塞。见 [hdl/axi_mm_reader.vhd:212-222](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L212-L222)。

**练习 3**：FIFO 的宽度是 33 位（`Width_g => 32+1`），多出来的 1 位是什么？

**参考答案**：是 Last 位，标记「这一拍是本读周期的最后一个值」，进 FIFO 时由组合逻辑 `Last` 驱动（[hdl/axi_mm_reader.vhd:239](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L239)），出 FIFO 时变成 `AxiS_Last`（[hdl/axi_mm_reader.vhd:243](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L243)）。它让下游知道一个「包」在哪里结束。

---

### 4.3 AXIS vs AXIMM

#### 4.3.1 概念说明

读回来的值进了 FIFO 之后，「怎么交出去」有两种风格，由 generic `Output_g` 选择：

- **AXIS（AXI-Stream）**：FIFO 的输出直接接到 IP 的 `m_axis` 端口，作为一个标准 AXI-Stream 数据流连续不断地吐给下游（比如 DMA、另一根 IP）。**软件不参与取数**。
- **AXIMM（AXI Memory-Mapped）**：FIFO 被映射到 IP 自己的寄存器空间，软件通过 `s00_axi` 读 `RdData` 寄存器来「弹出」一个值，读 `RdLast` 来判断「这是不是本周期最后一个」。**没有 `m_axis` 端口**。

为什么要有两种？因为用途不同：

- 想把寄存器值**实时喂给数据流**（如把温度转成 AXI-Stream 喂给打包器）→ 用 **AXIS**。
- 想让 **CPU 软件**按自己的节奏慢慢取数（尤其在 Linux 这种实时性差的系统上）→ 用 **AXIMM**，再配合 FIFO 的缓冲深度，软件晚几轮来取也不会丢数据。

#### 4.3.2 核心流程

两种模式的数据流分歧点在 wrapper 里——**同一个核心、同一个 FIFO 输出 `AxiS_*`**，由 `generate` 块选择如何「收尾」：

```text
              [核心 FIFO 输出]
              AxiS_Data/Vld/Last/Rdy/Level
                       │
          ┌────────────┴────────────┐
   Output_g="AXIS"            Output_g="AXIMM" (默认)
          │                           │
   g_axis generate              g_naxis generate
   m_axis_*  ◄──直连           reg_rdata(RdData) ◄── AxiS_Data
   (软件不参与)                 reg_rdata(RdLast) ◄── AxiS_Last
                                AxiS_Rdy ◄── reg_rd(RdData)  ← 软件读 RdData 才弹 FIFO
```

一个**极其重要**的细节（官方文档反复强调）：在 AXIMM 模式下，**读 `RdData` 会把当前值从 FIFO 弹出**，所以判断「这是不是最后一个」的 `RdLast` 必须**先于** `RdData` 读。否则你读完 `RdData`，对应的 `RdLast` 信息也跟着下一个值变了。

#### 4.3.3 源码精读

模式切换就靠两个互斥的 `generate` 块，这是本讲最核心的代码：

[hdl/axi_mm_reader_wrp.vhd:170-176](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L170-L176) —— `g_axis` 块（`Output_g = "AXIS"` 时综合）。把 FIFO 输出**直连**到 `m_axis_*`：`m_axis_tdata <= AxiS_Data`、`m_axis_tvalid <= AxiS_Vld`、`AxiS_Rdy <= m_axis_tready`、`m_axis_tlast <= AxiS_Last`。同时把 FIFO 水位 `AxiS_Level` 接到 `Level` 寄存器。这种模式下软件完全不碰数据。

[hdl/axi_mm_reader_wrp.vhd:178-184](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L178-L184) —— `g_naxis` 块（`Output_g /= "AXIS"`，即 AXIMM 时综合）。关键三行：
- `m_axis_tvalid <= '0'`：没有流端口，恒为无效。
- `AxiS_Rdy <= reg_rd(RegIdx_RdData_c)`：**只有软件读 `RdData` 寄存器那一拍，FIFO 才弹出一个值**——这就是「读 RdData 弹 FIFO」的实现。
- `reg_rdata(RegIdx_RdData_c) <= AxiS_Data` 和 `reg_rdata(RegIdx_RdLast_c)(BitIdx_RdLast_c) <= AxiS_Last`：把 FIFO 当前值与 Last 位映射回寄存器空间，供软件读。

注意两个块都把 `AxiS_Level` 接到 `reg_rdata(RegIdx_Level_c)`，所以无论哪种模式，软件都能读到 FIFO 水位 `Level`。

官方文档对两种模式的定义和那条「先读 RdLast」的约定：

[doc/Documentation.md:52-54](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/doc/Documentation.md#L52-L54) —— AXIS 与 AXIMM 的官方定义。

[doc/Documentation.md:74-82](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/doc/Documentation.md#L74-L82) —— `RdData`（RV：带副作用，读即弹出）、`RdLast`（R：只读）寄存器，以及 AXIMM 下「`RdLast` 必须先于 `RdData` 读」的明确要求。

寄存器索引常量在 `definitions_pkg.vhd`：

[hdl/definitions_pkg.vhd:25-33](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/definitions_pkg.vhd#L25-L33) —— `RegIdx_RdData_c := 2`、`RegIdx_RdLast_c := 3`、`RegIdx_Level_c := 4` 等，正是上面 `generate` 块里引用的下标来源。

> 包装层「`m_axis` 端口是否存在」的第三层一致性保证——component.xml 的端口启用条件与 RTL 的这两个 `generate` 块必须对齐——在 u1-l4 已讲过，本讲不重复。

#### 4.3.4 代码实践

**实践目标**：亲手对比两个 `generate` 块，理解「同样的 FIFO 输出，两种完全不同的收尾」。

**操作步骤**：

1. 打开 [hdl/axi_mm_reader_wrp.vhd:167-184](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L167-L184)。
2. 列一张两列对照表：左列「AXIS（g_axis）」，右列「AXIMM（g_naxis）」，分别填：`AxiS_Rdy` 由谁驱动、`AxiS_Data/Last` 流向哪里、`m_axis_*` 如何处理、`Level` 是否可读。
3. 在 AXIMM 那列里，用红笔圈出 `AxiS_Rdy <= reg_rd(RegIdx_RdData_c)`，并写一句旁注「软件读 RdData → 弹 FIFO」。

**需要观察的现象**：两个块对 `AxiS_Rdy` 的驱动来源完全不同——AXIS 来自外部 `m_axis_tready`，AXIMM 来自软件的寄存器读脉冲。

**预期结果**：你能用自己的话回答——「为什么 AXIMM 模式下软件必须先读 `RdLast` 再读 `RdData`？」因为读 `RdData` 的那一拍 `AxiS_Rdy` 才拉高、FIFO 才前进；若先读 `RdData`，`RdLast` 反映的就已经是下一个值了。

#### 4.3.5 小练习与答案

**练习 1**：默认 `Output_g` 是什么？默认打包出来的 IP 有没有 `m_axis` 端口？

**参考答案**：默认 `Output_g = "AXIMM"`（[hdl/axi_mm_reader_wrp.vhd:31](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L31)），所以默认**没有** `m_axis` 端口（该端口由 `g_axis` 块驱动，而 AXIMM 走 `g_naxis`）。

**练习 2**：在 AXIMM 模式下，如果软件从不读 `RdData`，FIFO 会怎样？

**参考答案**：因为 `AxiS_Rdy <= reg_rd(RegIdx_RdData_c)`，软件不读 `RdData` 时 `AxiS_Rdy` 恒为 0，FIFO 不会弹出。新读回的值会持续压栈，直到 FIFO 满，届时 `Fifo_Rdy` 为 0，核心侧 `AxiM_RdDat_Rdy` 也跟着为 0（[hdl/axi_mm_reader.vhd:182](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L182)），主机背压停读——这正是 FIFO 缓冲的意义，但缓冲溢出后仍会丢数据，所以软件必须及时取数。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面的「架构地图」任务。

**任务**：在一张白纸上（或任意画图工具）画出 `vivadoIP_axi_mm_reader` 的**完整数据流框图**，要求：

1. 画出并标注这些外部接口：`Clk`/`Rst`、`Trig`、`DoneIrq`、`s00_axi`、`m00_axi`、`m_axis`。
2. 画出 wrapper 内部的五个角色框：AXI 从机解码器、RegTable RAM、核心 FSM、AXI 主机、读数据 FIFO。
3. 用**实线箭头**表示「配置流」（软件 → 从机 → RegTable），用**虚线箭头**表示「读数据流」（核心 → 主机 → 外部寄存器 → FIFO → 输出）。
4. 在图上用**两个分支**画出输出收尾：一支 AXIS（→ `m_axis`），一支 AXIMM（→ `RdData`/`RdLast` 寄存器），并标注分支条件 `Output_g`。
5. 在图下方用**一句话**描述一次完整读周期的流程。

**参考的一句话描述**：

> 软件经 `s00_axi` 把待读地址写入 RegTable 并使能 IP；`Trig` 脉冲或超时启动核心 FSM，它逐个从 RegTable 取地址、经 `m00_axi` 主机读回寄存器值，连同 Last 位压入内部 FIFO，最后按 `Output_g` 选择经 `m_axis`（AXIS）流出、或映射到 `RdData`/`RdLast` 寄存器（AXIMM）由软件弹出，全部读完时拉一拍 `DoneIrq`。

**自检清单**（画完后逐条对照）：

- [ ] `m00_axi` 一侧只有读通道，没有写通道。
- [ ] RegTable RAM 画了两个端口（软件写 / 核心读）。
- [ ] FIFO 宽度标注了「32 位数据 + 1 位 Last」。
- [ ] AXIMM 分支上标注了「读 RdData → 弹 FIFO」「先读 RdLast 再读 RdData」。
- [ ] `DoneIrq` 是输出、单拍脉冲。

> 提示：如果你画完后想核对，可与官方架构图 [doc/Documentation.md:84-88](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/doc/Documentation.md#L84-L88)（`pics/architecture.png`）对照，但请注意官方图是静态截图，本讲要求你画的是带「两种输出分支」的版本。

## 6. 本讲小结

- 该 IP 是「黑盒」上有三类打交道方式：被配置（`s00_axi` 从机）、去读别人（`m00_axi` 主机，**只读不写**）、送出结果（`m_axis` 或 `RdData` 寄存器）；外加 `Trig` 启动脉冲与 `DoneIrq` 完成脉冲，所有接口共用一个时钟域。
- 一次读周期的数据流是：**软件填 RegTable → 核心 FSM 在 `Trig`/超时驱动下逐个取地址 → AXI 主机发单拍读事务到 `m00_axi` → 读回值连同 Last 位压入 FIFO → 按输出模式交出**。
- 触发有两种：`Trig` 单拍脉冲、或使能后超时自动触发（`TimeoutCkCycles_c = ⌊ClkFrequencyHz × TimeoutUs_g / 10⁶⌋`，默认 100 MHz / 100 µs 即 10 000 拍）。
- 两种输出模式的分歧点在 wrapper 的 `g_axis` / `g_naxis` 两个 `generate` 块：AXIS 把 FIFO 直连 `m_axis`；AXIMM 把 FIFO 映射到 `RdData`/`RdLast`，**软件读 `RdData` 才弹 FIFO**，所以必须先读 `RdLast`。
- 默认 `Output_g = "AXIMM"`，因此默认打包出来的 IP **没有** `m_axis` 端口——这一点由 RTL `generate`、GUI 参数、component.xml 端口启用条件三层共同保证。
- 核心 `axi_mm_reader.vhd` 内部由 **RegTable RAM（tdp_ram）+ 核心 FSM + 读数据 FIFO（sync_fifo）** 三件套拼成，wrapper 再加上 AXI 从机解码器与 AXI 主机，共同完成 AXI4 ↔ 内部逻辑的翻译。

## 7. 下一步学习建议

本讲建立了「大图」，接下来的讲义会逐块拆细：

- 想搞清每个寄存器的地址、位字段、读写模式 → 学 **u2-l2 寄存器映射与配置表**。
- 想看核心 FSM 的五个状态如何转移、`DoneCnt` 与 `Last` 如何工作 → 学 **u2-l3 核心 FSM：双进程状态机**。
- 想精确理解 `Trig`/超时/`Enable` 门控的代码 → 学 **u2-l4 触发与超时机制**。
- 想看 AXI 从机如何解码、AXI 主机如何握手 → 学 **u2-l5 AXI 从机配置接口** 与 **u2-l6 AXI 主机读取通路**。
- 想深入 FIFO 深度计算与两种输出模式的实现细节 → 学 **u2-l7 输出模式与 FIFO/RegTable 存储**。

建议按顺序学习，因为后一讲会用到本讲建立的数据通路心智模型。
