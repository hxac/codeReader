# 实时音频数据流：I2S 双缓冲与解调回调

## 1. 本讲目标

学完本讲，你应该能够：

1. 算清楚 `AUDIO_BUFFER_LEN = 480` 与「每 5ms 一次回调」之间的关系，并能推广到 48/96/192kHz 三档采样率。
2. 画出一次 I2S 回调里数据的完整旅程：`rx_buffer` → `buffer`/`buffer2` 中间缓冲 → `tx_buffer`，并说出每个缓冲的格式（交织复数 / 分离 IQ 平面 / 交织实数）。
3. 解释 `i2s_end_callback` 里那个函数指针 `signal_process` 如何让解调算法可以「热切换」，而不需要重启任何线程或 DMA。
4. 说出 `set_fs()` 切换采样率时为什么必须「先停编解码器的时钟、再停 I2S、睡 40ms、重启 I2S、最后重配编解码器」这个顺序的道理。

本讲是单元二的关键一讲：u2-l2 讲完了编解码器 TLV320AIC3204 怎么被配置，本讲讲数据怎么实时地「流」起来；它也是单元三全部解调算法的入口——所有 `*_demod` 函数都是被本讲的回调机制驱动的。

## 2. 前置知识

### 2.1 I2S 是什么

I2S（Inter-IC Sound）是飞利浦定下的芯片间传数字音频的协议，一共三根信号线：

| 信号 | 全称 | 作用 |
|------|------|------|
| BCLK | 位时钟 | 每一个 bit 一个脉冲 |
| WCLK | 字时钟（也叫 LRCLK） | 高半帧传右声道，低半帧传左声道 |
| SD | 串行数据 | 音频数据本身 |

「一帧」= 一次 WCLK 周期 = 同时传左、右两个声道各一个采样。对 CentSDR 来说，左/右两个声道装的不是「立体声」，而是正交检波器送来的一对 **I/Q 基带信号**（这是整个 SDR 的根基，见 u1-l1 的信号流向图）。

I2S 和 I2C 一样有主从之分，但这里**主从只决定谁来产生 BCLK/WCLK**。u2-l2 已经确认：TLV320AIC3204 被配置成 BCLK/WCLK 输出方（I2C 寄存器 0x1b 写 0x0c 即 "Set the BCLK,WCLK as output"），也就是说**编解码器是 I2S 主机，STM32 是从机**。这一点是理解本讲 4.4 节采样率切换时序的钥匙。

### 2.2 DMA 与双缓冲（ping-pong）

DMA（直接内存访问）控制器可以不经 CPU 搬运数据：外设每来一个采样，DMA 自动把它写进内存。但 CPU 什么时候处理呢？如果每来一个样本就中断一次，48kHz 下每秒要中断 48000 次，开销太大。

惯用手法是 **DMA 循环模式 + 半满/全满两次中断**：把缓冲区对半分成 A、B 两半，DMA 填满 A 半时触发一次中断，CPU 趁 DMA 去填 B 半的窗口处理 A 半；B 半填满时再来一次中断，如此往复。这叫双缓冲或乒乓缓冲。CPU 每次拿到的是「刚刚填满的半块」，永远有一整块的时间窗口可以慢慢算——只要在另一半填满之前算完，数据就永不丢失。

### 2.3 函数指针当「插槽」

```c
signal_process_func_t signal_process = am_demod;  // 类型是函数指针
...
(*signal_process)(p, q, n);                        // 通过指针调用
```

把函数指针当成一个可插拔的插槽：运行期给它赋不同的值，同一个调用点就会执行不同的算法。这是 C 语言里最常见的「策略模式」，也是本讲解调热切换的全部秘密。

## 3. 本讲源码地图

| 文件 | 在本讲中的角色 |
|------|----------------|
| [main.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c) | 定义 rx/tx 双缓冲与 `buffers_table`；`i2s_end_callback`、`I2SConfig`、`set_fs`、`mod_table` 全在这里 |
| [nanosdr.h](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h) | `AUDIO_BUFFER_LEN`、四组缓冲的 extern 声明、`buffer_ref_t` 类型、`signal_process_func_t` 函数指针类型 |
| [dsp.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c) | 定义 `buffer`/`buffer2` 中间缓冲；`am_demod`/`demod_weaver`/`fm_demod_stereo` 是回调的「插槽内容」，展示了缓冲的用法 |
| [mcuconf.h](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/mcuconf.h) | I2S 驱动的裁剪配置：用哪个 SPI、主/从模式、DMA 通道与优先级 |
| [tlv320aic3204.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/tlv320aic3204.c) | `tlv320aic3204_stop()`/`tlv320aic3204_set_fs()` 是 `set_fs()` 时序的另一半 |
| [display.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c) | 只需知道 `disp_fetch_samples()` 会顺着解调流程「搭便车」抓样本，细节留给 u4-l1 |

说明：I2S 驱动本体（`i2sStart`/`i2sStartExchange`/`i2sStopExchange` 的实现）位于 ChibiOS 子模块（edy555 的 fork）的 STM32F3xx 平台源中，由 Makefile 里的 `$(PLATFORMSRC)` 引入编译；本仓库未签出该子模块，驱动源文件路径待确认，本讲以 main.c 中可见的调用方式为准。

## 4. 核心概念与源码讲解

### 4.1 I2S 链路配置：从机模式与 DMA 搬运

#### 4.1.1 概念说明

这一节回答三个问题：STM32 的 I2S 外设是怎么配的？数据以什么格式在线上跑？DMA 怎么把它搬进内存？

