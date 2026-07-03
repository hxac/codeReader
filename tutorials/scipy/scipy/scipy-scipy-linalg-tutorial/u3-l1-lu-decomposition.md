# LU 分解与 lu_solve

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 LU 分解 \(A = PLU\) 中三个因子各自的数学含义，以及「部分主元（partial pivoting）」为什么必不可少。
- 区分 `scipy.linalg` 里三个相关函数的分工：`lu_factor`（紧凑分解，供 `lu_solve` 复用）、`lu_solve`（用已有分解快速求解）、`lu`（返回独立的 P、L、U，面向用户）。
- 读懂 `_decomp_lu.py` 里 Python 薄层的「校验 → 委派 → 汇报」三段式，并知道 `lu_factor` 直连 f2py 包装的 LAPACK `getrf`，而 `lu` 走的是 C++ 批量后端 `_linalg_lu`。
- 理解 C++ 后端 `_linalg_lu_det.hh` 如何把 LAPACK 的 1 基主元序列转成 0 基置换、如何抽出 L/U，以及如何顺便算出行列式。
- 用 `lu_factor + lu_solve` 的「两步法」对同一矩阵求解多个右端，并与直接 `solve` 对比效率。

## 2. 前置知识

本讲承接 u2-l2（`solve` 与 `assume_a` 调度）和 u2-l3（`inv`/`det`/`lstsq`）。在进入源码前，先用三段话补齐 LU 相关的数学直觉。

**为什么要分解矩阵？** 直接解 \(Ax=b\) 的高斯消元，本质是先把 \(A\)「拆」成一个下三角 \(L\) 和一个上三角 \(U\)，再分两次三角求解。把「拆」的结果存下来，就能对不同的 \(b\) 反复求解而不必每次都重新消元——这正是 `lu_factor` + `lu_solve` 的价值。

**什么是部分主元？** 高斯消元时，若第 \(k\) 步的对角元（主元）很小或为零，用它做除数会放大误差甚至崩溃。解决办法是在消元前，把第 \(k\) 列里第 \(k\) 行及以下中绝对值最大的那一行换到第 \(k\) 行。这些行交换记录成一个置换矩阵 \(P\)，于是分解形如：

\[
PA = LU \quad\Longleftrightarrow\quad A = P^{-1}LU = PLU
\]

（注意 \(P\) 是置换矩阵，\(P^{-1}=P^{T}\)，文档里两种写法都对，取决于把 \(P\) 放哪边。）

**部分主元的数学保证：** 有了行交换，\(L\) 的所有元素都满足 \(|L_{ij}|\le 1\)，算法对绝大多数矩阵数值稳定；代价只是要额外存一个主元序列 `piv`。这也是 LAPACK `getrf`（`get` + `rf` = factorize）和 `getrs`（`get` + `rs` = solve）这对例程的分工：前者算 \(LU\) 与主元，后者用结果解方程。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲关注 |
| --- | --- | --- |
| [`_decomp_lu.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_lu.py) | Python 薄层，定义 `lu` / `lu_factor` / `lu_solve` | 全文，是本讲主角 |
| [`_basic.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_basic.py) | `solve` 实现 | 实践任务里做对比基准 |
| [`src/_linalg_lu_det.hh`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/src/_linalg_lu_det.hh) | C++ 批量后端，`lu` 与 `det` 共用 | `getrf` 分发、主元转置换、LU 抽取、行列式 |
| [`src/_batched_linalg_module.cc`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/src/_batched_linalg_module.cc) | C++ 扩展的 Python 入口 | `_linalg_lu` 函数与类型分发 |
| [`lapack.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/lapack.py) | LAPACK 例程名注册表 | `getrf` / `getrs` 的 s/d/c/z 命名 |
| [`tests/test_decomp_lu.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/tests/test_decomp_lu.py) | LU 测试套件 | 验证分解正确性的断言 |

一个关键架构事实先记在心里：**`lu_factor` 与 `lu` 虽然都算 LU 分解，底层走的却是两条不同的路。** `lu_factor` 通过 `get_lapack_funcs('getrf')` 直接调用 f2py 包装的 LAPACK，返回紧凑的 `(lu, piv)`；`lu` 则调用 C++ 扩展 `_batched_linalg._lu`（别名 `_linalg_lu`），由 C++ 内部再调 `getrf` 并把 L、U、P 拆开返回。理解这一点，本讲后面所有源码就串起来了。

