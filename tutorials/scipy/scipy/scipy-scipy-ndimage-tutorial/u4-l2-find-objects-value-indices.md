# 切片定位 find_objects 与值索引 value_indices

## 1. 本讲目标

学完本讲后，你应该能够：

- 用 `scipy.ndimage.find_objects` 从一张标签数组里取出每个物体（连通区域）的**最小包围盒（bounding box）**，并用它直接裁剪原图。
- 说清 `max_label` 参数的默认行为、它对返回列表长度的影响，以及「标签缺失时返回 `None`」的规则。
- 用 `scipy.ndimage.value_indices` **一次扫描**就把数组里每个不同取值出现的所有坐标聚合到一个字典里，理解它为什么比反复 `np.where` 更高效。
- 读懂这两个函数在 Python 包装层（`_measurements.py`）与 C 内核（`src/nd_image.c`、`src/ni_measure.c`）之间的分工，能跟踪它们的调用链。

## 2. 前置知识

本讲承接 **u4-l1 连通区域标记 label**。那里我们学到：`ndimage.label` 把前景像素打上连续整数标签（背景为 0），返回 `(labeled_array, num_features)`。一个自然的问题随之而来——**拿到标签图之后，如何快速定位「第 i 号物体」在数组里的位置？** 本讲就回答这个问题，提供两种互补的工具。

先约定几个术语：

- **标签数组（labeled array）**：每个非零整数代表一个物体，0 代表背景。`find_objects` 的输入通常就是 `label` 的输出，但它其实接受任意整数数组。
- **包围盒 / bounding box**：能包住某个物体全部像素的最小「多维矩形」区域。在 2D 里是一个矩形子窗口，在 3D 里是一个长方体。
- **切片（slice）**：Python 的 `slice(start, stop)`。NumPy 里 `arr[slice(2,5), slice(2,5)]` 等价于 `arr[2:5, 2:5]`，`stop` 是**排他的**（不含端点）。
- **坐标元组 / index tuple**：像 `(array([2,2,3]), array([2,3,2]))` 这样的对象，可以直接用来索引数组 `arr[that_tuple]`，效果等价于花式索引。
- **键值仅关键字参数（keyword-only）**：函数签名里 `*` 之后的参数，调用时必须写 `func(arr, ignore_value=0)` 而不能写 `func(arr, 0)`。

一个贯穿全讲的关键直觉：**`find_objects` 关心「标签」，适合 label 之后按区域裁剪；`value_indices` 关心「取值」，适合任意整数数组（不一定是连续标签）按值分组**。两者都把「定位」这件事在 C 层用**单次或少数几次全数组扫描**做完，避免你在 Python 里写 `for val in values: np.where(arr == val)` 这种 O(V·N) 的低效循环。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [_measurements.py](_measurements.py) | Python 包装层。`find_objects`（L238）与 `value_indices`（L311）都在这里：做参数校验、`max_label` 默认值推导、`ignore_value` 包装，然后把真正的计算交给 C 内核。 |
| [src/nd_image.c](src/nd_image.c) | C 扩展 `_nd_image` 的入口。`Py_FindObjects`（L814）把包围盒结果组装成 Python 列表；`NI_ValueIndices`（L956）用「三遍扫描」算法构建值→坐标字典；`methods[]` 分发表（L1339-L1340）把 Python 名映射到 C 函数。 |
| [src/ni_measure.c](src/ni_measure.c) | `NI_FindObjects` 内核（L76）：逐点扫描，维护每个标签在各轴上的最小/最大坐标；`CASE_FIND_OBJECT_POINT` 宏（L40）是更新包围盒的核心。 |

调用链一览（以 `find_objects` 为例）：

```
ndimage.find_objects(input, max_label)        # 公开 API
  → _measurements.find_objects(...)           # Python 包装：校验、max_label 默认值
  → _nd_image.find_objects(input, max_label)  # methods[] 分发 → Py_FindObjects
  → NI_FindObjects(input, max_label, regions) # ni_measure.c：单遍扫描写 regions 数组
  → Py_FindObjects 把 regions 转成 list[slice | None] 返回
```

