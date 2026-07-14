# 结构化数组与 record array 的区别

## 1. 本讲目标

学完本讲，你应当能够：

- 说清「结构化 dtype」和「字段（field）」是什么，会用 `arr['x']` 这种**字典式**写法访问普通结构化数组的某一列。
- 区分**普通结构化数组**（`arr['x']`）与 **record array**（`arr.x` 属性式访问）——两者数据完全一样，只是访问语法不同。
- 认识 `record` 标量类型：取出数组里的**单条记录**时，普通结构化数组给你的是 `numpy.void`，而 record array 给你的是 `numpy.record`，后者也能用属性访问字段。
- 理解 `arr.view(np.recarray)` 为什么**不拷贝数据**就能让 `arr.x` 生效，以及幕后 `__array_finalize__` 做了什么。
- 知道 `record` 与 `nt.void` 的父子关系，能在源码里定位这两个类的定义位置。

本讲承接上一篇（u1-l1）建立的代码地图——「读 `numpy.rec` 的任何行为都去 `numpy/_core/records.py` 找」。本篇只建立**直觉**：先把「字段」「结构化数组」「record array」「record 标量」这四个概念的关系讲透，**还不深入** `__getattribute__` 的实现细节（那是进阶篇 u3-l2 的任务）。

---

## 2. 前置知识

### 2.1 dtype 是什么（复习）

上一篇提到，普通 NumPy 数组里每个元素都是同一种类型，比如全 `float64`。`dtype` 就是描述「元素类型」的对象，例如 `np.dtype('f8')` 表示 8 字节浮点。

但真实数据常像一张表，一行里有「姓名（字符串）、年龄（整数）、身高（浮点）」等**不同类型的列**。要表达这种「一个元素里塞了多个不同类型字段」的数据，就需要**结构化 dtype**。

### 2.2 类比：C 语言的 struct

如果你接触过 C 语言，结构化 dtype 就像一个 `struct`：

```c
struct Point {
    double x;   // 8 字节
    int    y;   // 4 字节
};
```

NumPy 的结构化 dtype `[('x', 'f8'), ('y', 'i4')]` 几乎就是同一回事：一个元素由若干**字段**拼成，每个字段有自己的**名字**和**子类型**，并占据内存里一段固定的**字节偏移（offset）**。

### 2.3 Python 的属性访问 vs 字典访问

- 字典式：`d['x']`——用方括号 + 字符串键取值。
- 属性式：`obj.x`——用点号取属性。

普通结构化数组走的是**字典式** `arr['x']`；record array 的卖点就是改成**属性式** `arr.x`。本讲的核心就是讲清「这俩到底差在哪、数据有没有变」。

### 2.4 子类继承（subclass）

Python 里 `class B(A)` 表示 B 继承 A，B 自动拥有 A 的全部能力，还可以**改写**其中一部分行为。后面你会看到：

- `recarray` 继承 `ndarray`，几乎和普通数组一模一样，只是改写了「属性访问」。
- `record` 继承 `nt.void`（即 `numpy.void`），只是给标量加上了「属性访问」。

理解了「子类 = 父类能力 + 少量改写」，本讲的两个核心类就很好懂了。

---

## 3. 本讲源码地图

本讲只读 2 个文件，重点是「类的定义与它们的关系」，不涉及复杂算法：

| 文件 | 关键位置 | 作用 |
| --- | --- | --- |
| `numpy/_core/records.py` | `record` 类（196 行起） | 结构化**标量**类型，让单条记录支持 `rec.x` |
| `numpy/_core/records.py` | `recarray` 类（279 行起） | 结构化**数组**子类，让整列字段支持 `arr.x` |
| `numpy/_core/records.py` | `format_parser._parseFormats` / `_createdto`（122、182 行起） | 用来观察「一个字段由哪些信息组成」 |
| `numpy/_core/numerictypes.py` | 541–544 行 | `void` 标量类型的注册处（`record` 的父类） |

> 提醒：本讲目录是 `numpy/rec/`，但实现都在 `numpy/_core/records.py` 和 `numpy/_core/numerictypes.py`（上一篇已建立这条心智地图）。

---

## 4. 核心概念与源码讲解

本讲拆成 3 个最小模块：

- **4.1 结构化 dtype 与字段（fields）**——字段是什么、怎么用 `arr['x']` 访问。
- **4.2 record 标量类型**——单条记录的属性访问，以及它与 `numpy.void` 的关系。
- **4.3 recarray：属性访问的 ndarray 子类**——为什么 `view(recarray)` 能让 `arr.x` 生效。

