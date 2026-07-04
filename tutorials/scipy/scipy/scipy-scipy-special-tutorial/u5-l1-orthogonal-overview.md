# _orthogonal.py 概览：orthopoly1d 与 Golub-Welsch 算法

## 1. 本讲目标

`scipy.special` 里有一族「正交多项式」相关的函数：`roots_legendre`、`legendre`、`roots_hermite`、`jacobi`…… 它们全部住在一个文件 `_orthogonal.py` 里。本讲带读者读懂这个文件的整体设计。读完本讲，你应当能够：

- 说清楚什么是「正交多项式的三项递推关系」，以及它如何决定一个对称三对角矩阵（Jacobi 矩阵）。
- 理解 **Golub-Welsch 算法** 的核心思想：把「求高斯求积的节点与权重」转化为「求三对角矩阵的特征值」。
- 看懂 `orthopoly1d` 类如何把一组节点与权重封装成一个可以像多项式一样调用的对象，以及为什么它附带一个 `_eval_func`。
- 明白 `_orthogonal.py` 中 `roots_*`（纯 Python，求节点权重）与 `eval_*`（来自 `_ufuncs`，逐元素求值）这两套接口的分工。

本讲只精读 [`_orthogonal.py`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_orthogonal.py) 这一个文件，是 U5「正交多项式与高斯求积」单元的开篇；更细致的 `roots_*` 与 `eval_*` 对比留到 u5-l2，多输出聚合留到 u5-l3。

## 2. 前置知识

阅读本讲前，最好已经知道（u1-l4、u2-l1、u2-l2 已建立）：

- **特殊函数大多是 NumPy ufunc**：标量与数组同源，逐元素求值、可广播。`scipy.special` 里的 `eval_legendre`、`eval_jacobi` 等就是这样的 ufunc，住在编译产物 `_ufuncs` 里。
- **`scipy.special` 命名空间由多个子模块拼装**：`__init__.py` 用 `from ._orthogonal import *` 把这里的函数提到顶层货架，并用 `__all__` 控制公开 API。

本讲会用到几个新概念，先用大白话解释：

- **高斯求积（Gaussian quadrature）**：用有限个「节点 \(x_i\)」和「权重 \(w_i\)」近似计算一个定积分 \(\int_a^b f(x)w(x)\,dx \approx \sum_i w_i f(x_i)\)。巧妙之处在于，只要 \(n\) 个节点选得对，就能**精确**积分次数不超过 \(2n-1\) 的多项式。节点就是某个正交多项式的根。
- **正交多项式（orthogonal polynomials）**：一族多项式 \(P_0, P_1, P_2, \ldots\)，关于某个权函数 \(w(x)\) 在区间 \([a,b]\) 上两两「垂直」：\(\int_a^b P_m(x)P_n(x)w(x)\,dx = 0\) 当 \(m\neq n\)。Legendre、Chebyshev、Hermite、Laguerre、Jacobi 都是。
- **三项递推（three-term recurrence）**：每个 \(P_{n+1}\) 都能写成它前一项 \(P_n\) 与前两项 \(P_{n-1}\) 的线性组合。这个递推里藏着的系数，正是构造求积节点的钥匙。
- **对称三对角矩阵的特征值**：一个只在主对角线和两条次对角线上有非零元素的方阵，它的特征值可以用专用算法（`scipy.linalg.eigvals_banded`）快速、稳定地求出。

## 3. 本讲源码地图

本讲只涉及一个源码文件，但它在概念上分成四块：

| 代码区域 | 行号区间 | 作用 |
| --- | --- | --- |
| 顶部数学注释 | 1–72 | 给出递推关系、\(A_n/B_n\) 系数与 Golub-Welsch 的文献出处 |
| 命名与导出 | 89–147 | `_polyfuns`、`_rootfuns_map`、`__all__` 的拼装 |
| `orthopoly1d` 类 | 150–192 | 多项式对象，封装节点、权重、系数与求值函数 |
| `_gen_roots_and_weights` | 195–241 | **Golub-Welsch 的通用实现**，是整个文件的心脏 |
| 各 `roots_*` / 多项式函数 | 246–2892 | 给每个多项式家族填入递推系数，调用上面的通用函数 |

其中各 `roots_*`（如 `roots_legendre`、`roots_jacobi`）都遵循同一个套路：**填好四个回调 `an_func`/`bn_func`/`f`/`df`，再交给 `_gen_roots_and_weights` 干活**。看懂 `_gen_roots_and_weights`，就看懂了整族函数。

## 4. 核心概念与源码讲解

### 4.1 三项递推关系

#### 4.1.1 概念说明

一族正交多项式 \(P_0, P_1, P_2, \ldots\)（最高次系数为 1 的「首一」规范化形式之外，还有各种其它规范化）总是满足一个**三项递推关系**。在教科书中，最通用的写法是：

