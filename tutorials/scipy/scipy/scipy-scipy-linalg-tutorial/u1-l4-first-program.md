# 快速上手：第一个线性代数程序

## 1. 本讲目标

学完本讲后，你应该能够：

- 用 `scipy.linalg.solve` 解一个线性方程组 $Ax=b$，并用残差验证答案的正确性。
- 用 `scipy.linalg.inv` 求矩阵的逆，用 `scipy.linalg.det` 求行列式，并理解它们底层都走 LU 分解。
- 用 `scipy.linalg.norm` 计算向量和矩阵的多种范数，知道它何时调用 BLAS/LAPACK、何时回退到 NumPy。
- 理解 `check_finite`、`overwrite_a` 这类「公共参数」的语义与权衡。
- 学会用 `LinAlgError` 捕获奇异矩阵导致的求解失败。

本讲是入门层第 4 讲，承接 u1-l1（项目定位）、u1-l2（目录与导出）。我们不再讲目录结构，而是真正写出一段可运行的程序，并顺着源码理解它「为什么这样工作」。

## 2. 前置知识

本讲会用到几个最基础的线性代数概念，这里用最通俗的方式解释。

**线性方程组**：把未知数排成向量 $x$，把系数排成矩阵 $A$，常数排成向量 $b$，求解的就是

\[
A x = b
\]

其中 $A$ 是 $N\times N$ 的方阵，$x$ 和 $b$ 是长度为 $N$ 的向量。只要 $A$ 不是「奇异」的，解就唯一存在。

**矩阵的逆**：若 $A$ 可逆，则存在矩阵 $A^{-1}$ 使得 $A A^{-1} = A^{-1} A = I$（$I$ 为单位阵）。于是 $Ax=b$ 的解也可以写成 $x = A^{-1} b$。但请注意：**数值计算中几乎不会真的用「先求逆再相乘」来解方程**，原因后面会讲。`inv` 主要用于你确实需要逆矩阵本身的场合。

**行列式**：方阵 $A$ 的一个标量值 $\det(A)$。关键性质是：

\[
\det(A) = 0 \iff A \text{ 奇异（不可逆）}
\]

所以行列式常被用来判断矩阵是否可逆。

**范数**：衡量向量或矩阵「大小」的标量函数，记作 $\|\cdot\|$。最常用的是向量的 2-范数（欧氏长度）和矩阵的 Frobenius 范数：

\[
\|v\|_2 = \sqrt{\sum_i |v_i|^2}, \qquad
\|A\|_F = \sqrt{\sum_{i,j} |a_{ij}|^2}
\]

**残差**：把求得的解 $x$ 代回原方程，看两边差多少：$r = Ax - b$。残差的范数 $\|Ax - b\|$ 越接近 0，说明解越准确。

**奇异矩阵**：行列式为 0、不可逆的矩阵。它对应的方程组要么无解、要么有无穷多解，数值求解器无法给出唯一解，会报 `LinAlgError`。

**LU 分解**：把矩阵 $A$ 拆成 $A = PLU$，其中 $L$ 是下三角（对角线为 1）、$U$ 是上三角、$P$ 是置换矩阵（记录行交换）。这是 LAPACK 求解线性系统、求逆、求行列式共同依赖的底层操作。本讲只需知道它的存在，u3 会专门讲。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲涉及的函数 |
|---|---|---|
| [`_basic.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py) | 基础线性代数运算（求解、求逆、行列式、最小二乘等） | `solve`、`inv`、`det`、`_format_emit_errors_warnings` |
| [`_misc.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_misc.py) | 范数、带宽检测、异常类、拷贝检测工具 | `norm`、`LinAlgError`、`LinAlgWarning`、`_datacopied` |

这两个文件都是「纯 Python 薄层」：它们负责参数校验、形状检查、错误聚合，真正的数值计算交给底层的编译扩展（`_batched_linalg`、BLAS、LAPACK）。这种「Python 接口 + 编译后端」的分层结构，是 scipy.linalg 的核心设计，本讲你会第一次看到它的全貌。

## 4. 核心概念与源码讲解

### 4.1 solve：解线性方程组

#### 4.1.1 概念说明

`solve` 解决的是 $Ax=b$。它是 scipy.linalg 里最高频的函数之一，也是本讲的「主轴」。

