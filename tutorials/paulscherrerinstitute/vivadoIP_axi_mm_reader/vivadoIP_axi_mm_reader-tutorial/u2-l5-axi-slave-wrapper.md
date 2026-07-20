# AXI 从机配置接口（wrapper）

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 `s00_axi`（AXI 从机）这一侧的「软件写一个字」是如何被解码成对内部信号的动作的；
- 解释 `psi_common_axi_slave_ipif` 把 AXI4 事务拆成**寄存器接口**（`reg_rd`/`reg_wr`/`reg_wdata`/`reg_rdata`）和**内存接口**（`mem_addr`/`mem_wr`/`mem_wdata`/`mem_rdata`）两套握手的原因与结果；
- 看懂 wrapper 中三处「位切片」表达式——`reg_wdata(RegIdx_RegCnt_c)(...)`、`reg_wdata(RegIdx_Ctrl_c)(BitIdx_Ctrl_Ena_c)`、`mem_addr(log2ceil(MaxRegCount_g)+1 downto 2)`——分别把 32 位的 AXI 写数据换算成核心需要的 `RegCount`、`Enable`、`RegCfg_Idx`；
- 能够独立追踪「软件写 `RegCnt`」与「软件写 `RegTable` 某一项」两条完整路径，从 `s00_axi_*` 端口一直追到核心 `axi_mm_reader` 的输入端口。

本讲只聚焦**配置通路（写方向）**与 AXI 从机的解码结构；读回方向（`RdData`/`RdLast`/`Level`）在 [u2-l2](u2-l2-register-map.md) 与 [u2-l7](u2-l7-output-modes-fifo.md) 已有铺垫，本讲只在必要处提一句。

## 2. 前置知识

在进入源码前，先用三段白话把背景补齐。

**什么是 AXI 从机（Slave）。** AXI4 是一套「主—从」握手协议。主机（Master）主动发起读写，从机（Slave）被动响应。本 IP 有两个 AXI 接口：`s00_axi` 是**从机**，被软件（CPU）配置；`m00_axi` 是**主机**，主动去读别人的寄存器（见 [u2-l6](u2-l6-axi-master-read.md)）。本讲的主角就是从机 `s00_axi`：软件往一段地址空间里写值，IP 内部就据此改变行为。

**什么是 IPIC 接口。** 直接处理 AXI4 的五个通道（读地址、读数据、写地址、写数据、写响应）非常繁琐。PSI 的 `psi_common` 库提供了一个中间件 `psi_common_axi_slave_ipif`：它在**外侧**吞下完整的 AXI4 信号，在**内侧**吐出一组简化得多的「寄存器/内存」握手信号，称为 **IPIC（IP Interconnect）接口**。这样核心逻辑 `axi_mm_reader`（见 [u2-l3](u2-l3-core-fsm.md)）就完全不必关心 AXI4 的握手细节，只关心「哪个寄存器被读/写了、数据是什么」。wrapper（包装层）的工作正是把 IPIC 信号**翻译**成核心需要的少量端口。

**寄存器区 vs 内存区。** IPIC 把从机的地址空间分成两段：

- **寄存器区**：少量、固定位置的 32 位寄存器（本 IP 的 `Ena`、`RegCnt`、`RdData`、`RdLast`、`Level`）。每个寄存器都有自己的一位「读选通」`reg_rd(i)`、一位「写选通」`reg_wr(i)` 和一组 32 位写数据 `reg_wdata(i)`。
- **内存区**：一块连续的、按地址索引的 RAM（本 IP 的 `RegTable`，即 `Addr[]` 配置表）。它用一组共享的 `mem_addr`/`mem_wr`/`mem_wdata`/`mem_rdata` 信号访问，更像普通的 SRAM 接口。

之所以分两段，是因为配置寄存器数量少、名字固定，用「按位选通」最直观；而 `RegTable` 可能多达上千项，用「地址 + 选通」的内存接口更省信号、也更自然。

