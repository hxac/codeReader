# 调幅解调：频移、低通与幅度检波（u3-l3）

> 前置讲义：u3-l1（q15 定点、NCO 与 cos_sin 表）、u3-l2（demod_weaver 与 CMSIS biquad）、u2-l3（I2S 回调机制）。本讲是单元三的第三站。

## 1. 本讲目标

学完本讲你应该能够：

1. 沿着 `set_tune → mod_table → set_modulation → am_demod` 这条链，讲清楚 `AM_FREQ_OFFSET = 10000` 在整机里的**两级作用**：本振先把载波放到基带 +10kHz 的低中频上，解调时 NCO 再用同样的 10kHz 把信号搬回 0Hz；
2. 读懂 `am_demod()` 的三段流水线：NCO 复数混频 → 6 阶椭圆低通 → `vsqrt.f32` 取模得到包络；
3. 解释为什么包络检波对载波相位旋转天然免疫——`PHASESTEP` 整数舍入留下的约 0.24Hz 残差为何无害；
4. 对比 `_VSQRTF` 内联汇编与库函数 `sqrtf` 的开销差异；
5. 分析编译期 `#if` 两种实现（带频移+滤波 vs 直接检波+`DCOFFSET`）各自的适用条件与取舍。

## 2. 前置知识

### 2.1 AM 信号与「包络」

调幅（Amplitude Modulation）是最古老的无线电调制：让高频载波的**幅度**随声音起伏，频率和相位保持不变。数学上一个单音调制的 AM 信号写作

\[ s(t) = A_c\,\bigl(1 + m\cos(2\pi f_m t)\bigr)\cos(2\pi f_c t) \]

其中 \(A_c\) 是载波幅度，\(f_c\) 是载波频率，\(f_m\) 是调制音频率，\(m\) 是调幅度（\(0 \le m \le 1\)）。\(A_c(1+m\cos(2\pi f_m t))\) 这一项就是**包络**——载波峰值的轨迹。收音机解调 AM，本质上就是把这个包络提取出来。

### 2.2 复数基带下的 AM：包络就是模长

CentSDR 的正交检波器输出的不是实数信号，而是 I/Q 两路（一个复数序列）。复基带下的 AM 信号是

\[ z(n) = A_c\bigl(1 + m\cos(\omega_m n)\bigr)\,e^{j\omega_c n} \]

它在复平面上是一个绕原点旋转的向量，向量长度随调制起伏。于是解调变得极其简单：

\[ |z(n)| = A_c\bigl(1 + m\cos(\omega_m n)\bigr)\quad (m \le 1 \text{ 时绝对值可省}) \]

**取模就是包络检波**。本讲的核心代码 `z = sqrt(x*x + y*y)` 就是干这件事。

还有一个关键性质贯穿全讲：

\[ \bigl|z\,e^{j\theta}\bigr| = |z| \quad \text{（对任意相位旋转 }\theta\text{）} \]

模值对相位旋转免疫。这解释了后面很多「看似不精确却没事」的设计。

### 2.3 为什么不能让载波落在 0Hz

既然取模就能解调，为什么不把本振直接对准载波（载波落在 0Hz）？因为在**模拟/ADC 域**，0Hz 附近是个黑洞：

- u2-l2 讲过，TLV320AIC3204 的 mini-DSP 里有一条承担 DC 抑制的一阶高通，它本来就负责清除模拟通路的直流；
- ADC 的直流失调也正好落在 0Hz，与载波混在一起无法区分。

载波若恰好坐在 0Hz 上，会被高通衰减、被失调污染，AM 解调会严重失真。所以 CentSDR 采用**低中频**方案：本振故意偏离 10kHz，让载波安全地待在 +10kHz；等信号数字化进入 STM32 之后，再由 NCO 搬回 0Hz——数字域里没有会杀直流的环节，而且围绕 0Hz 的对称低通滤波器最便宜。

### 2.4 与上一讲的衔接

u3-l2 的 `demod_weaver`（SSB/CW）是三级流水：**混频 → 低通 → 反向混频**。本讲的 `am_demod` 就是它的**前两级 + 取模**替代第三级：

| 步骤 | demod_weaver（SSB/CW） | am_demod（AM） |
|---|---|---|
| 1. NCO 搬移 | 把边带中心搬到 0Hz | 把 +10kHz 载波搬到 0Hz |
| 2. 低通 | 1300Hz / 150Hz 椭圆 | **7800Hz** 椭圆 |
| 3. 恢复 | 反向 NCO 搬回音频 | **取模**直接得到包络 |

AM 不需要第三级混频，因为音频信息就在包络里，取模之后已经是要听的信号了。

## 3. 本讲源码地图

| 文件 | 在本讲中的角色 | 关键位置 |
|---|---|---|
| [nanosdr.h](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h) | 全局共享头：`FS`/`AM_FREQ_OFFSET`/`PHASESTEP` 宏、解调函数声明、中间缓冲声明 | L93、L112-L116、L125-L128 |
| [dsp.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c) | 本讲主角：`am_demod()` 两个编译分支、`_VSQRTF`、AM 滤波器系数与实例 | L284-L285、L290-L291、L312-L320、L411-L416、L420-L483 |
| [main.c](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c) | 系统级接线：`mod_table`、`set_modulation`、`set_tune`、I2S 回调入口、shell 命令 | L113-L115、L165-L201、L258-L267、L657-L679 |
| [python/centsdr.py](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/centsdr.py) | 可选硬件实践：`-p N` 抓取四个内部缓冲区之一并绘图 | L77-L86、L135-L136 |

## 4. 核心概念与源码讲解

### 4.1 系统级视角：AM_FREQ_OFFSET 把载波放到 +10kHz

#### 4.1.1 概念说明

`AM_FREQ_OFFSET` 这个 10000 在整机里出现两次，方向相反：

