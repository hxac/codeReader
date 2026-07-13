# 广播机制：broadcast_to/arrays/shapes

## 1. 本讲目标

学完本讲，读者应该能够：

- 用「0 步长」这一内存层直觉解释广播为什么是零拷贝的、为什么结果天然只读。
- 读懂 `broadcast_to` / `broadcast_arrays` / `broadcast_shapes` 三个公开函数的实现，并能区分它们各自返回什么（只读视图 / 可写但警告的视图 / 纯形状元组）。
- 读懂两个内部内核 `_broadcast_to`（用 `np.nditer(itershape=...)` 真正生成视图）与 `_broadcast_shape`（用旧 `np.broadcast` 只算形状）的分工。
- 用 `broadcast_shapes` 预测任意多个形状广播后的结果形状，并用 `broadcast_arrays` 验证。

## 2. 前置知识

本讲紧接 u5-l1。那里建立了三个关键概念，本讲直接复用：

- **步长（stride）**：沿某一维前进一格需要跨过的字节数。步长可以是 0、负数或大于 `itemsize`。
- **视图（view）**：换一套 `shape`/`strides` 去读同一块内存，不复制数据。
- **`__array_interface__` 协议**：`as_strided` 通过 `DummyArray` 挂载这个接口字典来注入任意 shape/strides。

本讲的核心直觉只有一句话：**广播 = 插入步长为 0 的维度**。

当把一个形状 `(3,)` 的数组广播到 `(2, 3)` 时，numpy 并不会把数据复制两份，而是在前面「补」出一个大小为 2 的新维度，并把这个新维度的步长设为 0。于是沿这个新维度前进时，内存指针原地不动——同一行 `[1,2,3]` 被「读」了两遍，却只存一份。这正是广播零拷贝的根本原因，也直接承接 u5-l1 里 `as_strided` 自重叠内存的警告：广播结果的多个元素指向同一块内存，所以写它是危险的、默认只读。

NumPy 的广播规则可以形式化为一句话：先左对齐形状，再逐维取最大值。

\[ \text{result}[d] = \max_i\bigl(\text{shape}_i[d]\bigr) \]

兼容的充要条件是：对每个维度 \(d\) 和每个数组 \(i\)，要么 \(\text{shape}_i[d] = 1\)（被拉伸），要么 \(\text{shape}_i[d] = \text{result}[d]\)（已匹配），否则抛 `ValueError`。「拉伸 size-1 维」在内存层等价于「该维步长置 0」。

> 术语提示：本讲出现的 `dispatcher + impl 双函数写法`、`@set_module`、薄再导出模块等概念均在 u1-l2 讲过，这里只点出不再展开。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `numpy/lib/_stride_tricks_impl.py` | 全部实现所在。`broadcast_to` / `broadcast_arrays` / `broadcast_shapes` 三个公开函数与 `_broadcast_to` / `_broadcast_shape` 两个内部内核都在这里。 |
| `numpy/lib/stride_tricks.py` | 薄再导出模块。**只**搬 `as_strided` 与 `sliding_window_view`，**不**搬广播函数——后者不经此模块。 |
| `numpy/__init__.py` | 顶层入口。直接 `from .lib._stride_tricks_impl import` 把三个广播函数挂到 `np.` 命名空间。 |
| `numpy/lib/tests/test_stride_tricks.py` | 对应测试，含 `test_broadcast_to_succeeds/raises`、`test_broadcast_shape`、`test_broadcast_shapes_succeeds/raises`，是理解边界行为的最佳材料。 |

一个值得注意的导出细节：`_stride_tricks_impl.py` 的 `__all__` 只列了三个广播函数（见下文 L12），而薄模块 `stride_tricks.py` 却只导入 `as_strided`/`sliding_window_view`。也就是说三个广播函数是「直接上 `np.`」的，不经过 `np.lib.stride_tricks` 这一层——这与 u5-l1 里 `as_strided` 经薄模块暴露的路径不同。

## 4. 核心概念与源码讲解

### 4.1 broadcast_to：把单个数组广播到目标形状（只读视图）

#### 4.1.1 概念说明

`broadcast_to(array, shape)` 是最简单的广播入口：把一个数组「拉大」到指定 `shape`，返回一个**只读视图**。它解决的问题是「我想让一个形状较小的数组参与按目标形状的逐元素运算，但又不想真的复制数据」。

