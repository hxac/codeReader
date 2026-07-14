# Vandermonde 矩阵与最小二乘拟合

## 1. 本讲目标

本讲承接「求值与 Horner 法」(u3-l3)，把「给定系数求多项式值」这件事反过来：**给定一组采样点 \((x_i, y_i)\)，反求最贴合的多项式系数**。

读完本讲，你应当能够：

- 说清楚**范德蒙矩阵 (Vandermonde matrix)** 的构造 \(V[\ldots,i]=x^i\)，并能解释为什么 `np.dot(V, c)` 与 `polyval(x, c)` 结果一致。
- 看懂 `polyfit` 如何退化为「构造加权范德蒙矩阵 → 求解超定方程组」的过程，以及它对 `pu._fit` 的薄委托。
- 理解 `_fit` 中两步关键工程手段：**按列归一化**改善条件数、**`rcond` 截断奇异值**控制数值秩。
- 解释 `deg` 既可传一个整数、也可传一个度数列表（如 `[0, 2, 3]`）这两种用法的含义。
- 自己用 `polyvander` + `np.linalg.lstsq` 手工复现一次 `polyfit`。

## 2. 前置知识

### 2.1 从「求值」到「拟合」是一次视角翻转

上一讲我们解决了正向问题：**已知系数 \(c\)，求 \(p(x)\)**。Horner 法把这件事做得很高效。本讲解决逆向问题：**已知一堆 \((x_i, y_i)\)，反推 \(c\)**。

关键洞察是：求值本质是一个**矩阵—向量乘法**。把采样点 \(x\) 排成范德蒙矩阵 \(V\)（第 \(i\) 列是 \(x^i\)），那么求值就是

\[
p(x) = V \cdot c.
\]

而拟合，就是把上式反过来——已知 \(V\) 和 \(p(x)=y\)，**解关于 \(c\) 的线性方程组**。这就是为什么范德蒙矩阵会同时出现在求值与拟合两个场景里（`polyvander` 的文档字符串明确点出了这一点）。

### 2.2 最小二乘与正规方程

当数据点个数 \(M\) 大于系数个数 \(n+1\) 时，方程组 \(V c = y\) **超定**，通常没有精确解。最小二乘法寻找让残差平方和最小的 \(c\)：

\[
\min_c \sum_j w_j^2\,|y_j - p(x_j)|^2.
\]

经典的解析解是**正规方程** \(V^\top V\, c = V^\top y\)，但直接解正规方程会**平方化条件数**（条件数从 \(\kappa(V)\) 变成 \(\kappa(V)^2\)），数值上很脆弱。所以 NumPy 不走这条路，而是用**奇异值分解 (SVD)** 求解，下面会看到。

### 2.3 术语回顾

- **范德蒙矩阵 (Vandermonde matrix)**：每列是自变量的某次幂。
- **超定方程组 (over-determined system)**：方程数多于未知数。
- **条件数 (condition number)**：最大奇异值 / 最小奇异值，衡量矩阵「接近奇异」的程度；越大越病态。
- **奇异值分解 (SVD)**：把矩阵分解为 \(A = U\Sigma V^\top\)，是求解最小二乘最稳定的工具。
- 上一讲的术语继续生效：Horner 法、`polyval`、`domain`/`window` 与 `mapparms`。

## 3. 本讲源码地图

本讲只动两个文件，外加便捷类外壳的一个方法：

| 文件 | 本讲涉及的关键函数 | 作用 |
| --- | --- | --- |
| `polynomial.py` | `polyvander` / `polyvander2d` / `polyvander3d` / `polyfit` | 构造幂基范德蒙矩阵；幂级数最小二乘拟合的公开入口 |
| `polyutils.py` | `_vander_nd` / `_vander_nd_flat` / `_nth_slice` / `_fit` | 多维范德蒙的通用拼接引擎；六大基共享的拟合实现 |
| `_polybase.py` | `ABCPolyBase.fit` | 便捷类拟合：先做 `domain→window` 坐标映射，再委托 `_fit` |

回顾委托链（u1-l4、u2-l1 已建立）：**便捷类 `_polybase.py` → 函数式 API `polynomial.py` → 通用工具 `polyutils.py`**。本讲的 `polyfit` 与 `polyvander` 正好坐落在这条链的中段与底端。

## 4. 核心概念与源码讲解

### 4.1 polyvander 构造：把求值写成矩阵乘法

#### 4.1.1 概念说明