1. **调谐侧（模拟域）**：`set_tune()` 把本振频率设为「显示频率 − 10000」。载波因此落在复基带的 **+10kHz** 上——这就是 2.3 节说的低中频。u4-l1 会讲到，频谱显示的 CAP 模式抓的就是这个缓冲区，你能直接在屏幕上看到 +10kHz 处的载波谱线。
2. **解调侧（数字域）**：`am_demod()` 用一个步进为 `PHASESTEP(10000)` 的 NCO 与信号相乘，把 +10kHz 的信号**搬回 0Hz**，供围绕直流对称的椭圆低通滤波。

两个 10kHz 恰好抵消——但一个发生在 ADC 之前（避开直流黑洞），一个发生在 ADC 之后（数字域安全）。另外，CW 模式与 AM 共用同一个 `AM_FREQ_OFFSET`（见 `mod_table`），因为 CW 必须有非零差拍音，天然依赖「本振偏低 + NCO 搬移」的同一套架构。

#### 4.1.2 核心流程

从用户敲下 `tune 567000` 到 AM 音频出现：

```text
tune 567000 (shell, main.c cmd_tune)
  └─ set_tune(567000)
       ├─ center_frequency = 567000 − mode_freq_offset(=10000) = 557000
       └─ si5351_set_frequency(557000 × 4)        # 本振偏低 10kHz（×4 供正交检波，见 u2-l1）
            ↓
天线信号 567kHz × 本振 → 复基带：载波出现在 +10kHz
            ↓
每 5ms（48kHz 档）I2S 半满中断 → i2s_end_callback
  └─ (*signal_process)(p, q, n)                   # signal_process == am_demod
       ├─ 第 1 级：NCO 步进 PHASESTEP(10000) 反向混频 → 信号中心回到 0Hz
       ├─ 第 2 级：7800Hz 椭圆低通（信道选择）
       └─ 第 3 级：sqrt(I²+Q²) → 包络 → 左右声道
```

#### 4.1.3 源码精读

- [nanosdr.h:L125-L128](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L125-L128) —— 三个关键宏一字排开：`FS 48000`（写死，见 u2-l3 的提醒）、`AM_FREQ_OFFSET 10000`、`PHASESTEP(freq) (65536L * freq / FS)`。注意 `PHASESTEP` 是**整数运算**：\(65536 \times 10000 / 48000 = 13653.33\)，C 整数除法截断为 13653。
- [main.c:L165-L177](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L165-L177) —— `mod_table`：每种模式的四元组 `(demod_func, freq_offset, fs, name)`。AM 行（L174）是 `{ am_demod, AM_FREQ_OFFSET, 48, "am" }`——注意表里填的是**宏的值**，之后进入运行期变量。
- [main.c:L179-L194](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L179-L194) —— `set_modulation()` 在模式切换时做三件事：换采样率（AM 是 48kHz）、把 `signal_process` 函数指针换成对应解调函数、把 `mode_freq_offset` 从表里取出并立刻换算 `mode_freqoffset_phasestep = PHASESTEP(mode_freq_offset)`。这个步进就是 am_demod 里 NCO 的转速。
- [main.c:L196-L201](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L196-L201) —— `set_tune()`：`center_frequency = hz - mode_freq_offset` 然后 `si5351_set_frequency(center_frequency * 4)`。AM 模式下本振永远比显示频率低 10kHz。
- [main.c:L113-L115](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L113-L115) —— 一个容易被忽略的细节：`signal_process` 的**初值就是 `am_demod`**，`mode_freq_offset` 初值就是 `AM_FREQ_OFFSET`。开机默认处于 AM 状态（随后 `config_recall` 恢复用户设置，见 u1-l3）。
- [main.c:L258-L267](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L258-L267) —— `i2s_end_callback`：I2S 半满/全满中断里 `(*signal_process)(p, q, n)`，每回调处理 240 帧（480 个 int16）。这是 am_demod 唯一的调用点。

#### 4.1.4 代码实践

**实践目标**：亲手算一遍这台机器的「两个 10kHz」，并用 Python 核对。

1. 手算：AM 模式下 `tune 567000`，`center_frequency` 是多少？送给 SI5351 的数值是多少？`PHASESTEP(10000)` 等于多少？
2. 用 Python 核对（含 NCO 的实际频率与残差）：

```bash
python3 -c "print(65536*10000//48000, 13653*48000/65536)"
```

3. 需要观察的现象：步进是 13653 而不是 13653.33，NCO 实际频率 9999.756Hz，比 10000Hz **低约 0.24Hz**——混频后载波并不精确落在 0Hz，而是带着 0.24Hz 的缓慢旋转。
4. 预期结果：`13653 9999.755859375`。思考这 0.24Hz 会不会造成失真（答案见 4.4 节与练习 3）。待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：如果固件把 `AM_FREQ_OFFSET` 改成 20000，只改宏、其他不动，收听会发生什么？

**答案**：本振会偏低 20kHz（`set_tune` 与 `mod_table` 都引用同一个宏，自动一致），载波落在 +20kHz；`am_demod` 的 NCO 步进也变成 `PHASESTEP(20000)`，仍能把信号搬回 0Hz——链路依然自洽，仍可收听。但 48kHz 采样率下 Nyquist 只有 24kHz，+20kHz 载波只剩 ±4kHz 就是折叠边，且 7800Hz 低通带宽没有跟着改，通带不对称地切掉信号；同时越靠近 Nyquist，前端抗混叠余量越小。所以这个偏移量不是随便选的：10kHz 让 15.6kHz 宽的 AM 信道在 24kHz Nyquist 内居中且有余量。

**练习 2**：`mode_freq_offset` 为什么做成运行期变量，而 `AM_FREQ_OFFSET` 是编译期宏？

**答案**：`mod_table` 六种模式各有自己的 `freq_offset`（CW/AM 为 10000，其余为 0），`set_modulation` 在切换时把表里的值写进 `mode_freq_offset`，供 `set_tune` 与解调器共用。宏只是这张表的一个初始化常量。运行期变量让「频率偏移」成为每个模式自己的属性，而不是全局写死。

### 4.2 第一级：NCO 频移，把信号搬回 0Hz

#### 4.2.1 概念说明

