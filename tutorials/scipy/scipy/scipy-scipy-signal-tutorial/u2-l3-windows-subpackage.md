# 窗函数子包与 get_window 调度

## 1. 本讲目标

本讲聚焦 `scipy.signal` 中一个"子包中的子包"——`windows`。学完后你应该能够：

- 说清 `scipy/signal/windows/` 目录下三个文件（`__init__.py`、`_windows.py`、`windows.py`）各自的角色，以及它们如何复用单元 1 讲过的"四层命名空间编织 + 弃用 stub"模式。
- 掌握 `get_window` 这个"统一调度器"的工作原理：它如何把字符串名、元组参数、甚至一个浮点数，分发到二十多种具体窗函数。
- 理解 `sym`（对称）参数的真正含义，它和 `get_window` 的 `fftbins` 参数为何是"反转关系"，以及为什么窗函数要区分"对称型"与"周期型"。

本讲承接 [u1-l4 公共命名空间与 API 导出链路](u1-l4-namespace-export-chain.md)：那里讲的是"链路规则"，这里用 `windows` 子包作为最完整的实例来印证。

## 2. 前置知识

### 2.1 什么是窗函数

很多信号处理任务（比如 FFT 频谱估计、FIR 滤波器设计）都要截取一段有限长度的信号。直接"硬截断"等价于乘上一个矩形窗，会在频域引入严重的**泄漏（spectral leakage）**——本该集中在某个频率的能量被"摊"到相邻频率上。

窗函数（window function）就是一段在两端平滑衰减到 0（或接近 0）的权重序列 \(w[n]\)，用逐点相乘的方式给信号"加窗"，以减轻截断带来的泄漏：

\[ x_w[n] = x[n] \cdot w[n] \]

常见的窗有 Hann、Hamming、Blackman、Kaiser 等，它们在**主瓣宽度**（频率分辨率）与**旁瓣电平**（泄漏大小）之间做不同的折中。这没有"最优"，只有"最适合当前任务"。

### 2.2 对称型窗 vs 周期型窗

这是一个初学者最容易忽略、却非常关键的区分：

- **对称窗（symmetric, `sym=True`）**：窗在序列两端严格对称，\(w[0] = w[M-1]\)。常用于 **FIR 滤波器设计**（线性相位要求窗对称）。
- **周期窗（periodic, `sym=False`）**：又称 DFT-even 窗。它是把对称窗的**最后一个样本丢弃**得到的，使得窗做"周期延拓"时首尾衔接处没有跳变。常用于 **FFT 频谱分析**（避免周期延拓时的相位跳变引入泄漏）。

记住这个直觉：**滤波设计用对称窗，谱分析用周期窗**。后文我们会看到 `get_window` 的 `fftbins` 参数正是用来切换这两种模式的。

### 2.3 你需要记住的前置结论

来自 [u1-l4](u1-l4-namespace-export-chain.md)：`scipy.signal` 的扁平 API 由一条四层接力流水线生成（私有实现 → `_signal_api` 聚合 → `_support_alternative_backends` 装饰 → `__init__` 暴露），并附带一条弃用侧链（`filter_design.py` 等 stub 模块经 `__getattr__` 转发到私有模块并发出 `DeprecationWarning`）。本讲你会看到 `windows` 子包把这两条机制完整地"缩小复刻"了一遍。

## 3. 本讲源码地图

| 文件 | 角色 | 行数级别 |
|------|------|----------|
| [`windows/__init__.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/windows/__init__.py) | 子包入口：暴露函数列表、导入弃用 stub | 50 行 |
| [`windows/_windows.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/windows/_windows.py) | **真正的实现**：二十多种窗函数 + `get_window` 调度器 | 约 2600 行 |
| [`windows/windows.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/windows/windows.py) | **弃用 stub**：仅 `__getattr__` 转发到 `_windows` | 24 行 |
| [`_signal_api.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signal_api.py) | 主子包聚合层：把 `windows` 子包与 `get_window` 接入 `scipy.signal` | 相关 2 行 |

一个一句话总结：`_windows.py` 是干活的人，`__init__.py` 是前台（正式入口），`windows.py` 是贴了"即将拆除"告示的旧侧门，`_signal_api.py` 把前台和其中一个最受欢迎的函数（`get_window`）额外拉到主楼大厅。

