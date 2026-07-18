# u3-l1 PCIe 核心封装与 pcie_7x_0 IP

## 1. 本讲目标

本讲是「PCIe 核心与 TLP 处理」单元的第一篇。我们将打开三大子系统中的最后一块——`pcie_a7`（PCIe 核心子系统），看它如何把 Xilinx 提供的「PCIe 硬核 IP」包成一个可被工程其它部分调用的模块。

学完后你应当能够：

1. 说出 `pcileech_pcie_a7` 模块在整板中的职责——它是 PCIe 硬核 IP 与系统之间的「**适配层 / 封装层**」。
2. 看懂本模块的三层复位：`rst`（顶层复位）、`rst_subsys`（子系统软复位）、`rst_pcie`（PCIe 硬核硬复位），并说清后两者的关键区别。
3. 理解差分参考时钟 `pcie_clk_p/n` 如何经 `IBUFDS_GTE2` 进入 FPGA，成为 PCIe GTP 收发器的工作时钟。
4. 在 `pcie_7x_0` IP 的几十页端口中，按「发送 / 接收 / 配置管理 / 配置状态 / 物理层 / DRP / 用户接口」分组读懂它们各自的作用。
5. 跟踪一条真实的接收数据流：从 PCIe 硬核的 64 位 `m_axis_rx` 输出，经位宽转换模块 `pcileech_tlps128_src64`，变成工程内部使用的 128 位 AXIS 流。

---

## 2. 前置知识

本讲建立在 **u1-l4（顶层三大子系统）** 和 **u2-l1（interface 与 modport）** 之上。开始前请确认你理解下面几个概念：

- **三大子系统**：顶层 `pcileech_squirrel_top.sv` 例化了 `com`（USB3 通信）、`fifo`（路由与控制中枢）、`pcie_a7`（PCIe 核心）三大子系统，三者用 interface 相连。本讲的主角就是 `pcie_a7`。
- **interface / modport 契约**：一组信号打包成「粗电缆」叫 interface；从某一模块视角规定每个信号是 input 还是 output 叫 modport。两端 modport 方向必然互补。本模块对外暴露 4 个 interface：`dfifo_cfg`、`dfifo_tlp`、`dfifo_pcie`、`dshadow2fifo`，全部来自 u2-l1 讲过的 `pcileech_header.svh`。

补充几个 PCIe 与 FPGA 硬件的通俗概念：

- **PCIe 硬核 IP（Hard IP）**：Xilinx 7 系列 FPGA 里有一块专门为 PCIe 协议做好的固化电路（含 GTP 高速收发器、链路训练、配置空间寄存器等）。开发者不需要用 HDL 从零写一个 PCIe 控制器，只要把这块 IP 例化进来、按它规定的端口接线即可。在 pcileech-fpga 里这块 IP 叫 `pcie_7x_0`。
- **GTP / GTPE2_CHANNEL**：FPGA 里负责高速串行收发（SerDes）的物理层单元。PCIe 的差分信号 `tx_p/n`、`rx_p/n` 必须经过它才能与芯片外的金手指电气连接。
- **参考时钟（REFCLK）**：PCIe 板卡插槽上会提供一对 100MHz 差分参考时钟（`pcie_clk_p/n`），GTP 必须拿它做频率基准才能正确锁定数据。
- **PERST#**：PCIe 复位信号，由主板在启动或热复位时拉低，强制设备复位其 PCIe 链路。`pcie_perst_n` 就是这个引脚（`_n` 表示低有效）。
- **位宽转换（Width Conversion）**：PCIe 硬核对外吐的是 **64 位**数据流，而工程内部（尤其 TLP 处理）统一用 **128 位** AXIS 流。两者之间需要一个「把两个 64 位拼成一个 128 位」的模块。本模块里就是 `pcileech_tlps128_src64`（接收方向，64→128）和 `pcileech_tlps128_dst64`（发送方向，128→64）。

---

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [PCIeSquirrel/src/pcileech_pcie_a7.sv](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv) | 本讲主角。封装 `pcie_7x_0` IP，管理复位/时钟/LED，并例化 cfg、tlp 子模块与位宽转换模块。文件末尾还定义了 `pcileech_tlps128_dst64` 与 `pcileech_tlps128_src64` 两个位宽转换模块。 |
| [PCIeSquirrel/src/pcileech_header.svh](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh) | 全部 interface 定义。本讲重点引用 `IfPCIeSignals`（IP 配置/状态信号包）、`IfPCIeTlpRxTx`（64 位收发流）、`IfAXIS128`（128 位流）、`IfPCIeFifoCore`（含 DRP 与复位控制）。 |
| [PCIeSquirrel/ip/pcie_7x_0.xci](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/ip/pcie_7x_0.xci) | Xilinx PCIe 7 系列 IP 的配置文件（VLNV `xilinx.com:ip:pcie_7x:3.3`），记录了 Vendor/Device ID、通道宽度等参数。 |

> 主参考工程仍是 `PCIeSquirrel`（Artix-7 XC7A35T，PCIe x1）。其它设备目录里同名文件结构高度相似。

---

## 4. 核心概念与源码讲解

### 4.1 顶层封装 pcileech_pcie_a7：把 PCIe 硬核 IP 包成「子系统」

#### 4.1.1 概念说明

为什么要「封装」？Xilinx 的 `pcie_7x` IP 是一个黑盒：它对外暴露了上百个信号，命名风格（`s_axis_tx_tdata`、`cfg_mgmt_dwaddr`、`pl_ltssm_state` …）都是 Xilinx 自己的约定，并且裸露在 64 位数据宽度上。如果让顶层 `pcileech_squirrel_top` 直接去例化这块 IP，会面临两个问题：