> 本讲会反复用到 [u2-l2](u2-l2-register-map.md) 建立的寄存器地图：`Ena@0x00`、`RegCnt@0x04`、`RdData@0x08`、`RdLast@0x0C`、`Level@0x10`，以及从 `0x20` 开始的 `Addr[]` 表。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [hdl/axi_mm_reader_wrp.vhd](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd) | wrapper：真正的 AXI4 接口边界。例化 `psi_common_axi_slave_ipif`（从机解码）、`psi_common_axi_master_simple`（主机）、`axi_mm_reader`（核心），并用三处位切片把它们接起来。 |
| [hdl/definitions_pkg.vhd](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/definitions_pkg.vhd) | 共享常量包：定义所有寄存器索引（`RegIdx_*`）、位索引（`BitIdx_*`）、寄存器总数 `RegCount_c` 与内存区偏移 `MemOffs_c`。 |
| [hdl/axi_mm_reader.vhd](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd) | 核心：本讲只看它的**端口**（`RegCount`/`Enable`/`RegCfg_*`）与 RegTable 的双端口 RAM 例化，确认 wrapper 接进来的信号被谁消费。 |

## 4. 核心概念与源码讲解

### 4.1 psi_common_axi_slave_ipif 解码

#### 4.1.1 概念说明

`psi_common_axi_slave_ipif` 是 wrapper 的「AXI4 翻译官」。它把外侧复杂的五通道 AXI4 握手，化简成内侧两套干净的接口：

- **寄存器接口**：给少量固定寄存器用。每来一次 AXI 读/写，它就在 `reg_rd`/`reg_wr` 向量里拉高**对应那一位**（one-hot），并把写数据放在 `reg_wdata(对应索引)` 上；读数据则由 wrapper 填进 `reg_rdata(对应索引)` 提供给它。
- **内存接口**：给 `RegTable` 这块连续 RAM 用。它直接给出 `mem_addr`（字节地址）、`mem_wr`（4 位字节写选通）、`mem_wdata`（32 位写数据），wrapper 再转给核心里的双端口 RAM。

> 「IPIC」= IP Interconnect，是 Xilinx 早期 `axi_slave_ipif` 的概念命名，PSI 沿用了这个名字。它的核心价值是**让核心逻辑与 AXI4 解耦**——核心只看到简化的握手，换 AXI 版本或换总线只需动这层中间件。

#### 4.1.2 核心流程

软件发起一次 32 位写事务后，wrapper 内部发生：

1. `s00_axi_aw*`（写地址通道）与 `s00_axi_w*`（写数据通道）被 `psi_common_axi_slave_ipif` 接收并握手。
2. ipif 根据地址**判断属于哪一段**：
   - 落在寄存器区 → 置位 `reg_wr(i)`，把 `s00_axi_wdata` 放到 `reg_wdata(i)`；
   - 落在内存区 → 给出 `mem_addr`/`mem_wr`/`mem_wdata`。
3. wrapper 用位切片把 `reg_wdata(i)`/`mem_addr` 翻译成核心端口值。
4. ipif 在 `s00_axi_b*`（写响应通道）回一个 `OKAY`。

读事务类似，只是把方向反过来：ipif 置位 `reg_rd(i)` 或给出 `mem_addr`，wrapper 提供 `reg_rdata(i)` 或 `mem_rdata`，ipif 再经 `s00_axi_r*`（读数据通道）送回 CPU。

#### 4.1.3 源码精读

先看 wrapper 里对 ipif 的**例化与泛型**。`NumReg_g` 用的是向上取整为 2 的幂之后的寄存器个数 `USER_SLV_NUM_REG`，`UseMem_g => true` 表示「同时启用内存区」（即 `RegTable`）：

[hdl/axi_mm_reader_wrp.vhd:133](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L133) —— `USER_SLV_NUM_REG := 2**log2ceil(RegCount_c)` 把实际寄存器数（5 个）向上取整为 8 个，因为 ipif 的寄存器接口要求个数是 2 的幂。

[hdl/axi_mm_reader_wrp.vhd:191-200](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L191-L200) —— ipif 的泛型映射：`NumReg_g => USER_SLV_NUM_REG`（8）、`UseMem_g => true`、`AxiIdWidth_g`/`AxiAddrWidth_g` 跟随 wrapper 的类属。

