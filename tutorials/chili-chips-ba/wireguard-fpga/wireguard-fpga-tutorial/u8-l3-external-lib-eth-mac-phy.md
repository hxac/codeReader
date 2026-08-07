# 外部库 verilog-ethernet/axis 与以太网 MAC/PHY

## 1. 本讲目标

本讲聚焦 wireguard-fpga 数据面最底层的「物理接入层」——把四路千兆以太网线上的串行字节流，变成片内 DPE 能处理的 128 位 AXIS 数据包，再变回线上字节流。

学完后你应当能够：

- 认清本项目复用了 Alex Forencich 的两个开源 IP 库（`verilog-axis`、`verilog-ethernet`），并能从 `top.filelist` 中区分「外部库文件」与「自研文件」。
- 理解自研模块 `ethernet_mac` 如何把外部库的 `eth_mac_1g_gmii_fifo` 包成项目自己的接口，以及 MAC 在 GMII 上完成的成帧、FCS（帧校验）与填充（padding）。
- 读懂外部库的 `axis_async_fifo_adapter` 如何「一器三用」——同时完成跨时钟域、128↔8 位宽转换、整包存储转发。
- 看清 PHY 控制器 `ethernet_phy` 的现状：README 描述了「初始化 Realtek PHY、监测 link up/down」的设计意图，但当前 HEAD 的 RTL 是占位符，MDIO 实际尚未驱动（仅仿真侧有 BFM 验证）。

## 2. 前置知识

### 2.1 GMII——千兆以太网的并行接口

PHY（物理层芯片，本板为 Realtek）把线上的模拟信号还原成数字字节后，通过 **GMII（Gigabit Media Independent Interface）** 交给 MAC。GMII 在 1 Gbps 模式下是 8 位数据 `rxd/txd[7:0]` 加若干控制信号（`rx_dv`/`tx_en` 表示数据有效、`rx_er`/`tx_er` 表示错误），在 **125 MHz** 时钟下逐拍传输。算一下：

\[ 8 \text{ bit} \times 125 \text{ MHz} = 1000 \text{ Mbps} = 1 \text{ Gbps} \]

这正是「8 位 @ 125 MHz = 1 Gbps」的由来（见 [u2-l3](u2-l3-clock-reset-domains.md)）。

### 2.2 以太网帧结构、前导码与 FCS

一个标准以太网帧在线上是这样的：

```
前导码(7B, 0x55..) | SFD(1B, 0xD5) | 目的MAC | 源MAC | 类型/长度 | 载荷 | FCS(4B)
```

- **前导码 + SFD（Start of Frame Delimiter）**：让接收端做时钟/字节对齐，并以 `0xD5` 标记「下一字节就是目的 MAC」。
- **FCS（Frame Check Sequence）**：4 字节的 **CRC-32** 校验，覆盖「目的 MAC … 载荷」整段。发送方算出 CRC 追加在末尾，接收方重算并比对，不一致就丢弃——这是链路层完整性保障。

> 名词：CRC-32 是一种基于多项式除法的循环冗余校验码，标准以太网用多项式 \( x^{32}+x^{26}+\dots+1 \)（0x04C11DB7），初值与终值都取全 1，按字节最低位先发。本项目外部库用 `eth_crc_8` 这一逐字节 LFSR（线性反馈移位寄存器）模块实现它。

- **填充（padding）**：以太网规定最小帧长 64 字节（含 FCS）。短帧要补 0，以免冲突检测窗口内帧过短。

### 2.3 AXI-Stream（AXIS）总线

AXIS 是 Xilinx 定义的「点对点流式」总线，本项目核心数据通路就是它的变体（见 [u4-l1](u4-l1-dpe-overview-axis.md)）。一组信号同拍 `tvalid=1 && tready=1` 即完成一次握手（一个 beat）。`tdata` 是数据、`tkeep` 是字节使能、`tlast` 标记包尾。

### 2.4 MDIO——管理 PHY 的两线串口

**MDIO（Management Data Input/Output）** 是 STA（Station，这里指 FPGA/MAC）访问 PHY 寄存器的两线总线：`MDC`（时钟）+ `MDIO`（双向数据）。一次 **Clause 22** 事务为：32 位前导（全 1）+ 起始位 + 操作码（读/写）+ 5 位 PHY 地址 + 5 位寄存器地址 + 2 位 turnaround + 16 位数据。CPU 藉此配置 PHY 的速率/自协商，并读状态寄存器的「link up/down」位来监测链路。这套概念在本讲里先建立直觉。

### 2.5 IP 复用：站在巨人肩膀上

