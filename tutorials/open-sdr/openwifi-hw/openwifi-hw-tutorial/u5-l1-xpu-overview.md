# xpu 控制核心总览

## 1. 本讲目标

本讲进入 openwifi 六大自研 IP 中的「大脑」——**xpu**（eXternal Processing Unit）。`xpu` 是整个 WiFi 低层 MAC（Low-MAC）与控制中枢，是收发链路的总调度。

读完本讲，你应该能够：

1. 说清 `xpu` 在整套 openwifi 硬件中扮演的角色——它为什么叫「控制中枢」。
2. 打开 `xpu.v`，按「到 rx_intf / 到 openofdm_rx / 到 tx_intf / 到 side_ch / 到 PS」五个方向，把它的对外端口分组归类。
3. 看懂 `xpu` 内部例化了哪些子模块、它们各自承担什么职责，以及 `xpu` 自身「不做算法、只做装配与连线」的特点。
4. 理解 `retrans_in_progress`、`backoff_done`、`slice_en` 等关键控制信号的产生逻辑与含义。
5. 认识 `xpu` 的 AXI4-Lite 寄存器组（`slv_reg0`~`slv_reg63`）如何成为软件驱动控制 FPGA 行为的「旋钮面板」。

## 2. 前置知识

在开始前，请确认你已了解以下概念（前几讲已建立）：

- **PS / PL**：Xilinx Zynq 的「处理系统（ARM）」与「可编程逻辑（FPGA）」。本仓库是纯 PL 侧设计，`xpu` 运行在 PL 上。
- **openwifi_ip 层级**：`xpu` 与 `tx_intf`、`rx_intf`、`openofdm_tx`、`openofdm_rx`、`side_ch` 一起被打包进 `openwifi_ip` 这个 block design 层级单元（见 u2-l2）。
- **AXI4-Lite 寄存器**：PS 通过 AXI4-Lite 读写 PL 上的 `slv_reg` 来配置参数、读取状态（见 u2-l3）。
- **PHY / MAC**：物理层（OFDM 调制解调）与媒体接入控制层。`openofdm_tx/rx` 负责 PHY，`xpu` 负责 MAC 的低层部分。
- **CSMA/CA、ACK、重传、TSF**：802.11 的信道接入、应答、重传与定时同步机制。这些是 `xpu` 要在硬件里实现的核心协议行为，本讲只做总览，细节留给 u5-l2 ~ u5-l5。

一句话定位：如果把 `openofdm_tx/rx` 看成「负责把比特变成波形、把波形变成比特」的肌肉，那么 `xpu` 就是「决定什么时候发、发哪一帧、要不要等 ACK、要不要重传、收到的帧要不要上报」的神经系统。

## 3. 本讲源码地图

本讲只围绕一个文件展开，它是本讲唯一的「主角」：

| 文件 | 作用 |
| --- | --- |
| [ip/xpu/src/xpu.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v) | `xpu` IP 的顶层模块。声明全部对外端口、定义 AXI 寄存器 wire、例化 13 类子模块并把它们连线。自身不写时序算法，是「装配 + 连线 + 一层组合逻辑」。 |

为了看懂 `xpu.v` 里例化的子模块名，下表列出 `xpu/src/` 目录下的同族源码（本讲只点名、不逐行展开，细节在后续讲义）：

| 子模块文件 | 职责（一句话） | 详解讲义 |
| --- | --- | --- |
| `tx_on_detection.v` | 由 `phy_tx_start/started/done` 推导 `tx_bb_is_ongoing` / `tx_rf_is_ongoing` 等发射进行中标志 | u5-l3 |
| `cca.v` | 信道空闲评估（Clear Channel Assessment） | u5-l5 |
| `csma_ca.v` | CSMA/CA 退避、DIFS/EIFS/SIFS 计时、NAV | u5-l2 |
| `cw_exp.v` | 竞争窗口指数（动态 CW） | u5-l2 |
| `tx_control.v` | TX 状态机、ACK 等待、重传控制 | u5-l3 |
| `pkt_filter_ctl.v` | 接收帧地址过滤、决定是否上报 PS | u5-l4 |
| `phy_rx_parse.v` | 解析接收帧 MAC 头（FC/地址/SC/QoS） | u5-l4 |
| `rssi.v` | RSSI 能量计算与平滑 | u5-l5 |
| `time_slice_gen.v` | 时分切片（`slice_en` 队列门控） | 本讲简介 |
| `tsf_timer.v` | 64 位 TSF 定时器、产生 1µs 脉冲 | u5-l4 |
| `spi_module.v`（对应 `spi.v`） | 经 SPI 控制 AD9361 射频 | u5-l5 |
| `xpu_s_axi.v` | AXI4-Lite 从设备，寄存器读写 | u7-l1 |
| `edge_to_flip.v` | 把事件信号转成 LED「翻转」指示 | u7-l6 |

