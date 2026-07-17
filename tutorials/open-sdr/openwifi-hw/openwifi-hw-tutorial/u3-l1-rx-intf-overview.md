# rx_intf 接收接口总览

## 1. 本讲目标

本讲是「接收链路源码」单元（u3）的第一篇，带你进入 openwifi 接收机的最外层——`rx_intf`（receive interface）模块。读完本讲你应当能够：

- 说清 `rx_intf` 在整个 openwifi 接收链路中的位置，以及它对外承担的 4 件事：取 ADC 样点、喂样点给 `openofdm_rx`、收 `openofdm_rx` 解出的字节并做 FCS、把有效帧经 AXI-Stream 送往 PS。
- 看懂 `rx_intf.v` 顶层模块的端口列表，并能把它整理成「ADC 数据输入 / 与 openofdm_rx 的样点和包信号 / 与 xpu 的控制信号 / AXI 总线」四类接口。
- 理解 `mute_adc_out_to_bb`（发射时静音自收）和 `block_rx_dma_to_ps`（按地址过滤拦截上报）这两个来自 `xpu` 的控制信号如何改变数据流。
- 说清 `rx_pkt_intr` 中断在什么时机触发——它并不是「收到一个包」就立刻拉高，而是要等 Xilinx AXI DMA 把数据写进 DDR 之后，再延迟一段校准时间才发给 PS。

本讲只聚焦 `rx_intf.v` 顶层本身。它内部各子模块（`adc_intf`、`rx_iq_intf`、`byte_to_word_fcs_sn_insert`、`rx_intf_pl_to_m_axis`、`rx_intf_m_axis`）的逐行细节留到 u3-l2，`openofdm_rx` 留到 u3-l3。

## 2. 前置知识

本讲假设你已读过 u2-l2（openwifi_ip 层级），知道接收链路的总体走向是：

> ADC → `rx_intf`（出样点）→ `openofdm_rx`（解字节）→ `rx_intf`（拼 AXI-Stream）→ DMA → PS

在此基础上，还需要几个概念：

- **ADC 与 DAC**：AD9361 射频芯片里把模拟射频信号数字化（ADC，模数转换）和把数字变回模拟（DAC）。接收链路的入口就是 ADC。AD9361 有 2 路接收通道（antenna 0 / antenna 1），每路一对 I/Q，每样点 I、Q 各 16 bit。
- **I/Q 样点**：通信里把基带复信号拆成实部 I 和虚部 Q 两路。openwifi 里每个样点 I、Q 各 16 bit，两根天线共 \(2 \times 2 \times 16 = 64\) bit，正好打成一个 64 bit 字。
- **AXI-Stream（AXIS）**：一种用来传「数据流」的总线，核心是 `tvalid`/`tready` 握手 + `tdata` 数据 + `tlast`（一帧最后一个字）。`rx_intf` 用一个 AXIS 主口（M00_AXIS）把收到的帧推给 Xilinx AXI DMA。
- **AXI4-Lite**：用来读写少量「寄存器」的轻量总线。PS（ARM）通过它配置/读取 `rx_intf` 的 `slv_reg0..N`。
- **FCS（Frame Check Sequence）**：帧尾的 CRC 校验。`openofdm_rx` 解完一个包会给出 `fcs_ok`，表示这个包是否完整无错。
- **跨时钟域（CDC）**：ADC 样点在射频时钟 `adc_clk` 域，基带处理在 `m00_axis_aclk`（acc 域）里。信号跨这两个时钟要用同步器（这里用 Xilinx 原语 `xpm_cdc_array_single`）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [ip/rx_intf/src/rx_intf.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf.v) | **本讲主角**。接收接口顶层，声明所有对外端口，例化 6 个子模块，做天线选择/静音/中断映射等顶层 glue 逻辑。 |
| [ip/rx_intf/src/rx_intf_pl_to_m_axis.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf_pl_to_m_axis.v) | `rx_intf` 的子模块。维护「给 DMA 上报」的状态机，插入 DMA 头（TSF/RSSI/rate/len），处理地址过滤，并**产生 `rx_pkt_intr` 中断**。本讲讲中断时会读它。 |
| [ip/openwifi_ip.tcl](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl) | openwifi_ip 层级蓝图。本讲用它**核对端口连线**——确认 ADC、openofdm_rx、xpu 的信号确实是按源码端口名接到 `rx_intf_0` 上的。 |
| [ip/rx_intf/src/byte_to_word_fcs_sn_insert.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/byte_to_word_fcs_sn_insert.v) | `rx_intf` 的子模块。把 `openofdm_rx` 输出的字节流拼成 64 bit 字，并把 FCS 结果与序号插进去。本讲讲数据通路时简要引用。 |
| [ip/rx_intf/src/adc_intf.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/adc_intf.v) | `rx_intf` 的子模块。把 ADC 数据做抽取（decimate）、增益移位，并跨时钟域送到基带时钟。讲 ADC 输入时简要引用。 |

