# LDL、Polar 与 Cossin 分解

## 1. 本讲目标

本讲围绕 `scipy.linalg` 中三个相对独立的「高级」分解函数展开：`ldl`、`polar`、`cossin`。读完本讲，你应当能够：

1. 说清 **LDL 分解**（`A = L D L^H`）与 Cholesky 分解的区别，理解为什么 LDL 能处理「不定」对称矩阵，并能读懂 LAPACK 返回的紧凑存储与 `ipiv` 枢轴数组是如何被 Python 层解码的。
2. 说清 **极分解（Polar）** 的几何含义（一个矩阵 = 一个旋转/反射 + 一个拉伸），掌握 `side` 参数如何切换左右极分解，并理解它为什么只需一行 SVD 就能实现。
3. 说清 **余弦-正弦分解（CS / Cossin）** 对正交/酉矩阵分块的意义，理解 `p, q` 分块、`C² + S² = I` 角度关系，以及 `separate`/`swap_sign` 选项如何改变返回值。
4. 能够独立写出三个分解的调用与验证脚本，并对照源码定位关键行号。

## 2. 前置知识

在进入本讲前，建议你已经熟悉以下概念（在前面几讲中均已建立）：

- **SVD 奇异值分解**（u3-l4）：任意矩阵 \(A = W\Sigma V^H\)。本讲的 `polar` 直接复用 `svd`，`cossin` 的中间因子也由角度 \(\theta\) 表达，思想上同源。
- **Schur 分解**（u3-l5）：\(A = ZTZ^H\)。本讲承接待续，CS 分解可视作「对正交矩阵做分块版的 Schur」。
- **Cholesky 分解**（u3-l2）：\(A = LL^H\)，要求对称正定。LDL 正是「不要求正定」的推广，二者常被对照学习。
- **LAPACK 例程的分发**：`get_lapack_funcs` 按 dtype 选 `s/d/c/z` 前缀（u7-l1 会深入），`_compute_lwork` 用「`lwork=-1` 查询再正式调用」的两步法。
- **批量维度与 `_apply_over_batch`**：本讲三个函数都通过该装饰器支持在矩阵前再叠加若干「批处理维度」。

几个本讲会用到的术语先在这里统一：

| 术语 | 含义 |
|------|------|
| 对称矩阵 | \(A = A^T\)（实矩阵） |
| Hermitian 矩阵 | \(A = A^H\)，即 \(A = \bar A^T\)（复矩阵；实矩阵时退化为对称） |
| 正定 | 对任意非零向量 \(x\)，\(x^H A x > 0\)；等价于所有特征值为正 |
| 不定（indefinite） | 既有正特征值又有负特征值；Cholesky 无法处理，LDL 可以 |
| 惯性（inertia） | 一个对称矩阵的正、负、零特征值个数之比；LDL 的 \(D\) 可读出惯性 |
| 酉 / 正交矩阵 | \(U^H U = I\)（复）/ \(Q^T Q = I\)（实）；它们保持长度不变 |

## 3. 本讲源码地图

本讲涉及三个源码文件，每个文件只导出一个公共函数，主题一一对应：

| 文件 | 公共函数 | 作用 |
|------|----------|------|
| [_decomp_ldl.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_ldl.py) | `ldl` | 对称/Hermitian 矩阵的 \(LDL^H\)（Bunch-Kaufman）分解 |
| [_decomp_polar.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_polar.py) | `polar` | 任意矩阵的极分解（基于 SVD） |
| [_decomp_cossin.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_cossin.py) | `cossin` | 正交/酉矩阵的余弦-正弦（CS）分解 |