范德蒙矩阵的定义非常简单：给定采样点 \(x\) 和次数 `deg`，

\[
V[\ldots, i] = x^i, \quad 0 \le i \le \text{deg}.
\]

也就是「第 \(i\) 列就是 \(x\) 的 \(i\) 次幂」。它的形状是 `x.shape + (deg+1,)`——前面的轴索引采样点，**最后一根轴索引幂次**。

它最大的价值是一条等价关系：若 \(c\) 是长度为 `deg+1` 的系数数组（低次在前），\(V = \text{polyvander}(x, \text{deg})\)，那么

\[
\text{np.dot}(V, c) \;\equiv\; \text{polyval}(x, c).
\]

这条等式是本讲的命脉：它把「求值」与「拟合」焊接在一起。**求值**是已知 \(c\) 算 \(Vc\)；**拟合**是已知 \(V\) 和 \(y\) 解 \(Vc \approx y\)。同一张矩阵，两个方向。

#### 4.1.2 核心流程

`polyvander` 的实现思路和上一讲 Horner 法的精神一脉相承——**不重复计算高次幂，而是递推**：

1. 把 `x` 转成至少 1 维的浮点数组（`+ 0.0`）。
2. 预分配结果 `v`，形状为 `(deg+1,) + x.shape`，即**幂次轴在最前**。
3. `v[0] = 1`（零次幂全 1）；若 `deg > 0`，`v[1] = x`，之后 `v[i] = v[i-1] * x` 递推。
4. 最后用 `np.moveaxis(v, 0, -1)` 把幂次轴从最前搬到**最后**，符合「最后一根轴是幂次」的约定。

递推 `v[i] = v[i-1] * x` 的好处：复用上一列的结果，避免对每个点独立算 `x**i`，既省算量也数值更稳（与 Horner 法同样的哲学）。

#### 4.1.3 源码精读

公开入口 [`polyvander`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L1128-L1192)（[polynomial.py:L1128-L1192](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L1128-L1192)）。关键片段：

```python
ideg = pu._as_int(deg, "deg")
if ideg < 0:
    raise ValueError("deg must be non-negative")

x = np.array(x, copy=None, ndmin=1) + 0.0   # 1-D 浮点化
dims = (ideg + 1,) + x.shape
v = np.empty(dims, dtype=x.dtype)
v[0] = x * 0 + 1                             # 第 0 列：全 1
if ideg > 0:
    v[1] = x                                 # 第 1 列：x
    for i in range(2, ideg + 1):
        v[i] = v[i - 1] * x                  # 递推：复用上一列
return np.moveaxis(v, 0, -1)                 # 幂次轴搬到末尾
```

注意 `v[0] = x * 0 + 1` 而不是直接写 `1`——这是为了把形状与 dtype 一次性对齐到 `x`（与上一讲 `polyval` 中 `c[-1] + x*0` 是同一招）。`pu._as_int` 是 [polyutils.py:L703](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L703) 附近定义的整型校验工具，把 `deg` 强制成 `int` 并给出可读报错。

文档字符串里那个例子的输出最直观：

```
>>> P.polyvander([-1, 2, 3], 5)
array([[  1.,  -1.,   1.,  -1.,   1.,  -1.],   # x=-1 的 0..5 次幂
       [  1.,   2.,   4.,   8.,  16.,  32.],   # x= 2 的 0..5 次幂
       [  1.,   3.,   9.,  27.,  81., 243.]])  # x= 3 的 0..5 次幂
```

每一行是一个采样点，每一列是一次幂。

#### 4.1.4 代码实践

**实践目标**：亲眼验证 `np.dot(V, c) == polyval(x, c)`。

操作步骤：

```python
import numpy as np
from numpy.polynomial import polynomial as P

x = np.array([-1., 0.5, 2., 3.])
c = np.array([1., -2., 0.5, 3.])     # p(x) = 1 - 2x + 0.5x^2 + 3x^3

V = P.polyvander(x, len(c) - 1)
lhs = V @ c                          # 矩阵—向量乘法
rhs = P.polyval(x, c)                # Horner 求值
```

需要观察的现象与预期结果：`lhs` 与 `rhs` 应在浮点舍入误差内完全相等（`np.allclose(lhs, rhs)` 为 `True`）。这就是「求值 = 范德蒙矩阵乘系数」的实证。把 `c` 改成更高次、把 `x` 改成 2 维数组，等式依然成立——后者正是多维求值的基础。

#### 4.1.5 小练习与答案