1. **信号太多太杂**：顶层会被几百根细线淹没，可读性极差。
2. **数据宽度不匹配**：IP 用 64 位流，工程内部（fifo、tlp 处理）用 128 位 AXIS 流，需要转换。

`pcileech_pcie_a7` 就是为了解决这两点而存在的**适配层 / 封装层**。它对外只暴露 4 个干净的 interface（与 fifo 相连）和几根物理引脚（PCIe 金手指、LED）；对内则把 IP 的几百根信号分门别类地接到 cfg、tlp 等子模块上，并完成位宽转换。这样顶层只需把 `pcie_a7` 当成一个「PCIe 子系统」黑盒使用即可。

#### 4.1.2 核心流程

`pcileech_pcie_a7` 内部的组织可以用下面这张「三明治」图概括：

```
        ┌──────────────────────────────────────────────┐
        │              pcileech_pcie_a7                 │
  pcie  │                                               │
  金手指│   ┌─────────────────────────────────────┐    │  4 个 interface
  差分  │   │            pcie_7x_0 (PCIe 硬核)      │    │  → dfifo_cfg
  信号 ─┼──▶│  m_axis_rx (64b) │ s_axis_tx (64b)   │    │  → dfifo_tlp
        │   │  cfg_* / pl_* / DRP / user_*         │    │  → dfifo_pcie
        │   └────────┬──────────────────┬──────────┘    │  → dshadow2fifo
        │            │ tlp_rx(64b)      │ tlp_tx(64b)   │
        │   src64: 64→128 │            │ dst64: 128→64 │
        │            ▼                  ▼               │
        │   ┌─────────────────────────────────────┐    │
        │   │  pcileech_pcie_tlp_a7 (TLP 处理)     │◀── tlps_static
        │   └─────────────────────────────────────┘    │
        │   ┌─────────────────────────────────────┐    │
        │   │  pcileech_pcie_cfg_a7 (配置管理)     │    │
        │   └─────────────────────────────────────┘    │
        └──────────────────────────────────────────────┘
```

数据流分两条对称的链路：

- **接收（RX）**：IP 的 `m_axis_rx`（64 位）→ `tlp_rx` 接口 → `pcileech_tlps128_src64`（64→128）→ `tlps_rx`（128 位 AXIS）→ `pcileech_pcie_tlp_a7` 内部处理 → 最终经 `dfifo_tlp` 送回 fifo。
- **发送（TX）**：`pcileech_pcie_tlp_a7` 产出 `tlps_tx`（128 位）→ `pcileech_tlps128_dst64`（128→64）→ `tlp_tx` 接口 → IP 的 `s_axis_tx`（64 位）→ 经 GTP 发到金手指。

配置通路（`cfg`）则单独走 `pcileech_pcie_cfg_a7`，经 `dfifo_cfg` 与 fifo 交互，并通过 `ctx`（`IfPCIeSignals`）与 IP 的配置/状态端口直连。

#### 4.1.3 源码精读

模块声明对外只暴露 4 组信号：系统时钟与复位、PCIe 物理引脚、状态 LED、以及 4 个 interface。这正是「封装」的体现——复杂度都被藏进去了。

[pcileech_pcie_a7.sv:13-34](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L13-L34) —— 模块端口。注意四个 interface 都使用 `.mp_pcie` 或 `.shadow` 这种 modport，与 fifo 端的 modport 方向互补（u2-l1 已讲）：

```verilog
module pcileech_pcie_a7(
    input                   clk_sys,
    input                   rst,
    // PCIe fabric
    output  [0:0]           pcie_tx_p,
    ...
    input                   pcie_perst_n,
    output                  led_state,
    // PCIe <--> FIFOs
    IfPCIeFifoCfg.mp_pcie   dfifo_cfg,
    IfPCIeFifoTlp.mp_pcie   dfifo_tlp,
    IfPCIeFifoCore.mp_pcie  dfifo_pcie,
    IfShadow2Fifo.shadow    dshadow2fifo
    );
```

模块内部首先声明一组内部 interface，用来把 IP 与各子模块粘起来：

[pcileech_pcie_a7.sv:40-48](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L40-L48) —— 内部信号声明。`ctx`（配置/状态总线）、`tlp_tx/tlp_rx`（64 位收发）、`tlps_tx/tlps_rx`（128 位收发）、`tlps_static`（cfg 发给 tlp 的静态包）都在这里：

```verilog
IfPCIeSignals           ctx();
IfPCIeTlpRxTx           tlp_tx();
IfPCIeTlpRxTx           tlp_rx();
IfAXIS128               tlps_tx();
IfAXIS128               tlps_rx();
IfAXIS128               tlps_static();       // static tlp transmit from cfg->tlp
wire [15:0]             pcie_id;
wire                    user_lnk_up;
```

#### 4.1.4 代码实践

**实践目标**：直观感受「封装」的收益。

**操作步骤**：

1. 打开 `pcileech_pcie_a7.sv`，数一下 `pcie_7x_0` 这个 IP 例化（见 [L117-L260](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L117-L260)）一共连了多少根端口信号。
2. 再看模块对外端口（L13–L34）只有多少根。
3. 打开顶层 `pcileech_squirrel_top.sv`，看它例化 `pcileech_pcie_a7` 时只需连多少根线。

**需要观察的现象**：IP 自身有上百个端口，但经过封装后，模块对外只有约 10 根物理引脚 + 4 个 interface；顶层例化语句会非常简洁。

**预期结果**：你会明显感觉到「如果没有这一层封装，顶层会被 PCIe 的几百根细线淹没」——这就是适配层存在的意义。

