# 矩、偏度、峰度与众数

## 1. 本讲目标

上一讲（u2-l1）我们学会了用 `describe` 一次性拿到数据的「摘要卡片」，并提到它的 `variance` 字段背后其实调用了 `_moment`（二阶中心矩），`skewness`/`kurtosis` 字段也是「矩」算出来的。本讲就把「矩」这层抽象拆开，看清楚 `describe` 背后的零件。

学完本讲，你应当能够：

1. 说清楚什么是「中心矩」，并用 `moment` 计算任意阶的中心矩。
2. 理解偏度 `skew`、峰度 `kurtosis` 是如何由二、三、四阶中心矩组合出来的。
3. 区分 **Fisher 峰度**（正态分布为 0）与 **Pearson 峰度**（正态分布为 3），并理解 `bias=False` 的无偏校正公式。
4. 知道 `mode` 如何用排序 + 游程计数高效求众数。
5. 使用 `variation` 计算变异系数（标准差除以均值），并理解 `ddof` 的作用。

## 2. 前置知识

- **矩（moment）**：直觉上，矩是「数据相对某个中心点偏离程度的加权平均」。阶数 k 越高，越放大离群点的影响。一阶矩是均值，二阶中心矩是方差，三阶反映「不对称」，四阶反映「尖峰/厚尾」。
- **中心矩 vs 原点矩**：以均值为参考点算出来的叫中心矩（最常用），以 0 为参考点的叫原点矩。本讲的 `moment` 默认算中心矩，但可以用 `center=` 参数改参考点。
- **ddof（自由度修正）**：上一讲 `describe` 的方差用 `ddof=1`（除以 n−1，无偏），而本讲的 `moment` 默认 **不做** 自由度修正（除以 n）。这是两个函数最容易混淆的点。
- **bias（偏倚）**：用样本矩去估计总体矩时存在系统偏差，`bias=False` 会套用解析校正公式。这与 ddof 是两套独立的校正机制。

如果你还没读过 u2-l1，建议先看，因为本讲大量复用上一讲建立的 `_var`、`_moment`、ddof、bias 等概念。

## 3. 本讲源码地图

本讲涉及两个文件：

| 文件 | 作用 |
|------|------|
| [_stats_py.py](_stats_py.py) | 描述性统计的核心实现，本讲涉及 `moment`/`_moment`/`_var`/`skew`/`kurtosis`/`mode` |
| [_variation.py](_variation.py) | 变异系数 `variation` 的独立实现（体量小，单独成文件） |

一个贯穿全讲的观察：`moment`、`skew`、`kurtosis`、`mode`、`variation` 这五个函数 **全部** 被同一个装饰器 `@_axis_nan_policy_factory` 包裹。这个装饰器统一注入了 `axis`/`nan_policy`/`keepdims` 的处理逻辑（详见 u5-l1）。所以本讲我们只关注「统计公式本身」，把多维逐轴与 NaN 处理当作已经免费获得的能力。

## 4. 核心概念与源码讲解

### 4.1 中心矩与 moment 函数

#### 4.1.1 概念说明

把一批数据 \(x_1, x_2, \dots, x_n\) 围绕某个中心点 \(c\) 的偏离程度取 k 次方再求平均，就得到第 k 阶（样本）矩：

\[
m_k = \frac{1}{n}\sum_{i=1}^{n}(x_i - c)^k
\]

当 \(c = \bar{x}\)（样本均值）时，称为**中心矩**。注意分母是 \(n\)，**不做自由度修正**——这是 scipy 的 `moment` 的明确约定。

几个重要特例：

- \(k=1, c=\bar{x}\)：\(m_1 = 0\)（数据相对自身均值的偏差恒为 0）。
- \(k=2, c=\bar{x}\)：\(m_2\) 就是方差（但除以 n，不是 n−1）。
- \(k=3\)：用于偏度；\(k=4\)：用于峰度。

#### 4.1.2 核心流程

公共函数 `moment` 是一层薄薄的入口，真正的计算在内部函数 `_moment` 里：

```
moment(a, order, axis, center=None)
  ├── 校验 order 必须是整数
  ├── 若 order 是数组（多阶）→ reshape 后调用 _moment(axis=-1)
  └── 否则 → 调用 _moment，再 squeeze 标量

_moment(a, order, axis, center)
  ├── center 为 None → 取 xp.mean(a) 作为中心
  ├── _demean：a - center，并检测「灾难性抵消」
  ├── res = xp.mean((a - center) ** order)   ← 真正的公式
  └── 特例修正：order==0 → 1，order==1 且 center=None → 0
```

