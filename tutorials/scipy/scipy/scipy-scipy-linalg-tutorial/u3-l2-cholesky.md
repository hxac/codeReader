# Cholesky 分解与带状 Cholesky

## 1. 本讲目标

本讲专讲 `scipy.linalg` 中**对称正定（Symmetric Positive-Definite，SPD）/ Hermitian 正定矩阵**的专用分解与求解。学完后你应当能够：

1. 说清楚 Cholesky 分解成立的**前提条件**（对称/Hermitian + 正定），以及它的代价为什么只有 LU 的一半。
2. 区分 `cholesky` 与 `cho_factor` 的差别——前者把另一半三角清零返回「干净」因子，后者保留另一半为「垃圾数据」以便直接喂给 `cho_solve`。
3. 用 `cho_factor` + `cho_solve` 的「一次分解、多次求解」两步模式，复用地求解 \(Ax=b\)。
4. 看懂带状矩阵的压缩存储格式，并能用 `cholesky_banded` / `cho_solve_banded` 求解稀疏的带状正定系统。

本讲承接 u2-l2 中建立的 `overwrite_a` 门控、`_datacopied`、错误聚合等公共机制，并把 u3-l1 中 LU 分解「一次分解多次求解」的复用思想迁移到正定矩阵这一更高效的特例上。

---

## 2. 前置知识

在进入源码前，先用通俗语言把几个关键概念讲清楚。

### 2.1 对称 / Hermitian 与正定

- **对称矩阵**：实矩阵满足 \(A = A^{T}\)，即 \(a_{ij} = a_{ji}\)。
- **Hermitian 矩阵**：复矩阵满足 \(A = A^{H}\)，其中 \(A^{H}\) 是「转置再取共轭」。实矩阵时 Hermitian 就退化为对称。
- **正定**：对任意非零向量 \(x\)，都有 \(x^{H} A x > 0\)。直观上，这要求矩阵的所有特征值都严格为正，几何上对应一个「处处向上凸」的二次型碗。

判断一个矩阵是否正定，Cholesky 分解本身就是一种试金石：**分解能成功 ⟺ 矩阵是（Hermitian）正定的**。

### 2.2 三角矩阵与回代

下三角矩阵 \(L\) 指对角线以上全为 0；上三角矩阵 \(U\) 指对角线以下全为 0。解三角方程组（如 \(L y = b\)）非常便宜，只需按顺序**前代/回代**（forward/back substitution），复杂度是 \(O(n^{2})\) 而非一般求解的 \(O(n^{3})\)。`cho_solve` 的高效正来源于此。

### 2.3 LAPACK 的 potrf / potrs / pbtrf / pbtrs

LAPACK 给 Cholesky 相关计算准备了一族以 `po`（正定）开头的例程：

| 例程 | 作用 | 本讲对应 Python 函数 |
|------|------|----------------------|
| `potrf` | POsitive-definite Triangular Factor：算 Cholesky 因子 | `cholesky` / `cho_factor` |
| `potrs` | POsitive-definite Triangular Solve：用因子解方程 | `cho_solve` |
| `pbtrf` | Positive-definite Banded Triangular Factor：带状版分解 | `cholesky_banded` |
| `pbtrs` | Positive-definite Banded Triangular Solve：带状版求解 | `cho_solve_banded` |

> 关于 `get_lapack_funcs` 如何按 dtype 给这些名字加 `s/d/c/z` 前缀，详见 u7-l1。本讲只需知道：调用 `get_lapack_funcs(('potrs',), (c, b))` 会根据输入数据类型返回 `spotrs`/`dpotrs`/`cpotrs`/`zpotrs` 之一。

---

## 3. 本讲源码地图

本讲的所有 Python 逻辑都集中在一个文件里：

| 文件 | 作用 |
|------|------|
| [`_decomp_cholesky.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_cholesky.py) | `cholesky`、`cho_factor`、`cho_solve`、`cholesky_banded`、`cho_solve_banded` 五个公共函数，以及它们共用的私有助手 |

另外两个「下游」文件在源码精读中会被点到，帮助你看清调用链尽头：

| 文件 | 作用 |
|------|------|
| [`src/_linalg_cholesky.hh`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/src/_linalg_cholesky.hh) | C++ 批量后端，真正调用 LAPACK `potrf` 的地方 |
| [`tests/test_decomp_cholesky.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/tests/test_decomp_cholesky.py) | 专属测试，本讲实践会借用其中的小矩阵 |

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1** 稠密 Cholesky 分解：`cholesky` 与 `cho_factor`
- **4.2** 复用因子求解：`cho_solve`
- **4.3** 带状 Cholesky：`cholesky_banded` 与 `cho_solve_banded`

---

### 4.1 稠密 Cholesky 分解：cholesky 与 cho_factor

