# SVD 奇异值分解及正交/零空间

## 1. 本讲目标

学完本讲后，你应该能够：

1. 说清奇异值分解（SVD）的数学结构 \(A = U\Sigma V^H\)，以及 `scipy.linalg.svd` 返回的 `U`、`s`、`Vh` 三者各自对应什么。
2. 理解 `full_matrices`、`compute_uv`、`lapack_driver` 这三个关键参数如何改变返回结果的形状、内容和性能。
3. 知道 `svd` 内部如何把真正的数值计算委派给 C++ 批量后端 `_batched_linalg._svd`，以及错误如何被 `_format_emit_errors_warnings` 统一翻译。
4. 掌握 `orth`（列空间正交基）与 `null_space`（零空间）如何基于 `rcond` 做秩截断。
5. 理解 `diagsvd` 如何把一维奇异值向量重新撑成二维 $\Sigma$ 矩阵，以及 `subspace_angles` 如何用「先正交化、再两次 SVD」求两个子空间的主夹角。

本讲承接 [u2-l3](u2-l3-inv-det-lstsq.md)（`pinv` 已用过 SVD、`get_lapack_funcs`），并为本单元后续与 u9（插值分解）打下 SVD 这块「分解积木」的基础。

---

## 2. 前置知识

### 2.1 什么是奇异值分解

对任意一个 \(M\times N\) 的实或复矩阵 \(A\)，都存在分解：

\[
A = U\Sigma V^H
\]

其中：

- \(U\) 是 \(M\times M\) 的**酉矩阵**（实矩阵时即正交矩阵），满足 \(U^H U = I\)。它的列向量称为**左奇异向量**。
- \(V\) 是 \(N\times N\) 的酉矩阵，列向量称为**右奇异向量**，\(V^H\) 是它的共轭转置。
- \(\Sigma\) 是 \(M\times N\) 的「对角」矩阵，只在主对角线上有非负实数 \(\sigma_1 \ge \sigma_2 \ge \dots \ge \sigma_{\min(M,N)} \ge 0\)，这些 \(\sigma_i\) 称为**奇异值**。

直觉上，SVD 把任意线性变换拆成「旋转/反射 → 沿坐标轴拉伸 → 再旋转/反射」三步，拉伸幅度就是奇异值。奇异值恒非负、按从大到小排序，最大的奇异值 \(\sigma_1\) 等于矩阵的谱范数（2-范数）。

### 2.2 秩、列空间、零空间

- **秩**（rank）：非零奇异值的个数，记作 \(r\)。它等于 \(A\) 的列空间（值域）与行空间的维数。
- **列空间**（column space / range）：所有形如 \(Ax\) 的向量张成的空间，也就是 \(U\) 的前 \(r\) 列张成的空间。
- **零空间**（null space）：满足 \(Ax = 0\) 的全体 \(x\)，也就是 \(V\) 的后 \(N-r\) 列张成的空间。

### 2.3 数值秩与 rcond 截断

浮点运算下「奇异值是否为零」并不清晰，比如 `1e-17` 算不算零？因此引入**数值秩**：把小于阈值 \(\text{tol} = \text{rcond}\cdot \sigma_1\) 的奇异值视为零。`rcond` 是「相对条件数」，默认取 `eps * max(M, N)`。本讲的 `orth`、`null_space` 都靠它判断有效秩。

### 2.4 LAPACK 的两种 SVD 驱动

LAPACK 提供两个求解一般矩阵 SVD 的例程：

- `gesdd`：分治算法（divide-and-conquer），通常更快，是 `scipy.linalg.svd` 的默认。
- `gesvd`：常规 QR 迭代算法，更保守、更稳定，MATLAB/Octave 用的是它。

二者接口相近，但 `gesdd` 在某些病态情形下可能不收敛或对 NaN 更敏感——这正是 `svd` 里 `lapack_driver` 参数要解决的取舍。

### 2.5 本讲反复出现的几个术语

| 术语 | 含义 |
|---|---|
| 酉矩阵 / 正交矩阵 | \(U^H U = I\)；实矩阵时 \(U^T U = I\) |
| 奇异值 | \(\Sigma\) 对角线上的非负实数，降序排列 |
| 前导批量维度 | 形如 `(…, M, N)` 的最后两维之外的维度，函数会把它们当作「一摞矩阵」逐片处理 |
| `_apply_over_batch` 装饰器 | 自动把一个「单矩阵函数」提升为支持前导批量维度的版本 |

---

## 3. 本讲源码地图

本讲全部内容集中在**一个**源码文件中，这是 `scipy.linalg` 里主题拆分粒度很细的体现：

| 文件 | 作用 |
|---|---|
| [_decomp_svd.py](_decomp_svd.py) | SVD 及其派生函数（`svd`/`svdvals`/`diagsvd`/`orth`/`null_space`/`subspace_angles`）的全部 Python 层实现 |

它依赖的几块「积木」（本讲会点到，详细实现见对应讲义）：

