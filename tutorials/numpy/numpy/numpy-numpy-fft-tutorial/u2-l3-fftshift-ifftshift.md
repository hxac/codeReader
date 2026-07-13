# fftshift 与 ifftshift 频谱居中

## 1. 本讲目标

学完本讲后，你应当能够：

- 解释 `numpy.fft.fftshift` 在做什么：它**不计算任何傅里叶变换**，只是把数组沿指定轴做循环平移（roll），把零频（DC 分量）从索引 0 搬到频谱中心。
- 看懂 `fftshift` 的实现核心：`shift = dim // 2` 再调用 `roll(x, shift, axes)`。
- 理解 `ifftshift` 是 `fftshift` 的**精确逆运算**：它沿同样的轴反向平移相同大小，使 `ifftshift(fftshift(x)) == x` 对任意形状都成立。
- 说清楚为什么偶长度下 `fftshift` 与 `ifftshift` 完全相同，而奇长度下二者相差一个样本。
- 熟练使用 `axes` 参数，只在多维数组的部分轴上做平移。

本讲是纯 Python 层的 helper，**不涉及 C++ 后端**。它承接 u2-l1 建立的「standard 频率排列」与 u2-l2 的「频率箱」概念，为后续可视化频谱、设计滤波器打基础。

## 2. 前置知识

阅读本讲前，请先具备以下概念（在 u2-l1、u2-l2 已建立）：

- **DFT / FFT**：离散傅里叶变换是其数学定义，FFT 是它的快速算法。
- **standard 频率排列**：`np.fft.fft` 的输出并不是按频率从小到大排的，而是把零频放在 `A[0]`，正频率紧随其后，然后是负频率「绕回来」放在数组末尾。直观上像把一根频率轴「对折」后塞进数组。
- **频率箱 / `fftfreq`**：`np.fft.fftfreq(n, d)` 返回与 `fft` 输出逐位置对齐的频率值，排列方式与上面一致。
- **`np.roll`**：NumPy 的循环平移函数，`roll(x, s, axis)` 把 `x` 沿 `axis` 整体移动 `s` 个位置，超出边界的元素从另一端「卷」回来。它保持形状和 dtype 不变。

如果「standard 频率排列」还比较模糊，建议先回头做一遍 u2-l1 的实践（对长度 8 的信号做 `fft` 并标出各段索引），再读本讲。

## 3. 本讲源码地图

本讲只涉及两个真实文件：

| 文件 | 作用 | 是否参与计算 |
| --- | --- | --- |
| [`_helper.py`](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_helper.py) | `fftshift` / `ifftshift` / `fftfreq` / `rfftfreq` 四个 helper 的纯 Python 实现。本讲聚焦其中前两个。 | 纯 Python，不调后端 |
| [`tests/test_helper.py`](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/tests/test_helper.py) | `TestFFTShift` 测试类，用具体数值验证 `fftshift` / `ifftshift` 的定义、可逆性与 `axes` 行为。 | 测试 |

补充：类型存根 [`_helper.pyi`](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_helper.pyi) 用 `@overload` 描述了这两个函数的输入输出类型（保留形状与 dtype），运行时不参与，仅服务于类型检查器。

## 4. 核心概念与源码讲解

### 4.1 为什么需要把零频搬到中心

#### 4.1.1 概念说明

回忆 standard 频率排列：对长度为 \(n\)、采样间距为 \(d\) 的信号做 `fft`，输出数组第 \(k\) 个元素对应的频率是

\[
f_k = \begin{cases} k/(nd) & 0 \le k \le \lfloor n/2 \rfloor \\ (k-n)/(nd) & \lfloor n/2 \rfloor < k \le n-1 \end{cases}
\]

也就是说，输出在数组里是这样排的：

```
[ DC,  正频率递增 ...,  (负)Nyquist,  负频率递增 ... ]
   ↑                                  ↑
索引 0                            索引 n-1
```

这种排法对算法很友好（FFT 的蝶形网络天然产生它），但对**人眼不友好**：我们直觉上希望频谱是「从最负的频率排到最正的频率、零频在正中间」，就像一条对称的钟形曲线。`fftshift` 就是干这件事的搬运工——它把上面这段「绕回来」的数组重新摆成单调递增、零频居中的样子。

