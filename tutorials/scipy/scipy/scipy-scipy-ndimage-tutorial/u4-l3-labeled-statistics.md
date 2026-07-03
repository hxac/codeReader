# 标签统计 sum/mean/var/min/max 与共享内核

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 `scipy.ndimage` 里 `sum` / `sum_labels` / `mean` / `variance` / `standard_deviation` / `minimum` / `maximum` / `median` 这一组「按区域统计」函数的**共同契约**：`input + labels + index` 三个参数各扮演什么角色。
- 读懂这八个公开函数背后**只有两个**私有内核——`_stats`（算 count/sum/方差）与 `_select`（算 min/max/位置/中位数）——并能解释它们各自用什么 NumPy 技巧把「按标签分组统计」压成常数次数组遍历。
- 区分 `index` 为标量、序列、`None` 时返回值形态的不同，并能预测「缺失标签」「`index` 乱序」时的输出。
- 知道一个容易被忽略的事实：这一组统计函数**完全用纯 Python + NumPy 向量化**实现，不像滤波/插值那样下沉到 C 扩展。

## 2. 前置知识

本讲默认你已经掌握 u4-l1（`label` 与连通区域）和 u4-l2（`find_objects` / `value_indices`）。在此基础上补充三个 NumPy 概念，它们是两个内核的「地基」：

- **`np.bincount`**：给定一维非负整数标签数组 `lbl` 和（可选）等长权重数组 `w`，`np.bincount(lbl, weights=w)` 返回长度为 `lbl.max()+1` 的数组，其第 `k` 个元素是「所有 `lbl==k` 的位置上 `w` 之和」（不给 `weights` 时退化为计数）。这正是「按标签分组求和/计数」的最快原语，一次扫描 \(O(n)\) 完成。
- ** Fancy indexing 的「后写胜」（last-write-wins）**：当用 `arr[idx] = val` 而 `idx` 含重复下标时，NumPy 按从左到右顺序逐次赋值，相同下标上**最后一次赋值生效**。`_select` 正是利用这一点，配合「先排序、再正/反向赋值」，在一次遍历里取出每个标签的最小值、最大值。
- **广播（broadcast）**：`input` 与 `labels` 形状不必严格相同，只要能 `np.broadcast_arrays` 广播一致即可（典型情况就是同形）。

一个贯穿全讲的数学约定：对一个标签内的样本 \(x_1,\dots,x_n\)，记其均值 \(\bar{x}=\frac{1}{n}\sum x_i\)，则

\[
\text{variance}=\frac{1}{n}\sum_{i}(x_i-\bar{x})^{2}
\]

`_stats` 里的 `sums_c`（centered sum of squares）存的就是分母之前的分子 \(\sum_i(x_i-\bar{x})^{2}\)，方差只需再除以计数。

## 3. 本讲源码地图

本讲全部代码集中在一个文件里：

