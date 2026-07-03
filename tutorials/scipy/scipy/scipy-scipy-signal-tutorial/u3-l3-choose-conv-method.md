# choose_conv_method：直接法与 FFT 法的自动选择

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `convolve` / `correlate` 在 `method='auto'`（默认）时，是如何在「直接法」与「FFT 法」之间做选择的。
- 理解 `_conv_ops` 如何把两种方法的「算术运算量」抽象成可比较的标量。
- 掌握 `_fftconv_faster` 那张「经验常数表」背后的判别公式，以及为什么需要偏置项 `offset`。
- 认识 `measure=True` 这条「实测回退」路径，以及 `_timeit_fast` 的自适应计时逻辑。
- 理解为什么 `choose_conv_method` 有时即使 FFT 更快也会强制返回 `'direct'`（整型精度保护、布尔保护）。

本讲承接上一讲（u3-l2 `fftconvolve` / `oaconvolve`）。上一讲解决了「FFT 法怎么算、什么时候比分块重叠相加更快」；本讲解决的是更上游的一个问题：当用户什么都不指定（`method='auto'`）时，到底该把请求路由给直接法还是 FFT 法。

## 2. 前置知识

在进入源码前，先用直觉建立一个判别框架。

**复杂度直觉。** 设两路信号长度分别为 \(n\) 与 \(k\)（先看一维）。

- 直接法卷积：输出每个点要把核与信号逐点相乘再求和，运算量正比于 \(O(nk)\)。
- FFT 法卷积：要把两路信号都补零到 \(N = n + k - 1\)，做 2 次正向 FFT、1 次逆向 FFT，再逐点相乘，运算量正比于 \(O(N \log N)\)。

当 \(n, k\) 都很小时，直接法的常数因子小、没有 FFT 的固定开销，通常更快；当 \(n, k\) 较大时，\(O(nk)\) 的二次增长会被 \(O(N \log N)\) 的拟线性增长反超，FFT 法更快。**自动选择的目标，就是把这个交叉点找出来。**

**为什么不能只看复杂度。** 同样的运算量，直接法的「一次乘加」和 FFT 的「一次蝶形运算」在 CPU 上花费的真实时间不同，还受缓存、向量化、内存带宽影响。因此 SciPy 的做法是：先用 `_conv_ops` 估出运算量（纯算术、与硬件无关），再用一张**在真实硬件上标定过的经验常数表** `_fftconv_faster` 把运算量换算成「预测耗时」并比较。这就是一个「运算量模型 + 经验拟合常数」的两段式判别器。

**两条路径。** `choose_conv_method` 有两条决策路径：

- `measure=False`（默认）：只做上面的算术预测，**不真正计算**，开销极小。这是 `convolve`/`correlate` 在 `method='auto'` 时实际走的路径。
- `measure=True`：用 `_timeit_fast` 把两种方法都**真正跑一遍**计时，取更快者。更精确，但有计算开销，适合「同一 dtype/shape 要重复卷积很多次」时离线选定 `method`。

## 3. 本讲源码地图

本讲全部源码集中在一个文件里：

| 文件 | 作用 |
| --- | --- |
| [_signaltools.py](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/signal/_signaltools.py) | 卷积/相关/滤波的私有实现模块。本讲的四个目标函数 `choose_conv_method`、`_conv_ops`、`_fftconv_faster`、`_timeit_fast`，以及它们的调用方 `convolve` 都在此文件中。 |

辅助函数（同文件，简要引用）：

- `_numeric_arrays`：判断数组是否属于指定 dtype 种类（bool / 无符号整 / 整 / 浮 / 复），用于精度保护分支。
- `_prod`：即 `math.prod`，计算各维尺寸的乘积（多维运算量估算用）。

## 4. 核心概念与源码讲解

### 4.1 choose_conv_method：自动选择的总入口

#### 4.1.1 概念说明

`choose_conv_method(in1, in2, mode='full', measure=False)` 是「自动选择」对外的唯一入口。它有两个职责：

