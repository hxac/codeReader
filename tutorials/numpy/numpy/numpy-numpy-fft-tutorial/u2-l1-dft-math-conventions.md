# DFT 数学定义与实现约定

## 1. 本讲目标

上一篇（u1-l1）你已经把 `numpy.fft` 跑了起来，并且知道 `fft`/`ifft` 在 Python 层只是一层薄壳，真正的计算落在 C++ 扩展 `_pocketfft_umath`。本讲我们要回答一个更基础、也更关键的问题：

> **这套实现到底按哪一套数学约定来算？**

不同教材、不同软件库对「离散傅里叶变换（DFT）」的定义并不统一——指数上取正号还是负号？正变换归一化还是反变换归一化？输出数组里频率是怎么排的？这些「约定」一旦搞错，结果就会差一个符号、一个常数因子，或者频率对不上号。

读完本讲，你应当能够：

- 写出 `numpy.fft` 采用的 **DFT / IDFT 公式**，并指出正变换指数取负号、反变换取正号、默认在反变换带 \(\frac{1}{n}\) 归一化；
- 看懂 `fft` 输出数组采用的 **"standard" 频率排列**：`A[0]` 是零频、`A[1:n/2]` 是正频率、`A[n/2]`（偶数时）是 Nyquist、`A[n/2+1:]` 是负频率；
- 知道 **实输入下频谱的 Hermitian 共轭对称性**，并能用 `np.abs` / `np.angle` 得到幅度谱与相位谱；
- 理解 `numpy.fft` 的 **Type Promotion（类型提升）**：`float32`→`float64`、`complex64`→`complex128`，并知道它与 `scipy.fftpack` 的区别。

本讲是后续所有讲义的「数学地基」——后面讲 `norm`、`rfft`、多维变换时，都会反复回到这里的公式与排列约定。

---

## 2. 前置知识

本讲默认你已经读过 u1-l1，知道：

- `np.fft.fft(a)` 把时域信号 `a` 变到频域，`np.fft.ifft(A)` 把它变回来；
- `ifft(fft(a)) ≈ a`（这一点本讲会从公式上证明 *为什么* 成立）。

此外，请先接受下面几个最基本的概念（不需要会推导，有直觉即可）：

1. **复数与指数**。我们会用到复数单位 \(i\)（满足 \(i^2=-1\)），以及欧拉公式 \(\exp(i\theta)=\cos\theta+i\sin\theta\)。它把「一个复数」和「一个带相位的振荡」对应起来：复数的**模** \(\lvert\cdot\rvert\) 代表振幅，**幅角** \(\angle\) 代表相位。
2. **频率**。信号里「每秒振荡多少次」叫频率。采样得到的一串点，其 DFT 会告诉你这串点里「含有哪些频率成分、各有多强」。
3. **采样与 Nyquist**。若采样间隔为 \(\Delta t\)，则采样率为 \(1/\Delta t\)，能分辨的最高频率是采样率的一半 \(1/(2\Delta t)\)，这个极限频率叫 **Nyquist 频率**。它在本讲的频率排列里占据一个特殊位置。

术语对照表：

| 术语 | 含义 |
|------|------|
| DFT | 离散傅里叶变换，一个明确的数学公式 |
| IDFT | DFT 的反变换 |
| DC 分量 | 零频分量，即 `A[0]`，代表信号的直流（恒定）偏置，等于信号之和 |
| Nyquist | 采样率一半处的频率，偶数长度时单独占据一个输出位 |
| Hermitian 对称 | 实信号的频谱满足 \(A[-k]=\overline{A[k]}\)（共轭对称） |
| 幅度谱 / 相位谱 | 频谱的模 \(\lvert A\rvert\) / 幅角 \(\angle A\) |

---

## 3. 本讲源码地图

本讲的「权威定义」几乎全部写在 `__init__.py` 的 docstring 里——这是一段长达两百行、写得非常用心的教学文档。而 `out` 数组的 `dtype` 决策、归一化系数的计算，则落在 `_pocketfft.py` 的 `_raw_fft`。

| 文件 | 作用 | 本讲是否精读 |
|------|------|--------------|
| [`__init__.py`](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/__init__.py) | 子包入口。其 docstring 的 "Implementation details"、"Type Promotion"、"Normalization"、"Real and Hermitian transforms" 几节给出了全部数学约定 | ✅ 精读 |
| [`_pocketfft.py`](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft.py) | 变换主逻辑。本讲只看其中 `_raw_fft` 的归一化系数 `fct` 与输出 `dtype` 决策两处 | ✅ 精读（局部） |

