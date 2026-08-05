# 硬件平台 Alinx AX7201 与四口千兆以太网

## 1. 本讲目标

本讲聚焦于 wireguard-fpga 跑在「什么硬件」上。学完后你应当能够：

- 说出目标板卡（Alinx AX7201 / Artix-7）和目标 FPGA 器件的定位，理解为何选「便宜 + 支持开源工具」的硬件。
- 看懂四个 1000Base-T 以太网口的物理接口——每个口都用 **GMII**（8 位数据）传数据、用 **MDIO** 配置 PHY，并能区分收发时钟。
- 在约束文件 `top.xdc` 里定位以太网口、UART、LED、按键各自对应的引脚与电气标准（IOSTANDARD），理解「自包含、不依赖 PC 主机」这一设计目标。
- 理解板卡 200MHz 差分时钟、复位、UART 等基础外设如何通过引脚约束接入 FPGA。

本讲只讲「物理硬件平台与引脚约束」，不深入 SoC 内部——那是后续讲义的内容。

## 2. 前置知识

在开始前，先用大白话建立几个概念：

- **FPGA（现场可编程门阵列）**：一块可以通过「比特流（bitstream）」重新连线、重新定义内部电路的芯片。我们写的 SystemVerilog 最终会被综合成电路烧进它。
- **板卡（开发板）**：FPGA 芯片焊在一块电路板上，周围还连着网口、串口、按键、LED、时钟芯片、电源等。本项目用的板卡是 **Alinx AX7201**。
- **引脚约束（XDC）**：FPGA 内部电路需要和板卡上的外部器件「对上号」——某根网口接收线到底连到 FPGA 的哪个引脚（PACKAGE_PIN）、用什么电气标准（IOSTANDARD，如 LVCMOS33）。这个对应关系就写在约束文件 `top.xdc` 里。Vivado 用 `.xdc`，这是 Xilinx 约束文件格式。
- **GMII / MII**：千兆以太网媒体无关接口（Gigabit Media-Independent Interface）。它把「MAC（媒体访问控制，FPGA 内）」和「PHY（物理层芯片，板卡上）」分开：MAC 只管 8 位数据 @125MHz，不关心网线上的模拟信号。MII 是 4 位 @25MHz 的百兆版本，本项目的发送时钟 `txc` 就标注为 25MHz MII 时钟。
- **MDIO（Management Data Input/Output）**：两根线（MDC 时钟 + MDIO 数据）的串行管理总线，MAC 通过它读写 PHY 内部寄存器，从而初始化 PHY、查询 link up/down。
- **1000Base-T**：IEEE 802.3ab 千兆以太网，跑在普通 4 对双绞线上，即我们日常的「千兆网口」。

如果你对 WireGuard 本身或软硬协同 SoC 还没概念，建议先看上一讲 [u1-l2 仓库目录结构](u1-l2-repo-structure.md) 建立全局地图。

## 3. 本讲源码地图

本讲涉及的文件都真实存在于仓库中：

| 文件 | 作用 |
|------|------|
| [README.md](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/README.md) | 项目总说明，给出硬件平台定位（AX7201、Artix-7、四口千兆、自包含）与 Artix-7 的频率上限。 |
| [0.doc/Alinx/0.README.txt](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/0.doc/Alinx/0.README.txt) | Alinx 官方技术支持与 AX7201 教程/Demo 链接。 |
| [1.hw/top.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv) | 顶层模块，定义了全部对外端口（4 个以太网口、UART、按键、LED、时钟、复位）。 |
| [1.hw/constraints/top.xdc](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/constraints/top.xdc) | Vivado 引脚/电气标准/时钟约束文件，是本讲最核心的「硬件真相来源」。 |
| [99.warmup/0.blinky/](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/99.warmup/0.blinky/1.hw/led_test.v) | 板卡点亮 LED 的「上板第一课」示例，验证引脚连通性。 |
| [3.build/hw_build.openXC7/Makefile](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/hw_build.openXC7/Makefile) | 开源工具链 openXC7 的构建脚本，写明了目标 FPGA 器件型号。 |

