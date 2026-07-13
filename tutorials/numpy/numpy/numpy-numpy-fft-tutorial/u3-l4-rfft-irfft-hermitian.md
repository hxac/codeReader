# rfft/irfft 实数变换与 Hermitian 对称

## 1. 本讲目标

本讲承接 [u3-l1](u3-l1-fft-ifft-raw-fft.md) 中打通的 `_raw_fft` 主流程，把视野从「复数输入的 `fft`/`ifft`」推进到「**实数输入**的 `rfft`/`irfft`」这一对专用变换。

学完后你应该能够：

- 说清楚**为什么**实输入的 DFT 只需要输出 `n//2+1` 个频率点（Hermitian 共轭对称）。
- 在源码层面追踪 `rfft` 从公开函数到统一入口 `_raw_fft` 的完整路径，并解释 `n_out = n//2+1` 是怎么算出来的。
- 解释 `irfft` 在不传 `n` 时为何默认 `n = (m-1)*2`（偶数长度），以及这带来的「奇偶长度歧义」。
- 理解 `_raw_fft` 如何根据 `n` 的奇偶在后端 `rfft_n_even` 与 `rfft_n_odd` 之间二选一。
- 用 `irfft(rfft(a), m)` 这一行完成 Fourier 插值/重采样。

## 2. 前置知识

本讲默认你已经读过以下内容（否则建议先看对应讲义）：

- **DFT/IDFT 的数学定义与 standard 频率排列**（[u2-l1](u2-l1-dft-math-conventions.md)）：正变换指数取负号、`A[0]` 是零频、`A[n//2]`（偶数长度时）是 Nyquist、后半段是负频率。
- **`_raw_fft` 统一入口**（[u3-l1](u3-l1-fft-ifft-raw-fft.md)）：所有一维变换都走它，用 `(is_real, is_forward)` 两个布尔选定后端 ufunc。
- **`norm` 三模式与 `fct`**（[u3-l2](u3-l2-norm-and-swap-direction.md)）：`rfft`/`irfft` 同样支持 `backward/ortho/forward`，本讲不再重复其计算，只在需要时引用。
- **`out` 参数与输出 dtype 决策**（[u3-l3](u3-l3-out-arg-and-dtype.md)）：`irfft` 输出实数、`rfft` 输出复数。

用三句话补两个本讲用到的术语：

- **Hermitian 对称（共轭对称）**：一个序列 \(X[k]\) 满足 \(X[-k] = \overline{X[k]}\)（\(\overline{\cdot}\) 表示复共轭），就说它具有 Hermitian 对称性。
- **Nyquist 频率**：采样率 \(f_s\) 下可表示的最高频率 \(f_s/2\)。在长度为 \(n\) 的 DFT 里，只有当 \(n\) 为偶数时，索引 \(n/2\) 这个位置才精确对应 Nyquist。

## 3. 本讲源码地图

本讲只涉及两个文件，但侧重其中和「实数变换」相关的片段：

| 文件 | 作用 | 本讲关注的行 |
| --- | --- | --- |
| [_pocketfft.py](_pocketfft.py) | 全部变换的 Python 主逻辑 | `rfft`、`irfft` 两个公开函数，以及统一入口 `_raw_fft` 里 `is_real` 为真的分支 |
| [_pocketfft.pyi](_pocketfft.pyi) | 类型存根，描述类型提升 | `rfft`/`irfft` 的 `@overload`，看出「实进→复出 / 复进→实出」 |

另外在「代码实践」里会引用测试文件 [tests/test_pocketfft.py](tests/test_pocketfft.py) 中的几条断言作为正确性参照。

一个总体结论先放在前面，后面逐条展开：**`rfft` 与 `irfft` 的 Python 外壳和 `fft`/`ifft` 一样薄**——只做 `asarray`、确定 `n`、把 `out` 原样下传，然后调用同一个 `_raw_fft`。二者与 `fft`/`ifft` 的唯一区别，是传给 `_raw_fft` 的 `is_real` 标志从 `False` 变成了 `True`。所有「实数变换特有」的行为，全都集中在 `_raw_fft` 对 `is_real == True` 这一分支的处理上。

## 4. 核心概念与源码讲解

### 4.1 实数输入的 Hermitian 对称：rfft 只输出一半的根据

#### 4.1.1 概念说明

`fft` 对任意（复数）输入都输出完整的 \(n\) 个频率点。但当输入 \(x[j]\) 是**纯实数**时，它的 DFT \(X[k]\) 天然满足一个约束——**Hermitian 共轭对称**：

