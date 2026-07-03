# 矩阵求逆、行列式与最小二乘

> 讲义 id：u2-l3　所属单元：u2 基础线性代数运算　阶段：beginner　前置：u2-l1

## 1. 本讲目标

学完本讲后，你应该能够：

1. 说清 `scipy.linalg.inv`、`det`、`lstsq` 三个函数的 Python 层做了什么、真正的数值计算交给谁。
2. 理解「精确求逆」与「伪逆（Moore-Penrose）」的本质区别，并能根据矩阵是否方阵、是否满秩选择 `inv` 还是 `pinv`/`pinvh`。
3. 读懂 `lstsq` 返回的四元组（解、残差、秩、奇异值）各自的含义。
4. 认识 `get_lapack_funcs` 这套 LAPACK 分发机制在当代 scipy.linalg 中的真实地位：高层 `inv/det/lstsq` 已迁移到 C++ 批量后端，但 `get_lapack_funcs` 仍是「手动直连 LAPACK」的标准入口。

## 2. 前置知识

本讲承接 u2-l1（范数与结构检测）和 u2-l2（`solve` 与 `assume_a` 调度），默认你已经理解下面这些由前几讲建立的术语，这里只做一句话回顾：

- **`check_finite`**：用 `asarray_chkfinite` 拦截 NaN/Inf，关掉可提速但有风险。
- **`overwrite_a`**：允许原地覆写输入以省内存；scipy.linalg 用「二维 + Fortran 列主序连续」的严格门控约束它（详见 u2-l2）。
- **`_batched_linalg`**：scipy.linalg 的 C++ 批量后端。Python 层只做校验与错误聚合，真正的 LAPACK 调用在这里完成。
- **`_format_emit_errors_warnings`**：u2-l2 引入的批量错误聚合函数，按「奇异 > LAPACK 内部错误 > 病态」优先级，分别抛 `LinAlgError` / `ValueError` / 发 `LinAlgWarning`。
- **`assume_a` 与 `structure`**：把字符串结构标签（`'gen'`/`'pos'`/`'sym'` …）翻译成整数编码，传给 C++ 后端选择最优 LAPACK 例程。
- **`LinAlgError` / `LinAlgWarning`**：复用 NumPy 的 `LinAlgError`，另定义 `LinAlgWarning` 报告病态。

