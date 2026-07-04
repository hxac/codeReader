# 描述性统计：describe 与各种平均

## 1. 本讲目标

学完本讲后，你应该能够：

- 用 `describe` 一行代码拿到一组数据的「多指标摘要」（观测数、最值、均值、方差、偏度、峰度）。
- 区分**算术平均**、**几何平均**、**调和平均**、**幂平均（广义平均）** 的数学定义与适用场景，知道什么时候该用哪一个。
- 看懂 `gmean` / `hmean` / `pmean` 在源码层面如何被统一成「先变换数据、再做加权算术平均、再变换回来」的同一种套路。
- 理解 `axis` 参数如何控制「沿哪条轴做统计」，以及 `weights`（权重）如何改变平均的结果。

本讲是描述性统计的入口，承接 [u1-l3](u1-l3-import-and-run.md) 学会的「导入与首次调用」，为 [u2-l2](u2-l2-moments-skew-kurtosis.md) 讲偏度/峰度的精确定义打下基础。

## 2. 前置知识

### 2.1 什么是「平均」

日常说的「平均」通常指**算术平均**：把所有数加起来再除以个数。但在不同场景里，「平均」有不同含义：

- 想算**增长率**的平均（比如年收益率），该用**几何平均**——因为收益是连乘累积的。
- 想算**速率**的平均（比如往返平均速度），该用**调和平均**——因为时间是按距离/速度累加的。
- 想算一般化的「广义平均」，该用**幂平均**——它把上面三种都统一进一个公式，参数 \(p\) 取不同值就退化成不同平均。

### 2.2 axis（轴）是什么

NumPy 数组是多维的。一个二维数组有两条轴：`axis=0` 是「沿列向下」，`axis=1` 是「沿行向右」。统计函数里的 `axis` 参数告诉函数「沿哪条轴压缩、求平均」。

```
数据（2行3列）          axis=0 求平均（沿列，得3个数）   axis=1 求平均（沿行，得2个数）
[[1, 2, 3],      ──►    [ (1+4)/2, (2+5)/2, (3+6)/2 ]   [ (1+2+3)/3 ]
 [4, 5, 6]]                                              [ (4+5+6)/3 ]
```

若 `axis=None`，则把整个数组摊平成一维，对所有元素一起算。

### 2.3 幂平均不等式（直觉）

对同一组**正数**，幂平均 \(M_p\) 关于指数 \(p\) 单调递增：

\[ M_{-1} \le M_0 \le M_1 \le M_2 \quad\Longleftrightarrow\quad \text{调和} \le \text{几何} \le \text{算术} \le \text{平方平均} \]

等号成立当且仅当所有数据相等。本讲的实践任务就是要亲手验证这条不等式。

## 3. 本讲源码地图

本讲的全部内容集中在两个文件里：

| 文件 | 作用 |
|------|------|
| [`_stats_py.py`](_stats_py.py) | `scipy.stats` 最大的纯 Python 实现文件。`gmean`、`hmean`、`pmean`、`describe` 以及 `_xp_mean`、`_var`、`DescribeResult` 都定义在这里。 |
| [`_common.py`](_common.py) | 极小的共享定义文件，只放公共的 `namedtuple`（如 `ConfidenceInterval`）。本讲提到它是为了说明 `DescribeResult` 这种「结果命名元组」是 stats 全模块共享的一种风格。 |

一个关键事实：这四个函数并非各自为政。它们都依赖同一个**加权算术平均**内部函数 `_xp_mean`，并共享 `_axis_nan_policy_factory` 装饰器来统一处理 `axis`/`nan_policy`。理解了这条「公共底座」，四个函数就一起通了。

## 4. 核心概念与源码讲解

### 4.1 平均家族与公共底座：_xp_mean

#### 4.1.1 概念说明

先抛出一个能贯穿全讲的核心洞察：**几何平均、调和平均、幂平均，本质上都是「加权算术平均」套了一层变换**。

