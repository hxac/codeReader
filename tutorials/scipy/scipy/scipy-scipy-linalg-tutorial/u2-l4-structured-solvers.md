# 带状、三角、Toeplitz 与 Circulant 结构化求解器

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚「结构化矩阵」为什么要单独配一套求解器：抓住带状、三角、Toeplitz、循环（circulant）四种结构各自的稀疏规律，就能把求解复杂度从通用的 \(O(n^3)\) 降到 \(O(n)\)、\(O(n^2)\) 甚至 \(O(n\log n)\)。
- 掌握 `solve_banded` / `solveh_banded` 的带状存储格式 `ab[u+i-j, j] == a[i,j]`，以及它们分别委派给 LAPACK 的 `gbsv`/`gtsv` 与 `pbsv`/`ptsv`。
- 理解 `solve_triangular` 的 `lower`、`unit_diagonal`、`trans` 三个选项如何映射到底层 `trtrs`，以及它为何能作为矩阵函数内部反复调用的「积木」。
- 了解 Toeplitz 系统的专用路径：`solve_toeplitz` 通过 Cython 内核 `levinson` 做 \(O(n^2)\) 的 Levinson 递归；`solve_circulant` 与 `matmul_toeplitz` 则借助 FFT 在 \(O(n\log n)\) 完成。

本讲承接 u2-l2 的 `solve` 与 `assume_a` 调度：通用 `solve` 会自动检测结构，但**显式调用结构化求解器**能省去检测开销、还能直接接受压缩存储格式，是面向「已知结构」场景的更快路径。

## 2. 前置知识

### 2.1 什么是「结构化矩阵」

一个 \(n\times n\) 的普通稠密矩阵有 \(n^2\) 个元素，求解 \(Ax=b\) 通常要 \(O(n^3)\)。但很多实际问题的矩阵「绝大部分元素是 0」或「元素由很少几个参数决定」，例如：

| 结构 | 非零/取值规律 | 存储与求解复杂度 |
|------|--------------|-----------------|
| 三角 | 仅上三角或下三角非零 | 存储 \(O(n^2)\)，求解 \(O(n^2)\) |
| 带状 | 仅主对角线附近 \((l,u)\) 条对角线非零 | 存储 \(O(n(l+u))\)，求解 \(O(n(l+u)^2)\) |
| Toeplitz | 每条对角线取值相同，\(A_{i,j}\) 只依赖 \(i-j\) | 由首列+首行 \(O(n)\) 决定，求解 \(O(n^2)\) |
| 循环 | 特殊 Toeplitz，每行是上一行的循环移位 | 由单个向量 \(O(n)\) 决定，求解 \(O(n\log n)\) |

抓住结构，就能同时**省内存**（只存决定性参数）和**省算力**（调用专用算法）。

### 2.2 LAPACK 例程的命名约定

scipy.linalg 的结构化求解器大多是「Python 校验层 + LAPACK 数值内核」的薄壳（见 u2-l2、u2-l3）。LAPACK 例程名形如 `Xyyzz`：

- `X`：类型前缀，`s`/`d`/`c`/`z` 分别表示单精度实、双精度实、单精度复、双精度复。
- `yy`：问题类别，如 `gt`=三对角（tridiagonal）、`gb`=一般带状、`pt`=正定三对角、`pb`=正定带状、`tr`=三角。
- `zz`：算法，`sv`=solve（带分解）、`trs`=仅回代/前代（triangular solve）。

例如 `dgtsv` = 双精度三对角求解，`zpbsv` = 复双精度正定带状求解。Python 层通过 `get_lapack_funcs(('gtsv',), ...)` 自动按输入 dtype 选前缀（见 u2-l3）。

### 2.3 傅里叶变换对角化循环矩阵

循环矩阵 \(C\) 被离散傅里叶矩阵 \(F\) 对角化：其特征值恰是首列 \(c\) 的 FFT。本讲 4.4 会用到这一性质，届时再展开公式。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [_basic.py](_basic.py) | 所有公共结构化求解器的 Python 入口与校验层，`solve_banded`/`solveh_banded`/`solve_triangular`/`solve_toeplitz`/`solve_circulant`/`matmul_toeplitz` 全在这里。 |
| [_solve_toeplitz.pyx](_solve_toeplitz.pyx) | Cython 实现的 `levinson` 内核，是 `solve_toeplitz` 的数值核心，做 \(O(n^2)\) 的 Levinson 递归。 |
| [_matfuncs_inv_ssq.py](_matfuncs_inv_ssq.py) | 矩阵函数（分数幂、logm）的实现，其中反复调用 `solve_triangular`，证明三角求解器是「被复用的积木」而非孤立的入口。 |

> 提示：本讲引用的所有源码都在当前 HEAD `de190e7fde` 下，行号以该提交为准。

## 4. 核心概念与源码讲解

### 4.1 带状矩阵的存储格式与 solve_banded / solveh_banded

#### 4.1.1 概念说明

