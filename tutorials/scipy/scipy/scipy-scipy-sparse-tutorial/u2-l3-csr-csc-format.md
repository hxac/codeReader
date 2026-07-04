# CSR 与 CSC 压缩格式

## 1. 本讲目标

本讲深入稀疏存储里最常被“拿来算”的两种格式：**CSR**（Compressed Sparse Row，按行压缩）与 **CSC**（Compressed Sparse Column，按列压缩）。读完本讲你应当能够：

- 用 `indptr / indices / data` 三数组徒手写出一个矩阵的 CSR 与 CSC 表示，并能解释三者各自的含义；
- 读懂 `_cs_matrix.__init__` 如何把 `(data, ij)` 与 `(data, indices, indptr)` 两种输入统一翻译成内部三数组；
- 理解 `_swap` 这一关键抽象：为什么 CSR 与 CSC 几乎共享同一份源码，差别只是一个“是否交换坐标”；
- 明白一个深刻而优雅的事实：**CSR 的转置就是 CSC**，且 `.T` 不搬运任何数据。

本讲承接 [u2-l2 COO 格式](u2-l2-coo-format.md) 中“COO 用来造、CSR 用来算”的主线，把视线从“如何组装”转向“如何紧凑存储与快速运算”。

## 2. 前置知识

在继续前，请确认你已掌握以下概念（前几讲已建立）：

- **隐式零**：稀疏矩阵只显式存非零元，零是“不存即零”。（u1-l1）
- **`nnz`**：已存储元素数，含显式零，区别于真正非零的 `count_nonzero`。（u1-l1）
- **COO 三元组**：`(data, coords)`，允许重复坐标，`sum_duplicates` 会合并。（u2-l2）
- **类继承骨架**：所有格式类继承 `_spbase`；`csr_array(_csr_base, sparray)`、`csr_matrix(spmatrix, _csr_base)` 经多重继承组装，`*`、`@`、`_allow_nd` 的行为由 MRO 决定。（u2-l1）

本讲会频繁出现两个术语：

- **主轴（major axis）**：`indptr` 所索引的那个维度。CSR 的主轴是“行”，CSC 的主轴是“列”。
- **副轴（minor axis）**：`indices` 数组里存的是副轴坐标。CSR 副轴是“列号”，CSC 副轴是“行号”。

理解“主轴/副轴”这对抽象，是看懂 `_compressed.py` 的钥匙。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [_compressed.py](_compressed.py) | 定义 `_cs_matrix`，是 CSR/CSC 的**公共基类**，集中了构造、运算、索引、转换、去重等几乎全部逻辑。本讲的主角。 |
| [_csr.py](_csr.py) | 定义 `_csr_base`、`csr_array`、`csr_matrix`。只覆盖少量“行优先”专有方法，尤其是 `transpose`（转置后变 CSC）和 `_swap`（恒等）。 |
| [_csc.py](_csc.py) | 定义 `_csc_base`、`csc_array`、`csc_matrix`。与 `_csr.py` 对称，`transpose` 转置后变 CSR，`_swap` 交换坐标。 |
| [_base.py](_base.py) | 提供 `.T` 属性、`_shape_as_2d` 等公共能力，被本讲引用。 |
| [_coo.py](_coo.py) | 提供 `_coo_to_compressed`，是 `(data, ij)` 输入走向 CSR/CSC 的桥梁。 |

一句话口诀：**“布局看 `_compressed.py`，行优先/列优先的差别看 `_csr.py`/`_csc.py` 里那两个小小的 `_swap`。”**

## 4. 核心概念与源码讲解

### 4.1 CSR/CSC 的三数组紧凑布局

#### 4.1.1 概念说明

CSR 与 CSC 用三个一维数组表达一个稀疏矩阵：

- `data`：所有非零元的值，按“主轴”顺序排列；
- `indices`：每个非零元在副轴上的坐标；
- `indptr`：长度为「主轴长度 + 1」的指针数组，划定每个主轴区段在 `indices/data` 中的起止。

关键约束：