#### 4.1.5 小练习与答案

**练习 1**：`pcileech_pcie_a7` 对外的 4 个 interface 中，哪一个专门承载 DRP（动态重配置）与复位控制信号？

> **答案**：`dfifo_pcie`（类型 `IfPCIeFifoCore`）。它包含 `pcie_rst_core`、`pcie_rst_subsys`、以及 `drp_en/drp_we/drp_addr/drp_di/drp_rdy/drp_do`。详见 [pcileech_header.svh:244-265](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh#L244-L265)。

**练习 2**：模块内部声明了 `tlp_rx`（64 位）和 `tlps_rx`（128 位）两个接收接口，它们的命名区别（有没有 `s`）暗示了什么？

> **答案**：命名是「单数 = 64 位 PCIe 核原生流」「复数 `s` = 128 位工程内部 AXIS 流」。`tlp_rx` 直连 IP 的 `m_axis_rx`；`tlps_rx` 是经位宽转换后的产物，名字里的 `s`（stream）对应 `IfAXIS128` 那套 128 位流。

---

### 4.2 复位层级 rst/rst_subsys/rst_pcie 与参考时钟

#### 4.2.1 概念说明

PCIe 是一个对复位非常敏感的协议：复位粒度不同，链路行为完全不同。本模块设计了**两条独立的复位线**，对应「**软复位**」与「**硬复位**」两种粒度：

- **`rst_subsys`（子系统复位 / 软复位）**：只复位本模块里我们自己写的子模块（cfg、tlp、位宽转换等）——也就是 `pcileech_pcie_a7` 内部、IP **之外**的逻辑。**不会**让 PCIe 硬核 IP 重新开始链路训练。
- **`rst_pcie`（PCIe 核复位 / 硬复位）**：连 IP 的 `sys_rst_n` 都一起复位，会把整块 PCIe 硬核（含 GTP、链路训练状态机 LTSSM）推回初始状态，**链路会断开并重新训练**。代价大、应慎用。

「参考时钟」则是另一件物理层面的事：PCIe 插槽上的 100MHz 差分时钟 `pcie_clk_p/n` 是模拟差分信号，不能直接当普通时钟用，必须先经过一个专用硬件原语 `IBUFDS_GTE2`（Gigabit Transceiver 专用差分输入缓冲）转换成单端时钟 `pcie_clk_c`，才能喂给 GTP 收发器做参考。

#### 4.2.2 核心流程

两条复位线的逻辑组合如下（OR 关系，任一触发即复位）：

```
rst_subsys = rst              (顶层系统复位)
           OR rst_pcie_user   (IP 自己产生的 user_reset_out)
           OR dfifo_pcie.pcie_rst_subsys   (主机经寄存器触发的软复位)

rst_pcie   = rst              (顶层系统复位)
           OR ~pcie_perst_n   (PCIe 物理复位引脚 PERST#)
           OR dfifo_pcie.pcie_rst_core     (主机经寄存器触发的硬复位)
```

注意三个要点：

1. **顶层 `rst` 同时进入两条线**——上电时整个子系统与 IP 一起复位。
2. **`rst_pcie_user` 只进 `rst_subsys`**：IP 上电后内部会自发产生一个 `user_reset_out`（在 `user_clk_out` 稳定前为高），用来让 IP **之外**的子模块在用户时钟就绪前保持复位；但这不是一次新的链路训练，所以不进 `rst_pcie`。
3. **物理 `pcie_perst_n` 只进 `rst_pcie`**：主板拉低 PERST# 时必须复位整个 PCIe 硬核。

参考时钟通路：

```
pcie_clk_p / pcie_clk_n  (100MHz 差分，板卡金手指)
        │
        ▼
   IBUFDS_GTE2   (.O → pcie_clk_c, .ODIV2 不用)
        │
        ├──▶ 作为 GTP 参考时钟 (内部由 IP 接管)
        └──▶ 驱动 tickcount64_pcie_refclk 计数器与 LED
```

#### 4.2.3 源码精读

复位逻辑只用了两行 `wire` 赋值，但含义深刻：

[pcileech_pcie_a7.sv:53-55](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L53-L55) —— 两条复位线：

```verilog
wire rst_subsys = rst || rst_pcie_user || dfifo_pcie.pcie_rst_subsys;
wire rst_pcie   = rst || ~pcie_perst_n || dfifo_pcie.pcie_rst_core;
```

接下来看这两个复位分别喂给了谁。**`rst_subsys`** 喂给所有自研子模块与位宽转换模块的接收侧（`src64`）：

- cfg 子模块：`.rst(rst_subsys)` 见 [L74](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L74)
- tlp 子模块：`.rst(rst_subsys)` 见 [L95](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L95)
- 位宽转换 src64（接收）：`.rst(rst_subsys)` 见 [L88](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L88)

而 **`rst_pcie`** 只喂给 IP 的 `sys_rst_n`（注意取反）：

[pcileech_pcie_a7.sv:124](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L124) —— 硬复位喂给 IP（`sys_rst_n` 低有效，故取反 `~rst_pcie`）：

```verilog
.sys_rst_n                  ( ~rst_pcie                 ),  // <-
```

> 一个有意思的细节：发送侧位宽转换 `pcileech_tlps128_dst64` 用的是 `rst`（顶层复位），见 [L107](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L107)，而不是 `rst_subsys`。这是因为发送侧逻辑极简（仅一拍流水），用最顶层复位即可。

参考时钟的差分缓冲（物理原语，非自研模块）：

[pcileech_pcie_a7.sv:58](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L58) —— `IBUFDS_GTE2` 把差分参考时钟转成单端 `pcie_clk_c`，`ODIV2` 不使用：

```verilog
IBUFDS_GTE2 refclk_ibuf (.O(pcie_clk_c), .ODIV2(), .I(pcie_clk_p), .CEB(1'b0), .IB(pcie_clk_n));
```

`pcie_clk_c` 一方面在 IP 内部被 GTP 用作参考（IP 接管），另一方面驱动一个自由计数器，用来在没有链路时让 LED 慢闪：

[pcileech_pcie_a7.sv:64-67](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L64-L67) —— LED 诊断逻辑：

```verilog
time tickcount64_pcie_refclk = 0;
always @ ( posedge pcie_clk_c )
    tickcount64_pcie_refclk <= tickcount64_pcie_refclk + 1;
assign led_state = user_lnk_up || tickcount64_pcie_refclk[25];
```

含义：链路已通（`user_lnk_up`）时 LED 常亮；未通时由计数器第 25 位驱动慢闪（100MHz 下约 1.5Hz）。这就是板卡上 `led_pcie` 的「常亮 = OK，闪烁 = 链路未通」诊断行为（顶层把 `led_state` 经 OBUF 接到 LD1，见 u1-l4）。

#### 4.2.4 代码实践

**实践目标**：用一句话说清 `rst_pcie` 与 `rst_subsys` 的本质区别，并能在源码里指出它们各自连到哪里。

**操作步骤**：

1. 在 `pcileech_pcie_a7.sv` 中定位 [L54-L55](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L54-L55) 的两条赋值。
2. 全文搜索 `rst_subsys`，列出所有用到它的例化端口（应包括 cfg、tlp、src64）。
3. 全文搜索 `rst_pcie`，确认它只出现在 IP 的 `sys_rst_n` 处。
4. 在 [pcileech_header.svh:244-265](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_header.svh#L244-L265) 中确认 `pcie_rst_core` 与 `pcie_rst_subsys` 都来自 fifo（`mp_fifo` 端 output）。

**需要观察的现象**：`rst_subsys` 连到一堆子模块；`rst_pcie` 只连一处（IP 的 `sys_rst_n`）。

**预期结果**：你能得出结论——
- **`rst_pcie` 是「连根拔起」的硬复位**，会让 PCIe 链路断开重训，由顶层复位、物理 PERST#、或主机命令 `pcie_rst_core` 触发；
- **`rst_subsys` 是「只刷内部逻辑」的软复位**，不动 PCIe 硬核与已建立的链路，由顶层复位、IP 的 `user_reset_out`、或主机命令 `pcie_rst_subsys` 触发。

主机软件通常优先用 `pcie_rst_subsys` 重置 TLP 处理逻辑而不打断链路，只有需要彻底重来时才动 `pcie_rst_core`。

#### 4.2.5 小练习与答案

**练习 1**：为什么物理引脚 `pcie_perst_n` 只接到 `rst_pcie`，而不接 `rst_subsys`？

> **答案**：PERST# 是主板对设备的「全链路硬复位」要求，本意就是让 PCIe 硬核（含 GTP、LTSSM）回到初始、重新训练。如果只复位子系统而不复位硬核，链路状态机会处于不一致状态。所以它必须进入 `rst_pcie` 这条硬复位线。

**练习 2**：模块里有 `rst`、`rst_subsys`、`rst_pcie`、`rst_pcie_user` 四个复位名，哪个是「因」（输入源），哪个是「果」（派生）？

> **答案**：`rst`（顶层输入）与 `rst_pcie_user`（IP 输出）是源；`rst_subsys` 与 `rst_pcie` 是 L54–L55 由 OR 逻辑派生出来的两条「工程内部使用的复位线」。

---

### 4.3 pcie_7x_0 IP 的端口分组

#### 4.3.1 概念说明

`pcie_7x_0` 是 Xilinx 7 系列 PCIe 硬核 IP（VLNV `xilinx.com:ip:pcie_7x:3.3`），在 [pcie_7x_0.xci](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/ip/pcie_7x_0.xci) 中配置。它的端口虽然多达上百个，但可以归纳为 7 组，掌握分组就能快速读懂例化代码。本工程的默认身份是 `Vendor_ID=10EE`、`Device_ID=0666`、`LINK_CAP_MAX_LINK_WIDTH=1`（PCIe x1）。

#### 4.3.2 核心流程

7 组端口及其在本工程中的用途：

| 分组 | 代表端口 | 方向（相对 IP） | 作用 |
|------|----------|:---:|------|
| **PHY 物理层** | `pci_exp_txp/n`、`pci_exp_rxp/n`、`sys_clk`、`sys_rst_n` | 双向 | 差分串行收发引脚、100MHz 参考时钟、硬复位。 |
| **发送 s_axis_tx** | `s_axis_tx_tdata/tkeep/tlast/tready/tvalid/tuser` | 输入 | 把要发出去的 TLP（64 位）送进 IP。 |
| **接收 m_axis_rx** | `m_axis_rx_tdata/tkeep/tlast/tready/tvalid/tuser` | 输出 | IP 把收到的 TLP（64 位）吐出来。 |
| **配置管理 cfg_mgmt** | `cfg_mgmt_dwaddr/byte_en/di/do/rd_en/wr_en/rd_wr_done` | 双向 | 直接读写 PCIe 配置空间寄存器（如改 Device ID）。 |
| **配置控制/状态 cfg_control + cfg_status** | `cfg_dsn`、`cfg_bus/device/function_number`、`cfg_command`、`cfg_dcommand/dstatus/lcommand/lstatus`、`cfg_interrupt_*` | 双向 | 设备身份（DSN/BDF）、命令状态、中断。 |
| **物理层状态 pl_*** | `pl_phy_lnk_up`、`pl_ltssm_state`、`pl_sel_lnk_rate/width`、`pl_initial_link_width` | 输出 | 链路训练结果（LTSSM 状态、速率、宽度）。 |
| **DRP 动态重配置** | `pcie_drp_clk/en/we/addr/di/do/rdy` | 双向 | 运行时读写 IP 内部寄存器（调优用）。 |
| **用户接口** | `user_clk_out`、`user_reset_out`、`user_lnk_up`、`user_app_rdy` | 输出 | IP 为用户侧准备好的时钟、复位、链路就绪标志。 |

> **关键概念——用户接口**：`user_clk_out` 是 IP 链路训练成功后，根据协商速率内部产生的「用户时钟」（`clk_pcie`，本模块所有 PCIe 域逻辑都跑在这个时钟上）；`user_lnk_up` 表示链路真正建立；`user_reset_out` 在用户时钟稳定前保持复位。这三个信号是「IP → 用户逻辑」的握手边界，本模块正是用 `user_clk_out` 当 `clk_pcie`、用 `user_reset_out` 当 `rst_pcie_user`。

#### 4.3.3 源码精读

IP 例化从 [L117](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L117) 开始，到 [L260](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L260) 结束。按分组摘录关键片段：

**PHY 物理层 + 复位**（[L117-L124](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L117-L124)）：差分引脚直连顶层 `pcie_tx_p/n`、`pcie_rx_p/n`；参考时钟接 `pcie_clk_c`（来自 IBUFDS_GTE2）；复位取反接 `rst_pcie`。

**接收 m_axis_rx**（[L134-L140](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L134-L140)）——这是 4.4 要追踪的起点：

```verilog
.m_axis_rx_tdata            ( tlp_rx.data               ),  // -> [63:0]
.m_axis_rx_tkeep            ( tlp_rx.keep               ),  // -> [7:0]
.m_axis_rx_tlast            ( tlp_rx.last               ),
.m_axis_rx_tready           ( tlp_rx.ready              ),  // <-
.m_axis_rx_tuser            ( tlp_rx.user               ),  // -> [21:0]
.m_axis_rx_tvalid           ( tlp_rx.valid              ),
```

> 注意 `m_axis_rx_tuser[21:0]` 是 Xilinx PCIe 核特有的 22 位边带信息，其中 `[8:2]` 是 7 个 BAR 命中指示（BAR0..BAR5 + EXPROM），后续 `src64` 会把它抽出来放进 128 位流的 `tuser[8:2]`。

**配置管理 cfg_mgmt**（[L142-L151](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L142-L151)）：全部经 `ctx` 接到 cfg 子模块。读写完成的握手信号是 `cfg_mgmt_rd_wr_done`。

**物理层状态 pl_***（[L224-L244](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L224-L244)）：链路状态信号（LTSSM、速率、宽度、`pl_phy_lnk_up`）也都经 `ctx` 收集，cfg 子模块再把它们镜像到 fifo 的只读状态寄存器供主机查询（详见 u3-l2、u6-l3）。

**DRP**（[L246-L253](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L246-L253)）：DRP 端口直接接 `dfifo_pcie`（不经 cfg），由 fifo 的寄存器驱动，DRP 时钟用 `clk_sys`：

```verilog
.pcie_drp_clk               ( clk_sys                           ),
.pcie_drp_en                ( dfifo_pcie.drp_en                 ),
.pcie_drp_we                ( dfifo_pcie.drp_we                 ),
.pcie_drp_addr              ( dfifo_pcie.drp_addr               ),  // <- [8:0]
...
.pcie_drp_rdy               ( dfifo_pcie.drp_rdy                ),  // ->
.pcie_drp_do                ( dfifo_pcie.drp_do                 ),  // -> [15:0]
```

> 注释 L246 明确提醒：「write should only happen when core is in reset state」——DRP 写一般要在核复位态下做，以免与链路活动冲突。详见 u5-l4。

**用户接口**（[L255-L259](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L255-L259)）——本模块最重要的「时钟与就绪」来源：

```verilog
.user_clk_out               ( clk_pcie                          ),  // ->
.user_reset_out             ( rst_pcie_user                     ),  // ->
.user_lnk_up                ( user_lnk_up                       ),  // ->
.user_app_rdy               (                                   )   // ->
```

`user_clk_out` 成为整个 PCIe 域的 `clk_pcie`；`user_lnk_up` 既驱动 LED，也回送给 fifo 状态。`user_app_rdy` 在本工程中未使用（悬空）。

#### 4.3.4 代码实践

**实践目标**：把 IP 的上百个端口「读薄」成 7 组。

**操作步骤**：

1. 打开 [pcileech_pcie_a7.sv 的 IP 例化段 L117-L260](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L117-L260)。
2. 用本讲 4.3.2 的表格做模板，给每个端口打上分组标签（PHY / TX / RX / cfg_mgmt / cfg_status / pl_* / DRP / 用户接口）。
3. 注意哪些端口接 `ctx`、哪些直接接 `dfifo_*`、哪些接物理顶层引脚。

**需要观察的现象**：大部分 `cfg_*`/`pl_*` 信号都集中接到 `ctx` 这一条内部总线上（`IfPCIeSignals`），只有 DRP 和 TLP 收发「绕过」cfg 直接连 fifo 或位宽转换模块。

**预期结果**：你会看到一条清晰的分工——`ctx` 总线是「IP 配置/状态面」的汇总，cfg 子模块是它的消费者；而 TLP 收发与 DRP 是相对独立的数据/控制面。这就是为什么本模块要同时例化 cfg 和 tlp 两个子模块。

#### 4.3.5 小练习与答案

**练习 1**：本模块所有「PCIe 域」逻辑（clk_pcie 触发的 always 块）都跑在哪个时钟上？它从哪来？

> **答案**：跑在 `clk_pcie` 上，它来自 IP 的 `user_clk_out`（L256）。也就是说 PCIe 域时钟不是外部直接给的，而是 IP 链路训练成功后内部 PLL 产生的。

**练习 2**：`m_axis_rx_tuser` 是 22 位，而 128 位 AXIS 流的 `tuser` 只有 9 位。被抽走的是哪一段、表示什么？

> **答案**：`src64` 把 `tlp_rx.user[8:2]`（即 `m_axis_rx_tuser[8:2]`）这 7 位 BAR 命中指示抽出来，放进 `tlps_out.tuser[8:2]`（见 4.4.3）。其余边带位在工程里不使用。

---

### 4.4 TLP 数据流与 64↔128 位宽转换

#### 4.4.1 概念说明

PCIe 硬核 IP 吐出的 TLP 是 **64 位** AXIS 流（`m_axis_rx_*`），但工程内部的 TLP 处理子系统（`pcileech_pcie_tlp_a7`）统一工作在 **128 位** AXIS 流（`IfAXIS128`）上——因为 128 位能在一个节拍里装下 4 个 DWORD，与 TLP 头/数据的对齐更整齐。于是需要两个位宽转换模块：

- **`pcileech_tlps128_src64`（接收方向）**：把 IP 的 64 位 `m_axis_rx` 流拼成 128 位 `tlps_rx` 流。这是本讲实践任务要追踪的 RX 通路。
- **`pcileech_tlps128_dst64`（发送方向）**：把 128 位 `tlps_tx` 流拆回 64 位喂给 IP 的 `s_axis_tx`。

这两个模块都定义在 `pcileech_pcie_a7.sv` 文件**末尾**（[L269-L350](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L269-L350)），是本文件除封装主体外的两块自研逻辑。

#### 4.4.2 核心流程

**接收（RX）通路**——本讲的追踪目标：

```
pcie_7x_0 .m_axis_rx_tdata (64b)
        │  连到内部 interface tlp_rx (IfPCIeTlpRxTx, 64位)
        ▼
pcileech_tlps128_src64  .tlp_rx.sink      （L90）
   状态机：累加 64 位 → 128 位，统计 DWORD 数 len
   产出：tvalid = tlast || (len>2)
        │  输出到内部 interface tlps_rx (IfAXIS128, 128位)
        ▼
pcileech_pcie_tlp_a7    .tlps_rx.sink_lite （L100）
```

`src64` 的拼接逻辑（用 DWORD 计数 `len` 衡量，一个 128 位拍最多 4 个 DWORD）：

\[ \text{next\_base} = (\text{tvalid}\,||\,\text{tlast}) \,?\, 0 : \text{len} \]

\[ \text{next\_len} = \text{next\_base} + 1 + \text{keep}[4] \]

\[ \text{tvalid} = \text{tlast} \,\lor\, (\text{len} > 2) \]

\[ \text{tkeepdw} = \{(\text{len}>3),\,(\text{len}>2),\,(\text{len}>1),\,1\} \]

直觉解释：每收到一个 64 位字（含 1 或 2 个 DWORD，由 `keep[4]` 区分），就往 `tdata` 里对应位置写；当累计 ≥3 个 DWORD（`len>2`）或到达包尾（`tlast`）时，就产出一个有效的 128 位拍。`tkeepdw` 4 位分别表示 4 个 DWORD 槽位哪些有效。

**发送（TX）通路**（对称地拆回去）：

```
pcileech_pcie_tlp_a7   .tlps_tx.source     （L99，128位）
        ▼
pcileech_tlps128_dst64 .tlps_in.sink       （L110）
   用 d1_tvalid 暂存 128 位拍的后半段
        │  输出到 tlp_tx (IfPCIeTlpRxTx, 64位)
        ▼
pcie_7x_0 .s_axis_tx_tdata (64b)
```

`dst64` 的核心难点：一个 128 位拍可能含「1 个 64 位字」或「2 个 64 位字」（由 `tkeepdw[2]` 判断）。如果是 2 个，需要先用第一个字、再用一拍延迟寄存器 `d1_*` 吐出第二个字——这就是 `d1_tvalid` 的作用。

#### 4.4.3 源码精读

**RX 通路的例化接线**（[L87-L92](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L87-L92)）——注意三处连线正好串成一条链：

```verilog
pcileech_tlps128_src64 i_pcileech_tlps128_src64(
    .rst                        ( rst_subsys                ),
    .clk_pcie                   ( clk_pcie                  ),
    .tlp_rx                     ( tlp_rx.sink               ),   // 来自 IP 的 64 位流
    .tlps_out                   ( tlps_rx.source_lite       )    // 去往 tlp 子模块的 128 位流
);
```

其中 `tlp_rx` 这条 interface 在 [L135-L140](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L135-L140) 被 IP 的 `m_axis_rx_*` 驱动，所以「IP → tlp_rx → src64 → tlps_rx → tlp 子模块」是一条连续管线。

**`pcileech_tlps128_src64` 模块本体**（[L303-L350](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L303-L350)）核心几行：

```verilog
wire        tvalid      = tlast || (len>2);
assign tlps_out.tdata   = tdata;
assign tlps_out.tkeepdw = {(len>3), (len>2), (len>1), 1'b1};
...
assign tlps_out.tuser[8:2]  = bar_hit;     // BAR 命中：从 tlp_rx.user[8:2] 抽出
...
wire [3:0]  next_len    = next_base + 1 + tlp_rx.keep[4];   // keep[4]=1 表示本拍含 2 个 DWORD
always @ ( posedge clk_pcie )
    ...
    else if ( tlp_rx.valid ) begin
        tdata[(32*next_base)+:64] <= tlp_rx.data;   // 按 DWORD 偏移写入对应位置
        ...
        bar_hit <= tlp_rx.user[8:2];
    end
```

要点：
- `tlp_rx.keep[4]`（来自 `m_axis_rx_tkeep[4]`）为 1 时表示这个 64 位字两个 DWORD 都有效，`next_len` 因此 +2 而非 +1。
- `tdata[(32*next_base)++:64]` 用可变位偏移把 64 位数据写到 128 位寄存器的低半或高半。
- `bar_hit`（BAR 命中）从 22 位 `tuser` 里抽出 `[8:2]` 共 7 位，放进 128 位流的 `tuser[8:2]`（与 u2-l1 讲的 `IfAXIS128.tuser` 编码一致：`[0]first [1]last [8:2]BAR`）。

**`pcileech_tlps128_dst64` 模块本体**（[L269-L296](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L269-L296)）的延迟寄存器：

```verilog
bit [63:0]  d1_tdata;
bit         d1_tvalid = 0;
...
assign tlp_tx.data  = d1_tvalid ? d1_tdata : tlps_in.tdata[63:0];   // 先发低 64 位
...
always @ ( posedge clk_pcie ) begin
    d1_tvalid <= !rst && tlps_in.tvalid && tlps_in.tkeepdw[2];      // 本拍有第 2 个字才暂存
    d1_tdata  <= tlps_in.tdata[127:64];                             // 暂存高 64 位
    ...
end
```

当 128 位拍含 2 个 64 位字（`tkeepdw[2]=1`）时，第一拍发低 64 位、同时把高 64 位存进 `d1_tdata`，下一拍再发出去。

#### 4.4.4 代码实践（本讲主任务）

**实践目标**：亲自追踪一条完整的 RX 数据流，画出从物理引脚到 TLP 子模块的完整链路；并用一句话说清 `rst_pcie` 与 `rst_subsys` 的区别。

**操作步骤**：

1. **从物理引脚出发**：在 [L20-L23](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L20-L23) 找到 `pcie_rx_p/n`，它们接 IP 的 `pci_exp_rxp/rxn`（[L121-L122](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L121-L122)）。
2. **经 IP 输出**：IP 的 `m_axis_rx_tdata`（64 位）在 [L135](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L135) 赋给 `tlp_rx.data`。
3. **进位宽转换**：`tlp_rx` interface 在 [L90](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L90) 以 `.sink` 接进 `i_pcileech_tlps128_src64`。
4. **看转换内部**：在 [L329-L348](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L329-L348) 确认 64→128 拼接与 `bar_hit` 抽取。
5. **出转换、进 TLP 子模块**：转换结果经 `.tlps_out(tlps_rx.source_lite)`（[L91](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L91)）送到 `tlps_rx`，再在 [L100](https://github.com/ufrisk/pcileech-fpga/blob/c538c4170678c13f723dc921905fb81ff3c71d8e/PCIeSquirrel/src/pcileech_pcie_a7.sv#L100) 以 `.sink_lite` 进入 `i_pcileech_pcie_tlp_a7`。
6. **回答复位问题**：回顾 4.2，写出 `rst_pcie`（连根拔起的硬复位，复位 PCIe 硬核、链路重训）与 `rst_subsys`（仅刷内部 cfg/tlp/位宽转换逻辑、不动链路）的区别，并指出 `src64` 用的是哪一个（答：`rst_subsys`，L88）。

**需要观察的现象**：数据从物理层一路「无断点」地流到 TLP 子模块；中间 `src64` 一边拼装一边把 22 位 `tuser` 压缩成 9 位。

**预期结果**：你能画出下面这条完整链路并标注每段位宽：

```
pcie_rx_p/n (差分) → pcie_7x_0(GTP+核) → m_axis_rx(64b) → tlp_rx(64b)
   → pcileech_tlps128_src64(64→128) → tlps_rx(128b) → pcileech_pcie_tlp_a7
```

并能把复位区别讲清楚：**`rst_pcie` 复位 PCIe 硬核本身（含 GTP、LTSSM，链路会断重训，由 `rst`/`~pcie_perst_n`/`pcie_rst_core` 触发）；`rst_subsys` 只复位 IP 之外的自研子模块（cfg/tlp/位宽转换，不动链路，由 `rst`/`rst_pcie_user`/`pcie_rst_subsys` 触发）**。RX 位宽转换 `src64` 接的是 `rst_subsys`。

#### 4.4.5 小练习与答案

**练习 1**：在 `pcileech_tlps128_src64` 里，什么条件下一个 128 位拍会被判定为「有效」并送出（`tvalid=1`）？

> **答案**：`tvalid = tlast || (len>2)`。即：到达包尾（`tlast`），或已累计超过 2 个 DWORD（`len` 为 3 或 4）。这样能保证 128 位拍要么凑够 3~4 个 DWORD，要么是包的末尾。

**练习 2**：发送方向的 `pcileech_tlps128_dst64` 为什么要用 `d1_tvalid/d1_tdata` 这组延迟寄存器？什么情况下会用到它？

> **答案**：因为一个 128 位拍可能含 2 个 64 位字（`tkeepdw[2]=1`），而 IP 的 `s_axis_tx` 一次只吃 64 位。遇到这种情况，需要第一拍先发低 64 位、把高 64 位暂存到 `d1_tdata`，下一拍（`d1_tvalid=1`）再发出去。若 128 位拍只含 1 个 64 位字，则 `d1_tvalid` 保持 0，直接发。

**练习 3**：`src64` 里 `bar_hit` 的数据来源和去向分别是什么？

> **答案**：来源是 `tlp_rx.user[8:2]`（即 IP `m_axis_rx_tuser[8:2]` 的 7 位 BAR 命中指示），去向是 `tlps_out.tuser[8:2]`（128 位 AXIS 流的 BAR 字段，与 `IfAXIS128` 约定一致）。

---

## 5. 综合实践

**综合任务**：为 `pcileech_pcie_a7` 画一张完整的「端口—内部—子系统」三栏对照图，并用它向一个没读过该文件的同学解释「一个从主板发来的 TLP 是如何被搬进工程内部的」。

建议步骤：

1. **左栏（对外）**：列出模块端口（4 个 interface + 物理引脚 + `clk_sys`/`rst`/`led_state`），标注每个 interface 的对端是谁（fifo 侧）。
2. **中栏（内部信号）**：列出 `ctx`、`tlp_rx`、`tlp_tx`、`tlps_rx`、`tlps_tx`、`tlps_static`、`clk_pcie`、`rst_subsys`、`rst_pcie`、`pcie_clk_c`、`user_lnk_up`。
3. **右栏（子模块与 IP）**：列出 `pcie_7x_0`、`pcileech_pcie_cfg_a7`、`pcileech_pcie_tlp_a7`、`pcileech_tlps128_src64`、`pcileech_tlps128_dst64`、`IBUFDS_GTE2`。
4. **画箭头**：用一个 TLP 接收包为例，从 `pcie_rx_p/n` 一路画到 `dfifo_tlp`，标注位宽（差分 → 64 → 128 → 32×4）和经过的模块。
5. **标注复位与时钟**：在图上用不同颜色标出 `clk_sys`、`clk_pcie`（来自 `user_clk_out`）、`pcie_clk_c`（来自 `IBUFDS_GTE2`）三个时钟，以及 `rst_subsys`、`rst_pcie` 两条复位覆盖的范围。

完成后，你应当能不查源码就回答：「`rst_pcie` 触发时哪些模块会被复位？链路会怎样？」「`user_clk_out` 坏了，哪些逻辑会停摆？」——如果能答上来，说明你已经把 PCIe 核心封装层吃透了。

---

## 6. 本讲小结

- `pcileech_pcie_a7` 是 PCIe 硬核 IP `pcie_7x_0` 与工程其余部分之间的**适配层 / 封装层**，对外只暴露 4 个 interface + 物理引脚，对内把 IP 上百个端口分发给 cfg、tlp 子模块并完成位宽转换。
- 复位分两条线：`rst_pcie`（硬复位，连根拔起 PCIe 硬核、链路重训，由 `rst`/`~pcie_perst_n`/`pcie_rst_core` 触发）；`rst_subsys`（软复位，仅刷 IP 之外的 cfg/tlp/位宽转换，不动链路，由 `rst`/`rst_pcie_user`/`pcie_rst_subsys` 触发）。
- 100MHz 差分参考时钟 `pcie_clk_p/n` 经 `IBUFDS_GTE2` 原语转成单端 `pcie_clk_c`，既给 GTP 做参考，又驱动 LED 慢闪计数器；链路通时 `user_lnk_up` 让 LED 常亮。
- `pcie_7x_0` 的端口可归纳为 7 组：PHY、s_axis_tx、m_axis_rx、cfg_mgmt、cfg 控制/状态、pl_* 物理层状态、DRP，外加 `user_clk_out`/`user_reset_out`/`user_lnk_up` 用户接口——其中 `user_clk_out` 就是整个 PCIe 域的 `clk_pcie`。
- 接收通路：IP `m_axis_rx`(64b) → `tlp_rx` → `pcileech_tlps128_src64`(64→128，统计 DWORD、抽 BAR 命中) → `tlps_rx`(128b) → `pcileech_pcie_tlp_a7`；发送通路对称地经 `pcileech_tlps128_dst64`(128→64，用 `d1_*` 暂存后半段) 回到 IP `s_axis_tx`。

---

## 7. 下一步学习建议

本讲只拆开了 `pcileech_pcie_a7` 这个「外壳」，外壳里两个最重要的子模块——`pcileech_pcie_cfg_a7`（配置管理）和 `pcileech_pcie_tlp_a7`（TLP 处理）——都被当成黑盒带过。接下来建议按顺序深入：

- **u3-l2 PCIe 配置空间管理**：打开 `pcileech_pcie_cfg_a7.sv`，看 `cfg_mgmt` 的读写时序如何跨 `clk_sys`↔`clk_pcie` 时钟域，以及 `ctx` 总线上的状态如何镜像到 fifo 的只读寄存器。这是理解「主机如何查询 LTSSM 状态、如何改设备 ID」的基础。
- **u3-l3 TLP 处理总览与 128 位流**：打开 `pcileech_pcie_tlp_a7.sv`，看本讲送进去的 `tlps_rx`(128b) 在内部被过滤、桥接、多路复用的全貌——本讲的位宽转换正是它的前置环节。
- 如果你对本讲的复位/时钟/约束等物理层细节更感兴趣，也可以先跳到 **u5-l1 跨时钟域设计** 和 **u5-l2 约束文件**，那里会讲 GTP 的 LOC 约束（如本板 `GTPE2_CHANNEL_X0Y2`）和 FT601/PCIe 的时序约束。

建议阅读源码时随时回到本讲的「三栏对照图」，把新学的子模块挂到对应位置，逐步把整张 PCIe 子系统图补全。
