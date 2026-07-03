# COO 坐标（三元组）格式

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 COO（COOrdinate，又称 ijv / triplet）格式「只存非零元 + 它们的坐标」的内存布局；
- 看懂 `_coo_base.__init__` 如何把稠密数组、其它稀疏对象、坐标三元组、纯形状四类输入统一翻译成 `coords + data + _shape`；
- 解释 `row` / `col` / `coords` 三组属性的关系，以及 COO 为何是七种格式里唯一原生支持 1 到 64 维的格式；
- 理解 `has_canonical_format` 这个不变量标志，以及 `sum_duplicates` 在「重复坐标求和」中的作用；
- 动手用 COO 组装一个微型有限元刚度矩阵，体会「COO 用来造，CSR 用来算」这条设计主线。

## 2. 前置知识

本讲承接 [u2-l1 类继承体系] 的结论：所有稀疏类的公共实现基类是 `_spbase`，而 `_coo_base` 是 COO 格式的实现基类，`coo_array(_coo_base, sparray)` 与 `coo_matrix(spmatrix, _coo_base)` 只是套上「数组语义」或「矩阵语义」的外壳（见 [_coo.py:1694](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py#L1694) 与 [_coo.py:1811](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py#L1811)）。因此本讲讨论的几乎全部行为都来自 `_coo_base`。

复习几个上一讲已经建立、本讲会反复用到的概念：

- **隐式零**：稀疏存储里「没有显式存下来的零」。COO 只记录非零元，零是隐式的。
- **`nnz`**：已存储元素个数，**包含显式零**，区别于真正非零的 `count_nonzero`。
- **三元组（triplet / ijv）**：用一个值 `v` 配上它在每一维上的坐标 `i, j, ...` 来描述一个非零元，即 `(i, j, v)`。
- **`_formats` 字典**：在 [_base.py:36-46](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_base.py#L36-L46) 中登记所有格式全称，其中 `'coo': [6, "COOrdinate"]` 就是本讲主角。

## 3. 本讲源码地图

本讲聚焦两个文件：

| 文件 | 作用 |
| --- | --- |
| [_coo.py](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py) | COO 格式的全部实现：构造器 `__init__`、属性 `row/col/coords`、`sum_duplicates`、与 CSR/CSC 的互转 `tocsr/tocsc` 等。 |
| [_data.py](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_data.py) | `_data_matrix` 基类：所有「以 `.data` 为核心」的格式（含 COO、CSR、CSC、DIA）共享的逐元素运算，定义了 `_deduped_data`、`_with_data` 等契约。 |

还会少量引用 [_sputils.py](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_sputils.py) 里的工具函数（`getdtype` / `getdata` / `get_index_dtype` / `isshape` / `check_shape`）以及 [tests/test_coo.py](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/tests/test_coo.py) 中的真实测试用例。

## 4. 核心概念与源码讲解

### 4.1 COO 的数据布局：coords + data

#### 4.1.1 概念说明

COO 的存储思想极其朴素：**只存非零元的值，以及它们在每一维上的坐标**。一个 N 维的 COO 数组由两部分组成：

- `data`：长度为 `nnz` 的一维数组，存放每个非零元的数值；
- `coords`：一个长度为 `ndim` 的元组，元组里每个元素都是长度为 `nnz` 的整数数组，`coords[d][k]` 表示第 `k` 个非零元在第 `d` 维上的坐标。

也就是说，第 `k` 个非零元满足：

\[
\text{arr}[\text{coords}[0][k],\ \text{coords}[1][k],\ \ldots,\ \text{coords}[ndim-1][k]] = \text{data}[k]
\]

对 2 维情形，`coords = (row, col)`，这就是经典的「行坐标、列坐标、值」三元组（ijv / triplet），也是 `_coo.py` 顶部模块文档串的写法（[_coo.py:1](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py#L1)）。

COO 的关键特性是**允许重复坐标**：同一个 `(i, j)` 可以出现多次，它们的值在需要时（例如转 CSR 或调用 `sum_duplicates`）会被**累加**。这一特性让它特别适合「从多个小贡献拼装成一个大矩阵」的场景（详见 4.4）。

#### 4.1.2 核心流程

COO 的内存只与非零元数 `nnz` 与维数 `ndim` 有关。忽略对象开销，2 维情形下每个非零元大约占用：

\[
\text{字节/非零元} \approx \text{sizeof}(\text{data}) + ndim \times \text{sizeof}(\text{index})
\]

例如 `float64` 数据 + `int64` 行列索引、2 维，约为 \(8 + 2 \times 8 = 24\) 字节/非零元；而稠密 `ndarray` 占用正比于总元素数 \(\prod \text{shape}\)，与稀疏度无关。所以矩阵越大、越稀疏，COO 越省内存；但 COO 不擅长随机访问与逐元素运算（取一个 `A[i,j]` 需要线性扫描 `coords`）。

#### 4.1.3 源码精读

`coords` 与 `data` 是 `_coo_base` 实例上的普通属性（不是 `@property`），在 `__init__` 里被赋值（见 4.2）。类声明里只显式定义了 `_format` 与 `_allow_nd` 两个类属性：

[_coo.py:28-30](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py#L28-L30) — 定义 `_coo_base`，`_format='coo'` 是格式标识，`_allow_nd = tuple(range(1, 65))` 表示 COO 接受 1 到 64 维的输入，这是七种格式里唯一原生高维的。

`_data_matrix`（[_data.py:20](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_data.py#L20)）则把 `data` 当作核心，提供 `dtype`、逐元素运算等共享能力；它的 `dtype` 属性直接读 `self.data.dtype`：

[_data.py:24-26](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_data.py#L24-L26) — `dtype` 直接来自 `data` 数组，说明「值数组」是 COO 的主存储。

#### 4.1.4 代码实践

1. 实践目标：直观感受 COO 的 `coords` / `data` 布局。
2. 操作步骤：运行下面这段「示例代码」。

```python
# 示例代码
import numpy as np
from scipy.sparse import coo_array

# 经典三元组：3 个非零元
row  = np.array([0, 3, 1])
col  = np.array([0, 3, 1])
data = np.array([4, 5, 7])
A = coo_array((data, (row, col)), shape=(4, 4))

print("coords =", A.coords)        # (array([0,3,1]), array([0,3,1]))
print("data   =", A.data)          # [4 5 7]
print("nnz    =", A.nnz)           # 3
print(A.toarray())
```

3. 需要观察的现象：`coords` 是一个含两个整数数组的元组，与 `data` 一一对应。
4. 预期结果：`toarray()` 输出一个 4×4 矩阵，只有 `(0,0)=4`、`(3,3)=5`、`(1,1)=7` 三处非零。

#### 4.1.5 小练习与答案

**练习 1**：上面 `A` 中如果把 `shape` 改成 `(2, 2)` 会发生什么？为什么？

**参考答案**：会抛 `ValueError`。因为坐标里出现了 `3`，而 `_check`（[_coo.py:220-226](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py#L220-L226)）会校验 `idx.max() < self.shape[i]`，`3 >= 2` 不满足。

---

### 4.2 `_coo_base.__init__` 的四类输入分发

#### 4.2.1 概念说明

`coo_array` / `coo_matrix` 共用签名 `coo_array(arg1, shape=None, dtype=None, copy=False)`。`__init__` 的核心职责是把形形色色的 `arg1` 统一翻译成内部的 `coords + data + _shape`，并设置 `has_canonical_format`。它把输入分成四大类：

1. **纯形状**：`arg1` 是 `(3, 4)` 这样的形状元组 → 构造一个全零的空 COO；
2. **坐标三元组**：`arg1` 是 `(data, coords)` 元组 → 这是 COO 的「母语」输入；
3. **其它稀疏对象**：`arg1` 是 `csr_array` 等 → 调用 `arg1.tocoo()` 转换；
4. **稠密数组**：`arg1` 是 `ndarray` / 列表 → 用 `np.asarray` 转成数组，再 `nonzero()` 抽坐标。

#### 4.2.2 核心流程

分发逻辑可用下面的伪代码概括：

```
if arg1 是 tuple:
    if arg1 是合法形状(isshape):        # 分支 A：空矩阵
        _shape = arg1; coords = 全空; data = 空
        has_canonical_format = True
    else:                                # 分支 B：三元组
        obj, coords = arg1
        若未给 shape，则由各坐标的 max+1 推断
        has_canonical_format = False     # 允许重复/乱序
else if arg1 是稀疏对象(issparse):       # 分支 C：稀疏→COO
    if 同为 coo 且 copy: 直接复制 coords/data
    else: arg1.tocoo(copy=copy)
else:                                    # 分支 D：稠密
    M = np.asarray(arg1)
    coords = M.nonzero()                 # 天然有序且无重复
    data = M[coords]
    has_canonical_format = True
```

注意 `has_canonical_format` 的初值是分情况设置的：**只有三元组输入（分支 B）和跨格式稀疏转换（分支 C 的 else）才设为 `False`**——因为这两条路径都可能带来重复坐标；而空矩阵与稠密构造天然无重复，设为 `True`。

#### 4.2.3 源码精读

整段 `__init__` 在 [_coo.py:32-103](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py#L32-L103)。

- [_coo.py:37-45](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py#L37-L45) — 分支 A：`isshape(arg1, allow_nd=self._allow_nd)` 判定为形状时，建一组空坐标数组和一个空 `data`，`has_canonical_format=True`。索引 dtype 由 `get_index_dtype` 按形状最大值自动选 `int32`/`int64`。
- [_coo.py:46-65](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py#L46-L65) — 分支 B：解包 `obj, coords = arg1`；若没给 `shape`，则用 `tuple(operator.index(np.max(idx)) + 1 for idx in coords)` 从每个坐标轴的最大值推断形状；随后把每个坐标轴转成统一索引 dtype，`data` 用 `getdata` 处理，并设 `has_canonical_format=False`。
- [_coo.py:66-78](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py#L66-L78) — 分支 C：`issparse(arg1)` 时，若同为 coo 且 `copy`，直接拷贝 `coords`/`data` 并继承对方的 `has_canonical_format`；否则 `arg1.tocoo(copy=copy)`，并重置 `has_canonical_format=False`。
- [_coo.py:79-98](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py#L79-L98) — 分支 D：稠密输入。`np.asarray(arg1)` 后，对 `spmatrix`（非数组语义）强制 `atleast_2d` 并禁止 >2 维；坐标用 `M.nonzero()`（返回按 C 序排列、无重复的索引元组），所以 `has_canonical_format=True`。

末尾 [_coo.py:100-103](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py#L100-L103) 还有两步收尾：维数 > 2 时把坐标统一升级为 `int64`（防止高维扁平化索引溢出），再调用 `self._check()` 做一致性校验。

涉及的工具函数都在 [_sputils.py](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_sputils.py)：

- [getdtype (L111-139)](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_sputils.py#L111-L139)：按「显式 dtype → 输入对象 dtype → 默认值」三级回退，并校验 dtype 在白名单内；
- [getdata (L142-151)](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_sputils.py#L142-L151)：`np.array` 的薄封装，额外对 object 数组告警；
- [get_index_dtype (L264)](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_sputils.py#L264)：根据坐标数组与 `maxval` 在 `int32`/`int64` 间选型（详见 u6-l2）。

#### 4.2.4 代码实践

1. 实践目标：用四种方式构造同一个对角矩阵，验证它们殊途同归。
2. 操作步骤：运行下面这段「示例代码」。

```python
# 示例代码
import numpy as np
from scipy.sparse import coo_array, csr_array

D = np.diag([1.0, 2.0, 3.0])                 # 稠密
a_dense = coo_array(D)                       # 分支 D
a_shape = coo_array((3, 3))                  # 分支 A（空）
row = [0, 1, 2]; col = [0, 1, 2]; v = [1., 2., 3.]
a_trip  = coo_array((v, (row, col)), shape=(3, 3))   # 分支 B
a_from  = coo_array(csr_array(D))            # 分支 C

print(a_dense.has_canonical_format, a_trip.has_canonical_format)
print(np.array_equal(a_dense.toarray(), a_trip.toarray()))
```

3. 需要观察的现象：稠密构造 `has_canonical_format=True`，三元组构造为 `False`。
4. 预期结果：四种方式（除空矩阵）得到的 `toarray()` 完全一致。

#### 4.2.5 小练习与答案

**练习 1**：为什么分支 B（三元组）要把 `has_canonical_format` 设成 `False`，而分支 D（稠密）设成 `True`？

**参考答案**：三元组允许重复 `(i,j)` 且顺序任意，无法保证无重复、按序，故不能声称处于 canonical 形态；稠密路径用 `M.nonzero()` 抽坐标，`nonzero()` 对每个轴返回严格递增、无重复的索引，天然满足 canonical，故设 `True`。

---

### 4.3 `row` / `col` / `coords` 属性与 N 维支持

#### 4.3.1 概念说明

COO 内部只有一份「通用」的坐标存储 `coords`（一个长度为 `ndim` 的元组）。`row` 与 `col` 只是**为 2 维用户提供的便利属性**：在 2 维时 `row == coords[-2]`、`col == coords[-1]`。这一设计让同一套代码既能服务传统的 2 维矩阵，又能服务 1 维数组乃至高维张量。

#### 4.3.2 核心流程

- `col`：恒为 `coords[-1]`（最后一维坐标），对任何维数都有定义；
- `row`：当 `ndim > 1` 时为 `coords[-2]`（倒数第二维）；当 `ndim == 1`（一维数组）时返回一个**只读的全零数组**——因为一维数组没有「行」的概念，但保留 `row` 接口可以让 2 维向 1 维迁移的旧代码不致立刻崩溃。

#### 4.3.3 源码精读

`row` 属性在 [_coo.py:105-111](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py#L105-L111)：`ndim > 1` 返回 `self.coords[-2]`；否则构造一个与 `col` 等长的全零数组，并用 `setflags(write=False)` 标记为只读，防止用户误写。`col` 属性在 [_coo.py:121-123](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py#L121-L123)，直接返回 `self.coords[-1]`。

正因为 `row`/`col` 派生自 `coords`，构造器只维护 `coords`；`transpose`（[_coo.py:228-246](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py#L228-L246)）转置时也只是按 `axes` 重排 `coords` 元组与形状，`data` 原封不动，开销极小。

`coo_array` 的类文档（[_coo.py:1719-1735](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py#L1719-L1735)）把 `coords`、`data`、`has_canonical_format` 列为主属性，并写明 `coords` 是「tuple of index arrays」「`A[coords] = data`」。

#### 4.3.4 代码实践

1. 实践目标：观察 1 维与 2 维下 `row`/`col`/`coords` 的差别。
2. 操作步骤：运行下面这段「示例代码」。

```python
# 示例代码
import numpy as np
from scipy.sparse import coo_array

a1 = coo_array(([5., 7.], ([1, 3],)))          # 1 维
print("1D coords =", a1.coords)                # (array([1,3]),)
print("1D row    =", a1.row)                   # [0 0]（只读全零）
print("1D col    =", a1.col)                   # [1 3]

a2 = coo_array(([5., 7.], ([0, 1], [1, 0])), shape=(2, 2))   # 2 维
print("2D row    =", a2.row)                   # [0 1]
print("2D col    =", a2.col)                   # [1 0]
print("2D coords =", a2.coords)                # (array([0,1]), array([1,0]))
```

3. 需要观察的现象：1 维时 `coords` 只有一个数组，`row` 是全零只读数组；2 维时 `coords` 含两个数组，分别等于 `row` 和 `col`。
4. 预期结果：与上方注释一致。

#### 4.3.5 小练习与答案

**练习 1**：能否对 1 维 `coo_array` 执行 `a1.row = something`？会怎样？

**参考答案**：不能。`row` 的 setter（[_coo.py:114-119](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py#L114-L119)）在 `ndim < 2` 时直接抛 `ValueError('cannot set row attribute of a 1-dimensional sparse array')`。

**练习 2**：`_allow_nd = tuple(range(1, 65))` 意味着 COO 最多支持多少维？

**参考答案**：`range(1, 65)` 即 1 到 64（含端点），所以 COO 原生支持 1 到 64 维；这也是 CSR/CSC（只支持 1–2 维）在做高维运算前往往要先转 COO 的原因。

---

### 4.4 `has_canonical_format` 与 `sum_duplicates`：有限元组装的关键

#### 4.4.1 概念说明

`has_canonical_format` 是一个布尔不变量，表示「坐标已按行（再按列）升序排列、且没有重复的 `(i,j)`」。它之所以重要，是因为很多下游操作（求对角线 `diagonal`、求最大最小值的 `argmin/argmax`、逐元素 ufunc）都**假设坐标无重复**，否则结果会出错。

`sum_duplicates()` 就是把重复坐标合并、把对应 `data` 相加的「就地」操作，合并后会把 `has_canonical_format` 置为 `True`。而 `tocsr()` / `tocsc()` 在转换时**会自动调用一次 `sum_duplicates`**（若尚未 canonical）——这正是「COO 允许重复坐标、转 CSR 时自动求和」的实现机制，也是有限元刚度矩阵组装的标准套路。

#### 4.4.2 核心流程

`sum_duplicates` 的算法（[_coo.py:810-827](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py#L810-L827)）分三步：

1. **排序**：`order = np.lexsort(coords[::-1])`。`np.lexsort` 以最后一个键为主序，`coords[::-1]` 把最后一维翻到末尾，因此主序键是 `coords[0]`（即「先按行，再按列」的 C 序），与函数注释一致。
2. **找唯一边界**：比较相邻坐标，任一维不同即视为「新唯一组」，得到布尔掩码 `unique_mask`。
3. **分段求和**：用 `np.add.reduceat` 在每组重复坐标的区间上对 `data` 求和。

设合并前有 `nnz` 个三元组、去重后剩 `m` 个唯一坐标，则复杂度由排序主导，为 \(\mathcal{O}(nnz \log nnz)\)。

`has_canonical_format` 还与「显式零」配合：canonical 形态**允许 `data` 里出现显式零**（即存了值为 0 的元素），但**不允许重复坐标**。要去掉显式零需另用 `eliminate_zeros()`（[_coo.py:829-836](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py#L829-L836)）。

#### 4.4.3 源码精读

- `sum_duplicates` 入口在 [_coo.py:799-808](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py#L799-L808)：若已 canonical 直接 `return`（幂等），否则调用 `_sum_duplicates` 并把标志置 `True`。
- 真正的合并逻辑 `_sum_duplicates` 在 [_coo.py:810-827](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py#L810-L827)，即上面三步。
- 转换时的自动求和：`tocsr`（[_coo.py:368-412](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py#L368-L412)）在拿到 CSR 三数组后，若 `not self.has_canonical_format` 就调用 `x.sum_duplicates()`（见 [_coo.py:410-411](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py#L410-L411)）；`tocsc` 同理（[_coo.py:364-365](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py#L364-L365)）。注意这里调用的是**结果对象** `x` 的 `sum_duplicates`，因为 COO→CSR 的 C++ 内核 `coo_tocsr` 本身也会合并重复项。
- 逐元素运算前的去重：`_data_matrix._deduped_data`（[_data.py:32-35](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_data.py#L32-L35)）会在调用 `abs`、`sin` 等之前先 `sum_duplicates()`，确保逐元素作用不破坏数值正确性。例如 `__abs__`（[_data.py:37-38](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_data.py#L37-L38)）走的就是 `_with_data(abs(self._deduped_data()))`。
- 真实测试可参看 [tests/test_coo.py:275-300](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/tests/test_coo.py#L275-L300) 的 `test_sum_duplicates`：它对 1 维、4 维、无重复三种情形验证了「`sum_duplicates` 改变 `nnz` 但不改变 `toarray()` 结果」。

#### 4.4.4 代码实践（本讲主实践）

1. 实践目标：亲手看到重复坐标在 `sum_duplicates` 前后被合并，并用 COO 组装一个 3×3 有限元刚度矩阵。
2. 操作步骤：运行下面这段「示例代码」。

```python
# 示例代码
import numpy as np
from scipy.sparse import coo_array

# ---- Part 1: 重复坐标的合并 ----
row  = np.array([0, 0, 1, 3, 1, 0, 0])
col  = np.array([0, 2, 1, 3, 1, 0, 0])
data = np.array([1, 1, 1, 1, 1, 1, 1])
A = coo_array((data, (row, col)), shape=(4, 4))
print("before sum_duplicates: nnz =", A.nnz, " data =", A.data)
print("has_canonical_format =", A.has_canonical_format)

A.sum_duplicates()
print("after  sum_duplicates: nnz =", A.nnz, " data =", A.data)
print("has_canonical_format =", A.has_canonical_format)
print(A.toarray())   # (0,0) 处三个 1 被加成 3；(1,1) 处两个 1 被加成 2

# ---- Part 2: 用 COO 组装 1D 线性有限元刚度矩阵 ----
def assemble_stiffness(n_nodes):
    """n_nodes 个节点、(n_nodes-1) 个线性单元的一维 Laplacian 刚度矩阵。
    每个单元连接相邻两节点，单元刚度(未缩放)为 [[1,-1],[-1,1]]。"""
    rows, cols, vals = [], [], []
    for e in range(n_nodes - 1):
        dofs = [e, e + 1]                 # 该单元对应的全局自由度
        Ke = np.array([[1.0, -1.0],
                       [-1.0,  1.0]])     # 单元刚度
        for li, i in enumerate(dofs):
            for lj, j in enumerate(dofs):
                rows.append(i); cols.append(j); vals.append(Ke[li, lj])
    # 关键：直接把所有三元组喂给 COO，重复的 (i,j) 在 tocsr 时自动求和
    K = coo_array((vals, (rows, cols)), shape=(n_nodes, n_nodes))
    return K.tocsr()                      # 「COO 用来造，CSR 用来算」

K = assemble_stiffness(3)
print(K.toarray())
```

3. 需要观察的现象：
   - Part 1 中，`sum_duplicates` 前 `nnz=7`、`data` 含七个 1；之后 `nnz` 变小，重复项被累加（如 `(0,0)` 变成 3）。
   - Part 2 中，节点 1 同时属于两个单元，其刚度贡献在 `(1,1)` 处累加成 2，最终得到经典三对角刚度矩阵。
4. 预期结果：

```
before ... data = [1 1 1 1 1 1 1]
after  ... data 含 (0,0)=3, (0,2)=1, (1,1)=2, (3,3)=1 ...
K =
[[ 1. -1.  0.]
 [-1.  2. -1.]
 [ 0. -1.  1.]]
```

5. 说明：单元数量较大时，`coo_array((vals,(rows,cols)),...).tocsr()` 比「逐个 `K[i,j] += ...`」快得多——这正是 COO 重复坐标求和带来的组装红利。

#### 4.4.5 小练习与答案

**练习 1**：对一个 `has_canonical_format=True` 的 COO 再调用一次 `sum_duplicates()`，会发生什么？

**参考答案**：什么也不做。[_coo.py:804-805](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py#L804-L805) 在入口处判断 `if self.has_canonical_format: return`，所以该操作是幂等的。

**练习 2**：`sum_duplicates` 会去掉值为 0 的显式零吗？若要去掉该用什么？

**参考答案**：不会。`sum_duplicates` 只合并重复坐标，不删零；`has_canonical_format` 明确允许 `data` 中存在显式零。要去掉显式零应调用 `eliminate_zeros()`（[_coo.py:829-836](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py#L829-L836)），它用 `mask = self.data != 0` 过滤 `data` 与 `coords`。

**练习 3**：为什么 `_data_matrix` 在做 `abs()` 等逐元素运算前要先 `_deduped_data()`？

**参考答案**：若坐标有重复，直接对 `data` 逐元素求绝对值会破坏数值——例如两份 `(i,j)` 的 `+3` 与 `-3` 本应合成 `0`，先 `abs` 再合成会错误地得到 `6`。`_deduped_data`（[_data.py:32-35](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_data.py#L32-L35)）先就地 `sum_duplicates` 保证无重复，再做逐元素运算，从而保证正确。

## 5. 综合实践

把本讲的知识串起来，完成一个「**COO 组装 → 求和去重 → 转 CSR 计算**」的完整小任务。

任务背景：用中心差分离散一维泊松方程 \(-u'' = f\)，得到稀疏线性系统 \(K u = f\)，其中 \(K\) 是 \(N \times N\) 三对角刚度矩阵（主对角 2、两条次对角 -1）。要求：

1. **用 COO 三元组**组装 \(K\)（提示：循环每个对角线，往 `rows/cols/vals` 里追加三元组，允许重复贡献）；
2. 组装完成后打印 `K_coo.nnz` 与 `K_coo.has_canonical_format`；
3. 调用 `K_coo.sum_duplicates()`，再次打印 `nnz` 与 `has_canonical_format`，确认变化；
4. `.tocsr()` 后用 `K @ np.ones(N)` 做一次矩阵-向量乘，打印结果（思考：为什么结果是几乎全零的向量？）；
5. 额外：把同样的 \(K\) 直接用稠密 `np.ndarray` 构造一遍，比较两者的 `nbytes`，体会稀疏在大 \(N\) 下的内存优势。

参考骨架（「示例代码」，N 取 5）：

```python
# 示例代码
import numpy as np
from scipy.sparse import coo_array

N = 5
rows, cols, vals = [], [], []
for i in range(N):
    rows += [i, i, i]
    cols += [i, i-1 if i > 0 else i, i+1 if i < N-1 else i]
    vals += [2.0, -1.0 if i > 0 else 0.0, -1.0 if i < N-1 else 0.0]
# 注意上面会产生 (i,i) 等重复/自指三元组，正好用来观察 sum_duplicates

K_coo = coo_array((vals, (rows, cols)), shape=(N, N))
print("nnz before:", K_coo.nnz, K_coo.has_canonical_format)
K_coo.sum_duplicates()
print("nnz after :", K_coo.nnz, K_coo.has_canonical_format)
K = K_coo.tocsr()
print(K @ np.ones(N))        # 常数向量在二阶差分下近似为 0（边界有偏差）
```

> 说明：上面骨架刻意制造了一些自指/可合并的三元组；若你希望一开始就干净，可改成「主对角一批、上对角一批、下对角一批」分别追加，体会「COO 不在乎顺序、转 CSR 时统一整理」的便利。

## 6. 本讲小结

- COO 用 `coords`（每维一个整数数组）+ `data` 两个部分存储非零元，零是隐式的；这是最直观、最适合「拼装」的格式。
- `_coo_base.__init__` 把输入分成**形状 / 三元组 / 其它稀疏 / 稠密**四类，统一翻译成 `coords + data + _shape`，并按路径设置 `has_canonical_format` 的初值（三元组与跨格式转换设 `False`，其余设 `True`）。
- `coords` 是唯一的通用坐标存储；`row`/`col` 只是 2 维便利属性（`row=coords[-2]`、`col=coords[-1]`），1 维时 `row` 返回只读全零数组。
- COO 是七种格式中唯一原生支持 1 到 64 维的格式（`_allow_nd = tuple(range(1, 65))`）。
- `has_canonical_format` 表示「有序且无重复」；`sum_duplicates()` 用 `lexsort` 排序 + `reduceat` 分段求和来合并重复坐标，是幂等的，并在 `tocsr/tocsc` 时被自动触发。
- 逐元素运算（`abs`、保零 ufunc 等）会先经 `_deduped_data` 去重以保证正确，这是 `_data_matrix` 对所有「以 `.data` 为核心」格式的统一契约。

## 7. 下一步学习建议

- **下一讲 [u2-l3 CSR 与 CSC 压缩格式]** 会讲 `tocsr`/`tocsc` 的落点：`indptr/indices/data` 紧凑布局、`_cs_matrix.__init__` 对 `(data,ij)` 与 `(data,indices,indptr)` 两种输入的处理，以及 `_swap` 机制如何让 CSR/CSC 共用一套代码。
- 想深入转换复杂度与 canonical 不变量维护，可继续读 [u6-l1 格式转换、去重与 canonical format]。
- 想了解索引 dtype 如何随矩阵规模在 int32/int64 间选择（`get_index_dtype` / `safely_cast_index_arrays`），可读 [u3-l5 工具函数 _sputils.py] 与 [u6-l2 大规模 64 位索引]。
- 建议同时对照阅读源码：[_coo.py](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py) 的 `__init__`、`sum_duplicates`、`tocsr` 三段，以及 [tests/test_coo.py](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/tests/test_coo.py) 的 `test_sum_duplicates`。
