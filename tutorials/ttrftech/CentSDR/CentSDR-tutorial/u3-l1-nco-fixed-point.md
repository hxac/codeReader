# DSP 地基：定点表示、NCO 与三角函数表

## 1. 本讲目标

学完本讲，你应该能够：

1. 解释 q15/q31 定点格式的数值范围、分辨率与饱和（溢出）问题，看懂 `>> 15` 这类「重归一化」移位。
2. 推导 [nanosdr.h](nanosdr.h) 中 `PHASESTEP(freq)` 宏如何把「频率」换算成「每个采样点的相位增量」，并手工算出实际输出频率与误差。
3. 读懂 [dsp.c](dsp.c) 中 256 项的 `cos_sin_table` 与 `cos_sin()`：一张「值 + 斜率」双列表，如何用一条 SIMD 乘加指令完成线性内插，同时产出正交的一对载波。
4. 看懂用 `__SMLAD` / `__SMLSDX` / `__PKHBT` 实现的复数混频，理解它为什么比普通 C 写法快约 4 倍。

本讲是单元三的第一讲。上一讲（u2-l3）我们已经知道：I2S 每 5ms 产生 240 帧交织 IQ 样本，`i2s_end_callback` 通过函数指针 `signal_process` 调用解调函数。从本讲起，我们打开这些解调函数共同依赖的三块地基——定点数、NCO（数控振荡器）和 SIMD 混频。

## 2. 前置知识

### 2.1 什么是定点数

普通 C 程序用 `float` 存小数，而 Cortex-M4 虽有 FPU，但大多数实时 DSP 代码更喜欢**定点数**（fixed-point）：用普通整数表示小数，靠「约定小数点位置」来解释。

- **q15**：16 位整数，1 位符号 + 15 位小数。数值范围 \([-1, 1)\)，即 \(-32768 \sim 32767\)，分辨率 \(2^{-15} \approx 3.05 \times 10^{-5}\)。整数值 `x` 代表实数 \(x / 32768\)。
- **q31**：32 位整数，1 位符号 + 31 位小数，范围同样 \([-1, 1)\)，分辨率 \(2^{-31}\)。

定点乘法的位规则：两个 q15 相乘得 q30（30 位小数），存进 `int32_t`；要变回 q15 就算术右移 15 位。你在 dsp.c 里到处看到的 `>> 15` 就是这个「重归一化」。

**饱和（saturation）问题是定点的代价**：q15 最大只能表示 0.99997，两个接近满幅的 q15 相加会溢出成负数（回绕），产生严重失真。应对办法要么预留余量（信号不满幅），要么显式饱和，例如 `__SSAT(x, 16)` 或手动钳位。

### 2.2 什么是 NCO

NCO（Numerically Controlled Oscillator，数控振荡器）就是「用代码生成的正弦波发生器」。它只需要两样东西：

1. 一个**相位累加器**：每个采样周期累加一个固定增量 `phasestep`，累加器溢出回绕正好等价于相位转过 \(2\pi\) 回到原点。
2. 一个**相位→幅度**的转换器：把相位换算成 sin/cos 值。CentSDR 用查表 + 内插实现（本讲主角）。

NCO 是所有解调算法的「本地振荡器」：把接收到的一定带宽的信号乘上一个旋转复指数 \(e^{j\phi}\)，就能把它在频率轴上整体平移——SSB/AM 的频谱搬移、CW 的差拍音，全靠它。

### 2.3 什么是 SIMD

Cortex-M4 的 SIMD（单指令多数据）指令可以把一个 32 位寄存器当作**两个 16 位半字**并行处理，例如一条 `SMLAD` 同时完成两组 16 位乘法并累加。CMSIS 头文件把它们封装成 `__SMLAD()`、`__PKHBT()` 这样的内建函数（intrinsic），在 C 里直接可用。本讲会碰到其中四个，见 4.4 节的对照表。

## 3. 本讲源码地图

| 文件 | 本讲关注点 |
| --- | --- |
| [dsp.c](dsp.c) | `cos_sin_table`、`cos_sin()`、`nco1_phase/nco2_phase`、`demod_weaver` 中的混频循环、`PHASESTEP_NCO19KHz` 32 位 NCO |
| [nanosdr.h](nanosdr.h) | `FS`、`AM_FREQ_OFFSET`、`SSB_FREQ_OFFSET`、`PHASESTEP` 宏，`q15_t` 相关类型与解调函数声明 |
| [main.c](main.c) | `mod_table`、`set_modulation()` 中相位步进的运行期设置 |

