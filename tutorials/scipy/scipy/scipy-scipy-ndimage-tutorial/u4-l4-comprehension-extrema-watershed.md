# 通用聚合、极值与分水岭

## 1. 本讲目标

本讲是「测量与连通区域」单元（u4）的收尾篇。在前三讲里，你已经学会了：用 `label` 把前景打上连通标签（u4-l1）、用 `find_objects`/`value_indices` 定位区域（u4-l2）、用 `sum`/`mean`/`minimum` 等做按标签统计（u4-l3）。

但现实中的测量需求远不止「求和、求均值、求极值」这几样：

- 想算每个区域的**90 分位数**怎么办？`_stats` 和 `_select` 都没有这个开关。
- 想一次拿到每个区域的**最小值、最大值以及它们的位置**，不想分别调用 4 个函数怎么办？
- 想算每个区域的**质心（加权中心）**怎么办？
- 想把一张梯度图按**种子点**分割成若干区域（即「分水岭」）怎么办？

本讲围绕 `_measurements.py` 中最后四个公开函数展开，回答上述全部问题。学完后你应当掌握：

1. `labeled_comprehension` 的**通用聚合模型**：如何把任意 Python 函数套到每个标签区域上，以及它的 `func` / `out_dtype` / `default` / `pass_positions` 四个参数如何协同。
2. `extrema` 如何一次返回最小/最大值及其 N-D 坐标；`center_of_mass` 如何用 `sum_labels` 计算加权质心；`histogram` 其实只是 `labeled_comprehension` 的一个特例。
3. `watershed_ift` 如何用 **IFT（Image Foresting Transform，图像森林变换）** 算法，在桶优先队列上以「路径最大边权」为代价，把 marker 标签沿结构元邻域传播开来，完成分割。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**直觉一：labeled_comprehension 是「区域版 list comprehension」。**

NumPy 里你常写 `[f(arr[arr>0]) for ...]`。`labeled_comprehension` 把它泛化成「对每个标签区域分别套用一个函数」，并额外解决三件事：区域为空时给默认值、控制输出 dtype、把结果按 `index` 顺序对齐返回。你可以把它当作整组统计函数的「逃生舱」——当预置的 `sum`/`mean`/`median` 不够用时，就退到它这里自己写聚合。

**直觉二：极值与位置是一对。**

光知道「区域 1 的最小值是 3」往往不够，你还想知道「这个 3 落在数组的哪一格」。`extrema` 把「值」和「位置」打包返回。位置在内部用**扁平下标**（把 N-D 坐标按行优先压成一个整数）存储，最后用 `np.unravel_index` 的等价算式还原成 N-D 坐标。

**直觉三：分水岭是「洪水淹没」的图论版本。**

经典的分水岭比喻：把图像看成地形，灰度值是海拔；从每个局部极小值（marker）开始往上「注水」，不同水源相遇处筑坝，坝就是区域边界。`watershed_ift` 用 IFT 算法把这个过程变成**最优化问题**：每个像素归属于那个能让「路径上最大高度差」最小的 marker。它用一个以代价为下标的**桶队列**（bucket queue）来高效地按代价从小到大处理像素。

> 本讲涉及的术语：通用聚合、扁平下标（flat index）、unravel、加权质心、IFT、桶优先队列（bucket queue）、路径代价函数（path-cost function）、marker、structure 元、对象 marker / 背景 marker。

## 3. 本讲源码地图

本讲几乎全部内容集中在 `_measurements.py`，只有 `watershed_ift` 的内核下沉到 C。

| 文件 | 关键函数 | 作用 |
|------|----------|------|
| [_measurements.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py) | `labeled_comprehension` | 通用聚合引擎：把任意函数套到每个标签区域 |
| [_measurements.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py) | `extrema` | 一次返回每区域 min/max 值及其位置 |
| [_measurements.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py) | `center_of_mass` | 用 `sum_labels` 算每区域加权质心 |
| [_measurements.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py) | `histogram` | 每区域直方图（委托给 `labeled_comprehension`）|
| [_measurements.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py) | `_select` | `extrema` 的底层内核（u4-l3 已讲）|
| [_measurements.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py) | `sum_labels` | `center_of_mass` 的底层内核（u4-l3 已讲）|
| [src/ni_measure.c](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/ni_measure.c) | `NI_WatershedIFT` | 分水岭 IFT 的 C 实现（桶队列 + 邻域传播）|
| [src/nd_image.c](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/nd_image.c) | `Py_WatershedIFT` / `methods[]` | 把 Python 调用桥接到 C 内核的包装与分发表 |

## 4. 核心概念与源码讲解

### 4.1 labeled_comprehension：通用聚合引擎

#### 4.1.1 概念说明

`labeled_comprehension` 解决的问题是：**当预置统计函数（`sum`/`mean`/`minimum`/`median`……）都不满足需求时，如何对每个标签区域套用任意自定义函数？**

它的语义等价于：

```python
result = np.array([func(input[labels == i]) if i in labels else default
                   for i in index])
```

