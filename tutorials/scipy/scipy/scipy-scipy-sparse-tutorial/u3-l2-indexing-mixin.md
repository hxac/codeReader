# 索引机制：_index.py 的 IndexMixin

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚稀疏数组 `A[...]` 这一行代码在 `scipy.sparse` 内部是如何被一步步分发到底层计算的。
- 读懂 `_index.py` 中 `IndexMixin.__getitem__` 的调度流程，以及它如何区分 1-D 与 2-D 两条路径。
- 理解 `_validate_indices` 如何把「整数、切片、数组、布尔数组、`np.newaxis`、`Ellipsis`、稀疏对象」这一堆五花八门的 key 归一成统一的 `index + new_shape`。
- 区分 `_get_int` / `_get_slice` / `_get_array` 三类原子分发，以及 2-D 场景下 `_get_intXint`、`_get_sliceXarray` 等 3×3 分发网格。
- 理解为何 CSR/CSC/LIL 一旦遇到 `>2D` 的索引就报错并建议「转 COO」，以及 `np.newaxis` 是如何通过 `new_shape` 把结果「升维」的。

## 2. 前置知识

本讲默认你已经学过 **u2-l3（CSR 与 CSC 压缩格式）**，知道：

- CSR/CSC 用 `data / indices / indptr` 三数组存储，主轴与副轴的概念，以及 `_swap` 机制让 CSR 与 CSC 共用同一份代码。
- `nnz` 是「已存储元素数」，零可以是「显式零」。

此外需要一点 NumPy 索引常识：

- **整数索引**（`A[2]`）会「吃掉」一个维度；**切片**（`A[1:3]`）保留该维度；**花式数组索引**（`A[[0,2]]`）也保留该维度。
- **`np.newaxis`（即 `None`）**不索引任何元素，只「插」一个大小为 1 的新维度，例如 `A[None]` 把一维变成 `(1, N)`。
- **`Ellipsis`（`...`）**表示「用 `:` 填满到正确维数」，例如 `A[..., 0]`。
- **布尔数组索引**：`A[mask]` 中 `mask` 是 bool 数组，等价于取 `mask.nonzero()` 处的元素。

稀疏索引和稠密索引最大的不同在于：**稀疏矩阵「零是隐式的」**，所以索引返回的结果常常需要重新组装一个新的稀疏对象，而且不同格式对索引的支持力度差异极大（CSR 擅长行操作，COO 才支持 N-D）。`scipy.sparse` 用一个共享的 `IndexMixin` 把「解析 key + 调度」这件复杂但与格式无关的事统一起来，再让每种格式各自实现真正的取数细节。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [_index.py](_index.py) | **本讲主角**。定义 `IndexMixin` 基类（提供 `__getitem__` / `__setitem__` 的统一调度）与 `_validate_indices`、`_asindices`、`_compatible_boolean_index` 三个工具函数。所有支持索引的格式都混入（mixin）它。 |
| [_compressed.py](_compressed.py) | CSR/CSC 的公共基类 `_cs_matrix`，混入 `IndexMixin` 后实现了 `_get_intXint`、`_get_arrayXarray`、`_major_index_fancy` 等全部取数方法，最终落到 C++ 内核（如 `get_csr_submatrix`、`csr_row_index`）。 |
| [_lil.py](_lil.py) | LIL 格式的 `_lil_base`，同样混入 `IndexMixin`，但为 `(int, int)` 这种最常见情况写了快速路径，其余委托给 `IndexMixin`。 |
| [_dok.py](_dok.py) | DOK 格式的 `_dok_base`，混入 `IndexMixin` 后基于「字典键」实现索引。本讲用来对比「同一调度、不同实现」。 |
| [_coo.py](_coo.py) | COO 格式。它**没有**沿用 `IndexMixin.__getitem__` 的 3×3 网格，而是自己写了一个支持任意维度的 `__getitem__`——这就是「`>2D` 建议转 COO」的由来。 |
| [_base.py](_base.py) | 定义 `_spbase`，提供 `format`、`ndim`、`_allow_nd` 等被索引逻辑频繁使用的属性。 |

## 4. 核心概念与源码讲解

### 4.1 IndexMixin.__getitem__：统一调度入口

#### 4.1.1 概念说明

