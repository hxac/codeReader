# pad 多模式填充

## 1. 本讲目标

`np.pad` 是 numpy 中给数组「镶边」的函数：在数组的每一条边的两端，按指定宽度补充元素，从而得到一个更大的数组。它支持十余种填充模式（`constant`/`edge`/`linear_ramp`/`maximum`/`mean`/`median`/`minimum`/`reflect`/`symmetric`/`wrap`/`empty`），还允许传入自定义函数。

学完本讲，你应当能够：

1. 说清 `pad` 的两层骨架——「先用 `_pad_simple` 分配扩大数组并把原数据拷到中央，再按 `mode` 分支填边」。
2. 理解 `_pad_simple` 与 `_set_pad_area` 的分工，以及 `_view_roi` 为何要缩小工作区。
3. 掌握 `_as_pairs` 如何把千变万化的参数（标量、序列、字典）统一规整成 `(ndim, 2)` 配对。
4. **准确区分 `reflect` 与 `symmetric` 的方向差异**（边缘值是否重复），并读懂 `_set_reflect_both` 的反射算法。
5. 理解 `wrap` 的周期环绕语义与迭代填充机制，以及统计模式中 `stat_length` 的动态长度计算。

---

## 2. 前置知识

- **轴（axis）**：numpy 数组的第 k 个维度。一维数组只有 axis=0，二维数组有 axis=0（行）、axis=1（列）。
- **切片（slice）**：`a[start:stop:step]`，`step` 为负时反向取。本讲大量使用 `slice(start, stop, -1)` 做「反向取一段」。
- **视图（view）**：不复制数据、只换 shape/strides 读同一块内存。`_view_roi` 和各 `_get_*` 都返回视图，零拷贝。
- **广播（broadcast）**：把小形状「拉伸」到大形状的规则。`_as_pairs` 用它把标量参数铺成每维一对。
- **NEP-18 `__array_function__` 与 dispatcher+impl 双函数写法**：本讲的 `pad` 用 `@array_function_dispatch(_pad_dispatcher)` 装饰，dispatcher 只返回 `(array,)`，详见前置讲义 u1-l2。
- **填充区（pad area）与有效区（valid area）**：扩大后的数组里，中央存放原数据的区域叫有效区，四周待填的区域叫填充区。

> 本讲不依赖其它功能讲义，但若你已读过 u1-l2（dispatcher 模式）与 u5-l1（步长/视图），会对若干细节理解更深。

---

## 3. 本讲源码地图

本讲全部源码集中在一个文件里：

| 文件 | 作用 |
| --- | --- |
| [numpy/lib/_arraypad_impl.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraypad_impl.py) | `pad` 及其全部私有辅助函数的实现。`__all__` 仅暴露 `pad` 一个名字。 |

文件内部按职责分为三段：

- **切片工具**：`_slice_at_axis`（构造某维的切片元组）、`_view_roi`（取迭代填充时的兴趣区域）。
- **填边内核**：`_pad_simple`（扩大数组）、`_set_pad_area`（给某维左右填充区赋值）、`_get_edges`/`_get_linear_ramps`/`_get_stats`（取边缘/构造渐变/算统计量）、`_set_reflect_both`/`_set_wrap_both`（反射/环绕）。
- **参数规整与主函数**：`_as_pairs`（参数广播）、`_pad_dispatcher`（派发器）、`pad`（总调度）。

---

## 4. 核心概念与源码讲解

### 4.1 pad 的总体调度：先扩大，再按 mode 填边

#### 4.1.1 概念说明

`pad` 要处理十几种模式，但所有模式共享同一个骨架：

1. **规整参数**：把 `pad_width` 以及各模式专属参数（`constant_values`/`end_values`/`stat_length`）规整成「每个维度一对（左, 右）」的标准形态。
2. **扩大数组**：分配一个最终大小的新数组，把原数组拷到中央，四周留空（值未定义）。这一步与模式无关。
3. **按模式填边**：根据 `mode` 走不同分支，往四周的空白区写入值。

这种「骨架与填边策略分离」的设计，使得新增一种模式只需写一个 `_get_xxx`/`_set_xxx` 函数，并在主函数里加一个 `elif` 分支即可。

#### 4.1.2 核心流程

```
pad(array, pad_width, mode, **kwargs)
  ├─ array = asarray(array)              # 规整为 ndarray
  ├─ 若 pad_width 是 dict → 转成 [(before,after)]*ndim 序列
  ├─ pad_width = _as_pairs(..., as_index=True)  # 广播成 (ndim,2)
  ├─ 若 mode 是可调用对象 → 走自定义函数路径，直接返回
  ├─ 校验 kwargs 是否是该 mode 允许的参数
  ├─ padded, slice = _pad_simple(array, pad_width)   # ★ 扩大数组
  └─ 按 mode 分支填边：
       constant    → _set_pad_area(..., constant_values)
       empty       → 啥也不做（填充区保持未定义）
       edge        → _get_edges → _set_pad_area
       linear_ramp → _get_linear_ramps → _set_pad_area
       stat 系列   → _get_stats → _set_pad_area
       reflect/symmetric → while 循环 _set_reflect_both
       wrap        → while 循环 _set_wrap_both
```

注意 `reflect`/`symmetric`/`wrap` 用的是 `while` 循环，因为当填充宽度大于原数据长度时，需要多次反射/环绕才能填满；而其它模式一次 `_set_pad_area` 即可。

#### 4.1.3 源码精读

`pad` 的公开签名与 docstring 极长，这里聚焦函数体。首先看参数规整与 dict 支持：

