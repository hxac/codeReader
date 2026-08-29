# u2-l3 tlv320aic3204 编解码器与 I2S DMA 采集

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 NanoVNA 为什么用一颗**音频编解码器**（tlv320aic3204）充当射频测量的接收机，以及它的两个 ADC 通道如何对应「参考信号」与「被测信号（反射/传输）」。
2. 读懂 codec 的 I2C 寄存器初始化序列：8MHz 基准时钟如何经 PLL 变成 48kHz 采样率，左声道为何固定、右声道如何切换。
3. 解释 I2S + DMA 双缓冲如何把 48kHz 立体声样本源源不断搬进 `rx_buffer`，`i2s_end_callback` 在什么时机被触发、在中断上下文里做了什么。
4. 掌握 `dsp_start` / `dsp_wait` 这对「先丢再测」同步协议：`wait_count` 丢弃暂态缓冲、`accumerate_count` 相干累积，以及 5 档带宽（1kHz~10Hz）与累积次数的数学关系。
5. 学会用 `git log -S` 考古一段可疑注释，用 Python 在 PC 上复现固件的状态机——不需要硬件也能验证时序结论。

## 2. 前置知识

- **codec（编解码器）**：一颗同时包含 ADC/DAC 的音频芯片。本讲只用到它的 ADC：把模拟电压变成 16bit 数字样本。NanoVNA 把射频测量外差到 5kHz 音频中频（u2-l1 已讲），于是「射频接收机」退化成「音频采集卡」，一颗几元钱的音频 codec 就够用。
- **I2S 总线**：数字音频常用的三线总线：`BCLK`（位时钟）、`WCLK`/`WS`（字选择，左右声道切换）、`SD`（串行数据）。立体声按「左声道 16bit、右声道 16bit」交替传输。关键是：**总线上必须有一方当主机输出时钟**。NanoVNA 里 codec 是主机（它输出 BCLK/WCLK），STM32 只当从机接收。
- **DMA 与双缓冲（ping-pong）**：DMA 是不打扰 CPU 的数据搬运工。把一个缓冲区分成两半，DMA 填后半时 CPU 处理前半，反过来亦然——这就是「乒乓缓冲」。每个半区填满时 DMA 触发一次中断，是天然的数据节拍器。
- **中断上下文与 `volatile`**：回调函数运行在中断里，不能调用会阻塞的 RTOS 服务；而被主线程和中断「同时访问」的变量（如 `accumerate_count`）必须加 `volatile`，禁止编译器把它缓存进寄存器。
- **相干累积与分辨率带宽（RBW）**：对一个周期信号按其周期的整数倍时长求和，信号幅度线性增长，而噪声随机、只按 \(\sqrt{N}\) 增长，所以信噪比按 \(\sqrt{N}\) 改善。累积时长 \(T\) 对应的等效带宽约为 \(\mathrm{RBW} \approx 1/T\)——「以时间换精度」是本讲反复出现的主题。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [tlv320aic3204.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/tlv320aic3204.c) | codec 驱动：初始化寄存器表、通道切换、增益设置（经 I2C，地址 0x18） |
| [main.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c) | 本讲主角所在的宿主：`rx_buffer`、`i2sconfig`、`i2s_end_callback`、`dsp_start/dsp_wait`、`sweep()` 中的使用点，以及 `bandwidth/port/stat/gain` 四个 shell 命令 |
| [nanovna.h](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h) | `AUDIO_BUFFER_LEN`、`SAMPLE_LEN` 等与采样强耦合的常量，codec 函数原型 |
| [dsp.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/dsp.c) | `dsp_process`（回调里被调用的正交累积）与 `reset_dsp_accumerator`，本讲只看接口约定，算法细节留给 u2-l4 |
| [mcuconf.h](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/mcuconf.h) / [halconf.h](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/halconf.h) | ChibiOS 配置：启用 I2S、选择 SPI2 从模式接收 |
| [si5351.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.c) | CLK2 固定输出 8MHz 作为 codec 基准时钟；`si5351_set_frequency` 返回值就是「要丢几个缓冲」 |
| [ui.c](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c) | 屏幕菜单方式设置带宽（与 shell 的 `bandwidth` 命令殊途同归） |
| [NANOVNA_STM32_F072/board.h](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/NANOVNA_STM32_F072/board.h) | I2S2 引脚分配（PB12/PB13/PB15） |

> 提示：ChibiOS 的 I2S 驱动源码在 git 子模块里（本仓库 HEAD 不含其内容，目录当前为空），涉及驱动内部行为处本讲会用固件侧代码约束来推证，并明确标注。

## 4. 核心概念与源码讲解

本讲的三个最小模块：

1. **tlv320aic3204_init 编解码器初始化**（含通道切换 `tlv320aic3204_select`）
2. **I2SConfig 与 i2s_end_callback**（DMA 双缓冲采集通路）
3. **dsp_start / dsp_wait 同步**（「先丢再测」协议与带宽档位）

### 4.1 模块一：tlv320aic3204_init —— 用音频 codec 当射频接收机

#### 4.1.1 概念说明

u2-l1 讲过 NanoVNA 的外差架构：激励信号与本振恒差 5kHz，电桥/被测件输出的射频经混频后落在 5kHz 音频中频上。于是固件需要的是一个「双通道、16bit、几十 kHz 采样率」的采集器——这正好是一颗立体声音频 codec 的本职工作。

tlv320aic3204 是 TI 的低功耗立体声 codec，NanoVNA 只用它的 ADC 部分。它内部有两路差分麦克风输入通路（MicPGA），固件把它们分配为：