`solve` 有一个很重要的设计：它**不要求你告诉它矩阵的结构**。默认情况下（`assume_a=None`），它会自动检测 $A$ 是普通矩阵、对称、Hermitian 还是正定，然后选择最高效的 LAPACK 求解路径。当然你也可以用 `assume_a='pos'` 之类显式指定，省去检测开销（详见 u2-l2）。

#### 4.1.2 核心流程

`solve` 从 Python 层到拿到解 $x$，大致经历这几步：

1. **结构识别**：把 `assume_a` 字符串映射成内部整数编码 `structure`（普通=0、对角=11、三对角=31、正定=101、对称=201、Hermitian=211……）。
2. **输入校验**：用 `_asarray_validated` 把输入转成 ndarray；若 `check_finite=True`，顺带检查有没有 NaN/Inf；检查 $A$ 是否方阵、$b$ 维度是否匹配。
3. **形状归一化**：把一维的 $b$ 当作列向量，处理「批量维度」（一堆矩阵一起解，见 u8）。
4. **真正的求解**：调用编译后端 `_batched_linalg._solve`，它内部对每个矩阵片调用 LAPACK 的 `?GETRF`/`?GETRS`（LU 分解 + 三角求解）。
5. **错误聚合**：后端返回解和一个「错误列表」`err_lst`，Python 层用 `_format_emit_errors_warnings` 把其中的奇异/病态信息翻译成异常或告警。

伪代码：

```text
def solve(a, b, check_finite=True, assume_a=None, ...):
    structure = MAP[assume_a]              # 字符串 -> 内部编码
    a1 = asarray_validated(a, check_finite)
    b1 = asarray_validated(b, check_finite)
    assert a1 是方阵; assert b1 维度匹配
    x, err_lst = _batched_linalg._solve(a1, b1, structure, ...)
    if err_lst:
        _format_emit_errors_warnings(err_lst)   # 奇异 -> LinAlgError
    return x
```

#### 4.1.3 源码精读

`solve` 的函数签名（注意所有公共参数都带默认值，所以最简单的调用 `solve(A, b)` 就能跑）：

[_basic.py:57-59](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L57-L59) — `solve` 定义，参数包括 `lower`、`overwrite_a/b`、`check_finite`、`assume_a`、`transposed`。

结构字符串到内部编码的映射。**注意「与 C 端保持同步」的注释**——这个编码会被原样传给 C++ 后端 `_batched_linalg._solve`，所以 Python 和 C 两边必须用同一套数字：

[_basic.py:186-200](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L186-L200) — `structure` 字典把 `assume_a` 翻译成整数；无法识别的值抛 `ValueError`。

输入转数组 + `check_finite` 的实际作用点。`_asarray_validated` 内部会根据 `check_finite` 决定是否用 `asarray_chkfinite`（遇到 NaN/Inf 就抛错）：

[_basic.py:202-203](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L202-L203) — `a`、`b` 经 `_asarray_validated` 校验，`check_finite` 在这里生效。

`overwrite_a` 的「守门」逻辑非常关键：**用户传 `overwrite_a=True` 不等于一定会原地写入**。只有当矩阵是 2 维且 Fortran 连续（`F_CONTIGUOUS`）时，才真正允许覆盖。否则即使你传了 True，底层也会先拷贝一份：

[_basic.py:253-254](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L253-L254) — `overwrite_a` 与维度、内存布局取与，决定是否真正原地写。

真正干活的「重活」交给编译后端：

[_basic.py:257-259](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L257-L259) — 调用 `_batched_linalg._solve`，返回解 `x` 和错误列表 `err_lst`。

错误聚合：只要后端报告了任何异常情况，就交给 `_format_emit_errors_warnings` 处理：

[_basic.py:261-262](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L261-L262) — 有错误时调用 `_format_emit_errors_warnings`。

这个聚合函数的逻辑值得单独看：它遍历每个矩阵片的报告，把「奇异」聚合成 `LinAlgError`，把「LAPACK 内部错误」聚合成 `ValueError`，把「病态」聚合成 `LinAlgWarning`：

[_basic.py:27-54](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L27-L54) — `_format_emit_errors_warnings` 把后端的每片错误分类成异常或告警。

这就是为什么奇异矩阵会让 `solve` 抛出 `LinAlgError`——错误其实是在 C++ 后端检测到的，Python 层只是「翻译」并重新抛出。

#### 4.1.4 代码实践

