# 范数、带宽与结构检测

## 1. 本讲目标

学完本讲，你应当能够：

- 知道 `scipy.linalg.norm` 支持哪些矩阵/向量范数，以及它在什么情况下比 `numpy.linalg.norm` 更快、更安全。
- 理解「带宽（bandwidth）」这一带状矩阵的核心概念，会使用 `scipy.linalg.bandwidth` 读取一个矩阵的上下带宽。
- 理解对称（symmetric）与 Hermitian（共轭对称）两种矩阵结构的数学定义与区别。
- 看懂 `issymmetric` / `ishermitian` 在 Cython 后端中的判定逻辑，明白 `atol` / `rtol` 容差与精确比较两条路径的区别，以及内存布局如何影响调用分支。

本讲是后续所有「特殊结构矩阵求解」（如 `solveh_banded`、Cholesky、`eigh`）的预备知识：很多高效算法的前提就是「先确认矩阵具有某种结构」。

## 2. 前置知识

### 2.1 ndarray 与内存布局

NumPy 的多维数组 `ndarray` 在内存中是一段连续（或带步长）的缓冲区。对二维数组而言有两种主流布局：

- **C 序（row-major，C_CONTIGUOUS）**：按行存放，`a[i, j]` 与 `a[i, j+1]` 相邻。NumPy 默认创建的数组多为 C 序。
- **Fortran 序（column-major，F_CONTIGUOUS）**：按列存放，`a[i, j]` 与 `a[i+1, j]` 相邻。LAPACK/BLAS 这类 Fortran 传统的数值库内部默认列主序。

可用 `a.flags['C_CONTIGUOUS']` / `np.isfortran(a)` 判断。本讲的 `norm`、`bandwidth`、`issymmetric` 都会根据内存布局选择不同的快速路径，这正是它们「快」的来源之一。

### 2.2 范数是什么

通俗地讲，范数是衡量向量或矩阵「大小」的一个非负实数。向量范数 \(\|\mathbf{x}\|\) 要满足非负性、齐次性和三角不等式。最常用的是欧氏范数（2-范数）：

\[
\|\mathbf{x}\|_2 = \left(\sum_i |x_i|^2\right)^{1/2}
\]

矩阵范数种类更多，本讲涉及 Frobenius 范数、1-范数、无穷范数、2-范数（最大奇异值）等。

### 2.3 对称与 Hermitian

对一个方阵 \(A\)：

- **对称**：\(A = A^{\mathsf{T}}\)，即 \(A_{ij} = A_{ji}\)。对实矩阵和复矩阵都有意义。
- **Hermitian（共轭对称）**：\(A = A^{\mathsf{H}} = \overline{A^{\mathsf{T}}}\)，即 \(A_{ij} = \overline{A_{ji}}\)（下标对调后还要取共轭）。

对**实矩阵**而言，「对称」和「Hermitian」是同一回事。对**复矩阵**二者不同：例如 \(\begin{bmatrix}1 & 3i \\ 3i & 2\end{bmatrix}\) 是对称的但不是 Hermitian；而 \(\begin{bmatrix}1 & 2+3i \\ 2-3i & 4\end{bmatrix}\) 是 Hermitian。

### 2.4 带状矩阵与带宽

很多物理与数值问题（如沿一条链的差分、有限元一维问题）产生的矩阵只在主对角线附近几条对角线上有非零元，其余位置都是 0，称为**带状矩阵（banded matrix）**。

设矩阵第 \(r\) 行第 \(c\) 列元素为 \(A_{rc}\)（行号 \(r\)、列号 \(c\) 从 0 计）：

- **下带宽（lower bandwidth）\(p\)**：主对角线**下方**最远的非零元到主对角线的距离，即 \(p = \max\{r - c \mid A_{rc} \neq 0,\ r > c\}\)。若主对角线下方全为 0，则 \(p = 0\)（即上三角矩阵）。
- **上带宽（upper bandwidth）\(q\)**：主对角线**上方**最远的非零元到主对角线的距离，即 \(q = \max\{c - r \mid A_{rc} \neq 0,\ c > r\}\)。若主对角线上方全为 0，则 \(q = 0\)（即下三角矩阵）。

例如三对角矩阵的 \(p = q = 1\)；对角矩阵的 \(p = q = 0\)；稠密方阵的 \(p = q = N-1\)。带宽越小，专用求解器（如 `solve_banded`）能省下的计算量越大。

### 2.5 与前置讲义的衔接

