# FFT 卷积 fftconvolve 与重叠相加 oaconvolve

## 1. 本讲目标

本讲承接 [u3-l1](u3-l1-convolve-correlate-basics.md) 讲过的「直接法卷积/相关」，进入**频域卷积**这一更高性能的实现路径。读完本讲，你应当能够：

- 说清楚为什么「在频域做乘法」可以等价于「在时域做卷积」，以及它在复杂度上为何碾压直接法。
- 跟着 `fftconvolve` 的源码走一遍：从 `axes` 归一化（`_init_freq_conv_axes`）、到频域乘（`_freq_domain_conv`）、再到按 `mode` 裁剪（`_apply_conv_mode`）的完整三段式。
- 读懂 `_freq_domain_conv` 这个被 `fftconvolve` 与 `oaconvolve` 共用的核心引擎，包括实数走 `rfftn`、复数走 `fftn` 的分支与「快速 FFT 长度」优化。
- 理解当两个输入长度悬殊时，`oaconvolve` 用「重叠相加（overlap-add）」把长信号切块、分块 FFT 卷积再拼接的原理，以及 `_calc_oa_lens` 如何用 Lambert W 函数求出理论最优块大小。
- 通过一个可运行的实践，亲手对比 `convolve(direct)`、`fftconvolve`、`oaconvolve` 三者的输出一致性与运行时间。

## 2. 前置知识

本讲假设你已经掌握（若不熟，建议先读 u3-l1）：

- **离散线性卷积**：两个长度为 \(N\) 与 \(M\) 的一维信号做「完整」卷积，结果长度为 \(N+M-1\)；`mode` 取 `full`/`same`/`valid` 只是在这个完整结果上截取不同的片段。
- **DFT / FFT**：离散傅里叶变换把时域序列变到频域；FFT 是其 \(O(L\log L)\) 的快速算法。`numpy.fft` / `scipy.fft` 都提供它。
- **卷积定理（Convolution Theorem）**：时域卷积 \(\Longleftrightarrow\) 频域逐点相乘。这是本讲全部内容的地基。

几个本讲会用到的术语：

- **循环卷积（cyclic convolution）**：把信号看成「周期性」时得到的卷积，DFT 乘法天然对应的是循环卷积。
- **补零（zero-padding）**：为了让循环卷积不发生「绕回（wrap-around）」、从而等价于线性卷积，需要把两路信号都补零到至少 \(N+M-1\) 长。
- **快速长度（fast FFT length）**：FFT 在长度为 \(2^k\) 或若干小质数乘积时最快；`scipy.fft.next_fast_len` 给出「不小于给定值、且对 FFT 友好」的长度。
- **重叠相加（Overlap-Add, OLA）**：把长信号切成块、每块单独卷积、再把块与块之间「重叠的部分」相加拼回完整结果的多速率处理技巧。

## 3. 本讲源码地图

本讲只涉及一个源码文件，但其中聚集了 6 个最小模块：

