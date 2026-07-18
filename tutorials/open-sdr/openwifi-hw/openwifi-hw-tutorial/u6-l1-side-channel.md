# side_ch 侧信道监控（CSI/RSSI/IQ 捕获与计数）

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `side_ch` 这个 IP 在 openwifi 里的定位——它是一条**与主收发链路并行、只观测不干预**的「侧信道（side channel）」。
- 看懂 `side_ch.v` 顶层如何把 5 个子模块装配成「捕获源选择 → 事件计数 / CSI·IQ 捕获 → DMA 上报」的一条流水线。
- 理解 `side_ch_counter_event_cfg.v` + `side_ch_counter.v` 如何用软件下发的选择位，把几十种原始事件归并成 6 路可计数事件，并实现「读即清零」的硬件计数器。
- 读懂 `side_ch_control.v` 里两套独立状态机：CSI/Equalizer 捕获（按帧过滤抓信道状态）与 IQ 捕获（带预触发的环形缓冲，像示波器一样抓波形）。
- 掌握 `side_ch_m_axis.v` 如何用一个 8192 深的 FIFO 和三状态机，把捕获结果按 AXI-Stream 经 DMA 送往 PS（ARM）。

本讲是 openwifi「可观测性」的核心：没有 `side_ch`，openwifi 就只是一个能收发包的黑盒；有了它，研究者才能拿到 CSI、IQ、RSSI 这些做信道测量、调试、科研的原始数据。

## 2. 前置知识

阅读本讲前，建议你已了解（对应前置讲义）：

- **PS / PL 划分与 AXI 三种形态**（u2-l3）：寄存器走 AXI4-Lite、批量数据走 AXI DMA、流式数据走 AXI-Stream。`side_ch` 同时用到这三种。
- **openwifi_ip 层级**（u2-l2）：`side_ch` 是六个自研 IP 之一，只在 UltraScale+ 的六 IP 版本里实例化；它从 `openofdm_rx`、`rx_intf`、`tx_intf`、`xpu` **旁挂**取信号。
- **xpu 低层 MAC**（u5-l1）：`side_ch` 大量复用 xpu 解析出来的 MAC 头（FC、addr1/2/3）、收发状态（`phy_tx_start/done`、`tx_control_state`、`fcs_ok`）作为捕获与计数的事件源。
- **条件编译宏**（u2-l4、u7-l2）：`HAS_SIDE_CH`、`SIDE_CH_LESS_BRAM` 这类宏在构建期由 Tcl 注入，决定 `side_ch` 是否存在、FIFO 多深。

几个本讲反复用到的术语，先一句话解释：

| 术语 | 含义 |
|---|---|
| **CSI（Channel State Information）** | 每个子载波上的信道估计复数值，反映这一帧穿过空气后的信道响应，是 Wi-Fi 信道测量/感知的「黄金数据」。 |
| **Equalizer（均衡器系数）** | `openofdm_rx` 信道均衡时算出的系数，与 CSI 同源，可抓多组用于研究。 |
| **侧信道（side channel）** | 与主数据通路并行、只「旁听」信号并做记录的附加通路，不影响正常收发。 |
| **预触发（pre-trigger）** | 像示波器那样，在触发点**之前**就持续把数据写进环形缓冲，触发后回读，从而能抓到「事件发生前」的波形。 |
| **AXI-Stream（AXIS）** | 用于流式数据的 AXI 协议，靠 `tvalid/tready` 握手、`tlast` 标记一帧末尾。 |

## 3. 本讲源码地图

本讲涉及的源码全部在 `ip/side_ch/src/` 下，是一个自包含的小 IP：

| 文件 | 角色 |
|---|---|
| [side_ch.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch.v) | **顶层**。声明全部对外端口（输入各种观测信号、3 个 AXI 接口），例化 5 个子模块并连线。整段主体被 `HAS_SIDE_CH` 宏包住。 |
| [side_ch_counter_event_cfg.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_counter_event_cfg.v) | **事件选择器**。用 6 个软件选择位，把原始事件源二选一，产出 `event0..5`。 |
| [side_ch_counter.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_counter.v) | **6 个 16 位计数器**。对 `event0..5` 上升沿计数，软件写对应寄存器即清零。 |
| [side_ch_control.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_control.v) | **捕获引擎**（本讲最重）。内含 CSI/Equalizer 捕获与 IQ 捕获两套状态机、环形缓冲、触发条件查表。 |
| [side_ch_m_axis.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_m_axis.v) | **AXI-Stream 主设备**。8192 深 FIFO + 三状态机，把捕获数据打成 AXIS 流送 DMA。 |
| [side_ch_s_axi.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_s_axi.v) | AXI4-Lite 寄存器组（`slv_reg0..31`），软件经它配置与读状态。本讲只引用，细节留 u7-l1。 |
| [dpram.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/dpram.v) | 双口 RAM 原语，IQ 捕获用它做环形缓冲。 |

数据走向一句话概括：

```
观测信号 (CSI/IQ/RSSI/MAC头/收发状态)
        │
        ├──────────────► side_ch_counter_event_cfg ──event0..5──► side_ch_counter ──► slv_reg26..31 (软件读=计数值, 写=清零)
        │
        └──────────────► side_ch_control (CSI 捕获 / IQ 捕获) ──data_to_ps──► side_ch_m_axis (8192 FIFO)
                                                                                        │
                                                                              M00_AXIS (AXI-Stream)
                                                                                        │
                                                                              AXI DMA (S2MM) ──► PS DDR
```

## 4. 核心概念与源码讲解

### 4.1 side_ch 顶层：一条只观测、不干预的并行通路

#### 4.1.1 概念说明

`side_ch` 的设计哲学很纯粹：**把主链路里所有「值得看」的信号复制一份过来，记录/计数后通过 DMA 送给 ARM，但绝不向主链路回写任何控制信号**。这样研究者和开发者可以拿到物理层最底层的数据（每个子载波的 CSI、每个样点的 I/Q、每次收发的状态跳变），而不用担心侧信道会把正常的 Wi-Fi 收发搞坏。

它有三类对外接口：