#### 4.1.1 概念说明

对一个 Hermitian 正定矩阵 \(A\)，Cholesky 分解把它写成

\[
A = L L^{H}
\]

其中 \(L\) 是对角元为正实数的下三角矩阵，\(L^{H}\) 是它的共轭转置。也可以等价地写成上三角形式

\[
A = U^{H} U
\]

由 `lower` 参数选择要哪一种。两种形式是同一件事的两面，数学上完全等价，区别只在于「因子存在输出矩阵的哪一半」。

**为什么 Cholesky 值得单独学？** 三点优势：

1. **更快**：计算量约 \(\frac{1}{3}n^{3}\) 浮点运算，是 LU 分解（\(\frac{2}{3}n^{3}\)）的一半，因为只需处理一半的元素。
2. **更稳**：正定矩阵无需主元（pivoting），算法天然数值稳定。
3. **自带正定性检测**：只要分解过程中出现对负数开方（即某顺序主子式非正），`info > 0`，立刻知道矩阵不正定。

**存在性与唯一性**：\(A\) 是 Hermitian 正定 ⟺ Cholesky 因子存在；在「对角元取正」的约定下，因子是唯一的。

#### 4.1.2 核心流程

按列顺序计算的 Cholesky–Banachiewicz 算法（下三角情形）：

\[
L_{jj} = \sqrt{\, A_{jj} - \sum_{k=0}^{j-1} L_{jk}\,\overline{L_{jk}} \,}
\]

\[
L_{ij} = \frac{1}{L_{jj}} \left( A_{ij} - \sum_{k=0}^{j-1} L_{ik}\,\overline{L_{jk}} \right), \qquad i > j
\]

若根号内为负，说明第 \(j\) 个顺序主子式非正，矩阵不正定，分解失败（对应 LAPACK 返回 `info = j`）。

scipy 这两个函数的整体流程是「Python 薄壳 + C++/LAPACK 内核」：

```
cholesky(a, lower, overwrite_a, check_finite)
  └─ _cholesky(... clean=True ...)        # 共用私有函数
       ├─ asarray / 方阵校验 / dtype 归一化
       ├─ overwrite_a 门控（仅 2D 且连续才放行）
       ├─ _batched_linalg._cholesky(...)   # C++ 后端 → LAPACK potrf
       └─ 若有 err_lst → _check_format_errors_warnings → LinAlgError

cho_factor(a, lower, overwrite_a, check_finite)
  └─ _cholesky(... clean=False ...)        # 不清零另一半三角
       └─ 返回 (c, ret_lower)              # 直接喂给 cho_solve
```

`cholesky` 与 `cho_factor` 调用的是**同一个**私有函数 `_cholesky`，唯一差别是一个开关 `clean`：

| | `cholesky` | `cho_factor` |
|---|---|---|
| `clean` | `True` | `False` |
| 另一半三角 | 显式清零 | 保留（可能是旧数据/垃圾） |
| 返回值 | 仅因子 `c` | 元组 `(c, lower)`，可直接喂 `cho_solve` |
| 用途 | 想要干净的因子、查看、重构 \(A\) | 为 `cho_solve` 做准备，省掉清零开销 |

#### 4.1.3 源码精读