关键结论先摆出来：**STM32F303 是 I2S 从机**。位时钟和字时钟都由编解码器给出，STM32 只是跟着节拍收发数据。这意味着采样率的「旋钮」在编解码器那边（u2-l2 讲的 NDAC/MDAC/NADC/MADC 分频器），STM32 这边换采样率时要做的是「跟着重新对表」——这就是 4.4 节的内容。

#### 4.1.2 核心流程

```
TLV320AIC3204 (I2S 主机)                STM32F303 (I2S 从机)
  产生 BCLK(=64fs)、WCLK(fs)  ──────►  SPI2 的 I2S 外设跟着节拍移位
  SD(ADC 数据: I/Q 交织)       ──────►  DMA1 流4 → 自动写入 rx_buffer
  SD(DAC 数据: 耳机音频)       ◄──────  DMA1 流5 ← 自动读出 tx_buffer
                                         每填满半块缓冲 → 触发 i2s_end_callback
```

数据率核算（以 48kHz 为例）：

- 每秒 48000 帧，每帧 2 声道 × 16bit = 4 字节，单方向 192,000 字节/秒；
- BCLK = 64fs = 3.072MHz（每帧 64 个 bit 周期，数据占其中 16bit×2，其余为槽位空闲——由 u2-l2 中 BCLKN=28 的分频配置可反推验证）。

#### 4.1.3 源码精读

先看内核裁剪，I2S 用的是 SPI2 外设、从机模式、全双工：

