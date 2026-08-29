# 频谱显示：采样抓取、窗函数与 CFFT

> 所属单元：单元四（看得见摸得着——显示、UI 与配置持久化）
> 前置讲义：u2-l4（ILI9341 LCD 驱动与字库）、u3-l1（定点表示、NCO 与三角函数表）
> 建议同时回顾：u2-l3（I2S 双缓冲与解调回调）

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚 `disp_fetch_samples()` 如何「搭便车」于解调链路，在不增加任何采样通路的情况下，按当前 `spdispmode` 把样本零拷贝地搬进显示缓冲 `SPDISP_BUFFER`。
2. 读懂 `window_*_15to31()` 系列函数：加窗、q15→q31 升位、SIMD 乘法一次完成；并理解三张窗函数表（hamming / blackman-harris / chebychef，注意源码里**没有** hanning 表）如何通过一个指针热切换。
3. 理解 `arm_cfft_radix4_q31()` 的输入输出约定（原地变换、逐级缩放、1024 点输出约为 q21 格式），以及 `disp_init()` 如何初始化 CFFT 实例。
4. 手推 `draw_spectrogram()` 的「功率累加 → log2_i64 → 像素柱」换算，理解为什么 320 个像素能表示 1024 个 FFT bin、为什么刻度近似 1dB/像素、为什么 AM/CW 模式下载波出现在中心右侧 +10kHz 处。
5. 在 PC 上用 numpy 完整复现固件的频谱管线，并与真机屏幕对照。

## 2. 前置知识

### 2.1 为什么需要频谱显示

耳朵只能听解调后的音频，而短波波段里相邻几 kHz 就可能有别的电台。频谱图把一段信号的能量按频率铺开，让操作者「看见」整个波段谁在发声、滤波器通带卡在哪里。对 SDR 来说，频谱显示还是调 IQ 平衡、验证本振频率的最佳仪表。

### 2.2 FFT 与频率分辨率

对 \(N\) 个采样率为 \(f_s\) 的样本做离散傅里叶变换（DFT），得到 \(N\) 个频率点（bin），相邻 bin 间距即分辨率：

\[
\Delta f = \frac{f_s}{N}
\]

本讲中 \(N=1024\)。48kHz 采样时 \(\Delta f = 46.875\,\text{Hz}\)；192kHz 采样时 \(\Delta f = 187.5\,\text{Hz}\)。复数 FFT 的 bin 0 表示直流（0Hz），bin \(k\) 表示 \(+k\Delta f\)，bin \(N-k\) 表示负频率 \(-k\Delta f\)。

DFT 有个隐含假设：输入是周期信号的一个整周期。实际抓一段任意信号，首尾并不衔接，等效于对「矩形窗截断」的信号做 FFT，能量会从主瓣「泄漏」到整个频带，把弱信号淹没在强信号的旁瓣里。

### 2.3 窗函数：用主瓣换旁瓣

解决泄漏的办法是给样本乘一个两端渐零的**窗函数**再做 FFT。代价与收益：

- 主瓣变宽（频率分辨率变差）；
- 旁瓣大幅压低（强信号旁边能看见弱信号）。

三种常见窗的取舍：

| 窗 | 第一旁瓣约 | 主瓣宽度 | 适用 |
|---|---|---|---|
| 矩形（不加窗） | −13 dB | 最窄 | 分辨率优先 |
| Hamming | −43 dB | 中 | 通用默认 |
| Blackman-Harris | −92 dB | 宽 | 动态范围优先 |

固件把三张窗函数表预计算成 q15 整数数组存在 Flash 里（见 4.2），运行时只是查表乘法。

### 2.4 dB 刻度与 8.8 定点对数

人耳和射频工程师都用对数刻度看功率。功率 \(P\) 的分贝值：

\[
P_{\text{dB}} = 10\log_{10}P \approx 3.01 \times \log_2 P \times \tfrac{10}{3.01}
\]

关键是 \(10\log_{10}\) 与 \(\log_2\) 只差一个常数因子，而 \(\log_2\) 对二进制数有极快的整数算法（见 4.4）。固件用 8.8 定点表示 \(\log_2\)：高 8 位是整数部分，低 8 位是小数部分。

### 2.5 复习：本讲用到的既有机制

- **q15/q31 定点**（u3-l1）：int16 表示 \([-1,1)\)，int32 表示 \([-1,1)\)；两个 q15 相乘得 q30，需移位归一化。
- **I2S 解调回调**（u2-l3）：`i2s_end_callback()` 在 DMA 半满/全满中断里调用函数指针 `signal_process`，48kHz 时每次处理 240 帧（`len=480` 个 int16），是全固件唯一的解调入口。
- **LCD 批量绘制**（u2-l4）：`ili9341_draw_bitmap()` 一次送出一个矩形区域的 RGB565 像素，共享 8KB 的 `spi_buffer`。

## 3. 本讲源码地图

| 文件 | 本讲关注的角色 |
|---|---|
| [display.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c) | 主角：窗函数表、`window_*_15to31`、`disp_fetch_samples`、`log2_i64`、`draw_spectrogram`、`disp_process` |
| [nanosdr.h](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h) | `buffer_t`/`BT_*` 枚举、`uistat_t` 中 `spdispmode` 枚举、函数声明 |
| [main.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c) | `buffers_table`、`i2s_end_callback`、`winfunc` shell 命令、`Thread2` 显示线程、`mod_table` |
| [dsp.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c) | 各解调函数中埋设的 4 个抓取钩子（调用点） |
| [CMSIS/DSP_Lib/Source/TransformFunctions/arm_cfft_radix4_q31.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/CMSIS/DSP_Lib/Source/TransformFunctions/arm_cfft_radix4_q31.c) | 1024 点 radix-4 复数 FFT 的库实现（只读，理解接口与格式即可） |
| [ccmfunc.ld](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ccmfunc.ld) | 把整条频谱管线（抓取+CFFT+绘制）放进 CCM 紧耦合内存提速 |
| [python/centsdr.py](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/centsdr.py) | 综合实践中抓缓冲的 PC 端工具 |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：

1. 搭便车的采样抓取（`disp_fetch_samples` 状态机）；
2. 窗函数三张表与 `window_*_15to31` 升位加窗；
3. 1024 点 radix-4 复数 FFT（CMSIS）；
4. 对数刻度柱状谱（`log2_i64` + `draw_spectrogram`）。

---

### 4.1 模块一：搭便车的采样抓取——`disp_fetch_samples` 状态机

#### 4.1.1 概念说明

频谱显示需要一长串样本（1024 个），但固件里**没有**为显示另开一条采样通路。原因很实际：DSP 回调每一拍都在流过大量样本，显示只要「在解调函数搬运/处理这些样本的间隙，顺手复制一份」即可。这就是**搭便车（piggyback）**：

- 不占用额外 ADC 通道或 DMA；
- 不打断实时解调；
- 想看链路上不同位置的信号，就换一个「搭车点」。

