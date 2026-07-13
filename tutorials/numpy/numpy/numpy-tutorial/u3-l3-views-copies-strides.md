# 视图、拷贝与 strides 技巧

## 1. 本讲目标

本讲承接上一讲「形状操作只改写 shape 与 strides 两个小数组、底层 data 缓冲区不动」的结论，把这套思路推到极致：**人为给定任意的 shape 和 strides 来构造视图**。

学完后你应当能够：

- 说清视图（view）与拷贝（copy）的根本区别，并能用 `np.shares_memory` 判定。
- 读懂 `as_strided` 如何借助 `__array_interface__` 直接改写 ndarray 的元数据，以及它为何「危险」。
- 读懂 `sliding_window_view` 如何在 `as_strided` 之上算出安全的窗口形状与 strides。
- 读懂 `broadcast_to` 如何用 `strides=0` 让一个元素「铺满」一整条轴，并理解它为何只读。
- 在 C 层（`getset.c`）定位 `strides`、`flags`、`data` 等属性的 getter，理解它们的物理含义。

## 2. 前置知识

在进入源码前，先用三句话建立直觉。

**ndarray 的「三件套」。** 一个 ndarray 在内存里由三部分决定：一段连续的字节缓冲区 `data`、一个 `shape` 数组、一个 `strides` 数组。`strides[k]` 表示沿第 k 轴前进一个元素需要跨过的**字节数**（不是元素数）。元素 `(i_0, ..., i_{N-1})` 相对 `data` 起点的字节偏移为：

\[ \text{offset}(i_0,\dots,i_{N-1}) = \sum_{k=0}^{N-1} i_k \cdot s_k \]

其中 \(s_k\) 是 `strides[k]`。这个偏移公式是本讲一切技巧的根基——只要能保证偏移落在合法内存里，你就可以随意搭配 shape 和 strides。

**视图 vs 拷贝。** 视图是**新建一个 ndarray 对象、但 `data` 指针指向旧缓冲区（或其子区域）**的结果；两个数组共享同一段字节。拷贝则是**申请新内存、逐元素搬运**。判据不是「有没有改 shape」，而是「data 缓冲区是否共享」。

**strides 可以是 0 或负数。** `strides[k]=0` 意味着沿第 k 轴前进时字节偏移为 0，于是该轴上所有位置读到同一个元素——这就是广播的底层实现。`strides[k]<0` 则意味着该轴反向遍历，转置和 `[::-1]` 切片正是这么做的。

如果这三点你还觉得抽象，建议先复习上一讲（u3-l2）关于 reshape/transpose 的讲解。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [numpy/lib/_stride_tricks_impl.py](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/lib/_stride_tricks_impl.py) | 本讲主角。实现 `as_strided`、`sliding_window_view`、`broadcast_to`、`broadcast_arrays`、`broadcast_shapes`。全部是纯 Python，通过 `__array_interface__` 与 nditer 操纵底层 C 数组。 |
| [numpy/_core/src/multiarray/getset.c](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/getset.c) | C 层 ndarray 属性的 getter/setter 注册表 `array_getsetlist[]`，包括 `strides`、`flags`、`data`、`__array_interface__` 等。 |
| [numpy/_core/_internal.py](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/_internal.py) | 纯 Python 内部工具。本讲用到其中的 `_ctypes` 类（把 `data`/`strides` 暴露为 ctypes 对象）和 `_view_is_safe`（视图安全性检查）。 |
| [numpy/lib/_array_utils_impl.py](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/lib/_array_utils_impl.py) | 提供 `byte_bounds`，被 `as_strided(check_bounds=True)` 用来检查视图是否越界。 |
| [numpy/lib/tests/test_stride_tricks.py](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/lib/tests/test_stride_tricks.py) | 本模块的测试集，实践环节会阅读其中的断言。 |

> 提示：`as_strided` 等函数虽然定义在 `numpy/lib/_stride_tricks_impl.py`，但经 `numpy/lib/__init__.py` 以 `stride_tricks` 子模块再导出，运行时用 `np.lib.stride_tricks.as_strided` 即可访问。

## 4. 核心概念与源码讲解

### 4.1 视图与拷贝的根本区别

#### 4.1.1 概念说明

很多初学者把「视图」理解为「改了 shape 的数组」，这不够准确。准确的定义是：**视图是一个新的 ndarray 对象，它的 `data` 指针指向另一数组已经拥有的字节缓冲区（的某个起点）**。换句话说，视图**不拥有数据、只拥有元数据**（shape、strides、dtype、flags）。

拷贝则相反：它向系统申请一块全新的内存，把源数组的每个元素按字节搬过去。拷贝之后两个数组互不影响。

