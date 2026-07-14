# 五分钟创建你的第一个 record array

## 1. 本讲目标

上一讲（u1-l2）我们已经建立了直觉：record array 就是一个能用属性方式 `r.x` 访问字段的结构化数组。本讲把它落到「手上能跑」的层面。学完本讲，你应该能够：

- 用 `np.rec.fromrecords` 把「一行一条记录」的 Python 数据（list of tuple）快速变成 record array；
- 用 `np.rec.array` 这个通用构造函数，统一处理 list / tuple / ndarray / bytes / 文件等多种输入；
- 通过 `names` 参数给字段起名，并用 `r.字段名` 这种属性方式访问每一列。

本讲只讲「怎么最快建出来」，`fromrecords` 内部的自动类型探测细节、`array` 的二进制分支（fromstring/fromfile）会留到进阶/专家篇（u4）展开。

## 2. 前置知识

承接 u1-l1 与 u1-l2，你需要先记住三件事：

1. **真实实现不在 `numpy/rec/`。** `numpy/rec/__init__.py` 只是一个「再导出垫片」，所有函数和类的物理实现都在 `numpy/_core/records.py`。本讲引用的所有源码行号都指向这个文件。
2. **字段（field）= 结构化数组的一列。** 一个结构化 dtype 像 `[('x','f8'),('y','i4')]`，其中每个 `(名字, 子类型)` 就是一个字段。
3. **属性访问是 record array 的「招牌」。** 普通结构化数组用字典式 `arr['x']` 取列；record array 额外允许 `arr.x`。取出来的列是一个普通 ndarray；取出来的「单条记录」是一个 `numpy.record` 标量，它也能用属性访问。

本讲还会用到三个参数名词，先统一一下口径：

| 参数 | 作用 | 典型取值 |
|------|------|----------|
| `dtype` | 直接给出完整的结构化类型 | `[('id','i8'),('name','U8')]` |
| `formats` | 只给「每列的类型」，不含名字 | `['i8','U8']` 或 `'i8,U8'` |
| `names` | 只给「每列的名字」，逗号分割或列表 | `'id,name'` 或 `['id','name']` |

`dtype` 和 `formats+names` 是两套等价的表达方式：给了 `dtype` 就不用再给 `formats/names`；都不给，`fromrecords` 还能自动探测类型。这正是本讲要讲清楚的核心。

## 3. 本讲源码地图

本讲涉及的关键文件：

- [numpy/rec/__init__.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/rec/__init__.py) — 再导出垫片，把 `_core/records.py` 的符号挂到 `numpy.rec` 名下。
- `numpy/_core/records.py` — 真实实现，本讲关注其中四段：
  - `format_parser` 类（构造 dtype，处理 `names`）；
  - `recarray.__new__` 与 `recarray.__getattribute__`（构造与属性访问）；
  - `fromrecords` 函数（行式构建）；
  - `array` 函数（统一调度）。

## 4. 核心概念与源码讲解

### 4.1 fromrecords：从行数据构建 record array

#### 4.1.1 概念说明

`fromrecords` 接收的是**行方向**的数据：一个序列，里面每个元素是「一条记录」，每条记录又是一个 tuple/list，按顺序给出各字段的值。

```python
data = [
    (1, 'a', 1.1),   # 第 0 行：id=1, name='a', val=1.1
    (2, 'b', 2.2),   # 第 1 行
]
```

这和我们平时在表格里写数据的方式一致：**外层是行，内层是列**。与之相对，`fromarrays`（u2-l3 会讲）接收的是**列方向**数据。记住这个区别，后续就不会混淆。

`fromrecords` 最讨人喜欢的一点是：你**可以完全不指定类型**，它会自己探测每列该用什么 dtype。

#### 4.1.2 核心流程

`fromrecords` 内部有三条路径，按优先级判断：