第一级与 `demod_weaver` 的第一级**逐字相同**（copy-paste 级别的复用）：对每个交织 IQ 样本做一次复数乘法，乘上 NCO 产生的旋转复指数。u3-l1 详细讲过 `cos_sin()` 查表内插与 `__SMLAD/__SMLSDX` 的数据通路，u3-l2 推导过这里只换记号复述结论。

#### 4.2.2 核心流程

复数乘法（u3-l2 已推导 `__SMLSDX/__SMLAD` 的展开）：

\[ (I' + jQ') = (I + jQ)\cdot(\cos\phi_n + j\sin\phi_n),\qquad \phi_n = \phi_0 - n\cdot\Delta\phi \]

相位每样本递减 \(\Delta\phi\)（正步进），等效于乘以 \(e^{-j\omega n}\)，谱线**向下**搬移：+10kHz 的载波被搬到 0Hz 附近（带着 4.1.4 算出的 0.24Hz 残差）。用频率表示：

\[ f_{\text{out}} = f_{\text{in}} - \frac{\Delta\phi \cdot f_s}{65536} = 10000 - 9999.756 \approx 0.24\,\text{Hz} \]

注意符号约定的来历：`nco1_phase -= mode_freqoffset_phasestep` 与 USB 配置同向（+1300Hz 边带中心 → 0Hz）。正负号最终由前端 IQ 极性与 codec 频谱倒置共同决定（u3-l2 的结论）；若方向弄反，信号会被搬到 ±20kHz 然后**被低通滤掉**，整机只剩噪声——硬件上靠 `freq_inverse` 等配置把净频谱方向调对。

#### 4.2.3 源码精读

- [dsp.c:L420-L438](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L420-L438) —— `am_demod` 函数头与第一级混频循环：

```c
void
am_demod(int16_t *src, int16_t *dst, size_t len)
#if defined(AM_FREQ_OFFSET) && AM_FREQ_OFFSET
{
    ...
    for (i = 0; i < len/2; i++) {
        uint32_t cossin = cos_sin(nco1_phase);
        nco1_phase -= mode_freqoffset_phasestep;
        uint32_t iq = *s++;
        *bufi++ = __SMLSDX(iq, cossin, 0) >> (15-0);
        *bufq++ = __SMLAD(iq, cossin, 0) >> (15-0);
    }
```

  这段与 [dsp.c:L358-L364](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L358-L364)（`demod_weaver` 的第一级）只有一处实质差异：步进从 `dc->phasestep1` 换成了全局变量 `mode_freqoffset_phasestep`。交织 IQ 从 `src` 拆到平面格式的 `buffer[0]/buffer[1]`（「拆交织」，u2-l3 讲过的固定流程），`>>15` 把 q15 乘积重新归一化。

- [dsp.c:L284-L285](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L284-L285) —— `nco1_phase` 是**全局变量**：相位跨回调块保持连续（u3-l2 的关键结论），AM 与 SSB/CW 模式之间甚至共用它——切换模式的那一瞬间相位从上次停的地方继续，只影响瞬态，不影响稳态频移。
- [dsp.c:L430](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L430) 与 [dsp.c:L439](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L439) —— 混频前后的 `disp_fetch_samples(B_CAPTURE/B_IF1, ...)`：显示线程「搭便车」抓取样本（B_CAPTURE 是搬移前的 +10kHz 信号，B_IF1 是搬移后的 0Hz 信号），详见 u4-l1。

#### 4.2.4 代码实践

**实践目标**：在 PC 上用 numpy 直观验证「乘 e^{−jωn} 把 +10kHz 谱线搬到 0Hz、乘共轭则搬到 20kHz」。

```python
# 示例代码（非项目源码）
import numpy as np
fs, N = 48000, 2048
n = np.arange(N)
x = np.exp(2j*np.pi*10000*n/fs)            # 模拟 CAP 缓冲区里的 +10kHz 载波
f = np.fft.fftshift(np.fft.fftfreq(N, 1/fs))
for sign, tag in ((-1, 'phase -= step (固件写法)'), (1, 'phase += step')):
    y = x * np.exp(sign*2j*np.pi*10000*n/fs)
    mag = np.abs(np.fft.fftshift(np.fft.fft(y)))
    print('%-24s 谱峰位于 %8.1f Hz' % (tag, f[np.argmax(mag)]))
```

1. 操作步骤：保存为 `shift.py`，`python3 shift.py`。
2. 需要观察的现象：两种符号下谱峰的位置。
3. 预期结果：固件写法（−）谱峰在 0Hz 附近；反向（+）谱峰在 20000Hz（48kHz 采样下贴近 Nyquist）。待本地验证。
4. 顺手把 10000 换成 13653×48000/65536（NCO 的真实转速），观察谱峰落在 0Hz 附近但有一点点弥散——那就是 0.24Hz 残差在 2048 点 FFT 里的表现。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `am_demod` 不像 `demod_weaver` 那样通过配置结构体传入步进，而是读全局变量 `mode_freqoffset_phasestep`？

**答案**：SSB 家族的步进是**每个模式固定**的（±1300Hz），适合做成 `usb_demod_conf/lsb_demod_conf` 常量结构体；而 AM/CW 的步进来自 `mod_table` 的运行期 `freq_offset`，且 CW 的第二级步进（侧音）还要在运行期由 `cwtone` 命令热更新。用全局变量让 `set_modulation` 一处置值、`set_tune` 与解调器多处共用同一数值，保证「本振偏多少、NCO 就搬多少」永远一致。

**练习 2**：`len/2` 里的 `len` 是什么单位？为什么除以 2？

**答案**：`len` 是 **int16 样本个数**（不是帧数）。I2S 交织格式每个复数帧占 2 个 int16（I 和 Q），所以循环次数是 `len/2` 个复数样本。480 个 int16（240 帧）触发一次回调（u2-l3），对应 `len=480`、循环 240 次。

### 4.3 AM 信道滤波器：6 阶椭圆低通与共享的滤波器状态

#### 4.3.1 概念说明

