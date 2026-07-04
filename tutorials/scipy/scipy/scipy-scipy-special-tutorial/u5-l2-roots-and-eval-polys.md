# roots_\* 与 eval_\*：求积节点 vs 多项式求值

## 1. 本讲目标

u5-l1 读完 `_orthogonal.py` 的整体设计后，本讲把镜头拉近，专门比较 `scipy.special` 里**看起来很像、实则来自两个不同世界**的两类正交多项式接口：

- `eval_legendre`、`eval_jacobi`、`eval_hermite`…… 这类 **`eval_*`** 函数；
- `roots_legendre`、`roots_jacobi`、`roots_hermite`…… 这类 **`roots_*`** 函数。

读完本讲，你应当能够：

- 区分 `roots_*` 与 `eval_*` 的**用途差异**（求积节点/权重 vs 逐元素求值）和**实现来源差异**（`_orthogonal.py` 纯 Python 编排 vs `_ufuncs` 编译出的 C 内核 ufunc）。
- 说清楚为什么「用多项式系数去求值」在高阶时会**数值失稳**，以及 SciPy 用什么手段（`orthopoly1d.__call__` 里路由到 `eval_*`）来规避它。
- 在自己的代码里**正确选型**：要积分就用 `roots_*`，要求值（尤其高阶）就用 `eval_*`，要系数对象就留意它做算术后会退化。

本讲精读两个文件：[`_orthogonal.py`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_orthogonal.py) 与 [`_ufuncs.pyi`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_ufuncs.pyi)，并对照 [`__init__.py`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/__init__.py) 文档串里对三组函数的划分。

## 2. 前置知识

阅读本讲前，最好已经知道（u2-l1、u5-l1 已建立）：

- **特殊函数大多是 NumPy ufunc**：标量与数组同源、逐元素求值、可广播，类型由 `.types` 里登记的 loop 决定。`eval_*` 就是这样的 ufunc。
- **高斯求积与 Golub-Welsch**：用 \(n\) 个「节点 \(x_i\)」与「权重 \(w_i\)」近似 \(\int_a^b f(x)w(x)\,dx \approx \sum_i w_i f(x_i)\)，节点取某个正交多项式的根；Golub-Welsch 把「找节点」转化为「求对称三对角矩阵的特征值」。`roots_*` 就是干这件事的。

本讲会用到几个新概念，先用大白话解释：

- **多项式的系数表示 vs 求值**：一个 \(n\) 次多项式可以存成一组系数 \([c_n, c_{n-1}, \ldots, c_0]\)，再用 Horner 法（即 `numpy.polyval`）逐项算出 \(P(x)\)。这叫「系数表示」求值。
- **根→系数映射的病态（Wilkinson 现象）**：从一组根反推系数，对根的微小扰动极其敏感——著名的 *Wilkinson 多项式* 证明：仅改动最高次系数末位几位，根就会剧烈漂移。反过来，把高阶正交多项式的根（数值近似）乘成系数，**系数末尾的有效数字会被严重污染**，再拿这套污染过的系数去做 Horner 求值，结果自然失真。
- **递推求值**：不去碰系数，而是直接用三项递推 \(P_{n+1}=(x-A_n)P_n-B_nP_{n-1}\) 从 \(P_0、P_1\) 一路推到 \(P_n\)。这避开了系数表示，**数值上稳定得多**——这正是 `eval_*` ufunc 内核的求值方式。
- **逐元素 vs 跨元素**：ufunc 只能逐元素求值，输出形状由输入广播决定；高斯求积要做加权和 \(\sum_i w_i f(x_i)\)，是**跨元素聚合**，因此 `roots_*` 不可能是 ufunc，只能是返回数组、让你自己在 Python 层求和的普通函数。

## 3. 本讲源码地图

