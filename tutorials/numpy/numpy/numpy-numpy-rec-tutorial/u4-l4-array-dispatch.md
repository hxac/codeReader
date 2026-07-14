# array：统一调度构造函数

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 `np.rec.array` 是一个「总调度入口」，它本身几乎不构造数据，而是按 `obj` 的类型把请求转发给具体的构造函数。
- 默写出 `obj` 类型的七条判定分支及其先后顺序，并能解释为什么是这个顺序。
- 区分 `list`/`tuple` 输入时，「首元素是 tuple/list」走 `fromrecords`（行式）、「首元素是 ndarray」走 `fromarrays`（列式）这两条子路径。
- 理解顶部那段「dtype 预解析 / kwds 透传」的三选一逻辑，以及为什么有时要把类型描述**原样下推**而不在 `array` 里提前解析。
- 解释 `recarray` 与 `ndarray` 两条分支中 `view + copy` 的细微差别，以及 `copy=True/False` 对内存共享的影响。

本讲是 u4 单元（三种数据来源的构建函数与统一调度）的收口：u4-l1/l2/l3 分别讲完了 `fromrecords`、`fromstring`、`fromfile`，本讲把它们与 `fromarrays`、`recarray` 一起串到 `array` 这个唯一入口上。

## 2. 前置知识

在进入源码前，先用三句话建立直觉：

- **调度（dispatch）**：写一个函数，根据输入值的**类型**选择不同的实现路径。它像一个前台接线员，听完来意后把你转接到不同部门，自己不动手办事。`np.rec.array` 就是 numpy.rec 的「前台」。
- **视图（view）与拷贝（copy）**：`view` 只换一种「看内存的方式」（不复制字节、与原对象共享内存）；`copy` 则真把字节搬到一块新内存里（与原对象独立）。这一区别贯穿 `array` 的 `recarray`/`ndarray` 两条分支。
- **二元 dtype `(record, descr)`**：这是 u3-l1 引入的关键机制，保持 `descr` 的结构不变，只把标量类型从 `void` 换成 `record`，从而让 `r.x` 与 `r[0].x` 同时可用。本讲里 `array` 不会直接出现二元 dtype，但它调度到的 `fromarrays`/`recarray.__new__` 内部都会用到，请把它当作「保 record 语义」的底层约定。

如果你对 `fromarrays`/`fromrecords`/`fromstring`/`fromfile`/`recarray.__new__` 任一还不熟，建议先读 u2-l3、u4-l1、u4-l2、u4-l3、u3-l1 再回来。

## 3. 本讲源码地图

本讲只涉及一个真实实现文件，外加一个测试文件用于实践：

| 文件 | 作用 |
| --- | --- |
| `numpy/_core/records.py` | `array` 函数及其调度目标的全部真实实现。`array` 定义在文件末尾（约 943 行起）。 |
| `numpy/_core/tests/test_records.py` | `TestFromrecords` 等测试类，含 `test_method_array`（bytes 分支）、`test_method_array2`（list-of-tuple 分支）、`test_recarray_fromfile`（file 分支），是本讲实践的依据。 |

> 提醒：`numpy/rec/__init__.py` 只是再导出垫片（见 u1-l1）。下文所有永久链接都指向真实实现 `numpy/_core/records.py`。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 `array`：统一调度函数的职责与签名** —— 它是谁、为什么存在、整体两段式结构。
- **4.2 dtype/formats 预解析与 kwds 透传** —— 进入分支前的「类型描述预处理」三选一。
- **4.3 obj 类型分支判定** —— 七条路径的完整判定顺序与每条的语义。

### 4.1 array：统一调度函数的职责与签名

#### 4.1.1 概念说明

numpy.rec 提供了 5 个「专注于一种数据来源」的构造函数：

- `fromarrays` —— 从**列方向**数组列表构建（u2-l3）。
- `fromrecords` —— 从**行方向**记录列表构建（u4-l1）。
- `fromstring` —— 从 **bytes 缓冲**构建（u4-l2）。
- `fromfile` —— 从**二进制文件**读取（u4-l3）。
- `recarray(...)` —— 直接按 shape/dtype 分配空数组（u3-l1）。

