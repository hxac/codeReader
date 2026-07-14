# format_parser：把 formats/names/titles 转成 dtype

## 1. 本讲目标

上一讲（u1-l3）我们用 `np.rec.fromrecords` 快速建出了第一个 record array，当时只是把 `names='id,name,val'` 这样的字符串丢进去，字段名就「自动」挂上了。但你有没有想过：NumPy 是怎么把这些「人写的格式描述」翻译成内部那套严格的 `dtype` 的？本讲就来拆开这个翻译器。

学完本讲，你应该能够：

- 理解 `format_parser` 这个类的定位——它是一个「格式描述 → `dtype`」的翻译器，结果挂在 `.dtype` 属性上；
- 说清 `formats / names / titles / aligned / byteorder` 这五个参数各自的作用；
- 跟着 `_parseFormats → _setfieldnames → _createdto` 三个内部方法，把一段格式串一步步变成带 `names/formats/offsets/titles` 的结构化 `dtype`；
- 知道 `sb.dtype`（也就是 `np.dtype`）在其中扮演的「真正干活的解析器」角色。

本讲只讲「类型描述怎么变成 `dtype`」，至于拿这个 `dtype` 去真正装数据（`fromarrays/fromrecords`）是下一讲（u2-l3）的事。

## 2. 前置知识

承接 u1-l2，你需要先记住三件事：

1. **真实实现不在 `numpy/rec/`。** `numpy/rec/__init__.py` 只是再导出垫片，`format_parser` 的物理实现全部在 `numpy/_core/records.py`。本讲所有源码行号都指向这个文件。
2. **结构化 `dtype` 由「字段列表」描述。** 一个字段是 `(名字, 子dtype, 可选标题)`，整体形如 `[('x','<f8'), ('y','<i4')]`。`dtype.names` 给字段名列表，`dtype.fields` 给「名字 → (子dtype, 字节偏移[, 标题])」的字典。
3. **`formats` 与 `dtype` 是两套写法。** `dtype` 一次性给出完整的 `(名字, 类型)`；而 `formats` 只给「每列的类型」（如 `'f8,i4,S5'`），名字交给 `names` 单独给。`format_parser` 就是把后者补全成前者的桥梁。

再统一两个口径：

| 概念 | 含义 |
|------|------|
| `formats` | 只描述「每列的类型」，不含名字，如 `['f8','i4','S5']` 或 `'f8,i4,S5'` |
| `names` | 每列的名字，逗号字符串或列表，如 `'col1,col2'` 或 `['col1','col2']` |
| `titles` | 字段的「别名/标题」，可与 `names` 并存，访问字段时两种名字都能用 |

## 3. 本讲源码地图

本讲涉及的关键文件：

- [numpy/rec/__init__.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/rec/__init__.py) — 再导出垫片，把 `format_parser` 挂到 `numpy.rec` 名下。
- `numpy/_core/records.py` — 真实实现，本讲关注其中五段：
  - `format_parser` 类（[records.py:56-193](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L56-L193)），含 `__init__` 与三个内部方法；
  - `find_duplicate` 工具函数（检测重名字段）；
  - `_byteorderconv` 映射表（字节序字符转换）；
  - `recarray.__new__` 中对 `format_parser` 的实际调用点。
- `numpy/_core/numeric.py` — 提供 `sb.dtype`（即 `np.dtype`），是真正解析类型字符串的引擎。

## 4. 核心概念与源码讲解

### 4.1 format_parser 类总览：一个「格式描述 → dtype」的翻译器

#### 4.1.1 概念说明

当我们想建一个 record array 时，最自然的写法往往是「我有一列是 8 字节浮点、一列是 4 字节整数、一列是 5 字节字符串」，也就是 `['f8','i4','S5']`。但 NumPy 内部需要一个严格的 `dtype` 对象。`format_parser` 就是中间那个翻译员：你把 `formats / names / titles` 这种「人友好」的描述交给它，它算好后把结果放在自己的 `.dtype` 属性上。

它被设计成**类**而不是函数，是因为这样既完成了转换，又顺带把中间产物（字段列表、偏移量）作为实例属性 `_f_formats / _offsets / _names / _titles` 留在对象里，便于调试。典型用法就一行：

