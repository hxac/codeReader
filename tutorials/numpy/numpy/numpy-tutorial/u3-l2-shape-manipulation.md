# 形状操作：reshape、transpose 与轴

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚「形状操作」为什么通常只是改写 `shape` 与 `strides` 两个小数组，而底层 `data` 缓冲区原封不动。
- 判断 `reshape` 在什么条件下返回视图、什么条件下被迫拷贝，并能从源码层面解释原因。
- 解释 `transpose` / `swapaxes` 如何通过重排 `strides` 实现轴置换，而完全不搬动数据。
- 阅读并理解 `moveaxis` / `rollaxis` 如何在 Python 层把「搬轴」问题转化为一次 `transpose`。
- 在源码中定位这些操作的 Python 封装与 C 实现，并能给出永久链接与行号。

本讲承接上一讲「索引机制」中「视图 vs 拷贝」的判定思路——那里我们用 `strides` 解释切片为何是视图，这里我们把同一套模型推广到所有形状操作。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。这些概念在 u1-l4（ndarray 核心属性）中已初步引入，这里从「形状操作」的角度再强调一次。

**ndarray 的三件套。** 一个 `ndarray` 在内存里由三样东西决定：一块连续的原始数据缓冲区 `data`、一个 `shape` 数组（每一维的长度）、一个 `strides` 数组（沿每一维前进一格需要跨过的**字节数**）。给定一个下标元组 \((i_0, i_1, \dots, i_{n-1})\)，对应元素在 `data` 中的字节偏移是：

\[
\text{offset}(i_0, i_1, \dots, i_{n-1}) = \sum_{k=0}^{n-1} i_k \cdot \text{strides}[k]
\]

**视图的本质。** 「视图」就是新建一个 `ndarray` 对象，它的 `data` 指针指向（或偏移后指向）原始缓冲区，但 `shape` 和 `strides` 可以不同。既然偏移公式只依赖 `strides`，那么只要能找到一组新的 `strides` 让新 `shape` 下的每个下标都映射到原缓冲区里某个合法字节，就不需要复制数据——这就是几乎所有形状操作「不搬数据」的物理根源。

**C 连续与 F 连续。** 对一个 C 连续（行优先）数组，最末维 `strides` 等于 `itemsize`，其余满足 `strides[k] = strides[k+1] * shape[k+1]`；F 连续（列优先）则相反，首维 `strides` 等于 `itemsize`。`reshape` 与 `transpose` 改完 `strides` 后，NumPy 会调用 `PyArray_UpdateFlags` 重新评估这两个连续性标志位——这也是为什么转置后 `flags['C_CONTIGUOUS']` 常变成 `False`。

> 术语提示：本文「轴（axis）」即 `shape` 的某一维；「轴置换」指改变维的顺序；「视图」指共享 `data` 的新数组对象；「拷贝」指分配新缓冲区并搬动数据。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [numpy/_core/fromnumeric.py](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/fromnumeric.py) | 顶层 `np.reshape` / `np.transpose` / `np.swapaxes` / `np.squeeze` / `np.ravel` 的薄 Python 封装，经 `_wrapfunc` 委托给 ndarray 方法 |
| [numpy/_core/numeric.py](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/numeric.py) | `moveaxis`、`rollaxis`、`normalize_axis_tuple` 的纯 Python 实现，最终都归约成一次 `transpose` |
| [numpy/_core/shape_base.py](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/shape_base.py) | `atleast_1d/2d/3d`、`vstack`/`hstack`/`stack`/`unstack` 等组合式形状操作，用 `newaxis` 切片与 `reshape` 拼装视图 |
| [numpy/_core/src/multiarray/shape.c](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/shape.c) | C 层形状计算核心：`_reshape_with_copy_arg`、`_attempt_nocopy_reshape`、`PyArray_Transpose`、`PyArray_SwapAxes`、`PyArray_Ravel` 等 |
| [numpy/_core/src/multiarray/methods.c](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/methods.c) | ndarray 方法表：`array_reshape`、`array_transpose`、`array_swapaxes` 等的入口与参数解析 |
| [numpy/_core/src/multiarray/getset.c](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/getset.c) | `shape` / `strides` / `T` / `mT` 等属性的 getter 注册表 |

> 说明：本讲规格里把 reshape/transpose 的「Python 封装」归到 `shape_base.py` 与 `numeric.py`。实际源码中，顶层 `np.reshape`/`np.transpose` 等函数定义在 `fromnumeric.py`（因为它们本质是「转发到 ndarray 方法」的便利函数），而 `shape_base.py` 提供的是更高层的拼装函数、`numeric.py` 提供轴搬运函数。本讲按真实位置讲解，并照实引用 `fromnumeric.py`。

## 4. 核心概念与源码讲解

### 4.1 形状操作的总原理：只改 shape 与 strides

#### 4.1.1 概念说明

