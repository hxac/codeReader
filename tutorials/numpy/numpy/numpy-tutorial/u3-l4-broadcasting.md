# 广播机制原理与实现

## 1. 本讲目标

学完本讲后，你应当能够：

- 用三条规则判定任意一组形状能否互相广播，并算出广播后的形状。
- 解释「广播 = 把长度为 1 的轴的 stride 置 0」这一底层机制，明白它为何不复制数据。
- 在源码中定位 `broadcast_to` / `broadcast_arrays` / `broadcast_shapes` 的实现，并说明它们如何借助 `np.nditer` 与 C 层 `PyArray_Broadcast` 完成工作。
- 动手用 `broadcast_to` 制造一个广播视图，观察其 strides、连续性与只读性，并解释原因。

## 2. 前置知识

本讲承接上一讲 u3-l3（视图、拷贝与 strides 技巧），你需要已经掌握：

- ndarray 的三件套：`data` 缓冲区、`shape`、`strides`。
- 偏移公式：元素 \((i_0,\ldots,i_{nd-1})\) 的字节地址为

\[
\text{addr}(\mathbf{i}) = \text{data} + \sum_{k=0}^{nd-1} i_k \cdot \text{strides}[k].
\]

- 视图不复制 `data`，只复制 `shape`/`strides`；`np.shares_memory` 可判是否共享。
- `as_strided` 可用任意 strides 造视图，包括 strides 取 0。
- C 连续条件：\(\text{strides}[k] = \text{strides}[k+1]\cdot\text{shape}[k+1]\)，且最末轴 \(\text{strides}[nd-1]=\text{itemsize}\)。

本讲要回答的核心问题是：当两个形状不同的数组做逐元素运算时，NumPy 如何「假装」它们形状相同、又不真的复制数据？答案正是 strides=0 的广播。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| `doc/source/user/basics.broadcasting.rst` | 广播三条规则的权威说明（用户文档）。 |
| `doc/source/user/theory.broadcasting.rst` | 仅 16 行的孤儿（orphan）存根，把读者重定向到 `basics.broadcasting`。本讲会如实说明它只是个指针。 |
| `numpy/lib/_stride_tricks_impl.py` | `broadcast_to` / `broadcast_arrays` / `broadcast_shapes` 三个公开函数的 Python 实现，以及内部 `_broadcast_to` / `_broadcast_shape`。 |
| `numpy/_core/numeric.py` | 顶层 `np.broadcast`（C 类型）的再导出入口。 |
| `numpy/_core/src/multiarray/iterators.c` | C 层 `PyArray_Broadcast` 函数：广播形状求解与「strides 置 0」的真正实现。 |
| `numpy/_core/include/numpy/ndarraytypes.h` | `PyArrayMultiIterObject_fields` 结构体，即 `np.broadcast` 对象的内存布局。 |
| `numpy/_core/src/multiarray/multiarraymodule.c` | 把 C 类型注册为顶层名字 `broadcast`。 |

## 4. 核心概念与源码讲解

### 4.1 广播规则理论

#### 4.1.1 概念说明

「广播」（broadcasting）描述 NumPy 在逐元素运算中对形状不同的数组的处理方式：在不复制数据的前提下，把较小的数组「拉伸」到与较大数组相同的形状。

文档原文（`basics.broadcasting.rst`）：

> The term broadcasting describes how NumPy treats arrays with different shapes during arithmetic operations. ... Broadcasting provides a means of vectorizing array operations so that looping occurs in C instead of Python. It does this without making needless copies of data.

注意「拉伸」只是概念上的类比；实际上 NumPy 从不真的复制，而是用 strides=0 让同一个内存单元被反复读取（见 4.3）。

#### 4.1.2 核心流程

广播三条规则（`basics.broadcasting.rst` 第 66–107 行）：

