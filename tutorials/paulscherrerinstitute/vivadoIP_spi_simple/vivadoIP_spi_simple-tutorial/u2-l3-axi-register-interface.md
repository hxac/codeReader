# AXI4 从接口与寄存器映射

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 AXI4 总线的五个独立通道（AR/R/AW/W/B）以及每条通道上 `VALID`/`READY` 握手的含义。
- 读懂顶层 wrapper `spi_vivado_wrp.vhd` 里那一长串 `s00_axi_*` 端口分别属于哪条通道、是什么方向。
- 理解 `psi_common_axi_slave_ipif` 如何把复杂的 AXI4 五通道协议"翻译"成简单的 IPIC 寄存器接口（`o_reg_rd` / `i_reg_rdata` / `o_reg_wr` / `o_reg_wdata`）。
- 解释寄存器读回（`reg_rdata`）为什么由两段拼接而成：一段是 wrapper 自己把写入值回环，另一段是 `spi_simple` 输出的状态/RX/level。
- 追踪一次"向 Data 寄存器写一个字"的完整路径，从 `s00_axi_aw*` / `s00_axi_w*` 通道一路走到 `spi_simple` 的 `TxWrite`。

## 2. 前置知识

本讲假设你已经学过 u2-l1（寄存器地图）和 u2-l2（`spi_simple` 核心架构）。下面几个概念在正式读源码前先用大白话过一遍。

### 2.1 AXI 总线是什么

AXI（Advanced eXtensible Interface）是 ARM 提出的片上总线协议，是 Xilinx Zynq / Versal 等 FPGA 里 PS（处理系统）和 PL（可编程逻辑）之间最常用的通信方式。你可以把它理解成 FPGA 内部的"快递系统"：CPU 想读/写某个寄存器，就通过这套总线发请求。

AXI 最显著的特点是**五条独立通道**：

| 通道 | 缩写 | 方向（从 slave 看） | 作用 |
|------|------|------|------|
| 读地址 | AR | 输入 | 主机告诉从机："我要读这个地址" |
| 读数据 | R | 输出 | 从机把读到的数据送回主机 |
| 写地址 | AW | 输入 | 主机告诉从机："我要写这个地址" |
| 写数据 | W | 输入 | 主机把要写的数据送给从机 |
| 写响应 | B | 输出 | 从机告诉主机："写完了，状态如何" |

读操作只用 AR + R 两条通道；写操作要用 AW + W + B 三条通道。

### 2.2 VALID/READY 握手

每条通道都有一对 `VALID` / `READY` 信号。这是 AXI 最核心的机制：

- 发送方把数据准备好后拉高 `VALID`；
- 接收方有能力接收时拉高 `READY`；
- **只有当 `VALID` 和 `READY` 同时为高的那个时钟上升沿，数据才真正被传走**。

这就像寄快递：寄件人（`VALID`）和快递员（`READY`）都到位，包裹才算交接。

### 2.3 本项目用的是"完整版"AXI4

你可能会看到资料里区分 AXI4、AXI4-Lite。区别在于 AXI4 支持**突发传输（burst）**——一次给一个起始地址，连续传多个数据；而 AXI4-Lite 每次只能一个字一个字传。

看 wrapper 端口里的 `s00_axi_awlen`（突发长度）、`s00_axi_awsize`（突发大小）、`s00_axi_awburst`（突发类型）、`s00_axi_wlast`（突发最后一个）、`s00_axi_wstrb`（字节写掩码），就能确认本 IP 对外是**完整 AXI4 slave**。只不过寄存器访问是一次一个字，真正的突发协议处理被下文要讲的 `psi_common_axi_slave_ipif` 包揽了。

### 2.4 什么是 IPIC

IPIC（IP Interconnect）是 Xilinx 提出的一种"寄存器级"简化接口。它的思路是：AXI 五通道协议虽然强大，但对一个只想暴露几个寄存器的 IP 来说太重了。于是用一个"协议转换器"把 AXI 翻译成最朴素的寄存器读写信号——每个寄存器一对读/写脉冲加一根数据线。本项目里，`psi_common_axi_slave_ipif` 就扮演这个转换器。

## 3. 本讲源码地图

| 文件 | 在本讲的作用 |
|------|------|
| [hdl/spi_vivado_wrp.vhd](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd) | 顶层 wrapper，全部内容都在这里：声明 AXI 端口、例化 AXI 解码器、例化 `spi_simple`、做寄存器读回拼接。 |
| [hdl/definitions_pkg.vhd](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/definitions_pkg.vhd) | 寄存器索引与位宽常量包，是 wrapper 里到处引用的 `RegIdx_*_c` / `StatusSize_c` / `IrqSize_c` 的来源。 |
| [hdl/spi_simple.vhd](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd) | SPI 核心的实体声明（只需看端口），理解 wrapper 的 port map 怎么接进去。 |