## 4. 核心概念与源码讲解

### 4.1 windows 子包的三层文件分工

#### 4.1.1 概念说明

`windows` 是 `scipy.signal` 内部唯一一个"子包中的子包"（其余都是单文件模块）。它几乎把单元 1 讲过的整个命名空间编织机制又复刻了一遍：

- 一个**私有实现文件** `_windows.py`（算法真正所在）；
- 一个**正式入口** `__init__.py`（对外暴露）；
- 一个**弃用 stub** `windows.py`（向后兼容旧导入路径）。

这种"三件套"结构和顶层 `signal/`（`_signaltools.py` + `__init__.py` + `signaltools.py`）如出一辙，只是规模更小、没有编译扩展（窗函数都是纯数值运算，无需 C 加速）。

#### 4.1.2 核心流程

`__init__.py` 的执行流程：

1. 读入模块文档字符串（驱动 Sphinx 文档生成）。
2. `from ._windows import *` —— 把所有窗函数实现拉进 `windows` 命名空间。
3. `from . import windows` —— 故意再导入一次 stub 模块 `windows.py`，使 `scipy.signal.windows.windows.hamming` 这种**旧的三层路径**仍然可用（但触发弃用警告）。
4. 定义 `__all__`，列出对外承诺的 26 个名字。

注意第 3 步是"自我引用"的关键：包名叫 `windows`，包内又有一个文件叫 `windows.py`，`from . import windows` 取的是这个文件而非包本身——这正是为了兼容历史上 `scipy.signal.windows.windows.xxx` 的写法。

#### 4.1.3 源码精读

先看 `__init__.py` 的核心三行：

```python
from ._windows import *        # 拉入实现

# Deprecated namespaces, to be removed in v2.0.0
from . import windows          # 兼容旧路径 windows.windows.xxx
```

