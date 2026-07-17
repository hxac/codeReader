# CCA、RSSI 与 AD9361 SPI 控制

## 1. 本讲目标

本讲聚焦 `xpu` 低层 MAC 中的「感知与射频控制」三件事：**怎么测能量（RSSI）**、**怎么判定信道是否空闲（CCA）**、**怎么用 SPI 实时拨动 AD9361 射频前端（开/关 TX 本振）**。

学完后你应该能够：

- 沿着 `ddc_i/ddc_q → mv_avg → iq_abs_avg → iq_rssi_to_db → rssi_half_db` 这条链，说清每一级在做什么、为什么这样做。
- 读懂 `cca.v` 如何把「能量低于门限 + 没在解包 + 没在发包」综合成单比特信号 `ch_idle`，并理解解码后等待窗口的作用。
- 看懂 `spi.v` 这个 3 状态机如何在 `tx_chain_on` 跳变时，抢在 CPU 之前向 AD9361 发送预编译好的 24 位 SPI 命令字。
- 知道哪些软件寄存器（`slv_reg6/7/8/13`）驱动这些模块、状态从哪里读回（`slv_reg57`）。

本讲承接 [u5-l1](u5-l1-xpu-overview.md) 的「xpu 只装配不写算法」结论：本讲的全部算法都下沉在 `xpu` 的子模块里，`xpu.v` 只负责把它们连起来、把软件寄存器接到端口上。

## 2. 前置知识

- **I/Q 基带样点**：AD9361 把射频下变频到基带，输出两路正交样点 I 与 Q（本仓库里是 16 位有符号整数 `ddc_i/ddc_q`）。一个样点的瞬时功率正比于 \(I^2+Q^2\)，幅度约等于 \(\sqrt{I^2+Q^2}\)。
- **dB（分贝）与 RSSI**：Wi-Fi 信号能量跨几个数量级，工程上用对数刻度 dB 表示。RSSI（Received Signal Strength Indicator）就是对接收能量的一种量化。本仓库用「0.5 dB 步进」的定点整数 `rssi_half_db` 来表示，即真实 dB 值 ×2，从而用整数表达 0.5 dB 精度。
- **能量检测（Energy Detection, ED）与 CCA**：802.11 的 CCA（Clear Channel Assessment，信道空闲评估）有两种思路——物理载波监听看「有没有 802.11 前导」，能量检测看「空中有没有足够强的任意能量」。本仓库的 `cca.v` 主要用能量检测：能量低于门限且没在收发包，才算信道空闲。
- **AGC 与增益回读**：AD9361 内部有自动增益控制（AGC），`gpio_status` 这 8 位是 AD9361 回读的状态字，其中低 7 位携带当前接收增益。算「空中真实信号强度」时要从测量值里扣掉这部分增益。
- **滑动平均（moving average）**：用 FIFO 保存最近 N 个样点，维护一个滚动累加和，每来一个新样点「加新减旧」，再除以 N。这是本讲里去直流、平滑能量的核心原语。
- **SPI**：AD9361 提供一条 SPI 串行总线用于读写其内部寄存器。CPU（PS）和 FPGA 都想控制它，所以需要一个仲裁/复用机制。
- **`COUNT_SCALE` 与软件计数器标尺**：回顾 [u2-l4](u2-l4-board-config-clock.md)，`COUNT_SCALE = NUM_CLK_PER_US / 10`，它把「基带时钟周期数」换算成「软件假定的 10 MHz 计数器刻度」，本讲 CCA 的等待窗口会用到它。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [ip/xpu/src/dc_rm.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/dc_rm.v) | 去直流：用长滑动平均估出 I/Q 的直流偏置并减去 |
| [ip/xpu/src/mv_avg.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/mv_avg.v) | 单通道滑动平均原语（FIFO 实现的加新减旧） |
| [ip/xpu/src/mv_avg_dual_ch.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/mv_avg_dual_ch.v) | 双通道滑动平均（I、Q 共享一个 FIFO、同进同出） |
| [ip/xpu/src/iq_abs_avg.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/iq_abs_avg.v) | 把去直流后的 I/Q 取绝对值再平均，得到平滑幅度估计 `iq_rssi` |
| [ip/xpu/src/iq_rssi_to_db.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/iq_rssi_to_db.v) | 用分段二次多项式把线性的 `iq_rssi` 近似换算成 0.5 dB 步进的 `iq_rssi_half_db` |
| [ip/xpu/src/rssi.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/rssi.v) | RSSI 顶层：串起上面三级，扣除 AD9361 增益，输出校准后的 `rssi_half_db` |
| [ip/xpu/src/cca.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/cca.v) | 信道空闲评估：综合能量门限、解码状态、收发状态给出 `ch_idle` |
| [ip/xpu/src/spi.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/spi.v) | AD9361 SPI 主控：在 `tx_chain_on` 跳变时发送预编译命令字，并和 CPU 仲裁总线 |
| [boards/ip_repo_gen.tcl](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/ip_repo_gen.tcl) | 构建期生成 `spi_command.v`（即 `SPI_HIGH/SPI_LOW` 两条命令字） |
| [ip/xpu/src/xpu.v](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v) | 装配：例化 `rssi_i/cca_i/spi_module_i`，接寄存器与对外端口 |