带状（banded）矩阵指只有主对角线附近一个「带」内的元素非零。设下带宽为 \(l\)（主对角线下方有 \(l\) 条非零对角线）、上带宽为 \(u\)（上方 \(u\) 条），则当 \(|i-j|>u\) 且 \(j-i>l\) 时 \(a_{ij}=0\)。

如果仍按 \(n\times n\) 稠密存储，大量零元素会浪费内存，分解时也会做大量乘零的无用功。LAPACK 的做法是**只存带内的元素**，压成一个 `(l+u+1, n)` 的二维数组 `ab`，并约定映射关系：

\[
\texttt{ab}[u+i-j,\; j] = a_{ij}
\]

即「把每条对角线放进 `ab` 的一行」，带外的位置用 `*`（任意值，不参与计算）填充。

`scipy.linalg` 提供两个带状求解器：

- `solve_banded((l,u), ab, b)`：一般带状，内部走 LU 风格的带状分解。
- `solveh_banded(ab, b, lower=...)`：**对称/Hermitian 正定**带状，走 Cholesky 风格的分解（Thomas 算法），更快更省内存，但矩阵不正定时会报错。

#### 4.1.2 核心流程

`solve_banded` 的分派逻辑：

```text
solve_banded((l,u), ab, b)
  └─ _solve_banded(...)
       ├─ 校验: ab.shape[0] == l+u+1 ?
       ├─ 若 n == 1:        直接标量除法 b/a  (1×1 特例)
       ├─ 若 l == u == 1:   取 LAPACK 'gtsv'  (三对角，最快路径)
       └─ 否则:             取 LAPACK 'gbsv'  (一般带状)
            └─ 给 ab 顶部补 l 行零, 再传入 gbsv
```

`solveh_banded` 的分派逻辑（注意它只存一半带宽，所以 `ab.shape[0]` 等于「单侧带宽+1」）：

```text
solveh_banded(ab, b, lower)
  ├─ 若 ab.shape[0] == 2:  取 LAPACK 'ptsv'  (正定三对角)
  │     └─ 从 ab 抽出实对角 d 与次对角 e (复数取共轭)
  └─ 否则:                 取 LAPACK 'pbsv'  (正定带状)
       └─ info>0 报 "{k}th leading minor not positive definite"
```

`pbsv`/`ptsv` 失败时 `info>0` 表示第 `info` 个顺序主子式不正定，Python 层据此抛 `LinAlgError`。

#### 4.1.3 源码精读

公共入口 [`solve_banded`](_basic.py) 只做拆包后委派：

[ `_basic.py` L472-L474 ](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L472-L474)：把 `(l,u)` 拆成 `nlower, nupper`，转交内部 `_solve_banded`。注意函数签名上的 `@_apply_over_batch(...)` 装饰器，它支持把带状求解「向量化」到一批矩阵上（批处理维度见 u8-l1）。

真正的分派与 LAPACK 调用在内部函数：

[ `_basic.py` L507-L525 ](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L507-L525)：三对角特例 `gtsv` 与一般带状 `gbsv`。`gtsv` 路径里 `du=a1[0,1:]`（上次对角）、`d=a1[1,:]`（主对角）、`dl=a1[2,:-1]`（下次对角），正是把压缩存储重新拆成三条对角线喂给三对角求解器。`gbsv` 路径则额外构造 `a2 = zeros((2*l+u+1, n))` 并把 `a1` 放到下方——因为 LAPACK `gbsv` 要求工作数组行数比用户输入多 `l` 行用于分解时的填充。

[`solveh_banded`](_basic.py) 的正定分支：

[ `_basic.py` L650-L668 ](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L650-L668)：`ab.shape[0]==2`（即带宽 1，三对角）走 `ptsv`，否则走 `pbsv`。注意三对角分支里 `lower` 决定从 `ab` 的哪一行取主对角 `d`（取 `.real`，因为 Hermitian 矩阵对角必为实）与次对角 `e`（上存时取 `.conj()`，把上三角的复数共轭还原成下次对角）。

#### 4.1.4 代码实践

**实践目标**：理解带状压缩存储 `ab` 的「对角线打包」方式，并用 `solve_banded` 求解后与通用 `solve` 对比。

操作步骤（示例代码）：

```python
import numpy as np
from scipy.linalg import solve, solve_banded

# 5x5 带状矩阵, l=1, u=2
A = np.array([[5, 2, -1, 0, 0],
              [1, 4,  2, -1, 0],
              [0, 1,  3,  2, -1],
              [0, 0,  1,  2,  2],
              [0, 0,  0,  1,  1]], dtype=float)
b = np.array([0, 1, 2, 2, 3], dtype=float)

# 按公式 ab[u+i-j, j] = a[i,j] 手工打包, 带外填 0(*)
ab = np.array([[0,  0, -1, -1, -1],   # 上第2对角 (u=2)
               [0,  2,  2,  2,  2],   # 上第1对角 (u=1)
               [5,  4,  3,  2,  1],   # 主对角   (u=0)
               [1,  1,  1,  1,  0]])  # 下第1对角 (u-1=-1 -> 第3行)
x_band = solve_banded((1, 2), ab, b)
x_dense = solve(A, b)

print(x_band)                 # 预期: [-2.3729, 3.9322, -4., 4.3559, -1.3559]
print(np.allclose(x_band, x_dense))   # 预期: True
print(A @ x_band - b)         # 预期: 残差接近 0
```