问题是：用户拿到一段数据时，往往并不想先判断它属于哪一类，再去背对应的函数名。`np.rec.array` 就是为了消除这个心智负担——**你只管把数据丢进来，我来替你选路**。它对外像一个「万能构造器」，对内是一个 type-dispatch（按类型分派）的开关。

它的函数签名接收 11 个参数，目的是把上面 5 个函数的参数集做并集：

```python
def array(obj, dtype=None, shape=None, offset=0, strides=None, formats=None,
          names=None, titles=None, aligned=False, byteorder=None, copy=True):
```

见 [numpy/_core/records.py:943-944](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L943-L944)：`obj` 是输入数据，其余参数要么描述类型（`dtype`/`formats`/`names`/`titles`/`aligned`/`byteorder`），要么描述内存布局（`shape`/`offset`/`strides`），`copy` 仅在 `obj` 是数组类时生效。

#### 4.1.2 核心流程

`array` 的执行可以看成严格的两段：

```
┌─────────────────────────────────────────────────────────┐
│ 阶段一：类型描述预处理（pre-parse）                      │
│   dtype 给了？  → sb.dtype(dtype) 直接得到 descr         │
│   否则 formats 给了？ → format_parser(...) 得到 descr    │
│   否则         → 不解析，把 formats/names/... 塞进 kwds  │
│                  原样下推，交给被调函数自己决定           │
├─────────────────────────────────────────────────────────┤
│ 阶段二：按 obj 类型 dispatch（七条分支，顺序判定）        │
│   None → recarray(...)                                   │
│   bytes → fromstring(...)                                │
│   list/tuple → 看首元素：tuple/list→fromrecords           │
│                         否则→fromarrays                   │
│   recarray → view(dtype?) + copy?                        │
│   有 readinto（文件）→ fromfile(...)                     │
│   ndarray → view(dtype?) + copy? + .view(recarray)       │
│   其它 → __array_interface__ 探测，sb.array + .view       │
└─────────────────────────────────────────────────────────┘
```

阶段一的「三选一」决定了下游拿到的是「已经算好的 dtype」还是「一袋子原始 kwds」；阶段二只关心 `obj` 是什么类型。两个阶段解耦，是这段代码最值得学的设计点。

#### 4.1.3 源码精读

先看官方 docstring 里对分派规则的文字描述（这是理解意图的最佳入口）：

[numpy/_core/records.py:982-989](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L982-L989) —— 这段 Notes 用自然语言把七条分支讲了一遍：`None` 调 `recarray` 构造器；`bytes` 调 `fromstring`；`list`/`tuple` 看**第一个元素**是不是 ndarray，是则 `fromarrays`，否则 `fromrecords`；`recarray` 做（可选）拷贝并换 formats/names/titles；文件调 `fromfile`；`ndarray` 返回 `obj.view(recarray)`（`copy=True` 时先拷贝）。

注意一处**文档与代码的细微出入**：docstring 说「If obj is a string, then call the fromstring constructor」，但真实代码里分发的是 `bytes`（见 4.3），`str` 并没有对应的成功分支（详见 4.3.8 的陷阱）。读源码时以代码为准。

#### 4.1.4 代码实践

**实践目标**：用 docstring 自带的示例，验证 `array` 对 `ndarray` 与 `list` 两种输入都能产出 record array。

**操作步骤**：

```python
import numpy as np

# (1) ndarray 输入 → 走 ndarray 分支
a = np.array([[1, 2, 3], [4, 5, 6], [7, 8, 9]])
r1 = np.rec.array(a)
print(repr(r1))
print(type(r1))            # <class 'numpy.recarray'>
print(r1.dtype.type)       # numpy.record（经 __array_finalize__ 提升）

# (2) list-of-tuple 输入 + formats → 走 fromrecords 分支
b = [(1, 1), (2, 4), (3, 9)]
c = np.rec.array(b, formats=['i2', 'f2'], names=('x', 'y'))
print(c.x, c.y)
```

