# 接收通路 erx 流水线

## 1. 本讲目标

上一讲（u7-l2）我们顺着 **etx 发送通路**，把一个 104 位 emesh 包一路追到了 LVDS 引脚 `txo_data_p/n`。本讲我们站到链路的**对端**，沿着相反方向走一遍：对端 FPGA/ASIC 从 LVDS 引脚收到差分信号，如何一步步把它**还原**成一个 104 位 emesh 包，再分发到系统侧的 `rxwr/rxrd/rxrr` 三个通道。

学完后你应当能够：

- 说清 RX 从「8 对 LVDS 差分比特」到「104 位 emesh 包」的逐层还原过程；
- 理解 elink 采用的**源同步时钟恢复**思路（时钟与数据同行，而非从数据跳变里提取），并能算出 `erx_clocks` 里 PLL 各路输出频率；
- 看懂 `erx_io` 用一个 one-hot 游走指针 `rx_pointer` 把字节流重新「拼帧」成包；
- 理解 `erx_arbiter` 如何按写位与地址把包 demux 到三个通道，以及 `erx_fifo` 如何用 `oh_fifo_cdc` 把包从 RX 慢时钟域搬到 `sys_clk` 域；
- 能把 etx 的「串化」与 erx 的「解串/对齐」对照起来，讲清二者为何互为逆过程。

## 2. 前置知识

本讲默认你已经掌握以下内容（若生疏请先复习对应讲义）：

- **emesh 包格式**（u5-l1）：定长 104 位包（`PW = 2·AW + 40`，AW=32），低 8 位是控制字节（`write`/`datamode`/`ctrlmode`），其后是 `dstaddr/data/srcaddr`；包外伴随 `access`（≈valid）与 `wait`（高有效反压，`~wait≈ready`）。
- **elink IO 物理协议**（u7-l1）：源同步时钟 `LCLK` + `FRAME` + 8 位 DDR 数据线 + 读/写两路 `WAIT` 反压；一个 emesh 事务在线上被串行化成字节流 `B00–B09`，`FRAME` 上升沿标记事务起点；系统侧分 TX/RX 各 `wr/rd/rr` 三类事务共六个通道。
- **etx 发送通路**（u7-l2）：`etx_fifo`（跨域）→ `etx_arbiter`（三通道仲裁）→ `etx_protocol`（成帧，104 位包拆成两个 64 位并行字，`FRAME` 打出 0111 起点并做突发检测）→ `etx_io`（每 4 拍加载 64 位再每拍移 16 位，ODDR 双沿串化为 8 位 LVDS，时钟送 90° 相移版）→ `etx_clocks`（MMCM 产生多路时钟）。
- **FIFO 与跨时钟域**（u3-l2）：`oh_fifo_cdc` 把 `valid/ready`（这里叫 `access/wait`）握手包成组件，做指针格雷码化 + 两级同步的 CDC。

一条贯穿全讲的原则（沿用前面所有讲义）：**代码是事实，文档/注释可能滞后**。本讲涉及多个文件处于历史重构的过渡态（如 `erx_arbiter` 引用了仓库中无定义的 `packet2emesh`、`oh_fifo_cdc` 端口名存在漂移），凡遇此类问题一律以源码实际文本为准。

## 3. 本讲源码地图

本讲涉及的关键文件及其职责：

| 文件 | 作用 |
| --- | --- |
| [elink/hdl/erx.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx.v) | RX 顶层。声明 LVDS 引脚与六个系统侧通道，按 **时钟 → IO → core → fifo** 四段实例化子模块。 |
| [elink/hdl/erx_io.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_io.v) | 输入缓冲 + IDELAY + IDDR 解串 + **拆帧还原 104 位包**，并把包从快时钟同步到慢时钟；驱动 `WAIT` 反压输出。 |
| [elink/hdl/erx_clocks.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_clocks.v) | 用 PLL 把送来的 `LCLK` 整理成 `rx_lclk`（300 MHz）与 `rx_lclk_div4`（75 MHz），跑复位状态机，并用 `oh_rsync` 把复位同步进各时钟域。 |
| [elink/hdl/erx_protocol.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_protocol.v) | 突发（burst）地址自增 + 一级流水线。**注意：真正"拆帧"在 erx_io，这里只处理突发与位序微调。** |
| [elink/hdl/erx_arbiter.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_arbiter.v) | 接收分发器（demux）：按写位与目标地址把包分发到 `rxwr/rxrd/rxrr`，并合并 `ecfg/edma` 支路；产生各路 `wait`。 |
| [elink/hdl/erx_fifo.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_fifo.v) | 三个 `oh_fifo_cdc`，把三路包从 `rx_lclk_div4` 跨域搬到 `sys_clk`。 |
| [elink/hdl/erx_core.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_core.v) | erx 顶层里 `erx_io` 与 `erx_fifo` 之间的中间层，把 `erx_protocol/erx_remap/emmu/emailbox/erx_cfg/erx_arbiter` 打包在一起（spec 列出的 protocol/arbiter 实际住在这里）。 |

> **先纠正一个常见误会**：本讲的规格描述把流水线写成 `erx.v → erx_io → erx_clocks → erx_protocol → erx_fifo → erx_arbiter`，这是**数据流视角**的简化顺序。真实 RTL 里，`erx.v` 只实例化四个模块：`erx_clocks`、`erx_io`、`erx_core`、`erx_fifo`；而 `erx_protocol` 与 `erx_arbiter` 并不在 `erx.v` 里直接出现，它们被封装在 `erx_core` 内部（旁边还住着 `erx_remap/emmu/emailbox/erx_cfg`）。下面的 4.1～4.4 仍按「解串 → 时钟恢复 → 拆帧 → 分发」四个**概念模块**组织，但每一处都会落到真实文件与真实实例化层级上。