ipif 的**外侧**直接连到 `s00_axi_*` 端口（地址、数据、握手信号一一对接），**内侧**则吐出寄存器接口与内存接口两组信号。外侧连接是机械的端口对接，从 [hdl/axi_mm_reader_wrp.vhd:201-248](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L201-L248) 可以看到 `s_axi_aclk`/`s_axi_aresetn` 以及五通道信号都被原样接上（注意 `RstN <= not Rst`，AXI 用低有效复位）。

内侧的两组接口是本讲的重点，分别见 4.2、4.3。

### 4.2 寄存器/内存接口

#### 4.2.1 概念说明

ipif 内侧的两组信号在 wrapper 顶部集中声明。理解它们的**类型**是看懂后续位切片的前提：

- `reg_rd` / `reg_wr`：`std_logic_vector(USER_SLV_NUM_REG-1 downto 0)`，**one-hot** 的「第 i 号寄存器正在被读/写」。
- `reg_rdata` / `reg_wdata`：`t_aslv32(0 to USER_SLV_NUM_REG-1)`，是**32 位向量的数组**，下标就是寄存器索引。所以 `reg_wdata(1)` 是「1 号寄存器（`RegCnt`）的 32 位写数据」。
- `mem_addr`：`AxiSlaveAddrWidth_g` 位的字节地址（相对内存区起点）。
- `mem_wr`：4 位字节写选通（对应 32 位字的 4 个字节）。
- `mem_wdata` / `mem_rdata`：32 位写/读数据。
- `mem_wrena`：wrapper 自己派生的「内存写使能」单比特信号。

#### 4.2.2 核心流程

寄存器区的写数据 `reg_wdata(i)` 与内存区的 `mem_*` 都要被翻译成核心端口。wrapper 用了三处关键连接（在核心例化 `i_impl` 的端口映射里），把「AXI 视角的字」变成「核心视角的端口」：

| wrapper 表达式 | 核心端口 | 含义 |
|---|---|---|
| `reg_wdata(RegIdx_RegCnt_c)(log2ceil(MaxRegCount_g)-1 downto 0)` | `RegCount` | 取 `RegCnt` 寄存器的低 10 位作为本轮要读的寄存器个数 |
| `reg_wdata(RegIdx_Ctrl_c)(BitIdx_Ctrl_Ena_c)` | `Enable` | 取 `Ena`/`Ctrl` 寄存器的 bit0 作为使能开关 |
| `mem_addr(log2ceil(MaxRegCount_g)+1 downto 2)` | `RegCfg_Idx` | 内存区字节地址右移 2 位（÷4）得到 `RegTable` 的字索引 |
| `mem_wdata` | `RegCfg_WrReg` | 要写入 `RegTable` 某项的 32 位寄存器地址 |
| `mem_wrena` | `RegCfg_Wr` | 「现在要对 `RegTable` 写一项」的单拍使能 |
| `mem_rdata` | `RegCfg_RdReg` | `RegTable` 读回值（软件可回读校验） |

读回方向上，wrapper 把核心/FIFO 的输出**填回** `reg_rdata`，让 ipif 能经读数据通道送回 CPU：

- `Level` 在两种输出模式下都可读：`reg_rdata(RegIdx_Level_c) <= AxiS_Level`；
- `RdData`/`RdLast` 只在 AXIMM 模式下存在（见 [u2-l7](u2-l7-output-modes-fifo.md)）；
- AXIMM 模式下还有一个**带副作用的读**：`AxiS_Rdy <= reg_rd(RegIdx_RdData_c)`，即软件读 `RdData` 这一位选通被直接当成 FIFO 的弹出使能（详见 4.3.4 与 [u2-l2](u2-l2-register-map.md) 的 RV 概念）。

#### 4.2.3 源码精读

[hdl/axi_mm_reader_wrp.vhd:136-145](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L136-L145) —— IPIC 信号声明。注意 `reg_rdata`/`reg_wdata` 是**数组**类型 `t_aslv32`，下标 `0 to USER_SLV_NUM_REG-1`，这正是后面能用 `reg_wdata(RegIdx_RegCnt_c)` 索引的原因。

[hdl/axi_mm_reader_wrp.vhd:251-262](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L251-L262) —— ipif 内侧的寄存器接口与内存接口连接。`o_reg_rd`/`o_reg_wr`/`o_reg_wdata` 是 ipif **输出**给 wrapper 的（软件写时有效），`i_reg_rdata` 是 wrapper **回填**给 ipif 的（软件读时用）；内存侧同理。