**需要观察的现象**：
- `r1` 的 `repr` 以 `rec.array(...)` 开头，且 `type(r1)` 是 `numpy.recarray`。
- `c.x` / `c.y` 能用属性访问取列，dtype 分别是 `<i2` / `<f2`，与 `formats` 一致。

**预期结果**：两个调用都返回 `numpy.recarray`，且字段可点号访问。若现象不符，多半是 numpy 版本过老导致打印格式不同（属正常）。

#### 4.1.5 小练习与答案

**练习 1**：`np.rec.array` 与 `np.rec.fromrecords` 的关系是什么？
**答案**：`array` 是总调度入口；当 `obj` 是「首元素为 tuple/list 的 list/tuple」时，`array` 内部会调用 `fromrecords`。即 `fromrecords` 是 `array` 的众多被调函数之一。

**练习 2**：`array` 的 `copy` 参数默认值是多少？它对哪些输入类型生效？
**答案**：默认 `copy=True`。docstring 明确：「This option only applies when the input is an ndarray or recarray」，即只对 `recarray`/`ndarray` 两条分支生效，对 `bytes`/`list`/文件等无意义。

---

### 4.2 dtype/formats 预解析与 kwds 透传

#### 4.2.1 概念说明

进入类型分支之前，`array` 要先回答一个问题：**用户给的类型描述，要不要现在就解析成 dtype？**

类型描述有三套等价写法（见 u2-l1）：完整 `dtype`、简写 `formats`（+`names`/`titles`/`aligned`/`byteorder`）、或干脆什么都不给（让数据自己说话）。`array` 用一个三选一的分支处理这三种情况。

关键设计在于第三种：**当 `dtype` 和 `formats` 都没给时，`array` 不在本地解析，而是把 `formats`/`names`/`titles`/`aligned`/`byteorder` 五个原始参数打包成一个 `kwds` 字典，原样下推给被调函数**。这种「延迟解析」让 `fromrecords`/`fromarrays` 有机会根据**实际数据**自动推断类型（u4-l1、u2-l3 讲过的自动探测路径）。

#### 4.2.2 核心流程

```
if dtype is not None:        # 优先级最高：直接用 dtype
    dtype = sb.dtype(dtype)  #   → 规整成真正的 dtype 对象
elif formats is not None:    # 次优先：用 formats 现场造 dtype
    dtype = format_parser(formats, names, titles, aligned, byteorder).dtype
else:                        # 都没给：不解析，打包下推
    kwds = {'formats': formats, 'names': names, 'titles': titles,
            'aligned': aligned, 'byteorder': byteorder}
```

优先级是严格的 **dtype > formats > 延迟下推**。注意第二分支里 `byteorder` 在此刻就被 `format_parser` 折进了 `dtype`（u2-l2 讲过 `newbyteorder`），下游函数不会再看到 `byteorder`——这对 4.3 里 `fromfile` 分支的行为有直接影响。

#### 4.2.3 源码精读

[numpy/_core/records.py:1033-1045](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L1033-L1045) 就是上面这段三选一逻辑。`sb` 是模块顶部 `from . import numeric as sb`（[numpy/_core/records.py:11](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L11)）的别名，故 `sb.dtype` 即 `np.dtype`，`sb.asarray`/`sb.array` 即 `np.asarray`/`np.array`。

这段代码里还有一条容易被忽略的**前置守卫**，紧挨在它上面：

[numpy/_core/records.py:1028-1031](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L1028-L1031) —— 当 `obj` 是 `None`、`str`、或「带 `readinto` 的文件对象」，且 `formats` 与 `dtype` 同时为 `None` 时，直接抛 `ValueError("Must define formats (or dtype) if object is None, string, or an open file")`。

这条守卫的意义是：`None`/`str`/文件这三类输入**没有可推断类型的数据**（`None` 没数据，`str` 是文本，文件得按既定布局切分），所以必须强制用户提供 `formats` 或 `dtype`。注意它**只约束这三类**：`bytes`、`list`、`tuple`、`ndarray` 不在其中，因为它们要么自身带类型信息（`bytes` 长度可推 itemsize、`ndarray` 自带 dtype），要么元素可逐个探测（`list`/`tuple`）。