## 4. 核心概念与源码讲解

本讲把「xpu 顶层」这个最小模块拆成四个学习块来读：先看角色与架构（4.1），再看对外端口分组（4.2），再看内部装配（4.3），最后读关键控制信号（4.4）。

### 4.1 xpu 是什么：低层 MAC 控制中枢

#### 4.1.1 概念说明

`xpu` 名字可读作 **eXternal Processing Unit**，但理解它的关键不在名字，而在职责。它把 802.11 协议里那些「对时间极敏感、必须用硬件实现」的低层 MAC 行为，全部集中到一个 IP 里：

- **什么时候能发**：CSMA/CA 信道接入、随机退避（`csma_ca`）。
- **发出去之后怎么办**：等 ACK、ACK 超时、决定是否重传（`tx_control`）。
- **全网时间怎么对齐**：64 位 TSF 定时器、1µs 脉冲（`tsf_timer`）。
- **收到的帧怎么处理**：解析 MAC 头（`phy_rx_parse`）、按地址过滤决定上报还是丢弃（`pkt_filter_ctl`）。
- **信道到底忙不忙**：RSSI 能量计算（`rssi`）、CCA（`cca`）。
- **怎么控制射频前端**：经 SPI 配置 AD9361（`spi_module`）。

为什么要把这些放进硬件而不是软件？因为这些动作的精度要求在**微秒甚至亚微秒级**（例如 SIFS 只有 16µs，退避以 slot=9µs 为单位计数），靠 ARM 上跑 Linux 中断根本来不及。`xpu` 用基带时钟（`s00_axi_aclk`）逐拍处理，把这些实时性需求在 PL 内消化掉，只把「慢决策」和「统计」留给 PS 的驱动软件。

#### 4.1.2 核心流程

`xpu` 顶层本身没有大段 always 时序逻辑，它的「流程」体现在**信号在子模块之间的流动**。一个简化的全局图：

```
                    ┌──────────────── tsf_timer ────────────────┐
                    │            tsf_pulse_1M (1µs)              │
                    ▼                                           │
  ADC I/Q ──► rssi ──► cca ──► csma_ca ──┐   backoff_done        │
  (来自rx_intf)              (退避/计时) │     │                 │
                                        ▼     │                 │
   openofdm_rx 字节流 ──► phy_rx_parse ──► pkt_filter_ctl        │
   (FC/地址/SC)           (MAC头解析)      (block_rx_dma_to_ps)  │
                                        │                       │
   phy_tx_start/started/done ──► tx_on_detection ──► tx_control ◄─┘
   (来自tx_intf/openofdm_tx)      (TX进行中标志)    (TX状态机/重传)
                                        │
                                        ▼
                              slice_en / tx_status / ack_tx_flag ...
                                        │
                                        ▼
                                    tx_intf (发射)
```

关键节奏由 `tsf_pulse_1M`（每微秒一个脉冲）统一驱动：`csma_ca` 用它数 DIFS/SIFS/slot，`time_slice_gen` 用它做时分切片，`tsf_timer` 本身就是它的产生者。可以说 **TSF 是 `xpu` 内部的「心跳」**。

#### 4.1.3 源码精读

`xpu.v` 的开头是文件包含与调试宏定义，然后是模块声明与参数：

[xpu.v:1-11](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L1-L11) — 引入 git 版本号、`xpu_pre_def.v` 条件编译文件，并定义 `DEBUG_PREFIX` 宏。当编译时定义了 `XPU_ENABLE_DBG` 宏，`DEBUG_PREFIX` 会展开成 `(*mark_debug="true",DONT_TOUCH="TRUE"*)`，把对应信号标记给 ILA 抓波形；否则为空。这就是 u7-l6 要讲的「ENABLE_DBG + ILA」调试机制在本文件里的落点。

[xpu.v:13-26](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L13-L26) — 模块参数列表。注意几个关键宽度：`RSSI_HALF_DB_WIDTH=11`（RSSI 以 0.5dB 为单位、11 位）、`TSF_TIMER_WIDTH=64`（按 802.11 标准的 64 位 TSF）、`C_S00_AXI_ADDR_WIDTH=8`（AXI 地址 8 位 = 256 字节空间，对应 `slv_reg0`~`slv_reg63` 共 64 个 32 位寄存器）。

