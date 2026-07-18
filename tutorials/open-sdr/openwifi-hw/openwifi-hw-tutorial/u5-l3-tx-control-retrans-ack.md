# TX 控制、重传与 ACK

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `tx_control` 模块在 openwifi 低层 MAC（xpu）里承担的职责：它既是「收到单播帧后回送 ACK/CTS/Block Ack」的发送方，又是「自己发完帧后等对方 ACK、超时则重传」的接收方。
- 画出 `tx_control_state` 七个状态的含义与跳转关系。
- 看懂「发 ACK」分支（`PREP_ACK → SEND_DFL_ACK / SEND_BLK_ACK`）如何按 SIFS 节拍把确认帧写进 TX BRAM。
- 看懂「等 ACK」分支（`RECV_ACK_WAIT_TX_BB_DONE → RECV_ACK_WAIT_SIG_VALID → RECV_ACK`）如何在超时后决定重传还是放弃，以及 `retrans_limit`、`quit_retrans`、`tx_status` 的作用。
- 理解聚合帧（A-MPDU / Block Ack）与普通 ACK 在状态机里的差异。

本讲只聚焦 `tx_control.v` 这一个状态机模块，以及它在 `xpu.v` 中如何被连线、被软件寄存器驱动。

## 2. 前置知识

本讲假设你已经读过：

- **u5-l1（xpu 总览）**：知道 `xpu` 是装配体，算法下沉到子模块；`tx_control` 是其中之一；`backoff_done`、`retrans_in_progress`、`slice_en` 是三大指挥信号。
- **u5-l2（CSMA/CA）**：知道 `csma_ca` 产出 `backoff_done`（赢得信道）、消费 `retrans_trigger`（重传要重新退避）。
- **u4（发射链路）**：知道 `phy_tx_start / phy_tx_started / phy_tx_done` 是 openofdm_tx 的握手信号，待发字节放在双口 BRAM 里。

还需要一点 802.11 协议常识（用大白话解释）：

- **ACK（确认帧）**：收端收到一个需要确认的单播帧后，必须在一个极短的间隔（SIFS）内回一个 ACK，发端收到 ACK 才认为这一帧成功。否则发端会**重传**。
- **SIFS（Short Interframe Space）**：802.11 里最短的帧间间隔，ACK/CTS 必须在 SIFS 后发出，以「插队」方式抢占信道。
- **RTS/CTS**：发数据前先发一个短的 RTS（请求发送），对方回 CTS（允许发送），用来预约信道、避免长帧冲突。收到 RTS 要回 CTS，逻辑和「收到数据回 ACK」很像。
- **重传与 retry 位**：重发的帧在帧头 Frame Control 里把 `retry` 位置 1，告诉对方「这是重传」。
- **A-MPDU 与 Block Ack**：把多个 MPDU（子帧）聚合一次发出叫 A-MPDU；收端不再逐帧回 ACK，而是回一个 **Block Ack**，里面带一张 bitmap，标明哪些序号的子帧已正确收到。这样可以一次确认多个帧，提升效率。

关键术语对照：`phy_tx_done`（一帧基带 I/Q 全部送完）、`sig_valid`（收到了对方帧的 SIGNAL 头，能读出长度/速率）、`fcs_valid`（帧尾校验通过）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [ip/xpu/src/tx_control.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v) | 本讲主角。一个 7 状态的有限状态机，负责发 ACK/CTS/Block Ack，以及等 ACK、超时重传、终止。 |
| [ip/xpu/src/xpu.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v) | 例化 `tx_control` 的父模块，把软件寄存器（`slv_reg9/11/16/17/18…`）、`csma_ca`、`tx_intf`、`phy_tx` 的信号接到 `tx_control` 端口上。 |

注意：`tx_control.v` 顶部 `include "clock_speed.v"` 与 `board_def.v`（[tx_control.v:L2-L3](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L2-L3)），所以它直接用 `NUM_CLK_PER_US`、`COUNT_SCALE` 等宏（含义见 u2-l4）。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：先定位接口（4.1），再总览状态机（4.2），然后分别讲「发 ACK」分支（4.3）和「等 ACK / 重传」分支（4.4）。

### 4.1 模块定位与对外接口

#### 4.1.1 概念说明

`tx_control` 是发射侧的低层 MAC 调度器，但它**自己不生成 I/Q 样点**。它只做两件事：

1. **作为 ACK 发送方**：当 xpu 解析出一个「需要我确认」的接收帧（数据帧、管理帧、RTS、BlockAckReq、PS-Poll、或 A-MPDU 的最后一帧），它就在 SIFS 之后把 ACK/CTS/Block Ack 的**帧内容写进 TX BRAM**，再通知发射核发出去。
2. **作为 ACK 接收方**：当它自己发完一个 `tx_pkt_need_ack=1` 的帧，就进入等 ACK 流程；在规定窗口内收到 ACK 则成功，超时则按 `retrans_limit` 决定重传或放弃。

因此它的核心输出大多是「控制脉冲」与「BRAM 写端口」：`start_tx_ack`（触发发 ACK）、`start_retrans`（触发重传）、`wea/addra/dina`（写 TX BRAM）、`tx_try_complete`（本次发送尝试结束）、`tx_status`（回读状态）。