## 4. 核心概念与源码讲解

### 4.1 概念说明：rx_intf 是接收链路的「口岸」

如果把整条接收链路想象成一条物流线：

- `adc_intf`：港口卸货——把 AD9361 ADC 送来的 64 bit 原始 I/Q 样点接进来，做抽取和跨时钟域。
- `rx_iq_intf`：分拣 I/Q——把样点整理成 `sample0`（天线 0 的 I/Q）和 `sample1`（天线 1 的 I/Q），交给 `openofdm_rx` 去解调。
- `openofdm_rx`（外部子模块）：加工厂——做包检测、同步、FFT、均衡、Viterbi 译码，吐出**字节流**和包长/速率/FCS 等元信息。
- `byte_to_word_fcs_sn_insert`：装箱——把字节流每 8 字节拼成一个 64 bit 字，并在尾部插入 FCS 结果和帧序号。
- `rx_intf_pl_to_m_axis` + `rx_intf_m_axis`：报关发货——给这批数据加一个 DMA 头（时间戳、RSSI、速率、长度等），用 AXI-Stream 推给 AXI DMA，DMA 再写进 PS 的 DDR 内存。

`rx_intf.v` 这个顶层就是这条物流线的「口岸大楼」：它不亲自做数字信号处理，而是把上面这些子模块实例化并连起来，同时承担三件只能放在顶层做的事：

1. **天线选择与发射静音**：决定哪路天线当主路、发射时是否屏蔽自收。
2. **中断映射**：把内部的中断/事件按软件配置（`slv_reg`）选择路由到 `rx_pkt_intr`。
3. **复位分发**：用 `slv_reg0` 的若干位给各子模块提供独立的软复位。

### 4.2 核心流程：一个接收帧在 rx_intf 内的完整旅程

下面用伪流程描述一个有效帧从「无线电磁波」到「PS 的 DDR 内存」的处理过程（结合下面 4.3 的源码一起看）：

```
1. AD9361 ADC 产出 64bit adc_data（含天线0/1 的 I/Q），随 adc_valid 进入 rx_intf
2. adc_intf: 抽取(decimate) + bb_gain 移位 + 跨时钟域(adc_clk -> acc_clk)
            -> ant_data_after_sel (仍 64bit, 4×16)
3. 顶层做天线选择 (ant_flag) 与静音 (mute_adc_out_to_bb) -> adc_data_internal
4. rx_iq_intf: 拆成 I0/Q0/I1/Q1, 经 FIFO 平滑
            -> sample0={I0,Q0}, sample1={I1,Q1}, sample_strobe  ===> 送给 openofdm_rx
5. openofdm_rx 检测到信号 -> pkt_header_valid_strobe + pkt_header_valid(sig_valid)
6. rx_intf_pl_to_m_axis 状态机: WAIT_FOR_PKT -> DMA_HEADER0_INSERT -> DMA_HEADER1_INSERT_AND_START
   - 头0 = 锁存的 TSF 时间戳
   - 头1 = {phase, ht_flags, rate, len, rssi, gpio_status, ...}
7. openofdm_rx 持续吐字节 byte_in/byte_in_strobe + 字节计数 byte_count
   byte_to_word_fcs_sn_insert: 每 8 字节拼成 64bit word -> data_from_acc
8. openofdm_rx 给出 fcs_in_strobe + fcs_ok -> 插入 FCS 字节与帧序号
9. rx_intf_pl_to_m_axis: WAIT_FILTER_FLAG
   - 若 xpu 地址过滤放行 (block_rx_dma_to_ps==0) -> start_m_axis, 进入 WAIT_DMA_TLAST
   - 若被过滤 (block_rx_dma_to_ps==1) -> 复位, 不上报 PS
10. rx_intf_m_axis: 把 word 流送入 AXIS 主口 m00_axis -> AXI DMA(S2MM) -> 写入 DDR
11. DMA 写完一帧 -> s2mm_intr 拉高
12. rx_intf_pl_to_m_axis: 检测到 s2mm_intr 上升沿, 启动 count 计时,
    计满 count_top 后拉高 rx_pkt_intr_internal -> (经顶层映射) rx_pkt_intr -> PS 收到中断
```