一个非常关键、贯穿全文件的「软复位」约定：`xpu` 给每个子模块的 `rstn` 不是同一个信号，而是 `s00_axi_aresetn & (~slv_reg0[k])`，即**软件写 `slv_reg0` 的不同位可以单独复位某个子模块**。例如 `slv_reg0[0]` 复位 `tx_on_detection`、`slv_reg0[6]` 同时复位 `cca` 与 `csma_ca`。我们在 4.3 节会看到具体位分配。

#### 4.1.4 代码实践

**实践目标**：建立「xpu = 装配 + 连线 + 寄存器」的整体印象。

**操作步骤**：

1. 打开 [ip/xpu/src/xpu.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v)。
2. 用编辑器的折叠/搜索功能，数一下文件里 `always @` 时序逻辑块的数量。

**需要观察的现象**：你会发现 `xpu.v` 里**几乎没有** `always @(posedge clk)` 的状态机（顶层只有 `assign` 组合逻辑和子模块例化）。这正是「顶层不做算法」的直接证据。

**预期结果**：算法逻辑都被下沉到子模块（`tx_control`、`csma_ca` 等），顶层只负责把它们用 wire 连起来，并用 `assign` 做一层简单的位拼接与选择（如 `mute_adc_out_to_bb`、`slice_en`、`mac_addr`）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `xpu` 要把 CSMA/CA 放进硬件，而不是让 ARM 上的 Linux 驱动来做？

> **参考答案**：CSMA/CA 的 DIFS/SIFS/slot/退避计时精度在微秒级（如 SIFS≈16µs、slot=9µs），Linux 在 ARM 上靠中断响应做不到这么准、这么抖动小。把计时与退避放在 PL 的基带时钟域逐拍处理，才能满足 802.11 协议的时间约束。

**练习 2**：`xpu.v` 顶层自身几乎不写状态机，那它存在的价值是什么？

> **参考答案**：它是「装配与连线层」——把十几个低层 MAC 子模块按正确的信号关系连成一个完整 IP，统一对外暴露端口与 AXI 寄存器，并做必要的组合逻辑（位选择、多路选择、信号汇总）。这种分层让每个子模块可独立仿真、可单独软复位。

---

### 4.2 对外端口：五大方向的信号分组

#### 4.2.1 概念说明

`xpu` 是个「星形枢纽」：它同时与 `rx_intf`、`openofdm_rx`、`phy_tx`（即 `openofdm_tx`）、`tx_intf`、`side_ch`、AD9361（SPI/GPIO）、PS（AXI）打交道。读懂 `xpu` 的第一步，就是把它的端口按**对话对象**分组。这也是本讲代码实践任务的核心。

#### 4.2.2 核心流程

`xpu.v` 的端口区（L27-156）本身就是按对话对象分块注释的，作者已经用 `// Ports to ...` 注释把分组写好了。我们可以直接沿用，归纳为五组：

1. **到 rx_intf / 来自射频**：ADC 下变频后的 I/Q（`ddc_i/ddc_q`），以及回送给 rx_intf 的控制（`mute_adc_out_to_bb` 静音自收、`block_rx_dma_to_ps` 过滤上报）。
2. **到 openofdm_rx**：把 RSSI 送过去（`rssi_half_db`），并接收它吐出的字节流与帧头信息（`byte_in`、`pkt_rate`、`pkt_len`、`fcs_ok` 等）。
3. **到 tx_intf / phy_tx**：发出发射控制（`backoff_done`、`slice_en`、`tx_status`、`wea/addra/dina` 写 BRAM），接收发射反馈（`phy_tx_start/started/done`、`tx_pkt_need_ack`）。
4. **到 side_ch**：把解析出的 MAC 头与状态（`FC_DI`、`addr1/2/3`、`pkt_for_me`、`ch_idle_final`）送侧信道做观测。
5. **到 PS（AXI 与 GPIO/SPI）**：AXI4-Lite 寄存器接口（`s00_axi_*`）、SPI 控制 AD9361（`spi_*`）、AD9361 状态（`gpio_status`）。

#### 4.2.3 源码精读

**① 到 rx_intf 的方向**：

[xpu.v:31-41](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L31-L41) — `ddc_i/ddc_q/ddc_iq_valid` 是从 `rx_intf` 送来的数字下变频 I/Q 样点（`xpu` 拿去做 RSSI）；`mute_adc_out_to_bb` 是「发射时把自收静音」的控制（详见 u3-l1）；`block_rx_dma_to_ps` 控制接收帧是否上报 PS（硬件地址过滤的出口）。

