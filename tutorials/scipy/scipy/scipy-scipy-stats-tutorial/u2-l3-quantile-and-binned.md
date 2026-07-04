# 分位数与频率统计

## 1. 本讲目标

学完本讲，你应当能够：

- 说出「分位数」与「经验累积分布」的直觉含义，并理解它们为什么互为反函数。
- 用 `scipy.stats.quantile` 计算任意概率分位，并能区分 `linear`、`weibull`、`normal_unbiased`、`harrell-davis` 等多种 `method` 估计的差异。
- 用 `scipy.stats.estimated_cdf` 从样本估计经验累积分布函数（CDF）。
- 用 `scipy.stats.binned_statistic` 把数据「分箱」后对每个箱子计算 mean/sum/median/count 等统计量。
- 看懂 `_quantile.py` 与 `_binned_statistic.py` 的核心实现，并能定位到具体函数。

本讲只讲「频率统计」这一组工具，不涉及概率分布对象（那是 u3/u4 的内容）。

## 2. 前置知识

本讲假设你已经学过 **u2-l1**（`describe` 与各种平均）与 **u2-l2**（矩、偏度、峰度）。在它们里面，所有统计函数都被 `@_axis_nan_policy_factory` 装饰器统一注入了 `axis` / `nan_policy` / `keepdims` 能力。本讲你会看到**一个重要的例外**：`quantile` 和 `estimated_cdf` 是较新的函数，它们**没有**走那个共享装饰器，而是自带一套输入校验管线 `_quantile_iv`。这一点记在心里，等到 **u5-l1** 讲 `_axis_nan_policy` 装饰器时你会更清楚为什么。

复习三个关键词：

- **样本（sample）**：从某个未知分布里抽出来的一组数 `x`，比如 `[3, 1, 4, 1, 5]`。
- **经验分布（empirical distribution）**：把样本本身当作分布——每个观测点占 `1/n` 的概率质量。本讲的 `quantile` 和 `estimated_cdf` 都是对「经验分布」做估计。
- **分箱（binning）**：把数值轴切成一段段区间（箱子），把每个观测点归到某个箱子里，再在每个箱子内做聚合。这就是直方图的推广。

另外请记住 u1-l3 的结论：`scipy.stats` 靠 `from ._xxx import` 把分散文件里的名字搬运进统一命名空间。本讲的三个函数分别来自 `_quantile.py` 和 `_binned_statistic.py`。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注的关键符号 |
| --- | --- | --- |
| [`_quantile.py`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_quantile.py) | 经验分位数与经验 CDF 的实现 | `quantile`、`estimated_cdf`、共享校验 `_quantile_iv`、核心算法 `_quantile_hf`/`_quantile_hd`/`_estimated_cdf_hf`、输出整形 `_post_quantile` |
| [`_binned_statistic.py`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_binned_statistic.py) | 分箱统计（直方图的推广） | `binned_statistic`、真正的引擎 `binned_statistic_dd`、建边 `_bin_edges`、归箱 `_bin_numbers`、聚合 `_bincount` |