关键直觉：`rx_pkt_intr` 是在「DMA 已经把帧写进 DDR 之后」才发的，所以 PS 一收到这个中断，就可以放心地去 DDR 里读一个完整的帧。

### 4.3 源码精读：把端口列表分成四类

`rx_intf.v` 的端口声明从模块头开始。先看参数表（这些参数在打包成 IP 时被固定，决定了数据宽度等）：

[ip/rx_intf/src/rx_intf.v:L8-L26](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf.v#L8-L26) —— 定义了 `ADC_PACK_DATA_WIDTH=64`（ADC 一次送 64 bit，正好 4 个 16 bit = 2 天线 × I/Q）、`IQ_DATA_WIDTH=16`、AXI 数据宽度、`MAX_NUM_DMA_SYMBOL=8192`（DMA FIFO 深度上限）等。

把整个端口列表 [ip/rx_intf/src/rx_intf.v:L27-L124](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf.v#L27-L124) 按数据流向分成四类，最容易理解：

**第一类：ADC 数据输入（来自 AD9361）**

[ip/rx_intf/src/rx_intf.v:L45-L49](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf.v#L45-L49) —— `adc_clk`/`adc_rst` 是射频时钟与复位，`adc_data` 是 64 bit 的原始样点，`adc_valid` 是有效标志。注意这一类工作在 `adc_clk` 域，与后面的基带时钟不同。

另外还有一路「来自 tx_intf 的回环 I/Q」[ip/rx_intf/src/rx_intf.v:L51-L54](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf.v#L51-L54)，用于把发射链路的 I/Q 直接回环进接收链路做测试或自收（下面 4.4 会讲如何切换）。

**第二类：与 openofdm_rx 的样点/包信号（双向）**

这一类是 `rx_intf` 与 OFDM 接收机之间的「界面」，最密集：

[ip/rx_intf/src/rx_intf.v:L56-L73](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf.v#L56-L73)

- 出方向（喂给 openofdm_rx 的样点）：`sample0`/`sample1`/`sample_strobe`
- 入方向（openofdm_rx 解出的包信息与字节流）：`pkt_header_valid`/`pkt_header_valid_strobe`（检测到有效信号头）、`pkt_rate`/`pkt_len`（速率与长度）、`byte_in`/`byte_in_strobe`/`byte_count`（解出的字节）、`fcs_in_strobe`/`fcs_ok`（FCS 校验）、`ht_aggr`/`ht_sgi`/`ht_unsupport`（HT/11n 相关标志）、`phase_offset_taken`（相位补偿）。

> 注意源码里端口名是 `sample0`/`sample1`，在 block design 里对外打包成 `sample`/`sample_strobe` 接到 `openofdm_rx_0/sample_in`（见 [ip/openwifi_ip.tcl:L324-L325](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L324-L325)）。这是 IP 打包层的一次合并，源码层仍是两路。

**第三类：与 xpu 的控制信号（入方向为主）**

[ip/rx_intf/src/rx_intf.v:L84-L91](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf.v#L84-L91) —— 这一组是本讲的另一个重点：

- `mute_adc_out_to_bb`：发射时静音，屏蔽自收；
- `block_rx_dma_to_ps` / `block_rx_dma_to_ps_valid`：xpu 的地址过滤结果，决定这一帧要不要上报给 PS；
- `rssi_half_db_lock_by_sig_valid` / `gpio_status_lock_by_sig_valid`：在信号有效时刻锁存的 RSSI 与 GPIO 状态（用来填进 DMA 头）；
- `tsf_runtime_val` / `tsf_pulse_1M`：来自 `tsf_timer` 的 64 bit 计时器和 1µs 脉冲，用于打时间戳和超时计时。

**第四类：AXI 总线（与 PS 的寄存器 + DMA 数据通路）**

- AXI4-Lite 从口（PS 配置寄存器）：[ip/rx_intf/src/rx_intf.v:L93-L114](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf.v#L93-L114)
- AXI-Stream 主口（把帧推给 AXI DMA）：[ip/rx_intf/src/rx_intf.v:L116-L123](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf.v#L116-L123)
- 中断输出 `rx_pkt_intr`：[ip/rx_intf/src/rx_intf.v:L79](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf.v#L79)；DMA 完成中断输入 `s2mm_intr`：[ip/rx_intf/src/rx_intf.v:L82](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf.v#L82)

其中 `s2mm_intr` 是 Xilinx AXI DMA「S2MM 通道写完一段数据」时发出来的中断，`rx_pkt_intr` 是 `rx_intf` 自己产生的、最终送给 PS 的收包中断。两者的关系见 4.5。

### 4.4 源码精读：mute_adc_out_to_bb 与 block_rx_dma_to_ps

这两个控制信号都在顶层 `rx_intf.v` 里直接影响数据流，必须看懂。

**mute_adc_out_to_bb（发射时静音自收）**

发射机在发自己的包时，本机的 ADC 仍会采到空气中的信号（甚至自己发射的强信号），如果不去屏蔽，`openofdm_rx` 可能会「收到自己发的包」。`xpu` 在发射期间把 `mute_adc_out_to_bb` 拉高。由于该信号来自 acc（基带）时钟域，要先用 CDC 同步到 `adc_clk` 域：

[ip/rx_intf/src/rx_intf.v:L271-L283](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf.v#L271-L283) —— `xpm_cdc_array_single` 把 1 bit 的 `mute_adc_out_to_bb` 从 `s00_axi_aclk` 同步到 `adc_clk`，得到 `mute_adc_out_to_bb_in_rf_domain`。

然后真正的「静音」只是把天线 0 的那 32 bit 清零（天线 1 保留）：

[ip/rx_intf/src/rx_intf.v:L259-L260](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf.v#L259-L260)

```verilog
assign adc_data_after_sel = (ant_flag_in_rf_domain ? {adc_data[31:0],adc_data[63:32]} : adc_data);
assign adc_data_internal  = (mute_adc_out_to_bb_in_rf_domain ?
                             {adc_data_after_sel[63:32],32'd0} : adc_data_after_sel);
```

第一行用 `ant_flag`（由 `slv_reg16[0]` 经 CDC 而来）决定是否把两根天线的高低 32 bit 对调——即「哪根当天线 0」。第二行：静音时把低 32 bit（即天线 0 的 I0/Q0）强制清零，而送给 `openofdm_rx` 的 `sample0` 正是来自这低 32 bit（见 [ip/rx_intf/src/rx_intf.v:L262-L266](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf.v#L262-L266)），所以 `openofdm_rx` 拿到全零样点、检测不到包，达到「自收静音」的效果。

顺带一提，`slv_reg3[8]` 这一位可以把接收链路的输入从「ADC」切换到「tx_intf 回环 I/Q」[ip/rx_intf/src/rx_intf.v:L262-L266](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf.v#L262-L266)，用于不发射频、直接在板内做发射→接收的回环测试。

**block_rx_dma_to_ps（按地址过滤拦截上报）**

这个信号的作用不在顶层 `rx_intf.v` 里直接体现，而是透传给子模块 `rx_intf_pl_to_m_axis`，由那里的状态机决定。顶层只是把它连过去：

[ip/rx_intf/src/rx_intf.v:L452-L453](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf.v#L452-L453) —— 例化 `rx_intf_pl_to_m_axis` 时把 `block_rx_dma_to_ps`/`block_rx_dma_to_ps_valid` 接进子模块。

真正的过滤逻辑在 `rx_intf_pl_to_m_axis.v` 的 `WAIT_FILTER_FLAG` 状态里（地址过滤的具体来源在 u5-l4 讲）：

[ip/rx_intf/src/rx_intf_pl_to_m_axis.v:L237-L246](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf_pl_to_m_axis.v#L237-L246)

```verilog
if (block_rx_dma_to_ps_valid==1 && block_rx_dma_to_ps==0) begin
    ... start_m_axis <= 1; ... rx_state <= WAIT_DMA_TLAST;   // 放行：启动 DMA 上报
end else if (block_rx_dma_to_ps_valid==1 && block_rx_dma_to_ps==1) begin
    ... m_axis_rst<=1; rx_state <= WAIT_RST_DONE;            // 过滤：丢弃，复位，不上报
end
```

读法：`block_rx_dma_to_ps_valid` 表示「xpu 的地址过滤结果已就绪」，`block_rx_dma_to_ps==0` 表示「放行」，`==1` 表示「拦截」。放行的帧才会触发 `start_m_axis` 真正推给 DMA；被拦截的帧直接复位丢掉，PS 永远看不到——这就是 openwifi 在硬件层做的 MAC 地址过滤。

### 4.5 源码精读：rx_pkt_intr 何时触发

这是本讲最微妙、也最常被误解的一点。先看顶层 `rx_intf.v` 对中断的「路由」：

[ip/rx_intf/src/rx_intf.v:L250-L252](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf.v#L250-L252)

```verilog
assign rx_pkt_intr = (slv_reg2[8]==0 ? intr_internal : slv_reg2[0]);
assign intr_internal = (slv_reg2[12]==0 ? rx_pkt_intr_internal : fcs_valid_internal);
```

默认情况下（`slv_reg2` 这些调试位为 0），`rx_pkt_intr` 直接等于 `rx_pkt_intr_internal`，也就是子模块 `rx_intf_pl_to_m_axis` 算出来的那个内部中断。软件也可以把 `slv_reg2[8]` 置 1 用 `slv_reg2[0]` 软件强制触发中断（用于调试），或把 `slv_reg2[12]` 置 1 让中断改报「FCS 有效」事件。这些是调试用的旁路，正常运行都用 `rx_pkt_intr_internal`。

那 `rx_pkt_intr_internal` 又是怎么产生的？在 `rx_intf_pl_to_m_axis.v` 最末尾的独立进程里：

[ip/rx_intf/src/rx_intf_pl_to_m_axis.v:L296-L304](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf_pl_to_m_axis.v#L296-L304)

```verilog
always @(posedge clk) begin
  if ( (!rstn) || (s2mm_intr==1 && s2mm_intr_reg==0) ) begin   // 复位 或 检测到 s2mm_intr 上升沿
    count <= 0;
    rx_pkt_intr <= 0;
  end else begin
    count <= (count!=count_top_scale_plus1 ? (count+1) : count);
    rx_pkt_intr <= (count==count_top_scale);                    // 计满 count_top 后拉高一个周期
  end
end
```

读法分两步：

1. **触发点**：`(s2mm_intr==1 && s2mm_intr_reg==0)` 是 `s2mm_intr` 的**上升沿**。也就是说，中断计时器是在「AXI DMA 的 S2MM 通道刚完成一次写」的瞬间启动的，而不是在 `openofdm_rx` 解出包的瞬间。
2. **延迟**：启动后用一个计数器 `count` 数到 `count_top_scale` 才把 `rx_pkt_intr` 拉高一个时钟周期。其中 `count_top_scale = count_top * COUNT_SCALE`（`count_top` 来自 `slv_reg13[14:0]`，由软件配置；`COUNT_SCALE` 是软件 10MHz 计数器到基带时钟的换算标尺，见 u2-l4）。

为什么要这段延迟？因为 `s2mm_intr` 是 DMA 控制器在「描述符完成」时发的，它发中断的时机可能略早于数据真正在 DDR 中可见/缓存一致。`rx_intf` 故意延迟一段校准时间再通知 PS，确保 PS 读 DDR 时这一帧已经完好可读（收包走 ACP 端口保证缓存一致，见 u2-l3）。

> 结论一句话：**`rx_pkt_intr` 在「AXI DMA 把一帧写进 DDR 之后，再延迟一个软件可配的 `count_top` 时长」才触发**。它代表的是「有一个完整帧在 DDR 里等你处理」，而不是「电磁波里来了一个包」。

再补一个细节：上面 4.4 讲到被地址过滤的帧不会 `start_m_axis`，DMA 根本不会被启动，自然也不会有 `s2mm_intr`，于是也不会产生 `rx_pkt_intr`——所以「不上报 PS」是在源头就掐断的。

### 4.6 代码实践：端口分类与中断触发追踪

**实践目标**：亲手把 `rx_intf.v` 的端口列表整理成四类接口，并追出 `rx_pkt_intr` 的完整触发条件，建立「读 RTL 端口表」和「跨模块追信号」两项基本功。

**操作步骤**：

1. 打开 [ip/rx_intf/src/rx_intf.v:L27-L124](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf.v#L27-L124)，准备一张四列的表（建议用纸或文本文件）：「ADC 数据输入 / 与 openofdm_rx 的信号 / 与 xpu 的控制信号 / AXI 总线与中断」。
2. 逐个把每个 `input`/`output` 端口按「来源/去向」归入对应列。遇到拿不准的（例如 `gpio_status_rf`/`gpio_status_bb`），先记下，再去 `openwifi_ip.tcl` 里搜该端口名确认连到了谁。
3. 在 `openwifi_ip.tcl` 里用搜索确认三类关键连线：
   - ADC：搜 `adc_data`，确认 `adc_data` → `rx_intf_0/adc_data`（[L293](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L293)）。
   - openofdm_rx 字节流：搜 `openofdm_rx_0_byte_out`，确认 `openofdm_rx_0/byte_out` → `rx_intf_0/byte_in` 且同时 → `xpu_0/byte_in`（一拖二，[L301](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L301)）。
   - 中断：搜 `rx_pkt_intr`，确认 `rx_intf_0/rx_pkt_intr` → 层级对外口 `rx_pkt_intr`（[L317](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl#L317)）。
4. 追中断链：从顶层 [rx_intf.v:L250](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf.v#L250) 的 `rx_pkt_intr` → `rx_pkt_intr_internal`（来自 `rx_intf_pl_to_m_axis_i`，见 [L468](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf.v#L468)）→ 进入 [rx_intf_pl_to_m_axis.v:L296](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/rx_intf/src/rx_intf_pl_to_m_axis.v#L296)，确认它由 `s2mm_intr` 上升沿 + `count` 计数共同决定。

**需要观察的现象**（读代码时自问）：

- `mute_adc_out_to_bb` 这个信号在顶层经过了几级处理才作用到 `sample0`？（CDC 同步 → 清零低 32 bit → 经 `rx_iq_intf` 到 `sample0`）
- `rx_pkt_intr` 在「被地址过滤的帧」上会不会触发？（不会，因为 DMA 没启动就没有 `s2mm_intr`）
- 默认配置下（`slv_reg2`=0），`rx_pkt_intr` 和 `s2mm_intr` 是不是同一个信号？（不是，前者由后者触发后再延迟一段才产生）

**预期结果**：你应该得到一张大约 4 列、共 50 余个端口的分类表，并写出一句话结论：「`rx_pkt_intr` 在 `s2mm_intr`（DMA 写完 DDR）上升沿之后再延迟 `count_top×COUNT_SCALE` 个基带时钟才拉高一个周期」。

> 说明：本实践是源码阅读型实践，不需要综合/上板。若你想在仿真中观察，可参考 u7-l3 用 `create_vivado_proj.sh` 给 `rx_intf` 建单 IP 工程，但本讲不要求运行。「能否在不跑仿真的情况下，仅靠读 RTL 看出中断触发条件」——这正是本实践的检验点。

### 4.7 小练习与答案

**练习 1**：`adc_data` 是 64 bit，它里面具体装了什么？为什么是这个宽度？

参考答案：装的是 AD9361 两路接收天线、每路 I/Q 各 16 bit 的样点，即 \(\{I_1,Q_1,I_0,Q_0\}\)，共 \(4\times 16=64\) bit，对应参数 `ADC_PACK_DATA_WIDTH=64` 与 `IQ_DATA_WIDTH=16`。

**练习 2**：如果把 `mute_adc_out_to_bb` 长期拉高，`openofdm_rx` 还能收到包吗？为什么？

参考答案：基本收不到。因为静音会把天线 0 的低 32 bit 清零，而 `sample0`（送给 `openofdm_rx` 的主样点）正是来自这低 32 bit，于是 `openofdm_rx` 拿到全零样点，无法做包检测/同步。这也正是发射期间屏蔽自收的原理。

**练习 3**：`rx_pkt_intr` 与 `s2mm_intr` 谁先发生？中间隔了什么？

参考答案：`s2mm_intr` 先发生（AXI DMA 的 S2MM 通道写完一帧 DDR 时由 DMA 发出）；`rx_pkt_intr` 在此之后，由 `rx_intf_pl_to_m_axis` 检测到 `s2mm_intr` 上升沿、并用 `count` 计满 `count_top_scale` 后才拉高一个周期。中间隔的是一段软件可配置的延迟，用于保证数据在 DDR 中已可被 PS 安全读取。

## 5. 综合实践

把本讲的知识串起来，做一个**「接收帧的端到端信号追踪」**：

给定一个被 `xpu` 地址过滤放行、且 FCS 正常的有效帧，请在源码中按顺序标出它经过的 7 个关键「站点」及对应行号，并回答两个串联问题：

1. 这个帧从 `adc_data` 进入，到最终触发 `rx_pkt_intr`，依次经过哪些模块/赋值语句？（提示：`adc_data` → 天线选择/静音赋值 → `adc_intf` → `rx_iq_intf` → `sample0` → `openofdm_rx` → `byte_in` → `byte_to_word_fcs_sn_insert` → `rx_intf_pl_to_m_axis` 的 DMA 头插入与 `WAIT_FILTER_FLAG` → `rx_intf_m_axis` 的 AXIS 主口 → DMA → `s2mm_intr` → `rx_pkt_intr`）
2. 如果这个帧的地址被 `xpu` 过滤掉了，上面这条链在哪一步被掐断、之后还会不会有 `rx_pkt_intr`？

要求：把每个站点都写出对应的永久链接（形如 `[ip/rx_intf/src/rx_intf.v:Lxxx](...#Lxxx)`），并在链路上标注「数据形态」的变化（64bit ADC 字 → I/Q 样点 → 字节流 → 64bit AXIS 字 → DDR 帧）。这个练习会把本讲的端口分类、控制信号、中断触发三个最小知识点连成一条线，为 u3-l2（子模块逐行）和 u3-l3（openofdm_rx）打好地基。

## 6. 本讲小结

- `rx_intf` 是接收链路的「口岸」：它不做 OFDM 算法，而是把 6 个子模块连起来，并承担天线选择、发射静音、中断路由、软复位分发等只能放在顶层的事。
- 它的端口可干净地分成四类：ADC 数据输入（`adc_clk` 域）、与 `openofdm_rx` 的样点/包信号（双向）、与 `xpu` 的控制信号、AXI4-Lite 寄存器 + AXI-Stream 主口 + 中断。
- `mute_adc_out_to_bb` 经 CDC 同步后把天线 0 的低 32 bit 清零，从而让 `openofdm_rx` 收到全零样点，实现发射期间的「自收静音」。
- `block_rx_dma_to_ps` 在 `rx_intf_pl_to_m_axis` 的 `WAIT_FILTER_FLAG` 状态里决定一帧是放行上报还是复位丢弃，这是硬件层的 MAC 地址过滤。
- `rx_pkt_intr` 不是「来包即触发」，而是在 AXI DMA 写完 DDR（`s2mm_intr` 上升沿）之后、再延迟 `count_top×COUNT_SCALE` 个时钟才拉高一个周期，含义是「DDR 里有一个完整帧可读」。
- 默认配置下中断链路是 `rx_pkt_intr_internal → intr_internal → rx_pkt_intr`，`slv_reg2` 提供软件强制中断和「改报 FCS 事件」两个调试旁路。

## 7. 下一步学习建议

- 想深入 `rx_intf` 内部各子模块（`adc_intf` 的抽取与增益、`rx_iq_intf` 的 FIFO 平滑、`byte_to_word_fcs_sn_insert` 的拼字与 FCS/序号插入、`rx_intf_m_axis` 的 AXIS 主口 FIFO），请继续学 **u3-l2 rx_intf 关键子模块**。
- 想了解喂给 `rx_intf` 样点的那个 OFDM 接收机本身（包检测、同步、FFT、均衡、Viterbi），请学 **u3-l3 openofdm_rx：OFDM 接收机**。
- 想搞清 `mute_adc_out_to_bb`、`block_rx_dma_to_ps`、`tsf_pulse_1M` 这些控制信号是谁、在什么条件下发给 `rx_intf` 的，请跳到 **u5-l1 xpu 控制核心总览** 与 **u5-l4 TSF 定时器与接收包解析/过滤**。
- 想看 `slv_reg0..N` 这些寄存器如何被 PS 读写、`slv_reg2[8]` 这类调试位怎么用，请学 **u7-l1 AXI 寄存器映射与软件交互**。
