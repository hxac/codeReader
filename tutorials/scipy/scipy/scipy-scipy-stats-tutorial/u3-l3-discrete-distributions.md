# 使用离散分布：binom、poisson 等

## 1. 本讲目标

本讲承接 u3-l1（分布骨架：`rv_continuous`/`rv_discrete` 基类与公共方法）和 u3-l2（连续分布的形状参数、`loc/scale`、frozen 对象）。你已经知道每个分布都是某个 `*_gen` 类在模块加载时造出的单例实例，公共方法守门、私有钩子干活。本讲把镜头转向**离散分布**——它们在用法上和连续分布几乎一样，但有几个关键差异必须先讲清楚，否则会踩坑。

学完本讲，你应该能够：

- 用 **`pmf`**（probability mass function，概率质量函数）取代 `pdf` 处理离散分布，理解为什么是 `pmf` 而不是 `pdf`。
- 明白离散分布**没有 `scale` 参数**（只有 `loc` 平移），并能解释源码里这是怎么实现的。
- 理解离散分布的**整数支撑**（support 是一串整数），以及 `_get_support` 如何让支撑上界随形状参数变化（如 binom 的支撑是 \(\{0,1,\dots,n\}\)）。
- 读懂 `_discrete_distns.py` 中 `binom_gen`、`poisson_gen`、`geom_gen` 三个典型分布的源码。
- 用 `ppf` 在离散分布上找分位点，并理解「离散 cdf 不可严格求逆」导致的 `ppf` 特殊约定。

---

## 2. 前置知识

本讲默认你已经掌握 u3-l1 和 u3-l2 的结论，特别是这两条：

1. **公共/私有双层方法**：公共 `pdf/cdf/ppf/rvs` 负责参数校验与 `loc/scale` 标准化，私有钩子 `_pdf/_cdf/_ppf` 只算「标准型」。本讲会看到，离散分布把这层结构换成了 `pmf/cdf/ppf` + `_pmf/_cdf/_ppf`。
2. **连续分布的标准化关系 \(X=\text{loc}+\text{scale}\cdot Y\)**：上一讲给出的口诀是「`pdf` 除以 `scale`、`cdf` 不除、`ppf` 乘 `scale` 加 `loc`」。本讲会发现离散分布**砍掉了 `scale`**，于是口诀退化为「只有 `loc` 平移」。

补充一个直观的统计学背景，本讲会反复用到：

> **连续 vs 离散**：连续随机变量（如正态）取值落在任意一段小区间 \([x, x+\mathrm{d}x]\) 上的概率由**密度** \(f(x)\,\mathrm{d}x\) 给出，单点概率为 0；离散随机变量（如掷硬币次数）只能取一串**整数值** \(k\)，每个整数点上有**正的概率** \(P(X=k)\)，这个值就是 `pmf`。
>
> 因此对离散分布：
>
> \[ P(X = k) = \text{pmf}(k) \]
>
> \[ F(k) = P(X \le k) = \sum_{j \le k} \text{pmf}(j) \]
>
> `cdf` 是一个**右连续的阶梯函数**（在整数点处跳升），这正是 `ppf`「不可严格求逆」的根源。

把连续分布的「除以 scale、积分」换成离散分布的「只平移、求和」，就是本讲的全部差异所在。

---

## 3. 本讲源码地图

| 文件 | 作用 | 本讲用到的部分 |
| --- | --- | --- |
| [_discrete_distns.py](_discrete_distns.py) | 所有离散分布的具体实现，每个分布一个 `*_gen(rv_discrete)` 子类 + 一行实例化 | `binom_gen`、`poisson_gen`、`geom_gen` 及其实例化语句 |
| [_distn_infrastructure.py](_distn_infrastructure.py) | `rv_discrete` 基类机制：形状参数推断、`loc` 解析（无 scale）、整数支撑、`pmf/cdf/ppf` | `rv_discrete` 类与 `pmf/cdf/ppf/support`、`_construct_argparser` 调用、`_remove_scale_methods` |

> 提示：本讲的永久链接指向固定 commit `c3a772bd`，行号以该 commit 为准。

---

## 4. 核心概念与源码讲解

### 4.1 离散分布入门：从 pdf 到 pmf，从 scale 到只有 loc

#### 4.1.1 概念说明

离散分布和连续分布在 scipy 里共用同一套「`*_gen` 单例 + 公共/私有双层方法」骨架（见 u3-l1），但有三处关键差异。`rv_discrete` 基类的文档字符串把它们写得非常清楚：

1. **支撑是一串整数**：分布只在整数点上有概率，非整数点的 `pmf` 为 0。
2. **用 `pmf`/`_pmf` 代替 `pdf`/`_pdf`**：因为单点概率非零，描述它的函数叫「概率质量函数」而非「概率密度函数」。
3. **没有 `scale` 参数**：离散分布只保留 `loc`（平移），没有缩放。这是一个常被忽略、但必须记住的差异。