1. 被 `convolve` / `correlate` 在 `method='auto'` 时内部调用，决定路由（这是它的主要存在意义）。
2. 供用户**离线**调用：当你要对许多「相同 dtype、相同 shape」的输入反复卷积时，先调一次 `choose_conv_method` 拿到 `method` 字符串，再把该字符串传给后续每次 `convolve`/`correlate`，从而避免每次都重复决策（甚至可以用 `measure=True` 做一次精确标定）。

它返回 `'direct'` 或 `'fft'`；当 `measure=True` 时额外返回一个耗时字典。

#### 4.1.2 核心流程

`choose_conv_method` 的决策是一棵**带保护分支的优先级树**，从上到下短路返回：

```
1. 若 measure=True：
       分别计时 fft、direct 两种方法 → 返回更快者 + times 字典
       （走 _timeit_fast，见 4.4）

2. 整型精度保护：若任一输入是无符号整/整型，且
       max|in1| * max|in2| * min(size) > 2^52 - 1
   → 强制返回 'direct'（FFT 用 float64 中间结果，会丢精度）

3. 布尔保护：若两个输入都是 bool 型
   → 强制返回 'direct'

4. 一般数值（bool/整/浮/复）：调用 _fftconv_faster
       若预测 FFT 更快 → 'fft'，否则 → 'direct'

5. 其它（如 object dtype）→ 'direct'
```

注意第 2、3 步是**正确性保护**，而不是性能选择：即便 FFT 在这里更快，也会被强制走直接法。这正是其 docstring 里那句「There are cases when fftconvolve supports the inputs but this function returns `direct` (e.g., to protect against floating point integer precision)」的含义。

#### 4.1.3 源码精读

