# JESD204 框架

## 1. 本讲目标

JESD204 是高速数据转换器（ADC/DAC/射频收发器）与 FPGA 之间的高速串行链路标准。ADI 的 HDL 仓库把这条链路实现成一套**分层框架** `library/jesd204`，它是全仓最复杂的子系统之一。

学完本讲，你应该能够：

- 说清 JESD204 标准「物理层 / 链路层 / 传输层 / 应用层」四层模型，并把 `library/jesd204` 下的每个子目录归类到对应层。
- 理解 `axi_jesd204_rx` / `axi_jesd204_tx` 如何用一个 AXI4-Lite「瘦壳」把纯组合逻辑/时序逻辑的链路层核心包成 CPU 可控的 IP，以及它的寄存器如何被拆分到三个子模块。
- 理解传输层 `ad_ip_jesd204_tpl_adc`（接收/解帧）与 `ad_ip_jesd204_tpl_dac`（发送/组帧）如何把「链路层字节流」与「数据转换器通道采样」互相转换，并知道它们各自的寄存器面做了什么。
- 看懂一个真实工程（如 `daq3`）里从 GT 串行比特到 PS DDR 的完整分层流转。

本讲承接 u4-l5（`up_axi` 寄存器映射）与 u5-l2（数据转换器 IP 双通路）。在 u5-l2 中你已见过 `axi_ad9361` 这类 **LVDS 并行接口** 的数据转换器；本讲把视线移到 **JESD204 串行接口**，你会发现顶层同样是「寄存器面 + 数据面」，只是数据面换成了分层更深的串行协议栈。

## 2. 前置知识

### 2.1 为什么需要 JESD204

高采样率 ADC/DAC（例如 12 位、3 GSPS 的双通道 ADC）若用 LVDS 并行总线把每位采样都拉一根线，引脚数会爆炸：一个 16 位、双通道、每通道多路并行的器件可能要几十对差分线，PCB 布线、引脚占用与时序对齐都极难做。

JESD204 的核心思路是：**用少量高速串行通道（lane）替代大量并行 LVDS 线**。一条 lane 就是一对差分 CML 线，跑在几 Gbps 到几十 Gbps。比如 JESD204B 单 lane 可到 12.5 Gbps，JESD204C（64B/66B 编码）单 lane 可到 32.5 Gbps，单条链路最多 32 条 lane。引脚少了，布局简单了，还能做「通道对齐」「确定性延迟」「多芯片同步」。

### 2.2 四层模型与关键术语

JESD204B/C 标准把链路分成若干层，ADI 的 HDL 实现明确对应其中三层（应用层留给用户）：

| 层 | 职责 | ADI HDL 对应 |
| --- | --- | --- |
| 物理层（Physical） | 高速串行收发器（GT）的电气与 8B/10B（或 64B/66B）编解码 | `axi_adxcvr`、`util_adxcvr`、`jesd204_soft_pcs_*`、`jesd204_versal_gt_adapter_*` |
| 链路层（Link） | 协议处理：扰码/解扰、通道对齐、CGS、ILAS、LMFC、错误监控 | `jesd204_rx`、`jesd204_tx`、`axi_jesd204_rx/tx`（加 AXI 壳） |
| 传输层（Transport） | 转换器数据的组帧/解帧：把通道采样映射成字节流（或反向） | `ad_ip_jesd204_tpl_adc`（接收）、`ad_ip_jesd204_tpl_dac`（发送） |
| 应用层（Application） | 用户自定义信号处理 | `util_cpack2/upack2`、`axi_dmac`、用户算法 |