| 函数 | 行号 | 作用 |
|---|---|---|
| `fftconvolve` | [_signaltools.py:589-714](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signaltools.py#L589-L714) | 频域卷积的公开入口，编排「归一化轴 → 频域乘 → 裁剪」三步 |
| `_init_freq_conv_axes` | [_signaltools.py:424-482](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signaltools.py#L424-L482) | 统一处理 `axes` 参数：去冗余轴、校验形状、按需交换输入 |
| `_freq_domain_conv` | [_signaltools.py:485-548](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signaltools.py#L485-L548) | 真正干活的频域引擎：FFT→乘→IFFT，被 `fftconvolve`/`oaconvolve` 共用 |
| `_apply_conv_mode` | [_signaltools.py:551-586](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signaltools.py#L551-L586) | 按 `full`/`same`/`valid` 裁剪最终输出形状 |
| `_calc_oa_lens` | [_signaltools.py:717-825](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signaltools.py#L717-L825) | 为重叠相加计算「最优分块长度」，含 Lambert W 推导 |
| `oaconvolve` | [_signaltools.py:850-1066](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signaltools.py#L850-L1066) | 重叠相加法的公开入口：切块→分块频域卷积→重叠相加 |

辅助函数：`_centered`（[_signaltools.py:414-421](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signaltools.py#L414-L421)，取数组中心区域）、`_split`（[_signaltools.py:830-847](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signaltools.py#L830-L847)，Array-API 版的 `np.split`）。相关 import：`math`（[_signaltools.py:5](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signaltools.py#L5)）、`scipy.fft as sp_fft`（[_signaltools.py:17](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signaltools.py#L17)）、`scipy.special.lambertw`（[_signaltools.py:21](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signaltools.py#L21)）。

---

## 4. 核心概念与源码讲解

### 4.1 fftconvolve：频域卷积的完整编排

#### 4.1.1 概念说明

直接法卷积计算每一个输出点都要把核（kernel）滑过信号并逐点乘加。设两路信号长度为 \(N\)、\(M\)（不妨 \(N\ge M\)），完整卷积有 \(N+M-1\) 个输出点，每个点平均要做 \(O(M)\) 次乘加，于是**直接法的总复杂度是 \(O(NM)\)**。当 \(N,M\) 都很大时（比如图像与图像做卷积），这是不可接受的。

**卷积定理**给出了捷径：

\[
h = x * y \quad\Longleftrightarrow\quad H[k] = X[k]\cdot Y[k]
\]

即「时域卷积」等价于「频域逐点相乘」。于是我们只需：

1. 把 \(x\)、\(y\) 分别做 FFT；
2. 把两个频谱逐点相乘；
3. 再做一次逆 FFT（IFFT）就得到卷积结果。

这里有一个**关键陷阱**：DFT 乘法对应的是**循环**卷积，而我们要的是**线性**卷积。若不补零，长信号尾部会「绕回」叠到头部，产生混叠错误。解决办法是：把两路信号都补零到长度

\[
L \ge N + M - 1
\]

这样循环卷积的「绕回区」正好落在全零段上，结果就与线性卷积完全一致。

补零到 \(L\) 后，整个流程的复杂度为 \(O(L\log L)\)（三次 FFT：两次正变换、一次逆变换）。当 \(N,M\) 同阶且较大时，\(O(N\log N)\) 远胜 \(O(N^2)\)。`fftconvolve` 的文档直言：对大数组（\(n>\sim500\)）它通常远快于直接法。

#### 4.1.2 核心流程

`fftconvolve` 本身只做「编排」，真正的 FFT 在 `_freq_domain_conv` 里。顶层流程：

```
fftconvolve(in1, in2, mode, axes):
    1. 取数组命名空间 xp（Array-API 多后端）
    2. 退化分支：标量 → 直接相乘；空数组 → 返回空；维度不同 → 报错
    3. _init_freq_conv_axes(...)      # 归一化 axes，valid 模式可能交换 in1/in2
    4. shape[a] = s1[a]+s2[a]-1 (卷积轴) 或 max(s1[a], s2[a]) (非卷积轴，靠广播)
    5. ret = _freq_domain_conv(..., calc_fast_len=True)   # FFT→乘→IFFT，并补到快速长度
    6. return _apply_conv_mode(ret, s1, s2, mode, axes)   # 按 full/same/valid 裁剪
```

第 4 步里的 `shape` 就是「线性卷积的完整长度」，它随后被传给引擎作为补零目标。

#### 4.1.3 源码精读

先看 `fftconvolve` 的入口与边界处理（[_signaltools.py:691-714](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signaltools.py#L691-L714)）：

```python
xp = array_namespace(in1, in2)        # 支持 CuPy/JAX 等 Array-API 后端
in1 = xp.asarray(in1)
in2 = xp.asarray(in2)

if in1.ndim == in2.ndim == 0:          # 标量：直接乘
    return in1 * in2
elif in1.ndim != in2.ndim:
    raise ValueError("in1 and in2 should have the same dimensionality")
elif xp_size(in1) == 0 or xp_size(in2) == 0:   # 空数组
    return xp.asarray([])

in1, in2, axes = _init_freq_conv_axes(in1, in2, mode, axes, sorted_axes=False)
s1 = in1.shape
s2 = in2.shape
# 卷积轴取 N+M-1；非卷积轴取 max（靠广播对齐）
shape = [max((s1[i], s2[i])) if i not in axes else s1[i] + s2[i] - 1
         for i in range(in1.ndim)]

ret = _freq_domain_conv(xp, in1, in2, axes, shape, calc_fast_len=True)
return _apply_conv_mode(ret, s1, s2, mode, axes, xp=xp)
```

注意 `_init_freq_conv_axes` 可能会把 `in1`、`in2` 交换（见下），所以这里的 `s1`、`s2` 是交换**之后**的形状，`mode='same'` 取「与 `in1` 同长」时用的就是它。

再看 `_init_freq_conv_axes` 做了什么（[_signaltools.py:456-482](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signaltools.py#L456-L482)）：

```python
s1 = in1.shape
s2 = in2.shape
noaxes = axes is None
_, axes = _init_nd_shape_and_axes(in1, shape=None, axes=axes)
if not noaxes and not len(axes):
    raise ValueError("when provided, axes cannot be empty")

# 长度为 1 的轴可以直接靠广播乘，不必 FFT
axes = [a for a in axes if s1[a] != 1 and s2[a] != 1]
...
# 校验非卷积轴形状兼容
if not all(s1[a] == s2[a] or s1[a] == 1 or s2[a] == 1 for a in ...):
    raise ValueError(...)
# valid 模式可能需要交换，使 in1 成为较长者
if _inputs_swap_needed(mode, s1, s2, axes=axes):
    in1, in2 = in2, in1
return in1, in2, axes
```

两个要点：①「长度为 1 的轴被剔除」——这是把「逐轴 FFT」与「NumPy 广播」结合起来的小优化，`shape` 里这些轴取 `max` 即可；②`valid` 模式下的交换复用了 u3-l1 讲过的 `_inputs_swap_needed`（卷积可交换，故可随意换序）。

最后是 `_apply_conv_mode`（[_signaltools.py:576-586](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signaltools.py#L576-L586)），它把「完整结果」裁成三种模式：

```python
if mode == "full":
    return xp_copy(ret, xp=xp)
elif mode == "same":
    return xp_copy(_centered(ret, s1), xp=xp)
elif mode == "valid":
    shape_valid = [ret.shape[a] if a not in axes else s1[a] - s2[a] + 1
                   for a in range(ret.ndim)]
    return xp_copy(_centered(ret, shape_valid), xp=xp)
```

`_centered`（[_signaltools.py:414-421](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signaltools.py#L414-L421)）就是「从数组中心抠出指定大小的子块」：`start = (currshape - newshape)//2`。这与直接法 `convolve` 的 `mode` 语义完全一致——频域法只是换了算法，对外的接口契约不变。

#### 4.1.4 代码实践

**实践目标**：验证卷积定理——`fftconvolve` 与 NumPy 直接法 `np.convolve` 在 1-D 下结果一致。

**操作步骤**：

```python
import numpy as np
from scipy import signal

rng = np.random.default_rng(0)
x = rng.standard_normal(2000)      # 长信号
h = rng.standard_normal(50)        # 短核

y_fft = signal.fftconvolve(x, h, mode='full')        # 频域法
y_dir = np.convolve(x, h, mode='full')               # NumPy 直接法

print(y_fft.shape, y_dir.shape)
print("max |diff| =", np.max(np.abs(y_fft - y_dir)))
```

**预期结果**：两者形状都是 `(2049,)`；最大绝对误差在 `1e-11` 量级（浮点误差，非零），说明两者数值等价。

**待本地验证**：具体误差量级取决于你的 BLAS/FFT 实现，但应在 `1e-10` 以内。

#### 4.1.5 小练习与答案

**练习 1**：如果把上面的 `h` 换成复数（`h = rng.standard_normal(50) + 1j*rng.standard_normal(50)`），`fftconvolve` 的输出类型会怎样？为什么？
**答案**：输出变为复数。引擎 `_freq_domain_conv` 会检测到 `complex_result`，从而改用 `fftn/ifftn`（而非实数专用的 `rfftn/irfftn`），详见 4.2。

**练习 2**：`mode='same'` 时，输出长度为什么等于 `len(x)` 而不是 `len(h)`？
**答案**：`_apply_conv_mode` 在 `'same'` 分支调用 `_centered(ret, s1)`，其中 `s1` 是 `in1`（即 `x`）的形状，故截成与 `x` 同长。

---

### 4.2 _freq_domain_conv：被两套算法共用的频域引擎

#### 4.2.1 概念说明

`fftconvolve` 和 `oaconvolve` 虽然策略不同（一个是「一次性大 FFT」，一个是「分块小 FFT 再拼接」），但它们**每一段具体的「FFT→乘→IFFT」操作是完全一样的**。于是 scipy 把这段公共逻辑抽成 `_freq_domain_conv`。理解了它，就理解了两套算法各自的「原子操作」。

这个引擎要处理几个细节：

1. **实数 vs 复数**：输入是实数时，频谱是共轭对称的，可以用 `rfftn/irfftn`（只存一半频率，省一半内存与时间）；输入含复数时必须用完整的 `fftn/ifftn`。
2. **整数输入**：FFT 不接受整数，必须先转成浮点。
3. **快速长度**：可以把目标长度再向上取到一个「FFT 友好」的值，进一步提速；但要记得在 IFFT 之后把多余的部分切掉。

#### 4.2.2 核心流程

```
_freq_domain_conv(xp, in1, in2, axes, shape, calc_fast_len):
    if 没有 FFT 轴:  return in1 * in2            # 纯广播乘
    complex_result = (in1 或 in2 是复数)
    if calc_fast_len:
        fshape = next_fast_len(shape[a], real=not complex_result)   # 取快速长度
    else:
        fshape = shape
    (fft, ifft) = (rfftn, irfftn) 或 (fftn, ifftn)   # 按实/复选择
    把整数输入 cast 成默认浮点
    sp1 = fft(in1, fshape, axes);  sp2 = fft(in2, fshape, axes)
    ret = ifft(sp1 * sp2, fshape, axes)
    if calc_fast_len:  ret = ret[只取 shape 那么长]    # 切掉快速长度多出来的尾巴
    return ret
```

#### 4.2.3 源码精读

逐段看（[_signaltools.py:516-548](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signaltools.py#L516-L548)）：

```python
if not len(axes):
    return in1 * in2            # 所有轴长度均为 1，靠广播即可，免 FFT

complex_result = (xp.isdtype(in1.dtype, 'complex floating') or
                  xp.isdtype(in2.dtype, 'complex floating'))

if calc_fast_len:
    # 向上取快速 FFT 长度（实数走 real 路径可更省）
    fshape = [sp_fft.next_fast_len(shape[a], not complex_result) for a in axes]
else:
    fshape = shape

if not complex_result:
    fft, ifft = sp_fft.rfftn, sp_fft.irfftn      # 实数：只算一半频率
else:
    fft, ifft = sp_fft.fftn, sp_fft.ifftn         # 复数：全频率

if xp.isdtype(in1.dtype, 'integral'):
    in1 = xp.astype(in1, xp_default_dtype(xp))    # 整数 → 浮点
if xp.isdtype(in2.dtype, 'integral'):
    in2 = xp.astype(in2, xp_default_dtype(xp))

sp1 = fft(in1, fshape, axes=axes)
sp2 = fft(in2, fshape, axes=axes)
ret = ifft(sp1 * sp2, fshape, axes=axes)

if calc_fast_len:
    fslice = tuple([slice(sz) for sz in shape])
    ret = ret[fslice]            # 切回线性卷积的真实长度
return ret
```

几个要点：

- **`calc_fast_len` 是两条调用路径的分水岭**：`fftconvolve` 传 `True`（先把 `shape` 向上取快速长度，FFT 后再切回 `shape`）；`oaconvolve` 传 `False`（因为 `oaconvolve` 自己已经在 `_calc_oa_lens` 里调过 `next_fast_len`，`shape` 本就是快速长度，不必再切）。这就是为什么函数叫 `calc_fast_len` 而不是简单布尔——它同时控制「是否取快速长度」和「是否需要切片」。
- **`sp_fft` 就是 `scipy.fft`**（[_signaltools.py:17](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signaltools.py#L17)）。scipy 选择 `scipy.fft` 而非 `numpy.fft`，是因为前者默认使用 pocketfft，能自动选快速长度、支持更多后端。
- **`next_fast_len(shape[a], not complex_result)`** 第二个参数是 `real`：实数信号时传 `True`，允许利用 `rfftn` 的对称性，快速长度约束更宽松。

#### 4.2.4 代码实践

**实践目标**：亲手验证「快速长度」带来的加速，并观察实/复数分支的差异。

**操作步骤**：

```python
import numpy as np
from scipy import signal, fft as sp_fft

rng = np.random.default_rng(0)
N, M = 997, 211               # 都是质数：直接补零到 N+M-1=1207 不是快速长度
x = rng.standard_normal(N)
h = rng.standard_normal(M)

L = N + M - 1
print("raw L =", L, " fast L =", sp_fft.next_fast_len(L, True))
# 手写一个「不取快速长度」的频域卷积做对照
def fft_conv_nofast(x, h):
    n = x.shape[0] + h.shape[0] - 1
    X = sp_fft.rfft(x, n);  H = sp_fft.rfft(h, n)
    return sp_fft.irfft(X * H, n)

y_fast = signal.fftconvolve(x, h)          # 内部取快速长度
y_slow = fft_conv_nofast(x, h)             # 用质数长度 1207
print("max |diff| =", np.max(np.abs(y_fast - y_slow)))   # 应≈0
```

**预期结果**：`raw L = 1207`，`fast L = 1208`（或附近一个 2 的幂次友好值）。`max |diff|` 在 `1e-11` 量级，说明取快速长度只影响速度、不影响结果。

**待本地验证**：可用 `%timeit` 比较 `fft_conv_nofast` 与 `signal.fftconvolve` 的耗时，质数长度下后者通常明显更快。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `oaconvolve` 路径传 `calc_fast_len=False`？
**答案**：`oaconvolve` 在 `_calc_oa_lens` 里已经对块大小调用过 `next_fast_len`（见 [_signaltools.py:812](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signaltools.py#L812)），传入引擎的 `fft_shape` 本身已是快速长度；若再取一次，IFFT 后还要切片，反而破坏「块与块可对齐相加」的布局。

**练习 2**：输入是整数数组时，引擎做了什么处理？
**答案**：用 `xp.astype(..., xp_default_dtype(xp))` 把整数转成默认浮点（[_signaltools.py:534-537](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signaltools.py#L534-L537)），所以 `fftconvolve` 的输出永远是浮点（与文档「can only output float arrays」一致）。

---

### 4.3 oaconvolve：重叠相加法与最优分块

#### 4.3.1 概念说明

`fftconvolve` 的瓶颈在于：当两路信号长度悬殊（典型场景：超长信号 \(N\) 卷一个固定滤波器 \(M\)，且 \(N \gg M\)），它仍要把**整条长信号**做一次大 FFT，开销集中在那次 \(O(N\log N)\) 的变换上，且大数组对缓存不友好。

**重叠相加（Overlap-Add）** 的思路是把长信号切成若干短块：

1. 把长信号 \(x\)（长 \(N\)）按步长 \(B_{\text{step}}\) 切成 \(x_0, x_1, \dots\)，每块长 \(B\)。
2. 每块 \(x_i\) 与滤波器 \(h\)（长 \(M\)）单独做「小块 FFT 卷积」，得到长 \(B+M-1\) 的结果 \(y_i\)。
3. 相邻块的 \(y_i\) 会有 \(M-1\) 个样本**重叠**（因为卷积把每块都「拉长」了 \(M-1\)），把这 \(M-1\) 个重叠样本**相加**，就拼回了完整卷积结果。

每块的 FFT 长度只有 \(L_{\text{blk}} = B+M-1\)，远小于 \(N\)；共有约 \(N/B_{\text{step}}\) 块。合理选 \(B\) 可以让总工作量低于「一次性大 FFT」。**核心问题：块大小 \(B\) 取多大最优？** 这正是 `_calc_oa_lens` 要回答的。

#### 4.3.2 核心流程

`oaconvolve` 顶层（[_signaltools.py:938-976](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signaltools.py#L938-L976)）：

```
oaconvolve(in1, in2, mode, axes):
    退化分支（标量/空/形状完全相同 → 直接转给 fftconvolve）
    _init_freq_conv_axes(..., sorted_axes=True)        # 注意：这里要排序轴
    对每个卷积轴 a，调 _calc_oa_lens(s1[a], s2[a]) 得到 (block_size, overlap, in1_step, in2_step)
    若每个轴都只分一块 → 退回 fftconvolve
    计算每轴的步数 nsteps 与尾部补零 pad
    把 in1/in2 补零后 reshape 成 (nsteps, step) 的分块布局
    ret = _freq_domain_conv(..., fft_shape=block_size, calc_fast_len=False)   # 一次性对所有块做 FFT 卷积
    对每个轴做「重叠相加」：用 _split 切出重叠段，加到下一块头部
    reshape 回正常维度，按 shape_final 切片
    return _apply_conv_mode(ret, s1, s2, mode, axes)
```

**最优块大小的推导**（[_signaltools.py:758-811](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signaltools.py#L758-L811) 的注释里写得很详细）。设：

- \(K = M-1\)（重叠样本数，即每块被卷积「拉长」的量）；
- \(N\) = 块的 FFT 长度（即 \(B+M-1\)，代码里直接记作 `block_size`）。

每块 FFT 卷积的代价正比于 \(N\log_2(2N)\)，而每块只产出 \(N-K\) 个「不重叠的全新」样本。于是**每输出样本的平均代价**为：

\[
C(N) = \frac{N\log_2(2N)}{N-K}
\]

对 \(N\) 求导令其为零，经过一连串代数化简（注释里有完整步骤），可得极值点满足：

\[
\frac{N}{K} = \ln(2Ne)
\]

这是一个超越方程，解析解要用 **Lambert W 函数**（\(W\) 是 \(y = x e^x\) 的反函数）：

\[
N = -K \cdot W_{-1}\!\left(-\frac{1}{2eK}\right)
\]

其中 \(W_{-1}\) 是 \(W\) 的 \(-1\) 分支（取这个分支是因为另一分支给出过小的 \(N\)）。代码实现就一行（[_signaltools.py:811-812](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signaltools.py#L811-L812)）：

```python
overlap = s2-1
opt_size = -overlap*lambertw(-1/(2*math.e*overlap), k=-1).real
block_size = sp_fft.next_fast_len(math.ceil(opt_size))
```

即：算出理论最优 \(N\)，再向上取到最近的快速 FFT 长度。

**何时退回 `fftconvolve`**（[_signaltools.py:741-756](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signaltools.py#L741-L756)）：分块没有意义时直接回退——两信号等长、或其中一个长度为 1、或 \(M \ge N/2\)（重叠太大，分块反而更慢）、或算出的块比整条信号还长（只够一块）。这些情况下 `_calc_oa_lens` 返回 `fallback = (s1+s2-1, None, s1, s2)`，上层据此转调 `fftconvolve`（[_signaltools.py:975-976](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signaltools.py#L975-L976)）。

#### 4.3.3 源码精读：分块、卷积、重叠相加

分块与补零（[_signaltools.py:984-1036](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signaltools.py#L984-L1036)）：对每个卷积轴算出步数 `curnstep` 和尾部补零 `curpad`，补零后用 `reshape` 把信号「摊」成 `(nsteps, step)` 的二维布局——这样后续一次 `_freq_domain_conv` 就能并行处理所有块（利用了批处理 FFT）：

```python
# 每块的 FFT 卷积（对所有块一次性完成）
fft_shape = [block_size[i] for i in axes]
ret = _freq_domain_conv(xp, in1, in2, fft_axes, fft_shape, calc_fast_len=False)
```

**重叠相加**（[_signaltools.py:1043-1054](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signaltools.py#L1043-L1054)）逐轴处理：

```python
for ax, ax_fft, ax_split in zip(axes, fft_axes, split_axes):
    overlap = overlaps[ax]
    if overlap is None:
        continue
    # 把每块尾部「重叠段」切出来
    ret, overpart = _split(ret, [-overlap], ax_fft, xp=xp)
    overpart = _split(overpart, [-1], ax_split, xp=xp)[0]
    # 把它加到下一块的对应头部
    overlap_slice = [slice(None)] * ret.ndim
    overlap_slice[ax_fft] = slice(0, overlap)
    overlap_slice[ax_split] = slice(1, None)
    ret = xpx.at(ret)[tuple(overlap_slice)].add(overpart)
```

`_split`（[_signaltools.py:830-847](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signaltools.py#L830-L847)）是 `np.split` 的 Array-API 简化版（为兼容 CuPy/JAX）。`xpx.at(ret)[...].add(...)` 等价于 NumPy 的 `np.add.at(ret, idx, overpart)`，即在指定索引处**就地累加**——这正是「重叠相加」里「相加」二字的实现。

最后 `reshape` 回正常维度、按 `shape_final = s1[i]+s2[i]-1` 切片、再用 `_apply_conv_mode` 裁成所需 `mode`（[_signaltools.py:1057-1066](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signaltools.py#L1057-L1066)）。

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：对两个长随机序列，比较 `convolve(method='direct')`、`fftconvolve`、`oaconvolve` 的**输出一致性**与**运行时间**，并找出 `oaconvolve` 优于 `fftconvolve` 的规模条件。

**操作步骤**：

```python
import numpy as np
from scipy import signal
import timeit

rng = np.random.default_rng(0)

def bench(N, M, number=3):
    """对长 N 的信号、长 M 的滤波器，比较三种卷积。"""
    x = rng.standard_normal(N)
    h = rng.standard_normal(M)

    y_dir = signal.convolve(x, h, mode='full', method='direct')
    y_fft = signal.fftconvolve(x, h, mode='full')
    y_oa  = signal.oaconvolve(x, h, mode='full')

    # 一致性
    d_fft = np.max(np.abs(y_dir - y_fft))
    d_oa  = np.max(np.abs(y_dir - y_oa))

    # 计时（取最短）
    t_dir = min(timeit.repeat(lambda: signal.convolve(x, h, method='direct'), number=1, repeat=number))
    t_fft = min(timeit.repeat(lambda: signal.fftconvolve(x, h),        number=1, repeat=number))
    t_oa  = min(timeit.repeat(lambda: signal.oaconvolve(x, h),         number=1, repeat=number))
    print(f"N={N:>7} M={M:>5} | err_fft={d_fft:.2e} err_oa={d_oa:.2e} | "
          f"t_direct={t_dir*1e3:8.2f}ms t_fft={t_fft*1e3:8.2f}ms t_oa={t_oa*1e3:8.2f}ms")

# 场景 A：两信号等长 → fftconvolve 应更快，oaconvolve 会内部退回 fftconvolve
bench(2000, 2000)

# 场景 B：长信号 + 短滤波器 → oaconvolve 的主场
bench(200000, 256)

# 场景 C：极悬殊 → oaconvolve 优势更明显
bench(1000000, 512)
```

**需要观察的现象**：

1. 三种方法的 `err` 都在 `1e-10` 以内——**输出数值一致**（`oaconvolve` 与 `fftconvolve` 只是算法不同，结果相同）。
2. 场景 A（等长）下，`t_oa` 与 `t_fft` 接近——因为 `oaconvolve` 检测到分块无意义，内部直接转调 `fftconvolve`（见 [_signaltools.py:949-950](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signaltools.py#L949-L950) 与 [_signaltools.py:975-976](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signaltools.py#L975-L976)）。
3. 场景 B、C（\(N \gg M\)）下，`t_oa` 应明显小于 `t_fft`——**这正是 `oaconvolve` 的适用区**。

**预期结果**：场景 A 中 `t_oa ≈ t_fft`；场景 C 中 `t_oa` 可能只有 `t_fft` 的若干分之一。`err` 全部接近浮点精度。

**何时 `oaconvolve` 优于 `fftconvolve`**：当**一个数组远大于另一个、且二者尺寸显著不同**时（文档原文：「generally much faster than fftconvolve when one array is much larger than the other」）。直觉解释：`fftconvolve` 要对整条长信号做一次大 FFT；`oaconvolve` 只做很多次小 FFT，小 FFT 对缓存友好，且 `_calc_oa_lens` 给出的最优块大小把总浮点运算量压到了大 FFT 之下。反之，当两信号等长或尺寸接近时，分块没有收益，应直接用 `fftconvolve`。

**待本地验证**：具体的计时数字与加速比取决于机器、BLAS 与 FFT 后端，但定性结论（B/C 场景 `oaconvolve` 更快）应当稳定成立。

#### 4.3.5 小练习与答案

**练习 1**：`oaconvolve` 在哪些条件下会**退回** `fftconvolve`？至少说出三种。
**答案**：①两输入形状完全相同（[_signaltools.py:949-950](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signaltools.py#L949-L950)）；②`_calc_oa_lens` 返回 `fallback`，包括 \(s_1=s_2\)、某个长度为 1、\(s_2 \ge s_1/2\)、或算出的块不小于整条信号（[_signaltools.py:745-756](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signaltools.py#L745-L756)、[_signaltools.py:815-816](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signaltools.py#L815-L816)）；③每个轴都只分一块（[_signaltools.py:975-976](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signaltools.py#L975-L976)）。

**练习 2**：为什么 `_calc_oa_lens` 要把短信号记为 \(s_2\)（即保证 \(s_1 \ge s_2\)）？
**答案**：重叠量 \(K=s_2-1\) 来自「较短的那个」（滤波器），把它固定为 \(s_2\) 后，推导最优块大小的公式 \(N=-K\cdot W_{-1}(-1/(2eK))\) 才有意义；若 \(s_2\) 是长信号，\(K\) 会过大、分块无收益。代码用 `swapped` 标志记住是否交换过，最后据此决定 `in1_step`/`in2_step`（[_signaltools.py:748-752](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signaltools.py#L748-L752)、[_signaltools.py:818-823](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signaltools.py#L818-L823)）。

**练习 3**：把 `_calc_oa_lens` 注释里的复杂度公式 \(C(N)=N\log_2(2N)/(N-K)\) 代入 \(K=255\)（即 \(M=256\)），用 `scipy.special.lambertw` 算出理论最优 \(N\)，再 `next_fast_len` 取整，与你实践中 `bench(200000, 256)` 的实际块大小对照。
**答案**：运行 `_calc_oa_lens(200000, 256)` 直接可得 `(block_size, overlap, in1_step, in2_step)`；`block_size` 即理论与实践的折中结果。学生应观察到 `block_size` 远小于 200000，印证「分块有效」。

---

## 5. 综合实践

把本讲三个模块串起来，做一个「**为给定输入自动选最佳卷积方法**」的小工具：

**任务**：写一个函数 `best_conv(x, h, mode='full')`，它依次尝试 `convolve(method='direct')`、`fftconvolve`、`oaconvolve`，返回**最快那个**的结果，并打印选择了哪种方法。然后用三组输入测试它：(a) 两个 500 长信号；(b) 5000 长信号与 100 长滤波器；(c) 500000 长信号与 200 长滤波器。

**提示**：

- 用 `timeit`（或 `%timeit`）测每种方法的耗时，取最小值。
- 用 `np.allclose` 验证三种方法结果一致（容差放大到 `1e-8` 量级即可，因为浮点误差）。
- 解释你观察到的选择规律：小输入下 `direct` 可能最快；大输入下 `fftconvolve`/`oaconvolve` 取胜；悬殊尺寸下 `oaconvolve` 取胜。
- 进阶：把你的「实测选择」与本系列下一篇 u3-l3 将要讲的 `choose_conv_method`（基于运算量**估算**而非实测）做对比，思考「估算」与「实测」各自的优劣。

**预期结论**：你的函数在 (a) 倾向 `direct` 或 `fft`，在 (b) 倾向 `fft`/`oa`，在 (c) 倾向 `oa`——这正是 scipy 提供**三种**卷积接口、且让 `convolve(method='auto')` 自动选择的根本原因。

## 6. 本讲小结

- 频域卷积依赖**卷积定理**：时域卷积 \(\Leftrightarrow\) 频域相乘；为避免循环卷积的「绕回」，必须把两路信号补零到 \(N+M-1\)，复杂度从直接法的 \(O(NM)\) 降到 \(O(L\log L)\)。
- `fftconvolve` 只做编排：`_init_freq_conv_axes` 归一化轴（含 `valid` 交换）、`_freq_domain_conv` 做真正的 FFT→乘→IFFT、`_apply_conv_mode` 按 `full/same/valid` 裁剪。
- `_freq_domain_conv` 是被 `fftconvolve` 和 `oaconvolve` **共用**的核心引擎；它按实/复数选 `rfftn`/`fftn`，按 `calc_fast_len` 决定是否取快速长度并切片。
- `oaconvolve` 用**重叠相加法**把长信号切块、分块 FFT 卷积、再把重叠段相加拼回；适用场景是「一个数组远大于另一个」。
- `_calc_oa_lens` 用 **Lambert W 函数** 求出使每输出样本代价 \(C(N)=N\log_2(2N)/(N-K)\) 最小的块大小，并在分块无收益时优雅退回 `fftconvolve`。
- 三种方法**输出数值一致**，差异只在速度；如何自动择优正是下一篇 u3-l3 `choose_conv_method` 的主题。

## 7. 下一步学习建议

- **下一篇 u3-l3**：读 [u3-l3-choose-conv-method.md](u3-l3-choose-conv-method.md)，看 `convolve(method='auto')` 如何用 `_conv_ops`/`_fftconv_faster` **估算**运算量来在直接法与 FFT 法之间自动切换，把它与本讲「实测择优」的综合实践对照。
- **延伸阅读源码**：`_init_nd_shape_and_axes`（处理 N-D 的 `axes` 参数）、`scipy.fft.next_fast_len` 与 `scipy.special.lambertw` 的官方文档，以加深对快速长度与 Lambert W 的理解。
- **跨单元联系**：本讲的 `_freq_domain_conv` 与单元 4 的 `resample_poly`/`upfirdn`（u4-l4）共享「FFT + 分块」的多速率处理思想，学完滤波后可回看本讲，体会卷积与重采样的统一性。
