# out 参数、类型提升与输出 dtype

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `fft` / `ifft` / `rfft` / `irfft` 的 `out` 参数从公开函数一路透传到内部统一入口 `_raw_fft` 的完整路径。
- 在 `out=None`（默认）时，准确推断 `_raw_fft` 会用哪种 dtype 自动分配输出数组——理解「`irfft` 输出实数、其余变换输出复数」这条核心决策的代码落点。
- 理解 `result_type(a.dtype, 1j)` 与 `result_type(a.real.dtype, 1.0)` 这两个表达式如何把 `float32` 提升为 `complex64`、把整数提升为 `complex128`。
- 在 `out` 已给出时，看懂 `_raw_fft` 对其形状做的校验，以及校验失败抛出的那句 `ValueError`。
- 读懂 `_pocketfft.pyi` 里那一长串 `@overload` 是如何把上述 dtype 提升规则「翻译」给类型检查器（mypy 等）的。

## 2. 前置知识

本讲建立在前几讲之上，下面几个概念默认你已经掌握（若不熟可先回顾 u3-l1、u3-l2）：

- **`_raw_fft` 是所有一维变换的统一入口**：`fft` / `ifft` / `rfft` / `irfft` 都只是极薄的 Python 外壳，真正干活的是 `_raw_fft(a, n, axis, is_real, is_forward, norm, out=None)`，它再调用 C++ 后端 `pfu`（即 `_pocketfft_umath`）里的 gufunc。
- **`(is_real, is_forward)` 两个布尔决定走哪个后端**：复数变换用 `pfu.fft` / `pfu.ifft`，实数正变换用 `pfu.rfft_n_even` / `pfu.rfft_n_odd`，实数反变换用 `pfu.irfft`。
- **`n_out` 不一定等于 `n`**：复数变换输出长度恒为 `n`；`rfft` 借 Hermitian 对称只输出 `n//2+1` 个点；`irfft` 输出长度为 `n`。
- **`real_dtype` 与 `fct`**：上一讲（u3-l2）已讲过 `real_dtype = result_type(a.real.dtype, 1.0)` 用于把归一化系数 `fct` 的计算精度兜底；本讲会看到它还「一物二用」地充当 `irfft` 的输出 dtype。
- **类型存根 `.pyi`**：它不参与运行时，只是给类型检查器看的「说明书」（见 u1-l2）。

几个本讲新用到的基础术语：

- **`out` 参数（输出缓冲区）**：NumPy 里很多函数接受一个预先分配好的数组 `out`，函数把结果直接写进它，避免新建数组。这是省内存、支持原地复用的常见手段。
- **类型提升（type promotion）**：两种 dtype 参与运算时，NumPy 按一套规则选出一个能同时表示两者的「公共 dtype」。例如实数与复数运算结果必为复数。
- **NEP 50 弱类型**：Python 标量（如 `1j`、`1.0`）在提升中是「弱」的，会尽量贴合数组 dtype 的精度，而不是把 Python 默认的 `complex128`/`float64` 强行加上去。这正是 `result_type(float32, 1j)` 得到 `complex64` 而非 `complex128` 的原因。

## 3. 本讲源码地图

本讲只涉及两个文件，且绝大部分篇幅集中在 `_raw_fft` 这一个函数的后半段：

| 文件 | 作用 | 本讲用到的关键位置 |
|---|---|---|
| [`_pocketfft.py`](_pocketfft.py) | 全部 14 个变换的 Python 主逻辑 | `_raw_fft` 的 `out` 处理段（L90-101）；四个公开函数透传 `out`（fft L215、ifft L320、rfft L417、irfft L525） |
| [`_pocketfft.pyi`](_pocketfft.pyi) | 14 个变换的类型存根 | `fft` 的多组 `@overload`（L36-116）；`irfft` 的 `@overload`（L269-349） |

> 阅读建议：先把 `_raw_fft` 第 90–101 行整段读一遍（只有 12 行），它就是本讲全部内容的「心脏」。

## 4. 核心概念与源码讲解

### 4.1 out 参数：从公开函数到 _raw_fft 的透传

#### 4.1.1 概念说明

`out` 是 NumPy 通用的一种参数：调用者预先 `np.empty(...)` 一块形状与 dtype 都合适的数组，把它作为 `out=` 传进去，函数就把结果**原地写入**这块数组，并返回它。好处是：