于是产生一个设计问题：解调链路上有 4 个天然的观察点（ADC 原始 IQ、一次混频后、滤波后、输出音频），显示一次只想看其中一个。固件用**模式匹配 + 缓冲填充状态机**解决：解调函数在 4 个点各调用一次 `disp_fetch_samples`，把观察点编号传进去；函数内部比对 `uistat.spdispmode`，只有编号匹配的那次调用才真正干活。

#### 4.1.2 核心流程

先看两个枚举的**数值对齐**——这是整个机制的钥匙：

- [nanosdr.h:L100](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L100)：`B_CAPTURE=0, B_IF1=1, B_IF2=2, B_PLAYBACK=3`（解调侧的钩子编号）
- [nanosdr.h:L270](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L270)：`SPDISP_CAP=0, SPDISP_CAP2=1, SPDISP_IF=2, SPDISP_AUD=3`（用户可选的显示模式）

两组值一一对应，于是 `mode != uistat.spdispmode` 一行就完成了「四选一」：

| spdispmode | 抓取点（dsp.c 钩子） | 内容 | 采样率 |
|---|---|---|---|
| SPDISP_CAP (0) | B_CAPTURE | ADC 原始交织 IQ（解调前） | 与当前模式一致 |
| SPDISP_CAP2 (1) | B_IF1 | 第一次 NCO 混频后的 IQ（FM 立体声时为鉴频后实数复合基带） | 同上 |
| SPDISP_IF (2) | B_IF2 | 低通滤波后 / 立体声分离后的中频 | 同上 |
| SPDISP_AUD (3) | B_PLAYBACK | 解调输出音频（L/R 交织，被当作复数 I/Q） | 同上 |

状态机伪代码（完整版见 4.1.3）：

```text
disp_fetch_samples(mode, type, buf0, buf1, buflen):
    若 mode != uistat.spdispmode: 返回          # 别人的观察点，跳过
    若 缓冲已填满(spdisp_fetch_rest == 0):
        若 FLAG_SPDISP 仍置位: 返回              # 显示线程还没画完上一帧，放弃本帧
        复位写指针 / 剩余计数=2048 / 窗表指针回表头
    按 type 裁剪本次可写长度 buflen
    调 window_*_15to31(): 加窗 + q15→q31 + 写入 SPDISP_BUFFER
    若 剩余计数减到 0: 置 FLAG_SPDISP            # 通知显示线程可以画了
```

填充节奏（48kHz 模式，每次回调 240 帧、`buflen=480` 个 int16）：`SPDISP_BUFFER` 有 2048 个 q31 槽位 = 1024 个复数样本，每次搭车消费 480 个槽位，所以约 **5 次回调攒满一帧**，对应 1024/48000 ≈ 21.3ms 的信号；再受显示线程 10ms 轮询约束，屏幕刷新率大约在几十 fps 量级（真机可用 `stat` 命令的 `fps` 字段验证，属 u5-l1 主题）。

#### 4.1.3 源码精读

**① 显示缓冲与游标状态**（display.c）：

```c
#define SPDISP_BUFFER_LENGTH 2048
q31_t SPDISP_BUFFER[SPDISP_BUFFER_LENGTH];

q31_t *spdisp_fetch_current = SPDISP_BUFFER;
int16_t spdisp_fetch_rest = 0;
const int16_t *spdisp_wf_current = winfunc_blackmanharris;
```

[display.c:L599-L604](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L599-L604)——8KB 的目的缓冲和三个游标：写指针、剩余槽位数、窗函数表当前读位置。注意它们是普通全局变量：写者只有一个（I2S 中断上下文），读者（显示线程）通过标志位与之交接，见④。

**② 生产者-消费者的信箱**：

```c
typedef struct {
	q31_t *buffer;
	uint32_t buffer_rest;
	uint8_t update_flag;
} spectrumdisplay_t;

#define FLAG_SPDISP 	(1<<0)
#define FLAG_POWER 		(1<<1)
#define FLAG_UI 		(1<<2)
#define FLAG_AUX_INFO	(1<<3)

spectrumdisplay_t spdispinfo;
```

[display.c:L582-L596](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L582-L596)——一个信箱结构体加 4 个标志位（其余 3 个标志给 u4-l3 的 UI 刷新用，本讲只关心 `FLAG_SPDISP`）。结构体注释里的 "M4 core / SEV" 是从带有双核 M0/M4 的早期项目沿用来的说法，在本工程里没有实际含义，读源码时注意别被带偏。

**③ 抓取函数主体**：

```c
void
disp_fetch_samples(int mode, int type, int16_t *buf0, int16_t *buf1, size_t buflen)
{
    if (mode != uistat.spdispmode)
        return;

	if (spdisp_fetch_rest == 0) {
		if (spdispinfo.update_flag & FLAG_SPDISP) {
			// currently proccessing in M0APP
			return;
		}
        // start to fetch data
		spdisp_fetch_current = SPDISP_BUFFER;
		spdisp_fetch_rest = SPDISP_BUFFER_LENGTH;
		spdisp_wf_current = winfunc_table;
	}
	...
```

[display.c:L724-L740](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L724-L740)——第一行模式过滤；随后是「上一帧画完了吗」的检查：`FLAG_SPDISP` 还亮着说明显示线程没消费完，直接放弃（丢帧保实时，比阻塞等待安全得多——这里可是中断上下文）。

```c
    size_t length = spdisp_fetch_rest;
    if (type == BT_C_INTERLEAVE || type == BT_R_INTERLEAVE) {
      if (buflen > length)
        buflen = length;
      length = buflen;
    } else {
      if (buflen > length / 2)
        buflen = length / 2;
      length = buflen * 2;
    }
```

[display.c:L742-L751](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L742-L751)——长度裁剪分两类：**交织型**（`BT_C_INTERLEAVE`/`BT_R_INTERLEAVE`）的 1 个 int16 恰好产出 1 个 q31 槽位；**平面型**（`BT_IQ`/`BT_REAL`）每个样本要占「实部+虚部」2 个 q31 槽位，所以可用样本数是剩余槽位的一半。

```c
	switch (type) {
    case BT_C_INTERLEAVE:
      window_complex_interleaved_15to31(buf0, buflen);
      break;
    case BT_IQ:
      window_complex_15to31(buf0, buf1, buflen);
      break;
    case BT_REAL:
      window_real_15to31(buf0, buflen);
      break;
    case BT_R_INTERLEAVE:
      window_complex_interleaved_15to31(buf0, buflen);
      break;
	}

	if (spdisp_fetch_rest == 0) {
        // filled up buffer, then analyze and draw waveform
		spdispinfo.buffer = SPDISP_BUFFER;
		spdispinfo.update_flag |= FLAG_SPDISP;
	}
}
```

[display.c:L753-L772](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L753-L772)——按数据布局分发到 4 个加窗函数（见 4.2），填满后把缓冲挂到信箱并亮灯。两个值得注意的细节：