#### 4.1.2 核心流程

两条独立工作流共用同一个状态机：

```text
【发 ACK 流】 收到需确认帧 ──> IDLE 判定 ──> PREP_ACK(等SIFS,算duration/bitmap)
                                       └──> SEND_DFL_ACK 或 SEND_BLK_ACK(写BRAM)
                                       └──> phy_tx_done ──> IDLE

【等 ACK 流】 自己发完need_ack帧 ──> RECV_ACK_WAIT_TX_BB_DONE(预置retry位)
                                 ──> RECV_ACK_WAIT_SIG_VALID(等SIGNAL头)
                                 ──> RECV_ACK(等整帧+FCS)
                                 ──> 成功: tx_try_complete / 超时: 重传或放弃
```

#### 4.1.3 源码精读

模块端口声明在 [tx_control.v:L15-L90](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L15-L90)。按用途可分六组（这也是本讲代码实践的产出）：

- **复位与时钟**：`clk`、`rstn`（在 xpu 里是 `s00_axi_aresetn & (~slv_reg0[5])`，即软件写 `slv_reg0[5]` 可单独复位 `tx_control`，见 [xpu.v:L543](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L543)）。
- **定时参数**：`preamble_sig_time`、`ofdm_symbol_time`、`sifs_time`、`send_ack_wait_top`、`recv_ack_timeout_top_adj`、`recv_ack_sig_valid_timeout_top` 等，全部由 xpu 依频段 `band` 从软件寄存器算出（见 4.3/4.4）。
- **重传控制**：`max_num_retrans`、`tx_pkt_retrans_limit`、`tx_pkt_need_ack`、`tx_ht_aggr`、`quit_retrans`、`backoff_done`。
- **接收帧解析输入**：`FC_type`、`FC_subtype`、`FC_more_frag`、`duration`、`addr1/addr2`、`self_mac_addr`、`signal_rate/len`、`fcs_valid/fcs_in_strobe`、`sig_valid`，以及 Block Ack 相关的 `blk_ack_req_*`、`blk_ack_resp_*`、`qos_tid`。
- **发送核反馈**：`phy_tx_done`、`pulse_tx_bb_end`、`tx_core_is_ongoing`、`bram_addr`、`douta`（BRAM 回读）。
- **输出**：`tx_control_state_out`、`ack_cts_is_ongoing`、`retrans_in_progress`、`start_retrans`、`start_tx_ack`、`retrans_trigger`、`tx_try_complete`、`tx_status`、`ack_tx_flag`、以及 BRAM 写端口 `wea/addra/dina`。

重传次数上限是理解全模块的钥匙，它由一行组合逻辑决定：

```verilog
assign retrans_limit = (max_num_retrans[3]?max_num_retrans[2:0]:tx_pkt_retrans_limit);
```

出处 [tx_control.v:L165](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L165)。含义：`max_num_retrans` 来自软件寄存器 `slv_reg11[3:0]`（[xpu.v:L387](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L387)）。当它的 bit3=1 时，用全局上限 `max_num_retrans[2:0]`（0~7）；bit3=0 时，用**每帧自带**的 `tx_pkt_retrans_limit`（由上层随包下发）。也就是说 bit3 是「用全局还是用每帧上限」的开关。

另一个常被引用的输出是 `ack_cts_is_ongoing`，它告诉 xpu「当前正在发 ACK/CTS」，用于发射期静音自收（见 [xpu.v:L348](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L348) 的 `mute_adc_out_to_bb`）：

```verilog
assign ack_cts_is_ongoing = ((tx_control_state==PREP_ACK) || (tx_control_state==SEND_DFL_ACK) || (tx_control_state==SEND_BLK_ACK));
```

出处 [tx_control.v:L176](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L176)。

#### 4.1.4 代码实践：端口分组整理

1. **实践目标**：建立「`tx_control` 通过哪些信号与外界打交道」的全景。
2. **操作步骤**：打开 [tx_control.v:L25-L90](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L25-L90)，按本节列出的六组把每个端口归类。
3. **观察现象**：你会发现大量端口前缀是 `` `DEBUG_PREFIX ``（即 `(*mark_debug*)`），它们只在 `XPU_ENABLE_DBG` 宏打开时才真正连进 ILA 调试；非调试构建里它们是普通信号。
4. **预期结果**：整理出一张「端口 → 数据流向（来自/去往哪个模块）」的表。
5. 待本地验证（无需运行，纯阅读）。

#### 4.1.5 小练习与答案

**练习**：`tx_control` 既负责发 ACK 又负责等 ACK，会不会出现「我自己正在发 ACK，却同时把自己当成等 ACK 的状态」这种冲突？

**参考答案**：不会。状态机是互斥的——发 ACK 走 `PREP_ACK/SEND_*_ACK` 三态，等 ACK 走 `RECV_ACK_*` 三态，二者不会同时处于活动。而且 IDLE 里触发等 ACK 的条件是 `phy_tx_done && cts_toself_bb_is_ongoing==0`（[tx_control.v:L356](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L356)），发 ACK 产生的 `phy_tx_done` 不会回到 IDLE 触发等 ACK（ACK 帧本身不需要再被确认）。

---

### 4.2 七状态 TX 状态机

#### 4.2.1 概念说明

整个模块就是一个大的 `case (tx_control_state)` 状态机（[tx_control.v:L278](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L278)），共 7 个状态，编码为 4 bit。可以把它们分成三组：

- **IDLE（空闲/仲裁）**：唯一同时处理「收到的帧要不要回 ACK」和「自己的帧发完后要不要等 ACK」的枢纽。
- **发 ACK 组**：`PREP_ACK` → `SEND_DFL_ACK` / `SEND_BLK_ACK`。
- **等 ACK 组**：`RECV_ACK_WAIT_TX_BB_DONE` → `RECV_ACK_WAIT_SIG_VALID` → `RECV_ACK`。

#### 4.2.2 核心流程

状态跳转全景图（箭头上的标注是触发条件）：

```text
                        ┌────────── 收到需确认帧(且非聚合末帧) ──────────┐
                        │                                                  ▼
        ┌── phy_tx_done(need_ack) ──┐   IDLE ──收到A-MPDU末帧/BlockAckReq──> PREP_ACK
        │                           │     │                                     │
        │                           ▼     │                                     │ 等到SIFS