`scipy.sparse` 有七种格式，并非每一种都擅长索引，但只要某种格式「想做索引」，它就不必从零写 `__getitem__`。`_index.py` 提供了一个混入类 `IndexMixin`，把「**解析用户给的 key → 判断该走哪条分支 → 调用对应取数方法 → 重新包装结果**」这套与格式无关的流程集中起来。各格式只需要实现一组「原子取数方法」（如 `_get_intXint`），`IndexMixin` 负责把正确的参数喂给它们。

这是一种典型的 **模板方法 / 调度（dispatch）模式**：父类定流程骨架，子类填具体算法。`IndexMixin` 在末尾声明了一堆 `raise NotImplementedError()` 的占位方法，这就是它和子类之间的「契约」。

#### 4.1.2 核心流程

`__getitem__` 的执行可以概括为三步：

```text
A[key]
  │
  ▼
1) index, new_shape, _, _ = _validate_indices(key, self.shape, self.format)
      —— 把 key 解析成「归一化后的 index 元组」和「结果应有的形状 new_shape」
  │
  ▼
2) 若 len(new_shape) > 2：直接报错，提示转 COO
  │
  ▼
3) 按 index 的个数分两条路：
     · len(index) == 1  → 1-D 路径：_get_int / _get_slice / _get_array
     · len(index) == 2  → 2-D 路径：_get_intXint / _get_sliceXarray / ... 3×3 网格
  │
  ▼
4) 把取出的 res 按 new_shape 重新包装（处理 None 升维、sparray/spmatrix 差异）后返回
```

关键点：`__getitem__` 自己**不做任何取数**，它只做「翻译 + 调度 + 包装」。

#### 4.1.3 源码精读