阅读策略：**先用 docstring 建立数学直觉，再到 `_raw_fft` 看这些约定是如何落到代码上的。**

---

## 4. 核心概念与源码讲解

### 4.1 DFT / IDFT 公式约定（指数符号与 1/n 归一化）

#### 4.1.1 概念说明

「定义 DFT 的方式有很多种，差别在于指数的符号、归一化等等。」这是 `__init__.py` docstring 第一句就强调的。因此，谈 DFT 必须**先约定、再计算**。

`numpy.fft` 采用的约定可以一句话概括：

> **正变换（`fft`）指数取负号、不归一化；反变换（`ifft`）指数取正号、默认乘以 \(\frac{1}{n}\)。**

这个「正变换不归一化、反变换带 \(\frac{1}{n}\)」的默认模式有个名字，叫 **"backward"**（也是 `norm` 参数的默认值）。后面会讲 `norm` 还可以取 `"ortho"`、`"forward"`，它们只是改了归一化系数，公式骨架不变。

#### 4.1.2 核心流程

设输入为长度 \(n\) 的序列 \(a_m\)（\(m=0,\ldots,n-1\)），输出为同样长度的序列 \(A_k\)（\(k=0,\ldots,n-1\)）。在默认 `norm="backward"` 下：

**正变换（`fft`）：**

\[
A_k \;=\; \sum_{m=0}^{n-1} a_m\,\exp\!\left\{-2\pi i\,\frac{mk}{n}\right\},\qquad k=0,\ldots,n-1.
\]

**反变换（`ifft`）：**

\[
a_m \;=\; \frac{1}{n}\sum_{k=0}^{n-1} A_k\,\exp\!\left\{+2\pi i\,\frac{mk}{n}\right\},\qquad m=0,\ldots,n-1.
\]

为什么 `ifft(fft(a)) ≈ a` 成立？把正变换代入反变换：