- **省一次内存分配**：对大数组或循环里反复调用很有意义。
- **复用同一块缓冲区**：流水线处理时可以反复写同一个 `out`。

`numpy.fft` 的 `out` 参数是 **NumPy 2.0.0 才新增**的（见各函数 docstring 里的 `.. versionadded:: 2.0.0`）。它的设计原则很简单：**公开函数只负责把 `out` 原样转交，不解析、不校验**；所有「要不要自动分配、要不要校验形状」的逻辑都集中在 `_raw_fft` 一处。

#### 4.1.2 核心流程

```text
用户调用 fft(a, out=buf)
   └─ fft(): a = asarray(a); 取 n; 把 out 透传给 _raw_fft
        └─ _raw_fft(a, n, axis, is_real, is_forward, norm, out=buf)
             └─ 决定 ufunc、n_out、out_dtype
             └─ if out is None: 自动分配
                else:          校验 buf 形状
             └─ return ufunc(a, fct, axes=[...], out=out)   ← out 最终喂给 C++ gufunc
```

关键点：`out` 的最终归宿是 `ufunc(..., out=out)`——也就是说，「把结果写进 `out`」这件真正的事，是由 C++ 层的广义 ufunc 完成的，Python 层只决定 `out` 是「新分配的」还是「用户给的」。

#### 4.1.3 源码精读

四个公开函数的函数体都极薄，且都把 `out` 透传给 `_raw_fft`。以 `fft` 为例：