#### 4.2.4 代码实践

**实践目标**：观察「延迟下推」如何让 `fromrecords` 自动推断类型。

**操作步骤**：

```python
import numpy as np

# 既不给 dtype 也不给 formats → array 走第三分支，kwds 下推
r = np.rec.array([(1, 'a', 1.1), (2, 'b', 2.2)], names='id,name,val')
print(r.dtype)
# 对照：显式给 formats → array 在顶部就用 format_parser 解析
r2 = np.rec.array([(1, 'a'), (2, 'b')], formats='i4,S1', names='id,name')
print(r2.dtype)

# 守卫触发：obj=None 且无 dtype/formats
try:
    np.rec.array(None, shape=3)
except ValueError as e:
    print('ValueError:', e)
```

**需要观察的现象**：
- `r.dtype` 自动推断成类似 `[('id', '<i8'), ('name', '<U1'), ('val', '<f8')]`（整型提升到 i8、浮点 f8、字符串按最长长度）。
- `r2.dtype` 严格是 `[('id', '<i4'), ('name', 'S1')]`，由 `formats` 决定。
- 第三段抛出 `ValueError: Must define formats (or dtype) if object is None, string, or an open file`。

**预期结果**：自动推断与显式 `formats` 的 dtype 不同；`None` 无类型描述时报错。具体推断出的整型宽度可能因平台而异（待本地确认精确宽度）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `array` 不在「dtype 和 formats 都没给」时直接报错，而是把参数打包下推？
**答案**：因为 `fromrecords`/`fromarrays` 能从**实际数据**逐列推断类型（如 `[(1,'a')]` 推出 `i8, U1`）。若 `array` 提前报错，就剥夺了自动推断的能力；下推则把「能否推断」的决定权交给真正接触数据的函数。而 `fromstring`/`fromfile`/`None` 无法推断，所以才有 4.2.3 那条前置守卫替它们兜底。

**练习 2**：传入 `np.rec.array(fd, formats='f8,i4', byteorder='<')`（`fd` 是文件）时，`byteorder='<'` 最终被谁消费？
**答案**：被 `array` 顶部阶段一的 `format_parser` 消费，折进 `dtype`。下游 `fromfile` 拿到的是已经带字节序的 `dtype`，不会再单独收到 `byteorder`（见 4.3.5）。这与直接调 `np.rec.fromfile(fd, formats='f8,i4', byteorder='<')` 的最终效果一致，但参数传递路径不同。

---

### 4.3 obj 类型分支判定

#### 4.3.1 概念说明

这是 `array` 的核心：一段 `if-elif-elif-...-else` 链，按 `obj` 的类型逐条匹配，命中即转发。理解它的关键是**顺序敏感**——前面的分支先判，后面的兜底。判定的依据有两类：

- **类型判定**：`obj is None`、`isinstance(obj, bytes)`、`isinstance(obj, (list, tuple))`、`isinstance(obj, recarray)`、`isinstance(obj, ndarray)`。
- **能力判定（鸭子类型）**：`hasattr(obj, 'readinto')`（是不是文件）、`getattr(obj, '__array_interface__', None)`（是不是数组协议对象）。

注意 `recarray` 是 `ndarray` 的子类（u3-l1），所以 `isinstance(obj, recarray)` 必须排在 `isinstance(obj, ndarray)` **之前**，否则 recarray 会被 ndarray 分支吞掉。这正是「顺序敏感」的典型体现。

#### 4.3.2 核心流程：七条分支一览表