- `_batched_linalg`：C++ 扩展模块，真正调用 LAPACK 算 SVD 的地方（见 [u8-l1](u8-l1-batched-python-api.md)、[u8-l2](u8-l2-batched-cpp-backend.md)）。
- `lapack._normalize_lapack_dtype` / `_ensure_aligned_and_native`：dtype 归一化与内存对齐（见 [u7-l1](u7-l1-blas-lapack-dispatch.md)）。
- `_misc._datacopied`：判断输入是否已被拷贝（见 [u2-l2](u2-l2-solve-and-dispatch.md)）。
- `scipy._lib._util._apply_over_batch`：把单矩阵函数提升为批量函数的装饰器。
- `_decomp._asarray_validated`：带 `check_finite` 的 `asarray` 封装。

---

## 4. 核心概念与源码讲解

### 4.1 核心分解 svd

#### 4.1.1 概念说明

`svd` 是本讲的核心，其余五个函数要么直接调用它（`svdvals`、`orth`、`null_space`、`subspace_angles`），要么配合它做矩阵重构（`diagsvd`）。

它的签名是：

```python
svd(a, full_matrices=True, compute_uv=True, overwrite_a=False,
    check_finite=True, lapack_driver='gesdd')
```

- `a`：可以是 `(…, M, N)`，支持前导批量维度（一摞矩阵一起分解）。
- `full_matrices`：决定 \(U\) 是 \(M\times M\)（`True`，默认）还是 \(M\times K\)（`False`），其中 \(K=\min(M,N)\)。对 \(V^H\) 同理。
- `compute_uv`：是否同时返回 `U`、`Vh`；`False` 时只返回奇异值向量 `s`（这就是 `svdvals` 的实现）。
- `lapack_driver`：`'gesdd'`（默认，快）或 `'gesvd'`（稳）。
- `overwrite_a` / `check_finite`：与全包一致的公共参数。

返回约定要记牢：当 `compute_uv=True`，返回元组 `(U, s, Vh)`，其中 `Vh` 是 \(V^H\)（已经转置/共轭转置过），且重构公式为 `A == U @ diagsvd(s, M, N) @ Vh`。

#### 4.1.2 核心流程

`svd` 的 Python 层并不真正计算 SVD，它只是「校验 → 归一化 → 委派 → 汇报」的薄壳：

```
1. 校验 lapack_driver 必须是 'gesdd'/'gesvd'
2. _asarray_validated：转 ndarray，按 check_finite 拦 NaN/Inf
3. 要求 a.ndim >= 2（至少是矩阵）
4. _normalize_lapack_dtype：把非标准 dtype（如 float16）提升成 LAPACK 认的 s/d/c/z
5. _ensure_aligned_and_native：保证内存对齐、字节序原生
6. overwrite_a 门控：只有「二维 + Fortran 列主序连续」时才允许真正覆写
7. 空矩阵特判：直接返回单位阵形状的空结果
8. ILP64 溢出检查：full_matrices 下 U/Vh 可能巨大，32 位整数会溢出 → 报错
9. 委派 _batched_linalg._svd(...) 真正算
10. 用 _format_emit_errors_warnings 翻译每片的 LAPACK info
11. 按 compute_uv 返回 (U,s,Vh) 或 s
```

第 6 步的「二维 + F 序连续」门控、第 7 步空矩阵特判、第 8 步整数溢出检查，都是 `svd` 区别于「直接调 LAPACK」的工程细节，值得在源码精读里展开。

#### 4.1.3 源码精读