判别两者的金标准是 `np.shares_memory(a, b)`：返回 True 说明二者共享至少一个字节，即存在视图关系。

#### 4.1.2 核心流程

一个视图的诞生可以拆成三步：

1. **复制元数据**：从源数组读出 `data` 指针、`dtype`、`flags`。
2. **改写 shape/strides**：按需替换 shape 与 strides 两个小数组（各只有 `ndim` 个 `intp`，几十字节）。
3. **挂 base 引用**：把源数组记为新数组的 `base`，保证源缓冲区在新视图存活期间不被回收。

整个过程**不触碰 `data` 指向的那块大内存**，因此无论数组多大，构造视图的代价都是 O(ndim)，与元素个数无关。

#### 4.1.3 源码精读

视图「只换元数据、不搬数据」的事实，在 C 层 `getset.c` 的 `array_shape_set_internal` 里看得很清楚——它直接把 reshape 出来的新 strides `memcpy` 进老对象，并校验 `data` 指针没变：

[numpy/_core/src/multiarray/getset.c:68-94](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/getset.c#L68-L94) —— 这里先断言 `PyArray_DATA(ret) == PyArray_DATA(self)`（数据指针不变），再把新的 dimensions 与 strides 拷进老对象。这正是「视图」的 C 层定义。

而判断两个数组是否真的共享内存，不能用 `base is` 这种简单比较（视图可能层层嵌套），需要逐字节比对地址区间。`as_strided` 的越界检查正是基于这一思路，我们留到 4.2 详述。

#### 4.1.4 代码实践

1. 实践目标：用 `np.shares_memory` 验证「切片是视图、花式索引是拷贝」。
2. 操作步骤：

   ```python
   import numpy as np
   a = np.arange(12).reshape(3, 4)
   v = a[0, :]            # 基础切片
   c = a[[0, 1]]          # 花式索引
   print(np.shares_memory(a, v))
   print(np.shares_memory(a, c))
   ```
3. 需要观察的现象：两个布尔值。
4. 预期结果：`True`（切片共享内存）、`False`（花式索引是拷贝）。这与 u3-l1 的结论一致。
5. 若本地环境无 numpy，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`a = np.ones(10); b = a.reshape(2,5); c = a + 0`。`b` 和 `c` 分别是视图还是拷贝？
**答案**：`b` 是视图（reshape 尽量不复制，`np.shares_memory(a,b)` 为 True）；`c` 是拷贝（`+0` 触发 ufunc 返回新数组，不共享内存）。

**练习 2**：为什么说「构造视图的代价与数组大小无关」？
**答案**：视图只复制 shape/strides 两个长度为 `ndim` 的小数组并改 `data` 指针，不搬运 `data` 指向的元素缓冲区，代价是 O(ndim) 而非 O(size)。

---

### 4.2 as_strided：直接操纵 strides 的底层原语

#### 4.2.1 概念说明

`as_strided` 是 NumPy 里**最自由也最危险**的视图构造器：你直接给它任意的 shape 和 strides，它就给你一个按这套 shape/strides 解读源缓冲区的视图。它不做任何「合理性」校验（除非你显式传 `check_bounds=True`），因此可以造出越界读取、自重叠、甚至让程序崩溃的视图。

它之所以能做到这一点，靠的不是某个 C 函数，而是一个巧妙的 Python 把戏：**伪造一个 `__array_interface__` 字典，让 `np.asarray` 以为这是一个合法的数组来源**。

#### 4.2.2 核心流程

`as_strided(x, shape, strides)` 的执行流程：

1. 把输入 `x` 转成 ndarray（`copy=None` 尽量不复制），记为 `base`。
2. 复制 `base.__array_interface__` 字典，按需覆盖其中的 `shape` 和 `strides` 两项。
3. 把这个字典挂到一个 `DummyArray` 对象上，再用 `np.asarray(DummyArray(...))` 让 NumPy 按 `__array_interface__` 重新构造一个数组——新数组的 `data` 指针与 `base` 相同，但 shape/strides 已被替换。
4. 用 `_set_dtype` 恢复结构化 dtype（`__array_interface__` 路径会丢失字段信息）。
5. 若 `writeable=False`，把 `flags.writeable` 置为 False。
6. 若 `check_bounds=True`，用 `byte_bounds` 比对视图与 base 的地址区间，越界则抛 `ValueError`。

`DummyArray` 的关键作用是**保活 `base`**：只要视图还在，`base` 就不被垃圾回收，`data` 指针始终有效。

#### 4.2.3 源码精读

先看 `DummyArray`——它唯一的存在意义就是「挂一个 `__array_interface__` 字典，并持有 base 引用」：

[numpy/lib/_stride_tricks_impl.py:15-22](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/lib/_stride_tricks_impl.py#L15-L22) —— `__init__` 把 `interface` 存为 `self.__array_interface__`，把 `base` 存为属性。`np.asarray` 看到 `__array_interface__` 就会按其内容构造数组，而 `self.base` 作为一个属性引用，阻止了 base 被提前回收。

再看 `as_strided` 的主体：

[numpy/lib/_stride_tricks_impl.py:132-148](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/lib/_stride_tricks_impl.py#L132-L148) —— 核心四步：`np.array(x, copy=None, subok=subok)` 取 base；`dict(base.__array_interface__)` 拷贝接口字典；按参数覆盖 `shape`/`strides`；`np.asarray(DummyArray(interface, base=base))` 让 NumPy 按伪造的接口重建数组；最后 `array._set_dtype(base.dtype)` 修复结构化 dtype，并通过 `_maybe_view_as_subclass` 处理子类。

`__array_interface__` 这个字典长什么样？看 C 层的组装函数：

[numpy/_core/src/multiarray/getset.c:265-312](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/getset.c#L265-L312) —— `array_interface_get` 把 `data`（指针 + 是否只读）、`strides`、`descr`、`typestr`、`shape` 五项拼成一个字典返回。注意其中的 strides 来自 `array_protocol_strides_get`：

[numpy/_core/src/multiarray/getset.c:229-236](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/getset.c#L229-L236) —— 若数组是 C 连续的，`__array_interface__` 的 `strides` 字段返回 `None`（按约定 C 连续时 strides 可省略）；否则返回真实 strides 元组。这就是为什么 `as_strided` 在用户不传 strides 时，对 C 连续输入会拿到 `None`——但 `sliding_window_view` 调用时永远显式传了 strides，所以不受影响。

最后看 `check_bounds` 的越界检查：

[numpy/lib/_stride_tricks_impl.py:150-167](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/lib/_stride_tricks_impl.py#L150-L167) —— 先沿着 `base.base` 链回溯到真正拥有数据的根数组，再用 `byte_bounds` 算出根数组的字节区间 `[base_low, base_high]` 与视图的区间 `[view_low, view_high]`，若视图起点低于根起点、或视图终点高于根终点，就抛 `ValueError`。

`byte_bounds` 的算法正好是偏移公式的极值：

[numpy/lib/_array_utils_impl.py:51-62](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/lib/_array_utils_impl.py#L51-L62) —— 从 `data` 指针出发，对每一维：若 stride 为负，最低地址出现在该维最后一个下标处，故 `a_low += (shape-1)*stride`；若 stride 为正，最高地址出现在最后一个下标处，故 `a_high += (shape-1)*stride`。最后 `a_high += bytes_a`（itemsize）得到「恰过最后一个字节」的地址。这个区间就是视图可能访问的全部字节范围。

#### 4.2.4 代码实践

1. 实践目标：用 `as_strided` 把长度 6 的一维数组解读成 3×3 的「自重叠」矩阵，并体会越界风险。
2. 操作步骤：

   ```python
   import numpy as np
   from numpy.lib.stride_tricks import as_strided
   x = np.arange(6)               # data: 0,1,2,3,4,5  itemsize=8
   # 让它看起来像 3x3，每行步长仍是一个元素(8 字节)
   y = as_strided(x, shape=(3, 3), strides=(8, 8), writeable=False)
   print(y)
   # 试试 check_bounds 能否挡住越界
   try:
       as_strided(x, shape=(10,), strides=(8,), check_bounds=True)
   except ValueError as e:
       print("blocked:", e)
   ```
3. 需要观察的现象：`y` 是一个 3×3 矩阵，但其中元素来自仅 6 个槽位的缓冲区，必然有重复；第二条会抛 `ValueError`。
4. 预期结果：`y` 形如 `[[0,1,2],[1,2,3],[2,3,4]]`（自重叠），第 10 个元素会越界被 `check_bounds` 拦下。
5. 注意：**不要**对 `y` 写入（已设 `writeable=False`）。自重叠数组的向量化写行为不确定。结果细节「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `as_strided` 要保留对 `base` 的引用（`DummyArray(..., base=base)`）？
**答案**：视图的 `data` 指针直接指向 `base` 的缓冲区。若 `base` 被垃圾回收，这块内存会被释放，视图再访问就是悬空指针。`DummyArray.base` 持有引用，保证视图存活期间缓冲区不被回收。

**练习 2**：不传 `check_bounds` 时，`as_strided` 会检查越界吗？为什么默认不检查？
**答案**：不会。`check_bounds` 默认为 `None`（假值），检查分支不执行。默认不检查是为了零开销——`as_strided` 的典型用途（滑动窗口、广播）内部已保证合法，逐次检查会拖慢热路径；把安全责任交给调用方，并提供显式开关。

**练习 3**：`__array_interface__` 的 `strides` 字段在什么情况下是 `None`？
**答案**：当数组 C 连续时为 `None`（见 `array_protocol_strides_get`），因为 C 连续数组的 strides 可由 shape 与 itemsize 唯一确定，省略可减少传输开销。

---

### 4.3 sliding_window_view：安全的滑动窗口

#### 4.3.1 概念说明

`as_strided` 太自由、太危险。`sliding_window_view` 是它在「滑动窗口」这一常见场景下的**安全封装**：你只需告诉它窗口大小和轴向，它替你算出正确的 shape 与 strides，再交给 `as_strided` 构造视图，并默认 `writeable=False`。

「滑动窗口」就是把一个长度为 N 的数组，看成 N−W+1 个长度为 W 的子数组。例如 `[0,1,2,3,4,5]` 取窗口 3，得到 4 个窗口 `[0,1,2]、[1,2,3]、[2,3,4]、[3,4,5]`。这 4×3=12 个元素其实全部来自原来的 6 个槽位——靠的就是 strides 重用。

#### 4.3.2 核心流程

设输入 `x` 的 shape 为 \(d\)、strides 为 \(s\)，窗口沿 `axis` 这组轴、大小为 \(w\)。输出 shape 的构造规则是「**先裁剪、再拼接**」：

1. **裁剪**：每个被窗口化的轴 \(k\)，其长度从 \(d_k\) 减为 \(d_k - (w_k - 1)\)（窗口起点可取的位置数）。
2. **拼接**：把窗口大小 \(w\) 作为若干新轴追加到末尾。

strides 的构造更简单：**原 strides 原样保留，再追加每个窗口轴对应的原 stride**。因为「在窗口内沿窗口轴前进一格」就等于「在原数组沿该轴前进一格」，stride 自然是 \(s_{\text{axis}[j]}\)。

形式化地，若 `axis = (a_0, ..., a_{m-1})`：

\[ \text{out\_shape} = \big(d_0',\dots,d_{N-1}'\big) \oplus \big(w_0,\dots,w_{m-1}\big),\quad d_{a_j}' = d_{a_j} - w_j + 1 \]

\[ \text{out\_strides} = \big(s_0,\dots,s_{N-1}\big) \oplus \big(s_{a_0},\dots,s_{a_{m-1}}\big) \]

其中 \(\oplus\) 表示拼接。

#### 4.3.3 源码精读

strides 与 shape 的拼接就在这两行：

[numpy/lib/_stride_tricks_impl.py:433-442](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/lib/_stride_tricks_impl.py#L433-L442) —— `out_strides = x.strides + tuple(x.strides[ax] for ax in axis)` 把窗口轴的原 stride 追加到末尾；`x_shape_trimmed[ax] -= dim - 1` 把每个窗口轴长度减去「窗口大小−1」；`out_shape = tuple(x_shape_trimmed) + window_shape` 拼出最终形状。这正是上面两个公式的直译。

算好之后，直接调 `as_strided`：

[numpy/lib/_stride_tricks_impl.py:443-444](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/lib/_stride_tricks_impl.py#L443-L444) —— 把算好的 `out_strides`、`out_shape` 交给 `as_strided`，并透传 `subok` 与 `writeable`（默认 False）。注意它**没有**传 `check_bounds=True`：因为按上述公式算出的视图必然落在原数组范围内（窗口起点最大为 \(d_k-w_k\)，窗口内最远到 \(d_k-1\)），数学上已保证不越界，无需再花开销检查。

函数前半段是参数归一化与校验：

[numpy/lib/_stride_tricks_impl.py:409-431](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/lib/_stride_tricks_impl.py#L409-L431) —— 把标量 `window_shape`/`axis` 归一成元组；`axis=None` 时默认对所有轴开窗，并要求 `window_shape` 长度等于 `x.ndim`；否则用 `normalize_axis_tuple(..., allow_duplicate=True)` 归一化轴号（允许同一轴开多次窗，每次都会进一步裁剪该轴）。

#### 4.3.4 代码实践

1. 实践目标：用 `sliding_window_view` 看清窗口的形状与共享内存特性。
2. 操作步骤：

   ```python
   import numpy as np
   from numpy.lib.stride_tricks import sliding_window_view
   x = np.arange(6)
   v = sliding_window_view(x, 3)
   print(v.shape, v)
   print(np.shares_memory(x, v))
   # 试着写入——默认只读
   try:
       v[0, 0] = 99
   except ValueError as e:
       print("read-only:", e)
   ```
3. 需要观察的现象：`v.shape == (4, 3)`，且与 `x` 共享内存；写入被拒。
4. 预期结果：`v` 为 `[[0,1,2],[1,2,3],[2,3,4],[3,4,5]]`，`shares_memory` 为 True，写入抛 `ValueError: assignment destination is read-only`。
5. 结果细节「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：对 `x.shape == (3, 4)` 调 `sliding_window_view(x, (2, 2))`，输出 shape 是什么？为什么？
**答案**：`(2, 3, 2, 2)`。前两轴是裁剪后的原轴：第 0 轴 `3-2+1=2`，第 1 轴 `4-2+1=3`；后两轴是窗口大小 `(2, 2)`。

**练习 2**：`sliding_window_view` 为什么默认 `writeable=False`？
**答案**：窗口视图的内存自重叠——同一个元素出现在多个窗口里。若允许写入，改一个位置会同时改变多个窗口的值，向量化写操作结果不确定。默认只读可避免这种隐蔽 bug。

**练习 3**：为什么 `sliding_window_view` 调 `as_strided` 时不传 `check_bounds=True`？
**答案**：按「裁剪 + 拼接」公式算出的视图，窗口起点范围是 `[0, d_k-w_k]`、窗口内最远下标是 `d_k-1`，数学上必然落在原数组地址区间内，无需运行时检查；省掉检查降低热路径开销。

---

### 4.4 broadcast_to：用 stride=0 实现广播视图

#### 4.4.1 概念说明

广播（broadcasting）是 NumPy 的招牌能力：形状 `(3,)` 的数组能和形状 `(4, 3)` 的数组运算，前者被「复制」成 `(4, 3)`。但 NumPy **并不会真的复制**——它构造一个 shape 为 `(4, 3)` 的视图，其中第 0 轴的 stride 是 0。stride=0 意味着沿第 0 轴前进时字节偏移为 0，于是 4 行全部读到同一份原始数据。

`broadcast_to` 就是显式构造这种视图的函数。它的结果**只读**，因为多个位置别名同一内存单元，写入语义无法定义。

#### 4.4.2 核心流程

广播的形状规则（从右向左对齐）：

- 若两轴长度相同，正常对应；
- 若一轴长度为 1，它可被「拉长」到任意长度，方法是**把该轴 stride 设为 0**；
- 否则形状不兼容，报错。

数学上，把 shape \(d\)（长度 N）广播到 \(D\)（长度 M，\(M \ge N\)）：

\[ s'_k = \begin{cases} s_k & \text{若 } d_k = D_{M-N+k} \\ 0 & \text{若 } d_k = 1 \text{（被广播）} \end{cases} \]

新 leading 轴（\(M > N\) 的部分）stride 也为 0。

`broadcast_to` 在 Python 层没有手算这套公式，而是把活儿交给 C 层的 `nditer`：让迭代器以目标 shape 遍历输入数组，再取迭代器产出的视图。

#### 4.4.3 源码精读

`broadcast_to` 是 `array_function_dispatch` 装饰的公开函数，本身只是个壳：

[numpy/lib/_stride_tricks_impl.py:474-517](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/lib/_stride_tricks_impl.py#L474-L517) —— `broadcast_to` 的 docstring 明确说返回「A readonly view on the original array with the given shape. It is typically not contiguous.」末尾 `return _broadcast_to(array, shape, subok=subok, readonly=True)` 把 `readonly=True` 写死。

真正的实现在 `_broadcast_to`：

[numpy/lib/_stride_tricks_impl.py:447-467](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/lib/_stride_tricks_impl.py#L447-L467) —— 先做参数归一与校验（不能把非标量广播成标量、shape 不能含负数），然后关键一步：

[numpy/lib/_stride_tricks_impl.py:456-462](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/lib/_stride_tricks_impl.py#L456-L462) —— 用 `np.nditer((array,), flags=['multi_index','refs_ok','zerosize_ok'], op_flags=['readonly'], itershape=shape, order='C')` 创建一个以目标 shape 迭代输入的迭代器。`itershape=shape` 让 nditer 内部完成广播对齐（包括把长度为 1 的轴 stride 置 0），`it.itviews[0]` 取出这个广播后的视图。整个广播算法其实在 C 层 nditer 里，Python 层只是取视图。

readonly 的处理在最后：

[numpy/lib/_stride_tricks_impl.py:464-466](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/lib/_stride_tricks_impl.py#L464-L466) —— 仅当 `not readonly`（即 `broadcast_arrays` 走的路径）且原数组原本可写时，才把结果设为可写并打上 `_warn_on_write` 标记（写时告警）。`broadcast_to` 走 `readonly=True`，这条分支不执行，于是视图保持只读。

`broadcast_arrays` 复用 `_broadcast_to` 但传 `readonly=False`：

[numpy/lib/_stride_tricks_impl.py:649-655](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/lib/_stride_tricks_impl.py#L649-L655) —— 先算出共同广播形状，再对每个输入：若 shape 已等于目标就直接用，否则调 `_broadcast_to(..., readonly=False)`。注意 docstring 里那条 deprecation 警告（L612-L615）：未来版本会把 `broadcast_arrays` 的输出也设为只读。

#### 4.4.4 代码实践

1. 实践目标：观察广播视图的 strides，确认「第 0 轴 stride=0」。
2. 操作步骤：

   ```python
   import numpy as np
   x = np.array([1, 2, 3])          # shape (3,), strides (8,)
   b = np.broadcast_to(x, (4, 3))
   print(b.shape, b.strides, b.flags.c_contiguous, b.flags.writeable)
   print(np.shares_memory(x, b))
   ```
3. 需要观察的现象：`b.strides` 的第 0 轴是 0；`c_contiguous` 为 False；`writeable` 为 False。
4. 预期结果：`b.shape==(4,3)`、`b.strides==(0,8)`、C 连续为 False、只读为 True、与 x 共享内存为 True。
5. 结果细节「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么广播后的数组通常不是 C 连续？
**答案**：C 连续要求 `strides[k] = strides[k+1] * shape[k+1]` 且最后一轴 `strides[-1] = itemsize`。广播引入的 stride=0 轴破坏了这一关系（如 `(0,8)` 不满足 `0 == 8*3`），故不连续。

**练习 2**：`broadcast_to` 的结果为什么必须只读？
**答案**：stride=0 使得同一内存单元被多个位置别名。若允许写入，赋一个值会同时改掉一整条轴上的多个位置，语义无法定义，故强制只读。

**练习 3**：`_broadcast_to` 为什么用 `nditer` 而不是手算 strides？
**答案**：nditer 的 C 实现已经正确处理广播对齐、零大小数组、多数组协同等边界情况，复用它既减少重复代码又避免 Python 层手算的边界 bug。Python 层只需取 `itviews[0]` 这个现成的广播视图。

---

### 4.5 strides 属性、flags 与 _ctypes

#### 4.5.1 概念说明

前面几个模块都在「写」strides，这一节讲「读」strides 以及与之相关的 flags。它们都是 ndarray 的属性，但**不是普通实例变量**——而是 C 层 `getset.c` 中 `array_getsetlist[]` 登记的 getter 函数，在每次访问时动态从 `PyArrayObject_fields` 结构体里读出来。

理解这一点很重要：`arr.strides` 返回的是一个**新建的 Python 元组**，改它不会改数组；历史上能用的 `arr.strides = ...` 赋值已在 NumPy 2.4 被弃用，正确做法是用 `as_strided`。

#### 4.5.2 核心流程

属性的读写流程：

1. Python 访问 `arr.strides`，触发类型对象上的 getset 描述符。
2. 描述符调用 `array_strides_get`，它读 `PyArray_STRIDES(self)`（指向 strides 数组的指针），用 `PyArray_IntTupleFromIntp` 转成 Python 元组返回。
3. `arr.flags` 同理调 `array_flags_get`，返回一个 `flags` 对象，它包装了 `PyArray_FLAGS(self)` 这个位域。
4. `__array_interface__` 则把 data 指针、strides、shape、typestr、descr 打包成字典——正是 `as_strided` 伪造的那个接口。

#### 4.5.3 源码精读

strides 的 getter 极简：

[numpy/_core/src/multiarray/getset.c:129-133](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/getset.c#L129-L133) —— `array_strides_get` 直接把 C 层 `PyArray_STRIDES(self)` 指向的 `ndim` 个 `npy_intp` 转成 Python 整数元组。每次访问都新建元组，所以 `arr.strides is arr.strides` 为 False。

strides 的 setter 已被弃用：

[numpy/_core/src/multiarray/getset.c:135-149](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/getset.c#L135-L149) —— `array_strides_set` 先发 `DEPRECATE` 告警（NumPy 2.4，2025-05-11），提示改用 `np.lib.stride_tricks.as_strided`。其后是一段复杂的越界校验逻辑（`PyArray_CheckStrides`），仅供历史兼容。

flags 的 getter：

[numpy/_core/src/multiarray/getset.c:43-47](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/getset.c#L43-L47) —— `array_flags_get` 调 `PyArray_NewFlagsObject` 返回一个 `flags` 对象，它包装 `PyArray_FLAGS` 位域并暴露 `c_contiguous`、`f_contiguous`、`owndata`、`writeable`、`aligned`、`writebackifcopy` 等布尔属性。`as_strided` 设 `view.flags.writeable = False` 改的就是这个位域里的 `NPY_ARRAY_WRITEABLE` 位。

data 指针的 getter：

[numpy/_core/src/multiarray/getset.c:240-248](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/getset.c#L240-L248) —— `array_dataptr_get` 返回 `(整数指针, 是否只读)` 二元组。这个「是否只读」正是由 `NPY_ARRAY_WRITEABLE` 与 `NPY_ARRAY_WARN_ON_WRITE` 两个 flag 位算出的，与 `flags.writeable` 同源。

所有这些 getset 都登记在一张表里：

[numpy/_core/src/multiarray/getset.c:743-825](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/getset.c#L743-L825) —— `array_getsetlist[]` 是 `PyGetSetDef` 数组，每项是 `{名字, getter, setter, doc, closure}`。`ndim`、`flags`、`shape`、`strides`、`data`、`itemsize`、`size`、`nbytes`、`base`、`dtype`、`T`、`mT`、`__array_interface__` 等全部在此登记。注意大多数属性只有 getter、没有 setter（`ndim`/`flags`/`data`/`T` 等不可赋值）。

Python 层的 `_ctypes` 类提供另一条访问 strides 的路：

[numpy/_core/_internal.py:307-314](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/_internal.py#L307-L314) —— `strides_as` 把 strides 数组转成指定 ctypes 类型的定长数组。它的 `strides` 属性（[numpy/_core/_internal.py:347-356](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/_internal.py#L347-L356)）调它返回 ctypes 数组，便于直接传给 C 函数。`data` 属性（[numpy/_core/_internal.py:316-333](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/_internal.py#L316-L333)）返回数据指针整数。这条路由 getset.c 的 `array_ctypes_get` 触发（[numpy/_core/src/multiarray/getset.c:250-263](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/getset.c#L250-L263)），它 `import numpy._core._internal` 并调 `_ctypes(self, data_ptr)` 构造对象。

最后补一个与「视图安全」相关但常被忽略的函数。直接 `.view(newtype)` 改 dtype 时，NumPy 会调 `_view_is_safe` 防止把含对象引用的内存被重解释为非对象类型（反之亦然），避免悬空引用计数：

[numpy/_core/_internal.py:498-526](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/_internal.py#L498-L526) —— `_view_is_safe` 先用 C 层 `_is_view_safe_cast` 做精确判定，再对 `hasobject` 的 dtype 额外抛 `TypeError`。注意 `as_strided` **绕过了这道检查**（它走 `__array_interface__` 而非 `.view()`），这也是它危险的原因之一——它不会阻止你把对象数组的内存按 float 去读。

#### 4.5.4 代码实践

1. 实践目标：验证 `arr.strides` 每次返回新元组，并读懂 `flags` 各位。
2. 操作步骤：

   ```python
   import numpy as np
   a = np.arange(12, dtype=np.float64).reshape(3, 4)
   print(a.strides, a.strides is a.strides)   # 新元组，is 为 False
   f = a.flags
   for name in ["c_contiguous", "f_contiguous", "owndata", "writeable", "aligned"]:
       print(name, getattr(f, name))
   # 看看 __array_interface__ 的全貌
   import pprint; pprint.pprint(a.__array_interface__)
   ```
3. 需要观察的现象：`strides is strides` 为 False；C 连续数组 `c_contiguous=True, f_contiguous=False, owndata=True, writeable=True, aligned=True`；接口字典里有 `data`/`strides`(可能为 None)/`shape`/`typestr`/`descr`/`version`。
4. 预期结果：如上。若 `a` 是 C 连续，`__array_interface__['strides']` 为 `None`（呼应 4.2.3）。
5. 结果细节「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `a.strides is a.strides` 是 False？
**答案**：`array_strides_get` 每次都调 `PyArray_IntTupleFromIntp` 新建一个 Python 元组，不是返回缓存的实例变量，所以两次访问得到不同对象。

**练习 2**：`as_strided` 设 `view.flags.writeable = False` 时，改的是哪个 flag 位？它和 `view[0] = 1` 报错有什么关系？
**答案**：改的是 `PyArray_FLAGS` 中的 `NPY_ARRAY_WRITEABLE` 位。清掉后，任何赋值在 `PyArray_FailUnlessWriteable` 检查时抛 `ValueError: assignment destination is read-only`。

**练习 3**：为什么说 `as_strided` 绕过了 `_view_is_safe` 的安全检查？
**答案**：`_view_is_safe` 是 `.view()` 路径上的检查；`as_strided` 走 `__array_interface__` 由 `np.asarray` 重建数组，不经过 `.view()`，所以不会拦截「把对象数组按数值类型解读」这类危险重解释。安全责任完全在调用方。

---

## 5. 综合实践

把本讲三个核心模块（`as_strided`、`sliding_window_view`、`broadcast_to`）串起来，完成一个任务：**用两种方式计算长度为 100 的数组的滑动平均（窗口 5），并理解为什么要禁写**。

### 步骤 1：用 `sliding_window_view` 计算滑动平均

```python
import numpy as np
from numpy.lib.stride_tricks import sliding_window_view, as_strided

x = np.arange(100, dtype=np.float64)
v = sliding_window_view(x, 5)          # shape (96, 5)
ma1 = v.mean(axis=-1)                   # shape (96,)
print(ma1.shape, ma1[:5])
```

### 步骤 2：用 `as_strided` 复现同样的视图

参照 4.3 的公式：窗口轴 stride 就是原数组 stride（float64 的 `itemsize=8`，故 strides=(8,)），shape 是 `(100-5+1, 5) = (96, 5)`。

```python
w = 5
out_strides = x.strides + (x.strides[0],)   # (8, 8)
out_shape   = (x.shape[0] - w + 1, w)       # (96, 5)
v2 = as_strided(x, shape=out_shape, strides=out_strides, writeable=False)
ma2 = v2.mean(axis=-1)
print(np.array_equal(ma1, ma2), np.shares_memory(x, v2))
```

### 步骤 3：体会为何要 `writeable=False`

```python
print(v2.flags.writeable)           # False
try:
    v2[0, 0] = 999.0
except ValueError as e:
    print("blocked:", e)
```

### 需要观察的现象与预期结果

- `ma1` 与 `ma2` 形状均为 `(96,)`，`np.array_equal(ma1, ma2)` 为 True——两种方式等价。
- `np.shares_memory(x, v2)` 为 True——`as_strided` 没有复制数据。
- `v2.flags.writeable` 为 False，写入抛 `ValueError`。

### 为什么禁写（文字说明）

滑动窗口视图的内存是**自重叠**的：`v2[0]` 与 `v2[1]` 共享了 4 个元素（`x[1..4]`）。如果允许写入，`v2[0,1] = 999` 会同时改变 `v2[1,0]` 的值，于是向量化赋值（如 `v2[:] = something`）的执行顺序不同就会得到不同结果，行为不确定。把 `writeable` 置为 False，是在 C 层清掉 `NPY_ARRAY_WRITEABLE` flag 位，让任何赋值在 `PyArray_FailUnlessWriteable` 处立即抛错，把这类隐蔽 bug 变成显式失败。`sliding_window_view` 默认 `writeable=False` 正是这个原因；手动用 `as_strided` 时也应当养成同样习惯。

> 本实践未假定你已经运行；如本地未构建 numpy，相关输出「待本地验证」。可对照测试集 [numpy/lib/tests/test_stride_tricks.py](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/lib/tests/test_stride_tricks.py) 中对 `sliding_window_view` 的断言来核对预期。

## 6. 本讲小结

- 视图与拷贝的根本区别在于**是否共享 data 缓冲区**，判据是 `np.shares_memory`；构造视图只改 shape/strides 两个小数组，代价 O(ndim)。
- `as_strided` 通过伪造 `__array_interface__` 字典（挂在 `DummyArray` 上并保活 base），让 `np.asarray` 按任意 shape/strides 重建数组；它默认不校验越界，是最自由也最危险的视图原语。
- `sliding_window_view` 在 `as_strided` 之上用「裁剪原轴 + 追加窗口轴、strides 追加窗口轴原 stride」的公式构造安全窗口视图，默认只读。
- `broadcast_to` 用 `nditer` 的 `itershape` 完成广播对齐，本质是把长度为 1 的轴 stride 置 0，使一个元素铺满一整条轴；结果只读且通常不连续。
- `strides`/`flags`/`data`/`__array_interface__` 都是 `getset.c` 的 `array_getsetlist[]` 登记的 getter，动态从 C 结构体读出；`strides` 赋值已弃用，改用 `as_strided`。
- `as_strided` 绕过了 `.view()` 路径上的 `_view_is_safe` 检查，因此不会拦截危险的对象数组重解释，安全责任在调用方。

## 7. 下一步学习建议

- **下一讲 u3-l4（广播机制原理与实现）**：本讲只点了「stride=0 实现广播」的结论，下一讲会系统讲广播三条规则、`broadcast_shapes` 的实现，以及 nditer 在广播中的角色，建议接着读。
- **继续阅读源码**：`numpy/_core/src/multiarray/nditer_api.c` 看 nditer 的 C 实现，理解 `_broadcast_to` 调用的 `itviews` 是怎么来的；`numpy/_core/src/multiarray/mapping.c` 看基础切片如何像 `as_strided` 一样只改 shape/strides。
- **横向对比**：回看 u3-l2 的 reshape/transpose，你会发现它们与 `as_strided` 共享同一套「只改元数据」的原理，区别只在 strides 的计算方式与安全校验的强弱。