外部依赖（不在本仓库，无需深读，知道接口即可）：`psi_common_axi_slave_ipif`（协议转换器）、`psi_common_math_pkg` 提供 `log2ceil`、`psi_common_array_pkg` 提供 `t_aslv32` 数组类型。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **AXI4 通道与 wrapper 端口**——那一大堆 `s00_axi_*` 到底是什么。
2. **psi_common_axi_slave_ipif 的 IPIC 接口**——AXI 如何被翻译成寄存器信号。
3. **寄存器读回拼接逻辑**——`reg_rdata` 为什么是两段拼接。

---

### 4.1 AXI4 通道与 wrapper 端口

#### 4.1.1 概念说明

`spi_vivado_wrp` 这个实体的"对外脸面"就是两组端口：一组是 SPI 物理引脚（`spi_sck`、`spi_cs_n`、`spi_mosi`、`spi_miso` 等），另一组就是 AXI4 slave 总线（前缀统一为 `s00_axi_`）。CPU（比如 Zynq 的 ARM 核）通过这组 AXI 端口读写寄存器，从而驱动 SPI 收发。

之所以前缀叫 `s00_axi`，是 Xilinx IP 打包的命名惯例：`s00` 表示第 0 个 **s**lave 接口。打包成 IP 后，Vivado 会自动识别这一组端口为一个 AXI4 slave 总线接口。

#### 4.1.2 核心流程

一次 AXI 单字**写**事务（这是本讲实践要追踪的对象）的时序可以这样描述：

```text
  主机(Master/BFM)                    从机(spi_vivado_wrp 经 ipif)
  ────────────────                    ─────────────────────────────
  1. AW 通道：拉 awvalid + 给 awaddr ──►  解码地址
                  ◄── awready 握手
  2. W  通道：拉 wvalid  + 给 wdata  ──►  产生 o_reg_wr(i) 脉冲 + o_reg_wdata(i)
                  ◄── wready  握手
  3.                                  ──►  B 通道：拉 bvalid + bresp
                  ◄── bready 握手     (写完成应答)
```

一次单字**读**事务类似，但走 AR + R 通道，从机在 R 通道上把 `i_reg_rdata(i)` 回送出去。

注意：上面写的是"单字"流程。由于本 IP 对外是完整 AXI4，`psi_common_axi_slave_ipif` 内部还实现了突发拆解、字节掩码、`xRESP` 响应码等协议细节——这些都不在 wrapper 里，wrapper 只消费转换后的寄存器级信号。

#### 4.1.3 源码精读

**时钟与复位**——所有 AXI 操作都同步在 `s00_axi_aclk` 上，复位 `s00_axi_aresetn` 是**低有效**（名字里的 `n` = active low）：

[s00_axi_aclk / s00_axi_aresetn 声明：hdl/spi_vivado_wrp.vhd:64-65](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L64-L65) —— 这是 AXI slave 的系统时钟与低有效复位。

由于 `spi_simple` 内部用的是高有效复位，wrapper 做了一次极性翻转：

[AxiRst 翻转：hdl/spi_vivado_wrp.vhd:126-L131](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L126) —— `AxiRst <= not s00_axi_aresetn;` 把 AXI 的低有效复位翻成 `spi_simple` 需要的高有效复位。

**五通道端口**——读地址通道 AR（输入为主）：

[AR 通道：hdl/spi_vivado_wrp.vhd:67-76](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L67-L76) —— `s00_axi_araddr` 宽 8 位（地址空间 256 字节），`s00_axi_arvalid`/`s00_axi_arready` 是握手对。

读数据通道 R（输出为主）：

[R 通道：hdl/spi_vivado_wrp.vhd:78-83](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L78-L83) —— `s00_axi_rdata` 宽 32 位（每个寄存器 32 位），`s00_axi_rresp` 是 2 位响应码。

写地址通道 AW、写数据通道 W、写响应通道 B：

[AW 通道：hdl/spi_vivado_wrp.vhd:85-94](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L85-L94) —— 写地址，含 `awaddr`(8 位)、突发控制位、`awvalid`/`awready` 握手。

[W 通道：hdl/spi_vivado_wrp.vhd:96-100](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L96-L100) —— 写数据 `wdata`(32 位)、字节掩码 `wstrb`(4 位，每 bit 对应 8 位)、`wlast`、`wvalid`/`wready` 握手。