**函数签名与文档** [_decomp_svd.py:L37-L38](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_svd.py#L37-L38)：默认 `full_matrices=True`、`compute_uv=True`、`lapack_driver='gesdd'`，决定了「最常用调用得到完整三方阵」的行为。

**驱动名校验** [_decomp_svd.py:L144-L148](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_svd.py#L144-L148)：先校验 `lapack_driver` 必须是字符串且只能是 `'gesdd'`/`'gesvd'`，传错立刻抛 `TypeError`/`ValueError`，避免错误参数一路传进 C++ 后端。

**输入归一化** [_decomp_svd.py:L151-L164](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_svd.py#L151-L164)：
- L151 `check_finite` 拦 NaN/Inf；
- L154 要求至少二维；
- L160 `_normalize_lapack_dtype` 把不支持的 dtype 提升为 LAPACK 认的类型（提升意味着要拷贝，故顺带把 `overwrite_a` 设回 `False`）；
- L161 `_ensure_aligned_and_native` 保证内存对齐与原生字节序；
- L163–L164 是贯穿全包的 `overwrite_a` 门控——`_datacopied` 判断是否已拷贝，再叠加「必须二维 + Fortran 列主序连续」两个条件。这与 [u2-l2](u2-l2-solve-and-dispatch.md) 的 `solve` 完全一致。

**空矩阵特判** [_decomp_svd.py:L167-L183](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_svd.py#L167-L183)：当 `a.size == 0`，不去惊动 LAPACK，而是用一个 2×2 单位阵的 SVD 探测「正确的 dtype」，再手工填出形状正确的空 `U`/`s`/`Vh`（`full_matrices=True` 时给单位阵形状）。这保证 `orth(np.empty((0,0)))`、`null_space(np.empty((0,0)))` 等边界调用不崩溃。

**ILP64 整数溢出检查** [_decomp_svd.py:L185-L199](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_svd.py#L185-L199)：当 `compute_uv=True` 且非 ILP64 构建时，检查 `U`/`Vh` 的元素总数是否超过 `int32` 上限。`full_matrices=True` 时 `U`/`Vh` 是方阵，元素数是 \(\max(M,N)^2\)，超大矩阵会让 LAPACK 的 32 位索引溢出——此时主动报错，提示改用 `numpy.linalg.svd` 或构建 ILP64 版 SciPy。这正是 [u9-l2](u9-l2-ilp64-dtypes.md) 会讲的「32 位 vs 64 位整数」问题在本函数的具体体现。

**委派 C++ 后端** [_decomp_svd.py:L201-L207](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_svd.py#L201-L207)：真正的 SVD 计算由 `_batched_linalg._svd(a1, lapack_driver, compute_uv, full_matrices, overwrite_a)` 完成。它原生支持前导批量维度（对一摞矩阵逐片分解），返回结果的最后一个元素是错误列表 `err_lst`。Python 层用 `_format_emit_errors_warnings` 翻译它。

**错误翻译** [_decomp_svd.py:L17-L34](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_svd.py#L17-L34)：这是 `svd` 私有的错误聚合函数 `_format_emit_errors_warnings`。它遍历每个出错切片，按 `info` 分类：
- `info > 0`：SVD 不收敛 → 抛 `LinAlgError`；
- `info < 0` 且 `gesdd` 的 `-4`：该切片含 NaN → 抛 `ValueError`；
- 其他 `info < 0`：第 `-info` 个参数非法 → 抛 `ValueError`。

注意它把 `info > 0`（不收敛，最严重）排第一优先级，这与 [u2-l2](u2-l2-solve-and-dispatch.md) 讲过的 `_format_emit_errors_warnings` 范式（奇异 > 内部错误 > 病态）一脉相承。

**返回** [_decomp_svd.py:L209-L212](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_svd.py#L209-L212)：`compute_uv=True` 返回去掉错误列表后的元组 `(U, s, Vh)`；`False` 只返回 `s`。

#### 4.1.4 代码实践

**实践目标**：亲手验证 SVD 的三件事——重构恒等式、`full_matrices` 对形状的影响、批量维度。

**操作步骤**：

```python
# 示例代码（非项目原代码）
import numpy as np
from scipy.linalg import svd, diagsvd

rng = np.random.default_rng(0)
m, n = 9, 6
a = rng.standard_normal((m, n))

# (1) 默认 full_matrices=True
U, s, Vh = svd(a)
print(U.shape, s.shape, Vh.shape)          # (9,9) (6,) (6,6)

# (2) 重构
S = diagsvd(s, m, n)
print(np.allclose(a, U @ S @ Vh))          # True

# (3) full_matrices=False：U 变成 (9,6)
U2, s2, Vh2 = svd(a, full_matrices=False)
print(U2.shape)                            # (9,6)
print(np.allclose(a, U2 @ np.diag(s2) @ Vh2))  # True

# (4) 只取奇异值
print(np.allclose(s, svd(a, compute_uv=False)))  # True

# (5) 批量：一摞矩阵一起分解
aa = np.stack((a, 2*a))
sb = svd(aa, compute_uv=False)
print(sb.shape)                            # (2,6)
print(np.allclose(sb[1], 2*sb[0]))         # True，第2片是第1片的2倍
```

**需要观察的现象**：
- `U`/`Vh` 是酉矩阵：`np.allclose(U.T.conj() @ U, np.eye(m))` 应为 `True`。
- 奇异值降序且非负：`np.all(np.diff(s) <= 0)`、`np.all(s >= 0)`。
- `full_matrices=False` 时 `U` 的列数从 `m` 缩到 `min(m,n)=6`，节省内存但重构仍精确成立。

**预期结果**：上面所有 `allclose` 断言均为 `True`，形状打印与注释一致。

> 说明：以上命令未在本环境实际运行，结果基于源码逻辑与 docstring 示例推导；建议你在本地 `python` 中运行确认。

#### 4.1.5 小练习与答案

**练习 1**：对一个 `5×8` 的矩阵 `a`，`full_matrices=True` 和 `False` 时 `U`、`s`、`Vh` 的形状分别是什么？

**答案**：`K=min(5,8)=5`。
- `True`：`U` 为 `(5,5)`，`s` 为 `(5,)`，`Vh` 为 `(8,8)`。
- `False`：`U` 为 `(5,5)`，`s` 为 `(5,)`，`Vh` 为 `(5,8)`。（注意 `U` 此时列数本就是 5，与 `full_matrices` 无关；变化的是 `Vh`。）

**练习 2**：为什么 `gesdd` 算出的 `info=-4` 在源码里被单独处理成「该切片含 NaN」？

**答案**：因为 LAPACK `gesdd` 的内部约定中，`info=-4` 指第 4 个参数（矩阵本身）在算法中被判定为含 NaN/Inf。源码据此给出比「第 4 个参数非法」更准确的错误信息，提示用户检查数据而非参数顺序（见 [_decomp_svd.py:L28-L30](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_svd.py#L28-L30)）。

---

### 4.2 奇异值快捷接口 svdvals 与 Sigma 构造 diagsvd

#### 4.2.1 概念说明

- `svdvals(a)` 只返回奇异值向量 `s`，不计算 \(U\)、\(V^H\)。当你只关心谱范数、秩、条件数时，省掉两个大酉矩阵能显著省内存和时间。
- `diagsvd(s, M, N)` 是 `svd` 的逆操作之一：给定奇异值向量 `s` 和目标尺寸 `M×N`，撑出二维 \(\Sigma\) 矩阵，方便你写 `U @ Sigma @ Vh` 重构。

#### 4.2.2 核心流程

`svdvals` 极简——直接调 `svd(a, compute_uv=0, ...)`，丢掉 \(U\)、\(V^H\)，只取 `s`。

`diagsvd` 的逻辑：
1. `part = np.diag(s)` 得到 `len(s)×len(s)` 的对角阵。
2. 若 `len(s) == M`（行数）：在右侧补 `N-M` 列零 → `M×N`。
3. 若 `len(s) == N`（列数）：在底部补 `M-N` 行零 → `M×N`。
4. 否则报错。

#### 4.2.3 源码精读

**svdvals 即「只算奇异值的 svd」** [_decomp_svd.py:L300-L301](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_svd.py#L300-L301)：函数体只有一行 `return svd(a, compute_uv=0, overwrite_a=..., check_finite=...)`。注意它**没有** `lapack_driver` 参数——固定走默认的 `gesdd`，且因为不需要 \(U\)、\(V^H\)，自然也避开了 [4.1.3](#413-源码精读) 里的 ILP64 溢出检查路径。

**diagsvd 的「按行/按列补零」** [_decomp_svd.py:L344-L352](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_svd.py#L344-L352)：
- `np.diag(s)` 建对角阵；
- `len(s)==M` 用 `np.hstack` 右侧补零（「瘦高」\(\Sigma\)，列数多于行数）；
- `len(s)==N` 用 `np.r_` 底部补零（「矮胖」\(\Sigma\)，行数多于列数）。

它还被 `@_apply_over_batch(('s', 1))` 装饰 [_decomp_svd.py:L304](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_svd.py#L304)，意味着 `s` 可以带前导批量维度，对一摞奇异值向量同时撑出 \(\Sigma\)。

#### 4.2.4 代码实践

**实践目标**：体会 `svdvals` 的省内存效果，并用 `diagsvd` 重构矩阵。

**操作步骤**：

```python
# 示例代码
import numpy as np
from scipy.linalg import svd, svdvals, diagsvd

rng = np.random.default_rng(1)
a = rng.standard_normal((2000, 1000))   # 瘦长矩阵

# 只取奇异值，避免分配两个巨大酉矩阵
s = svdvals(a)
print(s.shape)                            # (1000,)
print(np.isclose(s[0], np.linalg.norm(a, 2)))   # 最大奇异值 = 谱范数 True

# diagsvd 重构（小矩阵演示）
b = rng.standard_normal((3, 4))
U, sb, Vh = svd(b)
B = U @ diagsvd(sb, 3, 4) @ Vh
print(np.allclose(b, B))                  # True
```

**需要观察的现象**：`svdvals` 的结果与 `svd(...)[1]` 完全一致；最大奇异值等于矩阵的 2-范数（可用 `np.linalg.norm(a, 2)` 交叉验证）。

**预期结果**：两处 `allclose/isclose` 均为 `True`。

> 说明：上述命令未在本环境运行，请本地验证。

#### 4.2.5 小练习与答案

**练习 1**：`diagsvd(np.array([1.,2.,3.]), 3, 5)` 返回什么形状、什么内容？

**答案**：`len(s)=3==M=3`，走 `hstack` 右补 2 列零，得 `3×5` 矩阵，对角线为 `[1,2,3]`，其余为 0：
```
[[1,0,0,0,0],
 [0,2,0,0,0],
 [0,0,3,0,0]]
```

**练习 2**：`diagsvd(np.array([1.,2.,3.]), 5, 3)` 与 `diagsvd(np.array([1.,2.,3.]), 2, 3)` 各会发生什么？

**答案**：前者 `len(s)=3==N=3`，走 `np.r_` 底部补 2 行零，得 `5×3`。后者 `len(s)=3` 既不等于 `M=2` 也不等于 `N=3`……实际上 `3==N=3` 成立，会走列分支返回 `2×3`——但此时 `s` 长度 3 与 `M=2` 不自洽（对角阵应是 3×3 却要塞进 2 行），会触发 NumPy 形状错误。可见 `diagsvd` 要求 `len(s)` 恰等于 `M` 或 `N` 之一。

---

### 4.3 列空间正交基 orth 与 rcond 秩截断

#### 4.3.1 概念说明

`orth(A)` 返回矩阵 \(A\) 的**列空间**（range）的一组**标准正交基**，形状 `(M, K)`，其中 `K` 是由 `rcond` 决定的「数值秩」。它的核心思想是：SVD 的左奇异向量里，对应「非零」奇异值的那几列，恰好就是列空间的标准正交基。

为什么用 SVD 而不是 Gram-Schmidt？因为 SVD 天然给出数值稳定的标准正交基，并且能通过奇异值大小优雅地判断「哪些方向是真实的列空间，哪些只是噪声」。

#### 4.3.2 核心流程

```
1. svd(A, full_matrices=False) 得到 (u, s, vh)，其中 u 形状 (M, K), K=min(M,N)
2. 若 rcond 为 None：rcond = eps * max(M, N)
3. tol = max(s) * rcond           # 绝对阈值
4. num = sum(s > tol)             # 数值秩
5. 取 u 的前 num 列 → 标准正交基
```

关键在于第 3、4 步：相对阈值 `tol` 是「最大奇异值的 rcond 倍」，比它小的奇异值被视为零，对应的左奇异向量被丢弃。

#### 4.3.3 源码精读

**orth 主体** [_decomp_svd.py:L396-L403](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_svd.py#L396-L403)：
- L396 调 `svd(A, full_matrices=False)`——这里**故意**用 `False`，因为列空间只需 `u` 的前 `K=min(M,N)` 列，无需完整方阵 \(U\)，省内存。`svdvals` 默认走 `gesdd`，`orth` 也跟着用。
- L398–L399 默认 `rcond = eps * max(M, N)`，其中 `M, N` 来自 `u.shape[0]` 与 `vh.shape[1]`。
- L400 `tol = np.amax(s, initial=0.) * rcond`，`initial=0.` 保证 `s` 为空时不报错。
- L401 `num = np.sum(s > tol, dtype=int)`——严格大于阈值才计入有效秩。
- L402 `Q = u[:, :num]`，切片即得列空间基。

注意 `orth` 被 `@_apply_over_batch(('A', 2))` 装饰 [_decomp_svd.py:L357](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_svd.py#L357)，支持 `(…, M, N)` 批量输入，每片独立算秩与基。

#### 4.3.4 代码实践

**实践目标**：用 `rcond` 控制「数值秩」，观察列空间维度如何随之变化。

**操作步骤**：

```python
# 示例代码
import numpy as np
from scipy.linalg import orth

rng = np.random.RandomState(1)
# 构造一个秩为 5 的 10x10 矩阵，再叠加一个微小的秩 1 扰动
X = rng.rand(10, 5) @ rng.rand(5, 10)
X = X + 1e-4 * rng.rand(10, 1) @ rng.rand(1, 10)

Q1 = orth(X, rcond=1e-3)   # 扰动小于阈值 → 视为噪声
Q2 = orth(X, rcond=1e-6)   # 扰动大于阈值 → 多算 1 维
print(Q1.shape)            # (10, 5)
print(Q2.shape)            # (10, 6)

# 验证 Q 是标准正交的
print(np.allclose(Q1.T @ Q1, np.eye(5)))   # True
```

**需要观察的现象**：`rcond` 越小（截断越宽松），`Q` 的列数越多；列向量两两正交、单位长。

**预期结果**：`Q1.shape=(10,5)`、`Q2.shape=(10,6)`（这正是 `tests/test_decomp.py` 里 `_check_orth` 的断言依据，见 [test_decomp.py:L3283-L3287](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/tests/test_decomp.py#L3283-L3287)）。

> 说明：此例改编自官方测试用例，数值上稳定可复现，建议本地运行确认。

#### 4.3.5 小练习与答案

**练习 1**：对一个全 1 的 `4×2` 矩阵 `X = np.ones((4,2))`，`orth(X)` 的形状和内容是什么？

**答案**：全 1 矩阵列空间是一维的（两列相同），数值秩为 1，故 `orth(X)` 形状为 `(4, 1)`，内容是归一化的全 $\frac12$ 向量 `[[0.5],[0.5],[0.5],[0.5]]`（见 [test_decomp.py:L3263-L3275](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/tests/test_decomp.py#L3263-L3275) 的 `_check_orth` 断言）。

**练习 2**：为什么 `orth` 用 `full_matrices=False` 而 `null_space` 用 `full_matrices=True`？

**答案**：列空间基取自 `u` 的前若干列，`full_matrices=False` 给出的 `u` 形状 `(M, K)` 已足够且更省；而零空间基取自 `Vh` 的**后**若干行（`vh[num:, :]`），必须用到完整的 `N×N` 行空间 \(V^H\)，所以 `null_space` 必须 `full_matrices=True` 才能取到对应零空间的右奇异向量。

---

### 4.4 零空间 null_space

#### 4.4.1 概念说明

`null_space(A)` 返回 \(A\) 的**零空间**（满足 \(Ax=0\) 的全体 \(x\)）的一组标准正交基，形状 `(N, K)`，`K` 是零空间的维数（= `N - 数值秩`）。与 `orth` 对偶：`orth` 取左奇异向量对应大奇异值的列，`null_space` 取右奇异向量对应小奇异值的列。

#### 4.4.2 核心流程

```
1. svd(A, full_matrices=True) 得到 (u, s, vh)，vh 形状 (N, N)
2. rcond 默认 eps * max(M, N)
3. tol = max(s) * rcond
4. num = sum(s > tol)              # 数值秩 r
5. Q = vh[num:, :].T.conj()        # vh 的后 (N-r) 行转置共轭 → 零空间基 (N, N-r)
```

第 5 步是关键：`vh` 的前 `num` 行对应非零奇异值（行空间），后 `N-num` 行对应零奇异值（零空间）。转置共轭是因为 `vh` 是 \(V^H\)，我们要的是 \(V\) 的列。

#### 4.4.3 源码精读

**null_space 主体** [_decomp_svd.py:L474-L482](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_svd.py#L474-L482)：
- L474 调 `svd(A, full_matrices=True, overwrite_a=..., check_finite=..., lapack_driver=...)`——与 `orth` 不同，这里用 `full_matrices=True` 以拿到完整的 `N×N` 的 `vh`，并且**透传** `overwrite_a`/`check_finite`/`lapack_driver` 三个参数，所以 `null_space` 的签名比 `orth` 多了它们 [_decomp_svd.py:L406-L408](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_svd.py#L406-L408)。
- L477–L480 与 `orth` 完全相同的 `rcond`/`tol`/`num` 逻辑。
- L481 `Q = vh[num:,:].T.conj()`——切片后 `num` 行再转置共轭。`.conj()` 对实矩阵无影响，对复矩阵保证零空间基是 \(V\) 的列而非 \(V^H\) 的行。

同样被 `@_apply_over_batch(('A', 2))` 装饰 [_decomp_svd.py:L406](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_svd.py#L406)。

#### 4.4.4 代码实践

**实践目标**：提取零空间并验证 \(AZ \approx 0\)、\(Z\) 标准正交。

**操作步骤**：

```python
# 示例代码
import numpy as np
from scipy.linalg import null_space

rng = np.random.RandomState(1)
X = rng.randn(1 + 10//2, 10)            # 6x10，秩 6，零空间维数 10-6=4
Z = null_space(X)
print(Z.shape)                           # (10, 4)
print(np.allclose(X @ Z, 0, atol=1e-10)) # True：AZ = 0
print(np.allclose(Z.T @ Z, np.eye(4), atol=1e-12))  # True：标准正交

# 用 rcond 控制：对秩亏损矩阵
X2 = rng.rand(10, 5) @ rng.rand(5, 10) + 1e-4 * rng.rand(10,1) @ rng.rand(1,10)
print(null_space(X2, rcond=1e-3).shape)  # (10, 5)
print(null_space(X2, rcond=1e-6).shape)  # (10, 4)
```

**需要观察的现象**：`X @ Z` 接近零矩阵；`Z.T @ Z` 接近单位阵；`rcond` 越小零空间维数越大（与 `orth` 的列数互补：`orth` 列数 + `null_space` 列数 = `N`）。

**预期结果**：形状与注释一致，两个 `allclose` 为 `True`。这些正是 [test_decomp.py:L3336-L3359](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/tests/test_decomp.py#L3336-L3359) 中 `TestNullSpace.test_null_space` 的断言。

> 说明：请本地运行确认。

#### 4.4.5 小练习与答案

**练习 1**：`A = np.array([[1,1],[1,1]])` 的 `null_space(A)` 是什么？

**答案**：`A` 秩为 1，零空间维数 1，基为 $\frac{1}{\sqrt2}[1, -1]^T$（符号可能相反）。这正是 docstring 示例的结果 [_decomp_svd.py:L450-L454](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_svd.py#L450-L454)。

**练习 2**：`null_space` 为什么必须用 `full_matrices=True`？如果误用 `False` 会怎样？

**答案**：零空间基取自 `vh[num:, :]`。`full_matrices=False` 时 `vh` 形状是 `(K, N)`，`K=min(M,N)`；当 `M < N`（行少于列）时 `K < N`，`vh` 行数不足以覆盖零空间所需的后 `N-r` 行，会取到错误的行甚至越界。只有 `full_matrices=True` 保证 `vh` 是完整的 `N×N`，才能正确切出零空间。

---

### 4.5 子空间夹角 subspace_angles

#### 4.5.1 概念说明

`subspace_angles(A, B)` 计算两个矩阵的**列空间**之间的**主夹角**（principal angles）\(\theta_1 \ge \theta_2 \ge \dots\)，返回弧度数组，形状 `(min(N, K),)`，按降序排列。

主夹角的几何含义：\(\cos\theta_i\) 是两个子空间「最接近的一对单位向量」内积的最大值（再取次大值……）。两个子空间重合则 \(\theta=0\)，正交则 \(\theta=\pi/2\)。算法依据是 Knyazev & Argentati (2002) 的论文（见源码 References）。

#### 4.5.2 核心流程

源码用「正交化 + 两次 SVD」的稳健算法，避免了直接对 \(A^H B\) 做 SVD 在小角度时的数值不稳：

```
1. QA = orth(A), QB = orth(B)        # 先把两子空间各自正交化
2. C = QA^H @ QB                      # 子空间之间的「余弦核」
3. sigma = svdvals(C)                 # 余弦的奇异值（即 cos θ 的候选）
4. 构造残差矩阵 B = (QB - QA@C) 或 (QA - QB@C^H)，取较「胖」的那一边
5. mu = svdvals(B)                    # 正弦的奇异值（即 sin θ 的候选）
6. 对每个角度：若 sigma^2 >= 0.5 用 arcsin(mu)（小角度更稳），否则用 arccos(sigma_reversed)
7. 返回 theta（降序）
```

第 6 步的分段策略是数值精度的关键：大角度时 \(\cos\theta\) 对角度敏感（用 `arccos`），小角度时 \(\sin\theta\) 对角度敏感（用 `arcsin`），以 \(0.5\)（即 \(\cos^2\theta = 0.5\)，\(\theta=45°\)）为分界。

#### 4.5.3 源码精读

**整体结构** [_decomp_svd.py:L550-L590](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_svd.py#L550-L590)：

- **步骤 1：正交化两子空间** [_decomp_svd.py:L553-L566](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_svd.py#L553-L566)：`QA = orth(A)`、`QB = orth(B)`，并校验二者行数相同。先把子空间正交化是为了让后续 `QA^H @ QB` 的奇异值落在 `[0,1]`，对应 \(\cos\theta\)。

- **步骤 2：余弦核的奇异值** [_decomp_svd.py:L569-L570](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_svd.py#L569-L570)：`QA_H_QB = QA.T.conj() @ QB`，`sigma = svdvals(QA_H_QB)`。这些 `sigma` 就是 \(\cos\theta_i\) 的候选（降序）。

- **步骤 3：构造正弦残差矩阵** [_decomp_svd.py:L573-L576](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_svd.py#L573-L576)：取两个正交基中**列数较多**的一边减去其在另一边的投影，得到与另一子空间正交的残差，其奇异值即 \(\sin\theta_i\) 的候选。`if QA.shape[1] >= QB.shape[1]` 决定用哪一边构造。

- **步骤 4：正弦核的奇异值** [_decomp_svd.py:L580-L584](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_svd.py#L580-L584)：`mask = sigma**2 >= 0.5`，只有存在大 \(\cos\)（即小角度）时才值得算 `arcsin(svdvals(B))`，否则 `mu_arcsin = 0.`。`svdvals(B, overwrite_a=True)` 顺手开了覆写省内存。

- **步骤 5：分段合成主夹角** [_decomp_svd.py:L588-L589](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_svd.py#L588-L589)：`theta = np.where(mask, mu_arcsin, np.arccos(np.clip(sigma[::-1], -1., 1.)))`。注意 `sigma[::-1]` 把余弦**反转**——因为最小的 \(\cos\) 对应最大的角度 \(\theta\)，降序返回。`np.clip(..., -1, 1)` 防止浮点误差把 `arccos` 参数顶出 `[-1,1]` 产生 NaN。

它也被 `@_apply_over_batch(('A', 2), ('B', 2))` 装饰 [_decomp_svd.py:L485](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_svd.py#L485)，支持两个批量矩阵的逐片配对计算。

#### 4.5.4 代码实践

**实践目标**：验证三类典型情形——正交子空间（\(\pi/2\)）、同一子空间（\(0\)）、部分重叠。

**操作步骤**：

```python
# 示例代码
import numpy as np
from scipy.linalg import hadamard, subspace_angles

H = hadamard(8)                  # 列两两正交
A = H[:, :3]
B = H[:, 3:]

# (1) 正交子空间：夹角应为 π/2
print(np.rad2deg(subspace_angles(A, B)))   # [90. 90. 90.]

# (2) 子空间与自身：夹角应为 0
print(np.rad2deg(subspace_angles(A, A)))   # [0. 0. 0.]

# (3) 重叠子空间
rng = np.random.default_rng(7)
x = rng.standard_normal((4, 3))
print(np.rad2deg(subspace_angles(x[:, :2], x[:, [2]])))  # 某个 0~90 之间的角度
```

**需要观察的现象**：Hadamard 矩阵列正交，故 `A`、`B` 列空间正交，三个夹角都是 `90°`；子空间对自身夹角为 `0`。

**预期结果**：前两组打印 `[90. 90. 90.]` 与 `[0. 0. 0.]`，这正是 [test_decomp.py:L3382-L3390](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/tests/test_decomp.py#L3382-L3390) 的断言；第三组为一个具体角度。

> 说明：请本地运行确认第三组数值。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `subspace_angles` 不直接对 `A.T @ B` 做 SVD，而要先 `orth` 再算？

**答案**：直接用 `A`、`B` 时，若它们列向量非单位长或彼此相关，`A^H B` 的奇异值会超过 1 或偏离真正的 \(\cos\theta\)，导致 `arccos` 越界。先 `orth` 把两个子空间各自化成标准正交基，才能保证 `QA^H QB` 的奇异值严格落在 `[0,1]`，正确对应 \(\cos\theta_i\)。

**练习 2**：返回的 `theta` 为什么要降序，而源码第 589 行却把 `sigma` 反转（`sigma[::-1]`）？

**答案**：`sigma` 是 `cos` 值降序（大→小），而角度 \(\theta=\arccos(\cos\theta)\) 与 \(\cos\) 反向——最小的 \(\cos\) 对应最大的 \(\theta\)。要把 \(\theta\) 排成降序，就需对 `cos` 升序取 `arccos`，等价于把降序的 `sigma` 反转后再 `arccos`。

---

## 5. 综合实践

把本讲六个函数串起来，完成一个「秩亏损矩阵的完整分析」任务。

**任务**：构造一个明确秩亏损的矩阵，完成下面全部步骤并解释每一步输出。

```python
# 示例代码（综合实践）
import numpy as np
from scipy.linalg import svd, svdvals, diagsvd, orth, null_space, subspace_angles

rng = np.random.default_rng(42)
# 构造真实秩 3 的 5x6 矩阵：两个低秩因子相乘
L = rng.standard_normal((5, 3))
R = rng.standard_normal((3, 6))
A = L @ R                      # 理论秩 3
print("理论秩:", 3)

# 1) 奇异值：应恰有 3 个明显非零，3 个为 ~0
s = svdvals(A)
print("奇异值:", np.round(s, 4))
print("数值秩(rcond默认):", np.sum(s > s[0] * np.finfo(float).eps * max(A.shape)))

# 2) 完整 SVD 并用 diagsvd 重构
U, s2, Vh = svd(A)
print("重构误差:", np.linalg.norm(A - U @ diagsvd(s2, *A.shape) @ Vh))

# 3) 列空间与零空间
Q = orth(A)                    # 形状应为 (5, 3)
Z = null_space(A)             # 形状应为 (6, 3)
print("orth 形状:", Q.shape, " null_space 形状:", Z.shape)
print("AZ≈0:", np.allclose(A @ Z, 0, atol=1e-12))
print("Q标准正交:", np.allclose(Q.T @ Q, np.eye(Q.shape[1])))

# 4) 子空间夹角：orth(A) 张成的子空间与 A 本身的列空间应重合 → 夹角 0
print("与自身夹角(度):", np.rad2deg(subspace_angles(Q, A)))
```

**你需要回答**：
1. `svdvals` 给出的 6 个奇异值里，为何有 3 个是 `1e-15` 量级而非精确 0？
2. `orth(A)` 与 `null_space(A)` 的列数之和为何等于 `A` 的列数 6？
3. `subspace_angles(Q, A)` 为何接近 0？

**参考解释**：
1. 浮点运算的舍入误差让「本应为 0」的奇异值落在机器精度量级（`1e-15`），而非精确 0；它们小于 `tol = s[0]*eps*max(M,N)`，故被数值秩判定为零。
2. `orth(A)` 给数值列空间维数 \(r=3\)，`null_space(A)` 给零空间维数 \(N-r=6-3=3\)，二者之和 = 列数 \(N=6\)，这是线性代数基本定理（秩-零化度定理）的体现。
3. `Q = orth(A)` 与 `A` 张成同一个列空间，同一子空间的主夹角定义为 0。

---

## 6. 本讲小结

- `svd` 是本讲的枢纽：它本身是「校验→归一化→覆写门控→ILP64 溢出检查→委派 C++ 后端 `_batched_linalg._svd`→错误翻译」的薄壳，真正算 SVD 的是 C++ 后端，且原生支持批量维度。
- `full_matrices` 控制 \(U\)、\(V^H\) 是方阵还是「瘦长」；`compute_uv=False` 退化为只取奇异值；`lapack_driver` 在快的 `gesdd` 与稳的 `gesvd` 间取舍。
- `svdvals` 是 `svd(compute_uv=0)` 的语法糖；`diagsvd` 把奇异值向量撑回二维 $\Sigma$，方便重构。
- `orth`（列空间）取左奇异向量对应大奇异值的列，`null_space`（零空间）取右奇异向量对应小奇异值的行，二者都靠 `rcond`（默认 `eps*max(M,N)`）做数值秩截断；`orth` 用 `full_matrices=False`，`null_space` 必须 `full_matrices=True`。
- `subspace_angles` 用「先 `orth` 正交化、再算余弦核与正弦残差两次 SVD」的稳健算法，并在 `cos²θ` 是否 ≥ 0.5 处分段选择 `arcsin`/`arccos` 以保证小角度精度。
- 错误处理统一由 `_format_emit_errors_warnings` 完成：`info>0` 抛 `LinAlgError`（不收敛），`gesdd` 的 `info=-4` 提示 NaN，其余 `info<0` 报参数非法。

---

## 7. 下一步学习建议

1. **向下钻 C++ 后端**：本讲的 `svd` 把计算委派给 `_batched_linalg._svd`。要理解批量 SVD 的真正实现，请阅读 [u8-l1](u8-l1-batched-python-api.md)（批量接口与 `_apply_over_batch`）与 [u8-l2](u8-l2-batched-cpp-backend.md)（`src/_batched_linalg_module.cc` 与 `_linalg_svd.hh`）。
2. **横向学其他分解**：SVD 是矩阵分解族的一员，建议接着学 [u3-l1](u3-l1-lu-decomposition.md)（LU）、[u3-l3](u3-l3-qr-decomposition.md)（QR），对比它们与 SVD 在「正交性、秩揭示、成本」上的差异。
3. **向应用延伸**：SVD 是低秩近似的基石，[u9-l1](u9-l1-interpolative-decomp.md) 讲的插值分解（ID）可与之互转（`id_to_svd`），适合阅读 `interpolative.py` 进一步体会「SVD ↔ ID」的关系。
4. **源码阅读建议**：重点重读 [_decomp_svd.py:L17-L34](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_svd.py#L17-L34) 的错误聚合与 [_decomp_svd.py:L185-L199](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_svd.py#L185-L199) 的 ILP64 检查，这两处最能体现 `scipy.linalg`「Python 薄壳做防御性校验」的设计哲学。
