# 实数序列 FFT：rfft/irfft 与实数打包格式

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出为什么实数序列要做「专门的 FFT」，以及它省下了什么。
- 准确描述 `scipy.fftpack.rfft` 输出的**实数交错打包格式**（interleaved packing），并能把它与 `scipy.fft.rfft` 返回的复数打包区分开。
- 看懂 `irfft` 如何从一串实数「拆包」回完整的共轭对称频谱，再还原成时域实数序列。
- 用 `rfftfreq` 给实数打包结果配上正确的频率轴。
- 解释 Python 层的 `rfft`/`irfft` 为什么只是「薄壳」，真正干活的是 DUCC 后端的 `rfft_fftpack`/`irfft_fftpack`。

## 2. 前置知识

在进入本讲前，建议你已经掌握（参见本系列 u1-l4）：

- **DFT 与 FFT 的关系**：DFT（离散傅里叶变换）是数学定义，FFT 是计算 DFT 的快速算法。
- **标准打包顺序**：`fft` 把长度为 \(n\) 的序列变成 \(n\) 个复数，下标对应频率为 \([0,1,\dots,n/2,-n/2+1,\dots,-1]\)（以 \(n=8\) 为例就是 `[0,1,2,3,-4,-3,-2,-1]`）。
- **归一化约定**：fftpack 把归一化全部压在逆变换一侧（`backward` 约定），所以有 `ifft(fft(x))≈x`。

本讲要新增的一个核心概念是**共轭对称性（Hermitian symmetry）**，这是「实数 FFT」能省一半计算的数学根源，我们先在 4.1 节展开。

## 3. 本讲源码地图

本讲涉及的源码文件都位于 `scipy/fftpack/` 下：

| 文件 | 作用 |
| --- | --- |
| [_basic.py](_basic.py) | 定义 `rfft`、`irfft` 的 Python 入口与文档字符串，是本讲的主角。打包格式的官方说明就写在它们的 docstring 里。 |
| [_helper.py](_helper.py) | 定义 `rfftfreq`，专门给 `rfft` 的实数打包结果标注频率轴。 |
| [tests/test_basic.py](tests/test_basic.py) | 含参考实现 `direct_rdft`/`direct_irdft`（精确描述打包逻辑的纯 Python 代码）以及 `rfft`/`irfft` 的行为测试。 |
| [__init__.py](__init__.py) | 把 `rfft`/`irfft` 通过聚合导入和 `__all__` 暴露为公共 API。 |

## 4. 核心概念与源码讲解

本讲拆成五个最小模块：先讲动机（4.1），再讲正向打包（4.2），然后是逆向拆包（4.3）、频率轴配套（4.4），最后看后端委托（4.5）。

### 4.1 为什么要单独为实数序列做 FFT

#### 4.1.1 概念说明

很多真实信号（声音、传感器读数、图像像素）都是**实数**序列。对一条长度为 \(n\) 的实数序列 \(x[0..n-1]\) 做 DFT，得到的复数频谱 \(y\) 并不是 \(n\) 个相互独立的复数，而是满足**共轭对称性**：

\[
y[k] = \overline{y[n-k]}, \quad k=1,\dots,n-1
\]

也就是说，正频分量和负频分量互为共轭（实部相等、虚部相反），知道一半就能推出另一半。再加上两个天然为实数的特殊点：

- \(y[0]\) 是所有样本的求和，恒为实数；
- 当 \(n\) 为偶数时，\(y[n/2]\)（奈奎斯特频率）也是实数。

把可省略的冗余信息丢掉后，独立实数恰好是 \(n\) 个（不是 \(2n\) 个）：

- \(y[0]\)：1 个实数；
- \(y[1],\dots,y[n/2-1]\)：\(n/2-1\) 个复数，折合 \(2(n/2-1)\) 个实数（仅 \(n\) 为偶数时）；
- \(y[n/2]\)（奈奎斯特）：1 个实数。

合计 \(1 + 2(n/2-1) + 1 = n\) 个实数。这就是「实数 FFT」存在的意义：**既然输入是实数，输出也只需要存一半的频谱**，计算量大约减半，存储也只需一个长度为 \(n\) 的实数数组。