**练习 1**：为什么不直接写 `v[i] = x ** i`，而要用 `v[i] = v[i-1] * x` 递推？

**参考答案**：递推复用了上一列的结果，每个点只需一次乘法；直接算 `x ** i` 要么内部重新做幂运算、要么丢失中间结果无法复用。更重要的是，递推与 Horner 法同源，避免单独计算高次幂，数值上更稳定。

**练习 2**：`polyvander` 返回矩阵的**最后一根轴**为什么必须是幂次轴，而不是第一根轴？

**参考答案**：为了与 `np.dot(V, c)` 的语义对齐——`dot` 会把 `V` 的最后一根轴与 `c` 的轴 contracted。幂次轴放最后，才能让「矩阵乘系数」自然等于求值；也正是这个布局，让 `V` 可直接作为最小二乘的设计矩阵。

---

### 4.2 _vander_nd / _vander_nd_flat：多维伪范德蒙的拼接

#### 4.2.1 概念说明

很多场景有多元多项式 \(p(x, y) = \sum_{i,j} c_{ij} x^i y^j\)。把一维范德蒙推广到多维，得到**伪范德蒙矩阵 (pseudo-Vandermonde)**：

\[
V[\ldots, (d_y+1)\,i + j] = x^i \cdot y^j, \quad 0 \le i \le d_x,\ 0 \le j \le d_y.
\]

最后一根轴把所有 \((i, j)\) 组合**拍平 (flatten)** 成一列；列的排列顺序是「\(y\) 的幂变得最快，\(x\) 最慢」，恰好对应系数数组 `c` 按行优先 `c.flat` 展开的顺序。于是多元情形也成立等价关系：

\[
\text{np.dot}(V, c.\text{flat}) \;\equiv\; \text{polyval2d}(x, y, c).
\]

#### 4.2.2 核心流程

通用引擎 `_vander_nd`（定义在 `polyutils.py`）的做法是**张量积拼接**：

1. 对每个维度 \(k\)，调用对应的一维 vander 函数 \(V_k = \text{vander\_fs[k]}(\text{points[k]}, \text{degrees[k]})\)。
2. 用 `_nth_slice(i, n)` 给每个 \(V_k\) 的幂次轴插一根**独立的尾随轴**（第 \(k\) 维的幂次占倒数第 \(n-k\) 根轴），让各维的幂次轴互不挤占。
3. 把所有 \(V_k\) **逐元素相乘**（`funct.reduce(operator.mul, ...)`），得到形状 `points[0].shape + (d_0+1, d_1+1, ..., d_{n-1}+1)` 的张量——这正好是多元基函数的乘积 \(x^i y^j z^k\)。

`_vander_nd_flat` 再做最后一步：把末尾 \(n\) 根幂次轴 **reshape 成一根**（`v.reshape(v.shape[:-len(degrees)] + (-1,))`），得到公开 API 期望的「点轴 + 单根列轴」布局。

#### 4.2.3 源码精读

切片工具 [`_nth_slice`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L358-L361)（[polyutils.py:L358-L361](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L358-L361)）：为第 \(i\) 个维度构造一个切片对象，让该维的幂次轴落在尾随第 \(i\) 根位置。

```python
def _nth_slice(i, ndim):
    sl = [np.newaxis] * ndim
    sl[i] = slice(None)
    return tuple(sl)
```

通用拼接引擎 [`_vander_nd`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L364-L430)（[polyutils.py:L364-L430](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L364-L430)），核心两步：

```python
# 每个维度算一维 vander，把它的幂次轴摆到独立的尾随位置
vander_arrays = (
    vander_fs[i](points[i], degrees[i])[(...,) + _nth_slice(i, n_dims)]
    for i in range(n_dims)
)
# 逐元素相乘 → 多元基函数的乘积（张量积）
return functools.reduce(operator.mul, vander_arrays)
```

拍平封装 [`_vander_nd_flat`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L433-L440)（[polyutils.py:L433-L440](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L433-L440)）：

```python
v = _vander_nd(vander_fs, points, degrees)
return v.reshape(v.shape[:-len(degrees)] + (-1,))
```

公开 API 只是一行委托：[`polyvander2d`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L1195-L1271) 与 [`polyvander3d`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L1274-L1345)（[polynomial.py:L1271](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L1271)、[polynomial.py:L1345](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L1345)）：

```python
def polyvander2d(x, y, deg):
    return pu._vander_nd_flat((polyvander, polyvander), (x, y), deg)

def polyvander3d(x, y, z, deg):
    return pu._vander_nd_flat((polyvander, polyvander, polyvander), (x, y, z), deg)
```