## 4. 核心概念与源码讲解

### 4.1 定点数在 dsp.c 中的样子：q15、重归一化与饱和

#### 4.1.1 概念说明

CentSDR 的整条音频链路都是 q15：I2S DMA 搬进来的交织 IQ 样本是 `int16_t`，中间缓冲、滤波器系数（`q15_t bq_coeffs[]`）、解调输出也全是 `int16_t`。只有个别地方（如 AM 包络的 `vsqrt.f32`、FM 立体声 PLL 的 `double` 计算）借用浮点。

要理解任何一行 dsp.c 的运算，先掌握两条规则：

1. **乘法后右移**：q15 × q15 = q30，`>> 15` 回到 q15。
2. **和会超范围**：两个满幅 q15 之和最大接近 2.0，超出 \([-1,1)\)，必须留意饱和。

#### 4.1.2 核心流程

以 AM 包络检波的一个样本为例（下一讲 u3-l3 详细讲，这里只看定点运算）：

```text
x (q15) ──┐
          ├─→ z = sqrt(x² + y²)   ← 幅度，单位仍是 q15
y (q15) ──┘
   z 上限 = sqrt(2)·32767 ≈ 46341 > 32767  ← 会溢出 int16！
   ↓
   手动钳位到 [-32768, 32767]
```

#### 4.1.3 源码精读

dsp.c 的 AM 检波循环末尾有一段教科书式的**手动饱和**：

- [dsp.c:L450-L463](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L450-L463) —— 对滤波后的 I/Q 求模 `z = sqrt(x*x + y*y)`（用 M4 的 `vsqrt.f32` 硬件指令），随后 `if (z > 32767) z = 32767; if (z < -32768) z = -32768;` 把结果钳回 int16 范围，再用 `__PKHBT(z, z, 16)` 把同一个值复制到左右声道两个半字。

  为什么这里会溢出？\(x, y \in [-32767, 32767]\)，模长最大 \(32767\sqrt{2} \approx 46341\)，装不进 int16。这就是 q15 的「动态范围」问题：**求模、求和这类非线性运算会把数值放大到格式之外**。

再看 `demod_weaver` 里的重归一化（完整解读在 4.4 节）：

- [dsp.c:L358-L364](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L358-L364) —— `__SMLSDX(iq, cossin, 0) >> (15-0)`：SIMD 乘加的结果是两个 q15 乘积之和（数值可达 \(2 \times (1-2^{-15})^2 \approx 2\)），`>> 15` 之后理论上仍可能到 65533——**满幅输入时这里其实存在回绕风险**，实际系统中信号经 AGC 控制远低于满幅，所以作者没有再钳位（这是我的分析，源码未加注释）。

另外注意 [dsp.c:L8](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L8) 的表头：`const int16_t cos_sin_table[256][2]`，最大值是 **32767 而不是 32768**——因为 \(+32768\) 超出 int16，q15 的正满幅必须「差一」。

#### 4.1.4 代码实践

1. **实践目标**：亲手感受 q15 的乘法与饱和。
2. **操作步骤**：在 PC 上编译运行下面这段示例代码（示例代码，非项目源码）：

   ```c
   #include <stdio.h>
   int main(void) {
       int32_t a = 32767, b = 32767;          // 两个 ~1.0 的 q15
       printf("q15 乘积 (q30) = %d\n", a * b);
       printf("回 q15 后      = %d (期望约 32766)\n", (int16_t)((a * b) >> 15));
       int32_t s = a + b;                     // 两个 ~1.0 相加
       printf("和 = %d, 塞回 int16 = %d (回绕成负数!)\n",
              s, (int16_t)s);
       return 0;
   }
   ```

3. **需要观察的现象**：乘积移位后正常；而直接把和塞回 `int16_t` 会从正数翻转成负数。
4. **预期结果**：`65534` → `32767`；`和 = 65534` → `(int16_t)65534 = -2`。这就是为什么 DSP 代码里到处是移位和 `__SSAT`。

#### 4.1.5 小练习与答案

**练习 1**：q15 能表示的最大正数和最小负数分别是多少？为什么不对称？
**答案**：+32767（≈ 0.99997）和 −32768（= −1.0）。补码表示中负数比正数多一个码点。

**练习 2**：两个 q15 数相乘，结果应存入什么类型？移位多少位回到 q15？
**答案**：`int32_t`（q30 需要 30 位小数 + 1 符号位）；算术右移 15 位。

