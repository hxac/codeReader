# 一般特征值问题 eig / eigvals

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚什么是「标准特征值问题」与「广义特征值问题」，以及它们之间的数学关系。
- 看懂 `scipy.linalg.eig` 从输入校验、驱动选择到结果后处理的完整 Python 薄壳流程。
- 理解 `eigvals` 与 `eig` 的关系（一个就是另一个的薄封装）。
- 掌握 `left` / `right` / `homogeneous_eigvals` 三个关键参数的语义。
- 理解 `_make_eigvals` 如何把 LAPACK 返回的 `(alpha, beta)` 翻译成普通比值或齐次坐标，以及它对无穷/不定特征值的特殊处理。
- 理解 `_check_format_errors_warnings` 如何把底层 LAPACK 的 `info` 收敛/错误信息聚合翻译成 `LinAlgError`。

## 2. 前置知识

### 2.1 特征值与特征向量

对于一个 \(n \times n\) 方阵 \(A\)，如果存在标量 \(\lambda\) 和非零向量 \(v\) 使得

\[
A v = \lambda v
\]

那么 \(\lambda\) 称为 \(A\) 的**特征值**（eigenvalue），\(v\) 称为对应的（右）**特征向量**（right eigenvector）。直观地说，矩阵 \(A\) 作用在 \(v\) 上只把它拉伸/缩放了 \(\lambda\) 倍，而不改变方向。

把满足 \(\det(A - \lambda I) = 0\) 的 \(\lambda\) 都找出来，就得到了全部特征值（共 \(n\) 个，计重数）。

### 2.2 广义特征值问题

更一般地，给定两个同阶方阵 \(A\) 和 \(B\)，**广义特征值问题**是求

\[
A v = \lambda B v
\]

当 \(B = I\)（单位阵）时，它就退化成标准问题。广义问题里，特征值写成

\[
\lambda = \frac{\alpha}{\beta}
\]

其中 \((\alpha, \beta)\) 是 LAPACK 直接返回的一对数。这种「分子/分母」表示法可以优雅地表达**无穷特征值**（\(\beta = 0\)）和**零特征值**（\(\alpha = 0\)），后面会看到 `_make_eigvals` 正是围绕这点设计的。

### 2.3 左特征向量

除了右特征向量 \(A v = \lambda v\)，还有**左特征向量** \(u\)，定义为（用 Hermitian 转置 \(A^H\)）

\[
A^H u = \overline{\lambda}\, u
\]

`eig` 用 `left=True` 可以同时返回它们。对实矩阵而言就是 \(A^T u = \lambda u\)。

### 2.4 齐次坐标（homogeneous coordinates）

对于广义问题，与其直接返回可能上溢/下溢的比值 \(\lambda = \alpha/\beta\)，不如把 \((\alpha, \beta)\) 原样返回。这样满足关系

\[
\beta_i \, A v_i = \alpha_i \, B v_i
\]

这种「不除」的返回方式称为**齐次特征值**。当 \(\alpha, \beta\) 的量级接近 \(\|A\|, \|B\|\) 时，直接相除容易数值溢出，齐次坐标更安全。

### 2.5 你需要回顾的前置讲义

本讲承接 [u2-l1](u2-l1-norm-and-structure.md)（范数与矩阵结构）与 [u2-l2](u2-l2-solve-and-dispatch.md)（公共参数 `check_finite`/`overwrite_a`、`_datacopied`、`_format_emit_errors_warnings` 的错误聚合范式）。请确认你已经熟悉：

- `_asarray_validated` 与 `check_finite` 的拦截作用；
- `overwrite_a` 的「二维 + Fortran 列主序连续」门控约束；
- `_datacopied` 用 `.base is None` 判断是否已拷贝；
- 编译后端 `_batched_linalg` 负责「真正的数值计算」这一架构。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|------|------|
| [`_decomp.py`](_decomp.py) | 特征值/特征向量相关函数的 Python 实现层，本讲的绝对主角。`eig`、`eigvals`、`_make_eigvals`、`_check_format_errors_warnings` 全部在此。 |
| [`src/_linalg_eig.hh`](src/_linalg_eig.hh) | C++ 批量后端的特征值内核模板：`_reg_eig`（标准问题，调 LAPACK `?geev`）、`_gen_eig`（广义问题，调 `?ggev`）、`transform_eigvecs`（实矩阵特征向量的打包→复数转换）。 |
| [`src/_batched_linalg_module.cc`](src/_batched_linalg_module.cc) | C++ 扩展模块入口，`_linalg_eig` 函数解析参数、按 dtype 分发、把逐片状态翻译成 Python 可读的 `err_lst`。 |