理解协议还需要一组「链路参数」（在 [docs/library/jesd204/index.rst:103-113](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/library/jesd204/index.rst#L103-L113) 有完整列表）：

- **L** — Lane 数（通道数）。
- **M** — Converter 数（转换器/通道数）。
- **F** — 每条 lane 每个 frame 的字节数（octets per frame per lane）。
- **S** — 每个转换器每个 frame 的采样数。
- **N / NP** — 转换器分辨率 / 每个采样的总位数（NP 通常补齐到字节边界，如 14 位补到 16 位）。
- **K** — 每个 multiframe 的 frame 数。

另外几个时序术语也很关键（同上文档 :115-132 行）：

- **link clock**（链路时钟）— 喂给链路层的并行时钟，JESD204B 下等于 line rate / 40 或 / 80，JESD204C 64B/66B 下等于 line rate / 66。
- **device clock**（器件时钟）— 帧时钟的整数倍，用于链路、传输与应用层。
- **SYSREF** — 慢速、高摆率信号，用来复位各器件的时钟分频器（含 LMFC 计数器），是实现「确定性延迟」的主参考。
- **LMFC**（Local Multi-frame Clock）— 本地多帧时钟，接收端用它判定何时释放弹性缓冲、对齐 lane。
- **CGS**（Code Group Synchronization）与 **ILAS**（Initial Lane Alignment Sequence）— 链路建立的两个握手阶段。

## 3. 本讲源码地图

本讲涉及的关键文件，按层归类：

| 文件 | 所属层 | 作用 |
| --- | --- | --- |
| `library/jesd204/axi_jesd204_rx/axi_jesd204_rx.v` | 链路层（AXI 壳） | 接收链路的 AXI4-Lite 顶层，拆分寄存器 |
| `library/jesd204/axi_jesd204_rx/jesd204_up_rx.v` | 链路层（寄存器） | 接收链路 lane 级状态/ILAS/缓冲寄存器译码 |
| `library/jesd204/axi_jesd204_tx/axi_jesd204_tx.v` | 链路层（AXI 壳） | 发送链路的 AXI4-Lite 顶层 |
| `library/jesd204/jesd204_rx/jesd204_rx.v` | 链路层（协议核心） | 接收协议处理：CGS/ILAS/解扰/对齐/弹性缓冲 |
| `library/jesd204/jesd204_tx/jesd204_tx.v` | 链路层（协议核心） | 发送协议处理：扰码/组帧/ILAS 注入 |
| `library/jesd204/jesd204_common/jesd204_lmfc.v` 等 | 链路层（公共原语） | LMFC、扰码器、CRC12、帧标记等被 rx/tx 共用的小模块 |
| `library/jesd204/jesd204_soft_pcs_rx/jesd204_soft_pcs_rx.v` | 物理层（软 PCS） | 8B/10B 解码（无硬 PCS 时用） |
| `library/jesd204/ad_ip_jesd204_tpl_adc/ad_ip_jesd204_tpl_adc.v` | 传输层（ADC） | 接收侧传输层顶层：解帧 + 寄存器 |
| `library/jesd204/ad_ip_jesd204_tpl_adc/ad_ip_jesd204_tpl_adc_deframer.v` | 传输层（ADC） | 把字节流解帧成各通道采样 |
| `library/jesd204/ad_ip_jesd204_tpl_dac/ad_ip_jesd204_tpl_dac.v` | 传输层（DAC） | 发送侧传输层顶层：组帧 + DDS + 寄存器 |
| `library/jesd204/axi_jesd204_rx/Makefile` | 构建 | 展示链路层 IP 的多厂商依赖桶 |
| `library/jesd204/scripts/jesd204.tcl` | 工程连线 | 把链路层 + 传输层拼成层级 BD 的 Tcl 助手 |
| `projects/daq3/common/daq3_bd.tcl` | 应用层（参考工程） | 完整 JESD204 收发链路实例 |

## 4. 核心概念与源码讲解

### 4.1 JESD204 分层结构

#### 4.1.1 概念说明

ADI 的 JESD204 HDL 解严格遵循标准分层，这一点官方文档 [docs/library/jesd204/index.rst:247-266](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/library/jesd204/index.rst#L247-L266) 说得很明确：

> The JESD204B/C standard defines multiple layers ... The Analog Devices JESD204B/C HDL solution follows the current standard and defines 4 layers. Physical layer, link layer, transport layer and application layer.

三层由 ADI 提供「标准组件」，应用层由用户实现。组件的选择规则是：

- **物理层** 由 FPGA 决定（Xilinx 用 GTXE2/GTHE3/GTHE4/GTYE4，Versal 用 GTY/GTYP，Intel 用 Arria10/Stratix10/Agilex 收发器）。
- **传输层** 由被连接的数据转换器决定（ADC 用 `tpl_adc`，DAC 用 `tpl_dac`）。
- **链路层** 由链路方向决定（接收用 `rx`，发送用 `tx`）。

这给了「组合」的灵活性：同一个传输层（`tpl_adc`）可以挂在不同 FPGA 的不同物理层（Xilinx GT / Intel 收发器）之上，只要链路层接口一致。

`library/jesd204` 的子目录命名几乎就是一张分层表：

- 物理层相关：`axi_adxcvr`（在 `library/` 下，不在 `jesd204/` 内）、`jesd204_soft_pcs_rx/tx`、`jesd204_versal_gt_adapter_rx/tx`。
- 链路层：`jesd204_rx`、`jesd204_tx`、`axi_jesd204_rx/tx`、`axi_jesd204_common`、`jesd204_common`、`jesd204_rx/tx_static_config`。
- 传输层：`ad_ip_jesd204_tpl_adc`、`ad_ip_jesd204_tpl_dac`、`ad_ip_jesd204_tpl_common`。

#### 4.1.2 核心流程

数据从 ADC 到 FPGA 内存的**接收（RX）**分层流转（自下而上）：

```text
ADC 芯片
  │  高速串行比特（CML 差分 lane 0..L-1）
  ▼
物理层  util_adxcvr / axi_adxcvr（GT 收发器 + 8B/10B 解码硬件）
  │  并行字符：phy_data[L×32], phy_charisk[L×4], phy_disperr ...
  ▼
链路层  jesd204_rx（CGS→ILAS→解扰→通道对齐→弹性缓冲释放）
  │  对齐后的字节流：rx_data, rx_valid, rx_sof/eof/somf/eomf
  ▼
传输层  ad_ip_jesd204_tpl_adc（deframer：按 L/M/F/S/N/NP 解帧）
  │  各通道采样：adc_data[M×...], adc_valid[M]
  ▼
应用层  util_cpack2 → axi_dmac → PS DDR
```

发送（TX）方向相反：应用层（`axi_dmac` + `util_upack2`）→ 传输层 `tpl_dac`（framer 把采样打包成字节流）→ 链路层 `jesd204_tx`（扰码/组帧/ILAS）→ 物理层 GT → DAC 芯片。

链路层在 JESD204B（8B/10B）模式下要经过两个握手阶段才能进入 DATA 相：

1. **CGS（Code Group Synchronization）**：接收端拉低 `SYNC~`，发送端发 K28.5 训练码，接收端完成字符边界对齐。
2. **ILAS（Initial Lane Alignment Sequence）**：发多帧特殊序列 `/R/…/A/`，完成 lane 间对齐，并把链路参数（L/M/F/S/N/NP/K）随 `/Q/` 帧广播。

JESD204C（64B/66B）模式下没有 `SYNC~` 与 ILAS，改用 Sync Header 与 EoMB（End of Extended Multiblock），由 `*_64b` 模块处理。

#### 4.1.3 源码精读

**链路层接收核心 `jesd204_rx` 的端口**直接暴露了它与上下层的接口（[library/jesd204/jesd204_rx/jesd204_rx.v:24-55](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/jesd204/jesd204_rx/jesd204_rx.v#L24-L55)）：

```verilog
input [DATA_PATH_WIDTH*8*NUM_LANES-1:0] phy_data,       // 来自物理层的并行字符
input [DATA_PATH_WIDTH*NUM_LANES-1:0] phy_charisk,       // 控制字符指示
input [DATA_PATH_WIDTH*NUM_LANES-1:0] phy_notintable,
input [DATA_PATH_WIDTH*NUM_LANES-1:0] phy_disperr,
...
output [TPL_DATA_PATH_WIDTH*8*NUM_LANES-1:0] rx_data,    // 解出后给传输层
output rx_valid,
output [TPL_DATA_PATH_WIDTH-1:0] rx_eof,                 // frame/multiframe 边界
output [TPL_DATA_PATH_WIDTH-1:0] rx_sof,
output [TPL_DATA_PATH_WIDTH-1:0] rx_eomf,
output [TPL_DATA_PATH_WIDTH-1:0] rx_somf,
```

即：物理层喂进 `phy_*`（每条 lane 每拍 `DATA_PATH_WIDTH` 个字符），链路层吐出 `rx_data` + 一组帧边界信号（`sof`/`eof`/`somf`/`eomf`）。这些边界信号正是传输层解帧所依赖的「刻度」。

`jesd204_rx` 内部用 `generate` 按 `LINK_MODE` 二选一（[library/jesd204/jesd204_rx/jesd204_rx.v:296](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/jesd204/jesd204_rx/jesd204_rx.v#L296) 与 :460）：

```verilog
if (LINK_MODE[0] == 1) begin : mode_8b10b   // JESD204B：8B/10B
   ... jesd204_rx_ctrl / jesd204_rx_lane ...
end
if (LINK_MODE[1] == 1) begin : mode_64b66b  // JESD204C：64B/66B
   ... jesd204_rx_ctrl_64b / jesd204_rx_lane_64b ...
end
```

8B/10B 分支里，`jesd204_rx_ctrl`（[jesd204_rx.v:301](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/jesd204/jesd204_rx/jesd204_rx.v#L301)）是链路状态机，驱动 CGS/ILAS；`jesd204_rx_lane`（[jesd204_rx.v:342](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/jesd204/jesd204_rx/jesd204_rx.v#L342)）是每条 lane 的解扰/对齐/弹性缓冲。注意每条 lane 被独立例化（`for (i=0; i<NUM_LANES; i=i+1)`），这是 JESD204「逐 lane 处理再对齐」的体现。

链路层与传输层之间的 **LMFC** 计算逻辑也值得一看（[jesd204_rx.v:107-121](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/jesd204/jesd204_rx/jesd204_rx.v#L107-L121)）。LMFC 计数器宽度由 `MAX_BEATS_PER_MULTIFRAME` 决定，向上取整到刚好够用的位宽，节省资源：

\[ \text{MAX\_BEATS\_PER\_MULTIFRAME} = \frac{\text{MAX\_OCTETS\_PER\_MULTIFRAME}}{\text{DATA\_PATH\_WIDTH}} \]

**物理层的软 PCS**：当 FPGA 没有硬 8B/10B 解码器（或需要自定义处理）时，用 `jesd204_soft_pcs_rx` 软实现解码。它逐 lane、逐字符调用 `jesd204_8b10b_decoder`（[library/jesd204/jesd204_soft_pcs_rx/jesd204_soft_pcs_rx.v:105-112](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/jesd204/jesd204_soft_pcs_rx/jesd204_soft_pcs_rx.v#L105-L112)），输出 `char/charisk/notintable/disperr`，正好喂给 `jesd204_rx` 的 `phy_*` 端口。这就是「物理层与链路层之间的标准接口」。

#### 4.1.4 代码实践

**实践目标**：把 `library/jesd204` 的子目录按层归类，建立「目录即分层」的直觉。

**操作步骤**：

1. 进入 `library/jesd204` 目录，列出所有子目录（参考本讲 §3 末尾的 `ls` 结果）。
2. 对每个子目录，依据其名字前缀（`axi_adxcvr`/`util_adxcvr`/`*_soft_pcs*`/`*_gt_adapter*` → 物理层；`jesd204_rx`/`jesd204_tx`/`axi_jesd204_*` → 链路层；`ad_ip_jesd204_tpl_*` → 传输层）归到对应层。
3. 打开 `docs/library/jesd204/index.rst`，对照其中的「Physical Layer / Link Layer / Transport Layer」三节（:299-339 行）核对你的归类。

**需要观察的现象**：传输层只有两个（ADC/DAC），但物理层有好几个（对应不同 FPGA 家族的 GT）；链路层也分「带 AXI 壳的 `axi_jesd204_*`」与「纯协议核心 `jesd204_rx/tx`」两套。这正是「物理层随 FPGA 变、传输层随转换器变、链路层随方向变」的物化。

**预期结果**：你能写出一张三列（物理/链路/传输）的分类表，并解释为什么 `axi_jesd204_common` 不属于传输层而属于链路层（它放的是被 rx 与 tx 共用的 `jesd204_up_common`、`jesd204_up_sysref` 寄存器模块）。

#### 4.1.5 小练习与答案

**练习 1**：`jesd204_rx_static_config` 与 `jesd204_tx_static_config` 这两个目录属于哪一层？它们解决什么问题？

> **答案**：属于链路层。它们在综合期把链路配置（lane 数、octets per frame 等）固化成常量，省去运行时通过寄存器配置的开销与可配置逻辑，用于对配置完全确定、想节省资源的场景。

**练习 2**：为什么传输层要分 `tpl_adc` 与 `tpl_dac` 两个独立 IP，而不是一个 IP 双向？

> **答案**：因为 ADC 接收与 DAC 发送的组帧方向相反——`tpl_adc` 做解帧（字节流→采样，含 PN 序列监测、符号扩展），`tpl_dac` 做组帧（采样→字节流，含 DDS 数字本振、IQ 校正、pattern 发生）。两者的寄存器集、数据通路与附加功能差异很大，按 JESD204 标准的「传输层由转换器类型决定」拆成两个 IP 更清晰。

---

### 4.2 axi_jesd204_rx/tx 控制接口

#### 4.2.1 概念说明

`jesd204_rx` / `jesd204_tx` 是**纯协议核心**，本身没有 CPU 接口——它只有一排 `cfg_*` 配置输入和 `status_*` 状态输出。但软件必须能配置 lane 使能、读取链路状态、处理中断。于是 ADI 加了一层「AXI 壳」：`axi_jesd204_rx` / `axi_jesd204_tx`。

这个壳的模式你在 u4-l5 已经见过（`up_axi` 把 AXI4-Lite 翻译成 `up_wreq/up_rreq`）。这里的特别之处在于：**壳不直接做寄存器译码，而是把 `up_*` 请求分发给三个子模块**，每个子模块负责一组寄存器。这是一种「按地址段切分寄存器面」的设计。

`LINK_MODE` 参数（1 = 8B/10B 的 JESD204B，2 = 64B/66B 的 JESD204C）决定数据通路宽度：8B/10B 时 `DATA_PATH_WIDTH=4`，64B/66B 时为 8（[axi_jesd204_rx.v:14-16](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/jesd204/axi_jesd204_rx/axi_jesd204_rx.v#L14-L16)）。

#### 4.2.2 核心流程

`axi_jesd204_rx` 内部的数据流（[axi_jesd204_rx.v:140-313](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/jesd204/axi_jesd204_rx/axi_jesd204_rx.v#L140-L313)）：

```text
CPU  ──AXI4-Lite──▶  up_axi（桥）  ──up_wreq/up_rreq/up_waddr/up_raddr──▶  三路分发
                                                                            │
                                       ┌────────────────────────────────────┤
                                       ▼                  ▼                   ▼
                            jesd204_up_common     jesd204_up_sysref      jesd204_up_rx
                            (版本/ID/lane使能/    (SYSREF/LMFC偏移/      (lane状态/ILAS/
                             octets_per_frame/    buffer_early_release/  弹性缓冲延迟/
                             scrambler 等)        buffer_delay/lmfc_off) 帧对齐错误统计)
                                       │                  │                   │
                                       └──────── up_rdata 各自只填自己地址段 ─┘
                                                       │  或运算合并
                                                       ▼
                                            up_rdata <= common | sysref | rx
                                                       │
                            core_cfg_* / device_cfg_* ─▶ 链路核心（跨时钟域）
                            core_status_* / device_event_* ─▶ 经 sync_* 采回 AXI 域
```

关键点：

1. **写**：CPU 写一个地址，`up_axi` 产生一次 `up_wreq`，三个子模块都「看到」这次写，但只有地址落在自己段内的那个真正改状态（其余忽略）。`up_wack` 只是对 `up_wreq` 打一拍。
2. **读**：读时三个子模块各自给出 `up_rdata_*`，未命中的段返回 0，顶层把三者**按位或**合并（[axi_jesd204_rx.v:313](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/jesd204/axi_jesd204_rx/axi_jesd204_rx.v#L313)）。这要求每个寄存器在任意时刻只能由一个子模块驱动非零，靠地址段不重叠保证。
3. **跨时钟域**：寄存器面在 `s_axi_aclk`（CPU 时钟），协议核心在 `core_clk`（链路时钟）与 `device_clk`（器件时钟）。配置从 AXI 域进核心、状态/事件从核心回 AXI 域，都经 `sync_bits`/`sync_event`/`sync_data` 跨域。例如帧对齐错误事件用 `sync_event` 采回（[axi_jesd204_rx.v:125-133](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/jesd204/axi_jesd204_rx/axi_jesd204_rx.v#L125-L133)）。

`axi_jesd204_tx` 结构几乎对称（写、读合并、跨域都一样），差别在：TX 没有 RX 的 `event_frame_alignment_error`（发送端不检测帧对齐错误），中断触发常驻 0（[axi_jesd204_tx.v:114](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/jesd204/axi_jesd204_tx/axi_jesd204_tx.v#L114)）；TX 多了 `continuous_cgs`/`continuous_ilas`/`skip_ilas`/`mframes_per_ilas` 等发送侧特有的 ILAS 控制位。

#### 4.2.3 源码精读

**身份寄存器**：每个 IP 都在顶层定义了 `PCORE_VERSION` 与 `PCORE_MAGIC`，RX 的 magic 是 `32'h32303452`（ASCII `"204R"`），TX 是 `"204T"`（[axi_jesd204_rx.v:93-94](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/jesd204/axi_jesd204_rx/axi_jesd204_rx.v#L93-L94)、[axi_jesd204_tx.v:85-86](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/jesd204/axi_jesd204_tx/axi_jesd204_tx.v#L85-L86)）。软件读 magic 寄存器即可确认「这个地址上挂的确实是 JESD204 RX/TX」，这是一种常见的自检约定。

**up_axi 例化**：注意它把 14 位 AXI 地址（`AXI_ADDRESS_WIDTH=14`）翻译成 12 位字地址 `up_waddr/up_raddr`（[axi_jesd204_rx.v:140-169](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/jesd204/axi_jesd204_rx/axi_jesd204_rx.v#L140-L169)）。字节地址右移两位去掉最低字节位，这与 u4-l5 讲的「字节地址→字地址」一致。

**寄存器三路分发与读合并**（[axi_jesd204_rx.v:264-315](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/jesd204/axi_jesd204_rx/axi_jesd204_rx.v#L264-L315)）：

```verilog
jesd204_up_rx #(...) i_up_rx (
   .up_rreq(up_rreq), .up_raddr(up_raddr), .up_rdata(up_rdata_rx),
   .up_wreq(up_wreq), .up_waddr(up_waddr), .up_wdata(up_wdata), ...);

always @(posedge s_axi_aclk) begin
   up_wack <= up_wreq;
   up_rreq_d1 <= up_rreq;
   up_rack <= up_rreq_d1;
   if (up_rreq_d1 == 1'b1) begin
      up_rdata <= up_rdata_common | up_rdata_sysref | up_rdata_rx;  // 三段按位或
   end
end
```

注意 RX 比 TX 多打了一拍 `up_rreq_d1`——注释（:309-310）解释：ILAS 存储器读数据要一个时钟周期才就绪，所以 RX 的读应答比 TX 晚一拍。这种细节正是源码注释的价值。

**lane 级寄存器 `jesd204_up_rx`**：它把每条 lane 的状态（CGS 状态、帧对齐错误计数、lane 延迟、ILAS 内容）打包成宽总线，并用 `sync_data` 从 `core_clk` 跨域到 `up_clk`（[jesd204_up_rx.v:62-70](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/jesd204/axi_jesd204_rx/jesd204_up_rx.v#L62-L70)）。软件能逐 lane 查「这条 lane 有没有进入 DATA 相」「帧对齐错了多少次」。

#### 4.2.4 代码实践

**实践目标**：理解「写广播、读合并」的寄存器分发机制，并验证 TX/RX 壳的对称差异。

**操作步骤**：

1. 打开 `axi_jesd204_rx.v`，找到第 305-315 行的 `always` 块，确认读路径是 `up_rdata_common | up_rdata_sysref | up_rdata_rx`。
2. 打开 `axi_jesd204_tx.v` 第 276-282 行，对比 TX 的同名块。注意 TX 没有 `up_rreq_d1` 这一拍。
3. 在两份文件里分别搜索 `up_irq_trigger`：RX 用了真实的帧对齐错误事件（[axi_jesd204_rx.v:135-138](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/jesd204/axi_jesd204_rx/axi_jesd204_rx.v#L135-L138)），TX 直接给 0。

**需要观察的现象**：RX 的 `up_extra_cfg` 只有 8 位（帧对齐错误阈值），而 TX 的 `up_extra_cfg` 有 11 位（多了 `continuous_cgs`/`continuous_ilas`/`skip_ilas`/`mframes_per_ilas`，见 [axi_jesd204_tx.v:191-196](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/jesd204/axi_jesd204_tx/axi_jesd204_tx.v#L191-L196)）。这反映了发送端需要控制 ILAS 行为，而接收端只需要被动等待并设阈值。

**预期结果**：你能用自己的话说出「为什么 RX 读路径多一拍」「为什么 TX 没有帧对齐错误中断」。

#### 4.2.5 小练习与答案

**练习 1**：三个 `up_*` 子模块的读数据用按位或合并，为什么不会冲突？

> **答案**：每个子模块只在地址落在自己负责的段内时才驱动非零数据，其余地址段输出 0。只要各段地址不重叠，按位或就等价于「选中谁就用谁的数据」。这是 ADI 多处 `*_regmap` 共用的「地址分区 + 或合并」套路。

**练习 2**：`PCORE_MAGIC = 32'h32303452` 代表什么？软件何时会读它？

> **答案**：它是 ASCII 字符串 `"204R"`（R 代表 RX）。驱动加载时常读这个 magic 寄存器，确认地址映射正确、IP 类型匹配，再做后续配置；不匹配则报错避免误操作。

---

### 4.3 transport 层 tpl_adc/tpl_dac

#### 4.3.1 概念说明

传输层是 JESD204 与「具体数据转换器」贴得最近的一层。链路层只关心「字节流 + 帧边界」，不关心这些字节代表几个通道、每个采样多少位。传输层负责：

- **ADC 接收（`ad_ip_jesd204_tpl_adc`）**：把链路层吐出的字节流，按 JESD 规则**解帧**成「每通道 N 位采样」，并做数据格式化（符号扩展/补码）、伪随机序列（PN）监测。
- **DAC 发送（`ad_ip_jesd204_tpl_dac`）**：把每通道采样**组帧**成字节流喂给链路层，并集成 DDS（数字本振，可发单音）、IQ 校正、pattern 发生等发送侧特有功能。

传输层自己也有 AXI4-Lite 寄存器面（数据格式、PN 选择、DDS 参数、通道使能、同步控制等），所以每个 `tpl_*` 也是独立的 IP。

#### 4.3.2 核心流程

传输层的关键是 JESD 参数到硬件位宽的换算。以 `tpl_adc` 为例（[ad_ip_jesd204_tpl_adc.v:113-117](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/jesd204/ad_ip_jesd204_tpl_adc/ad_ip_jesd204_tpl_adc.v#L113-L117)）：

\[ \text{DATA\_PATH\_WIDTH} = \frac{\text{OCTETS\_PER\_BEAT} \times 8 \times \text{NUM\_LANES}}{\text{NUM\_CHANNELS} \times \text{BITS\_PER\_SAMPLE}} \]

\[ \text{BYTES\_PER\_FRAME} = \frac{\text{NUM\_CHANNELS} \times \text{BITS\_PER\_SAMPLE} \times \text{SAMPLES\_PER\_FRAME}}{8 \times \text{NUM\_LANES}} \]

其中 `OCTETS_PER_BEAT` 是「每条 lane 每拍传几个字节」（即链路层的 `DATA_PATH_WIDTH`，8B/10B 下为 4）。这两个式子本质就是 JESD 标准 \(F = MS\cdot N_P/(8L)\) 的硬件化。

`tpl_adc` 的内部同样是「寄存器面 + 数据面」（[ad_ip_jesd204_tpl_adc.v:135-240](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/jesd204/ad_ip_jesd204_tpl_adc/ad_ip_jesd204_tpl_adc.v#L135-L240)）：

- 寄存器面 `ad_ip_jesd204_tpl_adc_regmap`：配置数据格式（`dfmt_enable`/`dfmt_sign_extend`/`dfmt_type`）、PN 序列选择、通道使能、ADC 复位/同步，并把 JESD 参数 \(M/L/S/F/N/N_P\) 通过 `up_tpl_common` 回读给软件（只读，便于软件核对实际综合参数）。
- 数据面 `ad_ip_jesd204_tpl_adc_core`：消费 `link_valid`/`link_data`/`link_sof`，内部含 `deframer`（解帧）与每通道的数据格式化、PN 监测，产出 `adc_valid`/`adc_data`。

解帧器 `ad_ip_jesd204_tpl_adc_deframer` 的核心是一组由 `NUM_LANES`/`NUM_CHANNELS`/`BITS_PER_SAMPLE` 决定的位拼接网络（[ad_ip_jesd204_tpl_adc_deframer.v:62-66](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/jesd204/ad_ip_jesd204_tpl_adc/ad_ip_jesd204_tpl_adc_deframer.v#L62-L66)）：

```verilog
localparam BITS_PER_LANE_PER_FRAME = BITS_PER_CHANNEL_PER_FRAME * NUM_CHANNELS / NUM_LANES;
localparam FRAMES_PER_BEAT = OCTETS_PER_BEAT * 8 / BITS_PER_LANE_PER_FRAME;
```

它告诉硬件：一拍里有几个完整 frame、一条 lane 一个 frame 占多少位——据此把字节流重新切分成各通道采样。`link_sof` 信号提供 frame 起点对齐，保证切分从正确位置开始。

`tpl_dac` 的结构与 `tpl_adc` 镜像：`ad_ip_jesd204_tpl_dac_core` 内含 `framer`（组帧，方向相反），外加 DDS 子系统（`dac_dds_scale/init/incr` 等参数）、IQ 校正（`dac_iqcor_*`）、pattern 发生（`dac_pat_data_*`、`dac_data_sel`）。注意 `tpl_dac` 顶层还有一段组合逻辑（[ad_ip_jesd204_tpl_dac.v:282-291](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/jesd204/ad_ip_jesd204_tpl_dac/ad_ip_jesd204_tpl_dac.v#L282-L291)）根据 `PADDING_TO_MSB_LSB_N` 决定从 DMA 数据的高位还是低位取 `BITS_PER_SAMPLE` 位——这是处理「DMA 位宽 > 转换器分辨率」时的对齐选择。

#### 4.3.3 源码精读

**链路层↔传输层接口约定**：`tpl_adc` 顶层端口清晰标注了两侧（[ad_ip_jesd204_tpl_adc.v:60-74](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/jesd204/ad_ip_jesd204_tpl_adc/ad_ip_jesd204_tpl_adc.v#L60-L74)）：

```verilog
// jesd interface    （连链路层）
input link_clk,
input [OCTETS_PER_BEAT-1:0] link_sof,
input link_valid,
input [NUM_LANES*8*OCTETS_PER_BEAT-1:0] link_data,
output link_ready,
// dma interface     （连应用层/DMA 侧）
output [NUM_CHANNELS-1:0] enable,
output [NUM_CHANNELS-1:0] adc_valid,
output [DMA_BITS_PER_SAMPLE * OCTETS_PER_BEAT * 8 * NUM_LANES / BITS_PER_SAMPLE-1:0] adc_data,
input adc_dovf,        // DMA FIFO 溢出反馈
```

这正是 §4.1 那张数据流图里「链路层 → 传输层 → 应用层」三段之间的物理接口。`adc_dovf`（overflow）回流是 u5-l2 讲过的「采集链关注溢出」——DMA 侧 FIFO 快满时拉高，传输层据此计数。

**真实工程的端到端连线**（[projects/daq3/common/daq3_bd.tcl:198-204](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/daq3/common/daq3_bd.tcl#L198-L204)）把抽象接口落到了具体连线上：

```tcl
ad_xcvrcon util_daq3_xcvr axi_ad9680_xcvr axi_ad9680_jesd {} {} {} $MAX_RX_NUM_OF_LANES  ;# 物理层↔链路层
ad_connect util_daq3_xcvr/rx_out_clk_0 axi_ad9680_tpl_core/link_clk                       ;# 链路时钟→传输层
ad_connect axi_ad9680_jesd/rx_sof          axi_ad9680_tpl_core/link_sof                    ;# 链路层→传输层
ad_connect axi_ad9680_jesd/rx_data_tdata   axi_ad9680_tpl_core/link_data
ad_connect axi_ad9680_jesd/rx_data_tvalid  axi_ad9680_tpl_core/link_valid
ad_connect axi_ad9680_tpl_core/adc_valid_0 axi_ad9680_cpack/fifo_wr_en                    ;# 传输层→应用层(cpack)
ad_connect axi_ad9680_cpack/fifo_wr_overflow axi_ad9680_tpl_core/adc_dovf                 ;# 溢出回流
```

这段 Tcl 把四层全部串起来：`util_daq3_xcvr`（物理层 GT）经 `ad_xcvrcon` 接到 `axi_ad9680_jesd`（链路层），链路层的 `rx_data_tdata/rx_data_tvalid/rx_sof` 接到 `axi_ad9680_tpl_core`（传输层），传输层的 `adc_valid_0` 接到 `util_cpack2` 再到 `axi_dmac`（应用层）。`ad_xcvrcon` 是 u3-l4 讲过的板级连线助手，这里负责把物理层 lane 映射到链路层 lane（DAC 侧还有逻辑 lane→物理 lane 的重排 `{0 2 3 1}`，见 [daq3_bd.tcl:170](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/daq3/common/daq3_bd.tcl#L170) 的注释）。

**层级拼装的 Tcl 助手**：工程里看到的 `axi_ad9680_jesd` 与 `axi_ad9680_tpl_core` 不是单个 IP，而是由 [library/jesd204/scripts/jesd204.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/jesd204/scripts/jesd204.tcl) 里的 `adi_axi_jesd204_rx_create` 与 `adi_tpl_jesd204_rx_create` 过程搭出的**层级 BD**（`create_bd_cell -type hier`）。前者把 `axi_jesd204_rx`（壳）与 `jesd204_rx`（核心）用 `ad_connect` 内部接好（[jesd204.tcl:110-115](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/jesd204/scripts/jesd204.tcl#L110-L115)），对外只露出 `s_axi`/`link_clk`/`rx_phy*`/`rx_data_tdata` 等少量管脚，屏蔽了壳与核心之间几十根 `cfg_*`/`status_*` 连线。`adi_tpl_jesd204_rx_create` 还会按 `adi_tpl_jesd204_tx_create`（[jesd204.tcl:176-188](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/jesd204/scripts/jesd204.tcl#L176-L188)）同样的公式自动算 `BYTES_PER_FRAME` 与 `samples_per_channel` 并传给传输层 IP 参数。

#### 4.3.4 代码实践

**实践目标**：用一个真实工程把「GT 串行 → 并行采样 → DDR」的分层流转说清楚。

**操作步骤**：

1. 打开 `projects/daq3/common/daq3_bd.tcl`，从第 91 行「adc peripherals」读到第 204 行。
2. 列出 RX 侧依次出现的 IP：`axi_ad9680_xcvr`、`axi_ad9680_jesd`、`axi_ad9680_tpl_core`、`axi_ad9680_cpack`、`axi_ad9680_dma`，并各自标注属于哪一层。
3. 跟踪 `rx_ref_clk_0`（参考时钟）如何经 `ad_xcvrpll`（:161）进入 `util_daq3_xcvr`，再以 `rx_out_clk_0` 形式同时驱动链路层与传输层的 `link_clk`（:199）。
4. 注意 `ad_xcvrcon`（:198）如何把物理层 GT 的 lane 数据接到链路层。

**需要观察的现象**：链路层 `axi_ad9680_jesd` 与传输层 `axi_ad9680_tpl_core` 共享同一个 `link_clk`（来自 `util_daq3_xcvr/rx_out_clk_0`），而 `axi_dmac` 用 `sys_dma_clk`（PS 提供的 DMA 时钟）——两套时钟域之间靠 cpack/offload 的 FIFO 跨域。这呼应了 u5-l3 讲的 CDC 机制。

**预期结果**：你能写一段话描述 AD9680 的 RF 采样如何变成 PS DDR 里的数据：ADC 串行 lane → GT（`util_adxcvr`）恢复成并行字符 → `jesd204_rx` 完成协议处理吐出 `rx_data` → `tpl_adc` 解帧成 4 通道 14 位采样 → `util_cpack2` 打包 → `axi_dmac` 搬进 DDR。其中「采样在传输层才真正出现，之前全是字节流/字符」是关键认知。

> **待本地验证**：若你有 Vivado 与 daq3 硬件，可在工程构建后用 Vivado 地址编辑器查看 `axi_ad9680_jesd` 与 `axi_ad9680_tpl_core` 各自分配的 AXI 地址段，对照 `docs/regmap` 下两者的寄存器表核对你读到的状态寄存器含义。

#### 4.3.5 小练习与答案

**练习 1**：`tpl_adc` 的 `link_sof` 信号如果断开（始终为 0），解帧器会出现什么问题？

> **答案**：`link_sof` 标记 frame 起点，是解帧器确定字节切分起点的依据。若丢失，解帧器虽然仍能消费数据，但无法保证通道与采样的边界对齐，会把采样错位切分，导致通道间数据串位、PN 监测报错。链路层与传输层必须共用 `link_clk` 且 `sof` 正确传递。

**练习 2**：为什么 `tpl_dac` 顶层要有一段根据 `PADDING_TO_MSB_LSB_N` 取高位或低位的组合逻辑，而 `tpl_adc` 没有？

> **答案**：DAC 发送方向上，DMA 送来的数据位宽（`DMA_BITS_PER_SAMPLE`）可能大于转换器分辨率（`BITS_PER_SAMPLE`），需要决定补齐位放在 MSB 还是 LSB、有效位从高 bits 还是低 bits 取。ADC 接收方向上，解帧后是先有原始 `CONVERTER_RESOLUTION` 位、再做格式化补齐到 `DMA_BITS_PER_SAMPLE`，方向相反，所以不需要这段取位逻辑。

**练习 3**：在 [library/jesd204/axi_jesd204_rx/Makefile](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/jesd204/axi_jesd204_rx/Makefile#L1-L43) 中，为什么 `jesd204_up_common.v` 在 Xilinx 侧通过 `XILINX_LIB_DEPS` 引用，而在 Intel 侧放在 `INTEL_DEPS` 里？

> **答案**：这正是 u4-l1/u4-l3 讲过的厂商不对称——Xilinx 走「跨库引用已打包 IP」（`XILINX_LIB_DEPS += jesd204/axi_jesd204_common`，把公共寄存器模块当作另一个已打包的 component.xml 来引用），Intel/Lattice 走「源码扁平嵌入」（`INTEL_DEPS += ../axi_jesd204_common/jesd204_up_common.v`，直接把源文件拉进本 IP 编译）。同一个 `.v` 文件因厂商流程不同落入不同依赖桶。

## 5. 综合实践

**任务**：以 `projects/daq3/common/daq3_bd.tcl` 为对象，画出该参考设计 **ADC 接收链** 与 **DAC 发送链** 的分层框图，并标注每一层用到的具体 IP 与关键连线信号。

要求：

1. 分别画 RX、TX 两张图（文本框图即可），从「ADC/DAC 芯片」画到「PS DDR」。
2. 每个方框标注：IP 实例名（如 `axi_ad9680_jesd`）、所属层（物理/链路/传输/应用）、对应的源码模块（如 `axi_jesd204_rx` + `jesd204_rx`）。
3. 在方框之间的连线上标注信号名：`phy_data/phy_charisk`、`rx_data_tdata/rx_data_tvalid/rx_sof`、`adc_data/adc_valid`、`dac_ddata/dac_valid`、`link_clk` 等。
4. 指出 DAC 侧的逻辑 lane 到物理 lane 的重排映射（提示：找 `ad_xcvrcon` 的 lane 列表参数与上方注释），并解释为什么需要这种重排。

**提示**：

- RX 侧参考本讲 §4.3.3 的连线摘录；TX 侧在 [daq3_bd.tcl:165-181](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/daq3/common/daq3_bd.tcl#L165-L181)，结构对称但方向相反，且 TX 侧在传输层与 DMA 之间多了 `util_upack2`（解包）而非 `util_cpack2`（打包）。
- lane 重排映射注释在 :166-169，参数 `{0 2 3 1}` 在 :170。物理 PCB 上 lane 走线为求等长/避开冲突，常不按逻辑顺序排列，FPGA 内部用 `ad_xcvrcon` 把逻辑 lane 重映射到物理 lane。

完成后再回到本讲 §4.1.2 的数据流图自查：你画的图是否覆盖了「CGS/ILAS/解扰/对齐」「解帧/组帧」「跨时钟域」这些关键环节。

## 6. 本讲小结

- JESD204 是数据转换器与 FPGA 间的高速串行链路标准，ADI 的 `library/jesd204` 严格按「物理 / 链路 / 传输 / 应用」四层实现，子目录命名几乎就是分层表。
- 物理层由 FPGA 决定（GT 收发器 + 可选软 PCS `jesd204_soft_pcs_*`），传输层由转换器决定（`tpl_adc`/`tpl_dac`），链路层由方向决定（`jesd204_rx`/`jesd204_tx`），这种正交让组件可灵活组合。
- `axi_jesd204_rx/tx` 是给纯协议核心套的 AXI4-Lite 壳，复用 `up_axi` 桥，并把寄存器按地址段拆给 `jesd204_up_common`/`up_sysref`/`up_rx(tx)` 三个子模块，读数据用「按位或」合并。
- `LINK_MODE` 参数区分 JESD204B（8B/10B，`DATA_PATH_WIDTH=4`）与 JESD204C（64B/66B，`=8`），链路核心用 `generate` 二选一。
- 传输层 `tpl_adc/tpl_dac` 做字节流与通道采样间的组帧/解帧，其位宽完全由 JESD 参数 \(L/M/F/S/N/N_P\) 决定；`tpl_dac` 还集成 DDS、IQ 校正、pattern 发生等发送侧功能。
- 真实工程（`daq3_bd.tcl`）通过 `ad_xcvrcon`（物理↔链路）、`link_data/valid/sof`（链路↔传输）、`adc_data/valid`（传输↔应用）把四层串成一条从 RF 采样到 PS DDR 的完整通路；壳与核心之间的细节连线被 `jesd204.tcl` 的层级 BD 助手封装。

## 7. 下一步学习建议

- **收发器与时序**：本讲把物理层当作黑盒，下一讲可读 u8-l3（收发器、时钟与时序约束），深入了解 `util_adxcvr`、`ad_xcvrcon`、`gtwizard_generator.tcl` 与 `auto_timing_fix_xilinx.tcl` 如何把 GT 与 JESD 链路接起来并收敛时序。
- **仿真**：`library/jesd204/tb/` 下有丰富的 testbench（`rx_tb`、`tx_tb`、`loopback_tb`、`soft_pcs_*`、`scrambler_tb` 等），可参照 u8-l1 的方法跑通一个 JESD204 环回仿真，观察 CGS→ILAS→DATA 的状态机跳变。
- **数据转换器 IP**：把本讲的传输层与 u5-l2 的 `axi_ad9361`（LVDS 接口）对比阅读，体会「串行 JESD204 vs 并行 LVDS」两种数据转换器接口在 IP 结构上的异同；进一步可看 `axi_adrv9001`、`axi_ad9081` 等基于 JESD204 的现代数据转换器 IP 如何复用本讲的传输层。
- **寄存器表**：在 `docs/library/jesd204/axi_jesd204_rx/`、`ad_ip_jesd204_tpl_adc/` 等页面查阅实际寄存器映射，配合 u4-l5 的 `up_axi` 知识，理解软件如何枚举 lane 状态、触发 SYSREF、施加确定性延迟。
