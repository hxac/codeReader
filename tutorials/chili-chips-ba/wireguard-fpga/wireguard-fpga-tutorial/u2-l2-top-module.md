# top.sv 顶层模块与实例化结构

## 1. 本讲目标

本讲是进入硬件 RTL 内部的第一站。读完本讲，你应当能够：

- 读懂 `top.sv` 的对外端口（4 路以太网 GMII、UART、按键、LED、时钟与复位）各代表什么物理信号。
- 理解 `top.sv` 内部如何把「时钟复位生成 → CPU 子系统 → DPE → 以太网 MAC/PHY」这几大块**实例化并连线**成一个完整 SoC。
- 认识 `to_csr` / `from_csr` 这对 CSR 硬件接口（hwif）：它不是一条带地址译码的总线，而是一个被所有外设按字段「共享分接」的结构体束（bundle）。
- 看懂 `top.filelist` 如何按类别把跨目录、跨外部库、跨生成产物的设计文件组装成「能让 `top` 综合通过」的一份清单。

本讲承接 u2-l1（控制面/数据面分区）与 u1-l4（构建三步走），把抽象的两层架构落到一个具体的、可读的 SystemVerilog 文件上。

---

## 2. 前置知识

在进入源码前，先澄清几个 RTL 初学者容易混淆的概念。

- **模块（module）与实例（instance）**：`module` 是一张「电路图纸」，定义了端口和内部逻辑；图纸本身不耗硬件。把图纸「摆放」到电路里叫**实例化**，例如 `ethernet_mac u_eth_1 (...)` 表示按 `ethernet_mac` 这张图纸摆一个实例，取名叫 `u_eth_1`。同一个 `module` 可以被实例化多次（这里 `ethernet_mac` 就摆了 4 次，对应 4 个网口）。

- **端口（port）与连线（net）**：`top` 模块最外层的 `input`/`output` 是芯片真实的对外引脚；`top` 内部声明的 `logic` 变量（如 `sys_clk`）则是把各实例端口**连起来**的导线。实例化时 `端口名(连线名)` 的写法，就是把图纸上的某个端口接到这根导线上。

- **接口（interface）**：SystemVerilog 的 `interface` 是把一组相关信号（如 AXI-Stream 的 `tvalid/tready/tdata/...`）打包成一个「多芯电缆」。这样实例端口只需写一行 `dpe_if.s_axis from_eth_1`，而不用把十几个信号逐一罗列。`modport` 进一步规定某一端是「主机（m_axis）」还是「从机（s_axis）」，限定信号方向。

- **CSR 硬件接口（hwif）**：PeakRDL 把寄存器规格生成成一个巨大的**结构体类型** `csr_pkg::csr__in_t` / `csr__out_t`。CPU 那一侧通过带地址译码的 `soc_if` 总线访问寄存器；而各个外设（UART、DPE、以太网 MAC、GPIO）那一侧，则直接「咬住」结构体里的某个字段（如 `from_csr.gpio.led2.value`）。所以 `to_csr` / `from_csr` 是「编译期就排好线」的扁平束，**没有运行期地址译码**——译码只发生在 CPU 访问入口 `soc_csr` 里。

- **GMII**：千兆以太网用的 8 位并行接口，时钟 125 MHz。一拍传 8 位，正好 \( 8 \times 125\,\text{MHz} = 1\,\text{Gbps} \)。这就是为什么每个网口端口里 `eX_rxd` 都是 `[7:0]` 的 8 位向量。

> 字节序提示：以太网/IP/UDP 头部按**大端**（网络字节序）排列，而 WireGuard 协议头部是**小端**。这点在 U4 系列讲义会再次用到，本讲只需记住 DPE 内部数据线 `tdata` 是按小端组织的。

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用它做什么 |
|------|------|----------------|
| [1.hw/top.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv) | 顶层模块，定义对外端口并实例化全部子模块 | 4.1 端口定义、4.2 实例化与连线的核心 |
| [1.hw/top.filelist](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.filelist) | 设计文件清单，告诉综合器要编译哪些源文件 | 4.3 filelist 组装规则 |
| [1.hw/ip.infra/dpe_if.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/dpe_if.sv) | AXI-Stream 接口定义（128 位 + 元数据） | 理解 4.2 中 `from_eth_X` / `to_eth_X` 这「多芯电缆」里有哪些信号 |
| [1.hw/ip.infra/clk_rst_gen.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/clk_rst_gen.sv) | 时钟复位生成（PLL + 复位同步） | 理解 4.2 第一个实例 `u_clk_rst_gen` 产出哪些时钟 |
| [1.hw/ip.infra/ethernet_mac.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/ethernet_mac.sv) | 以太网 MAC 包装层（GMII ↔ AXIS FIFO） | 4.2 实践中追踪接收路径时进入的实例 |
| [1.hw/ip.dpe/dpe.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv) | 数据面引擎（mux / dummy_switch / demux） | 理解 `from_eth_X` / `to_eth_X` 的另一端接到了哪里 |
| [1.hw/ip.infra/soc_csr.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/soc_csr.sv) | 把 `soc_if` 总线翻译成 PeakRDL 的 CSR 接口 | 理解 `to_csr`/`from_csr` 的「源头」 |

