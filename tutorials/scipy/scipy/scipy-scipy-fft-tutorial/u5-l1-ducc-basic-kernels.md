# ducc 内核：c2c / r2c / c2r 与 functools.partial

> 本讲进入 `scipy.fft` 四层架构的最底层——计算核心层 `_duccfft`。前面几讲我们反复看到「公共 API → uarray 分派 → 后端 → ducc 核心」这条链路，但始终把最后一层当作黑盒。本讲打开这个黑盒，看清 `scipy.fft` 真正算 FFT 的地方。

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 `c2c`、`r2c`、`c2r` 三个计算内核各自的输入/输出类型与它们共享的 `forward` 参数的作用。
- 解释为什么 14 个公共变换（fft/ifft/rfft/irfft/hfft/ihfft 及其 n 维版本）只由 3 个 Python 内核 + `functools.partial` 派生而来。
- 看懂 `partial` 派生后为何必须手动修正 `__name__`。
- 理解从 Python 到 C 扩展 `pyduccfft` 的「最后一跳」：参数如何从 `forward/norm/workers` 映射成 C 层的 `forward/inorm/nthreads`。
- 动手列出一张「公共函数 → 内核 + forward 值」的对照表。

## 2. 前置知识

本讲默认你已经读过前置讲义，具备以下认知：

- **四层架构**（u1-l2）：公共 API 层 `_basic.py` 只写签名，分派由 uarray 完成，后端 `_basic_backend.py` 桥接数组，最底层 `_duccfft` 真正算 FFT。
- **分派协议**（u4-l1 / u4-l3）：`fft(x)` 这个调用经过 `_dispatch` → `_ScipyBackend.__ua_function__` 按方法名路由，最终落到 `_basic_backend.fft`，再调 `_duccfft.fft`。
- **norm 三模式**（u2-l1）：`backward`/`ortho`/`forward` 三种归一化，正逆配对恒可逆。

几个需要澄清的术语：

- **内核（kernel）**：这里指 `_duccfft/basic.py` 里那几个真正组织计算流程的 Python 函数（`c2c`、`r2c`、`c2r` 等），它们本身不写 FFT 算法，而是做参数预处理后调用 C 扩展。注意区别于「C 内核」。
- **`forward` 参数**：一个布尔值，同时承担两件事——告诉 C 扩展算「正向 DFT」还是「逆向 DFT」，并决定归一化的方向。一个内核靠它复用出正、逆两个变换。
- **`functools.partial`**：Python 标准库工具，把一个函数的某些参数「固定」下来，生成一个新的可调用对象。本讲里它被用来把 `forward` 固定为 `True`/`False`，从而从 `c2c` 派生出 `fft`/`ifft`。
- **`pyduccfft`**：一个用 pybind11 编写的 C++ 扩展模块（源码是 `_duccfft/pyduccfft.cxx`），编译后即 `_duccfft/pyduccfft` 子模块。它包装了 ducc 库（-pocketfft 的继任者）的 FFT 实现，是 `scipy.fft` 里真正执行浮点运算的地方。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [_duccfft/basic.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/basic.py) | 本讲的主角。定义 `c2c`/`r2c`/`c2r`/`c2cn`/`r2cn`/`c2rn`/`r2r_fftpack` 七个内核，并用 `partial` 派生出全部基础变换。 |
| [_duccfft/__init__.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/__init__.py) | 子包入口，用 `from .basic import *` 把派生出的函数暴露出去。 |
| [_duccfft/helper.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/helper.py) | 内核调用的预处理工具：`_asfarray`、`_fix_shape`、`_normalization`、`_workers` 等（本讲引用，深入讲解留给 u5-l2/u5-l3）。 |
| [_duccfft/pyduccfft.cxx](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/pyduccfft.cxx) | C++ 扩展源码。用 `m.def("c2c", ...)` 把 C++ 函数注册成 Python 可调用对象（本讲只看绑定签名）。 |
| [_basic_backend.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic_backend.py) | 上层后端。`_execute_1D`/`_execute_nD` 在 numpy 路径下直接转调 `_duccfft.fft` 等——这是「谁在调内核」。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块组织：**4.1 三类内核 `c2c`/`r2c`/`c2r`**、**4.2 `functools.partial` 派生**、**4.3 `pyduccfft` C 扩展边界**。

### 4.1 三类内核：c2c / r2c / c2r 与 forward 参数复用