一个关键认知：`fftshift` **不改变任何数值，只改变它们的位置**。它甚至不知道你的数组是不是真的频谱——你拿任何数组进去，它都按同样规则平移。它纯粹是「为了方便看 / 方便对齐频率轴」而存在的视图工具。

#### 4.1.2 核心流程

把零频搬到中心，等价于把数组「左右两半」对调：

```
原数组（standard）： [左半 L | 右半 R]
fftshift 后       ： [右半 R | 左半 L]
```

这正是循环平移 `roll` 的效果：把数组整体向右推 `n//2` 个位置，原本在末尾的右半 R 就被「卷」到了开头。

> 直觉口诀：**fftshift = 把后半截挪到前面**。

#### 4.1.3 源码精读

`fftshift` 的 docstring 第一句就点明了它的职责——「把零频分量搬到频谱中心」，并强调它对所有列出的轴「交换两个半空间」：

[_helper.py:22-26](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_helper.py#L22-L26) —— 函数文档说明：搬零频到中心，对所有 `axes` 列出的轴交换半空间；并提示「只有当 `len(x)` 为偶数时 `y[0]` 才是 Nyquist 分量」（这个细节在 4.4 节解释）。

docstring 里还给了两个可直接对照的例子，建议读源码时把它们当作「标准答案」记下：

[_helper.py:44-62](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_helper.py#L44-L62) —— 示例：对 `fftfreq(10, 0.1)` 做 `fftshift`，得到从最负频率到最正频率、零频居中的数组；以及对二维数组只在第二轴平移。

#### 4.1.4 代码实践

**目标**：用肉眼确认「standard 排列 → fftshift 后单调居中」这件事。

**步骤**：

1. 生成一段频率轴（它本身就是 standard 排列，方便观察）。
2. 打印原数组与 `fftshift` 后的数组，对照索引。
3. 观察 DC（值为 0 的元素）从索引 0 移到了哪里。

```python
import numpy as np

freqs = np.fft.fftfreq(10, 0.1)   # 长度 10，偶数
print("原数组   :", freqs)
print("fftshift :", np.fft.fftshift(freqs))
```

**预期结果**（与 [_helper.py:46-50](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_helper.py#L46-L50) 中 docstring 给出的「标准答案」一致）：

```
原数组   : [ 0.  1.  2.  3.  4. -5. -4. -3. -2. -1.]
fftshift : [-5. -4. -3. -2. -1.  0.  1.  2.  3.  4.]
```

**观察现象**：值为 `0` 的 DC 从索引 0 跑到了索引 5（即 `10//2`，正中间）；末尾的负频率段 `[-5,-4,-3,-2,-1]` 整体被搬到了开头；最终数组从最负频率单调递增到最正频率。

> 提示：如果本机环境里 `fftfreq` 的 `...` 省略显示看不全，可用 `np.set_printoptions(threshold=10)` 或直接打印 `list(freqs)` 查看全部值。若无法运行，可对照 docstring 的「标准答案」手工核对——本结论为「待本地验证」的运行细节，但数值结论已由源码 docstring 与测试共同保证。

#### 4.1.5 小练习与答案

**练习 1**：长度 8 的 standard 频谱 `A = [DC, f1, f2, f3, Nyq, -f3, -f2, -f1]`，做 `fftshift` 后数组变成什么？DC 落在第几个索引？

**参考答案**：变成 `[Nyq, -f3, -f2, -f1, DC, f1, f2, f3]`，DC 落在索引 4（即 `8//2`）。

**练习 2**：如果对一个普通数组（不是频谱，比如 `[10,20,30,40]`）调用 `fftshift`，会发生什么？会报错吗？

**参考答案**：不会报错，也不会做任何「频域」计算——它只是按 `roll(x, n//2)` 平移，得到 `[30,40,10,20]`。`fftshift` 不检查数组含义，对任何输入都执行同样的搬运。

---

### 4.2 fftshift 的实现：roll 与 shift = dim // 2

#### 4.2.1 概念说明

`fftshift` 的全部计算逻辑只有三步：把输入转成数组、算出每个轴要平移多少、调用 `roll`。其中「算平移量」就是本模块的核心公式：

\[
\text{shift}_d = \left\lfloor \frac{n_d}{2} \right\rfloor
\]

其中 \(n_d\) 是第 \(d\) 个轴的长度，\(\lfloor \cdot \rfloor\) 是整除取下整（Python 的 `//`）。之所以取一半，是因为「交换两个半空间」恰好需要把数组平移半个长度。

`roll` 的数学含义是：

\[
\text{roll}(x, s)[i] = x\big[(i - s) \bmod n\big]
\]

即输出第 \(i\) 个位置取自输入的第 \((i-s) \bmod n\) 个位置。当 \(s = \lfloor n/2 \rfloor\) 时，原本在索引 0 的元素被搬到了索引 \(\lfloor n/2 \rfloor\)——正好是数组中心。

#### 4.2.2 核心流程

`fftshift` 根据 `axes` 的三种形态走不同分支计算 `shift`，但最终都汇聚到一句 `return roll(x, shift, axes)`：

```
输入 x, axes
 ├── axes is None        → axes = 全部轴;  shift = [每个轴 dim//2 的列表]
 ├── axes 是单个整数      → shift = x.shape[axes] // 2          （标量）
 └── axes 是元组/列表     → shift = [x.shape[ax]//2 for ax in axes]（列表）
return roll(x, shift, axes)
```

注意三个分支产生的 `shift` 类型不同：`None` 分支和元组分支产出**列表**（每个轴一个平移量），整数分支产出**单个整数**。这正是 `np.roll` 的接口约定——`shift` 与 `axis` 要么都是标量、要么都是等长序列。

#### 4.2.3 源码精读

函数体本身非常薄，先看导入与分发器：

[_helper.py:5-6](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_helper.py#L5-L6) —— 从 `numpy._core` 导入 `arange / asarray / empty / integer / roll`，其中 `roll` 是本讲的真正主角；`asarray` 把列表输入转成数组。

[_helper.py:15-16](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_helper.py#L15-L16) —— `_fftshift_dispatcher` 只返回 `(x,)`，这是 `__array_function__` 协议的分发器（详见 u2-l4），让第三方数组库能接管 `fftshift`。

再看 `fftshift` 的函数体——三分支算 `shift`，最后一句 `roll`：

[_helper.py:65-74](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_helper.py#L65-L74) —— `fftshift` 主体：`asarray` 转数组；`axes is None` 时把 `axes` 设为全部轴、`shift` 设为各轴 `dim//2` 的列表；整数轴走标量分支；元组轴走列表分支；最后 `return roll(x, shift, axes)`。

最关键的一行就是 `shift = [dim // 2 for dim in x.shape]`（`None` 分支）和对应的 `x.shape[axes] // 2`（整数分支）。整个函数的「魔法」都浓缩在这个整除取半里。

#### 4.2.4 代码实践

**目标**：验证「`fftshift(x)` 完全等价于 `np.roll(x, len(x)//2)`」，从而确信它只是个平移。

**步骤**：

```python
import numpy as np

for n in (8, 9, 10):
    x = np.arange(n)                 # 任意数组，不必是频谱
    shifted = np.fft.fftshift(x)
    manual  = np.roll(x, n // 2)     # 手工等价操作
    print(f"n={n}: fftshift==roll ?", np.array_equal(shifted, manual), "| shift =", n // 2)
```

**预期结果**：三行都打印 `True`，`shift` 分别为 `4, 4, 5`。

**观察现象**：无论奇偶，`fftshift` 都与「向右 roll `n//2` 位」完全相同。这印证了它没有任何频域语义，只是按公式平移。（运行细节为「待本地验证」，但等价性直接来自源码 `roll(x, dim//2, axes)` 一句。）

#### 4.2.5 小练习与答案

**练习 1**：对一个 `shape = (4, 5, 6)` 的数组调用 `fftshift(a)`（不传 `axes`），三个轴分别平移多少？

**参考答案**：三个轴都平移，分别是 `4//2=2`、`5//2=2`、`6//2=3`。

**练习 2**：为什么源码用整除 `dim // 2` 而不是 `dim / 2`？

**参考答案**：因为 `roll` 的 `shift` 必须是整数（平移只能是整数个位置）。`dim / 2` 在 Python 中返回浮点数，奇长度时会得到 `x.5`，既无法用于索引也会让 `roll` 报错；`//` 保证得到整数，并自然处理了奇长度（向下取整）。

---

### 4.3 ifftshift：fftshift 的精确逆

#### 4.3.1 概念说明

`ifftshift` 是 `fftshift` 的逆运算。它的实现几乎和 `fftshift` 一模一样，**唯一**的区别是平移量取负号：

\[
\text{ifftshift: shift}_d = -\left\lfloor \frac{n_d}{2} \right\rfloor
\]

为什么取负号就一定是逆？因为循环平移有一个基本性质：先正向平移 \(s\)、再反向平移 \(s\)，结果不变。

\[
\text{roll}\big(\text{roll}(x, s), -s\big) = x
\]

把 \(s = \lfloor n/2 \rfloor\) 代入：`fftshift` 做 `roll(x, +s)`，`ifftshift` 做 `roll(x, -s)`，二者复合恰好抵消。所以对**任意**形状、任意奇偶长度，都有：

\[
\text{ifftshift}\big(\text{fftshift}(x)\big) = x
\]

这条恒等式是 `ifftshift` 存在的全部意义：当你做完 `fftshift` 去可视化或处理频谱后，可以用 `ifftshift` 把数组「摆回 standard 排列」，再喂给 `ifft` 做反变换。**频谱和频率轴必须一起 `ifftshift` 回去**，才能与 `ifft` 的输入约定对齐。

#### 4.3.2 核心流程

`ifftshift` 的分支结构与 `fftshift` 完全对称，只是每个 `shift` 前面多了一个负号：

```
输入 x, axes
 ├── axes is None        → shift = [-(dim//2) for dim in x.shape]
 ├── axes 是单个整数      → shift = -(x.shape[axes] // 2)
 └── axes 是元组/列表     → shift = [-(x.shape[ax]//2) for ax in axes]
return roll(x, shift, axes)
```

#### 4.3.3 源码精读

docstring 一句话点明它和 `fftshift` 的关系——「偶长度下与 `fftshift` 相同，奇长度下相差一个样本」：

[_helper.py:80-81](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_helper.py#L80-L81) —— `ifftshift` 文档：说明它是 `fftshift` 的逆，偶长度相同、奇长度相差一个样本。

函数体与 `fftshift` 镜像对称，注意每个 `shift` 表达式前的负号：

[_helper.py:113-122](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_helper.py#L113-L122) —— `ifftshift` 主体：三分支结构与 `fftshift` 相同，唯一区别是 `shift` 取负，即 `-(dim // 2)`、`-(x.shape[axes] // 2)`、`-(x.shape[ax] // 2)`，最后 `return roll(x, shift, axes)`。

对照 `fftshift` 的 [_helper.py:65-74](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_helper.py#L65-L74) 与 `ifftshift` 的 [_helper.py:113-122](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_helper.py#L113-L122)，两段代码的差异**只有负号**——这是本讲最重要的一处对照阅读。

测试侧，`test_inverse` 直接用随机数验证了这条恒等性：

[tests/test_helper.py:23-26](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/tests/test_helper.py#L23-L26) —— `test_inverse`：对 `n in [1, 4, 9, 100, 211]`（覆盖长度 1、偶、奇、大奇数）的随机数组，断言 `ifftshift(fftshift(x)) == x`。

注意 `211` 这个质数长度——它专门用来确保「奇怪的大奇数」也不会破坏可逆性。

#### 4.3.4 代码实践

**目标**：亲手验证 `ifftshift(fftshift(x)) == x` 在奇偶长度下都成立，并完成一次「shift → 处理 → 反 shift → ifft」的完整闭环。

**步骤**：

```python
import numpy as np

# 1) 可逆性：奇偶长度都成立
for n in (1, 4, 9, 100, 211):           # 与 test_inverse 同一组长度
    x = np.random.random(n)
    ok = np.array_equal(np.fft.ifftshift(np.fft.fftshift(x)), x)
    print(f"n={n:>3}: ifftshift(fftshift(x))==x ?", ok)

# 2) 完整闭环：fft -> fftshift -> ifftshift -> ifft 还原信号
a = np.sin(np.linspace(0, 2*np.pi, 16, endpoint=False))
A = np.fft.fft(a)
A_centered = np.fft.fftshift(A)         # 居中，便于观察/处理
A_back = np.fft.ifftshift(A_centered)   # 必须先摆回 standard 排列
a_restored = np.fft.ifft(A_back)
print("闭环还原 max|err| =", np.max(np.abs(a_restored - a)))
```

**预期结果**：第一段五行全为 `True`；第二段 `max|err|` 在 1e-15 量级（浮点误差）。

**观察现象**：可逆性与长度奇偶无关；但若**忘了 `ifftshift` 直接对 `A_centered` 调 `ifft`**，还原信号会完全错乱——这说明 `ifftshift` 是「居中视图」与「`ifft` 输入约定」之间的桥梁。

> 说明：上述数值结论由源码逻辑（`roll` 复合抵消）与 `test_inverse` 共同保证；具体浮点误差量为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`ifftshift` 的实现里，如果**漏掉负号**（写成和 `fftshift` 一样），会发生什么？

**参考答案**：`ifftshift` 会退化成 `fftshift`，于是 `ifftshift(fftshift(x))` 变成 `fftshift(fftshift(x))`。对偶长度它仍等于 `x`（因为偶长度下 `fftshift` 是对合），但对奇长度不再等于 `x`——`test_inverse` 中 `n=9` 和 `n=211` 的断言会失败。这正是负号存在的意义。

**练习 2**：能否不用 `ifftshift`，而用 `fftshift` 自己来还原一个已经 `fftshift` 过的偶长度数组？奇长度呢？

**参考答案**：偶长度可以——因为偶长度下 `roll(·, n/2)` 是对合（平移两次共 \(n\) 位 ≡ 0），所以 `fftshift(fftshift(x)) == x`，`fftshift` 自己就是自己的逆。奇长度不行——平移两次共 \(2\lfloor n/2\rfloor = n-1\) 位 ≢ 0 (mod \(n\))，无法还原；必须用 `ifftshift`。

---

### 4.4 奇偶差异与 axes 多维控制

#### 4.4.1 概念说明

**奇偶差异的来源**。比较两个函数的平移量：

\[
\text{fftshift: } s = \lfloor n/2 \rfloor, \qquad \text{ifftshift: } s' = -\lfloor n/2 \rfloor
\]

它们作为「平移操作」是否相同，取决于 \(s\) 与 \(s'\) 在模 \(n\) 意义下是否相等（因为 `roll` 是周期的，平移量差 \(n\) 等价）：

- **偶长度** \(n\)：\(\lfloor n/2 \rfloor = n/2\)，于是 \(s' = -n/2 \equiv n/2 = s \pmod n\)。两者完全相同——`fftshift` 与 `ifftshift` 是同一个操作（也是对合）。
- **奇长度** \(n\)：\(\lfloor n/2 \rfloor = (n-1)/2\)，于是 \(s' = -(n-1)/2 \equiv (n+1)/2 \pmod n\)。而 \(s = (n-1)/2\)，两者相差

\[
\frac{n+1}{2} - \frac{n-1}{2} = 1
\]

**恰好一个样本**。这就是 docstring 里「odd-length 相差一个样本」的数学根因。

注意：尽管奇长度下二者不同，`ifftshift` 仍是 `fftshift` 的精确逆（因为 \(-s\) 永远抵消 \(+s\)）。「相差一个样本」描述的是「`fftshift` 与 `ifftshift` 这两个函数彼此的差异」，而不是「逆运算是否成立」。

**Nyquist 提示**。回看 [_helper.py:25](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_helper.py#L25) 那句注释——「只有当 `len(x)` 为偶数时 `y[0]` 才是 Nyquist 分量」。因为偶长度 standard 数组里存在唯一的 Nyquist 箱（索引 \(n/2\)，频率 \(\pm n/2\)），`fftshift` 后它被搬到 `y[0]`；而奇长度根本没有精确的 Nyquist 频率（\(n/2\) 不是整数），所以 `y[0]` 只是「最负的那个频率」。

**axes 多维控制**。对多维数组，`axes` 决定**哪些轴**参与平移、其余轴完全不动。常见用法：

- `axes=None`（默认）：所有轴都平移。
- `axes=k`（单个整数）：只平移第 `k` 轴。
- `axes=(k1, k2)`（元组/列表）：只平移 `k1`、`k2` 轴，各轴平移量独立由各自长度决定。

#### 4.4.2 核心流程

多维情况下，`shift` 是一个**列表**，每个元素对应一个轴的平移量；`roll` 接收等长的 `axes` 序列，逐轴独立平移：

```
对 axes 中的每个轴 ax：
    该轴平移量 = ±(x.shape[ax] // 2)        # fftshift 取 +，ifftshift 取 -
roll 沿这些轴分别平移（互不干扰）
```

#### 4.4.3 源码精读

`test_definition` 同时给了奇、偶两组「标准答案」，是最权威的对照表：

[tests/test_helper.py:13-21](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/tests/test_helper.py#L13-L21) —— `test_definition`：奇长度 9 与偶长度 10 两组输入，断言 `fftshift(x)==y` 且 `ifftshift(y)==x`。例如奇长度 `[0,1,2,3,4,-4,-3,-2,-1]` 经 `fftshift`（roll +4）得到 `[-4,-3,-2,-1,0,1,2,3,4]`。

`test_axes_keyword` 验证多维 + 部分轴的行为，并确认「单整数轴与单元素元组等价」：

[tests/test_helper.py:28-39](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/tests/test_helper.py#L28-L39) —— `test_axes_keyword`：对 3×3 频谱矩阵，断言 `fftshift(freqs, axes=(0,1))` 与默认 `fftshift(freqs)` 结果相同；`axes=0` 与 `axes=(0,)` 等价；`ifftshift` 能精确还原。

`test_uneven_dims` 进一步用「行列长度不同」的 3×2 矩阵，分别测试只在轴 0、只在轴 1、两轴都平移的情形：

[tests/test_helper.py:41-84](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/tests/test_helper.py#L41-L84) —— `test_uneven_dims`：3×2 矩阵，轴 0 平移 `3//2=1` 位、轴 1 平移 `2//2=1` 位，给出每种 `axes` 组合的预期矩阵，并验证 `ifftshift` 还原。

最后，`test_equal_to_original` 是一个回归测试，把当前实现与 v1.14 的旧实现（用 `concatenate`+`take` 而非 `roll`）在 16×16 的所有形状、所有 `axes` 关键字下逐一比对，确保重构没有改变行为：

[tests/test_helper.py:86-133](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/tests/test_helper.py#L86-L133) —— `test_equal_to_original`：用旧版 `original_fftshift` / `original_ifftshift`（基于 `take`+`concatenate`）与新实现逐形状、逐 `axes` 对照，断言完全相等（见 issue #10073）。

#### 4.4.4 代码实践

**目标**：构造 1D 和 2D 频谱数组，验证「奇偶都恒等可逆」「奇长度相差一个样本」「部分轴平移」，并与 `test_helper.py` 的预期值逐字对照。

**步骤**：

```python
import numpy as np
from numpy.testing import assert_array_almost_equal as aae

# === 1D：奇偶长度都满足 ifftshift(fftshift(x)) == x ===
x_odd  = [0, 1, 2, 3, 4, -4, -3, -2, -1]          # n=9，取自 test_definition
y_odd  = [-4, -3, -2, -1, 0, 1, 2, 3, 4]
aae(np.fft.fftshift(x_odd), y_odd)                 # 应通过
aae(np.fft.ifftshift(y_odd), x_odd)                # 应通过

x_even = [0, 1, 2, 3, 4, -5, -4, -3, -2, -1]      # n=10
y_even = [-5, -4, -3, -2, -1, 0, 1, 2, 3, 4]
aae(np.fft.fftshift(x_even), y_even)
aae(np.fft.ifftshift(y_even), x_even)

# === 奇长度：fftshift 与 ifftshift 相差一个样本；偶长度：完全相同 ===
same_odd  = np.array_equal(np.fft.fftshift(x_odd),  np.fft.ifftshift(x_odd))   # False
same_even = np.array_equal(np.fft.fftshift(x_even), np.fft.ifftshift(x_even))  # True
print("奇长度 fftshift==ifftshift ?", same_odd)
print("偶长度 fftshift==ifftshift ?", same_even)

# === 2D：只在第二轴平移（与 fftshift docstring 示例、test_axes_keyword 对照）===
freqs = np.fft.fftfreq(9, d=1./9).reshape(3, 3)
shifted_axis1 = np.fft.fftshift(freqs, axes=(1,))
print("原矩阵:\n", freqs)
print("只在轴1平移:\n", shifted_axis1)

# === 2D：默认 axes=None 等价于两轴都平移 ===
aae(np.fft.fftshift(freqs), np.fft.fftshift(freqs, axes=(0, 1)))   # 应通过
```

**预期结果**（与 docstring 示例 [_helper.py:54-62](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_helper.py#L54-L62) 及 [tests/test_helper.py:28-39](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/tests/test_helper.py#L28-L39) 完全一致）：

- `aae(...)` 断言全部通过（不抛异常）。
- 打印：`奇长度 fftshift==ifftshift ? False`；`偶长度 fftshift==ifftshift ? True`。
- 只在轴 1 平移的结果为 `[[2,0,1],[-4,3,4],[-1,-3,-2]]`（每行各自左右半交换）。

**观察现象**：

1. 奇长度下 `fftshift` 与 `ifftshift` 的输出确实「错开一位」，但二者仍是互逆。
2. 2D「只在轴 1 平移」时，每一行内部做了左右半交换，而行与行之间的顺序（轴 0）保持不变——这正是 `axes` 精确控制的力量。

> 说明：以上断言与数值结论由源码与 `test_helper.py` 共同保证；若在本机运行，`assert_array_almost_equal` 来自 `numpy.testing`，无须额外安装。

#### 4.4.5 小练习与答案

**练习 1**：长度 7 的数组 `a`，`fftshift` 把元素向哪个方向移动几位？`ifftshift` 呢？二者是否相同？

**参考答案**：`fftshift` 向右（正方向）移动 `7//2 = 3` 位；`ifftshift` 向左（负方向）移动 3 位，等价于向右移动 \(7-3=4\) 位。7 是奇数，二者相差一个样本，**不相同**。

**练习 2**：`shape = (4, 5, 6)` 的数组，`fftshift(a, axes=(0, 2))` 后哪些轴被平移、各平移多少？轴 1 呢？

**参考答案**：轴 0 平移 `4//2=2`，轴 2 平移 `6//2=3`；轴 1 **完全不动**（不在 `axes` 里）。

**练习 3**：为什么 `test_inverse` 要专门挑 `n=211` 这种质数长度来测？

**参考答案**：质数长度既是奇数、又「远离」2 的幂，能最大限度暴露「奇长度 + 非常规尺寸」下的潜在 bug（例如有人误把 `dim/2` 当整数用、或忘了负号）。它与 `n=1, 9` 一起覆盖了「边界、小奇、大奇」三档，确保可逆性不依赖长度的好性质。

## 5. 综合实践

把本讲内容串起来，完成一个**最小频谱可视化与还原**流水线。它综合了「standard 排列」「fftshift 居中」「频率轴对齐」「ifftshift 摆回」「ifft 还原」五个环节。

**任务**：

1. 构造一个由两个正弦波叠加的信号（长度取**奇数** 65，以体验奇长度行为），采样间距 `d=0.01`。
2. 做 `fft`，得到 standard 排列的复数频谱 `A`。
3. 用 `fftfreq` 生成对应的频率轴 `f`，并**同时**对 `A` 和 `f` 做 `fftshift`，使二者仍逐位置对齐、且零频居中。
4. 观察（或用 `np.argmax(np.abs(...))` 找出）居中频谱里幅度最大的两个频率位置，反推信号里的两个正弦频率。
5. 用 `ifftshift` 把 `A` 摆回 standard 排列，再 `ifft` 还原信号，验证 `max|restored - original|` 在浮点误差量级。

**参考代码骨架**：

```python
import numpy as np

d = 0.01
t = np.arange(65) * d                      # 奇长度 65
a = np.sin(2*np.pi*5*t) + 0.5*np.sin(2*np.pi*20*t)   # 5Hz 与 20Hz

A = np.fft.fft(a)
f = np.fft.fftfreq(65, d)                  # standard 频率轴，与 A 对齐

A_c = np.fft.fftshift(A)                   # 居中频谱
f_c = np.fft.fftshift(f)                   # 居中频率轴（必须一起 shift！）

# 找两个最强峰
idx = np.argsort(np.abs(A_c))[-2:]
print("两个峰频率:", np.sort(f_c[idx]))    # 预期接近 -20,-5,5,20 中的两个正频率

# 还原：必须先 ifftshift 摆回 standard 排列
a_restored = np.fft.ifft(np.fft.ifftshift(A_c))
print("还原 max|err| =", np.max(np.abs(a_restored - a)))
```

**验收标准**：

- 打印的两个峰频率应落在 `±5` 和 `±20` 附近（因 `fftshift` 后正负频率都出现，故会看到四个峰，其中正频率侧为 `5` 与 `20`）。
- `还原 max|err|` 在 `1e-12` 量级以内。
- 把第 5 行的 `fftshift` / `ifftshift` 任意一个去掉或只对 `A` 不对 `f` 做，观察峰频率与还原误差如何错乱——这是理解「为什么频谱和频率轴必须一起 shift」的最佳方式。

> 说明：本任务依赖 `numpy.fft` 与 `numpy.fft.fftfreq`（u2-l2）；具体浮点误差量为「待本地验证」。

## 6. 本讲小结

- `fftshift` 与 `ifftshift` 是**纯 Python 的数组搬运工**，位于 [_helper.py](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/_helper.py)，不计算任何傅里叶变换，全部逻辑是 `roll(x, shift, axes)`。
- 平移量公式：`fftshift` 取 `shift = dim // 2`，`ifftshift` 取 `shift = -(dim // 2)`，二者**只差一个负号**。
- 因为 `roll(roll(x, s), -s) == x`，`ifftshift` 是 `fftshift` 的**精确逆**，对任意形状、任意奇偶长度都成立（`test_inverse` 用 `n=1,4,9,100,211` 覆盖）。
- 偶长度下 `fftshift` 与 `ifftshift` 完全相同（互为对合）；奇长度下二者相差**一个样本**——根因是 \(\lfloor n/2 \rfloor\) 与 \(-\lfloor n/2 \rfloor \bmod n\) 在奇长度时差 1。
- `axes` 控制多维平移：`None` 平移全部轴，整数只平移一个轴，元组/列表平移指定轴且各轴平移量由各自长度独立决定（`test_axes_keyword`、`test_uneven_dims`）。
- 典型用法是「`fft` → 同时对频谱与 `fftfreq` 做 `fftshift` 以便观察 → `ifftshift` 摆回 standard 排列 → `ifft` 还原」。

## 7. 下一步学习建议

- 想理解 `fftshift` 头上的 `@array_function_dispatch` 装饰器、以及它和 `fftfreq` 用的 `@set_module` 有何区别，请读 **u2-l4 array_function 调度与 set_module 机制**。
- 想知道 `fftshift` 所服务的 `fft` / `ifft` 内部到底怎么调后端，请读 **u3-l1 fft/ifft 与 _raw_fft 主流程**。
- 想看 NumPy 如何用测试保证 helper 的正确性与回归安全，可直接精读 [`tests/test_helper.py`](https://github.com/numpy/numpy/blob/2f93aec234bd1d6ff076147e56a6865547f77598/numpy/fft/tests/test_helper.py)，尤其是 `test_equal_to_original`（issue #10073 的回归守护）。