[_pocketfft.py:212-216](_pocketfft.py#L212-L216)：`fft` 把收到的 `out` 原样转交给 `_raw_fft`（注意这里是第 7 个**位置参数**，没有写 `out=`）。

```python
a = asarray(a)
if n is None:
    n = a.shape[axis]
output = _raw_fft(a, n, axis, False, True, norm, out)
return output
```

其余三个变换则用**关键字** `out=out` 透传，效果完全等价（`out` 是 `_raw_fft` 的最后一个参数）：

[_pocketfft.py:320](_pocketfft.py#L320)：`ifft` 透传 `out=out`。
[_pocketfft.py:417](_pocketfft.py#L417)：`rfft` 透传 `out=out`。
[_pocketfft.py:522-525](_pocketfft.py#L522-L525)：`irfft` 先按默认规则推断 `n = (a.shape[axis] - 1) * 2`，再透传 `out=out`。

接收端 `_raw_fft` 的签名，`out=None` 作为默认值：

[_pocketfft.py:58](_pocketfft.py#L58)：`def _raw_fft(a, n, axis, is_real, is_forward, norm, out=None)`。

> 小观察：`fft` 用位置参数传 `out`、其余三个用关键字，这是源码里一处**风格不一致但行为一致**的细节。读源码时若被它绊住，知道它们等价即可。

#### 4.1.4 代码实践

**实践目标**：验证「`out` 被原地写入，且返回值就是传入的同一个对象」。

**操作步骤**：

```python
import numpy as np
a = np.arange(8.0)

buf = np.empty(8, dtype=np.complex128)   # 预分配形状、dtype 都对的复数缓冲区
r = np.fft.fft(a, out=buf)

print(r is buf)          # 是否同一个对象？
print(np.allclose(r, np.fft.fft(a)))   # 数值是否正确？
print(buf[:3])           # buf 是否真的被写入了？
```

**需要观察的现象**：`r is buf` 应为 `True`；`buf` 的前几个元素不再是 `empty` 的垃圾值，而是真实的 FFT 结果。

**预期结果**：`True` / `True`，且 `buf` 内容与 `np.fft.fft(a)` 完全一致。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `buf` 换成长度为 4 的缓冲区（`np.empty(4, dtype=complex)`）传给 `fft(a)`，会在哪一行代码、抛出什么异常？

**答案**：会在 [`_pocketfft.py:97-99`](_pocketfft.py#L97-L99) 的形状校验处抛出 `ValueError("output array has wrong shape.")`。因为 `n=8`，`out` 沿 `axis` 的长度必须是 `n_out=8`，而传入的 `buf` 长度是 4，`shape[axis] != n_out` 成立。（形状错误与 dtype 错误由不同层负责，详见 4.3。）

**练习 2**：为什么 `fft` 的函数体里看不到任何对 `out` 形状/dtype 的检查？

**答案**：因为 `_raw_fft` 被设计成所有一维变换的**唯一**入口，把 `out` 的处理逻辑集中在一处（L90-101）能避免 14 个公开函数各写一遍。`fft`/`ifft`/`rfft`/`irfft` 只做「转交」。

---

### 4.2 out 为 None 时：自动分配输出与 dtype 决策

#### 4.2.1 概念说明

绝大多数调用都不传 `out`（即 `out=None`），此时 `_raw_fft` 必须自己 `empty_like` 一个新数组来承接结果。这个新数组用哪种 dtype？这是本讲最核心的决策，规则只有一句话：

> **`irfft` 输出实数，其余所有变换输出复数。**

这是因为：

- `fft` / `ifft`：复→复，输出当然是复数。
- `rfft`：实→复（虽然输入是实数，但频谱有相位，是复数）。
- `irfft`：复→实（输入是 `rfft` 给出的非负频率复数，输出重建出的时域实信号）。

代码用 `if is_real and not is_forward:` 这一个条件就把 `irfft` 单独挑了出来（`is_real=True` 且 `is_forward=False` 的唯一组合就是 `irfft`）。

#### 4.2.2 核心流程

```text
if out is None:
    if is_real and not is_forward:     # 唯一的 irfft 分支
        out_dtype = real_dtype         # 实数输出
    else:                              # fft/ifft/rfft
        out_dtype = result_type(a.dtype, 1j)   # 复数输出
    out = empty_like(a, shape=(...把 axis 维替换成 n_out...), dtype=out_dtype)
```

两个 dtype 来源：

- **实数输出**：直接复用 4.1 提到的 `real_dtype = result_type(a.real.dtype, 1.0)`（在函数开头 L67 就算好了）。
- **复数输出**：`result_type(a.dtype, 1j)`——把输入 dtype 与 Python 复数字面量 `1j` 做「提升」，结果必为复数。

新数组的形状 = 输入形状，但把 `axis` 那一维替换成 `n_out`。

#### 4.2.3 源码精读

`_raw_fft` 自动分配输出的这 7 行是本讲的「心脏」：

[_pocketfft.py:90-96](_pocketfft.py#L90-L96)：`out` 缺省时，按「实/复」分流决定 `out_dtype`，再用 `empty_like` 分配。

```python
if out is None:
    if is_real and not is_forward:  # irfft, complex in, real output.
        out_dtype = real_dtype
    else:  # Others, complex output.
        out_dtype = result_type(a.dtype, 1j)
    out = empty_like(a, shape=a.shape[:axis] + (n_out,) + a.shape[axis + 1:],
                     dtype=out_dtype)
```

两个要点：

1. **`real_dtype` 的来源**（在函数更上方计算）：[_pocketfft.py:67](_pocketfft.py#L67) `real_dtype = result_type(a.real.dtype, 1.0)`。它本是为 `fct` 归一化系数算的（见 u3-l2），这里被**一物二用**：因为 `irfft` 输出的实数精度理应与 `fct` 的精度口径一致，于是直接拿来当 `out_dtype`。注释 `# irfft, complex in, real output.` 点明了这一点。

2. **形状拼接 `a.shape[:axis] + (n_out,) + a.shape[axis + 1:]`**：把 `axis` 那一维替换为 `n_out`，其余维度照抄。例如 `a.shape=(5,8)`、`axis=1`、`n_out=8`（复数变换）→ 输出 `(5,8)`；若 `n_out=5`（`rfft`，`8//2+1`）→ 输出 `(5,5)`。

`n_out` 本身在前几行就已根据 `(is_real, is_forward)` 定好：[_pocketfft.py:78-82](_pocketfft.py#L78-L82)（`rfft` 时 `n_out = n//2+1`，其余 `n_out = n`）。

#### 4.2.4 代码实践

**实践目标**：用不同 dtype 的输入，验证 `out_dtype` 决策与「实/复」分流。

**操作步骤**：

```python
import numpy as np
print(np.fft.fft(np.zeros(4, dtype=np.float32)).dtype)   # 实→复
print(np.fft.fft(np.zeros(4, dtype=np.float64)).dtype)
print(np.fft.fft(np.zeros(4, dtype=np.int32)).dtype)
print(np.fft.rfft(np.zeros(8, dtype=np.float32)).dtype)  # 实→复（且长度 8//2+1=5）
print(np.fft.irfft(np.zeros(5, dtype=np.complex64)).dtype)  # 复→实
print(np.fft.irfft(np.zeros(5, dtype=np.complex128)).dtype)
```

**需要观察的现象**：`fft` / `rfft` 的输出永远是复数 dtype；`irfft` 的输出永远是实数 dtype。

**预期结果**（由 `_pocketfft.pyi` 的提升表保证）：

| 调用 | 输出 dtype |
|---|---|
| `fft(float32)` | `complex64` |
| `fft(float64)` | `complex128` |
| `fft(int32)` | `complex128` |
| `rfft(float32)` | `complex64` |
| `irfft(complex64)` | `float32` |
| `irfft(complex128)` | `float64` |

#### 4.2.5 小练习与答案

**练习 1**：`irfft` 输出 dtype 为什么用 `real_dtype`（`result_type(a.real.dtype, 1.0)`），而不是另写一个 `result_type(a.dtype, 1.0)`？

**答案**：因为 `irfft` 的输入 `a` 是复数，`a.real.dtype` 取的是其**实部分量的浮点精度**（`complex64→float32`、`complex128→float64`），这正是我们希望输出实信号保持的精度。若写 `result_type(a.dtype, 1.0)`，对 `complex64` 输入会得到 `complex64`（提升不出实数），语义就错了。复用 `real_dtype` 还顺带保证输出精度与归一化系数 `fct` 同口径。

**练习 2**：对 `rfft`，`is_real=True, is_forward=True`，它会走哪个 `out_dtype` 分支？输出是实数还是复数？

**答案**：走 `else` 分支（因为条件 `is_real and not is_forward` 要求 `not is_forward` 为真，而 `rfft` 是 forward），`out_dtype = result_type(a.dtype, 1j)`，输出是**复数**。`rfft` 输入实、输出复——这与很多人「实数变换应该输出实数」的直觉相反，需要记住。

---

### 4.3 out 已给出时：形状校验与 ufunc 透传

#### 4.3.1 概念说明

当用户传了 `out`（非 `None`），`_raw_fft` 就**不再自动分配**，转而做一次轻量校验，然后把 `out` 直接喂给底层 gufunc。这里有一个非常关键、容易踩坑的分工：

- **Python 层（`_raw_fft`）只校验形状**：维度数对不对、`axis` 那一维长度是不是 `n_out`。
- **dtype 是否兼容，由 ufunc 自己负责**：当 `ufunc(..., out=out)` 执行时，NumPy 的 ufunc 机制会检查「计算结果能否安全地写入 `out` 的 dtype」，不能就抛 casting（类型转换）错误。

换句话说：**传错形状 → Python 层 `ValueError`；传错 dtype → ufunc 层 casting 错误**，两者来源不同、报错信息也不同。

#### 4.3.2 核心流程

```text
elif out is not None:
    shape = getattr(out, "shape", None)         # 没有 .shape 属性则跳过校验
    if shape is not None and (len(shape) != a.ndim or shape[axis] != n_out):
        raise ValueError("output array has wrong shape.")

return ufunc(a, fct, axes=[(axis,), (), (axis,)], out=out)   # out 透传给 C++ gufunc
```

`getattr(out, "shape", None)` 这一处很巧：如果 `out` 不是 ndarray（没有 `shape` 属性，比如某些伪数组对象），就**不做形状校验**，直接交给 ufunc 去处理，让 ufunc 用它自己的规则报错。

#### 4.3.3 源码精读

形状校验 + ufunc 透传，全在这 4 行：

[_pocketfft.py:97-99](_pocketfft.py#L97-L99)：`out` 非空时校验其形状。

```python
elif ((shape := getattr(out, "shape", None)) is not None
      and (len(shape) != a.ndim or shape[axis] != n_out)):
    raise ValueError("output array has wrong shape.")
```

校验条件拆开看：

- `len(shape) != a.ndim`：`out` 的维数必须和输入 `a` 一致（FFT 不改变维数，只改变 `axis` 那一维的长度）。
- `shape[axis] != n_out`：`out` 沿被变换轴的长度，必须等于本次变换的输出长度 `n_out`（复数变换是 `n`，`rfft` 是 `n//2+1`）。

注意这里**只查形状，完全不查 dtype**。

[_pocketfft.py:101](_pocketfft.py#L101)：把 `out` 透传给 C++ gufunc，由后者把结果写进去。

```python
return ufunc(a, fct, axes=[(axis,), (), (axis,)], out=out)
```

`axes=[(axis,), (), (axis,)]` 是广义 ufunc 的轴规约：输入 `a` 沿 `(axis,)` 这条核心轴变换，标量 `fct` 无轴，输出沿 `(axis,)` 写回。`out=out` 是标准 ufunc 关键字，NumPy 据此把结果写入 `out`，并在 dtype 不兼容时抛出 casting 错误。

#### 4.3.4 代码实践

**实践目标**：分别触发「形状错误」与「dtype 错误」两种失败，体会两层校验的分工。

**操作步骤**：

```python
import numpy as np
a = np.arange(8.0)

# (1) 形状错误：axis 维长度不是 n_out(=8)
try:
    np.fft.fft(a, out=np.empty(5, dtype=complex))
except ValueError as e:
    print("形状错误 ->", repr(e))

# (2) dtype 错误：fft 输出复数，却给一个实数 out
try:
    np.fft.fft(a, out=np.empty(8, dtype=np.float64))
except Exception as e:
    print("dtype 错误 ->", type(e).__name__, ":", e)
```

**需要观察的现象**：

- 第 (1) 种抛 `ValueError`，消息**正好是**字符串 `"output array has wrong shape."`（来自源码 L99，可逐字引用）。
- 第 (2) 种**不是**上面那句 `ValueError`，而是 ufunc 在写入 `out` 时报告的输出转换（casting）错误。

**预期结果**：第 (1) 种 `ValueError('output array has wrong shape.')`；第 (2) 种属于 NumPy ufunc 的输出 casting 错误类别（具体异常名与措辞随 NumPy 版本可能变化，精确文案**待本地验证**）。两者消息不同，正好印证「形状由 Python 层把关、dtype 由 ufunc 层把关」。

#### 4.3.5 小练习与答案

**练习 1**：为什么校验里要写 `getattr(out, "shape", None) is not None`，而不是直接 `out.shape`？

**答案**：为了对「没有 `.shape` 属性的对象」更宽容。若 `out` 是某个不暴露 `shape` 的类（理论上可被 `__array_function__` 协议接管的第三方对象），`getattr(..., None)` 会返回 `None`，于是跳过形状校验，把判断权下放给 ufunc，而不是在 Python 层因 `AttributeError` 崩掉。

**练习 2**：若给 `irfft` 传一个 `dtype=complex128` 的 `out`（而 `irfft` 本应输出实数 `float64`），会怎样？

**答案**：**形状**校验会通过（`irfft` 的 `n_out=n`，只要长度对就行，dtype 不查）；但在 [`_pocketfft.py:101`](_pocketfft.py#L101) 执行 `ufunc(..., out=out)` 时，gufunc 试图把实数结果写入复数 `out`。复数←实数通常是「安全」的向上转换（可写），所以大概率**不报错**、结果被存成复数（虚部为 0）。这提醒我们：**`out` 的 dtype 要自己保证正确**，Python 层不会替你把关。精确行为**待本地验证**。

---

### 4.4 类型提升规则与 .pyi 中的 out 重载

#### 4.4.1 概念说明

4.2 节我们看到两个决定 dtype 的提升表达式：

- `result_type(a.dtype, 1j)` —— 复数输出（`fft`/`ifft`/`rfft`）。
- `result_type(a.real.dtype, 1.0)` —— 实数输出（`irfft`，即 `real_dtype`）。

`result_type(x, y)` 的含义是「找一个能同时容纳 `x` 和 `y` 的最小公共 dtype」。这里 `1j`、`1.0` 是 Python 标量，在 NEP 50 下是**弱类型**：它们不会强行把数组拉到 `complex128`/`float64`，而是尽量贴合数组自身的精度。于是：

- 实数数组 + `1j` → 同精度的复数：`float32 → complex64`、`float64 → complex128`、`longdouble → clongdouble`。
- 整数数组 + `1j` → `complex128`（因为不存在「整数对应的低精度复数」，且 `complex64` 的 `float32` 分量无法安全表示大整数，故提升到 `complex128`）。

这套规则在 `_pocketfft.pyi` 里被**逐 dtype**写成了大量 `@overload`，这样 mypy/pyright 等类型检查器就能在**不运行代码**的前提下，推断出每个调用的返回 dtype。

#### 4.4.2 核心流程

两条提升链，对照一张权威映射表（取自 `.pyi`）：

```text
复数输出链： result_type(a.dtype, 1j)
   float32 / float16  ─┐
   float64 / int / bool├──→  见下表「复数输出」
   longdouble          │
   complex64/128       ─┘

实数输出链（仅 irfft）： result_type(a.real.dtype, 1.0)   即 real_dtype
   complex64   → float32
   complex128  → float64
   clongdouble → longdouble
   int/bool    → float64
```

`.pyi` 还为「显式传 `out`」单列了一条 overload：返回类型就是 `out` 自己的类型（`ArrayT`），因为结果被写进了你给的数组。

#### 4.4.3 源码精读

**复数输出的 dtype 决策点**（已在 4.2 引用）：[_pocketfft.py:94](_pocketfft.py#L94) `out_dtype = result_type(a.dtype, 1j)`。

`.pyi` 把 `result_type(a.dtype, 1j)` 的结果**逐项**写成 overload，构成下表（以 `fft` 为例，其余复数输出变换同构）：

| 输入 dtype（来自 `_pocketfft.pyi`） | `result_type(a.dtype, 1j)` = 输出 dtype | 对应 overload |
|---|---|---|
| `complex128` / `complex64` | 与输入相同 | [_pocketfft.pyi:36-43](_pocketfft.pyi#L36-L43) |
| `float64` / `integer` / `bool` | `complex128` | [_pocketfft.pyi:44-51](_pocketfft.pyi#L44-L51) |
| `float32` / `float16` | `complex64` | [_pocketfft.pyi:52-59](_pocketfft.pyi#L52-L59) |
| `longdouble` | `clongdouble` | [_pocketfft.pyi:60-67](_pocketfft.pyi#L60-L67) |

这张表就是「`float32 → complex64`、整数 → `complex128`」这条提升规则的权威出处。

**实数输出（`irfft`）的 dtype 决策点**：[_pocketfft.py:92](_pocketfft.py#L92) `out_dtype = real_dtype`，而 `real_dtype` 来自 [_pocketfft.py:67](_pocketfft.py#L67)。`.pyi` 里 `irfft` 的 overload 同样逐项给出：

| 输入 dtype（来自 `_pocketfft.pyi`） | `irfft` 输出 dtype | 对应 overload |
|---|---|---|
| `floating`（`float32`/`float64`） | 与输入相同 | [_pocketfft.pyi:269-276](_pocketfft.pyi#L269-L276) |
| `complex128` / `integer` / `bool` | `float64` | [_pocketfft.pyi:277-284](_pocketfft.pyi#L277-L284) |
| `complex64` | `float32` | [_pocketfft.pyi:285-292](_pocketfft.pyi#L285-L292) |
| `clongdouble` | `longdouble` | [_pocketfft.pyi:293-300](_pocketfft.pyi#L293-L300) |

注意 `irfft` 表里出现了「`floating` 输入」：虽然语义上 `irfft` 的输入应是复数频谱，但实现并不禁止实数输入（会被当作虚部为 0 处理），此时输出 dtype 沿用输入的浮点精度。

**显式 `out` 的 overload**：每个函数最后都有一条「`out` 已给出」的专用 overload，返回类型是 `out` 的类型变量 `ArrayT`。以 `fft` 为例：

[_pocketfft.pyi:108-116](_pocketfft.pyi#L108-L116)：传 `out` 时，返回值类型就是 `out` 的类型。

```python
@overload  # out: <given>
def fft[ArrayT: NDArray[np.complexfloating]](
    a: _ArrayLikeNumber_co,
    n: int | None = None,
    axis: int = -1,
    norm: _NormKind = None,
    *,
    out: ArrayT,
) -> ArrayT: ...
```

这条 overload 的约束 `ArrayT: NDArray[np.complexfloating]` 还顺带告诉类型检查器：`fft` 的 `out` 必须是复数数组（呼应 4.3 的「dtype 由 ufunc 把关」）。`irfft` 的对应 overload 则约束为 `NDArray[np.floating]`：[_pocketfft.pyi:341-349](_pocketfft.pyi#L341-L349)。

> 读 `.pyi` 的小窍门：每个函数末尾那条注释为 `# out: <given>` 的 overload，就是「显式 `out`」分支；它前面的若干条注释为 `# out: None` 的 overload，按输入 dtype 枚举了自动分配时的返回 dtype，正好对应 4.2 的提升表。

#### 4.4.4 代码实践

**实践目标**：用类型检查器视角核对 dtype 提升表，并理解「传 `out` 后返回类型跟随 `out`」。

**操作步骤**（这是一段「类型阅读型」实践，可粘贴进 `mypy` 或 IDE 悬停查看类型）：

```python
import numpy as np

a32 = np.zeros(4, dtype=np.float32)
a64 = np.zeros(4, dtype=np.float64)
ai  = np.zeros(4, dtype=np.int32)

# 让 IDE/mypy 推断下列变量的 dtype，对照 4.4.3 的两张表
r1 = np.fft.fft(a32)   # 期望 complex64
r2 = np.fft.fft(a64)   # 期望 complex128
r3 = np.fft.fft(ai)    # 期望 complex128（整数提升到 complex128）

c128 = np.zeros(5, dtype=np.complex128)
r4 = np.fft.irfft(c128)  # 期望 float64

# 传 out 后，返回类型应跟随 out
out = np.empty(8, dtype=np.complex128)
r5 = np.fft.fft(a32, out=out)   # r5 的推断类型应为 complex128（即 out 的类型），而非 complex64
```

**需要观察的现象**：不传 `out` 时，返回 dtype 严格按提升表走（`a32`→`complex64`）；传 `out` 后，静态推断的返回类型变成 `out` 的类型（`complex128`），因为结果写进了 `out`。

**预期结果**：与 4.4.3 两张表完全一致。`r5 is out` 为 `True`（运行期），其静态类型标注为 `np.ndarray[..., np.dtype[np.complex128]]`。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `fft(np.float32 数组)` 的输出是 `complex64` 而不是 `complex128`？提升发生在哪一行？

**答案**：因为 [_pocketfft.py:94](_pocketfft.py#L94) 的 `result_type(a.dtype, 1j)` 中，`1j` 是 NEP 50 弱类型标量，它贴合数组精度把 `float32` 提升为同精度的 `complex64`，而非强行升到 `complex128`。`.pyi` 在 [_pocketfft.pyi:52-59](_pocketfft.pyi#L52-L59) 把这条规则写死给类型检查器。

**练习 2**：若希望 `float32` 输入也得到 `complex128` 的精度，该怎么做？

**答案**：要么先把输入手动提升 `a.astype(np.complex128)` 再 `fft`；要么预分配一个 `complex128` 的 `out` 传进去（此时 gufunc 会把 `complex64` 结果安全地向上转换写入 `complex128` 的 `out`）。后者正好对应 `.pyi` 里 [_pocketfft.pyi:108-116](_pocketfft.pyi#L108-L116) 的「`out` 已给出」分支——返回类型跟随 `out`。

**练习 3**：`.pyi` 里 `irfft` 的 `out` overload（[_pocketfft.pyi:341-349](_pocketfft.pyi#L341-L349)）把 `out` 约束为 `NDArray[np.floating]`，而 `fft` 的 `out` overload 约束为 `NDArray[np.complexfloating]`。为什么不同？

**答案**：因为 `irfft` 输出实数、`fft` 输出复数。类型存根如实反映了 4.2 的「实/复」分流：传给 `fft` 的 `out` 必须能装复数，传给 `irfft` 的 `out` 装实数即可。这也从静态层面预警了 4.3 练习 2 提到的「dtype 由 ufunc 把关」风险。

## 5. 综合实践

把本讲四块内容串起来，完成下面这个「`out` 全流程」小任务。

**任务**：对一段实信号 `a`，分别给 `fft` 和 `irfft` 预分配**形状与 dtype 都正确**的 `out`，验证结果被原地写入；再制造一次形状错误、观察报错。

```python
import numpy as np

a = np.arange(8.0)                       # 实信号，长度 8

# ---- (A) 给 fft 预分配复数 out：axis 维长度 = n_out = 8 ----
out_fft = np.empty(8, dtype=np.complex128)
r_fft = np.fft.fft(a, out=out_fft)
assert r_fft is out_fft                  # 原地写入
assert np.allclose(r_fft, np.fft.fft(a))
print("fft out 写入成功，dtype =", r_fft.dtype)

# ---- (B) 给 irfft 预分配实数 out ----
spec = np.fft.rfft(a)                    # 长度 8//2+1 = 5 的复数频谱
out_irfft = np.empty(8, dtype=np.float64)   # irfft 输出实数，长度 n=8
r_irfft = np.fft.irfft(spec, out=out_irfft)
assert r_irfft is out_irfft
assert np.allclose(r_irfft, a)           # irfft(rfft(a), 8) ≈ a
print("irfft out 写入成功，dtype =", r_irfft.dtype)

# ---- (C) 故意传 axis 维长度不匹配的 out ----
try:
    np.fft.fft(a, out=np.empty(5, dtype=np.complex128))   # 应为 8，给了 5
except ValueError as e:
    print("形状错误捕获：", e)
```

**需要观察的现象与预期结果**：

1. (A) `r_fft is out_fft` 成立，dtype 为 `complex128`，数值正确。
2. (B) `r_irfft is out_irfft` 成立，dtype 为 `float64`，且 `irfft(rfft(a))` 还原回 `a`（误差在浮点精度内）。
3. (C) 抛出 `ValueError`，消息正是 `"output array has wrong shape."`（源码 [_pocketfft.py:99](_pocketfft.py#L99)）。

**进阶**（可选）：把 (A) 的 `out_fft` 改成 `dtype=np.float64`（实数），观察它**不会**触发 (C) 那句 `ValueError`，而是在 ufunc 层报告输出 casting 错误——印证「形状 Python 把关、dtype ufunc 把关」的分工。该 casting 错误的精确文案**待本地验证**。

## 6. 本讲小结

- `out` 参数由四个公开函数（`fft`/`ifft`/`rfft`/`irfft`）原样透传给唯一的入口 `_raw_fft`，公开函数自身不解析、不校验 `out`（[L212-216](_pocketfft.py#L212-L216) 等）。
- `out=None` 时，`_raw_fft` 在 [L90-96](_pocketfft.py#L90-L96) 自动分配输出：**`irfft` 用 `real_dtype`（实数），其余用 `result_type(a.dtype, 1j)`（复数）**，形状只把 `axis` 那一维换成 `n_out`。
- `out` 已给时，Python 层在 [L97-99](_pocketfft.py#L97-L99) **只校验形状**（维数 + `axis` 维长度 == `n_out`），失败抛 `ValueError("output array has wrong shape.")`；**dtype 兼容性交给 ufunc**（[L101](_pocketfft.py#L101)）。
- 类型提升遵循 NEP 50 弱类型规则：`float32→complex64`、`float64/integer/bool→complex128`、`longdouble→clongdouble`；`irfft` 则 `complex64→float32`、`complex128→float64`。
- `_pocketfft.pyi` 把上述提升逐 dtype 写成 `@overload`，并为「显式 `out`」单列返回类型跟随 `out` 的 overload（如 [fft L108-116](_pocketfft.pyi#L108-L116)、[irfft L341-349](_pocketfft.pyi#L341-L349)），让类型检查器无需运行即可推断。

## 7. 下一步学习建议

- **下一讲 u3-l4（`rfft`/`irfft` 与 Hermitian 对称）**：本讲的 `n_out = n//2+1`、`irfft` 实数输出与 `n = (m-1)*2` 默认值，都源自实信号频谱的 Hermitian 对称性，下一讲会从数学上讲透，并展开 `irfft` 默认偶数长度带来的奇偶歧义。
- **向多维延伸（u4 单元）**：`fftn`/`rfftn` 等会把 `out` 沿用 `_raw_fftnd` 的逐轴循环，注意 docstring 里反复出现的「`out` 只对最后一次变换生效」的限制（如 [_pocketfft.py:813-816](_pocketfft.py#L813-L816)）。
- **深入后端（u5 单元）**：本讲里 `out` 最终喂给的 `ufunc(..., out=out)`，其 gufunc 注册、循环实现、`copy_output` 等细节都在 `_pocketfft_umath.cpp`，可在 u5-l1/u5-l2 看到「写入 `out`」在 C++ 层具体怎么发生。
- **建议阅读的源码**：重读 [_pocketfft.py:58-101](_pocketfft.py#L58-L101) 整个 `_raw_fft`，把它与本讲四块内容一一对应；再浏览 [_pocketfft.pyi:36-116](_pocketfft.pyi#L36-L116) 的 `fft` overload 组，体会「提升表 = overload 列表」的对应关系。
