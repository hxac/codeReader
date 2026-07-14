# NaN 基础设施与聚合：nansum/nanprod 等

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 NumPy 处理「含 NaN 数组」的核心套路：**替换-聚合-还原**三段式。
- 读懂 `_nan_mask` / `_replace_nan` / `_copyto` 这三个私有基础设施各自负责什么，以及它们为何要先判断 `dtype.kind`。
- 理解 `_remove_nan_1d` 用「把非 NaN 搬到数组头部再切片」来就地剔除 NaN 的紧凑技巧，以及它为何**不保序**。
- 掌握 `_divide_by_count` 对均值/方差类运算的除法修正（为何要用 `errstate` 抑制 0/0 警告）。
- 看懂 `nansum`/`nanprod`/`nancumsum`/`nancumprod` 为何只有两行实现，以及 `nanmin`/`nanmax` 为何要走「快慢两条路径」。

本讲只覆盖聚合与累积类函数。涉及除法的均值（`nanmean`）、方差（`nanvar`）、中位数（`nanmedian`）、分位数（`nanpercentile`）留给下一讲 u9-l2，它们会复用本讲建立的全部基础设施。

## 2. 前置知识

### 2.1 NaN 是什么

`NaN`（Not a Number）是 IEEE 754 浮点标准中一类特殊值，表示「未定义或不可表示的结果」，例如 `0/0`、`inf - inf`。它有两个坑：

1. **传染性**：任何与 NaN 做运算的结果都是 NaN。`1 + nan == nan`，于是 `np.sum([1, nan, 3])` 会得到 `nan`，因为累加过程中一碰到 NaN 整个和就「中毒」了。
2. **不自等**：`nan == nan` 为 `False`。所以判断 NaN 不能用 `==`，必须用 `np.isnan`。对象数组（`dtype=object`）连 `isnan` 都不支持（gh-9009），NumPy 只能用「不自等」这一性质做兜底：`np.not_equal(a, a)`。

本讲的全部函数，本质都在解决一个问题：**在聚合时让 NaN「假装不在场」**。

### 2.2 单位元（identity element）

这是理解 `nansum`/`nanprod` 的关键数学概念：

- 加法单位元是 0：\(x + 0 = x\)。把 NaN 替换成 0，求和结果不受影响。
- 乘法单位元是 1：\(x \times 1 = x\)。把 NaN 替换成 1，求积结果不受影响。

于是「忽略 NaN 求和」等价于「把 NaN 替换为 0 后求和」：

\[
\operatorname{nansum}(x) = \sum_{i:\, x_i \ne \mathrm{NaN}} x_i = \sum_{i} \tilde{x}_i,\quad
\tilde{x}_i = \begin{cases} x_i & x_i \ne \mathrm{NaN} \\ 0 & x_i = \mathrm{NaN} \end{cases}
\]

这正是 NumPy 实现的直觉。

### 2.3 既有的导入分发认知

本讲继续承接 u1-l2 建立的「dispatcher + impl 双函数」写法：每个公开函数都以 `@array_function_dispatch(_xxx_dispatcher)` 装饰，dispatcher 只返回参与运算的数组参数（供 NEP-18 `__array_function__` 协议派发），真正的逻辑在函数体里。本文件没有薄再导出模块（目录里只有 `_nanfunctions_impl.py` 与 `.pyi`，没有 `nanfunctions.py`），函数由顶层 `numpy/__init__.py` 直接取名挂到 `np.` 命名空间。

## 3. 本讲源码地图

本讲全部源码集中在一个文件里：

| 文件 | 作用 |
|------|------|
| `numpy/lib/_nanfunctions_impl.py` | 全部 NaN 感知函数的实现，以及 `_nan_mask` / `_replace_nan` / `_copyto` / `_remove_nan_1d` / `_divide_by_count` 五个私有基础设施 |
| `numpy/lib/tests/test_nanfunctions.py` | 对应测试，按 `TestNanFunctions_MinMax` / `_SumProd` / `_CumSumProd` 等类分组 |

本讲涉及的关键函数及其行号一览（全部在 `_nanfunctions_impl.py`）：