注意它把**同一个 `polyvander` 函数对象**作为元组传入——这正是「虚函数注入」模式（u2-l1）：`_vander_nd` 不关心用哪种基，调用方注入哪个一维 vander 函数，它就拼接哪个族。Chebyshev 的 `chebvander2d` 注入的会是 `chebvander`，其余完全相同。

#### 4.2.4 代码实践

**实践目标**：验证 `polyvander2d` 的列编排与 `polyval2d` 的等价关系。

操作步骤：

```python
import numpy as np
from numpy.polynomial import polynomial as P

x = np.array([-1., 2.])
y = np.array([1., 3.])
deg = np.array([1, 2])                 # x 最高 1 次，y 最高 2 次

V = P.polyvander2d(x, y, deg)          # 形状 (2, 6)
# 验证第 i=0,j=1 列等于 x**0 * y**1
i, j = 0, 1
col = (deg[1] + 1) * i + j
print(np.all(V[:, col] == x**i * y**j))  # 应为 True

# 等价关系：dot(V, c.flat) == polyval2d
c = np.arange(6).reshape(deg[0] + 1, deg[1] + 1).astype(float)
print(np.allclose(V @ c.flat, P.polyval2d(x, y, c)))  # 应为 True
```

需要观察的现象与预期结果：两处断言均为 `True`。第二处尤其重要——它说明多元拟合同样可以写成 `V @ c.flat ≈ y`，从而复用同一套最小二乘求解器。

> **待本地验证**：列索引公式 `(deg[1]+1)*i + j` 的正确性，建议你手算 \(x=[-1,2], y=[1,3]\) 时第 3 列（\(i=1, j=0\)，即 \(x^1 y^0 = x\)）应为 `[-1, 2]`，与输出对照。

#### 4.2.5 小练习与答案

**练习 1**：`_vander_nd` 为什么用 `functools.reduce(operator.mul, ...)` 而不是 `np.dot` 或循环累加？

**参考答案**：因为多元基函数 \(x^i y^j\) 是各维一维基函数的**乘积**，需要在逐元素意义上相乘，并把各维独立的幂次轴通过广播组合成张量积。`operator.mul` 配合 `reduce` 正好是「把一串数组逐元素乘起来」；`np.dot` 会做缩并求和，语义错误。

**练习 2**：`polyvander2d(x, y, [m, 0])`（即 \(y\) 最高 0 次）应该退化成什么？

**参考答案**：退化成一维的 `polyvander(x, m)`。因为 \(y^0 = 1\)，所有含 \(y\) 的列都乘以 1，等价于只在 \(x\) 上做范德蒙。源码文档里也直接给了这条等价断言。

---

### 4.3 polyfit 与 _fit 流程：把拟合变成解方程

#### 4.3.1 概念说明

`polyfit` 是幂级数最小二乘拟合的公开入口，但它的函数体只有一行——把活全交给 `pu._fit`：

```python
return pu._fit(polyvander, x, y, deg, rcond, full, w)
```

这是「能复用就复用」的又一次体现：`_fit` 是六大基共享的通用拟合引擎，谁把自己的 vander 函数（`polyvander` / `chebvander` / …）注入进来，就按谁的基去拟合。

`_fit` 的核心思想是把拟合**翻译成一个加权超定线性方程组**，再用 SVD 求解。带权重时，问题等价于：

\[
V(x) \cdot c = w \cdot y,
\]

其中 \(V\) 是加权伪范德蒙矩阵（每一行乘以 \(w_j\)），\(c\) 是待求系数，\(w \cdot y\) 是加权观测值。文档字符串里写得很清楚：拟合最小化的是加权和 \(E = \sum_j w_j^2 |y_j - p(x_j)|^2\)。

#### 4.3.2 核心流程

`_fit` 的执行步骤（建议对照源码读）：

1. **参数校验**：`deg` 必须是整数或非空整数一维数组且最小值非负；`x` 必须是 1 维非空；`y` 只能 1 维或 2 维（2 维时每列做一次独立拟合）；`x` 与 `y` 长度必须一致。
2. **构造设计矩阵 `van`**（关键分支，见 4.3.3）：
   - `deg` 是标量：`lmax = deg`，`order = lmax + 1`，`van = vander_f(x, lmax)`（全列）。
   - `deg` 是列表：排序后取 `lmax = max(deg)`，但只**挑选指定幂次的列** `van = vander_f(x, lmax)[:, deg]`。
