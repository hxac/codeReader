# 常用波形生成：sawtooth / square / chirp / unit_impulse

> 本讲属于「单元 2：信号生成与窗函数」。在学完单元 1（你已经知道 `scipy.signal` 的命名空间是如何通过「私有实现模块 → `_signal_api` 聚合 → `_support_alternative_backends` 装饰 → `__init__` 暴露」这条链路编织出来的）之后，我们从**最容易上手**的一类功能讲起：合成一段测试信号。

## 1. 本讲目标

学完本讲，你应当能够：

- 理解 `sawtooth` / `square` 这两类**周期波形**是如何用一个标量参数（`width` / `duty`）控制波形形状的，并知道它们「不是带限的」这一重要性质。
- 理解 `chirp` 扫频信号的数学本质：它返回的不是频率本身，而是瞬时频率 \(f(t)\) 对时间积分得到的**相位**的余弦。
- 读懂 `_chirp_phase` 中线性 / 二次 / 对数 / 双曲四种扫频方法的相位公式，并能说出 `chirp` 与 `_chirp_phase` 的分工。
- 学会用 `unit_impulse` 构造离散单位冲激（Kronecker δ），并理解它在「测量系统冲激响应」中的典型用法。
- 独立完成一个「生成 chirp + 叠加 sawtooth + 画图」的小实践，并能把本讲四类波形组装成一个可分析频谱的测试信号。

## 2. 前置知识

在进入源码前，先建立四个直觉概念。

**(1) 周期与相位的关系。** 一个频率为 \(f\)（单位：cycles/单位）的余弦写成

\[
\cos(2\pi f t)
\]

其中 \(2\pi f t\) 就是**相位**（phase，单位弧度）。`scipy.signal` 的波形函数习惯把「一个完整周期」对应到 \(2\pi\)，因此传入的 `t` 通常要先乘以 \(2\pi f\)。例如 `sawtooth` / `square` 的周期被硬编码为 \(2\pi\)，调用时要自己写 `signal.sawtooth(2*np.pi*f*t)`。

**(2) 瞬时频率与调频信号。** 如果频率本身随时间变化 \(f(t)\)，那么相位不能再写成 \(2\pi f t\)，而要写成 \(f(t)\) 的积分：

\[
\phi(t) = \int_0^{t} 2\pi f(\tau)\,d\tau
\]

信号则为 \(s(t)=\cos(\phi(t))\)。`chirp`（啁啾 / 扫频信号）就是这种「频率随时间扫描」的信号，雷达、声呐、超声成像、扬声器测试里都很常见。

**(3) 带限（band-limited）。** 一个理想的锯齿波 / 方波在数学上含有无穷多个谐波。但计算机里的信号是**离散采样**的，高于奈奎斯特频率（采样率的一半）的谐波会被「混叠（alias）」回来。所以源码里反复提醒「this is not band-limited」——你画出来的 `sawtooth` / `square` 在高频段其实是被混叠扭曲过的，不能当成「纯净」模拟波形来用。

**(4) 离散单位冲激（Kronecker δ）。**

\[
\delta[n-k] \equiv \begin{cases}1, & n=k\\ 0, & n\neq k\end{cases}
\]

它只在第 \(k\) 个采样点为 1，其余全 0。把一个线性时不变（LTI）系统的输入设成 δ，输出就是该系统的**冲激响应**——这是后续讲滤波器（单元 4）时反复用到的探针。`unit_impulse` 就是用来生成这根「针」的。

> 术语速查：**周期 (period)**、**相位 (phase)**、**瞬时频率 (instantaneous frequency)**、**占空比 (duty cycle)**、**混叠 (aliasing)**、**带限 (band-limited)**、**Kronecker δ**。

## 3. 本讲源码地图

本讲只涉及一个文件，但它是 `scipy.signal` 里最「干净、自包含」的模块之一，非常适合作为读源码的起点。

