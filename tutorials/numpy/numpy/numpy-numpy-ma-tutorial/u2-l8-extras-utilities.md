# extras 实用工具函数集

## 1. 本讲目标

`numpy.ma` 的 `core.py` 提供了「掩码版 ufunc」与 `MaskedArray` 类本身，但很多日常使用的便利函数（沿轴应用任意函数、集合运算、行列压缩、连续区段定位、拼接……）其实都住在 `extras.py` 里。`extras` = 「add-ons（附加工具）」，它**依赖** `core` 而不被 `core` 依赖，是一层建立在三件套（data/mask/fill_value）之上的「上层工具带」。学完本讲你应当能够：

- 会用 `apply_along_axis` 沿指定轴对掩码数组的每一条 1-D 切片应用任意（掩码感知的）函数，并解释它「先用第一条切片探路、再用 object 数组中转、最后统一类型」的实现思路。
- 会用 `apply_over_axes` 把同一个函数依次作用到多个轴上。
- 掌握掩码集合运算家族（`unique` / `union1d` / `intersect1d` / `setdiff1d` / `setxor1d` / `in1d` / `isin`），并能说清楚「屏蔽值被视为同一个元素」这一共同约定是如何由 `ma.concatenate` + `unique` 共同支撑的。
- 区分「压缩」与「屏蔽」两个相反方向：`compress_rowcols` 把含屏蔽值的整行/整列**删掉**返回普通 ndarray，`mask_rowcols` 则把它们**整行/整列染成屏蔽**返回 MaskedArray。
- 会用 `clump_masked` / `clump_unmasked` 把一维数组里连续的屏蔽/非屏蔽区段切成一组 `slice`，并用 `ndenumerate` 在遍历时自动跳过屏蔽元素。
- 看懂 `_fromnxfunction_*` 这套「把普通 NumPy 函数批量包装成掩码函数」的装饰器机制，以及掩码版拼接器 `mr_`。

## 2. 前置知识

本讲承接以下已建立的认知，不再重复：

- **三件套模型**（[u1-l4](u1-l4-data-mask-fill-value.md)）：每个 `MaskedArray` 由 `.data`（含坏值的普通 ndarray 视图）、`.mask`（同形状布尔数组，无屏蔽时压缩为单例 `nomask`）、`.fill_value` 组成。
- **`nomask` 身份判断**（[u2-l1](u2-l1-mask-internal-representation.md)）：全库用 `m is nomask` 做 O(1) 的「是否无屏蔽」判断，避免给无屏蔽数组分配全 False 布尔数组。
- **`getmask` vs `getmaskarray`**（[u2-l1](u2-l1-mask-internal-representation.md)）：`getmask(a)` 忠实返回内部 `_mask`（可能是 `nomask`），`getmaskarray(a)` 永远展开成同形状全 False 布尔数组，适合后续做布尔索引。
- **索引与赋值语义**（[u2-l6](u2-l6-indexing-assignment.md)）：所有索引/赋值都在「对 `_data` 与 `_mask` 各做一次同样操作」，难点全在 mask 的边界情形。
- **归约会跳过屏蔽元素**（[u2-l7](u2-l7-reductions-stats-sort.md)）：`ma.sum` / `ma.mean` 等返回的是「屏蔽感知」的结果。

本讲会反复用到三个新背景概念：

- **`ma.concatenate`**：`core.py` 提供的掩码版拼接，它对 data 与 mask **分别**做 `np.concatenate`，因此拼接结果的屏蔽位会被完整保留（详见 [core.py:L7356-L7358](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L7356-L7358)）。它是几乎所有集合运算的地基。
- **`slice` 对象**：Python 的切片对象，如 `slice(3, 6)` 等价于 `[3:6]`。本讲里 `clump_*` 返回的就是一串 `slice`，可以直接拿去索引原数组：`a[slice(3,6)]`。
- **object dtype 数组**：元素类型为 Python 对象的 ndarray，可以装「任意类型、任意形状」的值。`apply_along_axis` 用它当中转站，最后再统一 cast 成数值 dtype。

## 3. 本讲源码地图

本讲源码集中在 `numpy/ma/extras.py`（少量引用 `core.py`）。按「功能归类」排列如下：

| 代码点 | 作用 |
| --- | --- |
| `__all__`（extras.py:10-20） | extras 对外公开的 API 清单（经 `from .extras import *` 进入 `np.ma.` 命名空间） |
| `_fromnxfunction_function` 装饰器（extras.py:249-276） | 把任意 NumPy 函数包装成「对 data 与 mask 各跑一次」的掩码函数 |
| `_fromnxfunction_single/seq/allargs`（extras.py:279-320） | 三种参数形态的包装模板；`atleast_1d` / `vstack` / `hstack` 等都由它批量生成（extras.py:323-334） |
| `flatten_inplace`（extras.py:340-347） | `apply_along_axis` 用的就地展平辅助 |
| `apply_along_axis`（extras.py:350-458） | 沿轴对每条 1-D 切片应用任意掩码函数 |
| `apply_over_axes`（extras.py:461-483） | 把同一函数依次作用到多个轴 |
| `compress_nd`（extras.py:868-923） | N 维通用版「删掉含屏蔽值的切片」 |
| `compress_rowcols` / `compress_rows` / `compress_cols`（extras.py:926-1060） | 二维版行列压缩，返回普通 ndarray |
| `mask_rowcols` / `mask_rows` / `mask_cols`（extras.py:1062-1254） | 与 compress 相反：把含屏蔽值的行列整体染成屏蔽 |
| `unique`（extras.py:1294-1341） | 掩码版去重，屏蔽值视为同一元素 |
| `intersect1d`（extras.py:1344-1374） | 交集 |
| `setxor1d`（extras.py:1377-1411） | 对称差 |
| `in1d`（extras.py:1414-1458） | 逐元素判断「是否在另一数组中」（掩码版，返回一维） |
| `isin`（extras.py:1461-1487） | `in1d` 的保形版本 |
| `union1d`（extras.py:1490-1511） | 并集 |
| `setdiff1d`（extras.py:1514-1540） | 差集 |
| `MAxisConcatenator` / `mr_class` / `mr_`（extras.py:1763-1820） | 掩码版 `r_`，按切片语法拼接掩码数组 |
| `ndenumerate`（extras.py:1827-1893） | 多维枚举，自动跳过（或用 `masked` 占位）屏蔽元素 |
| `flatnotmasked_contiguous`（extras.py:2004-2056） | 用 `itertools.groupby` 找连续非屏蔽区段（旧接口） |
| `_ezclump`（extras.py:2137-2163） | 用异或找跳变点，把一维布尔数组切成同值区段 |
| `clump_unmasked` / `clump_masked`（extras.py:2166-2235） | 返回连续非屏蔽 / 屏蔽区段的 `slice` 列表 |

> 提示：本讲引用的行号基于当前 HEAD `b21650c4f6`。所有永久链接都指向该 commit。

## 4. 核心概念与源码讲解

### 4.1 apply_along_axis / apply_over_axes：沿轴应用任意函数

#### 4.1.1 概念说明