---

### 4.1 结构化 dtype 与字段（fields）

#### 4.1.1 概念说明

「字段（field）」是结构化数组里「一列」的抽象。一个结构化 dtype 由若干字段组成，每个字段本质上是 4 元信息：

| 组成 | 含义 | 例子 |
| --- | --- | --- |
| 名字（name） | 字段名，访问时的键 | `'x'` |
| 格式（format） | 这一列的子 dtype | `float64` |
| 偏移（offset） | 该字段在每条记录里的字节起始位置 | `0` |
| 标题（title，可选） | 字段的别名 | `'x_coordinate'` |

有了结构化 dtype，就可以造**普通结构化数组**。访问某一列字段，用的是**字典式**写法：

```python
arr['x']   # 取出所有行的 x 列，得到一个普通 ndarray
```

这是 NumPy 普通 `ndarray` **本来就支持**的能力——不需要 record array，标准数组就能按字段名取列。

#### 4.1.2 核心流程

一个结构化 dtype 的构造与访问流程：

```
传入描述: [('x','f8'), ('y','i4')]
        │
        ▼
组装成 dtype（含 names / formats / offsets / titles）
        │
        ▼
dtype.names  -> ('x', 'y')                 # 有序字段名
dtype.fields -> {'x': (float64, 0),        # 子dtype + 字节偏移
                 'y': (int32,   8)}
        │
        ▼
arr = np.zeros(3, dtype=这个dtype)          # 普通结构化数组
arr['x']   -> array of float64             # 字典式取列
```

注意 `dtype.fields` 这个映射的值是 `(子dtype, 字节偏移)` 这样的小元组——这正是字段「4 元信息」里最关键的两项。

#### 4.1.3 源码精读

字段到底由哪些信息组成？实现文件里 `format_parser` 在解析格式时，正是从 `dtype.fields` 和 `dtype.names` 里把每个字段的「子 dtype」和「偏移」读出来，证明字段就是「名字 + 子dtype + 偏移」的组合：

[numpy/_core/records.py:137-144](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L137-L144) —— 逐字段读出 `fields[key][0]`（子 dtype）与 `fields[key][1]`（字节偏移），这就是一个字段的核心两要素。

```python
fields = dtype.fields
if fields is None:
    dtype = sb.dtype([('f1', dtype)], aligned)
    fields = dtype.fields
keys = dtype.names
self._f_formats = [fields[key][0] for key in keys]   # 每个字段的子 dtype
self._offsets   = [fields[key][1] for key in keys]    # 每个字段的字节偏移
self._nfields   = len(keys)
```

而反向「拼装」一个结构化 dtype 时，传的正是 `names / formats / offsets / titles` 这一组键——对应字段的 4 元信息：

[numpy/_core/records.py:182-193](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L182-L193) —— 用 `names/formats/offsets/titles` 四件套拼出一个结构化 dtype，正好印证「字段 = 名字 + 格式 + 偏移 + 标题」。

```python
def _createdto(self, byteorder):
    dtype = sb.dtype({
        'names': self._names,
        'formats': self._f_formats,
        'offsets': self._offsets,
        'titles': self._titles,
    })
    ...
    self.dtype = dtype
```

> 这两段只是为了让你看清「字段的内部结构」，`format_parser` 本身的参数细节会在进阶篇 u2-l1 专门讲。

#### 4.1.4 代码实践

1. **实践目标**：亲手造一个结构化 dtype，观察 `names` / `fields`，并用 `arr['x']` 取列。
2. **操作步骤**：

   ```python
   import numpy as np

   dt = np.dtype([('x', 'f8'), ('y', 'i4')])   # 结构化 dtype
   print("names :", dt.names)
   print("fields:", dt.fields)

   arr = np.array([(1.0, 2), (3.0, 4)], dtype=dt)   # 普通结构化数组
   print("arr['x'] :", arr['x'])
   print("arr['y'] :", arr['y'])
   print("type(arr['x']) :", type(arr['x']))
   ```
3. **需要观察的现象**：
   - `dt.names` 是字段名元组 `('x', 'y')`。
   - `dt.fields` 是个映射，每个字段对应 `(子dtype, 字节偏移)`。
   - `arr['x']` 返回一个**普通 `ndarray`**（不是 record array）。