取模检波有个隐藏前提：**输入里只能有一个想要的信号**。对多个信号的和取模会产生交叉调制（强台的包络会「压」到弱台上，这是包络检波的经典缺陷）。所以在检波之前必须用滤波器圈出信道。这就是第二级：一个 6 阶椭圆低通，截止 7800Hz——注释里写明 `fc = 6*1300`，即 SSB 1300Hz 滤波器带宽的 6 倍，对应约 ±7.8kHz、总共 15.6kHz 的 AM 信道带宽（中波广播信道间隔 9/10kHz，这个宽度是「宽收」的折中）。

滤波在 0Hz 附近对称进行，所以第一级必须先把信号搬回直流——两级的先后次序不可交换。

#### 4.3.2 核心流程

```text
buffer[0]/buffer[1]（平面 IQ，0Hz 中心）
   ├─ arm_biquad_cascade_df1_q15(&bq_am_i, buffer[0], buffer2[0], 240)   # I 路
   └─ arm_biquad_cascade_df1_q15(&bq_am_q, buffer[1], buffer2[1], 240)   # Q 路
        ↓
buffer2[0]/buffer2[1]（滤波后平面 IQ）
```

三个 biquad（二阶节）级联实现 6 阶；I/Q 两路各自独立一套实例（u3-l2 强调过：两路必须独立状态，否则 I 的历史会污染 Q）。CMSIS 的系数排列约定 `{b0, 0, b1, b2, a1, a2}`、a 已取反、系数统一乘 16384 并配 `postShift=1`、64 位累加防溢出——这些 u3-l2 已详细推导，本讲不重复。

#### 4.3.3 源码精读

- [dsp.c:L312-L317](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L312-L317) —— `bq_coeffs_am[]`：18 个 q15 系数 = 3 个 biquad × 6。注释标明 `6th order elliptic lowpass filter fc=6*1300=7800Hz`。
- [dsp.c:L319-L320](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L319-L320) —— 实例 `bq_am_i/bq_am_q`，postShift=1。**注意第三、四个字段**：它们的状态数组是 `bq_i_state/bq_q_state`——与 SSB 的 `bq_i/bq_q`（L308-L309）和 CW 的 `bq_cw_i/bq_cw_q`（L329-L330）**共用同一块内存**（[dsp.c:L290-L291](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L290-L291)）。因为同一时刻只有一个 `signal_process` 在跑，共享是安全的；代价是**切换模式的那一下，滤波器从上一模式残留的状态开始收敛**，听感上是一个几十毫秒的暂态。这是一处典型的嵌入式内存取巧。
- [dsp.c:L441-L445](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L441-L445) —— 调用点：I/Q 两路分别从 `buffer` 滤到 `buffer2`，之后 `disp_fetch_samples(B_IF2, ...)` 让显示线程还能抓到滤波后的版本（u4-l1 的 IF2 显示模式）。

#### 4.3.4 代码实践

**实践目标**：不运行任何东西，纯读源码回答三个问题（源码阅读型实践）。

1. 数一数 `bq_coeffs_am[]` 的元素个数，验证「3 个 biquad = 6 阶」。
2. AM 信道总带宽是多少？48kHz 采样率的 Nyquist 带宽是多少？余量还有多少？
3. 对比 [dsp.c:L301-L305](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L301-L305) 的 SSB 系数（fc=1300Hz）与 AM 系数（fc=7800Hz）：前两行都是 `xxx, 0, ...`，中间那个 0 是什么？

**预期结果**：18 个元素 ✓；带宽 \(2 \times 7800 = 15600\)Hz，Nyquist 带宽 24000Hz，上端余量 4200Hz——这正是 4.1 练习 1 里「10kHz 偏移不能随意加大」的定量依据；中间的 0 是 u3-l2 讲过的系数布局约定 `{b0, 0, b1, b2, a1, a2}` 的第二个槽位——CMSIS 的 q15 DF1 实现每级读 6 个系数槽、跳过第 2 槽，固件以 0 填充，三组系数结构完全同形，只是数值不同。

#### 4.3.5 小练习与答案

**练习 1**：中波信道间隔 9kHz，如果把 `bq_coeffs_am` 换成截止 4500Hz 的 6 阶椭圆（更贴合单信道），需要动本讲之外 的哪些代码？

**答案**：只需替换 `bq_coeffs_am[]` 的 18 个系数——用 python 目录下同类 notebook 的流程（ellip 设计 → 拆二阶节 → ×16384 取整，参考 u3-l2 与 u5-l5）生成新系数即可；`bq_am_i/bq_am_q` 实例、调用点都不用动，因为滤波器阶数结构（3 个 biquad、postShift=1）没变。这也说明了系数表驱动的好处。

**练习 2**：为什么 I/Q 两路不能共用一个滤波器实例轮流处理？

**答案**：biquad 是有记忆的（y[n−1]、y[n−2]、x[n−1]、x[n−2]）。若共用实例先滤 I 再滤 Q，Q 会从 I 的末状态开始，等于把 I 的历史卷进 Q 的开头，两路的相位关系被破坏，取模结果错误。所以必须两套实例、两块状态（哪怕状态内存被三种模式共享，I 与 Q 之间从不共享）。

### 4.4 幅度检波：vsqrt.f32、饱和钳位与双声道复制

#### 4.4.1 概念说明

第三级是全讲的高潮，也是最短的一段：对滤波后的每对 (I, Q) 计算 \(\sqrt{I^2+Q^2}\)，得到包络，再复制到左右两个声道。这里有两个工程看点：

1. **开方用硬件指令**：Cortex-M4F 的 FPU 有单条 `vsqrt.f32`，作者用内联汇编直接发射它，绕开库函数 `sqrtf` 的调用开销与 errno 语义检查。源码里保留着注释掉的 `sqrtf` 版本，是作者两种写法都试过的痕迹。
2. **定点溢出的边界算术**：\(I^2+Q^2\) 在 int32 里算，其上限恰好贴着 int32 的天花板——这是 q15 表示法下必须手工保证的不变式（u3-l1 讲过「求模会越界」）。

