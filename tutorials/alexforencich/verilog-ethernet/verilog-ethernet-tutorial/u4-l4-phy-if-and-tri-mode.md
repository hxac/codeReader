# PHY 接口、时钟与三模 GMII MAC

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 `gmii_phy_if` / `mii_phy_if` 这两个 PHY 接口模块做了什么：它们是 MAC 与外部 PHY 芯片之间的「源同步 IO 收发 + 时钟缓冲 + 复位同步」桥梁。
- 解释源同步（source-synchronous）IO 的含义，以及为什么 RX 侧的时钟要经过 `BUFG`/`BUFR`/`BUFIO`/`BUFIO2` 这类时钟缓冲原语。
- 看懂 `eth_mac_1g_gmii` 如何只靠一个本地 125 MHz 参考时钟 `gtx_clk`，**测量对端 RX 时钟的频率**，自动判定链路是 10 / 100 / 1000 Mbps，并据此生成 `mii_select` 与 `speed` 信号。
- 理解 `TARGET`、`CLOCK_INPUT_STYLE`、`IODDR_STYLE` 三个参数如何让同一份 RTL 在仿真、Xilinx、Altera 以及不同代际器件上综合出正确的 IO 原语。

## 2. 前置知识

在进入本讲前，请确认你已经掌握（这些都在前置讲义里讲过）：

- **GMII/MII 物理信号**（u4-l1）：GMII 是 8 位数据 + `tx_en`/`rx_dv`/`tx_er`/`rx_er` 控制位的并行接口；MII 是它的 4 位「半字节」退化版。一根 GMII 在 125 MHz 下跑 1000 Mbps。
- **clk_enable / mii_select 双控线**（u4-l1）：`axis_gmii_rx`/`axis_gmii_tx` 在单一时钟下，用 `clk_enable`（分频跳周期）和 `mii_select`（切 4 位半字节）覆盖 10/100/1000 Mbps 三档速率。
- **eth_mac_1g 是布线层**（u4-l3）：它例化 `axis_gmii_rx`/`axis_gmii_tx`，并把 `rx_clk_enable`/`tx_clk_enable`/`rx_mii_select`/`tx_mii_select` 暴露成端口。

本讲要回答的关键问题是：**那两根控制线 `clk_enable`、`mii_select`，以及 MAC 用的 `rx_clk`/`tx_clk`，到底从哪里来？** 答案就在 PHY 接口模块和三模 MAC 顶层里。

补充两个本讲要用到的硬件术语：

- **源同步（source synchronous）**：发送方把数据和自己用的时钟**一起**送过来，接收方用这个「伴随时钟」去采数据。GMII/MII/RGMII/XGMII 都是源同步接口——PHY 芯片给出 `rx_clk` 的同时也给出 `rxd`，MAC 用同一个 `rx_clk` 去寄存 `rxd`，这样时钟和数据经历相似的布线延迟，setup/hold 关系容易满足。
- **时钟缓冲原语**：FPGA 里从管脚进来的时钟不能直接拿来用，必须经过专用时钟资源（`BUFG` 全局、`BUFR` 区域、`BUFIO` IO 区域等），才能低抖动地驱动一批触发器。这些是厂商原语，仿真里不存在，故需要参数化区分。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [rtl/gmii_phy_if.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/gmii_phy_if.v) | GMII/MII PHY 接口：RX 源同步输入、TX 源同步输出、TX 时钟在 `gtx_clk` 与 `phy_mii_tx_clk` 间切换、各时钟域复位同步 |
| [rtl/mii_phy_if.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/mii_phy_if.v) | 纯 MII（4 位）PHY 接口，结构类似 `gmii_phy_if` 但 TX 时钟恒由 PHY 提供 |
| [rtl/ssio_sdr_in.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ssio_sdr_in.v) | 通用源同步 SDR 输入：缓冲输入时钟，并用它在 IOB（IO Block）寄存数据 |
| [rtl/ssio_sdr_out.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ssio_sdr_out.v) | 通用源同步 SDR 输出：在 IOB 寄存输出数据，并用 ODDR 转发一个时钟 |
| [rtl/oddr.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/oddr.v) | 跨厂商统一的 DDR 输出触发器封装（Xilinx ODDR/ODDR2、Altera altddio_out） |
| [rtl/eth_mac_1g_gmii.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g_gmii.v) | 三模 MAC 顶层：例化 `gmii_phy_if` + `eth_mac_1g`，并内置 PHY 速率自动检测 |

数据流向全景（自下而上）：

```
PHY 芯片管脚
   │  gmii_rx_clk / gmii_rxd / gmii_rx_dv / gmii_rx_er          (源同步输入)
   ▼
gmii_phy_if ── ssio_sdr_in ── 时钟缓冲 + IOB 寄存 ──▶ mac_gmii_rxd 等
   │                                                        + mac_gmii_rx_clk (= rx_clk)
   ▼
eth_mac_1g ── axis_gmii_rx ──▶ rx_axis_*  (AXI-Stream)
```

