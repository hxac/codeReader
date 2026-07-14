# 掩码记录：mrecords 与字段级屏蔽

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚「整行屏蔽（record mask）」和「字段级屏蔽（field mask）」的区别，理解为什么屏蔽一个字段不等于屏蔽整条记录。
- 掌握 `MaskedRecords`（别名 `mrecarray`）的三种构造方式：`fromarrays`、`fromrecords`、`fromtextfile`。
- 理解保留字段名机制 `reserved_fields` / `_checknames`，知道为什么字段不能取名为 `_mask`、`dtype` 等。
- 会用 `addfield` 给已有的掩码记录数组追加一个新字段（连同它的屏蔽信息）。
- 看懂 `mrecords.py` 这一整份独立模块的关键源码，并能在阅读时区分「文档字符串描述的设计意图」与「代码实际实现」。

---

## 2. 前置知识

本讲是专家层讲义，承接两讲的内容：

- **u2-l1 掩码的内部表示与构造**：我们讲到结构化 dtype 的屏蔽描述靠 `make_mask_descr`，它把 dtype 的每个字段递归替换成布尔类型；`make_mask_none(shape, dtype)` 则生成一个与该结构匹配、全 `False` 的屏蔽数组；`nomask` 是表示「无屏蔽」的省内存单例。本讲会反复用到这三个工具。
- **u3-l2 子类化 MaskedArray 与 mvoid**：我们讲到 `MaskedArray` 是 `ndarray` 的子类，结构化 dtype 的掩码数组取单条记录 `a[i]` 时返回 `mvoid`（一个 0 维 `MaskedArray` 子类，其 `_mask` 是逐字段布尔的 `np.void`）。本讲的 `MaskedRecords` 正是建立在「结构化 dtype + 逐字段布尔掩码」之上。

另外还需要一点 NumPy 基础概念（不熟悉的话先查文档）：

- **结构化数组（structured array）**：dtype 带有命名字段，例如 `[('a', int), ('b', float)]`，每条「记录」是一个由多字段组成的元组。
- **记录数组（record array，`np.recarray`）**：结构化数组的子类，允许把字段名当属性访问，如 `r.a` 而不只是 `r['a']`。

一句话承上启下：`MaskedArray` 本身已经支持结构化 dtype 和「逐字段屏蔽」（这正是 u2-l1 讲的 `make_mask_descr` 的产物）；`mrecords.py` 提供的 `MaskedRecords`（`mrecarray`）则在此基础上加了一层「把字段当属性访问 + 专门构造器 + 保留字段名保护」的便利外壳。**它不是屏蔽能力的来源，而是屏蔽能力在「记录」场景下的 ergonomic 封装。**

---

## 3. 本讲源码地图

本讲只涉及一个源码文件，外加 `core.py` 中两个被它复用的工具：

| 文件 | 作用 |
| --- | --- |
| [mrecords.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/mrecords.py) | 定义 `MaskedRecords`/`mrecarray`、构造器 `fromarrays`/`fromrecords`/`fromtextfile`、`addfield`、保留字段名保护 `_checknames`/`reserved_fields`。整个文件约 740 行，是一个自包含模块。 |
| core.py（复用） | `make_mask_descr`、`make_mask_none` 生成结构化布尔掩码（u2-l1 已讲）；`MaskedArray.recordmask` property 在此定义，`MaskedRecords` 通过继承直接获得。 |

注意（承接 u1-l2）：`numpy.ma.__init__.py` 只 re-export 了 `core` 与 `extras`，**`mrecords` 不会被自动导入**。所以使用本讲的类与函数，必须显式 `import numpy.ma.mrecords`（或 `from numpy.ma import mrecords`），直接写 `np.ma.mrecarray` 在某些环境下可能取不到。下文示例统一用 `import numpy.ma.mrecords as mr`。

---

## 4. 核心概念与源码讲解

### 4.1 MaskedRecords / mrecarray：可按字段屏蔽的记录数组

#### 4.1.1 概念说明

假设你在做气象观测表，每条记录有 `温度`、`湿度`、`气压` 三个字段。某次观测的温度计坏了，你只想屏蔽「这一条记录的温度字段」，而不是把整条记录都丢掉——因为湿度、气压仍是有效的。

普通 `MaskedArray` 对结构化 dtype 虽然也支持逐字段屏蔽（`_mask` 是结构化布尔数组），但：

- 字段必须用 `a['温度']` 这种下标语法访问，不能写成 `a.温度`；
- 没有专门的「从多个数组拼装」「从文本文件读取」这类记录场景的构造器。