#### 4.4.2 核心流程

对每个复数样本：

\[ z = \sqrt{x^2 + y^2} \]

数值边界（q15 的范围是 \([-32768, 32767]\)）：

- \(32767^2 + 32767^2 = 2147352578 < 2^{31}-1 = 2147483647\)：**勉强放得下**，安全边际只有 131069；
- 但 \(32768^2 \times 2 = 2^{31}\)：若 x = y = −32768，则恰好溢出 int32（未定义行为）。实际很难到达这个角点：I/Q 各自先被 ADC 限幅在 int16 范围内，复包络被压在满量程附近，AGC 又会把电平往回收；
- 开方结果上限 \(\lfloor\sqrt{2147352578}\rfloor = 46340 > 32767\)：\(\sqrt{2}\times 32767\) 的几何直觉——若 I、Q 同时接近满幅，包络可以超出 int16，需要某种饱和处理（固件的实际处理方式见下段）；
- 开方结果下限是 0：负方向本不会有问题。

u3-l1 曾把这段循环末尾的两行 `if` 总结为「教科书式的手动饱和」。但行号级细读会发现一个顺序陷阱——钳位写在 `(int16_t)` 强转**之后**，而强转会先执行：32768～46340 之间的值先回绕成 −32768～−19196 的负数，再经过两句钳位时已经都在 int16 范围内，**两句钳位实际上都不会触发**。详见 4.4.3 与练习 4.4-1。

#### 4.4.3 源码精读

- [dsp.c:L411-L416](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L411-L416) —— `_VSQRTF()`：`always_inline` 的静态内联函数，一条 `vsqrt.f32 %0,%1` 指令，输入输出走 FPU 寄存器（`"=w"`/`"w"` 约束）。Cortex-M4 TRM 标注 VSQRT.F32 为 14 周期；而直接调用 `sqrtf` 默认要遵守 C 语言的 errno 语义（负输入需置 `errno` 并返回 NaN），编译器因此生成一次真正的库函数调用——多出参数传递、调用/返回、域检查的开销。对每秒执行 48000 次的热路径来说值得手写。（GCC 在 `-ffast-math` 下也可能把 `sqrtf` 内联成 VSQRT，但固件不能随便放开数学语义。）
- [dsp.c:L448-L463](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L448-L463) —— 检波循环本体：

```c
for (i = 0; i < len/2; i++) {
  int32_t x = *bufi++;
  int32_t y = *bufq++;
  int32_t z;
  z = (int16_t)_VSQRTF((float)(x*x+y*y));      /* 硬件开方取模（注释为本讲所加） */
  if (z > 32767) z = 32767;                     /* 两句钳位都排在强转之后 */
  if (z < -32768) z = -32768;                   /* 实际不会触发，见下文分析 */
  *d++ = __PKHBT(z, z, 16);                     /* 同一包络复制到 L/R */
}
```

  `__PKHBT(z, z, 16)` 把同一个 16 位值装进高低两个半字——单声道包络变成交织立体声输出，正好满足 tx_buffer 的格式（u2-l3）。**顺序陷阱**：`(int16_t)` 强转的优先级高于赋值，`z` 拿到的是已经回绕后的 16 位值——若开方结果落在 32768～46340 之间，强转先把它回绕成 −32768～−19196 的负数，随后两句 `if` 看到的已经是一个「合法」的 int16，谁也不触发。也就是说：这两行钳位按现在的写法是**空转的**，真正防止越界的其实是 ADC 限幅 + AGC 电平控制把包络压在满量程之内。干净的写法应当先钳位后强转（`int32_t t = _VSQRTF(...); if (t > 32767) t = 32767; z = (int16_t)t;`）。这不妨碍固件正常工作，却是「读代码要读出执行顺序」的最佳教例。
- [dsp.c:L418](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L418) 与 [dsp.c:L447-L464](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L447-L464) —— **代码考古**：全局 `ave_z` 与注释掉的 `acc_z`/`z -= ave_z`/`ave_z = ave_z*0.98 + ...` 是一个被放弃的「自适应包络直流扣除」实验——作者显然想让输出减去包络的滑动平均以消掉载波直流分量，最终没有启用。现在的 `#if` 分支输出里保留了包络直流（对扬声器不可闻），`#else` 分支则用一个写死的常数硬扣（见 4.5）。读开源固件时，注释掉的代码往往比注释更能讲历史。

#### 4.4.4 代码实践

**实践目标**：在 PC 上量化「库调用 vs 单指令」的差异，并亲手验证 4.4.2 的溢出边界。

1. 基准测试（示例代码，非项目源码）：

```c
#include <stdio.h>
#include <time.h>
#include <math.h>
int main(void) {
    volatile float sink = 0; volatile int z; double t0;
    float vals[4] = {1073741824.f, 536870912.f, 268435456.f, 100.f};
    t0 = (double)clock();
    for (long i = 0; i < 4000000; i++) sink = sqrtf(vals[i & 3]);
    t0 = ((double)clock() - t0) / CLOCKS_PER_SEC;
    z = 0;
    double t1 = (double)clock();
    for (long i = 0; i < 4000000; i++)
        __asm__ volatile ("fsqrt %0, %1" : "=w"(sink) : "w"(vals[i & 3]));  /* x86 对应指令 */
    t1 = ((double)clock() - t1) / CLOCKS_PER_SEC;
    printf("sqrtf: %.3fs  指令内联: %.3fs  (sink=%f %d)\n", t0, t1, sink, z);
    return 0;
}
```

2. 边界验证：`python3 -c "print(32767**2*2, 2**31-1, (-32768)**2*2, 2**31)"`。
3. 需要观察的现象：x86 上 `fsqrt` 与 `sqrtf` 的耗时差（PC 的编译器比 MCU 聪明，差距会比 Cortex-M4 上小）；边界打印中两个「贴脸」的数字。
4. 预期结果：`2147352578 2147483647 2147483648 2147483648`——正向差 131069（安全），负向恰好相等（溢出）。基准数字待本地验证（不同机器/编译器结果不同，重点是比较而非绝对值）。

