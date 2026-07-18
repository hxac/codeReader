# TSF 定时器与接收包解析/过滤

> 所属单元：u5 控制核心 xpu 与低层 MAC
> 前置讲义：u5-l1 xpu 控制核心总览
> 本讲等级：intermediate

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清楚 `tsf_timer.v` 如何用一个小计数器生成 **每 1 微秒一拍** 的 `tsf_pulse_1M` 心跳，以及它如何被软件「装载（load）」来与 AP 的信标时间戳同步。
2. 看懂 `phy_rx_parse.v` 如何把 `openofdm_rx` 吐出的**字节流**按字节序号逐字节拼出 802.11 MAC 头部字段（Frame Control、Duration、三个/四个地址、Sequence Control、QoS、Block Ack 字段）。
3. 说清楚 `pkt_filter_ctl.v` 如何根据软件下发的 `filter_cfg`（对应 mac80211 的 `FIF_*` 标志）与解析出的地址，逐帧决定 `block_rx_dma_to_ps` 的取值——也就是「这帧要不要送进 PS（ARM）」。
4. 在 `xpu.v` 里把这三个模块的接线关系画出来，并解释软件寄存器（`slv_reg`）如何介入这条链路。

本讲是 u5 单元的「信息提取 + 时间基准」专题：TSF 给出**时间**，`phy_rx_parse` 给出**语义**，`pkt_filter_ctl` 据此做出**放行/丢弃**的决策。三者共同构成了低层 MAC「看懂一帧、过滤一帧、为它打时间戳」的能力。

---

## 2. 前置知识

在进入源码前，先用通俗语言建立几个概念。

### 2.1 为什么需要 TSF（Timing Synchronization Function）

Wi-Fi 是一个「时分」系统：谁能在信道上发包、退避多久、ACK 在几个微秒后回、信标（Beacon）何时出现，都依赖一个**全网同步的时钟**。802.11 规定每个站点维护一个 **64 位的 TSF 计时器**，单位是 1 微秒（µs），最大可计数约 5.8 万年才溢出。AP 周期性在 Beacon 里广播自己的 TSF 值，站点收到后把本地 TSF「对齐」过去。

在 openwifi 的 FPGA（PL）里，这个计时器由 `tsf_timer.v` 实现；它的 1µs 脉冲 `tsf_pulse_1M` 同时是 CSMA/CA 退避、SIFS/DIFS 计时、队列时分（time slice）等多个子模块的**统一心跳**（见 u5-l2）。

### 2.2 802.11 MAC 帧头长什么样

一个典型的数据帧，前 24 字节（QoS 帧再 +2）是 MAC 头：

| 字节序号 | 字段 | 长度 |
|---|---|---|
| 0–1 | Frame Control (FC) | 2 |
| 2–3 | Duration / ID | 2 |
| 4–9 | Address 1（接收地址 RA） | 6 |
| 10–15 | Address 2（发送地址 TA，ACK 回给它） | 6 |
| 16–21 | Address 3（目的地址 DA 或 BSSID，视 ToDS/FromDS 而定） | 6 |
| 22–23 | Sequence Control (SC) | 2 |
| 24–29 | Address 4（仅 ToDS=FromDS=1 的 WDS 帧） | 6 |
| 24–25 或 30–31 | QoS Control（仅 QoS 数据帧） | 2 |

`phy_rx_parse.v` 的工作就是**逐字节**把这些字段从 `openofdm_rx` 解码出的字节流里「抠」出来。关键字段 `FC` 里的 `Type/Subtype` 决定这是数据帧、管理帧还是控制帧；`ToDS/FromDS` 两位决定三个地址各自扮演 DA/SA/BSSID 中的哪一个。

### 2.3 为什么要硬件做地址过滤

`openofdm_rx` 把空口上收到的一切帧都解成字节流。如果不加过滤，所有帧（包括邻居 AP 的 Beacon、发给别人的单播、组播）都会经 AXI DMA 写进 DDR、再中断通知 ARM。这会**白白占用 DMA 带宽与 CPU**。

Linux mac80211 在软件层有一套 `FIF_*` 过滤标志（`FIF_OTHER_BSS`、`FIF_CONTROL`、`FIF_ALLMULTI`……）。openwifi 把这套标志**下沉到硬件**：驱动把 `filter_cfg` 写进 xpu 寄存器，`pkt_filter_ctl.v` 在帧到达 DMA 之前就用组合逻辑判定要不要拦截（`block_rx_dma_to_ps=1` 表示拦截）。这就是「硬件层 MAC 地址过滤」。

> 名词速查：**PS**（Processing System，Zynq 的 ARM 核）、**PL**（可编程逻辑，FPGA）、**DMA**（直接内存访问，这里指 AXI DMA 把帧搬进 DDR）、**BSSID**（基本服务集标识，即 AP 的 MAC）、**mac80211**（Linux 内核里管理 802.11 MAC 的子层）。

---

## 3. 本讲源码地图

本讲涉及 4 个文件，都在 `ip/xpu/src/` 下：

| 文件 | 作用 | 本讲定位 |
|---|---|---|
| [tsf_timer.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tsf_timer.v) | 64 位 TSF 计时器，产出 1µs 脉冲 `tsf_pulse_1M` 与运行值 `tsf_runtime_val` | 最小模块①：TSF 定时器 |
| [phy_rx_parse.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/phy_rx_parse.v) | 把字节流逐字节解析成 MAC 头字段 | 最小模块②：MAC 头解析 |
| [pkt_filter_ctl.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/pkt_filter_ctl.v) | 按地址 + `filter_cfg` 决定是否拦截 DMA | 最小模块③：地址过滤 |
| [xpu.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v) | 例化并连线上面三者，把结果接到 rx_intf / PS | 串起全链路的「主板」 |

