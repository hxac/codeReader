# 从 Notebook 到固件：算法设计与验证工作流

## 1. 本讲目标

学完本讲，你应该能够：

1. 独立复现 `python/SSB-Filter-Design.ipynb` 与 `python/CW-Filter-Design.ipynb` 中「椭圆 IIR 滤波器 → 双二阶级联 → q15 定点系数」的完整设计流程，并说清每一步（指标迭代、zpk 分解、增益分配、定点量化）存在的理由。
2. 沿着「notebook 输出的数字 → dsp.c 里的系数数组」这条链路逐字对照，确认固件中 `bq_coeffs`、`bq_coeffs_150hz` 以及 TLV320AIC3204 片内 mini-DSP 的 24 位系数分别来自哪个 notebook 单元格的哪行输出。
3. 把 `python/centsdr.py` 当作库来 `import`，写出一个扫频脚本：驱动整机画出接收选择性曲线，并解释实测曲线与设计指标之间的偏差从哪里来。

本讲是整套手册的收官篇之一：前四个单元分别讲了硬件链路、解调算法、显示与 UI、架构与扩展点，本讲把视角拉到「工程闭环」——算法如何在 PC 上设计、如何落地成固件常数、落地后又如何回到 PC 上验证。这正是嵌入式 DSP 开发区别于纯软件开发的典型工作流。

## 2. 前置知识

### 2.1 回顾：本讲要衔接的两条线索

- **u3-l2（Weaver 法 SSB/CW 解调）** 已经讲过 `demod_weaver()` 的三级流水，以及 CMSIS `arm_biquad_cascade_df1_q15` 的系数约定：排列为 `{b0, 0, b1, b2, a1, a2}`、反馈系数 `a` 取反、全体系数乘 16384（即 Q14）并配 `postShift = 1`。当时只是「引用」了这些结论，本讲回到系数的**出生地**——Jupyter notebook——看它们是怎么被算出来的。
- **u1-l4（USB Shell 与 Python 控制）** 已经讲过 shell 命令机制与 `centsdr.py` 的 `%04x ↔ '>h'` 编解码对。本讲从「把 `centsdr.py` 当模块用」的角度深化，用它搭建自动化测量脚本。

### 2.2 椭圆（elliptic / Cauer）滤波器是什么

IIR 滤波器设计就是在找一个有理传递函数

\[ H(z) = \frac{B(z)}{A(z)} = \frac{\sum_{i=0}^{N} b_i z^{-i}}{1 + \sum_{i=1}^{N} a_i z^{-i}} \]

使它的幅频响应落在规定的容差框内。经典逼近有三种：

| 类型 | 通带 | 阻带 | 特点 |
|------|------|------|------|
| Butterworth | 平坦 | 单调 | 最平但过渡带最宽 |
| Chebyshev I | 等波纹 | 单调 | 同阶过渡带更窄 |
| 椭圆（elliptic） | 等波纹 | 等波纹 | 同指标下阶数最低 |

椭圆滤波器在通带和阻带**都**允许波纹，因此同样「截止 1300Hz、阻带 60dB」的指标下，它所需的阶数最少——对要在中断里实时跑的固件来说，阶数就是 CPU 时间。scipy 的调用形式是：

```python
b, a = signal.ellip(N, rp, rs, Wn, 'low')   # N 阶、通带波纹 rp dB、阻带衰减 rs dB
```

其中 `Wn` 是归一化截止频率（相对 fs/2）。注意 notebook 里写的是 `1300.0/24000`——即 1300Hz 除以 48000/2，正是一个 48kHz 系统的归一化值。

### 2.3 为什么必须拆成双二阶（biquad）级联

一个 6 阶传递函数如果直接以多项式系数 `{b0..b6, a1..a6}` 实现，定点运算里会严重数值病态——高次多项式的根对系数扰动极其敏感，q15 的量化步进（1/32768）足以把极点推出单位圆使滤波器发散。工程做法是因式分解成三个二阶节（section）级联：

\[ H(z) = \prod_{j=1}^{3} H_j(z), \qquad H_j(z) = k_j\,\frac{(z-z_j)(z-z_j^{*})}{(z-p_j)(z-p_j^{*})} \]

每个二阶节系数动态范围小、可独立分配增益、状态量少，是定点 IIR 的事实标准形态（CMSIS 的 biquad cascade、音频插件里的 "biquad" 都指它）。

问题在于：**已经展开成 `b, a` 多项式的形式难以再可靠分解**（数值上等于对 6 次多项式求根，误差大）。所以 notebook 的做法是让 scipy 直接以零点-极点-增益形式（zpk）输出设计结果，在分解之前就不展开。

### 2.4 两套定点格式速查

| 落点 | 格式 | 量化公式 | 说明 |
|------|------|----------|------|
| `dsp.c`（STM32 侧 CMSIS） | Q14 存于 int16 | \( c = \mathrm{rint}(x \times 16384) \) | 反馈系数可达 2.0，装不进 Q15，故整体右移 1 位，用 `postShift=1` 在输出端补回 |
| `tlv320aic3204.c`（codec mini-DSP） | 符号 24 位定点 | \( c = \mathrm{rint}(x \times 2^{23}) \) | 1 个符号位 + 23 个小数位，±0.99999988 的表示范围 |

两处都要注意符号约定的差异：scipy 的差分方程是减反馈，CMSIS 与 codec mini-DSP 都是加反馈，所以 `a` 系数在搬运时**要取反**。

### 2.5 Python 2 与 Python 3