- `BT_R_INTERLEAVE`（输出音频 L/R 交织）复用的是**复数**交织加窗函数——左声道当 I、右声道当 Q，因此 SPDISP_AUD 显示的是复谱，并非共轭对称；
- `window_real_interleaved_15to31` 虽有定义（[display.c:L698-L722](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L698-L722)）但**没有出现在这个 switch 里**，是历史遗留的死代码。

**④ 解调函数里的 4 个钩子**（以 Weaver 法 SSB/CW 为例，dsp.c）：

```c
disp_fetch_samples(B_CAPTURE, BT_C_INTERLEAVE, src, NULL, len);   // 入口：原始 IQ
... // NCO 混频
disp_fetch_samples(B_IF1, BT_IQ, buffer[0], buffer[1], len/2);    // 混频后
... // 低通滤波
disp_fetch_samples(B_IF2, BT_IQ, buffer2[0], buffer2[1], len/2);  // 滤波后
... // 二次混频
disp_fetch_samples(B_PLAYBACK, BT_R_INTERLEAVE, dst, NULL, len);  // 出口：音频
```

见 [dsp.c:L355](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L355)、[dsp.c:L365](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L365)、[dsp.c:L371](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L371)、[dsp.c:L384](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L384)（`demod_weaver`）。`am_demod` 埋点相同（[dsp.c:L430](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L430)、[dsp.c:L439](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L439)、[dsp.c:L445](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L445)、[dsp.c:L466](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L466)）；FM 单声道只有入口/出口两点（[dsp.c:L577](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L577)、[dsp.c:L587](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L587)）；FM 立体声的 IF1/IF2 是**实数**复合基带，用 `BT_REAL`（[dsp.c:L812-L819](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L812-L819)）。钩子之间的信号处理步骤属于单元三的内容，这里只需确认「钩子埋在数据流经之处」。

**⑤ 调用链全景**：

```text
I2S DMA 半满/全满中断
  └─ i2s_end_callback()            main.c:258
       └─ (*signal_process)()      函数指针 = 当前解调模式
            └─ demod_weaver() 等
                 └─ disp_fetch_samples() × 4     ← 中断上下文，生产者
                      └─ window_*_15to31() → SPDISP_BUFFER
Thread2（10ms 轮询）  main.c:906
  └─ disp_process()                display.c:1411
       └─ draw_spectrogram() 等    ← 线程上下文，消费者
```