- `indptr` 长度 = 主轴长度 + 1，`indptr[0] == 0`，`indptr` 单调不减；
- `len(indices) == len(data)`；
- 对主轴第 \(k\) 段，非零元的副轴坐标在 `indices[indptr[k]:indptr[k+1]]`，对应值在 `data[indptr[k]:indptr[k+1]]`；
- `nnz` 直接等于 `indptr[-1]`。

CSR 与 CSC 的唯一区别是**谁是主轴**：CSR 主轴是行、副轴是列；CSC 主轴是列、副轴是行。

#### 4.1.2 核心流程

以矩阵

\[
A=\begin{bmatrix}1&0&2\\0&0&3\\4&5&6\end{bmatrix}
\]

为例，逐行（CSR）扫描非零元：

```
行0: (列0=1),(列2=2)  → indices[0:2]=[0,2],  data[0:2]=[1,2]
行1: (列2=3)          → indices[2:3]=[2],    data[2:3]=[3]
行2: (列0=4),(列1=5),(列2=6) → indices[3:6]=[0,1,2], data[3:6]=[4,5,6]
indptr = [0, 2, 3, 6]
```

即 `data=[1,2,3,4,5,6]`、`indices=[0,2,2,0,1,2]`、`indptr=[0,2,3,6]`。

若改按列（CSC）扫描，主轴变为列：

```
列0: (行0=1),(行2=4)  → data=[1,4], indices=[0,2]
列1: (行2=5)          → data=[5],   indices=[2]
列2: (行0=2),(行1=3),(行2=6) → data=[2,3,6], indices=[0,1,2]
data=[1,4,5,2,3,6], indices=[0,2,2,0,1,2], indptr=[0,2,3,6]
```

对比可见：**同样的 `indices`/`indptr`，配上不同的 `data` 顺序，对应不同矩阵**——主轴选谁，决定了数据如何排列。

#### 4.1.3 源码精读

CSR/CSC 的所有共享能力都挂在 `_cs_matrix` 上：

