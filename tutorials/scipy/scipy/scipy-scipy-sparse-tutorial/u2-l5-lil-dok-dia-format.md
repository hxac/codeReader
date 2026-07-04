# LIL、DOK、DIA 三种构造/增量格式

## 1. 本讲目标

学完本讲，读者应该能够：

- 说清 **LIL（List of Lists）** 的 `rows`/`data` 双列表结构，理解它为什么适合「一行一行地」增量构造稀疏矩阵。
- 说清 **DOK（Dictionary Of Keys）** 用 `_dict` 存 `(row, col) -> val` 映射、并且类本身继承内置 `dict` 的设计，理解它 O(1) 读写单个元素的代价与好处。
- 说清 **DIA（DIAgonal）** 用 `offsets` + 二维 `data` 按对角线存储的特点，能用它直接造出三对角矩阵等带状结构。
- 会做一件贯穿全讲的工程动作：**先用 LIL/DOK/DIA 增量或带状构造，再统一 `tocsr()` 转成 CSR 去做运算**——这正是这三个格式在真实代码里的定位（构造器，而非运算器）。

## 2. 前置知识

本讲建立在前面几讲已经建立的心智模型之上，不再重复，只做最短的承接：

- 稀疏存储只存非零元及其位置，零是隐式的（见 u1-l1）。
- 所有稀疏类都继承自公共基类 [`_spbase`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_base.py#L85-L88)，它把 `shape`/`nnz`/`format`/`asformat`/`tocsr` 等公共能力集中实现；`_format` 标识格式，`_allow_nd` 控制可接受的维度（默认只允许 2 维，见 [_base.py:91-92](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_base.py#L91-L92)）。
- `nnz` 是「已存储元素数」，`count_nonzero` 才是「真正非零数」，二者不一定相等（见 u1-l4、u2-l4）。
- 索引读写由 [_index.py](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_index.py#L25-L29) 的 `IndexMixin` 统一分发为 `_get_int/_get_slice/_get_array` 三类（见 u3-l2，本讲会顺带用到）。
- `_formats` 字典登记了每种格式的全称，例如 `'lil' -> "List of Lists"`、`'dia' -> "DIAgonal"`（见 [_base.py:36-45](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_base.py#L36-L45)），`__repr__` 用它把缩写翻译成人话。

本讲要回答的核心问题是：**COO 适合「批量三元组」构造，CSR/CSC 适合「运算」，那如果我想「一个元素一个元素地慢慢写」，或者矩阵天然是「带状」的，该用谁？** 答案就是 LIL、DOK、DIA 这三位。它们在源码里各有一个 `_*_base` 实现基类，外加 `*_array` / `*_matrix` 两个命名空间子类，结构与前几讲的格式完全对称。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [`_lil.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_lil.py#L20-L21) | LIL 格式实现 | `_lil_base` 的 `rows`/`data` 双列表、按行增量写入、`tocsr()` |
| [`_dok.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_dok.py#L20-L22) | DOK 格式实现 | `_dok_base` 继承 `dict`、`_dict` 字典存储、1-D/2-D 键、`toco()` |
| [`_dia.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_dia.py#L20-L21) | DIA 格式实现 | `_dia_base` 的 `offsets` + 二维 `data`、对角线对齐规则、`_data_mask` |
| [`_data.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_data.py#L20-L22) | 带 `.data` 属性的矩阵基类 | DIA 的父类 `_data_matrix`，及其 `_with_data` 契约 |
| [`_base.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_base.py#L85-L92) | 全格式公共基类 | `_spbase`、`_allow_nd` 默认值 |
| `sparsetools/`（C++/Cython 后端） | LIL 的快速插入与展平内核 | `lil_insert`/`lil_get1`/`lil_flatten_to_array` 等被 LIL 调用 |

一句话定位：**LIL 用「对象数组装 Python list」实现按行写入，DOK 用「字典」实现 O(1) 散点写入，DIA 用「对角线带」存储天然带状矩阵；三者都只擅长构造，算之前先转 CSR/CSC。**

## 4. 核心概念与源码讲解

### 4.1 LIL：按行存放的列表（rows/data）

#### 4.1.1 概念说明

LIL = **L**ist **o**f **L**ists（行的列表的列表）。它的存储原子不是「全局非零元列表」，而是**每一行各自维护一个有序列表**：

- `rows[i]`：第 `i` 行中**非零元素的列号**，按升序排列（一个 Python `list[int]`）。
- `data[i]`：与 `rows[i]` 一一对应的**值**（一个 Python `list[标量]`）。
- `rows` 和 `data` 本身都是长度为 `M`（行数）、`dtype=object` 的 NumPy 数组，每个槽位塞一个 Python list。

这种「按行分桶」的结构对**逐行、逐元素写入**非常友好：给 `(i, j)` 写一个值，只需要动第 `i` 行的那一个 list，不影响其它行。代价是它对**算术运算、列切片、矩阵-向量乘**都很慢——所以 LIL 的角色永远是「构造阶段的临时格式」，构造完立刻 `tocsr()`。

LIL 只支持 2 维（`_lil_base` 没有覆盖默认的 `_allow_nd = (2,)`，且构造器对非 2 维输入会直接报错）。

#### 4.1.2 核心流程

构造一个空 LIL，然后逐元素写入，再转 CSR 的整体流程：

```text
lil_array((M, N))            # __init__ 的「shape 元组」分支
        │
        ├── rows = object 数组, 长度 M, 每个元素 = []      # 空的按行桶
        ├── data = object 数组, 长度 M, 每个元素 = []
        ▼
L[i, j] = x                  # __setitem__ -> _set_intXint
        │
        └── _csparsetools.lil_insert(...)   # C++ 内核: 在 rows[i] 中保持升序插入 j
        ▼
A.tocsr()                    # 把 M 个 list 展平成 CSR 的 indptr/indices/data
        │
        ├── lil_get_lengths(rows) -> 每行长度
        ├── cumsum            -> indptr (长度 M+1)
        └── lil_flatten_to_array(rows/data) -> indices/data
```

写入单个元素的最坏时间复杂度是 \(O(\text{该行已存元素数})\)，因为 `lil_insert` 要在有序 list 里找到插入位置并后移元素。所以官方文档提醒：**若要高效构造，尽量让同一行内的写入按列号预先排好序**（见 [_lil.py:531-533](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_lil.py#L531-L533)）。

#### 4.1.3 源码精读

**类声明与格式标记**——`_lil_base` 继承 `_spbase` 和 `IndexMixin`，格式名标为 `'lil'`：

[_lil.py:20-21](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_lil.py#L20-L21) 定义 `_lil_base` 并设 `_format = 'lil'`，表明这是一个 LIL 格式实现。

**空 LIL 的构造（按行分桶的起点）**——当传入一个合法形状 `(M, N)` 时，建两个 object 数组，每行初始化为空 list：

[_lil.py:46-52](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_lil.py#L46-L52) 这几行就是「按行分桶」结构的诞生地：`self.rows[i] = []` 与 `self.data[i] = []`。后续所有逐元素写入都是往这两个 list 里塞值。

**逐元素写入（保持每行升序）**——`(int, int)` 索引走快速通道，最终落到 C++ 内核：

[_lil.py:277-299](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_lil.py#L277-L299) 的 `__setitem__` 先判断是不是简单的 `(int, int)`，是则把标量值交给 `_set_intXint`；

[_lil.py:261-263](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_lil.py#L261-L263) 的 `_set_intXint` 一行调用 `_csparsetools.lil_insert(...)`，由 C++ 内核在第 `row` 行的有序 list 里插入列号 `col` 与值 `x`。这就是「LIL 适合增量写」的底层原因——重活都在编译过的扩展里。

**单个元素的读取**——同样有 `(int, int)` 快速通道：

[_lil.py:174-187](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_lil.py#L174-L187)：`__getitem__` 对 `(int,int)` 直接调 `_get_intXint`，后者用 `_csparsetools.lil_get1` 在有序 list 里二分查找列号，找不到就返回 0。

**`nnz` 与按行统计**——`_getnnz` 把所有 `data[i]` 列表的长度加起来：

[_lil.py:95-110](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_lil.py#L95-L110)：`axis=None` 时返回 `sum(len(rowvals) for rowvals in self.data)`。注意 LIL 的 list 里可能存了显式零，所以这里的 `nnz` 同样可能大于 `count_nonzero`。

**`tocsr()`——把 list 展平成 CSR**——这是 LIL 最常见的「毕业动作」：

[_lil.py:414-444](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_lil.py#L414-L444)：先用 `lil_get_lengths` 取每行长度，`cumsum` 得到 `indptr`（长度 M+1），再用 `lil_flatten_to_array` 把 M 个 list 拍平成连续的 `indices` 和 `data`，最后交给 CSR 容器。注意当 \(M \times N\) 超过 int32 范围时会走 64 位索引分支（[_lil.py:428-436](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_lil.py#L428-L436)），这是 u6-l2 会讲的大规模索引话题。

> 小贴士：LIL 的算术运算（如 `_mul_scalar`，[_lil.py:301-312](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_lil.py#L301-L312)）是用 Python 层 `for` 循环逐行重建 list 实现的，所以官方明确建议「构造完就转 CSR/CSC」（[_lil.py:583-587](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_lil.py#L583-L587)）。

#### 4.1.4 代码实践

**实践目标**：亲手感受 LIL 的 `rows`/`data` 结构，并验证「逐元素写入 → tocsr」的链路。

**操作步骤**：

```python
import numpy as np
from scipy.sparse import lil_array

# 1) 建一个 3x4 的空 LIL
L = lil_array((3, 4), dtype=float)
print("rows =", L.rows.tolist())   # [[], [], []]
print("data =", L.data.tolist())   # [[], [], []]

# 2) 逐元素写入（注意：故意不按列号顺序写第 0 行）
L[0, 2] = 9.0
L[0, 0] = 1.0
L[2, 3] = 7.5

# 3) 观察 rows[0] 是否被自动排序
print("rows =", L.rows.tolist())   # 期望 [[0, 2], [], [3]]
print("data =", L.data.tolist())   # 期望 [[1.0, 9.0], [], [7.5]]
print("nnz  =", L.nnz)             # 期望 3

# 4) 转 CSR，验证结果
C = L.tocsr()
print(C.toarray())
```

**需要观察的现象**：写入 `L[0,2]` 再写 `L[0,0]` 后，`rows[0]` 应当是 `[0, 2]` 而非 `[2, 0]`——说明 `lil_insert` 始终保持每行升序。

**预期结果**：`C.toarray()` 还原出 `[[1,0,9,0],[0,0,0,0],[0,0,0,7.5]]`，三处非零值位置正确。

**若无法确定运行结果**：上述输出为「待本地验证」，但 `rows[0]` 保持升序这一点由 [_lil.py:261-263](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_lil.py#L261-L263) 调用的 `lil_insert` 内核保证。

#### 4.1.5 小练习与答案

**练习 1**：为什么 LIL 的列切片 `L[:, [1,3]]` 比 `L[[0,2], :]` 慢？

> **参考答案**：LIL 按行分桶，取若干整行只需访问对应行的 list（快）；而取列要在每一行的有序 list 里查找指定列号是否存在（慢）。官方在 [_lil.py:579-580](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_lil.py#L579-L580) 明确列出 "slow column slicing (consider CSC)"。

**练习 2**：把上一节代码里第 0 行的两次写入改成「先 `L[0,0]=1.0` 再 `L[0,2]=9.0`」（即天然有序），想一想为什么官方建议按列号预排序来构造。

> **参考答案**：`lil_insert` 在有序 list 末尾追加是 \(O(1)\)，在中间插入是 \(O(\text{行内元素数})\)。按列号预排序能让绝大多数写入落在末尾，把整体构造从 \(O(\text{nnz}^2)\) 量级压到接近 \(O(\text{nnz})\)。

### 4.2 DOK：键为坐标的字典（继承 dict）

#### 4.2.1 概念说明

DOK = **D**ictionary **O**f **K**eys（键为坐标的字典）。它的存储思想极其朴素：**用一个 Python 字典，把每个非零元的坐标当键、值当 value**。

- 二维时键是 `(row, col)` 元组，例如 `{(0,0): 1.0, (2,3): 7.5}`。
- 一维时键是单个整数，例如 `{0: 1.0, 3: 7.5}`——这是 DOK 区别于 LIL 的一个特性：**DOK 同时支持 1 维和 2 维**（`_allow_nd = (1, 2)`）。

字典的期望查找/插入时间是 \(O(1)\)，所以 DOK 对**散点式随机读写单个元素**很快，这正是它和 LIL 的分工：LIL 强在「按行顺序写」，DOK 强在「任意位置 O(1) 读写」。

源码里有两个容易看走眼的细节，本讲专门点破：

1. **真正的数据存在 `self._dict` 里**，而不是字典实例本身。
2. **类声明里确实写了 `dict` 作为基类**（`class _dok_base(_spbase, IndexMixin, dict)`），所以 DOK 对象「是一个 dict」，但实例本体基本是空的，各种 dict 方法（`keys/items/values/get/pop/__len__`…）都被改写去转发给 `self._dict`。

#### 4.2.2 核心流程

DOK 的「构造 → 散点写 → 转 COO」流程：

```text
dok_array((M, N))           # __init__ 的 shape 分支
        │
        ├── self._dict = {}            # 真正存数据的地方
        ▼
D[i, j] = x                 # __setitem__ -> _set_intXint
        │
        ├── 若 x 为真:  self._dict[(i,j)] = x
        └── 若 x 为 0:  del self._dict[(i,j)]   # 关键: DOK 永不存零!
        ▼
D.tocoo()                   # 把 _dict 的 keys/values 拆成 coords + data
```

最值得记住的一条不变量：**DOK 在写入零时会主动删除键**（见下方源码），因此 DOK 的 `nnz == count_nonzero` 恒成立，它永远不会像 CSR/COO 那样携带「显式零」。

#### 4.2.3 源码精读

**类声明——同时继承 `_spbase`、`IndexMixin` 和 `dict`**：

[_dok.py:20-22](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_dok.py#L20-L22)：`class _dok_base(_spbase, IndexMixin, dict)`，并把 `_allow_nd` 放宽到 `(1, 2)`，这是 DOK 能装一维数组的依据。继承 `dict` 主要是为了对外提供映射协议（`keys/items/values/get/pop`…）以及兼容 pickle（见下方 `__reduce__`）。

**真正的数据仓库 `_dict`**——构造时初始化：

[_dok.py:27-30](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_dok.py#L27-L30)（shape 分支）把 `self._dict = {}`；从稠密一维数组构造时则用字典推导 `{i: v for i, v in enumerate(arg1) if v != 0}`（[_dok.py:55](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_dok.py#L55)），同样只存非零。

**单个元素的读写（O(1) 散点访问）**：

[_dok.py:300-301](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_dok.py#L300-L301)：`_get_intXint` 直接 `self._dict.get((row, col), self.dtype.type(0))`——一次哈希查找，找不到就返回零。

[_dok.py:393-398](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_dok.py#L393-L398)：`_set_intXint` 是上面那条「DOK 永不存零」不变量的来源——`if x: self._dict[key] = x elif key in self._dict: del self._dict[key]`。

**映射协议全部转发给 `_dict`**——这就是「继承 dict 但数据在 `_dict`」的体现：

[_dok.py:115-119](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_dok.py#L115-L119) 的 `__len__`/`__contains__`、[_dok.py:205-236](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_dok.py#L205-L236) 的 `items/keys/values`、[_dok.py:238-269](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_dok.py#L238-L269) 的 `get`，无一例外都委托 `self._dict`。`get` 的默认值是 `0.0`（而非 `None`），呼应「未存的元素视为零」。

**`nnz` 就是字典长度**：

[_dok.py:100-105](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_dok.py#L100-L105)：`_getnnz` 返回 `len(self._dict)`。配合「写零删键」，DOK 的 `nnz` 始终等于真实非零数。

**`tocoo()`——DOK 最自然的出口**：

[_dok.py:568-580](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_dok.py#L568-L580)：把 `values()` 拼成 `data`，把 `keys()` 拆成坐标数组（二维时 `zip(*self.keys())` 得到 row/col 两组），并设 `has_canonical_format = True`（因为字典键天然无重复）。DOK 没有 `tocsr` 的直接实现，而是经 COO 中转（[_dok.py:591-594](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_dok.py#L591-L594) 的 `tocsc` 就是 `tocoo().tocsc()`）。

**关于 pickle 的小彩蛋**：

[_dok.py:519-523](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_dok.py#L519-L523) 的 `__reduce__` 显式 `return dict.__reduce__(self)`——这是 DOK 保留 `dict` 基类的现实原因之一：让序列化走 dict 的成熟路径。

#### 4.2.4 代码实践

**实践目标**：验证 DOK「写零删键」的不变量，以及 O(1) 散点写入。

**操作步骤**：

```python
from scipy.sparse import dok_array

D = dok_array((4, 4), dtype=float)
D[1, 2] = 3.0
D[3, 0] = 5.0
print("keys after writes:", list(D.keys()))   # [(1,2), (3,0)]
print("nnz =", D.nnz)                         # 2

# 故意写一个零，看它会不会被存进去
D[0, 0] = 0.0
print("nnz after zero write =", D.nnz)        # 期望仍是 2
print("(0,0) in D =", (0, 0) in D)            # 期望 False

# DOK 支持 1 维
d1 = dok_array((5,))
d1[1] = 9.0
d1[4] = 8.0
print("1D keys:", list(d1.keys()))            # [1, 4]（整数键，非元组）
print("d1.tocoo().toarray() =", d1.tocoo().toarray())   # [0,9,0,0,8]
```

**需要观察的现象**：`D[0,0]=0.0` 之后 `nnz` 不增加、`(0,0) in D` 为 `False`——印证「DOK 永不存零」。

**预期结果**：如注释所示。`nnz` 恒等于真实非零数。

**若无法确定运行结果**：键的打印顺序取决于字典迭代顺序（CPython 3.7+ 保持插入序），具体顺序「待本地验证」，但键的**集合内容**是确定的。

#### 4.2.5 小练习与答案

**练习 1**：既然 DOK 继承了 `dict`，为什么数据不直接存在实例本身（像普通 dict 那样 `self[(i,j)] = v`），而要另开一个 `self._dict`？

> **参考答案**：把数据集中放在 `_dict`，可以让 DOK 的坐标键、形状校验、dtype 管理与 `_spbase` 的属性体系解耦；同时 `get` 的默认值是 `0`（而非 dict 的 `None`），`__setitem__` 要拦截零值删键，这些行为都与原生 dict 不同，统一在 `_dict` 上实现更清晰。继承 `dict` 主要是为了对外暴露映射协议和兼容 pickle，而非真的把实例当容器用。

**练习 2**：同样要写入 1000 个散点，DOK 和 LIL 谁更快？写出后转 CSR 呢？

> **参考答案**：纯散点写入 DOK 通常更快，因为 dict 是 \(O(1)\) 平均，而 LIL 要在每行有序 list 里维护排序（最坏 \(O(\text{行内元素数})\)）。但两者的算术运算都慢，转成 CSR 后再做矩阵-向量乘，DOK/LIL 之间的差异就不再重要了——所以选 DOK 还是 LIL 主要看「写入模式是散点还是按行」。

### 4.3 DIA：按对角线存储（offsets/data）

#### 4.3.1 概念说明

DIA = **DIA**gonal（对角线存储）。很多数值问题里的矩阵是**带状**的：非零元只集中在主对角线及其邻近几条对角线上（典型如有限差分离散得到的二阶导算子，即三对角矩阵）。对这类矩阵，与其用 CSR 存一堆零碎的行，不如**直接按对角线存**。

DIA 用两个数组：

- `offsets`：一维整数数组，记录「存了哪几条对角线」。`offsets[k] = 0` 是主对角线，正值是上对角线（超对角线），负值是下对角线（子对角线）。
- `data`：二维数组，`data[k, :]` 存第 `k` 条对角线上的元素。

关键的对齐规则（出自官方文档字符串，[_dia.py:568-576](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_dia.py#L568-L576)）：

- 主对角线（offset=0）：从 `data` 的第 0 列开始放。
- 上对角线（offset>0）：右对齐（左边补零）。
- 下对角线（offset<0）：左对齐（右边补零）。

数学上，对角线 `offsets[i]` 上的元素，矩阵里位于第 `r` 行第 `c` 列时，存放在 `data[i, c - max(0, -offsets[i])]`（[_dia.py:580-583](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_dia.py#L580-L583)）。

DIA 只支持 2 维，且**不支持切片**（[_dia.py:564](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_dia.py#L564)）。它还有一个值得记住的彩蛋：**当 `offsets` 按降序给出时，DIA 的内存布局正好等价于 BLAS/LAPACK 的带状矩阵格式（如 `dgbmv` 用的格式）**（[_dia.py:585-586](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_dia.py#L585-L586)）。

#### 4.3.2 核心流程

用 `(data, offsets)` 直接构造一个三对角矩阵的流程（这是官方文档示例，[_dia.py:606-618](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_dia.py#L606-L618)）：

```text
n = 10
ex = np.ones(n)
data    = np.array([ex, 2*ex, ex])   # 3 行 x n 列
offsets = np.array([-1, 0, 1])        # 子/主/超 三条对角线
        │
        ▼  dia_array((data, offsets), shape=(n, n))
        │
        ├── self.data    = atleast_2d(data)    # 形状 (3, n)
        ├── self.offsets = atleast_1d(offsets) # 形状 (3,)
        └── 校验: offsets 1 维、data 2 维、data 行数==len(offsets)、offsets 无重复
```

DIA 的每条对角线长度不一定等于矩阵维度：上下对角线在矩阵里的有效长度短一些。第 `k` 条对角线在 \(M \times N\) 矩阵里的有效元素数为：

\[
\ell_k = \min(N,\; M+k) - \max(0,\; k), \quad \ell_k \ge 0
\]

对三对角 \(n \times n\) 矩阵（offsets = \(-1, 0, 1\)）有 \(\ell_{-1}=n-1,\ \ell_{0}=n,\ \ell_{1}=n-1\)，总 nnz \(=3n-2\)。`_getnnz` 就是把每条对角线的有效长度求和（[_dia.py:131-139](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_dia.py#L131-L139)）。

#### 4.3.3 源码精读

**类声明——DIA 继承的是 `_data_matrix` 而非 `_spbase`**：

[_dia.py:20-21](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_dia.py#L20-L21)：`class _dia_base(_data_matrix)`。`_data_matrix`（[_data.py:20-26](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_data.py#L20-L26)）是「带 `.data` 属性的矩阵」基类，它把 `dtype` 定义成 `self.data.dtype` 的 property，并提供 `__abs__`/`astype` 等基于 `.data` 的实现（[_data.py:32-41](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_data.py#L32-L41)）。这就是 DIA 能直接 `abs()`、能标量乘的原因。

**(data, offsets) 构造分支**：

[_dia.py:49-67](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_dia.py#L49-L67)：把元组拆成 `data, offsets`，要求同时给 `shape`；`data` 经 `np.atleast_2d` 保证二维，`offsets` 经 `atleast_1d` 保证一维，索引 dtype 用 `_get_index_dtype(maxval=max(shape))` 选择。

**四条格式不变量校验**：

[_dia.py:86-99](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_dia.py#L86-L99) 依次检查：`offsets` 必须一维、`data` 必须二维、`data.shape[0] == len(offsets)`（对角线条数要匹配）、`offsets` 不得有重复值。这些是 DIA 格式正确性的硬约束。

**`_data_mask`——算出哪些 data 元素落在矩阵范围内**：

[_dia.py:110-119](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_dia.py#L110-L119)：因为上下对角线会带「越界的填充零」，需要用掩码标出 `data` 中真正对应矩阵位置的那些元素。`count_nonzero`（[_dia.py:121-127](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_dia.py#L121-L127)）和按轴求和都依赖它。

**标量乘法只换 data 不动 offsets**：

[_dia.py:232-233](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_dia.py#L232-L233)：`_mul_scalar` 直接 `return self._with_data(self.data * other)`——`offsets` 完全不变，因为标量乘不改变稀疏结构。`_with_data` 的实现在 [_dia.py:442-454](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_dia.py#L442-L454)，这是 `_data_matrix` 要求子类必须实现的「换数据保结构」契约。

**矩阵-向量乘用 C++ 内核 `dia_matvec`**：

[_dia.py:286-299](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_dia.py#L286-L299)：尽管 DIA 主要用于构造，它的 SpMV 仍然走编译过的 `dia_matvec`（从 `_sparsetools` 导入，[_dia.py:17](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_dia.py#L17)），所以带状矩阵的 SpMV 在 DIA 下其实很快。

**`tocsr()`——DIA 转 CSR 会顺手清掉显式零**：

[_dia.py:411-438](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_dia.py#L411-L438)：调用 C++ 内核 `dia_tocsr`，返回真实 nnz（注释明确说 "eliminates explicit zeros"，[_dia.py:426](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_dia.py#L426)），并把 `has_canonical_format` 置为 `True`。

#### 4.3.4 代码实践

**实践目标**：用 `(data, offsets)` 直接构造三对角矩阵，并理解对齐规则。

**操作步骤**（沿用官方文档示例）：

```python
import numpy as np
from scipy.sparse import dia_array

n = 5
ex = np.ones(n)
# 第 0 行: 子对角线=1; 第 1 行: 主对角线=2; 第 2 行: 超对角线=1
data = np.array([ex, 2 * ex, ex])
offsets = np.array([-1, 0, 1])

A = dia_array((data, offsets), shape=(n, n))
print(A.toarray())
# 期望: 主对角线全是 2, 上下相邻对角线全是 1, 其余为 0
print("nnz =", A.nnz)             # 期望 3*n - 2 = 13
print("offsets =", A.offsets)     # [-1, 0, 1]
print("data.shape =", A.data.shape)  # (3, 5)

# 转成 CSR 做运算
C = A.tocsr()
x = np.arange(n)
print("A @ x =", A @ x)           # 用 DIA 直接算
print("C @ x =", C @ x)           # 用 CSR 算, 结果应一致
```

**需要观察的现象**：`toarray()` 显示标准的二阶差分算子形态（三对角）；`A @ x` 与 `C @ x` 完全相等，说明格式转换不改变数值。

**预期结果**：`nnz = 13 = 3*5 - 2`，与公式 \(\sum \ell_k = (n-1)+n+(n-1) = 3n-2\) 一致。

**若无法确定运行结果**：具体数组元素「待本地验证」，但 `nnz` 与公式吻合是确定的。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `offsets` 不允许有重复值？

> **参考答案**：每条对角线在 `data` 里占一行，重复的 offset 意味着同一条对角线被表示两次，存储与 `_data_mask`/`tocsr` 的索引都会歧义。[_dia.py:98-99](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_dia.py#L98-L99) 明确拒绝重复。

**练习 2**：把 `offsets` 改成降序 `np.array([1, 0, -1])`，矩阵 `toarray()` 的结果会变吗？为什么官方提到这会匹配 BLAS 带状格式？

> **参考答案**：`toarray()` 结果不变，因为 DIA 按对角线存值，与对角线在 `offsets` 里的排列顺序无关（`offsets` 只是个集合，每行 `data` 配一个 offset）。但当 `offsets` 降序时，`data` 的行排列与 BLAS/LAPACK 带状格式（如 `dgbmv` 的 `AB`）一致，便于与那些 Fortran 例程互操作（[_dia.py:585-586](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_dia.py#L585-L586)）。

## 5. 综合实践

把三种格式串起来，做一次「同款矩阵、三种造法、统一校验」的对比。目标矩阵：一个 \(1000 \times 1000\) 的三对角矩阵（主对角线 2、上下次对角线 1）。

```python
import time
import numpy as np
from scipy.sparse import lil_array, dok_array, dia_array

n = 1000
# 造法 A: DIA（最贴合结构）
ex = np.ones(n)
t0 = time.perf_counter()
A_dia = dia_array((np.array([ex, 2*ex, ex]), np.array([-1, 0, 1])), shape=(n, n))
t_dia = time.perf_counter() - t0

# 造法 B: DOK 散点写入（O(1) 单点）
t0 = time.perf_counter()
A_dok = dok_array((n, n))
for i in range(n):
    A_dok[i, i] = 2.0
    if i + 1 < n:
        A_dok[i, i+1] = 1.0
        A_dok[i+1, i] = 1.0
t_dok = time.perf_counter() - t0

# 造法 C: LIL 按行写入（天然按列号有序，最高效的 LIL 用法）
t0 = time.perf_counter()
A_lil = lil_array((n, n))
for i in range(n):
    if i - 1 >= 0:
        A_lil[i, i-1] = 1.0
    A_lil[i, i] = 2.0
    if i + 1 < n:
        A_lil[i, i+1] = 1.0
t_lil = time.perf_counter() - t0

print(f"DIA build : {t_dia:.4f}s")
print(f"DOK build : {t_dok:.4f}s")
print(f"LIL build : {t_lil:.4f}s")

# 统一转 CSR 后逐元素比较
C_dia = A_dia.tocsr()
C_dok = A_dok.tocsr()
C_lil = A_lil.tocsr()
print("DIA == DOK :", (C_dia != C_dok).nnz == 0)   # 期望 True
print("DIA == LIL :", (C_dia != C_lil).nnz == 0)   # 期望 True
print("三者的 nnz :", C_dia.nnz, C_dok.nnz, C_lil.nnz)  # 期望都 = 3n - 2 = 2998
```

**讨论要点**（请结合实际跑出来的时间思考，精确数值「待本地验证」）：

1. **DIA 应当碾压式最快**——它一次性把整条对角线塞进去，O(对角线条数) 而非 O(nnz)。
2. **DOK 与 LIL 的对比取决于写入模式**：这里两者都是循环写，DOK 的 dict 单点写入通常略快；但若把 LIL 的写入改成天然有序（如上面先 `i-1` 再 `i` 再 `i+1`，已是有序），差距会缩小。
3. **三者 `tocsr()` 后应当完全相等**——这验证了「格式只影响构造效率，不影响数值」这条贯穿全讲的结论。
4. **延伸思考**：如果矩阵不是规整的带状，而是随机散点，DIA 就完全失效（每条对角线只有零星元素，`data` 里全是填充零，浪费巨大）——这正好说明「DIA 只为带状结构而生」。

## 6. 本讲小结

- **LIL** 用 `rows`/`data` 两个 object 数组（每行一个有序 Python list）实现**按行增量写入**，适合「逐行、按列号有序」地搭骨架；算术、列切片、SpMV 都慢，构造完要 `tocsr()`。
- **DOK** 用 `self._dict` 存 `(row,col)->val`（1 维时键是 int），类继承 `dict` 仅为提供映射协议与兼容 pickle，真正数据在 `_dict`；**写零自动删键**，故 `nnz == count_nonzero` 恒成立；适合散点 O(1) 读写，支持 1-D/2-D。
- **DIA** 用 `offsets`（对角线编号）+ 二维 `data`（每行一条对角线）按对角线存储，**专为带状矩阵设计**；上下对角线靠填充零对齐，`_data_mask` 用来标出有效位置；不支持切片，降序 offsets 时等价于 BLAS 带状格式。
- 三者都是**构造型格式**：定位是「先把矩阵搭起来」，算之前一律转 CSR/CSC（DOK 通常先 `tocoo()`）。
- 三种格式的 `nnz` 语义有别：DOK 不存零；LIL 的 list 可能含显式零；DIA 的 `nnz` 只计对角线有效长度，`data` 内仍可能有显式零（`tocsr` 时才会被清掉）。
- 选型口诀：**散点随机写用 DOK，按行顺序写用 LIL，天然带状用 DIA，三者建好都转 CSR/CSC 再算。**

## 7. 下一步学习建议

- 想搞清楚 `tocsr()`/`tocoo()` 这类格式互转背后的复杂度与「去重 / 消零 / canonical format」不变量，请进入 **u6-l1（格式转换、去重与 canonical format）**。
- 想了解 LIL/DOK 写入时为什么有时会触发 `SparseEfficiencyWarning`、以及如何为不同访问模式选格式，请看 **u6-l5（二次开发：扩展点与最佳实践）**。
- 想看 LIL 调用的那些 `lil_insert`/`lil_get1`/`lil_flatten_to_array` C++ 内核是怎么被生成和编译出来的，请回顾 **u1-l3（构建系统）** 与 **u3-l6（sparsetools 代码生成）**。
- 建议随手阅读三个格式类的文档字符串（[_lil.py:526-595](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_lil.py#L526-L595)、[_dok.py:674-730](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_dok.py#L674-L730)、[_dia.py:517-619](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_dia.py#L517-L619)），里面官方已经把优缺点和适用场景列得很清楚。