发送方向相反：`eth_mac_1g` 的 `axis_gmii_tx` 在 `tx_clk` 下产出 `mac_gmii_txd` 等，`gmii_phy_if` 的 `ssio_sdr_out` 把它们寄存到 IOB 并转发时钟给 PHY。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **PHY 接口与源同步时钟**——`gmii_phy_if`/`mii_phy_if` 与底层 `ssio_sdr_in`/`ssio_sdr_out` 如何搬运数据与时钟。
2. **厂商适配参数**——`TARGET`/`CLOCK_INPUT_STYLE`/`IODDR_STYLE` 如何在一份 RTL 里切换不同器件的 IO 原语。
3. **三模速率自动检测**——`eth_mac_1g_gmii` 如何测量 RX 时钟频率，生成 `mii_select`/`speed`。

### 4.1 PHY 接口与源同步时钟

#### 4.1.1 概念说明

`gmii_phy_if` 夹在 MAC 内部逻辑与外部 PHY 芯片之间。它**不改任何数据内容**，只做三件事：

1. **RX 侧**：把 PHY 送来的源同步信号（`phy_gmii_rx_clk` + `phy_gmii_rxd/dv/er`）经过时钟缓冲后，用同一个时钟把数据**寄存到 IOB（IO Block 里的触发器）**，输出干净的内部 GMII 信号和 `mac_gmii_rx_clk`。
2. **TX 侧**：把 MAC 在 `mac_gmii_tx_clk` 下产出的 `mac_gmii_txd/en/er` 寄存到 IOB，并**向 PHY 转发一个伴随时钟** `phy_gmii_tx_clk`。
3. **TX 时钟选择**：1000 Mbps 时用本地 125 MHz `gtx_clk` 作 TX 时钟；10/100 Mbps 时改用 PHY 回送的 `phy_mii_tx_clk`（2.5 MHz 或 25 MHz）。

`mii_phy_if` 是它的「纯 MII」孪生兄弟：数据 4 位，且 TX 时钟恒为 PHY 提供的 `phy_mii_tx_clk`（MII 模式下 PHY 是时钟源），因此没有 `clk` 输入也没有 `mii_select`。

为什么要把数据寄存到 IOB？因为源同步接口对 setup/hold 极其敏感。把接收触发器放到紧挨管脚的 IOB 里，可使数据从管脚到触发器的延迟最小且可预测；同理发送触发器放进 IOB，可使各比特到管脚的延迟一致（skew 小）。注释里的 `(* IOB = "TRUE" *)` 就是把这个意图告诉综合工具。

#### 4.1.2 核心流程

RX 侧（源同步输入）数据与时钟路径：

```
phy_gmii_rx_clk ─▶ ssio_sdr_in ─┬─ 时钟缓冲(BUFG/BUFR/...) ─▶ clk_io  ─▶ 寄存 input_d
                                └─ 时钟缓冲                 ─▶ output_clk ─▶ mac_gmii_rx_clk (= rx_clk)
phy_gmii_rxd/dv/er ─────────────▶ IOB 触发器(posedge clk_io) ─▶ mac_gmii_rxd/dv/er
```

注意 `clk_io`（驱动 IOB 触发器）和 `output_clk`（驱动 MAC 内部逻辑）可以由**不同的时钟缓冲**产生——例如 7 系列常用 `BUFIO`（IO 区域、抖动小）打 IOB、`BUFR`（区域逻辑）打内部逻辑，二者来自同一源、相位接近。

TX 侧（源同步输出）：

```
mac_gmii_txd/en/er ─▶ ssio_sdr_out ─ IOB 触发器(posedge mac_gmii_tx_clk) ─▶ phy_gmii_txd/en/er
mac_gmii_tx_clk ─▶ ODDR(d1=0,d2=1) ─▶ phy_gmii_tx_clk   (时钟转发，保证与数据同源同相位)
```

`ssio_sdr_out` 用一个 `oddr`（`d1=0, d2=1`）把内部时钟「复制」一份到输出管脚，这是 Xilinx 推荐的时钟转发手法：ODDR 在上升沿放 `d2=1`、下降沿放 `d1=0`，输出端得到一个与 `clk` 同频同相的方波，且和 IOB 里的数据触发器共享同一个时钟，从而数据和伴随时钟的 skew 极小。

#### 4.1.3 源码精读

先看 `gmii_phy_if` 的端口与两处例化。RX 侧把 8 位数据 + 2 位控制（`rx_dv`、`rx_er`）拼成 10 位一起送进 `ssio_sdr_in`，TX 侧同理拼 10 位送进 `ssio_sdr_out`：

