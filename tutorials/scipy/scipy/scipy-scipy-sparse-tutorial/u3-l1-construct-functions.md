# 构造函数：_construct.py

## 1. 本讲目标

前几讲我们学会了「用某种格式（COO/CSR/BSR…）的构造器，从一个稠密数组或三元组手动捏出一个稀疏对象」。但现实里，很多稀疏矩阵并不是「先有稠密、再抽稀疏」捏出来的，而是**直接按结构生成**的：单位阵、带状矩阵、随机稀疏矩阵、由若干小块拼成的大矩阵、Kronecker 积……这些「批量制造」的需求，正是 `scipy/sparse/_construct.py` 这份模块存在的意义。

读完本讲你应当能够：

- 说出 `_construct.py` 里**新式 `*_array` 工厂函数**（`eye_array`、`diags_array`、`random_array`、`block_array`）与**旧式 `*_matrix` 工厂函数**（`eye`/`identity`、`diags`/`spdiags`、`random`/`rand`、`bmat`）之间的对应关系与命名规律；
- 用 `diags_array` 从若干条对角线直接造出带状矩阵，并解释它与 `dia_array` 在「是否需要填充」上的差别；
- 读懂 `_block` 的**两条执行路径**——全 CSR/CSC 时走 C++ 快速堆叠，否则退化为 COO 通用拼装，并理解 `hstack`/`vstack`/`bmat`/`block_array` 如何共用这一份核心代码；
- 理解 `kron` 如何在「BSR 路径」与「COO 路径」之间分流，以及 `block_diag` 如何把若干矩阵沿主对角线排开。

本讲承接 [u2-l3 CSR 与 CSC](u2-l3-csr-csc-format.md)：那里讲的是「单个矩阵如何紧凑存储」，本讲讲的是「如何批量、按结构地把矩阵造出来」。一句话主线：**先造 COO/DIA 这类「好造」的格式，最后用 `asformat` 转成你想要的格式。**

## 2. 前置知识

继续前请确认你已掌握（前几讲已建立）：

- **隐式零与 `nnz`**：稀疏只显式存非零元，`nnz` 是「已存储元素数」（含显式零）。（u1-l1）
- **七种格式各自擅长什么**：COO 好造、CSR/CSC 好算、DIA 适合带状、BSR 适合分块、LIL/DOK 适合逐元素增量写入。（u2-l2 ~ u2-l5）
- **`sparray` vs `spmatrix`**：`sparray` 是新式数组接口（允许 1 维、`*` 为逐元素乘），`spmatrix` 是待弃用的矩阵接口（强制 2 维、`*` 为矩阵乘）。（u2-l1）
- **`asformat(format)`**：所有格式共用的「格式转换统一入口」，传 `None` 表示不转换。（u3 的 `_base.py`）

本讲反复出现两个术语：