- **左声道（固定不变）**：接电桥的**参考信号**（激励经耦合取样）——这是后续 `calculate_gamma` 里做复数除法的分母；
- **右声道（可切换）**：接**被测信号**，通过改写 codec 的输入复用寄存器，在「反射通道（CH0）」与「传输通道（CH1）」之间二选一。

为什么右声道要切换而不是再用第三路 ADC？因为一颗 codec 只有左右两路 ADC；而参考信号必须全程在场（每个频点都要用它归一化），所以让固定的一路当参考，剩下的一路时分复用。这也解释了 `sweep()` 里「测 CH0 → 切通道 → 测 CH1」的顺序（4.3 节）。

codec 的配置全部通过 I2C 写入（地址 `0x18`），它的寄存器按「页」组织：先写寄存器 `0x00` 切页，再操作该页内的寄存器。

#### 4.1.2 核心流程

初始化与切换的流程（伪代码）：

```text
tlv320aic3204_init():
    对 conf_data 中每对 (reg, value) 逐对 I2C 写入   # 复位/PLL/时钟树/模拟上电/左声道路由
    等待 40ms                                        # 模拟基准充电、滤波器稳定
    对 conf_data_unmute 逐对写入                      # ADC 上电、解除数字静音

tlv320aic3204_select(channel):
    channel == 0 → 写 conf_data_ch3_select           # 右声道 ← IN3（反射/CH0）
    channel == 1 → 写 conf_data_ch1_select           # 右声道 ← IN1（传输/CH1）
```

时钟树是理解采样率的关键一环。codec 的基准时钟 MCLK 不是晶振，而是 **si5351 的 CLK2 固定输出的 8MHz**（射频信号源与本节采集共享同一颗时钟芯片，全系统时钟同源）：

\[ f_{\text{PLL}} = 8\,\text{MHz} \times 10.7520 = 86.016\,\text{MHz}, \qquad f_s = \frac{f_{\text{PLL}}}{N_{\text{DAC}} \times M_{\text{DAC}} \times \text{OSR}} = \frac{86.016\,\text{MHz}}{2 \times 7 \times 128} = 48\,\text{kHz} \]

#### 4.1.3 源码精读

**（1）地址与基准时钟宏**

