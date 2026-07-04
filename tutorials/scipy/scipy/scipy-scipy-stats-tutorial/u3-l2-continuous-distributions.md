# 使用连续分布：norm、gamma 等

## 1. 本讲目标

本讲承接上一讲（u3-l1）建立的「分布骨架」——你已经知道每个具体分布都是某个 `*_gen` 类在模块加载时造出的单例实例，公共方法（`pdf/cdf/ppf/rvs`）守门、私有钩子（`_pdf/_cdf/_ppf`）干活。本讲把镜头推进到「具体连续分布怎么用」。

学完本讲，你应该能够：

- 区分**形状参数**（shape）与 **`loc`/`scale`**——前者因分布而异、决定分布的形状本身，后者对所有连续分布统一、只做平移缩放。
- 用 **frozen 分布对象**把一组参数「冻结」下来，避免每次调用都重复传参。
- 理解分布的 **support（支撑域）** 边界如何随形状参数、`loc`、`scale` 变化。
- 读懂 `_continuous_distns.py` 中一个具体分布的源码，并知道它的形状参数个数和取值范围是从哪里声明的。

---

## 2. 前置知识

本讲默认你已经掌握 u3-l1 的结论，特别是这两条：

1. **实例化模型**：`norm` 不是类，而是 `norm_gen(name='norm')` 在模块加载时造出的单例实例；调用 `norm.pdf(...)` 是调用这个实例的方法。
2. **标准化关系 \(X = \text{loc} + \text{scale}\cdot Y\)**：每个具体分布的私有钩子（`_pdf/_cdf/_ppf`）只实现「标准型」\(Y\)（即 `loc=0, scale=1`）的计算，公共方法负责把用户传入的 `x`、`loc`、`scale` 与标准型对接。

补充一个直观的统计学背景，本讲会反复用到：

> 若 \(Y\) 是标准型连续随机变量，密度为 \(f_Y\)，定义 \(X = \text{loc} + \text{scale}\cdot Y\)（要求 `scale > 0`），那么 \(X\) 的密度、分布函数、分位函数满足：
>
> \[ f_X(x) = \frac{1}{\text{scale}}\, f_Y\!\left(\frac{x-\text{loc}}{\text{scale}}\right) \]
>
> \[ F_X(x) = F_Y\!\left(\frac{x-\text{loc}}{\text{scale}}\right) \]
>
> \[ Q_X(q) = \text{loc} + \text{scale}\cdot Q_Y(q) \]

这三条公式就是 scipy 在源码里真正在做的事，本讲第 4.2 节会逐一对照。如果你暂时不关心推导，只要记住一句口诀：**`pdf` 要除以 `scale`、`cdf` 不除、`ppf` 乘 `scale` 加 `loc`**。

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到的部分 |
| --- | --- | --- |
| [_continuous_distns.py](_continuous_distns.py) | 所有连续分布的具体实现，每个分布一个 `*_gen` 子类 + 一行实例化 | `norm_gen`、`gamma_gen` 及其实例化语句 |
| [_distn_infrastructure.py](_distn_infrastructure.py) | `rv_continuous` 基类机制：形状参数推断、`loc/scale` 解析、`freeze`、`support` | `pdf/cdf/ppf/support/freeze`、`_construct_argparser`、`_ShapeInfo` |
| [_distr_params.py](_distr_params.py) | 给测试用的「每个分布的合理形状参数」清单 | `distcont` 列表，看 `norm`/`gamma` 的形状参数个数 |

> 提示：本讲的永久链接指向固定 commit `c3a772bd`，行号以该 commit 为准。

---

## 4. 核心概念与源码讲解

### 4.1 形状参数：从 norm 到 gamma

#### 4.1.1 概念说明

scipy 里每个连续分布有三类参数，区分它们是本讲最重要的概念：

