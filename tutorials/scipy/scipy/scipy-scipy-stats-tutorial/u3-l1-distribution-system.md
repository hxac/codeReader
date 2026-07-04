# scipy.stats 分布体系与 rv_continuous / rv_discrete 基类

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 scipy.stats 里「一个分布」到底是什么东西——它不是你直接 `new` 出来的类，而是一个**已经实例化好的对象**。
- 区分三个基类的角色：`rv_continuous`（连续）、`rv_discrete`（离散）、`rv_histogram`（从直方图构造），并知道前两个由谁继承、第三个住在哪里。
- 理解 **公共方法 / 私有钩子** 这套双层设计：为什么你调用的 `pdf` 内部其实是在调 `_pdf`。
- 掌握 `pdf / pmf / cdf / ppf / rvs` 五个最常用方法的统计含义，并理解 `loc` / `scale` 这两个「平移缩放」参数在源码里到底做了什么。
- 用 `norm` 与 `binom` 亲手跑一遍这四类方法，看懂每个返回值的统计含义。

本讲只讲「分布对象的骨架与公共方法」。至于 `rv_generic` / `rv_frozen` 的缓存机制、`fit` 的最大似然实现、`_ShapeInfo` 与矩估计，留给 **u4（分布基础设施深入）** 展开。

## 2. 前置知识

本讲承接 **u1-l3**。那里我们得到两个关键结论，本讲会反复用到：

1. **命名空间聚合**：`scipy.stats` 是一层「纯路由」。一个名字要经过 `_continuous_distns.py → distributions.py → __init__.py` 三级搬运才抵达 `scipy.stats`。`norm` 就是这么来到你面前的。
2. **分布是「对象、方法」黑盒**：u1-l3 里我们写过 `norm.pdf`，但当时只说它「落到 `_norm_pdf` 与常量 √(2π)」，没有解释 `norm` 本身是谁、`pdf` 这个方法从哪来。本讲就来拆这个黑盒。

复习三个概率论关键词（不用很严格，有直觉即可）：

- **概率密度函数 pdf（continuous）/ 概率质量函数 pmf（discrete）**：连续随机变量在一点「密度」有多高；离散随机变量取某个整数「概率」有多大。注意：pdf 的值可以大于 1，pmf 的值必须在 [0, 1] 且所有取值之和为 1。
- **累积分布函数 cdf**：\(F(x) = P(X \le x)\)，单调不降、值域 [0, 1]。
- **分位点函数 ppf**：cdf 的反函数。给定概率 \(q\)，返回 \(x\) 使得 \(F(x)=q\)。比如 `norm.ppf(0.975)` 就是标准正态的 97.5% 分位点（约 1.96）。

另外要记住一个朴素事实：在 scipy.stats 里，几乎每个「具体分布名」（`norm`、`gamma`、`binom`、`poisson`……）都是某个「`名字_gen`」类的**单例实例**，而不是类本身。这个「实例化模型」是本讲第一块基石。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注的关键符号 |
| --- | --- | --- |
| [`_distn_infrastructure.py`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py) | 分布基础设施：定义所有基类与公共方法 | `rv_generic`、`rv_continuous`、`rv_discrete`、`rv_frozen`、公共方法 `pdf`/`cdf`/`ppf`/`rvs`、`freeze` |
| [`_continuous_distns.py`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_continuous_distns.py) | 所有具体连续分布的实现 | `norm_gen` 与实例 `norm`、`_norm_pdf` 等私有钩子、`rv_histogram` |
| [`_discrete_distns.py`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_discrete_distns.py) | 所有具体离散分布的实现 | `binom_gen` 与实例 `binom`、私有钩子 `_pmf`/`_cdf`/`_ppf` |
| [`distributions.py`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/distributions.py) | 聚合壳：把基类与具体分布汇总 | 第 8 行引入三个基类、第 19 行 `__all__` |
| [`__init__.py`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/__init__.py) | 模块入口与功能地图 | 第 35–43 行文档字符串列出三个基类；第 602 行 `from .distributions import *` |

