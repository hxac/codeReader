# 时钟复位与三个时钟域

## 1. 本讲目标

本讲承接 u2-l2（top.sv 顶层模块）。在上一讲里，你看到了 top.sv 把一众子模块实例化并连线，但有一个细节我们刻意略过了：**这些模块并不都跑在同一个时钟上**。top.sv 里同时出现 `sys_clk` 和 `eth_gtx_clk` 两根时钟，DPE 内部又按位宽分成了三个域。本讲就要把这套时钟复位体系彻底讲透。

读完本讲，你应当能够：

- 说清楚 `clk_rst_gen` 如何把板卡上的 200 MHz 差分时钟变成 80 MHz 与 125 MHz 两路系统时钟；
- 解释 PLLE2_BASE 的分频参数怎么算出目标频率，以及 `sync_reset` 为什么「异步复位、同步释放」；
- 列出系统的三个时钟域（125 MHz@8 bit 接 MAC、80 MHz@32 bit 接 CPU/CSR、80 MHz@128 bit 跑 DPE 流水线）及其频率、位宽的取舍依据；
- 理解跨时钟域 FIFO 为何必须同时做「时钟域转换 + 位宽转换」；
- 手算出 README 里「4 Gbps 加密速率」和「6 Mpps 包速率」是怎么来的。

## 2. 前置知识

### 2.1 为什么 FPGA 需要「时钟域」

软件世界里的程序默认共享一个统一的「时间」。而 FPGA 是一片真实的硅片，里面有成百上千个触发器（flip-flop），每个触发器都靠**时钟上升沿**采样数据。如果让一片 FPGA 里所有触发器都用同一根时钟，听起来简单，但实际上：

- 不同的外设有不同的物理时钟要求。比如千兆以太网 PHY 规定 GMII 接口必须用 125 MHz、8 位并行，这是协议铁律，不能改。
- 不同的任务有不同的频率/位宽权衡。CPU 的总线用 32 位一个字很自然；但线速转发要吞下 4 路 1 Gbps，用 32 位就得跑很高频率，用 128 位宽总线就能把频率压低。

于是设计者把整个片上系统（SoC）切成几个**时钟域（clock domain）**：同一个域里所有触发器共享同一根时钟，可以放心地直接连线；跨域的信号则必须经过特殊处理（最常见的就是异步 FIFO），否则会采样到亚稳态（metastability）数据。

### 2.2 PLL 是什么

板卡上给的晶振时钟（本项目是 200 MHz）频率往往不是我们想要的。**锁相环（PLL, Phase-Locked Loop）** 是 FPGA 内部的硬核电路，能把输入时钟倍频/分频成各种输出频率。Xilinx 7 系列 FPGA 里最基础的 PLL 原语叫 `PLLE2_BASE`。

它的核心算式是：

\[
f_{\text{VCO}} = f_{\text{in}} \times \frac{\texttt{CLKFBOUT\_MULT}}{\texttt{DIVCLK\_DIVIDE}}, \qquad
f_{\text{out}} = \frac{f_{\text{VCO}}}{\texttt{CLKOUT0\_DIVIDE}}
\]

其中 VCO（压控振荡器）有固定的合法频率范围，先倍频到 VCO，再分频到目标频率。PLL 启动后需要一小段时间才能「锁住」，期间输出时钟不稳定，于是有一个 `LOCKED` 信号告诉我们「现在可以用了」。

### 2.3 异步复位、同步释放

复位信号如果直接接到成千上万个触发器的异步复位端，释放瞬间很容易撞上时钟沿，造成部分触发器「看到复位已撤」、另一部分「还没看到」，系统状态混乱。标准做法是用一个**复位同步器**做到「**异步复位、同步释放**」（async assert, sync deassert）：