RECV_ACK_WAIT_TX_BB_DONE <─── (置retrans_in_progress)                          ▼
        │                           ▲     │                              SEND_DFL_ACK ─┐
        │ pulse_tx_bb_end           │     │                              SEND_BLK_ACK ─┤(写BRAM)
        ▼                           │     │                                     │        │
RECV_ACK_WAIT_SIG_VALID             │     │                                     └─phy_tx_done→IDLE
        │                           │     │
   sig_valid(len14/32)              │     │ 自己发完帧(need_ack=0) / quit_retrans
        ▼                           │     │ / 重传到达上限 / 等到ACK
RECV_ACK ──成功/超时─────────────────┴─────┘
                                       ▲
        backoff_done & retrans_in_progress & !retrans_started ─→ start_retrans(重发,仍回IDLE等下次)
```

要点：**所有分支最终都回到 IDLE**。重传不是在一个单独的「重传态」里完成，而是：超时后置 `retrans_trigger=1`、保持 `retrans_in_progress=1`、回到 IDLE；等 `csma_ca` 重新退避完成给出 `backoff_done`，IDLE 再发 `start_retrans` 让 `tx_intf` 重发缓冲帧。

#### 4.2.3 源码精读

七个状态的编码定义：

```verilog
localparam [3:0]    IDLE =                     4'b0000,
                    PREP_ACK=                  4'b0001,
                    SEND_DFL_ACK=              4'b0010,
                    SEND_BLK_ACK=              4'b0011,
                    RECV_ACK_WAIT_TX_BB_DONE = 4'b0100,
                    RECV_ACK_WAIT_SIG_VALID =  4'b0101,
                    RECV_ACK  =                4'b0110;
```

出处 [tx_control.v:L92-L98](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L92-L98)。`tx_control_state_out` 把这 4 bit 直接对外暴露（[L162](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L162)），软件/ILA 可以直接读当前态。

还有一个对软件很重要的派生信号 `tx_control_state_idle`：

```verilog
assign tx_control_state_idle =((tx_control_state==IDLE) && (~retrans_started));
```

出处 [tx_control.v:L163](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L163)。注意它不是简单「在 IDLE」，而是「在 IDLE 且没有挂起的重传」——如果一帧正在等退避重发（`retrans_started=1`），即使 `tx_control_state==IDLE`，这个指示也是 0，告诉上层「我还忙着，别塞新东西」。

#### 4.2.4 代码实践：列出状态及含义

1. **实践目标**：把 7 个状态、编码、含义写成一张速查表（这也是本讲主实践任务的第一步）。
2. **操作步骤**：读 [tx_control.v:L92-L98](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L92-L98)，再对照 4.2.2 的跳转图标注每个状态在做什么。
3. **观察现象**：`RECV_ACK_WAIT_TX_BB_DONE`、`RECV_ACK_WAIT_SIG_VALID`、`RECV_ACK` 三个状态名直接揭示了「等 ACK」的三段式等待结构。
4. **预期结果**（参考答案）：

| 状态 | 编码 | 含义 |
| --- | --- | --- |
| `IDLE` | 0000 | 枢纽态：判定收到的帧要不要回 ACK；判定自己发完的帧要不要等 ACK；响应 `backoff_done` 触发重发 |
| `PREP_ACK` | 0001 | 准备回 ACK：等 SIFS、计算 duration、为 Block Ack 构建 bitmap |
| `SEND_DFL_ACK` | 0010 | 把普通 ACK/CTS 帧内容写进 TX BRAM |
| `SEND_BLK_ACK` | 0011 | 把 Block Ack 帧内容写进 TX BRAM |
| `RECV_ACK_WAIT_TX_BB_DONE` | 0100 | 自己发完帧后，先把该帧 retry 位预置进 BRAM，等基带发完(`pulse_tx_bb_end`) |
| `RECV_ACK_WAIT_SIG_VALID` | 0101 | 等对方回帧的 SIGNAL 头(`sig_valid`)，据此设定整帧等待窗口 |
| `RECV_ACK` | 0110 | 在窗口内等完整 ACK/Block Ack 并校验，成功或超时后回 IDLE |

5. 待本地验证（纯阅读）。

#### 4.2.5 小练习与答案

**练习**：为什么 `tx_control_state_idle` 要额外乘上 `~retrans_started`，而不是直接等于 `tx_control_state==IDLE`？

**参考答案**：因为重传是「回到 IDLE 等 `backoff_done`」实现的。超时后 `retrans_in_progress=1`、`retrans_started` 仍可能为 0，此时状态机停在 IDLE 但实际上还欠一次重发。若把它当成空闲，上层可能塞入新帧造成冲突。乘 `~retrans_started` 确保「真的没事干」才报空闲。

---

### 4.3 收到帧后回送 ACK / Block ACK

#### 4.3.1 概念说明

当一个发给本机（`addr1 == self_mac_addr`）且 FCS 正确的帧到达，按 802.11 规定，本机要在 SIFS 内回一个确认帧：

- 普通数据/管理帧 → 回 **ACK**。
- RTS → 回 **CTS**。
- A-MPDU 的最后一帧 / BlockAckReq → 回 **Block Ack**（带 bitmap）。

`tx_control` 在 IDLE 里识别「该不该回 ACK」，进 `PREP_ACK` 等 SIFS 并算好 duration/bitmap，再进 `SEND_DFL_ACK` 或 `SEND_BLK_ACK` 把帧内容逐字写进 TX BRAM（由发射核按 `bram_addr` 来读），发完（`phy_tx_done`）回 IDLE。

#### 4.3.2 核心流程

```text
IDLE:
  收到帧 && fcs_valid && 是需确认类型 && addr1==self
     ├─ 是A-MPDU子帧: 在 blk_ack_bitmap_mem 里记下序号; 等最后一帧
     └─ 最后一帧或普通帧: 锁存 send_ack_wait_top_scale, 进 PREP_ACK