[u2-l7](u2-l7-reductions-stats-sort.md) 里的 `sum` / `mean` / `max` 都是**固定**的归约：轴选定、算法固定。但有时你想沿某条轴应用一个**自定义**函数——比如「把每一行当作时间序列，算它的自定义打分」。`numpy` 提供了 `np.apply_along_axis(func1d, axis, arr)` 来做这件事；`numpy.ma` 提供了同名的掩码版 `ma.apply_along_axis`，区别在于它返回 **MaskedArray**，并且把「屏蔽传播」交给 `func1d` 自己处理。

关键认知：**`apply_along_axis` 本身不做任何掩码运算**。它只负责「把数组沿 `axis` 切成一条条 1-D 切片、逐条喂给 `func1d`、再把结果拼回去」。屏蔽是否被正确处理，完全取决于你传进去的 `func1d` 是不是掩码感知的——传 `ma.sum` 就跳过屏蔽，传 `np.sum` 就不跳过。

`apply_over_axes` 则更简单：它把同一个 `func(a, axis)` 依次作用到 `axes` 列表里的每一个轴上，相当于 `func` 的「链式套用」。

#### 4.1.2 核心流程

`apply_along_axis` 的执行过程（[extras.py:L350-L458](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L350-L458)）：

1. **规整输入与轴**：`arr = array(arr, copy=False, subok=True)`，用 `normalize_axis_index` 把 `axis` 归一化到合法范围。
2. **构造索引骨架**：算出「除了 `axis` 之外」的其余轴下标列表 `indlist`，建立一个可变索引 `i`，其中 `i[axis] = slice(None)`（即沿目标轴全取），其余位置先填 0。
3. **探路（probe）**：用第一条切片 `arr[tuple(i.tolist())]` 调一次 `func1d`，拿到 `res`。
4. **判断结果形态**：`res` 是标量（`np.isscalar(res)` 或没有 `len()`）还是数组？这决定后续输出是「比输入少一维」还是「替换该轴长度」。
5. **逐切片填充一个 object dtype 的中转数组 `outarr`**：用一个手动进位的索引计数器（`ind[-1] += 1`，超界则向前进位）遍历所有切片，每条切片调一次 `func1d`，把结果塞进 `outarr` 对应位置；同时把每条结果的 dtype 收集进 `dtypes`。
6. **统一类型并封装**：取 `dtypes` 的最大值 `max_dtypes`，把 object 数组 cast 过去；若原数组带 `_mask`，则包成 MaskedArray 并设默认 `fill_value`，否则返回普通 ndarray。

之所以要用 object 数组中转、最后再统一 cast，源码注释讲得很直白（[extras.py:L401-L403](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L401-L403)）：「不该用第一条结果决定输出 dtype，所以先强制成 object，攒一组 dtype 再取最大的，避免被意外降级」。

`apply_over_axes`（[extras.py:L461-L483](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L461-L483)）则朴素得多：

```
for axis in axes:
    res = func(val, axis)
    if res.ndim == val.ndim:   val = res          # 维度没掉，直接替换
    else:                      val = expand_dims(res, axis)   # 掉了一维就补回来
```

它要求 `func` 把被归约的轴保留成长度 1（`keepdims` 语义），否则报 `ValueError`。

#### 4.1.3 源码精读

探路与标量/数组分支判定（[extras.py:L383-L403](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L383-L403)）——先用第一条切片跑一次 `func1d` 拿到 `res`，再用「能不能 `len()`」判断它是不是标量：

```python
arr = array(arr, copy=False, subok=True)
nd = arr.ndim
axis = normalize_axis_index(axis, nd)
...
res = func1d(arr[tuple(i.tolist())], *args, **kwargs)
asscalar = np.isscalar(res)
if not asscalar:
    try:
        len(res)
    except TypeError:
        asscalar = True
```

object 数组中转 + 手动进位索引（标量分支，[extras.py:L407-L423](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L407-L423)）——把每条切片的结果塞进 `outarr`，同时收集 dtype：

```python
outarr = zeros(outshape, object)
outarr[tuple(ind)] = res
Ntot = np.prod(outshape)
k = 1
while k < Ntot:
    ind[-1] += 1
    n = -1
    while (ind[n] >= outshape[n]) and (n > (1 - nd)):
        ind[n - 1] += 1
        ind[n] = 0
        n -= 1
    ...
    res = func1d(arr[tuple(i.tolist())], *args, **kwargs)
    outarr[tuple(ind)] = res
    dtypes.append(asarray(res).dtype)
```

最后的统一 cast 与掩码封装（[extras.py:L452-L458](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L452-L458)）——这是它「返回 MaskedArray」的唯一源头：只要原数组有 `_mask`，就用 `asarray` 包一层并补默认 fill_value：

```python
max_dtypes = np.dtype(np.asarray(dtypes).max())
if not hasattr(arr, '_mask'):
    result = np.asarray(outarr, dtype=max_dtypes)
else:
    result = asarray(outarr, dtype=max_dtypes)
    result.fill_value = ma.default_fill_value(result)
return result
```

> ⚠️ 注意：这段代码**只**决定「结果要不要包成 MaskedArray」与「fill_value」，而**不**重新计算结果的 mask。结果里的屏蔽位完全来自 `func1d` 的逐切片返回值（它们被原样塞进 object 数组，再随 cast 一起带过来）。所以传 `ma.sum` 才会跳过屏蔽、传 `np.sum` 才不会——`apply_along_axis` 只是「搬运工」。

`apply_over_axes` 的全部逻辑（[extras.py:L465-L483](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L465-L483)）——一个 for 循环逐轴调用，必要时 `expand_dims` 补维：

```python
val = asarray(a)
N = a.ndim
for axis in axes:
    if axis < 0:
        axis = N + axis
    res = func(val, axis)
    if res.ndim == val.ndim:
        val = res
    else:
        res = ma.expand_dims(res, axis)
        ...
```

#### 4.1.4 代码实践

**实践目标**：直观对比「掩码感知的 `ma.sum`」与「普通 `np.sum`」在 `apply_along_axis` 里的差别，验证屏蔽传播确实由 `func1d` 决定。

**操作步骤**（在装好 numpy 的环境里执行）：

```python
import numpy as np
import numpy.ma as ma

a = ma.array([[1, 2, 3],
              [4, 5, 6]], mask=[[0, 1, 0],
                                 [0, 0, 1]])

# 1) 用掩码感知的 ma.sum：每列跳过屏蔽值
r1 = ma.apply_along_axis(ma.sum, 0, a)
# 2) 用普通的 np.sum：屏蔽值被当成 fill_value(999999) 参与求和
r2 = ma.apply_along_axis(np.sum, 0, a)
```

**需要观察的现象**：

- `r1` 应为 `masked_array(data=[5, 5, 3], mask=False)`（这正是源码 docstring 给出的结果，[extras.py:L370-L373](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L370-L373)）：第 0 列 `1+4=5`，第 1 列只有 `5`（2 被屏蔽），第 2 列只有 `3`（6 被屏蔽）。
- `r2` 会把屏蔽位填成 `999999` 再相加，结果会出现巨大的数，且 `mask` 仍为 `False`。

