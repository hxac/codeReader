# 经典窗函数的实现细节

## 1. 本讲目标

本讲深入 `scipy.signal.windows._windows.py`，拆解几类「经典窗函数」的真实实现。读完本讲，你应当能够：

1. 说清 `_len_guards` / `_extend` / `_truncate` 三个小工具如何用「加一再去尾」这一**与窗形状无关的通用模板**统一实现「对称窗」与「周期窗」。
2. 理解 `general_cosine` 作为 Hann/Hamming/Blackman/Nuttall/Flattop 等**广义余弦窗族公共基底**的地位，并能解释为什么它们的实现体都只有「填几个系数 + 调一次 `_general_cosine_impl`」。
3. 掌握 `kaiser` 用**零阶修正贝塞尔函数** \(I_0\) 逼近 DPSS 的原理，以及 `beta` 参数如何在「主瓣宽度」与「旁瓣电平」之间权衡。
4. 了解 `dpss`（离散长球序列 / Slepian 序列）如何把「能量最集中」这件事转化成一个**对称三对角特征值问题**，并用 `scipy.linalg.eigh_tridiagonal` 求解。
5. 会用 `numpy.fft` 实际测量一个窗的主瓣宽度与最高旁瓣电平，并解释 `sym=True` 与 `sym=False` 的差别。

本讲承接 [u2-l3 窗函数子包与 get_window 调度](u2-l3-windows-subpackage.md)：上一讲讲的是「子包如何组织、`get_window` 如何按名字分发、`sym`/`fftbins` 如何取反」，本讲则钻进被分发的那些窗函数本身，看它们的数学公式是怎样变成代码的。

## 2. 前置知识

如果你对以下概念已经熟悉，可以跳过本节。

- **窗函数（window function）**：一段有限长、在两端逐渐衰减到 0（或接近 0）的序列，用于在频谱分析或 FIR 设计中给信号「加窗」，以减少截断带来的频谱泄漏。
- **主瓣（main lobe）与旁瓣（side lobe）**：把窗函数做傅里叶变换得到它的频率响应；中心最宽、最高的那个峰叫主瓣，决定**频率分辨率**；主瓣两侧较小的峰叫旁瓣，决定**频谱泄漏**的大小。主瓣越窄、旁瓣越低，窗越好——但这两者不可兼得，这是贯穿全讲的核心权衡。
- **分贝（dB）**：幅值比取对数再乘 20，即 \(20\log_{10}|A|\)。最高旁瓣常用负分贝表示，例如「-58 dB」表示旁瓣幅度是主瓣峰值的约 \(10^{-58/20}\approx 1.3\times 10^{-3}\) 倍。
- **对称窗 vs 周期窗**：用于滤波器设计的窗希望时域上严格左右对称（`sym=True`）；用于 FFT 谱分析的窗希望周期延拓后连续（`sym=False`，又称 DFT-even 或 periodic）。两者的差别本讲会从源码层面讲透。
- **零阶修正贝塞尔函数 \(I_0\)**：一类特殊函数，形状像一个钟形曲线，Kaiser 窗用它来生成窗形状。
- **特征值问题**：对矩阵 \(A\) 求解 \(Av=\lambda v\)，向量 \(v\) 称为特征向量。`dpss` 把求最优窗变成了求一个矩阵的若干个最大特征值对应的特征向量。

## 3. 本讲源码地图

本讲只涉及一个文件，但它有 2600 多行、实现了 26 个窗，所以我们只精读其中与「最小模块」相关的部分。

| 文件 | 本讲关注的内容 | 作用 |
|---|---|---|
| [windows/_windows.py](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/windows/_windows.py) | `_len_guards` / `_extend` / `_truncate` | 对称性辅助工具，所有窗共用 |
| 同上 | `_general_cosine_impl` / `general_cosine` | 广义余弦窗族的公共计算内核 |
| 同上 | `blackman` / `hann` / `hamming` / `general_hamming` | 三个经典窗只是给内核喂不同系数 |
| 同上 | `kaiser` | 贝塞尔窗 |
| 同上 | `dpss` / `_fftautocorr` | Slepian 序列（三对角特征值法） |

提示：上一讲提到的 `windows/__init__.py`（暴露 `_windows` 里的函数）和 `windows/windows.py`（弃用 stub）本讲不再展开。

## 4. 核心概念与源码讲解