1. **对齐**：从最末尾（最右）的轴开始，逐轴向前比较。
2. **兼容**：两轴兼容当且仅当二者相等，或其中一个为 1。
3. **取大**：结果轴长 = 各输入对应轴长的最大值；缺失的轴（输入 ndim 较小）视为长度 1。

形式化地，设 $n$ 个数组形状分别为 $s^{(j)}=(s^{(j)}_0,\ldots,s^{(j)}_{d_j-1})$，先左填充 1 到公共维数 $d=\max_j d_j$，得到 $\tilde{s}^{(j)}$，则

\[
\text{shape}_k = \max_j \tilde{s}^{(j)}_k,\qquad
\forall j:\ \tilde{s}^{(j)}_k\in\{1,\ \text{shape}_k\}.
\]

若任一 $j$ 不满足上式，抛出 `ValueError: operands could not be broadcast together`。

举例（来自文档）：

```
A      (4d array):  8 x 1 x 6 x 1
B      (3d array):      7 x 1 x 5
Result (4d array):  8 x 7 x 6 x 5
```

B 左填充 1 → $(1,7,1,5)$，逐轴取大且每个输入该轴要么 1 要么等于结果，故合法。

#### 4.1.3 源码精读

权威规则文本位于 `basics.broadcasting.rst` 的 "General broadcasting rules" 一节：