**预期结果**：`r1` 正确跳过屏蔽、`r2` 不跳过。两者形状都是 `(3,)`，但数值天差地别——这说明「跳不跳过屏蔽」由 `func1d` 决定，`apply_along_axis` 不插手。

> 待本地验证：`r2` 的具体数值取决于 `np.sum` 拿到那条 1-D MaskedArray 切片时如何把它转成普通数组（通常是填 `fill_value=999999`）。你可打印 `r2` 与 `r2.mask` 确认它确实没有屏蔽位。

#### 4.1.5 小练习与答案

**练习 1**：用 `apply_along_axis(ma.mean, 1, a)` 计算上例每行的均值，预期得到什么？

**参考答案**：`masked_array(data=[2. , 4.5])`（见 docstring [extras.py:L377-L380](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L377-L380)）。第 0 行非屏蔽元素是 `1, 3`（2 被屏蔽），均值 `(1+3)/2 = 2.0`；第 1 行非屏蔽元素是 `4, 5`（6 被屏蔽），均值 `(4+5)/2 = 4.5`。注意分母用的是**真实非屏蔽计数**而非元素总数，这正是 [u2-l7](u2-l7-reductions-stats-sort.md) 讲过的 `mean` 特例。

**练习 2**：为什么 `apply_along_axis` 要先用第一条切片「探路」，而不是预先固定输出形状？

**参考答案**：因为 `func1d` 的输出形状无法事先推断——它可能返回标量（输出比输入少一维），也可能返回一个长度不同的一维数组（替换该轴长度），甚至返回多维。探路一次拿到 `res`，才能据此决定 `outarr` 的形状与走哪个分支（[extras.py:L393-L400](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L393-L400)）。

---

### 4.2 集合运算家族：union1d / intersect1d / setdiff1d / in1d / ……

#### 4.2.1 概念说明

`numpy` 有一套 1-D 集合运算（`np.unique` / `np.union1d` / `np.intersect1d` / `np.setdiff1d` / `np.in1d` ……），`numpy.ma` 给它们各自做了一个掩码版，全部住在 `extras.py`。它们的**共同契约**是：

1. **输出永远是 MaskedArray**（即使没有任何屏蔽位，也会包一层，见 docstring 反复出现的「The output is always a masked array」）。
2. **屏蔽值被视为「同一个元素」**——无论输入里有几个屏蔽位，它们在集合意义下都代表同一个「缺失值」，去重后只剩一个 `--`。
3. 它们几乎都建立在地基 `ma.concatenate`（保留屏蔽位）与 `ma.unique`（把结果 `.view(MaskedArray)`）之上。

理解了这三点，整个家族就不必逐个背：它们都是「先拼接/去重、再排序比较、最后保证输出是 MaskedArray」的排列组合。

#### 4.2.2 核心流程

各函数的「配方」可归纳成下表（「实现」一栏引用的是真实源码行）：

| 函数 | 一句话实现 | 源码 |
| --- | --- | --- |
| `unique` | 调 `np.unique`，把结果 `[0]` 用 `.view(MaskedArray)` 重新封装 | [extras.py:L1332-L1341](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L1332-L1341) |
| `union1d` | `unique(ma.concatenate((ar1, ar2), axis=None))` | [extras.py:L1511](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L1511) |
| `intersect1d` | 拼接两边的 `unique`、排序、取「相邻相等」的左元素 | [extras.py:L1368-L1374](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L1368-L1374) |
| `setdiff1d` | `ar1[ in1d(ar1, ar2, invert=True) ]` | [extras.py:L1540](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L1540) |
| `setxor1d` | 拼接、排序、用「前后邻居是否都相等」挑出只出现一次的 | [extras.py:L1402-L1411](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L1402-L1411) |
| `in1d` | 拼接后用稳定 `mergesort` 排序，靠「相邻是否相等」判定 | [extras.py:L1442-L1458](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L1442-L1458) |
| `isin` | `in1d(...).reshape(element.shape)`（保形版本） | [extras.py:L1485-L1487](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L1485-L1487) |

`intersect1d` 的算法很值得记住：把 `unique(ar1)` 与 `unique(ar2)` 拼成一个有序序列，排序后，**凡是与右邻居相等的元素**就同时出现在两个集合里，取出来即交集（`aux[:-1][aux[1:] == aux[:-1]]`）。

`in1d` 的关键是注释里强调的**稳定排序** `mergesort`（[extras.py:L1443-L1446](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L1443-L1446)）：拼接时 `ar1` 在前、`ar2` 在后，排序后必须保证「值相同时 `ar1` 的元素仍排在 `ar2` 的前面」，这样「相邻相等」才能正确反映「`ar1` 的这个元素在 `ar2` 里也出现了」。

#### 4.2.3 源码精读

`unique`——核心就一句 `.view(MaskedArray)`，把 `np.unique` 的输出重新当成掩码数组（[extras.py:L1332-L1341](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L1332-L1341)）：

```python
output = np.unique(ar1, return_index=return_index,
                   return_inverse=return_inverse)
if isinstance(output, tuple):
    output = list(output)
    output[0] = output[0].view(MaskedArray)
    output = tuple(output)
else:
    output = output.view(MaskedArray)
return output
```

> 说明：`np.unique` 内部对数组做去重与排序。当输入是 MaskedArray 时，屏蔽位在排序/比较过程中被当作「同一个缺失值」处理，因此 docstring 才会出现 `[1, 2, 3, --]`——多个屏蔽位塌缩成一个排在末尾的 `--`（[extras.py:L1315-L1318](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L1315-L1318)）。`unique` 本身的代码只负责「保证输出类型是 MaskedArray」。

`intersect1d`——「相邻相等即交集」的精妙一行（[extras.py:L1368-L1374](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L1368-L1374)）：

```python
if assume_unique:
    aux = ma.concatenate((ar1, ar2))
else:
    aux = ma.concatenate((unique(ar1), unique(ar2)))
aux.sort()
return aux[:-1][aux[1:] == aux[:-1]]
```

`in1d`——稳定排序 + 「相邻相等」判定，注意末尾补一个 `invert` 来给拼接末尾的元素定终值（[extras.py:L1442-L1458](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L1442-L1458)）：

```python
ar = ma.concatenate((ar1, ar2))
order = ar.argsort(kind='mergesort')      # 必须稳定
sar = ar[order]
if invert:
    bool_ar = (sar[1:] != sar[:-1])
else:
    bool_ar = (sar[1:] == sar[:-1])
flag = ma.concatenate((bool_ar, [invert]))
indx = order.argsort(kind='mergesort')[:len(ar1)]
...
return flag[indx]                          # （assume_unique=False 时再按 rev_idx 还原）
```

`union1d` 与 `setdiff1d` 都只有一两行，是上面这些原语的直接组合（[extras.py:L1511](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L1511)、[extras.py:L1535-L1540](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L1535-L1540)）：

```python
def union1d(ar1, ar2):
    return unique(ma.concatenate((ar1, ar2), axis=None))

def setdiff1d(ar1, ar2, assume_unique=False):
    ...
    return ar1[in1d(ar1, ar2, assume_unique=True, invert=True)]
```

#### 4.2.4 代码实践