> 说明：`iq_abs_avg.v` 里还保留着单通道 `mv_avg` 的注释化例化，**实际生效的是双通道 `mv_avg_dual_ch`**；`mv_avg.v` 作为通用原语仍由仓库提供，本讲会先讲它再讲双通道版本。

## 4. 核心概念与源码讲解

### 4.1 RSSI 计算链：从 I/Q 样点到 0.5 dB 步进的能量值

#### 4.1.1 概念说明

RSSI 要回答的问题是：**「此刻空中的信号有多强？」** 但 ADC 给我们的只是带增益的数字 I/Q。要得到一个稳定、可比、接近「真实空中功率」的度量，需要四步处理：

1. **去直流（DC removal）**：I/Q 链路里常混入直流偏置（来自器件失调），它是一个恒定分量，会污染能量估计。因为信号本身是零均值的交流量，**对它做一个很长的滑动平均，得到的就是直流偏置**，再用原信号减掉即可。
2. **取幅度的滑动平均**：对去直流后的 I、Q 分别取绝对值（得到幅度），再做一段滑动平均，消除瞬时抖动，得到平滑的幅度估计。
3. **线性 → 对数（dB）换算**：把幅度转成 dB 需要做对数，FPGA 里不便直接算 `log`，于是用**分段二次多项式逼近**，离线把系数算好固化进 RTL。
4. **增益校准**：测得的 dB 里含 AD9361 的接收增益，扣掉它（并加一个软件偏置）才是接近天线口的强度。

#### 4.1.2 核心流程

```
ddc_i, ddc_q (16bit 有符号, 来自 DDC)
        │
        ▼
   dc_rm (128 点滑动平均 → 估直流 → 相减)        ── 去直流
        │  i_dc_rm, q_dc_rm
        ▼
   |·| 取绝对值  →  i_abs, q_abs
        │
        ▼
   mv_avg_dual_ch (32 点滑动平均)                  ── 平滑幅度
        │  i_abs_mv_avg, q_abs_mv_avg
        ▼
   iq_rssi = (i_abs_mv_avg + q_abs_mv_avg) >> 1    ── 合并为单路幅度
        │
        ▼
   iq_rssi_to_db (分段二次多项式, 4 拍 FSM)         ── 线性→0.5dB
        │  iq_rssi_half_db  (9bit 有符号, 步进 0.5dB)
        ▼
   rssi_half_db = offset + iq_rssi_half_db         ── 扣 AD9361 增益 + 校准
                 - (gpio_status_delay[6:0] << 1)
```

关键数量关系（以 20 MHz 基带采样率为例）：

- 去直流窗口 \(N_{dc}=128\) 个样点 \(\approx 6.4\,\mu s\)；幅度平滑窗口 \(N_{avg}=32\) 个样点 \(\approx 1.6\,\mu s\)。
- 幅度合成：\(iq\_rssi \approx \tfrac{1}{2}(\overline{|I|}+\overline{|Q|})\)，是线性幅度量。
- 多项式逼近：对输入 \(x=iq\_rssi\)，按其大小分 5 段，每段用
  \[
  y = \left\lfloor \frac{p_1 x^2 + p_2 x + p_3}{2^{n}} \right\rfloor
  \]
  得到 \(y=iq\_rssi\_half\_db\)（0.5 dB 步进）。系数 \((p_1,p_2,p_3,n)\) 由离线脚本（注释提到的 `test_iq_rssi_interp.m` / `calc_phy_header`）计算。

#### 4.1.3 源码精读

**(a) 滑动平均原语 `mv_avg`** —— 理解「加新减旧」。