## 4. 核心概念与源码讲解

### 4.1 find_objects：定位每个标签的最小包围盒

#### 4.1.1 概念说明

`find_objects` 解决的问题是：给定一张标签数组，**为每个标签返回一个能包住该标签全部像素的最小多维切片**。

举一个直观例子（取自官方 docstring）。假设有：

```
array([[2, 2, 2, 0, 0, 3],
       [2, 2, 2, 0, 0, 0],
       [0, 0, 1, 1, 0, 0],
       [0, 0, 1, 1, 0, 0],
       [0, 0, 0, 0, 1, 0],
       [0, 0, 0, 0, 0, 0]])
```

标签 1 的像素出现在第 2-4 行、第 2-4 列，所以它的包围盒是 `(slice(2,5), slice(2,5))`（注意 `stop=5` 排他，覆盖下标 2、3、4）。直接 `a[那个切片]` 就能把这块「抠」出来：

```python
loc = ndimage.find_objects(a)[0]
a[loc]
# array([[1, 1, 0],
#        [1, 1, 0],
#        [0, 0, 1]])
```

注意抠出来的是**整块矩形**，包括里面值为 0 的「空洞」——包围盒只保证「最小」，不保证里面全是目标像素。

为什么这件事重要？在 3D 体数据（比如医学影像）里，你无法「看穿」一个立方体。有了每个物体的包围盒切片，你就能逐个把感兴趣的小立方体裁出来单独处理，而不用对整个大数组做计算。这正是 docstring Notes 里说的「isolate a volume of interest inside a 3-D array, that cannot be 'seen through'」。

#### 4.1.2 核心流程

`find_objects` 的算法非常朴素，本质是**一次全数组扫描 + 逐点扩张包围盒**：

```text
初始化：regions 数组，长度 = 2 * ndim * max_label，全部填 -1
       （每个标签占 2*ndim 个槽：ndim 个 start + ndim 个 end）

for 数组里每个点 (value, coordinates):
    label_index = value - 1            # 标签 1 → 下标 0；标签 0（背景）和 > max_label 的都跳过
    if label_index 不在 [0, max_label) 内: 跳过

    if regions[start_axis_0] == -1:    # 这个标签还没见过
        对每个轴 k: start[k] = coord[k]; end[k] = coord[k] + 1
    else:                              # 已经见过，扩张包围盒
        对每个轴 k:
            start[k] = min(start[k], coord[k])
            end[k]   = max(end[k],   coord[k] + 1)

# 扫描结束后，把 regions 翻译成 Python：
for 每个标签 i:
    if start[i] == -1:  结果[i] = None      # 该标签在数组里根本没出现
    else:               结果[i] = tuple(slice(start[i][k], end[i][k]) for k in axes)
```

要点：

1. **`end = coord + 1`**：因为 Python 切片 `stop` 是排他的，存 `coord + 1` 正好让 `slice(start, end)` 覆盖下标 `coord`。
2. **一次扫描**：复杂度 O(N)（N 是像素总数），与标签数无关。
3. **结果按 `label - 1` 索引**：返回列表的第 0 项对应标签 1。
4. **缺失标签 → `None`**：如果某个标签在数组里完全没出现，它的 `start` 保持初值 `-1`，于是翻译成 `None`。

#### 4.1.3 源码精读

