# 时钟树与频率合成

## 1. 本讲目标

AERIS-10 是一台 10.5 GHz 相参雷达，整条信号链——发射的 chirp、接收的本振、采样的 ADC、运行的 FPGA——全部要靠一组**同源、低抖动、相位对齐**的时钟来驱动。如果时钟不稳，发射和接收之间的相位关系就会漂移，脉冲压缩与 Doppler 处理都会失真。本讲解决一个核心问题：**这一整套时钟从哪里来、怎么分配、怎么保证相位对齐？**

学完本讲你应当能够：

- 画出从 **AD9523-1 时钟发生器**到 ADF4382A TX/RX 频综、DAC、ADC、FPGA 各路时钟的**分配树**，并说出每一路的频率与分频比。
- 看懂 `ad9523.c`、`adf4382.c`、`adf4382a_manager.c` 三个驱动的分层结构，理解「寄存器读改写 → `io_update`/自校准 → 锁定检测」这套配置流程。
- 说清楚 **TX 本振 10.5 GHz 与 RX 本振 10.38 GHz 的差正好是 120 MHz 中频**，并把它和 [u4-l1](u4-l1-ddc-digital-downconversion.md) 里 DDC 的 NCO 调谐字 `0x4CCCCCCD`（= 0.3 × 2³²）对应起来。
- 解释为什么雷达对时钟抖动（相位噪声）极其敏感，以及 `ad9523_sync()` + ADF4382A Timed Sync 如何把多片芯片的输出相位对齐到同一条边沿。

## 2. 前置知识

在进入源码之前，先用三段话建立直觉。这些概念在后续源码精读里会反复出现。

### 2.1 为什么雷达需要「时钟树」而不是一颗晶振

一颗晶振只能给一个频率。但 AERIS-10 主板上同时需要：

- DAC 采样时钟 120 MHz（生成 chirp）；
- ADC 采样时钟 400 MHz（数字化回波）；
- FPGA 系统时钟 100 MHz；
- 两片本振频综（ADF4382A）的 300 MHz 参考；
- 两片本振之间同步用的 60 MHz SYNCP/SYNCN；
- 一路 20 MHz 测试时钟。

这些频率不仅要准，还必须**同源**——也就是都从同一个基准频率分频或倍频而来。只有这样，120 MHz 的 DAC 时钟与 400 MHz 的 ADC 时钟之间才有确定性的相位关系，发射与接收才能相参。**时钟树**（clock tree）就是把一个高质量基准频率「分发」成这一整族同源时钟的硬件结构。在 AERIS-10 上，这棵树的根是一颗 100 MHz 的 VCXO，树干是 **AD9523-1 时钟发生器**，树枝是它输出的 14 路可编程分频通道。

### 2.2 锁相环（PLL）与频率合成

如何从 100 MHz「长出」3.6 GHz、10.5 GHz？靠**锁相环**（Phase-Locked Loop, PLL）。一个 PLL 的核心结构是：

```
基准频率 f_ref → [R 分频] → 鉴相器(PFD) ← [N 分频] ← VCO 输出 f_out
                       ↓ 电荷泵 → 环路滤波 → 控制电压 → VCO
```

鉴相器比较「分频后的基准」与「分频后的 VCO 输出」的相位差，用误差电压去牵引 VCO（压控振荡器），直到两者频率相等。锁定时满足：

\[
f_{\text{out}} = N \cdot \frac{f_{\text{ref}}}{R}
\]

这样只要改变分频比 \(N\)，就能让 VCO 输出任意（在范围内）的频率。**频率合成器**（frequency synthesizer）就是基于 PLL、通过编程分频比来「合成」目标频率的芯片。AD9523-1 内部有一组 PLL2 把 100 MHz 倍频到 3.6 GHz VCO；ADF4382A 是宽带 RF 频率合成器，把 300 MHz 参考「合成」到 10.5 GHz。

### 2.3 时钟抖动、相位噪声与相参性

理想的方波/正弦波边沿是周期的；真实时钟的边沿会在理想位置附近**随机晃动**，这个晃动叫**抖动**（jitter），频域里表现为载波两侧的裙边噪声，叫**相位噪声**（phase noise）。对一颗频率为 \(f_c\) 的本振，时域抖动 \(\Delta t\) 直接转化为相位误差：

\[
\Delta\phi = 2\pi f_c \cdot \Delta t
\]

在 10.5 GHz，1 皮秒（ps）抖动就对应 \(\Delta\phi \approx 2\pi \times 10.5\times10^9 \times 10^{-12} \approx 0.066\) 弧度。这个相位误差会**直接混进**下变频后的中频信号，抬高噪声底，破坏脉冲压缩与 Doppler FFT 的相参积累增益。这就是雷达必须用**低抖动**时钟发生器（AD9523-1）与高质量本振（ADF4382A）的原因。

另一个相关概念是**相参性**（coherence）：发射的每个 chirp 起始相位必须与接收采样时钟有确定关系，否则把多个 chirp 沿「慢时间」做 FFT（[u4-l4](u4-l4-doppler-processing.md)）就得不到干净的速度谱。相参性要求所有时钟同源且**相位对齐**——这正是本讲最后 `sync` 机制要解决的问题。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [ad9523.c](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ad9523.c) | AD9523-1 时钟发生器的 no-OS 驱动：SPI 寄存器读写、PLL2/VCO 配置、14 路通道分频、校准与状态检查。 |
| [ad9523.h](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ad9523.h) | AD9523 寄存器位定义、`ad9523_platform_data`/`ad9523_channel_spec` 结构体、驱动模式枚举（LVDS/CMOS/LVPECL）。 |
| [adf4382.c](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/adf4382.c) | ADF4382/4382A/4383 宽带 RF 频综的 no-OS 驱动：分数分频计算、VCO 自校准、相位调整、同步。 |
| [adf4382a_manager.c](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/adf4382a_manager.c) | 项目自写的「双 LO 管理器」：把 TX（10.5 GHz）与 RX（10.38 GHz）两片 ADF4382A 一起初始化、同步、调相位、查锁定。 |
| [adf4382a_manager.h](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/adf4382a_manager.h) | LO 管理器的引脚映射、目标频率常量（`TX_FREQ_HZ`/`RX_FREQ_HZ`）、同步方式枚举。 |
| [main.cpp](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp) | STM32 入口；`configure_ad9523()` 与 `ADF4382A_Manager_Init()` 的调用点，定义了完整的通道分频表。 |