为什么没有 `scale`？直觉上：连续分布里 `scale` 是「拉伸坐标轴」（如把标准正态拉成 \(\sigma\) 倍宽），离散分布的取值是「计数」（成功次数、到达次数），把计数「拉伸 1.5 倍」会落到非整数上、破坏 pmf 的整数语义，所以 scipy 干脆不给离散分布提供 `scale`。

#### 4.1.2 核心流程

离散分布的标准化关系比连续简单：只有平移，没有缩放。

\[ X = \text{loc} + Y \]

其中 \(Y\) 是标准型（`loc=0`）离散变量。于是三个公共方法的口诀退化为：

- **`pmf`**：把用户 `k` 减去 `loc` 后送进 `_pmf`，**不除任何东西**（pmf 本身是概率，无量纲）。
- **`cdf`**：`k - loc` 后送进 `_cdf`，**不除任何东西**。
- **`ppf`**：标准型分位点算出来后**加 `loc`**，不乘 scale。

形状参数的推断流程和连续分布**完全一样**，只是基类 `__init__` 里**检查的钩子从 `_pdf` 换成了 `_pmf`**：写 `_pmf(self, k)` 就没有形状参数；写 `_pmf(self, k, mu)` 就有一个名叫 `mu` 的形状参数。

#### 4.1.3 源码精读

`rv_discrete` 基类定义在 [_distn_infrastructure.py:3175](_distn_infrastructure.py)。它的文档字符串在 [_distn_infrastructure.py:3253-3268](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L3253-L3268) 明确列出了与 `rv_continuous` 的「main differences」，本节开头那三条差异就出自这里——这是理解全部离散分布的纲领。

**「没有 scale」的实锤** 在 `__init__` 里。`rv_discrete.__init__` 的签名 [_distn_infrastructure.py:3333-3335](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L3333-L3335) 根本没有 `scale` 形参；随后 [_distn_infrastructure.py:3357-3360](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L3357-L3360) 调用 `_construct_argparser` 时写死了 `locscale_out='loc, 1'`——注释一行 `# scale=1 for discrete RVs` 直白说明：离散分布的 scale 永远是 1，对外不暴露。同时，形状参数的推断这里传的是 `[self._pmf, self._cdf]`（注意是 `_pmf`，而连续分布是 `_pdf`），与连续分布 [_distn_infrastructure.py:1905](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L1905) 的 `[self._pdf, self._cdf]` 形成对照。

为了让「没有 scale」连文档都一致，模块末尾还有一段 [_distn_infrastructure.py:3988-3997](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L3988-L3997) 的 `_remove_scale_methods`，它把 `entropy/mean/var/support` 等一串方法的文档里 `scale` 参数描述**逐个删掉**——所以你看 `binom.mean` 的帮助时不会看到 `scale`，这是程序化修出来的，不是手写的。

形状参数的推断逻辑本身是基类 `rv_generic._construct_argparser`，与 u3-l2 讲过的连续分布完全相同 [_distn_infrastructure.py:753-832](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L753-L832)：剥掉 `self`、`x`（离散里是 `k`）后剩下的位置参数即形状参数。所以读离散分布的形状参数，方法和连续一样——看 `_pmf` 签名。

#### 4.1.4 代码实践

**实践目标**：用实例属性确认三个离散分布的形状参数个数，并验证它们「没有 scale」。

**操作步骤**（示例代码）：

```python
# 示例代码
from scipy.stats import binom, poisson, geom
import inspect

for d in (binom, poisson, geom):
    print(f"{d.name}: numargs={d.numargs}, shapes={d.shapes!r}")

# 看看 pmf 的签名里有没有 scale
print("binom.pmf 签名:", inspect.signature(binom.pmf))
```

**需要观察的现象**：

- `binom.shapes` 为 `'n, p'`（2 个形状参数），`poisson.shapes` 为 `'mu'`（1 个），`geom.shapes` 为 `'p'`（1 个）。
- `binom.pmf` 的签名里**只有 `k`、形状参数和 `loc`，没有 `scale`**——和连续分布 `norm.pdf` 多一个 `scale` 形成对比。

**预期结果**：三个分布的 `numargs/shapes` 分别为 `2 'n, p'`、`1 'mu'`、`1 'p'`；`pmf` 签名无 `scale`。（具体打印字符串以本地源码为准——待本地验证，但「无 scale」与形状参数个数是确定的。）

#### 4.1.5 小练习与答案

**练习 1**：连续分布的标准化关系是 \(X=\text{loc}+\text{scale}\cdot Y\)，离散分布退化成什么？为什么？

参考答案：退化为 \(X=\text{loc}+Y\)（只剩平移）。因为离散分布的取值是整数计数，提供 `scale` 缩放会让取值落到非整数、破坏 pmf 的整数语义，所以 scipy 在 `rv_discrete.__init__` 里把 scale 写死为 1（`locscale_out='loc, 1'`）。

**练习 2**：为什么离散分布用 `pmf` 而不是 `pdf`？