这两个控制信号在文件后半段的组合逻辑里被赋值：

[xpu.v:348-349](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L348-L349) — `mute_adc_out_to_bb` 支持「软件强制（`slv_reg1[0]`/`slv_reg1[31]`）」和「自动（发射进行中或 CTS/ACK 进行中）」两种模式；`block_rx_dma_to_ps` 由过滤子模块的内部结果与软件覆盖位（`~slv_reg1[2]`）共同决定。

**② 到 openofdm_rx 的方向**：

[xpu.v:43-66](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L43-L66) — `rssi_half_db` 是 `xpu` 算出来送给接收机的；其余大多是 `openofdm_rx` → `xpu` 方向：`demod_is_ongoing`（正在解调）、`pkt_header_valid`（帧头有效）、`pkt_rate/pkt_len`（速率与长度）、`byte_in/byte_in_strobe`（解出的字节流）、`fcs_in_strobe/fcs_ok`（FCS 校验结果）。注意 `byte_in` 这条字节流被「一拖二」：既送 `xpu` 做解析，也被 `rx_intf` 拼包（见 u3-l3）。

**③ 到 tx_intf / phy_tx 的方向**：

[xpu.v:74-111](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L74-L111) — 这一组信号最多。`phy_tx_start/started/done` 是与 `openofdm_tx` 的发射握手（input，来自发射链）；而 `tx_status[79:0]`、`backoff_done`、`slice_en[3:0]`、`ack_tx_flag`、`wea/addra/dina`（写 tx BRAM，用于把某帧标记为「首帧/重传帧」）则是 `xpu` → `tx_intf` 方向。`band[3:0]` 与 `channel[15:0]` 决定工作频段与信道。

**④ 到 side_ch 的方向**：

[xpu.v:113-123](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L113-L123) — `FC_DI`（Frame Control）、三个地址、`pkt_for_me`、`ch_idle_final` 都送给 `side_ch` 做研究与调试观测（详见 u6-l1）。`pkt_for_me` 的定义见下文组合逻辑。

**⑤ 到 PS（AXI/SPI/GPIO）的方向**：

[xpu.v:125-155](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L125-L155) — `spi_*` 是经 `xpu` 转发去控制 AD9361 的 SPI（受 `spi_module` 子模块管理）；最后一大段 `s00_axi_*` 是标准的 AXI4-Lite 从设备接口，PS 经它读写 `slv_reg`。

此外有几条对地址/状态做汇总的组合逻辑，集中体现了「顶层只连线 + 简单选择」：

[xpu.v:351-353](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L351-L353) — `slice_en` 由 `time_slice_gen` 的 4 个单比特拼接成 4 位总线；`mac_addr` 由两个寄存器拼成本机 MAC 地址。

[xpu.v:378](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L378) — `pkt_for_me = (addr1==mac_addr)`，即「目的地址是不是我」，这一位直接送给 `side_ch` 观测，也参与过滤与 ACK 决策。

#### 4.2.4 代码实践

**实践目标**：亲手完成「五方向端口分组」整理，建立 `xpu` 信号全景图。

**操作步骤**：

1. 打开 [xpu.v 端口区 L27-156](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L27-L156)。
2. 建一张五行表格，行分别是 `rx_intf`、`openofdm_rx`、`tx_intf/phy_tx`、`side_ch`、`PS/AD9361`。
3. 把每个端口填进对应行，并标注方向（`xpu→对端` 还是 `对端→xpu`）。
4. 对照作者写好的 `// Ports to ...` 注释自检。

**需要观察的现象**：与 `openofdm_rx` 之间大量是 input（接收它的输出）；与 `tx_intf` 之间 output 偏多（`xpu` 在指挥发射）。

**预期结果**：你会得到一张清晰的「`xpu` 对话关系图」，这是后续阅读 `tx_control`、`csma_ca` 等子模块时的索引。

#### 4.2.5 小练习与答案

**练习 1**：`block_rx_dma_to_ps` 是 output，它的值最终由哪个子模块产生？

> **参考答案**：由 `pkt_filter_ctl`（地址过滤）产生内部信号 `block_rx_dma_to_ps_internal`，再在顶层与软件覆盖位组合：`block_rx_dma_to_ps = block_rx_dma_to_ps_internal & (~slv_reg1[2])`（见 [xpu.v:349](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L349)）。软件写 `slv_reg1[2]=1` 可强制放行所有帧（关闭过滤）。