PREP_ACK:
  计数到 send_ack_wait_top_scale_lock (≈SIFS)
  计算 duration_new (回ACK后还应保留信道多久)
  构建 blk_ack_bitmap_lock (若是Block Ack)
  start_tx_ack=1, 进 SEND_DFL_ACK 或 SEND_BLK_ACK
SEND_DFL_ACK / SEND_BLK_ACK:
  按 bram_addr 把帧头/地址/duration/bitmap 写进 BRAM (dina)
  phy_tx_done → IDLE
```

#### 4.3.3 源码精读

**判定回 ACK 的条件**在 IDLE 里（节选关键分支）：

```verilog
else if ( fcs_valid && ((is_data&&(~is_qosdata))||(is_qosdata&&(~^qos_ack_policy))||is_management
                        ||is_blockackreq||is_pspoll||(is_rts&&(!cts_torts_disable)))
                       && (self_mac_addr==addr1)) // send ACK will not back to this IDLE until the last IQ sample sent.
```

出处 [tx_control.v:L336-L353](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L336-L353)。这里的 `is_data`/`is_rts` 等都是依据 Frame Control 字段的组合逻辑（[tx_control.v:L167-L174](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L167-L174)），例如 `is_ack` 还要求 `signal_len==14`（ACK 帧长度恒为 14）。对 QoS 数据帧，只有当 `qos_ack_policy` 要求确认（`~^qos_ack_policy`，即两位异或为 0）时才回 ACK。

`PREP_ACK` 里先把回送类型和 duration 算好，然后**用一个计数器等 SIFS**：

```verilog
ack_timeout_count <= ( ( ack_timeout_count != send_ack_wait_top_scale_lock )?(ack_timeout_count + 1):ack_timeout_count );
tx_control_state  <= ( ( ack_timeout_count != send_ack_wait_top_scale_lock )?tx_control_state
                       :((rx_ht_aggr_last_flag||is_blockackreq_received) ? SEND_BLK_ACK : SEND_DFL_ACK) );
start_tx_ack <= ( ( ack_timeout_count != send_ack_wait_top_scale_lock )? 0:1);
```

出处 [tx_control.v:L455-L457](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L455-L457)。`send_ack_wait_top_scale_lock` 是 SIFS 对应的时钟数（见 4.3.4 的换算），计满后一次性给出 `start_tx_ack` 脉冲并跳到发送态。

duration 的计算体现 802.11 规则（注释也写明了标准出处）：

```verilog
FC_subtype_new <= (is_rts_received?(4'b1100):(4'b1101)); // 1100=CTS, 1101=ACK
...
if ( ((is_data_received||is_management_received)&&(FC_more_frag_received==1)) || is_rts_received ) begin
    duration_new<= duration_extra+((duration_standard<=0)?0:duration_standard);
end else begin
    duration_new<=duration_extra+0;  // 非分片帧: ACK 的 duration 置 0