定义加权算术平均为：

\[ \bar{x}_w = \frac{\sum_{i} w_i x_i}{\sum_{i} w_i} \]

其中 \(w_i\) 是权重（不指定时默认全为 1，即退化为普通算术平均）。那么：

- **几何平均**：先对数据取对数，求算术平均，再取指数——\( \exp\big(\bar{x}_w \;\big|\; x=\ln a\big) \)。
- **调和平均**：先取倒数，求算术平均，再取倒数——\( 1 \big/ \bar{x}_w \;\big|\; x=1/a \)。
- **幂平均**：先做 \(p\) 次方，求算术平均，再开 \(p\) 次方——\( \big(\bar{x}_w \;\big|\; x=a^p\big)^{1/p} \)。

也就是说，`scipy.stats` 没有为这三种平均各写一套加权逻辑，而是把「加权算术平均」抽成一个内部函数 `_xp_mean`，三种平均都复用它。这就是本讲的源码主线。

#### 4.1.2 核心流程

```
任意"特殊平均" =  transform_fwd(数据)  ──►  _xp_mean(加权算术平均)  ──►  transform_bwd(结果)
```

- `gmean`：`transform_fwd = log`，`transform_bwd = exp`
- `hmean`：`transform_fwd = 1/x`，`transform_bwd = 1/x`
- `pmean`：`transform_fwd = x**p`，`transform_bwd = **(1/p)`

`_xp_mean` 本身负责处理 `axis`、`weights`、`nan_policy`、`keepdims` 这些通用细节。

#### 4.1.3 源码精读

`_xp_mean` 的签名揭示了它是一个功能完整的加权平均内核：

[_stats_py.py:10993-10994](_stats_py.py) — `_xp_mean(x, /, *, axis=None, weights=None, keepdims=False, nan_policy='propagate', dtype=None, warn=True, xp=None)`，它是「带权重、带轴、带 NaN 策略、带数组库抽象」的算术平均，公式为：

\[ \bar{x}_w = \frac{\sum_{i=0}^{n-1} w_i x_i}{\sum_{i=0}^{n-1} w_i} \]

（见 [_stats_py.py:11043-11046](_stats_py.py) 的 docstring 公式，权重全为 1 时退化为 \((\sum_i x_i)/n\)。）

这四个公开函数还共享一个装饰器 `_axis_nan_policy_factory`，它在 [_stats_py.py:59](_stats_py.py) 从 `._axis_nan_policy` 导入，作用是统一注入 `axis`/`nan_policy` 处理逻辑（详见 [u5-l1](u5-l1-axis-nan-decorator.md)）。本讲只需要知道：被它装饰后，函数能自动支持多维数组的逐轴统计和 NaN 处理。

> 小贴士：`xp`（array namespace）是 SciPy 1.14+ 引入的「数组库抽象」。`xp.mean` 可能是 `numpy.mean`，也可能是 CuPy/PyTorch 的对应函数。初学阶段把它当成 `numpy` 即可。

#### 4.1.4 代码实践

**实践目标**：亲手验证「加权算术平均 + 变换」的统一套路，先从 `_xp_mean` 的等权重情形入手。

**操作步骤**：

1. 写一段脚本（**示例代码**，非项目原有代码）：

```python
import numpy as np
from scipy.stats import gmean, hmean, pmean

a = np.array([1.0, 2.0, 3.0, 4.0, 5.0])

# 用「变换 + np.average + 反变换」手工复刻三种平均
arith = np.average(a)                      # 算术平均，作为基准
geo_manual = np.exp(np.average(np.log(a)))  # 手工几何平均
harm_manual = 1.0 / np.average(1.0 / a)     # 手工调和平均
power_manual = np.average(a ** 2) ** 0.5    # 手工幂平均 p=2

# 与 scipy 官方实现比对
print("gmean:", gmean(a), "手动:", geo_manual)
print("hmean:", hmean(a), "手动:", harm_manual)
print("pmean(p=2):", pmean(a, 2), "手动:", power_manual)
print("算术平均:", arith)
```