| # | 判定条件 | 命中后转发到 | 内存语义 |
| --- | --- | --- | --- |
| 1 | `obj is None` | `recarray(shape, dtype, buf=obj, offset, strides)` | 新分配（类似 `empty`） |
| 2 | `isinstance(obj, bytes)` | `fromstring(obj, dtype, shape, offset, **kwds)` | 与缓冲共享内存，可能只读 |
| 3 | `isinstance(obj, (list, tuple))` 且 `obj[0]` 是 tuple/list | `fromrecords(obj, dtype, shape, **kwds)` | 新分配并逐行填充 |
| 3′ | `isinstance(obj, (list, tuple))` 且 `obj[0]` 不是 tuple/list | `fromarrays(obj, dtype, shape, **kwds)` | 新分配并逐列填充 |
| 4 | `isinstance(obj, recarray)` | `view(dtype?)` + `copy?` | copy=False 时可能就是原对象 |
| 5 | `hasattr(obj, 'readinto')`（文件） | `fromfile(obj, dtype, shape, offset)` | 新分配并 readinto 拷贝 |
| 6 | `isinstance(obj, ndarray)` | `view(dtype?)` + `copy?` + `.view(recarray)` | copy=False 时与原对象共享内存 |
| 7 | else（有 `__array_interface__`） | `sb.array(obj)` + `.view(recarray)` | 经 sb.array 转换 |

注意第 3 行内部还有一次「看首元素」的二级判定，这正是学习目标里强调的「list/tuple 区分 fromrecords 与 fromarrays」。

#### 4.3.3 源码精读：分支 1（None）与分支 2（bytes）

[numpy/_core/records.py:1047-1053](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L1047-L1053)：

- `obj is None`：要求必须给 `shape`，否则抛 `ValueError("Must define a shape if obj is None")`；随后 `recarray(shape, dtype, buf=obj, offset=offset, strides=strides)` —— 注意 `buf=obj` 即 `buf=None`，走 `recarray.__new__` 的「新分配」路径（u3-l1）。
- `isinstance(obj, bytes)`：`fromstring(obj, dtype, shape=shape, offset=offset, **kwds)`。若阶段一走了延迟下推（dtype 与 formats 都 None），这里 `**kwds` 会把 `formats=None` 传给 `fromstring`，触发其内部 `if dtype is None and formats is None: raise TypeError`（u4-l2）——即「bytes 但不给类型」最终由 `fromstring` 报 `TypeError`。

#### 4.3.4 源码精读：分支 3（list/tuple 的二级判定）

[numpy/_core/records.py:1055-1059](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L1055-L1059)：

```python
elif isinstance(obj, (list, tuple)):
    if isinstance(obj[0], (tuple, list)):
        return fromrecords(obj, dtype=dtype, shape=shape, **kwds)
    else:
        return fromarrays(obj, dtype=dtype, shape=shape, **kwds)
```

判定**只看第一个元素** `obj[0]`：

- 首元素是 tuple/list → 整个 `obj` 被当作「行式」数据（每行一条记录），走 `fromrecords`。例：`[(1,'a'), (2,'b')]`。
- 首元素是 ndarray（或标量、字符串等非 tuple/list）→ 整个 `obj` 被当作「列式」数据（每个元素是一整列），走 `fromarrays`。例：`[x1, x2]` 其中 `x1=np.array([1,2,3])`。

这条「看首元素」的规则有个**陷阱**：空列表 `[]` 会因 `obj[0]` 抛 `IndexError`（Python 对空序列取下标 0 的标准行为）。即 `np.rec.array([])` 不会优雅报错，而是 `IndexError`——实践中应避免对空容器调用 `array`。

#### 4.3.5 源码精读：分支 4（recarray）与分支 6（ndarray）

[numpy/_core/records.py:1061-1068](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L1061-L1068)（recarray 分支）与 [numpy/_core/records.py:1073-1080](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L1073-L1080)（ndarray 分支）几乎是孪生逻辑：

```python
# recarray 分支
if dtype is not None and (obj.dtype != dtype):
    new = obj.view(dtype)
else:
    new = obj
if copy:
    new = new.copy()
return new                       # 注意：没有 .view(recarray)

# ndarray 分支
if dtype is not None and (obj.dtype != dtype):
    new = obj.view(dtype)
else:
    new = obj
if copy:
    new = new.copy()
return new.view(recarray)        # 注意：末尾多了 .view(recarray)
```

两处差别只有一行：ndarray 分支末尾多了 `.view(recarray)`。原因是——