关键性质有三：

1. **零拷贝**：结果是原数组的视图，多个输出元素可能指向同一块内存（因为拉伸维的步长为 0）。
2. **只读**：默认 `readonly=True`，写入会抛 `ValueError: assignment destination is read-only`。这正是 u5-l1 里 `as_strided` 自重叠警告的同源考量——写一个会被多处看见的位置，行为不可预测。
3. **不连续**：广播视图通常 `C_CONTIGUOUS` 为 False。

#### 4.1.2 核心流程

```text
broadcast_to(array, shape)
  └─ _broadcast_to(array, shape, subok, readonly=True)
       ├─ 1. shape 规整为元组
       ├─ 2. array 转 ndarray（subok 控制是否保留子类）
       ├─ 3. 两道校验：标量→非标量 非法；shape 含负数 非法
       ├─ 4. np.nditer((array,), itershape=shape) 真正做广播
       ├─ 5. 取 it.itviews[0] 作为广播视图
       ├─ 6. _maybe_view_as_subclass 还原子类
       └─ 7. readonly=True 时跳过「可写+警告」分支，保持只读
```

广播规则在步骤 4 由 `nditer` 的 `itershape` 参数强制执行：若 `array` 无法广播到 `shape`，`nditer` 会抛 `ValueError`。

#### 4.1.3 源码精读

公开函数本身就是一行委托，`readonly=True` 是它「只读」特性的来源：