```
收到 recList + 可选的 dtype/formats/names
        │
        ├─ formats 和 dtype 都没给？
        │     └─ 慢速自动探测路径：转 object 二维数组 → 按列拆分 → 交给 fromarrays
        │
        ├─ 给了 dtype？
        │     └─ 把 dtype 包成 (record, dtype)，直接 sb.array(recList, dtype=descr)
        │
        └─ 给了 formats（没给 dtype）？
              └─ 用 format_parser 把 formats/names 组装成 dtype，再 sb.array(...)
                    │
                    └─ sb.array 成功 → .view(recarray) 返回
                       sb.array 失败 → 走「list of lists 兼容」分支（本讲先不展开）
```

关键点：无论哪条路径，最终都会经过一次 `.view(recarray)`，把普通结构化 ndarray「升级」成 record array。这个 view 不拷贝数据，只换视图类型（u1-l2 已演示）。

#### 4.1.3 源码精读

函数定义与签名（注意它用 `@set_module('numpy.rec')` 装饰，所以对外显示为 `numpy.rec.fromrecords`）：

[records.py:664-666](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L664-L666) — `fromrecords(recList, dtype=None, shape=None, formats=None, names=None, titles=None, aligned=False, byteorder=None)`，接收行式数据与一组可选的类型描述参数。

第一条路径：**自动探测**。当 `formats is None and dtype is None` 时走这里，把整张表先变成 object 二维数组，再按最后一维（列）逐列取出、各自提升成最合适的类型，最后委托给 `fromarrays`：

[records.py:708-714](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L708-L714) — `obj = sb.array(recList, dtype=object)` 做成 object 二维数组；`obj[..., i].tolist()` 取第 i 列并转回普通 list，让 NumPy 对每列单独推断 dtype。这就是「同一列里 `456` 和 `2` 提升成整数、`'dbe'`/`'de'` 提升成字符串」的来源。

第二条路径：**已有 dtype 或 formats**。先把类型描述规整成一个 dtype 对象 `descr`：

[records.py:716-721](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L716-L721) — 给了 `dtype` 就用 `sb.dtype((record, dtype))`（注意这里把标量类型显式设成 `record`）；否则用 `format_parser(formats, names, titles, ...).dtype` 组装。

然后用 `sb.array(recList, dtype=descr)` 一次性把数据按这个类型铺好：

[records.py:723-724](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L723-L724) — 尝试直接按 `descr` 构造 ndarray。

成功后的收尾：

[records.py:744-750](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L744-L750) — 若指定了 `shape` 且不匹配就 `reshape`，最后 `retval.view(recarray)` 升级成 record array 返回。

> 说明：`try` 失败时的 `except` 分支（[records.py:725-743](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L725-L743)）主要用来兼容「传了 list of lists 而非 list of tuples」的旧用法，会发一条 `FutureWarning`。本讲先不深入，留到 u4-l1。

#### 4.1.4 代码实践

**实践目标**：亲手跑一次自动探测路径，看清每列被推断成了什么类型。

**操作步骤**：

```python
import numpy as np

r = np.rec.fromrecords([(1, 'a', 1.1), (2, 'b', 2.2)],
                       names='id,name,val')
print(r)          # 整个数组
print(r.id)       # 属性访问第 0 列
print(r.name)     # 第 1 列
print(r.val)      # 第 2 列
print(r.dtype)    # 看自动推断出的 dtype
```

**需要观察的现象**：

1. 不给 `dtype`/`formats`，函数仍然成功，说明走了自动探测路径。
2. `r.id`、`r.name`、`r.val` 都能直接当属性用，返回的是普通 ndarray。
3. `r.dtype` 里三个字段的类型由数据自动决定。

**预期结果**（具体整数位宽随平台可能为 `<i8` 或 `<i4`）：

```
rec.array([(1, 'a', 1.1), (2, 'b', 2.2)],
          dtype=[('id', '<i8'), ('name', '<U1'), ('val', '<f8')])
[1 2]
['a' 'b']
[1.1 2.2]
[('id', '<i8'), ('name', '<U1'), ('val', '<f8')]
```

