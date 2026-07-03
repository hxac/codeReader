# 频域（傅里叶）滤波

## 1. 本讲目标

本讲是滤波单元（u2）的最后一篇，承接前 5 讲都在「空域」做加权求和的思路，转到一个完全不同的视角——**频域**。学完本讲你应该能够：

- 说清 `fourier_gaussian` / `fourier_uniform` / `fourier_ellipsoid` / `fourier_shift` 这 4 个函数为什么**不做 FFT**，而是假定输入「已经是 FFT 结果」，只做一次逐元素乘法。
- 解释参数 `n` 与 `axis` 的语义：它们如何区分「输入来自复 FFT」还是「输入来自实 FFT（rfft）」，以及为什么实 FFT 需要额外告诉函数原始长度。
- 理解 `fourier_shift` 为什么总是输出复数：因为它给每个频点乘上了一个复相位因子 \(e^{-2\pi i\,\text{shift}\,k/N}\)，这是频域里实现亚像素平移的标准做法。
- 区分 `_get_output_fourier`（实数可原样输出）与 `_get_output_fourier_complex`（强制提升为复数）两种输出策略的用途。
- 亲手验证「频域高斯」与「空域 `gaussian_filter`」在边界行为上的根本差异。

---

## 2. 前置知识

本讲需要三个直觉，都不复杂，先在这里建立。

**(1) 卷积定理。** 在空域里，滤波 = 用一个核去和信号做卷积/相关。在频域里，这等价于把信号和核**各自做 FFT，再逐点相乘，最后做 IFFT 回到空域**。也就是说，空域的卷积 ↔ 频域的逐点乘法。本讲的 4 个函数干的正是「频域逐点乘法」这一半——它们假定你已经把信号做完了 FFT，只负责构造乘法核并乘上去。

**(2) FFT 的频率排布。** NumPy 的 `np.fft.fft` 把长度为 \(N\) 的信号变换成长度同样为 \(N\) 的复数数组，其频点排布不是 `0,1,...,N-1`，而是按「正频率在前、负频率在后」的 fftfreq 顺序：

\[
\underbrace{0,\,1,\,2,\,\dots,\,\lfloor(N-1)/2\rfloor}_{\text{非负频率}},\;\underbrace{-\lfloor N/2\rfloor,\,\dots,\,-1}_{\text{负频率}}
\]

而 `np.fft.rfft`（实 FFT）只返回非负频率，长度为 \(\lfloor N/2\rfloor+1\)。本讲的 C 内核需要知道输入属于哪种排布，才能给每个频点配上正确的权重——这就是参数 `n`/`axis` 的作用。

**(3) 三种核的频域形状。** 不同形状的空域核，对应不同形状的频域乘法因子：

| 空域核 | 频域乘法因子 \(H(k)\) | 对应函数 |
|---|---|---|
| 高斯 | \(\exp\!\bigl(-2(\pi\sigma k/N)^2\bigr)\)（仍为高斯） | `fourier_gaussian` |
| 盒子（box） | \(\operatorname{sinc}(\text{size}\cdot k/N)=\dfrac{\sin(\pi\,\text{size}\,k/N)}{\pi\,\text{size}\,k/N}\) | `fourier_uniform` |
| 椭球（球/盘） | 1D：sinc；2D：\(2J_1(r)/r\)；3D：\(3(\sin r-r\cos r)/r^3\) | `fourier_ellipsoid` |
| 平移冲激 | \(e^{-2\pi i\,\text{shift}\,k/N}\)（纯相位） | `fourier_shift` |

其中 \(J_1\) 是第一类一阶贝塞尔函数。不必死记公式，记住「核在频域有确定的解析形状，函数只是把这些形状算出来再逐点相乘」即可。本讲义会把上表中每一行都对应到真实源码。

> 小贴士：如果你只想要一个直觉，可以这样记——**高斯平滑后还是高斯，盒子平滑在频域是 sinc（有波纹），平移在频域是转一个角度（相位）**。

---

## 3. 本讲源码地图

本讲只涉及一个 Python 文件，但它背后挂着两个 C 文件：

