# scipy.signal 是什么：定位与能力全景

## 1. 本讲目标

本讲是整个 `scipy.signal` 学习手册的第一篇。读完本讲，你应该能够：

- 说清楚 `scipy.signal` 在 SciPy 生态中的角色——它是一个面向**数字信号处理（Digital Signal Processing, DSP）**的子包。
- 认识它对外暴露的**功能分组全景**（卷积、滤波、滤波器设计、LTI 系统、频谱分析、峰值检测、窗函数、波形等）。
- 理解它的公开 API 是如何从一份**模块文档字符串**一步步组织起来的。
- 了解它的**历史来源**（SIGTOOLS 模块）与设计目标。

本篇刻意不深入任何一个算法，目的是先给你一张「地图」，让你在后续每一篇讲义中都知道自己在地图的哪个位置。

## 2. 前置知识

在开始之前，建议你大致了解以下概念（不需要精通，有印象即可）：

- **信号（signal）**：随时间或空间变化的数值序列，例如一段录音、一张图像的某一行像素、传感器采样数据。
- **采样（sampling）**：把连续的物理信号按固定时间间隔取值，变成离散序列。采样率（Hz）决定了每秒取多少个点。
- **卷积（convolution）与相关（correlation）**：把一个信号与一个「核」滑动相乘求和的运算，是滤波、模板匹配的基础。
- **滤波（filtering）**：去除信号中某些频率成分，例如「去掉低频漂移」「保留 20–2000Hz 的语音」。
- **频谱（spectrum）**：信号在「频率域」的能量分布，通过傅里叶变换得到。
- **Python 与 NumPy**：会用 `import`、数组、`numpy.fft` 即可。

如果你对「频率」「傅里叶变换」完全陌生，也不用担心——本讲只做能力概览，不要求会算。

## 3. 本讲源码地图

本讲只涉及两个文件，它们正好回答「`scipy.signal` 是什么」这个问题：

