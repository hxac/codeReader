# 矩阵对数 logm、funm 与分数幂

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `scipy.linalg.logm` 为什么采用「逆平方法（inverse scaling and squaring）」，并能描述它与上一讲 `expm` 的「缩放-平方」方向相反、互为逆运算的关系。
- 读懂 `_matfuncs_inv_ssq.py` 中两个被 `logm` 与 `fractional_matrix_power` 共享的核心引擎 `_inverse_squaring_helper` 与 `_fractional_power_pade`，理解它们如何决定「开几次方、用几阶 Padé」。
- 理解 `funm` 如何用 Schur 分解 + Parlett 递归把任意标量函数推广到矩阵，以及它在重特征值处为什么会失效。
- 知道 `fractional_matrix_power` 如何把分数幂拆成「整数幂 + 小数余项」、并用条件数选择拆分点；以及 `signm` 为何是「`funm` + Newton 迭代兜底」的组合。

## 2. 前置知识

### 2.1 矩阵函数不是「逐元素」

对向量我们常做逐元素运算，但 `scipy.linalg` 里的矩阵函数（`expm`、`logm`、`sqrtm`、`funm` 等）指的是 **把标量函数 \(f(x)\) 严格地推广成矩阵函数 \(f(A)\)**，它满足 \(f(A)\) 的泰勒级数定义：

\[ f(A) = \sum_{k=0}^{\infty} \frac{f^{(k)}(0)}{k!} A^k \]

例如矩阵指数 \(\mathrm{e}^A\) 与矩阵对数 \(\log A\) 互为逆运算（主对数分支下），即

\[ \mathrm{e}^{\,\log A} = A. \]

只有当 \(A\) 是对角阵时，矩阵函数才退化成逐元素作用。这一点是理解本讲所有函数的基石。

### 2.2 缩放-平方 与 逆平方

上一讲 `expm`（u5-l1）讲过 **缩放-平方（scaling and squaring）**：

\[ \mathrm{e}^A = \bigl(\mathrm{e}^{A/2^s}\bigr)^{2^s}. \]

把 \(A\) 缩小到范数很小（\(A/2^s\)），用 Padé 近似算 \(\mathrm{e}^{A/2^s}\)，再平方 \(s\) 次放大回来。

本讲的 `logm` 走的是**反方向**的 **逆平方（inverse scaling and squaring）**：利用

\[ \log A = 2^s \log\bigl(A^{1/2^s}\bigr), \]

对 \(A\) 反复开方（矩阵平方根）\(s\) 次，使 \(A^{1/2^s}\) 的特征值都聚集到 1 附近，从而 \(A^{1/2^s} = I + R\) 中 \(R\) 很小，\(\log(I+R)\) 就能用 Padé 高精度逼近，最后只要**乘以标量 \(2^s\)** 即可（注意：log 不需要「平方放大」回去，因为对数把幂次拉成了系数）。所以两者一正一反，正好配成一对。

> 关键差异：`expm` 缩小后**平方**回来；`logm` 开方缩小后**乘以 \(2^s\)** 回来。

### 2.3 Schur 分解是这些函数的共同出发点

回忆 u3-l5：实/复 Schur 分解 \(A = Z T Z^H\)，其中 \(Z\) 是酉矩阵、\(T\) 是上三角阵（复 Schur）或准上三角阵（实 Schur），且 \(T\) 的对角元就是 \(A\) 的特征值。本讲的 `logm`、`funm`、`fractional_matrix_power` 全部先把 \(A\) 化成上三角 \(T\)，在上三角上用专门算法算 \(f(T)\)，再用 \(Z f(T) Z^H\) 还原。这是「矩阵函数计算」的标准范式。