第一列 `1, 2` 没有小数点 → 整数类型；第二列 `'a','b'` → 长度 1 的字符串 `<U1`；第三列 `1.1, 2.2` → 浮点 `<f8`。这就是 [records.py:708-714](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L708-L714) 里逐列 `sb.array(obj[..., i].tolist())` 的效果。

#### 4.1.5 小练习与答案

**练习 1**：把上面的数据里的整数写成 `1.0, 2.0`，`r.id` 的 dtype 会变成什么？为什么？

> **答案**：变成浮点（如 `<f8`）。因为自动探测路径会把第 0 列的 `[1.0, 2.0]` 单独推断，带小数点的字面量被识别为 float。

**练习 2**：不传 `names`，直接 `np.rec.fromrecords([(1,'a'),(2,'b')])`，字段名会是什么？

> **答案**：字段名会是默认的 `f0`、`f1`（由 `format_parser._setfieldnames` 自动补齐，详见 4.2.3）。于是只能用 `r['f0']` 或 `r.f0` 访问。

---

### 4.2 names 参数与字段属性访问

#### 4.2.1 概念说明

光有数据还不够，我们还要给每一列起名字，才能用 `r.id` 这种语义化的属性访问。`names` 就是干这个的。

`names` 的两种等价写法：

- 逗号分割的字符串：`names='id,name,val'`
- 字符串列表/元组：`names=['id', 'name', 'val']`

如果不给 `names`，NumPy 会用默认名 `f0, f1, f2, ...`。此外，字段名不能重复——这是 `find_duplicate` 在守护的。

字段名一旦定下来，`recarray` 就靠 `__getattribute__` 这个魔法方法，把「取属性」翻译成「取这一列」。

#### 4.2.2 核心流程

`names` 从参数到字段名的处理过程：

```
names 传入
   │
   ├─ 是 list/tuple？ ────────── 直接用
   ├─ 是 str？        ────────── names.split(',') 按逗号拆分，再 strip 空白
   ├─ 为空/None？     ────────── 用空列表，随后补成 f0,f1,...
   └─ 其它？          ────────── 抛 NameError
   │
   ├─ 名字数量不够 nfields？ ── 末尾补 f[n], f[n+1], ...
   └─ 有重名？ ──────────────── find_duplicate 命中 → 抛 ValueError
```

定下名字后，组装成结构化 dtype，构造出的数组每个字段都能用属性访问。

#### 4.2.3 源码精读

`names` 的解析与补齐逻辑在 `format_parser._setfieldnames` 里：

[records.py:150-160](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L150-L160) — 如果 `names` 非空：是 list/tuple 就直接用；是 str 就 `names.split(',')` 按逗号拆分；都不是就抛 `NameError`。最后 `names[:self._nfields]` 截到字段数为止。

[records.py:166-167](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L166-L167) — 名字不够时，用 `f{i}` 补齐到字段总数。这就是「不给 names 就出现 f0/f1」的来源。

[records.py:169-171](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L169-L171) — 调用 `find_duplicate(self._names)`，若有重名字段就抛 `ValueError`。

字段名定下来后，dtype 被组装好，最终传给 `recarray`。`recarray.__new__` 里，只要没给 `dtype`，就用 `format_parser` 现场拼一个 dtype：

[records.py:389-394](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L389-L394) — `recarray.__new__` 中，`dtype is None` 时用 `format_parser(formats, names, titles, aligned, byteorder).dtype` 得到结构化类型 `descr`，并以 `(record, descr)` 作为标量类型构造。

属性访问的魔法在 `recarray.__getattribute__`（u3-l2 会精读，这里先建立印象）：