一句话定位：`_decomp.py` 是「校验 + 调度 + 后处理」的薄壳，真正的 QR 迭代发生在 `_linalg_eig.hh` 调用的 LAPACK `?geev`/`?ggev` 里。

## 4. 核心概念与源码讲解

### 4.1 eig 与 eigvals：通用特征值求解的整体流程

#### 4.1.1 概念说明

`scipy.linalg.eig` 是求解**一般（非对称、非 Hermitian）方阵**特征值问题的统一入口。它同时支持：

- **标准问题**：\(A v = \lambda v\)（`b=None`，默认）；
- **广义问题**：\(A v = \lambda B v\)（传入 `b`）；
- 只取特征值，或同时取左/右特征向量；
- 批量输入（前导若干维度视为「一摞矩阵」）。

`eigvals` 在功能上是 `eig` 的子集——它只返回特征值、不返回特征向量，实现上直接调用 `eig`。

> 与 `eigh`/`eigvalsh`（下一讲）的区别：`eigh` 专门针对**对称/Hermitian**矩阵，能用更高效、保证特征值为实数的专用驱动；而本讲的 `eig` 对**任意**方阵都适用，代价是特征值与特征向量通常都是复数。

#### 4.1.2 核心流程

`eig` 的 Python 层只做「准备工作和善后」，核心流程如下：

```
eig(a, b=None, left, right, ...)
  │
  ├─ 1. 校验：_asarray_validated（可选 check_finite）+ _deprecate_dtypes
  │        + 必须是方阵（末两维相等）
  ├─ 2. 归一化：_normalize_lapack_dtype → _ensure_aligned_and_native
  │        （把 dtype 规整成 LAPACK 认识的 s/d/c/z，内存对齐到本机字节序）
  ├─ 3. 覆写门控：overwrite_a = overwrite_a and (ndim==2) and F_CONTIGUOUS
  │        （复用 u2-l2 的 _datacopied 与「二维+F 序」门控）
  ├─ 4. 空矩阵短路：若末维为 0，直接返回空数组（避免传 0 维给 LAPACK）
  │
  ├─ 5. 分派到 C++ 后端 _batched_linalg._eig：
  │      • b is None  → 标准问题（内部走 ?geev）
  │      • b 给定     → 广义问题（内部走 ?ggev），并广播 a、b 的批维度
  │
  ├─ 6. 错误聚合：若后端返回 err_lst 非空 → _check_format_errors_warnings 抛错
  │
  ├─ 7. 后处理：
  │      • 用 _make_eigvals 把 (alpha, beta) 翻译成 w（见 4.3）
  │      • 广义问题的特征向量需归一化（?ggev 不保证归一化）
  │      • 向后兼容：实矩阵且特征值虚部全 0 时，把特征向量也变回实数
  │
  └─ 8. 按 left/right 组合返回 (w) / (w, vr) / (w, vl) / (w, vl, vr)
```

注意一个细节：与 `eigh`、`eig_banded` 不同，`eig` 与 `eigvals` **没有** `@_apply_over_batch` 装饰器——它们的批处理维度是**在 C++ 后端原生支持**的，不需要 Python 层逐片循环。

#### 4.1.3 源码精读

先看函数签名与文档里给出的核心关系式：