先看共用私有函数 [`_cholesky`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_cholesky.py#L26-L55)，它是两个公共函数的全部「干货」：

```python
def _cholesky(a, lower=False, overwrite_a=False, clean=True, check_finite=True):
    # sanity checks
    a1 = _asarray_validated(a, check_finite=check_finite)
    a1 = np.atleast_2d(a1)
    if a1.shape[-1] != a1.shape[-2]:
        raise ValueError(f"Expected a square matrix or batch thereof, got {a1.shape=}")
    ...
    a1, overwrite_a = _normalize_lapack_dtype(a1, overwrite_a)
    a1, overwrite_a = _ensure_aligned_and_native(a1, overwrite_a)
    overwrite_a = (overwrite_a and (a1.ndim == 2)
                   and (a1.flags["F_CONTIGUOUS"] or a1.flags["C_CONTIGUOUS"]))
    ...
    # Heavy lifting
    c, err_lst = _batched_linalg._cholesky(a1, lower, overwrite_a, clean)
    if err_lst:
        _check_format_errors_warnings("potrf", err_lst)
    return c
```

要点逐条说明：

- [`shape[-1] != shape[-2]`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_cholesky.py#L33-L34)：校验最后两维相等（支持批处理维度，所以用 `shape[-1]/[-2]` 而非 `shape[0]/[1]`）。
- [第 38–39 行](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_cholesky.py#L38-L39)的 `_normalize_lapack_dtype` 和 `_ensure_aligned_and_native`：把 dtype 规范到 LAPACK 能吃的类型、并确保内存对齐与原字节序，否则底层例程会出错或被迫拷贝。
- [第 40–41 行](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_cholesky.py#L40-L41)就是 u2-l2 讲过的 **`overwrite_a` 门控**：只有「2D 且 F 序或 C 序连续」时才允许真正原地覆写。批量（≥3D）输入即使你传 `overwrite_a=True` 也会被降级为 `False`，以免污染用户的多片数据。
- [第 50 行](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_cholesky.py#L50)：真正的数值计算在 C++ 后端 `_batched_linalg._cholesky` 里完成，它内部循环每片矩阵调用 LAPACK `potrf`。注意这里返回的是 `err_lst`（错误列表）而非单个 `info`，这是为了批量场景下收集每一片的失败信息。

再看清零开关如何决定两个公共函数的差别。[`cholesky`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_cholesky.py#L119-L122) 传 `clean=True`：

```python
def cholesky(a, lower=False, overwrite_a=False, check_finite=True):
    # `clean = True` represents setting other triangle to 0.
    c = _cholesky(a, lower=lower, overwrite_a=overwrite_a, clean=True,
                check_finite=check_finite)
    return c
```

而 [`cho_factor`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_cholesky.py#L211-L219) 传 `clean=False`，并多返回一个 `lower` 标志（为批量场景做了 `np.tile` 广播）：

```python
def cho_factor(a, lower=False, overwrite_a=False, check_finite=True):
    # `clean=False` to represent that it is not necessary to set other triangle to 0.
    c = _cholesky(a, lower=lower, overwrite_a=overwrite_a, clean=False,
                    check_finite=check_finite)
    # broadcast `lower` argument for backwards compat
    batch_shape = a.shape[:-2]
    ret_lower = np.tile(lower, reps=batch_shape)
    return c, ret_lower
```

> **关键提醒**：`cho_factor` 在 `overwrite_a=True` 时，返回矩阵的「另一半三角」里可能含有**随机/旧数据**（因为省掉了清零）。所以拿到 `cho_factor` 的结果后**不要**直接打印或当干净因子用，它的唯一正确去处是作为 `cho_solve` 的第一个参数。

最后看错误聚合。Cholesky 失败时走的是本文件私有的 [`_check_format_errors_warnings`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_cholesky.py#L18-L23)，它比 u2-l2 中 `_basic.py` 的 `_format_emit_errors_warnings` 简单——只把失败片号和 LAPACK `info` 拼进消息，然后直接抛 `LinAlgError`：

```python
def _check_format_errors_warnings(routine_name, err_lst):
    msg = (
        f"Internal {routine_name} return info = {[e['lapack_info'] for e in err_lst]} "
        f"for slices {[e['num'] for e in err_lst]}."
    )
    raise LinAlgError(msg)
```

至于 C++ 后端如何真正调用 `potrf`，可见 [`src/_linalg_cholesky.hh` 第 4–6 行](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/src/_linalg_cholesky.hh#L4-L6) 的模板函数 `_cholesky`。它有个值得了解的细节（见该文件第 38–39 行注释）：**LAPACK 的 `potrf` 按 Fortran 列主序解释数据，而 scipy 约定返回 C 行序结果**，因此后端会「翻转 `uplo`」让因子落在用户期望的那一半三角。这意味着 Python 层 `lower=True/False` 的语义，与实际传给 `potrf` 的 `uplo` 并非字面相同，而是经过了这次行列序翻转。

#### 4.1.4 代码实践

**实践目标**：亲手验证 \(A = L L^{H}\) 重构，并对比 `cholesky` 与 `cho_factor` 返回值的差别。

**操作步骤**：

```python
import numpy as np
from scipy.linalg import cholesky, cho_factor

# 一个 Hermitian 正定矩阵（直接借用官方文档示例）
A = np.array([[1, -2j], [2j, 5]])

# 1) cholesky 返回干净的下三角因子
L = cholesky(A, lower=True)
print("L =\n", L)
print("L @ L.T.conj() 是否还原 A：", np.allclose(L @ L.T.conj(), A))

# 2) cho_factor 返回 (c, lower) 元组，另一半未清零
c, low = cho_factor(A, lower=True)
print("cho_factor 的 c =\n", c)
print("low =", low)
```

**需要观察的现象**：

1. `cholesky` 得到的 `L` 严格上三角全为 0；而 `cho_factor` 的 `c` 上三角可能含有非零的「残留值」（本例 `overwrite_a=False` 时恰好被清零，需注意文档承诺的是「可能含随机数据」）。
2. `L @ L.T.conj()` 与 `A` 几乎完全相等。

**预期结果**：

```
L =
 [[1.+0.j  0.+0.j]
  [0.+2.j  1.+0.j]]
L @ L.T.conj() 是否还原 A： True
low = True
```

> 本实践的数值结论与官方 docstring 中的示例一致，可直接对照。若你的环境中 `cho_factor` 的上半三角恰好是 0，那是因为默认 `overwrite_a=False` 会触发一次拷贝、随后的清零路径仍生效；要稳定复现「垃圾数据」需配合 `overwrite_a=True` 且输入为 Fortran 连续，这在 [cho_factor 文档第 201–208 行的示例](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_cholesky.py#L201-L208) 中有展示。

#### 4.1.5 小练习与答案

**练习 1**：把上面的 `A` 改成 `np.array([[1, 2], [2, 1]])`（对称但**不**正定），调用 `cholesky` 会发生什么？为什么？

> **答案**：会抛 `LinAlgError`。该矩阵的特征值为 \(3\) 和 \(-1\)，有一个负特征值，不是正定的。Cholesky 算到第二个对角元时根号内为负，LAPACK `potrf` 返回 `info=2`，经 `_check_format_errors_warnings` 翻译成 `LinAlgError`。

**练习 2**：用 `cholesky` 同时求上三角因子 `U`（`lower=False`），并验证 \(A = U^{H} U\)。

> **答案**：`U = cholesky(A, lower=False)`；验证 `np.allclose(U.T.conj() @ U, A)` 为 `True`。注意是 `U.T.conj() @ U`（上三角在右边），与下三角形式 `L @ L.T.conj()`（下三角在左边）方向相反。

---

### 4.2 复用因子求解：cho_solve

#### 4.2.1 概念说明

一旦拿到了 Cholesky 因子，求解 \(A x = b\) 就变成两次廉价的三角求解。以下三角因子 \(A = L L^{H}\) 为例：

\[
L y = b \quad (\text{前代}), \qquad L^{H} x = y \quad (\text{回代})
\]

每步都是 \(O(n^{2})\)。这就是 LAPACK `potrs` 做的事。`cho_solve` 是 `potrs` 的薄包装，它的核心价值与 u3-l1 的 `lu_factor + lu_solve` 完全一致：**一次分解、多次求解**。当你需要对同一个 \(A\)、很多个不同的右端 \(b\) 求解时，先花 \(O(n^{3})\) 分解一次，之后每个 \(b\) 只需 \(O(n^{2})\)。

`cho_solve` 的接口故意设计成接收 `cho_factor` 的**元组返回值** `(c, lower)`，这样两条调用天然衔接：

```python
c, low = cho_factor(A)          # 一次分解
x1 = cho_solve((c, low), b1)    # 多次求解
x2 = cho_solve((c, low), b2)
```

#### 4.2.2 核心流程

```
cho_solve((c, lower), b, overwrite_b, check_finite)
  └─ 拆开元组 → _cho_solve(c, b, lower, ...)   # @_apply_over_batch 装饰
       ├─ check_finite 校验 c、b
       ├─ 方阵与维度兼容性校验
       ├─ overwrite_b = overwrite_b or _datacopied(b1, b)   # 已拷贝就可放心覆写
       ├─ potrs, = get_lapack_funcs(('potrs',), (c, b1))    # 按 dtype 选 s/d/c/z 前缀
       ├─ x, info = potrs(c, b1, lower=lower, overwrite_b=overwrite_b)
       └─ info != 0 → ValueError（非法参数）
```

注意：与 4.1 不同，`cho_solve` **没有**走 C++ 批量后端，而是直接用 `get_lapack_funcs` 取 `potrs`。批量能力由装饰器 [`@_apply_over_batch`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_cholesky.py#L271) 在 Python 层提供——它把多维输入切成一片片二维/一维核心，逐片调用 `_cho_solve`。

#### 4.2.3 源码精读

公共函数 [`cho_solve`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_cholesky.py#L267-L268) 只做拆包与转发：

```python
def cho_solve(c_and_lower, b, overwrite_b=False, check_finite=True):
    c, lower = c_and_lower
    return _cho_solve(c, b, lower, overwrite_b=overwrite_b, check_finite=check_finite)
```

真正的逻辑在 [`_cho_solve`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_cholesky.py#L272-L297)：

```python
@_apply_over_batch(('c', 2), ('b', '1|2'))
def _cho_solve(c, b, lower, overwrite_b, check_finite):
    ...
    overwrite_b = overwrite_b or _datacopied(b1, b)

    potrs, = get_lapack_funcs(('potrs',), (c, b1))
    x, info = potrs(c, b1, lower=lower, overwrite_b=overwrite_b)
    if info != 0:
        raise ValueError(f'illegal value in {-info}th argument of internal potrs')
    return x
```

要点：

- [第 271 行](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_cholesky.py#L271)装饰器声明 `c` 是 2 维核心、`b` 是 1 或 2 维核心，多出来的前导维度都被当成批处理维度。
- [第 291 行](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_cholesky.py#L291) `overwrite_b = overwrite_b or _datacopied(b1, b)`：这是 u2-l2 讲过的「如果前面校验阶段已经拷贝过，就顺手把覆写标志打开」的省内存技巧——既然 `b1` 已是副本，原地写它不会影响用户原始数据。
- [第 293–294 行](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_cholesky.py#L293-L294) 是本函数的心脏：`get_lapack_funcs` 取到正确类型的 `potrs`，然后一次调用完成两次三角求解。
- `potrs` 的 `info` 只在参数非法时非 0（不会因为矩阵不正定而失败——那是 `potrf` 阶段的事，到这里因子已经合法），所以这里抛 `ValueError` 而非 `LinAlgError`。

#### 4.2.4 代码实践

**实践目标**：用「分解一次 + 求解两次」对比「直接 `solve` 两次」，验证结果一致并体会复用的价值。

**操作步骤**：

```python
import numpy as np
from scipy.linalg import cho_factor, cho_solve, solve

np.random.seed(0)
# 构造对称正定矩阵：A = M^T M + n I 必然正定
M = np.random.randn(5, 5)
A = M.T @ M + 5 * np.eye(5)
b1 = np.array([1, 1, 1, 1, 1], dtype=float)
b2 = np.random.randn(5)

# 路径一：cho_factor 一次分解，cho_solve 两次求解
c, low = cho_factor(A)
x1 = cho_solve((c, low), b1)
x2 = cho_solve((c, low), b2)

# 路径二：直接用通用 solve（每次都重新分解）
y1 = solve(A, b1, assume_a='pos')
y2 = solve(A, b2, assume_a='pos')

print("x1 与 y1 一致：", np.allclose(x1, y1))
print("x2 与 y2 一致：", np.allclose(x2, y2))
print("残差 ||A x1 - b1|| =", np.linalg.norm(A @ x1 - b1))
```

**需要观察的现象**：两条路径得到的解几乎完全相等（只差浮点误差）；残差接近机器精度。

**预期结果**：

```
x1 与 y1 一致： True
x2 与 y2 一致： True
残差 ||A x1 - b1|| ≈ 1e-15 量级
```

> 关于 `solve(..., assume_a='pos')` 走 Cholesky 路径的细节，见 u2-l2。本实践是把那条路径手动拆成了 `cho_factor` + `cho_solve` 两步。

#### 4.2.5 小练习与答案

**练习 1**：如果传给 `cho_solve` 的因子 `c` 实际上来自一个**非正定**矩阵（你强行绕过了 `cho_factor` 的校验），`cho_solve` 会报什么错？

> **答案**：`cho_solve` 本身通常不会报「不正定」——它只调用 `potrs`，而 `potrs` 假定因子合法。错误会在上游 `cho_factor`/`cholesky` 阶段（`potrf` 返回 `info>0`）就以 `LinAlgError` 抛出。这也提醒：**永远不要绕过 `cho_factor` 直接拼造因子喂给 `cho_solve`**。

**练习 2**：`cho_solve` 为什么把 `lower` 标志也存进元组 `(c, lower)` 一起传，而不是让用户每次调用都重新指定？

> **答案**：因为 `cho_factor` 可能以 `clean=False` 返回，因子的另一半含垃圾数据，`potrs` 必须精确知道有效因子在哪一半（上/下三角），否则会用错数据。把 `lower` 与 `c` 绑定成元组，能保证「因子 + 它的朝向」始终成对传递，避免用户记错朝向导致静默错误。

---

### 4.3 带状 Cholesky：cholesky_banded 与 cho_solve_banded

#### 4.3.1 概念说明

很多来自微分方程离散化、时间序列的矩阵是**带状矩阵**（banded matrix）：非零元只集中在主对角线附近的一条带内，带宽之外全为 0。对这样的对称正定矩阵，普通 Cholesky 会浪费大量时间在「零 × 零」上。带状 Cholesky（LAPACK `pbtrf`/`pbtrs`）只存储和操作带内的元素，复杂度从 \(O(n^{3})\) 降到 \(O(n \cdot u^{2})\)（\(u\) 为半带宽），对大稀疏带状系统提升巨大。

**带状压缩存储格式**是这里的重点。设上带宽为 \(u\)（即 \(a_{ij}=0\) 当 \(j-i>u\)），矩阵 \(A\) 存成形状为 \((u+1, M)\) 的二维数组 `ab`。上三角形式约定为

\[
\texttt{ab}[u+i-j,\; j] = a[i,j], \qquad i \le j
\`

直观地说：把每条上对角线排成 `ab` 的一行，主对角线放最底行、最远的上对角线放最顶行，左上角用 `*`（任意值）补齐。源码 docstring 里画得很清楚（[第 311–321 行](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_cholesky.py#L311-L321)），以 \(M=6, u=2\) 为例：

```
upper form:                  lower form:
*   *   a02 a13 a24 a35      a00 a11 a22 a33 a44 a55
*   a01 a12 a23 a34 a45      a10 a21 a32 a43 a54 *
a00 a11 a22 a33 a44 a55      a20 a31 a42 a53 *   *
```

由 `lower` 参数选择用上三角还是下三角形式存储。这种「斜着塞进矩形」的存储和 u2-l4 里 `solve_banded` 用的是同一套约定（那边叫 `l,u` 双带宽，正定对称时 `l=u`，故这里只用一个 `u`）。

#### 4.3.2 核心流程

```
cholesky_banded(ab, overwrite_ab, lower, check_finite)
  └─ @_apply_over_batch(("ab", 2)) 装饰
       ├─ check_finite 校验
       ├─ pbtrf, = get_lapack_funcs(('pbtrf',), (ab,))
       ├─ c, info = pbtrf(ab, lower=lower, overwrite_ab=overwrite_ab)
       └─ info>0 → LinAlgError("n-th leading minor not positive definite")
           info<0 → ValueError（非法参数）

cho_solve_banded((cb, lower), b, overwrite_b, check_finite)
  └─ _cho_solve_banded(cb, b, lower, ...)  # @_apply_over_batch
       ├─ 形状兼容校验
       ├─ pbtrs, = get_lapack_funcs(('pbtrs',), (cb, b))
       ├─ x, info = pbtrs(cb, b, lower=lower, overwrite_b=overwrite_b)
       └─ info>0 → LinAlgError； info<0 → ValueError
```

与 4.1/4.2 的对比值得注意：

| | 稠密 `cholesky`/`cho_solve` | 带状 `cholesky_banded`/`cho_solve_banded` |
|---|---|---|
| 分解后端 | C++ `_batched_linalg._cholesky`（内部 `potrf`） | 直接 `get_lapack_funcs` 取 `pbtrf` |
| 求解后端 | `potrs` | `pbtrs` |
| 输入形状 | \((M, M)\) 方阵 | \((u+1, M)\) 压缩带状数组 |
| 失败语义 | `potrf` `info>0` → 通用 `LinAlgError`（含 info/片号） | `pbtrf` `info>0` → 明确「第 n 个顺序主子式非正定」 |

带状版本尚未迁移到 C++ 批量后端，仍走传统的 `get_lapack_funcs` 路径，错误信息也更「人类可读」。

#### 4.3.3 源码精读

先看分解 [`cholesky_banded`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_cholesky.py#L300-L378)，注意它**直接就是被 `@_apply_over_batch` 装饰的公共函数**（稠密版则是另起一个私有 `_cholesky`）：

```python
@_apply_over_batch(("ab", 2))
def cholesky_banded(ab, overwrite_ab=False, lower=False, check_finite=True):
    if check_finite:
        ab = asarray_chkfinite(ab)
    else:
        ab = asarray(ab)
    ...
    pbtrf, = get_lapack_funcs(('pbtrf',), (ab,))
    c, info = pbtrf(ab, lower=lower, overwrite_ab=overwrite_ab)
    if info > 0:
        raise LinAlgError(f"{info}-th leading minor not positive definite")
    if info < 0:
        raise ValueError(f'illegal value in {info}-th argument of internal pbtrf')
    return c
```

- [第 372 行](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_cholesky.py#L372) `pbtrf` 即 LAPACK 的带状正定 Cholesky 分解例程。
- [第 374–375 行](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_cholesky.py#L374-L375)：`info > 0` 表示第 `info` 个顺序主子式非正定，矩阵不正定——这是带状路径给出的**最明确**的错误信息。

再看求解 [`_cho_solve_banded`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_cholesky.py#L440-L465)：

```python
@_apply_over_batch(('cb', 2), ('b', '1|2'))
def _cho_solve_banded(cb, b, lower, overwrite_b, check_finite):
    ...
    if cb.shape[-1] != b.shape[0]:
        raise ValueError("shapes of cb and b are not compatible.")
    ...
    pbtrs, = get_lapack_funcs(('pbtrs',), (cb, b))
    x, info = pbtrs(cb, b, lower=lower, overwrite_b=overwrite_b)
    if info > 0:
        raise LinAlgError(f"{info}th leading minor not positive definite")
    if info < 0:
        raise ValueError(f'illegal value in {-info}th argument of internal pbtrs')
    return x
```

公共入口 [`cho_solve_banded`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_cholesky.py#L435-L437) 同样只做元组拆包：

```python
def cho_solve_banded(cb_and_lower, b, overwrite_b=False, check_finite=True):
    (cb, lower) = cb_and_lower
    return _cho_solve_banded(cb, b, lower, overwrite_b=overwrite_b,
                             check_finite=check_finite)
```

和 `cho_solve` 一样，带状因子 `cb` 与它的朝向 `lower` 必须成对传递。

#### 4.3.4 代码实践

**实践目标**：把同一个三对角对称正定矩阵分别用稠密 Cholesky 与带状 Cholesky 求解，验证答案一致；再构造一个非正定带状矩阵观察报错。

**操作步骤**：

```python
import numpy as np
from scipy.linalg import (
    cholesky_banded, cho_solve_banded, cho_factor, cho_solve, solveh_banded
)

# 经典三对角 SPD 矩阵 A = [[2,-1,0],[-1,2,-1],[0,-1,2]]（测试里用的同一个）
A = np.array([[2, -1, 0],
              [-1, 2, -1],
              [0, -1, 2]], dtype=float)

# 1) 稠密路径
c, low = cho_factor(A)
x_dense = cho_solve((c, low), np.ones(3))

# 2) 带状路径：u=1，上三角压缩存储 ab 形状 (u+1, M) = (2, 3)
#    第 0 行是上对角线 a[0,1], a[1,2]（首个位置补 0），第 1 行是主对角线
ab = np.array([[0, -1, -1],   # 上对角线（带前导填充 0）
               [2,  2,  2]])  # 主对角线
cb = cholesky_banded(ab)      # 默认 upper form
x_banded = cho_solve_banded((cb, False), np.ones(3))

print("稠密解 x_dense =", x_dense)
print("带状解 x_banded =", x_banded)
print("两者一致：", np.allclose(x_dense, x_banded))

# 3) 故意构造非正定带状矩阵：对角线取负值
ab_bad = np.array([[0, -1, -1],
                   [-2, -2, -2]])
try:
    cholesky_banded(ab_bad)
except Exception as e:
    print("非正定报错：", type(e).__name__, "-", e)
```

**需要观察的现象**：

1. 稠密解与带状解几乎完全相等。
2. 非正定矩阵触发 `LinAlgError`，消息里明确写出「第 1 个顺序主子式非正定」（`1-th leading minor not positive definite`）。

**预期结果**：

```
稠密解 x_dense ≈ [0.75 1.5  0.75]
带状解 x_banded ≈ [0.75 1.5  0.75]
两者一致： True
非正定报错： LinAlgError - 1-th leading minor not positive definite
```

> 这个带状矩阵 `ab = [[0,-1,-1],[2,2,2]]` 直接取自测试文件 [`tests/test_decomp_cholesky.py` 第 359 行](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/tests/test_decomp_cholesky.py#L359) 的 `test_cho_solve_banded`，可对照阅读。具体数值结果建议**待本地验证**（取决于 LAPACK 后端，但 `allclose` 为 `True` 与报错类型是确定的）。

#### 4.3.5 小练习与答案

**练习 1**：把上面 `ab` 改成下三角形式存储（`lower=True`），应该怎么填数组？

> **答案**：下三角形式约定 `ab[i-j, j] = a[i,j]`（\(i \ge j\)），第 0 行是主对角线，第 1 行是下对角线 `a[1,0], a[2,1]`（末尾补 `*`）。故 `ab_lower = np.array([[2, 2, 2], [-1, -1, 0]])`，然后 `cholesky_banded(ab_lower, lower=True)`，求解时也传 `cho_solve_banded((cb, True), b)`。注意 `lower` 标志必须前后一致。

**练习 2**：为什么 `cholesky_banded` 的输入形状是 \((u+1, M)\) 而不是 \((M, M)\)？

> **答案**：因为带状矩阵带宽之外全是 0，存储它们既浪费内存又让算法做无用的零运算。压缩成 \((u+1, M)\) 后，LAPACK `pbtrf`/`pbtrs` 只在带内活动，复杂度从 \(O(M^{3})\) 降到 \(O(M u^{2})\)；当 \(u \ll M\) 时（如三对角 \(u=1\)）几乎是线性时间。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个「同一个 SPD 矩阵，三种解法对比」的小任务。

**任务**：构造一个 \(M=6\)、半带宽 \(u=2\) 的对称正定带状矩阵 \(A\)，右端 \(b=\mathbf{1}\)。要求：

1. 用稠密 `cho_factor` + `cho_solve` 求解 \(x_{1}\)。
2. 把 \(A\) 压成上三角带状存储 `ab`（形状 \((3, 6)\)），用 `cholesky_banded` + `cho_solve_banded` 求解 \(x_{2}\)。
3. 用通用 `solve(A, b, assume_a='pos')` 求解 \(x_{3}\)。
4. 验证三者互相 `allclose`，并打印各自的残差 \(\|A x - b\|_{2}\)。
5. **思考题**：哪种解法在 \(M\) 很大、\(u\) 很小时最快？为什么？

**参考思路**：

```python
import numpy as np
from scipy.linalg import (cho_factor, cho_solve,
                          cholesky_banded, cho_solve_banded, solve)

np.random.seed(1)
M, u = 6, 2
# 构造带状 SPD：随机带内元素 + 大对角占优保证正定
A = np.zeros((M, M))
for k in range(1, u + 1):
    v = np.random.randn(M - k)
    A += np.diag(v, k) + np.diag(v, -k)
A += np.diag(np.sum(np.abs(A), axis=1) + 1.0)   # 严格对角占优 → 正定
b = np.ones(M)

# 1) 稠密
c, low = cho_factor(A)
x1 = cho_solve((c, low), b)

# 2) 带状：上三角形式 ab[u+i-j, j] = A[i,j]，i<=j
ab = np.zeros((u + 1, M))
for i in range(M):
    for j in range(i, min(i + u + 1, M)):
        ab[u + i - j, j] = A[i, j]
cb = cholesky_banded(ab)
x2 = cho_solve_banded((cb, False), b)

# 3) 通用 solve 走正定路径
x3 = solve(A, b, assume_a='pos')

print(np.allclose(x1, x2), np.allclose(x1, x3))
print(np.linalg.norm(A @ x1 - b),
      np.linalg.norm(A @ x2 - b),
      np.linalg.norm(A @ x3 - b))
```

**思考题答案**：带状解法（`cholesky_banded`/`cho_solve_banded`）最快。它只在带内活动，复杂度 \(O(M u^{2})\)；而稠密 Cholesky 与通用 `solve` 都要 \(O(M^{3})\)。\(u\) 越小、\(M\) 越大，差距越明显。这也是微分方程数值解大量使用带状求解器的原因。

---

## 6. 本讲小结

- Cholesky 分解只对 **Hermitian 正定**矩阵成立，写成 \(A = L L^{H}\)（或 \(U^{H} U\)），由 `lower` 选朝向；计算量约 \(\tfrac{1}{3}n^{3}\)，是 LU 的一半，且无需主元、数值稳定。
- `cholesky` 与 `cho_factor` 共用私有函数 `_cholesky`，唯一差别是 `clean` 开关：前者清零另一半三角返回干净因子，后者保留垃圾数据、返回 `(c, lower)` 元组直接喂给 `cho_solve`。
- 稠密分解走 **C++ 后端** `_batched_linalg._cholesky`（内部 LAPACK `potrf`，并处理 C/F 行列序的 `uplo` 翻转）；`cho_solve` 走 `get_lapack_funcs` 取 `potrs` 做两次三角求解，是「一次分解、多次求解」复用模式。
- 带状版本 `cholesky_banded`/`cho_solve_banded` 用 \((u+1, M)\) 的压缩存储格式，直接调 LAPACK `pbtrf`/`pbtrs`，复杂度降到 \(O(M u^{2})\)，且 `info>0` 时给出明确的「第 n 个顺序主子式非正定」错误信息。
- 所有函数都复用本包公共设施：`check_finite`、`overwrite_*` 门控（仅 2D 连续才放行）、`_datacopied`（已拷贝则放开覆写）、`@_apply_over_batch`（批处理维度切片），与 u2 建立的体系完全一致。

---

## 7. 下一步学习建议

- **继续矩阵分解主线**：本讲是「正定专用分解」，下一讲 u3-l3 讲 **QR 分解**（任意矩阵，含列主元），u3-l4 讲 **SVD**，u3-l6 讲 **LDL 分解**——后者可看作 Cholesky 对「对称但不一定正定」矩阵的推广（带 1×1/2×2 枢轴），学完会更清楚「正定」这个前提给 Cholesky 省了多少麻烦。
- **深入底层**：若想彻底搞懂 `potrf`/`pbtrf` 这些名字如何被 f2py 包成可调用的 Python 对象，以及 `s/d/c/z` 前缀如何分发，去读 u7-l1（`get_lapack_funcs` 与类型分发）和 u7-l2（f2py 与 `.pyf.src` 签名）。
- **批量与后端**：本讲多次出现「C++ 后端」与 `@_apply_over_batch`，它们的完整图景在 u8-l1（批量线性代数 Python 接口）与 u8-l2（C++ 批量后端实现）。
- **延伸阅读源码**：直接对照 [`tests/test_decomp_cholesky.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/tests/test_decomp_cholesky.py)，里面有针对复数、单精度、空数组、覆写安全性的大量用例，是检验你是否真正理解本讲的好材料。