**练习 3**：`am_demod` 为什么要对 `z` 手动钳位，而 `demod_weaver` 的混频输出没有钳位？
**答案**：求模的最大输出 \(32767\sqrt{2}\) 一定超范围；混频输出理论上限约 65533 也可能超，但依赖 AGC 保证输入远低于满幅，故作者省略了钳位（属于已知的设计取舍）。

---

### 4.2 PHASESTEP 宏与 16 位相位累加器

#### 4.2.1 概念说明

NCO 的「振荡频率」由**每个采样点的相位增量**决定。CentSDR 用 16 位无符号数表示一整圈相位：65536 ≡ \(2\pi\)。设采样率为 \(FS\)、目标频率为 \(f\)，则每步相位增量为

\[
\text{phasestep} = \frac{2\pi f}{FS} \div \frac{2\pi}{65536} = \frac{65536 \times f}{FS}
\]

这正是 [nanosdr.h](nanosdr.h) 里那个宏。

#### 4.2.2 核心流程

```text
uistat.cw_tone_freq (Hz)
        │  set_modulation() / update_cwtone()
        ▼
cw_tone_phasestep = PHASESTEP(freq)        ← 编译期/运行期换算成整数步进
        │
        ▼  每来一个 IQ 样本（5ms 一批，每批 240 帧）
nco1_phase -= phasestep;                    ← 16 位自然回绕 = 相位转圈
        │
        ▼
cos_sin(nco1_phase) → {cos, sin} 一对 q15 载波
```

三个直接推论：

- **频率分辨率**：步进是整数，最小可分辨频率为 \(FS/65536 = 48000/65536 \approx 0.73\) Hz。
- **截断误差**：`65536L * freq / FS` 是整数除法（截断），实际频率 \(f_{actual} = \text{step} \times FS / 65536\)，最大偏差不超过 0.73 Hz。
- **负频率**：步进取负（LSB 模式），累加器向回走，等价于共轭旋转 \(e^{-j\phi}\)，用来选择另一个边带。

#### 4.2.3 源码精读

- [nanosdr.h:L125-L128](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L125-L128) —— 宏定义一字排开：`FS 48000`、`AM_FREQ_OFFSET 10000`、`SSB_FREQ_OFFSET 1300`、`PHASESTEP(freq) (65536L * freq / FS)`。注意 `FS` **写死 48000**（u2-l3 已提示过：这只在 48kHz 档位精确）。

- [main.c:L113-L116](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L113-L116) —— 相位步进变量与默认值：`cw_tone_phasestep = PHASESTEP(800)`，即 CW 侧音默认 800Hz。

- [main.c:L170-L177](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L170-L177) —— `mod_table`：每种解调模式的 `(demod_func, freq_offset, fs)` 三元组。CW/AM 带外加 10kHz 频偏（下一讲解释为什么），FM 系列用 192kHz 采样。

- [main.c:L188-L190](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L188-L190) —— `set_modulation()` 在切换模式时重算两组步进：`mode_freqoffset_phasestep = PHASESTEP(mode_freq_offset)`（AM/CW 的 10kHz 搬移）和 `cw_tone_phasestep = PHASESTEP(uistat.cw_tone_freq)`（CW 差拍音）。CW 音调在运行期由 [main.c:L229-L232](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L229-L232) 的 `update_cwtone()` 单独刷新。

- [dsp.c:L284-L286](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L284-L286) —— 两个 16 位相位累加器 `nco1_phase`（第一次混频）、`nco2_phase`（Weaver 第二次混频），以及 `SSB_NCO_PHASESTEP = PHASESTEP(SSB_FREQ_OFFSET)`。**它们是全局变量而不是函数内局部变量**——相位必须跨 5ms 缓冲块连续，否则每块开头都会出现相位跳变（可闻的咔哒声与杂散）。

除了这套 16 位 NCO，dsp.c 里还有一个 **32 位变体**，专用于 FM 立体声 19kHz 导频：

- [dsp.c:L593-L594](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L593-L594) —— `PHASESTEP_NCO19KHz = (19.0*65536.0*65536.0)/IF_RATE`，`IF_RATE` 为 192.0。这里用 `double` 算出 32 位步进（425,022,805，对应 18999.999 Hz），分辨率 \(192000/2^{32} \approx 4.5 \times 10^{-5}\) Hz——因为导频 PLL 需要极细的频率微调去跟踪广播台。
- [dsp.c:L624-L625](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L624-L625) —— 使用方式：`cos_sin(phase_accum >> 16)`，32 位累加器取高 16 位喂给同一个 `cos_sin()`。这是「细频率分辨率」与「小查表」兼得的标准手法。

