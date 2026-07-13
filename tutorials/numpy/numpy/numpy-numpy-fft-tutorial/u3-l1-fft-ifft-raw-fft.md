# fft/ifft 与 _raw_fft 主流程

## 1. 本讲目标

本讲是「一维 FFT 核心流程」单元的第一篇。前面几讲我们已经认识了 `numpy.fft` 子包的定位、目录结构、构建方式，以及 DFT 的数学约定和调度装饰器。本讲要真正钻进**一维变换的调用链**。

学完本讲你应该能够：

1. 说清楚 `fft` 和 `ifft` 两个公开函数各自做了什么、它们之间唯一的本质差异是什么。
2. 解释 `n` 参数如何控制输出的「截断 / 补零」，`axis` 参数如何选择被变换的轴。
3. 理解为什么所有一维变换（包括后面的 `rfft`/`irfft`）都要汇聚到一个内部统一入口 `_raw_fft`。
4. 看懂 `_raw_fft` 内部如何用 `(is_real, is_forward)` 两个布尔标志，从一个四象限决策表里选出对应的 C++ 后端 ufunc（`pfu.fft` / `pfu.ifft` / `pfu.rfft_*` / `pfu.irfft`）。

本讲只聚焦一维、复数到复数的 `fft`/`ifft` 主链路。`rfft`/`irfft` 的 Hermitian 对称细节留到 u3-l4，多维变换留到 u4。

---

## 2. 前置知识

在进入源码之前，先用一句话复习 u2-l1 和 u2-l4 里建立的两个关键认知，本讲会直接承接它们。

**认知一：DFT 的数学约定（来自 u2-l1）。**

正变换 `fft` 不归一化，指数取负号：

\[
A_k = \sum_{m=0}^{n-1} a_m \exp\left\{-2\pi i \frac{mk}{n}\right\}, \quad k=0,\dots,n-1
\]

反变换 `ifft` 指数取正号，并默认带 \(1/n\) 归一化（`norm="backward"` 模式）：

\[
a_m = \frac{1}{n}\sum_{k=0}^{n-1} A_k \exp\left\{2\pi i \frac{mk}{n}\right\}
\]

由复指数的正交性，两者互逆，因此恒等式 `ifft(fft(a)) ≈ a` 成立。`norm` 还有 `"ortho"`（两边各除 \(\sqrt{n}\)）和 `"forward"`（归一化挪到正变换）两种模式，具体缩放规则留到 u3-l2 详讲，本讲只需知道 `norm` 这个参数会被一路透传下去。

**认知二：调度装饰器（来自 u2-l4）。**

`fft`/`ifft` 头上都顶着 `@array_function_dispatch(_fft_dispatcher)`。这个装饰器做两件事：把函数的 `__module__` 挂到 `numpy.fft`，并包一层 NEP 18 的 `__array_function__` 协议，让 CuPy/Dask 等第三方数组库能接管调用。`_fft_dispatcher` 是一个伴随函数，返回「本次调用里哪些参数是数组」——本讲只需知道它返回 `(a, out)`，调度逻辑本身不是本讲重点。

本讲真正要读的，是装饰器之下、C++ 后端之上的那段 Python 逻辑。

---

## 3. 本讲源码地图

本讲几乎全部内容集中在一个文件里：

| 文件 | 作用 |
| --- | --- |
| [_pocketfft.py](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft.py) | 全部 14 个变换的 Python 主逻辑。本讲精读其中的 `_raw_fft`、`fft`、`ifft` 三个函数。 |
| tests/test_pocketfft.py | 一维 FFT 的正确性测试，含朴素 DFT 参考 `fft1` 和截断/补零测试 `test_identity_long_short`，用于代码实践。 |

`_pocketfft.py` 里真正干 FFT 计算活的不是 Python 代码，而是它 import 进来的 C++ 扩展 `_pocketfft_umath`（别名 `pfu`）：

