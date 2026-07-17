# openofdm_tx OFDM 发射机总览

## 1. 本讲目标

本讲进入 openwifi 的**发射链路**（TX）。在 [u2-l2](u2-l2-openwifi-ip-hierarchy.md) 中我们已经知道：发射方向的数据流是 `PS → DMA → tx_intf（写 BRAM）→ openofdm_tx（生成 I/Q）→ tx_intf → DAC`。本讲要回答的核心问题是——**`openofdm_tx` 这个 IP 到底把 BRAM 里的一串字节变成了什么？它是被谁触发、什么时候开始、什么时候结束的？**

读完本讲你应该能做到：

1. 说清 `openofdm_tx` 在发射链路中的位置、它读什么、写什么。
2. 看懂它的输入（BRAM 字节、握手信号）和输出（基带 I/Q 样点）接口。
3. 画出 `phy_tx_start → phy_tx_started → result_iq_valid → phy_tx_done` 这条握手时序。
4. 解释 `bram_din` / `bram_addr` 是如何被驱动的——谁给地址、谁给数据。
5. 理解 `dot11_tx` 内部「三条状态机 + 两个 FIFO」的流水线骨架（细节子模块留到 [u4-l2](u4-l2-openofdm-tx-signal-processing.md)）。

> 说明：本讲是「总览」，重点讲**接口、握手与流水线骨架**；卷积编码、打孔交织、调制、IFFT、前导 ROM 等信号处理子模块的逐行实现属于下一讲 [u4-l2](u4-l2-openofdm-tx-signal-processing.md)。

## 2. 前置知识

### 2.1 为什么要「发射机」

Wi-Fi 是双向的。在 [u3（接收链路）](u3-l1-rx-intf-overview.md) 中我们看到 `openofdm_rx` 把天线收到的模拟波形一步步还原成字节。发射机做的是**反方向**的事：把上层（mac80211）要发的**字节**，变成 AD9361 数模转换器（DAC）能直接吃的**基带 I/Q 数字样点**。这两条链路在物理层是严格对称的——接收端做的每一步（FFT、均衡、Viterbi 译码……），发射端都要反过来做一遍（IFFT、调制、卷积编码……）。

### 2.2 OFDM 与 802.11 帧：发射机要拼出的东西

IEEE 802.11 a/g/n 在 20MHz 信道上用 **OFDM**（正交频分复用）：把数据分散到 56 个子载波上，每 64 个频域样点做一次 **IFFT** 得到 64 个时域样点（一个「OFDM 符号」），再在前面复制一段尾部样点作为**循环前缀 CP**，最后串行送出。一个完整的 802.11 PPDU（物理层帧）从前往后大致是：

```
Legacy: [L-STF][L-LTF][L-SIG][DATA 符号 × N]
HT:     [L-STF][L-LTF][L-SIG][HT-SIG][HT-STF][HT-LTF][DATA 符号 × N]
```

- **L-STF / L-LTF**：legacy 短/长训练序列，给接收端做包检测、同步、信道估计。
- **L-SIG**：legacy 信号域，告诉接收端「速率、长度」。
- **HT-SIG / HT-STF / HT-LTF**：802.11n（HT）才有的额外字段。

`openofdm_tx` 的全部职责，就是把 BRAM 里的字节，按上面这个结构拼成一段连续的 I/Q 样点流。这些训练/信号域样点不是实时算出来的，而是预先存在 ROM 里（`l_stf_rom`、`l_ltf_rom`、`ht_stf_rom`、`ht_ltf_rom`），DATA 符号才是实时从字节算出来的。

### 2.3 关键术语速查

| 术语 | 含义 |
|------|------|
| PSDU | 物理层服务数据单元，即「真正要发的数据字节」 |
| PLCP | 物理层会聚过程，`L-SIG`/`HT-SIG` 这些前导里的控制字段统称 PLCP |
| MCS | 调制与编码方案（Modulation and Coding Scheme），决定速率 |
| N_BPSC | 每子载波比特数（1=BPSK, 2=QPSK, 4=16-QAM, 6=64-QAM） |
| N_DBPS | 每个 OFDM 符号承载的数据比特数 |
| CP | 循环前缀（Cyclic Prefix），16 个样点 |
| S_GI | 短保护间隔（Short Guard Interval），HT 可选 0.4µS 取代 0.8µS |
| BRAM | FPGA 上的块 RAM，这里用作「待发字节缓存」 |
| back-pressure（背压） | 下游忙时通过一个 ready/hold 信号让上游停一停 |

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [ip/openofdm_tx/src/openofdm_tx.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/openofdm_tx.v) | **IP 顶层薄壳**。只做两件事：例化 `dot11_tx` 核与 `openofdm_tx_s_axi` 寄存器从设备，把对外端口接上。 |
| [ip/openofdm_tx/src/dot11_tx.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v) | **发射机核心**。三条状态机把字节变成 I/Q 样点，是本讲的主角。 |
| [ip/openofdm_tx/src/dot11_tx_tb.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx_tb.v) | 仿真 testbench。演示了「BRAM 初始化 → `phy_tx_start` 脉冲 → 收集 I/Q → `phy_tx_done` 结束」的最小用法，是理解接口的最佳样本。 |
| [ip/openwifi_ip.tcl](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl) | block design 脚本，记录 `openofdm_tx` 与 `tx_intf` 之间的真实连线（谁连到谁）。 |
| [ip/tx_intf/src/tx_intf.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_intf.v) | 上游邻居。它管理 BRAM、发 `phy_tx_start`、接收 I/Q 并送往 DAC。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 openofdm_tx 顶层薄壳**：接口与复位。
- **4.2 dot11_tx 的三条状态机流水线**：数据怎么一步步变成样点。
- **4.3 phy_tx_start 握手时序与 BRAM 驱动**：触发、背压、谁驱动 BRAM 地址线。

