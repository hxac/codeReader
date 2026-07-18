# rx_intf 关键子模块

## 1. 本讲目标

u3-l1 已经从顶层看过 `rx_intf.v`：它自己不做 OFDM 算法，只把若干子模块连起来，扮演接收链路的「口岸」。本讲打开这个盒子，逐个拆解它的五个关键子模块，目标是让你学完后能够：

1. 说清 `adc_intf` 如何把 AD9361 的 ADC 数据跨时钟、抽取、调增益后送进基带；
2. 说清 `rx_iq_intf` 如何把 I/Q 样点平滑地交给 `openofdm_rx`，以及它的「速率自适应」为何默认被旁路；
3. 看懂一帧接收数据如何被 `rx_intf_pl_to_m_axis`（指挥官）和 `rx_intf_m_axis`（搬运工）打包成 AXI-Stream 送往 DMA，包括两个 DMA 头部字段的含义；
4. 手动追踪 `byte_to_word_fcs_sn_insert` 如何把字节流拼成 64 bit 字，并把 FCS 校验结果与包序号塞进 DMA 头部；
5. 把 u3-l1 给出的三个高层结论（`mute_adc_out_to_bb`、`block_rx_dma_to_ps`、`rx_pkt_intr`）落实到具体子模块的具体行。

本讲是「源码精读型」讲义，阅读时建议同时打开五个 `.v` 文件对照。

## 2. 前置知识

- **跨时钟域（CDC）**：AD9361 的 ADC 采样数据在「射频时钟域」（`adc_clk`，来自 AD9361），而拼包、DMA、寄存器都在「基带时钟域」（`m00_axis_aclk`，由 PS 供给）。两个时钟异步，直接连线会采到亚稳态。本项目用 Xilinx 原语 `xpm_cdc_array_single`（打两拍同步）和 `xpm_fifo_sync`（异步 FIFO）来跨域。u2-l1 已见过 `xpm_cdc`。
- **抽取（decimate）**：降低样点速率。AD9361 数据有效信号频率高于基带所需，需要丢点把速率降到 20 Msps（`SAMPLING_RATE_MHZ`，见 board_def.v）。
- **AXI-Stream（AXI4-Stream）**：Xilinx 用来传数据流的握手协议，核心信号是 `tvalid`/`tready`（握手）、`tdata`（数据）、`tlast`（一帧最后一拍）。u2-l3 讲过它由 `axi_dma` 转成内存写入（S2MM 方向写 DDR）。
- **FCS（Frame Check Sequence）**：802.11 帧尾的 32 bit CRC32 校验，`openofdm_rx` 译码完会给出 `fcs_ok` 指示帧是否完好。
- **DMA 符号（DMA symbol）**：本仓库里一个「DMA symbol」就是一个 64 bit（8 字节）的 AXI-Stream 拍，是计量接收帧在 DDR 里占多少个 64 bit 字的单位。

如果这些概念还陌生，建议先回看 u2-l1（顶层与时钟）、u2-l3（PS-PL 互连）和 u3-l1（rx_intf 顶层总览）。

## 3. 本讲源码地图

本讲涉及的关键文件及其在接收链路里的角色：

| 文件 | 角色 | 一句话职责 |
| --- | --- | --- |
| [ip/rx_intf/src/rx_intf.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf.v) | 顶层装配 | 例化并连线下面五个子模块，做天线选择/静音/CDC |
| [ip/rx_intf/src/adc_intf.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/adc_intf.v) | ADC 接口 | ADC 数据抽取 + 数字增益 + 跨时钟到基带域 |
| [ip/rx_intf/src/rx_iq_intf.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_iq_intf.v) | I/Q 接口 | 把 I/Q 喂给 openofdm_rx（带可选 FIFO 速率自适应） |
| [ip/rx_intf/src/byte_to_word_fcs_sn_insert.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/byte_to_word_fcs_sn_insert.v) | 字节拼字 | 把 openofdm_rx 的字节流拼成 64 bit 字并插入 FCS/序号 |
| [ip/rx_intf/src/rx_intf_pl_to_m_axis.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf_pl_to_m_axis.v) | 指挥官 | 接收状态机：插 DMA 头、地址过滤、触发 DMA、产生中断 |
| [ip/rx_intf/src/rx_intf_m_axis.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf_m_axis.v) | 搬运工 | AXI-Stream 主口状态机 + FIFO，把数据推给 axi_dma |
| [ip/board_def.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/board_def.v) | 共享宏 | 采样率、`COUNT_SCALE` 等六 IP 共享常量（u2-l4 讲过） |

数据流向（自左向右）可概括为：

```
AD9361 ADC (adc_clk域)
   │  adc_data[63:0]  (4×16bit: I0/Q0/I1/Q1)
   ▼
adc_intf ── 抽取2:1 + 数字增益 + CDC ──►  ant_data_after_sel (基带域)
   │
   ▼
rx_iq_intf ──(bypass直通 / FIFO速率自适应)──► sample0/1 → openofdm_rx
                                                  │
                            openofdm_rx 译码出字节流 byte_in + byte_count
                                                  ▼
                                     byte_to_word_fcs_sn_insert
                                       拼64bit字 + 插FCS/序号
                                                  │ data_from_acc (64bit)
   ┌──────────────────────────────────────────────┘
   ▼
rx_intf_pl_to_m_axis ── 插2个DMA头部 + 地址过滤 + 触发 ──►  start_1trans
   │
   ▼
rx_intf_m_axis ── AXI-Stream + FIFO ──► m00_axis_tdata → axi_dma → DDR
                                                  │
                                          s2mm_intr 回来 → rx_pkt_intr → PS
```

下面按三个最小模块逐个拆。

## 4. 核心概念与源码讲解

### 4.1 ADC/IQ 接口：adc_intf 与 rx_iq_intf

#### 4.1.1 概念说明