| 文件 / 区域 | 行号区间 | 本讲关注什么 |
| --- | --- | --- |
| [`_orthogonal.py`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_orthogonal.py) — `orthopoly1d` 类 | 150–192 | 系数对象的求值为何能保持稳定（`_eval_func` 路由） |
| 同上 — `_gen_roots_and_weights` | 195–241 | `roots_*` 的通用引擎，内部**依赖 `eval_*`** |
| 同上 — `roots_jacobi` 的 `f`/`df` | 338–341 | `roots_*` 怎么调用 `eval_*` |
| 同上 — `roots_legendre` | 2543–2664 | 典型 `roots_*`：填四个回调交给引擎 |
| 同上 — `roots_chebyt`（闭式） | 1786–1795 | 不走 Golub-Welsch 的反例：解析公式直接给节点权重 |
| 同上 — `roots_hermite`（大 \(n\) 分支） | 958–978 | 另一种非 Golub-Welsch 路径：渐近算法 |
| [`_ufuncs.pyi`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_ufuncs.pyi) — `eval_*` 声明 | 351–361 | `eval_*` 的静态身份：`np.ufunc` |
| 同上 — `__all__` 清单 | 65–79 | `eval_*` 名单属于 `_ufuncs.__all__` |
| [`__init__.py`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/__init__.py) — 正交多项式章节 | 508–562 | 三组函数的官方划分与稳定性告诫 |
| 同上 — 导入 `_orthogonal` | 803–804 | `roots_*`/`orthopoly1d` 如何被提到顶层命名空间 |

一句话总览：**`eval_*` 住在 `_ufuncs`（编译产物，逐元素 ufunc）；`roots_*` 住在 `_orthogonal.py`（纯 Python 求积编排），并且 `roots_*` 在内部恰恰是 `eval_*` 的「大客户」**——它要用 `eval_*` 来给节点做牛顿抛光、按 Christoffel 公式算权重。

## 4. 核心概念与源码讲解

### 4.1 roots_\* 求积接口：高斯求积的纯 Python 编排

#### 4.1.1 概念说明

`roots_legendre(n)`、`roots_jacobi(n, a, b)`、`roots_hermite(n)`…… 这一族函数回答的问题是：**给我一套高斯求积的节点 \(x_i\) 和权重 \(w_i\)，让我能做数值积分**。它们的统一契约是：

- 输入：阶数 `n`（一个标量整数），外加该家族的参数（如 Jacobi 的 \(\alpha,\beta\)）。
- 输出：两个长度为 `n` 的一维数组 `(x, w)`，可选再返回权重之和 `mu`。