「形状操作」泛指一切改变 `ndarray` 的 `shape`（维数与各维长度）但不改变元素数值的运算，典型代表是 `reshape`、`transpose`、`swapaxes`、`moveaxis`、`squeeze`、`ravel`、`atleast_*`。它们共同的设计目标是：**只要物理上可行，就返回一个共享 `data` 的新数组对象（视图），绝不无谓地复制数据。**

之所以能这样做，是因为上一节讲过的偏移公式只依赖 `strides`。换句话说，`data` 缓冲区里那串字节是「哑」的——它不知道自己被解释成几维。维度的解释完全由 `shape` + `strides` 这两个小数组决定。于是「换个形状看同一块内存」就等价于「重算 `shape` 与 `strides`」。

这一节我们先看 NumPy 如何在属性层面暴露这三件套，再看一个最简单的「纯 Python 视图式形状操作」例子（`atleast_2d`），为后两节的 `reshape` 与 `transpose` 铺垫。

#### 4.1.2 核心流程

1. 访问 `arr.shape` / `arr.strides` 时，C 层 getter 动态从 `PyArrayObject` 的 `dimensions` / `strides` 字段构造一个 Python 元组返回。
2. `arr.T` 触发 `array_transpose_get`，它直接调用 `PyArray_Transpose(self, NULL)`（全反转）。
3. 纯 Python 的形状拼装函数（如 `atleast_2d`）通过 `newaxis` 切片（即 `None`）插入长度为 1 的轴，本质仍是基础索引返回视图——上一讲已说明基础索引只改 `shape`/`strides`。

#### 4.1.3 源码精读

**属性 getter 注册表。** `shape`、`strides`、`T`、`mT` 都登记在同一张 `array_getsetlist[]` 表里，访问时由对应 getter 现算返回：