[\_decomp.py:L67-L78](_decomp.py#L67-L78) — `eig` 的签名，以及它求解的关系式 `a @ vr[:, i] = w[i] * b @ vr[:, i]`（右特征向量）和 `a.H @ vl[:, i] = w[i].conj() * b.H @ vl[:, i]`（左特征向量）。注意左特征向量乘的是 \(B^H\) 且特征值取共轭。

校验与归一化是后续所有特征值函数共用的开头：

[\_decomp.py:L212-L226](_decomp.py#L212-L226) — 这一段做了三件事：(1) `_asarray_validated` 把输入转成 ndarray 并可选地检查 NaN/Inf；(2) `_deprecate_dtypes` 弃用非标准 dtype；(3) 检查末两维相等（必须是方阵）；(4) `_normalize_lapack_dtype` + `_ensure_aligned_and_native` 把 dtype 与内存对齐规整成 LAPACK 友好形式；(5) 用 u2-l2 讲过的「二维 + Fortran 列主序连续」门控约束 `overwrite_a`。

空矩阵的短路处理值得一看，它体现了「边界情况在 Python 层提前处理」的设计：

[\_decomp.py:L228-L242](_decomp.py#L228-L242) — 当输入末维为 0（0×0 矩阵）时，`eig` 不会去调用 LAPACK，而是先递归调一次 `eig(np.eye(2))` 探测正确的输出 dtype，然后直接构造空数组返回。返回元组的形状严格遵循 `left`/`right` 的组合。

最后是按 `left`/`right` 组合返回结果的部分：

[\_decomp.py:L302-L308](_decomp.py#L302-L308) — 四种返回形态：只要特征值（`left=right=False`）、只要右向量、只要左向量、左右都要。这解释了为什么 `eig` 的返回值个数会变化——调用方必须根据自己传的参数来解包。

再看 `eigvals`，它极其简短：

[\_decomp.py:L965-L967](_decomp.py#L965-L967) — `eigvals` 的全部实现就是 `return eig(a, b=b, left=0, right=0, ...)`。也就是说 `eigvals(a)` 严格等价于 `eig(a, right=False)`（且不取左向量）。理解了 `eig`，就理解了 `eigvals`。

#### 4.1.4 代码实践

**实践目标**：验证 `eig` 求出的右特征向量确实满足 \(A v = \lambda v\)，并体会「实矩阵也可能有复特征值」。

**操作步骤**：

```python
import numpy as np
from scipy.linalg import eig

# 一个 90 度旋转矩阵，显然没有实数特征值
A = np.array([[0., -1.],
              [1.,  0.]])

w, vr = eig(A)          # 默认 right=True
print("特征值 w =", w)
print("右特征向量 vr =\n", vr)

# 逐列验证 A @ vr[:,i] == w[i] * vr[:,i]
for i in range(A.shape[0]):
    lhs = A @ vr[:, i]
    rhs = w[i] * vr[:, i]
    print(f"第 {i} 列: ||A v - lambda v|| = {np.linalg.norm(lhs - rhs):.2e}")
```

**需要观察的现象**：

- 特征值是纯虚数对 \(+\mathrm{i}, -\mathrm{i}\)（90 度旋转在复平面上对应旋转 \(±90°\)）。
- 特征向量是复数矩阵。
- 每一列的残差 \(\|A v - \lambda v\|\) 应接近 0（机器精度量级，如 `1e-16`）。

**预期结果**：残差为 `0.00e+00` 或 `1e-16` 量级。`vr` 的每一列已被归一化（模长为 1）。

> 说明：本例不需要你「假装运行过」，请实际执行并核对残差。

#### 4.1.5 小练习与答案

**练习 1**：`eigvals(A)` 和 `eig(A, right=False)` 的返回值有什么关系？

**答案**：完全等价。`eigvals` 内部就是 `eig(a, b=b, left=0, right=0, ...)`，见 [\_decomp.py:L965-L967](_decomp.py#L965-L967)。两者都只返回长度为 `M` 的特征值数组。

**练习 2**：若调用 `eig(A, left=True, right=False)`，返回几个对象？分别是什么？

**答案**：返回两个对象 `(w, vl)`——特征值 `w` 和左特征向量 `vl`。返回形态由 [\_decomp.py:L302-L308](_decomp.py#L302-L308) 决定。

---

### 4.2 标准问题与广义问题：LAPACK 驱动 geev / ggev 的自动选择

#### 4.2.1 概念说明

特征值数值求解的核心算法是 **QR 迭代**：不断对矩阵做 QR 分解并相似变换，把它推向「上三角」（Schur 形），此时对角线上的元素就是特征值。LAPACK 提供了对应的标准与广义驱动：

- 标准问题 \(A v = \lambda v\)：例程族 `?geev`（`s/d/c/z` 前缀对应 float/double/复 float/复 double）。
- 广义问题 \(A v = \lambda B v\)：例程族 `?ggev`。

> 关键点：与 `svd`（有 `lapack_driver='gesdd'/'gesvd'`）或 `eigh` 不同，顶层 `eig` **没有**暴露 `lapack_driver` 参数给用户。驱动是**自动选择**的：看你有没有传 `b`。这是因为一般矩阵的特征值问题在 LAPACK 里只有 `geev`/`ggev` 这一条主路，没有可选的替代驱动。

#### 4.2.2 核心流程

Python 层根据 `b is None` 走两条分支：

```
if b is None:                       # 标准问题
    w, beta, vl, vr, err_lst = _batched_linalg._eig(a1, left, right, overwrite_a, False)
    若 err_lst 非空 → _check_format_errors_warnings("geev", err_lst)
else:                               # 广义问题
    校验 b、广播 a/b 批维度、门控 overwrite_b
    w, beta, vl, vr, err_lst = _batched_linalg._eig(a1, left, right, overwrite_a, overwrite_b, b1)
    若 err_lst 非空 → _check_format_errors_warnings("ggev", err_lst)
    归一化 vr/vl（?ggev 不保证归一化）
```

C++ 后端 `_batched_linalg._eig` 收到调用后，再做一次「按 `b` 是否存在」的分派（见 4.2.3），最终落到 `_reg_eig`（`?geev`）或 `_gen_eig`（`?ggev`）。

#### 4.2.3 源码精读

Python 层的「标准 vs 广义」分叉点：

[\_decomp.py:L244-L252](_decomp.py#L244-L252) — `b is None` 时走标准问题分支，调用 `_batched_linalg._eig`，注意它传 `False` 作为第 5 个参数（没有 `b`）。后端返回 5 元组 `(w, beta, vl, vr, err_lst)`；这里 `beta` 在标准问题里其实用不到（见 4.3）。

[\_decomp.py:L253-L290](_decomp.py#L253-L290) — 广义问题分支。要点：(1) `_ensure_dtype_cdsz(a1, b1)` 强制 `a` 和 `b` 同 dtype；(2) `np.broadcast_shapes` + `np.broadcast_to` 把 `a`、`b` 的批维度对齐广播；(3) 调 `_batched_linalg._eig` 时多传一个 `b1`；(4) [\_decomp.py:L286-L290](_decomp.py#L286-L290) 用 `np.linalg.vector_norm(..., axis=-2)` 归一化特征向量——因为 LAPACK `?ggev` 返回的特征向量**未归一化**，而 `?geev` 是归一化的，所以只有广义分支需要这一步。

真正按 dtype 分发并选择驱动的是 C++ 入口：

[src/\_batched\_linalg\_module.cc:L779-L801](src/_batched_linalg_module.cc#L779-L801) — 按 `typenum`（float32/float64/complex64/complex128）切换到对应模板实例 `_eig<float>` 等；若返回 `info < 0` 表示内存或 LAPACK 内部错误，抛 `RuntimeError`。

驱动选择发生在 C++ 模板内部：

[src/\_linalg\_eig.hh:L417-L436](src/_linalg_eig.hh#L417-L436) — `_eig` 函数：若 `ap_Bm == NULL`（没有 `b`）调 `_reg_eig`，否则调 `_gen_eig`。这里还做了一对一致性检查（`B` 与 `beta` 要么都为空、要么都不为空），违反时返回 `-222`/`-223` 这种哨兵错误码。

`_reg_eig` 内部对每一片矩阵调用 LAPACK：

[src/\_linalg\_eig.hh:L161](src/_linalg_eig.hh#L161) — `call_geev(...)` 这一行就是标准问题的 QR 迭代核心。若 `info != 0`，该片状态被记入 `vec_status` 并立即 `goto done` 中断（见 4.4）。

广义问题对应的 `?ggev`：

[src/\_linalg\_eig.hh:L358](src/_linalg_eig.hh#L358) — `_gen_eig` 中的 `call_ggev(...)`，对矩阵对 \((A, B)$ 求广义特征值，返回 `alphar/alphai`（即 \(\alpha\)）和 `beta`（即 \(\beta\)）。

#### 4.2.4 代码实践

**实践目标**：求解一个广义特征值问题 \(A v = \lambda B v\)，并验证之。

**操作步骤**：

```python
import numpy as np
from scipy.linalg import eig

A = np.array([[0., -1.],
              [1.,  0.]])
B = np.array([[0., 1.],
              [1., 1.]])

w, vr = eig(A, B)        # 广义问题
print("广义特征值 w =", w)

for i in range(2):
    lhs = A @ vr[:, i]
    rhs = w[i] * (B @ vr[:, i])
    print(f"第 {i} 列: ||A v - lambda B v|| = {np.linalg.norm(lhs - rhs):.2e}")
```

**需要观察的现象**：特征值为 `[ 1.+0.j, -1.+0.j]`（与 `eig` 文档示例一致），每列残差接近 0。

**预期结果**：残差为 `1e-16` 量级。注意此时 `vr` 已被归一化（因为走了广义分支，Python 层做了 [L286-L290](_decomp.py#L286-L290) 的归一化）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `eig` 不像 `svd` 那样提供 `lapack_driver` 参数？

**答案**：因为一般矩阵特征值问题在 LAPACK 里只有一条主驱动路径（标准用 `?geev`、广义用 `?ggev`），驱动由「是否传 `b`」自动决定，没有可替代的驱动供用户选择。

**练习 2**：广义分支里为什么要对 `vr`/`vl` 做归一化，而标准分支不用？

**答案**：LAPACK `?geev`（标准）返回的特征向量已归一化为单位模长，而 `?ggev`（广义）返回的特征向量**未归一化**。所以 [\_decomp.py:L286-L290](_decomp.py#L286-L290) 只在广义分支用 `vector_norm` 归一化。

---

### 4.3 特征值返回格式：_make_eigvals 与齐次坐标

#### 4.3.1 概念说明

底层 LAPACK 返回的不是直接的 \(\lambda\)，而是「分子 \(\alpha\) + 分母 \(\beta\)」一对数。`eig` 提供两种返回风格，由 `homogeneous_eigvals` 开关控制：

- **`homogeneous_eigvals=False`（默认）**：返回比值 \(\lambda = \alpha / \beta\)。
- **`homogeneous_eigvals=True`**：返回形如 `(2, M)` 的数组，第 0 行是 \(\alpha\)、第 1 行是 \(\beta\)，即「齐次坐标」。

齐次坐标的优势在于：当 \(\alpha, \beta\) 量级接近 \(\|A\|, \|B\|\) 时，直接相除可能上溢/下溢；保留原始对更安全，并且能无损表达无穷/零特征值。

注意一个微妙之处：标准问题（`b=None`）里 LAPACK 也不会返回有意义的 `beta`，此时 `_make_eigvals` 把 `beta` 视作「全 1」，于是 \(\lambda = \alpha / 1 = \alpha\)，等价于直接返回特征值。

#### 4.3.2 核心流程

`_make_eigvals(alpha, beta, homogeneous_eigvals)` 的判定逻辑：

```
if homogeneous_eigvals:
    beta 为空则补成全 1
    把 (alpha, beta) 沿倒数第 2 轴 stack → 形状 (..., 2, M)
else:
    beta 为空 → 直接返回 alpha（标准问题）
    否则逐元素算 w = alpha / beta，并处理三类边界：
        • beta != 0           → 正常相除
        • alpha != 0 且 beta == 0 → inf（无穷特征值）
        • alpha == 0 且 beta == 0 → nan（不定，0/0）
```

边界处理用「射影无穷」（projective infinity）思想：复数情形也用实数 `np.inf`，因为 `1/np.inf == 0`，能正确表现无穷的代数行为。

#### 4.3.3 源码精读

[\_decomp.py:L33-L55](_decomp.py#L33-L55) — `_make_eigvals` 全文。逐段说明：

- [L34-L37](_decomp.py#L34-L37)：齐次分支，`beta is None` 时用 `np.ones_like(alpha)` 补全，再 `np.stack((alpha, beta), axis=-2)` 拼成 `(..., 2, M)`。
- [L39-L40](_decomp.py#L39-L40)：非齐次且 `beta is None`（标准问题），直接返回 `alpha`。
- [L42-L50](_decomp.py#L42-L50)：非齐次广义问题，逐元素相除，并用布尔掩码分别给「\(\beta=0\) 且 \(\alpha\ne 0\)」的位置填 `np.inf`。注释 [L47-L49](_decomp.py#L47-L49) 解释了为何复数也用实 `np.inf`（射影无穷）。
- [L51-L54](_decomp.py#L51-L54)：对「\(\alpha=\beta=0\)」（不定 0/0）这种病态情况，根据 `alpha` 是否有虚部填 `nan` 或 `complex(nan, nan)`。

`eig` 在拿到后端结果后调用它：

[\_decomp.py:L292](_decomp.py#L292) — `w = _make_eigvals(w, beta, homogeneous_eigvals)`。

这里有一个容易忽略的细节：**标准问题里后端返回的 `beta` 其实是 `None`**。因为 C++ 端 [src/\_batched\_linalg\_module.cc:L758-L767](src/_batched_linalg_module.cc#L758-L767) 仅当 `ap_Bm != NULL`（即传了 `b`）时才分配 `ap_beta` 数组；否则在返回时 [src/\_batched\_linalg\_module.cc:L808](src/_batched_linalg_module.cc#L808) 直接填 `Py_None`。于是标准问题的 `_make_eigvals(w, None, ...)` 正好落入 [L39-L40](_decomp.py#L39-L40) 的「`beta is None` → 直接返回 `alpha`」分支，等价于把 `w` 原样返回。这也解释了为什么标准问题的特征值不需要任何 \(\alpha/\beta\) 除法。

#### 4.3.4 代码实践

**实践目标**：直观对比「比值」与「齐次」两种返回格式，并观察齐次坐标如何表达广义特征值。

**操作步骤**：

```python
import numpy as np
from scipy.linalg import eigvals

A = np.diag([3., 8., 7.])      # 对角阵，特征值就是 3, 8, 7
B = 2 * np.eye(3)              # 广义问题 A v = lambda (2I) v

print("比值格式:", eigvals(A, B))
print("齐次格式:\n", eigvals(A, B, homogeneous_eigvals=True))
```

**需要观察的现象**：

- 比值格式给出 `[1.5+0.j, 4.+0.j, 3.5+0.j]`（因为 \(\lambda = \alpha/2\)）。
- 齐次格式给出 `[[3,8,7],[2,2,2]]`——分子是原始对角元、分母全是 2。

**预期结果**：与 `eig` 文档示例 [\_decomp.py:L175-L181](_decomp.py#L175-L181) 完全一致。这印证了齐次格式就是「不除」地返回 \((\alpha, \beta)\)。

#### 4.3.5 小练习与答案

**练习 1**：对一个标准问题 `eigvals(A)`（不传 `b`）开 `homogeneous_eigvals=True`，第二行会是什么？

**答案**：全 1。因为 [L35-L36](_decomp.py#L35-L36) 在 `beta is None` 时用 `np.ones_like(alpha)` 补全。所以齐次格式第二行恒为 1，第一行就是特征值本身。

**练习 2**：若某广义特征值的 \(\alpha=5, \beta=0\)，`homogeneous_eigvals=False` 时 `eigvals` 返回什么？

**答案**：`inf`。对应 [L50](_decomp.py#L50) 的 `w[~alpha_zero & beta_zero] = np.inf`，表示无穷大特征值。

---

### 4.4 错误聚合与收敛信息：_check_format_errors_warnings 与 LAPACK info

#### 4.4.1 概念说明

LAPACK 例程通过一个整数 `info` 报告状态：

- `info == 0`：成功。
- `info < 0`：参数非法（通常是编程错误，第 \(-\)info 个参数有问题）。
- `info > 0`：算法层面的问题。对 `?geev`/`?ggev` 而言，`info > 0` 意味着 **QR 迭代未能收敛**——某些特征值没算出来。

在批量场景下（一摞矩阵逐片求解），每一片可能各自成功或失败。`scipy.linalg` 的做法是：C++ 后端把每一片的状态收集进 `vec_status`，回传 Python 后聚合成 `err_lst`，再由 `_check_format_errors_warnings` 统一翻译成异常。

> 与 `solve`/`inv` 用的 `_format_emit_errors_warnings`（u2-l2）对比：那个函数要区分「奇异 → LinAlgError」「LAPACK 内部错误 → ValueError」「病态 → LinAlgWarning」三档。而 `eig` 的 `_check_format_errors_warnings` 更简单——只要 `err_lst` 非空就**一律抛 `LinAlgError`**，因为特征值问题没有「奇异」这种良性失败，不收敛就是硬错误。

#### 4.4.2 核心流程

```
C++ 后端逐片求解：
    for each slice:
        call_geev / call_ggev(...)
        if info != 0:                       # 该片失败（含未收敛）
            slice_status.lapack_info = info
            vec_status.push_back(slice_status)
            goto done                        # 一片失败即中断
    return convert_vec_status(vec_status)    # → Python 的 err_lst

Python eig：
    w, beta, vl, vr, err_lst = _batched_linalg._eig(...)
    if err_lst:                              # 有任何片失败
        _check_format_errors_warnings("geev", err_lst)   # → raise LinAlgError
```

#### 4.4.3 源码精读

C++ 端逐片失败捕获：

[src/\_linalg\_eig.hh:L163-L169](src/_linalg_eig.hh#L163-L169) — `_reg_eig` 里调用 `call_geev` 后立即检查 `info != 0`，把 `info` 存入该片状态并入队，然后 `goto done` 中断整个批量循环（一片失败就不再继续算后续片）。

C++ 端把状态翻译回 Python：

[src/\_batched\_linalg\_module.cc:L797-L804](src/_batched_linalg_module.cc#L797-L804) — `info < 0` 抛 `RuntimeError`（内存/LAPACK 内部错误）；正常路径调 `convert_vec_status(vec_status)` 得到 `ret_lst`，这就是 Python 侧的 `err_lst`（空列表表示全部成功）。

Python 端的聚合与翻译：

[\_decomp.py:L58-L64](_decomp.py#L58-L64) — `_check_format_errors_warnings(routine_name, err_lst)`：把 `err_lst` 里每片的 `lapack_info` 和片号 `num` 拼进一条消息，直接 `raise LinAlgError(mesg)`。注释 `# XXX: find a test case to cover this` 说明这条路径较难被触发（需要构造真正不收敛的矩阵）。

`eig` 在两个分支都接住了它：

[\_decomp.py:L250-L251](_decomp.py#L250-L251) — 标准问题分支，`err_lst` 非空时调 `_check_format_errors_warnings("geev", err_lst)`。

[\_decomp.py:L283-L284](_decomp.py#L283-L284) — 广义问题分支，同理调 `_check_format_errors_warnings("ggev", err_lst)`。注意传入的例程名不同（`geev` vs `ggev`），这样错误消息能指明是哪种问题失败。

#### 4.4.4 代码实践

**实践目标**：本实践为**源码阅读型**——真实触发 `?geev` 不收敛较困难（需要病态矩阵），所以我们通过阅读源码理解错误链路，并用一个能稳定复现的「参数非法」错误观察异常形态。

**操作步骤**：

1. 阅读上面的源码链接，确认错误链路：`call_geev info!=0` → `vec_status` → `err_lst` → `_check_format_errors_warnings` → `LinAlgError`。
2. 用一个非方阵触发 Python 层的早期校验（注意这是 `ValueError`，不是 `LinAlgError`，因为它在调用后端之前就被拦下了）：

```python
import numpy as np
from scipy.linalg import eig

try:
    eig(np.zeros((3, 4)))    # 非方阵
except ValueError as e:
    print("校验拦截:", e)
```

3. 对照 [\_decomp.py:L216-L219](_decomp.py#L216-L219)，确认这条 `ValueError` 来自「必须是方阵」的检查，与底层 `info` 无关。

**需要观察的现象**：非方阵输入被 Python 层直接拦下，根本不会进入 C++ 后端，因此不会触发 `_check_format_errors_warnings`。

**预期结果**：打印出形如 `Expected a square matrix or a batch of square matrices. Got ...` 的消息。真正的 `LinAlgError`（`info != 0`）路径属于「待本地验证」——除非你能构造出令 QR 迭代不收敛的极端病态矩阵，否则日常使用中很难见到。

#### 4.4.5 小练习与答案

**练习 1**：`_check_format_errors_warnings` 抛的是 `ValueError` 还是 `LinAlgError`？为什么和 `solve` 的错误处理不同？

**答案**：抛 `LinAlgError`（见 [L64](_decomp.py#L64)）。`solve` 的 `_format_emit_errors_warnings` 要区分奇异/病态/内部错误三档；而特征值问题没有「奇异」这种良性失败，不收敛就是硬错误，所以一律 `LinAlgError`。

**练习 2**：在批量输入（一摞矩阵）中，如果第 3 片不收敛，C++ 后端会怎样处理后续片？

**答案**：立即 `goto done` 中断，不再计算后续片（见 [src/\_linalg\_eig.hh:L167-L169](src/_linalg_eig.hh#L167-L169)）。失败片的信息进入 `err_lst`，回传后由 `_check_format_errors_warnings` 抛错。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个端到端小任务。

**任务**：给定一个一般矩阵 \(A\) 与正定矩阵 \(B\)，分别用比值格式和齐次格式求广义特征值；取出右特征向量验证 \(A v = \lambda B v\)；再单独对 \(A\) 求标准特征值与左特征向量，验证 \(A^H u = \overline{\lambda} u\)。

```python
import numpy as np
from scipy.linalg import eig

np.random.seed(0)
A = np.random.randn(4, 4)
B = np.eye(4) + np.random.randn(4, 4) * 0.1
B = B @ B.T + 4 * np.eye(4)      # 保证正定

# (1) 广义问题：两种格式
w_ratio, vr = eig(A, B)
w_homog = eig(A, B, homogeneous_eigvals=True)
print("比值格式 w  =", np.round(w_ratio, 4))
print("齐次格式 alpha/beta =\n", np.round(w_homog, 4))

# (2) 验证右特征向量 A v = lambda B v
resid_right = max(
    np.linalg.norm(A @ vr[:, i] - w_ratio[i] * (B @ vr[:, i]))
    for i in range(4)
)
print("右向量最大残差:", resid_right)

# (3) 标准问题 + 左特征向量，验证 A^H u = conj(lambda) u
w_std, vl, vr_std = eig(A, left=True, right=True)
resid_left = max(
    np.linalg.norm(A.conj().T @ vl[:, i] - np.conj(w_std[i]) * vl[:, i])
    for i in range(4)
)
print("左向量最大残差:", resid_left)
```

**验收标准**：

- 两种格式的特征值一致（齐次第二行做分母相除应等于比值格式）。
- 右向量残差、左向量残差都在 `1e-10` 量级以内。
- 你能解释清楚：为什么广义分支返回的 `vr` 已归一化、而 `vl` 也被归一化（见 [4.2]）。

## 6. 本讲小结

- `eig` 是一般方阵特征值问题的统一入口，同时支持标准 \(A v=\lambda v\) 与广义 \(A v=\lambda B v\)；`eigvals` 只是 `eig(..., left=0, right=0)` 的薄封装。
- Python 层是「校验 + dtype/内存归一化 + 覆写门控 + 委派 + 后处理」的薄壳，真正的 QR 迭代发生在 C++ 后端调用的 LAPACK `?geev`（标准）/`?ggev`（广义）里。
- 驱动是**自动选择**的（看是否传 `b`），`eig` 不像 `svd`/`eigh` 那样暴露 `lapack_driver` 参数。
- `_make_eigvals` 把 LAPACK 的 \((\alpha, \beta)\) 翻译成比值 \(\lambda\) 或齐次坐标 `(2, M)`，并用 `inf`/`nan` 妥善处理无穷/不定特征值。
- `_check_format_errors_warnings` 把逐片 LAPACK `info` 聚合成 `LinAlgError`；与 `solve` 的三档错误处理不同，特征值问题失败一律抛 `LinAlgError`。
- `eig`/`eigvals` 没有 `@_apply_over_batch` 装饰器，批处理维度由 C++ 后端原生支持；广义分支返回的特征向量会被额外归一化（`?ggev` 不保证归一化）。

## 7. 下一步学习建议

- **下一讲 [u4-l2](u4-l2-eigh-hermitian.md)**：学习 `eigh`/`eigvalsh`。当你的矩阵对称/Hermitian 时，应该用它们而非 `eig`——专用驱动 `?syev`/`?heev` 更快、保证特征值为实数、特征向量正交，还能用 `subset_by_index` 只求部分特征值。建议对比阅读 `_decomp.py` 里 `eigh` 与本讲 `eig` 的异同。
- **后续 [u4-l3](u4-l3-banded-tridiagonal-eig.md)**：带状/三对角专用特征值驱动，适合稀疏结构矩阵。
- **源码延伸**：想理解实矩阵的复特征值是怎么从 LAPACK 的「打包实数」格式变回复向量的，可读 [src/\_linalg\_eig.hh:L13-L35](src/_linalg_eig.hh#L13-L35) 的 `transform_eigvecs`（它处理复共轭对共享实向量的情况）。
- **跨讲义联系**：本讲的错误聚合 `_check_format_errors_warnings` 与 u2-l2 的 `_format_emit_errors_warnings` 是同一设计范式的两种变体，建议对照体会 scipy.linalg 在「批量 + 编译后端」架构下统一的错误处理哲学。