## 4. 核心概念与源码讲解

### 4.1 RX 解串：从 LVDS 差分对比特

#### 4.1.1 概念说明

发送端（etx）把 8 位并行数据用 ODDR 双沿打出去，每对 `p/n` 差分线在一个时钟周期里携带 2 个比特（上升沿一个、下降沿一个）。所以 8 对数据线一个 `LCLK` 周期能传 16 比特。

接收端要做的第一件事，就是这 16 比特的**逆变换**：

1. 用差分输入缓冲 `IBUFDS` 把 `p/n` 还原成单端信号；
2. 用可调延迟线 `IDELAY` 给每根线加一个可配置的抽头延迟，把数据沿**对齐**到采样时钟的眼图中央（这是源同步链路里补偿走线偏斜的关键）；
3. 用 `IDDR` 在时钟的上升/下降沿各采样一次，重新拼成 16 位的字 `rx_word[15:0]`。

这一层的目的是「把模拟级的差分摆幅，变成一组与时钟对齐的、干净的 16 位并行数据」。它必须跑在**最快的 IO 时钟** `rx_lclk`（300 MHz）上，逻辑越简单越好——这是 u7-l2 讲过的"快域尽量笨"原则在 RX 侧的延续。

#### 4.1.2 核心流程

```
rxi_data_p/n[7:0] ──IBUFDS──► rxi_data[7:0] ──┐
                                               ├─► IDELAY(每线可配延迟) ──► rxi_delay_out[8:0]
rxi_frame_p/n ──────IBUFDS──► rxi_frame ──────┘            (含 8 数据 + 1 frame)
                                                               │
                            BUFIO(rx_clkin) ──► rx_lclk_iddr ◄─┘ 采样时钟
                                                               │
                                          IDDR(双沿) ──► rx_word_iddr[15:0]
                                                               │  posedge rx_lclk 打一拍
                                                               ▼
                                                       rx_word[15:0]  （每周期 16 比特）
```

要点：

- `IDDR` 用 `SAME_EDGE_PIPELINED` 模式：把上升沿采样值放到 `Q1`、下降沿采样值放到 `Q2`，二者在同一拍对齐输出，拼成 16 位。
- 给 IDDR 用的采样时钟不是 PLL 清洗过的 `rx_lclk`，而是 `BUFIO(rx_clkin)`——即**输入 LCLK 经 IBUFDS 后再过一级 BUFIO** 的版本，目的是让采样时钟与数据走过几乎相同的路径、把偏斜降到最小。
- `IDELAY` 的抽头值 `idelay_value[44:0]`（9 根线 × 5 位 = 45 位）与加载脉冲 `load_taps` 都由 `erx_cfg`（在 erx_core 里）动态给出，软件可在运行时训练延迟。

#### 4.1.3 源码精读

`erx_io` 的输入缓冲与 IDDR 全部基于 Xilinx 原语，并按平台（Ultrascale / Zynq）`generate` 分支。先看差分输入缓冲（数据、帧、时钟各一个 `IBUFDS`）：