**实践目标**：验证「屏蔽值被视为同一个元素」这一约定在交集与差集中的体现。

**操作步骤**：

```python
import numpy as np
import numpy.ma as ma

x = ma.array([1, 3, 3, 3], mask=[0, 0, 0, 1])   # 末位屏蔽
y = ma.array([3, 1, 1, 1], mask=[0, 0, 0, 1])   # 末位屏蔽

print(ma.intersect1d(x, y))
print(ma.setdiff1d(ma.array([1, 2, 3, 4], mask=[0, 1, 0, 1]), [1, 2]))
```

**需要观察的现象**：

- `intersect1d(x, y)` 应得到 `masked_array(data=[1, 3, --], mask=[False, False, True])`（正是 docstring 结果，[extras.py:L1362-L1365](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L1362-L1365)）：两边都有 `1`、都有 `3`、**都各有一个屏蔽值**，于是屏蔽值也算「公共元素」出现在交集里。
- `setdiff1d(...)` 应得到 `masked_array(data=[3, --], mask=[False, True])`（[extras.py:L1529-L1532](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L1529-L1532)）：`x` 去重后是 `{1, 3, --}`，扣掉 `[1, 2]` 里出现的，剩 `3` 和 `--`。

**预期结果**：屏蔽值在交集中保留为 `--`、在差集中也保留为 `--`，证明它们被当成「一个确定的、可参与集合运算的元素」。

#### 4.2.5 小练习与答案

**练习 1**：`union1d` 为什么直接写成 `unique(concatenate(...))` 一行就够，而 `intersect1d` 却要先对两边各跑一次 `unique` 再拼接？

**参考答案**：并集只需要「合起来去重」，`concatenate` 后一次 `unique` 即可（[extras.py:L1511](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L1511)）。交集用的是「相邻相等」算法——如果某一边自身有重复，拼接排序后会出现「同一集合内的两个相等元素被误判为公共元素」的假阳性，所以必须先把两边各自 `unique` 掉，保证每个值在每一边至多出现一次（[extras.py:L1371-L1372](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L1371-L1372) 的注释也提到这比 `unique(intersect1d(...))` 更快）。

**练习 2**：`in1d` 与 `isin` 的区别是什么？新代码官方推荐用哪个？

**参考答案**：`in1d` 把输入展平、返回**一维**布尔数组；`isin` 在 `in1d` 基础上 `.reshape(element.shape)`，返回与 `element` **同形**的布尔数组（[extras.py:L1485-L1487](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L1485-L1487)）。`in1d` 的 docstring 明确写着「推荐新代码使用 `isin`」（[extras.py:L1420-L1421](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L1420-L1421)），因为它保形、更不易出错。

---

### 4.3 compress_rowcols / mask_rowcols：行列的「删除」与「染黑」

#### 4.3.1 概念说明

处理二维表格里的缺失值时，常有两种相反的需求：

- **删除**：把「只要含一个屏蔽值」的整行或整列**删掉**，得到一个「全干净」的普通 ndarray——这是 `compress_rowcols`（及其快捷方式 `compress_rows` / `compress_cols`，还有更通用的 N 维版 `compress_nd`）。
- **染黑**：反过来，把「只要含一个屏蔽值」的整行或整列**整体标记为屏蔽**，得到一个形状不变、但相关行列全屏蔽的 MaskedArray——这是 `mask_rowcols`（及其快捷方式 `mask_rows` / `mask_cols`）。

两者都**只接受二维数组**（`compress_rowcols` / `mask_rowcols` 对非二维直接抛 `NotImplementedError`），而 `compress_nd` 是它们背后真正的 N 维通用实现。

#### 4.3.2 核心流程

**删除方向（`compress_nd`，[extras.py:L904-L923](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L904-L923)）**：

1. `m = getmask(x)`；若 `m is nomask` 或 `not m.any()`，直接返回 `x._data`（无屏蔽数组零成本返回普通 ndarray）；若 `m.all()`，返回空数组。
2. 对每一个要处理的轴 `ax`：算出「除 `ax` 以外」的其余轴，沿这些轴做 `m.any(...)`——得到一个一维布尔数组，**True 表示该切片含屏蔽值**。
3. 用 `~m.any(...)` 做布尔索引，把含屏蔽值的切片删掉。多个轴依次删。

一句话：`data = data[(slice(None),)*ax + (~m.any(axis=其余轴),)]`，对每个目标轴重复一次。

**染黑方向（`mask_rowcols`，[extras.py:L1135-L1148](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L1135-L1148)）**：

1. `a = array(a, subok=False)`（注意 `subok=False`，结果强制是基类 `MaskedArray` 而非更具体子类）；非二维抛错。
2. `m = getmask(a)`；无屏蔽直接返回 `a`。
3. `maskedval = m.nonzero()` 拿到所有屏蔽位的 `(行下标数组, 列下标数组)`。
4. **复制一份 mask**（`a._mask = a._mask.copy()`，避免污染原数组），然后：
   - 若 `axis` 为 `None` 或 `0`：`a[np.unique(maskedval[0])] = masked`——把「出现过屏蔽值的所有行」整行染黑。
   - 若 `axis` 为 `None` 或 `1/-1`：`a[:, np.unique(maskedval[1])] = masked`——把「出现过屏蔽值的所有列」整列染黑。
   - `axis=None` 时两者都做（行和列都染）。

这里用到了 [u2-l6](u2-l6-indexing-assignment.md) 讲过的 `__setitem__`：给 `a[某些行] = masked` 会把那些行的 mask 置 True（软掩码下覆盖）。

#### 4.3.3 源码精读

`compress_nd` 的核心循环——对每个目标轴，用 `~m.any(axis=其余轴)` 做布尔索引删切片（[extras.py:L919-L923](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L919-L923)）：

```python
data = x._data
for ax in axis:
    axes = tuple(list(range(ax)) + list(range(ax + 1, x.ndim)))
    data = data[(slice(None),) * ax + (~m.any(axis=axes),)]
return data
```

`compress_rowcols` 只是对二维做了个「必须是 2D」的守卫，然后转发给 `compress_nd`（[extras.py:L977-L979](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L977-L979)）：

```python
if asarray(x).ndim != 2:
    raise NotImplementedError("compress_rowcols works for 2D arrays only.")
return compress_nd(x, axis=axis)
```

`mask_rowcols` 的「染黑」实现——先复制 mask，再用 `np.unique` 拿到「涉及屏蔽的不重复行/列下标」，整行/整列赋 `masked`（[extras.py:L1135-L1148](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L1135-L1148)）：

```python
a = array(a, subok=False)
if a.ndim != 2:
    raise NotImplementedError("mask_rowcols works for 2D arrays only.")
m = getmask(a)
if m is nomask or not m.any():
    return a
maskedval = m.nonzero()
a._mask = a._mask.copy()
if not axis:
    a[np.unique(maskedval[0])] = masked
if axis in [None, 1, -1]:
    a[:, np.unique(maskedval[1])] = masked
return a
```

> 注意 `if not axis:` 这个条件：`axis=None` 时 `not None` 为 `True`，`axis=0` 时 `not 0` 也为 `True`，所以这两种情况都会染黑「行」；而 `axis in [None, 1, -1]` 决定是否染黑「列」。于是 `axis=None` ⇒ 行列都染，`axis=0` ⇒ 只染行，`axis=1/-1` ⇒ 只染列，与 docstring 完全对应。