3. **转置成 lstsq 习惯的形式**：`lhs = van.T`，`rhs = y.T`。
4. **施加权重**：若有 `w`，则 `lhs *= w`、`rhs *= w`（注释特意说明不用原地操作，避免 NA 问题）。
5. **设定 `rcond`**：默认 `len(x) * eps`（`eps` 是浮点相对精度，约 2e-16）。
6. **按列归一化**（4.4 节详解）：算每列 2-范数 `scl`。
7. **求解**：`np.linalg.lstsq(lhs.T / scl, rhs.T, rcond)`。
8. **回解除归一化**：`c = (c.T / scl).T`。
9. **零填充**：若 `deg` 是列表，把未参与拟合的幂次位置补 0。
10. **秩不足告警**：若数值秩 `< order` 且 `full=False`，抛 `RankWarning`。
11. **返回**：`full=False` 返回 `c`；`full=True` 返回 `(c, [resids, rank, s, rcond])`。

#### 4.3.3 源码精读

公开入口 [`polyfit`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L1348-L1499)（[polynomial.py:L1348-L1499](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L1348-L1499)），委托行在 [polynomial.py:L1499](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L1499)。文档串里的 Notes 给出了拟合的数学表述 \(E = \sum_j w_j^2 |y_j - p(x_j)|^2\) 与方程 \(V(x) c = w y\)。

通用拟合引擎 [`_fit`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L582-L667)（[polyutils.py:L582-L667](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L582-L667)）。`deg` 两种用法的分支：

```python
if deg.ndim == 0:
    lmax = deg
    order = lmax + 1
    van = vander_f(x, lmax)                 # 全部 0..lmax 列
else:
    deg = np.sort(deg)
    lmax = deg[-1]
    order = len(deg)
    van = vander_f(x, lmax)[:, deg]         # 只挑指定幂次的列
```

`deg` 为整数时，拟合包含所有 \(0 \ldots \text{deg}\) 次项（最常见的用法）；`deg` 为列表时，只拟合列出的幂次，其余在最后被零填充——这让你能拟合诸如「只含偶次项」的多项式：

```python
if deg.ndim > 0:
    ...
    cc[deg] = c        # 把拟合结果填回指定幂次，其余保持 0
    c = cc
```

便捷类外壳 [`ABCPolyBase.fit`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L946-L1031)（[_polybase.py:L946-L1031](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L946-L1031)）在做委托之前先处理坐标映射（u2-l2 已讲）：

```python
if domain is None:
    domain = pu.getdomain(x)            # 自动取数据范围作 domain
    ...
xnew = pu.mapdomain(x, domain, window) # domain → window 映射
res = cls._fit(xnew, y, deg, w=w, rcond=rcond, full=full)  # 委托 _fit
```

这解释了 u1-l3 的结论：便捷类 `Polynomial.fit` 先把数据映射到 `window`（如 \([-1,1]\)），再调 `_fit`，所以返回的系数是 **window 变量下的**。这正是自动 domain 改善数值稳定性的落点（u5-l1 会展开）。

#### 4.3.4 代码实践

**实践目标**：用 `deg` 列表拟合「只含偶次项」的多项式，体会 `van[:, deg]` 选列的效果。

操作步骤：

```python
import numpy as np
from numpy.polynomial import polynomial as P

x = np.linspace(-1, 1, 51)
y = 2.0 + 3.0 * x**2 + 5.0 * x**4     # 纯偶次多项式

# 只拟合 0,2,4 次项
c_even = P.polyfit(x, y, [0, 2, 4])
print(c_even)                          # 期望 [2, 0, 3, 0, 5]
```

需要观察的现象与预期结果：返回长度为 5 的数组（`lmax+1 = 5`），其中第 0、2、4 位约为 `2, 3, 5`，第 1、3 位严格为 `0`（未拟合项被零填充）。对比 `P.polyfit(x, y, 4)`（整数 deg，允许奇次项），后者奇次项系数会因数值噪声出现微小非零值。**待本地验证**：偶次项数值与真实值的吻合精度。

#### 4.3.5 小练习与答案

**练习 1**：传 `deg=[0, 2, 3]` 时，`_fit` 内部构造的 `van` 有几列？`order` 等于多少？