[_pocketfft.py:L48](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft.py#L48)

```python
from . import _pocketfft_umath as pfu
```

所以本讲的调用链本质是：**公开函数（`fft`/`ifft`，纯 Python 参数处理）→ 统一入口（`_raw_fft`，归一化 + 选 ufunc）→ C++ 后端（`pfu.fft` / `pfu.ifft`，真正的 FFT）**。这条链路记住，下面逐段拆。

---

## 4. 核心概念与源码讲解

### 4.1 fft(a, n, axis, norm, out)：正变换的公开函数

#### 4.1.1 概念说明

`fft` 是用户最常调用的一维傅里叶变换。它的职责非常轻——只做三件事：

1. 把任意 `array_like` 输入转成真正的 ndarray（`asarray`）。
2. 如果用户没给 `n`，就用输入数组沿 `axis` 的长度充当 `n`。
3. 把活儿整个委托给内部统一入口 `_raw_fft`，并告诉它「这是复数到复数（`is_real=False`）的正向（`is_forward=True`）变换」。

注意 `fft` 自己**完全不碰 FFT 算法**，也不算归一化——这些都在 `_raw_fft` 里。`fft` 的价值在于提供一个人性化的参数接口。

#### 4.1.2 核心流程

`fft` 的执行流程可以画成：

```
fft(a, n, axis, norm, out)
  │
  ├─ a = asarray(a)            # 保证是 ndarray
  ├─ if n is None:
  │      n = a.shape[axis]     # 默认取该轴长度
  └─ output = _raw_fft(a, n, axis,
                       is_real=False, is_forward=True,
                       norm, out)
      └─ return output
```

关键点：`is_real=False`（输入允许是复数，输出也是复数，不走 Hermitian 减半路径）、`is_forward=True`（正变换）。这两个布尔值是后面 `_raw_fft` 选 ufunc 的依据。

#### 4.1.3 源码精读

公开函数 `fft` 的完整主体（去掉 docstring）：

[_pocketfft.py:L212-L216](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft.py#L212-L216)

```python
a = asarray(a)
if n is None:
    n = a.shape[axis]
output = _raw_fft(a, n, axis, False, True, norm, out)
return output
```

逐行说明：

- `a = asarray(a)`：把列表、元组等 `array_like` 统一成 ndarray，同时不复制已经是 ndarray 的对象。
- `if n is None: n = a.shape[axis]`：用户省略 `n` 时，用输入沿 `axis` 轴的长度。注意此时 `axis` 仍可能是负数（默认 `-1`），`a.shape[axis]` 对负索引天然成立。
- `_raw_fft(a, n, axis, False, True, norm, out)`：四个布尔位置参数里，`False` 是 `is_real`、`True` 是 `is_forward`。

头上的装饰器和签名：

[_pocketfft.py:L120-L121](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft.py#L120-L121)

```python
@array_function_dispatch(_fft_dispatcher)
def fft(a, n=None, axis=-1, norm=None, out=None):
```

- `axis=-1`：默认变换最后一个轴，这是几乎所有 numpy.fft 函数的约定。
- `norm=None`：注意签名默认是 `None`，但它在语义上等价于 `"backward"`（这个等价在 `_raw_fft` 里实现，见 4.3.3）。
- `out=None`：2.0.0 新增的输出数组参数，不传则由 `_raw_fft` 自动分配。

#### 4.1.4 代码实践

**实践目标**：验证 `n` 参数的截断与补零语义，并确认 `axis` 的选择。

**操作步骤**（示例代码，可保存为脚本运行）：

```python
import numpy as np

a = np.array([1, 2, 3, 4], dtype=complex)

# (1) 不传 n：n 默认为 4
A0 = np.fft.fft(a)
print("n 默认:", A0.shape)          # 预期 (4,)

# (2) n 小于原长度：截断（只取前 n 个输入点）
A_small = np.fft.fft(a, n=2)
print("n=2 (截断):", A_small)        # 预期等于 fft([1, 2])

# (3) n 大于原长度：补零（末尾补 0 到长度 n）
A_big = np.fft.fft(a, n=8)
print("n=8 (补零):", A_big.shape)    # 预期 (8,)

# (4) axis 选择：对一个 2D 数组，沿不同轴做 fft
m = np.arange(12).reshape(3, 4)
print("axis=-1 形状:", np.fft.fft(m).shape)   # 预期 (3, 4)
print("axis=0 形状: ", np.fft.fft(m, axis=0).shape)  # 预期 (3, 4)
```

**需要观察的现象**：

- `n=2` 时输出长度为 2，且结果应等于 `np.fft.fft([1, 2])`——说明「n 小于输入长度」是**取前 n 个点**，而非均匀抽样。
- `n=8` 时输出长度为 8，是对 `[1,2,3,4,0,0,0,0]` 做 FFT。
- `axis` 改变时输出形状不变（都是 `(3,4)`），但变换施加的维度不同。

**预期结果**：截断取前缀、补零补后缀，输出沿被变换轴的长度恒等于 `n`。如果你对补零结果有疑问，可以用 `np.fft.ifft` 还原并和补零后的原数组比较——`ifft(fft(a, n=8))` 应近似 `[1,2,3,4,0,0,0,0]`。若本地未安装 numpy，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`np.fft.fft([1,2,3,4], n=2)` 的结果等于下列哪一项？
- (a) `np.fft.fft([1, 3])`
- (b) `np.fft.fft([1, 2])`
- (c) `np.fft.fft([3, 4])`

**答案**：(b)。`n` 小于输入长度时取**前 n 个点**，即 `[1, 2]`。

**练习 2**：为什么 `fft` 的签名里 `norm=None`，但文档说默认是 `"backward"`？

**答案**：因为 `None` 和 `"backward"` 的等价判断发生在 `_raw_fft` 里（`if norm is None or norm == "backward": fct = 1`）。`fft` 只负责把 `norm` 透传下去，自己不做归一化决策。

---

### 4.2 ifft(a, n, axis, norm, out)：反变换与它和 fft 的唯一差异

#### 4.2.1 概念说明

`ifft` 是 `fft` 的逆运算，对应 §2 里的 IDFT 公式。从源码角度看，`ifft` 与 `fft` **几乎完全相同**——同样 `asarray`、同样 `n is None` 取 `a.shape[axis]`、同样委托给 `_raw_fft`。

唯一的本质差异是传给 `_raw_fft` 的 `is_forward` 标志：`fft` 传 `True`，`ifft` 传 `False`。就这一个布尔位，决定了：

- 走正向 ufunc（`pfu.fft`）还是反向 ufunc（`pfu.ifft`）；
- 归一化方向是否需要 `_swap_direction` 翻转。

这也是 `_raw_fft` 被设计成统一入口的根本原因——正反变换、实复变换的差异，全都能用几个布尔参数表达。

#### 4.2.2 核心流程

```
ifft(a, n, axis, norm, out)
  │
  ├─ a = asarray(a)
  ├─ if n is None:
  │      n = a.shape[axis]
  └─ output = _raw_fft(a, n, axis,
                       is_real=False, is_forward=False,   ← 唯一差异
                       norm, out=out)
      └─ return output
```

把 4.1.2 的流程图拿过来，只把 `True` 改成 `False`，就是 `ifft`。

#### 4.2.3 源码精读

`ifft` 的主体：

[_pocketfft.py:L317-L321](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft.py#L317-L321)

```python
a = asarray(a)
if n is None:
    n = a.shape[axis]
output = _raw_fft(a, n, axis, False, False, norm, out=out)
return output
```

和 `fft` 的主体（4.1.3）并排对比，差异只有两处：

1. `_raw_fft(...)` 的第五个位置参数（`is_forward`）从 `True` 变成 `False`。
2. 关键字参数写法从 `out`（位置透传）变成 `out=out`（具名透传）——这只是书写风格差异，行为完全等价，都把用户传入的 `out` 传下去。

`is_forward=False` 这个标志会触发 `_raw_fft` 里两件事：调用 `pfu.ifft` 而非 `pfu.fft`，以及对 `norm` 做一次方向翻转（见 4.3.3 的 `_swap_direction`）。正因为反变换默认带 \(1/n\) 归一化，所以 `ifft(fft(a)) ≈ a` 成立。

#### 4.2.4 代码实践

**实践目标**：验证正反变换互逆，并体会 `n` 对 `ifft` 的截断/补零效果（与 `fft` 对称）。

**操作步骤**：

```python
import numpy as np

a = np.array([1+1j, 2-1j, 3, 4+2j])

# 正反互逆
roundtrip = np.fft.ifft(np.fft.fft(a))
print("ifft(fft(a)) ≈ a ?", np.allclose(roundtrip, a))   # 预期 True

# ifft 的 n 也会截断/补零
A = np.fft.fft(a)                       # 长度 4 的频谱
b_pad = np.fft.ifft(A, n=8)             # 输出长度 8：相当于频谱补零后反变换
print("ifft n=8 形状:", b_pad.shape)    # 预期 (8,)

# 对称性：ifft(fft(a, n=k), n=k) 应能复原长度 k 的信号
k = 6
print("fft/ifft 同 n 复原:", np.allclose(
    np.fft.ifft(np.fft.fft(a, n=k), n=k),
    np.concatenate([a, [0, 0]])))       # [1,2,3,4] 补两个 0 到长度 6
```

**需要观察的现象**：

- `ifft(fft(a))` 与 `a` 几乎相等（浮点误差范围内）。
- `ifft` 接受的 `n` 同样控制输出长度，且 `fft(..., n=k)` 再 `ifft(..., n=k)` 会复原「补零到长度 k」的原始信号。

**预期结果**：两个 `allclose` 都应为 `True`。这正是测试文件里 `test_identity_long_short` 验证的恒等式 `ifft(fft(x, n=i), n=i) ≈ xx[0:i]`（见 [tests/test_pocketfft.py:L46-L49](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/tests/test_pocketfft.py#L46-L49)）。无法本地运行时标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`fft` 和 `ifft` 在源码层面的唯一本质差异是什么？

**答案**：传给 `_raw_fft` 的 `is_forward` 标志不同——`fft` 传 `True`，`ifft` 传 `False`。其余（`asarray`、`n` 默认、`is_real=False`、委托 `_raw_fft`）完全一致。

**练习 2**：为什么 `ifft(fft(a))` 能近似还原 `a`，而 `fft(fft(a))` 不行？

**答案**：`ifft` 默认带 \(1/n\) 归一化且指数取正号，恰好抵消 `fft` 的负号指数与无归一化；两次 `fft` 都不归一化且指数同号，得不到恒等。

---

### 4.3 _raw_fft(a, n, axis, is_real, is_forward, norm, out)：所有一维变换的统一入口

#### 4.3.1 概念说明

`_raw_fft` 是一维变换的「心脏」。`fft`、`ifft`、`rfft`、`irfft` 四个公开函数，最终全部汇入这一个函数。它的设计哲学是：**把不同变换的差异压缩成两个布尔参数 `is_real` 和 `is_forward`**，加上一个 `norm` 字符串，就能复用同一套「校验 → 算归一化系数 → 选 ufunc → 分配 out → 调后端」的骨架。

`_raw_fft` 做四件事：

1. 校验 `n` 合法性（`n < 1` 报错）。
2. 根据 `is_forward` 和 `norm` 计算归一化系数 `fct`（缩放因子）。
3. 根据 `(is_real, is_forward)` 选出后端 ufunc，并算出输出长度 `n_out`。
4. 规范化 `axis`、分配或校验 `out`，最后调用后端 ufunc。

本模块聚焦第 1、2、4 步和归一化；第 3 步（选 ufunc）放到 4.4 单独细讲。

#### 4.3.2 核心流程

```
_raw_fft(a, n, axis, is_real, is_forward, norm, out=None)
  │
  ├─ if n < 1: raise ValueError
  │
  ├─ if not is_forward:                      # 反向变换要翻转 norm 方向
  │      norm = _swap_direction(norm)
  │
  ├─ real_dtype = result_type(a.real.dtype, 1.0)   # 算归一化用的实 dtype
  ├─ 根据 norm 算 fct:
  │      None / "backward" → 1
  │      "ortho"           → reciprocal(sqrt(n))
  │      "forward"         → reciprocal(n)
  │
  ├─ n_out = n
  ├─ (选 ufunc：见 4.4，可能改写 n_out)
  │
  ├─ axis = normalize_axis_index(axis, a.ndim)
  │
  ├─ if out is None:
  │      out_dtype = real_dtype if (is_real and not is_forward) else complex
  │      out = empty_like(a, shape=…(n_out)…, dtype=out_dtype)
  │  elif out 形状不匹配:
  │      raise ValueError
  │
  └─ return ufunc(a, fct, axes=[(axis,), (), (axis,)], out=out)
```

#### 4.3.3 源码精读

**函数签名与 n 校验**：

[_pocketfft.py:L58-L60](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft.py#L58-L60)

```python
def _raw_fft(a, n, axis, is_real, is_forward, norm, out=None):
    if n < 1:
        raise ValueError(f"Invalid number of FFT data points ({n}) specified.")
```

`n < 1` 直接报 `ValueError`——这与测试 [tests/test_pocketfft.py:L20-L21](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/tests/test_pocketfft.py#L20-L21) 里 `assert_raises(ValueError, np.fft.fft, [1, 2, 3], 0)` 对应（传 `n=0` 应抛错）。

**归一化方向翻转与系数计算**：

[_pocketfft.py:L64-L76](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft.py#L64-L76)

```python
    if not is_forward:
        norm = _swap_direction(norm)

    real_dtype = result_type(a.real.dtype, 1.0)
    if norm is None or norm == "backward":
        fct = 1
    elif norm == "ortho":
        fct = reciprocal(sqrt(n, dtype=real_dtype))
    elif norm == "forward":
        fct = reciprocal(n, dtype=real_dtype)
    else:
        raise ValueError(f'Invalid norm value {norm}; should be "backward",'
                         '"ortho" or "forward".')
```

几个要点：

- **`_swap_direction` 的作用**：用户口中的 `norm` 描述的是「正反变换对里哪一边被缩放」。但 `_raw_fft` 内部统一按「当前这次调用（无论正反）的缩放」来理解 `norm`。所以做反变换（`is_forward=False`）时，要先用 `_swap_direction` 把用户语义翻成内部语义。映射表见 [_pocketfft.py:L104-L105](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft.py#L104-L105)：`backward↔forward`、`ortho→ortho`、`None→forward`（即 `ifft` 默认要除以 `n`）。
- **`fct` 是除数**：代码注释明确写 `inv_norm` 是「结果需要被除以的因子」。`backward` 不除（`fct=1`）；`forward` 除以 `n`；`ortho` 除以 \(\sqrt{n}\)。用 `reciprocal` 算的是 \(1/n\) 或 \(1/\sqrt{n}\)，最后传给 ufunc 做乘法（乘以倒数 = 除）。
- **为什么用 `real_dtype`**：`result_type(a.real.dtype, 1.0)` 会把 `float32` 保持为 `float32`、把整型提升为 `float64`，避免归一化系数意外拖累精度（详见 u2-l1 的 Type Promotion 讨论）。
- **零长度轴保护**：注释（[_pocketfft.py:L54-L57](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft.py#L54-L57)）说明，之所以用 `fct`（先算倒数再乘）而不是直接除以 `n`，是为了在零长度轴场景下避免除零。

**out 分配与 axis 规范化**：

[_pocketfft.py:L88-L99](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft.py#L88-L99)

```python
    axis = normalize_axis_index(axis, a.ndim)

    if out is None:
        if is_real and not is_forward:  # irfft, complex in, real output.
            out_dtype = real_dtype
        else:  # Others, complex output.
            out_dtype = result_type(a.dtype, 1j)
        out = empty_like(a, shape=a.shape[:axis] + (n_out,) + a.shape[axis + 1:],
                         dtype=out_dtype)
    elif ((shape := getattr(out, "shape", None)) is not None
          and (len(shape) != a.ndim or shape[axis] != n_out)):
        raise ValueError("output array has wrong shape.")
```

- `normalize_axis_index` 把负 axis 转成正向索引并做越界校验（越界抛 `IndexError`，正是 `fft` 文档承诺的异常）。
- 自动分配 `out` 时：只有 `irfft`（`is_real and not is_forward`）输出实数，其余变换（含 `fft`/`ifft`）输出复数——`result_type(a.dtype, 1j)` 强行掺入虚数单位以得到复 dtype。
- `out` 形状约束：除被变换轴必须是 `n_out` 外，其余轴必须和输入一致，否则抛 `ValueError("output array has wrong shape.")`。

最后调用后端 ufunc 的那一行放到 4.4.3 讲。

#### 4.3.4 代码实践

**实践目标**：通过亲手复算，确认 `fct`（归一化系数）和 `out` 形状校验的行为。

**操作步骤**（源码阅读 + 小实验）：

```python
import numpy as np

x = np.array([1, 2, 3, 4], dtype=float)

# (1) 三种 norm 的缩放关系
A_back = np.fft.fft(x, norm="backward")   # fct=1
A_fwd  = np.fft.fft(x, norm="forward")    # fct=reciprocal(n)
A_orth = np.fft.fft(x, norm="ortho")      # fct=reciprocal(sqrt(n))
n = len(x)
print("forward = backward / n ?",  np.allclose(A_fwd,  A_back / n))
print("ortho   = backward / sqrt(n) ?", np.allclose(A_orth, A_back / np.sqrt(n)))

# (2) out 形状不匹配应抛 ValueError
out_bad = np.empty(8, dtype=complex)      # 长度 8，但 n 默认为 4
try:
    np.fft.fft(x, out=out_bad)
except ValueError as e:
    print("捕获到预期错误:", e)

# (3) 阅读源码：在 _pocketfft.py L64-L76 处，确认 ifft 走的是哪条 norm 分支
#     （提示：is_forward=False 时先 _swap_direction，None→forward，于是 fct=reciprocal(n)）
```

**需要观察的现象**：

- 第 (1) 步两个 `allclose` 都为 `True`，验证 `fct` 的三种取值。
- 第 (2) 步抛出 `ValueError: output array has wrong shape.`。
- 第 (3) 步是阅读型实践：对照源码确认 `ifft` 默认走 `fct = reciprocal(n)`。

**预期结果**：如上。第 (2) 步如果传一个形状完全匹配的 `out`（长 4、复 dtype），则不报错且结果写入 `out`。无法运行时标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`_raw_fft` 为什么要在 `is_forward=False` 时调用 `_swap_direction(norm)`？

**答案**：因为用户传入的 `norm` 描述的是「正反变换对中哪一侧被缩放」的用户视角；而 `_raw_fft` 内部按「本次调用是否缩放」的内部视角来算 `fct`。反变换的两个视角相反，所以需要翻转一次。

**练习 2**：`fft` 输入是 `float32` 时，`fct` 的计算精度由哪个变量控制？为什么？

**答案**：由 `real_dtype = result_type(a.real.dtype, 1.0)` 控制。它会保持 `float32` 不被提升成 `float64`，从而 `sqrt`/`reciprocal` 在 `float32` 精度下计算，与输入精度匹配。

---

### 4.4 (is_real, is_forward) 决策表与后端 ufunc pfu.fft / pfu.ifft

#### 4.4.1 概念说明

这是 `_raw_fft` 最核心的一段：用 `(is_real, is_forward)` 两个布尔标志，从一个四象限决策表里选出 C++ 后端 ufunc，并算出输出长度 `n_out`。对 `fft`/`ifft` 而言（`is_real=False`），落点就是 `pfu.fft` 和 `pfu.ifft`。

`pfu` 是 C++ 扩展 `_pocketfft_umath`，里面注册了 5 个广义 ufunc（gufunc）：`fft`、`ifft`、`rfft_n_even`、`rfft_n_odd`、`irfft`。本讲只涉及前两个；`rfft_*` 和 `irfft` 的选择逻辑也在同一块代码里，一并放进决策表方便理解全貌（详见 u3-l4 和 u5-l1）。

#### 4.4.2 核心流程：四象限决策表

把 [_pocketfft.py:L78-L86](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft.py#L78-L86) 的 `if/elif` 整理成一张决策表：

| `is_real` | `is_forward` | 选中的后端 ufunc | `n_out`（输出长度） | 对应公开函数 |
|:---:|:---:|:---|:---:|:---|
| `False` | `True`  | `pfu.fft`                          | `n`        | `fft`  |
| `False` | `False` | `pfu.ifft`                         | `n`        | `ifft` |
| `True`  | `True`  | `pfu.rfft_n_even`（n 偶）/ `pfu.rfft_n_odd`（n 奇） | `n//2 + 1` | `rfft` |
| `True`  | `False` | `pfu.irfft`                        | `n`        | `irfft` |

观察：

- 复数变换（`is_real=False`，即 `fft`/`ifft`）输出长度恒为 `n`，正反向只换 ufunc。
- 实数正变换（`rfft`）输出长度缩短为 `n//2+1`（利用 Hermitian 对称丢掉冗余的负频率），且因奇偶 `n` 下 Nyquist 位置不同，要拆成两个 ufunc。
- 这张表正是「公开函数 → 后端 ufunc」的完整映射。

#### 4.4.3 源码精读

决策逻辑本体：

[_pocketfft.py:L78-L86](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft.py#L78-L86)

```python
    n_out = n
    if is_real:
        if is_forward:
            ufunc = pfu.rfft_n_even if n % 2 == 0 else pfu.rfft_n_odd
            n_out = n // 2 + 1
        else:
            ufunc = pfu.irfft
    else:
        ufunc = pfu.fft if is_forward else pfu.ifft
```

对 `fft`/`ifft`，走最后的 `else` 分支：`is_real=False`，所以 `ufunc = pfu.fft if is_forward else pfu.ifft`，`n_out` 保持 `n` 不变。本讲关心的就是这一行——`fft` 选 `pfu.fft`、`ifft` 选 `pfu.ifft`。

选定 ufunc 后，最后一行真正发起计算：

[_pocketfft.py:L101](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft.py#L101)

```python
    return ufunc(a, fct, axes=[(axis,), (), (axis,)], out=out)
```

这一行把 Python 层和 C++ 层缝合起来：

- `a` 是输入数组，`fct` 是 4.3 算出的归一化系数。
- `axes=[(axis,), (), (axis,)]` 是 gufunc 的核心/广播轴描述：输入 `a` 沿 `(axis,)` 这一维是「待变换的 1D 向量」、`fct` 是标量 `()`、输出沿 `(axis,)` 是变换结果。其余维度被当作批处理（向量化）维度。这正是 `pfu.fft` 能一次处理整批 1D 向量的原因（详见 u5-l1/u5-l2）。
- `out=out` 把结果写入已分配好的输出数组。

至此，从 `fft(a)` 到 C++ 计算的完整链路闭合。

#### 4.4.4 代码实践

**实践目标**：把决策表从源码「翻译」成可观察的行为——验证 `fft`/`ifft` 输出长度恒为 `n`，并用朴素 DFT 参考 `fft1` 交叉验证后端计算正确。

**操作步骤**：

```python
import numpy as np

# 朴素 DFT 参考（仿照 tests/test_pocketfft.py 的 fft1）
def fft1(x):
    L = len(x)
    phase = -2j * np.pi * (np.arange(L) / L)
    phase = np.arange(L).reshape(-1, 1) * phase
    return np.sum(x * np.exp(phase), axis=1)

x = np.random.random(30) + 1j * np.random.random(30)

# (1) 决策表：fft / ifft 的输出长度恒等于 n
for n in (10, 30, 50):                  # 小于、等于、大于原长度
    print(f"n={n}: fft 长度 {np.fft.fft(x, n=n).shape}, "
          f"ifft 长度 {np.fft.ifft(x, n=n).shape}")

# (2) 交叉验证：pfu.fft 的结果应等于朴素 DFT
print("与朴素 DFT 一致:", np.allclose(np.fft.fft(x), fft1(x), atol=1e-6))

# (3) 画出决策表（打印即可）
table = [
    ("False", "True",  "pfu.fft",                          "n"),
    ("False", "False", "pfu.ifft",                         "n"),
    ("True",  "True",  "pfu.rfft_n_even/rfft_n_odd",       "n//2+1"),
    ("True",  "False", "pfu.irfft",                        "n"),
]
print("is_real | is_forward | ufunc            | n_out")
for row in table:
    print(" | ".join(f"{c:<6}" for c in row[:2]), "|", f"{row[2]:<18}", "|", row[3])
```

**需要观察的现象**：

- 第 (1) 步无论 `n` 取 10/30/50，`fft` 和 `ifft` 的输出长度都等于 `n`（复数变换不缩短）。
- 第 (2) 步 `allclose` 为 `True`，证明 `pfu.fft` 与 O(n²) 朴素 DFT 数值一致，只是快得多（FFT 是 O(n log n)）。
- 第 (3) 步打印出的就是 4.4.2 的决策表。

**预期结果**：如上。第 (2) 步若在本地运行，`fft1` 与 `np.fft.fft` 在 `atol=1e-6` 下一致；这正是测试 [tests/test_pocketfft.py:L78-L80](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/tests/test_pocketfft.py#L78-L80) 的断言。无法运行时标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`pfu.fft` 和 `pfu.ifft` 分别对应决策表里的哪一格？依据是哪两个标志？

**答案**：`pfu.fft` 对应 `(is_real=False, is_forward=True)`，`pfu.ifft` 对应 `(is_real=False, is_forward=False)`。依据是 `_raw_fft` 的 `else: ufunc = pfu.fft if is_forward else pfu.ifft`。

**练习 2**：为什么 `rfft`（`is_real=True, is_forward=True`）要拆成 `rfft_n_even` 和 `rfft_n_odd` 两个 ufunc，而 `fft` 只需要一个？

**答案**：`fft` 输出长度恒为 `n`，正反对称简单；`rfft` 利用 Hermitian 对称把输出压缩到 `n//2+1`，而 `n` 为奇偶时 Nyquist 频率位置不同、压缩方式不同，故后端需要两个 ufunc 分别处理偶/奇 `n`。

**练习 3**：最后一行 `ufunc(a, fct, axes=[(axis,), (), (axis,)], out=out)` 中，`fct` 的形状为什么用 `()`（空元组）？

**答案**：`fct` 是一个标量（归一化系数），在 gufunc 的轴描述里标量对应空元组 `()`，表示它没有「核心维度」，只参与广播。

---

## 5. 综合实践

把本讲四个模块串起来，做一个「跟踪一次 `ifft(fft(a))` 完整调用链」的综合任务。

**任务**：给定信号 `a = [1, 2, 3, 4, 5]`（实数），请你以「源码阅读者」的身份，写下 `np.fft.ifft(np.fft.fft(a))` 这一次调用在 `_pocketfft.py` 内部经过的每一步，并填出关键变量的取值。

**建议步骤**：

1. **入口层（4.1）**：`fft(a)` 内部先 `asarray(a)`，因 `n is None` 取 `n = a.shape[-1] = 5`，然后调用 `_raw_fft(a, 5, -1, False, True, None, None)`。记录下 `is_real=False`、`is_forward=True`。
2. **统一入口（4.3）**：在 `_raw_fft` 里，`n=5 ≥ 1` 通过校验；`is_forward=True` 故不调 `_swap_direction`；`norm is None` 命中 `fct = 1`；`real_dtype` 由 `a` 的 `float64` 决定。
3. **决策表（4.4）**：`is_real=False` 走 `else`，`is_forward=True` 选 `ufunc = pfu.fft`，`n_out = 5`；因 `out is None` 且非 irfft，`out_dtype` 取复数，`empty_like` 分配形状 `(5,)` 的复数组；最后 `pfu.fft(a, 1, axes=[(-1,), (), (-1,)])` 返回长度 5 的复频谱（注意实输入下频谱 Hermitian 对称）。
4. **反变换**：把第 1 步换成 `ifft`，`is_forward=False`，于是先 `_swap_direction(None) → "forward"`，`fct = reciprocal(5)`（即 `1/5`），决策表选 `pfu.ifft`。最终结果乘了 `1/5`，与正变换的无缩放抵消，还原出原信号。
5. **验证**：用代码 `np.allclose(np.fft.ifft(np.fft.fft([1,2,3,4,5])), [1,2,3,4,5])` 确认结果为 `True`。

**预期产出**：一张标注了 `is_real / is_forward / norm / fct / ufunc / n_out / out_dtype` 七个变量在「fft 这一次」和「ifft 这一次」各自取值的对照表，并用一行 `allclose` 验证恒等性。这个练习把本讲的「公开函数 → 统一入口 → 决策表 → 后端 ufunc」整条链路走了一遍。

---

## 6. 本讲小结

- `fft` 和 `ifft` 都是极薄的 Python 外壳：`asarray` + `n` 默认取轴长 + 委托 `_raw_fft`，自身不碰 FFT 算法。
- 二者唯一本质差异是传给 `_raw_fft` 的 `is_forward`：`fft` 传 `True`、`ifft` 传 `False`。
- `n` 参数控制输出长度：小于输入长度则取前 `n` 个点（截断），大于则末尾补零；`axis` 选择被变换的轴，默认 `-1`。
- `_raw_fft` 是所有一维变换的统一入口，用 `is_real`/`is_forward` 两个布尔标志把差异压缩成一张四象限决策表。
- 归一化系数 `fct`（即 `inv_norm`）在 `_raw_fft` 内计算，反变换通过 `_swap_direction` 翻转 `norm` 语义；用倒数相乘而非直接除，以保护零长度轴并匹配输入精度。
- 对 `fft`/`ifft`，决策表落点是 `pfu.fft` / `pfu.ifft` 两个 C++ gufunc，输出长度恒为 `n`；最后一行 `ufunc(a, fct, axes=[(axis,), (), (axis,)], out=out)` 把 Python 层与 C++ 后端缝合。

---

## 7. 下一步学习建议

本讲打通了一维、复数到复数变换的主链路。接下来建议：

1. **u3-l2 norm 归一化与 _swap_direction**：本讲只点到 `_swap_direction` 和三种 `fct`，下一讲会深挖 `backward/ortho/forward` 的数学含义、`_SWAP_DIRECTION_MAP` 的设计，以及 `reciprocal`/`sqrt` 的精度处理。
2. **u3-l3 out 参数与类型提升**：本讲展示了 `out` 的形状校验，下一讲会系统讲 `out` 的 dtype 决策、自动分配规则和 `float32→float64` 的提升行为。
3. **u3-l4 rfft/irfft 与 Hermitian 对称**：本讲决策表里 `is_real=True` 的两格（`rfft`/`irfft`）留到下一讲展开，理解实输入频谱为何能减半、`irfft` 默认偶数长度带来的奇偶歧义。
4. 想直接看后端实现的读者，可以跳到 **u5-l1/u5-l2** 读 `_pocketfft_umath.cpp`，看 `pfu.fft` 这个 gufunc 是如何注册和向量化执行的。