#### 4.3.4 代码实践

**实践目标**：对比「删除」与「染黑」两种处理在同一输入上的不同输出形态。

**操作步骤**：

```python
import numpy as np
import numpy.ma as ma

x = ma.array(np.arange(9).reshape(3, 3),
             mask=[[1, 0, 0],
                   [1, 0, 0],
                   [0, 0, 0]])      # 第 0 列的两个值被屏蔽

print("compress_rowcols(x)   =", ma.compress_rowcols(x))    # axis=None
print("compress_rowcols(x,0) =", ma.compress_rowcols(x, 0)) # 只删行
print("mask_rowcols(x).mask\n", ma.mask_rowcols(x).mask)
```

**需要观察的现象**：

- `compress_rowcols(x)`：第 0 列有屏蔽 ⇒ 删掉该列；第 0、1 行都有屏蔽 ⇒ 删掉这些行；只剩第 2 行第 1、2 列的 `7, 8`，即 `[[7, 8]]`（这正是 docstring 给出的结果，[extras.py:L967-L968](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L967-L968)）。
- `compress_rowcols(x, 0)`：只删行不删列 ⇒ 删掉第 0、1 行，剩 `[[6, 7, 8]]`（[extras.py:L969-L970](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L969-L970)）。

> 提示：`tests/test_extras.py` 的 `test_compress_rowcols`（test_extras.py:688 起）用多组不同输入断言同一函数，可作为对照阅读；注意它的测试输入与 docstring 的输入并不相同，故断言数值也会不同。
- `mask_rowcols(x).mask`：形状仍是 `(3,3)`，但第 0、1 行全 True、第 0 列全 True。

**预期结果**：`compress_*` 输出是**普通 ndarray 且形状变小**；`mask_rowcols` 输出是**MaskedArray 且形状不变**，只是相关行列被「染黑」。两者都正确处理了「屏蔽在哪条行/列」的信息，只是表达方式相反。

#### 4.3.5 小练习与答案

**练习 1**：`compress_rows(a)` 与 `compress_rowcols(a, 0)` 是什么关系？`mask_rows(a)` 与 `mask_rowcols(a, 0)` 呢？

**参考答案**：完全等价。`compress_rows` 的实现就是 `compress_rowcols(a, 0)`（[extras.py:L1015-L1018](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L1015-L1018)），`compress_cols` ⇔ `compress_rowcols(a, 1)`；`mask_rows` ⇔ `mask_rowcols(a, 0)`（[extras.py:L1199](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L1199)），`mask_cols` ⇔ `mask_rowcols(a, 1)`。它们都是单一参数的快捷方式。

**练习 2**：为什么 `mask_rowcols` 里要先 `a._mask = a._mask.copy()` 再去赋 `masked`？

**参考答案**：因为这一步要**就地修改** `_mask`（通过 `a[某些行] = masked` 的 `__setitem__`）。如果不先复制，就会改到调用方传入的原数组的 mask（mask 默认是共享视图，见 [u2-l2](u2-l2-maskedarray-ndarray-subclass.md) 的 `_sharedmask`）。docstring 也明确提示「The input array's mask is modified by this function」（[extras.py:L1102-L1103](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L1102-L1103)）——先 `copy` 正是为了把「修改」隔离在函数内部返回的副本上。

---

### 4.4 clump_masked / clump_unmasked / ndenumerate：连续区段与多维枚举

#### 4.4.1 概念说明

时间序列、图像行里常有「一段连续的好值、一段连续的坏值」的结构。你往往想问的不是「哪些位置被屏蔽」，而是「**哪些连续区段**被屏蔽 / 没被屏蔽」——拿到区段后可以整段 `a[sl]` 取出来分析。这就是 `clump_masked` 与 `clump_unmasked` 的用途：它们接受一个**一维**掩码数组，返回一个 **`slice` 列表**，每个 `slice` 对应一段连续的屏蔽（`clump_masked`）或非屏蔽（`clump_unmasked`）区段。

「clump」=「块、团」，指连续同质的区段。这两个函数背后共用同一个内部算法 `_ezclump`，它的核心思想极其优雅：**用一个异或（XOR）找出布尔数组里所有「跳变点」，跳变点之间的区间就是同质区段**。

`ndenumerate` 则是另一个常用工具：它是 `np.ndenumerate` 的掩码版，遍历多维数组的每个元素并给出 `(下标, 值)`，但**默认跳过屏蔽元素**（`compressed=True`），或可选地把屏蔽元素yield成全局单例 `masked`（`compressed=False`）。

#### 4.4.2 核心流程

**`_ezclump(mask)` 的异或找跳变算法（[extras.py:L2137-L2163](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L2137-L2163)）**：

设 `mask` 是一维布尔数组。核心一行：

\[
\text{idx} = (\text{mask}[1:] \;\oplus\; \text{mask}[:-1]).\text{nonzero}()[0] + 1
\]

即「相邻两个元素的异或」——只有当值从 True→False 或 False→True 跳变时异或才为 1。`nonzero()` 拿到这些跳变位置，`+1` 把它们对齐成「新区段的起点」。得到跳变点 `idx` 后，根据 `mask[0]`（数组以 True 还是 False 开头）和 `mask[-1]`（以什么结尾）把 `idx` 两两配对成一组 `slice(left, right)`。

直观例子：`mask = [T,T,T,F,F,F,T,F,F,T,T]`（T=True 屏蔽）。异或后跳变点在第 3、6、7、9 位（0-indexed 的「后一个元素」下标），`+1` 得 `idx=[3,6,7,9]`。`mask[0]=T`，所以第一段屏蔽是 `slice(0,3)`；之后成对取 `slice(6,7)`、`slice(9,11)`（`mask[-1]=T` 所以末尾再补一段到 `mask.size`）。

**`clump_masked(a)` / `clump_unmasked(a)`（[extras.py:L2166-L2235](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L2166-L2235)）**：二者都是 `_ezclump` 的薄壳——

- `clump_masked(a)`：`mask = ma.getmask(a)`；`nomask` 则返回 `[]`（没有任何屏蔽区段）；否则 `_ezclump(mask)`。
- `clump_unmasked(a)`：`mask = getattr(a, '_mask', nomask)`；`nomask` 则返回 `[slice(0, a.size)]`（整个数组都是一段非屏蔽）；否则 `_ezclump(~mask)`——**对 mask 取反**再喂给同一个 `_ezclump`。

> 注意两者取 mask 的方式不同：`clump_masked` 用 `ma.getmask`（可能是 `nomask`），`clump_unmasked` 用 `getattr(a, '_mask', nomask)`。结果一致（都拿到内部 `_mask`），但写法略异。

**`ndenumerate(a, compressed=True)`（[extras.py:L1889-L1893](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L1889-L1893)）**：把 `np.ndenumerate(a)` 与 `getmaskarray(a).flat` 拉链配对，逐元素看 mask：