- **recarray 分支**：`obj` 本身已是 `recarray`，`view(dtype)` 与 `copy()` 都会保留子类身份（u3-l1 的 `__array_finalize__` 还会把 void 提升为 record），所以无需再 `.view(recarray)`。
- **ndarray 分支**：`obj` 是普通 `ndarray`，`copy()` 出来的也是普通 `ndarray`，必须末尾 `.view(recarray)` 才能转成 record array。

`copy` 语义：

- `copy=True`（默认）：先 `.copy()` 再 view，结果与原对象**内存独立**。
- `copy=False` 且 dtype 匹配：recarray 分支直接 `return obj`（返回的就是原对象本身）；ndarray 分支 `new = obj` 后 `.view(recarray)`，返回一个**与原对象共享内存**的 recarray 视图。改其中一个会影响另一个。

#### 4.3.6 源码精读：分支 5（文件）

[numpy/_core/records.py:1070-1071](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L1070-L1071)：

```python
elif hasattr(obj, 'readinto'):
    return fromfile(obj, dtype=dtype, shape=shape, offset=offset)
```

用 `hasattr(obj, 'readinto')` 鸭子判定文件对象。注意此分支**只透传 `dtype`/`shape`/`offset`，不传 `**kwds`**——但这不会丢信息，因为若用户给过 `formats`/`byteorder`，阶段一已经把它们折进了 `dtype`（见 4.2.5 练习 2）。若 `obj` 是文件却没给 `dtype`/`formats`，4.2.3 的前置守卫已经拦下并报 `ValueError`，所以 `fromfile` 经由 `array` 调用时 `dtype` 必非 None。这与直接调 `np.rec.fromfile`（它内部自己再做 `format_parser`）的参数路径不同，但结果一致。

#### 4.3.7 源码精读：分支 7（else，数组协议兜底）

[numpy/_core/records.py:1082-1089](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L1082-L1089)：

```python
else:
    interface = getattr(obj, "__array_interface__", None)
    if interface is None or not isinstance(interface, dict):
        raise ValueError("Unknown input type")
    obj = sb.array(obj)
    if dtype is not None and (obj.dtype != dtype):
        obj = obj.view(dtype)
    return obj.view(recarray)
```

这是最后一道兜底：任何带 `__array_interface__` 字典的对象（其它库的数组、自定义数组协议对象等）都会被 `sb.array(obj)` 转成 ndarray，再 `.view(recarray)`。注意此分支**不尊重 `copy` 参数**（总是走 `sb.array`）。若对象既没有 `__array_interface__` 又没命中前面任何分支，则抛 `ValueError("Unknown input type")`。

#### 4.3.8 陷阱：str 既不在 bytes 分支，也没有自己的分支

读完七条分支后会发现：**没有任何分支处理 `str`**。docstring 说「string → fromstring」，但代码分发的是 `bytes`。结合 4.2.3 的守卫，`str` 的命运有两种：

- `str` + 无 dtype/formats → 守卫触发，`ValueError("Must define formats (or dtype) ...")`。
- `str` + 有 dtype → 守卫不触发，但 `str` 不命中任何分支，落到 else；`str` 没有 `__array_interface__`，于是 `ValueError("Unknown input type")`。

所以 `np.rec.array('abc', dtype='u1,u1,u1')` 永远失败。这与 `fromstring` 显式拒绝 `str`（u4-l2）一致——Python 3 里 `str` 是 Unicode 文本，字节布局不可直接按 dtype 切分。**要解析二进制文本，请传 `bytes`**。

#### 4.3.9 代码实践

**实践目标**：对照源码分支，验证四种典型输入分别走哪条路径。本实践直接复刻 `test_records.py` 中的真实测试。

**操作步骤**：

