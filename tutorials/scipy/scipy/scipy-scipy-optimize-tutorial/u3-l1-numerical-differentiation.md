# 数值微分：approx_derivative 与有限差分

## 1. 本讲目标

本讲是单元 3「导数近似与函数封装基础设施」的第一讲。在 `scipy.optimize` 里，几乎所有梯度类/牛顿类优化算法（BFGS、CG、Newton-CG、trust-constr、least_squares…）在用户**不提供解析导数**时，都要靠同一套「有限差分」机制去估计梯度或雅可比。这套机制全部集中在一个文件 [`_numdiff.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_numdiff.py) 里。

学完本讲你应当能够：

- 说清楚 `approx_derivative` 是什么、解决什么问题，以及它如何对**向量函数** \( f:\mathbb{R}^n \to \mathbb{R}^m \) 估计雅可比矩阵；
- 区分三种差分格式 `2-point` / `3-point` / `cs`（复步）的精度与代价，并理解默认相对步长为何取 \( \mathrm{EPS}^{1/s} \)；
- 解释 `_adjust_scheme_to_bounds` 如何在变量有上下界时，自动翻转步长方向或退化为中心/单侧差分，保证扰动点不越界；
- 理解 `_dense_difference`（逐列扰动）与 `_sparse_difference`（按列分组同时扰动）的差别；
- 掌握 `group_columns` 的 Curtis–Powell–Reid 贪心分组思想，明白它为何能把稀疏雅可比的函数求值次数从 \( n \) 降到「分组数」；
- 会用公开接口 `approx_fprime` / `check_grad` 估计标量梯度并校验解析梯度。

## 2. 前置知识

- **雅可比矩阵（Jacobian）**：若 \( f:\mathbb{R}^n \to \mathbb{R}^m \)，其雅可比是一个 \( m \times n \) 矩阵，第 \( (i,j) \) 元是 \( \partial f_i / \partial x_j \)。当 \( m=1 \) 时，雅可比退化为长度 \( n \) 的**梯度**。本讲中「求导」几乎都指「估计雅可比」。
- **有限差分（finite difference）**：用 \( \dfrac{f(x+h)-f(x)}{h} \) 这类代数差商去近似导数。它的误差来自两部分：
  - **截断误差（truncation error）**：泰勒展开里丢掉的高阶项，\( h \) 越小越小；
  - **舍入误差（round-off error）**：浮点数相减抵消有效数字，\( h \) 越小越大。
  - 二者此消彼长，存在一个「最优步长」使总误差最小。
- **复步差分（complex-step）**：把扰动取在虚轴上，求 \( \mathrm{Im}\,f(x+\mathrm{i}h)/h \)。它**没有减法抵消**，因而可用极小步长得到接近机器精度的导数，代价是要求函数能解析延拓到复数域。
- **稀疏雅可比**：很多实际问题里雅可比每行只有少数几个非零元（例如每个输出只依赖少数输入）。这时可以把「互不干扰」的若干列**同时**扰动，用一次函数求值同时估出多列，从而大幅减少求值次数。
- **机器精度 EPS**：`np.finfo(np.float64).eps ≈ 2.22e-16`，是 1 与下一个可表示浮点数的间距。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [`_numdiff.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_numdiff.py) | 数值微分全部核心：步长计算、边界自适应、稠密/稀疏/线性算子三种差分实现、`approx_derivative`、`check_derivative` |
| [`_group_columns.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_group_columns.py) | 稀疏雅可比列分组的 Pythran 加速实现（`group_dense` / `group_sparse`） |
| [`_optimize.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py) | 公开标量梯度接口 `approx_fprime`（[:L971](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L971)）与 `check_grad`（[:L1053](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L1053)），二者都薄封装到 `_numdiff` |

> 注意可见性：`approx_fprime`、`check_grad` 是 `scipy.optimize` 的**公开**接口；而 `approx_derivative`、`check_derivative`、`group_columns` 是 `_numdiff` 的**内部**接口（名字在 `__init__.py` 的 autosummary 列表里查不到），调用时要写 `from scipy.optimize._numdiff import approx_derivative`。

## 4. 核心概念与源码讲解

### 4.1 approx_derivative：向量函数雅可比的统一估计器

#### 4.1.1 概念说明

优化算法运行时反复需要雅可比 \( J(x) \)。当用户没给解析 `jac` 时，调度层（`minimize` / `least_squares` / `root`）会把目标函数连同 `jac='2-point'` 之类的字符串传下去，最终都汇聚到 `approx_derivative` 这一个函数。可以把它理解成：

> 「给我一个函数 \( f \)、一个点 \( x_0 \)、一种差分格式，我还你一个雅可比的近似。」

它支持三种格式：

