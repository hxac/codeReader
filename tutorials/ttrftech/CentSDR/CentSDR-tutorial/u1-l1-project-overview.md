# 项目总览：CentSDR 是一台什么样的接收机

## 1. 本讲目标

读完本讲，你应该能够：

1. 说清楚 CentSDR 的定位：一台**不依赖电脑、独立运行**的手持式软件定义无线电（SDR）接收机。
2. 按顺序说出完整的信号流向：天线 → 正交检波器 → 音频编解码器 ADC → STM32 数字信号处理 → DAC/LCD。
3. 列出仓库根目录每一个源文件的职责分工，知道「哪个功能去哪个文件里找」。
4. 理解 `nanosdr.h` 作为全局共享头文件的组织方式：它如何用注释分节，把十几个 `.c` 文件粘合在一起。
5. 独立完成综合实践：对照硬件框图，手工绘制一张从 SI5351 本振到 LCD 显示的信号/数据流向图，并为每个参与模块在 `nanosdr.h` 中找到对应的 `extern` 声明或类型定义。

本讲是整本学习手册的第一讲，不要求你已经读懂任何一行固件代码——我们将从「这台机器是什么」开始，建立起阅读后续讲义所需的全局地图。

## 2. 前置知识

本讲会用到的概念都在这里用通俗语言解释一遍。已经熟悉的读者可以快速浏览。

### 2.1 什么是 SDR（软件定义无线电）

传统收音机里，选台、解调这些工作由固定电路（滤波器、鉴频器）完成，做出来就只能收一种信号。**SDR 的思路是：把尽可能多的处理环节搬到软件里**——硬件只负责把无线电信号「搬运」成数字样本，剩下的滤波、解调、显示全部由程序完成。换一种解调方式，只需要换一段代码，而不是换一块电路。

CentSDR 就是一台这样的机器：它的「收音」本质是 STM32 单片机里的一段 C 代码（`dsp.c`），支持 CW / LSB / USB / AM / FM / FM 立体声 六种模式，全部由软件切换。

### 2.2 正交检波与 IQ 信号

要理解 SDR 接收机，绕不开两个字母：**I** 和 **Q**。

- **I**（in-phase，同相分量）和 **Q**（quadrature，正交分量）是一对相位相差 90° 的信号。
- 用两个相差 90° 的本振时钟分别与输入射频信号相乘（混频），就能把信号搬移到低频的同时**保留相位信息**——这是后续软件解调 SSB、FM 等各种调制的基础。
- 完成这件事的硬件电路叫**正交检波器**（quadrature detector）。CentSDR 用的正交检波器由 SI5351 芯片产生的两路正交时钟驱动，输出的 I、Q 两路就是「听起来像音频、实际包含全部信息」的基带信号。

一个直观类比：I 和 Q 就像平面直角坐标系的 x 和 y——只知道 x 无法确定一个点，知道 (x, y) 才能完整描述信号在复平面上的位置和运动。

### 2.3 ADC / DAC 与「声卡芯片」

- **ADC**（模数转换器）把模拟电压变成数字样本；**DAC**（数模转换器）反过来。
- CentSDR 没有使用专门的射频 ADC，而是用了一颗**音频编解码器 TLV320AIC3204**——它本来是给手机、录音笔用的「声卡芯片」，内部自带一对立体声 ADC、一对 DAC，还有增益控制和 AGC（自动增益控制）。
- 正交检波器输出的 I、Q 是两路「音频频率」的模拟信号，正好分别接进这颗芯片的左、右声道 ADC；解调完的音频也从它的 DAC 出去。这是低成本 SDR 的经典技巧：**用声卡芯片当射频基带采样器**。

### 2.4 三种芯片间总线：I2C、SPI、I2S

| 总线 | 用途 | 在 CentSDR 中连接谁 |
|------|------|---------------------|
| I2C | 低速控制（读写配置寄存器） | STM32 → SI5351（设本振频率）、STM32 → TLV320AIC3204（设增益/音量） |
| SPI | 中速数据传输 | STM32 → ILI9341 LCD（送像素、命令） |
| I2S | 专为数字音频设计的流总线 | STM32 ↔ TLV320AIC3204（持续不断地收发 IQ 样本和音频样本） |

记住这个分工：**I2C 管「配置」，I2S 管「数据流」，SPI 管「屏幕」**。

### 2.5 STM32F303 与 ChibiOS

- **STM32F303** 是意法半导体的 32 位单片机，内核是 ARM Cortex-M4F——主频 72MHz，带硬件浮点单元（FPU）和 DSP 指令（本讲只需知道「它算得快」）。
- **ChibiOS** 是一个开源的实时操作系统（RTOS），提供线程、互斥锁、硬件抽象层（HAL）。CentSDR 把它作为 git 子模块引入，构建时一起编译。RTOS 让固件可以同时跑「DSP 实时处理」「屏幕刷新」「按键扫描」「USB 命令行」多个任务。

### 2.6 一点数学：采样周期怎么算

固件里反复出现这样的换算：采样率 \( f_s = 48000\,\text{Hz} \) 时，每秒有 48000 个样本，那么 240 个立体声帧对应的实时处理周期就是

\[
T = \frac{240}{48000} = 0.005\,\text{s} = 5\,\text{ms}
\]