---

## 4. 核心概念与源码讲解

### 4.1 顶层端口定义

#### 4.1.1 概念说明

`top` 是整个芯片的「外壳」：它的端口就是 FPGA 对外的物理引脚（约束文件 `top.xdc` 会把这些端口绑定到具体引脚号，详见 u1-l3 与 u8-l1）。理解 `top` 的端口，等于理解「这块板子对外暴露了哪些物理通道」。

本项目的对外通道非常规整：

- **1 对差分时钟 + 1 个复位**：系统的节拍源与启动控制。
- **4 组千兆以太网 GMII**：每组包含 PHY 复位、MDIO 管理、GMII 收发数据。这是数据面的物理进出口。
- **1 路 UART**：兼任 CLI 字符终端与 IMEM 在线烧写（u2-l5 详述）。
- **2 个按键 + 2 个 LED**：挂在 CSR 的 GPIO 字段上，是最简单的「人机交互」。

#### 4.1.2 核心流程

端口定义本身没有「流程」，但有一个**命名规律**值得记住：4 个以太网口完全对称，信号名前缀 `e1_`/`e2_`/`e3_`/`e4_` 一一对应。这意味着 `top` 内部也会用 4 个**完全同构**的实例来处理它们（4.2 会看到 4 个 `ethernet_mac`）。

一个以太网口的 GMII 信号可分三类：

```
PHY 管理：  reset(出) / mdc(出,时钟) / mdio(双向,数据)   —— 走 ethernet_phy
GMII 接收： rxc(时钟) / rxdv(有效) / rxer(错误) / rxd[7:0](数据)  —— 走 ethernet_mac 的 RX
GMII 发送： gtxc(时钟,出) / txen(有效,出) / txer(错误,出) / txd[7:0](数据,出) / mii_txc(25M,入)
```

- 接收方向（PHY→FPGA）大多是 `input`；
- 发送方向（FPGA→PHY）大多是 `output`；
- `mdio` 是 `inout`（双向漏极开路式的管理总线）。

#### 4.1.3 源码精读

`top` 模块从第 43 行开始，端口表延续到第 113 行：