**Python 包装层**——[_measurements.py:238-308](_measurements.py#L238-L308)：`find_objects` 的 Python 端极薄。它只做三件事：拒绝复数、推导 `max_label` 默认值、转交 C 内核。

关键两行（[_measurements.py:305-308](_measurements.py#L305-L308)）说明 `max_label` 默认行为与委托：

```python
if max_label < 1:
    max_label = input.max()
return _nd_image.find_objects(input, max_label)
```

注意 `find_objects` **不调用** `_get_output`——它不是「输入数组→输出数组」的滤波函数，而是「输入数组→Python 对象（列表）」，所以没有就地输出之说。

**C 包装函数**——[src/nd_image.c:814-845](src/nd_image.c#L814-L845)：`Py_FindObjects` 负责「申请内存 → 调内核 → 把整数数组翻译成 Python 列表」。

它先用 `PyArg_ParseTuple` 解析参数（[src/nd_image.c:823-825](src/nd_image.c#L823-L825)），`"O&n"` 表示「一个数组（经 `NI_ObjectToInputArray` 转换）+ 一个整数 `max_label`」。然后按 `2 * ndim * max_label` 申请 `regions` 缓冲区（[src/nd_image.c:831-832](src/nd_image.c#L831-L832)）：

```c
regions = (npy_intp*)malloc(2 * max_label * PyArray_NDIM(input) * sizeof(npy_intp));
```

调用内核填充它（[src/nd_image.c:842-843](src/nd_image.c#L842-L843)），最后把每个标签的 2 个整数（`start`、`end`）翻译成一个 `slice(start, end)`，组装进结果列表（[src/nd_image.c:851-885](src/nd_image.c#L851-L885)）。翻译时用 `regions[idx] >= 0` 判定标签是否存在，否则填 `Py_None`（[src/nd_image.c:854](src/nd_image.c#L854) 与 [src/nd_image.c:881-884](src/nd_image.c#L881-L884)）。

**C 内核**——[src/ni_measure.c:76-138](src/ni_measure.c#L76-L138)：`NI_FindObjects` 把 `regions` 全部初始化为 `-1`（[src/ni_measure.c:91-94](src/ni_measure.c#L91-L94)），然后用 `NI_InitPointIterator` 建立点迭代器，对每个点调用宏。

包围盒更新的核心在宏 `CASE_FIND_OBJECT_POINT`（[src/ni_measure.c:40-74](src/ni_measure.c#L40-L74)）。关键判断 `_sindex = *(_type *)_pi - 1`（[src/ni_measure.c:46](src/ni_measure.c#L46)）：把像素值减 1 当作标签下标，于是背景 0 变成 `-1`（被 `if (_sindex >= 0 && ...)` 过滤掉，[src/ni_measure.c:47](src/ni_measure.c#L47)）。首次见到某标签时设置 `start=coord, end=coord+1`（[src/ni_measure.c:50-55](src/ni_measure.c#L50-L55)）；之后取 `min(start)` 与 `max(end)`（[src/ni_measure.c:60-65](src/ni_measure.c#L60-L65)）。一个 `switch` 把这段逻辑特化到 13 种整型/浮点 dtype（[src/ni_measure.c:101-127](src/ni_measure.c#L101-L127)）。

`methods[]` 分发表把 Python 名钉到 C 函数指针（[src/nd_image.c:1339](src/nd_image.c#L1339)）：

```c
{"find_objects", (PyCFunction)Py_FindObjects, METH_VARARGS, NULL},
```

#### 4.1.4 代码实践

**实践目标**：亲手验证「包围盒 = 最小矩形」与「`label - 1` 索引」两条规则。

**操作步骤**：

```python
import numpy as np
from scipy import ndimage

a = np.zeros((6, 6), dtype=int)
a[2:4, 2:4] = 1      # 标签 1：一个 2x2 块
a[4, 4] = 1          # 标签 1：再加一个对角的孤点
a[:2, :3] = 2        # 标签 2：左上角 2x3 块
a[0, 5] = 3          # 标签 3：右上角一个点
print(a)

locs = ndimage.find_objects(a)
print(locs)           # 列表第 0 项对应标签 1
```

**需要观察的现象**：

- 标签 1 的包围盒是 `(slice(2,5), slice(2,5))`——因为标签 1 的像素横跨第 2、3、4 行与第 2、3、4 列，孤点 `(4,4)` 把包围盒「拉」大了。
- `locs[0]` 对应标签 1，`locs[1]` 对应标签 2，`locs[2]` 对应标签 3（即 `label - 1` 索引）。
- 用切片裁剪原图：`a[locs[0]]` 会返回一个 3x3 子数组，里面包含标签 1 的全部像素，也包含中间值为 0 的空洞。

**预期结果**：

```python
a[locs[0]]
# array([[1, 1, 0],
#        [1, 1, 0],
#        [0, 0, 1]])
```

逐个裁剪并打印每个标签的子区域：

```python
for label_id, loc in enumerate(locs, start=1):
    if loc is None:
        continue
    print(f"标签 {label_id} 的包围盒 {loc}，子区域形状 {a[loc].shape}")
```

> 本实践结果可直接运行得到，无需本地编译。

#### 4.1.5 小练习与答案

**练习 1**：如果一个标签数组的标签是 `[1, 0, 3]`（标签 2 缺失），`find_objects` 返回什么？

**答案**：返回长度为 3 的列表（因为 `max_label` 默认取 `input.max() = 3`），第 2 项（对应标签 2）是 `None`，其余两项是正常切片。`max_label` 决定了列表长度，缺失标签用 `None` 占位。

**练习 2**：为什么 `end` 要存 `coord + 1` 而不是 `coord`？

**答案**：因为 Python 切片 `slice(start, stop)` 的 `stop` 是排他的——`a[2:5]` 取下标 2、3、4。为了让 `slice(start, end)` 恰好覆盖像素所在的下标 `coord`，必须存 `coord + 1`。这样 `a[包围盒]` 才不会漏掉边界像素。

---

### 4.2 max_label 参数：声明搜索范围与控制内存

#### 4.2.1 概念说明

`max_label` 是 `find_objects` 唯一的额外参数，初看不起眼，但它同时控制三件事：

1. **搜索范围**：只有值在 `[1, max_label]` 内的像素才参与包围盒计算（C 端的 `_sindex < _max_label` 判断，[src/ni_measure.c:47](src/ni_measure.c#L47)）。比 `max_label` 大的标签会被**静默忽略**。
2. **返回列表长度**：结果列表恰好有 `max_label` 个元素，第 `i` 项对应标签 `i+1`。
3. **内存占用**：C 端要分配 `2 * ndim * max_label` 个整数的 `regions` 缓冲区（[src/nd_image.c:831-832](src/nd_image.c#L831-L832)）。`max_label` 越大，缓冲区越大。

这三点都源于同一个事实：`regions` 是一个**按下标直接寻址**的数组，而不是哈希表。标签 `L` 的包围盒永远存在 `regions[2*ndim*(L-1) ... ]`，所以必须预先知道「最大标签」才能开好槽位。

#### 4.2.2 核心流程

```text
Python 端：
  if max_label < 1:                    # 即默认值 0 或负数
      max_label = input.max()          # 扫一遍取最大值
  → _nd_image.find_objects(input, max_label)

C 端：
  if max_label < 0: max_label = 0      # 防御
  regions = malloc(2 * ndim * max_label)   # 开槽
  NI_FindObjects: 只统计 1 <= value <= max_label 的像素
  返回长度 == max_label 的列表
```

#### 4.2.3 源码精读

`max_label` 的默认推导在 [_measurements.py:305-306](_measurements.py#L305-L306)：当用户不传（默认 `0`）或传非正值时，用 `input.max()` 兜底。这意味着**即使数组里有空洞标签，结果也会覆盖到最大标签**，空洞处填 `None`。

C 端对 `max_label` 的两处使用：

- 分配 `regions`：[src/nd_image.c:827-840](src/nd_image.c#L827-L840)。注意当 `max_label == 0` 时跳过分配（`if (max_label > 0)`），返回空列表。
- 范围判断：[src/ni_measure.c:47](src/ni_measure.c#L47) 的 `if (_sindex >= 0 && _sindex < _max_label)`，丢弃越界标签。

一个值得注意的用法来自官方示例 `ndimage.find_objects(a == 1, max_label=2)`（[_measurements.py:291-292](_measurements.py#L291-L292)）：传入一个布尔数组（最大值 1）却指定 `max_label=2`，于是结果长度为 2，第 2 项（标签 2）因不存在而返回 `None`。这示范了「`max_label` 可以独立于数组实际最大值」——你想查几个标签，列表就有几项。

#### 4.2.4 代码实践

**实践目标**：体会 `max_label` 对「返回长度」「忽略大标签」「内存」的三重影响。

**操作步骤**：

```python
import numpy as np
from scipy import ndimage

a = np.zeros((6, 6), dtype=int)
a[2:4, 2:4] = 1
a[:2, :3] = 2
a[0, 5] = 3

# (a) 默认：max_label = a.max() = 3
print(len(ndimage.find_objects(a)))           # 3

# (b) 截断：只关心前两个标签
locs2 = ndimage.find_objects(a, max_label=2)
print(len(locs2), locs2)                       # 2，不含标签 3

# (c) 布尔数组 + 指定 max_label，制造 None 占位
print(ndimage.find_objects(a == 1, max_label=2))
# [(slice(2, 5, None), slice(2, 5, None)), None]
```

**需要观察的现象**：

- (b) 中标签 3 被静默丢弃——它大于 `max_label`，结果列表里没有任何痕迹（不是 `None`，而是直接没这一项）。
- (c) 中标签 2 在布尔数组里不存在，于是用 `None` 占位，列表长度仍是 2。

**预期结果**：列表长度始终等于 `max_label`（或默认的 `input.max()`）；超出 `max_label` 的标签消失，范围内但不存在的标签变 `None`。

> 本实践结果可直接运行得到，无需本地编译。

#### 4.2.5 小练习与答案

**练习 1**：一张 1000×1000 的标签图，真实标签从 1 编到 50，但有一处误填了标签 `10_000_000`。调用 `find_objects(a)`（不传 `max_label`）会发生什么？怎样避免？

**答案**：默认 `max_label = input.max() = 10_000_000`，C 端会分配 `2 * 2 * 10_000_000` 个整数的 `regions`（约 320 MB），且返回一个长度一千万、几乎全是 `None` 的列表，极度浪费内存。应显式传 `find_objects(a, max_label=50)`，只开 50 个槽。

**练习 2**：`max_label` 比数组的 `input.max()` 还大时，结果会怎样？

**答案**：列表长度等于 `max_label`，超出真实标签范围的那些项因为没有像素而保持初值 `-1`，翻译成 `None`。这正是「缺失标签 → None」规则的来源。

---

### 4.3 value_indices：一次扫描聚合所有取值的位置

#### 4.3.1 概念说明

`value_indices` 解决的是另一类定位问题：**给定任意整数数组，把每个不同取值出现的所有坐标，一次性收集到一个字典里**。

返回结构是一个普通 Python 字典：

```text
{
   值 v1: (array_of_axis0_coords, array_of_axis1_coords, ...),   # v1 的所有出现位置
   值 v2: (array_of_axis0_coords, array_of_axis1_coords, ...),
   ...
}
```

每个值对应一个**坐标元组**（与数组维数相同），可以直接 `arr[那个元组]` 取回该值的所有像素。本质上，它对每个值 `v` 都给出 `np.where(arr == v)` 的结果，但只扫描数组常数次，而不是「每个值扫一遍」。

为什么要单独做这件事？docstring 的 Notes 给了动机（[_measurements.py:341-354](_measurements.py#L341-L354)）：对一个大数组、很多不同取值的场景（比如一张分类/分割图），如果你写

```python
for val in np.unique(arr):
    coords = np.where(arr == val)     # 每个值都要全数组扫一遍
```

复杂度是 O(V·N)（V 个不同值、N 个像素）。`value_indices` 把它降到约 3·N——「一次搜索，所有取值的索引都存下来」。这在把一张分类图与另一张同形状的数据图配对、做逐类统计时特别有用，是 `ndimage.mean()` / `ndimage.variance()` 这类按标签统计函数的更灵活替代（[_measurements.py:350-354](_measurements.py#L350-L354)）。

它和 `find_objects` 的区别要分清：`find_objects` 输入通常是**连续标签**、返回**包围盒切片**；`value_indices` 输入是**任意整数**（不必连续、不必从 1 开始）、返回**逐像素坐标**。前者粗（一个矩形），后者细（每个像素的精确位置）。

#### 4.3.2 核心流程

C 内核 `NI_ValueIndices` 用**三遍扫描**完成（注释见 [src/nd_image.c:904-906](src/nd_image.c#L904-L906)）：

```text
Pass 1：求最小值 min 与最大值 max（跳过 ignore_value）
Pass 2：建直方图 hist，长度 = max - min + 1
        遍历数组，hist[val - min] += 1          # 用「值 - min」作下标
        对每个 hist[i] > 0 的值，预分配 shape=(count,) 的 ndim 个索引数组
Pass 3：再遍历一遍，按值的计数器把每个像素的坐标写入对应索引数组
        最后把「值 → 索引元组」塞进字典
```

关键设计：它用**值减去最小值**作为直方图下标（`ii = val - VALUEINDICES_MINVAL`，见宏 `CASE_VALUEINDICES_MAKEHISTOGRAM`，[src/nd_image.c:929-946](src/nd_image.c#L929-L946)）。这意味着直方图覆盖 `[min, max]` 这段**连续区间**，而不是只记实际出现的值。因此：

- 如果取值稠密（如 0,1,2,…,255），非常高效。
- 如果取值稀疏（如只有 0 和 1_000_000），直方图会开一百万个槽，其中绝大部分为 0——这是用空间换简单性的取舍。最终字典只收录 `hist[i] > 0` 的值，所以字典本身不会膨胀，但内部 `hist` 数组可能很大。

`ignore_value` 参数允许跳过某个值（典型是 0 背景）。Python 端把它包成一个长度为 1 的 NumPy 数组传给 C，并另传一个布尔 `ignoreIsNone` 标志，这样 C 端不必处理 Python 的 `None`（[_measurements.py:413-422](_measurements.py#L413-L422)）。

#### 4.3.3 源码精读

**Python 包装层**——[_measurements.py:311-423](_measurements.py#L311-L423)：`value_indices` 的 Python 端主要在做 `ignore_value` 的封装。注意签名里的 `*`（[_measurements.py:311](_measurements.py#L311)）：`ignore_value` 是**关键字专用参数**，必须 `value_indices(a, ignore_value=0)` 而不能位置传参。

封装逻辑（[_measurements.py:416-422](_measurements.py#L416-L422)）：

```python
arr = np.asarray(arr)
ignore_value_arr = np.zeros((1,), dtype=arr.dtype)
ignoreIsNone = (ignore_value is None)
if not ignoreIsNone:
    ignore_value_arr[0] = ignore_value_arr.dtype.type(ignore_value)

val_indices = _nd_image.value_indices(arr, ignoreIsNone, ignore_value_arr)
```

这样 C 端永远收到一个「与 `arr` 同 dtype 的 1 元素数组」+ 一个布尔标志，逻辑得以简化。

**C 内核**——[src/nd_image.c:956-1030+](src/nd_image.c#L956-L1030)：`NI_ValueIndices` 的骨架清晰对应三遍扫描。

- 第一遍求 min/max：[src/nd_image.c:990-1009](src/nd_image.c#L990-L1009)，用宏 `CASE_VALUEINDICES_SET_MINMAX`（[src/nd_image.c:916-928](src/nd_image.c#L916-L928)），跳过 `ignore_value`。
- 第二遍建直方图：[src/nd_image.c:1014-1030](src/nd_image.c#L1014-L1030)，宏 `CASE_VALUEINDICES_MAKEHISTOGRAM`（[src/nd_image.c:929-946](src/nd_image.c#L929-L946)）算出 `numPossibleVals = max - min + 1` 并统计每个值出现次数。
- 然后为每个 `hist[ii] > 0` 的值预分配 `ndim` 个长度为 `count` 的 `NPY_INTP` 索引数组（[src/nd_image.c:1047-1073](src/nd_image.c#L1047-L1073)），第三遍填充坐标。

注意它只接受**整数数组**，`if (!PyTypeNum_ISINTEGER(arrType))` 直接报错（[src/nd_image.c:975-978](src/nd_image.c#L975-L978)），且特化了 8/16/32/64 位有符号与无符号整数（[src/nd_image.c:994-1006](src/nd_image.c#L994-L1006)）。

`methods[]` 把它钉到 C 函数（[src/nd_image.c:1340](src/nd_image.c#L1340)）：

```c
{"value_indices", (PyCFunction)NI_ValueIndices, METH_VARARGS, NULL},
```

#### 4.3.4 代码实践

**实践目标**：用 `value_indices` 按值分组打印坐标，并体会它相对 `np.where` 循环的便利。

**操作步骤**：

```python
import numpy as np
from scipy import ndimage

a = np.zeros((6, 6), dtype=int)
a[2:4, 2:4] = 1
a[4, 4] = 1
a[:2, :3] = 2
a[0, 5] = 3

vi = ndimage.value_indices(a)
print(vi.keys())          # dict_keys([0, 1, 2, 3])（标量类型为 np.int64）

for val, idx in sorted(vi.items()):
    print(f"值 {val} 出现在 {a[idx]}，共 {len(a[idx])} 处")

# 用坐标元组反向索引验证
ndx1 = vi[1]
print(a[ndx1])            # array([1, 1, 1, 1, 1])

# 跳过背景 0
vi2 = ndimage.value_indices(a, ignore_value=0)
print(vi2.keys())         # dict_keys([1, 2, 3])
```

**需要观察的现象**：

- 字典有 4 个键（0、1、2、3），其中 0（背景）也有自己的坐标元组——默认不忽略任何值。
- `vi[1]` 是 `(array([...]), array([...]))` 形式的元组，`len` 正好等于标签 1 的像素数（这里是 5：2x2 块 4 个 + 孤点 1 个）。
- 设 `ignore_value=0` 后，键里不再有 0。

**预期结果**：

```python
vi[1]
# (array([2, 2, 3, 3, 4]), array([2, 3, 2, 3, 4]))
a[vi[1]]
# array([1, 1, 1, 1, 1])
```

**进阶观察**：构造一个取值稀疏的数组，思考直方图开销：

```python
sparse = np.array([[0, 0], [0, 1000000]], dtype=np.int32)
vis = ndimage.value_indices(sparse)
print(vis.keys())         # 仍只有 0 和 1000000 两个键
```

虽然字典只有 2 项，但内部 C 端的 `hist` 会覆盖 `[0, 1000000]` 整段区间。这是「值减最小值作下标」设计的代价，使用大跨度稀疏取值时需留意内存。

> 本实践结果可直接运行得到，无需本地编译。

#### 4.3.5 小练习与答案

**练习 1**：`value_indices` 和「`for v in np.unique(a): np.where(a==v)`」相比，主要省在哪里？

**答案**：省在**扫描次数**。后者每个不同值都要全数组扫一遍，共 O(V·N)；`value_indices` 用三遍扫描（求 min/max、建直方图、填坐标）固定约 3·N 次访问，与取值个数无关，V 很大时优势显著。

**练习 2**：为什么 `value_indices` 要求输入是整数数组？

**答案**：它的算法用「值减最小值」作为直方图下标（`ii = val - min`），这只有在取值是离散整数时才有意义。浮点数无法这样直接映射到数组下标，所以 C 端在 [src/nd_image.c:975-978](src/nd_image.c#L975-L978) 直接拒绝非整数类型。

**练习 3**：`find_objects` 和 `value_indices` 各自适合什么场景？

**答案**：`find_objects` 适合**连续标签图**（典型是 `label` 的输出），返回每个物体的粗粒度**矩形包围盒**，便于裁剪子区域单独处理；`value_indices` 适合**任意整数数组**（取值不必连续），返回每个取值的**逐像素精确坐标**，便于按值分组做统计或配对。

## 5. 综合实践

把本讲两个工具串起来，模拟一次「分割后逐物体分析」的典型流程。

**任务**：构造一张含两个分离物体的二值图，用 `label` 标记，再用 `find_objects` 逐个裁出物体的子图、计算每个物体的像素数；然后对标签图本身调用 `value_indices`，验证它给出的坐标数与裁出的子图里该标签像素数一致。

```python
import numpy as np
from scipy import ndimage

# 1) 造图：两个不连通的物体 + 一些噪声背景
img = np.zeros((10, 10), dtype=int)
img[1:4, 1:4] = 1          # 物体 A：3x3 块（9 像素）
img[6:8, 6:9] = 1          # 物体 B：2x3 块（6 像素）

# 2) 标记
labeled, num = ndimage.label(img)
print("物体数：", num)      # 2

# 3) find_objects：逐物体裁剪并统计
locs = ndimage.find_objects(labeled)
for i, loc in enumerate(locs, start=1):
    sub = labeled[loc]                     # 裁出包围盒子图
    count = int((sub == i).sum())          # 子图里属于该标签的像素数
    print(f"物体 {i}：包围盒 {loc}，子图形状 {sub.shape}，实际像素 {count}")

# 4) value_indices：按标签值聚合所有坐标
vi = ndimage.value_indices(labeled, ignore_value=0)
for label_id in sorted(vi):
    coords = vi[label_id]
    print(f"标签 {label_id} 的坐标数：{len(labeled[coords])}")
```

**预期结果**：

- `find_objects` 报告物体 1 的子图形状为 `(3, 3)`、9 像素；物体 2 的子图形状 `(2, 3)`、6 像素。
- `value_indices` 给出标签 1 的坐标数 9、标签 2 的坐标数 6，与 `find_objects` 裁出的实际像素数完全一致——两种工具从不同粒度描述同一组物体。

**思考延伸**：若物体是凹形或带洞的，`find_objects` 的子图会包含不属于该物体的背景像素（所以才需要 `(sub == i)` 再筛一遍），而 `value_indices` 给出的坐标天然精确到每个目标像素。这正是「粗矩形 vs 精确坐标」的取舍。

> 本实践结果可直接运行得到，无需本地编译。

## 6. 本讲小结

- `find_objects` 对标签数组的每个标签返回一个**最小包围盒切片**，结果列表按 `label - 1` 索引；用 `a[loc]` 即可裁出子区域。
- 它的内核是**一次全数组扫描 + 逐点扩张**（`start=min`、`end=max(coord)+1`），复杂度 O(N)，C 端用 `regions` 数组按下标直接寻址（`src/ni_measure.c` 的 `CASE_FIND_OBJECT_POINT` 宏）。
- `max_label` 同时决定搜索范围、返回列表长度和 C 端 `regions` 缓冲区大小；默认取 `input.max()`，标签缺失处用 `None` 占位，超出范围的标签被静默忽略。
- `value_indices` 对任意整数数组返回 `{值: 坐标元组}` 字典，用 C 端**三遍扫描**（求 min/max、建直方图、填坐标）把多值定位降到常数次全数组访问。
- 它用「值减最小值」作直方图下标，故只接受整数；取值稀疏且跨度大时内部 `hist` 可能很大，需留意。
- 二者分工：`find_objects` 粗（连续标签 → 矩形）、`value_indices` 细（任意整数 → 逐像素坐标），常与 `label` 配合完成「标记 → 定位 → 分析」流程。

## 7. 下一步学习建议

- 掌握定位之后，下一步自然是**对每个区域做统计**。建议进入 **u4-l3 标签统计 sum/mean/var/min/max 与共享内核**，看 `sum` / `mean` / `variance` 等如何用 `labels` + `index` 参数只对指定区域聚合，它们与 `value_indices` 在「按区域统计」上是互补关系。
- 如果想了解更灵活的「任意聚合」，可继续阅读 **u4-l4 通用聚合、极值与分水岭** 中的 `labeled_comprehension`，它让你对每个标签区域套用任意 Python 函数。
- 想下探 C 层如何遍历 N-D 数组、`NI_Iterator` 与 `NI_ITERATOR_NEXT` 宏如何带回溯地推进多维坐标，可参考 **u6-l2 C 端迭代器、行缓冲与边界扩展**——本讲的 `NI_FindObjects` 与 `NI_ValueIndices` 都依赖这套迭代器抽象。