[windows/__init__.py:42-45](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/windows/__init__.py#L42-L45) —— 这三行就是前台的全部工作。注释明确标注 `windows` 这个子导入是"待在 v2.0.0 移除的弃用命名空间"。

[windows/__init__.py:47-52](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/windows/__init__.py#L47-L52) —— `__all__` 显式列出 26 个公开名。与顶层 `_signal_api` 用 `dir()` 自动生成 `__all__` 不同，这里手写是因为 `from ._windows import *` 已经依赖 `_windows.py` 自己的 `__all__`，再手写一份是为了精确控制对外可见集合。

再看弃用 stub `windows.py`：

```python
# This file is not meant for public use and will be removed in SciPy v2.0.0.
from scipy._lib.deprecation import _sub_module_deprecation

__all__ = [...]  # 一长串窗函数名

def __dir__():
    return __all__

def __getattr__(name):
    return _sub_module_deprecation(sub_package="signal.windows", module="windows",
                                   private_modules=["_windows"], all=__all__,
                                   attribute=name)
```

[windows/windows.py:1-5](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/windows/windows.py#L1-L5) —— 文件头三行注释直接说明：此文件非公开用途，v2.0.0 移除，请改用 `scipy.signal.windows` 命名空间。

[windows/windows.py:20-23](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/windows/windows.py#L20-L23) —— 这就是 [u1-l4](u1-l4-namespace-export-chain.md) 讲过的"模块级 `__getattr__` 延迟重定向"。当有人写 `from scipy.signal.windows.windows import hamming` 时，Python 找不到 `hamming` 这个属性，就回调这个 `__getattr__(name='hamming')`，进而调用 `_sub_module_deprecation`：它发出 `DeprecationWarning`，然后从私有模块 `_windows` 取回真正的 `hamming` 函数并返回。关键参数 `private_modules=["_windows"]` 告诉重定向器"真正的实现在 `_windows.py` 里"。

> **与 u1-l4 的呼应**：`windows.py` 的 `__getattr__` 模板与顶层 `filter_design.py` 的 `__getattr__` 完全同构，只是 `private_modules` 指向的私有模块不同。这印证了单元 1 的结论——弃用侧链是一个**统一的机械模式**，在子包层面被原样复用。

最后看 `_signal_api.py` 如何把整个子包接入主命名空间：

```python
from . import _sigtools, windows         # 把 windows 子包挂到 signal 下
...
from .windows import get_window  # keep this one in signal namespace
```

[_signal_api.py:9](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signal_api.py#L9) —— `from . import windows` 使 `scipy.signal.windows` 成为可访问的子模块对象（于是 `scipy.signal.windows.hamming` 可用）。

[_signal_api.py:28](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signal_api.py#L28) —— 额外把 `get_window` **单独**提到 `scipy.signal` 顶层。行内注释 `# keep this one in signal namespace` 说明这是一个有意的决定：`get_window` 太常用了，值得让用户直接 `from scipy.signal import get_window`，而不必每次都写 `scipy.signal.windows.get_window`。

#### 4.1.4 代码实践

**实践目标**：亲手验证三层文件分工与弃用链。

**操作步骤**（在已安装 SciPy 的 Python 环境中）：

```python
import warnings
import scipy.signal as signal

# 1) 正式路径：从子包直接取
from scipy.signal.windows import hamming as h1

# 2) 顶层快捷路径：get_window 被提升到 signal 顶层
from scipy.signal import get_window   # 注意：不是 scipy.signal.windows.get_window

# 3) 弃用路径：捕获警告
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    from scipy.signal.windows.windows import hamming as h2
    print("是否触发 DeprecationWarning:", any(issubclass(x.category, DeprecationWarning) for x in w))
```

**需要观察的现象**：
- 三种方式取到的 `hamming` / `get_window` 都能用。
- 第 3 种方式会打印 `True`，并在 `w` 中看到一条 `DeprecationWarning`，正文提示你改用 `scipy.signal.windows` 命名空间。
- `h1` 与 `h2` 是同一个函数对象（`h1 is h2` 为真），因为 `h2` 经 `__getattr__` 重定向后取回的就是 `_windows.hamming`，而 `h1` 也是它。

**预期结果**：弃用路径仍能工作但发警告；正式路径与顶层路径无警告。这正对应 [u1-l4](u1-l4-namespace-export-chain.md) 总结的"扁平路径是官方推荐、stub 路径仅为兼容"。

#### 4.1.5 小练习与答案

**练习 1**：`windows/__init__.py` 里同时有 `from ._windows import *` 和 `from . import windows`，两者都含单词 "windows"，它们分别指什么？

**参考答案**：前者 `._windows` 指同目录下的私有实现文件 `_windows.py`（注意下划线前缀和 `.py`），目的是把窗函数实现拉入命名空间；后者 `.windows` 指同目录下的弃用 stub 文件 `windows.py`（无下划线），目的是让旧的 `windows.windows.xxx` 路径继续可用。两者名字相似但指向完全不同的文件。

**练习 2**：为什么 `windows.py` 里要定义 `__dir__()`？

**参考答案**：Python 的 `dir(模块)` 会调用模块的 `__dir__()`。stub 模块本身没有真正定义那些窗函数属性（它们都靠 `__getattr__` 延迟生成），若无 `__dir__()`，`dir()` 就列不出这些名字，影响自动补全与文档工具。显式返回 `__all__` 能让这些名字"看起来"存在于模块中。

---

### 4.2 get_window：按名字统一分发各类窗

#### 4.2.1 概念说明

`_windows.py` 里有二十多个具体的窗函数（`hamming`、`kaiser`、`blackman`……），每个都有自己的参数签名。如果用户每次都要先判断"我要哪种窗"、再去 import 对应函数、再记它的参数顺序，体验会很差。

`get_window` 就是一个**统一的调度器（dispatcher）**：你只要给它一个"窗的名字 + 长度"，它就替你查表找到正确的窗函数并调用。它的 `window` 参数接受三种形态：

| `window` 形态 | 含义 | 例子 |
|---------------|------|------|
| 字符串 `'name'` | 无参窗的名字（可带别名与后缀） | `'hamming'`、`'ham'` |
| 元组 `('name', p1, p2, ...)` | 带参窗的名字 + 参数 | `('kaiser', 8.0)`、`('tukey', 0.5)` |
| 浮点数 | 直接当作 Kaiser 窗的 `beta` | `4.0` 等价于 `('kaiser', 4.0)` |

这种"一个函数搞定所有窗"的设计，使得上层代码（如 `firwin`、`welch`）只需接受一个 `window` 字符串参数，再统一交给 `get_window` 解析，极大简化了接口。

#### 4.2.2 核心流程

`get_window` 的调度逻辑可拆为五步：

1. **校验** `Nx`（正整数）与 `fftbins`（布尔）。
2. **分支一：浮点数** —— 尝试 `float(window)`，成功则返回 `kaiser(Nx, beta)`。
3. **解析名字与对称性** —— 取元组首元素或字符串作为 `win_name`；检查是否带 `_symmetric` / `_periodic` 后缀，若有则据此覆盖 `sym` 并去掉后缀。
4. **查表** `_WIN_FUNCS[win_name]`，得到 `(func, has_args)`；校验参数数量是否匹配 `has_args`（`True` 必须有参、`False` 不能有参、`'OPTIONAL'` 皆可）。
5. **调用** —— 无参直接 `func(Nx, sym=sym)`；有参 `func(Nx, *args, sym=sym)`；对 `dpss`、`general_cosine` 做特殊处理。

其中第 4 步的"表"是本节重点。它由一个数据字典 `_WIN_FUNC_DATA` 在模块加载时展开成查找表 `_WIN_FUNCS`：

```python
_WIN_FUNC_DATA = {  # 格式: {(名字0, 别名1, ...): (函数, 是否需要参数)}
    ('hamming', 'hamm', 'ham'): (hamming, False),
    ('kaiser', 'ksr'):           (kaiser, True),
    ('tukey', 'tuk'):            (tukey, 'OPTIONAL'),
    # ... 共约 28 组
}
_WIN_FUNCS = dict()
for nn_, v_ in _WIN_FUNC_DATA.items():
    _WIN_FUNCS.update({n_: v_ for n_ in nn_})   # 把每个名字/别名都映射到同一项
```

这样 `get_window` 只需一次字典查找 `O(1)` 就完成"名字→函数"的解析，并且别名 `'ham'`、`'hamm'` 与正式名 `'hamming'` 自动等价。`has_args` 的三态设计则把"参数校验"也压进了表里，避免在 `get_window` 中写一长串 `if`。

#### 4.2.3 源码精读

先看分发表的数据与展开：

[windows/_windows.py:2359-2386](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/windows/_windows.py#L2359-L2386) —— `_WIN_FUNC_DATA` 是"权威清单"。注意几个细节：`('boxcar', 'box', 'ones', 'rect', 'rectangular')` 一个窗有五个别名；`('tukey', 'tuk'): (tukey, 'OPTIONAL')` 用字符串 `'OPTIONAL'` 表示参数可选；`('general cosine', 'general_cosine')` 的正式名里**带空格**（`'general cosine'`），这是为了让人写 `get_window(('general cosine', a), N)` 更像自然语言。

[windows/_windows.py:2387-2389](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/windows/_windows.py#L2387-L2389) —— 把"多别名一组"展平成"单名→单项"的扁平字典 `_WIN_FUNCS`。这一步在模块导入时只执行一次，之后所有 `get_window` 调用共享这张表。

再看 `get_window` 的解析主体：

```python
sym = not fftbins
win_name = window if isinstance(window, str) else window[0]
if win_name.endswith('_symmetric'):   # 后缀可覆盖 fftbins / sym
    sym, win_name = True, win_name[:-10]
elif win_name.endswith('_periodic'):
    sym, win_name = False, win_name[:-9]

if win_name not in _WIN_FUNCS:
    raise ValueError(f"Invalid window name '{win_name}' ...")

func, has_args = _WIN_FUNCS[win_name]
args = window[1:] if isinstance(window, tuple) else tuple()
```

[windows/_windows.py:2561-2572](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/windows/_windows.py#L2561-L2572) —— 这是调度的"心脏"。第一行 `sym = not fftbins` 确立了**反转关系**（详见 4.3 节）。随后处理后缀、查表、提取元组中的附加参数。

[windows/_windows.py:2573-2576](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/windows/_windows.py#L2573-L2576) —— 基于 `has_args` 的参数校验。`has_args is False` 却传了参数、或 `has_args is True` 却没传参数，都会抛 `ValueError`。`'OPTIONAL'` 则两种都允许。

[windows/_windows.py:2579-2593](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/windows/_windows.py#L2579-L2593) —— 最终调用。无参走 `func(Nx, sym=sym, ...)`；有参走 `func(Nx, *args, sym=sym, ...)`。`dpss` 与 `general_cosine` 因签名特殊被单独特判（注释说是"沿用原始实现"的历史特例）。注意所有调用都透传 `xp`/`device` 参数——这是 Array API 多后端支持（CuPy/JAX），与 [u1-l4](u1-l4-namespace-export-chain.md) 讲的 `_support_alternative_backends` 装饰体系配套。

而最开头的浮点分支：

[windows/_windows.py:2550-2556](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/windows/_windows.py#L2550-L2556) —— 如果 `window` 既不是字符串也不是元组，就尝试 `float(window)`；成功则当作 Kaiser 的 `beta`。这就是文档里 `get_window(4.0, 9)` 等价于 `get_window(('kaiser', 4.0), 9)` 的实现原因。

#### 4.2.4 代码实践

**实践目标**：用同一种调用风格获取多种窗，验证别名、元组、浮点三种形态。

**操作步骤**：

```python
import numpy as np
from scipy.signal import get_window

N = 9

# 形态 1：字符串（无参窗）
w_hann = get_window('hann', N)

# 形态 1b：别名 —— 'han' 应与 'hann' 完全相同
w_alias = get_window('han', N)
print("别名等价:", np.allclose(w_hann, w_alias))

# 形态 2：元组（带参窗）
w_kaiser = get_window(('kaiser', 8.0), N)

# 形态 3：浮点 —— 4.0 应等价于 ('kaiser', 4.0)
w_float  = get_window(4.0, N)
w_tuple  = get_window(('kaiser', 4.0), N)
print("浮点等价:", np.allclose(w_float, w_tuple))

# 非法：给无参窗传参数
try:
    get_window(('hamming', 3.0), N)
except ValueError as e:
    print("预期报错:", e)
```

**需要观察的现象**：
- 两个 `True`。
- 最后一条打印出形如 `'hamming' does not allow parameters, but window=('hamming', 3.0)!` 的报错——这正是 [windows/_windows.py:2573-2574](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/windows/_windows.py#L2573-L2574) 的校验在起作用。

**预期结果**：别名与浮点两种便利形态都正确解析；参数不匹配时给出清晰的 `ValueError`。

#### 4.2.5 小练习与答案

**练习 1**：`get_window('box', 8)`、`get_window('ones', 8)`、`get_window('rectangular', 8)` 三者结果是否相同？为什么？

**参考答案**：完全相同。因为在 `_WIN_FUNC_DATA` 中，`('boxcar', 'box', 'ones', 'rect', 'rectangular')` 这五个名字被合并成一组、展开后都指向同一个 `(boxcar, False)` 项（见 [windows/_windows.py:2365](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/windows/_windows.py#L2365)）。这是分发表"别名自动等价"设计的直接体现。

**练习 2**：为什么 `dpss` 和 `general_cosine` 在 `get_window` 末尾要被特殊处理，而不是走通用的 `func(Nx, *args, ...)` 分支？

**参考答案**：因为它们的签名与通用模式不兼容。`dpss` 需要把第一个参数固定到 `Kmax` 位置（`dpss(Nx, args[0], Kmax=None, ...)`），而 `general_cosine` 不接受 `xp`/`device` 参数（见 [windows/_windows.py:2583-2591](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/windows/_windows.py#L2583-L2591)）。源码注释明确说这些是"沿用原始实现"的历史特例。

---

### 4.3 sym 参数与 fftbins 的反转关系

#### 4.3.1 概念说明

回顾 2.2 节：窗分**对称型**（`sym=True`，用于滤波设计）和**周期型**（`sym=False`，用于谱分析）。每个具体窗函数都用 `sym` 参数来切换，默认 `sym=True`。

但 `get_window` 没有直接暴露 `sym`，而是用了一个含义相反的参数 `fftbins`：

- `fftbins=True`（默认）→ 生成**周期窗**（`sym=False`）；
- `fftbins=False` → 生成**对称窗**（`sym=True`）。

两者是**逻辑取反**关系：`sym = not fftbins`。这个反转让初学者很困惑，但它有历史原因：`get_window` 默认服务于 FFT（谱分析），所以默认就是周期窗；而底层窗函数默认服务于滤波设计，所以默认是对称窗。两套默认值"恰好相反"，于是参数也设计成相反含义以保持各自的默认行为合理。

此外，`get_window` 还支持在窗名后加 `_symmetric` 或 `_periodic` 后缀来**强制覆盖** `fftbins`，这给用户一个"名字里就写清楚"的更显式的选择。

#### 4.3.2 核心流程

周期窗的实现采用一个经典技巧（DFT-even 约定）：

1. 若想要长度为 `M` 的周期窗（`sym=False`），先在内部把长度**加 1** 变成 `M+1`。
2. 用对称公式生成 `M+1` 个点。
3. 把**最后一个点丢弃**，得到 `M` 个点。

数学上，对称窗满足 \(w[n] = w[M-n]\)（\(n=0,\dots,M-1\)，共 \(M\) 点，两端相等）。周期窗则是取对称窗的前 \(M-1\) 点（即 \(n=0,\dots,M-2\)）后再丢弃末点，使得周期延拓时无缝衔接。用伪代码表达这一"加一再去尾"流程：

```
生成对称窗 w_sym (长度 M+1)   # w_sym[0] == w_sym[M]
周期窗 w_per = w_sym[:-1]     # 长度 M，丢弃 w_sym[M]
```

这套"加一/去尾"由两个内部辅助函数 `_extend` 与 `_truncate` 完成，所有窗函数共用。

#### 4.3.3 源码精读

先看反转关系本身：

[windows/_windows.py:2561](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/windows/_windows.py#L2561) —— `sym = not fftbins`。这一行确立了 `fftbins` 与 `sym` 的取反关系，是理解两套默认值的关键。

再看后缀覆盖：

```python
if win_name.endswith('_symmetric'):
    sym, win_name = True, win_name[:-10]    # 去掉 '_symmetric'(10 字符)
elif win_name.endswith('_periodic'):
    sym, win_name = False, win_name[:-9]    # 去掉 '_periodic'(9 字符)
```

[windows/_windows.py:2563-2566](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/windows/_windows.py#L2563-L2566) —— 如果名字带后缀，就**覆盖**前面由 `fftbins` 决定的 `sym`，并把后缀从名字里去掉（否则查表会失败）。所以 `get_window('bartlett_periodic', 4, fftbins=False)` 仍得到周期窗——后缀优先级高于 `fftbins` 参数。

然后看"加一/去尾"辅助函数：

```python
def _extend(M, sym):
    """Extend window by 1 sample if needed for DFT-even symmetry"""
    if not sym:            # 周期窗：内部多算一个点
        return M + 1, True
    else:                  # 对称窗：长度不变
        return M, False

def _truncate(w, needed):
    """Truncate window by 1 sample if needed for DFT-even symmetry"""
    if needed:
        return w[:-1]      # 丢掉最后一个样本
    else:
        return w
```

[windows/_windows.py:30-43](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/windows/_windows.py#L30-L43) —— 这就是周期窗"加一再去尾"的全部实现。`_extend` 返回的第二个值 `needs_trunc` 会被传给 `_truncate`，形成"对称生成、按需裁剪"的统一模板。

以 `general_cosine` 的实现为例看模板如何套用：

```python
def _general_cosine_impl(M, a, xp, device, sym=True):
    if _len_guards(M):
        return xp.ones(M, ...)
    M, needs_trunc = _extend(M, sym)        # 步骤 1：按需加一
    fac = xp.linspace(-xp.pi, xp.pi, M, ...)
    w = xp.zeros(M, ...)
    for k in range(a.shape[0]):
        w += a[k] * xp.cos(k * fac)         # 步骤 2：对称公式生成
    return _truncate(w, needs_trunc)        # 步骤 3：按需去尾
```

[windows/_windows.py:55-65](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/windows/_windows.py#L55-L65) —— `hamming`、`hann`、`blackman` 等广义余弦窗族都通过这套"三步模板"同时支持 `sym=True/False`。`_len_guards`（[windows/_windows.py:23-27](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/windows/_windows.py#L23-L27)）则处理 `M` 为 0 或 1 的退化情况，避免 `linspace` 出错。

#### 4.3.4 代码实践

**实践目标**：直观对比对称窗与周期窗的差异，验证"周期窗 = 对称窗去尾"。

**操作步骤**：

```python
import numpy as np
from scipy.signal import get_window
from scipy.signal.windows import bartlett

# 方式 A：用底层函数的 sym 参数
w_sym = bartlett(5, sym=True)      # 对称窗，长度 5
w_per = bartlett(4, sym=False)     # 周期窗，长度 4

# 方式 B：用 get_window 的 fftbins 参数（注意取反！）
gw_sym = get_window('bartlett', 5, fftbins=False)   # fftbins=False -> 对称
gw_per = get_window('bartlett', 4)                   # fftbins=True  -> 周期

# 方式 C：用后缀显式指定
suf_per = get_window('bartlett_periodic', 4, fftbins=False)  # 后缀覆盖参数

print("对称窗 (底层)       :", w_sym)
print("对称窗 (get_window) :", gw_sym, " 一致:", np.allclose(w_sym, gw_sym))
print("周期窗 (底层)       :", w_per)
print("周期窗 (get_window) :", gw_per, " 一致:", np.allclose(w_per, gw_per))
print("后缀覆盖 fftbins    :", suf_per,  " 仍为周期:", np.allclose(suf_per, gw_per))

# 验证"周期窗 = 长度+1 的对称窗去尾"
w_sym6 = bartlett(5, sym=True)  # 长度 5 对称
print("周期窗 == 对称窗[:-1]?", np.allclose(gw_per, bartlett(5, sym=True)[:-1]))
```

**需要观察的现象**：
- `bartlett(5, sym=True)` 给出 `[0, 0.5, 1, 0.5, 0]`（两端为 0，严格对称）。
- `get_window('bartlett', 4)` 给出 `[0, 0.5, 1, 0.5]`（周期窗，末尾的 0 被丢弃）。
- `get_window('bartlett', 5, fftbins=False)` 与 `bartlett(5, sym=True)` 完全相同——印证 `sym = not fftbins`。
- 即便给 `get_window('bartlett_periodic', 4, fftbins=False)`，结果仍是周期窗——印证后缀优先。

**预期结果**：四种等价关系全部成立。这组对照能让你彻底搞清 `sym` 与 `fftbins` 的反转，以及后缀的覆盖语义。

#### 4.3.5 小练习与答案

**练习 1**：假设你正在用 `welch` 做功率谱估计，应该用对称窗还是周期窗？通过 `get_window` 该怎么写？

**参考答案**：谱估计应使用**周期窗**（避免周期延拓时的相位跳变引入泄漏）。`get_window` 默认 `fftbins=True` 就是周期窗，所以直接 `get_window('hann', N)` 即可；若想更明确，写 `get_window('hann_periodic', N)` 或 `get_window('hann', N, fftbins=True)`。注意：如果错写成 `fftbins=False`，会得到对称窗，在谱分析中并非最佳。

**练习 2**：为什么 `_extend`/`_truncate` 要做成所有窗共用的辅助函数，而不是每个窗函数自己写一遍？

**参考答案**：因为"对称生成、周期去尾"是一个与具体窗形状**无关**的通用机制——无论窗是 Hann、Hamming 还是 Kaiser，"想要周期窗就内部多算一点再丢掉末点"的逻辑都一样。抽成公共辅助函数避免了在二十多个窗函数里重复同一段代码，也保证了所有窗的 `sym` 语义完全一致（修改一处即全局生效）。这是典型的"把横切关注点提取为工具函数"的设计。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成一个"窗函数调研小工具"。

**任务**：写一个函数 `describe_window(spec, N)`，它接受任意 `get_window` 能识别的 `spec`（字符串、元组或浮点），返回该窗的时域样本、最高旁瓣电平（dB），并打印它走了哪条调用路径。

**参考实现（示例代码，非项目原有代码）**：

```python
import numpy as np
from scipy.signal import get_window
from scipy.fft import fft, fftshift

def describe_window(spec, N=64):
    # 1) 调度：交给 get_window 统一解析（覆盖 4.2 节的全部形态）
    w = get_window(spec, N)   # 默认 fftbins=True -> 周期窗，适合谱分析

    # 2) 频域：用补零 FFT 估主瓣与最高旁瓣
    W = np.abs(fftshift(fft(w, 8 * N)))
    W /= W.max()
    W_db = 20 * np.log10(np.maximum(W, 1e-12))

    peak_idx = np.argmax(W)
    # 屏蔽主瓣区域（峰值附近），找最高旁瓣
    mask = np.ones(len(W_db), dtype=bool)
    half = N // 4
    mask[max(0, peak_idx - half):peak_idx + half + 1] = False
    sidelobe = W_db[mask].max()

    print(f"spec={spec!r:30}  最高旁瓣 ≈ {sidelobe:6.2f} dB")
    return w, sidelobe

# 一次对比多种窗
for s in ['boxcar', 'hann', 'hamming', 'blackman', ('kaiser', 8.0), 4.0]:
    describe_window(s)
```

**实践要点**：
- 第 1 步直接复用 `get_window` 的调度能力——你的函数因此自动支持所有别名、元组、浮点形态，无需自己写 `if/elif`。这验证了 4.2 节"调度器简化上层接口"的价值。
- 默认 `fftbins=True` 得到周期窗，适合这里的频域分析（呼应 4.3 节）。
- 观察输出：`boxcar`（矩形）旁瓣最高（约 −13 dB），`blackman` 与高 `beta` 的 Kaiser 旁瓣最低。这印证了 2.1 节"主瓣宽度 vs 旁瓣电平"的折中——若你把 `N` 调大，会发现旁瓣电平基本不变，但主瓣变窄。

**进阶**：把 `get_window(spec, N)` 换成 `get_window(spec, N, fftbins=False)`（对称窗），重新对比旁瓣电平，观察两者的频域差异。

## 6. 本讲小结

- `windows` 是 `scipy.signal` 内部的"子包中的子包"，由三个文件构成"三件套"：`_windows.py`（实现）、`__init__.py`（正式入口）、`windows.py`（弃用 stub），完整复刻了单元 1 讲过的命名空间编织与弃用侧链机制。
- `get_window` 是一个**统一调度器**：用一张在模块导入时展开的别名表 `_WIN_FUNCS`，把字符串名/元组/浮点三种形态 `O(1)` 分发到二十多种具体窗函数，并用 `has_args` 三态（`True`/`False`/`'OPTIONAL'`）把参数校验也压进表里。
- `sym`（对称）与 `fftbins` 是**逻辑取反**关系（`sym = not fftbins`）：底层窗函数默认对称（服务于滤波设计），`get_window` 默认周期（服务于谱分析）；窗名后缀 `_symmetric`/`_periodic` 可强制覆盖。
- 周期窗通过"加一再去尾"（`_extend`/`_truncate`）这一与窗形状无关的通用模板实现，所有窗共用，是典型的横切关注点提取。
- `_signal_api.py` 通过 `from . import windows` 把整个子包挂到主命名空间，又单独把 `get_window` 提升到 `scipy.signal` 顶层（带注释强调这是有意保留），因为它太常用了。

## 7. 下一步学习建议

- **本单元下一讲 [u2-l4 经典窗函数的实现细节](u2-l4-window-implementations.md)**：深入 `_windows.py` 内部，看 `general_cosine` 如何作为 Hann/Hamming/Blackman 的公共基底、`kaiser` 的 `beta` 如何权衡主瓣与旁瓣、以及 `dpss`（离散长球序列）这类更复杂的窗如何实现。本讲只讲了"如何调度到窗"，下一讲讲"窗本身怎么算"。
- **回顾滤波设计单元 u4/u5**：`firwin`（FIR 设计）内部就调用 `get_window` 把用户传入的窗名转成实际窗序列。学完本讲后，再去读 `firwin` 会发现 `window` 参数的处理非常自然。
- **建议阅读的源码**：在 `_windows.py` 中挑一个你感兴趣的窗（比如 `kaiser` 在 [windows/_windows.py:1203](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/windows/_windows.py#L1203)），对照它的数学定义与 `_extend`/`_truncate` 模板，亲手验证 `sym=True/False` 两种输出的长度与对称性。