```python
import numpy as np
from io import BytesIO

# (A) bytes 输入 → 分支 2 (fromstring)
r_a = np.rec.array(b'abcdefg' * 100, formats='i2,S3,i4', shape=3, byteorder='big')
print('A bytes  ->', r_a[1].item())   # 对照 test_method_array

# (B) list-of-tuple 输入 → 分支 3 → fromrecords
r_b = np.rec.array(
    [(1, 11, 'a'), (2, 22, 'b'), (3, 33, 'c')],
    formats='u1,f4,S1',
)
print('B tuples ->', r_b[1].item())   # 对照 test_method_array2

# (C) 二维 ndarray 输入 → 分支 6 (ndarray)
arr = np.array([[1, 2], [3, 4]])
r_c = np.rec.array(arr)
print('C ndarray->', type(r_c).__name__, r_c.dtype.type.__name__)

# (D) 已打开的文件对象 → 分支 5 (fromfile)
buf = BytesIO()
np.empty(3, dtype='f8,i4,S5').tofile(buf)
buf.seek(0)
r_d = np.rec.array(buf, formats='f8,i4,S5', shape=3)
print('D file   ->', r_d.shape, r_d.dtype.names)
```

**需要观察的现象**（对照 4.3.2 一览表）：

| 输入 | 命中分支 | 转发到 |
| --- | --- | --- |
| (A) `bytes` | 分支 2 | `fromstring` |
| (B) list-of-tuple | 分支 3（首元素是 tuple） | `fromrecords` |
| (C) 二维 ndarray | 分支 6 | `view + copy + .view(recarray)` |
| (D) 文件对象（有 `readinto`） | 分支 5 | `fromfile` |

**预期结果**：
- (A) 打印 `(25444, b'efg', 1633837924)`（与 `test_method_array` 断言一致，大端解释）。
- (B) 打印 `(2, 22.0, b'b')`（与 `test_method_array2` 断言一致）。
- (C) `type` 为 `recarray`，`dtype.type` 为 `record`（注意：非结构化 dtype 经 `__array_finalize__` 后 `dtype.type` 仍是 record，但取列会回退为 ndarray——此处只是验证类型转换发生）。
- (D) `shape=(3,)`，`dtype.names=('f0','f1','f2')`。