如果你对「奇异矩阵」「超定/欠定方程组」「奇异值分解（SVD）」这些线性代数概念本身还不熟，建议先翻一遍教材对应章节；本讲侧重**读源码**，数学上只点到为止。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲涉及的函数 |
|------|------|----------------|
| [`_basic.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py) | 基础线性代数运算的 Python 薄层 | `inv`、`det`、`lstsq`、`pinv`、`pinvh`、`_format_emit_errors_warnings` |
| [`_decomp_svd.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_svd.py) | SVD 分解 | `svd`（被 `pinv` 内部调用） |
| [`lapack.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/lapack.py) | 底层 LAPACK 分发 | `get_lapack_funcs` |

一句话记住三者的关系：`_basic.py` 是「门面」，`_decomp_svd.py` 提供 `pinv` 需要的 SVD 能力，`lapack.py` 的 `get_lapack_funcs` 是「手动直连 LAPACK」的底层入口。

## 4. 核心概念与源码讲解

本讲拆成 5 个最小模块：4.1 `inv`、4.2 `det`、4.3 `lstsq`、4.4 `pinv`/`pinvh`、4.5 `get_lapack_funcs`。

---

### 4.1 inv：矩阵求逆

#### 4.1.1 概念说明

矩阵 \(A\) 的逆 \(A^{-1}\) 定义为满足 \(A A^{-1} = A^{-1} A = I\) 的矩阵。**只有方阵且非奇异（行列式 ≠ 0）时才存在精确逆。** 实践中要牢记两点：

1. **能用 `solve` 就别用 `inv`。** 求 \(x\) 满足 \(Ax=b\) 时，直接 `solve(A, b)` 比 `inv(A) @ b` 更快更稳——前者不需要显式构造完整逆矩阵。
2. `inv` 真正有用的场景是：你需要逆矩阵本身（例如做后续多次乘法、或理论分析），而不仅仅是解一个方程。

#### 4.1.2 核心流程

`inv` 的 Python 层逻辑非常薄，和 u2-l2 讲过的 `solve` 几乎是同一套模板：

```
inv(a):
  1. _asarray_validated + check_finite        # 校验、拦 NaN/Inf
  2. 校验至少 2 维、末两维方阵
  3. _normalize_lapack_dtype / _ensure_aligned_and_native   # dtype/内存对齐
  4. overwrite 门控: overwrite_a and (ndim==2) and F_CONTIGUOUS
  5. assume_a 字符串 → structure 整数 (同 solve 的编码表)
  6. inv_a, err_lst = _batched_linalg._inv(a1, structure, overwrite_a, lower)
  7. 若 err_lst 非空 → _format_emit_errors_warnings(err_lst)  # 奇异抛 LinAlgError
  8. 返回 inv_a
```

注意第 6 步：真正的求逆在 C++ 后端 `_batched_linalg._inv` 里完成，Python 层只负责「校验 → 编码 → 委派 → 汇报」。这与 u2-l2 讲的 `solve` 完全同构。

#### 4.1.3 源码精读

`inv` 的签名与文档串：[ `_basic.py:984`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L984)。注意 `assume_a=None`、`lower` 是仅关键字参数（`*` 之后）。

关键片段——`structure` 编码表，与 `solve` 共用同一套整数语义，必须和 C++ 端的枚举保持一致：

```python
# _basic.py:1107-1117
# keep the numbers in sync with C at `linalg/src/_common_array_utils.hh`
structure = {
    None: -1,
    'general': 0, 'gen': 0,
    'diagonal': 11,
    'upper triangular': 21,
    'lower triangular': 22,
    'pos' : 101,
    'sym' : 201,
    'her' : 211,
}[assume_a]
```

委托给 C++ 后端并汇报错误：[ `_basic.py:1119-1125`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L1119-L1125)

```python
# a1 is well behaved, invert it.
inv_a, err_lst = _batched_linalg._inv(a1, structure, overwrite_a, lower)

if err_lst:
    _format_emit_errors_warnings(err_lst)

return inv_a
```

`_format_emit_errors_warnings` 是 u2-l2 详解过的批量错误聚合器：[ `_basic.py:27-54`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L27-L54)。当某片矩阵奇异时，`dct["is_singular"]` 为真，最终抛出 `LinAlgError`。奇异时**抛异常**而非返回垃圾值，这是 `inv` 与 `det`（返回 0）最大的行为差异。

#### 4.1.4 代码实践

**目标**：验证 `inv` 的正确性，并触发奇异矩阵的 `LinAlgError`。

**步骤**：

```python
import numpy as np
from scipy.linalg import inv, LinAlgError

A = np.array([[4., 7.],
              [2., 6.]])
Ainv = inv(A)
print("A @ Ainv ≈ I ?", np.allclose(A @ Ainv, np.eye(2)))

# 故意构造奇异矩阵（两行成比例）
S = np.array([[1., 2.],
              [2., 4.]])
try:
    inv(S)
except LinAlgError as e:
    print("捕获 LinAlgError:", e)
```

**预期结果**：第一行打印 `True`；第二行打印 `捕获 LinAlgError: A singular matrix detected: ...`。

#### 4.1.5 小练习与答案

**练习 1**：`inv(A) @ b` 和 `solve(A, b)` 解同一个方程组，结果一样吗？该用哪个？
**答案**：数值上几乎一致（都基于 LU 分解），但应优先 `solve`——它不显式构造逆，更快、数值更稳，且遇到奇异矩阵时 `solve` 同样会报错。

**练习 2**：`inv` 接收一个 `shape=(2, 3, 3)` 的数组会怎样？
**答案**：被当作 `(2,)` 批的 3×3 矩阵，返回 `shape=(2, 3, 3)` 的逆。这是批量维度（batch dimensions）特性，后续 u8 会深入。

---

### 4.2 det：行列式

#### 4.2.1 概念说明

行列式 \(\det(A)\) 是把方阵映射到一个标量的函数。它有两个直接用途：**判断矩阵是否奇异**（\(\det = 0\) 即奇异）；以及衡量线性变换对体积的缩放因子。

数学上，LU 分解 \(PA = LU\) 给出 \(\det(A) = (-1)^{p} \prod_i U_{ii}\)，其中 \(p\) 是置换次数。这正是 scipy 计算 `det` 的算法：调 LAPACK 的 `getrf`（LU 分解），再把 \(U\) 的对角元相乘。源码注释写得很直白：[ `_basic.py:1162-1165`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L1162-L1165)。

#### 4.2.2 核心流程

```
det(a):
  1. np.asarray_chkfinite(a) if check_finite else np.asarray(a)
  2. 校验至少 2 维、末两维方阵
  3. _normalize_lapack_dtype1
  4. 空矩阵特判 → 返回 1.0（数学约定）
  5. 标量 (1,1) 特判 → 直接取元素
  6. det = _linalg_det(a1, overwrite_a)          # 即 _batched_linalg._det，内部 getrf
  7. 单精度 float32/complex64 → 提升为 double（防溢出）
  8. 2D 输入返回标量，否则返回批量结果
```

第 7 步的单精度提升是个值得记住的工程细节：两个大单精度数相乘可能溢出，提升到 `float64` 再相乘更安全。

#### 4.2.3 源码精读

`check_finite` 在 `det` 里的写法和 `inv` 略有不同——这里直接用 NumPy 而非 `_asarray_validated`：[ `_basic.py:1198`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L1198)

```python
a1 = np.asarray_chkfinite(a) if check_finite else np.asarray(a)
```

真正的计算委托给 C 后端（注意 import 时的别名 `_det as _linalg_det`，见 [`_basic.py:18`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L18)）：[ `_basic.py:1228`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L1228)

```python
det = _linalg_det(a1, overwrite_a)
```

单精度→双精度提升，防止对角元乘积溢出：[ `_basic.py:1230-1235`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L1230-L1235)

```python
# Promote single precision to double to prevent overflows
if det.dtype.char == 'f':
    det = det.astype(np.float64)
elif det.dtype.char == 'F':
    det = det.astype(np.complex128)
```

注意 `det` **不抛异常**——奇异矩阵返回 `0.0`（优雅降级），这与 `inv` 的「抛 `LinAlgError`」形成对比。

#### 4.2.4 代码实践

**目标**：观察 `det` 的奇异返回与单精度提升。

**步骤**：

```python
import numpy as np
from scipy.linalg import det

# 奇异矩阵：det 返回 0，不报错
print("奇异 det:", det(np.array([[1, 2], [2, 4]])))   # 0.0

# 单精度提升：结果类型是 float64
d32 = det(np.array([[1., 2.], [3., 4.]], dtype=np.float32))
print("结果 dtype:", d32.dtype)   # float64
```

**预期结果**：`奇异 det: 0.0`；`结果 dtype: float64`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `det` 对奇异矩阵返回 0，而 `inv` 抛异常？
**答案**：两者用途不同。`det` 本就是一个「探测信号」，0 是有意义的返回值（表示奇异）；`inv` 则要求结果可作逆使用，奇异时没有合法逆，只能报错。

**练习 2**：`det` 的返回类型对 `float32` 输入为什么变成 `float64`？
**答案**：LU 分解后对角元连乘容易让单精度溢出，源码显式把结果提升为 `float64`（或 `complex128`）。

---

### 4.3 lstsq：最小二乘求解

#### 4.3.1 概念说明

当方程组 \(Ax = b\) **无精确解**时（典型场景：超定方程组，行数 \(M\) > 列数 \(N\)），最小二乘法找一个 \(x\) 让残差 \(\|Ax - b\|_2\) 最小。scipy 的 `lstsq` 求解：

\[
x^* = \arg\min_x \|Ax - b\|_2
\]

底层用 LAPACK 的三个驱动之一：`gelsd`（默认，分治 SVD）、`gelsy`（QR 分解，常更快但不返回奇异值）、`gelss`（经典 SVD，慢但省内存）。

`lstsq` 返回四元组 `(x, residues, rank, s)`：`x` 是最小二乘解；`residues` 是残差平方和（仅 `M > N` 且满列秩时有效，否则 NaN/空）；`rank` 是有效秩；`s` 是 `a` 的奇异值。

#### 4.3.2 核心流程

```
lstsq(a, b, cond, lapack_driver):
  1. driver = lapack_driver or 'gelsd'; 校验 ∈ {'gelsd','gelsy','gelss'}
  2. _asarray_validated(a, b); _deprecate_dtypes; _ensure_dtype_cdsz (统一 a,b dtype)
  3. _normalize_lapack_dtype / _ensure_aligned_and_native
  4. 零尺寸特判 → 直接返回
  5. b 升 2D；广播批量维度对齐 a 与 b
  6. overwrite 门控 (a: 2D+F序; b: 额外要求 m>=n 超定)
  7. cond = eps 或给定值
  8. x, rank, S, err_lst = _batched_linalg._lstsq(a1, b1, cond, driver, ...)
  9. _format_emit_errors_warnings(err_lst)
  10. 计算 residuals：超定且满秩时 = ||b - a x||^2；非满秩 → NaN
  11. 返回 (x1, residuals, rank, S)
```

第 10 步的残差计算有个反直觉的细节：LAPACK 把残差信息塞进 `x` 末尾的「填充行」里，Python 层用 `x[..., n:, :]` 的平方和算出残差。

#### 4.3.3 源码精读

驱动选择与校验，默认 `gelsd`（通过给函数对象挂属性实现默认值，是一种常见技巧）：[ `_basic.py:1386-1390`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L1386-L1390) 与 [ `_basic.py:1468`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L1468)

```python
driver = lapack_driver
if driver is None:
    driver = lstsq.default_lapack_driver
if driver not in ('gelsd', 'gelsy', 'gelss'):
    raise ValueError(f'LAPACK driver "{driver}" is not found')
...
lstsq.default_lapack_driver = 'gelsd'
```

委托 C++ 后端并聚合错误：[ `_basic.py:1441-1446`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L1441-L1446)

```python
x, rank, S, err_lst = _batched_linalg._lstsq(
    a1, b1, cond, driver, overwrite_a, overwrite_b
)

if err_lst:
    _format_emit_errors_warnings(err_lst)
```

残差的「从填充行里抠出来」逻辑——非满列秩时强制置 NaN（LAPACK 对此不做承诺）：[ `_basic.py:1448-1455`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L1448-L1455)

```python
x1 = x[..., :n, :]
if m > n and lapack_driver != "gelsy":
    residuals = np.sum(x[..., n:, :] * x[..., n:, :].conj(), axis=-2)
    # LAPACK makes no promises about residuals for non full-column rank, set to NaN
    residuals[rank < n, :] = np.nan
else:
    residuals = np.zeros(batch_shape + (0,), dtype=x.dtype)
```

#### 4.3.4 代码实践

**目标**：拟合一条曲线，理解四元组返回。

**步骤**：

```python
import numpy as np
from scipy.linalg import lstsq

# 超定方程组：7 个数据点拟合 y = a + b*x^2
x = np.array([1, 2.5, 3.5, 4, 5, 7, 8.5])
y = np.array([0.3, 1.1, 1.5, 2.0, 3.2, 6.6, 8.6])
M = x[:, None] ** [0, 2]          # 设计矩阵, shape (7, 2) —— 超定

p, res, rnk, s = lstsq(M, y)
print("解 p =", p)        # [截距, x^2 系数]
print("秩 =", rnk)        # 2（满列秩）
print("奇异值 =", s)      # 2 个，s[0]/s[-1] 即条件数
print("残差平方和 =", res)  # 标量（M>N 且满秩时有效）
```

**预期结果**：`p ≈ [0.209, 0.120]`，`rnk = 2`，`res` 为一个小的非负标量。把驱动换成 `lapack_driver='gelsy'` 再跑一次，观察 `s` 是否变成 `None`。

#### 4.3.5 小练习与答案

**练习 1**：`lstsq` 的 `s` 在用 `gelsy` 驱动时为什么是 `None`？
**答案**：`gelsy` 基于 QR 分解，不计算奇异值，因此无法返回 `s`；只有基于 SVD 的 `gelsd`/`gelss` 才返回奇异值。

**练习 2**：`M <= N`（欠定/恰定）时 `residues` 是什么？
**答案**：空数组 `(0,)`。欠定系统总能精确拟合，残差恒为 0，故不返回。

---

### 4.4 pinv 与 pinvh：Moore-Penrose 伪逆

#### 4.4.1 概念说明

伪逆（Moore-Penrose pseudoinverse）\(A^+\) 是逆的「泛化版本」：**对任意形状、任意秩的矩阵都存在**，且当 \(A\) 可逆时 \(A^+ = A^{-1}\)。它满足四个 Moore-Penrose 条件：

\[
ABA = A,\quad BAB = B,\quad (AB)^H = AB,\quad (BA)^H = BA
\]

其中 \(B = A^+\)。

伪逆的最大价值是处理**秩亏损或非方阵**：对超定方程 \(Ax = b\)，\(x = A^+ b\) 恰好是最小二乘解；对欠定方程，它给出**最小范数解**。

scipy 提供两个函数：
- **`pinv`**：通用矩阵，基于 SVD。
- **`pinvh`**：专用于 Hermitian（或实对称）矩阵，基于特征值分解——更快、更省内存。

截断策略：若 \(\sigma_{\max}\) 是最大奇异值，阈值定为 \(\text{atol} + \text{rtol}\cdot\sigma_{\max}\)，低于阈值的奇异值视为 0（对应方向被丢弃）。`rtol` 默认为 `max(M, N) * eps`。

#### 4.4.2 核心流程

**`pinv`（向量化批处理）**：

```
pinv(a):
  1. u, s, vh = svd(a.conj(), full_matrices=False)   # 注意 a.conj() !
  2. atol 默认 0; rtol 默认 max(M,N)*eps
  3. maxS = max(s); 阈值 val = atol + maxS*rtol
  4. large = (s > val); rank = sum(large)
  5. 对 large 的奇异值取 1/s，其余置 0
  6. B = vh.mT @ (s[..., None] * u.mT)                # mT = 交换末两轴的矩阵转置
  7. return_rank ? (B, rank) : B
```

**`pinvh`（逐片处理）**：

```
pinvh(a):                       # 被 @_apply_over_batch(('a', 2)) 装饰
  1. s, u = eigh(a, lower, driver='ev')              # 特征值分解
  2. maxS = max(|s|); 阈值 val = atol + maxS*rtol    # rtol 默认 N*eps
  3. above_cutoff = |s| > val
  4. psigma_diag = 1 / s[above_cutoff]; 丢弃小特征值对应的特征向量
  5. B = (u * psigma_diag) @ u.conj().T
  6. return_rank ? (B, len(psigma_diag)) : B
```

两个函数都做「分解 → 阈值截断 → 重构」三步，区别只在分解手段（SVD vs 特征值）和批处理方式。

#### 4.4.3 源码精读

`pinv` 的核心：先对 `a.conj()` 做 SVD，再用阈值过滤奇异值。注意 `a.conj()` 和最后的 `.mT`（NumPy 的矩阵转置，交换最后两个轴）配合使用，是为了让重构出的 \(B\) 正确成为伪逆：[ `_basic.py:1583-1606`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L1583-L1606)

```python
u, s, vh = _decomp_svd.svd(a.conj(), full_matrices=False, check_finite=False)

atol = 0. if atol is None else atol
rtol = max(a.shape[-2:]) * np.finfo(u.dtype).eps if (rtol is None) else rtol
...
maxS = np.max(s, axis=-1, initial=0., keepdims=True)
val = atol + maxS * rtol

large = s > val
rank = np.sum(large, axis=-1)

# zero out small singular values, 1/s large singular values
np.divide(1, s, where=large, out=s)
s[~large] = 0

B = vh.mT @ (s[..., None] * u.mT)
```

`pinv` 内部调用的 `svd`，本身也走 `_batched_linalg._svd` 后端：[ `_decomp_svd.py:201-203`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_svd.py#L201-L203)。所以 `pinv` 的整条链路最终也落到 C++ 批量后端。

`pinvh` 用 `@_apply_over_batch` 装饰器实现批处理（对每个二维切片分别调用），并改用 `eigh` 做特征值分解——这是它比 `pinv` 更快的关键，因为对 Hermitian 矩阵特征值分解比通用 SVD 便宜得多：[ `_basic.py:1609-1611`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L1609-L1611) 与 [ `_basic.py:1678-1695`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L1678-L1695)

```python
@_apply_over_batch(('a', 2))
def pinvh(a, atol=None, rtol=None, lower=True, return_rank=False, check_finite=True):
    ...
    s, u = _decomp.eigh(a, lower=lower, check_finite=False, driver='ev')
    ...
    psigma_diag = 1.0 / s[above_cutoff]
    u = u[:, above_cutoff]
    B = (u * psigma_diag) @ u.conj().T
```

注意一个对比细节：`pinv` 没有装饰器，而是靠纯向量化（`np.max(s, axis=-1)`、`np.divide(..., where=large)`）原生支持批量维度，比 `pinvh` 的「逐片循环」更高效。

#### 4.4.4 代码实践

**目标**：构造超定方程组，验证 `lstsq` 解与 `pinv` 解一致；再用 `pinvh` 处理 Hermitian 矩阵。

**步骤**：

```python
import numpy as np
from scipy.linalg import lstsq, pinv, pinvh

rng = np.random.default_rng(0)
A = rng.standard_normal((9, 6))      # 超定, 9 行 6 列
b = rng.standard_normal(9)

# 1) lstsq 最小二乘解
x_lstsq, *_ = lstsq(A, b)

# 2) pinv 伪逆解
x_pinv = pinv(A) @ b

print("lstsq 与 pinv 解一致 ?", np.allclose(x_lstsq, x_pinv))

# 3) 验证 Moore-Penrose 四条件
B = pinv(A)
print("ABA=A :", np.allclose(A @ B @ A, A))
print("BAB=B :", np.allclose(B @ A @ B, B))

# 4) Hermitian 矩阵用 pinvh
H = A @ A.T               # 对称正定, 9x9
Bh = pinvh(H)
print("pinvh 满足 ABA=A :", np.allclose(H @ Bh @ H, H))
```

**预期结果**：四个布尔值均为 `True`。

#### 4.4.5 小练习与答案

**练习 1**：`pinv` 和 `inv` 的适用条件分别是什么？
**答案**：`inv` 仅适用于方阵且非奇异（否则抛 `LinAlgError`）；`pinv` 对任意形状、任意秩矩阵都适用，秩亏损时自动丢弃小奇异值方向。

**练习 2**：为什么 Hermitian 矩阵应该用 `pinvh` 而不是 `pinv`？
**答案**：`pinvh` 用特征值分解（`eigh`），对 Hermitian 结构有专门优化，比通用 SVD 更快更省内存；结果数学上等价。

**练习 3**：`pinv` 对批量输入 `(K, M, N)` 怎么处理？`pinvh` 呢？
**答案**：`pinv` 用纯向量化原生处理批量（对 `s` 沿 `axis=-1` 操作），高效；`pinvh` 靠 `@_apply_over_batch` 逐片循环处理，相对慢一些。

---

### 4.5 get_lapack_funcs：LAPACK 分发与手动调用

#### 4.5.1 概念说明

前面四个函数的 Python 层都不直接调 LAPACK——它们统一委托给 C++ 后端 `_batched_linalg`。但 scipy 仍然保留了一条「手动直连 LAPACK」的路径：`get_lapack_funcs`。

LAPACK 的命名约定是每个例程名以类型前缀开头：

| 前缀 | NumPy 类型 |
|------|-----------|
| `s` | float32 |
| `d` | float64 |
| `c` | complex64 |
| `z` | complex128 |

例如 `getrf`（LU 分解）实际有 `sgetrf/dgetrf/cgetrf/zgetrf` 四个版本。`get_lapack_funcs(('getrf',), (a,))` 会根据 `a` 的 dtype 自动选出正确前缀的版本返回。这是 scipy 处理「同一算法、多种数据类型」的核心分发机制。

为什么本讲要提它？因为理解了 `get_lapack_funcs`，你就理解了 `_batched_linalg` 后端「内部」在做什么：它本质上也是在为每片矩阵选对前缀的 LAPACK 例程并调用，只是用 C++ 写得更紧凑、能批量循环。`get_lapack_funcs` 是这个机制的 Python 可见版本。

#### 4.5.2 核心流程

```
get_lapack_funcs(names, arrays, dtype, ilp64):
  1. 解析 ilp64: 'preferred' → 用当前构建是否启用 ILP64 (HAS_ILP64)
  2. 根据 arrays/dtype 选 s/d/c/z 前缀
  3. ilp64=True  → 从 _flapack_64 找 (64 位整数)
     ilp64=False → 从 _flapack   找 (32 位整数)
  4. 返回函数对象（带 .typecode 和 .int_dtype 属性）
```

#### 4.5.3 源码精读

`get_lapack_funcs` 的签名与 ilp64 分支：[ `lapack.py:954`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/lapack.py#L954) 与 [ `lapack.py:1046-1062`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/lapack.py#L1046-L1062)

```python
if not ilp64:
    return _get_funcs(names, arrays, dtype,
                      "LAPACK", _flapack, "flapack", _lapack_alias,
                      ilp64=False)
else:
    ...
    return _get_funcs(names, arrays, dtype,
                      "LAPACK", _flapack_64, "flapack_64", _lapack_alias,
                      ilp64=True)
```

它**至今仍被 `_basic.py` 的结构化求解器使用**——例如 `solve_triangular` 用它选 `trtrs`：[ `_basic.py:375-379`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L375-L379)

```python
def _solve_triangular(a1, b1, trans=0, lower=False, unit_diagonal=False,
                      overwrite_b=False):
    ...
    trtrs, = get_lapack_funcs(('trtrs',), (a1, b1))
```

`solve_banded` 同样用它选 `gtsv`/`gbsv`：[ `_basic.py:509`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L509)。所以准确的说法是：**`inv/det/lstsq` 已迁移到 `_batched_linalg`，不再直接调 `get_lapack_funcs`；但 `solve_triangular`、`solve_banded`、`solveh_banded`、`matrix_balance` 等结构化路径仍在用它。**

#### 4.5.4 代码实践

**目标**：用 `get_lapack_funcs` 手动调用 LU 分解 `getrf`，理解 `det` 背后到底发生了什么。

**步骤**：

```python
import numpy as np
from scipy.linalg import get_lapack_funcs, det

A = np.array([[4., 7.], [2., 6.]])
# 选出匹配 A dtype (float64 → 'd') 的 getrf
getrf, = get_lapack_funcs(('getrf',), (A,))
print("前缀:", getrf.typecode)          # 'd' → dgetrf

lu, piv, info = getrf(A)                # LU 分解: PA = LU
print("info:", info)                    # 0 表示成功

# det = (-1)^置换次数 * U 对角元乘积
# getrf 返回的 lu 是 L(不含单位对角)与 U 压缩在一个矩阵里
U_diag = np.diag(lu)
n_swaps = sum(i != p - 1 for i, p in enumerate(piv))   # Fortran 下标从 1
manual_det = ((-1) ** n_swaps) * np.prod(U_diag)
print("手动 det =", manual_det)
print("scipy det =", det(A))
print("一致 ?", np.isclose(manual_det, det(A)))
```

**预期结果**：`前缀: d`，`info: 0`，两个 `det` 值一致。这就从底层验证了 `det`「getrf + 对角元相乘」的实现思路。**待本地验证**：不同 LAPACK 版本返回的 `piv` 编码细节可能略有差异，若手动值差一个符号，请检查置换次数的计算口径。

#### 4.5.5 小练习与答案

**练习 1**：传一个 `float32` 数组给 `get_lapack_funcs(('getrf',), (a,))`，返回函数的 `.typecode` 是什么？
**答案**：`'s'`（对应 `sgetrf`）。前缀由数组 dtype 决定。

**练习 2**：`inv` 还用 `get_lapack_funcs` 吗？
**答案**：当代 scipy 的 `inv` 不再直接用——它走 `_batched_linalg._inv`（C++ 后端）。`get_lapack_funcs` 现主要用于 `solve_triangular`/`solve_banded` 等结构化路径，以及用户手动调用。

---

## 5. 综合实践

把本讲的五个最小模块串起来。任务是：**给定一个数据矩阵，判断它是否需要求逆、伪逆还是最小二乘，并各跑一遍对比。**

```python
import numpy as np
from scipy.linalg import inv, det, lstsq, pinv, pinvh, LinAlgError

rng = np.random.default_rng(42)

# 场景 A: 方阵可逆
A = rng.standard_normal((5, 5))
print("A 的 det =", det(A))
try:
    Ainv = inv(A)
    print("inv 可用, ||A Ainv - I|| =", np.linalg.norm(A @ Ainv - np.eye(5)))
except LinAlgError:
    print("A 奇异")

# 场景 B: 超定方程组（行多于列），无精确解 → 用 lstsq 或 pinv
M = rng.standard_normal((12, 4))
b = rng.standard_normal(12)
x_lstsq, res, rank, s = lstsq(M, b)
x_pinv = pinv(M) @ b
print("超定: lstsq 与 pinv 一致 ?", np.allclose(x_lstsq, x_pinv))
print("超定: 有效秩 =", rank, " 条件数 =", s[0] / s[-1])

# 场景 C: Hermitian 矩阵 → pinvh 更优
H = M @ M.T                     # 12x12 对称半正定
B1 = pinv(H)
B2 = pinvh(H)
print("pinv 与 pinvh 结果一致 ?", np.allclose(B1, B2))

# 场景 D: 秩亏损矩阵
R = np.array([[1., 2., 3.],
              [2., 4., 6.],
              [1., 1., 1.]])
print("R 的 det =", det(R))     # 接近 0
try:
    inv(R)                      # 应抛异常
except LinAlgError:
    print("R 奇异, inv 不可用, 改用 pinv")
Rp = pinv(R, return_rank=True)
print("pinv 给出秩 =", Rp[1] if isinstance(Rp, tuple) else "?")
```

**观察要点**：
1. 场景 A 中 `det ≠ 0` 是 `inv` 成功的前提。
2. 场景 B 中超定系统的 `lstsq` 解与 `pinv(M) @ b` 完全一致——二者数学等价。
3. 场景 C 中 `pinv` 与 `pinvh` 对 Hermitian 矩阵给出一致结果，但 `pinvh` 内部更省。
4. 场景 D 中 `det ≈ 0`、`inv` 抛异常，但 `pinv` 仍能给出有效伪逆和秩。

## 6. 本讲小结

- `inv`、`det`、`lstsq` 的 Python 层都极薄：校验 → dtype/内存对齐 → `overwrite` 门控 → 委派 `_batched_linalg` C++ 后端 → 用 `_format_emit_errors_warnings` 聚合错误。
- `inv` 奇异时**抛 `LinAlgError`**；`det` 奇异时**返回 0**（且单精度结果提升为 double 防溢出）。解方程优先 `solve` 而非 `inv`。
- `lstsq` 求最小二乘解，默认驱动 `gelsd`，返回 `(x, residues, rank, s)`；残差仅在超定且满列秩时有效，否则为空或 NaN。
- `pinv`（通用，基于 SVD，向量化批处理）与 `pinvh`（Hermitian 专用，基于 `eigh`，逐片批处理）给出 Moore-Penrose 伪逆，对任意形状、任意秩矩阵都存在。
- `get_lapack_funcs` 按 dtype 选 `s/d/c/z` 前缀；当代 `inv/det/lstsq` 已迁移到 C++ 后端，但 `solve_triangular`/`solve_banded` 等仍在用它，它也是「手动直连 LAPACK」的标准入口。
- 决策树：方阵非奇异 → `inv`/`det`；超定/欠定/秩亏损 → `lstsq` 或 `pinv`；Hermitian → `pinvh`。

## 7. 下一步学习建议

- **横向扩展**：u3 单元（矩阵分解）会讲 LU、Cholesky、QR、SVD 的完整分解接口——本讲的 `det`/`inv` 背后就是 LU，`pinv` 背后就是 SVD，去 u3 看它们的「完整形态」。
- **深入底层**：u7-l1 会讲 `get_lapack_funcs` / `find_best_blas_type` 的完整分发逻辑（含 ILP64）；u8 会讲 `_batched_linalg` 的 C++ 实现——本讲反复出现的「C++ 后端」在那里被拆开。
- **建议继续阅读**：直接打开 [`_basic.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py) 通读 `inv`/`det`/`lstsq`/`pinv`/`pinvh` 五个函数，对照本讲感受「薄 Python 层 + 厚 C++ 后端」的分工。