另外会用到一份共享宏文件 [ip/board_def.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/board_def.v)（定义 `COUNT_TOP_1M`、`COUNT_SCALE`），它在 u2-l4 已讲过，本讲直接引用。

---

## 4. 核心概念与源码讲解

### 4.1 TSF 定时器：64 位微秒级同步心跳

#### 4.1.1 概念说明

`tsf_timer.v` 要解决一个问题：FPGA 基带时钟并不是 1MHz，而是 100MHz/200MHz/250MHz（由板卡决定，见 u2-l4 的 `NUM_CLK_PER_US`）。如何在任意基带时钟下，稳定地产出一个「每 1µs 一拍」的脉冲，并维护一个 64 位、每拍 +1 的计时器？

思路很朴素：用一个**小计数器**数时钟周期，数满 1µs 对应的周期数就「拍一下」、并让 64 位大计数器 +1；同时提供一个**装载口**，让软件能把 AP 的信标时间戳直接写进来实现同步。

#### 4.1.2 核心流程

设基带时钟每微秒有 \(N\) 个周期（即 `NUM_CLK_PER_US`），则：

\[
\text{COUNT\_TOP\_1M} = N - 1
\]

小计数器 `counter_1M` 在每个时钟周期 +1，数到 `COUNT_TOP_1M` 后归零。**归零的那个周期**就拉高 `tsf_pulse_1M` 一拍，同时 `tsf_runtime_val`（64 位）+1。于是：

\[
\text{tsf\_pulse\_1M 的周期} = (N) \text{ 个时钟} = 1\,\mu s,\qquad \text{TSF 分辨率} = 1\,\mu s
\]

装载（load）采用**下降沿触发**：软件把 `tsf_load_control` 从 1 拉回 0 的那一拍，把 `tsf_load_val` 一次性灌进 `tsf_runtime_val`，并把当拍的脉冲压掉（避免装载瞬间多 +1）。这正好对应「收到 Beacon → 写入 AP 的时间戳 → 本地 TSF 跳到该值」。

伪代码：

```
每拍：
  load_reg <= load_control              // 打一拍用于边沿检测
  if (counter==TOP) or (load 下降沿):
      counter <= 0
  else:
      counter <= counter + 1

  if (load 下降沿):
      pulse_1M <= 0
      runtime_val <= load_val           // 同步到 AP 时间戳
  else if (counter==0):
      pulse_1M <= 1
      runtime_val <= runtime_val + 1    // 正常 +1
  else:
      pulse_1M <= 0
```

#### 4.1.3 源码精读

**模块端口与参数**——默认位宽就是 802.11 规定的 64 位：