三者都被 `__init__.py` 通过星号导入汇聚到顶层命名空间（分别见 [__init__.py:206](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/__init__.py#L206)、[__init__.py:212](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/__init__.py#L212)、[__init__.py:221](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/__init__.py#L221)），所以 `from scipy.linalg import ldl, polar, cossin` 即可使用。

每个文件的 `__all__` 都只列了自己那一个函数（如 [_decomp_ldl.py:12](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_ldl.py#L12) 的 `__all__ = ['ldl']`），其余带下划线前缀的（如 `_ldl_sanitize_ipiv`、`_cossin`）都是私有辅助函数，不对外暴露。

---

## 4. 核心概念与源码讲解

### 4.1 LDL 分解：对称矩阵的「不需正定」分解

#### 4.1.1 概念说明

Cholesky 分解（u3-l2）要求矩阵**对称正定**，否则分解失败。但很多现实中的对称矩阵是**不定**的（既有正特征值也有负特征值），比如来自物理方程的刚度矩阵、优化中的海森矩阵。LDL 分解正是为此而生：

\[
A = L\,D\,L^H
\]

其中：

- \(L\) 是**单位**下三角矩阵（对角线全为 1）；
- \(D\) 是**分块对角**矩阵，块的大小**至多 2×2**；
- \(L^H\) 是 \(L\) 的共轭转置（实矩阵时就是 \(L^T\)）。

为什么 \(D\) 会有 2×2 块？因为为了保证**数值稳定性**，LDL 采用 **Bunch-Kaufman 主元策略**：当某个 1×1 主元太小（会导致除法放大误差）时，算法会取出一个 2×2 块作为主元。这也是为什么 LAPACK 返回的结果里会带一个**置换**——主元策略重排了行列。

LDL 相比 Cholesky 的优势总结：

| 性质 | Cholesky (\(LL^H\)) | LDL (\(LDL^H\)) |
|------|---------------------|------------------|
| 要求正定？ | 是 | 否（只需对称/Hermitian） |
| 需要开方？ | 是（算对角元要开方） | 否（\(D\) 直接给出） |
| 主元/置换？ | 无 | 有（Bunch-Kaufman） |
| 能反映惯性？ | 不能（只能正定） | 能（\(D\) 的正负号给出惯性） |

一个重要用途：\(D\) 中 1×1 块的正负号个数，就是对角化后正、负特征值的个数——这就是「惯性」。所以 LDL 既能求解对称线性系统，也能判断矩阵定性。

#### 4.1.2 核心流程

`ldl` 的 Python 层是典型的「校验 → 委派 LAPACK → 解码紧凑结果」三段式：

```text
1. 校验：A 必须是方阵（复矩阵且 hermitian=True 时对角虚部发 ComplexWarning）
2. 选例程：复+hermitian → ?HETRF；否则 → ?SYTRF
3. 调用 LAPACK，得到紧凑存储 ldu、枢轴数组 piv、状态码 info
4. info < 0 → 抛 ValueError（参数非法）
5. 解码（三步）：
   a. _ldl_sanitize_ipiv(piv)  → 把 LAPACK 奇怪编码的 ipiv 转成「交换数组 + 块大小数组」
   b. _ldl_get_d_and_l(ldu, pivot_arr)  → 从紧凑存储抽出 D（块对角）和 L（单位三角）
   c. _ldl_construct_tri_factor(lu, swap_arr, pivot_arr)  → 应用交换序列，得到显式三角因子 lu 和置换向量 perm
6. 返回 (lu, d, perm)，满足 lu @ d @ lu.T ≈ A，且 lu[perm, :] 是真正的三角矩阵
```

关键点：LAPACK 的 `?(HE/SY)TRF` 返回的不是直接可用的 \(L\) 和 \(D\)，而是一种**紧凑编码**——上/下三角区域塞了因子，`ipiv` 数组用「正数表示 1×1 块、负数成对表示 2×2 块」的方式编码主元信息。Python 层的三个辅助函数就是来「翻译」这套编码的。

#### 4.1.3 源码精读

**入口与签名** —— [_decomp_ldl.py:15-16](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_ldl.py#L15-L16)：`ldl` 被 `@_apply_over_batch(('A', 2))` 装饰，因此可在 `A` 前叠加批量维度；参数 `lower`（取下/上三角）、`hermitian`（复矩阵按 \(A=A^H\) 还是 \(A=A^T\) 处理）、`overwrite_a`、`check_finite` 都是公共参数体系（见 u2-l2）。

**校验与空输入** —— [_decomp_ldl.py:126-131](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_ldl.py#L126-L131)：先把输入升维并做有限性检查，再要求方阵；对 `(0,0)` 空矩阵直接返回三个空数组，避免进入 LAPACK。

**选择 hetrf / sytrf** —— [_decomp_ldl.py:137-144](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_ldl.py#L137-L144)：复矩阵且 `hermitian=True` 走 Hermitian 例程 `hetrf`，否则走对称例程 `sytrf`；若用户对带虚部的对角复矩阵仍坚持 `hermitian=True`，发 `ComplexWarning` 提醒「虚部会被忽略」。

**调用 LAPACK** —— [_decomp_ldl.py:146-153](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_ldl.py#L146-L153)：先用 `get_lapack_funcs` 拿到带 `s/d/c/z` 前缀的例程与对应的 lwork 查询函数，用 `_compute_lwork` 查询最优工作数组长度，再一次性调用得到 `(ldu, piv, info)`；`info<0` 翻译成「第 `-info` 个参数非法」的 `ValueError`。

**三步解码** —— [_decomp_ldl.py:155-157](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_ldl.py#L155-L157)：依次调用三个私有辅助函数把 LAPACK 的紧凑结果翻译成用户友好的 `(lu, d, perm)`。

下面看这三个「翻译器」。

**`_ldl_sanitize_ipiv`：解码 ipiv** —— [_decomp_ldl.py:162-244](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_ldl.py#L162-L244)。LAPACK 的 `ipiv` 编码很特别：正数 `k` 表示第 `ind` 行与第 `k-1` 行做了 1×1 主元交换；负数（且与下一项成对）表示一个 2×2 块。该函数把 `ipiv` 转成两个规整数组：

- `swap_`：记录每一处行交换的目标索引（如 `[0,3,2,3]` 表示「第 1 行与第 4 行交换」）；
- `pivots`：用 `1`/`2`/`0` 标记主元块大小，`2` 后面自动跟一个 `0`（见 [_decomp_ldl.py:176-191](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_ldl.py#L176-L191) 的图示说明）。这样不必再单独维护一个块大小数组。

注意 [_decomp_ldl.py:217](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_ldl.py#L217) 那行偏移量 `(1,0,0,n,1)` vs `(-1,-1,n-1,-1,-1)`：因为 LAPACK 对上三角和下三角格式的索引起点不同（Fortran 1 基 + 上/下不同起点），上下两种模式要用不同的遍历方向与偏移。

**`_ldl_get_d_and_l`：抽出 D 和 L** —— [_decomp_ldl.py:247-300](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_ldl.py#L247-L300)。先取 `ldu` 的对角线作为 \(D\) 的初始骨架，再取严格下（或上）三角作为 \(L\) 的非对角部分、把对角线置 1（[_decomp_ldl.py:279-281](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_ldl.py#L279-L281)）。然后遍历 `pivots` 里的每个 `2`（[_decomp_ldl.py:283-298](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_ldl.py#L283-L298)）：把 2×2 块的跨对角元素从 `ldu` 拷进 \(D\)，并把 \(L\) 对应位置清零。若是 Hermitian 分解，\(D\) 的对称位置要取**共轭**（[_decomp_ldl.py:292-295](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_ldl.py#L292-L295)），这是 \(A=A^H\) 的体现。

**`_ldl_construct_tri_factor`：应用交换、求置换** —— [_decomp_ldl.py:303-357](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_ldl.py#L303-L357)。这一步把 Bunch-Kaufman 的交换序列真正施加到 \(L\) 上，得到显式的外因子 `lu`，并维护置换向量 `perm`。循环中（[_decomp_ldl.py:343-355](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_ldl.py#L343-L355)）逐个应用 `swap_vec` 记录的行交换，遇到 2×2 块时连同相邻列一起交换；最后用 `argsort(perm)`（[_decomp_ldl.py:357](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_ldl.py#L357)）返回一个置换索引，使得 `lu[perm, :]` 恰好是干净的上/下三角矩阵。文档里那句「`lu[perm, :]` is an upper/lower triangular matrix」就来自这里。

#### 4.1.4 代码实践

**实践目标**：对一个对称（但非正定）矩阵做 LDL 分解，验证 \(A = L D L^T\)，并通过 `D` 的对角符号读出惯性；再用 `perm` 把 `lu` 整理成三角矩阵。

**操作步骤**（保存为 `try_ldl.py` 并运行 `python try_ldl.py`）：

```python
# 示例代码
import numpy as np
from scipy.linalg import ldl

# 一个对称但「不定」的矩阵：有正也有负特征值
A = np.array([[ 2., -1.,  3.],
              [-1.,  2.,  0.],
              [ 3.,  0.,  1.]])
# 确认它对称
assert np.allclose(A, A.T)

lu, d, perm = ldl(A)              # 默认 lower=True, hermitian=True
print("lu =\n", lu)
print("d  =\n", d)
print("perm =", perm)

# 1) 验证重构
print("重构误差 ||lu @ d @ lu.T - A|| =", np.linalg.norm(lu @ d @ lu.T - A))

# 2) 用 perm 整理成干净的下三角
print("lu[perm] 是否严格上三角为 0：",
      np.allclose(np.triu(lu[perm], 1), 0))

# 3) 读惯性：d 的对角线正负号个数
diag_d = np.diag(d)
print("D 对角 =", diag_d)
print("正、负、零主元个数 =", (diag_d > 0).sum(), (diag_d < 0).sum(), (diag_d == 0).sum())
# 对照真实特征值的符号
print("A 特征值 =", np.linalg.eigvalsh(A))
```

**需要观察的现象**：

1. `lu @ d @ lu.T` 应当在浮点误差内等于 `A`（残差为 `1e-15` 量级）。
2. `lu[perm]` 的严格上三角应当接近 0，证明 `perm` 确实把外因子整理成了下三角。
3. `D` 对角线的正/负个数，应当与 `np.linalg.eigvalsh(A)` 给出的特征值正/负个数一致（这是 LDL 反映惯性的体现）。

**预期结果**：残差极小（接近机器精度）；由于本矩阵是不定矩阵（`A` 的特征值有正有负），`D` 对角会出现负值——这正是 Cholesky 会失败、而 LDL 成功的情形。

#### 4.1.5 小练习与答案

**练习 1**：把上面的 `A` 换成一个对称正定矩阵（例如 `M = B.T @ B`，`B` 随机），再做 `ldl`，观察 `D` 的对角符号有何不同？

**答案**：正定矩阵的所有特征值为正，故 `D` 的所有 1×1 对角元都为正、2×2 块对应的特征值也全正；惯性是「全正、无负、无零」。

**练习 2**：`ldl` 文档（[_decomp_ldl.py:104-123](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_ldl.py#L104-L123)）给了一个上三角输入的例子。请用 `lower=False` 重复本实践，并验证 `u @ d @ u.T ≈ A` 同样成立。

**答案**：`lower=False` 时函数读取 `A` 的上三角、返回上三角外因子 `u`，重构关系仍为 `u @ d @ u.T ≈ A`；此时 `u[perm]` 的严格下三角接近 0。

**练习 3**：为什么 LDL 需要 `perm` 而 Cholesky（u3-l2）不需要？

**答案**：Cholesky 不做主元（正定保证所有顺序主子式为正，无需交换就稳定），所以没有置换；LDL 用 Bunch-Kaufman 主元处理不定矩阵，主元策略重排行列，因此必须返回置换 `perm` 才能还原外因子的三角结构。

---

### 4.2 Polar 极分解：旋转 + 拉伸

#### 4.2.1 概念说明

极分解（Polar decomposition）是「把矩阵拆成一个旋转和一个拉伸」的代数刻画。对任意矩阵 \(a\)：

\[
a = u\,p \quad (\text{右极分解，默认})
\]

其中：

- \(u\) 是「广义酉/正交」部分——方阵时是酉矩阵，\(m>n\) 时其**列**正交归一，\(m<n\) 时其**行**正交归一；
- \(p\) 是 Hermitian **正半定**矩阵（若 \(a\) 非奇异则正定），代表纯拉伸。

这和复数的极坐标 \(z = r e^{i\theta}\) 是完全类比：\(u\) 对应「相角 \(e^{i\theta}\)」（只转不伸缩），\(p\) 对应「模长 \(r\)」（只伸缩不转）。

`side` 参数切换两种形式：

- `side="right"`（默认）：\(a = u\,p\)，\(p\) 形状为 \((n,n)\)；
- `side="left"`：\(a = p\,u\)，\(p\) 形状为 \((m,m)\)。

#### 4.2.2 核心流程

`polar` 的实现极其简短，核心思想是「极分解 = SVD 的重新组合」。设 \(a\) 的瘦 SVD 为 \(a = W\Sigma V^H\)（`full_matrices=False`），则：

\[
u = W V^H,\qquad
p_{\text{right}} = V \Sigma V^H,\qquad
p_{\text{left}} = W \Sigma W^H
\]

验证右极分解：
\[
u\,p = (W V^H)(V \Sigma V^H) = W \Sigma V^H = a \;\checkmark
\]

为什么 \(p\) 是正半定？因为 \(p = V\Sigma V^H\) 恰好是「用 \(a\) 的右奇异向量做相似变换、奇异值做特征值」的形式——奇异值 \(\Sigma\ge 0\)，所以 \(p\) 正半定。同理 \(u = WV^H\) 是两个酉矩阵的乘积，仍酉。

流程伪代码：

```text
1. 校验 side ∈ {'right','left'}，a 必须是 2 维
2. w, s, vh = svd(a, full_matrices=False)   # 瘦 SVD
3. u = w @ vh                                # 酉部分
4. 若 right: p = (vh.T.conj() * s) @ vh       # V Σ V^H
   若 left : p = (w * s) @ w.T.conj()         # W Σ W^H
5. 返回 (u, p)
```

这里 `(vh.T.conj() * s)` 利用 NumPy 广播：`vh.T.conj()` 形状 \((n,k)\)，乘以奇异值向量 `s` 形状 \((k,)\) 时按列广播，等价于「每一列乘以对应奇异值」。

#### 4.2.3 源码精读

整个实现只有十几行，见 [_decomp_polar.py:99-113](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_polar.py#L99-L113)：

- [_decomp_polar.py:99-100](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_polar.py#L99-L100)：校验 `side` 只能是 `'right'`/`'left'`。
- [_decomp_polar.py:101-103](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_polar.py#L101-L103)：要求 `a` 是二维数组。
- [_decomp_polar.py:105](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_polar.py#L105)：调用瘦 SVD。注意 `polar` 不自己调 LAPACK，而是复用 `scipy.linalg.svd`（u3-l4），所以 SVD 的批量、driver 选择等能力都隐式继承。
- [_decomp_polar.py:106](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_polar.py#L106)：\(u = W V^H\)。
- [_decomp_polar.py:107-112](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_polar.py#L107-L112)：按 `side` 选择 \(p\) 的构造方式，用广播避免显式构造对角 \(\Sigma\) 矩阵，更省内存。
- [_decomp_polar.py:113](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_polar.py#L113)：返回 `(u, p)`。

文件顶部的 `@_apply_over_batch(('a', 2))`（[_decomp_polar.py:9](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_polar.py#L9)）让它也支持批量维度——你可以传一堆矩阵进去一起分解。

#### 4.2.4 代码实践

**实践目标**：对方阵做右极分解，验证 \(a = up\)、\(u\) 酉、\(p\) 正定；再对比左右两种 `side`。

**操作步骤**：

```python
# 示例代码
import numpy as np
from scipy.linalg import polar

a = np.array([[1., -1.],
              [2.,  4.]])

# 右极分解 a = u @ p
u, p = polar(a)
print("u =\n", u)
print("p =\n", p)
print("重构 ||u @ p - a|| =", np.linalg.norm(u @ p - a))
print("u 是否酉 (u @ u.T ≈ I)：", np.allclose(u @ u.T, np.eye(2)))
print("p 的特征值（应全正）：", np.linalg.eigvalsh(p))

# 左极分解 a = p @ u
u2, p2 = polar(a, side="left")
print("左极重构 ||p2 @ u2 - a|| =", np.linalg.norm(p2 @ u2 - a))
print("左极 p2 形状 =", p2.shape, "（应为 (m,m)=(2,2)）")
```

**需要观察的现象**：

1. `u @ p` 在浮点误差内等于 `a`。
2. `u @ u.T` 接近单位阵（\(u\) 酉）。
3. `p` 的特征值全部为正（\(p\) 正定，因为 `a` 非奇异）。
4. 左极分解中 `p2` 形状为 `(2,2)`，而右极分解的 `p` 也是 `(2,2)`——对方阵二者同形，但含义不同。

**预期结果**：与 [_decomp_polar.py:52-59](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_polar.py#L52-L59) 文档示例一致：\(u\approx\begin{pmatrix}0.857 & -0.514\\ 0.514 & 0.857\end{pmatrix}\)（一个旋转矩阵），\(p\) 是对称正定矩阵。

#### 4.2.5 小练习与答案

**练习 1**：极分解的酉因子 \(u\) 和 `scipy.linalg.orth`（u3-l4）返回的列空间正交基有什么关系？

**答案**：\(u = WV^H\) 的列空间与 \(a\) 的列空间相同（因为 \(V^H\) 可逆地混合了 \(W\) 的列），所以 \(u\) 的列就是 \(a\) 列空间的一组正交归一基——`orth(a)` 给出的正是这样一组基（只是选了不同的正交化表示）。

**练习 2**：把 `a` 换成一个 \(2\times 3\) 的「扁」矩阵（\(m<n\)）做 `polar(a)`，`u` 和 `p` 各是什么形状？\(u\) 是「列正交」还是「行正交」？

**答案**：`u` 形状 \((2,3)\)，其**行**正交归一（`u @ u.T ≈ I`，见 [_decomp_polar.py:75-77](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_polar.py#L75-L77) 的非方示例）；`p` 形状 \((3,3)\)（右极，`p` 是 \((n,n)\)）。

**练习 3**：为什么 `polar` 选用 `svd(..., full_matrices=False)` 而不是 `full_matrices=True`？

**答案**：「瘦」SVD 让 \(W\) 形状为 \((m,k)\)、\(V^H\) 形状为 \((k,n)\)（\(k=\min(m,n)\)），既省存储又让 \(u=WV^H\) 直接得到 \((m,n)\) 的正确形状；用满 SVD 会多出与零奇异值对应的冗余列/行，对极分解无用。

---

### 4.3 Cossin 余弦-正弦分解：正交矩阵的分块解剖

#### 4.3.1 概念说明

余弦-正弦分解（Cosine-Sine decomposition，简称 CS 或 Cossin）专门针对**正交/酉矩阵**。给定 \(m\times m\) 的酉（或实正交）矩阵 \(X\)，按左上角 \((p,q)\) 分块：

\[
X = \begin{pmatrix} X_{11} & X_{12} \\ X_{21} & X_{22} \end{pmatrix}
\]

CS 分解把它写成：

\[
X = U \cdot \mathrm{CS} \cdot V^H
\]

其中 \(U=\mathrm{diag}(U_1, U_2)\)、\(V=\mathrm{diag}(V_1, V_2)\) 都是**分块对角**酉矩阵，而中间的 \(\mathrm{CS}\) 因子具有极规整的结构：除了单位阵块 \(I\)、零块、以及一对非负对角矩阵

\[
C = \mathrm{diag}(\cos\theta),\qquad S = \mathrm{diag}(\sin\theta),\qquad C^2 + S^2 = I
\]

之外几乎没有别的。角度 \(\theta\) 的个数是

\[
r = \min(p,\; m-p,\; q,\; m-q)
\]

直观上：CS 分解揭示了「一个正交变换在两个子空间之间是如何分配角度的」——这在量子计算、子空间比较、统计中的 Procrustes 问题里都有用。

`separate` 和 `swap_sign` 两个选项改变返回形态：

- `separate=True`：返回「低层组件」——`((u1, u2), theta, (v1h, v2h))`，直接给角度向量，不拼成大矩阵；
- `separate=False`（默认）：返回 `(u, cs, vh)` 三个完整矩阵；
- `swap_sign=True`：把 \(-S\)、\(-I\) 块放到左下角（默认在右上）。

#### 4.3.2 核心流程

`cossin` 分两层：外层 `cossin` 负责「输入解析」，内层 `_cossin` 负责「校验 + 调 LAPACK + 组装」。

```text
外层 cossin：
  - 若给了 p 或 q：把 X 当整体数组，按 (p,q) 切出四个子块 x11..x22
  - 否则：要求 X 是含 4 个子块的迭代器，直接取出
  - 调用 _cossin(x11, x12, x21, x22, ...)

内层 _cossin：
  1. 校验四个子块形状相容、能拼成方阵
  2. 复矩阵 → driver 'uncsd'；实矩阵 → 'orcsd'
  3. get_lapack_funcs 取例程，_compute_lwork 查工作数组
  4. 调 LAPACK csd，得到 theta、u1、u2、v1h、v2h、info
  5. info<0 → ValueError；info>0 → LinAlgError（未收敛）
  6. separate=True → 直接返回 ((u1,u2), theta, (v1h,v2h))
     否则 → 用 block_diag 拼 U、VDH，手工填出 CS 中间因子，返回 (U, CS, VDH)
```

CS 中间因子的手工组装是本函数最繁琐的部分：要根据 \(n_{11}, n_{12}, n_{21}, n_{22}, r\) 这些「各类单位阵块的阶数」算出每一块（\(I\)、\(C\)、\(S\)、\(-S\)、\(-I\)）在 \((m,m)\) 矩阵里的行列偏移，逐块写入。

#### 4.3.3 源码精读

**外层：输入解析** —— [_decomp_cossin.py:11-140](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_cossin.py#L11-L140)。两种入口：

- 给了 `p` 或 `q`（[_decomp_cossin.py:115-129](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_cossin.py#L115-L129)）：要求 `X` 方阵，校验 `0<p<m`、`0<q<m`，然后用切片 `X[..., :p, :q]` 等切出四块。注意用 `...` 是为了保留前面的批量维度。
- 没给 `p,q`（[_decomp_cossin.py:130-137](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_cossin.py#L130-L137)）：要求 `X` 是含恰好 4 个数组的迭代器，直接取出。

最后 [_decomp_cossin.py:139-140](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_cossin.py#L139-L140) 把四块交给内层 `_cossin`。

**内层：校验** —— [_decomp_cossin.py:143-167](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_cossin.py#L143-L167)。先确认每块非空（[_decomp_cossin.py:146-149](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_cossin.py#L146-L149)），再核验 `x12`、`x21` 的形状与 `x11`、`x22` 相容，以及四块能拼成方阵 `p+mmp == q+mmq`（[_decomp_cossin.py:161-165](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_cossin.py#L161-L165)），并由此得到 `m = p + mmp`。

**选 driver 与调用** —— [_decomp_cossin.py:169-182](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_cossin.py#L169-L182)。任意一块是复数就用复例程 `uncsd`，否则用实例程 `orcsd`（[_decomp_cossin.py:169-170](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_cossin.py#L169-L170)）。复数情形 lwork 返回 `(lwork, lrwork)` 两元组，故 [_decomp_cossin.py:174-175](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_cossin.py#L174-L175) 要分情况打包参数。LAPACK 的 `?uncsd`/`?orcsd` 一次返回 `theta` 和四个小酉矩阵（[_decomp_cossin.py:176-182](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_cossin.py#L176-L182)）。

**错误处理** —— [_decomp_cossin.py:184-189](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_cossin.py#L184-L189)：`info<0` 是参数非法（`ValueError`），`info>0` 是未收敛（`LinAlgError`）。注意这里 cossin 会真正抛 `LinAlgError`，与 ldl/polar 不同。

**组装结果** —— [_decomp_cossin.py:191-234](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_cossin.py#L191-L234)。`separate=True` 时（[_decomp_cossin.py:191-192](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_cossin.py#L191-L192)）直接把四个小矩阵和 `theta` 返回；否则用 `block_diag` 把 \((U_1,U_2)\)、\((V_1^H,V_2^H)\) 拼成大矩阵（[_decomp_cossin.py:194-195](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_cossin.py#L194-L195)），再由 `theta` 算出 \(C=\cos\theta\)、\(S=\sin\theta\)（[_decomp_cossin.py:198-199](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_cossin.py#L198-L199)），最后 [_decomp_cossin.py:200-232](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_cossin.py#L200-L232) 按 \(r, n_{11}, n_{12}, n_{21}, n_{22}\) 算出偏移，把 \(I/C/S/-S/-I\) 各块写进 \((m,m)\) 的 \(\mathrm{CS}\) 矩阵；`swap_sign` 控制 \(-S\)、\(-I\) 块落在右上（默认）还是左下。

#### 4.3.4 代码实践

**实践目标**：对一个随机正交矩阵做 CS 分解，验证 \(X = U\cdot\mathrm{CS}\cdot V^H\)，并对比「整体矩阵 + p,q」与「四子块」两种入口。

**操作步骤**：

```python
# 示例代码
import numpy as np
from scipy.stats import ortho_group
from scipy.linalg import cossin

rng = np.random.default_rng(0)
X = ortho_group.rvs(4, random_state=rng)   # 4x4 随机正交矩阵
p, q = 2, 2

# 入口 1：整体矩阵 + p, q
u, cs, vh = cossin(X, p=p, q=q)
print("重构 ||u @ cs @ vh - X|| =", np.linalg.norm(u @ cs @ vh - X))
print("u 是否正交：", np.allclose(u @ u.T, np.eye(4)))
print("cs 的 C/S 块对角关系（取 cos²+sin²）：待观察")
print("cs =\n", np.round(cs, 4))

# 入口 2：直接给四个子块
u2, cs2, vh2 = cossin((X[:p,:q], X[:p,q:], X[p:,:q], X[p:,q:]))
print("两种入口 cs 是否一致：", np.allclose(cs, cs2))

# separate=True：拿原始角度
(u1, u2s), theta, (v1h, v2h) = cossin(X, p=p, q=q, separate=True)
print("theta (弧度) =", theta)
print("cos²+sin² =", np.cos(theta)**2 + np.sin(theta)**2)
```

**需要观察的现象**：

1. `u @ cs @ vh` 在浮点误差内等于 `X`。
2. `u`、`vh` 都是正交矩阵（与 `X` 同型）。
3. 两种入口得到的 `cs` 完全一致。
4. `separate=True` 返回的 `theta` 满足 \(\cos^2\theta + \sin^2\theta = 1\)（逐元素）。

**预期结果**：重构误差为 `1e-15` 量级；`theta` 是长度为 \(r=\min(p,m-p,q,m-q)=2\) 的角度向量；\(\cos^2\theta+\sin^2\theta\) 逐项为 1。这与官方测试 [test_decomp_cossin.py:38-40](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/tests/test_decomp_cossin.py#L38-L40) 的断言一致（容差取 `m*1e3*eps`）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `cossin` 要求输入必须是正交/酉矩阵？如果输入一个普通方阵会怎样？

**答案**：CS 分解的数学前提是 \(X^H X = I\)（中间因子结构 \(C^2+S^2=I\) 由此保证）。输入普通方阵时 LAPACK 的 `orcsd`/`uncsd` 仍会运行，但分解的数学保证失效，重构误差会显著增大，甚至 `info>0` 抛 `LinAlgError`。

**练习 2**：`cossin` 外层函数本身**没有** `@_apply_over_batch` 装饰，但 [_decomp_cossin.py:143](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_cossin.py#L143) 的内层 `_cossin` 却有。批量维度是如何被处理的？

**答案**：外层 `cossin` 用 `X[..., :p, :q]` 等「带 `...` 的切片」保留前导批量维度（[_decomp_cossin.py:128-129](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_cossin.py#L128-L129)），把切出的带批量维度的四块传给 `_cossin`；批量展开由 `_cossin` 上的 `@_apply_over_batch` 完成（对每个二维切片调用一次核心逻辑）。

**练习 3**：`separate=True` 时返回 `theta` 而不是 `cs` 矩阵，有什么好处？

**答案**：`theta` 是长度为 \(r\) 的角度向量，存储量为 \(O(r)\)；而完整的 `cs` 是 \((m,m)\) 稀疏矩阵，存储 \(O(m^2)\) 且大部分是 0 或 ±1。对大规模问题或只需要角度信息的应用（如子空间夹角），`separate=True` 既省存储又直接给出物理上有意义的量。

---

## 5. 综合实践

把三个分解串起来，完成下面这个贯穿任务：

**任务背景**：你拿到一个对称矩阵 \(A\) 和一个正交矩阵 \(Q\)。请用本讲三个工具分别「解剖」它们，并理解每一步揭示的几何/代数信息。

```python
# 示例代码：综合实践
import numpy as np
from scipy.stats import ortho_group
from scipy.linalg import ldl, polar, cossin

rng = np.random.default_rng(42)

# ---- Part A：对称矩阵 A 的 LDL ----
B = rng.standard_normal((5, 5))
A = B + B.T                       # 对称（一般是不定的）
lu, d, perm = ldl(A)
assert np.allclose(lu @ d @ lu.T, A, atol=1e-10)
diag_d = np.diag(d)
print("[LDL] A 的惯性 (正,负,零) =",
      int((diag_d > 0).sum()), int((diag_d < 0).sum()), int((diag_d == 0).sum()))
print("[LDL] 对照特征值符号：",
      sum(np.linalg.eigvalsh(A) > 0), "正")

# ---- Part B：对 A 做极分解，看「旋转 + 拉伸」----
# 注意：A 对称但不一定正定，polar 仍可做（p 正半定）
u, p = polar(A)
assert np.allclose(u @ p, A, atol=1e-10)
print("[Polar] u 的偏离正交程度 ||u@u.T - I|| =", np.linalg.norm(u @ u.T - np.eye(5)))
print("[Polar] p 最小特征值（>=0 即正半定）=", np.linalg.eigvalsh(p).min())

# ---- Part C：对 Part B 得到的正交因子 u 做 CS 分解 ----
# u 是 5x5 正交矩阵，按 (p,q)=(2,3) 分块
p_dim, q_dim = 2, 3
U, CS, VH = cossin(u, p=p_dim, q=q_dim)
assert np.allclose(U @ CS @ VH, u, atol=1e-9)
print("[Cossin] 重构 u 的误差 =", np.linalg.norm(U @ CS @ VH - u))
(u1, u2), theta, (v1h, v2h) = cossin(u, p=p_dim, q=q_dim, separate=True)
print("[Cossin] 角度数 r =", len(theta), "（应等于 min(p,m-p,q,m-q) =",
      min(p_dim, 5-p_dim, q_dim, 5-q_dim), "）")
```

**操作要点与现象**：

1. **Part A**：LDL 的 \(D\) 对角正负个数应与 `eigvalsh` 给出的特征值正负个数一致——这是用 LDL 「数惯性」的标准做法；注意 `A=B+B.T` 通常不定，所以负主元一定存在（Cholesky 在这里会失败，而 LDL 成功）。
2. **Part B**：即便 \(A\) 不定，`polar` 仍返回合法的 \(u\)（正交）和 \(p\)（正半定，最小特征值 \(\ge 0\)）。这体现了极分解对任意矩阵都成立。
3. **Part C**：把 Part B 得到的正交因子 \(u\) 再喂给 `cossin`，验证 CS 分解只接受正交矩阵的约定；角度个数 \(r\) 应等于 \(\min(p,m-p,q,m-q)\)。

**预期结果**：三处 `assert` 均通过；LDL 惯性与特征值符号一致；`polar` 的 \(p\) 最小特征值非负；`cossin` 的角度个数与公式吻合。（具体数值随随机种子变化，但上述结构性结论稳定成立——若本地运行结果与本描述不符，以本地输出为准。）

## 6. 本讲小结

- **LDL**（`_decomp_ldl.py`）把对称/Hermitian 矩阵分解为 \(A=LDL^H\)，**不要求正定**，\(D\) 的块对角（1×1 与 2×2）来自 Bunch-Kaufman 主元；Python 层用 `_ldl_sanitize_ipiv`/`_ldl_get_d_and_l`/`_ldl_construct_tri_factor` 三个辅助函数把 LAPACK 的紧凑存储与 `ipiv` 编码翻译成 `(lu, d, perm)`，其中 `lu[perm,:]` 是干净三角矩阵，\(D\) 的对角符号给出矩阵惯性。
- **Polar**（`_decomp_polar.py`）实现极简：一次瘦 SVD 后 \(u=WV^H\)、\(p=V\Sigma V^H\)（右）或 \(p=W\Sigma W^H\)（左），把矩阵拆成「旋转/反射 \(u\) + 正半定拉伸 \(p\)」；`side` 切换左右两种形式。
- **Cossin**（`_decomp_cossin.py`）专攻正交/酉矩阵，按 \((p,q)\) 分块给出 \(X=U\cdot\mathrm{CS}\cdot V^H\)，中间因子由满足 \(C^2+S^2=I\) 的角度 \(\theta\) 主导；外层 `cossin` 解析两种输入（整体矩阵+`p,q` 或四子块迭代器），内层 `_cossin` 选 `uncsd`/`orcsd`、调 LAPACK 并手工组装 CS 中间因子。
- 三者共性：都是「Python 校验薄壳 + LAPACK 例程（`sytrf/hetrf`、`svd`、`uncsd/orcsd`）+ `info` 错误翻译」，且都通过 `@_apply_over_batch` 支持批量维度。
- 错误处理风格有别：`ldl`/`polar` 对数值问题较宽容（ldl 发 `LinAlgWarning`），`cossin` 在未收敛时会直接抛 `LinAlgError`。

## 7. 下一步学习建议

- **继续矩阵函数族**：本讲的 `polar` 给出的正定因子 \(p\) 与矩阵函数密切相关——下一讲 u5-l1 会讲 `expm`，u5-l2 讲 `logm`/`fractional_matrix_power`。事实上 \(p = (a^H a)^{1/2}\) 本质是「矩阵平方根」，可对照 u5-l3 的 `sqrtm` 阅读。
- **深入 LAPACK 接口**：本讲反复出现 `get_lapack_funcs`、`_compute_lwork`，u7-l1 会系统讲解它们的类型分发（`s/d/c/z` 前缀）与 lwork 两步查询法；想了解这些例程如何被 f2py 从 `.pyf.src` 生成，看 u7-l2。
- **批量后端**：`@_apply_over_batch` 只是 Python 层的批量展开，而 `inv/solve/det/svd` 等已迁移到 C++ 批量后端 `_batched_linalg`（u8-l1/u8-l2）。可以对比「Python 循环展开」与「C++ 内核循环」两种批量策略的差异。
- **配套测试**：想看更多边界用例（空矩阵、混合类型、错误子块），阅读 [tests/test_decomp_ldl.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/tests/test_decomp_ldl.py) 与 [tests/test_decomp_cossin.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/tests/test_decomp_cossin.py)，批量行为见 [tests/test_batch.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/tests/test_batch.py) 的 `test_ldl_cholesky`、`test_polar_qr_rq`、`test_cossin`。
