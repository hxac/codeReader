# numpy.fft 项目定位与快速上手

## 1. 本讲目标

本讲是整套 `numpy.fft` 学习手册的第一篇。读完本讲，你应当能够：

- 说清楚 `numpy.fft` 是什么、它在 NumPy 与 SciPy 生态中扮演什么角色；
- 写出第一段 FFT 代码：用 `np.fft.fft` 把一个信号变换到频域，再用 `np.fft.ifft` 还原回来；
- 理解 `numpy.fft.__all__` 是怎么由 `_pocketfft` 与 `_helper` 两个子模块的导出「拼」出来的。

本讲只要求你「跑起来、看懂入口」，不会深入 `_raw_fft` 的内部实现——那是后续讲义的内容。

---

## 2. 前置知识

在开始之前，你只需要具备以下几点：

1. **会一点点 Python 与 NumPy**：知道 `import numpy as np`，能创建数组、做加减乘除。
2. **听过「傅里叶变换」这个词**。不必会推导公式。直觉上只要理解一句话即可：

   > 任何一个（足够好的）信号，都可以拆成若干个不同频率的正弦波的叠加；傅里叶变换就是「把信号从『随时间变化』换到『由哪些频率组成』」的工具。

3. **离散 vs 连续**。电脑里存的信号都是离散的（一串采样点）。对离散信号做的傅里叶变换叫 **离散傅里叶变换（Discrete Fourier Transform, DFT）**。

4. **FFT 是什么**。直接按定义算 DFT 很慢；有一种叫 **快速傅里叶变换（Fast Fourier Transform, FFT）** 的算法能在 \(O(n\log n)\) 时间内算完。`numpy.fft` 这个名字里的 "fft" 指的就是这套快速算法。

术语对照表：

| 术语 | 含义 |
|------|------|
| DFT | 离散傅里叶变换，一个数学定义 |
| FFT | 计算 DFT 的快速算法 |
| 时域（time domain） | 信号随时间变化的样子，即原始采样点 |
| 频域（frequency domain） | 信号由哪些频率组成，即变换后的结果（也叫「频谱」spectrum） |

---

## 3. 本讲源码地图

`numpy.fft` 是 NumPy 仓库里 `numpy/fft/` 目录下的一个子包。本讲涉及的关键文件只有两个，外加一个用于对照的 helper 文件：

| 文件 | 作用 | 本讲是否精读 |
|------|------|--------------|
| [`__init__.py`](__init__.py) | 子包入口。包含整段教学性 docstring，并负责把两个子模块的导出合并成 `numpy.fft.__all__` | ✅ 精读 |
| [`_pocketfft.py`](_pocketfft.py) | 所有变换（`fft`/`ifft`/`rfft`/...）的 Python 主逻辑。本讲只看 `fft`/`ifft` 两个公开函数 | ✅ 精读 |
| [`_helper.py`](_helper.py) | 频率箱与频谱平移的纯 Python helper（`fftfreq`、`fftshift` 等）。本讲只用它的 `__all__` 来做对照 | 仅对照 |

一个重要的事实：`_pocketfft.py` 里真正的「重计算」并不在 Python 里完成，而是交给了 C++ 扩展模块 `_pocketfft_umath`（源码见同目录下的 `_pocketfft_umath.cpp`）。本讲只会点到为止，告诉你「计算最终落在 C++」，具体机制留到后面的专家层讲义。

---

## 4. 核心概念与源码讲解

### 4.1 numpy.fft 是什么：在 NumPy 生态中的定位

#### 4.1.1 概念说明

很多初学者第一次接触 FFT 时会困惑：到底该用 `numpy.fft` 还是 `scipy.fft`？

答案是：**`numpy.fft` 只提供「基础款」的傅里叶变换**，是 `scipy.fft` 的一个子集。`scipy.fft` 功能更全（更多变换类型、更多后端、更精细的控制），但 `numpy.fft` 胜在「随 NumPy 自带、无需额外依赖」。

这个定位在 `__init__.py` 的 docstring 开头就写得很明白。它也是我们理解「为什么 `numpy.fft` 的函数列表这么短」的关键。

#### 4.1.2 核心流程

从使用者角度看，`numpy.fft` 的定位可以概括为一句话流程：