- 复位**有效**时立刻拉低（异步，不等时钟）——保证确定的复位状态；
- 复位**释放**时，把信号串过两级触发器再放出（同步）——保证所有下游在同一两个时钟沿后才脱离复位。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [1.hw/ip.infra/clk_rst_gen.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/clk_rst_gen.sv) | 时钟复位总控：差分输入 → 两路 PLL → 两路同步复位，产出 `sys_clk`(80M)/`eth_gtx_clk`(125M) 与各自的复位 |
| [1.hw/fpgatech_lib/XILINX/fpga_pll_80M.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/fpgatech_lib/XILINX/fpga_pll_80M.sv) | 80 MHz 系统 PLL，综合用 `PLLE2_BASE`，仿真用行为模型 |
| [1.hw/fpgatech_lib/XILINX/fpga_pll_125M.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/fpgatech_lib/XILINX/fpga_pll_125M.sv) | 125 MHz 以太网 PLL，同样双变体 |
| [1.hw/external_lib/axis/sync_reset.v](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/external_lib/axis/sync_reset.v) | 复位同步器（异步复位、同步释放），来自 alexforencich 的 verilog-axis 库 |
| [1.hw/ip.infra/ethernet_mac.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/ethernet_mac.sv) | 以太网 MAC 包装器，是 125 M 与 80 M 域的交界，内含异步 FIFO |
| [1.hw/top.sv](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv) | 顶层，把 `sys_clk`/`eth_gtx_clk` 分配给各子模块 |
| [1.hw/README.md](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/README.md) | 给出三时钟域的定义与 4 Gbps / 6 Mpps 的速率推导 |

> 提示：`fpgatech_lib/XILINX/` 这层目录名说明本项目把「与 FPGA 厂商相关的硬核封装」做了隔离，方便将来移植到非 Xilinx 平台（或开源工具链）时只替换这一层。

## 4. 核心概念与源码讲解

### 4.1 PLL 时钟生成

#### 4.1.1 概念说明

板卡（Alinx AX7201）上有一颗 200 MHz 差分晶振，从 `clk_p`/`clk_n` 两根引脚进来。我们需要两种工作频率：

- **125 MHz**：千兆以太网 GMII 接口的法定频率；
- **80 MHz**：CPU、CSR、DPE 流水线共用的系统频率。

`clk_rst_gen` 就是这个频率变换的总控：它先用 `IBUFGDS` 把差分时钟转成单端，再分别喂给两个 PLL 实例。

#### 4.1.2 核心流程

```text
clk_p / clk_n (200MHz 差分)
        │
   [IBUFGDS] ──► clk (200MHz 单端)
        │
        ├──► fpga_pll_80M  ──► sys_pll_clk (80MHz)  + sys_pll_locked
        │                         │
        │                  [sync_reset N=4] ──► sys_rst / sys_rst_n
        │
        └──► fpga_pll_125M ──► eth_pll_clk (125MHz) + eth_pll_locked
                                  │
                           [sync_reset N=4] ──► eth_gtx_rst
```

#### 4.1.3 源码精读

先看 `clk_rst_gen` 的端口：差分时钟 `clk_p`/`clk_n`、异步低有效复位 `rst_n` 进；两路时钟 `sys_clk`/`eth_gtx_clk` 与各自复位出。