| 文件 | 作用 | 本讲涉及函数 |
| --- | --- | --- |
| [`_waveforms.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_waveforms.py) | 各类测试 / 合成信号发生器，纯 Python 实现，支持 Array-API 多后端 | `sawtooth`、`square`、`chirp`、`_chirp_phase`、`unit_impulse` |

整个模块的对外清单写在文件顶部：

[_waveforms.py:15-16](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_waveforms.py#L15-L16) —— 定义 `__all__`，列出 6 个公开函数：`sawtooth`、`square`、`gausspulse`、`chirp`、`sweep_poly`、`unit_impulse`。这正是单元 1 讲过的「`_signal_api` 用 `dir()` 自动聚合同一个 `__all__`」机制的输入来源——本模块新增 / 删除函数，公共命名空间会自动跟随。

模块的依赖也很轻量：

[_waveforms.py:7-12](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_waveforms.py#L7-L12) —— 只依赖 NumPy 的初等函数（`cos`、`sin`、`exp`、`log` 等）和两个 Array-API 工具：`array_namespace` / `xp_promote`（来自 `scipy._lib._array_api`）和 `array_api_extra as xpx`。后两者让 `sawtooth` / `square` 能透明地接受 CuPy、JAX 等非 NumPy 数组——这正是单元 1 讲的「多后端委托」在**实现层**的落地（注意：与命名空间层的 `delegate_xp` 不同，这里是函数体内部主动用 `xp.*` 写的）。

> 本讲按「**最小模块**」拆成 5 节。`gausspulse`（高斯调制脉冲）和 `sweep_poly`（多项式扫频）同属本模块、原理相近，但不在本讲最小模块清单内，留作第 7 节的延伸阅读。

## 4. 核心概念与源码讲解

### 4.1 sawtooth：周期锯齿波与三角波

#### 4.1.1 概念说明

锯齿波（sawtooth / triangle）是一段在一个周期内「线性上升、然后瞬间回落」的波形。`sawtooth` 用一个参数 `width` 控制上升段占整个周期的比例：

- `width=1`（默认）：全程上升，末尾垂直回落 —— 标准锯齿。
- `width=0`：全程下降 —— 反向锯齿。
- `width=0.5`：上升一半、下降一半 —— 对称三角波。

源码把「一个周期」固定为 \(2\pi\)，输出幅度在 \([-1, 1]\) 之间。

#### 4.1.2 核心流程

把任意时间 `t` 折叠进一个 \([0, 2\pi)\) 的周期（取模），再按 `width` 分两段线性映射：

\[
\text{tmod} = t \bmod 2\pi
\]

上升段（\(0 \le \text{tmod} < \text{width}\cdot 2\pi\)）：

\[
y = \frac{\text{tmod}}{\pi\cdot w} - 1
\]

下降段（\(\text{width}\cdot 2\pi \le \text{tmod} < 2\pi\)）：

\[
y = \frac{\pi(w+1) - \text{tmod}}{\pi(1-w)}
\]

可以验证：上升段在 \(\text{tmod}=0\) 时 \(y=-1\)，在 \(\text{tmod}=\text{width}\cdot 2\pi\) 时 \(y=+1\)；下降段起点也是 \(+1\)、终点回到 \(-1\)，两段在拼接处连续。

伪代码：

```
若 w 不在 [0,1]：该点置 NaN
tmod = t mod 2π
若 tmod < w*2π：y = tmod/(πw) - 1        # 上升
否则           ：y = (π(w+1) - tmod)/(π(1-w))  # 下降
```

#### 4.1.3 源码精读

函数签名与文档：

[_waveforms.py:19-57](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_waveforms.py#L19-L57) —— `sawtooth(t, width=1.)`。文档明确写了「not band-limited」「produces an infinite number of harmonics, which are aliased」，这是本函数最重要的使用警示。

实现主体（注意 Array-API 写法）：

[_waveforms.py:58-78](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_waveforms.py#L58-L78) —— 分三步：

1. `xp = array_namespace(t, width)` 探测输入属于哪个数组库；`xp_promote(..., force_floating=True)` 把 `t` 和 `width` 广播并强制转成浮点（避免整数除法把波形削平）。
2. `mask1 = (w>1)|(w<0)` 圈出非法 `width`，用 `xpx.at(y, mask1).set(nan)` 置 NaN。`xpx.at` 是 `np.ndarray.at` 的跨后端版本，用来做「带掩码的原地写入」。
3. `tmod = t % (2*xp.pi)` 折叠周期；再用 `mask2` / `mask3` 分别套上升段、下降段公式。

> 关键点：`width` 可以是**数组**（与 `t` 等长），这样波形形状会随时间变化——文档里专门提到这一点，可用于做时变的三角波调制。

#### 4.1.4 代码实践

**目标**：直观感受 `width` 如何改变波形。

```python
# 示例代码（非项目原有，为本讲编写）
import numpy as np
from scipy import signal
import matplotlib.pyplot as plt

t = np.linspace(0, 1, 1000)
for w, name in [(1.0, 'sawtooth (w=1)'),
                (0.5, 'triangle (w=0.5)'),
                (0.25, 'w=0.25')]:
    y = signal.sawtooth(2*np.pi*5*t, width=w)
    plt.plot(t, y, label=name)
plt.legend(); plt.xlabel('t [s]'); plt.title('sawtooth with different width')
plt.show()
```

**操作步骤**：把上面代码存成 `saw_demo.py` 并运行（需要 numpy / scipy / matplotlib）。

**观察现象 / 预期结果**：

- `w=1` 时每个周期是一条从 −1 斜升到 +1、再垂直落回的锯齿。
- `w=0.5` 时是上下对称的三角波。
- `w=0.25` 时上升段只占周期的 1/4，所以上升很陡、下降很缓。
- 所有曲线幅度都在 \([-1,1]\)，周期为 \(1/5=0.2\) 秒（因为频率取 5 Hz）。

（具体绘图外观「待本地验证」，但上述幅度 / 周期 / 陡缓关系由公式决定，是确定的。）

#### 4.1.5 小练习与答案

**练习 1**：`sawtooth(2*np.pi*t, width=0.5)` 与 `sawtooth(2*np.pi*t, width=1.0)` 的频谱有什么本质区别？

**参考答案**：理想情况下，`width=0.5` 的对称三角波只含**奇次**谐波且衰减较快（约 \(1/n^2\)）；`width=1.0` 的锯齿含**全部**谐波且衰减较慢（约 \(1/n\)）。但由于采样混叠，实际看到的频谱在高频会被扭曲。

**练习 2**：若把 `width` 传成 `1.5`，输出会是什么？

**参考答案**：对应 `mask1` 命中，该样本被置为 `NaN`。若整个 `width` 标量为 1.5，则输出全是 `NaN`。

---

### 4.2 square：方波与占空比

#### 4.2.1 概念说明

方波（square wave）在一个周期内只取两个值：\(+1\) 和 \(-1\)。`square` 用 `duty`（占空比）控制「\(+1\) 段」占整个周期的比例：

- `duty=0.5`（默认）：高电平与低电平各占一半 —— 标准方波。
- `duty=0.25`：高电平占 1/4 —— 脉冲宽度调制（PWM）里常见的窄脉冲。
- `duty=0` 或 `1`：退化为恒定 \(-1\) 或 \(+1\)。

周期同样固定为 \(2\pi\)，幅度 \(\pm1\)。和 `sawtooth` 一样，「not band-limited」。

#### 4.2.2 核心流程

\[
\text{tmod} = t \bmod 2\pi
\]

\[
y = \begin{cases}+1, & \text{tmod} < \text{duty}\cdot 2\pi\\ -1, & \text{otherwise}\end{cases}
\]

对比 `sawtooth`：`square` 把「线性上升/下降」整段替换成了「恒定 \(+1\) / 恒定 \(-1\)」，逻辑更简单。

#### 4.2.3 源码精读

[_waveforms.py:81-128](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_waveforms.py#L81-L128) —— 签名 `square(t, duty=0.5)`，文档给出两个示例：基础方波和「用正弦波当占空比」的 PWM。

[_waveforms.py:130-147](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_waveforms.py#L130-L147) —— 实现与 `sawtooth` 几乎是同一套模板：探测后端 → 非法 `duty` 置 NaN → 取模 → 用 `mask2` 把前 `duty` 比例置 \(+1\)、`mask3` 把其余置 \(-1\)。读这段代码时可以直接对照 4.1.3，体会两个函数共用同一种「分段掩码」写法。

> 关键点：因为 `duty` 也可以是数组，所以文档第二个示例 `signal.square(2*np.pi*30*t, duty=(sig+1)/2)` 能用一路正弦 `sig` 实时调制占空比，得到 PWM 波。

#### 4.2.4 代码实践

**目标**：对比不同占空比，并亲手做一次 PWM。

```python
# 示例代码（非项目原有，为本讲编写）
import numpy as np
from scipy import signal
import matplotlib.pyplot as plt

t = np.linspace(0, 1, 1000, endpoint=False)

# 1) 不同占空比的方波
for d in (0.25, 0.5, 0.75):
    plt.plot(t, signal.square(2*np.pi*5*t, duty=d) + d, label=f'duty={d}')
plt.legend(); plt.title('square with different duty'); plt.show()

# 2) PWM：用低频正弦去调制高频方波的占空比
carrier = np.sin(2*np.pi*2*t)            # 2 Hz 调制信号
pwm = signal.square(2*np.pi*40*t, duty=(carrier+1)/2)
plt.plot(t, carrier, label='modulating sine')
plt.plot(t, pwm, label='PWM output')
plt.legend(); plt.title('pulse-width modulation'); plt.show()
```

**观察现象 / 预期结果**：

- 第 1 组里 `duty=0.5` 是标准对称方波；`duty=0.25` 的「\(+1\) 段」明显更窄。
- 第 2 组 PWM 里，方波的脉冲宽度会跟随正弦的瞬时值变宽 / 变窄，正弦峰处脉冲最宽。

（绘图外观「待本地验证」。）

#### 4.2.5 小练习与答案

**练习 1**：理想 `square(duty=0.5)` 的谐波结构与 `sawtooth(width=1)` 有何不同？

**参考答案**：标准方波只含**奇次**谐波（\(1/n\) 衰减，\(n\) 为奇数）；锯齿波含**全部**谐波（\(1/n\) 衰减）。

**练习 2**：为什么源码强调方波「not band-limited」？这对采样有何影响？

**参考答案**：方波的无穷谐波会超过奈奎斯特频率，被混叠回基带，导致采到的方波边沿出现振铃（Gibbs 现象）与高频失真，不能视作纯净模拟方波。

---

### 4.3 chirp：扫频（调频）余弦信号

#### 4.3.1 概念说明

`chirp` 生成一段**频率随时间扫描**的余弦信号。你给定起点频率 `f0`（\(t=0\) 处）、参考时刻 `t1` 及该时刻的频率 `f1`，再选一种扫频规律 `method`，函数就返回 \(s(t)=\cos(\phi(t))\)（或复信号 \(\exp(j\phi(t))\)）。

四种 `method` 对应四种瞬时频率 \(f(t)\)：

1. **linear**：\(f(t)=f_0+\beta t\)，\(\beta=(f_1-f_0)/t_1\) —— 频率匀速变化。
2. **quadratic**：\(f(t)=f_0+\beta t^2\)（或顶点在 \(t_1\) 的形式），\(\beta=(f_1-f_0)/t_1^2\) —— 频率按抛物线变化。
3. **logarithmic**：\(f(t)=f_0(f_1/f_0)^{t/t_1}\) —— 频率按指数（几何）变化，要求 \(f_0,f_1\) 同号非零。
4. **hyperbolic**：\(f(t)=\alpha/(\beta t+\gamma)\) —— 频率按双曲线变化，要求 \(f_0,f_1\) 非零。

#### 4.3.2 核心流程

`chirp` 本身几乎是「转发器」，真正的数学在 `_chirp_phase` 里：

```
phase = _chirp_phase(t, f0, t1, f1, method, vertex_zero) + deg2rad(phi)
若 complex：返回 exp(1j*phase)
否则      ：返回 cos(phase)
```

其中 `phase` 是瞬时频率的积分：

\[
\phi(t) = \int_0^t 2\pi f(\tau)\,d\tau
\]

`phi` 是额外的**初相位偏置**，单位是**度**（不是弧度），在函数内被换算成弧度后加到 phase 上。

#### 4.3.3 源码精读

函数定义与详细公式（注意 docstring 里用 `.. math::` 给出了四种方法的公式）：

[_waveforms.py:249-413](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_waveforms.py#L249-L413) —— `chirp(t, f0, t1, f1, method='linear', phi=0, vertex_zero=True, *, complex=False)`。docstring 的 Notes 一节是四种扫频公式最权威的表述，建议直接对照阅读。`complex` 参数是 1.15.0 新增，用于生成解析信号（可携带负频率，适合通信里的复基带）。

真正的函数体只有两行：

[_waveforms.py:414-416](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_waveforms.py#L414-L416) —— 先调 `_chirp_phase` 算相位，加上 `phi`（度→弧度），再按 `complex` 选 `np.exp(1j*phase)` 或 `np.cos(phase)`。注释 `'phase' is computed in _chirp_phase, to make testing easier.` 解释了为什么要把相位拆到单独函数：方便对相位做独立单元测试。

> 注意：`chirp` 这里用的是 `np.cos` / `np.exp`（裸 NumPy），不像 `sawtooth`/`square` 用 `xp.*`。所以 `chirp` 对非 NumPy 后端的透明支持不如前两者（这与单元 10 会讲的后端能力标注 `capabilities_overrides` 相呼应）。

#### 4.3.4 代码实践（本讲主实践）

**目标**：生成 1 秒、100 Hz→1000 Hz 线性扫频信号，叠加一个 sawtooth，画图。

```python
# 示例代码（非项目原有，为本讲编写）
import numpy as np
from scipy import signal
import matplotlib.pyplot as plt

fs = 10000                       # 采样率 10 kHz
t = np.linspace(0, 1, fs, endpoint=False)

# 线性 chirp：t=0 时 100 Hz，t=1 时 1000 Hz
x = signal.chirp(t, f0=100, t1=1.0, f1=1000, method='linear')

# 叠加一个 5 Hz、幅度 0.3 的锯齿
y = x + 0.3 * signal.sawtooth(2*np.pi*5*t)

plt.subplot(2, 1, 1)
plt.plot(t, y); plt.title('chirp(100->1000 Hz) + sawtooth(5 Hz)')
plt.subplot(2, 1, 2)
# 用频谱图观察扫频轨迹（spectrogram 在单元 7 详讲，这里先用）
f_, t_, Sxx = signal.spectrogram(x, fs)
plt.pcolormesh(t_, f_, Sxx, shading='gouraud')
plt.ylabel('Frequency [Hz]'); plt.xlabel('Time [sec]')
plt.title('chirp spectrogram (频率随时间上升的斜线)')
plt.tight_layout(); plt.show()
```

**操作步骤**：运行脚本，观察上下两幅子图。

**观察现象 / 预期结果**：

- 上图：信号的「疏密」从左到右明显变化——开头稀疏（低频）、结尾密集（高频），整体被一个缓慢的锯齿轮廓调制。
- 下图频谱图：能看到一条从 100 Hz 斜升至 1000 Hz 的亮带，这正是线性 chirp 的「瞬时频率轨迹」。

（具体图像「待本地验证」，但「频率随时间线性上升」这一行为由 `method='linear'` 与公式 \(f(t)=f_0+\beta t\) 确定。）

#### 4.3.5 小练习与答案

**练习 1**：把 `method` 改成 `'logarithmic'`，但 `f0=0`，会发生什么？

**参考答案**：触发 `ValueError: For a logarithmic chirp, f0 and f1 must be nonzero and have the same sign.`（见 4.4.3 的 `_chirp_phase` 校验）。

**练习 2**：`chirp(..., complex=True)` 与默认实信号在频谱上的最大区别是什么？

**参考答案**：复信号是解析信号，只含正频率分量、幅度为 1；实信号是 \(\cos\)，正负频率对称、幅度为 1/2（docstring 末尾明确说明了这一点）。

---

### 4.4 _chirp_phase：chirp 的相位计算核

> `_chirp_phase` 是私有函数（下划线开头），不直接出现在公共命名空间里，但它才是 `chirp` 数学实现的核心，所以单列一节。

#### 4.4.1 概念说明

`_chirp_phase` 接收与 `chirp` 相同的 `t/f0/t1/f1/method/vertex_zero`，返回每个时刻的**相位数组** \(\phi(t)\)。把「算相位」从「取余弦」里剥离出来有两个好处：一是数学清晰（相位 = 瞬时频率的积分），二是便于测试（可以直接断言某个时刻的相位值，而不必从余弦反推）。

#### 4.4.2 核心流程

四种方法各自的相位公式（都对 \(f(t)\) 做了 \(2\pi\int_0^t\!f\,d\tau\) 积分）：

- **linear**：\(\phi=2\pi\big(f_0 t + \tfrac12\beta t^2\big)\)
- **quadratic**：\(\phi=2\pi\big(f_0 t + \tfrac{\beta}{3}t^3\big)\)（`vertex_zero=True`）；否则顶点在 \(t_1\)
- **logarithmic**：\(\phi=2\pi\beta f_0\big((f_1/f_0)^{t/t_1}-1\big)\)，\(\beta=t_1/\ln(f_1/f_0)\)
- **hyperbolic**：\(\phi=2\pi(-\text{sing}\cdot f_0)\ln|1-t/\text{sing}|\)，奇点 \(\text{sing}=-f_1 t_1/(f_0-f_1)\)

#### 4.4.3 源码精读

[_waveforms.py:419-425](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_waveforms.py#L419-L425) —— 函数签名与说明，明确它「就是 `chirp` 的相位实现」。

四种分支实现：

[_waveforms.py:430-449](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_waveforms.py#L430-L449) —— linear / quadratic / logarithmic 三段。注意 `method` 既接受全名也接受缩写（`'lin'`、`'li'`、`'quad'`、`'q'`、`'log'`、`'lo'`），这是 docstring 里「allowed abbreviations」的实现。对数分支显式校验 `f0*f1<=0` 抛错，并处理 `f0==f1` 的退化情形（退化为恒定频率）。

[_waveforms.py:451-466](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_waveforms.py#L451-L466) —— hyperbolic 分支与最终的 `else` 兜底：未知的 `method` 会抛 `ValueError`，提示可选值。双曲分支同样处理了 `f0==f1` 退化与 `f0/f1` 为零的校验。

> 阅读建议：把这里的相位公式与 4.3.1 中瞬时频率 \(f(t)\) 的公式对照——逐项积分就能从 \(f(t)\) 推出这里的 \(\phi(t)\)。`chirp` 与 `_chirp_phase` 的关系也因此清晰：**`chirp` =（余弦封装）+（`_chirp_phase` 提供相位）**。

#### 4.4.4 代码实践

**目标**：直接观察 `_chirp_phase` 输出的相位，验证「频率 = 相位对时间的导数 / \(2\pi\)」。

```python
# 示例代码（非项目原有，为本讲编写）
# _chirp_phase 是私有函数，需要从实现模块导入
from scipy.signal._waveforms import _chirp_phase
import numpy as np

t = np.linspace(0, 1, 10001)
phi = _chirp_phase(t, f0=100, t1=1.0, f1=1000, method='linear')

# 由相位差分数值估计瞬时频率，与理论 f(t)=100+900*t 对照
inst_freq = np.diff(phi) / np.diff(t) / (2*np.pi)
theo_freq = 100 + 900 * t[:-1]
print('max |est - theory| =', np.max(np.abs(inst_freq - theo_freq)))
```

**观察现象 / 预期结果**：打印的最大误差应是接近机器精度的极小数（差分本身有 \(O(\Delta t^2)\) 误差），说明 `_chirp_phase` 的相位确实对应线性瞬时频率。

**注意**：这里为了教学演示直接 import 了私有函数 `_chirp_phase`；**正式代码请用公共接口 `chirp`**，私有函数可能在后续版本调整。

#### 4.4.5 小练习与答案

**练习 1**：`method='quadratic'` 时 `vertex_zero=True` 与 `False` 在相位公式上有何区别？

**参考答案**：`True` 时频率抛物线顶点（最小值）在 \(t=0\)，相位用 \(f_0 t + \beta t^3/3\)；`False` 时顶点在 \(t=t_1\)，相位改用 \(f_1 t + \beta((t_1-t)^3-t_1^3)/3\)（见 [_waveforms.py:436-439](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_waveforms.py#L436-L439)）。

**练习 2**：为什么把相位拆到 `_chirp_phase` 而不直接写在 `chirp` 里？

**参考答案**：源码注释明说「to make testing easier」——可以对相位数组直接做精确断言，而不必从余弦反解相位。

---

### 4.5 unit_impulse：离散单位冲激信号

#### 4.5.1 概念说明

`unit_impulse` 生成 Kronecker δ：一个除了某一点为 1、其余全为 0 的数组。它是离散信号处理里最基础的「探针」信号——把 LTI 系统的输入设成 δ，输出就是冲激响应（详见单元 4 的 `lfilter`）。

它支持一维（一个标量长度）和 N 维（形状元组），并通过 `idx` 指定冲激位置：

- `idx=None`：默认在第 0 个样本。
- `idx='mid'`：放在 `shape//2` 的中心位置（每个维度都居中）。
- `idx=int`：每个维度都放在该索引。
- `idx=tuple`：按多维索引精确放置。

#### 4.5.2 核心流程

```
out = zeros(shape, dtype)
把 idx 规整成与 shape 维数相同的元组：
    None -> (0,0,...)
    'mid' -> (shape//2, shape//2,...)
    标量 -> (idx, idx,...)
out[idx] = 1
return out
```

#### 4.5.3 源码精读

[_waveforms.py:582-672](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_waveforms.py#L582-L672) —— `unit_impulse(shape, idx=None, dtype=float)`。docstring 给出了 Kronecker δ 的数学定义 \(u_k[n]=\delta[n-k]\)，并附了一个「用 δ 测 Butterworth 低通的冲激响应」的经典示例（与单元 4 / 单元 5 衔接）。

实现：

[_waveforms.py:673-685](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_waveforms.py#L673-L685) —— 先 `zeros(shape, dtype)` 建全零数组，再用一组 `if/elif` 把 `idx` 规整成与维度数匹配的元组，最后 `out[idx] = 1`。代码非常简短，是初学者读源码的好入口。

> 关键点：`idx` 用 `hasattr(idx, "__iter__")` 判断是否可迭代，从而区分「标量」与「元组」两种传入方式；`'mid'` 走字符串特判分支。

#### 4.5.4 代码实践

**目标**：生成一维 / 二维冲激，并体会「居中」`idx='mid'` 的用途。

```python
# 示例代码（非项目原有，为本讲编写）
from scipy import signal

print(signal.unit_impulse(8))           # δ[n]，第 0 点为 1
print(signal.unit_impulse(7, 2))         # δ[n-2]，第 2 点为 1
print(signal.unit_impulse((3, 3), 'mid'))# 3x3 中心冲激
print(signal.unit_impulse((4, 4), 2))    # 广播：(2,2) 处为 1
```

**预期结果**（与 docstring 示例一致）：

```
[1. 0. 0. 0. 0. 0. 0. 0.]
[0. 0. 1. 0. 0. 0. 0.]
[[0. 0. 0.]
 [0. 1. 0.]
 [0. 0. 0.]]
[[0. 0. 0. 0.]
 [0. 0. 0. 0.]
 [0. 0. 1. 0.]
 [0. 0. 0. 0.]]
```

> 这段输出是**纯确定性**的（只依赖数组赋值），可放心预期，无需「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：为什么 docstring 用 `unit_impulse(100, 'mid')` 而不是 `unit_impulse(100, 0)` 来测冲激响应？

**参考答案**：居中后，冲激位于时间轴中点，冲激响应向左右两侧自然展开，便于把「因果的」暂态完整显示在画面里，而不至于在 \(t=0\) 处被截断一半。

**练习 2**：`unit_impulse((4,4), 2)` 与 `unit_impulse((4,4), (2,2))` 结果是否相同？

**参考答案**：相同。因为标量 `2` 在内部被广播成 `(2,2)`（见 [_waveforms.py:681-682](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_waveforms.py#L681-L682)）。

---

## 5. 综合实践

**任务：组装一个「四合一」测试信号，并用 FFT 观察它们的频谱差异，把本讲四类波形串起来。**

背景：`sawtooth` / `square` 是周期信号，频谱是离散的谐波峰；`chirp` 是宽带扫频信号，频谱近似覆盖整个扫频区间；`unit_impulse` 的频谱是恒定的（平坦白谱）。这个实践正好把「周期 vs 宽带 vs 平坦」三种典型频谱放在一起对比。

```python
# 示例代码（非项目原有，为本讲编写）
import numpy as np
from scipy import signal
import matplotlib.pyplot as plt

fs, N = 8000, 8000
t = np.arange(N) / fs
freq = np.fft.rfftfreq(N, 1/fs)

def show_mag(x, ax, title):
    mag = np.abs(np.fft.rfft(x)) / N
    ax.plot(freq, mag)
    ax.set_title(title); ax.set_xlabel('Hz'); ax.set_xlim(0, fs/2)

fig, axes = plt.subplots(2, 2, figsize=(10, 6))
# 1) 锯齿：含全部谐波，幅度约 1/n
show_mag(signal.sawtooth(2*np.pi*100*t), axes[0,0], 'sawtooth @100Hz')
# 2) 方波：只含奇次谐波
show_mag(signal.square(2*np.pi*100*t), axes[0,1], 'square @100Hz')
# 3) chirp：宽带，近似覆盖 [100,1000]
show_mag(signal.chirp(t,100,1,1000), axes[1,0], 'chirp 100->1000Hz')
# 4) 单位冲激：频谱几乎平坦
show_mag(signal.unit_impulse(N), axes[1,1], 'unit_impulse (flat spectrum)')
plt.tight_layout(); plt.show()
```

**操作与观察**：

1. 运行脚本，对比四张子图的频谱包络。
2. **预期**：锯齿子图在 100, 200, 300 … Hz 出现逐渐降低的峰；方波只在 100, 300, 500 …（奇次）出现峰；chirp 子图在 100–1000 Hz 区间近似平坦抬升；冲激子图整体接近一条水平线（因为 δ 的 DTFT 恒为 1）。
3. **思考**：把 `signal.square` 那行换成不同 `duty`，观察奇 / 偶谐波分布的变化——这能帮你直观理解「占空比」如何改变频谱。

> 提示：FFT 的具体幅度会随 `N` 归一化方式不同而不同，但「峰的位置 / 谐波有无」是确定的。绘图外观「待本地验证」。

## 6. 本讲小结

- `scipy.signal` 的波形发生器都集中在纯 Python 的 [`_waveforms.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_waveforms.py)，文件顶部 `__all__` 列出 6 个公开函数，是单元 1「命名空间编织」最直接的输入。
- `sawtooth(t, width)` / `square(t, duty)` 都把周期固定为 \(2\pi\)，用一个标量（可数组化）参数控制形状；二者都「不是带限的」，高频谐波会被采样混叠。
- 它们的实现共用同一种「取模 + 分段掩码」模板，并内部使用 `array_namespace` / `xp_promote` / `xpx.at` 的 Array-API 写法，从而天然支持 CuPy / JAX 等后端。
- `chirp` 返回的是瞬时频率积分得到的**相位**的余弦；真正的相位公式在私有函数 `_chirp_phase` 里，按 linear / quadratic / logarithmic / hyperbolic 四种 `method` 给出不同的 \(\phi(t)\)，拆分是为了「便于测试」。
- `unit_impulse` 用最简短的 `zeros + out[idx]=1` 生成 Kronecker δ，支持 N 维与 `idx=None/'mid'/int/tuple` 多种定位，是后续测量系统冲激响应的标准探针。
- 读写源码的两条线索：看 docstring 的 `.. math::` 拿到权威公式，看 `mask1/mask2/mask3` 拿到分段实现逻辑。

## 7. 下一步学习建议

- **继续本单元**：下一讲 `u2-l2` 讲 `max_len_seq`（伪随机最大长度序列），它和本讲的 `unit_impulse` 一样是「系统辨识」常用的激励信号，但频谱更接近白噪声；再往后 `u2-l3` / `u2-l4` 讲 `windows` 子包——窗函数会与本讲的「非带限 / 谐波泄漏」问题直接呼应。
- **延伸阅读本模块**：本讲没展开的 [`gausspulse`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_waveforms.py#L150-L246)（高斯调制脉冲，超声常用）和 [`sweep_poly`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_waveforms.py#L471-L566)（任意多项式扫频，是 `chirp` 的泛化）。
- **向后衔接**：当你学完单元 4 的 `lfilter` 后，回头用 `signal.unit_impulse(N,'mid')` 喂给一个 `butter` 滤波器，就能亲手画出它的冲激响应——把本讲的「探针」和后续的「系统」连起来。
- **阅读源码建议**：`_waveforms.py` 全文不到 700 行、依赖极少，建议通读一遍，作为阅读 `scipy.signal` 其他更大模块（如 `_signaltools.py`）的暖身。