## 4. 核心概念与源码讲解

### 4.1 lu_factor：紧凑型 LU 分解（直连 LAPACK getrf）

#### 4.1.1 概念说明

`lu_factor` 解决的问题是：「我只想拿到一份紧凑的分解结果，稍后用它快速解很多次方程，不需要漂亮分开的 P、L、U 矩阵。」

它的返回值是一个二元组 `(lu, piv)`：

- `lu` 是一个和输入同形状的数组，**上三角部分（含对角线）存 U，严格下三角部分存 L 的非对角元**——L 的单位对角线不存（因为恒为 1，存了浪费）。这种「L 和 U 叠在一个数组里」的紧凑存储是 LAPACK `getrf` 的原生输出格式。
- `piv` 是一个长度为 `min(M, N)` 的主元索引数组，记录了每次消元时把哪一行换到了第 `i` 行。**LAPACK 返回的是 1 基索引，`lu_factor` 会把它转成 0 基**，方便 Python/NumPy 直接用。

这种紧凑格式的好处是省内存、与 `getrs`（求解例程）的输入格式完全对齐；缺点是不直观，所以面向「只求解、不展示因子」的场景。

#### 4.1.2 核心流程

`lu_factor` 的 Python 层只做四件事，真正的数值计算全在 LAPACK：

```
lu_factor(a):
  1. check_finite? → asarray_chkfinite / asarray      # 拦截 NaN/Inf
  2. 空数组?       → 直接返回空 lu + arange piv         # 快速短路
  3. overwrite_a = overwrite_a or _datacopied(a1, a)   # 已拷贝就可原地写
  4. getrf = get_lapack_funcs(('getrf',), (a1,))       # 按 dtype 选 s/d/c/z
     lu, piv, info = getrf(a1, overwrite_a=...)
  5. 解读 info：
       info < 0 → 抛 ValueError（参数非法）
       info > 0 → 发 LinAlgWarning（第 info 个对角元为 0，奇异）
       info = 0 → 正常
```

注意第 5 步的差别：`info > 0` 表示 \(U\) 的第 `info` 个对角元恰好为 0，矩阵（数值上）奇异，分解产物仍然返回，只是发一个 `LinAlgWarning`；`info < 0` 则是参数本身非法，直接抛错。这与 u2-l3 里 `inv`（奇异抛 `LinAlgError`）的策略不同，因为 LU 分解本身在奇异时也能给出合法因子，只是后续 `lu_solve` 会不稳定。

#### 4.1.3 源码精读

先看模块顶部的导入与导出（注意第 13 行从 C++ 扩展 `_batched_linalg` 引入 `_lu`，这是 `lu` 函数的后端）：