#### 4.4.5 小练习与答案

**练习 1**：`z = (int16_t)_VSQRTF(...)` 之后的两句钳位（`z > 32767` / `z < -32768`）按现在的写法能否拦住开方结果 46340？为什么？

**答案**：拦不住。C 的强制转换先于赋值执行：46340 超出 int16 范围，按 ARM GCC 的回绕语义变成 46340 − 65536 = −19196，落在 int16 区间内，随后两句 `if` 的条件都不成立，−19196 被原样写进 DAC——波形上表现为包络打满时突然深陷一个大负脉冲，而不是干净的削顶。要让钳位真正生效，必须把它挪到强转之前。实践中 ADC 限幅与 AGC 使包络很难越过满量程，这个缺陷基本不会被触发，但「先截断后钳位」的顺序读代码时必须看穿。

**练习 2**：`_VSQRTF` 的参数是 `(float)(x*x+y*y)`——先在 int32 里算平方和再转 float。为什么不直接 `(float)x*x + (float)y*y`？

**答案**：int32 平方和在 2¹⁵×2¹⁵×2 的范围内是**精确**的（每个乘积和求和都没有舍入），float（24 位尾数）反而对 2×10⁹ 附近的整数只能近似表示。先整数后转浮点，把唯一的舍入留给开方一步，精度最优；代价就是 4.4.2 分析的 int32 溢出边界必须人工保证。这是定点/浮点混合代码的典型权衡。

**练习 3**：4.1.4 算出 NCO 比本振偏移慢 0.244Hz，混频后载波带着 0.24Hz 的缓慢旋转。为什么听不出任何失真？

**答案**：包络检波取的是模值，而 \( |z\,e^{j\theta}| = |z| \)——0.24Hz 的旋转只是复平面上向量缓慢地转圈，模值（要提取的包络）完全不受影响。对比下一讲的 FM 鉴频：那里相位差分就是解调对象，同样的旋转会直接变成 0.24Hz 的直流偏移。同一现象在不同解调下的命运不同，根源在于检波量不同。

### 4.5 编译期 #if：两种实现的取舍

#### 4.5.1 概念说明

`am_demod` 的函数体被 `#if defined(AM_FREQ_OFFSET) && AM_FREQ_OFFSET` 劈成两个完整版本——这不是运行时开关，而是**编译期二选一**。作者把「教科书式完整链路」和「最小可行检波」同时留在源码里，是很好的教学对照：

- **`#if` 分支（当前生效）**：频移 + 7800Hz 滤波 + 取模。有信道选择性，输出幅度与信号电平无关（包络本身），对邻台干扰稳健。
- **`#else` 分支**：对原始交织 IQ 直接取模，减一个写死的 `DCOFFSET`。没有任何滤波，也不调用 `disp_fetch_samples`（显示线程无数据可抓）。数学上它仍是「完美」的包络检波（复信号取模与载波位置无关），但工程上它假设载波电平恰好打满量程（见下），且多信号交叉调制无从抑制。

#### 4.5.2 核心流程（两分支对比）

| 维度 | `#if` 分支（dsp.c:L422-L467） | `#else` 分支（dsp.c:L469-L482） |
|---|---|---|
| NCO 频移 | 有（搬回 0Hz） | 无 |
| 信道滤波 | 6 阶椭圆 7800Hz | 无 |
| 取模输入 | 滤波后 q15（可达 ±32767） | 原始 q15 **先除以 2** |
| 平方和上限 | \(2\times32767^2\)，贴 int32 顶 | \(2\times16384^2 = 2^{30}\)，宽裕 |
| 开方后钳位 | 写了两句，但顺序在强转之后、实际空转（见 4.4.3） | 不需要（上限 23170，天然在 int16 内） |
| 直流处理 | 不扣（包络直流留在输出里） | 硬扣 `DCOFFSET = 16383` |
| disp_fetch_samples | 4 个抓取点 | 0 个 |
| 适用条件 | 正式接收，需要选择性与电平无关性 | 强信号、无邻道、（配合 AGC 时）电平可预估的演示场景 |

#### 4.5.3 源码精读

- [dsp.c:L422](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L422) —— 分支条件 `#if defined(AM_FREQ_OFFSET) && AM_FREQ_OFFSET`：宏未定义**或**为 0 都会落入 `#else`。注意连锁反应——`mod_table` 的 AM 行（[main.c:L174](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L174)）也引用同一个宏，把宏改成 0 会同时让本振不再偏低（`set_tune` 不减偏移）并切换检波实现，两个版本各自与调谐侧自洽。
- [dsp.c:L469-L482](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L469-L482) —— `#else` 分支全文：

```c
{
  uint32_t i;
  for (i = 0; i < len; i += 2) {
    int32_t x = src[i];
    int32_t y = src[i+1];
    int32_t z;
#define DCOFFSET 16383
    x = x/2;
    y = y/2;
    z = (int16_t)_VSQRTF((float)(x*x+y*y)) - DCOFFSET;
    dst[i] = dst[i+1] = z;
  }
}
```

  三个细节：`x/2` 先砍半，让平方和放宽到 \(2^{30}\)（这就是它敢不钳位的原因）；`DCOFFSET = 16383` 恰好等于「满量程载波（32767）砍半后的包络直流」——也就是说它假设信号把 ADC 打满；`dst[i] = dst[i+1] = z` 用普通下标而非 SIMD 打包，整个分支没有任何一条 DSP 指令，是最朴素的写法。若配合 AGC 把电平稳定在目标值，这个固定偏移才勉强可用——但任何偏离都直接变成输出直流偏置或削顶。

#### 4.5.4 代码实践

**实践目标**：在 4.6 综合实践的模拟器里并排编译两个分支，观察 `DCOFFSET` 的电平依赖。