#### 4.2.4 代码实践

1. **实践目标**：会手工计算 `PHASESTEP` 并量化截断误差。
2. **操作步骤**：纸上或用一行 C/Python 计算 `PHASESTEP(1300)`、`PHASESTEP(10000)`、`PHASESTEP(800)`，再反算实际频率 \(f_{actual} = \text{step} \times 48000 / 65536\)。
3. **需要观察的现象**：三个 step 分别截断掉多少小数；反算频率偏差多少赫兹。
4. **预期结果**：
   - `PHASESTEP(1300)` = 85,196,800 / 48,000 = 1774.93 → **1774**，实际 1299.51 Hz，偏差 −0.49 Hz；
   - `PHASESTEP(10000)` = 13653.33 → **13653**，实际 9999.76 Hz，偏差 −0.24 Hz；
   - `PHASESTEP(800)` = 1092.27 → **1092**，实际 799.80 Hz，偏差 −0.2 Hz。
   亚赫兹级误差对音频解调完全无感——这就是 16 位相位累加器够用的原因。

#### 4.2.5 小练习与答案

**练习 1**：`nco1_phase` 为什么必须是全局/静态变量，而不是 `demod_weaver` 里的局部变量？
**答案**：解调函数每 5ms 被 `signal_process` 调用一次，局部变量每次归零，载波相位每块都从 0 跳变，输出会出现周期性毛刺；全局累加器保证相位跨块连续。

**练习 2**：如果实际采样率是 192kHz，仍用 `PHASESTEP(f)`（分母 `FS`=48000）设定 NCO，实际输出频率是多少？
**答案**：\(f \times 192000/48000 = 4f\)，频率抬高 4 倍。所以 FM 立体声路径专门用 `PHASESTEP_NCO19KHz`（分母 192.0）另算步进。

**练习 3**：`lsb_demod_conf` 里步进取负号（`-SSB_NCO_PHASESTEP`）在数学上等价于什么？
**答案**：NCO 反向旋转，即乘以共轭复指数 \(e^{-j\phi}\)，频谱向相反方向搬移，从而选出另一个边带。

---

### 4.3 cos_sin_table 与 cos_sin()：「值 + 斜率」双列查表

#### 4.3.1 概念说明

查表法最朴素的方案是：存 65536 个 sin 值，相位直接当索引。但表要占 128KB RAM——STM32F303 只有 40KB。另一个极端是只存少量点，相位精度骤降、杂散暴涨。

CentSDR 的方案只用 **256 项 × 2 列 × 2 字节 = 1KB**，却能把相位精度做到约 ±3 LSB（≈ −80 dB）。诀窍在于每项存**两列**：

- **第 1 列（cos 值列）**：满幅 q15 的余弦采样（带一个固定的 π 相位偏移与符号，见下）；
- **第 2 列（斜率列）**：恰等于第 1 列的**前向差分** `col0[i+1] − col0[i]`，即「走一步值会变多少」。

于是任意相位处的值 = 表项值 + 小数比例 × 斜率——这正是**相邻两点之间的线性内插**，只是把「取两个表项再加权平均」改写成「取一个表项 + 它的斜率」，让一条乘加指令就能完成。

#### 4.3.2 核心流程

`cos_sin(phase)`（phase 为 16 位，65536 ≡ \(2\pi\)）：

```text
si  = phase >> 8          ← 表索引（256 项，每项覆盖 256 个相位单位）
mod = phase & 0xff        ← 表内小数部分 (0..255)
r   = { 高半字: mod, 低半字: 256 }     ← 内插权重，8.8 定点，和恒为 256

正弦输出 s = ( 256·col0[si] + mod·col1[si] ) / 256
余弦输出 c = 同一公式，索引换成 ci = (si + 64) & 0xff   ← 1/4 圈 = 90°

返回一个 32 位字：{ 高半字: c, 低半字: s }
```

数学上（\(\theta_i = 2\pi i/256\)，\(\delta = \text{mod}/256\) 个表步长）：

\[
s = \text{col0}[i] + \delta \cdot (\text{col0}[i+1] - \text{col0}[i]) \approx -32767\cos(\theta_i + \delta\Delta)
\]
\[
c = s\big|_{i \to i+64} \approx +32767\sin(\theta_i + \delta\Delta), \qquad \Delta = \frac{2\pi}{256}
\]

