# eth_mac_1g 核心千兆 MAC

## 1. 本讲目标

本讲把前两讲（[u4-l1](u4-l1-axis-gmii-rx-tx.md) 的 GMII 互转、[u4-l2](u4-l2-mac-flow-control.md) 的流量控制）学过的零件「装配」成一块真正可用的千兆以太网 MAC。读完本讲，你应当能够：

- 画出 `eth_mac_1g` 的 TX/RX 数据通路，说清楚 AXI-Stream 帧「从哪个端口进、经过哪些子模块、最后变成什么样的 GMII 信号出」。
- 理解 `ENABLE_PADDING`、`MIN_FRAME_LENGTH` 这两个帧格式参数如何决定短帧是否被补零、补到多长。
- 说清楚 `PTP_TS_ENABLE` 打开后，接收时间戳如何「搭车」进 `tuser`、发送时间戳如何通过旁带（sideband）总线连同 tag 一起回送。

本讲只做**顶层装配**的解读：GMII 收发器的逐字节状态机已在 u4-l1 深入讲过，PAUSE/PFC 的语义已在 u4-l2 讲过，这里不再重复它们的内部细节，而是聚焦于「它们如何被拼起来」。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**MAC 是「AXI 世界」与「物理线路」之间的翻译官。** 上层逻辑（你的应用、ARP/IP/UDP 协议栈）只会产生和消费结构化的字节流（AXI-Stream 帧），而网线另一端的 PHY 芯片只认 GMII 的 8 位物理信号。`eth_mac_1g` 就是夹在两者之间的一块硬件：TX 方向把 AXI 帧补上前导码、必要时补零填充、追加 4 字节 FCS，串成合法的线路帧；RX 方向把线路帧的前导码/FCS 剥掉，还原成干净的 AXI 帧，并顺带告诉你这帧的 FCS 对不对。

**「装配」意味着选配。** 一块完整的 MAC 不只是收发器，还可以包含 MAC 控制帧（PAUSE/PFC）的收发逻辑。但不是所有应用都需要流量控制，所以这部分被包在一个 `generate` 块里，由参数 `PAUSE_ENABLE`/`PFC_ENABLE` 决定是否综合——默认关闭时，相关子模块根本不存在，AXI 数据直通。这是一种典型的「按需付费」硬件设计。

**时间戳有两条回送路径。** PTP（精确时间协议）要求记录每一帧「真正开始发送/接收」的时刻。接收侧的时间戳可以随帧一起走（塞进 `tuser` 的高位），因为收完一帧时时间戳也就有了；但发送侧的时间戳要等到帧真正从 GMII 口送出那一刻才产生，此时原始 AXI 输入早已过去，所以发送时间戳只能走一条独立的旁带总线回送给应用，并附带一个 tag 好让应用认出「这是我发的哪一帧」。

> 术语速查：**GMII**（Gigabit Media Independent Interface，千兆介质无关接口）是 8 位宽的物理层信号；**FCS**（Frame Check Sequence，帧校验序列）即 4 字节 CRC-32；**IFG**（Inter-Frame Gap，帧间隔）是两帧之间线路上的空闲字节；**PTP**（Precision Time Protocol，精确时间协议）是亚微秒级的时间同步协议；**ToD**（Time of Day，日历时间）是 96 位的绝对时间戳格式。这些在 u1-l3、u2-l2、u4-l1 已建立，本讲直接使用。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [rtl/eth_mac_1g.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v) | 千兆 MAC 顶层，装配所有子模块 | 端口、参数、子模块例化与 `generate` 选配 |
| [rtl/axis_gmii_tx.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_tx.v) | AXI→GMII 发送器 | `ENABLE_PADDING`/`MIN_FRAME_LENGTH`、PTP 旁带输出 |
| [rtl/axis_gmii_rx.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_rx.v) | GMII→AXI 接收器 | 时间戳搭车进 `tuser` |
| tb/eth_mac_1g/test_eth_mac_1g.py | cocotb 仿真平台 | 驱动 TX/RX、断言 FCS 与时间戳 |

---

## 4. 核心概念与源码讲解

### 4.1 MAC 顶层组装与数据通路

#### 4.1.1 概念说明

`eth_mac_1g` 本身几乎不含逻辑——它是一个「布线层」。真正干活的是它例化的几个子模块：

- **`axis_gmii_rx`**：接收方向的 GMII→AXI 翻译器，剥前导码/SFD、校验 FCS、报告坏帧。
- **`axis_gmii_tx`**：发送方向的 AXI→GMII 翻译器，补前导码/SFD、可选填充、追加 FCS、留 IFG。
- **`mac_ctrl_rx` / `mac_ctrl_tx`**：MAC 控制帧的编解码（只识别/构造控制帧，不懂「暂停多久」）。
- **`mac_pause_ctrl_rx` / `mac_pause_ctrl_tx`**：真正掌握 PAUSE/PFC 语义的量子倒计时与帧发送。