[hdl/axi_mm_reader_wrp.vhd:264](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L264) —— `mem_wrena <= '1' when mem_wr /= "0000" else '0';` 把 ipif 的 4 位字节选通**归约**成一个写使能：只要 4 个字节中任意一个被写，就视为一次有效的 `RegTable` 写。

[hdl/axi_mm_reader_wrp.vhd:170-184](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L170-L184) —— 两个 generate 块（`g_axis`/`g_naxis`）分别处理两种输出模式下 `reg_rdata` 的回填。本讲关注配置（写）通路，这里只需知道「读回值也是经 `reg_rdata` 数组按索引填入」。

#### 4.2.4 代码实践（源码阅读型）

**目标**：把「ipif 内侧信号 ↔ wrapper 位切片 ↔ 核心端口」三者的对应关系填出来。

**步骤**：

1. 打开 [hdl/axi_mm_reader_wrp.vhd:313-343](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L313-L343)（核心 `i_impl` 的端口映射）。
2. 对照下表，把每个核心端口左侧的 wrapper 表达式抄写一遍，并写出它「取的是哪个寄存器/内存信号的哪几位」：

| 核心端口 | wrapper 表达式（你来填） | 取自哪里 |
|---|---|---|
| `RegCount` |  | `reg_wdata( ? )` 的低 ? 位 |
| `Enable` |  | `reg_wdata( ? )` 的第 ? 位 |
| `RegCfg_Idx` |  | `mem_addr` 的第 ? 到 ? 位 |
| `RegCfg_Wr` |  | `mem_wrena`（由 `mem_wr` 派生） |

3. 打开 [hdl/axi_mm_reader.vhd:42-49](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L42-L49)，确认核心端口的方向（`in`/`out`）与你的理解一致。

**需要观察的现象**：核心端口 `RegCount`/`Enable`/`RegCfg_Idx`/`RegCfg_WrReg`/`RegCfg_Wr` 全是 `in`（输入），说明 wrapper 是「供给方」，核心是「消费方」；唯一由核心回送给 wrapper 的是 `RegCfg_RdReg`（`out`），用于软件回读 `RegTable`。

**预期结果（参考答案）**：`RegCount ← reg_wdata(1)(9 downto 0)`；`Enable ← reg_wdata(0)(0)`；`RegCfg_Idx ← mem_addr(11 downto 2)`（默认 `MaxRegCount_g=1024` 时）；`RegCfg_Wr ← mem_wrena`。

> 这是纯源码阅读实践，无需硬件；若要在仿真中观察这些信号的跳变，参见 [u1-l3](u1-l3-running-simulation.md) 跑 `top_tb`，并用波形查看 `reg_wdata`/`mem_addr`——具体波形数值「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `reg_rdata`/`reg_wdata` 要声明成**数组** `t_aslv32(0 to USER_SLV_NUM_REG-1)`，而不是单个 32 位向量？

> **答案**：因为寄存器接口是「按寄存器编号分别给出数据」的。ipif 为每个寄存器单独维护一拍写数据，用数组下标 = 寄存器索引最直观；这样 wrapper 写 `reg_wdata(RegIdx_RegCnt_c)` 就直接拿到「`RegCnt` 寄存器的 32 位写值」，无需再用乘法或拼接从某个总线里截取。

**练习 2**：`mem_wrena` 为什么用 `mem_wr /= "0000"` 而不是直接取 `mem_wr(0)`？

> **答案**：`mem_wr` 是 4 位**字节**选通，软件可能写整个字（`1111`）也可能只写某些字节。wrapper 不关心写哪几个字节，只要「这是一次写」就应当让 `RegTable` 接收，所以用「任意一位为 1」来归约。若只看 `mem_wr(0)`，则在软件只写高字节时会漏掉这次写。

### 4.3 位字段映射

#### 4.3.1 概念说明

软件视角下，配置寄存器是「一个 32 位字」；但核心视角下，它往往只关心其中的**几个比特**。例如 `Ena` 寄存器 32 位里只有 bit0 有意义，`RegCnt` 寄存器里只有低 10 位有意义（最多读 1024 个寄存器）。wrapper 的职责就是做这层「**位字段提取**」。