**练习 2**：`pkt_for_me` 是怎么算出来的？为什么它既要送 `side_ch`、又参与内部决策？

> **参考答案**：`pkt_for_me = (addr1==mac_addr)`，即接收帧的目的地址等于本机 MAC。送 `side_ch` 是为了观测「有多少帧是给我的」；参与内部决策是因为只有 `pkt_for_me` 的单播帧才需要回 ACK，过滤逻辑也依赖它。

---

### 4.3 内部子模块装配图

#### 4.3.1 概念说明

`xpu.v` 的主体（L394-865）是 13 类子模块的例化。理解 `xpu` 内部的关键是：**每个子模块各自独立工作，靠 wire 互联**；顶层就像一块「面包板」，把这些芯片插上去、用导线连好。其中还有一套统一的「软复位」机制，让软件能逐个复位子模块。

#### 4.3.2 核心流程

`xpu` 的复位体系可以用一张表概括（位 = 1 表示复位该子模块）：

| `slv_reg0` 位 | 复位的子模块 | 含义 |
| --- | --- | --- |
| `[0]` | `tx_on_detection` | TX 进行中检测 |
| `[2]` | `pkt_filter_ctl` | 接收过滤 |
| `[3]` | `phy_rx_parse` | MAC 头解析（与帧头选通一起复位） |
| `[4]` | `rssi` | RSSI 计算 |
| `[5]` | `cw_exp` + `tx_control` | 竞争窗口 + TX 状态机 |
| `[6]` | `cca` + `csma_ca` | CCA + CSMA/CA |
| `[7]` | `time_slice_gen` | 时分切片 |

子模块之间的数据流（精简版）：

```
tsf_timer ──tsf_pulse_1M──► csma_ca ──backoff_done──► tx_control
                              ▲                          │
              ch_idle          │                          │ start_retrans/tx_try_complete
        cca ◄──rssi◄── ddc_i/q │                          ▼
                              │                       (回 tx_intf/openofdm_tx)
   openofdm_rx 字节 ──► phy_rx_parse ──FC/addr/SC──► pkt_filter_ctl ──block_rx_dma_to_ps
                                                          │
                                          (FC/addr 也送 side_ch 与 tx_control 做 ACK 判定)
```

#### 4.3.3 源码精读

**① 软复位机制的统一写法**——以 `tx_control` 为例：

[xpu.v:537-543](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L537-L543) — `.rstn(s00_axi_aresetn&(~slv_reg0[5]))`。意思是：硬件总复位 `s00_axi_aresetn` 拉低时整体复位；此外软件把 `slv_reg0[5]` 写成 1，也能单独把 `cw_exp` 和 `tx_control` 复位。这个模式在 `cca`（`slv_reg0[6]`）、`rssi`（`slv_reg0[4]`）、`phy_rx_parse`（`slv_reg0[3]`）等处重复出现。

**② TSF 心跳的产生与分发**：

[xpu.v:745-754](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L745-L754) — `tsf_timer` 例化，输出 `tsf_runtime_val`（64 位运行时值，软件可经 `slv_reg58/59` 读回）和 `tsf_pulse_1M`（每微秒一脉冲）。软件写 `slv_reg2/slv_reg3` 可加载 TSF 初值（`tsf_load_control` 由 `slv_reg3` 最高位触发）。

这个 `tsf_pulse_1M` 随后被 `csma_ca`（[L472](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L472)）和 `time_slice_gen`（[L727](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L727)）消费，是整个 MAC 计时的基准。

**③ CSMA/CA 与 CCA 的协作**：

[xpu.v:445-464](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L445-L464) — `cca` 把 `rssi_half_db` 与阈值 `rssi_half_db_th` 比较，结合「是否在收/发」得出 `ch_idle`（信道空闲）。

[xpu.v:466-473](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L466-L473) — `csma_ca` 拿到 `ch_idle`、TSF 脉冲、帧头信息，跑 DIFS/退避状态机，最终产出 `backoff_done`。注意 `random_seed={ddc_q[2],ddc_i[0]}`（[L506](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L506)）——用 ADC 噪声样的最低几位当随机数种子，是个很实用的「物理噪声源」技巧。

**④ TX 状态机核心**：

[xpu.v:537-610](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L537-L610) — `tx_control` 例化。它的输入横跨发射反馈（`phy_tx_done`、`sig_valid`、`fcs_valid`）、解析结果（`FC_type`、`addr1/addr2`）、定时（`send_ack_wait_top`、`recv_ack_timeout_top_adj`），输出 `retrans_in_progress`、`tx_control_state`、`tx_status`、`wea/addra/dina`（改写 tx BRAM 标记重传）等。它和 `csma_ca` 通过 `backoff_done` ↔ `retrans_trigger`/`quit_retrans` 形成闭环。

