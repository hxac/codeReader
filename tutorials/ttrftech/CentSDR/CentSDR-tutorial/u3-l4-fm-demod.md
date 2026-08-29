# 调频解调：反正切鉴频器

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清楚「相邻样本共轭相乘、再取辐角」为什么等价于求瞬时频率——这是 `fm_demod()` 的全部数学。
2. 读懂 `atan_2iq()` 的完整技巧链：SIMD 求点积/叉积 → 象限折叠到 \([0, \pi/4]\) → 16.16 定点除法 → 查表 + 线性内插 → 展开回原象限。
3. 解释 `arctantbl` 这张只有 258 项的表为什么能覆盖整个复平面，并能用 Python 亲手重新生成它。
4. 说明 `fm_demod_state.last` 如何在两个 1.25ms 的音频块之间「接力」，保住相位差分的连续性。
5. 在 PC 上（不需要硬件）把 `atan_2iq` 提取出来跑通，用一个 5kHz→15kHz 的扫频信号验证鉴频器的线性，并统计它与标准 `atan2` 的最大角度误差。

## 2. 前置知识

### 2.1 调频（FM）：信息藏在「频率」里

AM 电台用幅度携带声音，FM 电台用**频率偏离**携带声音。发射端让载波频率在中心附近摆动：

\[ f(t) = f_c + \Delta f \cdot m(t) \]

其中 \(m(t)\) 是归一化的音频，\(\Delta f\) 是最大频偏（广播 FM 为 ±75kHz）。接收机的任务就是**测出每个时刻的瞬时频率**，把摆动还原成声音——这个部件叫**鉴频器**（discriminator）。

### 2.2 瞬时频率 = 相位的差分

u3-l1 讲过，IQ 两路采样合起来是一个复数序列 \(x[n] = I[n] + jQ[n]\)，极坐标下写作 \(x[n] = A[n]\,e^{j\phi[n]}\)。对理想 FM 信号，幅度 \(A\) 恒定，全部信息在相位 \(\phi[n]\) 里，而瞬时频率正是相位的**每样本增量**：

\[ f[n] = \frac{\phi[n] - \phi[n-1]}{2\pi} \cdot f_s \]

所以「测频率」可以转化为「测相邻两个样本的相位差」。

### 2.3 atan2：只看比值、不看长度的辐角

`atan2(y, x)` 返回点 \((x, y)\) 相对 x 轴正方向的辐角，取值 \((-\pi, \pi]\)。它有个关键性质：把 x、y 同时乘以任意正数，辐角不变。这正是鉴频器「天生限幅」的原因——信号强弱只改变 \((x,y)\) 的长度，不改变方向。

### 2.4 本讲要用的定点与 SIMD 知识（承接 u3-l1）

- **q15 定点**：int16 表示 \([-1, 1)\)，两个 q15 相乘得 q30（仍存于 int32）。
- **打包字**：固件把交织的 IQ 流按 32 位读取，**低 16 位是 I、高 16 位是 Q**（u2-l3、u3-l1 已从 `__SMLAD`/`__SMLSDX` 的用法反推确认）。
- **SIMD 内建函数**（本讲用到三个）：
  - `__SMUAD(a, b)` = `a.lo*b.lo + a.hi*b.hi`（双 16 位乘加，无累加链）
  - `__SMUSDX(a, b)`：先交换第二操作数 b 的高低半字，再做 lo×lo − hi×hi，等效 `a.lo*b.hi − a.hi*b.lo`
  - `__SSAT(x, 16)`：饱和到 int16 范围

## 3. 本讲源码地图

| 文件 | 本讲涉及的内容 | 作用 |
|---|---|---|
| `dsp.c` | `fm_demod_state`（L487）、`arctantbl`（L493）、`atan_2iq`（L528）、`fm_demod`（L568）、`fm_demod0`/`fm_adj_filter`（L751、L791，预告 u3-l5） | FM 解调的全部实现 |
| `nanosdr.h` | `AUDIO_BUFFER_LEN`（L93）、`signal_process_func_t`（L112）、`fm_demod` 声明（L120）、`FS`/`PHASESTEP`（L125-L128） | 缓冲长度约定、解调函数指针类型 |
| `main.c` | `mod_table`（L165-L177）、`set_modulation`（L179）、`set_tune`（L197）、`i2s_end_callback`（L258-L276） | FM 模式的接线与实时调用点 |

一句话数据流：I2S 中断把交织 IQ 交到 `fm_demod()`，它对**相邻两个打包样本**调用 `atan_2iq()` 求相位差，把差值复制到左右声道，交给 DAC 放音。

## 4. 核心概念与源码讲解

### 4.1 相位差分鉴频：共轭相乘的数学

#### 4.1.1 概念说明

设前后两个复数样本 \(x_0 = A_0 e^{j\phi_0}\)、\(x_1 = A_1 e^{j\phi_1}\)。把它们**共轭相乘**：

\[ \overline{x_0} \cdot x_1 = A_0 A_1 \, e^{j(\phi_1 - \phi_0)} \]

展开成实部/虚部：