MAC、FIFO、CRC 这些是标准协议电路，业界已有成熟开源实现。本项目不重复造轮子，而是直接复用 Alex Forencich 的两个高质量库（README **[Ref9]** 列出），自研代码只做「适配与封装」。理解「哪一层是借来的、哪一层是自己写的」是本讲的骨架。

## 3. 本讲源码地图

| 文件 | 角色 | 自研/外部 |
|------|------|-----------|
| `1.hw/top.filelist` | 源文件清单，按分组标注外部库与自研 | 自研 |
| `1.hw/top.sv` | 顶层，实例化 4 个 `ethernet_mac` + 1 个 `ethernet_phy` | 自研 |
| `1.hw/ip.infra/dpe_if.sv` | 项目自己的 128 位 AXIS 接口定义 | 自研 |
| `1.hw/ip.infra/ethernet_mac.sv` | **MAC 封装层**：把外部 `eth_mac_1g_gmii_fifo` 包成 `dpe_if` | 自研 |
| `1.hw/ip.infra/ethernet_phy.sv` | **PHY 控制层**（当前为占位符） | 自研 |
| `1.hw/external_lib/ethernet/eth_mac_1g_gmii_fifo.v` | 外部库顶层：MAC + TX/RX 异步 FIFO 适配器 | **外部 verilog-ethernet** |
| `1.hw/external_lib/ethernet/eth_mac_1g_gmii.v` | 外部库 MAC：内含 PHY 接口 + `eth_mac_1g` | **外部 verilog-ethernet** |
| `1.hw/external_lib/ethernet/eth_mac_1g.v` | 外部库 MAC 核心：例化 `axis_gmii_rx/tx` | **外部 verilog-ethernet** |
| `1.hw/external_lib/ethernet/axis_gmii_rx.v` | 外部库：RX 侧成帧 + FCS 校验 | **外部 verilog-ethernet** |
| `1.hw/external_lib/ethernet/axis_gmii_tx.v` | 外部库：TX 侧成帧 + 填充 + FCS 追加 | **外部 verilog-ethernet** |
| `1.hw/external_lib/axis/axis_async_fifo_adapter.v` | 外部库：跨时钟域 + 位宽转换 + 帧存储转发 | **外部 verilog-axis** |

> 一句话地图：**自研薄封装（`ethernet_mac`/`ethernet_phy`）→ 外部 MAC 核（`eth_mac_1g_gmii_fifo` 一族）→ GMII 物理引脚**。自研代码只做接口翻译，真正的以太网协议处理全部来自外部库。

## 4. 核心概念与源码讲解

### 4.1 外部库复用：verilog-axis 与 verilog-ethernet

#### 4.1.1 概念说明

数据面要处理以太网，标准做法是分两层借力：

