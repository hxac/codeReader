# 数据转换器 IP 模式：以 axi_ad9361 为例

## 1. 本讲目标

学完本讲，你应当能够：

- 说清一个 ADI「数据转换器 IP」（以 `axi_ad9361` 为代表）内部有哪**两条通路**——寄存器控制面与数据流通面，以及它们各自由哪些子模块组成。
- 在块设计脚本（`fmcomms2_bd.tcl`）里，完整追踪一条 **ADC capture（采集）链路**：从射频芯片的 LVDS 引脚，经 `util_wfifo` 跨时钟域、`util_cpack2` 通道打包，再到 `axi_dmac` 写进 PS DDR；以及反向的 **DAC playback（回放）链路**。
- 理解 `axi_ad9361_delay.tcl` 这类**校准脚本**在何时被运行、做了什么、产出什么。

本讲承接 u5-l1（`axi_dmac` 深入）与 u3-l4（`adi_board.tcl` 连线助手）。我们不再重复 DMA 引擎本身的细节，而是站在 DMA 的「上游」——看 DMA 搬的那一路采样数据到底从哪里来、又往哪里去。

## 2. 前置知识

阅读本讲前，建议你已经掌握以下概念（前序讲义已建立）：

- **ADC / DAC**：模数 / 数模转换器。ADC 把模拟信号采样成数字样本，DAC 反过来。AD9361 是一颗**射频收发器（RF transceiver）**，内部同时包含 ADC 与 DAC，外加射频前端，是软件无线电（SDR）的常用芯片。
- **IQ 数据**：通信里用一对同相（I）与正交（Q）分量表示一个复数采样。AD9361 工作在 2R2T（两收两发）时，每时刻有 4 路样本：I0、Q0、I1、Q1。
- **LVDS / CMOS**：芯片与 FPGA 之间的两种物理接口电气标准。LVDS 用差分对（`_p`/`_n`）传高速数据，CMOS 用单端线。AD9361 二选一。
- **ENABLE / VALID / DATA 三信号约定**：ADI 几乎所有数据转换器 IP 对外都用这三根线表达一路通道，下文 4.1 会展开。
- **AXI4-Lite 寄存器面 / `up_axi` 桥**：见 u4-l5，软件经 AXI4-Lite 读写 IP 寄存器。
- **`axi_dmac` 的 fifo_wr / m_axis 接口**：见 u5-l1，DMA 的源端（采集）用 `fifo_wr` 风格接口，目的端（回放）用 AXI-Stream `m_axis` 接口。

如果你对某一项还不熟悉，先回头读对应讲义再继续，本讲会直接使用这些术语。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [library/axi_ad9361/axi_ad9361.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_ad9361/axi_ad9361.v) | IP 顶层「瘦壳」：声明物理引脚、DMA 通道接口、AXI 接口，并例化接口子模块、RX、TX、TDD、`up_axi`。 |
| [library/axi_ad9361/axi_ad9361_rx.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_ad9361/axi_ad9361_rx.v) | 接收（ADC）数据通路：4 个 `axi_ad9361_rx_channel` + ADC 公共寄存器 + 延时控制。 |
| [library/axi_ad9361/axi_ad9361_tx.v](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_ad9361/axi_ad9361_tx.v) | 发送（DAC）数据通路：4 个 `axi_ad9361_tx_channel` + DAC 公共寄存器 + 延时控制。 |
| [library/axi_ad9361/axi_ad9361_delay.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_ad9361/axi_ad9361_delay.tcl) | 布线后延时校准/报告脚本：测量 RX 输入引脚到 IDDR 的数据路径延时，写日志。 |
| [projects/fmcomms2/common/fmcomms2_bd.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/common/fmcomms2_bd.tcl) | 评估板层块设计：把 `axi_ad9361` 与 pack/unpack、wfifo/rfifo、两个 `axi_dmac` 全部连起来。 |
| [projects/fmcomms2/zcu102/system_project.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_project.tcl) | 工程流程脚本：建工程→加文件→综合实现，**最后 source 延时脚本**。 |
| [docs/library/axi_ad9361/index.rst](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/library/axi_ad9361/index.rst) | 该 IP 的官方文档：功能描述、ENABLE/VALID/DATA 约定、寄存器基址表。 |

> 链接中的 commit `e57851ff…` 即当前 HEAD，行号均以此为基准。

## 4. 核心概念与源码讲解

### 4.1 数据转换器 IP 的「双通路」结构

#### 4.1.1 概念说明

数据转换器 IP 要同时完成两件性质完全不同的事：

1. **让软件能控制它**：采样率、通道开关、直流滤波、IQ 校正、DDS（直接数字频率合成）……这些都是「配置」，数据量小、由 CPU 经 AXI4-Lite 寄存器下发。这是**寄存器控制面**。
2. **让数据能流过它**：射频侧的高速 IQ 样本要源源不断地进/出 FPGA，软件不参与每个样本的搬运（那是 DMA 的活）。这是**数据流通面**。

ADI 的设计把这两面在 IP 内部物理地分开，`axi_ad9361.v` 顶层就是这种「双通路」最清晰的缩影：它几乎不写逻辑，只负责把三类接口（物理引脚、DMA 通道、AXI）接到对应的子模块上。