| 文件 | 作用 |
|---|---|
| [`_fourier.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_fourier.py) | 4 个公开函数的 Python 包装层：参数校验、序列归一化、输出数组准备，最后把工作交给 C 内核。 |
| [`src/ni_fourier.c`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/ni_fourier.c) | 真正的算法内核：`NI_FourierFilter` 构造高斯/盒子/椭球三种乘法核并逐点相乘；`NI_FourierShift` 构造相位因子并做复数乘法。 |
| [`src/nd_image.c`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/nd_image.c) | C 扩展的入口：`methods[]` 分发表把 Python 名 `fourier_filter`/`fourier_shift` 映射到 `Py_FourierFilter`/`Py_FourierShift`，后者解析参数后调用上面的内核。 |

模块导出清单只有 4 个名字，定义在 [`_fourier.py:36-37`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_fourier.py#L36-L37)（`__all__`），这 4 个名字会经 u1-l3 讲过的装配链最终出现在 `scipy.ndimage` 命名空间。

---

## 4. 核心概念与源码讲解

### 4.1 频域乘法核：fourier_gaussian / fourier_uniform / fourier_ellipsoid

#### 4.1.1 概念说明

这三个函数本质上是**同一个内核的三件不同外套**。它们都做同一件事：在频域里给输入数组乘上一个形状已知的乘法核。三者的区别只在于「核长什么样」：

- `fourier_gaussian`：核是高斯，对应空域的高斯平滑（和 `gaussian_filter` 是同一个目标，只是改在频域做）。
- `fourier_uniform`：核是 sinc，对应空域的盒子平均（和 `uniform_filter` 对应）。
- `fourier_ellipsoid`：核是椭球/球的傅里叶变换，对应空域里的椭球状邻域平均。

为什么要把同一个空域操作搬到频域？因为当核很大时，空域卷积是 \(O(N\cdot w)\)（\(w\) 是核宽），而 FFT + 频域乘法 + IFFT 可以做到接近 \(O(N\log N)\)，且与核大小无关。当你在频域里已经有数据、或要做多次滤波组合时，频域路线更划算。

#### 4.1.2 核心流程

三个函数的 Python 包装层长得几乎一模一样，流程是：

1. `np.asarray(input)`：把输入转成数组（**不**做 FFT，假定调用者已经做过了）。
2. `_get_output(output, input)`：准备输出数组（见 4.3）。
3. `normalize_axis_index(axis, input.ndim)`：把 `axis` 规范成合法非负轴。
4. `_normalize_sequence(sigma/size, input.ndim)`：把标量广播成「每轴一个值」的序列。
5. 把序列转成 `float64` 且内存连续的 NumPy 数组（C 内核要求连续内存）。
6. 调用 `_nd_image.fourier_filter(input, params, n, axis, output, filter_type)`，其中 `filter_type` 是一个整数：高斯=`0`、盒子=`1`、椭球=`2`。

关键在于第 6 步那个整数 `filter_type`。三个 Python 函数唯一的实质差别，就是传给 C 内核的这个整数不同。C 端用宏把它定义成易读的名字：

```c
#define _NI_GAUSSIAN 0
#define _NI_UNIFORM 1
#define _NI_ELLIPSOID 2
```

这定义在 [src/ni_fourier.c:45-47](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/ni_fourier.c#L45-L47)，是「Python 传整数、C 端 switch 分派」的单一事实来源。

#### 4.1.3 源码精读

**Python 侧——三个外套。** 先看 `fourier_gaussian` 的函数体（文档串略），它把 `sigma` 归一化后传 `filter_type=0`：

[`_fourier.py:117-126`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_fourier.py#L117-L126)（`fourier_gaussian` 的函数体）：把 `sigma` 归一化、转成连续 float64 数组，然后 `_nd_image.fourier_filter(input, sigmas, n, axis, output, 0)`，最后的 `0` 就是 `_NI_GAUSSIAN`。

`fourier_uniform` 几乎逐行相同，只把变量名换成 `sizes`、把末尾整数换成 `1`，见 [`_fourier.py:175-183`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_fourier.py#L175-L183)。

`fourier_ellipsoid` 多了两处保护，见 [`_fourier.py:236-250`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_fourier.py#L236-L250)：

- 第 237-238 行：`if input.ndim > 3: raise NotImplementedError(...)`——因为椭球核的解析公式只对 1/2/3 维有闭式解（C 端的 switch 也只 case 了 1/2/3）。
- 第 240-243 行：`if output.size == 0: return output`——提前拦截空数组。注释明说「C 代码有个 bug，空数组会 segfault（gh-17270），所以在这里先挡掉」。这是一个用 Python 层补丁绕开 C 层缺陷的真实例子。
- 末尾传 `filter_type=2`（`_NI_ELLIPSOID`）。

**C 侧——内核 `NI_FourierFilter`。** 真正的算法在 [`src/ni_fourier.c:187-452`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/ni_fourier.c#L187-L452) 的 `NI_FourierFilter`。它分三个阶段。

**阶段一：预处理每轴参数。** 见 [src/ni_fourier.c:204-219](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/ni_fourier.c#L204-L219)。对每个轴 `kk`，先算出该轴的「逻辑长度」`shape`：

```c
int shape = kk == axis ?
        (n < 0 ? PyArray_DIM(input, kk) : n) : PyArray_DIM(input, kk);
```

这一行正是 `n`/`axis` 语义的核心：

- 若 `n < 0`（默认）：输入假定来自复 FFT，逻辑长度就是数组在该轴的实际维度。
- 若 `n >= 0` 且 `kk == axis`：输入假定来自实 FFT（rfft），该轴的频谱被压缩了，逻辑长度要用**原始**长度 `n` 而不是数组的当前维度。
- 其它轴（不是实变换轴）：逻辑长度就是实际维度。

注意：实变换只允许发生在**单个**轴 `axis` 上，其余轴都按复 FFT 处理。这就是为什么 `axis` 是单个整数而不是序列。

然后按 `filter_type` 把 `sigma`/`size` 换算成核参数：高斯把 \(\sigma\) 换算成 \(-2(\pi\sigma/\text{shape})^2\)（负号 + 平方，后面套进 `exp` 自然衰减），盒子与椭球则原样保留 `size`（后面再用 `shape` 算 sinc）。

**阶段二：预计算每轴查找表。** 见 [src/ni_fourier.c:242-333](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/ni_fourier.c#L242-L333)。为了避免主循环里反复算 `exp`/`sin`，内核为每个轴预算一张长度等于该轴维度的表 `params[hh][k]`，存「频点 k 的权重」。表的内容按 `filter_type` 三选一：

- **高斯**（[L243-L269](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/ni_fourier.c#L243-L269)）：`params[hh][k] = exp(parameters[hh] * k*k)`，即 \(\exp(-2(\pi\sigma/N)^2 k^2)\)。注意频点 k 的取值顺序——非实变换轴走 fftfreq 顺序：先把 `k=0,1,...,(dim+1)/2-1` 填进去（非负频率），再把 `k=-(dim/2),...,−1` 填进去（负频率）；这正好匹配 NumPy `np.fft.fft` 的输出排布。若是实变换轴（`hh==axis and n>=0`），则只用非负频率 `k=0,1,...`，匹配 `np.fft.rfft` 排布。`fabs(tmp) > 50.0 ? 0.0 : exp(tmp)` 是为了在指数极负时直接取 0，避免下溢。
- **盒子**（[L270-L297](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/ni_fourier.c#L270-L297)）：`params[hh][k] = sin(tmp*k)/(tmp*k)`，其中 `tmp = π*size/dim`，正是 sinc 形状；`params[hh][0]=1.0`（sinc 在 0 处取 1）。
- **椭球**（[L298-L330](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/ni_fourier.c#L298-L330)）：先存每轴的「径向坐标平方」`params[hh][k] = (k*π*size/dim)^2`，等主循环里再按维度套用径向公式。

**阶段三：主循环逐点相乘。** 见 [src/ni_fourier.c:344-440](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/ni_fourier.c#L344-L440)。用 `NI_Iterator` 遍历每个元素，算出该频点的乘法因子 `tmp`，再乘以输入：

- 高斯/盒子（核可分离）：`tmp = ∏ params[kk][coord[kk]]`——各轴权重的乘积（[L347-L353](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/ni_fourier.c#L347-L353)）。正因为高斯与盒子的核在各轴之间可分离，才能写成「每轴一张表、最后相乘」。
- 椭球（核不可分离）：按维度用径向公式（[L354-L381](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/ni_fourier.c#L354-L381)）。1D：`sin(r)/r`；2D：`2*J1(r)/r`（调用本文件上方 [`_bessel_j1`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/ni_fourier.c#L82-L157)）；3D：`3*(sin(r)-r*cos(r))/r³`，其中 \(r\) 由各轴径向坐标平方和开根得到。这正是球/盘傅里叶变换的解析表达。
- 之后区分输入是复数还是实数，把 `tmp` 乘到输入上写入 output（[L386-L438](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/ni_fourier.c#L386-L438)）。复数输入走实/虚部各自乘 `tmp`（频域乘法因子是实数，所以不改变相位），实数输入直接标量乘。

**C 分发层。** Python 调用 `_nd_image.fourier_filter(...)` 时，实际进入 [`Py_FourierFilter`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/nd_image.c#L588-L610)（[src/nd_image.c:588-610](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/nd_image.c#L588-L610)）。它用格式串 `"O&O&niO&i"` 解析 6 个参数（input、parameters、n、axis、output、filter_type），然后 `NI_FourierFilter(input, parameters, n, axis, output, filter_type)`。这条映射登记在 `methods[]` 分发表里：[src/nd_image.c:1334-1335](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/nd_image.c#L1334-L1335)，把 Python 名 `fourier_filter` 绑到 `Py_FourierFilter`。

> 一句话总结：三个频域滤波函数 = 一段共享的 Python 参数处理 + 一个 `filter_type` 整数 + 一个能按 `filter_type` switch 出三种核形状的 C 内核。

#### 4.1.4 代码实践

**实践目标**：亲手验证「频域盒子核」与「空域盒子核」的关系，并体会 `n`/`axis` 在实 FFT 下的用法。

**操作步骤**（示例代码，请在你自己的环境运行）：

```python
import numpy as np
from scipy import ndimage

# 1) 构造一个含周期信号的 1D 数组
N = 64
x = np.cos(2 * np.pi * 3 * np.arange(N) / N)   # 3 个周期的余弦

# 2) 复 FFT 路线：默认 n=-1，假定输入是 fft 结果
Xc = np.fft.fft(x)
Yc = ndimage.fourier_uniform(Xc, size=5, n=-1, axis=-1)
y_freq_complex = np.fft.ifft(Yc).real

# 3) 实 FFT 路线：用 rfft，必须传 n=原始长度
Xr = np.fft.rfft(x)
Yr = ndimage.fourier_uniform(Xr, size=5, n=N, axis=-1)
y_freq_real = np.fft.irfft(Yr, n=N)

# 4) 空域对照
y_space = ndimage.uniform_filter1d(x, size=5)

print("复FFT vs 实FFT 最大差:", np.abs(y_freq_complex - y_freq_real).max())
print("频域 vs 空域 内部最大差:", np.abs(y_freq_complex[5:-5] - y_space[5:-5]).max())
print("频域 vs 空域 边界最大差:", np.abs(y_freq_complex[:5] - y_space[:5]).max())
```

**需要观察的现象**：

- 复 FFT 路线与实 FFT 路线的结果应该几乎相同（差异为浮点量级），证明 `n=N` 正确告诉了内核「这条轴来自实 FFT」。
- 频域结果与空域 `uniform_filter1d` 在**数组内部**高度吻合（注意盒子宽度略有差异：频域的 `size=5` 是连续意义下的宽度，空域 `uniform_filter1d(size=5)` 是 5 个离散点；要让两者精确对应需仔细对齐宽度，这里只看数量级）。
- 在**边界**处两者明显不同：频域滤波假设信号周期延拓（循环卷积），空域 `uniform_filter1d` 默认 `mode='reflect'`。

**预期结果**：内部吻合、边界偏离。边界差异正是「频域 = 循环卷积」与「空域 = 反射边界」的本质区别。

**若无法运行**：标注「待本地验证」，但上述结论可从卷积定理与 FFT 周期性直接推出。

#### 4.1.5 小练习与答案

**练习 1**：把上面的 `size=5` 换成很大的值（如 `size=30`），频域与空域哪个更接近「把信号抹平」？为什么？

> **答案**：两者都会更平滑。但频域路线无论 `size` 多大都只是一次逐点乘法 + 两次 FFT，成本与核宽无关；空域 `uniform_filter1d` 虽然用了 O(N) 的滑动和优化，但核宽很大时频域路线在大核场景下通常更划算——这正是频域滤波的存在意义。

**练习 2**：`fourier_ellipsoid` 为什么在 `input.ndim > 3` 时直接抛 `NotImplementedError`？

> **答案**：因为椭球核的径向闭式公式只对 1/2/3 维有解析表达（C 端 `switch(PyArray_NDIM)` 也只 case 了 1/2/3，分别用 sinc、贝塞尔 \(J_1\)、球贝塞尔）。更高维没有实现，故在 Python 层提前拦截，见 [`_fourier.py:237-238`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_fourier.py#L237-L238)。

---

### 4.2 相位平移：fourier_shift

#### 4.2.1 概念说明

`fourier_shift` 和前三个函数不同：它**不是平滑滤波**，而是**平移**——把整幅图（亚像素级地）挪开一段距离。它的妙处在于：空域里的平移，在频域里只是给每个频点乘上一个**单位模的复相位因子**，因此可以做**亚像素**平移（平移量不必是整数），且不会引入插值伪影（在不考虑周期性的前提下）。

数学上，空域平移定理告诉我们：若 \(f[n] \leftrightarrow F[k]\)，则 \(f[n-d] \leftrightarrow e^{-2\pi i\,d\,k/N}F[k]\)。所以只要在频域乘上相位因子 \(e^{-2\pi i\,d\,k/N}\)，再 IFFT，就得到平移 \(d\) 个样本后的信号。多维情形下，各轴相位因子相乘即可。

#### 4.2.2 核心流程

Python 包装层（`fourier_shift`）与前三个的差别只有两点：

1. 输出数组用 `_get_output_fourier_complex`（强制复数，见 4.3），因为相位因子是复数，哪怕输入是实数，输出也必然带虚部。
2. 调用的是**另一个** C 内核 `_nd_image.fourier_shift`（不是 `fourier_filter`），它没有 `filter_type` 参数。

C 内核 `NI_FourierShift` 的流程：

1. 把每轴平移量换算成「相位斜率」`shifts[hh] = -2π * shift / shape`（注意负号与 4.1 同样的 `shape` 取法）。
2. 为每轴预算一张表 `params[hh][k] = shifts[hh] * k`，即该频点的「相位角」；频点顺序同样是 fftfreq 顺序（实变换轴除外）。
3. 主循环里，对每个元素把各轴相位角相加得总相位 `tmp`，算 `cost = cos(tmp)`、`sint = sin(tmp)`，再把输入（实或复）乘以 `cost + i*sint`。

#### 4.2.3 源码精读

**Python 侧**：[`_fourier.py:298-306`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_fourier.py#L298-L306)。注意第 299 行用 `_get_output_fourier_complex`（而非 `_get_output_fourier`），第 305 行调用 `_nd_image.fourier_shift(input, shifts, n, axis, output)`——没有 `filter_type`。

**C 侧预处理相位斜率**：[src/ni_fourier.c:483-490](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/ni_fourier.c#L483-L490)。`shape` 的取法和 `NI_FourierFilter` 完全一致（实变换轴用 `n`、否则用维度），然后把用户给的 `shift` 换算：

\[
\texttt{shifts}[kk] = \frac{-2\pi \cdot \texttt{shift}}{\text{shape}}
\]

负号对应「正向平移 \(d\)」的标准平移定理方向。

**C 侧预算相位角表**：[src/ni_fourier.c:512-529](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/ni_fourier.c#L512-L529)，`params[hh][k] = shifts[hh] * k`，频点顺序同高斯。

**C 侧主循环**：[src/ni_fourier.c:540-547](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/ni_fourier.c#L540-L547) 把各轴相位角相加成总相位 `tmp`，再 `sint = sin(tmp); cost = cos(tmp)`。随后用宏分实/复两种情况乘上去：

- 复数输入（[CASE_FOURIER_SHIFT_C 宏, src/ni_fourier.c:461-465](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/ni_fourier.c#L461-L465)）：做完整的复数乘法 \((a+bi)(\cos\theta+i\sin\theta)\)，即 `_r = a*cost - b*sint; _i = a*sint + b*cost`。
- 实数输入（[CASE_FOURIER_SHIFT_R 宏, src/ni_fourier.c:454-459](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/ni_fourier.c#L454-L459)）：把实数值当成 `a + 0i`，得到 `_r = a*cost; _i = a*sint`——这就是为什么实数输入也会产生非零虚部，输出必须是复数。

整段内核在 [`src/ni_fourier.c:467-605`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/ni_fourier.c#L467-L605)。Python 到 C 的映射由 [`Py_FourierShift`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/nd_image.c#L612-L633)（[src/nd_image.c:612-633](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/nd_image.c#L612-L633)）承担，格式串 `"O&O&niO&"`（少一个 `i`，因为没有 `filter_type`），同样登记在 `methods[]` 表里。

> 注意：因为 FFT 假设信号周期延拓，`fourier_shift` 实现的是**循环平移**——从一边溢出的像素会从另一边回来。这与 u3 单元要讲的空域 `shift` 函数（带边界模式）行为不同。

#### 4.2.4 代码实践

**实践目标**：用 `fourier_shift` 做一次亚像素平移，验证它是循环平移。

**操作步骤**（示例代码）：

```python
import numpy as np
from scipy import ndimage

x = np.zeros(16, dtype=float)
x[4] = 1.0                      # 一个单位冲激在位置 4
X = np.fft.fft(x)

# 平移 2.5 个样本（亚像素！）
Y = ndimage.fourier_shift(X, 2.5, n=-1, axis=-1)
y = np.fft.ifft(Y).real

print("峰值位置附近:", np.round(y[2:8], 3))   # 应在 6.5 附近达峰
print("总和:", y.sum(), " 最大值:", y.max())  # 总和≈1，能量守恒
```

**需要观察的现象**：

- 峰值从位置 4 移到了 6.5（亚像素），且因 sinc 插值会出现小幅振荡（吉布斯现象）。
- 数组总和近似守恒（≈1），说明只是平移、未改变总量。
- 若把平移量改成 `14`（= 16−2），结果应与平移 `−2` 相同——印证「循环平移」。

**预期结果**：亚像素峰位正确移动、能量守恒、大平移量按模 N 循环。

**若无法运行**：标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `fourier_shift` 用 `_get_output_fourier_complex` 而不能用 `_get_output_fourier`？

> **答案**：因为相位因子 \(\cos\theta+i\sin\theta\) 是复数，即使输入是实数，相乘后也会有非零虚部（见 `CASE_FOURIER_SHIFT_R` 宏）。所以输出必须能容纳复数，`_get_output_fourier_complex` 保证输出至少是 `complex64`/`complex128`。

**练习 2**：用 `fourier_shift` 平移 `N/2` 个样本，与直接 `np.roll(x, N//2)` 有何异同？

> **答案**：若平移量恰好是整数，两者在「循环」意义上结果一致（不考虑数值误差）。区别在于 `fourier_shift` 经过 FFT 相位旋转，对非整数平移也能给出 sinc 插值结果；而 `np.roll` 只接受整数、是纯整数循环移位。

---

### 4.3 输出数组与 dtype 提升：_get_output_fourier / _get_output_fourier_complex

#### 4.3.1 概念说明

u1-l4 讲过通用的 `_ni_support._get_output`，本模块有**两个专用变体**。它们做的事类似——根据 `output` 是 `None`/dtype/已有数组三种情况准备输出——但策略不同：

- `_get_output_fourier`：给前三个滤波函数用。允许实数输入 → 实数输出（因为乘法因子是实数），所以接受 `float32/float64/complex64/complex128`。
- `_get_output_fourier_complex`：只给 `fourier_shift` 用。**强制**输出为复数，只接受 `complex64/complex128`，其它类型会被提升为 `complex128`。

#### 4.3.2 核心流程

两个函数都是三分支 `if/elif`：

1. `output is None`：新建数组。`_fourier` 版按输入 dtype 决定（complex64/128/float32 保持，否则用 float64）；`_complex` 版强制 complex（输入是复数则保持，否则升 complex128）。
2. `type(output) is type`（即传入了 dtype 类）：校验是否在允许集合内，再按该 dtype 新建。
3. 否则（传入已有数组）：校验形状一致，直接复用。

#### 4.3.3 源码精读

[`_get_output_fourier`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_fourier.py#L40-L53)（[_fourier.py:40-53](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_fourier.py#L40-L53)）：注意 `None` 分支里，输入是 `complex64/complex128/float32` 时**保持原 dtype**，其它（含 int）一律提升到 `float64`；dtype 分支只允许 4 种浮点/复数类型，否则 `raise RuntimeError("output type not supported")`。

[`_get_output_fourier_complex`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_fourier.py#L56-L68)（[_fourier.py:56-68](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_fourier.py#L56-L68)）：`None` 分支只在输入是 `complex64/complex128` 时保持，否则升 `complex128`；dtype 分支只允许两种复数类型。

这两个函数没有用 u1-l4 讲的通用 `_get_output`，因为频域对 dtype 的要求更窄（只允许 4 种 / 2 种），需要专门的校验逻辑——这是「专用变体」存在的理由。

#### 4.3.4 代码实践

**实践目标**：观察两种输出策略对 dtype 的不同处理。

**操作步骤**（示例代码）：

```python
import numpy as np
from scipy.ndimage import _fourier

real_in  = np.array([1, 2, 3, 4], dtype=np.float64)
cplx_in  = np.array([1+2j, 3+4j], dtype=np.complex128)

# _get_output_fourier：实数输入 -> float64 输出
o1 = _fourier._get_output_fourier(None, real_in)
o2 = _fourier._get_output_fourier(None, cplx_in)
print("fourier on real  ->", o1.dtype)   # float64
print("fourier on cplx  ->", o2.dtype)   # complex128

# _get_output_fourier_complex：实数输入也被提升为复数
o3 = _fourier._get_output_fourier_complex(None, real_in)
print("complex on real  ->", o3.dtype)   # complex128
```

**需要观察的现象**：同一个实数输入，`_fourier` 变体给 `float64`，`_complex` 变体给 `complex128`。

**预期结果**：如上注释所示。

**若无法运行**：标注「待本地验证」。（注：直接从 `scipy.ndimage._fourier` 导入私有函数仅为教学演示，正式代码请用公开 API。）

#### 4.3.5 小练习与答案

**练习 1**：给 `fourier_gaussian` 传入一个 `int` 数组，输出会是什么 dtype？为什么？

> **答案**：`_get_output_fourier(None, int_input)` 走 `None` 分支，int 不在 `[complex64, complex128, float32]` 列表里，所以提升到 `float64`。这与「频域乘法因子是实数，结果存为实数最省空间」的设计一致。

**练习 2**：如果给 `fourier_shift` 传 `output=np.zeros(..., dtype=np.float64)`，会发生什么？

> **答案**：`_get_output_fourier_complex` 的 dtype 分支会判定 `float64` 不在 `[complex64, complex128]` 中，抛 `RuntimeError("output type not supported")`。因为 `fourier_shift` 必然产生复数，无法写入实数数组。

---

## 5. 综合实践

把本讲的三个要点（频域乘法核、`n`/`axis` 语义、循环边界 vs 空域边界）串成一个完整任务。

**任务**：对一段含两个频率分量的 1D 信号，分别用「频域高斯」和「空域高斯」做平滑，比较它们在内部和边界的行为，并尝试用 `fourier_shift` 把信号做半个样本的亚像素平移。

```python
import numpy as np
from scipy import ndimage

N = 128
t = np.arange(N)
# 低频 + 高频两个分量
x = np.cos(2*np.pi*2*t/N) + 0.5*np.cos(2*np.pi*20*t/N)

sigma = 3.0

# 路线 A：频域高斯（复 FFT，循环卷积）
X  = np.fft.fft(x)
Y  = ndimage.fourier_gaussian(X, sigma=sigma, n=-1, axis=-1)
yA = np.fft.ifft(Y).real

# 路线 B：空域高斯（默认 reflect 边界）
yB = ndimage.gaussian_filter1d(x, sigma=sigma)

# 比较
inner = np.abs(yA[10:-10] - yB[10:-10]).max()
edge  = np.abs(yA[:10]    - yB[:10]).max()
print(f"内部最大差: {inner:.3e}   边界最大差: {edge:.3e}")

# 附加：亚像素平移 0.5 样本
Xs = np.fft.fft(x)
Ys = ndimage.fourier_shift(Xs, 0.5, n=-1, axis=-1)
ys = np.fft.ifft(Ys).real
print("平移后中心区域是否平滑:", np.allclose(np.diff(ys[60:68]), np.diff(ys[60:68])[::-1][::-1]))
```

**请解释**：

1. 为什么内部差异很小、边界差异较大？（提示：循环卷积 vs 反射边界）
2. 高频分量（第 20 个频率）在平滑后明显衰减了吗？这与「高斯是低通」一致吗？
3. 亚像素平移 0.5 后，信号峰值位置应落在哪两个整数样本之间？

**预期结论**：内部吻合、边界偏离；高频被压制（低通）；平移后峰位落在原峰位 +0.5 处。这些把本讲三个模块的知识点连成了一条线。若无法运行，标注「待本地验证」。

---

## 6. 本讲小结

- `fourier_gaussian` / `fourier_uniform` / `fourier_ellipsoid` 是同一个 C 内核 `NI_FourierFilter` 的三件外套，靠整数 `filter_type`（0/1/2）分派核形状；它们**不做 FFT**，只假定输入已是 FFT 结果，做一次逐元素乘法。
- 乘法核的形状分别是高斯、sinc（盒子）、球/盘的傅里叶变换（椭球）；高斯与盒子核可分离（各轴表相乘），椭球核不可分离（按维度用径向公式 1D sinc / 2D 贝塞尔 / 3D 球贝塞尔）。
- 参数 `n`/`axis` 区分输入来源：`n<0` 表示复 FFT（默认）；`n>=0` 表示沿 `axis` 做过实 FFT（rfft），此时该轴逻辑长度用原始长度 `n`。频点排布严格匹配 NumPy 的 fftfreq 顺序。
- `fourier_shift` 走另一个 C 内核 `NI_FourierShift`，给每个频点乘相位因子 \(e^{-2\pi i\,\text{shift}\,k/N}\)，实现**亚像素**且**循环**的平移；它必然输出复数。
- `_get_output_fourier`（允许实数输出）与 `_get_output_fourier_complex`（强制复数）是两个专用输出策略，前者给三个滤波函数、后者只给 `fourier_shift`。
- 频域滤波 = 循环卷积，因此与空域滤波（默认 `reflect` 边界）在边界处行为不同——这是把频域与空域结果对照时最常踩的坑。

---

## 7. 下一步学习建议

- **向插值单元过渡**：本讲的 `fourier_shift` 是「频域平移」，而 u3 单元会讲空域的 `shift` / `affine_transform` / `map_coordinates`——它们用样条插值实现平移与几何变换，带完整的边界模式。学完 u3 后，建议回来对比「频域相位平移」与「空域样条平移」在亚像素精度和边界伪影上的差异。
- **下探 C 内核**：若想看清 `NI_FourierFilter` 里 `NI_Iterator` 和 fftfreq 频点顺序的实现，可读 u6-l2（C 端迭代器与点迭代），那里会讲 `NI_InitPointIterator` / `NI_ITERATOR_NEXT2` 的多维遍历机制。
- ** FFT 上层 API**：本讲假定你会用 `np.fft.fft`/`ifft`。如果想系统了解 SciPy 自己的 FFT（`scipy.fft`），那是另一个子包，可作为后续独立学习方向。
- **建议继续阅读的源码**：把 [`src/ni_fourier.c`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/ni_fourier.c) 的 `NI_FourierFilter` 主循环与 `_bessel_j1` 实现通读一遍，确认你对「椭球核 2D 用贝塞尔、3D 用球贝塞尔」的理解。