\[ a_{1,n}\, f_{n+1}(x) = (a_{2,n} + a_{3,n}\, x)\, f_n(x) - a_{4,n}\, f_{n-1}(x) \]

其中 \(a_{1,n}, a_{2,n}, a_{3,n}, a_{4,n}\) 是随阶数 \(n\) 变化的常数（每个多项式家族有自己的表达式）。`_orthogonal.py` 顶部的注释原原本本地写下了这条式子（见下文源码精读）。

为了构造求积节点，作者把它改写成「标准化」的形式：

\[ P_{n+1}(x) = (x - A_n)\, P_n(x) - B_n\, P_{n-1}(x) \]

也就是说，把递推里 \(x\) 的系数规整成 1。两组系数的对应关系是：

\[ A_n = -\frac{a_{2,n}}{a_{3,n}} \qquad B_n = \left(\frac{a_{4,n}}{a_{3,n}} \sqrt{\frac{h_{n-1}}{h_n}}\right)^2 \]

其中 \(h_n = \int_a^b w(x) f_n(x)^2\,dx\) 是第 \(n\) 阶多项式（按原规范化）的平方范数。**\(A_n\) 进入矩阵主对角线，\(\sqrt{B_n}\) 进入次对角线。** 这两个系数就是连接「多项式理论」与「矩阵特征值」的桥梁。

#### 4.1.2 核心流程

把递推关系「翻译」成代码的流程是：

1. 对某个具体的正交多项式家族（如 Legendre），查出它的递推系数 \(a_{*,n}\) 与范数 \(h_n\)。
2. 用上面的公式算出 \(A_n\) 与 \(\sqrt{B_n}\)。
3. 在代码里写成两个函数 `an_func(k)` 返回 \(A_k\)、`bn_func(k)` 返回 \(\sqrt{B_k}\)。
4. 把这两个函数交给 `_gen_roots_and_weights`（见 4.2）。

以 Legendre 为例（权函数 \(w(x)=1\)，区间 \([-1,1]\)）：它关于 \(x=0\) 对称，递推里 \(x\) 的常数项为 0，所以 \(A_n \equiv 0\)；而 \(\sqrt{B_n} = n/\sqrt{4n^2-1}\)。

#### 4.1.3 源码精读