**⑤ AXI 寄存器从设备**：

[xpu.v:772-773](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L772-L773) — `xpu_s_axi` 例化，把 AXI4-Lite 接口转成对 `slv_reg0`~`slv_reg63` 的读写（细节留 u7-l1）。`xpu.v` 顶部 L159-223 的 `wire slv_regN` 声明，每个都带注释说明其含义，是「软件 ↔ 硬件契约文档」。

#### 4.3.4 代码实践

**实践目标**：用一张表把「软复位位 → 子模块」对应关系梳理清楚。

**操作步骤**：

1. 在 [xpu.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v) 中搜索 `~slv_reg0[`，统计每一处出现的位编号与它所在子模块例化名。
2. 把结果填进 4.3.2 的表格。
3. 注意哪些子模块共享同一个复位位（如 `[5]` 同时管 `cw_exp` 和 `tx_control`，`[6]` 同时管 `cca` 和 `csma_ca`）。

**需要观察的现象**：`phy_rx_parse` 的复位条件最特殊——`.rstn(s00_axi_aresetn&(~slv_reg0[3])&(~pkt_header_valid_strobe))`，除了软复位位，还会在每个帧头选通时复位一次。

**预期结果**：得到一份「软件如何逐块复位 `xpu` 内部」的速查表，调试时可直接用驱动写对应寄存器位来复位某一功能块，而不影响其他模块。

**注**：本实践为源码阅读型，无需运行综合。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `cca` 和 `csma_ca` 共用同一个软复位位 `slv_reg0[6]`？

> **参考答案**：二者是信道接入的一对紧密协作模块——`cca` 判空闲、`csma_ca` 据此做退避。让它们同时复位可以保证信道接入逻辑从一个一致、干净的初始态启动，避免二者状态错配。

**练习 2**：`tsf_pulse_1M` 在 `xpu` 内部被哪些子模块使用？

> **参考答案**：主要被 `csma_ca`（用于 DIFS/SIFS/slot/退避的微秒级计时）和 `time_slice_gen`（用于时分切片计数）使用；它本身由 `tsf_timer` 产生。

---

### 4.4 关键控制信号解读：retrans_in_progress、backoff_done、slice_en

#### 4.4.1 概念说明

本讲学习目标里点名要理解的几个信号，是 `xpu` 对外（尤其对 `tx_intf`）最关键的「指挥棒」：

- **`backoff_done`**：退避完成，表示「我已赢得信道、可以发了」。
- **`retrans_in_progress`**：重传进行中，表示「刚发出去的帧还在等 ACK / 还在重传循环里」。
- **`slice_en[3:0]`**：时分切片使能，4 位对应 4 个队列/时机的发送窗口门控。

这三个信号共同回答了 802.11 发射的三个问题：**什么时候能发**（`backoff_done`）、**这帧是否还在等回应**（`retrans_in_progress`）、**当前轮到哪个队列发**（`slice_en`）。

#### 4.4.2 核心流程

三者时序上是一条链：

```
有帧要发 → csma_ca 跑完 DIFS+退避 → backoff_done=1
        → tx_control 启动发射 → phy_tx_start/started/done
        → 若需 ACK：retrans_in_progress=1，进入 RECV_ACK_WAIT 状态
            → 收到 ACK：retrans_in_progress=0，tx_try_complete=1（成功）
            → ACK 超时/quit_retrans：计数+1，再次退避重传，或放弃
```

`slice_en` 则独立地在后台按 TSF 节拍轮转，决定 4 个队列（对应 WMM 的 4 个 AC）各自在哪个时间切片里被允许触发发送，是公平性/优先级的硬件闸门。

#### 4.4.3 源码精读

**① `backoff_done` 的产生**：

[xpu.v:522](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L522) — `csma_ca` 的输出 `backoff_done` 连到顶层端口。