- **verilog-ethernet**（[Ref9](https://github.com/chili-chips-ba/verilog-ethernet)）：提供从 10M 到 10G 的完整 MAC/PHY IP——成帧（`axis_gmii_rx/tx`）、FCS 计算（`eth_crc_8`）、ARP/IP 协议栈、各种速率的 MAC（`eth_mac_1g`、`eth_mac_10g` 等）。
- **verilog-axis**：配套的 AXIS 总线基础设施——异步 FIFO（`axis_async_fifo`）、位宽/时钟适配器（`axis_async_fifo_adapter`）、寄存器（`axis_register`）、多路复用器（`axis_mux`）等。

本项目不需要全部，只从两个库里「按需挑文件」编入综合，挑出来的清单就写在 `top.filelist` 里。复用开源库的好处是省去自行验证标准协议正确性的成本（CRC、帧间距、前导码这些细节极容易写错），坏处是要承担「别人的接口风格不一定贴合本项目」的适配工作——这正是 `ethernet_mac.sv` 存在的理由。

#### 4.1.2 核心流程

`top.filelist` 用注释把源文件分成几组，外部库两组、自研若干组：

```
External AXIS library      → 7 个 .v（FIFO/adapter/register 等）
External Ethernet library  → 10 个 .v（MAC、GMII PHY 接口、CRC 等）
Common packages/interfaces → 自研的 dpe_if/soc_if 等
Common infrastructure      → 自研的 ethernet_mac/ethernet_phy 等
```

判断一个模块是「外部」还是「自研」的判据很简单：**路径在 `external_lib/` 下即外部库**（Verilog-2001 语法、MIT 许可、Alex Forencich 版权头）；在 `ip.infra/` 下即自研（SystemVerilog、Chili.CHIPS BSD 版权头）。

注意：外部库两个目录其实各含几十个文件（`verilog-axis` 有 45+ 个、`verilog-ethernet` 有 40+ 个），但本项目只编入真正被例化的那一小撮——综合器只看 `top.filelist` 列出的文件，未列出的不会进设计。

#### 4.1.3 源码精读

**外部 AXIS 库组**（7 个文件）——[1.hw/top.filelist:13-21](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.filelist#L13-L21)：本讲最关键的是 `axis_async_fifo_adapter.v`（MAC 内部要用），`sync_reset.v` 是 u2-l3 提到的复位同步器。

**外部以太网库组**（10 个文件）——[1.hw/top.filelist:22-33](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.filelist#L22-L33)：注意这里**只挑了 1G GMII 相关**（`eth_mac_1g_gmii_fifo`、`eth_mac_1g_gmii`、`eth_mac_1g`、`gmii_phy_if`、`axis_gmii_rx/tx`）加几个底层 I/O 原语（`ssio_sdr_in/out`、`oddr`、`lfsr`）。10G、RGMII、ARP/IP 栈等库内其余文件一律未编入——因为本板只用 1G GMII。

**自研基础设施组**——[1.hw/top.filelist:46-58](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.filelist#L46-L58)：与本讲相关的两个自研文件是 `ethernet_mac.sv`（L47）与 `ethernet_phy.sv`（L48），它们是「自研封装层」，紧挨着外部 MAC 库。

对比一眼两类文件的「气质」差异：

```verilog
// 外部库：Verilog-2001，wire/always @(posedge)，MIT 许可
module eth_mac_1g_gmii #(...) (input wire gtx_clk, ...);  // eth_mac_1g_gmii.v:14
```

```systemverilog
// 自研：SystemVerilog，logic/interface/modport，BSD 许可
module ethernet_mac (input logic gtx_clk, ..., dpe_if.s_axis tx_fifo); // ethernet_mac.sv:43
```

这种「自研 SV 封装 + 外部 V2001 内核」的分层，是本项目接入第三方 IP 的通用范式。

#### 4.1.4 代码实践

**实践目标**：亲手把 `top.filelist` 中的外部库文件与自研文件分类。

**操作步骤**：

1. 打开 [1.hw/top.filelist](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.filelist)。
2. 为每个 `.v`/`.sv` 文件标注三列：**所属目录**（`external_lib/axis`、`external_lib/ethernet`、`ip.infra`、`ip.dpe`…）、**语法**（SystemVerilog `.sv` 还是 Verilog `.v`）、**自研/外部**。
3. 统计：外部库共编入多少个文件？axis 几个、ethernet 几个？

**需要观察的现象**：

- 所有 `external_lib/` 下的文件都是 `.v`（Verilog-2001），所有 `ip.infra/` 自研文件都是 `.sv`。
- 外部以太网库只编入 `eth_mac_1g_*` 系列，没有 `eth_mac_10g.*` 或 `arp.v`、`ip.v`——印证「只挑 1G GMII」。

**预期结果**：外部 axis 库 7 个、外部 ethernet 库 10 个，合计 17 个外部文件；`ethernet_mac.sv`/`ethernet_phy.sv` 是唯二与以太网接入相关的自研封装。

#### 4.1.5 小练习与答案

**练习 1**：库里有 `eth_mac_1g_rgmii.v`，为什么本项目不用它而用 `eth_mac_1g_gmii.v`？

> **答案**：本板 PHY 走的是 **GMII** 接口（8 位并行 @125 MHz，见 top.sv 的 `e1_rxd[7:0]` 等端口），不是 RGMII（4 位 DDR）。top.filelist 只编入与板子实际接口匹配的 `eth_mac_1g_gmii*`，避免引入未使用的模块。

**练习 2**：`arp.v`、`ip.v` 这些库文件在仓库里存在却没编入 `top.filelist`，会进入综合吗？

> **答案**：不会。综合器只编译 `top.filelist`（及它 `+incdir`/include 的头）列出的文件。ARP/IP 协议栈在本项目交由软件控制面处理（见 [u6-l3](u6-l3-network-stack-cli.md)），数据面 MAC 只做 L2 成帧，故不需要这些库文件。

---

### 4.2 以太网 MAC：GMII 收发、FCS 与位宽桥接

#### 4.2.1 概念说明

MAC（Media Access Controller）是数据链路层的硬件实现。在本项目里它负责三件事：

1. **成帧**：发送时加上前导码 + SFD；接收时检测 SFD 并剥离。
2. **FCS**：发送方追加 4 字节 CRC-32；接收方校验，错误帧标记 `error_bad_fcs`。
3. **填充与最小帧长**：短载荷补 0 到 64 字节（不含 FCS 为 60，算 FCS 共 64）。

但本项目对外暴露的不是 GMII，而是片内统一的 128 位 AXIS（`dpe_if`）。所以自研模块 `ethernet_mac` 的职责是**适配**：把 128 位/80 MHz 的 `dpe_if` 翻译成外部 MAC 核要的接口，再让外部 MAC 核去处理 GMII 物理引脚。README 对数据面接入层的描述「_1G MAC_ - execution of the 1G Ethernet protocol (framing, flow control, FCS, etc.)」「_Rx/Tx FIFOs_ - clock domain crossing, bus width conversion, and store & forward packet handling」（[README.md:131-141](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/README.md#L131-L141)）正是这两层的分工。

#### 4.2.2 核心流程

整个 MAC 子系统的数据流（以 1 路为例，共 4 路对称）：

```
                     ┌──────────── ethernet_mac (自研封装) ────────────┐
   DPE (sys_clk,     │  dpe_if tx_fifo ──►  eth_mac_1g_gmii_fifo ──►  │  GMII TX 引脚
   128bit, 80MHz) ◄──┤                          (外部 MAC + 双 FIFO)   │  e1_txd/tx_en
                     │  dpe_if rx_fifo ◄──                          ◄──│  GMII RX 引脚
                     └─────────────────────────────────────────────────┘  e1_rxd/rx_dv
```

外部 `eth_mac_1g_gmii_fifo` 内部其实是「三明治」结构：

```
logic_clk(80M,128bit) ─[TX FIFO adapter]→ tx_clk(125M,8bit) →[GMII TX: 成帧+FCS]→ 线
线 →[GMII RX: 成帧+FCS校验]→ rx_clk(125M,8bit) ─[RX FIFO adapter]→ logic_clk(80M,128bit)
```

其中两个 **`axis_async_fifo_adapter`** 是本讲的技术核心，它「一器三用」：

| 功能 | TX 侧 | RX 侧 |
|------|--------|--------|
| 跨时钟域 | logic_clk(80M) → tx_clk(125M) | rx_clk(125M) → logic_clk(80M) |
| 位宽转换 | 128 bit → 8 bit（16:1 抽取） | 8 bit → 128 bit（1:16 聚合） |
| 存储转发 | `TX_FRAME_FIFO=1`：整包缓存，可整体丢弃坏/超长帧 | `RX_FRAME_FIFO=1`：同上 |

两侧吞吐都富余：logic 侧 128×80M≈10 Gbps，GMII 侧 8×125M=1 Gbps，都 ≥ 1 Gbps 线速，FIFO 不会成为单口瓶颈。这正是 u2-l3 所说的「蓝（125M@8bit）绿（80M@128bit）之间用过桥 FIFO」在本讲里看到的实体——而这个「桥」就来自外部 `verilog-axis` 库。

FCS 处理在更内层的 `axis_gmii_rx/tx`：

- **TX（`axis_gmii_tx`）**：状态机 `STATE_IDLE→PREAMBLE→DATA→(PAD)→FCS`，发送前导+SFD，逐字节喂 `eth_crc_8` 累加 CRC，载荷不足则进 `STATE_PAD` 补 0，最后进 `STATE_FCS` 把 CRC 取反映码作为 4 字节 FCS 追加（见 [axis_gmii_tx.v](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/external_lib/ethernet/axis_gmii_tx.v) 中 `STATE_PAD`/`STATE_FCS` 与 `MIN_FRAME_LENGTH-4-1`）。
- **RX（`axis_gmii_rx`）**：同步检测 SFD（0xD5），随后逐字节累加 CRC，到帧尾取最后 4 字节与算出的 `~crc_next` 比对，相等则 FCS 正确、否则置 `error_bad_fcs`。

#### 4.2.3 源码精读

**自研封装 `ethernet_mac`**——[1.hw/ip.infra/ethernet_mac.sv:43-58](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/ethernet_mac.sv#L43-L58)：端口左半是 GMII 物理信号（`gtx_clk`、`gmii_rxd[7:0]`、`gmii_tx_en`…），右半是两个 `dpe_if`——`tx_fifo`（`s_axis`，DPE 发给 MAC）与 `rx_fifo`（`m_axis`，MAC 收到后给 DPE）。注意它的描述写着 "Triple-Speed Ethernet Wrapper"，但参数固定为 1G。

**关键参数化**——[1.hw/ip.infra/ethernet_mac.sv:59-72](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/ethernet_mac.sv#L59-L72)：这是「自研封装层」的核心动作——把外部库默认的 `AXIS_DATA_WIDTH` 从 8 改成 **128**、`AXIS_KEEP_WIDTH` 改成 **16**，并设 `TX/RX_FIFO_DEPTH=4096`、`TX/RX_FRAME_FIFO=1`、`ENABLE_PADDING=1`、`MIN_FRAME_LENGTH=64`。外部库本身支持任意位宽，自研层只负责「喂对本项目要的那组参数」。

**把 dpe_if 的线接到外部核**——[1.hw/ip.infra/ethernet_mac.sv:74-90](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/ethernet_mac.sv#L74-L90)：

```systemverilog
.logic_clk(tx_fifo.clk),   .logic_rst(tx_fifo.rst),  // logic_clk = sys_clk(80M)
.tx_axis_tdata(tx_fifo.tdata), .tx_axis_tkeep(tx_fifo.tkeep), ...
.rx_axis_tdata(rx_fifo.tdata), .rx_axis_tkeep(rx_fifo.tkeep), ...
```

这里把 `dpe_if` 的扁平信号（`tdata/tkeep/tvalid/...`）逐根接到外部核的 `tx_axis_*`/`rx_axis_*` 上。注意 `tx_axis_tuser('0)`——发送侧永远不标坏帧，而 `rx_axis_tuser()` 悬空（MAC 把坏帧直接在 FIFO 里丢掉了，见 `USER_BAD_FRAME_VALUE` 配置）。`logic_clk` 接的是 `tx_fifo.clk`，而 `tx_fifo` 在 top.sv 里挂在 `sys_clk`（80 MHz），见下文。

**外部核 `eth_mac_1g_gmii_fifo` 的「三明治」**——[1.hw/external_lib/ethernet/eth_mac_1g_gmii_fifo.v:238-296](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/external_lib/ethernet/eth_mac_1g_gmii_fifo.v#L238-L296) 是 TX FIFO 适配器，[1.hw/external_lib/ethernet/eth_mac_1g_gmii_fifo.v:298-356](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/external_lib/ethernet/eth_mac_1g_gmii_fifo.v#L298-L356) 是 RX FIFO 适配器。TX 侧参数 `S_DATA_WIDTH(AXIS_DATA_WIDTH)→M_DATA_WIDTH(8)`（[L240-L243](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/external_lib/ethernet/eth_mac_1g_gmii_fifo.v#L240-L243)），RX 侧反过来 `S_DATA_WIDTH(8)→M_DATA_WIDTH(AXIS_DATA_WIDTH)`（[L300-L302](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/external_lib/ethernet/eth_mac_1g_gmii_fifo.v#L300-L302)）。TX 的 `s_clk(logic_clk)`/`m_clk(tx_clk)`、RX 的 `s_clk(rx_clk)`/`m_clk(logic_clk)` 说明两侧是异步时钟域——这正是 CDC 的来源。

**MAC 核本体**——[1.hw/external_lib/ethernet/eth_mac_1g_gmii_fifo.v:197-236](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/external_lib/ethernet/eth_mac_1g_gmii_fifo.v#L197-L236)：例化 `eth_mac_1g_gmii`，它再向下例化 `gmii_phy_if`（DDR/时钟原语适配）与 `eth_mac_1g`（内含 `axis_gmii_rx/tx`）。整个调用链：`ethernet_mac`(自研) → `eth_mac_1g_gmii_fifo` → `eth_mac_1g_gmii` → `eth_mac_1g` → `axis_gmii_rx/tx` + `eth_crc_8`。

**FCS 校验逻辑**——[1.hw/external_lib/ethernet/axis_gmii_rx.v:209-216](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/external_lib/ethernet/axis_gmii_rx.v#L209-L216)：`{...4字节...} == ~crc_next` 为真则「FCS good」通过，否则 `error_bad_fcs_next = 1'b1` 丢帧。`~crc_next` 的取反是 CRC-32「终值异或全 1」惯例的体现。

#### 4.2.4 代码实践

**实践目标**：说明 `ethernet_mac` 如何把 128 位 AXIS 桥接到 GMII 8 位物理线，并核对两侧时钟。

**操作步骤**：

1. 在 [ethernet_mac.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/ethernet_mac.sv) 中，确认 `.logic_clk(tx_fifo.clk)`。
2. 在 [top.sv:141](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv#L141) 确认 `from_eth_1` 接的是 `dpe_if (.clk(sys_clk) ...)`，即 `tx_fifo.clk = sys_clk`。
3. 在 [top.sv:261-275](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv#L261-L275) 确认 `u_eth_1` 的 `.gtx_clk(eth_gtx_clk)`（125 MHz）与 GMII 引脚 `e1_rxd/e1_txd`。
4. 画出 TX 方向：`to_eth_1`(128bit@sys_clk) → `axis_async_fifo_adapter` → 8bit@tx_clk(125M) → `axis_gmii_tx`(加前导+FCS) → GMII `e1_txd`。

**需要观察的现象**：

- `dpe_if` 侧时钟是 `sys_clk`（80 MHz），GMII 侧是 `eth_gtx_clk`（125 MHz），二者异步——这正是 FIFO adapter 要跨的域。
- 位宽 128→8 的转换比是 16:1，与 u2-l3「蓝绿 16:1」一致。

**预期结果**：一条完整桥接链清晰可读——`ethernet_mac` 自研层只做参数化与信号搬线，真正的 CDC+位宽转换+成帧+FCS 全在外部库 `eth_mac_1g_gmii_fifo` 内部完成。

#### 4.2.5 小练习与答案

**练习 1**：为什么发送侧 `tx_axis_tuser` 接 `'0`，而接收侧 `rx_axis_tuser` 悬空？

> **答案**：`tuser=1` 在本库约定为「坏帧」标记（见 `USER_BAD_FRAME_VALUE=1`）。发送侧 DPE 发出的永远是正常帧，故恒接 0；接收侧 MAC 已在 FIFO 里把坏帧（含 FCS 错）整包丢弃（`RX_DROP_BAD_FRAME=1`），所以不会有坏帧冒到 `dpe_if`，`rx_axis_tuser` 对 DPE 无意义而悬空。

**练习 2**：如果把 `AXIS_DATA_WIDTH` 改回外部库默认的 8，系统还能工作吗？会有什么后果？

> **答案**：功能上 MAC 仍能收发，但 `dpe_if` 是 128 位接口，二者位宽不匹配无法直连——必须额外加一层位宽适配，或者整条 DPE 数据通路都改成 8 位（与 128 位 64 级加密流水线设计冲突）。所以 128 是为了对接 DPE 而特意选的，不能改回 8。

---

### 4.3 PHY 控制与 MDIO：设计意图与当前占位符现状

#### 4.3.1 概念说明

PHY 芯片（本板 Realtek）上电后的默认工作模式未必是「1G 全双工、自协商」。系统通常需要通过 MDIO 总线**初始化** PHY（写控制寄存器设速率/双工/自协商）并**周期性轮询**状态寄存器，把「link up/down」事件上报给软件。README 对数据面接入层的第一项就是：

> _PHY Controller_ - initial configuration of Realtek PHYs and monitoring link activity (link up/down events)（[README.md:130](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/README.md#L130)）

这是**设计意图**。但要看清本讲的现实：当前 HEAD 的 `ethernet_phy.sv` **并未实现**这套 MDIO 驱动——它是一个占位符（placeholder）。这正是项目「Phase1 PoC」现状在接入层的具体体现：先把 MAC 与 DPE 的明文直通链路打通，PHY 依赖其默认配置能 link up 即可，软件化的 MDIO 初始化与 link 监测留待后续。

#### 4.3.2 核心流程

按设计意图，一个完整的 PHY 控制器应当：

1. 上电后给 PHY 一个复位脉冲（拉低 `reset_n` 一段时间再释放）。
2. 经 MDIO（Clause 22）写 PHY 控制寄存器：使能自协商、强制 1G 全双工等。
3. 周期性读 PHY 状态寄存器，解析「link status」位，将 link up/down 通过 CSR 上报软件。

一次 Clause 22 读事务的位流（STA 主机发起）：

```
前导(32×1) | START(01) | OP(10=读) | PHYAD(5bit) | REGAD(5bit) | TA(2bit) | DATA(16bit)
```

写事务把 OP 换成 `01`、TA 换成 `10`。MDIO 是半双工，数据在 `MDC` 上升沿采样。本项目四口 PHY 各有一组独立的 `reset/mdc/mdio` 引脚（共 4×3=12 根）。

#### 4.3.3 源码精读

**当前占位符实现**——[1.hw/ip.infra/ethernet_phy.sv:43-61](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/ethernet_phy.sv#L43-L61)：

```systemverilog
module ethernet_phy (
   output logic  e1_reset, output logic e1_mdc, inout logic e1_mdio,
   ...e2/e3/e4 同构...
);
   assign e1_reset = 1'b1;
   assign e2_reset = 1'b1;
   assign e3_reset = 1'b1;
   assign e4_reset = 1'b1;
endmodule
```

文件头注释明确写着 `Description: Ethernet PHY Controller (placeholder)`。它把四路 `reset` 全恒接 1（不复位，依赖板上电默认），`mdc`/`mdio` **连信号声明带例化都有，但模块内部完全没驱动它们**——既没有状态机、也没有读 PHY 寄存器、更没有 link 监测。换言之，MDIO 引脚当前处于「悬空/由约束默认处理」的状态。

**顶层例化**——[1.hw/top.sv:333-346](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv#L333-L346)：`u_phy` 把 4 路 MDIO 引脚（`e1_mdc`/`e1_mdio`…）接到顶层对外端口，但这些端口在当前实现里没有功能性驱动。

**为什么现在能 link up？** 当前依赖的是 Realtek PHY 的硬件默认行为：上电后 PHY 通常默认进入自协商模式，与对端协商出 1G 全双工后自动 link up，MAC 无需软件干预。这在实验室点对点直连场景下能工作，但缺少「软件可配 + link 状态可观测」的能力——这正是 README「设计意图」与「当前实现」之间的差距。

**MDIO 概念在哪被验证了？** 在仿真侧。README 指出：

> An MDIO slave interface is also provided that maps *mem_model* memory areas to the registers with instantiated *mem_model* components（[README.md:196](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/README.md#L196)）

即 `4.sim` 里有 `bfm_phy_mdio`（MDIO 从机模型），它能响应 MAC 发出的 MDIO 事务并把寄存器映射到 `mem_model` 共享内存（详见 [u7-l4](u7-l4-udpippg-ethernet-vip.md)）。这说明「MDIO 读写 PHY 寄存器」这套协议在仿真框架里是被建模和验证过的——只是当前 RTL 的 PHY 控制器还没去用它。

> 待确认：`ethernet_phy.sv` 占位符何时被替换为带 MDIO 状态机的完整实现，需跟踪后续 commit。

#### 4.3.4 代码实践

**实践目标**：通过「源码阅读型实践」确认 PHY 控制器的现状，并理解未来完整实现要补什么。

**操作步骤**：

1. 读 [ethernet_phy.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/ethernet_phy.sv)，确认它只有 4 个 `assign ... = 1'b1`，没有任何 `always` 块或状态机。
2. 在 [top.sv:333-346](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv#L333-L346) 看 `u_phy` 的引脚连接，留意 `mdc`/`mdio` 是 `inout`/`output` 但无驱动源。
3. 查阅 [README.md:130](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/README.md#L130) 与 [README.md:196](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/README.md#L196)，对比「设计意图」与「仿真侧已有 BFM」与「RTL 现状」三者。

**需要观察的现象**：

- RTL 里 PHY 控制是空的；MDIO 协议处理目前只存在于仿真 BFM（`bfm_phy_mdio`）。
- README 把 PHY Controller 列为数据面第一项，说明它是「计划内但未完成」的功能。

**预期结果**：能够清楚说出「当前 PHY 依赖默认自协商 link up，软件化 MDIO 初始化与 link 监测为待实现项；MDIO 协议在仿真侧已被验证」。

#### 4.3.5 小练习与答案

**练习 1**：`ethernet_phy.sv` 里 `assign e1_reset = 1'b1`，这意味着复位引脚是什么极性？为什么这样反而能让 PHY 工作？

> **答案**：`reset=1` 说明该复位是高有效，恒接 1 等于「一直不复位」——但需对照约束与 PHY 数据手册确认极性。占位符让 PHY 跳过 FPGA 主导的复位，直接用上电默认状态（通常已使能自协商），因此在点对点直连下能 link up。

**练习 2**：如果将来要实现「软件经 CSR 读取 link 状态」，需要给 `ethernet_phy` 增加哪些能力？

> **答案**：① 一个 MDIO 主机状态机（产生 Clause 22 读/写位流，驱动 `mdc`/`mdio`）；② 周期轮询 PHY 状态寄存器的 link 位；③ 把结果接到 CSR（如 `to_csr.ethernet[i].status` 的某个字段，类比 `speed` 已挂 CSR）；④ 一条 CPU 经 CSR 触发 PHY 复位与配置写的路径。这些目前都没有。

## 5. 综合实践

**任务**：画出一路以太网（以 eth1 为例）从「DPE 发出 128 位 AXIS 包」到「线上字节」的完整模块调用链与时钟/位宽标注图，并回答三个问题。

要求在一张图里标注：

1. 每一级模块名（自研的加 ★，外部库的加 ◆）。
2. 每一级的时钟域与数据位宽。
3. FCS 在哪一级加、在哪一级校验。

参考答案（先自己画再对照）：

```
DPE demux ──to_eth_1(128b@sys_clk 80M)──►
  ★ethernet_mac (封装/参数化)
     └─◆eth_mac_1g_gmii_fifo
          ├─◆axis_async_fifo_adapter (TX: 128b@80M → 8b@tx_clk 125M, 整包存储转发)
          └─◆eth_mac_1g_gmii → ◆eth_mac_1g → ◆axis_gmii_tx (加前导/SFD, FCS在此追加)
                                  → GMII e1_txd/e1_tx_en (8b@125M, 线上)
反向 RX：线上 → ◆axis_gmii_rx (检测SFD, FCS在此校验) → ◆axis_async_fifo_adapter (8b@125M → 128b@80M)
       → ★ethernet_mac → from_eth_1(128b@sys_clk) → DPE mux
PHY 侧：★ethernet_phy (占位符，reset恒1，mdc/mdio未驱动) 接到 e1_mdc/e1_mdio
```

三个问题：

- **Q1**：整条链里 CDC（跨时钟域）发生在哪一级？→ 答：`axis_async_fifo_adapter`，TX 在 sys_clk↔tx_clk，RX 在 rx_clk↔sys_clk。
- **Q2**：为什么 DPE 不直接连 GMII，非要中间加 FIFO adapter？→ 答：DPE 是 128bit@80M，GMII 是 8bit@125M，时钟与位宽都不同，必须异步 FIFO 适配；且帧 FIFO 能在坏帧/超长帧上整包丢弃，保护 DPE。
- **Q3**：当前 bitstream 上，PHY 控制器对链路建立有没有贡献？→ 答：没有功能贡献，PHY 靠 Realtek 默认自协商 link up；`ethernet_phy` 仅占位。

## 6. 本讲小结

- 本项目从 Alex Forencich 的 `verilog-axis`（7 个文件）与 `verilog-ethernet`（10 个文件）「按需挑用」，只编入 1G GMII 相关部分，清单见 `top.filelist`。
- 自研的 `ethernet_mac.sv` 是一层薄封装：把外部 `eth_mac_1g_gmii_fifo` 参数化为 128 位（对接 `dpe_if`）并搬线，自身不含以太网协议逻辑。
- 外部 MAC 核内部用两个 `axis_async_fifo_adapter`「一器三用」——跨时钟域（80M↔125M）、位宽转换（128↔8）、整包存储转发；这正是 u2-l3「蓝绿过桥 FIFO」的实体。
- FCS（CRC-32）的追加在 `axis_gmii_tx`、校验在 `axis_gmii_rx`，由 `eth_crc_8` 逐字节 LFSR 实现；短帧由 `axis_gmii_tx` 的 `STATE_PAD` 补足 64 字节。
- `dpe_if` 侧时钟是 `sys_clk`（80 MHz）而非 125 MHz，确认了 MAC FIFO 适配器跨的是 80M@128bit ↔ 125M@8bit 两个域。
- **`ethernet_phy.sv` 当前是占位符**：README「初始化 PHY + 监测 link」是设计意图，当前 RTL 不驱动 MDIO，仅靠 PHY 默认自协商 link up；MDIO 协议本身已在仿真 BFM（`bfm_phy_mdio`）中验证。

## 7. 下一步学习建议

- **接入层之上**：本讲是数据面最底层。向上读 [u4-l1](u4-l1-dpe-overview-axis.md) 看 DPE 如何接收这些 128 位 AXIS 包并按 `tuser_dst` 路由，以及 [u4-l2](u4-l2-round-robin-mux.md)/[u4-l3](u4-l3-demultiplexer.md) 的 mux/demux。
- **跨时钟域细节**：本讲的 `axis_async_fifo_adapter` 是 CDC+位宽转换的范本，可结合 [u2-l3](u2-l3-clock-reset-domains.md) 把三个时钟域（125M MAC、80M CSR、80M 流水线）的全局划分串起来。
- **PHY/MDIO 的仿真侧**：想看 MDIO 协议「真的」怎么收发，读 [u7-l4](u7-l4-udpippg-ethernet-vip.md) 里的 `bfm_phy_mdio`（Clause 22 从机模型）和它如何把 PHY 寄存器映射到 `mem_model`。
- **外部库源码**：建议克隆 [verilog-ethernet](https://github.com/alexforencich/verilog-ethernet) 与 [verilog-axis](https://github.com/alexforencich/verilog-axis)，对照阅读 `axis_gmii_tx.v` 的完整状态机与 `eth_crc_8` 的实现，加深对成帧与 FCS 的理解。
- **构建衔接**：这些外部库文件如何被 Vivado/openXC7 编入综合，见 [u8-l1](u8-l1-vivado-build-constraints.md) 与 [u8-l2](u8-l2-openxc7-sv2v.md)（注意 sv2v 转换对外部 V2001 库通常无障碍，难点在自研 SV 的 interface/modport）。
