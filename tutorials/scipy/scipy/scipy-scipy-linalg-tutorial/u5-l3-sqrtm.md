# 矩阵平方根 sqrtm 与 Schur 分块算法

## 1. 本讲目标

矩阵平方根是矩阵函数族里最「硬核」的一员：它不像 `expm`/`logm` 那样有干净的级数或 Padé 逼近，而是必须先做 Schur 分解、再在三角因子上做一种特殊的递推。本讲带你彻底读懂 `scipy.linalg.sqrtm` 的完整链路，学完后你应能：

1. 说清「矩阵平方根」的定义、主平方根的概念，以及为什么不是每个矩阵都存在平方根、为什么实矩阵的平方根可能变复数。
2. 掌握上三角矩阵平方根的核心递推公式，以及它与 **Sylvester 方程**（LAPACK `trsyl`）的等价关系。
3. 读懂两套「分块 Schur 平方根」实现：Cython 版 `_sqrtm_triu` + `within_block_loop`（迭代分块，现被 `logm`/`fractional_matrix_power` 复用）与纯 C 版 `sqrtm_recursion_*`（递归分块，**当前 `sqrtm` 实际走的路径**）。
4. 手动验证 `sqrtm(A)` 满足 \( R R \approx A \)，并解释负特征值情形为何结果为复数。

## 2. 前置知识

- **标量平方根到矩阵的推广**：对数 \( a \)，\( \sqrt{a} \) 满足 \( (\sqrt{a})^2 = a \)。对矩阵 \( A \)，我们同样要找一个矩阵 \( R \) 使 \( R^2 = A \)（即 `R @ R = A`），但它**绝不是**逐元素开方。
- **Schur 分解**（见 u3-l5）：任意方阵 \( A = Z T Z^H \)，其中 \( Z \) 是酉矩阵、\( T \) 是上三角（复数情形）或准上三角（实数情形，2×2 块承载复共轭特征值对）。本讲的关键思想是：**矩阵函数在酉相似下保持不变**，即 \( f(A) = Z f(T) Z^H \)，于是问题降维成「求上三角矩阵 \( T \) 的平方根」。
- **Sylvester 方程**：形如 \( AX + XB = C \) 的矩阵方程，求未知矩阵 \( X \)。当 \( A \)、\( B \) 的特征值之和不带「相反配对」时解唯一，这正是 LAPACK `?trsyl` 求解的对象。
- **LAPACK 前缀 s/d/c/z**（见 u7-l1）：实单/实双/复单/复双精度的类型分发。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [_matfuncs.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs.py) | `sqrtm` 的 Python 入口薄壳：校验、dtype 规范化、边界情形、把数值工作委派给 C 后端。 |
| [_matfuncs_sqrtm.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs_sqrtm.py) | **迭代分块**版本的三角平方根 `_sqrtm_triu`（`blocksize=64`），现为 `logm`/`fractional_matrix_power` 复用。 |
| [_matfuncs_sqrtm_triu.pyx](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs_sqrtm_triu.pyx) | Cython 内核 `within_block_loop`，加速「块内」逐元素递推。另有同名纯 Python 兜底 `_matfuncs_sqrtm_triu.py`。 |
| [src/_matfuncs_sqrtm.c](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/src/_matfuncs_sqrtm.c) | **递归分块**版本：`matrix_squareroot_{s,d,c,z}`（含 Schur 前端与实→复转换）+ `sqrtm_recursion_{s,d,c,z}`（递归三角平方根）。**当前 `sqrtm` 走的就是这条路径。** |
| [_matfuncsmodule.c](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncsmodule.c) | C 扩展 `_internal_matfuncs` 的方法注册表，把 Python 调用桥接到 `matrix_squareroot_*`。 |
| src/_common_array_utils.h | 提供 `isschurf`/`isschur`（判定是否已是 Schur 形）与 `swap_cf_*`（C/F 布局互转）等公共工具。 |

> **重要事实**：尽管本讲主题（沿用大纲描述）提到「先用 `_sqrtm_triu` 做分块平方根」，但当前 `scipy.linalg.sqrtm` 的公共入口已经**不再调用** `_sqrtm_triu`/`within_block_loop`，而是走纯 C 的 `sqrtm_recursion_*`。`_sqrtm_triu` 这一族代码如今被 `logm` 与 `fractional_matrix_power`（经 `_matfuncs_inv_ssq.py`）当作「开方积木」复用。两者实现的是**同一个分块 Schur 算法**（Deadman–Higham–Ralha 2013），所以先读懂较直观的 `_sqrtm_triu`，再去读 C 版会非常顺。

