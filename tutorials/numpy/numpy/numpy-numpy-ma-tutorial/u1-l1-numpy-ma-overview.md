# 认识 numpy.ma：为什么需要掩码数组

## 1. 本讲目标

本讲是 `numpy.ma` 学习手册的第一讲。读完本讲后，你应当能够：

1. 说清楚什么是「掩码数组」，以及它由 `data`、`mask`、`fill_value` 三部分组成。
2. 理解为什么普通的 `numpy.ndarray` 在处理缺失值（如 `NaN`）或无效值时会「一坏坏一锅」，而掩码数组能优雅地把坏值排除在运算之外。
3. 知道 `numpy.ma` 在整个 NumPy 中的定位、它的由来，以及它曾在 2006 年被重写的背景。
4. 复述 README 中描述的「新 `MaskedArray`」与「旧实现」之间的主要差异。

本讲不要求你之前用过掩码数组，但假设你已经会用 `numpy` 创建数组、做加减乘除和求均值。

---

## 2. 前置知识

在进入 `numpy.ma` 之前，先用最朴素的语言回顾三个概念：

- **缺失值（missing value）**：数据本该有、但因为仪器故障、问卷漏填、网络中断等原因没有采集到的值。在浮点世界里，常用 `NaN`（Not a Number）来占位。
- **无效值（invalid value）**：数据虽然在，但语义上是错的，比如传感器读到一个超出物理范围的温度，或者一次「除以零」产生 `inf`。
- **`NaN` 的传染性**：在 IEEE 754 浮点标准里，任何数与 `NaN` 做运算结果仍是 `NaN`。这意味着只要数组里有一个 `NaN`，对整个数组求 `mean`、`sum`，结果往往都会变成 `NaN`。

一句话：`NaN` 像一滴墨水，滴进清水里整杯都黑了。`numpy.ma` 要解决的就是「如何让这滴墨水被标记出来、不影响其它干净的水」。

关于「子类（subclass）」：NumPy 里 `MaskedArray` 是 `numpy.ndarray` 的子类。如果你还不太理解「子类」没关系，本讲你只需要知道：**掩码数组把普通数组当作它的「底层数据」，再额外加了一张「标记表（mask）」**，详细的子类化机制会在进阶层讲义（u2-l2）展开。

---

## 3. 本讲源码地图

`numpy.ma` 子包位于 NumPy 源码的 `numpy/ma/` 目录下。本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [`README.rst`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/README.rst) | 讲述掩码数组的历史、新旧实现差异，以及「是否预先填充」的性能讨论。本讲的「历史与差异」部分主要依据它。 |
| [`__init__.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/__init__.py) | 子包入口。它用一段含 `NaN` 的示例讲明了掩码数组的动机，并负责把 `core` 和 `extras` 的内容统一 re-export 出来。 |
| [`core.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py) | 核心实现。约 7000 行，定义了 `MaskedArray` 类、`masked` 单例、各种 `masked_xxx` 构造函数、掩码 ufunc 包装等。本讲的源码精读绝大部分来自这里。 |

整个目录还包括 `extras.py`（统计与集合工具）、`mrecords.py`（字段级屏蔽记录）、`testutils.py`（测试断言）、`tests/`（测试集），以及与每个 `.py` 一一对应的 `.pyi` 类型桩文件。这些会在后续讲义中陆续登场，本讲只需建立整体印象。

---

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：

1. 缺失/无效值处理动机（为什么需要掩码数组）
2. 掩码数组三件套：`data`、`mask`、`fill_value`
3. `numpy.ma` 的历史与重写背景
4. 新 `MaskedArray` 与旧实现的主要差异

---

### 4.1 缺失/无效值处理动机

#### 4.1.1 概念说明

设想你采集到一组传感器读数：

```
[2.0, 1.0, 3.0, NaN, 5.0, 2.0, 3.0, NaN]
```

其中第 4、8 两个位置是设备故障导致的 `NaN`。你想算这组数据的平均值。

直观上，平均值应该是「把 6 个有效读数相加再除以 6」，结果约为 2.67。但如果你直接对原始数组调用 `np.mean`，由于 `NaN` 的传染性，结果会是 `NaN`。

`numpy.ma` 的核心思想是：**不要删除坏数据，而是给它贴一张「屏蔽标签」**。被贴标签的元素在统计运算中会被自动忽略，但它在数组里的位置和形状保持不变——这一点对时间序列、对齐运算非常重要。

