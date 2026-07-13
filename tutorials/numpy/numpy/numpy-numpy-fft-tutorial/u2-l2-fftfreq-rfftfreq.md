# fftfreq 与 rfftfreq 频率箱

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清楚 `fftfreq` 是如何按 **standard 频率排列**（0、正频率、Nyquist、负频率）生成完整频率轴的，以及奇偶长度的差异。
- 说清楚 `rfftfreq` 为什么只输出 **非负频率**、为什么把 Nyquist 视为正频率，以及它为何与 `rfft` 的输出逐元素对齐。
- 读懂 [`_helper.py`](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_helper.py) 中这两个函数的全部源码，包括 `N = (n - 1) // 2 + 1` 这一切分技巧、`integer_types` 校验、以及 `device` 参数。
- 能够为一段实际信号分别给 `fft` / `rfft` 的结果匹配上正确的频率轴，并解释两者长度差异。

本讲承接 u2-l1 已经建立的「DFT 数学定义与 standard 频率排列、实输入的 Hermitian 对称性」认知，专门解决一个落地问题：**算出来的频谱，横坐标到底是哪些频率？**

## 2. 前置知识

在进入源码前，先用两段话把直觉补齐（细节在 u2-l1 已讲过，这里只回顾与本讲直接相关的部分）。

**采样、采样率与频率分辨率。** 假设你对一段连续信号每 `d` 秒采一个点，共采了 `n` 个点。那么：

- 采样率（sampling rate）\( f_s = \dfrac{1}{d} \)（单位 Hz，即每秒采样数）。
- 总观测时长 \( T = n \cdot d \)。
- 频率分辨率（每个频率箱的宽度）\( \Delta f = \dfrac{f_s}{n} = \dfrac{1}{n \cdot d} \)。

`fftfreq` / `rfftfreq` 里那行 `val = 1.0 / (n * d)` 算的就是这个 \( \Delta f \)。频率箱的本质是一串整数「频率序号」\( k \)，再统一乘上 \( \Delta f \) 得到以 Hz 为单位的真实频率。

**standard 频率排列。** u2-l1 已经讲过 `fft` 输出数组的排列顺序：

- `A[0]` 是零频（DC，直流分量），对应频率 0；
- `A[1]` … `A[n/2-1]` 是正频率；
- 偶数长度时 `A[n/2]` 是 Nyquist 频率 \( f_s/2 \)；
- 后半段 `A[n/2+1:]`（奇偶都适用）是负频率。

`fftfreq` 返回的就是与这个排列一一对应的频率数组。而 `rfftfreq` 只覆盖前半段（非负频率），因为它要配合只输出非负频率的 `rfft` 使用。