1. 操作步骤：在综合实践的程序里再加一个 4.5.3 逐字提取的 `am_demod_else_pc()`，把主循环分别指向两个实现，各输出一列 CSV。
2. 需要观察的现象：载波幅度取 16000 时（未打满），`#else` 输出的波形整体沉在负半区；`#if` 输出围绕正的包络直流摆动。
3. 预期结果：`#else` 输出 ≈ \(8000(1+0.5\cos) - 16383 \in [-12383, -4383]\)（400Hz 周期骑在大负直流上）；把载波幅度改成 32767 再看，输出才大致居中。待本地验证。
4. 思考：哪个版本的输出更适合直接进 DAC？如果 DAC 通路有耦合电容，直流偏置是否还重要？

#### 4.5.5 小练习与答案

**练习 1**：`#else` 分支里 `#define DCOFFSET 16383` 写在函数体内部。这样定义的宏作用域是什么？有什么隐患？

**答案**：`#define` 不受花括号作用域限制，从定义处直到文件末尾（或 `#undef`）都有效——写在函数体内只是视觉上的「就近声明」。隐患是它此后会污染同文件后续代码：若后面有人再定义名为 `DCOFFSET` 的变量或常量会被宏替换破坏。固件里它因为处在文件末尾的 `#else` 分支而没有实际危害，但更稳妥的写法是 `static const int32_t`。

**练习 2**：假设把 `AM_FREQ_OFFSET` 宏删掉（走 `#else` 分支），频谱显示还能显示 AM 信号吗？

**答案**：不能像现在这样工作了。`#else` 分支一次 `disp_fetch_samples` 都不调用，显示线程抓不到任何缓冲区，频谱/瀑布会停在旧数据（u4-l1 会讲 `disp_fetch_samples` 是显示数据的唯一来源）。这从侧面印证 `#else` 是「最小可行」的演示版本，而非完整产品路径。

## 5. 综合实践：在 PC 上复活 am_demod

把 `am_demod` 的 `#if` 分支提取到 PC 上，喂一段自己生成的 AM 信号，验证包络被完整恢复。这是本讲指定的实践任务。

**实践目标**：生成 50% 调幅度、载波 +10kHz 的交织 IQ 信号 → 跑提取出的解调逻辑（滤波器按要求用一阶低通近似 7800Hz 椭圆）→ 绘制输出 → 验证包络频率等于调制频率。

**操作步骤**：

1. 保存以下程序为 `am_sim.c`（示例代码，非项目源码；SIMD 与 CMSIS 依任务要求替换为等价 C 实现，结构、变量名与顺序尽量保留固件原貌，关键行标注了对应的 dsp.c 行号）：

```c
/* am_sim.c —— dsp.c am_demod() (#if 分支) 的 PC 提取版
 * 编译：gcc -O2 am_sim.c -lm -o am_sim && ./am_sim > out.csv */
#include <stdio.h>
#include <stdint.h>
#include <math.h>

#define FS        48000
#define FC        10000                        /* = AM_FREQ_OFFSET */
#define FM        400                          /* 调制单音 400Hz */
#define DEPTH     0.5                          /* 调幅度 50% */
#define FRAMES    240                          /* 每回调 240 帧（u2-l3 的半缓冲） */
#define NBLOCKS   40                           /* 40 块 = 9600 帧 = 200ms */
#define AMPL      16000.0f                     /* 载波幅度（故意不打满） */

/* ---- 固件全局量的 PC 替身 ---- */
static uint16_t nco1_phase;                     /* dsp.c:284 */
#define PHASESTEP(freq) (65536L * (freq) / FS)  /* nanosdr.h:128，整数截断与固件一致 */
static const int16_t mode_freqoffset_phasestep = PHASESTEP(FC);   /* = 13653 */

static float lp_i = 0.0f, lp_q = 0.0f;          /* 近似 bq_am_i/bq_am_q 的状态（跨块保持！） */
#define LP_ALPHA  0.64f                         /* 1 - exp(-2*pi*7800/48000) ≈ 0.64 */

/* dsp.c:420-467 的提取版：len 是 int16 个数（= 2 * 帧数） */
static void am_demod_pc(const int16_t *src, int16_t *dst, size_t len)
{
    static float bufi[FRAMES], bufq[FRAMES];    /* buffer[0]/buffer[1] */
    size_t i;

    /* 第 1 级（dsp.c:432-438）：NCO 混频，+10kHz -> 0Hz */
    for (i = 0; i < len/2; i++) {
        float rad = (float)nco1_phase / 65536.0f * 6.2831853f;
        float c = 32767.0f * cosf(rad), s = 32767.0f * sinf(rad);
        nco1_phase -= mode_freqoffset_phasestep;
        float ii = src[2*i], qq = src[2*i+1];
        bufi[i] = (ii * c - qq * s) / 32768.0f; /* __SMLSDX >> 15 */
        bufq[i] = (ii * s + qq * c) / 32768.0f; /* __SMLAD  >> 15 */
    }

    /* 第 2 级（dsp.c:442-443）：一阶低通近似 6 阶椭圆 7800Hz */
    for (i = 0; i < len/2; i++) {
        lp_i += LP_ALPHA * (bufi[i] - lp_i);
        lp_q += LP_ALPHA * (bufq[i] - lp_q);
        bufi[i] = lp_i; bufq[i] = lp_q;
    }

    /* 第 3 级（dsp.c:450-463）：取模 + 钳位 + 双声道 */
    for (i = 0; i < len/2; i++) {
        int32_t x = (int32_t)(bufi[i] * 32768.0f);
        int32_t y = (int32_t)(bufq[i] * 32768.0f);
        int32_t z = (int16_t)sqrtf((float)(x*x + y*y));   /* vsqrt.f32 -> sqrtf */
        if (z > 32767) z = 32767;
        if (z < -32768) z = -32768;
        dst[2*i] = dst[2*i+1] = (int16_t)z;              /* __PKHBT(z,z,16) */
    }
}

int main(void)
{
    int16_t rx[FRAMES*2], tx[FRAMES*2];
    long n = 0;
    for (int b = 0; b < NBLOCKS; b++) {
        for (int i = 0; i < FRAMES; i++, n++) {          /* 模拟 rx_buffer 的交织 IQ */
            float env = AMPL * (1.0f + DEPTH * cosf(2*M_PI*FM*n/FS));
            float ph  = 2.0f*M_PI*FC*n/FS;
            rx[2*i]   = (int16_t)(env * cosf(ph));       /* I */
            rx[2*i+1] = (int16_t)(env * sinf(ph));       /* Q */
        }
        am_demod_pc(rx, tx, FRAMES*2);                   /* 每 240 帧一次"回调" */
        for (int i = 0; i < FRAMES; i++)
            printf("%ld,%d\n", b*(long)FRAMES + i, tx[2*i]);
    }
    return 0;
}
```