[rtl/gmii_phy_if.v:85-96](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/gmii_phy_if.v#L85-L96) —— RX 例化 `ssio_sdr_in`，宽度 10，把 PHY 的 `rx_clk` 缓冲成 `mac_gmii_rx_clk`，并把 `{rxd, dv, er}` 一起寄存。

[rtl/gmii_phy_if.v:98-109](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/gmii_phy_if.v#L98-L109) —— TX 例化 `ssio_sdr_out`，以 `mac_gmii_tx_clk` 为时钟寄存 `{txd, en, er}`，并向 PHY 转发 `phy_gmii_tx_clk`。

TX 时钟的选择是本模块最体现「三模」的地方：

[rtl/gmii_phy_if.v:111-125](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/gmii_phy_if.v#L111-L125) —— Xilinx 下用 `BUFGMUX` 在 `clk`（= `gtx_clk`，125 MHz）与 `phy_mii_tx_clk` 间二选一；非 Xilinx（仿真/通用）直接用 `assign` 三目。`mii_select=0`（1000M）选 `gtx_clk`，`mii_select=1`（10/100M）选 `phy_mii_tx_clk`。

复位同步是逐时钟域做的，每个域一个 4 位移位寄存器，复位值 `0xF`（即复位态），之后每拍从高位移入一个 0，3 拍后 `mac_*_rst` 才拉低，保证复位释放与对应时钟同步且持续若干拍：

[rtl/gmii_phy_if.v:127-148](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/gmii_phy_if.v#L127-L148) —— TX、RX 各一段独立的复位同步器，异步复位、同步释放。

再看 `ssio_sdr_in` 内部如何缓冲时钟（这是 RX 路径上最关键的一段）。`generate` 块按 `CLOCK_INPUT_STYLE` 选不同的 Xilinx 原语；非 Xilinx 分支则把时钟直接 `assign` 透传，仿真即走此路径：

[rtl/ssio_sdr_in.v:156-163](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ssio_sdr_in.v#L156-L163) —— 数据寄存：`(* IOB = "TRUE" *) reg` 在 `posedge clk_io` 采样 `input_d`，输出 `output_q`。注意它用 `clk_io`（IO 区域缓冲后的时钟）而非 `output_clk`，这正是把触发器锁进 IOB 的写法。

`mii_phy_if` 的 TX 侧没有用 `ssio_sdr_out`，而是直接用带 `IOB` 属性的寄存器，因为 MII 模式下时钟由 PHY 给（`phy_mii_tx_clk`），FPGA 不需要转发时钟，只需把数据寄存到 IOB：

[rtl/mii_phy_if.v:87-100](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/mii_phy_if.v#L87-L100) —— MII TX 输出寄存器，`posedge mac_mii_tx_clk` 采样，`mac_mii_tx_clk` 即 PHY 提供的 `phy_mii_tx_clk` 经 `BUFG` 后的版本（见下一节）。

#### 4.1.4 代码实践

> 实践目标：阅读 `gmii_phy_if`，画出 RX 侧从 PHY 管脚到内部 GMII 信号的时钟与数据路径，标注用到的时钟缓冲原语。

操作步骤：

1. 打开 [rtl/gmii_phy_if.v:85-96](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/gmii_phy_if.v#L85-L96)，确认 RX 把 `phy_gmii_rx_clk` 与 `{phy_gmii_rxd, phy_gmii_rx_dv, phy_gmii_rx_er}` 交给 `ssio_sdr_in`。
2. 打开 [rtl/ssio_sdr_in.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ssio_sdr_in.v)，画出 `input_clk → 时钟缓冲 → {clk_io, output_clk}` 与 `input_d → IOB 触发器 → output_q` 两条路径。
3. 在路径图上标注：`CLOCK_INPUT_STYLE="BUFR"` 时，`clk_io` 来自 `BUFIO`、`output_clk` 来自 `BUFR`（见 L78-L98）；`="BUFG"` 时二者都来自 `BUFG`（L65-L76）。

需要观察的现象（阅读型，无需运行）：

- `clk_io` 与 `output_clk` 由**同一根 `input_clk`** 经不同缓冲派生，因此同源、相位接近但驱动对象不同：前者只管 IOB 触发器，后者管整片 MAC 逻辑。
- 数据触发器 `output_q_reg` 用的是 `clk_io` 而非 `output_clk`，这是为了把它推进 IOB。
- 若把 `TARGET` 设成非 `"XILINX"`，所有原语都退化为 `assign` 透传（[rtl/ssio_sdr_in.v:143-152](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ssio_sdr_in.v#L143-L152)），这正是仿真能直接跑的原因。

预期结果：得到一张「PHY 管脚 → 输入时钟缓冲(BUFG/BUFR/BUFIO/BUFIO2) → clk_io 驱动 IOB 触发器 → 内部 GMII 信号 + output_clk 驱动 MAC」的路径图，并能指出 `output_clk` 就是后续 `eth_mac_1g` 的 `rx_clk`。

#### 4.1.5 小练习与答案

**练习 1**：`gmii_phy_if` 的 RX 把 `{phy_gmii_rxd[7:0], phy_gmii_rx_dv, phy_gmii_rx_er}` 拼成 10 位一起送进 `ssio_sdr_in`，为什么要把 `dv`/`er` 和数据一起寄存，而不是分开处理？

**答案**：源同步接口要求控制位和数据经历**相同的布线与触发器延迟**，彼此间才不会有相对 skew。把它们拼成一根总线、用同一个 `clk_io` 在同一组 IOB 触发器里同时采样，可保证 `dv`/`er` 与 `rxd` 严格对齐，MAC 侧采样才可靠。

**练习 2**：`mii_phy_if` 的 TX 侧为什么不像 `gmii_phy_if` 那样例化 `ssio_sdr_out`、也不需要转发时钟给 PHY？

**答案**：MII 协议规定 **TX 时钟由 PHY 提供**（`phy_mii_tx_clk`），FPGA 是接收方。所以 FPGA 只需在该时钟下把数据寄存到 IOB（[rtl/mii_phy_if.v:87-100](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/mii_phy_if.v#L87-L100)），不需要再用 ODDR 把时钟「送回去」。

### 4.2 厂商适配参数 TARGET / CLOCK_INPUT_STYLE / IODDR_STYLE

#### 4.2.1 概念说明

同一份 PHY 接口 RTL 既要能在 **Icarus 仿真**里跑通，又要能在 **Xilinx 7 系列 / Ultrascale / Spartan-6** 和 **Altera** 器件上综合出正确的 IO 原语。问题是：`BUFG`、`BUFIO`、`BUFR`、`BUFIO2`、`ODDR`、`altddio_out` 这些都是厂商专有原语，仿真器不认识，不同器件族也不一样。

作者的解法是用三个字符串参数在 `generate` 块里做编译期分支：

- **`TARGET`**：选厂商/场景。取值 `"SIM"`、`"GENERIC"`（默认，纯 `assign`，仿真和通用流程都能用）、`"XILINX"`、`"ALTERA"`。
- **`CLOCK_INPUT_STYLE`**：仅 RX 输入时钟生效，选 Xilinx 的时钟缓冲原语——`"BUFG"`（Ultrascale，全局）、`"BUFR"`（7 系列/Virtex-6，区域逻辑，常配 `BUFIO`）、`"BUFIO"`、`"BUFIO2"`（Spartan-6）。默认 `"BUFIO2"`。
- **`IODDR_STYLE`**：选 DDR 触发器原语——`"IODDR"`（Virtex-4/5/6、7 系列、Ultrascale）或 `"IODDR2"`（Spartan-6，默认）。

这套参数从顶层 `eth_mac_1g_gmii` 一路透传到 `gmii_phy_if`，再到 `ssio_sdr_in`/`ssio_sdr_out`/`oddr`，使用者只需在顶层设一次。

#### 4.2.2 核心流程

参数传递链：

```
eth_mac_1g_gmii(TARGET, IODDR_STYLE, CLOCK_INPUT_STYLE)
        │ 例化时透传
        ▼
gmii_phy_if(TARGET, IODDR_STYLE, CLOCK_INPUT_STYLE)
        │ 分别透传给 ssio_sdr_in / ssio_sdr_out
        ▼
ssio_sdr_in(TARGET, CLOCK_INPUT_STYLE) ── generate 选 BUFG/BUFR/BUFIO/BUFIO2/assign
ssio_sdr_out(TARGET, IODDR_STYLE)       ── 内部 oddr(TARGET, IODDR_STYLE)
```

`ssio_sdr_in` 里 `CLOCK_INPUT_STYLE` 的四种 Xilinx 分支各自构造一对「IO 缓冲 + 逻辑缓冲」，非 Xilinx 分支统一退化为 `assign`：

| `CLOCK_INPUT_STYLE` | `clk_io`（驱动 IOB） | `output_clk`（驱动 MAC） | 典型器件 |
| --- | --- | --- | --- |
| `"BUFG"` | `BUFG` | `BUFG`（同一颗） | Ultrascale |
| `"BUFR"` | `BUFIO` | `BUFR`（`BYPASS`） | 7 系列、Virtex-6 |
| `"BUFIO"` | `BUFIO` | `BUFG` | 需全局逻辑时钟时 |
| `"BUFIO2"` | `BUFIO2`(`IOCLK`) | `BUFG`（由 `DIVCLK`） | Spartan-6 |
| 非 XILINX | `assign input_clk` | `assign input_clk` | 仿真 / 通用 |

#### 4.2.3 源码精读

`ssio_sdr_in` 的 `generate` 块是本模块的核心，按 `CLOCK_INPUT_STYLE` 选时钟缓冲。这里看 `BUFR` 分支（7 系列最常用）：`BUFIO` 给 IOB、`BUFR` 给逻辑：

[rtl/ssio_sdr_in.v:78-98](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ssio_sdr_in.v#L78-L98) —— `"BUFR"` 分支：`BUFIO` 产 `clk_io`、`BUFR #(.BUFR_DIVIDE("BYPASS"))` 产 `output_clk`，二者同源。

非 Xilinx 分支（仿真走这里）把时钟直接透传，无任何原语：

[rtl/ssio_sdr_in.v:143-152](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ssio_sdr_in.v#L143-L152) —— `assign clk_io = input_clk; assign output_clk = input_clk;`。

`oddr` 把 `TARGET` 又细分成 Xilinx（`ODDR`/`ODDR2`，按 `IODDR_STYLE`）、Altera（`altddio_out`）和通用（行为级 `always`）。`ssio_sdr_out` 正是用它做时钟转发：

[rtl/ssio_sdr_out.v:54-64](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ssio_sdr_out.v#L54-L64) —— `oddr` 例化，`d1=0, d2=1`，把 `clk` 转成输出方波 `output_clk`。

`oddr` 头部的时序注释把 `d1`/`d2` 与输出 `q` 的关系画得很清楚：

[rtl/oddr.v:54-66](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/oddr.v#L54-L66) —— 时序图：上升沿输出 `d1`、下降沿输出 `d2`。`d1=0/d2=1` 时即得到与 `clk` 同相的方波。

`eth_mac_1g_gmii` 把这三个参数原样透传给 `gmii_phy_if`：

[rtl/eth_mac_1g_gmii.v:185-189](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g_gmii.v#L185-L189) —— 顶层把 `TARGET`/`IODDR_STYLE`/`CLOCK_INPUT_STYLE` 传给 `gmii_phy_if_inst`。

#### 4.2.4 代码实践

> 实践目标：通过修改参数，观察同一份 RTL 在「仿真」与「Xilinx 7 系列」下时钟路径的差异。

操作步骤：

1. 在仿真 testbench（或顶层例化）里把 `TARGET` 设为默认 `"GENERIC"`，确认 `ssio_sdr_in` 走 [rtl/ssio_sdr_in.v:143-152](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ssio_sdr_in.v#L143-L152) 的 `assign` 透传分支，仿真可编译通过。
2. （综合向，待本地验证）把 `TARGET="XILINX"`、`CLOCK_INPUT_STYLE="BUFR"`，在 7 系列工程里综合 `gmii_phy_if`，在综合报告里查找实例化的 `BUFIO` 与 `BUFR` 原语。

需要观察的现象：

- `TARGET="GENERIC"`：不存在任何 `BUFG`/`BUFIO` 等原语，`clk_io` 与 `output_clk` 完全相等。
- `TARGET="XILINX"` 且 `CLOCK_INPUT_STYLE="BUFR"`：应各出现 1 个 `BUFIO`（驱动 IOB）和 1 个 `BUFR`（驱动 MAC 逻辑）。

预期结果：能用一句话说出「`CLOCK_INPUT_STYLE` 决定的是 RX 时钟进 FPGA 后用哪颗时钟缓冲，而 `TARGET` 决定要不要用厂商原语」。第 2 步若无综合环境，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `TARGET` 的默认值是 `"GENERIC"` 而不是 `"XILINX"`？

**答案**：默认 `"GENERIC"` 让模块开箱即用于**仿真与通用流程**（不依赖任何厂商原语，`generate` 走 `assign` 透传分支）。上板时再按器件把 `TARGET` 改成 `"XILINX"`/`"ALTERA"`，体现「仿真优先、按需启用原语」的设计取向。

**练习 2**：`IODDR_STYLE` 的 `"IODDR"` 与 `"IODDR2"` 分别对应哪类 Xilinx 器件？它影响 `ssio_sdr_out` 的哪部分行为？

**答案**：`"IODDR"` 对应 Virtex-4/5/6、7 系列、Ultrascale（用 `ODDR` 原语），`"IODDR2"` 对应 Spartan-6（用 `ODDR2` 原语，默认值）。它只影响 `ssio_sdr_out` 内部用于**时钟转发**的那个 `oddr` 实例具体实例化成哪颗原语（见 [rtl/oddr.v:72-103](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/oddr.v#L72-L103)）。

### 4.3 三模速率自动检测与 mii_select / speed 生成

#### 4.3.1 概念说明

`eth_mac_1g_gmii` 是「三模」MAC：它要支持 10 / 100 / 1000 Mbps 三档速率。回顾 u4-l1，`axis_gmii_rx`/`axis_gmii_tx` 靠 `mii_select`（切 4 位半字节）和 `clk_enable`（分频）来适配速率。那么这两个信号谁来产生？

`eth_mac_1g_gmii` 的做法很巧妙：**它不去解析 PHY 的管理接口（MDIO），而是直接测量 PHY 送来的 RX 时钟频率**。因为不同速率下 RX 时钟频率本身就不同：

- 1000 Mbps：`gmii_rx_clk` = 125 MHz
- 100 Mbps：`gmii_rx_clk` = 25 MHz
- 10 Mbps：`gmii_rx_clk` = 2.5 MHz

只要量出 `rx_clk` 相对一个已知稳定参考时钟（`gtx_clk`，125 MHz）的频率比，就能反推速率。测出后：

- 生成 2 位 `speed`（`2'b10`=1000M，`2'b01`=100M，`2'b00`=10M）。
- 生成 `mii_select`：1000M 为 0（用 8 位 GMII），10/100M 为 1（切 4 位 MII）。
- 把 `mii_select` 跨时钟域同步到 `tx_clk` 和 `rx_clk`，分别送给 `axis_gmii_tx`/`axis_gmii_rx`。
- 把同一个 `mii_select` 也送给 `gmii_phy_if`，用于 TX 时钟在 `gtx_clk` 与 `phy_mii_tx_clk` 间切换（见 4.1）。

至于 `clk_enable`：注意 `eth_mac_1g_gmii` 把 `rx_clk_enable`/`tx_clk_enable` **恒接 1**（[rtl/eth_mac_1g_gmii.v:242-243](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g_gmii.v#L242-L243)）。也就是说，这里不用「125 MHz + 跳周期」的分频法，而是**真正切换时钟频率**：10/100M 时 `gmii_phy_if` 已把 TX/RX 时钟换成了 2.5/25 MHz 的 `phy_mii_tx_clk` 与对应的 `rx_clk`，于是 MAC 本身每个时钟周期都有效（`clk_enable=1`），只需 `mii_select` 切到 4 位即可。这是理解本模块的关键：**速率适配的一半（切 4 位）由 `mii_select` 完成，另一半（降频）由 `gmii_phy_if` 换时钟完成**。

#### 4.3.2 核心流程

速率检测的本质是「在 `gtx_clk` 域里数周期」。先把 `rx_clk` 预分频降低翻转速率，再同步到 `gtx_clk` 域，然后数「固定个数的 RX 预分频跳变」要花多少个 `gtx_clk` 周期：

```
rx_clk 域:  rx_prescale[2:0] 自由计数 → bit[2] 每 8 个 rx_clk 周期翻转一次（预分频 1/8）
                         │ 跨域同步（3 级移位寄存器到 gtx_clk 域）
                         ▼
gtx_clk 域: 检测 rx_prescale_sync 的跳变沿 → 每检到一个沿，rx_speed_count_2++
            同时 rx_speed_count_1 每个 gtx_clk 周期 +1（计参考周期数）

判定:
  若 rx_speed_count_1 先溢出(7 位满 ≈ 1016 ns) → 还没数够 3 个沿 → 10M
  若 rx_speed_count_2 先满(3 个沿)           → 看 rx_speed_count_1[6:5]:
                                                  非 0（计了 ≥32 个 gtx_clk）→ 100M
                                                  为 0（计了 <32 个 gtx_clk）→ 1000M
```

为什么这套判定能区分三档？设 \(T_{\text{rx}}\) 为 `rx_clk` 周期，\(T_g = 8\,\text{ns}\) 为 `gtx_clk` 周期。数 3 个预分频沿大约跨 \(8\,T_{\text{rx}}\)（两个完整翻转周期），对应的 `gtx_clk` 周期数约为：

\[ N \approx \frac{8\,T_{\text{rx}}}{T_g} \]

- 1000M：\(T_{\text{rx}}=8\,\text{ns}\) → \(N \approx 8\)（远小于 32 → `[6:5]=0`）。
- 100M：\(T_{\text{rx}}=40\,\text{ns}\) → \(N \approx 40\)（≥32 → `[6:5]≠0`）。
- 10M：\(T_{\text{rx}}=400\,\text{ns}\) → \(N \approx 400\)，但 `rx_speed_count_1` 只有 7 位，最多到 127（≈1016 ns）就溢出，根本数不到 3 个沿 → 走 10M 分支。

阈值 32（即 `count_1[5]`）正好卡在 8 与 40 之间，留有足够裕度，且因测的是**两时钟的频率比**而非绝对频率，对工艺/温漂不敏感。

#### 4.3.3 源码精读

第一步，`rx_clk` 域的自由预分频计数器：

[rtl/eth_mac_1g_gmii.v:128-133](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g_gmii.v#L128-L133) —— 3 位 `rx_prescale` 在 `rx_clk` 下持续自增，`bit[2]` 即 1/8 预分频指示。

把 `rx_prescale[2]` 跨域同步到 `gtx_clk` 域，用 3 级移位寄存器（取相邻两拍做沿检测）：

[rtl/eth_mac_1g_gmii.v:135-140](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g_gmii.v#L135-L140) —— `rx_prescale_sync <= {rx_prescale_sync[1:0], rx_prescale[2]}`。

核心判定逻辑（注意两个 `if` 的优先级与含义）：

[rtl/eth_mac_1g_gmii.v:142-181](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g_gmii.v#L142-L181) —— `rx_speed_count_1`（7 位，计 `gtx_clk` 周期数）与 `rx_speed_count_2`（2 位，计预分频沿数）。`&rx_speed_count_1` 满判 10M；`&rx_speed_count_2` 满后再用 `rx_speed_count_1[6:5]` 区分 100M/1000M。复位后默认 `speed_reg=2'b10`（1000M）、`mii_select_reg=0`。

测出的 `mii_select` 需要分别同步到 TX 和 RX 两个时钟域后再喂给 MAC（因为 MAC 的 `tx_mii_select`/`rx_mii_select` 分别工作在 `tx_clk`/`rx_clk`）：

[rtl/eth_mac_1g_gmii.v:114-126](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g_gmii.v#L114-L126) —— `tx_mii_select_sync`/`rx_mii_select_sync` 各 2 级，取 `[1]` 即两级同步后的稳态值送给 `eth_mac_1g`。

最后看 `eth_mac_1g` 的例化，确认 `mii_select` 与 `clk_enable` 的接法：

[rtl/eth_mac_1g_gmii.v:236-245](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g_gmii.v#L236-L245) —— `gmii_rxd/dv/er` 接 `gmii_phy_if` 输出的内部 GMII 信号；`rx_clk_enable`/`tx_clk_enable` 恒为 1；`rx_mii_select`/`tx_mii_select` 接同步后的 `mii_select`。这正印证了 4.3.1 的结论：降频靠换时钟，切位宽靠 `mii_select`。

`speed` 输出对外暴露，供上层（如 LED 指示、逻辑切换）使用：

[rtl/eth_mac_1g_gmii.v:183](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g_gmii.v#L183) —— `assign speed = speed_reg;`。

#### 4.3.4 代码实践

> 实践目标：通过追踪源码，验证「速率检测测量的是 `rx_clk` 与 `gtx_clk` 的频率比」，并理解阈值为何能区分三档。

操作步骤（源码阅读型）：

1. 在 [rtl/eth_mac_1g_gmii.v:128-181](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g_gmii.v#L128-L181) 中标注三个量：`rx_prescale`（rx_clk 域预分频）、`rx_prescale_sync`（gtx_clk 域同步）、`rx_speed_count_1/2`（gtx_clk 域计数器）。
2. 假设 `gtx_clk=125 MHz`，分别就 `rx_clk`=125/25/2.5 MHz 三种情形，套用 4.3.2 的公式估算 `rx_speed_count_1` 在 `rx_speed_count_2` 到 3 时的取值。

需要观察的现象 / 预期结果：

| rx_clk | 速率 | 3 个预分频沿约跨的 gtx_clk 数 | 落入分支 |
| --- | --- | --- | --- |
| 125 MHz | 1000M | ≈ 8 → `[6:5]=0` | 1000M，`mii_select=0` |
| 25 MHz | 100M | ≈ 40 → `[6:5]≠0` | 100M，`mii_select=1` |
| 2.5 MHz | 10M | ≈ 400 > 127 → `count_1` 先溢出 | 10M，`mii_select=1` |

若想动笔验证，可仿写一个最小 testbench：用不同频率的 `gmii_rx_clk` 驱动 `eth_mac_1g_gmii`，观察 `speed` 输出是否依次为 `2'b10`/`2'b01`/`2'b00`。该结果**待本地验证**（需配 cocotb 多时钟源）。

#### 4.3.5 小练习与答案

**练习 1**：`eth_mac_1g_gmii` 把 `rx_clk_enable`/`tx_clk_enable` 恒接 1，那 10/100 Mbps 的降速是怎么实现的？

**答案**：靠 `gmii_phy_if` **换时钟**。`mii_select=1` 时，`gmii_phy_if` 的 `BUFGMUX` 把 TX 时钟从 125 MHz 的 `gtx_clk` 切到 PHY 提供的 `phy_mii_tx_clk`（10M 时 2.5 MHz、100M 时 25 MHz），RX 侧 `rx_clk` 也随之是对应的低频时钟。于是 MAC 每拍都有效（`clk_enable=1`），只需 `mii_select=1` 把数据通路切到 4 位半字节即可（见 [rtl/eth_mac_1g_gmii.v:242-245](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g_gmii.v#L242-L245)）。

**练习 2**：速率检测为什么测 RX 时钟，却把同一个 `mii_select` 同时用于 TX？

**答案**：标准以太网链路 TX 与 RX 速率相同（自协商后两侧一致），故测出任一侧即可代表整条链路。测 RX 是因为 RX 时钟由 PHY 直接提供、最易获取；把结果也用于 TX 简化了设计，避免了再独立测 TX 时钟。

**练习 3**：复位后 `speed_reg` 与 `mii_select_reg` 的默认值是什么？为什么这样默认？

**答案**：复位后 `speed_reg=2'b10`（1000M）、`mii_select_reg=0`（[rtl/eth_mac_1g_gmii.v:149-150](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g_gmii.v#L149-L150)）。默认按最高的 1000M 配置，使 TX 时钟先指向 125 MHz 的 `gtx_clk`，与默认参考时钟一致，避免复位释放瞬间 TX 时钟域处于未知低频状态。

## 5. 综合实践

把三个最小模块串起来，完成一次「端到端理解」任务：

**任务**：对照 [rtl/eth_mac_1g_gmii.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g_gmii.v) 画出一张完整的「速率自适应数据通路图」，要求标注：

1. **外部管脚**：`gtx_clk`（125 MHz 参考）、`gmii_rx_clk/rxd/dv/er`、`mii_tx_clk`、`gmii_txd/tx_en/tx_er`。
2. **`gmii_phy_if` 内部**：`ssio_sdr_in`（RX，含时钟缓冲）、`ssio_sdr_out`（TX，含 ODDR 时钟转发）、`BUFGMUX`（TX 时钟二选一）、两个复位同步器。
3. **速率检测**：`rx_prescale → rx_prescale_sync → rx_speed_count_1/2 → speed_reg/mii_select_reg`。
4. **跨域与下发**：`mii_select_reg` 经 `tx_mii_select_sync`/`rx_mii_select_sync` 送 `eth_mac_1g`，同时回送 `gmii_phy_if` 的 `mii_select` 端口控制 `BUFGMUX`。
5. **`eth_mac_1g`**：`rx_clk_enable=tx_clk_enable=1`、`rx/tx_mii_select` 接同步值、`gmii_*` 接 `gmii_phy_if` 的内部 GMII 信号。

完成后，请用三句话回答：① 1000M 时 `tx_clk` 来自哪个源？② 100M 时 `tx_clk` 来自哪个源、`mii_select` 是多少？③ 10M 是靠什么判定的？这能检验你是否真正把「换时钟 + 切位宽 + 测频率」三件事对上了。

（参考答案：① `gtx_clk`（125 MHz）；② `phy_mii_tx_clk`（25 MHz），`mii_select=1`；③ `rx_speed_count_1` 在数够 3 个预分频沿之前就 7 位溢出。）

## 6. 本讲小结

- `gmii_phy_if` / `mii_phy_if` 是 MAC 与 PHY 之间的源同步 IO 桥梁，**不改数据**，只做时钟缓冲、IOB 寄存、TX 时钟选择与逐域复位同步。
- RX 数据用驱动 IOB 的 `clk_io` 采样；`clk_io` 与 `output_clk`（= MAC 的 `rx_clk`）由同一根 PHY 输入时钟经不同缓冲派生，`CLOCK_INPUT_STYLE` 决定具体原语。
- TX 侧用 `oddr`（`d1=0/d2=1`）把内部时钟转发给 PHY；GMII 模式下 TX 时钟由 `BUFGMUX` 在 `gtx_clk`（1000M）与 `phy_mii_tx_clk`（10/100M）间二选一，MII 模式下恒为 PHY 提供。
- `TARGET`/`IODDR_STYLE`/`CLOCK_INPUT_STYLE` 三个字符串参数用 `generate` 在编译期切换厂商原语，默认 `"GENERIC"` 保证仿真可跑。
- `eth_mac_1g_gmii` 通过测量 `rx_clk` 相对 `gtx_clk` 的频率比自动判定 10/100/1000M：`count_1` 先溢出→10M，否则 `count_1[6:5]` 为 0→1000M、非 0→100M。
- 速率适配一分为二：**降频靠 `gmii_phy_if` 换时钟**（故 `clk_enable` 恒 1），**切位宽靠 `mii_select`** 经跨域同步后送给 `axis_gmii_rx`/`axis_gmii_tx`。

## 7. 下一步学习建议

- **下一讲 u4-l5（RGMII MAC 与 DDR IO）**：RGMII 把 8 位 GMII 压缩到 4 位并在时钟双边沿传数据，会用到 `ssio_ddr_in`/`ssio_ddr_out` 与 `iddr`/`oddr`。本讲的 `ssio_sdr_*` 是它的 SDR 基础版，理解了本讲再去看 DDR 版会很顺。
- **若关注跨时钟域**：可先跳到 u5-l1（MAC FIFO 与 CDC），看 `eth_mac_1g_*_fifo` 如何用 `axis_async_fifo` 把这里的 PHY 时钟域（`rx_clk`/`tx_clk`）桥接到统一的 logic 时钟域。
- **建议继续阅读**：[rtl/ssio_sdr_in.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/ssio_sdr_in.v) 与 [rtl/oddr.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/oddr.v) 的完整 `generate`，把四种 `CLOCK_INPUT_STYLE` 分支都过一遍，体会「一份 RTL 多器件适配」的写法。