| 文件 | 作用 |
| --- | --- |
| [`__init__.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/__init__.py) | 子包的入口文件。它的**模块文档字符串**是整个子包的「能力目录」；文件末尾几行决定了哪些名字会被暴露为公开 API。 |
| [`docs/signaltools.README`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/docs/signaltools.README) | 一份 1999 年留下来的历史说明，记录了这个子包最初叫 **SIGTOOLS**，由 4 个 C 例程起步。 |

> 提示：在阅读后续每一篇讲义时，你都会回到 `__init__.py` 的文档字符串，因为它是「能力目录」的真相来源。

## 4. 核心概念与源码讲解

### 4.1 scipy.signal 的定位与生态角色

#### 4.1.1 概念说明

`scipy.signal` 是 SciPy 的一个**子包（subpackage）**，专门用于**数字信号处理**。它处理的对象是**离散的数值序列**（NumPy 数组），典型应用包括：

- 对一段录音做**降噪 / 带通滤波**；
- 在心电图、地震波里**找峰值**；
- 估计一段振动信号的**功率谱**；
- 设计一个**数字滤波器**并把它应用到数据上；
- 在两个信号之间做**卷积或相关**（如图像模板匹配）。

它在 SciPy 生态中的位置可以这样理解：

- **NumPy**：提供数组与最基础的 `numpy.fft`、`numpy.convolve`。
- **`scipy.signal`**：在 NumPy 之上，提供**专业级、面向信号**的工具（多速率重采样、滤波器设计、谱估计、STFT 等）。
- **`scipy.fftpack` / `scipy.fft`**：提供傅里叶变换本身（`scipy.signal` 内部会调用它）。
- **`scipy.ndimage`**：面向**图像/多维数组**的处理，与 `scipy.signal` 在 2D 滤波上有部分重叠但定位不同。

一句话：**当你处理「一维或多维、带时间/频率含义的序列」时，第一个该想到的就是 `scipy.signal`。**

#### 4.1.2 核心流程

一个典型的使用流程只有两步：

1. `import scipy.signal as signal`。
2. 调用某个具体函数，如 `signal.welch(x)`、`signal.find_peaks(x)`、`signal.filtfilt(b, a, x)`。

从源码角度看，这两步背后是一条「命名空间暴露链」：

```text
实现模块（私有，带 _ 前缀）
      │
      ▼
_signal_api.py        ← 把「裸 API」聚合起来
      │
      ▼
_support_alternative_backends.py  ← 装饰 / 委托到 CuPy/JAX 等后端
      │
      ▼
__init__.py           ← 对外暴露为 scipy.signal.*
```

这条链的**完整细节**会在 [u1-l4 公共命名空间与 API 导出链路](#) 专讲；本讲你只需知道：**`__init__.py` 是这条链的终点，也是用户看到的入口**。

#### 4.1.3 源码精读

打开 `__init__.py`，第一眼就是一段非常长的**模块文档字符串**。它的开头这样写：

[__init__.py:L1-L11](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/__init__.py#L1-L11)：这段文档字符串以 reStructuredText 标题 `Signal processing (:mod:`scipy.signal`)` 起头，紧接着就是按功能分组列出的「能力目录」。

文档字符串之外，文件**真正的可执行代码**非常短。核心就是这几行：

[__init__.py:L316-L319](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/__init__.py#L316-L319)：把所有公开功能从 `_support_alternative_backends` 一次性导入，并把它的 `__all__` 直接当作 `scipy.signal` 的 `__all__`。注意第 319 行 `del` 掉了 `_signal_api`、`_delegators` 等内部模块名，说明它们只是中转用的「脚手架」，不应出现在公开命名空间里。

```python
from ._support_alternative_backends import *
from . import _support_alternative_backends
__all__ = _support_alternative_backends.__all__
del _support_alternative_backends, _signal_api, _delegators  # noqa: F821
```

也就是说：**用户能看到的 `signal.xxx`，本质就是 `_support_alternative_backends.__all__` 里列出的那些名字。**

#### 4.1.4 代码实践

**实践目标**：确认本机安装的 SciPy 能正常导入 `scipy.signal`，并感受它的公开对象规模。

**操作步骤**：

1. 打开一个 Python 交互环境（`python` 或 IPython）。
2. 运行下面的「示例代码」。

```python
# 示例代码：导入子包并枚举公开对象
import scipy.signal as signal

public = [name for name in dir(signal) if not name.startswith('_')]
print("公开对象数量：", len(public))

# 抽查几个本讲提到的关键函数是否真的存在
for fn in ['convolve', 'welch', 'find_peaks', 'butter', 'filtfilt', 'get_window']:
    print(f"{fn:12s} -> ", hasattr(signal, fn))
```

**需要观察的现象**：脚本不应抛 `ImportError`；几个函数名都应打印 `True`。

**预期结果**：公开对象数量在数百级别（函数 + 类 + 常量）。**具体数值随 SciPy 版本变化，待本地验证。**

> 注意：本讲中给出的「示例代码」并非项目自带脚本，仅用于本地探索，请勿写入子包目录。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `import scipy.signal` 之后，`signal._signal_api` 这个名字通常访问不到（会报 `AttributeError`）？

**参考答案**：因为 [`__init__.py:L319`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/__init__.py#L319) 末尾用 `del` 显式删除了 `_signal_api`、`_delegators` 等中转脚手架名，它们只用于内部聚合，不对外暴露。

**练习 2**：如果有人说「`scipy.signal` 只是一堆 NumPy 函数的包装」，这句话对吗？

**参考答案**：不完全对。它确实建立在 NumPy 之上，但额外提供了大量 NumPy 没有的专业能力（滤波器设计、SOS 级联、谱估计、STFT、峰值突出度、CZT 等），并且在性能关键路径上使用了 C/Cython/Pythran 扩展（详见 u1-l3）。

---

### 4.2 功能能力全景：从文档字符串看 12 个功能分组

#### 4.2.1 概念说明

`scipy.signal` 涉及的函数有上百个，直接看 `dir(signal)` 会让人眼花。好在 `__init__.py` 的文档字符串已经把它们**按领域分组**了。每个分组对应 DSP 的一个子问题。认识这 12 个分组，等于拿到了整本手册的「目录」。

#### 4.2.2 核心流程

下面这张表是本讲的「核心地图」。左列是文档字符串里的分组标题，右列是该分组要解决的 DSP 子问题，每个分组各举一个代表性函数：

| 分组 | 解决的问题 | 代表函数 |
| --- | --- | --- |
| Convolution（卷积/相关） | 两个信号的滑动乘加 | `convolve` / `correlate` |
| B-splines（B 样条） | 样条插值与平滑 | `cspline1d` |
| Filtering（滤波） | 把滤波器作用到数据上 | `lfilter` / `filtfilt` |
| Filter design（滤波器设计） | 计算滤波器系数 | `iirfilter` |
| Matlab-style IIR filter design | 一行设计经典 IIR | `butter` |
| Linear Systems（LTI 系统） | 建模与分析线性系统 | `lti` / `step` |
| LTI representations（表示转换） | tf/zpk/sos/ss 互转 | `tf2zpk` |
| Waveforms（波形） | 生成测试/合成信号 | `chirp` / `sawtooth` |
| Window functions（窗函数） | 生成各类窗 | `get_window` |
| Peak finding（峰值检测） | 找信号里的峰 | `find_peaks` |
| Spectral analysis（谱分析） | 估计功率谱/时频谱 | `welch` / `spectrogram` |
| Chirp Z-transform & Zoom FFT | 在 z 平面螺旋采样 / 频段细化 | `czt` / `zoom_fft` |

你可以把这张表与后续讲义的对应关系记住：例如「滤波」对应单元 4、「滤波器设计」对应单元 5、「LTI 系统」对应单元 6、「谱分析」对应单元 7、「峰值检测」对应单元 8、「CZT / 样条」对应单元 9。

#### 4.2.3 源码精读

文档字符串里，每个分组都是一段形如下面的 reStructuredText 区块：

[__init__.py:L11-L25](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/__init__.py#L11-L25)：这是 **Convolution** 分组。`.. autosummary::` 是 Sphinx 文档工具的指令，它会让构建系统为列表中的每个函数自动生成一份 API 参考页。每个函数名后面跟的 `--` 短语就是它的「一句话用途」。

```rst
Convolution
===========

.. autosummary::
   :toctree: generated/

   convolve           -- N-D convolution.
   correlate          -- N-D correlation.
   fftconvolve        -- N-D convolution using the FFT.
   ...
```

再看几个有代表性的分组：

- [__init__.py:L43-L78](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/__init__.py#L43-L78)：**Filtering** 分组，包含本手册单元 4 的全部主角（`lfilter`、`filtfilt`、`sosfilt`、`hilbert`、`decimate`、`resample_poly` 等）。
- [__init__.py:L151-L168](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/__init__.py#L151-L168)：**Matlab-style IIR filter design** 分组，列出了经典五大族 `butter/cheby1/cheby2/ellip/bessel` 及阶数选择函数，加上二阶专用滤波器 `iirnotch/iirpeak/iircomb`。
- [__init__.py:L230-L242](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/__init__.py#L230-L242)：**Waveforms** 分组，`chirp/gausspulse/max_len_seq/sawtooth/square/sweep_poly/unit_impulse`，全是「生成信号」的工具。
- [__init__.py:L257-L269](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/__init__.py#L257-L269)：**Peak finding** 分组，`argrelmin/argrelmax/find_peaks/find_peaks_cwt/peak_prominences/peak_widths`。
- [__init__.py:L271-L292](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/__init__.py#L271-L292)：**Spectral analysis** 分组，注意这里同时保留了旧接口（`stft/istft/spectrogram/check_COLA`）和新接口（`ShortTimeFFT/closest_STFT_dual_window/check_NOLA`）。

> 关键认识：这份文档字符串**既是给人看的说明，也是给 Sphinx 看的生成指令**，所以它能长期和真实代码保持一致——这也是它适合作为「能力目录」的原因。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：阅读 `__init__.py` 顶部的功能分组清单，写出 `scipy.signal` 提供的功能类别，每个类别各举一个代表性函数名，并用一句话说明其用途。

**操作步骤**：

1. 打开 [`__init__.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/__init__.py)，定位文档字符串中的 12 个分组标题（每个标题下都有一条 `====` 下划线）。
2. 从中**任选 8 个类别**（任务要求 8 个），为每个类别挑一个代表性函数。
3. 自己用一句话写出该函数的用途（先不要抄文档里的 `--` 注释，写完再对照）。
4. 用下面的「示例代码」核对你选的函数确实存在于命名空间中。

