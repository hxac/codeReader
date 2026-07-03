# 实数与 Hermitian 变换：rfft/irfft 与 hfft/ihfft

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 `rfft` 为什么对实输入只输出「半谱」，以及 `irfft` 如何仅凭半谱还原出实信号；
- 说清楚 `hfft`/`ihfft` 与 `rfft`/`irfft` 是怎样一对「镜像」关系——对称性落在哪一端；
- 读懂 `_basic_backend.py` 中的 `_swap_direction`，并能解释它为什么只在「非 numpy 的 xp 分支」里出现，而在默认的 ducc/numpy 路径里不需要。

本讲承接 [u2-l1](./u2-l1-complex-fft.md) 已建立的「四层调用链 + `norm` 三模式 + `Dispatchable` 分派协议」认知，不再重复这些概念，而是聚焦「实输入 / Hermitian 输入」这两类特殊变换。

## 2. 前置知识

### 2.1 Hermitian 对称性

对于一个实序列 \(x[n]\)，它的离散傅里叶变换 \(X[k]\) 满足：

\[
X[k] = \overline{X[-k]}
\]

即「负频率分量等于对应正频率分量的复共轭」。这种性质叫 **Hermitian 对称性**。正因为如此，实信号的频谱里有一半信息是冗余的，可以不算、不存。

> 注意：这里的 Hermitian 指的是「沿同一个轴的共轭对称」 \(X[k]=\overline{X[-k]}\)，**不是**线性代数里「转置等于自身共轭」的 Hermitian 矩阵 \(A_{ij}=\overline{A_{ji}}\)。`hfftn` 的 docstring 里专门提醒了这一点。

### 2.2 共轭翻转指数符号

复共轭会把傅里叶核里的指数符号翻转：

\[
\overline{\displaystyle\sum_n x[n]\,e^{-2\pi i kn/N}}
\;=\;
\sum_n \overline{x[n]}\,e^{+2\pi i kn/N}
\]

「正变换用 \(e^{-2\pi i}\)，逆变换用 \(e^{+2\pi i}\)」。本讲后面会看到，`hfft`/`ihfft` 在非 numpy 路径下正是靠 `xp.conj`（取共轭）来「借用」`irfft`/`rfft` 这两个反方向的内核。

### 2.3 复习：norm 的方向相关缩放

在 [u2-l1](./u2-l1-complex-fft.md) 中我们讲过，`norm` 字符串经 `_NORM_MAP` 映射为整数 `0/1/2`，再用 `_normalization(norm, forward)` 按方向调整：