2. 编译运行并绘图：

```bash
gcc -O2 am_sim.c -lm -o am_sim && ./am_sim > out.csv
python3 -c "
import csv
xs, ys = zip(*((int(a), int(b)) for a, b in csv.reader(open('out.csv'))))
import matplotlib.pyplot as plt
plt.plot(xs, ys); plt.xlabel('sample'); plt.ylabel('envelope'); plt.show()"
```

**需要观察的现象与预期结果**（待本地验证）：

1. **包络恢复**：稳态输出在 \(8000 \sim 24000\) 之间以 400Hz 摆动（\(16000 \times (1 \pm 0.5)\)），周期 = \(48000/400 = 120\) 个样本。这验证了「包络频率与调制信号一致」。
2. **块边界无接缝**：输出在每 240 帧的块边界上连续——因为 `lp_i/lp_q/nco1_phase` 都是跨调用保持的状态（对齐固件的全局变量语义）。试着把 `lp_i/lp_q` 挪进函数内部变成局部变量，块边界会出现周期性的台阶/凹陷，这就是 u3-l2 反复强调「状态必须跨回调保持」的可视化证明。
3. **启动暂态**：开头约 1ms 输出从 0 爬升到稳态（一阶低通从零状态收敛）——对应固件里模式切换后共享滤波器状态的暂态（4.3.3）。
4. **残差免疫**：把 `PHASESTEP(FC)` 换成精确的浮点步进 13653.333，输出几乎不变——0.24Hz 的旋转不影响模值（练习 4.4-3）。

**延伸实验**（任选）：

- **滤波器的作用**：把 `FM` 改成 20000（调制音超出 7800Hz 通带），输出应塌缩为接近恒定的载波包络，20kHz 信息被滤除。
- **搬移方向**：把混频循环里的 `nco1_phase -= ...` 改成 `+=`，信号被搬到 20kHz 后被低通滤掉，输出只剩噪声级残留——亲手复现 4.2.2 说的「符号错则信号被滤掉」。
- **`#else` 对比**：按 4.5.4 加入 `am_demod_else_pc()` 并排输出，观察负直流偏置与电平依赖。

**有硬件的额外验证**（可选，待本地验证）：连接 USB 后执行 `tune 567000`、`mode am`，然后 `python python/centsdr.py -p 0` 抓 B_CAPTURE——复数频谱上应能看到 +10kHz 处的载波谱线；`-p 3` 抓 B_PLAYBACK——时域波形应就是本实践模拟出的包络形状（`-p` 编号对应 `buffers_table` 的 B_CAPTURE/B_IF1/B_IF2/B_PLAYBACK，见 [python/centsdr.py:L77-L86](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/centsdr.py#L77-L86) 与 [python/centsdr.py:L135-L136](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/centsdr.py#L135-L136)）。

## 6. 本讲小结

- `AM_FREQ_OFFSET = 10000` 在整机里出现两次：`set_tune()` 让本振**偏低 10kHz**（模拟域避开直流黑洞），`am_demod()` 的 NCO 用 `PHASESTEP(10000) = 13653` 把信号**搬回 0Hz**（数字域安全），两级恰好抵消。
- `am_demod` 三段流水线：NCO 复数混频（与 `demod_weaver` 第一级同源）→ 6 阶椭圆低通 7800Hz（信道选择性，防止包络检波的交叉调制）→ `vsqrt.f32` 取模得包络。
- 包络检波取模对相位旋转免疫：`PHASESTEP` 整数截断留下的 0.24Hz 残差完全无害；但符号方向错会让信号被低通滤掉，靠 `freq_inverse` 等配置保证净频谱方向正确。
- `_VSQRTF` 用内联汇编直接发射 FPU 的 `vsqrt.f32`（约 14 周期），绕开 `sqrtf` 的库调用与 errno 检查；\(I^2+Q^2\) 在 int32 中的上限 \(2\times32767^2\) 距离 \(2^{31}\) 只差 131069——而检波循环里两句钳位写在 `(int16_t)` 强转之后，按执行顺序实际空转，真正的越界防护来自 ADC 限幅与 AGC。
- 编译期 `#if` 二选一：完整版有频移+滤波+电平无关性；`#else` 版是无滤波的直接检波，靠 `x/2` 防溢出、靠写死的 `DCOFFSET=16383`（满幅载波假设）扣直流，是演示级的最小实现。
- 滤波器状态内存被 AM/SSB/CW 三组实例共享、`nco1_phase` 被各模式共用、注释掉的 `ave_z` 实验——三处细节共同展示了这固件「内存抠门 + 实验留痕」的工程风格。

## 7. 下一步学习建议

- **下一讲 u3-l4（调频解调：反正切鉴频器）**：AM 提取的是复信号的**模**，FM 提取的是**辐角的变化率**——`fm_demod()` 用相邻样本共轭相乘取虚部（`atan_2iq`）实现鉴频，与本讲形成完美对照；届时回头再看练习 4.4-3 会更有体会。
- **巩固本讲**：重读 [dsp.c:L346-L407](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L346-L407)（`demod_weaver` 与 cw/usb/lsb 包装函数），确认你能说出 AM 与 SSB/CW 在「第二级之后」的分岔点。
- **向前看**：u4-l1 会讲 `disp_fetch_samples` 如何借本讲的四个抓取点驱动频谱显示；u5-l4 的二次开发实践（SAM 同步 AM 解调）将要求你直接改写本讲的 `am_demod`——把它记熟。