```python
# 示例代码：验证你挑选的 8 个代表函数是否都在 scipy.signal 中
import scipy.signal as signal

my_picks = [
    'convolve',   # 卷积/相关
    'lfilter',    # 滤波
    'butter',     # Matlab 式 IIR 设计
    'lti',        # LTI 系统
    'chirp',      # 波形
    'get_window', # 窗函数
    'find_peaks', # 峰值检测
    'welch',      # 谱分析
]
for fn in my_picks:
    assert hasattr(signal, fn), f"{fn} 不存在！"
    print("OK:", fn)
```

**需要观察的现象**：8 个断言全部通过，没有任何 `AssertionError`。

**预期结果**：以上 8 个函数都属于「真实存在」的公开 API，断言应全部通过。若你替换成别的函数名，请确保它出现在文档字符串的某个分组里。

#### 4.2.5 小练习与答案

**练习 1**：`butter` 出现在哪个分组？为什么它和 `iirfilter` 不在同一个分组里？

**参考答案**：`butter` 出现在 **Matlab-style IIR filter design** 分组（[L157](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/__init__.py#L157)）；`iirfilter` 出现在更底层的 **Filter design** 分组（[L107](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/__init__.py#L107)）。原因是 `butter` 这类「便捷函数」其实在内部调用 `iirfilter`，文档把它们分开是为了区分「用户友好的一行式接口」与「通用底层后端」。这条调用链会在 u5-l1 详细展开。

**练习 2**：`ShortTimeFFT` 和 `stft/istft` 都在 Spectral analysis 分组里，这暗示了什么？

**参考答案**：暗示新旧两套短时傅里叶接口并存。`ShortTimeFFT` 是较新的类式接口，`stft/istft/spectrogram` 是旧式函数接口（部分被标注为 legacy）。两者的取舍会在 u7-l3 / u7-l4 专门讲解。

---

### 4.3 模块文档字符串如何驱动 API 暴露

#### 4.3.1 概念说明

在 Python 包里，「文档字符串」和「公开 API 名单」通常是两件事：前者是人读的说明，后者由 `__all__` 决定。但 `scipy.signal` 的设计有一个值得注意的特点：**文档字符串是能力目录的「真相来源」，而 `__all__` 由更下层的中转模块统一提供**。理解这一点，能帮你解释很多「为什么这个名字能用 / 那个名字会触发弃用警告」的现象。

#### 4.3.2 核心流程

公开 API 是这样一层层「接力」出来的：

1. 各**私有实现模块**（`_waveforms.py`、`_filter_design.py`、`_signaltools.py` 等，都带 `_` 前缀）定义真正的函数。
2. [`_signal_api.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signal_api.py) 把这些「裸 API」聚合，并维护一份 `__all__`。
3. [`_support_alternative_backends.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_support_alternative_backends.py) 用装饰器把它们包装一遍（支持委托到 CuPy / JAX 等其他数组后端）。
4. [`__init__.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/__init__.py) 用 `from ... import *` 把最终结果暴露为 `scipy.signal.*`。

> 这条链的**完整细节**是 u1-l4 的主题，这里你只需记住结论：`scipy.signal` 的 `__all__` 来自 `_support_alternative_backends.__all__`。

#### 4.3.3 源码精读

[__init__.py:L312-L319](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/__init__.py#L312-L319)：注意第 312 行的注释 `# bring in the public functionality from private namespaces`（把公开功能从私有命名空间引进来）——这句话精确概括了整条链的设计意图：**实现写在私有模块里，公开名字在最后一步才聚拢**。

[__init__.py:L322-L326](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/__init__.py#L322-L326)：这里还导入了一批**已弃用的 stub 模块**（`bsplines`、`filter_design`、`fir_filter_design`、`lti_conversion`、`ltisys`、`spectral`、`signaltools`、`waveforms`、`wavelets`、`spline`）。注释明确写着 `to be removed in v2.0.0`。它们的作用是：**让老代码里的 `scipy.signal.signaltools.xxx` 仍能运行（但会触发 `DeprecationWarning`），从而平滑迁移**。

```python
# Deprecated namespaces, to be removed in v2.0.0
from . import (
    bsplines, filter_design, fir_filter_design, lti_conversion, ltisys,
    spectral, signaltools, waveforms, wavelets, spline
)
```

> 这就是为什么你会看到「实现文件」叫 `_signaltools.py`（私有、带 `_`），而「旧入口」叫 `signaltools.py`（无 `_`、是 stub）。两者在 u1-l2 / u1-l4 会详细对比。

#### 4.3.4 代码实践

**实践目标**：验证「公开 API 名单 = `_support_alternative_backends.__all__`」这一结论。

**操作步骤**：

1. 运行下面的「示例代码」，比对 `scipy.signal.__all__` 与文档字符串里出现的函数名。
2. 观察哪些名字在 `__all__` 里却没在文档字符串里出现（或反之）。

```python
# 示例代码：查看公开 API 名单的来源
import scipy.signal as signal

# __all__ 就是最终对外暴露的名单
print("公开 API 数量：", len(signal.__all__))
print("前 10 个：", signal.__all__[:10])
```

**需要观察的现象**：`__all__` 是一个字符串列表，里面包含文档字符串各分组列出的函数名（如 `convolve`、`welch`）。

**预期结果**：`__all__` 长度与 4.1.4 中 `len(public)` 数量级一致（但可能略小，因为 `public` 还包含类、模块等不带 `_` 的对象）。**精确数值待本地验证。**

#### 4.3.5 小练习与答案

**练习 1**：有人写 `from scipy.signal.signaltools import convolve`。这条语句能不能成功？会有什么副作用？

**参考答案**：能成功，但会触发 `DeprecationWarning`。因为 `signaltools` 是 [L323-L326](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/__init__.py#L323-L326) 列出的弃用 stub，它会把访问重定向到私有实现模块 `_signaltools`，并提醒用户改用 `scipy.signal.convolve`。该 stub 计划在 v2.0.0 移除。

**练习 2**：为什么 `__init__.py` 选择 `__all__ = _support_alternative_backends.__all__`，而不是自己在文件里手写一份名单？

**参考答案**：因为函数在到达 `__init__` 之前要经过 `_signal_api` 聚合和 `_support_alternative_backends` 装饰（用于多后端委托）。如果在 `__init__` 手写名单，就需要和下层两处重复维护，容易漏掉新函数或漏装饰。把 `__all__` 的权威来源放在装饰之后的那一层，能保证「被暴露的名字 = 已被正确装饰的名字」。

---

### 4.4 历史源流：从 SIGTOOLS 到 scipy.signal

#### 4.4.1 概念说明

`scipy.signal` 并不是凭空设计的，它脱胎于 Travis Oliphant（NumPy 的核心作者之一）在 **1999 年**编写的 **SIGTOOLS** 模块。理解这段历史，能帮你解释两个现象：

- 为什么有些函数名（如 `signaltools`）今天还在以「stub」形式存在；
- 为什么子包里同时混用了 C、Cython、Python 三种语言——因为「速度关键路径用 C」是从第一天就定下的原则。

#### 4.4.2 核心流程

README 里给出的演变时间线：

```text
1999-02-05  第一版，只含 convolveND（N-D 卷积）
1999-02-08  0.20 版，加入 order_filterND（排序滤波）
1999-02-23  0.40 版，加入 linear_filter（1-D 线性滤波）和 remez（FIR 最优设计）
1999-05-01  0.5.1 版，引入 _signaltools.py 模块
1999-07-10  0.5.2 版，并入 multipack；remez 代码改用 LGPL 版本
   ↓
今天       演化为 scipy.signal 子包，上百个函数 + 多种编译扩展
```

注意 `linear_filter` 这个名字——它就是今天 `lfilter` 的前身；`remeze`/`remez` 至今仍是 FIR 等纹波设计的入口。

#### 4.4.3 源码精读

[docs/signaltools.README:L1-L7](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/docs/signaltools.README#L1-L7)：开头点明这是 **SIGTOOLS module**，版权 2002 年归 Travis Oliphant，采用 SciPy 的 BSD 风格许可证。

[docs/signaltools.README:L9-L14](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/docs/signaltools.README#L9-L14)：作者表达了最初的设计目标——做成一套**通用的 1-D/2-D/N-D 滤波例程集合**，并定下了一条贯穿至今的工程原则：

> Most additional code to be added should probably be written in Python unless a specific need for speed is needed.
> （大多数新增代码应当用 Python 写，除非确实有速度需求。）

这正是今天子包里「Python 实现为主 + C/Cython/Pythran 在热点路径加速」格局的源头。

[docs/signaltools.README:L16-L45](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/docs/signaltools.README#L16-L45)：列出了起步时的 **4 个 C 例程**：

1. 通用 **N-D 相关**（不做 180° 翻转，零填充处理边界）——对应今天的 `correlate` / N-D 相关内核（u3-l4 会精读）。
2. 通用 **N-D 排序滤波**——对应今天的 `order_filter`。
3. **1-D 线性滤波**（替换 MATLAB 的 `filter`，支持 Direct Form II Transposed 初值）——对应今天的 `lfilter`（u4-l1）。
4. **remez 交换算法**（Parks-McClellan 最优 FIR 设计）——对应今天的 `remez`（u5-l5）。

[docs/signaltools.README:L47-L51](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/docs/signaltools.README#L47-L51)：当时用 Python 实现的额外例程只有 3 个：`convolveND`、`wiener`、`medfilt`。其中 `wiener` 和 `medfilt` 这两个名字一直保留到了今天（见 u4-l7）。

[docs/signaltools.README:L76-L93](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/docs/signaltools.README#L76-L93)：CHANGELOG 记录了从 1999 年 2 月到 7 月的早期版本演进。

#### 4.4.4 代码实践

**实践目标**：做一次「源码阅读型实践」——验证 1999 年的 SIGTOOLS 例程在今天的 `scipy.signal` 中是否依然存在。

**操作步骤**：

1. 阅读 [docs/signaltools.README:L16-L51](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/docs/signaltools.README#L16-L51)，记下 4 个 C 例程与 3 个 Python 例程。
2. 在 [`__init__.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/__init__.py) 的文档字符串里搜索它们今天的名字。
3. 填写下面这张对照表（右列自行补全）：

| 1999 SIGTOOLS 例程 | 今天的 `scipy.signal` 函数 | 所在分组 |
| --- | --- | --- |
| N-D correlation（C） | `correlate` | Convolution |
| N-D order-filter（C） | `order_filter` | Filtering |
| 1-D linear_filter（C） | `lfilter` | Filtering |
| remez（C） | `remez` | Filter design |
| convolveND（Python） | `convolve` | Convolution |
| wiener（Python） | `wiener` | Filtering |
| medfilt（Python） | `medfilt` | Filtering |

**需要观察的现象**：7 个老例程的名字（或其直接后继）都能在今天的文档字符串里找到，说明 API 具备很强的向后兼容性。

**预期结果**：右列函数全部能在 `scipy.signal` 命名空间中找到（可用 4.2.4 的 `hasattr` 方式核对）。

> 这条「源码阅读型实践」不需要运行任何命令，只需要交叉阅读两份文件，目的是让你体会「历史 → 今天」的命名延续。

#### 4.4.5 小练习与答案

**练习 1**：README 说早期 N-D 相关例程「performs no 180 degree flipping」（不做 180° 翻转）。这一点对应了 DSP 中「相关」与「卷积」的什么区别？

**参考答案**：数学上，**卷积**需要把核翻转 180°（反转下标），而**相关**不做翻转。所以「不翻转」的例程对应的是「相关」。这也解释了为什么今天 `scipy.signal` 同时提供 `convolve` 和 `correlate` 两个函数（u3-l1 会用代码验证它们的翻转差异）。

**练习 2**：README 里的设计原则「非热点用 Python、热点用 C」在今天体现在哪里？

**参考答案**：体现在子包同时拥有大量纯 Python 实现模块（如 `_waveforms.py`、`_filter_design.py`）与一批编译扩展（`_sigtools`、`_sosfilt`、`_upfirdn_apply`、`_peak_finding_utils`、`_max_len_seq_inner` 等）。前者负责功能广度与可读性，后者负责性能关键路径。具体哪些函数用了哪种扩展，是 u1-l3（构建）和 u10-l4（扩展全景）的主题。

---

## 5. 综合实践

把本讲的三条主线——**定位 / 能力分组 / 历史源流**——串起来，完成下面这个小任务：

**任务**：为 `scipy.signal` 制作一张「一页速查卡」。

要求：

1. 从 [`__init__.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/__init__.py) 的文档字符串里选出你认为**最常用的 6 个功能分组**。
2. 为每个分组挑**一个**代表函数，写出它的「一句话用途」（参考各函数名后的 `--` 注释，但用自己的话改写）。
3. 在代表函数中，**标注哪些可以追溯到 1999 年的 SIGTOOLS**（参照 [docs/signaltools.README](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/docs/signaltools.README)）。
4. 用一段话（不超过 80 字）总结 `scipy.signal` 是什么、解决什么问题。

**参考产出形态**（你可以用 Markdown 表格 + 一段总结完成）。完成后，这张速查卡将作为你阅读后续每一篇讲义时的「目录索引」。

## 6. 本讲小结

- `scipy.signal` 是 SciPy 中面向**数字信号处理**的子包，处理对象是离散数值序列（NumPy 数组）。
- 它的能力被组织成 **12 个功能分组**，从卷积/滤波到谱分析、峰值检测、CZT 一应俱全，整本手册的单元划分正是对应这些分组。
- 这份「能力目录」就写在 [`__init__.py` 的模块文档字符串](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/__init__.py#L1-L311) 里，同时被 Sphinx 用来自动生成 API 文档。
- 公开 API 的真正来源是 `__all__ = _support_alternative_backends.__all__`，实现则散落在带 `_` 前缀的私有模块中（这条链在 u1-l4 详讲）。
- 子包保留了 `signaltools` 等一批**弃用 stub 模块**以维持旧代码兼容，计划在 v2.0.0 移除。
- `scipy.signal` 的祖先是 1999 年 Travis Oliphant 的 **SIGTOOLS** 模块，「Python 为主、热点用 C」是它沿用至今的工程原则。

## 7. 下一步学习建议

本讲只看了「入口」和「历史」。下一步建议：

- 想理解目录结构与文件组织 → 继续学习 **u1-l2 源码目录结构与模块组织**，搞清楚 `_xxx` 私有模块、stub 模块、C/Cython 扩展各放在哪。
- 想理解编译扩展是怎么造出来的 → 学习 **u1-l3 构建方式：Meson 与编译扩展入门**。
- 想彻底搞懂「实现 → 聚合 → 装饰 → 暴露」那条链 → 学习 **u1-l4 公共命名空间与 API 导出链路**。
- 如果你想先「玩起来」，也可以暂时跳到 **u2-l1 常用波形生成**，亲手生成一段 `chirp` 扫频信号，等需要时再回头补 u1 的架构知识。