[tlv320aic3204.c:23-26](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/tlv320aic3204.c#L23-L26)：`REFCLK_8000KHZ` 宏打开 8MHz 基准的 PLL 配置分支，`AIC3204_ADDR 0x18` 是 codec 的 I2C 7 位地址。8MHz 从哪来？[si5351.c:31](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.c#L31) 定义 `CLK2_FREQUENCY 8000000U`——si5351 的第三路输出固定 8MHz 送给 codec 当 MCLK。

**（2）主初始化表 `conf_data`：PLL 与时钟树**

[tlv320aic3204.c:28-55](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/tlv320aic3204.c#L28-L55)：这是一张 `(寄存器, 值)` 对的压缩表。关键几行：

- 第 33-39 行（`0x05~0x08`）：上电 PLL 并设 P=1、R=1、J=10、D=7520，注释直接给出算式 `8.000MHz*10.7520 = 86.016MHz, 86.016MHz/(2*7*128) = 48kHz`——采样率 48kHz 的出处就在这里，不是猜的。
- 第 42-43 行（`0x0b=0x82`、`0x0c=0x87`）：NDAC=2、MDAC=7 分频；第 44-45 行 DAC OSR=128。
- 第 52-55 行：NADC=2、MADC=7、ADC OSR=128、ADC 处理块选 PRB_R1——ADC 与 DAC 同为 48kHz。
- **第 48 行（`0x1b, 0x0c`）：把 BCLK、WCLK 设为输出**——codec 自己驱动 I2S 时钟线，也就是codec 当 I2S **主机**。与之呼应的是 [mcuconf.h:135-140](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/mcuconf.h#L135-L140)：STM32 侧启用 `STM32_I2S_USE_SPI2`，模式为 `SLAVE | RX`（从机、只收）。第 49 行 `0x1e = 0x80+28` 使能 BCLKN=28 分频，\( 86.016\,\text{MHz}/28 = 3.072\,\text{MHz} = 64 \times f_s \)，即每个立体声帧 64 个位时钟、与 16bit 数据的 I2S 帧兼容。

**（3）模拟路由：谁接左、谁接右**

[tlv320aic3204.c:56-73](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/tlv320aic3204.c#L56-L73)：切到 Page 1 做模拟配置。第 67-68 行（`0x34`/`0x36`）把 **IN2L→LEFT_P、IN2R→LEFT_N**：左声道差分对**固定**接参考信号源。被测信号接到右声道的哪一路，则由另外两张小表决定：

- [tlv320aic3204.c:82-87](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/tlv320aic3204.c#L82-L87) `conf_data_ch3_select`：**IN3R→RIGHT_P、IN3L→RIGHT_N**——选反射输入（CH0）；
- [tlv320aic3204.c:89-94](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/tlv320aic3204.c#L89-L94) `conf_data_ch1_select`：**IN1R→RIGHT_P、IN1L→RIGHT_N**——选传输输入（CH1）。

所谓「通道切换」只是改两个寄存器的路由位，代价是一次 3 对字节的 I2C 批量写（`sweep()` 注释估计约 60µs）。

**（4）I2C 写入与三个对外函数**

[tlv320aic3204.c:96-100](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/tlv320aic3204.c#L96-L100) `tlv320aic3204_bulk_write`：向 `0x18` 发 2 字节（寄存器号 + 数据）。[tlv320aic3204.c:115-122](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/tlv320aic3204.c#L115-L122) `tlv320aic3204_config`：持有 I2C 总线后逐对写表。

[tlv320aic3204.c:124-129](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/tlv320aic3204.c#L124-L129) `tlv320aic3204_init`：先写主表，**等 40ms**（对应表中「REF 充电时间 40ms」「MicPGA 启动延时 3.1ms」等模拟建立时间），再写 `conf_data_unmute`（[tlv320aic3204.c:75-80](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/tlv320aic3204.c#L75-L80)：寄存器 `0x51=0xc0` 左右 ADC 上电、`0x52=0x00` 解除数字音量静音）。

[tlv320aic3204.c:131-134](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/tlv320aic3204.c#L131-L134) `tlv320aic3204_select`：`channel ? conf_data_ch1_select : conf_data_ch3_select`，一行完成通道选择。[tlv320aic3204.c:136-144](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/tlv320aic3204.c#L136-L144) `tlv320aic3204_set_gain`：写 Page 1 的 `0x3b/0x3c` 分别设左右 MicPGA 增益（shell 命令 `gain` 提示范围 0-95，对应 datasheet 的 dB 表）。

**（5）shell 暴露的三个人工干预入口**

[main.c:1954-1963](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1954-L1963) `cmd_port`：`port {0:TX 1:RX}` 手动切右声道路由（0 对应反射测量口，即 `sweep()` 注释里的 `CH0:REFLECT`；1 对应传输口）。[main.c:1940-1952](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1940-L1952) `cmd_gain`：设 PGA 增益。两者都注册在命令表里（[main.c:2170-2173](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2170-L2173)），不需要 `CMD_WAIT_MUTEX`（直接在 shell 线程写 I2C，不碰测量数据）。

#### 4.1.4 代码实践：用 shell 命令亲手摸一摸 codec（真机）

1. **实践目标**：验证「右声道路由切换」与「PGA 增益」确实改变采集到的模拟信号。
2. **操作步骤**：
   - USB 连接 NanoVNA，打开串口终端（115200，见 u5-l1 的连接方式），输入 `pause` 暂停扫频，避免干扰观察；
   - 输入 `stat`，记录输出的 `average` 与 `rms` 两行（该命令对当前 `rx_buffer` 求均值/RMS，见 [main.c:1982-2016](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1982-L2016)）；
   - 输入 `gain 40`（把两个声道 PGA 都设为较大增益），再 `stat`；
   - 输入 `port 0` 与 `port 1` 各一次，每次之后 `stat`。
3. **需要观察的现象**：增益提高后 `rms` 明显变大；`port 0/1` 切换后 `rms` 也变化（两口的开路/接负载状态不同，被测通道信号幅度不同）；`average` 接近 0（信号无直流分量）。
4. **预期结果**：`rms` 随增益单调变化即证明链路 `模拟输入 → PGA → ADC → rx_buffer → stat` 全程畅通。注意 `i2sStartExchange` 启动后 DMA 永不休眠（4.2 节），所以暂停扫频时 `stat` 依旧有新鲜数据，且 `callback count` 每次执行都会明显增大。
5. 无真机时改为源码阅读实践：对照本节表格，把 `conf_data`/`conf_data_ch3_select`/`conf_data_ch1_select` 三张表里每个寄存器的作用各写成一句话，重点确认「左=固定参考、右=可切换被测」。

#### 4.1.5 小练习与答案

**练习 1**：为什么参考信号接在「固定」的左声道，而被测信号用右声道切换？反过来行不行？

**答案**：`calculate_gamma` 要用 `ref` 做复数除法的分母来归一化（消除激励幅度漂移），每个频点、每次测量都需要参考在场；而被测信号在同一时刻只需测反射或传输之一。反过来（参考切换、被测固定）会让每次测量都要先切参考并等待稳定，且两路测量无法共享同一参考样本，无任何好处。

**练习 2**：固件如何保证 codec 与 STM32 的 I2S 主从关系不冲突？给出两侧的代码证据。

**答案**：codec 侧 [tlv320aic3204.c:48](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/tlv320aic3204.c#L48)（`0x1b=0x0c` 把 BCLK/WCLK 设为输出，codec 当主机）；STM32 侧 [mcuconf.h:136-140](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/mcuconf.h#L136-L140)（`STM32_I2S_USE_SPI2 TRUE`、模式 `SLAVE | RX`）。两侧一主一从、只收不发（TX 缓冲为 NULL，见 4.2 节），因此不会驱动冲突。

**练习 3**：`tlv320aic3204_init` 中 `wait_ms(40)` 删掉可能出现什么现象？

**答案**：模拟基准（REF）充电与 MicPGA 启动需要时间（表中 `0x7b=0x01` 即「REF 充电时间 40ms」）。删掉后紧跟着的 `unmute` 与后续测量可能采到未稳定的偏置，表现为开机最初几百 ms 内 `stat` 的 `average`/`rms` 异常、扫频起点（`i == 0`）数据偏差。固件另在 `sweep()` 的 `dsp_start(delay + 1)`（4.3 节）里为首个频点多丢一个缓冲，也算一层兜底。

### 4.2 模块二：I2SConfig 与 i2s_end_callback —— DMA 双缓冲采集通路

#### 4.2.1 概念说明

codec 的 ADC 以 48kHz 连续吐出立体声样本，固件要解决的问题是如何「不打扰 CPU 地」把它们搬进内存并按节拍处理。答案就是 SPI2 外设的 I2S 从模式 + DMA 循环搬运：

- DMA 被配置成在 `rx_buffer` 上循环搬运，缓冲区分成两半。每填满一半，DMA 触发一次中断，ChibiOS I2S 驱动在**中断上下文**里调用我们注册的 `i2s_end_callback(i2sp, offset, n)`：`offset` 指明刚填满的是哪一半（0 或半区长度），`n` 是本半区的 16bit 样本数。
- 于是形成了 1ms 级的「数据节拍器」：CPU 在两拍之间干别的活（算上一个缓冲、切频率、处理 UI），每拍被中断唤醒一次处理刚到的半区。
- 这也带来硬性约束：**回调必须快**。它运行在中断里，处理时间超过一个半区的填充时间就会丢数据。

一个必须澄清的数字问题：`AUDIO_BUFFER_LEN` 的注释写着 "5ms @ 48kHz"，但 \( 48000 \times 0.005 = 240 \) 个半字，和 96 对不上。这不是你算错，而是**注释过期**——4.2.4 的实践会带你去 git 历史里找到真相。

#### 4.2.2 核心流程

从射频到内存的完整数据流：

```text
si5351 CLK0/CLK1(激励+本振, 恒差5kHz)      si5351 CLK2 = 8MHz
        │ 混频出 5kHz 中频                      │ MCLK
        ▼                                       ▼
   电桥/被测件 ──► codec MicPGA ──► ADC(48kHz,16bit,立体声)
                    左=IN2(参考,固定)  右=IN1/IN3(被测,可切换)
                        │ I2S: BCLK/WCLK(codec输出) + SD
                        ▼
              STM32 SPI2 从模式 RX (PB15 数据, PB12/PB13 时钟)
                        │ DMA 循环搬运 192 个半字
                        ▼
              rx_buffer[192]（两个半区各 96 半字 = 48 立体声帧）
                        │ 每填满一个半区 → 中断
                        ▼
        i2s_end_callback(offset, 96) ──► dsp_process(&rx_buffer[offset], 96)
                                          （仅当状态机允许，见 4.3）
```

每个半区 96 个 16bit 半字 = 48 个立体声帧，在 48kHz 下正好是 **1ms** 的音频。这个 1ms 就是全固件测量时序的基本节拍。

#### 4.2.3 源码精读

**（1）缓冲区与常量**

[main.c:594](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L594)：`int16_t rx_buffer[AUDIO_BUFFER_LEN * 2];`——总长 192 个半字，即两个 96 半字的半区。[nanovna.h:106-112](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L106-L112)：`AUDIO_BUFFER_LEN 96`（注释 "5ms @ 48kHz"，下节实践证伪）、`SAMPLE_LEN 48`（每次 `dsp_process` 处理的立体声帧数，与 `sincos_tbl[48]` 严格配套）。顺带一提：同处的 `STATE_LEN 32` 在当前代码里只有定义、没有使用（可用 Grep 验证），属于历史遗留。

**（2）I2S 驱动配置**

[main.c:672-680](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L672-L680)：`I2SConfig` 的六个字段——TX 缓冲 `NULL`（只收不发）、RX 缓冲 `rx_buffer`、尺寸 `AUDIO_BUFFER_LEN * 2`（=192，按 16bit 样本计）、TX 回调 `NULL`、RX 回调 `i2s_end_callback`、以及两个 SPI 寄存器初值（`i2scfgr=0` 用驱动默认数据格式；`i2spr=2` 是预分频，从模式下外部时钟不受它影响）。

启动序列在 `main()` 里（[main.c:2417-2424](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L2417-L2424)）：先 `tlv320aic3204_init()` 让 codec 开始输出 I2S 时钟，再 `i2sInit/i2sObjectInit/i2sStart(&I2SD2, &i2sconfig)`，最后 `i2sStartExchange` 启动收发。此后 **DMA 永不停歇**，回调每 1ms 一次，与扫频是否进行无关（空闲时回调里什么事都不做，只累计统计）。引脚分配见 [NANOVNA_STM32_F072/board.h:225-227](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/NANOVNA_STM32_F072/board.h#L225-L227)：PB12=WCLK、PB13=BCLK、PB15=数据线（复用功能 0）。

**（3）回调本体**

[main.c:641-670](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L641-L670) `i2s_end_callback`，全部逻辑只有十几行：

- 第 647 行：`int16_t *p = &rx_buffer[offset];`——`offset` 由驱动给出，指向刚填满的半区首地址；
- 第 651-661 行：三态状态机（详见 4.3）：`wait_count > 1` 只递减（丢弃暂态）；`wait_count == 1` 且 `accumerate_count > 0` 时调用 `dsp_process(p, n)` 并递减累积计数；两者皆空则什么都不做；
- 第 669 行：`stat.callback_count++`——给 shell 命令 `stat` 提供观测数据。

注意第 654 行的 `if (accumerate_count > 0)` 守卫不是多余的：`accumerate_count` 是 `uint8_t`，若在归零后再被 `--` 会回绕成 255，`dsp_wait()` 将永远等不到 0 而死锁。空闲期间回调持续到达，这个守卫正是防它们的。

**（4）`dsp_process` 的接口约定**

[dsp.c:49-86](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/dsp.c#L49-L86)：把缓冲区当作 `uint32_t` 数组逐字读取（第 52-53 行 `len = length / 2`——`length` 是 16bit 半字数，`len` 是立体声帧数），第 61-63 行约定**低 16 位是参考通道、高 16 位是被测通道**。算法（正交混频累积）是 u2-l4 的内容；这里只需确认两点：`len` 必须不超过 48（否则 `sincos_tbl[i]` 越界），以及它只做乘加和累加、不调用任何 RTOS 服务——这正是一个可以被中断安全调用的函数该有的样子。

**（5）推证：每个回调到底搬运多少？**

ChibiOS I2S 驱动源码在子模块中（本仓库 HEAD 不含），但回调粒度可以从固件自身约束推出来：若一次回调传入整个 192 半字，则 `dsp_process` 的 `len = 96 > 48`，`sincos_tbl[48]` 必然越界——所以驱动按**半区**触发回调，每次 `n = 192/2 = 96` 半字、`offset ∈ {0, 96}`。再结合带宽档位标签的反推（4.3.5 练习 3），可确认半区时长恰为 1ms。（驱动内部行为待本地 `git submodule update` 检出后进一步核对。）

#### 4.2.4 代码实践：证伪一条过期注释（PC，git + Python）

1. **实践目标**：亲手验证 `AUDIO_BUFFER_LEN` 的注释 "5ms @ 48kHz" 与当前值 96 不符，并用 git 历史找出它曾经正确的那一刻。
2. **操作步骤**：
   - 在仓库根目录执行 `git show 59020b8:nanovna.h | grep -B1 -A1 AUDIO_BUFFER`（`59020b8` 是 initial commit），再执行 `git log --oneline -S "AUDIO_BUFFER_LEN 96" -- nanovna.h` 找到改值的提交；
   - 编写并运行下面这段 Python（**示例代码**，非项目原有文件）：

     ```python
     FS = 48000                                # tlv320aic3204.c 注释: 86.016MHz/(2*7*128)
     print("48kHz 下 5ms 对应", int(FS * 0.005), "个 16bit 样本(半字),",
           "即", int(FS * 0.005) // 2, "个立体声帧")
     print("当前 AUDIO_BUFFER_LEN = 96 半字 = 48 帧, 对应", 96 / 2 / FS * 1000, "ms")
     print("初始提交的 AUDIO_BUFFER_LEN = 480 半字 = 240 帧, 对应", 480 / 2 / FS * 1000, "ms")
     ```

3. **需要观察的现象**：git 显示初始提交中该宏为 `480`，而 2016-09-30 的提交 `b2e3fe7`（"set sweep on draw, adjust grid"）把它改成 96 且未更新注释。
4. **预期结果**（按上述逻辑手工推演，脚本可本地运行复核）：输出依次为 `240 个半字(120 帧)`、`1.0 ms`、`5.0 ms`——480 半字才等于 5ms，今天的 96 半字只等于 1ms。结论：**注释描述的是历史值，当前每个半区（也是每次回调）的时长是 1ms**。
5. 这个练习的方法论比结论更重要：读嵌入式代码时，凡「常量 × 注释」声称的时序关系，都值得用采样率倒推一遍；不准时，`git log -S` 常能告诉你演变过程。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `rx_buffer` 要做成两个半区的双缓冲，而不是一个单独的缓冲？

**答案**：DMA 循环模式配合「半传输/全传输」两次中断，天然形成乒乓：DMA 填后半区时，CPU/中断处理前半区；反之亦然。只要单次处理时间短于半区时长（1ms），数据流就无缝衔接。若只有一个缓冲，处理期间新样本会覆盖还没读走的旧样本。

**练习 2**：`i2s_end_callback` 里能否调用 `chThdSleepMilliseconds` 或 `shell_printf`？为什么？

**答案**：不能。回调运行在中断上下文（DMA 中断），ChibiOS 的睡眠类调用会让中断服务例程挂起、直接破坏调度；`shell_printf` 走 USB 串口，涉及阻塞等待与大量格式化，时长不可控，会错过下一个半区。所以回调里只放 `dsp_process` 这种纯计算、时长有界的操作，慢活留给 `sweep()` 线程在 `dsp_wait()` 之外做。

**练习 3**：`stat` 命令（[main.c:1982-2016](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1982-L2016)）读的 `rx_buffer` 正在被 DMA 持续覆盖，读出的均值/RMS 为什么还算可信？

**答案**：统计量对 192 个半字求和平均，DMA 覆盖是渐进的，读的过程中最多有一半数据被新旧样本混合；对均值/RMS 这类统计量而言，新旧数据来自同一平稳信号，误差可忽略。它是诊断工具而非计量工具——真正进测量数据的路径是中断里的 `dsp_process`，两者互不干扰。

### 4.3 模块三：dsp_start / dsp_wait —— 「先丢再测」同步协议与带宽档位

#### 4.3.1 概念说明

采集链路每 1ms 送来一个缓冲，但**不是每个缓冲都值得处理**。两类暂态必须丢弃：

1. **换频后**：`set_frequency` 改的是 si5351 寄存器，正在填充/刚填满的缓冲里混的还是旧频率的信号，直接算会得到错误的 Γ；
2. **切换 codec 通道后**：右声道模拟复用器刚改路由，ADC 里残留前一个通道的样本。

所以测量协议是「先丢再测」：`dsp_start(count)` 声明要丢几个缓冲，`dsp_wait()` 睡到累积完成。两线程一中断协作：

- `sweep()` 线程（消费者）：设频率/切通道 → `dsp_start(delay)` → `dsp_wait()` 睡眠等待 → 取结果；
- I2S 中断（生产者）：每 1ms 一次回调，按 `wait_count/accumerate_count` 两个计数器决定丢弃还是累积；
- `volatile` 保证主线程写的初值对中断立即可见，中断里的递减对主线程立即可见（单字节读写在 Cortex-M0 上天然原子）。

「带宽」档位则决定**累积多少个缓冲**：相干累积 \(N\) 个 1ms 缓冲，等效噪声带宽约

\[ \mathrm{RBW} \approx \frac{1}{T_{\text{acc}}} = \frac{1}{N \times 1\,\text{ms}} \]

档位标签（1kHz/300Hz/100Hz/30Hz/10Hz）正是按这个关系标的：\(N=1 \to 1\,\text{kHz}\)、\(N=3 \to 333\,\text{Hz}\)、\(N=10 \to 100\,\text{Hz}\)、\(N=33 \to 30\,\text{Hz}\)、\(N=100 \to 10\,\text{Hz}\)。噪声底按 \(\sqrt{N}\) 下降，代价是测量时间按 \(N\) 线性上升。

#### 4.3.2 核心流程

两个计数器构成的三态状态机（每 1ms 走一格）：

```text
dsp_start(count):                          # sweep 线程调用
    wait_count       = count               # 含 1 个"起测"缓冲在内的丢弃数
    accumerate_count = bandwidth_accumerate_count[bandwidth]
    reset_dsp_accumerator()                # 4 个累积浮点器清零（dsp.c:124-131）

i2s_end_callback(offset, 96):              # 每 1ms 一次
    if wait_count > 1:   wait_count--                    # 状态1: 丢弃暂态
    elif wait_count > 0:
        if accumerate_count > 0:
            dsp_process(&rx_buffer[offset], 96)          # 状态2: 相干累积
            accumerate_count--
    # wait_count == 0:      状态3(空闲): 什么都不做       # 扫频间隙/暂停时

dsp_wait():                                # sweep 线程调用
    while (accumerate_count > 0) __WFI()   # 睡到中断把它减到 0
```

时间线（以 `dsp_start(3)`、带宽 100Hz 即 \(N=10\) 为例）：

```text
回调#:    1     2     3     4     5   ...  13
wait:     3→2   2→1   起测  1(保持起测状态 ...)
动作:     丢    丢    累积  累积  累积 ... 累积(第10次) → accumerate_count=0
                                  ↑ dsp_wait() 在此返回，sweep 线程被唤醒
```

一次测量的总回调数为 \((\text{count}-1) + N\)，其中前 `count-1` 次丢弃、后 \(N\) 次累积。

#### 4.3.3 源码精读

**（1）两个计数器与带宽表**

[main.c:601-610](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L601-L610)：`volatile uint8_t wait_count, accumerate_count`——跨线程/中断共享故加 `volatile`；`bandwidth_accumerate_count[] = {1, 3, 10, 33, 100}`，注释即五档带宽标签 1kHz/300Hz/100Hz/30Hz/10Hz。注意 300Hz 档实际 \(1/3\,\text{ms} = 333\,\text{Hz}\)、30Hz 档 \(1/33\,\text{ms} = 30.3\,\text{Hz}\)，取的是最接近的整数次数。

**（2）`dsp_start` / `dsp_wait`**

[main.c:614-620](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L614-L620) `dsp_start(count)`：一次函数调用完成「设丢弃数、按当前带宽档设累积数、清零 DSP 累积器」三件事。[main.c:622-627](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L622-L627) `dsp_wait()`：`while (accumerate_count > 0) __WFI();`——`__WFI`（Wait For Interrupt）让 CPU 睡到**任意**中断到来；这里的中断源正是每 1ms 一次的 I2S DMA 中断（也可能被其它中断唤醒，那就多查一次条件再睡）。忙等 + 睡眠的组合：逻辑上是轮询，功耗上是休眠。

**（3）`sweep()` 中的使用：一次频点的完整编排**

[main.c:854-882](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L854-L882)：`DELAY_CHANNEL_CHANGE` 定义为 2（切 codec 通道至少丢 1 个缓冲）。每个频点：

- 第 865 行：`delay = set_frequency(frequencies[i]);`——si5351 返回需要丢的缓冲数。返回值定义在 [si5351.c:43-52](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.c#L43-L52)：普通变频 `DELAY_NORMAL=2`、band 1 内 `DELAY_BAND_1=3`、换带（要复位 PLL）`DELAY_BANDCHANGE_1/2=3`，注释明言「最小值是 2，变频在下一次测量才生效，必须跳过」；
- 第 866 行：`tlv320aic3204_select(0);` 切到反射通道；
- 第 867 行：`dsp_start(delay + ((i == 0) ? 1 : 0));`——首个频点多丢一个（LED 刚灭需要电源稳定，同时把时间起点对齐）；第 869-870 行注释「Place some code thats need execute while delay」是个提示：丢弃窗口内的 CPU 时间是白送的，可以插活；
- 第 871-873 行：`dsp_wait()` 后 `(*sample_func)(measured[0][i]);` 取 Γ（`sample_func` 函数指针默认 `calculate_gamma`，[main.c:764](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L764)）；
- 第 875-882 行：`select(1)` → `dsp_start(DELAY_CHANNEL_CHANGE)` → `dsp_wait()` → `measured[1][i]`，同样套路测传输通道。

**（4）带宽档位的三个入口**

- shell：[main.c:1965-1980](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/main.c#L1965-L1980) `cmd_bandwidth`，把 `"1000|300|100|30|10"` 解析成索引 0-4 存入 `bandwidth`；
- 屏幕菜单：[ui.c:941-946](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L941-L946) `menu_bandwidth[]` 五个菜单项共用回调 [ui.c:635-637](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/ui.c#L635-L637)（`bandwidth = item;`）；
- 持久化：`bandwidth` 是 `current_props._bandwidth` 的别名宏（[nanovna.h:381-382, 409](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/nanovna.h#L381-L382)），随校准槽一起 `caldata_save`（u3-l4 详述）。

关键在于：改 `bandwidth` 不需要通知任何人——下一次 `dsp_start` 自然查表取新值，测量循环自动适配。

#### 4.3.4 代码实践：用 Python 复现回调状态机（PC，本讲主实践）

1. **实践目标**：把 `i2s_end_callback` 的状态机逐行翻译成 Python，回答规格问题——「设置 bandwidth=4（10Hz）时，一次 `dsp_wait` 需要经历多少次回调」，并用五档带宽的输出反证「每回调 1ms」。
2. **操作步骤**：保存以下脚本为 `u2l3_callback_sim.py` 并运行（**示例代码**，非项目原有文件）：

   ```python
   #!/usr/bin/env python3
   # 模拟 main.c:601-670 的 wait_count/accumerate_count 状态机
   BANDWIDTH_ACCUMERATE_COUNT = [1, 3, 10, 33, 100]   # main.c:604-610
   BW_LABELS   = ["1kHz", "300Hz", "100Hz", "30Hz", "10Hz"]
   CB_PERIOD_MS = 96 / 2 / 48000 * 1000               # 半区96半字=48帧 @48kHz

   def simulate(delay, bw_idx):
       wait_count = delay
       accumerate = BANDWIDTH_ACCUMERATE_COUNT[bw_idx]
       discard = accumulate = 0
       while accumerate > 0:                # dsp_wait() 的循环条件
           if wait_count > 1:               # main.c:651-653
               wait_count -= 1; discard += 1
           elif wait_count > 0:             # main.c:653-657
               if accumerate > 0:
                   accumerate -= 1; accumulate += 1   # dsp_process 后递减
           else:
               break                        # 空闲回调（实际固件不会停在循环里）
       return discard, accumulate

   for bw, label in enumerate(BW_LABELS):
       d, a = simulate(2, bw)               # delay=2: DELAY_CHANNEL_CHANGE
       t = a * CB_PERIOD_MS
       print(f"bandwidth={bw} ({label:>5}): 回调 {d+a:>3} 次"
             f"（丢 {d} + 累 {a}），累计 {t:5.0f} ms，"
             f"等效 RBW≈{1000/t:5.0f} Hz")
   ```

3. **需要观察的现象**：输出的「等效 RBW」一列与标签一列的对应关系。
4. **预期结果**（状态机为确定性算术，以下为手工推演，可本地运行复核）：

   ```text
   bandwidth=0 ( 1kHz): 回调   2 次（丢 1 + 累   1），累计     1 ms，等效 RBW≈ 1000 Hz
   bandwidth=1 (300Hz): 回调   4 次（丢 1 + 累   3），累计     3 ms，等效 RBW≈  333 Hz
   bandwidth=2 (100Hz): 回调  11 次（丢 1 + 累  10），累计    10 ms，等效 RBW≈  100 Hz
   bandwidth=3 ( 30Hz): 回调  34 次（丢 1 + 累  33），累计    33 ms，等效 RBW≈   30 Hz
   bandwidth=4 ( 10Hz): 回调 101 次（丢 1 + 累 100），累计   100 ms，等效 RBW≈   10 Hz
   ```

   **答案即问题所求：bandwidth=4 时一次 `dsp_wait` 经历 101 次回调（1 次丢弃 + 100 次累积），约 101ms**——与 `bandwidth_accumerate_count` 表的 100 互相印证。同时五档「等效 RBW」都落在标签附近，反证了 4.2 节「每回调 1ms」的结论（若按过期注释的 5ms/回调计算，得到的 RBW 会系统性偏小 5 倍、与标签全面不符）。
5. 有真机可加一步：`bandwidth 10` 后执行 `scan 1000000 900000000 101`，用秒表对比 `bandwidth 1000` 时的耗时差异，量级应与下表估算一致（见综合实践）。

#### 4.3.5 小练习与答案

**练习 1**：`wait_count` 的语义是「丢弃 count-1 个、第 count 个起测」。为什么 `set_frequency` 的返回值最小是 2 而不是 1？

**答案**：I2C 写 si5351 寄存器完成的瞬间，codec 正在填充的半区里已是旧频率信号，而「刚填满、即将被回调处理」的那个半区同样诞生于变频之前——至少要跨过这两个缓冲，新频率的稳态信号才完整占满一个半区。[si5351.c:43](https://github.com/ttrftech/NanoVNA/blob/d02db797a7032822137882f8f8a6c4ec21f064cd/si5351.c#L43) 的注释原文即「最小值是 2，变频在下一次测量才生效，需要跳过它」。

**练习 2**：若把 `i2s_end_callback` 中第 654 行的 `if (accumerate_count > 0)` 守卫删掉，会发生什么？

**答案**：空闲或两次测量之间，回调仍每 1ms 到达一次，此时 `wait_count` 可能仍为 1（上轮 `dsp_start` 后未清零），`accumerate_count--` 会在 0 上回绕成 255（`uint8_t` 下溢）。随后 `dsp_wait()` 要等它减回 0，需再等 255ms；更糟的情况下与新一轮 `dsp_start` 交错，测量时间完全错乱。守卫保证了「累积阶段一旦结束，状态机对多余回调免疫」。

**练习 3**：只用 `bandwidth_accumerate_count` 表和五档标签，反过来证明「每个回调的时长是 1ms」。

**答案**：设回调周期为 \(T\)，则第 \(i\) 档的等效带宽应为 \(1/(N_i T)\)。取 \(N = 1, 3, 10, 33, 100\)，令计算值与标签 1000/300/100/30/10 Hz 同时吻合，解得 \(T \approx 1\,\text{ms}\)（例如 \(N=100 \to 10\,\text{Hz}\) 要求 \(T = 1/(100 \times 10) = 1\,\text{ms}\)；\(N=3 \to 333\,\text{Hz}\approx 300\,\text{Hz}\) 亦吻合）。若 \(T=5\,\text{ms}\)，五档将变成 200/67/20/6/2 Hz，与标签全面矛盾——注释 "5ms" 不可能成立。

## 5. 综合实践：估算一次完整扫频的耗时

把三个模块串起来回答一个工程问题：**「101 点、某档带宽下，一次扫频要多久？」**——这正是 NanoVNA 用户切换带宽时最直观的感受，而它的答案完全由本讲的 1ms 节拍决定。

**任务**：编写 `sweep_time_estimator.py`（**示例代码**），输入带宽档位，输出估算耗时：

- 每个频点的回调数 = CH0 的 \((\text{delay}-1+N)\) + CH1 的 \((2-1+N)\)；同频段内不换带时 `delay = DELAY_NORMAL = 2`，首个频点再多丢 1 个（`sweep()` 的 `(i == 0) ? 1 : 0`）；
- 每次回调 1ms；另加每点约 2×60µs 的 I2C 通道切换与 2×60µs 的 `sample_func`（`sweep()` 行末注释给出的量级）。

参考骨架：

```python
N      = [1, 3, 10, 33, 100]          # bandwidth_accumerate_count
POINTS = 101                          # POINTS_COUNT
def sweep_time_ms(bw):
    n, overhead = N[bw], 0.25         # 0.25ms ≈ I2C切换+取Γ等杂项
    total = 0
    for i in range(POINTS):
        delay = 2 + (1 if i == 0 else 0)     # DELAY_NORMAL + 首点对齐
        total += (delay - 1 + n) + (2 - 1 + n) + overhead
    return total

for bw, label in enumerate(["1kHz", "300Hz", "100Hz", "30Hz", "10Hz"]):
    print(f"{label:>5}: {sweep_time_ms(bw)/1000:6.1f} s / {POINTS} 点")
```

**预期结果**（手工推演，可本地运行复核）：按上式总耗时为 \(202N + 228\) ms——1kHz 档（\(N=1\)）约 **0.43s**，10Hz 档（\(N=100\)）约 **20.4s**，两者相差约 47 倍，直观体现「以时间换精度」。

**真机验证（可选）**：串口执行 `bandwidth 1000` 后 `scan 1000000 900000000 101`，再换 `bandwidth 10` 重复，用秒表测两轮耗时，比值应接近估算（`scan` 额外的数据打印时间会带来少量偏差）。若估算与实测差一个量级，优先检查你是否把「每回调 1ms」误当成了注释里的 5ms。

## 6. 本讲小结

- NanoVNA 用立体声音频 codec tlv320aic3204 当接收机：**左声道固定接参考信号，右声道经输入复用器在反射（IN3/CH0）与传输（IN1/CH1）间切换**，切换只是 6 字节的 I2C 写（`tlv320aic3204_select`）。
- codec 是 I2S **主机**（BCLK/WCLK 输出），STM32 SPI2 从模式只收；8MHz MCLK 由 si5351 CLK2 提供，经 PLL/分频得到 **48kHz** 采样率——整个系统与射频信号源同源时钟。
- DMA 双缓冲让 `rx_buffer`（192 半字）每 **1ms** 交出一个 96 半字（48 立体声帧）的半区并在中断里触发 `i2s_end_callback`；头文件里 "5ms @ 48kHz" 的注释是历史遗留（初始提交值为 480，2016 年改成 96 后未更新注释）。
- `dsp_start/dsp_wait` 是「先丢再测」协议：`wait_count` 丢弃换频/换通道后的暂态缓冲，`accumerate_count` 按 `bandwidth_accumerate_count[] = {1,3,10,33,100}` 相干累积，等效带宽 \(\mathrm{RBW} \approx 1/(N \times 1\,\text{ms})\)，对应 1kHz~10Hz 五档。
- 回调运行在中断上下文：只做有界的纯计算（`dsp_process`），共享计数器必须 `volatile`，且 `accumerate_count > 0` 守卫防 `uint8_t` 下溢回绕导致 `dsp_wait` 死锁。
- 读固件时序类注释要动手核算，「常量 × 注释」对不上时用 `git log -S` 考古，往往能还原设计演变。

## 7. 下一步学习建议

本讲把样本安全送到了 `dsp_process` 的门口，并解释了「累积多少、何时取结果」；下一讲 **u2-l4（dsp.c：数字正交解调与 gamma 计算）** 将打开这扇门：`sincos_tbl[48]` 如何充当 5kHz 中频的正交本振、乘加累积为何等效于锁相放大器、`calculate_gamma` 的复数除法如何把 `acc_ref/acc_samp` 四个浮点数变成 Γ。读完 u2-l4 后，建议回头把本讲 4.3.4 的模拟脚本扩展成「给 `dsp_process` 喂合成正弦、验证累积器输出」的完整 DSP 仿真，为 u3 单元的校准数学打好基础。