[numpy/_core/src/multiarray/getset.c:743-807](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/getset.c#L743-L807) — 这段登记了 `ndim`/`flags`/`shape`/`strides`/`data`/`T`/`mT` 等属性的 getter（部分还有 setter）。注意 `shape` 和 `strides` 同时挂了 setter，所以 `arr.shape = (...)` 会触发 reshape 语义。

`T` 属性的实现极其简短，就是一次全反转 transpose：

[numpy/_core/src/multiarray/getset.c:732-735](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/getset.c#L732-L735) — `array_transpose_get` 直接 `return PyArray_Transpose(self, NULL);`，把「转置属性」与「转置函数」统一到同一条 C 路径。

**一个纯 Python 视图式形状操作的范例。** `atleast_2d` 把 1-D 数组变成 2-D 行向量，它没有调用任何 C 形状函数，而是用一个 `newaxis` 切片：

[numpy/_core/shape_base.py:117-130](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/shape_base.py#L117-L130) — 对 `ndim==1` 的输入执行 `ary[_nx.newaxis, :]`，即在第 0 轴前插入一个长度为 1 的轴。这正是上一讲「基础索引返回视图」的运用：`newaxis`（`None`）只把 `shape` 从 `(N,)` 改写成 `(1, N)`、`strides` 前面补一个任意值，`data` 不动。同理 `atleast_1d` 用 `result.reshape(1)`（[shape_base.py:60-64](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/shape_base.py#L60-L64)），`atleast_3d` 用 `ary[:, :, _nx.newaxis]`（[shape_base.py:195-198](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/shape_base.py#L195-L198)）。

这说明一个重要事实：**形状操作不一定要走专门的 C 函数，任何能改写 `shape`/`strides` 的视图手段（切片、`newaxis`、`reshape`、`transpose`）都算形状操作。** 后两节我们看两个最核心的 C 函数 `reshape` 与 `transpose`。

#### 4.1.4 代码实践

**实践目标：** 用属性与 `shares_memory` 验证「形状操作共享 `data`」。

**操作步骤：**

```python
import numpy as np

a = np.arange(12, dtype=np.float64).reshape(2, 6)   # C 连续，2x6
b = a[np.newaxis, :]                                 # atleast_2d 风格的视图
c = a.T                                              # 全反转 transpose

print("a.shape     =", a.shape,     "strides =", a.strides)
print("b.shape     =", b.shape,     "strides =", b.strides)
print("c.shape     =", c.shape,     "strides =", c.strides)
print("a flags C/F =", a.flags['C_CONTIGUOUS'], a.flags['F_CONTIGUOUS'])
print("c flags C/F =", c.flags['C_CONTIGUOUS'], c.flags['F_CONTIGUOUS'])
print("shares a,b  =", np.shares_memory(a, b))
print("shares a,c  =", np.shares_memory(a, c))
```

**需要观察的现象：**

- `a.strides` 应为 `(48, 8)`（`6*8=48`，`itemsize=8`）。
- `b.strides` 应为 `(48, 48, 8)` 之类——第 0 轴长度为 1，其 stride 任意（NumPy 通常填「下一轴的 stride」）。
- `c.shape` 应为 `(6, 2)`，`c.strides` 应为 `(8, 48)`——即把 `a` 的 strides 反转。
- `c` 的 `C_CONTIGUOUS` 应为 `False`、`F_CONTIGUOUS` 应为 `True`（2-D 下 C 数组的转置即 F 连续）。
- 两个 `shares_memory` 都应为 `True`。

**预期结果：** 上述全部成立。**待本地验证**具体打印文本，但 strides 数值可由偏移公式手工推出。

#### 4.1.5 小练习与答案

**练习 1：** 为什么 `arr.shape = newshape` 能改变数组形状而通常不复制数据？

**答案：** `shape` 的 setter（见 getset.c 中 `array_shape_set`）内部走的正是 reshape 路径，最终调用本讲 4.2 节的 `_reshape_with_copy_arg`。它只在新 `shape` 无法用原 `data` 解释时才复制；只要能找到匹配的 `strides`，就只改写 `dimensions`/`strides` 两个字段，`data` 指针不动。

**练习 2：** `atleast_2d` 对一个已经是 2-D 的数组会做什么？

**答案：** 见 [shape_base.py:124-125](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/shape_base.py#L124-L125)：`else: result = ary`，直接返回原对象，连视图都不新建。

---

### 4.2 reshape：重排形状与「能否免拷贝」

#### 4.2.1 概念说明

`reshape` 改变数组的 `shape`，但**不改变元素的总数与先后顺序**（按指定 `order` 解释）。它是日常用得最多的形状操作。关键问题只有一个：**这次 reshape 能不能不复制数据？**

直觉上，只要新形状能「正好」套在原缓冲区上（即存在一组 `strides` 使偏移公式对每个新下标都落到合法字节），就能返回视图；否则必须拷贝。例如对一个 C 连续的 `2x6` 数组：

- `reshape(3, 4)`：元素在内存里是 `0,1,...,11` 连续排列，新形状 `3x4` 仍是 C 序，能直接套上去 → 视图。
- `reshape(3, 4, order='F')`：要求按 Fortran 序重排元素，而原缓冲区是 C 序布局，套不上去 → 拷贝。

`reshape` 还允许一个维度写 `-1`，表示「这个维度的大小由元素总数和其余维度自动推断」。

#### 4.2.2 核心流程

Python 层 `np.reshape(a, shape, order='C', *, copy=None)` 的调用链：

1. `fromnumeric.reshape` 经 `array_function_dispatch` 装饰后，调用 `_wrapfunc(a, 'reshape', shape, order=order[, copy=copy])`。
2. `_wrapfunc` 取 `a.reshape` 这个绑定方法并调用它——即 C 方法 `array_reshape`。
3. `array_reshape` 解析 `order` 与 `copy` 关键字，把 `shape` 转成 `PyArray_Dims`，调用 `_reshape_with_copy_arg`。
4. `_reshape_with_copy_arg` 先用 `_fix_unknown_dimension` 处理 `-1`，再判断：若想要的目标序与缓冲区实际序不一致，就尝试 `_attempt_nocopy_reshape` 找一组免拷贝 strides；成功则用这组 strides 建视图，失败则（按 `copy` 策略）拷贝或报错。
5. 最终用 `PyArray_NewFromDescr_int` 创建新数组，`data` 指向原缓冲区（视图）或新缓冲区（拷贝），并以原数组为 `base`。

免拷贝判定算法 `_attempt_nocopy_reshape` 的思路（伪代码）：

```
# 去掉原 shape 中长度为 1 的轴（它们的 stride 无意义）
old = [(dim, stride) for axis in old_shape if dim != 1]
new = new_shape
# 用双指针把 old 与 new 都「乘开」到相同乘积，逐段匹配
# 对每一段，检查 old 的连续性是否足以合并（C 序要求 strides[k]==dim[k+1]*strides[k+1]）
# 若整段都能合并，按 C/F 序反推 new 的 strides
# 任意一段不满足 → 返回 0（需要拷贝）
```

#### 4.2.3 源码精读

**Python 封装。** 顶层函数非常薄，核心只有最后两行：

[numpy/_core/fromnumeric.py:223-315](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/fromnumeric.py#L223-L315) — `reshape` 的定义。`copy` 参数（NumPy 2.x 引入）控制三态：`True` 强制拷贝、`False` 不能拷贝（否则报错）、`None` 按需。注意 [L313-L315](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/fromnumeric.py#L313-L315)：`copy is not None` 时才把 `copy` 透传，否则按旧签名调用，避免向后兼容问题。

**`_wrapfunc` 委托机制。** 这是理解「顶层函数 → ndarray 方法」的关键：

[numpy/_core/fromnumeric.py:49-64](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/fromnumeric.py#L49-L64) — 先 `getattr(obj, method)` 拿到绑定方法（如 `a.reshape`）直接调用；若对象没有该方法或签名不兼容（如 pandas），退回 `_wrapit`：转成 ndarray 调用方法再包回原类型。所以 `np.reshape(a, ...)` 与 `a.reshape(...)` 走的是同一条 C 路径。

**C 方法入口。** ndarray 的 `reshape` 方法：

[numpy/_core/src/multiarray/methods.c:176-216](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/methods.c#L176-L216) — `array_reshape`。它解析 `order`/`copy` 关键字，把位置参数（无论是单个元组还是散开的整数）都用 `PyArray_IntpConverter` 转成 `PyArray_Dims newshape`，然后调用 `_reshape_with_copy_arg`。方法表登记在 [methods.c:3032-3034](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/methods.c#L3032-L3034)。

**核心实现 `_reshape_with_copy_arg`：**

[numpy/_core/src/multiarray/shape.c:226-335](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/shape.c#L226-L335) — 这是 reshape 的「大脑」。几个关键判断：

- [L239-L246](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/shape.c#L239-L246)：`order='K'`（KEEPORDER）在 reshape 中直接报错——`order='K'` 只在 `ravel`/`flatten` 里有意义。
- [L248-L260](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/shape.c#L248-L260)：快速路径——若新形状与旧形状完全相同，直接 `PyArray_View` 返回视图。
- [L265-L267](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/shape.c#L265-L267)：调用 `_fix_unknown_dimension` 处理 `-1`。
- [L287-L310](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/shape.c#L287-L310)：**免拷贝判定的入口**。当要求的序（C 或 F）与缓冲区实际连续性不一致时，调用 `_attempt_nocopy_reshape` 试图找一组免拷贝 strides；成功就用它，失败则按 `copy` 策略决定拷贝或报错。
- [L328-L332](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/shape.c#L328-L332)：用 `PyArray_NewFromDescr_int` 创建结果，`data` 传 `PyArray_DATA(array)`、`strides` 传算出的 `newstrides`（视图）或 `NULL`（拷贝时由新数组自行计算），并把 `array` 设为 `base`。

**`-1` 推断 `_fix_unknown_dimension`：**

[numpy/_core/src/multiarray/shape.c:485-530](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/shape.c#L485-L530) — 遍历新形状，至多允许一个负值（`-1`），用 `s_original / s_known` 算出它应该取多少；若多于一个负值或不能整除，报 `ValueError`。

**免拷贝判定算法 `_attempt_nocopy_reshape`：**

[numpy/_core/src/multiarray/shape.c:378-471](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/shape.c#L378-L471) — 算法先把原 shape 中长度为 1 的轴剔除（[L393-L399](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/shape.c#L393-L399)，因为它们的 stride 无意义），再用双指针把新旧两段维度乘开到相同乘积逐段对齐（[L406-L417](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/shape.c#L406-L417)），对每段检查原数组是否「连续到足以合并」（C 序要求 `oldstrides[ok] == olddims[ok+1]*oldstrides[ok+1]`，见 [L427-L433](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/shape.c#L427-L433)），最后反推新 strides（[L436-L449](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/shape.c#L436-L449)）。任何一段不满足就返回 0 表示「需要拷贝」。函数注释 [L358-L377](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/shape.c#L358-L377) 把语义讲得很清楚：返回 1 并填 `newstrides` 表示免拷贝成功。

#### 4.2.4 代码实践

**实践目标：** 对照源码，亲手验证 reshape 何时视图、何时拷贝。

**操作步骤：**

```python
import numpy as np

a = np.arange(12, dtype=np.float64).reshape(2, 6)   # C 连续，strides (48, 8)

r1 = a.reshape(3, 4)              # 同为 C 序，应免拷贝
r2 = a.reshape(3, 4, order='F')   # 要求 F 序，缓冲区是 C 序 → 拷贝
r3 = a.reshape(2, -1)             # -1 推断为 6

print("a    strides =", a.strides,  " shape =", a.shape)
print("r1   strides =", r1.strides, " shares =", np.shares_memory(a, r1))
print("r2   strides =", r2.strides, " shares =", np.shares_memory(a, r2))
print("r3   shape   =", r3.shape,   " shares =", np.shares_memory(a, r3))

# copy=False：在必须拷贝时应抛错
try:
    a.reshape(3, 4, order='F', copy=False)
except ValueError as e:
    print("copy=False raised:", e)
```

**需要观察的现象：**

- `r1.strides` 应为 `(32, 8)`（`4*8=32`），`shares_memory` 为 `True`。
- `r2` 因 `order='F'` 触发拷贝，`shares_memory` 为 `False`；其值是 `a` 的 Fortran 序重排。
- `r3.shape` 为 `(2, 6)`，与 `a` 共享内存。
- `copy=False` + 必须拷贝时抛 `ValueError: Unable to avoid creating a copy while reshaping.`（对应 [shape.c:296-301](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/shape.c#L296-L301)）。

**预期结果：** 上述全部成立。strides 数值由 `itemsize=8` 与偏移公式推出；**待本地验证**确切打印文本。

#### 4.2.5 小练习与答案

**练习 1：** 对一个 C 连续的 `2x6` 数组做 `reshape(3, 4)` 后，新数组的 `strides` 是多少？为什么？

**答案：** `(32, 8)`。新形状 `(3, 4)` 仍按 C 序解释，最末维 stride = `itemsize = 8`，第 0 维 stride = `4 * 8 = 32`。因为原缓冲区本就是 C 连续，`_attempt_nocopy_reshape` 成功算出这组 strides，故返回视图。

**练习 2：** `a.reshape(6, -1, 2)` 对 `size=12` 的数组推断出的形状是什么？

**答案：** `(6, 1, 2)`。`_fix_unknown_dimension` 用 `s_original / s_known = 12 / (6*2) = 1` 填入 `-1` 的位置。若写出两个 `-1` 会报「can only specify one unknown dimension」。

**练习 3：** 为什么 `order='K'` 在 reshape 里被禁止（[shape.c:242-246](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/shape.c#L242-L246)），但在 `ravel` 里允许？

**答案：** `order='K'`（KEEPORDER）意为「尽量按内存现有顺序」，它需要先对 strides 排序来判断「最自然」的轴序——这对 `ravel`（降成一维）是良定义的，但对 `reshape`（要指定新多维形状）会与用户显式给定的形状冲突，故禁止。

---

### 4.3 transpose / swapaxes / moveaxis：轴置换的视图实现

#### 4.3.1 概念说明

`reshape` 改的是「每维有多长」，`transpose` 改的是「维的顺序」。对一个 `n` 维数组，转置就是给定一个 `0..n-1` 的排列 `perm`，使结果第 `i` 轴对应原数组第 `perm[i]` 轴。

关键洞察：**轴置换永远不需要搬数据。** 因为偏移公式 \(\sum i_k \cdot \text{strides}[k]\) 里，`strides` 与 `shape` 是一一对应的——只要把 `shape` 和 `strides` 两个数组按同一个排列 `perm` 重新排列，每个新下标映射到的字节与原来完全一致。所以 transpose 的实现就是：建一个新数组对象，`data` 指针不变，把 `dimensions` 和 `strides` 按 `perm` 重排后填进去。

- `transpose(a)` / `a.T`：全反转，`perm = [n-1, n-2, ..., 0]`。
- `transpose(a, axes)`：用指定排列。
- `swapaxes(a, i, j)`：只交换两个轴（排列里对调两个位置）。
- `moveaxis(a, src, dst)`：把若干轴搬到新位置，其余轴保持相对顺序——它最终也归约成一次 `transpose`。
- `matrix_transpose` / `a.mT`：只反转最后两轴（矩阵堆栈的转置），等价于 `swapaxes(ndim-2, ndim-1)`。

#### 4.3.2 核心流程

`np.transpose(a, axes)` 的调用链：

1. `fromnumeric.transpose` 经 `_wrapfunc` 调 `a.transpose(axes)` → C 方法 `array_transpose`。
2. `array_transpose` 把 `axes` 转成 `PyArray_Dims`，调 `PyArray_Transpose(self, &permute)`（无参时传 `NULL` 表全反转）。
3. `PyArray_Transpose` 校验排列（去重、负轴归一化），用 `PyArray_NewFromDescrAndBase` 建新数组，`data` 指向原缓冲区、`base` 设为原数组，strides/dims 暂填占位。
4. 随后一个循环把结果的 `dimensions[i]`/`strides[i]` 填成源数组的 `dimensions[perm[i]]`/`strides[perm[i]]`。
5. 调 `PyArray_UpdateFlags` 重算连续性标志。

`moveaxis` 的归约：在 Python 层把 `source`/`destination` 解析成轴号，构造一个完整排列 `order`，再调 `a.transpose(order)`。

#### 4.3.3 源码精读

**`PyArray_Transpose` —— 轴置换的 C 核心：**

[numpy/_core/src/multiarray/shape.c:676-740](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/shape.c#L676-L740) — 这是最重要的一段。逐行看：

- [L685-L690](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/shape.c#L685-L690)：`permute == NULL`（即 `a.T` / `np.transpose(a)`）时，`permutation[i] = n-1-i`，全反转。
- [L699-L714](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/shape.c#L699-L714)：校验排列——逐个 `check_and_adjust_axis` 归一化负轴，用 `reverse_permutation[axis]` 检测重复轴（重复则报「repeated axis in transpose」），并建立 `permutation[i] = axis`。
- [L723-L727](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/shape.c#L723-L727)：**视图的关键**——用 `PyArray_NewFromDescrAndBase` 建结果，第 5、6 个参数 `PyArray_DIMS(ap)` 与 `NULL`（strides 暂不填），第 7 个参数 `PyArray_DATA(ap)` 直接复用源数据指针，最后两个 `(PyObject*)ap` 把 `base` 设为源数组。注释 [L719-L722](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/shape.c#L719-L722) 明说「points data at PyArray_DATA(ap)」。
- [L733-L736](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/shape.c#L733-L736)：**填 shape/strides 的循环**——`PyArray_DIMS(ret)[i] = PyArray_DIMS(ap)[permutation[i]]`、`PyArray_STRIDES(ret)[i] = PyArray_STRIDES(ap)[permutation[i]]`，即按排列重排两个数组。
- [L737-L738](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/shape.c#L737-L738)：`PyArray_UpdateFlags` 重算 `C_CONTIGUOUS`/`F_CONTIGUOUS`/`ALIGNED`。这就是转置后连续性标志会改变的原因。

**`PyArray_SwapAxes` —— 交换两轴，归约为 transpose：**

[numpy/_core/src/multiarray/shape.c:645-670](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/shape.c#L645-L670) — 先构造恒等排列 `dims[i]=i`，再 `dims[a1]=a2; dims[a2]=a1`，最后 `return PyArray_Transpose(ap, &new_axes);`。所以 `swapaxes` 没有独立的数据搬运逻辑，只是 transpose 的一个特例。

**`PyArray_MatrixTranspose` —— 只反转末两轴：**

[numpy/_core/src/multiarray/shape.c:745-756](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/shape.c#L745-L756) — `ndim < 2` 报错，否则 `PyArray_SwapAxes(ap, ndim-2, ndim-1)`。这是 Array API 的 `matrix_transpose`，对应属性 `a.mT`（见 [getset.c:738-741](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/getset.c#L738-L741) 的 `array_matrix_transpose_get`）。

**`array_transpose` 方法入口：**

[numpy/_core/src/multiarray/methods.c:2372-2398](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/methods.c#L2372-L2398) — 把位置参数规整成 `shape`，无参时 `PyArray_Transpose(self, NULL)`（全反转），有参时转成 `PyArray_Dims` 后调 `PyArray_Transpose`。方法表登记在 [methods.c:3080-3082](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/methods.c#L3080-L3082)，`swapaxes` 在 [methods.c:3062-3064](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/methods.c#L3062-L3064)。

**`moveaxis` —— 在 Python 层把搬轴归约为 transpose：**

[numpy/_core/numeric.py:1490-1557](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/numeric.py#L1490-L1557) — 算法分三步：

1. [L1545-L1546](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/numeric.py#L1545-L1546)：用 `normalize_axis_tuple` 把 `source`/`destination` 归一化为非负轴号元组。
2. [L1551](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/numeric.py#L1551)：`order = [n for n in range(a.ndim) if n not in source]`——先把「不动的轴」按原顺序列出来。
3. [L1553-L1554](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/numeric.py#L1553-L1554)：按 `destination` 升序逐个把 `source` 轴 `insert` 到目标位置，得到完整排列 `order`。
4. [L1556](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/numeric.py#L1556)：`result = transpose(order)`——一次 transpose 搞定。

注意 [L1538-L1543](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/numeric.py#L1538-L1543)：`moveaxis` 优先用 `a.transpose`，从而对定义了 `transpose` 的鸭子数组类型（如 CuPy、PyTorch）也能工作，体现了 NumPy 的互操作设计。

**`rollaxis` —— 旧式搬轴，同样归约为 transpose：**

[numpy/_core/numeric.py:1338-1425](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/numeric.py#L1338-L1425) — 文档 [L1342-L1344](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/numeric.py#L1342-L1344) 已说明「应优先使用 `moveaxis`」。其实现 [L1422-L1425](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/numeric.py#L1422-L1425)：构造 `axes = list(range(n)); axes.remove(axis); axes.insert(start, axis)`，再 `return a.transpose(axes)`。

**`normalize_axis_tuple` —— 轴号归一化的公共工具：**

[numpy/_core/numeric.py:1428-1483](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/numeric.py#L1428-L1483) — 把单个 int 或 int 序列统一成非负轴号元组，处理负索引（`-1` → `ndim-1`），并默认禁止重复轴（[L1478-L1482](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/numeric.py#L1478-L1482)）。`moveaxis`、`rollaxis`、`squeeze`、`stack` 等都依赖它。

#### 4.3.4 代码实践

**实践目标：** 这是本讲规格要求的综合实践——对 C 连续 `2x6` 数组做 `reshape(3,4)` 与 `transpose`，对照 strides 解释为何不复制数据。

**操作步骤：**

```python
import numpy as np

a = np.arange(12, dtype=np.float64).reshape(2, 6)   # C 连续

r = a.reshape(3, 4)        # reshape：重排形状
t = a.transpose()          # transpose：重排轴（2-D 下即矩阵转置）
mt = a.mT                  # matrix_transpose：2-D 下与 a.T 相同

print("=== a (原始) ===")
print("shape   =", a.shape, " strides =", a.strides)
print("C_CONTIG =", a.flags['C_CONTIGUOUS'], " F_CONTIG =", a.flags['F_CONTIGUOUS'])

print("\n=== r = a.reshape(3,4) ===")
print("shape   =", r.shape, " strides =", r.strides)
print("shares_memory(a, r) =", np.shares_memory(a, r))

print("\n=== t = a.transpose() ===")
print("shape   =", t.shape, " strides =", t.strides)
print("shares_memory(a, t) =", np.shares_memory(a, t))
print("C_CONTIG =", t.flags['C_CONTIGUOUS'], " F_CONTIG =", t.flags['F_CONTIGUOUS'])

# 验证 transpose 只是重排 strides/dims：原地改一个值，另一边可见
t[0, 0] = 999.0
print("\n改 t[0,0] 后, a[0,0] =", a[0, 0], "（应非 999，因为 t[0,0] 对应 a 的另一个元素）")

# moveaxis：把 3-D 数组的第 0 轴搬到末尾
x = np.zeros((3, 4, 5))
m = np.moveaxis(x, 0, -1)
print("\nmoveaxis(x, 0, -1).shape =", m.shape, " shares =", np.shares_memory(x, m))
```

**需要观察的现象与解释：**

- `a.strides == (48, 8)`：C 连续，`6*8=48`，`itemsize=8`。
- `r.strides == (32, 8)` 且 `shares_memory` 为 `True`：`reshape(3,4)` 仍是 C 序，`_attempt_nocopy_reshape` 成功，只换了 `shape`/`strides`，`data` 不动。这正是「reshape 不复制数据」的实例。
- `t.strides == (8, 48)` 且 `shares_memory` 为 `True`：transpose 把 `a` 的 strides `(48, 8)` 反排成 `(8, 48)`、shape `(2,6)` 反排成 `(6,2)`。`PyArray_Transpose` 复用 `PyArray_DATA(ap)`（[shape.c:726](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/shape.c#L726)），所以不复制数据。
- `t` 的 `C_CONTIGUOUS == False`、`F_CONTIGUOUS == True`：2-D 下 C 数组的转置即 F 连续，`PyArray_UpdateFlags`（[shape.c:737-738](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/shape.c#L737-L738)）重算后如实地反映了这一点。
- 改 `t[0,0]` 会改到 `a` 中某个元素（因为共享 `data`），但**不是** `a[0,0]`——因为 `t[0,0]` 在转置后对应原数组 `a[0,0]` 的位置由 strides 决定，2-D 转置下 `t[i,j]` 对应 `a[j,i]`，所以 `t[0,0]` 对应 `a[0,0]`……这里要小心：对 2-D 全反转，`t[i,j]==a[j,i]`，故 `t[0,0]==a[0,0]`。**请运行后核对** `a[0,0]` 是否变成 `999.0`，并据此理解「共享 data 但下标映射被重排」。
- `moveaxis(x, 0, -1).shape == (4, 5, 3)` 且共享内存：`moveaxis` 最终调一次 `transpose`，自然也是视图。

**预期结果：** strides 与共享内存判断如上；`a[0,0]` 是否被 `t[0,0]=999` 改动，请运行确认（按 2-D 全反转的映射 `t[i,j]↔a[j,i]`，应被改为 `999.0`）。**待本地验证**确切数值。

#### 4.3.5 小练习与答案

**练习 1：** 为什么 `transpose` 「永远」返回视图，而 `reshape` 偶尔返回拷贝？

**答案：** 轴置换只是把 `shape` 和 `strides` 两个数组按同一排列重排（[shape.c:733-736](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/shape.c#L733-L736)），原缓冲区的每个字节在新下标下仍合法映射，故必能共享 `data`。而 `reshape` 在要求的元素顺序（C/F 序）与缓冲区实际布局不一致时，找不到一组 `strides` 让新形状套上去，只能拷贝（见 4.2 节 `_attempt_nocopy_reshape` 返回 0 的情形）。

**练习 2：** 给定 `x.shape == (3, 4, 5)`，`np.moveaxis(x, [0, 1], [-1, -2]).shape` 是什么？它和 `np.transpose(x).shape` 结果一样吗？

**答案：** 都是 `(5, 4, 3)`。`moveaxis` 把 0→末尾、1→倒数第二，剩下轴 2 自然落到最前，等价于全反转。但二者语义不同：`moveaxis` 只搬指定轴、其余保序，只有当搬动恰好造成全反转时才与 `transpose()` 同结果。

**练习 3：** `a.swapaxes(0, 1)` 与 `a.transpose(1, 0)` 对 2-D 数组等价吗？从源码说明理由。

**答案：** 等价。`PyArray_SwapAxes`（[shape.c:645-670](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/shape.c#L645-L670)）构造的排列就是 `[1, 0]`，然后调 `PyArray_Transpose(ap, &new_axes)`；而 `a.transpose(1, 0)` 直接以排列 `(1, 0)` 调同一个 `PyArray_Transpose`。两者殊途同归。

---

## 5. 综合实践

**任务：用形状操作实现「批量矩阵转置」并验证全程零拷贝。**

设你有一批 `B` 个 `M×N` 矩阵堆叠成形状 `(B, M, N)` 的数组 `batch`。请：

1. 用 `transpose` 把它变成 `(B, N, M)`（每个矩阵各自转置，批量维不动）。
2. 用 `moveaxis` 把批量维从第 0 轴搬到末尾，得到 `(N, M, B)`。
3. 对第 2 步结果做 `reshape(N*M, B)`，并判断这一步是视图还是拷贝。
4. 对每一步用 `np.shares_memory(batch, result)` 验证是否共享 `data`，并打印 `strides` 与 `flags` 的 `C_CONTIGUOUS`/`F_CONTIGUOUS`。
5. 对照源码解释：哪一步必然是视图？哪一步可能拷贝？为什么？

参考骨架（示例代码，请自行补全观察部分）：

```python
import numpy as np

B, M, N = 4, 3, 5
batch = np.arange(B*M*N, dtype=np.float64).reshape(B, M, N)

step1 = batch.transpose(0, 2, 1)          # (B, N, M)
step2 = np.moveaxis(step1, 0, -1)         # (N, M, B)
step3 = step2.reshape(N*M, B)             # (N*M, B)

for name, arr in [("step1", step1), ("step2", step2), ("step3", step3)]:
    print(name, "shape=", arr.shape,
          "strides=", arr.strides,
          "shares=", np.shares_memory(batch, arr),
          "C/F=", arr.flags['C_CONTIGUOUS'], arr.flags['F_CONTIGUOUS'])
```

**预期与解释要点：**

- `step1`、`step2` 都是 `transpose`/`moveaxis` 的结果，必然共享 `data`（视图）。
- `step3` 是否视图取决于 `step2` 的内存布局：`step2` 通常既不 C 连续也不 F 连续，`reshape` 到 `(N*M, B)` 时 `_attempt_nocopy_reshape` 大概率失败 → 拷贝。请运行后用 `shares_memory` 核对，并对照 [shape.c:287-310](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/shape.c#L287-L310) 解释。**待本地验证** `step3` 的 `shares` 值。

这个任务把本讲三个模块串起来：`transpose`（4.3）→ `moveaxis`（4.3）→ `reshape` 免拷贝判定（4.2），并回到 4.1 的「视图 = 共享 data」总原理。

## 6. 本讲小结

- 形状操作的物理基础是偏移公式 \(\sum i_k \cdot \text{strides}[k]\)：只要能找到匹配的 `strides`，就只改 `shape`/`strides` 两个小数组而 `data` 不动，即返回视图。
- `np.reshape`/`np.transpose` 等顶层函数是 `fromnumeric.py` 里的薄封装，经 `_wrapfunc` 委托给 ndarray 的 C 方法，最终落到 `shape.c`。
- `reshape` 的免拷贝判定由 `_attempt_nocopy_reshape` 完成：要求的目标序与缓冲区实际布局一致时返回视图，否则按 `copy` 策略拷贝或报错；`-1` 由 `_fix_unknown_dimension` 推断。
- `transpose` 永远是视图：`PyArray_Transpose` 直接复用 `PyArray_DATA(ap)`，只把 `dimensions`/`strides` 按排列重排，再 `PyArray_UpdateFlags` 重算连续性。
- `swapaxes`、`matrix_transpose`(`mT`)、`moveaxis`、`rollaxis` 都归约为一次 `transpose`；`moveaxis` 在 Python 层用 `normalize_axis_tuple` 构造完整排列。
- `atleast_*` 等纯 Python 形状函数用 `newaxis` 切片或 `reshape` 拼装视图，证明「形状操作」不必走专门 C 函数，任何改写 `shape`/`strides` 的视图手段都算。

## 7. 下一步学习建议

- 下一讲 **u3-l3 视图、拷贝与 strides 技巧** 将把本讲的 strides 模型推到极限：`as_strided`、`sliding_window_view` 如何手工构造任意 strides（包括跨维跳跃），以及为何这类操作需要 `writeable=False` 防越界。
- 若想看形状操作在「降维」方向的对偶，可先读 `shape.c` 的 `PyArray_Ravel`/`PyArray_Flatten`（[shape.c:913-1009](https://github.com/numpy/numpy/blob/71d523a529c873cde8a11af2d4935a4082d4a60e/numpy/_core/src/multiarray/shape.c#L913-L1009)），理解 `ravel` 何时返回视图、何时拷贝。
- 进入单元 4 前，建议结合本讲重读 `PyArrayObject_fields` 结构（`ndarraytypes.h`），把 `data`/`dimensions`/`strides`/`flags` 四个字段与本讲的「改 shape/strides 不改 data」对应起来，为 ufunc 与 C 层内存模型打底。