即返回的一对半字是**严格相差 90° 的正交载波** \((\sin\phi, -\cos\phi)\)，只是相对名字里的 cos/sin 整体偏了固定相位并翻转了符号——对收音机而言这仅等价于把解调结果乘一个固定复系数，完全无害。

**误差预算**（回答「256 项为什么够」）：

- 线性内插误差上界 \(\dfrac{h^2 |f''|_{max}}{8} = \dfrac{(2\pi/256)^2 \times 32767}{8} \approx 2.5\) LSB；
- 表值取整 ±0.5 LSB、`/256` 截断约 ±1 LSB；
- 合计约 **±4 LSB / 32767**，信杂比约 \(20\log_{10}(32767/4) \approx 78\) dB——远好于 16 位音频链路本身和前端模拟电路，也优于机内 60dB 椭圆滤波器的阻带指标。继续加表长度对整机噪声没有可闻收益，1KB 的代价恰到好处。

#### 4.3.3 源码精读

- [dsp.c:L8-L265](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L8-L265) —— `cos_sin_table[256][2]`。抽查几个点即可自证结构：第 0 行 `{-32767, 10}`、第 64 行 `{0, 804}`、第 128 行 `{32767, -10}`——第 1 列正是 \(-\cos\theta \cdot 32767\) 过零、峰谷分明的余弦形状；第 2 列最大 804，恰是 \(32767 \times 2\pi/256\)。再验证斜率关系：`col0[1] − col0[0] = -32757 − (-32767) = 10`，正是第 0 行第 2 列的值；第 255 行的斜率 `-10` 也满足循环回绕 `col0[0] − col0[255]`。

- [dsp.c:L267-L281](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L267-L281) —— `cos_sin()` 全文仅 10 行：
  - `r = __PKHBT(0x0100, mod, 16)` 把权重打包成一个字：低半字 `0x0100`（= 256，即 8.8 定点里的「1.0」），高半字 `mod`。两个权重之和恒为 `256 + mod` 归一时无需求逆。
  - `cd = *(uint32_t *)&cos_sin_table[ci]` 一次 32 位读取同时拿到「值 + 斜率」两列。
  - `__SMUAD(r, cd)` = `256·值 + mod·斜率`，一条指令完成内插乘加；`/256` 落回 q15。
  - `return __PKHBT(s, c, 16)` 把 cos、sin 打包进一个 32 位字返回——调用方（4.4 节的混频循环）直接把它当 SIMD 操作数用，省一次拆包。

#### 4.3.4 代码实践（本讲主实践）

1. **实践目标**：把 `cos_sin_table` 和 `cos_sin()` 提取到 PC 上，用标准 C 整数运算替换 SIMD 内建函数，扫描全部 65536 个相位，与 libm 对比最大误差，验证「256 项足够」的结论。
2. **操作步骤**：
   1. 新建 `nco_test.c`，把 [dsp.c:L8-L265](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L8-L265) 的整个表原样复制进来（纯 `const int16_t` 数组，无任何依赖）。
   2. 写可移植版 `cos_sin()`（示例代码）：

      ```c
      #include <stdio.h>
      #include <math.h>

      /* 与 dsp.c 等价：用普通整数运算替换 __PKHBT / __SMUAD */
      static uint32_t
      cos_sin(uint16_t phase)
      {
          uint16_t mod = phase & 0xff;
          uint16_t si = phase / 256;
          uint16_t ci = (si + 64) & 0xff;
          /* r = { lo:256, hi:mod }；表项打包成 { lo:col0, hi:col1 } */
          int32_t c = 256 * (int32_t)cos_sin_table[ci][0]
                    + (int32_t)mod * cos_sin_table[ci][1];
          int32_t s = 256 * (int32_t)cos_sin_table[si][0]
                    + (int32_t)mod * cos_sin_table[si][1];
          c /= 256;
          s /= 256;
          return ((uint32_t)(uint16_t)c << 16) | (uint32_t)(uint16_t)s;
      }

      int main(void)
      {
          int max_err_c = 0, max_err_s = 0;
          for (uint32_t ph = 0; ph < 65536; ph++) {
              uint32_t cs = cos_sin((uint16_t)ph);
              double phi = 2.0 * M_PI * ph / 65536.0;
              double ref_c =  sin(phi) * 32767.0;   /* 高半字 */
              double ref_s = -cos(phi) * 32767.0;   /* 低半字 */
              int ec = (int16_t)(cs >> 16) - (int)lround(ref_c);
              int es = (int16_t)(cs      ) - (int)lround(ref_s);
              if (ec > max_err_c) max_err_c = ec;
              if (es > max_err_s) max_err_s = es;
          }
          printf("max err: cos %+d LSB, sin %+d LSB\n",
                 max_err_c, max_err_s);

          /* 附加验证：col1 恰为 col0 的循环前向差分 */
          int worst = 0;
          for (int i = 0; i < 256; i++)
              if (cos_sin_table[i][1] !=
                  cos_sin_table[(i + 1) & 255][0] - cos_sin_table[i][0])
                  worst++;
          printf("差分关系不符的表项数: %d (期望 0)\n", worst);
          return 0;
      }
      ```

      （注意：`__SMUAD` 乘的是**有符号** 16 位半字，移植时 `cos_sin_table` 的 `int16_t` 元素直接参与 `int32_t` 运算即可保持语义；`c /= 256` 与 `>> 8` 在负数时舍入方向不同，差 1 LSB 量级。）
   3. 编译运行：`gcc -O2 -o nco_test nco_test.c -lm && ./nco_test`。