| 文件 | 作用 |
|------|------|
| [`_measurements.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py) | measurements 功能域的全部实现：`label`、`find_objects`、统计函数、`watershed_ift` 等。本讲只关注其中的统计函数与两个私有内核。 |

本讲涉及的函数与其在文件中的位置（行号供永久链接使用）：

- 私有内核 `_stats`（求和类）：`_measurements.py` 第 591–698 行
- 私有内核 `_select`（顺序统计量）：`_measurements.py` 第 918–1034 行
- 公开函数 `sum` / `sum_labels` / `mean` / `variance` / `standard_deviation`：第 701–915 行
- 公开函数 `minimum` / `maximum` / `median`：第 1037–1239 行

注意：这一组统计函数**不调用** `_nd_image`、`_ni_label` 等 C/Cython 扩展，仅依赖 NumPy。`_measurements.py` 顶部的 `import` 也只在统计部分用到 `numpy`。

---

## 4. 核心概念与源码讲解

### 4.1 标签统计的统一契约：labels + index

#### 4.1.1 概念说明

做图像分析时，我们经常面对这样的需求：「这张图里有若干个物体（已由 `label` 标好号），请分别告诉我每个物体的总亮度、平均亮度、亮度方差、最亮像素……」。

如果只用 NumPy，最直观的写法是 `[input[labels == i].sum() for i in index]`——但每个 `i` 都要扫一遍整张图，标签多时是 \(O(\text{num\_labels}\times n)\)。`scipy.ndimage` 把这类需求抽象成统一的**三参数契约**：

- `input`：被统计的数据数组（任意维度，可整数可浮点，甚至复数）。
- `labels`：与 `input` 同形（或可广播）的整数数组，规定每个像素属于哪个区域；约定 `0` 表示背景、不参与统计。
- `index`：要统计「哪些标签」。可以是单个整数（返回标量）、整数序列（返回与 `index` 等长的数组），或 `None`（把所有 `labels>0` 的像素当作一个大区域，返回标量）。

`sum` / `mean` / `variance` / `minimum` / `maximum` / `median` 等八个函数**签名完全一致**，差别只在「把区域里的值压成一个什么数」。这套契约由两个私有内核统一实现。

#### 4.1.2 核心流程

任何一个统计函数的执行都可以概括为三步：

```
1. 解析三参数 → 决定「单组 / 标量 index / 序列 index」三种返回形态
2. 把「按标签分组求统计量」压成常数次 NumPy 全数组运算
   - 求和类 → _stats（基于 np.bincount）
   - 极值/中位 → _select（基于 np.argsort + fancy indexing）
3. 按 index 的顺序 gather 出结果；缺失标签补 0（或 0.0）
```

#### 4.1.3 源码精读

每个公开函数都是「调用内核 + 一行后处理」的薄壳。例如 [`sum_labels`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py#L715-L757) 的函数体只有两行（第 756–757 行）：

```python
count, sum = _stats(input, labels, index)
return sum
```

而 [`mean`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py#L760-L810)（第 809–810 行）则是「拿到 count 和 sum 再相除」：

```python
count, sum = _stats(input, labels, index)
return sum / np.asanyarray(count).astype(np.float64)
```

注意 `sum`（[第 701–712 行](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py#L701-L712)）只是 `sum_labels` 的别名，文档里明确建议新代码用 `sum_labels`（`sum` 会遮蔽内置 `sum`，仅为向后兼容保留）。

> 这一段要建立的直觉是：**八个公开函数只是同一对内核的不同「后处理」**。看懂了 `_stats` 和 `_select`，八个函数就都懂了。

#### 4.1.4 代码实践

1. **目标**：体会三参数契约下「标量 `index` 返回标量、序列 `index` 返回数组、`index=None` 返回标量」三种形态。
2. **步骤**：运行下面的脚本（依赖已安装的 scipy）。

   ```python
   import numpy as np
   from scipy import ndimage

   a = np.array([[1, 2, 0, 0],
                 [5, 3, 0, 4],
                 [0, 0, 0, 7],
                 [9, 3, 0, 0]])
   lbl, nb = ndimage.label(a)          # nb 个连通区域

   print(ndimage.sum(a, lbl, 1))                 # 标量 index → 标量
   print(ndimage.sum(a, lbl, index=[1, 2, 3]))   # 序列 index → 数组
   print(ndimage.sum(a, lbl))                     # index=None → 所有 labels>0 一组
   ```
3. **观察**：第一条打印一个数；第二条打印长度为 3 的数组，顺序与 `index` 完全对应；第三条把全部前景像素求和。
4. **预期结果**：`10`（区域 1：1+2+5+3−... 实际为 1+2+5+3=11？请以本地输出为准，亲手核对每个区域的像素，体会 `labels==0` 被自动排除）；第二条为三元素数组；第三条为单个总数。**若手算与输出不一致，先检查你对 `label` 默认连通性的理解**（参考 u4-l1）。
5. 上面 `10`/`11` 这类具体数字标注为「待本地验证」——以你机器上的真实输出为准。

#### 4.1.5 小练习与答案

**练习 1**：若 `labels` 为 `None`，`index` 还能给值吗？

**答案**：不能。`labels=None` 表示「没有分组概念，把整个 `input` 当作单一数据集」。此时 `index` 必须为 `None`，否则 `_stats` / `_select` 会按「单一组」走 `single_group` 分支并忽略你的 `index`（实现上 `labels is None` 是最先判断的早退条件，见 [_stats 第 630–631 行](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py#L630-L631)）。

**练习 2**：为什么统计函数能接受 `labels` 与 `input` 形状「不完全相同但可广播」的情况？

**答案**：因为内核开头都做了 `input, labels = np.broadcast_arrays(input, labels)`（如 [_stats 第 635 行](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py#L635)），把两者广播成同形后再处理。

---

### 4.2 _stats 内核与求和类统计（sum / mean / variance / standard_deviation）

#### 4.2.1 概念说明

`_stats` 是「求和类」统计的总引擎。它能一次算出每个标签的：

- `count`：像素数（`np.bincount(labels)`）；
- `sum`：像素值之和（`np.bincount(labels, weights=input)`）；
- 可选的 `sums_c`：**均值中心化后的平方和** \(\sum_i (x_i-\bar{x})^2\)（仅当 `centered=True`）。

关键是：`mean` / `variance` / `standard_deviation` 不必各自重新遍历数组，它们都复用 `_stats`：

\[
\text{mean}=\frac{\text{sum}}{\text{count}},\qquad
\text{variance}=\frac{\text{sums\_c}}{\text{count}},\qquad
\text{std}=\sqrt{\text{variance}}
\]

`centered` 这个布尔参数就是 `mean` 与 `variance` 复用同一内核的唯一开关——`mean` 传 `centered=False`（默认），`variance` 传 `centered=True`。

#### 4.2.2 核心流程

`_stats` 内部按 `labels` / `index` 的形态分三条路径，但核心都是 `bincount`：

```
若 labels is None            → single_group(整个 input)            [早退]
若 index is None             → single_group(input[labels>0])        [早退]
若 index 是标量              → single_group(input[labels==index])   [早退]
否则（index 是序列）         → 走 bincount 批量路径：
  ① 若 labels 不能安全当 int 下标（含负数/非整数/最大值过大）
        → np.unique(..., return_inverse=True) 把标签重映射成紧凑整数
  ② counts = bincount(labels)
     sums   = bincount(labels, weights=input)
     若 centered: sums_c = _sum_centered(labels)
  ③ 用 index 做 gather；缺失标签置 found=False → 结果填 0
```

`_sum_centered` 的巧妙之处：先用 `sums/counts` 得到每个标签的均值 `means`，再用 `means[labels]` 把「逐像素的均值」广播回去（每个像素减去**自己所属标签**的均值），最后再 `bincount` 一次求平方和。整个过程只有常数次全数组运算。

#### 4.2.3 源码精读

[`_stats`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py#L591-L698) 的签名（第 591 行）：

```python
def _stats(input, labels=None, index=None, centered=False):
```

`centered=True` 时多返回一项 `sums_c`。早退分支里的 [`single_group`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py#L622-L627) 直接对一维值数组算 size/sum/中心化平方和：

```python
def single_group(vals):
    if centered:
        vals_c = vals - vals.mean()
        return vals.size, vals.sum(), (vals_c * vals_c.conjugate()).sum()
    else:
        return vals.size, vals.sum()
```

> 注意 `.conjugate()`：这让 `_stats` 对**复数 input** 也成立（`|vals_c|²`）。这也是为什么 docstring 不禁止复数——求和类统计天然支持复数。

批量路径的核心是 [`_sum_centered`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py#L643-L653)，三行完成「广播均值 → 中心化 → 平方和」：

```python
means = sums / counts
centered_input = input - means[labels]               # 每像素减本标签均值
bc = np.bincount(labels.ravel(),
                 weights=(centered_input *
                          centered_input.conjugate()).ravel())
```

为了用 `bincount`（它要求下标是「紧凑、非负、不太大」的整数），`_stats` 在 [第 658–686 行](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py#L658-L686) 做了一个分支判断：

```python
if (not _safely_castable_to_int(labels.dtype) or
        labels.min() < 0 or labels.max() > labels.size):
    unique_labels, new_labels = np.unique(labels, return_inverse=True)
    ...
else:
    counts = np.bincount(labels.ravel())
    sums   = np.bincount(labels.ravel(), weights=input.ravel())
```

即：只有当标签已经是「合法、紧凑」的整数时才直接 `bincount`；否则先用 `np.unique` 重映射成 `0..k-1` 再算。两种情况最后都落到 `counts/sums` 上，再按 `index` 取出。

最后，[第 688–698 行](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py#L688-L698) 用 `idxs`（`index` 转成的下标）做 gather，并把 `~found`（缺失标签）位置清零。

公开函数对 `sums_c` 的使用：[`variance`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py#L813-L863)（第 862–863 行）

```python
count, sum, sum_c_sq = _stats(input, labels, index, centered=True)
return sum_c_sq / np.asanyarray(count).astype(float)
```

而 [`standard_deviation`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py#L866-L915)（第 915 行）只是 `np.sqrt(variance(...))`。

#### 4.2.4 代码实践

1. **目标**：用 `mean` 与 `standard_deviation` 计算各区域统计；用 `index=[1,3]` 验证「返回顺序与 index 一致」。
2. **步骤**：

   ```python
   import numpy as np
   from scipy import ndimage

   a = np.array([[1, 2, 0, 0],
                 [5, 3, 0, 4],
                 [0, 0, 0, 7],
                 [9, 3, 0, 0]])
   lbl, nb = ndimage.label(a)
   idx = np.arange(1, nb + 1)

   print("mean   :", ndimage.mean(a, lbl, index=idx))
   print("std    :", ndimage.standard_deviation(a, lbl, index=idx))
   print("var    :", ndimage.variance(a, lbl, index=idx))
   print("np.sqrt(var):", np.sqrt(ndimage.variance(a, lbl, index=idx)))

   # 验证顺序：把 index 反过来，结果也应跟着反过来
   print("mean[1,3]:", ndimage.mean(a, lbl, index=[1, 3]))
   print("mean[3,1]:", ndimage.mean(a, lbl, index=[3, 1]))
   ```
3. **观察**：`std` 与 `np.sqrt(var)` 逐元素相等（验证 `standard_deviation` 就是 `sqrt(variance)`）；`mean[1,3]` 与 `mean[3,1]` 是彼此的反序——说明输出严格跟随 `index` 顺序。
4. **预期结果**：手算每个区域的均值与方差（例如区域 1 的像素请先用 `lbl==1` 找出），与打印结果逐一对照；二者一致即说明你对 `_stats` 的「按 `index` gather」理解正确。
5. 具体数值标注为「待本地验证」，以你机器输出为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `mean` 能支持复数 `input`，而后续会看到 `label` 不支持复数？

**答案**：`label` 处理的是「前景/背景」的连通性，依赖 `input != 0` 判定，复数无天然序、且其 docstring 明确 `raise TypeError('Complex type not supported')`（见 [label 第 176–177 行](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py#L176-L177)）。而 `_stats` 只做求和/求均值，对复数有意义，并通过 `.conjugate()` 让中心化平方和成为模长平方。

**练习 2**：若某个 `index` 值在 `labels` 里不存在（缺失标签），`mean` 会返回什么？

**答案**：该位置的 `count=0`、`sum=0`（[第 688–691 行](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py#L688-L691)把 `~found` 清零），于是 `mean = 0/0` 得到 `nan`（并触发 RuntimeWarning）。所以「缺失标签 → 均值为 `nan`」是这一契约的固有行为，使用时需留意。

---

### 4.3 _select 内核与顺序统计量（minimum / maximum / median）

#### 4.3.1 概念说明

`_select` 是「顺序统计量」引擎：一次调用可同时求出每个标签的**最小值、最大值、最小值位置、最大值位置、中位数**——用五个布尔开关（`find_min` / `find_max` / `find_min_positions` / `find_max_positions` / `find_median`）按需开启。`minimum` / `maximum` / `median` 只是分别打开其中一个开关的薄壳，而 `extrema` / `minimum_position` / `maximum_position`（下一讲 u4-l4 详讲）则一次打开多个，**用同一次排序算出全部结果**，避免反复遍历。

#### 4.3.2 核心流程

`_select` 的核心是一次全局排序 + 巧妙的 fancy indexing：

```
1. 若需要位置信息 → positions = np.arange(input.size).reshape(input.shape)
2. 早退：labels is None / index is None / index 标量 → single_group
3. （序列 index 分支）必要时用 np.unique 重映射标签 → 得到紧凑 idxs
   把「缺失标签」的 idxs 统一指向 labels.max()+1（一个被初始化为 0 的哨兵槽）
4. order = input.ravel().argsort()        # 按值升序排
   input/labels/(positions) 全部按 order 重排
5. 用「后写胜」fancy indexing 一次性取出每标签结果：
   - 最小值：反向赋值  mins[labels[::-1]] = input[::-1]   → 最小者最后落定
   - 最大值：正向赋值  maxs[labels]      = input          → 最大者最后落定
   - 位置：同理
   - 中位数：用 lo/hi 夹出每标签在排序后数组里的取值区间，向中间靠拢后取平均
```

「后写胜」的精髓：因为 `input` 已升序，反向赋值时**最小的值排在赋值序列最后**，故每个标签槽里留下的是最小值；正向赋值时**最大的值排最后**，留下的是最大值。位置同理。

#### 4.3.3 源码精读

[`_select`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py#L918-L1034) 的签名（第 918–920 行）用五个开关描述要什么：

```python
def _select(input, labels=None, index=None, find_min=False, find_max=False,
            find_min_positions=False, find_max_positions=False,
            find_median=False):
```

排序与重排在 [第 986–993 行](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py#L986-L993)：

```python
if find_median:
    order = np.lexsort((input.ravel(), labels.ravel()))   # 中位需要同标签尽量聚拢
else:
    order = input.ravel().argsort()
input  = input.ravel()[order]
labels = labels.ravel()[order]
```

最小值/位置用「反向赋值」让最小者胜出（[第 996–1003 行](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py#L996-L1003)）：

```python
mins  = np.zeros(labels.max() + 2, input.dtype)
mins[labels[::-1]]  = input[::-1]            # 反向 → 最小值最后写入
minpos = np.zeros(labels.max() + 2, int)
minpos[labels[::-1]] = positions[::-1]
```

最大值/位置用「正向赋值」（[第 1004–1011 行](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py#L1004-L1011)）：

```python
maxs = np.zeros(labels.max() + 2, input.dtype)
maxs[labels] = input                          # 正向 → 最大值最后写入
```

> `labels.max() + 2` 这个长度多出来的槽，正是用来容纳「缺失标签」的哨兵（[第 984 行](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py#L984) `idxs[~ found] = labels.max() + 1`）。该槽初始化为 0，所以缺失标签的最小/最大值都报 0。

中位数（[第 1012–1032 行](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py#L1012-L1032)）用 `lo/hi` 夹取：`lo[label]` 是该标签在排序数组里出现的最小下标、`hi[label]` 是最大下标，二者向中间移动 `step=(hi-lo)//2` 后取平均，对奇偶个数都给出「中间一个」或「中间两个的平均」。注意整数/布尔 input 会先 `.astype('d')` 再相加，避免溢出（对应 gh-12836）。

公开函数都是一行调用，例如 [`minimum`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py#L1037-L1097)（第 1097 行）：

```python
return _select(input, labels, index, find_min=True)[0]
```

[`maximum`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py#L1100-L1177)（第 1177 行）用 `find_max=True`，[`median`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py#L1180-L1239)（第 1239 行）用 `find_median=True`。注意 `minimum` / `maximum` / `median` 返回的是 **Python list**（非 ndarray），docstring 的 Notes 都专门提醒「用 `np.array` 转换」。

> 关于中位数的精确性：`_select` 的中位数是「全局排序后用 `lo/hi` 夹取」的实现，当同一标签的值在全局排序里分布较散、且与其它标签的值交错时，夹取结果可能与严格定义的中位数有出入。这是该实现（基于单次排序、避免逐标签重排）的已知取舍。**建议用小数组本地单步验证**，不要假设它在所有病态输入下都与教科书定义逐位相同。

#### 4.3.4 代码实践

1. **目标**：用 `minimum` / `maximum` / `median` 计算各区域顺序统计量；用含 NaN 之外的小数组直观对照。
2. **步骤**：

   ```python
   import numpy as np
   from scipy import ndimage

   a = np.array([[1, 2, 0, 0],
                 [5, 3, 0, 4],
                 [0, 0, 0, 7],
                 [9, 3, 0, 0]])
   lbl, nb = ndimage.label(a)
   idx = np.arange(1, nb + 1)

   print("minimum:", ndimage.minimum(a, lbl, index=idx))
   print("maximum:", ndimage.maximum(a, lbl, index=idx))
   print("median :", ndimage.median(a, lbl, index=idx))

   # 单标签手算对照
   for i in idx:
       vals = a[lbl == i]
       print(f"label {i}: values={vals}, "
             f"min={vals.min()}, max={vals.max()}, median={np.median(vals)}")
   ```
3. **观察**：`minimum` / `maximum` 的输出与逐区域 `vals.min()/vals.max()` **完全一致**（这两者是精确的）；`median` 与 `np.median` 在大多数正常分布下一致。注意三个函数返回的都是 list 而非数组。
4. **预期结果**：`minimum` / `maximum` 严格吻合；`median` 吻合或需结合上一段「精确性」说明核对。
5. 中位数在病态分布下的具体表现标注为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_select` 用 `labels.max() + 2` 而不是 `labels.max() + 1` 作为结果数组长度？

**答案**：多出的一个槽（下标 `labels.max()+1`）是「缺失标签」的哨兵位置——[第 984 行](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py#L984)把缺失标签的 `idxs` 全部指向它，而该槽被初始化为 0，于是缺失标签统一报 0。所以容量必须是 `labels.max()+2` 才能容纳 `0..labels.max()+1` 这些下标。

**练习 2**：`find_median=True` 时为何改用 `np.lexsort((input, labels))` 而非单纯 `input.argsort()`？

**答案**：`lexsort` 以 `labels` 为主键、`input` 为次键排序，使**同一标签的元素在排序后尽量连续**，这样 `lo/hi` 夹出的区间更贴近「该标签自身的有序子段」，中位数更可靠。求 min/max 时不需要这种聚拢，故用更便宜的 `argsort`。

---

### 4.4 index 形态与返回顺序、缺失标签处理

#### 4.4.1 概念说明

`index` 的形态直接决定返回形态，这是使用这一组函数时最容易踩坑的地方，值得单独成节。三条规则：

| `index` 形态 | 返回形态 | 含义 |
|--------------|----------|------|
| 标量（如 `1`） | 标量 | 只统计这一个标签 |
| 序列（如 `[1,3]`） | 与 `index` 等长的 list/array | 依次统计，**顺序严格跟随 `index`** |
| `None` | 标量 | 把所有 `labels>0` 当成一个大区域 |

并且：**返回顺序永远与 `index` 的顺序一致**——把 `index` 反过来，结果也反过来。这一点在 `_stats` 里由最后的 `gather`（`counts = counts[idxs]`）保证，在 `_select` 里由 `mins[idxs]` 等保证。

「缺失标签」的统一行为：`index` 里出现 `labels` 中不存在的标签时，求和类把 count/sum 置 0（→ mean 为 `nan`、sum 为 0），顺序统计量置 0。

#### 4.4.2 核心流程

两个内核都用一个 `found` 布尔掩码标记「`index` 里的值是否真的存在于 `labels`」：

```
直接分支：found = (idxs >= 0) & (idxs < counts.size)   # _stats
重映射分支：found = (unique_labels[idxs] == index)       # 二者皆用
缺失 → idxs[~found] 指向哨兵；结果在 ~found 处填 0
```

#### 4.4.3 源码精读

`_stats` 的 `found` 计算与清零在 [第 683–691 行](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py#L683-L691)：

```python
idxs = np.asanyarray(index, np.int_).copy()
found = (idxs >= 0) & (idxs < counts.size)
idxs[~found] = 0
...
counts = counts[idxs]; counts[~found] = 0
sums   = sums[idxs];   sums[~found]   = 0
```

`_select` 对应在 [第 979–984 行](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py#L979-L984)，把缺失标签指向哨兵槽（值为 0）。两处都体现了「先把 `index` 转成合法下标 `idxs`，再 gather」的统一模式，因此输出顺序天然等于 `index` 顺序。

#### 4.4.4 代码实践

1. **目标**：亲手验证「返回顺序跟随 `index`」与「缺失标签报 0 / nan」。
2. **步骤**：

   ```python
   import numpy as np
   from scipy import ndimage

   a = np.array([[1, 2, 0, 0],
                 [5, 3, 0, 4],
                 [0, 0, 0, 7],
                 [9, 3, 0, 0]])
   lbl, nb = ndimage.label(a)
   print("nb =", nb)

   # (a) 顺序跟随 index
   print("sum [1,2,3] :", ndimage.sum(a, lbl, index=[1, 2, 3]))
   print("sum [3,2,1] :", ndimage.sum(a, lbl, index=[3, 2, 1]))
   print("sum [1,3]   :", ndimage.sum(a, lbl, index=[1, 3]))

   # (b) 缺失标签：标签号 99 不存在
   print("sum [1,99]  :", ndimage.sum(a, lbl, index=[1, 99]))
   print("mean[1,99]  :", ndimage.mean(a, lbl, index=[1, 99]))
   print("max [1,99]  :", ndimage.maximum(a, lbl, index=[1, 99]))
   ```
3. **观察**：
   - (a) 三行的结果彼此是不同排列，但都包含相同的几个数，顺序与传入 `index` 一一对应。
   - (b) 缺失标签 `99`：`sum` 该位为 `0.0`、`mean` 该位为 `nan`（0/0）、`maximum` 该位为 `0`。
4. **预期结果**：与上述对照；若 `mean` 出现 `nan` 是符合设计的，不是 bug。
5. 具体数值「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`index=[1, 1, 2]`（含重复标签）会发生什么？

**答案**：内核不做去重。`gather` 会把标签 1 的结果取两次，返回长度为 3、且前两个元素相同的数组。这是允许的用法，但通常说明调用方写法可以简化。

**练习 2**：为什么 `mean` 对缺失标签返回 `nan`，而 `maximum` 对缺失标签返回 `0`？

**答案**：`mean = sum/count`，缺失时 sum=0、count=0，相除得 `nan`；`maximum` 走 `_select`，缺失标签被指向「初始化为 0 的哨兵槽」（[第 984 行](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/ndimage/_measurements.py#L984)），故报 `0`。两条路径对「缺失」的语义不同，使用时要留意 `maximum` 的 `0` 可能与真实最大值混淆。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成一次「标记 → 定位 → 分组统计」的完整分析。

**任务**：构造一张含 3 个明显物体的小图，分别测量每个物体的面积、平均亮度、亮度标准差、最亮像素值，并按面积从大到小排序输出。

```python
import numpy as np
from scipy import ndimage

# 1) 造图：3 个互不连通的团块，亮度不同
img = np.zeros((10, 10), dtype=float)
img[1:3, 1:3] = 5.0      # 物体 A：4 像素，亮
img[1:2, 6:9] = 1.0      # 物体 B：3 像素，暗
img[6:9, 4:7] = 3.0      # 物体 C：9 像素，中等

# 2) 标记
lbl, nb = ndimage.label(img)
print("发现物体数:", nb)

# 3) 分组统计（注意 index 顺序决定输出顺序）
idx = np.arange(1, nb + 1)
area  = ndimage.sum(np.ones_like(img), lbl, index=idx)          # 用全 1 图求面积=像素数
meanv = ndimage.mean(img, lbl, index=idx)
stdv  = ndimage.standard_deviation(img, lbl, index=idx)
maxv  = ndimage.maximum(img, lbl, index=idx)

# 4) 按面积排序
order = np.argsort(area)[::-1]
for i in order:
    print(f"物体 {idx[i]}: 面积={area[i]:.0f} 均值={meanv[i]:.2f} "
          f"标准差={stdv[i]:.3f} 最亮={maxv[i]:.1f}")
```

**验收要点**：

- `area` 应为 `[4, 3, 9]`（按 `idx` 顺序），即物体 C 面积最大。
- 排序后第一行应是物体 C（面积 9）。
- `meanv` 对每个物体应等于其赋值亮度（5、1、3），标准差应为 0（每块内部亮度均匀）——这能反向验证「`_stats` 的分组确实只在自己区域内求」。
- 尝试把 `img[6,5]` 单独改成 `8.0`，重跑，观察物体 C 的 `maxv` 变成 8、`stdv` 变为非零，体会 `_select` 对「区域内单点异常」的敏感。

> 若你的 scipy 未在本地构建安装，可改为纯阅读型实践：在 `_measurements.py` 中定位 `_stats` 的 `bincount` 调用与 `_select` 的 `argsort`+反向赋值，画出上述脚本里 `mean` / `maximum` 各自走的代码路径（标注行号），即算完成。

## 6. 本讲小结

- 八个统计函数（`sum` / `sum_labels` / `mean` / `variance` / `standard_deviation` / `minimum` / `maximum` / `median`）共享统一契约 `input + labels + index`，背后**只有两个私有内核** `_stats` 与 `_select`。
- `_stats` 基于 `np.bincount` 在常数次全数组遍历内算出 count/sum/中心化平方和；`mean=sum/count`、`variance=sums_c/count`、`std=sqrt(variance)`，复用同一引擎。
- `_select` 基于「一次 `argsort` + 后写胜 fancy indexing」一次性取出每标签的 min/max/位置；中位数用 `lo/hi` 夹取（精确性在病态分布下建议本地验证）。
- 这一组函数是**纯 NumPy 向量化的 Python 实现**，不调用任何 C 扩展，与滤波单元的架构不同。
- `index` 决定返回形态与顺序：标量→标量、序列→等长数组且顺序严格跟随 `index`、`None`→全体前景为单组。
- 缺失标签：求和类置 0（`mean` 因 0/0 得 `nan`）、顺序统计量置 0（指向哨兵槽）；使用 `maximum` 时要警惕 `0` 与真实极值混淆。

## 7. 下一步学习建议

- 下一讲 **u4-l4（通用聚合、极值与分水岭）** 会讲 `labeled_comprehension`（任意聚合，是本讲固定统计的「通用版」）、`extrema` / `center_of_mass` / `histogram`（其中 `extrema` 正是 `_select` 一次打开四个开关的典型用例）、以及 `watershed_ift`。建议先把本讲的 `_select` 五开关模型记住，再去读 `extrema` 会非常顺。
- 若想横向对比「按标签分组」的不同实现，可去读 `_measurements.py` 里 `value_indices`（u4-l2 已讲，基于 C 的 `value_indices`）与 `labeled_comprehension`（纯 Python、排序 + `searchsorted`），体会它们与 `_stats`/`_select`（bincount/argsort）各自适合的规模与场景。
- 进阶读者可下探到 C 层：`_select` 的排序思路与 u6 单元里 `_rank_filter_1d.cpp` 的滑动窗口秩滤波、`_ni_label.pyx` 的逐行扫描都是「为某个聚合目标定制的数据结构」，对照阅读能加深对「同一问题不同性能取舍」的理解。