**需要观察的现象**：每组「scipy 实现」与「手工复刻」应当完全相等。

**预期结果**：

- `gmean ≈ 2.6052`，`hmean ≈ 2.1898`，`pmean(p=2) ≈ 3.3166`，算术平均 `= 3.0`。
- 大小关系满足 \( M_{-1} < M_0 < M_1 < M_2 \)，即 `hmean < gmean < 算术 < pmean(p=2)`，印证 2.3 节的幂平均不等式。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `gmean` 对负数会得到 NaN？

**参考答案**：几何平均内部对数据取 `log`，而实数自然对数仅对非负数有定义，负数取 `log` 会得到 NaN，再 `exp` 回来仍是 NaN。源码里 `gmean` 用 `np.errstate(divide='ignore')` 屏蔽了除零警告（见 4.2.3）。

**练习 2**：若把权重设为 `[0, 0, 0, 0, 1]`，`gmean(a, weights=...)` 会得到什么？

**参考答案**：权重全部集中在第 5 个元素 `5.0` 上，加权几何平均就是 \(5.0\) 本身。

---

### 4.2 gmean：几何平均

#### 4.2.1 概念说明

几何平均衡量「乘性增长」的平均，最经典的用途是算**多期收益率的平均**。若某投资三年收益率分别为 \(r_1,r_2,r_3\)，总财富是连乘得到的 \(\prod(1+r_i)\)，那么「等价的年平均收益率」应该用几何平均，而不是算术平均。

加权几何平均定义为：

\[ \text{gmean}(a,w) = \exp\!\left( \frac{\sum_i w_i \ln a_i}{\sum_i w_i} \right) \]

等权重时退化为大家熟悉的形式：

\[ \text{gmean}(a) = \sqrt[n]{\,a_1 a_2 \cdots a_n\,} \]

#### 4.2.2 核心流程

```
gmean(a, axis, weights):
  1. 推断浮点 dtype（保证整数输入也用浮点计算）
  2. 把 a、weights 转成数组
  3. log_a = log(a)            # 变换：取对数（屏蔽除零警告）
  4. return exp( _xp_mean(log_a, axis, weights) )   # 加权算术平均 + 反变换
```

#### 4.2.3 源码精读