3. **需要观察的现象**：两个最大误差各是多少 LSB；256 项差分关系是否全部吻合。
4. **预期结果**：最大误差在 ±5 LSB 量级（内插 ~2.5 + 舍入 ~1.5），差分关系 0 项不符——由此可直接回答「为什么 256 项仍可接受」：误差折合约 −78 dB，低于整机其余环节的失真底。具体数值待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：把 `__PKHBT(0x0100, mod, 16)` 里的 `0x0100` 改成 `0x00ff` 会发生什么？
**答案**：值项权重从 256 变成 255，输出整体乘上约 255/256（−0.03 dB）且不再精确经过表点，引入系统性幅度误差；`0x0100` 正是 8.8 定点中的「1.0」。

**练习 2**：`ci = (si + 64) & 0xff` 中的 64 从何而来？
**答案**：表有 256 项对应一整圈 \(2\pi\)，\(90° = 2\pi/4\) 对应 \(256/4 = 64\) 项。索引偏移 64 即从 sin 列取值得到正交的 cos 列，省掉第二张表。

**练习 3**：若把表扩展为 512 项（其余机制不变），内插误差大约降低多少？
**答案**：内插误差 \(\propto h^2\)，点距减半误差降为 1/4，约 0.6 LSB——收益已经小于表值取整误差（±0.5 LSB），所以作者停在 256 项。

---

### 4.4 SIMD 复数混频：__SMLAD / __SMLSDX / __PKHBT

#### 4.4.1 概念说明

频率搬移的数学核心是复数乘法：

\[
(I + jQ)(\cos\phi - j\sin\phi) = \underbrace{I\cos\phi + Q\sin\phi}_{I'} + j\underbrace{Q\cos\phi - I\sin\phi}_{Q'}
\]

一次样本混频需要 **4 次乘法 + 2 次加法**。SIMD 的思路：把 `{I, Q}` 打包成一个 32 位字、把 `{cos, sin}` 打包成另一个字，一条「双 16 位乘加」就能算出 \(I'\) 或 \(Q'\)——两条指令完成整次复数乘。这正是 `cos_sin()` 返回打包字的用意：它产的载波天生就是 SIMD 操作数。

本讲涉及的四个内建函数（都是 CMSIS 对 Cortex-M4 SIMD 指令的封装，`lo`/`hi` 指 32 位字的低/高 16 位半字）：

| 内建函数 | 语义 | 本讲用途 |
| --- | --- | --- |
| `__PKHBT(a, b, 16)` | 打包 `{lo: a, hi: b}` | 组装/拆分 IQ 对与载波对 |
| `__SMUAD(x, y)` | `x.lo·y.lo + x.hi·y.hi` | cos_sin 内插乘加 |
| `__SMLAD(x, y, acc)` | `acc + x.lo·y.lo + x.hi·y.hi` | 混频出一个分量 |
| `__SMLSDX(x, y, acc)` | `acc + x.lo·y.hi − x.hi·y.lo` | 混频出另一个分量 |

（`__SIMD32(p)` 把 `int16_t*` 转成 `int32_t*`，让指针一次步进两个样本；`__SSAT(x,16)` 是带符号饱和，本讲末尾再遇到。）