**参考答案**：`van = vander_f(x, lmax)[:, deg]`，`lmax = max([0,2,3]) = 3`，先构造 4 列（0..3 次）的完整范德蒙，再切片取 `[0, 2, 3]` 这 3 列，所以 `van` 有 3 列，`order = len(deg) = 3`。最终返回长度为 `lmax+1 = 4` 的系数，第 1 次项位置补 0。

**练习 2**：为什么便捷类 `Polynomial.fit` 要在调 `_fit` 之前先 `mapdomain(x, domain, window)`？

**参考答案**：因为系数始终按 `window` 变量表达（u2-l2 的双区间约定）。把数据点先从用户 `domain` 映射到 `window`，`_fit` 直接在 `window` 变量上构造范德蒙并求解，得到的系数才能正确配在 `window` 下；随后用 `p(x)`（它内部再做一次 domain→window 映射）求值才自洽。同时映射到 \([-1,1]\) 这类紧凑区间能显著改善范德蒙矩阵的条件数。

---

### 4.4 列归一化与 lstsq：让条件数不再失控

#### 4.4.1 概念说明

幂基范德蒙矩阵有一个著名的毛病：**各列量级天差地别**。当 \(x\) 的范围稍大（比如 \([0,10]\)）或次数稍高（比如 10 次），\(x^{10}\) 那一列的元素可能比 \(x^0\) 那列大十几个数量级。这会让矩阵**病态**——条件数极大，SVD 求出的系数被舍入误差严重污染（文档注释里说「双精度幂级数拟合大约在 20 次处失效」）。

`_fit` 用两招对付它：

1. **按列归一化 (column scaling)**：把每列除以自身的 2-范数，让所有列「等长」。这不改变列空间（因而不改变最小二乘解），但把条件数从「量级悬殊 + 角度接近」压成「纯角度」，通常能降好几个数量级。
2. **`rcond` 截断奇异值**：把小于「最大奇异值 × `rcond`」的奇异值当作 0 丢弃（截断 SVD），防止那些纯噪声方向污染解，并据此报告数值秩。

#### 4.4.2 核心流程

设加权设计矩阵为 \(A\)（即代码里的 `lhs.T`，形状 \(M \times \text{order}\)），观测为 \(b\)。列归一化相当于令对角阵

\[
D = \mathrm{diag}(s_1, \ldots, s_n), \quad s_i = \|A_{:,i}\|_2,
\]

然后求解**等价问题**