- 未屏蔽：`yield it`（即 `yield (下标, 值)`）。
- 屏蔽且 `compressed=True`：跳过（什么都不 yield）。
- 屏蔽且 `compressed=False`：`yield (it[0], masked)`（用全局单例 `masked` 占位）。

#### 4.4.3 源码精读

`_ezclump` 的异或找跳变点（[extras.py:L2143-L2163](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L2143-L2163)）——一行异或 + 根据首尾分两种配对方式：

```python
if mask.ndim > 1:
    mask = mask.ravel()
idx = (mask[1:] ^ mask[:-1]).nonzero()
idx = idx[0] + 1

if mask[0]:
    if len(idx) == 0:
        return [slice(0, mask.size)]
    r = [slice(0, idx[0])]
    r.extend((slice(left, right)
              for left, right in zip(idx[1:-1:2], idx[2::2])))
else:
    if len(idx) == 0:
        return []
    r = [slice(left, right) for left, right in zip(idx[:-1:2], idx[1::2])]

if mask[-1]:
    r.append(slice(idx[-1], mask.size))
return r
```

`clump_masked` 与 `clump_unmasked`（[extras.py:L2195-L2235](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L2195-L2235)）——只是 `_ezclump` 的两副不同「滤镜」：

```python
def clump_unmasked(a):
    mask = getattr(a, '_mask', nomask)
    if mask is nomask:
        return [slice(0, a.size)]
    return _ezclump(~mask)

def clump_masked(a):
    mask = ma.getmask(a)
    if mask is nomask:
        return []
    return _ezclump(mask)
```

`ndenumerate` 的生成器（[extras.py:L1889-L1893](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L1889-L1893)）——`np.ndenumerate` 与拍平后的 mask 拉链配对：

```python
for it, mask in zip(np.ndenumerate(a), getmaskarray(a).flat):
    if not mask:
        yield it
    elif not compressed:
        yield it[0], masked
```

#### 4.4.4 代码实践

**实践目标**：用 `clump_masked` 把一维数组里所有「连续被屏蔽」的区段切成 `slice`，验证它们能正确还原出屏蔽位置；体会异或算法。

**操作步骤**（这正是本讲综合实践的后半部分，这里单独跑一遍）：

```python
import numpy as np
import numpy.ma as ma

a = ma.masked_array(np.arange(10))
a[[0, 1, 2, 6, 8, 9]] = ma.masked     # 屏蔽位: 0,1,2 和 6 和 8,9

for sl in ma.clump_masked(a):
    print(sl, "->", a.data[sl], "mask=", a.mask[sl])
```

**需要观察的现象**：应得到三段 `[slice(0, 3), slice(6, 7), slice(8, 10)]`（这正是 docstring 与 `tests/test_extras.py:163-170` 的断言结果，[extras.py:L2228-L2229](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L2228-L2229)）：

- `slice(0, 3)`：连续屏蔽的 `0,1,2`
- `slice(6, 7)`：单独屏蔽的 `6`
- `slice(8, 10)`：连续屏蔽的 `8,9`

**预期结果**：三段 `slice` 完整覆盖且仅覆盖所有屏蔽位。你也可以顺便打印 `ma.clump_unmasked(a)`，应得到 `[slice(3, 6), slice(7, 8)]`（连续非屏蔽的 `3,4,5` 与 `7`，见 `tests/test_extras.py:174-180`）。

> 待本地验证：以上数值取自 docstring 与单元测试的既定断言，是 NumPy CI 验证过的「地面真值」。你本地运行应完全一致；若不一致说明环境异常。

#### 4.4.5 小练习与答案

**练习 1**：对一个**没有任何屏蔽位**的数组 `b = ma.arange(5)`，`clump_masked(b)` 与 `clump_unmasked(b)` 分别返回什么？为什么？

**参考答案**：`clump_masked(b)` 返回 `[]`（因为 `ma.getmask(b)` 是 `nomask`，直接走 `return []` 分支，[extras.py:L2233-L2234](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L2233-L2234)）；`clump_unmasked(b)` 返回 `[slice(0, 5)]`（因为 `nomask` 走 `return [slice(0, a.size)]` 分支，[extras.py:L2197-L2198](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L2197-L2198)）——整个数组就是一段连续的非屏蔽区段。

**练习 2**：`_ezclump` 为什么用「异或」而不是「相邻相减」来找跳变点？

**参考答案**：布尔数组的异或 `mask[1:] ^ mask[:-1]` 恰好在「相邻不同」处为 True、「相邻相同」处为 False，天然就是跳变检测，且对布尔类型语义清晰、无需关心数值大小；用相减要先转成整数再判非零，多一步且语义不如异或直接。异或一次就给出全部跳变点，是这一算法最简洁的写法（[extras.py:L2145](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L2145)）。

**练习 3**：`ndenumerate(a, compressed=False)` 对屏蔽元素 yield 的是什么？它和 `np.ndenumerate(a)` 有何不同？

**参考答案**：yield 的是 `(下标, masked)`——值被替换成全局单例 `masked`（[extras.py:L1892-L1893](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L1892-L1893)）。而 `np.ndenumerate` 完全无视 mask，直接 yield 底层 `_data` 的真实值（可能是被填充的 `999999` 或原始坏值）。这正是 docstring 强调的差异：「This behavior differs from that of `numpy.ndenumerate`, which yields the value of the underlying data array.」（[extras.py:L1832-L1835](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L1832-L1835)）。

---

### 4.5 包装机制 `_fromnxfunction_*` 与拼接器 `mr_`：extras 如何「借用」NumPy 函数

#### 4.5.1 概念说明

打开 `extras.py` 你会发现一个现象：`atleast_1d` / `atleast_2d` / `atleast_3d` / `vstack` / `hstack` / `dstack` / `column_stack` / `stack` / `hsplit` / `diagflat` 这些函数，**函数体只有一行**，甚至没有 `def` 体——它们是用一套装饰器「批量生产」出来的。这套装饰器就是 `_fromnxfunction_function` 加上三种模板 `_fromnxfunction_single/seq/allargs`。

统一思想：很多 NumPy 函数对「数据」和「掩码」的处理方式**完全对称**——只要把同一个函数分别作用到 `_data` 和 `getmaskarray(a)` 上，再把两个结果重新拼成一个 `masked_array(data=..., mask=...)` 就行。于是 extras 写了一套模板，把这种「data 与 mask 各跑一次」的套路固化下来，避免给每个函数手写一遍。

`mr_` 则是另一类便利：它是 `numpy.r_` 的掩码版，用切片语法快速拼接掩码数组（沿第 0 轴），底层用的还是 `core.py` 的掩码版 `concatenate`。

#### 4.5.2 核心流程

`_fromnxfunction_function` 是一个**装饰器的装饰器**（[extras.py:L249-L276](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L249-L276)）：被它装饰的 `_fromnxfunction_*` 函数本身是「包装模板」，接收一个 `npfunc`；`decorator(npfunc)` 返回的 `wrapper` 才是真正暴露给用户的掩码函数，它把 `(npfunc, *args, **kwargs)` 转发给模板。`update_wrapper` 负责把 `npfunc` 的 `__name__` / `__qualname__` / docstring 搬过来，再追加一句「The function is applied to both the `_data` and the `_mask`, if any.」。