| 参数类别 | 是否因分布而异 | 语义 | 典型例子 |
| --- | --- | --- | --- |
| **形状参数 shape** | **是** | 决定分布「形状本身」，个数与含义因分布而不同 | gamma 的 `a`、beta 的 `a, b` |
| `loc` | 否（所有连续分布统一） | 平移（位置） | `norm` 里就是均值 |
| `scale` | 否（所有连续分布统一） | 缩放（尺度） | `norm` 里就是标准差 |

关键直觉：**`loc/scale` 是「外衣」，形状参数是「骨架」**。给同一个 gamma 套不同的 `loc/scale`，只是把它平移拉伸，曲线形状不变；改形状参数 `a`，曲线本身（偏度、峰度）才会变。`norm` 是个特例——它**没有形状参数**，因为它只用 `loc`（均值）和 `scale`（标准差）就完全确定了。

#### 4.1.2 核心流程

那 scipy 怎么知道某个分布有几个形状参数、分别叫什么？答案是**从私有钩子的函数签名自动推断**，流程如下：

1. 加载 `_continuous_distns.py`，定义 `xxx_gen(rv_continuous)` 子类，里面写 `_pdf(self, x, ...)`。
2. 执行 `xxx = xxx_gen(name='xxx')` 实例化。
3. `rv_continuous.__init__` 调用 `_construct_argparser`，检查 `_pdf`/`_cdf` 的形参表。
4. 剥掉 `self` 和 `x` 之后，**剩下的位置参数就是形状参数**，个数即 `numargs`，名字拼成 `shapes` 字符串。
5. 据此动态生成 `_parse_args` 方法，用于把用户传入的「形状 + loc + scale」拆开。

也就是说：**你写 `_pdf(self, x)` 就没有形状参数；写 `_pdf(self, x, a)` 就有一个名叫 `a` 的形状参数**。无需手工登记。

#### 4.1.3 源码精读