### 4.1 对称性基础设施：`_len_guards` / `_extend` / `_truncate`

#### 4.1.1 概念说明

同一个窗函数名（比如 `hann`），调用时既可以返回「对称窗」也可以返回「周期窗」，二者差别仅在于**采样点的取法**：

- **对称窗（`sym=True`，默认）**：在区间 \([-\pi,\pi]\) 上等间隔取 \(M\) 个点，端点对称。用于 FIR 滤波器设计（线性相位要求严格对称的系数）。
- **周期窗（`sym=False`）**：把区间看作「首尾相接」的一个周期，取 \(M\) 个点使窗在循环延拓时连续。用于 FFT 谱分析（DFT 隐含周期延拓）。

如果为每一种窗各写一份「周期版」代码，会非常啰嗦。`_windows.py` 的做法是：**统一按「周期采样」生成 \(M+1\) 个点，再砍掉最后一个点得到对称的 \(M\) 个点**——但注意，这个「加一再去尾」的方向在源码里是反过来的：当 `sym=False` 时多算一个点、再砍掉，等价于把对称基底偏移成周期基底。无论哪种窗，这段逻辑都一样，所以被抽成三个公共小工具。

#### 4.1.2 核心流程

```
输入: M(长度), sym(是否对称)
  ├─ _len_guards(M): 检查 M 是否合法；若 M<=1 直接返回「无需延拓」标记
  ├─ _extend(M, sym):
  │     sym == False → 返回 (M+1, True)   # 多算一个点，标记稍后需要截断
  │     sym == True  → 返回 (M,   False)  # 不延拓
  ├─ (用延拓后的长度计算窗 w)
  └─ _truncate(w, needs_trunc):
        needs_trunc == True → w[:-1]        # 砍掉最后一个点
        needs_trunc == False→ w             # 原样返回
```

关键直觉：`_extend` 决定「要不要多算一个点」，`_truncate` 决定「要不要把那个点多算的砍掉」。两者通过 `needs_trunc` 这个布尔值串联，对窗的具体形状完全无感——这正是它能把所有窗的对称/周期处理统一起来的原因。

#### 4.1.3 源码精读

三个工具都非常短。先看长度守卫：