参考答案：因为离散随机变量在单个整数点 \(k\) 上的概率 \(P(X=k)\) **严格大于 0**，可以直接当作一个「质量」赋值，这个函数叫概率质量函数 `pmf`；而连续随机变量单点概率恒为 0，只能用「密度」`pdf`（单位长度上的概率）来描述。

---

### 4.2 binom_gen：有界整数支撑的典型

#### 4.2.1 概念说明

二项分布是最典型的「有界」离散分布：掷一枚不均匀硬币 \(n\) 次，成功次数 \(k\) 的分布。它的概率质量函数为

\[ f(k) = \binom{n}{k} p^{k}(1-p)^{n-k},\qquad k\in\{0,1,\dots,n\} \]

它有两个形状参数：试验次数 \(n\)（整数）和单次成功概率 \(p\)（实数，\([0,1]\)）。它的**支撑有界**——\(k\) 只能取 \(0\) 到 \(n\) 之间的整数，不可能小于 0 或大于 \(n\)。

binom 在源码里还体现了一个重要细节：它把 `pmf/cdf/sf/ppf/isf` 五个方法的实际计算**委托给 Boost Math C++ 库**（见 [_discrete_distns.py:54-56](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_discrete_distns.py#L54-L56) 的说明），目的是数值更稳、更快。这是 scipy.stats 用编译扩展加速的代表案例（u17-l1 会系统讲 Cython/C 扩展）。

#### 4.2.2 核心流程

binom 的几个关键钩子如何协作：

1. **`_shape_info`**：声明 `n` 是整数参数、\(p\) 是实数参数，给出取值域——供新一代 `fit`/文档使用。
2. **`_argcheck`**：校验 `n>=0` 且为整数、`0<=p<=1`，不合法的位置会被公共方法填 `badvalue`。
3. **`_get_support`**：返回 `(self.a, n)`——上界是 \(n\)（随形状参数变化），下界是基类默认的 `self.a`（=0）。
4. **`_pmf/_cdf/_ppf`**：分别委托 Boost 例程 `scu._binom_pmf/_binom_cdf/_binom_ppf`。

注意 `_cdf` 和 `_ppf` 内部都先做了 `k = floor(x)`——这正是「整数支撑」的体现：对非整数输入，向下取整到最近的整数点再算。

#### 4.2.3 源码精读

`binom_gen` 定义在 [_discrete_distns.py:30-123](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_discrete_distns.py#L30-L123)，关键几处：

- [_discrete_distns.py:66-68](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_discrete_distns.py#L66-L68)：`_shape_info` 返回两个 `_ShapeInfo`——`n` 的 `integrality=True`（整数）、域 \([0,+\infty)\) 且下端闭；`p` 的 `integrality=False`（实数）、域 \([0,1]\) 两端都闭。对比 u3-l2 gamma 的 `a`（实数、域 \((0,+\infty)\)），可看出 `_ShapeInfo` 的 `integrality` 正是为离散分布的「整数形状参数」准备的。
- [_discrete_distns.py:75-76](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_discrete_distns.py#L75-L76)：`_argcheck` 用 `& _isintegral(n)` 强制 `n` 必须是整数，再要求 `p∈[0,1]`。
- [_discrete_distns.py:78-79](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_discrete_distns.py#L78-L79)：`_get_support` 返回 `self.a, n`。binom 实例化时没传 `a=`（见下），所以 `self.a` 取基类默认值 0；上界则是形状参数 `n`。**这一行就是「binom 支撑上界随 n 变化」的全部秘密。**
- [_discrete_distns.py:86-88](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_discrete_distns.py#L86-L88)：`_pmf(self, x, n, p)` 委托 `scu._binom_pmf(x, n, p)`（`scu` 即 `scipy.special._ufuncs`，见 [_discrete_distns.py:11](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_discrete_distns.py#L11) 的 `import scipy.special._ufuncs as scu`）。`_pmf` 签名里除 `self`、`x` 外有 `n, p` 两个参数，于是自动推断出 `numargs=2`、`shapes='n, p'`。
- [_discrete_distns.py:90-92](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_discrete_distns.py#L90-L92) 与 [_discrete_distns.py:101-102](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_discrete_distns.py#L101-L102)：`_cdf` 先 `k = floor(x)` 再委托 `scu._binom_cdf`；`_ppf` 直接委托 `scu._binom_ppf`。
- [_discrete_distns.py:123](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_discrete_distns.py#L123)：`binom = binom_gen(name='binom')`——实例化时**没传 `a=`**，因此 `self.a` 取 `rv_discrete.__init__` 的默认值 0（见 [_distn_infrastructure.py:3333](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L3333) 的 `a=0`），binom 支撑下界为 0。

再看 binom 与「离散分布无 scale」在公共方法里的对照。`pmf` 方法定义在 [_distn_infrastructure.py:3506-3542](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L3506-L3542)：

- [_distn_infrastructure.py:3525](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L3525)：`args, loc, _ = self._parse_args(*args, **kwds)`——`_parse_args` 仍会返回三元素（形状、loc、scale），但 scale 被丢进 `_`，**直接丢弃不用**。
- [_distn_infrastructure.py:3529](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L3529)：`k = asarray(k-loc)`——只减 `loc`，**没有除以 scale**。
- [_distn_infrastructure.py:3531](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L3531)：`cond1 = (k >= _a) & (k <= _b)`——用支撑域 `(_a,_b)` 过滤，支撑外的点 pmf 为 0。对 binom，`_a=0, _b=n`。

> 顺带一提：`bernoulli`（伯努利分布）是 `binom_gen` 的子类，通过「把 n 固定为 1」实现——[_discrete_distns.py:126-191](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_discrete_distns.py#L126-L191) 的 `bernoulli_gen(binom_gen)` 把所有方法转调 `binom._xxx(x, 1, p)`，并在 [_discrete_distns.py:191](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_discrete_distns.py#L191) 用 `bernoulli_gen(b=1, ...)` 把支撑上界设成 1。所以 bernoulli = 「n=1 的二项分布」。

#### 4.2.4 代码实践（本讲主任务之一）

**实践目标**：对 `binom(n=10, p=0.3)` 画出 pmf，并用 `ppf` 找到 95% 分位点对应的整数取值。

**操作步骤**（示例代码）：

```python
# 示例代码
import numpy as np
from scipy.stats import binom

n, p = 10, 0.3
k = np.arange(0, n + 1)               # 整数支撑 {0,1,...,10}
probs = binom.pmf(k, n, p)            # 每个整数点的概率质量

# 打印 pmf 表
for ki, pi in zip(k, probs):
    print(f"P(X={ki}) = {pi:.4f}")

# 95% 分位点：最小的 k 使 cdf(k) >= 0.95
q95 = binom.ppf(0.95, n, p)
print("ppf(0.95) =", q95)
print("cdf(int(ppf(0.95))) =", binom.cdf(int(q95), n, p))
print("cdf(int(ppf(0.95))-1) =", binom.cdf(int(q95) - 1, n, p))

# 可选：画图（需要 matplotlib）
# import matplotlib.pyplot as plt
# plt.vlines(k, 0, probs)
# plt.show()
```

**需要观察的现象**：

1. pmf 在 \(k=3\)（均值 \(np=3\)）附近最大，两侧递减；\(k=0\) 到 \(k=10\) 之外的概率为 0。
2. `ppf(0.95)` 应返回某个整数 \(k^\*\)，使得 `cdf(k*) >= 0.95` 而 `cdf(k*-1) < 0.95`——这印证 4.4 节要讲的「ppf 返回最小满足的 k」。
3. 支撑被严格限制在 \([0,10]\)：`binom.pmf(11, n, p)` 为 0，`binom.pmf(-1, n, p)` 也为 0。

**预期结果**：对 `n=10, p=0.3`，`ppf(0.95)` 约为 `5`，因为 `cdf(5)≈0.9527≥0.95` 而 `cdf(4)≈0.8497<0.95`（精确数值待本地验证）。这说明「95% 分位点」对应的整数取值是 5 次成功。

#### 4.2.5 小练习与答案

**练习 1**：`binom._get_support` 返回 `(self.a, n)`。为什么下界用 `self.a` 而不直接写 `0`？

参考答案：因为基类 `rv_discrete.__init__` 默认 `a=0`，binom 实例化时没传 `a=`，所以 `self.a` 恰好是 0。用 `self.a` 而非硬编码 0，是保留「构造器可改下界」的灵活性（虽然 binom 没用到）。上界则必须是形状参数 `n`，因为它随分布参数变化，不能写死。

**练习 2**：`binom.pmf(3.7, 10, 0.3)` 会得到什么？为什么？

参考答案：会得到 0（支撑外/非整数）。因为公共 `pmf` 用 `cond1 = (k >= _a) & (k <= _b)` 且 `_nonzero` 做整数判定，非整数 `3.7` 不在整数支撑上，pmf 为 0；而 `binom.cdf(3.7, ...)` 内部会 `floor(3.7)=3` 再算（cdf 是阶梯函数，\(3.7\) 处的值等于 \(k=3\) 处的值）。

---

### 4.3 poisson_gen：无界右支撑与 mu=0 边界

#### 4.3.1 概念说明

泊松分布描述「单位时间内随机事件发生次数」，是「无界」离散分布的代表——\(k\) 可以取任意非负整数，没有上界。它的概率质量函数为

\[ f(k) = e^{-\mu}\,\frac{\mu^{k}}{k!},\qquad k\in\{0,1,2,\dots\} \]

只有一个形状参数 \(\mu\ge 0\)（均值，也是方差）。与 binom 的「支撑 \([0,n]\) 有界」不同，poisson 的支撑是 \([0,+\infty)\)——上界无穷。这带来两个源码细节：一是它**不重写 `_get_support`**（沿用基类默认的 `(self.a, self.b)`，poisson 实例化没传 `a=/b=`，所以是 `(0, inf)`）；二是它**重写了 `_argcheck`** 以允许 \(\mu=0\) 这个退化情形（此时 `pmf(0)=1`，其余为 0）。

poisson 的 `_pmf` 走「先算 `_logpmf` 再取 exp」的路线，比直接算 \(\mu^k/k!\) 数值上稳得多——这是离散分布常见的数值技巧。

#### 4.3.2 核心流程

1. **`_argcheck`**：重写为只要求 `mu >= 0`（基类默认要求参数严格正，会拒绝 `mu=0`，故需重写）。
2. **`_logpmf`**：\( \text{xlogy}(k,\mu) - \ln\Gamma(k+1) - \mu \)，其中 `xlogy(0,0)=0` 处理 \(k=0,\mu=0\) 的 \(0\cdot\ln 0\)。
3. **`_pmf`**：`exp(self._logpmf(k, mu))`。
4. **`_cdf`**：`k = floor(x)` 后用 `special.pdtr(k, mu)`（正则化下不完全伽马）。
5. **`_ppf`**：用 `special.pdtrik`（`pdtr` 的反函数）再加边界修正。
6. **支撑**：不重写 `_get_support` → 沿用 `(0, inf)`。

#### 4.3.3 源码精读

`poisson_gen` 定义在 [_discrete_distns.py:965-1039](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_discrete_distns.py#L965-L1039)：

- [_discrete_distns.py:990-991](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_discrete_distns.py#L990-L991)：`_shape_info` 声明 `mu` 为实数、域 \([0,+\infty)\) 且下端闭——注意是**闭区间**（允许 \(\mu=0\)），与 binom 的 `n` 不同。
- [_discrete_distns.py:993-995](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_discrete_distns.py#L993-L995)：`_argcheck` 重写为 `return mu >= 0`，注释明说「Override rv_discrete._argcheck to allow mu=0」。基类默认的 `_argcheck` 要求参数严格正，会拒绝 \(\mu=0\)，故必须重写。
- [_discrete_distns.py:1000-1006](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_discrete_distns.py#L1000-L1006)：`_logpmf` 用 `special.xlogy(k, mu) - gamln(k+1) - mu`。`xlogy(k, mu)` 在 \(k=0\) 时返回 0（即使 \(\mu=0\)），优雅地避开了 \(0\cdot\ln 0\) 的 NaN；`_pmf` 就是 `exp(self._logpmf(k, mu))`。
- [_discrete_distns.py:1008-1010](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_discrete_distns.py#L1008-L1010)：`_cdf` 先 `k = floor(x)`，再用 `special.pdtr(k, mu)`（泊松 cdf，对应正则化下不完全伽马）。
- [_discrete_distns.py:1024-1028](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_discrete_distns.py#L1024-L1028)：`_ppf` 用 `pdtrik` 算候选值，再做「`temp = pdtr(vals-1, mu)`，若 `temp>=q` 则退一格」的修正——这正是离散 ppf「取最小满足的 k」的实现。
- [_discrete_distns.py:1039](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_discrete_distns.py#L1039)：`poisson = poisson_gen(name="poisson", ...)`——没传 `a=/b=`，沿用基类默认 `(0, inf)`。

poisson **没有重写 `_get_support`**，于是走基类 `rv_generic._get_support` 的默认实现 [_distn_infrastructure.py:1018-1038](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L1018-L1038)，返回 `self.a, self.b` 即 `(0, inf)`。这就是「无界右支撑」在源码里的体现——没有上界，只有 `floor(x)` 把任意实数归到整数。

**适用场景对比**（bernoulli / binom / poisson）：bernoulli 是「单次试验」（n=1 的二项），binom 是「有限次试验有上界」，poisson 是「无界计数」。当 \(n\) 很大、\(p\) 很小、\(np=\mu\) 适中时，binom 逼近 poisson——这是经典的「二项逼近泊松」，也是为什么三者常被放在一起比较。

#### 4.3.4 代码实践

**实践目标**：验证 poisson 的「均值=方差=mu」性质，并观察 `mu=0` 的退化行为。

**操作步骤**（示例代码）：

```python
# 示例代码
from scipy.stats import poisson

mu = 4.0
m, v = poisson.stats(mu, moments='mv')
print(f"mean={m}, var={v}")          # 期望都是 4.0

# mu=0 的退化：P(X=0)=1
print("pmf(0; mu=0) =", poisson.pmf(0, 0))   # 期望 1.0
print("pmf(1; mu=0) =", poisson.pmf(1, 0))   # 期望 0.0

# 无界支撑：pmf 在很大的 k 上仍有微小正值
print("pmf(20; mu=4) =", poisson.pmf(20, 4)) # 非常小但不为 0
```

**需要观察的现象**：

1. `mean` 与 `var` 都等于 `mu=4`（泊松分布的标志性性质）。
2. `mu=0` 时 `pmf(0)=1`、其余为 0——这正是 [_discrete_distns.py:981-982](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_discrete_distns.py#L981-L982) 文档承诺的行为，得益于 `_argcheck` 的重写与 `xlogy`。
3. 即使 \(k=20\) 远大于均值，`pmf` 仍是正数（不像 binom 在 \(k>n\) 时严格为 0）——印证「无界右支撑」。

**预期结果**：`mean=4.0, var=4.0`；`pmf(0;mu=0)=1.0`、`pmf(1;mu=0)=0.0`；`pmf(20;mu=4)` 是一个极小正数（约 `8.3e-9`，待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：poisson 为什么必须重写 `_argcheck`？

参考答案：基类 `rv_discrete` 默认的 `_argcheck` 要求所有形状参数严格为正，会把合法的 `mu=0` 判为非法、结果填 `badvalue`。泊松在 `mu=0` 时有明确定义（退化到 \(k=0\)），所以 [_discrete_distns.py:994-995](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_discrete_distns.py#L994-L995) 重写为 `mu >= 0`。

**练习 2**：`poisson._pmf` 为什么不直接写 `exp(-mu)*mu**k/factorial(k)`，而要先算 `_logpmf` 再取 exp？

参考答案：为了数值稳定。当 \(k\) 较大时，\(\mu^k\) 和 \(k!\) 都是巨大的数，直接相除会溢出；改在对数域计算 `k*ln(mu) - ln(k!) - mu`（用 `xlogy` 和 `gammaln`），最后只 exp 一次，能把动态范围压缩在浮点可表示区间内。`xlogy` 还顺带处理了 \(k=0\) 时的 \(0\cdot\ln\mu\)。

---

### 4.4 geom_gen：从 1 开始的支撑与解析 ppf

#### 4.4.1 概念说明

几何分布描述「首次成功前需要的试验次数」。scipy 的 `geom` 约定支撑从 **1** 开始（即「试验到第几次才首次成功」，至少 1 次），概率质量函数为

\[ f(k) = (1-p)^{k-1}\,p,\qquad k\in\{1,2,3,\dots\} \]

只有一个形状参数 \(p\in(0,1]\)。它与 binom/poisson 有两点不同：

1. **支撑从 1 开始**，而非从 0。这通过实例化时传 `a=1` 实现（`geom_gen(a=1, ...)`），让基类默认 `self.a=0` 变成 `self.a=1`。
2. **`_cdf` 和 `_ppf` 有解析（闭式）表达式**，不依赖 Boost/特殊函数——`_cdf` 用 `−expm1(log1p(−p)*k)`，`_ppf` 用 `ceil(log1p(−q)/log1p(−p))`。这让 geom 成为「读源码学离散 ppf 推导」的最佳例子。

#### 4.4.2 核心流程

geom 的累积分布函数可解析推出：\(F(k)=P(X\le k)=1-(1-p)^k\)。scipy 用 `log1p(-p)` 与 `expm1` 数值稳定地实现它：

1. **`_cdf`**：`k = floor(x)`，返回 `−expm1(log1p(−p)*k)`（即 \(1-(1-p)^k\)，`expm1` 保证 \(k\) 小时精度）。
2. **`_ppf`**：由 \(q=1-(1-p)^k\) 解出 \(k=\lceil \ln(1-q)/\ln(1-p)\rceil\)，再做一个「若 `cdf(k-1)≥q` 则退一格」的整数修正。
3. **`_argcheck`**：要求 `0 < p <= 1`（注意 \(p\) 不能为 0，否则永远不成功）。
4. **支撑**：不重写 `_get_support` → 沿用 `(self.a, self.b)`，而 `self.a=1`（实例化传入），所以支撑是 \([1,+\infty)\)。

#### 4.4.3 源码精读

`geom_gen` 定义在 [_discrete_distns.py:497-577](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_discrete_distns.py#L497-L577)：

- [_discrete_distns.py:530-531](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_discrete_distns.py#L530-L531)：`_shape_info` 声明 `p` 为实数、域 \([0,1]\) 两端闭（虽然 `_argcheck` 实际要求严格 `p>0`）。
- [_discrete_distns.py:540-541](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_discrete_distns.py#L540-L541)：`_argcheck` 要求 `(p <= 1) & (p > 0)`——\(p=0\) 非法。
- [_discrete_distns.py:543-551](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_discrete_distns.py#L543-L551)：`_pmf` 是 `(1-p)^(k-1)*p`；`_cdf` 是 `−expm1(log1p(−p)*k)`。`log1p(-p)` 比 `log(1-p)` 在 \(p\) 接近 0 时更准，`expm1` 比 `exp-1` 在结果接近 0 时更准——这是教科书级的数值稳定写法。
- [_discrete_distns.py:560-563](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_discrete_distns.py#L560-L563)：`_ppf` 先算候选 `vals = ceil(log1p(-q) / log1p(-p))`，再 `temp = self._cdf(vals-1, p)`，若 `temp >= q` 且 `vals>0` 就退到 `vals-1`。这段「退一格」逻辑是离散 ppf「最小满足 k」的精髓。
- [_discrete_distns.py:577](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_discrete_distns.py#L577)：`geom = geom_gen(a=1, name='geom', ...)`——**传了 `a=1`**，于是 `self.a=1`，支撑下界变成 1。对比 binom/poisson 都没传 `a=`（下界为 0），这是 geom「支撑从 1 开始」的根因。

现在把视线拉回到「`ppf` 的离散语义」。公共 `ppf` 方法 [_distn_infrastructure.py:3745-3792](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L3745-L3792) 的文档 [_distn_infrastructure.py:3765-3768](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L3765-L3768) 写得很直白：

> 「For discrete distributions, the `cdf` is not strictly invertible. By convention, this method returns the minimum value `k` for which the `cdf` at `k` is at least `q`. There is one exception: the `ppf` of `0` is `a-1`.」

三处代码对应三段语义：

- [_distn_infrastructure.py:3783](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L3783)：`q==0` 时返回 `_a - 1 + loc`——特例「ppf(0)=a−1」，是为了让「cdf(a−1)=0」与「ppf(0)=a−1」严格互逆。
- [_distn_infrastructure.py:3784](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L3784)：`q==1` 时返回 `_b + loc`（支撑上界）。
- [_distn_infrastructure.py:3788](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L3788)：其余情况 `self._ppf(*goodargs) + loc`——注意是 **`+ loc`**，没有 `* scale`，再次印证「离散无 scale」。

最后看 `support` 方法。`rv_discrete.support` 直接转调基类 [_distn_infrastructure.py:3981-3982](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L3981-L3982)，而基类 `rv_generic.support` 在 [_distn_infrastructure.py:1546](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L1546) 返回 `_a*scale + loc, _b*scale + loc`——对离散，scale 恒为 1，所以就是 `_a + loc, _b + loc`。对 geom（`_a=1,_b=inf`），`geom.support(p)` 返回 `(1, inf)`；若加 `loc=2`，则返回 `(3, inf)`。

#### 4.4.4 代码实践

**实践目标**：验证 geom 的支撑从 1 开始，并手工核对 `_ppf` 的解析公式。

**操作步骤**（示例代码）：

```python
# 示例代码
import numpy as np
from scipy.stats import geom
from numpy import ceil, log1p

p = 0.25
print("support =", geom.support(p))          # 期望 (1, inf)
print("pmf(0; p) =", geom.pmf(0, p))          # 期望 0.0（0 不在支撑内）
print("pmf(1; p) =", geom.pmf(1, p))          # 期望 0.25 == p

# 手工算 ppf：k = ceil(log1p(-q)/log1p(-p))
q = 0.5
k_manual = ceil(log1p(-q) / log1p(-p))
print("手算 ppf(0.5) =", k_manual)
print("scipy ppf(0.5) =", geom.ppf(q, p))

# 检验「最小满足 k」语义
k = int(geom.ppf(q, p))
print("cdf(k) =", geom.cdf(k, p), ">= 0.5 ?", geom.cdf(k, p) >= q)
print("cdf(k-1) =", geom.cdf(k - 1, p), "< 0.5 ?", geom.cdf(k - 1, p) < q)
```

**需要观察的现象**：

1. `geom.support(p)` 返回 `(1, inf)`，`pmf(0)` 为 0——支撑从 1 开始。
2. 手算的 `ceil(log1p(-0.5)/log1p(-0.25))` 与 `geom.ppf(0.5)` 一致。
3. `cdf(k) >= 0.5` 且 `cdf(k-1) < 0.5`——验证「ppf 返回最小满足 cdf≥q 的 k」。

**预期结果**：`support=(1, inf)`、`pmf(0)=0`、`pmf(1)=0.25`；`ppf(0.5)` 约为 `3`（因为 \(1-0.75^3≈0.578≥0.5\)，\(1-0.75^2≈0.4375<0.5\)，待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：geom 的支撑为什么从 1 开始？源码里这一行在哪？

参考答案：因为 [_discrete_distns.py:577](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_discrete_distns.py#L577) 实例化时传了 `a=1`，使基类 `self.a=1`；geom 又没重写 `_get_support`，故支撑下界取 `self.a=1`。这对应「首次成功至少要 1 次试验」的语义。

**练习 2**：为什么离散分布的 `ppf(0)` 不返回支撑下界 `a`，而返回 `a-1`？

参考答案：为了让 `ppf` 与 `cdf` 严格互逆。`cdf(a-1)=0`（\(a-1\) 在支撑之外，概率为 0），所以 `ppf(0)` 应返回 `a-1`；若返回 `a`，则 `cdf(a)>0` 会破坏互逆性。这是 [_distn_infrastructure.py:3765-3768](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L3765-L3768) 文档与 L3783 代码共同约定的特例。

---

## 5. 综合实践

把本讲的「pmf 取代 pdf、无 scale、整数支撑、ppf 离散语义」串起来，完成下面这个以 binom 为中心的小任务。

1. **数形状参数**：读 [_discrete_distns.py:86-88](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_discrete_distns.py#L86-L88) 的 `_pmf(self, x, n, p)`，确认 binom 有 2 个形状参数 `n, p`；再用 `binom.numargs`/`binom.shapes` 复核。
2. **画 pmf 并找 95% 分位点**（本讲主任务）：对 `binom(n=10, p=0.3)`，用 `np.arange(0, n+1)` 生成整数支撑，调用 `binom.pmf` 得到每个点的概率；再调 `binom.ppf(0.95, 10, 0.3)` 找 95% 分位点。预期 `ppf(0.95)` 为 5。
3. **验证 ppf 语义**：对第 2 步得到的 \(k^\*=\text{ppf}(0.95)\)，分别算 `binom.cdf(k*, ...)` 与 `binom.cdf(k*-1, ...)`，确认前者 \(\ge 0.95\)、后者 \(<0.95\)——这正是 [_distn_infrastructure.py:3765-3768](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L3765-L3768) 约定的「最小满足 k」。
4. **对比 poisson**：把 binom 的 `cdf` 与 `poisson(mu=3).cdf` 在 \(k=0..10\) 上逐点比较，观察「\(n\) 大、\(p\) 小、\(np=\mu\) 时 binom 逼近 poisson」——可改用 `binom(n=30, p=0.1)`（均值仍为 3）看逼近更好。
5. **体会「无 scale」**：尝试给 `binom.ppf(0.95, 10, 0.3, scale=2)` 传 `scale`，观察结果是否变化（提示：不会变，因为 [_distn_infrastructure.py:3771](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L3771) 把 scale 丢进了 `_`）。改用 `loc=5` 则分位点会平移 5。

完成后换 `geom` 重复第 2、3 步（注意支撑从 1 开始），体会「有界 / 无界右 / 从 1 开始」三种支撑的差异。

---

## 6. 本讲小结

- 离散分布与连续分布共用「`*_gen` 单例 + 公共/私有双层方法」骨架，但有三处差异（[_distn_infrastructure.py:3253-3268](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L3253-L3268)）：**支撑是整数**、用 **`pmf`/`_pmf`** 代替 `pdf`/`_pdf`、**没有 `scale`**。
- 「没有 scale」的实锤：`rv_discrete.__init__` 写死 `locscale_out='loc, 1'`（[_distn_infrastructure.py:3357-3360](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L3357-L3360)），公共方法里 scale 被丢进 `_` 弃用；于是标准化退化为 \(X=\text{loc}+Y\)，「只平移」。
- 形状参数仍由 `_construct_argparser` 从钩子签名自动推断，只是离散检查的是 `_pmf`/`_cdf`（连续是 `_pdf`/`_cdf`）。
- **binom**（[_discrete_distns.py:30-123](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_discrete_distns.py#L30-L123)）：有界整数支撑 \(\{0,\dots,n\}\)，`_get_support` 返回 `(0, n)` 让上界随 n 变；`pmf/cdf/ppf` 委托 Boost。
- **poisson**（[_discrete_distns.py:965-1039](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_discrete_distns.py#L965-L1039)）：无界右支撑 \([0,\infty)\)，重写 `_argcheck` 以允许 `mu=0`，`_pmf` 走「`_logpmf` 再 exp」保数值稳定。
- **geom**（[_discrete_distns.py:497-577](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_discrete_distns.py#L497-L577)）：支撑从 1 开始（实例化传 `a=1`），`_cdf/_ppf` 有解析式，是读懂离散 ppf 推导的范本。
- 离散 `ppf` 因 cdf 是阶梯函数而不可严格求逆，约定为「最小的 k 使 cdf(k)≥q」，特例 `ppf(0)=a−1`（[_distn_infrastructure.py:3765-3768](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L3765-L3768)、[L3783-L3784](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_distn_infrastructure.py#L3783-L3784)）。

---

## 7. 下一步学习建议

- 至此 u3 单元（分布入门）的三讲已结束：u3-l1 讲骨架、u3-l2 讲连续分布用法、本讲讲离散分布用法。进入 **u4 单元（分布基础设施深入）**：u4-l1 会展开 `rv_generic` 与 frozen 分布对象的内部缓存机制，u4-l2 讲 `pdf/cdf/logpdf/sf` 的派生关系与 `fit` 的最大似然流程。
- 想系统了解 binom 等为何用 Boost C++ 加速、以及 `_biasedurn`/`_stats.pyx` 等编译扩展，进入 **u17-l1（Cython 加速扩展）**。
- 想看离散分布如何做拟合（如把 poisson 拟合到计数数据）与拟合优度检验，进入 **u13 单元（分布拟合与拟合优度）**。
- 想了解新一代分布基础设施（`make_distribution`/`ContinuousDistribution`/`DiscreteDistribution`）如何统一取代旧的 `rv_continuous`/`rv_discrete`，进入 **u12 单元（新一代分布基础设施）**——届时 `_ShapeInfo`、`_get_support` 这套机制会有更现代的对应物。
- 建议同时翻一遍 [_discrete_distns.py](_discrete_distns.py) 的目录（用 `grep '^class ' _discrete_distns.py`），把「分布名 → 形状参数」这张地图记在脑子里，再挑 `hypergeom`（无放回抽样）或 `nbinom`（负二项）对照本讲三步法（数形状参数、看 `_get_support`、看 `_pmf/_cdf/_ppf`）自己读一遍。