## 4. 核心概念与源码讲解

### 4.1 板卡与器件资源

#### 4.1.1 概念说明

wireguard-fpga 的一个核心设计取舍是：**不追求高性能、而是追求可及性**。前作 Blackwire 跑在昂贵的 AMD/Xilinx Alveo U50（数据中心加速卡）上、只能用专有 Vivado 工具链。本项目反其道而行——选了一块**低成本、支持开源工具的 Artix-7 板卡 Alinx AX7201**，并且要做到「**自包含（self-sufficient）**」，即不依赖 PC 主机就能独立运行成一个 VPN 节点。

README 把这一目标列得很清楚：

> - for an inexpensive hardware platform with four 1000Base-T ports
> - in a self-sufficient way, i.e. w/o requiring PC host
> - using a commodity Artix7 FPGA
> - which is supported by open-source tools

板卡上关键资源：一块 Artix-7 FPGA、**四个千兆以太网口（1000Base-T）**、一个 USB 转串口（UART）、两个用户 LED、两个用户按键、一个差分系统时钟和一个复位按键。

#### 4.1.2 核心流程

目标器件由构建脚本指定。在开源工具链 openXC7 的 Makefile 中：

```
# FPGA Part specification (from .xpr: xc7a200tfbg484-2)
PART = xc7a200tfbg484-2
```

即 **Artix-7 200T（fbg484 封装，速度等级 -2）**。这是 Xilinx 7 系列中容量较大的型号，足以容纳本项目「DPE 数据面 + RISC-V 控制面 + ChaCha20-Poly1305 加解密」整个 SoC。

需要留意一个 Artix-7 的物理限制——它不支持高性能（HP）I/O，频率天花板不高：

> Artix-7 does not support High-Performance (HP) I/O. Consequently, we cannot push its I/O beyond 600MHz, nor its core logic beyond 100 MHz.

这条约束直接影响后续讲义中「三个时钟域为何分别选 125MHz / 80MHz」的设计依据（见 u2-l3）。本项目把这个频率限制当作已知条件来规划架构。

#### 4.1.3 源码精读

README 对硬件平台定位的原文（「Back to the Future」小节）：

