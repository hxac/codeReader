# array_function 调度与 set_module 机制

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 `numpy.fft` 里两个「看起来很像、其实职责不同」的装饰器 `array_function_dispatch` 与 `set_module` 各自做了什么。
- 解释为什么 `fftshift` 用 `array_function_dispatch`，而 `fftfreq`/`rfftfreq` 只用 `set_module`——也就是「有数组参数需要被第三方数组库接管」与「只要归属到 `numpy.fft` 命名空间」两种诉求的区别。
- 看懂 `_pocketfft.py` 里用 `functools.partial(..., module='numpy.fft')` 把 `module=` 参数预先绑定的 DRY 写法，并与 `_helper.py` 里每次显式传 `module=` 的写法对照。
- 理解 `__array_function__` 协议（NEP 18）对 CuPy / Dask / JAX 等第三方数组库的意义，以及 dispatcher「返回相关参数」这一步的关键作用。

## 2. 前置知识

在进入源码前，先用大白话把几个概念铺平。

### 2.1 `__module__`：函数「挂」在哪个命名空间下

Python 里每个函数对象都有一个 `__module__` 属性，记录它定义在哪个模块。比如一个函数 `f` 的 `__module__ == 'numpy.fft'`，就意味着它对外呈现为「`numpy.fft` 这个包里的函数」。这会影响 `help(np.fft.fft)` 显示的内容、文档系统、以及 NumPy 内部做类型分发时读取的信息。

但是，`numpy.fft` 的实现其实是拆在两个私有子模块 `_pocketfft.py` 和 `_helper.py` 里的。如果不做任何处理，这些函数的 `__module__` 会是 `numpy.fft._pocketfft` / `numpy.fft._helper`，而不是干净的 `numpy.fft`。所以需要一个手段把它们「改挂」到 `numpy.fft` 名下——这正是两个装饰器都要做的事。

### 2.2 `__array_function__` 协议（NEP 18）：让第三方数组接管 NumPy 函数

NumPy 的很多函数（包括 `np.fft.fft`、`np.fft.fftshift`）希望不仅能处理原生 `np.ndarray`，还能处理 CuPy 的 GPU 数组、Dask 的分块数组、JAX 的数组等。NEP 18 定义的 `__array_function__` 协议就是为此而生：

- 当一个「被协议覆盖的」NumPy 函数被调用时，NumPy 会先调用一个 **dispatcher**，拿到这次调用里「相关的数组参数」（relevant args）。
- NumPy 收集这些参数的类型，问它们：「你们当中谁实现了 `__array_function__`？谁来接管这次调用？」
- 如果某个类型接管（返回非 `NotImplemented`），就由它来算；否则 NumPy 用自己的默认实现（也就是被包装前的那份原始函数）。

> 关键直觉：**dispatcher 只返回「数组类的」参数**。像 `n=8`、`axis=-1`、`norm="ortho"` 这些标量/字符串不是数组，不参与接管决策，所以不放进 dispatcher 的返回值里。

并非所有函数都需要这套机制。下面会看到，`fftfreq` 的输入根本没有数组，于是它**没有**调度，只需要 `set_module`。

### 2.3 `functools.partial`：把某个参数「提前钉死」

`functools.partial(func, a=1)` 会产生一个新的可调用对象，它调用时等价于 `func(a=1, ...)`，即把 `a=1` 这个关键字参数预先绑死。本讲会看到 `_pocketfft.py` 用它把 `module='numpy.fft'` 预先绑进 `array_function_dispatch`，省得 14 个变换函数每个都写一遍。

## 3. 本讲源码地图

