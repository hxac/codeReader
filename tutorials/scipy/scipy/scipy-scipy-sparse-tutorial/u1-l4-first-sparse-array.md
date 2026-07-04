# 动手：第一个稀疏数组与基本操作

## 1. 本讲目标

本讲是整个学习手册的**第一次真正动手**。前面三讲我们认识了「为什么要稀疏」「目录怎么分层」「怎么构建」，本讲要让你在 Python 里真正写出第一段稀疏数组代码。

读完本讲，你应该能够：

- 用 `coo_array`、`csr_array` 从**稠密数组**、**坐标三元组** `(data,(I,J))`、**形状** `(M,N)` 三类常见输入构造稀疏数组；
- 读懂 `_coo_base.__init__` 是如何「分发」这几类输入的，并能据此推断任意输入会走哪条分支；
- 理解 `nnz`、`data`、`coords`、`indices`、`indptr` 这几个核心属性分别存了什么；
- 用 `@` 做矩阵-向量乘，并解释**为什么不要直接把稀疏数组丢给 NumPy 函数**（如 `np.sin(A)`）。

本讲只覆盖**七种格式中最常用的两种**：COO（用来构造）和 CSR（用来运算）。其余五种格式有专门讲义。

---

## 2. 前置知识

本讲默认你已经读完 [u1-l1 项目定位与稀疏存储思想](u1-l1-sparse-overview.md)，至少理解以下概念：

- **稀疏数组**：只显式存储非零元素及其位置，零是「隐式」的。`nnz` 表示「已存储元素个数」（含显式零），区别于真正非零的个数 `count_nonzero`。
- **COO（坐标/三元组）格式**：用 `(data, (行坐标, 列坐标))` 描述每个非零元，最擅长组装；允许重复坐标。
- **CSR（压缩稀疏行）格式**：用 `indptr/indices/data` 三数组紧凑存储，最擅长矩阵-向量乘。
- **`sparray`（新）与 `spmatrix`（待弃用）**：本讲统一用 `*_array` 接口，不用 `*_matrix`。

> 一个关键直觉：**COO 用来「造」，CSR 用来「算」**。这是 scipy.sparse 设计的一条主线，本讲的所有操作都围绕它展开。

本讲运行环境假设：`import numpy as np` 和 `from scipy import sparse`（或 `from scipy.sparse import coo_array, csr_array`）可用。所有命令**未在讲义中实际执行**，涉及具体输出处会标注「预期结果」或「待本地验证」。

---

## 3. 本讲源码地图

本讲涉及的源码文件都位于 `scipy/sparse/` 目录下：

| 文件 | 本讲中的作用 |
| --- | --- |
| `__init__.py` | 子包入口。顶部文档字符串里有现成的 `coo_array` / `csr_array` 示例，是本讲代码实践的「官方对照」。 |
| `_coo.py` | COO 格式的全部实现：`coo_array` 类与 `_coo_base.__init__` 的输入分发逻辑（本讲源码精读的重点）。 |
| `_csr.py` | CSR 格式实现：`csr_array` 类定义与 `_csr_base`。 |
| `_compressed.py` | CSR/CSC 的公共基类 `_cs_matrix`，包含 CSR 的构造器与 `_matmul_vector`（`@` 的真正内核）。 |
| `_base.py` | 所有稀疏类的总基类 `_spbase`，定义了 `nnz` 属性与 `__matmul__`（`@` 运算符入口）。 |