| 函数 | 行号 | 角色 |
|------|------|------|
| `_nan_mask` | L43–L68 | 返回「非 NaN」布尔掩码，整数/布尔数组短路返回 `True` |
| `_replace_nan` | L70–L112 | 把 NaN 替换为哨兵值，返回 `(新数组, 掩码)` |
| `_copyto` | L115–L141 | 兼容 numpy 标量的「按掩码写回」工具 |
| `_remove_nan_1d` | L144–L201 | 一维就地剔除 NaN（不保序），供 nanmedian/分位用 |
| `_divide_by_count` | L204–L244 | 抑制无效除法的「除以计数」工具，供 nanmean/nanvar 用 |
| `nanmin` / `nanmax` | L252–L374 / L382–L503 | 极值聚合，快慢双路径 |
| `nansum` / `nanprod` | L634–L727 / L735–L809 | 替换-聚合，两行实现 |
| `nancumsum` / `nancumprod` | L816–L878 / L885–L944 | 累积版本 |

## 4. 核心概念与源码讲解

本讲把 11 个函数分成 4 个最小模块：**替换基础设施**（4.1）、**一维剔除与除法修正**（4.2）、**替换-聚合简洁范式**（4.3）、**极值聚合双路径**（4.4）。

---

### 4.1 NaN 检测与替换三件套：_nan_mask / _replace_nan / _copyto

#### 4.1.1 概念说明

几乎所有 nan* 函数的第一步都是「把 NaN 换成一个不影响结果的哨兵值」（sum 换 0、prod 换 1、min 换 +inf）。这一步由三个小函数配合完成：

- `_nan_mask`：只回答「哪些位置是有效值（非 NaN）」，不修改数据。
- `_replace_nan`：复制数组并把 NaN 替换为给定值，**同时**回传掩码，避免调用方再算一次。
- `_copyto`：在结果阶段把 NaN **写回**（例如某条切片全是 NaN 时，最终结果该位置应为 NaN 而非哨兵），并额外处理 numpy 标量。

为什么都要先看 `dtype`？因为整数、布尔类型**根本不可能**含 NaN——NaN 只存在于浮点（`f`）和复数（`c`）里。对整数数组做 `isnan` 既无意义又浪费。这三个函数都用「类型短路」跳过这种情况。

#### 4.1.2 核心流程

`_replace_nan` 的决策树（它是三者中最核心的）：

```
输入 a, val（哨兵值）
 ├─ asanyarray(a)
 ├─ 判断掩码来源：
 │    ├─ dtype == object     → mask = not_equal(a, a)   # gh-9009 兜底
 │    ├─ inexact(浮点/复数)   → mask = isnan(a)
 │    └─ 其它(整数/布尔)      → mask = None             # 不可能含 NaN
 ├─ 若 mask is not None：
 │    a = array(a, copy=True, subok=True)              # 复制，保子类
 │    copyto(a, val, where=mask)                       # 就地把 NaN 改成 val
 └─ return a, mask
```

关键点：返回值是 **`(处理后的数组, 掩码或 None)`** 二元组。掩码为 `None` 即代表「原数组不可能含 NaN，调用方可以直接走普通函数」——后面 `nansum` 等会利用这一点。

#### 4.1.3 源码精读

`_nan_mask` 用 `dtype.kind not in 'fc'` 做短路（`'f'`=float、`'c'`=complex），对整数/布尔直接返回 Python `True`（注意不是数组），表示「处处有效」。否则 `isnan` + 原地 `invert` 得到「非 NaN」掩码，两次都用 `out=` 避免分配：