需要观察的现象与预期结果：

1. `ab` 共 4 行（\(l+u+1=1+2+1=4\)），与 `A` 的 5 行相比压缩了 1 行；矩阵越大、带宽越窄，压缩比越高。
2. `solve_banded` 与通用 `solve` 结果数值一致（`allclose` 为 `True`），验证压缩存储正确。
3. 故意把 `ab` 的某条对角线填错，会得到与 `solve` 不一致的结果——这能帮你确认自己理解了 `u+i-j` 的偏移规则。

> 待本地验证：若你构造一个 \(1000\times 1000\)、带宽为 5 的带状矩阵，`solve_banded` 应比 `solve` 快一两个数量级（带状分解是 \(O(n\cdot(l+u)^2)\)，而通用 LU 是 \(O(n^3)\)）。

#### 4.1.5 小练习与答案

**练习 1**：一个 \(6\times6\) 矩阵只有主对角线和紧邻的上下各一条对角线非零（三对角），用 `solve_banded` 时 `l_and_u` 取什么？`ab` 几行？

答案：`l_and_u=(1,1)`；`ab` 有 \(l+u+1=3\) 行。此时 `_solve_banded` 会走 `gtsv` 三对角特例路径（见 [L507-L514](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L507-L514)）。

**练习 2**：把一个不正定的「带状」矩阵交给 `solveh_banded` 会怎样？应该改用哪个函数？

答案：`pbsv`/`ptsv` 返回 `info>0`，Python 层抛 `LinAlgError: ... leading minor not positive definite`（见 [L664-L665](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L664-L665)）。应改用不要求正定的 `solve_banded`。

---

### 4.2 三角系统求解 solve_triangular

#### 4.2.1 概念说明

三角矩阵（上三角或下三角）已经「半分解完成」。求解三角系统 \(Lx=b\)（前代）或 \(Ux=b\)（回代）只需 \(O(n^2)\)——从第一个或最后一个方程开始，逐个变量直接解出，无需再做分解。

这正是 `solve_triangular` 的价值：当你**已经持有**一个三角因子（比如 LU、Cholesky、QR 分解得到的 \(L\) 或 \(U\)），想换一个右端 \(b\) 重新求解时，直接用三角求解器比重跑一遍完整分解快得多。它也因此成为矩阵函数模块内部反复调用的「积木」（见 4.2.3）。

三个关键参数：

- `lower`：用 `a` 的下三角（`True`）还是上三角（`False`，默认）。注意是「告诉函数 `a` 是哪种三角」，不是「只取一半」。
- `unit_diagonal`：若 `True`，假设对角元全为 1 且不读取（适用于某些分解约定）。
- `trans`：解哪种系统——`0/'N'` 解 \(Ax=b\)，`1/'T'` 解 \(A^Tx=b\)，`2/'C'` 解 \(A^Hx=b\)。

#### 4.2.2 核心流程

```text
solve_triangular(a, b, trans, lower, unit_diagonal)
  ├─ 校验: a 方阵? a 与 b 第一维兼容?
  ├─ overwrite_b = overwrite_b or _datacopied(b1, b)   # 自动检测是否已拷贝
  └─ _solve_triangular(...)
       ├─ trans 归一化: 'N'->0, 'T'->1, 'C'->2
       ├─ 取 LAPACK 'trtrs'
       ├─ 若 a 是 F 列主序连续 或 trans==2:
       │      直接调 trtrs(a, b, lower, trans, unitdiag)
       └─ 否则:  # trtrs 期望 Fortran 序, 对 C 序矩阵改解转置系统
              调 trtrs(a.T, b, lower=not lower, trans=not trans, ...)
       └─ info==0 返回 x; info>0 报 singular; info<0 报非法参数
```

这里有一个容易忽略的细节：LAPACK 是面向 Fortran 列主序设计的，而用户传入的 `a` 可能是 C 行主序（`np.array` 默认）。当 `a` 不是列连续、且 `trans` 不是共轭转置 `'C'` 时，函数**转而求解转置系统**（`a.T`、`lower` 取反、`trans` 取反），等价但避免了一次显式转置拷贝。

#### 4.2.3 源码精读

公共入口 [`solve_triangular`](_basic.py)：

[ `_basic.py` L368-L371 ](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L368-L371)：`overwrite_b = overwrite_b or _datacopied(b1, b)` 这一行承接 u2-l2 引入的 `_datacopied`——只有当 `b` 已经被 `_asarray_validated` 拷贝过时，才安全地允许原地覆写。

无校验版 [`_solve_triangular`](_basic.py) 的列主序处理：