```
你要做傅里叶变换
   └─ 只需要「标准、基础」的正反变换、实数变换、频率箱、频谱平移？
         ├─ 是 → 用 numpy.fft（自带、够用）
         └─ 否（需要 worker 后端、多维分块、更丰富的归一化等）→ 用 scipy.fft
```

`numpy.fft` 把它的能力分成四大类（这也是 docstring 里 `autosummary` 的分组）：

1. **Standard FFTs**：`fft`、`ifft`、`fft2`、`ifft2`、`fftn`、`ifftn`（复数输入，复数输出）。
2. **Real FFTs**：`rfft`、`irfft`、`rfft2`、`irfft2`、`rfftn`、`irfftn`（实数输入，利用对称性省一半输出）。
3. **Hermitian FFTs**：`hfft`、`ihfft`（频域实、时域 Hermitian 对称的情形）。
4. **Helper routines**：`fftfreq`、`rfftfreq`、`fftshift`、`ifftshift`（配合上面变换使用的辅助函数）。

本讲只动手用第 1 类里的 `fft`/`ifft`。

#### 4.1.3 源码精读

定位的「权威出处」就在 docstring 的开头两行：

[\_\_init\_\_.py:L7-L8](__init__.py#L7-L8) — 这里明确写明：SciPy 的 `scipy.fft` 是 `numpy.fft` 的「更完整的超集（superset）」，而 `numpy.fft` 只包含「一组基础例程（a basic set of routines）」。

这段话直接回答了「`numpy.fft` 与 `scipy.fft` 是什么关系」：

- `scipy.fft` ⊃ `numpy.fft`（功能上）；
- `numpy.fft` 的设计目标是「够用、自带、零额外依赖」。

docstring 里还给出了本实现采用的 DFT 数学定义（后续讲义会逐字拆解，这里先混个眼熟）：

[\_\_init\_\_.py:L86-L88](__init__.py#L86-L88) — 正向 DFT 定义为

\[
A_k = \sum_{m=0}^{n-1} a_m \exp\left\{-2\pi i\,\frac{mk}{n}\right\},\qquad k=0,\ldots,n-1.
\]

[\_\_init\_\_.py:L117-L119](__init__.py#L117-L119) — 反向 DFT（IDFT）定义为

\[
a_m = \frac{1}{n}\sum_{k=0}^{n-1}A_k\exp\left\{2\pi i\,\frac{mk}{n}\right\},\qquad m=0,\ldots,n-1.
\]

注意两点（本讲只需记住结论）：

- 正变换指数是负号 \(-2\pi i\)，反变换是正号 \(+2\pi i\)；
- 反变换默认多了一个 \(\frac{1}{n}\) 的归一化，所以「先 `fft` 再 `ifft`」会还原回原信号。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `ifft(fft(a)) ≈ a`，建立「正反变换互逆」的直觉。

**操作步骤**：

```python
# 示例代码：第一次使用 numpy.fft
import numpy as np

N = 8                              # 采样点数
t = np.arange(N)                   # 时间轴 [0,1,2,...,7]
a = np.sin(2 * np.pi * t / N)      # 一个周期正好填满 N 个点的正弦波

A = np.fft.fft(a)                  # 正变换：时域 -> 频域
a_back = np.fft.ifft(A)            # 反变换：频域 -> 时域

print("原始信号  :", np.round(a, 4))
print("还原信号  :", np.round(a_back.real, 4))
print("最大误差  :", np.max(np.abs(a - a_back)))
print("是否还原  :", np.allclose(a, a_back))
```

**需要观察的现象**：

- `A` 是复数数组（即使输入 `a` 是实数），长度仍是 `N`。
- `a_back` 也是复数数组，但其虚部接近 0，实部接近原始 `a`。
- 「最大误差」是一个极小的数（约 \(10^{-16}\) 量级），「是否还原」打印 `True`。

**预期结果**：`np.allclose(a, a_back)` 返回 `True`。这不是巧合，而是源码里写死的约定——`ifft` 的 docstring 里就直说了 `ifft(fft(a)) == a`（在数值精度范围内）。精确到小数位的输出**待本地验证**（取决于你的 numpy 版本与平台），但「近似还原」这一结论是稳定的。

#### 4.1.5 小练习与答案

**练习 1**：如果把上面例子里的 `N` 从 8 改成 16，`np.allclose(a, a_back)` 还会是 `True` 吗？

**参考答案**：仍然是 `True`。正反变换的互逆性与点数无关（只要 `fft` 和 `ifft` 用相同的 `N`）。改变 `N` 只会改变频谱的分辨率，不会破坏还原性。

**练习 2**：为什么 `A = np.fft.fft(a)` 的结果是复数，哪怕 `a` 全是实数？

**参考答案**：因为 DFT 的定义里含有复指数 \(\exp(-2\pi i\,mk/n)\)，它本身是复数。实数输入的频谱虽然具有「共轭对称」性质（后续讲义会讲），但每个频率分量本身仍是复数，所以输出数组是复数 dtype。

---

### 4.2 fft / ifft：最基本的正反变换

#### 4.2.1 概念说明

`fft` 和 `ifft` 是 `numpy.fft` 里最核心的两个函数，也是后面所有变体（`rfft`、`fftn`、`hfft`……）的基础。它们都是**一维**变换：

- `fft(a)`：把数组 `a` 沿某一个轴做正向 DFT，时域 → 频域；
- `ifft(a)`：做反向 DFT，频域 → 时域。

它们的参数签名几乎一样：

```python
fft(a, n=None, axis=-1, norm=None, out=None)
ifft(a, n=None, axis=-1, norm=None, out=None)
```

本讲只关注前三个参数，`norm` 和 `out` 留到后续讲义：

| 参数 | 含义 | 默认值 |
|------|------|--------|
| `a` | 输入数组，可以是列表、可以是复数 | （必填） |
| `n` | 输出在变换轴上的长度。比输入短就截断，比输入长就补零 | `None`（= 输入长度） |
| `axis` | 沿哪条轴做变换 | `-1`（最后一条轴） |

#### 4.2.2 核心流程

`fft`/`ifft` 在 Python 层非常薄，真正的计算被委托出去。调用链如下：

```
np.fft.fft(a)
  │
  ▼
_pocketfft.fft(a, n=None, axis=-1, norm=None, out=None)
  │   ① a = asarray(a)            # 把列表等输入转成 ndarray
  │   ② n = a.shape[axis]         # n 没传就用该轴长度
  ▼
_raw_fft(a, n, axis, is_real=False, is_forward=True, norm, out)
  │   ③ 选后端 ufunc = pfu.fft    # pfu 就是 C++ 扩展 _pocketfft_umath
  ▼
pfu.fft(a, fct, axes=[...], out=out)   # 真正的 FFT 计算（C++）
```

`ifft` 的链路几乎相同，区别只在两处标志位：

- `fft` 调用 `_raw_fft(..., is_real=False, is_forward=True, ...)`；
- `ifft` 调用 `_raw_fft(..., is_real=False, is_forward=False, ...)`。

在 `_raw_fft` 内部，正是这两个布尔标志决定了选用哪个后端 ufunc。

#### 4.2.3 源码精读

先看公开函数 `fft` 的定义与函数体：

[_pocketfft.py:L121](_pocketfft.py#L121) — `def fft(a, n=None, axis=-1, norm=None, out=None):`，签名里 `n`、`axis`、`norm`、`out` 都有默认值。

[_pocketfft.py:L212-L216](_pocketfft.py#L212-L216) — `fft` 的真正函数体只有 4 行，核心是转成数组 + 调 `_raw_fft`：

```python
a = asarray(a)
if n is None:
    n = a.shape[axis]
output = _raw_fft(a, n, axis, False, True, norm, out)
return output
```

读法：

- `asarray(a)`：让你既能传 `np.ndarray`，也能传 Python 列表（如 `np.fft.fft([1, 2, 3, 4])`）。
- `n is None` 时取 `a.shape[axis]`：这就是「不传 `n` 就用输入长度」的实现。
- `False, True` 分别对应 `is_real=False`（输入不是「纯实数专用」路径）、`is_forward=True`（正向）。

再看 `ifft` 的函数体，结构完全对称：

[_pocketfft.py:L317-L321](_pocketfft.py#L317-L321) — `ifft` 同样 4 行，唯一不同是传给 `_raw_fft` 的第二个布尔是 `False`（`is_forward=False`）：

```python
a = asarray(a)
if n is None:
    n = a.shape[axis]
output = _raw_fft(a, n, axis, False, False, norm, out=out)
return output
```

那么 `_raw_fft` 怎么用这两个布尔选后端？看这一行就够了：

[_pocketfft.py:L86](_pocketfft.py#L86) — 复数（`is_real=False`）分支里：`ufunc = pfu.fft if is_forward else pfu.ifft`。也就是说，正向走 C++ 的 `pfu.fft`，反向走 `pfu.ifft`。

这里的 `pfu` 是什么？看文件顶部的导入：

[_pocketfft.py:L48](_pocketfft.py#L48) — `from . import _pocketfft_umath as pfu`。`pfu` 就是同目录下、由 C++ 编译出来的扩展模块 `_pocketfft_umath`。所以本讲的结论是：**`fft`/`ifft` 在 Python 层只做「参数整理 + 选后端」，真正的 FFT 数学计算发生在 C++ 扩展里**。

> 关于 `norm`：`fft` 签名默认 `norm=None`，而在 [_pocketfft.py:L68](_pocketfft.py#L68) 里 `norm is None` 与 `norm == "backward"` 走同一个分支（`fct = 1`）。所以「不传 `norm`」和「传 `norm="backward"`」完全等价——这一点 docstring 里也有说明（`None` 是 `"backward"` 的别名）。具体三种归一化的差异留到后续讲义。

#### 4.2.4 代码实践

**实践目标**：体会 `n` 参数的「截断 / 补零」效果，并对照频谱看变化。

**操作步骤**：

```python
# 示例代码：观察 n 参数的影响
import numpy as np

a = np.array([1.0, 2.0, 3.0, 4.0])   # 长度 4 的实信号

A_default = np.fft.fft(a)             # n=None，用输入长度 4
A_short   = np.fft.fft(a, n=2)        # n=2，先截断成 [1,2] 再变换
A_long    = np.fft.fft(a, n=8)        # n=8，先补 4 个零再变换

print("默认  :", A_default.shape, np.round(A_default, 3))
print("截断  :", A_short.shape,   np.round(A_short, 3))
print("补零  :", A_long.shape,    np.round(A_long, 3))
```

**需要观察的现象**：

- `A_default.shape == (4,)`、`A_short.shape == (2,)`、`A_long.shape == (8,)`——输出长度永远等于你指定的 `n`。
- 「截断」相当于丢掉了 `a` 的后半段，所以 `A_short` 其实就是 `fft([1, 2])`。
- 「补零」不会改变信号里包含的频率，但会让频谱采样得更密（更多频率点）。

**预期结果**：三个输出的 `shape` 分别是 `(4,)`、`(2,)`、`(8,)`；其中 `A_short` 与单独执行 `np.fft.fft([1.0, 2.0])` 完全一致。具体的复数值**待本地验证**，但形状与「截断=丢后半段」的行为是确定的。

**思考（不必运行）**：既然 `ifft` 是 `fft` 的逆，那么 `np.fft.ifft(A_long)` 还能还原成原来的 `[1,2,3,4]` 吗？

> 提示：补零是「先补再变换」，反变换会还原成「补零后的 8 点信号」，其前 4 个点才是原始 `[1,2,3,4]`，后 4 个点接近 0。

#### 4.2.5 小练习与答案

**练习 1**：调用 `np.fft.fft([1, 2, 3, 4])`（传列表而不是 ndarray）能成功吗？为什么？

**参考答案**：能成功。因为 `fft` 函数体第一行就是 `a = asarray(a)`，它会把 Python 列表转换成 NumPy 数组。这就是「`array_like` 输入」含义的实现。

**练习 2**：`fft` 和 `ifft` 在 Python 层的函数体几乎一模一样，唯一差别是什么？这个差别如何影响最终调用的 C++ 后端？

**参考答案**：唯一差别是传给 `_raw_fft` 的 `is_forward` 布尔：`fft` 传 `True`，`ifft` 传 `False`。在 `_raw_fft` 内部，`ufunc = pfu.fft if is_forward else pfu.ifft`（[_pocketfft.py:L86](_pocketfft.py#L86)）据此选用正向或反向的 C++ ufunc。

**练习 3**：如果不传 `n`，`fft` 怎么决定输出长度？

**参考答案**：见 [_pocketfft.py:L213-L214](_pocketfft.py#L213-L214)，`n is None` 时取 `n = a.shape[axis]`，即沿变换轴的输入长度。

---

### 4.3 \_\_all\_\_ 如何聚合 _pocketfft 与 _helper 的导出

#### 4.3.1 概念说明

当你 `import numpy.fft` 之后，能直接用 `np.fft.fft`、`np.fft.fftfreq`、`np.fft.fftshift`……这些函数并不是凭空出现的，而是 `numpy/fft/__init__.py` 这个入口文件「收集」进来的。

关键机制是 Python 的两个约定：

1. `from <模块> import *`：把目标模块里**所有在 `__all__` 中列出**的名字导入当前命名空间。
2. `__all__`：一个模块的「公开 API 清单」。`from module import *` 只会导入清单里的名字；`__all__` 也决定了 `help()`、文档工具、IDE 自动补全展示哪些名字。

`numpy.fft` 的设计是：**变换函数（`fft` 等）住在 `_pocketfft.py`，helper 函数（`fftfreq` 等）住在 `_helper.py`，入口 `__init__.py` 把两边的 `__all__` 拼成一份总的 `numpy.fft.__all__`。**

#### 4.3.2 核心流程

```
入口 __init__.py 的「导出装配」流程：

  from ._pocketfft import *   ──▶  导入 14 个变换函数
  from ._helper    import *   ──▶  导入 4 个 helper 函数

  __all__ = _pocketfft.__all__.copy()   # 先放变换函数（14 个）
  __all__ += _helper.__all__            # 再追加 helper（4 个）

  最终 numpy.fft.__all__ 长度 = 18，顺序：变换在前、helper 在后
```

注意 `test`（测试入口函数）虽然也被加进了命名空间，但它**不在** `__all__` 里，所以 `from numpy.fft import *` 不会带走它。

#### 4.3.3 源码精读

先看两个子模块各自声明的 `__all__`：

[_pocketfft.py:L30-L31](_pocketfft.py#L30-L31) — 变换函数清单，共 14 个：

```python
__all__ = ['fft', 'ifft', 'rfft', 'irfft', 'hfft', 'ihfft', 'rfftn',
           'irfftn', 'rfft2', 'irfft2', 'fft2', 'ifft2', 'fftn', 'ifftn']
```

[_helper.py:L10](_helper.py#L10) — helper 清单，共 4 个：

```python
__all__ = ['fftshift', 'ifftshift', 'fftfreq', 'rfftfreq']
```

再看入口文件如何把它们装配起来：

[\_\_init\_\_.py:L203-L208](__init__.py#L203-L208) — 这是整个子包「导出聚合」的核心 6 行：

```python
from . import _helper, _pocketfft
from ._helper import *
from ._pocketfft import *

__all__ = _pocketfft.__all__.copy()  # noqa: PLE0605
__all__ += _helper.__all__
```

逐行读：

- 第 203 行 `from . import _helper, _pocketfft`：导入两个子模块对象本身（这样后面才能写 `_pocketfft.__all__`）。
- 第 204、205 行 `from ._helper import *` / `from ._pocketfft import *`：按各自 `__all__` 把名字搬进 `numpy.fft` 命名空间——这就是为什么 `np.fft.fft`、`np.fft.fftfreq` 能直接用。
- 第 207 行 `__all__ = _pocketfft.__all__.copy()`：用 `.copy()` 而不是直接赋值，是为了**避免修改 `_pocketfft` 模块自己的 `__all__`**（`+=` 是原地操作，不拷贝就会污染源模块）。`# noqa: PLE0605` 是告诉 linter「这里故意用 `+=` 给 `__all__` 追加，不要报警」。
- 第 208 行 `__all__ += _helper.__all__`：把 helper 的 4 个名字追加到末尾。

[\_\_init\_\_.py:L210-L213](__init__.py#L210-L213) — 额外注册了一个 `test` 函数（`PytestTester`），让你能用 `numpy.fft.test()` 跑这个子包的测试。它不在 `__all__` 里，所以 `import *` 不会导出它。

#### 4.3.4 代码实践

**实践目标**：亲手打印 `numpy.fft.__all__`，并按「来源子模块」给它分组。

**操作步骤**：

```python
# 示例代码：解析 numpy.fft 的公开 API
import numpy.fft

all_names = numpy.fft.__all__

# 两个子模块各自的清单（与源码 _pocketfft.py / _helper.py 完全一致）
from_pocketfft = {
    'fft', 'ifft', 'rfft', 'irfft', 'hfft', 'ihfft', 'rfftn',
    'irfftn', 'rfft2', 'irfft2', 'fft2', 'ifft2', 'fftn', 'ifftn',
}
from_helper = {'fftshift', 'ifftshift', 'fftfreq', 'rfftfreq'}

print("numpy.fft.__all__ 共", len(all_names), "个")
print("顺序 :", all_names)
print()
print("来自 _pocketfft :", [n for n in all_names if n in from_pocketfft])
print("来自 _helper    :", [n for n in all_names if n in from_helper])
print("其它（应为空）  :", [n for n in all_names
                          if n not in from_pocketfft and n not in from_helper])
```

**需要观察的现象**：

- `len(all_names)` 应为 **18**（14 + 4）。
- `all_names` 的顺序是「先 14 个变换函数（按 `_pocketfft.__all__` 的顺序），再 4 个 helper」。
- 「来自 _pocketfft」「来自 _helper」两组加起来正好 18 个，「其它」这一组为空。
- `numpy.fft.test` 虽然存在（`hasattr(numpy.fft, 'test')` 为 `True`），但不在 `__all__` 里。

**预期结果**：`len(numpy.fft.__all__) == 18`；前 14 个元素与 [_pocketfft.py:L30-L31](_pocketfft.py#L30-L31) 完全一致，后 4 个元素与 [_helper.py:L10](_helper.py#L10) 完全一致。这是源码装配方式的直接推论，结论稳定；若你的 NumPy 版本与讲义不同，清单可能略有出入（**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：为什么第 207 行用 `_pocketfft.__all__.copy()`，而不是直接写 `__all__ = _pocketfft.__all__`？

**参考答案**：因为下一行 `__all__ += _helper.__all__` 会原地修改 `__all__`。如果直接赋值，`numpy.fft.__all__` 和 `_pocketfft.__all__` 会指向同一个列表对象，`+=` 就会污染 `_pocketfft` 模块自己的 `__all__`，把 helper 的名字也塞进去。`.copy()` 先复制一份，避免这种副作用。

**练习 2**：执行 `from numpy.fft import *` 之后，`test` 这个名字会被导入吗？为什么？

**参考答案**：不会。`from module import *` 只导入 `module.__all__` 中列出的名字，而 `test` 不在 `numpy.fft.__all__` 里（它由 [\_\_init\_\_.py:L212](__init__.py#L212) 单独绑定）。要想用 `test`，必须显式写 `import numpy.fft; numpy.fft.test()`。

**练习 3**：如果你给 `_helper.py` 增加一个新函数并加进它的 `__all__`，`numpy.fft` 这边需要改动吗？

**参考答案**：不需要。因为 `__init__.py` 用的是 `__all__ += _helper.__all__`，它自动反映 `_helper.__all__` 的最新内容。这正是「聚合式 `__all__`」设计的好处——子模块增删公开函数时，入口文件无需同步维护。

---

## 5. 综合实践

把本讲的三个最小模块串起来，完成下面这个「迷你频谱分析」任务。

**任务描述**：构造一个由两个正弦波叠加而成的信号，用 `numpy.fft` 完成正向变换、从频谱里「读出」这两个频率、再用反变换把信号还原，最后打印 `numpy.fft.__all__` 并按来源分组。

**操作步骤**：

```python
# 示例代码：迷你频谱分析（综合实践）
import numpy as np
import numpy.fft

# ---- 1. 构造信号：两个正弦波叠加 ----
fs = 800                       # 采样率 800 Hz
N  = 800                       # 采样点数（bin 宽 = fs/N = 1 Hz，便于定位频率）
t  = np.arange(N) / fs         # 时间轴（秒）
f1, f2 = 50.0, 80.0            # 两个频率
a  = 1.0 * np.sin(2 * np.pi * f1 * t) + 1.5 * np.sin(2 * np.pi * f2 * t)

# ---- 2. 正向变换 + 还原 ----
A       = np.fft.fft(a)        # 时域 -> 频域
a_back  = np.fft.ifft(A)       # 频域 -> 时域
print("还原正确 ?", np.allclose(a, a_back))

# ---- 3. 从幅度谱里「读出」频率 ----
mag   = np.abs(A)                       # 幅度谱
freqs = np.arange(N) * (fs / N)         # 简易正频率轴（0 ~ fs）
# 实信号频谱关于中点对称，只看前一半
top = np.argsort(mag[:N // 2])[-2:][::-1]
print("最强两个频率(Hz) :", sorted(freqs[top]))   # 预期接近 50 和 80

# ---- 4. 打印 __all__ 并分组 ----
helper = {'fftshift', 'ifftshift', 'fftfreq', 'rfftfreq'}
print("总 API 个数 :", len(numpy.fft.__all__))
print("变换函数   :", [n for n in numpy.fft.__all__ if n not in helper])
print("helper     :", [n for n in numpy.fft.__all__ if n in helper])
```

**需要观察的现象与预期结果**：

1. `还原正确 ?` 打印 `True`——验证 `ifft(fft(a)) ≈ a`（呼应模块 4.1/4.2）。
2. 「最强两个频率」打印出接近 `[50.0, 80.0]` 的两个值——因为信号恰由 50 Hz 和 80 Hz 两个正弦波组成，幅度谱会在对应的频率箱（bin 50 和 bin 80，因为 bin 宽 1 Hz）出现尖峰（呼应模块 4.2 的「频域」直觉）。
3. 「总 API 个数」为 18，「变换函数」14 个、「helper」4 个——验证 `__all__` 的聚合方式（呼应模块 4.3）。

> 说明：第 2 步的具体数值会因浮点与版本略有差异，若 `top` 取到相邻 bin，可改用「寻找局部极大值」或直接看 `mag[50]`、`mag[80]` 是否显著大于其它点来确认。这部分**待本地验证**；但「还原正确」与「API 分组」两条结论是稳定的。

---

## 6. 本讲小结

- `numpy.fft` 是 NumPy 自带的**基础款**傅里叶变换子包，定位上是 `scipy.fft` 的一个子集（权威出处：[\_\_init\_\_.py:L7-L8](__init__.py#L7-L8)）。
- 它的能力分四类：Standard FFTs、Real FFTs、Hermitian FFTs、Helper routines；本讲只动手用了 Standard 类里的 `fft`/`ifft`。
- `fft`/`ifft` 在 Python 层很薄：转成数组、决定长度 `n`，然后调用统一入口 `_raw_fft`（[_pocketfft.py:L212-L216](_pocketfft.py#L212-L216) 与 [_pocketfft.py:L317-L321](_pocketfft.py#L317-L321)），真正的 FFT 计算由 C++ 扩展 `_pocketfft_umath` 完成。
- `n` 参数控制输出长度：比输入短则截断、比输入长则补零；不传则取该轴长度。
- 入口 `__init__.py` 用 `from ._pocketfft import *` 和 `from ._helper import *` 收集导出，并把两边 `__all__` 拼成总共 18 个名字的 `numpy.fft.__all__`（[\_\_init\_\_.py:L203-L208](__init__.py#L203-L208)）。
- `ifft(fft(a)) ≈ a` 是本实现的约定，源于 DFT/IDFT 定义里指数符号相反且反变换带 \(1/n\) 归一化。

---

## 7. 下一步学习建议

本讲只「跑起来、看懂入口」。接下来的学习顺序建议如下（对应大纲里的后续讲义）：

1. **补齐目录与构建全貌**：阅读讲义 *u1-l2 目录结构与各文件职责*、*u1-l3 构建系统与 pocketfft 后端依赖*，搞清楚 `_pocketfft_umath.cpp` 是怎么被 Meson 编译成扩展的。
2. **补齐数学约定与 helper**：阅读 *u2 数学约定与频率 helper* 系列讲义，尤其是 DFT 的 "standard" 频率排列、`fftfreq`/`rfftfreq`、`fftshift`/`ifftshift`——它们是「看懂频谱」的必备工具。
3. **打通一维核心流程**：阅读 *u3-l1 fft/ifft 与 _raw_fft 主流程*，届时我们会回到 [_pocketfft.py:L58](_pocketfft.py#L58) 的 `_raw_fft`，把它内部如何选后端 ufunc、如何处理 `n_out`、如何分配输出数组，一行行讲透。

建议你顺手读一读下面两段源码作为热身：[`__init__.py` 的整段 docstring](__init__.py#L1-L201)（最好的「实现约定」说明书）和 [`_pocketfft.py` 顶部 1~51 行](_pocketfft.py#L1-L51)（模块级 docstring + 导入 + `__all__`）。