三种模板对应三种「NumPy 函数的参数形态」：

| 模板 | 适用形态 | 作用对象 |
| --- | --- | --- |
| `_fromnxfunction_single` | 单数组 + 辅助参数（如 `hsplit`） | 对 `np.asarray(a)` 与 `getmaskarray(a)` 各跑一次 |
| `_fromnxfunction_seq` | 一个「数组序列」参数（如 `vstack([a,b])`） | 对序列里每个数组分别取 data 与 mask |
| `_fromnxfunction_allargs` | 多个数组参数（如 `atleast_1d(a, b)`） | 每个参数独立处理，返回列表（单个时拆包） |

例如 `vstack = row_stack = _fromnxfunction_seq(np.vstack)`（[extras.py:L327](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L327)）就生成了一个掩码版 `vstack`：它对传入的每个数组的 `_data` 跑 `np.vstack`、对每个数组的 mask 跑 `np.vstack`，再合成新的 `masked_array`。

`mr_` 的实现是一条小继承链（[extras.py:L1763-L1820](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L1763-L1820)）：`MAxisConcatenator` 继承自 `numpy.lib` 的 `AxisConcatenator`，把它的 `concatenate` 替换成掩码版 `staticmethod(concatenate)`，并禁掉字符串「矩阵构造语法」（`a, b; c, d` 会抛 `MAError`）；`mr_class` 继承 `MAxisConcatenator` 并在 `__init__` 里固定沿第 0 轴拼接；最后 `mr_ = mr_class()` 是一个全局单例实例。`mr_[a, b, c]` 这种写法实际是在调用 `mr_.__getitem__((a, b, c))`。

#### 4.5.3 源码精读

`_fromnxfunction_single` 模板——「data 与 mask 各跑一次」的最朴素写法（[extras.py:L280-L288](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L280-L288)）：

```python
@_fromnxfunction_function
def _fromnxfunction_single(npfunc, a, /, *args, **kwargs):
    return masked_array(
        data=npfunc(np.asarray(a), *args, **kwargs),
        mask=npfunc(getmaskarray(a), *args, **kwargs),
    )
```

注意它用 `getmaskarray(a)` 而非 `getmask(a)`——因为要把 mask 当成普通布尔数组喂给 `npfunc`，必须保证它是同形数组而非 `nomask`（见 [u2-l1](u2-l1-mask-internal-representation.md)）。

批量生成的具体函数（[extras.py:L323-L334](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L323-L334)）——每个都只有一行：

```python
atleast_1d = _fromnxfunction_allargs(np.atleast_1d)
atleast_2d = _fromnxfunction_allargs(np.atleast_2d)
atleast_3d = _fromnxfunction_allargs(np.atleast_3d)

vstack = row_stack = _fromnxfunction_seq(np.vstack)
hstack = _fromnxfunction_seq(np.hstack)
...
hsplit = _fromnxfunction_single(np.hsplit)
diagflat = _fromnxfunction_single(np.diagflat)
```

`mr_` 的继承链与构造（[extras.py:L1763-L1820](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L1763-L1820)）——核心是把拼接函数换成掩码版：

```python
class MAxisConcatenator(AxisConcatenator):
    __slots__ = ()
    concatenate = staticmethod(concatenate)   # 掩码版 concatenate
    ...
    def __getitem__(self, key):
        if isinstance(key, str):
            raise MAError("Unavailable for masked array.")
        return super().__getitem__(key)

class mr_class(MAxisConcatenator):
    __slots__ = ()
    def __init__(self):
        MAxisConcatenator.__init__(self, 0)    # 沿第 0 轴

mr_ = mr_class()
```

docstring 给的用法（[extras.py:L1807-L1811](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L1807-L1811)）：`np.ma.mr_[np.ma.array([1,2,3]), 0, 0, np.ma.array([4,5,6])]` 会把掩码数组与标量沿第 0 轴拼起来，得到 `masked_array(data=[1, 2, 3, ..., 4, 5, 6])`。

#### 4.5.4 代码实践

**实践目标**：感受 `_fromnxfunction_*` 「data 与 mask 各跑一次」的等价性——用一个掩码数组调用 `ma.vstack`，验证它的结果等于「对 data 做 `np.vstack`、对 mask 做 `np.vstack` 再合成」。

**操作步骤**：

```python
import numpy as np
import numpy.ma as ma

a = ma.array([1, 2, 3], mask=[0, 1, 0])
b = ma.array([4, 5, 6], mask=[0, 0, 1])

r = ma.vstack([a, b])
# 手工还原 _fromnxfunction_seq 的逻辑
manual = ma.masked_array(
    data=np.vstack([np.asarray(a), np.asarray(b)]),
    mask=np.vstack([ma.getmaskarray(a), ma.getmaskarray(b)]),
)
print("vstack 结果:\n", r)
print("mask 是否一致:", np.array_equal(r.mask, manual.mask))
```

**需要观察的现象**：`r` 是一个 `(2,3)` 的 MaskedArray，`(0,1)` 与 `(1,2)` 两位被屏蔽，与手工版完全一致。你也可以试 `ma.mr_[a, b]`，它会沿第 0 轴拼成一维 `masked_array(data=[1, --, 3, 4, 5, --])`。

**预期结果**：`ma.vstack([a,b])` 与手工「data/mask 各 vstack 一次」完全等价，证明 `_fromnxfunction_seq` 确实只是这条机械规则的封装。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `_fromnxfunction_single` 用 `getmaskarray(a)` 而不是 `getmask(a)` 来取 mask？

**参考答案**：因为模板要把 mask 当成**普通布尔数组**喂给底层 `npfunc`。`getmask(a)` 在无屏蔽时会返回 `nomask`（即 `False`，一个标量），直接喂给 `np.hsplit` 之类的函数会因形状不对而出错；`getmaskarray(a)` 永远返回与 `a` 同形的布尔数组（无屏蔽时全 False），才能与 `np.asarray(a)` 配对、各跑一次同样的函数（[extras.py:L285-L287](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L285-L287)，并参见 [u2-l1](u2-l1-mask-internal-representation.md) 对这两个函数的区分）。

**练习 2**：`mr_[a, b]` 与 `ma.concatenate((a, b))` 有什么关系？

**参考答案**：`mr_` 沿第 0 轴拼接，底层调用的正是 `core.py` 的掩码版 `concatenate`（`MAxisConcatenator.concatenate = staticmethod(concatenate)`，[extras.py:L1776](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L1776)）。所以 `mr_[a, b]` 对一维 `a, b` 而言，效果与 `ma.concatenate((a, b))` 等价；`mr_` 的额外价值在于它支持切片语法、能把标量与数组混拼，写起来更像「快速构造器」。

---

## 5. 综合实践

设计一个贯穿本讲的小任务：**用 `apply_along_axis` 对二维掩码数组每行求和，再用 `clump_masked` 分析一段一维序列的缺失区段**，把 4.1 与 4.4 串起来。

**场景**：某传感器每秒采样一次，前 10 秒的数据如下，其中若干秒因故障缺失（被屏蔽）。我们想（a）按「每 3 秒一个窗口」求窗口内有效采样的和，（b）找出所有连续缺失的时段。