\[ \mathrm{Re} = I_0 I_1 + Q_0 Q_1 = A_0 A_1 \cos\Delta\phi, \qquad \mathrm{Im} = I_0 Q_1 - I_1 Q_0 = A_0 A_1 \sin\Delta\phi \]

其中 \(\Delta\phi = \phi_1 - \phi_0\) 就是我们要求的相位差。再取辐角：

\[ \operatorname{atan2}(\mathrm{Im},\, \mathrm{Re}) = \Delta\phi, \qquad f = \frac{\Delta\phi}{2\pi} f_s \]

幅度 \(A_0 A_1\) 作为公共因子被 `atan2` 自动约掉——**信号忽强忽弱时鉴频输出纹丝不动**，这就是 FM 广播抗幅度干扰的根源，也解释了为什么 FM 模式对增益不像 AM 那样敏感。

这个方案还有两个工程优点：

1. **只算一次反正切**。「对每个样本各做一次 atan2 再相减」要两次反正切，而共轭相乘把减法搬进了三角函数内部。
2. **天然无 ±π 回绕问题**。两个各自接近 ±π 的辐角相减会跳变 2π；而在乘积域里，\(\Delta\phi\) 本身就被表示成 \((-\pi, \pi]\) 内的一个点。

#### 4.1.2 核心流程

```text
对每个新样本 x1（上一样本为 x0）：
  re = I0*I1 + Q0*Q1          ← cos 分量 × A0*A1
  im = I0*Q1 - I1*Q0          ← sin 分量 × A0*A1（符号约定见 4.3.3 的讨论）
  v  = atan2(im, re)          ← 相位差 Δφ
  输出 v 到左右声道
  x0 = x1                     ← 接力，准备下一对
```

输出尺度换算（本固件，fs = 192kHz，推导见 4.3.3 末尾）：

\[ v = \Delta\phi \cdot 1024 = \frac{2\pi f}{f_s}\cdot 1024 \;\approx\; \frac{f}{29.8} \]

即 ±1kHz 频偏对应输出约 ±33（int16 满量程的 0.1%），±75kHz 广播满频偏对应约 ±2513——永不削顶。推导见 4.3.3 末尾的缩放链。

#### 4.1.3 源码精读：FM 模式如何被接线

先看 FM 在模式表里的注册。AM/CW 都有 10kHz 的频率偏移，唯独 FM 的 `freq_offset` 是 **0**：

- [main.c:165-L177](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L165-L177) —— `mod_table` 把解调函数、频率偏移、采样率、名字打包成一行；FM 行是 `{ fm_demod, 0, 192, "fm" }`：**载波直接放在 IQ 的 0Hz**，采样率切到 192kHz（广播 FM 占用约 200kHz 带宽，48kHz 装不下）。

为什么 FM 敢把载波放在 0Hz（AM 却要躲开直流，见 u3-l3）？因为鉴频只关心**相位差**：载波在 0Hz 还是偏了 2kHz，只是给所有相位差叠加一个固定偏置（输出加一个直流），音频照常叠加其上。FM 的信息在「变化量」里，直流不携带信息也不干扰解调。

- [main.c:197-L201](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L197-L201) —— `set_tune()` 里 `center_frequency = hz - mode_freq_offset`：FM 时 `mode_freq_offset` 为 0，本振直接等于载波（×4 后交外部正交检波）。若中心没调准，鉴频输出只是多一个直流，声音依然可懂——这就是 FM「调谐宽容」的数学解释。