这解释了 `nanosdr.h` 中 `AUDIO_BUFFER_LEN` 旁边那句注释「5ms @ 48kHz」——固件的实时 DSP 必须在每 5ms 到来的下一批数据之前处理完上一批，否则就会丢样本。这是理解整个固件架构的关键约束。

## 3. 本讲源码地图

本讲涉及的关键文件及其作用：

| 文件 | 作用 | 本讲视角 |
|------|------|----------|
| `README.md` | 项目自述：定位说明、硬件框图链接、构建与烧录步骤 | 理解项目定位与信号链 |
| `doc/centsdr-blockdiagram.png` | 硬件框图：天线到音频输出、各芯片间的连接 | 信号流向的权威依据 |
| `nanosdr.h` | 全局共享头文件：声明所有模块的接口、类型和全局变量 | 本讲核心精读对象 |
| `main.c` | 固件入口 `main()`、shell 命令、线程、I2S 回调、解调模式表 | 用代码证据验证信号链 |
| `Makefile` | 构建脚本：引入 ChibiOS/CMSIS，列出全部自有源文件 | 目录结构的「官方清单」 |
| `si5351.h` | SI5351 时钟发生器驱动的寄存器宏与接口 | 认识本振驱动的外形 |
| `.gitmodules` | 声明 ChibiOS 子模块 | 解释仓库目录为什么有个空目录 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 硬件组成与信号流向**、**4.2 仓库目录结构与源文件职责**、**4.3 nanosdr.h 的组织方式**。

### 4.1 CentSDR 的定位与硬件信号链

#### 4.1.1 概念说明

CentSDR 的 README 第一句话就给出了定位：

> CentSDR is tiny handheld standalone software defined receiver with LCD display, that is simple, low budget, but has reasonable perfomance.（CentSDR 是一台带 LCD 显示的微型手持**独立**软件定义接收机，简单、低成本，但性能尚可。）