[_decomp_lu.py:8-16](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_lu.py#L8-L16) — 导入 `_apply_over_batch`（批处理维度支持）、`_datacopied`（拷贝检测）、`get_lapack_funcs`（按 dtype 选例程），并从 C++ 扩展 `_batched_linalg` 引入 `_lu`。

接着是 `lu_factor` 主体。装饰器 `@_apply_over_batch(('a', 2))` 表示输入 `a` 的核心形状是 2D，前面允许挂任意多个「批处理维度」（详见 u8-l1）；对单个矩阵它等价于直接调用。

[_decomp_lu.py:106-120](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_lu.py#L106-L120) — 先做 `check_finite` 与空数组短路，再算出真正的 `overwrite_a`，然后用 `get_lapack_funcs` 选出 `getrf` 并调用。关键三行是：

```python
getrf, = get_lapack_funcs(('getrf',), (a1,))
lu, piv, info = getrf(a1, overwrite_a=overwrite_a)
```

`get_lapack_funcs` 会根据 `a1` 的 dtype 在 `s/d/c/z` 四个前缀里选一个（详见 u7-l1），例如 float64 选 `dgetrf`、complex128 选 `zgetrf`。这两个例程名注册在：

[lapack.py:229-252](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/lapack.py#L229-L252) — `getrf`（分解，229-232）与 `getrs`（求解，249-252）的 s/d/c/z 四类型注册。

最后是 `info` 的解读：

[_decomp_lu.py:121-131](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_lu.py#L121-L131) — `info < 0` 抛 `ValueError`；`info > 0` 用 `LinAlgWarning` 报告第 `info` 个对角元为零（奇异）。

关于 `piv` 如何从 LAPACK 的 1 基转成 0 基：f2py 在生成 `*getrf` 包装时已经做了 `ipiv - 1`，所以 Python 层拿到的 `piv` 已经是 0 基。文档字符串里专门强调了这一点（[_decomp_lu.py:64-65](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_lu.py#L64-L65)），并给出了把 `piv` 还原成置换的示例代码。

#### 4.1.4 代码实践

**实践目标：** 亲手验证紧凑分解的正确性，并观察奇异矩阵的告警。

**操作步骤：**

```python
import warnings
import numpy as np
from scipy.linalg import lu_factor

A = np.array([[2., 5, 8, 7],
              [5., 2, 2, 8],
              [7., 5, 6, 6],
              [5., 4, 4, 8]])

lu, piv = lu_factor(A)

# 从紧凑数组里手工拆出 L 和 U
L = np.tril(lu, k=-1) + np.eye(4)   # 严格下三角 + 单位对角
U = np.triu(lu)                      # 上三角（含对角）

# piv 是 0 基主元序列，把它还原成「第 i 行最终来自原始哪一行」的置换 p_inv
p_inv = np.arange(4)
for i in range(4):
    p_inv[i], p_inv[piv[i]] = p_inv[piv[i]], p_inv[i]

print("L @ U 应等于 A 行重排后：", np.allclose(L @ U, A[p_inv]))
print("piv =", piv)

# 奇异矩阵：第二行是第一行的 2 倍
S = np.array([[1., 2, 3], [2., 4, 6], [1., 1, 1]])
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    lu_s, piv_s = lu_factor(S)
    print("奇异矩阵触发的告警类别：", w[0].category.__name__ if w else "无")
```

**需要观察的现象：** 第一个断言应输出 `True`，说明紧凑数组确实编码了 L、U；奇异矩阵那一步应触发 `LinAlgWarning`（而不是抛异常）。

**预期结果：** `lu` 的对角线上会出现一个 0（对应 `info > 0`），告警类别为 `LinAlgWarning`。

**说明：** 上面的 `p_inv` 还原逻辑与源码文档字符串 [_decomp_lu.py:76-86](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_lu.py#L76-L86) 给出的 `pivot_to_permutation` 完全一致，可作为「主元序列 ⟷ 置换矩阵」互转的参考实现。

#### 4.1.5 小练习与答案

**练习 1：** `lu_factor` 对一个 `4×6` 的「胖」矩阵返回的 `lu` 和 `piv` 形状分别是什么？`L` 和 `U` 又各是什么形状？

**答案：** `lu` 形状为 `(4, 6)`（与输入同形），`piv` 形状为 `(4,)`（长度 `min(4,6)=4`）。`L` 是 `4×4` 的单位下三角，`U` 是 `4×6` 的上梯形。

**练习 2：** 为什么 `info > 0` 时只发告警而不抛异常？

**答案：** 因为 LU 分解在矩阵奇异时仍能产生合法的 L、U 因子（只是某个 \(U_{ii}=0\)），后续是否能继续用取决于场景；`lu_factor` 把「是否可用」的决定权交给调用方，用告警提示风险。

### 4.2 lu_solve：复用分解结果求解（getrs）

#### 4.2.1 概念说明

有了 `lu_factor` 的产物 `(lu, piv)`，解 \(Ax=b\) 就不必再做 O(\(n^3\)) 的消元，只需两次 O(\(n^2\)) 的三角求解：先解 \(Ly=Pb\)，再解 \(Ux=y\)。LAPACK 的 `getrs` 把这两步打包成一次调用。`lu_solve` 就是 `getrs` 的薄包装。

它的额外能力是 `trans` 参数，能直接求解转置或共轭转置系统，而不必真的去转置 \(A\) 再重新分解：

| `trans` | 求解的系统 |
| --- | --- |
| 0 | \(A x = b\) |
| 1 | \(A^{T} x = b\) |
| 2 | \(A^{H} x = b\)（共轭转置） |

典型用法是「一次分解，多次求解」：对同一个 \(A\) 有 \(k\) 个不同右端，先 `lu_factor` 一次，再 `lu_solve` \(k\) 次，比 `solve` \(k\) 次省下 \(k-1\) 次消元。

#### 4.2.2 核心流程

```
lu_solve((lu, piv), b, trans=0):
  → _lu_solve(lu, piv, b, trans, overwrite_b, check_finite):
      1. check_finite on b
      2. _deprecate_dtypes("lu_solve", lu, b)   # 拦截即将废弃的 dtype
      3. overwrite_b = overwrite_b or _datacopied(b1, b)
      4. 校验 lu 与 b 行数一致
      5. 空数组短路
      6. getrs = get_lapack_funcs(('getrs',), (lu, b1))
         x, info = getrs(lu, piv, b1, trans=trans, ...)
      7. info == 0 → 返回 x；否则 ValueError
```

注意 `lu_solve` 本身不挂 `@_apply_over_batch`，真正的批处理逻辑在它委托的 `_lu_solve` 上（[_decomp_lu.py:193](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_lu.py#L193)）。

#### 4.2.3 源码精读

外层 `lu_solve` 极薄，只拆包并转发：

[_decomp_lu.py:188-190](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_lu.py#L188-L190) — 从 `lu_and_piv` 解包出 `(lu, piv)`，调用内部 `_lu_solve`。

真正干活的是 `_lu_solve`：

[_decomp_lu.py:193-216](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_lu.py#L193-L216) — 完整的求解薄层。三个要点：

```python
getrs, = get_lapack_funcs(('getrs',), (lu, b1))
x, info = getrs(lu, piv, b1, trans=trans, overwrite_b=overwrite_b)
if info == 0:
    return x
raise ValueError(f'illegal value in {-info}th argument of internal gesv|posv')
```

注意两点：其一，`getrs` 同时接收 `lu` 和 `b1` 来决定类型前缀，这样即使 `b` 的 dtype 与 `lu` 不同也能正确匹配；其二，`getrs` 的 `info` 正常时为 0，非零只可能是「参数非法」（不会是「奇异」，奇异性在 `getrf` 阶段就已暴露），所以这里只抛 `ValueError`。

错误信息里写的是 `gesv|posv`，这是历史遗留：`getrs` 在不同 LAPACK 实现里可能与 `gesv`/`posv` 系列共享底层，提示文本沿用了通用说法。

#### 4.2.4 代码实践

**实践目标：** 体验「一次分解、多次求解」的收益，并与直接 `solve` 对比正确性和耗时。

**操作步骤：**

```python
import time
import numpy as np
from scipy.linalg import lu_factor, lu_solve, solve

rng = np.random.default_rng(0)
n = 600
A = rng.standard_normal((n, n))
Bs = [rng.standard_normal(n) for _ in range(20)]   # 20 个不同右端

# 方式一：每次都 solve（每次都重新做 LU 消元）
t0 = time.perf_counter()
xs_solve = [solve(A, b) for b in Bs]
t_solve = time.perf_counter() - t0

# 方式二：先 lu_factor 一次，再 lu_solve 多次
t0 = time.perf_counter()
lu, piv = lu_factor(A)
xs_lu = [lu_solve((lu, piv), b) for b in Bs]
t_lu = time.perf_counter() - t0

print(f"solve   总耗时: {t_solve*1e3:8.2f} ms")
print(f"lu 两步 总耗时: {t_lu*1e3:8.2f} ms")
print("两种方法结果最大误差:",
      max(np.max(np.abs(a - b)) for a, b in zip(xs_solve, xs_lu)))
```

**需要观察的现象：** 两种方法结果几乎完全一致（误差应在 \(10^{-10}\) 量级，受浮点精度影响）；当右端数量较多时，「两步法」总耗时应明显低于「每次 solve」。

**预期结果：** 在中等规模（如 600×600）且右端数 20 时，两步法通常更快；若只解一两个右端，差距不大甚至 `solve` 略快（因为省去了 Python 层往返开销）。「待本地验证」具体加速比，因为它取决于矩阵规模、右端数量与 BLAS 实现。

#### 4.2.5 小练习与答案

**练习 1：** 若把一个 `4×4` 的 `lu` 配上一个长度为 3 的 `b` 传给 `lu_solve`，会发生什么？

**答案：** `_lu_solve` 在 [_decomp_lu.py:204-205](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_lu.py#L204-L205) 检查 `lu.shape[0] != b1.shape[0]`，会抛 `ValueError`，提示形状不兼容。

**练习 2：** 如何用 `lu_solve` 求解 \(A^{T}x=b\) 而不重新分解？

**答案：** 复用同一个 `(lu, piv)`，调用 `lu_solve((lu, piv), b, trans=1)`；求解共轭转置系统则用 `trans=2`。

### 4.3 lu：用户友好的 P L U 分解

#### 4.3.1 概念说明

`lu` 面向「我要看清楚 P、L、U 三个因子」的场景，返回独立的矩阵而非紧凑数组。它还提供两个贴心选项：

- `permute_l=True`：直接返回已置换好的 \(PL\)（记作 `PL`）和 `U`，满足 \(A = PL \cdot U\)，省去外部再做矩阵乘法。
- `p_indices=True`：把置换以「行索引向量」形式返回（如 `[1,3,0,2]`）而非稀疏的置换矩阵，省内存。文档明确建议：2D 情况用索引向量（满足 \(A = L[P,:] @ U\)），高维情况用 `permute_l`。

与 `lu_factor` 最大的实现差异是：**`lu` 不走 f2py 的 `getrf`，而是走 C++ 扩展 `_linalg_lu`**（[_decomp_lu.py:13](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_lu.py#L13) 的导入）。C++ 后端内部仍然调用 `getrf`，但额外负责把 L、U 从紧凑数组里抽出来、把主元转成置换，并原生支持批处理（一次分解一叠矩阵）。

#### 4.3.2 核心流程

```
lu(a, permute_l=False, p_indices=False):
  1. asarray（按 check_finite）+ _deprecate_dtypes
  2. ndim < 2 → ValueError
  3. _normalize_lapack_dtype(a1, overwrite_a)   # 统一到 LAPACK 兼容 dtype
  4. 解析形状：*nd, m, n = shape；k = min(m,n)
  5. 空数组 / 标量(1×1) 特殊短路
  6. P, L, U = _linalg_lu(a1, permute_l, overwrite_a)   # ← C++ 后端
  7. 若不需要 p_indices 且不 permute_l：把索引向量 P 展开成置换矩阵
  8. 返回 (L, U) if permute_l else (P, L, U)
```

第 7 步值得注意：C++ 后端返回的 `P` 本身就是行索引向量；只有当用户没要 `p_indices` 时，Python 层才用 one-hot 编码把它展开成 \(M\times M\) 的稠密置换矩阵（这步有内存开销，所以文档建议直接用索引）。

#### 4.3.3 源码精读

形状解析与后端调用：

[_decomp_lu.py:324-350](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_lu.py#L324-L350) — 把末两维拆成 `m, n`，算 `k=min(m,n)`，处理空/标量后，调用 C++ 后端 `_linalg_lu(a1, permute_l, overwrite_a)`。

```python
*nd, m, n = a1.shape
k = min(m, n)
...
P, L, U = _linalg_lu(a1, permute_l, overwrite_a)
```

索引向量 → 置换矩阵的展开（仅在 2D、不要 `p_indices` 时）：

[_decomp_lu.py:354-364](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_lu.py#L354-L364) — 用 `Pa[np.arange(m), P] = 1` 的 one-hot 技巧把行索引向量变成置换矩阵；高维时借助 `np.ix_` 构造索引。

最终返回：

[_decomp_lu.py:366](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_lu.py#L366) — `permute_l` 为真返回 `(L, U)`（此时 L 已是 \(PL\)），否则返回 `(P, L, U)`。

#### 4.3.4 代码实践

**实践目标：** 对比 `permute_l`、`p_indices` 三种返回形式，并验证 \(A=PLU\)。

**操作步骤：**

```python
import numpy as np
from scipy.linalg import lu

A = np.array([[2., 5, 8, 7],
              [5., 2, 2, 8],
              [7., 5, 6, 6],
              [5., 4, 4, 8]])

# 默认：返回稠密置换矩阵 P
P, L, U = lu(A)
print("A ≈ P@L@U :", np.allclose(A, P @ L @ U))

# 用索引向量：A = L[P,:] @ U
p, L2, U2 = lu(A, p_indices=True)
print("p =", p)
print("A ≈ L[p,:]@U :", np.allclose(A, L2[p, :] @ U2))

# permute_l：直接得到 PL
PL, U3 = lu(A, permute_l=True)
print("A ≈ PL@U :", np.allclose(A, PL @ U3))
```

**需要观察的现象：** 三种形式都应输出 `True`；`p_indices=True` 返回的 `p` 是一个 `int32` 向量（如 `[1,3,0,2]`），而非 \(4\times4\) 矩阵。

**预期结果：** 与源码文档示例 [_decomp_lu.py:285-300](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_lu.py#L285-L300) 一致。

#### 4.3.5 小练习与答案

**练习 1：** 对一个 `3×2` 的「高」矩阵做 `lu(A)`，`P`、`L`、`U` 各是什么形状？

**答案：** `P` 是 `3×3`，`L` 是 `3×2`（单位下三角的「梯形」，对角线为 1），`U` 是 `2×2`（上三角）。

**练习 2：** 为什么高维（ndim>2）输入时文档推荐用 `permute_l=True` 而非索引向量？

**答案：** 因为高维下用索引向量重构 \(A=L[P,:]@U\) 需要复杂的多维 fancy indexing（Python 层要用 `np.ix_` 拼装），既慢又易错；`permute_l=True` 让 C++ 后端直接算好 \(PL\)，避免这些索引技巧（见文档 [_decomp_lu.py:275-276](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/_decomp_lu.py#L275-L276)）。

### 4.4 C++ 批量后端 _linalg_lu_det.hh

#### 4.4.1 概念说明

`lu` 函数背后的算力来自 C++ 扩展 `_batched_linalg`，其 LU 与行列式逻辑集中在头文件 `_linalg_lu_det.hh`（命名点明：**lu 与 det 共用同一份 getrf 调用**——行列式就是 LU 分解的「副产品」，\(\det A = (-1)^s\prod U_{ii}\)）。把它单独讲，是因为它揭示了三件 Python 层看不到的事：

1. **类型分发：** C++ 用模板 + `if constexpr` 把一份代码实例化成 `float/double/complex<float>/complex<double>` 四个版本，分别调 `s/d/c/z getrf`。
2. **主元 ⟷ 置换的转换：** LAPACK 返回的是「第 \(i\) 步把第 \(i\) 行和第 `ipiv[i]` 行互换」的交换序列（1 基），C++ 把它转成最终的置换向量。
3. **批处理：** 对 `(..., M, N)` 的 nD 输入，逐片（slice）调用 `getrf`，每片独立分解，错误信息按片收集。

#### 4.4.2 核心流程

单个矩阵的分解（`lu_decompose`）：

```
lu_decompose(f_buf 列主序输入, l_out, u_out, ipiv, perm, m, n, permute_l):
  1. getrf(&m, &n, f_buf, &m, ipiv, &info)   # 原地覆写 f_buf 为紧凑 LU
  2. 从列主序 f_buf 抽出 U 的上三角（含右侧块）→ 行主序 u_out
  3. 从 f_buf 抽出 L 的严格下三角，对角线置 1 → l_out
  4. ipiv_to_perm(ipiv, perm, m, mn)         # 主元序列 → 置换向量
  5. 若 permute_l：permute_rows(l_out, perm, ...)  # 把 P 作用到 L 上
```

主元转置换的关键算法（`ipiv_to_perm`）：从恒等排列 `[0,1,...,m-1]` 出发，**逆序**应用 LAPACK 的交换序列，直接得到逆置换 \(P^{-1}\)（正向应用得到 \(P\)）。数学上：

\[
P = S_{k-1} \cdots S_1 S_0, \qquad P^{-1} = S_0 S_1 \cdots S_{k-1}
\]

其中 \(S_i\) 是第 \(i\) 步的行交换。逆序应用省去了额外的工作数组。

#### 4.4.3 源码精读

类型分发的 `getrf` 模板：

[_linalg_lu_det.hh:37-44](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/src/_linalg_lu_det.hh#L37-L44) — 用 `if constexpr` 按类型 `T` 分发到 `sgetrf/dgetrf/cgetrf/zgetrf`；复数情况把 `std::complex` 指针 `reinterpret_cast` 成 LAPACK 期望的 `npy_complex` 类型（二者 ABI 兼容）。

单矩阵分解与 L/U 抽取：

[_linalg_lu_det.hh:168-202](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/src/_linalg_lu_det.hh#L168-L202) — 调 `getrf` 后，按列主序下标 `f_buf[j*m+i]` 把 U（上三角，含右侧矩形块）和 L（严格下三角 + 单位对角）分别写入行主序输出缓冲；再调 `ipiv_to_perm`，最后按需 `permute_rows`。

主元转置换：

[_linalg_lu_det.hh:107-117](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/src/_linalg_lu_det.hh#L107-L117) — 先令 `perm[i]=i`，再**逆序**遍历 `i`，交换 `perm[i]` 与 `perm[ipiv[i]-1]`（减 1 把 1 基转 0 基）。

批处理分发：

[_linalg_lu_det.hh:229-274](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/src/_linalg_lu_det.hh#L229-L274) — 外层循环遍历每个 2D 切片，按批维 strides 算出该片在输入缓冲里的偏移，必要时 `copy_strided_to_f` 拷成列主序，再调 `lu_decompose`；每片的 `info` 写入 `slice_info` 供 Python 层转换成告警。

行列式（det 的 LU 路径）：

[_linalg_lu_det.hh:295-322](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/src/_linalg_lu_det.hh#L295-L322) — 同样调 `getrf`，然后累乘 \(U\) 的对角线 `f_buf[k*(n+1)]`，并按行交换次数 `swaps` 决定符号 \((-1)^{swaps}\)。关键细节：**单精度（float32/complex64）的累乘提升到 double/complex128 精度**（`acc_type`），避免中间乘积溢出/下溢，这正是 u2-l3 里「det 单精度结果提升为 double 防溢出」的实现出处。

最后看 C++ 扩展如何被 Python 调用：

[_batched_linalg_module.cc:939-948](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/src/_batched_linalg_module.cc#L939-L948) — `_linalg_lu` 是 Python 可见的入口，解析 `(a, permute_l, overwrite_a)` 三个参数。

[_batched_linalg_module.cc:1049-1062](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/src/_batched_linalg_module.cc#L1049-L1062) — 按 NumPy dtype 用 `switch` 把调用分发到 `lu_dispatch<float/double/complex<float>/complex<double>>`。

[_batched_linalg_module.cc:1498](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/src/_batched_linalg_module.cc#L1498) — 方法表里把 `_linalg_lu` 注册为扩展模块的 `_lu` 方法，这就是 `_decomp_lu.py` 第 13 行 `from ._batched_linalg import _lu` 能 import 到的符号。

#### 4.4.4 代码实践

**实践目标：** 验证 C++ 后端的批处理能力（一次分解一叠矩阵），并对比「批量 lu」与「逐个 lu_factor」。

**操作步骤：**

```python
import numpy as np
from scipy.linalg import lu

rng = np.random.default_rng(1)
# 一叠 5 个 4×4 矩阵：批处理维度在前
stack = rng.standard_normal((5, 4, 4))

# lu 原生支持批处理维度，一次调用分解全部
P, L, U = lu(stack)
print("P, L, U 形状:", P.shape, L.shape, U.shape)
print("每片都满足 A≈P@L@U:",
      all(np.allclose(stack[i], P[i] @ L[i] @ U[i]) for i in range(5)))
```

**需要观察的现象：** 输出形状为 `(5,4,4) (5,4,4) (5,4,4)`；每一片都满足分解等式。这说明 C++ 后端的 `lu_dispatch` 循环（[_linalg_lu_det.hh:238-271](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/src/_linalg_lu_det.hh#L238-L271)）确实逐片处理了批维。

**预期结果：** 五个断言全为 `True`。

**说明：** `lu_factor` 也能处理批维（靠 `@_apply_over_batch`），但它的批处理是在 Python 层循环调度 f2py 的 `getrf`；而 `lu` 的批处理直接在 C++ 单次调用内循环，少了 Python 往返开销。具体性能差「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1：** 为什么 `det_from_lu` 在单精度时要提升到 double 累乘？

**答案：** 行列式是 \(n\) 个对角元的连乘，单精度下中间结果极易溢出（> \(3.4\times10^{38}\)）或下溢；提升到 double 累乘、最后再转回 float，能显著拓宽安全范围（[_linalg_lu_det.hh:303-313](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/src/_linalg_lu_det.hh#L303-L313)）。

**练习 2：** `ipiv_to_perm` 为什么用「逆序」应用交换序列？

**答案：** LAPACK 的 `ipiv` 记录的是消元过程中「正向」的行交换，正向应用得到 \(P\)；逆序应用等价于先做最后一个交换的逆、再倒数第二个……直接得到 \(P^{-1}\)，无需额外工作数组（[_linalg_lu_det.hh:109-110](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/src/_linalg_lu_det.hh#L109-L110) 注释）。

## 5. 综合实践

把本讲三个函数与一个真实小任务串起来：**用 LU 分解实现「矩阵求逆」，并与官方 `inv` 对比。**

求逆的本质是解 \(AX=I\)，即对 \(A\) 的 LU 分解一次性求解 \(n\) 个右端（\(I\) 的各列）。步骤如下：

```python
import numpy as np
from scipy.linalg import lu_factor, lu_solve, inv
from numpy.testing import assert_allclose

rng = np.random.default_rng(42)
n = 8
A = rng.standard_normal((n, n))

# 1. 分解一次
lu, piv = lu_factor(A)

# 2. 用 I 的每一列作为右端，逐列求解 → 拼成 A 的逆
I = np.eye(n)
X = np.column_stack([lu_solve((lu, piv), I[:, j]) for j in range(n)])

# 3. 验证：A @ X 应为单位阵
assert_allclose(A @ X, I, atol=1e-10)

# 4. 与官方 inv 对比
print("与 inv 的最大误差:", np.max(np.abs(X - inv(A))))
```

**进阶思考：**

1. 上面逐列求解用了 Python 循环。其实 `lu_solve` 支持右端为 2D（多列同时求解），试着改成 `lu_solve((lu, piv), I)` 一次求出整个逆矩阵，观察是否更快、结果是否一致。
2. 把 `A` 换成一个**列线性相关**的奇异矩阵（如某列是另一列的 2 倍），重跑：`lu_factor` 会发 `LinAlgWarning`，`lu_solve` 求出的「逆」与单位阵偏差巨大——这印证了「奇异矩阵没有逆」。
3. 阅读测试文件 [tests/test_decomp_lu.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/tests/test_decomp_lu.py)，找到 `test_simple_lu_shapes_real_complex`（[_decomp_lu 测试:84-90](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/linalg/tests/test_decomp_lu.py#L84-L90)），看官方如何用 `assert_allclose(a, p @ l @ u)` 跨多种形状与 dtype 验证分解。

## 6. 本讲小结

- LU 分解把 \(A\) 拆成 \(PLU\)（\(P\) 置换、\(L\) 单位下三角、\(U\) 上三角），部分主元保证 \(|L_{ij}|\le1\)、数值稳定。
- `lu_factor` 返回**紧凑的 `(lu, piv)`**，直连 f2py 包装的 LAPACK `getrf`；`info>0` 发 `LinAlgWarning`（奇异），`info<0` 抛 `ValueError`（参数非法）。
- `lu_solve` 是 `getrs` 的薄包装，复用 `(lu, piv)` 做 O(\(n^2\)) 求解，`trans` 参数支持 \(A/A^T/A^H\) 三种系统；「一次分解、多次求解」是其核心价值。
- `lu` 返回**独立的 P、L、U**，但走的是 **C++ 扩展 `_linalg_lu`**（而非 f2py），原生支持批处理维度，并提供 `permute_l`/`p_indices` 两种省内存的置换表示。
- C++ 后端 `_linalg_lu_det.hh` 是 `lu` 与 `det` 的共用底层：模板分发四类型、`ipiv_to_perm` 逆序转换主元、`det_from_lu` 把行列式作为 LU 副产品（单精度提升 double 累乘防溢出）。
- 选型建议：要反复解方程用 `lu_factor`+`lu_solve`；要看因子或批量分解用 `lu`；只要标量行列式用 `det`（内部也是 LU）。

## 7. 下一步学习建议

- **u3-l2 Cholesky 分解**：当 \(A\) 对称正定时，Cholesky 比 LU 快一倍且无主元，是「结构化分解」的下一个台阶，与本讲的 `cho_factor`/`cho_solve` 两步法完全对称。
- **u3-l3 QR 分解**：LU 适合方阵求解，QR 则是「最小二乘与正交化」的基础，二者对比能看清「为什么需要多种分解」。
- **u7-l1 BLAS/LAPACK 接口**：本讲多次出现 `get_lapack_funcs`，想彻底搞懂它如何按 dtype 选 `s/d/c/z` 前缀、如何处理 ILP64，就去读 `blas.py`/`lapack.py`。
- **u8-l2 C++ 批量后端**：想深入 `_linalg_lu_det.hh` 所在的整套 `_batched_linalg` 扩展（错误聚合、`module_methods` 注册、公共工具 `_common_array_utils.hh`），那是专家层的主场。