[mv_avg.v:38](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/mv_avg.v#L38) 用滚动累加和的高位作平均输出（右移 `LOG2_AVG_LEN` 位即除以 \(N\)）；[mv_avg.v:98-L100](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/mv_avg.v#L98-L100) 是核心：每个写入完成脉冲里 `running_total <= running_total + 新样点 - 最旧样点`，其中「最旧样点」由背后的 `xpm_fifo_sync`（深度 = `1<<LOG2_AVG_LEN`）回读（[mv_avg.v:40-L82](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/mv_avg.v#L40-L82)）。`rd_en_start`（[mv_avg.v:96](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/mv_avg.v#L96)）保证 FIFO 攒满 \(N\) 个样点后才开始「减旧」，即**前 \(N\) 个样点用于预热，之后才输出真正的滑动平均**。`mv_avg_dual_ch.v` 把 I、Q 拼成 `{q,i}` 共享同一个 FIFO、同一套控制（[mv_avg_dual_ch.v:47-L48](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/mv_avg_dual_ch.v#L47-L48) 输出、[mv_avg_dual_ch.v:111-L113](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/mv_avg_dual_ch.v#L111-L113) 双路加新减旧），节省资源。

**(b) 去直流 `dc_rm`** —— 长滑动平均当直流估计。

[dc_rm.v:29-L30](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/dc_rm.v#L29-L30) 就两句：`i_dc_rm = ddc_i - ddc_i_mv_avg; q_dc_rm = ddc_q - ddc_q_mv_avg;`。其中 `ddc_i_mv_avg/ddc_q_mv_avg` 来自一个 `LOG2_AVG_LEN=7`（即 128 点）的双通道滑动平均（[dc_rm.v:43-L54](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/dc_rm.v#L43-L54)）。直觉：信号是交流、零均值，128 点平均 ≈ 直流偏置；原信号减去它就把直流摘掉。

**(c) 幅度平滑 `iq_abs_avg`**。

[iq_abs_avg.v:35-L36](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/iq_abs_avg.v#L35-L36) 对去直流结果取绝对值（看符号位决定取反）；[iq_abs_avg.v:39](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/iq_abs_avg.v#L39) 把 I、Q 两路绝对值各自经 32 点滑动平均（[iq_abs_avg.v:65-L76](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/iq_abs_avg.v#L65-L76)，`LOG2_AVG_LEN=5`）后相加再除 2：`iq_rssi = (i_abs_mv_avg + q_abs_mv_avg) >> 1`。这是线性的「平滑幅度」。

**(d) 线性→dB 的 `iq_rssi_to_db`** —— 一个 4 状态 FSM。

[iq_rssi_to_db.v:25-L28](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/iq_rssi_to_db.v#L25-L28) 定义了 `WAIT_FOR_VALID → PREPARE_P1P2P3 → MULT_ADD_P1P2 → ADD_P3_GEN_FINAL` 四拍。注释（[iq_rssi_to_db.v:22-L24](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/iq_rssi_to_db.v#L22-L24)）点明动机：相邻两个 `iq_rssi_valid` 之间约有 8 拍空隙可用来分步算（100 MHz 时基带 20 MHz 样点正好 5 拍一个，留有余量），把一次「平方 + 乘加 + 移位」拆到多拍以降面积。`PREPARE_P1P2P3`（[iq_rssi_to_db.v:87-L118](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/iq_rssi_to_db.v#L87-L118)）按 `iq_rssi_reg` 的大小选 5 段之一的系数 \((p_1,p_2,p_3,num\_shfit\_bit)\)；最终在 `ADD_P3_GEN_FINAL`（[iq_rssi_to_db.v:143-L146](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/iq_rssi_to_db.v#L143-L146)）算出 `iq_rssi_half_db = (sum_p1p2 + p3) >> num_shfit_bit`。端口注释反复强调「步进是 0.5 dB 不是 1 dB」（如 [iq_rssi_to_db.v:18](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/iq_rssi_to_db.v#L18)）。

**(e) RSSI 顶层 `rssi`** —— 拼装 + 扣增益。

[rssi.v:58-L80](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/rssi.v#L58-L80) 依次例化 `iq_abs_avg` 与 `iq_rssi_to_db`。关键在对齐与校准：AD9361 的增益状态 `gpio_status` 要和「产生这一拍 RSSI 的那批 I/Q 样点」在时间上对齐，所以用 `fifo_sample_delay`（[rssi.v:82-L90](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/rssi.v#L82-L90)）把 `gpio_status` 延迟 `delay_ctl` 拍得到 `gpio_status_delay`。最终的校准公式在 [rssi.v:100-L104](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/rssi.v#L100-L104)：

```verilog
rssi_half_db <= (rssi_half_db_offset + iq_rssi_half_db
                 - {3'b0, gpio_status_delay[6:0], 1'b0} ); // temp formula
```

- `rssi_half_db_offset`：软件校准偏置（来自 `slv_reg7`）。
- `{3'b0, gpio_status_delay[6:0], 1'b0}` = `gpio_status_delay[6:0] << 1`，把「增益值（dB）」换算到「0.5 dB 步进」后**减掉**，相当于把 AD9361 的接收增益从测量值里扣除，得到输入侧（更接近天线口）的强度。
- 注释 `// temp formula` 表示这是经验式、可被软件 offset 进一步校准。

此外 [rssi.v:125-L129](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/rssi.v#L125-L129) 在 `pkt_header_valid_strobe`（收到一个包的 MAC 头）时把当时的 `rssi_half_db` 与 `gpio_status`「锁存」下来，供软件按包读取该帧的接收强度。

**装配点**：`xpu.v` 里 `rssi_i` 的例化见 [xpu.v:691-L721](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L691-L721)，其中 `delay_ctl=slv_reg7[6:0]`、`rssi_half_db_offset=slv_reg7[26:16]`、复位由 `slv_reg0[4]` 单独控制（[xpu.v:701](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L701)），延迟 FIFO 复位由 `slv_reg7[31]` 控制（[xpu.v:702](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L702)）。

#### 4.1.4 代码实践：追踪 RSSI 计算路径（源码阅读型）

1. **实践目标**：把「一个 `ddc_i/ddc_q` 样点」一路追到 `rssi_half_db`，确认每级的位宽、窗口长度与数学含义。
2. **操作步骤**：
   - 打开 `iq_abs_avg.v`，确认实际生效的是 `mv_avg_dual_ch`（`LOG2_AVG_LEN=5`），而不是被注释掉的 `mv_avg`。算出去直流窗口 \(128\)、幅度窗口 \(32\)。
   - 打开 `iq_rssi_to_db.v`，找到 `PREPARE_P1P2P3` 的 5 段分界点（`155 / 516 / 1733 / 5790` 与 `else`），说明它按输入幅度选择不同的二次逼近系数。
   - 打开 `rssi.v:103`，把校准式手算一遍：若 `iq_rssi_half_db = 80`（即 40 dB 的测量值）、`gpio_status_delay[6:0] = 20`（增益 20 dB）、`offset = 0`，则 `rssi_half_db = 0 + 80 - (20<<1) = 40`，对应天线口 20 dB。
3. **需要观察的现象**：注意 `iq_rssi_to_db` 的 FSM 是「样点驱动」的——只有在 `iq_rssi_valid` 来时才推进，100 MHz / 20 MHz 下平均每 5 拍来一个 valid，刚好够 4 拍 FSM 跑完。
4. **预期结果**：你能画出本节 4.1.2 的方框图，并标注每级位宽（`IQ_DATA_WIDTH=16`、`iq_rssi` 16 位、`iq_rssi_half_db` 9 位、`rssi_half_db` 11 位，均来自 `rssi` 模块参数 [rssi.v:13-L19](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/rssi.v#L13-L19)）。
5. 数值算例中的「dB 换算」为教学近似，**精确系数含义待本地用 `test_iq_rssi_interp.m` 验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么去直流用的是 128 点平均、而幅度平滑只用 32 点？
**答案**：直流是极低频分量，需要长窗口才能稳定估出且不伤信号；幅度平滑只需要去掉瞬时包络抖动，窗口太长反而让 RSSI 对「信道占用/空闲」的切换响应变慢，因此用较短窗口。

**练习 2**：`iq_rssi_to_db` 为什么不用查表（LUT）而用分段二次多项式 + FSM？
**答案**：输入 `iq_rssi` 是 16 位、范围大，完整查表代价高；分段二次只需存几组系数，配合分 4 拍的 FSM 还能把大位宽乘法拆散、降低单拍组合逻辑压力（见 [iq_rssi_to_db.v:22-L24](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/iq_rssi_to_db.v#L22-L24) 的注释）。

---

### 4.2 CCA：信道空闲评估

#### 4.2.1 概念说明

CSMA/CA（载波监听多址/冲突避免）在发射前必须确认「信道空闲」。信道忙闲由两路信号合成：**虚拟载波监听**（NAV，看收到的帧里 Duration 字段预留了多久，见 [u5-l2](u5-l2-csma-ca.md)）与**物理载波监听**（本讲的 `ch_idle`）。`cca.v` 负责后者，它综合三类信息：

- **能量**：当前 `rssi_half_db` 是否低于软件设的门限 `rssi_half_db_th`。
- **接收状态**：是否正在解一个包（`demod_is_ongoing`）——正在解包时哪怕能量计算有抖动也必须算「忙」。
- **发射状态**：自己在发（`tx_rf_is_ongoing`）、在等/发 CTS（`cts_toself_rf_is_ongoing`）、在发 ACK/CTS（`ack_cts_is_ongoing`）时一律算「忙」。

此外它还处理一个现实问题：**刚解完一个包的瞬间，能量往往还没掉下来（有拖尾），但信道逻辑上已经空闲**。为此引入「解码后等待窗口」临时强制判闲。

#### 4.2.2 核心流程

```
                 rssi_half_db ──┐
rssi_half_db_th ────────────────┤ (<= 门限?)
                                ├─► ch_idle_rssi ─┐
              demod_is_ongoing ─┘ (且未在解包)     │
                                                   │
   fcs_in_strobe (一个包解完) ──► 触发 wait 计时 ──┤ (计时中强制=1)
   wait_after_decode_top (软件)                   │
                                                   ▼
   tx_rf_is_ongoing ─┐                       ch_idle_rssi
   cts_toself.. ─────┼── 任一为真则忙 ──────►  AND  ──► ch_idle
   ack_cts_is_ongoing┘
```

两条关键表达式：

\[
ch\_idle\_rssi = is\_counting\ ?\ 1\ :\ \big((rssi\_half\_db \le rssi\_half\_db\_th)\ \land\ \lnot\,demod\_is\_ongoing\big)
\]

\[
ch\_idle = ch\_idle\_rssi\ \land\ \lnot\,tx\_rf\_is\_ongoing\ \land\ \lnot\,cts\_toself\_rf\_is\_ongoing\ \land\ \lnot\,ack\_cts\_is\_ongoing
\]

等待窗口长度（基带时钟周期数）：

\[
T_{wait} = wait\_after\_decode\_top \times \texttt{COUNT\_SCALE}
\]

#### 4.2.3 源码精读

`cca.v` 整个模块只有两段：一段等待计数器、一段组合逻辑。

等待计数器在 [cca.v:43-L58](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/cca.v#L43-L58)：
- [cca.v:49](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/cca.v#L49) 把软件值换算到基带时钟刻度：`wait_after_decode_top_scale <= wait_after_decode_top * COUNT_SCALE`（`COUNT_SCALE` 来自 `clock_speed.v`）。
- [cca.v:50](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/cca.v#L50) 是触发条件：**一个包的 FCS 选通**（`fcs_in_strobe`）且对非聚合帧（`~rx_ht_aggr`）或聚合帧的最后一帧（`rx_ht_aggr_last`）到来时，启动计时。聚合业务要等整个 A-MPDU 都解完才触发，避免中途误判。
- [cca.v:54-L55](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/cca.v#L54-L55) 计时满后清 `is_counting`。

组合输出在 [cca.v:60-L61](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/cca.v#L60-L61)：`ch_idle_rssi` 与最终 `ch_idle`，与上面两个公式一一对应。注意 [cca.v:61](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/cca.v#L61) 末尾注释「remove tx_control_state_idle condition, need to separate ch_idle and internal state」——作者刻意把 CCA 的对外结论和 TX 控制内部状态解耦，避免循环依赖。

**装配点**：`xpu.v` 里 `cca_i` 见 [xpu.v:445-L464](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L445-L464)。门限来自 `rssi_half_db_th = slv_reg8[10:0]`（[xpu.v:380](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L380)）；`wait_after_decode_top = slv_reg6[7:0]`（[xpu.v:461](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L461)）；复位由 `slv_reg0[6]` 控制（[xpu.v:449](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L449)，与 `csma_ca` 共享）。

`ch_idle` 随后送入 `csma_ca_i` 作为物理载波监听输入（[xpu.v:507](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L507)），与 NAV 合成最终的 `ch_idle_final`（[xpu.v:519](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L519)），再决定 `backoff_done` 是否产生——这把本讲与 [u5-l2](u5-l2-csma-ca.md) 串了起来。

#### 4.2.4 代码实践：理解 CCA 门限与等待窗口（源码阅读 + 寄存器分析型）

1. **实践目标**：搞清「门限怎么设、等待窗口多长」对 CCA 行为的影响。
2. **操作步骤**：
   - 在 `cca.v` 中确认 `ch_idle` 为真的三个必要条件（能量低、未解包、未收发）。
   - 计算：若基带时钟 100 MHz（`NUM_CLK_PER_US=100`，`COUNT_SCALE=10`），软件写 `slv_reg6[7:0]=50`，则解码后强制判闲的窗口 = \(50 \times 10 = 500\) 个基带时钟 = \(5\,\mu s\)。
3. **需要观察的现象**：思考如果 `wait_after_decode_top` 设太小，会出现什么——刚解完包、能量拖尾还没消退，`rssi_half_db` 仍高于门限，CCA 会**误判信道忙**，从而推迟下一次退避完成。
4. **预期结果**：你能向别人解释「为什么解包后要有一段强制空闲窗口」。
5. 上述时长换算基于 100 MHz 假设；**其它基带时钟（200/240 MHz）下的数值待本地确认 `clock_speed.v` 后重算**。

#### 4.2.5 小练习与答案

**练习 1**：`ch_idle_rssi` 里为什么要 `&& (~demod_is_ongoing)`？
**答案**：正在解包时，能量必定高于门限（否则检不到包），但此时信道显然是「忙」的；用 `demod_is_ongoing` 覆盖能量判断，避免在解包途中因门限比较抖动而误报空闲。

**练习 2**：为什么聚合帧（`rx_ht_aggr`）要等到 `rx_ht_aggr_last` 才触发等待窗口？
**答案**：A-MPDU 是多个子帧拼接的一次传输，信道在整个聚合帧期间都应判忙；只有最后一个子帧的 FCS 才代表「这次传输结束」，此时才开始解包后的空闲窗口。

---

### 4.3 AD9361 SPI 控制：动态开/关 TX 本振

#### 4.3.1 概念说明

AD9361 射频芯片内部寄存器既可由 PS（CPU）通过 SPI 配置，也可由 FPGA 直接驱动 SPI。openwifi 用 `spi.v` 这个小主控做了一件具体的事：**在发射窗口外关掉 TX 本振（LO）/切换射频口，发射时再打开**，以降低功耗与发射链路泄漏。这个「开/关」动作是实时的、跟着 `tx_chain_on` 走，比 CPU 介入快得多。

模块要解决三个工程问题：

1. **要发什么？** 两条 24 位命令字 `SPI_HIGH`（TX LO 关 / RF 口 B）与 `SPI_LOW`（TX LO 开 / RF 口 A），由构建脚本预编译生成。
2. **何时发？** `tx_chain_on` 上升沿（要发射了）发 `SPI_LOW`；下降沿（发完了）发 `SPI_HIGH`。
3. **谁来驱动总线？** 当 FPGA 的片选有效（`spif_csn==0`）时 FPGA 驱动 SCLK/MOSI/CSN；否则透传 CPU 的 SPI 信号——这是软仲裁。

#### 4.3.2 核心流程

```
                  ┌────────────── tx_chain_on (来自 tx_control) ──────────────┐
                  │ 上升沿 → spi_tx_low=1（准备发 SPI_LOW: TX LO 开）          │
                  │ 下降沿 → spi_tx_high=1（准备发 SPI_HIGH: TX LO 关）        │
                  ▼                                                           │
   DISABLED ──(spi_disable=0)──► IDLE ──(时机到 & CPU 未占用总线)──► ACTIVE
   (spi_disable=1:               挑选 data_tx_high/low                        逐位移出 24bit
    持续发 SPI_LOW)               data<=...; spif_csn<=0                       生成 ≤50MHz 的 SPI 时钟
                                                                              完成 → 回 IDLE
```

SPI 时钟分频由基带时钟决定：

\[
clk\_div = \lceil NUM\_CLK\_US\_PER / 100 \rceil,\qquad f_{SPI} \approx \frac{f_{bb}}{2\cdot clk\_div} \le 50\,\text{MHz}
\]

（`clk_div` 见 [spi.v:48](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/spi.v#L48)；每比特占 \(2\cdot clk\_div\) 个基带时钟，故 SPI 时钟约为基带时钟除以 \(2\cdot clk\_div\)。）

#### 4.3.3 源码精读

**(a) 命令字从哪来：构建期生成 `spi_command.v`。**

`spi.v` 顶部 `include "spi_command.v"`（[spi.v:2](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/spi.v#L2)），但仓库里并没有这个文件——它是 `ip_repo_gen.tcl` 在构建期写出来的（[ip_repo_gen.tcl:55-L67](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/ip_repo_gen.tcl#L55-L67)）。脚本用一个开关 `grounded_rf_port` 选两种语义：

- `grounded_rf_port=0`（默认，**LO 控制**）：`SPI_HIGH=24'h088A01`、`SPI_LOW=24'h008A01`。
- `grounded_rf_port=1`（**射频口控制**）：`SPI_HIGH=24'hC22001`、`SPI_LOW=24'hC02001`。

这些是 24 位 AD9361 SPI 命令字（含读/写位、地址、数据），其精确寄存器含义需对照 AD9361 数据手册与板子射频前端连接，**本仓库未在注释中逐位说明，需按实际板卡核对**。`spi.v` 里用 `data_tx_high/data_tx_low` 引用它们（[spi.v:45-L46](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/spi.v#L45-L46)）。

**(b) 总线仲裁：FPGA 与 CPU 二选一。**

[spi.v:62-L64](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/spi.v#L62-L64) 是关键的三态选择：只有当 FPGA 片选有效（`spif_csn==0`）时才用 FPGA 自己生成的 `spif_sclk/spif_mosi/spif_csn`，否则把 CPU 来的 `spi0_*` 透传给 AD9361。CPU 片选 `spi0_csn` 先经 `xpm_cdc_array_single` 从 `ps_clk` 同步到基带时钟域（[spi.v:25-L37](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/spi.v#L25-L37)），且 IDLE 状态里会确认 `spi0_csn_fpga==1`（CPU 未占用总线）才启动发送（[spi.v:97](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/spi.v#L97)）。

**(c) 3 状态机：`DISABLED / IDLE / ACTIVE`**（[spi.v:40-L42](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/spi.v#L40-L42)）。

- **DISABLED**（[spi.v:80-L85](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/spi.v#L80-L85)）：`spi_disable=1`（软件 `slv_reg13[0]=1`）时停留于此并持续发 `SPI_LOW`，注释「Tx LO should be on ... all the time if disabled」——即软件明确要求 TX LO 常开（例如某些调试或外部 PA 控制场景）。一旦 `spi_disable` 撤销，发一次 `SPI_HIGH` 后进 IDLE。
- **IDLE**（[spi.v:86-L108](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/spi.v#L86-L108)）：监听 `tx_chain_on` 跳变（[spi.v:91-L95](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/spi.v#L91-L95)），上升沿置 `spi_tx_low`、下降沿置 `spi_tx_high`；当 CPU 未占总线、且（发 HIGH 时不在发射中）时，把对应命令字装进 `data`、拉低 `spif_csn` 进 ACTIVE。
- **ACTIVE**（[spi.v:109-L135](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/spi.v#L109-L135)）：用 `clk_counter` 数到 `clk_div` 翻转 `spif_sclk` 生成 SPI 时钟（[spi.v:113-L121](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/spi.v#L113-L121)），逐位把 `data[data_counter]` 送 MOSI（[spi.v:111](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/spi.v#L111)），24 位移完后释放片选、回 IDLE（[spi.v:122-L134](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/spi.v#L122-L134)）。

**装配点**：`xpu.v` 里 `spi_module_i` 见 [xpu.v:756-L769](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L756-L769)，其中 `spi_disable=slv_reg13[0]`（[xpu.v:765](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L765)），`spi0_*` 来自 PS，`spi_*` 最终连到顶层 AD9361 的 SPI 引脚。

#### 4.3.4 代码实践：读懂 SPI 命令字的生成与触发（源码阅读型）

1. **实践目标**：搞清「SPI 要发哪两个字、何时发、谁来驱动总线」。
2. **操作步骤**：
   - 打开 `ip_repo_gen.tcl:55-67`，确认默认 `grounded_rf_port=0` 时生成的 `SPI_HIGH/SPI_LOW`，并说明这俩值会被 `cp` 到 `ip/xpu/src/spi_command.v`（[ip_repo_gen.tcl:85](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/boards/ip_repo_gen.tcl#L85)）。
   - 在 `spi.v` 的 IDLE 状态里，分别标出 `tx_chain_on` 上升沿与下降沿各自会发送哪个命令字。
   - 算一次 SPI 时钟：基带 100 MHz 时 `clk_div=(100+99)/100=1`，每比特 \(2\times1=2\) 拍，故 SPI 时钟 ≈ 50 MHz；基带 200 MHz 时 `clk_div=(200+99)/100=2`，每比特 4 拍，SPI 时钟 ≈ 50 MHz——体会「上限 50 MHz」的设计意图。
3. **需要观察的现象**：注意 IDLE 里 `spi_tx_high & !tx_chain_on` 这个额外条件（[spi.v:98](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/spi.v#L98)）——如果「发完了要关 LO」的命令还没来得及发出，新的发射又开始了，就**暂缓关 LO**，避免在发射中途误关。
4. **预期结果**：你能复述 SPI 主控的三状态与仲裁逻辑。
5. 命令字的精确 AD9361 寄存器含义**待本地对照数据手册/原理图确认**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `spi_disable=1` 时反而要持续发 `SPI_LOW`（TX LO 开）？
**答案**：`spi_disable` 表示「FPGA 不要动态切换」，此时软件希望 TX LO 保持常开（或固定选某个射频口），所以模块停在 DISABLED、持续发送代表「LO 开」的 `SPI_LOW`，把射频状态固定住。

**练习 2**：CPU 与 FPGA 都可能驱动同一条 SPI，如何避免冲突？
**答案**：FPGA 只在 `spif_csn==0` 时驱动总线，否则透传 CPU 信号（[spi.v:62-L64](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/spi.v#L62-L64)）；且 FPGA 在 IDLE 里会先确认 CPU 片选 `spi0_csn_fpga==1`（CPU 未占用）才启动自己的发送（[spi.v:97](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/spi.v#L97)），形成软仲裁。

---

## 5. 综合实践：用 `slv_reg57` 观测 RSSI 与 CCA，并理解一次发包前后的感知链

本任务把三节串起来，模拟「驱动工程师想调 CCA 门限」的典型场景。

**背景**：`xpu.v` 把一组观测信号拼进了状态寄存器 `slv_reg57`（[xpu.v:370](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/xpu.v#L370)）：

```verilog
assign slv_reg57 = {gpio_status_delay[6:0], iq_rssi_half_db, 1'b0,
                    (~ch_idle_final),
                    (tx_core_is_ongoing|tx_bb_is_ongoing|tx_rf_is_ongoing|cts_toself_rf_is_ongoing|ack_cts_is_ongoing),
                    demod_is_ongoing, (~gpio_status_delay[7]), rssi_half_db};
// rssi_half_db 11bit, iq_rssi_half_db 9bit
```

**任务步骤**：

1. **拆字段**：依据上面这行，算出 `slv_reg57` 各比特段的含义——最低 11 位是校准后的 `rssi_half_db`、再往上依次是 `demod_is_ongoing`、各种 `*_ongoing` 之和、`~ch_idle_final`（注意是取反，所以该位为 1 表示**信道空闲**）等。把这些字段列成一张表。
2. **算门限**：CCA 门限 `rssi_half_db_th` 来自 `slv_reg8[10:0]`，单位是 0.5 dB。若你想把门限设为 −72 dBm，应写入的值为 \((-72)\times 2\)（注意符号与 offset 的配合）。写出你要写进 `slv_reg8` 的 11 位值。**（精确的 dBm 标定与 `rssi_half_db_offset` 有关，待本地用已知信号源标定。）**
3. **追因果链**：写一段文字，描述「空中出现一个强信号」时，下列信号如何依次变化：`ddc_i/ddc_q` 幅度上升 → `iq_rssi` 上升 → `iq_rssi_half_db`/`rssi_half_db` 上升 → 在 `cca.v` 中 `rssi_half_db > rssi_half_db_th` → `ch_idle_rssi=0` → `ch_idle=0` → 喂进 `csma_ca` → 退避计数器冻结（见 [u5-l2](u5-l2-csma-ca.md)）。
4. **加观测**（可选进阶）：参考 [u7-l6](u7-l6-gpio-led-ila-debug.md)，在 `create_ip_repo.sh` 里给 xpu 启用 `XPU_ENABLE_DBG` 宏（它会让本讲模块端口上的 `mark_debug` 生效，见如 [cca.v:8-L12](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/cca.v#L8-L12)、[rssi.v:7-L11](https://github.com/open-sdr/openwifi-hw/blob/d047d794195beb72e12d2a9a6c205c16399cf288/ip/xpu/src/rssi.v#L7-L11)），用 ILA 抓 `rssi_half_db`、`ch_idle`、`tx_chain_on`、`spi_csn` 的时序关系，验证「发包时 `tx_chain_on` 上升 → `spi_csn` 拉低发 SPI_LOW；发完 → 发 SPI_HIGH」。

**预期产出**：一张 `slv_reg57` 字段表 + 一条门限计算 + 一段因果链文字。若你做完了第 4 步，还会得到一张包含 RSSI/CCA/SPI 时序的 ILA 波形截图。

## 6. 本讲小结

- **RSSI 链**：`ddc_i/ddc_q` 先经 `dc_rm`（128 点滑动平均去直流）→ 取绝对值 + 32 点滑动平均（`iq_abs_avg`）合成线性幅度 `iq_rssi` → 经 `iq_rssi_to_db` 的分段二次多项式 FSM 换算成 0.5 dB 步进的 `iq_rssi_half_db` → 在 `rssi.v` 里扣掉 AD9361 增益（`gpio_status_delay[6:0]<<1`）并加软件偏置，得到校准后的 `rssi_half_db`。
- **滑动平均**：仓库提供单通道 `mv_avg` 与双通道 `mv_avg_dual_ch` 两个原语，都是「FIFO 回读最旧样点 + 加新减旧」，预热 \(N\) 个样点后输出真平均；RSSI 链实际用的是双通道版。
- **CCA**：`cca.v` 用 `(rssi_half_db ≤ rssi_half_db_th) && !demod_is_ongoing` 作能量判闲，叠加「解码后等待窗口」（`wait_after_decode_top × COUNT_SCALE`）防止拖尾误判，再用各 `*_ongoing` 屏蔽收发态，输出 `ch_idle` 给 `csma_ca` 作物理载波监听。
- **SPI 控制**：`spi.v` 是 3 状态机（DISABLED/IDLE/ACTIVE），跟随 `tx_chain_on` 跳变发送预编译命令字 `SPI_LOW`（TX LO 开）/`SPI_HIGH`（TX LO 关），命令字由 `ip_repo_gen.tcl` 在构建期写进 `spi_command.v`。
- **总线仲裁**：FPGA 仅在 `spif_csn==0` 时驱动 SPI，否则透传 CPU，并在 IDLE 里确认 CPU 片选无效后才发送。
- **软件接口**：门限 `slv_reg8[10:0]`、等待窗口 `slv_reg6[7:0]`、RSSI 延迟/偏置 `slv_reg7`、SPI 开关 `slv_reg13[0]`，状态观测集中在一行 `slv_reg57` 的位拼接里。

## 7. 下一步学习建议

- **向控制侧延伸**：本讲的 `ch_idle` 是 `csma_ca` 的物理载波监听输入。接下来读 [u5-l2](u5-l2-csma-ca.md)（CSMA/CA）、[u5-l3](u5-l3-tx-control-retrans-ack.md)（TX 控制/重传/ACK），看 `tx_chain_on`、`backoff_done`、`retrans_in_progress` 这些本讲反复出现的信号是如何被产生和消费的。
- **向可观测侧延伸**：`rssi_half_db`、`iq_rssi_half_db`、CSI 等「感知」信号会被 `side_ch` 采集上报，建议接着读 [u6-l1](u6-l1-side-channel.md)。
- **动手验证**：若想真正看到本讲的波形，按 [u7-l2](u7-l2-conditional-compile-macros.md) 启用 `XPU_ENABLE_DBG`，再按 [u7-l3](u7-l3-ip-simulation-testbench.md) 跑 `mv_avg` 的 testbench（`ip/xpu/unit_test/mv_avg/`）理解滑动平均原语的行为，按 [u7-l6](u7-l6-gpio-led-ila-debug.md) 用 ILA 抓 RSSI/CCA/SPI 时序。
- **寄存器细节**：想确认每个 `slv_reg` 的精确地址映射，进入 [u7-l1](u7-l1-axi-register-map.md) 读 `xpu_s_axi.v`。