## 4. 核心概念与源码讲解

### 4.1 矩阵平方根的定义与 `sqrtm` 的 Python 薄壳

#### 4.1.1 概念说明

矩阵 \( A \) 的平方根是满足 \( R R = A \) 的矩阵 \( R \)。三个关键事实：

1. **不是逐元素开方**：`R[i,j] = sqrt(A[i,j])` 是错的，矩阵乘法有交叉项。
2. **不一定存在**：例如 \( \begin{pmatrix}0&1\\0&0\end{pmatrix} \) 没有任何平方根（它的 docstring 明确给出这个反例）。存在性的判定比较微妙，常见充分条件是「没有负实轴上的特征值」。
3. **主平方根（principal square root）**：当 \( A \) 没有负实轴（含原点）上的特征值时，存在唯一的、特征值都落在右半开平面的平方根，称为**主平方根**，这就是 `sqrtm` 试图返回的那个。
4. **实矩阵可能开成复数**：若实矩阵有负实特征值，其主平方根必为复矩阵——例如 \( \sqrt{-I} \) 是纯虚的。`sqrtm` 在「输入是实、但需要复结果」时会**自动升级返回类型为复数**。

#### 4.1.2 核心流程

`sqrtm` 的 Python 层只做「守门」与「翻译」，不做任何数值计算：

```
asarray(A)
  → 校验：至少 2D、末两维相等（方阵）
  → 边界：标量/空数组直接返回
  → dtype 规范化（统一到 fdFD 四类双精度）
  → 调 C 后端 recursive_schur_sqrtm(a)
  → 翻译返回的 (res, isIllconditioned, isSingular, info)
     · info<0  → 抛 LinAlgError（内部错误）
     · singular / ill-conditioned → 发 LinAlgWarning
  → 返回 res
```

注意它与 `expm`（u5-l1，C 后端内部完成 Padé）一样遵循「Python 薄壳 + 编译后端」的分工，但 `sqrtm` **没有** `@_apply_over_batch` 装饰器——批处理维度由 C 后端原生处理（在 `matrix_squareroot_*` 里按 slice 循环）。

#### 4.1.3 源码精读

入口函数定义与 docstring，明确给出了定义、反例与「实→复」的类型说明：