### 4.1 openofdm_tx 顶层薄壳：接口与复位

#### 4.1.1 概念说明

`openofdm_tx` 这个 IP 对外看起来像一个「黑盒」：左边吃字节，右边吐 I/Q。但在 Verilog 里它其实是个**很薄的壳**——真正的活儿全在 `dot11_tx` 里。这个壳存在的意义有两个：

1. **套上 AXI4-Lite 寄存器接口**，让 ARM（PS）能通过寄存器配置它（复位、扰码初始状态）。
2. **统一对外端口**，便于在 block design 里当成一个标准 IP 来接线。

所以顶层 `openofdm_tx.v` 全文只例化了两个子模块：`dot11_tx`（干活）和 `openofdm_tx_s_axi`（寄存器），把两者的信号连起来。

#### 4.1.2 核心流程

```text
                ┌──────────────── openofdm_tx（顶层薄壳）────────────────┐
                │                                                          │
 phy_tx_arestn─►│─┐                                                        │
 phy_tx_start──►│─┼──────────────────────────────┐                        │
 bram_din◄──────│─┼──────────────────────────────┤                        │
 bram_addr─────►│─┼──────────────────────────────┤   ┌──────────────┐     │
 result_iq_hold►│─┼─────────────┐                ├──►│   dot11_tx   │────►│ result_iq_valid
                │ │             │                │   │  (发射核心)  │────►│ result_i
                │ │             │                │   └──────────────┘     │ result_q
                │ │             │                │                        │ phy_tx_done
                │ │             │  slv_reg0/1/2  │   ┌──────────────┐     │ phy_tx_started
   s00_axi_* ──►│─┼─────────────┴────────────────┼──►│openofdm_tx   │     │
                │ │                                │   |_s_axi (寄存器)│     │
                │ └──► phy_tx_arest = ~arestn      │   └──────────────┘     │
                │             | slv_reg0[0]        │                        │
                └──────────────────────────────────────────────────────────┘
```

关键点：**复位可以来自两处**——硬件 `phy_tx_arestn`（低有效，来自上游复位链）和软件 `slv_reg0[0]`（PS 写寄存器触发的软复位）。两者「或」起来送给 `dot11_tx` 的 `phy_tx_arest`（高有效）。

#### 4.1.3 源码精读

顶层端口就是「握手 + BRAM + I/Q + AXI」四组。注意 `bram_din` 是 64 位输入（一次读 8 字节），`bram_addr` 是 10 位输出（可寻址 1024 个 64-bit 字 = 8KB）：