`definitions_pkg.vhd` 把「哪个寄存器叫什么索引」「使能位在第几位」这些魔法数字集中成命名常量，避免 wrapper 里出现裸数字、也便于后续扩展（见 [u3-l5](u3-l5-extending-ip.md)）。

#### 4.3.2 核心流程：三处切片的数学含义

设默认 `MaxRegCount_g = 1024`，则 `log2ceil(1024) = 10`。

**(a) `Enable` —— 单比特提取**

\[

\text{Enable} \;=\; \text{reg\_wdata}(\text{RegIdx\_Ctrl\_c}=0)\,\big(\text{BitIdx\_Ctrl\_Ena\_c}=0\big)

\]

即「0 号寄存器（`Ena`/`Ctrl`，字节地址 `0x00`）的 bit0」。软件写 `1` 到 `0x00` 即可使能 IP。

**(b) `RegCount` —— 低 N 比特提取**

\[

\text{RegCount} \;=\; \text{reg\_wdata}(\text{RegIdx\_RegCnt\_c}=1)\,\big(\text{log2ceil}(\text{MaxRegCount\_g})-1 \,\text{downto}\, 0\big)

\]

即「1 号寄存器（`RegCnt`，`0x04`）的低 10 位」。因为最多读 1024 个寄存器，10 位足够表示个数（0\~1023；实际有效范围受 `MaxRegCount_g` 约束）。

**(c) `RegCfg_Idx` —— 字节地址转字索引**

\[

\text{RegCfg\_Idx} \;=\; \text{mem\_addr}\,\big(\text{log2ceil}(\text{MaxRegCount\_g})+1 \,\text{downto}\, 2\big)

\]

这一步最关键。`mem_addr` 是内存区内的**字节地址**：

- 丢弃低 2 位 `downto 2` ⇔ 除以 4，把「字节地址」换成「32 位字索引」，因为每个 `RegTable` 项占 4 字节；
- 高位取到 `log2ceil(MaxRegCount_g)+1`：内存区最多 `MaxRegCount_g` 项 = \(4 \times 1024 = 4096\) 字节 = \(2^{12}\) 字节，字节地址需要 bit 0\~11，字索引需要 bit 2\~11，共 10 位，正好是 `log2ceil(MaxRegCount_g)` 位，与核心 `RegCfg_Idx` 的位宽 `log2ceil(MaxRegCount_g)-1 downto 0` 一致。

**为什么 `mem_addr` 是「内存区内偏移」而不是全地址？** 因为 `Addr[0]` 在软件眼里是字节地址 `0x20`（=32）。若 `mem_addr` 是全地址 32，取 bit 11\~2 会得到 8，而不是 0；但核心的 `RegTable` 第 0 项必须对应 `Addr[0]`。所以 ipif 吐出的 `mem_addr` 必然是「已扣除寄存器区基底（`0x20`）之后的内存区内字节偏移」，使 `Addr[0]`→偏移 0→字索引 0。这层「扣除寄存器区」的工作由 `psi_common_axi_slave_ipif` 内部完成（具体实现在 `psi_common` 库内，本仓库不直接包含）。

#### 4.3.3 源码精读