`MaskedRecords`（类）=`mrecarray`（别名）就是为了补这两点而存在的：它继承自 `ma.MaskedArray`，把底层数据视图成 `np.recarray`，从而允许属性式字段访问；并提供一族构造器。先看类与别名的定义位置：

- 类定义：[mrecords.py:76-L92](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/mrecords.py#L76-L92) —— 注意类文档字符串里对四个属性的描述。
- 别名：[mrecords.py:463](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/mrecords.py#L463) —— `mrecarray = MaskedRecords`，二者完全等价。

> ⚠️ **文档字符串与实现有出入（重要）**：类的 docstring 把 `_data`/`_mask`/`_fieldmask` 描述成三个不同含义的属性（`_mask` 是「整行掩码」，`_fieldmask` 是「逐字段掩码」）。但**实际代码里 `_fieldmask` 只是一个返回 `self._mask` 的 property 别名**（见 4.2.3）。也就是说，逐字段的屏蔽信息**就存在 `_mask` 里**（因为 `_mask` 的 dtype 是结构化布尔），并没有单独的 `_fieldmask` 数组。读这份源码时，要以代码为准，别被 docstring 误导。

#### 4.1.2 核心流程

`MaskedRecords` 继承链是 `MaskedRecords → ma.MaskedArray → ndarray`，但它把「数据视图的基类」换成了 `np.recarray`：

```text
MaskedRecords.__new__
   │
   ├── np.recarray.__new__(...)      # 先按 recarray 的方式分配内存、确定结构化 dtype
   │
   ├── mdtype = ma.make_mask_descr(self.dtype)   # 把每个字段转成 bool，得到掩码 dtype
   │
   └── 规整 mask（形状对齐、keep_mask 决定合并/覆盖）→ 写入 self._mask
```

因为是 `ndarray` 子类，内存分配必须在 `__new__` 阶段完成（这一点 u2-l2 已详细解释过子类化的三个钩子 `__new__`/`__array_finalize__`/`__array_wrap__`）。`MaskedRecords` 重写了 `__new__` 和 `__array_finalize__`，但**没有**重写 `__array_wrap__`——ufunc 后的掩码传播沿用父类 `MaskedArray` 的实现。

属性式访问字段靠重写 `__getattribute__`：普通属性走 `object.__getattribute__`，取不到时再去 `dtype.fields` 里找字段名，取出对应字段列、配上对应字段列的掩码、包成一个普通 `MaskedArray` 返回。

#### 4.1.3 源码精读

**构造：`__new__`** —— [mrecords.py:94-L132](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/mrecords.py#L94-L132)

关键几句（精简）：

```python
self = np.recarray.__new__(cls, shape, dtype=dtype, ...)   # 以 recarray 身份分配内存
mdtype = ma.make_mask_descr(self.dtype)                    # 结构化 → 逐字段 bool
if mask is ma.nomask or not np.size(mask):
    if not keep_mask:
        self._mask = tuple([False] * len(mdtype))          # 无掩码时的占位
else:
    mask = np.array(mask, copy=copy)
    ...                                                    # 形状对齐、合并/覆盖
    self._mask = _mask
```

注意 `__new__` 里**直接把 `_mask` 当作一个逐字段的结构化数组来写**，这就是「字段级屏蔽」的物理载体。

**`__array_finalize__`** —— [mrecords.py:134-L151](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/mrecords.py#L134-L151)

这是切片/视图/ufunc 后的「兜底传播」钩子（u2-l2 讲过它的角色）。这里它专门保证「无论如何，`_mask` 一定存在且是结构化的」：若来源对象没有 `_mask`，就用 `make_mask_none` 造一个全 False 的结构化掩码；若是普通布尔掩码，就广播成结构化。结尾还有一句把 `_baseclass` 从 `ndarray` 改写成 `recarray`，保证后续属性访问走记录数组语义。

**属性式访问：`__getattribute__`** —— [mrecords.py:180-L225](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/mrecords.py#L180-L225)

核心是「先试普通属性，失败再当代字段名」。取出字段列后，它把该列连同该字段的掩码列包成一个普通 `MaskedArray`：

```python
obj = _data.getfield(*res)                  # 取出该字段的数据列
...
obj = obj.view(ma.MaskedArray)
obj._mask = _mask[attr]                     # 配上该字段的掩码列
```

所以 `mbase.a` 返回的是一个**普通 `MaskedArray`**（不是 `MaskedRecords`），其 `_mask` 就是结构化 `_mask` 里 `'a'` 那一列。

#### 4.1.4 代码实践

**实践目标**：体会 `MaskedRecords` 是「`MaskedArray` + `recarray` 字段属性访问」的组合。

**操作步骤**（示例代码）：

```python
import numpy as np
import numpy.ma as ma
import numpy.ma.mrecords as mr

ddtype = [('a', int), ('b', float), ('c', '|S8')]
base = ma.array(list(zip([1, 2, 3], [1.1, 2.2, 3.3], [b'x', b'y', b'z'])),
                mask=[1, 0, 0], dtype=ddtype)
mbase = base.view(mr.mrecarray)          # 把普通结构化 MaskedArray “升级”成 mrecarray
```

**需要观察的现象**：

1. `type(mbase).__name__` 应为 `MaskedRecords`。
2. `mbase.a` 能用属性语法取出字段，等价于 `mbase['a']`。
3. `isinstance(mbase._data, np.recarray)` 为 `True`（数据被视图成 recarray）。

**预期结果**（依据 [tests/test_mrecords.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/tests/test_mrecords.py) 的 `test_byview` 断言）：

- `mbase['a']` 与 `base['a']` 逐元素相等；
- `mbase._data` 是 `recarray` 实例，且与 `base._data.view(recarray)` 逐记录相等。

> 这一步属于「源码阅读 + 运行验证」型实践，命令需在你本地 Python 环境执行；若运行结果与上述不一致，请以你本地实际版本为准（「待本地验证」的最终口径以你机器为准）。

#### 4.1.5 小练习与答案

**练习 1**：`MaskedRecords` 继承自谁？为什么它没有重写 `__array_wrap__`？

> **答案**：继承自 `ma.MaskedArray`（进而继承 `ndarray`）。不重写 `__array_wrap__` 是因为 ufunc 后的掩码合并逻辑（`mask_or`、域屏蔽）在父类 `MaskedArray.__array_wrap__` 里已经正确处理结构化掩码，子类无需重复。

**练习 2**：为什么 `mbase.a` 返回的是普通 `MaskedArray` 而不是 `MaskedRecords`？

> **答案**：单个字段列不再是「记录」（没有多个命名子字段），所以 `__getattribute__` 在 [mrecords.py:212](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/mrecords.py#L212) 处显式 `obj.view(ma.MaskedArray)` 并把 `_baseclass` 设为 `ndarray`。

---

### 4.2 字段屏蔽 vs 整行屏蔽：_fieldmask 与 recordmask

#### 4.2.1 概念说明

这是本讲最核心、也最容易混淆的一对概念。一条「记录」有多个字段；屏蔽可以作用在两个粒度上：

| 粒度 | 含义 | 在代码里的体现 |
| --- | --- | --- |
| **字段级（field）** | 「这条记录的某个字段无效」 | 存在结构化的 `_mask` 里，每个字段一列布尔 |
| **记录级（record）** | 「整条记录都无效」 | 由 `recordmask` 派生：仅当一条记录的**所有**字段都被屏蔽时才为 `True` |

举例：一条 `(温度=屏蔽, 湿度=有效, 气压=有效)` 的记录，其字段级掩码是 `(True, False, False)`，而记录级掩码 `recordmask` 是 `False`——因为并非全部字段屏蔽。

> 关键结论：**屏蔽一个字段 ≠ 屏蔽整条记录。** 只有当一条记录的全部字段都被屏蔽时，`recordmask` 才为 `True`。

#### 4.2.2 核心流程

字段级掩码的「物理真相」其实非常简单——它就是结构化的 `_mask` 本身：

```text
                  dtype = [('a', int), ('b', float)]
        make_mask_descr ────────────────────────────────►
                  _mask.dtype = [('a', '|b1'), ('b', '|b1')]
        _mask[i] = (a 是否屏蔽, b 是否屏蔽)     ← 这就是「字段级掩码」
```

记录级掩码则是对字段级掩码做一次「按字段维度取与」：

```text
recordmask[i] = _mask[i]['a'] AND _mask[i]['b'] AND ...   # 所有字段都 True 才 True
```

用 NumPy 的写法就是 `np.all(flatten_structured_array(_mask), axis=-1)`——把结构化掩码「摊平」成一个普通二维布尔数组（每个字段一列），再沿最后一维（字段维）全取与。

#### 4.2.3 源码精读

**`_fieldmask` 其实是 `_mask` 的别名** —— [mrecords.py:161-L167](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/mrecords.py#L161-L167)

```python
@property
def _fieldmask(self):
    """Alias to mask."""
    return self._mask
```

这证实了 4.1.1 的提醒：**没有独立的 `_fieldmask` 数组**，逐字段屏蔽信息就住在结构化的 `_mask` 里，`_fieldmask` 只是个历史遗留的别名。这也是为什么类 docstring 与实现不符。

**`recordmask` 定义在父类 `MaskedArray`** —— [core.py:3597-L3618](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3597-L3618)

```python
@property
def recordmask(self):
    """...结构化数组时：当所有字段都被屏蔽才返回 True..."""
    _mask = self._mask.view(ndarray)
    if _mask.dtype.names is None:
        return _mask                       # 非结构化：直接返回掩码
    return np.all(flatten_structured_array(_mask), axis=-1)

@recordmask.setter
def recordmask(self, mask):
    raise NotImplementedError("Coming soon: setting the mask per records!")
```

两个要点：① 对结构化数组，`recordmask` 是**计算出来的派生视图**，不是独立存储；② 它的 **setter 直接抛 `NotImplementedError`**——所以 `recordmask` 只能读不能写（至少在当前实现里）。

**`flatten_structured_array`** —— [core.py:2559](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L2559) 把 `dtype=[('a','|b1'),('b','|b1')]` 的结构化布尔数组摊成 `shape + (nfields,)` 的普通布尔数组，供上面的 `np.all(..., axis=-1)` 使用。

**顺带一提：`_get_fieldmask` 是「死代码」** —— [mrecords.py:69-L73](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/mrecords.py#L69-L73)

```python
def _get_fieldmask(self):
    mdescr = [(n, '|b1') for n in self.dtype.names]
    fdmask = np.empty(self.shape, dtype=mdescr)
    fdmask.flat = tuple([False] * len(mdescr))
    return fdmask
```

这个函数会构造一个全 `False` 的结构化掩码，但**全仓库没有任何地方调用它**（它既未被 `__new__` 也未被 `__array_finalize__` 使用；那些地方用的是 `make_mask_descr` / `make_mask_none`）。它是早期设计的残留，现在已无作用。读源码时遇到它，知道「这是个未使用的辅助函数」即可，不要以为字段掩码是它生成的。

#### 4.2.4 代码实践（本讲主实践）

**实践目标**：亲手验证「屏蔽一个字段，`recordmask` 不一定变 `True`；只有全部字段屏蔽才会」。

**操作步骤**：

```python
import numpy as np
import numpy.ma as ma
import numpy.ma.mrecords as mr

# 用 fromarrays 构造一个 2 字段、5 条记录的 mrecarray（初始全不屏蔽）
m = mr.fromarrays([np.arange(5), np.arange(5.0)],
                  dtype=[('a', int), ('b', float)])

# 只屏蔽第 2 条记录的 a 字段（其它字段、其它记录都不动）
m['a'][2] = ma.masked

print('字段级掩码 _mask.tolist() =', m._mask.tolist())
print('字段级掩码（别名）_fieldmask is _mask =', m._fieldmask is m._mask)
print('记录级掩码 recordmask =', list(m.recordmask))
```

**需要观察的现象 + 预期结果**（依据 [tests/test_mrecords.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/tests/test_mrecords.py) 中 `test_set_fields_mask` 的同款断言）：

- `m['a']._mask` 应为 `[False, False, True, False, False]`（只有第 2 条的 a 被屏蔽）。
- `m._mask.tolist()` 应为：
  ```python
  [(False, False), (False, False), (True, False), (False, False), (False, False)]
  ```
  注意第 2 条记录是 `(True, False)`——a 屏蔽、b 未屏蔽。
- `m._fieldmask is m._mask` 为 `True`（别名指向同一对象）。
- `recordmask` 应为 `[False, False, False, False, False]`——**因为没有任何一条记录的全部字段都被屏蔽**（第 2 条的 b 仍然有效）。

**对比实验**：把第 2 条记录的 b 也屏蔽掉：

```python
m['b'][2] = ma.masked
print('现在 _mask[2] =', tuple(m._mask[2]))   # (True, True)
print('现在 recordmask =', list(m.recordmask)) # 第 2 条变 True
```

此时第 2 条记录的字段掩码变成 `(True, True)`，`recordmask` 才在第 2 位变成 `True`。

> 这一步需在你本地环境运行；若版本差异导致输出不同，以本地为准（「待本地验证」）。

#### 4.2.5 小练习与答案

**练习 1**：如果一个 3 字段的 `mrecarray` 某条记录的字段掩码是 `(True, False, True)`，它的 `recordmask` 是什么？

> **答案**：`False`。`recordmask` 要求**所有**字段（三个都得 True）才为 True，这里 b 字段未屏蔽。

**练习 2**：为什么写 `m.recordmask = [1, 0, 0]` 会报错？

> **答案**：`recordmask` 的 setter 在 [core.py:3616-L3618](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3616-L3618) 显式 `raise NotImplementedError`。记录级掩码目前是只读的派生量。

**练习 3**：`_fieldmask` 和 `_mask` 是两个不同的数组吗？

> **答案**：不是。`_fieldmask` 是 [mrecords.py:161-L167](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/mrecords.py#L161-L167) 定义的 property，`return self._mask`，二者是同一个对象（`is` 判定为 True）。字段级屏蔽信息就存在结构化的 `_mask` 里。

---

### 4.3 三种构造方式：fromarrays / fromrecords / fromtextfile

#### 4.3.1 概念说明

`mrecords.py` 提供三个「场景化构造器」，全部定义在模块级，返回 `mrecarray`：

| 函数 | 输入 | 典型场景 |
| --- | --- | --- |
| `fromarrays` | 「每个字段一个（掩码）数组」的列表 | 各字段数据已分别就绪，按列拼装 |
| `fromrecords` | 「每条记录一个元组/列表」的列表 | 数据以行为单位，按行录入 |
| `fromtextfile` | 一个文本文件句柄/路径 | 从带表头的分隔符文本（如 CSV）读入，缺失值自动转屏蔽 |

它们都遵循同一个套路：先用 NumPy 原生的 `np.rec.fromarrays` / `np.rec.fromrecords` 把数据装成普通 `recarray`，再 `.view(mrecarray)` 升级成掩码记录数组，最后单独把掩码灌进 `_mask`。

#### 4.3.2 核心流程

以 `fromarrays` 为代表（最常用）：

```text
fromarrays([arr_a, arr_b], dtype=...)
   │
   ├── datalist = [getdata(x) for x in arraylist]      # 抽出每个输入的纯数据
   ├── masklist = [getmaskarray(x) for x in arraylist] # 抽出每个输入的掩码（无则全 False）
   │
   ├── _array = np.rec.fromarrays(datalist, ...).view(mrecarray)  # 装成 recarray 再升级
   │
   └── _array._mask.flat = list(zip(*masklist))        # 把逐字段掩码转置后灌入结构化 _mask
```

关键在最后一步 `zip(*masklist)`：`masklist` 是「字段在外、记录在内」的列表，转置后变成「每条记录一个 (mask_a, mask_b, ...) 元组」，正好匹配结构化 `_mask` 的布局。

#### 4.3.3 源码精读

**`fromarrays`** —— [mrecords.py:471-L511](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/mrecords.py#L471-L511)

注意它对每个输入用 `ma.getdata` / `ma.getmaskarray`（u1-l4 讲过的安全取值函数）：即使传入的是普通 ndarray（没有掩码），`getmaskarray` 也会返回全 False，保证「输入可普通可掩码」。

**`fromrecords`** —— [mrecords.py:514-L576](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/mrecords.py#L514-L576)

比 `fromarrays` 多处理两种情况：① 输入本身可能是 `MaskedArray`，会先用 `reclist.filled()` 抹掉掩码再 `tolist()`（行 [552-L553](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/mrecords.py#L552-L553)），随后从 `getattr(reclist, '_mask', None)` 抢回原始字段掩码（行 [548](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/mrecords.py#L548) 与 [574-L575](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/mrecords.py#L574-L575)）；② 接受一个额外的 `mask=` 参数作为「外部整行掩码」。

**`fromtextfile`** —— [mrecords.py:636-L702](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/mrecords.py#L636-L702)

这是最「智能」的一个：

1. 把第一个非空行当作字段名（`varnames`）；
2. 用 `_guessvartypes`（[mrecords.py:579-L613](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/mrecords.py#L579-L613)）逐列尝试 `int → float → complex → str` 推断每列 dtype；
3. 用 `missingchar`（默认空串）标记缺失值——`_mask = (_variables.T == missingchar)` 这一句（[mrecords.py:698](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/mrecords.py#L698)）把缺失位置直接变成屏蔽；
4. 最后把每列构造成 `masked_array` 再调 `fromarrays`。

> `_guessvartypes` 的推断顺序是 `int → float → complex → str`：能转成 `int` 就当整数，否则试 `float`，再否则试 `complex`，都失败就保持字符串 dtype。这是个「贪心窄化」策略。

#### 4.3.4 代码实践

**实践目标**：用三种构造器各构造一次 `mrecarray`，对比它们处理「缺失」的方式。

**操作步骤**：

```python
import numpy as np
import numpy.ma as ma
import numpy.ma.mrecords as mr
import io

# (1) fromarrays：第二个数组的第 2 个元素被屏蔽
a = np.array([1, 2, 3])
b = ma.array([1.1, 2.2, 3.3], mask=[0, 1, 0])
m1 = mr.fromarrays([a, b], dtype=[('a', int), ('b', float)])

# (2) fromrecords：从普通记录列表构造（无掩码）
m2 = mr.fromrecords([(1, 1.1), (2, 2.2)], dtype=[('a', int), ('b', float)])

# (3) fromtextfile：含缺失值的文本，缺失处自动屏蔽
text = io.StringIO("a,b\n1,1.1\n2,\n3,3.3\n")   # 第二行 b 缺失
m3 = mr.fromtextfile(text, delimiter=',')
```

**需要观察的现象 + 预期结果**：

- `m1._mask.tolist()` 第二条记录应为 `(False, True)`——`b` 的屏蔽被带进来。
- `m2._mask.tolist()` 全为 `(False, False)`（无掩码输入）。
- `m3` 的第 2 条记录的 `b` 字段应被屏蔽（`m3.b._mask[1]` 为 `True`），因为输入文本里该位置是空串，命中 `missingchar=''`。

> `fromtextfile` 默认按空白分隔；这里用 `delimiter=','` 指定逗号。完整行为以你本地 NumPy 版本为准（「待本地验证」）。

#### 4.3.5 小练习与答案

**练习 1**：`fromarrays` 为什么对每个输入都用 `getmaskarray` 而不是 `getmask`？

> **答案**：`getmask` 在无掩码时返回 `nomask` 单例（u2-l1），无法直接 `zip`；`getmaskarray` 永远返回同形状全 False 数组（u1-l4），保证普通 ndarray 与掩码数组都能统一处理。

**练习 2**：`_guessvartypes` 对列 `["3", "1.5", "x"]` 会推断出什么 dtype？

> **答案**：`str`（保持原 dtype）。`"3"` 能转 int，但 `_guessvartypes` 只测试**第一行**（[mrecords.py:591-L592](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/mrecords.py#L591-L592) 在 2D 时取 `arr[0]`），而推断是逐元素尝试同一行的每个单元——`"x"` 三种转换都失败，故该列保持字符串。这是该函数的一个已知局限：推断只看第一行。

---

### 4.4 字段名安全与动态加字段：reserved_fields / _checknames / addfield

#### 4.4.1 概念说明

`MaskedRecords` 把字段当属性访问（`m.a`），而它自身又用 `_data`、`_mask`、`_fieldmask`、`dtype` 这些名字做内部簿记。如果用户给字段起名叫 `_mask`，属性访问就会撞车——`m._mask` 到底是「掩码」还是「名为 `_mask` 的字段」？

为此模块定义了一份保留字段名清单，并在构造时把撞名的字段**自动重命名**为 `f0`、`f1`、……。此外，`addfield` 允许你在数组造好之后再加一个字段，它会同步扩展数据 dtype 与掩码 dtype。

#### 4.4.2 核心流程

```text
reserved_fields = ['_data', '_mask', '_fieldmask', 'dtype']

_checknames(descr, names):
   逐字段检查 → 撞保留名的字段改名为 'f<i>' → 返回修正后的 dtype

addfield(mrecord, newfield, newfieldname):
   newdtype = 旧 dtype.descr + [(新字段名, 新字段dtype)]
   新建空 recarray → 拷贝旧数据各字段 + 写入新字段数据
   新建空 mask recarray → 拷贝旧掩码各字段 + 写入新字段掩码
   返回带新掩码的 MaskedRecords
```

#### 4.4.3 源码精读

**保留字段名清单** —— [mrecords.py:32](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/mrecords.py#L32)

```python
reserved_fields = ['_data', '_mask', '_fieldmask', 'dtype']
```

注意 `mask`、`fieldmask`（不带下划线）不在清单里——因为 `__setattr__` 对这两个名字有特判（见下）。

**`_checknames`** —— [mrecords.py:35-L66](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/mrecords.py#L35-L66)

逻辑：为每个字段准备一个「默认名」`f0/f1/...`；若用户提供的名字 `n` 落在 `reserved_fields` 里，就用默认名 `d` 替换；否则保留用户名。它还兼容 `names` 为逗号分隔字符串、或长度不足时自动补 `f<i>` 的情形。

**`addfield`** —— [mrecords.py:705-L739](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/mrecords.py#L705-L739)

要点：

- 新字段名缺省时取 `f{len(_data.dtype)}`（即「现有字段数」作为下标），且若撞保留名也会走这个缺省（行 [715-L716](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/mrecords.py#L715-L716)）。
- 它**新建**一个更大的 `recarray` 与更大的掩码 `recarray`，把旧字段逐个 `setfield` 拷过去，再写入新字段的数据与掩码——本质是「拷贝重建」，不是原地扩容。
- 新字段的掩码来自 `ma.getmaskarray(newfield)`（行 [736](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/mrecords.py#L736)），所以传进来的 `newfield` 若本身是掩码数组，其屏蔽会被保留。

**`__setattr__` 对 `mask`/`fieldmask` 的特判** —— [mrecords.py:227-L281](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/mrecords.py#L227-L281)

开头几行（[233-L235](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/mrecords.py#L233-L235)）：

```python
if attr in ['mask', 'fieldmask']:
    self.__setmask__(val)
    return
```

这就是为什么 `m.mask = ...` 会设置整组掩码（转交父类 `MaskedArray.__setmask__`，[core.py:3511-L3581](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3511-L3581)），而不是「创建一个叫 mask 的字段」。而给字段赋 `ma.masked`（如 `m.c = ma.masked`）会把该字段全部屏蔽（行 [269-L280](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/mrecords.py#L269-L280)）。

#### 4.4.4 代码实践

**实践目标**：观察保留字段名自动改名，并用 `addfield` 追加一个带掩码的字段。

**操作步骤**：

```python
import numpy as np
import numpy.ma as ma
import numpy.ma.mrecords as mr

# (1) 故意把一个字段命名为 '_mask'（保留名）
m = mr.fromarrays([np.arange(3), np.arange(3.0)],
                  names='_mask,b', formats='i8,f8')
print('字段名 =', m.dtype.names)        # 预期 _mask 被改名为 f0

# (2) 追加一个带掩码的新字段
m2 = mr.fromarrays([np.arange(3)], dtype=[('a', int)])
new = ma.array([10, 20, 30], mask=[0, 1, 0])
m3 = mr.addfield(m2, new, newfieldname='c')
print('新字段名 =', m3.dtype.names)
print('新字段掩码 c._mask =', list(m3['c']._mask))
```

**需要观察的现象 + 预期结果**：

- 第 (1) 步：`m.dtype.names` 中原本的 `_mask` 应被替换为 `f0`（依据 `_checknames` 的逻辑），`b` 保留。
- 第 (2) 步：`m3.dtype.names` 为 `('a', 'c')`；`m3['c']._mask` 为 `[False, True, False]`，新字段的掩码被正确带过来。

> 这一步以你本地运行结果为准（「待本地验证」）。

#### 4.4.5 小练习与答案

**练习 1**：如果用户给字段起名叫 `mask`（不带下划线），会被 `_checknames` 改名吗？

> **答案**：不会。`reserved_fields`（[mrecords.py:32](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/mrecords.py#L32)）只含带下划线的 `_mask` 等，不含 `mask`。但写 `m.mask = ...` 仍会触发 `__setattr__` 的特判走 `__setmask__`，而不是设置该字段——所以即便字段名是 `mask`，属性赋值也访问不到它，需用 `m['mask']` 下标语法。

**练习 2**：`addfield` 是原地修改还是返回新数组？

> **答案**：返回新数组。它在 [mrecords.py:720-L738](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/mrecords.py#L720-L738) 新建了更大的 `recarray` 与掩码 `recarray`，逐字段拷贝后返回，原 `mrecord` 不变。

---

## 5. 综合实践

把本讲四个模块串起来：**构造 → 字段级屏蔽 → 观察两种掩码差异 → 追加字段 → 持久化往返**。

```python
import numpy as np
import numpy.ma as ma
import numpy.ma.mrecords as mr
import pickle

# 1. 用 fromarrays 构造 3 字段的气象观测表
temp  = ma.array([23.5, 25.0, -999.0], mask=[0, 0, 1])   # 第 3 条温度缺失
humid = ma.array([0.4, 0.5, 0.6])
press = ma.array([1013, 1010, 1008])
obs = mr.fromarrays([temp, humid, press],
                    dtype=[('temp', float), ('humid', float), ('press', int)])

# 2. 只屏蔽第 1 条的 humid 字段（其它字段、其它记录不动）
obs['humid'][0] = ma.masked

# 3. 对比字段级掩码与记录级掩码
print('字段掩码 _mask =', obs._mask.tolist())
print('记录掩码 recordmask =', list(obs.recordmask))
#   预期：第 0 条字段掩码 (False, True, False) → recordmask[0] 仍为 False
#         第 2 条字段掩码 (True, False, False)  → recordmask[2] 仍为 False
#         即没有一条记录「全部字段屏蔽」，recordmask 全 False

# 4. 追加一个 wind 字段
wind = ma.array([3.0, 4.0, 5.0], mask=[0, 1, 0])
obs2 = mr.addfield(obs, wind, newfieldname='wind')
print('追加后字段名 =', obs2.dtype.names)
print('wind 掩码 =', list(obs2['wind']._mask))

# 5. pickle 往返，确认子类与掩码都还原（承接 u3-l4 的持久化主题）
buf = pickle.dumps(obs2)
obs3 = pickle.loads(buf)
assert obs3._mask.tolist() == obs2._mask.tolist()
print('pickle 往复成功，类型 =', type(obs3).__name__)
```

**检查清单**：

- [ ] 字段掩码 `_mask` 是「逐字段」的元组列表，`recordmask` 是「逐记录」的布尔列表。
- [ ] 第 0、2 条虽有字段被屏蔽，但 `recordmask` 仍为 `False`（并非全字段屏蔽）。
- [ ] `addfield` 后 `wind` 的掩码被保留。
- [ ] pickle 往返后类型仍是 `MaskedRecords`，掩码逐字段一致。

> 完整运行结果以你本地 NumPy 版本为准（「待本地验证」）。

---

## 6. 本讲小结

- `MaskedRecords`（= `mrecarray`）是 `ma.MaskedArray` 的子类，把底层数据视图成 `np.recarray`，从而支持 `m.字段名` 属性式访问；屏蔽能力本身来自父类对结构化 dtype 的支持。
- **字段级屏蔽 vs 记录级屏蔽**是本讲核心：逐字段的屏蔽信息就存在结构化的 `_mask` 里（`_fieldmask` 只是它的别名 property）；`recordmask`（定义在父类 `MaskedArray`）是派生量，仅当一条记录的**所有**字段都被屏蔽时才为 `True`，且其 setter 抛 `NotImplementedError`。
- 三个构造器 `fromarrays`/`fromrecords`/`fromtextfile` 共用「先用 `np.rec.*` 装成 recarray，再 `.view(mrecarray)`，最后灌掩码」的套路；`fromtextfile` 还会自动推断 dtype 并把 `missingchar` 处转屏蔽。
- `reserved_fields` + `_checknames` 防止字段名撞内部簿记属性（`_data`/`_mask`/`_fieldmask`/`dtype`），撞名自动改 `f<i>`；`addfield` 以拷贝重建方式追加字段并同步其掩码。
- 读这份源码要警惕「文档字符串描述的设计 vs 实际实现」的出入：`_fieldmask` 是别名而非独立数组，`_get_fieldmask` 是无调用的死代码。

---

## 7. 下一步学习建议

- **回归测试体系**：接下来建议读 [u3-l6 测试体系与 testutils 工具](u3-l6-testutils-testing.md)，特别看 `tests/test_mrecords.py` 如何用 `assert_equal_records`、`assert_mask_equal` 把本讲的字段级/记录级屏蔽行为钉成可执行契约——本讲的「预期结果」大多取自该文件。
- **持久化深入**：本讲综合实践用到了 pickle 往返，其底层是 `MaskedRecords.__reduce__`/`_mrreconstruct`（[mrecords.py:443-L460](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/mrecords.py#L443-L460)），可对照 u3-l4 讲的 `MaskedArray` 版 `_mareconstruct` 理解二者差异。
- **架构取舍**：mrecords 是「便利外壳」而非「新机制」，若你只关心屏蔽本身，父类 `MaskedArray` + 结构化 dtype 已足够；可结合 u3-l7 评估「何时该用 mrecords、何时不必」。