#### 4.4.2 核心流程

`demod_weaver` 第一级混频（SSB/CW/AM 共用）对每个交织 IQ 样本：

```text
iq      = { lo: I, hi: Q }                ← 一次 32 位读入（__SIMD32 视角）
cossin  = { lo: s, hi: c }                ← cos_sin() 的打包返回值

I' = __SMLSDX(iq, cossin, 0) >> 15        = (I·c − Q·s) >> 15
Q' = __SMLAD (iq, cossin, 0) >> 15        = (I·s + Q·c) >> 15

nco1_phase -= phasestep                   ← 相位推进到下一个样本
```

对照 4.4.1 的公式：`SMLAD`/`SMLSDX` 的交叉/直连搭配恰好凑出复数乘的两个输出（载波符号约定见 4.3.2，正负频率对应加减 phasestep）。对比普通 C：4 次乘、2 次加、2 次移位、多次 16 位装拆，这里压成 2 条乘加 + 1 条移位 + 1 条打包读，且无需拆包——实测 DSP 负载（`stat` 命令的 load 指标）因此显著下降。

#### 4.4.3 源码精读

- [dsp.c:L351-L352](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L351-L352) —— `__SIMD32(src)` 把交织 `int16_t` 缓冲当作 `int32_t` 数组遍历：循环变量每步进 1，实际处理 2 个样本（一对 IQ）。

- [dsp.c:L357-L364](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L357-L364) —— 第一级混频循环全文。每轮：`cos_sin(nco1_phase)` 产载波 → `nco1_phase -= dc->phasestep`（注意是**减**，LSB 配置里再取负步进就变回正转）→ `__SMLSDX`/`__SMLAD` 两条乘加分别产出 I'、Q' 写入**分离平面**缓冲 `buffer[0]`/`buffer[1]`（u2-l3 讲过的「拆交织」步骤在这里完成）。

- [dsp.c:L373-L382](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L373-L382) —— Weaver 第二级混频（滤波之后把音频搬回基带）：`__PKHBT(*bufi++, *bufq++, 16)` 把分离的 I/Q 重新打包成 SIMD 字，单条 `__SMLAD` 取实部 `r`，再 `__PKHBT(r, r, 16)` 把同一个音频值复制到立体声左右两个半字——输出交织的实数音频。

#### 4.4.4 代码实践

1. **实践目标**：不靠硬件，用手工演算验证两条 SIMD 混频公式的正确性。
2. **操作步骤**：取 `phase = 0`，由 4.3 的分析知 `cossin = { hi: 0, lo: -32767 }`；设输入 `iq = { lo: 16384, hi: 0 }`（即 I=0.5、Q=0 的直流复信号）。手算：
   - `__SMLSDX(iq, cossin, 0)` = `I·c − Q·s` = `16384·0 − 0·(-32767)` = 0，`>> 15` 得 `bufi = 0`；
   - `__SMLAD(iq, cossin, 0)` = `I·s + Q·c` = `16384·(-32767)` = −536,854,528，`>> 15` = **−16383**。
3. **需要观察的现象**：直流输入经混频后变成恒定值——幅度减半（0.5 × 0.99997 ≈ 16383），这正是「NCO 在 phase=0 处的瞬时载波值乘上输入」。
4. **预期结果**：`bufi = 0`、`bufq = -16383`。若把 `phase` 推进一个步进再算，输出值会随之旋转——读者可再用 `PHASESTEP(1300)` 的步进 1774 连算几个样本，观察正弦起伏（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `__SMLSDX`（交叉相乘再相减）恰好能和 `__SMLAD`（直连相乘相加）配对成复数乘法？
**答案**：复数乘 \((a+jb)(c+jd)\) 的实部是 \(ac - bd\)、虚部是 \(bc + ad\)。`SMLAD` 给出 `I·s + Q·c`、`SMLSDX` 给出 `I·c − Q·s`，二者分别是 \((I+jQ)\) 乘以 \((c − js)\) 的虚部与实部——两个分量一次配齐，只是顺序对调，不影响使用。

**练习 2**：`demod_weaver` 第二级混频 [dsp.c:L380-L381](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L380-L381) 里为什么只算 `r` 一个分量、还要 `__PKHBT(r, r, 16)` 复制成两份？
**答案**：SSB/CW 输出是实数音频，滤波后只需取复信号的一路（实部）；复制两份是因为 I2S 输出缓冲是交织的立体声格式，左右声道填同一个单声道值。