[top.sv:43-113](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv#L43-L113) — 定义 `top` 的全部对外端口。

其中时钟、复位和以太网口 1 的关键片段：

[top.sv:44-60](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv#L44-L60) — 差分时钟 `clk_p/clk_n`、低有效复位 `rst_n`，以及以太网口 1 的全部 GMII/MDIO 信号。注意 `e1_rxd` 是 `input [7:0]`（8 位 GMII 数据），`e1_mdio` 是 `inout`。

以太网口 2/3/4 的端口（第 62–102 行）与口 1 结构**逐字相同**，只是前缀换成 `e2_/e3_/e4_`，此处不重复贴出。

UART、按键、LED 集中在端口表末尾：

[top.sv:104-112](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv#L104-L112) — UART 收发 `uart_rx/uart_tx`、2 位按键 `key_in[1:0]`、2 位 LED `led[1:0]`。

端口表之后，`top` 立即导入了两个包，让后续能用上里面的类型：

[top.sv:114-115](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv#L114-L115) — `import csr_pkg::*` 与 `import soc_pkg::*`，引入 CSR 结构体类型与 SoC 总线类型。

#### 4.1.4 代码实践

**实践目标**：把「对外引脚」与「物理功能」对上号，为后面追踪信号路径打基础。

**操作步骤**：

1. 打开 [top.sv:43-113](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv#L43-L113)。
2. 对 4 个以太网口，各列出「PHY 管理信号」「GMII 接收信号」「GMII 发送信号」三类，核对 `input`/`output`/`inout` 方向。
3. 统计 `top` 对外端口总数，并回答：4 个网口中，`inout` 类型的信号有哪几个？方向为 `output` 的时钟信号叫什么？

**需要观察的现象**：每个网口都恰好有 1 个 `inout`（即 `eX_mdio`）；每个网口都有 1 个输出时钟 `eX_gtxc`（GMII 发送时钟）和 1 个输入时钟 `eX_rxc`（GMII 接收时钟）以及 1 个输入时钟 `eX_txc`（25 M MII 发送时钟，用于百兆降速兼容）。

**预期结果**：你会得到一张「4 网口 × 3 类信号」的对照表，并确认接收路径的物理入口是 `eX_rxc`（时钟）与 `eX_rxd[7:0]`（数据）。

> 待本地验证：若你想确认引脚绑定，可对照 [top.xdc](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/constraints/top.xdc)（u8-l1 详述）查看这些端口被绑到了哪些 FPGA 引脚与电气标准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `e1_mdio` 是 `inout` 而 `e1_mdc` 是 `output`？
> **答**：MDIO 管理总线由两根线组成——`mdc` 是时钟，永远由 FPGA（主）驱动给 PHY（从），所以是 `output`；`mdio` 是数据，在写周期由 FPGA 驱动、在读周期由 PHY 驱动，方向会翻转，所以是 `inout`。

**练习 2**：`e1_rxd` 为什么是 `[7:0]` 共 8 位？
> **答**：GMII 是千兆以太网的 8 位并行接口，每拍 8 位、125 MHz。8 位 × 125 MHz = 1 Gbps，恰好是千兆速率，所以数据线是 8 位宽。

---

### 4.2 子模块实例化与连线

#### 4.2.1 概念说明

`top` 内部不含任何复杂的组合/时序逻辑——它几乎只是**「连线 + 实例化」**。这是优秀的 SoC 顶层设计风格：顶层只负责「把盒子摆好、用线连好」，真正的功能都在各子模块里。

`top` 把系统分成四大块并依次实例化：

1. **时钟复位生成** `u_clk_rst_gen` —— 产出系统时钟 `sys_clk` 与以太网时钟 `eth_gtx_clk`。
2. **CPU 子系统** —— 包括 CPU `u_cpu`、互联 `u_fabric`、数据存储 `u_dmem`、CSR 包装 `u_soc_csr`、UART `u_uart`、CPU 收发 FIFO `u_cpu_fifo`。
3. **DPE** `u_dpe` —— 数据面引擎，5 路输入（CPU + 4 eth）、5 路输出。
4. **以太网子系统** —— 4 个 MAC `u_eth_1..4` + 1 个 PHY `u_phy`。

块与块之间的连线分两类：
- **接口束（interface）**：`dpe_if` 与 `soc_if`，分别承载包数据与 CPU 总线事务；
- **CSR 硬件束（hwif）**：`to_csr` / `from_csr`，一个扁平结构体，被所有外设分接。

#### 4.2.2 核心流程

`top` 内部的数据流可以这样理解（自上而下）：

```
               ┌──────────── clk_rst_gen ────────────┐
  clk_p/clk_n ─▶│ sys_clk(80M) , eth_gtx_clk(125M)  │── 时钟分发给所有块
  rst_n ───────▶│ sys_rst / sys_rst_n                │
               └────────────────────────────────────┘

  ┌──────────────────── CPU 子系统（控制面，80M 域）────────────────────┐
  │                                                                    │
  │  u_cpu ──bus_cpu(MST)──▶ u_fabric ──┬─bus_uart(SLV)─▶ u_uart ──UART│
  │                                      ├─bus_dmem(SLV)─▶ u_dmem       │
  │                                      └─bus_csr (SLV)─▶ u_soc_csr   │
  │                                                          │         │
  │   u_cpu_fifo ◀──from_cpu/to_cpu──┐                       │         │
  │      │                            │                       │         │
  └──────┼────────────────────────────┼───────────────────────┼─────────┘
         │                            │                       │
         │   to_csr ◀──────── 所有外设按字段分接 ──────────▶ from_csr
         │   (UART / cpu_fifo / DPE / eth MAC speed / GPIO 都咬这个束)
         │
         ▼
  ┌──────────────────── DPE（数据面）────────────────────┐
  │  from_cpu/from_eth_1..4 ─▶ u_dpe_multiplexer         │
  │        (5 路轮询合 1 路)        │                     │
  │                                 ▼                     │
  │                        u_dpe_dummy_switch (直通)       │
  │                                 │                     │
  │                                 ▼                     │
  │                       u_dpe_demultiplexer              │
  │        (1 路按目的分到 to_cpu/to_eth_1..4)             │
  └────────────────────────────────────────────────────────┘
         │              │             │             │
         ▼              ▼             ▼             ▼
  ┌─── ethernet_mac u_eth_1 ───┐  ... ×4   ┌── ethernet_phy u_phy ──┐
  │ AXIS↔GMII + FIFO + 跨时钟域 │           │ 4 路 PHY 复位/MDIO 管理 │
  │      rx_fifo=from_eth_1     │           └─────────────────────────┘
  │      tx_fifo=to_eth_1       │
  └─────────────────────────────┘
```

要点：
- **控制面**（CPU 子系统）通过 `soc_if` 总线（`bus_cpu/bus_uart/bus_dmem/bus_csr`）互联，CPU 是唯一主机，外设是从机；`u_fabric` 做地址译码（u2-l4 详述）。
- **数据面**（DPE + MAC）通过 `dpe_if`（AXI-Stream）互联，4 路网口 + CPU 共 5 路进、5 路出。
- **两个面唯一的交汇点**是 `to_csr`/`from_csr` 这对 hwif 束：CPU 经 `soc_csr` 读写寄存器，寄存器值经 hwif 散播到数据面各处；同时 `cpu_fifo` 把包级数据也搭在这套寄存器机制上，让 CPU 能收发数据面包。

#### 4.2.3 源码精读

**(a) 时钟复位生成**

`top` 第一件事是声明内部时钟连线，然后实例化 `clk_rst_gen`：

[top.sv:120-135](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv#L120-L135) — 实例化 `u_clk_rst_gen`，把差分时钟与复位变换成 `sys_clk`、`sys_rst`、`sys_rst_n`、`eth_gtx_clk`、`eth_gtx_rst` 五条内部线。

进入 `clk_rst_gen` 内部可见它做了三件事：用 `IBUFGDS` 把差分时钟转单端，再用两个 PLL（80 M 与 125 M）产出两套时钟，各自经 `sync_reset` 同步复位：

[clk_rst_gen.sv:60-65](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/clk_rst_gen.sv#L60-L65) — `IBUFGDS` 原语把 `clk_p/clk_n` 差分输入转成单端 `clk`。

[clk_rst_gen.sv:74-79](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/clk_rst_gen.sv#L74-L79) — 80 M 系统时钟 PLL（`fpga_pll_80M`）；125 M 以太网 PLL 在 [clk_rst_gen.sv:99-104](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/clk_rst_gen.sv#L99-L104)。

> 时钟域细节（三个域的频率/位宽/跨域 FIFO）在 u2-l3 专讲，本讲只需知道 `sys_clk` 喂 CPU 与 CSR、`eth_gtx_clk` 喂以太网 MAC。

**(b) 互联束的声明**

接下来 `top` 声明了大量 `dpe_if` 与 `soc_if`「电缆」，并声明 CSR 硬件束：

[top.sv:140-151](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv#L140-L151) — 10 条 `dpe_if`（5 条 `from_*` 接收方向、5 条 `to_*` 发送方向），以及 `to_csr`（`csr__in_t`，硬件→CSR）和 `from_csr`（`csr__out_t`，CSR→硬件）。

注意 `dpe_if` 在声明时就把 `sys_clk`/`sys_rst` 作为参数传进去（如 `.clk(sys_clk), .rst(sys_rst)`），这是因为 `dpe_if` 接口内部把 `clk`/`rst` 也作为信号（供 modport 引用）。每条 `dpe_if`「电缆」内部包含哪些芯，看 `dpe_if` 定义即可一目了然：

[dpe_if.sv:48-57](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/dpe_if.sv#L48-L57) — `dpe_if` 内部信号：标准 AXI-Stream 的 `tready/tvalid/tdata[127:0]/tlast/tkeep[15:0]`，外加本项目自定义的元数据 `tuser_bypass_all`、`tuser_bypass_stage`、`tuser_src[2:0]`、`tuser_dst[2:0]`、`tid[7:0]`（这些元数据含义见 u4-l1）。

[dpe_if.sv:60-88](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/dpe_if.sv#L60-L88) — `m_axis`（主机）与 `s_axis`（从机）两个 modport，规定了同一束信号在两端的方向相反。这就是「一根电缆、两个插头」。

**(c) CPU 子系统**

CPU 子系统先用 `localparam` 定了 IMEM/DMEM 容量，再声明 4 条 `soc_if` 总线：

[top.sv:156-162](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv#L156-L162) — 指令/数据 RAM 各 16384 字（64 KB），以及 `bus_cpu/bus_uart/bus_dmem/bus_csr` 四条 SoC 总线。

CPU 实例（这里 `soc_cpu` 是 picoRV32 的「即插即用」包装，含 IMEM）：

[top.sv:170-180](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv#L170-L180) — `u_cpu` 以 `bus_cpu` 为**主机**口（MST），同时暴露 4 根 IMEM 在线烧写信号（`imem_cpu_rstn/imem_we/imem_waddr/imem_wdat`）供 UART 操纵（u2-l5）。

互联 fabric（地址译码，把 CPU 主口分发到 3 个从口）：

[top.sv:183-189](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv#L183-L189) — `u_fabric`：`cpu/uart` 是从口（SLV），`dmem/csr` 是主口（MST）。注意 fabric 把自己「夹」在中间——对 CPU 它是从，对 DMEM/CSR 它是主。

数据 RAM、CSR 包装：

[top.sv:192-203](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv#L192-L203) — `u_dmem`（数据 RAM，从口）与 `u_soc_csr`（CSR 包装：`bus_csr` 是从口，`hwif_in=to_csr`、`hwif_out=from_csr`）。**`from_csr` 的源头就在这里**——它由 PeakRDL 生成的 `csr` 模块驱动。

UART 与 CPU FIFO：

[top.sv:206-222](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv#L206-L222) — `u_uart`：既是 CLI/烧写通道，又是 CSR 的一个主机（`bus` 是 MST，用于 BUSR/BUSW 直接访问 DMEM/CSR），还输出 IMEM 烧写信号、收发 `to_csr`/`from_csr`。

[top.sv:225-230](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv#L225-L230) — `u_cpu_fifo`：把数据面包的 128 位 AXIS 与 CSR 寄存器对接，一端咬 `from_csr`/`to_csr`，一端接 `from_cpu`/`to_cpu` 两条 `dpe_if`。

**(d) GPIO：最简单的 CSR 分接**

GPIO 部分最能说明「hwif 束是按字段分接的」这个特点——它甚至没有实例化任何模块，就是几条 `assign`：

[top.sv:235-238](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv#L235-L238) — LED 输出取反（`led[1] = ~from_csr.gpio.led2.value`，低有效点亮），按键输入取反写回（`to_csr.gpio.key2.next = ~key_in[1]`，注释里写了 TODO 待加消抖）。

注意读写两种字段的命名差别：读硬件状态用 `from_csr.<字段>.value`；硬件要更新寄存器用 `to_csr.<字段>.next`。这是 PeakRDL 生成的 hwif 约定（u3-l2 详述）。

**(e) DPE 实例**

[top.sv:243-256](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv#L243-L256) — `u_dpe`：咬 `from_csr`/`to_csr`，并以 `s_axis`（从机）收 5 路 `from_*`、以 `m_axis`（主机）发 5 路 `to_*`。进入 `dpe` 内部可见这 5 进 5 出是怎么串起来的：

[dpe.sv:67-92](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv#L67-L92) — `dpe_multiplexer`（5 路轮询合 1 路 `muxed_1`）→ `dpe_dummy_switch`（直通 `muxed_1`→`muxed_2`）→ `dpe_demultiplexer`（按目的分发到 5 路 `to_*`）。

> Phase1 PoC 现状：真正的 WG 处理链（`dpe_egress_ip_lookup`、`dpe_wg_disassembler` 等）源码已写好，但在 `dpe.sv` 内被注释（[dpe.sv:95-103](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv#L95-L103)），当前线上的是直通的 `dpe_dummy_switch`。这点 u4-l2/u4-l5 会展开。

**(f) 以太网子系统**

4 个 MAC 实例同构，以口 1 为例：

[top.sv:261-276](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv#L261-L276) — `u_eth_1`：GMII 物理信号（`e1_rxd` 等）接 MAC 的 `gmii_*` 端口；`speed` 端口连到 `to_csr.ethernet[0].status.speed.next`（MAC 把协商到的速率回报给 CSR）；`rx_fifo = from_eth_1`、`tx_fifo = to_eth_1`——这就是数据面包进出以太网的挂载点。

口 2/3/4 的实例在 [top.sv:279-330](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv#L279-L330)，结构完全相同，只是换了 `eX_*` 前缀与 `ethernet[X]` 索引。

最后是 PHY 管理实例，把 4 个网口的 `reset/mdc/mdio` 集中管理：

[top.sv:333-346](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv#L333-L346) — `u_phy`：集中驱动 4 路 PHY 的复位与 MDIO 管理（初始化 Realtek PHY、监测 link up/down，u8-l3 详述）。第 347 行 `endmodule` 收尾。

#### 4.2.4 代码实践（本讲核心实践）

**实践目标**：在 `top.sv` 中追踪一个以太网**接收**包，从物理引脚 `e1_rxd` 一路跟到数据面入口 `from_eth_1`，标注沿途经过的每一个实例与端口。

**操作步骤**：

1. 物理入口：在 [top.sv:55](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv#L55) 找到 `input [7:0] e1_rxd`，以及接收时钟/有效信号 `e1_rxc`(L52)、`e1_rxdv`(L53)、`e1_rxer`(L54)。
2. 进入 MAC：在 `u_eth_1` 实例 [top.sv:264-267](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv#L264-L267) 看到 `gmii_rxd(e1_rxd)`、`gmii_rx_clk(e1_rxc)`、`gmii_rx_dv(e1_rxdv)`、`gmii_rx_er(e1_rxer)`——接收信号被接到 `ethernet_mac` 的 GMII 接收端口。
3. MAC 内部转换：打开 [ethernet_mac.sv:43-58](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/ethernet_mac.sv#L43-L58)，看到 `rx_fifo` 是 `dpe_if.m_axis`（主机，往外发数据）。其内部 [ethernet_mac.sv:85-90](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/ethernet_mac.sv#L85-L90) 把 GMII 的 8 位流经 `eth_mac_1g_gmii_fifo` 转成 128 位 AXI-Stream（`rx_axis_tdata` 等接到 `rx_fifo.*`），同时完成跨时钟域与 store-and-forward 缓冲。
4. 出 MAC 接到 `from_eth_1`：回到 `u_eth_1` 实例 [top.sv:274](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv#L274)，`rx_fifo(from_eth_1)`——MAC 输出的包流进了第 4.2.3(b) 节声明的那条 `from_eth_1` 电缆。
5. 进入 DPE：在 `u_dpe` 实例 [top.sv:248](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv#L248) 看到 `from_eth_1(from_eth_1)`，电缆的另一头插进了 DPE 的 `s_axis` 从机口；进入 `dpe` 内部 [dpe.sv:67-76](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv#L67-L76) 可见它进了 `dpe_multiplexer` 的 `from_eth_1` 输入。

**需要观察的现象**：一个 8 位 @125 MHz 的 GMII 接收流，经过 MAC 实例后变成了 128 位 AXI-Stream（带 `tvalid/tready/tlast/tkeep` + 元数据），再经一根名为 `from_eth_1` 的 `dpe_if`「电缆」送进 DPE 的多路复用器。

**预期结果**：你能画出一条链 `e1_rxd → u_eth_1.gmii_rxd → (eth_mac_1g_gmii_fifo 内部 GMII→AXIS) → u_eth_1.rx_fifo → from_eth_1 → u_dpe.from_eth_1 → dpe_multiplexer`，并标注每一跳是 8 位 GMII 还是 128 位 AXIS。

> 待本地验证：本实践是「源码阅读型」，无需上板。若要观察真实波形，可在仿真（u7-l1）中对 `from_eth_1.tdata` 等信号打波形，配合 `VUserMainUdp`（u7-l4）注入一个 UDP 包。

#### 4.2.5 小练习与答案

**练习 1**：`from_csr` 这条「线」由谁驱动？又被谁消费？请至少说出三个消费者。
> **答**：`from_csr` 由 `u_soc_csr`（其内部的 PeakRDL `csr` 模块）驱动（[top.sv:199-203](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv#L199-L203)）。消费者（按字段分接）包括：`u_uart`（[top.sv:213](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv#L213)）、`u_cpu_fifo`（[top.sv:226](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv#L226)）、`u_dpe`（[top.sv:244](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv#L244)）、以及 GPIO 的 `from_csr.gpio.led*`（[top.sv:235-236](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv#L235-L236)）。

**练习 2**：为什么 `u_fabric` 的 `cpu`/`uart` 是 `SLV`，而 `dmem`/`csr` 是 `MST`？
> **答**：fabric 是「中间桥」。对上游 CPU 而言它是从机（`cpu` 端口接 CPU 的主口）；对下游 DMEM/CSR 而言它是主机（它代表 CPU 去驱动 DMEM/CSR 的从口）。`uart` 被列为 SLV 是因为 UART 也作为主机访问总线（BUSR/BUSW），fabric 这一侧以从机身份接收 UART 的访问请求。

**练习 3**：数据面 5 进 5 出里，哪一路不属于以太网？它代表什么？
> **答**：`from_cpu` / `to_cpu` 这一路。它不属于以太网，而是 CPU 经 `cpu_fifo` 收发数据面包的通道（用于处理握手包等控制流量，详见 u3-l3）。

---

### 4.3 top.filelist 的组装规则

#### 4.3.1 概念说明

SystemVerilog 的综合不会自动「找到」所有源文件——你得告诉工具「要编译哪些文件、按什么顺序」。本项目用一份**文件清单**（filelist）来做这件事：[top.filelist](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.filelist)。

filelist 的本质是一行一个文件路径，被 Makefile（Vivado 的 `MakefileHW` 或 openXC7 链）展开后喂给综合器。它解决两个问题：

1. **跨目录拼装**：`top.sv` 的实例分散在 `ip.infra/`、`ip.dpe/`、`ip.cpu/`、`external_lib/`、`csr_build/generated-files/` 等多处，filelist 把它们汇总。
2. **编译顺序**：包（`*_pkg.sv`）和接口（`*_if.sv`）必须在使用它们的模块之前被编译。filelist 按依赖顺序排列。

#### 4.3.2 核心流程

filelist 用两个变量来避免硬编码绝对路径：

- `${HW_SRC}` —— 硬件源码根（指向 `1.hw/`）。
- `${BLD_DIR}` —— 构建产物目录（指向 `3.build/`），生成的 CSR 与 SW 产物在这里。

文件按**功能类别**分组，每组用注释行分隔。顺序大致是「底层依赖 → 上层模块 → 顶层」：

```
1. 目标专用原语（Xilinx PLL）
2. 外部 AXIS 库（FIFO/adapter 等，来自 verilog-axis）
3. 外部以太网库（MAC/GMII 等，来自 verilog-ethernet）
4. 公共包与接口（soc/dpe 的 pkg/if）
5. PeakRDL 生成的 CSR（csr_pkg.sv / csr.sv）+ 自研 soc_csr 包装
6. 通用基础设施（ethernet_mac/phy、soc_ram/fabric、clk_rst_gen、cpu_fifo、uart、tdp_ram...）
7. CPU（imem + picorv32 + soc_cpu 包装）—— 注意 +incdir+ 指向 SW 构建产物
8. DPE（dpe + mux + demux + dummy_switch；注意 disassembler 被注释）
9. 顶层 top.sv
```

这个顺序不是随意的：例如 `csr_pkg.sv`（第 42 行）必须在 `soc_csr.sv`（第 44 行）和 `top.sv`（第 77 行）之前，因为后两者都用到了 `csr_pkg::csr__in_t` 等类型。

#### 4.3.3 源码精读

filelist 开头声明用途：

[top.filelist:5-7](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.filelist#L5-L7) — 注释说明本清单用于构建 `top` 设计。

目标专用原语（PLL，只在 Xilinx 上综合有意义）：

[top.filelist:9-11](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.filelist#L9-L11) — `fpga_pll_80M.sv` 与 `fpga_pll_125M.sv`，供 `clk_rst_gen` 实例化。

外部以太网库（被 `ethernet_mac` 内部使用）：

[top.filelist:22-32](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.filelist#L22-L32) — `eth_mac_1g_gmii_fifo.v`、`eth_mac_1g_gmii.v`、`axis_gmii_rx/tx.v` 等。这些是 alexforencich 的 verilog-ethernet 库，是 4.2 实践里 MAC 内部 GMII↔AXIS 转换的真正实现。

公共包与接口（必须在使用前编译）：

[top.filelist:34-39](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.filelist#L34-L39) — `soc_pkg.sv`/`soc_if.sv`、`dpe_pkg.sv`/`dpe_if.sv`、`gmii_if.sv`。

PeakRDL 生成的 CSR（注意路径在 `${BLD_DIR}` 下，是构建产物）：

[top.filelist:41-44](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.filelist#L41-L44) — `csr_pkg.sv`/`csr.sv`（生成产物）+ 自研 `soc_csr.sv`（包装层，即 `u_soc_csr` 的实现）。这正是 u1-l4 所说「CSR 步骤先于 HW 步骤」的物理体现——HW 综合时要把生成的 CSR 文件纳入。

CPU 段，关键的 SW↔HW 耦合点：

[top.filelist:60-67](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.filelist#L60-L67) — `+incdir+${BLD_DIR}/sw_build` 把 SW 构建产物目录加入 include 搜索路径（因为 `imem.sv` 会 `\`include "imem.INIT.vh"`，即把固件二进制焊进指令存储器，详见 u1-l4）；随后是 `imem.sv`、picoRV32 内核 `picorv32.CHILI.sv` 与包装 `soc_cpu.PICORV32.sv`。

DPE 段，体现 Phase1 PoC 现状的关键证据：

[top.filelist:69-74](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.filelist#L69-L74) — 编入了 `dpe_multiplexer`、`dpe_demultiplexer`、`dpe_dummy_switch`，但 `dpe_wg_disassembler.sv` 被 `#` 注释掉了。这意味着综合时根本不会包含 WG 解封装块，印证了 u2-l1 与 4.2.3(e) 所说的「当前 bitstream 是 dummy_switch 直通」。

> 另外两处注释也透露了设计演进：[top.filelist:54](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.filelist#L54) 的 `debounce.sv` 与 [top.filelist:56](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.filelist#L56) 的 `sync_fifo_srl.sv` 都被注释——前者对应 `top.sv` 中按键「TODO 加消抖」的备注，后者是一种备选 FIFO 实现未启用。

最后是顶层：

[top.filelist:76-77](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.filelist#L76-L77) — `top.sv` 放在最后，因为它的所有依赖此时都已就位。

#### 4.3.4 代码实践

**实践目标**：把 filelist 与 `top.sv` 的实例一一对应，验证「顶层实例的每个模块都能在 filelist 里找到来源」。

**操作步骤**：

1. 在 [top.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv) 中列出 `top` 实例化的全部模块名：`clk_rst_gen`、`soc_cpu`、`soc_fabric`、`soc_ram`、`soc_csr`、`uart`、`cpu_fifo`、`dpe`、`ethernet_mac`、`ethernet_phy`。
2. 在 [top.filelist](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.filelist) 里为每个模块找到它对应的源文件。注意有些模块名与文件名不同（如 `soc_cpu` 在 `soc_cpu.PICORV32.sv`、`soc_ram` 在 `soc_ram.sv`）。
3. 找出 filelist 中**存在但 `top.sv` 未直接实例化**的文件（提示：它们多是被 `top` 的子模块间接实例化的，例如 `eth_mac_1g_gmii_fifo.v` 由 `ethernet_mac` 实例化、`picorv32.CHILI.sv` 由 `soc_cpu` 实例化）。

**需要观察的现象**：filelist 不仅包含 `top` 直接实例化的模块，还包含它们的**全部传递依赖**（子模块的子模块），直到外部库原语。综合器需要整棵依赖树。

**预期结果**：你会得到一张「`top` 直接实例 ↔ filelist 行号」对照表，并理解为什么 filelist 不能只列 `top.sv` 一个文件。

> 待本地验证：可选地，若已配置 Vivado 或 openXC7（u8-l1/u8-l2），可运行综合并观察其读入的文件列表，与 filelist 对照。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `csr_pkg.sv` 出现在 filelist 第 42 行，而用到它的 `top.sv` 在第 77 行？
> **答**：SystemVerilog 的类型（如 `csr_pkg::csr__in_t`）必须「先定义后使用」。`csr_pkg.sv` 定义了这些结构体类型，`top.sv` 与 `soc_csr.sv` 要用到它们，所以包文件必须排在最前面。filelist 的顺序就是编译顺序。

**练习 2**：filelist 中 `${BLD_DIR}` 与 `${HW_SRC}` 分别指向哪类文件？为什么 CSR 和 IMEM 用 `${BLD_DIR}`？
> **答**：`${HW_SRC}` 指手写源码（`1.hw/`），`${BLD_DIR}` 指构建产物（`3.build/`）。CSR 的 `csr.sv`/`csr_pkg.sv` 由 PeakRDL 生成、`imem.INIT.vh` 由 SW 交叉编译产物转换而来，都是构建期产物而非手写代码，所以走 `${BLD_DIR}`。这也解释了 u1-l4 强调的「SW 必须先于 HW 构建」——HW 综合时要 include 这些产物。

**练习 3**：如果你要把 `dpe_dummy_switch` 换成真正的 `dpe_wg_disassembler`，需要在 filelist 改什么？
> **答**：注释/删除 `dpe_dummy_switch.sv` 行（第 73 行），取消 `dpe_wg_disassembler.sv` 行（第 74 行）的注释，并把 `dpe.sv` 内部相应的实例化与 `dpe_egress_ip_lookup` 注释块（[dpe.sv:95-103](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.dpe/dpe.sv#L95-L103)）恢复。这正是 Phase1 → 后续阶段的「上线」操作。

---

## 5. 综合实践

**任务**：为 `top` 画一张完整的「实例框图 + 连线图」，把本讲三个最小模块串起来。

具体要求：

1. **端口层**：画出 `top` 的对外端口分组（时钟/复位、4×以太网 GMII、UART、按键、LED），标注每个以太网口的接收/发送/管理三类信号方向。
2. **实例层**：画出 `top` 内部的 10 个实例（`u_clk_rst_gen`、`u_cpu`、`u_fabric`、`u_dmem`、`u_soc_csr`、`u_uart`、`u_cpu_fifo`、`u_dpe`、`u_eth_1..4`、`u_phy`）。
3. **连线层**：用三种颜色/线型分别画出
   - `soc_if` 总线（`bus_cpu/bus_uart/bus_dmem/bus_csr`），
   - `dpe_if` 电缆（`from_cpu/from_eth_1..4`、`to_cpu/to_eth_1..4`），
   - CSR 硬件束（`to_csr`/`from_csr`，用虚线表示它是「分接」而非点对点）。
4. **追踪验证**：在你的图上用高亮标出「一个数据包从 `e1_rxd` 进入、经 `u_eth_1` 转成 AXIS、经 `from_eth_1` 进 `u_dpe`、再被 `u_dpe_demultiplexer` 分到 `to_eth_2`、经 `u_eth_2` 发往 `e2_txd`」的完整路径。

完成后，对照 4.2.3 与 4.2.4 的源码行号，确认图上每根线都能在 `top.sv` 中找到对应的声明与端口连接语句。这张图将是你后续阅读 u2-l3（时钟域）、u2-l4（fabric 译码）、Unit 4（DPE 内部）时的导航地图。

> 待本地验证：本实践以画图 + 源码核对为主。若想用工具辅助，可用 `grep` 在 `top.sv` 中统计 `dpe_if`、`soc_if`、`csr_pkg::csr__in_t` 出现的次数，验证你的连线数量是否吻合。

---

## 6. 本讲小结

- `top` 模块是纯「连线 + 实例化」的顶层外壳，对外暴露差分时钟/复位、4 路千兆以太网 GMII、UART、2 按键、2 LED。
- 内部按四大块实例化：时钟复位生成 `u_clk_rst_gen` → CPU 子系统（`u_cpu`/`u_fabric`/`u_dmem`/`u_soc_csr`/`u_uart`/`u_cpu_fifo`）→ DPE `u_dpe` → 以太网 4×`u_eth` + `u_phy`。
- 控制面用 `soc_if` 总线（vld/rdy 握手）互联，CPU 是唯一主机，`u_fabric` 做地址译码分发到 UART/DMEM/CSR。
- 数据面用 `dpe_if`（128 位 AXI-Stream + TUSER/TID 元数据）互联，4 网口 + CPU 共 5 进 5 出接 DPE。
- `to_csr`/`from_csr` 是 PeakRDL 生成的扁平 hwif 结构体束，被所有外设按字段分接（读用 `.value`、写回用 `.next`），译码只在 CPU 入口 `soc_csr` 里发生——这是控制面与数据面唯一的交汇点。
- `top.filelist` 按依赖顺序（包/接口 → 外部库 → 生成 CSR → 基础设施 → CPU → DPE → 顶层）组装跨目录源文件；当前 Phase1 PoC 在其中以注释 `dpe_wg_disassembler`、启用 `dpe_dummy_switch` 体现了「直通」现状。

---

## 7. 下一步学习建议

本讲你掌握了 `top` 的「壳」与「连线」。接下来建议：

- **u2-l3 时钟复位与三个时钟域**：本讲把 `sys_clk`/`eth_gtx_clk` 当作黑盒，下一讲深入 `clk_rst_gen` 与三个时钟域（125M@8bit / 80M@32bit / 80M@128bit）的划分依据，以及为何 MAC 与 DPE 之间需要跨域 FIFO。
- **u2-l4 SoC 互联 fabric 与总线**：本讲把 `u_fabric` 当作「地址译码黑盒」，下一讲打开它，看 CPU 一条 `vld` 访问是如何被译码到 DMEM 或 CSR 的。
- **u2-l5 UART 与 IMEM 在线编程**：本讲看到 `u_uart` 暴露了 `imem_we/imem_waddr/imem_wdat` 等烧写信号，下一讲讲清 UART 特殊模式如何不重综合就更新固件。
- 若你想提前理解控制面与数据面的「桥」，可先跳到 **Unit 3（CSR）**，再回头看本讲的 `to_csr`/`from_csr` 分接，会有更深的体会。