[ `_basic.py` L378-L392 ](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L378-L392)：先 `{'N':0,'T':1,'C':2}.get(trans, trans)` 把字符与整数统一，再按 `a1.flags.f_contiguous` 与 `trans==2` 二选一调用 `trtrs`。`info>0` 时抛 `LinAlgError: singular matrix: resolution failed at diagonal {info-1}`，精确定位是第几个对角元为零导致失败。

**作为积木被复用**：`solve_triangular` 不只是公共 API。在矩阵函数 [`_matfuncs_inv_ssq.py`](_matfuncs_inv_ssq.py) 里，分数幂的 Padé 求值就用它解上三角系统：

[ `_matfuncs_inv_ssq.py` L506-L510 ](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs_inv_ssq.py#L506-L510)：在分数幂 Padé 循环中 `Y = solve_triangular(ident + Y, rhs)`。因为 `ident+Y` 是上三角，这里相当于调用 `trtrs` 做一次 \(O(n^2)\) 回代——这正是 4.2.1 说的「持有三角因子后高效重解」的真实用例。`_logm_triu` 中也有类似调用（[L792](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs_inv_ssq.py#L792)）。

#### 4.2.4 代码实践

**实践目标**：验证三角求解的 \(O(n^2)\) 直观，并体会 `lower`/`trans` 的含义。

```python
import numpy as np
from scipy.linalg import solve_triangular

# 下三角矩阵
L = np.array([[3, 0, 0, 0],
              [2, 1, 0, 0],
              [1, 0, 1, 0],
              [1, 1, 1, 1]], dtype=float)
b = np.array([4, 2, 4, 2], dtype=float)

x = solve_triangular(L, b, lower=True)         # 解 L x = b
print(x)                          # 预期: [1.3333, -0.6667, 2.6667, -1.3333]
print(L @ x - b)                  # 预期: 残差为 0

# trans='T': 解 L^T x = b (注意 L^T 是上三角, 但仍传 lower=True 因为 a=L)
xT = solve_triangular(L, b, lower=True, trans='T')
print(L.T @ xT - b)               # 预期: 残差为 0

# unit_diagonal: 假设对角为 1, 不读取对角元
U_unit = np.array([[99, 5, 7],   # 对角 99 会被忽略
                   [0,  99, 6],
                   [0,  0, 99]], dtype=float)
xu = solve_triangular(U_unit, [1, 1, 1], unit_diagonal=True)
# 等价于解 [[1,5,7],[0,1,6],[0,0,1]] x = [1,1,1]
print(xu)                         # 预期: [1., -6., 35.] (待本地验证)
```

需要观察的现象与预期结果：

1. `solve_triangular(L, b, lower=True)` 与手动前代一致，残差为 0。
2. `trans='T'` 解的是 \(L^Tx=b\)，用 `L.T @ xT` 验证。
3. `unit_diagonal=True` 时对角元 `99` 被当成 `1`，结果与显式单位上三角一致。

> 待本地验证：最后一段 `unit_diagonal` 的精确数值结果请在本地跑一次确认。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_solve_triangular` 在 `a` 不是列连续时要改解「转置系统」而不是直接转置 `a`？

答案：直接 `a.T` 在 NumPy 里只是视图，但 `trtrs` 期望列主序连续内存；与其做一次显式拷贝转置，不如利用「解 \(A^Tx=b\) 等价于在转置因子上换方向」这一数学等价性，把 `lower`、`trans` 同时取反，零拷贝完成（见 [L384-L386](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L384-L386)）。

**练习 2**：`solve_triangular` 报 `singular matrix: resolution failed at diagonal 2`，是什么意思？

答案：对应 `info>0` 且 `info-1==2`，即第 3 个（0 基下标 2）对角元为零，回代/前代在这一步无法继续（见 [L390-L391](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L390-L391)）。

---

### 4.3 Toeplitz 系统求解：solve_toeplitz 与 Levinson 递归

#### 4.3.1 概念说明

Toeplitz 矩阵的每条对角线取值相同，即 \(T_{ij}\) 只依赖差值 \(i-j\)。整矩阵由「首列 \(c\)」和「首行 \(r\)」完全决定：\(T_{ij} = \begin{cases} c_{i-j} & i\ge j \\ r_{j-i} & i<j \end{cases}\)。存储只需 \(O(n)\) 而非 \(O(n^2)\)。

直接对 Toeplitz 矩阵做通用 LU 是 \(O(n^3)\)，浪费了结构。Levinson-Durbin 递归利用 Toeplitz 的「嵌套」性质——\(n\times n\) 解可由 \((n-1)\times(n-1)\) 解递推得到——把求解降到 \(O(n^2)\)。这正是 `solve_toeplitz` 的算法，数值核心用 Cython 实现。

scipy 提供两个相关入口：

- `solve_toeplitz(c_or_cr, b)`：解 \(Tx=b\)，走 Levinson。
- `matmul_toeplitz(c_or_cr, x)`：算 \(Tx\)（矩阵乘向量/矩阵），但走的是 FFT，不是 Levinson（见 4.4）。

#### 4.3.2 核心流程

`solve_toeplitz` 的 Python 层只是「拼装首列+首行」再委派：

```text
solve_toeplitz(c_or_cr, b)
  ├─ 若传入元组 (c, r): 直接用
  └─ 若只传 c:          r = conjugate(c)   # 此时若 c[0] 为实, T 是 Hermitian
  └─ _solve_toeplitz(c, r, b)   # @_apply_over_batch, 支持批量
       ├─ _validate_args_for_toeplitz_ops: 校验+统一 dtype (任一复数则 complex128)
       ├─ vals = concatenate(( r[-1:0:-1], c ))   # 长度 2n-1 的"压扁"数组
       └─ levinson(vals, b)   # Cython 内核, O(n^2) 递归
            └─ 返回 (x, reflection_coeff)
```

关键变换：把首列 \(c\) 和「翻转去掉首元」的首行拼成一条长度 \(2n-1\) 的数组 `vals`，正好对应 levinson 内核期望的输入格式（详见下文源码）。

Levinson 递归的直觉：维护三组数组——当前解 `x`、两个辅助向量 `g`/`h`（相当于正向/反向预测残差），以及 `reflection_coeff`（反射系数，在自回归问题里就是偏自相关函数）。每步 `m` 把解从长度 \(m\) 扩展到 \(m+1\)：先用 `g` 修正已有解，再用 `h` 计算下一步的反射系数。所有主子式非奇异（`x_den`、`g_den` 不为 0）时算法成功。

#### 4.3.3 源码精读

公共入口 [`solve_toeplitz`](_basic.py)：

[ `_basic.py` L746-L747 ](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L746-L747)：一行解包 `c_or_cr`，单参数时默认 `r=conjugate(c)`，使实 `c[0]` 自动构成 Hermitian Toeplitz。然后转交 `_solve_toeplitz`。

内部 [`_solve_toeplitz`](_basic.py) 的拼装：

[ `_basic.py` L759-L769 ](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L759-L769)：`vals = np.concatenate((r[-1:0:-1], c))` 把 `r` 反向去掉首元后拼到 `c` 前。多右端时对 `b` 的每一列各调一次 `levinson`（向量化在 `@_apply_over_batch` 层处理）。

校验与 dtype 归一化在 [`_validate_args_for_toeplitz_ops`](_basic.py)：

[ `_basic.py` L1916-L1922 ](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L1916-L1922)：检查方形（`enforce_square`）与维度兼容；只要 `r`、`c`、`b` 任一为复数，整体提升为 `complex128`，否则 `float64`——因为 levinson 内核只接受这两种 dtype。

**Cython 数值核心 `levinson`**：

[ `_solve_toeplitz.pyx` L14-L46 ](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_solve_toeplitz.pyx#L14-L46)：函数签名 `def levinson(const dz[::1] a, const dz[::1] b)`，其中 `dz` 是 `fused type`（`float64_t` 或 `complex128_t`），用一份代码同时编译出双精度实/复两个版本（与 u1-l3 讲过的「一份模板管多类型」同思路）。`const dz[::1]` 是只读连续 memoryview，配合文件顶部 `# cython: boundscheck=False, wraparound=False, cdivision=True` 关掉边界检查与负索引，换取 C 级速度。`assert len(a) == (2*n)-1` 验证压扁数组长度。

[ `_solve_toeplitz.pyx` L66-L88 ](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_solve_toeplitz.pyx#L66-L88)：递归的初始化与主循环开头。`a[n-1]` 是对角元（压扁数组的正中央），为零则抛 `LinAlgError('Singular principal minor')`；`x[0]=b[0]/a[n-1]` 起步。主循环 `for m in range(1, n)` 里先算新分量 `x[m]` 的分子分母，`x_den==0` 再次触发「奇异主子式」错误。

[ `_solve_toeplitz.pyx` L98-L126 ](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_solve_toeplitz.pyx#L98-L126)：用 `g`、`h` 计算反射系数并就地更新这两个辅助向量。注意 `m2=(m+1)>>1` 与 `k=m-1` 的对称指针——这是把长度 \(m\) 的向量「两端同时往中间更新」的典型写法，省去临时数组。

#### 4.3.4 代码实践

**实践目标**：用 `solve_toeplitz` 求解一个 Toeplitz 系统，并与通用 `solve` 对比，确认 Levinson 的正确性。

```python
import numpy as np
from scipy.linalg import solve, solve_toeplitz, toeplitz

c = np.array([1, 3, 6, 10])       # 首列
r = np.array([1, -1, -2, -3])     # 首行 (r[0] 必须等于 c[0])
b = np.array([1, 2, 2, 5], dtype=float)

# 只给首列+首行, 不显式构造矩阵
x = solve_toeplitz((c, r), b)
print(x)                          # 预期: [1.6667, -1., -2.6667, 2.3333]

# 用稠密 Toeplitz 矩阵走通用 solve 对比
T = toeplitz(c, r)
x_dense = solve(T, b)
print(np.allclose(x, x_dense))    # 预期: True
print(T @ x - b)                  # 预期: 残差为 0
```

需要观察的现象与预期结果：

1. 传入 `(c, r)` 元组时，`r[0]` 会被忽略（首行取 `[c[0], r[1:]]`），所以即使 `r[0]` 写错也不影响结果——但建议保持 `r[0]==c[0]` 以免误解。
2. `solve_toeplitz` 与 `solve(toeplitz(c,r), b)` 数值一致。
3. 当 `c` 为实向量、只传单参数 `solve_toeplitz(c, b)` 时，`r=conjugate(c)=c`，得到对称 Toeplitz 系统。

> 待本地验证：构造一个首列含复数的 Toeplitz 系统，确认 `solve_toeplitz` 自动走 `complex128` 内核并与 `solve` 一致。

#### 4.3.5 小练习与答案

**练习 1**：`solve_toeplitz` 文档说 Levinson 「faster than generic least-squares methods, but can be less numerically stable」。结合源码，不稳定来自哪里？

答案：递归每步都要做 `x_num/x_den`、`g_num/g_den` 这样的除法（[L89](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_solve_toeplitz.pyx#L89)、[L111](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_solve_toeplitz.pyx#L111)），当某个主子式接近奇异时分母很小，误差会被逐步放大。源码注释也提到若稳定性成问题，可改用 GKO 或 Bareiss 等其他 \(O(n^2)\) 求解器（见 [L743-L745](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L743-L745)）。

**练习 2**：为什么 `levinson` 要求输入是长度 \(2n-1\) 的「压扁」数组，而不是直接给 `c` 和 `r` 两个向量？

答案：压扁数组 `vals=[r[-1:0:-1], c]` 把整条 Toeplitz 矩阵的对角取值按「从最下次对角到最上超对角」排成一条，递归中用单个下标 `a[n-1+m-(j+1)]` 就能取到任意对角元（见 [L84-L85](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_solve_toeplitz.pyx#L84-L85)），避免在 Cython 里处理两段数组与下标换算，既快又简洁。

---

### 4.4 循环矩阵求解 solve_circulant 与 matmul_toeplitz：FFT 路径

#### 4.4.1 概念说明

循环（circulant）矩阵是 Toeplitz 的特例：每一行是上一行的循环右移。它由单个向量 \(c\)（首列）完全决定。循环矩阵最大的好处是**被傅里叶矩阵对角化**：设 \(F\) 为离散傅里叶矩阵，\(\hat{c}=\mathrm{fft}(c)\)，则

\[
C = F^{H}\,\mathrm{diag}(\hat{c})\,F
\]

即 \(C\) 的特征值就是 \(\mathrm{fft}(c)\)。于是解 \(Cx=b\) 变成纯逐元素运算：

\[
x = F^{H}\,\mathrm{diag}(\hat{c})^{-1}\,F\,b
   = \mathrm{ifft}\!\left(\frac{\mathrm{fft}(b)}{\mathrm{fft}(c)}\right)
\]

复杂度由通用 \(O(n^3)\) 降到 \(O(n\log n)\)（两次 FFT + 一次逐元素除法）。这正是 `solve_circulant` 的全部原理。

`matmul_toeplitz` 走的也是 FFT：把 Toeplitz 矩阵「嵌入」一个更大的循环矩阵，用 FFT 算乘积，再把结果截回原尺寸——复杂度 \(O(n\log n)\)，且从不显式构造 \(n\times n\) 矩阵，适合超大规模 Toeplitz。

#### 4.4.2 核心流程

`solve_circulant` 的流程：

```text
solve_circulant(c, b, singular, tol, ...)
  ├─ fc = fft(c)                       # 循环矩阵的特征值
  ├─ abs_fc = |fc|; tol 默认 = max(|fc|) * n * eps
  ├─ near_zeros = abs_fc <= tol        # 近零特征值 = 近奇异
  ├─ 若近奇异:
  │     singular='raise' -> 抛 LinAlgError
  │     singular='lstsq' -> 把近零处置 1, 最后把对应结果置 0 (最小二乘)
  ├─ fb = fft(b)
  ├─ q = fb / fc                       # 频域逐元素除法
  └─ x = ifft(q); 实输入取 .real
```

`tol` 的默认值 `max(|fc|) * n * eps` 与 `np.linalg.matrix_rank` 的容差一致，是一个「相对」阈值：只要某个特征值比最大的小 \(n\) 个机器精度量级，就视为零。

`matmul_toeplitz` 的流程：

```text
matmul_toeplitz(c_or_cr, x)
  └─ _matmul_toepltiz(r, c, x, ...)
       ├─ embedded_col = concatenate((c, r[-1:0:-1]))   # 嵌入循环矩阵的首列
       ├─ p = len(c) + len(r) - 1                        # 嵌入后的尺寸
       ├─ fft_mat = fft(embedded_col)   (复) / rfft(...) (实, 更快)
       ├─ fft_x   = fft(x, n=p)         / rfft(...)
       └─ 取 ifft(fft_mat*fft_x)[:len(c)] 截回 Toeplitz 的行数
```

实数输入走 `rfft`/`irfft`（只存一半频率，更快），复数输入走完整 `fft`/`ifft`。

#### 4.4.3 源码精读

[`solve_circulant`](_basic.py) 的频域除法：

[ `_basic.py` L942-L946 ](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L942-L946)：`fc=np.fft.fft(...)` 得特征值；`tol` 默认 `abs_fc.max()*nc*eps`。`caxis`/`baxis`/`outaxis` 三个轴参数让函数能处理「一批循环向量与一批右端」的广播求解。

[ `_basic.py` L952-L964 ](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L952-L964)：近奇异处理——`singular='lstsq'` 时先把 `fc` 的近零位置成 1（避免除零），算完 `q=fb/fc` 后再用掩码把对应位置置 0，得到最小二乘解。

[ `_basic.py` L975-L980 ](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L975-L980)：`x=np.fft.ifft(q)`；若 `c`、`b` 都不是复对象，取 `.real` 丢弃数值噪声产生的微小虚部。

[`matmul_toeplitz`](_basic.py) 的嵌入与 FFT：

[ `_basic.py` L2081-L2095 ](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L2081-L2095)：`embedded_col=concatenate((c, r[-1:0:-1]))` 把 Toeplitz 嵌入 \(p=n+m-1\) 阶循环矩阵；实数走 `rfft` 分支，复数走 `fft` 分支；最后 `[:T_nrows,:]` 截回原行数。注意 `_matmul_toepltiz` 函数名里有个拼写（toepl*t*iz），但导出的公共名是正确的 `matmul_toeplitz`。

#### 4.4.4 代码实践

**实践目标**：用 `solve_circulant` 解循环系统并与通用 `solve` 对比；体会近奇异时 `singular='lstsq'` 的行为。

```python
import numpy as np
from scipy.linalg import solve_circulant, solve, circulant, lstsq

c = np.array([2, 2, 4])
b = np.array([1, 2, 3])

x = solve_circulant(c, b)
print(x)                                   # 预期: [0.75, -0.25, 0.25]
print(solve(circulant(c), b))              # 预期: 同上

# 近奇异: c=[1,1,0,0] 对应的循环矩阵奇异
cs = np.array([1., 1., 0., 0.])
bs = np.array([1., 2., 3., 4.])
# solve_circulant(cs, bs)                  # 默认会抛 LinAlgError("near singular ...")
x_ls = solve_circulant(cs, bs, singular='lstsq')
print(x_ls)                                # 预期: [0.25, 1.25, 2.25, 1.25]
xx, *_ = lstsq(circulant(cs), bs)
print(np.allclose(x_ls, xx))               # 预期: True
```

需要观察的现象与预期结果：

1. `solve_circulant(c, b)` 与 `solve(circulant(c), b)` 数值一致，但前者从不构造 \(n\times n\) 矩阵。
2. 默认 `singular='raise'` 时近奇异抛 `LinAlgError`；`singular='lstsq'` 给出与 `lstsq` 一致的最小二乘解。
3. （扩展）用 `matmul_toeplitz([1]+[0]*999999, np.ones(1000000))` 算一个百万维 Toeplitz 乘积，应几乎瞬间返回——因为它走 FFT，从不构造百万阶矩阵（见文档示例 [L2053-L2055](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L2053-L2055)）。

> 待本地验证：第 3 项百万维乘积的实际耗时请在本地体验。

#### 4.4.5 小练习与答案

**练习 1**：为什么循环矩阵的特征值恰好是 `fft(c)`？

答案：循环矩阵 \(C\) 的第 \(k\) 个标准列向量恰是首列 \(c\) 循环移位 \(k\) 次；而 DFT 基向量 \(v_k=(1,\omega^k,\omega^{2k},\dots)\)（\(\omega=e^{-2\pi i/n}\)）在循环移位下只乘一个常数 \(\omega^k\)，所以 \(Cv_k=\mathrm{fft}(c)_k\cdot v_k\)，即 \(v_k\) 是特征向量、\(\mathrm{fft}(c)_k\) 是特征值。这正是 4.4.1 公式的来源。

**练习 2**：`solve_circulant` 默认 `tol` 为 `max(|fc|)*n*eps`，为什么乘 `n`？

答案：这是相对容差，乘 `n`（矩阵规模）是数值线性代数的惯例（与 `np.linalg.matrix_rank` 一致，见 [L945-L946](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L945-L946)）：大矩阵累计舍入误差更大，阈值随 \(n\) 放宽，避免把纯由舍入噪声产生的「假特征值」当成有效值。

---

## 5. 综合实践

把本讲四种结构串起来，完成下面这个「同一问题、四种结构、对比验证」的小任务。

**任务背景**：构造一个对称正定三对角矩阵 \(A\)（它同时是带状、对称正定带状、Toeplitz），用四种路径分别求解 \(Ax=b\)，再互相验证。

```python
import numpy as np
from scipy.linalg import (solve, solve_banded, solveh_banded,
                          solve_toeplitz, toeplitz)

n = 5
# 对称正定三对角 Toeplitz: 主对角 4, 次对角 1
c = np.array([4., 1., 0., 0., 0.])          # 首列 -> 对称 Toeplitz
A = toeplitz(c)                              # 稠密形式, 用于通用 solve
b = np.arange(1., n+1)

# 路径1: 通用 solve (O(n^3), 自动检测)
x1 = solve(A, b)

# 路径2: 带状 solve_banded, l=u=1 (O(n))
ab = np.array([[0, 1, 1, 1, 1],             # 上次对角 (u=1)
               [4, 4, 4, 4, 4],             # 主对角
               [1, 1, 1, 1, 0]])            # 下次对角
x2 = solve_banded((1, 1), ab, b)

# 路径3: 对称正定带状 solveh_banded, 上存 (带宽1 -> ab 2行)
ab_h = np.array([[0, 1, 1, 1, 1],           # 上次对角
                 [4, 4, 4, 4, 4]])          # 主对角
x3 = solveh_banded(ab_h, b)                 # 默认 lower=False (上存)

# 路径4: Toeplitz, 只给首列 (Hermitian, 因为 c 实)
x4 = solve_toeplitz(c, b)

print(np.allclose(x1, x2), np.allclose(x1, x3), np.allclose(x1, x4))
# 预期: True True True
```

**讨论要点**（请在本地验证后思考）：

1. 四条路径结果应完全一致（`allclose` 全 `True`），证明四种结构化求解器等价。
2. 它们的「输入格式」截然不同：路径1 给稠密矩阵、路径2/3 给压缩带状数组、路径4 只给首列——这正是「结构化」的代价：你需要**自己**把矩阵表示成对应格式，换取速度与内存。
3. 复杂度对比：路径1 是 \(O(n^3)\)，路径2/3 是 \(O(n)\)（带宽固定），路径4 是 \(O(n^2)\)（Levinson）。当 \(n\) 很大时，结构化路径的优势会非常明显。

> 待本地验证：把 `n` 调到 2000，比较 `solve(A,b)` 与 `solveh_banded(ab_h,b)` 的耗时差异。

## 6. 本讲小结

- **结构化求解的核心思想**：抓住矩阵的零元/取值规律，用更紧凑的存储格式（带状 `ab`、首列首行、单个向量）和更快的专用算法（带状分解、三角回代、Levinson、FFT），把求解复杂度从通用 \(O(n^3)\) 降下来。
- **带状**：`solve_banded` 走 LAPACK `gtsv`（三对角）/`gbsv`（一般带状）；`solveh_banded` 走 `ptsv`/`pbsv`，要求正定，存储映射为 `ab[u+i-j,j]=a[i,j]`。
- **三角**：`solve_triangular` 走 `trtrs`，\(O(n^2)\)，靠 `lower`/`unit_diagonal`/`trans` 描述系统；它还是矩阵函数内部反复调用的「积木」（见 `_matfuncs_inv_ssq.py`）。
- **Toeplitz**：`solve_toeplitz` 把首列首行压成 \(2n-1\) 数组，交给 Cython 内核 `levinson` 做 \(O(n^2)\) Levinson-Durbin 递归；快但稳定性弱于最小二乘。
- **循环**：`solve_circulant` 利用「循环矩阵被 DFT 对角化」，\(x=\mathrm{ifft}(\mathrm{fft}(b)/\mathrm{fft}(c))\)，\(O(n\log n)\)，近奇异时 `singular='lstsq'` 退化为最小二乘。
- **公共机制**：所有结构化求解器都是「Python 校验层 + 编译数值内核」的薄壳，并复用 `_datacopied`（覆写门控）、`@_apply_over_batch`（批量维度）等贯穿全包的基础设施——这些与 u2-l2、u2-l3 一脉相承。

## 7. 下一步学习建议

- **进入矩阵分解专题（u3）**：本讲的三角求解、带状分解是 LU/Cholesky/QR 等分解的「下游」。读完 u3-l1（LU）和 u3-l2（Cholesky）后，你会更清楚 `lu_factor`/`cho_factor` 产出的三角因子如何配合 `solve_triangular` 反复求解。
- **深入 Cython 内核**：若你对 `levinson` 的 `fused type`、memoryview、`nogil` 感兴趣，可先读 u7-l4（Cython 扩展实践），那里会把 `_solve_toeplitz.pyx` 与 `_cythonized_array_utils.pyx` 放在一起讲。
- **批量化**：本讲所有函数都带 `@_apply_over_batch`，支持批处理维度。u8-l1 会专门讲批处理维度的广播规则与 `_format_emit_errors_warnings` 的错误聚合。
- **建议继续阅读的源码**：`_basic.py` 中 [`_validate_args_for_toeplitz_ops`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py#L1859-L1929) 看 Toeplitz 入参校验；`_decomp_cholesky.py` 看带状 Cholesky 的更完整接口（`cholesky_banded`/`cho_solve_banded`）。