[B 通道：hdl/spi_vivado_wrp.vhd:102-105](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L102-L105) —— 写响应 `bresp`(2 位)、`bvalid`/`bready` 握手。

注意地址宽度都是 8 位，与 u1-l2 里讲到的"地址空间 256 字节、8 位地址"一致。每个寄存器占 4 字节，因此软件看到的字节地址 = `RegIdx × 4`（u2-l1 已确立）。

> 小贴士：端口方向有个口算技巧——AXI 里带 `valid` 的多是"主动方"方向：AW/W 的 valid 是输入（主机发起写），B 的 valid 是输出（从机回应写完）；AR 的 valid 是输入（主机发起读），R 的 valid 是输出（从机回数据）。`ready` 永远是相对方向。

#### 4.1.4 代码实践（源码阅读型）

**目标**：在不看答案的前提下，自己把 wrapper 端口按五通道归类，建立"看到信号名就能报出通道"的反射。

**操作步骤**：

1. 打开 [hdl/spi_vivado_wrp.vhd:64-105](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L64-L105)。
2. 画一张 5 行表格，表头为 `AR | R | AW | W | B`。
3. 把每个 `s00_axi_*` 端口填进对应通道，并标注方向（`in`/`out`）。
4. 数一下每条通道各有几个信号，验证是否与 2.1 节的五通道模型一致。

**需要观察的现象**：

- AR 通道应有 `arid/araddr/arlen/arsize/arburst/arlock/arcache/arprot/arvalid` 共 9 个输入 + `arready` 1 个输出。
- R 通道应有 `rid/rdata/rresp/rlast/rvalid` 共 5 个输出 + `rready` 1 个输入。
- B 通道最精简：`bid/bresp/bvalid` 3 个输出 + `bready` 1 个输入。

**预期结果**：你应当能确认这是一个支持 ID 标签（`C_S00_AXI_ID_WIDTH`）和突发的完整 AXI4 slave，而不是 AXI4-Lite（后者没有 `*len/*size/*burst/*last/*id/*strb`）。

#### 4.1.5 小练习与答案

**练习 1**：`s00_axi_awaddr` 是 8 位，那么这个 IP 的寻址空间是多大？为什么 `RegIdx_Data_c` 对应的字节地址是 `0x00`？