[doc/source/user/basics.broadcasting.rst:66-107](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/doc/source/user/basics.broadcasting.rst#L66-L107) —— 这是 NumPy 广播三条规则的官方陈述：从最右轴起对齐、相等或为 1 即兼容、结果取最大；并以 `8x1x6x1` 与 `7x1x5` → `8x7x6x5` 给出示例。

而规格中点名的 `theory.broadcasting.rst` 实际只是一个孤儿存根：

[doc/source/user/theory.broadcasting.rst:1-16](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/doc/source/user/theory.broadcasting.rst#L1-L16) —— 全文仅一个 `:orphan:` 标记加一句 "Please refer to the updated basics.broadcasting document."，真正的理论在 `basics.broadcasting.rst`，不要在此存根里找规则。

#### 4.1.4 代码实践

1. 实践目标：用纸笔验证一组形状能否广播，再用 `np.broadcast_shapes` 核对。
2. 操作步骤：预测下列各组的广播形状，再运行代码。
   ```python
   import numpy as np
   print(np.broadcast_shapes((8,1,6,1), (7,1,5)))   # 预测 (8,7,6,5)
   print(np.broadcast_shapes((5,4), (4,)))           # 预测 (5,4)
   try:
       np.broadcast_shapes((3,), (4,))               # 预测报错
   except ValueError as e:
       print("ValueError:", e)
   ```
3. 需要观察的现象：前两组返回元组，第三组抛 `ValueError`。
4. 预期结果：`(8,7,6,5)`、`(5,4)`、第三组 "could not be broadcast"。

#### 4.1.5 小练习与答案

练习 1：`(15,3,5)` 与 `(3,1)` 的广播结果形状？
答案：左填充后者为 `(1,3,1)`，逐轴取大得 `(15,3,5)`。

练习 2：为什么 `(2,1)` 与 `(8,4,3)` 不能广播？
答案：左填充 `(1,2,1)` 与 `(8,4,3)` 比较，倒数第二轴 `2 vs 4`，既不等也非 1，故不兼容，抛 `ValueError`。

### 4.2 broadcast_to / broadcast_arrays / broadcast_shapes

#### 4.2.1 概念说明

文档规则只描述「能否广播」与「结果形状」，但有时你需要显式拿到那个广播后的数组，而不真的写一次运算。NumPy 提供三个公开函数（全部定义在 `numpy/lib/_stride_tricks_impl.py`，`__all__` 第 12 行只导出这三个）：

- `broadcast_to(array, shape)`：把单个数组广播到指定 shape，返回**只读视图**。
- `broadcast_arrays(*args)`：把多个数组互相广播到同一 shape，返回一组视图。
- `broadcast_shapes(*args)`：只算形状不建数组，纯整数元组输入、元组输出。

三者都不复制数据，结果通常是「非连续且只读」的视图——原因见 4.3。

#### 4.2.2 核心流程

`broadcast_to` 是后两者的基石，流程如下：

1. 校验 shape 非负、标量情形合法（不能把非标量广播到标量）。
2. 调 `np.nditer((array,), itershape=shape, order='C')`，让迭代器把数组广播到目标 shape。
3. 取 `it.itviews[0]`——这就是广播视图。
4. 处理子类与只读标志后返回。

`broadcast_shapes` 则更巧妙：它为每个输入形状造一个**零字节 dtype** 的空数组（`np.empty(shape, dtype=np.dtype([]))`），再调 `_broadcast_shape` 走 `np.broadcast` 求形状。零字节 dtype 让「建数组」几乎零成本，纯粹为了借广播算法算形状。

`broadcast_arrays` 先用 `_broadcast_shape` 求公共 shape，再对每个输入调 `_broadcast_to`（注意 `readonly=False`，故可写但会触发告警），形状已相等的输入直接原样返回。

#### 4.2.3 源码精读

`_broadcast_to`——借助 nditer 制造广播视图的核心：

[numpy/lib/_stride_tricks_impl.py:447-467](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/lib/_stride_tricks_impl.py#L447-L467) —— 用 `np.nditer((array,), flags=['multi_index','refs_ok','zerosize_ok'], op_flags=['readonly'], itershape=shape, order='C')` 把 array 广播到目标形状，`it.itviews[0]` 即所得视图；默认 `readonly=True`，仅当原数组可写且允许时才打开 `_warn_on_write`。

公开入口 `broadcast_to` 只是一行转发：

[numpy/lib/_stride_tricks_impl.py:474-517](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/lib/_stride_tricks_impl.py#L474-L517) —— `broadcast_to` 转发到 `_broadcast_to(..., readonly=True)`，docstring 明确返回值是 "A readonly view on the original array with the given shape. It is typically not contiguous. Furthermore, more than one element of a broadcasted array may refer to a single memory location."

`broadcast_shapes` 用零字节 dtype 借广播算法算形状：

[numpy/lib/_stride_tricks_impl.py:537-581](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/lib/_stride_tricks_impl.py#L537-L581) —— `_size0_dtype = np.dtype([])` 是零字节结构化 dtype；`broadcast_shapes` 为每个形状造一个该 dtype 的 `np.empty`，再调 `_broadcast_shape`。

`_broadcast_shape` 处理 ≥64 个参数的分块与 `np.broadcast` 调用：

[numpy/lib/_stride_tricks_impl.py:520-534](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/lib/_stride_tricks_impl.py#L520-L534) —— 注意注释 "use the old-iterator because np.nditer does not handle size 0 arrays consistently" 与 64 个参数上限：先 `np.broadcast(*args[:64])`，之后每 63 个一批续算。

`broadcast_arrays` 明确不用 nditer 以避开 64 数组上限：

[numpy/lib/_stride_tricks_impl.py:644-656](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/lib/_stride_tricks_impl.py#L644-L656) —— 注释说明改用 `_broadcast_shape` + 逐个 `_broadcast_to(..., readonly=False)` 是为了不受 nditer 64 数组限制；形状已相等的输入直接原样返回，其余才广播。

#### 4.2.4 代码实践

1. 实践目标：分别用三个函数对同一组形状操作，观察返回类型、strides 与是否共享内存。
2. 操作步骤：
   ```python
   import numpy as np
   x = np.arange(3)                                # shape (3,), int64
   b = np.broadcast_to(x, (4, 3))
   print("b.shape      =", b.shape)
   print("b.strides    =", b.strides)
   print("b.writeable  =", b.flags.writeable)
   print("shares_memory=", np.shares_memory(x, b))

   a0, a1 = np.broadcast_arrays(x, np.arange(4).reshape(4, 1))
   print("a0.strides   =", a0.strides, " a1.strides =", a1.strides)
   print("shapes       =", np.broadcast_shapes((3,), (4, 1)))
   ```
3. 需要观察的现象：`b.strides` 为 `(0, 8)`，`writeable=False`，`shares_memory=True`；`a0` 的 strides 含 0（第一维广播），`a1` 的 strides 含 0（第二维广播）；`broadcast_shapes` 返回 `(4, 3)`。
4. 预期结果：确认广播视图共享原数据、strides 中含 0、且只读。
5. 若无法本地运行，明确标注「待本地验证」strides 数值随 itemsize 变化（int64 为 8，int32 为 4）。

#### 4.2.5 小练习与答案

练习 1：`np.broadcast_to(np.arange(3), (4,3))` 与 `np.tile(np.arange(3), (4,1))` 结果数值相同，二者内存行为有何不同？
答案：前者是只读视图、共享原 3 个元素（strides 含 0）、不复制；后者是全新 C 连续数组、复制了 12 个元素、可写。

练习 2：为什么 `broadcast_shapes` 要用 `np.dtype([])` 这种零字节 dtype？
答案：只为借广播算法算形状，不需要任何真实数据；零字节 dtype 使 `np.empty` 几乎不分配内存，既快又省。

### 4.3 nditer 在广播中的角色与 C 层 strides=0 机制

#### 4.3.1 概念说明

`_broadcast_to` 为何调 `np.nditer` 就能得到广播视图？因为 nditer 是 NumPy 的「多数组同步迭代器」，其核心能力之一就是：给定若干输入和目标 `itershape`，自动把每个输入广播到该形状并同步遍历。广播的本质——strides=0——正是在迭代器内部落实的。

要真正看清「strides 置 0」这件事，最清晰的源码是 C 层老式多迭代器 `PyArray_Broadcast`。它服务于 `np.broadcast` 类型（即 `PyArrayMultiIterObject`），与 nditer 共享同一广播思想，但代码最可读。

#### 4.3.2 核心流程

`PyArray_Broadcast` 接收一个含多个 `PyArrayIterObject` 的 `PyArrayMultiIterObject`，做两件事：

1. **求广播形状**：`nd = max(各输入 ndim)`；对每一维 `i`，遍历各输入，遇到非 1 的尺寸就记下，遇到不同且都非 1 就报 "shape mismatch"。
2. **改写每个迭代器的 strides**：对每个输入的每一维，若该维是「补出来的」（输入 ndim 较小，`k<0`）或「输入该维长度为 1 但广播长度更大」，则把 `strides[j]` 置 0；否则沿用原 stride。

strides=0 的数学含义：在偏移公式 \(\text{addr}(\mathbf{i}) = \text{data} + \sum_k i_k\cdot\text{strides}[k]\) 中，若 \(\text{strides}[k]=0\)，则下标 \(i_k\) 不影响地址——同一内存单元被反复读取，这正是「拉伸」的实现，且零拷贝。

#### 4.3.3 源码精读

`PyArrayMultiIterObject_fields` 结构体——`np.broadcast` 对象的内存布局：

[numpy/_core/include/numpy/ndarraytypes.h:1249-1277](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/include/numpy/ndarraytypes.h#L1249-L1277) —— 字段 `numiter` / `size` / `index` / `nd` / `dimensions[]` / `iters[]`，注释明确 "Any object passed to PyArray_Broadcast must be binary compatible with this structure"；`iters` 数组大小在内部构建时为 64。

`PyArray_Broadcast` 求形状部分：

[numpy/_core/src/multiarray/iterators.c:1149-1184](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/iterators.c#L1149-L1184) —— 先 `nd = max(ndim)`，再逐维取最大尺寸；`tmp==1` 时 `continue`（跳过长度 1 的轴），不同且都非 1 时 `set_shape_mismatch_exception` 返回 -1。

`PyArray_Broadcast` 置 strides=0 的关键几行：

[numpy/_core/src/multiarray/iterators.c:1198-1227](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/iterators.c#L1198-L1227) —— 对每个迭代器逐维：若 `k<0`（补出来的轴）或 `PyArray_DIMS(it->ao)[k] != mit->dimensions[j]`（该输入此轴长度为 1 而广播长度更大），则 `it->strides[j]=0`；否则沿用 `PyArray_STRIDES(it->ao)[k]`。这就是广播用 strides=0 实现的铁证。

`np.broadcast` 类型的注册：

[numpy/_core/src/multiarray/multiarraymodule.c:5241-5242](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/multiarraymodule.c#L5241-L5242) —— 把 `PyArrayMultiIter_Type` 注册为顶层名字 `broadcast`，再经 numeric.py 再导出为 `np.broadcast`。

`np.broadcast` 在 numeric.py 的再导出：

[numpy/_core/numeric.py:30-76](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/numeric.py#L30-L76) —— 第 30 行 `broadcast,` 从 `_multiarray_umath` 导入，第 76 行纳入 `__all__`，于是 `np.broadcast` 即 C 类型 `PyArrayMultiIter_Type`。

迭代 `np.broadcast` 时每次产出一个标量元组（每个输入一个标量），由 `arraymultiter_next` 实现：

[numpy/_core/src/multiarray/iterators.c:1399-1419](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/iterators.c#L1399-L1419) —— `arraymultiter_next` 每步用 `PyArray_ToScalar(it->dataptr, it->ao)` 从每个子迭代器取一个标量，组装成长度为 `numiter` 的元组返回；`PyArray_ITER_NEXT` 推进各子迭代器（strides=0 的轴不会真的移动 `dataptr`）。

#### 4.3.4 代码实践

1. 实践目标：用 `np.broadcast` 直观验证「strides=0 让少量真实元素被重复读取」。
2. 操作步骤：
   ```python
   import numpy as np
   a = np.arange(3)                  # (3,)
   b = np.arange(4).reshape(4, 1)    # (4,1)
   m = np.broadcast(b, a)            # np.broadcast 对象
   print("shape  =", m.shape)
   print("numiter =", m.numiter)
   print("size   =", m.size)
   for i, (x, y) in enumerate(np.broadcast(b, a)):
       if i < 6:
           print(i, "b_elem=", x, " a_elem=", y)
   ```
3. 需要观察的现象：`m.shape==(4,3)`、`m.numiter==2`、`m.size==12`；迭代时 `b` 的元素按行各重复 3 次、`a` 的元素按列各重复 4 次，但底层分别只有 4 个与 3 个真实元素。
4. 预期结果：直观看到「12 次配对」由 4+3=7 个真实元素经 strides=0 复用而成，而非 12 个拷贝。
5. 若对「`dataptr` 不移动」存疑，可对照 4.3.3 的 `PyArray_ITER_NEXT`+strides=0 自行推导：步进长度为 0 时指针不动。

#### 4.3.5 小练习与答案

练习 1：`np.broadcast_to(np.arange(3), (4,3))` 的 strides 是多少？它为何不是 C 连续？
答案：`(0, 8)`（int64）。C 连续的 `(4,3)` int64 应为 `(24, 8)`（即 \(\text{strides}[0]=\text{shape}[1]\cdot\text{itemsize}=3\times8\)），而广播视图 \(\text{strides}[0]=0\neq24\)，故不满足 C 连续条件；\(\text{strides}[1]=8=\text{itemsize}\) 说明最末轴仍逐元素前进。

练习 2：广播视图为何默认只读？
答案：strides 含 0 时多个下标指向同一内存单元，写入会让一次赋值改到多个位置且顺序依赖、结果不可预测，故 `broadcast_to` 强制 `readonly=True`（见 4.2.3 `_broadcast_to` 的 `readonly` 参数）。

## 5. 综合实践

把本讲三块知识串起来：用广播实现「每行减去该行均值」的零拷贝归一化，并验证它确实是视图。

任务步骤：

1. 造一个 `(4, 5)` 的随机数组 `M`：
   ```python
   import numpy as np
   rng = np.random.default_rng(0)
   M = rng.random((4, 5))
   ```
2. 用 `M.mean(axis=1, keepdims=True)` 得到 `(4,1)` 的行均值 `r`。
3. 直接 `N = M - r`，利用广播得到 `(4,5)` 的去均值结果。
4. 用 `np.broadcast_shapes(M.shape, r.shape)` 预测运算时的广播形状，应为 `(4,5)`。
5. 用 `rv = np.broadcast_to(r, M.shape)` 显式造一个广播视图，打印 `rv.strides`，确认第一维 stride 非 0、第二维 stride 为 0（因为 `r` 的第二维长度为 1）。
6. 解释：`M - r` 没有把 `r` 复制成 `(4,5)`，而是 ufunc 在 C 层用 strides=0 逐元素读取 `r`。

预期结果：

- `np.broadcast_shapes((4,5), (4,1))` 返回 `(4, 5)`。
- `r` 是 `(4,1)` 的 C 连续 float64 数组，strides 为 `(8, 8)`（最末轴 itemsize=8，第一轴 = `shape[1]*strides[1] = 1*8`）。
- `rv = np.broadcast_to(r, (4,5))` 的 strides 为 `(8, 0)`：第一维沿用 `r.strides[0]=8`，第二维因 `r` 该轴长度 1 而置 0。
- `np.shares_memory(r, rv)` 为 `True`。
- `N` 每行均值近似 0（可用 `N.mean(axis=1)` 验证）。

> 注：第 5 步若你对「第二维 stride 为 0」存疑，可对照 4.3.3 的 `PyArray_Broadcast` 分支自行推导：`r` 形状 `(4,1)`，广播到 `(4,5)`，第二维 `1 != 5` → 落入 `it->strides[j]=0` 分支。

## 6. 本讲小结

- 广播三条规则：从最右轴对齐、相等或为 1 即兼容、结果取最大；不满足抛 `ValueError`。
- 「拉伸」只是概念；底层靠把长度为 1 的轴的 stride 置 0，使同一内存单元被反复读取，零拷贝。
- `broadcast_to` / `broadcast_arrays` / `broadcast_shapes` 都在 `numpy/lib/_stride_tricks_impl.py`；`broadcast_to` 是基石，借助 `np.nditer(..., itershape=...)` 的 `itviews[0]` 拿到广播视图。
- 广播视图通常非连续且只读：strides 含 0 破坏了 C 连续条件，写入会因多下标指向同一单元而不可预测。
- `broadcast_shapes` 用零字节 dtype 借广播算法算形状，几乎零成本；`np.broadcast`（C 类型 `PyArrayMultiIter_Type`）由 `PyArray_Broadcast` 实现，是看清 strides=0 的最直接源码。
- 规格里点名的 `theory.broadcasting.rst` 实为孤儿存根，真正的理论在 `basics.broadcasting.rst`。

## 7. 下一步学习建议

- 下一讲 u4-l1（ufunc 概念与 frompyfunc）会把广播与「逐元素 + 类型解析」结合，看 ufunc 如何在 C 层调用广播迭代器对多输入求同形。
- 想深入迭代器本身，读 u9-l2（nditer）与 `numpy/_core/src/multiarray/nditer_api.c`，理解 `itershape`、`op_flags`、`multi_index` 的完整能力。
- 想看广播在归约里的作用，回到 u4-l4（归约、累积与方法分发），对照 `_methods.py` 中 `keepdims=True` 如何为广播归一化铺路。
