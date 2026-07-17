# tx_intf 发射接口与 BRAM 缓存

## 1. 本讲目标

在上一篇（u4-l1）里，我们把 `openofdm_tx` 当成一个「吃字节、吐 I/Q」的黑盒来看。本讲要回答的是这个黑盒**前面**和**后面**的两件事：

- 待发送的字节从哪来？——从 PS（ARM）经 AXI DMA 送下来，本讲要讲清楚它如何进入一块 BRAM 缓存，再被 `openofdm_tx` 消费。
- `openofdm_tx` 算出的 I/Q 样点到哪去？——经过增益、FIFO、跨时钟域，最终送到 AD9361 的 DAC。

学完本讲，你应当能够：

1. 说清 `tx_intf` 在发射链路里的「口岸」定位——它把 PS 的 DMA 数据流、`openofdm_tx` 的 BRAM 接口、`openofdm_tx` 的 I/Q 输出和 AD9361 的 DAC 接口粘在一起。
2. 画出 **DMA → s_axis FIFO → BRAM → openofdm_tx → tx_iq_intf → dac_intf → DAC** 这条完整数据流，并指出每一段由哪个子模块负责。
3. 解释 `tx_status_fifo`、`ht_sig_crc_calc`、`csi_fuzzer` 三个辅助模块各自解决什么问题。

## 2. 前置知识

本讲默认你已经读过 u4-l1（`openofdm_tx` 总览），知道以下概念：

- **PS / PL**：Zynq 的 ARM 处理系统（PS）与可编程逻辑（PL）。本讲的 `tx_intf` 在 PL 侧，PS 通过 DMA 给它喂数据。
- **AXI-Stream**：一种用于「搬数据流」的总线，核心信号是 `tvalid/tready/tdata/tlast`，靠握手逐拍传输。PS 的发包 DMA（MM2S）就把待发数据以 AXI-Stream 形式送进 `tx_intf`。
- **AXI4-Lite**：用于「读写少量寄存器」的总线，PS 用它配置 `tx_intf`（速率、增益、队列阈值等）并读状态。
- **BRAM（Block RAM）**：FPGA 片上的双口存储块。本讲里它充当 PS 与 `openofdm_tx` 之间的「数据缓存盘」。
- **跨时钟域（CDC）**：基带逻辑跑在 ~100/200MHz（`s00_axis_aclk`），DAC 侧跑在 40MHz（`dac_clk`），两边交换 I/Q 必须做 CDC。本讲用 Xilinx 原语 `xpm_cdc_*` / `xpm_fifo_sync` 处理。
- **phy_tx_start / phy_tx_done**：上一篇讲过的握手。`phy_tx_start`（脉冲）触发 `openofdm_tx` 开时；`phy_tx_done`（本讲里叫 `tx_end_from_acc`）表示一帧 I/Q 已全部吐完。

> 名词提示：源码里频繁出现 `acc`，例如 `data_to_acc`、`rf_i_from_acc`。`acc` = accelerator，特指 `openofdm_tx` 这个物理层加速器。所以「to_acc」=「送给 openofdm_tx」，「from_acc」=「来自 openofdm_tx」。记住这个，端口名就不绕了。

## 3. 本讲源码地图

本讲全部源码都在 `ip/tx_intf/src/` 下：

| 文件 | 作用 |
|------|------|
| [tx_intf.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_intf.v) | 顶层。自身不做算法，只例化并连线下面 7 个子模块，分配复位、LED、中断。 |
| [tx_bit_intf.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_bit_intf.v) | 发射主控状态机 + BRAM。把 s_axis FIFO 的数据写入 BRAM、拼 PHY 头（L-SIG / HT-SIG）、产生 `phy_tx_start`。**最大也最核心**。 |
| [tx_intf_s_axis.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_intf_s_axis.v) | AXI-Stream 从设备：按 4 个发送队列把 DMA 数据写入 4 条 FIFO。 |
| [tx_iq_intf.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_iq_intf.v) | I/Q 通路：对 `openofdm_tx` 的 I/Q 施加基带增益，过 FIFO 缓冲与背压，再送 `csi_fuzzer`。 |
| [dac_intf.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/dac_intf.v) | DAC 接口：把 20Msps 基带 I/Q 跨时钟域搬到 40MHz DAC 域，做天线/CDD 选择。 |
| [tx_status_fifo.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_status_fifo.v) | 把每包的发送结果（重传次数、Block-Ack 位图等）压入 4 条 FIFO 供 PS 读取。 |
| [ht_sig_crc_calc.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/ht_sig_crc_calc.v) | 计算 802.11 HT-SIG 字段的 CRC8。 |
| [csi_fuzzer.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/csi_fuzzer.v) | 一个 2 抽头 FIR，用于「故意」给发射 I/Q 注入可控信道畸变（研究 CSI 用）。 |

## 4. 核心概念与源码讲解

### 4.1 tx_intf 顶层：发射链路的「口岸」与子模块装配

#### 4.1.1 概念说明

`tx_intf`（transmit interface）是发射链路的口岸：它自己不做 OFDM 算法（那是 `openofdm_tx` 的活），也不直接和 DDR 打交道（那是 PS DMA 的活）。它的职责是「把各方信号粘起来」——接收 PS 经 DMA 送来的待发数据、缓存进 BRAM、在合适时机启动 `openofdm_tx`、再把 `openofdm_tx` 吐出的 I/Q 经增益与跨时钟域送到 DAC。