本讲承接 [u1-l4 快速上手](u1-l4-first-program.md) 中已介绍的「公共参数」体系，尤其是 `check_finite`（用 `np.asarray_chkfinite` 拦截 NaN/Inf）。本讲中你会看到 `norm` 同样接收 `check_finite`，而 `bandwidth` / `issymmetric` / `ishermitian` 则各自有不同的输入校验策略。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [_misc.py](_misc.py) | 定义 `norm`、`bandwidth`、`LinAlgWarning` 等。本讲的两个高层函数都在这里。 |
| [_cythonized_array_utils.pyx](_cythonized_array_utils.pyx) | Cython 实现 `issymmetric`、`ishermitian` 及其底层判定内核 `is_sym_her_real_*`、`is_sym_her_complex_*`。 |
| [_cythonized_array_utils.pxd](_cythonized_array_utils.pxd) | Cython 头文件，用 `fused` 类型（`np_numeric_t`、`np_complex_numeric_t` 等）描述多数据类型代码生成。 |
| [src/_common_array_utils.hh](src/_common_array_utils.hh) | C++ 后端 `_batched_linalg` 中的 `bandwidth` 数值内核，被 Python 层 `bandwidth` 通过 `_bandwidth` 调用。 |
| [src/_batched_linalg_module.cc](src/_batched_linalg_module.cc) | C++ 扩展的 Python 方法注册表，`_bandwidth` 在此注册并按 dtype 分发到 `bandwidth_contiguous_scalar` / `bandwidth_strided_scalar`。 |

> 说明：`bandwidth` 的高层校验在 `_misc.py`，真正的扫描算法在 C++ 后端（详见 [u8-l2](u8-l2-batched-cpp-backend.md)）；本讲聚焦 Python 接口语义与算法思路，C++ 实现细节留待后续讲义展开。

## 4. 核心概念与源码讲解

### 4.1 范数 norm

#### 4.1.1 概念说明

`scipy.linalg.norm` 的接口与 `numpy.linalg.norm` 几乎一致，能计算多种向量范数与矩阵范数。它的价值在于两点增强：

1. **非有限值检查**：通过 `check_finite=True`（默认）调用 `np.asarray_chkfinite`，在计算前就拦截 NaN/Inf，避免把它们送进底层库导致崩溃或 hang 住。
2. **关键路径走 BLAS/LAPACK**：对最常见的几种范数，调用经过高度优化的底层例程，比纯 NumPy 实现更快更稳。

下表汇总了支持的取值（摘自函数文档字符串）：

| `ord` | 矩阵范数 | 向量范数 |
| --- | --- | --- |
| `None` | Frobenius 范数 | 2-范数 |
| `'fro'` | Frobenius 范数 | — |
| `'nuc'` | 核范数（奇异值之和） | — |
| `inf` | \(\max\) 行绝对值和 | \(\max\|a_i\|\) |
| `-inf` | \(\min\) 行绝对值和 | \(\min\|a_i\|\) |
| `1` | \(\max\) 列绝对值和 | 见下 |
| `-1` | \(\min\) 列绝对值和 | 见下 |
| `2` | 2-范数（最大奇异值） | 见下 |
| `-2` | 最小奇异值 | 见下 |
| 其它整数 | — | \(\sum|a_i|^{ord}\)^{1/ord} |

其中 Frobenius 范数为：

\[
\|A\|_F = \left(\sum_{i,j} |a_{ij}|^2\right)^{1/2}
\]

#### 4.1.2 核心流程

`norm` 的调度逻辑可概括为「先校验、再尝试快速路径、最后回退 NumPy」：

```text
输入 a, ord, axis, keepdims, check_finite
 │
 ├─ check_finite=True ? → np.asarray_chkfinite(a)   # 拦截 NaN/Inf
 │                       else np.asarray(a)
 ├─ 是否能走底层例程？(dtype ∈ fdFD 且 axis is None 且 不 keepdims)
 │    ├─ 一维 + ord ∈ {None, 2}  → BLAS nrm2（欧氏范数）
 │    └─ 二维 + ord ∈ {1, inf}    → LAPACK lange（'1' 列和 / 'i' 行和）
 │           （且要求矩阵本身或其转置是 Fortran 连续）
 └─ 其余所有情况 → np.linalg.norm（回退）
```

为什么二维的 `lange` 路径要判断 Fortran 连续？因为 LAPACK 是列主序库，`lange(norm='1', a)` 直接吃列主序数组算「最大列和」最快；如果数组是行主序，就改用其转置 `a.T` 配合 `norm='i'`（最大行和）来达到同样结果，避免额外拷贝。

#### 4.1.3 源码精读