[_stride_tricks_impl.py:L474-L517](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_stride_tricks_impl.py#L474-L517) — `broadcast_to` 用 `@array_function_dispatch(_broadcast_to_dispatcher, module='numpy')` 装饰（dispatcher 只返回 `(array,)`，参与 NEP-18 派发；`module='numpy'` 把 `__module__` 钉到 `numpy`），函数体仅 `return _broadcast_to(array, shape, subok=subok, readonly=True)`。

`module='numpy'` 加上顶层直接导入，决定了它的可见路径：

[numpy/__init__.py:L583-L587](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/__init__.py#L583-L587) — 顶层 `from .lib._stride_tricks_impl import (broadcast_arrays, broadcast_shapes, broadcast_to,)`，不经薄模块。

对比薄模块只搬另两个函数：

[stride_tricks.py:L1](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/stride_tricks.py#L1) — `from ._stride_tricks_impl import __doc__, as_strided, sliding_window_view`，没有广播函数。

`__all__` 的「缺席」也呼应这一点：

[_stride_tricks_impl.py:L12](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_stride_tricks_impl.py#L12) — `__all__ = ["broadcast_to", "broadcast_arrays", "broadcast_shapes"]`，注意它**不**含 `as_strided`/`sliding_window_view`，与薄模块的导入清单恰好互补。

#### 4.1.4 代码实践

1. 实践目标：验证 `broadcast_to` 返回只读视图，且拉伸维的步长为 0。
2. 操作步骤：

   ```python
   import numpy as np
   x = np.arange(3)                 # shape (3,), strides (8,)
   y = np.broadcast_to(x, (2, 3))   # 广播到 (2, 3)
   print(y.shape, y.strides, y.flags.writeable, y.flags.c_contiguous)
   y[0, 0] = 99                     # 期望抛 ValueError
   ```
3. 需要观察的现象：`y.shape` 为 `(2, 3)`；`y.strides` 形如 `(0, 8)`——第一维步长为 0 正是「拉伸」的内存证据；`writeable` 为 `False`；最后一行抛 `ValueError: assignment destination is read-only`。
4. 预期结果：与上述一致。`y.base` 应与 `x` 共享同一块内存（可通过 `np.shares_memory(y, x)` 为 `True` 印证）。
5. 若无法确定运行结果，明确写「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`np.broadcast_to(np.arange(3), (3,))` 的结果与输入是什么关系？ strides 是否变化？

> **答案**：形状已匹配，无需拉伸，结果是与输入同形状同数据的只读视图，strides 不变（仍是 `(8,)`），但 `writeable` 被强制为 `False`。

**练习 2**：下列哪个会抛 `ValueError`？为什么？
(a) `np.broadcast_to(np.arange(3), (2, 3))`
(b) `np.broadcast_to(np.arange(3), (3, 2))`
(c) `np.broadcast_to(np.ones((1, 2)), (0, 2))`

> **答案**：(b) 抛错。`(3,)` 左对齐成 `(1, 3)`，目标第二维是 2 而 `1→2` 不兼容（3≠2 且 3≠1）。(a) 兼容（`(1,3)→(2,3)`，第一维 1 拉伸为 2）。(c) 兼容且结果形状 `(0, 2)`——size-0 在广播里是合法的，对应测试 `test_broadcast_to_succeeds` 中的 `[np.ones((1, 2)), (0, 2), np.ones((0, 2))]`。

---

### 4.2 _broadcast_to：nditer + itershape 内核

#### 4.2.1 概念说明

`_broadcast_to` 是 `broadcast_to` 与 `broadcast_arrays` 共用的真正实现。它做三件事：**校验、用 `nditer` 生成广播视图、按调用方意愿决定只读还是可写带警告**。理解它就理解了「广播视图从哪里来」。

`broadcast_to` 传 `readonly=True`（只读）；`broadcast_arrays` 传 `readonly=False`（可写但带弃用警告）。同一个内核，两种调用姿态。

#### 4.2.2 核心流程

```text
_broadcast_to(array, shape, subok, readonly)
  ├─ shape = tuple(shape) if iterable else (shape,)     # 标量也变元组
  ├─ array = np.array(array, copy=None, subok=subok)    # 转 ndarray
  ├─ if not shape and array.shape:  raise ValueError     # 校验 A：非标量→标量
  ├─ if any(size < 0 for size in shape): raise ValueError # 校验 B：负尺寸
  ├─ it = np.nditer((array,), flags=[...], op_flags=['readonly'],
  │                itershape=shape, order='C')           # nditer 强制广播
  ├─ broadcast = it.itviews[0]                           # 取广播后的视图
  ├─ result = _maybe_view_as_subclass(array, broadcast)  # 还原子类
  └─ if not readonly and array.flags._writeable_no_warn:
        result.flags.writeable = True
        result.flags._warn_on_write = True               # 弃用警告路径
```

`itviews[0]` 是 `nditer` 暴露的「按 `itershape` 广播后的视图」。`nditer` 在构造时就会按广播规则校验 `array` 是否能广播到 `itershape`，不能则抛 `ValueError`——这就是「非法形状」报错的来源。

#### 4.2.3 源码精读

[_stride_tricks_impl.py:L447-L467](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_stride_tricks_impl.py#L447-L467) — `_broadcast_to` 全貌。关键片段：

```python
shape = tuple(shape) if np.iterable(shape) else (shape,)
array = np.array(array, copy=None, subok=subok)
if not shape and array.shape:
    raise ValueError('cannot broadcast a non-scalar to a scalar array')
if any(size < 0 for size in shape):
    raise ValueError('all elements of broadcast shape must be non-negative')
...
it = np.nditer(
    (array,), flags=['multi_index', 'refs_ok', 'zerosize_ok'] + extras,
    op_flags=['readonly'], itershape=shape, order='C')
with it:
    broadcast = it.itviews[0]
result = _maybe_view_as_subclass(array, broadcast)
if not readonly and array.flags._writeable_no_warn:
    result.flags.writeable = True
    result.flags._warn_on_write = True
return result
```

逐点说明：

- `not shape and array.shape`：目标形状为空元组 `()`（标量）但输入是非标量数组，无法广播，报错。
- `any(size < 0 ...)`：禁止负尺寸（注意：numpy 切片里的 `-1` 在这里不是「推断」而是非法）。
- `flags` 里 `zerosize_ok` 允许结果含 0 维；`refs_ok` 允许对象数组；`multi_index` 保留多维索引能力。
- `itershape=shape`：这是广播的真正执行点——`nditer` 把 `array` 广播到这个形状。
- `it.itviews[0]`：取第一个操作数广播后的视图，这就是零拷贝的广播结果。
- 末尾的 `if not readonly` 分支：`broadcast_to` 传 `readonly=True` 时直接跳过，保持只读；`broadcast_arrays` 传 `readonly=False` 时进入，置可写并打上 `_warn_on_write` 标记（写入时发弃用警告）。

`_maybe_view_as_subclass` 在 u5-l1 已讲过：`subok=True` 且输入是 ndarray 子类时，用 `.view(type=...)` 还原子类并触发 `__array_finalize__`。

#### 4.2.4 代码实践

1. 实践目标：通过阅读测试用例表，反推 `_broadcast_to` 两道校验的边界。
2. 操作步骤：打开 [test_stride_tricks.py:L268-L284](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_stride_tricks.py#L268-L284) 的 `test_broadcast_to_raises`，逐条对照源码解释报错原因。
3. 需要观察的现象：用例 `[(1,), -1]` 与 `[(1,), (-1,)]` 走「校验 B（负尺寸）」；用例 `[(3,), ()]`、`[(1,2),(2,1)]` 走「`nditer` 广播不兼容」；用例 `[(1,1),(1,)]` 属于「非标量→标量」之外但被 `nditer` 拒。
4. 预期结果：能对每条用例指出是「校验 A / 校验 B / nditer 广播失败」中的哪一类。
5. 待本地验证：可选地用 `np.nditer((np.zeros((3,)),), itershape=(2,))` 手工复现一次广播失败。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `broadcast_to` 的结果默认只读，而 `broadcast_arrays` 的结果可写（带警告）？

> **答案**：两者共用 `_broadcast_to`，差别在 `readonly` 参数。`broadcast_to` 传 `readonly=True`，跳过末尾的可写分支，保持只读；`broadcast_arrays` 传 `readonly=False`，进入分支置 `writeable=True` 并打 `_warn_on_write`，所以写入会发弃用警告。设计上 `broadcast_arrays` 的可写行为自 1.17 起被标记弃用，未来会改为只读。

**练习 2**：`_broadcast_to` 里 `extras = []` 是一个空列表，却被拼进 `flags`。这个设计意图是什么？

> **答案**：留作扩展钩子。当前恒为空，等价于 `flags=['multi_index','refs_ok','zerosize_ok']`；保留 `+ extras` 使得将来可以在不改动函数签名的前提下追加 nditer 标志，是一种向前兼容的写法。

---

### 4.3 _broadcast_shape：只算形状的内核（旧广播器 + 分块）

#### 4.3.1 概念说明

`_broadcast_shape(*args)` 接收若干数组，返回它们广播后的**形状元组**，不分配任何结果数据。它是 `broadcast_arrays` 与 `broadcast_shapes` 共同依赖的形状计算内核。

它有两个不太显眼但很关键的设计决策：

1. **用旧 `np.broadcast` 而非 `np.nditer`**：因为 `nditer` 对 size-0 数组的处理「不一致」（源码注释原话）。
2. **分块处理 64+ 个参数**：`np.broadcast` 直接最多接 64 个参数，超过要分块。

#### 4.3.2 核心流程

```text
_broadcast_shape(*args)
  ├─ b = np.broadcast(*args[:64])              # 前 64 个先广播
  ├─ for pos in range(64, len(args), 63):      # 之后每 63 个一批
  │     b = broadcast_to(0, b.shape)           # 把累计形状变回 0 维数组
  │     b = np.broadcast(b, *args[pos:pos+63]) # 与下一批继续广播
  └─ return b.shape
```

注意循环里 `broadcast_to(0, b.shape)`：`np.broadcast` 不接受「另一个 `np.broadcast` 对象」作参数（它会把 broadcast 对象当标量），所以必须把累计形状物化成一个真正的 0 维数组（标量 `0` 广播到 `b.shape`，得到一个形状为 `b.shape`、值为 0 的数组），再喂给下一轮 `np.broadcast`。这是个精巧的 workaround。

#### 4.3.3 源码精读

[_stride_tricks_impl.py:L520-L534](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_stride_tricks_impl.py#L520-L534) — `_broadcast_shape` 全貌：

```python
def _broadcast_shape(*args):
    """Returns the shape of the arrays that would result from broadcasting the
    supplied arrays against each other.
    """
    # use the old-iterator because np.nditer does not handle size 0 arrays
    # consistently
    b = np.broadcast(*args[:64])
    # unfortunately, it cannot handle 64 or more arguments directly
    for pos in range(64, len(args), 63):
        # ironically, np.broadcast does not properly handle np.broadcast
        # objects (it treats them as scalars)
        # use broadcasting to avoid allocating the full array
        b = broadcast_to(0, b.shape)
        b = np.broadcast(b, *args[pos:(pos + 63)])
    return b.shape
```

注释把两道坑都说清了：上面「`nditer` 对 size-0 不一致」解释为何用 `np.broadcast`；中间「`np.broadcast` 把 `np.broadcast` 对象当标量」解释为何每轮要 `broadcast_to(0, b.shape)` 物化一次。

测试 [test_stride_tricks.py:L287-L301](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_stride_tricks.py#L287-L301) 覆盖了边界：100 个 `(1,2)` 数组广播得 `(1,2)`（验证分块正确），以及 gh-5862 回归——32 个 `(2,)` 加 1 个标量得 `(2,)`、32 个 `(2,)` 加 32 个 `(3,)` 抛错。

#### 4.3.4 代码实践

1. 实践目标：验证分块逻辑在 64+ 参数下仍正确。
2. 操作步骤：

   ```python
   import numpy as np
   from numpy.lib._stride_tricks_impl import _broadcast_shape
   # 100 个 (1,2) 数组
   arrs = [np.ones((1, 2))] * 100
   print(_broadcast_shape(*arrs))          # 期望 (1, 2)
   # 触发分块边界：正好 64 个走首段，第 65 个进循环
   print(_broadcast_shape(*([np.ones((1,2))]*64 + [np.ones((3,2))])))  # 期望 (3, 2)
   ```
3. 需要观察的现象：两组都正确返回，证明 `range(64, len(args), 63)` 的分块在 64/127 边界无误。
4. 预期结果：`(1, 2)` 与 `(3, 2)`。
5. 待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：为什么循环步长是 `63` 而不是 `64`？

> **答案**：每轮 `np.broadcast(b, *args[pos:pos+63])` 里第一个参数 `b` 已经占了一个位置，`np.broadcast` 上限是 64 个参数，所以还能再塞 63 个新数组。步长 63 保证每批不超限。

**练习 2**：`_broadcast_shape()` 不传任何参数返回什么？为什么？

> **答案**：返回 `()`（空元组，标量形状）。`args[:64]` 为空，`np.broadcast()` 无参数得到一个代表标量的 broadcast 对象，其 `.shape` 为 `()`。对应测试 `assert_equal(_broadcast_shape(), ())`。

---

### 4.4 broadcast_shapes：纯形状 API（零字节占位数组技巧）

#### 4.4.1 概念说明

`broadcast_shapes(*shapes)` 是面向「形状」而非「数组」的公开 API（1.20 新增）。它接收若干形状元组（或整数），返回它们广播后的形状元组。典型用途：在真正分配数组之前，先预测多数组运算的结果形状。

它的实现用了一个漂亮技巧：**用零字段结构化 dtype 构造零字节数组**，把形状信息「挂」在不占内存的占位数组上，再交给 `_broadcast_shape` 计算。

#### 4.4.2 核心流程

```text
broadcast_shapes(*args)            # args 是形状元组/整数
  ├─ _size0_dtype = np.dtype([])   # 零字段结构化 dtype，itemsize=0
  ├─ arrays = [np.empty(x, dtype=_size0_dtype) for x in args]
  │      # 每个占位数组形状为 x，但占用 0 字节
  └─ return _broadcast_shape(*arrays)
```

`np.dtype([])` 是一个没有任何字段的结构化 dtype，其 `itemsize` 为 0。于是 `np.empty((5,6,7), dtype=np.dtype([]))` 虽然形状是 `(5,6,7)`，却分配 0 字节内存——纯粹用来「携带形状」给 `_broadcast_shape`。

#### 4.4.3 源码精读

[_stride_tricks_impl.py:L537-L581](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_stride_tricks_impl.py#L537-L581) — `_size0_dtype` 与 `broadcast_shapes`：

```python
_size0_dtype = np.dtype([])

@set_module('numpy')
def broadcast_shapes(*args):
    ...
    arrays = [np.empty(x, dtype=_size0_dtype) for x in args]
    return _broadcast_shape(*arrays)
```

要点：

- `@set_module('numpy')`（注意：**不是** `array_function_dispatch`）——因为参数是形状元组而非数组，不涉及 NEP-18 `__array_function__` 派发，所以不需要 dispatcher。这与 `broadcast_to`/`broadcast_arrays` 形成对照：后两者接数组，需要 dispatcher；`broadcast_shapes` 接形状，只用 `set_module` 把 `__module__` 钉到 `numpy`。
- `np.empty(x, dtype=_size0_dtype)`：`x` 可以是元组或整数（`np.empty` 接受整数作 1D 形状），与文档「tuples of ints, or ints」对应。
- 模块级常量 `_size0_dtype` 复用，避免每次调用都重建 dtype。

测试 [test_stride_tricks.py:L304-L359](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_stride_tricks.py#L304-L359) 覆盖了从 `[[()], ()]` 到 `[(6, 7), (5, 6, 1), (7,), (5, 1, 7)], (5, 6, 7)` 的各种情形，以及 size-0 维的特殊规则（如 `[(1, 0), (0, 1)]` 得 `(0, 0)`）。

#### 4.4.4 代码实践

1. 实践目标：用 `broadcast_shapes` 预测一个复杂多形状广播的结果，并亲手验证零字节技巧。
2. 操作步骤：

   ```python
   import numpy as np
   # 预测
   print(np.broadcast_shapes((6, 7), (5, 6, 1), (7,), (5, 1, 7)))  # 期望 (5, 6, 7)
   # 验证 _size0_dtype 零字节
   d = np.dtype([])
   a = np.empty((1000, 1000), dtype=d)
   print(a.nbytes, a.shape)   # 期望 0 与 (1000, 1000)
   ```
3. 需要观察的现象：广播结果 `(5, 6, 7)`；占位数组 `nbytes` 为 0 但 `shape` 为 `(1000, 1000)`。
4. 预期结果：与上述一致，印证「形状-only」数组不耗内存。
5. 待本地验证。

#### 4.4.5 小练习与答案

**练习 1**：`np.broadcast_shapes((1, 0), (0, 1))` 为什么结果是 `(0, 0)` 而不是 `(1, 1)`？

> **答案**：广播逐维取最大值，但规则是「size-1 拉伸到目标，其余必须相等」。两数组第一维分别是 1 和 0：1 可拉伸到 0，故第一维目标 0；第二维分别是 0 和 1：1 可拉伸到 0，故第二维目标 0。结果 `(0, 0)`。size-0 在广播里会「传染」：一旦某维出现 0 且无冲突，结果该维就是 0。

**练习 2**：`broadcast_shapes` 为什么不需要 `array_function_dispatch`？

> **答案**：NEP-18 的 `__array_function__` 协议是为「接收 ndarray 的函数」设计的，让第三方数组类型能拦截运算。`broadcast_shapes` 的入参是形状元组/整数，不是数组，没有「第三方数组类型要拦截」的场景，所以只需 `@set_module('numpy')` 设定归属，不加 dispatcher。

---

### 4.5 broadcast_arrays：多数组对齐广播（可写但警告）

#### 4.5.1 概念说明

`broadcast_arrays(*args)` 把任意多个数组**互相广播**到同一形状，返回一个元组，每个元素是对应输入的广播视图。典型场景：让两个不同形状的数组逐元素比较或运算前的对齐。

它和 `broadcast_to` 的两个关键差异：

1. **多输入**：先用 `_broadcast_shape` 算出公共形状，再对每个数组调 `_broadcast_to`。
2. **可写带警告**：传 `readonly=False`，结果可写但写入会发弃用警告（未来版本会改成只读）。

还有一个实现细节：它**故意不用 `nditer`**，因为 `nditer` 有 64 数组上限，而 `broadcast_arrays` 要支持任意多个数组。

#### 4.5.2 核心流程

```text
broadcast_arrays(*args, subok=False)
  ├─ args = [np.array(_m, copy=None, subok=subok) for _m in args]  # 全转 ndarray
  ├─ shape = _broadcast_shape(*args)        # 算公共形状（用旧 np.broadcast）
  ├─ result = [array if array.shape == shape
  │            else _broadcast_to(array, shape, subok=subok, readonly=False)
  │            for array in args]           # 已匹配的直传，否则广播
  └─ return tuple(result)
```

注意「已匹配的直传」优化：若某输入的形状已经等于公共形状，就不必再调 `_broadcast_to`，直接用原数组（仍是可写的）。

#### 4.5.3 源码精读

[_stride_tricks_impl.py:L588-L656](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_stride_tricks_impl.py#L588-L656) — `broadcast_arrays` 全貌。核心片段：

```python
# nditer is not used here to avoid the limit of 64 arrays.
# Otherwise, something like the following one-liner would suffice:
# return np.nditer(args, flags=['multi_index', 'zerosize_ok'],
#                  order='C').itviews

args = [np.array(_m, copy=None, subok=subok) for _m in args]
shape = _broadcast_shape(*args)
result = [array if array.shape == shape
          else _broadcast_to(array, shape, subok=subok, readonly=False)
          for array in args]
return tuple(result)
```

要点：

- 注释明说：用 `nditer` 一行就能写完，但 `nditer` 限 64 数组，所以改走 `_broadcast_shape` + 逐个 `_broadcast_to`。这是与 `_broadcast_to`（单数组、用 `nditer`）的分工差异。
- `readonly=False`：与 `broadcast_to` 的 `readonly=True` 对照，决定了结果可写带警告。
- 文档字符串 [L612-L615](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_stride_tricks_impl.py#L612-L615) 明确：自 1.17 起写入被标记弃用，未来会置 `writable=False`。其实现机制正是 4.2 节 `_broadcast_to` 末尾的 `_warn_on_write`。
- dispatcher [_broadcast_arrays_dispatcher:L584-L585](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_stride_tricks_impl.py#L584-L585) 返回 `args`（全部数组都参与 NEP-18 派发），与 `broadcast_to` 的 dispatcher 只返回 `(array,)` 形成对照——多输入函数要把所有数组参数都交给协议。

文档还给了一个常用惯用法 [L637](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_stride_tricks_impl.py#L637)：`[np.array(a) for a in np.broadcast_arrays(x, y)]`——用 `np.array(a)` 把非连续的广播视图物化成连续副本，便于安全写入。

#### 4.5.4 代码实践

1. 实践目标：用 `broadcast_arrays` 对齐两个不同形状数组，验证拉伸维步长为 0，并观察写入的弃用警告。
2. 操作步骤：

   ```python
   import numpy as np
   import warnings
   x = np.array([[1, 2, 3]])      # (1, 3)
   y = np.array([[4], [5]])       # (2, 1)
   bx, by = np.broadcast_arrays(x, y)
   print(bx.shape, by.shape)      # 期望 (2, 3) (2, 3)
   print(bx.strides, by.strides)  # 期望 x 的第一维步长为 0，y 的第二维步长为 0
   print(np.shares_memory(bx, x), np.shares_memory(by, y))  # 期望 True True
   with warnings.catch_warnings(record=True) as w:
       warnings.simplefilter("always")
       bx[0, 0] = 99
       print([str(wi.category) for wi in w])  # 观察是否有 DeprecationWarning
   ```
3. 需要观察的现象：两者形状同为 `(2, 3)`；`bx.strides` 形如 `(0, 8)`（第一维拉伸）、`by.strides` 形如 `(8, 0)`（第二维拉伸）；`shares_memory` 均为 `True`（零拷贝）；写入触发 `DeprecationWarning`。
4. 预期结果：与上述一致。写入警告行为属于弃用路径，未来版本可能改为直接抛错——此部分**待本地验证**当前 numpy 版本的具体警告形态。
5. 若只需对齐后比较，推荐改用 `bx == by` 这类只读运算，避免触发警告。

#### 4.5.5 小练习与答案

**练习 1**：`broadcast_arrays` 里「`array if array.shape == shape else _broadcast_to(...)`」这个分支判断省掉了什么？

> **答案**：省掉了「形状已匹配」时的冗余广播。若某输入形状已等于公共形状，直接用原数组（零开销、保持原可写性），不必再过一遍 `_broadcast_to`。例如广播 `(2,3)` 与 `(2,1)` 时，公共形状 `(2,3)`，第一个数组直传，只对第二个调 `_broadcast_to`。

**练习 2**：为什么 `broadcast_arrays` 不直接用 `np.nditer(args, ...).itviews` 一行实现？

> **答案**：`nditer` 最多接 64 个操作数，而 `broadcast_arrays` 要支持任意多个数组。所以改用 `_broadcast_shape`（本身用旧 `np.broadcast` + 63 分块支持任意多参数）算形状，再逐个数组调 `_broadcast_to`（单数组用 `nditer` 无 64 限制问题）。源码注释里保留了那行被否决的一行实现作为说明。

---

## 5. 综合实践

把三个公开 API 与「0 步长」直觉串起来：

```python
import numpy as np

# 1. 给定两个不同形状的数组
a = np.arange(6).reshape(2, 3)   # (2, 3)
b = np.arange(4).reshape(4, 1)   # (4, 1)

# 2. 先用 broadcast_shapes 预测广播结果形状（不分配数据）
shape = np.broadcast_shapes(a.shape, b.shape)
print("predicted shape:", shape)   # 期望 (4, 3)

# 3. 用 broadcast_arrays 实际对齐，验证形状与 strides
ba, bb = np.broadcast_arrays(a, b)
assert ba.shape == shape == bb.shape
print("ba.strides:", ba.strides)   # 第一维步长期望为 0（a 沿 0 维拉伸 2→4）
print("bb.strides:", bb.strides)   # 第二维步长期望为 0（b 沿 1 维拉伸 1→3）
assert np.shares_memory(ba, a) and np.shares_memory(bb, b)

# 4. 用只读的 broadcast_to 把 b 广播到目标形状，做一次安全的逐元素运算
bb_ro = np.broadcast_to(b, shape)  # readonly=True
result = (ba + bb_ro)              # 两个广播视图相加，结果是新数组
print("result:\n", result)
print("result writeable:", result.flags.writeable)  # 期望 True（新数组，非广播视图）
```

**需要观察与解释的现象**：

- `broadcast_shapes` 给出 `(4, 3)`，与第 2 步预测一致。
- `ba` 第一维步长为 0：因为 `a` 形状 `(2,3)` 要拉到 `(4,3)`，第一维从 2 拉到 4，对应步长置 0——这正是「广播 = 插入 0 步长维度」的直接证据。
- `bb` 第二维步长为 0：`b` 形状 `(4,1)` 第二维从 1 拉到 3。
- `ba + bb_ro` 的结果是全新分配的数组（可写），与两个广播视图本身只读/带警告无关——运算会物化结果。
- 若把第 4 步换成直接 `ba += 1`（写入广播视图），应触发弃用警告；这印证了「广播视图写不安全」的设计取舍。

**待本地验证**：strides 的具体字节数取决于 `itemsize`（int64 为 8），但「拉伸维步长为 0」这一结论与 dtype 无关。

## 6. 本讲小结

- **广播 = 插入步长为 0 的维度**：拉伸 size-1 维在内存层就是把该维步长置 0，所以广播结果是零拷贝视图，且天然只读（多处指向同一内存，写入不安全）。
- 三个公开函数同源而异用：`broadcast_to`（单数组→只读视图，`readonly=True`）、`broadcast_arrays`（多数组→可写带警告视图，`readonly=False`）、`broadcast_shapes`（只算形状，零字节占位数组）。
- 两个内部内核分工明确：`_broadcast_to` 用 `np.nditer(itershape=...)` 生成单数组广播视图；`_broadcast_shape` 用旧 `np.broadcast` 算多数组公共形状（因 `nditer` 对 size-0 不一致），并按 63 分块绕开 64 参数上限。
- `broadcast_shapes` 的零字节技巧：`_size0_dtype = np.dtype([])` 使 `np.empty(shape, dtype=...)` 只携带形状不占内存。
- 导出路径上，三个广播函数经 `module='numpy'` + 顶层 `numpy/__init__.py` 直接取名挂到 `np.`，**不**经薄模块 `stride_tricks.py`（后者只搬 `as_strided`/`sliding_window_view`）。
- `broadcast_shapes` 不带 `array_function_dispatch`（参数是形状非数组），而 `broadcast_to`/`broadcast_arrays` 带（dispatcher 分别返回 `(array,)` 与全部 `args`）。

## 7. 下一步学习建议

- **横向对照**：回到 u5-l1 的 `as_strided` / `sliding_window_view`，把本讲的「0 步长广播视图」与那里的「自定义步长滑窗视图」放在一起看——两者都是「换 shape/strides 读同一块内存」的同族机制，差别只在参数是手工给定还是由广播规则自动推导。
- **向上追 nditer**：`_broadcast_to` 把广播真正交给 `np.nditer`。建议阅读 `numpy/_core/src/multiarray/nditer` 相关实现或在 Python 层实验 `np.nditer` 的 `itershape` / `itviews`，理解「广播视图」在 C 层是如何构造的。
- **NEP-18 落点**：本讲多次出现 `array_function_dispatch`，可结合 u1-l2 阅读 `numpy/_core/overrides.py`，理解 dispatcher 返回的数组参数如何被第三方数组类型的 `__array_function__` 拦截。
- **下一站（u6）**：进入数值处理函数（`diff`/`gradient`/`trapezoid` 等），那里会频繁依赖广播来对齐多维输入，本讲的形状预测与只读视图直觉将反复用到。