文件顶部那段「数学注释」就是上面这些公式的出处：[_orthogonal.py:10-47](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_orthogonal.py#L10-L47) —— 这段注释先用通用递推式 \(a_{1,n} f_{n+1} = (a_{2,n}+a_{3,n}x) f_n - a_{4,n} f_{n-1}\) 起头，再给出标准化的 \(P_{n+1}=(x-A_n)P_n - B_n P_{n-1}\)，最后写明 \(A_n\)、\(B_n\) 与 \(h_n\) 的换算，并指明初值假设 \(P_0=1,\; P_{-1}=0\)。这是整个文件算法思想的「契约」。

看一个具体填表例子——Legendre：[_orthogonal.py:2655-2664](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_orthogonal.py#L2655-L2664)

```python
mu0 = 2.0
def an_func(k):
    return 0.0 * k                  # A_k ≡ 0（Legendre 关于 0 对称）
def bn_func(k):
    return k * np.sqrt(1.0 / (4 * k * k - 1))   # sqrt(B_k) = k / sqrt(4k^2-1)
f = _ufuncs.eval_legendre           # P_n(x)，用 ufunc 求值
def df(n, x):                       # P_n'(x)，用解析递推而非符号求导
    return (-n * x * _ufuncs.eval_legendre(n, x)
            + n * _ufuncs.eval_legendre(n - 1, x)) / (1 - x ** 2)
```

要点：

- `mu0 = 2.0` 是权函数 1 在 \([-1,1]\) 上的积分，它最终会用来**归一化权重之和**（见 4.2）。
- `an_func` 返回主对角元 \(A_k\)，`bn_func` 返回次对角元 \(\sqrt{B_k}\)。注意 `0.0 * k` 这种写法是为了让返回值是数组形状兼容的广播结果，而不是标量。
- `f` 直接复用 `scipy.special.eval_legendre` 这个 **ufunc**；`df` 不是符号求导，而是 Legendre 多项式导数的解析恒等式 \(\displaystyle (1-x^2)P_n'(x) = n\bigl(P_{n-1}(x) - x P_n(x)\bigr)\)。这两个函数后面既用来「抛光」节点，又用来算权重。

> 承接 u2-l1：`eval_legendre` 是来自 `_ufuncs` 的 ufunc。所以 `_orthogonal.py` 本身是纯 Python，但它「站在 ufunc 的肩膀上」——多项式求值部分由 C 内核的 ufunc 完成，本文件只负责高斯求积的编排逻辑。

#### 4.1.4 代码实践

**目标**：验证你确实读懂了 \(A_n\)、\(B_n\) 的换算，并能把一个多项式家族的递推系数对上号。

**步骤**：

1. 打开 [`_orthogonal.py:24-37`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_orthogonal.py#L24-L37)，抄下 \(A_n\)、\(B_n\)、\(h_n\) 的公式。
2. 阅读上面的 `roots_legendre` 源码片段，确认 `bn_func` 给出的 \(\sqrt{B_k}\) 与 Legendre 的标准结果 \(B_n = \dfrac{n^2}{(2n-1)(2n+1)} = \dfrac{n^2}{4n^2-1}\) 一致。
3. 在 Python 里手动构造同样的 \(\sqrt{B_k}\) 序列，与 `roots_legendre` 内部隐含的次对角线对比。

**示例代码**（请本地运行确认）：

```python
import numpy as np
k = np.arange(1, 6, dtype='d')
sqrtB_legendre = k * np.sqrt(1.0 / (4 * k * k - 1))   # 仿照源码的 bn_func
# Legendre 的标准解析结果 B_n = n^2 / ((2n-1)(2n+1))
sqrtB_ref = np.sqrt(k * k / ((2*k - 1) * (2*k + 1)))
print(np.allclose(sqrtB_legendre, sqrtB_ref))   # 预期为 True
```

**预期现象**：打印 `True`，说明源码里 `bn_func` 写出的确实是 \(\sqrt{B_k}\)。具体数值打印结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Legendre 的 `an_func` 恒返回 0，而 `roots_jacobi` 的 `an_func` 不恒为 0？

**参考答案**：Legendre 多项式关于 \(x=0\) 对称，递推中 \(x\) 的「平移项」\(A_n\) 为 0；Jacobi 多项式 \(P_n^{(\alpha,\beta)}\) 当 \(\alpha\neq\beta\) 时不对称，递推里有非零的 \(A_n = (b^2-a^2)/((2k+a+b)(2k+a+b+2))\)（见 [`roots_jacobi` 的 an_func`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_orthogonal.py#L320-L329)）。

**练习 2**：`df` 为什么用解析递推公式而不是有限差分来近似导数？

**参考答案**：求权重时需要 \(P_n'(x_i)\)（见 4.2.3）。有限差分在节点附近（\(P_n\) 本身接近 0 的地方）会引入不可控误差；解析恒等式 \((1-x^2)P_n'=n(P_{n-1}-xP_n)\) 借助 ufunc `eval_legendre` 给出几乎机器精度的结果。

### 4.2 Golub-Welsch 算法

#### 4.2.1 概念说明

Golub-Welsch（1969）是计算高斯求积节点与权重的经典算法。它的核心洞见是：

> 把三项递推系数排成一个对称三对角矩阵（**Jacobi 矩阵**），这个矩阵的特征值就是高斯求积的节点，而权重由特征向量（或等价的 Christoffel 公式）给出。

记 \(n\) 阶 Jacobi 矩阵为：

\[ J_n = \begin{pmatrix} A_0 & \sqrt{B_1} & & & \\ \sqrt{B_1} & A_1 & \sqrt{B_2} & & \\ & \sqrt{B_2} & A_2 & \ddots & \\ & & \ddots & \ddots & \sqrt{B_{n-1}} \\ & & & \sqrt{B_{n-1}} & A_{n-1} \end{pmatrix} \]

那么 \(J_n\) 的 \(n\) 个特征值 \(\{x_0,\ldots,x_{n-1}\}\) 恰好是第 \(n\) 阶正交多项式 \(P_n\) 的 \(n\) 个根，也就是高斯求积的节点。

> 直觉理解：求多项式的根本质上是解一个特征值问题。三项递推恰好把 \(P_n\) 的「求根」编码成了一个三对角矩阵的特征值——而三对角对称矩阵的特征值有专门的高速、稳定算法（QR 变体的带状求解器）。

#### 4.2.2 核心流程

`_gen_roots_and_weights` 的算法步骤（对应源码 195–241 行）：

1. **组带状矩阵**：构造一个 `(2, n)` 的数组 `c`，`c[1,:]` 是主对角线 \(A_k\)，`c[0,1:]` 是上对角线 \(\sqrt{B_k}\)。
2. **求特征值**：调用 `scipy.linalg.eigvals_banded(c)` 得到 \(n\) 个特征值，即节点 `x`。
3. **抛光节点**：对每个节点做**一步牛顿迭代** \(x \leftarrow x - P_n(x)/P_n'(x)\)，把特征值算法的微小误差再压低一个量级。
4. **算权重**：用 Christoffel 公式 \(w_i \propto 1/(P_{n-1}(x_i)\,P_n'(x_i))\)。
5. **数值稳定化**：因为 \(P_{n-1}\) 与 \(P_n'\) 在高阶下会指数级地很大/很小，先做对数尺度的归一化再相乘。
6. **对称化（可选）**：对关于 0 对称的多项式（`symmetrize=True`），强制 \(w_i=w_{n-1-i}\)、\(x_i=-x_{n-1-i}\)，消除非对称的舍入误差。
7. **归一化**：缩放权重使其和等于 \(\mu_0\)（权函数在区间上的积分）。

注意一个与本讲标题「Golub-Welsch」相关的细节：**经典 Golub-Welsch 用特征向量的第一个分量算权重**，而本实现用「特征值给节点 + Christoffel 公式给权重」的等价路线，省去了求特征向量。`roots_hermite` 的文档串就把这称作「a modified version of the Golub-Welsch algorithm」（见 [`roots_hermite` 文档`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_orthogonal.py#L930-L935)）。

#### 4.2.3 源码精读

整个算法封装在一个函数里：[_orthogonal.py:195-241](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_orthogonal.py#L195-L241)。逐段看：

**(a) 组带状矩阵并求特征值** —— [_orthogonal.py:210-216](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_orthogonal.py#L210-L216)

```python
from scipy import linalg          # 惰性导入，避免给整个模块强加 linalg 依赖（gh-23420）
k = np.arange(n, dtype='d')
c = np.zeros((2, n))
c[0,1:] = bn_func(k[1:])          # 上对角线：sqrt(B_1)..sqrt(B_{n-1})
c[1,:] = an_func(k)               # 主对角线：A_0..A_{n-1}
x = linalg.eigvals_banded(c, overwrite_a_band=True)   # 特征值 = 节点
```

这就是把递推系数摆成 Jacobi 矩阵 \(J_n\)、再求其特征值。`c` 用的是 `scipy.linalg` 的「带状存储约定」：第 0 行存上对角线、第 1 行存主对角线。注释里的「gh-23420」说明这里**惰性导入 `linalg`** 是有意的工程取舍——不让一个只想要 `eval_legendre` 的用户被迫拖入 BLAS/LAPACK 依赖。

**(b) 牛顿抛光节点** —— [_orthogonal.py:218-221](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_orthogonal.py#L218-L221)

```python
y = f(n, x)        # P_n(x)
dy = df(n, x)      # P_n'(x)
x -= y/dy          # 一步牛顿迭代：x <- x - P_n/P_n'
```

特征值解出的节点已经很准，这里再补一步牛顿，把精度推到接近机器精度。注意 `f`、`df` 是调用方（如 `roots_legendre`）传进来的——这就是为什么每个 `roots_*` 都要提供这两个回调。

**(c) 算权重 + 对数稳定化** —— [_orthogonal.py:223-230](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_orthogonal.py#L223-L230)

```python
fm = f(n-1, x)                       # P_{n-1}(x_i)
log_fm = np.log(np.abs(fm))
log_dy = np.log(np.abs(dy))
fm /= np.exp((log_fm.max() + log_fm.min()) / 2.)   # 把 fm 缩到「适中」量级
dy /= np.exp((log_dy.max() + log_dy.min()) / 2.)
w = 1.0 / (fm * dy)                  # Christoffel 公式：w_i ∝ 1/(P_{n-1} P_n')
```

这里用到了高斯求积的 Christoffel 公式：节点 \(x_i\) 处的权重正比于 \(\dfrac{1}{P_{n-1}(x_i)\,P_n'(x_i)}\)。常数因子不影响，因为下一步会整体缩放。对数归一化是关键工程细节——对高阶 Chebyshev/Hermite，\(P_{n-1}\) 与 \(P_n'\) 的绝对值可能跨越几十个数量级，直接相乘会溢出或下溢；先把它们各自除以「几何平均量级」（即 `exp((max+min)/2)`），让乘积落在健壮的数值范围内。

**(d) 对称化与归一化** —— [_orthogonal.py:232-241](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_orthogonal.py#L232-L241)

```python
if symmetrize:                       # 仅对关于 0 对称的多项式（Legendre/Hermite/Gegenbauer）
    w = (w + w[::-1]) / 2
    x = (x - x[::-1]) / 2
w *= mu0 / w.sum()                   # 强制 sum(w) = mu0 = 权函数在区间上的积分
```

`symmetrize=True` 时，理论上有 \(x_i = -x_{n-1-i}\)、\(w_i = w_{n-1-i}\)；这里用「正反两组取平均」显式抹平特征值算法残留的微小不对称。最后 `w *= mu0 / w.sum()` 把权重和精确归一到 \(\mu_0\)——这正是练习里「\(\sum w_i = 2\)」的来源。

> **大 n 的例外路径**：并非所有 `roots_*` 都走 `_gen_roots_and_weights`。`roots_chebyt` 直接用闭式解 \(\cos\)（[`_orthogonal.py:1786-1795`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_orthogonal.py#L1786-L1795)），而 `roots_hermite` 在 \(n>150\) 时改用基于 Airy 函数渐近展开的 `_roots_hermite_asy`（[`_orthogonal.py:973-978`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_orthogonal.py#L973-L978) 与 [`_roots_hermite_asy`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_orthogonal.py#L1301-L1355)）。这是因为在数千阶时，三对角特征值法既慢又失精度，渐近法有线性时间复杂度。Golub-Welsch 是「默认通用引擎」，但不是唯一引擎。

#### 4.2.4 代码实践

**目标**：用 `roots_legendre(5)` 亲手验证 Golub-Welsch 的产物满足高斯求积的基本性质。

**步骤**：

1. 调用 `roots_legendre(5)` 得到节点 `x` 与权重 `w`。
2. 验证 \(\sum_i w_i = 2\)（因为 Legendre 的权函数 \(w(x)=1\) 在 \([-1,1]\) 上积分 \(\mu_0=2\)，而源码用 `w *= mu0/w.sum()` 强制了这一点）。
3. 用 \(f(x)\equiv 1\) 验证 \(\sum_i w_i f(x_i) = \int_{-1}^{1} 1\,dx = 2\)。
4. 进一步用 \(f(x)=x^2\) 验证 \(\sum_i w_i x_i^2 = \int_{-1}^{1} x^2\,dx = 2/3\)（5 点 Legendre 能精确积分到 9 次多项式，\(x^2\) 远在其内）。

**示例代码**（请本地运行确认数值）：

```python
import numpy as np
from scipy.special import roots_legendre

x, w = roots_legendre(5)
print("nodes   =", x)
print("weights =", w)
print("sum(w)            =", w.sum())                 # 期望 2.0
print("sum(w * 1)        =", np.dot(w, np.ones_like(x)))   # 期望 2.0
print("sum(w * x**2)     =", np.dot(w, x**2))         # 期望 2/3 ≈ 0.6667
print("sum(w * x**9)     =", np.dot(w, x**9))         # 期望 2/10 = 0.2（9 次仍精确）
print("sum(w * x**10)    =", np.dot(w, x**10), " vs ", 2/11)  # 10 次开始有误差
```

**预期现象**：

- `sum(w)` 应当极其接近 `2.0`（由 `w *= mu0/w.sum()` 保证）。
- `sum(w * x**2)` 应接近 `0.6667`，`sum(w * x**9)` 应接近 `0.2`——因为 5 点高斯-勒让德精确积分到 \(2\times5-1=9\) 次。
- `sum(w * x**10)` 与精确值 \(2/11\) 开始出现偏差，正好印证「\(2n-1\) 次以上不再精确」。
- 各节点、权重的具体打印值待本地验证；按文档，5 点 Legendre 节点约为 \(\{0,\, \pm 0.53846931,\, \pm 0.90617985\}\)。

#### 4.2.5 小练习与答案

**练习 1**：把 `roots_legendre` 的 `symmetrize` 参数（在它调用 `_gen_roots_and_weights` 时传的是 `True`，见 [`第 2664 行`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_orthogonal.py#L2664)）从概念上改为 `False`，节点/权重的**数值**会变吗？

**参考答案**：理论上完全不变（对称是数学性质），但由于特征值算法的浮点舍入，关掉对称化后会出现量级约 \(10^{-16}\) 的非对称残差。`symmetrize=True` 的意义就是把这点残差抹平，让 \(x_i\) 严格成对为相反数、\(w_i\) 严格成对相等。

**练习 2**：为什么 `_gen_roots_and_weights` 要在算权重前对 `fm`、`dy` 做对数归一化，而不能直接 `w = 1.0/(fm*dy)`？

**参考答案**：高阶多项式 \(P_{n-1}(x_i)\) 与导数 \(P_n'(x_i)\) 的绝对值随 \(n\) 指数增长，相乘极易溢出为 `inf` 或下溢为 0，导致权重全为 `nan`/`inf`。对数归一化（除以几何平均量级）把每个量都拉回 \(O(1)\) 量级再做乘除，乘积的**比例关系**（即各权重的相对大小）不变，但落在可表示范围内。最后再由 `w *= mu0/w.sum()` 把绝对量级补回来。

### 4.3 orthopoly1d

#### 4.3.1 概念说明

`roots_*` 返回的是「节点数组 + 权重数组」两个 numpy 数组，只回答了「怎么数值积分」。但很多时候用户想要的是**多项式本身**——例如把 \(P_3(x) = \tfrac{1}{2}(5x^3-3x)\) 当作一个可调用对象，去绘图、求值、做多项式运算。`legendre(n)`、`jacobi(n,alpha,beta)` 这族函数返回的就是一个 `orthopoly1d` 对象。

`orthopoly1d` 是 `numpy.poly1d` 的子类（[`_orthogonal.py:150`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_orthogonal.py#L150)），在它之上额外携带了：

- `weights`：以 `(节点, 权重, 等效权重)` 三元组打包的求积信息。
- `weight_func`：权函数 \(w(x)\)（用于把通用权重换算回「等效权重」）。
- `limits`：正交区间 \((a,b)\)。
- `normcoef`：范数系数 \(\sqrt{h_n}\)。
- `_eval_func`：一个**更稳定的求值函数**（通常就是对应的 `eval_*` ufunc），优先于系数法使用。

#### 4.3.2 核心流程

构造一个 `orthopoly1d` 的流程（以 `legendre` 为例，[`_orthogonal.py:2705-2720`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_orthogonal.py#L2705-L2720)）：

1. 调用对应的 `roots_*` 拿到节点 `x`、权重 `w`。
2. 查表/公式得到范数 \(h_n\)、首项系数 \(k_n\)、权函数 \(w(x)\)、区间。
3. `orthopoly1d(x, w, hn, kn, wfunc, limits, monic, eval_func=...)` 构造对象。

构造时（`__init__`）做三件事：

- 用 `np.poly1d(roots, r=True)` 从根反解多项式系数（即展开 \(\prod_i (x - x_i)\)），再乘上首项系数 \(k_n\)。
- 打包 `weights`、`weight_func`、`limits`、`normcoef`。
- 存下 `_eval_func`。

调用时（`__call__`）做一件关键的事：**优先用 `_eval_func` 求值，而不是用系数法**。

#### 4.3.3 源码精读

**(a) 类定义与构造** —— [_orthogonal.py:150-176](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_orthogonal.py#L150-L176)

```python
class orthopoly1d(np.poly1d):
    def __init__(self, roots, weights=None, hn=1.0, kn=1.0, wfunc=None,
                 limits=None, monic=False, eval_func=None):
        equiv_weights = [weights[k] / wfunc(roots[k]) for k in range(len(roots))]
        mu = sqrt(hn)
        if monic:                          # 把多项式缩成首一（首项系数=1）
            ...
            mu = mu / abs(kn); kn = 1.0
        poly = np.poly1d(roots, r=True)    # 从根反解系数
        np.poly1d.__init__(self, poly.coeffs * float(kn))   # 再乘首项系数
        self.weights = np.array(list(zip(roots, weights, equiv_weights)))
        self.weight_func = wfunc
        self.limits = limits
        self.normcoef = mu
        self._eval_func = eval_func        # 会在算术运算后丢失（见下）
```

注意两点：

- 系数是从「节点 = 多项式的根」**反解**出来的：`np.poly1d(roots, r=True)`。这意味着系数是数值计算的产物，高阶时会引入误差（这正是 `eval_func` 存在的理由）。
- `equiv_weights = weights/wfunc(roots)` 是「等效权重」——把高斯求积权重除以节点处的权函数值，得到的是一个不带权函数的等价积分权重。

**(b) 调用：优先用稳定求值** —— [_orthogonal.py:178-182](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_orthogonal.py#L178-L182)

```python
def __call__(self, v):
    if self._eval_func and not isinstance(v, np.poly1d):
        return self._eval_func(v)          # 走 eval_* ufunc，数值稳定
    else:
        return np.poly1d.__call__(self, v) # 系数法（用于多项式复合等场景）
```

这是 `orthopoly1d` 设计上最关键的一行：**对普通数值输入，用 `_eval_func`（即底层 `eval_legendre` 等 ufunc）求值，绕开不稳定的系数展开**。只有当输入本身是一个 `np.poly1d`（即做多项式复合，如 `p(q(x))`）时，才退回系数法。承接 u3-l4 与 u2-l1：这里的 `_eval_func` 走的正是编译产物 `_ufuncs` 里的 ufunc，既快又准。

**(c) 缩放方法** —— [_orthogonal.py:184-192](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_orthogonal.py#L184-L192)

```python
def _scale(self, p):
    if p == 1.0:
        return
    self._coeffs *= p
    evf = self._eval_func
    if evf:
        self._eval_func = lambda x: evf(x) * p   # 求值函数也要同步缩放
    self.normcoef *= p
```

`_scale` 用来在「非首一」规范化之间切换（很多 Chebyshev/Gegenbauer 的 `polyfuns` 都先构造一个基准多项式再 `_scale` 一个因子，如 [`chebyu`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_orthogonal.py#L2048-L2053)）。注意它**同时缩放系数和 `_eval_func`**——否则两条求值路径会给出不一致的结果。

> **稳定性警告（衔接 u5-l2）**：`orthopoly1d` 继承自 `numpy.poly1d`，而 NumPy 官方已把 `poly1d` 标记为遗留接口、推荐改用 `numpy.polynomial`。高阶（一般 \(n \gtrsim 20\)）时，由根反解系数再按系数法（Horner）求值会显著失精。这就是 `orthopoly1d` 坚持「优先 `_eval_func`」的原因；也是为什么官方文档建议高阶直接用 `eval_*` ufunc 而非 `legendre(n)` 系数对象。这条线索是下一讲 u5-l2 的主线。

#### 4.3.4 代码实践

**目标**：直观感受 `orthopoly1d` 的「双求值路径」，以及它作为 `np.poly1d` 子类的行为。

**步骤**：

1. 构造 `legendre(3)`，观察它打印出来是一个 `poly1d([2.5, 0, -1.5, 0])`（即 \(\tfrac12(5x^3-3x)\)）。
2. 对它求值，验证 \(P_3(0.5) = \tfrac12(5\cdot 0.125 - 1.5) = -0.4375\)。
3. 观察它的额外属性 `.weights`、`.weight_func`、`.limits`、`.normcoef`。

**示例代码**（请本地运行确认数值）：

```python
import numpy as np
from scipy.special import legendre

p = legendre(3)
print(p)                  # poly1d([ 2.5,  0. , -1.5,  0. ])
print(p(0.5))             # 期望 -0.4375
print("limits  =", p.limits)     # (-1, 1)
print("wfunc?  =", p.weight_func)
print("normcoef=", p.normcoef)
```

**预期现象**：

- `p` 的系数为 `[2.5, 0, -1.5, 0]`，对应 \(\tfrac{1}{2}(5x^3 - 3x)\)。
- `p(0.5)` 约为 `-0.4375`。注意此时走的是 `_eval_func`（即 `eval_legendre(3, 0.5)`），而非系数法——两者在此低阶下结果一致，但语义上 `__call__` 优先选了稳定的那条。
- `p.limits` 为 `(-1, 1)`，`p.weight_func` 是 Legendre 的权函数（恒 1）。
- 具体打印数值待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：既然 `orthopoly1d` 已经存了多项式系数，为什么 `__call__` 还要绕道 `_eval_func`？

**参考答案**：系数是由「节点反解」得到的，再按 Horner/系数法求值在高阶时会放大舍入误差；`_eval_func`（底层 `eval_*` ufunc）用三项递推在 C 内核里直接求值，数值上稳定得多。所以默认走 `_eval_func`，只在多项式复合这种系数法无法避免的场景才退回 `np.poly1d.__call__`。

**练习 2**：`orthopoly1d` 上的算术运算（如 `p1 + p2`）会发生什么？

**参考答案**：算术由父类 `np.poly1d` 处理，结果是普通 `np.poly1d` 而**不再是 `orthopoly1d`**，因此会丢失 `.weights`、`.weight_func`、`_eval_func` 等属性。源码注释 [`第 175 行`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_orthogonal.py#L175) 「`eval_func will be discarded on arithmetic`」正是说明这一点。

## 5. 综合实践

把本讲的三块知识（三项递推 → Golub-Welsch → orthopoly1d）串起来，完成下面这个「亲手复现」小任务：

**任务**：用 `scipy.special` 提供的工具，从「递推系数」一路走到「数值积分」，并对照 `orthopoly1d` 对象。

1. 选定 **Gauss-Legendre** 与 **Gauss-Hermite** 两个家族。
2. 对每个家族，回答三件事：
   - 它的 \(A_n\)、\(\sqrt{B_n}\)、\(\mu_0\) 分别是什么？（从对应 `roots_*` 源码里的 `an_func`/`bn_func`/`mu0` 读出）
   - 调用 `roots_legendre(10)` / `roots_hermite(10)` 得到节点与权重，验证 `w.sum()` 等于该家族的 \(\mu_0\)（Legendre 为 2，Hermite 为 \(\sqrt{\pi}\)）。
   - 用得到的节点权重，数值积分一个已知解析结果的函数：
     - Legendre（区间 \([-1,1]\)，权 1）：\(\int_{-1}^{1} e^x\,dx = e - e^{-1}\)。
     - Hermite（区间 \((-\infty,\infty)\)，权 \(e^{-x^2}\)）：\(\int_{-\infty}^{\infty} x^2 e^{-x^2}\,dx = \sqrt{\pi}/2\)。
3. 再构造 `legendre(5)` 这个 `orthopoly1d` 对象，验证它的根（`p.r`，继承自 `np.poly1d`）与 `roots_legendre(5)` 给出的节点一致——印证 `orthopoly1d` 的系数正是从这些节点反解出来的。

**参考框架代码**（请本地运行填入数值）：

```python
import numpy as np
from scipy.special import roots_legendre, roots_hermite, legendre

# --- Legendre ---
xL, wL = roots_legendre(10)
print("Legendre sum(w)        =", wL.sum(), " (期望 2.0)")
integral_L = np.dot(wL, np.exp(xL))              # ∫e^x w(x)dx, w(x)=1
print("Legendre ∫e^x dx       =", integral_L, " vs ", np.e - 1/np.e)

# --- Hermite ---
xH, wH = roots_hermite(10)
print("Hermite  sum(w)        =", wH.sum(), " (期望 sqrt(pi) ≈", np.sqrt(np.pi), ")")
integral_H = np.dot(wH, xH**2)                    # ∫x^2 e^{-x^2} dx，权已在 w 里
print("Hermite  ∫x^2 exp(-x^2)=", integral_H, " vs ", np.sqrt(np.pi)/2)

# --- orthopoly1d 的根 == roots_legendre 的节点 ---
p = legendre(5)
print("roots match?           ", np.allclose(np.sort(p.r), np.sort(roots_legendre(5)[0])))
```

**自我检查**：

- 两个 `sum(w)` 是否分别精确等于 2 与 \(\sqrt{\pi}\)？（应当是的，因为 `w *= mu0/w.sum()`。）
- 两个数值积分是否在小数点后多位上吻合解析值？（10 点求积对 \(e^x\) 非多项式，会有小误差；对 \(x^2 e^{-x^2}\) 因为被积多项式部分次数低，应非常精确。）
- `roots match?` 是否为 `True`？（这验证了 `orthopoly1d`「系数由节点反解」的构造逻辑。）

## 6. 本讲小结

- `_orthogonal.py` 把每个正交多项式家族的「求节点权重」抽象成同一套模板：填好 `an_func`（主对角元 \(A_n\)）、`bn_func`（次对角元 \(\sqrt{B_n}\)）、`f`/`df`（多项式及其导数的 ufunc 求值），交给通用的 `_gen_roots_and_weights`。
- 三项递推 \(P_{n+1}=(x-A_n)P_n - B_n P_{n-1}\) 的系数被摆成一个对称三对角的 **Jacobi 矩阵**，其特征值就是高斯求积的节点——这就是 **Golub-Welsch 算法** 的核心。
- 本实现是「改良版 Golub-Welsch」：节点取特征值，权重用 Christoffel 公式 \(w_i \propto 1/(P_{n-1}(x_i)P_n'(x_i))\) 并对数稳定化，省去了求特征向量；最后用 `w *= mu0/w.sum()` 把权重和归一到权函数的积分 \(\mu_0\)。
- `roots_*` 是纯 Python（住在 `_orthogonal.py`），而它依赖的 `eval_*`（`eval_legendre` 等）是来自 `_ufuncs` 的 ufunc；这一分工印证了前几讲「多数函数是 ufunc，但求积编排是纯 Python」的认识。
- `orthopoly1d` 继承自 `numpy.poly1d`，额外携带节点/权重/权函数/区间，并坚持用 `_eval_func`（底层 ufunc）求值以规避系数法的高阶失稳；它是「遗留 `poly1d` 接口」的延伸，稳定性上是下一代 `numpy.polynomial` 想取代的对象。
- 闭式解（`roots_chebyt`）与渐近算法（`roots_hermite` 的 \(n>150\) 路径 `_roots_hermite_asy`）说明 Golub-Welsch 是默认通用引擎，但不是唯一引擎——大 \(n\) 或可解析时会切换更优路径。

## 7. 下一步学习建议

- **u5-l2（roots_* 与 eval_*）**：本讲已经多次提到「系数法高阶失稳，应改用 `eval_*`」。下一讲正面对比这两套接口，用 `eval_legendre(25, x)` 与 `legendre(25)` 系数法做数值对照，把这条稳定性主线讲透。建议接着读 [`_orthogonal.py:1580`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_orthogonal.py#L1580) 之后由 Jacobi 派生其余多项式的「`# The remainder of the polynomials can be derived from the ones above`」段落。
- **u5-l3（MultiUFunc）**：本讲的 `eval_*` 一次只算一个阶；想一次返回所有阶（如 `legendre_p_all`）需要 `_multiufuncs.py` 的聚合机制。
- **回到 u2-l1 / u3-l4**：若想深究 `eval_legendre` 这个 ufunc 背后的 C/C++ 内核，可回到代码生成管线（u3）与后端版图（u3-l4）追踪 `_ufuncs` 是怎么从 `functions.json` 生成出来的。
- **阅读 `scipy.linalg.eigvals_banded` 的文档**：理解本讲第 216 行依赖的那个带状特征值求解器，会对 Golub-Welsch 为什么「快」有更具体的认识。