[README.md:L42-L48](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/README.md#L42-L48) —— 这是本项目选择硬件平台的四条原则：便宜、四口千兆、自包含（无需 PC）、商品级 Artix-7 且支持开源工具。

[README.md:L50-L53](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/README.md#L50-L53) —— 嵌入了 AX7201 板卡的实物图片（单板与两板拼接，对应两节点实验拓扑）。

[README.md:L228-L230](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/README.md#L228-L230) —— 明确 Artix-7 的 I/O 与核心逻辑频率上限，是后续时钟设计的硬约束。

[3.build/hw_build.openXC7/Makefile:L28-L29](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/3.build/hw_build.openXC7/Makefile#L28-L29) —— 目标器件型号 `xc7a200tfbg484-2`。（补充：PipelineC/Pypeline 流程中另一处写的是同系列但不同封装的 `xc7a200tffg1156-2`，两者都是 Artix-7 200T、速度等级 -2；具体封装以你手头板卡为准——**待本地核对**。）

Alinx 官方文档入口（提供板卡教程与 Demo 下载，是 AX7201 引脚与原理图的权威来源）：

[0.doc/Alinx/0.README.txt:L1-L18](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/0.doc/Alinx/0.README.txt#L1-L18) —— 给出 Alinx 技术支持邮箱、AX7201 教程与 Demo 的网盘/飞书链接。

#### 4.1.4 代码实践

**实践目标**：从仓库确认本项目「自包含、四口千兆、Artix-7」的硬件定位。

**操作步骤**：

1. 打开 `README.md`，定位「Back to the Future」小节（约第 42–48 行）与「Take1」小节（约第 228–230 行）。
2. 打开 `3.build/hw_build.openXC7/Makefile` 第 28–29 行，记下 `PART` 值。
3. 打开 `0.doc/Alinx/0.README.txt`，复制其中的 AX7201 教程链接。

**需要观察的现象**：README 反复强调「不依赖 PC 主机」「支持开源工具」，这与前作 Blackwire 形成对照。

**预期结果**：你能用一句话回答「这块板是什么 FPGA、为什么选它」——例如「Artix-7 200T，因为便宜且能被 openXC7 开源工具链综合」。

> 若你需要板卡原理图/引脚定义来核对封装，请以 Alinx 官方 AX7201 手册为准——本仓库的 `0.doc/Alinx/` 下也提供了 `AX7201_User_Manual.pdf`（**待本地查阅**）。

#### 4.1.5 小练习与答案

**练习 1**：本项目为什么放弃 Blackwire 那样的 Alveo U50 高端平台？
> **答**：为了「真正的开源可及性」——Blackwire 平台昂贵、闭源工具链依赖、且用小众 HDL（SpinalHDL）；本项目改用便宜的 Artix-7 商品级 FPGA，使其能被教育机构与社区负担，并用通用 Verilog/SystemVerilog + 开源工具链实现。

**练习 2**：README 说 Artix-7 的核心逻辑不超过 100MHz，这对设计意味着什么？
> **答**：FPGA 内部任意一个时钟域的频率天花板大约是 100MHz，所以 SoC 内部时钟（如 80MHz 系统域）必须留余量；只有 I/O 可达更高频率（如以太网 125MHz GMII）。后续时钟域划分要服从这条物理约束。

---

### 4.2 四口千兆以太网物理接口

#### 4.2.1 概念说明

板卡有**四个 1000Base-T 千兆网口**，这是本项目「VPN 节点 = 带加密的 IP 路由器」的物理基础。每个网口在 FPGA 侧都遵循 **GMII** 接口：

- **数据**：8 位（1 字节）并行数据 `rxd[7:0]` / `txd[7:0]`，每个时钟周期传 1 字节。
- **时钟**：接收方向 `rxc` 是 **125MHz GMII 接收时钟**（由 PHY 驱动给 FPGA）；发送方向 `gtxc` 是 **125MHz GMII 发送时钟**（FPGA 输出给 PHY）。注意还有一个 `txc`，注释写的是「25MHz MII tx clock」，即百兆 MII 模式的发送时钟。
- **控制**：`rxdv`/`txen` 表示数据有效，`rxer`/`txer` 表示错误。
- **管理**：`mdc`（时钟）+ `mdio`（双向数据）是 MDIO 总线，FPGA 用它配置板载 **Realtek PHY**（初始化、查链路状态）。`reset` 是 PHY 复位引脚。

千兆速率可以这样验证：8 位 @ 125MHz → \(8 \times 125\text{M} = 1000\text{Mbit/s} = 1\text{Gbps}\)。

#### 4.2.2 核心流程

单个以太网口的信号分组（以网口 1 为例，前缀 `e1_`；网口 2/3/4 同构）：

| 方向 | 信号 | 含义 |
|------|------|------|
| 管理 | `e1_reset` | PHY 复位（FPGA 输出） |
| 管理 | `e1_mdc` / `e1_mdio` | MDIO 时钟 / 双向数据 |
| 接收 RX | `e1_rxc` | 125MHz GMII 接收时钟（FPGA 输入） |
| 接收 RX | `e1_rxdv` / `e1_rxer` | 接收数据有效 / 错误 |
| 接收 RX | `e1_rxd[7:0]` | 接收数据 8 位 |
| 发送 TX | `e1_txc` | 25MHz MII 发送时钟（FPGA 输入） |
| 发送 TX | `e1_gtxc` | 125MHz GMII 发送时钟（FPGA 输出） |
| 发送 TX | `e1_txen` / `e1_txer` | 发送数据有效 / 错误 |
| 发送 TX | `e1_txd[7:0]` | 发送数据 8 位 |

数据流走向：网线 → Realtek PHY（模拟前端） → GMII → FPGA 内的 1G MAC → 进入数据面引擎 DPE。PHY 的初始化与链路监测由「PHY Controller」负责。

#### 4.2.3 源码精读

顶层模块对四个以太网口的端口声明（每个口 13 个信号，四个口完全对称）：

[1.hw/top.sv:L48-L102](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv#L48-L102) —— 四个网口的 GMII 端口定义。以网口 1 为例，可看到 `e1_rxc` 注释为「125Mhz ethernet gmii rx clock」、`e1_txc` 注释为「25Mhz ethernet mii tx clock」、`e1_gtxc` 为「125Mhz ethernet gmii tx clock」。

约束文件中网口 1 的引脚与电气标准（其余三个口同理，电气标准全是 LVCMOS33）：

[1.hw/constraints/top.xdc:L73-L149](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/constraints/top.xdc#L73-L149) —— 以太网 PORT1 的 RX/TX 引脚约束。其中 `create_clock -period 8.000 -name e1_rx_clk [get_ports e1_rxc]` 把 125MHz（周期 8ns）声明为接收时钟；`e1_gtxc` 同样声明 8ns（125MHz）发送时钟。

四个网口的 MDIO 管理引脚约束：

[1.hw/constraints/top.xdc:L30-L50](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/constraints/top.xdc#L30-L50) —— `e1_mdio`~`e4_mdio` 的引脚与上拉（PULLUP）/慢摆率（SLEW SLOW）设置，对应 MDIO 总线对 PHY 的读写。

由于每路以太网时钟（125MHz）和系统时钟异步，约束文件用 `set_false_path` 切断跨时钟域的时序检查（这是跨域 FIFO 之外的时序收敛手段）：

[1.hw/constraints/top.xdc:L385-L405](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/constraints/top.xdc#L385-L405) —— 把 `sys_pll_out` 与各路 `eN_rx_clk`/`eth_pll_out` 之间设为 false path，告诉综合器这些跨域路径不需要做建立/保持时间检查。

README 对 PHY 控制器的描述（数据面第一个模块）：

[README.md:L130](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/README.md#L130) —— 「PHY Controller - initial configuration of Realtek PHYs and monitoring link activity (link up/down events)」，确认板载 PHY 为 Realtek 系列。

#### 4.2.4 代码实践

**实践目标**：核对四个网口的 GMII 接口结构与速率推导。

**操作步骤**：

1. 打开 `1.hw/top.sv` 第 48–102 行，数一数每个网口声明了多少个信号；确认四个网口（`e1_`~`e4_`）结构完全一致。
2. 打开 `1.hw/constraints/top.xdc` 第 73–149 行（网口 1），找到 `create_clock` 行，确认 RX/GTX 时钟周期都是 8ns。
3. 滚动到第 151 行之后，确认网口 2/3/4 的约束段同样存在。

**需要观察的现象**：四个网口的电气标准统一是 `LVCMOS33`；RX 时钟 `eN_rxc` 与 GTX 时钟 `eN_gtxc` 都是 8ns（125MHz）。

**预期结果**：用 \(8\text{ bit} \times 125\text{ MHz} = 1\text{ Gbps}\) 验证单口千兆速率；四口合计 4Gbps——这与后续讲义中「加密核至少要处理 4Gbps」的需求呼应。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `e1_rxc` 是 input（FPGA 输入），而 `e1_gtxc` 是 output（FPGA 输出）？
> **答**：接收方向由 PHY 恢复出 125MHz 时钟并连同数据一起送给 FPGA，所以 `rxc` 是输入；发送方向由 FPGA 内部 MAC/PLL 产生 125MHz 时钟驱动 PHY 发送，所以 `gtxc` 是输出。GMII 中收发时钟方向相反。

**练习 2**：MDIO 总线和 GMII 数据总线各负责什么？
> **答**：GMII（`rxd/txd/rxdv/txen` 等）负责**数据面**——线速传输以太网帧；MDIO（`mdc/mdio`）负责**管理面**——低速读写 PHY 内部寄存器，做初始化和链路状态查询。两者职责分离、互不干扰。

---

### 4.3 外设：UART / LED / 按键（及复位与时钟）

#### 4.3.1 概念说明

除四个网口外，板卡还提供几类「管理用」外设，本讲从硬件平台角度认识它们：

- **UART（串口）**：板卡通过 USB 转串口芯片引出 `uart_rx`/`uart_tx` 两根线，PC 用 minicom 等终端连上，就得到一个命令行（CLI），用来配置 WireGuard 节点。它还有一个「特殊二进制模式」，用于在线烧写 CPU 指令存储器（IMEM），免去重新综合——详见 u2-l5。
- **LED**：两个用户 LED（`led[1:0]`），低电平点亮（板卡 LED 是灌电流接法）。
- **按键**：两个用户按键（`key_in[1:0]`），按下为低（代码里取反后再用）。
- **系统时钟**：200MHz 差分时钟（`clk_p`/`clk_n`），由差分输入缓冲（IBUFDS / DIFF_SSTL15）接入，片内 PLL 再分出各时钟域。
- **复位**：`rst_n`，低有效按键复位。

这些外设让节点真正做到「自包含」：开机后只需一根串口线就能配置和管理，不需要 PC 主机参与数据转发。

#### 4.3.2 核心流程

外设到 FPGA 内部的接入路径：

```
板卡外设 ──引脚约束(top.xdc)──> top.sv 端口 ──> 片内 CSR/逻辑
   LED       LVCMOS33, E17/F16    led[1:0]   <-- ~gpio.ledN.value（低有效点亮）
   按键      LVCMOS33, D16/E16     key_in[1:0]--> ~后送 gpio.keyN.next（TODO: 消抖）
   UART      LVCMOS33, AA15/AB15   uart_rx/tx  <-> uart 模块(CLI + 特殊模式)
   时钟      DIFF_SSTL15, R4       clk_p/n     --> clk_rst_gen(PLL) --> sys_clk/eth_gtx
   复位      LVCMOS15, T6          rst_n       --> 复位同步
```

值得注意：LED 与按键并没有独立的复杂外设控制器，而是直接接到 CSR（控制状态寄存器）的 GPIO 字段——CPU 读写 CSR 就能点灯或读键。这体现了「CSR 是软硬件桥梁」的思想（详见 U3 单元）。

#### 4.3.3 源码精读

顶层模块对外设、时钟、复位的端口声明：

[1.hw/top.sv:L43-L113](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv#L43-L113) —— 包含 `clk_p/clk_n`、`rst_n`、四个网口、`uart_rx/uart_tx`、`key_in[1:0]`、`led[1:0]` 全部对外端口。

LED 与按键到 CSR 的映射（注意取反 `~`）：

[1.hw/top.sv:L235-L238](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv#L235-L238) —— `assign led[1] = ~from_csr.gpio.led2.value;` 等四行：LED 取反后输出（低有效点亮），按键取反后送 CSR；注释里写了 `TODO: Add debounce`（按键尚未做消抖）。

约束文件中时钟、复位、MDIO、LED、按键、UART 的电气标准与引脚：

[1.hw/constraints/top.xdc:L21-L28](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/constraints/top.xdc#L21-L28) —— 系统时钟：`create_clock -period 5.000`（200MHz）、`clk_p` 在 R4、电气标准 `DIFF_SSTL15`；复位 `rst_n` 在 T6、`LVCMOS15`。

[1.hw/constraints/top.xdc:L52-L64](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/constraints/top.xdc#L52-L64) —— LED 在 E17/F16（`LVCMOS33`），按键在 D16/E16（`LVCMOS33`）。

[1.hw/constraints/top.xdc:L66-L71](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/constraints/top.xdc#L66-L71) —— USB UART：`uart_rx` 在 AA15、`uart_tx` 在 AB15，均 `LVCMOS33`。

「上板第一课」LED 闪烁示例（Take1 阶段验证引脚连通性）：

[99.warmup/0.blinky/1.hw/led_test.v:L34-L61](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/99.warmup/0.blinky/1.hw/led_test.v#L34-L61) —— 用 200MHz 时钟计数到约 1 秒翻转 LED，正是 README「Take1」中「Create our first FPGA program that blinks LEDs / Verify pinouts」的产物。

[99.warmup/0.blinky/1.hw/led.xdc:L15-L20](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/99.warmup/0.blinky/1.hw/led.xdc#L15-L20) —— 闪烁示例里 LED 同样约束在 E17/F16、`LVCMOS33`，与主设计 `top.xdc` 完全一致——说明板卡引脚映射在早期就固化了。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：把本项目用到的 4 个以太网口 + UART + LED + 按键，按「FPGA 引脚约束类别」归类，建立「外设 → 引脚 → 电气标准」的对照表。

**操作步骤**：

1. 打开 `1.hw/constraints/top.xdc`。
2. 分别定位以下小节（靠注释分隔）：
   - 以太网 PORT1–PORT4（`L73` 起，每个口约 70+ 行）；
   - MDIO（`L30`–`L50`）；
   - USB UART（`L66`–`L71`）；
   - LED（`L52`–`L57`）；
   - 按键 key（`L59`–`L64`）；
   - 系统时钟与复位（`L21`–`L28`）。
3. 逐组记录每个信号的 `PACKAGE_PIN`（引脚号）和 `IOSTANDARD`（电气标准）。

**需要观察的现象**：

- 以太网数据/控制线、MDIO、UART、LED、按键几乎都是 **`LVCMOS33`**（3.3V LVCMOS）。
- 系统时钟 `clk_p` 是 **`DIFF_SSTL15`**（1.5V 差分 SSTL，DDR 内存级电平）。
- 复位 `rst_n` 是 **`LVCMOS15`**（1.5V LVCMOS，与差分时钟同属 Bank 15 的 1.5V 供电区）。
- 四个网口的 RX/GTX 时钟都被 `create_clock -period 8.000` 声明为 125MHz。

**预期结果**（参考答案表）：

| 外设组 | 代表信号 | IOSTANDARD | 引脚(Bank 推断) |
|--------|----------|-----------|-----------------|
| 4× 以太网 GMII | `eN_rxd/txd/rxdv/txen/...` | LVCMOS33 | 分散在多个 3.3V Bank |
| 4× 以太网 MDIO | `eN_mdc/mdio` | LVCMOS33（带上拉、慢摆率） | L16/AB22/V19/U20 等 |
| UART | `uart_rx/uart_tx` | LVCMOS33 | AA15/AB15 |
| LED | `led[1:0]` | LVCMOS33 | E17/F16 |
| 按键 | `key_in[1:0]` | LVCMOS33 | D16/E16 |
| 系统时钟 | `clk_p/n` | DIFF_SSTL15 | R4（差分） |
| 复位 | `rst_n` | LVCMOS15 | T6 |

> 结论：除「差分系统时钟用 1.5V SSTL、复位随其 Bank 用 1.5V LVCMOS」之外，**绝大多数外设都是 3.3V LVCMOS33**——这正是 Artix-7「不支持 HP I/O、只用普通 HR I/O」的体现（见 4.1.3）。Bank 的精确划分以 AX7201 原理图为准——**待本地核对**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 LED 要写 `assign led[1] = ~from_csr.gpio.led2.value;`，多加一个取反 `~`？
> **答**：板卡上的 LED 采用灌电流接法（阳极接 VCC，阴极接 FPGA），FPGA 输出**低电平**才点亮。而 CSR 里的 `ledN.value` 语义是「1 = 点亮」，因此代码取反，把「逻辑 1」翻译成「物理低电平」。

**练习 2**：系统时钟为什么用差分（`clk_p`/`clk_n` + DIFF_SSTL15）而不是单端？
> **答**：200MHz 时钟频率较高，差分信号（如 SSTL/HSTL）抗共模噪声能力更强、抖动更小、翻转更干净，适合做整板的高质量参考时钟。单端 LVCMOS 在这么高频率下边沿质量和抗噪都较差。差分时钟经 IBUFDS/PLL 后再生成各路内部时钟。

**练习 3**：UART 在本项目承担哪两种角色？
> **答**：① 字符模式：承载 CLI，PC 通过串口终端配置 WireGuard 节点（network/routes/cryptokeys）；② 特殊二进制模式：在线烧写/修改 IMEM、原子读写 DMEM/CSR 总线（IMPR/IMWR/BUSR/BUSW），免去重新综合。详细机制见 u2-l5。

## 5. 综合实践

**任务：绘制「AX7201 板卡外设 → FPGA 引脚约束 → 片内去向」全图。**

1. 以本讲 4.3.4 的对照表为基础，补全四个以太网口（PORT1–4）各自的 RX/TX 时钟周期与电气标准。
2. 打开 `1.hw/top.sv`，把每个端口在片内接到哪个子模块（如 `clk_rst_gen`、`ethernet_mac/phy`、`uart`、CSR 的 GPIO 字段）尽量标注出来（不必精确到行，画出数据流即可）。
3. 用 200MHz 系统时钟推导：为何 GMII 用 125MHz、而内部 sys_clk 选 80MHz——结合 README「Artix-7 核心逻辑 < 100MHz」的约束给出你的解释（开放题，目的是建立「硬件物理约束 → 时钟域设计」的因果链）。
4. 最后写一段话：本项目如何仅靠「板卡 + 一根串口线 + 网线」就独立运行成一个 VPN 节点（即「自包含」体现在哪些外设上）。

> 这道题把「物理平台（4.1）+ 以太网接口（4.2）+ 管理外设（4.3）」三部分串起来，为下一讲 [u1-l4 构建流程总览](u1-l4-build-overview.md) 中「bitstream 如何烧进这块板」做铺垫。

## 6. 本讲小结

- 本项目硬件平台是 **Alinx AX7201**，搭载**商品级 Artix-7 200T** FPGA（构建脚本中为 `xc7a200tfbg484-2`），选择它的理由是便宜、四口千兆、支持开源工具链。
- 板卡有 **四个 1000Base-T 千兆网口**，每口走 **GMII**（8 位 @125MHz，正好 1Gbps），用 **MDIO**（MDC+MDIO）管理板载 Realtek PHY。
- 顶层 `top.sv` 声明了全部对外端口；约束文件 `top.xdc` 把这些端口绑到具体引脚并指定电气标准——绝大多数外设是 **LVCMOS33**，差分系统时钟是 **DIFF_SSTL15**（200MHz），复位随其 Bank 用 LVCMOS15。
- LED/按键直接挂到 CSR 的 GPIO 字段（LED 低有效点亮、按键取反、暂未消抖）；UART 同时承担 CLI 与在线烧写两种角色。
- Artix-7「核心逻辑 < 100MHz、I/O < 600MHz」的物理上限，是后续 SoC 时钟域划分的硬约束。
- `99.warmup/0.blinky` 是项目早期的「点亮 LED」上板验证，固化了 LED 引脚映射。

## 7. 下一步学习建议

现在你已经知道「硬件是什么、引脚怎么连」。接下来：

- 想知道「这个 bitstream 是怎么一步步生成的」→ 学习 [u1-l4 构建流程总览：CSR → SW → HW → bitstream](u1-l4-build-overview.md)。
- 想知道「烧板后怎么用串口 CLI 配置节点并验证加密隧道」→ 学习 [u1-l5 实验室运行：上板、CLI 配置与端到端验证](u1-l5-lab-run-cli.md)。
- 想深入「这些端口在片内如何组成 SoC、跨时钟域如何处理」→ 进入 [u2-l2 top.sv 顶层模块](u2-l2-top-module.md) 与 [u2-l3 时钟复位与三个时钟域](u2-l3-clock-reset-domains.md)。
- 建议同步阅读：`1.hw/README.md` 的「Hardware Data Flow」小节（三个时钟域说明）与 `0.doc/Alinx/AX7201_User_Manual.pdf`（板卡原理图，**待本地查阅**）。