**实践目标**：用 `solve` 解一个 3×3 方程组，并用 `A @ x == b` 验证。

**操作步骤**：

```python
import numpy as np
from scipy.linalg import solve

A = np.array([[3.0, 2.0, 0.0],
              [1.0, -1.0, 0.0],
              [0.0, 5.0, 1.0]])
b = np.array([2.0, 4.0, -1.0])

x = solve(A, b)
print("解 x =", x)
print("A @ x =", A @ x)
print("与 b 相等？", np.allclose(A @ x, b))
```

**需要观察的现象**：
- `x` 是一个一维数组（因为 `b` 是一维，`solve` 会把结果也压回一维，见源码注释里的「1-D array with N elements」说明）。
- `A @ x` 应当与 `b` 几乎相等（浮点数有微小误差，所以用 `np.allclose` 而非 `==`）。

**预期结果**：`x = [2. -2. 9.]`，`np.allclose` 返回 `True`。这与 `_basic.py` 里 `solve` 的 docstring 示例完全一致（见 [_basic.py:150-158](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L150-L158)）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `b` 改成形状 `(3, 2)` 的矩阵（两个右端），`solve` 会返回什么形状？

> **答案**：返回 `(3, 2)`，即同时解两个共享同一个 $A$ 的方程组。`solve` 支持多右端，参数 `b` 形状为 `(N,)` 或 `(N, NRHS)`。

**练习 2**：源码里 `_format_emit_errors_warnings` 区分了「奇异」「LAPACK 内部错误」「病态」三种情况，它们分别抛/警告什么？

> **答案**：奇异 → `LinAlgError`；LAPACK 内部错误（`info<0`）→ `ValueError`；病态（`is_ill_conditioned`）→ `LinAlgWarning`。

### 4.2 inv 与 det：求逆与行列式

#### 4.2.1 概念说明

`inv` 求 $A^{-1}$，`det` 求 $\det(A)$。两者看似不同，但**底层都依赖 LU 分解**：

- 求逆：对 $A$ 做 LU 分解后，解 $N$ 个三角方程组得到 $A^{-1}$ 的各列。
- 行列式：因为 $A=PLU$，$L$ 对角线为 1，所以

\[
\det(A) = \det(P)\cdot \det(L) \cdot \det(U) = (\pm 1)\prod_i U_{ii}
\]

符号由置换矩阵 $P$ 的行交换次数决定（奇数次取负）。这就是 `det` 的 docstring 里说的「用 LAPACK `getrf` 做 LU 分解，再取 $U$ 对角元乘积」。

一个重要细节：`det` 即使输入是 `float32`，**结果也会返回 `float64`**，以防止连乘溢出（见 [_basic.py:1167-1169](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L1167-L1169)）。

#### 4.2.2 核心流程

`inv` 的流程与 `solve` 几乎一样（校验 → 结构编码 → 调后端 → 错误聚合），差别只在于后端例程不同：

```text
def inv(a, check_finite=True, assume_a=None, lower=False):
    a1 = asarray_validated(a, check_finite)
    assert a1 方阵
    structure = MAP[assume_a]
    inv_a, err_lst = _batched_linalg._inv(a1, structure, overwrite_a, lower)
    if err_lst:
        _format_emit_errors_warnings(err_lst)
    return inv_a
```

`det` 略简单：它不返回「错误列表」（行列式对奇异矩阵只是返回 0，而不算错误），直接调用 `_linalg_det`：

```text
def det(a, check_finite=True):
    a1 = asarray_chkfinite(a) if check_finite else asarray(a)
    assert a1 方阵
    return _linalg_det(a1, overwrite_a)   # 内部 getrf + U 对角元乘积
```

#### 4.2.3 源码精读

`inv` 的定义与签名（`assume_a`、`lower` 是仅关键字参数）：

[_basic.py:984](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L984) — `inv` 定义。

`inv` 的方阵与维度校验：

[_basic.py:1090-1093](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L1090-L1093) — 非 2 维或非方阵抛 `ValueError`。

`inv` 调用后端 `_batched_linalg._inv`，并复用同一个错误聚合函数（注意它和 `solve` 共享 `_format_emit_errors_warnings`，所以奇异时也抛 `LinAlgError`）：

[_basic.py:1120-1123](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L1120-L1123) — `inv` 调用 `_batched_linalg._inv` 并处理错误。