记住上一讲的口诀「实现看 `_xxx.py`、公开看 `__init__.py`」：`coo_array`、`csr_array` 这两个名字能从顶层 `scipy.sparse` 直接用，是因为 [`__init__.py`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/__init__.py#L250-L262) 里用 `from ._coo import *` / `from ._csr import *` 把它们聚合到了顶层命名空间。

---

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：

- **4.1 构造稀疏数组的四种输入方式**——`coo_array` / `csr_array` 能吃什么；
- **4.2 `_coo_base.__init__` 的分支分发**——它怎么识别你的输入（含主实践：观察重复坐标求和）；
- **4.3 核心属性：nnz、data、coords、indices**——稀疏数组的「身份证」；
- **4.4 矩阵-向量乘 `@` 与 `toarray`**——以及为什么不要直接套用 NumPy 函数。

### 4.1 构造稀疏数组的四种输入方式

#### 4.1.1 概念说明

`coo_array` 和 `csr_array` 都是**类**，调用它们就是「构造一个稀疏数组对象」。它们的构造器签名一致：

```python
coo_array(arg1, shape=None, dtype=None, copy=False)
csr_array(arg1, shape=None, dtype=None, copy=False)
```

关键在于第一个参数 `arg1` 是什么。同一个类，`arg1` 不同，构造方式就完全不同。文档里列出了 `coo_array` 的几种构造方式（见 [`_coo.py` 的类文档](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py#L1700-L1717)）：

1. **稠密数组**：`coo_array(D)`，`D` 是 `np.ndarray`；
2. **另一个稀疏数组**：`coo_array(S)`，等价于 `S.tocoo()`；
3. **形状**：`coo_array((M, N))`，构造一个全空的稀疏数组；
4. **三元组**：`coo_array((data, coords), shape=(M, N))`，用数据和坐标数组构造。

`csr_array` 多一种原生输入：`csr_array((data, indices, indptr), shape=(M, N))`，即直接给出 CSR 的三数组（见 [`_csr.py` 的类文档](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_csr.py#L337-L357)）。

#### 4.1.2 核心流程

不论哪种格式，构造的总体流程是：

```
输入 arg1
   │
   ├── 是元组(tuple)?
   │     ├── 元组本身就是形状 (M,N)  → 造一个空数组
   │     └── 否则当 (data, 坐标) 处理 → 解析数据和坐标
   │
   ├── 是稀疏数组(issparse)?        → 转成当前格式（如 S.tocoo()）
   │
   └── 其它                         → 当稠密数组处理（取 np.asarray + nonzero()）
```

注意第 4 步「三元组」构造时，**坐标不需要排序，也允许重复**——这是 COO 相对 CSR 的最大便利，也是有限元刚度矩阵组装的关键（后文实践会用到）。

#### 4.1.3 源码精读

`coo_array` 和 `csr_array` 都只是一个「壳」，真正干活的是它们的基类：

- `coo_array(_coo_base, sparray)` 见 [_coo.py:L1694](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py#L1694)：多重继承 `_coo_base`（实现）和 `sparray`（数组语义标记）。
- `csr_array(_csr_base, sparray)` 见 [_csr.py:L333](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_csr.py#L333)：同理继承 `_csr_base`。
- `_csr_base(_cs_matrix)` 见 [_csr.py:L18](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_csr.py#L18)，进一步继承公共基类 `_cs_matrix`（定义在 `_compressed.py`）。

所以：`coo_array` 的构造逻辑在 `_coo_base.__init__`，`csr_array` 的构造逻辑在 `_cs_matrix.__init__`。

`_coo_base` 用两个类属性标识自己的身份（见 [_coo.py:L28-L30](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py#L28-L30)）：

```python
class _coo_base(_data_matrix, _minmax_mixin):
    _format = 'coo'
    _allow_nd = tuple(range(1, 65))
```

- `_format = 'coo'`：格式名，`__repr__`、`asformat`、以及后文 `@` 里都靠它分派；
- `_allow_nd = range(1,65)`：COO 允许 1~64 维（所以 COO 能存 1-D 稀疏数组）。对比 `_csr_base._allow_nd = (1, 2)`（见 [_csr.py:L20](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_csr.py#L20)），CSR 只接受 1-D 或 2-D。

#### 4.1.4 代码实践

**实践目标**：用三种最常见的方式构造同一个 2×3 矩阵，验证它们表示的是同一个东西。

**操作步骤**：

```python
import numpy as np
from scipy.sparse import coo_array, csr_array

target = np.array([[0, 2, 0],
                   [5, 0, 1]])

# 方式 A：从稠密数组
A = coo_array(target)

# 方式 B：从坐标三元组 (data, (I, J))
#   target 中非零位置：(0,1)=2, (1,0)=5, (1,2)=1
I = [0, 1, 1]
J = [1, 0, 2]
V = [2, 5, 1]
B = coo_array((V, (I, J)), shape=(2, 3))

# 方式 C：从形状（造空的，再换格式）
C = csr_array((2, 3))          # 先造一个全空的 CSR

print(A.toarray())             # 应与 target 一致
print(B.toarray())             # 应与 target 一致（坐标顺序无所谓）
print(C.nnz)                   # 0，因为只给了形状
```

**需要观察的现象**：
- `A.toarray()`、`B.toarray()` 都应还原出 `target`；
- 方式 B 的坐标 `(I,J)` 故意按「乱序」给出（(0,1) 在 (1,0) 之前但不是按行排列），还原结果不受影响——这就是 COO「坐标无需排序」的特性。

**预期结果**：三个构造方式得到的 `toarray()` 都等于 `[[0,2,0],[5,0,1]]`，`C.nnz == 0`。

#### 4.1.5 小练习与答案

**练习 1**：下面哪种构造方式会报错？
(a) `coo_array((3, 4))`  (b) `coo_array(([1,2], ([0,1],[0,1])))`  (c) `csr_array((5,5,5))`

**答案**：(c) 报错。`csr_array` 的 `_allow_nd = (1,2)`，三元组 `(5,5,5)` 会被当成形状，但 3 维形状不被 CSR 接受（COO 才支持）。

**练习 2**：`coo_array([[0,0],[0,0]])` 得到的对象 `nnz` 是多少？

**答案**：`0`。从稠密构造时走的是 `M.nonzero()`，全零矩阵没有任何非零位置，于是 `data` 为空数组。

---

### 4.2 `_coo_base.__init__` 的分支分发

#### 4.2.1 概念说明

4.1 我们看到了「能吃什么」，本模块回答「**它是怎么认出来的**」。理解 `_coo_base.__init__` 的分支分发，能让你在遇到诡异报错时一眼定位问题（比如「为什么我传的元组被当成形状了」）。

`__init__` 做的事情本质上是**根据 `arg1` 的类型，把输入统一翻译成 COO 的内部表示 `self.coords + self.data + self._shape`**。翻译完成后，无论你用哪种方式构造，对象的内部结构都一样。

#### 4.2.2 核心流程

`_coo_base.__init__`（[_coo.py:L32](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py#L32)）的三层判断：

```
if isinstance(arg1, tuple):          # 输入是元组
    if isshape(arg1):                 # (a) 元组本身是形状 → 空数组
    else:                             # (b) 当作 (data, coords)
else:
    if issparse(arg1):                # (c) 从另一个稀疏数组转来
    else:                             # (d) 稠密数组
```

注意一个**初学者常踩的坑**：`coo_array((5, 5))` 走分支，而 `coo_array(([1,2], (5,5)))` 中第二个参数 `(5,5)` 也会被 `isshape` 判断为形状——这就是为什么「只给两个标量的元组」永远被当成形状，而不是「两个坐标」。

#### 4.2.3 源码精读

下面是 `__init__` 的精简骨架（[_coo.py:L32-L103](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py#L32-L103)），逐段看：

**(a) 元组是形状 → 空数组**（[_coo.py:L38-L45](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py#L38-L45)）：

```python
if isshape(arg1, allow_nd=self._allow_nd):
    self._shape = check_shape(arg1, allow_nd=self._allow_nd)
    ...
    self.coords = tuple(np.array([], dtype=idx_dtype) for _ in range(len(self._shape)))
    self.data = np.array([], dtype=data_dtype)
    self.has_canonical_format = True
```

这段说明：空数组的 `coords` 是「每个维度一个空数组」，`data` 也是空数组。

**(b) 元组是 (data, coords) → 三元组**（[_coo.py:L46-L65](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py#L46-L65)）：

```python
obj, coords = arg1            # obj 是 data，coords 是坐标元组
...
self.coords = tuple(np.array(idx, copy=copy, dtype=idx_dtype) for idx in coords)
self.data = getdata(obj, copy=copy, dtype=dtype)
self.has_canonical_format = False   # 三元组构造时默认「未规范化」
```

两个关键点：
- **`has_canonical_format = False`**：COO 不保证坐标有序、也不去重，因此三元组构造后这个标志默认为 `False`。后文 `tocsr()` 会据此决定是否求和去重。
- **形状可省略**：如果没给 `shape`，代码会用 `np.max(idx)+1` 推断（见 [_coo.py:L52-L57](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py#L52-L57)），即「每个坐标维度的最大值 +1」。

**(d) 稠密数组**（[_coo.py:L79-L98](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py#L79-L98)）：

```python
M = np.asarray(arg1)
...
coords = M.nonzero()                       # 直接取非零位置
self.coords = tuple(idx.astype(index_dtype, copy=False) for idx in coords)
self.data = getdata(M[coords], copy=copy, dtype=dtype)
self.has_canonical_format = True           # 从稠密来：天然有序无重复
```

从稠密构造时用 `np.nonzero()` 拿到所有非零坐标，天然无重复且按数组遍历顺序，所以 `has_canonical_format = True`。

> 对比 CSR：`csr_array` 的稠密构造会**先转成 COO 再压缩**（见 [`_compressed.py` 的构造器](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_compressed.py#L84-L98)）。这就是上一讲说的「COO 是构造的主力，其它格式在底层借它」。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：用坐标数组 `I,J,V` 构造 4×4 的 `coo_array`，再 `tocsr()` 观察重复 `(i,j)` 被求和，并与官方文档 Example 2 对照，记录每一步 `nnz` 的变化。

**操作步骤**（直接来自 [`__init__.py` 的 Example 2](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/__init__.py#L210-L231)）：

```python
from scipy import sparse
from numpy import array

I = array([0,0,1,3,1,0,0])
J = array([0,2,1,3,1,0,0])
V = array([1,1,1,1,1,1,1])

A = sparse.coo_array((V,(I,J)), shape=(4,4))   # 第 1 步：COO 构造
print("COO nnz :", A.nnz)                       # 第 2 步：观察 nnz

B = A.tocsr()                                   # 第 3 步：转 CSR（触发求和）
print("CSR nnz :", B.nnz)
print(B.toarray())
```

**每一步 nnz 变化**（关键观察点）：

| 步骤 | 操作 | `nnz` | `has_canonical_format` | 说明 |
| --- | --- | --- | --- | --- |
| 第 1 步 | `coo_array((V,(I,J)),shape=(4,4))` | **7** | `False` | COO 把 7 个三元组原样存下，**不**去重、**不**求和 |
| 第 3 步 | `A.tocsr()` | **4** | `True` | 转 CSR 时把重复坐标求和，剩下 4 个唯一非零 |

**为什么是 7 → 4？** 7 个三元组里 `(0,0)` 出现 3 次、`(1,1)` 出现 2 次：

- `(0,0)`：值 1+1+1 = **3**
- `(0,2)`：值 **1**
- `(1,1)`：值 1+1 = **2**
- `(3,3)`：值 **1**

求和后矩阵应为（与文档一致）：

```
[[3, 0, 1, 0],
 [0, 2, 0, 0],
 [0, 0, 0, 0],
 [0, 0, 0, 1]]
```

**求和发生在哪里？** 在 `_coo_base.tocsr`（[_coo.py:L368-L412](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py#L368-L412)）里，关键两行：

```python
x = self._csr_container((data, indices, indptr), shape=self.shape)
if not self.has_canonical_format:
    x.sum_duplicates()        # ← 只有非规范化时才求和去重
```

而底层的 `coo_tocsr`（C++ 内核）负责把坐标压缩成 `indptr/indices`（[_coo.py:L438](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py#L438)）。这正好呼应 u1-l3 讲过的 `_sparsetools`（C++ 计算心脏）。

**预期结果**：`COO nnz = 7`，`CSR nnz = 4`，`B.toarray()` 为上面那个 4×4 矩阵。

> **真实用途**：这种「允许重复坐标 + 转 CSR 自动求和」的特性，正是**有限元刚度/质量矩阵组装**的标准套路（见 [`__init__.py:L231`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/__init__.py#L231) 的注释）：每个单元贡献叠加到同一全局坐标上，最后一次性求和，比逐元素写入高效得多。

#### 4.2.5 小练习与答案

**练习 1**：如果 4.2.4 里改成 `A.sum_duplicates()`（不转 CSR，直接在 COO 上求和），`A.nnz` 会变成多少？

**答案**：变成 **4**。`sum_duplicates` 直接在 COO 上合并重复坐标，所以 `nnz` 同样从 7 降到 4，且 `A.has_canonical_format` 变为 `True`。

**练习 2**：为什么 `_coo_base.__init__` 在「三元组」分支里要把 `has_canonical_format` 设成 `False`，而「稠密」分支设成 `True`？

**答案**：三元组由用户给出，既不保证坐标有序、也不保证无重复，所以必须设 `False`；稠密数组经 `np.nonzero()` 取出的坐标天然有序且唯一，故可安全设 `True`。这个标志会在后续 `tocsr/tocsc/diagonal` 等操作里用来决定「是否需要先求和」。

---

### 4.3 核心属性：nnz、data、coords、indices

#### 4.3.1 概念说明

构造完稀疏数组后，你需要看懂它内部存了什么。不同格式的「内部表示」不同：

- **COO**：`coords`（坐标元组）+ `data`；
- **CSR**：`indptr` + `indices` + `data`。

但所有格式共享一个最常用的属性 **`nnz`**——「已存储元素个数」。理解 `nnz` 和 `count_nonzero` 的区别，是避免后续踩坑的前提。

#### 4.3.2 核心流程

`nnz` 的定义在总基类 `_spbase`：

- `nnz` 是**属性**（`@property`），返回 `self._getnnz()`（见 [_base.py:L372-L380](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_base.py#L372-L380)）；
- 各格式各自实现 `_getnnz`：
  - COO：`_getnnz` 返回 `len(self.data)`（[_coo.py:L166-L176](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py#L166-L176)）；
  - CSR：`_getnnz` 返回 `int(self.indptr[-1])`（[_compressed.py:L119-L121](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_compressed.py#L119-L121)）。

注意文档对 `nnz` 的措辞是「Number of stored values, **including explicit zeros**」（[_base.py:L374](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_base.py#L374)），即**包含显式零**。这与真正「非零」个数 `count_nonzero`（[_coo.py:L188-L201](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py#L188-L201)，内部用 `np.count_nonzero(self.data)`）不同。

#### 4.3.3 源码精读

**COO 的坐标属性 `row` / `col` / `coords`**：

`coords` 是 COO 的核心存储，`row`/`col` 是 2-D 情况下的便捷视图（[_coo.py:L105-L128](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py#L105-L128)）：

```python
@property
def row(self):
    if self.ndim > 1:
        return self.coords[-2]      # 倒数第二个坐标轴 = 行
    ...

@property
def col(self):
    return self.coords[-1]          # 最后一个坐标轴 = 列
```

即 `A.coords = (row_array, col_array)`，`A.row` 和 `A.col` 分别取出来。

**CSR 的三数组 `indptr` / `indices` / `data`**：

CSR 把第 `i` 行的非零元紧凑地放在 `indices[indptr[i]:indptr[i+1]]` 和 `data[indptr[i]:indptr[i+1]]`。对矩阵

\[
A = \begin{bmatrix} 1 & 2 & 0 \\ 0 & 0 & 3 \\ 4 & 0 & 5 \end{bmatrix},
\]

其 CSR 表示为：

| 数组 | 值 | 含义 |
| --- | --- | --- |
| `indptr` | `[0, 2, 3, 5]` | 第 0 行占 `indices[0:2]`，第 1 行占 `indices[2:3]`，第 2 行占 `indices[3:5]` |
| `indices` | `[0, 1, 2, 0, 2]` | 每个非零元的**列号** |
| `data` | `[1, 2, 3, 4, 5]` | 每个非零元的**值** |

`indptr` 长度恒为「行数 +1」。读取第 `i` 行：`data[indptr[i]:indptr[i+1]]` 配 `indices[indptr[i]:indptr[i+1]]`。这个布局在 4.4 的矩阵-向量乘里会直接用到。

#### 4.3.4 代码实践

**实践目标**：亲手验证 COO 与 CSR 的内部数组。

**操作步骤**：

```python
import numpy as np
from scipy.sparse import coo_array, csr_array

A = csr_array([[1, 2, 0],
               [0, 0, 3],
               [4, 0, 5]])

print("nnz          :", A.nnz)        # 5
print("indptr       :", A.indptr)     # [0 2 3 5]
print("indices      :", A.indices)    # [0 1 2 0 2]
print("data         :", A.data)       # [1 2 3 4 5]

# 转成 COO 看坐标表示
C = A.tocoo()
print("C.coords     :", C.coords)     # (array([0,0,1,2,2]), array([0,1,2,0,2]))
print("C.row, C.col :", C.row, C.col)

# nnz 与 count_nonzero 的区别：构造一个含「显式零」的对象
Z = coo_array(([1, 0, 2], ([0, 1, 2], [0, 0, 0])), shape=(3, 1))
print("Z.nnz        :", Z.nnz)                # 3（含一个显式 0）
print("Z.count_nonzero():", Z.count_nonzero())# 2（真正非零）
```

**需要观察的现象**：
- `A.indptr[-1] == A.nnz`（5）——CSR 的 `nnz` 就是从 `indptr` 末尾读的；
- `Z.nnz`（3）比 `Z.count_nonzero()`（2）大 1，因为 `data` 里存了一个值为 0 的「显式零」。

**预期结果**：如上注释所示。

#### 4.3.5 小练习与答案

**练习 1**：一个 `csr_array` 形状为 `(M, N)`，它的 `indptr` 长度是多少？

**答案**：`M + 1`。`indptr[i]` 到 `indptr[i+1]` 圈出第 `i` 行，所以需要 `M+1` 个端点。

**练习 2**：`A.nnz == 0` 是否意味着 `A` 是「全零矩阵」？反过来呢？

**答案**：`nnz == 0` 一定全零（什么都没存）。但全零矩阵不一定 `nnz == 0`——如果 `data` 里塞了显式 0，`nnz > 0` 而矩阵仍全零。所以判断「是否全零」要用 `count_nonzero() == 0`，而非 `nnz == 0`。

---

### 4.4 矩阵-向量乘 `@` 与 `toarray`

#### 4.4.1 概念说明

构造稀疏数组最常见的目的是**做矩阵-向量乘**（sparse matrix-vector product，SpMV）。scipy.sparse 强烈推荐用 `@` 运算符：

```python
A @ v     # 等价于 A.dot(v)
```

`toarray()` 则把稀疏数组「还原」成稠密 `np.ndarray`，方便打印、校验或交给不支持稀疏的库。

还有一个**必须牢记的规则**：**不要直接把稀疏数组丢给 NumPy 的逐元素函数**（如 `np.sin`、`np.exp`）。原因是 NumPy 通常把稀疏对象当成「普通 Python 对象」，逐元素地套用，得到的是形状为 `()` 的 0-D 对象数组，**结果静默错误**。文档在 [`__init__.py:L143-L150`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/__init__.py#L143-L150) 明确警告了这一点。

#### 4.4.2 核心流程

`@` 运算符的执行链：

```
A @ v
  │  （Python 触发 A.__matmul__(v)）
  ▼
_spbase.__matmul__            # 拒绝标量，转交 _matmul_dispatch
  ▼
_spbase._matmul_dispatch      # 按 v 的类型分流：标量/向量/矩阵/稀疏
  ▼
（CSR 走）_cs_matrix._matmul_vector  →  调用 C++ 内核 csr_matvec
（COO 走）_coo_base._matmul_vector   →  调用 C++ 内核 coo_matvec
```

对于 CSR 的 SpMV，设结果为 \(y = A x\)，则第 \(i\) 个分量为：

\[
y_i = \sum_{j=\text{indptr}[i]}^{\text{indptr}[i+1]-1} \text{data}[j]\cdot x_{\text{indices}[j]}.
\]

即：第 \(i\) 行只要遍历 `indptr[i]` 到 `indptr[i+1]` 这段，对每个存储的 `(列号 indices[j], 值 data[j])` 做 `data[j] * x[indices[j]]` 并累加。复杂度正比于**该行非零元数**，与矩阵总规模无关——这就是稀疏运算省算力的根源。

#### 4.4.3 源码精读

**`@` 入口**（[_base.py:L1006-L1010](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_base.py#L1006-L1010)）：

```python
def __matmul__(self, other):
    if isscalarlike(other):
        raise ValueError("Scalar operands are not allowed, use '*' instead")
    return self._matmul_dispatch(other)
```

注意：`@` 拒绝标量（标量乘法请用 `*`，那是逐元素乘 `_mul_scalar`）。

**分流逻辑** `_matmul_dispatch`（[_base.py:L875-L902](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_base.py#L875-L902)）：当 `other` 是形状 `(N,)` 的 `np.ndarray` 时，走快路径 `self._matmul_vector(other)`。

**CSR 的 SpMV 内核**（[_compressed.py:L388-L398](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_compressed.py#L388-L398)）：

```python
def _matmul_vector(self, other):
    M, N = self._shape_as_2d
    result = np.zeros(M, dtype=upcast_char(self.dtype.char, other.dtype.char))
    fn = getattr(_sparsetools, self.format + '_matvec')   # 'csr_matvec'
    fn(M, N, self.indptr, self.indices, self.data, other, result)
    return result[0] if self.ndim == 1 else result
```

这里 `getattr(_sparsetools, self.format + '_matvec')` 很巧妙：CSR 格式时取 `'csr_matvec'`，CSC 格式时取 `'csc_matvec'`，**用格式名拼接出对应的 C++ 内核函数**。真正的累加循环在 C++/Cython 里，Python 层只负责准备数组和结果缓冲区。

**COO 的 SpMV 内核**（[_coo.py:L890-L917](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py#L890-L917)）也类似，调用 `coo_matvec`（[_coo.py:L917](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py#L917)）。注意 COO 自己实现了 SpMV，**不**需要先转 CSR——这与你可能听过的「COO 不能算乘法」的旧印象不同；新接口里 COO 也能直接 `@`。不过做**大量**运算时，转成 CSR 通常更高效（见 [`__init__.py:L155-L158`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/__init__.py#L155-L158)）。

**`toarray`**（[_coo.py:L297-L320](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py#L297-L320)）：分配一个全零稠密数组 `B`，再调用 `coo_todense` 把每个 `(row, col, data)` 散落到 `B[row, col]`。`toarray()` 返回 `np.ndarray`，`todense()` 返回 `np.matrix`（旧接口，本讲不用）。

#### 4.4.4 代码实践

**实践目标**：用 `@` 做矩阵-向量乘，**手算**一遍验证结果，并演示「直接套 NumPy 函数」会出错。

**操作步骤**（官方示例，见 [`__init__.py:L167-L172`](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/__init__.py#L167-L172)）：

```python
import numpy as np
from scipy.sparse import csr_array

A = csr_array([[1, 2, 0],
               [0, 0, 3],
               [4, 0, 5]])
v = np.array([1, 0, -1])
print(A @ v)                    # 预期 [ 1 -3 -1]

# 反面教材：直接套 NumPy 逐元素函数
result = np.sin(A)              # 看起来"能跑"，但……
print(type(result), np.shape(result))   # 不是你想要的逐元素 sin！
```

**手算验证**（用 4.3 的 CSR 三数组）：

- `indptr=[0,2,3,5]`，`indices=[0,1,2,0,2]`，`data=[1,2,3,4,5]`，`v=[1,0,-1]`。
- \(y_0 = \text{data}[0]\cdot v_{\text{indices}[0]} + \text{data}[1]\cdot v_{\text{indices}[1]} = 1\cdot v_0 + 2\cdot v_1 = 1\cdot1 + 2\cdot0 = 1\)
- \(y_1 = \text{data}[2]\cdot v_{\text{indices}[2]} = 3\cdot v_2 = 3\cdot(-1) = -3\)
- \(y_2 = \text{data}[3]\cdot v_{\text{indices}[3]} + \text{data}[4]\cdot v_{\text{indices}[4]} = 4\cdot v_0 + 5\cdot v_2 = 4\cdot1 + 5\cdot(-1) = -1\)

得到 \(y = [1, -3, -1]\)，与 `A @ v` 输出一致。

**需要观察的现象**：
- `A @ v` 输出 `array([ 1, -3, -1], dtype=int64)`；
- `np.sin(A)` **不会报错**，但返回的不是「对每个非零元取 sin」的稀疏数组——这正是文档警告的「静默错误」。正确做法是先 `A.toarray()` 再 `np.sin`，或用 scipy.sparse 提供的逐元素方法（下一阶段讲义会讲）。

**预期结果**：`A @ v` 为 `[ 1 -3 -1]`；`np.sin(A)` 的类型/形状异常（待本地确认其具体输出，但**绝不是**逐元素正弦的稀疏结果）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `A @ 3` 会抛 `ValueError`？

**答案**：`__matmul__` 在入口就用 `isscalarlike(other)` 拦截了标量（[_base.py:L1007-L1009](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_base.py#L1007-L1009)）。标量乘法（逐元素放大）应使用 `*`：`A * 3`。

**练习 2**：用 `getattr(_sparsetools, self.format + '_matvec')` 这个技巧，如果 `self.format == 'csr'`，实际调用的是哪个 C++ 函数？为什么这样设计？

**答案**：调用 `csr_matvec`。这样设计让 `_cs_matrix` 这一个公共基类能同时服务 CSR 和 CSC：只需把 `'csr'`/`'csc'` 代入字符串，就能拿到对应内核，无需写两份几乎一样的 Python 代码。

---

## 5. 综合实践

**任务**：模拟一次「真实」的稀疏计算流程——**构造 → 组装 → 求和去重 → 运算 → 校验**，把本讲四个模块串起来。

请完成下面的脚本，并回答末尾的问题：

```python
import numpy as np
from scipy.sparse import coo_array

# 1) 用坐标三元组构造一个 5×5 的"组装中"矩阵（含重复坐标）
I = [0, 0, 0, 1, 2, 2, 4, 0]
J = [0, 0, 1, 1, 2, 2, 4, 0]
V = [1, 2, 3, 5, 7, 1, 9, 4]
A = coo_array((V, (I, J)), shape=(5, 5))

# 2) 记录组装阶段的 nnz，转 CSR 触发求和
print("组装中 nnz:", A.nnz)
B = A.tocsr()
print("求和后 nnz:", B.nnz)

# 3) 打印内部三数组
print("indptr :", B.indptr)
print("indices:", B.indices)
print("data   :", B.data)

# 4) 做矩阵-向量乘，再与稠密结果对比
x = np.ones(5)
y = B @ x                        # B @ 全 1 向量 = 每行的行和
y_dense = B.toarray() @ x
print("y      :", y)
print("一致?   :", np.array_equal(y, y_dense))
```

**需要回答**：

1. 「组装中 nnz」和「求和后 nnz」分别是多少？差值对应哪些被合并的坐标？
2. `B @ x`（`x` 全 1）得到的 `y`，每个分量在数学上等于什么？为什么用「全 1 向量」能直接读出它？
3. 把第 4 步的 `B @ x` 换成 `np.sum(B, axis=1)`，结果一致吗？如果不完全一致，可能差在哪里？

**参考思路（非唯一答案）**：

1. 组装中 `nnz = 8`（8 个三元组）；求和后看唯一坐标数。`(0,0)` 出现 3 次（1+2+4=7），其余坐标各 1 次，唯一坐标共 6 个，故求和后 `nnz = 6`。
2. `B @ x` 中 \(y_i = \sum_j B_{ij} x_j\)，当 \(x\) 全 1 时 \(y_i = \sum_j B_{ij}\)，即**第 \(i\) 行的行和**。所以全 1 向量是「读取行和」的快捷方式。
3. `np.sum(B, axis=1)` 走的是 scipy.sparse 自己的 `sum`（不是 NumPy 逐元素），通常与 `B @ np.ones` 一致；但要小心 dtype 上溢（整数行和可能需要 `np.result_type` 提升）。建议本地实际运行对比。

> 本任务整合了：三元组构造（4.1/4.2）、`tocsr` 求和去重（4.2）、内部数组读取（4.3）、`@` 与 `toarray` 校验（4.4）。

---

## 6. 本讲小结

- `coo_array` / `csr_array` 通过同一个构造器签名接受**四类输入**：稠密数组、其它稀疏数组、形状、三元组（CSR 还多一种 `(data, indices, indptr)` 原生形式）。
- `_coo_base.__init__` 用 `isinstance(arg1, tuple)` + `isshape` + `issparse` 三层判断分发输入，最终统一成 `coords + data + _shape`；三元组构造时 `has_canonical_format=False`，稠密构造时为 `True`。
- COO 允许**重复坐标**，转 CSR 时由 `tocsr()` 调 `sum_duplicates()` 自动求和（`coo_tocsr` 是 C++ 内核）——这是有限元矩阵组装的标准套路。
- `nnz` 是「已存储元素数（含显式零）」，与真正非零的 `count_nonzero()` 不同；CSR 的 `nnz` 直接读自 `indptr[-1]`。
- `@` 经 `__matmul__ → _matmul_dispatch → _matmul_vector`，最终调用 C++ 内核 `csr_matvec` / `coo_matvec`；标量乘法要用 `*` 而非 `@`。
- **不要把稀疏数组直接丢给 NumPy 逐元素函数**（如 `np.sin`），会静默出错；应先 `toarray()` 或用 scipy.sparse 自带方法。

---

## 7. 下一步学习建议

本讲只动手了 COO 与 CSR 两种格式。接下来建议：

1. **横向对比七种格式**：进入 [u2 七种稀疏格式与类继承体系] 系列，重点读 `u2-l1 类继承体系`（搞清 `_spbase / sparray / spmatrix` 的关系）与 `u2-l3 CSR 与 CSC 压缩格式`（深入 `indptr/indices` 的 `_swap` 机制）。
2. **理解为什么 CSR 适合运算**：在 `u2-l3` 里你会看到 CSR 与 CSC 互为转置（`_csr_base.transpose` 直接复用 `_csc_container`，见 [_csr.py:L22-L32](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_csr.py#L22-L32)）。
3. **想自己做更多练习**：把本讲 4.2.4 的 Example 2 与 4.4.4 的 SpMV 示例改成 1000×1000 的随机稀疏矩阵，用 `timeit` 对比 `csr_array @ v` 与 `dense @ v` 的耗时——你会直观看到稀疏运算在「大而稀疏」时的优势（呼应 u1-l1 的内存/算力论证）。