[elink/hdl/erx_io.v:238-263](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_io.v#L238-L263) —— `ibuf_data[7:0]`、`ibuf_frame` 把差分 `p/n` 还原成单端；`ibuf_lclk` 还源同步时钟，并输出 `rx_clkin`（它会被送回 `erx_clocks` 喂给 PLL，见 4.2）。`TARGET_E64` 分支把 `p/n` 对调，是 64 核板走线交错的硬件适配。

接着是采样时钟的缓冲：

[elink/hdl/erx_io.v:306-307](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_io.v#L306-L307) —— `BUFIO` 专给 IO 列的高扇出低偏斜时钟，`rx_lclk_iddr` 直接喂 IDDR。

IDELAY 把 9 根线（8 数据 + 1 帧）拼成一组分别延迟：

[elink/hdl/erx_io.v:313-398](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_io.v#L313-L398) —— `rxi_delay_in[8:0]={rxi_frame,rxi_data[7:0]}`，Ultrascale 走 `IDELAYE3`、Zynq 走 `IDELAYE2`，都配成 `VAR_LOAD`（由 `load_taps` 装载 `CNTVALUEIN`）。注释里多处写 `BROKEN!!!` 等字样，提示这部分的 Ultrascale 适配在仓库中处于未完工状态，阅读以参数与端口为准。

最后是 IDDR 解串本体：

[elink/hdl/erx_io.v:404-465](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_io.v#L404-L465) —— 8 个 `iddr_data` 各产出 `Q1→rx_word_iddr[i]`、`Q2→rx_word_iddr[i+8]`，拼出 16 位；`iddr_data`（Q2）+ `iddr_data`（Q1）正好是一拍内两个沿的 16 比特。帧信号同理走 `iddr_frame → rx_frame_iddr`。

打一拍对齐后得到本层最终产物 `rx_word[15:0]`：

[elink/hdl/erx_io.v:97-101](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_io.v#L97-L101) —— 把 IDDR 输出寄存一拍以改善时序，得到 `rx_word` 与 `rx_frame`，交给 4.3 的拆帧逻辑。

#### 4.1.4 代码实践（源码阅读型）

1. **目标**：在 `erx_io.v` 里把「8 对差分 → 16 位字」这条路径走一遍，标出每级延迟。
2. **步骤**：打开 `erx_io.v`，从 `rxi_data_p/n` 开始，依次定位 `IBUFDS → IDELAY → IDDR → rx_word_iddr → rx_word`；记下每级用到的时钟（`rx_lclk_iddr` 还是 `rx_lclk`）。
3. **观察**：注意 `rx_word_iddr[i+8]` 来自 `Q2`（下降沿），`rx_word_iddr[i]` 来自 `Q1`（上升沿）——也就是说 16 位字的高 8 位对应下降沿采样、低 8 位对应上升沿采样。
4. **预期结果**：你能用一句话说清"为什么 8 对线一个周期传 16 比特"。
5. **待本地验证**：Ultrascale 分支因 `IDELAYE3` 注释自述未完工，综合/仿真行为需在真实工具上确认。

#### 4.1.5 小练习与答案

**Q1**：为什么 IDDR 的采样时钟用 `BUFIO(rx_clkin)`，而不是 PLL 出来的 `rx_lclk`？
**答**：`rx_clkin` 是输入 LCLK 经 IBUFDS 后的最短路径副本，与数据经历的缓冲路径几乎一致，时钟-数据偏斜最小；PLL 输出虽然抖动更小，但引入了额外相移与延迟，反而会让采样偏离眼图中央。BUFIO 的作用是在 IO 列上低偏斜地分发这个"贴近数据"的时钟。

**Q2**：`rx_word_iddr` 是 16 位，但 8 个 `iddr_data` 实例每个只直连 `Q1/Q2` 两位，总位数对得上吗？
**答**：对得上。8 个实例 × 2（Q1+Q2）= 16 位：`Q1` 占 `rx_word_iddr[7:0]`，`Q2` 占 `rx_word_iddr[15:8]`（见实例 `Q2(rx_word_iddr[i+8])`）。帧那路只用 `Q1`，`Q2` 悬空。

---

### 4.2 CDR：源同步时钟恢复（erx_clocks）

#### 4.2.1 概念说明

很多高速串行链路（如 PCIe、SerDes）是**盲时钟**的：数据线上没有伴随时钟，接收端必须用时钟数据恢复电路（CDR）从数据的跳变沿里"猜"出时钟。那复杂且耗功耗。

elink 不走这条路。它是**源同步**（source-synchronous）链路：发送端（etx）在发数据的同时，**专门用一对差分线把时钟 `LCLK` 也发过来**（见 u7-l1）。于是接收端根本不需要从数据里恢复时钟——它只要把送来的 `LCLK` 缓冲、清洗一下就能用。这就是本讲的"CDR"：严格说是**源同步时钟的整理**，而非真正的时钟"提取"。

`erx_clocks` 做三件事：

1. 用 `rx_clkin`（erx_io 里 `IBUFDS(LCLK)` 的输出）喂一个 PLL，生成两路干净时钟：快 IO 时钟 `rx_lclk`（300 MHz）和慢逻辑时钟 `rx_lclk_div4`（75 MHz）；
2. 跑一个复位状态机：等 PLL 锁定、IDELAY 就绪后，才宣布 `rx_active`；
3. 用 `oh_rsync` 把复位分别同步进 `rx_lclk`（给 IO）与 `rx_lclk_div4`（给 core）两个域——这是 u2-l4 讲过的"异步生效、同步释放"。

#### 4.2.2 核心流程

PLL 的频率数学（参数见 [erx_clocks.v:9-14](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_clocks.v#L9-L14)，默认 `FREQ_RXCLK=300`、`FREQ_IDELAY=200`、`PLL_VCO_MULT=4`）：

\[
f_{in} = 300\,\text{MHz},\qquad f_{VCO} = \text{CLKFBOUT\_MULT}\cdot f_{in} = 4\cdot 300 = 1200\,\text{MHz}
\]

三路输出由 VCO 分频得到：

\[
f_{rx\_lclk} = \frac{f_{VCO}}{\text{CLKOUT4\_DIVIDE}} = \frac{1200}{4} = 300\,\text{MHz}\;(1{:}1)
\]

\[
f_{rx\_lclk\_div4} = \frac{f_{VCO}}{\text{CLKOUT5\_DIVIDE}} = \frac{1200}{16} = 75\,\text{MHz}
\]

\[
f_{idelay\_ref} = \frac{f_{VCO}}{\text{IREF\_DIVIDE}},\quad \text{IREF\_DIVIDE} = \frac{\text{PLL\_VCO\_MULT}\cdot f_{RXCLK}}{f_{IDELAY}} = \frac{4\cdot 300}{200} = 6 \Rightarrow f_{idelay\_ref} = 200\,\text{MHz}
\]

复位状态机：

```
        sys_nreset & tx_active = 0
                  │
                  ▼
           ┌─────────────┐
           │ RX_RESET_ALL│◄──────────── soft_reset=1 ───┐
           └─────┬───────┘                              │
   (~soft_reset) │                                      │
                 ▼                                      │
           ┌─────────────┐                              │
           │ RX_START_PLL│                              │
           └─────┬───────┘                              │
 (pll_locked &   │                                      │
  idelay_ready)  ▼                                      │
           ┌─────────────┐  soft_reset=1                │
           │  RX_ACTIVE  │──────────────────────────────┘
           └─────────────┘
```

注意 `rx_nreset_in = sys_nreset & tx_active`（[erx_clocks.v:78](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_clocks.v#L78)）：RX 必须等 TX 起来后才放开复位——这是全双工链路的启动顺序约束，避免 RX 在对端还没开始发时钟时去采悬空输入。

#### 4.2.3 源码精读

复位状态机是本模块的"指挥"：

[elink/hdl/erx_clocks.v:87-107](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_clocks.v#L87-L107) —— 三态 `RX_RESET_ALL → RX_START_PLL → RX_ACTIVE`，靠一个自由运行的 `reset_counter` 溢出产生的 `heartbeat` 节拍推进；只有在 PLL 锁定（`pll_locked_sync`，先用 `oh_dsync` 跨到 `sys_clk`）且 IDELAY 控制器就绪时才进 `RX_ACTIVE`。

`rx_active` 与复位派生：

[elink/hdl/erx_clocks.v:109-118](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_clocks.v#L109-L118) —— `pll_reset`/`idelay_reset` 只在 `RX_RESET_ALL` 拉高；`rx_nreset` 流水一拍改善时序；`rx_active` 直接由状态译码。

复位同步进两个域：

[elink/hdl/erx_clocks.v:123-135](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_clocks.v#L123-L135) —— `oh_rsync` 把 `rx_nreset` 分别同步到 `rx_lclk`（产 `erx_io_nreset`）与 `rx_lclk_div4`（产 `erx_nreset`），实现"异步生效、同步释放"。

PLL 本体（Xilinx `PLLE2_ADV`）：

[elink/hdl/erx_clocks.v:144-194](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_clocks.v#L144-L194) —— 输入 `CLKIN1=rx_clkin`，`CLKOUT4→rx_lclk_pll`、`CLKOUT5→rx_lclk_div4_pll`、`CLKOUT3→idelay_ref_clk_pll`，分频比对应上面公式（`CLKOUT4_DIVIDE=RXCLK_DIVIDE=4`、`CLKOUT5_DIVIDE=RXCLK_DIVIDE*4=16`，见 [L153-L155](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_clocks.v#L153-L155)）。

时钟网络与锁定信号同步：

[elink/hdl/erx_clocks.v:197-207](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_clocks.v#L197-L207) —— 三路 PLL 输出各经 `BUFG` 走全局时钟网络；`pll_locked` 经 `oh_dsync` 两级同步到 `sys_clk` 才送给状态机，避免亚稳态。

> 与 etx 对照（u7-l2）：etx 的 `etx_clocks` 用 MMCM 从**本地** `sys_clk` 倍频出 `tx_lclk_io(300M)/tx_lclk90/tx_lclk_div4(75M)`；erx 的 `erx_clocks` 则是从**对端送来的** `rx_clkin` 起锁相。一方"造"时钟，一方"收"时钟，这正是源同步全双工的两面。

#### 4.2.4 代码实践（源码阅读 + 手算）

1. **目标**：验证 `erx_clocks` 三路输出频率，并理解复位同步链。
2. **步骤**：读 [erx_clocks.v:9-26](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_clocks.v#L9-L26) 的参数与 `localparam`；按 4.2.2 的公式手算 VCO 与三路输出；再追 `rx_nreset → oh_rsync → erx_io_nreset/erx_nreset`。
3. **观察**：仿真时 `RCW=4`（`TARGET_SIM` 下复位计数器变窄，见 [L17-L21](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_clocks.v#L17-L21)），上电后状态机会很快从 `RX_RESET_ALL` 走到 `RX_ACTIVE`。
4. **预期结果**：`rx_lclk=300 MHz`、`rx_lclk_div4=75 MHz`、`idelay_ref=200 MHz`；`rx_active` 在 PLL 锁定后拉高。
5. **待本地验证**：PLL/BUFG 是 Xilinx 专属原语，纯 iverilog 仿真需配合 `xilibs` 里的行为模型，频率值需在带原语支持的环境里确认。

#### 4.2.5 小练习与答案

**Q1**：为什么 `erx_clocks` 要等 `tx_active` 才放开 RX 复位？
**答**：源同步链路里，RX 依赖对端持续发送 `LCLK`。TX 没启动时对端（或本端发往对端的握手）未就绪，`LCLK` 可能悬空或未稳定，此时让 RX 采样会采到垃圾。`tx_active` 是"链路已建立"的代理信号，确保 RX 在有时钟可收的前提下才放开。

**Q2**：`rx_lclk` 与 `rx_lclk_div4` 的频率比是多少？为什么 RX 要两个时钟？
**答**：4:1（300 MHz : 75 MHz）。快时钟 `rx_lclk` 只给 IDDR 等极简 IO 逻辑用（必须赶上 300 MHz 的双沿节拍）；慢时钟 `rx_lclk_div4` 给 `erx_core` 里的协议/MMU/仲裁等"重活"用——慢域里综合更容易、功耗更低。这是 u7-l2 "快域笨、慢域干活"原则的 RX 镜像。

---

### 4.3 协议拆帧：从字节流到 104 位 emesh 包

#### 4.3.1 概念说明

发送端的成帧（framing）在 etx 里被拆成两步：`etx_protocol` 把 104 位包组织成 64 位并行字 + `FRAME` 起点模式，`etx_io` 再串成比特。接收端做了**对称但合并**的处理：`erx_io` 一个模块同时完成「拆帧 + 重新拼成 104 位包」。也就是说，本讲规格里写的「`erx_protocol` 拆帧还原 emesh 包」其实主要发生在 `erx_io`——`erx_protocol` 只剩两个轻量职责：突发地址自增、一级流水线 + 位序微调。

拆帧的核心难点是**对齐**：解串得到的 `rx_word[15:0]` 是一串无始无终的 16 位字，接收端必须知道"哪一个字是一个事务的开头"。这靠 `FRAME` 信号——发送端在事务起点把 `FRAME` 拉出一个上升沿（u7-l1 讲过线上 `FRAME` 打出 0111 的模式）。`erx_io` 用一个 **one-hot 游走指针 `rx_pointer`** 跟踪当前在一个事务的第几个字上：检测到帧起点就对齐到 bit0，之后每个有效周期左移一位，走满 7 个字（7×16=112 ≥ 104 位包）就声明一个完整包。

#### 4.3.2 核心流程

```
rx_word[15:0] (每拍16位)        rx_frame / rx_frame_iddr
      │                                │
      ▼                                ▼
  ┌──────────────── rx_pointer 游走 ─────────────────┐
  │ 复位: 0000001                                     │
  │ bit6 且 frame 仍高 → 0001000 (预判突发, 跳到 bit3)│
  │ bit6 且 frame 低   → 0000001 (准备下一帧)         │
  │ 否则 frame 高      → 左移一位 (帧内前进)          │
  └──────────────────────────────────────────────────┘
      │  rx_pointer[i]=1 时把 rx_word 写进 rx_sample 的第 i 段
      ▼
  rx_sample[111:0]  (7 段 × 16 位)
      │  按 emesh 字段位序 reshuffle
      ▼
  rx_packet_lclk[103:0]  (write/datamode/ctrlmode/dstaddr/data/srcaddr)
      │  access/burst 也在此拍生成
      ▼  跨到慢时钟域 rx_lclk_div4
  rx_packet[103:0], rx_access, rx_burst  ──► 交给 erx_core
```

`rx_pointer` 的"bit6 且 frame 仍高 → 跳到 bit3"是突发（burst）优化：连续事务时后继包省掉了头部几个字，所以指针不从 bit0 重新开始而是从 bit3 接上。`erx_protocol` 随后用 `rx_burst` 标志做地址自增，二者配合还原突发地址序列。

#### 4.3.3 源码精读

`rx_pointer` 状态机（本模块的灵魂）：

[elink/hdl/erx_io.v:108-116](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_io.v#L108-L116) —— 复位置 `0000001`；`rx_pointer[6]`（走满 7 字）时按 `rx_frame_iddr` 决定跳到 `0001000`（突发续帧）或回 `0000001`（新帧）；帧内则左移。注意这是异步复位（`negedge erx_io_nreset`），跑在快时钟 `rx_lclk` 上。

把字攒成 112 位样本：

[elink/hdl/erx_io.v:119-135](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_io.v#L119-L135) —— `rx_pointer[i]` 为 1 时把 `rx_word[15:0]` 写入 `rx_sample` 的第 i 个 16 位段，从 `[15:0]` 一路到 `[111:96]`。

有效信号与突发检测：

[elink/hdl/erx_io.v:140-161](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_io.v#L140-L161) —— `access <= rx_pointer[6]`：指针走到最后一字时声明本拍是完整包；`burst_detect` 在"包完成且 frame 仍高"时置 1，锁存进 `burst`。

把 112 位样本按字段位序重排成 104 位包（关键reshuffle）：

[elink/hdl/erx_io.v:164-196](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_io.v#L164-L196) —— 注意包字段在 `rx_sample` 里的位置是**交错**的（线上的字节序不等于包字段序）：`access` 取 `sample[40]`、`write` 取 `sample[41]`、`datamode` 取 `sample[43:42]`、`ctrlmode` 取 `sample[15:12]`，`dstaddr/data/srcaddr` 各自从 `rx_sample` 的多个不相邻段拼接而成。这段拼接逻辑必须和 etx 成帧侧的字节排放严格对齐（互为逆映射）。

跨到慢时钟域：

[elink/hdl/erx_io.v:206-232](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_io.v#L206-L232) —— `access` 在快域只有 1 拍宽，但慢时钟 `rx_lclk_div4` 比 `rx_lclk` 慢 4 倍，直接采样可能漏。于是用移位寄存器 `valid[3:0]` 把 access 展宽到 4 拍（`access_wide=valid[3]`），再在慢沿采样 `rx_access/rx_packet/rx_burst`。

`erx_protocol` 的轻量后处理（在 erx_core 内）：

[elink/hdl/erx_protocol.v:44-64](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_protocol.v#L44-L64) —— 突发时 `dstaddr_reg` 每拍 `+4`（`dstaddr_next = dstaddr_reg + 4'b1000`，[L48](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_protocol.v#L48)），由 `rx_burst` 在"自增"与"用包内原地址"间选择（[L50-L51](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_protocol.v#L50-L51)）；末尾把包重打一拍，并刻意把 access 位清零（`{1'b0,rx_packet[7:1]}`，注释说"去掉冗余的 access 包位"以符合新格式，[L60-L63](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_protocol.v#L60-L63)）。

> 与 etx 对照：etx 的成帧分在 `etx_protocol`（组 64 位字 + FRAME 模式 + 突发检测）和 `etx_io`（串化）两处；erx 把"解串 + 拆帧 + 重排"全部压进 `erx_io`，`erx_protocol` 只保留突发地址自增。这是 RX/XT 在结构上的一个有意不对称——接收端要在最快的时钟域里完成"对齐"，所以把对齐相关的活集中在一个快域模块里，尽量减少跨域。

#### 4.3.4 代码实践（源码阅读 + 推理）

1. **目标**：验证 `rx_pointer` 的 7 步游走与 112→104 的字段重排。
2. **步骤**：读 [erx_io.v:108-135](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_io.v#L108-L135)，在纸上画一个事务到达时 `rx_pointer` 从 `0000001` 逐拍左移到 `1000000` 的过程；再对照 [L168-L195](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_io.v#L168-L195)，把 `rx_sample[111:0]` 的 7 段标注成哪些位属于 `dstaddr`、哪些属于 `data`、哪些属于 `srcaddr`。
3. **观察**：`dstaddr` 在 `rx_sample` 里散布在 `[11:8]`、`[23:16]`、`[31:24]`、`[39:32]`、`[47:44]` 五段——并非连续，这是线上字节序的体现。
4. **预期结果**：你能解释"为什么 `access` 取 `rx_sample[40]` 而不是第 0 位"——因为线上的 B00 字节里，控制位并不在最低位。
5. **待本地验证**：完整字段映射需与 etx 成帧侧（u7-l2 的 `etx_protocol`）逐位核对，建议两边对着看。

#### 4.3.5 小练习与答案

**Q1**：`rx_pointer` 复位值是 `0000001`（bit0=1）。为什么用 one-hot 游走，而不是一个普通二进制计数器？
**答**：one-hot 让"当前处于第 i 字"可以直接用单根 `rx_pointer[i]` 做 `rx_sample` 段写使能与 `access` 译码（`rx_pointer[6]` 直接当"包完成"），无需译码器，在 300 MHz 快域里时序最优。代价是位宽稍宽（7 位表 7 个状态）。

**Q2**：`erx_protocol` 既然不拆帧，它存在的意义是什么？
**答**：两件 etx 侧也有、RX 必须对称处理的活——突发地址自增（让连续包的 `dstaddr` 自动 +4，省得每包都重发完整地址）和一级流水线打拍（时序隔离 + 把 access 冗余位清零以符合新包格式）。它名字虽叫 protocol，但实质是"成帧后处理"，真正的拆帧在 `erx_io`。

**Q3**：`access` 在快域是 1 拍，`erx_io` 如何保证慢域 `rx_lclk_div4` 一定能采到？
**答**：用移位寄存器把 access 展宽成 4 拍（`valid[3:0]<=4'b1111` 后逐拍右移补 0，`access_wide=valid[3]`），慢时钟频率恰好是快时钟的 1/4，所以展宽 4 拍必被慢沿命中至少一次。

---

### 4.4 分发：erx_arbiter 的 demux 与 erx_fifo 的跨域

#### 4.4.1 概念说明

拆好的 104 位包（`erx_packet` + `erx_access`）现在在 `rx_lclk_div4` 时钟域。它最终要去 `sys_clk` 域的三个通道：

- **rxwr**（master write）：远端发来的写事务；
- **rxrd**（master read request）：远端发来的读请求；
- **rxrr**（slave read response）：本端寄存器读回的响应（要回送给远端）。

两件事要做：① **demux（分发）**——按"是不是写、地址是不是指向本端寄存器"把包分到三条路；② **CDC（跨时钟域）**——把每条路从 `rx_lclk_div4` 搬到 `sys_clk`。分别由 `erx_arbiter`（在 erx_core 内）和 `erx_fifo` 负责。

`erx_arbiter` 名字叫 arbiter（仲裁器），但在 RX 侧它其实是 **distributor/demux**（源码注释写的是 "ELINK RECEIVE DISTRIBUTION (DEMUX)"）。分发依据是包的写位与地址区号：

- 写位=0（读请求）→ `rxrd`；
- 写位=1 且地址不是本端 ID → `rxwr`（要转交系统总线的远端写）；
- 地址命中本端 ID 的读响应组 → `rxrr`（本端寄存器回读响应）；
- 此外还把 `ecfg`（elink 自身配置寄存器读回）并进 `rxrr`、把 `edma` 读请求并进 `rxrd`。

反压（`wait`）则反向汇聚：把下游各 FIFO 的 `wait`、mailbox 的 `wait` 等或起来回送给 IO，形成逐级反压链。

#### 4.4.2 核心流程

```
                  erx_access/erx_packet (来自 erx_protocol, rx_lclk_div4 域)
                          │
                  ┌───────┴────────┐
                  │  erx_arbiter   │  按 write / dstaddr 区号 demux
                  │  (在 erx_core) │  并合并 ecfg/edma；汇聚各路 wait
                  └───┬────┬────┬──┘
                      │    │    │
        rxwr_*_fifo_* │    │    │ rxrr_*_fifo_*     （仍 rx_lclk_div4 域）
                      │    │    │
                     rxwr rxrd rxrr_*_fifo_*
                      │    │    │
              ┌───────┴────┴────┴───────┐
              │      erx_fifo           │  三个 oh_fifo_cdc
              │  rx_lclk_div4 → sys_clk │
              └───┬──────┬──────┬───────┘
                  │      │      │
                rxwr    rxrd   rxrr   （sys_clk 域，送往系统侧/AXI）
```

#### 4.4.3 源码精读

`erx_arbiter` 先用 `packet2emesh` 把包拆出 `write` 与 `dstaddr` 字段：

[elink/hdl/erx_arbiter.v:54-67](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_arbiter.v#L54-L67) —— `packet2emesh` 实例 `p2e` 从 `erx_packet` 译出 `erx_write`、`erx_dstaddr`。（⚠️ `packet2emesh` 在仓库中无定义，见 u6-l3/u6-l4/u7-l2 的相同遗留问题，本文件不能原样编译，以源码文本为准。）

读响应路 `rxrr`（命中本端 ID 的读响应 + ecfg 读回）：

[elink/hdl/erx_arbiter.v:73-79](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_arbiter.v#L73-L79) —— `rxrr_access = ecfg_access | (erx_access & dstaddr[31:20]==ID & dstaddr[19:16]==EGROUP_RR)`；包源在 `ecfg_access` 与 `erx_packet` 间二选一。

写路 `rxwr`（写且不指向本端）：

[elink/hdl/erx_arbiter.v:85-89](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_arbiter.v#L85-L89) —— `rxwr_access = erx_access & erx_write & ~(dstaddr[31:20]==ID)`。

读请求路 `rxrd`（读，并合并 edma）：

[elink/hdl/erx_arbiter.v:95-100](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_arbiter.v#L95-L100) —— `erx_read = erx_access & ~erx_write`；`rxrd_access = erx_read | edma_access`。

反压汇聚（把下游 wait 回送给 IO 与各源）：

[elink/hdl/erx_arbiter.v:105-114](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_arbiter.v#L105-L114) —— `rx_wr_wait = ecfg_access | mailbox_wait | rxwr_wait | rxrr_wait`，把写路径上所有可能卡住的环节或起来回送 IO；`rx_rd_wait = rxrd_wait`；`edma_wait/ecfg_wait` 分别回送给 edma/ecfg 源。

三个 `oh_fifo_cdc`（在 erx_fifo 里）做跨域：

[elink/hdl/erx_fifo.v:80-128](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_fifo.v#L80-L128) —— `rxrd_fifo`/`rxwr_fifo`/`rxrr_fifo` 各是一个 `oh_fifo_cdc #(.DW(104), .DEPTH(32))`，`clk_in=rx_lclk_div4`、`clk_out=sys_clk`、复位 `erx_nreset`，端口是 `access_in/wait_in/packet_in → access_out/wait_out/packet_out` 的标准 valid/ready 握手（u3-l2）。模板注释（[L63-L76](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_fifo.v#L63-L76)）显示了 verilog-mode 自动派生 `clk_out=sys_clk`、`clk_in=rx_lclk_div4` 的连接约定。

> 与 etx 对照（u7-l2）：etx 那边是 `etx_fifo`（sys_clk→tx_lclk_div4）→ `etx_arbiter`（**三合一路**，从 txwr/txrd/txrr 仲裁选出一路发）；erx 这边方向相反，是 `erx_arbiter`（**一分三路** demux）→ `erx_fifo`（rx_lclk_div4→sys_clk）。etx_arbiter 是 N→1 优先级仲裁，erx_arbiter 是 1→N 地址分发，命名相同但职责对偶。

#### 4.4.4 代码实践（源码阅读型）

1. **目标**：在 `erx_core.v` 里看清 `erx_arbiter` 的输入输出与三路 FIFO 的衔接。
2. **步骤**：读 [erx_core.v:249-272](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_core.v#L249-L272)（erx_arbiter 实例）与 [erx.v:181-206](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx.v#L181-L206)（erx_fifo 实例），追 `emmu_access → erx_arbiter → rxwr/rxrd/rxrr_fifo_* → erx_fifo → rxwr/rxrd/rxrr_*`。
3. **观察**：`erx_core` 把 `erx_arbiter` 的 `erx_access` 输入接的是 `emmu_access`（即"经过 MMU 翻译后"的包），而不是 `erx_protocol` 直接的输出——说明 demux 发生在地址重映射与 MMU 之后。
4. **预期结果**：你能画出"一个写包从 `rx_packet` 到 `rxwr_packet(sys_clk 域)`"经过 erx_io→erx_protocol→erx_remap→emmu→erx_arbiter→erx_fifo 的完整站点。
5. **待本地验证**：`oh_fifo_cdc` 端口名在仓库中存在漂移（u7-l2），跨域行为需带正确版本库替换后仿真确认。

#### 4.4.5 小练习与答案

**Q1**：`erx_arbiter` 在 RX 叫 arbiter，但它和 u3-l4 / etx 里的 `oh_arbiter`/`etx_arbiter` 是同一种东西吗？
**答**：不是。`oh_arbiter`/`etx_arbiter` 是 N→1 的优先级仲裁（多请求抢一个资源）；`erx_arbiter` 是 1→N 的 demux（一个输入按地址分发到多路）。源码注释自己写的是 "RECEIVE DISTRIBUTOR (DEMUX)"。同名是历史命名，理解时要看职责而非名字。

**Q2**：为什么 `rx_wr_wait` 要把 `mailbox_wait | rxwr_wait | rxrr_wait` 都或进来？
**答**：一个写包到达后，可能因 mailbox 满、rxwr FIFO 满、或 rxrr FIFO 满（响应路堵了）而任一处卡住。任一环节反压，整个写路径都得向远端回 wait，否则包会丢。把下游所有 wait 源或起来回送 IO，就是构造这条逐级反压链。

**Q3**：三个 FIFO 都用 `DEPTH=32`、`DW=104`，跨同一个时钟域对（`rx_lclk_div4 → sys_clk`）。为什么 RX 末端要三个独立 FIFO 而不是一个？
**答**：因为写、读请求、读响应三类事务语义不同、去往不同目的地（系统侧 write 通道 / read 请求通道 / read 响应通道），且彼此速率与反压相互独立。分 FIFO 让它们互不阻塞（head-of-line blocking 隔离），这与 etx 发送端用三个独立 FIFO（txwr/txrd/txrr）是完全对称的设计。

---

## 5. 综合实践

> 这是本讲的核心代码实践任务（对应规格里的 practice_task）。请在阅读完 4.1～4.4 后完成。

**任务**：对比 [elink/hdl/etx_io.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/etx_io.v)（发送）与 [elink/hdl/erx_io.v](https://github.com/aolofsson/oh/blob/7edfcb5f0506fb854449fbe09598a7ec88bb2067/elink/hdl/erx_io.v)（接收），说明发送的"串化（serialize）"与接收的"解串/对齐（deserialize/align）"是如何互为逆过程的，并指出两边的**结构性不对称**。

### 步骤

1. **填表**（先把两边关键要素对齐）：

   | 维度 | etx_io（发送） | erx_io（接收） |
   | --- | --- | --- |
   | 数据走向 | 64 位并行 → 16 位 → 8 对 LVDS | 8 对 LVDS → 16 位 → 112 位样本 → 104 位包 |
   | 并串变换原语 | `ODDR`（双沿，`D1/D2`） | `IDDR`（双沿，`Q1/Q2`） |
   | 时钟对齐手段 | `oh_edgealign` 找快/慢沿 + 时钟送 90° 相移（眼图中央） | `IDELAY` 给每线加可调延迟 + `BUFIO` 低偏斜采样时钟 |
   | 帧处理 | `tx_frame[3:0]` 移位打出 0111 起点 | `rx_pointer` one-hot 游走，按 `FRAME` 对齐并攒包 |
   | 字段重排 | `etx_protocol` 组 64 位字（成帧） | **erx_io 内部**把 112 位样本 reshuffle 成 104 位包（拆帧） |
   | WAIT 信号 | 输入：`IBUFDS` 采对端 wait → negedge 同步 + 两级展宽 | 输出：`OBUFT`/`OBUFDS` 驱动 wait 给对端 |

2. **找出三处逆过程**，各写一句话：
   - **并串 vs 串并**：etx 用 64 位寄存器每 4 拍重载、每拍右移 16 位送 ODDR；erx 用 IDDR 每拍收 16 位、按 `rx_pointer` 写入 `rx_sample` 的对应 16 位段。前者"分"，后者"聚"。
   - **时钟相位 vs 延迟对齐**：etx 把采样用的时钟（发送给对端的就是数据采样沿）移相 90° 落在数据眼中央；erx 则在本地用 IDELAY 给数据线加抽头延迟，把数据沿挪到本地采样时钟的眼中央。两者都是为了"在最佳采样点对齐"，但发送端挪时钟、接收端挪数据。
   - **成帧 vs 拆帧**：etx 用 `FRAME` 打出 0111 模式标记起点；erx 用同一根 `FRAME` 的电平驱动 `rx_pointer` 对齐到一个事务的第 0 字。

3. **指出不对称**（这是本题的加分点）：etx 把"成帧（组字 + FRAME 模式 + 突发检测）"放在 `etx_protocol`、"串化"放在 `etx_io`，两模块分工；erx 却把"解串 + 拆帧 + 字段重排"全部压进 `erx_io`，`erx_protocol` 只剩突发地址自增。请结合 4.3.3 解释**为什么**接收端要把对齐逻辑集中在快域一个模块里（提示：对齐必须发生在采样之后、跨域之前，且要在最快的时钟域里完成以追上数据速率）。

### 需要观察的现象与预期结果

- 你应当能用一张框图标出 etx_io 与 erx_io 中"数据宽度"在每个节点的变化（64↔16↔8/112↔104），并验证两者宽度变化是镜像的。
- 你应当能解释：为什么 etx_io 在快域只做"加载-移位-ODDR"这种极简动作，而 erx_io 在快域却要多做"`rx_pointer` 对齐 + `rx_sample` 攒包 + 字段 reshuffle"——因为发送端的"对齐"由接收端负责（发送只管按节拍发），接收端必须自己找回对齐。
- **待本地验证**：完整的字段位映射（尤其 112→104 的 reshuffle）需要与 etx 成帧侧逐位核对；建议在带 iverilog + `xilibs` 仿真模型的环境里跑 `elink/dv` 下的回环测试，观察一个写包从 `txwr_packet` 出发、经 LVDS、在 `rxwr_packet` 还原的全过程波形。

## 6. 本讲小结

- **erx 顶层四段实例化**：`erx.v` 实际实例化 `erx_clocks → erx_io → erx_core → erx_fifo`；规格里列的 `erx_protocol`/`erx_arbiter` 住在 `erx_core` 内部（旁边还有 `erx_remap/emmu/emailbox/erx_cfg`）。
- **解串（4.1）**：`IBUFDS → IDELAY → IDDR` 把 8 对 LVDS 还原成每拍 16 位的 `rx_word`，采样时钟用 `BUFIO(rx_clkin)` 而非 PLL 输出，以最小化时钟-数据偏斜。
- **时钟恢复（4.2）**：elink 是**源同步**链路，"CDR"实为用 PLL（`PLLE2_ADV`）把送来的 `LCLK` 整理成 `rx_lclk`(300 M) 与 `rx_lclk_div4`(75 M)；复位状态机等 PLL 锁定 + `tx_active` 后才宣布 `rx_active`，并用 `oh_rsync` 把复位同步进两个域。
- **拆帧（4.3）**：真正把字节流还原成 104 位包的是 `erx_io`——靠 one-hot 游走指针 `rx_pointer`（复位 `0000001`，走 7 字完成一包，突发时跳到 bit3）对齐 `FRAME`，把 7×16 位 `rx_sample` 按字段位序 reshuffle 成 `rx_packet`，再展宽 access 跨到慢域；`erx_protocol` 只做突发地址自增与流水线打拍。
- **分发（4.4）**：`erx_arbiter`（实为 demux）按写位与地址区号把包分到 `rxwr/rxrd/rxrr` 三路，并汇聚下游 wait 形成反压链；`erx_fifo` 用三个 `oh_fifo_cdc` 把三路包从 `rx_lclk_div4` 搬到 `sys_clk`，与 etx 的三 FIFO 设计对称对偶。
- **工程现实**：`erx_arbiter` 依赖仓库中无定义的 `packet2emesh`、`oh_fifo_cdc` 端口名存在漂移、Ultrascale 的 IDELAY 分支注释自述未完工——RX 通路与 TX 一样不能脱离完整库替换直接编译，阅读一律以源码实际文本为准。

## 7. 下一步学习建议

- **接下来读 u7-l4（elink 配置子系统）**：本讲反复出现的 `idelay_value/load_taps`（4.1）、`test_mode`、MMU/remap/mailbox 的使能位都来自 `erx_cfg`。读完配置讲义，你就能把"软件如何训练接收延迟、如何开关 MMU/mailbox"与本讲的硬件通路串起来。
- **横向对比 mio（u8-l4）**：mio 是 elink 的轻量替代，TX/RX 也对称分层。带着本讲的"解串/CDR/拆帧/demux"四件套去读 `mrx.v/mtx.v`，会更轻易看清哪些是 elink 专属、哪些是源同步链路的通用范式。
- **回看 u7-l2**：建议把本讲与 u7-l2 对照重读一遍，画出完整的 etx→线→erx 全双工框图，标注每一段的数据宽度与时钟域，这是检验你是否真正理解 elink 数据通路的最好方式。
- **继续阅读的源码**：`elink/hdl/erx_core.v`（看清 protocol/remap/mmu/mailbox/cfg/arbiter 的拼接）、`elink/hdl/erx_cfg.v`（配置寄存器如何驱动本讲的 idelay/test_mode）、以及 `elink/dv/tests/` 下的 `.emf` 回环测试激励。