注意输出的**长度由标量 `n` 决定，而不是由某个输入数组的形状决定**——这正是 u2-l2/u4-l2 反复强调的「非 ufunc 特征」：ufunc 的输出形状只能由输入广播决定，而这里输出长度等于 `n`，所以 `roots_*` 注定是普通 Python 函数，住在 `_orthogonal.py` 里，由 `from ._orthogonal import *` 提到命名空间（见 [`__init__.py:803-804`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/__init__.py#L803-L804)）。

#### 4.1.2 核心流程

每个 `roots_*` 都套用同一个模板（u5-l1 已讲过原理，这里只回顾与求值相关的环节）：

1. 校验 `n` 是正整数。
2. 给出本家族的递推系数 `an_func(k)→A_k`、`bn_func(k)→√B_k`。
3. **给出两个回调 `f` 和 `df`**：它们能在任意一组点 `x` 上**批量**求出 \(P_n(x)\) 与 \(P_n'(x)\)。关键在于，这两个回调的实现几乎全是「调用某个 `eval_*` ufunc」。
4. 把 `n`、权函数积分 `mu0`、四个回调交给通用引擎 `_gen_roots_and_weights`，它：
   - 用 `an_func`/`bn_func` 摆出对称三对角矩阵，求特征值得初始节点；
   - 用 `f`/`df` 做一步牛顿法抛光节点：\(x \leftarrow x - f(n,x)/df(n,x)\)；
   - 用 Christoffel 公式 \(w_i \propto 1/(P_{n-1}(x_i)\,P_n'(x_i))\) 算权重；
   - 把权重归一到 `w *= mu0 / w.sum()`。

第 3、4 步揭示了一个反直觉的事实：**`roots_*` 表面上和 `eval_*` 是「两套并行接口」，实际上 `roots_*` 的实现根本离不开 `eval_*`**——没有 `eval_*` 提供稳定的 \(P_n(x)\)、\(P_n'(x)\)，`roots_*` 就既没法抛光节点，也没法按 Christoffel 公式定权重。

#### 4.1.3 源码精读

通用引擎 `_gen_roots_and_weights` 在 [_orthogonal.py:195-241](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_orthogonal.py#L195-L241)。其中与「求值」直接相关的是牛顿抛光与 Christoffel 权重这两段：

- [_orthogonal.py:218-230](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_orthogonal.py#L218-L230)：先 `y = f(n, x); dy = df(n, x); x -= y/dy` 做一步牛顿抛光；再取 \(P_{n-1}\) 与 \(P_n'\)，对它们做对数归一化（`log_fm`/`log_dy` 减去中点）以维持 `fm*dy` 乘积的精度，最后 `w = 1.0 / (fm * dy)`。这里 `f`、`df` 就是从外面传进来的回调。
- [_orthogonal.py:236](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_orthogonal.py#L236)：`w *= mu0 / w.sum()`，把权重整体缩放到权函数的积分 \(\mu_0\)。

那 `f`、`df` 长什么样？以 `roots_jacobi` 为例：

[roots_jacobi 的 f/df，_orthogonal.py:338-341](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_orthogonal.py#L338-L341) —— 注意它们**就是 `_ufuncs.eval_jacobi` 的直接调用**：

```python
def f(n, x):
    return _ufuncs.eval_jacobi(n, a, b, x)
def df(n, x):
    return 0.5 * (n + a + b + 1) * _ufuncs.eval_jacobi(n - 1, a + 1, b + 1, x)
```

`df` 用了 Jacobi 多项式的导数恒等式 \(P_n'{}^{(\alpha,\beta)}(x)=\tfrac12(n+\alpha+\beta+1)P_{n-1}^{(\alpha+1,\beta+1)}(x)\)，于是「求导」被转换成「换参数再求值一次」，仍落到 `eval_jacobi` 这个 ufunc 上。`roots_legendre`（[_orthogonal.py:2660-2663](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_orthogonal.py#L2660-L2663)）、`roots_genlaguerre`（[_orthogonal.py:649-653](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_orthogonal.py#L649-L653)）、`roots_hermite`（[_orthogonal.py:969-971](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_orthogonal.py#L969-L971)）的 `f`/`df` 全是同一套写法。

要记住一个反例：**并非所有 `roots_*` 都走 Golub-Welsch**。两类家族有更便宜的闭式/特例路径，能绕开特征值问题、也就绕开 `f`/`df`：

- `roots_chebyt` 用 **解析公式** 直接给节点权重（[_orthogonal.py:1786-1795](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_orthogonal.py#L1786-L1795)）：节点是 \(\cos(\cdot)\) 的等角分布，权重恒为 \(\pi/n\)，根本不调 `eval_*`。
- `roots_hermite` 在 \(n>150\) 时改走 **Townsend–Trogdon–Olver 渐近算法**（[_orthogonal.py:958-978](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_orthogonal.py#L958-L978)）：用抛物柱面函数的渐近展开给初值、牛顿迭代抛光，线性时间复杂度，让几千点的 Hermite 求积变得可行。只有 \(n\le150\) 时才回落到 Golub-Welsch（即 `_gen_roots_and_weights`）路径。

这两个反例的意义是：Golub-Welsch 是**默认通用引擎**，不是唯一引擎；但无论走哪条路，最终交到你手里的都是同一形状的 `(x, w)`。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `roots_hermite` 返回的节点/权重确实能**精确**积分多项式。

**操作步骤**：

```python
import numpy as np
from scipy.special import roots_hermite

n = 10
x, w = roots_hermite(n)          # 物理学家的 Hermite，权函数 e^{-x^2}，区间 (-inf, inf)

# 高斯求积精确积分次数 <= 2n-1 = 19 的多项式，对 n=10 远远够用。
I0 = np.sum(w * 1.0)             # 积分 f(x)=1，应等于 mu0 = sqrt(pi)
I2 = np.sum(w * x**2)            # 积分 f(x)=x^2，应等于 sqrt(pi)/2

print(I0, np.sqrt(np.pi))        # 期望两者相等
print(I2, np.sqrt(np.pi) / 2)    # 期望两者相等
```

**需要观察的现象**：`I0` 与 `np.sqrt(np.pi)` 几乎逐位相等；`I2` 与 `np.sqrt(np.pi)/2` 几乎逐位相等。这是因为 \(\int_{-\infty}^{\infty}e^{-x^2}dx=\sqrt{\pi}\)、\(\int_{-\infty}^{\infty}x^2 e^{-x^2}dx=\sqrt{\pi}/2\)，而 \(n=10\) 的求积对不超过 19 次的多项式数学上精确。

**预期结果**：两组比较的相对误差都在机器精度量级（约 \(10^{-15}\)）。若把 `n` 调到 2，则 \(x^4\)（4 次，仍 \(\le 2n-1=3\)？不，\(2\cdot2-1=3\)）就会开始失配——这正是「\(n\) 个节点精确积分不超过 \(2n-1\) 次」的边界。（具体数值**待本地验证**。）

#### 4.1.5 小练习与答案

**练习 1**：`roots_chebyt(n)` 的权重为什么「恰好」全是 \(\pi/n\)，而 `roots_legendre(n)` 的权重却各不相同、需要 Christoffel 公式来算？

**参考答案**：Chebyshev 第一类多项式的权函数 \(1/\sqrt{1-x^2}\) 配上等角分布的节点，恰好让每个节点处的求积分量相等（这是 Chebyshev 求积的著名性质，对应源码 [_orthogonal.py:1790-1791](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_orthogonal.py#L1790-L1791) 的 `w = np.full_like(x, pi/m)`）。Legendre 的权函数是常数 1，没有这种对称红利，权重必须按 \(w_i\propto 1/(P_{n-1}(x_i)P_n'(x_i))\) 逐点算出，所以各不相同。

**练习 2**：把 `roots_jacobi` 里 `df` 的定义抄出来，说明它为什么没有调用任何「数值求导」。

**参考答案**：`df`（[_orthogonal.py:340-341](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_orthogonal.py#L340-L341)）用的是 Jacobi 多项式的**解析**导数恒等式，把「求 \(P_n'\)」换成「换参数 \((\alpha+1,\beta+1)\)、降一阶 \(n-1\)、再求值一次」。这是一次解析恒等变形，不是有限差分，因而既快又精确。

---

### 4.2 eval_\* 求值 ufunc：逐元素、稳定的求值器

#### 4.2.1 概念说明

`eval_legendre(n, x)`、`eval_jacobi(n, a, b, x)`、`eval_hermite(n, x)`…… 这一族 `eval_*` 回答的是另一个问题：**给定阶数 \(n\) 和一组点 \(x\)，求出多项式在这些点上的值**。

它们的「身份」和 `roots_*` 完全不同：`eval_*` **是 NumPy ufunc**，住在编译产物 `_ufuncs` 里（u2-l1 已讲过 ufunc 机制）。也就是说：

- `n`、`x` 都可以是数组，会**广播**：`eval_legendre([0,1,2], 0.0)` 一次返回 \(P_0(0)、P_1(0)、P_2(0)\)。
- 输出形状由输入广播决定，逐元素求值，支持 `out=`。
- 内核是 C 写的（多数走 Boost.Math / xsf 后端，见 u3-l4），用**三项递推**而非系数展开来求值，数值稳定。

#### 4.2.2 核心流程

从「我想要 \(P_n(x)\)」到「ufunc 返回结果」，链路是：

1. Python 层 `scipy.special.eval_legendre` 其实是 `_ufuncs.eval_legendre` 的转发（由 `from ._ufuncs import *` 提到命名空间）。
2. `_ufuncs` 是构建期由 `functions.json` 经 `_generate_pyx.py` 生成的 Cython 扩展（u3-l1、u3-l2）。
3. 运行时，ufunc 按 `x` 的 dtype 选 loop（如 `d->d` 实数、`D->D` 复数），逐元素调用 C 内核；内核内部用稳定的三项递推从低阶推到 \(n\)。
4. 若发生数值错误（如 domain），由 sf_error 机制转成 NaN 或告警（u2-l3、u7）。

类型桩 [`_ufuncs.pyi`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_ufuncs.pyi) 把它们统一标注成 `np.ufunc`，且全部列在 `_ufuncs.__all__` 里——这是判断「某函数是不是 ufunc」的机器目录（u2-l2）。

#### 4.2.3 源码精读

类型桩里 `eval_*` 的声明一目了然——[_ufuncs.pyi:351-361](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_ufuncs.pyi#L351-L361)：

```python
eval_gegenbauer: np.ufunc
eval_genlaguerre: np.ufunc
eval_hermite: np.ufunc
eval_hermitenorm: np.ufunc
eval_jacobi: np.ufunc
eval_laguerre: np.ufunc
eval_legendre: np.ufunc
eval_sh_chebyt: np.ufunc
eval_sh_chebyu: np.ufunc
...
```

每一行都说明这些 `eval_*` 在类型系统里就是 `np.ufunc`，与 `erf`、`gamma` 同属一类。它们也确实出现在 `_ufuncs.__all__` 中，见 [_ufuncs.pyi:65-79](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_ufuncs.pyi#L65-L79)（`'eval_chebyc'`、`'eval_legendre'`、`'eval_sh_jacobi'` 等都在列）。

`eval_legendre` 的文档串（在 `_add_newdocs.py`，构建时挂到 ufunc 上）进一步点明它的语义：参数 `n`、`x` 都是 `array_like`，并可选 `out=`——这是标准 ufunc 签名。其数学定义 \(P_n(x)={}_2F_1(-n,n+1;1;(1-x)/2)\) 在 \(n\) 为整数时退化为 \(n\) 次多项式，C 内核正是据此实现稳定的递推求值（参见 [_add_newdocs.py:2309-2355](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_add_newdocs.py#L2309-L2355) 的文档串）。

#### 4.2.4 代码实践

**实践目标**：用 ufunc 的标准玩法确认 `eval_*` 的「逐元素 + 广播」身份。

**操作步骤**：

```python
import numpy as np
import scipy.special as sc

# 1) 它确实是 ufunc
print(isinstance(sc.eval_legendre, np.ufunc))          # True
print(sc.eval_legendre.types)                          # 形如 ['dd->d', 'DD->D']

# 2) 广播：n 是数组、x 是标量
print(sc.eval_legendre(range(0, 5), 0.0))
# 期望 [1, 0, -0.5, 0, 0.375]  （P_0..P_4 在 0 处的值）

# 3) out= 写入
x = np.linspace(-1, 1, 5)
out = np.empty_like(x)
sc.eval_legendre(3, x, out=out)
print(out)
```

**需要观察的现象**：`isinstance(..., np.ufunc)` 为 `True`；`.types` 列出它支持的 loop；第 2 步一次返回多个阶的值（广播）；第 3 步 `out=` 被原地填上。

**预期结果**：与注释里写的一致。具体 `P_2(0)=-0.5`、`P_4(0)=0.375` 等可手算核对（**待本地验证**精确浮点输出）。

#### 4.2.5 小练习与答案

**练习 1**：用 `eval_legendre.types` 查它支持哪些 loop。它能不能处理复数输入？依据是什么？

**参考答案**：`eval_legendre.types` 通常含 `'dd->d'`（双精度实数）与 `'DD->D'`（双精度复数）。看到 `'DD->D'` 就说明它能处理复数（参见 u2-l1 的「多类型分发」）。这是判断任意 `eval_*` 是否支持复数的一眼法。

**练习 2**：为什么 `eval_*` 能做成 ufunc，而 `roots_*` 不能？用「逐元素」和「输出形状由谁决定」两点回答。

**参考答案**：`eval_*` 是逐元素的——给定 \((n, x)\) 求一个 \(P_n(x)\)，输出形状由 `n`、`x` 广播决定，天然符合 ufunc 模型。`roots_*` 要返回「长度恰为 \(n\) 的节点/权重数组」，输出长度由标量 `n` 决定而非输入数组形状，且求积分量要做跨元素聚合，违背 ufunc「逐元素」前提，因此只能是普通 Python 函数。

---

### 4.3 数值稳定性权衡：为什么高阶要避开「系数法」

#### 4.3.1 概念说明

除了 `roots_*` 和 `eval_*`，`scipy.special` 还有第三组：`legendre(n)`、`chebyt(n)`、`jacobi(n,a,b)`…… 它们返回一个 `orthopoly1d` 对象（继承自 `numpy.poly1d`），里面**存着多项式系数**，并附带节点、权重、权函数等信息。文档把它们单独列为一组（[__init__.py:555-562](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/__init__.py#L555-L562)），并告诫：

> Note that ``orthopoly1d`` objects are converted to `~numpy.poly1d` when doing arithmetic, and lose information of the original orthogonal polynomial.

这条告诫的根源是一个数值分析常识：**把高阶正交多项式表示成展开系数 \([c_n,\ldots,c_0]\) 是病态的**。原因有二（即第 2 节提到的 Wilkinson 现象）：

1. `orthopoly1d` 的系数是用 `np.poly1d(roots, r=True)` **从（数值近似的）根反乘出来的**。根的末位误差在连乘过程中被放大，污染系数的低位有效数字。
2. 即便系数精确，用 Horner 法对高阶多项式求值时，大系数之间的**灾难性相消**会吃掉有效数字。

经验上，当阶数 \(n\) 较大（常用经验阈值约 \(n\gtrsim 20\)，随多项式族与求值点而异——这不是源码里的硬编码阈值，而是数值实践的通用经验法则）时，系数法求值会明显失真。

**关键设计**：SciPy 没有坐视不管。`orthopoly1d` 内部藏了一个 `_eval_func`，默认指向对应的 `eval_*` ufunc；**当你直接「调用」这个对象时，它优先走 `_eval_func`（稳定的递推求值），而不是用存下来的系数做 Horner**。只有当你对它做算术（加减乘除、`+0`、`*1`……）时，`numpy.poly1d` 的运算会把它「降级」回普通 `poly1d`，丢掉 `_eval_func`，此后再求值就只能依赖那套被污染的系数了。

#### 4.3.2 核心流程

`orthopoly1d` 的求值分流，可以用一段伪代码说清：

```
构造 orthopoly1d:
    coeffs = np.poly1d(roots, r=True).coeffs * kn     # 从根反乘出系数（病态！）
    self._eval_func = eval_func                         # 指向稳定的 eval_* ufunc

调用 P(v):
    if 存在 _eval_func 且 v 不是 poly1d:
        return _eval_func(v)        # 走 ufunc 递推求值（稳定）
    else:
        return numpy.poly1d.__call__(self, v)   # 退回 Horner 系数求值（高阶失稳）

算术（P + Q、P * 2 ……）:
    返回普通 numpy.poly1d            # _eval_func 丢失！此后调用只能走 Horner
```

于是稳定性规则很清楚：

- **要积分** → 用 `roots_*`；
- **要求值（尤其高阶）** → 直接用 `eval_*`，或调用「未做过算术的」`orthopoly1d` 对象（因为它内部也走 `eval_*`）；
- **要拿系数去做别的事** → 当心系数本身已被污染，不要指望高阶系数末位可信；
- **避免**对高阶 `orthopoly1d` 做算术后再求值——那会强制走系数 Horner。

#### 4.3.3 源码精读

`orthopoly1d` 类见 [_orthogonal.py:150-192](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_orthogonal.py#L150-L192)。三个关键点：

- **系数从根反乘**：[_orthogonal.py:166-168](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_orthogonal.py#L166-L168)，`poly = np.poly1d(roots, r=True)` 再乘 `kn`——这正是 Wilkinson 病态映射的入口。
- **挂上稳定的求值器**：[_orthogonal.py:175-176](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_orthogonal.py#L175-L176) 一句注释点明设计意图与代价：

  ```python
  # Note: eval_func will be discarded on arithmetic
  self._eval_func = eval_func
  ```

  即：算术会丢掉 `_eval_func`。各 `legendre`/`jacobi`/`hermite` 等构造函数在创建 `orthopoly1d` 时，正是把 `lambda x: _ufuncs.eval_legendre(n, x)` 之类传进 `eval_func`（见 `legendre` 在 [_orthogonal.py:2717-2719](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_orthogonal.py#L2717-L2719) 的传参）。
- **调用时分流**：[_orthogonal.py:178-182](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_orthogonal.py#L178-L182)：

  ```python
  def __call__(self, v):
      if self._eval_func and not isinstance(v, np.poly1d):
          return self._eval_func(v)
      else:
          return np.poly1d.__call__(self, v)
  ```

  有 `_eval_func` 就走 ufunc（稳定）；没有（算术之后）就退回 `numpy.poly1d` 的 Horner（系数法，高阶失稳）。

把这三点连起来，就能解释一个容易让人困惑的现象：**`legendre(25)(0.3)` 看起来「用了系数」，结果却和 `eval_legendre(25, 0.3)` 一致**——因为 `__call__` 压根没碰系数，直接路由到了 `eval_legendre`。真正的失稳只在「显式拿 `.coeffs` 做 `np.polyval`」或「先做算术再调用」时才暴露。

#### 4.3.4 代码实践

**实践目标**：在高阶（\(n=25\)）下，对比「稳定的 `eval_*`」与「系数 Horner 求值」，亲眼看到系数法失真；并验证 `orthopoly1d` 的直接调用为何不失真。

**操作步骤**：

```python
import numpy as np
from scipy.special import eval_legendre, legendre

n, x = 25, 0.3

# (A) 参考值：稳定的 ufunc 递推求值
ref = eval_legendre(n, x)

# (B) 直接调用 orthopoly1d —— 内部路由到 eval_legendre，应当与 ref 一致
p = legendre(n)
via_obj = p(x)

# (C) 强制走系数 Horner：取出系数，用 numpy.polyval 求值
c = p.coeffs
via_coeffs = np.polyval(c, x)

print("ref          =", ref)
print("via_obj      =", via_obj, "  相对误差 =", abs(via_obj - ref) / abs(ref))
print("via_coeffs   =", via_coeffs, "  相对误差 =", abs(via_coeffs - ref) / abs(ref))

# (D) 进一步：对 p 做一次无害算术（+0），_eval_func 丢失，此后调用退回 Horner
p2 = p + np.poly1d([0.0])
print("after +0     =", p2(x), "  相对误差 =", abs(p2(x) - ref) / abs(ref))

# (E) 把阶数扫一遍，看系数法误差如何随 n 增长
for nn in [5, 15, 25, 40]:
    r = eval_legendre(nn, x)
    cc = np.polyval(legendre(nn).coeffs, x)
    print(f"n={nn:2d}  系数法相对误差 = {abs(cc-r)/abs(r):.3e}")
```

**需要观察的现象**：

- `(B)` 的 `via_obj` 与 `(A)` 的 `ref` 几乎完全相同（相对误差 ~机器精度）——证明 `orthopoly1d.__call__` 走的是 `eval_*`。
- `(C)` 的 `via_coeffs` 偏离 `ref`，相对误差明显大于 `(B)`——这就是系数法在高阶下的精度损失。
- `(D)` 做完 `+0` 后，对象退化为普通 `poly1d`，调用结果退化到与 `(C)` 一致的失真值——印证「算术丢弃 `_eval_func`」。
- `(E)` 随 `n` 从 5 涨到 40，系数法的相对误差应单调放大（可能从 \(10^{-13}\) 量级恶化到 \(10^{-5}\) 甚至更差）。

**预期结果**：定性结论如上；具体的相对误差数值**待本地验证**（它依赖平台浮点与 `numpy.poly1d` 的实现，不要把这里的估计当成精确预言）。

#### 4.3.5 小练习与答案

**练习 1**：代码片段 `p = legendre(50); q = p * 2; q(0.1)` 中，`q(0.1)` 走的是稳定路径还是系数路径？为什么？

**参考答案**：走**系数路径**（失稳）。因为 `p * 2` 是算术运算，`numpy.poly1d.__mul__` 返回一个普通 `poly1d`，不会保留 `orthopoly1d` 的 `_eval_func`（参见 [_orthogonal.py:175](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_orthogonal.py#L175) 的注释）。于是 `q(0.1)` 命中 `__call__` 的 `else` 分支，退回 Horner。若想保持稳定，应改为 `2 * eval_legendre(50, 0.1)`。

**练习 2**：假如让你给 `scipy.special` 加一条「求 \(P_n(x)\)」的推荐用法提示，结合本讲你会怎么写？

**参考答案**：「需要数值积分 → `roots_*` 取节点权重；需要逐点求值 → 直接 `eval_*`（它是 ufunc，可广播、支持 `out=`）；需要系数对象 → 用 `legendre`/`jacobi` 等，但记住：直接调用它仍走稳定的 `eval_*`，一旦做算术就会退回病态的系数 Horner，所以高阶下宁可全程用 `eval_*`。」

## 5. 综合实践

把本讲三块内容串起来，完成一个小任务：**用 `roots_*` 做一次高斯求积，并用 `eval_*` 同时充当「被积函数求值器」和「精度参照」**。

任务：用 Gauss–Legendre 求积近似 \(\int_{-1}^{1} e^{x}\,dx\)（精确值 \(e - 1/e\)）。

```python
import numpy as np
from scipy.special import roots_legendre, eval_legendre

n = 8
x, w = roots_legendre(n)                 # (1) roots_* 给节点、权重
approx = np.sum(w * np.exp(x))           # (2) 加权和：跨元素聚合（Python 层完成）
exact = np.e - 1/np.e
print("误差 =", abs(approx - exact))     # 应非常小

# (3) 顺带验证节点确实是 P_n 的根：用 eval_* 逐元素求值，应≈0
resid = eval_legendre(n, x)
print("max|P_n(x_i)| =", np.max(np.abs(resid)))   # 应≈机器精度
```

思考与延伸：

1. 步骤 (1) 用 `roots_*`、步骤 (3) 用 `eval_*`，恰好覆盖本讲的两个接口；请说明为什么步骤 (2) 的求和**必须**在 Python 层做，而不能指望某个 ufunc 帮你算。（答：求和是跨元素聚合，违背 ufunc「逐元素」前提。）
2. 把 `n` 从 2 逐步加到 20，画出误差曲线，验证误差随 \(n\) 快速下降（被积函数 \(e^x\) 解析，Gauss 求积对它收敛极快）。
3. 进阶：把被积函数换成 \(P_{20}(x)\)（即 `eval_legendre(20, x)`）。由于它是 Legendre 多项式，与求积节点同族，\(\int_{-1}^{1} P_{20}(x)\,dx = 0\)（正交于 \(P_0\)）；用 `n=8` 的求积会得到什么？为什么？（提示：\(P_{20}\) 是 20 次多项式，而 \(n=8\) 只精确到 \(2n-1=15\) 次，不足以精确积分它。）**待本地验证**。

## 6. 本讲小结

- `scipy.special` 的正交多项式有**三组接口**：`eval_*`（求值，是 ufunc）、`roots_*`（求积节点/权重，纯 Python）、`legendre`/`jacobi`/…（返回存系数的 `orthopoly1d` 对象）。
- **来源不同**：`eval_*` 住在编译产物 `_ufuncs`（类型桩 [`_ufuncs.pyi:351-361`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_ufuncs.pyi#L351-L361) 标注为 `np.ufunc`）；`roots_*` 与 `orthopoly1d` 住在 `_orthogonal.py`（由 [`__init__.py:803-804`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/__init__.py#L803-L804) 提到命名空间）。
- **`roots_*` 内部依赖 `eval_*`**：通用引擎 `_gen_roots_and_weights` 的牛顿抛光与 Christoffel 权重都靠 `f`/`df` 回调，而这些回调（如 [_orthogonal.py:338-341](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_orthogonal.py#L338-L341)）就是直接调 `eval_*`。
- **系数法在高阶失稳**：`orthopoly1d` 的系数由根反乘得到（Wilkinson 病态），高阶（经验上约 \(n\gtrsim 20\)）下做 Horner 求值会失真。
- **SciPy 的缓解**：`orthopoly1d.__call__`（[_orthogonal.py:178-182](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_orthogonal.py#L178-L182)）优先路由到 `_eval_func`（即 `eval_*`），所以直接调用对象是稳定的；但算术会丢弃 `_eval_func`（[_orthogonal.py:175](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_orthogonal.py#L175)），此后退回失稳的系数 Horner。
- **选型口诀**：积分用 `roots_*`，求值用 `eval_*`，碰系数对象时留意算术后退化。

## 7. 下一步学习建议

- 本讲只讲了「单输出」的 `eval_*` 和 `roots_*`。当你想**一次拿到所有阶** \(P_0,P_1,\ldots,P_n\) 的值（例如算球谐函数 \(Y_l^m\) 的全部 \((l,m)\) 组合），就会用到 `legendre_p_all`、`sph_harm_y_all` 这类「多输出聚合」函数——那是下一讲 **u5-l3：`_multiufuncs.py` 与 MultiUFunc** 的主题，它会解释 `MultiUFunc` 如何把多个 ufunc 包成单一可调用对象。
- 想更深入了解 `roots_*` 的算法变体，可重读 `_orthogonal.py` 里 `roots_hermite` 的 \(n>150\) 渐近路径（[_orthogonal.py:1301-1355](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_orthogonal.py#L1301-L1355) 的 `_roots_hermite_asy`），体会「Golub-Welsch 之外」的工程取舍。
- 想确认 `eval_*` 的 C 后端到底走 Boost 还是 xsf，可回到 u3-l4 的后端版图，并在 `functions.json` 里查 `eval_legendre`、`eval_jacobi` 等条目的头文件字段。