`det` 的定义；注意 docstring 解释了「LU 分解 + 对角元乘积」的算法：

[_basic.py:1130](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L1130) — `det` 定义。
[_basic.py:1163-1165](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L1163-L1165) — docstring 说明行列式经 `getrf` LU 分解计算。

`det` 中 `check_finite` 的直接体现——和 `solve`/`inv` 不同，`det` 没有用 `_asarray_validated`，而是直接 `asarray_chkfinite`：

[_basic.py:1198](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L1198) — `det` 根据 `check_finite` 选择是否做有限性检查。

`det` 的实际计算（`_linalg_det` 即从 `_batched_linalg` 导入的 `_det`，见文件顶部 [_basic.py:18](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L18)）：

[_basic.py:1228](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L1228) — 调用 `_linalg_det` 完成行列式计算。

#### 4.2.4 代码实践

**实践目标**：对同一矩阵 $A$，验证 `A @ inv(A) ≈ I`，并验证 `det` 与 LU 对角元乘积的关系。

**操作步骤**：

```python
import numpy as np
from scipy.linalg import inv, det

A = np.array([[1.0, 2.0],
              [3.0, 4.0]])

Ainv = inv(A)
print("inv(A) =\n", Ainv)
print("A @ inv(A) =\n", A @ Ainv)        # 应接近单位阵
print("det(A) =", det(A))                # 1*4 - 2*3 = -2
print("dtype(det) =", det(A).dtype)      # float64，即使 A 是整数也提升
```

**需要观察的现象**：
- `A @ inv(A)` 的对角线接近 1、非对角接近 0。
- `det(A) = -2.0`（手算 $1\cdot4-2\cdot3=-2$）。
- 即便 `A` 是整数或 `float32`，`det` 结果也是 `float64`。

**预期结果**：`det(A) = -2.0`，`A @ inv(A)` 近似单位阵。可对照 docstring 示例 [_basic.py:1052-1060](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L1052-L1060)。

#### 4.2.5 小练习与答案

**练习 1**：既然 $x = A^{-1}b$，为什么数值计算中不推荐用 `x = inv(A) @ b` 来解方程？

> **答案**：求逆比直接解方程更费计算（约 3 倍工作量）且数值稳定性更差。正确做法是 `solve(A, b)`，它直接做 LU 分解并解三角系统，不显式构造 $A^{-1}$。`inv` 只在你确实需要逆矩阵本身时使用。

**练习 2**：把上例 $A$ 改成 `[[1,2],[2,4]]`（两行成比例，奇异），分别调用 `inv` 和 `det` 会怎样？

> **答案**：`det` 返回 `0.0`（奇异矩阵行列式为 0，不报错）；`inv` 抛 `LinAlgError`，因为逆不存在。这正体现了「行列式可优雅返回 0、求逆则必须报错」的设计差异。

### 4.3 norm：范数计算

#### 4.3.1 概念说明

`norm` 计算向量或矩阵的范数。它的特殊之处在于：**它是对 `numpy.linalg.norm` 的「增强版」**。源码注释直接点明「与 NumPy 的区别在于非有限值处理和使用 BLAS」。

关键差异：
1. `check_finite`：scipy 版默认 `True`，会先把 NaN/Inf 挡掉；NumPy 版没有这个参数。
2. 在常见情况下，scipy 版会**优先调用 BLAS/LAPACK 的高度优化例程**，而不是走 NumPy 的纯 Python/通用路径。

`ord` 参数决定算哪种范数。常用取值见下表（节选自源码 docstring）：

| ord | 矩阵范数 | 向量范数 |
|---|---|---|
| `None` | Frobenius | 2-范数 |
| `'fro'` | Frobenius | — |
| `1` | 最大列和 | $\sum|a_i|$ |
| `np.inf` | 最大行和 | $\max|a_i|$ |
| `2` | 最大奇异值 | 同向量 |

#### 4.3.2 核心流程

`norm` 的决策树：

```text
def norm(a, ord=None, check_finite=True):
    a = asarray_chkfinite(a) if check_finite else asarray(a)
    if 是浮点且 axis=None 且不 keepdims:
        if (ord in {None,2}) 且 一维:
            return BLAS.nrm2(a)            # 快速稳定的欧氏范数
        if 二维 且 (ord 是 1 或 inf):
            return LAPACK.lange(...)       # 快速矩阵范数
    return np.linalg.norm(a, ord, axis, keepdims)   # 其余回退 NumPy
```