`numpy.ma` 子包入口 `__init__.py` 的模块文档字符串正是用这个例子开门见山：

> Arrays sometimes contain invalid or missing data. When doing operations on such arrays, we wish to suppress invalid values, which is the purpose masked arrays fulfill.

#### 4.1.2 核心流程

把「直接对脏数据求均值」与「用掩码数组求均值」放在一起对比，流程如下：

```text
【普通数组】
原始数组 [2,1,3,NaN,5,2,3,NaN]
        │
        ▼ np.mean
   sum = 2+1+3+NaN+... = NaN   ← 一个 NaN 污染了整个结果
   mean = NaN / 8 = NaN        ← 你什么都得不到

【掩码数组】
原始数组 [2,1,3,NaN,5,2,3,NaN]
        │ 用 mask 标记 NaN 位置（True=屏蔽）
        ▼ masked_array(data, mask=np.isnan(x))
   有效元素 = [2,1,3,5,2,3]    ← NaN 被忽略
   mean = (2+1+3+5+2+3)/6 = 2.666...
```

可以把它写成一条朴素的公式。设数组有 \(n\) 个元素，其中被屏蔽（mask=True）的集合为 \(M\)，未屏蔽的集合为 \(U \)。掩码数组的均值只对未屏蔽元素求：

\[
\mu = \frac{1}{|U|}\sum_{i \in U} x_i, \qquad U = \{ i \mid \mathrm{mask}_i = \mathrm{False} \}
\]

而普通数组的均值 \(\frac{1}{n}\sum_{i} x_i\) 只要某个 \(x_i\) 是 `NaN`，整个和就被污染成 `NaN`。

#### 4.1.3 源码精读