[records.py:415-430](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L415-L430) — 先尝试 `object.__getattribute__` 拿真正的对象属性（如 `shape`、`dtype`）；拿不到（抛 `AttributeError`）才去 `self.dtype.fields` 里按名字查字段，再用 `getfield` 取出那一列。这就是 `r.id` 能当属性用的底层原因。

#### 4.2.4 代码实践

**实践目标**：直观感受 `names` 的字符串与列表两种写法等价，以及默认名 `f0/f1` 和重名报错。

**操作步骤**：

```python
import numpy as np

# 写法 A：逗号字符串
r1 = np.rec.fromrecords([(1, 'a'), (2, 'b')], names='id,name')
# 写法 B：列表
r2 = np.rec.fromrecords([(1, 'a'), (2, 'b')], names=['id', 'name'])
print(r1.dtype.names == r2.dtype.names)   # True，两种写法等价

# 不给 names
r3 = np.rec.fromrecords([(1, 'a'), (2, 'b')])
print(r3.dtype.names)                      # ('f0', 'f1')

# 故意重名
try:
    np.rec.fromrecords([(1, 2)], formats='i4,i4', names='x,x')
except ValueError as e:
    print("caught:", e)
```

**需要观察的现象**：

1. 写法 A 与 B 产出完全相同的字段名。
2. 不给 `names` 时字段名是 `('f0', 'f1')`。
3. 重名字段会抛 `ValueError: Duplicate field names: ['x']`。

**预期结果**：

```
True
('f0', 'f1')
caught: Duplicate field names: ['x']
```

> 说明：本实践未在文档中逐字给出输出，整数位宽等细节「待本地验证」；字段名与报错文案以你本机实际版本为准。

#### 4.2.5 小练习与答案

**练习 1**：`names='id,name'` 多写了个空格 `'id, name'`，结果会出错吗？

> **答案**：不会。`_setfieldnames` 对每个名字都做了 `n.strip()`（见 [records.py:158](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L158)），前导空格会被去掉，字段名仍是 `'name'`。

**练习 2**：给 3 个字段却只写了 2 个名字 `names='a,b'`，会怎样？

> **答案**：不会报错。第 3 个字段会被自动补成 `f2`（见 [records.py:166-167](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L166-L167)），最终字段名是 `('a', 'b', 'f2')`。

---

### 4.3 array：通用调度构造函数

#### 4.3.1 概念说明

前面 `fromrecords` 只认「行式数据」。但现实里数据来源五花八门：可能是一个普通 ndarray，可能是一段 bytes，可能是一个打开的文件，也可能是 None（只想建个空壳）。`np.rec.array` 就是把这些情况统一收口的一个**总调度入口**——你不用记该调哪个 `fromxxx`，把东西丢给 `array` 就行，它替你判断。

#### 4.3.2 核心流程

`array` 先把类型参数（`dtype`/`formats`/`names`...）预处理成一个 `dtype` 或打包成 `kwds`，然后按 `obj` 的类型依次判断，分发到对应的具体函数：

```
array(obj, dtype=None, formats=None, names=None, ..., copy=True)
   │
   ├─ obj is None        → recarray(shape, dtype, ...)        （需提供 shape 与 dtype/formats）
   ├─ obj is bytes       → fromstring(obj, ...)
   ├─ obj is list/tuple  → 看首元素：
   │       ├─ 首元素是 tuple/list → fromrecords(...)   （行式）
   │       └─ 否则                → fromarrays(...)    （列式）
   ├─ obj is recarray    → 按需 view(dtype) + copy
   ├─ obj 有 readinto    → fromfile(obj, ...)              （已打开的文件对象）
   ├─ obj is ndarray     → view(recarray)（copy=True 时先 copy）
   └─ 其它               → 试 __array_interface__，否则抛 "Unknown input type"
```

注意 list/tuple 的二选一：**首元素是 tuple/list 就当行式（fromrecords），否则当列式（fromarrays）**。这是最容易记混的一个分支。

#### 4.3.3 源码精读