```python
import numpy as np
import numpy.ma as ma

# 10 秒采样，第 0,1,2 秒和第 6 秒和第 8,9 秒故障（屏蔽）
signal = ma.masked_array(np.arange(10) * 2 + 1)   # [1,3,5,7,9,11,13,15,17,19]
signal[[0, 1, 2, 6, 8, 9]] = ma.masked

# (a) 改造成 2 行 5 列：每行是一个「5 秒窗口」，对每行求和（跳过屏蔽）
windows = signal.reshape(2, 5)
row_sum = ma.apply_along_axis(ma.sum, 1, windows)
print("每个窗口有效采样之和:", row_sum)
# 预期: 第 0 行 [_,_,_,7,9] -> 7+9 = 16;  第 1 行 [11,_,15,_,_] -> 11+15 = 26

# (b) 找出原序列里所有「连续缺失」时段
for sl in ma.clump_masked(signal):
    print("缺失时段", sl, "对应秒:", list(range(sl.start, sl.stop)))
# 预期: slice(0,3)->[0,1,2]秒, slice(6,7)->[6]秒, slice(8,10)->[8,9]秒
```

**操作步骤**：

1. 构造带屏蔽的一维 `signal`，屏蔽 `0,1,2,6,8,9` 这几秒。
2. `reshape(2,5)` 得到两个窗口，用 `apply_along_axis(ma.sum, 1, windows)` 求每个窗口内**跳过屏蔽**的和。
3. 对原始一维 `signal` 调 `clump_masked`，把每段 `slice` 翻译成「人类可读的秒数区间」。
4. （可选延伸）用 `ma.compress_rowcols` 把 `windows` 里含屏蔽的行删掉，对比它与 `apply_along_axis` 的区别：前者**丢弃**整行、后者**保留并跳过屏蔽求和**。

**需要观察的现象**：

- `row_sum` 应为 `masked_array(data=[16, 26])`（`mask=False`），证明 `ma.sum` 在每行内正确跳过了屏蔽位，且 `apply_along_axis` 把每行的掩码感知结果忠实搬运到了输出。
- `clump_masked(signal)` 应给出 `[slice(0,3), slice(6,7), slice(8,10)]`，与 4.4 的结果一致。
- `ma.compress_rowcols(windows)` 会因为两行都含屏蔽而**把两行都删掉**，得到几乎空的结果——这正好说明「压缩」与「跳过屏蔽求和」是两种不同的缺失处理策略，不能混用。

**预期结果**：你应当能用一句话总结——`apply_along_axis + ma.sum` 适合「保留形状、按窗口聚合有效值」，`clump_masked` 适合「定位连续缺失时段」，`compress_rowcols` 适合「只要完全干净的行」。三者解决的是缺失数据的不同侧面。

> 待本地验证：(a) 中 `row_sum` 的具体数值依赖 `ma.sum` 对全屏蔽/部分屏蔽行的处理。本例两个窗口都至少有一个非屏蔽元素，故不会被整体屏蔽；若某窗口**全部**屏蔽，`ma.sum` 会返回该位为 `masked`（见 [u2-l7](u2-l7-reductions-stats-sort.md) 的「整轴全屏蔽才屏蔽」规则），你可构造这种边界情况自行验证。

## 6. 本讲小结

- `extras.py` 是建立在 `core.py` 三件套之上的「上层工具带」，它**依赖** core 而不被 core 依赖；本讲覆盖的工具都以 `ma.concatenate`（保留屏蔽位）与 `unique`（输出恒为 MaskedArray）为地基。
- `apply_along_axis` 本身不做掩码运算，它只是「沿轴切片、逐条喂给 `func1d`、再拼回」的搬运工；用 object 数组中转、最后取最大 dtype 统一 cast，以避免输出被意外降级。屏蔽是否被跳过，完全取决于你传的 `func1d` 是 `ma.sum` 还是 `np.sum`。
- 集合运算家族（`unique`/`union1d`/`intersect1d`/`setdiff1d`/`setxor1d`/`in1d`/`isin`）的共同契约：输出恒为 MaskedArray、屏蔽值被视为「同一个元素」、几乎都由「拼接 + 去重 + 排序后比较相邻」组合而成；`in1d` 用 `mergesort` 保证稳定性，`isin` 是它的保形版本（官方推荐新代码用 `isin`）。
- `compress_rowcols` 与 `mask_rowcols` 是两个相反方向：前者**删掉**含屏蔽值的整行/列、返回更小的普通 ndarray；后者把含屏蔽值的整行/列**染黑**、返回形状不变的 MaskedArray。`compress_rows`/`compress_cols`/`mask_rows`/`mask_cols` 都只是固定 `axis` 的快捷方式，背后真正的 N 维实现是 `compress_nd`。
- `clump_masked`/`clump_unmasked` 用一个优雅的**异或**（`mask[1:] ^ mask[:-1]`）找出一维布尔数组的所有跳变点，从而把连续同质区段切成 `slice` 列表；`ndenumerate` 是 `np.ndenumerate` 的掩码版，默认跳过屏蔽元素。
- `_fromnxfunction_single/seq/allargs` 三套模板把「对 data 与 mask 各跑一次同一函数」的套路固化，批量生成了 `vstack`/`hstack`/`atleast_1d` 等；`mr_` 是 `numpy.r_` 的掩码版，底层用的是同一个掩码版 `concatenate`。

## 7. 下一步学习建议

本讲讲完了 `extras.py` 的主要工具函数。沿着学习路线，建议接下来：

- **进入专家层**：读 [u3-l1 硬掩码、软掩码与共享掩码](u3-l1-hard-soft-shared-mask.md)，理解 `harden_mask` / `soften_mask` / `shrink_mask` / `unshare_mask`。本讲的 `mask_rowcols` 用到了 `__setitem__` 的软掩码路径，硬掩码下它的行为会不同——这是自然的进阶切入点。
- **统计函数深挖**：本讲只在 4.1 用到 `ma.sum`，`extras.py` 还提供 `average` / `median` / `cov` / `corrcoef`（[u2-l7](u2-l7-reductions-stats-sort.md) 已覆盖），可对照阅读它们的源码（extras.py:536 起），体会「屏蔽点权重清零」「按真实计数取中点」等细节。
- **更多区段工具**：本讲的 `clump_*` 是较新、较简洁的接口；`extras.py` 还有一组更老的 `flatnotmasked_edges` / `notmasked_edges` / `flatnotmasked_contiguous` / `notmasked_contiguous`（extras.py:1896-2135），它们用 `itertools.groupby` 实现，功能与 `clump_*` 部分重叠，建议对比阅读以理解 API 演进。
- **测试驱动验证**：本讲所有断言都可在 `numpy/ma/tests/test_extras.py` 找到对应测试（如 `test_clump_masked`、`test_compress_rowcols`、`test_mask_rowcols`）。建议挑一两个测试，对照本讲源码逐行读懂断言，这是巩固「源码 ↔ 行为」对应关系最直接的方式（也为 [u3-l6 测试体系与 testutils 工具](u3-l6-testutils-testing.md) 做铺垫）。