数据流上的位置：本讲属于 [u7-l1](u7-l1-stm32-main-and-peripherals.md) 之后的 STM32 上电时序细分——在「电源时序」之后、「FPGA 上电」之前，STM32 必须先把整板时钟点亮并确认锁定，下游数字处理才有意义。

## 4. 核心概念与源码讲解

本讲按三个最小模块组织：**时钟发生器**（AD9523-1）、**频综驱动**（ADF4382）、**相位对齐与双 LO 管理**（adf4382a_manager）。

### 4.1 时钟发生器：AD9523-1

#### 4.1.1 概念说明

AD9523-1 是一颗**低抖动时钟发生器**（jitter cleaner / clock generator），它的任务是把板上一颗 100 MHz VCXO「清洗 + 倍频 + 分发」成一整族同源时钟。内部结构可以简化为：

```
100 MHz VCXO ──► PLL2 (倍频) ──► 3.6 GHz VCO ──► 14 路 channel divider ──► OUT0..OUT13
                     ↑                                    (每路独立分频 + 驱动模式)
                  SYNC 对齐 ─────────────────────────────►
```

它的核心价值有两点：

1. **PLL2 把 100 MHz 倍频到高频 VCO**，于是输出不再受限于晶振本身能到的频率；
2. **14 路独立的通道分频器**，每一路可以单独设置分频比与驱动电平（LVDS / LVPECL / LVCMOS），用同一棵树同时喂饱 DAC、ADC、FPGA、频综等电平与频率各异的负载。

AD9523 没有片上非易失默认值，每次上电都要由 STM32 经 SPI 把整套寄存器写进去——这就是 `ad9523.c` 存在的理由。

#### 4.1.2 核心流程

AD9523 的配置流程是一个典型的「**写保持寄存器 → 触发 IO_UPDATE → 校准 → 查锁定 → 同步**」序列：

1. **SPI 寄存器读写**：所有配置先写进芯片的「保持寄存器」（hold registers），此时并不生效。
2. **IO_UPDATE**：向 `AD9523_IO_UPDATE` 寄存器写 `AD9523_IO_UPDATE_EN`，把保持寄存器的值一次性搬运到生效寄存器。
3. **VCO 校准**：触发 `AD9523_PLL2_VCO_CALIBRATE`，让芯片自动选择正确的 VCO 频段与校准值，然后轮询 `AD9523_READBACK_1` 直到校准完成。
4. **状态检查**：读 `AD9523_READBACK_0`，确认 VCXO、PLL2 锁定（`STAT_PLL2_LD`）等关键标志。
5. **SYNC**：拉 `AD9523_STATUS_SIGNALS_SYNC_MAN_CTRL` 一个周期，把所有通道分频器**同时复位到已知相位**，实现相位对齐。

PLL2 的输出频率由公式给出：

\[
f_{\text{VCO}} = \frac{f_{\text{VCXO}}}{R2} \cdot N, \qquad N = 4 \cdot B + A
\]

其中 `R2 = pll2_r2_div + 1`（代码里 `pll2_r2_div = 0` 表示 R2=1），\(N = 4B + A\) 由 `pll2_ndiv_b_cnt`（B）与 `pll2_ndiv_a_cnt`（A）组成。本板取 VCXO=100 MHz、R2=1、B=9、A=0，即 \(N = 36\)，得到 \(f_{\text{VCO}} = 3.6\) GHz。每个通道再对 3.6 GHz 做整数分频得到目标频率。

#### 4.1.3 源码精读

**SPI 写函数**——注意它会把多字节寄存器拆成多次 3 字节传输（`buf[0..2]` = 高地址、低地址、数据）：