搬运路径：`quantile`、`estimated_cdf` 在 [`__init__.py:631`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/__init__.py#L631) 由 `from ._quantile import quantile, estimated_cdf` 引入；`binned_statistic*` 三件套在 [`__init__.py:606`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/__init__.py#L606) 由 `from ._binned_statistic import *` 引入。它们也都出现在 [`__init__.py:282-294`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/__init__.py#L282-L294) 的模块文档「功能地图」里。

---

## 4. 核心概念与源码讲解

### 4.1 quantile：经验分位函数

#### 4.1.1 概念说明

「分位数」回答这样一个问题：**给定一个概率 p（0 到 1 之间），样本里有多大一个数，使得大约比例为 p 的数据不超过它？**

- p = 0.5 的分位数就是**中位数**（median）。
- p = 0.25 是**下四分位数**，p = 0.75 是**上四分位数**。
- p = 0 是样本最小值，p = 1 是样本最大值。

数学上，`quantile` 估计的是分布的「**反累积分布函数**」（分位函数 \(Q(p)\)）。直觉是：把样本排序成 \(y_{[1]} \le y_{[2]} \le \dots \le y_{[n]}\)，然后在排好序的数轴上「按比例 p 取一个位置」。

问题来了：排序后只有 n 个离散的点，但 p 是连续的。比如 n = 8 个点、p = 0.25，位置 0.25 落在哪两个点之间？**不同 `method` 就是不同的「在两点之间插值」的约定**。这就是本讲最容易混淆、也最重要的概念。

#### 4.1.2 核心流程

`quantile` 的整体流程：

1. **输入校验与标准化**（`_quantile_iv`）：检查 `x`/`p`/`weights` 的类型、`method` 是否合法、`axis` 是否合法，处理 NaN 策略，把数据沿 `axis` 排序得到 `y`，并把 `axis` 移到最后一维方便统一计算。
2. **按 method 分发到不同算法**：
   - 9 种 Hyndman & Fan（H&F）方法 → `_quantile_hf`
   - `harrell-davis` → `_quantile_hd`
   - 兼容旧 NumPy 的 `_lower/_higher/_midpoint/_nearest` → `_quantile_bc`
   - 用于缩尾/修剪的 `round_*` → `_quantile_winsor`
3. **输出整形**（`_post_quantile`）：把 NaN 填回、按 `keepdims` 调整形状。

**线性插值的直觉（默认 `method='linear'`）**：把排序后的样本当作折线的顶点，横坐标是 \(0, \tfrac{1}{n-1}, \tfrac{2}{n-1}, \dots, 1\)，纵坐标是排序后的数据。给定 p，就在这条折线上取值：

\[ \hat{Q}(p) = (1-g)\,y_{[j]} + g\,y_{[j+1]}, \quad j=\lfloor p(n-1)\rfloor,\; g = p(n-1)-j \]

其中 \(y_{[j]}\) 是排序后第 j 个（0 基）元素，\(j\) 是整数部分下标，\(g \in [0,1)\) 是小数部分插值系数。

**更一般的 H&F 公式**：不同方法只是改了一个常数偏移 \(m\)：

\[ \text{index} = pn + m - 1,\quad j=\lfloor\text{index}\rfloor,\quad g=\text{index}-j \]

源码里的 `m` 对照表（摘自函数文档字符串）：

| method | H&F 编号 | \(m\) |
| --- | --- | --- |
| `interpolated_inverted_cdf` | 4 | 0 |
| `hazen` | 5 | 1/2 |
| `weibull` | 6 | p |
| `linear`（默认） | 7 | 1 − p |
| `median_unbiased` | 8 | p/3 + 1/3 |
| `normal_unbiased` | 9 | p/4 + 3/8 |

还有一个完全不同的 `harrell-davis` 方法：它不挑两个相邻点插值，而是**用全部样本点的加权平均**，权重来自不完全贝塔函数：

\[ w_{n,i} = I_{i/n}(a,b) - I_{(i-1)/n}(a,b),\quad a=p(n+1),\; b=(1-p)(n+1) \]

\[ \hat{Q}(p) = \sum_{i=1}^{n} w_{n,i}\,y_{[i]} \]

它的优点是对小样本更稳健，但计算更贵，且不支持权重。

#### 4.1.3 源码精读

**函数签名与装饰器**——注意是 `@xp_capabilities`（数组 API 适配），而**不是** u2-l1 见过的 `@_axis_nan_policy_factory`：

[_quantile.py:157-162](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_quantile.py#L157-L162) —— `quantile` 用 `@xp_capabilities` 装饰后定义，签名是 `quantile(x, p, *, method='linear', axis=0, nan_policy='propagate', keepdims=None, weights=None)`。`*` 表示 `method` 之后全是关键字参数。

**入口：校验 + 分发 + 整形三步走**：

[_quantile.py:393-408](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_quantile.py#L393-L408) —— 先调 `_quantile_iv` 拿到排序后的 `y`、修正后的 `n`、`p_mask` 等；再用一连串 `if method in {...}` 把不同方法分发到 `_quantile_hf` / `_quantile_hd` / `_quantile_bc` / `_quantile_winsor`；最后用 `_post_quantile` 统一整形。这就是「策略模式」：校验、算法、整形三者解耦。

**共享校验管线 `_quantile_iv`**（本函数与 `estimated_cdf` 共用）：

[_quantile.py:19-20](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_quantile.py#L19-L20) —— 它通过参数 `fun='quantile'` 还是 `'estimated_cdf'` 切换：合法 `method` 集合、第二个参数叫 `p` 还是 `y`、类型校验都因此不同。这就是为什么两个函数能共用一大段校验代码。

[_quantile.py:91-102](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_quantile.py#L91-L102) —— **排序**：无权重时直接 `xp.sort(x)`；有权重时先把零权重点设成 `+inf`（让它们排到最后），再 `argsort` 并同步重排 `weights`，保证数据和权重对齐。这就是 `weights` 参数的实现基础。

**核心算法 `_quantile_hf`（9 种 H&F 方法共用）**：

[_quantile.py:429-431](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_quantile.py#L429-L431) —— 用一个字典 `ms` 把每个方法映射到它的偏移 \(m\)。这一行就是上表 \(m\) 的代码化身：`linear=1 - p`、`weibull=p` 等等。

[_quantile.py:435-437](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_quantile.py#L435-L437) —— 无权重时核心三行：`jg = p*n + m`（即 index+1）、`jp1 = jg // 1`（取整得 \(j+1\)）、`j = jp1 - 1`。这就是「整数部分与小数部分分离」。

[_quantile.py:463-464](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_quantile.py#L463-L464) —— 最终插值：`(1-g)*y[j] + g*y[j+1]`，用 `take_along_axis` 在排序数组里按下标取值。这一行就是公式 \(\hat{Q}(p)=(1-g)y_{[j]}+g\,y_{[j+1]}\)。

**Harrell-Davis `_quantile_hd`**：

[_quantile.py:474-480](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_quantile.py#L474-L480) —— 计算 `a = p*(n+1)`、`b = (1-p)*(n+1)`，用 `betainc`（正则化不完全贝塔函数）算累计权重再差分得到每个点的权重 `w`，最后 `vecdot(w, y)` 做加权求和。这正是上面那条 \(\sum_i w_{n,i}y_{[i]}\) 公式。

#### 4.1.4 代码实践

**目标**：用同一组数据、同一个 p，比较 3 种以上 `method` 的分位估计差异，亲手验证「方法不同结果不同」。

**操作步骤**：

```python
# 示例代码
import numpy as np
from scipy import stats

x = np.arange(8.0)              # [0,1,2,3,4,5,6,7], n=8
p = 0.25
for m in ['linear', 'weibull', 'normal_unbiased', 'hazen']:
    print(f"{m:18s} p={p} -> {stats.quantile(x, p, method=m)}")
# harrell-davis 用全部样本加权，单独看
print(f"{'harrell-davis':18s} p={p} -> {stats.quantile(x, p, method='harrell-davis')}")
```

**需要观察的现象**：四个 H&F 方法会给出**不同**的数，说明同一个 p 在「两点之间插值」的方式不同。

**预期结果**（按 `_quantile_hf` 的公式手算）：

- `linear`：index = p(n−1) = 0.25×7 = 1.75，j=1，g=0.75 → 0.25×y[1] + 0.75×y[2] = 0.25×1 + 0.75×2 = **1.75**
- `weibull`：m=p，index = pn+m−1 = 0.25×8+0.25−1 = 1.25，j=0，g=0.25 → 0.75×y[0]+0.25×y[1] = **0.25**
- `normal_unbiased`：m = p/4+3/8 = 0.4375，index = 0.25×8+0.4375−1 = 1.4375 → **0.4375**
- `hazen`：m=0.5，index = 0.25×8+0.5−1 = 1.5 → j=1，g=0.5 → 0.5×1+0.5×2 = **1.5**
- `harrell-davis`：依赖 `betainc`，精确值**待本地验证**，但它会在 0 与 1 之间、且与上面都不同。

**结论**：从小到大排大致是 weibull(0.25) < normal_unbiased(0.4375) < hazen(1.5) < linear(1.75)。方法越「靠左」（小 m），分位估计越偏向小值。

#### 4.1.5 小练习与答案

**练习 1**：对 `x = [10, 8, 7, 5, 4]`（n=5）用默认 `linear` 方法求 p=0.5（中位数）。请手算并与 `stats.quantile(x, 0.5, axis=-1)` 对照。

答案：排序后 `y = [4,5,7,8,10]`，index = 0.5×(5−1) = 2，j=2，g=0，结果 = y[2] = **7.0**。

**练习 2**：为什么 `method='harrell-davis'` 不允许传 `weights`？（提示：看 `_quantile_iv` 里的 `no_weights` 集合。）

答案：因为 Harrell-Davis 已经用「全部样本的贝塔加权」定义了权重，再叠加频率权重会语义冲突；源码在 [`_quantile.py:71-75`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_quantile.py#L71-L75) 把它列入 `no_weights`，传权重会抛 `ValueError`。

**练习 3**：`weights=[1,1,1,1,1]` 与不传 `weights`，对 `linear` 方法结果应否相同？

答案：相同。全 1 权重等价于无权重（每个点贡献相等），源码里有权重分支 [`_quantile.py:438-446`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_quantile.py#L438-L446) 用累积权重 `cumulative_sum` 定位，全 1 时退化为无权重情形。

---

### 4.2 estimated_cdf：经验累积分布函数

#### 4.2.1 概念说明

「累积分布函数（CDF）」回答的是**反过来的问题**：给定一个数值 y，样本里有多大比例的数据**不超过** y？记作 \(\hat{F}(y)\)。

- 如果 y 很小（小于所有样本），\(\hat{F}(y)=0\)。
- 如果 y 很大（大于所有样本），\(\hat{F}(y)=1\)。
- 如果 y 正好等于中位数，\(\hat{F}(y)=0.5\)。

注意它正好是 `quantile` 的**反函数**：`quantile` 是「给概率求数值」，`estimated_cdf` 是「给数值求概率」。源码里专门强调：当样本无重复值、且用 `linear` 方法时，两者在某个定义域内互为逆运算。

最朴素的 CDF 估计是「**经验累积分布函数（ECDF）**」：\(\hat{F}(y) = \frac{1}{n}\#\{i: x_i \le y\}\)，它是一个阶梯函数。`estimated_cdf` 提供了它（`method='inverted_cdf'`）以及一系列**插值平滑**的变体。

#### 4.2.2 核心流程

`estimated_cdf` 的流程几乎与 `quantile` 镜像：

1. **复用 `_quantile_iv`** 做校验（传 `fun='estimated_cdf'`），同样拿到排序后的 `x`、`n`、掩码等。
2. **核心算法 `_estimated_cdf_hf`**：用 `_xp_searchsorted` 找出 y 落在排序数组的哪个间隙，然后按 method 插值出概率 p。
3. **复用 `_post_quantile`** 整形输出。

**线性插值的直觉（默认 `method='linear'`）**：在排序数组 `z` 中找到最大的下标 j 使 `z[j] ≤ y`，那么：

\[ \hat{F}(y) = \frac{1}{n-1}\left( j + \frac{y - z_{[j]}}{z_{[j+1]} - z_{[j]}} \right) \]

即 y 落在第 j 与第 j+1 个排序点之间的什么「分数位置」。若 y 正好等于某个样本点 `z[j]`，公式就退化成直观的 `j/(n-1)`。

#### 4.2.3 源码精读

**函数签名**：

[_quantile.py:554-555](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_quantile.py#L554-L555) —— `estimated_cdf(x, y, *, method='linear', axis=0, nan_policy='propagate', keepdims=None)`。注意第二参数叫 `y`（要评估的点），这与 `quantile` 的 `p` 对偶。

**入口：复用校验管线**：

[_quantile.py:757-759](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_quantile.py#L757-L759) —— 调 `_quantile_iv(x, y, ..., fun='estimated_cdf')`，把 `fun` 切到 `'estimated_cdf'` 分支，于是合法方法集合、第二参数名、类型校验都自动切换。这是与 `quantile` 共享代码的关键设计。

**核心算法 `_estimated_cdf_hf`**：

[_quantile.py:794](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_quantile.py#L794) —— `_xp_searchsorted(x, y, side='right')` 找出 y 在排序数组里的插入位置 `jp1`，这决定了 y 落在哪两个样本点之间。

[_quantile.py:805-809](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_quantile.py#L805-L809) —— 连续方法分支：算出间隙内的插值分数 `delta`（除以相邻样本差），再用 `(jp1 + delta - a)/(n + 1 - a - b)` 得到概率。这里的 `a, b` 来自方法字典 [`_estimated_cdf_continuous_methods`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_quantile.py#L776-L783)（`linear` 对应 `(1,1)`），与 `quantile` 的 \(m\) 表对应。

[_quantile.py:811-816](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_quantile.py#L811-L816) —— 边界裁剪：y 小于样本最小值时概率置 0，大于最大值时置 1，最后 `clip` 到 \([0,1]\)。这就是「y 在样本范围外」的处理。

#### 4.2.4 代码实践

**目标**：验证 `estimated_cdf` 与 `quantile` 互为反函数。

**操作步骤**：

```python
# 示例代码
import numpy as np
from scipy import stats

x = list(range(11))                       # [0,1,...,10], n=11
print("estimated_cdf(x, 5):", stats.estimated_cdf(x, 5, axis=-1))
print("estimated_cdf(x, [2.5, 7.5]):", stats.estimated_cdf(x, [2.5, 7.5], axis=-1))

# 往返测试：quantile -> estimated_cdf 应还原 p
rng = np.random.default_rng(0)
xs = rng.standard_normal(300)
p = np.linspace(0, 1, 300)
y = stats.quantile(xs, p)
p2 = stats.estimated_cdf(xs, y)
print("往返最大误差:", np.max(np.abs(p2 - p)))
```

**需要观察的现象**：`estimated_cdf(x, 5)` 应等于 0.5（中位数处 CDF 为 0.5）；`estimated_cdf(x, [2.5, 7.5])` 应为 `[0.25, 0.75]`；往返误差应接近 0。

**预期结果**：
- `estimated_cdf(x, 5)` = **0.5**。手算：z=[0..10]，n=11，y=5 正好等于 z[5]，p = 5/(11−1) = 0.5。
- `estimated_cdf(x, [2.5, 7.5])` = **[0.25, 0.75]**。y=2.5 落在 z[2]=2 与 z[3]=3 正中间，p = (2 + 0.5)/10 = 0.25；y=7.5 同理得 0.75。
- 往返误差：**待本地验证**，但 `linear` 方法在样本唯一时理论上可精确还原（文档示例用 `np.testing.assert_allclose` 断言）。

#### 4.2.5 小练习与答案

**练习 1**：`method='inverted_cdf'` 给出的就是经典 ECDF 阶梯函数。对 `x=[1,2,3,4]`，求 `estimated_cdf(x, [2, 2.5])`（用 `inverted_cdf`）。

答案：`inverted_cdf` 用 `m=0`，\(p = j_p1/n\)（见 [`_quantile.py:796-798`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_quantile.py#L796-L798)）。y=2：≤2 的有 {1,2} 共 2 个，p=2/4=**0.5**；y=2.5：≤2.5 的仍只有 {1,2}，p=**0.5**（阶梯函数在 2 与 3 之间恒为 0.5）。

**练习 2**：为什么 `estimated_cdf` 没有 `weights` 参数？（对比 `quantile`。）

答案：`quantile` 的权重是「频率权重」（重复计数），其反问题「y 落在哪个累积权重位置」才有意义；而 `estimated_cdf` 评估的是经验分布的累积概率，权重语义不明确，故入口 [`_quantile.py:757-758`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_quantile.py#L757-L758) 直接写死 `weights=None`。

**练习 3**：当 y 大于样本最大值时，`estimated_cdf` 返回什么？

答案：返回 1.0。由 [`_quantile.py:814`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_quantile.py#L814) 的 `p = xpx.at(p)[y > xmax].set(1.)` 强制设定。

---

### 4.3 binned_statistic：分箱统计

#### 4.3.1 概念说明

直方图只能数「每个箱子里有几个点」（count）。`binned_statistic` 是它的推广：把数值轴切成若干**箱子（bin）**，把每个观测点按它的 `x` 值归入某个箱子，然后对每个箱子里的 **`values`** 计算任意统计量——mean、sum、median、min、max、count，甚至你自己写的函数。

典型场景：你有「风速」和「船速」两组配对数据，想知道「不同风速区间内，平均船速是多少」。这里 `x` 是风速（用来分箱），`values` 是船速（用来算均值）。

`binned_statistic` 是 1 维版；还有 `binned_statistic_2d`（2 维）和 `binned_statistic_dd`（N 维），但**三者内部都委托给 `binned_statistic_dd`**——后者才是真正的引擎。

#### 4.3.2 核心流程

`binned_statistic_dd` 的流程：

1. **校验**：`statistic` 必须是 `known_stats` 之一或可调用对象；样本含 NaN 直接报错（语义歧义）。
2. **建边 `_bin_edges`**：根据 `bins`（个数或显式边）和 `range` 生成每维的边数组。**关键技巧**：每维额外预留 2 个「离群箱子」——一个在最左捕获小于下界的点，一个在最右捕获大于上界的点。
3. **归箱 `_bin_numbers`**：用 `np.digitize` 把每个点映射到一个箱子编号，并对「正好落在最右边界」的点做修正（归入最后一箱而非离群）。
4. **聚合**：按 `statistic` 类型用 `np.bincount`（极速计数/求和）或排序（median/min/max）在每个箱子里算结果。
5. **裁剪与整形**：切掉两个离群箱子，按 `values` 原形状还原。

**半开区间约定**：除最右一箱外，每个箱子是**左闭右开** `[edge[i], edge[i+1])`；最右一箱是**双闭** `[edge[-2], edge[-1]]`（包含右端点）。这是 `np.digitize` 的标准行为。

#### 4.3.3 源码精读

**1D 包装函数**：

[_binned_statistic.py:14-15](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_binned_statistic.py#L14-L15) —— 结果命名元组 `BinnedStatisticResult(statistic, bin_edges, binnumber)`，与 u2-l1 的 `DescribeResult` 同属「命名元组结果」模式。

[_binned_statistic.py:187-190](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_binned_statistic.py#L187-L190) —— `binned_statistic` 几乎只做参数归一化，然后调 `binned_statistic_dd([x], values, ...)`，把 1D 当成「Ndim=1 的 N 维」处理。这就是「1D/2D/DD 都委托给 dd」的体现。

**真正的引擎 `binned_statistic_dd`**：

[_binned_statistic.py:542-544](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_binned_statistic.py#L542-L544) —— `known_stats` 白名单校验，非法统计量直接报错。

[_binned_statistic.py:554-558](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_binned_statistic.py#L554-L558) —— 样本含 NaN 直接抛错并提示「语义歧义」。注意这与 `quantile` 的 `nan_policy` 不同：分箱场景下 NaN 没有合理的默认处理。

[_binned_statistic.py:591-593](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_binned_statistic.py#L591-L593) —— 主路径：`_bin_edges` 建边 + `_bin_numbers` 归箱。这两步决定了「每个点进哪个箱」。

**建边（含离群箱技巧）**：

[_binned_statistic.py:763-766](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_binned_statistic.py#L763-L766) —— 当 `bins[i]` 是个数时，`nbin[i] = bins[i] + 2`（+2 就是左右各加一个离群箱），再用 `np.linspace(smin, smax, nbin[i]-1)` 生成等宽边。这就是为什么内部箱数比用户要的多 2。

**归箱**：

[_binned_statistic.py:782-785](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_binned_statistic.py#L782-L785) —— 用 `np.digitize` 把每维样本映射到箱子，再用 `np.ravel_multi_index` 把多维箱号压成一维线性编号，供后续 `bincount` 使用。

[_binned_statistic.py:796-801](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_binned_statistic.py#L796-L801) —— **最右边界修正**：由于 `digitize` 会把「等于最右边界的点」当成离群，这里专门把它左移一箱，符合「最右一箱双闭」的约定。

**聚合（以 mean 为例）**：

[_binned_statistic.py:369-377](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_binned_statistic.py#L369-L377) —— `_bincount` 用 `np.bincount` 极速计数/加权求和，还兼容复数（实部、虚部分别 bincount）。

[_binned_statistic.py:605-611](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_binned_statistic.py#L605-L611) —— `mean` 的实现：先 `flatcount`（每箱点数），再 `flatsum`（每箱加权和），最后相除。空箱填 NaN。这就是「每箱均值」的全部代码——非常紧凑。

#### 4.3.4 代码实践

**目标**：用 `binned_statistic` 对配对数据分箱求均值，并理解 `binnumber` 的含义。

**操作步骤**：

```python
# 示例代码
import numpy as np
from scipy import stats

x = [1, 1, 2, 5, 7]                       # 用来分箱的自变量
values = [1.0, 1.0, 2.0, 1.5, 3.0]        # 每个点上要聚合的值
res = stats.binned_statistic(x, values, statistic='mean', bins=2)
print("statistic:", res.statistic)
print("bin_edges:", res.bin_edges)
print("binnumber:", res.binnumber)
```

**需要观察的现象**：默认 `range = (min(x), max(x)) = (1, 7)`，`bins=2` 给出两条等宽边 `[1, 4, 7]`。`binnumber` 告诉你每个原始点进了第几箱。

**预期结果**：
- `bin_edges` = **[1., 4., 7.]**（`linspace(1,7,3)`）。
- 箱1 `[1,4)`：包含 x=1,1,2 三个点 → 对应 values 1.0,1.0,2.0，均值 = 4.0/3 ≈ **1.333**。
- 箱2 `[4,7]`：包含 x=5,7 两个点 → values 1.5,3.0，均值 = 4.5/2 = **2.25**。
- `statistic` = **[1.333..., 2.25]**。
- `binnumber` = **[1, 1, 1, 2, 2]**（前三个点进箱1，后两个进箱2）。

> 这与官方文档 `binned_statistic(..., 'sum', bins=2)` 给出 `statistic=[4., 4.5]`（sum）一致；本练习只是把 `'sum'` 换成 `'mean'`。

#### 4.3.5 小练习与答案

**练习 1**：把上面例子改成 `statistic='count'`，结果是什么？

答案：`count` 不看 `values`，只数每箱点数。箱1 有 3 个点、箱2 有 2 个点 → `[3., 2.]`。见 [`_binned_statistic.py:624-629`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_binned_statistic.py#L624-L629)，它直接用 `flatcount` 填充。

**练习 2**：如果某个箱子是空的，`statistic='mean'` 会返回什么？

答案：返回 `NaN`。因为 [`_binned_statistic.py:606`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_binned_statistic.py#L606) 先 `result.fill(np.nan)`，只有 `flatcount.nonzero()`（非空箱）的位置才被覆盖。

**练习 3**：为什么 `binned_statistic` 对含 NaN 的 `x` 直接报错，而 `quantile` 却有 `nan_policy`？

答案：分箱时 NaN 无法映射到任何数值区间（不能 `digitize`），且「忽略它是否改变其他点的箱号归属」语义不清，故 [`_binned_statistic.py:554-558`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_binned_statistic.py#L554-L558) 直接抛错让用户先清洗数据；`quantile` 走的是不同的新式校验管线 `_quantile_iv`，内置了 propagate/omit/raise 三策略。

---

## 5. 综合实践

**任务**：把本讲三个工具串起来，对一组模拟数据做一次完整的「频率统计分析」。

```python
# 示例代码
import numpy as np
from scipy import stats

rng = np.random.default_rng(42)
data = rng.standard_normal(500)          # 500 个标准正态样本

# 1) 用 quantile 取四分位数与极端分位
qs = stats.quantile(data, [0.25, 0.5, 0.75], method='linear')
print("线性四分位:", qs)
qs_hd = stats.quantile(data, [0.25, 0.5, 0.75], method='harrell-davis')
print("harrell-davis 四分位:", qs_hd)

# 2) 用 estimated_cdf 估计经验分布，验证它在中位数处≈0.5
med = qs[1]
print("estimated_cdf(中位数):", stats.estimated_cdf(data, med, axis=-1))

# 3) 用 binned_statistic 把数据分成 10 箱，看每箱均值与计数
res = stats.binned_statistic(data, data, statistic='mean', bins=10)
print("每箱均值:", np.round(res.statistic, 3))
print("每箱计数:", stats.binned_statistic(data, data, statistic='count', bins=10).statistic)
```

**需要观察与思考**：

1. `linear` 与 `harrell-davis` 的四分位数应**非常接近**（大样本下不同方法趋同），但对小样本会有可见差异——这正是 4.1.4 的结论。
2. `estimated_cdf(data, 中位数)` 应**接近 0.5**（不一定精确等于 0.5，取决于中位数附近是否有重复值与插值）。
3. 标准正态数据在 0 附近（中箱）计数应最多、均值应接近 0；两端箱计数少。

**预期结果**：由于使用了固定种子 `default_rng(42)`，结果是可复现的，但具体数值**待本地验证**。定性结论（方法趋同、中位数处 CDF≈0.5、中箱最密）应稳定成立。

---

## 6. 本讲小结

- `quantile` 估计**经验分位函数** \(\hat{Q}(p)\)，核心是「在排序样本的两点之间按 method 约定插值」，默认 `linear` 用 \(p(n-1)\) 定位。
- 多种 `method`（9 种 H&F + `harrell-davis` + `round_*`）本质是改一个偏移常数 \(m\) 或换一套加权方案；同一 p 会得到不同结果。
- `estimated_cdf` 估计**经验 CDF** \(\hat{F}(y)\)，是 `quantile` 的对偶/反函数，两者**共用 `_quantile_iv` 校验管线与 `_post_quantile` 整形**。
- 与 u2-l1/u2-l2 不同，`quantile`/`estimated_cdf` **没有**用 `@_axis_nan_policy_factory`，而是自带 `_quantile_iv`——这是较新函数的设计选择，为 u5-l1 埋下伏笔。
- `binned_statistic` 是直方图的推广，1D/2D/DD **都委托给 `binned_statistic_dd`**；核心三步是 `_bin_edges`（建边，含 +2 离群箱技巧）、`_bin_numbers`（`digitize` 归箱）、按 `statistic` 用 `bincount` 聚合。
- 分箱对 NaN「零容忍」直接报错，与 `quantile` 的 `nan_policy` 形成对比——反映了两个工具对缺失值的不同立场。

## 7. 下一步学习建议

- **下一讲 u3-l1** 将进入「概率分布对象」体系（`rv_continuous`/`rv_discrete`）。`quantile` 估计的是**经验**分位，而分布对象上的 `ppf` 方法给出**理论**分位——对比两者能加深对「样本 vs 模型」的理解。
- 想深入 `quantile` 的高级用法，可读函数文档里的 `:ref:`outliers`` 主题（用 `round_outward`/`round_inward` 做缩尾与修剪）。
- 想理解 `axis`/`nan_policy` 为何在其他函数里走共享装饰器，请直接进入 **u5-l1**（`_axis_nan_policy_factory`），届时你会清楚 `quantile` 为何选择「另起炉灶」。
- 对 `binned_statistic` 感兴趣的读者，可继续阅读 [`_binned_statistic.py`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_binned_statistic.py) 中 `median`/`min`/`max` 分支（它们用排序而非 `bincount`），并对照 `numpy.histogram`/`numpy.digitize` 的行为。