| 文件 | 本讲关注的内容 |
| --- | --- |
| [_helper.py](_helper.py) | 第 6 行导入两个装饰器；`fftshift`/`ifftshift` 用 `array_function_dispatch(..., module='numpy.fft')`；`fftfreq`/`rfftfreq` 用 `set_module('numpy.fft')`。 |
| [_pocketfft.py](_pocketfft.py) | 第 50–51 行用 `functools.partial` 预绑定 `module='numpy.fft'`；14 个变换函数（`fft`/`ifft`/.../`irfft2`）全部用这个 partial 版的 `array_function_dispatch`；两个 dispatcher `_fft_dispatcher` 与 `_fftn_dispatcher`。 |
| `numpy/_core/overrides.py`（包外，不在 fft/ 内） | `array_function_dispatch` 与 `set_module` 的真实实现。本讲描述其行为，行号不引用（ fft/ 目录之外，本讲无法直接核对）。 |

> 说明：本讲的永久链接都指向 `fft/` 目录下能直接核对的文件。`overrides.py` 的内部实现位于 `numpy/_core/overrides.py`，超出本子包范围，我们只依据其在 `_helper.py` / `_pocketfft.py` 中**可观察到的行为**来讲解，不杜撰其行号。

## 4. 核心概念与源码讲解

### 4.1 两个装饰器各自要解决什么问题

#### 4.1.1 概念说明

`numpy.fft` 的四个 helper 和十四个变换函数，在「归属到 `numpy.fft` 命名空间」这件事上需求一致，但在「要不要支持 `__array_function__` 调度」这件事上需求不同：

| 需求 | 由谁满足 |
| --- | --- |
| 把 `__module__` 改成 `numpy.fft` | 两个装饰器**都能**做到 |
| 在被调用时走 `__array_function__` 协议、允许第三方数组接管 | 只有 `array_function_dispatch` 能做到 |

所以可以这样记：

- **`set_module('numpy.fft')`**：轻量级，只改 `__module__`，**不**加调度。适合「输入里没有数组、不需要被接管」的函数。
- **`array_function_dispatch(dispatcher, module='numpy.fft')`**：重量级，既改 `__module__`，**又**把函数包一层调度逻辑。适合「输入里有数组、第三方数组库可能想接管」的函数。

#### 4.1.2 核心流程

被 `array_function_dispatch` 包装后的「公开函数」（记作 `public_api`）在被调用时，大致流程是：

```
用户调用 public_api(*args, **kwargs)
        │
        ▼
dispatcher(*args, **kwargs)   ← 拿到「相关数组参数」relevant_args
        │
        ▼
收集 relevant_args 里每个元素的类型 types
        │
        ▼
有没有某个类型实现了 __array_function__ 且返回非 NotImplemented？
        │                                   │
        是                                   否
        │                                   │
        ▼                                   ▼
交给那个类型的实现               回退到 NumPy 默认实现
（如 CuPy/Dask 自己的 fft）       （即被包装前的原始函数 impl）
```

而 `set_module` 装饰后的函数，**没有上面这一整段**，它就是原函数本身，只是多了正确的 `__module__`。

> 经验法则：dispatcher 返回的是「相关参数」，也就是那些**可能是数组、从而有资格接管调用**的实参。`n`、`axis`、`norm`、`s`、`axes` 这些都不是数组，所以一律不放进 dispatcher 的返回值。

#### 4.1.3 源码精读

两个装饰器都从同一个地方导入：