[hdl/definitions_pkg.vhd:25-35](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/definitions_pkg.vhd#L25-L35) —— 所有索引常量集中于此：`RegIdx_Ctrl_c=0`、`BitIdx_Ctrl_Ena_c=0`、`RegIdx_RegCnt_c=1`、`RegIdx_RdData_c=2`、`RegIdx_RdLast_c=3`、`BitIdx_RdLast_c=0`、`RegIdx_Level_c=4`，并由此派生 `RegCount_c = RegIdx_Level_c+1 = 5`（实际寄存器数），`MemOffs_c = 8`（内存区起始的**字**索引，仅供测试台使用，RTL 不直接读它）。

[hdl/axi_mm_reader_wrp.vhd:326-331](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L326-L331) —— 三处位切片的**落点**，核心例化的端口映射：

```vhdl
RegCount   => reg_wdata(RegIdx_RegCnt_c)(log2ceil(MaxRegCount_g)-1 downto 0),
Enable     => reg_wdata(RegIdx_Ctrl_c)(BitIdx_Ctrl_Ena_c),
RegCfg_Idx => mem_addr(log2ceil(MaxRegCount_g)+1 downto 2),
RegCfg_WrReg => mem_wdata,
RegCfg_RdReg => mem_rdata,
RegCfg_Wr    => mem_wrena,
```

这几行就是本讲的「心脏」：它们把 AXI 写数据换算成核心端口。

再看这些端口在核心里被谁消费。[hdl/axi_mm_reader.vhd:42-49](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L42-L49) 是核心端口声明；[hdl/axi_mm_reader.vhd:206-223](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L206-L223) 是 `RegTable` 的双端口 RAM 例化：

```vhdl
i_ram : entity work.psi_common_tdp_ram
    ...
    port map (
        AddrA  => RegCfg_Idx,   -- 软件写侧地址（来自 mem_addr 切片）
        WrA    => RegCfg_Wr,    -- 软件写使能（mem_wrena）
        DinA   => RegCfg_WrReg, -- 软件写数据（mem_wdata）
        DoutA  => RegCfg_RdReg, -- 软件回读
        AddrB  => r.RamAddr,    -- 核心读侧地址（FSM 遍历指针）
        DoutB  => RamRegAddr    -- 喂给 AXI 主机的目标寄存器地址
        ...
    );
```

可见 RAM 的 **A 口**完全服务于软件配置（写 `RegTable`），**B 口**服务于核心 FSM 的遍历读取。wrapper 接过来的 `RegCfg_*` 信号正是 A 口。

#### 4.3.4 代码实践（追踪两条完整路径）

**目标**：分别追踪「软件写 `RegCnt`」与「软件写 `RegTable` 第 3 项」两条路径，把每一跳的信号名写出来，并算出位切片的数值。

**路径 A：软件写 `RegCnt = 5`（本轮读 5 个寄存器）**

| 跳 | 位置 | 信号取值 |
|---|---|---|
| 1. 软件发起 AXI 写 | `s00_axi_awaddr=0x04`, `s00_axi_wdata=0x00000005` | 字节地址 `0x04` 落在寄存器区 |
| 2. ipif 解码 | [hdl/axi_mm_reader_wrp.vhd:251-255](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L251-L255) | `reg_wr(1)` 拉高一拍，`reg_wdata(1) = 0x00000005` |
| 3. wrapper 切片 | [hdl/axi_mm_reader_wrp.vhd:326](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L326) | `RegCount = reg_wdata(1)(9 downto 0) = "0000000101"` |
| 4. 核心采样 | [hdl/axi_mm_reader.vhd:125](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L125) | FSM 在 `Idle_s` 把 `RegCount` 锁进 `r.RegCount`，下次启动读周期就只读 5 项 |

**路径 B：软件写 `Addr[3] = 0x4000_0000`（把第 4 个要读的寄存器地址设为 `0x4000_0000`）**

| 跳 | 位置 | 信号取值 |
|---|---|---|
| 1. 软件发起 AXI 写 | `s00_axi_awaddr=0x2C`, `s00_axi_wdata=0x40000000` | `0x2C = 0x20 + 4*3`，落在内存区 |
| 2. ipif 解码 | [hdl/axi_mm_reader_wrp.vhd:257-262](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L257-L262) | ipif 扣除寄存器区基底后，`mem_addr = 0x2C - 0x20 = 0x0C`（字节偏移），`mem_wr="1111"`，`mem_wdata=0x40000000` |
| 3. wrapper 派生写使能 | [hdl/axi_mm_reader_wrp.vhd:264](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L264) | `mem_wrena = '1'`（因为 `mem_wr ≠ "0000"`） |
| 4. wrapper 切片地址 | [hdl/axi_mm_reader_wrp.vhd:328](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader_wrp.vhd#L328) | `RegCfg_Idx = mem_addr(11 downto 2)`；`0x0C = "1100"`，bit 11\~2 = `"11"` = **3** ✓ |
| 5. 写入 RegTable | [hdl/axi_mm_reader.vhd:206-223](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L206-L223) | RAM A 口：`AddrA=3, WrA=1, DinA=0x40000000` → `RegTable[3] := 0x4000_0000` |

**需要观察的现象**：路径 B 第 4 步是验证理解的关键——`0x0C` 右移 2 位（丢掉 bit1、bit0 的 `00`）正好得到 3，对应 `Addr[3]`。这印证了「`mem_addr` 是内存区内字节偏移、`downto 2` 是字节地址÷4」。

**预期结果**：两条路径都能在源码里逐跳对上号；路径 B 的地址换算 `0x0C → 3` 成立。若你在仿真波形里实测，`mem_addr` 是否确实是 `0x0C`（而非全地址 `0x2C`）取决于 ipif 内部实现——这一点「待本地验证」，但**切片表达式成立的前提**就是它必须是区内偏移。

> 想在真实软件里看这两条路径，可以参考 [u3-l1](u3-l1-c-driver.md) 的 C 驱动：`MmReader_SetRegTable`（[drivers/axi_mm_reader/src/axi_mm_reader.c:37-60](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/drivers/axi_mm_reader/src/axi_mm_reader.c#L37-L60)）正是用 `Xil_Out32(baseAddr + 0x20 + 4*idx, regs_p[idx])` 循环写整张表，再写 `RegCnt`，与本讲路径 A/B 完全对应。

#### 4.3.5 小练习与答案

**练习 1**：若把 `MaxRegCount_g` 从 1024 改成 256，`RegCount` 和 `RegCfg_Idx` 两处切片的位宽分别变成多少？

> **答案**：`log2ceil(256) = 8`。`RegCount` 变为 `reg_wdata(1)(7 downto 0)`（8 位）；`RegCfg_Idx` 变为 `mem_addr(9 downto 2)`（8 位，因为 `log2ceil(256)+1 = 9`）。两处都由同一个 `log2ceil(MaxRegCount_g)` 表达式驱动，所以改一个泛型就能自适应。

**练习 2**：软件写 `Addr[0]` 时 `s00_axi_awaddr=0x20`。请说明为什么 wrapper 里 `RegCfg_Idx` 最终得到 0，而不是 8。

> **答案**：`0x20` 是全地址。ipif 内部已扣除寄存器区基底（`0x20`），吐出的 `mem_addr` 是内存区内偏移 = `0x20 - 0x20 = 0`。再 `mem_addr(11 downto 2)` 取字索引 = 0。若 `mem_addr` 是未扣除的全地址 32，切片会得 8，与「`Addr[0]` 应落到 `RegTable[0]`」矛盾——由此反推 ipif 必然做了基底扣除。

**练习 3**：为什么文档强调「`RegCnt` 不要在 IP 使能时修改」？（提示：看核心在哪个状态采样 `RegCount`。）

> **答案**：核心只在 `Idle_s` 采样 `RegCount` 到 `r.RegCount`（[hdl/axi_mm_reader.vhd:125](https://github.com/paulscherrerinstitute/vivadoIP_axi_mm_reader/blob/ca5ef76b35221949ccac652c9e97978268dd956f/hdl/axi_mm_reader.vhd#L125)），整个读周期用的是这次锁存的值。若使能期间改 `RegCnt`，新值要等下一次回到 `Idle_s` 才生效，但软件可能误以为它立即生效，从而与 FSM 当前轮的长度（`RamAddr = RegCount` 判据）不一致。所以约定「先禁用→改配置→再使能」，C 驱动 `MmReader_SetRegTable` 也据此强制要求 IP 处于禁用态。

## 5. 综合实践

**任务**：画出 `s00_axi` 配置通路的完整信号流图，并用一个具体例子端到端走一遍。

要求：

1. 画一张图，包含这些节点：`s00_axi_*`（五通道）→ `psi_common_axi_slave_ipif` →（寄存器接口 `reg_wr`/`reg_wdata` 与内存接口 `mem_addr`/`mem_wr`/`mem_wdata`）→ wrapper 位切片 → 核心 `axi_mm_reader` 端口（`Enable`/`RegCount`/`RegCfg_*`）→ `RegTable` 双端口 RAM 的 A 口与 B 口。在「位切片」处标注三处表达式。
2. 设定场景：软件要配置「使能关闭 → 写 `RegCnt=4` → 写 `Addr[0..3]` 四个地址 → 使能打开」。请按时间顺序列出每一步会在 `reg_wdata`/`mem_addr`/`RegCount`/`RegCfg_Idx` 上产生什么值，并指出 `RegTable` 的 A 口与 B 口分别由谁驱动。
3. 思考题：如果软件只想更新 `Addr[2]` 一个项，是否需要重写整张表？为什么？参考 C 驱动 `MmReader_SetRegTable` 的实现说明它的取舍。

**参考要点**：

- A 口（`AddrA=RegCfg_Idx`, `WrA=RegCfg_Wr`, `DinA=RegCfg_WrReg`）始终由**软件侧**驱动，软件可以只写某一项而不动其它项（AXI 是按地址写的，ipif 只会在被写的那一项给出 `mem_wrena` 脉冲）。
- B 口（`AddrB=r.RamAddr`）由**核心 FSM** 驱动，读周期开始后从 0 遍历到 `RegCount-1`。
- `MmReader_SetRegTable` 选择「整表重写 + 必须先禁用」是一种**保守且易用**的策略：它避免了软件自己维护「哪些项已写」的状态，代价是更新任何一个寄存器都要重写整表——这在配置阶段（非实时）是可接受的。

> 本实践为源码阅读 + 推理型，无需硬件。若想用仿真核对，可在 [u1-l3](u1-l3-running-simulation.md) 的 `top_tb` 中观察 `top_tb` 激励进程对 `s00_axi` 的写操作，以及 DUT 内部 `reg_wdata`/`mem_addr` 的波形——具体数值「待本地验证」。

## 6. 本讲小结

- wrapper 用 `psi_common_axi_slave_ipif` 把 `s00_axi` 的 AXI4 事务解码成两套 IPIC 信号：**寄存器接口**（one-hot `reg_rd`/`reg_wr` + 数组 `reg_wdata`/`reg_rdata`）与**内存接口**（`mem_addr`/`mem_wr`/`mem_wdata`/`mem_rdata`）。
- 寄存器区放少量固定寄存器（`Ena`/`RegCnt`/`RdData`/`RdLast`/`Level`），内存区放 `RegTable`（`Addr[]`）；实际寄存器数 5 向上取整为 8（`USER_SLV_NUM_REG`），内存区从字索引 8（字节 `0x20`）开始。
- 三处位切片是 wrapper 的核心翻译动作：`Enable = reg_wdata(0)(0)`、`RegCount = reg_wdata(1)(9 downto 0)`、`RegCfg_Idx = mem_addr(11 downto 2)`。
- `mem_addr(downto 2)` = 字节地址 ÷ 4 得字索引；`mem_addr` 是 ipif 扣除寄存器区基底后的**内存区内偏移**，这样 `Addr[0]@0x20` 才能落到 `RegTable[0]`。
- `mem_wrena` 由 4 位字节选通 `mem_wr` 归约而来，作为 `RegTable` A 口的单比特写使能；A 口服务软件配置，B 口服务核心 FSM 遍历。
- 所有「哪个寄存器在第几位」的魔法数字都集中在 `definitions_pkg.vhd`，扩展时只动这一处（见 [u3-l5](u3-l5-extending-ip.md)）。

## 7. 下一步学习建议

- 下一步读 [u2-l6 AXI 主机读取通路](u2-l6-axi-master-read.md)，看核心拿到 `RegTable` 里的地址后，如何经 `psi_common_axi_master_simple` 与 `m00_axi` 把寄存器值读回来——那是配置通路的「下游」。
- 之后读 [u2-l7 输出模式与 FIFO/RegTable 存储](u2-l7-output-modes-fifo.md)，把读回值如何经 `g_axis`/`g_naxis` 两个 generate 块交出（含本讲提到的 `AxiS_Rdy <= reg_rd(RegIdx_RdData_c)` 这一 RV 副作用）补全。
- 想看软件侧如何使用这些配置寄存器，直接读 [u3-l1 C 软件驱动](u3-l1-c-driver.md)，对照 `MmReader_SetRegTable`/`MmReader_SetEnable` 与本讲的两条追踪路径。
- 若打算自己加一个配置寄存器，按 [u3-l5 二次开发实践](u3-l5-extending-ip.md) 的端到端流程，同步修改 `definitions_pkg.vhd`、wrapper 位切片、文档与 C 驱动。