`python/README.md` 明确要求 python 2.7（[python/README.md:L18-L26](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/README.md#L18-L26)），`centsdr.py` 也用了 `print` 语句和 `bytes.decode('hex')` 这类 Python 2 专属写法。本讲的实践分两类：

- **跑 notebook 的设计部分**：只依赖 numpy/scipy，建议直接用 Python 3 重写（本文给出等价代码）；
- **跑 `centsdr.py` 的验证部分**：要么装 python2.7，要么先打一个约 5 行的移植补丁（见 4.4.4）。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| `python/SSB-Filter-Design.ipynb` | SSB 模式 1300Hz 椭圆低通的设计现场：从 40dB 迭代到 60dB、zpk 分解、增益分配、q15 量化 |
| `python/CW-Filter-Design.ipynb` | CW 模式 150Hz 椭圆低通，流程同上，多了一个自动生成 C 数组的 `bq()` 函数 |
| `python/TLV320AIC3204-1st-IIR-HPF.ipynb` | codec 片内一阶高通（DC 抑制）设计：24 位定点、3 字节大端十六进制输出 |
| `python/centsdr.py` | 模块兼 CLI：封装 shell 命令、抓取内部缓冲、读功率，是验证端的抓手 |
| `python/README.md` | python 目录的使用说明：依赖、设备指定、缓冲编号含义、import 用法 |
| `dsp.c` | 设计结果的落点：`bq_coeffs`（SSB）、`bq_coeffs_150hz`（CW）系数数组及其实例化 |
| `tlv320aic3204.c` | 24 位系数的落点：`adc_iir_filter_dcreject2` 字节表与 `tlv320aic3204_config_adc_filter2` |
| `main.c` | 验证端依赖的固件侧命令：`data`（缓冲转储）、`power`（功率读数）、`tune` |

> 提示：`.ipynb` 本质是 JSON 文件，本文对 notebook 的行号引用指向 GitHub 上该 JSON 的原始行——点击链接后看到的是 JSON 源码，`"source"` 字段里就是 cell 的代码。这是给 notebook 内容打永久锚点的可靠方式。

## 4. 核心概念与源码讲解

本讲的四个最小模块正好构成一条流水线：

```
[模块一/二] PC 上设计      [模块三] 另一路径         [模块四] PC 上验证
SSB/CW 椭圆滤波器 notebook  codec HPF notebook 24bit   centsdr.py 扫频/抓波形
        │ q15 系数                  │ 24bit 系数                │
        ▼                           ▼                          ▼
   dsp.c bq_coeffs_*       tlv320aic3204.c 系数表      实测曲线 vs 设计指标
```

### 4.1 模块一：SSB 滤波器 notebook——从指标到 q15 系数的五步法

#### 4.1.1 概念说明

`SSB-Filter-Design.ipynb` 解决的问题是：Weaver 法 SSB 解调需要一个以 0Hz 为中心、正负对称的 1300Hz 低通（见 u3-l2：先搬移使边带跨越直流，滤波后再搬回音频）。这个滤波器的指标直接决定邻道抑制能力，而它要在 STM32 的 I2S 中断里每 5ms 跑一遍，所以既要陡峭又要低阶。

notebook 开头（日文原文）交代了设计动机：沿用了先前 NanoSDR/FriskSDR 项目的滤波器，但 40dB 的阻止量不够，因此**再设计**（[python/SSB-Filter-Design.ipynb:L33-L45](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/SSB-Filter-Design.ipynb#L33-L45)）。规格写在下面几行：fs=48kHz、fc=1300Hz、6 阶、通带波纹 1dB、阻止量从 40dB 提到 80dB（最终定稿 60dB）。

#### 4.1.2 核心流程

整个 notebook 是一条五步流水线，每一步都有明确的「为什么」：

```text
① 试设计     ellip(6, 1, 40, 1300/24000)  → freqz 画频响 → 发现 40dB 不够
② 指标迭代   ellip(6, 1, 60, ...)          → 阻带加深 20dB，代价是过渡带更陡/系数更大
③ zpk 分解   ellip(..., output='zpk')      → 拿到零点 z、极点 p、总增益 k
④ 增益分配   共轭配对拆成 3 个 biquad，
              增益拆成 k^0.68 · k^0.22 · k^0.1（指数和 = 1，总增益不变）
⑤ 定点量化   rint(系数 × 16384)            → 手工重排 + a 取反 → 拷进 dsp.c
```

关键数学关系：

- **共轭配对保证实系数**：zpk 输出的零极点都是共轭成对的，取 \( (z - z_j)(z - z_j^{*}) \) 展开必得实系数二阶节。notebook 利用 scipy 返回顺序「每 3 个一组」的规律，用切片 `z[n::3]`、`p[m::3]` 一次取出一对。
- **增益分配防溢出**：总增益 \( k \approx 0.00108 \)（SSB 60dB 版）。若把全部增益塞进第一节，第一节输出会非常小（信噪比损失）；若全放最后一节，中间级的共振峰可能溢出 int16。分配为

  \[ k_1 k_2 k_3 = k^{0.68} \cdot k^{0.22} \cdot k^{0.1} = k^{1.0} = k \]

  指数和恰为 1，级联总增益不变；数值上 0.68/0.22/0.1 是「先大幅衰减、再逐级放大」的手工权衡（notebook 的说法是：让峰值先被压下去的那一节排在前面，避免中间溢出）。
- **×16384 而非 ×32768**：第二节反馈系数约 -1.998，Q15 的 int16 只能存 \((-1, 1)\)，装不下。整体乘 \( 2^{15}/2 = 16384 \) 后最大可表示 1.9999，CMSIS 实例的 `postShift = 1` 让输出再左移 1 位补回增益（u3-l2 已建立此结论，这里能看到它的来源）。

#### 4.1.3 源码精读

**① → ② 指标迭代**。两行只差一个参数：

- 第一次尝试，40dB 阻带：[python/SSB-Filter-Design.ipynb:L93](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/SSB-Filter-Design.ipynb#L93)

  ```python
  bb, aa = signal.ellip(6, 1, 40, 1300.0/24000, 'low')
  ```

- 定稿，60dB 阻带：[python/SSB-Filter-Design.ipynb:L157](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/SSB-Filter-Design.ipynb#L157)

  ```python
  bb, aa = signal.ellip(6, 1, 60, 1300.0/24000, 'low')
  ```

  两处都用 `signal.freqz` 画频响、`pl.xlim(0, 3000)` 放大通带附近检查，符合「先看图再谈系数」的设计纪律。

**③ zpk 分解**：[python/SSB-Filter-Design.ipynb:L229](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/SSB-Filter-Design.ipynb#L229)

```python
z, p, k = signal.ellip(6, 1, 60, 1300.0/24000, 'low', output='zpk')
```

注意与 ② 是**同一次设计**、只是换了输出格式——这保证了「分解前的多项式」从未被真正展开过。

**④ 共轭配对 + 增益分配**：[python/SSB-Filter-Design.ipynb:L297-L306](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/SSB-Filter-Design.ipynb#L297-L306)

```python
def plot_biquad(n, m, kk=k):
    bb, aa = signal.zpk2tf(z[n::3], p[m::3], kk)   # 取第 n 组共轭零点、第 m 组共轭极点
    ...
b0,a0 = plot_biquad(0, 0, k**0.68)                 # 增益大头给第一节
b1,a1 = plot_biquad(1, 1, k**0.22)
b2,a2 = plot_biquad(2, 2, k**0.1)
```

`z[n::3]` 是切片取共轭对的技巧；每节单独 `freqz` 叠画在一张图上，人工确认「峰值先被衰减」的排序。

**⑤ 定点量化**：[python/SSB-Filter-Design.ipynb:L383-L384](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/SSB-Filter-Design.ipynb#L383-L384)

```python
print np.rint(b0*16384),np.rint(a0*16384)
```

notebook 保存的输出（60dB 版）：

```text
[ 157. -238.  157.] [ 16384. -31237.  14936.]
[ 3643. -6974.  3643.] [ 16384. -31656.  15580.]
[ 8272. -16096.  8272.] [ 16384. -32074.  16158.]
```

**落点对照**——当前固件启用的正是这组数（`#else` 分支）：[dsp.c:L299-L306](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L299-L306)

```c
// 6th order elliptic lowpass filter fc=1300Hz, 60dB
q15_t bq_coeffs[] = {
		  157, 0,   -238,   157, 31237, -14936,
		 3643, 0,  -6974,  3643, 31656, -15580,
		 8272, 0, -16096,  8272, 32074, -16158
};
```

逐字核对三处变换：① 每行补插一个 `0` 占位（CMSIS 的 `{b0, 0, b1, b2, a1, a2}` 五系数布局）；② `-31237 → 31237`，`a` 系数全部**取反**；③ 分母首系数 `16384`（即 1.0）不存。而 `#if 0` 的旧版 [dsp.c:L292-L298](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L292-L298)（`515, 0, -906, 515, 30977, -14714, ...`）正是 notebook 结果页里贴的 **40dB 版本**（[python/SSB-Filter-Design.ipynb:L392-L401](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/SSB-Filter-Design.ipynb#L392-L401)）——一次指标迭代在固件里留下的化石层。

系数的消费者在 [dsp.c:L308-L309](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L308-L309)：

```c
arm_biquad_casd_df1_inst_q15 bq_i = { 3, bq_i_state, bq_coeffs, 1};
```

结构体第 4 个字段 `1` 就是 `postShift`，与 ×16384 的选择互为因果。

#### 4.1.4 代码实践

**实践目标**：在 PC 上（无需任何硬件）完整复现这条设计链，验证你手里跑的 scipy 能重新生产出固件里的那 18 个整数。

**操作步骤**（Python 3 + numpy/scipy 即可，以下为示例代码）：

```python
# repro_ssb.py —— 复现 SSB 1300Hz/60dB 椭圆低通的 q15 系数（示例代码）
import numpy as np
from scipy import signal

z, p, k = signal.ellip(6, 1, 60, 1300.0/24000, 'low', output='zpk')
for n, e in enumerate((0.68, 0.22, 0.1)):
    b, a = signal.zpk2tf(z[n::3], p[n::3], k**e)
    q15 = np.rint(np.r_[b[:1], 0, b[1:], -a[1:]] * 16384).astype(int)
    print(q15)
```

**需要观察的现象**：输出应为三行、每行 6 个整数；第一行 `157 0 -238 157 31237 -14936`，与 `dsp.c` L302 逐字一致。

**预期结果**：三行分别等于 [dsp.c:L301-L305](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L301-L305) 的三行。注意 `np.r_[b[:1], 0, b[1:], -a[1:]]` 这一行同时完成了「插 0 占位」与「a 取反」两件事。若你的 scipy 版本较新导致个别数值差 ±1（LSB），属正常——四舍五入的边界情况会随底层实现微变；写固件时以重新量化的整机频响为准即可。

#### 4.1.5 小练习与答案

**练习 1**：把阻带衰减从 60dB 改回 40dB 重跑，会得到什么？它对应固件里的哪段代码？

**答案**：会得到 `515, 0, -906, 515, 30977, -14714 / 5171, 0, -10087, 5171, 31760, -15739 / 16384, 0, -32182, 16384, 32165, -16253`——正是 `dsp.c` 中 `#if 0` 注释掉的旧版（L294-L298），也与 notebook 结果页（L392-L401）记录的文字一致。这条练习证明了「notebook 是固件系数的唯一权威来源」：两次设计、两套系数，在两个 artifact 里都能对上。

**练习 2**：为什么增益分配写成 `k**0.68, k**0.22, k**0.1`，而不是三节各 `k**(1/3)`？

**答案**：因为 \( k^{0.68} k^{0.22} k^{0.1} = k^{0.68+0.22+0.1} = k^1 \)，指数和必须为 1 才能保证级联总增益等于原设计 \( k \)；具体分配比例则是人工权衡——让承担最大衰减的第一节获得最大份额的「先衰减」，使各节中间信号幅度都远离 int16 上下限。等分 \( k^{1/3} \) 同样满足指数和为 1，只是溢出裕度不同；0.68/0.22/0.1 是作者看着各节频响峰值选定的一组。

**练习 3**：`bq_coeffs` 第二行反馈系数 `31656/16384 ≈ 1.932`。如果当初直接乘 32768（Q15）会发生什么？

**答案**：\( 1.932 \times 32768 = 63312 \)，超出 int16 的表示范围 \([-32768, 32767]\)，静态初始化就会回绕成错误值，滤波器直接发散。乘 16384 后最大系数 1.9997 恰好可表示（32735 < 32767），代价是所有系数精度降 1 位（量化噪声 +6dB），再用 `postShift = 1` 在输出端左移补回整体增益。

### 4.2 模块二：CW notebook 与 `bq()` 代码生成器

#### 4.2.1 概念说明

`CW-Filter-Design.ipynb` 与 SSB 版共享同一套五步法，只有两处实质差异，而这两处恰好都值得学：

1. **指标不同**：CW 是窄带模式，300Hz 带宽 → 以 0Hz 为中心 ±150Hz 低通（[python/CW-Filter-Design.ipynb:L142](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/CW-Filter-Design.ipynb#L142) 用 `signal.ellip(6, 1, 60, 150.0/24000, 'low', worN=4096)` 一路迭代到 60dB 定稿）。截止从 1300Hz 收窄到 150Hz 意味着极点更靠近单位圆、系数更接近极限值，定点化的难度更高。
2. **流程终点自动化**：SSB 版最后一步是「人肉重排 + 人肉取反 + 手抄进 C 源码」（靠 markdown 里的文字提醒注意事项）；CW 版则写了一个 `bq()` 函数把这一步也程序化了。这是设计流程本身的进化——消除手工转录这个最大的人为出错点。

#### 4.2.2 核心流程

```text
ellip(6,1,60,150/24000)  →  zpk  →  三节增益分配(同 0.68/0.22/0.1)
      →  bq(b,a):  rint(x*16384) → 重排 {b0,0,b1,b2,-a1,-a2} → 打印 C 数组文本
      →  拷贝进 dsp.c 的 bq_coeffs_150hz[]
```

#### 4.2.3 源码精读

**代码生成器**：[python/CW-Filter-Design.ipynb:L407-L417](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/CW-Filter-Design.ipynb#L407-L417)

```python
def bq(b, a):
    b, a = np.rint(b*16384), np.rint(a*16384)
    print "\t" + "".join(["%d, "%v for v in [b[0], 0, b[1], b[2], -a[1], -a[2]]])
print "q15_t bq_coeffs[] = {"
bq(b0, a0); bq(b1, a1); bq(b2, a2)
print "};"
```

短短四行完成了 SSB 版靠人肉做的全部转换：量化（`rint*16384`）、CMSIS 五系数布局（列表里的 `0` 占位）、符号取反（`-a[1], -a[2]`），并直接输出可粘贴的 C 代码。notebook 保存的运行结果：

```text
q15_t bq_coeffs[] = {
	149, 0, -296, 149, 32593, -16210, 
	3579, 0, -7154, 3579, 32669, -16289, 
	8206, 0, -16406, 8206, 32735, -16358, 
};
```

**落点对照**：[dsp.c:L322-L327](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L322-L327)

```c
// 6th order elliptic lowpass filter fc=150Hz, 60dB
q15_t bq_coeffs_150hz[] = {
	149, 0, -296, 149, 32593, -16210,
	3579, 0, -7154, 3579, 32669, -16289,
	8206, 0, -16406, 8206, 32735, -16358
};
```

逐字一致（仅去掉行尾逗号）。注意第三行反馈系数 `32735/16384 = 1.99994`——离 int16 上限 32767 只剩 32 个量化级，直观展示了「150Hz 窄带把系数逼到表示极限」这句话的含义。

该数组的实例化与使用在 [dsp.c:L329-L330](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L329-L330)（`bq_cw_i/bq_cw_q`）与 [dsp.c:L399-L407](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L399-L407)：`cw_demod()` 在栈上组装配置时把 `&bq_cw_i/&bq_cw_q` 填进 `weaver_demod_conf_t`，与 SSB 共用同一个 `demod_weaver()`。也就是说，**一套解调框架 + 两份 notebook 产物 = 三个模式（LSB/USB/CW）**，这正是 u5-l4「新增解调模式只需新滤波器系数表 + 三字段配置」结论的算法侧注脚。

顺带验证 notebook 浮点输出的换算（第二节）：`b0 = 0.00907821`，\( 0.00907821 \times 16384 = 148.76 \to 149 \)；`a1 = -1.98932652`，\( -1.98932652 \times 16384 = -32592.98 \to -32593 \)，取反得 `32593`。手算与 `bq()` 输出、固件数组三方一致。

#### 4.2.4 代码实践

**实践目标**：体验「改一个指标，自动再生成一版系数」的完整闭环，为 u5-l4 的 `cwn` 窄带变体做好算法准备。

**操作步骤**（示例代码，PC 上运行）：

1. 复制 4.1.4 的脚本，把截止频率从 `150.0/24000` 改为 `300.0/24000`（600Hz 带宽变体）；
2. 把打印部分换成 `bq()` 的 Python 3 等价形式：

```python
def bq(b, a):
    b, a = np.rint(b*16384), np.rint(a*16384)
    print("\t" + "".join("%d, " % v for v in [b[0], 0, b[1], b[2], -a[1], -a[2]]))
```

3. 用 `signal.freqz` 画出三节级联与总频响，确认通带 ±300Hz、阻带 60dB。

**需要观察的现象**：截止翻倍后，第三行反馈系数应明显小于 `32735`（极点离单位圆更远），整体离 int16 极限更宽松。

**预期结果**：得到一组可直接粘贴的 `q15_t` 数组。**待本地验证**：具体数值依赖你机器上的 scipy 版本，本文不预先填写；验证标准是级联频响在 ±300Hz 内波纹 ≤1dB、阻带 ≤−60dB。若想进一步落地，按 u5-l4 的五插口流程把它注册成新模式即可。

#### 4.2.5 小练习与答案

**练习 1**：`bq()` 里为什么是 `[b[0], 0, b[1], b[2], -a[1], -a[2]]`，而不是 `[b[0], b[1], b[2], a[1], a[2]]`？

**答案**：CMSIS `arm_biquad_cascade_df1_q15` 要求每个节的 5 个系数按 `{b0, b1, b2, a1, a2}` 排列，实现里固定按 5 取模访问，因此数组里**必须**为不存在的 b3 位置补一个 0 占位（`b[0], 0, b[1], b[2]`）；同时 CMSIS 的差分方程对反馈项做**加法**（\( y[n] = b_0 x[n] + b_1 x[n-1] + b_2 x[n-2] + a_1 y[n-1] + a_2 y[n-2] \)），与 scipy 的减法约定相反，所以 `a1/a2` 要取反。漏掉任何一个都会得到一个「看起来也在滤波、但频响完全不对」的滤波器。

**练习 2**：CW notebook 的 `freqz` 调用比 SSB 版多了 `worN=4096`，为什么？

**答案**：CW 通带只有 ±150Hz，相对 24kHz 的归一化频率轴只占 0.0125。`freqz` 默认在单位圆上均匀取 512 点，落在通带内的点太少，画出的频响曲线会失真甚至错过波纹细节；`worN=4096` 把频率网格加密 8 倍才能看清 0~300Hz 的过渡带形状。这是窄带滤波器设计的常规检查习惯。

### 4.3 模块三：TLV320AIC3204 一阶 HPF——24 位定点与另一条搬运路径

#### 4.3.1 概念说明

前两个模块的系数落在 STM32 的 q15 世界；第三个 notebook 服务于另一个处理器——TLV320AIC3204 编解码器**片内 mini-DSP**（u2-l2 讲过它兼任 DC 抑制、IQ 幅度平衡与频谱倒置补偿）。它的系数格式是**符号 24 位定点**（1 位符号 + 23 位小数），且按**3 字节大端**写进 I2C 寄存器。设计目标是一个 fc≈2.4Hz 的一阶高通，用来在 ADC 一侧压掉直流分量（[python/TLV320AIC3204-1st-IIR-HPF.ipynb:L56-L76](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/TLV320AIC3204-1st-IIR-HPF.ipynb#L56-L76)：`fc = 0.0001×(fs/2)`，fs=48kHz 时即 2.4Hz）。

一阶高通的物理直觉：极点放在 \( p \approx 0.9997 \) 处，直流（z=1）增益为零，稍高频率增益趋近 1，转折频率 \( f_c \approx \frac{f_s}{2\pi}(1-p) \)。

#### 4.3.2 核心流程

```text
iirfilter(1, [0.0001], 'highpass')     →  浮点 b=[0.99984, -0.99984], a=[1, -0.99969]
rint(系数 × 2**23)                      →  24 位定点整数
to_bytes(n, 3) → 3 字节大端 → to_hex    →  '0x7f, 0xfa, 0xda' 这样的字节文本
a1 单独取反（to_hex(-al[1])）            →  codec 约定与 CMSIS 同为加反馈
拷进 tlv320aic3204.c 的字节表 / config 函数
```

\[ c_{24} = \mathrm{rint}(c \times 2^{23}), \qquad \text{字节序：} (c_{24} \gg 16)\ \&\ 0xff,\ (c_{24} \gg 8)\ \&\ 0xff,\ c_{24}\ \&\ 0xff \]

#### 4.3.3 源码精读

**设计**：[python/TLV320AIC3204-1st-IIR-HPF.ipynb:L76-L77](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/TLV320AIC3204-1st-IIR-HPF.ipynb#L76-L77)

```python
b, a = signal.iirfilter(1, [0.0001], btype='highpass')
```

保存的输出：`b = [0.99984295, -0.99984295]`，`a = [1, -0.99968589]`。

**量化与字节化**：[python/TLV320AIC3204-1st-IIR-HPF.ipynb:L155-L201](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/TLV320AIC3204-1st-IIR-HPF.ipynb#L155-L201)——`np.rint(a * 2**23)`、`np.vectorize(int)` 取整、`to_bytes/to_hex` 拆 3 字节大端。notebook 输出：

```text
b → ['0x7f, 0xfa, 0xda', '0x80, 0x5, 0x26']
-a[1] → '0x7f, 0xf5, 0xb5'
```

（`to_hex(-al[1])` 在 [L221-L222](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/TLV320AIC3204-1st-IIR-HPF.ipynb#L221-L222)，单独处理正是为了 a1 取反。）

**落点一（静态字节表）**：[tlv320aic3204.c:L313-L330](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/tlv320aic3204.c#L313-L330)

```c
// implement HPF of first order IIR
const uint8_t adc_iir_filter_dcreject2[] = {
  12, 8, 24,                    /* len, page, reg */
  0x7f, 0xfa, 0xda, 0x00,       /* C4  = b0 */
  0x80, 0x05, 0x26, 0x00,       /* C5  = b1 */
  0x7f, 0xf5, 0xb5, 0x00,       /* C6  = a1 (已取反) */
  ...
```

三个 24 位数与 notebook 输出逐字节一致（尾字节 `0x00` 是 codec 寄存器以 4 字节为一组的填充）。这张表沿用了 u2-l2 讲过的「哨兵结尾 (len, page, reg, data...) 解释器」模式，由 [tlv320aic3204.c:L350-L367](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/tlv320aic3204.c#L350-L367) 的 `tlv320aic3204_config_adc_filter()` 在使能 DC 抑制时整组下发，并在帧边界切换系数缓冲。

**落点二（带幅度补偿的运行时版本）**：[tlv320aic3204.c:L369-L411](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/tlv320aic3204.c#L369-L411)

```c
void tlv320aic3204_config_adc_filter2(double adj)
{
  int32_t b0 = 0x7ffada00;
  int32_t b1 = 0x80052600;
  int32_t a1 = 0x7ff5b500;
  ...
  b0 = (int32_t)(b0 * adj);   /* 右声道乘幅度修正 */
```

这里把同一组系数写成了 32 位形式（`0x7ffada00` = `0x7ffada << 8`，写寄存器时 `>>24` 取高 3 字节，数值等价于字节表），**新增的自由度是 `adj`**：右声道 b0/b1 乘一个修正系数，实现 IQ 幅度平衡（u2-l2 讲过的 `adj=-1` 常开还顺带完成频谱倒置补偿）。对照两个落点可以看到同一 notebook 产物的两种工程用法：静态表走「页/寄存器解释器」，需要参数化的版本直接写函数。

#### 4.3.4 代码实践

**实践目标**：不依赖硬件，手工走一遍「浮点 → 24 位定点 → 大端字节」的换算，确认你能读懂数据手册式的系数编码。

**操作步骤**：

1. 用 Python 算 `0.99984295 * 2**23`，与 notebook 输出 `8387291`（近似，取整边界见下）核对；
2. 用 Python 把 `8387290`（= `0x7FFADA`）拆成 3 字节：`[(n>>16)&0xff, (n>>8)&0xff, n&0xff]`；
3. 对照 [tlv320aic3204.c:L319-L321](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/tlv320aic3204.c#L319-L321) 的三个字节 `0x7f, 0xfa, 0xda`。

**需要观察的现象**：`0x7f = 0111_1111`，最高位为 0——正数；而 b1 的首字节 `0x80` 最高位为 1，按 24 位补码解读为负数。这正是「符号 24 位定点」的直观体现。

**预期结果**：步骤 2 输出 `[127, 250, 218]` 即 `[0x7f, 0xfa, 0xda]`。`np.rint`（四舍五入）与 `np.vectorize(int)`（截断）在 .9 边界可能差 1 LSB，notebook 里两者先后使用，最终字节以 `to_hex` 输出为准——这也是「量化路径要单一、可复现」的一个小教训。

#### 4.3.5 小练习与答案

**练习 1**：同样是「×2^n 再取整」，为什么 dsp.c 用 \( 2^{14} \) 而 codec 用 \( 2^{23} \)？

**答案**：定点格式跟随**目标硬件的字宽**。STM32 侧滤波器跑在 int16（q15）寄存器上，考虑反馈系数可达 2.0 故再缩一位成 Q14；codec mini-DSP 内部是 24 位数据通路，系数用满 23 个小数位。位宽越大，量化噪声越低——24 位系数的量化底噪约 -140dB，远低于音频链路的模拟噪声，而 Q14 系数的 -90dB 量化底噪才是整机阻带能力的实际天花板之一。

**练习 2**：`adc_iir_filter_dcreject2` 表里 a1 的字节是 `0x7f, 0xf5, 0xb5`（正数），而 scipy 算出的 `a[1] ≈ -0.99969` 是负数。谁对？

**答案**：都对，差异就是符号取反。scipy 约定 \( y[n] = b_0 x[n] + b_1 x[n-1] - a_1 y[n-1] \)（减反馈），codec mini-DSP 与 CMSIS 一样做**加**反馈，所以写入硬件前取反：\( -(-0.99969) \approx +0.99965 \)，即 `0x7FF5B5`。notebook 专门写了 `to_hex(-al[1])` 而不是 `to_hex(al[1])`，就是把这个约定固化在生成器里。

### 4.4 模块四：centsdr.py——把整机变成一台可编程仪器

#### 4.4.1 概念说明

设计落地之后，还剩最后一问：**怎么知道它真的工作？** 手持机没有 JTAG 示波器，但 u1-l4 讲过的 USB CDC shell 暴露了 `data`/`power`/`show` 等命令，等于把内部总线接到了 PC 上。`centsdr.py` 就是建立在这层之上的驱动库：既是一条 CLI（`./centsdr.py -F 27500000 -M fm`），也是一个可 `import` 的模块。**模块身份是本讲的重点**——所有自动化测量（扫频、长时间记录、统计）都从 `from centsdr import CentSDR` 开始。

#### 4.4.2 核心流程

`centsdr.py` 的类结构是一个典型的「薄命令层 + 一个协议状态机」：

```text
CentSDR(dev)
   │ open() ── pyserial 打开 USB-CDC 串口
   │ send_command(cmd) ── 写一行命令，读掉回显的空行
   │ fetch_data() ── 逐字符读到 "ch>" 提示符为止（一条命令输出的结束边界）
   │
   ├── 模板方法群：set_tune/set_mode/set_fs/set_gain/set_agc/...（一行 shell 命令的封装）
   ├── fetch_array(sel) ── "data sel" + hex16 解码 → numpy int16 数组（波形抓取）
   ├── read_power() ── "power" + 正则 → 浮点 dBm
   └── read_status(arg) ── "show arg" → 原始文本
```

关键机制只有两个：**`ch>` 提示符作为帧边界**、**`%04x` ↔ `'>h'` 作为数据编码对**（u1-l4 已详述，此处是它的实现现场）。

#### 4.4.3 源码精读

**连接管理与上下文管理器**：[python/centsdr.py:L10-L29](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/centsdr.py#L10-L29)。设备名的解析顺序是「构造参数 → 环境变量 `CENTSDR_DEVICE` → 硬编码默认值」（[python/README.md:L28-L34](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/README.md#L28-L34) 给出了用法）。实现了 `__enter__/__exit__`，所以推荐 `with` 用法：

```python
with CentSDR() as sdr:
    ...
```

**命令发送**：[python/centsdr.py:L31-L34](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/centsdr.py#L31-L34)

```python
def send_command(self, cmd):
    self.open()
    self.serial.write(cmd)
    self.serial.readline() # discard empty line
```

**模板方法群**：[python/centsdr.py:L36-L58](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/centsdr.py#L36-L58)，每个都是 `send_command` 的一行封装（`set_tune` 在 L39-L40）。要支持新命令（比如 u5-l4 里你自己加的 `cwn` 模式名走 `set_mode` 即可），照抄一个方法即可——u1-l4 说过的「模板方法」扩展模式。

**协议读回**：[python/centsdr.py:L60-L75](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/centsdr.py#L60-L75) 的 `fetch_data()` 逐字符累积，直到行尾出现 `ch>`（shell 提示符）才认为这条命令的输出结束。这是没有长度前导、没有校验和的「提示符定界」协议——简单可靠，但要求 shell 提示符永远出现在输出末尾。

**波形抓取**：[python/centsdr.py:L77-L86](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/centsdr.py#L77-L86)

```python
def fetch_array(self, sel):
    def hex16(h):
        return struct.unpack('>h', h.decode('hex'))[0]
    self.send_command("data %d\r" % sel)
    data = self.fetch_data()
    ...
```

它对应的固件侧是 [main.c:L315-L349](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L315-L349) 的 `cmd_data()`：`sel` 0/1/2/3 分别选 `rx_buffer`（原始交织 IQ）、`tx_buffer`（输出音频）、`buffer[0]`（混频后）、`buffer2[0]`（滤波后），以 `%04x` 每行 16 个转储（[main.c:L342-L347](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L342-L347)）；`'>h'` 把十六进制文本还原成**大端有符号 16 位**。缓冲编号的官方说明在 [python/README.md:L120-L126](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/README.md#L120-L126)。

**功率读数**：[python/centsdr.py:L98-L102](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/centsdr.py#L98-L102) 用正则 `power: ([\d.-]+)dBm` 从 [main.c:L461-L467](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L461-L467) 的 `chprintf(chp, "power: %d.%01ddBm\r\n", ...)` 里抠出数值。

> **一个必须先想清楚的限制**：`power` 读数来自 `measure_power_dbm()`（[main.c:L380-L393](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L380-L393)），它统计的是 `stat.rms[0]`——由 [main.c:L351-L376](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L351-L376) 的 `calc_stat()` 对 **`rx_buffer`（解调之前的原始 IQ）** 计算而来。也就是说，**`power` 根本不经过你的 CW 滤波器**，扫 `power` 得到的是前端（codec 增益链 + ADC）的频响，而不是解调选择性。要测滤波器形状，必须抓 `data 3`（滤波后的 `buffer2[0]`）在 PC 端自己算 RMS——这个坑是综合实践的核心考点。

**模块身份与 CLI 身份的分界**：[python/centsdr.py:L112-L186](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/centsdr.py#L112-L186) 的 `run_as_command()` 用 optparse 把同一批方法拼成命令行；文件末尾 [L188-L189](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/centsdr.py#L188-L189) 才是 `if __name__ == '__main__'` 入口。README 推荐的模块用法（[python/README.md:L145-L155](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/python/README.md#L145-L155)）：

```python
from centsdr import CentSDR
sdr = CentSDR()
sdr.set_tune(27500000)
sdr.set_mode('fm')
```

#### 4.4.4 代码实践

**实践目标**：让 `centsdr.py` 在现代 Python 3 环境可用（或确认你需回退 python2.7），并验证「提示符定界」协议的鲁棒性。

**操作步骤**：

1. 直接 `python3 centsdr.py -h`，观察报错点；
2. 施加以下移植补丁（示例代码，共 3 处）：

   | 位置 | Python 2 | Python 3 |
   |------|----------|----------|
   | L79 | `h.decode('hex')` | `bytes.fromhex(h)`（若 `h` 已是 str 先 `.encode()`） |
   | L64/L67 等 | `c = self.serial.read()` 返回 `bytes`，与 `chr(13)` 比较永假 | 改成 `c == 13` 或 `c == b'\r'`，拼行用 `line.decode()` |
   | L154/L184 | `print sdr.read_status(...)` | `print(sdr.read_status(...))` |

3. 无硬件时的替代验证：用 `socat -d -d pty,raw,echo=0 pty,raw,echo=0` 建一对虚拟串口，一端跑一个模仿 `ch>` 提示符与 `power:` 输出格式的小脚本，另一端 `CENTSDR_DEVICE=/dev/pts/N python3 centsdr.py -P` 验证你的 `read_power` 解析正确。

**需要观察的现象**：打补丁前 `fetch_data` 在 Python 3 下会**永远收不到结束条件**（`bytes` 与 `str` 比较不相等，`line.endswith('ch>')` 对 bytes/str 混拼也可能抛异常），表现为命令挂死。

**预期结果**：补丁后 `-h`、`-s`、`-P` 等子命令行为与 README 描述一致；虚拟串口回环里 `read_power()` 返回值与模仿脚本打印的数字一致。硬件在手的读者可直接 `./centsdr.py -p 0` 看 IQ 波形（README L128-L139）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `fetch_data` 用「逐字符读 + 检测提示符」而不是「读固定字节数」或「readline」？

**答案**：因为 shell 输出的行数事先未知（`data` 命令一次输出 30 行十六进制，`show` 输出 7 行），且命令回显、空行、提示符混杂——`readline` 无法知道何时该停。提示符 `ch>` 是唯一可靠的帧结束标志。代价是效率低（每字符一次系统调用），对 2.9KB 的 `data` 传输尚可接受；若要提速，可换成 `read_until(b'ch>')` 类 API（pyserial 自带）——这也是你打 Python 3 补丁时值得顺手做的优化。

**练习 2**：`fetch_array(0)` 返回 480 个 int16，CLI 代码里为什么要 `samp[0::2] + samp[1::2]*1j` 重组，而抓 buffer 3 时不需要？

**答案**：`data 0` 转储的 `rx_buffer` 是**交织**存储的 IQ（I0 Q0 I1 Q1 ...，480 个 int16 = 240 个复样本），偶数位是 I、奇数位是 Q，所以要按步长 2 拆开拼成复数；`data 3` 转储的 `buffer2[0]` 是**平面**格式的单路实信号（解调中间缓冲的 I 路，480 个样本全是同一路），无需重组。存储格式的差异来自 u2-l3 讲过的「拆交织—处理—装交织」流水线。分不清这两种格式，是抓波分析中最常见的第一个错误。

**练习 3**：`read_power()` 的正则里 `[\d.-]` 为什么同时包含小数点和减号？

**答案**：因为固件输出是 `"power: %d.%01ddBm"`（[main.c:L465-L466](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L465-L466)），数值带一位小数（点），且信号弱时为负 dBm（减号）；漏掉任何一个字符类成员，`re.match` 返回 `None`，下一行 `m.group(1)` 抛 `AttributeError`。写协议解析时先看清对端的 `printf` 格式串，是最朴素的「契约对齐」。

## 5. 综合实践：扫频测出整机选择性曲线

本实践把四个模块串成闭环：用模块四的 `centsdr.py` 驱动整机，测出 CW 模式的实际通带，再与模块一/二 notebook 设计的 150Hz（±150Hz，整带 300Hz）指标对照，分析偏差。

### 5.1 实验设计（先想清楚测什么）

- **信号路径**：信号源（或天线）注入固定 7.000MHz 信号，CW 模式。扫频方式采用「**信号不动、整机 tune 扫**」：`set_tune(f)` 每步改变接收频率，等效于把信号在通带上一格格平移。`tune` 命令在固件侧经 [main.c:L83-L91](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L83-L91) → [main.c:L197-L199](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L197-L199)（`center = hz - mode_freq_offset`，CW 时偏移 10kHz）→ SI5351 四倍频下发，全程自动。
- **测量量**：如 4.4.3 所述，`power` 不经过滤波器，只能当「前端平坦度参考曲线」；**选择性曲线必须抓 `data 3`**（`buffer2[0]`，Weaver 滤波后的 I 路）在 PC 端算 RMS。
- **防污染**：扫频前 `set_agc('manual')` 固定增益——否则 [main.c:L384-L386](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L384-L386) 显示 AGC 增益会被计入 power 补偿，且 AGC 会把带内带外的增益拉来拉去，曲线彻底失真。
- **步进与范围**：通带仅 ±150Hz，100Hz 步进只有 3 个点落在带内，建议 25~50Hz；范围 ±600Hz 足以看到 -40dB 以下。每步之间 `sleep` 要足够长——150Hz 窄带 IIR 的建立时间常数为毫秒级，加上 SI5351 重配与 I2C 传输，每步 ≥100ms 稳妥。

### 5.2 参考脚本（示例代码，基于 Python 3 移植版 centsdr 模块）

```python
# sweep_selectivity.py —— CentSDR CW 模式选择性扫频（示例代码）
import time
import numpy as np
import matplotlib.pyplot as plt
from centsdr import CentSDR

CENTER, SPAN, STEP = 7_000_000, 600, 25     # 7MHz ± 600Hz，25Hz 步进
sdr = CentSDR()
sdr.open()
sdr.set_mode('cw')
sdr.set_agc('manual')
sdr.set_gain(40)                            # 固定前端增益

freqs, rms_db, pwr = [], [], []
for off in range(-SPAN, SPAN + 1, STEP):
    f = CENTER + off
    sdr.set_tune(f)
    time.sleep(0.1)                         # 等本振稳定 + IIR 建立到稳态
    x = sdr.fetch_array(3).astype(float)    # buffer2[0]：滤波后的 I 路（平面格式）
    rms_db.append(20 * np.log10(np.sqrt(np.mean(x**2)) + 1e-9))
    pwr.append(sdr.read_power())            # 参考曲线：前端频响
    freqs.append(off)

fig, ax = plt.subplots(2, 1, sharex=True)
ax[0].plot(freqs, rms_db, 'o-'); ax[0].set_ylabel('filtered RMS (dBFS)')
ax[1].plot(freqs, pwr, 's-');     ax[1].set_ylabel('rx power (dBm)')
ax[1].set_xlabel('offset from 7.000MHz (Hz)')
plt.show()
```

### 5.3 需要观察的现象与预期结果

1. **上图（data 3 的 RMS）**：以 0Hz 为中心的馒头形曲线，±150Hz 内基本平坦（波纹 ≤1dB 量级），向两侧快速跌落，600Hz 处应低 40dB 以上——这就是 `bq_coeffs_150hz` 的真实形状。**待本地验证**（需硬件与信号源）。
2. **下图（power）**：在 ±600Hz 范围内应当几乎平坦——因为它测的是滤波前的 `rx_buffer`。两条曲线的巨大反差，就是 4.4.3 那个限制的实验证明。
3. 若把脚本里 `fetch_array(3)` 换成 `fetch_array(2)`（滤波前 IF1），应得到与 power 曲线同样平坦的形状——三个缓冲三种性格，一次实验全部看清。

### 5.4 偏差来源清单（实测 vs notebook 设计）

对照你画的曲线与 `signal.ellip` 的理论频响，逐项核对：

| 来源 | 机理 | 量级估计 |
|------|------|----------|
| q15 系数量化 | 系数只保留 14 个小数位，量化底噪约 -90dB，会抬高实测阻带底部 | 阻带深处几十 dB 的差异 |
| NCO 相位步进量化 | `PHASESTEP` 为 16 位整数，10kHz 偏移的舍入误差 ±0.37Hz（48000/65536 ≈ 0.73Hz 分辨率） | 通带中心平移 <0.4Hz，对 300Hz 通带可忽略 |
| 本振频率误差 | SI5351 分数分频的剩余分辨率 + 调谐链路里 `hz-10000` 再四倍频的取整 | 数 Hz以内，表现为曲线整体微移 |
| 扫描速度 | 每步 100ms 若不够，窄带 IIR 尚未建立稳态就读数，阻带读数偏高 | 带边过渡变缓；加倍 sleep 后曲线变陡即为此因 |
| 信号源与本振相位噪声 | 把噪声能量「抹」进阻带 | 阻带底部地板 |
| 前端幅度频响 | codec 增益链在 7MHz 变换后并非严格平坦 | 已由 power 参考曲线单独暴露 |

分析时建议把理论曲线（PC 上重跑 notebook 的 `freqz`）与实测曲线画进同一张图、以通带中心对齐归一化，偏差便一目了然。

## 6. 本讲小结

- CentSDR 的算法工作流是完整的「**PC 设计 → 定点落地 → PC 验证**」闭环：三个 Jupyter notebook 是固件里三组滤波系数的唯一权威来源，`centsdr.py` 则把整机变成可编程的验证仪器。
- 滤波器设计的五步法环环相扣：**指标迭代**（40dB→60dB）→ **zpk 形式分解**（避免多项式求根的数值病态）→ **共轭配对成 biquad** → **增益分配**（指数和为 1，先衰减防溢出）→ **定点量化**（×16384 配 postShift=1，a 取反，0 占位）。CW notebook 的 `bq()` 把最后一步自动化成了代码生成器。
- 另一条换算路径同样重要：codec mini-DSP 的 24 位定点（×2²³、3 字节大端、a1 取反）说明「定点格式跟随目标硬件字宽」，同一套 scipy 流程可以对接任意位宽的 DSP 后端。
- `power` 命令测的是**解调之前**的 `rx_buffer`，不反映解调滤波器；测选择性必须抓 `data 3`（滤波后缓冲）自己算 RMS，并且扫频时要锁定 AGC——这两个坑比任何公式都更容易让测量失败。
- `centsdr.py` 的协议核心是「`ch>` 提示符定界 + `%04x`/`'>h'` 编解码对」，它是 Python 2 代码，现代环境需先打小补丁；其「模块兼 CLI」的双重身份使扩展成本极低。

## 7. 下一步学习建议

本讲是学习手册正课的最后一篇技术讲义。接下来建议：

1. **动手完成 u5-l4 的收尾**：如果你在 u5-l4 实现了 `cwn`（600Hz 窄带 CW 变体），用本讲的五步法设计它的系数、用本讲的扫频脚本实测它的通带——那将是一次完整的「规格→系数→固件→曲线」毕业设计。
2. **扩展验证仪器**：把 `sweep_selectivity.py` 推广成通用工具——扫 `data 0/2/3` 三个缓冲画出「前端/混频后/滤波后」三联图，或用 `fetch_array(1)` 抓输出音频做 THD 分析。仿照 `centsdr.py` 的模板方法风格，为你在 u1-l4 新增的 shell 命令补上 Python 封装。
3. **读上游**：CMSIS-DSP 的 `arm_biquad_cascade_df1_q15.c`（本仓库 `CMSIS/DSP_Lib/Source/FilteringFunctions/` 下）与 scipy 的 `ellip` 文档对照着读，把「notebook 与固件各自背后的实现」也纳入视野；有兴趣可以研究为什么 CMSIS 内部用 64 位累加（u3-l2 的伏笔）。
4. **回到整体**：以 README 的框图为纲，从 `main()` 走一遍 `i2s_end_callback → signal_process → disp_fetch_samples → disp_process` 的完整数据旅程，检验五个单元的知识是否已经连成一张网。