但直接按上面写会很慢——每个 `input[labels == i]` 都要全数组扫描一次，复杂度 O(N · len(index))。源码通过「排序 + `searchsorted` 二分查找」把整体降到 O(N log N)。

它有四个关键参数：

- **`func`**：用户提供的聚合函数，接收一个 1-D 数组（该区域的所有像素值），返回一个标量。
- **`out_dtype`**：输出数组的 dtype。因为不同 `func` 返回类型各异（如分位数返回 float、众数返回 int），由调用方显式声明，避免类型推断的歧义。
- **`default`**：当某个 `index` 在 `labels` 里不存在（区域为空）时填入的值。
- **`pass_positions`**：若为 True，额外把每个像素的**扁平下标**作为第二个参数传给 `func`，用于「既需要值又需要位置」的场景（如求加权重心）。

#### 4.1.2 核心流程

```
1. 规范化 input / labels（广播到同形状）
2. 用 index 的 min/max 裁出一个子掩码，减少后续排序规模
3. 把裁出的 (input, labels[, positions]) 按 labels 排序
   → 相同标签的像素在内存里变成连续段
4. 对每个想要的 index，用 np.searchsorted 在已排序的 labels 上
   二分找到该标签的连续段 [l, h)
5. 对每段调用 func(input[l:h])，写入临时结果 temp[i]
6. 把 temp 按 index 原始顺序重排回 output
   （因为第 3/4 步可能改了 index 的顺序）
7. 缺失标签处保持初始化时填的 default
```

第 3 步是关键：排序后，「取出标签 i 的所有像素」从「全扫描」变成「二分查找一段连续区间」，这正是性能提升的来源。

#### 4.1.3 源码精读

函数签名与文档明确给出「等价于 `[func(input[labels == i]) for i in index]`」的语义契约：