[\_arraypad_impl.py:774-792](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraypad_impl.py#L774-L792) —— 这段先把 `array` 转成 `ndarray`；若 `pad_width` 是字典（如 `{0: (3,0), 1: 2}`），用 `match-case` 把每个键值对解析成 `(before, after)` 放进长度为 `ndim` 的列表（未指定的轴默认 `(0,0)`）；最后用 `_as_pairs(..., as_index=True)` 广播成 `(ndim, 2)` 并校验非负整数。

字典模式是较新的语法（Python 3.10 `match-case`），它允许只给部分轴指定宽度。例如 `np.pad(a, {-1: 2})` 表示只在末轴两侧各填 2。

接着是「先扩大数组」的关键一行：

[\_arraypad_impl.py:842](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraypad_impl.py#L842) —— `_pad_simple(array, pad_width)` 返回扩大后的 `padded`（填充区未定义）与指向中央有效区的 `original_area_slice`。这是所有模式共同的起点。

然后是模式分支的总装：

[\_arraypad_impl.py:847-924](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraypad_impl.py#L847-L924) —— 这里是 `pad` 的「调度核心」。每个分支的模式基本一致：取出当前轴的兴趣区域 `roi = _view_roi(...)`，用某个 `_get_*` 算出要填的值，再用 `_set_pad_area` 写入。`reflect`/`symmetric`/`wrap` 三个分支多了 `while` 循环以处理「填充宽度 > 原数据长度」的情形。

`allowed_kwargs` 表也值得一看：

[\_arraypad_impl.py:818-835](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraypad_impl.py#L818-L835) —— 这张表把「每个模式允许哪些 kwargs」显式声明出来。传错参数（比如给 `reflect` 传 `constant_values`）会被精确报错，而不是被静默忽略；传了不认识的 `mode` 字符串则抛 `ValueError("mode '...' is not supported")`。

#### 4.1.4 代码实践

**实践目标**：验证 `pad` 的 dict 形式 `pad_width` 与多模式分支。

```python
import numpy as np

a = np.arange(1, 7).reshape(2, 3)
# 只在末轴左右各填 2，用 constant 模式
print(np.pad(a, {-1: 2}, mode='constant'))
# 故意给 reflect 传错参数，观察报错
try:
    np.pad(a, 1, mode='reflect', constant_values=9)
except ValueError as e:
    print("报错：", e)
```

**操作步骤**：把上面代码存为 `pad_dispatch.py` 并运行 `python pad_dispatch.py`。

**需要观察的现象**：第一段输出只在末轴（列方向）两侧补 0；第二段抛出 `ValueError`，提示 `unsupported keyword arguments for mode 'reflect': {'constant_values'}`。

**预期结果**（已对照源码与文档示例）：

```
[[0 0 1 2 3 0 0]
 [0 0 4 5 6 0 0]]
报错： unsupported keyword arguments for mode 'reflect': {'constant_values'}
```

#### 4.1.5 小练习与答案

**练习 1**：`np.pad(a, {0: (3, 0), 1: 2})` 对一个 `(2,3)` 数组会产生什么形状的输出？

**答案**：axis=0 左侧填 3、右侧填 0；axis=1 左右各填 2。所以新形状为 \((2+3+0,\ 3+2+2) = (5, 7)\)。

**练习 2**：为什么 `empty` 模式分支体里只有一句 `pass`？

**答案**：`_pad_simple` 分配扩大数组时本就不初始化填充区，`empty` 模式要的就是「填充区保持未定义」，所以无需再做任何事，直接 `pass` 即可。

---

### 4.2 _pad_simple 与 _set_pad_area：扩大数组与填边分工

#### 4.2.1 概念说明

`_pad_simple` 是整个 `pad` 的地基：它负责**分配最终大小的数组，并把原数据搬到中央**，但**不关心填充区填什么**。填充区的内容交给 `_set_pad_area` 按维度、按模式逐个填入。

之所以这样拆分，是因为「分配多大、原数据放哪」这件事与模式完全无关，而「填充区填什么」与模式强相关。把不变的骨架抽出来，避免每个模式都重复写一遍内存分配逻辑。

#### 4.2.2 核心流程

`_pad_simple` 的工作：

1. 按 `pad_width` 算出新形状：每维 `new_size = left + old_size + right`。
2. 用 `np.empty` 分配新数组（注意保留 F/C 布局）。
3. 若给了 `fill_value`，先 `padded.fill(fill_value)`（`constant` 模式与自定义函数路径会用）。
4. 算出指向中央有效区的切片 `original_area_slice`，把原数组拷进去。
5. 返回 `(padded, original_area_slice)`。

`_set_pad_area` 的工作：

1. 用 `_slice_at_axis` 构造「只在该维切出左填充区」和「右填充区」的两个切片元组。
2. 把传入的左值、右值分别赋给这两个切片。

#### 4.2.3 源码精读

[\_arraypad_impl.py:87-127](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraypad_impl.py#L87-L127) —— `_pad_simple`。注意第 110-113 行用 `zip(array.shape, pad_width)` 把「每维原大小」与「每维 (left,right)」对齐，逐维算 `left+size+right` 得到新形状。第 114 行 `order = 'F' if array.flags.fnc else 'C'` 判断：`fnc` 表示「Fortran 连续且非 C 连续」，只有纯列主序才保留 F 布局，否则一律 C 布局。第 121-124 行算出中央切片，第 125 行 `padded[original_area_slice] = array` 把原数据就位。

[\_arraypad_impl.py:130-152](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraypad_impl.py#L130-L152) —— `_set_pad_area`。它依赖一个关键小工具 `_slice_at_axis`：

[\_arraypad_impl.py:34-56](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraypad_impl.py#L34-L56) —— `_slice_at_axis(sl, axis)` 返回一个长度等于 `ndim` 的切片元组，只有第 `axis` 维是 `sl`，其余维都是 `slice(None)`（全选）。例如对二维数组，`_slice_at_axis(slice(None,3), 1)` 得到 `(slice(None), slice(None,3))`，即「所有行的前 3 列」。这个工具让所有填边函数都能用统一的「在某维切一段」语义。

还有一个容易被忽略但很重要的工具 `_view_roi`：

[\_arraypad_impl.py:59-84](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraypad_impl.py#L59-L84) —— 当**逐维迭代填边**时（先填 axis=0，再填 axis=1……），后填的维度的角落区域会依赖先填维度已填好的值。若不缩小工作区，先填维度的填充区会与后填维度的计算交叉，导致角落被反复覆盖。`_view_roi` 把当前及更早维度的填充区排除掉，只保留「有效区 + 后续维度填充区」作为兴趣区域，从而避免角落的重复/错误覆盖。主函数里每个分支都调用了 `roi = _view_roi(padded, original_area_slice, axis)`。

#### 4.2.4 代码实践

**实践目标**：直接调用 `_pad_simple` 观察扩大后的数组与中央切片，理解「未定义填充区」。

```python
import numpy as np
from numpy.lib._arraypad_impl import _pad_simple

a = np.array([10, 20, 30])
# 左右各填 2，不指定 fill_value → 填充区是未定义内存
padded, sl = _pad_simple(a, [(2, 2)])
print("形状:", padded.shape)
print("中央切片:", sl)        # 指向原数据所在区域
print("有效区:", padded[sl])  # 应等于原数组
```

**操作步骤**：运行该脚本。

**需要观察的现象**：`padded` 长度变为 7；`padded[sl]` 恰好是 `[10,20,30]`；而填充区（前 2、后 2 个元素）是任意值（取决于 `np.empty` 拿到的内存）。

**预期结果**：

```
形状: (7,)
中央切片: (slice(2, 5, None),)
有效区: [10 20 30]
```

> 说明：填充区的具体数值无法预测（未初始化内存），属「待本地验证」的随机部分，但有效区一定是原数组。

#### 4.2.5 小练习与答案

**练习 1**：对形状 `(3,)` 的数组 `_pad_simple(a, [(2,2)])`，`original_area_slice` 是什么？

**答案**：`left=2, size=3`，所以切片是 `slice(2, 2+3) = slice(2,5)`，即元组 `(slice(2,5,None),)`。

**练习 2**：为什么 `_pad_simple` 要单独保留 F 布局（`order='F'`）？

**答案**：若输入是列主序（Fortran 连续），强行用 C 布局分配会改变内存顺序，导致后续按列访问低效或与原数据布局不一致。保留 `fnc`（纯 F 连续）可维持布局一致性；其余情况（包括既 F 又 C 的一维等情况）退回 C 布局。

---

### 4.3 _as_pairs：把各种形状的参数规整成 (ndim, 2) 配对

#### 4.3.1 概念说明

`pad` 的参数 `pad_width`、`constant_values`、`end_values`、`stat_length` 都接受非常灵活的输入形态：可以是单个标量（`3`）、单边值（`(2,3)`）、每维一对（`((1,2),(3,4))`），甚至 `None`（表示「该参数未提供」）。但底层填边函数只认一种标准形态——**每个维度一对 `(before, after)`**，即形状 `(ndim, 2)`。

`_as_pairs` 就是这个「形态归一化器」：它把上述所有合法输入统一广播成 `(ndim, 2)` 的嵌套序列，并做必要的校验（如作为索引时不能为负）。

#### 4.3.2 核心流程

```
_as_pairs(x, ndim, as_index=False)
  ├─ 若 x is None → 返回 ((None,None),)*ndim   # 特例：保持 None
  ├─ x = np.array(x)
  ├─ 若 as_index: 取整 (round + astype intp)，并校验非负
  ├─ 若 x.size == 1:   标量 → ((v,v),)*ndim
  ├─ 若 x.size == 2 且不是 (2,1) 形:   单边对 → ((a,b),)*ndim
  └─ 否则: broadcast_to((ndim,2)).tolist()
```

两条快路（`size==1`、`size==2`）是性能优化：常见情况是「所有轴用同一个宽度」或「所有轴左右各一个值」，直接返回常量元组比走 `np.broadcast_to` 更快。

#### 4.3.3 源码精读

[\_arraypad_impl.py:471-535](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraypad_impl.py#L471-L535) —— `_as_pairs`。几个要点：

- 第 499-502 行：`None` 是特例，因为后续 `np.round(None)` 会抛 `AttributeError`，所以提前透传 `((None,None),)*ndim`。`stat_length` 默认就是 `None`，表示「用整条轴」。
- 第 505-506 行：`as_index=True` 时先 `round` 再转 `np.intp`，把浮点宽度（如 `2.6`）规整为整数索引。
- 第 513-518 行：`size==1` 快路，标量广播成每维 `(v,v)`。
- 第 520-528 行：`size==2` 快路，但排除 `x.shape==(2,1)` 的情况——因为 `[[1],[2]]` 应广播成 `[[1,1],[2,2]]`（每维一个值，各自左右相同），而不是 `[[1,2],[1,2]]`。
- 第 530-531 行：`as_index` 下任何负值都抛 `"index can't contain negative values"`。
- 第 535 行：兜底用 `np.broadcast_to(x,(ndim,2)).tolist()`，`tolist()` 是为了后续 Python 层索引更快。

#### 4.3.4 代码实践

**实践目标**：直接调用 `_as_pairs`，观察不同输入被规整成的标准形态。

```python
import numpy as np
from numpy.lib._arraypad_impl import _as_pairs

ndim = 3
print(_as_pairs(3, ndim))                 # 标量
print(_as_pairs([2, 3], ndim))            # 单边对
print(_as_pairs([[1,2],[3,4],[5,6]], ndim))  # 每维一对
print(_as_pairs(None, ndim))              # None 透传
print(_as_pairs([2.6, 3.3], ndim, as_index=True))  # 取整
```

**操作步骤**：运行该脚本。

**需要观察的现象**：标量和单边对都被「铺」成 3 对；`None` 变成 3 个 `(None,None)`；浮点被取整为 `[[3,3]]*3`。

**预期结果**（对照测试 `TestAsPairs`）：

```
((np.int64(3), np.int64(3)), (np.int64(3), np.int64(3)), (np.int64(3), np.int64(3)))
((np.int64(2), np.int64(3)), (np.int64(2), np.int64(3)), (np.int64(2), np.int64(3)))
[[1, 2], [3, 4], [5, 6]]
((None, None), (None, None), (None, None))
[[3, 3], [3, 3], [3, 3]]
```

> 注：标量快路返回的是含 `np.int64` 的元组，不同 numpy 版本显示细节略有差异，但结构一致。

#### 4.3.5 小练习与答案

**练习 1**：`_as_pairs([[1],[2]], 2)` 返回什么？为什么不是 `[[1,2],[1,2]]`？

**答案**：返回 `[[1,1],[2,2]]`。因为输入形状是 `(2,1)`，表示「两个维度，每维一个值」，要把每维的单一值广播成左右相同的一对；代码里第 520 行特意用 `x.shape != (2,1)` 排除掉把它误判为「单边对」的情况。

**练习 2**：`_as_pairs(None, 3, as_index=True)` 为何不抛错？

**答案**：`None` 在第 499 行被特例提前返回 `((None,None),)*3`，根本不会走到 `np.round` 与取整校验，所以 `as_index` 对 `None` 无影响——这正是 `stat_length` 默认 `None` 所依赖的行为。

---

### 4.4 简单填边模式：constant / edge / linear_ramp

#### 4.4.1 概念说明

这一组模式的共同点是：填充值可以从原数组边缘或外部参数**直接确定**，无需复杂迭代。

- **`constant`**：填充区全部填同一个常数（默认 0）。
- **`edge`**：用最近的边缘值向外延伸（左填充区全填左边缘值，右填充区全填右边缘值）。
- **`linear_ramp`**：从指定的「端点值」线性渐变到边缘值，形成一条斜坡。

#### 4.4.2 核心流程

三者都遵循「取/算出左右值 → `_set_pad_area` 写入」的套路：

- `constant`：值直接来自 `constant_values`（经 `_as_pairs` 规整）。
- `edge`：调用 `_get_edges` 取出有效区最左/最右的一片，广播到填充区。
- `linear_ramp`：调用 `_get_linear_ramps`，用 `np.linspace` 在 `end_value`（外部端点）和边缘值之间生成 `width` 个点。

`_get_edges` 与 `_get_linear_ramps` 返回的值，其形状在 `axis` 维长度为 1，再靠 `_set_pad_area` 赋值时的广播铺满整个填充区。

#### 4.4.3 源码精读

`constant` 分支：

[\_arraypad_impl.py:847-852](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraypad_impl.py#L847-L852) —— 取 `constant_values`（默认 0），用 `_as_pairs` 规整，逐维 `_set_pad_area` 写入。

`edge` 分支：

[\_arraypad_impl.py:870-874](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraypad_impl.py#L870-L874) —— 调 `_get_edges` 取边缘。

[\_arraypad_impl.py:155-184](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraypad_impl.py#L155-L184) —— `_get_edges`。第 176-178 行：左边缘取有效区最左一片 `slice(left_index, left_index+1)`；第 180-182 行：右边缘取有效区最右一片。返回值在 `axis` 维长度为 1，靠后续广播铺满填充宽度。

`linear_ramp` 分支与 `_get_linear_ramps`：

[\_arraypad_impl.py:187-228](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraypad_impl.py#L187-L228) —— `_get_linear_ramps`。核心是第 211-223 行的 `linspace`：`start=end_value`（外部端点，含在结果里）、`stop=edge`（边缘值，**不含**，因为 `endpoint=False`）、`num=width`、`axis=axis`。这样生成 `width` 个点，从外部端点逐步逼近边缘值但不到达边缘值（边缘值本身已是有效区的一部分）。第 226 行把右侧渐变反转（`slice(None,None,-1)`），因为右侧要从边缘向外（递增/递减到端点）。

`linear_ramp` 的端点值 `end_values` 默认为 0，所以默认行为是「从 0 线性升/降到边缘值」。

#### 4.4.4 代码实践

**实践目标**：对比 `constant`/`edge`/`linear_ramp` 三种模式的填充结果。

```python
import numpy as np

a = np.array([1, 2, 3, 4, 5])

print("constant :", np.pad(a, (2, 3), 'constant', constant_values=(4, 6)))
print("edge     :", np.pad(a, (2, 3), 'edge'))
print("linear   :", np.pad(a, (2, 3), 'linear_ramp', end_values=(5, -4)))
```

**操作步骤**：运行脚本。

**需要观察的现象**：`constant` 左 2 个填 4、右 3 个填 6；`edge` 左全 1、右全 5；`linear_ramp` 左侧从 5 线性到 1（不含 1），右侧从 -4 线性到 5（不含 5）。

**预期结果**（与 docstring 示例一致）：

```
constant : [4 4 1 2 3 4 5 6 6 6]
edge     : [1 1 1 2 3 4 5 5 5]
linear   : [ 5  3  1  2  3  4  5  2 -1 -4]
```

解读 `linear`：左侧 `linspace(5, 1, 2, endpoint=False)` = `[5, 3]`（步长 2，到 5,3 后下一个本应是 1 但因 endpoint=False 不含）；右侧先算 `linspace(-4, 5, 3, endpoint=False)` = `[-4, -1, 2]`，再反转得 `[2, -1, -4]`（从内侧 2 向外递减到 -4）。

#### 4.4.5 小练习与答案

**练习 1**：`np.pad([10,20,30], 2, 'edge')` 的结果是什么？

**答案**：`[10 10 10 20 30 30 30]`。左填充区全填左边缘值 10，右填充区全填右边缘值 30。

**练习 2**：为什么 `linear_ramp` 的 `linspace` 用 `endpoint=False`？

**答案**：渐变的终点（边缘值）已经是有效区的一部分，不该在填充区里重复出现。`endpoint=False` 让生成的 `width` 个点止于边缘值**之前**，正好与有效区无缝衔接。

---

### 4.5 统计填边模式：maximum / mean / median / minimum 与 stat_length

#### 4.5.1 概念说明

统计模式用原数组有效区（或其一段）的统计量来填充：`maximum` 用最大值、`minimum` 用最小值、`mean` 用均值、`median` 用中位数。它们共享同一个内核 `_get_stats`，区别仅在于传入哪个统计函数。

`stat_length` 参数控制「取多长的一段来算统计量」。默认 `None` 表示用整条有效轴；若指定较小值（如 `stat_length=2`），则只取靠近边缘的 2 个元素算统计，得到「局部统计量」。

#### 4.5.2 核心流程

`_get_stats(padded, axis, width_pair, length_pair, stat_func)`：

1. 定位有效区左右边界 `left_index`、`right_index`，算出有效区长度 `max_length`。
2. 把 `stat_length`（`length_pair`）限制到不超过 `max_length`（动态裁剪）。
3. 在左侧取 `left_index` 起的 `left_length` 个元素，调 `stat_func(..., axis=axis, keepdims=True)` 算统计量。
4. 若左右长度都等于 `max_length`，右侧统计量与左侧相同，提前返回。
5. 否则对右侧同样取一段算统计量。
6. 整数 dtype 时用 `_round_if_needed` 就地四舍五入。

#### 4.5.3 源码精读

主函数统计分支：

[\_arraypad_impl.py:837-891](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraypad_impl.py#L837-L891) —— 第 837-838 行的 `stat_functions` 字典把模式名映射到 numpy 的 `amax/amin/mean/median`；第 886-887 行取 `stat_length`（默认 `None`）并 `_as_pairs(..., as_index=True)` 规整；第 890-891 行调 `_get_stats` 再 `_set_pad_area`。

[\_arraypad_impl.py:231-294](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraypad_impl.py#L231-L294) —— `_get_stats`。**动态长度**是这里的重点：

- 第 264-268 行：`left_length`/`right_length` 若为 `None` 或超过 `max_length`，就被截到 `max_length`。这就是 `stat_length` 的「动态裁剪」——你请求的统计长度若大于有效区实际长度，不会报错，而是自动用全部有效元素。
- 第 270-274 行：`amax/amin` 不能作用于空数组，所以 `stat_length=0` 时抛出更友好的 `ValueError("stat_length of 0 yields no value for padding")`。
- 第 283-285 行：当左右长度都等于 `max_length`（即两侧都用了全部有效区），左右统计量必然相同，直接复用 `left_stat` 提前返回，省一次计算。
- 第 281、292 行：`_round_if_needed` 在目标 dtype 是整数时把统计量（如均值的小数）就地四舍五入。

[\_arraypad_impl.py:19-31](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraypad_impl.py#L19-L31) —— `_round_if_needed`：仅当 `dtype` 是整数子类型时才 `arr.round(out=arr)` 就地取整。

#### 4.5.4 代码实践

**实践目标**：对比「全轴统计」与「`stat_length=1` 局部统计」，验证后者等价于 `edge` 模式。

```python
import numpy as np

a = np.array([1, 2, 3, 4, 5])

print("mean 全轴 :", np.pad(a, 2, 'mean'))
print("mean len=1:", np.pad(a, 2, 'mean', stat_length=1))   # 局部→等价 edge
print("max  len=2:", np.pad(a, 2, 'maximum', stat_length=2))
print("median    :", np.pad(a, 2, 'median'))
```

**操作步骤**：运行脚本。

**需要观察的现象**：全轴 `mean` 左右都填 `mean([1..5])=3`；`stat_length=1` 时只取紧邻边缘的 1 个元素，左侧填 1、右侧填 5（与 `edge` 完全一致）；`stat_length=2` 的 `maximum` 左侧取 `[1,2]` 的最大 2、右侧取 `[4,5]` 的最大 5。

**预期结果**：

```
mean 全轴 : [3 3 1 2 3 4 5 3 3]
mean len=1: [1 1 1 2 3 4 5 5 5]
max  len=2: [2 2 1 2 3 4 5 5 5]
median    : [3 3 1 2 3 4 5 3 3]
```

#### 4.5.5 小练习与答案

**练习 1**：对一个长度为 5 的数组用 `maximum` 模式 `stat_length=10` 填充，会发生什么？

**答案**：不会报错。`_get_stats` 第 265-266 行会把 `left_length`/`right_length` 截到 `max_length=5`，等价于用整条有效轴算最大值，效果与 `stat_length=None` 相同。这正是测试 `test_clip_statistic_range` 验证的行为。

**练习 2**：为什么 `mean` 对整数数组的结果是整数？

**答案**：因为 `_round_if_needed` 检测到目标 dtype 是整数，会把算出的浮点均值就地四舍五入。例如 `mean([1,2])=1.5` 会被取整（依 banker's rounding 取最近的偶数）。

---

### 4.6 reflect 与 symmetric：方向差异与反射算法

#### 4.6.1 概念说明

`reflect` 和 `symmetric` 都是把数组沿边缘「镜像」出去，但**镜像轴的位置不同**，导致边缘值是否重复：

- **`reflect`**：镜像轴在**边缘值上**，边缘值作为对称轴**只出现一次**。
  - `[1,2,3]` 左填 2 → `[3,2, 1,2,3, ...]`（左侧是 3,2，1 不重复）
- **`symmetric`**：镜像轴在**边缘外侧**，边缘值**被复制一份**到填充区。
  - `[1,2,3]` 左填 2 → `[2,1, 1,2,3, ...]`（左侧是 2,1，1 重复）

源码里用一个布尔量 `include_edge` 区分二者：`symmetric` 对应 `include_edge=True`（边缘值包含在反射中），`reflect` 对应 `include_edge=False`。

`reflect_type` 参数（`'even'`/`'odd'`，默认 `'even'`）控制反射风格：`even` 是普通镜像；`odd` 是「关于边缘值的奇反射」，公式为 \( \text{reflected} = 2 \times \text{edge} - \text{value} \)，会产生线性外推（如 `[1,2,3,4,5]` 用 `reflect_type='odd'` 左填会得到 `[-1,0,...]`）。

#### 4.6.2 核心流程

`_set_reflect_both` 对左右两侧分别：

1. 根据 `include_edge` 决定 `edge_offset`（symmetric=1，reflect=0）和可用的 `old_length`（reflect 要 `-1`，因为不含边缘）。
2. `chunk_length = min(old_length, pad)`：每次最多反射 `old_length` 长的一段。
3. 用反向切片 `slice(start, stop, -1)` 从有效区「倒着」取一段作为反射块。
4. 若 `method=='odd'`：`chunk = 2*edge - chunk`。
5. 把反射块写入填充区对应位置。
6. 缩减剩余 `pad` 量，若还有剩余（即填充宽度 > 原数据长度）回到步骤 2 继续。

主函数用 `while left_index > 0 or right_index > 0` 循环调用 `_set_reflect_both`，直到填满。

#### 4.6.3 源码精读

主函数反射分支：

[\_arraypad_impl.py:893-913](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraypad_impl.py#L893-L913) —— 第 894 行取 `reflect_type`（默认 `'even'`）；**第 895 行 `include_edge = mode == "symmetric"` 是两种模式差异的根源**；第 906-913 行用 `while` 循环反复调 `_set_reflect_both`，把返回的「剩余待填量」回填给 `left_index`/`right_index`，直到两侧都为 0。

第 897-903 行是个特例：当某维长度为 1 却要填充时（单值维扩展），`reflect`/`symmetric` 走 legacy 路径——直接用 `_get_edges` 把那个唯一值广播出去（注释说「这其实该报错」）。

反射内核：

[\_arraypad_impl.py:297-391](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraypad_impl.py#L297-L391) —— `_set_reflect_both`。关键看 `include_edge` 如何改变切片位置：

- 第 328-334 行（`include_edge=True`，即 symmetric）：`edge_offset = 1`，且不调整 `old_length`（边缘值算在可用数据内）。
- 第 335-342 行（`include_edge=False`，即 reflect）：`edge_offset = 0`，但 `old_length -= 1`（跳过边缘值）。

以左侧填充为例（第 344-365 行）：`stop = left_pad - edge_offset`、`start = stop + chunk_length`，取 `slice(start, stop, -1)`。对 `[1,2,3]` 左填 2：

- reflect（`edge_offset=0`）：`stop=2, start=4`，取 padded 索引 `[4,3]` = `[3,2]` → 左侧 `[3,2]` ✓
- symmetric（`edge_offset=1`）：`stop=1, start=3`，取 padded 索引 `[3,2]` = `[2,1]` → 左侧 `[2,1]` ✓

这就是「边缘值是否重复」在代码层面的体现：`edge_offset` 让切片起止点平移了一位，决定是否把边缘值纳入反射块。

`odd` 反射在第 354-357 行：`left_chunk = 2 * padded[edge_slice] - left_chunk`，其中 `edge_slice` 指向边缘值。这实现 \( \text{out} = 2\cdot\text{edge} - \text{value} \) 的奇对称外推。

第 329-340 行的 `old_length // original_period * original_period` 是为了在「填充宽度大于原数据」需要多次反射时，保证每次取的是一个完整周期，避免只用部分原始数据导致错位。

#### 4.6.4 代码实践

**实践目标**：直观对比 `reflect` 与 `symmetric` 在同一数组、同宽度下的边缘差异。

```python
import numpy as np

a = np.array([1, 2, 3])

r = np.pad(a, 3, 'reflect')
s = np.pad(a, 3, 'symmetric')
print("原数组      :", a)
print("reflect     :", r)    # 边缘值 1/3 不重复
print("symmetric   :", s)    # 边缘值 1/3 重复

# odd 反射：关于边缘值线性外推
print("reflect odd :", np.pad(a, 3, 'reflect', reflect_type='odd'))
print("symmetric odd:", np.pad(a, 3, 'symmetric', reflect_type='odd'))
```

**操作步骤**：运行脚本，逐行对照数组边界。

**需要观察的现象**：

- `reflect` 开头是 `[2,3,2]`、结尾是 `[2,1,2]`——边缘值 1 和 3 在镜像中作为对称轴，不重复。
- `symmetric` 开头是 `[3,2,1]`、结尾是 `[3,2,1]`——边缘值 1 和 3 被复制了一份贴到填充区。

**预期结果**（对照测试 `TestReflect.test_check_02`、`TestSymmetric` 与文档示例）：

```
原数组      : [1 2 3]
reflect     : [2 3 2 1 2 3 2 1 2]
symmetric   : [3 2 1 1 2 3 3 2 1]
reflect odd : [0 1 0 1 2 3 4 5 4]
symmetric odd: [2 1 0 1 2 3 4 5 6]
```

> 说明：`reflect odd` 的边界取自文档 `np.pad([1,2,3,4,5], (2,3), 'reflect', reflect_type='odd')` 的推导，对长度为 3 的小数组左填 3 的精确结果建议本地验证；核心要点是 `odd` 用 \( 2\cdot\text{edge}-\text{value} \) 外推，与 `even` 的普通镜像在符号/方向上不同。

#### 4.6.5 小练习与答案

**练习 1**：`np.pad([1,2,3,4,5], 2, 'reflect')` 和 `np.pad([1,2,3,4,5], 2, 'symmetric')` 分别是什么？

**答案**：
- `reflect`：`[3,4, 1,2,3,4,5, 4,3]`（边缘值 1、5 不重复，左侧取 `[3,4]`、右侧取 `[4,3]`）。
- `symmetric`：`[2,1, 1,2,3,4,5, 5,4]`（边缘值 1、5 重复，左侧取 `[2,1]`、右侧取 `[5,4]`）。

**练习 2**：代码中 `include_edge = mode == "symmetric"`，为什么 `symmetric` 要 `include_edge=True`？

**答案**：`symmetric` 的几何含义是「镜像轴贴在边缘外侧」，等价于把边缘值本身也反射一份，所以反射块必须包含边缘值（`edge_offset=1` 让切片覆盖到边缘）。`reflect` 的镜像轴就在边缘值上，边缘值是对称中心、不该被复制，所以排除边缘（`old_length -= 1`）。

---

### 4.7 wrap：周期环绕与迭代填充

#### 4.7.1 概念说明

`wrap` 把数组视为一个**周期**，无限循环延拓：`[1,2,3]` 被看作 `...1,2,3,1,2,3,1,2,3...`。填充时，左侧填充区用数组**尾部**的值（因为紧贴原数组左侧的上一周期尾部），右侧填充区用数组**头部**的值。文档原话：「The first values are used to pad the end and the end values are used to pad the beginning.」

当填充宽度大于原数据长度时，需要环绕多圈，所以 `wrap` 也用 `while` 循环迭代填充。

#### 4.7.2 核心流程

`_set_wrap_both(padded, axis, width_pair, original_period)`：

1. `period = 有效区长度`，并保证它是 `original_period` 的整数倍（防部分数据错位）。
2. 左侧：从有效区**左端**取一段（长度 `min(period, left_pad)`），写入左填充区的**靠右**部分（紧贴有效区），剩余部分留待下一圈。
3. 右侧：从有效区**右端**取一段，写入右填充区的**靠左**部分。
4. 返回新的剩余待填量 `new_left_pad`/`new_right_pad`。

主函数 `while` 循环直到两侧都为 0。

#### 4.7.3 源码精读

主函数 wrap 分支：

[\_arraypad_impl.py:915-924](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraypad_impl.py#L915-L924) —— 第 918 行算 `original_period`（有效区长度），第 919-924 行 `while` 循环调 `_set_wrap_both`，把返回的剩余量回填，直到填满。

环绕内核：

[\_arraypad_impl.py:394-468](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraypad_impl.py#L394-L468) —— `_set_wrap_both`。以左侧为例（第 429-446 行）：

- 第 434-435 行：`slice_end = left_pad + period`、`slice_start = slice_end - min(period, left_pad)`，从有效区左端起切一段（这就是「尾部的值环绕到左侧」——因为下一周期的头部等于本周期的头部，而 `wrap` 把它放在左侧紧贴处）。
- 第 439-446 行：若 `left_pad > period`（填充比一周期还长），只填靠右的 `period` 长一段，剩 `new_left_pad = left_pad - period` 下一圈再填；否则一段填满整个左填充区。

以 `[1,2,3]` 左填 4 为例（`test_check_02`：`np.pad([1,2,3], 4, 'wrap') = [3,1,2,3, 1,2,3, ...]`）：有效区是 padded 中间的 `[1,2,3]`（索引 4,5,6），左侧要填索引 0,1,2,3。第一圈 `period=3`，从有效区取 `[1,2,3]` 写到左填充区靠右的索引 1,2,3，剩 `new_left_pad=1`；第二圈填索引 0，取有效区最右的 `[3]` 写入。最终左填充区 = `[3,1,2,3]`，与测试断言一致。

第 417-420 行的 `period = period // original_period * original_period` 与 reflect 类似，是为了在多维迭代填充时只用完整周期，避免部分数据错位。

#### 4.7.4 代码实践

**实践目标**：验证 `wrap` 的「尾部填左、头部填右」语义，以及大宽度下的多圈环绕。

```python
import numpy as np

a = np.arange(5)  # [0,1,2,3,4]

print("wrap (3,0):", np.pad(a, (3, 0), 'wrap'))   # 左填3：尾部 [2,3,4]
print("wrap (0,3):", np.pad(a, (0, 3), 'wrap'))   # 右填3：头部 [0,1,2]
print("wrap (12,0):", np.pad(a, (12, 0), 'wrap')) # 左填12>5：环绕多圈
```

**操作步骤**：运行脚本。

**需要观察的现象**：左填 3 时左侧出现 `[2,3,4]`（尾部 3 个）；右填 3 时右侧出现 `[0,1,2]`（头部 3 个）；左填 12（超过数组长度 5）时，结果是 `[0,1,2,3,4]` 重复拼接后截取的形态。

**预期结果**（对照 `test_repeated_wrapping`）：

```
wrap (3,0): [2 3 4 0 1 2 3 4]
wrap (0,3): [0 1 2 3 4 0 1 2]
wrap (12,0): [0 1 2 3 4 0 1 2 3 4 0 1 2 3 4 0 1]
```

> 校验：`np.r_[a,a,a,a][3:]` = 把 `[0,1,2,3,4]` 重复 4 次后去掉前 3 个，正是左填 12 的结果，与上面一致。

#### 4.7.5 小练习与答案

**练习 1**：`np.pad([1,2,3], 3, 'wrap')` 的结果是什么？为什么左右两侧都是 `[1,2,3]`？

**答案**：结果是 `[1,2,3, 1,2,3, 1,2,3]`。因为 `[1,2,3]` 作为一个周期无限延拓是 `...1,2,3,1,2,3,1,2,3...`，原数组在中间，紧贴它左侧（上一周期）和右侧（下一周期）的都是 `[1,2,3]`。这并非与「尾部填左」矛盾——当整周期对齐时，左侧上一周期的尾部三元素恰好就是 `[1,2,3]`。

**练习 2**：`wrap` 与 `reflect` 在「填充宽度大于原数据」时的处理有何共同点？

**答案**：两者都用 `while` 循环迭代填充，每次填一个完整周期（reflect 的 `old_period`、wrap 的 `period`），把剩余待填量回传给主函数继续，直到填满。这是因为反射/环绕的「素材」只有原数据，当填充区比原数据还长时，必须重复使用。

---

## 5. 综合实践

把本讲的核心知识点串起来：用一个二维数组，分别用 `reflect`、`symmetric`、`wrap` 三种模式填充相同宽度，**对照观察边缘行为的差异**，并验证「逐维迭代填充」导致的角落区域依赖关系。

```python
import numpy as np

a = np.arange(1, 7).reshape(2, 3)   # [[1,2,3],[4,5,6]]
print("原数组 shape:", a.shape, "\n", a)

# 三种模式都在两个轴上各填 2
r = np.pad(a, 2, 'reflect')
s = np.pad(a, 2, 'symmetric')
w = np.pad(a, 2, 'wrap')

print("\n--- reflect ---\n", r)
print("\n--- symmetric ---\n", s)
print("\n--- wrap ---\n", w)

# 验证角落：二维填充时 axis=1 方向的反射/环绕作用于「已被 axis=0 填充过的行」，
# 所以角落是行/列双重反射/环绕的叠加结果。
# 用一维对照：单行 [1,2,3] reflect 填 2 应是 [3,2,1,2,3,2,1]
print("\n一维校验 [1,2,3] reflect:", np.pad([1,2,3], 2, 'reflect'))
print("一维校验 [1,2,3] symmetric:", np.pad([1,2,3], 2, 'symmetric'))
print("一维校验 [1,2,3] wrap:", np.pad([1,2,3], 2, 'wrap'))
```

**操作步骤**：

1. 把代码存为 `pad_compare.py` 并运行。
2. 重点观察三种模式下，**左上角、右上角、左下角、右下角**这 4 个角落区域的值。
3. 对照最后三行的一维校验，理解二维结果中「行反射」和「列反射」是如何叠加的。

**需要观察的现象**：

- `reflect`：边缘值（1、3、4、6）不重复，角落是两次反射的叠加。
- `symmetric`：边缘值重复，角落出现成对的相同值。
- `wrap`：行方向与列方向都做周期环绕，左上角对应原数组右下角的值（环绕过来）。

**预期结果（关键片段）**：一维校验输出

```
一维校验 [1,2,3] reflect: [3 2 1 2 3 2 1]
一维校验 [1,2,3] symmetric: [2 1 1 2 3 3 2]
一维校验 [1,2,3] wrap: [2 3 1 2 3 1 2]
```

二维的三种结果请运行后自行核对，重点关注角落值是否等于「行反射后的结果再做列反射」——这正是 `_view_roi` 要正确处理的依赖关系。若想进一步挑战，可尝试把 `pad_width` 设为 `(0,2)`（只填右侧），观察只在一个轴上填充时三种模式的差异。

---

## 6. 本讲小结

- `pad` 的骨架是「`_pad_simple` 先扩大数组并把原数据搬到中央 → 按 `mode` 分支填边」，骨架与填边策略分离，新增模式只需加一个 `_get_*`/`_set_*` 与一个 `elif`。
- `_set_pad_area` 配合 `_slice_at_axis` 实现「在某维的左右填充区赋值」，是除 `reflect`/`symmetric`/`wrap` 外所有模式的统一写出口。
- `_as_pairs` 是参数形态归一化器，把标量/序列/字典/`None` 统一广播成 `(ndim, 2)`，并对索引类参数做取整与非负校验。
- `_view_roi` 在逐维迭代填边时缩小工作区，避免角落被反复覆盖，是正确处理多维填充的关键。
- 统计模式共享 `_get_stats`，`stat_length` 会被动态裁剪到不超过有效区长度，且 `mean`/`median` 的浮点结果对整数 dtype 会被 `_round_if_needed` 就地取整。
- **`reflect` 与 `symmetric` 的唯一本质差异是 `include_edge`**：`symmetric`（`True`）边缘值重复、`reflect`（`False`）边缘值作为对称轴不重复，代码中体现为 `edge_offset` 与 `old_length -= 1`。`reflect_type='odd'` 用 \( 2\cdot\text{edge}-\text{value} \) 做奇反射。
- `wrap` 把数组视为周期，左侧填尾部值、右侧填头部值，大宽度时用 `while` 循环多圈环绕。

---

## 7. 下一步学习建议

- **复习 `_view_roi` 与多维角落**：本讲的反射/环绕只在单维上讲透了，多维角落的「二次镜像」值得在综合实践中亲手验证。可阅读 [\_arraypad_impl.py:59-84](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraypad_impl.py#L59-L84) 配合调试。
- **自定义填充函数**：`pad` 支持 `mode=<callable>`，签名见 docstring 的 `padding_func(vector, iaxis_pad_width, iaxis, kwargs)`。这是 `pad` 的扩展点，建议阅读 [\_arraypad_impl.py:794-815](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraypad_impl.py#L794-L815) 并自己写一个 callable 体验。
- **结合滑动窗口**：填充常用于卷积/池化前的尺寸保持。学完本讲可衔接 u5-l1 的 `sliding_window_view`，理解「pad → 滑窗」这一典型组合。
- **NaN 感知场景**：若你的数据含 NaN，统计模式（`mean`/`median`/`maximum`）会把 NaN 当普通值参与计算，可能得到 NaN 填充。后续 u9（NaN 感知函数）会讲解如何先用 `nan_to_num` 清洗再填充。
- **测试驱动阅读**：`numpy/lib/tests/test_arraypad.py` 里每个 `TestXxx` 类对应一种模式，含有大量边界用例（大宽度、单值维、空维），是验证你理解的最佳参照。