`gmean` 的装饰器声明与定义在 [_stats_py.py:150-154](_stats_py.py#L150-L154)：`@_axis_nan_policy_factory(...)` 装饰器带 `kwd_samples=['weights']`，告诉装饰器「`weights` 是与 `a` 配对的样本」，从而能正确处理它们的轴对齐。

函数体只有寥寥几行，核心是 [_stats_py.py:231-234](_stats_py.py#L231-L234)：

```python
with np.errstate(divide='ignore'):
    log_a = xp.log(a)
return xp.exp(_xp_mean(log_a, axis=axis, weights=weights))
```

- `np.errstate(divide='ignore')`：当 `a` 含 0 时 `log(0)=-inf` 会触发除零警告，这里主动屏蔽，让结果自然变成 0（因为 \(e^{-\infty}=0\)）。
- `xp.exp(_xp_mean(...))`：正是 4.1 节描述的「log → 加权平均 → exp」三步。

#### 4.2.4 代码实践

**实践目标**：体会几何平均在「乘性增长」场景下比算术平均更合理。

**操作步骤**：

1. 运行（**示例代码**）：

```python
import numpy as np
from scipy.stats import gmean

# 一只股票三年净值变化倍数：1.5, 0.5, 2.0
returns = np.array([1.5, 0.5, 2.0])

print("算术平均倍数:", returns.mean())     # (1.5+0.5+2.0)/3
print("几何平均倍数:", gmean(returns))     # (1.5*0.5*2.0)**(1/3)
print("三年总倍数:", returns.prod())       # 1.5*0.5*2.0
```

**需要观察的现象**：算术平均会高估真实「等价年倍数」。

**预期结果**：三年总倍数 \(=1.5\)（三年后净值为初始的 1.5 倍）。几何平均 \(\approx 1.1447\)，且 \(1.1447^3 \approx 1.5\)，正好复现总倍数；而算术平均 \(\approx 1.333\)，\(1.333^3 \approx 2.37 \neq 1.5\)，明显高估。这正说明对连乘量该用几何平均。

#### 4.2.5 小练习与答案

**练习 1**：`gmean([1, 4])` 等于多少？为什么？

**参考答案**：等于 \(2.0\)。因为 \(\sqrt{1\times4}=2\)。这也是 docstring 里给出的第一个例子（[_stats_py.py:215-216](_stats_py.py#L215-L216)）。

**练习 2**：`gmean` 的 `axis` 默认值是多少？对一个 2×3 的二维数组用默认 `axis` 会得到几个数？

**参考答案**：默认 `axis=0`（沿列），对一个 2×3 数组会得到 3 个数（每列一个几何平均）。

---

### 4.3 hmean：调和平均

#### 4.3.1 概念说明

调和平均对「按倒数有意义」的量最合适，典型例子是**平均速度**：以速度 \(v_1\) 走一段路程、以 \(v_2\) 走同样路程，往返平均速度不是 \((v_1+v_2)/2\)，而是调和平均。

加权调和平均：

\[ \text{hmean}(a,w) = \frac{\sum_i w_i}{\sum_i w_i / a_i} \]

等权重时：

\[ \text{hmean}(a) = \frac{n}{\sum_i 1/a_i} \]

#### 4.3.2 核心流程

```
hmean(a, axis, weights):
  1. 推断 dtype、转数组
  2. 若 a 含负数：把负数置为 NaN 并发 RuntimeWarning（调和平均只对非负数定义）
  3. return 1.0 / _xp_mean(1.0/a, axis, weights)   # 倒数 → 加权平均 → 再取倒数
```

注意第 3 步恰好是「调和平均 = 倒数的算术平均的倒数」。

#### 4.3.3 源码精读

负数检测与警告在 [_stats_py.py:324-330](_stats_py.py#L324-L330)：

```python
negative_mask = a < 0
a = xp.where(negative_mask, xp.nan, a)
if not is_lazy_array(negative_mask) and xp.any(negative_mask):
    message = ("The harmonic mean is only defined if all elements are "
               "non-negative; otherwise, the result is NaN.")
    warnings.warn(message, RuntimeWarning, stacklevel=2)
```

返回值在 [_stats_py.py:332-333](_stats_py.py#L332-L333)：

```python
with np.errstate(divide='ignore'):
    return 1.0 / _xp_mean(1.0 / a, axis=axis, weights=weights)
```

这里 `1.0/a` 对 0 会得 `inf`，`_xp_mean` 后仍为 `inf`，再取倒数得 0——即「数据里含 0 时调和平均为 0」，与数学定义一致；`errstate` 同样是为了屏蔽这个过程的警告。

#### 4.3.4 代码实践

**实践目标**：用调和平均正确计算「等路程往返平均速度」。

**操作步骤**：

1. 运行（**示例代码**）：

```python
from scipy.stats import hmean

# 去程 60 km/h，回程 120 km/h，路程相同
v = [60, 120]
print("算术平均速度:", sum(v)/len(v))
print("调和平均速度:", hmean(v))
```

**需要观察的现象**：算术平均给出 90，但真实平均速度低于 90。

**预期结果**：调和平均 \(= 2/(1/60+1/120) = 2/(3/120) = 80\)。原因：慢速段（60）花费的时间更多，拉低了平均速度，所以真实平均是 80 而非 90。这印证「等路程平均速度用调和平均」。

#### 4.3.5 小练习与答案

**练习 1**：`hmean([1, 4])` 等于多少？

**参考答案**：\(= 2/(1/1 + 1/4) = 2/(1.25) = 1.6\)。与 docstring 例子（[_stats_py.py:308-309](_stats_py.py#L308-L309)）一致。

**练习 2**：对同一组正数，`hmean` 与 `gmean` 哪个更小？

**参考答案**：`hmean` 更小。因为调和平均对应幂平均 \(p=-1\)，几何平均对应 \(p=0\)，而幂平均关于 \(p\) 单调递增，故 \(M_{-1} \le M_0\)。

---

### 4.4 pmean：幂平均（广义平均）

#### 4.4.1 概念说明

幂平均（又称广义平均、Hölder 平均）用一个参数 \(p\) 把所有平均统一进一个公式：

\[ M_p(a,w) = \left( \frac{\sum_i w_i a_i^p}{\sum_i w_i} \right)^{1/p} \]

不同 \(p\) 对应不同平均：

| \(p\) | 对应的平均 |
|-------|-----------|
| \(-\infty\) | 最小值 |
| \(-1\) | 调和平均 |
| \(0\) | 几何平均（极限定义） |
| \(1\) | 算术平均 |
| \(2\) | 平方平均（RMS） |
| \(+\infty\) | 最大值 |

因此 `pmean` 是「平均之母」：调一下 \(p\)，就能在调和、几何、算术、平方之间连续滑动。

#### 4.4.2 核心流程

```
pmean(a, p, axis, weights):
  1. 校验 p 必须是 int/float 且有限
  2. 若 p == 0：直接 return gmean(...)          # 极限情形交给 gmean
  3. 推断 dtype、转数组；负数置 NaN 并警告
  4. 若 |p| 很小（≤2e-6）：用 _linearized_pmean 线性近似，避免数值病态
  5. return _xp_mean(a**p, axis, weights) ** (1/p)
```

第 2 步和第 4 步是 `pmean` 区别于 `gmean`/`hmean` 的两个「特例处理」，体现实现者对数值稳定性的考量。

#### 4.4.3 源码精读

`p==0` 的特例在 [_stats_py.py:440-441](_stats_py.py#L440-L441)：

```python
if p == 0:
    return gmean(a, axis=axis, dtype=dtype, weights=weights)
```

这是因为 \(M_0\) 在公式里是 \(0/0\) 型未定式，数学上需用极限定义才等于几何平均，所以源码直接复用 `gmean`。

对很小的 \(p\)，直接算 \(a^p\) 和开 \(p\) 次方会严重丢精度，于是 [_stats_py.py:462-468](_stats_py.py#L462-L468) 做了保护：

```python
# Linearized approximation for small p to avoid numerical issues; see gh-23407
p_threshold = 2e-6
if abs(p) <= p_threshold:
    return _linearized_pmean(a, p, axis=axis, weights=weights, xp=xp)

with np.errstate(divide='ignore', invalid='ignore'):
    return _xp_mean(a**float(p), axis=axis, weights=weights)**(1/p)
```

注释里提到的 `gh-23407` 是修复该数值问题的 GitHub issue，这也是「读源码能学到工程细节」的好例子。

#### 4.4.4 代码实践

**实践目标**：用 `pmean` 连续滑动 \(p\)，观察平均值如何从调和平均逐步上升到平方平均。

**操作步骤**：

1. 运行（**示例代码**）：

```python
import numpy as np
from scipy.stats import pmean, hmean, gmean

a = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
for p in [-1, 0, 1, 2]:
    print(f"p={p:+d}  M_p = {pmean(a, p):.4f}")

# 验证特例：p=-1 应等于 hmean，p=0 应等于 gmean
print("hmean:", hmean(a), " pmean(p=-1):", pmean(a, -1))
print("gmean:", gmean(a), " pmean(p=0): ", pmean(a, 0))
```

**需要观察的现象**：随着 \(p\) 从 \(-1\) 升到 \(2\)，\(M_p\) 单调上升；两个特例与 `hmean`/`gmean` 完全相等。

**预期结果**：\(M_{-1}\approx2.1898\)、\(M_0\approx2.6052\)、\(M_1=3.0\)、\(M_2\approx3.3166\)，严格递增；`pmean(a,-1)==hmean(a)`，`pmean(a,0)==gmean(a)`。（这两组等式也正是 docstring 里专门演示的内容，见 [_stats_py.py:422-434](_stats_py.py#L422-L434)。）

#### 4.4.5 小练习与答案

**练习 1**：调用 `pmean(a, float('inf'))` 会发生什么？

**参考答案**：会抛 `NotImplementedError`，提示 "Power mean only implemented for finite `p`"。源码在 [_stats_py.py:442-444](_stats_py.py#L442-L444) 显式拒绝了无穷大 \(p\)（虽然数学上 \(M_{+\infty}=\max\)，但官方实现尚未做此特例）。

**练习 2**：为什么 `pmean` 要对极小的 \(p\) 用线性近似而不是直接算？

**参考答案**：当 \(p\to0\) 时，\(a^p\to1\) 且开 \(p\) 次方接近取 \(0/0\)，浮点运算会丢失大量有效数字。线性近似（基于 \(a^p = e^{p\ln a}\approx 1+p\ln a\)）能在 \(p\) 很小时给出稳定结果，对应 issue `gh-23407`。

---

### 4.5 describe：一次性描述性摘要

#### 4.5.1 概念说明

前面三个「平均」只算一个数，而 `describe` 一次性吐出一组数据的**多维度摘要**：观测数、最值、均值、方差、偏度、峰度。它是探索性数据分析（EDA）的瑞士军刀——拿到一份数据，先 `describe` 一下，就能快速知道它的规模、范围、中心、离散度和形状。

`describe` 把结果打包成一个命名元组 `DescribeResult`，字段含义如下：

| 字段 | 含义 |
|------|------|
| `nobs` | 观测数（沿 axis 的样本个数） |
| `minmax` | 元组 `(最小值, 最大值)` |
| `mean` | 算术平均 |
| `variance` | 无偏方差（默认 `ddof=1`，分母为 \(n-1\)） |
| `skewness` | 偏度（分布不对称程度） |
| `kurtosis` | 峰度（Fisher 定义，正态分布为 0） |

#### 4.5.2 核心流程

```
describe(a, axis, ddof=1, bias=True, nan_policy='propagate'):
  1. _chk_asarray：把 a 转数组，axis=None 时摊平
  2. _contains_nan：按 nan_policy 处理 NaN
     - 若 nan_policy='omit' 且含 NaN：转给掩码版本 mstats_basic.describe
  3. 若数组为空：raise ValueError
  4. 逐项计算：
       n  = _count_nonmasked(...)         # 观测数
       mm = (min(a), max(a))               # 最值
       m  = mean(a)                        # 算术平均
       v  = _var(a, ddof=ddof)             # 方差（默认无偏）
       sk = skew(a, bias=bias)             # 偏度
       kurt = kurtosis(a, bias=bias)       # 峰度
  5. return DescribeResult(n, mm, m, v, sk, kurt)
```

#### 4.5.3 源码精读

结果类型 `DescribeResult` 是一个 `namedtuple`，定义在 [_stats_py.py:1441-1443](_stats_py.py#L1441-L1443)：

```python
DescribeResult = namedtuple('DescribeResult',
                            ('nobs', 'minmax', 'mean', 'variance', 'skewness',
                             'kurtosis'))
```

> 这是 stats 模块的一种通用风格——把多值返回包成命名元组，既支持 `r.mean` 属性访问，又支持 `nobs, minmax, mean, ... = r` 解包。（参见 [u1-l3](u1-l3-import-and-run.md) 对 `DescribeResult` 的初步介绍，以及 [_common.py](_common.py) 里的 `ConfidenceInterval` 也是同样套路。）

`describe` 函数体的核心计算在 [_stats_py.py:1533-1542](_stats_py.py#L1533-L1542)：

```python
n = xp.asarray(_count_nonmasked(a, axis, xp=xp), dtype=xp.int64, device=xp_device(a))
n = n[()] if n.ndim == 0 else n
mm = (xp.min(a, axis=axis), xp.max(a, axis=axis))
a = xp_promote(a, force_floating=True, xp=xp)
m = xp.mean(a, axis=axis)
v = _var(a, axis=axis, ddof=ddof, xp=xp)
v = v[()] if v.ndim == 0 else v
sk = skew(a, axis, bias=bias)
kurt = kurtosis(a, axis, bias=bias)
```

要点：

- `n[()] if n.ndim == 0 else n`：把 0 维数组「降」成 Python 标量，让 `nobs` 在一维输入时返回普通整数而非 0 维数组。
- `_var` 是 stats 内部的方差计算，定义在 [_stats_py.py:1220-1228](_stats_py.py#L1220-L1228)：它先用 `_moment(x, 2, ...)` 算二阶中心矩，再用 \(n/(n-\text{ddof})\) 做无偏校正——这就是 `ddof=1` 时分母为 \(n-1\) 的由来。
- `skew` / `kurtosis` 复用同模块的函数，它们的精确定义留到 [u2-l2](u2-l2-moments-skew-kurtosis.md) 讲。

`axis=None` 时，[_stats_py.py:1517-1518](_stats_py.py#L1517-L1518) 的 `_chk_asarray` 会把数组摊平成一维，所以统计对全体元素进行。

#### 4.5.4 代码实践

**实践目标**：用 `describe` 快速摘要一组数据，并解读每个字段；再体会 `axis` 对多维数组的影响。

**操作步骤**：

1. 运行（基于 docstring 示例，**示例代码**）：

```python
import numpy as np
from scipy import stats

a = np.arange(10)          # 0,1,...,9
r = stats.describe(a)
print(r)
print("字段单独访问：", r.nobs, r.mean, r.variance)

# 二维数据，观察 axis 默认行为
b = [[1, 2], [3, 4]]
print(stats.describe(np.array(b)))
```

**需要观察的现象**：

- 一维输入得到标量结果；二维输入得到**数组**形式的最值/均值/方差（沿 `axis=0` 即每列一组统计量）。
- 偏度为 0、峰度为负。

**预期结果**：

- 一维：`DescribeResult(nobs=10, minmax=(0, 9), mean=4.5, variance=9.1667, skewness=0.0, kurtosis=-1.2242)`。偏度为 0 是因为 0..9 关于 4.5 对称；峰度为负说明该均匀分布比正态分布更「平」。
- 二维 `[[1,2],[3,4]]`：`minmax=(array([1,2]), array([3,4]))`、`mean=array([2.,3.])`，即分别对两列做摘要。（以上数值与 docstring 给出的示例完全一致，见 [_stats_py.py:1506-1514](_stats_py.py#L1506-L1514)。）

#### 4.5.5 小练习与答案

**练习 1**：把 `ddof` 从默认的 1 改成 0，`variance` 会变________（变大/变小）？

**参考答案**：变大。`ddof=1` 时分母为 \(n-1\)（无偏样本方差），`ddof=0` 时分母为 \(n\)（有偏），分母变小则方差变大。源码校正项为 `var *= (n / (n-ddof))`（[_stats_py.py:1227](_stats_py.py#L1227)）。

**练习 2**：对一个**含有 NaN** 的一维数组调用 `stats.describe(a, nan_policy='omit')`，代码会走哪条分支？

**参考答案**：会走 [_stats_py.py:1524-1527](_stats_py.py#L1524-L1527) 的分支：先用 `ma.masked_invalid(a)` 把 NaN 屏蔽掉，再委托给掩码数组版本 `mstats_basic.describe(a, axis, ddof, bias)` 计算，从而忽略缺失值。

---

## 5. 综合实践

**任务**：拿到一份「假想的增长率数据」，用本讲全部四个函数完成一次小型探索性分析。

**数据**：五个项目的年度增长率（百分比，已转成倍数）：

```python
import numpy as np
from scipy import stats

growth = np.array([1.05, 1.12, 0.95, 1.20, 1.08])  # 5 个项目的一年净值倍数
```

**要求**：

1. 用 `stats.describe(growth)` 打印摘要，回答：均值是多少？这组倍数是否对称（看偏度符号）？波动大不大（看方差与 `minmax` 范围）？
2. 这组数据本质是「增长率/倍数」，是乘性量。请分别用 `gmean`、算术平均、`hmean` 计算平均倍数，比较三者大小，并解释为什么对「增长率」应该相信 `gmean`。
3. 用 `pmean(growth, p)` 让 \(p\) 取 `-1, 0, 1, 2`，验证它们正好等于第 2 步的调和、几何、算术、平方平均，且满足幂平均不等式。
4. （进阶）把数据 reshape 成 `(5, 1)` 或两列的二维数组，调用 `describe` 与 `gmean`，观察 `axis` 默认值带来的输出形状变化，体会 `axis` 的作用。

**预期产出**：一段脚本 + 一段文字结论。结论应指出：由于增长率是乘性量，几何平均才是「等价年增长率」的正确度量；而 `pmean` 通过滑动 \(p\) 给出了一族平均值，三者（调和/几何/算术）的大小关系严格满足 \(M_{-1}\le M_0\le M_1\)。

> 说明：本任务不需要你修改 SciPy 源码，全部在你的脚本里调用公开 API 即可。若手头没有 SciPy 运行环境，数值结果可标为「待本地验证」，但应能根据本讲的公式手算预估。

## 6. 本讲小结

- `gmean`、`hmean`、`pmean` 在源码层面共享同一个加权算术平均内核 `_xp_mean`，三者只是「变换→平均→反变换」的不同组合。
- `gmean = exp(mean(log a))`，适合乘性/增长率数据；含 0 或负数会产生 NaN。
- `hmean = 1 / mean(1/a)`，适合速率/单价等「按倒数有意义」的数据；只对非负数定义。
- `pmean(a, p)` 是广义平均，\(p=-1,0,1,2\) 分别给出调和、几何、算术、平方平均；`p==0` 直接转调 `gmean`，极小 `p` 用线性近似保证数值稳定。
- `describe` 一次返回 `DescribeResult`（命名元组），含 nobs/minmax/mean/variance/skewness/kurtosis；方差默认无偏（`ddof=1`），峰度为 Fisher 定义。
- `axis` 控制沿哪条轴压缩统计，`axis=None` 表示对全体元素；`weights` 让平均变成加权版本。

## 7. 下一步学习建议

- **下一步学 [u2-l2](u2-l2-moments-skew-kurtosis.md)**：本讲里 `describe` 用到了 `skew`/`kurtosis` 和 `_var`→`_moment`，下一讲会正式讲清**中心矩、偏度、峰度**的定义与 `bias` 校正，把这里的「黑盒」打开。
- **随后学 [u2-l3](u2-l3-quantile-and-binned-stats.md)**：从「平均」走向「分位数」，学习 `quantile`、`binned_statistic` 等频率统计工具。
- **进阶**：若想理解 `gmean`/`hmean`/`pmean` 上的 `@_axis_nan_policy_factory` 装饰器究竟如何自动注入 `axis`/`nan_policy`，可跳读 [u5-l1](u5-l1-axis-nan-decorator.md)。
- **源码阅读建议**：打开 [_stats_py.py:10993](_stats_py.py) 把 `_xp_mean` 完整读一遍，再回头看三个平均函数，你会对「公共底座 + 薄封装」的设计有更深体会。