[_helper.py:6](_helper.py#L6) —— 从 `numpy._core.overrides` 导入 `array_function_dispatch` 与 `set_module`：

```python
from numpy._core.overrides import array_function_dispatch, set_module
```

这一行是整个机制的入口。两个名字来自 `numpy/_core/overrides.py`（在 `fft/` 目录之外，本讲只依据其在 fft 子包中的可观察行为讲解）。

#### 4.1.4 代码实践

**实践目标**：用最直接的方式，验证「两个装饰器都改了 `__module__`」。

**操作步骤**：

```python
import numpy as np
print(np.fft.fftshift.__module__)   # 期望 numpy.fft
print(np.fft.ifftshift.__module__)  # 期望 numpy.fft
print(np.fft.fftfreq.__module__)    # 期望 numpy.fft
print(np.fft.rfftfreq.__module__)   # 期望 numpy.fft
print(np.fft.fft.__module__)        # 期望 numpy.fft
```

**预期结果**：全部打印 `numpy.fft`（而不是 `numpy.fft._helper` / `numpy.fft._pocketfft`）。这证明两种装饰器都起到了「改挂命名空间」的作用。

**需要观察的现象**：如果把 `_helper.py` 里 `fftfreq` 上的 `@set_module('numpy.fft')` 去掉，`fftfreq.__module__` 会变回 `numpy.fft._helper`——可见这个装饰器不是装饰，是实打实改属性。

#### 4.1.5 小练习与答案

**练习 1**：`array_function_dispatch` 和 `set_module` 各能做到哪件事？哪件事只有前者能做？

**参考答案**：两者都能改 `__module__`；但只有 `array_function_dispatch` 会额外包一层 `__array_function__` 调度逻辑，并要求你提供一个 dispatcher。`set_module` 只改属性、不加调度。

**练习 2**：如果一个函数的输入里没有任何数组（只有整数、浮点数），它应该用哪个装饰器？为什么？

**参考答案**：用 `set_module`。因为没有数组参数，`__array_function__` 协议没有「相关参数」可接管，调度毫无意义；此时只需要把 `__module__` 改成 `numpy.fft`。

---

### 4.2 `array_function_dispatch` 与 dispatcher

#### 4.2.1 概念说明

`array_function_dispatch` 接受两个关键输入：

1. **dispatcher**：一个签名与被装饰函数完全相同的「伴随函数」，负责返回这次调用里的「相关数组参数」元组。
2. **`module=`**：要写进 `__module__` 的模块名字符串（本子包里固定是 `'numpy.fft'`）。

`numpy.fft` 里所有「输入含数组」的函数都用了它：

- helper：`fftshift`、`ifftshift`（dispatcher 为 `_fftshift_dispatcher`，返回 `(x,)`）。
- 一维变换：`fft`、`ifft`、`rfft`、`irfft`、`hfft`、`ihfft`（dispatcher 为 `_fft_dispatcher`，返回 `(a, out)`）。
- 多维变换：`fftn`、`ifftn`、`fft2`、`ifft2`、`rfftn`、`rfft2`、`irfftn`、`irfft2`（dispatcher 为 `_fftn_dispatcher`，返回 `(a, out)`）。

注意：dispatcher 返回的是 **`(a, out)` 而不是 `(a,)`**。这是因为 2.0 新增的 `out=` 参数本身也是一个数组（输出缓冲区），第三方数组库同样需要判断它是否要被接管。

#### 4.2.2 核心流程

以 `fftshift` 为例，dispatcher 与被装饰函数签名一一对应：

```
_fftshift_dispatcher(x, axes=None)  ──返回──>  (x,)          # 只 x 是数组
fftshift(x, axes=None)              ──真正实现──>  roll(...)  # x 是数组，axes 是标量/元组
```

以 `fft` 为例：

```
_fft_dispatcher(a, n=None, axis=None, norm=None, out=None)  ──返回──>  (a, out)
fft(a, n=None, axis=-1, norm=None, out=None)                ──真正实现──>  _raw_fft(...)
```

返回值里**故意排除** `n`、`axis`、`norm`、`s`、`axes` 这些非数组参数——它们不参与接管决策。

#### 4.2.3 源码精读

**helper 侧的 dispatcher** [_helper.py:15-16](_helper.py#L15-L16)：`_fftshift_dispatcher` 与 `fftshift` 形参完全一致，只返回数组 `x`：

```python
def _fftshift_dispatcher(x, axes=None):
    return (x,)
```

**helper 侧的装饰** [_helper.py:19-20](_helper.py#L19-L20)（`fftshift`）与 [_helper.py:77-78](_helper.py#L77-L78)（`ifftshift`），每次都显式带 `module='numpy.fft'`：

```python
@array_function_dispatch(_fftshift_dispatcher, module='numpy.fft')
def fftshift(x, axes=None):
    ...
```

**一维变换的 dispatcher** [_pocketfft.py:116-117](_pocketfft.py#L116-L117)：返回 `(a, out)`，把输入 `a` 和输出缓冲 `out` 都算作「相关参数」：

```python
def _fft_dispatcher(a, n=None, axis=None, norm=None, out=None):
    return (a, out)
```

**多维变换的 dispatcher** [_pocketfft.py:751-752](_pocketfft.py#L751-L752)：签名换成 `s`/`axes`，但返回的依然是 `(a, out)`：

```python
def _fftn_dispatcher(a, s=None, axes=None, norm=None, out=None):
    return (a, out)
```

**14 个变换函数的装饰**：每个变换都贴一行 `@array_function_dispatch(...)`，例如 [_pocketfft.py:120-121](_pocketfft.py#L120-L121) 的 `fft`：

```python
@array_function_dispatch(_fft_dispatcher)
def fft(a, n=None, axis=-1, norm=None, out=None):
    ...
```

其余位置：`ifft`(219)、`rfft`(324)、`irfft`(421)、`hfft`(529)、`ihfft`(632) 用 `_fft_dispatcher`；`fftn`(755)、`ifftn`(887)、`fft2`(1019)、`ifft2`(1144)、`rfftn`(1266)、`rfft2`(1393)、`irfftn`(1473)、`irfft2`(1612) 用 `_fftn_dispatcher`。注意这里**没有**写 `module=`——原因见 4.3。

#### 4.2.4 代码实践

**实践目标**：验证 `array_function_dispatch` 包装出来的「公开函数」保留了原始实现，并能观察到 dispatcher 的存在。

**操作步骤**：

```python
import numpy as np

# 1) _implementation 属性：被 array_function_dispatch 包装后，
#    原始的 NumPy 实现被挂在 _implementation 上，可绕过调度直接调用。
print(np.fft.fft._implementation)        # <function fft ...>
print(np.fft.fft._implementation.__module__)  # 期望 numpy.fft

# 2) dispatcher 返回的是元组里的"数组参数"。可用如下方式间接验证：
#    传一个 __array_function__ 拒绝接管的类型，看它如何报错/回退。
class Tracer:
    def __array_function__(self, func, types, args, kwargs):
        print(f"接管调用: func={func.__name__}, types={types}")
        return NotImplemented  # 表示"我不处理"，让 NumPy 回退

# Tracer 不是 ndarray，roll 会再走 asarray 转换；这里只用于观察"是否被询问"
try:
    np.fft.fftshift.__wrapped__ if hasattr(np.fft.fftshift, "__wrapped__") else None
except Exception as e:
    print(e)
```

**需要观察的现象**：

- `np.fft.fft._implementation` 存在且可调用，证明 `array_function_dispatch` 把原始实现「藏」在了这个属性里。`set_module` 装饰的函数（如 `fftfreq`）**没有** `_implementation` 属性——这是两类装饰器的一个直观差别。
- 运行后自检：`hasattr(np.fft.fft, '_implementation')` 为 `True`，`hasattr(np.fft.fftfreq, '_implementation')` 为 `False`（待本地验证后者）。

**预期结果**：`fft` 有 `_implementation`，`fftfreq` 没有。

> 如果 `_implementation` 的存在性与你预期不符，记为「待本地验证」并在自己机器上复现。

#### 4.2.5 小练习与答案

**练习 1**：`_fft_dispatcher` 返回 `(a, out)` 而不是 `(a,)`。为什么 `out` 也要算相关参数？

**参考答案**：因为 `out` 也是一个数组（输出缓冲区）。如果用户传了一个 CuPy 数组作为 `out`，第三方数组库同样需要判断它是否要接管这次调用，否则把结果写进错误类型的缓冲区会出问题。

**练习 2**：为什么 `_fft_dispatcher` 的返回值里没有 `n`、`axis`、`norm`？

**参考答案**：它们分别是整数、整数、字符串，都不是数组，没有 `__array_function__`，没有资格接管调用，所以不放进「相关参数」。

---

### 4.3 `functools.partial`：把 `module=` 预先绑死

#### 4.3.1 概念说明

`_pocketfft.py` 里有 14 个变换函数都要用 `array_function_dispatch`，而且 `module=` 永远是 `'numpy.fft'`。如果像 `_helper.py` 那样每次都写 `module='numpy.fft'`，就要重复 14 遍。为了 DRY（Don't Repeat Yourself），作者用 `functools.partial` 把 `module='numpy.fft'` 预先「钉」进 `array_function_dispatch`，得到一个模块本地的同名简写，之后调用就只需写 dispatcher 了。

#### 4.3.2 核心流程

```
# 原始（包外实现）
overrides.array_function_dispatch(dispatcher, module='numpy.fft')

# 用 partial 预绑定 module= 之后
array_function_dispatch = functools.partial(
    overrides.array_function_dispatch, module='numpy.fft')

# 之后每次只需：
@array_function_dispatch(_fft_dispatcher)   # module='numpy.fft' 已自动带上
def fft(...): ...
```

两种写法**等价**，区别只在要不要重复写 `module='numpy.fft'`。

#### 4.3.3 源码精读

[_pocketfft.py:50-51](_pocketfft.py#L50-L51) 把 partial 版的 `array_function_dispatch` 定义在模块顶层，覆盖了从 `numpy._core` 间接导入的同名引用：

```python
array_function_dispatch = functools.partial(
    overrides.array_function_dispatch, module='numpy.fft')
```

> 注意这里有个**命名遮蔽（shadowing）**：模块内后续出现的 `array_function_dispatch`（装饰器）都是这个 partial 产物，而不是原始的 `overrides.array_function_dispatch`。阅读 14 个 `@array_function_dispatch(...)` 时要明白这一点。

对照 [_helper.py:19-20](_helper.py#L19-L20)：`_helper.py` 只有 `fftshift`、`ifftshift` 两处用得到，于是干脆每次显式写 `module='numpy.fft'`，不值得为两处再做 partial。这是同一个目标、按使用频次选择的两种写法。

#### 4.3.4 代码实践

**实践目标**：亲手验证 partial 版与显式版语义等价。

**操作步骤**（示例代码，非项目源码）：

```python
import functools

def make(dispatcher, module=None):
    print(f"  调用原始: dispatcher={dispatcher}, module={module}")
    return lambda fn: fn   # 极简版装饰器，仅用于演示

# 仿照 _pocketfft.py 的 partial 写法
afp = functools.partial(make, module='numpy.fft')

@afp(lambda a, out=None: (a, out))     # 等价于 make(..., module='numpy.fft')
def f1(a, out=None):
    pass

# 仿照 _helper.py 的显式写法
@make(lambda x: (x,), module='numpy.fft')
def f2(x):
    pass
```

**需要观察的现象**：装饰 `f1` 时打印的 `module='numpy.fft'` 是 partial 自动补上的；装饰 `f2` 时是显式传的。两者最终传给 `make` 的 `module` 完全一致。

**预期结果**：两次打印都显示 `module='numpy.fft'`，证明两种写法等价。

#### 4.3.5 小练习与答案

**练习 1**：`_pocketfft.py` 里的 `array_function_dispatch` 与 `overrides.array_function_dispatch` 是同一个对象吗？

**参考答案**：不是。前者是 `functools.partial(overrides.array_function_dispatch, module='numpy.fft')` 产生的新可调用对象，后者是原始函数。前者调用时会自动补上 `module='numpy.fft'`。

**练习 2**：为什么 `_helper.py` 不用同样的 partial 写法？

**参考答案**：`_helper.py` 里只有 `fftshift`/`ifftshift` 两处用到 `array_function_dispatch`，引入 partial 反而增加一层间接性、得不偿失；直接每次显式写 `module='numpy.fft'` 更清晰。partial 适合 `_pocketfft.py` 这种 14 处复用的场景。

---

### 4.4 `set_module('numpy.fft')`：只改属性、不加调度

#### 4.4.1 概念说明

`set_module` 是更轻的装饰器：它只把函数的 `__module__` 设成给定值，**不**做任何 `__array_function__` 调度。`numpy.fft` 里用它的是 `fftfreq` 和 `rfftfreq`。

为什么这两个不用 `array_function_dispatch`？看一眼它们的签名就明白了：

- `fftfreq(n, d=1.0, device=None)`
- `rfftfreq(n, d=1.0, device=None)`

`n` 是整数（窗口长度），`d` 是浮点（采样间距），`device` 是字符串。**输入里没有任何数组**——函数是「凭空」用 `arange`/`empty` 构造出频率数组的。既然没有数组参数，`__array_function__` 协议就没有「相关参数」可接管，调度无从谈起。但它们仍然需要 `__module__ == 'numpy.fft'`（命名空间归属、`help` 显示、内部读取），所以用 `set_module` 恰到好处。

> 一句话对比：**有数组参数 → `array_function_dispatch`；没有数组参数 → `set_module`。**

#### 4.4.2 核心流程

```
@set_module('numpy.fft')
def fftfreq(n, d=1.0, device=None):
    ...
        │
        ▼
set_module 直接做: fftfreq.__module__ = 'numpy.fft'
        │
        ▼
返回 fftfreq 本身（不包一层 public_api，不加 _implementation）
```

#### 4.4.3 源码精读

[_helper.py:125-126](_helper.py#L125-L126)（`fftfreq`）与 [_helper.py:180-181](_helper.py#L180-L181)（`rfftfreq`）：

```python
@set_module('numpy.fft')
def fftfreq(n, d=1.0, device=None):
    ...

@set_module('numpy.fft')
def rfftfreq(n, d=1.0, device=None):
    ...
```

注意它们的形参里没有数组（`n`、`d`、`device`），所以也没有对应的 dispatcher 函数——这是它们与 `fftshift`/`fft` 在源码形态上最直观的差别。

#### 4.4.4 代码实践

**实践目标**：用对比实验把「两类装饰器」的差别钉死。

**操作步骤**：

```python
import numpy as np

def classify(name):
    f = getattr(np.fft, name)
    has_impl = hasattr(f, '_implementation')
    print(f"{name:10s} __module__={f.__module__:12s} "
          f"has _implementation={has_impl}")

for n in ['fft', 'fftshift', 'fftfreq', 'rfftfreq']:
    classify(n)
```

**需要观察的现象 / 预期结果**：

| 函数 | 装饰器 | `__module__` | 有 `_implementation` |
| --- | --- | --- | --- |
| `fft` | `array_function_dispatch` | `numpy.fft` | 是 |
| `fftshift` | `array_function_dispatch` | `numpy.fft` | 是 |
| `fftfreq` | `set_module` | `numpy.fft` | 否 |
| `rfftfreq` | `set_module` | `numpy.fft` | 否 |

> 解释：`_implementation` 是 `array_function_dispatch` 包装时挂上去的「绕过调度的原始实现」；`set_module` 不做这层包装，所以没有这个属性。两个函数的 `__module__` 都是 `numpy.fft`，说明归属需求两者都满足。`_implementation` 的有无是「有没有调度」最直接的判别标志（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `fftfreq` 的装饰器从 `set_module('numpy.fft')` 换成 `array_function_dispatch(some_dispatcher, module='numpy.fft')`，会出什么问题？

**参考答案**：你需要写一个 dispatcher 返回「相关数组参数」，但 `fftfreq` 的输入（`n`、`d`、`device`）都不是数组，dispatcher 无意义可返回；而且每次调用都会白白走一遍 `__array_function__` 协议的收集/询问流程，徒增开销，没有任何数组能真正接管。属于用错了工具。

**练习 2**：从「是否参与 NEP 18 接管」的角度，给 `numpy.fft` 的 18 个公开名字分类。

**参考答案**：

- 参与 `__array_function__` 接管（用 `array_function_dispatch`）：`fft`、`ifft`、`rfft`、`irfft`、`hfft`、`ihfft`、`fftn`、`ifftn`、`fft2`、`ifft2`、`rfftn`、`rfft2`、`irfftn`、`irfft2`、`fftshift`、`ifftshift`（共 16 个，输入含数组）。
- 不参与（用 `set_module`）：`fftfreq`、`rfftfreq`（共 2 个，输入无数组）。

## 5. 综合实践

把本讲的三条主线串起来：命名空间归属、dispatcher 返回什么、partial 预绑定。

**任务**：写一个小脚本，自动扫描 `numpy.fft` 的 18 个公开函数（即 `np.fft.__all__`），对每个函数判断它属于哪一类装饰器，并打印一张总表。

**参考实现**（示例代码）：

```python
import numpy as np

rows = []
for name in np.fft.__all__:
    f = getattr(np.fft, name)
    decorated = hasattr(f, '_implementation')   # array_function_dispatch 的标志
    kind = 'array_function_dispatch' if decorated else 'set_module'
    rows.append((name, f.__module__, kind))

print(f"{'function':12s}{'__module__':16s}{'decorator':24s}")
for name, mod, kind in rows:
    print(f"{name:12s}{mod:16s}{kind:24s}")

# 汇总
from collections import Counter
print(Counter(k for *_, k in rows))
```

**预期结果**：

- 所有 18 个函数的 `__module__` 都是 `numpy.fft`（证明归属需求被一致满足）。
- 16 个被判定为 `array_function_dispatch`，2 个（`fftfreq`、`rfftfreq`）被判定为 `set_module`。
- 计数约 `{'array_function_dispatch': 16, 'set_module': 2}`（待本地验证，以 `_implementation` 属性的实际有无为准）。

**延伸思考**：如果把判定依据从「`_implementation` 有无」改成「`__module__`」，还能区分两类吗？为什么？（答：不能，因为两类都把 `__module__` 改成了 `numpy.fft`，这正是为什么本讲强调两者的差别在「调度」而非「归属」。）

## 6. 本讲小结

- `numpy.fft` 用两个装饰器统一解决「归属到 `numpy.fft` 命名空间」：`set_module` 只改 `__module__`；`array_function_dispatch` 既改 `__module__` 又包一层 `__array_function__`（NEP 18）调度。
- dispatcher 的职责是返回**这次调用里的相关数组参数**：`_fftshift_dispatcher` 返回 `(x,)`，`_fft_dispatcher`/`_fftn_dispatcher` 返回 `(a, out)`；`n`/`axis`/`norm`/`s`/`axes` 等非数组参数一律排除。
- 判断函数用哪个装饰器的准则是「输入有没有数组」：`fftshift` 及 14 个变换含数组 → `array_function_dispatch`；`fftfreq`/`rfftfreq` 输入无数组 → `set_module`。
- `_pocketfft.py` 用 `functools.partial(overrides.array_function_dispatch, module='numpy.fft')` 把 `module=` 预先绑死，14 处装饰不再重复；`_helper.py` 只有 2 处，直接显式写。
- 被装饰函数可通过 `func._implementation` 取得绕过调度的原始实现，这是 `array_function_dispatch` 独有、`set_module` 没有的标志属性。
- 这一整套机制是 CuPy / Dask / JAX 等第三方数组库能复用 `np.fft.*` API 的基础设施。

## 7. 下一步学习建议

- 下一讲将进入 **第 3 单元：一维 FFT 核心流程**，从 [u3-l1](_pocketfft.py) 的 `fft`/`ifft` 与统一入口 `_raw_fft` 开始。届时你会看到本讲的 dispatcher 把 `(a, out)` 暴露出去之后，真正的计算如何落到 `_raw_fft` 再到 C++ 后端。
- 想深入了解 `__array_function__` 协议本身，可阅读 NumPy 官方文档的 NEP 18，以及 `numpy/_core/overrides.py` 中 `array_function_dispatch` 与 `set_module` 的实现（在 fft 子包之外）。
- 想验证第三方数组库的接管效果，可尝试安装 CuPy 或 Dask，构造其数组后调用 `np.fft.fftshift(x)`，观察是否被对应库接管。
- 继续阅读 [overrides 导入行](_helper.py#L6) 与 [partial 定义](_pocketfft.py#L50-L51)，把本讲的两条装饰器主线在源码里彻底定位。