参见 [README.md:L8-L13](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/README.md#L8-L13)——项目旨在为射频技术的学习、实验与教学提供素材。

这句话里每个词都值得咀嚼：

- **standalone（独立）**：不像很多「SDR」实际是一个接电脑的 USB 硬件（如 RTL-SDR dongle），CentSDR 自己就是一台完整的收音机——本振、采样、解调、显示、按键全在机内，开机即用。
- **low budget（低成本）**：它的射频前端极其简单，核心技巧就是 2.2 和 2.3 节说的「音频声卡芯片 + 正交检波器」组合。
- **receiver（接收机）**：只收不发。

硬件上涉及五块主要芯片（框图见 [doc/centsdr-blockdiagram.png](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/doc/centsdr-blockdiagram.png)，README 中引用位置 [README.md:L15-L19](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/README.md#L15-L19)）：

| 芯片/部件 | 角色 | 驱动文件 |
|-----------|------|----------|
| SI5351 | 可编程时钟发生器，产生正交本振时钟驱动正交检波器 | `si5351.c` / `si5351_low.c` |
| 正交检波器 | 模拟电路，把射频信号混频成 I/Q 基带（无驱动代码，纯硬件） | —— |
| TLV320AIC3204 | 音频编解码器：双 ADC 采样 I/Q，双 DAC 输出音频 | `tlv320aic3204.c` |
| STM32F303 | 主控：运行 ChibiOS + 全部 DSP 解调算法 | `main.c` / `dsp.c` |
| ILI9341 | 320×240 彩色 LCD，显示频谱/瀑布/频率 | `ili9341.c` / `display.c` |

注意 README 的一个重要声明（[README.md:L86-L88](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/README.md#L86-L88)）：**本仓库只包含固件源码，不含硬件设计资料**。所以框图是我们了解硬件的最重要窗口。

#### 4.1.2 核心流程

把框图和代码对应起来，CentSDR 的完整信号/数据流向如下（箭头方向即信号方向）：

```text
【射频/模拟域】
天线
  → 带通滤波/前端开关
  → 前置放大器（约 24dB 增益）
  → 正交检波器 ←──────── SI5351 的两路正交本振时钟（I2C 配置频率）
       │  输出两路基带模拟信号
       ▼
   I 路 ──┐
          ├─→ TLV320AIC3204 的双 ADC（I2C 配置增益/AGC/采样率）
   Q 路 ──┘
       │  数字 IQ 样本（交织存放）
       ▼  【I2S 总线，持续数据流】
【数字域：STM32F303】
rx_buffer（I2S DMA 自动写入）
  → i2s_end_callback() 每 5ms 触发一次
  → signal_process 函数指针指向的解调函数（am/fm/lsb/usb/cw/fms 之一）
  → tx_buffer
       │  解调后的音频样本
       ▼  【I2S 总线】
TLV320AIC3204 的 DAC → 耳机/扬声器（听得见的音频）

【旁路数据流：显示与人机交互】
rx_buffer ──搭便车抓样本──→ display.c（FFT 频谱/瀑布/波形）─SPI→ ILI9341 LCD
按键/旋钮（ui.c）──→ uistat 状态 ──→ 改变本振频率/增益/音量/解调模式
```

用一句话概括：**模拟域把射频搬到「音频」，TLV320AIC3204 把音频变成数字，STM32 里的软件完成一切「收音」逻辑，再原路送回音频，并旁路一份去屏幕画画。**

#### 4.1.3 源码精读

上面这张流向图不是凭空画的，每一步都能在源码中找到证据。下面按信号经过的顺序逐一验证。

**证据一：本振频率是目标频率的 4 倍。** `set_tune()` 是「调谐到某频率」的入口：

```c
void
set_tune(int hz)
{
  center_frequency = hz - mode_freq_offset;
  si5351_set_frequency(center_frequency * 4);
}
```

见 [main.c:L196-L201](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L196-L201)。这行代码告诉我们两件事：

1. `si5351_set_frequency(center_frequency * 4)`——送进 SI5351 的频率是中心频率的 **4 倍**。这正是正交检波器的需求：检波器内部用 4 倍频时钟通过分相产生 0°/90°/180°/270° 四相开关信号，等效于两路正交本振。如果你以后看到别的 SDR 项目 SI5351 也设 4 倍频，原因相同。
2. `hz - mode_freq_offset`——不同解调模式有一个频率偏移补偿（例如 AM 模式偏 10kHz，见 `AM_FREQ_OFFSET`），本讲只需知道「显示频率 ≠ 本振对应的中心频率」即可，细节留到单元三。

SI5351 的接口声明在 [si5351.h:L56-L70](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/si5351.h#L56-L70)，其中 `si5351_setupPLL`、`si5351_setupMultisynth` 对应芯片内部「PLL 合成 + 多合成器分频」的两级时钟架构（单元二第一讲会逐行剖析）。

**证据二：IQ 样本经 I2S 双缓冲进入 STM32，解调由函数指针分发。** 这是整个固件最核心的 30 行：

```c
int16_t rx_buffer[AUDIO_BUFFER_LEN * 2];   // I2S 接收：I/Q 交织
int16_t tx_buffer[AUDIO_BUFFER_LEN * 2];   // I2S 发送：解调后音频

signal_process_func_t signal_process = am_demod;  // 默认 AM 解调

void i2s_end_callback(I2SDriver *i2sp, size_t offset, size_t n)
{
  ...
  int16_t *p = &rx_buffer[offset];
  int16_t *q = &tx_buffer[offset];
  ...
  (*signal_process)(p, q, n);   // 实时解调：rx_buffer → tx_buffer
  ...
}
```

见 [main.c:L99-L100](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L99-L100)、[main.c:L113](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L113) 与 [main.c:L258-L276](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L258-L276)。解读要点：

- `rx_buffer` 长度是 `AUDIO_BUFFER_LEN * 2`，因为 I、Q 两路**交织**存放（`rx_buffer[0]`=I₀, `rx_buffer[1]`=Q₀, `rx_buffer[2]`=I₁ …）。
- I2S 驱动由 DMA 搬运数据，每搬完一段就调用 `i2s_end_callback`——它运行在**中断上下文**里，是全固件的实时心跳。
- `signal_process` 是一个**函数指针**，指向谁就用什么算法解调。这就是「软件定义」的落地形式：换解调模式 = 换一个指针，硬件纹丝不动。

**证据三：六种解调模式通过 `mod_table` 注册。**

```c
struct {
  signal_process_func_t demod_func;
  int16_t freq_offset;
  int16_t fs;
  const char *name;
} mod_table[] = {
  { cw_demod,         AM_FREQ_OFFSET, 48, "cw" },
  { lsb_demod,                     0, 48, "lsb" },
  { usb_demod,                     0, 48, "usb" },
  { am_demod,         AM_FREQ_OFFSET, 48, "am" },
  { fm_demod,                      0, 192, "fm" },
  { fm_demod_stereo,               0, 192, "fms" },
};
```

见 [main.c:L165-L177](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L165-L177)。这张表把「模式名 ↔ 解调函数 ↔ 频率偏移 ↔ 采样率」绑定在一起：CW/SSB/AM 用 48kHz 采样，FM 因为需要更宽的带宽（广播 FM 信号宽达约 200kHz）而用 192kHz。`set_modulation()`（[main.c:L179-L194](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L179-L194)）在切换模式时做的正是「改采样率 + 把 `signal_process` 指向新函数」。

**证据四：I2S 配置把 rx/tx 双缓冲接到硬件上。**

```c
static const I2SConfig i2sconfig = {
  tx_buffer,              // TX Buffer
  rx_buffer,              // RX Buffer
  AUDIO_BUFFER_LEN * 2,   // 缓冲长度
  i2s_end_callback,       // 完成回调
  NULL, 0, 2
};
```

见 [main.c:L278-L286](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L278-L286)，它在 `main()` 中被 `i2sStart()`/`i2sStartExchange()` 启用（[main.c:L1009-L1012](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L1009-L1012)）。至此，「TLV320AIC3204 ↔ I2S ↔ rx_buffer/tx_buffer ↔ 解调函数」这条数据链在代码里完全闭合。

#### 4.1.4 代码实践

**实践 A：从框图到代码的「对号入座」**

1. **实践目标**：验证 4.1.2 的信号流向图不是虚构——框图上的每一条主要连线都能在代码里找到一个函数/变量作为落点。
2. **操作步骤**：
   - 在浏览器打开仓库中的 [doc/centsdr-blockdiagram.png](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/doc/centsdr-blockdiagram.png)，对照辨认：天线、前放、正交检波器、SI5351、TLV320AIC3204、STM32、LCD。
   - 打开 [main.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c)，依次定位：`set_tune()`（L196 附近）、`i2s_end_callback()`（L258 附近）、`mod_table[]`（L170 附近）、`i2sconfig`（L278 附近）。
   - 在纸上（或文本编辑器里）为框图的每个箭头标注对应的代码符号，例如：`STM32 →SI5351` 标注 `si5351_set_frequency()`；`TLV320AIC3204 →STM32` 标注 `rx_buffer + i2s_end_callback()`；`STM32 →LCD` 去单元四的 `display.c` 找 `disp_process()`。
3. **需要观察的现象**：框图上的「模拟部分」（滤波、前放、正交检波器）在代码里**找不到**任何驱动——它们是纯硬件，这正是 README「只含固件源码」的含义。
4. **预期结果**：得到一张「框图箭头 ↔ 代码符号」对照表，至少覆盖 6 条连线。本实践无需硬件、无需编译，纯阅读即可完成。

#### 4.1.5 小练习与答案

**练习 1**：CentSDR 和 RTL-SDR USB dongle 都是 SDR，架构上最大的不同是什么？

**参考答案**：RTL-SDR 只是「射频到 USB」的前端，解调、显示全部依赖上位机电脑；CentSDR 把本振控制、采样、解调 DSP、显示、交互全部放在机内的 STM32 固件里，是一台 standalone（独立）设备——`dsp.c` 里的解调函数和 `display.c` 的界面就是「电脑上那部分软件」的机内等价物。

**练习 2**：为什么 `set_tune()` 里送给 SI5351 的频率要乘 4？

**参考答案**：正交检波器需要 0°/90° 两相本振时钟，电路用 4 倍于中心频率的时钟分相产生四相开关序列，所以软件必须把目标中心频率乘 4 再写入 SI5351（`si5351_set_frequency(center_frequency * 4)`，main.c:L200）。

**练习 3**：`rx_buffer` 为什么声明为 `AUDIO_BUFFER_LEN * 2` 个 `int16_t`，而注释却说「5ms @ 48kHz」？

**参考答案**：因为 I、Q 两路样本**交织**存放在同一个缓冲里，每对 IQ 占 2 个 `int16_t`。一次回调处理 480 个 `int16_t`（即 240 对 IQ 帧），按 \( 240 / 48000 = 5\,\text{ms} \) 换算正是 5ms 的实时周期；`dsp.c` 中解调循环写 `for (i = 0; i < len/2; i++)`（如 dsp.c:L358）也是同样的原因——每两格取一对 IQ。

### 4.2 仓库目录结构与各源文件职责

#### 4.2.1 概念说明

CentSDR 仓库采用「**平铺式**」布局：十几个自有源文件全部放在根目录，没有 `src/` 子目录。这种布局对小项目反而高效——打开编辑器一眼看全。理解每个文件的职责，是后续每一讲「知道去哪找代码」的前提。

目录层面只有 5 个子目录：

| 目录 | 内容 |
|------|------|
| `ChibiOS/` | RTOS 子模块（克隆后需 `git submodule update --init` 才有内容；子模块声明见 [.gitmodules:L1-L3](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/.gitmodules#L1-L3)，指向 edy555 的 fork） |
| `CMSIS/` | ARM 官方 DSP 库的**子集**，只保留本固件用到的 biquad 滤波、radix-4 FFT 等 |
| `NANOSDR_STM32_F303/` | 板级支持包：`board.c`/`board.h` 定义引脚映射与时钟，`board.mk` 供构建系统包含 |
| `python/` | 上位机工具：`centsdr.py` 控制脚本 + 三个滤波器设计 Jupyter notebook |
| `doc/` | 实物照片、硬件框图、波形截图 |

#### 4.2.2 核心流程

Makefile 是目录结构的「官方清单」——它决定了哪些文件会被编译进固件。构建流程可以概括为：

```text
make
 ├─ 引入 ChibiOS 的启动代码 / 内核 / HAL / shell（各 .mk 片段）
 ├─ 引入 CMSIS DSP 库子集（DSPLIBSRC）
 ├─ 编译根目录自有源文件（CSRC）
 ├─ 用 STM32F303xB.ld 链接（LDSCRIPT）
 └─ 产出 build/ch.elf / ch.bin / ch.hex
```

#### 4.2.3 源码精读

**自有源文件清单就在 Makefile 里。** [Makefile:L121-L134](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L121-L134) 的 `CSRC` 变量列出了全部自有 C 文件，按功能加上注释就是一张「文件职责表」：

```makefile
CSRC = $(STARTUPSRC) ... # ChibiOS 内核/HAL/shell + CMSIS
       usbcfg.c \                       # USB CDC 虚拟串口配置
       si5351.c si5351_low.c \          # 本振时钟驱动（含裸机 I2C 底层）
       tlv320aic3204.c \                # 音频编解码器驱动
       ui.c \                           # 按键/旋钮 UI 状态机
       display.c ili9341.c \            # 屏幕内容绘制 / LCD 底层驱动
       numfont20x24.c numfont32x24.c \  # 大号数字字库（频率显示用）
       Font5x7.c icons.c \              # 5x7 ASCII 字库 / 图标位图
       dsp.c \                          # 全部解调算法（DSP 核心）
       main.c flash.c crt2.c            # 入口+命令+线程 / 配置持久化 / 启动文件
```

由此得到根目录文件的职责分工表（**建议收藏，整本手册都会用到**）：

| 文件 | 层次 | 一句话职责 |
|------|------|-----------|
| `main.c` | 应用 | 固件入口 `main()`、27 条 shell 命令、两个线程、I2S 回调、`mod_table` |
| `nanosdr.h` | 应用 | 全局共享头：跨模块类型/变量/函数声明（见 4.3） |
| `dsp.c` | 算法 | 六种解调器、NCO、IQ 平衡、去直流等全部 DSP |
| `si5351.c` / `si5351.h` / `si5351_low.c` | 驱动 | SI5351 本振频率计算与寄存器写入；`si5351_low.c` 是上电早期的位操作 I2C |
| `tlv320aic3204.c` | 驱动 | 编解码器寄存器配置、增益/音量/AGC、采样率切换 |
| `ili9341.c` | 驱动 | LCD 初始化、矩形填充、位图/字符绘制的 SPI 传输 |
| `display.c` | 界面 | 频谱/瀑布/波形绘制、频率与状态栏排版、采样抓取 |
| `ui.c` | 界面 | 按钮消抖、单击/长按判定、正交编码器解码、模式档位机 |
| `flash.c` | 系统 | 配置的 Flash 持久化（页擦除+半字编程+校验和） |
| `usbcfg.c` / `usbcfg.h` | 系统 | USB CDC 设备描述符与虚拟串口驱动 |
| `Font5x7.c` / `numfont20x24.c` / `numfont32x24.c` / `icons.c` | 数据 | 字库与图标位图数组（无逻辑，纯数据） |
| `crt2.c` | 系统 | 自定义 C 启动例程 |
| `STM32F303xB.ld` / `ccmfunc.ld` / `rules_code.ld` | 构建 | 链接脚本：Flash/RAM/CCM 内存布局 |
| `chconf.h` / `halconf.h` / `mcuconf.h` | 配置 | ChibiOS 内核/HAL/MCU 裁剪配置 |
| `build.sh` / `prog.sh` / `flash-*.gdb` | 工具 | 构建、烧录辅助脚本 |

**构建系统的三个关键点**，见 [Makefile:L89-L117](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L89-L117)：

- `CHIBIOS = ChibiOS`（L89） + 一连串 `include *.mk`（L92-L105）：ChibiOS 采用「模块化 make 片段」组织，内核、HAL、shell 各自带 `.mk`。
- `LDSCRIPT= STM32F303xB.ld`（L109）：指定链接脚本，决定代码和数据在 Flash/RAM 里的最终住址。
- `DSPLIBSRC`（L113-L117）：只编译了 5 个 CMSIS 文件——biquad 滤波、radix-4 FFT 及其公共表，说明作者对体积做了严格取舍。

另外注意 [Makefile:L176-L177](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L176-L177) 的 `MCU = cortex-m4` 和 L216 的 `-DARM_MATH_CM4`：目标 CPU 是 Cortex-M4，CMSIS 库按 M4 的 DSP 指令路径编译——这为单元五的 SIMD 优化讲义埋下伏笔。

#### 4.2.4 代码实践

**实践 B：验证职责表 + 亲手分类**

1. **实践目标**：不背表格，通过工具自己「发现」每个文件的职责。
2. **操作步骤**（在本仓库根目录执行，全部为只读命令）：
   - `wc -l *.c *.h | sort -n` —— 按行数排序，直观看到 `display.c`、`main.c`、`Font5x7.c` 是最大的三个文件。
   - `grep -c "^" dsp.c` 与 `grep -n "_demod" dsp.c` —— 数一数 dsp.c 里有几个解调函数。
   - `head -30 Font5x7.c` —— 确认它只是巨大的 `const` 数组，没有任何函数逻辑。
   - `git log --oneline -5 -- ili9341.c` —— 看看 LCD 驱动最近的提交（例如 lcd rotation 支持），体会「文件 ↔ 功能」的对应在提交历史里同样成立。
3. **需要观察的现象**：字库/图标文件（`Font5x7.c` 等）里几乎全是 `0x...` 十六进制数据；驱动文件里全是 `i2c`/`spi` 相关调用；`dsp.c` 里找不到任何外设操作。
4. **预期结果**：你能口头回答「想改开机画面去哪个文件？想加一种解调去哪个文件？想改按键行为去哪个文件？」（答案分别是 icons.c/display.c、dsp.c+main.c、ui.c）。若某一步命令在本地环境不可用，标注「待本地验证」即可。

#### 4.2.5 小练习与答案

**练习 1**：克隆仓库后直接 `make` 会失败，为什么？

**参考答案**：`ChibiOS/` 是 git 子模块（见 `.gitmodules`），刚克隆时目录是空的，而 Makefile 第 92 行起要 `include $(CHIBIOS)/os/.../startup_stm32f3xx.mk` 等文件。必须先执行 `git submodule update --init --recursive`（README「Fetch Source」一节，L36-L38）。

**练习 2**：想给固件新增一个自己的源文件 `myfilter.c`，除了写文件本身还需要改哪里？

**参考答案**：改 `Makefile` 的 `CSRC` 变量（L121-L134），把 `myfilter.c` 追加进去；若放在新目录还要加 `INCDIR`。ChibiOS 构建系统按 `CSRC` 清单编译。

**练习 3**：`CMSIS/` 目录里为什么只有 5 个 DSP 库源文件被编译（`DSPLIBSRC`），而不是整个库？

**参考答案**：`Makefile` L113-L117 只列出 `arm_biquad_cascade_df1_q15.c`、两个 radix-4 FFT 文件、位反转和公共表——固件只用到 IIR 滤波和 FFT。STM32F303 的 Flash 有限（链接脚本按 STM32F303xB 即 128KB 规划），裁掉不用的 CMSIS 部分可以省体积、加快编译。

### 4.3 nanosdr.h：全局共享头文件的组织方式

#### 4.3.1 概念说明

`nanosdr.h` 是这个项目的「**中央车站**」：所有跨模块共享的类型定义、全局变量声明（`extern`）和函数原型都集中在这一个 310 行的头文件里，任一 `.c` 文件 `#include "nanosdr.h"` 之后就能互相「看见」。

嵌入式项目常见两种风格：一是每个模块配自己的 `xxx.h`（如本项目的 `si5351.h`、`usbcfg.h`）；二是再来一个全局伞形头把应用层粘起来。CentSDR 两者兼用——**芯片驱动自带专用头，应用层共享 `nanosdr.h`**。这种中心化设计的利与弊：

- 好处：找任何接口只需打开一个文件；新增模块不需要发明头文件组织方案。
- 代价：任何模块改动都会触发大量 `.c` 重新编译；接口没有分层约束，谁都能碰谁。

#### 4.3.2 核心流程

`nanosdr.h` 内部用 `/* xxx.c */` 风格的注释把声明按「来源模块」分成节，节顺序大致就是数据流向顺序：

```text
nanosdr.h
 ├─ /* main.c */        stat_t 统计结构、set_agc_mode()
 ├─ /* tlv320aic3204.c */ AGC 配置结构体 + 编解码器全部 API
 ├─ /* dsp.c */         缓冲区定义、buffer_ref_t、signal_process 函数指针、
 │                      六个解调函数原型、FS/PHASESTEP 宏、立体声状态
 ├─ /* font */          四张字库位图的 extern 声明
 ├─ /* ili9341.c */     font_t 类型、RGB565 宏、LCD 绘制 API
 ├─ /* display.c */     disp_init/disp_process/采样抓取等
 ├─ /* ui.c */          modulation_t 枚举、uistat_t 状态结构（人机交互核心）
 └─ /* flash.c */       channel_t、config_t（持久化布局）、CONFIG_MAGIC
```

读这个头文件的正确姿势：**从头读到尾，等于把整个系统的「接口面」扫了一遍**。它也是回答「模块 A 如何影响模块 B」的最快索引。

#### 4.3.3 源码精读

精读三段最有代表性的声明。

**第一段：dsp.c 节——实时数据链的全部「轨道」**（[nanosdr.h:L92-L114](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L92-L114)）：

```c
// 5ms @ 48kHz
#define AUDIO_BUFFER_LEN 480

extern int16_t rx_buffer[AUDIO_BUFFER_LEN * 2];
extern int16_t tx_buffer[AUDIO_BUFFER_LEN * 2];
extern int16_t buffer[2][AUDIO_BUFFER_LEN];
extern int16_t buffer2[2][AUDIO_BUFFER_LEN];

typedef enum { B_CAPTURE, B_IF1, B_IF2, B_PLAYBACK, BUFFERS_MAX } buffer_t;

typedef struct {
  enum { BT_C_INTERLEAVE, BT_IQ, BT_R_INTERLEAVE, BT_REAL } type;
  int16_t length;
  int16_t *buf0;
  int16_t *buf1;
} buffer_ref_t;

typedef void (*signal_process_func_t)(int16_t *src, int16_t *dst, size_t len);

extern signal_process_func_t signal_process;
```

解读：

- 四个缓冲各有分工：`rx_buffer`（原始交织 IQ）、`tx_buffer`（输出音频）、`buffer`/`buffer2`（中间处理暂存）。
- `buffer_ref_t` 是一张「缓冲区说明书」：`type` 说明数据格式（交织复数 / 分离 IQ / 实数），`buf0/buf1` 指向数据。配套的实表 `buffers_table` 定义在 [main.c:L102-L107](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L102-L107)，shell 的 `data` 命令和显示模块靠它按编号取缓冲（单元四频谱讲义会用到）。
- `signal_process_func_t` 定义了解调函数的统一签名 `(src, dst, len)`——**这就是插入新算法的插槽**，单元五的综合实践会真正用它新增一种解调模式。

**第二段：ui.c 节——整机运行状态的中枢 `uistat_t`**（[nanosdr.h:L240-L276](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L240-L276)）：

```c
typedef enum {
    MOD_CW, MOD_LSB, MOD_USB, MOD_AM, MOD_FM, MOD_FM_STEREO, MOD_MAX
} modulation_t;

typedef struct {
    enum { CHANNEL, FREQ, VOLUME, MOD, AGC, RFGAIN, AGC_MAXGAIN, CWTONE, IQBAL,
         SPDISP, WFDISP, MODE_MAX } mode;
    int8_t volume;
    uint8_t channel;
    uint32_t freq;
    modulation_t modulation;
    int16_t rfgain;
    uint8_t fs; /* 48, 96, 192 */
    ...
} uistat_t;

extern uistat_t uistat;
```

`uistat`（UI 状态）是用户一切操作在固件里的影子：当前频率、解调模式、音量、增益、旋钮当前调什么参数（`mode` 枚举）。注意 `modulation_t` 的枚举值顺序与 [main.c:L170-L177](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L170-L177) 的 `mod_table[]` 下标一一对应——`mod_table[uistat.modulation]` 即可取到该模式对应的解调函数和采样率。这种「枚举当索引用」的表驱动手法贯穿全固件。

**第三段：flash.c 节——掉电保存的配置布局 `config_t`**（[nanosdr.h:L282-L306](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L282-L306)）：

```c
#define CHANNEL_MAX 100

typedef struct {
  uint32_t freq;
  modulation_t modulation;
} channel_t;

typedef struct {
  int32_t magic;
  uint16_t dac_value;
  tlv320aic3204_agc_config_t agc;
  channel_t channels[CHANNEL_MAX];
  uistat_t uistat;
  int8_t freq_inverse;
  uint8_t button_polarity;
  int8_t lcd_rotation;
  int32_t checksum;
} config_t;

#define CONFIG_MAGIC 0x434f4e45 /* 'CONF' */
```

解读：`config_t` 就是「这台收音机的全部记忆」——100 个信道、当前 UI 状态、AGC 参数等，整体写入 Flash 末页。`magic` 字段（ASCII 恰为 `'CONF'`）用于判断 Flash 里是否有有效配置，`checksum` 用于校验数据完整性；开机时 `config_recall()`（flash.c）校验通过才恢复，否则使用 `main.c` 里的默认值（[main.c:L120-L163](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L120-L163)，默认 567kHz AM 起步，还预置了 18 个常用电台信道）。

顺带一提 [nanosdr.h:L26-L45](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L26-L45) 的 `stat_t`：它由 `main.c` 的 Thread1 每 100ms 更新一次，字段 `interval_cycles`/`busy_cycles` 记录 I2S 回调的周期与耗时（DWT 周期计数器测得），`fps`/`overflow` 记录刷新率与 ADC 溢出——这是观察系统实时健康状态的仪表盘，单元五并发讲义的主角之一。

#### 4.3.4 代码实践

**实践 C：给 `nanosdr.h` 写一张「模块 → 声明」注释清单**

1. **实践目标**：熟悉全局头文件的分节结构，练习「按声明找实现、按实现找声明」的双向检索。
2. **操作步骤**：
   - 打开 [nanosdr.h](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h)，逐节浏览注释（`/* main.c */`、`/* dsp.c */`…）。
   - 为下列 5 个模块各找出至少 1 个 `extern` 变量声明、1 个函数原型、1 个类型定义，抄录行号：`tlv320aic3204.c`（提示：L56-L86）、`dsp.c`、`ili9341.c`（提示：font_t 在 L191）、`ui.c`、`flash.c`。
   - 反向验证：对每个函数原型，用 `grep -n "函数名" 对应的.c文件` 找到定义行，确认声明与定义签名一致。
3. **需要观察的现象**：`ili9341.c` 节里有 `extern uint16_t spi_buffer[]`（L205）这样的全局绘制缓冲声明；`ui.c` 节的 `uistat_t` 内嵌了好几个匿名枚举——C 语言里枚举可以定义在结构体内部，这里被用来给字段做「取值范围自文档化」。
4. **预期结果**：产出一张 5 行的清单表（模块 | extern 变量 | 函数原型 | 类型定义，各带行号）。此表在综合实践中会直接复用。

#### 4.3.5 小练习与答案

**练习 1**：`extern int16_t rx_buffer[...]` 写在头文件里，为什么不会导致「重复定义」？

**参考答案**：`extern` 声明只告诉编译器「这个变量在别处定义，类型如下」，不分配存储。真正的定义（无 `extern`）只出现在 `main.c` L99-L100。多个 `.c` 包含同一个带 `extern` 的头是安全的；如果谁在头文件里去掉 `extern` 写了定义，链接时就会报 multiple definition 错误。

**练习 2**：`modulation_t` 枚举定义在 `ui.c` 节，但它被 `dsp.c` 的模式表和 `flash.c` 的 `config_t` 都用到了。这体现了 `nanosdr.h` 的什么作用？

**参考答案**：它充当**跨模块的单一事实来源**（single source of truth）。枚举顺序与 `main.c` 的 `mod_table[]` 下标、`flash.c` 持久化的信道数据都隐式耦合，集中定义保证了所有模块看到同一套值——如果各自定义，一旦顺序改动就会出现「保存的模式重启后变成另一个模式」这类隐患。

**练习 3**：不看源码，仅凭 `config_t` 结构体（nanosdr.h:L284-L299），你能推断出这台收音机为用户保存哪些「记忆」？

**参考答案**：100 个信道（每个含频率+调制模式）、完整的 `uistat`（音量、当前信道/频率/模式、增益、采样率、AGC 档、CW 音调、IQ 平衡等）、DAC 初值（背光/偏置相关）、AGC 参数、按键极性、LCD 旋转方向；外加 `magic` 与 `checksum` 两个完整性字段。读结构体即读产品需求，这是嵌入式代码阅读的重要技巧。

## 5. 综合实践

本讲的综合实践把 4.1 的信号链、4.2 的文件职责和 4.3 的头文件结构合成一张图。

**任务：手工绘制 CentSDR 的信号/数据流向图 + `nanosdr.h` 注释清单**

1. **实践目标**：不借助任何讲义文字，仅对照 [README 的模块图](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/doc/centsdr-blockdiagram.png)和源码，产出一张自己画的、每个环节都标注了代码落点的流向图。

2. **操作步骤**：
   - **第一步（画硬件链）**：在纸上或用任意画图工具，从天线开始画到扬声器/LCD，至少包含：前放、正交检波器、SI5351（含 I2C 控制线）、TLV320AIC3204（双 ADC + DAC）、STM32F303、ILI9341；总线用不同颜色区分 I2C / I2S / SPI。
   - **第二步（标数据名）**：在数字域标注数据经过的缓冲与函数：`rx_buffer` → `i2s_end_callback()` → `signal_process` 指向的解调函数 → `tx_buffer`；旁路一条 `display.c` 抓样本 → SPI → LCD。
   - **第三步（写注释清单）**：在图旁边（或图的背面）为每个参与模块列出它在 `nanosdr.h` 中的对应声明，格式示例：
     ```text
     模块: SI5351 本振
       - 声明位置: si5351.h L56-L70（专用头，不在 nanosdr.h）
       - 调用入口: set_tune() main.c L196-L201，4 倍频写入

     模块: TLV320AIC3204 编解码器
       - 类型: tlv320aic3204_agc_config_t  nanosdr.h L56-L64
       - 原型: tlv320aic3204_init / set_gain / set_fs ...  nanosdr.h L66-L84

     模块: DSP 解调
       - 类型: signal_process_func_t  nanosdr.h L112
       - 变量: signal_process  nanosdr.h L114（定义于 main.c L113）
       - 原型: am_demod 等 6 个  nanosdr.h L116-L121

     模块: UI 状态
       - 类型: uistat_t / modulation_t  nanosdr.h L240-L274
       - 变量: uistat  nanosdr.h L276

     模块: 配置持久化
       - 类型: config_t / channel_t  nanosdr.h L284-L299
       - 变量: config  nanosdr.h L301
     ```
     按此样式补全：LCD 驱动（ili9341 节）、显示绘制（display 节）、字体图标（font 节）、运行统计（main.c 节的 `stat_t`）。
   - **第四步（自检）**：沿图从天线走到扬声器，每经过一个箭头就问一句「这个箭头对应哪个函数/缓冲/中断？」答不上来的就是后续讲义要重点补的洞。

3. **需要观察的现象**：画完你会发现固件代码覆盖的只是框图的「数字半区」——模拟半区（天线到 I/Q 输出）在代码里只有 SI5351 一个可控点。这种「软件能控制什么」的边界感，是嵌入式 SDR 开发的重要直觉。

4. **预期结果**：一张 A4 大小的手绘/电子流向图 + 一份 8 行左右的模块声明注释清单。本实践全部为阅读与绘制，无需硬件、无需编译，可 100% 完成。后续讲义（尤其是单元二各外设驱动）会反复回到这张图。

## 6. 本讲小结

- **CentSDR 是一台独立的低成本手持 SDR 接收机**：SI5351 提供正交本振，正交检波器把射频搬为 I/Q 基带，声卡芯片 TLV320AIC3204 的双 ADC 完成采样，「收音」的全部智慧都在 STM32F303 固件里（[README.md:L8-L13](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/README.md#L8-L13)）。
- **信号主链**：`rx_buffer`（交织 IQ）→ `i2s_end_callback()` 每 5ms → `signal_process` 函数指针指向的解调函数 → `tx_buffer` → DAC；显示链在 `rx_buffer` 上「搭便车」抓样本送 LCD。
- **解调模式是表驱动的**：`mod_table[]`（main.c L170-L177）把 6 种模式与解调函数、频率偏移、采样率绑定，`set_modulation()` 换采样率 + 换函数指针即完成切换——「软件定义」的具体形态。
- **仓库是平铺布局**：根目录十几个 `.c` 按「驱动 / 算法 / 界面 / 系统」分层，职责清单就在 `Makefile` 的 `CSRC`（L121-L134）；`ChibiOS/` 是必须先 init 的子模块，`CMSIS/` 只保留了 5 个用到的 DSP 源文件。
- **`nanosdr.h` 是全局中央车站**：按 `/* xxx.c */` 注释分节集中声明跨模块接口；`uistat_t` 是用户操作的影子、`config_t` 是掉电记忆、`stat_t` 是实时仪表盘，三者构成理解固件行为的钥匙。

## 7. 下一步学习建议

下一讲（`u1-l2-build-and-flash.md`）将动手把固件真正构建出来：安装 arm-none-eabi 交叉工具链、初始化 ChibiOS 子模块、走读 `Makefile` 与 `build.sh`、用 OpenOCD / st-util / Nucleo 三种方式之一烧录。届时你会看到本讲提到的 `build/ch.elf` 真实出现在磁盘上。

在进入下一讲之前，建议先做两件小事巩固本讲：

1. 通读一遍 [nanosdr.h](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h) 全文（310 行，10 分钟），验证 4.3 节的分节描述。
2. 浏览 [README.md](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/README.md) 的 Build Firmware 一节，对照检查自己机器上是否已有 git 和 make，为下一讲做准备。

如果你已迫不及待想看算法，可以提前跳读 [dsp.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c) 开头的 `cos_sin_table`——那张 256 项的三角函数表是单元三整块拼图的第一块，但现在看不懂完全正常。