- [_duccfft/helper.py:181](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/helper.py#L181)：`_NORM_MAP = {None: 0, 'backward': 0, 'ortho': 1, 'forward': 2}`
- [_duccfft/helper.py:184-188](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/helper.py#L184-L188)：`return inorm if forward else (2 - inorm)`

也就是说，`forward=True`（正变换）和 `forward=False`（逆变换）会拿到「互补」的归一化模式。本讲的 `_swap_direction` 就是手工在字符串层面完成同样的「互补翻转」。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [_basic.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py) | 公共 API 层。`rfft`/`irfft`/`hfft`/`ihfft` 在此只声明签名、写 docstring，函数体一律 `return (Dispatchable(x, np.ndarray),)`，是分派协议声明而非计算代码。 |
| [_basic_backend.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic_backend.py) | 后端层。`_execute_1D` 把 1-D 变换路由到 ducc 或 `xp.fft`；`hfftn`/`ihfftn` 在 xp 分支里复用 `irfftn`/`rfftn`，并用 `_swap_direction` 翻转 norm。 |
| [_duccfft/basic.py](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/basic.py) | 计算核心层。`r2c`（实→复半谱）/`c2r`（复半谱→实）两个内核，用 `functools.partial` 配 `forward` 派生出 `rfft`/`ihfft` 与 `hfft`/`irfft`。 |

记住四层调用链（沿用 [u2-l1](./u2-l1-complex-fft.md)）：

```
_basic.rfft → _basic_backend.rfft → _execute_1D → _duccfft.rfft (=r2c partial True) → pyduccfft.r2c
```

`hfft` 在 ducc 路径下链尾是 `_duccfft.hfft`（`c2r partial True`）；在 xp 路径下则被改写成 `irfftn(...)`，这就是本讲的看点。

---

## 4. 核心概念与源码讲解

### 4.1 实数变换 rfft/irfft：只存半谱

#### 4.1.1 概念说明

`fft` 对任意复输入做正变换，输出长度等于输入长度 `n`。但当输入是**实数**时，由 §2.1 的 Hermitian 对称性，输出的负频率一半完全是正频率一半的共轭，存下来纯属浪费。

`rfft` 就是「为实输入量身定做的 `fft`」：它只计算并返回**非负频率**那一半，输出长度从 `n` 降到：

\[
\text{len}(\text{rfft 输出}) = \left\lfloor n/2 \right\rfloor + 1
\]

`irfft` 是它的逆运算：输入半谱，输出实信号。难点在于——半谱里**没有记录原始长度 `n`**（偶数 `n` 还是奇数 `n` 对应同样的半谱长度），所以 `irfft` 必须由调用者显式告诉它「你想要多长的实输出」，缺省值 `n = 2*(m-1)`（`m` 为输入长度）默认按**偶数**长度还原。

#### 4.1.2 核心流程

一次 `rfft(x)` 的四层穿透（输入实，输出复半谱）：

1. `_basic.rfft` 仅 `return (Dispatchable(x, np.ndarray),)`，告诉 uarray「`x` 是可替换参数」。
2. `_basic_backend.rfft` 调 `_execute_1D('rfft', _duccfft.rfft, x, ...)`。
3. `_execute_1D` 判断：numpy 数组直连 `_duccfft.rfft`；否则走 `xp.fft.rfft` 或回退到 numpy。
4. `_duccfft.rfft` 其实是 `functools.partial(r2c, True)`，进入 `r2c` 内核，最终调用 C 扩展 `pyduccfft.r2c`。

注意一个细节：`rfft` 要求实输入。docstring 明确写着「If the input `a` contains an imaginary part, it is silently discarded.」（虚部被静默丢弃）。

#### 4.1.3 源码精读

公共签名与 docstring（注意函数体只是分派声明）：

[_basic.py:280-281](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py#L280-L281) 定义 `rfft`；[_basic.py:321-322](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py#L321-L322) 在 docstring 里写明输出长度规律「`n` 为偶则 `(n/2)+1`，`n` 为奇则 `(n+1)/2`」；[_basic.py:370](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py#L370) 是那个标志性的 `return (Dispatchable(x, np.ndarray),)`。

`irfft` 同理，[_basic.py:395-401](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py#L395-L401) 解释了为什么 `n` 默认是 `2*(m-1)`——半谱丢掉了「原信号是偶数还是奇数长度」的信息。

后端层只是一层薄转发：

[_basic_backend.py:89-98](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic_backend.py#L89-L98) —— `rfft`/`irfft` 把全部参数原样喂给 `_execute_1D`，分别绑定 `_duccfft.rfft` / `_duccfft.irfft`。

计算核心里，实→复和复→实共用一对内核：

[_duccfft/basic.py:64-67](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/basic.py#L64-L67) —— `rfft = partial(r2c, True)`、`ihfft = partial(r2c, False)`，二者复用同一个 `r2c` 内核，仅 `forward` 不同。

[_duccfft/basic.py:98-101](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/basic.py#L98-L101) —— `hfft = partial(c2r, True)`、`irfft = partial(c2r, False)`，二者复用同一个 `c2r` 内核。

这条派生关系非常重要，它是后面理解 `_swap_direction` 的前提：**`hfft` 与 `irfft` 本就是同一个 `c2r` 内核的两个方向**。

#### 4.1.4 代码实践

**目标**：直观看到 `rfft` 的半谱长度，并用 `irfft` 验证可逆性。

操作步骤（示例代码，可保存为 `rfft_demo.py` 运行）：

```python
# 示例代码
import numpy as np
import scipy.fft as sf

x = np.array([0.0, 1.0, 0.0, 0.0, 2.0, 0.0, 0.0, 0.0])  # 长度 8 的实信号
X_full = sf.fft(x)      # 复 fft，长度 8
X_half = sf.rfft(x)     # 实 rfft，长度 8//2+1 = 5

print("fft 长度 :", X_full.shape)   # (8,)
print("rfft 长度:", X_half.shape)  # (5,)
print("前 5 项是否相等:", np.allclose(X_full[:5], X_half))  # True

# irfft 还原；必须显式给出原始长度 8
x_back = sf.irfft(X_half, n=len(x))
print("还原误差:", np.max(np.abs(x_back - x)))  # 约 1e-16
```

需要观察的现象：

1. `rfft` 输出长度为 `8//2+1 = 5`，且等于 `fft` 输出的前 5 项。
2. `irfft(X_half, n=8)` 几乎完美还原 `x`（误差量级 `1e-16`）。
3. 若把 `irfft(X_half)` 的 `n` 省略，默认 `n = 2*(5-1) = 8`，本例恰好对；但若原信号是奇数长度，省略 `n` 会得到错误长度——这正是 docstring 强调「必须显式给 `n`」的原因。

预期结果：上面三条 print 依次输出 `(8,)` / `(5,)` / `True` / `约 1e-16`。

#### 4.1.5 小练习与答案

**练习 1**：把上面 `x` 的长度改成 7（奇数），`rfft(x)` 的输出长度是多少？`fft(x)` 的输出长度又是多少？

**答案**：`rfft` 输出 `(7+1)//2 = 4`；`fft` 仍输出 7。

**练习 2**：为什么 `rfft` 在 `n` 为偶数时，输出最后一项一定是实数？

**答案**：偶数 `n` 时，最后一项对应 Nyquist 频率 `fs/2`，它「自己和自己共轭」（正负 Nyquist 重叠），由 Hermitian 对称 `X[k]=conj(X[-k])` 推出 `X[n/2] = conj(X[n/2])`，故必为实数。

---

### 4.2 Hermitian 变换 hfft/ihfft：对称在另一端

#### 4.2.1 概念说明

`rfft`/`irfft` 处理的是「**时域实**、频域 Hermitian」的情形。`hfft`/`ihfft` 是它的镜像：处理「**时域 Hermitian**、频域实」的情形——即输入信号本身满足 Hermitian 对称，它的频谱是实数。

对比两张表：

| 函数 | 输入域 | 输出域 | 长度变化 | 方向 |
|------|--------|--------|----------|------|
| `rfft`  | 实（时域） | 复半谱（频域 Hermitian） | `n → n//2+1` | 正 |
| `irfft` | 复半谱（频域 Hermitian） | 实（时域） | `m → n`（需指定） | 逆 |
| `hfft`  | 复半谱（时域 Hermitian） | 实（频域） | `m → n`（需指定） | 正 |
| `ihfft` | 实（频域） | 复半谱（时域 Hermitian） | `n → n//2+1` | 逆 |

关键洞察：**`hfft` 与 `irfft` 接受同样形状的输入（复半谱）、产出同样形状的输出（实数组）**，唯一区别是「正变换 vs 逆变换」的归一化与指数符号约定；`ihfft` 与 `rfft` 同理。这个「镜像」关系，正是后端层能用 `irfftn` 实现 `hfftn`、用 `rfftn` 实现 `ihfftn` 的依据。

#### 4.2.2 核心流程

`hfft` 在两条路径下走法不同：

**ducc/numpy 路径**（默认）：`hfft` 是一等公民，直接由 `c2r` 内核 `forward=True` 计算，不需要任何 norm 翻转。

**xp（非 numpy）路径**：`hfftn` 没有等价的 `xp.fft.hfftn` 可调（多数数组库不提供），于是改写成「取共轭 + 调 `irfftn`」：

1. `xp.conj(x)` —— 用共轭抵消正/逆变换的指数符号差（§2.2）。
2. 调 `irfftn(..., _swap_direction(norm), ...)` —— 用 norm 翻转抵消正/逆变换的归一化方向差。
3. 两步合起来，让「用逆内核 `irfftn` 算出的结果」在数值上等于「正变换 `hfftn`」。

`ihfftn` 对称地把 `rfftn` 的结果取共轭。

#### 4.2.3 源码精读

公共签名（同样只是分派声明）：

[_basic.py:477-478](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py#L477-L478) 定义 `hfft`；[_basic.py:561-562](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py#L561-L562) 定义 `ihfft`。两者的 docstring（[_basic.py:533-540](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic.py#L533-L540)）用一句话点明了镜像关系：「`hfft`/`ihfft` are a pair analogous to `rfft`/`irfft`, but for the opposite case」。

后端层的一等公民路径（ducc 直连）：

[_basic_backend.py:169-178](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic_backend.py#L169-L178) —— `hfftn` 先判断 `is_numpy(xp)`：是则直接 `_duccfft.hfftn(...)`（即 `c2rn` `forward=True`，无需翻转）；否则取共轭后调 `irfftn(..., _swap_direction(norm), ...)`。

[_basic_backend.py:186-193](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic_backend.py#L186-L193) —— `ihfftn` 同构：numpy 直连 `_duccfft.ihfftn`；否则返回 `xp.conj(rfftn(..., _swap_direction(norm), ...))`。

ducc 核心里 `hfft`/`irfft` 共用 `c2r`：

[_duccfft/basic.py:70-95](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/basic.py#L70-L95) —— `c2r` 把复半谱变实输出，靠 `n = (tmp.shape[axis] - 1) * 2`（缺省）还原偶数长度；最终 `pfft.c2r(...)` 落到 C 扩展。

另外注意 [_basic_backend.py:19](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic_backend.py#L19) 的 `complex_funcs` 集合里同时包含 `'hfft'` 和 `'irfft'`：它标记「这些函数在数组标准后端里期望复数输入」，因此 `_execute_1D` 对它们会做 `xp_float_to_complex` 的兜底转换（见 [_basic_backend.py:38-44](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic_backend.py#L38-L44)）。

#### 4.2.4 代码实践

**目标**：构造一个 Hermitian 对称的「频谱」，用 `hfft` 得到实输出，并和直接 `fft` 的结果对照。

```python
# 示例代码
import numpy as np
import scipy.fft as sf

# 构造一个 Hermitian 对称的复序列（长度 6）
# 满足 X[k] == conj(X[-k])：X[0],X[3] 为实；X[2]=conj(X[1])
X = np.array([1+0j, 2+1j, -1+0j, 3+0j, -1-0j, 2-1j])
print("是否 Hermitian 对称:", np.allclose(X, np.conj(X[np.r_[0, 5, 4, 3, 2, 1]])))  # True

y_h = sf.hfft(X, n=10)   # 取前半 + 补零到 n=10，做正 Hermitian 变换
y_f = sf.fft(X, n=10)    # 对照：对同样的 X 做普通正 fft
print("hfft 输出 dtype:", y_h.dtype)            # float64（实）
print("fft  虚部最大值:", np.max(np.abs(y_f.imag)))  # 约为 0
print("两者实部接近:", np.allclose(y_h.real, y_f.real, atol=1e-10))  # True
```

需要观察的现象：

1. `hfft` 的输出 `dtype` 是 `float64`（实数），因为输入 Hermitian 对称 ⇒ 频谱实。
2. 对同样的 Hermitian 输入，`fft` 输出的虚部接近 0，实部与 `hfft` 一致——印证「`hfft` 本质是对 Hermitian 输入做正变换」。

预期结果：三条判断依次为 `True` / `float64` / `True`（数值容差内）。若本地环境精度不同，`allclose` 的容差可适当放宽。

#### 4.2.5 小练习与答案

**练习 1**：`ihfft(hfft(a, 2*len(a)-2))` 是否等于 `a`？为什么强调 `2*len(a)-2`？

**答案**：等于 `a`（数值精度内）。因为 `hfft` 默认输出长度 `2*(m-1)`，但若原信号 `a` 是奇数长度，必须显式指定偶数 `2*len(a)-2`（或奇数 `2*len(a)-1`）才能让 `ihfft` 完整还原，否则会丢失「奇偶长度」信息（与 `irfft` 同理）。

**练习 2**：`rfft` 和 `ihfft` 都把「实输入」变成「复半谱」，它们的差别在哪？

**答案**：方向相反。`rfft` 是**正变换**（`r2c` `forward=True`），`ihfft` 是**逆变换**（`r2c` `forward=False`）；归一化方向互补，指数符号相反。后端层里 `ihfftn` 正是用 `conj(rfftn(..., _swap_direction(norm)))` 来实现这层「方向 + 共轭」的差异。

---

### 4.3 _swap_direction：方向翻转的归一化补偿

#### 4.3.1 概念说明

`_swap_direction` 是 [_basic_backend.py:158-166](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic_backend.py#L158-L166) 里一个极小的工具函数，作用只有一个：把 norm 字符串的「方向」翻一下——

\[
\texttt{backward} \leftrightarrow \texttt{forward}, \qquad \texttt{ortho} \rightarrow \texttt{ortho}, \qquad \texttt{None} \rightarrow \texttt{forward}
\]

它**只服务于 xp（非 numpy）路径**下的 `hfftn`/`ihfftn`。理解它的关键是搞清楚「为什么要翻」。

#### 4.3.2 核心流程：为什么必须翻 norm

回顾 [_duccfft/helper.py:184-188](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/helper.py#L184-L188) 的 `_normalization(norm, forward)`：`forward=True` 时返回 `inorm`，`forward=False` 时返回 `2 - inorm`。也就是说，内核自带「按方向互补」的逻辑。

在 ducc/numpy 路径下，`hfft` 是 `c2r` 的 `forward=True`，`ihfft` 是 `r2c` 的 `forward=False`，方向由 `forward` 标志原生处理，**不需要翻 norm**。

但在 xp 路径下没有 `xp.fft.hfftn`，于是：

- `hfftn`（用户期望的「正变换」）被改写成调 `irfftn`（`c2rn` 的 `forward=False`，是个**逆内核**）。
- 用户给 `hfft` 传 `norm="backward"` 时，期望「正变换不归一化」（`inorm=0`）。但若直接把这个字符串透传给 `irfftn`，逆内核会按 `2 - 0 = 2` 归一化（变成 1/N 缩放），结果就错了。

解决办法：在字符串层面先做 `_swap_direction`，把 `backward` 翻成 `forward`，再交给 `irfftn`。于是 `irfftn` 的 `_normalization('forward', False) = 2 - 2 = 0`，正好等于 `hfft` 正变换应得的 `0`。用一张表对照：

| 用户传给 `hfft` 的 norm | `inorm` | `hfft`(正) 应得模式 | `_swap_direction` 后 | `irfftn`(逆) `_normalization(swapped, False)` |
|-------------------------|---------|--------------------|----------------------|----------------------------------------------|
| `backward`              | 0       | 0                  | `forward`(2)         | `2 - 2 = 0` ✓ |
| `ortho`                 | 1       | 1                  | `ortho`(1)           | `2 - 1 = 1` ✓ |
| `forward`               | 2       | 2                  | `backward`(0)        | `2 - 0 = 2` ✓ |

三行全部吻合——`_swap_direction` 在字符串层完成了与 `_normalization` 在整数层「等效但相反」的翻转，使「逆内核 + 翻转 norm」数值上等价于「正内核」。

一句话总结：**`_swap_direction` 是「用错方向的内核去算」时付出的代价——因为 `hfftn` 借用了逆内核 `irfftn`，必须把 norm 方向也一起借反，才能让最终缩放正确。**（`ihfftn` 借用正内核 `rfftn`，同理。）

#### 4.3.3 源码精读

[_basic_backend.py:158-166](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic_backend.py#L158-L166) —— `_swap_direction` 的全部逻辑：`None`/`backward → forward`、`forward → backward`、`ortho` 不变、其余抛 `ValueError`。

它在两处被调用：

- [_basic_backend.py:177](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic_backend.py#L177) —— `hfftn` 的 xp 分支：`irfftn(x, s, axes, _swap_direction(norm), ...)`。
- [_basic_backend.py:192](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic_backend.py#L192) —— `ihfftn` 的 xp 分支：`xp.conj(rfftn(x, s, axes, _swap_direction(norm), ...))`。

注意 `_swap_direction` 只出现在 `hfftn`/`ihfftn`，**从不出现在 1-D 的 `hfft`/`ihfft`**。原因：1-D 的 [_basic_backend.py:101-110](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic_backend.py#L101-L110) 走的是 `_execute_1D`，而 `_execute_1D`（[_basic_backend.py:27-49](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic_backend.py#L27-L49)）在 numpy 路径直连 `_duccfft.hfft`（一等公民内核），在 xp 路径调 `xp.fft.hfft`（若存在）。两条路都不需要「借用反方向内核」，自然不需要翻 norm。N-D 的 `hfftn`/`ihfftn` 之所以特殊，是因为它们**绕开了 `_execute_nD`、自己手写了 xp 回退**，而回退目标恰好是反方向的 `irfftn`/`rfftn`。

#### 4.3.4 代码实践

**目标**：通过纯推理（源码阅读型实践）验证 `_swap_direction` 的正确性，再用一个数值实验佐证「翻转 + 共轭」确实等价。

第 1 步——手工推演。对照 §4.3.2 的表，自己填一遍：用户对 `hfft` 传 `norm="ortho"`，请推出 xp 路径最终传给 `irfftn` 的 norm 字符串，以及 `irfftn` 内部 `_normalization` 的整数返回值。

参考答案：`ortho → ortho`（不变），`_normalization('ortho', False) = 2 - 1 = 1`，与正变换 `hfft` 的 `_normalization('ortho', True) = 1` 一致。

第 2 步——数值佐证（示例代码）：

```python
# 示例代码
import numpy as np
import scipy.fft as sf

rng = np.random.default_rng(0)
# 造一个 Hermitian 对称的半谱（保证 hfft 输出实数）
m = 5
half = rng.standard_normal(m) + 1j * rng.standard_normal(m)
half[0] = half[0].real          # 0 频必为实
# 让它满足 Hermitian：把 m 视为 n//2+1，n=2*(m-1)=8
n = 2 * (m - 1)
full = np.concatenate([half, np.conj(half[-2:0:-1])])  # 长度 8 的 Hermitian 序列

for nm in ("backward", "ortho", "forward"):
    # hfft 自身（ducc 一等公民路径）
    a = sf.hfft(full, n=n, norm=nm)
    # 手工模拟 xp 路径：conj + irfft + 翻转 norm
    swap = {"backward": "forward", "forward": "backward", "ortho": "ortho", None: "forward"}[nm]
    b = sf.irfft(np.conj(full), n=n, norm=swap)
    print(f"norm={nm:8s} 一致:", np.allclose(a, b, atol=1e-10))
```

需要观察的现象：三种 norm 下，`sf.hfft(full, norm=nm)` 与「`conj` + `irfft` + 翻转 norm」的结果全部一致，从而验证 `_swap_direction` 在数值上的正确性。

预期结果：三行均打印 `True`。若本地未装 scipy 或版本不同，记为「待本地验证」，但推导部分（第 1 步）独立成立。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_swap_direction(None)` 返回 `'forward'` 而不是 `'backward'`？

**答案**：因为 `None` 与 `'backward'` 等价（见 `_NORM_MAP`，二者都映射到 `inorm=0`）。对「正变换 `hfft`」而言，`None`/`backward` 表示「不归一化」。但这条 norm 要交给逆内核 `irfftn`，必须翻成 `'forward'`（`inorm=2`），使 `irfftn` 的 `_normalization('forward', False) = 2 - 2 = 0`，最终仍是不归一化。所以 `_swap_direction` 把 `None` 归到 `backward` 一类一起翻成 `forward`。

**练习 2**：如果把 `_swap_direction(norm)` 这一句从 `hfftn` 里删掉，直接 `irfftn(..., norm, ...)`，哪种 `norm` 下结果依然正确？

**答案**：只有 `norm="ortho"` 依然正确，因为 `ortho` 翻转后还是 `ortho`（`2 - 1 = 1`），正逆方向缩放对称。`backward`/`forward` 会互换缩放归属，结果错。

---

## 5. 综合实践

把本讲三个模块串起来：用一个实信号走完 `rfft → irfft` 的半谱往返，再用一个 Hermitian 频谱走完 `hfft` 的实输出，最后验证 `hfft` 与「conj + irfft + 翻转 norm」的等价性。

```python
# 示例代码：本讲综合实践
import numpy as np
import scipy.fft as sf

# ---- 模块 1：实信号的 rfft/irfft 往返 ----
rng = np.random.default_rng(42)
x = rng.standard_normal(16)                 # 实信号
half = sf.rfft(x)                           # 半谱，长度 16//2+1 = 9
x_rec = sf.irfft(half, n=len(x))            # 必须显式给 n
assert np.allclose(x, x_rec), "rfft/irfft 往返失败"
print("rfft/irfft 往返 OK，半谱长度:", half.shape[0])   # 9

# ---- 模块 2：Hermitian 频谱的 hfft 实输出 ----
m = 6
spec = rng.standard_normal(m) + 1j * rng.standard_normal(m)
spec[0] = spec[0].real
n_out = 2 * (m - 1)                         # = 10
full_spec = np.concatenate([spec, np.conj(spec[-2:0:-1])])  # 长度 10，Hermitian
y = sf.hfft(full_spec, n=n_out)             # 实输出
assert np.isrealobj(y), "hfft 输出应为实数"
print("hfft 输出 dtype:", y.dtype, "长度:", y.shape[0])    # float64, 10

# ---- 模块 3：验证 _swap_direction 的等价性 ----
swap = lambda nm: {"backward": "forward", "forward": "backward",
                   "ortho": "ortho", None: "forward"}[nm]
for nm in ("backward", "ortho", "forward"):
    direct = sf.hfft(full_spec, n=n_out, norm=nm)
    via_swap = sf.irfft(np.conj(full_spec), n=n_out, norm=swap(nm))
    assert np.allclose(direct, via_swap, atol=1e-9), f"norm={nm} 不一致"
print("_swap_direction 等价性验证 OK")
```

预期结果：三段全部通过断言，依次打印 `9` / `float64 10` / `_swap_direction 等价性验证 OK`。若本地未安装 scipy，请记为「待本地验证」，但代码逻辑与源码一致。

> **思考延伸**：第 3 段里的 `np.conj(full_spec)` 对应 [_basic_backend.py:175-176](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic_backend.py#L175-L176) 的 `xp.conj`，`swap(nm)` 对应 [_basic_backend.py:177](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_basic_backend.py#L177) 的 `_swap_direction(norm)`。你实际上在应用层手动复刻了 scipy 内部对非 numpy 数组的 `hfftn` 回退实现。

## 6. 本讲小结

- `rfft` 利用实输入的 Hermitian 对称性，只输出非负频率的「半谱」（长度 `n//2+1`），`irfft` 凭半谱还原实信号，但必须由调用者指定输出长度 `n`。
- `hfft`/`ihfft` 是 `rfft`/`irfft` 的镜像：处理「时域 Hermitian、频域实」的情形；`hfft` 与 `irfft` 接受同形输入、产出同形输出，仅正/逆方向不同。
- 在 ducc/numpy 默认路径下，`hfft` 由 `c2r` 内核 `forward=True` 直接计算，是一等公民；`ihfft` 由 `r2c` `forward=False` 计算。
- `_swap_direction` 只在 xp（非 numpy）路径的 `hfftn`/`ihfftn` 中出现，用于补偿「借用反方向内核（`irfftn`/`rfftn`）」带来的归一化方向差，规则是 `backward↔forward`、`ortho` 与 `None→forward` 不破坏 ortho。
- `_swap_direction` 的本质与 `_normalization(norm, forward)` 里 `2 - inorm` 的整数翻转是同一件事的两面：前者翻字符串，后者翻整数，二者配合让「逆内核 + 翻转 norm」数值上等价于「正内核」。
- `complex_funcs` 集合（含 `'hfft'`、`'irfft'`）标记了在数组标准后端下需要复数输入兜底的函数。

## 7. 下一步学习建议

- 接着学 [u2-l3 多维变换](./u2-l3-multidimensional-fft.md)：本讲的 `hfftn`/`ihfftn` 已经涉及 `s`/`axes`，下一讲会系统讲 `fftn`/`rfftn` 的多轴控制与 `s` 的逐轴含义。
- 若对归一化的整数映射还感兴趣，可精读 [_duccfft/helper.py:181-192](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/fft/_duccfft/helper.py#L181-L192) 的 `_NORM_MAP` 与 `_normalization`，对照本讲的 `_swap_direction` 表加深理解。
- 进入进阶层后，[u4-l1 uarray 分派](./u4-l1-uarray-dispatch.md) 会解释 `Dispatchable` 的分派协议，届时你会更清楚为何公共函数体只写一句 `return (Dispatchable(x, np.ndarray),)`。