4. **预期结果**（依据源码，待本地验证）：

   ```text
   names : ('x', 'y')
   fields: {'x': (dtype('float64'), 0), 'y': (dtype('int32'), 8)}
   arr['x'] : [1. 3.]
   arr['y'] : [2 4]
   type(arr['x']) : <class 'numpy.ndarray'>
   ```

#### 4.1.5 小练习与答案

**练习 1**：上面 `dt.fields['y']` 得到的元组里，第二个数字 `8` 代表什么？

> **答案**：字段 `y` 在每条记录中的**字节偏移**。因为 `x` 是 `f8`（8 字节），所以 `y` 从第 8 个字节开始。

**练习 2**：`arr['x']` 返回的对象是 record array 吗？

> **答案**：不是。它是一个普通的 `numpy.ndarray`（只是 dtype 变成了 `float64`）。普通结构化数组本身就能用 `arr['x']` 取列，这是 `ndarray` 的原生能力，与 record array 无关。

---

### 4.2 record 标量类型：单条记录的属性访问

#### 4.2.1 概念说明

上一节讲的是「取**整列**」（`arr['x']` 得到所有行的 x）。那如果只取**某一行**呢？比如 `arr[0]`——这会得到一个**标量**，它代表「一条完整的记录」。

这里有个关键区别：

| 数组类型 | 取一条记录 `arr[0]` 得到的标量类型 | 能否 `arr[0].x` 属性访问 |
| --- | --- | --- |
| 普通结构化数组 | `numpy.void` | 否（只能 `arr[0]['x']`） |
| record array | `numpy.record` | **能** |

也就是说：

- `numpy.void` 是 NumPy 里**结构化标量**的基础类型（「一条记录」的默认形态）。
- `numpy.record` 是 `void` 的**子类**，额外支持「用属性访问字段」。

`record` 就是「标量版」的 record array：record array 解决**整列**的属性访问，record 解决**单条记录**的属性访问。

#### 4.2.2 核心流程

`record` 与 `void` 的关系，以及取标量时发生了什么：

```
numpy.void                  结构化标量基类（每条记录的默认类型）
    │  继承
    ▼
numpy.record                加了「属性访问字段」能力的标量
    │
    ▼
rec[0].x   →  通过 __getattribute__ 在 dtype.fields 里查 'x'
              再用 getfield 取出该字段的值
```

要点：

1. 普通结构化数组的元素类型是 `void`，`arr[0]` 是 `void` 标量，只能用 `arr[0]['x']`。
2. record array 的元素类型被改成了 `record`，`r[0]` 是 `record` 标量，可以用 `r[0].x`。
3. `record` 通过改写 `__getattribute__`，把 `rec.x` 翻译成「在 `dtype.fields` 里找 `x`，再取它的值」。

#### 4.2.3 源码精读

`record` 类的定义——它直接继承 `nt.void`，注释也说明了它的作用：

[numpy/_core/records.py:196-203](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L196-L203) —— `record(nt.void)`：结构化标量类型，让字段可以像属性一样被访问；并手动把名字/模块改成 `numpy.record`。

```python
class record(nt.void):
    """A data-type scalar that allows field access as attribute lookup.
    """

    # manually set name and module so that this class's type shows up
    # as numpy.record when printed
    __name__ = 'record'
    __module__ = 'numpy'
```

这里的 `nt.void` 就是 `numpy.void`。它从哪来？`records.py` 顶部把同包的 `numerictypes` 取了别名 `nt`：

[numpy/_core/records.py:9-11](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L9-L11) —— `nt` 是 `numerictypes` 的别名，`nt.void` 即 `numpy.void`。

```python
from numpy._utils import set_module

from . import numeric as sb, numerictypes as nt
```

而 `void` 标量类型是在 `numerictypes.py` 里被注册进模块命名空间的（`allTypes` 里汇集了所有标量类型，循环注入到模块全局变量）：