- [main.c:258-L276](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/main.c#L258-L276) —— I2S 回调 `i2s_end_callback()` 每半块缓冲触发一次，`(*signal_process)(p, q, n)`（L267）经函数指针进入 `fm_demod`。`n` 是 int16 计数 480，即 240 个打包 IQ 对；192kHz 时回调周期 240/192000 = **1.25ms**（与 u2-l3 的推导一致）。

#### 4.1.4 代码实践：亲眼看见「幅度被约掉」

实践目标：验证共轭相乘取辐角对幅度完全不敏感。

操作步骤（PC，Python 即可，无需硬件）：

```python
# 示例代码
import numpy as np
phi = 0.7                       # 任取一个相位差
for A in (1.0, 0.01):           # 幅度差 100 倍
    x0 = 0.3 + 0.2j             # 随便一个"上一样本"
    x1 = x0 * np.exp(1j * phi) * A / abs(x0) * abs(x0)  # 保持幅度比为 A
    p = np.conj(x0) * x1
    print(A, np.angle(p))       # 输出辐角
```

需要观察的现象：两次的辐角打印值相同（都 ≈ 0.7）。

预期结果：辐角恒等于 0.7，幅度只影响 `abs(p)`。这正是 FM 接收不怕衰落的原因。此实践结果确定，可直接运行验证。

#### 4.1.5 小练习与答案

**练习 1**：证明 \(\overline{x_0} \cdot x_1\) 的模等于 \(A_0 A_1\)。
**答案**：\(|\overline{x_0}| = |x_0|\)，复数乘积的模等于模的乘积，故模为 \(A_0 A_1\)；辐角为 \(-\phi_0 + \phi_1 = \Delta\phi\)。

**练习 2**：为什么不用「对 \(x_0\)、\(x_1\) 各做一次 atan2 再相减」？
**答案**：需要两次 atan2（运算翻倍），而且两个辐角各自落在 \((-\pi,\pi]\)，当真实相位差跨越 ±π 边界时（信号频率接近半采样率），相减结果会跳变 2π；共轭相乘先减后取角，\(\Delta\phi\) 天然落在正确区间。

**练习 3**：载波中心偏了 +50kHz 没调准，鉴频输出会怎样？
**答案**：叠加一个直流 \(50000/29.8 \approx 1677\)，音频照常叠加其上；耳机里只是多了一点直流偏置（喇叭耦合电容会挡掉），声音依然可懂。

### 4.2 `fm_demod()` 主循环与跨缓冲块状态保持

#### 4.2.1 概念说明

音频是以 1.25ms 一块的方式分段送来的（u2-l3 的乒乓双缓冲）。鉴频需要「当前样本与**上一个**样本」做差分——可上一块的最后一个样本在上一块里，本块看不见。如果不做任何处理，每块开头的第一个样本就成了「无源之水」。`fm_demod_state.last` 就是跨块的接力棒：**把上一块最后一个打包样本存下来，供下一块第一个差分使用**。

注意这个结构体是三个字段的匿名结构（dsp.c L487-L491）：`last` 属于 mono 鉴频；`pre1`/`pre2` 是立体声路径里 `fm_adj_filter` 的历史样本（本讲只在 4.4.4 提及，u3-l5 详讲）。

#### 4.2.2 核心流程

```text
进入 fm_demod(src, dst, len):
  x0 = fm_demod_state.last          ← 取出上一块最后样本（块间接力）
  循环 len/2 次（每次消费一个 32 位打包 IQ 对）:
    x1 = *s++                       ← 读当前样本
    v  = atan_2iq(x0, x1)           ← 相位差（下一节展开）
    *dst32++ = 打包(v, v)           ← 复制到左右两个声道
    x0 = x1                         ← 块内接力
  fm_demod_state.last = x0          ← 存回本块最后样本，交给下一块
```

#### 4.2.3 源码精读

- [dsp.c:568-L588](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L568-L588) —— `fm_demod()` 主体。对照上面流程图逐行看：
  - `int32_t *s = __SIMD32(src)`：把交织 IQ 缓冲按 32 位字看待，一次取出一个 I/Q 对（低 16 位 I、高 16 位 Q）。
  - `uint32_t x0 = fm_demod_state.last`：**块边界的连续性就在这一行**。
  - `for (i = 0; i < len; i += 2)`：`len` 按 int16 计（480），步进 2 即每次一个 IQ 对，共 240 次。
  - `*dst32++ = __PKHBT(v, v, 16)`：把同一个鉴频值塞进低、高两个半字——单声道复制成双声道输出。
  - `fm_demod_state.last = x0`：把接力棒交回。
  - 首尾两行 `disp_fetch_samples(B_CAPTURE/B_PLAYBACK, ...)` 是显示子系统的「搭便车」取样点（u4-l1 专题），解调本身不依赖它们。

- [dsp.c:487-L491](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L487-L491) —— 状态结构体定义。无类型名的匿名 struct 直接定义全局变量 `fm_demod_state`，这是 C 里「只此一份、内部联动」状态的常见简写。

- [dsp.c:894-L898](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L894-L898) —— `dsp_init()` 只调用 `stereo_separate_init()`，**没有**清零 `fm_demod_state`。它靠 BSS 段开机清零，且切换模式时不重置——残留的 `last` 只影响切换后第一个样本的差分值，是一次性的单点瞬态，听感上不可辨。

- [nanosdr.h:93](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L93) 与 [nanosdr.h:112-L114](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/nanosdr.h#L112-L114) —— `AUDIO_BUFFER_LEN 480` 与 `signal_process_func_t` 类型：`fm_demod` 正是经这个函数指针类型被挂进 `mod_table` 的。

#### 4.2.4 代码实践（源码阅读型）：删掉接力棒会怎样

实践目标：理解 `last` 的必要性，学会用「改一行、推后果」的方式读实时代码。

操作步骤：

1. 推导：192kHz 采样率下，回调每块处理多少个 IQ 对？周期多少毫秒？
2. 思想实验：把 `uint32_t x0 = fm_demod_state.last;` 改为 `uint32_t x0 = 0;`（即不接力），每块第一个样本的 `atan_2iq(0, x1)` 会算出什么？
3. 有硬件的话（可选）：真的改这一行重新编译烧录，收听一个 FM 电台对比。

需要观察的现象 / 预期结果：

1. 每块 240 对，周期 1.25ms。
2. `re`、`im` 全为 0：`re < 0` 不成立、`im < 0` 不成立、`im >= re`（0≥0）成立走交换分支，随后除法 `d /= re >> 16` 是除以 0——Cortex-M 的 SDIV 指令在默认配置（DIV_0_TRP 关闭）下**不产生异常，返回 0**，于是查表得 0，输出一个 0 值样本。结果是每 1.25ms 出现一个错误样本，即周期约 800Hz 的规律性毛刺咔哒声（此推断待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么循环体里读输入用 `int32_t*`、步进是 `i += 2`？
**答案**：`len` 以 int16 计（480），交织缓冲每两个 int16 组成一个 IQ 对；按 int32 读取一次拿到一个打包对，所以循环 240 次、每次 i 增加 2。

**练习 2**：`fm_demod_state` 的 `pre1`/`pre2` 在 `fm_demod` 里用到了吗？
**答案**：没有。它们是 `fm_adj_filter()`（立体声路径的频响校正滤波器）的历史样本状态，只在 `fm_demod_stereo` 路径使用；mono 的 `fm_demod` 只用 `last`。

**练习 3**：从 AM 切到 FM 再切回来，`fm_demod_state.last` 里残留的是 FM 最后一个样本，切回 FM 时会有问题吗？
**答案**：只影响切换后第一个差分值（一个样本的错误输出，1/192000 秒），随后即被正确的接力覆盖；实际不可闻。

### 4.3 `atan_2iq()`：象限折叠 + 查表 + 线性内插

#### 4.3.1 概念说明

`atan2` 若用浮点库函数（`atan2f`）在 Cortex-M4 上要上百个周期，而鉴频每个样本都要算一次（192kHz！）。固件的做法是教科书级的定点技巧组合：

1. **用 SIMD 一次算出 cos/sin 分量**：输入是两个打包的 IQ 字，`__SMUAD`/`__SMUSDX` 各一条指令完成四个 16 位乘法，直接得到 \(\propto \cos\Delta\phi\) 的 `re` 和 \(\propto \sin\Delta\phi\) 的 `im`。
2. **折叠到第一八分圆**：利用对称性把任意 \((-\pi, \pi]\) 的角折叠进 \([0, \pi/4]\)，这样查表范围只需比值 \(t = \mathrm{im}/\mathrm{re} \in [0, 1]\)。
3. **16.16 定点除法取索引**：`d = im / (re >> 16)` 得到约 16 位的比值，高 8 位做表索引、低 8 位做内插权重。
4. **查表 + 线性内插，再展开回原象限**。

#### 4.3.2 核心流程

```text
输入: iq0（上一样本打包字）、iq1（当前样本打包字）
  re = iq1.lo*iq0.lo + iq1.hi*iq0.hi        ← ∝ cos Δφ   （__SMUAD）
  im = iq1.lo*iq0.hi − iq1.hi*iq0.lo        ← ∝ sin Δφ   （__SMUSDX，符号讨论见 4.3.3）
  ang = 0, neg = 0
  若 re < 0:  re 取正, neg 翻转, ang −= π
  若 im < 0:  im 取正, neg 翻转
  若 im ≥ re: 交换 im/re, neg 翻转, ang = −ang − π/2   ← 折叠进 [0, π/4]
  d = im / (re >> 16)                        ← ≈ (im/re)·2²⁶?（注意：见源码精读，实际是 2¹⁶）
  idx = d 的高 8 位, f = d 的低 8 位
  ang += 表[idx] + (表[idx+1] − 表[idx])·f / 256
  若 neg: ang = −ang                          ← 展开回原象限
  返回 ang / 32                              ← 32768/rad → 1024/rad
```

象限折叠的正确性可以抽四个代表区间验证（\(\theta\) 为真实相位差，单位弧度）：

| 区间 | 走过的分支 | 折叠后表值 a | ang 累积 | 输出 |
|---|---|---|---|---|
| \((0, \pi/4)\) | 无 | \(\theta\) | \(\theta\) | \(\theta\) ✓ |
| \((\pi/4, \pi/2)\) | 交换 | \(\pi/2-\theta\) | \(-\pi/2 + a = -\theta\)，neg 翻转 | \(\theta\) ✓ |
| \((\pi/2, 3\pi/4)\) | re<0 且交换 | \(\theta-\pi/2\) | \(\pi-\pi/2+a=\theta\) | \(\theta\) ✓ |
| \((3\pi/4, \pi)\) | 仅 re<0 | \(\pi-\theta\) | \(-\pi+a=-\theta\)，neg 翻转 | \(\theta\) ✓ |

（其余四个负角区间由 `neg` 对称覆盖，建议你在实践里补全这张表。）

#### 4.3.3 源码精读

- [dsp.c:528-L566](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L528-L566) —— `atan_2iq()` 全文。关键片段：

  ```c
  int32_t re = __SMUAD(iq1, iq0);   // I0*I1 + Q0*Q1
  int32_t im = __SMUSDX(iq1, iq0);  // I0*Q1 - I1*Q0   ← 注意：注释与指令语义的符号之争，见下
  ```

  **一个值得较真的细节**：注释声称 `im = I0*Q1 − I1*Q0`，即 \(\sin\Delta\phi\) 方向。但按 ARM 手册对 `SMUSDX` 的定义（先交换**第二**操作数 iq0 的高低半字，再 lo×lo − hi×hi），严格展开得到的是 `iq1.lo*iq0.hi − iq1.hi*iq0.lo = I1*Q0 − Q1*I0`，与注释**互为相反数**。到底哪个成立？——这正是下面 4.3.4 实践的第一个验证点。无论哪种符号成立，影响都只是**鉴频输出整体反相**（音频波形上下翻转，人耳不可辨），且前端 codec 的频谱倒置补偿（u2-l2 讲过的 `adj=-1`）还会再翻一次极性。学会不迷信注释、动手验证，比结论本身更重要。

  象限折叠段：

  ```c
  if (re < 0) { re = -re; neg = !neg; ang += -Q15_PI_4 * 4; }      // −π
  if (im < 0) { im = -im; neg = !neg; }
  if (im >= re) { swap(im, re); neg = !neg; ang = -ang - Q15_PI_4*2; } // −π/2
  ```

  内部角度单位是 **32768/弧度**（`Q15_PI_4 = 25736 = π/4 × 32768`，见 [dsp.c:525](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L525)），所以 \(\pi\) 写作 `Q15_PI_4*4`、\(\pi/2\) 写作 `Q15_PI_4*2`。

  查表内插段：

  ```c
  d = im << 0;            // 就是 d = im（写法上保留的移位占位）
  d /= re >> 16;          // 用"砍掉低16位"的除数，一步得到 ≈(im/re)·2¹⁶
  idx = (d >> 8) & 0xff;  // 高8位：表索引
  f = d & 0xff;           // 低8位：内插权重
  a = arctantbl[idx]; b = arctantbl[idx+1];
  ang += a + (((b - a) * f) >> 8);
  ```

  `re`、`im` 都是 q30（两个 q15 的乘积之和），`re >> 16` 只保留高 16 位，于是 `d = q30 / q14`，量纲上恰好得到比值乘 \(2^{16}\)——**一次移位替代了归一化**。副作用有二：除数被砍到 15 位有效精度（对此用途足够）；当信号极弱、\(|re| < 2^{16}\) 时 `re >> 16` 为 0，Cortex-M 的 SDIV 默认返回 0 而不陷入异常，`d = 0` → 查表得 0——不崩溃，但角度精度退化，这是弱信号下鉴频噪声的一个来源。

  收尾一行 `return __SSAT(ang/32, 16)` 是缩放链的最后一级：

  \[ \underbrace{32768/\text{rad}}_{\text{表与象限修正}} \;\xrightarrow{\;/32\;}\; \underbrace{1024/\text{rad}}_{\text{输出}} \]

  \(|\Delta\phi| \le \pi\) → \(|v| \le 3217\)，永远在 int16 内，`__SSAT` 只是保险。换算成频率：\(v = \Delta\phi \cdot 1024 = 2\pi f \cdot 1024 / f_s\)，在 \(f_s = 192\,\mathrm{kHz}\) 时 \(v \approx f / 29.8\)。

#### 4.3.4 代码实践（本讲主实践）：PC 上提取 `atan_2iq`，扫频验证鉴频线性

实践目标：把 `atan_2iq` 与 `arctantbl` 从固件中提取到 PC，用 5kHz→15kHz 线性扫频信号验证「鉴频输出随时间线性上升」，并与标准 `atan2` 对照统计最大角度误差。

操作步骤：

1. 新建 `fm_atan_test.c`，把 [dsp.c:493-L523](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L493-L523) 的 `arctantbl` 258 项**原样复制**进去，再照下框敲入提取版（示例代码，SIMD 内建函数用标准 C 复刻，其余与固件逐行相同）：

   ```c
   // 示例代码：fm_atan_test.c   编译: gcc -O2 fm_atan_test.c -lm -o fm_atan_test
   #include <stdio.h>
   #include <stdint.h>
   #include <math.h>
   #define Q15_PI_4 25736
   static const int16_t arctantbl[256+2] = {
       0, 128, 256, /* ……共 258 项，从 dsp.c 原样复制…… */ 25736, 25736 };

   static inline int32_t smuad(uint32_t a, uint32_t b) {      // __SMUAD
       return (int16_t)(a&0xffff)*(int16_t)(b&0xffff)
            + (int16_t)(a>>16)  *(int16_t)(b>>16); }
   static inline int32_t smusdx(uint32_t a, uint32_t b) {     // __SMUSDX：交换第二操作数
       return (int16_t)(a&0xffff)*(int16_t)(b>>16)
            - (int16_t)(a>>16)  *(int16_t)(b&0xffff); }
   static inline int32_t ssat(int32_t v) {
       return v > 32767 ? 32767 : (v < -32768 ? -32768 : v); }
   static inline uint32_t pkhbt(int32_t lo, int32_t hi) {     // __PKHBT(x,y,16)
       return ((uint32_t)lo & 0xffff) | ((uint32_t)hi << 16); }

   static int16_t atan_2iq(uint32_t iq0, uint32_t iq1) {      // 与 dsp.c 逐行对应
     int32_t re = smuad(iq1, iq0);
     int32_t im = smusdx(iq1, iq0);
     int32_t ang = 0; uint8_t neg = 0;
     if (re < 0) { re = -re; neg = !neg; ang += -Q15_PI_4*4; }
     if (im < 0) { im = -im; neg = !neg; }
     if (im >= re) { int32_t x=im; im=re; re=x; neg=!neg; ang = -ang - Q15_PI_4*2; }
     uint32_t d, f; int32_t a, b; int idx;
     d = im; d /= (uint32_t)(re >> 16);
     idx = (d >> 8) & 0xff; f = d & 0xff;
     a = arctantbl[idx]; b = arctantbl[idx+1];
     ang += a + (((b - a) * (int32_t)f) >> 8);
     if (neg) ang = -ang;
     return ssat(ang/32);
   }

   int main(void) {
     const double fs = 192000.0, T = 0.1;                     // 0.1 秒数据
     const int N = (int)(fs*T);
     uint32_t prev = 0;
     double maxerr = 0;
     for (int n = 0; n < N; n++) {
       double t = n/fs;
       double f = 5000.0 + 10000.0*t/T;                       // 5kHz → 15kHz 线性扫频
       double ph = 2*M_PI*(5000.0*t + 5000.0*t*t/T);          // ∫2πf dt
       uint32_t x1 = pkhbt((int16_t)(20000*cos(ph)), (int16_t)(20000*sin(ph)));
       int16_t v = atan_2iq(prev, x1);
       double I0 = (int16_t)(prev&0xffff), Q0 = (int16_t)(prev>>16);
       double I1 = (int16_t)(x1&0xffff),   Q1 = (int16_t)(x1>>16);
       double ref = atan2(I0*Q1 - I1*Q0, I0*I1 + Q0*Q1) * 1024; // 注释口径的 atan2 对照
       double err = fabs(v - ref);
       if (err > maxerr) maxerr = err;
       if (n % 1920 == 0)                                     // 每 10ms 打一点，看趋势
         printf("t=%.4fs f=%7.0fHz v=%6d 理论=%8.2f\n", t, f, v, 2*M_PI*f/fs*1024);
       prev = x1;
     }
     printf("与 atan2 的最大输出偏差 = %.2f LSB（1024/rad 单位，合约 %.4f 度）\n",
            maxerr, maxerr/1024*180/M_PI);
     return 0;
   }
   ```

2. 编译运行：`gcc -O2 fm_atan_test.c -lm -o fm_atan_test && ./fm_atan_test`。
3. 检查每 10ms 的打印点：`v` 是否随 `f` 线性增长（理论斜率 1/29.8）。
4. 看最后统计的最大偏差。
5. 回头验证 4.3.3 的符号之争：把 `ref` 一行里的 `I0*Q1 - I1*Q0` 换成 `I1*Q0 - I0*Q1`，看哪一版与 `v` 吻合。

需要观察的现象 / 预期结果：

- `v` 从约 \(5000/29.8 \approx 168\) 线性上升到约 \(15000/29.8 \approx 503\)——鉴频器对扫频输入给出线性上升的输出（待本地验证具体数值）。
- 与口径一致的 `atan2` 参照相比，最大偏差预计在零点几个 LSB（千分之几度）量级：线性内插的理论误差约 \(h^2\max|\mathrm{atan}''|/8 \approx 10^{-6}\) rad，真正的主导误差是 int16 输入量化和 `d /= re>>16` 的整数截断（待本地验证）。
- 符号实验会告诉你 `smusdx`（即 ARM 的 `SMUSDX` 交换第二操作数语义）到底对应注释的哪个方向——若发现 `v` 是负斜率而换符号后完全吻合，就证明实际指令算出的是 \(I_1 Q_0 - I_0 Q_1\)，源码注释写反了；这只是输出反相，不影响鉴频功能。

#### 4.3.5 小练习与答案

**练习 1**：折叠后为什么查表索引一定不会越界？
**答案**：交换分支的条件是 `im >= re`，进入查表时必有 `im < re` 严格成立，故 \(t = \mathrm{im}/\mathrm{re} < 1\)，\(d < 2^{16}\)，`idx = (d>>8)&0xff ≤ 255`；`arctantbl[idx+1]` 访问的第 256 项就是表尾 25736，第 257 项是冗余哨兵。

**练习 2**：`re >> 16` 等于 0 时会发生什么？为什么固件敢这么写？
**答案**：Cortex-M 的 SDIV 在默认配置（DIV_0_TRP 关闭）下除以 0 返回 0、不触发异常，于是 `d = 0`、查表得 0。信号极弱时输出退化为 0/粗量化值——不崩溃但精度丧失，是弱信号鉴频噪声的来源之一。

**练习 3**：输出尺度为什么除以 32？换成除以 16 会怎样？
**答案**：内部角度单位 32768/rad，除 32 得 1024/rad，使 \(\pm\pi\) 映射到 ±3217，落在 int16 且留足余量。除以 16 则为 2048/rad，\(\pm\pi\) 映射 ±6434，仍不溢出但输出幅度翻倍——增益与后级音量链配合的问题，不影响是否溢出。

### 4.4 `arctantbl`：258 项小表如何覆盖整个复平面

#### 4.4.1 概念说明

[arctantbl](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L493-L523) 是一张 `int16_t arctantbl[256+2]` 的常量表。它之所以能用 258 个数服务任意象限的角度，靠的是 4.3 的折叠：所有对称性都被折叠逻辑吃掉后，只剩 \([0, 1]\) 上的 \(\arctan(t)\) 需要存储，而 \(\arctan\) 在 \([0,1]\) 上曲率平缓，256 点 + 线性内插绰绰有余。

表的构造公式（角度单位 32768/rad）：

\[ \mathrm{arctantbl}[i] = \operatorname{round}\!\left(\arctan\frac{i}{256} \times 32768\right), \quad i = 0, 1, \dots, 257 \]

#### 4.4.2 核心流程

```text
生成（离线，设计期）:        for i in 0..257:  round(atan(i/256)*32768)
使用（在线，每个样本）:      idx = 比值高8位; f = 比值低8位
                             结果 = 表[idx] + (表[idx+1]−表[idx])·f/256
```

三个可手算的锚点，用来核对理解：

- `arctantbl[1] = 128`：\(\arctan(1/256) \approx 1/256\)，小角度近似 \(\tan\theta\approx\theta\)，\(32768/256 = 128\)。
- `arctantbl[128] = 15193`：\(\arctan(0.5) \times 32768 = 0.46365 \times 32768\)。
- `arctantbl[256] = 25736`：\(\arctan(1) = \pi/4\)，恰好等于 `Q15_PI_4`。

#### 4.4.3 源码精读

- [dsp.c:493-L523](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L493-L523) —— 表定义。头部是精确的等差 0, 128, 256, …（小角度区近似线性）；尾部两行：

  ```c
  …, 25607, 25672, 25736, 25736
  ```

  最后**两项同为 25736**：第 256 项是数学上的表尾 \(\pi/4\)；第 257 项是**哨兵**——因为内插要访问 `idx+1`，多留一项就不用在代码里做边界判断（`arctantbl[257]` 按公式算出 \(\operatorname{round}(\arctan(257/256)\cdot 32768) = 25736\)，与表尾天然相同）。这种「哨兵结尾」的表设计与 tlv320aic3204 的寄存器配置表（u2-l2）、ILI9341 初始化表（u2-l4）是同一思想：用数据布局换控制流简化。

- [dsp.c:525](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L525) —— `#define Q15_PI_4 25736`，注释注明 \(3.14159/4 \times 32768\)，被象限折叠的三个修正量复用（×2 = π/2，×4 = π）。

- 与 NCO 的 `cos_sin_table` 对比（u3-l1）：一张是「相位→正余弦值」，供混频用；一张是「正切比值→角度」，供鉴频用。两者都以「值 + 前向差分」的精神压进 256 点，都以 32768 为满幅单位——固件的表设计语言一以贯之。

#### 4.4.4 代码实践：用 Python 重新生成整张表并 diff

实践目标：证明表确实是 \(\operatorname{round}(\arctan(i/256)\cdot 32768)\)，并理解哨兵项。

操作步骤：

1. 运行下面的示例代码（需 numpy，从 dsp.c 复制表数据或解析源文件均可）：

   ```python
   # 示例代码
   import numpy as np, re
   src = open('dsp.c').read()
   body = src[src.index('arctantbl'):src.index('Q15_PI_4')]
   table = np.array([int(x) for x in re.findall(r'-?\d+', body)][1:])  # 跳过 "256+2"
   mine = np.round(np.arctan(np.arange(258)/256) * 32768).astype(int)
   diff = np.nonzero(table != mine)[0]
   print("项数:", len(table), "与公式不符的下标:", diff, "差值:", table[diff]-mine[diff])
   print("表尾:", table[-3:], "哨兵检验: arctantbl[257]==arctantbl[256] ->", table[257]==table[256])
   ```

2. 观察 `与公式不符的下标` 一行。

需要观察的现象 / 预期结果：

- 258 项全部与公式吻合（`不符的下标` 为空），仅可能因舍入规则（round 与 rint 的 .5 处理）有个别 ±1 的分歧——若有，记录下标与差值。
- 哨兵检验输出 True。
- 顺带把 4.2 练习里提到的 `fm_adj_filter`（[dsp.c:751-L789](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L751-L789)）和 `fm_demod0`（[dsp.c:791-L804](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L791-L804)）扫一眼即可：`fm_demod0` 就是去掉显示搭便车的裸鉴频循环，被 `fm_demod_stereo` 调用——那是下一讲的入口。

#### 4.4.5 小练习与答案

**练习 1**：`arctantbl[64]` 的值是多少？先按公式算，再到 dsp.c 里数一数（每行 9 个值）。
**答案**：\(\operatorname{round}(\arctan(0.25)\times 32768) = \operatorname{round}(8027.46) = 8027\)。按每行 9 个数到第 8 行（dsp.c L501）：下标 63 是 7907，下标 64 正是 8027。

**练习 2**：若把表长改成 128+2 项（索引/内插各 7 位），最大误差会恶化多少？
**答案**：内插误差 \(\propto h^2\)，\(h\) 翻倍则误差 ×4，从约 \(10^{-6}\) rad 到 \(4\times10^{-6}\) rad——仍然极小。但 `d` 的低 7 位做权重、高 7 位做索引，比值分辨率变粗，叠加整数除法截断后弱信号抖动更明显；选 256 主要是与「值+差分」256 点 cos 表保持一致的工程整齐性（此量化比较为分析推断，可在 4.3.4 的程序里改表长实测）。

**练习 3**：为什么表类型是 `int16_t` 而不是 `uint16_t`（表内全是非负数）？
**答案**：最大值 25736 < 32768，int16 放得下；选有符号是为了与内插表达式 `(((b - a) * f) >> 8)` 的算术语义（有符号乘移位）自然配合，避免混合符号性的提升陷阱。

## 5. 综合实践：分块复刻 `fm_demod`，验证跨块状态的作用

把 4.3 提取的 `atan_2iq` 组装成完整的分块鉴频器，模拟固件真实的运行节奏，对比「有 `last` 接力 / 无接力」两个版本：

1. **生成测试信号**：0.05 秒、fs=192kHz 的复数 FM 信号——先用 1kHz 正弦做调制信号，频偏 ±5kHz，即瞬时频率 \(f(t) = 5000\sin(2\pi \cdot 1000 t)\)，相位取其积分（可直接复用 4.3.4 的 `ph` 写法，把扫频公式换成积分后的相位）。
2. **模拟固件分块**：每 240 个样本为一块循环调用你写的 `my_fm_demod(block_in, block_out, 480)`，函数内部结构与 4.2.2 流程图一致，用静态变量充当 `fm_demod_state.last`。
3. **对照版本**：再写一个把 `last` 换成局部变量 0 的版本（不接力）。
4. **观察**：把两个版本的输出按样本序号画出来（gnuplot/matplotlib 均可），放大任意块边界处（每 240 的倍数）对比。
5. **预期结果**：接力版在块边界平滑连续，且与「一次性整批处理」的结果逐样本一致；不接力版每 240 个样本出现一个异常点（趋近 0 的错误差分），形成周期 1.25ms（800Hz）的规律毛刺。有硬件者可进一步在真机上删除 [dsp.c:574](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L574) 这行听对比（待本地验证）。

这个实践把本讲三个模块串成一条线：4.1 的差分数学 → 4.3 的 `atan_2iq` → 4.2 的跨块状态。

## 6. 本讲小结

- FM 信息在**相位差**里：\(\overline{x_0}x_1\) 的辐角 = \(\Delta\phi\) = 瞬时频率 × \(2\pi/f_s\)；幅度在 `atan2` 的比值中被自动约掉，鉴频天生限幅、抗幅度干扰。
- `fm_demod()` 是全固件最短的解调器：一个循环里对相邻打包 IQ 样本调 `atan_2iq`，结果复制双声道；FM 在 `mod_table` 中 `freq_offset=0`、`fs=192`——载波就在 0Hz，鉴频对直流天然免疫，这与 AM 的 10kHz 低中频策略形成对照。
- `atan_2iq()` 的技巧链：SIMD 点积/叉积 → 象限折叠进 \([0,\pi/4]\) → 16.16 定点除法取「8 位索引 + 8 位权重」→ 查表线性内插 → 展开回原象限 → `/32` 缩放到 1024/rad 输出（fs=192kHz 时 \(v \approx f/29.8\)）。
- `arctantbl[i] = round(atan(i/256)·32768)`，258 项含哨兵；除法 `d /= re>>16` 用移位除数一步完成归一化，但 \(\mathrm{re}<2^{16}\) 时除零退化为 0（SDIV 默认不陷阱），是弱信号精度崩溃点。
- `fm_demod_state.last` 是块间接力棒：没有它，每 1.25ms 丢一个差分样本，产生 800Hz 周期毛刺。
- 一个诚实的发现：`__SMUSDX` 一行的注释与按 ARM 手册推导的符号互为相反数，只影响输出极性——注释也可能笔误，动手验证是读源码的必修课（4.3.4 实践第 5 步）。

## 7. 下一步学习建议

下一讲 **u3-l5「FM 立体声：导频 PLL、去加重与和差矩阵」** 在本讲的基础上继续走 `fm_demod_stereo()` 的链路：先用 `fm_adj_filter`（用到本讲状态结构体里的 `pre1`/`pre2`）校正频响，再经 `fm_demod0`（即本讲的裸鉴频循环）得到基带，然后用 `stereo_separate()` 里的 19kHz 导频软件 PLL 恢复 38kHz 副载波、`stereo_matrix` 分离左右声道。建议阅读顺序：先重读 [dsp.c:791-L820](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L791-L820)，再预习 [dsp.c:596-L683](https://github.com/ttrftech/CentSDR/blob/e4079566e42790497f004e16f9ebf68c83d024a0/dsp.c#L596-L683) 的 `stereo_separate()`——那里出现的 32 位相位步进 `PHASESTEP_NCO19KHz` 会再次用到 u3-l1 讲过的 NCO 知识，只是精度升到了 32 位。