[windows/_windows.py:23-27](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/windows/_windows.py#L23-L27) —— 校验 `M` 必须是非负整数，并在 `M<=1` 时返回 `True`（调用方据此直接返回 `ones(M)`，避免后续除零或空数组问题）。

[windows/_windows.py:30-35](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/windows/_windows.py#L30-L35) —— `_extend`：`sym=False` 时长度变 `M+1` 并置 `needs_trunc=True`，这是「DFT-even 延拓」的核心一行。

[windows/_windows.py:38-43](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/windows/_windows.py#L38-L43) —— `_truncate`：按 `needed` 决定是否 `w[:-1]`。注意它返回的是切片视图/副本，调用方无需关心。

#### 4.1.4 代码实践

**实践目标**：亲手验证「`sym` 只改变采样点的取法，不改变窗的数学形状」。

**操作步骤**：

```python
# 示例代码：对比 hann 的两种对称性
import numpy as np
from scipy.signal.windows import hann

M = 8
w_sym = hann(M, sym=True)      # 对称
w_per = hann(M, sym=False)     # 周期 (DFT-even)

print("sym=True :", np.round(w_sym, 4))
print("sym=False:", np.round(w_per, 4))
```

**需要观察的现象**：

- `sym=True` 时窗严格左右对称，且（M 为偶数时）**最大值 1.0 不会出现**，两端值相等。
- `sym=False` 时窗在 `w[0]` 处可以等于 0、而末点 `w[-1]` 与首点 `w[0]` 在循环延拓后能拼成连续波形。

**预期结果**：两组数值不同但形状同源；`sym=False` 恰好比 `sym=True` 多采了「一个周期」的相位，再砍掉末点。精确数值**待本地验证**，但你会看到二者都是钟形、对称中心一致。

#### 4.1.5 小练习与答案

1. **问**：如果调用 `hann(1)`，会进入 `_general_cosine_impl` 的哪条分支？为什么？
   **答**：会先被 `_len_guards(1)` 命中（`1<=1` 返回 `True`），直接返回长度为 1 的 `ones`，根本不会走到延拓/截断逻辑。这是为了避免 `alpha=(M-1)/2=0` 导致后续除零。

2. **问**：把 `_extend` 里的 `sym=False` 分支改成「返回 `(M, False)`」会有什么后果？
   **答**：周期窗将退化成对称窗——所有 `sym=False` 的窗都不再具备 DFT-even 性质，FFT 谱分析时会出现额外的边界跳变泄漏。

---

### 4.2 `general_cosine`：广义余弦窗族的公共基底

#### 4.2.1 概念说明

很多经典窗（Hann、Hamming、Blackman、Nuttall、Blackman-Harris、Flattop……）本质上都是**若干个余弦项的加权和**：

\[
w(n) = \sum_{k=0}^{K-1} a_k \cos(k\theta_n)
\]

不同的窗，只是**系数列表 `[a_0, a_1, ..., a_{K-1}]` 不同**、项数 K 不同而已。`_windows.py` 把这个共同形式抽成一个内核 `_general_cosine_impl`，于是上面那一长串窗的实现体都退化成「填系数 + 调内核」两行。这就是为什么这个文件能装下 26 个窗却仍然不臃肿——**公共数学形式被抽象掉了**。

特别要注意一个实现约定（写在 `general_cosine` 文档字符串里）：系数采用「**以原点为中心**」的约定，所以它们**通常全是正数**，而不是教科书里正负交替的写法。例如 Blackman 教科书写法是 `0.42 - 0.5·cos(...) + 0.08·cos(...)`，而源码里存成 `[0.42, 0.50, 0.08]` 全正——因为 `cos` 自变量 \(\theta_n\) 从 \(-\pi\) 扫到 \(\pi\)，到边缘时 \(\cos(\theta_n)\) 自然变负，等价于正负交替。

#### 4.2.2 核心流程

```
输入: M(长度), a(系数数组 [a0,a1,...]), sym
  ├─ _len_guards(M) 命中 → 返回 ones(M)
  ├─ M, needs_trunc = _extend(M, sym)
  ├─ fac = linspace(-pi, pi, M)        # 关键：相位轴以 0 为中心
  ├─ w = 0
  ├─ for k in range(len(a)):
  │       w += a[k] * cos(k * fac)      # 广义余弦叠加
  └─ return _truncate(w, needs_trunc)
```

相位轴 `fac = linspace(-π, π, M)` 是理解整个窗族的关键变量。它以原点对称，所以 `cos(0·fac)=1` 恒成立、`cos(k·fac)` 在中心取 1、在两端取 \((-1)^k\)。于是窗在中心处的值恰为 \(\sum_k a_k\)（归一化使它等于 1），在两端按余弦自然衰减——这就是「系数全正、形状仍正确」的数学原因。

#### 4.2.3 源码精读

先看内核实现，它把上面的流程逐行写出：

[windows/_windows.py:55-65](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/windows/_windows.py#L55-L65) —— `_general_cosine_impl`。注意三处与 4.1 的呼应：开头 `_len_guards` 守卫、中间 `_extend` 决定长度、结尾 `_truncate` 收尾。`for k in range(a.shape[0])` 累加各阶余弦，是整个窗族唯一的「计算」所在。

公共入口 `general_cosine` 只负责把系数转成数组、探查数组命名空间，再转发给内核：

[windows/_windows.py:145-148](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/windows/_windows.py#L145-L148) —— `general_cosine` 公开函数的函数体（含 `xp`/`device` 的多后端处理，这部分会在 u10 单元展开）。

现在看三个经典窗如何复用这个内核——它们的实现体短得惊人。**Blackman**（3 项）：

[windows/_windows.py:491-494](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/windows/_windows.py#L491-L494) —— `blackman` 主体：`a = [0.42, 0.50, 0.08]`，然后一行 `_general_cosine_impl`。

**Hamming/Hann** 走另一条更巧的路：它们都属于「广义 Hamming 窗」`w(n)=\alpha-(1-\alpha)\cos(\cdot)`，所以共用一个带参数 `alpha` 的 `general_hamming`：

[windows/_windows.py:1115-1118](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/windows/_windows.py#L1115-L1118) —— `general_hamming` 主体：`a = [alpha, 1-alpha]`。

[windows/_windows.py:876](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/windows/_windows.py#L876) —— `hann` 主体就是一行：`return general_hamming(M, 0.5, ...)`（即 `alpha=0.5`）。

[windows/_windows.py:1199](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/windows/_windows.py#L1199) —— `hamming` 主体也是一行：`return general_hamming(M, 0.54, ...)`。

> 源码里 `blackman` 的文档字符串给出的公式是 `w(n)=0.42-0.5·cos(2πn/M)+0.08·cos(4πn/M)`（正负交替、分母为 M），而内核实际用的是「全正系数、分母随 `sym` 变化、相位以 0 为中心」的 Nuttall 约定。两者数学上等价、约定不同——这是阅读本文件时最容易被「文档公式」误导的地方，记住**代码里跑的是 `_general_cosine_impl` 那一版**。

#### 4.2.4 代码实践

**实践目标**：用 `general_cosine` 自己「拼」出一个 Hann 窗，并与官方 `hann` 对照，验证「换系数即换窗」。

**操作步骤**：

```python
# 示例代码：用 general_cosine 复刻 hann
import numpy as np
from scipy.signal.windows import general_cosine, hann

M = 16
# hann 对应 alpha=0.5 → general_hamming → a=[0.5, 0.5]
my_hann = general_cosine(M, [0.5, 0.5], sym=True)
ref_hann = hann(M, sym=True)

print("最大绝对误差:", np.max(np.abs(my_hann - ref_hann)))
```

**预期结果**：最大绝对误差应为 `0.0`（二者完全一致），证明 `hann` 确实就是 `general_cosine(M, [0.5,0.5])` 的语法糖。若不为 0，说明系数或 `sym` 取错。

#### 4.2.5 小练习与答案

1. **问**：`general_cosine(M, [1.0])`（只有一项、系数为 1）等价于哪个窗？
   **答**：等价于**矩形窗（boxcar）**。因为 `w = 1·cos(0·fac) = 1`，处处为 1。

2. **问**：为什么 Blackman 用 3 项、Hann 只用 2 项，但 Blackman 的旁瓣更低？
   **答**：项数越多，可调系数越多，越能在边缘做更平滑的过渡（更高阶导数为零），从而把能量更集中在主瓣、旁瓣更低——代价是主瓣更宽。Blackman 多出的 `0.08·cos(2·fac)` 项就是用来压低旁瓣的。

3. **问**：若把 `fac = linspace(-π, π, M)` 改成 `linspace(0, 2π, M)`，窗的形状会变吗？
   **答**：形状不变（只是相位起点平移了 π），因为窗对中心对称；但系数的「全正」约定会失效——这正说明「以原点为中心」是一个为「系数全正、易读」而刻意选择的约定。

---

### 4.3 `kaiser` 窗：用贝塞尔函数做主瓣-旁瓣权衡

#### 4.3.1 概念说明

Kaiser 窗不是余弦叠加，而是用**零阶修正贝塞尔函数** \(I_0\) 构造：

\[
w(n) = \frac{I_0\!\left(\beta\sqrt{1-\left(\frac{2n}{M-1}\right)^2}\right)}{I_0(\beta)},\qquad -\tfrac{M-1}{2}\le n\le \tfrac{M-1}{2}
\]

只有一个形状参数 `beta`（有的文献用 \(\alpha=\beta/\pi\)）。它的魅力在于：**调一个 `beta` 就能在「主瓣窄、旁瓣高」和「主瓣宽、旁瓣低」之间连续滑动**。源码文档给出经验对照表：

| beta | 近似于 |
|---|---|
| 0 | 矩形窗 |
| 5 | Hamming 窗 |
| 6 | Hann 窗 |
| 8.6 | Blackman 窗 |

更重要的是，Kaiser 窗是**离散长球序列（DPSS / Slepian 窗）的一个良好近似**——而 DPSS 才是「主瓣能量占比最大」的理论最优窗（见 4.4）。Kaiser 的价值在于：它只用一个解析的贝塞尔函数就能逼近那个需要解特征值问题才能得到的最优窗，计算上便宜得多。

#### 4.3.2 核心流程

```
输入: M, beta, sym
  ├─ _len_guards(M) 命中 → 返回 ones(M)
  ├─ M, needs_trunc = _extend(M, sym)
  ├─ n = arange(M)                  # 0..M-1
  ├─ alpha = (M - 1) / 2            # 中心位置
  ├─ w = i0(beta * sqrt(1 - ((n - alpha)/alpha)**2)) / i0(beta)
  └─ return _truncate(w, needs_trunc)
```

把变量代换 \(m = n-\alpha\) 代入源码，即可得到上面的标准公式：`(n-alpha)/alpha = m/((M-1)/2) = 2m/(M-1)`，正是公式根号里的项。`special.i0` 是 SciPy 提供的 \(I_0\)，分子分母相除完成归一化，使窗峰值为 1。

#### 4.3.3 源码精读

[windows/_windows.py:1310-1322](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/windows/_windows.py#L1310-L1322) —— `kaiser` 主体。注意它**没有走 `_general_cosine_impl`**，因为贝塞尔函数不是余弦叠加，但同样复用了 `_len_guards/_extend/_truncate` 这套对称性基础设施。`special.i0` 来自 `from scipy import special`（文件顶部第 9 行导入）。文档里关于「beta 过大、采样不足会返回 NaN」的警告，根源就在 `sqrt(1 - ...)` 里被开方的项可能因为浮点误差略小于 0。

#### 4.3.4 代码实践

**实践目标**：直观感受 `beta` 对主瓣/旁瓣的影响。

**操作步骤**：

```python
# 示例代码：观察 beta 对 Kaiser 窗的影响
import numpy as np
from scipy.signal.windows import kaiser
from scipy.fft import fft, fftshift

M = 64
for beta in [0, 5, 8.6, 14]:
    w = kaiser(M, beta, sym=False)
    A = fft(w, 8192)
    resp = 20*np.log10(np.abs(fftshift(A / np.abs(A).max())))
    # 最高旁瓣电平 ≈ 主瓣之外的最大值
    half = len(resp)//2
    sidelobe = resp[half+8:].max()      # 跳过主瓣前几格
    print(f"beta={beta:>4}: 最高旁瓣 ≈ {sidelobe:.1f} dB")
```

**预期结果**：`beta=0` 时旁瓣约 -13 dB（同矩形窗）；随 `beta` 增大，旁瓣单调下降（`beta=8.6` 约 -58 dB，接近 Blackman），但主瓣相应变宽。具体 dB 数值依赖 FFT 补零长度，**精确值待本地验证**，但「beta↑ → 旁瓣↓、主瓣↑」的趋势是确定的。

#### 4.3.5 小练习与答案

1. **问**：Kaiser 窗为什么不用 `_general_cosine_impl`？
   **答**：因为它是贝塞尔函数而非余弦多项式，数学形式不属于广义余弦族；但它仍能复用对称性三件套（`_len_guards/_extend/_truncate`），说明这三个工具抽象层次恰到好处——只管「采样点取法」，不管「窗形状公式」。

2. **问**：`beta=0` 时 `kaiser` 退化成什么？
   **答**：`i0(0)=1`，于是 `w(n)=1/1=1`，退化为矩形窗。这与对照表第一行一致。

---

### 4.4 `dpss`：能量最集中的 Slepian 序列

#### 4.4.1 概念说明

离散长球序列（DPSS，又称 Slepian 序列）回答一个理论问题：**在所有长度为 M 的窗里，哪一个能把最多的能量集中在某个给定频带 \([-W, W]\) 内？** 这个「能量最集中」的窗被证明是某个矩阵的**最大特征向量**，前 \(2NW\) 个特征向量（按特征值降序）都具有良好的集中性，常用于**多窗谱估计（multitaper）**。

`dpss` 的关键实现技巧是：不直接构造那个稠密的浓度矩阵，而是利用 Slepian / Percival-Walden 给出的等价**对称三对角（tridiagonal）矩阵**——它的特征向量与原问题相同，但因为是三对角，可以用 `scipy.linalg.eigh_tridiagonal` 快速只求最大的几个特征值，复杂度远低于一般特征值分解。

参数 `NW` 是「标准化半带宽」：\(2NW = BW/f_0\)。`Kmax` 指定要返回几个 taper（`None` 时只返回最优的一个）。

#### 4.4.2 核心流程

```
输入: M, NW, Kmax=None, sym=True, norm=None, return_ratios=False
  ├─ 参数校验（NW<M/2, 0<Kmax<=M, norm ∈ {2,'approximate','subsample'}）
  ├─ M, needs_trunc = _extend(M, sym)
  ├─ W = NW / M;  nidx = arange(M)
  ├─ 构造三对角矩阵的对角 d 与次对角 e：
  │     d[t] = ((M-1-2t)/2)^2 * cos(2πW)
  │     e[t] = t(M-t)/2
  ├─ w, windows = eigh_tridiagonal(d, e, select='i', select_range=(M-Kmax, M-1))
  ├─ 翻转成降序；按约定修正各 taper 的符号（偶阶均值>0、奇阶首叶>0）
  ├─ (可选) 用自相关法 _fftautocorr 计算浓度比 ratios
  ├─ 归一化（norm='approximate' 用 M^2/(M^2+NW) 修正偶数 M）
  └─ _truncate 截断 + singleton 处理 → 返回
```

三对角构造的直觉：主对角 `d[t]` 与位置 \(t\) 的平方乘以 \(\cos(2\pi W)\) 有关，次对角 `e[t]=t(M-t)/2` 在两端为 0、中间最大——这种「中间耦合强、两端弱」的结构正是产生「钟形、能量集中」特征向量的原因。

#### 4.4.3 源码精读

[windows/_windows.py:2189-2196](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/windows/_windows.py#L2189-L2196) —— 三对角矩阵的构造与求解。`d` 是主对角、`e` 是次对角；`eigh_tridiagonal(..., select='i', select_range=(M-Kmax, M-1))` 只算最大的 `Kmax` 个特征对，避免全量分解。注释（2183-2188 行）解释了为什么可以用这个等价三对角系统代替原始浓度问题。

[windows/_windows.py:2226-2237](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/windows/_windows.py#L2226-L2237) —— 归一化处理。`norm='approximate'` 用解析修正 `M^2/(M^2+NW)`（快），`norm='subsample'` 用 FFT 频域亚采样平移求修正（更准但慢），`norm=2` 直接用 l2 范数。

[windows/_windows.py:2348-2357](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/windows/_windows.py#L2348-L2357) —— `_fftautocorr`：用 FFT 计算自相关（`rfft` 后乘共轭再 `irfft`），比直接卷积快，用于在 `return_ratios=True` 时估计各 taper 的能量浓度比。

> `dpss` 同样复用了 `_len_guards/_extend/_truncate`（如 2167、2238 行），再一次印证 4.1 抽象的通用性——无论窗来自余弦叠加、贝塞尔函数还是特征向量，对称性处理都是同一套。

#### 4.4.4 代码实践

**实践目标**：取一组 DPSS taper，验证前几个的浓度比都接近 1、且彼此正交。

**操作步骤**：

```python
# 示例代码：观察 DPSS 多窗
import numpy as np
from scipy.signal.windows import dpss

M, NW, Kmax = 256, 2.5, 4
v, ratios = dpss(M, NW, Kmax, return_ratios=True)
print("浓度比:", np.round(ratios, 4))
# 验证两两正交
gram = v @ v.T
print("Gram 阵非对角最大值:", np.round(np.abs(gram - np.eye(Kmax)).max(), 4))
```

**预期结果**：前 \(2NW\approx 5\) 个 taper 的浓度比都接近 1（这里取了 4 个），Gram 矩阵接近单位阵说明它们正交。具体数值**待本地验证**。

#### 4.4.5 小练习与答案

1. **问**：为什么 `dpss` 用三对角矩阵而不是原始的浓度矩阵？
   **答**：等价但更稀疏，能用 `eigh_tridiagonal` 只求最大几个特征对，复杂度与内存都远优于稠密分解。

2. **问**：`NW` 越大，能得到的「好 taper」数量怎么变？
   **答**：约等于 \(2NW\) 个——这是 Slepian 的核心结论，也是 multitaper 方法取窗数的依据。

---

## 5. 综合实践

把本讲四个最小模块串起来：测量并对比 **Hann、Hamming、Blackman、Kaiser(β=8)** 四个窗的**频域主瓣宽度**与**最高旁瓣电平**，并解释 `sym=True` 与 `sym=False` 的差别。

**实践步骤**：

```python
# 示例代码：四窗频域对比（综合实践）
import numpy as np
from scipy.signal.windows import hann, hamming, blackman, kaiser
from scipy.fft import fft, fftshift

M = 128
Nfft = 16384                      # 大量补零，便于看清主瓣与旁瓣
windows = {
    'hann(0.5)':    hann(M, sym=False),
    'hamming(0.54)':hamming(M, sym=False),
    'blackman':     blackman(M, sym=False),
    'kaiser(8)':    kaiser(M, 8, sym=False),
}

def measure(w):
    A = np.abs(fftshift(fft(w, Nfft)))
    A /= A.max()
    dB = 20*np.log10(np.maximum(A, 1e-12))
    half = Nfft // 2
    # 主瓣宽度：找主瓣两侧降到 -3dB 的两点间距 (单位：归一化频率)
    above3 = np.where(dB[half:] >= -3)[0]
    mainlobe_w = (above3[-1] - above3[0]) / Nfft
    # 最高旁瓣：主瓣之外 (跳过前若干格) 的最大值
    sidelobe = dB[half + 2*above3[-1]:].max()
    return mainlobe_w, sidelobe

for name, w in windows.items():
    ml, sl = measure(w)
    print(f"{name:16s} 主瓣宽≈{ml*Nfft:5.1f} 格  最高旁瓣≈{sl:6.1f} dB")
```

**需要观察与解释**：

1. **旁瓣排序**：通常 Hamming < Hann 在「最近旁瓣」上更低（Hamming 约 -42 dB，Hann 约 -31 dB），Blackman 与 Kaiser(β=8) 都能到约 -58 dB。精确值依赖 M 与补零长度，**待本地验证**。
2. **主瓣排序**：旁瓣越低的窗主瓣越宽（Blackman/Kaiser 主瓣明显比 Hann/Hamming 宽）——这正是「分辨率 vs 泄漏」的不可兼得。
3. **`sym` 差别**：把上面任一窗的 `sym=False` 换成 `sym=True` 重测，会发现主瓣/旁瓣的**数值略有不同**。原因是 `sym=True` 的采样分母是 `M-1`、`sym=False` 是 `M`（见 4.1 的「加一再去尾」）。谱分析应选 `sym=False`（DFT 隐含周期延拓），FIR 设计应选 `sym=True`（线性相位要求严格对称）。
4. **为什么 Hann/Hamming 代码这么短**：回看 4.2，它们只是给 `_general_cosine_impl` 喂了不同系数——这次实践让你从「数值结果」一侧再次确认了这一点。

## 6. 本讲小结

- `_len_guards/_extend/_truncate` 是一套**与窗形状无关的对称性模板**：`_extend` 决定是否多算一个点，`_truncate` 决定是否砍掉，靠布尔值 `needs_trunc` 串联，统一了所有窗的 `sym=True/False` 处理。
- `general_cosine` + 内核 `_general_cosine_impl` 是**广义余弦窗族的公共基底**；Hann、Hamming、Blackman、Nuttall、Flattop 等都只是「填系数 + 调内核」，系数采用「以原点为中心、全正」的 Nuttall 约定。
- `hann`/`hamming` 进一步共享 `general_hamming`（参数 `alpha`，`a=[alpha, 1-alpha]`）；`hann` 即 `alpha=0.5`、`hamming` 即 `alpha=0.54`。
- `kaiser` 用零阶修正贝塞尔函数 \(I_0\) 构造，单个 `beta` 参数即可在主瓣宽度与旁瓣电平间连续权衡，且是 DPSS 的解析近似。
- `dpss` 把「能量最集中」转化为**对称三对角特征值问题**，用 `eigh_tridiagonal` 只求最大几个特征对；前 \(2NW\) 个 taper 浓度高且两两正交，支撑 multitaper 谱估计。
- 阅读本文件的最大收获：**识别出被复用的抽象**——对称性三件套与余弦内核，它们让 26 个窗的实现保持精简。

## 7. 下一步学习建议

1. 回到 [u2-l3](u2-l3-windows-subpackage.md)，结合本讲重新看 `get_window`：现在你能解释「字符串名/元组参数」是如何最终落到 `_general_cosine_impl` 或 `kaiser` 这类内核上的。
2. 进入单元 4 学 **数字滤波**：FIR 滤波器设计（`firwin`）会大量用到窗函数，本讲对 `sym=True`（滤波用）的理解将直接派上用场。
3. 进入单元 7 学 **频谱分析**：`welch`/`spectrogram` 内部分窗、以及 multitaper 思想，都与本讲的 `sym=False`（谱分析用）和 `dpss` 直接相关。
4. 进阶阅读 `_windows.py` 中未覆盖的窗：`chebwin`（Dolph-Chebyshev，用另一种方式做等纹波旁瓣）、`tukey`（余弦锥形）、`kaiser_bessel_derived`（完美重构滤波器组用），它们的实现也大量复用了本讲讲过的抽象。