end
```

出处 [tx_control.v:L442-L452](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L442-L452)。`duration_standard` 在每拍算出（[L264](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L264)）= `duration_received - ackcts_time - sifs_time`，即「原帧 duration 减去一个 ACK+SIFS 的时长」，用于分片帧或 RTS 的 NAV 续期。

`SEND_DFL_ACK` 里按 `bram_addr` 把帧拼进 BRAM（节选）：

```verilog
if(bram_addr==0) begin
    dina<={32'h0, 14'd0, ackcts_signal_parity, ackcts_signal_len, 1'b0, ackcts_rate};
end else if(bram_addr==2) begin
    dina<={ack_addr[31:0], duration_new, 8'd0, FC_subtype_new, FC_type_new, 2'd0};
end else if(bram_addr==3) begin
    dina<={48'h0,ack_addr[47:32]};
end
```

出处 [tx_control.v:L461-L476](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L461-L476)。这里 `ackcts_signal_len=14`（[L178](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L178)），`ackcts_rate` 来自软件配置 `cts_torts_rate`（[L259](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L259)）。Block Ack 的写法类似但多了 bitmap 字（[tx_control.v:L478-L498](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L478-L498)）。

**Block Ack bitmap 的构建**发生在 `PREP_ACK`（聚合帧场景）：每收到一个正确的 A-MPDU 子帧，就在 `blk_ack_bitmap_mem` 里把对应序号位置 1（[tx_control.v:L319](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L319) 与 [L348](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L348)）；回送时在 `PREP_ACK` 里循环把 mem 搬到 `blk_ack_bitmap_lock`（[tx_control.v:L420-L437](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L420-L437)）。

#### 4.3.4 代码实践：理解 SIFS 等待的换算

1. **实践目标**：看懂 `send_ack_wait_top` 这个软件寄存器如何被换算成 `tx_control` 内部的等待时钟数。
2. **操作步骤**：
   - 在 xpu 里找 `send_ack_wait_top` 的来源：[xpu.v:L339](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L339) `assign send_ack_wait_top = (band==1?slv_reg18[14:0]:slv_reg18[30:16]);`（2.4GHz 与 5GHz 各占一半，因为两频段 SIFS 不同）。
   - 在 tx_control 里看换算：[tx_control.v:L271](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L271)
     `send_ack_wait_top_scale <= ((send_ack_wait_top-relative_decoding_latency)*COUNT_SCALE);`
3. **观察现象**：等待计数 = (SIFS 目标值 − 解码延迟补偿) × `COUNT_SCALE`。`COUNT_SCALE` 把「软件 10MHz 参考」刻度换算成 FPGA 时钟数（u2-l4）；`relative_decoding_latency` 是个用 0.1µs 分辨率表示的解码延迟补偿（[xpu.v:L337-L338](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L337-L338)），让 ACK 在对方「刚解完上一帧」的时机发出，又不早于 SIFS。
4. **预期结果**：能说清「软件写 `slv_reg18` → 按频段选位 → 减去解码补偿 → 乘 COUNT_SCALE → 作为 PREP_ACK 的等待终点」这条链路。
5. 待本地验证（纯阅读 + 计算）。

#### 4.3.5 小练习与答案

**练习 1**：收到 RTS 时回的是什么帧？代码里如何区分？

**参考答案**：回 CTS。在 [tx_control.v:L442](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L442)，`FC_subtype_new <= (is_rts_received?(4'b1100):(4'b1101));`——`is_rts_received` 为真时 subtype=4'b1100（CTS），否则 4'b1101（ACK）。

**练习 2**：为什么普通非分片数据帧回 ACK 时 `duration_new` 直接置 0？

**参考答案**：见 [tx_control.v:L450-L452](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L450-L452) 与其引用的标准注释：非分片帧的最后一帧不需要再为后续帧预约信道，ACK 的 duration/ID 应置 0；只有分片帧（`FC_more_frag==1`）或 RTS 才需要把剩余 NAV 续到 ACK 的 duration 里。

---

### 4.4 发完帧后等 ACK 与重传决策

#### 4.4.1 概念说明

这是本讲的另一半，也是重传机制的核心。当本机发完一个 `tx_pkt_need_ack=1` 的帧（注意 xpu 里它被 AND 了 `~ack_rx_disable`：[xpu.v:L550](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L550)），`tx_control` 进入三段式等 ACK：

1. **`RECV_ACK_WAIT_TX_BB_DONE`**：先把该帧的 retry 位预置进 BRAM（为可能的 重传做准备），等基带真正把 I/Q 送完（`pulse_tx_bb_end`）。
2. **`RECV_ACK_WAIT_SIG_VALID`**：等对方回帧的 SIGNAL 头（`sig_valid` 且长度为 14=ACK 或 32=Block Ack），据此设定整帧等待窗口 `recv_ack_timeout_top`；若信号头迟迟不来则超时。
3. **`RECV_ACK`**：在窗口内等完整 ACK 并校验地址/类型/FCS；成功则结束，超时则进入「重传 or 放弃」决策。

决策依据是 `retrans_limit`（见 4.1.3）：已重传次数 `num_retrans` 达到上限、或上限为 0，就放弃（`tx_try_complete`）；否则 `num_retrans++` 并置 `retrans_trigger`，让 `csma_ca` 重新退避，退避完成后由 IDLE 发 `start_retrans` 真正重发。

#### 4.4.2 核心流程

```text
IDLE ─(phy_tx_done, tx_pkt_need_ack)→ RECV_ACK_WAIT_TX_BB_DONE
        retrans_in_progress<=1; addra<=2; (计数到2时)把BRAM第2字的retry位置1
        ─(pulse_tx_bb_end)→ RECV_ACK_WAIT_SIG_VALID

RECV_ACK_WAIT_SIG_VALID:
   计 ack_timeout_count;
   if 超时(count==recv_ack_sig_valid_timeout_top_scale):
       ──> IDLE + 决策: (num_retrans==limit || limit==0)? 放弃 : (num_retrans++; retrans_trigger=1)
   else if sig_valid && len∈{14,32}:
       设 recv_ack_timeout_top (len14→24us; len32→48us 的时钟数 + 微调)
       ──> RECV_ACK

RECV_ACK:
   计 ack_timeout_count;
   if 窗口内收到 (普通ACK: is_ack&&(fcs_disable|fcs_in_strobe)) 或 (BlockAck: fcs_valid&&is_blockackresp) && addr1==self:
       ──> IDLE; tx_try_complete=1; 锁存 tx_status; num_retrans=0; retrans_in_progress=0  (成功)
   else if count==recv_ack_timeout_top:
       ──> IDLE + 决策(同上: 放弃 或 num_retrans++ & retrans_trigger)  (超时)

(回到IDLE后)若 retrans_trigger 已置位且 retrans_in_progress=1:
   csma_ca 重新退避 ──backoff_done──> IDLE 发 start_retrans (重发), retrans_started=1
```

#### 4.4.3 源码精读

**进入等 ACK**：IDLE 里 `phy_tx_done` 分支（节选）：

```verilog
else if ( phy_tx_done && cts_toself_bb_is_ongoing==0 ) // phy_tx_done must be from high layer
  begin
      retrans_started<=0;
      if (tx_pkt_need_ack==1) begin
          tx_control_state<= RECV_ACK_WAIT_TX_BB_DONE;
          addra<=2;
          tx_try_complete<=0;
          retrans_in_progress<=1;
      end else begin
          tx_try_complete<=1;   // 不需要ACK的帧: 直接完成
          num_retrans_lock <= num_retrans;
          ...
          retrans_in_progress<=0;
      end
  end
```

出处 [tx_control.v:L356-L375](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L356-L375)。`cts_toself_bb_is_ongoing==0` 排除 CTS-to-self（它后面跟自己的数据，不需要 ACK）。

**预置 retry 位**（`RECV_ACK_WAIT_TX_BB_DONE`）：用一个小计数器 `tx_dpram_op_counter` 在第 2 拍把 BRAM 回读字的 bit11（`FC_retry`）写回为 1，且只对非聚合帧做：

```verilog
if(tx_ht_aggr==0 && tx_dpram_op_counter==2) begin
    wea <= 1;
    dina <= {douta[63:12], 1'b1, douta[10:0]};   // 置 bit11 = retry
end
```

出处 [tx_control.v:L506-L513](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L506-L513)。这样一旦后续判定要重传，重发的帧天然带 retry 位。等 `pulse_tx_bb_end`（基带发完）即转下一态（[L524-L526](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L524-L526)）。

**等 SIGNAL 头并设窗口**（`RECV_ACK_WAIT_SIG_VALID`）：

```verilog
if ( (ack_timeout_count<recv_ack_sig_valid_timeout_top_scale) && sig_valid && (signal_len==14||signal_len==32) ) begin
    tx_control_state<= RECV_ACK;
    if(signal_len==14)
       recv_ack_timeout_top <= (({4'd6, 2'd0})*`NUM_CLK_PER_US)+recv_ack_timeout_top_adj_scale;   // ACK: 24us
    else if(signal_len==32)
       recv_ack_timeout_top <= (({4'd12,2'd0})*`NUM_CLK_PER_US)+recv_ack_timeout_top_adj_scale;   // BlockAck: 48us
end else if ( ack_timeout_count==recv_ack_sig_valid_timeout_top_scale ) begin // sig valid timeout
    tx_control_state<= IDLE;
    ... 决策(放弃 或 num_retrans++ & retrans_trigger) ...
end
```

出处 [tx_control.v:L543-L573](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L543-L573)。`{4'd6,2'd0}` 是 6 左移两位 = 24（6 个 OFDM 符号 × 4µs = 24µs，对应 6Mbps 的 ACK 时长），乘 `NUM_CLK_PER_US` 换算成时钟数。

**成功判定**（`RECV_ACK`）——本讲主实践任务的核心：

```verilog
if ( (ack_timeout_count<recv_ack_timeout_top)
     && (((recv_ack_fcs_valid_disable|fcs_in_strobe) && is_ack) || (fcs_valid && is_blockackresp))
     && (self_mac_addr==addr1)) begin   // 窗口内收到合格 ACK
    tx_control_state<= IDLE;
    tx_try_complete<=1;
    num_retrans_lock <= num_retrans;
    if (is_blockackresp) begin
        blk_ack_resp_ssn_lock <= blk_ack_resp_ssn;
        blk_ack_bitmap_lock <= blk_ack_resp_bitmap;
    end else begin
        blk_ack_resp_ssn_lock <= 0; blk_ack_bitmap_lock <= 1;  // 普通ACK: bitmap记为1(成功)
    end
    num_retrans<=0;
    retrans_in_progress<=0;
end else if ( ack_timeout_count==recv_ack_timeout_top ) begin// 超时
    tx_control_state<= IDLE;
    if  ((num_retrans==retrans_limit) || (retrans_limit==0)) begin // 已达上限/不允许重传
        tx_try_complete<=1; ... retrans_in_progress<=0;            // 放弃
    end else begin
        num_retrans<=num_retrans+1; retrans_trigger<=1;            // 触发重传
    end
end
```

出处 [tx_control.v:L597-L629](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L597-L629)。注意 L596 的注释：「检测到一个普通 ACK 帧就足以确认该业务；但聚合业务必须收到合法的 Block Ack 响应」。所以普通 ACK 不强求 FCS 通过（`is_ack && (recv_ack_fcs_valid_disable|fcs_in_strobe)`），而 Block Ack 必须满足 `fcs_valid && is_blockackresp`。

**状态回读**：每次尝试结束（成功或放弃）都会锁存 `tx_status`，供软件读：

```verilog
tx_status <= {blk_ack_bitmap_lock, blk_ack_resp_ssn_lock, num_retrans_lock};
```

出处 [tx_control.v:L266](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L266)。这是 64+12+4 = 80 bit，告诉上层「这次重传了几次、Block Ack 的起始序号与 bitmap」。

**软件强制终止**：`quit_retrans` 是软件可以拉起的「放弃重传」信号，在 IDLE 里优先处理：

```verilog
else if ((quit_retrans == 1) && (retrans_in_progress == 1)) begin
    tx_try_complete<=1; num_retrans_lock <= num_retrans;
    num_retrans<=0; retrans_in_progress<=0; retrans_started<=0;
end
```

出处 [tx_control.v:L376-L385](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L376-L385)。它也被 `backoff_done` 分支再次检查（[L386-L400](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L386-L400)），确保即使退避已完成、即将重发的瞬间，软件仍能喊停。

**重发触发**：当 `retrans_in_progress=1` 且 `csma_ca` 重新退避完成给出 `backoff_done`，IDLE 发 `start_retrans`：

```verilog
else if ((backoff_done==1) && (retrans_in_progress==1) && (retrans_started==0)) begin
    if(quit_retrans) begin ... 放弃 ... end
    else begin
        start_retrans <= 1 ;
        retrans_started <= 1;
    end
end
```

出处 [tx_control.v:L386-L400](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L386-L400)。`start_retrans` 与 `retrans_trigger` 的分工：`retrans_trigger` 是「超时后请求重传」，喂给 `csma_ca`（[xpu.v:L509](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L509)）和 `cw_exp`（[xpu.v:L533](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L533)，用于增大竞争窗）；`start_retrans` 是「退避完成后真正让 `tx_intf` 重发缓冲帧」。

#### 4.4.4 代码实践：收到 ACK 与 ACK 超时的对比（本讲主实践）

1. **实践目标**：用自己的话讲清状态机在这两种情形下分别怎么走（这是规格里要求的核心任务）。
2. **操作步骤**：
   - 打开 [tx_control.v:L585-L640](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L585-L640)（`RECV_ACK` 态）。
   - 分别标注「成功分支」（L597-L611）与「超时分支」（L612-L629）。
   - 再看 [tx_control.v:L386-L400](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L386-L400)，理解超时后「重发」是如何在 IDLE 里被 `backoff_done` 兑现的。
3. **观察现象**：两个分支都先回 IDLE；区别在 `num_retrans` 是否清零、`retrans_in_progress` 是否清零、是否置 `retrans_trigger`、是否置 `tx_try_complete`。
4. **预期结果**（参考答案）：

| 情形 | 状态机动作 | 关键信号 |
| --- | --- | --- |
| **窗口内收到合格 ACK** | `RECV_ACK → IDLE`；本次尝试成功结束 | `tx_try_complete=1`；`num_retrans_lock<=num_retrans` 后 `num_retrans=0`；`retrans_in_progress=0`；锁存 Block Ack 的 ssn/bitmap（普通 ACK 则 bitmap=1） |
| **ACK 超时（未收到）** | `RECV_ACK → IDLE`；按 `retrans_limit` 决策 | 若已达上限或上限为 0：`tx_try_complete=1`、`retrans_in_progress=0`（放弃）；否则 `num_retrans++`、`retrans_trigger=1`，保持 `retrans_in_progress=1`，等 `csma_ca` 退避完成后由 IDLE 的 `backoff_done` 分支发 `start_retrans` 重发 |

5. 待本地验证（纯阅读）。

#### 4.4.5 小练习与答案

**练习 1**：`retrans_trigger` 和 `start_retrans` 都是「重传相关」的输出，它们有什么区别？

**参考答案**：`retrans_trigger`（[L571/L627](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L567-L572)）在**超时瞬间**置位，作用是通知 `csma_ca`（重新退避）和 `cw_exp`（增大竞争窗）——它表示「需要重传，请先排队等信道」。`start_retrans`（[L397](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L397)）在**退避完成、赢得信道后**才发出，作用是通知 `tx_intf`「现在把缓冲帧重发出去」。一个是「请求」，一个是「执行」。

**练习 2**：如果软件把 `max_num_retrans` 设成 `4'b1000`（bit3=1，低 3 位为 0），会发生什么？

**参考答案**：由 [tx_control.v:L165](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L165)，`retrans_limit = max_num_retrans[2:0] = 0`。在超时决策里 `(num_retrans==retrans_limit) || (retrans_limit==0)` 恒真，所以**一超时就立即放弃，不重传**。这等价于「关闭重传」。

---

## 5. 综合实践

**任务**：用一张完整的时序图，把「本机发一个需要 ACK 的数据帧、第一次对方没回 ACK、第二次重传成功」的全过程串起来，标注 `tx_control` 在每个阶段处于哪个状态、哪些输出信号被置位。

建议步骤：

1. 从 [tx_control.v:L356-L375](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L356-L375) 出发：`phy_tx_done` → `RECV_ACK_WAIT_TX_BB_DONE`，标出 `retrans_in_progress=1`、retry 位预置（[L506-L513](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L506-L513)）。
2. 经 `RECV_ACK_WAIT_SIG_VALID`（[L533-L583](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L533-L583)）到 `RECV_ACK`（[L585-L640](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L585-L640)），第一次 `ack_timeout_count==recv_ack_timeout_top`：标出 `num_retrans: 0→1`、`retrans_trigger=1`、回 IDLE。
3. 在 IDLE 标出 `csma_ca` 退避完成后 `backoff_done` → `start_retrans=1`（[L386-L400](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L386-L400)），帧被 `tx_intf` 重发。
4. 第二次重走 `RECV_ACK_*` 链，这次在 `RECV_ACK` 成功分支（[L597-L611](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L597-L611)）命中：标出 `tx_try_complete=1`、`num_retrans_lock=1`、`tx_status` 更新、`num_retrans=0`、`retrans_in_progress=0`。
5. 最后对照 [xpu.v:L537-L610](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L537-L610)，在图上注明 `retrans_trigger` 喂给了 `csma_ca`/`cw_exp`，`start_retrans`/`tx_status`/`tx_try_complete` 去往 `tx_intf`，体会「控制中枢 xpu 把多个子模块串成一次完整的发送尝试」。

如果手头有 Vivado 工程，可进一步：用 `XPU_ENABLE_DBG` 宏重新打包 xpu（见 u7-l2/u7-l6），把 `tx_control_state`、`num_retrans`、`retrans_trigger`、`backoff_done`、`start_retrans` 接入 ILA，触发一次丢包重传，对照你画的时序图验证。这一步**待本地验证**。

## 6. 本讲小结

- `tx_control` 是发射侧低层 MAC 的调度状态机，身兼两职：**收到需确认帧后回送 ACK/CTS/Block Ack**，以及**自己发完帧后等 ACK、超时重传**。
- 它只有 7 个状态：枢纽 `IDLE`；发 ACK 组 `PREP_ACK → SEND_DFL_ACK/SEND_BLK_ACK`；等 ACK 组 `RECV_ACK_WAIT_TX_BB_DONE → RECV_ACK_WAIT_SIG_VALID → RECV_ACK`（[L92-L98](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L92-L98)）。
- 发 ACK 分支用 `send_ack_wait_top`（按频段从 `slv_reg18` 取、减解码补偿、乘 `COUNT_SCALE`）实现 SIFS 等待，再按 `bram_addr` 把确认帧写进 TX BRAM。
- 等 ACK 三段式：先预置 retry 位，再等 SIGNAL 头设窗口（ACK=24µs、Block Ack=48µs 量级），再在窗口内校验整帧；普通 ACK 只需检测到 ACK 帧，Block Ack 必须通过 FCS。
- 重传决策由 `retrans_limit`（`max_num_retrans[3]` 选全局/每帧上限）掌控：达上限或上限为 0 即放弃（`tx_try_complete`），否则 `num_retrans++` 并 `retrans_trigger`；真正的重发由 IDLE 在 `backoff_done` 时发 `start_retrans`。
- 软件可通过 `quit_retrans` 强制终止重传、通过 `tx_status`（`{bitmap, ssn, num_retrans}`，80bit）回读每次尝试的结果；`slv_reg0[5]` 可单独复位本模块。

## 7. 下一步学习建议

- **u5-l4（TSF 与接收包解析/过滤）**：本讲多次用到 `addr1/addr2`、`FC_type/subtype`、`duration`、`SC_seq_num`，这些字段都由 `phy_rx_parse.v` 从字节流里解析出来；读完后你会理解 `tx_control` 的输入是怎么来的。
- **u5-l5（CCA/RSSI/SPI）**：`ack_cts_is_ongoing`、`tx_rf_is_ongoing` 如何参与静音与 CCA，可补全「发 ACK 期间射频侧发生了什么」。
- **u7-l1（AXI 寄存器映射）**：本讲的 `slv_reg9/11/16/17/18/19/26` 等都在 `xpu_s_axi.v` 里有地址映射；想从软件侧（openwifi 驱动）调 SIFS、重传上限、ACK 超时窗口，就去看那一讲。
- **源码延伸**：如果想看「`start_retrans` 之后 `tx_intf` 如何重发缓冲帧」，可结合 u4-l3 的 `tx_intf.v` 与 `tx_bit_intf` 的 BRAM 写状态机一起读。