- [_matfuncs.py:326-388](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs.py#L326-L388) —— `def sqrtm(A):`，docstring 写明平方根定义、`[[0,1],[0,0]]` 无平方根的反例、以及实矩阵可能返回复数。

核心委派只有一行，把算力完全交给 C 后端：

- [_matfuncs.py:421-423](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs.py#L421-L423) —— 调用 `recursive_schur_sqrtm(a)`，拿到结果与三个状态标志；`info < 0` 直接抛 `LinAlgError`。

C 后端是从 `_internal_matfuncs` 导入的：

- [_matfuncs.py:19](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs.py#L19) —— `from ._internal_matfuncs import recursive_schur_sqrtm, matrix_exponential`。

病态/奇异的告警由 Python 层负责翻译（奇异 = 出现 0 特征值；病态 = Sylvester 求解器返回非零 info）：

- [_matfuncs.py:425-432](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs.py#L425-L432) —— 根据 `isSingular`/`isIllconditioned` 发出 `LinAlgWarning`，提示「结果可能不准确或矩阵可能没有平方根」。

C 侧的桥接函数与 dtype 分发：

- [_matfuncsmodule.c:43-54](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncsmodule.c#L43-L54) —— `recursive_schur_sqrtm`：解析输入数组，校验 dtype 必须是 `float32/float64/complex64/complex128`。
- [_matfuncsmodule.c:88-116](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncsmodule.c#L88-L116) —— 按输入 dtype 分派到 `matrix_squareroot_{s,d,c,z}`。注意对实数输入会**预分配两倍空间**，以便「中途升级为复数」时不必重新分配（详见 4.4）。
- [_matfuncsmodule.c:283-284](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncsmodule.c#L283-L284) —— 模块方法表把 `recursive_schur_sqrtm` 注册为 `_internal_matfuncs` 的方法。

#### 4.1.4 代码实践

1. **目标**：跑通 docstring 例子，确认 `R @ R == A`，并观察返回 dtype。
2. **步骤**：

   ```python
   import numpy as np
   from scipy.linalg import sqrtm
   a = np.array([[1.0, 3.0], [1.0, 4.0]])
   r = sqrtm(a)
   print(r)            # 期望接近 [[0.7559, 1.1339],[0.3779, 1.8898]]
   print(r.dtype)      # float64
   print(r @ r - a)    # 应接近 0
   ```

3. **观察**：`r @ r` 应回到原矩阵；`r.dtype` 为 `float64`（该矩阵特征值都为正，无需复数）。
4. **预期结果**：残差在 \(10^{-15}\) 量级。

#### 4.1.5 小练习与答案

**Q1**：`sqrtm(np.eye(2))` 返回什么？为什么？
**答**：返回 `eye(2)` 本身。单位阵的主平方根就是它自己（\( I^2 = I \)，且特征值 1 在右半平面）。

**Q2**：如果输入是整数数组 `np.array([[1,2],[3,4]])`，`sqrtm` 会报错吗？
**答**：不会。`sqrtm` 在 [_matfuncs.py:408-415](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs.py#L408-L415) 会把整数/低精度浮点统一 `astype` 到 `float64`（或 `complex128`），再交给后端。

### 4.2 三角平方根的递推：Sylvester 方程与 LAPACK `trsyl`

#### 4.2.1 概念说明

经过 Schur 分解 \( A = Z T Z^H \)，求 \( \sqrt{A} \) 归结为求上三角 \( T \) 的上三角平方根 \( R \)（满足 \( R^2 = T \)），再 \( \sqrt{A} = Z R Z^H \)。为什么 \( R \) 也是上三角？因为矩阵函数保持三角结构。

对 \( R^2 = T \) 展开**对角元**：\( R_{ii}^2 = T_{ii} \)，所以

\[ R_{ii} = \sqrt{T_{ii}}. \]

展开**严格上三角元** \( (i<j) \)。注意 \( (R^2)_{ij} = \sum_k R_{ik} R_{kj} \)，而 \( R \) 上三角使 \( k \) 的取值被限制在 \( i \le k \le j \)，拆出 \( k=i \) 与 \( k=j \) 两端：

\[ (R^2)_{ij} = R_{ii}R_{ij} + R_{ij}R_{jj} + \sum_{k=i+1}^{j-1} R_{ik}R_{kj} = T_{ij}. \]

整理得递推公式：

\[ R_{ij} = \frac{T_{ij} - \displaystyle\sum_{k=i+1}^{j-1} R_{ik}R_{kj}}{R_{ii} + R_{jj}}, \qquad i<j. \]

这个公式说明：只要按「从对角线向右上角、逐对角线」的顺序计算（保证算 \( R_{ij} \) 时 \( R_{ik} \)、\( R_{kj} \) 已就绪），就能逐元素填出 \( R \)。**分母 \( R_{ii}+R_{jj} \) 是两特征值的平方根之和**——若它为 0（两个相反特征值）而分子非 0，则平方根不存在。

**与 Sylvester 方程的等价性**：把三角矩阵分块 \( T = \begin{pmatrix} T_{11} & T_{12} \\ 0 & T_{22}\end{pmatrix} \)，对应 \( R = \begin{pmatrix} R_{11} & X \\ 0 & R_{22}\end{pmatrix} \)。由 \( R^2 = T \) 的右上块得：

\[ R_{11} X + X R_{22} = T_{12}. \]

这正是 **Sylvester 方程** \( AX + XB = C \)（取 \( A=R_{11}, B=R_{22}, C=T_{12} \)），由 LAPACK `?trsyl` 求解。于是「分块递推」与「逐元素递推」是同一件事在两个粒度上的体现——小块用逐元素公式（Cython），大块用 Sylvester 求解器（LAPACK）。

#### 4.2.2 核心流程

LAPACK `?trsyl` 求解 \( \mathrm{op}(A)\,X + \mathrm{isgn}\cdot X\,\mathrm{op}(B) = C \)，其中 `op` 可为转置/共轭转置/恒等。本讲一律用 `trana='N', tranb='N', isgn=+1`，即 \( AX+XB=C \)。

scipy 包装器 `get_lapack_funcs('trsyl', ...)` 返回的函数签名是 `trsyl(a, b, c, trana='N', tranb='N', isgn=1, ...)`，返回 `(x, scale, info)`。注意 **`scale`**：为防止溢出，求解器可能把解整体缩放，所以真正的解是 `x * scale`。`info>0` 表示 \( A \)、\( B \) 的某对特征值之和接近 0（方程接近奇异，即矩阵接近没有平方根）。

#### 4.2.3 源码精读

`_sqrtm_triu` 中对角元开方与取 `trsyl` 例程：

- [_matfuncs_sqrtm.py:57-59](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs_sqrtm.py#L57-L59) —— `R = np.diag(np.sqrt(T_diag))`（对角元开方）；`trsyl = get_lapack_funcs('trsyl', (R,), ilp64="preferred")`（按 dtype 选 `strsyl`/`dtrsyl`/`ctrsyl`/`ztrsyl`）。

块间调用 `trsyl`，并把 `scale` 乘回去：

- [_matfuncs_sqrtm.py:100-103](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs_sqrtm.py#L100-L103) —— `x, scale, info = trsyl(Rii, Rjj, S)`，再 `R[...] = x * scale`。注意 `S` 已经扣除了中间块的贡献（见 4.3）。

C 版的等价调用（实双精度）：

- [src/_matfuncs_sqrtm.c:886](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/src/_matfuncs_sqrtm.c#L886) —— `BLAS_FUNC(dtrsyl)("N", "N", &int1, &halfn, &otherhalfn, T11, &intbign, T22, &intbign, T12, &intbign, &scale, &info)`；`int1=1` 即 `isgn=+1`。

#### 4.2.4 代码实践

1. **目标**：用 NumPy 手写 3×3 上三角矩阵的逐元素平方根递推，验证公式与 `trsyl` 一致。
2. **步骤**：

   ```python
   import numpy as np
   from scipy.linalg import get_lapack_funcs
   T = np.array([[4., 3., 2.],
                 [0., 9., 1.],
                 [0., 0., 16.]])
   # 手写递推
   R = np.zeros_like(T)
   R[0,0], R[1,1], R[2,2] = np.sqrt(T[0,0]), np.sqrt(T[1,1]), np.sqrt(T[2,2])
   R[0,1] = (T[0,1] - 0) / (R[0,0] + R[1,1])
   R[1,2] = (T[1,2] - 0) / (R[1,1] + R[2,2])
   R[0,2] = (T[0,2] - R[0,1]*R[1,2]) / (R[0,0] + R[2,2])
   # 对照 LAPACK trsyl 的块解（把 T 看成 1+2 分块）
   trsyl = get_lapack_funcs('trsyl', (T,))
   print(R @ R - T)            # 接近 0
   ```

3. **观察**：`R @ R - T` 各元应在 \(10^{-15}\) 量级。
4. **预期结果**：手写公式与 `sqrtm`（C 后端）对该三角阵给出一致结果（待本地验证浮点尾差）。

#### 4.2.5 小练习与答案

**Q1**：递推公式分母 \( R_{ii}+R_{jj}=0 \) 但分子也为 0 时怎么办？
**答**：此时该元不构成约束，可取 0（代码里正是这么处理的，见 4.3 的 `denom==0 and num==0` 分支）。

**Q2**：为什么 `trsyl` 的返回值要乘 `scale`？
**答**：求解器为避免中间溢出可能整体缩放解，返回的 `x` 是缩放后的、`scale` 是缩放因子，真解是 `x * scale`。

### 4.3 迭代分块实现 `_sqrtm_triu` 与 Cython `within_block_loop`

#### 4.3.1 概念说明

逐元素递推对大矩阵是 \( O(n^3) \) 但**纯标量循环**，访存差、无法复用 BLAS3。分块 Schur 算法（Deadman–Higham–Ralha 2013）把三角矩阵划成若干大小约 `blocksize` 的方块，分两阶段处理：

- **块内（within-block）**：对每个对角块自己内部的严格上三角部分，用 4.2 的逐元素递推公式——但放在 Cython 内核里跑，去掉 Python 开销。这部分对应 `within_block_loop`。
- **块间（between-block）**：对上三角中所有非对角块矩形 \( (i<j) \)，用 Sylvester 方程求解——这部分调用 LAPACK `trsyl`，因为块尺寸足够大、能享受分治算法的效率。

`_sqrtm_triu` 还做了一件关键优化：对于不相邻的块 \( i<j \)，\( C \) 不是直接取 \( T_{ij} \)，而要**扣除已经算好的中间块贡献** \( \sum R_{i,\cdot} R_{\cdot,j} \)。

> **现状澄清**：这套 `_sqrtm_triu` + `within_block_loop` 实现如今是 `logm` 与 `fractional_matrix_power` 的「开方积木」（见 [_matfuncs_inv_ssq.py:9](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs_inv_ssq.py#L9) 的导入与第 376/403/415 行的调用），而**不是** `sqrtm` 本体的路径（`sqrtm` 走 4.4 的 C 递归版）。但它是理解分块算法最直观的入口。

#### 4.3.2 核心流程

```
_sqrtm_triu(T, blocksize=64):
  1. 对角元开方：R = diag(sqrt(diag(T)))
  2. 决定分块：nblocks = max(n // blocksize, 1)，块大小尽量均匀
     → 得到 start_stop_pairs（每个块的 [start, stop) 区间）
  3. 块内：within_block_loop(R, T, start_stop_pairs, nblocks)   # Cython
  4. 块间：for j in blocks: for i = j-1 ... 0:
            S = T[i,j]
            if 非相邻: S -= R[i, 中间] @ R[中间, j]   # 扣除中间块
            R[i,j] = trsyl(Rii, Rjj, S) * scale        # LAPACK
  5. 返回 R
```

一个细节：块划分用 `divmod` 让块大小只取两种相邻整数（`bsmall` 与 `blarge=bsmall+1`），保证恰好铺满 \( n \)。

#### 4.3.3 源码精读

`_sqrtm_triu` 的分块策略与块划分：

- [_matfuncs_sqrtm.py:61-79](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs_sqrtm.py#L61-L79) —— `nblocks = max(n // blocksize, 1)`；用 `divmod` 得到 `bsmall, nlarge`，`blarge = bsmall + 1`，循环拼出每个块的 `(start, stop)` 区间。

块内交给 Cython 内核，并把 `SqrtmError` 透传出来：

- [_matfuncs_sqrtm.py:82-85](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs_sqrtm.py#L82-L85) —— `within_block_loop(R, T, start_stop_pairs, nblocks)`；捕获 `RuntimeError` 转 `SqrtmError`。

块间扣除中间块 + Sylvester 求解：

- [_matfuncs_sqrtm.py:88-103](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs_sqrtm.py#L88-L103) —— 双层循环 `for j ... for i in range(j-1, -1, -1)`；`j-i>1` 时 `S = S - R[i, istop:jstart].dot(R[istop:jstart, j])`（扣除中间块），再 `trsyl(Rii, Rjj, S)`。

Cython 内核 `within_block_loop` 把 4.2 的逐元素递推直接翻译成带 typed memoryview 的 C 循环，**完全照搬公式**：

- [_matfuncs_sqrtm_triu.pyx:12-33](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs_sqrtm_triu.pyx#L12-L33) —— 用融合类型 `floating ∈ {float64_t, complex128_t}`，三重循环（块 → 列 \(j\) → 行 \(i\) 从 \(j-1\) 向上）。核心三行 `denom = R[i,i]+R[j,j]`、`num = T[i,j]-s`（`s` 即 \( \sum_{k} R_{ik}R_{kj} \)）、`R[i,j] = (T[i,j]-s)/denom`，与公式逐字对应；`denom==0 and num!=0` 抛 `SqrtmError`。

  关键三行（[_matfuncs_sqrtm_triu.pyx:25-32](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs_sqrtm_triu.pyx#L25-L32)）：

  ```cython
  denom = R[i, i] + R[j, j]
  num = T[i, j] - s
  if denom != 0:
      R[i, j] = (T[i, j] - s) / denom
  elif denom == 0 and num == 0:
      R[i, j] = 0
  else:
      raise SqrtmError('failed to find the matrix square root')
  ```

还有一个**纯 Python 兜底**版本（逻辑完全相同，仅用于在没有 Cython 编译产物时回退，且附带了 Pythran 导出注释）：

- [_matfuncs_sqrtm_triu.py:6-24](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs_sqrtm_triu.py#L6-L24) —— 与 `.pyx` 同名的纯 Python `within_block_loop`，公式一致。

构建侧：Cython 扩展 `_matfuncs_sqrtm_triu` 把 `.pyx` 转成 `.c` 再编译：

- [meson.build:208-216](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/meson.build#L208-L216) —— `py3.extension_module('_matfuncs_sqrtm_triu', linalg_init_cython_gen.process('_matfuncs_sqrtm_triu.pyx'), ...)`。

#### 4.3.4 代码实践

1. **目标**：直接调用内部函数 `_sqrtm_triu`，对一个上三角矩阵开方，验证与 `sqrtm` 一致。
2. **步骤**：

   ```python
   import numpy as np
   from scipy.linalg._matfuncs_sqrtm import _sqrtm_triu
   T = np.array([[4., 12., -5.],
                 [0.,  9.,  6.],
                 [0.,  0., 16.]])
   R = _sqrtm_triu(T)
   print(R @ R - T)     # 接近 0；R 为上三角
   print(np.allclose(R, np.triu(R)))   # True
   ```

3. **观察**：返回严格上三角，`R @ R` 还原 \( T \)。
4. **预期结果**：残差在 \(10^{-14}\) 量级；`blocksize=64` 对 3×3 会退化成 1 个块（`nblocks=1`），此时只有块内、没有块间循环。

#### 4.3.5 小练习与答案

**Q1**：对 \( n=200 \)、`blocksize=64`，会分成几个块、各多大？
**答**：`nblocks = 200//64 = 3`，`divmod(200,3) = (66, 2)`，故 2 个大块（67）+ 1 个小块（66），\( 67\times2+66=200 \)。

**Q2**：为什么块间循环要把 `S` 减去 `R[i,中间] @ R[中间,j]`？
**答**：因为 \( R^2 = T \) 的右上块展开后含所有中间块的乘积，块内已算好的部分要扣除，剩下的才是交给 `trsyl` 的右端项。

### 4.4 递归分块实现：C 后端 `sqrtm_recursion_*`（当前 `sqrtm` 的真正路径）

#### 4.4.1 概念说明

C 后端用**递归**而不是迭代分块来实现同一个分块 Schur 算法：把三角矩阵对半切成 \( T_{11}, T_{12}, T_{22} \)，先递归求 \( R_{11}=\sqrt{T_{11}} \)、\( R_{22}=\sqrt{T_{22}} \)，再用一次 `trsyl` 解 \( R_{11}X+XR_{22}=T_{12} \) 得到 \( R_{12} \)。基底情形（\( n=1 \)、\( n=2 \)）直接闭式求解。

C 后端还承担了 `_sqrtm_triu` 不管的**前端工作**：

1. **判定是否已是 Schur 形**（`isschur`/`isschurf`）：若输入本身就是（准）上三角，可跳过昂贵的 Schur 分解（`logm`/`fractional_matrix_power` 已经在 Schur 域上调用 `_sqrtm_triu`，所以它不需要这一步）。
2. **Schur 分解**：否则调 LAPACK `?gees` 得实/复 Schur 形与 Schur 向量 `vs`。
3. **实→复转换**：若实矩阵有负实特征值，需要把实准上三角（含 2×2 块）转成复上三角才能开主平方根——代码用「斑马模式」（每个实数后插一个 0 变复数）+ 对 2×2 块做相似变换拍平。
4. **复原**：开方后用两次 `gemm` 把 \( R = Z R_T Z^H \) 算回来。

#### 4.4.2 核心流程

以实双精度 `matrix_squareroot_d` 为例（每个批处理 slice）：

```
matrix_squareroot_d(slice):
  1. 拷贝 slice 到 Fortran 列主序（swap_cf_d）
  2. isschur(data, n)?
       否 → dgees 做 Schur 分解，拿 T 与 vs
       是 → 直接读对角元当特征值（含 2×2 块的 dlanv2）
  3. 扫特征值：负实数 → isComplex=1；零 → isSingular=1
  4. 若 isComplex:
       斑马展开 + 拍平 2×2 块 → 复上三角
       sqrtm_recursion_z(complex_data)        # 复递归
       两次 zgemm 复原 R = vs @ R_T @ vs^H
     否则:
       sqrtm_recursion_d(data)                # 实递归
       两次 dgemm 复原 R = vs @ R_T @ vs^T
  5. 拷回 C 序输出
```

`sqrtm_recursion_d`（递归核心）：

```
sqrtm_recursion_d(T, bign, n):
  n==1: T[0]=sqrt(T[0]); return
  n==2: 闭式处理（三角或复特征值情形）; return
  else:
    halfn = n/2          # 注意：若切点压在 2×2 块上则 halfn++ 不切坏块
    递归 sqrtm_recursion_d(T11, bign, halfn)
    递归 sqrtm_recursion_d(T22, bign, otherhalfn)
    dtrsyl(T11, T22, T12) → 解 R11·T12 + T12·R22 = 原 T12（原地）
    若 scale!=1：把 T12 块整体乘 scale
    info 沿递归栈向上传播
```

#### 4.4.3 源码精读

Schur 判定与（必要时）Schur 分解：

- [src/_matfuncs_sqrtm.c:366-376](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/src/_matfuncs_sqrtm.c#L366-L376) —— `isschur(data, n)` 为假时调 `BLAS_FUNC(dgees)(...)` 求实 Schur 形与 Schur 向量 `vs`。
- [src/_common_array_utils.h:335-367](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/src/_common_array_utils.h#L335-L367) —— `isschurf`：判定矩阵是否为（准）上三角，且 2×2 块形如 \( \begin{pmatrix}a&b\\c&a\end{pmatrix}, bc<0 \)（即确实承载复共轭特征值）。

负/零特征值检测（决定 isComplex / isSingular）：

- [src/_matfuncs_sqrtm.c:399-405](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/src/_matfuncs_sqrtm.c#L399-L405) —— 遍历 `wr/wi`：实特征值（`wi==0`）若 `wr<0` 置 `isComplex=1`；若 `wr==0` 置 `*isSingular=1`。

按 isComplex 分派到复/实递归：

- [src/_matfuncs_sqrtm.c:407-482](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/src/_matfuncs_sqrtm.c#L407-L482) —— `isComplex` 分支先「斑马展开 + 拍平 2×2 块」（行 409–464）再 `sqrtm_recursion_z`（[行 465](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/src/_matfuncs_sqrtm.c#L465)）；否则 `sqrtm_recursion_d`（[行 475](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/src/_matfuncs_sqrtm.c#L475)）。

递归核心 `sqrtm_recursion_d` 的基底情形与递归 + `dtrsyl`：

- [src/_matfuncs_sqrtm.c:818-906](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/src/_matfuncs_sqrtm.c#L818-L906) —— `n==1`（行 824–828）、`n==2`（行 829–859）闭式处理；否则切半（行 862–865，含「不切坏 2×2 块」的 `halfn++` 修正）、两次递归（行 868–870）、`dtrsyl` 解 Sylvester（[行 886](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/src/_matfuncs_sqrtm.c#L886)）并把 `scale` 乘回（行 887–897）。

构建侧：`_internal_matfuncs` 把 `_matfuncsmodule.c`、`src/_matfuncs_expm.c`、`src/_matfuncs_sqrtm.c` 链成一个扩展：

- [meson.build:258-268](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/meson.build#L258-L268) —— `py3.extension_module('_internal_matfuncs', ['_matfuncsmodule.c', 'src/_matfuncs_expm.c', 'src/_matfuncs_sqrtm.c'], ...)`。

#### 4.4.4 代码实践

1. **目标**：源码阅读型——确认递归基底与 Sylvester 调用，并对照 `info` 的传播。
2. **步骤**：打开 [src/_matfuncs_sqrtm.c:818](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/src/_matfuncs_sqrtm.c#L818) 的 `sqrtm_recursion_d`。回答：(a) `n==2` 且子对角 `c==0`（即三角 2×2 块）时，非对角元怎么算？(b) 递归返回后 `i1 || i2 || info` 的作用是什么？
3. **参考答案**：
   - (a) `T[bign] = T[bign] / (sqrt(a) + sqrt(d))`（行 841），即 \( R_{21} = T_{21}/(R_{11}+R_{22}) \)——正是 4.2 的逐元素公式在 2×2 上的特例。
   - (b) 它把「子问题或本层 Sylvester 求解是否失败」沿递归栈向上传，任一为真则返回 1，最终被 `matrix_squareroot_*` 捕获并置 `isIllconditioned=1`（[行 484](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/src/_matfuncs_sqrtm.c#L484)），再由 Python 层翻译成 `LinAlgWarning`。

#### 4.4.5 小练习与答案

**Q1**：递归切半时为什么要有 `if (T[(halfn-1)*bign + halfn] != 0.0) halfn++;`（[行 864](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/src/_matfuncs_sqrtm.c#L864)）？
**答**：实 Schur 形允许 2×2 块出现在对角线上；若切点正好压在一个 2×2 块中间，会把它的两行/列拆到不同子问题，破坏「子矩阵仍是 Schur 形」的前提。检测到切点处非零即说明压住了 2×2 块，把 `halfn` 加 1 让切点移到块外。

**Q2**：复矩阵版本 `sqrtm_recursion_c/z` 为什么没有 `halfn++` 这种保护？
**答**：复 Schur 形是**严格**上三角，对角线上没有 2×2 块，切半永远不会劈坏块。

## 5. 综合实践

把本讲知识串起来：对三类矩阵（普通正特征值、负特征值、奇异）调用 `sqrtm`，逐项验证。

```python
import numpy as np
import warnings
from scipy.linalg import sqrtm

# (1) 普通 2×2，验证 R @ R ≈ A
A = np.array([[1.0, 3.0], [1.0, 4.0]])
R = sqrtm(A)
print("residual:", np.abs(R @ R - A).max())     # ~1e-15
print("dtype   :", R.dtype)                      # float64

# (2) 负特征值矩阵 → 结果应为复数
B = np.diag([-4.0, -9.0])                        # 特征值 -4,-9 均为负实数
with warnings.catch_warnings():
    warnings.simplefilter("ignore")
    S = sqrtm(B)
print("complex dtype:", np.iscomplexobj(S))      # True
print("check:", np.abs(S @ S - B).max())         # ~1e-15，主平方根为纯虚对角
#   sqrtm(diag(-4,-9)) ≈ diag(2j, 3j)

# (3) 奇异矩阵 → 触发 LinAlgWarning
C = np.array([[1.0, 1.0], [1.0, 1.0]])           # 秩 1，有 0 特征值
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    Q = sqrtm(C)
    print("warnings:", [str(x.message) for x in w])   # 含 "Matrix is singular..."
```

**需要观察与思考**：

1. 第 (1) 组：`R @ R - A` 应极小，`R.dtype` 仍为实数——因为特征值都为正。
2. 第 (2) 组：`sqrtm` 把返回类型**自动升级为复数**，且 \( \sqrt{-4}=2j \)、\( \sqrt{-9}=3j \)，对应 4.4 中 `isComplex` 分支的「实→复转换 + `sqrtm_recursion_z`」。
3. 第 (3) 组：秩亏损矩阵含 0 特征值，C 后端置 `isSingular=1`，Python 层据此发 `LinAlgWarning`（对应 4.1 的 [_matfuncs.py:425-432](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_matfuncs.py#L425-L432)）。该矩阵其实**仍有**平方根，但主平方根可能病态，故只告警不报错。

> 关于第 (3) 组是否真能算出可用结果、数值精度如何，建议**待本地验证**——奇异/接近奇异矩阵的平方根本身就高度病态。

## 6. 本讲小结

- **定义**：矩阵平方根 \( R \) 满足 \( RR=A \)，是矩阵函数而非逐元素开方；主平方根在「无负实轴特征值」时唯一，实矩阵有负特征值时结果为复数。
- **算法骨架**：先 Schur 分解 \( A=ZTZ^H \)，求三角因子平方根 \( R_T \)，再 \( \sqrt{A}=ZR_TZ^H \)。三角平方根的逐元素递推 \( R_{ij}=(T_{ij}-\sum R_{ik}R_{kj})/(R_{ii}+R_{jj}) \) 与分块 Sylvester 方程 \( R_{11}X+XR_{22}=T_{12} \) 是同一件事的两个粒度。
- **LAPACK `trsyl`**：求解 Sylvester 方程的核心例程，返回 `(x, scale, info)`，真解要乘回 `scale`；`info>0` 表示接近无解（对应矩阵接近没有平方根）。
- **两套实现**：Cython 版 `_sqrtm_triu`+`within_block_loop`（迭代分块，`blocksize=64`）现为 `logm`/`fractional_matrix_power` 复用；纯 C 版 `sqrtm_recursion_*`（递归分块）是**当前 `sqrtm` 实际路径**，且额外承担 Schur 前端与实→复转换。
- **错误处理**：`sqrtm` 用 `LinAlgWarning` 报告奇异/病态、用 `LinAlgError` 报告内部错误；`info` 沿 C 递归栈向上传播。
- **构建**：`_internal_matfuncs`（C 扩展，链 `_matfuncsmodule.c`+`src/_matfuncs_sqrtm.c`）与 `_matfuncs_sqrtm_triu`（Cython 扩展）分别由 [meson.build:258-268](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/meson.build#L258-L268) 与 [:208-216](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/meson.build#L208-L216) 构建。

## 7. 下一步学习建议

- **横向打通矩阵函数**：本讲的 `_sqrtm_triu` 正是 u5-l2（`logm`/`fractional_matrix_power`）逆平方法反复调用的「开方积木」；现在回头读 `_inverse_squaring_helper` 会豁然开朗。
- **深入 Schur 前端**：C 后端的 `?gees`、`isschur`、实→复「斑马展开」与 2×2 块拍平，本质是 u3-l5（Schur/rsf2csf）在 C 层的再实现，可对照阅读。
- **三角/双曲矩阵函数**（u5-l4）：`cosm`/`sinm` 等经 `expm` 实现，与本讲的 Schur 路线不同，读完可对比「Schur 派 vs 级数/Padé 派」两类矩阵函数算法。
- **底层接口**（u7）：`get_lapack_funcs('trsyl', ...)`、`get_lapack_funcs('gees', ...)` 的 s/d/c/z 分发机制，是把本讲函数连到 LAPACK 的最后一公里。
