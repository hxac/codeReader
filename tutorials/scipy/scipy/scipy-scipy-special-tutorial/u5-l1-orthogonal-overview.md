# _orthogonal.py 概览：orthopoly1d 与 Golub-Welsch 算法

## 1. 本讲目标

`scipy.special` 里有一族「正交多项式」函数：`roots_legendre`、`legendre`、`roots_hermite`、`jacobi`、`roots_chebyt`…… 它们全部住在一个文件 [`_orthogonal.py`](_orthogonal.py) 里。本讲带读者从零读懂这个文件的**整体设计**。读完本讲，你应当能够：

- 说清楚什么是「正交多项式的三项递推关系」，以及它如何决定一个对称三对角矩阵（Jacobi 矩阵）。
- 理解 **Golub-Welsch 算法** 的核心思想：把「求高斯求积的节点与权重」转化为「求一个三对角矩阵的特征值」。
- 看懂 `orthopoly1d` 类如何把一组节点与权重封装成一个可以像多项式一样调用的对象，以及它为什么还附带一个 `eval_func`。
- 明白 `_orthogonal.py` 中 `roots_*`（纯 Python，求节点权重）与 `eval_*`（来自 `_ufuncs`，逐元素求值）这两套接口的分工。

本讲只精读 [`_orthogonal.py`](_orthogonal.py) 这一个文件，是 U5「正交多项式与高斯求积」单元的开篇；更细致的 `roots_*` 与 `eval_*` 对比留到 u5-l2，多输出聚合（`MultiUFunc`）留到 u5-l3。

## 2. 前置知识

阅读本讲前，最好已经知道（前置讲义 u1-l4、u2-l1、u2-l2 已建立）：

- **`scipy.special` 里绝大多数函数是 NumPy ufunc**：标量与数组同源、逐元素求值、可广播。本讲里反复出现的 `eval_legendre`、`eval_jacobi`、`eval_hermite` 就是这样的 ufunc，住在编译产物 `_ufuncs` 里。
- **命名空间由多个子模块拼装**：`__init__.py` 用 `from ._orthogonal import *` 把这里的函数提到顶层 `scipy.special` 货架，并用 `__all__` 控制公开 API。

本讲会用到几个新概念，先用大白话解释：

- **高斯求积（Gaussian quadrature）**：用有限个「节点 \(x_i\)」和「权重 \(w_i\)」近似计算一个带权定积分
  \(\int_a^b f(x)\,w(x)\,dx \approx \sum_{i=1}^{n} w_i\, f(x_i)\)。
  巧妙之处在于，只要 \(n\) 个节点选得对（取某个正交多项式的 \(n\) 个根），就能**精确**积分次数不超过 \(2n-1\) 的多项式——这是它比等距梯形/辛普森法更高效的根本原因。
- **正交多项式（orthogonal polynomials）**：一族多项式 \(P_0, P_1, P_2, \ldots\)，关于某个权函数 \(w(x)\) 在区间 \([a,b]\) 上两两「垂直」：
  \(\int_a^b P_m(x)P_n(x)\,w(x)\,dx = 0\) 当 \(m\neq n\)。Legendre、Chebyshev、Hermite、Laguerre、Jacobi 都属于这一族。
- **三项递推（three-term recurrence）**：每个 \(P_{n+1}\) 都能写成它前一项 \(P_n\) 与前两项 \(P_{n-1}\) 的线性组合。这个递推里藏着的系数，正是构造求积节点的钥匙。
- **对称三对角矩阵的特征值**：一个只在主对角线和紧邻的两条次对角线上有非零元素的方阵，叫「三对角矩阵」。它的特征值可以用专用算法（`scipy.linalg.eigvals_banded`）快速、稳定地求出。

## 3. 本讲源码地图

本讲只涉及一个源码文件，但它在概念上分成五块：

| 代码区域 | 行号区间 | 作用 |
| --- | --- | --- |
| 顶部数学注释 | 1–72 | 给出递推关系、\(A_n/B_n\) 系数与 Golub-Welsch 的文献出处 |
| 导入与命名导出 | 78–147 | `_gam`、`_polyfuns`、`_rootfuns_map`、`__all__` 的拼装 |
| `orthopoly1d` 类 | 150–192 | 多项式对象，封装节点、权重、系数与求值函数 |
| `_gen_roots_and_weights` | 195–241 | **Golub-Welsch 的通用实现**，整个文件的心脏 |
| 各 `roots_*` / 多项式函数 | 246–2892 | 给每个多项式家族填入递推系数，调用上面的通用函数 |