```python
dtype = format_parser(formats, names, titles).dtype
```

注意它的 `@set_module('numpy.rec')` 装饰器：物理上代码在 `_core/records.py`，但对外显示为 `numpy.rec.format_parser`（这套路在 u1-l1 讲过）。

#### 4.1.2 核心流程

`__init__` 把翻译工作拆成三个有序步骤，每步填好一部分实例属性，最后一步把它们组装成 `dtype`：

```
format_parser(formats, names, titles, aligned, byteorder)
        │
        ▼
1) _parseFormats(formats, aligned)
   ── 把 formats 解析成「每列子dtype」列表 _f_formats
   ── 顺便拿到每列的字节偏移 _offsets 和字段数 _nfields
        │
        ▼
2) _setfieldnames(names, titles)
   ── 规整 names（逗号串→列表、缺省补 f0/f1、查重）
   ── 规整 titles（长度对齐，不足补 None）
        │
        ▼
3) _createdto(byteorder)
   ── 用 {names, formats, offsets, titles} 字典造 dtype
   ── 可选：按 byteorder 统一改字节序
        │
        ▼
   self.dtype  ← 最终成果
```

三步之间是**严格的数据依赖**：第 2 步要用第 1 步算出的 `_nfields` 来决定要补几个默认名；第 3 步要用前两步的全部结果。所以顺序不能换。

#### 4.1.3 源码精读

类声明与装饰器（注意 `@set_module('numpy.rec')`）：

- [records.py:56-57](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L56-L57) — `class format_parser:`，docstring 里写明了用法 `dtype = format_parser(formats, names, titles).dtype`。

`__init__` 极简，只做编排：

```python
def __init__(self, formats, names, titles, aligned=False, byteorder=None):
    self._parseFormats(formats, aligned)
    self._setfieldnames(names, titles)
    self._createdto(byteorder)
```

- [records.py:117-120](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L117-L120) — 五个参数原样传给三步，`aligned` 跟着第 1 步、`byteorder` 跟着第 3 步。

docstring 里给的三组示例值得记住，本讲后面会逐个复现：

- [records.py:100-113](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L100-L113) — 分别演示「带 titles」「空 titles」「空 names（默认 f0/f1）」三种输出形态。

谁在调用它？最典型的入口是 `recarray.__new__`：当你没给 `dtype`、只给 `formats/names/titles` 时，它就用 `format_parser` 现场造一个 `dtype`：

- [records.py:389-394](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L389-L394) — `descr = format_parser(formats, names, titles, aligned, byteorder).dtype`。`fromarrays/fromrecords/fromstring/fromfile` 内部也都走这一句（行号见 [records.py:636](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L636)、[records.py:1037](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L1037)）。所以搞懂 `format_parser`，就搞懂了所有「`formats` 系」构造函数的类型来源。

#### 4.1.4 代码实践

> **实践目标**：亲手构造一个 `format_parser`，确认 `.dtype` 就是一个标准结构化 dtype。
>
> **操作步骤**：
>
> ```python
> import numpy as np
>
> fp = np.rec.format_parser(
>     ['f8', 'i4', 'S5'],
>     ['col1', 'col2', 'col3'],
>     ['T1', 'T2', 'T3'],
> )
> print(fp.dtype)
> print(type(fp.dtype))
> ```
>
> **需要观察的现象**：打印出的 `dtype` 里，每个字段长成 `(('T1', 'col1'), '<f8')` 这种「`(标题, 名字)` 配子dtype」的形态；`type(fp.dtype)` 是 `numpy.dtype`。
>
> **预期结果**（小端机器上）：
>
> ```
> dtype([(('T1', 'col1'), '<f8'), (('T2', 'col2'), '<i4'), (('T3', 'col3'), 'S5')])
> <class 'numpy.dtype'>
> ```
>
> 注意 `'S5'` 没有前缀 `<`：字节串类型与字节序无关，NumPy 会省略掉 `<`/`>`。

#### 4.1.5 小练习与答案

**练习 1**：既然 `format_parser` 只产出 `dtype`、不装数据，那它和直接写 `np.dtype(...)` 相比，价值在哪？