`norm` 的完整定义在 [_misc.py:L19-L181](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_misc.py#L19-L181)。注意第 146 行的注释点明了它和 NumPy 的区别：

```python
# Differs from numpy only in non-finite handling and the use of blas.
if check_finite:
    a = np.asarray_chkfinite(a)   # 非有限值检查
else:
    a = np.asarray(a)
```

（[_misc.py:L146-L150](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_misc.py#L146-L150)）

进入快速路径的总闸门在 [_misc.py:L153](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_misc.py#L153)：

```python
if a.size and a.dtype.char in 'fdFD' and axis is None and not keepdims:
```

只有 dtype 是单/双精度的实/复（`f/d/F/D`）、不指定 `axis`、不要 `keepdims` 时，才尝试底层例程。

**BLAS 欧氏范数路径**（[_misc.py:L155-L158](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_misc.py#L155-L158)）：

```python
if ord in (None, 2) and (a.ndim == 1):
    # use blas for fast and stable euclidean norm
    nrm2 = get_blas_funcs('nrm2', dtype=a.dtype, ilp64='preferred')
    return nrm2(a)
```

对一维数组，`ord=None` 或 `ord=2` 都走 BLAS 的 `nrm2` 例程。BLAS 的 `nrm2` 内部用缩放累加避免大数相加时的溢出，比朴素写法更稳定。`get_blas_funcs` / `get_lapack_funcs` 的类型分发机制详见 [u7-l1](u7-l1-blas-lapack-dispatch.md)。

**LAPACK 矩阵范数路径**（[_misc.py:L160-L178](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_misc.py#L160-L178)）：

```python
if a.ndim == 2:
    lange_args = None
    if ord == 1:
        if np.isfortran(a):
            lange_args = '1', a          # 最大列绝对值和
        elif np.isfortran(a.T):
            lange_args = 'i', a.T        # 转置后用行和等价代替
    elif ord == np.inf:
        if np.isfortran(a):
            lange_args = 'i', a          # 最大行绝对值和
        elif np.isfortran(a.T):
            lange_args = '1', a.T
    if lange_args:
        lange = get_lapack_funcs('lange', dtype=a.dtype, ilp64='preferred')
        return lange(*lange_args)
```

`lange` 是 LAPACK 的「矩阵范数估计」例程，`'1'` 表示最大列绝对值之和，`'i'` 表示最大行绝对值之和。注意 `ord='fro'`（Frobenius）并**没有**走 `lange`——源码注释里写着「the `*lange` frobenius norm is slow」，所以 Frobenius 范数最终落到下面的 NumPy 回退路径。

**NumPy 回退**（[_misc.py:L181](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_misc.py#L181)）：

```python
# fall back to numpy in every other case
return np.linalg.norm(a, ord=ord, axis=axis, keepdims=keepdims)
```

也就是说：Frobenius、`'nuc'`、`ord=2/-2`（矩阵情形，需要奇异值）、任意 `axis`、`keepdims=True`、非 `fdFD` dtype 等，统统交给 `np.linalg.norm`。

#### 4.1.4 代码实践

**实践目标**：观察 `norm` 在不同 `ord` 下的结果差异，并验证快速路径确实被命中。

**操作步骤**：

```python
import numpy as np
from scipy.linalg import norm

a = np.arange(9) - 4.0          # 一维向量
b = a.reshape((3, 3))           # 二维矩阵

print(norm(a))                  # ord=None，一维 → BLAS nrm2，欧氏范数
print(norm(b, 'fro'))           # Frobenius
print(norm(b, 1))               # 最大列和，二维 → LAPACK lange
print(norm(b, np.inf))          # 最大行和，二维 → LAPACK lange
print(norm(b, 2))               # 最大奇异值 → 回退 NumPy（需 SVD）
```

**需要观察的现象**：
- `norm(a)` 与 `norm(a, 2)` 数值相同（都是欧氏范数）。
- `norm(b, 'fro')` 等于 `norm(a)`（因为 `b` 是 `a` 重排，元素平方和不变）。
- `norm(b, 1)` 是各列绝对值和的最大值；`norm(b, np.inf)` 是各行绝对值和的最大值。

**预期结果**（与函数文档字符串示例一致）：

```
7.745966692414834
7.745966692414834
7.0
9.0
7.3484692283495345
```

如果想进一步验证「快速路径」，可以把输入改成 `np.asfortranarray(b)` 后再调用 `norm(b, 1)`，并用 `%timeit`（Jupyter）或 `timeit` 对比大矩阵上 `norm(b, 1)` 与 `np.linalg.norm(b, 1)` 的耗时差异——理论上 SciPy 版本略快。

#### 4.1.5 小练习与答案

**练习 1**：对一个含 `np.inf` 的一维数组调用 `norm`，默认会发生什么？如何让它不报错（哪怕结果无意义）？

**参考答案**：默认 `check_finite=True`，`np.asarray_chkfinite` 会抛出 `ValueError`。把 `check_finite=False` 传给 `norm` 即可跳过检查（但含 Inf 的范数结果通常是 Inf，且可能引发底层库异常，需自行承担风险）。

**练习 2**：为什么 `norm(A, 'fro')` 没有走 LAPACK `lange` 的 Frobenius 分支？

**参考答案**：因为源码注释明确指出 `*lange` 的 Frobenius 范数实现较慢，所以 SciPy 故意只对 `ord=1` 和 `ord=inf` 启用 `lange`，Frobenius 落到 NumPy 回退路径。

---

### 4.2 带宽 bandwidth

#### 4.2.1 概念说明

`scipy.linalg.bandwidth(a)` 返回元组 `(lower, upper)`，分别表示输入矩阵的下带宽和上带宽（见 §2.4 的定义）。它的典型用途是：在调用 `solve_banded` / `solveh_banded` / `eig_banded` 等带状专用求解器之前，先得知矩阵的带宽，从而决定存储格式与算法参数。

`bandwidth` 还支持「批量」输入：如果 `a` 形状是 `(..., N, M)`（最后两维是矩阵，前面是批量维），则返回两个形状为 `(...)` 的 `int64` 数组，每个批量切片给出各自的带宽。这与本模块批处理后端的设计一脉相承（详见 [u8-l1](u8-l1-batched-python-api.md)）。

#### 4.2.2 核心流程

Python 层 `bandwidth` 只做「输入校验 + 空数组处理」，真正的扫描交给 C++ 后端的 `_bandwidth`：

```text
输入 a
 ├─ np.asarray(a)
 ├─ 维度检查：ndim < 2 ? → 抛 ValueError
 ├─ dtype 检查：
 │     float16 / longdouble / clongdouble → 抛 TypeError
 │     非数值非布尔                  → 抛 TypeError
 ├─ 空数组 ? → 返回 (0, 0)（或批量为全 0 数组）
 └─ 否则 → _bandwidth(a)   # C++ 后端逐元素扫描
```

C++ 内核 `bandwidth`（[_common_array_utils.hh:L1305-L1333](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/src/_common_array_utils.hh#L1305-L1333)）的策略很巧妙，**不是**朴素地遍历所有元素，而是利用「只检测尚未覆盖的更远对角线」来跳过大量已知区域：

- 求**下带宽** `lb`：从左到右遍历列 `c`，在每一列中从下往上扫描行 `r`，只看 `r > c + lb` 的位置（即比当前已知下边界更远的地方）。一旦发现非零元，立即更新 `lb = r - c` 并 `break`；当 `c + lb` 已经填满剩余列时提前结束整组循环。
- 求**上带宽** `ub`：从右到左遍历列 `c`，从上往下扫描行 `r`，只看 `r < c - ub`。发现非零元则 `ub = c - r`。

这样对一个稠密矩阵，扫描代价约为 \(O(n)\) 而非 \(O(n^2)\)，因为每确认一条对角线就不再回头检查它「内侧」的区域。函数文档字符串里也说明了这一点：策略是「分别在上、下三角部分只检测未测试过的带元素；依据内存布局按行或按列扫描；若第 6 行第 4 列已非零，则后续行的水平搜索只到该带为止」。

> **待确认**：本讲不展开 `bandwidth_strided`（带步长的非连续版本）与 `detect_bandwidths`（批量版）的细节，它们属于 C++ 后端实现，将在 [u8-l2](u8-l2-batched-cpp-backend.md) 详述。

#### 4.2.3 源码精读

Python 高层函数 [_misc.py:L197-L274](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_misc.py#L197-L274) 的校验部分：

```python
a = np.asarray(a)
if a.ndim < 2:
    raise ValueError('Input array must be at least 2D.')

if np.isdtype(a.dtype, (np.float16, np.longdouble, np.clongdouble)):
    raise TypeError(f'Input array with {a.dtype} dtype is not supported.')
elif not np.isdtype(a.dtype, ("numeric", "bool")):
    raise TypeError(f'Input array must have a numeric dtype, got {a.dtype}.')
```

（[_misc.py:L252-L261](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_misc.py#L252-L261)）

注意 `bandwidth` 不像 `norm` 那样接受 `check_finite`——它只关心「数值或布尔」dtype，对 NaN/Inf 不过滤（因为带宽判定只看「是否为零」）。空数组的特殊处理在 [_misc.py:L264-L272](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_misc.py#L264-L272)：二维空数组返回标量 `(int64(0), int64(0))`，更高维则返回形状为批量的全零 `int64` 数组。

最后委托给编译后端（[_misc.py:L274](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_misc.py#L274)）：

```python
return _bandwidth(a)
```

`_bandwidth` 来自 `from ._batched_linalg import _bandwidth`（[_misc.py:L5](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_misc.py#L5)），它是一个 C++ 扩展方法，注册在 [_batched_linalg_module.cc:L1496](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/src/_batched_linalg_module.cc#L1496)：

```cpp
{"_bandwidth", _linalg_bandwidth, METH_VARARGS, doc_bandwidth},
```

该入口会根据输入是连续还是带步长，分发到 `bandwidth_contiguous_scalar<T>`（[_batched_linalg_module.cc:L1261](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/src/_batched_linalg_module.cc#L1261)）或 `bandwidth_strided_scalar<T>`（[_batched_linalg_module.cc:L1230](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/src/_batched_linalg_module.cc#L1230)），并按 dtype 实例化模板参数 `T`。核心数值逻辑即上文 §4.2.2 引用的 `bandwidth` 模板函数。

#### 4.2.4 代码实践

**实践目标**：用 `bandwidth` 读取一个手工构造的带状矩阵的上下带宽，并与定义对照。

**操作步骤**：

```python
import numpy as np
from scipy.linalg import bandwidth

# 取自函数文档字符串的例子
A = np.array([[3., 0., 0., 0., 0.],
              [0., 4., 0., 0., 0.],
              [0., 0., 5., 1., 0.],
              [8., 0., 0., 6., 2.],
              [0., 9., 0., 0., 7.]])
print(bandwidth(A))   # 期望 (3, 1)
```

**需要观察的现象**：
- 下带宽为 3：因为 `A[3,0]=8`（行 3 − 列 0 = 3）是主对角线下方最远的非零元。
- 上带宽为 1：因为 `A[2,3]=1` 和 `A[3,4]=2`（列 − 行 = 1）是主对角线上方最远的非零元。

**预期结果**：

```
(3, 1)
```

**批量验证**（可选）：构造一个形状 `(2, 4, 4)` 的堆栈，第一片是单位阵（带宽 0,0），第二片是三对角阵（带宽 1,1），观察返回的是两个长度为 2 的数组。

**待本地验证**：批量输入的具体返回形状与 dtype，建议在你本机的 SciPy 环境中实际运行确认。

#### 4.2.5 小练习与答案

**练习 1**：一个 \(5\times 5\) 的单位矩阵，`bandwidth` 返回什么？一个全 1 的 \(5\times 5\) 矩阵呢？

**参考答案**：单位阵只有主对角线非零，上下带宽都是 0，返回 `(0, 0)`；全 1 矩阵所有位置都非零，最远到 \(N-1=4\)，返回 `(4, 4)`。

**练习 2**：为什么 `bandwidth(np.array([[1,2],[3,4]], dtype=np.float16))` 会抛 `TypeError`？

**参考答案**：因为 [_misc.py:L256](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_misc.py#L256) 显式拒绝了 `float16`、`longdouble`、`clongdouble` 三种 dtype——它们在 C++ 后端的模板实例化中没有对应分支（见 [_batched_linalg_module.cc](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/src/_batched_linalg_module.cc) 的 dtype switch 没有 `NPY_FLOAT16`）。

---

### 4.3 结构检测 issymmetric / ishermitian

#### 4.3.1 概念说明

`issymmetric(a)` 判断方阵是否对称（\(A = A^{\mathsf{T}}\)），`ishermitian(a)` 判断是否 Hermitian（\(A = A^{\mathsf{H}}\)）。它们的核心价值是：

- **快**：用 Cython 写成 `nogil` 内核，逐元素精确比较，比 `np.allclose` 快得多（精确比较模式下）。
- **批量**：通过 `@_apply_over_batch` 装饰器支持对 `(..., N, N)` 堆栈逐切片判定。
- **可容差**：当提供 `atol` 或 `rtol` 时，回退到 `np.allclose` 做近似比较，适合判断「几乎对称」的浮点矩阵。

两者都提供 `atol`（绝对容差）与 `rtol`（相对容差）参数。注意几个约定（见各自文档字符串）：

- 空方阵按约定返回 `True`。
- `issymmetric` **不扫描对角线**——因为对称性只要求 \(A_{ij}=A_{ji}\ (i\neq j)\)，对角元天然满足；故对角线上的 NaN 会被忽略，但 `[[1,inf],[inf,2]]` 因 `inf` 被当作普通数而返回 `True`，`[[1,nan],[nan,2]]` 则返回 `False`。
- `ishermitian` 的复数内核**会**检查对角线：因为 Hermitian 矩阵的对角元必须是实数（\(A_{ii}=\overline{A_{ii}}\) 当且仅当虚部为 0），所以对角线上有纯虚数会让 `ishermitian` 返回 `False`。
- 对实矩阵，`ishermitian` 直接复用 `issymmetric` 的实数内核（实数情形下二者等价）。

#### 4.3.2 核心流程

以 `issymmetric` 为例，分派逻辑如下（`ishermitian` 类似，多一步复/实分支）：

```text
输入 a, atol, rtol
 ├─ 维度检查：必须是 2D 且方阵，否则 ValueError
 ├─ 空数组 → True
 ├─ 若 (给了 atol 或 rtol) 且 非整数 dtype：
 │     → np.allclose(a, a.T, atol=…, rtol=…)        # 近似路径
 ├─ 否则（精确路径，按内存布局选内核）：
 │     ├─ C_CONTIGUOUS  → is_sym_her_real_c(a)
 │     ├─ F_CONTIGUOUS  → is_sym_her_real_c(a.T)    # 转置使其变 C 序
 │     └─ 其它(非连续)  → is_sym_her_real_noncontig(a)
 └─ 返回 bool
```

`ishermitian` 的差异在于近似路径比较的是 `a` 与 `a.conj().T`（[_cythonized_array_utils.pyx:L249](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_cythonized_array_utils.pyx#L249)）；精确路径先判断是否复数对象，复数走 `is_sym_her_complex_*` 内核（比较时对下标对调元素取共轭），实数走与 `issymmetric` 相同的 `is_sym_her_real_*` 内核。

精确比较内核的核心是一个双层循环，只遍历严格下三角（实数对称）或包含对角线的下三角（复数 Hermitian）。这是 \(O(n^2/2)\) 的比较，但全部在 `nogil` 下用 fused 类型实例化，无 Python 开销。

#### 4.3.3 源码精读

先看 fused 类型定义。[_cythonized_array_utils.pxd:L17-L34](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_cythonized_array_utils.pxd#L17-L34) 定义了两个关键 fused 类型：

```cython
ctypedef fused np_numeric_t:        # 整数 + 浮点 + 复数（含 longdouble）
    cnp.int8_t ... cnp.float64_t, cnp.longdouble_t, cnp.complex64_t, cnp.complex128_t

ctypedef fused np_complex_numeric_t: # 仅复数
    cnp.complex64_t, cnp.complex128_t
```

`fused` 类型让 Cython 为每种 dtype 生成一份专用代码（类似 C++ 模板），调用时按实际 dtype 分派到对应实例——这正是「快」的原因之一。

`issymmetric` 的 Python 入口 [_cythonized_array_utils.pyx:L39-L125](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_cythonized_array_utils.pyx#L39-L125)，关键的两段：

近似路径（[_pyx:L111-L116](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_cythonized_array_utils.pyx#L111-L116)）：

```python
if (atol or rtol) and not np.issubdtype(a.dtype, np.integer):
    return np.allclose(a, a.T,
                       atol=atol if atol else 0.,
                       rtol=rtol if rtol else 0.)
```

精确路径的布局分派（[_pyx:L118-L123](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_cythonized_array_utils.pyx#L118-L123)）：

```python
if a.flags['C_CONTIGUOUS']:
    s = is_sym_her_real_c(a)
elif a.flags['F_CONTIGUOUS']:
    s = is_sym_her_real_c(a.T)        # F 序则传其转置（变为 C 序）
else:
    s = is_sym_her_real_noncontig(a)  # 非连续，走通用步长内核
```

注意一个小细节：Fortran 连续时传入的是 `a.T`，因为 `a.T` 此时是 C 连续的，能匹配内核签名 `np_numeric_t[:, ::1]`（最后一维连续）。整数 dtype 不走 `allclose`，因为整数没有浮点容差的概念，直接精确比较即可。

底层精确比较内核 [_pyx:L147-L154](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_cythonized_array_utils.pyx#L147-L154)：

```cython
@cython.boundscheck(False)
@cython.wraparound(False)
cdef inline bint is_sym_her_real_c_internal(const np_numeric_t[:, ::1]A) noexcept nogil:
    cdef Py_ssize_t n = A.shape[0], r, c
    for r in xrange(n):
        for c in xrange(r):                 # c < r：严格下三角，跳过对角线
            if A[r, c] != A[c, r]:
                return False
    return True
```

外层包装 `is_sym_her_real_c`（[_pyx:L128-L133](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_cythonized_array_utils.pyx#L128-L133)）用 `with nogil:` 释放 GIL，让大矩阵比较可以在多线程下并行。关闭 `boundscheck`、`wraparound`、`initializedcheck` 进一步消除运行时检查开销。

`ishermitian` 的复数内核 [_pyx:L291-L298](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_cythonized_array_utils.pyx#L291-L298) 有两处与对称内核不同：

```cython
cdef inline bint is_sym_her_complex_c_internal(const np_complex_numeric_t[:, ::1]A) noexcept nogil:
    cdef Py_ssize_t n = A.shape[0], r, c
    for r in xrange(n):
        for c in xrange(r+1):                       # c <= r：含对角线
            if A[r, c] != A[c, r].conjugate():       # 对调并取共轭
                return False
    return True
```

两处差异恰好对应 §4.3.1 的两个约定：
1. `xrange(r+1)` 含对角线（\(c=r\)），用于检测对角元是否为实数（\(A_{rr} \neq \overline{A_{rr}}\) 当虚部非 0）。
2. `A[c, r].conjugate()` 体现 Hermitian 定义 \(A_{rc} = \overline{A_{cr}}\)；而对称内核是朴素的 `A[c, r]`，体现 \(A_{rc} = A_{cr}\)。

`ishermitian` 主体 [_pyx:L170-L271](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_cythonized_array_utils.pyx#L170-L271) 的复/实分支（[_pyx:L254-L269](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_cythonized_array_utils.pyx#L254-L269)）：复数走 `is_sym_her_complex_*`，实数走 `is_sym_her_real_*`（与 `issymmetric` 共用）。

#### 4.3.4 代码实践

**实践目标**：验证「对称但非 Hermitian」的复矩阵，以及容差对近似 Hermitian 判定的影响。

**操作步骤**：

```python
import numpy as np
from scipy.linalg import issymmetric, ishermitian

# (1) 实对称矩阵
A = np.arange(9).reshape(3, 3)
A = A + A.T
print(issymmetric(A), ishermitian(A))   # True True（实矩阵两者等价）

# (2) 对称但非 Hermitian 的复矩阵
Ac = np.array([[1. + 1.j, 3.j],
               [3.j, 2.]])
print(issymmetric(Ac))    # True（A[0,1]==A[1,0]==3j）
print(ishermitian(Ac))    # False（conj(A[1,0])=-3j != A[0,1]=3j）

# (3) 容差下的近似 Hermitian
Af = np.array([[0,         1 + 1j],
               [1 - (1+1e-12)*1j, 0]])
print(ishermitian(Af))              # False（精确比较，有 1e-12 量级误差）
print(ishermitian(Af, atol=5e-11))  # True（容差内视为 Hermitian）
```

**需要观察的现象**：
- 实矩阵 `A` 上 `issymmetric` 与 `ishermitian` 结果相同。
- 复矩阵 `Ac` 上两者分歧：`issymmetric` 为 True、`ishermitian` 为 False。
- 给定 `atol` 后，原本精确比较失败的 `Af` 被判为 Hermitian（走 `np.allclose` 路径）。

**预期结果**（与 `ishermitian` 文档字符串示例一致）：

```
True True
True
False
False
True
```

#### 4.3.5 小练习与答案

**练习 1**：为什么 `issymmetric` 的精确内核循环用 `xrange(r)`，而 `ishermitian` 的复数内核用 `xrange(r+1)`？

**参考答案**：对称性只约束非对角元（\(i\neq j\)），对角线天然满足 \(A_{ii}=A_{ii}\)，所以 `issymmetric` 跳过对角线（`xrange(r)`）。而 Hermitian 还要求对角元为实数（\(A_{ii}=\overline{A_{ii}}\)），所以复数内核必须检查对角线（`xrange(r+1)`）。

**练习 2**：把一个 C 连续的矩阵改成 `np.asfortranarray(...)` 后再调用 `issymmetric`，走的是哪条分支？为什么源码要传 `a.T`？

**参考答案**：走 `F_CONTIGUOUS` 分支，调用 `is_sym_her_real_c(a.T)`。因为 `a.T` 在 `a` 为 Fortran 连续时恰好是 C 连续的，能匹配内核签名中要求最后一维连续的 `np_numeric_t[:, ::1]` memoryview，从而避免拷贝。

**练习 3**：`issymmetric(np.array([[1, np.nan],[np.nan, 2]]))` 返回什么？为什么对角线上的 NaN 不会让它报错？

**参考答案**：返回 `False`。因为非对角位置的 `nan != nan` 恒为真，循环立即返回 False。对角线上的 NaN 之所以不影响，是因为对称内核用 `xrange(r)` 跳过了对角线（见文档字符串说明）。

---

## 5. 综合实践

**任务**：构造一个对称的带状矩阵，用本讲的三个工具把它「刻画」清楚——计算 Frobenius 范数、读取上下带宽、并用结构检测验证它确实对称（且 Hermitian）。

```python
import numpy as np
from scipy.linalg import norm, bandwidth, issymmetric, ishermitian

n = 6
# 构造一个对称三对角矩阵（上下带宽均为 1）
rng = np.random.default_rng(0)
d = rng.standard_normal(n)            # 主对角线
e = rng.standard_normal(n - 1)        # 次对角线
A = np.diag(d) + np.diag(e, 1) + np.diag(e, -1)
A = np.asfortranarray(A)              # 故意用 Fortran 序，观察 norm/issymmetric 的布局分派

# 1) 范数：Frobenius 走 NumPy 回退路径
print("Frobenius norm =", norm(A, 'fro'))

# 2) 带宽：期望 (1, 1)
print("bandwidth =", bandwidth(A))

# 3) 结构：实矩阵，对称 == Hermitian
print("issymmetric =", issymmetric(A))
print("ishermitian =", ishermitian(A))

# 4) 用容差路径再次验证（应仍为 True）
print("issymmetric(rtol=1e-9) =", issymmetric(A, rtol=1e-9))
```

**预期结果**：
- `norm(A, 'fro')` 为一个正实数（等于 \(\sqrt{\sum d_i^2 + 2\sum e_i^2}\)）。
- `bandwidth(A)` 为 `(1, 1)`。
- `issymmetric(A)` 与 `ishermitian(A)` 均为 `True`。
- 给定 `rtol` 后仍为 `True`（此时走 `np.allclose` 近似路径）。

**延伸思考**：
1. 把 `A` 改成非对称（例如只加 `np.diag(e, 1)` 不加下三角），观察 `issymmetric` 变为 `False` 而 `bandwidth` 变为 `(0, 1)`（上三角带状）。
2. 在 `A` 上加一个微小非对称扰动 `1e-15`，观察精确路径可能因浮点误差返回 `False`，而给 `rtol` 后恢复 `True`——这正是 `atol`/`rtol` 参数存在的意义。

## 6. 本讲小结

- `scipy.linalg.norm` 与 NumPy 同名函数的差异在于：默认 `check_finite` 拦截非有限值，且对一维欧氏范数走 BLAS `nrm2`、对二维 1/无穷范数走 LAPACK `lange`，其余回退 `np.linalg.norm`。
- `bandwidth(a)` 返回 `(lower, upper)`，Python 层只做 dtype/维度校验，真正的「只扫未测对角线」扫描由 C++ 后端 `_bandwidth` 完成；支持 `(..., N, M)` 批量输入。
- 带宽是带状矩阵的核心结构特征，下带宽 0 即上三角、上带宽 0 即下三角、\(N-1\) 即满矩阵；它是 `solve_banded` 等专用求解器的前置输入。
- `issymmetric` / `ishermitian` 由 Cython `nogil` 内核做精确逐元素比较，按内存布局（C 序 / F 序 / 非连续）选择 `is_sym_her_real_*` 或 `is_sym_her_complex_*` 内核；给定 `atol`/`rtol` 时回退 `np.allclose`。
- 对称比较 \(A_{rc}=A_{cr}\) 且跳过对角线；Hermitian 比较 \(A_{rc}=\overline{A_{cr}}\) 且复数内核检查对角线（要求对角元为实数）。
- 实矩阵上「对称」与「Hermitian」等价，`ishermitian` 直接复用 `issymmetric` 的实数内核。

## 7. 下一步学习建议

- **结构化求解器**：本讲确认了矩阵的带宽与对称性后，下一站是 [u2-l4 带状、三角、Toeplitz 与 Circulant 结构化求解器](u2-l4-structured-solvers.md)，学习如何把这些结构信息喂给 `solve_banded` / `solveh_banded` / `solve_triangular` 以获得数量级的加速。
- **公共参数体系**：想更系统地理解 `check_finite`、`overwrite_a` 等参数如何贯穿整个 `scipy.linalg`，可阅读 [u2-l2 solve 与 assume_a 调度](u2-l2-solve-and-dispatch.md) 与 [u9-l3 性能、内存布局与错误处理取舍](u9-l3-performance-layout.md)。
- **底层机制**：若你对 `bandwidth` 背后的 C++ 批量后端、或 `issymmetric` 背后的 Cython memoryview/nogil/fused 类型感兴趣，可在进入专家层后阅读 [u7-l4 Cython 扩展模块实践](u7-l4-cython-extensions.md) 与 [u8-l2 C++ 批量后端实现](u8-l2-batched-cpp-backend.md)。
- **延伸阅读源码**：[_cythonized_array_utils.pyx](_cythonized_array_utils.pyx) 全文不到 320 行，是理解 Cython 数值内核写法的优秀范本；[tests/test_cythonized_array_utils.py](tests/test_cythonized_array_utils.py) 则展示了带宽与结构检测的完整测试用例，可作为行为参照。