[_nanfunctions_impl.py:L43-L68](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_nanfunctions_impl.py#L43-L68) —— `dtype.kind not in 'fc'` 时短路返回 `True`；否则两次原地操作生成「非 NaN」布尔掩码。

`_replace_nan` 的三分支掩码判断。对象数组用 `not_equal(a, a)`（利用 NaN 不自等）绕开 `isnan` 不支持对象数组的问题：

[_nanfunctions_impl.py:L100-L112](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_nanfunctions_impl.py#L100-L112) —— 对象走 `not_equal(a,a)`、inexact 走 `isnan`、其余 `None`；`mask is not None` 时复制数组并就地 `copyto(a, val, where=mask)`。

`_copyto` 与标准库 `np.copyto` 的唯一差别：它能处理「numpy 标量」（0 维）。标量没有 `where` 写入语义，所以走 `a.dtype.type(val)` 直接重新转型：

[_nanfunctions_impl.py:L137-L141](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_nanfunctions_impl.py#L137-L141) —— `ndarray` 走 `copyto(..., casting='unsafe')`；标量走 `a.dtype.type(val)` 保类型。

#### 4.1.4 代码实践

**实践目标**：亲手验证「整数数组短路」与「掩码回传」两个行为。

```python
# 示例代码：直接调用私有函数观察返回
import numpy as np
from numpy.lib._nanfunctions_impl import _replace_nan, _nan_mask

# 1. 浮点数组：返回 (替换后数组, 掩码)
a = np.array([1.0, np.nan, 3.0])
b, mask = _replace_nan(a, 0)
print(b)        # [1. 0. 3.]   <- NaN 被替换为 0
print(mask)     # [ True False  True]  <- False 处曾是 NaN

# 2. 整数数组：掩码为 None，原样返回（未复制、未替换）
c = np.array([1, 2, 3])
d, mask2 = _replace_nan(c, 0)
print(mask2 is None)   # True
print(d is c)          # True：mask 为 None 时 _replace_nan 走的是 asanyarray 原路返回

# 3. _nan_mask 对整数短路返回 Python True
print(_nan_mask(np.array([1, 2, 3])))   # True（不是数组）
```

**需要观察的现象**：第 2 步中掩码为 `None`，说明 NumPy 确认了「整数不可能含 NaN」并跳过了全部处理。

**预期结果**：浮点分支得到替换数组与布尔掩码；整数分支得到 `(原数组, None)`。

> 待本地验证：第 2 步 `d is c` 的真值——取决于 `asanyarray` 是否对已是 ndarray 的输入返回同一对象。建议本地打印确认。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_replace_nan` 对 `dtype=object` 的数组要用 `np.not_equal(a, a)` 而不是 `np.isnan(a)`？

**答案**：因为 `np.isnan` 不支持对象数组（gh-9009），对象里可能装任意 Python 对象，无法用浮点.isnan 判定。利用 NaN「不自等」的性质 `nan != nan`，`not_equal(a, a)` 在 NaN 位置返回 True，是通用兜底方案。

**练习 2**：`_replace_nan` 返回的掩码是「NaN 处为 True」还是「非 NaN 处为 True」？

**答案**：是「NaN 处为 True」（直接来自 `isnan`）。这与 `_nan_mask` 相反——`_nan_mask` 做了 `invert`，返回的是「非 NaN 处为 True」。后续 `nanmean` 里会看到 `np.sum(~mask, ...)`，那个 `~` 就是把 `_replace_nan` 的掩码翻成「有效值计数」。

**练习 3**：`_copyto` 里 `casting='unsafe'` 的作用是什么？

**答案**：允许把哨兵值（如 `np.nan` 这种浮点）写回可能为其它类型的数组而不报类型错误。`unsafe` 表示「强制转换、不检查精度丢失」，因为这里调用方已确知类型兼容。

---

### 4.2 一维 NaN 剔除与除法修正：_remove_nan_1d / _divide_by_count

#### 4.2.1 概念说明

这两个函数不直接面向用户，却分别支撑了两类后续运算：

- `_remove_nan_1d`：服务于 **需要排序的统计量**（中位数 `nanmedian`、分位数 `nanpercentile`，见 u9-l2）。这些运算不在乎元素顺序（反正要排序），所以可以用一种「就地紧凑」技巧把 NaN 挤出去，**比 `arr[~isnan(arr)]` 更省拷贝**。
- `_divide_by_count`：服务于 **需要除以有效计数的统计量**（均值 `nanmean`、方差 `nanvar`，见 u9-l2）。当某条切片全是 NaN 时，计数为 0，会出现 0/0。本函数用 `errstate` 抑制由此产生的浮点警告，让结果「安静地」变成 NaN。

#### 4.2.2 核心流程

`_remove_nan_1d` 的紧凑技巧（关键：**不保序**，因为下游会排序）：

```
输入 arr1d（一维），求 s = nonzero(isnan)  # s 是 NaN 的下标
 ├─ 全是 NaN（s.size == size）：warn，返回空切片 arr1d[:0]
 ├─ 没有 NaN（s.size == 0）  ：原样返回
 └─ 否则（核心紧凑）：
      enonan = arr1d[-s.size:][~c[-s.size:]]   # 从尾部 s.size 个里挑出非 NaN
      arr1d[s[:len(enonan)]] = enonan           # 用它们覆盖开头的 NaN 位
      return arr1d[:-s.size]                    # 丢弃尾部 s.size 个，得到紧凑结果
```

直觉：数组尾部那 `s.size` 个位置注定要被丢弃，所以可以当作「草稿区」——把散落各处的非 NaN 值搬过去再切掉，避免分配新数组。返回的 `overwrite_input=True` 告知调用方「结果就是输入缓冲的一部分，可以就地改」。

`_divide_by_count` 的流程：

```
with errstate(invalid='ignore', divide='ignore'):   # 抑制 0/0 警告
   若 a 是 ndarray：
       out is None → np.divide(a, b, out=a, casting='unsafe')   # 原地除
       否则        → np.divide(a, b, out=out, casting='unsafe')
   否则（numpy 标量）：
       return a.dtype.type(a / b)   # 保类型
```

#### 4.2.3 源码精读

`_remove_nan_1d` 的核心紧凑逻辑，注意注释「select non-nans at end of array」「fill nans in beginning」——它**故意改变了元素顺序**，因此只在下游会排序的场景使用：

[_nanfunctions_impl.py:L185-L201](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_nanfunctions_impl.py#L185-L201) —— 用尾部非 NaN 覆盖开头 NaN 位，再切掉尾部，得到不含 NaN 的紧凑切片；返回 `overwrite_input=True` 表示可就地修改。它还支持 `second_arr1d`（如分位数的权重数组），按同样位置同步剔除。

全 NaN 分支的警告与空切片返回（中位数/分位数据此给出 NaN 结果）：

[_nanfunctions_impl.py:L176-L182](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_nanfunctions_impl.py#L176-L182) —— `s.size == arr1d.size` 即全是 NaN，发 `RuntimeWarning` 并返回 `arr1d[:0]`。

`_divide_by_count` 用 `errstate` 包住整个除法，这就是「全 NaN 切片求均值静默得 NaN」的原因（0/0 不报警告，直接得 NaN）：

[_nanfunctions_impl.py:L229-L244](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_nanfunctions_impl.py#L229-L244) —— `errstate` 抑制 `invalid`/`divide` 两类警告；ndarray 原地除、标量保类型。

#### 4.2.4 代码实践

**实践目标**：观察 `_remove_nan_1d` 的「不保序」行为，理解它为何只服务于排序型运算。

```python
# 示例代码
import numpy as np
from numpy.lib._nanfunctions_impl import _remove_nan_1d

arr = np.array([10.0, np.nan, 30.0, np.nan, 50.0])
# 普通「保序」剔除：
print(arr[~np.isnan(arr)])           # [10. 30. 50.]  顺序不变

# _remove_nan_1d「不保序」剔除（允许 overwrite_input，所以会动 arr，先拷贝）：
res, _, ow = _remove_nan_1d(arr.copy())
print(res)                            # [10. 50. 30.]  顺序变了！
print(ow)                             # True
```

**需要观察的现象**：`_remove_nan_1d` 的输出 `[10. 50. 30.]` 与保序版本 `[10. 30. 50.]` 元素相同但顺序不同。

**预期结果**：两个结果 `np.sort` 后一致；说明该函数专为「反正要排序」的统计量设计。

> 待本地验证：由于紧凑搬运用的是尾部元素，具体顺序取决于 NaN 的分布，建议本地多试几种分布体会搬运方向。

#### 4.2.5 小练习与答案

**练习 1**：`_remove_nan_1d` 为什么不直接 `return arr1d[~isnan(arr1d)]`？

**答案**：那样会分配一个全新的数组并拷贝。本函数通过把非 NaN 值搬到尾部「草稿区」再切片，复用输入缓冲，**减少拷贝**（docstring 明言 "Presumably faster as it incurs fewer copies"）。代价是不保序，但对排序型统计量无影响。

**练习 2**：`_divide_by_count` 为何要用 `errstate(invalid='ignore', divide='ignore')`？

**答案**：当某条切片全是 NaN 时，有效计数 `b` 为 0，出现 0/0。默认情况下 NumPy 会发 `invalid value encountered` 警告。这里故意抑制它，让结果「安静地」变成 NaN——因为对全 NaN 切片返回 NaN 正是期望行为，`nanmean` 会另外用 `cnt==0` 单独发 "Mean of empty slice" 警告。

**练习 3**：`_remove_nan_1d` 的 `second_arr1d` 参数解决什么问题？

**答案**：分位数运算支持 `weights`（权重数组）。剔除 `arr1d` 的 NaN 时，必须**同步剔除权重数组对应位置**，否则下标错位。`second_arr1d` 让两个数组按完全相同的位置一起被紧凑。

---

### 4.3 替换-聚合的简洁范式：nansum / nanprod / nancumsum / nancumprod

#### 4.3.1 概念说明

这是本讲最优雅的一组：四个函数的**实现各只有两行**。它们把「忽略 NaN」彻底转化为「替换为单位元再调普通函数」：

| 函数 | 哨兵值（单位元） | 委托给 |
|------|----------------|--------|
| `nansum` | 0（加法单位元） | `np.sum` |
| `nanprod` | 1（乘法单位元） | `np.prod` |
| `nancumsum` | 0 | `np.cumsum` |
| `nancumprod` | 1 | `np.cumprod` |

由于单位元不改变聚合结果，NaN 被替换后等同于「不在场」。这种写法的巨大优势：NaN 处理逻辑全部集中在 `_replace_nan` 里，聚合本身完全复用经过高度优化的 C 内核（`np.sum` 等）。

一个重要的边界行为：**全 NaN 切片**。`nansum` 把所有 NaN 换成 0 后求和得 0，所以全 NaN 切片返回 **0**（而非 NaN）；`nanprod` 同理返回 **1**。这与 `nanmin`/`nanmax`（返回 NaN 并报警）不同，是历史约定（docstring 注明 "In NumPy versions <= 1.9.0 Nan is returned ... In later versions zero is returned"）。

#### 4.3.2 核心流程

四个函数完全同构，以 `nansum` 为例：

```
def nansum(a, axis, dtype, out, keepdims, initial, where):
    a, mask = _replace_nan(a, 0)                 # NaN → 0
    return np.sum(a, axis, dtype, out, keepdims, initial, where)
```

`_replace_nan` 对整数数组返回 `mask=None` 且 `a` 不变，于是 `nansum` 对整数数组等价于 `np.sum`——无需特殊分支，自然短路。

#### 4.3.3 源码精读

`nansum` 的全部实现就是这两行（注意 `_replace_nan(a, 0)` 中的 0 即加法单位元）：

[_nanfunctions_impl.py:L725-L727](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_nanfunctions_impl.py#L725-L727) —— `nansum` 把 NaN 替换为 0 后委托 `np.sum`。

`nanprod` 把 NaN 替换为 1（乘法单位元）后委托 `np.prod`：

[_nanfunctions_impl.py:L807-L809](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_nanfunctions_impl.py#L807-L809) —— `nanprod` 把 NaN 替换为 1 后委托 `np.prod`。

`nancumsum` / `nancumprod` 同构，只是委托给 `np.cumsum` / `np.cumprod`：

[_nanfunctions_impl.py:L877-L878](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_nanfunctions_impl.py#L877-L878) —— `nancumsum` 把 NaN 替换为 0 后委托 `np.cumsum`，前导 NaN 变成 0，累积和从头开始。

[_nanfunctions_impl.py:L943-L944](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_nanfunctions_impl.py#L943-L944) —— `nancumprod` 把 NaN 替换为 1 后委托 `np.cumprod`。

注意 docstring 中 `nansum` 的一个微妙点：当 `+inf` 与 `-inf` 同时存在时，`inf + (-inf) = NaN`，故结果为 NaN（见 [L698-L700](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_nanfunctions_impl.py#L698-L700) 的 Notes）。

#### 4.3.4 代码实践

**实践目标**（本讲核心实践任务）：对含 NaN 的数组分别用 `nansum` 与 `sum` 求和，并验证 `nancumsum` 的累积结果。

```python
import numpy as np

a = np.array([1.0, 2.0, np.nan, 4.0])

# 1. nansum vs sum
print(np.sum(a))        # nan   <- NaN 传染，整个和中毒
print(np.nansum(a))     # 7.0   <- NaN 当 0，1+2+0+4=7

# 2. nancumsum 的累积过程
print(np.nancumsum(a))  # [1. 3. 3. 7.]
# 第 3 位原是 NaN→0，累积和不增长，停在 3.0；第 4 位加 4 得 7.0

# 3. 全 NaN 切片返回 0（不是 nan）
print(np.nansum(np.array([np.nan, np.nan])))   # 0.0

# 4. nanprod 与 nancumprod 同理
print(np.nanprod(a))    # 8.0   <- 1*2*1*4
print(np.nancumprod(a)) # [1. 2. 2. 8.]
```

**需要观察的现象**：
1. `np.sum` 因 NaN 传染得 `nan`，`np.nansum` 得 `7.0`。
2. `nancumsum` 在 NaN 位置「停滞」（累积和不增长），验证了「NaN→0」。
3. 全 NaN 切片 `nansum` 返回 `0.0`，与 `nanmin` 行为不同。

**预期结果**：如上注释所示。

**对照测试阅读**：`tests/test_nanfunctions.py` 的 `class TestNanFunctions_SumProd`（[L569](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_nanfunctions.py#L569)）和 `class TestNanFunctions_CumSumProd`（[L628](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_nanfunctions.py#L628)）系统测试了这两组函数，可与上面的结果互相对照。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `nansum` 全 NaN 切片返回 0，而 `nanmin` 全 NaN 切片返回 NaN？

**答案**：`nansum` 把 NaN 换成加法单位元 0，全 NaN 即全 0，求和自然得 0。`nanmin` 把 NaN 换成 +inf 做哨兵，全 NaN 即全 +inf，`np.amin` 得 +inf，随后代码检测到「该切片原本全是 NaN」就用 `_copyto` 把 +inf 改回 NaN 并报警。两者策略不同：sum 用单位元（无副作用），min/max 用「必败哨兵」并额外还原。

**练习 2**：`nancumsum([1, nan, 3])` 的第三个值为什么是 4 而不是 3？

**答案**：NaN 被替换为 0，于是序列变为 `[1, 0, 3]`，累积和是 `[1, 1+0, 1+0+3] = [1, 1, 4]`。注意「替换为 0」不等于「跳过」——位置仍在，只是贡献为 0，所以后续累加照常进行。

**练习 3**：如果输入是整数数组，`nansum` 会调用 `_replace_nan` 吗？效率如何？

**答案**：会调用，但 `_replace_nan` 检测到整数类型后返回 `mask=None` 且 `a` 不变（不复制、不替换），随后直接 `np.sum`。所以 `nansum` 对整数数组等价于 `np.sum`，几乎无额外开销。

---

### 4.4 极值聚合的双路径：nanmin / nanmax

#### 4.4.1 概念说明

`nanmin` / `nanmax` 不能像 sum/prod 那样简单替换单位元——因为「忽略 NaN 求最小」没有合适的有限哨兵（把 NaN 换成 0 会错误地让负数 vs 0、把 NaN 换成 ±inf 又得在事后还原全 NaN 切片）。NumPy 用了**两条路径**：

1. **快路径**：直接用 `np.fmin.reduce` / `np.fmax.reduce`。`fmin`/`fmax` 是 NumPy 的两个 ufunc，**天生就忽略 NaN**（`fmin(nan, 5) == 5`）。于是求极值连替换都不用。但它们对**对象数组**（gh-8975）和**ndarray 子类**行为不正确，所以快路径有严格准入条件。
2. **慢路径**：对子类和对象数组回退到 `_replace_nan(a, ±inf)` + 普通 `amin`/`amax`，再事后用 `_copyto` 把全 NaN 切片的结果改回 NaN。

哨兵值的选择很讲究：`nanmin` 用 `+inf`（永远不会赢得最小值比较），`nanmax` 用 `-inf`（永远不会赢得最大值比较）。这样真实值总能胜出，只有全 NaN 切片才会得到 ±inf，再被还原成 NaN。

#### 4.4.2 核心流程

```
nanmin(a, axis, ...):
  kwargs = {keepdims?, initial?, where?}            # 仅收集显式传入的
  if type(a) is ndarray/memmap 且非 object:          # 快路径准入
      res = fmin.reduce(a, axis, out, **kwargs)     # fmin 天生忽略 NaN
      if res 含 NaN: warn("All-NaN slice encountered")
  else:                                             # 慢路径（子类/对象）
      a, mask = _replace_nan(a, +inf)               # NaN → +inf（必败哨兵）
      res = amin(a, axis, out, **kwargs)
      if mask is None: return res                   # 无 NaN（如对象但无 nan）
      mask = all(mask, axis)                        # 哪些切片全是 NaN
      if any(mask):
          res = _copyto(res, nan, mask)             # 把 ±inf 改回 NaN
          warn("All-NaN axis encountered")
  return res
```

#### 4.4.3 源码精读

快路径的准入条件与 `fmin.reduce` 调用。注意 `type(a) is np.ndarray`（用 `is` 而非 `isinstance`）刻意排除子类：

[_nanfunctions_impl.py:L353-L359](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_nanfunctions_impl.py#L353-L359) —— 快路径用 `fmin.reduce`（天生忽略 NaN），若结果仍含 NaN 说明该切片全 NaN，发警告。

慢路径：`_replace_nan(a, +inf)` 后用普通 `amin`，再用 `np.all(mask, axis)` 找出全 NaN 切片并用 `_copyto` 写回 NaN：

[_nanfunctions_impl.py:L360-L373](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_nanfunctions_impl.py#L360-L373) —— 慢路径替换为 +inf、普通 amin、检测全 NaN 切片并还原。注意 `kwargs.pop("initial", None)`——`initial` 会干扰 `np.all` 的全 NaN 判定，必须先剔除。

`nanmax` 完全对称，只是 `fmin`→`fmax`、`+inf`→`-inf`、`amin`→`amax`：

[_nanfunctions_impl.py:L482-L502](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_nanfunctions_impl.py#L482-L502) —— `nanmax` 的快慢双路径，哨兵为 `-inf`。

`kwargs` 的 `_NoValue` 哨兵收集模式（区分「未传」与「显式传 False/0」）：

[_nanfunctions_impl.py:L345-L351](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_nanfunctions_impl.py#L345-L351) —— 只有当 `keepdims`/`initial`/`where` 不是默认哨兵 `np._NoValue` 时才放进 `kwargs` 透传给底层 reduce。

#### 4.4.4 代码实践

**实践目标**：观察快路径的「全 NaN 报警」与慢路径对子类的兼容。

```python
import numpy as np
import warnings

a = np.array([[1.0, 2.0], [np.nan, np.nan]])
# 1. 快路径：axis=1 的第二行全 NaN
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    res = np.nanmin(a, axis=1)
    print(res)                       # [1. nan]
    print(any("All-NaN" in str(x.message) for x in w))   # True

# 2. fmin/fmax 天生忽略 NaN 的证据
print(np.fmin(np.nan, 5))           # 5.0
print(np.fmax(np.nan, 5))           # 5.0

# 3. 慢路径：对象数组走 _replace_nan
obj = np.array([[1.0, 2.0], [np.nan, 4.0]], dtype=object)
print(np.nanmin(obj, axis=1))       # [1.0 4.0]，对象数组也能正确处理

# 4. 子类走慢路径且保留类型
class MyArr(np.ndarray): pass
m = np.eye(3).view(MyArr)
print(type(np.nanmin(m, axis=0)))   # <class '__main__.MyArr'>  类型被保留
```

**需要观察的现象**：第 1 步既得到正确结果 `[1, nan]` 又触发 "All-NaN slice encountered" 警告；第 4 步子类结果仍是 `MyArr`（慢路径用 `subok=True` 的 `_replace_nan` + `amin` 保留了类型，而快路径的 `fmin.reduce` 不保证）。

**预期结果**：如上注释。

**对照测试阅读**：`tests/test_nanfunctions.py` 的 `class TestNanFunctions_MinMax`（[L93](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_nanfunctions.py#L93)），其中 `test_allnans`（[L147](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_nanfunctions.py#L147)）专门验证全 NaN 切片报警且返回 NaN、`test_subclass`（[L173](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_nanfunctions.py#L173)）验证子类走慢路径并保留类型、`test_object_array`（[L216](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/tests/test_nanfunctions.py#L216)）验证对象数组路径——三者正好对应本节讲的三个关注点。

#### 4.4.5 小练习与答案

**练习 1**：为什么快路径用 `type(a) is np.ndarray` 而不是 `isinstance(a, np.ndarray)`？

**答案**：`isinstance` 会把 ndarray 的子类也判为 True，但 `fmin.reduce` 对子类的处理不正确（无法保证保留子类类型与 `__array_finalize__` 行为）。用 `is` 严格匹配基类，把子类排除到慢路径，慢路径用 `subok=True` 的 `_replace_nan` + `amin` 来正确保留类型。

**练习 2**：慢路径里 `kwargs.pop("initial", None)` 为什么是必要的？

**答案**：之后要用 `np.all(mask, axis, **kwargs)` 判断「哪些切片全是 NaN」。如果 `initial` 透传进去，会给 `all` 一个初始值，干扰全 NaN 判定（可能让本该判为全 NaN 的切片被 initial「救活」）。所以在做这个检测前必须把 `initial` 拿掉。

**练习 3**：`nanmin` 的哨兵为什么选 `+inf` 而不是 `-inf`？

**答案**：求最小值时，希望 NaN 不影响结果。把 NaN 换成 `+inf`（一个极大的值），它在 `amin` 比较中永远不可能赢，于是真实有限值总能胜出；只有当切片全是 NaN（全是 +inf）时结果才是 +inf，再被还原成 NaN。若换成 `-inf`，它反而会赢得最小值，把真实值覆盖，完全错误。`nanmax` 则相反，用 `-inf`。

---

## 5. 综合实践

把本讲四个模块串起来：手工用 `_replace_nan` 复刻 `nansum` 和 `nanmin`，并对比官方实现，验证你对「替换-聚合-还原」三段式的理解。

```python
import numpy as np
import warnings
from numpy.lib._nanfunctions_impl import _replace_nan, _copyto

data = np.array([
    [1.0, 2.0, np.nan],
    [np.nan, np.nan, np.nan],   # 全 NaN 行
    [4.0, np.nan, 6.0],
])

# 任务 1：手工复刻 nansum（沿 axis=1）
def my_nansum(a, axis):
    a2, mask = _replace_nan(a, 0)            # 第 1 段：替换
    return np.sum(a2, axis=axis)             # 第 2 段：聚合（全 NaN 行自然得 0）

print("nansum 对比：")
print(my_nansum(data, axis=1))               # 期望 [3. 0. 10.]
print(np.nansum(data, axis=1))               # 官方实现，应一致

# 任务 2：手工复刻 nanmin（沿 axis=1，慢路径思路）
def my_nanmin(a, axis):
    a2, mask = _replace_nan(a, +np.inf)      # 替换为必败哨兵 +inf
    res = np.amin(a2, axis=axis)             # 聚合
    allnan = np.all(mask, axis=axis)         # 第 3 段：还原——找全 NaN 切片
    res = _copyto(res, np.nan, allnan)       # 把 +inf 改回 NaN
    return res

print("nanmin 对比：")
with warnings.catch_warnings():
    warnings.simplefilter("ignore")          # 官方版会报警，我们的简化版不报
    print(my_nanmin(data, axis=1))           # 期望 [1. nan 4.]
    print(np.nanmin(data, axis=1))           # 官方实现，应一致
```

**需要观察的现象**：
1. 任务 1 中两行手工实现与 `np.nansum` 结果完全一致（含全 NaN 行返回 0）。
2. 任务 2 中手工版与 `np.nanmin` 数值一致；全 NaN 行都得 NaN。区别仅在于官方版会额外发 "All-NaN slice encountered" 警告，你的简化版没有这一步。

**预期结果**：两组对比数值完全相同。

**进阶思考**：你的 `my_nanmin` 没有报警逻辑。试着参照 4.4.3 的源码，在 `np.any(allnan)` 为真时加一句 `warnings.warn("All-NaN slice encountered", RuntimeWarning)`，让行为与官方完全对齐。

## 6. 本讲小结

- **核心范式是「替换-聚合-还原」**：`_replace_nan` 把 NaN 换成哨兵值（sum→0、prod→1、min→+inf、max→-inf），复用普通聚合函数，必要时把全 NaN 切片的结果还原为 NaN。
- **类型短路贯穿始终**：整数/布尔不可能含 NaN，`_replace_nan` 返回 `mask=None`、`_nan_mask` 返回 Python `True`，避免无谓的 isnan 计算与数组分配。
- **对象数组有专门兜底**：`isnan` 不支持 `dtype=object`（gh-9009），用 `not_equal(a, a)`（利用 NaN 不自等）替代。
- **`nansum`/`nanprod`/`nancumsum`/`nancumprod` 各只有两行**：因为单位元替换无副作用，全 NaN 切片返回单位元（0 或 1）而非 NaN。
- **`nanmin`/`nanmax` 走快慢双路径**：普通 ndarray 用天生忽略 NaN 的 `fmin`/`fmax.reduce` 快路径；子类与对象数组回退到 `_replace_nan(±inf)` + `amin`/`amax` 慢路径，并用 `_copyto` 还原全 NaN 切片。
- **`_remove_nan_1d` 与 `_divide_by_count` 是下一讲的弹药**：前者用「尾部草稿区」就地紧凑剔除 NaN（不保序，仅供排序型统计量），后者用 `errstate` 抑制 0/0 警告以静默得到 NaN。

## 7. 下一步学习建议

下一讲 **u9-l2「NaN 均值、方差与分位」** 将直接消费本讲建立的基础设施：

- `nanmean` / `nanvar` / `nanstd` 会用 `_replace_nan(a, 0)` + `np.sum(~mask)` 数有效计数，再用本讲的 `_divide_by_count` 做除法修正，处理 `_divide_by_count` 抑制 0/0 警告后「Mean of empty slice」的单独报警。
- `nanmedian` 会用本讲的 `_remove_nan_1d` 先剔除 NaN，再调普通 `median`；并区分 1D、小数组（`_nanmedian_small` 用 masked_array）、大数组（`apply_along_axis`）三条路径。
- `nanpercentile` / `nanquantile` 同样用 `_remove_nan_1d` 剔除 NaN，再委托 u7-l2 讲过的 `_quantile_unchecked`。

建议在进入 u9-l2 前，先回顾 u7-l1 的 `_ureduce` 通用归约框架与 u7-l2 的虚索引插值算法，因为 nan* 版本的中位数与分位数正是「`_remove_nan_1d` + `_ureduce` + `_quantile_unchecked`」的组合。