入口与「先校验、再分流」的骨架见 [_index.py:L29-L34](_index.py#L29)：先调用 `_validate_indices` 得到 `index` 与 `new_shape`，紧接着对 `>2D` 的情况抛出明确错误，并建议转 COO 格式。

```python
def __getitem__(self, key, /):
    index, new_shape, _, _ = _validate_indices(key, self.shape, self.format)
    if len(new_shape) > 2:
        raise IndexError("Indexing that leads to >2D is not supported by "
                         f"{self.format} format. Try converting to COO format")
```

这正是学习目标里「为何 `>2D` 索引会建议转 COO」的根源：CSR/CSC/LIL 的 `_allow_nd` 最多只到 2（见 [_csr.py:L20](_csr.py#L20) 的 `_allow_nd: tuple[int, ...] = (1, 2)` 与 [_base.py:L92](_base.py#L92) 的默认 `(2,)`），所以 `IndexMixin` 在 `len(new_shape) > 2` 时直接拒绝；而 COO 原生支持 1–64 维（见 [_coo.py:L560](_coo.py#L560) 的自实现 `__getitem__`），故错误信息提示「Try converting to COO format」。

随后是 **1-D 路径** [_index.py:L35-L57](_index.py#L35-L57)：当归一化后只剩一个索引时，把它拆成 `int / slice / array` 三类，分别调用 `_get_int`、`_get_slice`、`_get_array`，最后处理 `np.newaxis` 把标量结果「撑」回稀疏数组。

**2-D 路径** [_index.py:L59-L104](_index.py#L59-L104) 是一张 3×3 的分发网格。它先取出 `row, col = index`，再按 `row` 是 int / slice / array 三种情况嵌套判断 `col` 的类型，调用对应的 `_get_intXint`、`_get_intXslice`、`_get_sliceXarray` 等方法。其中还有一个特殊优化：当 `row == col == slice(None)`（即 `A[:, :]`）时直接 `self.copy()`，跳过所有计算（见 [_index.py:L78-L79](_index.py#L78)）。

数组×数组时还要区分两种语义 [_index.py:L91-L104](_index.py#L91)：当 `row` 是「列向量」(`shape[1]==1`) 而 `col` 是一维时走 **外积索引（outer indexing）** `_get_columnXarray`；否则先把 `row`、`col` 广播成同形，走 **内积索引（inner indexing）** `_get_arrayXarray`。这部分语义和 NumPy 的「花式索引」对齐。

最后是结果包装 [_index.py:L106-L125](_index.py#L106)：`spmatrix` 必须保持 2-D（会用 `(1,) + new_shape` 把被 `None` 撑出的 1-D 再压回 2-D），而 `sparray` 允许 1-D 结果，直接 `res.reshape(new_shape)`。

#### 4.1.4 代码实践

1. **实践目标**：亲眼看到 `__getitem__` 把同一个 key 翻译成不同分支。
2. **操作步骤**：

   ```python
   import numpy as np
   from scipy.sparse import csr_array

   A = csr_array(np.arange(12).reshape(3, 4))
   print(A[1, 2])          # (int, int)  -> _get_intXint  -> 标量
   print(A[1, :].toarray())# (int, slice)-> _get_intXslice -> 1×4 行
   print(A[1:3, :].toarray())  # (slice, slice) -> _get_sliceXslice
   print(A[[0, 2], [1, 3]].toarray())  # (array, array) -> _get_arrayXarray
   ```

3. **需要观察的现象**：四种 key 各自落到不同的 `_get_*` 方法，返回类型在「标量 / 1-D / 2-D 稀疏数组」之间切换。
4. **预期结果**：`A[1,2]` 返回 `6`；`A[1,:]` 是 `[[4,5,6,7]]`；`A[[0,2],[1,3]]` 是 `[[1,3],[9,11]]`（内积索引，取 (0,1) 与 (2,3) 两点）。
5. 运行结果「待本地验证」后再下结论。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `A[:, :]` 不会进入 `_get_sliceXslice`？

> **答**：因为 [_index.py:L78-L79](_index.py#L78) 对 `row == slice(None) and row == col` 做了特判，直接返回 `self.copy()`，省去重组稀疏结构的开销。

**练习 2**：若把 `csr_array` 换成 `csc_array`，`__getitem__` 的代码会变吗？

> **答**：不会变。`csr_array` 与 `csc_array` 共用同一个 `_cs_matrix` 基类（见 [_compressed.py:L25](_compressed.py#L25)），调度逻辑完全一致；差别只在底层 `_get_intXint` 等方法里通过 `_swap` 决定主轴是行还是列。

---

### 4.2 _validate_indices：把任意 key 归一为 index + new_shape

#### 4.2.1 概念说明

`__getitem__` 能那么「干净」，是因为脏活累活都丢给了 `_validate_indices`。用户写 `A[1, None, 0:2]` 时，key 是 `(1, None, slice(0,2))`，里面同时混着整数、`None`、切片。`_validate_indices` 的职责是：把这些杂乱的 key 翻译成两个干净的东西——

- `index`：归一化后的索引元组，每个元素只能是 `int`、`slice` 或 `ndarray`（布尔数组已被转成 `.nonzero()` 的整数数组，`None` 已被剔除）。
- `new_shape`：结果应有的形状（**包含** `None` 带来的大小为 1 的新维度），供 `__getitem__` 最后 `reshape`。

它还顺带做了边界校验、ellipsis 展开、布尔数组匹配检查。

#### 4.2.2 核心流程

函数采用**两遍扫描（two-pass）**，因为有些判断要等 `Ellipsis` 展开后才能做：

```text
输入 key（可能是单个对象或 tuple）
   │
   ▼ 第一遍 pass 1（[_index.py:L320-L356]）
   · 记录 Ellipsis 位置（只允许一个）
   · 识别 None
   · 把「数组/列表」转成 np.asarray，统计 prelim_ndim
   · 把兼容的布尔索引单独标记
   · 用稀疏对象作索引 → 报错
   · 若 prelim_ndim > self_ndim → 报 "Too many indices"
   · 把 Ellipsis 展开成若干个 slice(None)
   │
   ▼ 第二遍 pass 2（[_index.py:L374-L431]）
   · None  → 记入 none_positions，给 new_shape 加一个 1
   · slice → 算出长度，并入 new_shape
   · int   → 边界校验、负数转正，并入 index 与 arr_int_pos
   · bool  → 校验形状，调用 .nonzero() 展开成整数索引
   · array → _asindices 校验后并入 index
   · 多个数组索引 → 用 broadcast_shapes 协调形状并定位到 new_shape
   │
   ▼ 返回 (index, new_shape, arr_int_pos, none_positions)
```

#### 4.2.3 源码精读

函数签名与返回值说明见 [_index.py:L298-L306](_index.py#L298)：返回四元组 `(index, requested_shape, arr_pos, none_pos)`，其中 `requested_shape` 就是贯穿全流程的 `new_shape`。

第一遍里，对每一项 key 做分类 [_index.py:L323-L345](_index.py#L323)：`Ellipsis` 记位置、`None` 保留、`slice` 或整数原样保留并累加 `prelim_ndim`、布尔索引用 `_compatible_boolean_index` 判定、稀疏对象直接报错、其余当作稠密数组。紧接着是「索引维数过多」的检查 [_index.py:L346-L350](_index.py#L346)，再按 `Ellipsis` 位置把缺省维度补成 `slice(None)` [_index.py:L351-L356](_index.py#L351)。

第二遍是核心 [_index.py:L375-L411](_index.py#L375)。其中 `None` 的处理 [_index.py:L376-L378](_index.py#L376) 解释了 `np.newaxis` 的本质：

```python
if idx is None:
    none_positions.append(len(idx_shape))
    idx_shape.append(1)        # 给 new_shape 塞一个大小为 1 的轴
```

也就是说 `new_shape` 里被 `None` 撑出的那些 `1`，正是 `__getitem__` 末尾 `res.reshape(new_shape)` 用来「升维」的依据。

整数索引会做边界校验并把负数翻转 [_index.py:L385-L392](_index.py#L385)：

```python
N = self_shape[index_ndim]
if not (-N <= idx < N):
    raise IndexError(f'index ({idx}) out of range')
idx = int(idx + N if idx < 0 else idx)
```

布尔数组则要求其形状严格匹配对应维度，再展开成 `.nonzero()` [_index.py:L394-L404](_index.py#L394)。普通数组索引交给 `_asindices`（见 4.2 末尾）做边界与维度校验 [_index.py:L405-L411](_index.py#L405)。最后 [_index.py:L412-L431](_index.py#L412) 处理「多个数组索引」时用 `np.broadcast_shapes` 协调形状，并决定这些数组维度在 `new_shape` 中的插入位置。

辅助函数 `_asindices` [_index.py:L435-L467](_index.py#L435) 负责把数组索引规范成 1-D/2-D 整数数组、做上下界检查、把负索引就地修正（注意它对 `lil` 格式网开一面，交由 LIL 自己的 C 内核做边界检查 [_index.py:L449-L450](_index.py#L449)）。`_compatible_boolean_index` [_index.py:L470-L491](_index.py#L470) 则用「先偷看第一个元素是否 bool」的技巧快速排除非布尔索引，避免昂贵的 `asanyarray`。

#### 4.2.4 代码实践

1. **实践目标**：直接观察 `_validate_indices` 对几种典型 key 的归一化结果。
2. **操作步骤**：

   ```python
   from scipy.sparse import csr_array
   from scipy.sparse._index import _validate_indices
   import numpy as np

   A = csr_array(np.arange(12).reshape(3, 4))
   for key in [2, (1, 2), (slice(None), 0), (1, None, 2), (np.array([0, 1]),)]:
       index, new_shape, arr_pos, none_pos = _validate_indices(key, A.shape, A.format)
       print(f"key={key!r:30} -> index={index}, new_shape={new_shape}, none_pos={none_pos}")
   ```

3. **需要观察的现象**：`None` 如何在 `new_shape` 里多塞一个 `1`；负数/布尔如何被改写。
4. **预期结果**：`(1, None, 2)` 会得到 `new_shape=(1,)`、`none_positions=[0]`、`index=(1, 2)`；布尔/负索引在 `index` 里被替换为正整数数组。
5. 运行结果「待本地验证」。注意：`_validate_indices` 是私有函数，未来版本可能调整，此处仅用于学习内部机制。

#### 4.2.5 小练习与答案

**练习 1**：`A[1, None, 2]` 经过 `_validate_indices` 后 `index` 是什么？为什么？

> **答**：`index = (1, 2)`。`None` 不参与实际取数，只把一个大小为 1 的轴记进 `new_shape` 与 `none_positions`，因此被剔除出 `index`。

**练习 2**：用稀疏数组当索引（如 `A[B]`，`B` 是另一个稀疏数组）会发生什么？

> **答**：会抛 `IndexError`，因为 [_index.py:L337-L342](_index.py#L337) 明确禁止，只有「形状相等的布尔稀疏索引」是个 TODO 例外。

---

### 4.3 _get_int / _get_slice / _get_array：1-D 三类原子分发

#### 4.3.1 概念说明

经过 `_validate_indices` 归一后，1-D 路径只剩「一个索引」。这个索引只可能是 `int`、`slice` 或 `ndarray` 三种之一——这就是 `IndexMixin` 在 1-D 场景下的三类原子分发：`_get_int` / `_get_slice` / `_get_array`。它们是抽象契约：`IndexMixin` 只声明不实现（`raise NotImplementedError`），由每种支持 1-D 的格式自己填。

> 注意：并非所有格式都支持 1-D。CSR/CSC/DOK 支持（`_allow_nd` 含 1），LIL 只支持 2-D，COO 支持任意维但走自己的 `__getitem__`。所以这套 1-D 分发主要服务 CSR/CSC/DOK。

#### 4.3.2 核心流程

```text
1-D 路径（[_index.py:L36-L57]）
  idx = index[0]
  if isinstance(idx, np.ndarray) and idx.shape == (): idx = idx.item()  # 0-D 数组当标量
  if int   -> self._get_int(idx)
  if slice -> self._get_slice(idx)
  else     -> self._get_array(idx)          # 花式数组

  包装结果：
  · spmatrix：直接返回（矩阵必须 2-D，1-D 场景其实不会进到这里）
  · sparray：若 res 是标量但 new_shape 非 ()（说明 None 要升维），
             用 self.__class__([res], shape=new_shape) 重新造一个稀疏数组；
             否则 res.reshape(new_shape)
```

#### 4.3.3 源码精读

1-D 分支与三类判别见 [_index.py:L36-L57](_index.py#L36)。其中 [_index.py:L38-L40](_index.py#L38) 有个细节：当 `idx` 是「0-D 数组」（`shape == ()`）时先 `idx.item()` 转成 Python 标量，避免它被当成花式数组。

抽象方法声明集中放在 [_index.py:L240-L247](_index.py#L240)，`_get_int` / `_get_slice` / `_get_array` 都只是 `raise NotImplementedError()`，提示子类必须实现。

具体实现方面，**DOK** 最直观：[_dok.py:L272-L292](_dok.py#L272) 直接基于字典键取值，`_get_int` 取单个键、`_get_slice` 把切片转成范围再取、`_get_array` 遍历数组逐键取。**CSR/CSC** 走 `_cs_matrix`，1-D 的 `_get_int` / `_get_slice` / `_get_array` 复用了 2-D 那套 `_get_intXint` / `_get_sliceXslice` / `_get_arrayXarray`（通过把 1-D 视作一行），底层仍是 C++ 内核。**LIL** 不实现这三个 1-D 方法，所以对 1-D `lil_array` 索引会落到 `NotImplementedError`（但 LIL 也不允许 1-D 输入，见 [_lil.py:L61-L62](_lil.py#L61)）。

#### 4.3.4 代码实践

1. **实践目标**：用整数、切片、花式数组三种方式索引同一个 1-D `csr_array`，验证三类分发。
2. **操作步骤**：

   ```python
   import numpy as np
   from scipy.sparse import csr_array

   v = csr_array(np.array([0.0, 10.0, 0.0, 30.0, 0.0, 50.0]))
   print(v[3])                # int  -> _get_int   -> 标量 30.0
   print(v[1:4].toarray())    # slice-> _get_slice -> [10,0,30]
   print(v[[0, 3, 5]].toarray())  # array -> _get_array -> [0,30,50]
   assert v[1:4].shape == (2,)   # 切片只保留非零所在的结构，长度=3 但稀疏存储
   ```

3. **需要观察的现象**：三种 key 返回类型不同（标量 vs 1-D 数组），且 `v[1:4]` 的 `shape` 反映切片区间长度而非 `nnz`。
4. **预期结果**：`v[3]==30.0`；`v[[0,3,5]]` 是 `[0,30,50]`（注意显式零 `0` 被保留在结果里）。
5. 运行结果「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`v[1:4]` 的结果里包含显式零吗？

> **答**：包含。切片只是「按位置选行/列」，并不会把中间的显式零删掉，所以 `v[1:4]` 仍带着 `v[1]=10`、`v[3]=30` 以及位置 2 上的零结构（取决于源数据是否有显式零）。要真正去掉零值需用 `eliminate_zeros()`。

**练习 2**：为什么 `IndexMixin` 不直接实现 `_get_int`，而要留给子类？

> **答**：因为「取出一个元素」的物理操作与存储格式强相关：DOK 是查字典、CSR 是在 `indptr/indices` 里二分、LIL 是在行的有序表里 `bisect`。`IndexMixin` 无法写一份通用实现，只能定契约。

---

### 4.4 2-D 分发网格与各格式实现差异

#### 4.4.1 概念说明

2-D 索引是 `row` 与 `col` 两个索引的组合，每个又可能是 int/slice/array，于是天然形成一张 3×3 的分发网格（外加 array×array 的 inner/outer 两种语义）。`IndexMixin.__getitem__` 的 2-D 段就是把 `(row 类型, col 类型)` 路由到 `_get_intXint`、`_get_intXslice`、`_get_sliceXarray`、`_get_arrayXarray` 等方法。本模块对比 CSR、LIL、DOK 三种格式的实现差异，体会「同一调度、不同实现」。

#### 4.4.2 核心流程

```text
2-D 路径（[_index.py:L60-L104]）
  row, col = index
  按 row ∈ {int, slice, array} 与 col ∈ {int, slice, array} 路由：
    int×int   -> _get_intXint(row, col)        # 返回标量
    int×slice -> _get_intXslice                # 取一行的一段
    slice×slice-> _get_sliceXslice (或 copy)   # 取子矩阵
    slice×array-> _get_sliceXarray
    array×array-> 先判 inner/outer：
                   outer(列向量×行) -> _get_columnXarray
                   inner(同形广播)  -> _get_arrayXarray
```

#### 4.4.3 源码精读

2-D 分发网格的判别逻辑见 [_index.py:L63-L104](_index.py#L63)，结构是三层嵌套 `if`：外层按 `row` 是 int/slice/else(array) 分，内层再按 `col` 的类型细分子方法。

**CSR/CSC（`_cs_matrix`）** 的实现最丰富。例如标量取值 `_get_intXint` [_compressed.py:L546-L552](_compressed.py#L546) 调用 C++ 内核 `get_csr_submatrix` 取出 1×1 子块再 `.sum()`：

```python
def _get_intXint(self, row, col):
    M, N = self._swap(self.shape)
    major, minor = self._swap((row, col))
    indptr, indices, data = get_csr_submatrix(
        M, N, self.indptr, self.indices, self.data,
        major, major + 1, minor, minor + 1)
    return data.sum(dtype=self.dtype)
```

注意 `_swap` 让 CSR 与 CSC 共用此代码：CSR 时 `(major, minor)=(row, col)`，CSC 时对调成 `(col, row)`。更复杂的数组索引拆成「主轴」与「副轴」两条流水线：`_major_index_fancy` [_compressed.py:L580-L613](_compressed.py#L580) 用 `csr_row_index` 按行重排，`_minor_index_fancy` [_compressed.py:L655-L692](_compressed.py#L655) 用 `csr_column_index1/2` 两遍扫描按列筛选；切片 `_major_slice` / `_minor_slice` [_compressed.py:L615-L708](_compressed.py#L615) 在步长为 1 时走快速 `_get_submatrix`，否则退化为花式索引。`_get_submatrix` [_compressed.py:L710-L729](_compressed.py#L710) 则是统一的「取连续子块」底层，调用 `get_csr_submatrix`。

**LIL（`_lil_base`）** 的做法很有代表性 [_lil.py:L174-L182](_lil.py#L174)：它**重写**了 `__getitem__`，为最常见的 `(int, int)` 加了一条快速路径直接调 `_get_intXint`，其余情况才回到 `IndexMixin.__getitem__`：

```python
def __getitem__(self, key):
    if (isinstance(key, tuple) and len(key) == 2 and
            isinstance(key[0], INT_TYPES) and isinstance(key[1], INT_TYPES)):
        return self._get_intXint(*key)     # 快速路径，跳过 _validate_indices
    return IndexMixin.__getitem__(self, key)
```

`_get_intXint` [_lil.py:L184-L187](_lil.py#L184) 调用 Cython 内核 `lil_get1` 直接定位；切片类则统一走 `_get_row_ranges` [_lil.py:L232-L259](_lil.py#L232)（调 `lil_get_row_ranges`）。LIL 还把 `_get_intXarray`、`_get_sliceXarray` 都归并到 `_get_columnXarray` [_lil.py:L209-L220](_lil.py#L209)，体现了它「按行存储」的特性。

**DOK（`_dok_base`）** 最简单 [_dok.py:L300-L365](_dok.py#L300)：因为底层就是 `{(row,col): val}` 字典，`_get_intXint` 就是 `self.get((row,col), 0)`，切片/数组则是遍历键集合筛选。它无需任何 C 内核。

> 一句话对比：**CSR 用 C++ 内核按压缩数组搬数据，LIL 用 Cython 按行的有序表插取，DOK 用纯 Python 查字典**——但三者顶上都是同一个 `IndexMixin.__getitem__`。

#### 4.4.4 代码实践

1. **实践目标**：用整数、切片、花式数组三种 key 索引一个 2-D `csr_array`，并用断言验证结果形状；再构造一个触发 `>2D` 报错的索引。
2. **操作步骤**：

   ```python
   import numpy as np
   from scipy.sparse import csr_array

   A = csr_array(np.arange(12).reshape(3, 4))

   # (1) 三类索引取行/列，断言形状
   r_int   = A[1, :]            # _get_intXslice  -> shape (4,)
   r_slice = A[1:3, :]          # _get_sliceXslice-> shape (2, 4)
   r_arr   = A[[0, 2], :]       # _get_sliceXarray(实际经 _major_index_fancy) -> shape (2, 4)
   assert r_int.shape   == (4,)
   assert r_slice.shape == (2, 4)
   assert r_arr.shape   == (2, 4)
   assert np.array_equal(r_arr.toarray(), A.toarray()[[0, 2], :])

   # (2) 构造一个会触发 >2D 报错的索引
   try:
       _ = A[None, 1, 2, None, None]   # None 会把 new_shape 撑到 3 维
   except IndexError as e:
       print(" IndexError:", e)
   ```

3. **需要观察的现象**：三类索引返回形状与稠密 `np.ndarray` 一致；带多个 `None` 的索引让 `new_shape` 长度超过 2，触发 [_index.py:L31-L33](_index.py#L31) 的报错。
4. **预期结果**：三个断言全部通过；`>2D` 的异常信息形如 `Indexing that leads to >2D is not supported by csr format. Try converting to COO format`。
5. 运行结果「待本地验证」。

> 进阶：把上面 `A` 换成 `A.tocsc()` 再跑一遍，断言依旧成立——这正是 `_swap` 机制的功劳。把 `A` 换成 `A.tocoo()`，则 `A[None,1,2,None,None]` **不再报错**，因为 COO 走自己的 N-D `__getitem__`（[_coo.py:L560](_coo.py#L560)）。

#### 4.4.5 小练习与答案

**练习 1**：`A[[0,2],[1,3]]` 与 `A[[0,2],:][:,[1,3]]` 结果一样吗？

> **答**：不一样。前者是 **inner indexing**（行、列数组同形广播），取的是点 `(0,1)` 和 `(2,3)`，结果是 1-D `[1, 11]`；后者先取第 0、2 行得 2×4，再取第 1、3 列得 2×2 子矩阵。这正是 [_index.py:L91-L104](_index.py#L91) 区分 inner/outer 的意义。

**练习 2**：为什么 LIL 要为 `(int,int)` 单独写快速路径，而 CSR 不用？

> **答**：CSR 的 `_get_intXint` [_compressed.py:L546](_compressed.py#L546) 本身就是一次 C++ `get_csr_submatrix` 调用，已经很快，前面再加 `_validate_indices` 的 Python 开销相对可接受；而 LIL 若每次都走完整 `_validate_indices` 再调 Cython，Python 层开销更明显，故为最常见情况开直通车（[_lil.py:L174-L182](_lil.py#L174)）。

---

## 5. 综合实践

把本讲的知识串起来，完成一个「**稀疏子矩阵提取器**」小任务：

1. 用 `diags_array` 构造一个 8×8 的三对角稀疏矩阵 `T`（`from scipy.sparse import diags_array`）。
2. 分别用 CSR、CSC、LIL、DOK 四种格式持有它（`T.tocsr()`、`T.tocsc()`、`T.tolil()`、`T.todok()`）。
3. 写一个函数 `sub(T, rows, cols)`，对四种格式统一执行 `T[rows, cols]`：
   - `rows = slice(1, 5)`、`cols = slice(2, 6)`：验证四种格式返回的子矩阵 `toarray()` 完全相等。
   - `rows = np.array([0, 3, 7])`、`cols = np.array([1, 4, 6])`：观察 inner indexing 下四种格式返回的 1-D 结果是否一致。
4. 对 CSR 版本，尝试 `T[None, 1, 2, None]`，捕获 `IndexError` 并解释：`new_shape` 为何变成 `>2D`？再 `T.tocoo()[None, 1, 2, None]` 验证 COO 不报错。
5. （可选）打开 `_index.py`，在 `__getitem__` 的 2-D 分发处临时加一行 `print(type(row), type(col))`（仅本地学习，不要提交），重新跑第 3 步，亲眼确认每个 key 落到哪个 `_get_*` 方法。

**验收标准**：第 3 步四种格式结果逐元素相等；第 4 步能用自己的话解释「`None` 把大小为 1 的轴塞进 `new_shape`，导致 `len(new_shape)>2`，而 CSR 的 `_allow_nd` 只到 2，故报错并建议转 COO」。

## 6. 本讲小结

- `scipy.sparse` 把索引的「解析 + 调度 + 包装」集中到 `IndexMixin`（[_index.py:L25](_index.py#L25)），各格式只实现原子取数方法，是典型的模板方法/调度模式。
- `__getitem__`（[_index.py:L29](_index.py#L29)）先调 `_validate_indices` 得到 `index` 与 `new_shape`，再按 1-D / 2-D 分流，最后按 `new_shape` 包装结果。
- `_validate_indices`（[_index.py:L298](_index.py#L298)）用两遍扫描把 `int / slice / array / bool / None / Ellipsis / sparse` 一律归一为 `(index, new_shape, arr_pos, none_pos)`，其中 `None` 只影响 `new_shape`、不进 `index`。
- 1-D 三类分发是 `_get_int / _get_slice / _get_array`（[_index.py:L240-L247](_index.py#L240)），由 CSR/CSC/DOK 各自实现；2-D 是 `_get_intXint` 等构成的 3×3 网格（[_index.py:L63-L104](_index.py#L63)）。
- CSR 用 C++ 内核（`get_csr_submatrix`、`csr_row_index`、`csr_column_index1/2`）按压缩数组取数，LIL 为 `(int,int)` 开快速路径并靠 Cython 按行插取，DOK 直接查字典——同一调度、不同实现。
- `>2D` 索引因 `_allow_nd` 最多为 2 而被拒（[_index.py:L31-L33](_index.py#L31)），错误信息提示转 COO；`np.newaxis` 通过给 `new_shape` 塞大小为 1 的轴来实现「升维」。

## 7. 下一步学习建议

- 接着读 **u3-l3（基类公共操作 _base.py）**：`__getitem__` 末尾依赖的 `reshape`、`asformat` 等公共方法都在那里定义。
- 想深入「取数真正怎么搬数据」可看 **u3-l6（C++ 后端与 sparsetools 代码生成）**，理解 `get_csr_submatrix`、`csr_row_index` 等 C++ 内核的签名与生成方式。
- 对 1-D 索引的更多边角可读 [tests/test_indexing1d.py](tests/test_indexing1d.py) 与 [tests/test_array_api.py](tests/test_array_api.py)，它们展示了 `None`、布尔、空数组等极端 key 的预期行为。
- 若关心「写一个会改变稀疏结构的赋值」为何会触发 `SparseEfficiencyWarning`，可对照 `IndexMixin.__setitem__`（[_index.py:L127](_index.py#L127)）与 `_compressed._set_many`，这部分会在 **u6-l5（二次开发与最佳实践）** 展开。