其中所有 `roots_*`（如 `roots_legendre`、`roots_jacobi`、`roots_hermite`）都遵循同一个套路：**填好四个回调 `an_func`/`bn_func`/`f`/`df`，再交给 `_gen_roots_and_weights` 干活**。看懂 `_gen_roots_and_weights`，就看懂了整族函数。

## 4. 核心概念与源码讲解

### 4.1 三项递推关系

#### 4.1.1 概念说明

一族正交多项式 \(P_0, P_1, P_2, \ldots\) 总是满足一个**三项递推关系**——每一项只依赖前两项。教科书中，最通用的写法是：

\[ a_{1,n}\, f_{n+1}(x) = (a_{2,n} + a_{3,n}\, x)\, f_n(x) - a_{4,n}\, f_{n-1}(x) \]

其中 \(a_{1,n}, a_{2,n}, a_{3,n}, a_{4,n}\) 是随阶数 \(n\) 变化的常数（每个多项式家族有自己的表达式）。[`_orthogonal.py` 顶部的注释](_orthogonal.py#L10-L22)原原本本地写下了这条式子。

为了构造求积节点，作者把它改写成「标准化」的形式（把 \(x\) 的系数规整成 1）：

\[ P_{n+1}(x) = (x - A_n)\, P_n(x) - B_n\, P_{n-1}(x) \]

两组系数的对应关系是：

\[ A_n = -\frac{a_{2,n}}{a_{3,n}} \qquad B_n = \left(\frac{a_{4,n}}{a_{3,n}} \sqrt{\frac{h_{n-1}}{h_n}}\right)^2 \]

其中 \(h_n = \int_a^b w(x)\, f_n(x)^2\, dx\) 是第 \(n\) 阶多项式的「范数平方」。注意 \(B_n\) 里那个 \(\sqrt{h_{n-1}/h_n}\) 因子，是因为从通用形式 \(f\) 改写到标准化形式 \(P\) 时换了规范化（\(P\) 不一定是首一的），需要用范数比来补偿。

#### 4.1.2 核心流程

把递推关系标准化后，整个求积节点问题被压缩成两串数：

- 数组 \(A_0, A_1, \ldots, A_{n-1}\)：将出现在矩阵的**主对角线**。
- 数组 \(\sqrt{B_1}, \sqrt{B_2}, \ldots, \sqrt{B_{n-1}}\)：将出现在矩阵的**次对角线**（取平方根是为了让矩阵对称）。

每个多项式家族（Legendre、Hermite、Jacobi……）只需要给出「如何由 \(k\) 算出 \(A_k\) 与 \(\sqrt{B_k}\)」的两条公式，剩下的事就交给 4.2 的 Golub-Welsch 通用代码。这是一种典型的**「数据驱动 + 单一通用算法」**工程模式。

#### 4.1.3 源码精读

文件顶部第 10–45 行用 `.. math::` 把上面两组公式直接写进文档字符串，既是给读者看的数学说明，也是给后续实现当作「规格书」：

- [_orthogonal.py#L10-L22](_orthogonal.py#L10-L22)：通用递推与标准化递推两式并排，交代 \(P\) 与 \(f\) 规范化不同。
- [_orthogonal.py#L24-L36](_orthogonal.py#L24-L36)：给出 \(A_n\)、\(B_n\) 与 \(h_n\) 的换算公式——这就是本模块「从教科书公式到代码」的桥梁。
- [_orthogonal.py#L46-L72](_orthogonal.py#L46-L72)：列两篇关键文献：Golub & Welsch 1969 的原文，以及 Townsend–Trogdon–Olver 2014/2015 的大规模快速算法（仅 `roots_hermite` 在 \(n>150\) 时用到）。

以 `roots_legendre` 为例，看它如何把数学系数填成代码（注意 `bn_func` 返回的是 \(\sqrt{B_n}\) 而不是 \(B_n\)）：

```python
mu0 = 2.0                       # ∫_{-1}^{1} 1 dx = 2，即权函数的积分
def an_func(k):
    return 0.0 * k              # Legendre 对称，所有 A_n = 0
def bn_func(k):
    return k * np.sqrt(1.0 / (4 * k * k - 1))   # sqrt(B_n)
```

完整代码见 [roots_legendre 实现](_orthogonal.py#L2651-L2664)。`an_func` 全为 0 正反映了 Legendre 多项式关于原点对称（节点必关于 0 对称）。

#### 4.1.4 代码实践

1. **目标**：直观感受「两条系数公式就定义了一族正交多项式」。
2. **步骤**：
   - 打开 [roots_legendre 实现](_orthogonal.py#L2651-L2664)，对照本节公式手算 \(B_1 = 1^2/(4\cdot1-1)=1/3\)，故 `bn_func(1)=sqrt(1/3)≈0.5774`。
   - 再看 [roots_hermite 实现](_orthogonal.py#L963-L972)，记下它的 `an_func`/`bn_func`，与 Legendre 对比。
3. **观察**：两个家族的 `an_func` 都恒为 0（因为 Hermite、Legendre 都关于原点对称），但 `bn_func` 截然不同——Legendre 是 \(k/\sqrt{4k^2-1}\)，Hermite 是 \(\sqrt{k/2}\)。
4. **预期结果**：你会确信「换系数即换多项式家族」，而 `_gen_roots_and_weights` 一行都不用改。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `bn_func` 返回的是 \(\sqrt{B_n}\) 而不是 \(B_n\)？

**参考答案**：因为 4.2 里要把 \(B_n\) 放进对称三对角矩阵的次对角线，对称矩阵的次对角线元素必须满足「上下成对且相等」。把 \(\sqrt{B_n}\) 同时放在 \((k,k+1)\) 与 \((k+1,k)\) 两个位置，其乘积正好是 \(B_n\)，从而构造出的 Jacobi 矩阵是实对称的，特征值全部为实数、且数值稳定。

**练习 2**：`roots_legendre` 里 `mu0 = 2.0` 的几何含义是什么？

**参考答案**：`mu0` 是权函数 \(w(x)=1\) 在区间 \([-1,1]\) 上的积分 \(\int_{-1}^{1}1\,dx=2\)。它等于所有求积权重之和 \(\sum_i w_i\)，4.2 末尾会用它把权重整体归一化。

---

### 4.2 Golub-Welsch 算法

#### 4.2.1 概念说明

Golub-Welsch 算法（1969）是一条优美的等价：

> **\(n\) 点高斯求积的节点，正好是某个 \(n\times n\) 对称三对角矩阵（Jacobi 矩阵）的特征值；而权重由对应特征向量（或等价的 Christoffel 公式）给出。**

这个三对角矩阵 \(J\) 用上一节的递推系数构造：

\[ J = \begin{pmatrix} A_0 & \sqrt{B_1} & & \\ \sqrt{B_1} & A_1 & \sqrt{B_2} & \\ & \sqrt{B_2} & \ddots & \ddots \\ & & \ddots & A_{n-1} \end{pmatrix} \]

主对角线是 \(A_0,\ldots,A_{n-1}\)，次对角线是 \(\sqrt{B_1},\ldots,\sqrt{B_{n-1}}\)。求 \(J\) 的特征值，就一次拿到全部 \(n\) 个节点——比「逐个用牛顿法找根」稳定得多，因为特征值算法（QL/QR）对对称三对角矩阵有极高的精度。

#### 4.2.2 核心流程

`_gen_roots_and_weights` 的执行流程可以概括为四步：

1. **组装带状矩阵**：把 `an_func(k)` 放主对角线、`bn_func(k[1:])` 放次对角线，得到一个 `(2,n)` 的带状表示。
2. **求特征值 = 节点**：调用 `scipy.linalg.eigvals_banded` 求对称三对角矩阵的特征值，得到 \(n\) 个节点 \(x\)。
3. **牛顿法精修节点**：用底层 `eval_*` ufunc 对每个节点做**一步**牛顿迭代 \(x \leftarrow x - f(n,x)/f'(n,x)\)，把特征值算法残留的微小误差压到机器精度。
4. **算权重并归一化**：用 Christoffel 公式 \(w_i \propto 1/\big(|P_{n-1}(x_i)|\cdot|P_n'(x_i)|\big)\)，再整体缩放使 \(\sum_i w_i = \mu_0\)。其中 `f`/`df` 是求值与求导的两个回调（通常是 `_ufuncs.eval_*`）。

伪代码如下：

```
k = [0, 1, ..., n-1]
c[0, 1:] = bn_func(k[1:])     # 次对角线 = sqrt(B_1..B_{n-1})
c[1, :]  = an_func(k)         # 主对角线 = A_0..A_{n-1}
x = eigvals_banded(c)          # 特征值 → 节点（Golub-Welsch 的核心）
x -= f(n, x) / df(n, x)        # 一步牛顿精修
w  = 1 / (f(n-1, x) * df(n, x))   # Christoffel 权重
w *= mu0 / w.sum()             # 归一化使 ∑w = μ₀
若对称家族: 对称化 x、w（消除数值不对称的微小漂移）
```

> 备注：这里用 Christoffel 公式（而非「特征向量第一分量平方」）来算权重，是因为前者复用了已经在手的 `eval_*` ufunc，无需从 `eigvals_banded` 取特征向量（`eigvals_banded` 只返回特征值）。`symmetrize` 参数是给对称家族（如 Legendre、Hermite）用的「数值对称化」补丁——理论上节点本应关于 0 对称，实际算出来有微小漂移，强制对称可进一步提升精度。

#### 4.2.3 源码精读

整个函数体在 [`_gen_roots_and_weights`](_orthogonal.py#L195-L241)，逐段对应上面的流程：

- [_orthogonal.py#L210-L216](_orthogonal.py#L210-L216)：惰性导入 `scipy.linalg`（注释 `gh-23420` 说明这是为了避免给整个模块引入 linalg 依赖），组装 `(2,n)` 带状矩阵并求特征值——**这一行就是 Golub-Welsch 的全部灵魂**。
- [_orthogonal.py#L218-L221](_orthogonal.py#L218-L221)：对每个节点做一步牛顿迭代精修。`f` 是第 \(n\) 阶多项式、`df` 是它的导数，两者都来自调用方传入的回调。
- [_orthogonal.py#L223-L230](_orthogonal.py#L223-L230)：权重的 Christoffel 公式。注意 `fm`、`dy` 可能同时含极大/极小值，直接相乘会损失精度，作者先取对数、用「最大值+最小值的平均」做**对数归一化**再相乘——这是一个很值得学的数值技巧。
- [_orthogonal.py#L232-L236](_orthogonal.py#L232-L236)：对称家族的数值对称化（`symmetrize=True` 时），以及用 `mu0` 把权重缩放成和为 \(\mu_0\)。
- [_orthogonal.py#L238-L241](_orthogonal.py#L238-L241)：`mu` 参数控制是否额外返回权重之和。

#### 4.2.4 代码实践

1. **目标**：用 `roots_legendre` 验证 Golub-Welsch 的产物确实满足高斯求积的精度承诺。
2. **步骤**（在已安装 SciPy 的环境运行）：

   ```python
   import numpy as np
   from scipy.special import roots_legendre, eval_legendre

   x, w = roots_legendre(5)
   print("节点 x =", x)
   print("权重 w =", w)

   # (1) 节点应是 P_5 的根：在节点处 P_5(x) ≈ 0
   print("P_5(x) =", eval_legendre(5, x))

   # (2) 验证 ∑ w_i f(x_i) 对 f(x)=1 等于 ∫_{-1}^{1} 1 dx = 2
   print("权重之和 =", w.sum(), "（应为 2）")

   # (3) 验证精确积分：n=5 应精确积分到 2n-1=9 次多项式
   #     取 f(x) = x^8 与 x^10 对比
   print("∫x^8 近似 =", np.sum(w * x**8), " 真值 =", 2/9)
   print("∫x^10 近似 =", np.sum(w * x**10), " 真值 =", 2/11)
   ```

3. **观察现象**：`P_5(x)` 各分量应接近 0（数量级 \(10^{-16}\)）；权重和应等于 `2.0`；`∫x^8` 应与真值高度吻合，而 `∫x^10`（次数 \(10 > 2\cdot5-1=9\)）开始出现偏差。
4. **预期结果**：这验证了「\(n\) 点高斯求积精确积分次数 \(\le 2n-1\) 的多项式」这一承诺，也说明节点确为 \(P_5\) 的根——即 Golub-Welsch 算对了。

#### 4.2.5 小练习与答案

**练习 1**：为什么第 3 步牛顿迭代只做**一步**，而不是迭代到收敛？

**参考答案**：`eigvals_banded` 给出的特征值已经非常接近真实根（误差通常已是机器精度量级），牛顿法在根附近是**二阶收敛**的，一步就足以把残余误差平方掉，达到满精度。多做几步既无收益，又可能因为导数 `df` 在节点附近也参与计算而引入额外舍入，得不偿失。

**练习 2**：`roots_hermite` 在 \(n>150\) 时**不走** `_gen_roots_and_weights`，改调 `_roots_hermite_asy`。请结合 [roots_hermite 实现](_orthogonal.py#L958-L978) 与顶部文献说明这是为什么。

**参考答案**：Golub-Welsch 的复杂度为 \(O(n^2)\)（特征值问题），当 \(n\) 达到几千时既慢又会因 \(\sqrt{B_n}=\sqrt{n/2}\) 增长过大而损失精度。Townsend–Trogdon–Olver 给出的渐近算法是 \(O(n)\) 且数值稳定，所以大 \(n\) 时切换路径。这是「同一接口、内部按规模切换算法」的典型工程取舍。

---

### 4.3 orthopoly1d 类

#### 4.3.1 概念说明

`roots_*` 解决了「求节点与权重」，而 `legendre(n)`、`jacobi(n,...)` 这类函数（不带 `roots_` 前缀）返回的是一个**多项式对象**——你可以像调用函数一样 `legendre(3)(x)` 得到 \(P_3(x)\) 的值，也可以用 `legendre(3).coef` 看它的系数。这个对象就是 `orthopoly1d`。

它继承自 `numpy.poly1d`，但比普通 `poly1d` 多记三样东西：

- `weights`：配套的高斯求积节点与权重（可直接拿去做数值积分）。
- `weight_func` 与 `limits`：所属多项式的权函数与正交区间（用于自查）。
- `_eval_func`：一个**底层 ufunc 求值函数**（如 `eval_legendre`），用于高精度求值。

> **为什么要 `_eval_func`？** 因为 `numpy.poly1d` 用**系数**（幂基 \(c_0 + c_1 x + \cdots\)）求值，对高阶多项式数值极不稳定（系数随阶数爆炸/抵消）。`orthopoly1d` 在被调用时优先走 `_eval_func`（即稳定的 `eval_*` ufunc），只有在做多项式代数运算（加减乘）时才退化为系数运算——这也是官方文档反复警告「高阶不要用系数法、改用 `eval_*`」的根源（见 u5-l2）。

#### 4.3.2 核心流程

`orthopoly1d` 的构造与调用流程：

1. **构造**：传入一组根（节点）、权重、范数 `hn`、首一系数 `kn`、权函数 `wfunc`、区间 `limits`、是否归一为 `monic`、以及求值函数 `eval_func`。
2. **算系数**：用 `np.poly1d(roots, r=True)` 从根反推多项式系数（即 \(\prod_i (x-x_i)\)），再乘上首一系数 `kn` 缩放。
3. **存权重**：把 `(节点, 权重, 等价权重)` 三元组打包存进 `self.weights`。
4. **调用**：`__call__` 时若设有 `_eval_func` 且入参不是 `poly1d`，就委托给稳定的 ufunc 求值；否则走 `numpy.poly1d` 的系数求值。

每个多项式家族的构造函数（如 `legendre`）都是先调 `roots_*` 拿到节点权重，再把这些信息打包给 `orthopoly1d`。例如 [legendre 构造函数](_orthogonal.py#L2705-L2720)：先 `roots_legendre(n1)` 取节点，算出范数 `hn = 2/(2n+1)` 与首一系数 `kn`，然后 `orthopoly1d(x, w, hn, kn, wfunc=..., eval_func=lambda t: eval_legendre(n, t))`。

#### 4.3.3 源码精读

`orthopoly1d` 类完整定义在 [_orthogonal.py#L150-L192](_orthogonal.py#L150-L192)，要点：

- [_orthogonal.py#L152-L168](_orthogonal.py#L152-L168)：`__init__`。先算「等价权重」`equiv_weights = weights[k]/wfunc(roots[k])`（这是把带权求积权重折算成普通求积权重用的）；处理 `monic`（把多项式缩成首一）；用 `np.poly1d(roots, r=True)` 从根建系数，再 `poly.coeffs * kn` 缩放，最后调父类 `np.poly1d.__init__` 完成初始化。
- [_orthogonal.py#L170-L176](_orthogonal.py#L170-L176)：保存 `weights`（节点/权重/等价权重三元组的数组）、`weight_func`、`limits`、`normcoef`（范数），以及 `_eval_func`。注释明确写「`eval_func` 在算术运算后会丢失」——因为加减乘没有对应的 ufunc 路径。
- [_orthogonal.py#L178-L182](_orthogonal.py#L178-L182)：`__call__` 的「双轨求值」——有 `_eval_func` 就走稳定 ufunc，否则退化到系数求值。
- [_orthogonal.py#L184-L192](_orthogonal.py#L184-L192)：`_scale` 内部缩放方法，同时缩放系数、求值函数与范数。

#### 4.3.4 代码实践

1. **目标**：观察 `orthopoly1d` 的「双轨求值」，理解 `_eval_func` 的作用与高阶系数法的不稳定性。
2. **步骤**：

   ```python
   import numpy as np
   from scipy.special import legendre, eval_legendre

   p = legendre(3)              # 返回一个 orthopoly1d 对象
   print(type(p))               # <class 'scipy.special._orthogonal.orthopoly1d'>
   print(p)                     # 打印系数形式 poly1d([2.5, 0., -1.5, 0.])
   print(p.coef)                # 系数 [2.5, 0, -1.5, 0]
   print(p.weights)             # 配套的 (节点, 权重, 等价权重) 三元组

   # 双轨求值：p(x) 内部走 _eval_func (= eval_legendre(3, x))
   x = np.linspace(-1, 1, 5)
   print(p(x) - eval_legendre(3, x))   # 应全为 0，证明走的是同一条 ufunc

   # 高阶不稳定性演示（u5-l2 会展开）
   p25 = legendre(25)
   print("系数法 vs eval_ufunc 在 x=0.3 的差异:",
         p25(0.3) - eval_legendre(25, 0.3))   # 差异可能显著
   ```

3. **观察**：`p(x)` 与 `eval_legendre(3, x)` 完全一致（差异为 0），证实 `__call__` 委托给了 `_eval_func`。`p25(0.3)` 与 `eval_legendre(25, 0.3)` 的差异在高阶时变大——这正是系数法不稳定的证据。
4. **预期结果**：低阶时两种求值吻合；高阶（如 25）时 `orthopoly1d` 因为内部走 `_eval_func` 仍然准确，而它暴露的 `coef` 系数若被外部拿来手动求值则会失真。

#### 4.3.5 小练习与答案

**练习 1**：注释 [_orthogonal.py#L175](_orthogonal.py#L175) 写「`eval_func` will be discarded on arithmetic」。请解释为什么。

**参考答案**：两个 `orthopoly1d` 相加/相乘后，得到的新多项式不再是某个标准正交族（比如 \(P_2 + P_3\) 不再是 Legendre 多项式），因此没有对应的 `eval_*` ufunc 可用。父类 `numpy.poly1d` 的算术运算只合并系数，所以 `_eval_func` 必须被丢弃，新对象只能退化为系数求值。

**练习 2**：`equiv_weights = weights[k] / wfunc(roots[k])` 的 `equiv_weights` 有什么用？

**参考答案**：高斯求积权重 \(w_i\) 是针对**带权**积分 \(\int f(x)\,w(x)\,dx \approx \sum w_i f(x_i)\) 的。若你想做的是**无权**积分 \(\int g(x)\,dx\)，令 \(g(x) = f(x)\,w(x)\) 即 \(f(x)=g(x)/w(x)\)，对应的权重就变成 \(w_i / w(x_i)\)，即 `equiv_weights`。它把带权求积规则折算成等价的无权规则，方便在不同积分约定间换算。

## 5. 综合实践

把本讲三块知识（三项递推、Golub-Welsch、orthopoly1d）串成一个小任务：**手工模拟** `roots_legendre(3)` 的前半段，并与库函数对照。

任务步骤：

1. 取 \(n=3\)，按 [roots_legendre 实现](_orthogonal.py#L2651-L2664) 的公式写出 `an_func`、`bn_func`：
   - \(A_0=A_1=A_2=0\)；
   - \(\sqrt{B_1}=1\cdot\sqrt{1/(4-1)}=\sqrt{1/3}\)，\(\sqrt{B_2}=2\cdot\sqrt{1/(16-1)}=2/\sqrt{15}\)。
2. 手工组装 \(3\times3\) 对称三对角矩阵 \(J\)（主对角全 0，次对角为 \(\sqrt{B_1},\sqrt{B_2}\)）。
3. 用 `numpy.linalg.eigvalsh(J)` 求 \(J\) 的特征值，记为 `x_hand`。
4. 调用 `roots_legendre(3)` 得到 `x_lib, w_lib`，比较 `x_hand` 与 `x_lib`（应几乎一致，微小差异来自库内多做的一步牛顿精修）。
5. 用 `eval_legendre(3, x_lib)` 验证这些点是 \(P_3\) 的根；再用 `legendre(3)` 返回的 `orthopoly1d` 对象验证 `legendre(3)(x) == eval_legendre(3, x)`。

参考脚本（示例代码）：

```python
import numpy as np
from scipy.special import roots_legendre, eval_legendre, legendre

# 1-2. 手工组装 Jacobi 矩阵
b1 = np.sqrt(1/3); b2 = 2/np.sqrt(15)
J = np.array([[0, b1, 0],
              [b1, 0, b2],
              [0, b2, 0]])
x_hand = np.sort(np.linalg.eigvalsh(J))

# 3. 库函数
x_lib, w_lib = roots_legendre(3)

print("手工特征值 :", x_hand)
print("库节点     :", x_lib)
print("差异       :", np.abs(x_hand - x_lib))   # 很小
print("P_3(节点)  :", eval_legendre(3, x_lib))   # ≈ 0

# 4. orthopoly1d 双轨求值一致
p = legendre(3)
x = np.linspace(-1, 1, 7)
print("orthopoly1d 与 eval 一致:", np.allclose(p(x), eval_legendre(3, x)))
```

预期：手工特征值与库节点几乎一致（库版本因多做一步牛顿迭代略更精确）；`P_3` 在节点处约为 0；`orthopoly1d` 调用结果与 `eval_legendre` 完全一致。

## 6. 本讲小结

- `_orthogonal.py` 把全部正交多项式相关的 `roots_*` 与构造函数集中在一个文件，靠**单一通用算法** `_gen_roots_and_weights` 服务所有家族——每个家族只需贡献「递推系数」两条公式。
- **三项递推关系**标准化为 \(P_{n+1}=(x-A_n)P_n-B_nP_{n-1}\)，系数 \(A_n\)、\(\sqrt{B_n}\) 分别成为对称三对角矩阵的主、次对角线。
- **Golub-Welsch 算法**：求积节点 = 该三对角矩阵的特征值（`eigvals_banded`），再配一步牛顿精修 + Christoffel 公式算权重，并对称化与归一化。
- `orthopoly1d` 继承 `numpy.poly1d`，但额外存节点/权重/权函数，并保留一个 `_eval_func`（底层 `eval_*` ufunc）用于**稳定求值**，规避高阶系数法的数值灾难。
- 大规模场景下（`roots_hermite` 的 \(n>150\)）会切换到 Townsend–Trogdon–Olver 的 \(O(n)\) 渐近算法，体现「同接口、按规模换算法」的工程取舍。
- 本讲只覆盖求积与构造的「主链路」；`roots_*` 与 `eval_*` 的细致分工与高阶数值稳定性留到 u5-l2，多输出聚合 `MultiUFunc` 留到 u5-l3。

## 7. 下一步学习建议

- **下一步读 u5-l2**：深入对比 `roots_*`（本讲的求积接口）与 `eval_*`（逐元素求值的 ufunc），理解为什么文档警告「高阶（>20）别用系数法、改用 `eval_*`」，并亲手观察高阶系数法的精度损失。
- **横向联系 u2-l1**：本讲反复出现的 `eval_legendre`、`eval_jacobi`、`eval_hermite` 都是 NumPy ufunc，其类型签名、广播规则已在 u2-l1 建立过；可回头对照 `.types` 验证它们支持 float/complex 多类型分发。
- **延伸阅读源码**：若想看更多家族如何「填系数」，可读 [roots_jacobi 实现](_orthogonal.py#L301-L342)（带 \(\alpha,\beta\) 参数，`an_func`/`bn_func` 更复杂）与 [roots_chebyt 实现](_orthogonal.py#L1748-L1797)；它们与本讲的 `roots_legendre` 共享同一套 Golub-Welsch 骨架。