正因为它要同时对接 PS（DMA + 寄存器）、`openofdm_tx`（BRAM + I/Q + 握手）、`xpu`（MAC 控制）、`dac`（射频）四方，所以 `tx_intf.v` 的端口列表很长，但模块体里几乎全是「例化子模块 + assign 连线」，逻辑都被拆到了 7 个子模块里。

#### 4.1.2 核心流程

`tx_intf` 把发射链路装配成下面这条流水线（控制信号没画）：

```
PS/DDR
  │  AXI-Stream (s00_axis, 64-bit)
  ▼
tx_intf_s_axis ──► 4 条 FIFO（按 tx_queue_idx 0..3 分队列）
  │  (DATA_TO_ACC / EMPTYN_TO_ACC / ACC_ASK_DATA 握手)
  ▼
tx_bit_intf ──写─► BRAM(port A) ──读(port B)──► openofdm_tx (bram_addr 驱动)
  │                                            │
  │  phy_tx_start 脉冲 ──────────────────────► │ 触发发射
  │                                            │ 生成 I/Q
  │  ◄──────── rf_i/rf_q/rf_iq_valid ───────── │ (result_i/q)
  ▼
tx_iq_intf (×bb_gain 增益 → FIFO 缓冲 → csi_fuzzer)
  │  wifi_iq_pack
  ▼
dac_intf (CDC 到 40MHz + 天线/CDD 选择)
  │  dac_data
  ▼
AD9361 DAC → 射频
```

四个方向的对外接口分别由不同子模块负责：

- **PS 数据入口**：`tx_intf_s_axis`（AXI-Stream 从设备）。
- **BRAM + 主控**：`tx_bit_intf`（写 BRAM、发 `phy_tx_start`）。
- **I/Q 通路**：`tx_iq_intf`（增益 + FIFO + csi_fuzzer）。
- **DAC 接口**：`dac_intf`（跨时钟域 + 天线选择）。
- **寄存器**：`tx_intf_s_axi`（AXI4-Lite 从设备，本讲不讲细节，留到 u7-l1）。
- **状态上报**：`tx_status_fifo`。
- **中断选择**：`tx_interrupt_selection`。

#### 4.1.3 源码精读

先看顶层模块的参数与三类核心端口。参数里有几个决定了缓存与数据宽度，初学者可以重点关注 `MAX_NUM_DMA_SYMBOL`：