[numpy/_core/numerictypes.py:541-544](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/numerictypes.py#L541-L544) —— 把所有标量类型（含 `void`）注入到 `numerictypes` 模块，于是 `nt.void` 可用，`record` 正是继承自它。

```python
# Now add the types we've determined to this module
for key in allTypes:
    globals()[key] = allTypes[key]
    __all__.append(key)
```

`record` 改写属性访问的方法是 `__getattribute__`（查 `dtype.fields`，再用 `getfield` 取值）。本篇只点明它的存在，**实现细节留到进阶篇 u3-l2**：

[numpy/_core/records.py:215-237](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L215-L237) —— `record.__getattribute__`：先按普通属性找，找不到再去 `dtype.fields` 里按字段名取值，从而实现 `rec.x`。

#### 4.2.4 代码实践

1. **实践目标**：对比「普通结构化数组」与「record array」取单条记录时的标量类型差异。
2. **操作步骤**：

   ```python
   import numpy as np

   dt = [('x', 'f8'), ('y', 'i4')]

   arr = np.array([(1.0, 2), (3.0, 4)], dtype=dt)   # 普通结构化数组
   print("普通数组 arr[0] 的类型 :", type(arr[0]))
   print("普通数组取字段        :", arr[0]['x'])     # 只能用字典式

   r = arr.view(np.recarray)                          # 转成 record array
   print("record  r[0] 的类型   :", type(r[0]))
   print("record  取字段        :", r[0].x)          # 可以用属性式
   print("record  也能字典式    :", r[0]['x'])
   ```
3. **需要观察的现象**：
   - 普通数组的 `arr[0]` 是 `numpy.void`。
   - record array 的 `r[0]` 是 `numpy.record`。
   - `r[0].x` 和 `r[0]['x']` 都能取到字段值 `1.0`。
4. **预期结果**（依据源码，待本地验证）：

   ```text
   普通数组 arr[0] 的类型 : <class 'numpy.void'>
   普通数组取字段        : 1.0
   record  r[0] 的类型   : <class 'numpy.record'>
   record  取字段        : 1.0
   record  也能字典式    : 1.0
   ```

#### 4.2.5 小练习与答案

**练习 1**：`numpy.record` 和 `numpy.void` 是什么关系？

> **答案**：`numpy.record` 是 `numpy.void` 的**子类**（源码 `class record(nt.void)`）。`void` 是结构化标量的基础类型，`record` 在它之上增加了「属性访问字段」的能力。

**练习 2**：对一个**普通结构化数组**，`arr[0].x`（属性式）能成功吗？

> **答案**：不能。普通结构化数组的元素是 `void` 标量，没有为字段做属性访问的改写，所以 `arr[0].x` 会抛 `AttributeError`；只能用 `arr[0]['x']`。要支持属性访问，需先 `arr.view(np.recarray)`。

---

### 4.3 recarray：让整列字段支持属性访问的 ndarray 子类

#### 4.3.1 概念说明

`recarray` 是 `ndarray` 的子类。源码里有一句注释，把它的定位说得很直白：recarray 与标准数组「几乎一样」，最大的区别只有两点——**可以用属性查找字段**，并且**用 `record` 来构造**。

| 维度 | 普通结构化数组（`ndarray`） | record array（`recarray`） |
| --- | --- | --- |
| 取整列 | `arr['x']` | `r.x`（也可 `r['x']`） |
| 取一条记录的类型 | `numpy.void` | `numpy.record` |
| 底层数据 | —— | **完全相同**（view 不拷贝） |

最关键的一点：**`arr.view(np.recarray)` 不会拷贝数据**。它只是给同一块内存换了个「视图类型」，于是访问语法从 `arr['x']` 变成了 `arr.x`，取出的列结果却是一样的普通 `ndarray`。

#### 4.3.2 核心流程

`view(recarray)` 之所以能「无副作用」地改变访问方式，是因为 NumPy 在视图转换时会调用 `__array_finalize__` 这个钩子：

```
arr = np.array(..., dtype=[('x','f8'),('y','i4')])   # 元素类型 = void
        │
        │  arr.view(np.recarray)
        ▼
触发 recarray.__array_finalize__：
  检测到 dtype.type 是 void 的子类、且有字段(names 非空)
        │
        ▼
把 dtype 改成 (record, 原 void dtype)   # 元素类型 void → record
        │
        ▼
r.x  →  经 recarray.__getattribute__ 映射到字段 'x'  （细节见 u3-l2）
r[0] →  元素类型现在是 record，所以 r[0].x 也能用
```

注意那个「二元 dtype」`(record, descr)`：它的意思是「在原有结构化 dtype 的基础上，把标量类型从 `void` 换成 `record`」。这一步是「数组级属性访问」和「标量级属性访问」能同时生效的根源。

#### 4.3.3 源码精读

先看那句点明 recarray 定位的注释：

[numpy/_core/records.py:269-272](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L269-L272) —— 注释：recarray 与支持字段的标准数组几乎一样，区别在于「属性查找字段」且「用 record 构造」。

```python
# The recarray is almost identical to a standard array (which supports
#   named fields already)  The biggest difference is that it can use
#   attribute-lookup to find the fields and it is constructed using
#   a record.
```

再看 `recarray` 类的定义与它的 docstring——这段 docstring 本身就是本讲最好的总结，它直接对比了 `arr['x']` 与 `arr.x`：

[numpy/_core/records.py:278-287](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L278-L287) —— `recarray(ndarray)`：构造一个允许「属性访问字段」的数组；docstring 明确对比了 `arr['x']`（字典式）与 `arr.x`（属性式）。

```python
@set_module("numpy.rec")
class recarray(ndarray):
    """Construct an ndarray that allows field access using attributes.

    Arrays may have a data-types containing fields, analogous
    to columns in a spread sheet.  An example is ``[(x, int), (y, float)]``,
    where each entry in the array is a pair of ``(int, float)``.  Normally,
    these attributes are accessed using dictionary lookups such as ``arr['x']``
    and ``arr['y']``.  Record arrays allow the fields to be accessed as members
    of the array, using ``arr.x`` and ``arr.y``.
```

`view(recarray)` 幕后改写元素类型的地方，就是 `__array_finalize__`：只要发现当前 dtype 是「带字段的 void 类型」且还不是 `record`，就把它升级成 `record`：

[numpy/_core/records.py:407-413](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L407-L413) —— `__array_finalize__`：视图转换时把 `void` 结构化类型自动提升为 `record`，使数组级与标量级属性访问都生效。

```python
def __array_finalize__(self, obj):
    if (self.dtype.type is not record and
            issubclass(self.dtype.type, nt.void) and
            self.dtype.names is not None):
        # if self.dtype is not np.record, invoke __setattr__ which will
        # convert it to a record if it is a void dtype.
        ndarray._set_dtype(self, sb.dtype((record, self.dtype)))
```

最后，docstring 里还自带了一个对照示例，把「`x['x']` → `x.x`」完整演示了一遍（这正是本讲实践任务的官方出处）：

[numpy/_core/records.py:357-373](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L357-L373) —— docstring 示例：先 `x['x']` 取列，再 `x.view(np.recarray)` 后用 `x.x` 取列，结果一致。

```python
>>> x = np.array([(1.0, 2), (3.0, 4)], dtype=[('x', '<f8'), ('y', '<i8')])
>>> x['x']
array([1., 3.])

>>> x = x.view(np.recarray)

>>> x.x
array([1., 3.])

>>> x.y
array([2, 4])
```

#### 4.3.4 代码实践

1. **实践目标**：用同一个结构化数组，对比 `arr['x']`（字典式）与 `arr.view(np.recarray)` 后的 `arr.x`（属性式），确认「数据相同、只是语法不同」。
2. **操作步骤**：

   ```python
   import numpy as np

   arr = np.array([(1.0, 2), (3.0, 4)], dtype=[('x', 'f8'), ('y', 'i4')])

   # (1) 字典式访问（普通结构化数组）
   print("arr['x']        :", arr['x'])

   # (2) 转成 record array（不拷贝数据）
   r = arr.view(np.recarray)
   print("r.x (属性式)    :", r.x)
   print("r.y (属性式)    :", r.y)

   # (3) 验证数据共享 + 元素类型
   arr['x'][0] = 99.0                 # 改原数组的 x 列
   print("改后 r.x        :", r.x)    # r 也会变 → 说明没有拷贝
   print("r.dtype.type    :", r.dtype.type)
   ```
3. **需要观察的现象**：
   - `arr['x']` 与 `r.x` 取出的列内容相同。
   - 修改 `arr['x']` 后 `r.x` 也跟着变 → 证明 `view` 共享同一块内存、**没有拷贝**。
   - `r.dtype.type` 是 `numpy.record`（被 `__array_finalize__` 改写过）。
4. **预期结果**（依据源码，待本地验证）：

   ```text
   arr['x']        : [1. 3.]
   r.x (属性式)    : [1. 3.]
   r.y (属性式)    : [2 4]
   改后 r.x        : [99.  3.]
   r.dtype.type    : <class 'numpy.record'>
   ```

#### 4.3.5 小练习与答案

**练习 1**：`arr.view(np.recarray)` 之后，`r.x` 和原来的 `arr['x']` 返回的对象是不是同一份列数据？

> **答案**：它们都返回基于同一块内存的视图。事实上 `r.x` 内部最终也会 `.view(ndarray)` 返回一个普通 ndarray（见 `recarray.__getattribute__` 末尾的 `obj.view(ndarray)`），所以从「取列」角度看，两者结果等价。当你改原数组 `x` 列时，`r.x` 也会反映出来，说明底层内存共享。

**练习 2**：`recarray` 与普通 `ndarray` 的关系是什么？它「多」出了什么能力？

> **答案**：`recarray` 是 `ndarray` 的子类（源码 `class recarray(ndarray)`）。普通 `ndarray` 本就支持字段（`arr['x']`）；`recarray` 额外提供了「属性查找字段（`arr.x`）」并把元素类型升级为 `numpy.record`（由 `__array_finalize__` 完成），使标量也能属性访问。数据结构与普通结构化数组一致。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个**对比小实验**：用同一组数据，分别走「普通结构化数组」和「record array」两条路，逐项对比。

操作步骤：

```python
import numpy as np

dtype = [('id', 'i4'), ('temp', 'f8')]
data = [(1, 36.5), (2, 37.0), (3, 36.8)]

# === 路线 A：普通结构化数组 ===
a = np.array(data, dtype=dtype)
print("A 类型         :", type(a).__name__)
print("A 元素类型     :", a.dtype.type.__name__)
print("A 取列 a['id'] :", a['id'])
print("A 取记录 a[0]  :", a[0], "类型:", type(a[0]).__name__)
# a[0].id   # ← 取消注释会抛 AttributeError（void 不支持属性访问）

# === 路线 B：record array ===
r = a.view(np.recarray)
print("B 类型         :", type(r).__name__)
print("B 元素类型     :", r.dtype.type.__name__)
print("B 取列 r.id    :", r.id)
print("B 取记录 r[0]  :", r[0], "类型:", type(r[0]).__name__)
print("B 标量属性     :", r[0].id, r[0].temp)
print("B 数据共享?    :", np.shares_memory(a, r))
```

阅读这段输出后，用一两句话回答：

- 路线 A 和路线 B 的「取列结果」是否相同？为什么？
- `a[0]` 和 `r[0]` 的类型分别是什么？哪一种支持 `.id` 属性访问？
- `np.shares_memory(a, r)` 是 `True` 还是 `False`？这说明 `view` 做了什么？

**预期结果**（依据源码，待本地验证）：

- 取列结果相同（都是 `[1 2 3]` 与 `[36.5 37.  36.8]`），因为 record array 只是换了访问语法，数据没变。
- `a[0]` 是 `void`，不支持 `.id`；`r[0]` 是 `record`，支持 `r[0].id`。
- `np.shares_memory(a, r)` 为 `True`，说明 `view(recarray)` **不拷贝数据**，只换视图类型。

---

## 6. 本讲小结

- **字段（field）**是结构化数组的一「列」，由**名字 + 子dtype + 字节偏移（+ 可选标题）**组成；`dtype.names` 给字段名，`dtype.fields` 给 `(子dtype, 偏移)`。
- 普通结构化数组（`ndarray`）本来就支持**字典式**取列 `arr['x']`，这是 `ndarray` 原生能力，不需要 record array。
- 取一条记录时，普通结构化数组给的是 `numpy.void` 标量；record array 给的是 `numpy.record` 标量。`record` 是 `nt.void` 的子类，多了属性访问能力。
- `recarray` 是 `ndarray` 的子类，源码注释说它与标准数组「几乎一样」，区别只在于**属性查找字段**并**用 record 构造**。
- `arr.view(np.recarray)` **不拷贝数据**，只换视图类型；`__array_finalize__` 会把 `void` 结构化 dtype 自动提升为 `record`，使数组级（`r.x`）与标量级（`r[0].x`）属性访问都生效。

---

## 7. 下一步学习建议

- **下一篇（u1-l3）**：动手用 `np.rec.array` / `np.rec.fromrecords` 真正「从数据」造出你的第一个 record array，而不只是 `view` 转换。
- **进阶篇（u3-l1 / u3-l2）**：本讲刻意没展开 `recarray.__new__` 构造细节与 `__getattribute__` 的字段查找实现，到那里会逐行拆解「`r.x` 到底是怎么映射到字段的」。
- 想加深印象，可以再翻一眼 `numpy/_core/records.py` 里 `recarray` 的 docstring 示例（357–373 行）和 `record` 类（196 行起），对照本讲的对比表自己复述一遍两者的关系。