**答案**：8 位地址 → \(2^8 = 256\) 字节寻址空间。`RegIdx_Data_c = 0`（见 [definitions_pkg.vhd:33](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/definitions_pkg.vhd#L33)），字节地址 = 索引 × 4 = `0 × 4 = 0x00`。

**练习 2**：为什么 wrapper 里要写 `AxiRst <= not s00_axi_aresetn;`，而不是直接把 `s00_axi_aresetn` 接给 `spi_simple`？

**答案**：AXI 复位约定是低有效（`aresetn` 里的 `n`），而 `spi_simple` 的 `Rst` 端口是高有效（见 [spi_simple.vhd:45](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_simple.vhd#L45)）。两者极性相反，必须取反才能对接。

---

### 4.2 psi_common_axi_slave_ipif 的 IPIC 接口

#### 4.2.1 概念说明

4.1 讲的是 wrapper 的"对外脸面"。但 wrapper 自己并不实现 AXI 协议状态机——它把这件苦差事外包给了 PSI 生态里的 `psi_common_axi_slave_ipif`。这个组件把 AXI4 五通道翻译成一组扁平的、按寄存器编号索引的信号，即 Xilinx 所谓的 **IPIC（IP Interconnect）** 接口：

| IPIC 信号 | 方向（相对 ipif） | 类型 | 含义 |
|-----------|------|------|------|
| `o_reg_rd` | 输出 | 每寄存器 1 bit 的向量 | 第 i 位为高 = 正在请求读第 i 号寄存器 |
| `i_reg_rdata` | 输入 | 每寄存器 32 bit 的数组 | 用户提供的第 i 号寄存器读回数据 |
| `o_reg_wr` | 输出 | 每寄存器 1 bit 的向量 | 第 i 位为高 = 正在写第 i 号寄存器（单周期脉冲） |
| `o_reg_wdata` | 输出 | 每寄存器 32 bit 的数组 | ipif 送出的第 i 号寄存器写入数据 |

`o_` / `i_` 前缀是**从 ipif 自身视角**命名的：`o_` 是它输出的、`i_` 是它需要你输入的。所以在 wrapper 里，`o_reg_wr` 接到 wrapper 的内部信号 `reg_wr`，再喂给 `spi_simple` 的 `TxWrite` 等；`i_reg_rdata` 则由 wrapper / `spi_simple` 共同驱动。

#### 4.2.2 核心流程

转换关系可以画成下面这条链：

```text
  AXI 写 (AW+W 通道)
        │
        ▼
  psi_common_axi_slave_ipif
   - 解码 awaddr → 得到寄存器下标 i
   - 在握手完成的那拍，置 o_reg_wr(i)=1, o_reg_wdata(i)=wdata
   - 回 B 通道 bvalid+bresp
        │
        ▼
  reg_wr(i) ──► spi_simple 的写类端口 (如 TxWrite)
  reg_wdata(i) ──► spi_simple 的写类数据 (如 TxData)
```

读路径类似：ipif 解码 `araddr` 得到下标 i，置 `o_reg_rd(i)=1`，并在 R 通道把 `i_reg_rdata(i)` 送出。

#### 4.2.3 源码精读

**IPIC 信号声明**——wrapper 在架构体里声明了这四个内部信号，作为 ipif 与 `spi_simple` 之间的"中转站"：

[IPIC 信号：hdl/spi_vivado_wrp.vhd:119-123](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L119-L123) —— 注意 `reg_rdata` 和 `reg_wdata` 的类型是 `t_aslv32(0 to USER_SLV_NUM_REG-1)`，即"32 位 std_logic_vector 的数组"，每个元素对应一个寄存器。

`USER_SLV_NUM_REG` 是寄存器**槽位**数，由 u2-l1 讲过的 `RegCount_c` 向上取整为 2 的幂得到：

[USER_SLV_NUM_REG：hdl/spi_vivado_wrp.vhd:117](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L117) —— `2**log2ceil(RegCount_c)`。`RegCount_c = 10`（[definitions_pkg.vhd:55](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/definitions_pkg.vhd#L55)），\(\lceil \log_2 10 \rceil = 4\)，所以 \(2^4 = 16\) 个槽位。也就是说 IPIC 数组有 16 格，但实际只用了 0..9 这 10 格，10..15 是空槽（访问它们没有实际寄存器接驳）。这正是 u2-l1 里"硬件地址解码阵列为 16 槽、其中 10..15 为空槽"的由来。

**ipif 的 generic 配置**：

[ipif generic map：hdl/spi_vivado_wrp.vhd:136-145](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L136-L145) —— `num_reg_g => USER_SLV_NUM_REG`（16）、`use_mem_g => false`（不使用存储器模式，纯寄存器）、`axi_id_width_g => C_S00_AXI_ID_WIDTH`、`axi_addr_width_g => 8`。

**ipif 的 AXI 侧端口映射**——把所有 `s00_axi_*` 一对一接到 ipif 的 `s_axi_*`：

[ipif AXI 侧 port map：hdl/spi_vivado_wrp.vhd:152-193](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L152-L193) —— 注意前缀从 `s00_axi_`（顶层 IP 惯例）改成了 `s_axi_`（ipif 内部惯例），信号本身是同一根线。

**ipif 的寄存器侧端口映射**——这四行才是本模块的重点，AXI 与 `spi_simple` 的"接口契约"全在这：

[ipif 寄存器侧 port map：hdl/spi_vivado_wrp.vhd:197-200](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L197-L200) —— `o_reg_rd => reg_rd`、`i_reg_rdata => reg_rdata`、`o_reg_wr => reg_wr`、`o_reg_wdata => reg_wdata`。这四组信号把 AXI 世界和寄存器世界焊在一起。

> 说明：`psi_common_axi_slave_ipif` 的内部实现（突发拆解、`xRESP` 生成、字节掩码处理、握手状态机）属于外部依赖 psi_common，不在本仓库源码范围内。本讲只关心它的接口契约——这对读懂 wrapper 已经足够。

#### 4.2.4 代码实践（源码阅读型）

**目标**：通过对照 IPIC 信号和 `spi_simple` 的端口，验证"AXI 写某寄存器 = `reg_wr(i)` 脉冲 + `reg_wdata(i)` 数据"这个映射。

**操作步骤**：

1. 打开 [hdl/spi_vivado_wrp.vhd:228-255](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L228-L255)（`spi_simple` 的 port map）。
2. 找出所有形如 `=> reg_wr(...)` 的连接（写脉冲）和所有形如 `=> reg_wdata(...)` 的连接（写数据）。
3. 把它们整理成"寄存器索引 → `spi_simple` 端口"的映射表。

**需要观察的现象**：

- 写脉冲连接：`CfgIrqClrVld => reg_wr(RegIdx_IrqVec_c)`、`TxWrite => reg_wr(RegIdx_Data_c)`。
- 写数据连接：`CfgSlave`、`CfgStoreRx`、`CfgTxAlmEmpty`、`CfgRxAlmFull`、`CfgIrqClr`、`CfgIrqEna`、`TxData` 都来自 `reg_wdata(...)`。

**预期结果**：你会发现只有两个寄存器的写入会触发"脉冲型"动作——`RegIdx_Data_c`（推 TX FIFO）和 `RegIdx_IrqVec_c`（清除中断）。其余配置寄存器（SlaveNr/StoreRx/阈值/IrqEna）是"电平型"读取：`spi_simple` 持续采样 `reg_wdata` 的当前值，写入动作本身不产生边沿事件。这种区分是 u2-l6 中断逻辑和 u2-l5 FIFO 机制的关键。

#### 4.2.5 小练习与答案

**练习 1**：`RegCount_c` 是 10，为什么 IPIC 数组却开了 16 格？

**答案**：因为 `USER_SLV_NUM_REG = 2**log2ceil(RegCount_c) = 2**4 = 16`（[spi_vivado_wrp.vhd:117](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L117)）。`psi_common_axi_slave_ipif` 要求寄存器数为 2 的幂（便于地址译码），所以把 10 向上取整到 16，多出的 6 个槽位（10..15）没有实际寄存器接驳，是空槽。

**练习 2**：从 ipif 的视角看，`o_reg_wr` 是输出还是输入？`i_reg_rdata` 呢？为什么用 `o_`/`i_` 前缀而不是 `reg_wr` 这种方向中性的名字？

**答案**：`o_reg_wr` 是 ipif 的输出（它告知"发生了写"），`i_reg_rdata` 是 ipif 的输入（用户要把读回数据喂给它）。`o_`/`i_` 前缀明确表达了"相对于 ipif 这个组件的方向"，避免和 wrapper 里同名的中转信号（如 `reg_wr`）在方向上混淆。

---

### 4.3 寄存器读回拼接逻辑

#### 4.3.1 概念说明

`i_reg_rdata` 是 ipif 要求 wrapper 提供的"每个寄存器的读回值"。问题是：这些读回值从哪儿来？

仔细看会发现，本 IP 的 10 个寄存器读回数据有**两个来源**：

1. **`spi_simple` 直接驱动**：Status、RxLevel、TxLevel、IrqVec、Data(RX)。这些是动态值（FIFO 水位、忙闲、中断向量、收到的数据），必须由硬件核心实时输出。
2. **wrapper 自己回环（readback）**：SlaveNr、StoreRx、TxAlmEmptyLevel、RxAlmFullLevel、IrqEna。这些是"粘性配置"——软件上次写了什么，读回来应该还是什么。但 `spi_simple` 并没有把这些配置值再输出一遍，所以 wrapper 直接把 `reg_wdata`（刚写进来的值）接到 `reg_rdata`（读出去的值），实现"写什么读什么"。

这就是 u2-l2 提到的"1.2.2 版本支持 R/W 寄存器的 AXI 读回"在源码里的落点——4.3 这段代码正是为此而存在。

#### 4.3.2 核心流程

读回数据流向（以一次 AXI 读为例）：

```text
  AXI 读 (AR 通道, araddr=i)
        │
        ▼
  ipif 置 o_reg_rd(i)=1, 并在 R 通道索取 i_reg_rdata(i)
        │
        ▼
  reg_rdata(i) 的值由两路并行驱动：
   ├─ 路径 A（配置类）: reg_wdata(i) ──► wrapper 回环赋值 ──► reg_rdata(i)
   └─ 路径 B（动态类）: spi_simple 的输出 (Status/RxData/...) ──► reg_rdata(i)
        │
        ▼
  ipif 把 reg_rdata(i) 经 R 通道送回主机
```

#### 4.3.3 源码精读

**配置寄存器的回环**——这五行并发赋值语句把写入值直接读回，每行只取该寄存器真正用到的位段（高位不接，读回为 0）：

[register readback：hdl/spi_vivado_wrp.vhd:207-211](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L207-L211) —— 逐行含义：

- `reg_rdata(RegIdx_SlaveNr_c)(log2ceil(SlaveCnt_g)-1 downto 0) <= reg_wdata(RegIdx_SlaveNr_c)(...)`：回读从机号，位宽 = \(\lceil\log_2 \text{SlaveCnt}\_g\rceil\)。
- `reg_rdata(RegIdx_StoreRx_c)(0) <= reg_wdata(RegIdx_StoreRx_c)(0)`：回读 StoreRx 单 bit。
- `reg_rdata(RegIdx_TxAlmEmptyLevel_c)(...) <= reg_wdata(...)(...)`：回读 TX 几乎空中断阈值，位宽 \(\lceil\log_2 \text{FifoDepth}\_g\rceil + 1\)。
- `reg_rdata(RegIdx_RxAlmFullLevel_c)(...) <= ...`：回读 RX 几乎满阈值。
- `reg_rdata(RegIdx_IrqEna_c)(IrqSize_c-1 downto 0) <= ...`：回读中断使能，位宽 `IrqSize_c`（=5，见 [definitions_pkg.vhd:29](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/definitions_pkg.vhd#L29)）。

**动态寄存器由 spi_simple 驱动**——在 `i_spi` 的 port map 里，这些 `spi_simple` 输出端口直接接到 `reg_rdata` 的对应位段：

[spi_simple 输出接到 reg_rdata：hdl/spi_vivado_wrp.vhd:242-255](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L242-L255) —— 关键几行：

- `CfgIrqVec => reg_rdata(RegIdx_IrqVec_c)(IrqSize_c-1 downto 0)`：中断向量锁存值（动态，读看锁存、写按位清）。
- `Status => reg_rdata(RegIdx_Status_c)(StatusSize_c-1 downto 0)`：状态寄存器（7 位，FIFO 水位 + Busy）。
- `RxData => reg_rdata(RegIdx_Data_c)(TransWidth_g-1 downto 0)`：读 Data 寄存器时拿到的是 RX FIFO 弹出的数据。
- `RxLevel => reg_rdata(RegIdx_RxLevel_c)(...)`、`TxLevel => reg_rdata(RegIdx_TxLevel_c)(...)`：FIFO 水位。

**Data 寄存器的双语义**——把"写 Data 推 TX FIFO"和"读 Data 弹 RX FIFO"放在一起看，这是整个 IP 最巧妙的设计：

[Data 寄存器读/写分离：hdl/spi_vivado_wrp.vhd:250-254](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L250-L254) —— 同一个索引 `RegIdx_Data_c`：

- 写方向：`TxData <= reg_wdata(RegIdx_Data_c)(...)` + `TxWrite <= reg_wr(RegIdx_Data_c)` —— 写入数据推入 TX FIFO；
- 读方向：`RxData => reg_rdata(RegIdx_Data_c)(...)` + `RxAck <= reg_rd(RegIdx_Data_c)` —— 读取数据从 RX FIFO 弹出并应答。

也就是说，软件对地址 `0x00` 的"写"和"读"走的是两条完全独立的物理通路（TX 方向 / RX 方向），只是恰好共享了同一个寄存器槽位。这与 u2-l1 讲的"Data 寄存器最特殊"完全对应。

> VHDL 语法提示：`reg_rdata` 被多个进程/并发语句驱动时必须保证"同一个 bit 只有一个源"。这里配置类（回环）和动态类（`spi_simple` 输出）分别驱动**不同索引**的 `reg_rdata` 元素，互不冲突；而 `reg_wdata` 全部由 ipif 单独驱动，`spi_simple` 只是按位段读取它，属于多读者、单写者，没有多驱动冲突。

#### 4.3.4 代码实践（贯穿实践·重点）

**目标**：把本讲三个模块串起来，追踪一次"向 Data 寄存器写一个字（例如 `0xAB`）"的完整信号路径，从 AXI 通道一直到 `spi_simple` 的 `TxWrite`。

**操作步骤**：

1. **找到测试激励**。打开 [tb/top_tb.vhd](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd)，定位 BFM 调用 `axi_single_write(RegIdx_Data_c*4, 16#AB#, axi_ms, axi_sm, aclk);`（[top_tb.vhd:184](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L184)）。这就是"写 Data 寄存器 = 0xAB"的激励，地址 `RegIdx_Data_c*4 = 0*4 = 0x00`。
2. **走 AXI 五通道**。BFM `axi_ms` 会在 AW 通道发 `awvalid + awaddr=0x00`、在 W 通道发 `wvalid + wdata=0x000000AB`，wrapper 端口 [s00_axi_aw* / s00_axi_w*：hdl/spi_vivado_wrp.vhd:85-100](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L85-L100) 收到这些信号。
3. **进入 ipif**。端口映射 [ipif AXI 侧：hdl/spi_vivado_wrp.vhd:172-188](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L172-L188) 把 `s00_axi_aw*/w*` 接到 ipif 的 `s_axi_aw*/w*`。
4. **ipif 输出寄存器级信号**。ipif 解码 `awaddr=0x00` 得到下标 `i=0`（即 `RegIdx_Data_c`），握手完成后置 `o_reg_wr(0)=1` 一拍、`o_reg_wdata(0)=0xAB`。
5. **进入 wrapper 中转信号**。[ipif 寄存器侧映射：hdl/spi_vivado_wrp.vhd:199-200](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L199-L200) 把 `o_reg_wr/o_reg_wdata` 接到内部信号 `reg_wr/reg_wdata`。于是 `reg_wr(RegIdx_Data_c)` 出现一个单周期脉冲，`reg_wdata(RegIdx_Data_c)` 在该拍等于 `0xAB`。
6. **到达 spi_simple**。[TxWrite/TxData 映射：hdl/spi_vivado_wrp.vhd:253-254](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L253-L254) 把 `reg_wr(RegIdx_Data_c)` 接到 `TxWrite`、`reg_wdata(RegIdx_Data_c)(TransWidth_g-1 downto 0)` 接到 `TxData`。`spi_simple` 在 `TxWrite` 上升沿把 `TxData` 连同当前粘性配置（SlaveNr、StoreRx）拼成命令字推入命令 FIFO（u2-l2 已讲）。
7. **写响应回送**。ipif 在 B 通道发 `bvalid + bresp=0b00`（OKAY 响应），wrapper 端口 [s00_axi_b*：hdl/spi_vivado_wrp.vhd:102-105](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L102-L105) 把它送回 BFM，`axi_single_write` 过程返回，一次写事务结束。

**需要观察的现象**：

- 信号名前缀的两次转换：`s00_axi_*`（顶层）→ `s_axi_*`（ipif 内部）→ `reg_wr/reg_wdata`（寄存器级）→ `TxWrite/TxData`（`spi_simple` 端口）。
- `TxWrite` 是**单周期脉冲**：只在 ipif 完成握手的那一拍为高，下一拍自动回落。`spi_simple` 必须在这一拍采样 `TxData`。
- 数据位宽被切片：`reg_wdata(RegIdx_Data_c)` 是 32 位，但接到 `TxData` 时只取低 `TransWidth_g` 位（[hdl/spi_vivado_wrp.vhd:253](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L253)）。

**预期结果**：你能画出下面这条完整信号链（写 `0xAB` 到 Data 寄存器）：

```text
axi_single_write(0x00, 0xAB)
  → s00_axi_awaddr=0x00, s00_axi_wdata=0xAB
  → [ipif 解码] reg_wr(0)=1 (一拍), reg_wdata(0)=0xAB
  → TxWrite=1, TxData=0xAB (取低 TransWidth_g 位)
  → spi_simple 命令 FIFO 推入一条命令
  → (B 通道回 bresp=OKAY)
```

> 待本地验证：上述时序的精确拍数（从 `awvalid` 拉高到 `TxWrite` 出现脉冲）依赖 `psi_common_axi_slave_ipif` 的内部实现，建议在 Modelsim 里跑一次 [sim/run.tcl](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/sim/run.tcl)，在 `TxWrite` 信号上加断点或波形游标，观察从 `axi_single_write` 调用到 `TxWrite` 翻转实际经过了几个 `s00_axi_aclk` 周期。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `reg_rdata(RegIdx_SlaveNr_c)` 由 wrapper 显式回环赋值（[spi_vivado_wrp.vhd:207](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L207)），而 `reg_rdata(RegIdx_Status_c)` 不需要？

**答案**：因为 `spi_simple` 把 Status 作为输出端口持续驱动（`Status => reg_rdata(RegIdx_Status_c)(...)`，[spi_vivado_wrp.vhd:247](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L247)），是动态值，由核心实时提供。而 SlaveNr 是输入配置（`CfgSlave <= reg_wdata(...)`），`spi_simple` 只消费不回吐，所以必须由 wrapper 把 `reg_wdata` 回环到 `reg_rdata`，软件才能读到上次写入的从机号。

**练习 2**：软件对地址 `0x00`（Data 寄存器）先写 `0xAB`、紧接着读一次，读到的值是 `0xAB` 吗？为什么？

**答案**：通常**不是**。写 `0x00` 走 TX 方向（`TxWrite`/`TxData`，把 `0xAB` 推入命令 FIFO 发往 SPI 总线）；读 `0x00` 走 RX 方向（`RxData`/`RxAck`，从响应 FIFO 弹出的是 SPI 从机返回的数据）。两条通路物理独立，读到的不是刚写进去的那个数，而是 MISO 上收到的响应。这正是 u2-l1 讲的 Data 寄存器"写推 TX、读弹 RX"双语义的体现。

---

## 5. 综合实践

**任务**：为 `spi_vivado_wrp` 的 AXI → `spi_simple` 路径画一张完整的"寄存器级接线表"，并把 4.3.4 的写路径扩展成"写后读"的完整时序图。

**具体要求**：

1. **接线表**：列一张三列表格，表头为 `寄存器(索引/地址) | reg_wr/reg_rd/reg_wdata/reg_rdata 中用到的信号 | 对应的 spi_simple 端口`。遍历全部 10 个寄存器（Data/Status/RxLevel/TxLevel/SlaveNr/StoreRx/TxAlmEmptyLevel/RxAlmFullLevel/IrqVec/IrqEna），逐一填出它们在 [hdl/spi_vivado_wrp.vhd:207-264](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/hdl/spi_vivado_wrp.vhd#L207-L264) 中接到了哪些 IPIC 信号和 `spi_simple` 端口。
2. **分类标注**：在表里标注每个寄存器属于"回环配置类"（4.3.3 第一段）、"动态状态类"（`spi_simple` 输出）、还是"双语义 Data 类"。
3. **时序图**：参考 [top_tb.vhd:181-189](https://github.com/paulscherrerinstitute/vivadoIP_spi_simple/blob/fda4db7a10b98ac138f37decc5211130dc425ada/tb/top_tb.vhd#L181-L189) 的测试场景（写 SlaveNr、写 StoreRx、写 Data、读 Status），画出对应的 AXI 五通道波形草图，重点标出 `awvalid/awready`、`wvalid/wready`、`bvalid/bready`、以及内部 `reg_wr(RegIdx_Data_c)` 脉冲的出现时刻。

**验收标准**：你的接线表应当能回答下面三个问题而不需要再翻源码——

- 写哪个寄存器会触发 `spi_simple` 的脉冲型输入？（答：Data → `TxWrite`、IrqVec → `CfgIrqClrVld`）
- 哪些寄存器读回的是"写什么读什么"？（答：SlaveNr/StoreRx/TxAlmEmptyLevel/RxAlmFullLevel/IrqEna）
- 哪些寄存器读回的是硬件实时状态？（答：Status/RxLevel/TxLevel/IrqVec/Data）

---

## 6. 本讲小结

- wrapper `spi_vivado_wrp` 对外暴露一组前缀为 `s00_axi_` 的 **AXI4 slave** 端口，覆盖五通道（AR/R/AW/W/B），是支持 ID 和突发的完整 AXI4（非 Lite）。
- 真正的 AXI 协议处理外包给 `psi_common_axi_slave_ipif`，它把五通道翻译成扁平的 **IPIC 寄存器接口**：`o_reg_rd` / `i_reg_rdata` / `o_reg_wr` / `o_reg_wdata`，按寄存器下标索引。
- 寄存器槽位数 `USER_SLV_NUM_REG = 2**log2ceil(RegCount_c) = 16`（10 实际使用 + 6 空槽），这就是 8 位地址、256 字节空间的由来。
- 寄存器读回 `reg_rdata` 由两路拼接：**配置类**（SlaveNr/StoreRx/阈值/IrqEna）由 wrapper 把 `reg_wdata` 回环；**动态类**（Status/RxLevel/TxLevel/IrqVec）由 `spi_simple` 输出端口驱动。
- **Data 寄存器是双语义**：同一地址 `0x00`，写走 TX 通路（`TxWrite`/`TxData` 推命令 FIFO），读走 RX 通路（`RxData`/`RxAck` 弹响应 FIFO），两条路径物理独立。
- 复位有极性翻转 `AxiRst <= not s00_axi_aresetn`，把 AXI 低有效复位转成 `spi_simple` 高有效复位。

## 7. 下一步学习建议

- **u2-l4（SPI 主控时序与引擎集成）**：本讲停在 `TxWrite`/`TxData` 进入 `spi_simple`。下一步看 `spi_simple` 如何把这些命令交给 `psi_common_spi_master`，以及 `ClockDivider_g` / `SpiCPOL_g` / `SpiCPHA_g` 如何决定 SCK 波形。
- **u2-l5（FIFO 缓冲与背压机制）**：深入命令 FIFO / 响应 FIFO，理解 `TxLevel` / `RxLevel` / almost empty/full 与本讲 Status 寄存器读回值的关系。
- **u2-l6（中断向量与状态机制）**：本讲提到的 `CfgIrqClr` / `CfgIrqClrVld` / `CfgIrqVec` / `CfgIrqEna` 四个 IRQ 相关端口，在那里会完整解释锁存、按位清除与自动重置。
- **u3-l4（IP 打包与发布流程）**：想了解 `s00_axi_*` 这组端口如何被 `component.xml` 声明为 AXI4 slave 总线接口、`C_S00_AXI_ID_WIDTH` 如何在 Block Design 里自动传播，进入打包讲义。