决策树的主体在 [_signaltools.py:1365-1390](_signaltools.py#L1365-L1390)，这里逐段说明。

**`measure=True` 实测分支**（更细的计时逻辑见 4.4）：

[_signaltools.py:1365-1372](_signaltools.py#L1365-L1372) — 对 `'fft'` 与 `'direct'` 各跑一次 `convolve`，用 `_timeit_fast` 取耗时，比较后返回更快的方法及耗时字典。注意它**真正调用了 `convolve`**（带 `method=` 参数），所以这条路径有真实计算开销。

**整型精度保护分支**：

[_signaltools.py:1377-1381](_signaltools.py#L1377-L1381) — 这段做的是「最坏情况累加值」上界估计。`fftconvolve` 内部把整型提升为 `float64` 再做 FFT，而 `float64` 的尾数只有 52 位（`np.finfo('float').nmant == 52`）。直接法卷积的任意一个输出点，其精确整数值都不会超过

\[
V_{\max} = \max|in_1| \;\cdot\; \max|in_2| \;\cdot\; \min(\text{size}(in_1), \text{size}(in_2))
\]

一旦 \(V_{\max} > 2^{52} - 1\)，`float64` 就无法精确表示每个整数累加结果，FFT 路径会引入舍入误差，于是强制返回 `'direct'` 以保证整数卷积的**精确性**。`any([... for x in [volume, kernel]])` 表示只要两个输入里有任意一个是整型就启用该检查。

**布尔保护**：

[_signaltools.py:1383-1384](_signaltools.py#L1383-L1384) — `kinds='b'` 即 bool。两个布尔数组做卷积应保持精确的整数计数语义（每个输出是「多少对同时为真」），走 FFT 会先转浮点，故强制直接法。

**一般数值分支**：

[_signaltools.py:1386-1388](_signaltools.py#L1386-L1388) — `_numeric_arrays` 不传 `kinds` 时用默认值 `'buifc'`（bool/unsigned/int/float/complex），即「是数值数组」。此时把真正的快慢判断委托给 `_fftconv_faster`（4.3）。`_fftconv_faster` 返回 `True` 表示预测 FFT 更快 → 返回 `'fft'`。

最后的兜底 [_signaltools.py:1390](_signaltools.py#L1390) 处理 object 等非数值 dtype，返回 `'direct'`。

**调用方**：在 `convolve` 内部，`method='auto'` 仅是一行替换：

[_signaltools.py:1514-1515](_signaltools.py#L1514-L1515) — `if method == 'auto': method = choose_conv_method(volume, kernel, mode=mode)`。`correlate` 同样以 `method='auto'` 为默认，复用同一判别器（相关在内部被翻转化为卷积，见上一单元 u3-l1）。

#### 4.1.4 代码实践

**实践目标**：亲手触发 4.1.2 决策树里的不同分支，确认「保护分支」会覆盖「性能选择」。

**操作步骤**：

```python
import numpy as np
from scipy.signal import choose_conv_method

# (a) 一般浮点：走 _fftconv_faster 性能判别
print(choose_conv_method(np.zeros(50), np.zeros(50), mode='full'))   # 多半 'direct'
print(choose_conv_method(np.zeros(2000), np.zeros(2000), mode='full'))  # 多半 'fft'

# (b) 布尔保护：即便很长也强制 'direct'
xb = np.zeros(2000, dtype=bool); hb = np.zeros(2000, dtype=bool)
print(choose_conv_method(xb, hb, mode='full'))   # 'direct'

# (c) 整型精度保护：构造一个会超出 2^52-1 的最坏累加上界
xi = np.full(2000, 1 << 30, dtype=np.int64)   # 每点 ~1e9
hi = np.full(2000, 1 << 30, dtype=np.int64)
# V_max = (2^30)^2 * 2000 ≈ 2.27e21 >> 2^52 ≈ 4.5e15
print(choose_conv_method(xi, hi, mode='full'))  # 'direct'（精度保护）
```

**需要观察的现象**：(a) 中短信号选 `direct`、长信号选 `fft`；(b)(c) 中即使输入很长、FFT 本应更快，结果仍是 `direct`。

**预期结果**：保护分支优先于性能判别——这正是「自动选择」既要快、又要对的体现。

（具体切换长度阈值依赖 `_fftconv_faster` 的常数，见 4.3 与综合实践；本步骤只需确认分支方向正确。）

#### 4.1.5 小练习与答案

**练习 1**：为什么整型精度保护要用 `min(size(in1), size(in2))` 而不是 `max`？

**参考答案**：直接法卷积在 `valid`/`full` 输出点上的最大可能重叠加点数，受限于较短的那个输入（核或信号）的尺寸——任意一个输出点最多与较短数组完全重叠一次。因此最坏累加次数是 `min(size)`，用它做上界既安全又不过度保守。

**练习 2**：`measure=True` 与默认 `measure=False` 各自的适用场景是什么？

**参考答案**：默认 `measure=False` 走纯算术预测，零额外计算，适合每次卷积都可能改变 shape 的一次性调用（即 `method='auto'` 的默认行为）。`measure=True` 要真正把两种方法各跑一遍，开销大但更贴合本机硬件，适合「同一 shape/dtype 要卷积成百上千次」时离线标定一次、复用结果。

---

### 4.2 _conv_ops：估算两种方法的运算量

#### 4.2.1 概念说明

`_conv_ops(x_shape, h_shape, mode)` 是运算量抽象层。它不关心硬件、不做计时，只回答一个问题：「给定两个输入的形状和 mode，直接法和 FFT 法各需要多少次运算？」返回一个二元组 `(fft_ops, direct_ops)`。

它的存在让上层判别器可以把「快慢比较」降维成「两个标量的线性组合比较」。

#### 4.2.2 核心流程

运算量分一维与多维两套公式。

**FFT 法运算量**（与维数无关，统一公式）：

输出全长的各维尺寸为 \(o_i = n_i + k_i - 1\)，总点数 \(N = \prod_i o_i\)。`fftconvolve` 内部做 2 次正向 FFT、1 次逆向 FFT（共 3 次），每次 FFT 复杂度为 \(O(N\log N)\)，故

\[
\text{fft\_ops} = 3\,N\log N, \qquad N = \prod_i (n_i + k_i - 1)
\]

这里 `np.log` 是自然对数。注意这只是个**相对量纲**，并非严格「浮点操作数」（真实的 `next_fast_len` 优化、实数走 `rfftn` 等并未在此体现），但对判别而言够用。

**直接法运算量**：

- 一维 `full`：\(s_1 s_2\)
- 一维 `valid`：\((s_2 - s_1 + 1)s_1\)（当 \(s_2 \ge s_1\)，否则对称交换）
- 一维 `same`：若 \(s_1 < s_2\) 为 \(s_1 s_2\)，否则 \(s_1 s_2 - \lfloor s_2/2\rfloor\cdot\lceil s_2/2\rceil\)（扣除 `same` 模式裁掉的两端不必计算的部分）
- 多维 `full`/`valid`：\(\min(\prod n_i,\ \prod k_i)\cdot\prod o_i\)——较短那个数组在每个输出点滑过的乘加数，乘以输出点总数
- 多维 `same`：\((\prod n_i)(\prod k_i)\)（保守上界）

#### 4.2.3 源码精读

[_signaltools.py:1095-1135](_signaltools.py#L1095-L1135) 是完整实现。关键点：

[_signaltools.py:1104-1112](_signaltools.py#L1104-L1112) — 先按 `mode` 算出 `out_shape`（`full`: 逐维 \(n+k-1\)；`valid`: 逐维 \(|n-k|+1\)；`same`: 取 `x_shape`）。

[_signaltools.py:1115-1130](_signaltools.py#L1115-L1130) — 一维与多维分别套用上文的直接法公式。注意一维 `same` 的那行 [_signaltools.py:1122-1123](_signaltools.py#L1122-L1123) 用了「较短/较长」分支，避免对裁剪掉的输出端重复计数。

[_signaltools.py:1132-1135](_signaltools.py#L1132-L1135) — FFT 运算量统一由「全输出形状」的乘积 \(N\) 与 `3*N*np.log(N)` 给出，注释 `# 3 separate FFTs of size full_out_shape` 直白说明了「3 次 FFT」的来历。最后 `return fft_ops, direct_ops`。

#### 4.2.4 代码实践

**实践目标**：用 `_conv_ops` 观察「直接法运算量随尺寸二次增长、FFT 运算量拟线性增长」的交叉趋势。

**操作步骤**：

```python
import numpy as np
import scipy.signal._signaltools as st

for n in [50, 200, 800, 3200]:
    fft_ops, direct_ops = st._conv_ops((n,), (n,), 'full')
    ratio = direct_ops / fft_ops
    print(f"n={n:5d}  fft_ops={fft_ops:12.1f}  direct_ops={direct_ops:10.0f}  direct/fft={ratio:5.2f}")
```

**需要观察的现象**：随着 `n` 增大，`direct/fft` 比值单调上升——小尺寸时直接法运算量更小，大尺寸时 FFT 运算量更小。

**预期结果**：比值从小于 1（直接法运算量更少）增长到大于 1（FFT 运算量更少），交叉点在数百量级。（精确的「快慢」交叉点还要乘以 4.3 的经验常数，运算量交叉只是其近似。）

#### 4.2.5 小练习与答案

**练习 1**：为什么多维 `full` 的直接法运算量是 \(\min(\prod n_i, \prod k_i)\cdot\prod o_i\)，而不是 \((\prod n_i)(\prod k_i)\)？

**参考答案**：直接卷积可写成「把较短数组在较长数组上滑动」，每个输出点只需较短数组那么多次乘加，乘以输出点总数即可。\((\prod n_i)(\prod k_i)\) 会重复计算，是上界而非紧凑估计；用 `min` 既准确又能让判别器对「一大一小」的卷积（如小核卷大图）正确倾向直接法。

**练习 2**：`fft_ops` 里为什么是 `3` 而不是 `2`？

**参考答案**：频域卷积需要：正向 FFT(signal) + 正向 FFT(kernel) + 逐点相乘 + 逆向 FFT(乘积)。计入的 3 次 FFT 对应「两次正向 + 一次逆向」；逐点相乘 \(O(N)\) 相对 \(O(N\log N)\) 是低阶项，未单列。

---

### 4.3 _fftconv_faster：基于经验常数的判别公式

#### 4.3.1 概念说明

`_conv_ops` 给出的是「运算量」，但 1 次 FFT 蝶形运算与 1 次直接法乘加的真实耗时不同。`_fftconv_faster(x, h, mode)` 的作用是把运算量换算成**预测耗时**再比较。

它的核心是一张「经验常数表」：这组常数是在一台 Amazon EC2 `r5a.2xlarge` 机器上，用大量随机形状做线性回归拟合得到的（详见其 docstring 引用的 [PR 11031](https://github.com/scipy/scipy/pull/11031) 与 `choose_conv_method` 的 Notes）。拟合模型是线性的：

\[
\text{耗时} \approx C\cdot\text{ops} + C_{\text{offset}}
\]

其中 \(C\) 是「每次运算的秒数」（含该方法的常数因子），\(C_{\text{offset}}\) 是固定开销（建数组、调用栈等）。

#### 4.3.2 核心流程

判别规则很简洁——预测 FFT 更快，当且仅当

\[
C_{\text{fft}}\cdot\text{fft\_ops} \;<\; C_{\text{direct}}\cdot\text{direct\_ops} + C_{\text{offset}}
\]

常数按 `(ndim, mode)` 组合查表，一维与多维各有一套，`same` 模式在一维下还按 `h.size <= x.size` 再分两种情况。偏置项 `offset` 一律是微小的负数（一维 `-1e-3`、多维 `-1e-4`），表示该路径有少量「固定启动开销」需要被扣除后才值得切换。

**为什么用预测耗时而非纯运算量比值？** 因为直接法与 FFT 法的「每运算耗时」差近一个数量级（看常数：FFT 的 \(C\) 约 \(2\times10^{-9}\)，直接法约 \(2\times10^{-10}\)，FFT 每运算更贵）。只比运算量会高估 FFT 的优势；引入经验常数把「每运算贵多少」量化进去，判别才准。

**理论交叉点（一维 full，等长 \(s_1=s_2=n\)）。** 代入常数 \(C_{\text{fft}}=1.7649\times10^{-9}\)、\(C_{\text{direct}}=2.1415\times10^{-10}\)、\(N=2n-1\)，忽略偏置：

\[
1.7649\times10^{-9}\cdot 3(2n-1)\ln(2n-1) \;<\; 2.1415\times10^{-10}\cdot n^{2}
\]

化简得 \(\displaystyle \frac{n}{\ln(2n)} \;\gtrsim\; \frac{3\times 1.7649\times10^{-9}}{2.1415\times10^{-10}} \approx 49.4\)。手算求解 \(n/\ln(2n)\approx 49.4\) 得 \(n\approx 320\)（这是从源码常数手工推导的估计值，精确切换点请在本地按综合实践实测确认）。也就是说，等长一维信号在长度约几百量级时 FFT 开始反超——这与 `_conv_ops` 的运算量交叉趋势一致，但因 FFT「每运算更贵」，阈值被推高了一些。

#### 4.3.3 源码精读

[_signaltools.py:1138-1177](_signaltools.py#L1138-L1177) 是完整实现。

[_signaltools.py:1163](_signaltools.py#L1163) — 调 `_conv_ops` 拿到 `fft_ops, direct_ops`。

[_signaltools.py:1164-1175](_signaltools.py#L1164-L1175) — 这段嵌套字典就是经验常数表。外层 `if x.ndim == 1 else ...` 区分一维/多维；内层按 `'valid'/'full'/'same'` 取三元组 `(O_fft, O_direct, offset)`。注意一维 `same` 那项 [_signaltools.py:1168-1170](_signaltools.py#L1168-L1170) 是一个条件表达式，按 `h.size <= x.size` 选不同常数（甚至 offset 也不同，为 `-1e-5`），因为 `same` 模式下核比信号大或小，裁剪方向不同、耗时模型也不同。`offset` 在 [_signaltools.py:1164](_signaltools.py#L1164) 统一定义为 `-1e-3 if x.ndim == 1 else -1e-4`。

[_signaltools.py:1176-1177](_signaltools.py#L1176-L1177) — 解包常数并直接返回判别布尔值 `O_fft * fft_ops < O_direct * direct_ops + O_offset`。`True` 即「预测 FFT 更快」。

#### 4.3.4 代码实践

**实践目标**：验证 `_fftconv_faster` 的判别结果与 `choose_conv_method`（默认 `measure=False`）完全一致——因为后者内部正是调用前者。

**操作步骤**：

```python
import numpy as np
import scipy.signal._signaltools as st
from scipy.signal import choose_conv_method

for n in [50, 200, 320, 500, 1000]:
    x = np.zeros(n); h = np.zeros(n)
    chosen = choose_conv_method(x, h, mode='full')          # 内部走 _fftconv_faster
    faster = st._fftconv_faster(x, h, 'full')               # True => 预测 fft 更快
    print(f"n={n:5d}  choose={chosen:6s}  fft_predicted_faster={faster}")
```

**需要观察的现象**：每一行里，`choose='fft'` 当且仅当 `fft_predicted_faster=True`，二者永不矛盾。

**预期结果**：在 \(n\) 从小到大扫描时，`_fftconv_faster` 在约 \(n\approx 320\) 附近由 `False` 翻转为 `True`，`choose_conv_method` 同步切换 `direct → fft`。这正是 4.3.2 手算的交叉点。

#### 4.3.5 小练习与答案

**练习 1**：常数表里 `offset` 为什么是**负**数？把它设成 0 会怎样？

**参考答案**：`offset` 代表切换到某方法时的固定启动开销（建临时数组、Python/C 调用边界等）。判别式写成 `O_fft*fft_ops < O_direct*direct_ops + offset`，把负的 `offset` 放在直接法一侧，等价于「直接法要再多省一点固定开销才被保持」，即对 FFT 略微保守。若设为 0，会在尺寸刚好处于交叉点附近时频繁切换、甚至误判，实测稳定性下降。

**练习 2**：一维与多维用了**不同的**常数表，但 docstring 说 2D 结果能推广到 3D/4D。这两件事矛盾吗？

**参考答案**：不矛盾。一维实现（`fftconvolve` 一维分支、可能走 `np.convolve` 快通道）与多维实现（`fftn`/`rfftn`）的常数因子不同，所以一维单独一张表。而所有 ≥2 维的实现走的是同一套 N 维 FFT 内核，常数因子相近，因此 2D 标定的常数可外推到更高维。

---

### 4.4 _timeit_fast：measure=True 的实测回退

#### 4.4.1 概念说明

经验常数表是基于某台 EC2 机器标定的，换到你的硬件未必最优。`measure=True` 提供了一条**实测**路径：把两种方法都真正跑一遍，取更快者。承担「真正跑一遍并计时」的，就是 `_timeit_fast`。

它是 `timeit.Timer` 的一个轻量、自适应包装：比标准 `timeit` 更快（迭代次数更少），精度略低但对「选哪个方法」这种粗粒度决策足够。

#### 4.4.2 核心流程

`_timeit_fast(stmt, setup, repeat=3)` 的自适应逻辑：

1. 用 `timeit.Timer(stmt, setup)` 建计时器。
2. 以 `number = 10**p`（p=0..9）几何级数增长调用次数，每次 `timer.timeit(number)`，直到单轮总耗时 ≥ 0.5ms（即 `5e-3/10`）就停——目的是找到一个「单轮够长、噪声可控」的调用次数。
3. 若该轮耗时已经 > 1 秒（宏观量级），直接用它，不再重复（太慢的函数没必要多次计时）。
4. 否则把 `number` 再 ×10，重复 `repeat=3` 轮，取**最小值** `best`（最小值最能反映无干扰下的真实速度）。
5. 返回 `best / number`，即「平均每次调用的秒数」。

这套「先定 number、再取 min」的策略，正是 IPython `%timeit` 的简化版：自动放大循环次数以压平噪声，取最小值以剔除操作系统调度等正向干扰。

#### 4.4.3 源码精读

[_signaltools.py:1216-1248](_signaltools.py#L1216-L1248) 是完整实现。

[_signaltools.py:1230](_signaltools.py#L1230) — 建计时器，`stmt` 既可以是字符串语句，也可以是 callable（`timeit.Timer` 原生支持 callable）。

[_signaltools.py:1233-1238](_signaltools.py#L1233-L1238) — 几何级数搜索合适的 `number`，阈值 `5e-3 / 10`（0.5ms）是为了让「最终 ×10 后的那一轮」落在约 5ms 量级，兼顾噪声与总耗时。

[_signaltools.py:1239-1245](_signaltools.py#L1239-L1245) — 慢函数（>1s）直接取单次；快函数则 `number *= 10` 后跑 `repeat=3` 轮取 `min`。

[_signaltools.py:1247-1248](_signaltools.py#L1247-L1248) — 归一化为「每次调用秒数」返回。

`choose_conv_method` 在 `measure=True` 分支里正是用 lambda 把「一次 `convolve` 调用」包成 callable 传给它，见 [_signaltools.py:1366-1371](_signaltools.py#L1366-L1371)：`times[method] = _timeit_fast(lambda: convolve(volume, kernel, mode=mode, method=method))`。

#### 4.4.4 代码实践

**实践目标**：用 `measure=True` 实测两种方法的真实耗时，与 `measure=False` 的算术预测对比，体会「预测 vs 实测」的差异。

**操作步骤**：

```python
import numpy as np
from scipy.signal import choose_conv_method

rng = np.random.default_rng(0)
for n in [200, 500, 1000]:
    x = rng.standard_normal(n); h = rng.standard_normal(n)
    pred = choose_conv_method(x, h, mode='full')                      # 算术预测
    chosen, times = choose_conv_method(x, h, mode='full', measure=True)  # 实测
    print(f"n={n:5d}  pred={pred:6s}  measured={chosen:6s}  "
          f"t_fft={times['fft']:.2e}s  t_direct={times['direct']:.2e}s")
```

**需要观察的现象**：`pred`（基于 EC2 常数）与 `measured`（你的本机实测）在大多数长度上吻合，但靠近交叉点（约数百量级）时可能出现不一致——这正是 docstring 所说「1D 信号约 85% 准确、对 1~10ms 区间的直接卷积最不准」的体现。

**预期结果**：本机越接近标定机器，吻合度越高；偏离时以 `measured` 为准。这也说明了 `measure=True` 的价值：当预测不可靠时，实测能给出本机最优解。（具体数值待本地验证。）

#### 4.4.5 小练习与答案

**练习 1**：`_timeit_fast` 为什么取 `min(repeat)` 而不是 `mean`？

**参考答案**：计时噪声几乎都是**正向**的（操作系统调度、后台进程、缓存未命中等只会让某次运行变慢，不会让它比「无干扰理论值」更快）。因此最小值最接近「真实硬件能力」，而均值会被偶发慢轮次拉高。这是 `%timeit`/`perf_counter` 类基准测试的通用约定。

**练习 2**：`measure=True` 会在每次 `convolve(..., method='auto')` 调用时触发吗？为什么？

**参考答案**：不会。`convolve`/`correlate` 在 `method='auto'` 时调的是 `choose_conv_method(volume, kernel, mode=mode)`，**不传 `measure`**（默认 `False`），走纯算术预测。`measure=True` 是留给用户**离线显式调用** `choose_conv_method` 的——否则每次卷积都要把两种方法各跑一遍，得不偿失。

---

## 5. 综合实践

**任务**：在递增的输入长度下，绘制 `choose_conv_method` 的 direct/fft 选择曲线，标出切换分界点，并对照 `_conv_ops` 的运算量比值与 `_fftconv_faster` 的预测，验证「理论阈值」。

**操作步骤**：

```python
import numpy as np
import scipy.signal._signaltools as st
from scipy.signal import choose_conv_method

# 等长一维 full 模式扫描
ns = list(range(100, 1001, 25))
rows = []
for n in ns:
    x = np.zeros(n); h = np.zeros(n)
    method = choose_conv_method(x, h, mode='full')          # 默认 measure=False
    fft_ops, direct_ops = st._conv_ops((n,), (n,), 'full')
    faster = st._fftconv_faster(x, h, 'full')               # 与 choose 内部一致
    rows.append((n, method, fft_ops, direct_ops, faster))

# 找切换点
boundary = next((n for n, m, *_ in rows if m == 'fft'), None)
print("切换分界点(首个 fft): n =", boundary)
print("手算估计: n ≈ 320（来自 4.3.2 的 n/ln(2n)≈49.4）")

# 打印交叉附近几行
print(f"{'n':>5} {'method':>8} {'direct/fft_ops':>15} {'pred_fft_faster':>16}")
for n, m, fo, do, fa in rows:
    if 250 <= n <= 400:
        print(f"{n:5d} {m:>8} {do/fo:15.3f} {str(fa):>16}")
```

**需要观察的现象**：

1. `method` 在某个长度附近由 `direct` 翻转为 `fft`，且该翻转点 `n` 就是 `pred_fft_faster` 由 `False` 变 `True` 的点（二者严格同步，因为同源）。
2. `direct/fft_ops` 比值在该点附近跨过某个阈值——但**不是**跨过 1，而是跨过「考虑每运算耗时差异后的等效阈值」（见 4.3 的常数比）。
3. 实测切换点应落在手算估计 \(n\approx 320\) 附近。

**进阶（可选）**：把上面的 `choose_conv_method(...)` 换成 `measure=True` 版本，比较「算术预测切换点」与「本机实测切换点」的差距；差距越大，说明你的硬件越偏离 EC2 标定机，此时应考虑用 `measure=True` 离线标定一次、复用 `method` 字符串。

**预期结果**：三条曲线（运算量比、`_fftconv_faster` 布尔、`choose_conv_method` 字符串）在同一长度附近同步翻转，验证了「运算量模型 → 经验常数换算 → 自动选择」这条判别链的自洽性。具体切换长度待本地验证。

## 6. 本讲小结

- `convolve` / `correlate` 默认 `method='auto'`，内部仅用一行 `choose_conv_method(...)` 把请求路由给直接法或 FFT 法。
- `choose_conv_method` 是一棵带**保护分支**的决策树：`measure=True` 实测优先；否则依次做整型精度保护（\(V_{\max}>2^{52}-1\)）、布尔保护，最后才把一般数值交给 `_fftconv_faster` 做性能判别。
- `_conv_ops` 是与硬件无关的运算量抽象层，FFT 法 \(\text{fft\_ops}=3N\log N\)、直接法按维数与 mode 分别给出紧凑估计。
- `_fftconv_faster` 用一张在 EC2 机器上回归标定的**经验常数表**把运算量换算成预测耗时，判别式为 \(C_{\text{fft}}\cdot\text{fft\_ops}<C_{\text{direct}}\cdot\text{direct\_ops}+C_{\text{offset}}\)；等长一维 full 的理论交叉点手算约为 \(n\approx 320\)。
- `_timeit_fast` 是 `measure=True` 的自适应计时器（几何级数定 `number`、`repeat` 取 `min`），提供硬件相关的实测回退，适合离线标定复用。
- 自动选择既要「快」（性能判别）又要「对」（精度/布尔保护），二者优先级不同：保护分支会覆盖性能判别。

## 7. 下一步学习建议

- **横向**：回到调用方，对比 `convolve` 在 `method` 三种取值（`'auto'`/`'direct'`/`'fft'`）下的分发逻辑（[_signaltools.py:1514-1539](_signaltools.py#L1514-L1539)），理解 `'fft'` 分支里对整型输出的 `xp.round` 与 NAN/INF 告警——它们与本讲的整型精度保护是一脉相承的「数值正确性」考量。
- **纵向（下一单元 u4）**：进入「数字滤波」。`lfilter` 是另一个高频核心能力，其差分方程与初始状态 `zi` 是理解分段滤波、`filtfilt` 零相位滤波的基础。
- **深入阅读**：若对经验常数表的来历感兴趣，可阅读 docstring 引用的 [PR 11031](https://github.com/scipy/scipy/pull/11031)，了解回归拟合的数据集与「95% 概率 < 1.5 倍最优」等统计结论的原始实验设置。