[ip/openofdm_tx/src/openofdm_tx.v:L16-L29](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/openofdm_tx.v#L16-L29) — 顶层端口：`clk`、握手三件套（`phy_tx_start/done/started`）、BRAM 接口（`bram_din` 入、`bram_addr` 出）、I/Q 输出（`result_i/result_q/result_iq_valid`）与背压 `result_iq_hold`。

`dot11_tx` 的例化是本层的核心。看这一行复位表达式，它把「硬件复位取反」和「软件寄存器位」用或门合并：

[ip/openofdm_tx/src/openofdm_tx.v:L91-L109](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/openofdm_tx.v#L91-L109) — 例化 `dot11_tx`。注意三处：
- `.phy_tx_arest((~phy_tx_arestn)|slv_reg0[0])`：硬件复位与软件复位合并。
- `.init_pilot_scram_state(slv_reg1[6:0])` / `.init_data_scram_state(slv_reg2[6:0])`：扰码器的 7 位初始状态由 PS 经寄存器写入（每包可不同，避免连续包用相同扰码序列）。
- `.result_iq_ready(~result_iq_hold)`：**背压取反**。下游（`tx_intf`）给的是「hold（暂停）」语义，而 `dot11_tx` 内部用的是「ready（就绪）」语义，于是取一次反衔接起来。

> 寄存器 `slv_reg0/1/2/20` 经 `openofdm_tx_s_axi` 暴露给 PS。`slv_reg0[0]` 是软复位，`slv_reg1/slv_reg2` 是扰码初值，`slv_reg20` 用于回读核内状态。地址译码遵循 Xilinx 标准 AXI4-Lite 模板，每个 `slv_reg` 占 4 字节（`slv_reg0`→0x00，`slv_reg1`→0x04，`slv_reg2`→0x08，`slv_reg20`→0x50），寄存器组的逐行映射细节留到 [u7-l1](u7-l1-axi-register-map.md)。

#### 4.1.4 代码实践

**实践目标**：用「连线清单」的方式确认顶层薄壳的端口分组。

**操作步骤**：

1. 打开 [openofdm_tx.v 端口段](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/openofdm_tx.v#L15-L52)。
2. 把端口分成四组抄下来：① 时钟/复位 ② 握手 ③ BRAM ④ I/Q + AXI。

**预期表格**（参考答案）：

| 分组 | 端口 | 方向 |
|------|------|------|
| 时钟/复位 | `clk`, `phy_tx_arestn`, `s00_axi_aclk`, `s00_axi_aresetn` | in |
| 握手 | `phy_tx_start`(in), `phy_tx_done`(out), `phy_tx_started`(out) | — |
| BRAM | `bram_din[63:0]`(in), `bram_addr[9:0]`(out) | 双向（不同方向） |
| I/Q | `result_i[15:0]`, `result_q[15:0]`, `result_iq_valid`(out), `result_iq_hold`(in) | out/in |

**需要观察的现象**：注意 `bram_addr` 是**输出**——意味着寻址由 `openofdm_tx` 主导，BRAM 的另一端口（写入端）由 `tx_intf` 控制；两边通过同一块双口 BRAM 交换数据。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `result_iq_hold` 要取反成 `result_iq_ready` 再喂给 `dot11_tx`？

> 答案：`tx_intf`（下游）用「hold=1 表示别发了」的负逻辑，而 `dot11_tx` 内部状态机用「ready=1 表示可以发」的正逻辑。取反是把两套背压约定衔接起来，`~hold` 即「不被按住 = 就绪」。

**练习 2**：`phy_tx_arestn` 是低有效还是高有效？`phy_tx_arest` 呢？

> 答案：`phy_tx_arestn` 低有效（名字带 `n` 后缀，惯例同 AXI 的 `aresetn`）；`phy_tx_arest` 高有效。所以顶层要写 `~phy_tx_arestn` 把低有效翻转成高有效再参与或逻辑。

---

### 4.2 dot11_tx 的三条状态机流水线

#### 4.2.1 概念说明

进入 `dot11_tx.v`（800 多行）如果不抓主线会迷路。它的设计精髓是**三条独立运转的状态机 + 两个 FIFO**，构成一条三级流水线。为什么这么设计？因为「从字节生成一个 OFDM 符号」要经过扰码→卷积编码→打孔→交织→调制→插导频→IFFT 一长串步骤，远不止一个时钟；但射频口要求**每个时钟都要有一个 I/Q 样点**送出。解决办法：让前面慢的处理「跑在前面」把结果囤进 FIFO，让最后一级按固定节拍从 FIFO 取样点往外送。

| 级 | 状态机 | 职责 | 产物去向 |
|----|--------|------|----------|
| ① 数据收集 | `state1` / `state11` | 从 BRAM 取字节、选比特源、扰码、卷积编码 | → `bits_enc_fifo` |
| ② 样点生成 | `state2` | 从 `bits_enc_fifo` 取比特、打孔交织、调制、插导频/DC、IFFT | → `pkt_fifo` + `CP_fifo` |
| ③ 样点转发 | `state3` | 按帧结构依次输出训练序列（ROM）和数据样点（FIFO） | → `result_i/q` |

三级之间用 FIFO 解耦：①→② 是 `bits_enc_fifo`（存编码后的 2-bit 对），②→③ 是 `pkt_fifo`（存 IFFT 后的完整符号样点）和 `CP_fifo`（存循环前缀样点）。每级只要下游 FIFO 有空位就往前处理，互不阻塞。

#### 4.2.2 核心流程

三条状态机的状态划分（localparam 原样保留，便于和源码对照）：

**FSM1（数据收集）** [源码](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v#L34-L46)：

```text
state1:  S1_WAIT_PKT ──phy_tx_start──► S1_L_SIG ──(若是 HT)──► S1_HT_SIG ──► S1_DATA
                                                              └─(若是 Legacy)──────────────► S1_DATA
state11(S1_DATA 内子状态): S11_SERVICE → S11_PSDU_DATA → S11_PSDU_CRC → S11_TAIL → S11_PAD → S11_RESET
```

`S1_L_SIG` 在 `plcp_bit_cnt==0` 时解析 BRAM 首字的 `bram_din[3:0]`（legacy 速率码）和 `bram_din[24]`（包类型 HT/Legacy）；若是 HT 包再进 `S1_HT_SIG` 解析 MCS（`bram_din[2:0]`）。这一步同时算出本包要用到的 `N_BPSC`、`N_DBPS`、`PSDU_BIT_LEN` 等速率参数。

**FSM2（样点生成）** [源码](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v#L49-L53)：

```text
state2:  S2_PUNC_INTERLV → S2_PILOT_DC_SB → S2_MOD_IFFT_INPUT → S2_RESET → (回 S2_PUNC_INTERLV 直到符号做完)
```

- `S2_PUNC_INTERLV`：从 `bits_enc_fifo` 取编码比特，按查表地址写入 `bits_ram`（打孔与交织隐式完成）。
- `S2_PILOT_DC_SB`：计算 4 个导频子载波的极性、插入 DC/边带零值。
- `S2_MOD_IFFT_INPUT`：把 56 个数据子载波按频率位置摆好、读调制值、送进 IFFT。
- 每做完一个 OFDM 符号，IFFT 输出被推进 `pkt_fifo` 与 `CP_fifo`。

**FSM3（样点转发）** [源码](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v#L56-L64)：

```text
state3:  S3_WAIT_PKT → S3_L_STF → S3_L_LTF → S3_L_SIG →
         (Legacy: → S3_DATA)
         (HT:     → S3_HT_SIG → S3_HT_STF → S3_HT_LTF → S3_DATA)
```

这一级**直接读前导 ROM**（`l_stf_rom` 等）输出训练序列，到了 `S3_L_SIG`/`S3_HT_SIG`/`S3_DATA` 才从 `pkt_fifo`/`CP_fifo` 取实时算出的样点。最后的 `result_i`/`result_q` 由一个大的多路选择器产生：前导阶段选 ROM，数据阶段选 FIFO。

#### 4.2.3 源码精读

先看三条状态机的状态声明（注意注释里写明了每个状态的含义）：

[ip/openofdm_tx/src/dot11_tx.v:L34-L64](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v#L34-L64) — 三组 `localparam`：`state1/state11`（数据收集）、`state2`（样点生成）、`state3`（样点转发）。这是阅读整份文件的「目录」。

FSM1 如何解析 BRAM 首字、决定速率。看 legacy 速率码查表（`bram_din[3:0]` → 速率/MCS），以及包类型位 `bram_din[24]`：

[ip/openofdm_tx/src/dot11_tx.v:L237-L275](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v#L237-L275) — `S1_L_SIG` 状态：`case(bram_din[3:0])` 把 4 位速率码翻译成 `N_BPSC`/`N_DBPS`/`RATE`（从 6 到 54 Mbps）；`PSDU_BIT_LEN <= {4'd0, bram_din[16:5], 3'd0}` 把字节长度左移 3 位变成比特长度；`PKT_TYPE <= bram_din[24]` 决定走 Legacy 还是 HT 分支。这段就是「BRAM 首字 = 本包发射参数表」的证据。

两个解耦 FIFO 的例化。`bits_enc_fifo` 连接 FSM1 与 FSM2：

[ip/openofdm_tx/src/dot11_tx.v:L385-L393](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v#L385-L393) — `bits_enc_fifo`（宽 2-bit、深 1024）：FSM1 的 `bits_enc`（卷积编码器输出）写入，FSM2 在 `S2_PUNC_INTERLV` 读出。`i_tvalid` 由「不在等待且未到 RESET」决定，`o_tready` 由 FSM2 的处理进度决定——典型的 AXI-Stream 握手。

最后看输出多路选择器，它把「ROM 里的前导」和「FIFO 里的数据」拼成最终样点流：

[ip/openofdm_tx/src/dot11_tx.v:L826-L828](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v#L826-L828) — `result_i`/`result_q`/`result_iq_valid` 的产生：前导状态（`S3_L_STF/L_LTF/HT_STF/HT_LTF`）选对应的 ROM 输出；`S3_L_SIG/HT_SIG/DATA` 状态按 `fifo_turn` 选 `pkt_fifo_odata` 或 `CP_fifo_odata`。`result_iq_valid` 在前导阶段恒为 1，数据阶段取对应 FIFO 的 `ovalid`。

> 三条状态机在 `phy_tx_start` 时**同时**离开 `WAIT_PKT`（见 FSM1 [L229-L235](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v#L229-L235) 与 FSM3 [L746-L751](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v#L746-L751)）。因为 FSM3 的前导来自 ROM、不依赖 FSM1/2，所以 `result_iq_valid` 几乎在 `phy_tx_start` 后立刻拉高。

#### 4.2.4 代码实践

**实践目标**：把三条状态机之间的数据流画成一张「生产者-消费者」图。

**操作步骤**：

1. 在源码里定位三个 FIFO：`bits_enc_fifo`（[L385](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v#L385)）、`pkt_fifo`（[L697](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v#L697)）、`CP_fifo`（[L680](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v#L680)）。
2. 找到每个 FIFO 的 `i_tdata`（谁写）和 `o_tready`（谁读）。

**预期结果**（参考答案）：

```text
BRAM ──►(FSM1: 选比特/扰码/卷积编码)──► bits_enc_fifo ──►(FSM2: 打孔交织/调制/IFFT)──► ┐
                                                                                        ├─►(FSM3: 按帧结构转发)──► result_i/q
                                                                                       └──► pkt_fifo / CP_fifo
前导 ROM(l_stf/l_ltf/ht_stf/ht_ltf) ──────────────────────────────────────────────────►─┘
```

**需要观察的现象**：`bits_enc_fifo` 的 `o_tready`（[L393](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v#L393)）依赖 `enc_pos` 与 `punc_info`——说明 FSM2 的读取节奏由打孔模式决定，这正是 FIFO 解耦的价值：FSM1 不用关心下游打孔节奏，只管把编码比特塞进去。

#### 4.2.5 小练习与答案

**练习 1**：为什么 FSM3 能在 FSM1/2 还没处理完数据时就开始输出？

> 答案：因为帧头是 `L-STF/L-LTF` 等训练序列，预先存在 ROM 里，FSM3 直接读 ROM 即可，不需要等 FSM1/2。FSM1/2 是在「后台」并行地把数据符号算好囤进 FIFO，等 FSM3 走到 `S3_DATA` 时再取用。

**练习 2**：`state11` 有个 `S11_PSDU_CRC` 子状态，它对应 802.11 帧的哪个部分？为什么 legacy 包有、HT 聚合包（`HT_AGGR=1`）会跳过它（见 [L326-L329](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v#L326-L329)）？

> 答案：对应 PSDU 末尾的 32-bit FCS（帧校验序列，由 [crc32_tx](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v#L132-L139) 算出）。普通包每包自带 FCS；而 A-MPDU 聚合帧里每个子帧各自带 FCS、由软件在 BRAM 里已经填好，所以硬件在 `HT_AGGR=1` 时跳过 `S11_PSDU_CRC`，直接从 `S11_PSDU_DATA` 进 `S11_TAIL`。

---

### 4.3 phy_tx_start 握手时序与 BRAM 驱动

#### 4.3.1 概念说明

这是本讲最重要的「接口行为」部分。`openofdm_tx` 与上游 `tx_intf`、下游 `xpu` 之间用三个脉冲信号握手：

| 信号 | 方向 | 含义 |
|------|------|------|
| `phy_tx_start` | 上游 → openofdm_tx | 「开始发包」的单时钟脉冲 |
| `phy_tx_started` | openofdm_tx → 上游/下游 | 「已经开始往射频口送样点了」的脉冲（用于精确的发射定时） |
| `phy_tx_done` | openofdm_tx → 上游/下游 | 「整包样点已全部送完」的电平 |

理解这条时序的关键是：**`phy_tx_start` 触发后，`result_iq_valid` 很快就有效（前导），但 `phy_tx_done` 要等所有数据符号都送完才拉高**。中间的 `phy_tx_started` 是个关键时刻标记——它告诉 MAC 层「此刻波形真的出门了」，CSMA/CA 与 ACK 定时都以此为基准。

而 BRAM 这边，`openofdm_tx` 是**地址的主设备**：它自己递增 `bram_addr` 去读 BRAM，BRAM 把对应 64-bit 字送回 `bram_din`。BRAM 的写入端在 `tx_intf` 那侧（DMA 把字节写进去）。这是一块双口 BRAM，一端写、一端读。

#### 4.3.2 核心流程

完整的一次发包时序（伪时序图，`clk` 为基带时钟，例如 200MHz）：

```text
clk          __|‾|__|‾|__|‾|__|‾|__|‾|__|‾|__|‾|__|‾|__|‾|__|‾|__|‾|__|‾|__
phy_tx_start ──┐ ‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾（单脉冲）
                │
bram_addr      0 ────────► 1 ────────► 2 ────────► ... （dot11_tx 自行递增）
bram_din       word0(MCS/len/type)  word1   word2(payload) ...

state3        WAIT_PKT → L_STF → L_LTF → L_SIG → [HT...] → DATA
result_iq_valid        ‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾‾（前导一开始就有效）
phy_tx_started          ‾|（preamble_addr==0 时脉冲一次）
                                               ... 所有样点送完
phy_tx_done                                            ────────────────（拉高并保持）
FSM3_reset                                             ────────────────（触发整核复位，准备下一包）
```

要点：
1. `phy_tx_start` 是**脉冲**，不是电平（testbench 里 `# 25 phy_tx_start = 0` 即还原）。
2. `bram_addr` 由 `dot11_tx` 内部 `reg` 驱动，FSM1 在 `S1_L_SIG` 走到 `plcp_bit_cnt==22` 时递增到 1，进入 `S1_DATA` 后按每消费 64 bit（8 字节）递增一次。
3. `phy_tx_done` 一旦在 `S3_DATA` 满足条件就置 1，同时 `FSM3_reset` 置 1；`FSM3_reset` 参与 `reset_int`，把整核拉回复位态，等待下一个 `phy_tx_start`。

#### 4.3.3 源码精读

**复位聚合**：`FSM3_reset`（发完一包后自动复位）与外部 `phy_tx_arest` 合并成内部复位 `reset_int`，所有状态机共用：

[ip/openofdm_tx/src/dot11_tx.v:L31-L32](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v#L31-L32) — `wire reset_int = phy_tx_arest | FSM3_reset;`。这就是「每发完一包自动复位」的机制来源。

**BRAM 地址驱动**：`bram_addr` 在 FSM1 里被递增。看复位时清零、`S1_L_SIG` 解析参数、`S1_DATA` 按 PSDU 比特推进：

[ip/openofdm_tx/src/dot11_tx.v:L208-L235](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v#L208-L235) — 复位时 `bram_addr<=0`；收到 `phy_tx_start` 时也 `bram_addr<=0`（从头读）；`S1_L_SIG` 在 `plcp_bit_cnt==22` 时 `bram_addr<=1`（读完首字参数，准备读第二个字）。每读满 64 位数据，地址自增的逻辑见 [L358-L359](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v#L358-L359)（`psdu_bit_cnt[5:0]==6'b111110` 即每 62…实际按 64 bit 边界推进）。

> 也就是说：`bram_din` 是被 `bram_addr`「拉」出来的——`openofdm_tx` 既是地址主设备，又是数据从设备。在 block design 里，`bram_addr` 直接连到 `tx_intf` 的 BRAM 读端口（见下方连线）。

**phy_tx_started 的产生**：在 FSM3 输出 L-STF 的第一个样点时脉冲一次：

[ip/openofdm_tx/src/dot11_tx.v:L753-L767](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v#L753-L767) — `S3_L_STF` 状态里 `if(preamble_addr==0) phy_tx_started<=1; else phy_tx_started<=0;`。注意它受 `result_iq_ready`（背压）门控（整个 FSM3 在 `else if(result_iq_ready==1)` 块里）——下游若 hold，`phy_tx_started` 也会顺延，保证它与真正送出的第一个样点对齐。

**phy_tx_done 的产生**：在 `S3_DATA` 末尾，当已送样点数达到 `nof_iq2send-2` 时拉高，并触发复位：

[ip/openofdm_tx/src/dot11_tx.v:L817-L822](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v#L817-L822) — `if((pkt_iq_sent + {2'b00, CP_iq_sent}) == nof_iq2send-2) begin phy_tx_done<=1; FSM3_reset<=1; end`。`nof_iq2send` 是本包要发的总样点数（[L635](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v#L635) 注释：`480 + 20169*80 = 1614000` 最大样点数），它在 FSM2 每生成一个符号时累加（[L659-L670](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v#L659-L670)）。

**block design 里的真实连线**（这是理解 BRAM 驱动的最直接证据）。`openofdm_tx_0` 与 `tx_intf_0` 共享一块 BRAM，地址由前者出、数据回前者：

- [ip/openwifi_ip.tcl:L311](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L311) — `openofdm_tx_0/bram_addr` → `tx_intf_0/bram_addr`（地址由 openofdm_tx 给）。
- [ip/openwifi_ip.tcl:L333](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L333) — `tx_intf_0/data_to_acc` → `openofdm_tx_0/bram_din`（数据从 BRAM 流回 openofdm_tx）。

握手与 I/Q 连线（注意 `phy_tx_started` 和 `phy_tx_done` 还同时送给 `xpu`，用于 MAC 定时）：

- [ip/openwifi_ip.tcl:L312-L314](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L312-L314) — `result_i/result_q/result_iq_valid` → `tx_intf_0/rf_i_from_acc/rf_q_from_acc/rf_iq_valid_from_acc`（I/Q 回流给 tx_intf，再送往 DAC）。
- [ip/openwifi_ip.tcl:L322-L323](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L322-L323) — `phy_tx_done` → `tx_intf_0/tx_end_from_acc` 与 `xpu_0/phy_tx_done`；`phy_tx_started` → `tx_intf_0/tx_start_from_acc` 与 `xpu_0/phy_tx_started`。
- [ip/openwifi_ip.tcl:L336-L337](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L336-L337) — `tx_intf_0/phy_tx_start` → `openofdm_tx_0/phy_tx_start`（触发）；`tx_intf_0/tx_hold` → `openofdm_tx_0/result_iq_hold`（背压）。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：阅读 `openofdm_tx.v` 与 `dot11_tx.v` 的端口与 testbench，画出从 `phy_tx_start` 到 `result_iq_valid` 的处理时序，并说明 `bram_din`/`bram_addr` 如何被驱动。

**操作步骤**：

1. **看 testbench 怎么用这个核**——这是最权威的「接口用法说明书」。打开 [dot11_tx_tb.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx_tb.v)。
   - [L21-L28](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx_tb.v#L21-L28)：声明 `Memory[0:1023]`（64-bit × 1024）并用 `$readmemh` 把一个 `.mem` 文件（HT MCS7、GI=1、8176 字节的测试包）加载进去——这就是「BRAM 内容」。
   - [L36-L38](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx_tb.v#L36-L38)：`reset=0` 后拉高 `phy_tx_start` 一个 `#25` 时长，再拉低——演示 `phy_tx_start` 是**脉冲**。
   - [L45-L58](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx_tb.v#L45-L58)：每个时钟把 `Memory[bram_addr]` 送给 `bram_din`，并在 `result_iq_valid` 有效时把 `result_i/result_q` 写入文件 `dot11_tx.txt`；检测到 `phy_tx_done` 就 `$finish`。**这一段完整再现了「核给地址、tb 给数据、核回吐 I/Q」的关系。**

2. **画出时序图**：基于上面的源码，在纸上（或工具里）画出 `clk / phy_tx_start / bram_addr / bram_din / state3 / result_iq_valid / phy_tx_started / phy_tx_done` 的时间轴，对齐关键事件。

3. **回答两个问题**（见下方预期结果）。

**预期结果**：

- **「`phy_tx_start` 到 `result_iq_valid` 的时序」**：`phy_tx_start` 上升沿后，FSM1 与 FSM3 同时启动；FSM3 立刻进入 `S3_L_STF` 并从 `l_stf_rom` 取样点，于是 `result_iq_valid` 在几个时钟内（前导输出使能后）拉高；`phy_tx_started` 在 `preamble_addr==0` 那拍脉冲一次；此后样点连续输出，直到 `S3_DATA` 末尾 `phy_tx_done` 拉高。**数据符号并不在前导时刻就绪——它们由 FSM1/2 在后台算好囤进 FIFO，等 FSM3 走到 DATA 段才被消费。**
- **「`bram_din`/`bram_addr` 如何被驱动」**：`bram_addr` 是 `dot11_tx` 内部的 `reg [9:0]` 输出，复位与 `phy_tx_start` 都把它清 0，之后由 FSM1 按消费进度自增；`bram_din` 是 64-bit 输入，其内容由 `bram_addr` 选中的 BRAM 字提供（真实硬件里这块 BRAM 的另一端口由 `tx_intf` 从 DMA 写入；testbench 里则用 `Memory[bram_addr]` 模拟）。首字（`bram_addr==0`）是本包的参数表：速率/MCS、长度、包类型、聚合、短 GI 等。

**需要观察的现象**：testbench 里 `bram_din <= Memory[bram_addr]` 是**非阻塞赋值、晚一拍**——即核给出新地址后，下一个时钟沿才拿到对应数据。这与真实 BRAM 的读延迟一致；FSM1 在解析首字时（`plcp_bit_cnt==0`）正好对应这一拍的稳定数据。

> 待本地验证：以上时序基于源码静态阅读。若要观测真实波形，可在 Vivado 里用本 IP 的 testbench 跑一次行为仿真（见 [u7-l3](u7-l3-ip-simulation-testbench.md) 的仿真实践），把 `.mem` 测试向量换成更小的 `ht_..._byte100.mem` 以缩短仿真时间，再用波形窗核对 `phy_tx_started` 与 `result_iq_valid` 的相对位置。

#### 4.3.5 小练习与答案

**练习 1**：`phy_tx_done` 和 `FSM3_reset` 为什么在同一条 `if` 里同时置 1？

> 答案：`phy_tx_done` 是通知上游「本包发完」的状态，`FSM3_reset` 则用来触发 `reset_int`（[L32](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v#L32)）把三条状态机、计数器、扰码状态全部复位，让核立刻能接收下一个 `phy_tx_start`。二者同时发生，表示「收尾即复位」。

**练习 2**：如果下游 `tx_intf` 暂时不能收样点（`result_iq_hold=1`），发射会出错吗？

> 答案：不会丢数据，只会「暂停」。`result_iq_hold=1` → `result_iq_ready=0`，FSM3 整个 `always` 块停在 `else if(result_iq_ready==1)` 之外，不推进 `state3`、不增 `pkt_iq_sent`；同时 FSM2 推 FIFO 的 `oready` 也与 `result_iq_ready` 相关，于是 FIFO 逐渐填满后 FSM2/FSM1 依次反压停住。等下游恢复，整条流水线继续。这正是用 FIFO 做背压解耦的好处。

**练习 3**：`bram_addr` 是 10 位，最多寻址 1024 个 64-bit 字（8KB）。这对应多大长度的包？

> 答案：8KB ≈ 8192 字节的 PSDU 上限。源码里 `psdu_bit_cnt` 是 19 位（[L78](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openofdm_tx/src/dot11_tx.v#L78) 注释 `65536*8 = 524288` 比特 = 65536 字节），实际受 BRAM 容量与 `nof_iq2send` 计数器位宽约束。testbench 用的最大测试向量正是 8176 字节的聚合包。

## 5. 综合实践

**任务：把 `openofdm_tx` 放回完整发射链路，画一张「从 DMA 字节到 DAC 样点」的全链路时序与数据流图，并标注每个握手信号的发起方。**

要求：

1. 复习 [u2-l2](u2-l2-openwifi-ip-hierarchy.md) 给出的发射链路：`PS→DMA→tx_intf→BRAM→openofdm_tx→tx_intf→DAC`。
2. 在图上标出本讲讲过的三件事：
   - BRAM 的「双口、地址由 openofdm_tx 出、数据回 openofdm_tx」关系（依据 [openwifi_ip.tcl:L311,L333](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L311)）。
   - 三条状态机流水线 + 两个 FIFO（`bits_enc_fifo`、`pkt_fifo`/`CP_fifo`）。
   - 三个握手信号（`phy_tx_start` 由 `tx_intf` 发起；`phy_tx_started`/`phy_tx_done` 由 `openofdm_tx` 发出，并副本送 `xpu`）。
3. 在图旁用一句话写出：**为什么 `phy_tx_started` 要同时送给 `xpu`？**（提示：CSMA/CA 与 ACK 的 SIFS 定时需要一个精确的「波形真正出门」时刻，见 [u5-l3](u5-l3-tx-control-retrans-ack.md)。）
4. 进阶（可选）：阅读 `tx_intf.v` 里 `phy_tx_start` 的产生处（`tx_bit_intf` 的 `.start(phy_tx_start)`，[tx_intf.v:L481](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_intf.v#L481)），确认是上游在「BRAM 写够、CSMA/CA 放行」后才发起这个脉冲。

> 待本地验证：第 4 步涉及 `tx_intf` 内部逻辑，结论留待 [u4-l3](u4-l3-tx-intf.md) 讲透；本讲只需在图上标注「`phy_tx_start` 来自 tx_intf」即可。

## 6. 本讲小结

- `openofdm_tx` 是发射链路「字节 → I/Q」的核心 IP；顶层 `openofdm_tx.v` 是薄壳，只例化 `dot11_tx` 核与 AXI4-Lite 寄存器从设备。
- 复位来自两处：硬件 `~phy_tx_arestn` 与软件 `slv_reg0[0]`，二者或合并；每发完一包还会被 `FSM3_reset` 自动复位一次。
- `dot11_tx` 由**三条状态机**组成流水线：FSM1（`state1/state11`）收集比特并卷积编码 → `bits_enc_fifo`；FSM2（`state2`）打孔交织调制 IFFT → `pkt_fifo`/`CP_fifo`；FSM3（`state3`）按帧结构转发训练序列（ROM）与数据样点（FIFO）到 `result_i/q`。
- 握手时序：`phy_tx_start`（脉冲）触发；前导来自 ROM，所以 `result_iq_valid` 几乎立刻有效，`phy_tx_started` 在首个 L-STF 样点脉冲；全部样点送完后 `phy_tx_done` 拉高并触发复位。
- BRAM 是双口：`bram_addr` 由 `dot11_tx` 自行驱动递增，`bram_din` 是被地址选出的 64-bit 字回读；首字即「速率/MCS/长度/包类型/聚合/短GI」参数表。
- 下游背压 `result_iq_hold` 取反为 `result_iq_ready`，经 FIFO 链逐级反压，保证不丢样点。

## 7. 下一步学习建议

- **[u4-l2 发射信号处理子模块](u4-l2-openofdm-tx-signal-processing.md)**：本讲只画了流水线骨架，下一讲逐个拆 FSM2 内部的 `convenc`（卷积编码）、`punc_interlv_lut`（打孔交织查表）、`modulation`（BPSK/QPSK/16-QAM/64-QAM）、`ifftmain`（64 点 IFFT）与前导 ROM、`crc32_tx`，看清楚「一个字节如何变成一个 OFDM 符号的 64 个频域样点」。
- **[u4-l3 tx_intf 发射接口](u4-l3-tx-intf.md)**：看上游 `tx_intf` 如何从 DMA 收字节写进 BRAM、何时发起 `phy_tx_start`、如何把 `result_i/q` 转送给 DAC，以及 `tx_status_fifo` 等调度子模块。
- **[u7-l3 IP 仿真与 testbench](u7-l3-ip-simulation-testbench.md)**：本讲的 `dot11_tx_tb.v` 是跑通本核最简单的入口，学完该讲你就能在 Vivado XSim 里亲手生成 `dot11_tx.txt` 波形文件，验证 `phy_tx_started` 与 `result_iq_valid` 的时序。
- **[u5-l3 TX 控制、重传与 ACK](u5-l3-tx-control-retrans-ack.md)**：理解本讲的 `phy_tx_started`/`phy_tx_done` 在 MAC 层 CSMA/CA 与 ACK 定时中如何被消费。