也就是说，只有「热门路径」才走编译后端，其余全部回退到 `np.linalg.norm`。这是一种很务实的工程取舍。

#### 4.3.3 源码精读

`norm` 定义（注意它和 NumPy 版相比多了 `check_finite`）：

[_misc.py:19](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_misc.py#L19) — `norm` 定义。

`ord` 取值表（含 Frobenius 公式）：

[_misc.py:65-79](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_misc.py#L65-L79) — 各 `ord` 对应的矩阵/向量范数定义。

`check_finite` 的实际作用点：

[_misc.py:147-150](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_misc.py#L147-L150) — `check_finite=True` 时用 `asarray_chkfinite`，否则 `asarray`。

BLAS 快路径：一维 + 欧氏范数时调用 BLAS 的 `nrm2`，注释说这「又快又稳定」：

[_misc.py:153-158](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_misc.py#L153-L158) — 一维欧氏范数走 BLAS `nrm2`。

LAPACK 快路径：二维 + `ord` 为 1 或 inf 时，调用 LAPACK 的 `lange`（并巧妙利用转置和内存布局选择 `'1'`/`'i'` 模式）：

[_misc.py:160-178](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_misc.py#L160-L178) — 二维 1-范数/inf-范数走 LAPACK `lange`。

兜底回退 NumPy：

[_misc.py:180-181](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_misc.py#L180-L181) — 其余情况调用 `np.linalg.norm`。

#### 4.3.4 代码实践

**实践目标**：计算残差 $r = Ax-b$ 的 2-范数，作为解的「准确度指标」。这是本讲综合实践的核心一步，先单独练。

**操作步骤**：

```python
import numpy as np
from scipy.linalg import solve, norm

A = np.array([[3.0, 2.0, 0.0],
              [1.0, -1.0, 0.0],
              [0.0, 5.0, 1.0]])
b = np.array([2.0, 4.0, -1.0])
x = solve(A, b)

r = A @ x - b
print("残差 r =", r)
print("||r||_2 (默认 ord=None) =", norm(r))
print("||r||_2 (显式 ord=2)   =", norm(r, ord=2))
print("||r||_inf            =", norm(r, ord=np.inf))
```

**需要观察的现象**：
- 默认 `ord=None` 对一维数组返回 2-范数（走 BLAS `nrm2`）。
- 残差范数应在 $10^{-15}$ 量级，接近机器精度。
- `norm(r, ord=np.inf)` 返回残差各分量的最大绝对值。

**预期结果**：残差范数为极小值（如 `1e-15` 左右）。如需对照 docstring 示例，见 [_misc.py:97-117](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_misc.py#L97-L117)。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `norm` 对一维欧氏范数要专门走 BLAS `nrm2`，而不是用 `np.sqrt(np.sum(a**2))`？

> **答案**：直接平方求和会**溢出**——若某分量很大，`a**2` 可能超出浮点范围变成 `inf`。BLAS `nrm2` 内部做了缩放（先除以最大元再求和），避免溢出，因而更稳定。

**练习 2**：对一个含 NaN 的数组，`norm(a)` 默认会怎样？传 `check_finite=False` 又会怎样？

> **答案**：默认 `check_finite=True`，`asarray_chkfinite` 会直接抛 `ValueError`（报告出现 NaN/Inf）；传 `check_finite=False` 则跳过检查，结果会是 NaN（NaN 参与运算污染结果）。

### 4.4 公共参数与错误处理：check_finite / overwrite / LinAlgError

#### 4.4.1 概念说明

scipy.linalg 的函数几乎都带几个「公共参数」，它们不是某个算法独有的，而是**整个子包的通用约定**。掌握它们，等于掌握了所有函数的一半用法。

- **`check_finite`（默认 `True`）**：是否检查输入含 NaN/Inf。开启更安全，关闭更快。
- **`overwrite_a` / `overwrite_b`（默认 `False`）**：是否允许函数原地改写输入数组以省一次拷贝。开启可能更快，但调用后你的输入数组可能被破坏。
- **`LinAlgError` / `LinAlgWarning`**：scipy.linalg 的两类异常。`LinAlgError` 直接复用 NumPy 的（所以两包抛的是同一个类）；`LinAlgWarning` 是 scipy 自定义的。

一个容易踩的坑：**`overwrite_a=True` 不保证真的原地写**。前面 4.1.3 看到过，只有当数组是 2 维且 Fortran 连续时才真正生效；否则底层照样拷贝。判断「到底有没有拷贝」可以用 `_datacopied`。

#### 4.4.2 核心流程

公共参数的「效应链」：

```text
用户调用 solve(A, b, check_finite=False, overwrite_a=True)
   │
   ├─ check_finite=False → asarray 跳过有限性检查（更快，但 NaN 会让结果变垃圾）
   │
   ├─ overwrite_a=True → 但只有 A 是 2D + F_CONTIGUOUS 时才真生效
   │                     否则 _datacopied 返回 True（发生了拷贝，输入 A 不受影响）
   │
   └─ A 奇异 → 后端报告 → _format_emit_errors_warnings → raise LinAlgError
```

#### 4.4.3 源码精读

`LinAlgError` 是从 NumPy 直接导入的（印证 u1-l1 说的「两包共享同一个异常」）：

[_misc.py:2](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_misc.py#L2) — `from numpy.linalg import LinAlgError`。

`LinAlgWarning` 是 scipy.linalg 自己定义的（继承自 `RuntimeWarning`），用于「接近失败条件」的告警，比如病态矩阵：

[_misc.py:11-16](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_misc.py#L11-L16) — `LinAlgWarning` 类定义。

`_datacopied`：判断 `arr = asarray(original)` 之后，`arr` 是否真的拷贝了数据（用于解释 overwrite 是否生效、输入是否被改动）：

[_misc.py:184-194](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_misc.py#L184-L194) — `_datacopied` 严格判断两数组是否共享内存。

`solve` 中 `overwrite_a` 与内存布局的「与」逻辑（再次强调 True 不一定生效）：

[_basic.py:253-254](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L253-L254) — 只有 2D 且 Fortran 连续时 overwrite 才生效。

错误聚合函数把「奇异」翻译成 `LinAlgError`（这就是用户捕获的异常来源）：

[_basic.py:41-44](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L41-L44) — 奇异片列表被聚合成 `LinAlgError`。

#### 4.4.4 代码实践

**实践目标**：故意构造奇异矩阵，捕获 `LinAlgError`；再观察 `check_finite` 对 NaN 输入的不同行为。

**操作步骤**：

```python
import numpy as np
from scipy.linalg import solve, LinAlgError

# 1) 奇异矩阵：第二行 = 2 * 第一行
A = np.array([[1.0, 2.0],
              [2.0, 4.0]])
b = np.array([1.0, 2.0])
try:
    x = solve(A, b)
except LinAlgError as e:
    print("捕获到 LinAlgError：", e)

# 2) 含 NaN 的输入：默认 check_finite=True 会抛 ValueError
A_nan = np.array([[1.0, 2.0], [np.nan, 4.0]])
try:
    solve(A_nan, b)
except ValueError as e:
    print("check_finite 拦截 NaN，抛 ValueError：", e.__class__.__name__)

# 3) 关闭 check_finite：NaN 不被拦截，结果被污染成 NaN
x_nan = solve(A_nan, b, check_finite=False)
print("关闭 check_finite 后结果 =", x_nan)
```

**需要观察的现象**：
- 第 1 步：抛 `LinAlgError`，提示某片奇异。
- 第 2 步：抛 `ValueError`（来自 `asarray_chkfinite`），而非 `LinAlgError`——这是「输入非法」而非「数学奇异」。
- 第 3 步：不抛错，但结果是 NaN——这就是「关闭检查换性能」的代价。

**预期结果**：分别打印 `LinAlgError`、`ValueError`、以及 NaN 结果。第 3 步的具体数值「待本地验证」（取决于 NaN 如何传播），但一定是非有限值。

#### 4.4.5 小练习与答案

**练习 1**：`overwrite_a=True` 时，什么条件下输入数组 `A` 才真的会被改写？

> **答案**：必须同时满足 `A.ndim == 2` 且 `A.flags["F_CONTIGUOUS"]` 为真（Fortran 列主序连续）。否则即使传 True，`_datacopied` 会显示发生了拷贝，原 `A` 不受影响。这是 LAPACK 要求列主序导致的（详见 u9-l3）。

**练习 2**：为什么 scipy 选择复用 NumPy 的 `LinAlgError` 而不是自己定义一个？

> **答案**：为了让用户写 `except numpy.linalg.LinAlgError` 或 `except scipy.linalg.LinAlgError` 都能捕获到同一类异常，降低迁移成本。两个包的异常是同一个类对象。

## 5. 综合实践

把本讲的 `solve`、`inv`、`det`、`norm`、`LinAlgError` 串成一个完整的「求解 + 验证 + 异常处理」流程。

**任务**：构造一个 4×4 系数矩阵 $A$ 与右端 $b$，用 `solve` 求 $x$，用 `norm` 计算残差验证正确性，顺带算 `inv` 和 `det`，最后故意用一个奇异矩阵触发 `LinAlgError`。

```python
import numpy as np
from scipy.linalg import solve, inv, det, norm, LinAlgError

np.random.seed(0)
# 1) 构造一个良态的 4x4 系统
A = np.random.randn(4, 4)
b = np.random.randn(4)

# 2) 求解
x = solve(A, b)

# 3) 残差验证
r = A @ x - b
print("残差 ||Ax-b||_2 =", norm(r))          # 应 ~1e-15
print("求解成功？", np.allclose(A @ x, b))

# 4) 顺带求逆与行列式，并交叉验证
Ainv = inv(A)
print("||A@inv(A) - I||_F =", norm(A @ Ainv - np.eye(4)))   # 应 ~1e-15
print("det(A) =", det(A))

# 5) 故意构造奇异矩阵（一行是另一行的倍数），捕获异常
S = A.copy()
S[1] = 3 * S[0]              # 第 2 行 = 3 倍第 1 行 → 奇异
print("det(S) =", det(S))    # 行列式为 0（不报错）
try:
    solve(S, b)
except LinAlgError as e:
    print("奇异矩阵求解被捕获：", e.__class__.__name__)
```

**预期结果**：残差与 `A@inv(A)-I` 的范数都在 $10^{-15}$ 量级；`det(S)` 接近 0；最后 `solve(S, b)` 抛 `LinAlgError` 并被捕获。具体数值「待本地验证」（依赖随机种子与平台），但定性结论稳定。

**延伸思考**：把 `solve(A, b)` 换成 `inv(A) @ b` 也能得到近似的 $x$，但残差通常略大、且更慢。动手比较两者的 `norm(A@x-b)` 与耗时，体会「不要用求逆来解方程」这条经验。

## 6. 本讲小结

- `solve(A, b)` 解 $Ax=b$，默认会自动检测矩阵结构并选最优 LAPACK 路径；真正计算在编译后端 `_batched_linalg._solve`，Python 层只做校验与错误聚合。
- `inv` 和 `det` 底层都依赖 LU 分解（`getrf`）；`inv` 遇奇异矩阵抛 `LinAlgError`，而 `det` 对奇异矩阵优雅返回 0。
- `norm` 是 `numpy.linalg.norm` 的增强版：多了 `check_finite`，并在热门路径（一维欧氏、二维 1/inf 范数）走 BLAS/LAPACK，其余回退 NumPy。
- `check_finite`（默认开）用 `asarray_chkfinite` 挡住 NaN/Inf，关掉更快但有风险；`overwrite_a=True` 只有在 2D 且 Fortran 连续时才真正生效。
- `LinAlgError` 直接复用 NumPy 的；scipy 另定义 `LinAlgWarning` 报告病态；后端的每片错误经 `_format_emit_errors_warnings` 统一翻译。
- 数值线性代数的金科玉律：**解方程用 `solve`，不要用 `inv`**——更稳更快。

## 7. 下一步学习建议

- **u2-l1（范数、带宽与结构检测）**：本讲只用了 `norm` 的最常见用法，下一讲会展开 `bandwidth`、`issymmetric`/`ishermitian` 等结构判定工具。
- **u2-l2（solve 与 assume_a 调度）**：本讲提到了 `assume_a` 但没深入，下一讲会讲清楚它如何在通用/对称/正定/三角/带状求解器间分派。
- **u3-l1（LU 分解）**：本讲多次提到 `getrf`/LU 分解是 `solve`/`inv`/`det` 的共同底座，u3 会从源码层面拆解 `lu`、`lu_factor`、`lu_solve`。
- 想直接验证本讲结论，可阅读 [`tests/test_basic.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/tests/test_basic.py) 中 `solve`/`inv`/`det` 的测试用例，看官方如何断言这些函数的行为。