`_demean` 是一个值得注意的细节：当数据几乎完全相同（如 `[1e9, 1e9, 1e9+1e-3]`），从每个元素减去均值会发生**灾难性抵消**（catastrophic cancellation），精度丢失。`_demean` 会比较残差与均值的大小，一旦超阈值就抛出 `Precision loss occurred in moment calculation` 警告。

#### 4.1.3 源码精读

公共入口 `moment`，签名与文档说明分母是 n、不做自由度修正：

[_stats_py.py:L1083-L1164](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_stats_py.py#L1083-L1164)

文档注释里的核心定义（标注 `m_k`、`n`、`c`）位于 [L1124-L1133](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_stats_py.py#L1124-L1133)。

真正干活的 `_moment`：

[_stats_py.py:L1198-L1217](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_stats_py.py#L1198-L1217)

其中最关键的两行：先取均值当中心（`center is None` 时），再用 `xp.mean(a_zero_mean**order)` 算矩（[L1209-L1211](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_stats_py.py#L1209-L1211)）。

承上启下的 `_var`——上一讲 `describe` 的方差就来自这里，它先算二阶矩再乘 `n/(n-ddof)` 做自由度修正：

[_stats_py.py:L1220-L1228](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_stats_py.py#L1220-L1228)

可以看到 `var *= (n / (n-ddof))` 这一行正是 u2-l1 里 `describe` 方差 `ddof=1` 的来源——`moment` 本身除以 n，`_var` 再把它校正回除以 n−1。

#### 4.1.4 代码实践

1. **目标**：直观验证 `moment` 的中心矩定义与 `center` 参数。
2. **操作步骤**（待本地验证）：
   ```python
   from scipy.stats import moment
   x = [1, 2, 3, 4, 5]
   print(moment(x, order=1))   # 应为 0.0（一阶中心矩）
   print(moment(x, order=2))   # 应为 2.0（方差但除以 n）
   print(moment(x, order=2, center=0))  # 原点矩，不再是中心矩
   ```
3. **观察现象**：`order=1` 恒为 0；`order=2` 等于 2.0（注意 `np.var(x)` 也是 2.0，因为 NumPy 默认 ddof=0）。
4. **预期结果**：`0.0`、`2.0`、`11.0`（原点二阶矩 \(= (1+4+9+16+25)/5\)）。
5. **结论**：`center=0` 把「中心矩」退化成「原点矩」，这是理解矩定义最直接的方式。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `moment(x, order=1)` 永远等于 0？源码里是靠公式算出来的，还是靠特例分支？
**答案**：靠特例分支。[_moment](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_stats_py.py#L1212-L1215) 里 `order_1 = (order == 1) & (center is None)`，命中后直接 `xp.where(order_1, 0, res)`，避免浮点减法带来的微小残差。

**练习 2**：`moment` 的分母是 n 还是 n−1？这与 `describe` 的方差一致吗？
**答案**：`moment` 分母是 n（不做修正）。`describe` 的方差用 `_var(..., ddof=1)`，会额外乘 `n/(n−1)` 校正到 n−1。所以两者**不一致**——`moment(x,2)` 比 `describe(x).variance` 略小。

---

### 4.2 偏度 skew：用三阶矩衡量不对称性

#### 4.2.1 概念说明

偏度衡量分布的「不对称」。右尾更长/更重时偏度为正（右偏），左尾更长时为负（左偏），完全对称（如正态分布）偏度为 0。

scipy 用的是 **Fisher-Pearson 系数** \(g_1\)：

\[
g_1 = \frac{m_3}{m_2^{3/2}}
\]

其中 \(m_2, m_3\) 是有偏的样本中心矩（除以 n）。分母里的 \(m_2^{3/2}\) 起到「标准化」作用，让偏度变成无量纲数，可跨数据尺度比较。

#### 4.2.2 核心流程

```
skew(a, axis=0, bias=True, ...)
  ├── n = 样本数（非掩码）
  ├── mean = 均值；m2 = _moment(a, 2)；m3 = _moment(a, 3)
  ├── 零方差保护：若 m2 ≈ 0 → 返回 nan（数据全相等时无偏度可言）
  ├── 有偏值：vals = m3 / m2**1.5          ← g_1
  └── bias=False 时：nval = sqrt(n(n-1))/(n-2) * m3/m2**1.5   ← G_1
```

无偏校正后的估计量记作 \(G_1\)：

\[
G_1 = \frac{\sqrt{N(N-1)}}{N-2}\cdot\frac{m_3}{m_2^{3/2}}
\`

注意校正只在 `n > 2` 且方差非零时才施加（`can_correct = ~zero & (n > 2)`），否则保留原值。

#### 4.2.3 源码精读

`skew` 的声明与文档公式：[_stats_py.py:L1237-L1329](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_stats_py.py#L1237-L1329)

公式 \(g_1 = m_3/m_2^{3/2}\) 写在文档 [L1274-L1293](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_stats_py.py#L1274-L1293)。

核心实现，注意它直接复用 `_moment` 取 m2、m3：

[_stats_py.py:L1311-L1329](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_stats_py.py#L1311-L1329)

其中 `nval = ((n - 1.0) * n)**0.5 / (n - 2.0) * m3 / m2**1.5`（[L1326](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_stats_py.py#L1326)）正是无偏系数 \(G_1\) 的源码写照。

#### 4.2.4 代码实践（本讲主实践）

1. **目标**：验证正态分布样本的偏度与峰度都接近 0，并比较 `bias=True/False` 的差异。
2. **操作步骤**（待本地验证）：
   ```python
   import numpy as np
   from scipy.stats import skew, kurtosis, norm

   rng = np.random.default_rng(42)
   data = norm.rvs(size=100000, random_state=rng)

   print("skew bias=True :", skew(data, bias=True))
   print("skew bias=False:", skew(data, bias=False))
   ```
3. **观察现象**：两个值都应接近 0（理想正态偏度为 0）；`bias=False` 会略作校正。
4. **预期结果**：两者均在 ±0.02 量级，`bias=True` 与 `bias=False` 差异很小（n 很大时校正系数趋近 1）。
5. **结论**：大样本下偏度估计已很稳定，`bias` 校正主要在小样本时有用。

#### 4.2.5 小练习与答案

**练习**：右偏数据 `[1, 1, 1, 1, 100]` 的偏度应该是正还是负？为什么？
**答案**：正。极端大值 100 把右尾拉长，均值被它抬高，多数数据落在均值左侧，形成右偏（正偏度）。可用 `skew([1,1,1,1,100])` 验证为正数。

---

### 4.3 峰度 kurtosis：Fisher 与 Pearson 的两种约定

#### 4.3.1 概念说明

峰度用四阶矩衡量分布的「尖峰 + 厚尾」程度：

\[
g_2 = \frac{m_4}{m_2^{2}}
\]

对正态分布，这个比值理论上等于 **3**。为了让正态分布成为「零点」，scipy 默认用 **Fisher 定义**：减去 3：

\[
\text{kurtosis}_{\text{Fisher}} = \frac{m_4}{m_2^{2}} - 3 \quad\Rightarrow\quad \text{正态分布} = 0
\]

如果你想要「不减 3」的 **Pearson 定义**（正态分布 = 3），传 `fisher=False`。

| 设置 | 正态分布的值 | 含义 |
|------|------------|------|
| `fisher=True`（默认） | 0 | 超额峰度（excess kurtosis） |
| `fisher=False` | 3 | 原始峰度（Pearson） |

直觉：峰度 > 0（Fisher）表示比正态「更尖、尾更重」（如 Laplace 分布），峰度 < 0 表示更平坦（如均匀分布）。

#### 4.3.2 核心流程

```
kurtosis(a, axis=0, fisher=True, bias=True, ...)
  ├── m2 = _moment(a, 2)；m4 = _moment(a, 4)
  ├── 零方差保护 → nan
  ├── vals = m4 / m2**2.0           ← Pearson 峰度（正态=3）
  ├── bias=False 时套用 k 统计量校正（先加回 3）
  └── vals = vals - 3  若 fisher     ← Fisher 化
```

无偏校正用的是基于 k 统计量的公式：

\[
g_2^{\text{corr}} = \frac{1}{(n-2)(n-3)}\left[(n^2-1)\frac{m_4}{m_2^{2}} - 3(n-1)^2\right]
\]

一个容易踩坑的实现细节：校正公式算出的是 **Pearson 口径** 的无偏估计（正态 ≈ 3），所以源码里 `vals = xp.where(can_correct, nval + 3.0, vals)`——先在 Pearson 口径里替换，最后再用 `vals = vals - 3 if fisher else vals` 统一做 Fisher 化。这样无论 `fisher` 取何值，校正都正确。

#### 4.3.3 源码精读

`kurtosis` 的声明与文档（含 `fisher`/`bias` 参数说明）：

[_stats_py.py:L1338-L1438](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_stats_py.py#L1338-L1438)

核心计算与 Fisher 化的一行：

[_stats_py.py:L1419-L1438](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_stats_py.py#L1419-L1438)

其中无偏校正与「先加 3 再 Fisher 化」的精妙配合在 [L1431-L1437](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_stats_py.py#L1431-L1437)。

#### 4.3.4 代码实践

1. **目标**：对比 `fisher=True/False`，并验证三类分布的峰度排序（均匀 < 正态 < Laplace）。
2. **操作步骤**（待本地验证）：
   ```python
   import numpy as np
   from scipy.stats import kurtosis, norm, laplace, uniform

   rng = np.random.default_rng(0)
   for name, dist in [("uniform", uniform), ("norm", norm), ("laplace", laplace)]:
       d = dist.rvs(size=50000, random_state=rng)
       print(f"{name:8s} fisher=True:{kurtosis(d, fisher=True): .3f}  "
             f"fisher=False:{kurtosis(d, fisher=False): .3f}")
   ```
3. **观察现象**：`fisher=False` 恒比 `fisher=True` 大 3；均匀分布峰度为负，正态接近 0，Laplace 为正。
4. **预期结果**：uniform ≈ −1.2，norm ≈ 0，laplace ≈ 3（Fisher 口径）。
5. **结论**：Fisher 与 Pearson 仅差一个常数 3，但「正态归零」的约定让 Fisher 更便于做假设检验参考。

#### 4.3.5 小练习与答案

**练习 1**：把上一节的偏度实践扩展为本讲主实践——同时计算峰度，验证正态样本 `fisher=True` 接近 0。
**答案**：在 4.2.4 的脚本后加 `print(kurtosis(data))` 与 `print(kurtosis(data, bias=False))`，两者都应接近 0；`bias=False` 略有校正。

**练习 2**：为什么源码在 `bias=False` 校正时写成 `nval + 3.0` 再统一 `vals - 3`，而不是直接校正 Fisher 值？
**答案**：因为无偏校正公式 \(g_2^{\text{corr}}\) 推导自 Pearson 口径（正态=3）。先在 Pearson 口径完成校正，再按 `fisher` 决定是否减 3，可以用同一段代码同时服务 `fisher=True/False`，避免重复实现两套公式。

---

### 4.4 众数 mode：排序与游程计数

#### 4.4.1 概念说明

众数是数据中出现次数最多的值。连续数据几乎不会有重复值，所以 `mode` 主要用于离散/整数数据。

返回值是一个命名元组 `ModeResult(mode, count)`——同时给出众数值和它出现的次数。这与 `describe` 返回 `DescribeResult`、上一讲 `_xp_mean` 的设计哲学一致：把相关结果打包成可属性访问的对象。

当出现多个值频次并列最多时，scipy 只返回其中一个（最小的，因为底层会先排序）。

#### 4.4.2 核心流程

`mode` 有两条路径：

```
mode(a, ...)
  ├── 空数组 → 返回 ModeResult(NaN, 0)
  ├── 一维快速路径 → xp.unique_counts，取 count 最大的值
  └── 多维路径：
        1. xp.sort(a, axis=-1)              ← 沿轴排序
        2. 标记「与前一个元素不同」的位置     ← 游程边界
        3. 边界索引之差 = 每段游程长度（counts）
        4. 把 counts 广播回每个元素
        5. argmax(counts) → 取最长游程的代表值
```

多维路径的核心思想是：**排序后，相等的值必然相邻成「游程」（run），游程长度就是该值的频次**。通过对「边界」做 `diff`，可以一次性求出所有游程的长度，再用 `take_along_axis` 精准提取每个切片的众数。

注意一个特殊行为：`mode` 的装饰器带 `override={'nan_propagation': False}`，意思是 NaN **不传播**——NaN 被当作一个普通值参与计数（多个 NaN 视为同一个值）。

#### 4.4.3 源码精读

`ModeResult` 命名元组与 `_mode_result` 后处理（空切片时把 NaN count 改成 0）：

[_stats_py.py:L471-L482](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_stats_py.py#L471-L482)

`mode` 函数体（含一维快速路径与多维游程计数）：

[_stats_py.py:L501-L623](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_stats_py.py#L501-L623)

多维游程计数的关键四行（排序 → 边界 → diff 求长度 → argmax）在 [L609-L620](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_stats_py.py#L609-L620)。

#### 4.4.4 代码实践

1. **目标**：理解多维 `mode` 的逐轴行为与并列时的取值规则。
2. **操作步骤**（待本地验证）：
   ```python
   import numpy as np
   from scipy.stats import mode
   a = np.array([[3, 0, 3, 7],
                 [3, 2, 6, 2],
                 [1, 7, 2, 8],
                 [3, 0, 6, 1],
                 [3, 2, 5, 5]])
   r = mode(a, keepdims=True)
   print(r.mode, r.count)
   print(mode(a, axis=None, keepdims=False))  # 全局众数
   ```
3. **观察现象**：默认 `axis=0`，对每一**列**求众数；`axis=None` 在整个数组上求众数。
4. **预期结果**：列方向 `mode=[[3,0,6,1]]`，`count=[[4,2,2,1]]`；全局众数为 3，count 为 5。
5. **结论**：`axis` 决定「沿哪个方向压缩」，`keepdims` 决定结果是否保留被压缩的轴。

#### 4.4.5 小练习与答案

**练习**：对 `[1, 2, 2, 3, 3]`（2 和 3 并列最多）调用 `mode`，返回的是 2 还是 3？为什么？
**答案**：返回 2。因为底层先排序，再取第一个达到最大频次的游程，所以并列时返回**较小的**值。可用 `mode([1,2,2,3,3]).mode` 验证。

---

### 4.5 变异系数 variation：标准差除以均值

#### 4.5.1 概念说明

变异系数（CV）是标准差与均值的比值，用来衡量**相对离散程度**：

\[
\mathrm{CV} = \frac{s}{\bar{x}}
\]

它的好处是无量纲——比较「身高波动」和「体重波动」时，CV 比标准差更公平，因为它消去了量纲。

一个细节：scipy 的 `variation` **不**对均值取绝对值，所以均值是负数时 CV 也是负数。这一点在文档里明确写了。

#### 4.5.2 核心流程

`variation` 独立在 [_variation.py](_variation.py) 里，逻辑很精炼：

```
variation(a, axis=0, ddof=0, ...)
  ├── n = 非掩码样本数
  ├── mean_a = xp.mean(a)
  ├── std_a  = xp.std(a)            ← 注意：这里 ddof=0
  ├── correction = sqrt(n / (n-ddof))   ← 把 ddof=0 校正到目标 ddof
  ├── result = std_a * correction / mean_a
  └── 边界情况（ddof==n）：std>0 → copysign(inf, mean)，否则 nan
```

这里有个巧妙的实现：NumPy 的 `xp.std` 只算 ddof=0 的标准差，scipy 想支持任意 `ddof`，于是乘上校正因子 \(\sqrt{n/(n-\text{ddof})}\) 把它转换成 ddof 对应的版本。

默认 `ddof=0`（向后兼容），但文档建议用 `ddof=1` 以得到无偏样本标准差——这与本讲「分母」主题一脉相承。

#### 4.5.3 源码精读

`variation` 的声明与文档（含边界情况说明）：

[_variation.py:L20-L133](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_variation.py#L20-L133)

核心计算（mean、std、校正因子、相除）：

[_variation.py:L105-L133](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_variation.py#L105-L133)

校正因子 `correction = (n / (n - ddof))**0.5` 与 `result = std_a * correction / mean_a` 在 [L119-L120](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_variation.py#L119-L120)。

#### 4.5.4 代码实践

1. **目标**：比较 `ddof=0` 与 `ddof=1` 的差异，并验证 CV 是无量纲的。
2. **操作步骤**（待本地验证）：
   ```python
   from scipy.stats import variation
   x = [1, 2, 3, 4, 5]
   print(variation(x, ddof=0))   # 默认
   print(variation(x, ddof=1))   # 文档推荐
   print(variation([100, 200, 300, 400, 500], ddof=1))  # 同样放大 100 倍
   ```
3. **观察现象**：第二组数据是第一组放大 100 倍，但 CV 相同（因为是无量纲量）。
4. **预期结果**：第一行约 0.471，第二行约 0.527，第三行与第二行相同（≈ 0.527）。
5. **结论**：CV 消去量纲，适合比较不同尺度的数据离散度；`ddof` 越大，CV 越大（因为标准差被放大）。

#### 4.5.5 小练习与答案

**练习**：为什么 `variation` 文档要单独说明「均值是负数时返回负数」？
**答案**：因为 CV 定义为 \(s/\bar{x}\)，没有取绝对值。若用户期望 CV 恒为正（表示「波动幅度」），负均值会得到负值，造成误解。scipy 选择忠实于定义而非猜测用户意图，所以用文档明确这一行为。

---

## 5. 综合实践

把本讲五个函数串起来，对同一批数据做一次「手工版 describe」：

1. **目标**：用 `moment`/`skew`/`kurtosis`/`mode`/`variation` 重建 `describe` 的关键字段，理解它们的内在联系。
2. **操作步骤**（待本地验证）：
   ```python
   import numpy as np
   from scipy.stats import (moment, skew, kurtosis, mode, variation, describe)

   rng = np.random.default_rng(7)
   data = rng.poisson(lam=3.0, size=1000).astype(float)  # 离散、右偏数据

   # 手工摘要
   print("mean      =", moment(data, order=1, center=0))  # 原点一阶矩=均值
   print("var(n)    =", moment(data, order=2))            # 除以 n 的方差
   print("skew      =", skew(data))
   print("kurt(F)   =", kurtosis(data, fisher=True))
   print("kurt(P)   =", kurtosis(data, fisher=False))
   print("mode/count=", mode(data))
   print("cv(ddof=1)=", variation(data, ddof=1))

   # 对照 describe
   print("\n--- describe 对照 ---")
   print(describe(data))
   ```
3. **观察现象**：
   - 泊松分布是右偏的，`skew` 应为正。
   - `moment(order=2)` 与 `describe().variance` 不相等（前者除以 n，后者除以 n−1）。
   - `fisher=False` 比 `fisher=True` 恰好大 3。
   - `mode` 返回最常出现的计数值。
4. **预期结果**：skew 为正（约 0.5 量级）；`fisher` 差为 3；`moment(2) < describe().variance`。
5. **结论**：`describe` 就是本讲这些「矩零件」的组装成品。理解了零件，你就能按需替换（比如用 `bias=False` 或换 `ddof`）。

## 6. 本讲小结

- **矩是底层零件**：`moment` 计算 \(m_k=\frac{1}{n}\sum(x_i-c)^k\)（除以 n、不修正），`skew`/`kurtosis`/`_var` 都建立在它之上。
- **偏度 = 三阶矩 / 二阶矩^1.5**：`bias=False` 用系数 \(\sqrt{N(N-1)}/(N-2)\) 校正为 \(G_1\)。
- **峰度有 Fisher/Pearson 两套约定**：默认 Fisher（减 3，正态为 0），`fisher=False` 为 Pearson（正态为 3）；无偏校正巧妙地「先加 3 再 Fisher 化」以复用代码。
- **自由度修正（ddof）与偏倚校正（bias）是两套独立机制**：`moment` 不做 ddof 修正，`_var(ddof=1)` 才校正到 n−1；`bias` 则是针对矩估计量的解析校正。
- **mode 用排序+游程计数**：并列时返回较小值；NaN 不传播，被当作普通值。
- **variation = std/mean**：无量纲的相对离散度，通过 \(\sqrt{n/(n-\text{ddof})}\) 把 ddof=0 的标准差校正到任意 ddof。
- **五个函数共享 `_axis_nan_policy_factory` 装饰器**：统一获得 axis/nan_policy/keepdims 能力（u5-l1 详讲）。

## 7. 下一步学习建议

- **继续本单元**：下一讲 u2-l3「分位数与频率统计」将讲解 `quantile`、`binned_statistic` 等，补齐描述性统计的最后一块拼图。
- **深入装饰器**：本讲反复出现的 `@_axis_nan_policy_factory` 是整个 scipy.stats 的公共基础设施，建议读 u5-l1「`_axis_nan_policy` 装饰器工厂」与 u5-l2「nan_policy 的三种策略」，理解 `axis`/`nan_policy` 是如何被统一注入的。
- **回到 describe**：带着本讲对矩的理解，重读 u2-l1 的 `describe`，你会清楚地看到 `variance`/`skewness`/`kurtosis` 三个字段是如何由 `_var`/`skew`/`kurtosis` 拼装的。
- **延伸阅读源码**：本讲的 [_moment](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_stats_py.py#L1198-L1217) 与 [_demean](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/stats/_stats_py.py#L1167-L1195) 是后续假设检验（如 `zscore`、`ttest`）也会复用的底层工具，值得收藏。