#### 4.1.1 概念说明

FFT 这件事，按「输入是实数还是复数」「输出是实数还是复数」可以分成三类组合：

| 内核 | 输入 | 输出 | 对应的数学操作 | 典型公共函数 |
|------|------|------|----------------|--------------|
| `c2c`（complex→complex） | 复 | 复 | 通用 DFT | `fft` / `ifft` |
| `r2c`（real→complex） | 实 | 复（半谱） | 实信号的 DFT，利用对称性只算一半 | `rfft` / `ihfft` |
| `c2r`（complex→real） | 复（半谱） | 实 | 半谱还原回实信号 | `irfft` / `hfft` |

为什么实信号值得单独搞两个内核？因为**实序列的 DFT 满足共轭对称性** \(X[k] = \overline{X[-k]}\)（详见 u2-l2）。既然负频率是正频率的共轭，就不必算也不必存，`r2c` 只输出非负频率的「半谱」；反过来 `c2r` 拿半谱补回完整实信号。这就是实变换比复变换省一半存储和近一半计算的根本原因。

这三类内核之所以能共用一个 `forward` 布尔参数，关键洞见是：

> **「输入/输出哪个是实数」由内核本身（`r2c` vs `c2r`）决定，与 `forward` 无关；`forward` 只决定 DFT 的方向（正变换还是逆变换）和归一化归属。**

于是同一个 `c2c` 内核：`forward=True` 时是 `fft`，`forward=False` 时是 `ifft`；同一个 `r2c` 内核：`forward=True` 是 `rfft`，`forward=False` 是 `ihfft`；同一个 `c2r` 内核：`forward=True` 是 `hfft`，`forward=False` 是 `irfft`。

#### 4.1.2 核心流程

每个内核做的都是同一套「进入 C 之前的预处理」流程，只是细节有别：

```
c2c / r2c / c2r 的统一流程
─────────────────────────────
1. 拒绝 plan              # 预计算计划尚未支持
2. _asfarray(x)           # 升级为浮点/复数数组，保证字节序与对齐
3. _normalization(norm, forward)  # 把 norm 字符串映射成整数，并按 forward 翻转
4. _workers(workers)      # 解析并行线程数（None→默认）
5. _fix_shape_1d(...)     # 按 n 截断或补零（r2c/c2r 还有额外的实数校验/升级）
6. pfft.<内核>(...)        # 调用 C 扩展，返回结果数组
```

其中 `c2r` 比另外两个多一个独有参数 `lastsize`：因为从半谱还原实信号时，原始实信号的长度信息在半谱里丢失了，必须由调用方告知（或按偶数长度 `(m-1)*2` 推断）。这是 `c2r` 与 `c2c`/`r2c` 最显著的区别。

归一化的方向翻转用一条整数规则实现（详见 u2-l1）：

\[
\text{inorm}_{\text{传出}} =
\begin{cases}
\text{inorm} & \text{if forward} \\
2 - \text{inorm} & \text{if not forward}
\end{cases}
\]

其中 `_NORM_MAP = {None: 0, 'backward': 0, 'ortho': 1, 'forward': 2}`。这条规则保证了无论用哪种 `norm` 模式，正逆变换配对后缩放因子一定抵消（`ifft(fft(x)) == x`）。

#### 4.1.3 源码精读

先看 `c2c`——最通用、也最简单的内核：