[ip/xpu/src/tsf_timer.v:7-18](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tsf_timer.v#L7-L18) — `TIMER_WIDTH=64`，输入 `tsf_load_control`（上升/下降沿用于装载）、`tsf_load_val`，输出 `tsf_runtime_val` 与 `tsf_pulse_1M`。

**计数与脉冲核心逻辑**：

[ip/xpu/src/tsf_timer.v:35-52](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tsf_timer.v#L35-L52) — 关键两段：

- 第 35–39 行：`counter_1M` 数到 `COUNT_TOP_1M` 归零；装载下降沿也强制归零。
- 第 41–52 行：装载下降沿把 `tsf_load_val` 灌入 `tsf_runtime_val` 并压掉脉冲；否则在 `counter_1M==0` 当拍输出 1µs 脉冲、`tsf_runtime_val` 自增。

注意它 `include "clock_speed.v"`，因此 `COUNT_TOP_1M` 取的是构建期根据板卡时钟生成的真值（见 [ip/board_def.v:12](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/board_def.v#L12)：`` `define COUNT_TOP_1M ((`NUM_CLK_PER_US)-1) ``）。

**xpu 如何例化与装载**：

[ip/xpu/src/xpu.v:745-754](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L745-L754) — 例化 `tsf_timer_i`：

- `tsf_load_control` 接 `slv_reg3[31]`（最高位当装载触发位）；
- `tsf_load_val` = `{slv_reg3, slv_reg2}`（高 32 位来自 `slv_reg3`，低 32 位来自 `slv_reg2`）；
- 产物 `tsf_pulse_1M` 与 `tsf_runtime_val` 都挂到 xpu 内部网络。

软件回读 TSF 当前值走两个只读寄存器：

[ip/xpu/src/xpu.v:375-376](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L375-L376) — `slv_reg58` = 低 32 位、`slv_reg59` = 高 32 位，驱动软件随时可读 64 位 TSF。

> 小细节：`slv_reg3[31]` 既是装载值的高字最高位，又是装载触发位。因此驱动装载时通常先写低 32 位到 `slv_reg2`，再把高字写入 `slv_reg3` 并令 bit31 由 1 变 0，用这次「下降沿」触发装载。

#### 4.1.4 代码实践

**实践目标**：验证 TSF 的「每微秒一拍」与装载机制。

**操作步骤（源码阅读型）**：

1. 打开 [ip/board_def.v:9-13](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/board_def.v#L9-L13)。
2. 假设板卡为 `zc706`、`NUM_CLK_PER_US=100`（100MHz），手算：`COUNT_TOP_1M = 99`，`NUM_CLK_PER_SAMPLE = 100/20 = 5`。
3. 在 [tsf_timer.v:35-52](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tsf_timer.v#L35-L52) 里跟踪 `counter_1M`：从 0 数到 99 共 100 拍，期间 `counter_1M==0` 仅在第 1 拍为真，故 `tsf_pulse_1M` 每 100 拍（=1µs）拉高一拍。

**需要观察的现象 / 预期结果**：

- 100MHz 下，`tsf_pulse_1M` 每 100 个时钟周期出现一次单拍高电平；`tsf_runtime_val` 每出现一次脉冲就 +1，即每微秒 +1。
- 若换到 `zcu102` 的 240MHz，则 `COUNT_TOP_1M=239`，脉冲周期变为 240 拍，仍是 1µs——**TSF 分辨率与板卡时钟无关**，这是把时序逻辑建在「数满 1µs」而非「固定周期数」上的好处。
- 装载：软件令 `slv_reg3[31]` 由 1→0，应看到 `tsf_runtime_val` 在下一拍跳变为 `{slv_reg3, slv_reg2}`。

> 波形层面结论为「待本地验证」（本仓库未提供 tsf_timer 的 testbench；若要仿真，可参照 u7-l3 介绍的 `ip/xpu/unit_test/mv_avg/mv_avg_tb.v` 用 `$fopen/$fscanf` 喂激励的方式自建一个最小测试台）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `counter_1M` 用 8 位寄存器（`reg [7:0]`）就够？

**参考答案**：`counter_1M` 最大要数到 `COUNT_TOP_1M = NUM_CLK_PER_US-1`。openwifi 支持的最高基带时钟是 UltraScale+ 的 250MHz，`250-1=249 < 256`，8 位（最大 255）足以覆盖所有板卡；对 100/200MHz 更是绰绰有余。

**练习 2**：装载为什么用**下降沿**而不是上升沿触发？

**参考答案**：装载值 `tsf_load_val` 由 `slv_reg2/slv_reg3` 提供，而 `slv_reg3[31]` 本身又是触发位。若用上升沿触发，软件写 `slv_reg3`（bit31=1）的同一拍寄存器值尚未稳定，存在竞争；用下降沿（bit31 由 1→0）触发时，高字数据已在前一拍写稳，能可靠采样到完整 64 位目标值，并可在当拍把脉冲压掉、避免装载后立即多 +1。

---

### 4.2 接收帧 MAC 头解析：phy_rx_parse

#### 4.2.1 概念说明

`openofdm_rx` 解出的是一条**字节流**：每个有效字节附带一个递增的序号 `byte_count`（接进模块的 `ofdm_byte_index`）和一个选通 `byte_in_strobe`（接进 `ofdm_byte_valid`）。字节 0 就是 MAC 帧的 Frame Control 第一字节（PHY 的 SIGNAL 头已被 `openofdm_rx` 剥掉）。

`phy_rx_parse.v` 的工作是：**按 `ofdm_byte_index` 把每个字节「装配」到正确的字段位**，并在每个字段装配完成的那拍给一个 `_valid` 选通脉冲。它不算法、不判过滤，只负责「把字节流变成有名字的字段」。后续的过滤（4.3）、ACK 回送（u5-l3）、Block Ack 处理都消费它的输出。

#### 4.2.2 核心流程

整体是一个对 `ofdm_byte_index` 的大 `if-else if` 链，序号到字段的映射固定：

```
index 0..3   -> FC_DI[31:0]   (FC 2B + Duration 2B)，index==3 时拉 FC_DI_valid
index 4..9   -> rx_addr(addr1)，index==9 时拉 rx_addr_valid
index 10..15 -> tx_addr(addr2)，index==15 时拉 tx_addr_valid
index >=16   -> 按 Type/Subtype 分三支：
   (a) Block Ack Request (type=1,subtype=8)：16..17 BAR Control，18..19 BAR SSC
   (b) Block Ack Response(type=1,subtype=9)：18..19 SSN，20..27 Bitmap
   (c) 其余（数据/管理）：16..21 addr3，22..23 SC；
        若 ToDS=FromDS=1（WDS）：24..29 addr4，30..31 QoS
        否则（QoS 数据）：24..25 QoS
```

要点：

- 各 `_valid` 是**单字节宽的脉冲**：在字段最后一字节装配当拍置 1，下一字节装配时（下一个 `index` 分支）清 0。
- WDS 帧（`FC_DI[9:8]==2'b11`）多一个 addr4，QoS 字段顺延到 30–31；非 WDS 的 QoS 帧，QoS 字段在 24–25。
- Block Ack 相关字段（`blk_ack_req_*`、`blk_ack_resp_*`）专供 u5-l3 的 tx_control 回送 Block Ack / 解析收到的 Block Ack 使用。

#### 4.2.3 源码精读

**端口——一口气声明了它能提取的全部字段**：

[ip/xpu/src/phy_rx_parse.v:5-56](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/phy_rx_parse.v#L5-L56) — 输入字节流三件套（`ofdm_byte_index/ofdm_byte/ofdm_byte_valid`），输出 FC、addr1/addr2/addr3/addr4、SC、Block Ack 字段、QoS 字段，每项都配一个 `_valid`。

**FC + Duration（index 0–3）**：

[ip/xpu/src/phy_rx_parse.v:102-114](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/phy_rx_parse.v#L102-L114) — index 0/1 装配 FC 低 16 位，index 2/3 装配 Duration（高 16 位），并在 index==3 拉高 `FC_DI_valid`。于是下游在第 4 字节到来前就能拿到完整的 32 位 `FC_DI`（含 FC 与 Duration/ID）。

**地址 1 / 地址 2（index 4–15）**：

[ip/xpu/src/phy_rx_parse.v:117-158](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/phy_rx_parse.v#L117-L158) — `rx_addr`（addr1）在 index 4–9 装配，index==9 拉高 `rx_addr_valid`；`tx_addr`（addr2）在 index 10–15 装配，index==15 拉高 `tx_addr_valid`。注意 `FC_DI_valid` 在 index==4 被清零——典型的「脉冲只亮一字节」。

**地址 3 + Sequence Control（index 16–23，普通数据/管理帧分支）**：

[ip/xpu/src/phy_rx_parse.v:232-263](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/phy_rx_parse.v#L232-L263) — `dst_addr`（addr3）在 16–21 装配，index==21 拉高 `dst_addr_valid`；`SC`（序号控制）在 22–23 装配，index==23 拉高 `SC_valid`。其中 `SC[3:0]` 是片段号、`SC[15:4]` 是 12 位序号（见 xpu 里 [xpu.v:372-373](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L372-L373) 的拆分）。

**Block Ack Request / Response 分支**：

[ip/xpu/src/phy_rx_parse.v:160-230](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/phy_rx_parse.v#L160-L230) — 通过 `FC_DI[3:2]==2'b01 && FC_DI[7:4]==4'b1000`（Block Ack Req）/ `==4'b1001`（Block Ack）分流，分别提取 BAR 的 Control/SSC 与 Block Ack 的 SSN/64 位 Bitmap，供 u5-l3 的聚合重传使用。

**地址 4 + QoS（WDS 与非 WDS 两种排布）**：

[ip/xpu/src/phy_rx_parse.v:265-320](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/phy_rx_parse.v#L265-L320) — 仅当 `FC_DI[9:8]==2'b11`（ToDS=FromDS=1，WDS 四地址帧）才装配 addr4（index 24–29），其 QoS 在 30–31；否则 QoS 在 24–25。`qos_tid` 取 `ofdm_byte[3:0]`，`qos_ack_policy` 取 `ofdm_byte[6:5]`。

**xpu 里的复位与连线**：

[ip/xpu/src/xpu.v:646-689](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L646-L689) — 注意复位端接的是 `s00_axi_aresetn & (~slv_reg0[3]) & (~pkt_header_valid_strobe)`：除了全局复位和软件复位（`slv_reg0[3]`），**每个新帧的 `pkt_header_valid_strobe` 都会把解析器复位一次**，保证字节序号从 0 重新对齐。模块输出的 `rx_addr→addr1`、`tx_addr→addr2`、`dst_addr→addr3`、`src_addr→addr4`。

xpu 还从 `FC_DI` 拆出更细的位字段，供全 MAC 使用：

[ip/xpu/src/xpu.v:355-365](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L355-L365) — `FC_type=FC_DI[3:2]`、`FC_subtype=FC_DI[7:4]`、`FC_to_ds=FC_DI[8]`、`FC_from_ds=FC_DI[9]`、`FC_retry=FC_DI[11]`、`duration=FC_DI[31:16]` 等，正是 4.3 节过滤逻辑的输入。

#### 4.2.4 代码实践

**实践目标**：给定一个真实的数据帧字节序，预测每个 `_valid` 在哪个 `ofdm_byte_index` 拉高。

**操作步骤（源码阅读型）**：

1. 假设收到一个 **From DS 的 QoS 数据帧**（AP 下发，`ToDS=0, FromDS=1`），即 `FC_DI[9:8]==2'b01`。
2. 列出 `ofdm_byte_index` 与对应字段、`_valid` 拉高拍：

| index | 字段 | _valid 拉高？ |
|---|---|---|
| 0–3 | FC + Duration | index=3 → `FC_DI_valid` |
| 4–9 | addr1 (RA=本站 DA) | index=9 → `rx_addr_valid` |
| 10–15 | addr2 (TA=BSSID) | index=15 → `tx_addr_valid` |
| 16–21 | addr3 (SA) | index=21 → `dst_addr_valid` |
| 22–23 | SC | index=23 → `SC_valid` |
| 24–25 | QoS Control | index=24 → `qos_tid_valid` |

3. 注意：因为 `FC_DI[9:8]` 是 `2'b01`（不是 `2'b11`），代码走 [phy_rx_parse.v:305-320](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/phy_rx_parse.v#L305-L320) 的非 WDS 分支，**不**装配 addr4。

**预期结果**：每个字段的有效选通都在该字段最后一字节到达的**当拍**（次拍即被清零）。下游（`pkt_filter_ctl`、`tx_control`）正是靠捕捉这些单拍脉冲来锁存地址、启动 ACK 的。

> 波形层面为「待本地验证」：仓库未提供 phy_rx_parse 的 testbench，可仿照 u7-l3 的 `mv_avg_tb.v` 自建激励（用 `$fwrite` 把一帧真实 MAC 头逐字节写入文本，再 `$fscanf` 读回驱动）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `FC_DI_valid` 在 index==3 拉高、index==4 就清零，而不是一直保持？

**参考答案**：下游只需在「FC 已装配完整」的那一刻采样一次 `FC_DI`（拿 Type/Subtype/ToDS/FromDS/Duration）。做成单拍脉冲既避免重复触发，又自然地把 `FC_DI` 与后续地址字段的装配节拍隔开，逻辑更简单可靠。

**练习 2**：一个 **WDS 帧**（`ToDS=FromDS=1`）的 QoS 字段出现在哪个 index？为什么和普通 QoS 数据帧不同？

**参考答案**：出现在 index 30–31。因为 WDS 帧多了 addr4（index 24–29），所以 QoS Control 顺延到 30–31；普通 QoS 数据帧没有 addr4，QoS Control 在 24–25。代码用 `FC_DI[9:8]==2'b11` 判定后选择不同分支（[phy_rx_parse.v:267-320](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/phy_rx_parse.v#L267-L320)）。

---

### 4.3 地址过滤与 RX DMA 门控：pkt_filter_ctl

#### 4.3.1 概念说明

`pkt_filter_ctl.v` 是「一帧到底要不要送进 PS」的最终裁决者。它有三个输入来源：

1. **软件策略**：`filter_cfg`（14 位，对应 mac80211 的 `FIF_*` 标志，见 [pkt_filter_ctl.v:74-86](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/pkt_filter_ctl.v#L74-L86)）、`high_priority_discard_mask`、`max_signal_len_th`、`self_mac_addr`、`self_bssid`——驱动在运行期写入，决定「我想让哪些帧进来」。
2. **解析出的字段**：`FC_type/subtype/tofrom_ds`、`addr1/addr2/addr3` 及其 `_valid`、`signal_len`、`sig_valid`——来自 4.2 的 `phy_rx_parse` 与 `openofdm_rx`。
3. **PHY 状态**：`ht_unsupport`（本实现不支持某些 HT 帧时直接拦截）。

输出只有一个核心信号 `block_rx_dma_to_ps`：=1 表示**拦截**（帧不进 DMA/DDR），=0 表示放行。它被 `rx_intf` 在 `WAIT_FILTER_FLAG` 状态消费（见 u3-l1/u3-l2），决定把帧拼进 AXI-Stream 还是复位丢弃。

#### 4.3.2 核心流程

模块由一个 6 状态有限状态机驱动，本质是**等地址逐个到位再一次性裁决**：

```
FILTER_IDLE
  -- sig_valid && !ht_unsupport && (14<=signal_len<=max_signal_len_th) -->
WAIT_FOR_ADDR1   (锁 addr1；按 ToDS/FromDS 决定 addr1 是否是 BSSID)
  -- addr1_valid；signal_len>=20 -->
WAIT_FOR_ADDR2   (锁 addr2；按 ToDS/FromDS 决定 addr2 是否是 BSSID)
  -- addr2_valid；signal_len>=26 -->
WAIT_FOR_ADDR3   (锁 addr3；IBSS 帧的 addr3 是 BSSID)
  -- addr3_valid -->
FILTER_ACTION    (按 filter_cfg 逐条匹配，写 allow_rx_dma_to_ps_reg/high_priority_discard_reg)
  --> 回 FILTER_IDLE
```

任何一步 `signal_len` 落在「异常区间」（例如 14–20 之间却没等到 addr1）就进 `ABNORMAL_STATE`，直接丢弃该帧。

`FILTER_ACTION` 里维护一个 14 位的 `allow_rx_dma_to_ps_reg`（每位对应一条「放行规则」是否命中）和一个 9 位的 `high_priority_discard_reg`（「强制丢弃」覆盖位）。最终裁决在**组合逻辑**里完成：

\[
\text{block\_tmp} = \neg\,\bigvee_{i=0}^{13} \text{allow}[i]
\]

\[
\text{block\_rx\_dma\_to\_ps} = (\neg\,\text{MONITOR\_ALL}) \,\wedge\, (\text{block\_tmp} \,\vee\, \text{high\_priority\_discard\_flag})
\]

含义：

- 若软件开了 `MONITOR_ALL`（抓包/monitor 模式），一律放行（block=0）；
- 否则：**没有任何放行规则命中**（`block_tmp=1`）→ 拦截；或**命中了强制丢弃规则**（`high_priority_discard_flag=1`，例如「组播但没开 FIF_ALLMULTI」「别的 BSS 的 Beacon」）→ 拦截；
- 只有命中至少一条放行规则且没被强制丢弃，才放行。

#### 4.3.3 源码精读

**帧类型译码**——把 Type/Subtype 组合翻译成一组布尔线：

[ip/xpu/src/pkt_filter_ctl.v:60-71](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/pkt_filter_ctl.v#L60-L71) — `is_beacon=(type==0)&&(subtype==8)`、`is_blkackreq=(type==1)&&(subtype==8)`、`is_ack=(type==1)&&(subtype==13)`、`is_rts=…subtype==11` 等，后续 `FILTER_ACTION` 直接复用。

**FIF_\* 标志常量**——与内核 `ieee80211_filter_flags` 对齐：

[ip/xpu/src/pkt_filter_ctl.v:74-86](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/pkt_filter_ctl.v#L74-L86) — `FIF_ALLMULTI`、`FIF_BCN_PRBRESP_PROMISC`、`FIF_CONTROL`、`FIF_OTHER_BSS`、`FIF_PSPOLL`、`FIF_PROBE_REQ`、`UNICAST_FOR_US`、`BROADCAST_ALL_ONE`、`BROADCAST_ALL_ZERO`、`MY_BEACON`、`MONITOR_ALL` 等。注释里也标了 `FIF_FCSFAIL`/`FIF_PLCPFAIL` 暂不支持。

**状态机定义**：

[ip/xpu/src/pkt_filter_ctl.v:88-93](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/pkt_filter_ctl.v#L88-L93) — `FILTER_IDLE / WAIT_FOR_ADDR1 / WAIT_FOR_ADDR2 / WAIT_FOR_ADDR3 / FILTER_ACTION / ABNORMAL_STATE`。

**最终裁决组合逻辑**——本模块最关键的三行：

[ip/xpu/src/pkt_filter_ctl.v:119-134](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/pkt_filter_ctl.v#L119-L134) — 先把 14 位 `allow` 或起来再取反得到 `block_rx_dma_to_ps_tmp`（无放行规则命中=1）；再与 `high_priority_discard_flag` 取或；最后用 `~MONITOR_ALL` 作总闸。这就是上一节那两个公式的直接实现。

**IDLE / 各 WAIT 状态——等地址到位并捕获 BSSID**：

[ip/xpu/src/pkt_filter_ctl.v:151-178](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/pkt_filter_ctl.v#L151-L178)（IDLE：用 `signal_len>=14` 做长度初筛，否则进 ABNORMAL）；[180-220](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/pkt_filter_ctl.v#L180-L220)（WAIT_FOR_ADDR1）；[222-262](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/pkt_filter_ctl.v#L222-L262)（WAIT_FOR_ADDR2）；[264-292](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/pkt_filter_ctl.v#L264-L292)（WAIT_FOR_ADDR3）。

这里有个精巧点：`FC_tofrom_ds` 的取值决定**哪个地址是 BSSID**，据此把 `addr1/addr2/addr3` 之一锁存进 `filter_bssid`：

- `FC_tofrom_ds==2'b10`（ToDS=1,FromDS=0，上行）→ addr1 是 BSSID（[第189-193行](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/pkt_filter_ctl.v#L189-L193)）；
- `FC_tofrom_ds==2'b01`（ToDS=0,FromDS=1，下行）→ addr2 是 BSSID（[第231-235行](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/pkt_filter_ctl.v#L231-L235)）；
- `FC_tofrom_ds==2'b00`（IBSS/管理帧）→ addr3 是 BSSID（[第274-278行](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/pkt_filter_ctl.v#L274-L278)）。

> 注意 `FC_tofrom_ds = {FC_to_ds, FC_from_ds}`（见 [xpu.v:631](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L631)），即高位是 ToDS、低位是 FromDS，与上面的判定一致。

**FILTER_ACTION——逐条规则匹配**：

[ip/xpu/src/pkt_filter_ctl.v:294-418](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/pkt_filter_ctl.v#L294-L418) — 每条规则都是「`if ((某 FIF 标志 & filter_cfg) && (帧匹配)) allow[位]<=1; else 保持`」的模板，外加一个对称的 `high_priority_discard_reg[位]`：当对应 `FIF` 标志**未开**却命中这类帧时，置强制丢弃。例如：

- 组播（`addr1[23:0]==0x5E0001` 或 `addr1[15:0]==0x3333`）：开 `FIF_ALLMULTI` 才放行（[第304-316行](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/pkt_filter_ctl.v#L304-L316)）；
- 单播给自己（`addr1==self_mac_addr`）：开 `UNICAST_FOR_US` 放行（[第388-391行](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/pkt_filter_ctl.v#L388-L391)）；
- 全 1 广播：开 `BROADCAST_ALL_ONE` 放行（[第393-396行](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/pkt_filter_ctl.v#L393-L396)）；
- 别的 BSS 的 Beacon（`filter_bssid != self_bssid`）：只有开 `FIF_BCN_PRBRESP_PROMISC` 或 `FIF_OTHER_BSS` 才放行，否则强制丢弃（[第318-330行](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/pkt_filter_ctl.v#L318-L330)、[第346-358行](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/pkt_filter_ctl.v#L346-L358)）；
- monitor 模式：开 `MONITOR_ALL` 一律放行（[第408-411行](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/pkt_filter_ctl.v#L408-L411)）。

**ABNORMAL_STATE——异常帧直接丢弃**：

[ip/xpu/src/pkt_filter_ctl.v:420-430](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/pkt_filter_ctl.v#L420-L430) — 清零 `allow`、把 `high_priority_discard_reg` 置全 1，从而 `block_rx_dma_to_ps=1`，并置 `abnormal_flag`。

**xpu 里的连线与「软件总闸」**：

[ip/xpu/src/xpu.v:612-644](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L612-L644) — 例化 `pkt_filter_ctl_i`：`filter_cfg=slv_reg27[13:0]`、`high_priority_discard_mask=slv_reg27[24:16]`、`max_signal_len_th=slv_reg5[31:16]`、`self_mac_addr=mac_addr`、`self_bssid={slv_reg29[15:0],slv_reg28}`；产物叫 `block_rx_dma_to_ps_internal`。

[ip/xpu/src/xpu.v:349](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L349) — `assign block_rx_dma_to_ps = (block_rx_dma_to_ps_internal & (~slv_reg1[2]));`。即硬件过滤结果再被 `slv_reg1[2]` **与一下**：软件把 `slv_reg1[2]` 写 1，就能**强制放行一切**（block 恒为 0），这是绕过硬件过滤的「总闸」，供抓包/调试用。

#### 4.3.4 代码实践

**实践目标**：本讲指定的实践任务——说明 `phy_rx_parse` 提取了哪些 MAC 字段，并结合 `pkt_filter_ctl` 解释 `block_rx_dma_to_ps` 如何取值。我们用两个对照场景走一遍。

**场景 A：发给本站的普通单播数据帧（应放行）**

1. 帧：From DS 的 QoS 数据帧，`addr1 == self_mac_addr`，`signal_len=100`（远大于 26）。
2. `phy_rx_parse` 按字节序给出 `FC_DI_valid`(idx3) → `addr1_valid`(idx9) → `addr2_valid`(idx15) → `addr3_valid`(idx21)。
3. `pkt_filter_ctl`：`sig_valid` 触发，长度 100∈[14, max] → WAIT_FOR_ADDR1 → （`FC_tofrom_ds==2'b01`）→ WAIT_FOR_ADDR2 锁 BSSID=addr2 → WAIT_FOR_ADDR3 → FILTER_ACTION。
4. 在 [FILTER_ACTION 第388-391行](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/pkt_filter_ctl.v#L388-L391)：若 `filter_cfg` 含 `UNICAST_FOR_US` 且 `addr1==self_mac_addr` → `allow[9]<=1`。
5. 裁决：`block_tmp = ~(…|allow[9]|…) = 0`；该帧不触发任何 `high_priority_discard` → `block_rx_dma_to_ps = (~MONITOR_ALL) & (0|0) = 0`。**放行**。✅

**场景 B：组播帧但软件没开 FIF_ALLMULTI（应拦截）**

1. 帧：组播（`addr1[23:0]==0x5E0001`），其余同上。
2. 解析路径相同，进入 FILTER_ACTION。
3. [第304-316行](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/pkt_filter_ctl.v#L304-L316)：`(FIF_ALLMULTI & filter_cfg)==0`，于是 `allow[1]` 不置位，反而 `high_priority_discard_reg[1]<=1`。
4. 裁决：`high_priority_discard_flag=1` → `block_rx_dma_to_ps = (~MONITOR_ALL) & (block_tmp | 1) = 1`。**拦截**。🚫（除非软件另开 `MONITOR_ALL` 或 `slv_reg1[2]` 总闸）。

**需要观察的现象 / 预期结果**：场景 A 的 `block_rx_dma_to_ps` 在帧头解析完成后应保持 0，`rx_intf` 把该帧拼进 AXI-Stream 经 DMA 写入 DDR；场景 B 应为 1，`rx_intf` 在 `WAIT_FILTER_FLAG` 复位丢弃该帧（不产生 `rx_pkt_intr`）。

> 实际寄存器取值与波形为「待本地验证」（仓库无 pkt_filter_ctl 的 testbench）。可在驱动侧用 `/sys/kernel/debug/ieee80211/...` 观察收包计数来间接验证：开启/关闭 `FIF_ALLMULTI` 后，组播流量是否进入内核应与上述判定一致。

#### 4.3.5 小练习与答案

**练习 1**：`allow_rx_dma_to_ps_reg` 与 `high_priority_discard_reg` 为什么是**两套**寄存器而不是一套？

**参考答案**：它们对应两种不同语义。`allow` 是「正向放行列表」：命中任意一条且 `filter_cfg` 允许 → 倾向放行。`high_priority_discard` 是「负向强制丢弃」：当某类帧（组播、别的 BSS、控制帧等）的对应 `FIF` 标志**没开**时，即便它恰好匹配了某条放行规则（如广播全 1 也可能被 `BROADCAST_ALL_ONE` 放行），也要用强制丢弃覆盖。最终 `block = block_tmp | discard_flag`，丢弃优先级更高，避免误把噪声/邻区流量送进 PS。

**练习 2**：软件把 `slv_reg1[2]` 写成 1 会发生什么？为什么需要这个开关？

**参考答案**：[xpu.v:349](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L349) 里 `block = internal & (~slv_reg1[2])`，`slv_reg1[2]=1` 使 `block` 恒为 0，等于**关闭硬件过滤、所有帧都进 PS**。它用于 monitor/抓包模式或调试——当上层想看空口上的全部帧时，不必逐个配置 `FIF_*`，直接拉这个总闸即可。

**练习 3**：`WAIT_FOR_ADDR1` 里为什么要用 `signal_len>=20` 作为去往 `WAIT_FOR_ADDR2` 的门槛？

**参考答案**：MAC 头前 20 字节正好是 FC(2)+Duration(2)+addr1(6)+addr2 的前 4 字节……更实际地看：addr2 在 index 10–15 装配完成（第 16 字节 = index 15）。`signal_len` 是 PHY 报告的帧长，用它做长度门槛是在**用帧长交叉校验地址字段是否来得及到达**：若帧长连 20 字节都不到却声称是数据帧，说明帧异常或截断，与其等 `addr1_valid` 超时，不如直接判 ABNORMAL 丢弃，节省状态机时间。

---

## 5. 综合实践

**任务**：把本讲三个最小模块串起来，跟踪「一帧 From-DS 单播数据帧从空口到 PS 的完整判定链路」，并指出 TSF 在其中的作用。

**步骤**：

1. **时间基准就位**：[tsf_timer.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tsf_timer.v) 持续输出 `tsf_pulse_1M`（每 µs 一拍）和递增的 `tsf_runtime_val`。`rx_intf_pl_to_m_axis` 会用 `tsf_runtime_val` 给每帧打时间戳（见 u3-l2）——本讲确认这个时间戳来自这里。
2. **字节流到达**：`openofdm_rx` 解出字节流（`byte_in/byte_in_strobe/byte_count`）→ [phy_rx_parse.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/phy_rx_parse.v) 按 index 逐字段装配，在 index 3/9/15/21/23 给出 `FC_DI_valid/addr1_valid/addr2_valid/addr3_valid/SC_valid`。
3. **xpu 拆位**：[xpu.v:355-365](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L355-L365) 把 `FC_DI` 拆成 `FC_type/subtype/to_ds/from_ds/duration`，连同地址一起喂给过滤模块。
4. **过滤裁决**：[pkt_filter_ctl.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/pkt_filter_ctl.v) 走 IDLE→ADDR1→ADDR2→ADDR3→ACTION，命中 `UNICAST_FOR_US`（addr1 是自己）→ `allow[9]=1` → 组合逻辑给出 `block_rx_dma_to_ps_internal=0`。
5. **总闸**：[xpu.v:349](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L349) 再与 `~slv_reg1[2]`，若软件未拉总闸，最终 `block_rx_dma_to_ps=0`。
6. **下游消费**：`rx_intf` 据 `block_rx_dma_to_ps=0` 把帧拼进 AXI-Stream → AXI DMA 写 DDR → 拉收包中断 `rx_pkt_intr`（见 u3-l1）；同时把第 1 步锁存的 TSF 时间戳插进 DMA 头部第一个字。

**交付物**：

- 一张时序图：横轴是 `ofdm_byte_index`（0→23），纵轴标出每个 `_valid` 脉冲、过滤状态机跳转、`block_rx_dma_to_ps` 的取值变化。
- 一段说明：如果此帧的 addr1 不是本站、且 `filter_cfg` 不含 `MONITOR_ALL`/`FIF_OTHER_BSS`，`block_rx_dma_to_ps` 会变成什么、`rx_intf` 会如何处置。

> 预期：addr1 不是本站的单播帧，`allow` 全 0 → `block_tmp=1` → `block_rx_dma_to_ps=1`，`rx_intf` 复位丢弃，**不**触发 `rx_pkt_intr`，DDR 里不会出现该帧。结论可在真实板卡上用抓包+驱动收包计数对比来验证（「待本地验证」）。

---

## 6. 本讲小结

- `tsf_timer.v` 用一个小计数器数满 `COUNT_TOP_1M = NUM_CLK_PER_US-1` 个时钟，生成每 1µs 一拍的 `tsf_pulse_1M`，并维护 64 位 `tsf_runtime_val`；软件通过 `slv_reg2/slv_reg3`（`slv_reg3[31]` 下降沿触发）装载 AP 信标时间戳实现同步，通过 `slv_reg58/59` 回读当前 TSF。
- `phy_rx_parse.v` 是一个按 `ofdm_byte_index` 逐字节装配的解析器，输出 FC+Duration、addr1/addr2/addr3/addr4、SC、Block Ack 字段、QoS 字段，每个字段配单拍 `_valid` 脉冲；每个新帧由 `pkt_header_valid_strobe` 复位一次。
- `pkt_filter_ctl.v` 用 6 状态机等地址到位，依 `FC_tofrom_ds` 锁存 BSSID，在 `FILTER_ACTION` 按 14 条 `FIF_*` 规则写 `allow`、按 9 条写 `high_priority_discard`，最终用组合逻辑算出 `block_rx_dma_to_ps`（`MONITOR_ALL` 总放行；否则无放行规则或命中强制丢弃即拦截）。
- xpu 把三者连成一体：`filter_cfg/self_mac_addr/self_bssid` 等来自 `slv_reg27/30/31/28/29`，过滤结果再被 `slv_reg1[2]` 这个「软件总闸」与一下，最终 `block_rx_dma_to_ps` 送往 `rx_intf` 决定帧是否进 DMA/DDR。
- 这条链路体现了 openwifi 把 mac80211 的 `FIF_*` 软件过滤**下沉到硬件**的设计：用 PL 的组合/状态机逻辑在 DMA 之前就把无关帧丢掉，节省带宽与 CPU。

---

## 7. 下一步学习建议

- 若想看 `tsf_pulse_1M` 如何驱动 CSMA/CA 的 DIFS/SIFS/退避计时，继续读 **u5-l2（CSMA/CA 信道接入）** 与 `csma_ca.v`、`cw_exp.v`。
- 若想看解析出的 `addr2/SC/Block Ack 字段` 如何被用于回送 ACK 与重传决策，继续读 **u5-l3（TX 控制、重传与 ACK）** 与 `tx_control.v`。
- 若想看 `block_rx_dma_to_ps` 下游如何消费、以及 TSF 时间戳如何插进 DMA 头部，回顾 **u3-l1/u3-l2（rx_intf）** 与 `rx_intf_pl_to_m_axis.v`。
- 若想动手给本讲的三个模块写仿真，参考 **u7-l3（IP 仿真与 testbench 实践）**，仿照 `ip/xpu/unit_test/mv_avg/mv_avg_tb.v` 用文件 IO 喂字节流激励。
- 想从软件侧对照寄存器含义，可读 **u7-l1（AXI 寄存器映射与软件交互）**，把本讲反复出现的 `slv_reg1/2/3/5/27/28/29/30/31/58/59` 与驱动代码一一对应。