关键约定——每个通道对外只用三根线 `ENABLE` / `VALID` / `DATA`，官方文档 [docs/library/axi_ad9361/index.rst:142-212](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/library/axi_ad9361/index.rst#L142-L212) 把它说得很明白：

- **ENABLE**：纯软件位，IP 把寄存器里写的 bit 直接反射成输出，给下游 pack/unpack 用来「路由数据到当前启用的通道」。
- **VALID**：IP 发出的「本拍 DATA 上是一个有效样本」标志。注意 IP 永远跑在**接口时钟**（如 244 MHz）而非采样时钟（如 61 MHz），所以 VALID 常常是「每 4 拍拉高 1 拍」。
- **DATA**：永远按 16 位对齐。ADC 不足 16 位则符号扩展到 16 位，DAC 不足 16 位则取最高有效位——这样上下游可以「不关心 ADC/DAC 实际位宽」地复用同一套通路。

> 一句话：双通路 = 「CPU 经 AXI 写寄存器来配置（控制面）」+「样本经 ENABLE/VALID/DATA 端口连续流过（数据面）」，二者在 IP 内部各自独立、由同一套 `up_*` 寄存器逻辑缝合。

#### 4.1.2 核心流程

`axi_ad9361` 顶层的内部组成（对应官方文档 [index.rst:56-91](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/library/axi_ad9361/index.rst#L56-L91)）可以画成：

```
                       ┌─────────────── axi_ad9361 (顶层瘦壳) ───────────────┐
  物理引脚(LVDS/CMOS)  │                                                      │
  rx_clk/data/frame ──►│ Interface 子模块 (CMOS 或 LVDS，二选一)              │
  tx_clk/data/frame ◄──│   产生 l_clk/clk/rst，串并转换，拆出 adc_data[47:0]  │
                       │                                                      │
   DMA(ADC) ──►  adc_enable/valid/data_i0..q1  ◄──┐ 数据面(接收)              │
   DMA(DAC) ◄──  dac_enable/valid/data_i0..q1  ──┤ 数据面(发送)              │
                       │                          │                           │
                       │   axi_ad9361_rx  ────────┘  4×rx_channel + ADC reg   │
                       │   axi_ad9361_tx  ─────────  4×tx_channel + DAC reg   │
                       │   axi_ad9361_tdd           (可选 TDD 控制)           │
                       │                                                      │
  AXI4-Lite ──► s_axi_* ──► up_axi ──► up_wreq/up_rreq ──► rx/tx/tdd 寄存器  │ 控制面
                       └──────────────────────────────────────────────────────┘
```

读写的「合流」很关键：`up_axi` 把 AXI 事务翻译成简单的 `up_wreq/up_rreq`（见 u4-l5），然后**同一组请求被 RX、TX、TDD 三个子模块同时「听」**，每个子模块只应答属于自己的地址段，最终的 `up_wack/up_rack/up_rdata` 是三者应答的按位或——谁认领了地址，谁的应答就非零。

#### 4.1.3 源码精读

**顶层端口的三类接口**先认清。[axi_ad9361.v:76-104](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_ad9361/axi_ad9361.v#L76-L104) 是物理引脚（LVDS 差分对与 CMOS 单端并存，由参数 `CMOS_OR_LVDS_N` 选择其一）：

```verilog
// physical interface (receive-lvds)
input           rx_clk_in_p,
input   [ 5:0]  rx_data_in_p,      // 6 对 LVDS 数据线
...
// physical interface (receive-cmos)
input   [11:0]  rx_data_in,        // CMOS 12 位数据
```

[axi_ad9361.v:136-164](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_ad9361/axi_ad9361.v#L136-L164) 是 **DMA 接口**，正是上文 ENABLE/VALID/DATA 约定的具象——4 个 ADC 通道输出（`adc_*` 方向为 output，数据流出）与 4 个 DAC 通道输入（`dac_*` 方向为 input，数据流入）：

```verilog
output          adc_valid_i0,  output [15:0] adc_data_i0,  // ADC I 路，通道0
...
input           dac_dunf,      // DAC 下溢反馈给 IP
```

[axi_ad9361.v:166-188](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_ad9361/axi_ad9361.v#L166-L188) 是标准 AXI4-Lite 从接口（控制面入口）。

**双通路的「缝合点」**在 [axi_ad9361.v:309-324](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_ad9361/axi_ad9361.v#L309-L324)：`up_clk` 就是 `s_axi_aclk`，而 `up_wack/up_rack/up_rdata` 把 RX、TX、TDD 的应答按位或起来：

```verilog
assign up_clk = s_axi_aclk;
...
up_wack  <= up_wack_rx_s | up_wack_tx_s | up_wack_tdd_s;
up_rdata <= up_rdata_rx_s | up_rdata_tx_s | up_rdata_tdd_s;
```

这就是「同一组 AXI 请求广播给三个子模块、谁认领谁应答」的实现。

**数据面两个核心子模块的例化**：接收通路 [axi_ad9361.v:594-659](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_ad9361/axi_ad9361.v#L594-L659) 例化 `axi_ad9361_rx i_rx`，发送通路 [axi_ad9361.v:663-725](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_ad9361/axi_ad9361.v#L663-L725) 例化 `axi_ad9361_tx i_tx`。注意二者**共享同一对 `up_wreq/up_rreq`**（控制面广播），但各自驱动自己的 `adc_*`/`dac_*` 数据端口（数据面互不干扰）。

**控制面的总入口** [axi_ad9361.v:729-756](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_ad9361/axi_ad9361.v#L729-L756) 例化 `up_axi`，把外部 `s_axi_*` 翻译成内部 `up_*`（u4-l5 已详述）。

**RX 子模块内部**（[axi_ad9361_rx.v:190-406](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_ad9361/axi_ad9361_rx.v#L190-L406)）例化了 4 个 `axi_ad9361_rx_channel`（每个对应 I0/Q0/I1/Q1，通道 2、3 由 `DISABLE (MODE_1R1T)` 控制在单收模式时裁掉）+ `up_adc_common` 公共寄存器 + `up_delay_cntrl` 延时控制。注意 [axi_ad9361_rx.v:183-186](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_ad9361/axi_ad9361_rx.v#L183-L186) 同样把 6 个子应答按位或回顶层。

**TX 子模块内部**（[axi_ad9361_tx.v:222-425](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_ad9361/axi_ad9361_tx.v#L222-L425)）结构对称：4 个 `axi_ad9361_tx_channel`（内含 DDS/pattern/PRBS 数据发生器、IQ 校正）+ `up_dac_common` + `up_delay_cntrl`。

> 控制面寄存器在地址空间里也是分区映射的。官方文档 [index.rst:383-413](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/library/axi_ad9361/index.rst#L383-L413) 给出基址：RX COMMON/CHANNEL 在 `0x0000`，TX COMMON/CHANNEL 在 `0x1000`（HDL reg），TDD 在 `0x2000`。子模块靠 `BASE_ADDRESS` 参数认领自己的地址段——这正是「按位或」能正确工作的前提。

#### 4.1.4 代码实践

**实践目标**：在源码里亲手验证「同一组 AXI 请求被多个子模块广播接收」这一机制。

**操作步骤**：

1. 打开 `library/axi_ad9361/axi_ad9361.v`，定位 [L729 的 `up_axi` 例化](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_ad9361/axi_ad9361.v#L729-L756)，确认它输出的 `up_wreq/up_rreq` 是 wire。
2. 跟着 `up_wreq_s` 这个网络名，分别在 `i_rx`（L645）、`i_tx`（L718）、`i_tdd`（L555）三处例化端口里找到 `.up_wreq (up_wreq_s)`——你会看到**三个子模块接在同一根线上**。
3. 再看顶层 [L320-322](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_ad9361/axi_ad9361.v#L320-L322) 的应答汇总。

**需要观察的现象**：写请求是一对多（一个 `up_wreq_s` 驱动三个子模块），应答是多对一（三个 `up_wack_*_s` 按位或回一个 `up_wack`）。

**预期结果**：你能用一句话解释「为什么这种总线不会冲突」——因为地址译码后同一时刻只有一个子模块的 `up_rdata` 非零，其余输出全 0，按位或天然无冲突。这是 ADI 全仓 IP 控制面复用的通用范式。

#### 4.1.5 小练习与答案

**练习 1**：`axi_ad9361` 同时声明了 LVDS 与 CMOS 两套物理端口，为什么不会综合出两套硬件？

**参考答案**：物理接口由参数 `CMOS_OR_LVDS_N` 在综合期二选一。顶层用 `generate if` 分别在 [L328 的 CMOS 分支](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_ad9361/axi_ad9361.v#L328-L392) 与 [L394 的 LVDS 分支](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_ad9361/axi_ad9361.v#L394-L464) 例化 `axi_ad9361_cmos_if` 或 `axi_ad9361_lvds_if`，未选中分支的端口在 `generate` 里被常量驱动（如 L331-336 把 CMOS 不用的 LVDS 输出接死）。综合器会裁掉未命中的分支，最终只有一套接口硬件。

**练习 2**：为什么 ADC 数据端口方向是 `output`，而 DAC 数据端口方向是 `input`？

**参考答案**：从 IP 的视角看，ADC（接收）方向是「样本从射频芯片进来、经 IP 处理后输出给下游 DMA」，所以 `adc_data_*` 是 IP 的输出；DAC（发送）方向是「样本从上游 DMA 进来、经 IP 处理后送给射频芯片」，所以 `dac_data_*` 是 IP 的输入。方向始终以「站在 IP 顶层看」为基准。

---

### 4.2 capture / playback 的级联：pack / unpack / fifo / dmac

#### 4.2.1 概念说明

`axi_ad9361` 的数据面只给出 4 路 ×16 位的 ENABLE/VALID/DATA，但要真正把样本搬进 PS DDR（采集）或从 DDR 搬出（回放），还差几座「桥」：

- **跨时钟域 FIFO**：IP 跑在 `l_clk`（接口时钟），而 DMA 跑在 `sys_cpu_clk`（处理器时钟），必须做 CDC（clock domain crossing）。
- **通道打包/解包**：4 路 16 位样本要拼成一条宽总线（如 64 位）一次性喂给/取自 DMA，减少 DMA 的 beat 数；反向则要把宽总线拆回 4 路。这就是 u5-l3 提到的 `util_cpack2` / `util_upack2`。
- **采样时钟分频**：AD9361 的接口时钟可能是采样时钟的 2 倍或 4 倍，需要按 `r1_mode` 自适应分频，给数据通路一个稳定的「数据时钟」。
- **DMA 引擎**：`axi_dmac` 本身（u5-l1）。

这些「桥」全部在评估板层块设计 `fmcomms2_bd.tcl` 里用 `adi_board.tcl` 的连线原语（u3-l4）拼装。理解这条级联，是理解任何 ADI 数据转换器参考设计的钥匙——`axi_ad9361` 换成 `axi_ad9364`、`axi_adrv9009`，级联形状几乎一样。

> 一句话：采集链 = `axi_ad9361(ADC)` → `util_wfifo`(CDC) → `util_cpack2`(打包) → `axi_dmac`(写 DDR)；回放链 = `axi_dmac`(读 DDR) → `util_upack2`(解包) → `util_rfifo`(CDC) → `axi_ad9361(DAC)`。

#### 4.2.2 核心流程

**ADC 采集链（capture）** 数据流向（RF → DDR）：

```
AD9361 LVDS ──► axi_ad9361 i_dev_if (串并转换)
                    │ adc_enable/valid/data_i0,q0,i1,q1  (l_clk 域)
                    ▼
              util_wfifo  util_ad9361_adc_fifo   ← 跨时钟域：din_clk=l_clk → dout_clk=divclk
                    │ dout_enable/valid/data_0..3   (divclk 域)
                    ▼
              util_cpack2 util_ad9361_adc_pack   ← 4×16 打包成 64 位
                    │ packed_fifo_wr (single AXIS-like bundle)
                    ▼
              axi_dmac axi_ad9361_adc_dma        ← DMA_TYPE_SRC=2(fifo_wr) → DEST=0(AXI-MM)
                    │ m_dest_axi
                    ▼
              PS DDR (经 ad_mem_hp*_interconnect)
```

**DAC 回放链（playback）** 数据流向（DDR → RF），方向相反：

```
PS DDR ──► axi_dmac axi_ad9361_dac_dma   (SRC=0 AXI-MM → DEST=1 AXI-Stream)
              │ m_axis
              ▼
        util_upack2 util_ad9361_dac_upack   ← 64 位解包成 4×16
              │ fifo_rd_data_0..3   (divclk 域)
              ▼
        util_rfifo axi_ad9361_dac_fifo      ← 跨时钟域：din_clk=divclk → dout_clk=l_clk
              │ dout_data_0..3
              ▼
        axi_ad9361 i_tx → AD9361 LVDS
```

两条链的「时钟骨架」是同一个 `util_ad9361_divclk`：它根据 `adc_r1_mode`/`dac_r1_mode`（由 `ilconcat`+`ilreduced_logic` 拼成 2 位选择）分频出 `clk_out`，作为 wfifo/cpack/upack/rfifo/dmac 的统一数据时钟。这保证除了 wfifo/rfifo 的「靠近 IP 那一侧」用 `l_clk`，其余数据通路全部跑在同一个 `divclk` 上，省去额外的 CDC。

#### 4.2.3 源码精读

打开 [projects/fmcomms2/common/fmcomms2_bd.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/common/fmcomms2_bd.tcl)，按数据流顺序逐段读。

**① 实例化 IP 并接物理引脚** [L31-56](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/common/fmcomms2_bd.tcl#L31-L56)：

```tcl
ad_ip_instance axi_ad9361 axi_ad9361
ad_ip_parameter axi_ad9361 CONFIG.ID 0
...
ad_connect rx_data_in_p axi_ad9361/rx_data_in_p   ;# 差分数据引脚直连 IP
```

注意 [L39-40](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/common/fmcomms2_bd.tcl#L39-L40) 把 IP 自己的 `l_clk` 反接回自己的 `clk`（IP 要求二者同源），并把 `$sys_iodelay_clk` 接到 `delay_clk`——这是延时校准的参考时钟。

**② 分频时钟骨架** [L70-90](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/common/fmcomms2_bd.tcl#L70-L90)：用 `ilconcat`+`ilreduced_logic` 把 `adc_r1_mode`/`dac_r1_mode` 拼成 2 位选择信号喂给 `util_clkdiv`，注释（L70-72）说明「2r2t 模式下接口跑 4 倍速、1r1t 跑 2 倍速」。

**③ ADC 采集链三段**：

- **wfifo（CDC）** [L94-115](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/common/fmcomms2_bd.tcl#L94-L115)：`util_wfifo`，4 通道、16 位宽，`din_clk` 接 `axi_ad9361/l_clk`，`dout_clk` 接 `divclk/clk_out`。注意 [L103-114](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/common/fmcomms2_bd.tcl#L103-L114) 把 IP 的 `adc_enable/valid/data_i0/q0/i1/q1` 逐一映射到 `din_enable_0..3`，溢出反馈 `din_ovf → adc_dovf`。
- **cpack2（打包）** [L119-133](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/common/fmcomms2_bd.tcl#L119-L133)：`util_cpack2`，4 通道 ×16 位，把 wfifo 的 4 路输出打包成一个 `packed_fifo_wr` 接口；用一个 `for` 循环把 `dout_data_$i` 接到 `fifo_wr_data_$i`。
- **dmac（写 DDR）** [L137-154](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/common/fmcomms2_bd.tcl#L137-L154)：`axi_dmac`，关键参数 `DMA_TYPE_SRC 2`（fifo_wr 风格源）、`DMA_TYPE_DEST 0`（AXI-MM 目的，即写内存）、`SYNC_TRANSFER_START 1`（用 cpack 的 `packed_sync` 触发传输起拍）、`DMA_SG_TRANSFER 1`（scatter-gather）。[L151](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/common/fmcomms2_bd.tcl#L151) 把 cpack 的 `packed_fifo_wr` 整体接到 dmac 的 `fifo_wr`。

**④ DAC 回放链三段**（方向相反）：

- **dmac（读 DDR）** [L201-217](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/common/fmcomms2_bd.tcl#L201-L217)：`DMA_TYPE_SRC 0`（AXI-MM 源，读内存）、`DMA_TYPE_DEST 1`（AXI-Stream 目的）、`CYCLIC 1`（**循环**模式，回放波形反复播）。[L214](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/common/fmcomms2_bd.tcl#L214) 把 dmac 的 `m_axis` 接到 upack 的 `s_axis`。
- **upack2（解包）** [L182-197](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/common/fmcomms2_bd.tcl#L182-L197)：64 位解包成 4×16，`fifo_rd_data_$i` 接到 rfifo 的 `din_data_$i`。
- **rfifo（CDC）** [L158-178](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/common/fmcomms2_bd.tcl#L158-L178)：`util_rfifo`，`dout_clk` 接 `l_clk`（回到 IP 域），输出 `dout_data_0..3` 接到 `axi_ad9361` 的 `dac_data_i0/q0/i1/q1`，下溢反馈 `dout_unf → dac_dunf`。

**⑤ 控制面与中断** [L221-244](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/common/fmcomms2_bd.tcl#L221-L244)：

- [L221-223](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/common/fmcomms2_bd.tcl#L221-L223) `ad_cpu_interconnect` 把三个 IP（`axi_ad9361`、ADC dmac、DAC dmac）挂到 CPU 地址空间，注意 `axi_ad9361` 基址 `0x79020000`。
- [L225-239](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/common/fmcomms2_bd.tcl#L225-L239) `ad_mem_hp*_interconnect` 把两个 DMA 的 AXI-MM 主口连到 PS 的 HP/HPC 端口，并按 `$CACHE_COHERENCY` 在 Zynq-7000（HP1/HP2）与 ZynqMP（HPC0/HPC1）之间自适应——这正是 u3-l4 讲的「同一份评估板脚本跨载板复用」。
- [L243-244](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/common/fmcomms2_bd.tcl#L243-L244) `ad_cpu_interrupt` 把两个 DMA 的中断分别接到 `ps-13`/`ps-12`。

> 注意 ADC 与 DAC 用了**不同的 fifo**：采集侧用 `util_wfifo`（写 FIFO，写入侧是 IP 的连续流），回放侧用 `util_rfifo`（读 FIFO，读出侧是 IP 的连续流）。二者都是跨时钟域 FIFO，但「满/溢」与「空/欠」的方向语义不同，所以用两个不同 IP。u5-l3 对此有详细对比。

#### 4.2.4 代码实践

**实践目标**：在 `fmcomms2_bd.tcl` 里完整追踪一条 ADC 采集链，画出从射频采样到 PS DDR 的数据流向，并标注每一段的时钟域。

**操作步骤**：

1. 在 [fmcomms2_bd.tcl](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/common/fmcomms2_bd.tcl) 中定位 ADC 链的三段（L94 wfifo、L119 cpack、L137 dmac）。
2. 对每一段，记录：**输入端口名 → 来自哪个 IP 的哪个端口 → 时钟域**。例如 wfifo 的 `din_clk` 来自 `axi_ad9361/l_clk`（IP 域），`dout_clk` 来自 `util_ad9361_divclk/clk_out`（divclk 域）。
3. 找到 [L151](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/common/fmcomms2_bd.tcl#L151) 的 `ad_connect util_ad9361_adc_pack/packed_fifo_wr axi_ad9361_adc_dma/fifo_wr`，确认 cpack 与 dmac 之间是**一个打包接口整体对接**（不是逐通道）。
4. 找到 [L227](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/common/fmcomms2_bd.tcl#L227) 的 `ad_mem_hpc0_interconnect ... axi_ad9361_adc_dma/m_dest_axi`，确认 dmac 的内存主口连到 PS HPC0。

**需要观察的现象**：数据每经过一段，要么时钟域变了（wfifo、rfifo），要么位宽变了（cpack 把 4×16 变成 64 位打包，upack 反向），要么协议变了（cpack 的 `packed_fifo_wr` 到 dmac 内部转成 AXI-MM 突发）。

**预期结果**：你能画出这样一条带标注的链（时钟域用括号注明）：

```
AD9361 ─► axi_ad9361(ADC) [l_clk]
       ─► util_wfifo          [l_clk → divclk]
       ─► util_cpack2 (4×16→64)[divclk]
       ─► axi_dmac (fifo_wr→AXI-MM)[divclk, SYNC_TRANSFER_START]
       ─► PS DDR (HPC0)        [sys_cpu_clk]
```

并指出：跨时钟域只发生在 wfifo（采集）/ rfifo（回放）这两处；除此之外整条数据通路都跑在 `divclk` 上，这是该设计简化 CDC 的关键。

#### 4.2.5 小练习与答案

**练习 1**：ADC 的 dmac 配置了 `SYNC_TRANSFER_START 1` 并连接了 `packed_sync`，DAC 的 dmac 却没有。为什么？

**参考答案**：采集是**异步**的——射频信号何时到来不可预测，DMA 需要一个由数据通路产生的「同步脉冲」来对齐一次传输的起点（避免读到半截样本），所以 ADC 链用 cpack 的 `packed_sync` 触发起拍（见 [L141 与 L152](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/common/fmcomms2_bd.tcl#L141-L152)）。回放则是**自主**的——DMA 用 `CYCLIC 1` 循环把波形反复喂给 DAC，起拍由软件/DMA 内部控制，不需要外部同步。

**练习 2**：为什么采集链用 `util_wfifo`、回放链用 `util_rfifo`，而不是都用同一种？

**参考答案**：二者方向相反。采集时 IP 是「生产者」连续写、DMA 是「消费者」，FIFO 关注「溢出（overflow）」——`util_wfifo` 的 `din_ovf` 接到 IP 的 `adc_dovf`。回放时 DMA 是「生产者」、IP 是「消费者」连续读，FIFO 关注「欠溢（underflow）」——`util_rfifo` 的 `dout_unf` 接到 IP 的 `dac_dunf`。两个 IP 分别针对各自方向的满/空语义做了状态反馈，所以不能互换。

---

### 4.3 delay 校准脚本：`axi_ad9361_delay.tcl`

#### 4.3.1 概念说明

AD9361 与 FPGA 之间是高速 LVDS 接口，数据线与采样时钟之间的**建立/保持时间余量**非常紧张。为了在硬件上可靠采样，IP 内部有可编程的 **IO 延时（IODELAY）** 资源——软件可以逐根线调延时抽头（tap）。这套机制分两部分：

- **运行时**：由 `up_delay_cntrl`（在 [axi_ad9361_rx.v:410](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_ad9361/axi_ad9361_rx.v#L410-L432) 与 [axi_ad9361_tx.v:429](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_ad9361/axi_ad9361_tx.v#L429-L451) 例化）把寄存器里的延时值（`up_dld`/`up_dwdata`）写进底层 IODELAY 原语，软件据此逐线校准。
- **构建时**：`axi_ad9361_delay.tcl` 是一个**报告脚本**，在 Vivado 布线（route）之后运行，测量每根 RX 数据/时钟/帧引脚到内部 IDDR 采样触发器的**实际数据路径延时**，写进日志 `axi_ad9361_delay.log`。这份日志是硬件调试时判断「哪根线需要加多少延时」的依据。

> ⚠️ 注意区分：`*_delay.tcl` 不是「自动算出并写入延时值」的闭环校准，它只**报告延时**。真正的逐线抽头值由软件（no-OS/Linux 驱动）在运行时根据这份日志和实测眼图来决定并写入 `up_delay_cntrl` 寄存器。把它叫「校准脚本」是指它服务于校准流程，而非自动完成校准。

#### 4.3.2 核心流程

`axi_ad9361_delay.tcl` 的执行逻辑（在 Vivado Tcl shell 里跑）：

1. 打开日志文件 `axi_ad9361_delay.log`（覆盖写）。
2. 用 `get_ports` 取出所有名字匹配 `rx_*_in*` 的引脚（即 RX 的时钟、帧、数据输入引脚）。
3. 用 `get_pins -hierarchical` 取出 IDDR 原语的 C/D 输出管脚作为终点。
4. 用 `report_timing -from $m_ios -to $m_ddr_ios -max_paths 100` 拿到最多 100 条路径的时序报告字符串。
5. 用三段 `regexp` 循环分别从报告里抽出 `Source:`（起点）、`Destination:`（终点）、`Data Path Delay:`（延时值）三个列表。
6. 逐条把「起点 终点 延时」打印到终端并写入日志，最后把完整报告原文也附在日志末尾。

这个脚本**何时运行**是关键——它依赖布线完成后的网表与时序信息，所以必须在 `adi_project_run`（跑完综合+实现+布线）**之后**才能跑。在工程脚本里的体现见下文。

#### 4.3.3 源码精读

**脚本本体** [library/axi_ad9361/axi_ad9361_delay.tcl:6-10](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_ad9361/axi_ad9361_delay.tcl#L6-L10)：

```tcl
# report delays
set m_file [open "axi_ad9361_delay.log" w]
set m_ios [get_ports -filter {NAME =~ rx_*_in*}]
set m_ddr_ios [get_pins -hierarchical -filter {NAME =~ *i_rx_data_iddr/C || NAME =~ *i_rx_data_iddr/D}]
set m_info [report_timing -no_header -return_string -from $m_ios -to $m_ddr_ios -max_paths 100]
```

- `rx_*_in*` 同时匹配 `rx_clk_in_*`、`rx_frame_in_*`、`rx_data_in_*` 三类差分引脚。
- `*i_rx_data_iddr/C` 指向 LVDS 接口里 IDDR 触发器的输出——这正是采样点，所以「引脚→IDDR」的延时就是我们要量的数据路径。

抽出三列的循环（[L22-42](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_ad9361/axi_ad9361_delay.tcl#L22-L42)，以延时为例）：

```tcl
while {[regexp {\s+Data\s+Path\s+Delay:\s+(.*?)\s+(.*)} $m_string m1 m_value m_string] == 1} {
  lappend m_delays $m_value
}
...
puts $m_file "$m_source $m_destination $m_delay"
```

**何时被 source**：打开 [projects/fmcomms2/zcu102/system_project.tcl:23-24](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_project.tcl#L23-L24)：

```tcl
adi_project_run fmcomms2_${BOARD_NAME}
source $ad_hdl_dir/library/axi_ad9361/axi_ad9361_delay.tcl
```

注意顺序——`adi_project_run`（u3-l3 讲过它会驱动 `synth_1`→`impl_1`→`write_bitstream`）**先完成布线**，返回之后才 `source` 延时脚本。这是因为脚本依赖布线后的实际延时，必须排在最后。

**延时寄存器侧的承接**：[axi_ad9361_rx.v:410-415](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_ad9361/axi_ad9361_rx.v#L410-L415) 例化 `up_delay_cntrl`，`DATA_WIDTH 13`、`BASE_ADDRESS 6'h02`；TX 侧 [axi_ad9361_tx.v:429-434](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_ad9361/axi_ad9361_tx.v#L429-L434) 则是 `DATA_WIDTH 16`、`BASE_ADDRESS 6'h12`。这两组参数说明：RX 有 13 根延时线（时钟+帧+6 数据等），TX 有 16 根（多了 enable/txnrx 等），且各自占用 `up_*` 地址空间里 `0x02` 与 `0x12` 起的寄存器段——软件写这些寄存器就是写每根线的延时抽头。

> 工程脚本里还有一行容易混洧的 `set ADI_POST_ROUTE_SCRIPT .../auto_timing_fix_xilinx.tcl`（[system_project.tcl:9](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_project.tcl#L9)）。那是 u8-l3 的「时序自动修复」脚本，由 Vivado 在布线后**自动**调用；与本讲的 `axi_ad9361_delay.tcl` 不同——后者是手动 `source`、只报告不修复。两者都排在布线之后，但机制与目的不同。

#### 4.3.4 代码实践

**实践目标**：理清延时校准链路上「报告（构建时）」与「施加（运行时）」两段的关系。

**操作步骤**：

1. 打开 `system_project.tcl`，确认 [L24](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_project.tcl#L24) 的 `source .../axi_ad9361_delay.tcl` 紧跟在 [L23](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_project.tcl#L23) 的 `adi_project_run` 之后。
2. 打开 `axi_ad9361_delay.tcl`，在 [L8](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_ad9361/axi_ad9361_delay.tcl#L8) 确认它量的是 `rx_*_in*` 引脚；在 [L10](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_ad9361/axi_ad9361_delay.tcl#L10) 确认终点是 IDDR 的 C/D。
3. 打开 `axi_ad9361_rx.v`，在 [L410-415](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_ad9361/axi_ad9361_rx.v#L410-L415) 找到 `up_delay_cntrl`，记录它的 `BASE_ADDRESS` 与 `DATA_WIDTH`。
4. 设想：构建结束后，工程目录下会多出一个 `axi_ad9361_delay.log`。

**需要观察的现象**：脚本的输入（`get_ports`/`get_pins`/`report_timing`）只有在「设计已实现（implemented）」之后才有效；若在综合前 source 它会报找不到 pin。

**预期结果**：你能用两句话讲清这条链路——「构建末尾 `axi_ad9361_delay.tcl` 报告每根 RX 线的布线延时到日志；运行时软件读日志决定每根线的抽头值，经 AXI 写到 `up_delay_cntrl`（基址 `0x02`）寄存器，硬件把抽头施加到 IODELAY 原语。」如果手头没有 Vivado 与硬件，这一步标注为「待本地验证」——可在真实跑过 `make` 的工程目录里查看 `axi_ad9361_delay.log` 的内容。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `system_project.tcl` 里 [L24 的 source](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_project.tcl#L24) 移到 `adi_project_run`（L23）**之前**，会发生什么？

**参考答案**：脚本会失败或输出为空。`report_timing -from rx_*_in*` 依赖实现（impl）阶段产生的网表与布线延时；在 `adi_project_run` 之前设计还没综合、更没布线，`get_pins -hierarchical *i_rx_data_iddr*` 找不到对象，`report_timing` 拿不到任何路径，三列列表为空，最终日志里只有表头而没有数据行。

**练习 2**：`up_delay_cntrl` 在 RX 与 TX 两侧的 `BASE_ADDRESS` 分别是 `0x02` 与 `0x12`，为什么不同？

**参考答案**：RX 与 TX 各自的 `up_delay_cntrl` 只在**自己的子模块**（`axi_ad9361_rx` / `axi_ad9361_tx`）内部译码 `up_waddr`。`0x02` 与 `0x12` 是相对于各自子模块的局部地址段，互不冲突——因为整个 IP 的 AXI 地址空间又把 RX 区放在 `0x0000`、TX 区放在 `0x1000`（见 [index.rst:390-405](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/library/axi_ad9361/index.rst#L390-L405)），所以绝对地址不会重叠。两侧 `DATA_WIDTH`（13 vs 16）也不同，反映 TX 多出 enable/txnrx 等需要校准的线。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「端到端追踪」小任务：

**任务**：你是新接手 fmcomms2 参考设计的工程师，需要在代码评审会上讲清「一段射频信号如何变成 PS 内存里的样本」。请基于本讲三个最小模块，产出一份**带行号引用的追踪报告**，包含：

1. **控制面**：软件写哪个 AXI 地址（基址 + 子模块段）来使能 RX 通道、复位 IP？至少引用 [index.rst 的寄存器基址表](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/library/axi_ad9361/index.rst#L383-L413) 与 [adi_regmap_adc.txt 的 RSTN 寄存器](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/docs/regmap/adi_regmap_adc.txt)。
2. **数据面（采集）**：从 `rx_data_in_p` 引脚开始，依次列出经过的 5 个 IP 实例（含 `axi_ad9361` 自身、wfifo、cpack、dmac、PS DDR），每一步标注时钟域与位宽变化，引用 `fmcomms2_bd.tcl` 的对应行号。
3. **校准面**：说明构建末尾 `axi_ad9361_delay.tcl`（[system_project.tcl:24](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/projects/fmcomms2/zcu102/system_project.tcl#L24)）产出的日志如何对应到运行时写入 [axi_ad9361_rx.v:410 的 `up_delay_cntrl`](https://github.com/analogdevicesinc/hdl/blob/e57851ffcea3d92d821a38c908c909d61db4a492/library/axi_ad9361/axi_ad9361_rx.v#L410-L432)。

**完成标准**：报告里的每一步都能点开一个永久链接、指向真实代码行；三面（控制/数据/校准）之间能说清「谁在什么时候、写什么、影响哪一段数据流」。

> 提示：可参照本讲 4.1.2 的总框图、4.2.2 的两条链路图与 4.3.2 的脚本流程，把它们合并成一张完整的「三面视图」。

## 6. 本讲小结

- **双通路**：数据转换器 IP = 「AXI4-Lite 寄存器控制面」+「ENABLE/VALID/DATA 数据流通面」，在 `axi_ad9361.v` 顶层里由 `up_axi` 把同一组请求广播给 RX/TX/TDD，应答按位或汇合。
- **统一通道约定**：每路样本恒为 16 位（ADC 符号扩展、DAC 取高位），让上下游 pack/unpack、DMA 可与具体 ADC/DAC 位宽解耦。
- **采集链级联**：`axi_ad9361(ADC)` → `util_wfifo`(CDC, l_clk→divclk) → `util_cpack2`(4×16→64) → `axi_dmac`(fifo_wr→AXI-MM) → PS DDR；起拍由 `SYNC_TRANSFER_START` 同步。
- **回放链级联**：方向相反，PS DDR → `axi_dmac`(AXI-MM→m_axis, CYCLIC) → `util_upack2`(64→4×16) → `util_rfifo`(CDC) → `axi_ad9361(DAC)`；采集用 wfifo 关注溢出，回放用 rfifo 关注欠溢。
- **时钟骨架**：`util_ad9361_divclk` 按 `r1_mode` 分频出统一数据时钟，把跨时钟域只压缩到 wfifo/rfifo 两处。
- **延时校准**：`axi_ad9361_delay.tcl` 在 `adi_project_run` 之后 `source`，报告每根 RX 线的布线延时到日志；运行时软件据此写 `up_delay_cntrl`（RX 基址 `0x02`、TX `0x12`）寄存器施加抽头。报告与施加是两段、不自动闭环。

## 7. 下一步学习建议

- **横向迁移**：用本讲建立的「采集/回放级联」模板，去读 `library/axi_adrv9001` 或 JESD204 类数据转换器（`library/jesd204/axi_jesd204_rx`），对比它们的 pack/unpack、DMA 连线方式有何异同——这是 u6-l1（JESD204 框架）的切入点。
- **纵向深入 util IP**：本讲提到的 `util_wfifo`/`util_rfifo`/`util_cpack2`/`util_upack2` 是 u5-l3（util 工具 IP）的主角，建议接着学，搞清它们的满/空、通道使能路由内部实现。
- **延时闭环**：若想了解软件如何**自动**完成延时校准（而非本讲的手动报告），可阅读 no-OS 仓库 `drivers/rf-transceiver/ad9361` 里的延时校准例程，对照本讲的 `up_delay_cntrl` 寄存器语义。
- **时序与收发器**：本讲刻意回避了 LVDS 物理层（IDDR/BUFG/MMCM）与时序约束细节，那是 u8-l3（收发器、时钟与时序约束）的主题，可在学完 util IP 后推进。