> **答案**：它接受 `formats / names / titles` 这种**分散、人友好**的描述（格式串可以只给类型不给名字、名字可以少给靠默认补齐、还能附带标题），并自动处理「逗号串→列表」「默认名 f0/f1」「重名检测」「字节序统一」等琐事；而 `np.dtype` 要求你一次给出规范的字段列表。换句话说，`format_parser` 是 `np.dtype` 之上的一层「便捷/容错封装」。

**练习 2**：`format_parser` 的结果为什么要放在 `.dtype` 属性、而不是直接 `return`？

> **答案**：因为它是**类**，`__init__` 不能返回值。把结果挂在 `self.dtype` 上，同时把中间产物（`_f_formats`、`_offsets` 等）也留在实例里，方便调用方调试和复用。

---

### 4.2 _parseFormats：用 sb.dtype 把格式串解析成字段列表

#### 4.2.1 概念说明

`formats` 这一步要解决的问题是：「`'f8,i4,S5'` 或 `['f8','i4','S5']` 这种写法，怎么变成一列子 dtype？」

`format_parser` 的聪明之处在于：它**不自己写解析器**，而是借用 NumPy 已经很成熟的 `sb.dtype`（即 `np.dtype`）来干这件苦力活。`sb` 是模块 `numpy._core.numeric` 的别名（`from . import numeric as sb`），而 `dtype` 本身是从更底层的 `multiarray` 导入的：