**练习 3**：满幅输入（\(|I|=|Q|=32767\)）时 `__SMLAD` 的结果会不会溢出 32 位？
**答案**：两个乘积之和最大 \(2 \times 32767^2 = 2\,147\,352\,578 < 2^{31}-1\)，32 位内安全；但 `>> 15` 后最大约 65533，塞回 `int16_t` 会回绕——所以该路径依赖 AGC 把输入压到远低于满幅（见 4.1.3 的分析）。

---

## 5. 综合实践

**任务：在 PC 上完整复刻 CentSDR 的 NCO，并量化它的频谱纯度。**

1. 在 4.3.4 的 `nco_test.c` 基础上，增加一个 `main` 之外的模式：以 `phasestep = PHASESTEP(1000)`（= 1365）循环推进 `nco1_phase`，取 `cos_sin()` 的高半字作为输出，生成 65536 个样本，按 q15 写入二进制文件 `nco_raw.bin`。
2. 用下面的 Python 片段（示例代码）读回并做频谱分析：

   ```python
   import numpy as np
   x = np.fromfile("nco_raw.bin", dtype="<i2").astype(np.float64) / 32768.0
   X = np.fft.rfft(x * np.hanning(len(x)))
   db = 20 * np.log10(np.abs(X) / np.abs(X).max() + 1e-12)
   print("最大杂散 (dBc):", np.sort(db)[-2])   # 除主峰外最高的一条
   ```

3. 对照三个问题收尾：
   - 主峰是否落在 999.76 Hz（`1365 × 48000 / 65536`，即 4.2.4 预测的截断频率）？
   - 最大杂散电平是否与 4.3.2 推算的 ~−78 dB 一致？
   - 把 `phasestep` 改成 1366（+1 LSB），主峰移动多少 Hz（应约 0.73 Hz，即分辨率）？
4. 若手头有真机，还可以用 `python/centsdr.py` 的 `data` 命令抓取 CAP 缓冲，与你的仿真输出并排画图对照（可选，待本地验证）。

这个任务把本讲四个模块串成一条线：q15 定点（样本格式）→ PHASESTEP（设频率）→ cos_sin 查表内插（产生载波）→（若进一步接到混频）SIMD 复数乘。

## 6. 本讲小结

- q15 用 int16 表示 \([-1,1)\)，乘积 `>> 15` 重归一化；求模/求和会越界，dsp.c 用手动钳位或预留余量应对饱和。
- `PHASESTEP(freq) = 65536·freq/FS` 把频率变成 16 位相位累加器的整数步进，分辨率 0.73 Hz；累加器必须是跨回调存活的全局变量，负步进即共轭旋转选另一个边带。
- `cos_sin_table` 是「值 + 前向差分（斜率）」双列结构，`cos_sin()` 用 `__PKHBT` 打包的 `{256, mod}` 权重和一条 `__SMUAD` 完成相邻表项间的线性内插，索引偏移 64 免费得到正交的 cos 输出。
- 256 项表的误差约 ±4 LSB（≈ −78 dB），已被 16 位音频链路和模拟前端淹没——这就是「表小仍可接受」的定量答案。
- `__SMLAD`/`__SMLSDX` 把 4 乘 2 加的复数混频压缩成两条乘加指令，`cos_sin()` 返回打包字使载波免拆包直接参与 SIMD 运算。
- FM 立体声的 19kHz 导频 NCO 是 32 位步进的增强版（`phase_accum >> 16` 喂表），用 `double` 算步进获得 \(4.5\times10^{-5}\) Hz 的微调分辨率。

## 7. 下一步学习建议

下一讲（u3-l2「Weaver 法 SSB/CW 解调」）将把本讲的 NCO 与 SIMD 混频装进完整链路：`demod_weaver()` 的「两次频移夹一级 CMSIS 椭圆低通」结构，以及 [python/SSB-Filter-Design.ipynb](python/SSB-Filter-Design.ipynb) 如何设计出 `bq_coeffs[]` 里那些 q15 系数。建议先自行浏览 [dsp.c:L289-L344](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L289-L344) 的 biquad 滤波器配置与 `weaver_demod_conf_t`，带着「usb/lsb 配置为何只有步进符号不同」这个问题进入下一讲。若想深挖 SIMD，可提前翻阅 [display.c](display.c) 中 `__QADD16`、`__SSAT` 的更多用法（u5-l2 会系统总结）。