搬运链（u1-l3 已建立，这里给出精确落点）：基类与 `norm`/`binom` 等实例在各自实现文件里定义 → 经 [`distributions.py:8`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/distributions.py#L8)（基类）与 [`distributions.py:13-15`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/distributions.py#L13-L15)（具体分布）汇总 → 经 [`__init__.py:602`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/__init__.py#L602) `from .distributions import *` 进入 `scipy.stats` 命名空间。

---

## 4. 核心概念与源码讲解

### 4.1 分布体系的「实例化模型」与三个基类

#### 4.1.1 概念说明

很多初学者第一次用 scipy.stats 时会困惑：`norm` 到底是什么？为什么是 `norm.pdf(x)` 而不是 `norm().pdf(x)` 或 `Normal().pdf(x)`？

答案是：**scipy.stats 的每个分布都是一个「已经造好的对象」，你直接对它调用方法。** 这个对象是某个「`_gen`」类的实例。以正态分布为例：

- `norm_gen` 是一个**类**，它继承自 `rv_continuous`。
- `norm` 是 `norm_gen` 的**唯一实例**（单例），在模块加载时就创建好了。

你拿到的 `scipy.stats.norm`，本质是这个单例对象。所有具体分布都遵循同一个套路：定义一个 `名字_gen(rv_continuous)` 或 `名字_gen(rv_discrete)` 类，再 `名字 = 名字_gen(name='名字')` 造出实例。这套「先定义生成类、再造单例实例」的模式，就是本讲的**实例化模型**。

由此自然引出三个基类的分工：

| 基类 | 住的文件 | 服务对象 | 关键方法 |
| --- | --- | --- | --- |
| `rv_continuous` | `_distn_infrastructure.py` | 连续分布（`norm`、`gamma`、`expon`…） | `pdf`、`cdf`、`ppf`、`rvs`、`fit` |
| `rv_discrete` | `_distn_infrastructure.py` | 离散分布（`binom`、`poisson`、`geom`…） | `pmf`、`cdf`、`ppf`、`rvs` |
| `rv_histogram` | `_continuous_distns.py` | 从已有直方图「反推」一个分布 | 继承 `rv_continuous` 全部方法 |

注意一个容易踩的坑：**`rv_histogram` 并不和前两个并列在 `_distn_infrastructure.py`**，它住在 `_continuous_distns.py`，而且是 `rv_continuous` 的**子类**（见 [`_continuous_distns.py:12117`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_continuous_distns.py#L12117)）。`__init__.py` 的功能地图把它和前两个并列展示，只是因为它是用户构造「自定义分布」的第三条入口，而非因为它与前两个同级。这点务必记牢，免得去错文件找代码。

#### 4.1.2 核心流程

一个具体分布（比如 `norm`）从「被定义」到「被你调用 `norm.pdf(0)`」的流程：

1. **定义类**：写 `class norm_gen(rv_continuous):` 并实现私有钩子 `_pdf` / `_cdf` / `_rvs` 等（描述「标准型」，即 loc=0、scale=1 时的形状）。
2. **造单例**：`norm = norm_gen(name='norm')`。这一步在 [`_continuous_distns.py:506`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_continuous_distns.py#L506)。
3. **搬运**：经 `distributions.py` → `__init__.py` 进入 `scipy.stats.norm`。
4. **你调用**：`norm.pdf(x, loc=μ, scale=σ)`。公共方法 `pdf` 做「参数校验 + 标准化 + 调私有 `_pdf` + 反标准化」。
5. **（可选）冻结**：`norm(loc=μ, scale=σ)` 会调 `__call__` → `freeze`，返回一个缓存了参数的 `rv_frozen` 对象。这块细节留给 u4-l1。

关键设计思想（来自 `rv_continuous` 文档字符串）：**公共方法负责「守门」，私有方法负责「算」。** 公共 `pdf` 检查参数合法性、处理 `loc`/`scale` 与支撑域，再调用私有的 `_pdf` 做真正计算。

#### 4.1.3 源码精读

先看正态分布这个最经典的例子，建立直觉。

**私有钩子**（描述标准型的形状），位于 [`_continuous_distns.py:362-391`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_continuous_distns.py#L362-L391)：

```python
def _norm_pdf(x):
    return np.exp(-x**2/2.0) / _norm_pdf_C   # _norm_pdf_C = sqrt(2*pi)

def _norm_cdf(x):
    return sc.ndtr(x)          # 标准正态 cdf，委托给 scipy.special

def _norm_ppf(q):
    return sc.ndtri(q)         # 标准正态分位点函数（cdf 的反函数）
```

这些函数都只认「标准型」的 `x`（即已经减去 loc、除以 scale 之后的值）。注意它们不带 `loc` / `scale` 参数——平移缩放是公共方法的事。

**类定义与单例**，[`_continuous_distns.py:394-423`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_continuous_distns.py#L394-L423) 与 [`_continuous_distns.py:506`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_continuous_distns.py#L506)：

```python
class norm_gen(rv_continuous):
    r"""A normal continuous random variable."""
    def _shape_info(self):
        return []                       # 正态没有形状参数，只有 loc/scale
    def _rvs(self, size=None, random_state=None):
        return random_state.standard_normal(size)
    def _pdf(self, x):
        return _norm_pdf(x)
    ...

norm = norm_gen(name='norm')             # 第 506 行：造出单例
```

`norm` 没有形状参数（`_shape_info` 返回空列表），它只有 `loc`（均值）和 `scale`（标准差）。对比之下，`gamma` 有一个形状参数 `a`，`binom` 有 `n`、`p` 两个——这些形状参数就是「`_gen` 类签名」与「公共方法参数」里多出来的部分。

**搬运落点**：基类在 [`distributions.py:8`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/distributions.py#L8) 引入：

```python
from ._distn_infrastructure import (rv_discrete, rv_continuous, rv_frozen)
```

`norm`、`binom` 等具体实例则由 [`distributions.py:13`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/distributions.py#L13) 的 `from ._continuous_distns import *` 一并带入；最后 [`distributions.py:19`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/distributions.py#L19) 用 `__all__` 明确把三个基类登记为公开 API。

#### 4.1.4 代码实践

**实践目标**：亲手验证「`norm` 是对象、`norm_gen` 是类」，建立实例化模型的肌肉记忆。

**操作步骤**：

```python
import scipy.stats as stats
from scipy.stats._continuous_distns import norm_gen

print(type(stats.norm))              # 看看 norm 的类型
print(isinstance(stats.norm, norm_gen))
print(isinstance(stats.norm, stats.rv_continuous))
print(stats.norm.name)               # 单例在创建时传入的名字
```

**需要观察的现象**：

- `type(stats.norm)` 应显示它是 `norm_gen` 的实例，而不是 `rv_continuous` 本身。
- `isinstance(stats.norm, stats.rv_continuous)` 应为 `True`——因为 `norm_gen` 继承自 `rv_continuous`。
- `stats.norm.name` 应为字符串 `'norm'`。

**预期结果**：你会看到 `norm` 是一个**实例**，类型名带 `_gen` 后缀，且确实「是一个 `rv_continuous`」。这就是「实例化模型」的直接证据。

> 说明：以上 `isinstance` 与 `name` 的结果是确定的（由 [`_continuous_distns.py:506`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_continuous_distns.py#L506) `norm_gen(name='norm')` 决定），可在本地直接验证。

#### 4.1.5 小练习与答案

**练习 1**：`stats.gamma` 有几个形状参数？它的类名是什么？

<details><summary>参考答案</summary>

`gamma` 有一个形状参数 `a`（形状参数定义在对应的 `gamma_gen` 类里）。类名是 `gamma_gen`，同样继承自 `rv_continuous`。你可以用 `type(stats.gamma).__name__` 与 `stats.gamma.numargs` 自行核对。
</details>

**练习 2**：为什么 `rv_continuous` 的文档字符串说它「不能直接当作分布使用」？

<details><summary>参考答案</summary>

因为 `rv_continuous` 只提供「公共方法 + 参数校验 + 标准化」这套**骨架**，真正的形状信息（`_pdf` / `_cdf`）需要子类提供。骨架本身没有定义任何具体的概率密度，所以直接拿 `rv_continuous` 去算 `pdf` 没有意义——你必须用一个实现了 `_pdf` 的子类（如 `norm_gen`）的实例。见 [`_distn_infrastructure.py:1670-1674`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L1670-L1674)。
</details>

---

### 4.2 rv_continuous：连续分布基类与公共方法

#### 4.2.1 概念说明

`rv_continuous` 是所有连续分布的「母类」。它的核心贡献是两件事：

1. **一套公共方法**：`pdf`、`logpdf`、`cdf`、`logcdf`、`sf`、`ppf`、`isf`、`rvs`、`moment`、`stats`、`entropy`、`expect`、`median`、`mean`、`std`、`var`、`interval`、`fit`、`support` 等（完整列表见 [`_distn_infrastructure.py:1721-1745`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L1721-L1745)）。子类只要实现 `_pdf` 或 `_cdf` 之一，其余方法大都能**自动派生**。
2. **`loc` / `scale` 平移缩放语义**：任何连续分布 \(X\) 都被建模成

\[
X = \text{loc} + \text{scale}\cdot Y,
\]

其中 \(Y\) 是「标准型」（loc=0、scale=1）。这是 scipy 分布最强大也最容易误解的设计——理解了它，`pdf` 源码里为什么要「除以 scale」就一目了然。

由上面的关系，三个核心函数在标准型 \(Y\) 与实际 \(X\) 之间的换算为：

\[
f_X(x) = \frac{1}{\text{scale}}\, f_Y\!\left(\frac{x-\text{loc}}{\text{scale}}\right) \quad\text{(密度要除以 scale)}
\]

\[
F_X(x) = F_Y\!\left(\frac{x-\text{loc}}{\text{scale}}\right) \quad\text{(累积概率不除以 scale)}
\]

\[
Q_X(q) = \text{loc} + \text{scale}\cdot Q_Y(q) \quad\text{(分位点要乘 scale 再加 loc)}
\]

直觉解释：`pdf` 是「单位长度的概率」，x 轴被拉伸了 `scale` 倍，密度自然要除以 `scale` 才能保证总面积仍为 1；`cdf` 是纯粹的「概率」，与坐标尺度无关，所以不除；`ppf` 是 cdf 的反函数，方向反过来，所以乘回 `scale` 再加 `loc`。

#### 4.2.2 核心流程

公共方法（以 `pdf` 为例）的执行步骤，对应 `loc/scale` 换算公式：

1. **解析参数** `_parse_args`：把传入的 `*args, **kwds` 拆成 `(shape参数, loc, scale)`。
2. **标准化 x**：计算 \(u = (x - \text{loc}) / \text{scale}\)，转入标准型的坐标系。
3. **守门检查**：`_argcheck` 检查形状参数合法、`scale > 0`；`_support_mask` 检查 \(u\) 是否落在支撑域 \([a, b]\) 内。
4. **调用私有钩子**：对合法点调用 `_pdf(u)`，得到标准型密度。
5. **反标准化**：除以 `scale`（`pdf` 特有），写回输出数组。支撑域外的点直接填 0，非法参数填 `badvalue`（默认 NaN）。

`cdf` 流程几乎相同，只是第 5 步**不除以 scale**；`ppf` 流程是「反过来」：先在标准型上算 `_ppf(q)`，再 `* scale + loc` 还原到实际坐标系。

#### 4.2.3 源码精读

**`pdf` 的实现**，[`_distn_infrastructure.py:2054-2091`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L2054-L2091)：

```python
def pdf(self, x, *args, **kwds):
    args, loc, scale = self._parse_args(*args, **kwds)   # 1. 解析参数
    x, loc, scale = map(asarray, (x, loc, scale))
    ...
    x = np.asarray((x - loc)/scale, ...)                 # 2. 标准化 x
    cond0 = self._argcheck(*args) & (scale > 0)          # 3. 守门
    cond1 = self._support_mask(x, *args) & (scale > 0)
    cond = cond0 & cond1
    output = zeros(shape(cond), dtyp)
    putmask(output, (1-cond0)+np.isnan(x), self.badvalue)  # 非法参数→NaN
    if np.any(cond):
        goodargs = argsreduce(cond, *((x,)+args+(scale,)))
        scale, goodargs = goodargs[-1], goodargs[:-1]
        place(output, cond, self._pdf(*goodargs) / scale)  # 4+5. 调私有钩子并除以 scale
    ...
    return output
```

注意最后那行 `self._pdf(*goodargs) / scale`——这就是公式里「除以 scale」的来源。支撑域外的点 `cond` 为 False，`output` 保持初始的 0，所以分布支撑域外 pdf 恒为 0。

**`cdf` 的实现**（精简），[`_distn_infrastructure.py:2135-2175`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L2135-L2175)：

```python
def cdf(self, x, *args, **kwds):
    args, loc, scale = self._parse_args(*args, **kwds)
    ...
    x = np.asarray((x - loc)/scale, dtype=dtyp)
    ...
    if np.any(cond):
        goodargs = argsreduce(cond, *((x,)+args))
        place(output, cond, self._cdf(*goodargs))   # 注意：没有 / scale
    ...
    return output
```

对比 `pdf`，`cdf` 的第 5 步**没有除以 scale**，正是上面公式 \(F_X(x) = F_Y((x-\text{loc})/\text{scale})\) 的体现。

**`ppf` 的实现**，[`_distn_infrastructure.py:2305-2348`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L2305-L2348)：

```python
def ppf(self, q, *args, **kwds):
    args, loc, scale = self._parse_args(*args, **kwds)
    ...
    cond2 = cond0 & (q == 0)            # q=0 → 下界
    cond3 = cond0 & (q == 1)            # q=1 → 上界
    ...
    place(output, cond2, argsreduce(cond2, lower_bound)[0])   # 直接返回支撑下界
    place(output, cond3, argsreduce(cond3, upper_bound)[0])
    if np.any(cond):
        goodargs = argsreduce(cond, *((q,)+args+(scale, loc)))
        scale, loc, goodargs = goodargs[-2], goodargs[-1], goodargs[:-2]
        place(output, cond, self._ppf(*goodargs) * scale + loc)  # 反标准化：乘 scale + loc
    ...
    return output
```

`ppf` 是「反过来」的：在标准型上算 `_ppf(q)`，再 `* scale + loc` 还原。`q=0` 和 `q=1` 是边界特例，直接返回支撑域的上下界。

**`rvs`（随机抽样）的实现**，[`_distn_infrastructure.py:1080-1147`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L1080-L1147)：

```python
def rvs(self, *args, **kwds):
    ...
    args, loc, scale, size = self._parse_args_rvs(*args, **kwds)
    cond = logical_and(self._argcheck(*args), (scale >= 0))
    if not np.all(cond):
        raise ValueError(...)                       # 形状/scale 非法→报错
    ...
    vals = self._rvs(*args, size=size, random_state=random_state)  # 私有钩子生成标准型样本
    vals = vals * scale + loc                        # 反标准化
    ...
    return vals
```

`rvs` 同样先在标准型抽样，再 `* scale + loc`。这就是为什么 `norm.rvs(loc=10, scale=2)` 会给你均值 10、标准差 2 的样本。

#### 4.2.4 代码实践

**实践目标**：用 `norm` 跑通 `pdf / cdf / ppf / rvs` 四类方法，对照公式理解每个返回值，并亲手验证「除以 scale」「乘 scale 加 loc」这些源码行为。

**操作步骤**：

```python
import numpy as np
import scipy.stats as stats

# ---- 1. pdf：标准正态在 0 点的密度 ----
print("pdf(0)        =", stats.norm.pdf(0))          # 期望 ≈ 0.3989
print("1/sqrt(2*pi)  =", 1/np.sqrt(2*np.pi))         # 公式对照

# ---- 2. loc/scale 换算：pdf 要除以 scale ----
# X = loc + scale*Y；f_X(x) = f_Y((x-loc)/scale)/scale
print("pdf(10, loc=10, scale=2) =", stats.norm.pdf(10, loc=10, scale=2))
print("pdf(0)/2                 =", stats.norm.pdf(0)/2)   # 应与上面相等

# ---- 3. cdf：不除以 scale，是纯概率 ----
print("cdf(0) =", stats.norm.cdf(0))                       # 期望 = 0.5
print("cdf(10, loc=10, scale=2) =", stats.norm.cdf(10, loc=10, scale=2))  # 均值处 = 0.5

# ---- 4. ppf：分位点，cdf 的反函数 ----
print("ppf(0.5) =", stats.norm.ppf(0.5))             # 期望 = 0（中位数）
print("ppf(0.975) =", stats.norm.ppf(0.975))         # 期望 ≈ 1.96
print("ppf(0.5, loc=10, scale=2) =", stats.norm.ppf(0.5, loc=10, scale=2))  # = 10

# ---- 5. rvs：随机抽样，用 random_state 保证可复现 ----
sample = stats.norm.rvs(loc=10, scale=2, size=100000, random_state=42)
print("sample mean ≈", sample.mean(), "  sample std ≈", sample.std())
```

**需要观察的现象 / 预期结果**：

- `pdf(0)` ≈ 0.3989，与 \(1/\sqrt{2\pi}\) 完全一致——这是 [`_norm_pdf`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_continuous_distns.py#L362-L363) 在标准型上的取值。
- `pdf(10, loc=10, scale=2)` 应**等于** `pdf(0)/2`：x=10 对应标准型 u=0，密度再除以 scale=2。这正是源码里 `/ scale` 的体现。
- `cdf(0)` = 0.5；`cdf(10, loc=10, scale=2)` = 0.5（均值处累积概率恒为 0.5，且不随 scale 变化）。
- `ppf(0.5)` = 0；`ppf(0.975)` ≈ 1.96；`ppf(0.5, loc=10, scale=2)` = 10（= `loc + scale * 0`）。
- `rvs` 的大样本均值应接近 10、标准差接近 2。

> 说明：前四步（pdf/cdf/ppf）的解析值是确定的，可与公式逐项核对。第五步 `rvs` 的样本统计量是随机的，但 10 万样本下均值/标准差会非常接近 10/2；具体数值「待本地验证」，设定 `random_state=42` 可保证结果可复现。

#### 4.2.5 小练习与答案

**练习 1**：用一行代码验证 `norm.ppf` 与 `norm.cdf` 互为反函数（在 (0,1) 内）。

<details><summary>参考答案</summary>

`stats.norm.ppf(stats.norm.cdf(1.23))` 应返回 `1.23`（允许浮点误差）。因为 `ppf` 是 `cdf` 的反函数，见 [`_distn_infrastructure.py:2306`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L2306) 的文档「inverse of cdf」。
</details>

**练习 2**：为什么 `norm.pdf(x, loc=0, scale=2)` 在 x=0 处的值是 `norm.pdf(0)/2` 而不是 `norm.pdf(0)`？

<details><summary>参考答案</summary>

因为 pdf 是「单位长度的概率」。scale=2 把 x 轴拉伸了 2 倍，曲线变矮变宽，但总面积仍需为 1，所以高度必须除以 scale。源码 [`_distn_infrastructure.py:2088`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L2088) 的 `self._pdf(*goodargs) / scale` 就是干这件事。
</details>

---

### 4.3 rv_discrete：离散分布基类

#### 4.3.1 概念说明

`rv_discrete` 是 `rv_continuous` 的离散兄弟，服务于取值在整数上的分布（`binom`、`poisson`、`geom`、`hypergeom` 等）。它的文档字符串在 [`_distn_infrastructure.py:3175`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L3175)，并明确列出了与连续版本的四点区别（[`_distn_infrastructure.py:3260-3268`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L3260-L3268)）：

1. **支撑域是整数集合**（不是连续区间）。
2. 用 **`pmf`（probability mass function，概率质量函数）** 取代 `pdf`；对应的私有钩子是 `_pmf` 而不是 `_pdf`。
3. **没有 `scale` 参数**——离散分布只平移（`loc`）不缩放。这是与连续版最大的 API 差异。
4. 默认方法实现**不适用于支撑域向下无界**（即 `a = -inf`）的分布，这类分布必须自行覆盖 `_cdf` 等钩子。

为什么没有 `scale`？因为离散分布的取值是「整数计数」（次数、个数），缩放会破坏整数性。`loc` 仍可用来整体平移支撑域（比如把取值从 {0,1,2,...} 平移到 {5,6,7,...}）。

`rv_discrete` 还有一个连 `rv_continuous` 都没有的能力：你可以直接用一个支撑点列表和对应概率 `(xk, pk)` 构造一个任意离散分布（见文档参数 `values`，[`_distn_infrastructure.py:3191-3194`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L3191-L3194)）。这是「无需子类化」的快捷构造路径。

#### 4.3.2 核心流程

以二项分布 `binom` 为例，调用 `binom.pmf(k, n, p)` 的流程：

1. **解析参数**：`n`、`p` 是形状参数，`loc` 默认 0，**没有 scale**。
2. **守门** `_argcheck`：对 `binom`，要求 `n >= 0` 且为整数、`0 <= p <= 1`（见 [`_discrete_distns.py:75-76`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_discrete_distns.py#L75-L76)）。
3. **取整**：离散分布的输入 `k` 会被 `floor` 取整（见 [`_discrete_distns.py:82`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_discrete_distns.py#L82)），因为只有整数点上有概率质量。
4. **调用 `_pmf` / `_cdf` / `_ppf`**：这些钩子大多委托给 `_binom_pmf`、`_binom_cdf`、`_binom_ppf` 等专用数值实现。
5. **`rvs` 抽样后转 int**：公共 `rvs` 会把结果强制转换成整数（[`_distn_infrastructure.py:1140-1145`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L1140-L1145)）。

#### 4.3.3 源码精读

**`binom_gen` 的私有钩子**，[`_discrete_distns.py:81-102`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_discrete_distns.py#L81-L102)：

```python
def _logpmf(self, x, n, p):
    k = floor(x)                                   # 离散取整
    combiln = (gamln(n+1) - (gamln(k+1) + gamln(n-k+1)))
    return combiln + special.xlogy(k, p) + special.xlog1py(n-k, -p)

def _pmf(self, x, n, p):
    return scu._binom_pmf(x, n, p)                 # 委托给专用实现

def _cdf(self, x, n, p):
    k = floor(x)
    return scu._binom_cdf(k, n, p)

def _ppf(self, q, n, p):
    return scu._binom_ppf(q, n, p)
```

注意两点：第一，钩子签名是 `_pmf(self, x, n, p)`——`n`、`p` 是形状参数，**没有 scale**；第二，`_logpmf` / `_cdf` 都先 `floor(x)`，体现「整数支撑」。

**实例化**，[`_discrete_distns.py:123`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_discrete_distns.py#L123)：

```python
binom = binom_gen(name='binom')
```

和 `norm` 一样的套路：定义 `binom_gen(rv_discrete)`，再造单例。

**公共 `pmf` 与连续版 `pdf` 的对照**：`rv_discrete.pmf`（[`_distn_infrastructure.py:3506`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L3506)）的整体结构与 `rv_continuous.pdf` 几乎一样（解析参数 → 守门 → 调 `_pmf`），唯一区别是**不除以 scale**——因为根本没有 scale。

**`rvs` 的离散特例**，[`_distn_infrastructure.py:1140-1145`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L1140-L1145)：

```python
# Cast to int if discrete
if discrete and not isinstance(self, rv_sample):
    if size == ():
        vals = int(vals)
    else:
        vals = vals.astype(np.int64)
```

公共 `rvs` 在判断为离散分布时，会把抽样结果强制转成 `int64`，确保返回整数。

#### 4.3.4 代码实践

**实践目标**：用 `binom` 跑通 `pmf / cdf / ppf / rvs`，对照连续版理解「整数支撑、无 scale」带来的差异。

**操作步骤**：

```python
import numpy as np
import scipy.stats as stats

n, p = 10, 0.3

# ---- 1. pmf：取每个整数 k 的概率 ----
ks = np.arange(0, n+1)
print("pmf:", stats.binom.pmf(ks, n, p))
print("sum(pmf) =", stats.binom.pmf(ks, n, p).sum())   # 期望 = 1.0

# ---- 2. pmf 在非整数输入上的行为（floor 取整）----
print("pmf(2.0) =", stats.binom.pmf(2.0, n, p))
print("pmf(2.7) =", stats.binom.pmf(2.7, n, p))        # 期望与 pmf(2.0) 相等

# ---- 3. cdf：P(X <= k) ----
print("cdf(3) =", stats.binom.cdf(3, n, p))            # P(X<=3)

# ---- 4. ppf：给定概率，返回最小整数 k 使 cdf(k) >= q ----
print("ppf(0.95) =", stats.binom.ppf(0.95, n, p))      # 95% 分位对应的整数

# ---- 5. rvs：抽样，返回整数数组 ----
sample = stats.binom.rvs(n, p, size=100000, random_state=42)
print("dtype:", sample.dtype)                          # 期望 int64
print("sample mean ≈", sample.mean(), " (理论 n*p =", n*p, ")")
```

**需要观察的现象 / 预期结果**：

- `pmf` 在 k=0..10 上取值，所有 pmf 之和 = 1（pmf 的基本性质）。
- `pmf(2.7)` 应**等于** `pmf(2.0)`——因为钩子里 `floor(2.7)=2`（[`_discrete_distns.py:82`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_discrete_distns.py#L82)）。
- `cdf(3)` 给出 P(X ≤ 3)。
- `ppf(0.95)` 返回一个整数（二项分布的 95% 分位点）。
- `sample.dtype` 是 `int64`（公共 `rvs` 强制转换），样本均值接近理论值 n·p = 3。

> 说明：pmf/cdf/ppf 的解析值是确定的，可逐项核对。`rvs` 的样本均值随机，但大样本下接近 3，具体「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`binom.ppf(0.95, 10, 0.3)` 返回的整数 k，满足什么性质？

<details><summary>参考答案</summary>

它满足 \(F(k-1) < 0.95 \le F(k)\)，即 k 是使累积概率首次达到或超过 0.95 的最小整数。这就是「分位点函数」在离散分布上的广义定义（取最小的「上确界」整数）。
</details>

**练习 2**：尝试调用 `stats.binom.pmf(5, 10, 0.3, scale=2)`，会发生什么？为什么？

<details><summary>参考答案</summary>

会报错（`TypeError`），因为 `rv_discrete` **没有 scale 参数**。这是它与 `rv_continuous` 的核心 API 差异（[`_distn_infrastructure.py:3265`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L3265)）。离散分布只接受 `loc`，不接受 `scale`。
</details>

---

### 4.4 rv_histogram：从直方图构造分布

#### 4.4.1 概念说明

前两个基类服务于「已知公式的参数分布」。但有时你只有一组**已经分箱的数据**（直方图），想把它当成一个连续分布来用——算 pdf、cdf、抽样。`rv_histogram` 就是干这个的。

关键定位（务必记住）：`rv_histogram` **是 `rv_continuous` 的子类**，住在 [`_continuous_distns.py:12117`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_continuous_distns.py#L12117)，**不是** `_distn_infrastructure.py` 里和前两个并列的第三个基类。它的文档第一句话就点明了关系：

> Generates a distribution given by a histogram. ... As a subclass of the `rv_continuous` class, `rv_histogram` inherits from it a collection of generic methods.

它的输入是一个二元组 `(histogram, bin_edges)`——恰好是 `numpy.histogram` 的返回值。所以典型用法是：

```python
counts, edges = np.histogram(data, bins=100)
dist = stats.rv_histogram((counts, edges))
dist.pdf(1.0)   # 当成普通连续分布用
```

它没有额外的形状参数，只有 `loc` / `scale`。pdf 定义为**阶梯函数**（每个箱内密度恒定），cdf 是 pdf 的分段线性插值（[`_continuous_distns.py:12159-12161`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_continuous_distns.py#L12159-L12161)）。

#### 4.4.2 核心流程

1. **输入**：一个 `(内容数组, 箱边界数组)` 元组，箱边界长度 = 内容长度 + 1。
2. **归一化**：根据 `density` 参数，把内容解释为「计数」或「密度」，归一化成总概率为 1 的密度函数。
3. **派生全套方法**：因为继承自 `rv_continuous`，pdf/cdf/ppf/rvs/fit 等方法全自动可用——pdf 在每个箱内取阶梯值，cdf 是累积。
4. **当成普通分布使用**：`dist.pdf(x)`、`dist.cdf(x)`、`dist.rvs(size=100)` 都和 `norm` 的用法一模一样。

#### 4.4.3 源码精读

**类定义与定位**，[`_continuous_distns.py:12117-12127`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_continuous_distns.py#L12117-L12127)：

```python
class rv_histogram(rv_continuous):
    """
    Generates a distribution given by a histogram.
    ...
    As a subclass of the `rv_continuous` class, `rv_histogram` inherits from it
    a collection of generic methods ..., and implements them based on the
    properties of the provided binned datasample.
    """
```

**pdf / cdf 的定义方式**，[`_continuous_distns.py:12159-12161`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_continuous_distns.py#L12159-L12161)：

```
There are no additional shape parameters except for the loc and scale.
The pdf is defined as a stepwise function from the provided histogram.
The cdf is a linear interpolation of the pdf.
```

**构造函数签名**，[`_continuous_distns.py:12211`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_continuous_distns.py#L12211)：

```python
def __init__(self, histogram, *args, density=None, **kwargs):
```

`density` 参数（1.10.0 加入）决定如何解释输入：`False` 表示「计数」（来自 `np.histogram` 默认），`True` 表示「密度」（来自 `np.histogram(density=True)`）。当箱宽不均时这个区分很重要。

> 注意：`rv_histogram` 是少数「直接实例化基类子类」就能用的入口——你不必自己写 `_pdf`，只要喂一个直方图。这和 `norm`（写好 `_pdf` 的固定分布）形成对比：一个面向「已知公式」，一个面向「已知数据」。

#### 4.4.4 代码实践

**实践目标**：从一组真实样本造一个 `rv_histogram` 分布，验证它继承了 `rv_continuous` 的全套方法，并对比它和理论 `norm` 的接近程度。

**操作步骤**：

```python
import numpy as np
import scipy.stats as stats

# 1. 造一组正态样本
data = stats.norm.rvs(size=100000, loc=0, scale=1.5, random_state=123)

# 2. 用 np.histogram 分箱，喂给 rv_histogram
hist = np.histogram(data, bins=100)
hist_dist = stats.rv_histogram(hist, density=False)

# 3. 当成普通连续分布用（继承自 rv_continuous）
print("pdf(0)    =", hist_dist.pdf(0.0))      # 应接近 N(0; 0, 1.5) 的密度
print("norm.pdf(0, scale=1.5) =", stats.norm.pdf(0, scale=1.5))
print("cdf(2.0)  =", hist_dist.cdf(2.0))      # 累积概率

# 4. 用它来抽样（rvs 也能用）
print("rvs sample mean ≈", hist_dist.rvs(size=10000, random_state=1).mean())

# 5. 支撑域：超出最大/最小箱边界 pdf 为 0
print("pdf(极大值) =", hist_dist.pdf(np.max(data)))   # 文档示例说会 ≈ 0
```

**需要观察的现象 / 预期结果**：

- `hist_dist.pdf(0.0)` 应接近 `norm.pdf(0, scale=1.5)` ≈ 0.266（因为样本来自 N(0, 1.5²)）。
- `hist_dist` 能调用 `pdf`/`cdf`/`rvs`——证明它继承了 `rv_continuous` 的全套公共方法。
- 在最大数据点处 pdf 接近 0（因为那是最后一个箱的边缘，见 [`_continuous_distns.py:12184-12194`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_continuous_distns.py#L12184-L12194) 的文档示例）。

> 说明：`hist_dist` 的精确取值依赖随机样本，具体数值「待本地验证」；但它能调用 pdf/cdf/rvs 这一**结构性事实**是确定的。

#### 4.4.5 小练习与答案

**练习 1**：为什么说 `rv_histogram`「不需要写 `_pdf`」？

<details><summary>参考答案</summary>

因为它直接从输入直方图「读出」每个箱的密度，pdf 是阶梯函数，由数据本身定义，没有需要用公式表达的 `_pdf`。它的「形状信息」就是那组箱计数。这也是它和 `norm_gen`（必须实现 `_pdf`）的根本区别。见 [`_continuous_distns.py:12159-12161`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_continuous_distns.py#L12159-L12161)。
</details>

**练习 2**：如果分箱时箱宽不均匀，为什么 `density` 参数很重要？

<details><summary>参考答案</summary>

因为「计数」和「密度」在箱宽不均时不等价：计数 = 密度 × 箱宽。`rv_histogram` 需要知道你给的是计数（`density=False`）还是密度（`density=True`），才能正确归一化成总概率为 1 的分布。箱宽均匀时两者只差一个常数，可忽略；不均匀时就会出错。见 [`_continuous_distns.py:12148-12157`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_continuous_distns.py#L12148-L12157)。
</details>

---

## 5. 综合实践

**任务**：用一条「数据 → 直方图分布 → 抽样 → 假设检验」的链路，把本讲三个基类的知识串起来。

背景：假设你拿到一组「不知道来自什么分布」的连续数据。你想 (a) 用 `rv_histogram` 把它包装成分布对象；(b) 用这个分布的 `pdf`/`cdf` 做初步描述；(c) 从中 `rvs` 重抽样，并与原始数据对比。

```python
import numpy as np
import scipy.stats as stats

# 0. 「神秘数据」——实际中你不知道它来自哪
rng = np.random.default_rng(7)
data = rng.standard_normal(50000) * 1.5 + 10     # 均值10，标准差1.5（你假装不知道）

# (a) 用 rv_histogram 把数据包装成分布对象
counts, edges = np.histogram(data, bins=80)
D = stats.rv_histogram((counts, edges), density=False)

# (b) 用 D 的 pdf / cdf 描述这份数据
xs = np.linspace(D.support()[0], D.support()[1], 200)
density_vals = D.pdf(xs)
print("D.pdf 在众数附近最大；cdf 从 0 单调到 1")
print("D.cdf(10) =", D.cdf(10))                   # 应接近 0.5（均值处）
print("D.ppf(0.5) =", D.ppf(0.5))                 # 中位数，应接近 10

# (c) 从 D 重抽样，与原始数据对比样本均值/标准差
resample = D.rvs(size=20000, random_state=1)
print("原始  mean/std:", data.mean(), data.std())
print("重抽样 mean/std:", resample.mean(), resample.std())

# 进阶：连续分布 vs 离散分布的方法对照
print("\n--- 连续 norm vs 离散 binom 的方法名对照 ---")
print("连续: pdf  cdf  ppf  rvs   （有 scale）")
print("离散: pmf  cdf  ppf  rvs   （无 scale）")
print("norm.pdf(0)      =", stats.norm.pdf(0))
print("binom.pmf(0,5,.5)=", stats.binom.pmf(0, 5, 0.5))
```

**你需要解释清楚的事**（这是本综合实践的「交付物」）：

1. `D` 是哪个类的实例？它和 `stats.norm` 在「类型层级」上有什么共同点？（答：`D` 是 `rv_histogram` 实例，`rv_histogram` 继承 `rv_continuous`；`norm` 是 `norm_gen` 实例，`norm_gen` 也继承 `rv_continuous`——所以两者「都是 `rv_continuous` 的（间接）实例」，共享同一套公共方法。）
2. 为什么 `D.cdf(10)` 接近 0.5、`D.ppf(0.5)` 接近 10？（答：数据均值约为 10，对称分布的均值 ≈ 中位数。）
3. 重抽样的均值/标准差为什么接近原始数据？（答：`rv_histogram` 保留了数据的分布形状，`rvs` 从这个形状里抽样，统计量自然接近。）
4. 连续 `norm` 用 `pdf`、离散 `binom` 用 `pmf`，但 `cdf`/`ppf`/`rvs` 同名——这套**统一的方法命名**正是三个基类共享设计的体现。

> 说明：综合实践里的数值结果依赖随机数，「待本地验证」；但第 1、4 点的**结构性结论**（类型层级、方法命名）是确定的，无需运行也能断言。

## 6. 本讲小结

- scipy.stats 的每个具体分布（`norm`、`binom`…）都是某个 `名字_gen` 类的**单例实例**，而不是类本身——这就是「实例化模型」。实例在模块加载时由 `名字 = 名字_gen(name=...)` 创建。
- 三个基类分工：`rv_continuous`（连续，`_distn_infrastructure.py`）、`rv_discrete`（离散，同文件）、`rv_histogram`（从直方图构造，住 `_continuous_distns.py`，**是 `rv_continuous` 的子类**）。
- 核心设计是**「公共方法守门 + 私有钩子计算」**：公共 `pdf`/`cdf`/`ppf` 负责参数校验与 `loc`/`scale` 标准化，再调用私有 `_pdf`/`_cdf`/`_ppf`。
- `loc`/`scale` 的本质是 \(X = \text{loc} + \text{scale}\cdot Y\)；于是 `pdf` 要除以 scale、`cdf` 不除、`ppf` 乘 scale 加 loc——源码里这三行分别对应 [`pdf:2088`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L2088)、[`cdf:2172`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L2172)、[`ppf:2345`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L2345)。
- 离散版与连续版的关键差异：用 `pmf`/`_pmf` 取代 `pdf`/`_pdf`，**没有 scale 参数**，输入会 `floor` 取整，`rvs` 结果转 int64。
- `rv_histogram` 是「面向数据」的入口：喂一个直方图就能得到一个继承 `rv_continuous` 全套方法的分布对象，无需写 `_pdf`。

## 7. 下一步学习建议

本讲建立的是「分布对象的骨架与公共方法」。接下来：

- **u4-l1（rv_generic 与 frozen 分布机制）**：本讲你看到 `norm(loc, scale)` 会调 `__call__` → `freeze`（[`_distn_infrastructure.py:893-914`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L893-L914)），返回 `rv_frozen`（[`_distn_infrastructure.py:507`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L507)）。u4-l1 会拆开 `rv_frozen` 如何缓存 `loc`/`scale`、`rv_generic` 作为公共基类提供了什么。
- **u4-l2（方法内部实现）**：本讲只看了 `pdf`/`cdf`/`ppf`/`rvs` 四个方法，u4-l2 会讲 `logpdf`/`sf`/`isf` 如何从 `_pdf`/`_cdf` **自动派生**，以及 `fit` 的通用最大似然实现。
- **u3-l2 / u3-l3**：本讲用的是 `norm`/`binom` 这两个「代表」，接下来两讲会系统介绍更多连续分布（`gamma` 等的形状参数）与离散分布（`poisson`/`geom` 等）。

建议先把本讲的综合实践跑通，确认你能解释「`norm.pdf` 为什么要除以 scale」「`binom` 为什么没有 scale」这两个问题，再进入 u4。