AD9361 射频芯片把空中模拟信号变成数字 I/Q，经 LVDS 送进 FPGA。但这些数据：

1. 跑在 `adc_clk`（射频时钟域），和基带处理时钟不同步；
2. 有效速率比基带需要的 20 Msps 高，要抽取；
3. 幅度可能需要数字增益调整。

`adc_intf` 就负责这三件事。`rx_iq_intf` 接在它后面，负责把 I/Q 样点「匀速」地交给 `openofdm_rx`——这一点对 OFDM 接收机很关键，因为 FFT 需要均匀采样的样点。

#### 4.1.2 核心流程

`adc_intf` 的处理流水线：

```
adc_data (adc_clk域, 每拍有效)
   │
   ├─① bb_gain 经 xpm_cdc 同步到 adc_clk 域 → bb_gain_in_rf_domain
   │
   ├─② adc_valid_count 1bit翻转 → adc_valid_decimate (每两拍取一拍 = 2:1抽取)
   │
   ├─③ 在抽取拍上, 按 bb_gain 把4路I/Q各自左移0~6位 (数字增益)
   │      结果存入 adc_data_shift (仍是 adc_clk域)
   │
   └─④ adc_data_shift 经两级寄存器同步到 acc_clk(基带)域
          stage1 ← adc_data_shift; stage2 ← stage1
          data_to_bb = stage2
          data_to_bb_valid = decimate信号的上升沿(跨域后)
```

`rx_iq_intf` 则有两种工作模式，由宏 `RX_IQ_RATE_ADAPTATION_BYPASS` 切换：

- **bypass 模式（默认）**：直接把输入 I/Q 接到输出，不做任何缓冲。注释（rx_iq_intf.v:61）说明调试已经完成，I/Q 已是均匀的 20M 有效信号（zcu102 每 12 个时钟一拍）。
- **速率自适应模式（`else` 分支）**：用一个 `xpm_fifo_sync` 缓冲，根据 FIFO 里数据量动态调整读取周期 `counter_top`，让 `openofdm_rx` 尽量均匀地拿到样点。

#### 4.1.3 源码精读