[csma_ca.v:147](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/csma_ca.v#L147) — `assign backoff_done = (backoff_state==BACKOFF_WAIT_FOR_OWN);`。这是一个纯组合逻辑：当退避状态机进入 `BACKOFF_WAIT_FOR_OWN`（已数完随机退避槽数、轮到自己）时拉高。换言之，`backoff_done=1` ≈ **退避已结束、赢得信道、可立即发射**。

**② `retrans_in_progress` 的产生与清除**：

[xpu.v:598](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L598) — `tx_control` 的输出 `retrans_in_progress` 连到顶层端口（注意它是 `DEBUG_PREFIX` 输出，只有在 `XPU_ENABLE_DBG` 下才带 `mark_debug`，但端口始终存在）。

[tx_control.v:364](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L364) — 当一帧**需要 ACK** 的包发射完成（`phy_tx_done`）时，`retrans_in_progress<=1`，状态进入 `RECV_ACK_WAIT_TX_BB_DONE`，开始等 ACK。

[tx_control.v:376-385](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L376-L385) — 若收到 `quit_retrans`（放弃重传），则 `retrans_in_progress<=0`、`tx_try_complete<=1` 结束本次发送尝试。

[tx_control.v:386-399](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L386-L399) — 若 `backoff_done` 且仍在重传循环且尚未开始本次重传（`retrans_started==0`），则发 `start_retrans` 触发再次发送。

所以 `retrans_in_progress` 的语义是：**「当前有一帧正处于「等 ACK / 重传」的循环中」**——它从「需要 ACK 的帧发完」置位，到「收到 ACK / 放弃 / 超时」清零。`tx_intf` 和上层可以据此知道硬件还在忙这帧。

**③ `slice_en` 的产生**：

[xpu.v:723-743](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L723-L743) — `time_slice_gen` 例化。它消费 `tsf_pulse_1M` 与三组寄存器（`slv_reg20/21/22` 分别配置某个切片索引的 count_total / count_start / count_end），输出 `slice_en0~3`。

[xpu.v:351](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L351) — `assign slice_en = {slice_en3, slice_en2, slice_en1, slice_en0};` 拼成 4 位送给 `tx_intf`。这 4 位是 4 个发送队列的「时间窗口使能」：只有对应位为 1 时，该队列才被允许在这个 TSF 切片内发起发送，是实现发送调度公平性的硬件闸门。

#### 4.4.4 代码实践

**实践目标**：追踪 `backoff_done` 与 `retrans_in_progress` 在子模块里的真实定义，确认本节给出的语义。

**操作步骤**：

1. 打开 [csma_ca.v:147](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/csma_ca.v#L147)，确认 `backoff_done` 是组合逻辑、由状态机当前态决定。
2. 打开 [tx_control.v:355-399](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L355-L399)，找到 `retrans_in_progress` 的所有置位（`<=1`）与清零（`<=0`）点。
3. 把每个置/清条件对应到一句中文（如「需 ACK 的帧发完 → 置 1」「收到 quit_retrans → 清 0」）。

**需要观察的现象**：`retrans_in_progress` 的清零条件不止一个（成功收 ACK、放弃、超时计数到上限），置位条件只有一个（需 ACK 的帧发完）。

**预期结果**：你能用一句话准确向别人解释这两个信号，而不是含糊地说「跟重传有关」。这正是阅读 IP 端口表（不少上游工程文档只给信号名不给语义）时最需要的功夫。

**注**：若想看 `slice_en` 在真实波形里如何随 TSF 翻转，需要在 Vivado 里开启 `XPU_ENABLE_DBG` 综合、用 ILA 抓 `slice_en0~3` 与 `tsf_pulse_1M`——这部分留作可选的本地验证（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：`backoff_done` 是寄存器输出还是组合逻辑输出？这对时序有什么影响？

> **参考答案**：组合逻辑输出（`assign backoff_done = (backoff_state==BACKOFF_WAIT_FOR_OWN);`）。好处是退避一数完立刻可见、不额外延迟一拍；代价是它在组合路径上，综合时要注意别让它进入过长的关键路径。`backoff_state` 本身是寄存器，所以 `backoff_done` 在状态切换后稳定。

**练习 2**：假设一个不需要 ACK 的广播帧刚发完，`retrans_in_progress` 会是多少？为什么？

> **参考答案**：是 0。因为 [tx_control.v:359-374](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/tx_control.v#L359-L374) 中，只有 `tx_pkt_need_ack==1` 的分支才会把 `retrans_in_progress<=1`；不需 ACK 的帧走 `else` 分支，直接 `tx_try_complete<=1` 并把 `retrans_in_progress<=0`——发完即结束，不进入重传循环。

**练习 3**：`slice_en` 的 4 位对应 openwifi 发射侧的什么概念？

> **参考答案**：对应 4 个发送队列（与 WMM 的 4 个接入类别 AC 对应，见 u4-l3 提到的 `tx_intf` 4 条 FIFO）。`time_slice_gen` 按 TSF 节拍让某一位置 1，表示「当前时间切片允许该队列发包」，从而在硬件层实现多队列的时分调度与优先级。

---

## 5. 综合实践

**任务**：为本讲「xpu 顶层」产出一份一页纸的《xpu 信号速查卡》，把本讲四个学习块串起来。

请完成以下三件事：

1. **端口分组表**：按 4.2 节的五个方向，从 [xpu.v 端口区 L27-156](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L27-L156) 整理出每个方向的关键信号及方向（至少各列 3 个），并在每个信号后用一句话写出你推断的功能。
2. **内部装配图**：画一张简图，标出 `tsf_timer`、`cca`、`csma_ca`、`tx_control`、`phy_rx_parse`、`pkt_filter_ctl`、`rssi`、`time_slice_gen` 之间的主要连线（提示：以 `tsf_pulse_1M`、`ch_idle`、`backoff_done`、`block_rx_dma_to_ps`、`byte_in` 为骨架）。
3. **关键信号释义**：用你自己的话写清 `backoff_done`、`retrans_in_progress`、`slice_en[3:0]` 三个信号的含义，并各举一个「这个信号为 1 / 为 0 时 `xpu` 在做什么」的例子。

完成后，你应该能把这张速查卡递给一个没读过 `xpu.v` 的同事，让他/她在 5 分钟内知道 `xpu` 是干什么的、和谁对话、关键指挥信号有哪些。

> **检查点**：如果第 2 步的图里，`csma_ca` 的输出 `backoff_done` 没有连到 `tx_control`，你的图就漏了「退避完成 → 触发发送/重传」这条最重要的控制链，请回看 4.4.3。

## 6. 本讲小结

- `xpu`（eXternal Processing Unit）是 openwifi 的低层 MAC 控制中枢，把 CSMA/CA、TX/重传、TSF、接收解析与过滤、RSSI/CCA、AD9361 SPI 控制等微秒级实时逻辑集中在一个 PL IP 内。
- `xpu.v` 顶层自身几乎不写状态机，它是「装配 + 连线 + 一层组合逻辑」：13 类子模块靠 wire 互联，复杂算法都下沉到子模块。
- `xpu` 的对外端口可按 `rx_intf` / `openofdm_rx` / `tx_intf(phy_tx)` / `side_ch` / `PS(AXI+SPI+GPIO)` 五个方向分组，作者在端口区已用 `// Ports to ...` 注释标好。
- `xpu` 用统一的软复位机制 `rstn = s00_axi_aresetn & (~slv_reg0[k])`，软件写 `slv_reg0` 不同位即可单独复位某个子模块。
- `backoff_done`（来自 `csma_ca`，退避完成赢得信道）、`retrans_in_progress`（来自 `tx_control`，帧在等 ACK/重传循环中）、`slice_en[3:0]`（来自 `time_slice_gen`，4 队列时分门控）是 `xpu` 指挥发射的三大关键信号。
- 软件经 AXI4-Lite 读写 `slv_reg0`~`slv_reg63`（写寄存器 0-31、读回寄存器 57-63）来配置与观测 `xpu`，是「软件 ↔ 硬件契约」。

## 7. 下一步学习建议

本讲只打开了 `xpu` 顶层这只「盒子」。后续 u5 单元会逐个子模块深入，建议按依赖顺序阅读：

- **u5-l2 CSMA/CA 信道接入**：进 `csma_ca.v`，看 DIFS/EIFS/SIFS/Slot 与随机退避、NAV 如何用 `tsf_pulse_1M` 实现，理解 `backoff_done` 的完整产生过程。
- **u5-l3 TX 控制、重传与 ACK**：进 `tx_control.v`，读 `tx_control_state` 状态机，彻底搞清 `retrans_in_progress`、`tx_try_complete`、`quit_retrans` 的跳转。
- **u5-l4 TSF 定时器与接收包解析/过滤**：读 `tsf_timer.v`、`phy_rx_parse.v`、`pkt_filter_ctl.v`，看 TSF 心跳如何产生、MAC 头如何被解析、地址过滤如何控制 `block_rx_dma_to_ps`。
- **u5-l5 CCA、RSSI 与 AD9361 SPI 控制**：读 `cca.v`、`rssi.v` 及其 I/Q 处理链，以及 `spi.v`，看能量如何变成 `ch_idle`、`xpu` 如何配置射频前端。

此外，若你想现在就理解 `xpu_s_axi` 的寄存器地址映射，可先跳读 **u7-l1 AXI 寄存器映射与软件交互**，再带着「软件怎么操作这些 `slv_reg`」的视角回到本单元。