[_measurements.py:426-427](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py#L426-L427) — `def labeled_comprehension(input, labels, index, func, out_dtype, default, pass_positions=False)`。文档串首行写明 Roughly equivalent to `[func(input[labels == i]) for i in index]`。

**裁剪优化**：用 `index` 的最小/最大值圈出一个范围，只保留落在 `[lo, hi]` 内的像素。注意 `index` 里的值可能远小于 `labels.max()`，这一步能显著缩小后续要排序的数据量：

[_measurements.py:532-542](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py#L532-L542) — 先 `lo = index.min(); hi = index.max()`，构造 `mask = (labels >= lo) & (labels <= hi)`，再用 `labels = labels[mask]` 等三行把 input/labels/positions 都按掩码压缩并拉平。

**按 labels 排序**：这一步把同一标签的像素搬到连续位置，为后续 `searchsorted` 做准备。同时单独保存 `index` 自身的排序顺序 `index_order`，供最后还原：

[_measurements.py:544-552](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py#L544-L552) — `label_order = labels.argsort()` 重排 input/labels/positions；`index_order = index.argsort()`、`sorted_index = index[index_order]` 得到升序的 index。

**核心映射 `do_map`**：在已排序的 `labels` 上，用 `np.searchsorted` 两侧（`side='left'` 与 `side='right'`）夹出每个 `sorted_index` 对应的连续段 `[l, h)`，对非空段调用 `func`：

[_measurements.py:554-566](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py#L554-L566) — `lo = np.searchsorted(labels, sorted_index, side='left')`、`hi = np.searchsorted(labels, sorted_index, side='right')`；循环里 `if l == h: continue`（缺失标签跳过，保留 default），否则 `output[i] = func(*[inp[l:h] for inp in inputs])`。

**默认值填充与顺序还原**：先用 `default` 初始化整个 `temp`（这样缺失标签自然得到默认值），跑完 `do_map` 后，再按 `index_order` 把 `temp` 重排回 `index` 的原始顺序：

[_measurements.py:568-578](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py#L568-L578) — `temp = np.empty(index.shape, out_dtype); temp[:] = default`，调用 `do_map`，最后 `output[index_order] = temp` 还原顺序；若 `as_scalar` 则取 `output[0]` 返回标量。

> **承接 u4-l3**：回忆 `_stats` 用 `np.bincount`、`_select` 用一次 `argsort` + fancy indexing。`labeled_comprehension` 走的是另一条路——它牺牲了一些速度（多一次排序），换取了「任意 `func`」的完全通用性。`histogram`（4.2.3）正是因为要调用 NumPy 的 `np.histogram`（无法塞进 `_stats`/`_select` 的固定框架），才选择直接复用 `labeled_comprehension`。

#### 4.1.4 代码实践

**实践目标**：亲手用 `labeled_comprehension` 计算每个标签区域的 90 分位数，并验证 `default` 在缺失标签上的行为。

**操作步骤**（这是示例代码，可直接运行）：

```python
# 示例代码
import numpy as np
from scipy import ndimage

a = np.array([[1, 2, 0, 0],
              [5, 3, 0, 4],
              [0, 0, 0, 7],
              [9, 3, 0, 0]])
lbl, nlbl = ndimage.label(a)          # nlbl = 3 个连通区域
print("labels:\n", lbl, " num=", nlbl)

# 1) 对每个区域算 90 分位数；注意显式给出 out_dtype=float、default=-1
lbls = np.arange(1, nlbl + 1)
p90 = ndimage.labeled_comprehension(
    a, lbl, lbls, lambda v: np.percentile(v, 90), float, -1)
print("per-region p90:", p90)

# 2) 故意包含一个不存在的标签 99，观察 default 生效
p90b = ndimage.labeled_comprehension(
    a, lbl, np.append(lbls, 99), lambda v: np.percentile(v, 90), float, -1)
print("with missing label 99:", p90b)   # 最后一项应为 -1
```

**需要观察的现象**：
- `p90` 长度等于 `lbls` 长度（3），每个值是该区域像素的 90 分位数。
- `p90b` 长度为 4，最后一项是 `-1`（`default`），证明缺失标签处被默认值填充。

**预期结果**：
- 区域 1（值 `[1,2,5,3]`）90 分位数约为 `4.3`；区域 2（`[4,7]`）约为 `6.7`；区域 3（`[9,3]`）约为 `8.4`。具体数值可能因 NumPy 分位数插值方法略有差异，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：若把 `out_dtype` 写成 `int`，而 `func` 返回浮点数，会发生什么？

**答案**：`temp` 是 `int` 数组，`output[i] = func(...)` 时浮点结果会被**截断/强转**成 int 存入（小数部分丢失）。这正是 `out_dtype` 要由调用方显式声明的原因——它决定了结果的精度。

**练习 2**：用 `pass_positions=True` 实现一个简化版 `center_of_mass`（只算第 0 轴的加权重心）。

**答案**：
```python
# 示例代码
def com_axis0(vals, pos):
    return (vals * pos).sum() / vals.sum()
res = ndimage.labeled_comprehension(
    a, lbl, lbls, com_axis0, float, 0, pass_positions=True)
```
其中 `pos` 是该区域像素在扁平数组中的下标。注意真正的 `center_of_mass`（4.2）按每个 N-D 轴分别算，而非用扁平下标，故结果不会完全相同——本练习仅演示 `pass_positions` 的用法。

---

### 4.2 extrema / center_of_mass / histogram：极值、质心与直方图

#### 4.2.1 概念说明

这三个函数分别覆盖「极值 + 位置」「加权中心」「值分布」三类常见测量，但实现路径截然不同：

- **`extrema`**：把 u4-l3 学过的 `_select`（一次 `argsort` + fancy indexing）开到「全功率」——同时打开 `find_min`/`find_max`/`find_min_positions`/`find_max_positions` 四个开关，**一次遍历**就拿到每区域的最小值、最大值及它们的 N-D 坐标。它只是 `_select` 的一层薄包装，外加把扁平下标还原成 N-D 坐标。

- **`center_of_mass`**：质心的定义是「以像素值为权重的加权平均坐标」。对第 \(d\) 轴，质心坐标为

  \[
  c_d = \frac{\sum_i x_i^{(d)} \cdot v_i}{\sum_i v_i},
  \]

  其中 \(x_i^{(d)}\) 是像素 \(i\) 在第 \(d\) 轴的坐标、\(v_i\) 是它的值。它**完全没有新内核**，而是把分子分母都表达成 `sum_labels`，复用 u4-l3 的 `_stats` 引擎。

- **`histogram`**：对每个区域算值分布。它直接把 NumPy 的 `np.histogram` 包成一个闭包，交给 `labeled_comprehension`（4.1）执行——是 4.1 的最典型用例。

#### 4.2.2 核心流程

**`extrema` 流程**：
```
1. 计算 dim_prod（用于把扁平下标还原成 N-D 坐标）
2. 调用 _select(find_min=find_max=find_min_positions=find_max_positions=True)
3. _select 内部：
   a. positions = arange(size).reshape(shape)   # 每个像素的扁平下标
   b. order = input.ravel().argsort()            # 按值排序
   c. min:  反向赋值 mins[labels[::-1]] = input[::-1]  → 「先写的被后写覆盖」→ 保留最小
   d. max:  正向赋值 maxs[labels]    = input       → 保留最大
   （min_position / max_position 同理，用 positions 数组）
4. 把返回的扁平位置 // dim_prod % dims 还原成 N-D 坐标元组
```

**`center_of_mass` 流程**：
```
1. normalizer = sum_labels(input, labels, index)            # 分母 Σv
2. grids = np.ogrid[...]                                    # 每轴的坐标网格
3. 对每个轴 d：result_d = sum_labels(input * grids[d], ...) / normalizer
4. 标量 index → 返回 tuple；序列 index → 返回 list of tuples
```

**`histogram` 流程**：
```
1. _bins = np.linspace(min, max, bins+1)        # 全局 bin 边界
2. def _hist(vals): return np.histogram(vals, _bins)[0]
3. return labeled_comprehension(input, labels, index, _hist, object, None)
```

#### 4.2.3 源码精读

**`extrema`：扁平下标 → N-D 坐标。** 关键两行：先算 `dim_prod`（即「行优先展开时各轴的步长」），再调用 `_select` 开满四个开关：

[_measurements.py:1460-1469](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py#L1460-L1469) — `dim_prod = np.cumprod([1] + list(dims[:0:-1]))[::-1]`（等价于 `np.unravel_index` 所需的列步长，例如 2D 形状 (4,5) 得 `[5,1]`）；随后 `minimums, min_positions, maximums, max_positions = _select(input, labels, index, find_min=True, find_max=True, find_min_positions=True, find_max_positions=True)`。

下标还原用整除取模。对扁平下标 `r`、各轴尺寸 `dims`、步长 `dim_prod`，第 \(k\) 轴坐标为 `(r // dim_prod[k]) % dims[k]`：

[_measurements.py:1471-1480](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py#L1471-L1480) — 标量分支 `tuple((min_positions // dim_prod) % dims)`；序列分支用列表推导 `min_positions.reshape(-1,1) // dim_prod) % dims` 把每个位置转成 N-D 坐标元组。

> 「先写被后写覆盖」是 `_select` 的精髓。要理解它，看这两行：

[_measurements.py:996-1007](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py#L996-L1007) — `mins[labels[::-1]] = input[::-1]`（**反向**赋值取最小：相等下标中后写的覆盖先写的，反向即让大值先写、小值后写，最终留下最小值）；`maxs[labels] = input`（**正向**赋值取最大：input 已升序排序，相等下标里大值后写、覆盖小值）。这正是 u4-l3 讲过的「五开关」模型，`extrema` 只是一次性打开了与极值相关的全部开关。

**`center_of_mass`：质心 = 加权和 / 总和。** 三行核心代码，分子分母都委托给 `sum_labels`（u4-l3），没有任何新内核：

[_measurements.py:1546-1551](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py#L1546-L1551) — `normalizer = sum_labels(input, labels, index)`（分母）；`grids = np.ogrid[...]`（用 `ogrid` 生成各轴的稀疏坐标网格，乘 `input` 时自动广播）；`results = [sum_labels(input * grids[dir].astype(float), labels, index) / normalizer for dir in range(input.ndim)]`（每轴一个加权和除以总质量）。

注意三处细节：① `grids[dir].astype(float)` 把坐标转成浮点，避免整型数组相乘溢出；② `np.ogrid[[slice(0,i) for i in shape]]` 用切片列表语法生成各轴坐标（第 d 轴形状为在 d 维上是满的、其余维为 1，便于广播）；③ 当 `sum_labels` 为 0（如 `[-1,1]` 这种总质量为 0 的数组）时除零得到 `inf/nan`，函数只发 `RuntimeWarning` 不报错——docstring 的 `d = np.array([-1, 1])` 示例正是演示这点。

**`histogram`：labeled_comprehension 的最典型客户。** 它没有自己的内核，只是把 `np.histogram` 包成闭包 `_hist`，交给 4.1 的引擎逐区域执行；注意 `out_dtype=object`（因为返回的是数组而非标量）：

[_measurements.py:1612-1618](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py#L1612-L1618) — `_bins = np.linspace(min, max, bins+1)` 生成全局 bin 边界；`def _hist(vals): return np.histogram(vals, _bins)[0]`；最后 `return labeled_comprehension(input, labels, index, _hist, object, None, pass_positions=False)`。这一行证明了 4.1 的定位：当聚合逻辑复杂到无法塞进 `_stats`/`_select` 的固定框架时，就退回 `labeled_comprehension`。

#### 4.2.4 代码实践

**实践目标**：对同一张含 3 个区域的强度图，分别用 `extrema` 一次性取出极值与位置、用 `center_of_mass` 算质心，并对照验证。

**操作步骤**（示例代码）：

```python
# 示例代码
import numpy as np
from scipy import ndimage

a = np.array([[1, 2, 0, 0],
              [5, 3, 0, 4],
              [0, 0, 0, 7],
              [9, 3, 0, 0]])
lbl, nlbl = ndimage.label(a)
lbls = np.arange(1, nlbl + 1)

# 1) extrema 一次返回 4 项：(min值, max值, min位置, max位置)
mins, maxs, min_pos, max_pos = ndimage.extrema(a, lbl, lbls)
print("mins :", mins)        # 每区域最小值
print("maxs :", maxs)        # 每区域最大值
print("min_pos :", min_pos)  # [(0,0),(1,3),(3,1)]
print("max_pos :", max_pos)  # [(1,0),(2,3),(3,0)]

# 2) 对照 minimum / maximum（u4-l3）确认极值一致
print("check min:", ndimage.minimum(a, lbl, lbls))
print("check max:", ndimage.maximum(a, lbl, lbls))

# 3) center_of_mass：区域 1 的质心（行优先，下标从 0 起）
print("com:", ndimage.center_of_mass(a, lbl, lbls))
```

**需要观察的现象**：
- `mins`、`maxs` 与 `ndimage.minimum`/`maximum`（u4-l3）逐元素相等，但 `extrema` 只扫一遍就额外拿到了位置。
- `max_pos[2]` 是 `(3,0)`，对应值 9（区域 3 的最大值），位置坐标与数组下标一致。
- `center_of_mass` 对区域 1（4 个像素 (0,0)=1,(0,1)=2,(1,0)=5,(1,1)=3）给出加权质心，行坐标约为 (0·1+0·2+1·5+1·3)/(1+2+5+3) = 8/11 ≈ 0.727。

**预期结果**：`extrema` 返回 `(array([1,4,3]), array([5,7,9]), [(0,0),(1,3),(3,1)], [(1,0),(2,3),(3,0)])`（与 docstring 示例一致）。质心数值**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`extrema` 不传 `labels`/`index` 时返回什么形态？为什么 docstring 里 `ndimage.extrema(a)` 返回 `(0, 9, (0, 2), (3, 0))`？

**答案**：无 `labels` 时 `_select` 走 `single_group` 分支，对整个数组求极值，返回 4 个**标量**（不是数组）。最小值 0 出现在扁平下标 2 即 `(0,2)`；最大值 9 出现在扁平下标 12 即 `(3,0)`。

**练习 2**：为什么 `histogram` 的 `out_dtype` 选 `object` 而不是 `int`？

**答案**：`_hist` 返回的是一个**长度为 `bins` 的数组**（每个区域的直方图），不是标量。`object` dtype 允许数组的每个槽位存放一个 ndarray；若用 `int`，赋值数组时会报形状错误。

**练习 3**：用 `center_of_mass` 计算 `np.array([[0,0,0],[0,1,0],[0,0,0]])`（单个像素）的质心，并解释结果。

**答案**：返回 `(1.0, 1.0)`——唯一的非零像素位于 (1,1)，质心就是它自身。`sum_labels` 的分母为 1、分子在该轴为 `1*1=1`，故 `1/1=1`。

---

### 4.3 watershed_ift：基于 IFT 的标记驱动分水岭

#### 4.3.1 概念说明

`watershed_ift` 是本讲唯一有独立 C 内核的函数，它做的是**标记驱动的图像分割**：给定一张图（通常是梯度图）和若干**种子点（markers）**，把每个像素划归到「沿着代价最小的路径能到达的那个 marker」。

它基于 **IFT（Image Foresting Transform，图像森林变换）** 算法 [1]。IFT 把分割问题转化为图上的**最优森林**问题：

- 每个像素是一个节点，结构元定义邻接边。
- 每条边 (v,p) 的**弧权**（arc weight）取两端像素的灰度差 \(|I(p)-I(v)|\)。
- 一条路径的**代价**定义为路径上所有弧权的**最大值**：

  \[
  \mathrm{cost}(\pi) = \max_{(u,w)\in\pi} |I(u)-I(w)|.
  \]

- 每个 marker 是一棵树的根（代价 0）。每个非 marker 像素归属于那个能以**最小路径代价**到达它的 marker，并继承该 marker 的标签。

直觉上：代价 = 路径上「最陡的一级台阶」。水从 marker 出发，优先沿「台阶小」的路径蔓延；遇到山脊（大灰度差）就难越过，山脊自然成为不同 marker 的分界——这就是分水岭。

**marker 的正负约定**：

- **正数 marker**（>0）是「对象」标记，会被**优先**处理（同代价时对象先占地）。
- **负数 marker**（<0）是「背景」标记，在对象之后处理。
- **0** 表示该点不是 marker。

**为何 input 只接受 uint8 / uint16？** 因为 IFT 用的是**桶优先队列**（bucket queue）：把待处理像素按代价 `0..maxval` 分桶，从代价最小的桶开始处理。代价上界 = 图的最大灰度值 `maxval`，桶数组大小 = `maxval+1`。这只有在灰度值有界且不太大（uint8 最多 256 个桶、uint16 最多 65536 个桶）时才高效，故函数硬性限制 dtype。

#### 4.3.2 核心流程

```
Python 端（_measurements.py:watershed_ift）:
  1. 校验 input.dtype ∈ {uint8, uint16}
  2. structure 缺省 → generate_binary_structure(ndim, 1)（十字结构元）
  3. 校验 structure 形状全为 3、与 input 同维
  4. 校验 markers 为整型、与 input 同形状
  5. _get_output(output, input) 准备输出数组
  6. _nd_image.watershed_ift(input, markers, structure, output)

C 端（ni_measure.c:NI_WatershedIFT）:
  1. 扫一遍 input 求最大灰度 maxval
  2. 为每个像素建一个 NI_WatershedElement 节点
  3. 分配 maxval+1 个桶 (first[]/last[])，组成链式优先队列
  4. 扫 markers：对象 marker → 入桶 0 的队首；背景 marker → 入桶 0 的队尾；
     非 marker → cost = maxval+1（视为无穷）
  5. 传播阶段：for jj in 0..maxval:
       while 桶 jj 非空:
         v = 出队（标记 done）
         for 每个 structure 邻居 p（且未 done）:
           wvp = |I(p) - I(v)|                       # 这条边的弧权
           newcost = max(v.cost, wvp)               # 经过 v 到 p 的路径代价
           if newcost < p.cost:                     # 找到更便宜路径
             p.cost = newcost
             p.label = v.label                       # 继承标签
             把 p 移入桶 newcost
  6. 最终每个像素的 output 即其归属 marker 的标签
```

#### 4.3.3 源码精读

**Python 入口：校验与内核委托。**

[_measurements.py:1652-1654](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py#L1652-L1654) — `if input.dtype.type not in [np.uint8, np.uint16]: raise TypeError('only 8 and 16 unsigned inputs are supported')`。这个限制直接源于桶队列的设计（见下）。

[_measurements.py:1656-1657](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py#L1656-L1657) — `structure` 缺省时取 `_morphology.generate_binary_structure(input.ndim, 1)`（默认十字连通，承接 u5-l1）；随后强制 `np.asarray(structure, dtype=bool)` 并要求每维尺寸为 3。

[_measurements.py:1687-1688](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py#L1687-L1688) — `output = _ni_support._get_output(output, input)`（复用 u1-l4 的输出工具）后，`_nd_image.watershed_ift(input, markers, structure, output)` 把工作交给 C 内核。注意它**不返回内核结果**，而是直接写入 `output` 再返回——这是 ndimage 内核的一贯模式。

**C 包装与分发表**（承接 u6-l1 的 methods 分发表）：

[src/nd_image.c:1146-1156](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/nd_image.c#L1146-L1156) — `Py_WatershedIFT` 用 `PyArg_ParseTuple(args, "O&O&O&O&", ...)` 解析四个数组参数（input/markers/strct/output），调用 `NI_WatershedIFT(input, markers, strct, output)`。

[src/nd_image.c:1341](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/nd_image.c#L1341) — `{"watershed_ift", (PyCFunction)Py_WatershedIFT, METH_VARARGS, NULL}` 是 `methods[]` 分发表里的条目，把 Python 名 `watershed_ift` 映射到 C 包装。

**C 内核：节点结构。** 每个像素一个节点，用链表指针串成桶队列：

[src/ni_measure.c:204-209](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/ni_measure.c#L204-L209) — `typedef struct { npy_intp index; void *next, *prev; npy_uint32 cost; npy_uint8 done; } NI_WatershedElement;`。`next/prev` 是双向链表指针（同一桶内的元素串成链表），`cost` 是当前已知最优路径代价，`done` 标记是否已最终确定。

**桶队列分配。** 桶数量 = `maxval+1`（代价上界）：

[src/ni_measure.c:265-266](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/ni_measure.c#L265-L266) — `first = malloc((maxval + 1) * sizeof(...)); last = malloc((maxval + 1) * sizeof(...));`。每个代价等级一个桶，`first[c]`/`last[c]` 指向该桶链表的头/尾。这就是为何 input 必须是 uint8/uint16——`maxval` 受限，桶数组才不会爆炸。

**对象 vs 背景 marker 的入队顺序。** 二者都从代价 0（桶 0）出发，但对象入队首、背景入队尾，保证同代价时对象先被取出：

[src/ni_measure.c:331-345](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/ni_measure.c#L331-L345) — `if (label > 0)` 则 `temp[jj].next = first[0]; ...; first[0] = &(temp[jj])`（**头插**入桶 0）；`else`（负 marker）则 `temp[jj].prev = last[0]; ...; last[0] = &(temp[jj])`（**尾插**入桶 0）。注释写明：object markers 在队首→先处理，background markers 在队尾→后处理。

**传播阶段：核心代价计算与松弛。** 这十几行就是整个 IFT 算法的灵魂：

[src/ni_measure.c:403-404](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/ni_measure.c#L403-L404) — `for(jj = 0; jj <= maxval; jj++) { while (first[jj]) { ... } }`。外层按代价从小到大扫桶（等价于优先队列按 cost 出队），内层处理当前桶里所有节点。

[src/ni_measure.c:451-462](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/ni_measure.c#L451-L462) —
```c
wvp = pval - vval;
if (wvp < 0) wvp = -wvp;        // wvp = |I(p) - I(v)|  弧权
pcost = p->cost;
max = v->cost > wvp ? v->cost : wvp;   // newcost = max(路径代价, 这条边)
if (max < pcost) {              // 松弛：找到更优路径
    p->cost = max;              // 更新代价
    // 把 p 的标签设为 v 的标签（CASE_WINDEX2 写 output）
    // 把 p 移入桶 max
}
```
这正是「路径代价 = 路径上最大弧权」的松弛操作。当 `max < p.cost` 时，说明经由 v 到达 p 比之前已知的路径更便宜，于是更新 p 的代价、把 p 的标签改成 v 的标签、并把 p 重新入到更小的桶里等候传播。

> **小结**：`watershed_ift` 没有用通用的堆优先队列，而是利用「代价是 0..maxval 的整数」这一约束，用**桶数组**实现了 O(1) 出队的优先队列，整体复杂度接近 O(N · nneigh)。代价是 input 必须是 uint8/uint16。结构元决定了邻域连通性（默认 4-连通 / 十字），与 `label`（u4-l1）共用 `generate_binary_structure` 的概念。

#### 4.3.4 代码实践

**实践目标**：用 `watershed_ift` 对一张「方框边框 + 中心种子 + 角落背景种子」的二值图做分割，体会对象/背景 marker 与结构元连通性的影响。本例改编自测试 `test_watershed_ift01`。

**操作步骤**（示例代码，已对照 [tests/test_measurements.py:1465-1494](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/tests/test_measurements.py#L1465-L1494)）：

```python
# 示例代码
import numpy as np
from scipy import ndimage

# 一张 8x7 的图：中间一个 5x5 的空心方框（边=1，内部=0），外圈背景=0
data = np.array([[0,0,0,0,0,0,0],
                 [0,1,1,1,1,1,0],
                 [0,1,0,0,0,1,0],
                 [0,1,0,0,0,1,0],
                 [0,1,0,0,0,1,0],
                 [0,1,1,1,1,1,0],
                 [0,0,0,0,0,0,0],
                 [0,0,0,0,0,0,0]], dtype=np.uint8)

# markers：左上角(0,0)=-1 背景，方框中心(3,3)=1 对象
markers = np.zeros((8,7), dtype=np.int8)
markers[0,0] = -1     # 背景 marker
markers[3,3] = 1      # 对象 marker

# 1) 用 8-连通结构元（含对角）
struct8 = np.ones((3,3), dtype=bool)
out8 = ndimage.watershed_ift(data, markers, structure=struct8)
print("8-connectivity:\n", out8)

# 2) 用默认（4-连通十字）结构元
out4 = ndimage.watershed_ift(data, markers)
print("4-connectivity:\n", out4)
```

**需要观察的现象**：
- 8-连通下，对象标签 `1` 能沿对角穿过方框边、填满整个内部和边框，背景 `-1` 只占据最外圈（与 `test_watershed_ift01` 的 expected 一致）。
- 4-连通下，对象 `1` 只能上下左右蔓延，方框的四个角会留给背景 `-1`（与 `test_watershed_ift02` 的 expected 一致，角上是 `-1`）。这直观展示了结构元连通性对分割结果的巨大影响。

**预期结果**：8-连通时方框边框及内部全为 `1`、最外圈为 `-1`；4-连通时方框四角为 `-1`、其余边框与内部为 `1`。完整 expected 矩阵见 [tests/test_measurements.py:1486-1493](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/tests/test_measurements.py#L1486-L1493)（8-连通）与 [tests/test_measurements.py:1514-1521](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/tests/test_measurements.py#L1514-L1521)（4-连通）。

**对一张「含两个峰值的梯度图」做分割**（呼应学习目标）：把 `data` 换成一张有两个亮斑的距离/梯度图、各放一个正 marker，即可看到 IFT 沿低代价区把两个亮斑各自的吸引区划分开来。**完整梯度图实验待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么把一个 uint8 图转成 float64 后传给 `watershed_ift` 会报错？

**答案**：桶队列的大小 = `maxval+1`，要求 `maxval` 是有界小整数。float 图没有有限上界、也无法做桶下标，故函数在 [_measurements.py:1653-1654](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py#L1653-L1654) 直接 `raise TypeError`。若需要在浮点梯度图上分割，需先归一化并量化到 uint8/uint16。

**练习 2**：如果把对象 marker 改成负数、背景 marker 改成正数，结果会怎样？

**答案**：标签值的符号会随 marker 改变（输出即 marker 的符号），且在**同代价平局**时，正 marker（现在原是背景）会先占地。一般约定正=对象、负=背景，颠倒会让原本应是背景的区域在平局处被对象抢占，分割边界可能微移。

**练习 3**：IFT 的路径代价取「路径上最大弧权」，若改成「路径上弧权之和」会发生什么？

**答案**：那将不再是标准分水岭，而更接近最短路径（geodesic）分割——长而平缓的路径会被惩罚，短而陡的路径反而可能更优。分水岭的关键正是用 `max`（而非 `sum`）作代价，使「最高一级台阶」成为唯一屏障，从而让山脊自然成分界。

---

## 5. 综合实践

把本讲三个模块串起来，完成一次小型的「区域测量 + 分割」流程。任务：对一张含两个亮斑的图，先用 `label` 找连通块、用 `extrema` + `center_of_mass` 测量每个亮斑，再以亮斑峰值点为 marker、用 `watershed_ift` 把整张图分成两个吸引区。

**操作步骤**（示例代码骨架）：

```python
# 示例代码
import numpy as np
from scipy import ndimage

# 1) 造图：两个亮斑（峰值在 (2,2)=200 和 (2,7)=200），背景=10
img = np.full((6,10), 10, dtype=np.uint16)
img[2,2] = 200; img[1,2]=img[3,2]=img[2,1]=img[2,3]=120   # 左亮斑
img[2,7] = 200; img[1,7]=img[3,7]=img[2,6]=img[2,8]=120   # 右亮斑

# 2) 测量：label 找块（用阈值二值化）、extrema 取峰值位置、center_of_mass 取质心
mask = img > 50
lbl, n = ndimage.label(mask)
print("num blobs:", n)
mins, maxs, min_pos, max_pos = ndimage.extrema(img, lbl, np.arange(1, n+1))
print("peak positions (max_pos):", max_pos)             # 应在 (2,2) 与 (2,7)
print("center of mass:", ndimage.center_of_mass(img, lbl, np.arange(1, n+1)))

# 3) 分割：以两个峰值为对象 marker，左上角为背景 marker
markers = np.zeros(img.shape, dtype=np.int32)
markers[max_pos[0]] = 1     # 左峰
markers[max_pos[1]] = 2     # 右峰
markers[0,0] = -1           # 背景
seg = ndimage.watershed_ift(img, markers)
print("segmentation unique labels:", np.unique(seg))
```

**需要观察的现象**：
- `max_pos` 给出两个亮斑的精确峰值坐标，与 `center_of_mass` 的加权质心接近（若亮斑对称则几乎重合）。
- 分割结果 `seg` 在两个亮斑之间出现一条分界：分界线两侧像素分别归属 marker 1 与 marker 2，因为跨越该线的灰度差（代价）最大。
- 整条流程只用了本讲 + 前几讲的函数，没有任何额外依赖。

**预期结果**：两个亮斑各成一个连通块，`extrema` 给出峰值与质心，`watershed_ift` 在两峰之间画出分水岭边界。具体边界位置取决于亮斑间距与背景灰度，**待本地验证**。

## 6. 本讲小结

- **`labeled_comprehension`** 是测量子包的「通用聚合逃生舱」：用「排序 + `searchsorted` 二分夹段」把 `[func(input[labels==i]) for i in index]` 从 O(N·K) 降到 O(N log N)，并通过 `out_dtype` / `default` / `pass_positions` 三个参数控制结果类型、空区域回退、是否传位置。
- **`extrema`** 是 `_select`（u4-l3）的「全开关」用法：一次 `argsort` 配合正/反向 fancy indexing，一趟同时拿到每区域的 min/max 值与扁平位置，再用 `dim_prod` 整除取模把扁平下标还原成 N-D 坐标。
- **`center_of_mass`** 没有新内核：质心 \(c_d = \sum x_d v / \sum v\) 的分子分母都委托给 `sum_labels`，巧妙地把「加权坐标平均」化归为两次加权和。
- **`histogram`** 是 `labeled_comprehension` 的最典型客户：把 `np.histogram` 包成闭包即可逐区域统计值分布（`out_dtype=object` 容纳每区域返回的数组）。
- **`watershed_ift`** 是本讲唯一有 C 内核的函数：基于 IFT 算法，以「路径最大弧权 \(|I(p)-I(v)|\)」为代价，用**桶优先队列**（代价 0..maxval 各一桶）从 marker 出发松弛传播，把每个像素划归最优 marker；正/负 marker 区分对象/背景并在平局时让对象优先。input 限于 uint8/uint16 正是桶队列的设计约束。
- 这四个函数 + u4-l1/l2/l3 共同构成了 `scipy.ndimage` 的**测量与连通区域**完整能力：标记 → 定位 → 统计 → 通用聚合 → 极值/质心/直方图 → 分水岭分割。

## 7. 下一步学习建议

- **进入形态学单元（u5）**：本讲的 `watershed_ift` 默认结构元来自 `generate_binary_structure`，这正是 u5-l1 的主题；分水岭与距离变换（u5-l4）在经典分割流水线里常配合使用（先距离变换、再分水岭分离粘连物体），建议学完 u5-l4 后回头设计「距离变换 + watershed_ift」综合实验。
- **下探 C 内核（u6）**：`watershed_ift` 的 IFT 实现位于 [src/ni_measure.c:211](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/src/ni_measure.c#L211) 起的 `NI_WatershedIFT`，是阅读 ndimage C 内核的优秀起点——它同时涉及迭代器（`NI_Iterator`）、邻域偏移计算与优先队列，可结合 u6-l2（C 迭代器与行缓冲）一起读。
- **回顾 u4-l3**：若对 `_select` 的「正/反向 fancy indexing 取 min/max」仍不熟练，建议重读 u4-l3 再回到本讲的 `extrema`，二者是同一内核的不同用法。
- **测试阅读**：[tests/test_measurements.py](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/tests/test_measurements.py) 中 `test_watershed_ift01`~`09`、`test_extrema01`~`04`、`test_center_of_mass01`~`09`、`test_histogram01`~`03` 覆盖了本讲全部函数的边界行为，是最好的「可执行文档」。

---

*参考*：
[1] A.X. Falcao, J. Stolfi and R. de Alencar Lotufo, "The image foresting transform: theory, algorithms, and applications", Pattern Analysis and Machine Intelligence, vol. 26, pp. 19-29, 2004.（亦见 `_measurements.py:watershed_ift` docstring 的 References。）