参数预处理：如果有 `dtype` 就转成 dtype 对象；没有 `dtype` 但有 `formats` 就用 `format_parser` 拼一个 dtype；两者都没有时，把 `formats/names/titles/...` 原样打包进 `kwds`，留给下游具体函数处理：

[records.py:1033-1045](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L1033-L1045) — `dtype`/`formats`/`kwds` 三选一的预处理。

几条关键分支：

[records.py:1055-1059](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L1055-L1059) — list/tuple 分支：`isinstance(obj[0], (tuple, list))` 决定走 `fromrecords`（行式）还是 `fromarrays`（列式）。

[records.py:1061-1068](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L1061-L1068) — 输入已是 recarray 时：dtype 不同就先 `view(dtype)`，再按 `copy` 决定是否拷贝。

[records.py:1073-1080](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L1073-L1080) — 输入是普通 ndarray 时：按需 `view(dtype)`、按 `copy` 决定拷贝，最后 `.view(recarray)` 升级为 record array。

完整的分发规则在 docstring 的 Notes 里有一段权威说明：

[records.py:980-989](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L980-L989) — 文档里对 `obj` 各类型如何分发的官方描述，与本讲的流程图一一对应。

#### 4.3.4 代码实践

**实践目标**：用同一个 `np.rec.array` 入口，喂不同类型的输入，对照源码分支确认各自走了哪条路径。

**操作步骤**：

```python
import numpy as np

# (A) list of tuple → fromrecords 分支
a = np.rec.array([(1, 'a', 1.1), (2, 'b', 2.2)], names='id,name,val')
print("A id:", a.id)

# (B) 普通结构化 ndarray → ndarray 分支，view(recarray)
base = np.array([(1, 2), (3, 4)], dtype=[('x', 'i8'), ('y', 'i8')])
b = np.rec.array(base)        # copy=True 默认会拷贝
print("B type:", type(b).__name__, "| x:", b.x)
print("B shares memory with base?", np.shares_memory(b, base))  # False，因为 copy=True

# (C) list of ndarray（首元素不是 tuple/list）→ fromarrays 分支
c = np.rec.array([np.array([1, 2, 3]), np.array([4, 5, 6])], names='a,b')
print("C a:", c.a, "| b:", c.b)
```

**需要观察的现象**：

1. (A) 与 u1-l3 实践结果一致——`array` 对 list of tuple 走的就是 `fromrecords`。
2. (B) 普通 ndarray 进去，出来的是 `recarray`，且因为 `copy=True` 默认会拷贝，与原数组不共享内存。
3. (C) 首元素是 ndarray 而非 tuple，于是走 `fromarrays`（列式），每个 ndarray 当一列。

**预期结果**（整数位宽待本地验证）：

```
A id: [1 2]
B type: recarray | x: [1 3]
B shares memory with base? False
C a: [1 2 3] | b: [4 5 6]
```

> 说明：bytes 与文件对象分支（fromstring/fromfile）涉及二进制布局，留到 u4-l2/u4-l3 再专门实践，本讲不展开。

#### 4.3.5 小练习与答案

**练习 1**：把 (B) 里的调用改成 `np.rec.array(base, copy=False)`，`np.shares_memory(b, base)` 会变成什么？为什么？

> **答案**：变成 `True`。`copy=False` 时 ndarray 分支不再调用 `new.copy()`，只做 `.view(recarray)`，view 共享同一块内存（见 [records.py:1078-1080](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L1078-L1080)）。

**练习 2**：`np.rec.array([(1,2),(3,4)])` 和 `np.rec.array([[1,2],[3,4]])` 分别走哪条分支？

> **答案**：前者首元素是 tuple → `fromrecords`（行式）；后者首元素是 list，`isinstance(obj[0], (tuple, list))` 同样为真，也会走 `fromrecords`。两者都会被当作「行式记录」处理（注意 list of lists 在 `fromrecords` 内部会触发兼容告警，见 u4-l1）。