**抽取计数器**：`adc_valid_count` 是 1 bit 寄存器，每来一个 `adc_data_valid` 就翻转；`adc_valid_decimate` 仅在它为 0 时为真，于是只保留「每隔一拍」的有效样点，实现 2:1 抽取。[ip/rx_intf/src/adc_intf.v:L33](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/adc_intf.v#L33)：

```verilog
assign adc_valid_decimate = (adc_valid_count==0);
```

抽取计数器本身在射频时钟域翻转（[adc_intf.v:L110-L118](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/adc_intf.v#L110-L118)），保证抽取发生在跨时钟之前。

**数字增益**：`bb_gain` 是来自寄存器 `slv_reg11[2:0]` 的 3 bit 控制字，先经 `xpm_cdc_array_single` 同步到 `adc_clk` 域（`bb_gain_in_rf_domain`），再用一个 `case` 把 4 路 I/Q 各自左移 0~6 位。[adc_intf.v:L52-L107](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/adc_intf.v#L52-L107) 里，例如 `bb_gain=3'b011`：

```verilog
3'b011 :  begin
  adc_data_shift[...0*W..1*W-1] <= {adc_data[...低位截1位], 3'd0}; // 左移3位
  ... // 4路同样处理
end
```

左移 1 位即乘 2，对应约 \(6.02\,\text{dB} \) 增益，所以 `bb_gain` 取值 0~6 对应 0~\(36\,\text{dB}\) 的数字增益（粗调）。注意实现是「砍掉最高位、低位补 0」，会饱和截断。

**跨时钟到基带域**：`adc_data_shift` 仍是 `adc_clk` 域，靠在 `acc_clk`（即 `m00_axis_aclk`）域里打两级寄存器同步（[adc_intf.v:L120-L135](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/adc_intf.v#L120-L135)）：

```verilog
adc_data_shift_stage1 <= adc_data_shift;
adc_data_shift_stage2 <= adc_data_shift_stage1;
...
adc_valid_decimate_stage2_delay <= adc_valid_decimate_stage2;
```

输出有效信号用「跨域后的 decimate 上升沿」产生（[adc_intf.v:L36](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/adc_intf.v#L36)），确保数据已经稳定：

```verilog
assign data_to_bb_valid = (adc_valid_decimate_stage2_delay==0 && adc_valid_decimate_stage2==1);
```

> 说明：这里用两级寄存器「同步」单 bit 控制信号是工程简化；多 bit 的 `adc_data_shift` 严格说也应走异步 FIFO，本设计靠「先抽取降速、再两级打拍」规避了大部分亚稳态风险，是本项目的一贯做法。

**rx_iq_intf 的 bypass**：宏在文件顶部直接定义为 1（[rx_iq_intf.v:L10](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_iq_intf.v#L10)），于是 `ifdef` 分支（[rx_iq_intf.v:L49-L59](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_iq_intf.v#L49-L59)）生效，I/Q 直通：

```verilog
`define RX_IQ_RATE_ADAPTATION_BYPASS 1
...
assign rf_i0 = bw20_i0;
assign rf_iq_valid = bw20_iq_valid;
```

**速率自适应（被旁路的分支）**：当 bypass 关闭时，`else` 分支用一个 32 深的 `xpm_fifo_sync` 缓冲（[rx_iq_intf.v:L226-L269](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_iq_intf.v#L226-L269)），并依据 FIFO 数据量 `data_count` 动态调读取周期（[rx_iq_intf.v:L159-L184](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_iq_intf.v#L159-L184)）。其中 `fractional_flag`（[rx_iq_intf.v:L156](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_iq_intf.v#L156)）判断是否存在「分数采样率」：

```verilog
assign fractional_flag = ((`NUM_CLK_PER_SAMPLE*`SAMPLING_RATE_MHZ) != `NUM_CLK_PER_US);
```

这正好对应 u2-l4 讲过的情形：zcu102 基带 240 MHz、采样率 20 MHz，\( 240/20 = 12 \) 个时钟一个样点是整数；而有些组合下 `NUM_CLK_PER_SAMPLE × SAMPLING_RATE_MHZ ≠ NUM_CLK_PER_US`，需要周期性「插一拍/扣一拍」来均匀化。

**顶层如何接这两块**：在 `rx_intf.v` 里，`adc_intf_i` 把 `adc_data_internal` 处理成 `ant_data_after_sel`（[rx_intf.v:L323-L336](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf.v#L323-L336)）；而 `adc_data_internal` 正是 u3-l1 讲过的「发射静音」逻辑产物（[rx_intf.v:L259-L260](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf.v#L259-L260)）——发射时把天线 0 低 32 bit 清零，使 `openofdm_rx` 收到全零。随后 `rx_iq_intf_i` 把 I/Q 送给接收机（[rx_intf.v:L399-L421](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf.v#L399-L421)），其输入 `bw20_i0/...` 在发射回环时还可切换成 `iq0_from_tx_intf`（[rx_intf.v:L262-L266](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf.v#L262-L266)），用于自发自收测试。

#### 4.1.4 代码实践

仓库已提供 `adc_intf` 的 testbench，可以直接用来观察抽取与跨时钟行为。

1. **实践目标**：在仿真里确认 `adc_intf` 的 2:1 抽取，并观察 `data_to_bb_valid` 的脉冲间隔。
2. **操作步骤**：
   - 阅读测试台 [ip/rx_intf/unit_test/adc_intf/adc_intf_tb.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/unit_test/adc_intf/adc_intf_tb.v)。注意它生成两个异步时钟：`adc_clk` 周期 25 ns（40 MHz，见 adc_intf_tb.v:47-49），`acc_clk` 周期 10 ns 带 3.3 ns 延迟（≈100 MHz，见 adc_intf_tb.v:42-45）；`adc_data_valid` 恒为 1（adc_intf_tb.v:63）。
   - 用 u7-l3 介绍的方式创建单 IP 工程并在 XSim 里跑 `adc_intf_tb.tcl`，观察波形里的 `data_to_bb_valid` 与 `data_to_bb`。
3. **需要观察的现象**：
   - `data_to_bb_valid` 的脉冲频率应约为 `adc_data_valid`（恒 1，每个 `adc_clk` 一拍）的一半，体现 2:1 抽取。
   - 由于跨到更快的 `acc_clk` 域并经两级同步，`data_to_bb_valid` 相对 `adc` 域的抽取指示会有固定延迟，但不会漏拍。
4. **预期结果**：每两个 `adc_clk` 有效样点对应约一次 `data_to_bb_valid` 脉冲，`data_to_bb` 随 `adc_data` 递增（testbench 里 `adc_data` 每拍 +1，adc_intf_tb.v:51-57）而阶梯式变化。
5. 若本地没有 Vivado/XSim 环境，无法运行仿真，则标注「待本地验证」——此时退而求其次，纯静态阅读 adc_intf.v:L33 与 L110-L118，口算 `adc_valid_count` 在 `adc_data_valid=1` 时的翻转序列，确认抽取比。

#### 4.1.5 小练习与答案

**练习 1**：把 `bb_gain` 设为 `3'b001` 时，单路 I/Q 的增益约为多少 dB？为什么实现上要「砍最高位」？

**答案**：左移 1 位即乘 2，约 \( 20\lg 2 \approx 6.02\,\text{dB} \)。砍最高位是因为位宽固定为 16 bit，左移后超出位宽的高位必须丢弃（饱和/截断），否则无法装回 16 bit 寄存器。

**练习 2**：如果把 `RX_IQ_RATE_ADAPTATION_BYPASS` 改成 `0`，`rx_iq_intf` 会启用哪条数据通路？它解决什么问题？

**答案**：会启用 `else` 分支的 `xpm_fifo_sync` + 动态 `counter_top` 速率自适应通路。它解决「射频前端时钟与基带时钟存在频偏/分数采样率」时，`openofdm_rx` 拿到的样点间隔不均匀、影响 FFT 的问题；靠根据 FIFO 水位调整读速度来平滑样点节奏。

---

### 4.2 m_axis DMA 输出：rx_intf_pl_to_m_axis 与 rx_intf_m_axis

#### 4.2.1 概念说明

`openofdm_rx` 译码出的字节流，最终要变成 DDR 里的一帧数据让 ARM（PS）去读。这需要两个角色配合：

- **`rx_intf_pl_to_m_axis`（指挥官）**：决定「什么时候有一帧、这一帧往 DMA 里塞什么、要不要过滤掉、DMA 完了没」。它跑一个 6 状态的接收状态机，负责插入两个 DMA 头部字、执行地址过滤、触发 DMA、并在 DMA 写完 DDR 后产生收包中断 `rx_pkt_intr`。
- **`rx_intf_m_axis（搬运工）**：执行 AXI-Stream 主口协议，内部用一个深度 8192 的 FIFO 把指挥官给的数据按拍推给 Xilinx `axi_dma`。

这是 u3-l1 三个高层结论中 `block_rx_dma_to_ps`（地址过滤）和 `rx_pkt_intr`（收包中断）的真正落点。

#### 4.2.2 核心流程

一帧接收数据的生命周期（状态机见 [rx_intf_pl_to_m_axis.v:L86-L91](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf_pl_to_m_axis.v#L86-L91)）：

```
WAIT_FOR_PKT ──sig_valid且长度合法──► DMA_HEADER0_INSERT   (塞TSF时间戳)
        ▲                                      │
        │                                      ▼
WAIT_RST_DONE ◄──复位8拍── WAIT_FILTER_FLAG ◄── DMA_HEADER1_INSERT_AND_START (塞rate/len/rssi)
        │                      │  │
        │           block=1丢弃 │  │ block=0放行
        │           (m_axis_rst)│  ▼
        │                 WAIT_DMA_TLAST ──m_axis_tlast──┘
        │                      │
        └────────超时自动恢复tlast────────┘
```

关键步骤：

1. **WAIT_FOR_PKT**：等到 `sig_valid`（包头部有效）、且非 `ht_unsupport`、且 `pkt_len` 在 `[14, max_signal_len_th]` 范围内才认为来了一个合法帧。同时按帧长算出这一帧占多少个 64 bit DMA 字：`monitor_num_dma_symbol_to_ps = ceil(pkt_len/8) + 2`（+2 是两个头部字）。
2. **DMA_HEADER0_INSERT**：输出第 0 个头部字 = 锁存的 TSF 时间戳 `tsf_val_lock_by_sig`（收到帧的时间）。
3. **DMA_HEADER1_INSERT_AND_START**：输出第 1 个头部字 = 打包好的「PHY 元数据」（速率、长度、RSSI、GPIO 状态、HT 标志、相位等）。
4. **WAIT_FILTER_FLAG**：等 `xpu` 给出的地址过滤结果。`block_rx_dma_to_ps==0` 放行 → 触发 `start_m_axis`，进 WAIT_DMA_TLAST；`block_rx_dma_to_ps==1` 丢弃 → 复位 m_axis，进 WAIT_RST_DONE。还有一条超时路径：DMA 卡死超时则伪造一个 `tlast` 自恢复。
5. **WAIT_DMA_TLAST**：等搬运工反馈 `m_axis_tlast`（最后一拍发完），回到 WAIT_FOR_PKT。
6. **rx_pkt_intr**：由一个独立进程产生——当 `axi_dma` 写完 DDR 给出 `s2mm_intr` 上升沿后，延迟若干拍拉高 `rx_pkt_intr` 一周期，告诉 PS「DDR 里有完整帧可读了」。

`rx_intf_m_axis` 这边则是个标准 AXI-Stream 主设备三状态机：`IDLE → INIT_COUNTER → SEND_STREAM → IDLE`，在 `SEND_STREAM` 里从 FIFO 读数据、按 `tvalid/tready` 握手发送，并在「读指针 == 总字数」时拉 `tlast`。

#### 4.2.3 源码精读

**状态定义**（[rx_intf_pl_to_m_axis.v:L86-L91](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf_pl_to_m_axis.v#L86-L91)）：

```verilog
localparam [2:0] WAIT_FOR_PKT = 3'b000,
                 DMA_HEADER0_INSERT = 3'b001,
                 DMA_HEADER1_INSERT_AND_START = 3'b010,
                 WAIT_FILTER_FLAG = 3'b011,
                 WAIT_DMA_TLAST = 3'b100,
                 WAIT_RST_DONE = 3'b101;
```

**DMA 字数计算**（[rx_intf_pl_to_m_axis.v:L192-L195](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf_pl_to_m_axis.v#L192-L195)）——即一帧要发多少个 64 bit 字：

```verilog
if ( sig_valid && (ht_unsupport==0) && (pkt_len>=14 && pkt_len<=max_signal_len_th) ) begin
  monitor_num_dma_symbol_to_ps <= ( pkt_len[15:3] + (pkt_len[2:0]!=0) ) + 2;
  rx_state <= DMA_HEADER0_INSERT;
end
```

`pkt_len[15:3]` 即 `pkt_len>>3`，`pkt_len[2:0]!=0` 是向上取整的修正，所以：

\[
N_{\text{dma}} = \left\lceil \frac{L_{\text{pkt}}}{8} \right\rceil + 2
\]

后缀 `+2` 就是接下来要插入的两个头部字（TSF 与 rate/len）。

**两个 DMA 头部字**：第 0 个头部字是收到帧时刻的 TSF（[rx_intf_pl_to_m_axis.v:L202](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf_pl_to_m_axis.v#L202)）；TSF 在 `sig_valid` 时被锁存（[rx_intf_pl_to_m_axis.v:L149-L160](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf_pl_to_m_axis.v#L149-L160)）。第 1 个头部字是把一堆 PHY 元数据拼进 64 bit（[rx_intf_pl_to_m_axis.v:L215](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf_pl_to_m_axis.v#L215)）：

```verilog
data_to_m_axis <= {phase_offset_taken[7:0], ht_aggr_last, ht_aggr, ht_sgi,
                   pkt_rate[7], pkt_rate[3:0], pkt_len,
                   8'd1, gpio_status_lock_by_sig_valid, 5'd0,
                   rssi_half_db_lock_by_sig_valid};
```

这个位域就是软件（openwifi 驱动）解析接收帧时读取的「PHY 头」：包含速率、长度、RSSI（半 dB）、AGC/GPIO 状态、HT 聚合与短 GI 标志、相位偏移等。注释里那句 `8'd1 ... is for pkt exist flag` 说明这一位是「帧存在」标志。这些字段在 `sig_valid` 时一并锁存（如 `rssi_half_db_lock_by_sig_valid`），保证整帧用同一组元数据。

**地址过滤（u3-l1 的 block_rx_dma_to_ps 落点）**：在 WAIT_FILTER_FLAG 里（[rx_intf_pl_to_m_axis.v:L224-L254](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf_pl_to_m_axis.v#L224-L254)）：

```verilog
if (block_rx_dma_to_ps_valid==1 && block_rx_dma_to_ps==0) begin
  start_m_axis <= 1;                 // 放行：触发DMA上报
  rx_state <= WAIT_DMA_TLAST;
end else if (block_rx_dma_to_ps_valid==1 && block_rx_dma_to_ps==1) begin
  m_axis_rst <= 1;                   // 丢弃：复位DMA通路
  rx_state <= WAIT_RST_DONE;
end
```

这正是 u3-l1 总结的「硬件层 MAC 地址过滤」：目的地址不匹配的帧在 FPGA 里就地丢弃，根本不上 DDR、不打扰 PS。

**超时自恢复**：如果 DMA 因为某种原因没有及时返回 `tlast`，`timeout_timer_1M`（由 `tsf_pulse_1M` 驱动的 1 µs 计时器）超过阈值（`m_axis_tlast_auto_recover_timeout_top`）就伪造一个 `m_axis_tlast_auto_recover` 释放 ARM 端 DMA 并复位自己（[rx_intf_pl_to_m_axis.v:L229-L234](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf_pl_to_m_axis.v#L229-L234)）。顶层把这个伪造信号 OR 到真实 `tlast`（见 rx_intf.v:247）。

**收包中断 rx_pkt_intr（u3-l1 第三个结论落点）**：独立进程（[rx_intf_pl_to_m_axis.v:L296-L304](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf_pl_to_m_axis.v#L296-L304)）：

```verilog
if ( (!rstn) || (s2mm_intr==1 && s2mm_intr_reg==0) ) begin
  count <= 0;  rx_pkt_intr <= 0;          // s2mm_intr上升沿：DMA写完DDR, 重启计数
end else begin
  count <= (count!=count_top_scale_plus1?(count+1):count);
  rx_pkt_intr <= (count==count_top_scale); // 数到top拉高一周期
end
```

这就是 u3-l1 说的「DMA 写完 DDR 后再延迟若干拍才触发中断」。延迟长度由 `count_top`（寄存器 `slv_reg13[14:0]`）经 `COUNT_SCALE` 缩放得到（[rx_intf_pl_to_m_axis.v:L181-L182](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf_pl_to_m_axis.v#L181-L182)）。`COUNT_SCALE` 在 board_def.v 定义为 `NUM_CLK_PER_US / ASSUMED_COUNTER_CLK_MHZ`（[board_def.v:L13](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/board_def.v#L13)），作用是把「软件假定的 10 MHz 计数器刻度」换算到当前基带时钟——这正是 u2-l4 讲过的跨时钟刻度标尺。

**搬运工 rx_intf_m_axis**：三状态机定义（[rx_intf_m_axis.v:L44-L46](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf_m_axis.v#L44-L46)）。关键握手信号（[rx_intf_m_axis.v:L66-L68](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf_m_axis.v#L66-L68)）：

```verilog
assign axis_tvalid = ((mst_exec_state==SEND_STREAM) &&
                      (read_pointer <= M_AXIS_NUM_DMA_SYMBOL || endless_mode) && (!EMPTY));
assign axis_tlast  = ((read_pointer == M_AXIS_NUM_DMA_SYMBOL) && tx_en) && (endless_mode==0);
assign tx_en       = ( M_AXIS_TREADY && axis_tvalid );
```

即：在 SEND_STREAM 状态、FIFO 非空、且还没发够 `M_AXIS_NUM_DMA_SYMBOL` 个字时持续发 `tvalid`；发到「读指针 == 总字数」时拉 `tlast` 标记帧尾。数据来自一个深度 `MAX_NUM_DMA_SYMBOL`（=8192）的 `xpm_fifo_sync`（[rx_intf_m_axis.v:L180-L222](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf_m_axis.v#L180-L222)）。状态机主体在 [rx_intf_m_axis.v:L88-L130](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf_m_axis.v#L88-L130)。

**两个模块的衔接**：顶层里 `rx_intf_pl_to_m_axis_i` 算好头部、过滤、触发，把 `start_1trans_from_acc_to_m_axis` 交给 `rx_intf_m_axis_i` 作为 `start_1trans`（[rx_intf.v:L440-L500](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf.v#L440-L500) 与 [rx_intf.v:L502-L524](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf.v#L502-L524)）。`start_1trans` 的来源由 `start_1trans_mode`（`slv_reg5[2:0]`）选择（[rx_intf_pl_to_m_axis.v:L115-L146](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf_pl_to_m_axis.v#L115-L146)）：正常工作用 `mode=3'b101` 跟随状态机的 `start_m_axis`；调试时也可选 `fcs_valid`、`sig_valid` 或外部触发。

#### 4.2.4 代码实践

1. **实践目标**：跟踪一帧数据在指挥官状态机里的旅程，并手算它占多少个 DMA 字。
2. **操作步骤**：
   - 假设收到一帧 `pkt_len = 100` 字节、`pkt_rate` 合法、`sig_valid` 拉高、地址过滤放行（`block_rx_dma_to_ps==0`）。
   - 对照 [rx_intf_pl_to_m_axis.v:L183-L291](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf_pl_to_m_axis.v#L183-L291) 的状态机，逐状态写出 `rx_state` 的跳变序列和每个状态输出的 `data_to_m_axis` 取值。
   - 用上面的公式算 `monitor_num_dma_symbol_to_ps`。
3. **需要观察的现象**：状态依次走 `WAIT_FOR_PKT → DMA_HEADER0_INSERT → DMA_HEADER1_INSERT_AND_START → WAIT_FILTER_FLAG → WAIT_DMA_TLAST → WAIT_FOR_PKT`；前两个状态分别输出 TSF 与 PHY 元数据头部字。
4. **预期结果**：\(\lceil 100/8 \rceil + 2 = 13 + 2 = 15\) 个 DMA 字。即这一帧在 DDR 里占 15 × 8 = 120 字节（前 16 字节是两个头部，后 104 字节里 100 字节是净荷拼出来的，剩余 4 字节含 FCS/序号等填充，见 4.3）。
5. 「待本地验证」：若想看真实波形，需要构造一个能产生 `sig_valid`/`byte_in`/`fcs_in_strobe` 的激励（本仓库未提供 rx_intf 整体的 testbench），因此静态推演是主路径。

#### 4.2.5 小练习与答案

**练习 1**：为什么收包中断不直接用 `sig_valid`（包到来），而要等 `s2mm_intr`（DMA 写完 DDR）？

**答案**：因为 PS 要读的是 DDR 里的完整帧数据。`sig_valid` 时 OFDM 还在译码、DDR 里还没数据；必须等 `axi_dma` 把整帧搬进 DDR（`s2mm_intr` 上升沿）之后，再延迟一点（确保 DMA 总线事务彻底落盘）才通知 PS，PS 此刻去读 DDR 才能拿到完整帧。

**练习 2**：`rx_intf_m_axis` 的 FIFO 深度是 8192，而 `MAX_NUM_DMA_SYMBOL` 也是 8192。小容量 FPGA 上这个深度会被谁、用什么宏改小？

**答案**：由 u1-l4/u2-l4 讲过的 `SMALL_FPGA` 宏（经 `parse_board_name.tcl` 的 `fpga_size_flag` 驱动）把 `MAX_NUM_DMA_SYMBOL` 从 8192 裁到 4096，FIFO 深度随之缩小，以适配 zynq 7020 等小容量器件。

---

### 4.3 FCS 拼字：byte_to_word_fcs_sn_insert

#### 4.3.1 概念说明

`openofdm_rx` 译码出的成果是「一个字节一个字节」的流（`byte_in` + `byte_in_strobe` + `byte_count`），并最终给出 `fcs_in_strobe` 和 `fcs_ok`。但 AXI-Stream 主口的数据宽度是 64 bit（8 字节）。`byte_to_word_fcs_sn_insert` 就负责：

1. 把字节流「8 个一组」拼成 64 bit 字；
2. 在帧尾把 FCS 校验结果（`fcs_ok`）和递增的包序号（`rx_pkt_sn`）作为一个额外的「字节」插进字节流，从而进入最后一个 DMA 字；
3. 处理帧长不是 8 整数倍时的尾部零头。

它是本讲的实践重点（对应任务规格的主实践）。

#### 4.3.2 核心流程

```
byte_in (8bit) ──┐
                 ├─► 延迟链 dly0→dly1 (2拍)
byte_in_strobe ──┘        │
                          ▼
fcs_in_strobe ? ◄── 在fcs拍, 把字节替换为 {fcs_ok, rx_pkt_sn}
                          │ byte_in_final (8bit)
                          ▼
                 byte_buf 移位寄存器: 每拍新字节进[63:56], 旧内容右移8位
                          │
                 byte_count[2:0]==7 ? ──是──► 输出一个完整64bit word
                                     ──否──► 继续累积
                          │
                 byte_count==num_byte(帧尾) ? ──是──► 处理不满8字节的余数并输出
```

序号 `rx_pkt_sn`（7 bit）是一个独立维护的计数器，由指挥官在「帧放行」时通过 `rx_pkt_sn_plus_one` 让它 +1（见 4.2.3 的 `rx_pkt_sn_plus_one` 赋值）。

#### 4.3.3 源码精读

**FCS/序号插入字节**：核心一行（[byte_to_word_fcs_sn_insert.v:L35](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/byte_to_word_fcs_sn_insert.v#L35)）：

```verilog
assign byte_in_final = (fcs_in_strobe?{fcs_ok,rx_pkt_sn}:byte_in_dly1);
```

即：在 `fcs_in_strobe` 这一拍，不再用真实数据字节，而是用 `{fcs_ok(1bit), rx_pkt_sn(7bit)}` 作为一个「合成字节」塞进流水线。注意真实数据已被延迟链 `dly1` 对齐到此刻（见下），所以这个替换不会丢数据，只是把 FCS 结果附在帧尾的数据流里。

**延迟链**：[byte_to_word_fcs_sn_insert.v:L48-L64](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/byte_to_word_fcs_sn_insert.v#L48-L64)，把 `byte_in/byte_in_strobe/byte_count` 各延迟两拍（`dly0→dly1`）。这两拍延迟是为了给「在 fcs 拍替换字节」留出时序对齐的余量——当 `fcs_in_strobe` 到来时，对应的最后一个数据字节正好走到 `dly1`。

**序号计数器**：[byte_to_word_fcs_sn_insert.v:L39-L45](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/byte_to_word_fcs_sn_insert.v#L39-L45)：

```verilog
always @(posedge clk) begin
  if (rstn_sn==1'b0) rx_pkt_sn <= 0;
  else rx_pkt_sn <= (rx_pkt_sn_plus_one?(rx_pkt_sn+1):rx_pkt_sn);
end
```

`rx_pkt_sn_plus_one` 由指挥官只在「帧放行（地址过滤通过）」时为真（[rx_intf_pl_to_m_axis.v:L113](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf_pl_to_m_axis.v#L113)），所以只有真正上报给 PS 的帧才会消耗一个序号——被地址过滤丢弃的帧序号不变。`rstn_sn` 还会在每次 `pkt_header_valid_strobe`（新帧头部到来）时复位序号逻辑（见 rx_intf.v:425-426 的接线）。

**字节拼字（移位寄存器）**：[byte_to_word_fcs_sn_insert.v:L67-L92](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/byte_to_word_fcs_sn_insert.v#L67-L92)。关键三句（L73-L76）：

```verilog
byte_buf[63:56] <= byte_in_final;          // 新字节塞到最高字节
byte_buf[55:0]  <= byte_buf[63:8];         // 旧内容整体右移8位
word_out_strobe <= (byte_count_final[2:0]==7?1:0);  // 凑满8字节才输出
word_out        <= (byte_count_final[2:0]==7 ? {byte_in_final, byte_buf[63:8]} : word_out);
```

`byte_buf` 是一个 64 bit 的移位桶：每收到一个字节就放到 `[63:56]`，原内容右移 8 位。当 `byte_count[2:0]`（低 3 位，即「当前是 8 字节组里的第几个」）等于 7 时，说明这一组 8 字节凑齐，立刻把 `{当前字节, byte_buf 高 56 位}` 拼成一个完整 64 bit 字输出。这是一种大端（big-endian）拼装：先到的字节落在高字节位。

**帧尾零头处理**：如果帧长不是 8 的倍数，最后一组不满 8 字节。代码在 `byte_count_final==num_byte`（帧的最后一个字节）时用一个 `case`（[byte_to_word_fcs_sn_insert.v:L77-L88](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/byte_to_word_fcs_sn_insert.v#L77-L88)），按 `byte_count[2:0]` 的余数把已缓存的有效字节高位对齐、低位补 0 输出：

```verilog
3'b001: begin word_out <= {56'b0,byte_buf[63:56]}; end  // 只剩1字节有效
3'b010: begin word_out <= {48'b0,byte_buf[63:48]}; end  // 剩2字节
...
```

> 注意：因为 FCS/序号「合成字节」本身也被当作数据流的一部分拼进去，所以上面那个「帧尾」实际上发生在 FCS 字节之后，软件解析时要记得 DMA 缓冲区末尾有这个 `{fcs_ok, sn}` 字节。这也解释了 4.2.4 里「120 字节里有填充」的来源。

**顶层接线**：[rx_intf.v:L423-L438](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf.v#L423-L438) 把 `openofdm_rx` 的 `byte_in/byte_in_strobe/byte_count/fcs_in_strobe/fcs_ok` 接进来，把拼好的 `data_from_acc`（64 bit）和 `data_ready_from_acc` 送给指挥官 `rx_intf_pl_to_m_axis`。

#### 4.3.4 代码实践（本讲主实践）

1. **实践目标**：手动追踪 `byte_to_word_fcs_sn_insert`，说明它如何把字节流拼成 64 bit AXI-Stream 字，并如何把 FCS 结果与序号附加到 DMA 头部（帧尾）。
2. **操作步骤**：
   - 打开 [byte_to_word_fcs_sn_insert.v:L35](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/byte_to_word_fcs_sn_insert.v#L35) 与 [L67-L92](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/byte_to_word_fcs_sn_insert.v#L67-L92)。
   - 设想一个最小输入序列：连续 8 个数据字节 `0x01,0x02,...,0x08`，`byte_count` 从 0 递增到 7，`fcs_in_strobe` 全程为 0。逐拍画出 `byte_buf` 与 `word_out_strobe`、`word_out` 的变化。
   - 再设想第 9 拍 `fcs_in_strobe=1`、`fcs_ok=1`、`rx_pkt_sn=3`，看 `byte_in_final` 被替换成什么。
3. **需要观察的现象**：
   - 前 7 拍 `word_out_strobe` 始终为 0（没凑齐 8 字节），`byte_buf` 在逐拍把新字节推到高位、旧字节右移。
   - 第 8 个字节（`byte_count[2:0]==7`）这一拍 `word_out_strobe=1`，`word_out = {0x08,0x07,0x06,0x05,0x04,0x03,0x02,0x01}`（先到的字节在高字节，大端）。
   - 第 9 拍 `byte_in_final = {1'b1, 7'd3} = 0x83`，这个「FCS/序号字节」会进入下一个拼字组的最高字节。
4. **预期结果**：每凑满 8 个字节输出一个 64 bit 字，字节顺序为大端；FCS 拍输出的合成字节为 `0x83`（`fcs_ok=1` 在 bit7，`sn=3` 在 bit6..0）。这正是软件在 DMA 缓冲区里据以判断「该帧 CRC 是否正确、这是第几个上报帧」的依据。
5. 「待本地验证」：本仓库未提供 `byte_to_word_fcs_sn_insert` 的独立 testbench，若要验证需自行写一个最小 testbench（参考 u7-l3 的文件 IO 方式，用 `$fread` 读字节向量、`$fwrite` 写回拼出的字），或借助 ILA 在真实板上抓 `word_out`/`word_out_strobe`。

#### 4.3.5 小练习与答案

**练习 1**：为什么序号 `rx_pkt_sn` 是 7 bit，而不是 8 bit？

**答案**：因为它被拼进一个 8 bit 字节 `{fcs_ok, rx_pkt_sn}`（[byte_to_word_fcs_sn_insert.v:L35](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/byte_to_word_fcs_sn_insert.v#L35)），最高位留给 `fcs_ok`，只剩 7 bit 给序号，所以序号范围是 0~127，到 128 回绕。

**练习 2**：如果一帧被地址过滤丢弃（`block_rx_dma_to_ps==1`），`rx_pkt_sn` 会 +1 吗？为什么？

**答案**：不会。`rx_pkt_sn_plus_one` 只在指挥官 WAIT_FILTER_FLAG 状态且 `block_rx_dma_to_ps_valid==1 && block_rx_dma_to_ps==0`（即放行）时才为真（[rx_intf_pl_to_m_axis.v:L113](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf_pl_to_m_axis.v#L113)）。丢弃的帧不会触发 +1，所以 PS 看到的序号是连续的「实际上报帧」计数。

**练习 3**：拼字用的是大端（先到的字节在高字节）。如果软件驱动按小端去解析 DMA 缓冲区，会发生什么？

**答案**：会读反字节序，把每 8 字节字里的字节顺序颠倒，导致 MAC 头部字段全部错位。所以软件必须知道 FPGA 是按大端拼装的，或者 DMA/IP 配置上做了字节序转换。这是软硬协同时必须对齐的契约。

---

## 5. 综合实践

把三个最小模块串起来，做一次「端到端纸面推演」：

**任务**：假设空中收到一帧合法的 802.11 数据帧，净荷 100 字节，目的地址是本机（地址过滤放行），`fcs_ok=1`，当前 `rx_pkt_sn=5`。请沿接收链路写出：

1. **adc_intf 阶段**：AD9361 的 ADC 数据经历了哪三步处理？`bb_gain=0` 时输出与输入的关系是什么？
2. **rx_iq_intf 阶段**：在默认 `RX_IQ_RATE_ADAPTATION_BYPASS=1` 下，I/Q 到 `openofdm_rx` 的通路是怎样的？
3. **byte_to_word_fcs_sn_insert 阶段**：100 字节净荷会被拼成多少个完整 64 bit 字？帧尾的 FCS/序号合成字节是什么值（二进制）？
4. **rx_intf_pl_to_m_axis 阶段**：指挥官会插几个头部字？这一帧总共占多少个 DMA 字（用 4.2 的公式验证）？地址过滤在哪一拍决定放行？
5. **rx_intf_m_axis 与中断阶段**：搬运工在哪个状态发 `tlast`？PS 何时、靠哪个信号知道 DDR 里有帧可读？

**参考答案要点**：

1. 三步：① `bb_gain` 经 CDC 同步；② 2:1 抽取（`adc_valid_count`）；③ 按 `bb_gain` 左移后在 `acc_clk` 域打两拍同步。`bb_gain=0` 时直通，`data_to_bb == adc_data`（抽取后）。
2. 直通：`rf_i0=bw20_i0` 等，`rf_iq_valid=bw20_iq_valid`，`rf_iq=0`。
3. \( \lceil 100/8 \rceil = 13 \) 个完整字（第 13 个字含 4 字节零头，按 `case` 高位对齐补 0）。FCS/序号合成字节 = `{fcs_ok=1, sn=5} = 8'b10000101 = 0x85`，它会进入第 14 个字的最高字节（即这帧净荷+FCS 实际占 14 个字）。
4. 插 2 个头部字（TSF、PHY 元数据）；总 DMA 字 = \( 13 + 2 = 15 \)（净荷部分按 4.2 的 `pkt_len` 公式，注意 `pkt_len` 是「待传字节数」，与这里手算的净荷口径一致即可，关键是用 `ceil(L/8)+2` 这个关系）；地址过滤在 WAIT_FILTER_FLAG 状态、`block_rx_dma_to_ps_valid==1` 时按 `block_rx_dma_to_ps` 决定。
5. 搬运工在 `read_pointer == M_AXIS_NUM_DMA_SYMBOL` 时发 `tlast`（SEND_STREAM 状态）；PS 在 `axi_dma` 写完 DDR 产生 `s2mm_intr` 上升沿、再经 `count_top_scale` 延迟后，靠 `rx_pkt_intr` 中断知道有帧可读。

> 说明：第 3、4 问里「净荷字数」与「DMA 总字数」的口径要细心对齐——FCS/序号字节是额外插进数据流的，会影响最后一个字的内容；而 `monitor_num_dma_symbol_to_ps` 用的 `pkt_len` 由 `openofdm_rx` 给出。精确数值「待本地验证」，重点是掌握 `ceil(L/8)+2` 这一关系与 FCS/序号的插入位置。

## 6. 本讲小结

- `adc_intf` 在射频时钟域做 **2:1 抽取 + 数字增益（左移0~6位）**，再跨到基带域，是接收链路的第一道处理；`rx_iq_intf` 默认 `BYPASS` 直通把 I/Q 交给 `openofdm_rx`，仅在分数采样率时才需要 FIFO 速率自适应。
- 接收数据出 DMA 是 **指挥官 + 搬运工** 双人配合：`rx_intf_pl_to_m_axis` 跑 6 状态机，负责插 2 个 DMA 头部字（TSF 与 PHY 元数据）、地址过滤、触发 DMA、产生收包中断；`rx_intf_m_axis` 是标准 AXI-Stream 主设备 + 8192 深 FIFO，负责按握手把数据推给 `axi_dma`。
- 一帧占的 DMA 字数满足 \( N_{\text{dma}}=\lceil L_{\text{pkt}}/8\rceil+2 \)，`+2` 就是两个头部字。
- u3-l1 的三个高层结论全部落到了具体子模块：`mute_adc_out_to_bb` → rx_intf.v:L260 的清零；`block_rx_dma_to_ps` → 指挥官 WAIT_FILTER_FLAG 的放行/丢弃；`rx_pkt_intr` → 指挥官在 `s2mm_intr` 上升沿后延迟计数产生。
- `byte_to_word_fcs_sn_insert` 用 64 bit 移位桶把字节流大端拼字，并在帧尾把 `{fcs_ok, rx_pkt_sn}` 合成字节插入数据流，使软件能在 DMA 缓冲区读到 CRC 结果与递增序号；序号只在帧放行时 +1。

## 7. 下一步学习建议

- **横向对照发射链路**：本讲的「字节拼字 + AXI-Stream 主口 + DMA」模式，在发射链路 `tx_intf` 里是反过来的（AXI-Stream 从口收数据 → BRAM）。学完 u4-l1/u4-l3 后可以对比 `tx_intf_m_axis`（从口）与本讲 `rx_intf_m_axis`（主口）的差异。
- **向上追问地址过滤的来源**：`block_rx_dma_to_ps` 来自 `xpu` 的 `pkt_filter_ctl`。建议进入 u5-l1（xpu 总览）和 u5-l4（接收包解析/过滤），看清「目的地址匹配」是如何在 MAC 头解析后给出这个 1 bit 结论的。
- **寄存器视角**：本讲反复出现的 `slv_reg5[2:0]`（start_1trans_mode）、`slv_reg3[8]`（回环选择）、`slv_reg13`（count_top）等都属于 AXI4-Lite 寄存器组，u7-l1 会专门讲 `rx_intf_s_axi.v` 的地址映射，届时可回头核对软件如何配置这些开关。
- **仿真实践**：u7-l3 会系统讲单 IP 仿真与 testbench 文件 IO 方法，届时可以用本讲的 `adc_intf_tb` 为模板，为 `byte_to_word_fcs_sn_insert` 写一个最小 testbench 来验证 4.3.4 的手推结果。