> 一个关键对照：`rfft` 利用实输入的 Hermitian 对称性（\( A[-k] = \overline{A[k]} \)），只保留 \( n//2+1 \) 个非负频率点，丢弃冗余的负频率。因此 `rfftfreq` 的输出长度也正是 \( n//2+1 \)，二者天然对齐。

## 3. 本讲源码地图

本讲只涉及 `numpy/fft/` 下两个文件：

| 文件 | 作用 | 本讲用到的部分 |
| --- | --- | --- |
| [`numpy/fft/_helper.py`](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_helper.py) | 纯 Python 的 4 个 helper（`fftshift`/`ifftshift`/`fftfreq`/`rfftfreq`） | `fftfreq`、`rfftfreq` 两个函数的完整实现，以及模块顶部的 `integer_types` 常量 |
| [`numpy/fft/_helper.pyi`](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_helper.pyi) | 上述函数的类型存根（stub），运行时不参与，仅供类型检查器使用 | `fftfreq` / `rfftfreq` 的一组 `@overload` 签名，以及 `_Device`、`_IntLike` 等类型别名 |

回顾 u1-l2：`_helper.py` 是**纯 Python**实现，与 FFT 算法本身完全解耦——它既不调用 C++ 后端，也不做任何变换计算，只负责「按公式生成一串频率」。这也意味着这两个函数非常短、非常适合逐行精读。

## 4. 核心概念与源码讲解

### 4.1 fftfreq：生成「正频段 + 负频段」的完整频率箱

#### 4.1.1 概念说明

`fftfreq` 解决的问题是：**给定窗口长度 `n` 与采样间距 `d`，返回长度为 `n` 的频率数组，使其与 `fft` / `ifft` 的输出按位置一一对应。**

它要同时满足两个约束：

1. **长度必须等于 `n`**，因为 `fft` 的输出长度就是 `n`。
2. **排列必须与 standard 频率排列一致**：先是 `0` 和正频率，再跳到负频率（偶数 `n` 时中间夹一个负的 Nyquist）。

其数学定义为（与源码 docstring 一致）：

\[
\begin{aligned}
\text{偶数 } n &: \quad f = [\,0,\ 1,\ \dots,\ n/2{-}1,\ {-}n/2,\ \dots,\ {-}1\,] \cdot \frac{1}{n \cdot d} \\
\text{奇数 } n &: \quad f = [\,0,\ 1,\ \dots,\ (n{-}1)/2,\ {-}(n{-}1)/2,\ \dots,\ {-}1\,] \cdot \frac{1}{n \cdot d}
\end{aligned}
\]

注意偶数情形下，**Nyquist 频率 \( f_s/2 \) 只出现一次**，且实现约定把它放在**负频段**的位置（即 \( -n/2 \cdot \Delta f = -f_s/2 \)）。正频段只到 \( n/2 - 1 \)。这是 standard 排列的一个固定约定，不是 bug。

#### 4.1.2 核心流程

`fftfreq` 的算法可以用三步概括：

```
1. 校验 n 必须是整数（int 或 numpy.integer），否则抛 ValueError。
2. 算频率分辨率 val = 1.0 / (n * d)。
3. 把「正频段 + 负频段」两段整数序号拼进一个长度 n 的数组，再整体乘 val：
     a. N = (n - 1) // 2 + 1          # 正频段（含 0）的长度
     b. 前缀 p1 = [0, 1, ..., N-1]    # 0 与正频率
     c. 后缀 p2 = [-(n//2), ..., -1]  # 负频率（偶数 n 时含负 Nyquist）
     d. return concatenate(p1, p2) * val
```

**关键的切分点 `N = (n - 1) // 2 + 1`** 一行同时处理奇偶两种情况：

- 偶数 `n`：\( N = (n{-}1)//2 + 1 = n/2 \)。于是正频段为 `[0, 1, ..., n/2-1]`，负频段 `p2 = arange(-n//2, 0)` 即 `[-n/2, ..., -1]`，正好把负 Nyquist 接在正频段之后。
- 奇数 `n`：\( N = (n{-}1)//2 + 1 = (n{+}1)/2 \)。正频段为 `[0, 1, ..., (n-1)/2]`，负频段 `p2 = arange(-(n//2), 0)` 即 `[-(n-1)/2, ..., -1]`。

无论奇偶，`len(p1) + len(p2) = N + (n//2) = n`，正好填满长度为 `n` 的输出。

#### 4.1.3 源码精读

先看函数签名与装饰器。`fftfreq` 用 `@set_module('numpy.fft')` 装饰，使其 `__module__` 显示为 `numpy.fft`（关于这个装饰器与 `array_function_dispatch` 的区别，详见 u2-l4）：

[numpy/fft/_helper.py:125-126](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_helper.py#L125-L126) —— 用 `set_module` 把函数挂到 `numpy.fft` 命名空间，并定义 `fftfreq(n, d=1.0, device=None)` 签名。

进入函数体，第一件事是整数校验：

[numpy/fft/_helper.py:168-169](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_helper.py#L168-L169) —— 若 `n` 不是 `integer_types`，抛出 `ValueError("n should be an integer")`。

其中 `integer_types` 是模块顶部定义的常量：

[numpy/fft/_helper.py:12](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_helper.py#L12) —— `integer_types = (int, integer)`，即 Python 内置 `int` 与 NumPy 的 `numpy.integer` 两种。

接下来是算法核心（逐行对应 4.1.2 的三步）：

[numpy/fft/_helper.py:170-177](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_helper.py#L170-L177) —— 计算 `val`、用 `empty(n, int)` 开整数缓冲、按 `N` 切分写入 `p1`/`p2` 两段、最后 `* val` 提升为浮点数组返回。

几个值得注意的实现细节：

- `empty(n, int, device=device)` 先建一个**未初始化的 int 数组**作缓冲，而不是直接建 float 数组。频率序号本身是整数，先填整数索引、最后一次性乘标量 `val`，`int64 * float64` 会自然提升为 `float64`，代码因此非常简洁，也避免了重复除法。
- `results * val` 返回的是一个**新数组**（`results` 本身仍是 int、不参与返回），所以返回值的 dtype 是 `float64`。
- `device=device` 被原样透传给 `empty` / `arange`，用于 Array-API 互操作（见 4.3）。

#### 4.1.4 代码实践

> **实践目标：** 直观看到 `fftfreq` 的输出长什么样，并验证它与 `fft` 的输出逐位置对齐。

操作步骤（可在 Python 交互环境中运行）：

```python
import numpy as np

# 1) 先单独观察 fftfreq 本身：n=8（偶数）
print(np.fft.fftfreq(8))
# 预期: [ 0.     0.125  0.25   0.375 -0.5   -0.375 -0.25  -0.125]
#       |-- 正频段(含0) --|  |---- 负频段(含负Nyquist) ----|

# 2) n=9（奇数），注意没有 Nyquist 这一项
print(np.fft.fftfreq(9))
# 预期: [ 0.    0.111 0.222 0.333 0.444 -0.444 -0.333 -0.222 -0.111]

# 3) 配合 d：采样率 fs=100 -> d=0.01，n=10
fs, n = 100, 10
t = np.arange(n) / fs                      # 10 个采样时刻
x = np.sin(2*np.pi*10*t)                    # 一个 10Hz 的正弦
X = np.fft.fft(x)                           # 频谱，长度 10
f = np.fft.fftfreq(n, d=1/fs)               # 频率轴，长度 10
print(f)
# 预期: [  0.  10.  20.  30.  40. -50. -40. -30. -20. -10.]
#         长度与 X 完全一致，可逐位置画在一起
```

**需要观察的现象：**

1. `fftfreq(8)` 的第 5 个元素（索引 4）是 `-0.5`，即负的 Nyquist \( -f_s/2 \)，而正频段只到 `0.375`。
2. `fftfreq(9)` 没有 \( \pm 0.5 \)，因为奇数长度不存在精确的 Nyquist 频率点。
3. 第 3 步里 `f` 的长度与 `X` 完全相同（都是 10），所以可以放心地 `plt.plot(f, np.abs(X))`。

**预期结果：** 在第 3 步的频谱图上，会在 `f = 10` 和 `f = -10` 两处各出现一个尖峰（实正弦信号的 Hermitian 对称表现）。如果想把零频挪到画面中央，再套一层 `np.fft.fftshift`（见 u2-l3）。

> 若你当前环境无法显示图形，「待本地验证」绘图部分；但前两步 `print` 的数值结果是确定的，可直接对照。

#### 4.1.5 小练习与答案

**练习 1：** `np.fft.fftfreq(8)` 的输出是什么？请手算。

> **参考答案：** `val = 1/(8*1) = 0.125`；`N = (8-1)//2+1 = 4`；`p1 = [0,1,2,3]`，`p2 = arange(-4, 0) = [-4,-3,-2,-1]`；拼接后 `*0.125` 得 `[0, 0.125, 0.25, 0.375, -0.5, -0.375, -0.25, -0.125]`。

**练习 2：** 为什么 `fftfreq(8)` 里出现的是 `-0.5` 而不是 `+0.5`？

> **参考答案：** 偶数 `n=8` 时 Nyquist 频率 \( f_s/2 = 0.5 \)（归一化后）在频域只对应一个独立样本，实现约定把它放进负频段（`p2` 的第一个元素 `-n//2 = -4`），正频段只到 `n/2-1 = 3`。这是 standard 排列的固定约定。

**练习 3：** 如何把 `fftfreq(8)` 的结果按频率从小到大排序？

> **参考答案：** `np.fft.fftshift(np.fft.fftfreq(8))`，得到 `[-0.5, -0.375, -0.25, -0.125, 0, 0.125, 0.25, 0.375]`。注意对频谱本身也要同步 `fftshift`，否则横纵坐标会错位（详见 u2-l3）。

---

### 4.2 rfftfreq：只为实数变换生成非负频率轴

#### 4.2.1 概念说明

`rfftfreq` 是 `fftfreq` 的「半身版」：它只返回**非负频率**，专门配合 `rfft` / `irfft` 使用。

为什么需要单独造一个？因为 `rfft` 利用实输入的 Hermitian 对称性，把输出从 `n` 个点压缩成 \( n//2+1 \) 个点（只保留 0、正频率、以及 Nyquist）。如果你用 `fftfreq(n)` 去配 `rfft` 的结果，长度对不上（`n` vs `n//2+1`），横纵坐标立刻错位。`rfftfreq` 正好把频率轴也截成 \( n//2+1 \) 长，且全部非负。

它的数学定义为：

\[
\begin{aligned}
\text{偶数 } n &: \quad f = [\,0,\ 1,\ \dots,\ n/2{-}1,\ n/2\,] \cdot \frac{1}{n \cdot d} \\
\text{奇数 } n &: \quad f = [\,0,\ 1,\ \dots,\ (n{-}1)/2{-}1,\ (n{-}1)/2\,] \cdot \frac{1}{n \cdot d}
\end{aligned}
\]

与 `fftfreq` 最大的差别在 docstring 里的一句话：**「the Nyquist frequency component is considered to be positive」**。偶数 `n` 时，`fftfreq` 把 Nyquist 放在负频段（`-n/2`），而 `rfftfreq` 把它当作正频率（`+n/2`）放在数组末尾。这并非矛盾——`rfftfreq` 只表示非负那一半，Nyquist 当然以正号出现。

#### 4.2.2 核心流程

`rfftfreq` 比姊妹函数更短，因为不需要拼接负频段：

```
1. 校验 n 必须是整数（同 fftfreq）。
2. 算频率分辨率 val = 1.0 / (n * d)。
3. N = n // 2 + 1                         # 非负频率点数（也是 rfft 输出长度）
4. results = arange(0, N)                 # [0, 1, ..., N-1]，全是非负整数序号
5. return results * val
```

切分点从 `fftfreq` 的 `N = (n-1)//2 + 1` 变成了这里的 `N = n//2 + 1`：

- 偶数 `n`：\( N = n/2 + 1 \)，序号 `[0, 1, ..., n/2]`，末尾的 `n/2` 正是当作正频率的 Nyquist。
- 奇数 `n`：\( N = (n-1)/2 + 1 \)，序号 `[0, 1, ..., (n-1)/2]`。

无论奇偶，输出长度都是 \( n//2+1 \)，与 `rfft` 的输出长度完全一致——这是它「能配 rfft」的根本原因。

#### 4.2.3 源码精读

[numpy/fft/_helper.py:180-181](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_helper.py#L180-L181) —— `@set_module('numpy.fft')` 装饰，定义 `rfftfreq(n, d=1.0, device=None)`。

函数体同样以整数校验开头，然后是极其简短的主体：

[numpy/fft/_helper.py:230-235](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_helper.py#L230-L235) —— 校验 `n`、算 `val`、取 `N = n//2+1`、`arange(0, N)` 直接得到非负频率序号、乘 `val` 返回。

对比 `fftfreq` 的实现要点：

- 这里**不需要 `empty` 缓冲**，因为 `arange(0, N)` 一次性就生成了完整的非负序号数组，直接 `* val` 即可。
- 同样靠 `int64 * float64` 的类型提升得到 `float64` 输出。
- docstring 明确点出「Unlike `fftfreq` … the Nyquist frequency component is considered to be positive」，对应到代码就是 `N = n//2 + 1`（偶数 `n` 时末尾多一个 `+n/2`），而非 `fftfreq` 那种把 `-n/2` 接在正频段之后。

#### 4.2.4 代码实践

> **实践目标：** 比较 `fftfreq` 与 `rfftfreq` 的长度与取值差异，并验证 `rfftfreq` 能与 `rfft` 的输出逐位置对齐。

```python
import numpy as np

fs, n = 100, 10
d = 1 / fs

# 1) 直接对比两个频率轴
f_full  = np.fft.fftfreq(n, d=d)    # 配 fft
f_r     = np.fft.rfftfreq(n, d=d)   # 配 rfft
print("fftfreq :", f_full)          # 长度 10，含负频率
print("rfftfreq:", f_r)             # 长度 6，全非负

# 2) 用同一实信号，分别做 fft 与 rfft，看频谱长度
t = np.arange(n) / fs
x = 0.6*np.sin(2*np.pi*10*t) + 0.4*np.sin(2*np.pi*30*t)   # 含 10Hz 与 30Hz
X_full = np.fft.fft(x)
X_r    = np.fft.rfft(x)
print("fft  长度:", X_full.shape, " rfft 长度:", X_r.shape)
# 预期: fft 长度: (10,)   rfft 长度: (6,)

# 3) 关键验证：rfftfreq 长度 == rfft 输出长度
assert f_r.shape == X_r.shape, "频率轴与频谱长度不一致！"
```

**需要观察的现象与预期结果：**

1. `fftfreq(10, d=0.01)` 长度 10：`[0, 10, 20, 30, 40, -50, -40, -30, -20, -10]`（含负频率，Nyquist 是 `-50`）。
2. `rfftfreq(10, d=0.01)` 长度 6：`[0, 10, 20, 30, 40, 50]`（全非负，Nyquist 是 `+50`）。
3. `rfft` 的输出长度恰好是 6（\( 10//2+1 \)），与 `rfftfreq` 完全相等，因此可以放心 `plt.plot(f_r, np.abs(X_r))`。
4. 在 `rfft` 频谱上会在 `f_r = 10` 与 `f_r = 30` 处看到两个尖峰——这正是信号里两个正弦分量的频率。

> 绘图部分若环境不支持，「待本地验证」；断言 `f_r.shape == X_r.shape` 是纯逻辑检查，必过。

#### 4.2.5 小练习与答案

**练习 1：** `np.fft.rfftfreq(8)` 的输出是什么？长度多少？

> **参考答案：** `val = 0.125`；`N = 8//2+1 = 5`；`arange(0,5) = [0,1,2,3,4]`；`*0.125` 得 `[0, 0.125, 0.25, 0.375, 0.5]`，长度 5。末尾的 `0.5` 是当作正频率的 Nyquist。

**练习 2：** 对同一个 `n=10`，`rfftfreq(10)` 与 `fftfreq(10)` 的输出长度各是多少？为什么不同？

> **参考答案：** `rfftfreq(10)` 长度 \( 10//2+1 = 6 \)，`fftfreq(10)` 长度 10。前者只覆盖非负频率（配 `rfft` 的压缩输出），后者覆盖完整正负频率（配 `fft` 的完整输出）。

**练习 3：** 假设你误用 `fftfreq(10)` 去画 `rfft(x)` 的频谱，会发生什么？

> **参考答案：** 长度不匹配（10 vs 6），`plot` 会因广播失败报错；即便强行截断，也会把负频率当成正频率，导致横坐标完全错乱。正确做法是始终用 `rfftfreq` 配 `rfft`、`fftfreq` 配 `fft`。

---

### 4.3 共享机制：integer_types 校验、device 参数与 .pyi 类型签名

`fftfreq` 与 `rfftfreq` 共享三处公共约定，单独拎出来讲清楚，避免在两个函数里重复。

#### 4.3.1 概念说明

- **整数校验：** `n` 代表「窗口长度」，必须是整数。源码用 `integer_types = (int, integer)` 这一元组做 `isinstance` 检查，把浮点 `n`（如 `8.0`）挡在门外并给出明确报错。
- **device 参数：** NumPy 2.0.0 起新增，仅用于 Array-API 互操作——让遵循数组 API 标准的第三方数组库能指明数组创建位置。对本机 CPU 而言，传值只能是 `"cpu"`。
- **类型存根（`.pyi`）：** 运行时不参与，只供 mypy / pyright 等类型检查器使用。`_helper.pyi` 用一组 `@overload` 精确描述了「`d` 的类型如何决定返回数组 dtype」的提升规则。

#### 4.3.2 核心流程

三者落点：

```
integer_types 校验：  函数入口 if not isinstance(n, integer_types): raise ValueError(...)
device 参数：         透传给 empty(n, int, device=device) / arange(0, N, dtype=int, device=device)
.pyi 类型签名：        对每个 (n, d) 组合声明返回的标量类型，反映类型提升
```

值得强调的是 `device` 的「只透传」性质：`fftfreq` / `rfftfreq` 自己不解释 `device`，而是把它原样交给底层的 `empty` / `arange`。在纯 CPU 的 NumPy 里，`device=None` 与 `device="cpu"` 等价；传别的值会由 `empty` / `arange` 报错。

#### 4.3.3 源码精读

**整数校验常量：**

[numpy/fft/_helper.py:12](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_helper.py#L12) —— 定义 `integer_types = (int, integer)`，其中 `integer` 来自 `from numpy._core import ... integer`，即 `numpy.integer`。

`fftfreq` 与 `rfftfreq` 各有一份完全相同的校验代码：

[numpy/fft/_helper.py:168-169](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_helper.py#L168-L169) 与 [numpy/fft/_helper.py:230-231](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_helper.py#L230-L231) —— 两处都是 `if not isinstance(n, integer_types): raise ValueError("n should be an integer")`。

> 小知识：Python 的 `bool` 是 `int` 的子类，所以 `isinstance(True, int)` 为 `True`，`fftfreq(True)` 会通过校验（被当成 `n=1`）；但 `numpy.bool_` 不是 `numpy.integer` 的子类，`fftfreq(np.bool_(True))` 会被拒。这是一个容易被忽略的边界。

**device 参数（docstring 标注 `.. versionadded:: 2.0.0`）：**

[numpy/fft/_helper.py:145-149](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_helper.py#L145-L149) —— `fftfreq` 的 docstring 说明 `device` 仅供 Array-API 互操作，传值必须为 `"cpu"`。

透传落点（注意 `device=device` 出现在 `empty` / `arange` 调用里）：

[numpy/fft/_helper.py:171](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_helper.py#L171) 与 [numpy/fft/_helper.py:173](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_helper.py#L173) —— `empty(n, int, device=device)` 与 `arange(0, N, dtype=int, device=device)` 都把 `device` 原样下传。

**`.pyi` 类型签名：**

[numpy/fft/_helper.pyi:10](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_helper.pyi#L10) 与 [numpy/fft/_helper.pyi:12](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_helper.pyi#L12) —— 定义 `_Device = Literal["cpu"]` 与 `_IntLike = int | np.integer`，分别对应运行时的 `device` 取值与 `integer_types` 校验。

[numpy/fft/_helper.pyi:48-53](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_helper.pyi#L48-L53) —— `fftfreq` 的「默认」overload：`d` 为标量浮点或 float 时，返回 `_Array[_1D, np.float64]`。其余 overload（如 `d` 为 `complex` 返回 `complex128`、为 `longdouble` 返回 `longdouble`）编码了完整的类型提升表。`rfftfreq` 的 overload 在 [numpy/fft/_helper.pyi:98-103](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_helper.pyi#L98-L103) 起与之结构相同。

这些 overload 与运行时并不冲突——运行时统一走 `int 数组 * val(float)` 得到 `float64`；存根只是把「`d` 是复数时结果应为复数」这种**期望语义**提前告诉类型检查器。

#### 4.3.4 代码实践

> **实践目标：** 触发 `integer_types` 校验与 `device` 透传，观察实际行为。

```python
import numpy as np

# 1) 整数校验：传 float 会怎样？
try:
    np.fft.fftfreq(8.0)
except ValueError as e:
    print("ValueError:", e)          # 预期: n should be an integer

# 1b) numpy.int32 是合法的（属于 numpy.integer）
print(np.fft.fftfreq(np.int32(8)))   # 正常返回，与 fftfreq(8) 一致

# 2) device 参数：None 与 "cpu" 等价
a = np.fft.fftfreq(8, device=None)
b = np.fft.fftfreq(8, device="cpu")
print(np.array_equal(a, b))          # 预期: True
```

**需要观察的现象与预期结果：**

1. `fftfreq(8.0)` 抛 `ValueError: n should be an integer`。
2. `fftfreq(np.int32(8))` 正常运行，因为 `np.int32` 是 `numpy.integer` 的子类，属于 `integer_types`。
3. `device=None` 与 `device="cpu"` 输出完全相同；若尝试 `device="gpu"` 之类非法值，会由底层 `empty`/`arange` 抛错（「待本地验证」具体报错文案，因其取决于 NumPy 版本）。

> 这些断言性的行为是确定的；`device="gpu"` 的报错文案请以本地实际输出为准。

#### 4.3.5 小练习与答案

**练习 1：** 调用 `np.fft.fftfreq(10.0)` 会发生什么？为什么？

> **参考答案：** 抛 `ValueError("n should be an integer")`。因为 `10.0` 是 `float`，不属于 `integer_types = (int, integer)`。窗口长度 `n` 必须是整数，源码在入口处显式拦截。

**练习 2：** `device` 参数是哪个版本引入的？有什么限制？

> **参考答案：** NumPy 2.0.0 引入（docstring 标注 `versionadded:: 2.0.0`）。它仅供 Array-API 互操作，在纯 CPU 的 NumPy 里传值只能是 `"cpu"`，函数本体只是把它透传给 `empty` / `arange`。

**练习 3：** 为什么源码先建 `empty(n, int)` 整数缓冲、最后再 `* val`，而不直接建 float 数组逐个填频率值？

> **参考答案：** 频率序号 \( k \) 本身是整数，先用整数缓冲填好 \( [0,1,\dots] \) 与负频段，再统一乘标量 `val`，代码更简洁（两段拼接 + 一次缩放），且 `int64 * float64` 自然提升为 `float64`，无需手动管理 dtype。这也让 `device` 只需在一处（`empty`/`arange`）透传。

## 5. 综合实践

把本讲全部内容串起来：**给一段含两个频率分量的实信号，分别走「fft 全谱」和「rfft 单边谱」两条路，正确地为每条路配上频率轴并解释差异。**

```python
import numpy as np

fs = 100                       # 采样率 100Hz
n  = 16                        # 窗口长度（偶数，便于观察 Nyquist）
d  = 1 / fs
t  = np.arange(n) / fs
x  = np.sin(2*np.pi*10*t) + 0.5*np.sin(2*np.pi*25*t)   # 10Hz + 25Hz

# 路线 A：fft 全谱 + fftfreq
X_full = np.fft.fft(x)
f_full = np.fft.fftfreq(n, d=d)         # 长度 16，含正负频率

# 路线 B：rfft 单边谱 + rfftfreq
X_r    = np.fft.rfft(x)
f_r    = np.fft.rfftfreq(n, d=d)        # 长度 9，全非负

print("A fftfreq  :", np.round(f_full, 2))
print("A |fft|    :", np.round(np.abs(X_full), 2))
print("B rfftfreq :", np.round(f_r, 2))
print("B |rfft|   :", np.round(np.abs(X_r), 2))

# 自检：频率轴长度必须与对应频谱长度一致
assert f_full.shape == X_full.shape
assert f_r.shape    == X_r.shape
```

请回答下列问题（这是检验你是否真正掌握本讲的标志）：

1. 路线 A 的 `|fft|` 在哪些频率处有尖峰？为什么是「成对」出现的？
2. 路线 B 的 `|rfft|` 尖峰出现在哪些频率？为什么数量是路线 A 的一半？
3. `f_full` 中那个等于 `-fs/2 = -50` 的元素，在 `f_r` 中对应什么？为什么符号变了？
4. 如果把 `n` 从 16 改成 15（奇数），`f_full` 与 `f_r` 的长度分别变成多少？还会出现 `±50` 的 Nyquist 吗？

> 参考要点：(1) 在 `f = 10` 与 `f = -10`、`f = 25` 与 `f = -25` 各成对出现尖峰，源于实信号的 Hermitian 对称；(2) `rfft` 丢弃冗余负频率，故只在 `f_r = 10`、`f_r = 25` 各一个尖峰；(3) `f_full` 里的 `-50`（负 Nyquist）在 `f_r` 里以 `+50`（正 Nyquist）出现，因为 `rfftfreq` 把 Nyquist 视为正；(4) `n=15` 时 `f_full` 长 15、`f_r` 长 \( 15//2+1=8 \)，奇数长度无精确 Nyquist 点，故不再出现 `±50`。

## 6. 本讲小结

- `fftfreq(n, d)` 返回长度为 `n` 的完整频率轴，按 standard 排列：`0、正频率、（偶数时的）负 Nyquist、负频率`，与 `fft`/`ifft` 输出逐位置对齐。
- 切分点 `N = (n-1)//2 + 1` 一行兼容奇偶：偶数 `n` 时 `N=n/2`、奇数 `n` 时 `N=(n+1)/2`，正频段 `arange(0,N)`、负频段 `arange(-n//2, 0)`。
- `rfftfreq(n, d)` 返回长度为 `n//2+1` 的非负频率轴，与 `rfft` 输出逐位置对齐；它把 Nyquist 视为**正**频率（`N = n//2+1`），这是与 `fftfreq` 最本质的区别。
- 两者都用「整数序号数组 `* val`（`val=1/(n·d)`）」的写法生成频率，靠 `int*float` 提升得到 `float64`，代码极简。
- 公共约定：`integer_types = (int, integer)` 在入口拦截非整数 `n`；`device`（2.0.0 新增）仅供 Array-API 互操作、必须为 `"cpu"`、被透传给 `empty`/`arange`；`.pyi` 用一组 `@overload` 描述类型提升。

## 7. 下一步学习建议

- **紧接的下一篇 u2-l3** 会讲 `fftshift` / `ifftshift`——本讲练习里反复出现的「把零频搬到频谱中央」正是靠它实现，学完后你就能画出居中的、符合直觉的双边频谱图。
- 如果想立刻看到「频率轴 + 变换」在主流程里如何被串起来，可以预习 u3-l1（`fft`/`ifft` 与 `_raw_fft` 主流程）和 u3-l4（`rfft`/`irfft` 实数变换与 Hermitian 对称），那里会解释 `rfft` 输出长度为何恰好是 `rfftfreq` 的长度。
- 想深入理解 `device` 与 Array-API 互操作的读者，建议查阅 NumPy 关于 Array API standard 的文档；这超出了 `numpy.fft` 子包本身，但有助于理解 `device` 参数为何如此「克制」。
- 继续阅读源码时，可以把 [`_helper.py`](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_helper.py) 与 [`tests/test_helper.py`](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/tests/test_helper.py) 对照看——`TestFFTFreq` 与 `TestRFFTFreq` 里的断言（如 `9 * fft.fftfreq(9)` 应等于 `[0,1,2,3,4,-4,-3,-2,-1]`）是验证你理解是否正确的最佳标尺。