后四个模块是否综合，由 `PAUSE_ENABLE`/`PFC_ENABLE` 决定。`eth_mac_1g` 的职责就是把这些模块按正确的数据流向连起来，并把对外端口收敛成一套干净的 AXI + GMII + 配置接口。

#### 4.1.2 核心流程

两条数据通路如下（默认 `MAC_CTRL_ENABLE=0`，即控制帧逻辑不综合时的直通情形）：

```
TX 方向（应用 → 线路）:
  tx_axis_*  ──►  axis_gmii_tx  ──►  gmii_txd / gmii_tx_en / gmii_tx_er
  (AXI 输入)      (补前导/填充/FCS)   (8 位 GMII 线路信号)

RX 方向（线路 → 应用）:
  gmii_rxd / gmii_rx_dv / gmii_rx_er  ──►  axis_gmii_rx  ──►  rx_axis_*
  (8 位 GMII 线路信号)                   (剥前导/校FCS)      (AXI 输出)
```

当 `MAC_CTRL_ENABLE=1`（开启 PAUSE/PFC）时，TX/RX 各多串一级 `mac_ctrl`：

```
TX: tx_axis_* ─► mac_ctrl_tx ─► axis_gmii_tx ─► gmii_txd
RX: gmii_rxd  ─► axis_gmii_rx ─► mac_ctrl_rx ─► rx_axis_*
                       │              │
                       └── mcf 旁路 ──┴──► mac_pause_ctrl_rx/tx（量子倒计时）
```

`mac_ctrl_rx` 在透传数据帧的同时，旁路扫描是否为 MAC 控制帧；命中则在帧尾从 `mcf_*` 侧信道送出解码结果给 `mac_pause_ctrl_rx`。`mac_ctrl_tx` 则把 `mac_pause_ctrl_tx` 产生的暂停帧**优先插队**发送（但不拆散正在发的数据帧）。

#### 4.1.3 源码精读

**端口与参数总览。** 模块声明集中体现了「这块 MAC 可调什么」：