- **工厂函数（factory function）**：不写 `SomeClass(...)`，而是调用一个普通函数 `xxx_array(...)` 来「帮你 new 一个稀疏对象」。工厂函数的好处是参数更友好、能自动选格式。
- **分派（dispatch）**：同一个函数，根据输入的类型/结构选择不同的实现路径。`_construct.py` 几乎每个函数内部都有「快速路径」与「通用路径」的分派，这是读懂它的钥匙。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [_construct.py](_construct.py) | 本讲主角。提供全部构造与组合工厂函数，文件顶部 [`__all__`](_construct.py#L6-L9) 列出 20 个导出名。 |
| [_coo.py](_coo.py) | 提供 `coo_array`/`coo_matrix`。绝大多数工厂函数「先造 COO，再 `asformat`」，所以 COO 是构造阶段的事实标准中间格式。 |
| [_bsr.py](_bsr.py) | 提供 `bsr_array`/`bsr_matrix` 及 [`tobsr`](_bsr.py#L356)。`kron` 在「B 较稠密」时走 BSR 路径。 |
| [_dia.py](_dia.py) | 提供 `dia_array`/`dia_matrix`。`diags_array`/`eye_array` 默认产出 DIA 格式。 |
| [_spfuncs.py](_spfuncs.py) | 提供 [`estimate_blocksize`](_spfuncs.py#L11) / [`count_blocks`](_spfuncs.py#L62)，被 BSR 相关逻辑（间接）使用。 |
| [_base.py](_base.py) | [`asformat`](_base.py#L471-L486) 与 [`_shape_as_2d`](_base.py#L98-L101) 等公共能力，被堆叠函数频繁调用。 |

### 新式 vs 旧式：一张总表

`_construct.py` 最容易让人迷路的是「同一个功能有两套名字」。记住下表即可（旧式函数返回 `spmatrix`，新式返回 `sparray`）：

| 功能 | 新式（推荐，返回 `sparray`） | 旧式（返回 `spmatrix`，部分会发 DeprecationWarning） |
|------|------------------------------|------------------------------------------------------|
| 单位/对角阵 | `eye_array` | `eye`、`identity` |
| 多对角线 | `diags_array` | `diags`、`spdiags` |
| 随机稀疏 | `random_array` | `random`、`rand` |
| 分块拼装 | `block_array` | `bmat` |
| 水平/垂直堆叠 | `hstack` / `vstack`（输入有 array 则返回 array） | 同名（全 matrix 输入返回 matrix） |
| 对角块 | `block_diag`（输入有 array 则返回 array） | 同名 |
| Kronecker 积 | `kron`（输入有 array 则返回 array） | 同名 |

> 规律：**带 `_array` 后缀的一定返回数组；不带后缀的「双面函数」（`kron`/`hstack`/`vstack`/`block_diag`）则「只要任一输入是 `sparray` 就返回数组，否则返回 matrix」。** 全是 NumPy 稠密数组输入时，这几个双面函数会发 `DeprecationWarning`，提示你显式 `coo_array(A)` 来锁定返回类型。

## 4. 核心概念与源码讲解

### 4.1 对角线工厂：`diags_array` 与 `eye_array`

#### 4.1.1 概念说明

很多数值矩阵是「带状」的：只有主对角线及上下若干条次对角线非零。一维有限差分得到的二阶导数矩阵、三对角方程组的系数矩阵都是典型例子。如果用稠密方式存，绝大多数位置都是显式零，浪费巨大；用 DIA（按对角线存储）格式则天然契合——每条对角线只需一个一维数组。

`diags_array` 就是这样一条「把若干条对角线缝成一个 DIA 矩阵」的流水线。你只需告诉它「这些值放在哪条对角线上」，它自动算形状、自动对齐、自动选格式。`eye_array`（单位阵/偏移对角全 1 阵）则是 `diags_array` 的特例与薄封装。

#### 4.1.2 核心流程

`diags_array(diagonals, *, offsets=0, shape=None, format=None, dtype=None)` 的执行流程：

1. **归一 offsets**：标量当成单条对角线；序列则要求与 `diagonals` 数量一致，否则报错。
2. **推形状**：若未给 `shape`，取「主对角线长度 + 首个偏移绝对值」作为方形边长。
3. **推 dtype**：用 `np.result_type(*diagonals)` 求公共类型。
4. **搭一个 `(对角线条数, M)` 的 `data_arr` 零矩阵**：其中列数 `M` 取所有对角线「对齐后所需长度」的最大值。
5. **逐条对角线把值填进 `data_arr` 的正确切片**，超长则截断、不符则报错。
6. **用 `dia_array((data_arr, offsets), shape=...)` 收尾，再 `.asformat(format)`**。

`eye_array` 的特别之处在于它对「方阵 + 主对角线」这一最常见情形有一条**快速路径**，直接手工拼出 CSR/CSC/COO 三数组，绕开 `diags_array`；其余情形（矩形、偏移对角线）才退化为调用 `diags_array`。

#### 4.1.3 源码精读

先看 `diags_array` 如何推断形状与搭 `data_arr`：

[_construct.py:416-431](_construct.py#L416-L431) —— 形状缺省时按主对角线推算；`M` 是所有对角线「对齐长度」的最大值，决定了 `data_arr` 的列数。注意 `M = max(0, M)`，防负。

接着是逐条对角线填值的关键循环：

[_construct.py:435-449](_construct.py#L435-L449) —— 对第 `j` 条对角线，`k = max(0, offset)` 是它在 `data_arr` 行内的起始列（因为偏移对角线在 DIA 里需要前导填充零对齐），`length = min(m+offset, n-offset, K)` 是该对角线在矩阵里真正有效的元素数；把 `diagonal[:length]` 填进 `data_arr[j, k:k+length]`。

> 这正是 `diags_array` 与 `dia_array` 的本质差别（[文档注释](_construct.py#L363-L367)）：`dia_array` 假定你**已经手工填好了**对角线两端的 padding（被忽略值）；`diags_array` 假定你**只给有效值、没有 padding**，由函数替你对齐。所以日常用 `diags_array` 更省心。

收尾一行：

[_construct.py:451](_construct.py#L451) —— 把 `data_arr` 与 `offsets` 交给 `dia_array`，再 `.asformat(format)`。`format=None` 时保持 DIA。

再看 `eye_array` 的快速路径：

[_construct.py:657-675](_construct.py#L657-L675) —— 当 `m==n` 且 `k==0`（即标准单位阵）时，对 `csr`/`csc`/`coo` 三种格式直接用 `np.arange` 手搓 `indptr/indices/data`（或 `row/col/data`），零拷贝、最快；其余情形（[第 674 行](_construct.py#L674-L675)）才构造一条全 1 对角线交给 `diags_array`。

旧式 `eye`/`identity`/`diags`/`spdiags` 都是新式的薄包装：[`identity` 调 `eye`](_construct.py#L596)，[`eye` 调 `_eye(..., False)`](_construct.py#L725)，[`diags` 调 `diags_array` 再 `dia_matrix`](_construct.py#L543-L544)，[`spdiags` 直接 `dia_matrix((data, diags), shape).asformat(format)`](_construct.py#L311-L315)。

#### 4.1.4 代码实践

**实践目标**：用 `diags_array` 造一个 5×5 三对角矩阵，观察 DIA 内部布局；再对比 `eye_array` 的快速路径与退化路径。

```python
# 示例代码
import numpy as np
import scipy.sparse as sp

n = 5
main = -2 * np.ones(n)        # 主对角线
lower = np.ones(n - 1)        # 下对角线（n-1 个值）
upper = np.ones(n - 1)        # 上对角线（n-1 个值）

A = sp.diags_array([main, lower, upper], offsets=[0, -1, 1])
print(A.toarray())
print("format =", A.format)          # 期望 dia
print("A.data=\n", A.data)           # DIA 内部 data 矩阵，留意 padding
print("A.offsets =", A.offsets)      # 期望 [0 -1 1] 或其排列

# 快速路径 vs 退化路径
I_csr = sp.eye_array(5, format='csr')      # 命中快速路径
I_k1  = sp.eye_array(5, k=1, format='csr') # k!=1 偏移，走 diags_array
print(I_csr.indptr, I_csr.indices)         # indptr=0..5, indices=0..4
```

**操作步骤**：把上述代码存为 `diag_practice.py` 并 `python diag_practice.py` 运行。

**需要观察的现象**：`A.data` 是一个 3 行的矩阵，每行对应一条对角线；上下对角线两端会出现被「填充」的位置（其值不影响结果，因为它们落在矩阵之外）。`A.offsets` 给出每行对应的对角线编号。

**预期结果**：`A.toarray()` 是标准二阶有限差分三对角阵（主对角 -2，上下次对角 1）。

> **待本地验证**：不同 SciPy 版本 `A.offsets` 的排列顺序与 `A.data` 的 padding 值可能略有差异，请以本地实际输出为准。

#### 4.1.5 小练习与答案

**练习 1**：`diags_array([1,2,3], offsets=1)` 不给 `shape`，结果形状为何是 4×4？
**答**：未指定形状时，`m = len(diagonals[0]) + abs(offsets[0]) = 3 + 1 = 4`，方形即 4×4（见 [_construct.py:417-419](_construct.py#L417-L419)）。

**练习 2**：为什么 `diags_array([1.0,-2.0,1.0], offsets=[-1,0,1], shape=(4,4))` 能做到「三条等长对角线」？等长为什么没报错？
**答**：因为填充循环用 `length = min(m+offset, n-offset, K)` 截取每条对角线的有效长度，并只拷贝 `diagonal[:length]`（[_construct.py:438-L442](_construct.py#L438-L442)）；只要输入长度 ≥ 有效长度即允许，多余的尾部被忽略。

---

### 4.2 随机稀疏：`random_array`

#### 4.2.1 概念说明

测试、基准、算法验证时，我们常需要「给定形状与非零密度」的随机稀疏矩阵。难点有二：一是**采样位置不能重复**（稀疏矩阵同一坐标只应有一个值），二是**数值分布要可定制**（默认均匀 [0,1)，但可能想要整数、复数或自定义分布）。`random_array` 正是为这两个需求设计的，核心实现在私有函数 `_random` 里。

#### 4.2.2 核心流程

`random_array(shape, *, density=0.01, format='coo', dtype=None, rng=None, data_sampler=None)`：

1. **算非零数**：`size = round(density * prod(shape))`，`density ∈ [0,1]`。
2. **采样位置（无放回）**：
   - 小矩阵：`rng.choice(总元素数, size, replace=False)` 采样「扁平下标」，再 `np.unravel_index` 还原成多维坐标；
   - 超大矩阵（扁平下标超过 `int64` 范围）：改用「逐维采样 + set 去重」的策略。
3. **采样数值**：`data_sampler` 缺省时按 dtype 选——整数用 `rng_integers`，复数用两个均匀分布合成，浮点用 `rng.uniform`。
4. **下标 dtype 降配**：`get_index_dtype(maxval=max(shape))` 尽量用 int32 省内存，必要时升 int64。
5. **`coo_array((data, ind), shape).asformat(format)`** 收尾。

#### 4.2.3 源码精读

位置采样的两条路径：

[_construct.py:1628-1639](_construct.py#L1628-L1639) —— 小规模用 `rng.choice` + `unravel_index`（Fortran 序，与稀疏列优先习惯一致）；大规模用 set 去重，避免单个超大整数下标溢出。

数值采样的默认分发：

[_construct.py:1611-1624](_construct.py#L1611-L1624) —— 根据 dtype 自动选 sampler：整数走全域整数、复数走单位方块、其余走 `rng.uniform`。用户传入的 `data_sampler` 只需接受 `size=` 关键字。

外层 `random_array` 的收尾与索引降配：

[_construct.py:1591-1596](_construct.py#L1591-L1596) —— 调 `_random` 拿到 `(data, ind)`，再用 `get_index_dtype` 把坐标数组降到最小够用的整型，最后 `coo_array(...).asformat(format)`。

旧式 `random`/`rand` 同样是薄包装：[`random` 调 `_random` 再 `coo_matrix`](_construct.py#L1749-L1750)，[`rand` 调 `random`](_construct.py#L1808)。

#### 4.2.4 代码实践

**实践目标**：生成一个 1000×1000、密度 1% 的随机稀疏矩阵，验证 `nnz` 与密度吻合，并体验自定义 `data_sampler`。

```python
# 示例代码
import numpy as np
import scipy.sparse as sp

rng = np.random.default_rng(0)
S = sp.random_array((1000, 1000), density=0.01, rng=rng)
print("nnz =", S.nnz, "期望 ≈", round(0.01 * 1000 * 1000))
print("format =", S.format)

# 自定义数值分布：只取 1（用来造 0/1 邻接矩阵）
def ones(size=None):
    return np.ones(size)
B = sp.random_array((5, 5), density=0.4, rng=rng, data_sampler=ones, dtype=np.int8)
print(B.toarray())
```

**需要观察的现象**：`S.nnz` 应非常接近 10000；`B` 的非零位置随机，但值恒为 1。

**预期结果**：`nnz` 约为 10000；`B.toarray()` 是一个 0/1 矩阵。

#### 4.2.5 小练习与答案

**练习 1**：`density=1.0` 时 `random_array` 返回的矩阵 `nnz` 等于多少？为什么？
**答**：`size = round(1.0 * prod(shape)) = prod(shape)`，即全稠密，`nnz` 等于总元素数，但仍是 COO/指定格式的稀疏对象（只是退化为「满」的）。

**练习 2**：为什么超大形状要走 set 去重而非 `rng.choice`？
**答**：`rng.choice(tot_prod, ...)` 在 `tot_prod > int64.max` 时无法用单个整数表示扁平下标（[_construct.py:1628](_construct.py#L1628)），故改逐维采样并用 set 消除重复坐标。

---

### 4.3 分块拼装：`block_array` / `hstack` / `vstack` / `bmat`

#### 4.3.1 概念说明

把若干现成的小矩阵按网格拼成大矩阵，是构造稀疏矩阵的另一大类需求：把局部刚度矩阵装配进全局矩阵、把多个特征向量横向拼成特征矩阵、把对角块与耦合块组合……`block_array` 接受一个二维「块网格」，`None` 表示全零块。`hstack`/`vstack` 是它的特例（单行/单列网格），`bmat` 是它的旧式别名。

精妙之处在于 `_block`（核心实现）有**两条路径**：当所有块恰好都是 CSR（或都是 CSC）时，走 C++ 快速堆叠，几乎零开销；否则退化为「全部转 COO 再拼」的通用路径。这正体现了「分派」思想。

#### 4.3.2 核心流程

`block_array(blocks)` → `_block(blocks, format, dtype)`：

1. `blocks = np.asarray(blocks, dtype='object')`，要求二维。
2. **快速路径 A（全 CSR）**：若所有块都是 CSR 且未指定其它格式，先沿副轴用 `_stack_along_minor_axis` 把每行拼成一个块（内部调 C++ `csr_hstack`），再用 `_compressed_sparse_stack` 沿主轴串行拼。
3. **快速路径 B（全 CSC）**：对称地处理列优先。
4. **通用路径**：把每个非 `None` 块转成 `coo_array`，校验每行/每列块尺寸一致，累计 `row_offsets`/`col_offsets`，把每个块的坐标加上偏移后写入一个全局大 COO，最后 `asformat`。

`hstack(blocks)` = `_block([blocks], ...)`（一行）；`vstack(blocks)` = `_block([[b] for b in blocks], ...)`（一列）。

#### 4.3.3 源码精读

`hstack`/`vstack` 如何共用 `_block`：

[_construct.py:1117-1121](_construct.py#L1117-L1121) —— `hstack` 把块列表包成单行网格 `[blocks]`，根据「是否有 array 输入」决定 `return_spmatrix`，再统一调 `_block`。`vstack`（[_construct.py:1165-1169](_construct.py#L1165-L1169)）同理，只是包成单列。

`_block` 的两条快速路径判别：

[_construct.py:1292-1317](_construct.py#L1292-L1317) —— 全 CSR 时先 `_stack_along_minor_axis`（副轴，调 C++ `csr_hstack`）化每行为单块，再 `_compressed_sparse_stack`（主轴，纯 NumPy 拼三数组）；全 CSC 对称。注意校验：所有块必须是同一格式且 `format in (None, 'csr'/'csc')` 才命中。

通用路径的坐标偏移拼接：

[_construct.py:1353-1376](_construct.py#L1353-L1376) —— `row_offsets`/`col_offsets` 是各块尺寸的累加；遍历每个块，把它的 `row + row_offsets[i]`、`col + col_offsets[j]` 写进全局 `row`/`col`，值写进全局 `data`，最后用 `coo_array` 收口。索引 dtype 用 `get_index_dtype` 按最大坐标自适应。

快速堆叠的纯数组实现（CSR 主轴）：

[_construct.py:978-1018](_construct.py#L978-L1018) —— `_compressed_sparse_stack` 直接 `np.concatenate` 各块的 `data`/`indices`，并把各块 `indptr` 错位相加串成新 `indptr`，零拷贝地拼出大 CSR/CSC。这是「全同格式」时的性能关键。

> 设计意图很清楚：**「先造 COO 拼装」是兜底通用方案，「全 CSR/CSC」是性能快车道**。所以工程上若要频繁拼装，先把各块统一转成 CSR/CSC 再 `hstack`/`vstack`，能拿到接近零开销的体验。

#### 4.3.4 代码实践

**实践目标**：用 `block_array` 把 4 个 2×2 小块拼成 4×4 分块矩阵；再用 `hstack`/`vstack` 对比快速路径与通用路径。

```python
# 示例代码
import numpy as np
import scipy.sparse as sp

A = sp.coo_array([[1, 2], [3, 4]])
B = sp.coo_array([[5], [6]])
C = sp.coo_array([[7]])
Z = None  # 全零块

# 2x2 分块网格 → 3x3 矩阵
M = sp.block_array([[A, B], [Z, C]])
print(M.toarray())

# 全 CSR 时命中快速路径
a = sp.csr_array([[1, 2], [3, 4]])
b = sp.csr_array([[5, 6]])
print(sp.vstack([a, b]).toarray())   # 走 _compressed_sparse_stack
print(sp.hstack([a, b]).toarray())   # 走 _stack_along_minor_axis -> csr_hstack
```

**需要观察的现象**：`M.toarray()` 应与 `bmat` 文档示例（[_construct.py:L1269-L1272](_construct.py#L1269-L1272)）一致；`vstack`/`hstack` 对全 CSR 输入返回 CSR。

**预期结果**：
```
[[1 2 5]
 [3 4 6]
 [0 0 7]]
```

#### 4.3.5 小练习与答案

**练习 1**：`block_array([[A, None], [None, C]])` 中 `None` 块如何处理？
**答**：通用路径里 `block_mask` 标记非 `None` 块（[_construct.py:1326-L1329](_construct.py#L1326-L1329)），`None` 块不产生任何坐标，等价于全零子块；最终大矩阵对应位置全为隐式零。

**练习 2**：为什么全 CSR 输入时 `hstack` 比 `block_array` 混合格式快？
**答**：全 CSR 命中 `_stack_along_minor_axis` → C++ `csr_hstack`（[_construct.py:1061-L1063](_construct.py#L1061-L1063)），直接操作三数组；混合格式必须先全转 COO 再重排坐标（通用路径），开销大得多。

---

### 4.4 Kronecker 积与对角块：`kron` 与 `block_diag`

#### 4.4.1 概念说明

**Kronecker 积** \( A \otimes B \) 把 `B` 的每个元素乘以 `A` 的对应元素，铺成一个分块大矩阵：若 `A` 为 \( m \times n \)、`B` 为 \( p \times q \)，则结果为 \( mp \times nq \)。形式化地：

\[
(A \otimes B)_{i,j} = A_{\lfloor i/p \rfloor,\, \lfloor j/q \rfloor} \cdot B_{i \bmod p,\, j \bmod q}
\]

它是构造高维离散算子的利器——比如用 `kronsum` 把两个一维 Laplacian 合成二维 Laplacian（见 [kronsum 文档示例](_construct.py#L915-L926)）。

**`block_diag`** 则更简单：把若干矩阵沿主对角线排开，非对角位置全零。它常用于「互不耦合的子系统」拼装。

#### 4.4.2 核心流程

`kron(A, B, format=None)`：

1. **决定返回容器**：输入有 `sparray` → 用 `*_array` 容器；有 `spmatrix` → `*_matrix`；全稠密 → `*_matrix` 并发 `DeprecationWarning`。
2. **路径分流**：若 `B` 是 2 维且「较稠密」（判据 `2*B.nnz >= prod(B.shape)`）且格式兼容 BSR，则走 **BSR 路径**：把 `A` 转 CSR，用 `A.data.repeat(B.size)` 把每个非零元展开成一个 `B` 形状的小块，乘以 `B` 的稠密形式，直接造成 BSR。
3. 否则走 **COO 路径**：把 `A` 转 COO，把每个非零元的坐标「按 `B` 的形状放大」，再叠加上 `B` 自身的相对坐标；支持 `A`、`B` 维度不同（用前导 1 维对齐）。
4. 任一为空（`nnz==0`）直接返回零矩阵。

`block_diag(mats)`：把每个矩阵转 COO，沿主对角累加偏移 `r_idx`/`c_idx`，最后 `coo_array(...).asformat(format)`。

#### 4.4.3 源码精读

`kron` 的 BSR/COO 分流判据：

[_construct.py:816-819](_construct.py#L816-L819) —— 当 `B` 是 2 维且 `2*B.nnz >= math.prod(B.shape)`（即 B 至少半稠密）时走 BSR；这条判据避免对稠密 `B` 做低效的逐元素展开。

BSR 路径的核心：把 `A` 的每个非零元展开成一个 `B` 形状的稠密块：

[_construct.py:832-836](_construct.py#L832-L836) —— `A.data.repeat(B.size)` 把每个非零值复制 `B.size` 次，`reshape(-1, p, q)` 后乘以稠密 `B`，得到 BSR 的 `data`；`indices`/`indptr` 复用 `A` 的，因为「块级结构」与 `A` 的稀疏结构同构。这正是 BSR 格式的用武之地。

COO 路径的坐标放大与叠加：

[_construct.py:856-872](_construct.py#L856-L872) —— `data = A.data.repeat(B.nnz)` 把 `A` 每个非零元复制 `B.nnz` 次；`coords` 各维先乘以 `B` 对应维长度（块定位），再加上 `B` 自身坐标（块内定位）；最后 `data` 与 `B.data` 相乘得到块内数值。这一段同时支持 `A`、`B` 维度不等（前导补 1，见 [_construct.py:845-L861](_construct.py#L845-L861)）。

`block_diag` 的对角偏移累加：

[_construct.py:1465-1490](_construct.py#L1465-L1490) —— 逐个矩阵转 COO，`row += r_idx`、`col += c_idx` 把它「挪」到对角线的下一段，`r_idx`/`c_idx` 随各矩阵尺寸累加；稠密输入则用 `np.divmod` 生成全坐标。`get_index_dtype` 按最大坐标选索引类型。

> 这两个函数都体现了 `_construct.py` 的统一手法：**「先在 COO 层把坐标与数据算好，最后一次 `coo_array(...).asformat(format)` 成型」**。COO 是构造期的通用中间表示。

#### 4.4.4 代码实践

**实践目标**：计算两个小矩阵的 Kronecker 积并验证形状；再用 `block_diag` 把三个矩阵沿对角排开。

```python
# 示例代码
import numpy as np
import scipy.sparse as sp

A = sp.csr_array([[0, 2], [5, 0]])
B = sp.csr_array([[1, 2], [3, 4]])

K = sp.kron(A, B)
print("K.shape =", K.shape)        # 期望 (4, 4)
print(K.toarray())                 # 与 kron 文档示例一致
print("K.format =", K.format)      # B 较稠密 → 期望 bsr

# block_diag
D = sp.block_diag([A, B, sp.coo_array([[7]])])
print("D.shape =", D.shape)        # 期望 (5, 5)
print(D.toarray())
```

**需要观察的现象**：`K.shape == (4,4)`；由于 `B` 是全稠密 2×2（`2*4 >= 4` 成立），`kron` 走 BSR 路径，`K.format` 为 `bsr`。`D` 是块对角矩阵。

**预期结果**：`K.toarray()` 与 [_construct.py:L770-L774](_construct.py#L770-L774) 文档示例一致：
```
[[ 0  0  2  4]
 [ 0  0  6  8]
 [ 5 10  0  0]
 [15 20  0  0]]
```

> **待本地验证**：`K.format` 在不同 SciPy 版本可能因判据微调而不同；若想强制 CSR，传 `format='csr'`。

#### 4.4.5 小练习与答案

**练习 1**：`kron(A, B)` 中 `A` 为 2×2、`B` 为 3×3，结果形状与 `format`（默认）分别是什么？
**答**：形状 (6,6)。若 `B` 较稠密（`2*9 >= 9` 成立）则默认 `bsr`，否则 `coo`（见 [_construct.py:816-L819](_construct.py#L816-L819)）。

**练习 2**：`block_diag` 何以能保持各块原有的稀疏结构而不引入多余非零？
**答**：它只在 COO 层平移坐标（`row + r_idx`、`col + c_idx`），不改变 `data`，非对角区域天然没有坐标，故为隐式零（[_construct.py:1473-L1475](_construct.py#L1473-L1475)）。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「组装一个稀疏算子」的小任务：

> 用 `diags_array` 构造一个带状矩阵（一维二阶差分算子）；用 `block_array` 把四个小块拼成一个 2×2 分块矩阵；最后用 `kron` 计算两个小矩阵的 Kronecker 积并打印形状。

```python
# 示例代码：综合实践
import numpy as np
import scipy.sparse as sp

# (1) 带状矩阵：一维二阶导数（三对角）
n = 4
e = np.ones(n)
Dxx = sp.diags_array([e[:-1], -2*e, e[:-1]], offsets=[-1, 0, 1], format='csr')
print("Dxx.shape =", Dxx.shape, " nnz =", Dxx.nnz)

# (2) block_array：4 个 2x2 小块拼成 4x4 分块矩阵
A = sp.csr_array([[1, 2], [3, 4]])
B = sp.csr_array([[5, 6], [7, 8]])
C = sp.csr_array([[9, 0], [0, 1]])
Z = sp.csr_array((2, 2))           # 显式全零块（也可用 None）
M = sp.block_array([[A, B], [Z, C]])
print("M.shape =", M.shape)
print(M.toarray())

# (3) kron：两个小矩阵的 Kronecker 积
P = sp.csr_array([[1, 0], [0, 2]])
Q = sp.csr_array([[1, 2], [3, 4]])
K = sp.kron(P, Q)
print("K.shape =", K.shape)         # 期望 (4, 4)
print(K.toarray())
```

**验收点**：
- `Dxx` 是三对角阵，`nnz` 应为 `3n - 2`；
- `M` 命中全 CSR 快速路径，`M.format == 'csr'`；
- `K.shape == (4, 4)`，且因 `Q` 稠密而走 BSR 路径（`K.format` 多半为 `bsr`）。

> **进阶**：把 (1) 的两个一维算子用 `kronsum` 合成二维 Laplacian（参考 [kronsum 文档示例](_construct.py#L915-L926)），观察其 `nnz` 与稀疏结构——这是 Kronecker 族在偏微分方程离散化中的经典应用。

## 6. 本讲小结

- `_construct.py` 把「按结构批量造稀疏矩阵」的需求集中成一组工厂函数，顶部 [`__all__`](_construct.py#L6-L9) 列出 20 个导出名。
- **新式 `*_array` vs 旧式 `*_matrix`** 有清晰对应：`eye_array`↔`eye`/`identity`、`diags_array`↔`diags`/`spdiags`、`random_array`↔`random`/`rand`、`block_array`↔`bmat`；`kron`/`hstack`/`vstack`/`block_diag` 是「双面函数」，按输入是否含 `sparray` 决定返回类型。
- **统一手法**：几乎所有函数都「先造 COO/DIA，再 `asformat(format)` 成型」——COO 是构造期的通用中间表示。
- `diags_array` 替你对齐对角线 padding（区别于 `dia_array` 要你自己填）；`eye_array` 对方阵主对角线有手搓三数组的快速路径。
- `_block` 有两条路径：全 CSR/CSC 走 C++/纯数组快速堆叠（`_compressed_sparse_stack`、`_stack_along_minor_axis` → `csr_hstack`），混合格式退化为 COO 通用拼装。
- `kron` 在「B 较稠密」时走 BSR 路径（块级结构复用 `A` 的 CSR），否则走 COO 逐元素展开路径；`block_diag` 在 COO 层平移坐标即可。

## 7. 下一步学习建议

- 本讲只读了「构造层」。下一步建议进入 [u3-l2 索引机制 `_index.py`](u3-l2-indexing-mixin.md)，看 `__getitem__` 如何把整数/切片/数组索引分派到 `_get_int/_get_slice/_get_array`。
- 若对构造出的矩阵如何做算术感兴趣，可跳读 [u3-l3 `_base.py` 公共操作](u3-l3-base-common-ops.md) 里的 `dot`/`multiply`/`sum`/`diagonal`，与本讲的 `asformat` 互相呼应。
- 想深入 `kron` 的 BSR 路径为何高效，建议重读 [u2-l4 BSR 格式](u2-l4-bsr-format.md) 中 `estimate_blocksize`/`count_blocks`（[_spfuncs.py](_spfuncs.py)）如何度量块结构。
- 动手方向：挑一个本讲未覆盖的组合函数 `kronsum`，仿照 4.4 的方法画出它的 BSR/COO 调用链，并写一个最小示例验证二维 Laplacian 的装配。