其中 [main.c:L258-L276](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L258-L276) 是回调，[main.c:L906-L924](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L906-L924) 是显示线程。生产者在 `FLAG_SPDISP` 亮着时不碰缓冲、消费者画完才熄灯——一个朴素的单缓冲无锁交接（标志位本身在中断与线程之间的原子性、以及极端时序下丢一帧的可能，留到 u5-l1 从 RTOS 视角细谈）。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：验证「钩子编号与显示模式编号一一对应」不是巧合，而是设计约束。
2. **操作步骤**：
   - 打开 [nanosdr.h:L100-L109](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L100-L109) 和 [nanosdr.h:L270-L271](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L270-L271)，抄下两组枚举的数值；
   - 用 Grep 在 dsp.c 里搜索全部 `disp_fetch_samples` 调用，按 B_CAPTURE/B_IF1/B_IF2/B_PLAYBACK 分成四组，统计每个解调函数各埋了哪几点；
   - 在真机（或对照 ui.c 逻辑）确认：按住旋钮调到 SPDISP 档后旋转，`uistat.spdispmode` 在 0~3 间循环（[ui.c:L346-L350](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ui.c#L346-L350)），屏幕频谱随之在「原始 IQ / 混频后 / 滤波后 / 音频」四个观察点间切换。
3. **需要观察的现象**：AM 模式下从 CAP 切到 IF，载波从中心右侧 +10kHz「跳」到 0Hz 附近（混频钩子抓的正是搬移后的信号）；切到 AUD 只剩解调音频的窄带。
4. **预期结果**：四个观察点的频谱内容与上表一一对应。真机显示效果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：48kHz 模式下，`disp_fetch_samples(B_CAPTURE, BT_C_INTERLEAVE, src, NULL, 480)` 连续调用几次能把 `SPDISP_BUFFER` 填满？最后一段长度是多少？

**答案**：2048 ÷ 480 = 4 余 128，所以 5 次；前 4 次各写 480 个 q31，第 5 次被裁剪为 128 个（`buflen > length` 触发钳位），恰好对应 1024 个复数样本，且窗函数表指针同步前进 64 帧，不会错位。

**练习 2**：如果显示线程卡死（`FLAG_SPDISP` 永远不清零），频谱显示会怎样？解调会受影响吗？

**答案**：`disp_fetch_samples` 在每次试图开新帧时都会因 `update_flag & FLAG_SPDISP` 提前返回，频谱/瀑布/波形全部冻结；但解调链路本身一行未变，照常出声。这正是「搭便车 + 丢帧保实时」设计的价值：显示永远不能拖垮 DSP。

**练习 3**：FM 立体声模式下选择 SPDISP_IF，抓到的是什么信号？为什么用 `BT_REAL` 而 SSB 模式下同一个钩子用 `BT_IQ`？

**答案**：FM 立体声中 B_IF2 抓的是 `stereo_separate()` 输出的副信道实数序列（[dsp.c:L816](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L816)），而 SSB 的 B_IF2 抓的是滤波后的 I/Q 两路平面数据（[dsp.c:L371](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L371)）。数据布局不同，所以 `type` 参数由解调函数按实际情况传入，抓取函数据此选择正确的加窗函数和长度裁剪规则。

---

### 4.2 模块二：窗函数三张表与 `window_*_15to31` 升位加窗

#### 4.2.1 概念说明

这个模块解决三件事，而且是在**一次内存遍历中同时完成**：

1. **加窗**：样本乘窗系数，压旁瓣；
2. **升位**：把 q15 样本变成 q31，喂给 CFFT（CMSIS 的 q31 版 FFT 要求 q31 输入）；
3. **格式重排**：解调链路的缓冲有「平面 IQ」「交织 IQ」「实数」等多种布局，CFFT 只认「I,Q,I,Q,…交织的 q31」，重排在此一并完成。

窗函数的切换用**函数指针式的数据选择**实现：一个全局指针 `winfunc_table` 指向当前窗表，shell 命令 `winfunc {0|1|2}` 一键切换，无需重编译。

#### 4.2.2 核心流程

```text
winfunc 命令 (main.c cmd_winfunc)
  └─ set_window_function(type)          0=blackmanharris 1=hamming 2=chebychef
       └─ winfunc_table = 对应表地址

下一次开新帧时:
  spdisp_wf_current = winfunc_table     (disp_fetch_samples 复位游标)
  此后每个音频块: window_*_15to31() 逐样本  w[n] * x[n] → q31
```

窗表本身是 **1024 个 q15 系数**的对称序列。以 hamming 为例：首尾约 2621（≈0.08），中心 32767（≈1.0），关于中点对称（[display.c:L118-L249](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L118-L249) 数组前 512 项从 2621 升到 32767，后 512 项镜像回落）。blackman-harris 首尾仅 2（≈0.00006，旁瓣最低），chebychef 首项异常值 67 是表的端点瑕疵，对显示无感。注意源码中**没有名为 `winfunc_hanning` 的表**——学习目标里提到的 "hanning" 实为 `winfunc_hamming`。

三个加窗函数的分工：

| 函数 | 输入布局 | 输出（q31 交织复数） | 每样本开销 |
|---|---|---|---|
| `window_complex_15to31(s1,s2,len)` | 平面 IQ（两数组） | (I·w, Q·w) 对 | 2 次 16×16 乘 |
| `window_complex_interleaved_15to31(src,len)` | 交织 IQ / 交织 L-R | 同上 | 2 次 16×16 乘 |
| `window_real_15to31(s1,len)` | 实数 | (x·w, 0) 对 | 1 次 16×16 乘 + 写 0 |

#### 4.2.3 源码精读

**① 窗表指针与热切换**（display.c）：

```c
const int16_t *winfunc_table = winfunc_hamming;

void
set_window_function(int wf_type)
{
  if (wf_type == 0)
    winfunc_table = winfunc_blackmanharris;
  else if (wf_type == 1)
    winfunc_table = winfunc_hamming;
  else if (wf_type == 2)
    winfunc_table = winfunc_chebychef;
}
```

[display.c:L516-L527](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L516-L527)——默认 hamming；`winfunc_table` 只在开新帧时被读取一次（4.1.3 ③），所以切换在下一帧生效，永远不会出现半帧混用两种窗的撕裂。shell 侧接线在 [main.c:L716-L726](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L716-L726)（`cmd_winfunc`），命令表注册于 [main.c:L893](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L893)。

**② 交织复数版（最常用，CAP/AUD 模式走这条路）**：

```c
inline static void
window_complex_interleaved_15to31(q15_t *src, size_t length)
{
    q31_t *dest = spdisp_fetch_current;
    const q15_t *wf = spdisp_wf_current;
    spdisp_fetch_current += length;
	spdisp_fetch_rest -= length;
    spdisp_wf_current += length / 2;

    length /= 4;
    while (length-- > 0) {
		uint32_t w = *__SIMD32(wf)++;
		uint32_t i1q1 = *__SIMD32(src)++;
		uint32_t i2q2 = *__SIMD32(src)++;
		*dest++ = __SMULBB(i1q1, w) << mag_shift;
		*dest++ = __SMULBT(i1q1, w) << mag_shift;
		*dest++ = __SMULBT(i2q2, w) << mag_shift;
		*dest++ = __SMULTT(i2q2, w) << mag_shift;
	}
}
```

[display.c:L676-L696](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L676-L696)——逐行拆解：

- `length` 单位是 int16 个数；`length/2` 才是帧数，所以窗指针每次只前进 `length/2`（每帧用一个窗系数）；
- 循环每次处理 2 帧：一次读出打包的窗系数 `w`（两个 q15 拼成 32 位）和两个输入打包字；
- `__SMULBB/__SMULBT/__SMULTT` 是 Cortex-M4 的 16×16→32 SIMD 乘法（取操作数的低低/低高/高高半字），四条乘法完成两帧的 I、Q 各自乘窗；
- 结果 `<< mag_shift`：`mag_shift` 平时为 0（[display.c:L533](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L533)），只在波形显示的放大模式被 `draw_waveform` 置 3 或 6（[display.c:L913-L918](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L913-L918)），给频谱用时可视为不存在；
- 注意乘积是 q15×q15 = q30，直接放进 q31 容器（差 1 位移位，CFFT 内部有保护位，不碍事）。

**③ 平面复数版（IF1/IF2 模式，SSB/AM）**：

[display.c:L634-L654](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L634-L654)——`window_complex_15to31(s1, s2, length)` 从两个分离数组各取一样本，输出交织的 `(I·w, Q·w)`；游标前进 `length*2`（每样本占 2 个 q31）。实数版 [display.c:L656-L674](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L656-L674) 则在虚部位置写 0，把实信号零填充成复信号（FM 立体声 IF 模式使用）。

**④ SIMD 辅助内建函数**：[display.c:L608-L631](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L608-L631) 用内联汇编定义了 `__SMULBB/__SMULTT/__SMULTB/__SMULBT` 四个乘法（CMSIS 头文件只提供了部分，这里补齐），语义是「从两个 32 位操作数中各取指定半字做有符号 16×16 乘」。系统性梳理见 u5-l2。

#### 4.2.4 代码实践

1. **实践目标**：用 PC 验证「窗表数值 = 理论窗函数的 q15 量化」，并观察三种窗对同一信号频谱的影响。
2. **操作步骤**（以下为**示例代码**，可直接运行）：

```python
# check_window.py —— 验证 display.c 中的 hamming 窗表
import numpy as np

# 从 display.c 复制 winfunc_hamming 的前几个值即可起步；完整验证请整表复制
head = [2621, 2622, 2622, 2624, 2626, 2628, 2632, 2635]
n     = np.arange(len(head))
ref   = 0.54 - 0.46*np.cos(2*np.pi*n/1023)   # hamming 理论式（N=1024）
q15   = np.round(ref * 32767)

print("固件表值:", head)
print("理论量化:", q15.astype(int).tolist())
print("最大误差(首8点):", int(np.max(np.abs(np.array(head) - q15))))
```

   然后把三种窗（用 numpy 生成等价的 1024 点 hamming / blackman-harris(`0.35875−0.48829cos+0.14128cos2−0.01168cos3`) / chebwin）分别乘同一段含强 tone + 弱 tone 的信号做 FFT，画 dB 谱对比旁瓣。
3. **需要观察的现象**：hamming 表首 8 点与理论量化的误差 ≤ 1 LSB；blackman-harris 的旁瓣比 hamming 低约 50dB，但主瓣更宽，弱 tone 更容易被主瓣淹没。
4. **预期结果**：理论量化与固件表逐点吻合（误差在量化舍入范围内）。
5. 三种窗在真机上的屏幕效果差异**待本地验证**（`winfunc 0|1|2` 后观察频谱底噪和邻近信号）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `window_complex_interleaved_15to31` 里窗指针每次只前进 `length/2`，而写指针前进 `length`？

**答案**：`length` 以 int16 计。交织 IQ 中每帧占 2 个 int16（I 和 Q），却只对应 1 个窗系数；输出端每帧要写 2 个 q31（I·w 和 Q·w）。所以窗指针按帧数前进（`length/2`），写指针按输出槽位数前进（`length`）。

**练习 2**：切窗后正赶上缓冲填到一半，会出现半帧旧窗、半帧新窗的画面吗？

**答案**：不会。`winfunc_table` 只在开新帧那一刻被读入 `spdisp_wf_current`（[display.c:L739](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L739)），帧内全程使用这个游标快照，切换最早在下一帧生效。

**练习 3**：`window_real_15to31` 把实信号零填充成复信号再做 CFFT，频谱是什么形状？与直接做实数 FFT（RFFT）有何区别？

**答案**：零填充复信号的复谱上下半区互为镜像（bin k 与 bin N−k 幅度相同），一半信息冗余；RFFT 则只输出一半 bin。固件选择复 FFT + 零填充是为了与 IQ 路径共用同一套 `arm_cfft_radix4_q31` 和绘制代码，代价是多算了一半点数——对 1024 点、非实时关键的显示通路来说是可以接受的取舍。

---

### 4.3 模块三：1024 点 radix-4 复数 FFT——CMSIS `arm_cfft_radix4_q31`

#### 4.3.1 概念说明

FFT 是 DFT 的快速算法，这里不推导蝶形公式，只需建立两个直觉：

- **radix-4**：每次把序列按位置模 4 拆成 4 组，合并时做 4 点蝶形。1024 = 4⁵，共 5 级，比 radix-2 少一级乘法；
- **定点防溢出**：每级蝶形最多让数据增长 2 倍，5 级就是 32 倍。CMSIS 的策略是**每级把数据右移缩位**（首级先给 4 个保护位），保证绝不饱和，代价是输出比输入「小」了若干位——格式从 q31 变成约 q21，**比较幅度时必须记住这个缩放**（它最终被 4.4 中的常数 36 吸收）。

CMSIS-DSP 是 ARM 官方定点信号库，本工程只取 5 个文件（[Makefile:L113-L117](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/Makefile#L113-L117)），CFFT 相关的是 `arm_cfft_radix4_init_q31.c`、`arm_cfft_radix4_q31.c`、`arm_bitreversal.c` 和公共表 `arm_common_tables.c`（旋转因子、位反转表都在其中）。我们以**库使用者**视角读它：接口约定是什么、数据格式如何变化。

#### 4.3.2 核心流程

```text
disp_init()                                  display.c:1471
  └─ arm_cfft_radix4_init_q31(&cfft_inst, 1024, FALSE, TRUE)
       参数: 长度1024 / 正变换(非IFFT) / 启用位反转
       产物: 实例内填好旋转因子表指针、位反转表指针

draw_spectrogram()                           display.c:783
  └─ arm_cfft_radix4_q31(&cfft_inst, buf)
       输入: SPDISP_BUFFER, 2048 个 q31 (I,Q,I,Q,…交织)
       行为: 原地变换, 输出覆盖输入
       输出: 1024 个复数 bin, 格式约 q21 (内部逐级缩位)
       bin k ↔ 频率 k·fs/1024, bin N-k ↔ 负频率
```

#### 4.3.3 源码精读

**① 初始化**：

```c
void
disp_init(void)
{
  arm_cfft_radix4_init_q31(&cfft_inst, 1024, FALSE, TRUE);
  waterfall_init();
  clear_background();
  spdispinfo.update_flag = FLAG_UI;
}
```

[display.c:L1469-L1475](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L1469-L1475)——实例 `cfft_inst` 是全局变量（[display.c:L529](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L529)），开机初始化一次，之后每帧复用（查表结构不变，无需重复 init）。`disp_init` 在 `main()` 里紧随 `ili9341_init()` 之后被调用（[main.c:L1028](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L1028)），`lcd` 旋转命令也会重新调用它做全量重画（[main.c:L871](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L871)）——重复 init 无害。

**② 库入口的分发逻辑**：

```c
void arm_cfft_radix4_q31(
  const arm_cfft_radix4_instance_q31 * S,
  q31_t * pSrc)
{
  if(S->ifftFlag == 1u)
  {
    arm_radix4_butterfly_inverse_q31(pSrc, S->fftLen, S->pTwiddle,
                                     S->twidCoefModifier);
  }
  else
  {
    arm_radix4_butterfly_q31(pSrc, S->fftLen, S->pTwiddle,
                             S->twidCoefModifier);
  }

  if(S->bitReverseFlag == 1u)
  {
    arm_bitreversal_q31(pSrc, S->fftLen, S->bitRevFactor, S->pBitRevTable);
  }
}
```

[arm_cfft_radix4_q31.c:L90-L114](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/CMSIS/DSP_Lib/Source/TransformFunctions/arm_cfft_radix4_q31.c#L90-L114)——三步走：正/逆蝶形 + 位反转重排。输入缓冲须为 `2*fftLen` 个 q31 的**交织复数**，且**原地**覆盖输出。文件头注释（[L74-L87](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/CMSIS/DSP_Lib/Source/TransformFunctions/arm_cfft_radix4_q31.c#L74-L87)）明确两点，务必记住：

- 该函数在新版 CMSIS 中已标记 deprecated（建议 `arm_cfft_q31`），本工程用的是旧接口，功能完整只是不再演进；
- **内部每级缩放 2 倍防饱和，输出格式随 FFT 长度不同而不同**。

**③ 缩放在哪里发生**：

```c
    /* input is in 1.31(q31) format and provide 4 guard bits for the input */
    r1 = (pSi0[0] >> 4u) + (pSi2[0] >> 4u);
```

[arm_cfft_radix4_q31.c:L447-L461](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/CMSIS/DSP_Lib/Source/TransformFunctions/arm_cfft_radix4_q31.c#L447-L461)——首级先 `>>4` 给 4 个保护位；中间级每级 `>>2`（如 [L595](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/CMSIS/DSP_Lib/Source/TransformFunctions/arm_cfft_radix4_q31.c#L595)）；文件在两处注释写明最终格式：1024 点输出为 **11.21(q21)**（[L658-L663](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/CMSIS/DSP_Lib/Source/TransformFunctions/arm_cfft_radix4_q31.c#L658-L663) 与 [L759-L762](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/CMSIS/DSP_Lib/Source/TransformFunctions/arm_cfft_radix4_q31.c#L759-L762)）。radix-4 蝶形的数学定义在同文件 [L121-L152](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/CMSIS/DSP_Lib/Source/TransformFunctions/arm_cfft_radix4_q31.c#L121-L152) 的注释块里，供有兴趣的读者对照。

**④ 谁在消费输出**：`draw_spectrogram()`（4.4）读 `buf[(i&1023)*2]` 与 `buf[(i&1023)*2+1]`——正是「第 i 个 bin 的实部/虚部」这个交织布局。另外注意 [ccmfunc.ld:L9-L14](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/ccmfunc.ld#L9-L14) 把 `arm_cfft_radix4_q31`、`disp_fetch_samples`、`draw_spectrogram` 等整条频谱管线的代码段都放进了 CCM 紧耦合 RAM（零等待存储器）以换取速度，链接细节属 u5-l3 主题。

#### 4.3.4 代码实践

1. **实践目标**：验证「1024 点 CFFT 输出约缩到 q21」这一文档说法。
2. **操作步骤**（**示例代码**，PC 端模拟）：

```c
// cfft_scale.c —— 用幅度估算 CMSIS radix-4 q31 的总缩放
// 思路: 单频满幅 q31 复正弦的能量集中于一个 bin,
// 比较该 bin 幅度与 N/2 理论值, 差值即缩放位数。
// PC 上无 CMSIS, 可用等价的浮点 FFT 后除以 2^10 对照:
//   固件: 输出 ≈ 输入·N/2 / 1024  (即小 2^10 倍)
// 结论对照点: display.c 的 draw_spectrogram 用常数 36 吸收此缩放
```

   有条件的话在固件里临时加一行 chprintf（属源码修改，请只在本地实验分支做）：CFFT 后打印 `buf[0]` 与输入直流分量之比。
3. **需要观察的现象**：满幅直流输入时，bin 0 的输出约为输入的 1024/2/1024 = 0.5 倍（若不缩位应为 512 倍），印证 `>>10`。
4. **预期结果**：与文件注释「1024 点输出为 11.21 格式」一致；真机数值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：`arm_cfft_radix4_init_q31` 的三个数值参数在本工程里各是什么含义？

**答案**：`1024` 是 FFT 长度（对应 `SPDISP_BUFFER` 的 2048 个 q31）；`FALSE` 是 ifftFlag，选择正变换（我们做频谱分析，不需要逆变换）；`TRUE` 是 bitReverseFlag，启用位反转，使输出按自然频率顺序排列（否则是码位倒序，绘制代码的 `i&1023` 索引就乱了）。

**练习 2**：为什么频谱显示选 q31 版 CFFT 而不是速度更快的 q15 版？

**答案**：显示链路要看 60dB 以上的动态范围（邻道弱信号 vs 强载波）。q15 只有约 90dB 理论动态、实际可用更少，且窗后还要与强信号同场竞争；q31 配合 int64 功率累加和 8.8 定点对数，能稳定呈现 64 像素（≈60dB）刻度。显示每帧只算一次 1024 点，q31 的开销可接受——这是「精度优先、算力够用」的典型工程取舍。

---

### 4.4 模块四：对数刻度柱状谱——`log2_i64` 与 `draw_spectrogram`

#### 4.4.1 概念说明

CFFT 输出 1024 个复数 bin，但屏幕只有 320 列像素、64 行高度。本模块回答两个「怎么画」：

1. **横向 1024→320**：查一张几何参数表，每个像素取 `stride` 个相邻 bin 的功率求和（欠采样显示）；
2. **纵向线性→对数**：功率是个巨大的整数（最大可到 \(2^{63}\)），直接线性映射会把一切压到底部。做法是 \(\log_2\) 后做一次线性变换，让 1 个像素 ≈ 1dB。

其中 \(\log_2\) 用纯整数实现：规格化 + 查位 + 一阶近似，一次乘除都没有。

#### 4.4.2 核心流程

先看几何参数表（[display.c:L562-L579](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L562-L579)），它定义了每个 spdispmode 的显示几何：

| 模式 | offset(起始bin) | stride | origin(中心px) | 48kHz 视野 | 192kHz 视野 |
|---|---|---|---|---|---|
| CAP / CAP2 | −480 | 3 | 160 | ±22.5kHz | ±90kHz |
| IF | −320 | 2 | 160 | ±15kHz | ±60kHz |
| AUD | 0 | 1 | 0 | 0~15kHz | 0~60kHz |

（源码注释 `320pixel = 1024pt = 48kHz / 35.55 pixel = 5kHz`，即 CAP 模式 48kHz 下 1 像素 = 3bin × 46.875Hz = 140.6Hz，5kHz 刻度线间隔 5000/140.6 ≈ 35.55 像素。192kHz 有独立表 `spdispparam_tbl_192khz`，由宏 `GET_SPDISP_PARAM` 按 `uistat.fs==192` 选择。）

绘柱流程：

```text
draw_spectrogram():
  CFFT(buf)                                  # 原地, 1024 bin, 交织 q21
  对屏幕每一列 x = 0..319:
    i = offset + x*stride                    # 该列对应的起始 bin
    acc = Σ (bin_i.re² + bin_i.im²)          # stride 个 bin 的功率和, int64
    v = (log2_i64(acc) - 36*256) / 77        # 对数 + 线性映射, ≈1dB/像素
    v 钳位到 [0, 64]
    画柱: v 个白色像素 + (64-v) 个背景像素
    每 10 行画一格深灰刻度线                  # 10dB 网格
  调谐Marker: 在 tune_pos 列涂绿             # AM/CW 时 = origin + 71px
```

#### 4.4.3 源码精读

**① 整数 log2（8.8 定点）**：

```c
static inline uint16_t
log2_i64(uint64_t x)
{
	uint64_t mask = 0xffffffff00000000;
	uint16_t bit = 32;
	int16_t y = 63;
	...
	// 32/16/8/4/2/1 六步二分, 把 x 左移到最高位为 1, 每步更新整数部分 y
	...
	// msb should be 1. take next 8 bits.
	i = (x >> (63-8)) & 0xff;
	// lookup logarythm table
	return (y << 8) | i;
}
```

[display.c:L60-L114](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L60-L114)——算法分两段：

- **整数部分**：六步二分规格化（先看高 32 位是否全零，是则整体左移 32 位并从 y=63 扣 32；再 16、8、4、2、1 位递归），结束时 MSB=1，`y` 即 \(\lfloor\log_2 x\rfloor\)；
- **小数部分**：规格化后 \(x = 1.f \times 2^y\)（f 为尾数小数），取 MSB 之下 8 位作为小数字节直接拼进低 8 位。这利用了一阶近似 \(\log_2(1+f) \approx f\)，在 f∈[0,1) 上最大误差约 0.0016（换算功率约 0.005dB），对显示完全够用。注释写的 "lookup logarythm table"（含拼写错误）名不副实——**并没有查表**，低 8 位本身就是答案。32 位版本 `log2_q31`（[display.c:L10-L58](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L10-L58)）同构，供功率计用（u4-l3）。

**② 主绘制函数**：

```c
void
draw_spectrogram(void)
{
	q31_t *buf = spdispinfo.buffer;
	arm_cfft_radix4_q31(&cfft_inst, buf);

	uint16_t mode = uistat.spdispmode;
    const spectrumdisplay_param_t *param = GET_SPDISP_PARAM(mode);
	int i = param->offset;
	int16_t stride = param->stride;
    int16_t tune_pos = param->origin;
    if (uistat.spdispmode == 0)
      tune_pos += (int)mode_freq_offset*1024 / (48000 * stride);
```

[display.c:L779-L798](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L779-L798)——先 CFFT（一帧一次），再按参数表定位起始 bin 与中心像素。注意 `mode_freq_offset` 是 AM/CW 的 10kHz 频偏（[main.c:L114](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L114)、由 [main.c:L170-L177](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L170-L177) 的 mod_table 设定）：CAP 模式下本振比信号低 10kHz（u2-l1/u3-l3 的结论），所以信号出现在 +10kHz 处，绿色 Marker 挪到 \(160 + 10000 \times 1024 / (48000 \times 3) \approx 160 + 71 = 231\) 像素列。

```c
	uint16_t (*block)[32] = (uint16_t (*)[32])spi_buffer;
	int sx, x, y;
	for (sx = 0; sx < 320; sx += 32) {
		for (x = 0; x < 32; x++) {
          ...
          int i0 = i;
          int64_t acc = 0;
          for (; i < i0 + stride; i++) {
			q31_t ii = buf[(i&1023)*2];
			q31_t qq = buf[(i&1023)*2+1];
            acc += (int64_t)ii*ii + (int64_t)qq*qq;
          }
          int v = (log2_i64(acc) - (36<<8)) / 77; // 1dB/pixel
			if (v > 64) v = 64;
			if (v < 0) v = 0;
			for (y = 0; y < v; y++)
				block[63-y][x] = 0xffff;
			for ( ; y < 64; y++) {
              block[63-y][x] = bg;
              if (bg == 0 && y % 10 == 0)
                block[63-y][x] = RGB565(15,15,15);
			}
		}
		ili9341_draw_bitmap(sx, 72, 32, 64, (uint16_t*)block);
	}
}
```

[display.c:L797-L830](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L797-L830)——几个关键点：

- **`i & 1023` 负频率换算**：`i` 从 −480 起步，C 里 `-480 & 1023 = 544 = 1024 - 480`，恰好是「−480 对应的 bin」，二进制按位与天然完成了 mod 1024；
- **功率累加用 int64**：单个 \(|X|^2\) 可达 \(2^{62}\)，3 个 bin 求和必须 64 位；
- **dB 映射**：\(\log_2\) 差 1 即功率差 3.01dB。\(v\) 每加 1 对应 \(\log_2\) 增加 \(77/256 \approx 0.30\)，即 \(10\log_{10}2^{0.30} \approx 0.91\,\text{dB}\)——注释写 `1dB/pixel` 是工程取整（此前被注释掉的 `>>6` 版本约 0.75dB/像素，作者迭代成 /77 更接近 1dB）；
- **常数 36**：\((36<<8)\) 一次性吸收三件事——CFFT 内部 \(2^{-10}\) 缩放、窗函数相干增益（hamming 约 0.54，功率约 −10.7dB）、以及 q30 输入的半位差，把「满幅信号」大致对齐到柱高 25~64 像素区间，属于经验整定值；
- **分块送屏**：320×64 的图拆成 10 块 32×64（2048 像素）填进共享 `spi_buffer`（u2-l4 讲过 4096 像素上限），逐块 `ili9341_draw_bitmap`，绘制起点 y=72；
- **10dB 网格**：`y % 10 == 0` 时该像素画深灰（`RGB565(15,15,15)`），配合 ≈1dB/像素即每 10 行一条网格线。

**③ 消费侧的调度**（`disp_process`）：

```c
void
disp_process(void)
{
  if (spdispinfo.update_flag & FLAG_SPDISP) {
    draw_waveform();
    draw_spectrogram();
    draw_waterfall();
    spdispinfo.update_flag &= ~FLAG_SPDISP;
  }
  ...
```

[display.c:L1411-L1448](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L1411-L1448)——Thread2 每 10ms 调一次；`FLAG_SPDISP` 置位时依次画波形、频谱、瀑布（各自内部按 `wfdispmode` 决定是否真正动笔，如 `draw_waterfall` 开头就检查 `uistat.wfdispmode != WATERFALL` 则返回），**全部画完才清标志**，这就保证了 4.1 中生产者不会在绘制中途覆写缓冲。频谱区和瀑布/波形区共享同一份 `spdispinfo.buffer` 数据源，只是渲染方式不同（u4-l2 主题）。

#### 4.4.4 代码实践

1. **实践目标**：亲手算出「AM 模式、CAP 显示、48kHz 采样」时强电台在屏幕上的像素列，与固件公式对账。
2. **操作步骤**：
   - 读 [main.c:L170-L177](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L170-L177)：AM 的 `freq_offset = AM_FREQ_OFFSET = 10000`；
   - 读 [nanosdr.h:L126](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L126) 确认宏值；
   - 代入 [display.c:L795-L796](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/display.c#L795-L796)：\(tune\_pos = 160 + \frac{10000 \times 1024}{48000 \times 3} = 160 + 71.1 \to 231\)；
   - 用频率换算复核：+10kHz ÷ 140.6Hz/px = 71.1px，中心 160px，一致。
3. **需要观察的现象**：真机 AM 模式收强台，频谱主峰与绿色 Marker 都在第 231 列附近。
4. **预期结果**：Marker 与主峰重合；若你用的是 SSB（offset=0），两者都在中心 160 列。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`draw_spectrogram` 中 `tune_pos` 的计算硬编码了 `48000`。什么情况下这会算错？实际会出问题吗？

**答案**：当采样率非 48kHz 且 `mode_freq_offset ≠ 0` 同时成立时会算错。但查 mod_table：offset 非 0 的只有 AM/CW，两者固定 fs=48；FM 系列offset=0，该项恒为 0。所以现实中不会触发——这是「依赖隐式约束」的写法，如果将来有人做 96kHz 的 AM 模式就会踩坑（可作为 u5-l4 扩展实践的一个修复点）。

**练习 2**：为什么功率累加循环里要用 `(i&1023)*2` 和 `*2+1` 两个下标，而不是直接 `buf[i]`？

**答案**：CFFT 输出是 I,Q 交织的复数序列：第 i 个 bin 的实部在 `2i`、虚部在 `2i+1`。`(i&1023)` 同时完成负 bin 到正索引的回卷。功率 \(P_i = ii^2 + qq^2\) 需要实虚两部分别平方再相加。

**练习 3**：把 `(log2_i64(acc) - (36<<8)) / 77` 的 77 改成 154，屏幕会怎么变？

**答案**：154 = 2×77，每像素对应的 dB 数翻倍（≈1.8dB/px），同样的信号柱高减半，相当于「增益降 6dB 的显示」；同时 10 行一格的网格实际变成 18dB/格，与「10dB/10pixel」注释不再相符。这个参数就是频谱仪的「dB/格」旋钮。

---

## 5. 综合实践：在 PC 上复现 CentSDR 频谱管线

**任务**：固件的频谱是一条「加窗 → 1024 点 CFFT → 功率累加 → 整数 log2 → 像素柱」管线。请用 Python/numpy 把它原样复现，先用合成数据离线跑通（无硬件也可完成），有真机时再抓真数据对照，验证 AM/CW 模式下载波出现在 +10kHz。

### 5.1 离线版（无硬件，可直接运行）

以下为**示例代码** `replay_spectrum.py`：

```python
import numpy as np

# ---- 1. 合成 "CAP 模式看到的" IQ: AM 模式本振低于信号 10kHz ----
fs, N = 48000, 1024
n = np.arange(N)
# 强载波在 +10kHz, 幅度 0.8; 旁边 -8kHz 处一个弱 tone, 幅度 0.001 (-58dB)
iq = 0.8*np.exp(2j*np.pi*10000*n/fs) + 0.001*np.exp(2j*np.pi*(-8000)*n/fs)
iq = np.round(iq * 32767).astype(np.int16)          # 量化到 q15 (rx_buffer 同款)

# ---- 2. 加窗: 复刻 window_complex_interleaved_15to31 ----
w = np.round((0.54 - 0.46*np.cos(2*np.pi*np.arange(N)/(N-1))) * 32767)  # winfunc_hamming
x = (iq.real.astype(np.int32) * w.astype(np.int32)     # q15*q15 = q30
     + 1j*iq.imag.astype(np.int32)*w.astype(np.int32))

# ---- 3. CFFT: 原地 1024 点复数 FFT (numpy 代替 arm_cfft_radix4_q31) ----
X = np.fft.fft(x)                                     # 输出约 q21, 但对数刻度下
                                                      # 常数偏移会吸收缩放差
# ---- 4. 功率累加 + log2 + 映射: 复刻 draw_spectrogram (CAP 模式) ----
OFFSET, STRIDE = -480, 3
cols = []
for px in range(320):
    acc = np.int64(0)
    for k in range(STRIDE):
        i = (OFFSET + px*STRIDE + k) & 1023
        acc += np.int64(X[i].real)**2 + np.int64(X[i].imag)**2
    if acc <= 0:
        cols.append(0); continue
    # log2_i64 的等价: floor(log2(acc)*256), 一阶近似误差可忽略
    v = int((np.floor(np.log2(np.float64(acc))*256) - 36*256) / 77)
    cols.append(min(64, max(0, v)))

# ---- 5. 验证: 强载波应出现在 +10kHz 列 ----
peak = int(np.argmax(cols))
freq = (OFFSET + peak*STRIDE + STRIDE//2) * fs / N
print(f"峰值列 = {peak}, 中心=160, 对应频率 ≈ {freq:.0f} Hz")
assert abs(freq - 10000) < 150, "载波未落在 +10kHz!"

# ---- 6. 画成与屏幕同款的柱状图 ----
import matplotlib.pyplot as plt
plt.figure(figsize=(10, 2.5))
plt.bar(range(320), cols, width=1.0, color='white',
        edgecolor='none')
plt.gca().set_facecolor('black')
plt.axvline(160+71, color='lime', lw=0.8)   # 固件的 tune_pos=231 绿色 Marker
plt.xlabel('pixel'); plt.ylabel('level (≈1dB/px)')
plt.show()
```

**核对清单**（预期结果，可离线验证）：

- 峰值列 ≈ 231（160 + 71），频率 ≈ +10kHz，断言通过；
- 绿色 Marker 线恰好压在主峰上；
- −8kHz 处的弱 tone（列 ≈ 160 − 8×35.55/5×... 自行换算：−8000/140.6 ≈ −57px → 列 ≈ 103）也能看到一根矮柱——这正是窗函数压旁瓣的意义；可把窗换成矩形（`w=ones`）重跑，弱 tone 会被强载波的旁瓣淹没。

### 5.2 真机版（需要硬件，**待本地验证**）

1. **环境**：python 2.7 + pyserial + numpy + matplotlib（`python/centsdr.py` 是 Python 2 代码：有 `print x` 语句和 `decode('hex')` 调用）。
2. **操作步骤**：
   - 连接 USB CDC 串口，`python centsdr.py -M am -F 567000` 设 AM 模式与频率（[-M/-F 选项见 python/centsdr.py:L125-L142](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/centsdr.py#L125-L142)）；
   - `python centsdr.py -p 0` 连续绘制 rx_buffer（CAP 观察点，240 帧交织 IQ；[-p 的取数与组复数逻辑见 python/centsdr.py:L155-L183](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/centsdr.py#L155-L183)，其内核是 `data` 命令 → `fetch_array()` → [python/centsdr.py:L77-L96](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/centsdr.py#L77-L96)，对应固件 [main.c:L315-L349](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L315-L349) 的 `cmd_data`）；
   - 把 5.1 脚本的数据源换成 `sdr.fetch_array(0)` 抓到的 240 帧（注意：串口转储期间音频流仍在跑，缓冲会被回调覆写，240 帧内部可能不连续——这是该抓取方式的固有局限；且 240 点 FFT 的分辨率 200Hz 低于屏幕的 1024 点，形状一致但细节更粗）；
   - `-p 2` / `-p 3` 分别抓 `buffer[0]` / `buffer2[0]`（IF1/IF2 观察点的 I 路，对应屏幕 CAP2/IF 模式），对比观察 NCO 混频前后频谱的搬移。
3. **需要观察的现象**：AM 强台的谱峰在 +10kHz（240 点 FFT 即第 50 个 bin 附近）；切 `-p 2` 后同一信号聚到 0Hz 附近。
4. **预期结果**：与屏幕 CAP/CAP2 显示一致；数值细节**待本地验证**。

## 6. 本讲小结

- **搭便车抓取**：`disp_fetch_samples` 利用钩子编号与 `uistat.spdispmode` 的数值对齐实现「四选一」，在解调数据流经之处零拷贝抄近道攒样本；`FLAG_SPDISP` 标志在中断（生产者）与 Thread2（消费者）之间交接，忙则丢帧、绝不阻塞 DSP。
- **一次遍历三件事**：`window_*_15to31` 家族在抄样本的同时完成加窗、q15→q31 升位与交织重排，靠 `__SMULBB/__SMULTT` 等 SIMD 16×16 乘法压开销；三张 q15 窗表（hamming/blackman-harris/chebychef）经 `winfunc_table` 指针热切换，`winfunc` 命令直达。
- **库的正确用法**：`arm_cfft_radix4_q31` 原地变换 2048 个交织 q31；内部逐级缩位使 1024 点输出约为 q21——比较幅度时必须计账，本工程把这笔账统一记进了常数 36。
- **对数即性价比**：`log2_i64` 用二分规格化 + 一阶近似 \( \log_2(1+f)\approx f \) 免掉查表和浮点；`(log2−36×256)/77` 把 60dB 动态范围压进 64 像素，约 1dB/像素。
- **几何即参数表**：1024 bin → 320 像素的映射、±22.5kHz/±15kHz/0~15kHz 的视野切换、+10kHz 调谐 Marker，全部由 `spdispparam_tbl`（48k/192k 两套）参数化，改视野只需改表。
- **速度兜底**：整条管线（`disp_fetch_samples`、`arm_cfft_radix4_q31`、`draw_spectrogram`）被链接脚本放进 CCM 紧耦合内存。

## 7. 下一步学习建议

本讲只讲了「怎么把频谱画出来」，同一份 `spdispinfo.buffer` 还有两种渲染方式在下一讲 u4-l2（瀑布图与波形绘制）精读：`draw_waterfall()` 的逐行滚动与 `pick_color()` 伪彩色、`draw_waveform()` 的 `v2ypos` 坐标映射，以及 `mag_shift` 在波形放大模式中扮演的角色。之后 u4-l3 会补齐屏幕的信息架构（`FLAG_UI`/`FLAG_POWER` 标志位驱动的增量刷新、频率与功率显示）。如果你对本讲的中断-线程交接和 `stat` 里的 `load`/`fps` 指标感兴趣，可以提前跳读 u5-l1 的并发分析；对 SIMD 乘法族想系统梳理则见 u5-l2。