- [mcuconf.h:158-175](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/mcuconf.h#L158-L175) — I2S 驱动系统设置。这段声明：启用 SPI2 上的 I2S（`STM32_I2S_USE_SPI2 TRUE`），模式为**从机 + 发送 + 全双工**（`STM32_I2S_MODE_SLAVE | STM32_I2S_MODE_TX | STM32_I2S_MODE_FULLDUPLEX`），收发分别走 DMA1 流 4 和流 5，中断优先级为 2。注意 SPI1 用于驱动 LCD（`STM32_SPI_USE_SPI1 TRUE`），两者互不冲突。
- [halconf.h:83-86](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/halconf.h#L83-L86) — `HAL_USE_I2S TRUE` 打开 I2S 子系统，这样 ChibiOS 的 I2S 驱动源码才会被编进固件。

再看 main() 里 I2S 的启动顺序：

- [main.c:1003-1013](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L1003-L1013) — 先 `tlv320aic3204_init()` 把编解码器配好（此时它开始输出 BCLK/WCLK），随后 `i2sInit()` / `i2sObjectInit(&I2SD2)` / `i2sStart(&I2SD2, &i2sconfig)` / `i2sStartExchange(&I2SD2)` 四步启动 I2S 驱动并开始 DMA 交换。顺序不能反：从机得等主机的节拍出现。

I2S 驱动实例的配置结构体：

- [main.c:278-286](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L278-L286) — `i2sconfig`：TX 缓冲指向 `tx_buffer`，RX 缓冲指向 `rx_buffer`，总长度 `AUDIO_BUFFER_LEN * 2`（= 960 个 int16），回调用 `i2s_end_callback`，RX 回调为空，`i2scfgr`/`i2spr` 分别填 0 和 2（这两个字段对应 STM32 的 I2S 配置/预分频寄存器；由于本机是从机、节拍由主机给定，其取值影响待确认）。

注意这个 `I2SD2` 的编号：ChibiOS 的 I2S 驱动实例挂在 SPI2 上，所以叫 `I2SD2`，与 mcuconf 的 `STM32_I2S_USE_SPI2` 一一对应。

#### 4.1.4 代码实践：从 `stat` 反推回调频率

1. **实践目标**：用固件自带的计数器验证「回调频率随采样率翻倍」。
2. **操作步骤**（需硬件，无硬件则只做第 4 步推导）：连接 USB shell，执行 `mode am`（48kHz），记下 `stat` 输出的 `callback count`；隔约 10 秒再执行一次 `stat`，求差值；然后执行 `mode fms`（192kHz）重复同样操作。
3. **需要观察的现象**：48kHz 下每秒回调次数约 200 次；192kHz 下约 800 次。
4. **预期结果**：差值除以间隔秒数 ≈ 200（48kHz）/ 800（192kHz），与本讲 4.2 节推导一致。**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 STM32 配成 I2S 从机而不是主机？

**答案**：因为 BCLK/WCLK 由编解码器产生（tlv320aic3204.c 初始化时写寄存器 0x1b 设为输出）。采样率由编解码器内部的 NDAC/MDAC 等分频器决定，时钟源头在那里；如果 STM32 当主机，两边各自的采样率时钟稍有偏差就会积累失配，造成周期性丢样/重复样。让提供采样的芯片当主机、消费数据的 MCU 当从机，天然保证节拍一致。

**练习 2**：`I2SD2` 这个名字里的 2 指什么？

**答案**：指它复用的 SPI2 外设。STM32F3 的 I2S 功能与 SPI 共享同一组引脚和寄存器，mcuconf.h 中 `STM32_I2S_USE_SPI2 TRUE` 启用的就是它。

**练习 3**：48kHz、16bit 立体声 I2S 的 BCLK 为什么是 3.072MHz 而不是 1.536MHz？

**答案**：1.536MHz = 32fs 对应「32bit 帧里装满 2×16bit 数据」；而本系统 BCLK = 64fs（每帧 64 个位周期），数据只占每声道槽位的前 16bit，槽位更宽。由编解码器侧 BCLKN 分频值 28 可反推出 64fs。

### 4.2 缓冲区家族：双缓冲、中间缓冲与 buffers_table

#### 4.2.1 概念说明

本讲的「数据结构课」。固件里一共有 **6 组**缓冲，按数据流顺序分成三层：

| 层 | 缓冲 | 大小（int16） | 格式 | 生命周期 |
|----|------|---------------|------|----------|
| 1 | `rx_buffer` / `tx_buffer` | 各 960 | 交织复数 I,Q,I,Q…（rx）/ 交织实数 L,R,L,R…（tx） | DMA 直接读写，也叫**双缓冲** |
| 2 | `buffer[2][480]` | 960 | 分离 IQ 平面：`buffer[0]`=I 路，`buffer[1]`=Q 路 | 解调算法的第一级工作台 |
| 2 | `buffer2[2][480]` | 960 | 同上 | 滤波器输出 / 第二级工作台 |
| 3 | `buffers_table[4]` | — | 描述上面这些缓冲的**元数据表** | 静态常量 |

「交织」和「平面」是一对经典概念：DMA 和 I2S 天生喜欢交织格式（一个流顺序搬运），而 DSP 算法更喜欢平面格式（I、Q 各自连续，方便滤波器逐路处理）。所以几乎每个解调函数的第一步都是「拆交织」，最后一步都是「装交织」。

#### 4.2.2 核心流程

一次回调内，一个典型解调（以 AM/Weaver 为代表）的缓冲旅程：

```
rx_buffer (交织 IQ, len 个 int16)
   │  ① NCO 混频循环：一次读 32bit（I,Q 各 16bit），拆开写两路
   ▼
buffer[0] (I 路)   buffer[1] (Q 路)        ← 拆交织 + 频率搬移
   │  ② CMSIS biquad 低通，逐路滤波
   ▼
buffer2[0] (I 路)  buffer2[1] (Q 路)       ← 滤波后的 IQ
   │  ③ 检波/第二次混频，两路合成一路实数
   ▼
tx_buffer (交织实数, len 个 int16)          ← 装交织，左右声道同值
```

关键数字推导（本讲学习目标之一）。设采样率 \( f_s \)，缓冲总长 480 帧、DMA 对半触发：

\[ T_{\text{回调}} = \frac{480/2}{f_s} = \frac{240}{f_s} \]

| 采样率 | 每次回调处理帧数 | 回调周期 | 每秒回调次数 |
|--------|------------------|----------|--------------|
| 48kHz | 240 | **5ms** | 200 |
| 96kHz | 240 | **2.5ms** | 400 |
| 192kHz | 240 | **1.25ms** | 800 |

48kHz 一栏的 5ms 正是 nanosdr.h 里 `AUDIO_BUFFER_LEN` 头上那句注释「5ms @ 48kHz」的出处。注意换采样率时**缓冲大小不变，变的只是节拍**——192kHz 立体声模式下 DSP 必须在 1.25ms 内算完 240 帧，这就是 `stat` 命令里 load 指标存在的意义（见 4.3 节）。

#### 4.2.3 源码精读

- [nanosdr.h:92-98](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L92-L98) — 缓冲区家族的全部声明：`AUDIO_BUFFER_LEN 480`（注释「5ms @ 48kHz」）、DMA 层的 `rx_buffer`/`tx_buffer`（长度都是 `AUDIO_BUFFER_LEN * 2`，因为交织格式每个帧占 2 个 int16）、DSP 层的 `buffer[2][AUDIO_BUFFER_LEN]` 和 `buffer2[2][AUDIO_BUFFER_LEN]`。
- [main.c:99-100](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L99-L100) — `rx_buffer` 和 `tx_buffer` 的定义处。它们是全局数组，DMA 与 CPU 共享。
- [dsp.c:4-5](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L4-L5) — 两级中间缓冲 `buffer`/`buffer2` 的定义处，在 dsp.c 而不是 main.c，暗示它们属于算法层的私有工作台。
- [nanosdr.h:100-109](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L100-L109) — `buffer_t` 枚举（`B_CAPTURE`/`B_IF1`/`B_IF2`/`B_PLAYBACK`，即信号链上的四个「取样龙头」）和 `buffer_ref_t` 结构：每个缓冲用「类型 + 长度 + 最多两个指针」描述。
- [main.c:102-107](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L102-L107) — `buffers_table`：把四个龙头各自的格式（`BT_C_INTERLEAVE` 交织复数 / `BT_IQ` 平面 IQ / `BT_R_INTERLEAVE` 交织实数）、长度和地址填成一张元数据表。诚实地说明：现有代码中这张表只在此定义、在 nanosdr.h 声明，没有被其他函数消费——shell 的 `data` 命令转储缓冲用的是硬编码 switch（见下面 cmd_data）。可以把它理解为「机器可读的缓冲清单」，为将来通用的缓冲转储/显示抓样功能预留的扩展点。
- [main.c:315-349](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L315-L349) — `cmd_data`：shell 的 `data {0|1|2|3}` 命令分别转储 `rx_buffer`（捕获）、`tx_buffer`（回放）、`buffer[0]`（一级 IF 的 I 路）、`buffer2[0]`（二级 IF 的 I 路），以每行 16 个 `%04x` 十六进制字打印。这就是 python 工具抓波形的底层通道（u1-l4）。
- 拆交织的实例见 dsp.c（下一节详读），例如 [dsp.c:355-364](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L355-L364) 的 Weaver 混频循环：`__SIMD32(src)` 把交织缓冲视为 int32 数组，一次取出一对 I/Q，再用两条 SIMD 乘加分别写出 I 路和 Q 路。

顺带一个容易踩的坑：[nanosdr.h:125-128](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L125-L128) 的 `FS` 宏**写死为 48000**，`PHASESTEP(freq)` 用它换算 NCO 相位步进。这之所以正确，是因为使用 `PHASESTEP` 的模式（AM/CW/SSB）全部运行在 48kHz；192kHz 的 FM 模式另用 [dsp.c:593-594](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L593-L594) 的 `PHASESTEP_NCO19KHz`（按 `IF_RATE 192.0` 计算）。给你自己加新模式时务必想清楚该用哪个。

#### 4.2.4 代码实践：亲眼看看两种格式

1. **实践目标**：区分「交织」与「平面」两种缓冲格式。
2. **操作步骤**（需硬件）：shell 里先 `mode am`，再分别执行 `data 0`（rx_buffer，交织 IQ）与 `data 2`（buffer[0]，平面 I 路）。若有 Python 2 环境，用 `python/centsdr.py -p 0` 和 `-p 2` 直接画图（参见 u1-l4 的用法）。
3. **需要观察的现象**：`data 0` 的相邻两个字是 I、Q 交替（对单边带信号二者数值独立跳变）；`data 2` 是连续的同一路信号。给设备输入一个已知单音（如调幅电台的载波），`data 2` 应呈现接近直流的平线（AM 模式下载波被搬到 0 频附近，见 u3-l3）。
4. **预期结果**：两种转储的数值序列形态明显不同；交织缓冲长度是平面路的两倍。**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`rx_buffer` 为什么声明成 `AUDIO_BUFFER_LEN * 2` 个 int16，而不是 `AUDIO_BUFFER_LEN` 个？

**答案**：`AUDIO_BUFFER_LEN = 480` 数的是**帧**（一个 I/Q 对），交织存储时每帧占 2 个 int16，所以数组长度是 960。而 `buffer[2][480]` 是平面存储，每路刚好 480 个——两边的「480」含义不同，一个是帧数、一个是路内样本数，数值恰好相等纯属格式使然。

**练习 2**：如果 DSP 在半块窗口内没算完会怎样？

**答案**：DMA 会覆盖还在处理的半块，产生劈啪声并可能使 ADC 溢出（`stat` 的 `overflow` 计数与编解码器 sticky 标志会记录）。固件用 `stat` 的 load 百分比监控这个风险，负载接近 100% 就该换低采样率或优化算法了。

**练习 3**：`buffers_table` 里 `BT_C_INTERLEAVE` 和 `BT_R_INTERLEAVE` 有什么区别？

**答案**：C 指 Complex（rx_buffer 里的交织 I/Q **复数**对），R 指 Real（tx_buffer 里左右声道同值的交织**实数**音频）。对显示抓样来说二者都按「交织」处理（[display.c:753-766](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L753-L766) 里两者走同一加窗分支），语义上一个是解调前、一个是解调后。

### 4.3 i2s_end_callback：5ms 心跳与解调热切换

#### 4.3.1 概念说明

整台接收机的「心脏」就是 [main.c:258](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L258) 这个不到 20 行的函数。它运行在**中断上下文**里（DMA 半满/全满中断），每 5ms（48kHz 时）被硬件叫醒一次，做三件事：

1. 对刚填满的半块 `rx_buffer` 调用**当前解调算法**，结果写进对应的 `tx_buffer` 半块；
2. 用 DWT 周期计数器测量本次耗时与两次回调的间隔（负载统计）；
3. 点亮/熄灭 LED（给示波器看的「正在算 DSP」标志）。

而 `signal_process` 这个**函数指针**就是解调算法的插槽：初始化时插的是 `am_demod`，`mode` 命令换台时拔出来换一个。换算法不重启 DMA、不重启线程、不停一个采样——下次回调自然就执行新函数。这就是「热切换」。

#### 4.3.2 核心流程

```
DMA 半满中断（每 240 帧）
   └─ i2s_end_callback(I2SD2, offset, n)        // offset=0 或 480，n=480
        ├─ p = &rx_buffer[offset]; q = &tx_buffer[offset]   // 定位这半块
        ├─ 点亮 LED；读 DWT 计数器 t0
        ├─ (*signal_process)(p, q, n)            // ★ 整个解调在这里发生
        │     ├─ disp_fetch_samples(B_CAPTURE,…) // 顺路给显示抓样本（u4-l1）
        │     ├─ 拆交织 + 混频 → buffer[2]
        │     ├─ 滤波 → buffer2[2]
        │     └─ 检波/合成 → 装交织写 *q
        ├─ 读 DWT 计数器 t1；busy = t1-t0；interval = t0-上次t0
        └─ callback_count++；熄灭 LED

shell: mode fm ──► set_modulation(MOD_FM)
        ├─ set_fs(192)          // 见 4.4
        ├─ signal_process = fm_demod   // ★ 原子地换插槽（一个指针赋值）
        └─ 更新 uistat、刷屏
```

负载指标的含义：\( \text{load} = \frac{\text{busy\_cycles}}{\text{interval\_cycles}} \times 100\% \)，即「DSP 占用的周期 / 两次心跳间的总周期」。interval 恒为 5ms 对应的周期数（48kHz 时），busy 越大负载越高。

#### 4.3.3 源码精读

- [main.c:258-276](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L258-L276) — `i2s_end_callback` 全文。`offset` 参数指出刚填满的是缓冲的哪一半（0 或 480），据此 `p`、`q` 指向输入输出半块的起点；`(*signal_process)(p, q, n)` 是全固件唯一的解调调用点；前后两次 `port_rt_get_counter_value()`（读 Cortex-M 的 DWT 周期计数器）之差算出 `busy_cycles` 与 `interval_cycles`，存进 `stat` 供 `stat` 命令显示；`palSetPad`/`palClearPad` 翻转 GPIOC 的 LED，用示波器量这个引脚的高电平宽度就是 DSP 耗时。
- [main.c:113-117](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L113-L117) — 插槽的初始状态：`signal_process = am_demod`（开机默认 AM 解调），以及 `mode_freq_offset`/`mode_freqoffset_phasestep`/`cw_tone_phasestep` 等算法参数。开机后第一次回调执行的就是 AM。
- [nanosdr.h:112-121](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L112-L121) — 插槽的类型定义 `signal_process_func_t`：任何 `(int16_t *src, int16_t *dst, size_t len)` 签名的函数都能插进来；下面六个 `*_demod` 原型就是全部原装插件。
- [main.c:165-177](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L165-L177) — `mod_table`：每个调制模式一行，登记「解调函数 + 频率偏移 + 采样率 + 名字」。这张表是热切换的数据来源，也是给固件加新模式时要改的表（u5-l4 的主题）。注意 cw/lsb/usb/am 都用 48kHz，fm/fms 用 192kHz。
- [main.c:179-194](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L179-L194) — `set_modulation()`：按表依次 `set_fs`（换采样率，见 4.4）、给 `signal_process` 赋新函数指针、刷新各 NCO 相位步进参数。**先换采样率、后换指针**的顺序有讲究：新算法一插上马上就会被回调执行，此时采样率必须已经就位。
- [dsp.c:420-467](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L420-L467) — 插件的样例 `am_demod`（AM_FREQ_OFFSET 分支）：开头 `disp_fetch_samples(B_CAPTURE, BT_C_INTERLEAVE, src, NULL, len)` 让显示模块搭便车抓原始样本；随后三步走满 4.2 节画的缓冲旅程；结尾再抓一次 `B_PLAYBACK`。算法细节（为什么先搬 10kHz）留给 u3-l3，本讲只看它的「接口姿态」——签名与 `signal_process_func_t` 一致。
- [dsp.c:791-820](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L791-L820) — 最复杂的插件 `fm_demod_stereo` 也遵守同样契约：同样签名、同样在四个龙头处调用 `disp_fetch_samples`。任何你 future 写的解调器照这个模子写即可。
- 顺带一提 [main.c:288-313](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L288-L313)：`tone` 命令演示了对 I2S 外设的直接控制——先清 `SPI_I2SCFGR` 的 `I2SE` 位停掉 I2S、往 `tx_buffer` 填一段测试单音、再置位恢复，DMA 便循环送出这段音。这是不换固件就能让设备发声的自测手段。

一个值得注意的并发细节：回调跑在中断上下文，而 `calc_stat()`（Thread1）也在读 `rx_buffer`、`cmd_data`（shell 线程）也在转储它。这些读取没有加锁，读到跨越半块边界的混合数据是可能的——对统计和调试而言无害。标志位式的生产者-消费者（`spdispinfo.update_flag`）才是这里真正的同步工具，深入讨论在 u5-l1。

#### 4.3.4 代码实践：给心脏搭脉

1. **实践目标**：体会「换模式 = 换函数指针」，并用 load 指标量化不同算法的开销。
2. **操作步骤**（需硬件）：依次执行 `mode am` → `stat`、`mode usb` → `stat`、`mode fms` → `stat`，抄下每组的 `load` 与 `callback count` 增速。
3. **需要观察的现象**：三种模式的 load 不同；fms（192kHz、链路最长）应显著高于 am/usb（48kHz）。
4. **预期结果**：am/usb 负载较低（滤波器级联为主），fms 接近上限（1.25ms 窗口内要做鉴频 + 19kHz PLL + 立体声矩阵）。具体数值**待本地验证**——这正是 u5-l1 要系统研究的课题。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `set_modulation` 里 `signal_process = fm_demod;` 这一句是「原子」的、不需要关中断？

**答案**：32 位对齐的指针赋值在 Cortex-M 上是单条指令，不会被打断。最坏情况是：赋值前刚进了一次回调执行旧算法、赋值后下一次回调执行新算法——两次回调之间数据流只是「晚半拍切换」，不会出现半个块用旧算法、半个块用新算法的撕裂。

**练习 2**：`interval_cycles` 和 `busy_cycles` 各自量的是什么？为什么负载公式用两者相除？

**答案**：`interval_cycles` 是**相邻两次回调进入时刻**之间的周期数（≈ 5ms 换算的周期，反映实时预算），`busy_cycles` 是本次回调内 `signal_process` 执行的净耗时。相除得到「CPU 被这条数据流占用的时间比例」，>100% 即意味着撑不住实时。

**练习 3**：如果不给 `disp_fetch_samples` 传 `B_CAPTURE` 等模式参数、让它无条件拷贝，会发生什么？

**答案**：每次回调都要做一次全量加窗拷贝，白白吃掉宝贵的 DSP 预算。实际的实现（[display.c:726-740](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L726-L740)）先比较当前显示模式、再检查上一次抓的样本是否已被显示线程消费（`update_flag`），多数调用直接返回，几乎零成本。

### 4.4 set_fs：换采样率的「先停钟、再握手」时序

#### 4.4.1 概念说明

48/96/192kHz 三档采样率不是 STM32 说了算，而是编解码器的分频器说了算（u2-l2）。换档时要同时动两台机器：编解码器要重写 NDAC/MDAC/NADC/MADC/BCLKN 一串分频寄存器，STM32 的 I2S 从机 + DMA 要从任意相位重新同步。如果两边各改各的、不管对方，正在半途的 DMA 会收到一段频率跳变、时快时慢的时钟，状态机可能错位，从此采样永久串位。

所以 `set_fs()` 的设计是**让总线先安静下来**：先把编解码器的时钟分频器全部关掉（BCLK/WCLK 停跳），再停 STM32 的 DMA 交换，等一切沉淀，重启 I2S 交换（此时它安静地等待主机节拍），最后把编解码器按新分频比点亮、解除静音。

#### 4.4.2 核心流程

```
set_fs(48/96/192)                       main.c:205-226
  ├─ 参数不是 48/96/192？→ 直接返回
  ├─ 与当前相同？→ 什么都不做（幂等）
  ├─ ① tlv320aic3204_stop()            停 ADC/DAC/NDAC/MDAC/NADC/MADC → 时钟停跳
  ├─ ② i2sStopExchange(&I2SD2)          停 STM32 侧 DMA
  ├─ ③ chThdSleepMilliseconds(40)       沉淀 40ms（注释：20ms 不够）
  ├─ ④ i2sStartExchange(&I2SD2)         重启 DMA（从机等节拍）
  └─ ⑤ tlv320aic3204_set_fs(fs)         写新分频表 → wait 10ms → 解除静音
```

停机顺序「先停主机时钟、后停从机 DMA」，重启顺序「先起从机 DMA、后起主机时钟」——两边角色对称地交错，保证 DMA 启停永远发生在总线安静期间。

#### 4.4.3 源码精读

- [main.c:203-226](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L203-L226) — `set_fs` 全文：静态变量 `current_fs` 记住当前档位，相同则直接返回（`set_modulation` 每次换模式都会调它，幂等性避免了无谓停机）；中间那句注释「wait a second (not enough in 20ms)」是作者实测留下的坑碑——20ms 不够、40ms 才稳。注意③睡的是**当前线程**（通常是 shell 线程），睡醒前 I2S 回调一次都不会发生，音频静默约 40ms。
- [tlv320aic3204.c:174-181](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/tlv320aic3204.c#L174-L181) — ① 的寄存器表 `conf_data_divoff`：五个写操作依次关左右 ADC、NDAC、MDAC、NADC、MADC——分频器一关，BCLK/WCLK 立即停跳。
- [tlv320aic3204.c:197-211](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/tlv320aic3204.c#L197-L211) — ⑤ 的 `tlv320aic3204_set_fs`：按目标档位选三张分频表之一（[conf_data_clk 48kHz：L81-97](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/tlv320aic3204.c#L81-L97)、96kHz、192kHz各一张），等 10ms 让锁相环/分频器稳定，最后写解除静音表。对比 48k 与 96k 两张表可见差异就在 OSR（128→64）与 BCLKN（28→14）这类分频值上。
- [main.c:700-714](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L700-L714) — shell 的 `fs {48|96|192}` 命令手动直调 `set_fs`，让你能在**不换解调算法**的前提下单独改采样率做实验（比如 192kHz 下听 48kHz 的 AM，通带会宽四倍）。
- [main.c:184-185](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L184-L185) — `set_modulation` 里调 `set_fs` 的位置：表驱动的档位切换，模式与采样率绑定。

#### 4.4.4 代码实践：亲手切换并计时

1. **实践目标**：感受 40ms 停机窗口与档位切换后的回调频率变化。
2. **操作步骤**（需硬件）：在 AM 模式（48kHz）下执行 `fs 192`，仔细听耳机（或观察 `stat` 的 `callback count` 增速），再执行 `fs 48` 切回。
3. **需要观察的现象**：切换瞬间有约 40ms 的静默（第③步睡眠 + 编解码器重启）；之后 `callback count` 每秒增量从 200 变成 800（或反之）。
4. **预期结果**：静默可闻、计数增速四倍。若把 `chThdSleepMilliseconds(40)` 改成 20 重新烧录（作者注释说不够），预期会出现偶发的采样错位/杂音——改完记得改回来。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么停机顺序是「先停编解码器、后停 STM32 的 I2S」，而不是反过来？

**答案**：编解码器是时钟主机。先停它，BCLK/WCLK 立即停止，STM32 从机外设和 DMA 自然无数据可搬，此时再停 DMA 是在「总线安静」状态下进行的干净操作。反过来则 DMA 停在半途时线上还有节拍，重启时机与新时钟的相位无从保证。

**练习 2**：`set_fs` 里的 `if (fs != current_fs)` 幂等判断为什么重要？

**答案**：`set_modulation` 每次都无条件调 `set_fs(mod_table[mod].fs)`。没有这个判断，从 usb 切到 lsb（都是 48kHz）也会触发一次 40ms 静默的停机重启，既难听又无谓。

**练习 3**：`set_fs` 执行期间，`i2s_end_callback` 还会被调用吗？DSP 状态（如滤波器历史、`fm_demod_state`）会怎样？

**答案**：不会——时钟停了、DMA 停了，心跳暂停。但 `signal_process` 的静态状态（biquad 历史样本、NCO 相位、`fm_demod_state.last`）都保留在内存里，恢复后从旧状态继续。跨块状态保持是这套设计的重要特性，u3-l4 会专门讨论。

## 5. 综合实践

### 在 PC 上复活一次 5ms 回调：am_demod 模拟器

**实践目标**：不靠任何硬件，把「回调 → 拆交织 → 混频 → 滤波 → 检波 → 装交织」这条链在你电脑上跑通，并验证一个教科书结论：幅度恒定的复单音经过 AM 解调器，输出包络是常数。这同时是单元三所有算法实验的模板。

**第一步：纸面推导（无硬件必做）**

填写下表（答案见 4.2.2 节核对）：

| 采样率 | 回调周期 | 每秒回调次数 |
|--------|----------|--------------|
| 48kHz | 5ms | 200 |
| 96kHz | ？ | ？ |
| 192kHz | ？ | ？ |

**第二步：搭建模拟器**

新建目录 `i2s_sim/`，先从 dsp.c 原样抽取正余弦表（表恰好是 dsp.c 的第 8 到 265 行）：

```bash
sed -n '8,265p' dsp.c > table.inc
```

再创建 `sim.c`（**以下为示例代码，非项目原有代码**）。思路：把 dsp.c 中 `am_demod` 及其依赖原样拷贝，用等价 C 宏替换 ARM SIMD 内建函数，给 CMSIS 滤波器和显示抓样函数做桩：

```c
/* i2s_sim.c — PC 端 am_demod 模拟器（示例代码，非项目原有代码） */
#include <stdio.h>
#include <stdint.h>
#include <stddef.h>
#include <math.h>

/* 1. 用标准整数运算替换 Cortex-M4 SIMD 内建函数 */
#define __SMUAD(a, b) ((int16_t)((uint32_t)(a)>>16)*(int16_t)((uint32_t)(b)>>16) \
                     + (int16_t)(uint32_t)(a)*(int16_t)(uint32_t)(b))
#define __SMLAD(a, b, c) ((int32_t)(a) \
                     + (int16_t)((uint32_t)(b)>>16)*(int16_t)((uint32_t)(c)>>16) \
                     + (int16_t)(uint32_t)(b)*(int16_t)(uint32_t)(c))
#define __SMLSDX(a, b, c) ((int32_t)(a) \
                     + (int16_t)((uint32_t)(b)>>16)*(int16_t)(uint32_t)(c) \
                     - (int16_t)(uint32_t)(b)*(int16_t)((uint32_t)(c)>>16))
#define __PKHBT(a, b, l) ((((uint32_t)(b)) << 16) | (((uint32_t)(a)) & 0xffff))
#define __SIMD32(p) ((int32_t*)(p))
static inline float _VSQRTF(float x) { return sqrtf(x); }   /* 替换 vsqrt.f32 */

/* 2. 桩：CMSIS 类型 / 滤波器（直通）/ 显示抓样（空） */
typedef int16_t q15_t;
typedef struct { int dummy; } arm_biquad_casd_df1_inst_q15;
static arm_biquad_casd_df1_inst_q15 bq_am_i, bq_am_q;
static void arm_biquad_cascade_df1_q15(const arm_biquad_casd_df1_inst_q15 *S,
                                       q15_t *s, q15_t *d, size_t n)
{ (void)S; while (n--) d[n] = s[n]; }        /* 直通桩：DC 分量不受影响 */
static void disp_fetch_samples(int a,int b,int16_t*c,int16_t*d,size_t e)
{ (void)a;(void)b;(void)c;(void)d;(void)e; }

/* 3. 从 dsp.c 原样拷贝：表 + cos_sin */
#include "table.inc"

static uint32_t
cos_sin(uint16_t phase)                       /* dsp.c L267-281 原样拷贝 */
{
    uint16_t mod = phase & 0xff;
    uint32_t r = __PKHBT(0x0100, mod, 16);
    uint16_t si = phase / 256;
    uint16_t ci = (si + 64) & 0xff;
    uint32_t cd = *(uint32_t *)&cos_sin_table[ci];
    uint32_t sd = *(uint32_t *)&cos_sin_table[si];
    int32_t c = __SMUAD(r, cd);
    int32_t s = __SMUAD(r, sd);
    c /= 256;
    s /= 256;
    return __PKHBT(s, c, 16);
}

/* 与 nanosdr.h 一致的全局（FS 固定 48000，见 4.2.3 的坑提示） */
#define AUDIO_BUFFER_LEN 480
#define FS 48000
#define PHASESTEP(freq) (65536L * freq / FS)
static int16_t buffer[2][AUDIO_BUFFER_LEN], buffer2[2][AUDIO_BUFFER_LEN];
static uint16_t nco1_phase = 0;
static int16_t mode_freqoffset_phasestep = PHASESTEP(10000);

void
am_demod(int16_t *src, int16_t *dst, size_t len)   /* dsp.c L421-467 原样拷贝 */
{
    q15_t *bufi = buffer[0];
    q15_t *bufq = buffer[1];
    int32_t *s = __SIMD32(src);
    int32_t *d = __SIMD32(dst);
    uint32_t i;

    disp_fetch_samples(0, 0, src, NULL, len);

    for (i = 0; i < len/2; i++) {
        uint32_t cossin = cos_sin(nco1_phase);
        nco1_phase -= mode_freqoffset_phasestep;
        uint32_t iq = *s++;
        *bufi++ = __SMLSDX(iq, cossin, 0) >> (15-0);
        *bufq++ = __SMLAD(iq, cossin, 0) >> (15-0);
    }

    arm_biquad_cascade_df1_q15(&bq_am_i, buffer[0], buffer2[0], len/2);
    arm_biquad_cascade_df1_q15(&bq_am_q, buffer[1], buffer2[1], len/2);

    bufi = buffer2[0];
    bufq = buffer2[1];
    for (i = 0; i < len/2; i++) {
      int32_t x = *bufi++;
      int32_t y = *bufq++;
      int32_t z;
      z = (int16_t)_VSQRTF((float)(x*x+y*y));
      if (z > 32767) z = 32767;
      if (z < -32768) z = -32768;
      *d++ = __PKHBT(z, z, 16);
    }
}

/* 4. 模拟一次 5ms 回调：填 rx_buffer → 调 am_demod → 检查 tx_buffer */
int main(void)
{
    static int16_t rx_buffer[AUDIO_BUFFER_LEN * 2];
    static int16_t tx_buffer[AUDIO_BUFFER_LEN * 2];
    const float A = 10000.0f, f = 10000.0f;
    int i;

    /* 复单音 e^{j2pi*f*n/FS} 交织写入：I,Q,I,Q,... */
    for (i = 0; i < AUDIO_BUFFER_LEN; i++) {
        float ph = 2.0f * M_PI * f * i / FS;
        rx_buffer[i*2  ] = (int16_t)(A * cosf(ph));
        rx_buffer[i*2+1] = (int16_t)(A * sinf(ph));
    }

    am_demod(rx_buffer, tx_buffer, 480);     /* 半块 = 480 个 int16 = 240 帧 */

    int16_t vmin = 32767, vmax = -32768;
    for (i = 0; i < 240; i++) {              /* 检查 240 个输出帧 */
        if (tx_buffer[i*2] < vmin) vmin = tx_buffer[i*2];
        if (tx_buffer[i*2] > vmax) vmax = tx_buffer[i*2];
    }
    printf("first output: %d\n", tx_buffer[0]);
    printf("min=%d max=%d ripple=%d\n", vmin, vmax, vmax - vmin);
    return 0;
}
```

编译运行（PC 上用系统 gcc 即可）：

```bash
gcc -o sim sim.c -lm    # 若提示 M_PI 未定义，改用 gcc -std=gnu17 或自行 #define M_PI 3.14159265f
./sim
```

**第三步：需要观察的现象与预期结果**

- 输入是幅度 10000 的复单音，`am_demod` 先用 NCO 把它从 10kHz 搬到 0 频（`nco1_phase` 每样本递减 `PHASESTEP(10000)`），于是 `buffer` 里的 I/Q 成为两个缓变量，\( \sqrt{x^2+y^2} \) 检波后应输出**接近常数的包络**。
- 理论预期：`first output` 在 9900～10000 之间，`ripple`（max−min）只有几个 LSB。误差来自三处：256 点表的插值截断（一阶泰勒插值，误差 \( \approx A \cdot \frac{h^2}{8}, h = \frac{2\pi}{256} \)，约 2～3 LSB）、`PHASESTEP(10000) = 13653` 取整后 NCO 实际频率 9999.76Hz 与信号相差约 0.24Hz（5ms 内相位漂移仅 0.43°，影响可忽略）、以及 `>>15` 的舍位。
- 直通滤波器桩在这里是合法的：混频后信号已位于 0 频（通带中心），低通与否不改变常数输出。真实滤波器行为是 u3-l3 的实验内容。
- 若把 `f` 改成 11000（偏离 10kHz 载频 1kHz），预期输出变为 1kHz 的低频正弦——这正是「AM 信号解调出音频」的最小演示。动手改一下试试。
- 本模拟器在讲义撰写环境中**未实际运行**，运行输出**待本地验证**。

**检查清单**：完成后你应当能回答——`am_demod` 的 `len` 参数为什么传 480 而不是 960？（半块回调，见 4.2.2）为什么交织缓冲要用 `__SIMD32` 转成 int32 指针来读？（一次取一对 I/Q，见 4.2.3）

## 6. 本讲小结

- **缓冲三层结构**：DMA 层 `rx_buffer`/`tx_buffer`（交织，960 个 int16 = 480 帧）→ 算法层 `buffer`/`buffer2`（分离 IQ 平面）→ 元数据层 `buffers_table`（四个取样龙头的格式清单，目前预留未消费）。
- **心跳周期**：DMA 对 480 帧缓冲做半满/全满双中断，每次回调处理 240 帧，周期 \( 240/f_s \)——48kHz 为 5ms（与 nanosdr.h 注释互证），96kHz 为 2.5ms，192kHz 为 1.25ms。
- **一个调用点**：`i2s_end_callback` 是全固件唯一的解调调用点，通过函数指针 `signal_process` 驱动六种算法；`mod_table` 表驱动 + 指针单条赋值 = 零成本热切换。
- **回调即中断**：它运行在中断上下文，顺带用 DWT 计数器测出 `busy_cycles`/`interval_cycles`（`stat` 的 load 指标），并用 LED 引脚提供示波器观测点。
- **采样率切换的握手时序**：`set_fs` 依次「停编解码器时钟 → 停 I2S 交换 → 睡 40ms → 重启 I2S → 重配编解码器并解除静音」，先停钟后握手、幂等跳过同档位。
- **格式换算是易错点**：`FS` 宏写死 48000，`PHASESTEP` 只对 48kHz 模式有效，192kHz 模式另有 `PHASESTEP_NCO19KHz`。

## 7. 下一步学习建议

- **进入单元三**：本讲的回调机制是所有解调算法的舞台，下一讲 [u3-l1] 将拆开 `cos_sin()` 这个你在模拟器里已经用过的查表内插 NCO，讲透定点表示与 SIMD 混频；随后 u3-l3 会用同一个模拟器框架补上真实的 AM 滤波与检波实验。
- **回看一处伏笔**：本讲两次提到 `disp_fetch_samples` 在解调链上「搭便车」——它如何用标志位在中断与线程间无锁传数据，留到 u4-l1（频谱显示）揭晓。
- **并发深读**：如果你关心「中断里跑 DSP、线程里跑显示」的竞争与实时性测量，本讲的 load/LED 现象将在 u5-l1 得到系统分析。
- **动手方向**：把综合实践的模拟器改造成 `fm_demod` 版本（用扫频复信号验证鉴频输出线性），你就提前完成了 u3-l4 的一半作业。