先看没有形状参数的 `norm`。[_continuous_distns.py:394-506](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_continuous_distns.py#L394-L506) 定义了 `norm_gen`，关键几处：

- [_continuous_distns.py:417-418](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_continuous_distns.py#L417-L418)：`_shape_info` 返回空列表 `[]`——声明 norm 没有形状参数（这是新一代元信息，见下）。
- [_continuous_distns.py:423-425](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_continuous_distns.py#L423-L425)：`_pdf(self, x)` 签名里除 `self`、`x` 外没有别的参数，所以推断出 `numargs=0`。
- [_continuous_distns.py:506](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_continuous_distns.py#L506)：`norm = norm_gen(name='norm')`——实例化时**没有传 `a=` 或 `b=`**，因此支撑域默认是 \((-\infty, +\infty)\)。

再看有一个形状参数的 `gamma`。[_continuous_distns.py:3543-3566](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_continuous_distns.py#L3543-L3566) 的文档字符串写明了标准型密度：

\[ f(x, a) = \frac{x^{a-1} e^{-x}}{\Gamma(a)},\qquad x\ge 0,\ a>0 \]

- [_continuous_distns.py:3584-3585](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_continuous_distns.py#L3584-L3585)：`_shape_info` 返回一个 `_ShapeInfo("a", False, (0, np.inf), (False, False))`，意思是「形状参数名叫 `a`、非整数、取值域 \((0,+\infty)\)、两端都开（严格大于 0）」。
- [_continuous_distns.py:3590-3595](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_continuous_distns.py#L3590-L3595)：`_pdf(self, x, a)` 比 norm 多出一个 `a`，于是推断出 `numargs=1`、`shapes='a'`。`_logpdf` 用 `sc.xlogy(a-1.0, x) - x - sc.gammaln(a)` 计算，数值上比直接算 \(x^{a-1}\) 更稳。
- [_continuous_distns.py:3759](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_continuous_distns.py#L3759)：`gamma = gamma_gen(a=0.0, name='gamma')`——实例化时传了 `a=0.0`，注意这是**基类构造器参数**（支撑域下界），不是形状参数 `a`！这一行把 gamma 标准型的支撑域下界设成 0，于是支撑域是 \([0,+\infty)\)。

> ⚠️ 易混点：`gamma_gen(a=0.0, ...)` 里的 `a=0.0` 是 `rv_continuous` 基类的「支撑域下界」形参，而分布的形状参数也叫 `a`，两者只是同名、语义完全不同。后面 `support` 一节会看到这个下界怎么生效。

形状参数的「自动推断」逻辑在基类 [_distn_infrastructure.py:753-832](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L753-L832) 的 `_construct_argparser`：当子类没显式传 `shapes` 字符串时，[_distn_infrastructure.py:785-815](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L785-L815) 检查 `_pdf`/`_cdf` 的形参表，剥掉 `x` 之后剩下的就是形状参数；最终在 [_distn_infrastructure.py:829-832](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L829-L832) 写入 `self.shapes` 与 `self.numargs`。

形状参数的取值域元信息由 [_distn_infrastructure.py:1627-1640](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L1627-L1640) 的 `_ShapeInfo` 承载：`name`、`integrality`（是否必须整数）、`endpoints`（取值域端点）、`inclusive`（端点是否闭）。注意它还做了一件细致的事——当端点有限且不闭时，用 `np.nextafter` 把域向内缩一格，避免数值上误取到边界。

最后，[_distr_params.py:8-131](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distr_params.py#L8-L131) 的 `distcont` 列表给测试提供「合理形状参数」，可以一眼看出每个分布有几个形状参数：`norm` 对应 [_distr_params.py:94](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distr_params.py#L94) 的 `['norm', ()]`（空元组，0 个），`gamma` 对应 [_distr_params.py:38](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distr_params.py#L38) 的 `['gamma', (1.993...,)]`（1 个）。这张表是「形状参数个数」最直观的索引。

#### 4.1.4 代码实践

**实践目标**：用实例属性亲眼确认 `norm` 与 `gamma` 的形状参数个数和名字。

**操作步骤**（示例代码）：

```python
# 示例代码
from scipy.stats import norm, gamma

print("norm.numargs =", norm.numargs)   # 期望 0
print("norm.shapes  =", norm.shapes)    # 期望 None（无形状参数）

print("gamma.numargs =", gamma.numargs) # 期望 1
print("gamma.shapes  =", gamma.shapes)  # 期望 'a'
print("gamma._shape_info() =", gamma._shape_info())  # 期望包含一个 _ShapeInfo(name='a')
```

**需要观察的现象**：

- `norm.shapes` 是 `None`（而不是空字符串），因为没有任何形状参数。
- `gamma.shapes` 是 `'a'`，与 `_pdf(self, x, a)` 的形参名一致。
- `gamma._shape_info()` 返回的列表里，元素的 `.endpoints` 约为 `(0, inf)`、`.inclusive` 为 `(False, False)`，对应 \(a>0\)。

**预期结果**：`norm.numargs=0`、`gamma.numargs=1`、`gamma.shapes='a'`。（若你的 scipy 版本/commit 不同，`_shape_info` 的细节以源码为准——待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：`beta` 分布的形状参数是什么？请用一行代码确认它有几个。

参考答案：`beta` 的 `_pdf(self, x, a, b)` 有两个形状参数；`import scipy.stats as st; print(st.beta.numargs, st.beta.shapes)` 应输出 `2 'a, b'`。

**练习 2**：为什么 `norm = norm_gen(name='norm')` 不传 `a=`，而 `gamma = gamma_gen(a=0.0, name='gamma')` 传了 `a=0.0`？

参考答案：这里的 `a` 是 `rv_continuous` 基类构造器的「支撑域下界」形参。norm 的支撑域是整条实轴（默认 \(-\infty\)），无需指定；gamma 的标准型只在 \(x\ge 0\) 上有定义，所以要把下界设成 0，让基类知道密度在负半轴恒为 0。

---

### 4.2 loc/scale 的平移缩放语义

#### 4.2.1 概念说明

上一节强调：`loc/scale` 对所有连续分布含义一致。但「一致」到底体现在代码哪里？本节对照源码验证第 2 节那三条公式。再复习一次口诀：**`pdf` 除以 `scale`、`cdf` 不除、`ppf` 乘 `scale` 加 `loc`**。

一个常被初学者忽略的点：`scale` 必须**严格大于 0**。源码里多处用 `(scale > 0)` 当掩码，`scale <= 0` 会被判为非法参数，结果填 `badvalue`（默认 `np.nan`）。

#### 4.2.2 核心流程

公共方法对 `loc/scale` 的处理是统一的「三步走」：

1. `_parse_args(*args, **kwds)` 把用户传入的「形状 + loc + scale」拆成三元组 `(args, loc, scale)`，缺省 `loc=0, scale=1`。
2. 把输入标准化：`x = (x - loc) / scale`，得到标准型变量 \(Y\) 的取值。
3. 调用私有钩子算标准型的值，再按方法各自补回 `loc/scale`：
   - `pdf`：乘上雅可比 \(1/\text{scale}\)；
   - `cdf`：概率是无量纲量，不补；
   - `ppf`：反标准化，乘 `scale` 加 `loc`。

#### 4.2.3 源码精读

**pdf** [_distn_infrastructure.py:2054-2091](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L2054-L2091)。两处关键：

- [_distn_infrastructure.py:2079](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L2079)：`x = np.asarray((x - loc)/scale, ...)`——把用户 \(x\) 映射到标准型 \(Y\)。
- [_distn_infrastructure.py:2088](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L2088)：`place(output, cond, self._pdf(*goodargs) / scale)`——最后**除以 `scale`**，对应 \(f_X = f_Y / \text{scale}\)。

**cdf** [_distn_infrastructure.py:2135-2175](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L2135-L2175)。关键：

- [_distn_infrastructure.py:2162](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L2162)：同样 `x = (x - loc)/scale`。
- [_distn_infrastructure.py:2172](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L2172)：`place(output, cond, self._cdf(*goodargs))`——**没有除以 `scale`**。另外 L2160 先取支撑域 `_a, _b`，L2165 的 `cond2 = (x >= b)` 把超出上界的点直接置为 `1.0`，这就是支撑域边界在 cdf 里的体现。

**ppf** [_distn_infrastructure.py:2305-2348](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L2305-L2348)。关键：

- [_distn_infrastructure.py:2345](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L2345)：`place(output, cond, self._ppf(*goodargs) * scale + loc)`——把标准型分位点反标准化，**乘 `scale` 加 `loc`**，与公式 \(Q_X = \text{loc} + \text{scale}\cdot Q_Y\) 完全一致。L2337-2338 还处理了 `q=0`/`q=1` 的退化情况，直接返回平移缩放后的支撑域端点。

三条方法、三处源码，正好对应第 2 节的三条公式——scipy 没有对每个分布重复实现平移缩放，而是**在基类公共方法里统一做**，子类只管标准型。

#### 4.2.4 代码实践

**实践目标**：用 `norm` 手工验证「`pdf` 除以 `scale`」这条公式。

**操作步骤**（示例代码）：

```python
# 示例代码
import numpy as np
from scipy.stats import norm
from math import exp, pi, sqrt

loc, scale = 2.0, 0.5
x = 2.3

# scipy 的计算（带 loc/scale）
val_scipy = norm.pdf(x, loc=loc, scale=scale)

# 手工：先标准化，再用标准型密度，最后除以 scale
y = (x - loc) / scale                       # 标准型取值
fY = exp(-y*y/2) / sqrt(2*pi)               # 标准正态密度
val_manual = fY / scale                      # pdf 要除以 scale

print("scipy =", val_scipy)
print("manual =", val_manual)
print("一致？", np.isclose(val_scipy, val_manual))
```

**需要观察的现象**：`val_scipy` 与 `val_manual` 应几乎相等。

**预期结果**：两者一致（约 `0.7365`，取决于 `x`）；`np.isclose` 返回 `True`。如果把 `val_manual` 漏写 `/ scale`，两者会相差一个 `scale` 倍，正好印证那条口诀。

#### 4.2.5 小练习与答案

**练习 1**：`norm.cdf(x, loc=μ, scale=σ)` 在代码里为什么不除以 `scale`，而 `norm.pdf` 要除？

参考答案：因为 cdf 是「概率」（无量纲），\(F_X(x)=P(X\le x)=P(Y\le (x-\text{loc})/\text{scale})=F_Y(\cdot)\)，变量代换不引入额外因子；而 pdf 是 cdf 的导数，链式法则 \(\mathrm{d}F_Y/\mathrm{d}x = f_Y\cdot (1/\text{scale})\) 会带出一个 \(1/\text{scale}\)。

**练习 2**：若误把 `scale=0` 传给 `norm.pdf`，会得到什么？为什么？

参考答案：得到 `np.nan`（`badvalue`）。因为源码用 `(scale > 0)` 作合法性掩码，`scale=0` 不满足，对应位置填 `badvalue`。这也说明 `scale` 必须严格正。

---

### 4.3 frozen 分布对象与 support 边界

#### 4.3.1 概念说明

每次写 `gamma.cdf(2.0, 3, loc=0, scale=1)` 都要把形状 `a=3`、`loc`、`scale` 重抄一遍，既啰嗦又易错。**frozen 分布对象**就是为了解决这个问题：把一组参数「冻结」到一个对象里，之后调用方法时只需传 `x`，参数自动复用。

语法上有两种等价写法：

```python
rv = gamma(a, loc=loc, scale=scale)   # 调用实例（走 __call__）
rv = gamma.freeze(a, loc=loc, scale=scale)  # 显式调用 freeze
```

两者都返回一个 `rv_continuous_frozen` 对象（frozen 机制的内部细节留到 u4-l1 展开，本讲只关注「怎么用」）。

同时，本节顺带讲清 **support（支撑域）**：分布「密度为正」的取值范围。gamma 标准型支撑域是 \([0,+\infty)\)，但加了 `loc/scale` 后会平移缩放成 \([\text{loc},\,+\infty)\)（scale>0 时下界乘 scale 加 loc）。

#### 4.3.2 核心流程

- `freeze` / `__call__`：返回 frozen 对象，缓存 `(args, loc, scale)`。
- `support(*args, loc, scale)`：返回平移缩放后的支撑域端点 `(a_out, b_out)`，其中 `a_out = _a*scale + loc`、`b_out = _b*scale + loc`，`_a/_b` 是标准型支撑域（由 `_get_support` 给出，默认取基类的 `self.a/self.b`）。

#### 4.3.3 源码精读

**freeze 与 \_\_call\_\_** [_distn_infrastructure.py:893-915](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L893-L915)：

- [_distn_infrastructure.py:893-911](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L893-L911)：`freeze` 把形状参数和 `loc/scale` 交给 `rv_continuous_frozen` 构造。
- [_distn_infrastructure.py:913-915](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L913-L915)：`__call__` 直接转调 `freeze`，所以 `gamma(a)` 与 `gamma.freeze(a)` 完全等价。

**support** [_distn_infrastructure.py:1521-1554](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L1521-L1554)：

- [_distn_infrastructure.py:1540-1542](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L1540-L1542)：先 `_parse_args` 拆出 `(args, loc, scale)`。
- [_distn_infrastructure.py:1544](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L1544)：`_a, _b = self._get_support(*args)` 拿到标准型支撑域。
- [_distn_infrastructure.py:1546](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L1546)：返回 `_a * scale + loc, _b * scale + loc`——支撑域端点同样按 \(X=\text{loc}+\text{scale}\cdot Y\) 平移缩放。

**`_get_support` 默认实现** [_distn_infrastructure.py:1018-1038](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L1018-L1038)：直接返回 `self.a, self.b`。而 `self.a/self.b` 在 [_distn_infrastructure.py:1895-1900](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L1895-L1900) 的 `__init__` 里被设成构造器参数 `a/b`（缺省 \(-\infty/+\infty\)）。这就把前面 `gamma_gen(a=0.0, ...)` 的那行 `a=0.0` 串起来了：它设了 `self.a=0.0`，所以 `gamma._get_support()` 返回 `(0.0, inf)`，进而 `gamma.support()` 返回 `(0*scale+loc, inf)`。

> 名字再提醒一次：构造器参数 `a`（支撑域下界）与形状参数 `a`（gamma 的形状）同名但不同物。`gamma_gen(a=0.0, name='gamma')` 设的是前者。

#### 4.3.4 代码实践（本讲主任务）

**实践目标**：实例化 gamma 分布（指定形状参数 `a`），分别用「普通调用」和「frozen 对象」计算 cdf，验证两者完全一致。同时观察 support 如何随 `loc/scale` 变化。

**操作步骤**（示例代码）：

```python
# 示例代码
import numpy as np
from scipy.stats import gamma

a, loc, scale = 3.0, 1.0, 2.0
x = 4.5

# 方式一：普通调用，每次都把参数传全
val_plain = gamma.cdf(x, a, loc=loc, scale=scale)

# 方式二：frozen 对象，参数只写一次
rv = gamma(a, loc=loc, scale=scale)   # 等价于 gamma.freeze(a, loc=loc, scale=scale)
val_frozen = rv.cdf(x)

print("plain  =", val_plain)
print("frozen =", val_frozen)
print("一致？  ", np.isclose(val_plain, val_frozen))

# 观察 support 边界如何随 loc/scale 平移缩放
print("标准型 support     =", gamma.support(a))                  # 期望 (0.0, inf)
print("平移缩放后 support =", rv.support())                      # 期望 (loc, inf) = (1.0, inf)
```

**需要观察的现象**：

1. `val_plain` 与 `val_frozen` 数值完全相同——frozen 只是「参数绑定」，计算逻辑与普通调用一模一样。
2. 标准型 `gamma.support(a)` 下界为 `0.0`；加了 `loc=1, scale=2` 后，下界变成 `0*2 + 1 = 1.0`，上界仍是 `inf`。
3. 把 `x` 改成小于下界的值（如 `0.5`），`rv.cdf(0.5)` 应返回 `0.0`（落在支撑域外）。

**预期结果**：两种调用方式结果一致（如 `a=3,loc=1,scale=2,x=4.5` 时约 `0.0227`）；support 从 `(0, inf)` 平移到 `(1, inf)`。具体数值「待本地验证」，但「两者一致」与「support 下界=loc」这两条结论是确定的。

**进阶观察（可选）**：把 `gamma(a, loc=loc, scale=scale)` 换成等价的 `gamma.freeze(a, loc=loc, scale=scale)`，结果不变——印证 `__call__` 转调 `freeze`。

#### 4.3.5 小练习与答案

**练习 1**：`gamma(a=3, loc=1, scale=2)` 与 `gamma(a=3, loc=1, scale=2).freeze(...)` 的写法对吗？frozen 对象还能再 freeze 吗？

参考答案：前半句的实例化写法正确。frozen 对象本身一般不再调用 `.freeze`（它已经是一组固定参数的视图）；要换参数应直接重新 `gamma(新参数)`。`__call__`/`freeze` 定义在 `*_gen` 实例（如 `gamma`）上，而非 frozen 对象上。

**练习 2**：若 `gamma.support(a, loc=5, scale=2)` 的下界是多少？为什么？

参考答案：下界是 `5.0`。因为标准型下界 `_a=0`，`support` 返回 `_a*scale+loc = 0*2+5 = 5`；上界 `inf` 不受有限 scale 影响。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个「读懂并验证一个分布」的小任务，以 `gamma` 为对象。

1. **数形状参数**：读 [_continuous_distns.py:3590-3595](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_continuous_distns.py#L3590-L3595) 的 `_pdf(self, x, a)`，确认 gamma 有 1 个形状参数 `a`；再用 `gamma.numargs` / `gamma.shapes` 在运行时复核。
2. **验证 loc/scale 语义**：选一组 `(a, loc, scale)`，用 `gamma.cdf(x, a, loc=loc, scale=scale)` 与「先标准化再调标准型 `_cdf`、不做 scale 校正」的手工计算对比（提示：标准型 cdf 是 `scipy.special.gammainc(a, y)`，`y=(x-loc)/scale`，参考 [_continuous_distns.py:3597-3598](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_continuous_distns.py#L3597-L3598)）。两者应相等，印证「cdf 不除 scale」。
3. **用 frozen 复用参数**：把第 2 步的参数冻进 `rv = gamma(a, loc=loc, scale=scale)`，用 `rv.cdf(x)`、`rv.ppf(0.5)`、`rv.support()` 各算一次，体会「参数只写一次」的便利。
4. **检查 support**：用 `rv.support()` 与手算 `_a*scale+loc` 比较，确认下界等于 `loc`（因为 gamma 标准型 `_a=0`）。

完成后再换一个分布（如 `expon`，[_continuous_distns.py:2165](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_continuous_distns.py#L2165) 的 `expon = expon_gen(a=0.0, name='expon')`，0 个形状参数）重复一遍，体会「无形状参数」与「有形状参数」在使用上的差别。

---

## 6. 本讲小结

- 连续分布有三类参数：**形状参数**（因分布而异，决定形状本身）、`loc`（统一平移）、`scale`（统一缩放，必须 >0）。
- 形状参数的个数与名字由 `_construct_argparser` 从 `_pdf`/`_cdf` 的签名**自动推断**；`norm` 无形状参数（`numargs=0`），`gamma` 有 1 个 `a`（`numargs=1`）。
- `loc/scale` 的数学关系是 \(X=\text{loc}+\text{scale}\cdot Y\)，在源码里体现为口诀：**`pdf` 除以 `scale`、`cdf` 不除、`ppf` 乘 `scale` 加 `loc`**（分别在 L2088、L2172、L2345）。
- **frozen 对象**（`gamma(a, loc, scale)` 或 `gamma.freeze(...)`）把参数绑定到一个对象，等价于普通调用，但参数只写一次。
- **support** 端点同样按 \(X=\text{loc}+\text{scale}\cdot Y\) 平移缩放：`support()` 返回 `_a*scale+loc, _b*scale+loc`；gamma 因构造时 `a=0.0` 而下界为 0，加 `loc` 后下界变 `loc`。
- `_ShapeInfo` 用 `name/integrality/endpoints/inclusive` 描述形状参数的取值域（如 gamma 的 `a>0`），是新一代分布基础设施的元信息载体。

---

## 7. 下一步学习建议

- **u3-l3** 会把同样的用法推广到离散分布（`binom`/`poisson`），重点对比 `pmf` vs `pdf`，以及离散分布**没有 `scale`** 的差异。
- 想深入「frozen 对象内部如何缓存参数、`rv_generic`/`rv_frozen` 的类层级」，进入 **u4-l1（rv_generic 与 frozen 分布机制）**。
- 想了解 `pdf/cdf/ppf/logpdf/sf` 之间的派生关系与 `fit` 的最大似然流程，进入 **u4-l2（分布方法的内部实现）**。
- 想看 `_ShapeInfo` 这套元信息如何服务于 `fit` 与矩计算，进入 **u4-l3（矩估计、形状参数与 _ShapeInfo）**。
- 建议同时翻一遍 [_distr_params.py](_distr_params.py) 的 `distcont`，把「分布名 → 形状参数个数」这张地图记在脑子里，后续读任何分布都能快速定位。