[1.hw/ip.infra/clk_rst_gen.sv:43-53](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/clk_rst_gen.sv#L43-L53) —— 模块端口定义，对外只暴露「两路时钟 + 两路复位」。

差分转单端用 Xilinx 原语 `IBUFGDS`：

[1.hw/ip.infra/clk_rst_gen.sv:60-65](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/clk_rst_gen.sv#L60-L65) —— 把 `clk_p`/`clk_n` 合成单端 `clk`，作为两路 PLL 的公共输入。

接着实例化两个 PLL。系统域：

[1.hw/ip.infra/clk_rst_gen.sv:74-79](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/clk_rst_gen.sv#L74-L79) —— 实例 `u_sys_pll` 产出 `sys_pll_clk` 与锁定信号 `sys_pll_locked`。

[1.hw/ip.infra/clk_rst_gen.sv:89-91](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/clk_rst_gen.sv#L89-L91) —— 把 PLL 输出直连为 `sys_clk`；`sys_rst` 高有效、`sys_rst_n` 低有效两套都给（不同子模块用不同极性）。

以太网域：

[1.hw/ip.infra/clk_rst_gen.sv:99-104](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/clk_rst_gen.sv#L99-L104) —— 实例 `u_eth_pll` 产出 `eth_pll_clk`(125MHz)。

[1.hw/ip.infra/clk_rst_gen.sv:114](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/clk_rst_gen.sv#L114) —— `eth_gtx_clk = eth_pll_clk`。

现在打开 PLL 内部，看 80 MHz 是怎么算出来的。`fpga_pll_80M` 用 `PLLE2_BASE`，关键参数：

[1.hw/fpgatech_lib/XILINX/fpga_pll_80M.sv:76-102](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/fpgatech_lib/XILINX/fpga_pll_80M.sv#L76-L102) —— `CLKIN1_PERIOD=5.0`（即 200 MHz 输入），`CLKFBOUT_MULT=4`，`DIVCLK_DIVIDE=1`，`CLKOUT0_DIVIDE=10`。

代入公式：

\[
f_{\text{VCO}} = 200 \times \frac{4}{1} = 800\ \text{MHz}, \qquad
f_{\text{out}} = \frac{800}{10} = 80\ \text{MHz}\ \checkmark
\]

输出再过一颗 `BUFG`（全局时钟缓冲），保证时钟到达芯片各处的歪斜（skew）最小：

[1.hw/fpgatech_lib/XILINX/fpga_pll_80M.sv:121-125](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/fpgatech_lib/XILINX/fpga_pll_80M.sv#L121-L125) —— `BUFG` 把 PLL 送上全局时钟树。

125 MHz 的算法同理，只是倍频/分频比不同：

[1.hw/fpgatech_lib/XILINX/fpga_pll_125M.sv:76-102](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/fpgatech_lib/XILINX/fpga_pll_125M.sv#L76-L102) —— `CLKFBOUT_MULT=5`、`CLKOUT0_DIVIDE=8`：

\[
f_{\text{VCO}} = 200 \times \frac{5}{1} = 1000\ \text{MHz}, \qquad
f_{\text{out}} = \frac{1000}{8} = 125\ \text{MHz}\ \checkmark
\]

> 小贴士：为什么不全用 125 MHz 一根时钟省事？因为 125 MHz 的 8 位 GMII 域是给以太网 PHY 用的物理接口；而 80 MHz 是为了让 128 位宽的 DPE 流水线在较低频率下也能喂饱 4 Gbps（见 4.3）。两套频率服务于两个不同的工程目标。

#### 4.1.4 仿真专用变体（SIM_ONLY）

`PLLE2_BASE` 是 Xilinx 专有原语，开源仿真器（如 Verilator/Icarus）未必有对应的仿真模型。于是两个 PLL 都用 `\`ifdef SIM_ONLY` 提供一个纯行为模型：

[1.hw/fpgatech_lib/XILINX/fpga_pll_80M.sv:59-69](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/fpgatech_lib/XILINX/fpga_pll_80M.sv#L59-L69) —— 仿真模型里 `#6_250 ps` 翻转一次，周期 \(6250\times 2 = 12500\ \text{ps} = 12.5\ \text{ns}\)，正好 80 MHz；`#40_000 ps`(40 ns) 后置 `locked=1`。

[1.hw/fpgatech_lib/XILINX/fpga_pll_125M.sv:59-69](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/fpgatech_lib/XILINX/fpga_pll_125M.sv#L59-L69) —— `#4_000 ps` 翻转，周期 8 ns = 125 MHz；25 ns 后锁定。

这种「综合走硬核、仿真走行为模型」的双写法，是跨工具链（Vivado + 开源仿真）项目的常见手法，也是本工程能在 openXC7 与 VProc 仿真之间复用同一份 RTL 的前提。

#### 4.1.5 代码实践

**实践目标**：验证 PLL 分频参数确实算出 80 MHz 与 125 MHz。

**操作步骤**：

1. 打开 `fpga_pll_80M.sv`，记录 `CLKFBOUT_MULT`、`DIVCLK_DIVIDE`、`CLKOUT0_DIVIDE`、`CLKIN1_PERIOD`。
2. 用本节的 VCO 公式手算 `f_out`。
3. 对 `fpga_pll_125M.sv` 重复一遍。

**需要观察的现象**：两组参数分别算出 80 MHz 与 125 MHz。

**预期结果**：80 M 模块 → 80 MHz；125 M 模块 → 125 MHz。（待本地验证：若你装了 Vivado，可用 `report_clocks` 或时序报告核对。）

#### 4.1.6 小练习与答案

**练习 1**：若想从 200 MHz 输入得到 50 MHz 输出，保持 `CLKFBOUT_MULT=4`，`CLKOUT0_DIVIDE` 应设为多少？

**答案**：VCO=800 MHz，`CLKOUT0_DIVIDE = 800/50 = 16`。

**练习 2**：为什么 PLL 输出都要串一颗 `BUFG`？

**答案**：`BUFG` 是全局时钟缓冲，驱动低歪斜的全局时钟网络；不经过它的话时钟走普通布线，歪斜大，会导致保持时间违例。

---

### 4.2 复位同步

#### 4.2.1 概念说明

PLL 上电后，输出时钟先抖动一段时间才稳定，期间 `LOCKED=0`。我们绝不能让下游电路在时钟没稳定时就脱离复位。`clk_rst_gen` 的策略是：**用 `~pll_locked` 当作复位源**——PLL 没锁就一直复位，锁住之后才经过同步器「干净地」释放复位。

本项目复用了 verilog-axis 库里的 `sync_reset`：一个参数化深度的移位寄存器，实现「异步复位、同步释放」。

#### 4.2.2 核心流程

```text
        rst (异步, 来自 ~pll_locked)
         │
   ┌─────┴─────┐  posedge clk ──►  逐级移位
   │ N 级触发器 │   rst=1 时全部置 1（立刻复位）
   └─────┬─────┘   rst=0 后经 N 拍才把 0 移出（同步释放）
         │
        out ──► 给本时钟域所有子模块的复位
```

关键点：复位**置位**走的是 `posedge rst`（异步、不等时钟）；复位**撤除**靠的是寄存器在 `posedge clk` 下逐级移位（同步、对齐本域时钟）。

#### 4.2.3 源码精读

`clk_rst_gen` 里系统域的复位同步：

[1.hw/ip.infra/clk_rst_gen.sv:81-87](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/clk_rst_gen.sv#L81-L87) —— `sync_reset #(.N(4))`，输入 `clk=sys_pll_clk`、`rst=~sys_pll_locked`、输出 `sys_reset`。注意复位源就是「PLL 未锁」。

[1.hw/ip.infra/clk_rst_gen.sv:106-112](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/clk_rst_gen.sv#L106-L112) —— 以太网域同理，`rst=~eth_pll_locked` → `eth_gtx_rst`。

`sync_reset` 的实现非常精炼：

[1.hw/external_lib/axis/sync_reset.v:26-37](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/external_lib/axis/sync_reset.v#L26-L37) —— `sync_reg` 初值全 1（上电即复位）；`always @(posedge clk or posedge rst)` 表示复位异步生效；一旦 `rst=0`，每个时钟沿把一个 0 移入最高位，经 N 拍后 `out` 才变 0。

> 设计意图：每个时钟域**各自用自己的时钟**做同步释放。系统域用 80 MHz 同步器、以太网域用 125 MHz 同步器，互不干扰。这是多时钟域设计的标准纪律——复位同步器必须工作在它要复位的那个时钟下。

#### 4.2.4 代码实践

**实践目标**：理解 `sync_reset` 上电时的复位时序。

**操作步骤（源码阅读型）**：

1. 读 [sync_reset.v:31-37](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/external_lib/axis/sync_reset.v#L31-L37)。
2. 设 `N=4`、`rst` 一直为 1，回答 `sync_reg` 与 `out` 各是什么值。
3. 设 `rst` 在某时刻变为 0，画出此后 4 个 `clk` 沿里 `sync_reg[3:0]` 的逐拍变化。

**预期结果**：`rst=1` 时 `sync_reg=4'b1111`、`out=1`；`rst=0` 后 `sync_reg` 依次变成 `0111→0011→0001→0000`，`out` 在第 4 拍才落 0。这就是「同步释放」的延迟来源。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `sync_reset` 的 `N` 从 4 改成 2，会有什么影响？

**答案**：复位释放延迟从 4 拍降到 2 拍（更快脱离复位），但抗亚稳态的余量变小（同步级数少一级，MTBF 变差）。一般 N≥2 即可，4 是很保守的选择。

**练习 2**：为什么 `clk_rst_gen` 给系统域和以太网域**各**放一个 `sync_reset`，而不是共用一个？

**答案**：复位释放必须对齐各自的目标时钟。系统域的复位要在 80 MHz 时钟域里同步，以太网域要在 125 MHz 时钟域里同步，二者异步，不能共用同一个同步器输出。

---

### 4.3 三时钟域划分

#### 4.3.1 概念说明

有了两路时钟，整个 SoC 被划成三个域。注意：这里「域」的区别**既有频率也有位宽**。README 把它们用颜色区分，是理解整个数据流向的关键地图。

[1.hw/README.md:29-32](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/README.md#L29-L32) —— 官方对三域的定义。

| 域（颜色） | 时钟 | 位宽 | 用途 | 带宽（频率×位宽） |
|------------|------|------|------|-------------------|
| 蓝色 | 125 MHz | 8 bit | DPE ↔ 1G MAC（GMII SDR） | 1 Gbps |
| 红色 | 80 MHz | 32 bit | DPE ↔ CPU / 全部 CSR 外设 | 2.56 Gbps |
| 绿色 | 80 MHz | 128 bit | DPE 流水线内部包传输 | ~10 Gbps |

#### 4.3.2 核心流程：每个域的位宽/频率依据

**蓝色域（125 MHz @ 8 bit）**：这不是随便选的，是 GMII 协议的硬性规定。SDR GMII 在每个 125 MHz 时钟沿传 8 bit，正好：

\[
125\ \text{MHz} \times 8\ \text{bit} = 1000\ \text{Mbit/s} = 1\ \text{Gbps}
\]

这就是千兆以太网「1 Gbps」的物理来源。

**红色域（80 MHz @ 32 bit）**：CPU（picoRV32）和所有 CSR 外设都挂在 32 位总线上。注意 [README:36](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/README.md#L36) 的说明：WireGuard 握手报文是**偶发**的（连接初始化 + 每隔几分钟轮换密钥），控制面流量对带宽几乎无要求，所以这里**故意不用 DMA**，让 CPU 直接经 CSR 慢慢读写 Tx/Rx FIFO 就够了。32 位够宽（一个 CPU 字），80 MHz 够慢（省功耗、好时序），这正是控制面的恰当取舍。

**绿色域（80 MHz @ 128 bit）**：这是数据面的「高速公路」。DPE 要把 4 路以太网的流量复用到一条流水线上线速处理，所以位宽拉到 128 bit。算一下它的「理论带宽」：

\[
80\ \text{MHz} \times 128\ \text{bit} = 10240\ \text{Mbit/s} \approx 10\ \text{Gbps}
\]

这就是 README 里「DPE 大约以 10 Gbps 传包」的来源。绿色域和红色域**同频 80 MHz**，但位宽不同（128 vs 32），所以严格说它们是「同频不同位宽」的两个子域——CPU 经 `cpu_fifo` 做 128↔32 的位宽转换接入绿色域（见 4.4 与 u3-l3）。

#### 4.3.3 源码精读：top.sv 如何分配时钟

在 top.sv 里，两路时钟被分发给不同模块：

[1.hw/top.sv:126-135](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv#L126-L135) —— 实例化 `clk_rst_gen`，得到 `sys_clk`/`sys_rst`/`sys_rst_n`/`eth_gtx_clk`/`eth_gtx_rst`。

绿色/红色域的所有 `dpe_if` 接口都绑定到 `sys_clk`（80 MHz）：

[1.hw/top.sv:140-149](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv#L140-L149) —— 9 个 `dpe_if`（5 进 4 出，含 CPU）全部 `.clk(sys_clk), .rst(sys_rst)`。128 位 TDATA 在这里定义：

[1.hw/ip.infra/dpe_if.sv:43-57](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/dpe_if.sv#L43-L57) —— `logic [127:0] tdata;` 即绿色域的 128 位包数据。

蓝色域（125 MHz）则喂给每个 `ethernet_mac`：

[1.hw/top.sv:262-263](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/top.sv#L262-L263) —— `gtx_clk(eth_gtx_clk)`（125 M）与 `gtx_rst(eth_gtx_rst)`。

#### 4.3.4 代码实践

**实践目标**：用一张表把三个域的参数钉死，并算出每个域的带宽。

**操作步骤**：

1. 仿照本节的表格，自己重画一张「域 / 时钟 / 位宽 / 用途 / 带宽」五列对照表。
2. 用 `频率 × 位宽` 公式手算三域带宽，标注单位。
3. 在 top.sv 里用搜索找到所有 `.clk(sys_clk)` 与 `eth_gtx_clk` 的连接点，确认归属。

**预期结果**：蓝 1 Gbps、红 2.56 Gbps、绿 ~10 Gbps。（待本地验证：实际有效带宽会被有效载荷比例与流水线握手占用率拉低。）

#### 4.3.5 小练习与答案

**练习 1**：绿色域若改成 32 位宽、要维持 ~10 Gbps，频率需要多少？为什么作者不这么干？

**答案**：\(10\,\text{Gbps}/32\,\text{bit} \approx 312.5\,\text{MHz}\)，远超 Artix-7 核心逻辑的舒适频率（<100 MHz 量级），时序极难收敛。所以选择「加宽位宽、降低频率」。

**练习 2**：红色域带宽（2.56 Gbps）看似比单口以太网（1 Gbps）还高，为什么 README 还说 CPU 接口跑不满数据面？

**答案**：因为 `cpu_fifo` 把 128 位拆成 4 个 32 位 CSR 字、CPU 要用约 10 步逐字读写（详见 u3-l3），有效吞吐远低于理论 2.56 Gbps；但这无所谓，控制面只处理偶发的握手包，不需要线速。

---

### 4.4 跨域 FIFO：同时做时钟域转换与位宽转换

#### 4.4.1 概念说明

蓝色域（125 MHz @ 8 bit）和绿色域（80 MHz @ 128 bit）既不同频、也不同宽，DPE 又必须和 MAC 互通——直接连必错。解决办法是在 MAC 与 DPE 之间放一个**异步 FIFO**：

- **时钟域转换（CDC）**：写端用一侧时钟、读端用另一侧时钟，FIFO 内部用格雷码指针跨域，避免亚稳态；
- **位宽转换**：写端 8 bit 进、读端 128 bit 出（或反向），FIFO 自动按比例攒/拆数据；
- **存储转发（store-and-forward）**：收完整帧再向 DPE 上游汇报，方便按包仲裁。

这正是 README 里 Rx/Tx FIFO 的三重职责。

[1.hw/README.md:17](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/README.md#L17) —— Rx FIFO 的职责：clock domain crossing, bus width conversion, and store & forward。

#### 4.4.2 核心流程

```text
   GMII 8bit @125MHz                    AXIS 128bit @80MHz (sys_clk)
       │                                            ▲
       ▼                                            │
  ┌─────────────────────────────────────────────────┴──┐
  │ eth_mac_1g_gmii_fifo                                │
  │  ─ 写端 gtx_clk(125M)/8bit   读端 logic_clk(80M)/128bit
  │  ─ 内部 axis_async_fifo_adapter：异步 + 位宽适配
  │  ─ 帧级缓冲（store & forward）                        │
  └─────────────────────────────────────────────────────┘
       ▲                                            │
       │                                            ▼
   PHY/MAC 物理层                           DPE 绿色域流水线
```

位宽比 128/8 = 16，所以 MAC 侧每写 16 个字节（16 个 125 MHz 拍），FIFO 才向 DPE 侧吐 1 个 128 bit beat；两边速率自然平衡（1 Gbps 进、1 Gbps 出）。

#### 4.4.3 源码精读

`ethernet_mac` 是自研包装器，它把外部库的 `eth_mac_1g_gmii_fifo` 夹在「蓝色 GMII」和「绿色 AXIS」之间：

[1.hw/ip.infra/ethernet_mac.sv:43-58](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/ethernet_mac.sv#L43-L58) —— 端口：`gtx_clk`/`gtx_rst` 是蓝色域；`tx_fifo`/`rx_fifo` 是 `dpe_if`（绿色域，128 位 AXIS）。

关键在 `logic_clk`/`logic_rst` 接到了 `tx_fifo.clk`（也就是 `sys_clk`）：

[1.hw/ip.infra/ethernet_mac.sv:73-76](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/ethernet_mac.sv#L73-L76) —— `gtx_clk(gtx_clk)`（125 M）与 `logic_clk(tx_fifo.clk)`（80 M）并存，证明这是真正的双时钟 FIFO；同时 `AXIS_DATA_WIDTH(128)` 把绿色侧定成 128 位。

进入外部库看异步 FIFO 实体：

[1.hw/external_lib/ethernet/eth_mac_1g_gmii_fifo.v:238](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/external_lib/ethernet/eth_mac_1g_gmii_fifo.v#L238) 与 [L298](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/external_lib/ethernet/eth_mac_1g_gmii_fifo.v#L298) —— 内部用 `axis_async_fifo_adapter`，分别接 `.s_clk(logic_clk)` / `.m_clk(...)` 与反向，名字里的 `async` 正是「跨时钟域」之意。

CPU 侧的 `cpu_fifo` 则是另一类转换：**同频 80 MHz、128↔32 位宽转换**（红↔绿），它把 128 位 `tdata` 拆成 4 个 32 位 CSR 字段：

[1.hw/ip.infra/cpu_fifo.sv:73-76](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/ip.infra/cpu_fifo.sv#L73-L76) —— `{data_127_96, data_95_64, data_63_32, data_31_0}` 四段拼回 128 位，是红/绿域位宽接力的现场（细节留给 u3-l3）。

> 把两处 FIFO 放在一起看：以太网侧 FIFO 解决「125 M↔80 M + 8bit↔128bit」，CPU 侧 FIFO 解决「同频 80 M + 128bit↔32bit」。两者共同把蓝色 PHY 世界、绿色 DPE 世界、红色 CPU 世界缝合成一个整体。

#### 4.4.4 代码实践

**实践目标**：把「跨域 FIFO 同时承担频率转换与位宽转换」在源码里坐实。

**操作步骤（源码阅读型）**：

1. 打开 `ethernet_mac.sv`，确认 `gtx_clk` 与 `logic_clk(tx_fifo.clk)` 是两个不同时钟。
2. 打开 `eth_mac_1g_gmii_fifo.v` 第 238、298 行附近，确认内部是 `axis_async_fifo_adapter`（async）。
3. 回到 top.sv，确认 `tx_fifo.clk` 来自 `sys_clk`（80 M），而 `gtx_clk` 来自 `eth_gtx_clk`（125 M）。
4. 核算位宽比：MAC 侧 8 bit vs AXIS 侧 128 bit = 16:1。

**预期结果**：能画出「PHY(8bit@125M) → 异步FIFO → DPE(128bit@80M)」的标注框图，并指出 16:1 的位宽比。

#### 4.4.5 小练习与答案

**练习 1**：为什么不能直接把蓝色域的 8 位 GMII 数据接到绿色域的 128 位总线上？

**答案**：一是跨时钟域（125 M vs 80 M）会采样到亚稳态；二是位宽不匹配（8 vs 128）。必须用异步 FIFO 同时解决频率与位宽。

**练习 2**：位宽比是 16:1，MAC 写 16 个字节 DPE 才读 1 个 beat。如果一帧不足 16 字节整数倍，FIFO 怎么保证不丢尾？

**答案**：用 AXI-Stream 的 `TLAST`（帧结束）与 `TKEEP`（最后一 beat 有效字节掩码）来标记。FIFO 在帧尾强制吐出最后一个不完整 beat，并用 `TKEEP` 告诉下游哪些字节有效（`dpe_if` 里就有 `tlast` 与 `tkeep[15:0]`）。

---

## 5. 综合实践：核算 4 Gbps 加密速率与 6 Mpps 包速率

这是本讲的收官任务，把三个域的参数与 README 的性能指标串起来。README 的原文推导在：

[1.hw/README.md:38](https://github.com/chili-chips-ba/wireguard-fpga/blob/9887a3b39a1bb6aff9642f3a21ea4a8863f3dfaf/1.hw/README.md#L38) —— 性能推导段落。

### 任务 A：4 Gbps 从哪来

系统有 4 个 1 Gbps 以太网口，要保证线速全双工转发，加密核必须吞得下 4 路总流量：

\[
4 \times 1\ \text{Gbps} = 4\ \text{Gbps}
\]

所以「加密核至少要 4 Gbps」是 4 口线速的硬下限。而绿色域 ~10 Gbps 的带宽（80 MHz × 128 bit）为它留出了充足余量。

### 任务 B：6 Mpps 从哪来

对 IP 查找这类**只看包头**的模块，关键不是数据率而是**包率**。最坏情况是全网都用最小包（64 字节）。1 Gbps 链路上 64 字节小包的极限包率要算上帧间隔（IFG 12 字节）与前导码（preamble+SFD 8 字节）：

\[
\text{每帧在线上的比特数} = (8 + 64 + 12)\ \text{字节} \times 8 = 84 \times 8 = 672\ \text{bit}
\]

\[
\text{单口包率} = \frac{10^9\ \text{bit/s}}{672\ \text{bit}} \approx 1\,488\,095\ \text{pps} \approx 1.488\ \text{Mpps}
\]

（README 写作 1,488,096 pps，取整差异。）4 路口合计：

\[
4 \times 1.488\ \text{Mpps} \approx 5.95\ \text{Mpps} \approx 6\ \text{Mpps}
\]

这就是「6 Mpps / 每秒 6 百万次 IP 表查找」的来源。

### 操作步骤

1. 准备一张三列表：指标 | README 数值 | 你的推导。
2. 填入「4 Gbps」「单口 1.488 Mpps」「4 口合计 ~6 Mpps」三行。
3. 在旁边写出每一步算式（如上）。
4. 反思：绿色域 10 Gbps 远大于 4 Gbps，为什么还要留这么大余量？提示——复用后 DPE 流水线要在同一时刻承载多路已切到 128 位的流量，且 IP 查找等阶段会引入缓冲与流水气泡。

**预期结果**：得到一张可复算的性能推导表，能向别人解释「4 Gbps 与 6 Mpps 不是拍脑袋，而是 4 口 1G + 最小包假设的必然结果」。（待本地验证：真实极限包率受 MAC 的 IFG/preamble 精确配置影响，可能在小数位上有出入。）

## 6. 本讲小结

- `clk_rst_gen` 把 200 MHz 差分板载时钟经 `IBUFGDS` 转单端，再用两颗 `PLLE2_BASE` PLL 分频出 **80 MHz（系统）** 与 **125 MHz（以太网）** 两路时钟，各配 `BUFG` 上全局时钟树。
- 80 MHz 由 `CLKFBOUT_MULT=4 / CLKOUT0_DIVIDE=10`（VCO 800 M）得到；125 MHz 由 `5 / 8`（VCO 1000 M）得到；PLL 还提供 `SIM_ONLY` 行为模型供开源仿真用。
- 复位采用「**异步复位、同步释放**」：用 `~pll_locked` 当复位源，每个时钟域**各自**用 `sync_reset #(N=4)` 在自己的时钟下同步释放，保证 PLL 未锁期间整个域保持复位。
- 系统划成三个域：**蓝色 125 MHz@8 bit（接 MAC）、红色 80 MHz@32 bit（CPU/CSR）、绿色 80 MHz@128 bit（DPE 流水线）**；频率与位宽都各有依据（GMII 协议、CPU 字宽、线速余量）。
- 蓝色↔绿色之间用 `eth_mac_1g_gmii_fifo` 里的 **`axis_async_fifo_adapter`** 同时完成跨时钟域（125 M↔80 M）与位宽转换（8↔128，比 16:1），并做存储转发；红色↔绿色由 `cpu_fifo` 做同频 128↔32 位宽转换。
- README 的 **4 Gbps = 4×1G 线速下限**、**6 Mpps = 4 口 × 64 字节最小包的极限包率**，都能用本讲的频率/位宽参数手算复现。

## 7. 下一步学习建议

本讲把「时钟复位」和「三域边界」讲清了，但**域内部**的总线协议还没展开。建议接着学：

- **u2-l4（SoC 互联 fabric 与 soc_if 总线）**：红色域内部 CPU↔DMEM↔CSR 的 `vld/rdy` 握手与地址译码，是理解控制面的下一块拼图。
- **u3-l3（CPU FIFO：AXIS 到 CSR 的映射）**：把本讲提到的 `cpu_fifo` 128↔32 位宽转换展开成 CPU 收发包的 10 步流程。
- 若你对跨时钟域的底层原理感兴趣，可额外阅读 verilog-axis 库里 `axis_async_fifo` / `sync_reset` 的实现，以及 Xilinx UG472（7 系列 Clocking Resources 用户指南）对 PLLE2/BUFG 的官方说明。