- [_compressed.py:25-28](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_compressed.py#L25-L28)：`_cs_matrix` 的声明，继承 `_data_matrix`、`_minmax_mixin`、`IndexMixin`，注释明说它是“行优先或列优先数组/矩阵的基类”。

`nnz = indptr[-1]` 这一不变量直接体现在 `_getnnz`：

- [_compressed.py:119-121](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_compressed.py#L119-L121)：`axis is None` 时直接返回 `int(self.indptr[-1])`，O(1)。

`check_format` 验证上述布局约束：

- [_compressed.py:195-198](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_compressed.py#L195-L198)：校验 `len(indptr) == M+1` 且 `indptr[0] == 0`，其中 `M` 是主轴长度（由 `_swap` 决定，见 4.2）。

CSR 与 CSC 的优劣在各自 docstring 里写得清清楚楚，可作为选型依据：

- [_csr.py:394-405](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_csr.py#L394-L405)：CSR 的优势是“算术运算、行切片、矩阵-向量乘都快”，劣势是“列切片慢、改结构贵”。
- [_csc.py:250-257](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_csc.py#L250-L257)：CSC 把“行切片快”换成了“列切片快”，其余对称。

CSR docstring 里的示例与本节 4.1.2 完全一致，可作为权威对照：

- [_csr.py:425-431](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_csr.py#L425-L431)：`csr_array((data, indices, indptr), shape=(3,3)).toarray()` 还原成 `[[1,0,2],[0,0,3],[4,5,6]]`。

#### 4.1.4 代码实践

1. 实践目标：徒手填出三数组，验证它确实还原成目标稠密矩阵。
2. 操作步骤：

```python
import numpy as np
from scipy.sparse import csr_array

# 目标矩阵 A = [[1,0,2],[0,0,3],[4,5,6]]
data    = np.array([1, 2, 3, 4, 5, 6])
indices = np.array([0, 2, 2, 0, 1, 2])
indptr  = np.array([0, 2, 3, 6])

A = csr_array((data, indices, indptr), shape=(3, 3))
print(A.toarray())
print("nnz =", A.nnz, " indptr[-1] =", A.indptr[-1])
```

3. 观察现象：打印的稠密矩阵应与目标一致；`nnz` 与 `indptr[-1]` 都为 6。
4. 预期结果：

```
[[1 0 2]
 [0 0 3]
 [4 5 6]]
nnz = 6  indptr[-1] = 6
```

（dtype/打印格式可能因环境略异，数值关系稳定。）

#### 4.1.5 小练习与答案

**练习 1**：把同一个矩阵 `A` 写成 CSC 的三数组。

答案：`data=[1,4,5,2,3,6]`、`indices=[0,2,2,0,1,2]`、`indptr=[0,2,3,6]`。

**练习 2**：一个 5×5 全零矩阵的 `indptr` 长度是多少？`data` 长度又是多少？

答案：`indptr` 长度为 5+1=6（全为 0），`data`/`indices` 长度为 0（隐式零不存储）。

---

### 4.2 `_swap` 机制：一份代码服务两种格式

#### 4.2.1 概念说明

CSR 与 CSC 的算法（求和、切片、转置、矩阵乘……）几乎完全相同，唯一的差别是“主轴是行还是列”。如果为两者各写一份代码，会带来巨大的重复。`_compressed.py` 的解法是写**一份按“主轴/副轴”抽象**的代码，再用一个小小的 `_swap` 方法决定“坐标要不要交换”：

- CSR 的 `_swap(x)` 返回 `x` 不变 → 主轴固定为行；
- CSC 的 `_swap(x)` 返回 `(x[1], x[0])` → 主轴固定为列（即把“行列”对调）。

于是同一套逻辑，传入 CSR 就是行优先运算，传入 CSC 就是列优先运算。

#### 4.2.2 核心流程

以“沿主轴数每段有多少非零元”为例（即 `nnz` 按轴统计）。把 `shape` 与 `axis` 都过一遍 `_swap` 后，`axis==1` 永远代表“主轴”：

```
对 CSR 求 axis=1（每行 nnz）：          axis 经 _swap 后仍是 1 → 返回 np.diff(indptr)
对 CSC 求 axis=1（每行 nnz）：          相当于 CSC 的“副轴”  → 返回 bincount(indices)
对 CSC 求 axis=0（每列 nnz）：          axis 经 _swap 后变成 1 → 返回 np.diff(indptr)
```

`np.diff(indptr)` 给出每个主轴段的长度，因此它对 CSR 是“每行 nnz”、对 CSC 是“每列 nnz”——`_swap` 让这一行代码自然地服务两种语义。

#### 4.2.3 源码精读

两个 `_swap` 实现极短，却是整个机制的支点：

- [_csr.py:137-141](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_csr.py#L137-L141)：`_csr_base._swap` 恒等返回 `x`（行优先）。
- [_csc.py:145-149](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_csc.py#L145-L149)：`_csc_base._swap` 返回 `x[1], x[0]`（列优先，交换坐标对）。

`_shape_as_2d` 把 1-D 视作 `(1, N)`，便于统一按 2-D 处理：

- [_base.py:98-101](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_base.py#L98-L101)：1-D 形状补成 `(1, s[-1])`。

`_getnnz` 是 `_swap` 的典型用例：

- [_compressed.py:119-135](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_compressed.py#L119-L135)：`axis, _ = self._swap((axis, 1-axis))` 与 `_, N = self._swap(self.shape)` 之后，`axis==1` 走 `np.diff(self.indptr)`（主轴），`axis==0` 走 `bincount(indices)`（副轴）。

构造空矩阵时也用 `_swap` 决定 `indptr` 长度：

- [_compressed.py:46-53](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_compressed.py#L46-L53)：`M, N = self._swap(self._shape_as_2d)`，随后 `indptr = np.zeros(M+1, ...)`——对 CSR，M 是行数；对 CSC，M 是列数。

#### 4.2.4 代码实践

1. 实践目标：用同一个矩阵，对比 CSR 与 CSC 上 `nnz` 按轴统计的结果，体会 `_swap` 的作用。
2. 操作步骤：

```python
import numpy as np
from scipy.sparse import csr_array, csc_array

A = np.array([[1, 0, 2],
              [0, 0, 3],
              [4, 5, 6]])
r = csr_array(A)
c = csc_array(A)

print("CSR 每行 nnz:", r.getnnz(axis=1))   # 期望 [2 1 3]
print("CSC 每列 nnz:", c.getnnz(axis=0))   # 期望 [2 1 3] —— 同一段 np.diff(indptr) 逻辑
```

3. 观察现象：两次输出相同，因为两者都落在 `_getnnz` 的“主轴 = `np.diff(indptr)`”分支。
4. 预期结果：

```
CSR 每行 nnz: [2 1 3]
CSC 每列 nnz: [2 1 3]
```

#### 4.2.5 小练习与答案

**练习**：在 `_compressed.py:131-134` 中，为什么 CSR 求 `axis=0`（每列 nnz）用的是 `bincount(indices)` 而不是 `diff(indptr)`？

答案：CSR 的主轴是行，`indptr` 只直接编码“每行”的段长；列方向是副轴，需统计 `indices` 里每个列号出现的次数，故用 `bincount`。（CSC 反之亦然，由 `_swap` 自动对调。）

---

### 4.3 `_cs_matrix.__init__` 的输入分发

#### 4.3.1 概念说明

`csr_array` / `csc_array` / `csr_matrix` / `csc_matrix` 四个类的构造器最终都落到 `_cs_matrix.__init__`。它要兼容五类输入：

1. 另一个稀疏对象（`issparse(arg1)` 为真）；
2. 纯形状元组 `(M, N)`，造空矩阵；
3. 二元组 `(data, ij)`，即坐标三元组风格（`ij` 是 `(row, col)` 数组对）；
4. 三元组 `(data, indices, indptr)`，即原生 CSR/CSC 三数组；
5. 稠密数组，转成稀疏。

其中第 3、4 种是本讲重点：`(data, ij)` 走 COO 中转，`(data, indices, indptr)` 直接落库。

#### 4.3.2 核心流程

```
__init__(arg1, shape, dtype, copy):
  if issparse(arg1):        # 1. 另一稀疏对象 → asformat 成本格式后直接抄三数组
  elif isinstance(arg1, tuple):
      if isshape(arg1):     # 2. 形状 → 造空矩阵（indptr = zeros(M+1)）
      elif len(arg1)==2:    # 3. (data, ij) → 先造 COO，再 _coo_to_compressed(self._swap)，并 sum_duplicates
      elif len(arg1)==3:    # 4. (data, indices, indptr) → 选 index dtype 后直接 np.array 落库
      else:                 # 报错
  else:                     # 5. 稠密 → np.asarray → 造 COO → _coo_to_compressed
  统一处理 shape / dtype / check_format
```

两条主线值得记住：**`(data, ij)` 经 COO 中转**（因此会触发 `sum_duplicates`，重复坐标被求和）；**`(data, indices, indptr)` 直接落库**（不重新排序、不去重，保持你给的原始结构）。

#### 4.3.3 源码精读

整个 `__init__` 在一处：

- [_compressed.py:30-117](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_compressed.py#L30-L117)：入口，先调用 `_data_matrix.__init__`，再按上面五类分发。

`(data, ij)` 分支（经 COO）：

- [_compressed.py:55-60](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_compressed.py#L55-L60)：`coo = self._coo_container(arg1, ...)`，然后 `coo._coo_to_compressed(self._swap)` 得到三数组，最后 `self.sum_duplicates()`。注意 `_swap` 被作为参数传入，COO 据此决定压缩方向。

`_coo_to_compressed` 的实现，把坐标按主轴压缩：

- [_coo.py:414-439](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_coo.py#L414-L439)：`M, N = swap(self._shape_as_2d)`、`major, minor = swap(self.coords)`，调用 C++ 内核 `coo_tocsr` 完成压缩。

`(data, indices, indptr)` 分支（直接落库）：

- [_compressed.py:61-79](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_compressed.py#L61-L79)：解包三元组，用 `_get_index_dtype(..., check_contents=True)` 按实际内容选 `int32`/`int64`，再 `np.array(..., copy=copy)`。这里**不做排序、不做去重**，结构原样保留。

index dtype 的选择策略（承自 u3-l5，此处先建立直觉）：

- [_compressed.py:65-72](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_compressed.py#L65-L72)：当给出 `shape` 且不含 0 维时，`maxval = max(shape)`，据此选择能容纳该最大值的索引整数类型。

#### 4.3.4 代码实践

1. 实践目标：对比 `(data, ij)` 与 `(data, indices, indptr)` 两种构造方式在“重复坐标”上的不同表现。
2. 操作步骤：

```python
import numpy as np
from scipy.sparse import csr_array

# (data, ij) 带重复坐标 (0,0) 出现两次：值 1 与 8
row  = np.array([0, 1, 2, 0])
col  = np.array([0, 1, 1, 0])
data = np.array([1, 2, 4, 8])
a = csr_array((data, (row, col)), shape=(3, 3))
print("(data,ij) 重复坐标求和后:\n", a.toarray())   # (0,0)=1+8=9

# (data, indices, indptr) 直接落库，不去重
# 故意构造重复 (0,0)：第0行有两项 indices=[0,0]
b = csr_array((np.array([1, 8]), np.array([0, 0]), np.array([0, 2, 2, 2])),
              shape=(3, 3))
print("(data,indices,indptr) 直接落库 toarray:")
print(b.toarray())
print("has_canonical_format:", b.has_canonical_format)  # 期望 False
```

3. 观察现象：第一种 `(data,ij)` 会把 `(0,0)` 处的 1 与 8 求和为 9；第二种 `(data,indices,indptr)` 保持“重复结构”，`toarray()` 仍显示 9（显示时合并），但 `has_canonical_format` 为 `False`，说明内部尚未去重。
4. 预期结果：

```
(data,ij) 重复坐标求和后:
 [[9 0 0]
 [0 2 0]
 [0 4 0]]
(data,indices,indptr) 直接落库 toarray:
[[9 0 0]
 [0 0 0]
 [0 0 0]]
has_canonical_format: False
```

> 注：`(data,indices,indptr)` 分支是否对传入数组做拷贝、index dtype 取 int32 还是 int64，可能因 NumPy 版本与 copy 策略略有差异；本实践关注的是“是否去重/排序”这一语义差异，结论稳定。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `(data, ij)` 构造后会调用 `sum_duplicates()`，而 `(data, indices, indptr)` 不会？

答案：前者来自用户随意书写的坐标三元组（可能含重复），转成 CSR 前必须合并以保证语义正确；后者是“原生三数组”，约定由调用者保证结构，构造器原样落库以节省开销，去重留给显式调用。

**练习 2**：若你传入 `(data, indices, indptr)` 但 `indptr` 长度不等于“主轴长度+1”，会发生什么？

答案：`check_format(full_check=False)` 在 [_compressed.py:195-196](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_compressed.py#L195-L196) 抛出 `ValueError: index pointer size ... should be ...`。

---

### 4.4 CSR 与 CSC 互为转置

#### 4.4.1 概念说明

这是本讲最深刻的结论：**矩阵 \(A\) 的行压缩三数组，直接当作列压缩来读，得到的就是 \(A^{T}\)。** 推导很简单——

CSR 把 \(A\) 的非零元按行排列：第 \(k\) 段是第 \(k\) 行。把同样的段按“列”来理解，第 \(k\) 段就成了第 \(k\) 列，而“第 \(k\) 列”正是 \(A^{T}\) 的第 \(k\) 行。因此：

\[
\text{CSR 三数组}(A) \;\equiv\; \text{CSC 三数组}(A^{T})
\]

推论极其优雅：`csr_array.T` 不需要搬运或重排任何数据，只需把同一组 `(data, indices, indptr)` 重新标注为 CSC，并交换形状 `(M,N) → (N,M)`。同理 `csc_array.T` 直接得到 CSR。

#### 4.4.2 核心流程

```
csr.transpose():
    (M, N) = self.shape
    return csc_container((self.data, self.indices, self.indptr), shape=(N, M))

csc.transpose():
    (M, N) = self.shape
    return csr_container((self.data, self.indices, self.indptr), shape=(N, M))
```

`.T` 属性只是 `return self.transpose()`。注意：真正的“行列重排”发生在 `tocsc`/`tocsr` 这种**需要改变主轴**的转换里（调用 C++ 内核 `csr_tocsc`），而 `.T` 这一步是零拷贝的语义重标注。

#### 4.4.3 源码精读

CSR 的转置生成 CSC：

- [_csr.py:22-34](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_csr.py#L22-L34)：`return self._csc_container((self.data, self.indices, self.indptr), shape=(N, M), copy=copy)`——三数组原样传入，仅形状对调。1-D 时直接返回自身（转置无意义）。

CSC 的转置生成 CSR，完全对称：

- [_csc.py:20-31](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_csc.py#L20-L31)：`return self._csr_container((self.data, self.indices, self.indptr), (N, M), copy=copy)`。

`.T` 与 `mT`（矩阵转置）都复用 `transpose`：

- [_base.py:397-400](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_base.py#L397-L400)：`T` 属性即 `self.transpose()`。

而“真正改主轴”的 `tocsc`（CSR→CSC，非转置）则要调用 C++ 内核做一次列方向重排：

- [_csr.py:73-95](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_csr.py#L73-L95)：分配新数组，调用 `csr_tocsc(M, N, ...)`，结果 `has_sorted_indices = True`。这与零拷贝的 `.T` 形成对照。

#### 4.4.4 代码实践

1. 实践目标：验证 `csr_array.T` 得到 `csc_array`，且不搬运数据；并确认数值上等于数学转置。
2. 操作步骤（即本讲规格要求的实践任务）：

```python
import numpy as np
from scipy.sparse import csr_array

# 目标 4x4 矩阵 A
# [[0,5,0,0],
#  [1,0,2,0],
#  [0,0,0,7],
#  [0,0,3,0]]
data    = np.array([5, 1, 2, 7, 3])
indices = np.array([1, 0, 2, 3, 2])
indptr  = np.array([0, 1, 3, 4, 5])   # 行0:[0:1] 行1:[1:3] 行2:[3:4] 行3:[4:5]

A = csr_array((data, indices, indptr), shape=(4, 4))
print("A =\n", A.toarray())

T = A.T
print("type(A.T) =", type(T).__name__)   # 期望 csc_array
print("A.T =\n", T.toarray())            # 期望 A 的数学转置
```

3. 观察现象：`A.T` 的类型是 `csc_array`（来自 `_csr_base.transpose` 里的 `_csc_container`），其稠密形式等于 \(A^{T}\)。三数组 `(data, indices, indptr)` 与原 CSR 相同，仅被重新解读为 CSC。
4. 预期结果：

```
A =
 [[0 5 0 0]
 [1 0 2 0]
 [0 0 0 7]
 [0 0 3 0]]
type(A.T) = csc_array
A.T =
 [[0 1 0 0]
 [5 0 0 0]
 [0 2 0 3]
 [0 0 7 0]]
```

> 手算验证 \(A^{T}\)：\(A\) 的列 0 是 \((0,1,0,0)\)，列 1 是 \((5,0,0,0)\)，列 2 是 \((0,2,0,3)\)，列 3 是 \((0,0,7,0)\)，把它们作为行即得上面的 \(A^{T}\)。结论可本地复现。

#### 4.4.5 小练习与答案

**练习 1**：为什么说 `csr_array.T` 是“零拷贝”的，而 `csr_array.tocsc()` 不是？

答案：`.T` 只把同一组 `(data, indices, indptr)` 重新标注为 CSC 并对调形状，不搬运数据；`tocsc()` 要把主轴从行换成列，必须用 `csr_tocsc` 重排所有非零元，产生全新数组。

**练习 2**：若你对一个 `csc_array` 调 `.T`，得到什么类型？再调一次 `.T` 又回到什么？

答案：得到 `csr_array`；再 `.T` 一次又回到 `csc_array`（形状还原）。两次 `.T` 在数学上等于自身。

---

## 5. 综合实践

把本讲的四个模块串起来，完成一个“徒手构造并相互转换”的小任务：

1. 自选一个含 5~7 个非零元的 4×4 稀疏矩阵 \(A\)，**先在纸上**写出它的 CSR 三数组 `(data, indices, indptr)`，并写出按 CSC 主轴（列）扫描得到的三数组。
2. 用 `csr_array((data, indices, indptr), shape=(4,4))` 验证你的 CSR 书写正确（`toarray()` 与目标一致）。
3. 调用 `.T` 得到 `csc_array`，确认其类型与数值；再对原 CSR 调 `.tocsc()` 得到“真正的 CSC 表示”，比较两者 `data` 顺序的区别——体会“转置重标注”与“转格式重排”的差异。
4. 用 `(data, (row, col))` 坐标三元组重新构造同一个 \(A\)（故意在某处放一个重复坐标），观察 `sum_duplicates` 前后 `nnz` 与 `data` 的变化，验证 4.3 中“`(data,ij)` 经 COO 中转并去重”的结论。
5. 用 `getnnz(axis=...)` 在 CSR 与 CSC 上分别统计“每行/每列”非零数，验证 4.2 中 `_swap` 让同一份逻辑服务两种语义。

> 这是一个纯源码理解 + 动手验证型实践，不需要改源码。若某一步结果与预期不符，回到对应模块的源码链接处核对不变量（尤其 `indptr` 长度与 `has_canonical_format`）。

## 6. 本讲小结

- CSR/CSC 共用 `indptr / indices / data` 三数组布局；二者唯一区别是主轴（CSR=行，CSC=列），`nnz = indptr[-1]`。
- `_cs_matrix`（[_compressed.py](_compressed.py)）是公共基类，集中了构造、运算、索引、转换、去重等几乎全部逻辑。
- `_swap` 是行优先/列优先的统一抽象：CSR 的 `_swap` 恒等，CSC 的 `_swap` 交换坐标对，让同一份代码服务两种格式。
- `__init__` 分发五类输入；其中 `(data, ij)` 经 COO 中转并 `sum_duplicates`，`(data, indices, indptr)` 直接落库、不重排不去重。
- **CSR 的转置即 CSC**：`.T` 是零拷贝的语义重标注（三数组不变、形状对调），而 `tocsc()`/`tocsr()` 才是真正改变主轴的重排。
- CSR 适合行切片与矩阵-向量乘，CSC 适合列切片；选型本质是“按哪个轴访问更频繁”。

## 7. 下一步学习建议

- 继续学习 [u2-l4 BSR 格式](u2-l4-bsr-format.md)：BSR 在 CSR 之上引入 `blocksize`，把等大小稠密小块当作存储单元，是 CSR 思想的自然推广（可见 [_csr.py:97-133](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_csr.py#L97-L133) 的 `tobsr`）。
- 想深入“去重/规范化与显式零的维护”，可读 [_compressed.py:1035-1131](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_compressed.py#L1035-L1131) 的 `eliminate_zeros` / `sum_duplicates` / `sort_indices`，对应 u6-l1。
- 想理解矩阵-向量乘如何落到 C++ 内核，可顺藤摸瓜到 [_compressed.py:388-398](https://github.com/scipy/scipy/blob/ce1f64777ed7d36fd54f0f7c3d30bd88731b9c10/scipy/sparse/_compressed.py#L388-L398) 的 `_matmul_vector` 与 `sparsetools/csr.h`，对应 u3-l6。
- 建议同时对照 [u3-l2 索引机制](u3-l2-indexing-mixin.md) 阅读 `_get_submatrix` 等方法，看 `_swap` 如何让 CSR 行切片（`_getrow`）与 CSC 列切片（`_getcol`）共享同一套子矩阵抽取逻辑。