| method | 公式 | 精度阶 | 每列函数求值数 | 是否需要复数 |
| --- | --- | --- | --- | --- |
| `2-point` | 前向/后向差分 | \( O(h) \) 一阶 | 1 | 否 |
| `3-point` | 中心差分（内部）/ 二阶前向后向（边界） | \( O(h^2) \) 二阶 | 2 | 否 |
| `cs` | 复步差分 | 近似 \( O(h^2) \)，且无抵消 | 1 | 是 |

默认是 `method='3-point'`（见函数签名 [:L288](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_numdiff.py#L288)），因为中心差分精度更高、且不要求函数支持复数。

#### 4.1.2 核心流程

`approx_derivative` 自身**不做**差分算术，它是一个编排器（orchestrator）：

```text
approx_derivative(fun, x0, method, ...)
  │
  ├─ 1. 校验 method ∈ {'2-point','3-point','cs'}
  ├─ 2. 把 x0 提升为 1-D 浮点数组；准备边界 lb, ub
  ├─ 3. 用 _Fun_Wrapper 封装 fun（便于多进程 pickle）
  ├─ 4. 计算/复用 f0 = fun(x0)
  ├─ 5. 计算绝对步长 h（相对步长 → 绝对步长）
  ├─ 6. _adjust_scheme_to_bounds(h, lb, ub)  # 让扰动点不越界
  │
  └─ 7. 按需分派：
        ├─ as_linear_operator=True → _linear_operator_difference  # 只给 J·p
        ├─ sparsity=None          → _dense_difference             # 逐列扰动
        └─ sparsity 给定          → _sparse_difference            # 分组扰动
```

#### 4.1.3 源码精读

**(a) 入口校验与边界准备** —— 先把 `x0` 规整成 1-D 浮点数组，并要求 `x0` 不能越界：

[_numdiff.py:522-542](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_numdiff.py#L522-L542) —— 校验 `method`，提升 `x0` 为浮点，准备 `lb/ub` 并检查形状一致、检查 `x0` 是否落在 `[lb, ub]` 内。

**(b) 复用 f0 与 nfev 计数** —— `f0` 如果调用方已经算过就不再重算（这是 `ScalarFunction`/`VectorFunction` 缓存能省一次求值的关键）：

[_numdiff.py:561-572](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_numdiff.py#L561-L572) —— `f0 is None` 时才调用 `fun` 并把 `nfev` 置 1；同时显式检查 `x0` 不违反边界。

> 注释 [:L554-560](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_numdiff.py#L554-L560) 解释了为何 `nfev` 改在函数内部累加：因为 `workers` 可能是多进程 map，跨进程同步计数器很难，所以由差分循环自己统计本循环产生的求值数 `_nfev`。

**(c) 三路分派** —— 这是编排器的核心：

[_numdiff.py:574-628](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_numdiff.py#L574-L628) —— 根据 `as_linear_operator` 与 `sparsity` 三个分支：线性算子、稠密、稀疏。注意 `sparsity` 既可以是结构矩阵（此时内部调 `group_columns` 自动分组），也可以是 `(structure, groups)` 二元组（调用方预分组以省时间）。

**(d) 线性算子模式** —— 当算法只需要 \( J \cdot p \) 而不需要显式 \( J \)（例如 `hessp` 类牛顿法、GMRES）时，返回一个 `LinearOperator`，其 `matvec` 对任意方向 \( p \) 只扰一次：

[_numdiff.py:638-680](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_numdiff.py#L638-L680) —— `_linear_operator_difference`，对 `2-point` 单次求值、`3-point` 两次、`cs` 一次（虚部）。

#### 4.1.4 代码实践

1. **目标**：亲手调用内部接口 `approx_derivative`，对一个二维→二维的向量函数估计雅可比，并用解析值核对。
2. **操作步骤**：

```python
# 示例代码
import numpy as np
from scipy.optimize._numdiff import approx_derivative

def f(x, c1, c2):
    return np.array([x[0] * np.sin(c1 * x[1]),
                     x[0] * np.cos(c2 * x[1])])

def jac_analytic(x, c1, c2):
    return np.array([
        [np.sin(c1*x[1]),  c1*x[0]*np.cos(c1*x[1])],
        [np.cos(c2*x[1]), -c2*x[0]*np.sin(c2*x[1])],
    ])

x0 = np.array([1.0, 0.5*np.pi])
for method in ['2-point', '3-point', 'cs']:
    J, info = approx_derivative(f, x0, method=method, args=(1, 2),
                                full_output=True)
    err = np.abs(J - jac_analytic(x0, 1, 2)).max()
    print(f"{method:8s} nfev={info['nfev']}  max_err={err:.2e}")
```

3. **观察现象**：`2-point` 的 `nfev` 应为 `1(基点) + 2(列)`；`3-point` 为 `1 + 2*2`；`cs` 为 `1 + 2`。误差上 `cs` 最小（接近机器精度），`3-point` 次之，`2-point` 最大。
4. **预期结果**：`cs` 的 `max_err` 在 `1e-15` 量级；`3-point` 在 `1e-10` 量级；`2-point` 在 `1e-8` 量级（具体数值「待本地验证」，取决于平台）。

#### 4.1.5 小练习与答案

**练习 1**：把上面的 `f` 改成标量函数（`m=1`），观察返回 `J` 的形状。
**答案**：稠密模式下 `m==1` 时返回的是形状 `(n,)` 的一维梯度而非 `(1,n)`，见 [:L764-765](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_numdiff.py#L764-L765) 的 `np.ravel` 处理。

**练习 2**：为什么 `approx_derivative` 默认方法不是精度最高的 `cs`？
**答案**：`cs` 要求函数能解析延拓到复平面（不能含 `abs`、`np.where`、不连续分支等），大多数用户的实函数不满足；而 `3-point` 中心差分精度已达二阶且无此限制，是更安全的默认。

---

### 4.2 步长与边界自适应：_eps_for_method 与 _adjust_scheme_to_bounds

#### 4.2.1 概念说明

差分成败的关键在步长 \( h \)。`_numdiff` 用两个函数各管一头：

- **`_eps_for_method`**：决定**相对**步长基准。理论上最优相对步长是 \( \mathrm{EPS}^{1/s} \)，其中 \( s \) 是格式的「阶数分母」——前向差分与复步取 \( s=2 \)、中心差分取 \( s=3 \)。这来自「截断误差 \( \propto h^s \)」与「舍入误差 \( \propto \mathrm{EPS}/h \)」求和取极小。
- **`_adjust_scheme_to_bounds`**：决定**绝对**步长在边界附近怎么调整。当 \( x_0 \) 离边界很近、按正常步长扰动会越出 `[lb, ub]` 时，要么翻转步长方向（前向变后向），要么把中心差分退化成单侧二阶差分。

#### 4.2.2 核心流程

最优步长的推导（以前向差分为例）：总误差

\[
E(h) \;\approx\; \underbrace{c_1\, h}_{\text{截断}} \;+\; \underbrace{c_2\,\dfrac{\mathrm{EPS}}{h}}_{\text{舍入}}
\]

令 \( \mathrm{d}E/\mathrm{d}h = 0 \) 得 \( h^\* \propto \mathrm{EPS}^{1/2} \)。对中心差分，截断项是 \( O(h^2) \)，故 \( h^\* \propto \mathrm{EPS}^{1/3} \)。代码用 `**0.5` 与 `**(1/3)` 表达这两者。

边界自适应的核心是把每个坐标分类：

- `1-sided`（前向/后向）：若正向步长越界，则能容纳就翻号，不能容纳就贴着边界取最大可用步长；
- `2-sided`（中心）：若两侧都够，用中心；否则退化成单侧二阶差分，并返回 `use_one_sided=True` 告诉差分循环该列改用单侧公式。

#### 4.2.3 源码精读

**(a) 相对步长 `_eps_for_method`** —— 取 `x0` 与 `f0` 中**更小**的浮点类型的 EPS（避免给 float32 用 float64 步长），再按方法取幂：

[_numdiff.py:138-141](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_numdiff.py#L138-L141) —— `2-point`/`cs` 返回 `EPS**0.5`，`3-point` 返回 `EPS**(1/3)`。用 `@functools.lru_cache` 装饰（[:L93](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_numdiff.py#L93)）以缓存，因为同 dtype+method 组合会反复调用。

**(b) 绝对步长 `_compute_absolute_step`** —— 把相对步长乘上 `sign(x0)·max(1,|x0|)`，并处理「步长太小被浮点吞掉」的退化：

[_numdiff.py:177-201](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_numdiff.py#L177-L201) —— 注意 `sign_x0 = (x0>=0)*2-1`，目的是让 `x0==0` 时符号为 `+1`（而不是 `np.sign(0)==0`）。若用户自定义 `rel_step` 导致 `(x0+abs_step)-x0 == 0`（步长被吞），则回退到默认步长（[:L196-201](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_numdiff.py#L196-L201)）。

**(c) 边界自适应 `_adjust_scheme_to_bounds`** —— 分 `1-sided` 与 `2-sided` 两条路径：

[_numdiff.py:62-71](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_numdiff.py#L62-L71) —— `1-sided` 路径：`violated & fitting` 的列翻号；放不下的列按「离哪侧边界更远」选前向或后向，并把步长压成 `upper_dist/num_steps` 或 `-lower_dist/num_steps`。

[_numdiff.py:72-88](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_numdiff.py#L72-L88) —— `2-sided` 路径：`central` 掩码是两侧都放得下；其余退化成单侧，并把步长缩到 `0.5 * dist / num_steps` 以保证单侧两点都落在界内。

> 快速出口 [:L53-54](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_numdiff.py#L53-L54)：若完全无界（`lb=-inf, ub=inf`），直接原样返回 `h`，跳过所有边界逻辑——这是无约束问题最常见的快路径。

#### 4.2.4 代码实践

1. **目标**：观察边界如何改变差分方向。文档里给的经典例子是在分段函数的「折点」上分别求左、右导数。
2. **操作步骤**：

```python
# 示例代码
import numpy as np
from scipy.optimize._numdiff import approx_derivative

def g(x):
    x = np.atleast_1d(x)[0]
    return np.array([x**2 if x >= 1 else x])

x0 = np.array([1.0])
# 上界=1 → 只能向左扰动 → 得到左导数（折点左侧斜率 = 1）
print(approx_derivative(g, x0, bounds=(-np.inf, 1.0)))   # 预期 [1.]
# 下界=1 → 只能向右扰动 → 得到右导数（折点右侧斜率 = 2）
print(approx_derivative(g, x0, bounds=(1.0, np.inf)))    # 预期 [2.]
```

3. **观察现象**：两次调用得到 `1.` 和 `2.`，说明边界约束让 `_adjust_scheme_to_bounds` 自动选择了不同的扰动方向。
4. **预期结果**：第一行输出接近 `[1.]`，第二行接近 `[2.]`（与文档示例 [:L474-481](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_numdiff.py#L474-L481) 一致）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_eps_for_method` 要在 `x0` 和 `f0` 的 dtype 之间取**较小**者？
**答案**：雅可比的精度受「参数表示精度」与「函数值表示精度」共同限制，取较小的 EPS（即较低精度类型）才能保证步长对该类型也够大、不被吞掉（见 [:L132-136](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_numdiff.py#L132-L136)）。

**练习 2**：把上面的 `g` 改成完全无界调用，跟踪代码会走哪条快路径？
**答案**：会命中 [:L53-54](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_numdiff.py#L53-L54) 的 `np.all((lb==-inf)&(ub==inf))` 直接返回，`use_one_sided` 全为原值。

---

### 4.3 稠密与稀疏差分：_dense_difference 与 _sparse_difference

#### 4.3.1 概念说明

`approx_derivative` 把「具体怎么扰动、怎么算差商」交给两个实现：

- **`_dense_difference`**：最朴素——**逐列**扰动。要估第 \( j \) 列就把 \( x_j \) 加一个步长，求一次 `fun`。代价是 `2-point` 需 \( n \) 次求值、`3-point` 需 \( 2n \) 次。当雅可比实际是稠密的，这已经最优。
- **`_sparse_difference`**：当雅可比**稀疏**时，逐列太浪费。它借助 `group_columns` 给出的分组，把「互不干扰」的一组列**同时**扰动，用一次（或两次）求值同时估出整组列。求值次数从 \( n \) 降到「分组数」，这是 Curtis–Powell–Reid（1974）的经典思想。

#### 4.3.2 核心流程

**稠密**（以 `3-point` 为例）：

```text
for i in 0..n-1:
    if use_one_sided[i]:        # 边界退化
        x1 = x0; x1[i] += h[i]
        x2 = x0; x2[i] += 2h[i]
        df = -3 f0 + 4 f(x1) - f(x2)      # 二阶前向
    else:                       # 标准 3-point 中心
        x1 = x0; x1[i] -= h[i]
        x2 = x0; x2[i] += h[i]
        df = f(x2) - f(x1)
    J[:, i] = df / dx
```

**稀疏**：

```text
groups = group_columns(structure)        # 每列归属哪个组
for group in 0..n_groups-1:
    e = (groups == group)                # 本组要扰动的列掩码
    x_pert = x0 + h * e                  # 本组列同时扰动
    f_eval = fun(x_pert)
    # 只把 f_eval 写回 structure 中本组列的非零行
```

#### 4.3.3 源码精读

**(a) 稠密 `2-point` 生成器** —— 每次复制 `x0` 再扰动一维：

[_numdiff.py:694-712](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_numdiff.py#L694-L712) —— `x_generator2` 逐列产出 `x1`；注释 [:L697-701](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_numdiff.py#L697-L701) 解释了为何用 `np.copy` 而非预生成大数组——是多进程安全与 \( n^2 \) 内存的权衡。注意分母 `dx` 用 `(x0[i]+h[i])-x0[i]` 重新计算而非直接用 `h[i]`，这正是为了避免浮点吞步长带来的误差。

**(b) 稠密 `3-point` 与单侧退化**：

[_numdiff.py:715-747](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_numdiff.py#L715-L747) —— `x_generator3` 按 `use_one_sided` 决定中心还是单侧二阶公式；单侧用 `df = -3 f0 + 4 f1 - f2`（二阶前向），中心用 `df = f2 - f1`。

**(c) 稠密 `cs` 复步**：

[_numdiff.py:749-757](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_numdiff.py#L749-L757) —— 把 `x0` 转成复数，沿虚轴扰动，取 `f1.imag / h`。无需减 `f0`，故无抵消。

**(d) 稀疏差分的分组扰动**：

[_numdiff.py:782-792](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_numdiff.py#L782-L792) —— `e_generator` 产出每个组的列掩码 `e`，`x_generator2` 把整组列同时加上 `h*e`。

[_numdiff.py:827-877](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_numdiff.py#L827-L877) —— 对每个组，用 `structure[:, cols]` 的非零位置 `(i,j)` 把差商 `df[i]/dx[j]` 写到正确的稀疏位置，最后用 `csr_array` 组装（[:L879-893](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_numdiff.py#L879-L893)）。

#### 4.3.4 代码实践

1. **目标**：用同一个稀疏雅可比结构，对比稠密与稀疏两种模式的 `nfev`。
2. **操作步骤**：

```python
# 示例代码
import numpy as np
from scipy.optimize._numdiff import approx_derivative

# f: R^4 -> R^4，但每个输出只依赖两个输入 → 雅可比是带状稀疏
def f(x):
    return np.array([x[0]+x[1], x[1]+x[2], x[2]+x[3], x[0]+x[3]])

x0 = np.array([1.0, 2.0, 3.0, 4.0])

# 结构矩阵：1 表示该位置雅可比可能非零
structure = np.array([
    [1, 1, 0, 0],
    [0, 1, 1, 0],
    [0, 0, 1, 1],
    [1, 0, 0, 1],
])

J_dense, info_d = approx_derivative(f, x0, full_output=True)
J_sparse, info_s = approx_derivative(f, x0, sparsity=structure, full_output=True)

print("dense  nfev =", info_d['nfev'])
print("sparse nfev =", info_s['nfev'])
print("J equal?", np.allclose(np.asarray(J_sparse.todense()), J_dense))
```

3. **观察现象**：稠密 `3-point` 的 `nfev` 应为 `1 + 2*4 = 9`；稀疏模式的分组数通常为 2，故 `nfev ≈ 1 + 2*2 = 5`，明显更少，而两者雅可比数值一致。
4. **预期结果**：`sparse nfev < dense nfev`，且 `J equal?` 为 `True`（精确分组数「待本地验证」，取决于 `group_columns` 的随机种子）。

#### 4.3.5 小练习与答案

**练习 1**：稠密 `2-point` 模式下，估一个 \( n=100 \) 维函数的雅可比需要多少次 `fun` 求值（含基点）？
**答案**：`1 + n = 101` 次（基点 1 次 + 每列 1 次，见 [:L712](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_numdiff.py#L712) 的 `nfev += len(df_dx)`）。

**练习 2**：为什么稀疏模式必须配合 `structure` 才有意义？
**答案**：没有结构信息就不知道哪些列能合并扰动；`structure` 标出每个雅可比位置的零/非零模式，`group_columns` 才能据此把「不共享任何非零行」的列归为一组（见 4.4）。

---

### 4.4 稀疏雅可比的列分组：group_columns

#### 4.4.1 概念说明

`group_columns` 解决一个**图着色**问题：给定雅可比的稀疏结构（一个 0/1 矩阵），把列分成尽量少的组，使得**同一组内任意两列在每一行都至少有一个为零**（即它们「结构正交」，同时扰动不会互相干扰）。这是 Curtis–Powell–Reid（1974）提出的稀疏雅可比估计法，文献索引见 [:L252-255](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_numdiff.py#L252-L255)。

直观例子：

\[
\begin{bmatrix} 1 & 1 & 0 & 0 \\ 0 & 1 & 1 & 0 \\ 0 & 0 & 1 & 1 \\ 1 & 0 & 0 & 1 \end{bmatrix}
\]

列 0 与列 2 在每行都不同时非零 → 可同组；列 1 与列 3 同理 → 可同组。于是 4 列只需 2 组，求值次数减半。

#### 4.4.2 核心流程

贪心算法（`group_dense`）：

```text
groups = [-1] * n
current_group = 0
for i in 0..n-1:
    if groups[i] >= 0: continue          # 已分组
    groups[i] = current_group
    union = structure[:, i]              # 本组已选列的「并」
    for j in 0..n-1:
        if groups[j] >= 0: continue
        if 列 j 与 union 无交集:          # 结构正交
            union += 列 j
            groups[j] = current_group
    current_group += 1
return groups
```

注意：列的处理顺序会影响分组数，所以 `group_columns` 允许传 `order`（列枚举顺序），默认用 `RandomState(order)` 生成可复现的随机排列（[:L268-270](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_numdiff.py#L268-L270)）。

#### 4.4.3 源码精读

**(a) Python 包装 `group_columns`** —— 把输入规整成 0/1 矩阵或 CSC 稀疏，做列置换后分派：

[_numdiff.py:257-285](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_numdiff.py#L257-L285) —— 稠密输入转 `int32` 的 0/1 矩阵（[:L260-261](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_numdiff.py#L260-L261)），稀疏输入转 `csc_array`（[:L257-258](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_numdiff.py#L257-L258)）；按 `order` 置换列后调用加速后端，最后用 `groups[order] = groups.copy()` 把结果还原回原始列序（[:L283](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_numdiff.py#L283)）。

**(b) Pythran 加速实现 `group_dense`**：

[_group_columns.py:10-52](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_group_columns.py#L10-L52) —— 即上面伪代码的落地。`union` 数组累积本组已选列的非零行，内层 `for k` 检测列 `j` 是否与 `union` 有公共非零行（[:L37-40](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_group_columns.py#L37-L40)）；无交集则并入（[:L43-45](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_group_columns.py#L43-L45)）。文件头注释 [:L1-4](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_group_columns.py#L1-L4) 说明这是从早期 Cython 版本迁移来的 Pythran 实现，`#pythran export` 行声明了导出签名以供编译。

**(c) 稀疏输入版本 `group_sparse`**：[_group_columns.py:59-97](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_group_columns.py#L59-L97)，逻辑相同，但用 CSR 的 `indices/indptr` 直接遍历非零元，避免稠密化的内存爆炸。

#### 4.4.4 代码实践

1. **目标**：直接调用 `group_columns`，对上面的带状结构算分组，验证分组数与「同组列结构正交」。
2. **操作步骤**：

```python
# 示例代码
import numpy as np
from scipy.optimize._numdiff import group_columns

structure = np.array([
    [1, 1, 0, 0],
    [0, 1, 1, 0],
    [0, 0, 1, 1],
    [1, 0, 0, 1],
], dtype=np.int32)

groups = group_columns(structure, order=0)
print("groups =", groups)              # 形如 [0 1 0 1]
print("n_groups =", groups.max() + 1)  # 预期 2

# 验证同一组内任意两列结构正交
for g in range(groups.max() + 1):
    cols = np.where(groups == g)[0]
    overlap = structure[:, cols].sum(axis=1)
    assert np.all(overlap <= 1), f"组 {g} 有行冲突"
print("同组列结构正交：通过")
```

3. **观察现象**：4 列被分成 2 组，每组 2 列，且每组内任意一行最多一个非零。
4. **预期结果**：`n_groups == 2`，断言通过（具体 `groups` 数值因随机置换可能为 `[0 1 0 1]` 或 `[1 0 1 0]`，组数不变）。

#### 4.4.5 小练习与答案

**练习 1**：把 `structure` 换成一个全稠密矩阵（全 1），`group_columns` 会返回多少组？
**答案**：`n` 组（每列单独一组），因为任意两列在所有行都同时非零，无法合并——此时稀疏差分退化为稠密，没有收益。

**练习 2**：为什么 `group_columns` 默认用随机置换列顺序？
**答案**：贪心着色的分组数依赖列的处理顺序，固定顺序可能对某些结构不优；用可复现的随机置换（`order=0` 作种子）在多数结构上能得到较好的分组，同时保证可重复。

---

### 4.5 标量梯度公开接口：approx_fprime 与 check_grad

#### 4.5.1 概念说明

`_numdiff` 是内部基础设施，普通用户日常用到的是 `scipy.optimize` 暴露的两个**公开**函数：

- **`approx_fprime(xk, f, epsilon, *args)`**：用**前向差分**（`2-point`）估计标量或向量函数的梯度/雅可比。它就是 `approx_derivative(..., method='2-point', abs_step=epsilon)` 的薄封装。
- **`check_grad(func, grad, x0, ...)`**：把你手写的解析梯度 `grad` 与 `approx_fprime` 的有限差分估计做差，返回二者差的 2-范数，用于单元测试里校验梯度实现是否正确。

此外 `_numdiff` 还有一个内部版 `check_derivative(fun, jac, x0)`（[:L919](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_numdiff.py#L919)），用相对误差度量雅可比，且支持稀疏 `jac`。

#### 4.5.2 核心流程

`approx_fprime` 的前向差分公式（见其 docstring [:L1023-1028](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L1023-L1028)）：

\[
f'_i \;\approx\; \frac{f(x_k + \epsilon\, e_i) - f(x_k)}{\epsilon}
\]

默认 `epsilon = sqrt(np.finfo(float).eps) ≈ 1.49e-8`（即 `_epsilon`，定义在 [:L192](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L192)），正是前向差分的最优相对步长。

`check_grad` 支持两种模式：

- `direction='all'`：沿每个坐标轴 \( e_i \) 各查一次，返回 `||grad(x0) - approx_fprime(x0)||`；
- `direction='random'`：沿一个随机方向 \( v \) 查（只对**标量**函数有效），用方向导数 \( \nabla f \cdot v \) 比对，适合高维时降低检查成本。

#### 4.5.3 源码精读

**(a) `approx_fprime` 委托给 `approx_derivative`**：

[_optimize.py:1045-1049](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L1045-L1049) —— 先算 `f0 = f(xk)`，再调用 `approx_derivative(f, xk, method='2-point', abs_step=epsilon, args=args, f0=f0)`。注意它显式传 `f0`，复用基点求值，省一次函数调用。

**(b) `check_grad` 的方向导数技巧**：

[_optimize.py:1117-1118](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L1117-L1118) —— 定义 `g(w) = func(x0 + w*v)`，把「沿方向 v 的方向导数」转化为「g 在 w=0 处的标量导数」，从而复用 `approx_fprime`。

[_optimize.py:1120-1138](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L1120-L1138) —— `direction='random'` 生成随机向量 `v`、解析方向导数 `dot(grad, v)`；`direction='all'` 直接用各坐标轴。

[_optimize.py:1140-1142](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L1140-L1142) —— 返回 `sqrt(sum(|analytical - approx_fprime|**2))`，即差的 2-范数。

#### 4.5.4 代码实践

1. **目标**：用 `check_grad` 校验一段手写梯度，体会它在单元测试里的用法。
2. **操作步骤**：

```python
# 示例代码
import numpy as np
from scipy.optimize import check_grad, approx_fprime

def func(x):
    return x[0]**2 - 0.5 * x[1]**3

def grad(x):
    return np.array([2*x[0], -1.5*x[1]**2])

x0 = np.array([1.5, -1.5])

print("check_grad (all)    =", check_grad(func, grad, x0))
rng = np.random.default_rng(0)
print("check_grad (random) =", check_grad(func, grad, x0, direction='random', rng=rng))
print("approx_fprime       =", approx_fprime(x0, func))
print("grad analytic       =", grad(x0))
```

3. **观察现象**：`check_grad` 的返回值应在 `1e-8` 量级（前向差分固有的截断误差），说明解析梯度正确；`direction='all'` 与 `'random'` 结果相近。
4. **预期结果**：两个 `check_grad` 输出都在 `~3e-8`（与文档示例 [:L1106-1111](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L1106-L1111) 一致，精确值「待本地验证」）。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `check_grad` 用**前向差分**（一阶）而不是中心差分（二阶）来核对？
**答案**：`check_grad` 的目的是快速发现解析梯度的**明显错误**（量级、符号、维度），前向差分虽精度低但每次只需 `n+1` 次求值，成本最低；要更高精度校验应直接用内部的 `approx_derivative(method='cs')` 或 `check_derivative`。

**练习 2**：`direction='random'` 为何对**向量值**函数无效？
**答案**：方向导数 \( \nabla f \cdot v \) 只对标量 \( f \) 定义；向量函数的雅可比与方向向量的乘积是 \( Jv \)，无法用一个标量方向导数覆盖，故代码在 [:L1122-1124](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L1122-L1124) 显式拒绝 `_grad.ndim > 1`。

---

## 5. 综合实践

把本讲的「步长/边界」「稠密 vs 稀疏」「公开接口」串起来。

**任务**：给定一个带边界、雅可比稀疏的向量函数，分别用三种差分格式与稀疏分组估计雅可比，对比精度与函数求值次数；最后用 `check_derivative` 给出相对误差度量。

```python
# 示例代码
import numpy as np
from scipy.optimize import rosen, rosen_der
from scipy.optimize._numdiff import (
    approx_derivative, group_columns, check_derivative,
)

# ---- Part A：含边界的标量函数 rosen，比较三种 method ----
x0 = 0.1 * np.arange(8)
lb = np.full_like(x0, -1.0)
ub = np.full_like(x0,  3.0)
bounds = (lb, ub)

print("== rosen 梯度估计（含边界）==")
g_true = rosen_der(x0)
for method in ['2-point', '3-point', 'cs']:
    J, info = approx_derivative(rosen, x0, method=method,
                                bounds=bounds, full_output=True)
    print(f"{method:8s} nfev={info['nfev']:3d}  err={np.abs(J-g_true).max():.2e}")

# ---- Part B：稀疏雅可比，用 group_columns 减少求值 ----
# 每个输出只依赖 2~3 个相邻输入 → 三对角结构
n = 6
def f_tri(x):
    y = np.empty(n)
    y[0]   = x[0] + x[1]
    y[1:-1]= x[:-2] + x[1:-1] + x[2:]      # 中间输出依赖 3 个相邻输入
    y[-1]  = x[-2] + x[-1]
    return y

# 构造三对角稀疏结构
struct = np.zeros((n, n), dtype=np.int32)
for i in range(n):
    for j in (i-1, i, i+1):
        if 0 <= j < n:
            struct[i, j] = 1

groups = group_columns(struct, order=0)
print("\n== 稀疏雅可比分组 ==")
print("groups =", groups, " n_groups =", groups.max()+1, "（稠密需", n, "组）")

x0b = np.arange(1.0, n+1.0)
Jd, idd = approx_derivative(f_tri, x0b, full_output=True)
Js, iss_ = approx_derivative(f_tri, x0b, sparsity=struct, full_output=True)
print(f"dense  nfev={idd['nfev']}   sparse nfev={iss_['nfev']}")
print("两种雅可比一致？", np.allclose(np.asarray(Js.todense()), Jd))

# ---- Part C：用 check_derivative 做相对误差度量 ----
def jac_tri(x):
    J = np.zeros((n, n))
    for i in range(n):
        for j in (i-1, i, i+1):
            if 0 <= j < n:
                J[i, j] = 1.0
    return J
print("\ncheck_derivative 相对误差 =", check_derivative(f_tri, jac_tri, x0b))
```

**操作要点**：

1. Part A 通过 `bounds` 强制 `_adjust_scheme_to_bounds` 生效，注意 `cs` 模式不受边界方向影响（虚部扰动）。
2. Part B 先 `group_columns` 看分组数（三对角结构理论最小分组数为 3），再对比稠密/稀疏 `nfev`。
3. Part C 用内部 `check_derivative` 得到一个统一的相对误差标量（[:L990-991](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_numdiff.py#L990-L991) 的 `max(|err|/max(1,|J|))`）。

**预期结果**：`cs` 的误差最小（`1e-15` 量级）；稀疏 `nfev` 约为稠密的 `n_groups/n`；`check_derivative` 返回值在 `1e-10` 量级（精确数值「待本地验证」）。

## 6. 本讲小结

- `approx_derivative` 是 `scipy.optimize` **所有**有限差分求导的唯一汇聚点，支持 `2-point`/`3-point`/`cs` 三种格式，默认 `3-point`；它本身是编排器，真正算术交给 `_dense_difference`/`_sparse_difference`/`_linear_operator_difference`。
- 最优相对步长为 \( \mathrm{EPS}^{1/s} \)（`s=2` 对应前向/复步，`s=3` 对应中心），由 `_eps_for_method` 按 `x0`/`f0` 中较小浮点类型决定，并由 `_compute_absolute_step` 转成绝对步长。
- `_adjust_scheme_to_bounds` 在变量有界时自动翻号或退化为中心/单侧二阶差分，保证扰动点不越界；无界时走快路径直接返回。
- `_dense_difference` 逐列扰动（\( n \) 或 \( 2n \) 次求值），`_sparse_difference` 按 `group_columns` 的分组同时扰动多列，把稀疏雅可比的求值次数降到「分组数」。
- `group_columns` 是 Curtis–Powell–Reid 贪心图着色，由 Pythran 加速的 `group_dense`/`group_sparse` 实现；列顺序影响分组数，故默认用可复现随机置换。
- 公开接口 `approx_fprime`（前向差分）与 `check_grad`（校验解析梯度）都薄封装到 `approx_derivative`，是日常写单元测试、核对梯度时的入口。

## 7. 下一步学习建议

- 本讲的 `approx_derivative` 是下一讲 **u3-l2「ScalarFunction 与 VectorFunction」** 的直接基石：那两个类正是把 `fun` + `jac`（或 `jac='2-point'` 等）+ `approx_derivative` 统一封装成「带缓存的求值对象」，建议接着读 [`_differentiable_functions.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_differentiable_functions.py)，重点看 `ScalarFunction` 如何在多次 `x` 之间缓存 `f`/`grad`/`hess`。
- 若对复步差分的精度优势感兴趣，可对照 `_dense_difference` 的 `cs` 分支与 `np.finfo` 的 EPS，亲手画出「误差 vs 步长」曲线，直观验证 \( \mathrm{EPS}^{1/s} \) 为何是最优点。
- 想深入稀疏雅可比的读者可继续阅读 Curtis–Powell–Reid 1974 原文，并尝试给 `group_columns` 传入不同 `order`，观察分组数变化对 `_sparse_difference` 的 `nfev` 影响。