[_duccfft/basic.py#L11-L31](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/basic.py#L11-L31) —— `c2c` 内核：复到复的通用 DFT，做预处理后调 `pfft.c2c`。

```python
def c2c(forward, x, n=None, axis=-1, norm=None, overwrite_x=False,
        workers=None, *, plan=None):
    if plan is not None:
        raise NotImplementedError(...)
    tmp = _asfarray(x)
    overwrite_x = overwrite_x or _datacopied(tmp, x)
    norm = _normalization(norm, forward)          # forward 同时影响方向与归一化
    workers = _workers(workers)

    if n is not None:
        tmp, copied = _fix_shape_1d(tmp, n, axis) # 截断或补零
        overwrite_x = overwrite_x or copied
    elif tmp.shape[axis] < 1:
        raise ValueError(...)

    out = (tmp if overwrite_x and tmp.dtype.kind == 'c' else None)  # 仅复数可原地写

    return pfft.c2c(tmp, (axis,), forward, norm, out, workers)
```

注意最后一行的传参顺序对应 C 扩展签名 `c2c(a, axes, forward, inorm, out, nthreads)`：`tmp`→`a`、`(axis,)`→`axes`（即使是 1-D 也包成单元素元组）、`forward`→`forward`、`norm`→`inorm`、`out`→`out`、`workers`→`nthreads`。`overwrite_x` 仅在「复数输入且确有可写副本」时把 `tmp` 作为输出缓冲复用，避免一次分配——这是唯一的性能微优化。

再看 `r2c`——实到复，多了实数校验：

[_duccfft/basic.py#L40-L61](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/basic.py#L40-L61) —— `r2c` 内核：实输入→复半谱，拒绝复输入并丢弃 `overwrite_x`。

```python
def r2c(forward, x, n=None, axis=-1, norm=None, overwrite_x=False,
        workers=None, *, plan=None):
    ...
    tmp = _asfarray(x)
    norm = _normalization(norm, forward)
    workers = _workers(workers)

    if not np.isrealobj(tmp):
        raise TypeError("x must be a real sequence")   # 复输入直接报错
    ...
    # Note: overwrite_x is not utilised
    return pfft.r2c(tmp, (axis,), forward, norm, None, workers)
```

两个关键点：其一，`r2c` 强制要求实输入，传复数会抛 `TypeError`（这是 `rfft` 会「静默丢弃虚部」说法的真相——丢弃发生在更上层的 `_asfarray`/`np.isrealobj` 之前，本内核只负责严格校验）；其二，注释明说 `overwrite_x` 在实变换里**不生效**，因为半谱输出与输入形状不同，无法原地写，所以 `out` 恒为 `None`。

最后看 `c2r`——复到实，多了 `lastsize`：

[_duccfft/basic.py#L70-L95](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/basic.py#L70-L95) —— `c2r` 内核：复半谱→实信号，独有 `lastsize` 告知原始实长度。

```python
def c2r(forward, x, n=None, axis=-1, norm=None, overwrite_x=False,
        workers=None, *, plan=None):
    ...
    tmp = _asfarray(x)
    norm = _normalization(norm, forward)
    workers = _workers(workers)

    if np.isrealobj(tmp):
        tmp = tmp + 0.j                          # 实数组升级成复数（半谱需复数承载）

    if n is None:
        n = (tmp.shape[axis] - 1) * 2            # 默认按偶数长度还原
        if n < 1:
            raise ValueError(...)
    else:
        tmp, _ = _fix_shape_1d(tmp, (n//2) + 1, axis)

    return pfft.c2r(tmp, (axis,), n, forward, norm, None, workers)  # n 即 lastsize
```

这里的 `n` 在传给 C 扩展时扮演 `lastsize`（注意 C 签名 `c2r(a, axes, lastsize, forward, inorm, out, nthreads)`，比 `c2c`/`r2c` 多一位）。当调用者不指定 `n` 时，`c2r` 假设原始实信号是偶数长度，用 `(m-1)*2` 反推——这正是 `irfft` 在不传 `n` 时默认输出偶数长度的由来（u2-l2）。

> **N-D 内核是「同一批 C 函数」**：`c2cn`/`r2cn`/`c2rn` 三个 N-D 内核最后分别调用 `pfft.c2c`/`pfft.r2c`/`pfft.c2r`——和 1-D 是**同一个** C 函数，区别只在 `axes` 传的是多元素列表而非单元素元组。例如 [_duccfft/basic.py#L149](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/basic.py#L149) 的 `c2cn` 末行就是 `return pfft.c2c(tmp, axes, forward, norm, out, workers)`。C 扩展天生支持多轴，Python 侧的 `n` 后缀只是为了处理 `s`/`axes` 的多轴标准化。

#### 4.1.4 代码实践

**实践目标**：亲手验证三类内核对输入类型的校验逻辑，理解 `r2c` 拒绝复输入、`c2r` 接受并升级实输入的行为差异。

**操作步骤**：

```python
# 示例代码（在 Python REPL 中运行）
import numpy as np
from scipy.fft import _duccfft as d

x_real = np.arange(8, dtype=np.float64)        # 实信号
x_complex = x_real + 0j                         # 复信号（同样数值）

# 1) c2c 对两种输入都接受
print(d.c2c(True, x_real).dtype)     # complex128 —— 实输入被升级为复
print(d.c2c(True, x_complex).dtype)  # complex128

# 2) r2c 只接受实输入
try:
    d.r2c(True, x_complex)
except TypeError as e:
    print("r2c 拒绝复输入:", e)        # x must be a real sequence

# 3) c2r 拿到实数组时会主动升级成复数（tmp = tmp + 0.j）
print(d.c2r(False, x_real[:5]).dtype)  # float64 —— 输出是实信号
```

**需要观察的现象**：

- `c2c(True, x_real)` 返回 `complex128`，说明 `c2c` 不在乎输入是实是复，统一按复数处理。
- `r2c(True, x_complex)` 抛 `TypeError`，印证「半谱假设」要求输入严格为实。
- `c2r(False, x_real[:5])` 能跑通且返回实数，因为内核内部把实数组升级成了复数。

**预期结果**：第 1、3 步正常返回，第 2 步抛 `TypeError: x must be a real sequence`。

> 待本地验证：上述行为基于源码静态分析，请在你的环境里实际运行确认。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `c2r` 需要一个 `lastsize` 参数，而 `c2c` 和 `r2c` 都不需要？

> **答案**：`c2r` 从「半谱」还原实信号，而半谱只记录了非负频率，原始实信号的长度信息（奇偶、具体点数）在半谱里丢失了，所以必须由 `lastsize` 告知。`c2c` 输入输出都是完整复谱、长度一致；`r2c` 输出半谱长度由输入长度直接决定（`n//2+1`），都不存在信息缺失。

**练习 2**：`_normalization(norm, forward)` 里为什么要写成 `inorm if forward else (2 - inorm)`？

> **答案**：为了让正逆配对自动抵消缩放。`backward`(0) 与 `forward`(2) 互为 `2 - x`，`ortho`(1) 的 `2 - 1 = 1` 仍是自己。于是无论用户选哪种 `norm`，`ifft(fft(x))` 的总缩放都归一，保证可逆。

---

### 4.2 functools.partial：从一个内核派生正逆变换

#### 4.2.1 概念说明

`functools.partial(func, *args, **kwargs)` 会返回一个新的可调用对象，它等价于把 `func` 的开头几个参数预先「钉死」。在 `_duccfft/basic.py` 里，作者用它把内核的第一个参数 `forward` 钉死成 `True` 或 `False`，从而：

- `c2c` + `forward=True` → `fft`
- `c2c` + `forward=False` → `ifft`
- `r2c` + `forward=True` → `rfft`
- `r2c` + `forward=False` → `ihfft`
- `c2r` + `forward=True` → `hfft`
- `c2r` + `forward=False` → `irfft`

这样做的好处是**消除重复**：三个内核的预处理逻辑（校验、归一化、补零、调 C）只写一遍，正逆两个方向共享同一份代码，区别仅在一个布尔值。

#### 4.2.2 核心流程

```
functools.partial(c2c, True)
   │  生成一个等价于 lambda *a, **k: c2c(True, *a, **k) 的对象
   │  但它的 __name__ 仍是 "c2c"（或丢失）
   ▼
fft.__name__ = 'fft'    # 手动修正名字
   │
   ▼
from .basic import *    # __init__.py 把 fft 暴露成 _duccfft.fft
```

#### 4.2.3 源码精读

派生代码极其简短，但有一处容易被忽略的细节：

[_duccfft/basic.py#L34-L37](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/basic.py#L34-L37) —— 用 `partial` 把 `c2c` 派生成 `fft`/`ifft`，并手动设置 `__name__`。

```python
fft = functools.partial(c2c, True)
fft.__name__ = 'fft'  # pyrefly:ignore[missing-attribute]
ifft = functools.partial(c2c, False)
ifft.__name__ = 'ifft'  # pyrefly:ignore[missing-attribute]
```

为什么必须手动设 `__name__`？因为 `partial` 对象**没有 `__name__` 属性**——它不是 `function` 类型。这一点至关重要，因为上一讲（u4-3）我们看到 `_ScipyBackend.__ua_function__` 是靠 `method.__name__` 在后端模块里 `getattr` 查找实现的；上上层 `_basic_backend.py` 也用方法名字符串（如 `'fft'`）来路由。如果 `partial` 对象的 `__name__` 不对，整条「按名字路由」的分派链就会断掉。所以这行 `fft.__name__ = 'fft'` 不是装饰，而是**分派协议正常工作的前提**。

> 注释 `# pyrefly:ignore[missing-attribute]` 是给静态类型检查器看的：`partial` 对象在类型系统里没有 `__name__` 字段，pyrefly 会报错，这里显式忽略。

`r2c`/`c2r` 的派生完全同构：

[_duccfft/basic.py#L64-L67](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/basic.py#L64-L67) —— `r2c` 派生出 `rfft`/`ihfft`。

```python
rfft = functools.partial(r2c, True)
rfft.__name__ = 'rfft'
ihfft = functools.partial(r2c, False)
ihfft.__name__ = 'ihfft'
```

[_duccfft/basic.py#L98-L101](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/basic.py#L98-L101) —— `c2r` 派生出 `hfft`/`irfft`。

```python
hfft = functools.partial(c2r, True)
hfft.__name__ = 'hfft'
irfft = functools.partial(c2r, False)
irfft.__name__ = 'irfft'
```

N-D 版本同理，`c2cn`/`r2cn`/`c2rn` 各自派生出 `fftn`/`ifftn`、`rfftn`/`ihfftn`、`hfftn`/`irfftn`（见 [_duccfft/basic.py#L152-L155](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/basic.py#L152-L155)、[#L180-L183](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/basic.py#L180-L183)、[#L221-L224](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/basic.py#L221-L224)）。

这些派生函数最终经 [_duccfft/__init__.py#L3](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/__init__.py#L3) 的 `from .basic import *` 暴露为 `_duccfft.fft`、`_duccfft.ifft` 等，供 `_basic_backend.py` 调用。

#### 4.2.4 代码实践

**实践目标**：直观体会 `partial` 派生与 `__name__` 修正的必要性。

**操作步骤**：

```python
# 示例代码
import functools

def kernel(forward, x):
    return f"kernel(forward={forward}, x={x})"

# 模拟 basic.py 的派生方式
fft = functools.partial(kernel, True)
print(callable(fft))         # True
print(fft(5))                # kernel(forward=True, x=5)

# 不修正 __name__ 会怎样？
import scipy.fft._duccfft.basic as B
print(B.fft)                 # functools.partial(<function c2c at ...>, True)
print(B.fft.__name__)        # 'fft'   —— 已被手动修正
```

**需要观察的现象**：

- `B.fft` 的 `repr` 显示它是 `functools.partial(...)`，证实它不是普通函数。
- 但 `B.fft.__name__` 仍然是 `'fft'`，证明手动修正生效——这正是按名字分派能找到它的原因。

**预期结果**：`repr` 是 partial 对象，`__name__` 是 `'fft'`。

> 待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `fft.__name__ = 'fft'` 这一行删掉，`scipy.fft.fft(x)` 还能算出正确结果吗？

> **答案**：很可能不能（取决于 uarray 的具体回退路径）。因为 `_basic_backend.py` 用字符串 `'fft'` 去 `getattr`，而 `_ScipyBackend` 用 `method.__name__` 路由；`partial` 对象没有 `__name__`，访问会抛 `AttributeError`，分派链断裂。即使侥幸跑通，调试与错误信息也会错乱。所以这行是必需的。

**练习 2**：为什么作者选 `functools.partial` 而不是直接写两个独立函数 `def fft(...)` / `def ifft(...)`？

> **答案**：为了避免重复。`c2c` 的预处理逻辑（校验 plan、`_asfarray`、`_normalization`、`_fix_shape_1d`、调 C）有十几行，正逆方向完全相同；用 `partial` 共享一份实现，只差一个布尔参数，维护时改一处即可。这是「用高阶函数消除重复」的典型手法。

---

### 4.3 pyduccfft：从 Python 到 C 扩展的最后一跳

#### 4.3.1 概念说明

`c2c`/`r2c`/`c2r` 仍然只是「组织者」——它们把参数收拾干净，真正的浮点运算发生在一个叫 `pyduccfft` 的 C++ 扩展里。这个扩展用 [pybind11](https://github.com/pybind/pybind11) 把 C++ 函数包装成 Python 可调用对象，底层调用的是 [ducc](https://gitlab.mpcdf.mpg.de/mtr/ducc) 库（-pocketfft 的现代继任者）的 FFT 实现，包含 Bluestein 算法，保证**任意长度**（包括素数长度）的 FFT 都不低于 \(O(N \log N)\) 复杂度。

理解这一层的关键，是看懂**参数命名的翻译**：

| Python 内核侧（`basic.py`） | C 扩展侧（`pyduccfft.cxx`） | 含义 |
|------------------------------|------------------------------|------|
| `forward`（bool） | `forward`（bool，默认 `true`） | DFT 方向：正/逆 |
| `norm`（来自 `_normalization`，整数 0/1/2） | `inorm`（int，默认 0） | 归一化模式 |
| `workers`（来自 `_workers`，int） | `nthreads`（int，默认 1） | 并行线程数 |
| `out`（数组或 None） | `out`（数组或 None） | 输出缓冲 |
| `axis` → 包成 `(axis,)` | `axes`（tuple/list） | 变换轴 |
| ——（仅 `c2r`）`n` | `lastsize`（int，默认 0） | 实信号原始长度 |

注意 `workers` → `nthreads` 的改名：Python 对用户暴露的叫 `workers`，到了 C 层就叫 `nthreads`，本质都是「线程数」。

#### 4.3.2 核心流程

```
basic.py 顶部
─────────────
from . import pyduccfft as pfft     # 导入编译好的 C 扩展
                  │
                  ▼  （c2c 末行）
            pfft.c2c(tmp, (axis,), forward, norm, out, workers)
                  │   位置参数对应 C++ 签名
                  ▼
pyduccfft.cxx  (pybind11 注册)
─────────────
m.def("c2c", c2c, ..., "a"_a, "axes"_a=None, "forward"_a=true,
      "inorm"_a=0, "out"_a=None, "nthreads"_a=1);
                  │
                  ▼
ducc C++ 库执行真正的 FFT，返回结果数组
```

#### 4.3.3 源码精读

先看导入语句——这是 Python 与 C 的接缝：

[_duccfft/basic.py#L6](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/basic.py#L6) —— 导入编译好的 C 扩展，别名 `pfft`。

```python
from . import pyduccfft as pfft
```

`pyduccfft` 不是一个 `.py` 文件，而是 meson 编译出的扩展模块（源码是 `pyduccfft.cxx`）。从 [_duccfft/meson.build#L34-L42](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/meson.build#L34-L42) 可以看到它的构建方式：

```meson
py3.extension_module(
    'pyduccfft',
    'pyduccfft.cxx',
    cpp_args: duccfft_args,
    dependencies: [fft_deps, pybind11_dep, duccfft_dep],
    ...
    subdir: 'scipy/fft/_duccfft',
)
```

它依赖 `pybind11_dep` 和 `duccfft_dep`（ducc 库），编译成 `pyduccfft.<arch>.so`，安装到 `scipy/fft/_duccfft/` 下，于是可以被 `from . import pyduccfft` 导入。

再看 C++ 侧的函数注册（pybind11 的 `m.def`）：

[_duccfft/pyduccfft.cxx#L646-L651](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/pyduccfft.cxx#L646-L651) —— 把三个 C++ FFT 函数注册为 Python 可调用对象，并声明参数默认值。

```cpp
m.def("c2c", c2c, c2c_DS, "a"_a, "axes"_a=None, "forward"_a=true,
  "inorm"_a=0, "out"_a=None, "nthreads"_a=1);
m.def("r2c", r2c, r2c_DS, "a"_a, "axes"_a=None, "forward"_a=true,
  "inorm"_a=0, "out"_a=None, "nthreads"_a=1);
m.def("c2r", c2r, c2r_DS, "a"_a, "axes"_a=None, "lastsize"_a=0,
  "forward"_a=true, "inorm"_a=0, "out"_a=None, "nthreads"_a=1);
```

对照 `c2c` 内核末行 `pfft.c2c(tmp, (axis,), forward, norm, out, workers)`，六个位置参数依次绑定到 `a`、`axes`、`forward`、`inorm`、`out`、`nthreads`，严丝合缝。`c2r` 多一个 `lastsize`（第 3 位），所以它的调用写成 `pfft.c2r(tmp, (axis,), n, forward, norm, None, workers)`——`n` 落在 `lastsize` 槽位上。

值得强调：C 扩展只注册了 **3 个基础变换**（`c2c`/`r2c`/`c2r`），却同时服务于一维和多维——因为 `axes` 参数接受任意长度的轴列表，传 `(axis,)` 就是一维，传 `(0, 2)` 就是多维。这也解释了为什么 `c2cn`/`r2cn`/`c2rn` 三个 N-D Python 内核最终调的是同一个 `pfft.c2c`/`pfft.r2c`/`pfft.c2r`：C 层根本不区分维度，区分只发生在 Python 侧的形状预处理。

#### 4.3.4 代码实践

**实践目标**：直接探查 `pyduccfft` 模块，确认它是 C 扩展并查看其注册的函数。

**操作步骤**：

```python
# 示例代码
from scipy.fft._duccfft import pyduccfft as pfft
import numpy as np

# 1) 查看模块类型与注册的函数
print(type(pfft))                 # <class 'module'>，但由 C 扩展提供
print([n for n in dir(pfft) if not n.startswith('__')])
# 期望看到 'c2c', 'r2c', 'c2r', 'r2r_fftpack', 'dct', 'dst', 'good_size', ...

# 2) 直接用最底层 C 函数算一个 4 点 FFT，绕过全部 Python 预处理
x = np.array([0.0, 1.0, 2.0, 3.0])
# c2c(a, axes, forward, inorm, out, nthreads)
print(pfft.c2c(x, (-1,), True, 0, None, 1))
# 与 scipy.fft.fft 对照
import scipy.fft
print(scipy.fft.fft(x))
```

**需要观察的现象**：

- `dir(pfft)` 列出 `c2c`、`r2c`、`c2r` 等名字，正是 pybind11 `m.def` 注册的那些。
- 直接调 `pfft.c2c(x, (-1,), True, 0, None, 1)` 的结果与 `scipy.fft.fft(x)` 数值一致——说明上层所有封装最终都汇聚到这一个 C 调用。
- 注意必须传**位置参数**且顺序正确（`axes` 要是元组、`inorm` 是整数），因为这里没有 Python 内核帮你做 `norm` 字符串→整数的翻译。

**预期结果**：两次打印的复数数组数值相同（约 `[ 6+0j, -2+2j, -2+0j, -2-2j]`）。

> 待本地验证：具体打印格式依 numpy 版本，以数值相等为准。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `pfft.c2c` 的 `axes` 参数即使做一维变换也要求传元组 `(axis,)`，而不是单个整数？

> **答案**：因为 C 层用同一个函数同时服务一维和多维，`axes` 被设计成「轴列表」。传单个整数会让 C++ 的参数解析（pybind11 把它转成 vector<int>）出问题；统一传列表/元组，传 `(axis,)` 就是一维、传 `(0,1,2)` 就是三维，代码路径唯一。

**练习 2**：`pyduccfft` 注册了 `c2c`/`r2c`/`c2r` 三个 FFT 内核，却没有 `fft`/`ifft`/`rfft` 这些名字。那 `scipy.fft.fft` 是怎么最终调到 C 的？

> **答案**：靠 Python 侧的两层「派生」。第一层 `_duccfft/basic.py` 用 `partial(c2c, True)` 派生出 `fft`；第二层 `_basic_backend.py` 在 numpy 路径下 `_execute_1D('fft', _duccfft.fft, ...)` 调用这个派生的 `fft`，而 `fft` 内部又调 `pfft.c2c`。所以名字 `fft` 只存在于 Python 层，C 层只有 `c2c`。

---

## 5. 综合实践

把本讲三个模块串起来，完成一张**完整的派生关系对照表**，并用代码自验。

**任务**：阅读 [_duccfft/basic.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/basic.py)，填写下表（一维部分），然后用脚本验证 `forward` 与内核的对应关系。

| 公共函数 | Python 内核 | `forward` | C 扩展调用 | 输入→输出 |
|----------|-------------|-----------|-----------|-----------|
| `fft`    | `c2c`       | `True`    | `pfft.c2c` | 复→复 |
| `ifft`   | `c2c`       | `False`   | `pfft.c2c` | 复→复 |
| `rfft`   | `r2c`       | `True`    | `pfft.r2c` | 实→复（半谱） |
| `ihfft`  | `r2c`       | `False`   | `pfft.r2c` | 实→复（半谱） |
| `hfft`   | `c2r`       | `True`    | `pfft.c2r` | 复（半谱）→实 |
| `irfft`  | `c2r`       | `False`   | `pfft.c2r` | 复（半谱）→实 |

**验证脚本**（示例代码）：

```python
import numpy as np
import scipy.fft._duccfft.basic as B

# 1) 确认每个派生函数的 .func 和 .args（partial 对象的属性）
for name in ['fft', 'ifft', 'rfft', 'ihfft', 'hfft', 'irfft']:
    fn = getattr(B, name)
    kernel_name = fn.func.__name__      # 被包裹的内核，如 'c2c'
    forward_val = fn.args[0]            # partial 钉死的第一个参数
    print(f"{name:6s} <- {kernel_name}(forward={forward_val})")

# 2) 数值验证：直接调内核 vs 调派生函数，结果应一致
x = np.arange(8.0)
assert np.allclose(B.c2c(True, x), B.fft(x))
assert np.allclose(B.c2c(False, x), B.ifft(x))
print("派生与内核数值一致 ✅")
```

**预期输出**：

```
fft    <- c2c(forward=True)
ifft   <- c2c(forward=False)
rfft   <- r2c(forward=True)
ihfft  <- r2c(forward=False)
hfft   <- c2r(forward=True)
irfft  <- c2r(forward=False)
派生与内核数值一致 ✅
```

`fn.func` 和 `fn.args` 是 `functools.partial` 对象的标准属性——`func` 是被包裹的原函数，`args` 是预先固定的位置参数元组。这给了我们一个反向透视 `partial` 派生关系的好工具。

> 待本地验证：请在你的环境运行确认输出。

## 6. 本讲小结

- `_duccfft/basic.py` 用 **3 个 Python 内核**（`c2c`/`r2c`/`c2c` 的实变换兄弟 `r2c`/`c2r`，加上 N-D 的 `c2cn`/`r2cn`/`c2rn`）组织了全部基础变换的预处理流程；N-D 内核与 1-D 内核调用的是**同一个** C 函数，区别仅在 `axes` 列表长度。
- 三类内核按「实/复」组合分工：`c2c` 复→复（通用）、`r2c` 实→复半谱（`rfft`/`ihfft`）、`c2r` 复半谱→实（`hfft`/`irfft`）；**「哪边是实数」由内核决定，与 `forward` 无关**。
- `forward` 布尔参数一箭双雕：既告诉 C 扩展算正/逆 DFT，又通过 `_normalization(norm, forward)` 的 `2 - inorm` 翻转决定归一化方向，保证正逆配对恒可逆。
- `functools.partial(内核, True/False)` 把每个内核派生出一对正逆变换，消除重复；派生后必须手动 `__name__ = '...'`，因为 `partial` 对象本身无此属性，而整条按名字路由的分派链依赖它。
- 真正的浮点运算发生在 C++ 扩展 `pyduccfft`（源码 `pyduccfft.cxx`，pybind11 注册），参数从 Python 的 `forward/norm/workers` 翻译为 C 层的 `forward/inorm/nthreads`；底层 ducc 库用 Bluestein 算法保证任意长度都不差于 \(O(N \log N)\)。
- 14 个公共基础变换汇聚到 C 层仅 3 个函数（`c2c`/`r2c`/`c2r`），是「宽 Python 接口、窄 C 内核」设计的典范。

## 7. 下一步学习建议

本讲只看了「内核如何调 C」，但故意略过了内核调用的几个预处理函数的内部细节。建议接下来：

- **读 u5-l2（输入预处理）**：深入 `_duccfft/helper.py`，弄清 `_asfarray` 如何处理 float16/字节序/对齐、`_fix_shape` 如何用切片同时实现截断与补零、`_init_nd_shape_and_axes` 如何标准化 `s`/`axes`。本讲的 `c2c`/`r2c`/`c2r` 依赖它们。
- **读 u5-l3（并行 workers）**：本讲反复出现的 `_workers(workers)` 来自 `helper.py`，它背后是基于 `threading.local` 的线程级默认 worker 数与 `set_workers`/`get_workers` 上下文管理器，下一讲会展开。
- **回头对照 u6-l1**：当你之后学到 `_basic_backend._execute_1D` 的 `is_numpy` 分支时，会看到它直接调 `_duccfft.fft`——本讲解释了那个 `_duccfft.fft` 到底是什么（一个 `partial(c2c, True)` 派生、内部调 `pfft.c2c` 的对象）。
- **延伸阅读源码**：[_duccfft/realtransforms.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/realtransforms.py) 用同样的 `partial` 手法派生 DCT/DST（内核是 `_r2r`/`_r2rn`，调 `pfft.dct`/`pfft.dst`），可对照本讲加深理解。