1. **一大堆输入观测信号**——来自 `openofdm_rx`、`rx_intf`、`tx_intf`、`xpu`，是它的「眼睛」。
2. **S00_AXI**——AXI4-Lite 从设备，软件通过它读写 `slv_reg0..31` 来配置捕获参数、读计数器。
3. **M00_AXIS / S00_AXIS**——两个 AXI-Stream 口。M00_AXIS 是**主设备**（PL→PS），把捕获结果经 DMA 送往 DDR；S00_AXIS 是**从设备**（PS→PL），用于回环测试。

注意一个关键事实：`side_ch` 在普通 Zynq 五 IP 版本里**不存在**，只在 UltraScale+ 六 IP 版本里实例化（详见 u2-l2）。构建期由 `ip_repo_gen.tcl` 生成的 `has_side_ch_flag.v` 决定它的去留。

#### 4.1.2 核心流程

顶层的核心流程就是「装配」：

1. `side_ch_counter_event_cfg` 吃原始事件源 + 软件选择位 → 输出 6 路事件。
2. `side_ch_counter` 对这 6 路事件上升沿计数 → 结果回填 `slv_reg26..31`。
3. `side_ch_control` 吃全部观测信号 + 捕获配置 → 产出 `data_to_ps/data_to_ps_valid` 流。
4. `side_ch_m_axis` 把该流缓存进 8192 FIFO，按软件设定的长度打成 AXIS 传输。
5. `side_ch_s_axi` 把所有 `slv_reg` 暴露给 PS。

整个主体包在 `\`ifdef HAS_SIDE_CH` … `\`else`（输出全接 0）… `\`endif` 里。换言之，若板卡不需要侧信道，这个 IP 会**编译成一个空壳**，所有输出置零，不占资源也不影响综合。

#### 4.1.3 源码精读