\[
\min_{c'} \big\| A D^{-1}\, c' - b \big\|_2, \qquad c' = D\,c,
\]

最后回除得到 \(c = D^{-1} c'\)（即每位系数除以对应列范数 \(s_i\)）。因为 \(A D^{-1}\) 的每列都是单位长度，条件数只反映列与列之间的「角度」关系，不再被量级绑架。

`rcond` 的作用：`np.linalg.lstsq` 内部对 \(A D^{-1}\) 做 SVD，凡是奇异值 \(\sigma < \sigma_{\max} \cdot \text{rcond}\) 的方向，都被视为数值零、不参与求解。默认 `rcond = len(x) * eps`。被截断的奇异值个数决定 `rank`；若 `rank < order` 说明矩阵不满秩，拟合结果可能不可靠。

#### 4.4.3 源码精读

列归一化与求解的关键段落 [`_fit` 内部](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L635-L648)（[polyutils.py:L635-L648](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L635-L648)）：

```python
# set rcond
if rcond is None:
    rcond = len(x) * np.finfo(x.dtype).eps

# 每列的 2-范数（复数时分别取实虚部平方和）
if issubclass(lhs.dtype.type, np.complexfloating):
    scl = np.sqrt((np.square(lhs.real) + np.square(lhs.imag)).sum(1))
else:
    scl = np.sqrt(np.square(lhs).sum(1))
scl[scl == 0] = 1                       # 防止除以 0（全零列）

# 在归一化后的矩阵上求解，再回除
c, resids, rank, s = np.linalg.lstsq(lhs.T / scl, rhs.T, rcond)
c = (c.T / scl).T                       # 撤销归一化：c' / scl → c
```

注意几个细节：

- `scl` 沿 `axis=1` 求和——因为此时 `lhs = van.T`，行对应原矩阵的**列**，所以 `scl` 正是设计矩阵各列的 2-范数。
- `scl[scl == 0] = 1` 是护栏：若某列全零（理论上范德蒙不会，但 `deg` 列表选列或权重退化时可能触发），避免除零。
- 复数情形用 \(|\cdot|^2 = \text{Re}^2 + \text{Im}^2\)，而非 `np.abs(...)**2`，避免多余的平方根开销。

秩不足告警 [`_fit` 末尾](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L659-L662)（[polyutils.py:L659-L662](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L659-L662)）：

```python
if rank != order and not full:
    msg = "The fit may be poorly conditioned"
    warnings.warn(msg, np.exceptions.RankWarning, stacklevel=2)
```

只有 `full=False` 时才告警（`full=True` 时调用方自己会看 `rank`，不需要噪声）。

#### 4.4.4 代码实践

**实践目标**：手工用 `polyvander + np.linalg.lstsq` 复现 `polyfit`，并对比「做列归一化」与「不做列归一化」的差异。

操作步骤：

```python
import numpy as np
from numpy.polynomial import polynomial as P

x = np.linspace(-1, 1, 51)
y = x**3 - x
deg = 3
rcond = len(x) * np.finfo(float).eps

# ① polyfit 标准结果
c_fit = P.polyfit(x, y, deg)

# ② 手工复现（带列归一化，与 _fit 一致）
V = P.polyvander(x, deg)
scl = np.sqrt((V**2).sum(0)); scl[scl == 0] = 1
c_scaled, *_ = np.linalg.lstsq(V / scl, y[:, None], rcond=rcond)
c_manual = (c_scaled / scl).ravel()

print("带归一化 系数差:", np.abs(c_fit - c_manual).max())   # 应极小

# ③ 不做列归一化，直接 lstsq
c_raw, res_raw, rank_raw, sv_raw = np.linalg.lstsq(V, y[:, None], rcond=rcond)
print("无归一化 系数差:", np.abs(c_fit - c_raw.ravel()).max())
print("归一化 最小奇异值:", np.linalg.svd(V / scl, compute_uv=False).min())
print("原始   最小奇异值:", sv_raw.min())
```

需要观察的现象与预期结果：

- 低次数（`deg=3`）下，三条系数几乎一致（差异在 1e-12 量级）——因为问题本身良态，归一化与否数学上同解。
- 把 `deg` 提高到 12 以上，或把 `x` 范围换成 `np.linspace(0, 10, 51)`，**不做归一化的系数会明显偏离**，甚至出现乱序大数；而带归一化的结果仍稳定。
- 原始矩阵的最小奇异值往往极小（接近机器精度），这正是「病态」的直接证据；归一化后条件数通常改善若干数量级。

> **关于「残差差异」的诚实结论**：列归一化**不改变最小二乘解的数学值**，所以良态情形下残差几乎相同；它的真正收益在**条件数与高次/远区间下的数值精度**。若你观察到低次时残差完全一致，那是正确的，不要误以为归一化「没起作用」。

> **待本地验证**：在 `deg=15`、`x` 取 \([0,10]\) 时，原始 `lstsq` 与归一化版本系数差异的具体量级。

#### 4.4.5 小练习与答案

**练习 1**：列归一化为什么「不改变最小二乘解的数学值」？

**参考答案**：因为把矩阵 \(A\) 换成 \(A D^{-1}\)、未知量换成 \(c' = Dc\)，只是对列空间做了一组可逆的列缩放，列空间本身（因而投影、残差、最优解对应的 \(p(x)\)）不变。求出 \(c'\) 后再除以 \(s_i\) 还原回 \(c\)，得到的还是同一个最小二乘解。改变的是数值求解过程的稳定性。

**练习 2**：把 `rcond` 调大（比如从默认的 `len(x)*eps` 调到 `1e-2`）会怎样？

**参考答案**：`rcond` 越大，被判为「零」的奇异值越多，数值秩 `rank` 下降，更多高频方向被丢弃。好处是抑制噪声方向、结果更平滑；坏处是可能丢掉真实信号、欠拟合，并更易触发 `RankWarning`。文档特别警告：把 `rcond` 调小（更激进地保留奇异值）可能得到「假拟合」，系数被舍入误差主导。

---

## 5. 综合实践

把本讲四块知识串起来：**从构造范德蒙、到手工拟合、再到与便捷类对照**。

任务：对带噪声的正弦信号做幂级数拟合，并比较三种求解路径。

```python
import numpy as np
from numpy.polynomial import polynomial as P
from numpy.polynomial import Polynomial

# 1) 造数据：sin(x) + 噪声，x 在 [-1, 1]
rng = np.random.default_rng(0)
x = np.linspace(-1, 1, 80)
y = np.sin(x) + 0.02 * rng.normal(size=x.size)
deg = 5

# 路径 A：函数式 polyfit（内部走 _fit，带列归一化）
cA = P.polyfit(x, y, deg)

# 路径 B：手工 polyvander + lstsq（自行做列归一化）
V = P.polyvander(x, deg)
scl = np.sqrt((V**2).sum(0)); scl[scl == 0] = 1
cB = (np.linalg.lstsq(V / scl, y[:, None],
                      rcond=len(x)*np.finfo(float).eps)[0] / scl).ravel()

# 路径 C：便捷类 Polynomial.fit（自动 domain→window 映射，再委托 _fit）
p = Polynomial.fit(x, y, deg)
cC_window = p.coef                       # window 变量下的系数
cC = p.convert().coef                    # 转回标准基（可与 cA 直接比）

print("A vs B 最大系数差:", np.abs(cA - cB).max())   # 应极小
print("A vs C 最大系数差:", np.abs(cA - cC).max())   # 应极小

# 4) 用范德蒙等价关系核验拟合值
y_hat = P.polyvander(x, deg) @ cA        # 等价于 P.polyval(x, cA)
print("拟合残差 SSR:", np.sum((y - y_hat)**2))
```

**思考要点**（建议在注释里写下你的观察）：

1. 路径 A 与 B 系数差为何远小于路径 A 与「不做列归一化的 lstsq」之差？回到 4.4 的结论。
2. 路径 C 里 `p.coef` 与 `p.convert().coef` 为什么不同？回顾 u2-l2 的 domain/window 映射、u1-l3 的「`fit` 返回 window 系数」。
3. 若把 `deg` 提高到 12，三条路径是否仍吻合？`RankWarning` 是否出现？用 `full=True` 看 `rank` 与奇异值。

> **待本地验证**：上述三条系数差的具体数值；高次时 `RankWarning` 触发的临界 `deg`。

## 6. 本讲小结

- **范德蒙矩阵** `V[...,i]=x^i` 是连接求值与拟合的枢纽：`np.dot(V, c) ≡ polyval(x, c)`。`polyvander` 用递推 `v[i]=v[i-1]*x` 复用上一列、避免重复求幂，最后 `moveaxis` 把幂次轴摆到末尾。
- **多维伪范德蒙** 由通用引擎 `_vander_nd`（张量积逐元素相乘）拼接、`_vander_nd_flat` 拍平末尾幂次轴而成；公开的 `polyvander2d/3d` 只是把一维 `polyvander` 注入引擎的一行委托。
- **`polyfit` 是 `pu._fit(polyvander, ...)` 的薄委托**，`_fit` 是六大基共享的拟合引擎，把问题翻译成加权超定方程组 \(V c = w y\) 后用 SVD 求解。
- **`deg` 有两种语义**：整数表示拟合全部 \(0 \ldots \text{deg}\) 次项；列表表示只拟合指定幂次，未拟合项被零填充。
- **列归一化**（每列除以自身 2-范数）与 **`rcond` 截断奇异值** 是两道数值护栏：前者把条件数从「量级悬殊」压成「纯角度」，后者丢弃噪声方向并报告数值秩，`rank < order` 时发 `RankWarning`。
- 便捷类 `Polynomial.fit` 在委托 `_fit` 之前先做 `domain→window` 映射，所以返回系数是 window 变量下的，需 `p(x)` 求值或 `.convert()` 取标准基系数。

## 7. 下一步学习建议

- **横向迁移到正交基**：下一单元 u4-l1 会讲 Chebyshev 的 `chebvander`/`chebfit`。它们复用同一套 `_fit`/`_vander_nd` 引擎，但因为正交基各列天然接近正交，条件数远好于幂基——文档注释里「Chebyshev/Legendre 拟合条件更好」的承诺在那里落地。
- **继续幂级数主线**：u3-l6 讲 `polycompanion` 与 `polyroots`，从「拟合系数」走到「求多项式的根」，是本单元的收尾。
- **进阶阅读**：本讲的列归一化与 `rcond` 是 u5-l1「数值稳定性与架构取舍」的具体落点，那里会把 domain 自动映射、正交基优势、友矩阵求根局限统一起来讨论。
- **源码延伸**：想看清 SVD 在底层如何被截断，可阅读 NumPy 的 `numpy.linalg.lstsq` 与 `numpy.linalg.svd` 实现；想看清其它正交族如何注入引擎，可对比 `chebyshev.py` 里的 `chebfit`/`chebvander2d` 与本讲的幂级数版本。