上一讲末尾还澄清过：`expm` **不**调用 `scipy.sparse.linalg.onenormest`，而那个 1-范数估计器「仅供 `logm`/`fractional_matrix_power` 使用」——本讲就会看到它如何登场。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [`_matfuncs.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs.py) | 矩阵函数的**公共 Python 入口**。`logm`、`fractional_matrix_power`、`funm`、`signm` 都在此定义，但 `logm`/`fractional_matrix_power` 的数值核心被委派到下一个文件。 |
| [`_matfuncs_inv_ssq.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs_inv_ssq.py) | 「逆平方 + Padé」家族的实现，文件名 `inv_ssq` 即 inverse scaling and squaring。包含 `_logm`、`_logm_triu`、`_inverse_squaring_helper`、`_fractional_power_pade`、`_fractional_matrix_power` 等真正的数值内核。 |
| [`_linalg_pythran.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_linalg_pythran.py) | Pythran 编译的 `_funm_loops`，是 `funm` 中 Parlett 递归的高性能循环。 |

阅读建议：先看 `_matfuncs.py` 里的薄壳入口（校验 + 委派 + 误差估计），再钻进 `_matfuncs_inv_ssq.py` 看真正的算法。

## 4. 核心概念与源码讲解

### 4.1 logm：矩阵对数与「逆平方法」

#### 4.1.1 概念说明

矩阵对数 \(\log A\) 是矩阵指数的逆：\(\mathrm{e}^{\log A} = A\)。它要求 \(A\) 非奇异（特征值均非零），且主对数要求 \(A\) 没有负实轴上的特征值。

直接用幂级数 \(\log A = \log(I + (A-I)) = \sum_{k\ge 1}(-1)^{k+1}(A-I)^k/k\) 只在 \(A\) 接近单位阵 \(I\) 时收敛。对于一般 \(A\)，采用 **逆平方法**（Al-Mohy & Higham 2012）：反复开方把 \(A\) 拉近 \(I\)，对 \(\log(I+R)\) 用 Padé，再乘 \(2^s\)。

注意：对数把「开方」转化为「系数」，所以 `logm` 的还原步骤是 **标量乘法**（`U *= np.exp2(s)`），不像 `expm`/分数幂那样需要「平方」回去。

#### 4.1.2 核心流程

`logm` 的完整链路分两层：

```text
scipy.linalg.logm(A)          # _matfuncs.py 公共入口（薄壳）
   ├── _asarray_square / 方阵校验
   ├── 委派 → _matfuncs_inv_ssq._logm(A)   # 真正的算法
   ├── _maybe_real(A, F)        # 实输入+复输出且虚部可忽略时，剥离虚部
   └── 误差估计 norm(expm(F)-A, 1)/norm(A,1)  # 超阈值则发 RuntimeWarning

_matfuncs_inv_ssq._logm(A)
   ├── 若 A 已是上三角 → 直接 _logm_triu
   └── 否则 schur(A)（必要时 rsf2csf 转复三角）→ _logm_triu(T) → Z·U·Z^H 还原

_logm_triu(T):                         # 在上三角阵上算 log
   ├── _inverse_squaring_helper(T0, theta)  # 反复开方，返回 (R, s, m)
   │       R = T0^(1/2^s) - I，s=开方次数，m=Padé 阶
   ├── 用 m 阶 Gauss-Legendre 部分分式求 log(I+R) 的 Padé 值 U
   ├── U *= 2**s                       # 逆平方的还原（标量乘法）
   └── 用 _briggs / _logm_superdiag_entry 精化 U 的对角与超对角
```

#### 4.1.3 源码精读

**公共入口 `logm`** 是一个薄壳，真正算活委派给 `_logm`：

- [_matfuncs.py:L146-L207](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs.py#L146-L207) —— `logm` 全函数。注意它被 `@_apply_over_batch(('A', 2))` 装饰，支持批量维度（上一讲 u3-l6 解释过该装饰器）。
- [_matfuncs.py:L195-L198](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs.py#L195-L198) —— 委派：`import` 内部模块并调用 `_logm(A)`，再用 `_maybe_real` 处理「实输入、复输出且虚部可忽略」的情形。
- [_matfuncs.py:L199-L207](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs.py#L199-L207) —— **自检式误差估计**：直接计算 \(\|\mathrm{e}^{F}-A\|_1 / \|A\|_1\)（即 `expm(logm(A))` 是否真的回到 `A`），若超过 `1000*eps` 就发 `RuntimeWarning`。这正是综合实践要复现的验证。

`_maybe_real` 是个值得认识的小工具：

- [_matfuncs.py:L60-L91](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs.py#L60-L91) —— 当输入 `A` 是实矩阵、而计算结果 `B` 是复矩阵但其虚部「可以视为数值噪声」时，把 `B` 截断成实矩阵返回。它被 `logm`、`funm`、`tanm` 等共用。

**算法内核 `_logm`** 决定是否需要 Schur 分解：

- [_matfuncs_inv_ssq.py:L839-L885](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs_inv_ssq.py#L839-L885) —— `_logm` 全函数。
- [_matfuncs_inv_ssq.py:L866-L881](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs_inv_ssq.py#L866-L881) —— 分支：若 `A` 本身已是上三角，直接 `_logm_triu`；否则对实矩阵用 `schur`（必要时 `rsf2csf` 把实准三角转复三角），对复矩阵用 `schur(output='complex')`，最后 `Z.dot(U).dot(ZH)` 还原。`_logm_force_nonsingular_triangular_matrix`（L867/L878）会在对角元为 0（精确奇异）或过小（近奇异）时发警告，并对精确 0 的对角元做 ad-hoc 微扰 `1e-20` 以便继续算。

**上三角对数 `_logm_triu`** 是逆平方算法的主体：

- [_matfuncs_inv_ssq.py:L720-L816](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs_inv_ssq.py#L720-L816) —— `_logm_triu` 全函数。
- [_matfuncs_inv_ssq.py:L770-L776](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs_inv_ssq.py#L770-L776) —— 定义 Padé 近似阈值表 `theta`（取自 Higham 2008 表 2.1，共 16 阶），并调用 `_inverse_squaring_helper(T0, theta)` 得到 \((R, s, m)\)。
- [_matfuncs_inv_ssq.py:L783-L793](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs_inv_ssq.py#L783-L793) —— **Gauss-Legendre 部分分式求值**：`scipy.special.p_roots(m)` 给出 \(m\) 阶 Gauss-Legendre 节点/权重，从区间 \([-1,1]\) 线性变换到 \([0,1]\)，然后

  \[ \log(I+R) \approx \sum_k \alpha_k\, R\,(I+\beta_k R)^{-1}, \]

  其中每个 \((I+\beta_k R)^{-1}(\alpha_k R)\) 都通过 `solve_triangular` 求解（因为 \(R\) 上三角，求解是 \(O(n^2)\) 的廉价回代）。最后 `U *= np.exp2(s)` 即 \(2^s\)，完成逆平方的还原。

- [_matfuncs_inv_ssq.py:L798-L811](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs_inv_ssq.py#L798-L811) —— **对角/超对角精化**：`U` 的对角元用 `np.log(np.diag(T0))` 直接赋值，超对角元用 `_logm_superdiag_entry`（L264–L308）的解析公式重算，避免反复开方—Padé 引入的相消误差。

`_inverse_squaring_helper` 是 `logm` 与 `fractional_matrix_power` 共用的「开方 + 选阶」引擎，见 4.1.6 的精读。

#### 4.1.4 代码实践

**目标**：直观观察「逆平方」中的开方次数 \(s\) 与误差自检。

1. 构造一个特征值远离 1 的矩阵 \(A\)（例如正定阵，特征值较大）。
2. 调用 `scipy.linalg.logm(A)` 得到 \(F\)。
3. 用 `norm(expm(F) - A, 1) / norm(A, 1)` 复算入口里的自检误差。

```python
# 示例代码
import numpy as np
from scipy.linalg import logm, expm, norm

rng = np.random.default_rng(0)
B = rng.standard_normal((5, 5))
A = B @ B.T + 10*np.eye(5)   # 对称正定，特征值远离 0 和 1

F = logm(A)
errest = norm(expm(F) - A, 1) / norm(A, 1)
print("自检误差 =", errest)
print("expm(logm(A)) 是否回到 A:", np.allclose(expm(F), A))
```

**需要观察的现象**：自检误差通常在 \(10^{-15}\) 量级（远小于 `1000*eps ≈ 2.2e-13`），`np.allclose` 返回 `True`。

**预期结果**：`errest` 极小，说明 `logm` 与 `expm` 互为逆运算成立。

> 若想直接看到 \(s\) 与 \(m\)，可在交互环境里 `import scipy.linalg._matfuncs_inv_ssq as m; R, s, md = m._inverse_squaring_helper(A, m._logm_triu.__code__...)`（这仅用于学习，正式调用走 `logm`）。实际 \(s\) 取决于 \(A\) 的特征值分布：特征值离 1 越远，\(s\) 越大。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `logm` 在 `_logm_triu` 里最后是 `U *= 2**s`（标量乘），而 `expm` 是反复平方（矩阵乘）？

**答案**：因为 \(\log(A^{1/2^s}) = \frac{1}{2^s}\log A\)，开方在对数下被「拉成系数」，所以 \(\log A = 2^s \log(A^{1/2^s})\)，还原只需乘 \(2^s\)；而指数满足 \(\mathrm{e}^{A/2^s}\) 的 \(2^s\) 次方才是 \(\mathrm{e}^A\)，必须矩阵平方。

**练习 2**：若输入矩阵有一个对角元恰好为 0，`_logm` 会怎样？

**答案**：`_logm_force_nonsingular_triangular_matrix`（L819–L836）会发 `LogmExactlySingularWarning`，并把该对角元替换成 `1e-20` 继续算（返回近似值而非 `NaN`），因为严格奇异矩阵不存在对数。

#### 4.1.6 共享引擎：`_inverse_squaring_helper`

这个函数同时服务于 `logm`（4.1）与 `fractional_matrix_power`（4.2），值得单独看清。

- [_matfuncs_inv_ssq.py:L311-L445](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs_inv_ssq.py#L311-L445) —— 全函数。输入上三角 \(T_0\) 与一个阈值表 `theta`，输出 \((R, s, m)\)。
- [_matfuncs_inv_ssq.py:L366-L376](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs_inv_ssq.py#L366-L376) —— **先开方 \(s_0\) 次**：对角元取平方根直到谱半径（最大对角元偏离 1 的程度）≤ `theta[7]`，再对整矩阵做 \(s_0\) 次 `_sqrtm_triu`（u5-l3 会详讲）。
- [_matfuncs_inv_ssq.py:L383-L416](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs_inv_ssq.py#L383-L416) —— **选 Padé 阶 \(m\)**：用 `_onenormest_m1_power` 估计 \(\|(T-I)^p\|_1^{1/p}\)（\(p=2,3,4,5\)），与 `theta[m]` 比较；若低阶不够就再开一次方（\(s\) 加 1）并重估。这是**代价权衡**：多开方 → \(R\) 更小 → 可用更低阶 Padé，但开方本身贵；算法寻找总成本最优的 \((s,m)\)。注意 `_onenormest_m1_power` 内部用的正是 `scipy.sparse.linalg.onenormest`（L72–L111），这就是上一讲末尾埋下的伏笔。
- [_matfuncs_inv_ssq.py:L421-L440](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs_inv_ssq.py#L421-L440) —— 令 \(R = T - I\)，并在 \(T_0\) 存在主分支（无负实轴特征值）时，用 `_briggs_helper_function`（L153–L205，Al-Mohy 2012 的 Briggs 公式，减少相消）精化 \(R\) 的对角元、用 `_fractional_power_superdiag_entry` 精化超对角元。

> 一句话：`_inverse_squaring_helper` = 「反复开方把矩阵拉近 \(I\)，再用 1-范数估计挑一个最划算的 Padé 阶」。

### 4.2 fractional_matrix_power：分数幂与 Schur-Padé

#### 4.2.1 概念说明

\(A^p\)（\(p\) 为非整数）要求把「幂」推广到矩阵。它的数学定义是 \(A^p = \mathrm{e}^{p\log A}\)，但直接这样算精度不佳。SciPy 采用 Higham & Lin (2011) 的 **Schur-Padé 算法**：把 \(p\) 拆成整数部分 \(a\) 与小数余项 \(b\in[-1,1]\)，整数幂 \(A^a\) 用 `np.linalg.matrix_power`（精确），小数余项 \(A^b\) 走 Schur 分解 + 逆平方 + Padé。

小数余项的核心想法与 `logm` 同源：\(A^b\) 满足

\[ A^b = \bigl((A^{1/2^s})^b\bigr)^{2^s}, \]

开方 \(s\) 次把 \(A\) 拉近 \(I\)，对 \((I+R)^b\) 用 Padé，再**平方 \(s\) 次**还原（注意这里和 `logm` 不同——分数幂还原要平方，因为 \((A^{1/2^s})^b = A^{b/2^s}\)，平方 \(s\) 次才回到 \(A^b\)）。

#### 4.2.2 核心流程

```text
scipy.linalg.fractional_matrix_power(A, t)   # _matfuncs.py 薄壳
   └── 委派 → _matfuncs_inv_ssq._fractional_matrix_power(A, p)

_fractional_matrix_power(A, p):
   ├── p 为整数 → np.linalg.matrix_power(A, int(p))  # 精确快速路径
   ├── svdvals(A) 算条件数 k2 = σ_max/σ_min
   ├── 用 k2 在 floor/ceil 之间选 a，余项 b∈[-1,1]
   ├── R = _remainder_matrix_power(A, b)              # 小数余项
   └── return A^a @ R
        （若奇异导致余项失败且 p≥0 → 退化为 funm(A, x↦x**b)）

_remainder_matrix_power(A, t):       # -1 < t < 1
   ├── schur(A) → 上三角 T、酉 Z
   ├── 对角元 0 抛 FractionalMatrixPowerError（逆平方无法处理奇异）
   └── U = _remainder_matrix_power_triu(T, t) → Z·U·Z^H

_remainder_matrix_power_triu(T, t):
   ├── _inverse_squaring_helper(T0, m_to_theta) → (R, s, m)   # 复用！
   ├── U = _fractional_power_pade(-R, t, m)                   # Padé 求 (I+R)^t
   └── 平方 s 次还原为 T^t，并用 _fractional_power_superdiag_entry 精化对角/超对角
```

#### 4.2.3 源码精读

**公共入口** 仍是薄壳：

- [_matfuncs.py:L98-L143](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs.py#L98-L143) —— `fractional_matrix_power` 全函数。注释说明它「按文献 [1]（Higham & Lin 2011）第 6 节实现」。
- [_matfuncs.py:L141-L143](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs.py#L141-L143) —— 委派到 `_matfuncs_inv_ssq._fractional_matrix_power(A, t)`。延迟 `import` 是为规避循环依赖。

**整数/小数拆分**：

- [_matfuncs_inv_ssq.py:L670-L717](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs_inv_ssq.py#L670-L717) —— `_fractional_matrix_power` 全函数。
- [_matfuncs_inv_ssq.py:L680-L681](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs_inv_ssq.py#L680-L681) —— **整数快速路径**：`p == int(p)` 直接走 `np.linalg.matrix_power`，跳过所有 Padé 逻辑。
- [_matfuncs_inv_ssq.py:L683-L698](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs_inv_ssq.py#L683-L698) —— **条件数选拆点**：用 `svdvals` 算条件数 \(k_2 = \sigma_{\max}/\sigma_{\min}\)，比较 `p1*k2**(1-p1)` 与 `-p2*k2`（\(p_1=p-\lfloor p\rfloor\)、\(p_2=p-\lceil p\rceil\)）决定取 `floor` 还是 `ceil`，使余项 \(b\) 的误差放大最小。
- [_matfuncs_inv_ssq.py:L700-L702](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs_inv_ssq.py#L700-L702) —— 主路径：`R = _remainder_matrix_power(A, b)`，`Q = np.linalg.matrix_power(A, a)`，返回 `Q.dot(R)`。
- [_matfuncs_inv_ssq.py:L706-L717](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs_inv_ssq.py#L706-L717) —— **兜底**：奇异且 \(p<0\) 时返回全 `NaN`（负分数幂对奇异阵无定义）；奇异且 \(p\ge 0\) 时退化为通用 `funm(A, lambda x: pow(x, b))`。

**Schur + 三角余项**：

- [_matfuncs_inv_ssq.py:L595-L667](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs_inv_ssq.py#L595-L667) —— `_remainder_matrix_power` 全函数。
- [_matfuncs_inv_ssq.py:L636-L658](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs_inv_ssq.py#L636-L658) —— Schur 化三角；对角元全非零才允许继续（否则 `FractionalMatrixPowerError`）；若实三角有负对角元则强制转复数（负数的分数幂是复数）。
- [_matfuncs_inv_ssq.py:L662-L667](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs_inv_ssq.py#L662-L667) —— `U = _remainder_matrix_power_triu(T, t)`，再 `Z.dot(U).dot(ZH)` 还原。

**三角余项主体**：

- [_matfuncs_inv_ssq.py:L516-L592](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs_inv_ssq.py#L516-L592) —— `_remainder_matrix_power_triu` 全函数。
- [_matfuncs_inv_ssq.py:L548-L556](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs_inv_ssq.py#L548-L556) —— 分数幂专用的阈值表 `m_to_theta`（与 `logm` 的 `theta` 不同，故 `_inverse_squaring_helper` 的 `theta` 形参要由调用方传入）。
- [_matfuncs_inv_ssq.py:L563-L568](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs_inv_ssq.py#L563-L568) —— **复用逆平方引擎**：调 `_inverse_squaring_helper(T0, m_to_theta)` 得 \((R,s,m)\)，再 `_fractional_power_pade(-R, t, m)`。注释强调「传入的是 helper 返回值的相反数」。
- [_matfuncs_inv_ssq.py:L577-L589](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs_inv_ssq.py#L577-L589) —— **平方还原**：在最高层用 `_fractional_power_superdiag_entry` 精化对角/超对角，随后 `U = U.dot(U)` 共 \(s\) 次，把 \(A^{b/2^s}\) 还原成 \(A^b\)。

**Padé 求值 `_fractional_power_pade`**：

- [_matfuncs_inv_ssq.py:L466-L513](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs_inv_ssq.py#L466-L513) —— 用 **连分数自底向上**（Higham & Lin 2011 算法 4.1）求 \((1+x)^t\) 的 \(m\) 阶 Padé 近似。
- [_matfuncs_inv_ssq.py:L506-L510](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs_inv_ssq.py#L506-L510) —— 核心循环：从 \(j=2m-1\) 递减到 1，反复 `Y = solve_triangular(I + Y, R*c(j,t))`。系数 `c(j,t)` 由 [_matfuncs_inv_ssq.py:L448-L463](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs_inv_ssq.py#L448-L463) 的 `_fractional_power_pade_constant` 给出。因为传入了 \(-R\)，结果是 \((I+R)^t\)。

#### 4.2.4 代码实践

**目标**：验证 `fractional_matrix_power` 与开方、整数幂的一致性。

```python
# 示例代码
import numpy as np
from scipy.linalg import fractional_matrix_power as fmp, sqrtm

A = np.array([[1.0, 3.0], [1.0, 4.0]])

# p = 0.5 应等于 sqrtm
print("与 sqrtm 误差:", np.linalg.norm(fmp(A, 0.5) - sqrtm(A)))

# 整数次幂走快速路径
print("p=2 等于 A@A:", np.allclose(fmp(A, 2), A @ A))

# (A^0.5)^2 ≈ A
B = fmp(A, 0.5)
print("(A^0.5)^2 ≈ A:", np.allclose(B @ B, A))

# 分数幂再合并：(A^0.3)@(A^0.2) ≈ A^0.5
print("指数律:", np.allclose(fmp(A, 0.3) @ fmp(A, 0.2), fmp(A, 0.5)))
```

**需要观察的现象**：`fmp(A,0.5)` 与 `sqrtm(A)` 几乎相等；`(A^0.5)^2` 回到 `A`；指数律成立（小误差）。

**预期结果**：各 `allclose` 返回 `True`，误差在 \(10^{-14}\) 量级。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_fractional_matrix_power` 要用条件数 \(k_2\) 来选 `floor`/`ceil`，而不是简单取 \(b = p - \lfloor p\rfloor\)？

**答案**：因为整数幂 \(A^a\) 是精确的、不放大误差，而小数余项 \(A^b\) 的相对误差会被 \(A\) 的条件数按 \(k_2^{1-b}\) 量级放大。选 `floor`（\(b\ge 0\)）还是 `ceil`（\(b\le 0\)）会改变这个放大因子，代码在两者间取误差更小的拆分。

**练习 2**：`_fractional_matrix_power(A, 2)` 会调用 Padé 吗？

**答案**：不会。`p == int(p)` 时（L680–L681）直接 `return np.linalg.matrix_power(A, int(p))`，跳过整个 Schur-Padé 链路。

### 4.3 funm：通用矩阵函数的 Schur-Parlett 方法

#### 4.3.1 概念说明

`funm(A, func)` 把**任意**标量可调用对象 `func`（如 `np.sin`、`np.exp`、`lambda x: x*x`）推广成矩阵函数。它是矩阵函数家族里最通用、也最容易出错的入口。

原理是 **Schur-Parlett**：\(A = Z T Z^H\)，则 \(f(A) = Z f(T) Z^H\)。对上三角 \(T\)：
- 对角元：\(f(T)_{ii} = f(T_{ii})\)，直接对对角元调用 `func`。
- 非对角元：用 **Parlett 递归** 求解（Golub & Van Loan 算法 11.1.1）：

  \[ F_{ij} = \frac{T_{ij}\bigl(f(T_{jj}) - f(T_{ii})\bigr) + \sum_{k=i+1}^{j-1}\bigl(T_{ik}F_{kj} - F_{ik}T_{kj}\bigr)}{T_{jj} - T_{ii}}, \quad i < j. \]

  它本质来自「\(f(T)\) 与 \(T\) 可交换 → \(T f(T) = f(T) T\)」逐条比对上三角元素。

**致命弱点**：当 \(T_{ii} = T_{jj}\)（重特征值）时分母 \(T_{jj}-T_{ii}=0\)，递归失效。`funm` 对此只能跳过除法（精度下降），并在误差估计里把这种情况反映出来。这是 `logm`/`sqrtm` 不用 Parlett 而用更稳健的逆平方/Schur 分块算法的原因。

#### 4.3.2 核心流程

```text
scipy.linalg.funm(A, func, disp=True)
   ├── _asarray_square(A)
   ├── T, Z = schur(A); T, Z = rsf2csf(T, Z)      # 强制复上三角
   ├── F = diag(func(diag(T)))                     # 对角元直接作用
   ├── F, minden = _funm_loops(F, T, n, minden)   # Parlett 递归（Pythran）
   ├── F = Z @ F @ Z^H                             # 还原到原基
   ├── F = _maybe_real(A, F)                       # 剥离可忽略虚部
   └── 误差估计 err ∝ norm(triu(T,1),1)/minden，超阈值则打印警告
        （disp=False 时返回 (F, err)）
```

#### 4.3.3 源码精读

- [_matfuncs.py:L679-L771](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs.py#L679-L771) —— `funm` 全函数。docstring 点明它实现「基于 Schur 分解的通用算法（Golub & Van Loan 算法 9.1.1）」，并提示若已知矩阵可对角化（如 Hermitian），用 `eigh` 更快。
- [_matfuncs.py:L745-L748](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs.py#L745-L748) —— `schur(A)` 后用 `rsf2csf(T,Z)`（u3-l5）把实准三角转复三角；然后 `diag(func(diag(T)))` 对对角元调 `func`。
- [_matfuncs.py:L755](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs.py#L755) —— 调用 Pythran 内核 `_funm_loops(F, T, n, minden)` 执行 Parlett 递归，返回更新后的 \(F\) 与最小分母 `minden`。
- [_matfuncs.py:L757-L758](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs.py#L757-L758) —— `Z @ F @ Z^H` 还原；`_maybe_real` 剥离虚部。
- [_matfuncs.py:L760-L771](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs.py#L760-L771) —— **误差估计**：`err = min(1, max(tol, (tol/minden)*norm(triu(T,1),1)))`，即「上三角严格上三角部分的范数」与「最小特征值差」之比；若 `F` 含非有限值则 `err=inf`。`disp=True` 时超 `1000*tol` 打印警告，否则返回 `(F, err)`。

**Parlett 递归内核**（Pythran 编译，签名导出 4 种 dtype）：

- [_linalg_pythran.py:L5-L20](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_linalg_pythran.py#L5-L20) —— `_funm_loops`。双重循环按超对角线 `p` 由近及远地填 \(F_{ij}\)：分子 `s = T[i,j]*(F[j,j]-F[i,i]) + Σ(T[i,k]F[k,j] - F[i,k]T[k,j])`，分母 `den = T[j,j]-T[i,i]`，`den != 0` 才除（否则保留 `s`，精度下降），并记录 `minden = min(minden, abs(den))`。

#### 4.3.4 代码实践

**目标**：用 `funm` 作用 `np.sin`，并与 `sinm` 对比；体会「对可对角化矩阵用 `eigh` 更快」。

```python
# 示例代码
import numpy as np
from scipy.linalg import funm, sinm, schur

A = np.array([[1.0, 3.0], [1.0, 4.0]])

# funm 作用 sin，与 sinm 比较
print("funm(sin) 与 sinm 误差:", np.linalg.norm(funm(A, np.sin) - sinm(A)))

# 二次：funm(A, x->x*x) 应等于 A@A
print("funm(x^2) == A@A:", np.allclose(funm(A, lambda x: x*x), A @ A))

# 用 disp=False 拿误差估计
F, err = funm(A, np.exp, disp=False)
print("funm(exp) 误差估计:", err)
```

**需要观察的现象**：`funm(A, np.sin)` 与 `sinm(A)` 几乎相同（`sinm` 走 `expm` 路径，两者精度都高）；二次函数 `funm(A, x*x)` 与 `A@A` 完全一致。

**预期结果**：误差在 \(10^{-15}\) 量级，`allclose` 为 `True`。

> **思考（待本地验证）**：构造一个有重特征值的矩阵（如 `[[1,1],[0,1]]`，特征值均为 1），观察 `funm(A, np.exp, disp=False)` 返回的 `err` 是否明显变大——这就是 Parlett 递归在重特征值处退化的表现。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `funm` 要先 `rsf2csf` 把实 Schur 形转成复上三角？

**答案**：Parlett 递归要求 \(T\) 是**严格上三角**（对角元即特征值）。实 Schur 形是准上三角，2×2 块承载复共轭特征值对，会破坏递归的逐元素结构，所以必须先 `rsf2csf` 拍平成复上三角。

**练习 2**：`funm` 的误差估计里 `minden` 越小代表什么？

**答案**：`minden = \min |T_{jj}-T_{ii}|` 是最接近的一对特征值之差。它越小，Parlett 递归中某一步的分母越接近 0，该步的相对误差越大，所以误差估计随 `1/minden` 增大。

### 4.4 signm：矩阵符号函数

#### 4.4.1 概念说明

矩阵符号函数 `signm` 是标量 `sign(x)` 的推广：对没有虚轴上特征值的方阵 \(A\)，它把右半平面的特征值映成 \(+1\)、左半平面映成 \(-1\)。等价地 \(\mathrm{signm}(A)^2 = I\)，且它与 \(A\) 有相同的（右/左半平面划分的）不变子空间。

#### 4.4.2 核心流程

```text
scipy.linalg.signm(A):
   ├── 定义标量 rounded_sign(x)：先把接近 0 的实部按容差归 0，再取 sign
   ├── result, errest = funm(A, rounded_sign, disp=0)   # 复用 funm！
   ├── if errest < errtol: return result                # 一次成功就返回
   └── 否则（缺陷矩阵，funm 失效）→ Newton 迭代兜底：
         S0 = A + c*I   （c=0.5/σ_max，平移避开零特征值）
         反复 S0 = 0.5*(S0 + inv(S0))，直到收敛或不动
```

#### 4.4.3 源码精读

- [_matfuncs.py:L774-L842](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs.py#L774-L842) —— `signm` 全函数。
- [_matfuncs.py:L803-L810](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs.py#L803-L810) —— 内嵌 `rounded_sign`：把实部绝对值小于 `1e3*eps*max|x|` 的视为 0，再取 `sign`；然后直接 `funm(A, rounded_sign, disp=0)` 复用 Schur-Parlett。这是 `signm` 的主路径。
- [_matfuncs.py:L811-L813](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs.py#L811-L813) —— 若 `funm` 的误差估计 `errest < errtol`（`1e3*eps` 量级），直接返回，**不进入迭代**。
- [_matfuncs.py:L815-L842](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs.py#L815-L842) —— **缺陷矩阵兜底**（注释引用 Denman & Leyva-Ramos 1981）：当 `funm` 因重特征值失效时，用 `svd(A)` 取最大奇异值算平移量 `c = 0.5/max_sv`，令 `S0 = A + cI`（平移以避开零特征值，避免奇异），再迭代

  \[ S_{k+1} = \tfrac{1}{2}(S_k + S_k^{-1}), \]

  这是经典的 **Newton-Schulz 符号迭代**，对每个特征值二阶收敛到 \(\pm 1\)。迭代最多 100 次，用 `norm(Pp@Pp - Pp,1)`（其中 `Pp = 0.5*(S0@S0 + S0)` 是投影算子）监测收敛；不收敛则打印警告。

#### 4.4.4 代码实践

**目标**：观察 `signm` 把特征值映射到 \(\pm 1\)，并验证 `signm(A)^2 = I`。

```python
# 示例代码
import numpy as np
from scipy.linalg import signm, eigvals

A = np.array([[1.0, 2.0, 3.0],
              [1.0, 2.0, 1.0],
              [1.0, 1.0, 1.0]])

print("A 的特征值:", eigvals(A))
S = signm(A)
print("signm(A) 的特征值:", eigvals(S))
print("signm(A)^2 ≈ I:", np.allclose(S @ S, np.eye(3)))
```

**需要观察的现象**：`A` 有正有负的实特征值；`signm(A)` 的特征值被映成 \(\pm 1\)（正特征值→+1，负特征值→−1）；`S@S` 是单位阵。

**预期结果**：`eigvals(S)` 全为 \(\pm 1\)，`S@S ≈ I` 成立。

#### 4.4.5 小练习与答案

**练习 1**：`signm` 为什么不直接调 `funm`，而要额外准备 `rounded_sign`？

**答案**：`sign` 在 0 处不连续且对接近 0 的实部极其敏感。`rounded_sign` 把「容差内的近零实部」强制归零后再取 `sign`，避免数值噪声导致特征值符号判反；同时让 `funm` 在 Schur 对角元上调用的函数更稳定。

**练习 2**：Newton 兜底迭代里的 `c = 0.5/max_sv` 平移起什么作用？

**答案**：缺陷矩阵可能有零特征值（或极接近零），`inv(S0)` 会爆炸。用 `A + cI` 把谱整体右移，确保 `S0` 非奇异、迭代可执行；`c` 取 `0.5/σ_max` 是经验值，既避开零又不改变谱的左右半平面划分太多。

## 5. 综合实践

把本讲四个函数串起来，完成下面这个端到端验证任务（即本讲指定的实践任务）：

1. **正定矩阵的 log/exp 互逆**：构造对称正定矩阵 \(A\)，计算 `logm(A)`，验证 `expm(logm(A)) ≈ A`，并打印自检误差。
2. **分数幂与对数的关系**：验证 \(A^{0.5} \approx \mathrm{expm}(0.5\cdot\mathrm{logm}(A))\)（理论上分数幂等价于 \(e^{p\log A}\)），体会两条实现路径（Schur-Padé vs 逆平方-for-log）殊途同归。
3. **funm 与 sinm 对比**：用 `funm(A, np.sin)` 作用正定矩阵，与 `sinm(A)` 比较，验证两者一致。
4. **signm 的不变性**：对同一 \(A\) 计算 `signm`，验证 `signm(A)^2 ≈ I`。

```python
# 示例代码（综合实践）
import numpy as np
from scipy.linalg import logm, expm, fractional_matrix_power as fmp, funm, sinm, signm, norm

rng = np.random.default_rng(42)
B = rng.standard_normal((4, 4))
A = B @ B.T + 2*np.eye(4)   # 对称正定，无负实轴特征值

# (1) log/exp 互逆
F = logm(A)
print("[1] expm(logm(A)) 误差:", norm(expm(F) - A, 1) / norm(A, 1))

# (2) 分数幂 ≈ expm(p*logm(A))
p = 0.5
lhs = fmp(A, p)
rhs = expm(p * F)
print("[2] A^0.5 vs expm(0.5*logm(A)) 误差:", norm(lhs - rhs, 1) / norm(A, 1))

# (3) funm(sin) vs sinm
print("[3] funm(sin) vs sinm 误差:", norm(funm(A, np.sin) - sinm(A), 1))

# (4) signm^2 = I
S = signm(A)
print("[4] signm(A)^2 ≈ I:", np.allclose(S @ S, np.eye(4)))
```

**预期结果**：
- [1] 误差约 \(10^{-15}\)，且不会触发 `logm` 的 `RuntimeWarning`。
- [2] 两条路径结果一致，误差 \(10^{-14}\) 量级——这验证了「分数幂 = 指数·对数」的数学定义，尽管 SciPy 用了完全不同的算法实现。
- [3] 误差 \(10^{-15}\) 量级。
- [4] `True`。

**进阶思考（待本地验证）**：若把 \(A\) 换成有重特征值的矩阵（如 `[[2,1,0],[0,2,0],[0,0,3]]`），观察 `funm(A, np.sin, disp=False)` 返回的 `err` 是否飙升，以及 `signm` 是否会落入 Newton 兜底分支——这正是 Schur-Parlett 在重特征值处的固有局限。

## 6. 本讲小结

- `logm` 采用**逆平方法**：反复开方把 \(A\) 拉近单位阵 \(I\)，对 \(\log(I+R)\) 用 Gauss-Legendre 部分分式 Padé 逼近，最后乘 \(2^s\) 还原；与上一讲 `expm` 的「缩放-平方」方向相反、互为逆运算。
- `_inverse_squaring_helper` 是 `logm` 与 `fractional_matrix_power` **共享的引擎**，负责「开方次数 \(s\) + Padé 阶 \(m\)」的代价最优选择，并用 `scipy.sparse.linalg.onenormest` 估计 \(\|(A-I)^p\|_1\)。
- `fractional_matrix_power` 把 \(A^p\) 拆成「整数幂（精确）+ 小数余项（Schur-Padé）」，并用条件数 \(k_2\) 选最优拆分点；`\_fractional_power_pade` 用连分数自底向上求 \((I+R)^t\)。
- `funm` 是最通用的入口，走 **Schur 分解 + Parlett 递归**（Pythran 内核 `_funm_loops`），对角元直接作用 `func`、非对角元用递归公式填；**致命弱点**是重特征值时分母为零、精度下降。
- `signm` = `funm(A, rounded_sign)` + **Newton-Schulz 迭代兜底**，主路径一次成功就返回，缺陷矩阵才进入迭代。
- 四者共性：先 Schur 化上三角、在三角阵上用专门算法、再 \(Z(\cdot)Z^H\) 还原；实输入若得到可忽略虚部的复输出，统一用 `_maybe_real` 截断。

## 7. 下一步学习建议

- **下一讲 u5-l3（sqrtm）**：矩阵平方根是逆平方法反复调用的「积木」（`_sqrtm_triu`），本讲多次出现却未展开。学完 sqrtm 能把 `_inverse_squaring_helper` 的内部完全打通。
- **u5-l4（三角/双曲矩阵函数与 Fréchet 导数）**：`cosm`/`sinm`/`tanhm` 等都基于 `expm`，本讲已用 `sinm` 做过对比；下一讲会系统讲解，并引入 `expm_frechet`/`expm_cond`。
- **延伸阅读源码**：`_matfuncs_inv_ssq.py` 中的 `_briggs_helper_function`（L153）、`_fractional_power_superdiag_entry`（L208）、`_logm_superdiag_entry`（L264）、`_unwindk`（L114）这些标量辅助函数体现了「在三角阵的对角/超对角上用解析公式精化、避免相消」的高精度技巧，值得逐个精读。
- **算法文献**：本讲反复引用 Al-Mohy & Higham (2012) 的矩阵对数算法、Higham & Lin (2011) 的分数幂算法，以及 Golub & Van Loan《Matrix Computations》的 Parlett 递归；想深入误差分析可对照原文阅读。