[ip/tx_intf/src/tx_intf.v:16-37](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_intf.v#L16-L37) —— 模块参数。`SMALL_FPGA` 宏会把 `MAX_NUM_DMA_SYMBOL` 从 8192 砍到 4096（s_axis FIFO 深度随之减半），这就是同一份代码适配小容量器件（如 zynq 7020）的开关，详见 u2-l4。

[ip/tx_intf/src/tx_intf.v:42-50](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_intf.v#L42-L50) —— DAC 接口。`dma_data/dma_valid/dma_ready` 是和 ADI 的 `axi_ad9361_dac_dma` 对接（本讲不深入），`dac_data/dac_valid/dac_ready` 是给 `util_ad9361_dac_upack` 的真正 DAC 数据通路，64bit 打包 I/Q。

[ip/tx_intf/src/tx_intf.v:61-71](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_intf.v#L61-L71) —— 与 `openofdm_tx`（acc）的 BRAM + I/Q + 握手接口。注意 `bram_addr` 是 **input**（地址由 openofdm_tx 驱动来读），`data_to_acc` 是 **output**（tx_intf 把 BRAM 读出的数据送给 openofdm_tx），`rf_i_from_acc/rf_q_from_acc` 是 openofdm_tx 算出的 I/Q 回送。

接着看顶层如何把这 7 个子模块装配起来——这一段是「口岸」最直接的体现：

[ip/tx_intf/src/tx_intf.v:293-316](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_intf.v#L293-L316) —— 例化 `dac_intf`，把 `wifi_iq_pack`（来自 tx_iq_intf）接成 `data_from_acc`，输出 `dac_data`。注意复位接成 `s00_axi_aresetn & (~slv_reg0[5])`，即 PS 写 `slv_reg0[5]` 可单独软复位 DAC 接口。

[ip/tx_intf/src/tx_intf.v:385-409](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_intf.v#L385-L409) —— 例化 `tx_status_fifo`，输入是 `xpu` 送来的 `tx_status[79:0]`，输出挂到 `slv_reg22~25` 供 PS 读。

[ip/tx_intf/src/tx_intf.v:412-438](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_intf.v#L412-L438) —— 例化 `tx_intf_s_axis`。`S_AXIS_NUM_DMA_SYMBOL_raw` 取自 `slv_reg8[12:0]`（PS 告诉本包有多少个 64bit DMA 符号），`tx_queue_idx_indication_from_ps` 取自 `slv_reg8[19:18]`（PS 指定本包进哪个队列）。

[ip/tx_intf/src/tx_intf.v:454-522](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_intf.v#L454-L522) —— 例化 `tx_bit_intf`，这是最大的一块，端口里既有 s_axis 数据握手，也有和 `xpu` 的全套 MAC 控制信号（`slice_en`、`backoff_done`、`retrans_in_progress`、`start_retrans` 等），还有到 BRAM 的 `bram_addr/data_to_acc` 和到 openofdm_tx 的 `phy_tx_start`。

[ip/tx_intf/src/tx_intf.v:524-562](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_intf.v#L524-L562) —— 例化 `tx_iq_intf`，接 `rf_i/rf_q/rf_iq_valid`（来自 openofdm_tx）和 `wifi_iq_pack`（去 dac_intf），增益参数 `bb_gain` 取自 `slv_reg13[9:0]`。

最后看一个关键的顶层 assign——中断产生逻辑，它体现了「口岸」对信号的汇聚：

[ip/tx_intf/src/tx_intf.v:264](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_intf.v#L264) —— `tx_itrpt` 最终中断。它由 `slv_reg14[2:0]` 选源（见 `tx_interrupt_selection`），再用 `slv_reg14[8]`（发 ACK 时屏蔽）和 `slv_reg14[17]`（总开关）做门控。

#### 4.1.4 代码实践

**实践目标**：建立「顶层 = 子模块装配」的全局印象，把端口按对接方分组。

**操作步骤**：

1. 打开 [tx_intf.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_intf.v)。
2. 在端口列表（L38–L151）里，把每个端口按「对接方」分成 5 组：①DAC/ADI；②openofdm_tx（acc）；③xpu；④PS（AXI-Lite + AXI-Stream）；⑤side_ch/LED。
3. 在模块体（L160–L563）里，数一下 `xxx_i (` 形式的例化个数，对应到上表的 7 个子模块。

**需要观察的现象**：顶层除了 `assign` 和 `edge_to_flip`（LED 翻转）外，几乎没有任何组合逻辑/状态机——所有逻辑都下沉到了子模块。

**预期结果**：你会得到 7 个例化（`dac_intf_i`、`tx_intf_s_axi_i`、`tx_status_fifo_i`、`tx_intf_s_axis_i`、`tx_interrupt_selection_i`、`tx_bit_intf_i`、`tx_iq_intf_i`），印证「口岸只负责接线」。

#### 4.1.5 小练习与答案

**练习 1**：`bram_addr` 在 `tx_intf` 端口里是 input 还是 output？为什么？

> **答案**：是 input。因为 BRAM 的**读地址由 `openofdm_tx` 驱动**（`openofdm_tx` 是 BRAM 的消费者/读取方），`tx_intf` 只是把这根地址线接进自己内部的 BRAM 端口 B。

**练习 2**：`rf_i_from_acc` 里的 `acc` 指谁？

> **答案**：指 `openofdm_tx`（物理层加速器）。`rf_i_from_acc` = 来自 openofdm_tx 的 I 路 I/Q 样点（即 openofdm_tx 端口侧的 `result_i`）。

---

### 4.2 BRAM 缓存：DMA → BRAM → openofdm_tx 的数据流

#### 4.2.1 概念说明

这是本讲最核心的一块。问题是：PS 用 DMA 把一帧待发字节以 AXI-Stream 形式「推」给 PL，而 `openofdm_tx` 是被一个 `phy_tx_start` 脉冲触发后**自己按地址去读**字节的——一个推、一个拉，速率也不同。中间必须有一块缓存把它们解耦，这块缓存就是 BRAM。

openwifi 的设计是：**BRAM 物理上放在 `tx_intf` 里**（具体在 `tx_bit_intf.v` 中例化的 `xpm_memory_tdpram`），它是一个真双口 RAM：

- **端口 A**：`tx_bit_intf` 写（来自 s_axis FIFO 的数据 + 自己拼的 PHY 头）。
- **端口 B**：`openofdm_tx` 读（`openofdm_tx` 驱动 `bram_addr`，数据从 `data_to_acc` 回送）。

`tx_bit_intf` 既是 BRAM 的「写入者」，又是发射的「总指挥」：它跑一个状态机，决定何时往 BRAM 写 PHY 头（L-SIG / HT-SIG）、何时写数据净荷、何时给 `openofdm_tx` 发 `phy_tx_start`。

#### 4.2.2 核心流程

`tx_bit_intf` 的主控状态机叫 `high_tx_ctl_state`，状态定义如下（完整的 12 个状态）：

[ip/tx_intf/src/tx_bit_intf.v:88-99](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_bit_intf.v#L88-L99) —— 状态机全部状态。

简化后的主流程（省略 CTS-to-self、聚合等分支）：

```
WAIT_TO_TRIG:  等待「队列 slice_en 允许 + CSMA 空闲 + FIFO 有数据 + 无 ACK/重传在进行」
      │  (满足后选一个 tx_queue_idx)
      ▼
WAIT_CHANCE:   等待 backoff_done（CSMA/CA 退避完成，见 u5-l2）
      │
      ▼
PREPARE_TX_FETCH / PREPARE_TX_JUDGE:  从 tx_config FIFO 取本包配置（速率、长度、是否需要 ACK）
      │
      ▼
PREP_PHY_HDR:  计算长度字段、必要时启动 HT-SIG CRC（ht_sig_crc_calc）
      │
      ▼
DO_PHY_HDR1:   写 BRAM[0] = L-SIG（legacy 信号字段）
      │  (若是 HT 速率，再走 DO_PHY_HDR2 写 BRAM[1] = HT-SIG + CRC)
      ▼
DO_TX:         逐拍把 s_axis FIFO 的数据写进 BRAM[2..]，同时 bram 地址推进
      │  (openofdm_tx 一边读 BRAM 一边吐 I/Q；tx_end_from_acc=1 表示吐完)
      ▼
WAIT_TO_TRIG:  回到空闲，等下一包
```

关键时序点：**`phy_tx_start` 不是在 DO_TX 一开始就发**，而是当 BRAM 里攒够一定深度（`addra == num_dma_symbol_th`）时才拉一个脉冲，确保 `openofdm_tx` 启动时 BRAM 里已经有足够数据可读，不会读空。这个脉冲还被故意拉长（`start_delay0..5` 移位寄存器展宽），保证 `openofdm_tx` 能稳定采样到。

#### 4.2.3 源码精读

先看 BRAM 本体和它的双口接线：

[ip/tx_intf/src/tx_bit_intf.v:1102-1162](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_bit_intf.v#L1102-L1162) —— 真双口 BRAM `xpm_memory_tdpram`。`MEMORY_SIZE = 8*8192` 字节，两端口都是 64bit 数据 / 10bit 地址。端口 A 的 `wea/addra/dina` 由本模块驱动（写），`douta` 回送给 xpu 用来标记「首包/重传包」；端口 B 的 `addrb = bram_addr`（openofdm_tx 驱动，只读，`web=1'b0`），`doutb` 就是回送给 openofdm_tx 的 `bram_data_to_acc_int`。

[ip/tx_intf/src/tx_bit_intf.v:244-248](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_bit_intf.v#L244-L248) —— BRAM 端口 A 的写信号多路选择。正常发包时用 `wea_internal/addra_internal/dina_internal`（本状态机驱动）；**重传时改用 `wea_from_xpu/addra_from_xpu/dina_from_xpu`**——也就是说重传由 `xpu` 直接改写 BRAM 里某些比特（比如把某字节标记成重传包），`tx_bit_intf` 让出写控制权。

再看写 BRAM 的三个关键状态——你会清楚地看到「PHY 头 + 净荷」是如何拼进 BRAM 的：

[ip/tx_intf/src/tx_bit_intf.v:620-635](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_bit_intf.v#L620-L635) —— `DO_PHY_HDR1`：写 BRAM 地址 0 = L-SIG 字段（`{速率, 长度, parity}`）。若非 HT 速率，直接把写指针设到 2（跳过头两字），进入 `DO_TX`。

[ip/tx_intf/src/tx_bit_intf.v:637-647](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_bit_intf.v#L637-L647) —— `DO_PHY_HDR2`：写 BRAM 地址 1 = HT-SIG 字段（`{ht_sig_crc_reg, ht_sig_data}`，其中 CRC 由 `ht_sig_crc_calc` 算出，见 4.4）。

[ip/tx_intf/src/tx_bit_intf.v:649-662](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_bit_intf.v#L649-L662) —— `DO_TX`：核心数据搬运。`wea_internal = wea_high = (read_from_s_axis_en & emptyn_from_s_axis)`，即只要 s_axis FIFO 非空就往 BRAM 写一拍；`addra_internal = wr_counter` 递增；`dina_internal = data_from_s_axis`（直接来自 s_axis FIFO 的 64bit 数据）。写满 `2 + len_pkt_sym - 1` 个字后停止读 FIFO；收到 `tx_end_from_acc`（openofdm_tx 吐完）后回 `WAIT_TO_TRIG`。

最后看 `phy_tx_start` 是怎么产生的：

[ip/tx_intf/src/tx_bit_intf.v:242](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_bit_intf.v#L242) —— `start`（即顶层 `phy_tx_start`）只在 `auto_start_mode`（`slv_reg2[3]`）打开时由 `start_delay0..5` 之或产生。

[ip/tx_intf/src/tx_bit_intf.v:729-734](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_bit_intf.v#L729-L734) —— `start_delay0` 的产生条件分三种：发 ACK 时用 `start_tx_ack`；重传时用 `start_retrans`；正常发包时用 `addra == num_dma_symbol_th && num_dma_symbol_th != 0`（BRAM 攒够阈值深度）。后续 `start_delay1..5` 把这个脉冲展宽成多拍。

`s_axis` 这一侧如何把 DMA 数据送进 4 条 FIFO，可以看 `tx_intf_s_axis`：

[ip/tx_intf/src/tx_intf_s_axis.v:90-98](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_intf_s_axis.v#L90-L98) —— 4 路 FIFO 的写使能。PS 用 `tx_queue_idx_indication_from_ps`（`slv_reg8[19:18]`）指明当前 DMA 数据进哪个队列（0..3，对应 802.11 的 WMM/AC 优先级队列），只有被选中的那路 FIFO 写使能才有效。

[ip/tx_intf/src/tx_intf_s_axis.v:109-153](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_intf_s_axis.v#L109-L153) —— 简单的 IDLE / WRITE_FIFO 状态机 + `write_pointer`，数够 `S_AXIS_NUM_DMA_SYMBOL`（`slv_reg8[12:0] - 1`）或收到 `S_AXIS_TLAST` 即结束本包。

#### 4.2.4 代码实践

**实践目标**：亲手把「DMA → BRAM → openofdm_tx」这条链路在源码里走一遍，并确认 BRAM 是双方共享的。

**操作步骤**：

1. 在 [tx_bit_intf.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_bit_intf.v) 里找到 `DO_TX` 状态（L649），确认 `data_from_s_axis` → `dina_internal` → BRAM 端口 A 的 `dina`（L1141）。
2. 看端口 B：`addrb = bram_addr`（L1154，来自 openofdm_tx），`doutb`（L1158）→ `bram_data_to_acc_int` → 顶层 `data_to_acc`（经 L248 的 mux）→ 接到 openofdm_tx 的 `bram_din`。
3. 打开 [openwifi_ip.tcl](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/openwifi_ip.tcl) 第 311 行和 333 行，确认 block design 里确实把 `openofdm_tx_0/bram_addr` 接到 `tx_intf_0/bram_addr`、把 `tx_intf_0/data_to_acc` 接到 `openofdm_tx_0/bram_din`。

**需要观察的现象**：BRAM 的写端口（A）和读端口（B）分属两个 IP——`tx_intf` 写、`openofdm_tx` 读，但物理上是同一块 `xpm_memory_tdpram`。

**预期结果**：你能在源码与 Tcl 里双向验证「BRAM 是 tx_intf 与 openofdm_tx 之间的共享缓存」这一结论，数据流方向为 `s_axis FIFO → BRAM(A 写) → openofdm_tx(B 读)`。

> 待本地验证：因综合/实现需 Vivado 与板卡，本实践为源码阅读型，不要求跑通。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `phy_tx_start` 要等 `addra == num_dma_symbol_th` 才发，而不是 DO_TX 一进入就发？

> **答案**：为了保证 `openofdm_tx` 被触发后去读 BRAM 时不会读空。先往 BRAM 攒够 `num_dma_symbol_th` 深度的数据再启动读取方，是典型的「写快于读、先蓄水再放水」的缓冲设计。

**练习 2**：重传（`retrans_in_progress`）时，BRAM 端口 A 的写控制权交给谁？

> **答案**：交给 `xpu`（见 L245–L247）。此时 `wea/addra/dina` 改用 `wea_from_xpu/addra_from_xpu/dina_from_xpu`，由 xpu 直接改写 BRAM 中某些比特以标记重传，`tx_bit_intf` 的状态机让出写权。

**练习 3**：s_axis 一侧为什么有 4 条 FIFO？

> **答案**：对应 802.11 的 4 个发送队列（WMM 的 VO/VI/BE/BK 四种 AC）。PS 在 `slv_reg8[19:18]` 里指明当前包进哪条队列，`tx_bit_intf` 在 `WAIT_TO_TRIG` 里按优先级（`tx_config_fifo_empty` + `slice_en`）选一个队列发包。

---

### 4.3 DAC 接口与跨时钟域：tx_iq_intf 与 dac_intf

#### 4.3.1 概念说明

`openofdm_tx` 吐出的 I/Q 样点（`rf_i_from_acc/rf_q_from_acc`）还要经过两道关才能到 DAC：

1. **`tx_iq_intf`**：给 I/Q 乘一个可调的**基带增益** `bb_gain`（数字域的粗增益控制），再用一条 FIFO 做缓冲，并产生 `tx_hold` 反压信号回送给 `openofdm_tx`（「别再吐了，我这边 FIFO 快满了」）。FIFO 输出再过 `csi_fuzzer`（见 4.4）。
2. **`dac_intf`**：把基带域（`s00_axis_aclk`，~100/200MHz）的 I/Q **跨时钟域**搬到 DAC 域（`dac_clk`，40MHz），并按天线/CDD 模式把 I/Q 拼成 64bit 的 `dac_data`。

这里有个采样率问题：openwifi 基带是 20Msps（每秒 20 万个 I/Q 样点，见 u2-l4 的 `SAMPLING_RATE_MHZ=20`），而 AD9361 的 DAC 数据通路是 40Msps。`dac_intf` 用一个 toggle 的 `dac_phase` 信号做 2:1 的速率适配。

#### 4.3.2 核心流程

```
rf_i/rf_q (from openofdm_tx, 16bit 有符号)
   │  × bb_gain (slv_reg13[9:0], Q7 定点)
   ▼
rf_i_tmp/rf_q_tmp (26bit) → 取高 16bit (>>7)
   │  写入 tx_iq FIFO (depth 512)
   ▼
tx_iq_fifo_out ──► csi_fuzzer ──► wifi_iq_pack (20Msps 基带)
   │
   │  (data_count > tx_hold_threshold ? tx_hold=1 : 0)  ──反压──► openofdm_tx.result_iq_hold
   ▼
dac_intf: CDC 到 40MHz + 天线/CDD 选择
   │
   ▼
dac_data (64bit) → AD9361 DAC
```

增益的数学：`bb_gain` 是 10bit **有符号**数，按 Q7 定点理解，即真实增益 \( g = \text{bb\_gain} / 128 \)。代码里 `rf_i_tmp = rf_i * bb_gain`（26bit），再取 `rf_i_tmp[22:7]`，等价于

\[
\text{out} = \text{round}(\text{rf\_i} \cdot \text{bb\_gain} / 128)
\]

所以 `bb_gain = 128`（即 `0x080`）对应增益 \(1.0\)（单位增益），`bb_gain = 256` 对应 \(2.0\)（放大 6dB）。

#### 4.3.3 源码精读

[ip/tx_intf/src/tx_iq_intf.v:110-122](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_iq_intf.v#L110-L122) —— 增益乘法。`rf_i_tmp <= rf_i * bb_gain; rf_q_tmp <= rf_q * bb_gain;`，写 FIFO 的使能是 `~tx_hold & rf_iq_valid`（被反压时不写）。

[ip/tx_intf/src/tx_iq_intf.v:85](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_iq_intf.v#L85) —— 背压产生：`tx_hold = (data_count > tx_hold_threshold ? 1 : 0)`。`tx_hold_threshold` 由 `slv_reg12[9:0]` 配置。这个 `tx_hold` 顶层层输出，最终接到 `openofdm_tx` 的 `result_iq_hold`（见 openwifi_ip.tcl 第 337 行），让 openofdm_tx 暂停生成 I/Q。

[ip/tx_intf/src/tx_iq_intf.v:95-96](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_iq_intf.v#L95-L96) —— FIFO 输入选择：正常模式把 `{rf_q_tmp[22:7], rf_i_tmp[22:7]}`（高 16bit，即 >>7）写入 FIFO；arbitrary IQ 模式（调试用，PS 直接写 `slv_reg1` 产生任意 I/Q）则写 `tx_arbitrary_iq_in`。

[ip/tx_intf/src/tx_iq_intf.v:163-205](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_iq_intf.v#L163-L205) —— `xpm_fifo_sync` 单时钟 FIFO（depth 512，FWFT 模式），用 `rd_data_count` 做 `data_count` 实现上面的背压。注意它同时输出 `tx_iq_fifo_empty` 给 LBT（先听后说）等逻辑用。

再看 DAC 侧的跨时钟域与拼字：

[ip/tx_intf/src/dac_intf.v:60-65](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/dac_intf.v#L60-L65) —— `dac_data` 拼装：根据 `ant_flag`（天线 0/1 选择）把 32bit 的 `dac_data_internal`（I+Q）放到 64bit 的高半或低半；`simple_cdd_flag` 实现 CDD（循环延迟分集）把同一样点放到两天线。`dac_valid = 1`（DAC 始终要 40Msps 数据，靠 `dac_phase` 做 2:1）。

[ip/tx_intf/src/dac_intf.v:69-87](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/dac_intf.v#L69-L87) —— DAC 域（40MHz）进程。`dac_phase` 每 DAC 时钟翻转一次；`data_from_acc` 经两级寄存器（`stage1/stage2`）同步到 DAC 域；`dac_data_internal = (dac_phase ? stage2 : 0)`，即每两个 DAC 拍输出一次有效样点 + 一次 0，把 20Msps「拉伸」成 40Msps。

[ip/tx_intf/src/dac_intf.v:101-141](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/dac_intf.v#L101-L141) —— 三处 `xpm_cdc_array_single` 跨时钟域同步：把 acc 域的 `ant_flag`、`simple_cdd_flag` 同步到 DAC 域；把 DAC 域的 `dac_phase` 同步回 acc 域产生 `read_bb_fifo` 读脉冲。这正是 u2-l1 提到的 `xpm_cdc` 用法。

#### 4.3.4 代码实践

**实践目标**：理解「20Msps 基带 → 40Msps DAC」的速率适配是怎么做到的。

**操作步骤**：

1. 打开 [dac_intf.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/dac_intf.v)，定位 L82 的 `dac_phase <= (~dac_phase)`。
2. 结合 L65 的 `dac_data_internal = (dac_phase ? data_from_acc_stage2 : 0)`，画一个时序表：DAC 时钟拍 0/1/2/3 对应 `dac_phase` 的值与 `dac_data_internal` 的输出。

**需要观察的现象**：`dac_phase` 在 0、1 之间交替；只有 `dac_phase==1` 的那一拍才输出真实样点，另一拍输出 0。

**预期结果**：每两个 DAC 拍消耗一个 20Msps 基带样点，等效把基带样点率翻倍到 40Msps，与 AD9361 DAC 数据通路（`util_ad9361_dac_upack`）对齐。

> 待本地验证：精确波形建议在 Vivado 仿真里抓 `dac_clk/dac_phase/dac_data` 观察。

#### 4.3.5 小练习与答案

**练习 1**：`bb_gain = 128` 对应多少 dB 增益？

> **答案**：\(128/128 = 1.0\)，即 0dB（单位增益，不放大不衰减）。

**练习 2**：`tx_hold` 信号起什么作用？最终接到哪里？

> **答案**：当 `tx_iq_intf` 的 FIFO 里数据量超过 `tx_hold_threshold` 时拉高，反压 `openofdm_tx`（接其 `result_iq_hold`），让它暂停生成 I/Q，防止 FIFO 溢出。这是一个典型的「下游背压上游」流控。

**练习 3**：为什么 `dac_valid` 恒为 1，而不是按 20Msps 节拍拉高？

> **答案**：AD9361 的 DAC 数据通路固定按 40Msps 取数，必须每拍都给数据；20→40 的速率适配通过「奇偶拍交替输出真实样点与 0」实现，而不是靠 `dac_valid` 选通。

---

### 4.4 状态 FIFO 与 CSI 辅助：tx_status_fifo / ht_sig_crc_calc / csi_fuzzer

#### 4.4.1 概念说明

最后三个子模块都是「辅助」性质，但各司其职、缺一不可：

- **`tx_status_fifo`**：每发完一包，`xpu` 会送来一个 80bit 的 `tx_status`（重传次数、Block-Ack 序号与位图等）。PS 需要异步地、按包读走这些结果来更新驱动状态。`tx_status_fifo` 用 4 条 FIFO 把每包结果缓存起来，PS 通过读 `slv_reg22~25` 取走。
- **`ht_sig_crc_calc`**：802.11n 的 HT-SIG 字段末尾带一个 CRC8（用于接收端校验 HT-SIG 是否正确）。`tx_bit_intf` 在 `PREP_PHY_HDR` 阶段启动它，算出的 CRC 写进 BRAM 的 HT-SIG 字（见 4.2 的 `DO_PHY_HDR2`）。
- **`csi_fuzzer`**：一个挂在 I/Q 输出路径上的 2 抽头 FIR，用 `bb_gain1/bb_gain2` 两个系数（可选旋转 90°）给发射信号注入**可控的多径畸变**。它不是正常通信必需的，而是研究/调试用——故意「弄脏」CSI（Channel State Information），用来测试接收机的信道估计与均衡。

#### 4.4.2 核心流程

**tx_status_fifo**：

```
xpu.tx_status[79:0] ──┬─ num_retrans      = tx_status[3:0]
                      ├─ blk_ack_resp_ssn = tx_status[15:4]
                      ├─ blk_ack_bitmap_low  = tx_status[47:16]
                      └─ blk_ack_bitmap_high = tx_status[79:48]
                              │
tx_try_complete ──延迟1拍─► wr_en（同时写 4 条 FIFO，各存不同字段 + linux_prio/pkt_cnt/...）
                              │
PS 读 slv_reg22/23/24/25 ──► rd_en（axi_araddr_core == 0x16/0x17/0x18/0x19）
                              │
                              ▼
                       tx_status_out1..4（空时 out1=0xFFFFFFFF 作哨兵值）
```

**ht_sig_crc_calc**：标准的 CRC8 串行计算，逐比特（共 34 比特）移位，初值 `0xFF`，结束取反，35 拍出结果。

**csi_fuzzer**：输出 = 当前样点 + 1 拍前样点×增益1 + 2 拍前样点×增益2（增益可选 90° 旋转），等价一个人工多径信道。

#### 4.4.3 源码精读

[ip/tx_intf/src/tx_status_fifo.v:66-73](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_status_fifo.v#L66-L73) —— 把 `tx_status[79:0]` 拆成 4 段：`num_retrans`（低 4 位，实际重传次数）、`blk_ack_resp_ssn`（Block-Ack 起始序号）、`blk_ack_bitmap_low/high`（Block-Ack 位图，每位对应一个子帧是否成功）。

[ip/tx_intf/src/tx_status_fifo.v:128](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_status_fifo.v#L128) —— FIFO1 的写数据拼装：`{cw_delay1, num_slot_random[8:0], linux_prio, tx_queue_idx, 4'd0, bd_wr_idx, 1'd0, num_retrans}`，把退避参数（CW、随机 slot 数）、队列优先级、buffer 描述符索引、重传次数打包成一拍。

[ip/tx_intf/src/tx_status_fifo.v:131](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_status_fifo.v#L131) 与 [L135](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_status_fifo.v#L135) —— FIFO1 读使能 `= slv_reg_rden && (axi_araddr_core == 5'h16)`，对应 PS 读 `slv_reg22`；写使能 `= tx_try_complete_reg`（`tx_try_complete` 延迟 1 拍）。FIFO2/3/4 同理对应 `0x17/0x18/0x19`（`slv_reg23/24/25`）。

[ip/tx_intf/src/tx_status_fifo.v:60-63](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_status_fifo.v#L60-L63) —— 读出时若 FIFO 空，`out1` 给 `0xFFFFFFFF`、其余给 0，作为「没有新状态」的哨兵值，PS 据此判断是否有效。

[ip/tx_intf/src/ht_sig_crc_calc.v:20-60](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/ht_sig_crc_calc.v#L20-L60) —— HT-SIG 的 CRC8 串行计算。`start` 时装载 34bit 数据、`c` 初值 `0xFF`；之后每拍处理 1 bit（`temp = c[7]^data[i]`，按生成多项式反馈到 `c[2]/c[1]/c[0]`）；到 `i==34` 时 `valid` 拉高、输出 `crc = ~{c[0..7]}`（取反映 RFC 习惯）。

[ip/tx_intf/src/ht_sig_crc_calc.v:292-300](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_bit_intf.v#L292-L300) —— `tx_bit_intf` 里例化 `ht_sig_crc_calc`（输入 `ht_sig_data`，输出 `ht_sig_crc`），算完后存进 `ht_sig_crc_reg` 供 `DO_PHY_HDR2` 写进 BRAM。

[ip/tx_intf/src/csi_fuzzer.v:40-41](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/csi_fuzzer.v#L40-L41) —— 把输入 `iq` 拆成 `i0/q0`。

[ip/tx_intf/src/csi_fuzzer.v:63-70](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/csi_fuzzer.v#L63-L70) —— 2 抽头 FIR 核心：`i1/q1`、`i2/q2` 是延迟 1、2 拍的历史样点；`tap1 = i1*bb_gain1`（或 `-q1*bb_gain1` 当 `rot90`），`tap2` 同理；`iq_out = i0 + tap1 + tap2`。`rot90` 标志把 `(i,q)` 旋转 90°（`(-q, i)`），用于构造特定的复数信道。这个模块在 `tx_iq_intf` 里例化（见 [tx_iq_intf.v:125-144](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_iq_intf.v#L125-L144)），位于 FIFO 输出之后、`wifi_iq_pack` 之前。

#### 4.4.4 代码实践

**实践目标**：搞清 `tx_status` 的用途与 PS 读取方式（这是本讲总实践任务的后半段）。

**操作步骤**：

1. 打开 [tx_status_fifo.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_status_fifo.v)，确认 4 条 FIFO 的写使能都是 `tx_try_complete_reg`、读使能分别是 `axi_araddr_core == 0x16/0x17/0x18/0x19`。
2. 回到 [tx_intf.v:385-409](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_intf.v#L385-L409)，确认这 4 条 FIFO 的输出分别挂到 `slv_reg22/23/24/25`。

**需要观察的现象**：每来一个 `tx_try_complete`（一包发完/尝试结束），4 条 FIFO 同时各写入一拍；PS 随后按地址 `0x16~0x19` 读 4 个寄存器，即可拿到这一包的完整状态。

**预期结果**：你会得出 `tx_status` 的用途——**它是 PS 驱动获取「每包发送结果」（重传了几次、Block-Ack 哪些子帧成功）的回读通道**，PS 据此更新 mac80211 的速率控制与重传队列。

> 待本地验证：实际寄存器布局请结合 openwifi 软件仓库的驱动头文件对照。

#### 4.4.5 小练习与答案

**练习 1**：PS 怎么知道某个 `slv_reg22` 读出来的是「有效状态」还是「没有新包」？

> **答案**：看 FIFO 空标志。FIFO1 空时 `tx_status_out1` 给哨兵值 `0xFFFFFFFF`（L60），PS 读到这个值即知无新状态；FIFO2/3/4 空时给 0。

**练习 2**：`csi_fuzzer` 在正常 Wi-Fi 通信里必须开启吗？

> **答案**：不必。它用于研究/调试，故意给发射 I/Q 注入可控多径畸变以测试接收端 CSI 估计。正常通信时把 `bb_gain1/bb_gain2` 设为 0（它们来自 `slv_reg5`，见 [tx_intf.v:543-546](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_intf.v#L543-L546)），`iq_out` 就等于原始 `i0/q0`，相当于直通。

**练习 3**：`ht_sig_crc_calc` 算一次 CRC 需要多少个时钟周期？

> **答案**：`start` 后进入 `busy`，逐拍处理 1 bit，`i` 从 0 计到 34，第 35 拍（`i==34`）出 `valid`。所以约需 35 个时钟周期。

---

## 5. 综合实践

**任务**：把本讲整条发射数据流在源码里完整走一遍，并回答「`tx_status` 的用途」。

请按下列顺序在源码里追踪一帧待发数据，并在每一步给出对应的文件、行号与一句话说明：

1. **入口**：PS 经 DMA 把 64bit 数据以 AXI-Stream 送入 `s00_axis`。指出 `tx_intf_s_axis` 如何按 `slv_reg8[19:18]` 把数据分发到 4 条 FIFO（[tx_intf_s_axis.v:90-93](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_intf_s_axis.v#L90-L93)）。
2. **入 BRAM**：`tx_bit_intf` 在 `DO_TX` 状态把 FIFO 数据写入 BRAM 端口 A（[tx_bit_intf.v:649-662](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_bit_intf.v#L649-L662)）。
3. **触发**：BRAM 攒够深度后产生 `phy_tx_start` 脉冲（[tx_bit_intf.v:729](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_bit_intf.v#L729)），启动 `openofdm_tx`。
4. **被消费**：`openofdm_tx` 驱动 `bram_addr` 从 BRAM 端口 B 读字节（[tx_bit_intf.v:1153-1158](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_bit_intf.v#L1153-L1158)），生成 I/Q 回送（`rf_i_from_acc/rf_q_from_acc`）。
5. **增益与缓冲**：`tx_iq_intf` 乘 `bb_gain`、过 FIFO、经 `csi_fuzzer` 得到 `wifi_iq_pack`（[tx_iq_intf.v:110-122](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_iq_intf.v#L110-L122)）。
6. **到 DAC**：`dac_intf` 跨时钟域并按天线/CDD 拼成 `dac_data` 送往 AD9361（[dac_intf.v:60-65](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/dac_intf.v#L60-L65)）。
7. **回读状态**：发完后 `xpu` 送 `tx_status[79:0]`，经 `tx_status_fifo` 缓存，PS 读 `slv_reg22~25` 取走（[tx_status_fifo.v:60-73](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/tx_intf/src/tx_status_fifo.v#L60-L73)）。

**交付物**：一张数据流图（自画）+ 一句话回答「`tx_status` 的用途」。

> 参考答案：`tx_status` 是 PS 驱动获取**每包发送结果**（重传次数 `num_retrans`、Block-Ack 起始序号与位图）的回读通道，由 `tx_status_fifo` 按 FIFO 缓存，PS 读 `slv_reg22~25` 消费，用于 mac80211 的速率控制与重传决策。

## 6. 本讲小结

- `tx_intf` 是发射链路的「口岸」，顶层只做子模块装配，真正逻辑在 7 个子模块里。
- **BRAM 是核心缓存**：物理上在 `tx_bit_intf` 里（`xpm_memory_tdpram`），端口 A 由 `tx_bit_intf` 写，端口 B 由 `openofdm_tx` 读——它把「PS 推数据」与「openofdm_tx 拉数据」解耦。
- `tx_bit_intf` 的 `high_tx_ctl_state` 状态机负责：选队列 → 等 CSMA → 写 PHY 头（L-SIG/HT-SIG）→ 写净荷 → 在 BRAM 攒够深度时发 `phy_tx_start`。
- `s_axis` 侧用 **4 条 FIFO** 对应 4 个 WMM 发送队列；`tx_bit_intf` 在 `WAIT_TO_TRIG` 按优先级与 `slice_en` 选队。
- I/Q 回来后经 `tx_iq_intf`（`bb_gain` 增益 + FIFO 背压 `tx_hold` + `csi_fuzzer`）再到 `dac_intf`（CDC 到 40MHz + 天线/CDD 选择）送达 DAC。
- 三个辅助模块：`tx_status_fifo`（每包发送结果回读）、`ht_sig_crc_calc`（HT-SIG 的 CRC8）、`csi_fuzzer`（研究用的人工多径 FIR）。

## 7. 下一步学习建议

- **控制侧**：本讲里反复出现的 `slice_en`、`backoff_done`、`retrans_in_progress`、`tx_try_complete`、`tx_status` 都来自 `xpu`。要真正理解「何时允许发射、重传怎么决策」，请进入 u5 单元，尤其是 **u5-l3（TX 控制、重传与 ACK）**。
- **CSMA/CA**：`WAIT_CHANCE` 状态等待的 `backoff_done` 由 `csma_ca.v` 产生，详见 **u5-l2**。
- **寄存器细节**：本讲多次提到 `slv_reg0/2/5/7/8/11/12/13/14/22~25`，它们的具体比特含义与 AXI4-Lite 从设备实现见 **u7-l1（AXI 寄存器映射与软件交互）**。
- **跨时钟域原理**：`xpm_cdc_*`、`xpm_fifo_sync` 的底层机制可结合 Xilinx `UG974`/`PG057` 文档进一步学习。