**说明**：本实践未运行，断言值取自 `numpy/_core/tests/test_records.py` 的 `test_method_array`（[numpy/_core/tests/test_records.py:57-61](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_records.py#L57-L61)）与 `test_method_array2`（[numpy/_core/tests/test_records.py:63-71](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_records.py#L63-L71)）。若本地结果与断言不符，先检查 numpy 版本与字节序设置。

#### 4.3.10 小练习与答案

**练习 1**：为什么 `isinstance(obj, recarray)` 必须排在 `isinstance(obj, ndarray)` 之前？
**答案**：因为 `recarray` 是 `ndarray` 的子类，任何 `recarray` 实例都满足 `isinstance(_, ndarray)`。若 ndarray 分支在前，recarray 输入会被它吞掉，走「末尾 `.view(recarray)`」的冗余路径（虽然结果类型不变，但语义上应优先走 recarray 专属分支，且二者 copy 行为细节不同）。

**练习 2**：`np.rec.array([(1,2),(3,4)])` 与 `np.rec.array([np.array([1,3]), np.array([2,4])])` 分别走哪条分支？
**答案**：前者首元素 `(1,2)` 是 tuple → 分支 3 的 `fromrecords`（行式，2 行 2 列）；后者首元素 `np.array([1,3])` 是 ndarray（非 tuple/list）→ 分支 3′ 的 `fromarrays`（列式，2 列各 2 元素）。两者最终都是 shape `(2,)` 的 record array，但数据组装方向相反。

**练习 3**：`np.rec.array(some_recarray, copy=False)` 在 dtype 匹配时返回的对象与输入是什么关系？
**答案**：`new = obj`（不 view 不 copy），直接 `return obj`——返回的就是输入对象本身（`is` 判定为同一对象）。若 `copy=True` 则返回独立拷贝。

---

## 5. 综合实践

**任务**：写一个小函数 `rec_from_anything(obj, **kw)`，它内部直接调用 `np.rec.array`，但要求**在调用前用你自己的代码预测它将走哪条分支**，并把预测打印出来，最后与真实返回的类型对照。

**目标**：把本讲的「七条分支判定」内化为可编码的判断逻辑。

**参考实现（示例代码，非项目原有代码）**：

```python
import numpy as np

def predict_branch(obj):
    if obj is None:
        return 'branch1: recarray(...) [需 shape]'
    if isinstance(obj, bytes):
        return 'branch2: fromstring'
    if isinstance(obj, (list, tuple)):
        if isinstance(obj[0], (tuple, list)):
            return 'branch3  fromrecords (行式)'
        return "branch3' fromarrays (列式)"
    if isinstance(obj, np.recarray):
        return 'branch4: view(dtype?)+copy?'
    if hasattr(obj, 'readinto'):
        return 'branch5: fromfile'
    if isinstance(obj, np.ndarray):
        return 'branch6: view+copy+.view(recarray)'
    return 'branch7: __array_interface__ / Unknown'

def rec_from_anything(obj, **kw):
    print('预测 ->', predict_branch(obj))
    r = np.rec.array(obj, **kw)
    print('实际 ->', type(r).__name__, '| dtype.type =', r.dtype.type.__name__)
    return r

# 验证四种输入
rec_from_anything([(1,'a'),(2,'b')], formats='i4,S1', names='id,s')
rec_from_anything(np.array([[1,2],[3,4]]))
rec_from_anything(b'\x01\x02\x03abc', dtype='u1,u1,u1,S3')
```

**验收标准**：
1. 每行的「预测」与「实际」分支语义一致（实际类型恒为 `recarray`）。
2. 你能解释为什么 `predict_branch` 里 `recarray` 判定也在 `ndarray` 之前（与源码顺序保持一致）。
3. 把 `[(1,'a'),(2,'b')]` 换成 `[np.array([1,2]), np.array(['a','b'])]`，预测应从 `fromrecords` 变为 `fromarrays`，并验证 `r` 的字段数与列数一致。

**延伸思考（选做）**：尝试让 `predict_branch` 复现 4.2.3 的前置守卫——即对 `None`/`str`/文件且无 dtype/formats 的情况提前报错，观察是否与 `np.rec.array` 的报错信息一致。

## 6. 本讲小结

- `np.rec.array` 是 numpy.rec 的**总调度入口**：自身几乎不构造数据，按 `obj` 类型把请求转发给 `recarray`/`fromstring`/`fromrecords`/`fromarrays`/`fromfile`。
- 执行分两段：**阶段一**做类型描述预处理（dtype > formats > 打包 kwds 下推），**阶段二**按 `obj` 类型走七条 `if-elif` 分支。
- 「dtype 与 formats 都没给」时不在本地报错，而是把五个原始参数塞进 `kwds` **延迟下推**，让 `fromrecords`/`fromarrays` 有机会自动推断类型；`None`/`str`/文件因无法推断，被前置守卫强制要求 `formats` 或 `dtype`。
- `list`/`tuple` 输入看**首元素**：是 tuple/list → `fromrecords`（行式），否则 → `fromarrays`（列式）；空列表会因 `obj[0]` 抛 `IndexError`。
- `recarray` 分支与 `ndarray` 分支是孪生逻辑，唯一差别是 ndarray 分支末尾多了 `.view(recarray)`；`copy=False` 时二者都可能返回与原对象共享内存的结果。
- 分支顺序敏感：`isinstance(obj, recarray)` 必须排在 `isinstance(obj, ndarray)` 之前；`str` 没有成功分支，二进制要用 `bytes`。

## 7. 下一步学习建议

本讲讲完 `array` 总调度，意味着 u4 单元（构建函数族）已收口。建议：

1. **进入 u5-l1（视图、拷贝与 record/void dtype 转换）**：本讲多次提到 `view(recarray)`、`copy`、二元 dtype `(record, void_dtype)`，u5-l1 会把这些内存与类型转换机制讲透，是本讲 4.3.5 的自然延伸。
2. **重读 u3-l1 的 `__array_finalize__`**：本讲反复依赖「view/copy 后 dtype.type 自动变成 record」这一钩子，回去对照三条 AND 条件会更有体会。
3. **阅读 `numpy/_core/records.py` 的 `array` 与四个 `from*` 函数**：把它们当作一个「调度器 + 五个专精实现」的小型架构案例，体会「宽入口、窄实现」的设计权衡。