- [numeric.py:35](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/numeric.py#L35) — `dtype` 在 `numeric` 模块的导入位置。`sb.dtype` 与你常用的 `np.dtype` 是同一个对象。

思路是：先用 `sb.dtype` 造一个**临时的**结构化 dtype，再从它的 `.fields` / `.names` 里把「每列子 dtype」和「字节偏移」抽出来。这样类型解析的复杂度（`'f8'`→float64、`'(2,3)f4'`→子数组、对齐填充等）全部由 `np.dtype` 买单。

#### 4.2.2 核心流程

`_parseFormats` 区分 `formats` 是「列表」还是「字符串」两条入口，但殊途同归地拿到一个带 `.fields` 的 dtype：

```
formats 是 list？
├─ 是：拼成 [(f0, fmt0), (f1, fmt1), ...] 交给 sb.dtype(aligned)
└─ 否（字符串/已有dtype）：直接 sb.dtype(formats, aligned)

拿到中间 dtype 后：
├─ dtype.fields is None？（说明是单个标量类型，如 'f8'）
│     └─ 包成单字段 [('f1', dtype)]，再造一次
└─ 否：正常多字段

从 dtype.fields 抽取：
   _f_formats = [每列的子dtype]
   _offsets   = [每列的字节偏移]
   _nfields   = 字段个数
```

关于「字节偏移」：在一个结构化 dtype 里，第 `i` 个字段并不是紧挨着上一个字段存放的——尤其当 `aligned=True` 时，编译器会按各子类型的对齐要求插入填充字节。每个字段相对于记录起点的位置就是它的 **offset**。粗略地：

\[
\text{offset}_i = \text{offset}_{i-1} + \text{itemsize}_{i-1} + \text{padding}_i
\]

其中 \(\text{padding}_i\) 在 `aligned=True` 时按 C 编译器规则补齐，`aligned=False`（默认）时通常为 0（紧密排列）。`_parseFormats` 不自己算这些，而是直接向 `sb.dtype` 要现成的 offset。

#### 4.2.3 源码精读

- [records.py:122-144](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L122-L144) — `_parseFormats` 全文。关键几行：

```python
if formats is None:
    raise ValueError("Need formats argument")
if isinstance(formats, list):
    dtype = sb.dtype(
        [(f'f{i}', format_) for i, format_ in enumerate(formats)],
        aligned,
    )
else:
    dtype = sb.dtype(formats, aligned)
fields = dtype.fields
if fields is None:
    dtype = sb.dtype([('f1', dtype)], aligned)   # 标量类型包成单字段
    fields = dtype.fields
keys = dtype.names
self._f_formats = [fields[key][0] for key in keys]   # 每列子dtype
self._offsets    = [fields[key][1] for key in keys]   # 每列偏移
self._nfields    = len(keys)
```

三个要点：

1. **list 分支用临时名 `f0,f1,...`**：因为这一步还不知道真实字段名（名字在下一步才处理），所以先用占位名造 dtype，等会儿在 `_createdto` 里再用真实 `names` 重建。占位名本身会被丢弃，真正要的只是 `fields[key][0]`（子dtype）和 `fields[key][1]`（偏移）。
2. **`fields is None` 的兜底**：如果你传的 `formats='f8'`（单个标量类型），`sb.dtype('f8')` 不会有 `.fields`（它不是结构化的），于是包成 `[('f1', dtype)]` 凑成一个单字段结构化 dtype。这就是「单列也能用」的原因。
3. **`aligned` 只在这一步生效**：对齐带来的 padding 会反映在 `_offsets` 里，并在下一步原样带进最终 dtype。

#### 4.2.4 代码实践

> **实践目标**：对比「字符串」与「列表」两种 `formats`，确认它们解析出的子 dtype 完全一致。
>
> **操作步骤**：
>
> ```python
> import numpy as np
>
> # 列表写法
> a = np.rec.format_parser(['f8', 'i4', 'S5'], ['a', 'b', 'c']).dtype
> # 逗号字符串写法
> b = np.rec.format_parser('f8,i4,S5', ['a', 'b', 'c']).dtype
>
> print(a)
> print(b)
> print(a == b)
> ```
>
> **需要观察的现象**：两种写法打印出的 `dtype` 完全相同，`a == b` 为 `True`。
>
> **预期结果**：
>
> ```
> dtype([('a', '<f8'), ('b', '<i4'), ('c', 'S5')])
> dtype([('a', '<f8'), ('b', '<i4'), ('c', 'S5')])
> True
> ```

#### 4.2.5 小练习与答案

**练习 1**：为什么 list 分支里要先造一个用 `f0/f1` 占位名的临时 dtype，而不是直接造一个无名字段列表？

> **答案**：因为 `np.dtype` 的字段**必须有名字**——你不能造一个「匿名字段」的 dtype。这一步还拿不到真实字段名（`names` 在 `_setfieldnames` 才处理），所以用临时占位名 `f0,f1,...` 先把 dtype 造出来，目的只是借用 `np.dtype` 算出每列的子 dtype 和偏移；占位名随后丢弃，真实名字在 `_createdto` 里重新组装。

**练习 2**：传 `formats='f8'`（单个标量）为什么不报错？

> **答案**：因为 `sb.dtype('f8')` 返回的是非结构化 dtype（`.fields is None`），`_parseFormats` 检测到这点后把它包成单字段 `[('f1', dtype)]` 再造一次，于是得到一个只有一列的结构化 dtype，`_nfields=1`。

---

### 4.3 _setfieldnames：字段名、标题、默认名与重复检测

#### 4.3.1 概念说明

类型（`formats`）解析完后，还要给每列「上户口」：名字叫什么、有没有别名（标题）。这一步要处理四种用户习惯：

1. `names` 既可以传逗号字符串 `'a,b,c'`，也可以传列表 `['a','b','c']`；
2. 用户**可以不给名字**（空），这时用默认名 `f0,f1,...`；
3. 用户**可以少给名字**（给 2 个但有三列），缺的用 `f2` 补；
4. 名字**不能重复**——否则字段访问会有歧义，必须报错。

标题（`titles`）是可选的「别名」，可以和名字并存。访问字段时，名字和标题都能用来取列（这一点在 u3-l2 讲属性访问时会用到）。

#### 4.3.2 核心流程

```
_setfieldnames(names, titles)
│
├─ names 非空？
│   ├─ 是 list/tuple：直接用
│   ├─ 是 str：按 ',' 分割
│   └─ 其它：抛 NameError
│   然后：_names = [n.strip() for n in names[:_nfields]]   # 截断到字段数
│         （多了的名字直接丢弃）
│
├─ names 为空：_names = []
│
├─ 补齐：_names += [f'i' for i in range(len(_names), _nfields)]
│        （不足的部分用 f{n}, f{n+1}... 补）
│
├─ 查重：find_duplicate(_names) 非空 → 抛 ValueError
│
└─ titles 同理：截断到 _nfields，不足补 None
```

注意 `_names[:self._nfields]` 这个截断：如果你给的 `names` 比字段数多，多出来的会被**静默丢弃**；少了则会用 `f{i}` 补齐。

#### 4.3.3 源码精读

- [records.py:146-180](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L146-L180) — `_setfieldnames` 全文。核心片段：

```python
if names:
    if type(names) in [list, tuple]:
        pass
    elif isinstance(names, str):
        names = names.split(',')
    else:
        raise NameError(f"illegal input names {repr(names)}")
    self._names = [n.strip() for n in names[:self._nfields]]
else:
    self._names = []

# 默认名补齐：f0, f1, ...
self._names += [f'f{i}' for i in range(len(self._names), self._nfields)]
# 查重
_dup = find_duplicate(self._names)
if _dup:
    raise ValueError(f"Duplicate field names: {_dup}")
```

查重用的是模块级工具 `find_duplicate`：

- [records.py:46-53](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L46-L53) — 用 `collections.Counter` 统计每个名字出现次数，返回出现超过一次的名字列表。这个函数也被 `numpy.rec` 公开导出（在 `__all__` 里），需要时你可以直接用。

标题处理（接在同函数后半段）：

```python
if titles:
    self._titles = [n.strip() for n in titles[:self._nfields]]
else:
    self._titles = []
    titles = []
if self._nfields > len(titles):
    self._titles += [None] * (self._nfields - len(titles))
```

即标题同样截断到 `_nfields`，不足的补 `None`（表示「这一列没有标题」）。

#### 4.3.4 代码实践

> **实践目标**：触发重名报错，再观察「空 names」时的默认命名。
>
> **操作步骤**：
>
> ```python
> import numpy as np
>
> # 1) 故意用重复字段名
> try:
>     np.rec.format_parser(['f8', 'i4'], ['x', 'x'])
> except ValueError as e:
>     print("重复名报错：", e)
>
> # 2) 不给 names，看默认命名
> fp = np.rec.format_parser(['f8', 'i4', 'S5'], [], [])
> print(fp.dtype.names)
> print(fp.dtype)
> ```
>
> **需要观察的现象**：第 1 段抛 `ValueError` 并列出重复的名字；第 2 段字段名变成 `('f0', 'f1', 'f2')`。
>
> **预期结果**：
>
> ```
> 重复名报错： Duplicate field names: ['x']
> ('f0', 'f1', 'f2')
> dtype([('f0', '<f8'), ('f1', '<i4'), ('f2', 'S5')])
> ```

#### 4.3.5 小练习与答案

**练习 1**：如果传 `names=['a','b','c','d']` 但 `formats` 只有两列，会发生什么？

> **答案**：不会报错。`_setfieldnames` 用 `names[:self._nfields]` 截断，只保留前两个 `['a','b']`，多余的 `'c','d'` 被静默丢弃。最终 dtype 只有两个字段 `a` 和 `b`。

**练习 2**：`find_duplicate(['a','b','a','c','b'])` 返回什么？顺序重要吗？

> **答案**：返回 `['a','b']`（出现超过一次的名字）。它内部用 `Counter`，返回顺序依字典插入顺序；对 `format_parser` 而言顺序不重要——只要有任何重复就抛 `ValueError`。

---

### 4.4 _createdto：组装最终的 names/formats/offsets/titles dtype

#### 4.4.1 概念说明

前三步攒齐了四样东西：字段名 `_names`、每列子 dtype `_f_formats`、每列偏移 `_offsets`、标题 `_titles`。`_createdto` 要把它们组装成一个最终的、规范的 `dtype`，并可选地统一字节序。

NumPy 的 `dtype` 构造器支持一种「字典形式」：传一个含 `names / formats / offsets / titles` 四个键的 dict，它就能按你给的偏移精确布局字段。这正好对应 `format_parser` 手里的四样东西，可谓严丝合缝。

#### 4.4.2 核心流程

```
_createdto(byteorder)
│
├─ dtype = sb.dtype({
│       'names':   _names,      # ['col1','col2',...]
│       'formats': _f_formats,  # [子dtype...]
│       'offsets': _offsets,    # [偏移...]
│       'titles':  _titles,     # ['T1','T2', None, ...]
│   })
│
├─ byteorder 非空？
│   └─ byteorder = _byteorderconv[byteorder[0]]   # 取首字符做映射
│      dtype = dtype.newbyteorder(byteorder)       # 统一改字节序
│
└─ self.dtype = dtype
```

关于 `offsets` 的复用：这里**再次**把 `_parseFormats` 算出的 offset 交给 `sb.dtype`，意味着第 1 步应用的对齐 padding 被原封不动保留——因为给了显式 `offsets`，`dtype` 构造器不会重新对齐。

#### 4.4.3 源码精读

- [records.py:182-193](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L182-L193) — `_createdto` 全文：

```python
def _createdto(self, byteorder):
    dtype = sb.dtype({
        'names': self._names,
        'formats': self._f_formats,
        'offsets': self._offsets,
        'titles': self._titles,
    })
    if byteorder is not None:
        byteorder = _byteorderconv[byteorder[0]]
        dtype = dtype.newbyteorder(byteorder)

    self.dtype = dtype
```

字节序映射表（取 `byteorder` 字符串的首字符做归一化）：

- [records.py:23-36](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L23-L36) — `_byteorderconv`。常见映射：`'b'/'B'→'>'`（大端）、`'l'/'L'→'<'`（小端）、`'n'/'N'/'='→'='`（本机）、`'|'/'I'/'i'→'|'`（不适用，如字节串）。归一化后再交给 `dtype.newbyteorder`。

拿到最终 `dtype` 后，你可以用三个标准属性复查 `format_parser` 的全部成果：

| 属性 | 含义 | 示例（`['f8','i4']`+`['a','b']`+`['T1','T2']`） |
|------|------|------|
| `dtype.names` | 字段名元组 | `('a','b')` |
| `dtype.fields` | 名字→`(子dtype, offset[, 标题])` | `{'a':(...), 'b':(...), 'T1':(...)}` |
| `dtype.itemsize` | 一条记录的总字节数 | 依对齐而定 |

注意：当字段带标题时，`dtype.fields` 里**标题和名字都是键**，都指向同一个 `(子dtype, offset)`——这就是「标题也能用来取列」的实现基础。

#### 4.4.4 代码实践

> **实践目标**：观察 `byteorder` 如何统一改写所有字段的字节序，并复查 `dtype.fields` 里标题与名字共存。
>
> **操作步骤**：
>
> ```python
> import numpy as np
>
> # 不指定 byteorder（本机序）
> d1 = np.rec.format_parser(['f8', 'i4'], ['a', 'b'], ['T1', 'T2']).dtype
> # 强制大端
> d2 = np.rec.format_parser(['f8', 'i4'], ['a', 'b'], ['T1', 'T2'],
>                           byteorder='>').dtype
>
> print("默认：", d1)
> print("大端：", d2)
> print("fields 的键：", list(d1.fields.keys()))
> ```
>
> **需要观察的现象**：`d2` 中 `f8/i4` 都带上 `>` 前缀；`d1.fields` 的键里同时出现 `'a'`、`'T1'`、`'b'`、`'T2'`。
>
> **预期结果**（小端机器上）：
>
> ```
> 默认： dtype([(('T1', 'a'), '<f8'), (('T2', 'b'), '<i4')])
> 大端： dtype([(('T1', 'a'), '>f8'), (('T2', 'b'), '>i4')])
> fields 的键： ['a', 'T1', 'b', 'T2']
> ```

#### 4.4.5 小练习与答案

**练习 1**：`byteorder='big'` 会怎样？

> **答案**：`_createdto` 只取字符串首字符，`'big'[0] == 'b'`，再经 `_byteorderconv['b'] == '>'`，最终等价于 `byteorder='>'`（大端）。所以 `'b'/'big'/'>'` 都是大端，`'l'/'little'/'<'` 都是小端。

**练习 2**：为什么 `_createdto` 要把 `_parseFormats` 算出的 `_offsets` 再传一次给 `sb.dtype`，而不是让 `sb.dtype` 自己重新布局？

> **答案**：因为对齐 padding 已经在 `_parseFormats` 里（通过 `aligned` 参数）算好并固化在 `_offsets` 中了。再次传入显式 `offsets`，`dtype` 构造器会**原样采用**这些偏移而不会重新对齐，从而保证 `aligned=True` 的布局被忠实保留到最终 dtype。

---

## 5. 综合实践

把本讲三个方法串起来，做一次「逆向复盘」：用 `format_parser` 造一个带标题、带对齐、带字节序的 dtype，然后逐项核对它内部的状态。

> **实践目标**：用一个 `format_parser` 调用，验证 `_parseFormats`（子dtype + 对齐偏移）、`_setfieldnames`（名字 + 标题 + 默认补齐）、`_createdto`（字节序统一）三步的成果都在最终 `.dtype` 上体现。
>
> **操作步骤**：
>
> ```python
> import numpy as np
>
> # 三列类型；只给两个名字（第三列应被默认补 f2）；给两个标题；开对齐；强制小端
> fp = np.rec.format_parser(
>     ['f8', 'i4', 'S5'],
>     names=['lat', 'lon'],          # 少给一个 → 第三列应为 f2
>     titles=['Latitude', 'Longitude'],
>     aligned=True,
>     byteorder='<',
> )
> d = fp.dtype
>
> print("names :", d.names)
> print("itemsize:", d.itemsize, "（aligned=True 时通常 > 8+4+5=17）")
> for name in d.names:
>     sub, off = d.fields[name][:2]
>     print(f"  {name:>4}: {str(sub):>6} @ offset {off}")
> print("标题键存在？", 'Latitude' in d.fields)
> ```
>
> **需要观察的现象**：
> 1. `d.names` 里第三列是 `f2`（默认补齐生效）；
> 2. `itemsize` 因 `aligned=True` 会有 padding，通常大于紧密排列的 17；
> 3. 各字段 offset 之间有空隙（对齐填充）；
> 4. `'Latitude'` 也能在 `d.fields` 里查到（标题与名字并存）。
>
> **预期结果**（具体 itemsize/offset 待本地验证，依平台对齐规则）：
>
> ```
> names : ('lat', 'lon', 'f2')
> itemsize: 24 （aligned=True 时通常 > 8+4+5=17）
>    lat:   <f8 @ offset 0
>    lon:   <i4 @ offset 8
>     f2:    S5 @ offset 12
> 标题键存在？ True
> ```
>
> 如果你把 `aligned=True` 改成默认的 `False`，再跑一次，应看到 `itemsize` 变小、offset 紧密排列——这就直观体现了 `_parseFormats` 中 `aligned` 参数的作用。

## 6. 本讲小结

- `format_parser` 是「人友好的 `formats/names/titles` 描述 → 严格的 `dtype`」的翻译器，结果挂在 `.dtype` 上；它被 `recarray` 和所有 `from*` 函数在「没给 `dtype`」时统一调用。
- `__init__` 把翻译拆成三步：`_parseFormats`（解析类型）→ `_setfieldnames`（规整名字/标题）→ `_createdto`（组装最终 dtype），顺序有严格数据依赖。
- `_parseFormats` 不自己写解析器，而是借用 `sb.dtype`（即 `np.dtype`）造一个临时 dtype，再从中抽取每列子 dtype、偏移和字段数；`aligned` 的对齐 padding 在此固化。
- `_setfieldnames` 处理逗号串/列表两种 `names`、缺省补 `f0/f1`、`find_duplicate` 查重，标题不足补 `None`。
- `_createdto` 用 `sb.dtype` 的「字典形式」（`names/formats/offsets/titles`）精确组装，并用 `_byteorderconv` 表 + `newbyteorder` 统一字节序。
- 带标题时，`dtype.fields` 里名字和标题都是键，都能用来取列——这是下一讲属性访问的基础。

## 7. 下一步学习建议

本讲只产出了 `dtype`，还没装数据。建议接下来：

- **u2-l2（字段命名、标题、重复检测）**：从 `_setfieldnames` / `find_duplicate` / `_byteorderconv` 的角度再深入，覆盖更多边界（非法 names 类型、标题与名字同名等）。
- **u2-l3（fromarrays）**：看 `format_parser` 产出的 dtype 如何被用来把「列方向」的数组列表装进一个 record array。
- 想直接看 `format_parser` 的真实调用现场，可跳读 [records.py:389-394](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L389-L394)（`recarray.__new__`）、[records.py:636](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L636)（`fromarrays`）与 [records.py:1037](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L1037)（`array` 统一调度函数）。