`__init__.py` 把这个动机写成了一段可执行的 doctest。先看「脏数据」一侧——构造含 `NaN` 的数组，并对它求均值，结果是 `nan`：[__init__.py:L10-L20](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/__init__.py#L10-L20)

```python
>>> x = np.array([2, 1, 3, np.nan, 5, 2, 3, np.nan])
...
>>> np.mean(x)
nan
```

接着，「Enter masked arrays」——用 `masked_array` 把 `isnan(x)` 的位置屏蔽掉，再求均值，就得到了正确的 2.667：[__init__.py:L23-L33](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/__init__.py#L23-L33)

```python
>>> m = np.ma.masked_array(x, np.isnan(x))
>>> m
masked_array(data=[2.0, 1.0, 3.0, --, 5.0, 2.0, 3.0, --],
             mask=[False, False, False, True, False, False, False, True],
      fill_value=1e+20)
>>> np.mean(m)
2.6666666666666665
```

注意打印结果里的 `--`：这就是被屏蔽元素在显示时的占位符，表示「这里有个值，但已被忽略」。

本讲的实践任务会用到 `masked_invalid`，它是把上面的 `np.isnan + inf` 一步到位的便捷函数。看它的实现——它本质上是 `masked_where` 的一个快捷方式：[core.py:L2400-L2434](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L2400-L2434)

```python
def masked_invalid(a, copy=True):
    """Mask an array where invalid values occur (NaNs or infs).
    ...
    """
    a = np.array(a, copy=None, subok=True)
    res = masked_where(~(np.isfinite(a)), a, copy=copy)
    ...
    return res
```

关键就是 `~(np.isfinite(a))`：`np.isfinite` 对有限数返回 `True`，取反后，凡是 `NaN` 或 `±inf` 的位置就变成 `True`（即「需要屏蔽」），再交给 `masked_where` 去贴标签。

#### 4.1.4 代码实践

> 这正是本讲规格指定的实践任务。

**实践目标**：亲手感受「一个 `NaN` 污染整个均值」与「掩码数组排除 `NaN` 后得到正确均值」的差别。

**操作步骤**：

```python
# 示例代码：在 Python 解释器或脚本中运行
import numpy as np
import numpy.ma as ma

# 1) 纯 numpy：含 NaN 的数组
x = np.array([2.0, 1.0, 3.0, np.nan, 5.0, 2.0, 3.0, np.nan])
print("np.mean(x) =", np.mean(x))

# 2) 用 masked_invalid 屏蔽 NaN/inf，再求均值
m = ma.masked_invalid(x)
print("m =", m)
print("m.mask =", m.mask)
print("np.mean(m) =", np.mean(m))
```

**需要观察的现象**：

1. `np.mean(x)` 应为 `nan`——`NaN` 的传染性让整组求和失效。
2. `m` 的打印里，第 4、8 个位置显示为 `--`。
3. `m.mask` 在这两个位置为 `True`，其余为 `False`。
4. `np.mean(m)` 返回 `2.6666666666666665`（即 `(2+1+3+5+2+3)/6`）。

**预期结果**：

```text
np.mean(x) = nan
m = masked_array(data=[2.0, 1.0, 3.0, --, 5.0, 2.0, 3.0, --], ...)
m.mask = [False False False  True False False False  True]
np.mean(m) = 2.6666666666666665
```

> 若你本地 NumPy 版本与本文档不同导致打印格式略有差异，属正常现象；核心数值（`nan` 与 `2.667`）不变。

#### 4.1.5 小练习与答案

**练习 1**：如果把上面数组里的 `np.nan` 换成 `np.inf`，`np.mean(x)` 会是多少？`np.ma.masked_invalid` 还能正确处理吗？

**参考答案**：`np.mean(x)` 仍会得到 `inf`（无限大的传染性，类似于 `NaN`）。`masked_invalid` 同样能处理，因为 `np.isfinite` 对 `±inf` 也返回 `False`，会被屏蔽，最终均值为 `2.667`。

**练习 2**：为什么不能简单地用 `np.nansum(x)/len(x)` 来得到正确均值？

**参考答案**：`np.nansum` 会跳过 `NaN` 求和（得到 16），但分母 `len(x)` 仍是 8，于是得到 2.0 而非 2.667。正确做法是要么用 `np.nanmean(x)`（自动按有效个数除），要么用掩码数组。掩码数组的优势在于它把「哪些位置无效」这个信息固化在对象里，后续所有运算都能一致地遵守，而不必每次都记得调用 `nan*` 版本的函数。

---

### 4.2 掩码数组三件套：data、mask、fill_value

#### 4.2.1 概念说明

一个掩码数组对象由三部分构成：

1. **`data`**：底层数据，是一个普通的 `ndarray`（或其子类）。它「原样」保存了所有元素，包括那些被判定为无效的——也就是说 `data` 里仍然看得到 `NaN`、`999999` 这些原始坏值。
2. **`mask`**：与 `data` 同形状的布尔数组，`True` 表示该位置被屏蔽（无效），`False` 表示有效。如果没有任何元素被屏蔽，`mask` 是一个特殊值 `nomask`（见 4.4）而不是全 `False` 数组，以节省内存。
3. **`fill_value`**：填充值。当需要把掩码数组「还原」成一个普通数组（比如保存到文件、喂给不认识掩码的库）时，被屏蔽的位置用什么具体数值来顶替？这个值由 `fill_value` 决定，默认随数据类型变化（整数默认 `999999`，浮点默认 `1e+20`）。

这三件套的关系可以理解为：`data` 是「全部事实」，`mask` 是「哪些事实不可信」，`fill_value` 是「不可信的事实对外要怎么包装」。

#### 4.2.2 核心流程

掩码数组在「对外提供数据」时有两条常见路径，对应两个常用方法：

```text
        ┌─────────────────────────────┐
        │      MaskedArray 对象        │
        │  data  = [2, 999, 3]         │
        │  mask = [F,  T,  F]          │
        │  fill_value = -999           │
        └──────────────┬──────────────┘
                       │
        ┌──────────────┴──────────────┐
        ▼                             ▼
   .filled()                     .compressed()
 把 mask=True 的位置            直接丢掉 mask=True 的位置
 替换成 fill_value              只返回有效元素
 → [2, -999, 3]（普通数组）     → [2, 3]（一维普通数组）
 形状不变                        形状改变（被压扁）
```

- `.filled()`：保持形状，把屏蔽位用填充值「填实」，得到普通 `ndarray`。
- `.compressed()`：丢弃屏蔽位，把剩余有效元素压成一维，得到普通 `ndarray`。

#### 4.2.3 源码精读

先看三件套的访问入口。

**data**：`MaskedArray` 用一个只读 property 暴露底层 `_data`，它返回的是按 `baseclass` 视图后的底层数据：[core.py:L3762-L3780](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3762-L3780)

```python
def _get_data(self):
    """Returns the underlying data, as a view of the masked array.
    ...
    """
    return ndarray.view(self, self._baseclass)

_data = property(fget=_get_data)
data = property(fget=_get_data)
```

注意 `_data` 和 `data` 指向同一个 getter，所以 `m.data` 和 `m._data` 都能拿到底层数据（包括那些坏值本身）。

**mask**：`mask` property 返回内部 `_mask` 的一个视图（view），这样能保证外部无法意外改变它的 dtype/shape：[core.py:L3583-L3595](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3583-L3595)

```python
@property
def mask(self):
    """ Current mask. """
    # Return a view so that the dtype and shape cannot be changed in place
    return self._mask.view()

@mask.setter
def mask(self, value):
    self.__setmask__(value)
```

**fill_value**：它是一个 property，当没有显式设置时会按 dtype 自动生成默认值：[core.py:L3792-L3832](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3792-L3832)

```python
@property
def fill_value(self):
    if self._fill_value is None:
        self._fill_value = _check_fill_value(None, self.dtype)
    ...
    return self._fill_value
```

默认填充值的「字典」定义在文件靠前位置，按 dtype 的 kind 分类：[core.py:L163-L174](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L163-L174)

```python
default_filler = {'b': True,
                  'c': 1.e20 + 0.0j,
                  'f': 1.e20,
                  'i': 999999,
                  'O': '?',
                  'S': b'N/A',
                  ...
                  'U': 'N/A'}
```

可以看到整数（`i`）默认填充 `999999`，浮点（`f`）默认 `1e+20`——这正好解释了 4.1.3 中打印结果末尾的 `fill_value=1e+20`。

再来看两条「还原」路径。

**filled**：把屏蔽位替换为填充值（或自定义值），返回普通 `ndarray`：[core.py:L3857-L3902](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3857-L3902)（关键行为见其文档示例）

```python
>>> x = np.ma.array([1,2,3,4,5], mask=[0,0,1,0,1], fill_value=-999)
>>> x.filled()
array([   1,    2, -999,    4, -999])
>>> type(x.filled())
<class 'numpy.ndarray'>
```

> 官方强调：`filled()` 的返回**不是** `MaskedArray`，而是普通 `ndarray`（保留 `_data` 的子类，比如 `recarray`）。

**compressed**：丢弃屏蔽位，压成一维：[core.py:L3938-L3972](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3938-L3972)

```python
def compressed(self):
    """Return all the non-masked data as a 1-D array."""
    data = ndarray.ravel(self._data)
    if self._mask is not nomask:
        data = data.compress(np.logical_not(ndarray.ravel(self._mask)))
    return data
```

注意它先 `ravel` 拍平，再用「mask 取反」做 `compress`，因此对多维数组也只返回一维结果。同样，返回类型是普通 `ndarray`。

#### 4.2.4 代码实践

**实践目标**：验证三件套各自能取到什么，并比较 `filled` 与 `compressed` 两种「还原」方式的输出差异。

**操作步骤**：

```python
# 示例代码
import numpy as np
import numpy.ma as ma

m = ma.array([1, 2, 3, 4, 5], mask=[0, 0, 1, 0, 1])
print("data       =", m.data)
print("mask       =", m.mask)
print("fill_value =", m.fill_value)

m.fill_value = -99  # 自定义填充值
print("\nfilled()     =", m.filled(), "类型:", type(m.filled()).__name__)
print("filled(1000) =", m.filled(1000))
print("compressed() =", m.compressed(), "类型:", type(m.compressed()).__name__)
```

**需要观察的现象与预期结果**：

1. `m.data` = `[1 2 3 4 5]`——屏蔽位在 `data` 里**仍然保留**原始值 `3` 和 `5`。
2. `m.mask` = `[False False True False True]`。
3. `m.fill_value` 初始为 `999999`（整数默认），改成 `-99` 后生效。
4. `m.filled()` = `[1 2 -99 4 -99]`，形状不变，是普通 `ndarray`。
5. `m.filled(1000)` = `[1 2 1000 4 1000]`——可临时指定填充值。
6. `m.compressed()` = `[1 2 4]`，被压成一维，只剩 3 个有效元素。

> 待本地验证：不同 NumPy 版本里 `m.mask` 在「全屏蔽/无屏蔽」时的显示细节可能略有不同，但本例有屏蔽值，行为稳定。

#### 4.2.5 小练习与答案

**练习 1**：`m.data` 里屏蔽位置 `3` 和 `5` 还在不在？这说明 `mask` 和 `data` 是什么关系？

**参考答案**：还在。`data` 保存的是「全部原始事实」，`mask` 只是「附加的标签」。屏蔽并不会从 `data` 里删掉数值，只是让运算时跳过它。这正是 `filled()` 能用填充值替换它的前提——底层值还在，要不要用、用什么替换，由 `mask` 和 `fill_value` 决定。

**练习 2**：对一个二维掩码数组，`.compressed()` 返回的形状是几维？

**参考答案**：一维。源码里先 `ravel()` 拍平、再按 `mask` 取反 `compress`，所以无论原数组是几维，`compressed()` 永远返回一维数组。

---

### 4.3 numpy.ma 的历史与重写背景

#### 4.3.1 概念说明

要理解 `numpy.ma` 今天的样子，需要知道它经历过一次「推倒重来」。

- **最初版本**：掩码数组最早由 Paul F. Dubois 在 `numarray` 时代实现，后来由 Travis Oliphant 和 Paul Dubois 适配到 NumPy，对应模块是 `numpy.core.ma`。
- **2006 重写**：Pierre Gerard-Marchant 因为「子类化掩码数组时丢失附加属性」的痛苦，**把 `MaskedArray` 改写成了 `ndarray` 的子类**，并大幅改善了对结构化数组的支持。这就是我们今天用的版本。

这两段历史直接写在 `core.py` 的模块文档字符串开头：[core.py:L1-L8](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1-L8)

```python
"""
numpy.ma : a package to handle missing or invalid values.

This package was initially written for numarray by Paul F. Dubois
at Lawrence Livermore National Laboratory.
In 2006, the package was completely rewritten by Pierre Gerard-Marchant
(University of Georgia) to make the MaskedArray class a subclass of ndarray,
and to improve support of structured arrays.
...
"""
```

#### 4.3.2 核心流程

重写的核心动机是一条「需求 → 痛点 → 方案」的链路：

```text
需求：在「可缺失数值」之外，还想给数组附加额外信息（如时间戳）
   │
   ▼ 子类化旧的 MaskedArray
痛点：对子数组做运算（如 +1）时，自定义属性/类型信息丢失
      （旧实现返回的是「普通 masked ndarray」，而不是「我那个子类」）
   │
   ▼ 学习 ndarray 的 __new__ / __array_finalize__
方案：让 MaskedArray 本身就是 ndarray 的子类
      这样它的子类化行为和普通 ndarray 一致，属性能正确传播
```

也就是说，重写不是为了「更快」，而是为了「**更好子类化、更好和 ndarray 生态融合**」。速度问题在 README 里坦承「initially marginally slower」（最初略慢），但「预期可以做到比旧的更快」。

#### 4.3.3 源码精读

最完整的「第一人称」叙述在 `README.rst` 的 History 一节。作者自述了他为什么要重写：[README.rst:L14-L46](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/README.rst#L14-L46)。其中点明了重写的两条核心变化：

```rst
The main differences with the initial *numpy.core.ma* package are
that MaskedArray is now a subclass of *ndarray* and that the
*_data* section can now be any subclass of *ndarray*.
```

这两点是后续所有机制的基础：

1. `MaskedArray` 是 `ndarray` 的子类（所以 `isinstance(m, np.ndarray)` 为 `True`）。
2. `MaskedArray` 的 `_data` 部分可以是任意 `ndarray` 子类（比如矩阵、记录数组等）。

而在源码中，`MaskedArray` 类的定义本身就印证了第 1 点——它的基类赫然写着 `ndarray`：[core.py:L2770-L2771](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L2770-L2771)

```python
class MaskedArray(ndarray):
    """
    An array class with possibly masked values.
    ...
```

子包入口 `__init__.py` 也把这两位作者（Pierre Gerard-Marchant、Jarrod Millman）记在了模块作者里：[__init__.py:L38-L39](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/__init__.py#L38-L39)

```rst
.. moduleauthor:: Pierre Gerard-Marchant
.. moduleauthor:: Jarrod Millman
```

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：通过阅读源码与一个小实验，确认「`MaskedArray` 是 `ndarray` 子类」这一历史决策在今天的代码里依然成立。

**操作步骤**：

1. 打开 [core.py:L2770](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L2770)，确认 `class MaskedArray(ndarray)` 的基类。
2. 运行下面的代码验证继承关系：

```python
import numpy as np
import numpy.ma as ma

m = ma.array([1, 2, 3])
print("isinstance(m, np.ndarray)    :", isinstance(m, np.ndarray))
print("isinstance(m, ma.MaskedArray):", isinstance(m, ma.MaskedArray))
print("type(m).__mro__              :", [c.__name__ for c in type(m).__mro__])
```

**需要观察的现象与预期结果**：

- `isinstance(m, np.ndarray)` 为 `True`——这正是 2006 重写带来的核心特性。
- `isinstance(m, ma.MaskedArray)` 也为 `True`。
- `__mro__`（方法解析顺序）里能看到 `MaskedArray` 排在 `ndarray` 之前，最终到 `object`，形如 `['MaskedArray', 'ndarray', 'object']`。

> 待本地验证：`__mro__` 列表里可能还会出现中间类型（取决于版本），但 `MaskedArray` 在 `ndarray` 之前、最终归于 `object` 的结构稳定。

#### 4.3.5 小练习与答案

**练习 1**：为什么作者「学习 `__new__` / `__array_finalize__`」之后，会得出「masked arrays 应该是 ndarray 而不是普通 Python 对象」的结论？

**参考答案**：因为 `__new__` 和 `__array_finalize__` 是 NumPy 子类化机制的关键钩子。如果 `MaskedArray` 继承自 `ndarray`，它就能复用这套钩子来保证「切片、视图、ufunc 运算后返回的依然是正确的子类类型」，自定义属性也能随之传播。这正是旧实现做不到、让作者痛苦的点。

**练习 2**：README 说重写「initially marginally slower」，为什么改成 `ndarray` 子类反而可能变慢？

**参考答案**：子类化意味着每次运算、切片都要走 `__array_finalize__` 等钩子来传播 `mask` 和属性，这带来了额外开销；同时旧实现可以选择「先填充再算」等捷径。不过作者认为这些是可以优化的，目标是最终更快。具体的「填充 vs 不填充」性能取舍会在专家层讲义 u3-l7 展开。

---

### 4.4 新 MaskedArray 与旧实现的主要差异

#### 4.4.1 概念说明

`README.rst` 用一节「Main differences」列出了新旧实现的具体行为差异。这些差异大多在今天仍然体现在 API 设计里，是理解「为什么 `numpy.ma` 行为是这个样子」的关键。

几个最值得记住的差异：

- **`fill_value` 从「函数」变成了「属性」**：旧实现用 `fill_value(a)` 这种函数调用，新实现用 `a.fill_value` 属性访问。
- **无屏蔽时掩码压缩为 `nomask`**：当没有任何元素被屏蔽时，`mask` 不是「全 `False` 数组」，而是特殊单例 `nomask`，以省内存。
- **`bool(a)` 会抛 `ValueError`**：和普通 `ndarray` 一样，元素数大于 1 时不能转成单个布尔值。
- **两个掩码数组的比较结果是掩码数组，而不是单个布尔值**。
- **`cumsum` / `cumprod` 把屏蔽位当 0 / 1 来算**，mask 保留但不更新。
- **掩码总是被打印出来**（即使没有屏蔽值），方便一眼认出「这是掩码数组」。

#### 4.4.2 核心流程

`nomask` 这个「空掩码单例」是差异里最基础的一条，它的定义只有一行，但意义贯穿全包：[core.py:L87-L88](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L87-L88)

```python
MaskType = np.bool
nomask = MaskType(0)
```

`nomask` 就是一个布尔 `False`。当你创建一个没有任何屏蔽值的掩码数组时，它的 `_mask` 会被设成 `nomask`，而不是一个和 `data` 同形状的全 `False` 数组：

```text
旧实现：mask 永远是同形状布尔数组（哪怕没屏蔽值）→ 浪费内存
新实现：没有屏蔽值时，mask = nomask（单个 False）→ 省内存
        一旦出现屏蔽值，才「展开」成真正的布尔数组
```

这就是 `compressed()` 源码里要先判断 `if self._mask is not nomask` 的原因——如果已经是 `nomask`，就根本没有屏蔽位可压缩。

#### 4.4.3 源码精读

完整的差异清单在 README：[README.rst:L56-L68](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/README.rst#L56-L68)

```rst
Main differences
----------------
 * The *_data* part of the masked array can be any subclass of ndarray ...
 * *fill_value* is now a property, not a function.
 * in the majority of cases, the mask is forced to *nomask* when no value is actually masked ...
 * *put*, *putmask* and *take* now mimic the ndarray methods ...
 * if *a* is a masked array, *bool(a)* raises a *ValueError*, as it does with ndarrays.
 * the comparison of two masked arrays is a masked array, not a boolean
 * *filled(a)* returns an array of the same subclass as *a._data* ...
 * the mask is always printed, even if it's *nomask* ...
 * *cumsum* works as if the *_data* array was filled with 0 ...
 * *cumprod* works as if the *_data* array was filled with 1 ...
```

对应到源码，可以挑两条验证：

1. **`fill_value` 是 property**：在 4.2.3 已经看到 `@property def fill_value`（[core.py:L3792](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3792)），并且支持 `m.fill_value = X` 赋值。
2. **掩码总是被打印**：掩码数组的 `__repr__` 会把 `data=`、`mask=`、`fill_value=` 三段都打出来（4.1.3 里的打印结果就是证据），即使 `mask` 是 `nomask` 也会显示为 `mask=False`。

另外，README 还专门讨论了一个工程取舍：「运算前要不要先把数组 fill 起来」？这一节叫 *Optimizing maskedarray*：[README.rst:L153-L219](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/README.rst#L153-L219)。结论是：**预填充能避免浮点异常（`inf`/`nan` 污染结果），不填充更快但可能产生 `inf`/`nan`**。这个取舍会在专家层讲义 u3-l7 详细展开，本讲只需建立印象。

#### 4.4.4 代码实践

**实践目标**：亲手验证三条差异——`nomask` 压缩、`bool(a)` 抛异常、比较结果是掩码数组。

**操作步骤**：

```python
# 示例代码
import numpy as np
import numpy.ma as ma

# 差异 1：没有任何屏蔽值时，mask 是 nomask
a = ma.array([1, 2, 3])           # 没传 mask
print("a.mask is ma.nomask :", a.mask is ma.nomask)
print("a.mask              :", a.mask)

# 差异 2：bool(a) 对多元素数组会抛 ValueError
try:
    bool(a)
except ValueError as e:
    print("bool(a) raised ValueError:", e)

# 差异 3：两个掩码数组比较，结果是掩码数组而非单个 bool
b = ma.array([1, 2, 9], mask=[0, 0, 1])
cmp = (a == ma.array([1, 2, 3]))
print("type(a == another) :", type(cmp).__name__)
print("cmp                :", cmp)
```

**需要观察的现象与预期结果**：

1. `a.mask is ma.nomask` 为 `True`；`a.mask` 显示为 `False`（即 `nomask`）。一旦你 `a[0] = ma.masked`，`a.mask` 就会变成一个真正的布尔数组。
2. `bool(a)` 抛出 `ValueError`，提示「真值不明确」。
3. `a == 另一个数组` 的结果是 `MaskedArray`，而不是单个 `bool`；参与比较的若有屏蔽位，结果对应位置也会被屏蔽。

> 待本地验证：`bool(a)` 的异常文案在不同 NumPy 版本里措辞可能不同，但异常类型是 `ValueError`。

#### 4.4.5 小练习与答案

**练习 1**：为什么把「无屏蔽值时的 mask」设成 `nomask` 单例，而不是直接用一个全 `False` 的布尔数组？

**参考答案**：为了节省内存和判断开销。一个全 `False` 数组要和 `data` 同形状，对大数组是一笔不小的开销；而 `nomask` 只是一个布尔 `False` 单例。源码里大量出现 `if self._mask is nomask` 的判断（比如 `compressed`），就是为了利用这个「短路」：没有屏蔽值时直接跳过所有掩码相关计算。

**练习 2**：README 说 `cumsum` 「works as if the `*_data*` array was filled with 0」。请推测：对一个 `mask=[0,1,0]` 的数组 `[10, 20, 30]`，`cumsum()` 会得到什么？

**参考答案**：相当于先把屏蔽位当 0 填充得到 `[10, 0, 30]`，再累加：`[10, 10, 40]`。同时 mask 被保留但不更新（仍是 `[False, True, False]`）。注意它**不会**跳过屏蔽位做累加，这一点和 `sum`/`mean` 这类「跳过屏蔽位」的归约不同。

---

## 5. 综合实践

把本讲的四个模块串起来，完成下面这个小任务。它模拟一个真实场景：**你有一份带缺失值的传感器数据，需要做清洗、查看和初步统计**。

**任务**：读取下面这组数据（其中 `NaN` 表示传感器掉线，`-999` 是另一套系统用的「缺失占位符」），用 `numpy.ma` 完成清洗与统计。

```python
# 示例代码：综合实践
import numpy as np
import numpy.ma as ma

raw = np.array([12.5, np.nan, 13.0, -999.0, 14.0, np.nan, 15.0])

# 步骤 1：屏蔽无效值（NaN/inf）
m1 = ma.masked_invalid(raw)
print("步骤1 masked_invalid:", m1)

# 步骤 2：再屏蔽 -999 这种自定义缺失占位符
m2 = ma.masked_where(m1 == -999.0, m1)
print("步骤2 masked_where  :", m2)
print("mask                :", m2.mask)

# 步骤 3：查看三件套
print("data                :", m2.data)    # 仍能看到原始坏值
print("fill_value          :", m2.fill_value)

# 步骤 4：统计有效数据
print("有效个数            :", m2.count())
print("均值                :", m2.mean())

# 步骤 5：导出给「不认识掩码」的下游程序
print("filled(-1)          :", m2.filled(-1))   # 形状不变，缺失位填 -1
print("compressed()        :", m2.compressed()) # 只剩有效值，压成一维
```

**你应当观察到**：

- 经过两步屏蔽后，`NaN` 和 `-999` 的位置 mask 都为 `True`。
- `m2.data` 里依然能看到 `nan` 和 `-999.0`（原始事实没被删除）。
- `mean` 只对 `[12.5, 13.0, 14.0, 15.0]` 求均值，约为 `13.625`。
- `filled(-1)` 保持 7 个元素、缺失位变成 `-1`；`compressed()` 只有 4 个有效元素。

**思考**：如果下游程序要求「缺失值用 `0` 顶替并保持形状」，你该用 `filled(0)` 还是 `compressed()`？为什么？（答：用 `filled(0)`，因为它保持形状；`compressed()` 会改变形状、丢失对齐关系。）

> 提示：`masked_where` 在 4.1.3 出现过；`count()`、`mean()` 是掩码感知的归约方法，其内部机制会在进阶层讲义 u2-l7 展开。本综合实践中你只需把它们当作「会自动跳过屏蔽值」的黑盒来用。

---

## 6. 本讲小结

- **掩码数组 = `data` + `mask` + `fill_value`**：`data` 保存全部原始值（含坏值），`mask` 标记哪些位置无效（`True`=屏蔽），`fill_value` 决定屏蔽位对外如何填充。
- **动机是隔离坏值**：`NaN`/`inf` 有传染性，会污染整组运算；掩码数组把这些坏值「标记并跳过」，让 `mean`/`sum` 等运算只作用于有效数据。
- **`masked_invalid` 是便捷入口**：它等价于 `masked_where(~np.isfinite(a), a)`，一步屏蔽所有 `NaN`/`inf`。
- **`filled()` vs `compressed()`**：前者保持形状、用填充值替换屏蔽位，返回普通 `ndarray`；后者丢弃屏蔽位、压成一维。
- **`MaskedArray` 是 `ndarray` 的子类**：这是 2006 年重写的核心决策，使它能融入 NumPy 生态、支持子类化。
- **新旧差异至今可见**：`fill_value` 是 property、无屏蔽时 `mask` 压缩为 `nomask` 单例、`bool(a)` 抛 `ValueError`、比较结果是掩码数组。

---

## 7. 下一步学习建议

本讲建立了「为什么需要掩码数组」和「三件套」的整体印象。下一步建议：

1. **先学 u1-l2《导入、命名空间与目录结构》**：搞清楚 `import numpy.ma` 之后，`array`、`masked_where`、`average` 这些名字分别来自 `core` 还是 `extras`，并认识 `.pyi` 类型桩。
2. **再学 u1-l3《创建掩码数组的多种方式》**：系统掌握 `masked_where`、`masked_equal`、`masked_values`、`masked_inside/outside` 等构造函数的差别。
3. **然后学 u1-l4《读取与提取：data、mask、fill_value》**：深入本讲点到的 `filled`、`compressed`、`getdata`、`getmask`。
4. 进入进阶层后，**u2-l1《掩码的内部表示与构造》**会正式展开 `nomask`、`make_mask_descr`、`getmask` vs `getmaskarray` 等掩码内部细节，**u2-l2《MaskedArray 类与 ndarray 子类化机制》**会讲清本讲反复提到的 `__new__` / `__array_finalize__` 钩子。

建议你在进入下一篇之前，先把本讲「综合实践」完整跑一遍，确认 `masked_invalid`、`masked_where`、`filled`、`compressed` 的输出和预期一致。