\[
X[-k] \;=\; \overline{X[k]} \qquad (\text{也即 } X[n-k] = \overline{X[k]})
\]

直觉上：实信号里「正频率 \(k\)」和「负频率 \(-k\)」携带的信息完全一样，只是一个互为共轭。于是负频率那一半是**冗余**的——知道正频率半段，就能把负频率半段无损还原出来。

`rfft` 就是利用这一点：**它只计算并输出非负频率那一半**，把输出长度从 \(n\) 砍到 \(n//2+1\)。这正是 `rfft` 相对 `fft` 省一半内存与近一半算力的来源。

#### 4.1.2 核心流程

对长度 \(n\) 的实输入，standard 频率排列下 `rfft` 的输出布局是：

| `rfft` 输出索引 | 频率含义 | 是否一定为实数 |
| --- | --- | --- |
| `A[0]` | 零频（DC） | 是（直流分量为实） |
| `A[1] … A[n//2-1]` | 正频率 | 一般为复数 |
| `A[n//2]`（仅 \(n\) 偶） | Nyquist（正负 Nyquist 混叠） | 是 |
| `A[(n-1)//2]`（\(n\) 奇时的最后一项） | 最大正频率 | 一般为复数 |

可以验证，无论是奇还是偶，输出长度都等于 \(n//2+1\)：

- \(n\) 偶：\(n//2+1 = n/2+1\)，最后一项是实数 Nyquist。
- \(n\) 奇：\(n//2+1 = (n-1)/2+1 = (n+1)/2\)，没有 Nyquist 项，最后一项是最大正频率。

数学上的对称推导（用于后面的练习）：

\[
X[n-k] = \sum_{j=0}^{n-1} x[j]\,e^{-2\pi i\,j(n-k)/n}
       = \sum_{j=0}^{n-1} x[j]\,e^{-2\pi i\,j}\,e^{+2\pi i\,jk/n}
       = \sum_{j=0}^{n-1} x[j]\,e^{+2\pi i\,jk/n}
       = \overline{X[k]}
\]

其中用到 \(x[j]\in\mathbb{R}\)（故 \(\overline{x[j]}=x[j]\)）与 \(e^{-2\pi i\,j}=1\)。

#### 4.1.3 源码精读

这段结论性文字直接写在 `rfft` 的 docstring 的 Notes 里，是本讲最权威的「设计意图」出处：

[_pocketfft.py:381-399](_pocketfft.py#L381-L399) —— 说明实输入的输出是 Hermitian 对称的、负频率冗余、因此 `rfft` 不算负频率、输出长度为 `n//2+1`；并逐条说明 `A[0]` 是实数零频、偶长度时 `A[-1]` 是实数 Nyquist、奇长度时 `A[-1]` 是复数最大正频率。

Returns 一节则把输出长度用一个公式统一：

[_pocketfft.py:360-366](_pocketfft.py#L360-L366) —— 「`n` even → `(n/2)+1`；`n` odd → `(n+1)/2`」，两式合起来就是 `n//2+1`。

`rfft` 还有一个容易忽视的细节：**输入若带虚部会被静默丢弃**。这也写在同一段 Notes 的结尾（[_pocketfft.py:399](_pocketfft.py#L399)），因为 `rfft` 的语义就是「实输入变换」，虚部不在它的职责内。

#### 4.1.4 代码实践

**实践目标**：用 `fft` 与 `rfft` 同时算一个实信号的频谱，亲眼看到 `rfft` 输出恰好等于 `fft` 输出的前 `n//2+1` 项，并验证 Hermitian 对称。

**操作步骤**（示例代码）：

```python
import numpy as np

x = np.arange(8, dtype=float)          # 长度 8（偶）的实信号
F = np.fft.fft(x)                       # 完整 n=8 个频率点
R = np.fft.rfft(x)                      # 只输出非负频率

print(F.shape, R.shape)                 # 预期 (8,) (5,)  —— 5 = 8//2+1
print(np.allclose(R, F[:8 // 2 + 1]))   # 预期 True：rfft == fft 前 5 项

# Hermitian 对称：F[k] 与 F[-k] 互为共轭
print(np.allclose(F[1], np.conj(F[-1])))  # 预期 True
print(np.allclose(F[2], np.conj(F[-2])))  # 预期 True
```

**需要观察的现象**：

- `R.shape` 是 `(5,)` 而不是 `(8,)`——`rfft` 砍掉了一半冗余。
- `R` 与 `F[:5]` 完全一致——这是 [tests/test_pocketfft.py:249-264](tests/test_pocketfft.py#L249-L264) 中 `test_rfft` 反复断言的核心关系（`fft(x, n=n)[:(n//2+1)] == rfft(x, n=n)`）。
- `F[1]` 与 `F[-1]` 互为共轭——负频率确实是冗余的。

**预期结果**：三处 `allclose` 都为 `True`；`F.shape==(8,)`、`R.shape==(5,)`。

> 说明：以上数值结论由源码与测试断言推出，未在本地实跑；如需精确打印值，请本地运行确认（下同）。

#### 4.1.5 小练习与答案

**练习 1**：把上面的信号长度改成 9（奇数），`rfft(x)` 的输出长度是多少？它等于 `fft(x)` 的前几项？

**答案**：长度是 `9//2+1 = 5`。它等于 `fft(x)` 的前 5 项。这与 [tests/test_pocketfft.py:272-275](tests/test_pocketfft.py#L272-L275) 的 `test_rfft_odd` 一致（那里对长度 5 的奇信号断言 `rfft(x) == fft(x)[:3]`，`3 = 5//2+1`）。

**练习 2**：为什么 `A[0]`（零频）对实输入一定是实数？

**答案**：\(X[0] = \sum_j x[j]\)，是实数之和，自然是实数。这正是 docstring [_pocketfft.py:390-391](_pocketfft.py#L390-L391) 所说的「`A[0]` contains the zero-frequency term, which is real due to Hermitian symmetry」。

---

### 4.2 rfft 实现：n_out = n//2+1 与 rfft_n_even/rfft_n_odd 后端选择

#### 4.2.1 概念说明

上一模块讲了「为什么」要砍半；本模块讲「源码里**在哪一行**砍半、又是怎么挑后端的」。要点有三：

1. `rfft` 公开函数本身几乎不干活，它把 `is_real=True, is_forward=True` 传给 `_raw_fft`。
2. `_raw_fft` 在 `is_real and is_forward` 这个分支里，把输出长度从 `n` 改写成 `n_out = n // 2 + 1`。
3. 同一个分支还根据 `n % 2` 在两个**不同的** C++ 后端 ufunc（`rfft_n_even` / `rfft_n_odd`）之间二选一。

为什么要拆成两个后端？因为「由输出长度反推输入长度」对奇偶是**有歧义**的（见 4.3），偶数与奇数情况下的 Nyquist 处理方式不同，pocketfft 提供了两条独立代码路径，NumPy 用 `n % 2` 把它们分流。

#### 4.2.2 核心流程

`rfft` 的执行流程（伪代码）：

```text
rfft(a, n, axis, norm, out):
    a = asarray(a)
    if n is None: n = a.shape[axis]          # 默认沿用整轴
    return _raw_fft(a, n, axis,
                    is_real=True, is_forward=True,   # ← 与 fft 的唯一区别
                    norm=norm, out=out)
```

`_raw_fft` 中 `is_real == True` 分支的选路逻辑（伪代码）：

```text
n_out = n
if is_real:
    if is_forward:                            # —— rfft 走这里
        ufunc = rfft_n_even if (n % 2 == 0) else rfft_n_odd
        n_out = n // 2 + 1                    # ← 砍半就在这一行
    else:                                     # —— irfft 走这里（下一模块）
        ufunc = irfft
```

一张决策小表，把上一讲 [u3-l1](u3-l1-fft-ifft-raw-fft.md) 的「四象限」补齐到实数部分：

| `is_real` | `is_forward` | 选用后端 ufunc | 输出长度 `n_out` | 对应公开函数 |
| --- | --- | --- | --- | --- |
| `False` | `True`  | `pfu.fft`  | `n`       | `fft`  |
| `False` | `False` | `pfu.ifft` | `n`       | `ifft` |
| `True`  | `True`  | `pfu.rfft_n_even` / `pfu.rfft_n_odd` | `n//2+1` | `rfft` |
| `True`  | `False` | `pfu.irfft` | `n`       | `irfft` |

#### 4.2.3 源码精读

先看 `rfft` 公开函数的函数体，确认它真的只是个薄外壳：

[_pocketfft.py:414-418](_pocketfft.py#L414-L418) —— `a = asarray(a)`；`n is None` 时取 `a.shape[axis]`；最后 `_raw_fft(a, n, axis, True, True, norm, out=out)`。对比 [u3-l1](u3-l1-fft-ifft-raw-fft.md) 里 `fft` 的函数体，唯一的差别就是第 5、6 个参数从 `False, True` 变成了 `True, True`（`is_real` 由 `False` 改 `True`）。

真正决定输出长度和后端的是 `_raw_fft` 这 9 行（本模块的「主战场」）：

[_pocketfft.py:78-86](_pocketfft.py#L78-L86) —— 先令 `n_out = n`；进入 `if is_real:` 后，`if is_forward:` 分支里 `ufunc = pfu.rfft_n_even if n % 2 == 0 else pfu.rfft_n_odd`，并立刻 `n_out = n // 2 + 1`。`else:`（即 `irfft`）分支选 `pfu.irfft`，`n_out` 保持 `n`。最后的 `else`（非实）分支才是 `fft`/`ifft`。

`n_out` 一旦确定，就被用于两件事：

1. 分配输出数组的形状（把 `axis` 那一维替换成 `n_out`），见 [u3-l3](u3-l3-out-arg-and-dtype.md) 精读过的 [_pocketfft.py:90-99](_pocketfft.py#L90-L99)。
2. 校验用户传入的 `out` 形状是否匹配（`shape[axis] != n_out` 则抛 `ValueError`）。

类型存根 [_pocketfft.pyi:201-266](_pocketfft.pyi#L201-L266) 则从静态类型角度印证了「实进→复出复数」：`rfft` 的所有 `@overload` 都返回 `np.complexfloating`（如 `float64→complex128`、`float32→complex64`），且 `out` 被约束为 `NDArray[np.complexfloating]`——这正是 `n_out = n//2+1` 个复数频率箱。

> 注：`pfu` 是 [_pocketfft.py:48](_pocketfft.py#L48) 里 `from . import _pocketfft_umath as pfu` 引入的 C++ 扩展模块。`rfft_n_even`、`rfft_n_odd`、`irfft` 都是其中注册的广义 ufunc，它们的注册细节留到 [u5-l1](#)（gufunc 注册）与 [u5-l2](#)（循环实现）讲。

#### 4.2.4 代码实践

**实践目标**：验证 `_raw_fft` 选路与奇偶长度匹配——分别用偶、奇 `n` 调 `rfft`，确认输出长度公式，并用测试里的等价关系自检。

**操作步骤**：

```python
import numpy as np

# 偶长度 n=4：取自 test_rfft_even
x = np.arange(8, dtype=float)
y = np.fft.rfft(x, 4)
print(y.shape)                                  # 预期 (3,)  = 4//2+1
print(np.allclose(y, np.fft.fft(x[:4])[:3]))    # 预期 True

# 奇长度 n=5：取自 test_rfft_odd 的思路
x = np.array([1, 0, 2, 3, -3], dtype=float)
y = np.fft.rfft(x)
print(y.shape)                                  # 预期 (3,)  = 5//2+1
print(np.allclose(y, np.fft.fft(x)[:3]))        # 预期 True
```

**需要观察的现象**：偶、奇两种 `n` 下，输出长度都恰好等于 `n//2+1`；且都等于「先截断/补零到 `n` 再做 `fft`，取前 `n//2+1` 项」。这与 [tests/test_pocketfft.py:266-275](tests/test_pocketfft.py#L266-L275) 中 `test_rfft_even` / `test_rfft_odd` 的断言一致。

**预期结果**：四次 `print` 依次为 `(3,)`、`True`、`(3,)`、`True`。

#### 4.2.5 小练习与答案

**练习 1**：`_raw_fft` 里为什么是 `n_out = n // 2 + 1` 而不是 `n // 2`？少的那一个点对应什么？

**答案**：因为输出要把 `A[0]`（零频）和 `A[n//2]`（偶数时的 Nyquist）/ `A[(n-1)//2]`（奇数时的最大正频）都算上，正频率段是 `A[1..n//2]` 共 `n//2` 个，再加 `A[0]` 共 `n//2+1` 个。`n//2` 会漏掉零频。

**练习 2**：如果把 `rfft` 调用的 `is_real` 误改成 `False`（即当成 `fft` 走），输出会变成什么？

**答案**：会走 `pfu.fft`、`n_out = n`，输出长度变回 `n` 个复数点，相当于普通的 `fft`——「砍半」和「`rfft_n_even/rfft_n_odd` 选路」都发生在 `is_real == True` 这个 `if` 里，一旦不进入就全部失效。

---

### 4.3 irfft 实现：默认 n=(m-1)*2 与奇偶长度歧义

#### 4.3.1 概念说明

`irfft` 是 `rfft` 的逆：输入是 `n//2+1` 个非负频率点（含 Hermitian 对称的前半段），输出一个**实数**信号。

这里藏着一个 `rfft` 没有的麻烦——**奇偶长度歧义**。给定一段长 \(m\) 的「半谱」（`rfft` 的输出），它可能来自两种不同的原始信号：

- 原始长度 \(n = 2(m-1)\)（**偶**）：最后一项 `A[m-1]` 是实数 Nyquist。
- 原始长度 \(n = 2m-1\)（**奇**）：没有 Nyquist，最后一项 `A[m-1]` 是复数最大正频率。

这两种原始长度做 `rfft` 都得到长 \(m\) 的输出，所以**光看半谱长度 \(m\)，无法判断原始信号是奇还是偶**。`irfft` 必须靠用户给的 `n` 来消歧。

`irfft` 的默认选择是**假定偶数长度**：`n = (m-1)*2`。这一假定把最后一项当成实数 Nyquist 处理。后果是：**如果原始信号其实是奇数长度，默认调用会丢信息、还原不出来**。docstring 因此强调 `irfft(rfft(a), len(a)) == a` 里那个 `len(a)` 是必需的（[_pocketfft.py:428-429](_pocketfft.py#L428-L429)）。

#### 4.3.2 核心流程

`irfft` 的执行流程（伪代码）：

```text
irfft(a, n, axis, norm, out):
    a = asarray(a)
    if n is None:
        n = (a.shape[axis] - 1) * 2          # ← 默认偶数长度！(m-1)*2
    return _raw_fft(a, n, axis,
                    is_real=True, is_forward=False,   # 反向、实输出
                    norm=norm, out=out)
```

`_raw_fft` 在 `is_real and (not is_forward)` 分支（即 `irfft`）里：

```text
n_out = n                                    # irfft 输出长度就是 n
ufunc = pfu.irfft
# 输出 dtype 用 real_dtype（实数），见 _raw_fft 的 out 分配
```

把歧义用一张表说清楚（设半谱长 \(m\)）：

| 真实原始长度 | `rfft` 输出长 \(m\) | 默认 `irfft(rfft(a))` 推断的 \(n\) | 能否还原？ |
| --- | --- | --- | --- |
| \(2(m-1)\)（偶） | \(m\) | \((m-1)\cdot2\) ✅ 一致 | 能 |
| \(2m-1\)（奇） | \(m\) | \((m-1)\cdot2\) ❌ 错为偶 | 不能（丢 Nyquist/最大正频的虚部信息） |

#### 4.3.3 源码精读

`irfft` 的函数体，注意默认 `n` 的算法与 `fft`/`rfft` 都不同：

[_pocketfft.py:522-526](_pocketfft.py#L522-L526) —— `a = asarray(a)`；`if n is None: n = (a.shape[axis] - 1) * 2`；最后 `_raw_fft(a, n, axis, True, False, norm, out=out)`。对比 `rfft` 的 `n = a.shape[axis]`，这里改成 `(a.shape[axis] - 1) * 2`，即「半谱长 \(m\) 减 1 再乘 2」。

默认 \(n\) 的文字表述在参数说明里：

[_pocketfft.py:441-447](_pocketfft.py#L441-L447) —— 「For `n` output points, `n//2+1` input points are necessary. … If `n` is not given, it is taken to be `2*(m-1)` where `m` is the length of the input along the axis specified by `axis`.」

歧义的来源与默认假定的后果，docstring 用一整段 Notes 反复强调：

[_pocketfft.py:488-506](_pocketfft.py#L488-L506) —— 关键三句：(1) `n` 是结果的长度、不是输入的长度；(2) 「each input shape could correspond to either an odd or even length signal」（每个输入形状都可能对应奇或偶长度信号）；(3) 「By default, `irfft` assumes an even output length which puts the last entry at the Nyquist frequency … To avoid losing information, the correct length of the real input **must** be given.」

`_raw_fft` 里 `irfft` 分支与输出 dtype 的落点（与 [u3-l3](u3-l3-out-arg-and-dtype.md) 衔接）：

[_pocketfft.py:78-86](_pocketfft.py#L78-L86) —— `is_real` 为真、`is_forward` 为假时选 `ufunc = pfu.irfft`，`n_out = n`。

[_pocketfft.py:90-96](_pocketfft.py#L90-L96) —— `out is None` 时，`if is_real and not is_forward:`（即 `irfft`）用 `out_dtype = real_dtype` 输出**实数**；其余变换用 `result_type(a.dtype, 1j)` 输出复数。这就是「复进→实出」的来源。

类型存根 [_pocketfft.pyi:269-276](_pocketfft.pyi#L269-L276) 印证：`irfft` 的第一个 `@overload` 接 `np.floating` 返回 `np.floating`，且若干 overload 把 `complex128/integer/bool` 输入映射到 `float64` 输出——「复进实出」。

一个真实回归测试说明 `n` 的边界行为是受保护、不会崩的：

[tests/test_pocketfft.py:578-583](tests/test_pocketfft.py#L578-L583) —— `test_irfft_with_n_1_regression`（gh-25661）：`irfft(x, n=1)` 与 `irfft(np.array([0], complex), n=10)` 都不应报错。这说明即便 `n` 取到极端的 1，`_raw_fft` 里 `n < 1` 的守卫（[_pocketfft.py:59-60](_pocketfft.py#L59-L60)）也能正确放行 `n=1`。

#### 4.3.4 代码实践

**实践目标**：用一段**奇数长度**的实信号，亲眼看到默认 `irfft(rfft(a))` 还原失败、而显式 `irfft(rfft(a), n=len(a))` 还原成功。这正是本讲规格里要求的核心实验。

**操作步骤**：

```python
import numpy as np

a = np.arange(9, dtype=float)            # 长度 9（奇）的实信号
spec = np.fft.rfft(a)
print(spec.shape)                        # 预期 (5,)  = 9//2+1

# 情形 A：默认 n —— irfft 推断 n = (5-1)*2 = 8（偶），还原到长度 8
rec_default = np.fft.irfft(spec)
print(rec_default.shape)                 # 预期 (8,)  —— 已经不是 9 了！

# 情形 B：显式 n=9 —— 正确还原奇长度
rec_explicit = np.fft.irfft(spec, n=9)
print(rec_explicit.shape)                # 预期 (9,)
print(np.allclose(rec_explicit, a))      # 预期 True
```

**需要观察的现象**：

- `spec.shape == (5,)`：奇长度 9 的半谱长是 5。
- 默认 `irfft` 输出长度是 **8**（`(5-1)*2`），不是 9——它默认了偶数长度，于是「猜错了奇偶」。
- 默认还原结果与 `a` 长度都不一样，自然谈不上还原；显式给 `n=9` 才能精确还原。

**预期结果**：四次 `print` 依次为 `(5,)`、`(8,)`、`(9,)`、`True`。

> 对照：若把 `a` 改成长度 8（偶），默认 `irfft(rfft(a))` 推断 `n=(5-1)*2=8` 恰好等于原长，能还原。这正是 [tests/test_pocketfft.py:277-285](tests/test_pocketfft.py#L277-L285) 的 `test_irfft` 敢用 `assert_allclose(x, irfft(rfft(x)))` 而不传 `n` 的原因——它用的 `x = random(30)` 是**偶数**长度。

#### 4.3.5 小练习与答案

**练习 1**：`irfft` 的默认 `n` 为什么写成 `(a.shape[axis] - 1) * 2` 而不是 `a.shape[axis] * 2`？

**答案**：因为半谱长 \(m\) 与原始偶数长度 \(n\) 的关系是 \(m = n//2+1\)，即 \(n = 2(m-1)\)。代码里 `a.shape[axis]` 就是半谱长 \(m\)，故默认 \(n = (m-1)\cdot 2\)。若写成 `m*2` 就多算了 2。

**练习 2**：为什么 docstring 说「To get an odd number of output points, `n` must be specified」（[_pocketfft.py:472-473](_pocketfft.py#L472-L473)）？

**答案**：默认 \(n=(m-1)\cdot 2\) 必是偶数（2 的倍数），所以不传 `n` 永远得不到奇数长度输出。想要奇数长度（如还原一段奇长度原信号），必须显式传 `n`。

**练习 3**：半谱 `[1, -1j, -1]`（长 \(m=3\)）默认 `irfft` 会输出多长的实信号？它对应原始偶数长度是多少？

**答案**：默认 \(n=(3-1)\cdot 2=4\)，输出长度 4。这正是 docstring 例子 [_pocketfft.py:508-514](_pocketfft.py#L508-L514) 中 `irfft([1, -1j, -1])` 得到 `array([0., 1., 0., 0.])`（长度 4）的情形。

---

### 4.4 Fourier 插值与重采样：irfft(rfft(a), m)

#### 4.4.1 概念说明

`irfft` 的 `n` 不仅能「还原」原信号，还能**改变长度**——这就是 Fourier 插值（也叫频域重采样）。做法只有一行：

```python
a_resamp = irfft(rfft(a), m)
```

原理很直观：`rfft(a)` 拿到信号的频谱；`irfft(spec, m)` 从这个频谱重建一个**长 \(m\)** 的实信号。

- \(m > \mathrm{len}(a)\)：在频域「补零」（高频补零），相当于带限插值——得到更密采样的平滑曲线。
- \(m < \mathrm{len}(a)\)：在频域「截断」（砍掉高频），相当于低通滤波后降采样。

注意 docstring 的提醒（[_pocketfft.py:495-498](_pocketfft.py#L495-L498)）：当 `n`（即这里的 \(m\)）要求补零或截断时，增删的值都发生在**高频端**，这正是「带限插值」的数学含义。

#### 4.4.2 核心流程

```text
给定原信号 a（长 L），重采样到 m 点：
1. spec = rfft(a)              # 得到 n//2+1 个频率点
2. a_resamp = irfft(spec, m)   # 指定输出长 m 重建实信号
```

需要注意的「坑」：因为 `irfft` 默认把半谱最后一项当 Nyquist（实数），如果原信号是**奇数长度**，直接 `irfft(rfft(a), m)` 也会丢掉最大正频率的虚部。严谨做法是 `irfft(rfft(a, n=L), m)`——在 `rfft` 这一步也显式钉死原始长度，避免奇偶歧义在频谱生成端就引入误差。

#### 4.4.3 源码精读

这条用法的权威出处就是 `irfft` docstring 的 Notes：

[_pocketfft.py:495-498](_pocketfft.py#L495-L498) —— 「If you specify an `n` such that `a` must be zero-padded or truncated, the extra/removed values will be added/removed at high frequencies. One can thus resample a series to `m` points via Fourier interpolation by: `a_resamp = irfft(rfft(a), m)`.」

「补零/截断发生在高频端」这一点，对应到 `_raw_fft` 的机制：`irfft` 分支里 `n_out = n`（[_pocketfft.py:78-84](_pocketfft.py#L78-L84)），C++ 后端 `pfu.irfft` 会把输入半谱与目标长度 \(m\) 一同比对——半谱长不足 \(m//2+1\) 则高频补零、超出则高频截断。

顺带一提，`hfft` 内部就是用 `irfft(conjugate(a), n, ...)` 实现的（[_pocketfft.py:624-629](_pocketfft.py#L624-L629)），所以本模块讲的 `irfft` 长度/奇偶行为，同样适用于 `hfft`。

#### 4.4.4 代码实践

**实践目标**：把一段带限信号从长度 8 重采样到 16（加密）和 4（降采样），观察插值/截断效果。

**操作步骤**：

```python
import numpy as np

# 一个低频的带限信号（一个周期的正弦，8 个采样点）
a = np.sin(2 * np.pi * np.arange(8) / 8)

up   = np.fft.irfft(np.fft.rfft(a), 16)   # 升采样到 16 点
down = np.fft.irfft(np.fft.rfft(a), 4)    # 降采样到 4 点

print(up.shape, down.shape)                # 预期 (16,) (4,)
# 升采样应得到更密的同形正弦；降采样得到 4 点
print(np.allclose(up[::2], a))             # 预期接近 True（奇偶对齐时）
```

**需要观察的现象**：

- `up` 有 16 个点、`down` 有 4 个点，长度按 `m` 改变。
- 由于原信号是单一低频正弦、频谱能量集中在前几个频率箱，升采样几乎「无损」地补出了中间点，曲线仍是平滑正弦；降采样则丢了高频信息但保留了主波形。

**预期结果**：`up.shape==(16,)`、`down.shape==(4,)`；`up[::2]` 与 `a` 接近（数值精度内可能不完全相等，建议本地验证具体误差量级——待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：为什么用 `irfft(rfft(a), m)` 做 Fourier 插值，比「直接线性插值」更适合**带限信号**？

**答案**：带限信号的所有信息都在其有限频谱里。Fourier 插值在频域只做高频补零/截断，不引入原信号频带之外的新频率成分，理论上对带限信号是无失真的；线性插值会改变频谱形状、引入频带外的失真。

**练习 2**：若原信号长度是 9（奇），直接 `irfft(rfft(a), 16)` 会有什么隐患？如何写更稳妥？

**答案**：隐患是 `rfft(a)` 得到的半谱最后一项本应是「最大正频率」（复数），但下游 `irfft(..., 16)` 默认按偶数长度处理、把它当 Nyquist（实数），丢了虚部信息。更稳妥的写法是显式钉死原始长度：`irfft(rfft(a, n=9), 16)`，让频谱端就按奇长度正确生成。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个小任务：**用 `rfft` 观察一段实信号的频谱结构，用 `irfft` 在「默认 vs 显式 `n`」两种方式下还原，最后用 Fourier 插值把它重采样到任意点数。**

```python
import numpy as np

# 1) 构造一段奇数长度的实信号（两个正弦叠加 + 直流）
n = 9
t = np.arange(n)
a = 1.0 + 0.7 * np.sin(2 * np.pi * t / n) + 0.3 * np.sin(2 * np.pi * 2 * t / n)

# 2) 频谱分析
spec = np.fft.rfft(a)
freq = np.fft.rfftfreq(n)                 # 与 rfft 对齐的非负频率轴（见 u2-l2）
print("半谱长 =", spec.shape[0], " 预期 n//2+1 =", n // 2 + 1)
print("零频 spec[0] 是否为实数？", np.isreal(spec[0]))

# 3) 还原：默认 n（会错为偶长度） vs 显式 n
rec_bad  = np.fft.irfft(spec)             # 默认 n=(5-1)*2=8
rec_good = np.fft.irfft(spec, n=n)        # 显式 n=9
print("默认还原长度 =", rec_bad.shape[0], " 显式还原长度 =", rec_good.shape[0])
print("显式还原误差 =", np.max(np.abs(rec_good - a)))

# 4) Fourier 重采样到 32 点（注意显式钉死原始长度 n=9，避免奇偶歧义）
a_up = np.fft.irfft(np.fft.rfft(a, n=n), 32)
print("重采样长度 =", a_up.shape[0])
```

**自检清单**：

1. `spec.shape[0]` 是否等于 `n//2+1 = 5`？（对应 4.2）
2. `spec[0]` 是否为实数？（对应 4.1 的 Hermitian 结论）
3. 默认还原长度是否被错当成 8？显式 `n=9` 是否近似还原 `a`？（对应 4.3）
4. 重采样是否得到 32 个点？（对应 4.4）

把每一步的输出与「预期」对照，若全部吻合，说明你已掌握 `rfft`/`irfft` 的长度规则、奇偶歧义与 Fourier 插值。

## 6. 本讲小结

- **Hermitian 对称是 `rfft` 砍半的根据**：实输入的 DFT 满足 \(X[-k]=\overline{X[k]}\)，负频率冗余，`rfft` 只输出非负频率。
- **输出长度统一为 `n//2+1`**：这一改写发生在 [_pocketfft.py:78-86](_pocketfft.py#L78-L86) 的 `is_real and is_forward` 分支（`n_out = n // 2 + 1`），无论 `n` 奇偶都成立。
- **后端二选一**：同一分支用 `n % 2` 在 `pfu.rfft_n_even` 与 `pfu.rfft_n_odd` 之间分流，因为奇偶情况下 Nyquist 处理不同。
- **`rfft`/`irfft` 的外壳极薄**：与 `fft`/`ifft` 唯一的差别是传给 `_raw_fft` 的 `is_real=True`；所有实数特有行为都在 `_raw_fft` 内部。
- **`irfft` 默认偶数长度 `n=(m-1)*2`**（[_pocketfft.py:522-526](_pocketfft.py#L522-L526)），由此产生奇偶长度歧义——还原奇长度信号**必须**显式传 `n`。
- **Fourier 插值一行搞定**：`irfft(rfft(a), m)`，增删发生在高频端；稳妥写法是 `irfft(rfft(a, n=len(a)), m)`。

## 7. 下一步学习建议

- 想看「半谱的频率轴怎么来的」：回顾 [u2-l2](u2-l2-fftfreq-rfftfreq.md) 的 `rfftfreq`，它生成的非负频率轴与 `rfft` 输出逐位置对齐。
- 想把 `rfft`/`irfft` 推广到多维：进入 [u4-l1](u4-l1-fftn-fft2-multidim.md)（fftn/fft2）与 [u4-l3](u4-l3-rfftn-irfftn-multidim.md)（rfftn/irfftn），那里会看到「末轴做 rfft、其余轴做 fft」的组合，以及 `_cook_nd_args(invreal=1)` 如何处理末轴奇偶。
- 想理解「时域 Hermitian、频域实」的对偶情形：看 [u4-l4](u4-l4-hfft-ihfft-hermitian.md) 的 `hfft`/`ihfft`，它们正是复用本讲的 `irfft`/`rfft` 加一次共轭实现的。
- 想深入 `rfft_n_even`/`rfft_n_odd`/`irfft` 这三个 C++ gufunc 怎么注册、怎么循环：进入第 5 单元，尤其是 [u5-l1](u5-l1-gufunc-registration.md)（gufunc 注册）与 [u5-l2](u5-l2-loops-vectorization-fftpack.md)（循环实现与 FFTpack 排序）。
- 想看更多正确性/regression 测试：浏览 [tests/test_pocketfft.py](tests/test_pocketfft.py) 中的 `test_rfft`、`test_rfft_even`、`test_rfft_odd`、`test_irfft`、`test_irfft_with_n_1_regression`，它们正是本讲各处引用的断言来源。