[ad9523.c:87-107](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ad9523.c#L87-L107) 把 `reg_addr`/`reg_data` 打包成 `{高地址字节, 低地址字节, 数据}` 经 `no_os_spi_write_and_read` 发出，这正是所有后续配置调用的底层原语。

**IO_UPDATE 触发生效**——一行寄存器写，把保持寄存器搬运到生效寄存器：

[ad9523.c:116-121](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ad9523.c#L116-L121) 只写 `AD9523_IO_UPDATE` 寄存器为 `AD9523_IO_UPDATE_EN`，却是一切配置生效的「扳机」。

**VCO 校准与轮询**——触发后必须在超时窗口内等到校准完成位清零：

[ad9523.c:206-235](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ad9523.c#L206-L235) 写 `AD9523_PLL2_VCO_CTRL` 的 `AD9523_PLL2_VCO_CALIBRATE` 位，然后在最多 100 ms 内轮询 `AD9523_READBACK_1` 的 bit0；仍为 1 就判定校准失败。

**VCO 频率计算**——这就是公式 \(f_{\text{VCO}} = f_{\text{VCXO}}/R2 \cdot (4B+A)\) 的代码化：

[ad9523.c:592-597](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ad9523.c#L592-L597) 把 VCXO 频率、倍频器、R2 分频、N 分频（用宏 `AD9523_PLL2_FB_NDIV(a,b) = 4*b+a`）乘在一起，缓存到 `dev->ad9523_st.vco_freq`，供下游通道频率推导与状态上报使用。

**通道分发循环**——这是时钟树真正「长出树枝」的地方。`ad9523_setup` 遍历平台数据里的每个通道，把驱动模式、分频比、相位拼进 `AD9523_CHANNEL_CLOCK_DIST(ch)` 寄存器：

[ad9523.c:646-672](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ad9523.c#L646-L672) 对每个 `channels[i]` 写入 `AD9523_CLK_DIST_DRIVER_MODE | AD9523_CLK_DIST_DIV | AD9523_CLK_DIST_DIV_PHASE` 等字段；紧接其后的 [ad9523.c:674-681](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ad9523.c#L674-L681) 把没被 `active_mask` 选中的通道强制 `TRISTATE` + 关断，避免悬空输出引入干扰。

**状态检查**——确认 PLL2 真的锁定了才返回 0：

[ad9523.c:248-295](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ad9523.c#L248-L295) 轮询 `AD9523_READBACK_0`，检查 `STAT_VCXO` 与 `STAT_PLL2_LD`（PLL2 lock detect）位；任一缺失就打印错误并返回 -1。

**真正的「时钟树表」不在驱动里，而在 main.cpp**。驱动的 `ad9523_setup` 只是把 `pdata` 翻译成寄存器写；真正决定每路输出频率的是 STM32 端填写的通道表：

[main.cpp:1078-1092](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L1078-L1092) 设定 VCXO=100 MHz、PLL2 的 N=36（B=9, A=0）、R2=1，即 VCO=3.6 GHz。

[main.cpp:1116-1174](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L1116-L1174) 是整棵时钟树的真值表，逐通道写分频比与驱动模式：

| 通道 | 分频比 | 频率 | 驱动 | 去向 |
|------|--------|------|------|------|
| OUT0 | /12 | 300 MHz | LVDS_7mA | ADF4382A **TX** 参考 |
| OUT1 | /12 | 300 MHz | LVDS_7mA | ADF4382A **RX** 参考 |
| OUT4 | /9 | 400 MHz | LVDS_7mA | ADC 采样时钟 |
| OUT5 | /9 | 400 MHz | LVDS_7mA | FPGA ADC 时钟 |
| OUT6 | /36 | 100 MHz | LVCMOS | FPGA 系统时钟 |
| OUT7 | /180 | 20 MHz | LVCMOS | FPGA 测试时钟 |
| OUT8 | /60 | 60 MHz | LVDS_4mA | SYNC_TX（本振同步） |
| OUT9 | /60 | 60 MHz | LVDS_4mA | SYNC_RX（本振同步） |
| OUT10 | /30 | 120 MHz | LVCMOS | DAC 时钟 |
| OUT11 | /30 | 120 MHz | LVCMOS | FPGA DAC 时钟 |

这张表把本讲与前后几讲串了起来：OUT4 的 400 MHz 正是 [u4-l1](u4-l1-ddc-digital-downconversion.md) AD9484 ADC 的采样率；OUT10 的 120 MHz 正是 [u5-l1](u5-l1-plfm-chirp-and-transmitter.md) DAC chirp 生成的 `FS`；OUT6 的 100 MHz 是 FPGA 系统域；OUT0/OUT1 的 300 MHz 喂给下面要讲的两片 ADF4382A。

> 注意「同源」的工程含义：因为 400 MHz（ADC）、120 MHz（DAC）、300 MHz（LO 参考）、100 MHz（FPGA）全部是 3.6 GHz VCO 的整数分频，它们的边沿在每次 SYNC 后都保持固定的相对相位，这正是相参雷达的硬件前提。

#### 4.1.4 代码实践

**实践目标**：通过阅读 `configure_ad9523()` 与 `ad9523_setup()`，验证「通道分频表 → 实际输出频率」的推导，并理解 IO_UPDATE/校准/锁定三步时序。

**操作步骤**：

1. 打开 [main.cpp:1068-1252](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L1068-L1252)，定位 `configure_ad9523()`。
2. 用公式 \(f_{\text{out}} = f_{\text{VCO}} / \text{channel\_divider}\) 手算 OUT7：3.6 GHz / 180 = ? 应得到 20 MHz。
3. 顺着函数体列出 STM32 调用 AD9523 驱动的顺序：`AD9523_RESET_RELEASE()` → `AD9523_REF_SEL(true)` → `ad9523_setup()` → `ad9523_status()` → `ad9523_sync()`。
4. 在 `ad9523_setup` 内部再追一层：它最后依次调用 `ad9523_io_update()` → `ad9523_sync()` → `ad9523_calibrate()` → `ad9523_sync()` → `ad9523_status()`（见 [ad9523.c:695-712](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ad9523.c#L695-L712)）。

**需要观察的现象**：`ad9523_setup` 返回前会调用 `ad9523_status`，若 PLL2 未锁定（`STAT_PLL2_LD` 缺失），该函数返回 -1，`configure_ad9523` 随即返回非 0，main 里的调用方会进入错误处理（`ERROR_AD9523_CLOCK`）。也就是说，**时钟没锁定，整板不会继续往下跑**。

**预期结果**：OUT7 = 20 MHz；时序链为「复位释放 → 选参考 → setup（内部含 io_update/sync/calibrate/sync/status）→ status → sync」。

**待本地验证**：若你有实板，可在 `configure_ad9523()` 的 `ad9523_status()` 之后加一行 `printf` 打印 `dev->ad9523_st.vco_freq`，确认其等于 3600000000。

#### 4.1.5 小练习与答案

**练习 1**：如果把 OUT6 的 `channel_divider` 从 36 改成 72，FPGA 系统时钟会变成多少？会对哪一讲描述的 FPGA 时钟域产生影响？

**答案**：3.6 GHz / 72 = 50 MHz。FPGA 系统时钟会减半到 50 MHz，影响 [u3-l1](u3-l1-fpga-top-module.md) 与 [u3-l2](u3-l2-clock-domains-and-cdc.md) 里描述的 100 MHz 主处理域——所有定时参数（如 [u4-l2](u4-l2-matched-filter-pulse-compression.md) 里 `LATENCY=3187` 对应的微秒数）都要按新时钟重算。

**练习 2**：`ad9523_setup` 在结尾连续调用了两次 `ad9523_sync()`（[ad9523.c:699-708](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/ad9523.c#L699-L708)），中间还夹了一次 `ad9523_calibrate()`。为什么校准前后都要 sync？

**答案**：SYNC 把所有通道分频器复位到统一相位。校准前的 sync 保证校准起点一致；VCO 校准会扰动分频器相位，所以校准后必须再 sync 一次，才能让 14 路输出在最终生效时彼此相位对齐。

---

### 4.2 频综驱动：ADF4382

#### 4.2.1 概念说明

AD9523 解决了「低频时钟分发」，但雷达真正需要的 10.5 GHz 射频 carrier 还没有——AD9523 的 VCO 只到 3.6 GHz。产生 10.5 GHz 本振（LO）的是另一类芯片：**宽带 RF 频率合成器 ADF4382A**。它内部有一颗高达 ~11 GHz 的 VCO，外接 300 MHz 参考（来自 AD9523 的 OUT0/OUT1），靠**分数锁相环**把参考倍频到目标 RF 频率。

为什么用「分数」分频？整数分频只能产出 \(f = N \cdot f_{\text{PFD}}\) 的离散点，分辨率受 PFD 频率限制；分数分频用一个ΣΔ调制器实现 \(f = (N + \text{FRAC}/M) \cdot f_{\text{PFD}}\)，分辨率可达亚赫兹，代价是引入分数杂散（靠 bleed 电流与选窗补偿）。`adf4382.c` 就是 Analog Devices 官方的 no-OS 驱动，`adf4382a_manager.c` 是项目自写、把它用在本板双 LO 场景的薄包装。

本板用 ADF4382**A** 变体（`id = ID_ADF4382A`），与基型 ADF4382 的差别主要在频率范围（注释见 [adf4382.c:527-529](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/adf4382.c#L527-L529)）。

#### 4.2.2 核心流程

ADF4382 的配置流程比 AD9523 更复杂，因为它要自己算分频比：

1. **设参考频率** `adf4382_set_ref_clk`：把期望的参考频率（300 MHz）存入 `dev->ref_freq_hz`，并钳制到 [10 MHz, 5 GHz]。
2. **算 PFD 频率**：\(f_{\text{PFD}} = f_{\text{ref}} / \text{ref\_div}\)，若开了 doubler 再 ×2。
3. **选 VCO 分频比** `clkout_div`：找一个 \(2^k\) 使 \(f_{\text{out}} \cdot 2^k\) 落在 VCO 范围内。
4. **算反馈分频比** `adf4382_pll_fract_n_compute`：把目标 RF 频率分解为整数 \(N\)、分数 FRAC1、次分数 FRAC2/MOD2。
5. **算 bleed 与锁窗**：分数模式下开 bleed 电流、按 PFD 频率选 `ldwin_pw`。
6. **按序写寄存器**：把 FRAC、MOD、bleed、clkout_div、N 依次写入；**N_INT 必须最后写**，因为写 N_INT 会触发一次 VCO 自校准。
7. **查锁定**：等待 `ADF4382_LKD_DELAY_US` 后读寄存器 `0x58` 的 `ADF4382_LOCKED_MSK`，未锁返回 `-EIO`。

输出频率的合成公式（锁定时）：

\[
f_{\text{out}} = \frac{f_{\text{PFD}}}{2^{\text{clkout\_div}}} \cdot \left(N + \frac{\text{FRAC1}}{2^{24}} + \frac{\text{FRAC2}}{\text{MOD2}}\right)
\]

其中 \(2^{24}\) 即 `ADF4382_MOD1WORD`（FRAC1 的固定模）。

#### 4.2.3 源码精读

**SPI 读改写原语**——`adf4382_spi_update_bits` 是几乎所有配置的底层，先读、改指定 bit、再写回；若值未变则跳过写以减少总线流量：

[adf4382.c:137-154](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/adf4382.c#L137-L154) 实现 `tmp = orig & ~mask; tmp |= data & mask;` 的标准「读改写」，且只在 `tmp != orig` 时才真正发起 SPI 写。

**设参考频率**——入口钳制 + 联动重算：

[adf4382.c:203-222](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/adf4382.c#L203-L222) 把 `val` 钳到 `[ADF4382_REF_CLK_MIN, ADF4382_REF_CLK_MAX]`，存入 `dev->ref_freq_hz`，然后调用 `adf4382_set_freq` 与 `adf4382_set_vco_cal_timeout` 重新计算整套分频参数。这说明在 ADF4382 的抽象里，**改参考频率等于重新合成一次**。

**PFD 频率计算**——这是上面公式的第一步：

[adf4382.c:555-564](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/adf4382.c#L555-L564) `pfd_freq = ref_freq_hz / ref_div`，若 `ref_doubler_en` 再 ×2。本板 `ref_div=1`、doubler 关，故 PFD = 300 MHz。

**分数分频核心**——把目标频率拆成 N + FRAC1 + FRAC2/MOD2：

[adf4382.c:742-763](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/adf4382.c#L742-L763) 先 `n_int = freq / pfd_freq` 取整，余数乘 `ADF4382_MOD1WORD` 得 FRAC1；若仍有余数，进 `adf4382_frac2_compute` 算次级 FRAC2/MOD2 以达到亚赫兹分辨率。

**N_INT 最后写 + 查锁定**——这是触发自校准与判定成功的关键：

[adf4382.c:1579-1594](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/adf4382.c#L1579-L1594) 注释明写「Need to set N_INT last to trigger an auto-calibration」；写完 0x10 后 `no_os_udelay(ADF4382_LKD_DELAY_US)` 再读 0x58 的 `ADF4382_LOCKED_MSK`，未锁返回 `-EIO`。这条「N_INT 必须最后写」的约定解释了为什么 `adf4382_set_change_freq`（[adf4382.c:1148-1311](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/adf4382.c#L1148-L1311)）故意把 N_INT 留到 `adf4382_set_start_calibration`（[adf4382.c:1331-1336](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/adf4382.c#L1331-L1336)）单独触发——把「改频率」与「启动校准」解耦，方便精确测量锁定时间。

**初始化总入口**——`adf4382_init` 串起复位、4-wire SPI、寄存器默认表、设频、校准：

[adf4382.c:1989-2103](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/adf4382.c#L1989-L2103) 关键步骤：写 `ADF4382_RESET_CMD` 软复位 → 配 4-wire SPI → `adf4382_check_scratchpad`（写读 0x00A 验证 SPI 链路通）→ 循环写 `adf4382_reg_defaults` 默认表 → `adf4382_set_freq` → `adf4382_set_vco_cal_timeout` → 设输出功率。其中 `switch(init_param->id)`（[adf4382.c:2017-2041](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/adf4382.c#L2017-L2041)）按 `ID_ADF4382`/`ID_ADF4382A`/`ID_ADF4383` 选不同的 `freq_max/freq_min/vco_max/vco_min`，这就是「一个驱动支持三款芯片」的分支点。

#### 4.2.4 代码实践

**实践目标**：跟踪「目标频率 → N/FRAC → 寄存器写」的完整链路，理解分数分频为什么能做到亚赫兹分辨率。

**操作步骤**：

1. 打开 [adf4382.c:1343-1595](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/adf4382.c#L1343-L1595) 的 `adf4382_set_freq`。
2. 在脑海里代入本板 RX 的数字：`freq = 10.38 GHz`，`pfd_freq = 300 MHz`，手算 `n_int = 10380000000 / 300000000 = 34`，余 180 MHz；FRAC1 = 180e6 × 2²⁴ / 300e6。
3. 顺着函数体列出寄存器写入顺序：bleed(0x1D/0x1E) → en_bleed(0x1F) → MOD2(0x1A..0x1C) → FRAC2(0x17..0x19) → FRAC1(0x12..0x15) → clkout_div(0x11) → **N_INT(0x10) 最后**。
4. 确认最后 `no_os_udelay` + 读 0x58 查锁定。

**需要观察的现象**：N_INT（寄存器 0x10）是**最后一个被写的**，在此之前所有 FRAC/MOD/bleed 都已就位。这正是「写 0x10 触发自校准」这条芯片约定在代码里的体现。

**预期结果**：RX LO 的 N_INT=34，剩余分数部分由 FRAC1/FRAC2 承担；函数末尾读 0x58 的 `ADF4382_LOCKED_MSK` 为 1 才返回 0。

**待本地验证**：若你接入实板，可调用 `adf4382_reg_dump`（[adf4382.c:161-192](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/adf4382.c#L161-L192)）打印 0x00–0x67、0x100–0x111、0x200–0x273 三段寄存器，对照数据手册验证 0x10/0x11 与你手算的 N_INT/clkout_div 一致。

#### 4.2.5 小练习与答案

**练习 1**：本板 PFD = 300 MHz。若改用整数分频（关掉分数部分），能直接合成 10.5 GHz 吗？最近的可输出频率是多少？

**答案**：整数分频时 \(f_{\text{out}} = N \cdot f_{\text{PFD}}\)，10.5 GHz / 300 MHz = 35，正好整除，所以 TX LO（10.5 GHz = 35 × 300 MHz）可用整数模式；但 RX LO 10.38 GHz / 300 MHz = 34.6，无法整除，必须用分数分频才能精确合成。这正是驱动里保留 FRAC1/FRAC2 路径的必要性。

**练习 2**：为什么 `adf4382_set_freq` 在写完所有 FRAC/MOD/bleed 之后，要把 N_INT 留到最后写？

**答案**：ADF4382 硬件约定「写寄存器 0x10（N_INT LSB）会触发一次 VCO 自校准」。如果先写 N_INT 再写 FRAC，校准会在错误的 FRAC 值上发生；把 N_INT 放最后，保证校准基于完整、正确的分频参数，校准完即可锁定。

---

### 4.3 相位对齐与双 LO 管理：adf4382a_manager

#### 4.3.1 概念说明

到目前为止，AD9523 已经分发好低频时钟，ADF4382 驱动也能合成单颗 LO。但 AERIS-10 需要**两片** ADF4382A：一片做 TX 本振（10.5 GHz，等于载波），一片做 RX 本振（10.38 GHz）。两片之间有两个额外要求：

1. **共享 SPI 总线**：两片芯片挂在同一条 SPI4 上，用不同的片选（CS）区分。
2. **相位对齐**：两片 LO 的输出相位必须确定，才能保证上下变频的相参性；为此用 60 MHz 的 SYNCP/SYNCN 信号（来自 AD9523 的 OUT8/OUT9）做硬件 Timed Sync。

`adf4382a_manager.c` 就是项目自写的「双 LO 管理器」，把上面这些细节封装成一个 `ADF4382A_Manager` 对象。它还顺便管理了 DELADJ/DELSTR 引脚——这是 ADF4382A 的模拟延迟调整引脚，通过 STM32 的 TIM3 PWM 输出可调占空比、经外部低通滤波成直流电压，实现**皮秒级的本振相位微调**。

> **贯穿性洞察**：TX LO − RX LO = 10.5 GHz − 10.38 GHz = **120 MHz**。这个 120 GHz……不，120 MHz，正是接收链混频后得到的中频（IF）。而 [u4-l1](u4-l1-ddc-digital-downconversion.md) 里 DDC 的 NCO 调谐字 `FTW = 0x4CCCCCCD = 0.3 × 2³²`，对应的归一化频率 0.3 × 400 MHz = **120 MHz**。两讲在这里精确咬合：本讲决定的 LO 频差，就是下一级数字下变频要搬移的中频。

#### 4.3.2 核心流程

`ADF4382A_Manager_Init` 的流程（带 `DIAG` 诊断日志）：

1. **配两套 SPI 参数**：都指向 `hspi4`，但 `cs_pin` 分别是 `TX_CS_Pin` 与 `RX_CS_Pin`（GPIOG 上的不同引脚）。
2. **填 TX/RX 初始化参数**：TX 目标 10.5 GHz、RX 目标 10.38 GHz，参考都是 300 MHz，`id = ID_ADF4382A`；电荷泵电流（`cp_i`）与 bleed 不同（TX 用 cp_i=3/bleed=1000，RX 用 cp_i=4/bleed=1200），因为两片的工作点不同。
3. **拉 CE（chip enable）**：先置位 `TX_CE_Pin`/`RX_CE_Pin` 使能两片芯片。
4. **依次初始化**：先 `adf4382_init(&tx_dev, &tx_param)`，再 `adf4382_init(&rx_dev, &rx_param)`；任一失败则回滚（关 CE、释放已建对象）。
5. **设输出功率与使能**：两片都设 ch0 功率、使能 ch0、关 ch1。
6. **置 `initialized = true`**：注意必须在 sync 配置**之前**置位（这是源码注释里点名的 Bug #1 修复）。
7. **配同步**：按传入的 `method` 调 `ADF4382A_SetupTimedSync`（硬件 60 MHz）或 `ADF4382A_SetupEZSync`（纯 SPI）。

锁定的检测用「**双源交叉验证**」：既读 ADF4382A 寄存器 0x58 的 `ADF4382_LOCKED_MSK`，又读 STM32 的 `TX_LKDET`/`RX_LKDET` GPIO；两者一致才认为真锁定，不一致会打 `DIAG_WARN` 提示「possible pin mapping issue」。

相位微调的数据通路：

\[
\text{phase\_ps} \xrightarrow{\text{phase\_ps\_to\_duty\_cycle}} \text{duty} \xrightarrow{\text{TIM3 PWM}} \text{DELADJ 引脚} \xrightarrow{\text{外部低通滤波}} V_{\text{DELADJ}} \xrightarrow{\text{ADF4382A}} \Delta\phi
\]

`ADF4382A_StrobePhaseShift` 再发一个 DELSTR 脉冲，把当前的 DELADJ 电压锁存进芯片。

#### 4.3.3 源码精读

**目标频率与引脚常量**——全部集中在头文件，便于一处修改：

[adf4382a_manager.h:34-37](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/adf4382a_manager.h#L34-L37) 定义 `REF_FREQ_HZ = 300 MHz`、`TX_FREQ_HZ = 10.5 GHz`、`RX_FREQ_HZ = 10.38 GHz`、`SYNC_CLOCK_FREQ = 60 MHz`。

[adf4382a_manager.h:10-31](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/adf4382a_manager.h#L10-L31) 把 RX 引脚排在 GPIOG 6–10、TX 引脚排在 GPIOG 11–15，每片各有 LKDET/DELADJ/DELSTR/CE/CS 五根控制线。

**共享 SPI4 + 软件片选**——这是「一条总线挂两片」的关键：

[adf4382a_manager.c:56-63](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/adf4382a_manager.c#L56-L63) `spi_tx_extra` 与 `spi_rx_extra` 都把 `hspi` 指向 `&hspi4`，差别只在 `cs_port`/`cs_pin`（`TX_CS_Pin` vs `RX_CS_Pin`）。`platform_ops = &stm32_spi_ops` 把 no-OS 的 SPI 调用翻译成 HAL 调用——这正是 [u7-l1](u7-l1-stm32-main-and-peripherals.md) 讲过的 no-OS 抽象层在时钟子系统里的应用。

**TX/RX 参数差异**——两片工作点不同：

[adf4382a_manager.c:87-113](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/adf4382a_manager.c#L87-L113) TX `freq=TX_FREQ_HZ`、`cp_i=3`、`bleed_word=1000`；RX `freq=RX_FREQ_HZ`、`cp_i=4`、`bleed_word=1200`；两者 `ref_freq_hz=REF_FREQ_HZ`、`ref_div=1`、`id=ID_ADF4382A` 一致。

**Bug #1 修复**——`initialized` 必须在 sync 配置前置位：

[adf4382a_manager.c:187-192](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/adf4382a_manager.c#L187-L192) 注释明写：以前把 `initialized` 放在 sync 调用之后，导致 `SetupTimedSync`/`SetupEZSync` 检查 `initialized` 时永远是 false 而被拒（返回 -2 NOT_INIT）。现在前置位，sync 才真正配置硬件。这是一个典型的「**状态标志与硬件配置的顺序依赖**」陷阱。

**Timed Sync 配置**——两片都接到 60 MHz SYNCP/SYNCN：

[adf4382a_manager.c:228-266](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/adf4382a_manager.c#L228-L266) 对 TX 与 RX 分别调 `adf4382_set_timed_sync_setup(dev, true)`，这样两片的输出分频器会在**同一条 SYNCP/SYNCN 上升沿**上对齐——这就是「双 LO 相位对齐」的硬件实现。触发动作在 `ADF4382A_TriggerTimedSync`（[adf4382a_manager.c:306-359](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/adf4382a_manager.c#L306-L359)）里，用 `adf4382_set_sw_sync` 的「置位 → 等 10 µs → 清零」脉冲完成。

**锁定双源交叉验证**——寄存器 + GPIO 同时读：

[adf4382a_manager.c:455-508](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/adf4382a_manager.c#L455-L508) 先 SPI 读 0x58 的 `ADF4382_LOCKED_MSK`，再 `HAL_GPIO_ReadPin` 读 `TX_LKDET`/`RX_LKDET`；两者都为真才返回 `tx_locked=true`/`rx_locked=true`。若两者不一致，打 `DIAG_WARN("LO", "... LOCK DISAGREE ...")` 提示可能是引脚映射问题——这种「冗余传感 + 不一致告警」是安全关键子系统的常见做法。

**相位微调 → PWM → DELADJ**——皮秒级相位控制：

[adf4382a_manager.c:693-706](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/adf4382a_manager.c#L693-L706) TX 用 TIM3_CH2、RX 用 TIM3_CH3，`__HAL_TIM_SET_COMPARE` 设占空比后 `HAL_TIM_PWM_Start`；外部低通滤波把 PWM 平均成直流电压送进 ADF4382A 的 DELADJ 引脚。`ADF4382A_SetFinePhaseShift`（[adf4382a_manager.c:614-647](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/adf4382a_manager.c#L614-L647)）对边界情形做了处理：duty=0 时停 PWM 拉低引脚，duty=max 时停 PWM 拉高引脚，只有中间值才真正跑 PWM。

#### 4.3.4 代码实践

**实践目标**：把「AD9523 时钟树 → 双 ADF4382A LO → 120 MHz IF → DDC」这条贯穿链在源码层面走通，并验证两片 LO 的同步与锁定检测机制。

**操作步骤**：

1. 在 [adf4382a_manager.c:23-226](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/adf4382a_manager.c#L23-L226) 的 `ADF4382A_Manager_Init` 里，标出「CE 拉高 → TX init → RX init → 设功率 → 使能输出 → initialized=true → Timed Sync」的顺序。
2. 用计算器验证 `TX_FREQ_HZ - RX_FREQ_HZ = 120,000,000`，并回忆 [u4-l1](u4-l1-ddc-digital-downconversion.md) 的 `FTW = 0.3 × 2³²` 对应 0.3 × 400 MHz = 120 MHz——两讲在此处对齐。
3. 阅读 [adf4382a_manager.c:455-508](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/adf4382a_manager.c#L455-L508)，列出 `ADF4382A_CheckLockStatus` 用到的两个信息源（SPI 寄存器 0x58、GPIO LKDET 引脚）。
4. 在 main.cpp 中搜索 `ADF4382A_Manager_Init(&lo_manager, SYNC_METHOD_TIMED)`，确认本板用的是 Timed Sync（硬件 60 MHz）而非 EZSync。

**需要观察的现象**：`ADF4382A_Manager_Init` 内部对两片芯片分别调用 `adf4382_init`，每次都会经历 [adf4382.c:1989-2103](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/adf4382.c#L1989-L2103) 的完整初始化（软复位 → scratchpad → 默认表 → set_freq → 查锁定）。若 TX 失败，函数会关掉两片 CE 并返回 `ADF4382A_MANAGER_ERROR_SPI`，不会继续初始化 RX。

**预期结果**：`ADF4382A_Manager_Init` 返回 `ADF4382A_MANAGER_OK (0)`；之后 `ADF4382A_CheckLockStatus` 同时返回 `tx_locked=true`、`rx_locked=true`。

**待本地验证**：实板上可在 `ADF4382A_CheckLockStatus` 之后打印 `tx_status`/`rx_status` 的原始 0x58 字节值，确认 `ADF4382_LOCKED_MSK` 位为 1；若寄存器与 GPIO 不一致，优先怀疑 LKDET 引脚映射而非芯片未锁。

#### 4.3.5 小练习与答案

**练习 1**：为什么 TX LO 与 RX LO 必须用 Timed Sync（共享 60 MHz SYNCP/SYNCN）而不是各自独立运行？

**答案**：两片 ADF4382A 即使参考同为 300 MHz、频率都正确，上电后各自的输出分频器起始相位是随机的，TX 与 RX 之间的相位差不确定。Timed Sync 让两片的分频器在同一条 SYNCP/SYNCN 上升沿上复位，确立确定的相位关系，发射与接收才能相参——这正是「相参雷达」里「相参」二字的硬件根基。

**练习 2**：`ADF4382A_CheckLockStatus` 为什么要同时读 SPI 寄存器和 GPIO 引脚，并在两者不一致时告警？

**答案**：两路独立指示互为冗余。SPI 读反映芯片内部锁相环状态，GPIO LKDET 是芯片硬件输出的锁定电平；正常情况两者一致。若不一致，最可能的原因是 PCB 引脚映射或 CubeMX 配置错误（信号接错了脚），而不是芯片真的没锁——此时只信一路会误判。交叉验证把「软件可见状态」与「硬件物理电平」对齐，是排障时定位「是固件问题还是硬件问题」的第一手依据。

**练习 3**：`adf4382a_manager.c` 注释里提到的 Bug #1 是什么？它属于哪一类错误？

**答案**：Bug #1 是「`initialized` 标志在 sync 调用之后才置位，导致 sync 函数的入口保护检查 `if (!manager->initialized)` 永远成立、sync 被拒」。它属于**状态机顺序依赖错误**——一个布尔标志的赋值时机错误地让一段本应执行的硬件配置代码被静默跳过，且因为功能「看起来能初始化」而难以从输出行为直接发现。修复方式是把 `initialized = true` 前移到 sync 配置之前。

## 5. 综合实践

把本讲三个最小模块串成一个端到端的小任务：**手工还原 AERIS-10 的完整时钟分配树，并标注每一路的「来源—频率—去向—配置总线」**。

**任务步骤**：

1. 画一棵树，根节点是「100 MHz VCXO」。
2. 第一级写「AD9523-1：PLL2 N=36 → 3.6 GHz VCO」（依据 [main.cpp:1078-1092](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L1078-L1092)）。
3. 第二级把 OUT0/1/4/5/6/7/8/9/10/11 十路画出来，每路标分频比与频率（依据 [main.cpp:1116-1174](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_3_C_Cpp_Code/main.cpp#L1116-L1174)）。
4. 第三级在 OUT0（300 MHz）下挂两片 ADF4382A：TX → 10.5 GHz、RX → 10.38 GHz；在 OUT8/9（60 MHz）下画一根虚线连到两片 ADF4382A 的 SYNCP/SYNCN（依据 [adf4382a_manager.c:87-113](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/adf4382a_manager.c#L87-L113) 与 [adf4382a_manager.h:34-37](https://github.com/NawfalMotii79/PLFM_RADAR/blob/749bd0f86a07a28a86347d8ce9c141e601057c40/9_Firmware/9_1_Microcontroller/9_1_1_C_Cpp_Libraries/adf4382a_manager.h#L34-L37)）。
5. 在树旁列表回答「STM32 用什么总线配置谁」：
   - AD9523-1：**SPI**（经 no-OS `stm32_spi_ops`，HAL 句柄 `hspi4`，CS 在 GPIOF）。
   - ADF4382A TX/RX：**SPI4（`hspi4`）共享，软件 CS** `TX_CS_Pin`/`RX_CS_Pin`（GPIOG 14/10）。
   - 两片 LO 的 CE/DELADJ/DELSTR/LKDET：**GPIO** + TIM3 PWM（相位微调用）。
6. 在树的最末端画一个气泡：「TX 10.5 GHz − RX 10.38 GHz = 120 MHz IF → 被 [u4-l1](u4-l1-ddc-digital-downconversion.md) 的 DDC（FTW=0.3×2³²）搬移到基带」。

**自检问题**（答得出才算过关）：

- 如果 AD9523 的 PLL2 没锁定（`STAT_PLL2_LD` 缺失），`configure_ad9523` 返回什么？main 会继续初始化 LO 吗？
- 如果只把 OUT0 的 300 MHz 改成 150 MHz（分频比 24），TX/RX LO 还能各自合成 10.5/10.38 GHz 吗？N_INT 会变成多少？（提示：PFD 会变，分数分频参数全部重算。）
- Timed Sync 用到的 60 MHz 从哪条 AD9523 输出来？如果这路没启用，`ADF4382A_TriggerTimedSync` 还能真正对齐两片 LO 吗？

## 6. 本讲小结

- AERIS-10 的**时钟树根**是一颗 100 MHz VCXO，经 **AD9523-1** 的 PLL2（N=36）倍频到 3.6 GHz VCO，再由 14 路可编程分频器分发成 300 MHz（LO 参考）、400 MHz（ADC）、120 MHz（DAC）、100 MHz（FPGA 系统）、60 MHz（SYNC）、20 MHz（测试）等同源时钟——同源是相参的硬件前提。
- `ad9523.c` 的配置套路是「**写保持寄存器 → IO_UPDATE → VCO 校准 → 查 PLL2 锁定 → SYNC 对齐**」；真正的通道频率表不在驱动里，而在 `main.cpp` 的 `configure_ad9523()` 中。
- 10.5 GHz 本振由宽带 RF 频率合成器 **ADF4382A** 产生，`adf4382.c` 负责「目标频率 → 分数分频 N/FRAC1/FRAC2 → 按序写寄存器（N_INT 最后写触发自校准）→ 查锁定」。
- 项目自写的 `adf4382a_manager.c` 把两片 ADF4382A（TX 10.5 GHz / RX 10.38 GHz）挂在**共享 SPI4 + 软件 CS**上，用 **60 MHz Timed Sync** 对齐两片相位，并用 TIM3 PWM 驱动 DELADJ 做皮秒级相位微调。
- **TX − RX = 120 MHz IF**，精确对应 [u4-l1](u4-l1-ddc-digital-downconversion.md) DDC 的 NCO 调谐字 `0x4CCCCCCD`（0.3 × 2³²）——本板「模拟频差」与「数字搬移」在此咬合。
- 时钟子系统是**安全关键**：AD9523 未锁则整板停机（`ERROR_AD9523_CLOCK`），LO 锁定用「SPI 寄存器 + GPIO LKDET」双源交叉验证；`adf4382a_manager.c` 的 Bug #1 修复说明状态标志的赋值时机会静默影响硬件配置。

## 7. 下一步学习建议

- 继续 STM32 外设线，进入 **[u7-l3 ADAR1000 波束赋形与 Idq 校准](u7-l3-adar1000-beamforming.md)**：看相移器如何用同一套 SPI 抽象（`stm32_spi_ops`）被 STM32 配置，以及 PA 偏置的闭环校准如何依赖本讲建立的稳定时钟与电源。
- 想看时钟如何被 FPGA 消费，回到 **[u3-l2 时钟域、复位同步与 CDC 基础](u3-l2-clock-domains-and-cdc.md)**：AD9523 输出的 100/120/400 MHz 在 FPGA 侧如何经 BUFG/MMCM 缓冲与跨域。
- 想理解 120 MHz IF 的去向，复习 **[u4-l1 数字下变频 DDC](u4-l1-ddc-digital-downconversion.md)**：NCO 调谐字与本讲 LO 频差的对应关系。
- 若关心板级验证，预习 **[u10-l2 硬件 Bring-up 流程](u10-l2-board-bringup.md)**：bring-up 检查清单里「时钟锁定」是上电第一阶段必过项。