**条件编译决定存在与否**：主体从 [`side_ch.v:144`](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch.v#L144) 的 `\`ifdef HAS_SIDE_CH` 开始；若未定义则走 [`side_ch.v:523-539`](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch.v#L523-L539) 的 `\`else` 分支，把所有 AXI 输出接地：

```verilog
`else
assign m00_axis_tvalid = 0;
assign m00_axis_tdata = 0;
...
assign s00_axi_rvalid = 0;
`endif
```

`HAS_SIDE_CH` 由构建脚本生成。在 [boards/ip_repo_gen.tcl:25-35](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/ip_repo_gen.tcl#L25-L35) 里，`set has_side_ch 1` 默认开启，写出的 `has_side_ch_flag.v` 含 `\`define HAS_SIDE_CH 1`；想完全裁掉侧信道，把该值改 0 即可（会定义 `NO_SIDE_CH`）。

**观测信号输入（它的「眼睛」）**：端口集中在 [`side_ch.v:42-100`](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch.v#L42-L100)。按下表分类理解最清晰：

| 来源 | 信号 | 作用 |
|---|---|---|
| 射频/基带回调 | `gpio_status`、`rssi_half_db` | AD9361 的 AGC/增益状态与 RSSI |
| 时基 | `tsf_runtime_val` | 64 位 TSF 时间戳，给每条捕获打时间 |
| 接收基带 I/Q | `sample0_in/sample1_in`、`sample_in_strobe` | 来自 `rx_intf` 的 RX 样点 |
| 发射基带 I/Q | `openofdm_tx_iq0/1`、`openofdm_tx_iq_valid` | `openofdm_tx` 产出的 TX I/Q |
| 发射回送 I/Q | `tx_intf_iq0/1`、`tx_intf_iq_valid` | 送往 DAC 前的 TX I/Q（自查用） |
| OFDM 接收机 | `csi/csi_valid`、`equalizer/equalizer_valid`、`phase_offset_taken`、`ofdm_symbol_eq_out_pulse` | 信道估计/均衡结果 |
| 包检测/解析 | `long/short_preamble_detected`、`pkt_rate/pkt_len`、`pkt_header_valid/strobe`、`ht_unsupport`、`phy_type` | 收到的物理层包头信息 |
| 低层 MAC（xpu） | `FC_DI/valid`、`addr1/2/3_valid`、`pkt_for_me`、`fcs_in_strobe/fcs_ok`、`block_rx_dma_to_ps`、`ch_idle_final` | MAC 头解析与过滤结果 |
| 发射控制（xpu/tx） | `tx_control_state`、`phy_tx_start/started/done`、`tx_pkt_need_ack`、`tx_bb_is_ongoing`、`tx_rf_is_ongoing`、`tx_pkt_iq_to_dac_ongoing`、`retrans_in_progress` | 发射与重传状态机内部信号 |

可以看到，它把整条收发链路上几乎所有「状态量」都旁挂了一份。

**三个 AXI 接口**：M00_AXIS（主，PL→PS）在 [`side_ch.v:102-109`](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch.v#L102-L109)，S00_AXIS（从，PS→PL）在 [`side_ch.v:111-118`](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch.v#L111-L118)，S00_AXI（寄存器）在 [`side_ch.v:120-141`](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch.v#L120-L141)。注意 `m00_axis_*` 几个关键输出前缀了 `` `DEBUG_PREFIX ``（即 `mark_debug`，见 [`side_ch.v:9-13`](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch.v#L9-L13)），方便用 ILA 抓波形。

**FIFO 深度受宏控制**：[`side_ch.v:32-36`](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch.v#L32-L36) 里 `MAX_NUM_DMA_SYMBOL` 在 `SIDE_CH_LESS_BRAM` 时为 4096，否则 8192——小容量器件可裁 BRAM。

**实例化与连线**：5 个活动实例分别在 [`side_ch.v:220-267`](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch.v#L220-L267)（事件选择）、[`side_ch.v:269-293`](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch.v#L269-L293)（计数器）、[`side_ch.v:295-411`](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch.v#L295-L411)（捕获引擎）、[`side_ch.v:413-436`](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch.v#L413-L436)（m_axis）、[`side_ch.v:462-522`](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch.v#L462-L522)（s_axi）。注意 [`side_ch.v:438-460`](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch.v#L438-L460) 的 `side_ch_s_axis` 实例**被整段注释掉了**——所以 S00_AXIS 的回环通路当前未启用，仅保留端口。

> 提示：`side_ch` 的 `M00_AXIS` 在 block design 里接到 AXI DMA 的 S2MM（PL→PS）通道，捕获数据经 HP 端口写入 DDR；`S00_AXIS` 接到另一个 DMA 的 MM2S（PS→PL）用于回环。可在 [ip/openwifi_ip_ultra_scale.tcl:264-278](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip_ultra_scale.tcl#L264-L278) 看到 `side_ch_0` 的三个 AXI 接口如何挂到两条 DMA 与 `axi_interconnect_1` 的 `M07_AXI` 上。

#### 4.1.4 代码实践

**实践目标**：建立对 `side_ch` 端口规模与「旁挂」特性的直观认识。

**操作步骤**：

1. 打开 [`side_ch.v`](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch.v)，只看端口声明（第 41–142 行）。
2. 用三个颜色/记号分别标出：①来自 `openofdm_rx` 的信号、②来自 `xpu` 的低层 MAC 信号、③来自发射侧（`openofdm_tx`/`tx_intf`）的信号。
3. 数一数：纯输入信号大约有多少个？输出信号里，除了 AXI 接口，有没有任何一根是回送给主链路（`rx_intf`/`xpu`/`openofdm_*`）的控制信号？

**需要观察的现象 / 预期结果**：

- 输入信号非常多（几十个），输出几乎全是 AXI 口——这印证了「只观测、不干预」。
- 你应当**找不到**任何回写主链路的控制输出（`side_ch` 唯一的「输出」影响只有 DMA 上报和寄存器可读）。
- 结论：`side_ch` 是个纯「探针」IP，删掉它（`set has_side_ch 0`）不会影响 Wi-Fi 收发功能，只会让你失去观测能力。

> 待本地验证：若你手头有综合好的工程，可在 Vivado Block Design 里删除 `side_ch_0` 实例后重新综合，对比资源报告里 BRAM/UTRAM 的减少量，验证它确实独立于主链路。

#### 4.1.5 小练习与答案

**练习 1**：`side_ch.v` 顶部 `` `include "has_side_ch_flag.v" ``（连同 `fpga_scale.v`、`side_ch_pre_def.v`）在仓库里能找到这几个文件吗？为什么？

<details><summary>参考答案</summary>

**找不到**。这三个文件是构建期由 `boards/ip_repo_gen.tcl` 现场生成、再 `cp` 进 `ip/side_ch/src/` 的（见 [ip_repo_gen.tcl:25-35](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/ip_repo_gen.tcl#L25-L35) 与 [ip_repo_gen.tcl:87](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/ip_repo_gen.tcl#L87)），不进 git。只有跑过 `create_ip_repo.sh` 之后它们才存在。
</details>

**练习 2**：为什么 `side_ch` 的 M00_AXIS 相关输出前缀了 `mark_debug`（`` `DEBUG_PREFIX ``）？

<details><summary>参考答案</summary>

因为 `side_ch` 本身就是为调试/科研而生的可观测通路。给关键输出打上 `mark_debug`（配合 `SIDE_CH_ENABLE_DBG` 宏），就能在 Vivado ILA 里直接抓这些信号的片上波形，方便验证捕获与 DMA 时序是否正确。
</details>

---

### 4.2 事件计数：把几十种现象归并成 6 个可读计数器

#### 4.2.1 概念说明

做无线实验时，常需要回答「一段时间内收到了多少个数据帧、多少个 FCS 校验通过的帧、AGC 锁定了几次」这类**统计**问题。用 ARM 软件去逐包统计太慢、且会漏掉纳秒级的硬件事件。`side_ch` 用两个小模块在硬件里做了「**可配置事件 → 计数器**」：

- `side_ch_counter_event_cfg`：每路事件是一个 **2 选 1 多路器**，由软件下发 1 个选择位决定这一路数哪种现象。
- `side_ch_counter`：6 个独立的 16 位计数器，对事件**上升沿**计数；软件读对应寄存器得到计数值，**写**对应寄存器则清零。

#### 4.2.2 核心流程

```
软件写 slv_reg19 (event0..5_sel 各占 1 bit)
            │
            ▼
side_ch_counter_event_cfg: 每路 case(sel) 二选一
   event0 = sel0 ? phy_tx_start        : short_preamble_detected
   event1 = sel1 ? phy_tx_done         : long_preamble_detected
   event2 = sel2 ? rssi_above_th       : pkt_header_valid_strobe
   event3 = sel3 ? gain_change         : (hdr_strobe & hdr_valid)
   event4 = sel4 ? agc_lock            : (fcs & addr2_match & for_me & is_data)
   event5 = sel5 ? tx_pkt_need_ack     : (fcs_ok & addr2_match & for_me & is_data)
            │ event0..5 (电平信号)
            ▼
side_ch_counter: 检上升沿 → counterN++
            │
            ▼
slv_reg26..31 (软件读=计数值; 写=清零)
```

每个计数器宽度为 `COUNTER_WIDTH = 16`，即最大可累计 65535 次。

#### 4.2.3 源码精读

**事件选择器**在 [side_ch_counter_event_cfg.v:91-137](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_counter_event_cfg.v#L91-L137)。每路就是一个 `case(sel)` 两分支，例如 `event4`：

```verilog
always @( event4_sel, fcs_in_strobe, addr2_match, pkt_for_me, is_data, agc_lock)
begin
  case (event4_sel)
    0: event4 <= (((fcs_in_strobe&addr2_match)&pkt_for_me)&is_data); // 收到一个给我的数据帧
    1: event4 <= agc_lock;                                            // 或：AGC 处于锁定
  endcase
end
```

注意几个原始判定信号的来源（[side_ch_counter_event_cfg.v:73-81](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_counter_event_cfg.v#L73-L81)）：

```verilog
assign agc_lock      = gpio_status[GPIO_STATUS_WIDTH-1];                 // gpio_status 最高位=AGC锁定
assign gain_change    = (gpio_status[(GPIO_STATUS_WIDTH-2):0] != gpio_status_reg); // 增益挡位发生变化
assign rssi_above_th  = (rssi_half_db > rssi_half_db_th);                // RSSI 超过软件门限
assign addr2_match    = ({addr2[23:16],addr2[31:24],addr2[39:32],addr2[47:40]} == addr2_target); // addr2 命中
```

其中 `is_data` 在顶层由帧控制字段算得（[`side_ch.v:217-218`](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch.v#L217-L218)）：`FC_type=FC_DI[3:2]; is_data=(FC_type==2'b10)`，即 802.11 帧类型 = 2（Data）。

6 个选择位来自 `slv_reg19` 的特定位（[side_ch.v:253-258](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch.v#L253-L258)）：`event0_sel=slv_reg19[0]`、`event1_sel=slv_reg19[4]`……间隔 4 位。

**计数器**在 [side_ch_counter.v:65-137](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_counter.v#L65-L137)。清零条件很巧妙——**软件写哪一个寄存器，就清哪一个计数器**：

```verilog
assign counter0_rst = (slv_reg_wren_signal==1 && axi_awaddr_core==26); // 写 slv_reg26 → 清 counter0
...
always @(posedge clk) begin
  if (counter0_rst) begin
    counter0 <= 0; event0_reg <= 0;
  end else begin
    event0_reg <= event0;
    if (event0==1 && event0_reg==0) counter0 <= counter0 + 1; // 仅在上升沿 +1
  end
end
```

计数结果回填到 `slv_reg26..31`（[side_ch.v:287-292](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch.v#L287-L292)），所以软件读这 6 个寄存器就是 6 个计数值。`slv_reg_wren_signal` 与 `axi_awaddr_core` 由 `side_ch_s_axi` 在每次寄存器写时产生（寄存器地址高 5 位，见 [side_ch_s_axi.v:19-20](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_s_axi.v#L19-L20)）。

> 关键设计：用「上升沿」而非电平计数，是因为像 `agc_lock`、`rssi_above_th` 这类信号可能长期为高，按电平数会每个时钟都 +1。检上升沿等价于「事件发生次数」。

#### 4.2.4 代码实践

**实践目标**：学会配置一对「事件 → 计数器」来统计某种现象。

**操作步骤**（源码阅读型）：

1. 假设你要统计「**FCS 校验通过且发给我的数据帧**」数量。查 [side_ch_counter_event_cfg.v:131-137](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_counter_event_cfg.v#L131-L137)：这对应 `event5` 在 `event5_sel=0` 时的表达式。
2. 要让 `event5_sel=0`，由 [side_ch.v:258](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch.v#L258) 知 `event5_sel=slv_reg19[20]`，故软件应把 `slv_reg19` 的 bit20 写 0。
3. 计数值在 `slv_reg31`（counter5，见 [side_ch.v:292](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch.v#L292)）。开始测量前**写一次 `slv_reg31`** 清零，等若干秒后**读 `slv_reg31`** 得到累计帧数。

**预期结果**：在一次受控的抓包（例如用另一台设备发 N 个数据帧）后，读回的 `slv_reg31` 应等于实际成功接收且 FCS 正确的帧数。若同时把 `event4_sel` 设 0，`slv_reg30` 统计的是「所有给我的数据帧（不管 FCS）」，两者之差即为 CRC 错误帧数——一个简单的 PER（包错误率）测量就搭好了。

> 待本地验证：上述读写需在 openwifi 软件仓库里用驱动提供的寄存器访问接口完成；本仓库不含该软件，故标注为源码阅读型推演。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `event3_sel=1` 时数的是 `gain_change`，而它需要先寄存一拍 `gpio_status`？

<details><summary>参考答案</summary>

`gain_change = (gpio_status[6:0] != gpio_status_reg)`（[side_ch_counter_event_cfg.v:79](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_counter_event_cfg.v#L79)）需要「当前值 ≠ 上一拍值」。`gpio_status_reg` 在每个时钟沿用 `gpio_status` 更新（[side_ch_counter_event_cfg.v:83-89](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_counter_event_cfg.v#L83-L89)），从而把「电平的比较」转成「变化的脉冲」，再经计数器的上升沿检测即可统计增益调整次数。
</details>

**练习 2**：如果某事件发生频率很高，16 位计数器溢出了怎么办？

<details><summary>参考答案</summary>

`COUNTER_WIDTH=16`（最大 65535）确实可能溢出。对策：①软件周期性地读并清零（写对应 `slv_reg`），把统计窗口控制在 65535 以内；②若确需长累计，可改 `side_ch.v` 的 `COUNTER_WIDTH` 参数（[side_ch.v:39](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch.v#L39)）为 32，但要同步修改 `slv_reg` 宽度约定。
</details>

---

### 4.3 捕获引擎 side_ch_control：CSI 抓帧与 IQ 预触发抓波形

#### 4.3.1 概念说明

`side_ch_control` 是整个 IP 最复杂、也最有科研价值的模块。它做两件互斥的事，由 `iq_capture`（`slv_reg3[0]`）选择：

- **`iq_capture==0`：CSI/Equalizer 捕获**。当收到一个**满足过滤条件**（指定 FC/addr1/addr2）的帧时，把这个帧每个子载波的 CSI（以及可选的若干组均衡器系数）连同时间戳、频偏，打包成一帧送 DMA。这是 Wi-Fi 信道测量、CSI-based 感知的标准数据来源。
- **`iq_capture==1`：IQ 捕获**。选定一个 I/Q 源（RX 样点 / TX 基带 / TX 回送），用一块双口 RAM 做**环形缓冲**持续写入；一旦某个**触发条件**命中（共 32 种，例如「FCS 失败」「RSSI 上穿门限」「tx_control_state 跳到某值」），就从「触发点往前 `pre_trigger_len`」处回读 `iq_len_target` 个样点。等价于一个带预触发的示波器/逻辑分析仪。

两者的产物都汇入 `data_to_ps/data_to_ps_valid`，再由 `side_ch_m_axis` 送出。

#### 4.3.2 核心流程

**CSI 捕获状态机**（9 态，[side_ch_control.v:177-185](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_control.v#L177-L185) 的 localparam）：

```
WAIT_FOR_CONDITION  -- FC 命中?(FC_DI_valid↑ & FC==FC_target)
   ↓ pkt_len>=14
WAIT_FOR_CONDITION1 -- addr1 命中?
   ↓
WAIT_FOR_CONDITION2 -- addr2 命中?
   ↓
WAIT_FOR_CAPTURE_DONE -- 等 last_ofdm_symbol_flag(整帧解完)
   ↓
PREPARE_TO_M_AXIS    -- 检查 FIFO 够不够装得下本帧 CSI
   ↓
HEADER_TO_M_AXIS     -- 写头1: TSF 时间戳
HEADER1_TO_M_AXIS    -- 写头2: phase_offset_taken(频偏); 开始读 CSI FIFO
CSI_INFO_TO_M_AXIS   -- 按 subcarrier_mask 流式输出 56 个 CSI
   ↓ num_eq>0?
EQ_INFO_TO_M_AXIS    -- 输出 num_eq 组 equalizer (非HT补齐到52)
   ↓
(回 WAIT_FOR_CONDITION)
```

一次 CSI 传输的字数（[side_ch_control.v:290](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_control.v#L290)）：

\[
N_{\text{dma}} = \text{HEADER\_LEN} + \text{CSI\_LEN} + \text{num\_eq}\times\text{EQUALIZER\_LEN} = 2 + 56 + \text{num\_eq}\times 52
\]

**IQ 捕获状态机**（4 态，[side_ch_control.v:172-175](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_control.v#L172-L175)）：

```
(后台) 每个 iq_strobe 把 {侧信息, IQ} 写进 dpram 环形缓冲, iq_waddr++

IQ_WAIT_FOR_CONDITION -- 等 iq_trigger(由 iq_trigger_select 选 32 种之一)
   ↓ 命中: iq_raddr <= iq_waddr - pre_trigger_len; 锁存 TSF
IQ_PREPARE_TO_M_AXIS  -- 检查 FIFO 剩余空间 >= iq_len_target+1
   ↓
IQ_HEADER_TO_M_AXIS   -- 写头: 触发时刻的 TSF
IQ_INFO_TO_M_AXIS     -- 从 dpram 回读 iq_len_target 个样点输出
   ↓ (回 IQ_WAIT_FOR_CONDITION)
```

#### 4.3.3 源码精读

**I/Q 源选择**（[side_ch_control.v:300-302](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_control.v#L300-L302)）：`iq_source_select`（`slv_reg5[2:1]`）三选一——0=RX 样点、1=openofdm_tx 的 TX I/Q、2=tx_intf 回送 I/Q：

```verilog
assign iq0_inner = (iq_source_select==0?iq0:(iq_source_select==1?openofdm_tx_iq0:tx_intf_iq0));
```

**侧信息打包**（[side_ch_control.v:307-308](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_control.v#L307-L308)）：抓 IQ 时并非只存 I/Q，而是把 `rssi_half_db`、`gpio_status`、`demod_is_ongoing`、`tx_control_state`、`ch_idle_final`、`phase_offset_taken` 等 24 bit 状态**挤压进同一个 64 bit 字**（与 IQ 一起），这样回放 IQ 波形时能同步看到当时的硬件状态。两种打包格式由 `iq_capture_cfg` 选择。

**环形缓冲**（[side_ch_control.v:488-499](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_control.v#L488-L499)）就是上一节读过的 [dpram.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/dpram.v)：A 口持续写（`iq_waddr` 递增，地址自动回卷）、B 口按计算出的 `iq_raddr` 回读，实现「预触发」。

**32 种触发条件**（[side_ch_control.v:583-617](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_control.v#L583-L617)）由 `iq_trigger_select`（`slv_reg8[4:0]`）选择，摘几条典型的：

```verilog
5'd0:  iq_trigger <= (fcs_in_strobe|iq_trigger_free_run_flag); // 任意帧结束 / 自由运行
5'd1:  iq_trigger <= (fcs_in_strobe&&(fcs_ok==1));             // FCS 正确的帧
5'd2:  iq_trigger <= (fcs_in_strobe&&(fcs_ok==0));             // FCS 错误的帧 ← 抓坏包利器
5'd8:  iq_trigger <= long_preamble_detected;                    // 长前导检测
5'd10: iq_trigger <= rssi_posedge;                              // RSSI 上穿门限
5'd16: iq_trigger <= tx_control_state_hit;                      // tx 状态机跳到指定状态
5'd25: iq_trigger <= (addr2 出现 & addr1/addr2 命中);            // 指定来源的帧到达
```

其余还有 AGC 锁定/解锁边沿、增益边沿、`phy_tx_done`、`tx_bb/rf_is_ongoing` 边沿、TX IQ 幅值超限等，覆盖了收发链路上几乎所有值得抓波形的瞬间。

**IQ 回放状态机**（[side_ch_control.v:619-654](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_control.v#L619-L654)）核心两段：

```verilog
IQ_WAIT_FOR_CONDITION: if (iq_trigger) begin
    iq_raddr <= iq_waddr - pre_trigger_len;   // 关键：回拨到触发点之前
    tsf_val_lock_by_iq_trigger <= tsf_runtime_val;
    iq_state <= IQ_PREPARE_TO_M_AXIS;
end
...
IQ_INFO_TO_M_AXIS: begin
    side_info_iq <= side_info_iq_dpram;       // 回读的 IQ+侧信息
    side_info_iq_valid <= iq_strobe_inner;
    if (iq_strobe_inner) iq_raddr <= iq_raddr + 1; // 顺序往后读
    if (iq_count == iq_len_target) iq_state <= IQ_WAIT_FOR_CONDITION;
end
```

**CSI 过滤状态机**（[side_ch_control.v:740-834](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_control.v#L740-L834)）逐级核对帧头。三级过滤的写法一致，以 FC 为例（[side_ch_control.v:750-755](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_control.v#L750-L755)）：

```verilog
if ( (FC_DI_valid==1 && FC_DI_valid_reg==0) && (FC_DI[15:0]==FC_target || match_cfg[0]==0) ) begin
    if (pkt_len >= 14) begin ht_flag_capture <= ht_flag; side_ch_state <= WAIT_FOR_CONDITION1; end
end
```

`match_cfg`（`slv_reg1[15:12]`）的每位决定对应条件是否启用（1=必须匹配，0=跳过该条件），给软件灵活的过滤粒度。

**整帧结束判定**（[side_ch_control.v:429-471](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_control.v#L429-L471)）：用一个伴随状态机 `ofdm_rx_state` 累加 `num_bit_decoded`，与 `num_bit_target = 22 + pkt_len*8`（[side_ch_control.v:296](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_control.v#L296)）比较，从而给出 `last_ofdm_symbol_flag`，告诉主状态机「整帧的 CSI 都进 FIFO 了，可以开传」。每符号比特数 `N_DBPS` 按 MCS 查表（[side_ch_control.v:329-350](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_control.v#L329-L350)）。

**子载波掩码**（[side_ch_control.v:152-162](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_control.v#L152-L162)）：`SUBCARRIER_MASK`/`HT_SUBCARRIER_MASK` 标记哪些子载波有效。`CSI_INFO_TO_M_AXIS` 状态逐位旋转 `subcarrier_mask`，仅在有效位输出 `valid`（[side_ch_control.v:796-808](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_control.v#L796-L808)），把 FIFO 里 64 项压成 56 个有效 CSI。

**输出源选择**（[side_ch_control.v:313-314](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_control.v#L313-L314)）：`iq_capture` 决定把 CSI 流还是 IQ 流送到 `data_to_ps`：

```verilog
assign side_info       = (iq_capture==0?side_info_csi:side_info_iq);
assign side_info_valid = (iq_capture==0?side_info_csi_valid:side_info_iq_valid);
```

#### 4.3.4 代码实践

**实践目标**：学会配置一次「FCS 错误帧的 IQ 预触发捕获」，理解预触发回拨。

**操作步骤**（源码阅读型）：

1. 设 `iq_capture=1`（写 `slv_reg3` bit0=1）。
2. 选 I/Q 源为 RX 样点：`iq_source_select=0`（`slv_reg5[2:1]=0`）。
3. 选触发条件为「FCS 失败」：`iq_trigger_select=5'd2`（写 `slv_reg8[4:0]=2`）。
4. 设预触发长度 `pre_trigger_len`（`slv_reg11`）——例如想看坏包到达**前** 200 个样点，就写 200。
5. 设捕获总长 `iq_len_target`（`slv_reg12`）——例如 1000。
6. 让链路收到一个 CRC 错误的帧。届时 [side_ch_control.v:586](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_control.v#L586) 的 `iq_trigger` 命中，`iq_raddr` 被设为 `iq_waddr-200`，随后回读 1000 个样点送 DMA。

**需要观察的现象 / 预期结果**：

- DMA 上报的数据里，第一个字是触发时刻的 TSF 时间戳，随后是 1000 个 `{24bit 侧信息, IQ}` 字。
- 由于预触发=200，这 1000 个样点里**前 200 个发生在「帧结束(FCS 失败)」之前**——即包含了坏包的完整接收波形。这正是定位「为何 FCS 失败」（星座图、包检测、AGC 跳变）所需的现场。
- 把 `iq_capture` 切回 0，并把 `FC_target`/`addr1_target`/`addr2_target` 设为某个已知发送端，即可改为抓该发送端每帧的 CSI。

> 待本地验证：实际触发与样点对齐需结合 openwifi 软件仓库的上位机脚本解析 DMA 数据后确认；本仓库仅提供 FPGA 侧实现。

#### 4.3.5 小练习与答案

**练习 1**：CSI 捕获里，`match_cfg[0]==0` 是什么意思？为什么需要它？

<details><summary>参考答案</summary>

`match_cfg[0]` 对应 FC 过滤的「启用位」（[side_ch_control.v:750](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_control.v#L750)）。`match_cfg[0]==0` 表示「不校验 FC，任意帧控制字段都通过」。`match_cfg[1]/[2]` 同理对应 addr1/addr2。这样软件可灵活选择「只抓某种帧」「只抓某地址」「抓所有帧」。
</details>

**练习 2**：为什么 CSI 捕获要等 `last_ofdm_symbol_flag` 才开始往 DMA 传，而不是来一个子载波传一个？

<details><summary>参考答案</summary>

因为只有整帧解完，CSI FIFO 里才有完整的 56 个子载波；且要先确认这帧确实满足 FC/addr 过滤条件。提前传可能传出半帧或随后被丢弃的无效数据。`last_ofdm_symbol_flag`（[side_ch_control.v:429-471](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_control.v#L429-L471)）就是「整帧 CSI 已就绪」的同步信号。同时，传输前还在 `PREPARE_TO_M_AXIS` 检查 m_axis FIFO 剩余空间是否够（[side_ch_control.v:778-779](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_control.v#L778-L779)），不够就放弃本帧，避免溢出。
</details>

**练习 3**：`num_dma_symbol_per_trans = 2 + 56 + num_eq*52`（[side_ch_control.v:290](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_control.v#L290)）。若软件设 `num_eq=0`，一次 CSI 传输多少个 64 bit 字？

<details><summary>参考答案</summary>

\(2 + 56 + 0\times52 = 58\) 个字：1 个 TSF 时间戳 + 1 个频偏 + 56 个子载波的 CSI。这也是软件解析 DMA 缓冲区时每帧应读取的固定长度（当 `num_eq=0` 时）。
</details>

---

### 4.4 m_axis 上报：把捕获结果经 AXI-Stream 送给 DMA

#### 4.4.1 概念说明

`side_ch_control` 产出的是「零散的有效字」(`data_to_ps`/`data_to_ps_valid`)，而 PS 那边收数靠的是 AXI DMA 的 S2MM 通道——它需要标准的 AXI-Stream 握手（`tvalid/tready/tlast`）和一次确定长度的突发。`side_ch_m_axis` 就是这中间的「**搬运工 + 流量整形器**」：内部一个 8192（或 4096）深的 FIFO 做弹性缓冲，一个三状态机把「软件指定长度的一段数据」作为一个 AXIS 事务送出去。

它还向上层反馈两个状态：`m_axis_data_count`（FIFO 里还剩多少未发送，顶层接到 `slv_reg20` 供软件轮询）和 `fulln_to_pl`（FIFO 没满，告知 control 还能不能继续往里写）。

#### 4.4.2 核心流程

```
side_ch_control 不断写 data_to_ps/valid ──► xpm_fifo_sync (深度 MAX_NUM_DMA_SYMBOL)
                                                 │ (din/wr_en)
                                                 ▼
                                              m_axis_data_count ──► slv_reg20 (软件可读)
                                              fulln_to_pl ──► control (反压)

触发一次传输 (m_axis_start_1trans 脉冲):
   IDLE ──init_txn_pulse──► INIT_COUNTER ──► SEND_STREAM
                                  │
   SEND_STREAM: 边读 FIFO 边按 AXIS 输出, read_pointer 从 0 数到 M_AXIS_NUM_DMA_SYMBOL
                                  │ read_pointer==M_AXIS_NUM_DMA_SYMBOL 且 tx_en → axis_tlast
                                  ▼ tx_done
                                 IDLE
```

`m_axis_start_1trans` 有三种产生方式（`m_axis_start_mode`，`slv_reg1[1:0]`，[side_ch_control.v:851-883](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_control.v#L851-L883)）：00=回环、01=**自动触发**（软件写 `slv_reg2` 设长度时自动启动，最常用）、10=外部触发、11=停用。

#### 4.4.3 源码精读

**自动触发的产生**（[side_ch_control.v:291-292](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_control.v#L291-L292)）：检测「软件写 `slv_reg2`」的上升沿：

```verilog
assign num_dma_symbol_reg_wr_is_onging = (slv_reg_wren_signal==1 && axi_awaddr_core==2); // 写 slv_reg2
assign m_axis_start_auto_trigger = (num_dma_symbol_reg_wr_is_onging_reg==1 && ..._reg1==0); // 上升沿
```

这就是「软件写一次传输长度 = 踢一次 DMA」的握手约定。

**三状态机**（[side_ch_m_axis.v:88-131](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_m_axis.v#L88-L131)）：`IDLE → INIT_COUNTER → SEND_STREAM`。启动靠 `init_txn_pulse`（[side_ch_m_axis.v:70](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_m_axis.v#L70)），它是 `m_axis_start_1trans` 的上升沿检测：

```verilog
assign init_txn_pulse = (!init_txn_ff) && m_axis_start_1trans;
```

**AXIS 信号**（[side_ch_m_axis.v:61-68](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_m_axis.v#L61-L68)）：

```verilog
assign M_AXIS_TVALID = ((mst_exec_state==SEND_STREAM) && (read_pointer<=M_AXIS_NUM_DMA_SYMBOL || m_axis_endless_mode) && (!EMPTY));
assign axis_tlast     = ((read_pointer==M_AXIS_NUM_DMA_SYMBOL) && tx_en) && (m_axis_endless_mode==0);
assign tx_en          = (M_AXIS_TREADY && axis_tvalid);
```

`M_AXIS_NUM_DMA_SYMBOL` 来自 `slv_reg2`（顶层 [`side_ch.v:420`](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch.v#L420) 减 1）。`m_axis_endless_mode`（`slv_reg1[4]`）置 1 时变成「永不打 tlast」的连续流模式，适合自由运行的 IQ 流式采集。

**读指针与完成**（[side_ch_m_axis.v:147-167](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_m_axis.v#L147-L167)）：每成功握手一次（`tx_en`）`read_pointer+1`，到达 `M_AXIS_NUM_DMA_SYMBOL+1` 时 `tx_done=1`，状态机回 `IDLE`。

**FIFO**（[side_ch_m_axis.v:181-223](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_m_axis.v#L181-L223)）：Xilinx 原语 `xpm_fifo_sync`，读写同宽 64 bit、深度 `MAX_NUM_DMA_SYMBOL`、FWFT（首字直读）模式。`rd_data_count` 即 `m_axis_data_count`，回送给顶层 `slv_reg20`（[side_ch.v:215](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch.v#L215)）。

#### 4.4.4 代码实践

**实践目标**：理清「软件取一次数据」的完整寄存器握手。

**操作步骤**（源码阅读型，画出时序）：

1. 轮询读 `slv_reg20`（`m_axis_data_count`），等到累积的捕获字数 ≥ 你想要的一次取数长度 N。
2. 把要取的字数写进 `slv_reg2`（即 `M_AXIS_NUM_DMA_SYMBOL = N-1`）。**这一次写会触发 `m_axis_start_auto_trigger`**（见 4.4.3）。
3. `side_ch_m_axis` 进入 `SEND_STREAM`，DMA 的 S2MM 通道从 `M00_AXIS` 把 N 个字搬进 DDR。
4. 软件等 DMA 完成中断（在 PS 侧），即可在 DDR 缓冲区解析这 N 个字（CSI 或 IQ）。

**需要观察的现象 / 预期结果**：

- 写 `slv_reg2` 之后，`slv_reg20`（FIFO 余量）会随之下降 N；下降到接近 0 说明本次取数完成。
- 若 `slv_reg20` 长期不涨，说明 `side_ch_control` 没在写（捕获条件没命中或 `m_axis_start_mode` 不是 01）；若涨到接近 `MAX_NUM_DMA_SYMBOL` 还不取，会触发 `fulln_to_pl=0` 反压，control 侧 `PREPARE_TO_M_AXIS` 会放弃当前帧。

> 待本地验证：DMA 完成中断与 DDR 缓冲区的实际地址，由 openwifi 软件仓库的 DMA 描述符配置决定，超出本仓库范围。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `M_AXIS_NUM_DMA_SYMBOL` 在顶层要减 1（[`side_ch.v:420`](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch.v#L420)）？

<details><summary>参考答案</summary>

因为内部用「`read_pointer <= M_AXIS_NUM_DMA_SYMBOL`」作为继续发送的条件、「`read_pointer == M_AXIS_NUM_DMA_SYMBOL`」作为打 `tlast` 的条件（[side_ch_m_axis.v:66-67](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_m_axis.v#L66-L67)）。指针从 0 开始计，要发 N 个字，比较值应为 N-1。所以软件写 `slv_reg2 = N` 时，硬件实际比较 N-1，正好发 N 个字。
</details>

**练习 2**：`fulln_to_pl` 这个「非满」信号回送给 `side_ch_control` 起什么作用？

<details><summary>参考答案</summary>

它是 m_axis FIFO 给 control 的**反压**信号。control 在 `PREPARE_TO_M_AXIS`（CSI，[side_ch_control.v:778-779](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_control.v#L778-L779)）和 `IQ_PREPARE_TO_M_AXIS`（[side_ch_control.v:631-633](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch_control.v#L631-L633)）里，会先比较「FIFO 剩余空间 ≥ 本次要写的字数」，不够就**丢弃本帧、回等待态**，而不是硬写导致 FIFO 溢出丢数据。这把流量整形与背压耦合在一起。
</details>

---

## 5. 综合实践：搭一个「CSI + PER」联合测量

把本讲三个最小模块串起来，设计一个真实可用的测量流程：

**场景**：你想对某个发射端做信道测量，同时统计包错误率（PER）。

**配置步骤**（对应寄存器，均为源码阅读型推演）：

1. **设捕获模式为 CSI**：`slv_reg3[0]=0`（`iq_capture=0`）。
2. **设过滤条件**：把目标发射端的 MAC 写进 `slv_reg7`（`addr2_target`），`slv_reg1[15:12]`（`match_cfg`）设为只校验 addr2（即 bit2=1，其余=0）。
3. **设 CSI 长度**：`slv_reg4[3:0]`（`num_eq`）按需设（只要 CSI 设 0；想顺带抓均衡器设 1~15）。
4. **设上报模式**：`slv_reg1[1:0]=2'b01`（自动触发模式）。
5. **配 PER 计数器**：
   - `slv_reg19[20]=0` → `event5` 数「FCS 正确且给我的数据帧」，清一次 `slv_reg31`。
   - `slv_reg19[12]=0` → `event4` 数「给我的数据帧（不限 FCS）」，清一次 `slv_reg30`。
6. **运行**：让发射端连续发 1000 个数据帧。
7. **取 CSI**：周期性轮询 `slv_reg20`，每当 ≥ (2+56) 就写 `slv_reg2=2+56` 触发一次 DMA，在 PS 侧解析出每帧 56 子载波 CSI。
8. **算 PER**：测量结束后读 `slv_reg30`（总给我的数据帧）与 `slv_reg31`（其中 FCS 正确的），

\[
\text{PER} = 1 - \frac{\text{slv\_reg31}}{\text{slv\_reg30}}
\]

**预期结果**：你同时拿到了每个收到帧的 CSI（用于信道分析）和整段的 PER（用于链路质量评估），全程由硬件完成事件计数与数据搬运，ARM 仅做配置与解析——这正是 `side_ch` 的设计目的。

> 待本地验证：步骤 7、8 的寄存器访问与 DMA 解析需在 openwifi 软件仓库中实现；本仓库只提供 FPGA 侧逻辑。若暂无硬件，可只做步骤 1–5 的「配置表推演」，并对照本讲引用的源码行号核对每一位配置的含义。

## 6. 本讲小结

- `side_ch` 是 openwifi 的**并行观测通路**：旁挂主收发链路的几十个信号，只记录/计数、不回控，删掉它不影响 Wi-Fi 功能。
- 顶层 [side_ch.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/side_ch/src/side_ch.v) 装配 5 个子模块（事件选择、计数器、捕获引擎、m_axis、s_axi），整体被 `HAS_SIDE_CH` 宏包住，宏由 `ip_repo_gen.tcl` 在构建期生成。
- **事件计数** = `side_ch_counter_event_cfg`（6 路 2 选 1，由 `slv_reg19` 选择）+ `side_ch_counter`（6 个 16 位计数器，检上升沿，写 `slv_reg26..31` 清零）。
- **捕获引擎** `side_ch_control` 两套状态机：CSI 抓帧（按 FC/addr1/addr2 过滤，输出「TSF+频偏+56 CSI+可选均衡器」）与 IQ 预触发抓波形（dpram 环形缓冲 + 32 种触发条件 + 预触发回拨）。
- **上报** `side_ch_m_axis` 用 8192 深 FIFO + 三状态机把捕获数据打成 AXI-Stream，软件「写 `slv_reg2` 设长度」即自动触发一次 DMA 传输，余量可由 `slv_reg20` 轮询、由 `fulln_to_pl` 反压。
- 数据经 `M00_AXIS` → AXI DMA（S2MM）→ DDR 送 PS；`S00_AXIS` 留作回环测试（实例当前注释禁用）。

## 7. 下一步学习建议

- **寄存器映射全貌**：本讲只用到 `slv_reg0/1/2/3/4/5/6/7/8/9/10/11/12/19/20/21/22/26..31` 的含义，完整的 AXI4-Lite 读写握手与地址译码在 `side_ch_s_axi.v`，建议结合 **u7-l1（AXI 寄存器映射）** 系统学习，并把本讲的 slv_reg 用法对照那张映射表。
- **ILA 调试**：本讲多次出现 `mark_debug`（`SIDE_CH_ENABLE_DBG`）。想亲手抓 `side_ch` 内部波形，看 **u7-l6（GPIO/LED 调试、ILA 与 ENABLE_DBG）**，了解如何开宏、插探针、在 Vivado 里看片上波形。
- **信号来源的上游**：CSI/均衡器/前导检测来自 `openofdm_rx`（**u3-l3**），MAC 头与收发状态来自 `xpu`（**u5 单元**）。若想搞清某个被捕获信号的确切含义，回头读这些上游讲义。
- **条件编译与裁剪**：`HAS_SIDE_CH`、`SIDE_CH_LESS_BRAM`、`SIDE_CH_ENABLE_DBG` 的注入链路在 **u7-l2（条件编译与 Verilog 宏体系）**，想给 `side_ch` 加自定义宏或裁 FIFO 深度可参考。