[rtl/eth_mac_1g.v:34-49](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v#L34-L49) — 模块名与全部参数。其中 `DATA_WIDTH` 固定为 8（千兆 GMII 是 8 位的），`ENABLE_PADDING`/`MIN_FRAME_LENGTH` 控制帧格式，`PTP_TS_ENABLE`/`PTP_TS_FMT_TOD` 控制时间戳，`PFC_ENABLE`/`PAUSE_ENABLE` 控制流量控制选配。

模块有**两套独立时钟**：`rx_clk`/`rx_rst` 服务接收侧，`tx_clk`/`tx_rst` 服务发送侧（[L51-L54](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v#L51-L54)）。在纯千兆应用中两者常常同频同相甚至共用，但接口上分开，为 RGMII 等收发时钟独立的 PHY 留出余地（见 [u4-l5](u4-l5-rgmii-mac-ddr-io.md)）。

**接收通路例化。** `axis_gmii_rx` 直接把 GMII 管脚信号翻译成内部 AXI 流：

[rtl/eth_mac_1g.v:205-228](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v#L205-L228) — `axis_gmii_rx_inst`。注意它**没有** `tready` 输入（u4-l1 讲过：接收是线速、不可反压），同时把 `cfg_rx_enable`、`clk_enable`、`mii_select` 等控制线接进来，并上报 `start_packet`/`error_bad_frame`/`error_bad_fcs` 三个状态脉冲。

**发送通路例化。** `axis_gmii_tx` 把内部 AXI 流翻译成 GMII 管脚信号：

[rtl/eth_mac_1g.v:230-262](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v#L230-L262) — `axis_gmii_tx_inst`。这里把 `ENABLE_PADDING`、`MIN_FRAME_LENGTH`、`cfg_ifg`、`cfg_tx_enable` 都透传给发送器，并接收 `error_underflow`（AXI 源供不上数据导致的欠载错误）。

**`generate` 选配是本模块最关键的结构。** 整个控制帧子系统包在一个条件块里：

[rtl/eth_mac_1g.v:264-266](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v#L264-L266) — `generate if (MAC_CTRL_ENABLE)` 开启控制帧分支。而 `MAC_CTRL_ENABLE` 的定义只有一行：

[rtl/eth_mac_1g.v:191](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v#L191) — `parameter MAC_CTRL_ENABLE = PAUSE_ENABLE || PFC_ENABLE;`。只要二者任一为 1，就综合全部四个 `mac_ctrl_*`/`mac_pause_ctrl_*` 子模块（[L346-L600](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v#L346-L600)）；否则走 `else` 分支直接连线（[L602-L638](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v#L602-L638)），把所有 `stat_*` 统计输出恒置 0。这就是「按需付费」：不需要流量控制的应用，综合后这块逻辑面积就是零。

> **为什么 RX 侧 `mac_ctrl_rx` 的 `m_axis_tready` 接常 1？** 见 [L441](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v#L441)：因为上游 `axis_gmii_rx` 根本没有 `tready`（线速不可反压），下游必须照单全收。

#### 4.1.4 代码实践

**实践目标：** 用阅读源码的方式建立顶层数据通路的心理模型，为后面的仿真实践打基础。

**操作步骤：**

1. 打开 [rtl/eth_mac_1g.v](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v)。
2. 定位 `axis_gmii_rx_inst`（[L205](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v#L205)）和 `axis_gmii_tx_inst`（[L230](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v#L230)）。
3. 追踪一个发送字节：`tx_axis_tdata`（[L59](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v#L59)）→ 在 `MAC_CTRL_ENABLE=0` 时经 [L604](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v#L604) 连到 `tx_axis_tdata_int` → 进入 `axis_gmii_tx_inst` 的 `s_axis_tdata`（[L244](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v#L244)）→ 从 `gmii_txd`（[L249](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v#L249)）送出。
4. 在纸上画出 4.1.2 的两张通路图，标注每个子模块的时钟域（TX 子模块挂 `tx_clk`，RX 子模块挂 `rx_clk`）。

**需要观察的现象：** 你会发现 `MAC_CTRL_ENABLE=1` 时，TX 数据要在 `mac_ctrl_tx` 与 `axis_gmii_tx` 之间多经过一拍寄存；RX 数据则从 `axis_gmii_rx` 流向 `mac_ctrl_rx` 再到对外端口。

**预期结果：** 能不看源码说出「TX 侧 AXI 入口先碰到的子模块是 `mac_ctrl_tx`（若开启）或直连 `axis_gmii_tx`」。

#### 4.1.5 小练习与答案

**练习 1：** `eth_mac_1g` 为什么把 `MAC_CTRL_ENABLE` 设成 `PAUSE_ENABLE || PFC_ENABLE`，而不是单独依赖 `PAUSE_ENABLE`？

**参考答案：** 因为 PFC（优先级流量控制）是 PAUSE（LFC）的超集，二者都建立在 MAC 控制帧之上。只要用户开了 PFC，就同样需要 `mac_ctrl_rx/tx` 这套编解码基础设施。所以任一开启即需综合整套控制帧逻辑。

**练习 2：** 接收侧的 `axis_gmii_rx_inst` 没有 `tready` 输入，但 `mac_ctrl_rx_inst` 的输出 `m_axis_tready` 接了常 1（[L441](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v#L441)）。这对上层应用意味着什么？

**参考答案：** 意味着接收方向是**不可反压**的线速流：MAC 不缓存整帧，应用必须能以线速（1 Gbps / 125 MHz）消费 `rx_axis_*`，否则会丢帧。若应用做不到，就需要在 MAC 之外挂一个 FIFO 做缓冲——这正是 `eth_mac_1g_fifo` 变体（[u5-l1](u5-l1-mac-fifo-cdc.md)）存在的理由。

---

### 4.2 填充与最小帧长

#### 4.2.1 概念说明

以太网规范规定，一个帧在线路上的**总长度**（从目的 MAC 到 FCS，不含前导码/SFD）不得小于 64 字节。这条规定的根本原因是 CSMA/CD 冲突检测：在百兆/千兆半双工时代，发得太短的帧会在冲突信号传回之前就发完，导致发收方感知不到冲突。虽然现代全双工链路已无冲突，但 64 字节下限作为兼容约定保留至今。

问题是：上层应用完全可能产生一个只有十几字节的短帧（比如一个纯 ARP 请求的某种极短控制报文）。MAC 在发送侧必须负责把它**补零填充**到 64 字节，否则对端设备的合法性检查会把它当成 runt（残帧）丢弃。这就是 `ENABLE_PADDING` 与 `MIN_FRAME_LENGTH` 这对参数存在的意义。

#### 4.2.2 核心流程

填充完全发生在 `axis_gmii_tx` 的发送状态机里（u4-l1 已介绍过这台七状态机：IDLE→PREAMBLE→PAYLOAD→LAST→PAD→FCS→IFG）。关键在 `PAD` 状态：

```
发送一帧时:
  1. IDLE: 初始化计数器 frame_min_count = MIN_FRAME_LENGTH - 4 - 1
           （-4 是为 4 字节 FCS 预留；这样计数器追踪的是「MAC 体」还需多少字节）
  2. PAYLOAD: 边发载荷边递减计数器
  3. LAST:   发最后一个载荷字节；若 ENABLE_PADDING 且计数器仍 >0 → 进入 PAD
  4. PAD:    每拍发一个 0x00，递减计数器，直到归零 → 进入 FCS
  5. FCS:    连发 4 字节 ~crc_state（小端），CRC 覆盖了上面所有实际输出字节（含填充）
  6. IFG:    发帧间间隔
```

一个关键细节：**FCS 是在填充之后、基于「实际发出去的所有字节（含填充零）」算出来的**。所以即使载荷只有 10 字节，对端收到的也是一个合法的 64 字节帧，FCS 校验通过。

> 说明：计数器初值 `MIN_FRAME_LENGTH-4-1` 中的 `-1` 来自发送流水线的预取对齐（`axis_gmii_tx` 用 `s_tdata_reg` 缓存一字节做预取，详见 u4-l1 的状态机）。本讲只需记住「`-4` 留给 FCS，计数器随每个实际输出字节递减，归零即停止填充」即可，不必纠结于个别的 ±1。

#### 4.2.3 源码精读

**参数声明。** `eth_mac_1g` 把这两个参数透传给 `axis_gmii_tx`：

[rtl/eth_mac_1g.v:37-38](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v#L37-L38) — `parameter ENABLE_PADDING = 1, parameter MIN_FRAME_LENGTH = 64;`。默认即开启填充、最小 64 字节。

[rtl/eth_mac_1g.v:232-233](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v#L232-L233) — 例化 `axis_gmii_tx` 时把这两个参数原样传入。

**计数器宽度由最小帧长决定。** 在 `axis_gmii_tx` 内：

[rtl/axis_gmii_tx.v:93](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_tx.v#L93) — `parameter MIN_LEN_WIDTH = $clog2(MIN_FRAME_LENGTH-4-1+1);`。用 `$clog2` 自动算出计数器需要几位宽，省去手工指定。

**IDLE 里初始化计数器：**

[rtl/axis_gmii_tx.v:250](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_tx.v#L250) — `frame_min_count_next = MIN_FRAME_LENGTH-4-1;`。每帧开始前都重置一次。

**LAST 状态决定是否进入填充：**

[rtl/axis_gmii_tx.v:331-338](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_tx.v#L331-L338) — 若 `ENABLE_PADDING && frame_min_count_reg` 仍非零，就递减计数器、把下一字节设为 `8'd0`、跳到 `STATE_PAD`；否则直接跳 `STATE_FCS`。注意这里的 `update_crc = 1'b1`（见 [L323](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_tx.v#L323)）——最后一个载荷字节也参与 CRC。

**PAD 状态发零并递减：**

[rtl/axis_gmii_tx.v:340-359](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_tx.v#L340-L359) — 每拍 `gmii_txd_next = 8'd0`、`update_crc = 1'b1`（**填充零也喂给 CRC**），计数器归零后跳 `STATE_FCS`。

**FCS 状态发 4 字节小端 CRC：**

[rtl/axis_gmii_tx.v:360-372](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_tx.v#L360-L372) — 按 `frame_ptr_reg` 依次输出 `~crc_state[7:0]`、`~crc_state[15:8]`、`~crc_state[23:16]`、`~crc_state[31:24]`。取反（`~`）是 CRC-32 的标准终值处理（u2-l1 已讲过）。

#### 4.2.4 代码实践

**实践目标：** 通过现有仿真平台直观观察填充效果，验证「短帧被补到 64 字节且 FCS 仍正确」。

**操作步骤：**

1. 确认本机已装好 cocotb、cocotbext-eth、cocotbext-axi 与 Icarus Verilog（见 [u1-l4](u4-l1-axis-gmii-rx-tx.md)）。
2. 打开 [tb/eth_mac_1g/test_eth_mac_1g.py](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py)，找到 `run_test_tx`（[L231](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L231)）。它通过 `axis_source.send` 注入 AXI 帧，用 `gmii_sink.recv` 捕获线路帧，并断言 `rx_frame.check_fcs()`（[L265](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L265)）。
3. 在 `test_eth_mac_1g`（[L709](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L709)）的 `parameters` 里把 `ENABLE_PADDING` 改成 `1`（默认就是 1），确保 `MIN_FRAME_LENGTH=64`。
4. 进入 `tb/eth_mac_1g` 目录运行 `make`（或 `pytest tb/eth_mac_1g`）。
5. 想看填充字节，可在 `run_test_tx` 的 `rx_frame = await tb.gmii_sink.recv()` 之后加一行 `tb.log.info("frame len=%d", len(rx_frame.get_payload()))`。
6. 再把 `ENABLE_PADDING` 改成 `0` 重跑一次做对比。

**需要观察的现象：** `ENABLE_PADDING=1` 时，即便注入 10 字节载荷，捕获到的线路帧（含 FCS）长度应为 64，且 `check_fcs()` 通过；`ENABLE_PADDING=0` 时，线路帧长度等于「载荷 + 4 字节 FCS」，仍能通过 FCS 校验，但短于 64 字节。

**预期结果：** 仿真正常通过，日志里 `frame len` 与上述描述一致。

**待本地验证：** 上述具体字节数依赖本机实际仿真输出，当前环境未安装仿真器，请以本地 `make` 结果为准。

#### 4.2.5 小练习与答案

**练习 1：** 假如把 `MIN_FRAME_LENGTH` 设成 128，对一个 60 字节的载荷帧会发生什么？

**参考答案：** 发送器会把帧补零到「MAC 体 = 128 − 4 = 124 字节」，即在 60 字节载荷后追加 64 字节零，再发 4 字节 FCS，线路帧总长 128 字节，FCS 覆盖全部 124 字节 MAC 体。这个参数允许你把帧人为撑大（某些应用用它来固定帧长、简化调度）。

**练习 2：** 为什么填充的零字节也必须喂给 CRC（`PAD` 状态里 `update_crc = 1'b1`）？

**参考答案：** 因为 FCS 校验的是线路上的**实际字节流**。如果填充零不参与 CRC，那么发送方算出的 FCS 与接收方（它并不知道哪些是填充、哪些是载荷，只看到一串字节）算出的 FCS 就对不上，合法帧会被误判为坏帧。

---

### 4.3 PTP 时间戳旁路与 tag 机制

#### 4.3.1 概念说明

PTP 的核心需求是：**精确记录每一帧开始发送/接收的时刻**（具体说是 SFD——帧起始定界符出现的时刻），精度要到纳秒甚至亚纳秒。`eth_mac_1g` 用 `PTP_TS_ENABLE` 这一个开关，把这套时间戳机制挂到数据通路上。

时间戳的传递有两条截然不同的路径，理解它们的区别是本节的核心：

- **接收（RX）时间戳：搭车进 `tuser`。** 收帧时，`axis_gmii_rx` 在检测到 SFD 的那一拍锁存外部送来的 `rx_ptp_ts`，然后在帧尾把它拼到 AXI 的 `tuser` 高位，与载荷一起送达应用。这是「带内」（in-band）传递，因为收完帧时时间戳也已就绪，天然可随帧走。
- **发送（TX）时间戳：旁带回送。** 发帧时，应用把帧交给 MAC 后就「撒手」了，但时间戳要等到帧真正从 GMII 口送出（SFD 上线）那一刻才产生。此时应用的 AXI 输入早已结束，无法再回头修改。所以发送时间戳走一条**独立的旁带总线**（`tx_axis_ptp_ts` + `tx_axis_ptp_ts_valid`）单独回送，并附带一个 `tx_axis_ptp_ts_tag`，让应用能把回送的时间戳和「我之前发出去的那一帧」对应起来。

#### 4.3.2 核心流程

```
接收侧时间戳（PTP_TS_ENABLE=1）:
  gmii 上出现 SFD ──► axis_gmii_rx 锁存 rx_ptp_ts 到 ptp_ts_reg
                  └─► 帧尾: m_axis_tuser = {ptp_ts_reg, bad_frame}
                                （时间戳在高位，坏帧标志在最低位）

发送侧时间戳（PTP_TS_ENABLE=1）:
  应用在 tx_axis_tuser 里放 tag(16bit) + 请求位(1bit, 可选) + 坏帧位(1bit)
     │
     ▼
  axis_gmii_tx 在 SFD 上线那一拍:
     ──► tx_axis_ptp_ts      = tx_ptp_ts（外部送入的当前时间）
     ──► tx_axis_ptp_ts_tag  = 应用给的 tag（原样回传）
     ──► tx_axis_ptp_ts_valid= 1（一个周期的脉冲）
```

时间戳的格式由 `PTP_TS_FMT_TOD` 决定：为 1（默认）则是 96 位的 ToD（日历时间）格式；为 0 则是 64 位的相对时间格式。`PTP_TS_WIDTH` 据此自动推导（[L41](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v#L41)）。

#### 4.3.3 源码精读

**PTP 端口。** 顶层暴露的时间戳接口：

[rtl/eth_mac_1g.v:86-90](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v#L86-L90) — `tx_ptp_ts`/`rx_ptp_ts` 是外部（通常是 `ptp_clock` 模块，见 [u11-l1](u11-l1-ptp-clock.md)）送进来的「当前时间」；`tx_axis_ptp_ts`/`tx_axis_ptp_ts_tag`/`tx_axis_ptp_ts_valid` 是发送时间戳的旁带回送。

**tuser 位宽随 PTP 膨胀。** 这一对参数体现了「时间戳搭车」如何改变接口宽度：

[rtl/eth_mac_1g.v:45-46](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v#L45-L46) — `TX_USER_WIDTH` 在 PTP 开启时包含 tag 宽度（+可选控制位）+1；`RX_USER_WIDTH` 在 PTP 开启时包含整段时间戳 +1。关闭时两者都退化为 1（仅坏帧位）。

**接收侧锁存与拼接。** 在 `axis_gmii_rx` 内：

[rtl/axis_gmii_rx.v:138](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_rx.v#L138) — 声明 `ptp_ts_reg` 寄存器。

[rtl/axis_gmii_rx.v:258-261](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_rx.v#L258-L261) — 在 `start_packet_int_reg` 置位（即检测到 SFD）的那一拍，把当前 `ptp_ts` 锁进 `ptp_ts_reg`，同时拉高 `start_packet`。

[rtl/axis_gmii_rx.v:146](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_rx.v#L146) — `assign m_axis_tuser = PTP_TS_ENABLE ? {ptp_ts_reg, m_axis_tuser_reg} : m_axis_tuser_reg;`。PTP 开启时，`tuser` 的高位是时间戳、最低位是坏帧标志——这正是 u1-l3 所说「`axis_gmii_rx` 把 `tuser` 扩展为时间戳＋坏帧位，坏帧位恒居最低位」的实现。

**发送侧旁带输出。** 在 `axis_gmii_tx` 内：

[rtl/axis_gmii_tx.v:200-209](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_tx.v#L200-L209) — 当 `start_packet_reg` 为 1（SFD 上线那一拍）且 `PTP_TS_ENABLE` 时：`m_axis_ptp_ts_next = ptp_ts`（锁存当前时间为发送时间戳）；若 `PTP_TS_CTRL_IN_TUSER`，则 tag 与 valid 从 `tuser` 的相应位取（应用可按帧选择是否请求时间戳），否则 tag = `tuser >> 1`、valid 恒为 1（每帧都回送）。

[rtl/axis_gmii_tx.v:155-157](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/axis_gmii_tx.v#L155-L157) — 三个旁带输出在 PTP 关闭时恒为 0，对应用透明。

**`eth_mac_1g` 内的一个微妙交互。** 当 MAC 控制帧逻辑也开启时，`tuser` 里要多留一位给 `mac_ctrl_tx` 用，所以 `PTP_TS_CTRL_IN_TUSER` 被强制：

[rtl/eth_mac_1g.v:236](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v#L236) — `.PTP_TS_CTRL_IN_TUSER(MAC_CTRL_ENABLE ? PTP_TS_ENABLE : TX_PTP_TS_CTRL_IN_TUSER)`。并在 [L338-L344](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v#L338-L344) 构造内部 `tuser` 时注入这一位。这是「PTP 时间戳位」与「MAC 控制帧位」共用 `tuser` 总线时的仲裁处理，初学时了解即可。

#### 4.3.4 代码实践

**实践目标：** 借助现有仿真平台理解「RX 时间戳在 `tuser` 高位、TX 时间戳走旁带总线」，并读懂时间戳精度的断言。

**操作步骤：**

1. 确认 `tb/eth_mac_1g/Makefile` 中 `PARAM_PTP_TS_ENABLE := 1`（[L45](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/Makefile#L45)），这是默认配置。
2. 打开 `test_eth_mac_1g.py`，定位 `run_test_rx`（[L184](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L184)）。注意 [L211-L213](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L211-L213)：测试代码正是用 `rx_frame.tuser & 1` 取坏帧位、`rx_frame.tuser >> 1` 取时间戳——这印证了 4.3.3 的 `tuser` 拼接顺序。
3. 看 [L215-L223](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L215-L223)：它把 96 位 ToD 时间戳换算成纳秒（`ptp_ts / 2**16`，因为低 16 位是小数纳秒），与仿真器记录的 SFD 时刻比对，误差要求 `< 0.01 ns`。
4. 再看 `run_test_tx`（[L253-L267](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L253-L267)）：TX 时间戳从独立的 `tx_ptp_ts_sink`（[L78](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L78)）收取，而不是从 AXI 帧。
5. 运行 `make`。

**需要观察的现象：** 仿真日志会打印 `RX frame PTP TS: ... ns`、`TX frame SFD sim time: ... ns`、`Difference: ...`，差值应极小。

**预期结果：** 全部用例通过，`Difference` 满足 `< 0.01 ns` 的断言。

**待本地验证：** 具体日志数值依赖本机仿真，当前环境无仿真器，请以本地输出为准。

#### 4.3.5 小练习与答案

**练习 1：** 为什么 RX 时间戳可以放进 `tuser`（带内），而 TX 时间戳必须走旁带总线？

**参考答案：** RX 方向在帧尾组装 AXI 输出时，时间戳（SFD 时刻）早已锁存好，可以随帧一起输出，故走 `tuser` 高位即可。TX 方向的时间戳在 SFD 真正上线那一刻才产生，而此时应用的 AXI 输入早已结束、`tuser` 已被消费，无法回头修改，只能用独立的 `tx_axis_ptp_ts` 总线异步回送。

**练习 2：** `tx_axis_ptp_ts_tag` 的作用是什么？为什么需要它？

**参考答案：** 它把应用在发送时打在 `tx_axis_tuser` 里的 tag 原样随时间戳回传。因为发送时间戳是滞后、乱序回送的（尤其有多帧在管线中时），应用需要一个关联手段判断「这个时间戳属于我发的哪一帧」。tag 就是这个关联键，典型实现是用一个递增的帧序号作 tag。

---

## 5. 综合实践：TX→RX 环回验证填充与 FCS

把本讲三个模块串起来，做一个端到端的小任务：让 `eth_mac_1g` 自发自收——从 TX 侧 AXI 注入一帧短数据，让它的 GMII 输出环回到自己的 RX 侧 GMII 输入，然后在 RX 侧 AXI 观察收到的帧是否带正确填充与 FCS。

**实践目标：** 验证「TX 填充 + FCS 追加」与「RX 剥 FCS + 校验」这两条通路在同一块 MAC 上闭环后能自洽。

**思路与操作步骤：**

1. **理解现有平台为何不是直接环回。** 现有 `tb/eth_mac_1g/test_eth_mac_1g.py` 用 `GmiiSource`（驱动 `gmii_rxd`）和 `GmiiSink`（采集 `gmii_txd`）两个**独立**端点，分别测 RX 与 TX。要做成硬件环回，需要把 TX 的 GMII 输出连到 RX 的 GMII 输入。

2. **编写一个最小环回 wrapper（示例代码，非项目原有文件）：**

   ```verilog
   // 示例代码：eth_mac_1g_loopback_wrapper.v（需自行新建于仿真目录）
   // 仅当 rx_clk 与 tx_clk 同频时可直接连线；本例两者均为 125 MHz。
   module eth_mac_1g_loopback_wrapper #(parameter PTP_TS_ENABLE = 0) (
       input wire clk, input wire rst,
       input wire [7:0] tx_axis_tdata, input wire tx_axis_tvalid,
       output wire tx_axis_tready, input wire tx_axis_tlast,
       input wire tx_axis_tuser,
       output wire [7:0] rx_axis_tdata, output wire rx_axis_tvalid,
       output wire rx_axis_tlast, output wire rx_axis_tuser
   );
       wire [7:0] gmii_txd, gmii_rxd;
       wire gmii_tx_en, gmii_tx_er, gmii_rx_dv, gmii_rx_er;
       // 环回：把 TX 的 GMII 输出接到 RX 的 GMII 输入
       assign gmii_rxd   = gmii_txd;
       assign gmii_rx_dv = gmii_tx_en;
       assign gmii_rx_er = gmii_tx_er;

       eth_mac_1g #(.ENABLE_PADDING(1), .MIN_FRAME_LENGTH(64),
                    .PTP_TS_ENABLE(PTP_TS_ENABLE)) dut (
           .rx_clk(clk), .rx_rst(rst), .tx_clk(clk), .tx_rst(rst),
           .tx_axis_tdata(tx_axis_tdata), .tx_axis_tvalid(tx_axis_tvalid),
           .tx_axis_tready(tx_axis_tready), .tx_axis_tlast(tx_axis_tlast),
           .tx_axis_tuser(tx_axis_tuser),
           .rx_axis_tdata(rx_axis_tdata), .rx_axis_tvalid(rx_axis_tvalid),
           .rx_axis_tlast(rx_axis_tlast), .rx_axis_tuser(rx_axis_tuser),
           .gmii_rxd(gmii_rxd), .gmii_rx_dv(gmii_rx_dv), .gmii_rx_er(gmii_rx_er),
           .gmii_txd(gmii_txd), .gmii_tx_en(gmii_tx_en), .gmii_tx_er(gmii_tx_er),
           .tx_ptp_ts(0), .rx_ptp_ts(0),
           .rx_clk_enable(1'b1), .tx_clk_enable(1'b1),
           .rx_mii_select(1'b0), .tx_mii_select(1'b0),
           .cfg_ifg(8'd12), .cfg_tx_enable(1'b1), .cfg_rx_enable(1'b1)
           /* 其余 cfg_* 与 stat_* 端口按需接地或留空 */
       );
   endmodule
   ```

   > 说明：`eth_mac_1g` 端口很多（控制帧配置、统计等），上面只连了与数据通路、时钟、使能相关的关键端口；其余端口在 `MAC_CTRL_ENABLE=0`（默认）时仍需合法连接或在该 wrapper 顶层一并引出。完整端口见 [eth_mac_1g.v:50-189](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/rtl/eth_mac_1g.v#L50-L189)。本仓库的 `eth_mac_1g_fifo`（见 [u5-l1](u5-l1-mac-fifo-cdc.md)）就是一个更完整的「把 MAC 包起来只留 AXI 接口」的范例，可直接参考它的连线。

3. **更省事的做法：软件环回。** 不写 wrapper，而是仿照 `run_test_tx`/`run_test_rx`，用 cocotb 把 `gmii_sink` 收到的帧再喂回 `gmii_source`。由于本平台 `rx_clk` 与 `tx_clk` 都是 8 ns（[test_eth_mac_1g.py:65-66](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L65-L66)），两个域同频，软件搬运可行。

4. **注入一帧 10 字节短数据**（例如 `b'\xAA'*10`），从 `axis_source` 发送。

5. **在 `axis_sink` 接收**，检查：载荷仍是 10 字节（**RX 会剥掉填充与 FCS**，因为填充属于线路层、不进 AXI），`tuser` 最低位（坏帧标志）为 0，且 `error_bad_fcs` 不触发。

**需要观察的现象：** 环回后，TX 侧线路帧被补到 64 字节（含 4 字节 FCS）；RX 侧还原出原始 10 字节载荷，且 FCS 校验通过（坏帧位=0）。

**预期结果：** 收到的 AXI 帧载荷与发送的 10 字节完全一致，`rx_frame.tuser & 1 == 0`。注意：环回后 RX 的 `tuser` 里**不含时间戳**（若 `PTP_TS_ENABLE=1` 则会含 `rx_ptp_ts`，但本 wrapper 把 `rx_ptp_ts` 接了 0）。

**待本地验证：** 当前环境未安装仿真器，上述环回行为未实际运行；关键结论（TX 填充、RX 剥 FCS、载荷还原）已被仓库现有 `run_test_tx`/`run_test_rx` 的断言分别证明（[L265 check_fcs](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L265)、[L221 tdata 比对](https://github.com/alexforencich/verilog-ethernet/blob/77320a9471d19c7dd383914bc049e02d9f4f1ffb/tb/eth_mac_1g/test_eth_mac_1g.py#L221)），把它们合并成环回即得本实践。请以本地仿真结果为准。

> ⚠️ **重要：本实践只可在 `verilog-ethernet-tutorial/` 目录下新建文件做仿真练习，不要修改 `rtl/` 源码或仓库原有的 testbench。** wrapper 与新测试脚本应放在你自己的工作目录。

## 6. 本讲小结

- `eth_mac_1g` 是一个**布线层**：它例化 `axis_gmii_rx`/`axis_gmii_tx` 完成 GMII↔AXI 翻译，并按 `MAC_CTRL_ENABLE = PAUSE_ENABLE || PFC_ENABLE` 用 `generate` 块选配 `mac_ctrl_*`/`mac_pause_ctrl_*` 四个流量控制子模块——默认关闭时这部分逻辑零面积。
- 收发各走独立时钟域（`rx_clk`/`tx_clk`）；接收方向无 `tready`、线速不可反压，需要缓冲时得用 `eth_mac_1g_fifo`。
- `ENABLE_PADDING=1` 时，`axis_gmii_tx` 在 `PAD` 状态把短帧补零到 `MIN_FRAME_LENGTH`（默认 64，计数器初值 `MIN_FRAME_LENGTH-4-1`，`-4` 留给 FCS），且填充零也参与 CRC，故对端 FCS 校验仍通过。
- PTP 时间戳有两条路径：**RX 时间戳搭车进 `tuser` 高位**（SFD 时刻锁存，帧尾随帧送出），**TX 时间戳走旁带总线** `tx_axis_ptp_ts` 连同 `tag`/`valid` 回送（因发送时间戳产生时 AXI 输入已结束）。
- 时间戳格式由 `PTP_TS_FMT_TOD` 决定（1→96 位 ToD，0→64 位相对），`tuser` 位宽随 `PTP_TS_ENABLE` 自动膨胀。
- 仓库的 `tb/eth_mac_1g` cocotb 平台已分别覆盖 TX（`check_fcs`）与 RX（时间戳精度 `< 0.01 ns`）两侧，是验证上述行为的现成依据。

## 7. 下一步学习建议

- **[u4-l4 PHY 接口与时钟](u4-l4-phy-if-and-tri-mode.md)**：本讲的 `gmii_*` 管脚如何对接真实 PHY 芯片、`gmii_phy_if` 如何处理源同步时钟、三模 MAC 如何在 10/100/1000M 间自动适配。
- **[u4-l5 RGMII MAC 与 DDR IO](u4-l5-rgmii-mac-ddr-io.md)**：`eth_mac_1g_rgmii` 如何在本讲 MAC 外再包一层 RGMII 物理接口，用 DDR 原语在时钟双边沿传 4 位。
- **[u5-l1 MAC FIFO 集成](u5-l1-mac-fifo-cdc.md)**：当你需要把接收方向变成可反压、或桥接 logic/PHY 两个时钟域时，就看 `eth_mac_1g_fifo`——它是把本讲 MAC 包成「只留 AXI 接口」的最佳范例。
- **[u11-l1 ptp_clock](u11-l1-ptp-clock.md)**：本讲只用了 `tx_ptp_ts`/`rx_ptp_ts` 的「消费者」视角；想了解这个时间从哪里来、如何微调频率与漂移，进入 PTP 子系统。