#### 4.1.2 核心流程

实数 FFT 的整体流程可以用一句话概括：

```text
实数序列 x (长度 n)
      │  做实数 FFT（只算非冗余的半边频谱）
      ▼
实数打包数组 r (长度 n，存放 y(0), Re/Im(y(1)), …)
      │  做实数 IFFT（按共轭对称性补回另一半，再逆变换）
      ▼
实数序列 y (长度 n)  ——  应满足 irfft(rfft(x)) ≈ x
```

注意：相比普通 `fft` 输出 \(n\) 个复数，`rfft` 输出的是 \(n\) 个**实数**，下标含义完全不同。这是本讲最容易踩坑的地方，下一节专门讲。

#### 4.1.3 源码精读

`__init__.py` 的 autosummary 里对 `rfft` 的定位写得很直白——它是「严格实值序列的 FFT」：

```text
rfft - FFT of strictly real-valued sequence
irfft - Inverse of rfft
```

来源：[__init__.py:22-23](__init__.py#L22-L23)（autosummary 条目）和 [__init__.py:81-91](__init__.py#L81-L91)（`__all__` 里 `rfft`、`irfft` 与 `rfftfreq` 并列）。

`rfft` 的 docstring 开头也明确标注输入必须是实数：

```python
def rfft(x, n=None, axis=-1, overwrite_x=False):
    """
    Discrete Fourier transform of a real sequence.
    ...
    x : array_like, real-valued
        The data to transform.
```

见 [_basic.py:147-159](_basic.py#L147-L159)。

#### 4.1.4 代码实践

实践目标：用 `fft` 验证实数序列的共轭对称性，体会「一半频谱是冗余的」。

操作步骤：

1. 构造一段实数信号。
2. 调用 `scipy.fftpack.fft` 得到复数频谱。
3. 逐项检查 `y[k] == y[n-k].conj()`。

```python
import numpy as np
from scipy.fftpack import fft

x = np.array([9.0, -9.0, 1.0, 3.0])
y = fft(x)
n = len(x)
print("y =", y)
print("y[1] 与 y[n-1] 共轭？", np.allclose(y[1], y[n-1].conj()))
print("y[0] 的虚部 ≈ 0？", np.isclose(y[0].imag, 0))
```

需要观察的现象：`y[0]` 和 `y[2]` 的虚部都接近 0（实数），`y[1]` 与 `y[3]` 互为共轭。

预期结果（与 docstring 一致）：

```text
y = [ 4.+0.j  8.+12.j 16.+0.j  8.-12.j]
```

#### 4.1.5 小练习与答案

**练习**：对于长度为 8 的实数序列，其 `fft` 输出里有几个「独立」的实数值？

**参考答案**：8 个。即 `y[0]`（1 个实数）+ `y[1]`、`y[2]`、`y[3]`（3 个复数 = 6 个实数）+ `y[4]` 奈奎斯特（1 个实数）= 8。`y[5..7]` 是 `y[3..1]` 的共轭，属冗余。

---

### 4.2 rfft 与 fftpack 实数交错打包格式

#### 4.2.1 概念说明

`rfft` 最特殊的地方是它的**输出是一个实数数组**，而不是复数数组。它把 4.1 节算出来的 \(n\) 个独立实数，按一种称为 **fftpack / 交错打包（interleaved packing）** 的格式塞进一个长度为 \(n\) 的实数数组：

- \(n\) 为偶数：

\[
[\,y(0),\ \mathrm{Re}(y(1)),\ \mathrm{Im}(y(1)),\ \dots,\ \mathrm{Re}(y(n/2))\,]
\]

- \(n\) 为奇数：

\[
[\,y(0),\ \mathrm{Re}(y(1)),\ \mathrm{Im}(y(1)),\ \dots,\ \mathrm{Re}(y((n-1)/2)),\ \mathrm{Im}(y((n-1)/2))\,]
\]

换句话说，第 0 位放直流分量；从第 1 位开始，**实部、虚部交替存放**；当 \(n\) 为偶数时，最后一位单独存放奈奎斯特频率的实部。

> ⚠️ **这是 fftpack 独有的「非标准」格式**。新模块 `numpy.fft.rfft` / `scipy.fft.rfft` 返回的是 \(n/2+1\) 个**复数** `[y(0), y(1), …, y(n/2)]`，更直观也更通用。这也是 `rfft` docstring 末尾提醒你「想要复数输出请用 `scipy.fft.rfft`」的原因。

#### 4.2.2 核心流程

以 \(n=8\)（偶数）为例，打包结果的下标布局是：

| 输出下标 | 0 | 1 | 2 | 3 | 4 | 5 | 6 | 7 |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| 含义 | \(y(0)\) | \(\mathrm{Re}(y(1))\) | \(\mathrm{Im}(y(1))\) | \(\mathrm{Re}(y(2))\) | \(\mathrm{Im}(y(2))\) | \(\mathrm{Re}(y(3))\) | \(\mathrm{Im}(y(3))\) | \(\mathrm{Re}(y(4))\) |

通用规则（伪代码）：

```text
r[0]            ← y(0)                      # 实数
for i in 1..n//2:
    r[2*i - 1]  ← Re(y(i))
    if 2*i < n:                            # 不是奈奎斯特
        r[2*i]     ← Im(y(i))
    # 若 2*i == n（仅 n 偶数、i == n//2），这是奈奎斯特，只存实部
```

可以验证长度恒为 \(n\)：偶数时 \(1 + 2(n/2-1) + 1 = n\)；奇数时 \(1 + 2((n-1)/2) = n\)。

#### 4.2.3 源码精读

打包格式的权威说明写在 `rfft` 的 docstring 里：

```python
Returns
-------
z : real ndarray
    The returned real array contains::

      [y(0),Re(y(1)),Im(y(1)),...,Re(y(n/2))]              if n is even
      [y(0),Re(y(1)),Im(y(1)),...,Re(y(n/2)),Im(y(n/2))]   if n is odd
```

见 [_basic.py:166-176](_basic.py#L166-L176)。docstring 还给了一个把 `fft` 与 `rfft` 并排对比的经典例子，最能说明两种打包的区别：

```python
>>> from scipy.fftpack import fft, rfft
>>> a = [9, -9, 1, 3]
>>> fft(a)
array([  4. +0.j,   8.+12.j,  16. +0.j,   8.-12.j])
>>> rfft(a)
array([ 4.,   8.,  12.,  16.])
```

见 [_basic.py:195-204](_basic.py#L195-L204)。对照解读：

- `fft(a)` = `[4, 8+12j, 16, 8-12j]`（标准复数打包，`a[3]=8-12j` 是 `a[1]` 的共轭）。
- `rfft(a)` = `[4, 8, 12, 16]` = `[y(0), Re(y(1)), Im(y(1)), Re(y(2))]`。
  - `4` 是直流；
  - `8`、`12` 是 `y(1)=8+12j` 的实部、虚部；
  - `16` 是奈奎斯特 `y(2)` 的实部。

测试套件里有一份**纯 Python 的参考实现 `direct_rdft`**，精确地复现了这套交错写入逻辑，非常适合用来确认理解：

```python
def direct_rdft(x):
    x = asarray(x)
    n = len(x)
    w = -arange(n)*(2j*pi/n)
    r = zeros(n, dtype=double)
    for i in range(n//2+1):        # i = 0..n//2（含）
        y = dot(exp(i*w), x)       # 第 i 个 DFT 系数（复数）
        if i:
            r[2*i-1] = y.real      # Re(y(i)) 写到 2i-1 位
            if 2*i < n:
                r[2*i] = y.imag    # Im(y(i)) 写到 2i 位（奈奎斯特跳过）
        else:
            r[0] = y.real          # 直流 y(0)，实数
    return r
```

见 [tests/test_basic.py:79-92](tests/test_basic.py#L79-L92)。这段代码就是 4.2.2 流程图的直译，并且正是测试 `rfft` 正确性的「金标准」（[tests/test_basic.py:275-281](tests/test_basic.py#L275-L281) 里 `test_definition` 就拿它和 `rfft(x)` 逐项比对）。

#### 4.2.4 代码实践

实践目标：亲手实现一次「从 `fft` 到 `rfft` 打包」的转换，验证两者携带的信息完全一致。

操作步骤：

```python
import numpy as np
from scipy.fftpack import fft, rfft

a = np.array([9.0, -9.0, 1.0, 3.0])
y = fft(a)               # 标准复数打包
r = rfft(a)              # 实数交错打包
n = a.size

# 按 fftpack 规则，自己从 y 构造交错数组
manual = np.empty(n)
manual[0] = y[0].real
for i in range(1, n // 2 + 1):
    manual[2*i - 1] = y[i].real
    if 2*i < n:
        manual[2*i] = y[i].imag

print("rfft   =", r)
print("manual =", manual)
print("一致？", np.allclose(r, manual))
```

需要观察的现象：`manual` 与 `rfft` 完全相同；并且能直观看到「最后一个数 16 是奈奎斯特实部，没有配对的虚部」。

预期结果：`一致？ True`。

#### 4.2.5 小练习与答案

**练习 1**：`rfft([9,-9,1,3])` 返回 4 个实数，而 `scipy.fft.rfft([9,-9,1,3])` 返回几个复数？

**参考答案**：3 个复数 `[y(0), y(1), y(2)]` = `[4+0j, 8+12j, 16+0j]`。fftpack 把它们展平成 4 个实数（多出的 1 个是因为 `y(1)` 的实虚部占了两位）。

**练习 2**：对奇数长度 \(n=5\) 的实数序列，`rfft` 输出第几位是 `Im(y(2))`？

**参考答案**：第 4 位（下标从 0 算）。布局是 `[y(0), Re(y(1)), Im(y(1)), Re(y(2)), Im(y(2))]`，`Im(y(2))` 在下标 \(2\cdot2=4\)。

---

### 4.3 irfft：从实数打包还原实数序列

#### 4.3.1 概念说明

`irfft` 是 `rfft` 的逆运算：它接收一个**按 fftpack 实数格式打包**的数组，把它「拆包」回完整的共轭对称复数频谱，再做逆 DFT，最后取实部得到原始的实数序列。

关键点在于：`irfft` 的输入 `x` 会被**当作 `rfft` 的输出来解读**。这意味着你必须用同样的打包约定来构造输入，否则结果毫无意义。逆变换的归一化仍遵循 `backward` 约定（结果乘以 \(1/n\)），所以有 `irfft(rfft(x))≈x`。

#### 4.3.2 核心流程

拆包逻辑（与 4.2 的打包严格对称）：

```text
构造长度 n 的复数数组 x1，初值 0
x1[0]       ← x[0]                              # 直流，实数
for i in 1..n//2:
    if 2*i < n:                                 # 非奈奎斯特
        x1[i]     ← x[2*i-1] + 1j * x[2*i]      # 恢复 y(i)
        x1[n-i]   ← x[2*i-1] - 1j * x[2*i]      # 补上共轭 y(n-i)
    else:                                        # 奈奎斯特（n 偶数）
        x1[i]     ← x[2*i-1]                     # 实数
返回 idft(x1).real
```

被恢复出来的 \(y(i)\) 和 \(y(n-i)\) 恰好互为共轭，这正是 4.1 节共轭对称性的「逆向应用」——打包时丢掉的冗余负频，在这里凭对称性补回来。

#### 4.3.3 源码精读

`irfft` 的 docstring 用数学公式写出了逆变换（注意 \(1/n\) 的归一化和 `c.c.` 表示前一项的复共轭）：

```text
y(j) = 1/n (sum[k=1..n/2-1] (x[2*k-1]+sqrt(-1)*x[2*k])
                             * exp(sqrt(-1)*j*k* 2*pi/n)
                + c.c. + x[0] + (-1)**(j) x[n-1])
```

见 [_basic.py:240-258](_basic.py#L240-L258)。其中 `(-1)**j * x[n-1]` 这一项正是奈奎斯特频率 `y(n/2)` 的贡献。

对应的纯 Python 参考实现 `direct_irdft` 把上面的拆包逻辑写得很清楚：

```python
def direct_irdft(x):
    x = asarray(x)
    n = len(x)
    x1 = zeros(n, dtype=cdouble)
    for i in range(n//2+1):
        if i:
            if 2*i < n:
                x1[i]   = x[2*i-1] + 1j*x[2*i]
                x1[n-i] = x[2*i-1] - 1j*x[2*i]   # 共轭补回负频
            else:
                x1[i]   = x[2*i-1]               # 奈奎斯特，实数
        else:
            x1[0] = x[0]                          # 直流
    return direct_idft(x1).real
```

见 [tests/test_basic.py:95-108](tests/test_basic.py#L95-L108)。`irfft` 的 docstring 还给出往返一致性的例子：

```python
>>> from scipy.fftpack import rfft, irfft
>>> a = [1.0, 2.0, 3.0, 4.0, 5.0]
>>> irfft(rfft(a))
array([1., 2., 3., 4., 5.])
```

见 [_basic.py:264-273](_basic.py#L264-L273)。测试套件里的 `test_random_real` 系统验证了 `irfft(rfft(x))` 与 `rfft(irfft(x))` 在多种长度下都能还原原信号（[tests/test_basic.py:346-354](tests/test_basic.py#L346-L354)）。

#### 4.3.4 代码实践

实践目标：体会「`irfft` 把输入当作打包数组解读」这一约定——给它一段任意实数，它也会按打包规则硬拆。

操作步骤：

1. 用 `rfft` 得到正确打包，再 `irfft` 还原（验证往返）。
2. 直接给 `irfft` 一段「未打包」的自然数，看它如何解读。

```python
import numpy as np
from scipy.fftpack import rfft, irfft

a = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
print("往返:", irfft(rfft(a)))          # 应还原 a

# 把同一组数字直接喂给 irfft：它会当成 [y0, Re(y1), Im(y1), Re(y2), Im(y2)]
print("直接解读:", irfft(a))
```

需要观察的现象：第一行还原成 `[1,2,3,4,5]`；第二行的结果与第一行完全不同——因为 `irfft` 把 `[1,2,3,4,5]` 当成「直流=1，y(1)=2+3j，y(2)=4+5j」的打包数组，而非时域样本。

预期结果：与 docstring 的两行输出一致（`[1.,2.,3.,4.,5.]` 与 `[2.6, -3.164…, …]`）。

#### 4.3.5 小练习与答案

**练习**：如果 `rfft` 打包数组长度 \(n=6\)（偶数），`irfft` 拆包时会生成几个复数频谱点？哪些是成对共轭的？

**参考答案**：生成 6 个复数点。其中 `x1[0]`（直流）和 `x1[3]`（奈奎斯特）是实数；`x1[1]` 与 `x1[5]`、`x1[2]` 与 `x1[4]` 成对互为共轭。

---

### 4.4 rfftfreq：为实数打包结果标注频率轴

#### 4.4.1 概念说明

`fft` 配套的频率轴函数是 `fftfreq`（每个输出位对应一个频率）。但 `rfft` 用的是交错打包，**同一个频率的实部和虚部占了相邻两位**，所以普通的 `fftfreq`（每位一个频率）对不上号。`rfftfreq` 就是为这种打包量身定做的：它返回一个与 `rfft` 输出**逐位对齐**的频率数组，非直流、非奈奎斯特的频率会连续出现两次（分别对应实部和虚部）。

#### 4.4.2 核心流程

设采样间距为 \(d\)，窗口长度 \(n\)，则频率分辨率为 \(1/(nd)\)。`rfftfreq` 的输出为：

- \(n\) 为偶数：

\[
f = [0,1,1,2,2,\dots,n/2-1,n/2-1,n/2]/(dn)
\]

- \(n\) 为奇数：

\[
f = [0,1,1,2,2,\dots,n/2-1,n/2-1,n/2,n/2]/(dn)
\]

实现上非常巧妙，一行就够：

\[
f = \big(\,\text{floor}((1,2,\dots,n)/2)\,\big) / (nd)
\]

即 `(np.arange(1, n+1) // 2) / (n*d)`。前两个数 `1//2=0`、`2//2=1`，于是开头自然是 `[0, 1, 1, 2, 2, …]`。

#### 4.4.3 源码精读

`rfftfreq` 定义在 [_helper.py:11-51](_helper.py#L11-L51)，频率布局写在 docstring：

```python
  f = [0,1,1,2,2,...,n/2-1,n/2-1,n/2]/(d*n)   if n is even
  f = [0,1,1,2,2,...,n/2-1,n/2-1,n/2,n/2]/(d*n)   if n is odd
```

见 [_helper.py:18-19](_helper.py#L18-L19)。核心实现只有一行（含对 `n` 的非负校验）：

```python
n = operator.index(n)
if n < 0:
    raise ValueError(f"n = {n} is not valid. "
                     "n must be a nonnegative integer.")
return (np.arange(1, n + 1, dtype=int) // 2) / float(n * d)
```

见 [_helper.py:46-51](_helper.py#L46-L51)。docstring 还给了一个完整例子，展示 `rfft` + `rfftfreq` 的标准用法：

```python
>>> sig = np.array([-2, 8, 6, 4, 1, 0, 3, 5], dtype=float)
>>> sig_fft = fftpack.rfft(sig)
>>> n = sig_fft.size
>>> timestep = 0.1
>>> freq = fftpack.rfftfreq(n, d=timestep)
>>> freq
array([ 0.  ,  1.25,  1.25,  2.5 ,  2.5 ,  3.75,  3.75,  5.  ])
```

见 [_helper.py:33-43](_helper.py#L33-L43)。可以看到 `1.25`、`2.5`、`3.75` 各出现两次（实部/虚部各一次），而直流 `0` 和奈奎斯特 `5` 各一次——这与 4.2 的打包布局完全吻合。

#### 4.4.4 代码实践

实践目标：验证 `rfftfreq` 与 `rfft` 输出逐位对齐。

操作步骤：

```python
import numpy as np
from scipy.fftpack import rfft, rfftfreq

sig = np.array([-2., 8., 6., 4., 1., 0., 3., 5.])
spec = rfft(sig)
freq = rfftfreq(spec.size, d=0.1)
print("spec:", spec)
print("freq:", freq)
print("长度相等？", spec.size == freq.size)
```

需要观察的现象：两个数组长度都是 8；`freq` 中每个频率恰好与 `spec` 中的实部/虚部位置对齐（成对的频率值成对出现）。

预期结果：与上面的 docstring 输出一致。

#### 4.4.5 小练习与答案

**练习**：为什么 `rfftfreq(8)` 的结果里 `1.25` 出现两次，而 `0` 只出现一次？

**参考答案**：因为 `rfft` 把 `y(1)` 的实部和虚部存在相邻两位，两者频率都是 \(1/(8\cdot0.1)=1.25\)，所以 `1.25` 出现两次；而直流 `y(0)` 只有一位、频率为 0，所以 `0` 只出现一次。

---

### 4.5 Python 薄壳与 DUCC 后端委托

#### 4.5.1 概念说明

和 `fft`/`ifft` 一样（见 u1-l4），`rfft`/`irfft` 在 Python 层**不真正做计算**，只是把参数整理好后委托给 C/C++ 后端 DUCC。注意后端函数名带 `_fftpack` 后缀（`rfft_fftpack`、`irfft_fftpack`），这正是为了**沿用 fftpack 的实数交错打包约定**——DUCC 同时还提供返回复数打包的 `rfft`/`irfft`（供 `scipy.fft` 使用），二者靠后缀区分。

委托时第 4 个参数被硬编码为 `None`，那是归一化参数（`norm`）。fftpack 不暴露 `norm`，永远使用 `backward` 约定，所以这里固定传 `None`，让后端按默认（逆变换归一化）处理。

#### 4.5.2 核心流程

```text
rfft(x, n, axis, overwrite_x)
   └─ return _duccfft.rfft_fftpack(x, n, axis, None, overwrite_x)
                                          ^^^^
                                          norm 固定 None（backward 约定）

irfft(x, n, axis, overwrite_x)
   └─ return _duccfft.irfft_fftpack(x, n, axis, None, overwrite_x)
```

也就是说，本讲前面学到的「打包格式」「共轭对称」「\(1/n\) 归一化」全部由这个 DUCC 后端实现并保证，Python 层一行不差地透传参数。

#### 4.5.3 源码精读

`_basic.py` 顶部从 `scipy.fft` 导入后端模块：

```python
from scipy.fft import _duccfft
from ._helper import _good_shape
```

见 [_basic.py:8-9](_basic.py#L8-L9)。`rfft` 函数体去掉 docstring 后只有一行：

```python
def rfft(x, n=None, axis=-1, overwrite_x=False):
    """..."""
    return _duccfft.rfft_fftpack(x, n, axis, None, overwrite_x)
```

见 [_basic.py:147-205](_basic.py#L147-L205)，关键委托行在 [_basic.py:205](_basic.py#L205)。`irfft` 同理：

```python
def irfft(x, n=None, axis=-1, overwrite_x=False):
    """..."""
    return _duccfft.irfft_fftpack(x, n, axis, None, overwrite_x)
```

见 [_basic.py:208-274](_basic.py#L208-L274)，委托行在 [_basic.py:274](_basic.py#L274)。

关于「拒绝复数输入」这条行为，Python 层并没有显式写 `if`——它是由后端在 C 层校验并抛出 `TypeError` 的。测试套件直接断言了这一点：

```python
def test_complex_input(self):
    assert_raises(TypeError, rfft, np.arange(4, dtype=np.complex64))
```

见 [tests/test_basic.py:310-311](tests/test_basic.py#L310-L311)（`irfft` 的对应断言在 [tests/test_basic.py:375-376](tests/test_basic.py#L375-L376)）。同理，空数组会触发 `ValueError`（[tests/test_basic.py:283-285](tests/test_basic.py#L283-L285)）。

#### 4.5.4 代码实践

实践目标（源码阅读型）：确认 Python 入口确实是「一行委托」，并观察复数输入被后端拒绝。

操作步骤：

1. 打开 [_basic.py:147-205](_basic.py#L147-L205)，确认 `rfft` 函数体只有一行 `return`。
2. 运行下面代码，触发后端的类型校验：

```python
import numpy as np
from scipy.fftpack import rfft
try:
    rfft(np.arange(4, dtype=np.complex64))
except TypeError as e:
    print("后端拒绝复数输入：", e)
```

需要观察的现象：抛出 `TypeError`，证明「实数序列专用」这个约束由后端强制执行，而非 Python 层判断。

预期结果：捕获到 `TypeError`。具体报错文案以本地运行结果为准（待本地验证）。

#### 4.5.5 小练习与答案

**练习**：后端函数叫 `rfft_fftpack`，而 `scipy.fft.rfft` 背后用的 DUCC 函数却没有 `_fftpack` 后缀。这两者最本质的区别是什么？

**参考答案**：`rfft_fftpack` 输出 fftpack 的**实数交错打包**（长度 \(n\) 的实数数组，本讲主题）；无后缀的 `rfft` 输出**复数打包**（长度 \(n/2+1\) 的复数数组）。后缀 `_fftpack` 就是为了在同一个后端里区分这两套打包约定。

---

## 5. 综合实践

把本讲的知识串起来：用 `rfft` 分析一段含噪声的实信号，在**实数打包格式**下找到主频率，再用 `irfft` 还原信号。

任务要求：

1. 构造采样率 `fs = 1000` Hz、长度 `N = 1024` 的实信号：一个 50 Hz 的正弦波加上随机噪声。
2. 用 `rfft` 得到实数打包频谱，用 `rfftfreq` 得到对齐的频率轴。
3. **注意打包格式**：由于实部、虚部交错，建议先把打包数组按 4.2 的规则手动还原成「频率 → 复数幅值」的映射（直流单独处理、奈奎斯特单独处理、其余两两配对），再求模长。
4. 找出模长最大的非直流频率，验证它接近 50 Hz。
5. 用 `irfft(rfft(sig))` 还原信号，确认 `np.allclose` 成立。

参考骨架（请你补全「拆包求模长」的部分）：

```python
import numpy as np
from scipy.fftpack import rfft, irfft, rfftfreq

fs, N = 1000, 1024
t = np.arange(N) / fs
rng = np.random.default_rng(0)
sig = 1.0 * np.sin(2*np.pi*50*t) + 0.3 * rng.standard_normal(N)

spec = rfft(sig)
freq = rfftfreq(N, d=1/fs)

# TODO: 把 spec（实数交错打包）拆成 (频率, 模长) 两列
# 提示：
#   spec[0] 是直流
#   spec[-1] 是奈奎斯特（N 偶数）
#   其余按 i 配对：spec[2*i-1] + 1j*spec[2*i]，对应频率 freq[2*i-1]
# 然后找出非直流里模长最大的频率，应接近 50。

restored = irfft(rfft(sig))
print("往返还原？", np.allclose(restored, sig))
```

需要观察的现象：还原成立；主频率峰值落在 50 Hz 附近。

预期结果：`往返还原？ True`，且峰值频率 ≈ 50 Hz（待本地验证精确数值）。

> 进阶思考：如果改用 `scipy.fft.rfft`（复数打包），上面的「拆包」步骤就不需要了——直接对复数数组取模长即可。这正是 docstring 推荐 `scipy.fft.rfft` 的原因。

## 6. 本讲小结

- `rfft` 专为**实数序列**设计，利用共轭对称性 \(y[k]=\overline{y[n-k]}\) 只算非冗余的半边频谱，计算量约减半，独立信息恰好是 \(n\) 个实数。
- `rfft` 的输出是**长度 \(n\) 的实数数组**，采用 fftpack 独有的**实数交错打包**：`[y(0), Re(y(1)), Im(y(1)), …]`（偶数 \(n\) 末位为奈奎斯特实部）。这与 `scipy.fft.rfft` 返回 \(n/2+1\) 个复数完全不同。
- `irfft` 是逆向「拆包」：按相同约定把实数数组还原成共轭对称的完整复数频谱，再做逆 DFT 并取实部；遵循 `backward` 归一化，保证 `irfft(rfft(x))≈x`。
- `rfftfreq` 为实数打包结果提供逐位对齐的频率轴，非直流/非奈奎斯特频率成对出现，对应实部、虚部两位。
- Python 层的 `rfft`/`irfft` 只是薄壳，真正计算由 DUCC 后端 `_duccfft.rfft_fftpack`/`irfft_fftpack` 完成；`_fftpack` 后缀正是用来区分这套实数交错打包与 `scipy.fft` 的复数打包。

## 7. 下一步学习建议

- **进入多维 FFT**：本讲的 `rfft`/`irfft` 只是一维。接下来可学习 `fftn`/`fft2` 如何处理多维输入与 `shape`/`axes` 校验（对应 u2-l1），理解多维打包。
- **进入实变换族**：实数序列除了 FFT，还有 `dct`（离散余弦变换）和 `dst`（离散正弦变换），它们进一步利用信号的对称性，在压缩（如 JPEG）中极为常见。源码在 [_realtransforms.py](_realtransforms.py)。
- **阅读打包格式的「金标准」**：若想彻底吃透打包约定，强烈建议通读 [tests/test_basic.py:79-108](tests/test_basic.py#L79-L108) 的 `direct_rdft`/`direct_irdft`，它们是用纯 Python 写的格式说明书。
- **对比新模块**：动手把本讲的例子全部用 `scipy.fft.rfft`/`irfft` 重写一遍，体会「复数打包」相比「实数交错打包」的便利，理解 fftpack 为何被标记为遗留模块。