\[
\frac{1}{n}\sum_{k=0}^{n-1}\!\left[\sum_{m'=0}^{n-1} a_{m'} e^{-2\pi i\,m'k/n}\right] e^{+2\pi i\,mk/n}
= \sum_{m'} a_{m'}\underbrace{\left(\frac{1}{n}\sum_{k} e^{2\pi i\,(m-m')k/n}\right)}_{\text{等于 } \delta_{m,m'}}
= a_m.
\]

中间那一步用到的是**复指数的正交性**：\(\frac{1}{n}\sum_{k=0}^{n-1} e^{2\pi i\,(m-m')k/n}\) 在 \(m=m'\) 时等于 1，否则等于 0。这就是「正负号配对 + \(\frac{1}{n}\) 归一化」能精确还原信号的数学根源。

把上述约定与三种 `norm` 模式列成表（本讲只需先记住默认的 "backward"）：

| `norm` | 正变换 `fft` 缩放 | 反变换 `ifft` 缩放 |
|--------|------------------|-------------------|
| `"backward"`（默认，`None` 同义） | 不缩放（\(1\)） | \(\frac{1}{n}\) |
| `"ortho"` | \(\frac{1}{\sqrt{n}}\) | \(\frac{1}{\sqrt{n}}\) |
| `"forward"` | \(\frac{1}{n}\) | 不缩放（\(1\)） |

#### 4.1.3 源码精读

公式的「权威出处」是 docstring 的 "Implementation details" 一节：

[\_\_init\_\_.py#L82-L89](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/__init__.py#L82-L89) — 开宗明义：「定义 DFT 的方式有很多种……本实现把 DFT 定义为」，紧接着给出正变换公式（对应上面 §4.1.2 的第一条式子）。注意指数里是 \(-2\pi i\,\frac{mk}{n}\)，**负号**。

[\_\_init\_\_.py#L115-L122](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/__init__.py#L115-L122) — 反变换 IDFT 的定义，指数变为 \(+2\pi i\)，且前面多了 \(\frac{1}{n}\)。最后一句（L121-L122）点明它与正变换的「两处不同」：指数符号相反、默认多了 \(\frac{1}{n}\) 归一化。

[\_\_init\_\_.py#L131-L144](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/__init__.py#L131-L144) — "Normalization" 一节，把三种 `norm` 的缩放规则讲清，并明确 `None` 是 `"backward"` 的别名（为了向后兼容）。

那么这套公式在代码里是怎么「落」的？关键在 `_raw_fft` 里的归一化系数 `fct`：

[_pocketfft.py#L54-L76](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft.py#L54-L76) — 这是 `_raw_fft` 的开头。注意上方 L54-L57 的一段注释：归一化系数这里叫 `inv_norm`（对结果做「除以它」的缩放），之所以不用更直观的 `fct`（除以 `n`），是为了**避免在零长度轴上出现除以零**。随后：

```python
real_dtype = result_type(a.real.dtype, 1.0)
if norm is None or norm == "backward":
    fct = 1
elif norm == "ortho":
    fct = reciprocal(sqrt(n, dtype=real_dtype))
elif norm == "forward":
    fct = reciprocal(n, dtype=real_dtype)
```

对照 §4.1.2 的表格：`backward` 时 `fct=1`（正变换不缩放）、`forward` 时 `fct=1/n`、`ortho` 时 `fct=1/√n`。这里的 `fct` 就是对应表格里**正变换**的缩放因子——但 `_raw_fft` 既要服务正变换也要服务反变换，怎么区分？答案在下面这段：

[_pocketfft.py#L64-L65](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft.py#L64-L65) — 进入函数后，如果 `not is_forward`（即反变换），先调用 `_swap_direction(norm)` 把 `norm` 翻一面。配合下面的映射表：

[_pocketfft.py#L104-L113](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft.py#L104-L113) — `_SWAP_DIRECTION_MAP` 把 `"backward"↔"forward"` 互换、`"ortho"` 保持不变。这样，反变换 `ifft(norm="backward")` 在算 `fct` 时，会先被换成 `"forward"`，于是 `fct=1/n`——正好对应表格里「反变换 backward 缩放 \(\frac{1}{n}\)」。

> 小结：**公式里的归一化，不是分散写在 `fft`/`ifft` 两个函数里，而是统一由 `_raw_fft` 的 `fct` 加上 `_swap_direction` 实现。** 这是后续 u3-l2 会专门展开的设计，本讲只要知道它「对应了那张表格」即可。

#### 4.1.4 代码实践

**实践目标**：从数值上验证「正负号配对 + \(1/n\)」确实让 `ifft(fft(a)) ≈ a`，并亲手感受 `norm` 改变带来的常数因子差异。

**操作步骤**：

```python
import numpy as np

rng = np.random.default_rng(0)
a = rng.standard_normal(8) + 1j * rng.standard_normal(8)   # 复信号，长度 8

# 1) 默认 backward：ifft(fft(a)) 应还原 a
roundtrip = np.fft.ifft(np.fft.fft(a))
print("max |ifft(fft(a)) - a|  =", np.max(np.abs(roundtrip - a)))

# 2) 观察三种 norm 下的正变换幅度差异
A_back  = np.fft.fft(a, norm="backward")
A_ortho = np.fft.fft(a, norm="ortho")
A_fwd   = np.fft.fft(a, norm="forward")

print("|A_ortho| / |A_back|    =", np.abs(A_ortho[1]) / np.abs(A_back[1]))   # 期望 ≈ 1/√8
print("|A_fwd|   / |A_back|    =", np.abs(A_fwd[1])   / np.abs(A_back[1]))   # 期望 ≈ 1/8

# 3) ortho 模式下正反都带 1/√n，应仍互逆
print("max |ifft(fft(a,ortho),ortho) - a| =",
      np.max(np.abs(np.fft.ifft(np.fft.fft(a, norm="ortho"), norm="ortho")) - a))
```

**需要观察的现象**：

1. 第 1 步的误差应是机器精度量级（约 \(10^{-15}\)），说明正反变换互逆；
2. 第 2 步两个比值应分别接近 \(1/\sqrt{8}\approx 0.3536\) 与 \(1/8=0.125\)，与表格一致；
3. 第 3 步误差同样是机器精度量级——`ortho` 两边对称缩放，依然互逆。

**预期结果**：三个数值都符合上述描述。若你在不同机器上运行，浮点误差的末位可能略有差异，这是正常的。

> 说明：以上代码需要本地安装 NumPy 才能运行；若当前环境无法执行，请将其复制到本地 Python 解释器中验证（本讲后续实践同此说明）。

#### 4.1.5 小练习与答案

**练习 1**：若把正变换的指数从 \(-2\pi i\) 改成 \(+2\pi i\)（其它不变），`ifft(fft(a))` 还原信号吗？

**答案**：依然能还原。因为「互逆」只要求正、反两个变换的指数符号**相反**且配好 \(1/n\)，并不规定哪个取正、哪个取负。改成 \(+2\pi i\) 后，它实际上就变成了本实现的 *反变换*；只要对应的「反变换」也跟着翻号，整套仍是互逆的。`numpy.fft` 只是约定了正变换取负号这一种风格。

**练习 2**：为什么 `_raw_fft` 里算 `fct` 要用 `reciprocal(sqrt(n, ...))` 而不是直接写 `1/np.sqrt(n)`？

**答案**：源码 L54-L57 的注释给了两个理由——一是用 `reciprocal` 配合 `dtype` 参数能在指定精度下计算，避免精度损失；二是这种写法便于处理「零长度轴」（\(n=0\)）这一边界，避免直接除以零或额外加判断。

---

### 4.2 standard 频率排列（0、正频率、Nyquist、负频率）

#### 4.2.1 概念说明

公式告诉你每个 \(A_k\) 是什么，但没告诉你「输出数组里第 `k` 个元素对应哪个频率」。这正是初学者最容易栽跟头的地方：**`fft` 的输出并不是按频率从低到高排好的**。

`numpy.fft` 采用一种叫 **"standard" order（标准排列）** 的约定：

- `A[0]` 是**零频（DC）**分量，等于信号之和 \(\sum_m a_m\)；
- 接下来是**正频率**段；
- 对偶数长度，正中间那一位是 **Nyquist** 频率；
- 最后是**负频率**段，从最负开始往零靠近。

换句话说，零频被放在数组**最前面**，而不是中间。要把零频搬到中间可视化，需要用 `fftshift`（下一篇 u2-l3 会讲）。

#### 4.2.2 核心流程

对长度 \(n\)，输出索引到「频率箱」的映射如下（设采样间隔为 \(\Delta t\)，频率单位为 \(1/\Delta t\)）：

**偶数 \(n\)（以 \(n=8\) 为例）：**

| 索引 `k` | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 |
|-----------|---|---|---|---|---|---|---|---|
| 频率 | 0 | \(+1\) | \(+2\) | \(+3\) | \(\pm n/2=\pm4\)（Nyquist） | \(-3\) | \(-2\) | \(-1\) |
| 含义 | 零频 DC | 正频率段 `A[1:n//2]` | | | Nyquist `A[n//2]` | 负频率段 `A[n//2+1:]` | | |

**奇数 \(n\)（以 \(n=7\) 为例）：**

| 索引 `k` | 0 | 1 | 2 | 3 | 4 | 5 | 6 |
|-----------|---|---|---|---|---|---|---|
| 频率 | 0 | \(+1\) | \(+2\) | \(+3\) | \(-3\) | \(-2\) | \(-1\) |
| 含义 | 零频 DC | 正频率段 `A[1:(n-1)//2+1]` | | 最大正频 | 最大负频起，负频率段 | | |

奇偶两种情形的差别在于：**偶数长度多出一个独立的 Nyquist 位**（正负 Nyquist 在那里「混叠」到一起）；奇数长度没有 Nyquist 位，正频率和负频率各占 \((n-1)/2\) 个。

> 一个等价的、最省心的看法：直接用 `np.fft.fftfreq(n, d=Δt)` 生成与 `A` 逐位对齐的频率轴，就不用背这张表。`fftfreq` 的实现细节是下一篇 u2-l2 的主题，本讲先用它的结果来「贴标签」。

**实输入的 Hermitian 共轭对称性**：当输入 \(a_m\) 是实数时，频谱满足

\[
A_{-k \bmod n} \;=\; \overline{A_k},
\]

即「频率 \(+f\)」处的值是「频率 \(-f\)」处值的**复共轭**。推论：

- `A[0]`（零频）必为实数；
- 偶数长度时 Nyquist 位 `A[n//2]` 也必为实数；
- 对 \(n=8\)：`A[1]=conj(A[7])`、`A[2]=conj(A[6])`、`A[3]=conj(A[5])`。

这意味着**实信号的负频率分量没有带来任何新信息**——这正是 `rfft` 只算非负频率、输出长度减半的依据（u3-l4 详讲）。

**幅度谱与相位谱**：当 `A = fft(a)` 时，

- 幅度谱（amplitude spectrum）= `np.abs(A)`；
- 功率谱（power spectrum）= `np.abs(A)**2`；
- 相位谱（phase spectrum）= `np.angle(A)`。

#### 4.2.3 源码精读

"standard" 排列的权威描述在 docstring：

[\_\_init\_\_.py#L96-L104](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/__init__.py#L96-L104) — 逐句给出排列规则：`A[0]` 含零频项；`A[1:n/2]` 含正频率项；`A[n/2+1:]` 含负频率项（按「从最负到接近零」的顺序）；偶数点数时 `A[n/2]` 是 Nyquist（且对实输入为实数）；奇数点数时 `A[(n-1)/2]` 是最大正频、`A[(n+1)/2]` 是最大负频。

[\_\_init\_\_.py#L105-L109](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/__init__.py#L105-L109) — 指出 `fftfreq(n)` 给出与输出逐位对应的频率，`fftshift` 把零频搬到中间、`ifftshift` 是其逆操作。（这三个 helper 是接下来 u2-l2、u2-l3 的主角。）

[\_\_init\_\_.py#L111-L113](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/__init__.py#L111-L113) — 明确 `np.abs(A)` 是幅度谱、`np.abs(A)**2` 是功率谱、`np.angle(A)` 是相位谱。

[\_\_init\_\_.py#L146-L159](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/__init__.py#L146-L159) — "Real and Hermitian transforms" 一节，给出 Hermitian 对称的定义：频率 \(f_k\) 处的分量是 \(-f_k\) 处分量的复共轭；并由此推出 `n` 个实输入点只产生 `n/2+1` 个独立复数输出（`rfft` 的依据）。

> 注意：`_pocketfft.py` 里**并没有**任何 Python 代码在「排列」频率顺序——输出顺序是由 C++ 后端 pocketfft 直接给出的、与上面 docstring 一一对应的标准排列。Python 层只负责把 `n`（输出长度）和 `axis`（变换轴）传下去，见 [_pocketfft.py#L78-L101](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft.py#L78-L101) 里 `n_out` 与 `ufunc(...)` 的调用。

#### 4.2.4 代码实践

**实践目标**：对一段长度 8 的实信号做 `fft`，亲手标出 `A[0]`、正频率段、Nyquist、负频率段对应的索引，并用 `np.abs`/`np.angle` 得到幅度谱与相位谱，验证 Hermitian 共轭对称。

**操作步骤**：

```python
import numpy as np

# 长度 8 的实信号：1Hz 与 3Hz 两个正弦波叠加 + 直流
n = 8
t = np.arange(n)
a = 1.0 + 2.0*np.sin(2*np.pi*1*t/n) + 0.5*np.sin(2*np.pi*3*t/n)
A = np.fft.fft(a)
freq = np.fft.fftfreq(n, d=1.0)        # 与 A 逐位对齐的频率轴

print("索引  频率    A[k]                |A[k]|          angle(A[k])")
for k in range(n):
    print(f"{k:>3}  {freq[k]:>+5.2f}  {A[k]:>20.4f}  {np.abs(A[k]):>12.4f}  {np.angle(A[k]):>+9.4f}")

# 1) 标注四个区段
print("\nA[0]        零频 DC        =", A[0].real, "(应为实数 ≈ 信号之和 =", a.sum(), ")")
print("A[1:4]      正频率段        =", A[1:4])
print("A[4]        Nyquist(n//2)   =", A[4], "(应为实数)")
print("A[5:8]      负频率段        =", A[5:8])

# 2) 验证 Hermitian 共轭对称：A[k] == conj(A[n-k])
print("\nHermitian 对称性检验：")
for k in [1, 2, 3]:
    print(f"  A[{k}]={A[k]:.4f}   conj(A[{n-k}])={np.conj(A[n-k]):.4f}   "
          f"差={np.abs(A[k]-np.conj(A[n-k])):.2e}")

# 3) 幅度谱与相位谱
amp   = np.abs(A)
phase = np.angle(A)
print("\n幅度谱 |A| =", np.round(amp, 4))
print("相位谱 ∠A =", np.round(phase, 4))
```

**需要观察的现象**：

1. `freq` 数组应为 `[0, 0.125, 0.25, 0.375, -0.5, -0.375, -0.25, -0.125]`，正好对应 §4.2.2 表中 \(n=8\) 的频率箱（索引 4 是 Nyquist，对应 \(\pm 0.5\)）；
2. `A[0]` 应近似等于 `a.sum()`（即 `8.0`，因为直流分量为 1），且虚部为 0；
3. `A[4]`（Nyquist）虚部应近似为 0；
4. 三行 Hermitian 检验的「差」都应是机器精度量级（约 \(10^{-16}\)），证实 `A[k]=conj(A[n-k])`；
5. 幅度谱里，`A[1]` 与 `A[7]`、`A[3]` 与 `A[5]` 的模应两两相等（共轭对的模相等）。

**预期结果**：上述 5 点全部成立。若输入改成复信号，则第 2、3、4 点不再成立——因为 Hermitian 对称只对**实输入**有效。

> 若环境无法运行，请标注「待本地验证」并把代码带回本地执行。

#### 4.2.5 小练习与答案

**练习 1**：对 \(n=7\)（奇数）做同样的标注，正频率、负频率各占几个位置？有没有 Nyquist 位？

**答案**：正频率占索引 `1,2,3`（3 个），负频率占索引 `4,5,6`（3 个），共 \((n-1)/2=3\) 对。**没有**独立的 Nyquist 位——奇数长度下 Nyquist 频率 \(n/2=3.5\) 不是整数倍，落不在任何一个箱上。可用 `np.fft.fftfreq(7)` 验证：得到 `[0, 1, 2, 3, -3, -2, -1]/7`。

**练习 2**：为什么实信号频谱的 `A[0]` 一定是实数？

**答案**：把 \(k=0\) 代入正变换公式：\(A_0=\sum_m a_m\,e^0=\sum_m a_m\)。当所有 \(a_m\) 为实数时，实数之和仍是实数。从对称性看，\(A_0=A_{-0}=A_0\)，而 Hermitian 对称要求 \(A_0=\overline{A_0}\)，故 \(A_0\) 必为实数。

---

### 4.3 Type Promotion（类型提升）背景说明

#### 4.3.1 概念说明

「Type Promotion（类型提升）」指的是：当不同精度的数值参与运算时，结果统一取到「更宽」的精度。`numpy.fft` 在这件事上有一个**很多人踩坑**的规则：

> **无论输入是 `float32` 还是 `complex64`，`fft` 的输出都会被提升到双精度（`float64` / `complex128`）。**

也就是说：

| 输入 dtype | `fft`/`ifft`/`rfft` 输出 dtype | `irfft` 输出 dtype |
|------------|--------------------------------|--------------------|
| `float32` | `complex128` | `float64` |
| `complex64` | `complex128` | `float64` |
| `float64` | `complex128` | `float64` |
| `complex128` | `complex128` | `float64` |

这不是 bug，而是**有意的精度策略**：FFT 是层层叠加的求和，低精度（尤其 `float32`）累计误差会很快放大，因此实现统一在双精度上计算。

**为什么特意拿出来说？** 因为并非所有 FFT 库都这样——docstring 明确指出：如果你需要**保留输入精度、不做提升**的 FFT，应改用 `scipy.fftpack`。在大规模、对内存与带宽敏感的场景（比如处理巨大的 `float32` 数组），`complex128` 输出会让内存占用翻倍，这一点必须在选库时就心里有数。

#### 4.3.2 核心流程

在 `_pocketfft.py` 的 `_raw_fft` 里，输出数组的 `dtype` 由两处决定：

```
进入 _raw_fft(a, ...)
   ├─ real_dtype = result_type(a.real.dtype, 1.0)      # 用于算归一化系数 fct
   │                                                    # 也用于 irfft 的实输出 dtype
   ├─ 若需要分配 out：
   │     ├─ irfft（实数复数进、实数出）→ out_dtype = real_dtype
   │     └─ 其余变换（复数输出）   → out_dtype = result_type(a.dtype, 1j)
   └─ out = empty_like(a, shape=..., dtype=out_dtype)
```

关键点：`result_type(a.dtype, 1j)` 里的 `1j` 是 Python 复数标量。它参与类型提升后，最终把输出 `dtype` 钉在**至少双精度复数** `complex128` 上——这正是 §4.3.1 表格里所有复数输出列都是 `complex128` 的代码根源。而 `real_dtype = result_type(a.real.dtype, 1.0)` 同理把归一化计算（以及 `irfft` 的实输出）放在**至少双精度实数** `float64` 上，避免 `float32` 下 `sqrt`/`reciprocal` 的精度损失。

#### 4.3.3 源码精读

Type Promotion 的「权威声明」在 docstring：

[\_\_init\_\_.py#L124-L129](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/__init__.py#L124-L129) — "Type Promotion" 一节白纸黑字：`numpy.fft` 把 `float32`/`complex64` 提升为 `float64`/`complex128`；并指向 `scipy.fftpack` 作为「不提升」的替代。

而这一策略在代码里的落点，是 `_raw_fft` 里的两行：

[_pocketfft.py#L67](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft.py#L67) — `real_dtype = result_type(a.real.dtype, 1.0)`。它既决定归一化系数 `fct` 的计算精度，也决定 `irfft` 的实数输出精度。注释 L62-L63 说得很明白：传入数组 dtype 是「为了避免 `sqrt`/`reciprocal` 的精度损失」。

[_pocketfft.py#L90-L96](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_pocketfft.py#L90-L96) — 自动分配 `out` 时的 `dtype` 决策。注意 L91-L94 的分支：

```python
if is_real and not is_forward:   # irfft: 复数进、实数出
    out_dtype = real_dtype       # → 至少 float64
else:                            # 其余变换: 复数输出
    out_dtype = result_type(a.dtype, 1j)   # → 至少 complex128
```

这两行就是 §4.3.1 那张表的代码化身：`irfft` 走 `real_dtype`（实输出，至少 `float64`），其余变换走 `result_type(a.dtype, 1j)`（复输出，至少 `complex128`）。

> 旁注：若你**显式**传入了 `out` 参数，则 `_raw_fft` 不会重新分配，而是校验你给的 `out` 形状是否正确（见 L97-L99）。但 `out` 的 `dtype` 仍需你自己保证——这部分（含报错条件）会在 u3-l3 详讲。

#### 4.3.4 代码实践

**实践目标**：用 `float32` / `complex64` 输入调用各变换，亲眼看到输出被提升为双精度。

**操作步骤**：

```python
import numpy as np

f32 = np.array([1, 2, 3, 4], dtype=np.float32)
c64 = np.array([1+1j, 2, 3-1j, 4], dtype=np.complex64)

print("fft(f32).dtype   =", np.fft.fft(f32).dtype)      # 期望 complex128
print("fft(c64).dtype   =", np.fft.fft(c64).dtype)      # 期望 complex128
print("rfft(f32).dtype  =", np.fft.rfft(f32).dtype)     # 期望 complex128
print("ifft(c64).dtype  =", np.fft.ifft(c64).dtype)     # 期望 complex128
print("irfft(c64).dtype =", np.fft.irfft(c64).dtype)    # 期望 float64

# 对照：同一段计算用 float64 输入，输出也是 complex128
f64 = f32.astype(np.float64)
print("fft(f64).dtype   =", np.fft.fft(f64).dtype)      # 期望 complex128
```

**需要观察的现象**：所有 `fft`/`ifft`/`rfft` 输出都是 `complex128`；`irfft` 输出是 `float64`。无论输入是 32 位还是 64 位，输出都被「钉」在双精度上。

**预期结果**：输出与注释里的期望完全一致。这就是 docstring L124-L129 所说的提升行为。

> **无法确定运行结果时的说明**：以上 dtype 行为是 `numpy.fft` 长期稳定、且在当前代码 docstring 中明确保证的契约；若你在极特殊的自定义后端上运行，结果可能不同，此时请以本地实际输出为准。

#### 4.3.5 小练习与答案

**练习 1**：如果你有一个巨大的 `float32` 信号、内存吃紧，`np.fft.fft` 的输出会占用多少倍于输入的内存？为什么？

**答案**：约 **4 倍**。输入是 4 字节/元素的 `float32`；输出被提升为 `complex128`，每个复数占 16 字节（实部、虚部各一个 8 字节双精度浮点）。所以内存比值是 \(16/4=4\)。换句话说，一个 1GB 的 `float32` 信号，`fft` 输出约 4GB。这个 4 倍同时包含了两件事：实数→复数的元素宽度翻倍（4→8 字节），以及精度翻倍（8→16 字节）。若不可接受，可考虑 `scipy.fftpack`（保留 `float32`/`complex64`）或分块处理。

**练习 2**：`real_dtype = result_type(a.real.dtype, 1.0)` 里，为什么取 `a.real.dtype` 而不是 `a.dtype`？

**答案**：因为 `a` 可能是复数数组，`a.dtype` 会是 `complex...`；而归一化系数 `fct` 是个**实数**标量，应当在实数 dtype 上计算。`a.real.dtype` 取的是 `a` 实部的 dtype（复数组的实部精度与数组本身一致），用它来决定 `fct` 的计算精度既正确（实数）又贴合输入精度，再通过 `result_type(..., 1.0)` 保证至少双精度。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「**手写一个最小 DFT 参考实现并与 `np.fft.fft` 对照**」的任务。它一次性验证：公式约定（§4.1）、频率排列（§4.2）、Type Promotion（§4.3）。

**任务**：

1. 对长度 \(n=8\) 的实信号 `a`，按 §4.1.2 的正变换公式，**用三重循环手写**一个朴素 DFT（不要调用 `np.fft`），得到 `A_ref`；
2. 调用 `np.fft.fft(a)` 得到 `A`，比较 `A_ref` 与 `A`，确认它们逐位相等——这验证了「指数取负号、不归一化」的约定，也验证了 standard 排列；
3. 用 `np.fft.fftfreq(8)` 给 `A` 贴上频率标签，确认 `A_ref` 的第 `k` 位与频率 `fftfreq(8)[k]` 对应；
4. 把输入转成 `float32`，再次调用 `np.fft.fft`，用 `.dtype` 确认输出被提升为 `complex128`；
5. （选做）把手写 DFT 的指数符号翻转（取 \(+2\pi i\)），观察结果等于 `np.fft.ifft(a)*n`——亲手验证「正负号配对」的互逆关系。

参考骨架（**示例代码**，请补全）：

```python
import numpy as np

n = 8
a = np.cos(2*np.pi*2*np.arange(n)/n) + 0.3   # 含 2Hz 分量 + 直流

# 1) 手写朴素 DFT：A_ref[k] = sum_m a[m] * exp(-2πi·m·k/n)
A_ref = np.zeros(n, dtype=complex)
for k in range(n):
    for m in range(n):
        A_ref[k] += a[m] * np.exp(-2j*np.pi*m*k/n)

# 2) 与 np.fft.fft 对照
A = np.fft.fft(a)
print("max |A_ref - A| =", np.max(np.abs(A_ref - A)))   # 期望 ~1e-15

# 3) 频率标签
print("freq =", np.fft.fftfreq(n))

# 4) Type Promotion
print("fft(float32).dtype =", np.fft.fft(a.astype(np.float32)).dtype)
```

**验收标准**：第 2 步误差为机器精度量级；第 3 步频率数组与 §4.2.2 的 \(n=8\) 表一致；第 4 步输出 `complex128`。若全部通过，说明你已经把本讲的三个最小模块真正「吃透」了。

---

## 6. 本讲小结

- `numpy.fft` 的 DFT 约定是：**正变换指数取负号、不归一化；反变换指数取正号、默认带 \(\frac{1}{n}\)**。这保证 `ifft(fft(a)) ≈ a`，根源是复指数的正交性。
- 公式里的归一化在代码中由 `_raw_fft` 的系数 `fct` 加上 `_swap_direction` 统一实现；`None` 是默认 `"backward"` 的别名。
- 输出采用 **"standard" 排列**：`A[0]` 是零频 DC、`A[1:n/2]` 是正频率、偶数长度时 `A[n/2]` 是 Nyquist、`A[n/2+1:]` 是负频率；奇数长度无独立 Nyquist 位。用 `fftfreq(n)` 可逐位对齐频率。
- **实输入的频谱满足 Hermitian 共轭对称** \(A_{-k}=\overline{A_k}\)，故 `A[0]`（及偶数时的 Nyquist）必为实数，负频率不带新信息——这是 `rfft` 减半输出的依据。
- `np.abs(A)` 是幅度谱、`np.abs(A)**2` 是功率谱、`np.angle(A)` 是相位谱。
- **Type Promotion**：`float32`/`complex64` 一律提升为 `float64`/`complex128`，落点是 `_raw_fft` 里 `real_dtype = result_type(a.real.dtype, 1.0)` 与 `out_dtype = result_type(a.dtype, 1j)`；若需保留低精度，改用 `scipy.fftpack`。

---

## 7. 下一步学习建议

本讲建立了「数学约定」这层地基，接下来可以顺着两条线走：

1. **先把 helper 用熟**：下一篇 **u2-l2（`fftfreq` 与 `rfftfreq`）** 会讲清频率箱数组到底怎么由 `n` 和采样间距 `d` 算出来、奇偶长度差异在哪；**u2-l3（`fftshift`/`ifftshift`）** 讲怎么把零频搬到频谱中央做可视化。它们让你不用再背本讲的排列表。
2. **再下探代码**：当你想看清 `fft`/`ifft` 的完整调用链与 `_raw_fft` 的逐行逻辑时，进入第三单元 **u3-l1（`fft`/`ifft` 与 `_raw_fft` 主流程）**；想彻底搞懂 `norm` 的三模式与 `_swap_direction` 的设计，看 **u3-l2**；想了解 `out` 参数与 `dtype` 决策的细节，看 **u3-l3**。

建议的阅读顺序：u2-l2 → u2-l3 → u3-l1。
