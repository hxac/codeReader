# 快速上手：一维复数 FFT

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `scipy.fftpack.fft` 与 `ifft` 各个参数（`x`、`n`、`axis`、`overwrite_x`）的含义，以及「默认沿最后一个轴」这一约定从何而来。
- 写出离散傅里叶变换（DFT）的正、逆公式，并解释为什么 `fft` 是「求和」而 `ifft` 是「求平均（除以 \(n\)）」。
- 看懂 `fft` 输出的「标准打包顺序」：`[0 频, 正频, 负频]`，并能用 `fftpack.fftfreq` 给输出逐位标上频率。
- 理解「长度为 2 的幂时最快、长度为素数时最慢」这一性能规律，知道如何用 `fftpack.next_fast_len` 选一个高效的长度。
- 独立完成一次「加噪正弦信号 → `fft` → 主频定位 → `ifft` 还原」的完整实践。

本讲覆盖的最小模块是 **`fft`** 和 **`ifft`**，对应源码文件 [`_basic.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/_basic.py#L1-L429)。

---

## 2. 前置知识

在开始之前，先用最朴素的方式建立几个直觉。本讲不要求你已经会算 DFT，只要跟着下面的概念走即可。

### 2.1 时域与频域

一段随时间变化的信号（例如一段录音、一组传感器读数），既可以用「每个时刻的取值」来描述（**时域**），也可以用「它由哪些频率成分叠加而成」来描述（**频域**）。这两种描述是同一份信息的两种「视角」，**离散傅里叶变换（Discrete Fourier Transform, DFT）** 就是把时域视角切换到频域视角的工具；**逆变换（IDFT）** 则把它切换回来。

### 2.2 为什么需要「快速」算法

直接按定义计算 \(n\) 点 DFT 大约需要 \(O(n^2)\) 次乘法。当 \(n\) 很大时这会慢得不可接受。**快速傅里叶变换（Fast Fourier Transform, FFT）** 是一类巧妙算法，能把复杂度降到约 \(O(n\log n)\)，但它对长度 \(n\) 有偏好：当 \(n\) 能被分解成许多小素因子（尤其全是 2）时，FFT 跑得最快；当 \(n\) 本身就是个大素数时，几乎退化成 \(O(n^2)\)。这就是后面「性能规律」一节的根源。

> 术语约定：**DFT** 指数学定义，**FFT** 指实现该定义的快速算法。`scipy.fftpack.fft` 这个函数名用的是 FFT，但它的数学含义就是 DFT。

### 2.3 与前几讲的衔接

- 第一讲（u1-l1）我们确认了 `fftpack` 是 **legacy 模块**，但 `from scipy.fftpack import fft` 是合法且**不报警**的路径——`fft` 真正定义在私有的 [`_basic.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/_basic.py#L1-L429)，经聚合导入抬到包顶层。
- 第三讲（u1-l3）我们看到 `fftpack.fftfreq` 其实是从 `numpy.fft` **再导出**的辅助函数。本讲在「标注频率轴」时会用到它，到时你会亲手验证它的真实出处。

---

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用到的部分 |
| --- | --- | --- |
| [`scipy/fftpack/_basic.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/_basic.py#L1-L429) | **核心**：定义 `fft`/`ifft` 等一维变换 | `fft`（L12–L88）、`ifft`（L91–L144）两条委托语句 |
| [`scipy/fftpack/_helper.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/_helper.py#L1-L116) | 辅助函数：频率刻度、补零长度 | `fftfreq` 的再导出（L4） |

理解这两个文件就足以掌握本讲的 `fft`/`ifft`。文件里还有 `fftn`/`fft2`/`rfft` 等，它们是后续讲义（u2-l1、u2-l2）的主题，本讲只在「延伸」里点到为止。

> 关于后端：`fft`/`ifft` 的真正计算并不在 `_basic.py` 里完成，而是**一行委托**给 `scipy.fft._duccfft`（DUCC 后端）。这一点对初学者可以先「当成黑盒」，本讲在 4.1.3 的延伸阅读中给出线索，但主线只读 `_basic.py`。

---

## 4. 核心概念与源码讲解

### 4.1 fft：前向离散傅里叶变换

#### 4.1.1 概念说明

`fft` 做的事用一句话说就是：**给定一段（复数或实数）序列 \(x[0..n-1]\)，算出它的 \(n\) 个频率分量 \(y[0..n-1]\)**。

每个 \(y(j)\) 是「整段信号与一个特定频率的复正弦波」做内积的结果。这个内积衡量的是「信号里含有多少该频率的成分」：

\[
y(j) = \sum_{k=0}^{n-1} x[k]\, e^{-\,2\pi i \cdot j k / n}, \qquad j = 0,1,\ldots,n-1
\]

- \(k\) 是时间下标，\(j\) 是频率下标。
- 指数里的 \(-2\pi i \cdot jk/n\) 决定了「第 \(j\) 个频率分量的旋转方向和快慢」；负号代表**前向**变换（`fft`）。
- 这是**未归一化**的：前面没有 \(1/n\) 系数，所以 `fft` 本质是「求和」。归一化被留给了逆变换 `ifft`（见 4.2）。

为什么要这么设计？因为「前向不除、逆向除以 \(n\)」是一种被广泛采用的约定（NumPy、MATLAB 都遵循它），SciPy 在内部称为 `backward` 归一化模式。它让正逆变换的符号清晰、便于手算验证。

#### 4.1.2 核心流程

`fft` 在 Python 层几乎不做计算，它的执行流程非常薄：

```text
用户调用 fft(x, n=None, axis=-1, overwrite_x=False)
        │
        ├─ 参数 x: 待变换序列（实数或复数 array_like）
        ├─ 参数 n:  变换长度。n < 实长 → 截断；n > 实长 → 末尾补零；默认 = x.shape[axis]
        ├─ 参数 axis: 沿哪条轴变换，默认 -1（最后一个轴）
        ├─ 参数 overwrite_x: 是否允许就地破坏 x 的内存（True 可省一次拷贝，默认 False）
        │
        └─→ return _duccfft.fft(x, n, axis, None, overwrite_x)
                        │
                        └─→ 交给 DUCC 后端真正做 FFT，返回复数 ndarray（标准打包）
```

几条关键语义，都来自函数文档字符串：

1. **长度 `n` 的截断/补零规则**：当 `n` 小于数据实际长度时，**截断**（只取前 `n` 个）；大于时**末尾补零**；不传则用 `x.shape[axis]`。这是用同一个参数同时表达「缩短」和「补零」两种操作。
2. **默认轴 `axis=-1`**：约定沿最后一个轴变换，这样对一维数组天然成立，对多维数组则「对每一行各自做一维 FFT」。
3. **标准打包顺序**：输出不是「频率从小到大」排的，而是 `[0 频, 正频, 负频]`。对偶数 \(n\)，频率下标依次是：

\[
[0,\;1,\;2,\;\ldots,\;n/2,\;1-n/2,\;\ldots,\;-1]
\]

   例如 \(n=8\) 时，输出各位对应的频率下标是 `[0, 1, 2, 3, -4, -3, -2, -1]`。注意中间那个 `n/2`（这里是 4，即 \(-4\)）是**奈奎斯特（Nyquist）频率**，它正负同值。
4. **性能规律**：`n` 为 2 的幂时最快，为素数时最慢。

#### 4.1.3 源码精读

先看 `fft` 的签名与文档，再读它唯一的执行语句。

`fft` 的定义与逐参数说明：[`_basic.py:L12-L88`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/_basic.py#L12-L88) —— 这里规定了参数 `x, n, axis, overwrite_x`，以及默认 `axis=-1`、默认 `overwrite_x=False`。

文档字符串里直接给出了 DFT 定义式（与 4.1.1 一致）：[`_basic.py:L44`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/_basic.py#L44)

```python
y(j) = sum[k=0..n-1] x[k] * exp(-sqrt(-1)*j*k* 2*pi/n), j = 0..n-1
```

「标准打包」的官方描述在这里：[`_basic.py:L53-L59`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/_basic.py#L53-L59)。它明确写出 8 点变换的频率是 `[0, 1, 2, 3, -4, -3, -2, -1]`，并提示若想把 0 频挪到正中间可以用 `fftshift`。

性能规律一句话点明：[`_basic.py:L66-L67`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/_basic.py#L66-L67)

```python
This function is most efficient when `n` is a power of two, and least
efficient when `n` is prime.
```

还有一条容易被忽略、但很关键的细节：**当输入是实数时，`fft` 会自动改用「实数 FFT」算法，输出仍是完整的复数标准打包结果**，见 [`_basic.py:L72-L77`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/_basic.py#L72-L77)。也就是说 `fft` 对实数输入会「偷偷」加速近一倍，但对外接口和返回类型保持不变。

最后是真正的执行——只有一行委托：[`_basic.py:L88`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/_basic.py#L88)

```python
return _duccfft.fft(x, n, axis, None, overwrite_x)
```

> **延伸阅读（超出 fftpack 目录，可选）**：`_duccfft.fft` 实为 `functools.partial(c2c, True)`，真正的实现是 `c2c` 函数——见 DUCC 后端 [`scipy/fft/_duccfft/basic.py:L34`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/basic.py#L34) 与 [`L11-L31`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/basic.py#L11-L31)。调用里第 4 个实参 `None` 对应 `c2c` 的 `norm` 参数。这套「前向传 norm=None 得到不归一化」的约定，由 [`scipy/fft/_duccfft/helper.py:L181-L192`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/helper.py#L181-L192) 的 `_NORM_MAP` 与 `_normalization` 落实。初学阶段把它当黑盒即可，本讲主线只依赖 `_basic.py` 的文档与那行委托。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「标准打包顺序」，并确认 `fftpack.fftfreq` 与 `fft` 输出一一对齐。

**操作步骤**（示例代码，非项目原有代码）：

```python
# 示例代码
import numpy as np
from scipy.fftpack import fft, fftfreq

n = 8
x = np.arange(n, dtype=float)          # 一段实数信号
y = fft(x)                             # 默认 n=x.shape[-1], axis=-1
print("fft 输出:", y)
print("实数输入仍是复数类型:", y.dtype)  # 复数（标准打包），并非 rfft 的实数打包

# 用 fftfreq 给每一位标频率（d=1.0）
f = fftfreq(n, d=1.0)
print("fftfreq:", f)
```

**需要观察的现象**：

1. `y` 是**复数** `dtype`（即便输入是实数），印证「实数输入也只是加速、不改返回类型」。
2. `fftfreq(8)` 应得到 `[0, 1, 2, 3, -4, -3, -2, -1]/8`，即 `[0, 0.125, 0.25, 0.375, -0.5, -0.375, -0.25, -0.125]`——与文档里的频率下标 `[0,1,2,3,-4,-3,-2,-1]` 完全对应。
3. 因此 `f[i]` 就是 `y[i]` 这一格代表的频率，二者天然对齐，无需手动重排。

**预期结果**：`fftfreq` 的输出与 8 点变换的频率下标一一对应；`y.dtype` 为复数。

> 关于「素数慢、幂次快」的计时对比：你可以用 `%timeit fft(np.zeros(1024))` 与 `%timeit fft(np.zeros(1013))`（1013 是素数）各跑一次比较耗时。具体数字取决于机器，**待本地验证**；但素数长度明显更慢这一趋势是确定的。

#### 4.1.5 小练习与答案

**练习 1**：对 `n=8`，标准打包里下标为 4 的那一格代表什么频率？为什么是 `-4` 而不是 `+4`？

**参考答案**：它代表奈奎斯特频率 \(n/2 = 4\)。由于频率以 \(n\) 为周期，\(y(4)\) 与 \(y(-4)\) 是同一个分量（\(e^{-2\pi i \cdot 4k/8}\) 与 \(e^{+2\pi i \cdot 4k/8}\) 在 \(n=8\) 时恰好相等，因为 \(\pm 4 \equiv 4 \pmod 8\)）。文档把它归入负频一侧写作 `-4`，所以 8 点变换的频率序列是 `[0,1,2,3,-4,-3,-2,-1]`。

**练习 2**：`fft(np.array([1,2,3,4]), n=6)` 会把输入怎样处理？输出长度是多少？

**参考答案**：输入实长为 4，`n=6 > 4`，所以末尾补 2 个零，变成 `[1,2,3,4,0,0]` 后再做 6 点 DFT；输出是长度为 6 的复数数组。

**练习 3**：如果想让 `fft` 的 0 频出现在输出正中间，应该额外调用哪个函数？

**参考答案**：调用 `fftpack.fftshift(y)`（由 numpy 再导出），它会把 `[0,正频,负频]` 重排成 `[负频,…,0,…,正频]`，0 频居中。

---

### 4.2 ifft：逆变换与往返一致性

#### 4.2.1 概念说明

`ifft` 是 `fft` 的逆运算：给你一份频域表示 \(y\)，还原出原来的时域序列 \(x\)。它的定义只比 `fft` 多两处不同——指数取正号、整体除以 \(n\)：

\[
y(j) = \frac{1}{n}\sum_{k=0}^{n-1} x[k]\, e^{+\,2\pi i \cdot j k / n}
\]

文档字符串用一个更紧凑的写法表达「除以 \(n\)」：`y(j) = (x * exp(...)).mean()`——`.mean()` 就是「先求和再除以 \(n\)」。这与 `fft` 的「`.sum()`」正好互补：**`fft` 求和，`ifft` 求平均**，二者抵消，于是：

\[
\texttt{ifft}(\texttt{fft}(x)) \approx x
\]

这就是「往返一致性（round-trip consistency）」，也是 DFT/IDFT 最基本的正确性保证。

#### 4.2.2 核心流程

`ifft` 的参数与 `fft` **完全对称**：

```text
用户调用 ifft(x, n=None, axis=-1, overwrite_x=False)
        │
        ├─ x: 频域数据（待还原）
        ├─ n: 还原长度，截断/补零规则同 fft
        ├─ axis: 沿哪条轴，默认 -1
        ├─ overwrite_x: 是否允许就地破坏 x，默认 False
        │
        └─→ return _duccfft.ifft(x, n, axis, None, overwrite_x)
                        │
                        └─→ DUCC 后端：取共轭方向、乘以 1/n，返回时域 ndarray
```

理解 `ifft` 的关键在于「归一化」。虽然 `ifft` 和 `fft` 调用语句长得几乎一样（都把第 4 个参数写成 `None`），但 DUCC 后端的 `_normalization(None, forward)` 会**根据方向自动翻转**：

- `fft`：`forward=True` → 不归一化（求和）；
- `ifft`：`forward=False` → 除以 \(n\)（求平均）。

所以「前向不除、逆向除」不是在 `_basic.py` 里手写的，而是由后端的归一化映射表统一管理——这正是为什么 `_basic.py` 里两个函数都那么短。

> 概念上记一句话即可：**`fft` 是 `.sum()`，`ifft` 是 `.mean()`**。这意味着 `fft/ifft` 这对函数的「能量」并不守恒地分配——所有归一化都压在 `ifft` 一侧。若你需要对称归一化，新版 `scipy.fft` 提供了 `norm='ortho'`，但 `fftpack.fft`/`ifft` **没有** `norm` 参数（第 4 个实参被固定写死为 `None`）。

#### 4.2.3 源码精读

`ifft` 的定义、参数与文档：[`_basic.py:L91-L144`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/_basic.py#L91-L144)。

逆向变换的「求平均」定义式：[`_basic.py:L97`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/_basic.py#L97)

```python
y(j) = (x * exp(2*pi*sqrt(-1)*j*np.arange(n)/n)).mean()
```

注意指数里是正号（`exp(+...)`），并且 `.mean()` 等价于除以 \(n\)。

唯一的执行语句——一行委托，与 `fft` 结构相同：[`_basic.py:L144`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/_basic.py#L144)

```python
return _duccfft.ifft(x, n, axis, None, overwrite_x)
```

文档自己给出的「往返」示例就在 docstring 里：[`_basic.py:L135-L141`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fftpack/_basic.py#L135-L141)，断言 `np.allclose(ifft(fft(x)), x, atol=1e-15)` 为 `True`。这正是我们在实践里要复现的核心事实。

#### 4.2.4 代码实践

**实践目标**：复现「`ifft(fft(x)) ≈ x`」这一往返一致性，并直观感受 `fft`/`ifft` 的归一化差异。

**操作步骤**（示例代码）：

```python
# 示例代码
import numpy as np
from scipy.fftpack import fft, ifft

rng = np.random.default_rng(0)
x = rng.standard_normal(16) + 1j * rng.standard_normal(16)   # 复数信号

# 1) 往返一致性
x_back = ifft(fft(x))
print("往返最大误差:", np.max(np.abs(x_back - x)))   # 应接近 0
print("allclose:", np.allclose(x_back, x, atol=1e-15))

# 2) 感受归一化：fft 是求和、ifft 是求平均
#    用一段「全 1」的常数序列对比最直观：
const = np.ones(8, dtype=complex)
print("fft(全1)[0] =", fft(const)[0],   "（求和：n 个 1 相加 = 8）")
print("ifft(全1)[0] =", ifft(const)[0], "（求平均：先求和 8 再 /n = 1）")
```

**需要观察的现象**：

1. 第 1 步往返误差应在 \(10^{-15}\) 量级，`allclose` 为 `True`——浮点误差以内完全还原。
2. 第 2 步：同一个「全 1」输入，`fft` 给出 8（\(y(0)=\sum_k x[k]=8\)，**求和**），而 `ifft` 给出 1（\(\frac{1}{n}\sum_k x[k]=\frac{8}{8}=1\)，**求平均**）。二者正好相差一个因子 \(n\)，这正是「`fft` 求和、`ifft` 求平均」的直接体现。
3. 进一步：`fft(const)` 其实每一位都是 8，`ifft(const)` 是 `[1,0,0,0,0,0,0,0]`（常数只在 0 频有分量，逆变换后只在 \(k=0\) 非零）。

**预期结果**：`allclose` 返回 `True`；`fft(全1)[0]=8`、`ifft(全1)[0]=1`，相差 \(n=8\) 倍。具体打印值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `fft` 用 `.sum()` 而 `ifft` 用 `.mean()`？如果两者都用 `.sum()`，`ifft(fft(x))` 会差多少倍？

**参考答案**：这是 `backward` 归一化约定——把全部 \(1/n\) 系数都放在逆变换一侧，使前向变换保持「纯净的求和」。若两者都用求和，则 `ifft(fft(x))` 会等于 \(n \cdot x\)，即放大 \(n\) 倍，不再还原。

**练习 2**：`fftpack.ifft` 是否支持 `norm='ortho'`？为什么？

**参考答案**：不支持。`_basic.py` 里 `ifft` 调用 `_duccfft.ifft(x, n, axis, None, overwrite_x)` 时，`norm` 形参被**固定写死**为 `None`，没有对外暴露；若需要正交归一化，应改用新版 `scipy.fft.ifft(..., norm='ortho')`。

**练习 3**：给定一段复数频谱 `Y = fft(x)`，如何在不调用 `ifft` 的情况下，用 `fft` 本身还原 `x`？

**参考答案**：利用「逆变换 = 共轭后做正变换再共轭再除以 \(n\)」的性质：`x = np.conj(fft(np.conj(Y))) / n`。这等价于 `ifft(Y)`，常被用来在只有 `fft` 实现的场合完成逆变换。

---

## 5. 综合实践

把本讲的 `fft`、`ifft`、`fftfreq`、`next_fast_len` 串成一条完整的数据流水线：**构造一段加噪正弦信号 → 用 `fft` 得到频谱 → 用 `fftfreq` 标频率轴、定位主频 → 用 `ifft` 还原并验证一致**。

**操作步骤**（示例代码）：

```python
# 示例代码
import numpy as np
from scipy.fftpack import fft, ifft, fftfreq

# ---- 1. 构造含噪声的正弦信号 ----
N = 1000                      # 采样点数（1000 是 5-smooth 数，效率不错）
d = 0.001                     # 采样间隔 1 ms → 采样率 1000 Hz
t = np.arange(N) * d
f0 = 50.0                     # 信号真实频率 50 Hz
rng = np.random.default_rng(42)
signal = np.sin(2 * np.pi * f0 * t) + 0.3 * rng.standard_normal(N)

# ---- 2. 前向 FFT 得到频谱 ----
S = fft(signal)               # 复数频谱，标准打包
mag = np.abs(S)               # 幅度谱

# ---- 3. 用 fftfreq 标频率轴、定位主频 ----
freqs = fftfreq(N, d=d)       # 与 S 逐位对齐的频率
peak_idx = np.argmax(mag)
print("检测到的主频:", freqs[peak_idx], "Hz（期望约 50 Hz）")

# ---- 4. 用 ifft 还原信号并验证 ----
reconstructed = ifft(S)
print("往返 allclose:", np.allclose(reconstructed, signal, atol=1e-12))
print("往返最大误差:", np.max(np.abs(reconstructed - signal)))
```

**需要观察的现象与预期结果**：

1. **主频定位**：频率分辨率是 \(1/(N\cdot d) = 1\) Hz，所以 `freqs[peak_idx]` 应**精确等于 50.0 Hz**（50 落在整数频格上）。若把 `f0` 改成非整数（如 50.7 Hz），主峰会出现在最近的频格上、两侧出现「频谱泄漏」——这是后续学习窗函数的入口。
2. **往返还原**：`np.allclose(reconstructed, signal)` 应为 `True`，最大误差在 \(10^{-12}\) 量级，证明 `fft`/`ifft` 是严格互逆的（噪声也被无损还原，因为 FFT 是线性可逆变换）。
3. **对称性（可选）**：因为信号是实数，应有 `S[j] == np.conj(S[N-j])`，幅度谱关于奈奎斯特频率（500 Hz）对称。可加一行 `assert np.allclose(mag[1:N//2], mag[N-1:N//2:-1])` 验证——左半段 `mag[1..N/2-1]` 与右半段倒序 `mag[N-1..N/2+1]` 逐项相等。

**进阶观察（可选）**：把 `N` 改成素数（例如 `N = 997`）再计时，与 `N=1000` 对比，体会「素数长度最慢」。若想兼顾长度与效率，可用 `fftpack.next_fast_len(997)`（会返回 1000）先把信号补零到高效长度。

> 具体主频数值和往返误差的精确位数取决于运行环境与随机种子，**待本地验证**；但「主频≈50 Hz、往返 allclose 为 True」这两条结论是稳定的。

---

## 6. 本讲小结

- `fft(x, n=None, axis=-1, overwrite_x=False)` 是未归一化的前向 DFT，默认沿**最后一个轴**变换；`n` 同时表达截断（`n<实长`）与补零（`n>实长`）。
- 输出采用**标准打包**：`[0 频, 正频, 负频]`，\(n=8\) 时频率下标为 `[0,1,2,3,-4,-3,-2,-1]`；`fftpack.fftfreq` 给出的频率与输出逐位对齐。
- `ifft` 是 `fft` 的逆：指数取正、整体除以 \(n\)（`.mean()`）。一句口诀：**`fft` 求和、`ifft` 求平均**，于是 `ifft(fft(x)) ≈ x`。
- 归一化方向（前向不除、逆向除）由 DUCC 后端的 `_normalization(None, forward)` 自动翻转，`_basic.py` 只做一行委托，因此 `fftpack` 的 `fft`/`ifft` **不暴露** `norm` 参数。
- 性能规律：`n` 为 2 的幂最快、为素数最慢；可用 `fftpack.next_fast_len` 选高效长度补零。
- 实数输入会让 `fft` 自动改用实数 FFT 算法加速近一倍，但**返回类型仍是复数**的标准打包结果（`rfft` 才返回实数打包，见 u2-l2）。

---

## 7. 下一步学习建议

- **u2-l1 多维复数 FFT 与形状校验**：本讲的 `fft`/`ifft` 是一维版本；下一讲进入 `fftn`/`fft2`，看多维输入如何通过 `_helper._good_shape` 校验 `shape`/`axes`。
- **u2-l2 实数序列 FFT（rfft/irfft）与实数打包格式**：本讲提到「实数输入会自动用实数 FFT」，但 `fft` 仍返回复数；`rfft` 才会返回特殊的**实数交错打包**格式，值得专门学一篇。
- **延伸阅读源码**：若你对「一行委托背后的真实计算」感兴趣，可读 DUCC 后端 [`scipy/fft/_duccfft/basic.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/basic.py#L11-L37) 的 `c2c` 与 `fft = functools.partial(c2c, True)`，以及 [`helper.py` 的 `_normalization`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/helper.py#L181-L192)，理解 `backward` 归一化的完整实现。