## 5. 综合实践

把本讲三个模块串起来：用 `array` 作为统一入口，从一份「行式」原始数据出发，命名字段、访问列、再换一种构造方式做对照。

**任务**：

1. 用 `np.rec.array` 从下面的数据建一个 record array，字段名为 `id,kind,value`：

   ```python
   rows = [(10, 'apple', 0.5), (20, 'pear', 1.25), (30, 'kiwi', 3.0)]
   ```

2. 分别用属性方式打印 `value` 列、用字典方式打印 `'kind'` 列，确认两种取列方式结果一致。
3. 取出第 1 条记录 `r[1]`，用 `r[1].value` 访问它的标量字段（这是 `numpy.record` 标量的属性访问，承接 u1-l2）。
4. 对照 4.3.2 的流程图，说出第 1 步的 `rows` 走的是 `array` 的哪条分支、最终落到哪个具体函数。

**参考做法**：

```python
import numpy as np

rows = [(10, 'apple', 0.5), (20, 'pear', 1.25), (30, 'kiwi', 3.0)]
r = np.rec.array(rows, names='id,kind,value')

print(r.value)            # 属性访问
print(r['kind'])          # 字典访问，结果与 r.kind 一致
print(r[1].value)         # 标量属性访问 → 1.25
```

**分析与预期**：

- `rows` 是 list、首元素 `(10, 'apple', 0.5)` 是 tuple → 命中 [records.py:1055-1057](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L1055-L1057) 的 `fromrecords` 分支。
- 因为没给 `dtype`/`formats`，`fromrecords` 内部进一步走自动探测（[records.py:708-714](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L708-L714))：`id` 列推断为整数，`kind` 列推断为字符串（长度按最长 `'apple'`/`'pear'`/`'kiwi'`，即 `<U5`），`value` 列推断为浮点。
- `r.value` 与 `r['value']` 返回同一个 ndarray；`r[1].value` 返回标量 `1.25`（类型为 numpy 浮点标量）。

> 说明：字符串字段的确切长度、整数位宽依本机平台而定，属「待本地验证」的细节，但「属性访问 == 字典访问」「标量可属性访问」这两条结论是稳定的。

## 6. 本讲小结

- `fromrecords` 接收**行式**数据（每行一条记录），不指定 `dtype`/`formats` 时会自动逐列探测类型，最后 `.view(recarray)` 升级。
- `names` 支持逗号字符串或列表两种写法，缺省时自动用 `f0, f1, ...`，且 `find_duplicate` 会拦截重名字段。
- `np.rec.array` 是**总调度入口**：按 `obj` 的类型分发到 `recarray / fromstring / fromrecords / fromarrays / fromfile` 等具体函数；list/tuple 的分支看首元素是 tuple/list（行式）还是其它（列式）。
- record array 的属性访问 `r.x` 由 `recarray.__getattribute__` 实现：先查对象属性，找不到再去 `dtype.fields` 里按名取列。
- 普通结构化 ndarray 可通过 `array(...)` 或 `.view(recarray)` 升级为 record array，`copy` 参数控制是否共享内存。

## 7. 下一步学习建议

本讲让你「能建出来、能取列」。接下来建议：

1. **进阶 u2-l1 / u2-l2**：精读 `format_parser`，彻底搞清 `formats/names/titles` 如何拼成 dtype，以及字节序、对齐、重复检测等细节。
2. **进阶 u2-l3**：学 `fromarrays`（列式构建），和本讲的 `fromrecords`（行式）形成完整对照。
3. **进阶 u3-l1 / u3-l2**：钻进 `recarray` 类本身，理解 `__new__` 构造与 `__getattribute__`/`__setattr__` 的字段访问魔法——本讲只用了它的「效果」，那里讲清它的「原理」。
4. 暂时不用碰 u4 的 `fromstring`/`fromfile`，那是二进制数据来源，等需要处理文件/缓冲区时再看。
