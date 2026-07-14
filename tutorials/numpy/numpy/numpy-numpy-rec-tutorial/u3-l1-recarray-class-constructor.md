# recarray 类的 `__new__` 构造与 `__array_finalize__`

> 本讲属于「进阶：recarray 与 record 两个核心类」单元的第一篇。
> 前置讲义：`u1-l3`（五分钟创建你的第一个 record array）。

---

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清 `recarray` 与 `ndarray` 的继承关系，并理解「几乎一样，只是多了属性访问」这句话在源码层面的确切含义。
2. 读懂 `recarray.__new__` 的两条构造路径（`buf=None` 分配新内存 / `buf` 复用现有缓冲），以及 `dtype` 与 `format_parser` 的优先级。
3. 解释为什么构造 record array 时要把 dtype 写成 `(record, descr)` 这种「二元 dtype」，以及它如何把结构化 dtype 的标量类型从 `numpy.void` 改写成 `numpy.record`。
4. 理解 `__array_finalize__` 钩子与 `_set_dtype = None` 的协作：为什么 `arr.view(recarray)` 能在绕过 `__new__` 的情况下，仍然把 `void` dtype 自动「提升」为 `record`。

---

## 2. 前置知识

本讲默认你已经掌握 `u1` 单元和 `u2` 单元的内容。为避免你来回切换，这里把最关键的几条直觉浓缩如下：

- **字段（field）与结构化 dtype**：结构化 dtype 由若干「字段」组成，每个字段有名字、子 dtype、字节偏移，可选「标题（title）」。`dtype.names` 给字段名列表，`dtype.fields` 给 `{名字: (子dtype, 偏移[, 标题])}` 字典。
- **普通结构化数组 vs record array**：普通 `ndarray` 已经支持字典式取列 `arr['x']`；`recarray` 是 `ndarray` 的子类，额外允许属性式取列 `arr.x`，并且取单条记录 `arr[0]` 时返回 `numpy.record` 标量（普通数组返回 `numpy.void` 标量）。
- **`format_parser`**：把人友好的 `formats / names / titles / aligned / byteorder` 描述翻译成严格的 `dtype`，结果挂在 `.dtype` 属性上（详见 `u2-l1`、`u2-l2`）。
- **真实实现位置**：`numpy/rec/__init__.py` 只是再导出垫片，本讲引用的全部源码都在 [`numpy/_core/records.py`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py)。

此外，本讲会用到两个 NumPy 子类化的术语，先给一句话解释：

- **`__new__`**：Python 中创建实例的「构造」方法。`ndarray` 及其子类习惯把所有构造逻辑放在 `__new__` 里（而不是 `__init__`），因为数组的内存布局在创建时就必须定死。
- **`__array_finalize__`**：`ndarray` 子类化的「收尾钩子」。每当 NumPy 内部通过切片、视图、`view()`、`from-template` 等方式「派生」出一个新数组时，都会调用这个钩子，让子类有机会补全自己的状态。

> 一句话定位：`__new__` 负责「从头造一个」，`__array_finalize__` 负责「从已有数组派生一个」时把状态补齐。record array 之所以在两种途径下都能保持 `dtype.type == record`，正是这两个方法分工合作的结果。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [numpy/_core/records.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py) | 本讲全部核心实现：`record` 类（标量）、`recarray` 类（数组）、`__new__`、`__array_finalize__` 都在这里。 |
| [numpy/_core/numerictypes.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/numerictypes.py) | 提供 `nt.void`（`numpy.void` 标量类型）。`record` 继承自它，`__array_finalize__` 用 `issubclass(..., nt.void)` 判定是否需要提升。 |
| [numpy/_core/tests/test_records.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_records.py) | `test_recarray_views`、`test_assign_dtype_attribute`、`test_nested_fields_are_records` 等用例，是本讲实践与论断的依据。 |

本讲聚焦的最小模块有三个，对应第 4 节的 4.1 / 4.2 / 4.3：

1. **`recarray(ndarray)` 子类** —— 继承关系与 `(record, descr)` 二元 dtype。
2. **`__new__` 构造** —— dtype 解析与 `buf` 两条内存路径。
3. **`__array_finalize__` 钩子** —— `void → record` 的自动提升。

---

## 4. 核心概念与源码讲解

### 4.1 `recarray(ndarray)` 子类：继承关系与 `(record, descr)` 二元 dtype

#### 4.1.1 概念说明

`recarray` 的源码注释用一句话定调：

> The recarray is almost identical to a standard array (which supports named fields already). The biggest difference is that it can use attribute-lookup to find the fields and it is constructed using a record.

也就是说，`recarray` 在内存布局上和普通结构化 `ndarray` **完全一样**——同样的字节、同样的字段偏移、同样的形状。它的「特异功能」只有两点：

- 取列用属性 `arr.x` 而不是字典 `arr['x']`（由 `__getattribute__` 实现，留到 `u3-l2`）。
- 取单条记录 `arr[0]` 返回 `numpy.record` 标量，而不是 `numpy.void` 标量。

第二点正是本讲的焦点。普通结构化数组的标量类型是 `numpy.void`，它**不支持**属性访问（你只能 `arr[0]['x']`）。要让 `arr[0].x` 也能用，就必须把 dtype 的「标量类型」从 `void` 换成它的子类 `record`。

NumPy 的 dtype 对象有一个 `.type` 属性，表示「从这个 dtype 取出的标量用什么 Python 类型来表示」。结构化 dtype 默认 `.type` 是 `numpy.void`。NumPy 提供了一种「改写标量类型」的写法——**二元 dtype**：

```python
dtype((scalar_type, flexible_dtype))
```

它表示「结构和 `flexible_dtype` 一模一样，但标量类型改用 `scalar_type`」。于是 `recarray` 把自己的 dtype 构造成：

```python
sb.dtype((record, descr))
```

得到一个「结构和 `descr` 完全相同、但 `.type == numpy.record`」的 dtype。这样数组级 `r.x` 和标量级 `r[0].x` 才会同时生效。

> 直觉总结：`recarray` ≈ 结构化 `ndarray` + 一个被「换芯」过的 dtype（标量类型 `void` → `record`）。

#### 4.1.2 核心流程

继承与类型关系可以画成下面这样：

```
        Python 类继承                  dtype.type 改写
        ─────────────                  ──────────────
   record  ──继承──▶  nt.void          descr.type = void
     ▲                                 │  sb.dtype((record, descr))
     │                                 ▼
   (作为 dtype.type)               new dtype.type = record
     │                                 │
   recarray  ──继承──▶  ndarray    ◀──┘  recarray 实例持有这个新 dtype
```

关键事实清单：

- `recarray` 直接继承 `ndarray`，因此 `isinstance(r, np.ndarray)` 为真，所有普通数组方法都可用。
- `record` 直接继承 `nt.void`，因此 `isinstance(rec_scalar, np.void)` 为真，但多了属性访问。
- `recarray` 实例的 `dtype.type` **通常是** `numpy.record`（除非是 subarray / 非 void 结构等特殊情况，见 4.3）。
- 类头上的 `@set_module("numpy.rec")` 装饰器把 `__module__` 改写成 `numpy.rec`，所以 `np.rec.recarray` 这个名字才成立（物理上代码在 `_core`，对外却显示在 `numpy.rec` 命名空间）。`record` 类则**手动**把 `__module__` 设成 `'numpy'`，因此正式地址是 `numpy.record`。

#### 4.1.3 源码精读

**类定义与模块归属**（注意第 278–279 行的装饰器与继承）：

[numpy/_core/records.py:L278-L279](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L278-L279) —— `@set_module("numpy.rec")` 装饰 `class recarray(ndarray)`：物理定义在 `_core`，对外归属 `numpy.rec`。

**类注释定调**（「几乎和普通数组一样」）：

[numpy/_core/records.py:L269-L276](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L269-L276) —— 说明 recarray 与标准数组的唯一实质差别是「属性查找字段 + 用 record 构造」。

**`record` 标量类型与模块来源**（为什么 `np.record` 这个名字成立）：

[numpy/_core/records.py:L196-L203](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L196-L203) —— `class record(nt.void)`，并手动设置 `__name__ = 'record'`、`__module__ = 'numpy'`，使其打印为 `numpy.record`。这是 4.1.1 中「二元 dtype」里那个 `record` 的真实来源。

**`numerictypes` 的导入**（`nt.void` 从哪来）：

[numpy/_core/records.py:L11](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L11) —— `from . import numeric as sb, numerictypes as nt`。后文所有 `sb.dtype`、`sb.asarray`、`nt.void` 都源于此；`sb` 即 `numpy._core.numeric`（`np` 的多数函数所在），`nt` 即 `numpy._core.numerictypes`。

#### 4.1.4 代码实践

**实践目标**：用肉眼确认 `recarray` 既是 `ndarray` 的子类，又拥有被「换芯」过的 dtype。

**操作步骤**：

```python
import numpy as np

# 1) 普通结构化 ndarray：标量类型是 void
a = np.array([(1, 2.0), (3, 4.0)], dtype=[('x', 'i4'), ('y', 'f8')])
print(type(a))            # <class 'numpy.ndarray'>
print(a.dtype.type)       # <class 'numpy.void'>

# 2) 转成 recarray：标量类型变成 record
r = a.view(np.recarray)
print(type(r))            # <class 'numpy.recarray'>
print(r.dtype.type)       # <class 'numpy.record'>

# 3) 继承关系
print(isinstance(r, np.ndarray))   # True —— recarray 是 ndarray 子类
print(isinstance(r[0], np.void))   # True —— record 是 void 子类
print(type(r[0]))                  # <class 'numpy.record'>
```

**需要观察的现象**：

- 第 1 步里 `a.dtype.type` 是 `numpy.void`，普通结构化数组无法 `a.x`。
- 第 2 步里 `r.dtype.type` 变成 `numpy.record`；注意 `r.dtype == a.dtype` 仍为 `True`（结构完全相同，只有 `.type` 不同）。
- 第 3 步确认 `recarray` 与 `record` 都「是」其父类的子类。

**预期结果**：与上面注释一致。`view(recarray)` 把 dtype 的标量类型从 `void` 改写成 `record`（具体机制在 4.3 展开）。

#### 4.1.5 小练习与答案

**练习 1**：`r.dtype == a.dtype` 为真，但 `r.dtype.type != a.dtype.type`。请解释这两个 dtype 到底「哪里相同、哪里不同」。

> **参考答案**：两者结构（`names` / `formats` / `offsets` / `itemsize`）完全相同，所以用 `==` 比较为真；不同之处仅在于 `.type` 属性——普通数组是 `numpy.void`，recarray 是 `numpy.record`。`.type` 不参与 dtype 的 `==` 比较。

**练习 2**：为什么不能直接 `r = np.recarray(...)` 之后用一个 `dtype=[('x','i4')]`（即 `.type` 仍是 `void`）就完事？非要绕一圈 `(record, descr)` 的意义是什么？

> **参考答案**：因为「标量级属性访问 `r[0].x`」是由 `record` 类的 `__getattribute__` 提供的（见 `u3-l3`）。如果 dtype 的标量类型仍是 `void`，那么 `r[0]` 就是一个普通 `void` 标量，没有 `.x` 这个属性。把 `.type` 改成 `record`，是为了让取出的标量也是 `record` 实例，从而标量级属性访问一并生效。

---

### 4.2 `__new__` 构造：dtype 解析与 `buf` 两条内存路径

#### 4.2.1 概念说明

`recarray.__new__` 是「从头造一个 record array」的唯一入口。它要做的事情可以拆成两步：

1. **确定 dtype**：要么用户直接给了 `dtype=`，要么用 `formats/names/titles/aligned/byteorder` 五元组让 `format_parser` 现场拼一个出来。
2. **分配内存**：要么新申请一块内存（`buf=None`），要么复用调用方提供的缓冲（`buf=`），后者还需要 `offset` 与 `strides` 来定位。

注意 `recarray.__new__` 的定位和 `np.empty` 类似——**它只负责「造一个空壳」，不负责装数据**。官方 docstring 的 Notes 段写得很清楚：要装数据，请走 `arr.view(np.recarray)`、`buf` 关键字、或 `np.rec.fromrecords`。

第 2 步有两条路径，对应两种典型用法：

- **`buf=None`（新分配）**：调用 `ndarray.__new__(cls, shape, (record, descr), order=order)`。这是「造一个全新的空 record array」，内存里是未初始化的随机字节。
- **`buf` 已给（复用缓冲）**：调用 `ndarray.__new__(cls, shape, (record, descr), buffer=buf, offset=offset, strides=strides, order=order)`。这是 `fromstring`、`fromfile` 等函数「把一段已存在的字节当 record array 来读」的底层支撑。

两条路径都把 dtype 传成 `(record, descr)`——这正是 4.1 里讲的「换芯」操作。也就是说，**`__new__` 一手包办了「让 dtype.type 变成 record」这件事**，前提是数据确实经由 `__new__` 进入。

#### 4.2.2 核心流程

`__new__` 的执行流程（伪代码）：

```
function __new__(cls, shape, dtype?, buf?, offset?, strides?,
                 formats?, names?, titles?, byteorder?, aligned?, order='C'):

    # 第一步：解析出「描述符」descr（一个普通结构化 dtype，type 仍是 void）
    if dtype is not None:
        descr = sb.dtype(dtype)                       # 优先级最高：直接给 dtype
    else:
        descr = format_parser(formats, names, titles, # 否则用 format_parser 拼
                              aligned, byteorder).dtype

    # 第二步：分配内存，并把 dtype「换芯」为 (record, descr)
    if buf is None:
        self = ndarray.__new__(cls, shape, (record, descr), order=order)
    else:
        self = ndarray.__new__(cls, shape, (record, descr),
                               buffer=buf, offset=offset,
                               strides=strides, order=order)
    return self
```

两个要点：

- **dtype 优先级**：`dtype` > `formats`。给了 `dtype` 就直接用；否则才走 `format_parser`。这与 `fromarrays` / `fromrecords` 等 `from*` 函数的优先级一致（详见 `u2-l3`）。
- **`(record, descr)` 始终作为 dtype.type 覆盖**：无论哪条内存路径，传给 `ndarray.__new__` 的 dtype 都是 `(record, descr)`，所以经由 `__new__` 造出来的数组，`dtype.type` 一开始就是 `record`。

> 与 `format_parser` 的衔接回顾（`u2-l1`/`u2-l2`）：`format_parser` 只产出**结构化 dtype**（`.type` 是 `void`），它不关心标量类型；把 `void` 换成 `record` 是 `__new__` 在这一步用 `(record, descr)` 完成的。

#### 4.2.3 源码精读

**`__new__` 签名**（参数全集）：

[numpy/_core/records.py:L385-L387](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L385-L387) —— 注意 `shape` 是必填位置参数，`dtype`/`buf`/`formats` 等都可选。

**第一步：解析 dtype（`dtype` 与 `format_parser` 二选一）**：

[numpy/_core/records.py:L389-L394](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L389-L394) —— 若给了 `dtype` 则 `descr = sb.dtype(dtype)`；否则交给 `format_parser(formats, names, titles, aligned, byteorder)`。

**第二步：两条内存路径**：

[numpy/_core/records.py:L396-L403](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L396-L403) —— `buf is None` 时直接按 `shape` 与 `(record, descr)` 新建；`buf` 已给时把 `buffer=buf, offset=offset, strides=strides` 透传给 `ndarray.__new__`，复用现有缓冲。

**docstring 里关于「构造器 ≈ empty」的说明**：

[numpy/_core/records.py:L342-L352](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L342-L352) —— 明确说 `__new__` 只造空壳、不装数据，并列出三种「装数据」的替代方式（`view(recarray)` / `buf` / `fromrecords`）。

#### 4.2.4 代码实践

**实践目标**：直接用 `np.recarray(...)` 造一个空 record array，逐元素赋值，并确认 `dtype.type` 是 `numpy.record`。这是本讲规格里指定的实践。

**操作步骤**：

```python
import numpy as np

# 用 __new__ 直接构造一个「空」record array（3 行，两个字段 x/y）
r = np.recarray(3, dtype=[('x', 'f8'), ('y', 'i4')])
print(type(r))            # <class 'numpy.recarray'>
print(r.dtype)            # [('x', '<f8'), ('y', '<i4')]
print(r.dtype.type)       # <class 'numpy.record'>   ← 关键

# 逐元素（逐条记录）赋值——此时数组原本是未初始化的随机字节
r[0] = (1.5, 10)
r[1] = (2.5, 20)
r[2] = (3.5, 30)

# 属性访问取列
print(r.x)                # [1.5 2.5 3.5]
print(r.y)                # [10 20 30]

# 取单条记录，确认标量类型是 record
print(type(r[0]))         # <class 'numpy.record'>
print(r[0].x, r[0].y)     # 1.5 10
```

**需要观察的现象**：

- 构造完成后立刻打印 `r`，里面的数值是「垃圾值」（未初始化内存），印证 docstring「相当于 `empty`」的说法。
- `r.dtype.type` 直接就是 `numpy.record`——这是 `__new__` 里 `(record, descr)` 起的作用，**无需经过 `__array_finalize__`**（见 4.3 对比）。
- 赋值后再取列、取标量，属性访问全部可用。

**预期结果**：与上面注释一致。赋值前 `r.x` 是三个随机浮点数（待本地验证具体值，因未初始化内存不可预测）。

> 补充实践（对应 `buf` 路径，帮助理解 4.2.3 第二条分支）：
> ```python
> raw = np.ones(4, dtype='f8,i4')          # 普通结构化数组，4 条记录
> r2 = np.recarray(raw.shape, dtype=raw.dtype, buf=raw.data)
> print(r2.dtype.type)                      # numpy.record（buf 路径同样换芯）
> ```
> 这里把 `raw.data`（缓冲）交给 `recarray` 复用，结果与 `raw` 共享内存。完整二进制读取流程见 `u4-l2` / `u4-l3`。

#### 4.2.5 小练习与答案

**练习 1**：如果同时给了 `dtype` 和 `formats`，`__new__` 会用哪个？为什么？

> **参考答案**：用 `dtype`。源码第 389–394 行是 `if dtype is not None: descr = sb.dtype(dtype) else: ... format_parser(...)`，`dtype` 分支优先，`formats` 只在 `dtype is None` 时才被 `format_parser` 消费。

**练习 2**：`np.recarray(3, dtype=[('x','f8')])` 构造出来的数组，`dtype.type` 是什么？这个结果是由 `__new__` 直接产生的，还是由 `__array_finalize__` 事后补救的？

> **参考答案**：是 `numpy.record`，且是 `__new__` 直接产生的。因为 `__new__` 第二步把 dtype 写成了 `(record, descr)`，创建出来的数组天生 `.type == record`；随后 `__array_finalize__` 虽然也会被调用，但其 `if self.dtype.type is not record` 条件为假，不会再做任何事（详见 4.3）。

**练习 3**：`fromstring` / `fromfile` 都不直接 `np.recarray(shape, dtype=...)`，而是 `np.recarray(shape, descr, buf=..., offset=...)`。结合本节，解释它们为什么要走 `buf` 路径而不是 `buf=None` 路径。

> **参考答案**：因为 `fromstring` / `fromfile` 要把「已经存在的字节」（一段 `bytes` 或文件内容）当作 record array 来解释，而不是新申请一块空内存。走 `buf` 路径能让 `recarray` 复用那段缓冲，避免拷贝；`offset` 用于从缓冲中段开始读。`shape` 由调用方（按 `itemsize` 推断）事先算好再传入。

---

### 4.3 `__array_finalize__` 钩子：`void → record` 的自动提升

#### 4.3.1 概念说明

4.2 解决了「经由 `__new__` 创建」的情形。但 record array 还有一条更常用的创建路径——`arr.view(np.recarray)`，以及切片、`from-template` 等派生操作。**这些路径不经过 `recarray.__new__`**，新数组的 dtype 直接继承自源数组。如果源数组是普通结构化 `ndarray`，那派生出来的 recarray 的 `dtype.type` 就仍是 `void`，属性访问会部分失效。

`__array_finalize__` 就是为这种「绕过 `__new__`」的场景准备的收尾钩子。它的职责很窄：**检查新派生数组的 dtype，如果它是一个「带字段的 `void` 类型」却不是 `record`，就把它提升成 `record`**。

这里有一个关键的类属性配合：`_set_dtype = None`。它是 NumPy 子类化的一个约定信号——告诉底层 C 机制：「当需要在本类实例上改写 dtype 时，不要调用某个 `_set_dtype` 方法，而是创建新视图后调用 `__array_finalize__`，由它来处理 dtype 变化」。这样 `recarray` 才能在 `view()` / 切片等操作之后，通过 `__array_finalize__` 把 dtype 修正成 `(record, descr)` 形态。

> 一句话：`__new__` 让「新建」的数组天生是 record；`__array_finalize__` 让「派生」的数组也被补成 record。两者共同保证：无论哪条路径，`dtype.type` 都稳定为 `record`。

#### 4.3.2 核心流程

`__array_finalize__` 的判定逻辑只有三条 AND 条件，全部满足才提升：

```
function __array_finalize__(self, obj):   # obj 是派生来源（可能为 None）
    if  self.dtype.type is not record              # ① 还不是 record
        and issubclass(self.dtype.type, nt.void)   # ② 但是个 void 家族成员
        and self.dtype.names is not None:          # ③ 且是「带字段」的结构化类型
            # 把 dtype 换芯为 (record, 当前 dtype)
            ndarray._set_dtype(self, sb.dtype((record, self.dtype)))
```

为什么三条缺一不可：

- **① `is not record`**：如果已经是 `record`（比如经由 `__new__` 直接创建，或已经被提升过），就不用再动，避免无限递归 / 无谓重写。
- **② `issubclass(..., nt.void)`**：只对 void 家族的结构化标量提升。非 void 类型（如 subarray `('f4', 2)` 的 `.type` 是 `numpy.float32`，或非结构化 dtype）不提升——它们本来就没有「字段属性访问」的概念。
- **③ `names is not None`：必须是「带字段」的结构化类型。裸的 `V8`（8 字节未结构化 void）虽然 `.type` 是 `void`，但 `names is None`，不提升——它没有字段可访问，转成 record 没有意义。

满足三条时，调用 **`ndarray._set_dtype(self, sb.dtype((record, self.dtype)))`** 完成「就地改写 dtype」。注意这里刻意调用的是基类 `ndarray._set_dtype`（内部就地 dtype 设置器），而不是 `self.dtype = ...`——后者在新版 NumPy 里会触发 `DeprecationWarning`（见 `test_assign_dtype_attribute` 用例）。

提升前后 dtype 的关系：

\[ \texttt{descr}_\text{原}\ \xrightarrow{\ \texttt{dtype((record, descr}_\text{原}\texttt{))}\ }\ \texttt{descr}_\text{新},\quad \text{结构不变，仅 } \texttt{.type}: \texttt{void}\to\texttt{record} \]

#### 4.3.3 源码精读

**`_set_dtype = None` 信号**：

[numpy/_core/records.py:L405](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L405) —— 注释明说「`__array_finalize__` can deal with dtype changes」。这是告诉 NumPy C 层：dtype 改写交给 `__array_finalize__` 处理。

> （旁注，便于理解）NumPy 的 C 层 [`numpy/_core/src/multiarray/convert.c`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/multiarray/convert.c) 在判定子类如何响应 dtype 变化时，区分三种情况：`_set_dtype is None`（走 `__array_finalize__`）、子类覆写了 `_set_dtype`（调用它）、覆写了 `dtype` setter（旧路，已弃用）。`recarray` 选第一种。

**`__array_finalize__` 主体**：

[numpy/_core/records.py:L407-L413](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L407-L413) —— 三条件判断 + `ndarray._set_dtype(self, sb.dtype((record, self.dtype)))`。

**测试依据 1（view 路径自动变 record）**：

[numpy/_core/tests/test_records.py:L199-L202](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_records.py#L199-L202) —— `a.view(np.recarray).dtype.type == np.record`，正是 `__array_finalize__` 提升的结果。

**测试依据 2（赋值 dtype 后仍是 record）**：

[numpy/_core/tests/test_records.py:L493-L504](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_records.py#L493-L504) —— `test_assign_dtype_attribute`：把一个 `void` 型 dtype 赋给 recarray 后，断言 `data.dtype.type == np.record` 仍成立，印证「void 结构化类型被自动提升为 record」。

**测试依据 3（不提升的边界情况）**：

[numpy/_core/tests/test_records.py:L225-L232](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_records.py#L225-L232) —— 嵌套结构 `('f4,f4')` 字段提升为 `record`，但裸 `V8` 字段保持 `void`、subarray `('f4',2)` 字段保持 `float32`、`('i8','i4,i4')` 的标量是 `int64`。这正是条件 ②③ 在起作用。

#### 4.3.4 代码实践

**实践目标**：亲眼看到 `view(recarray)` 在绕过 `__new__` 的情况下，由 `__array_finalize__` 把 `void` 提升为 `record`；并能用 `view(ndarray)` 反向「褪色」。

**操作步骤**：

```python
import numpy as np

# 1) 普通结构化 ndarray，dtype.type 是 void
a = np.ones(4, dtype='f4,i4')
print(a.dtype.type)            # <class 'numpy.void'>

# 2) view 成 recarray：不经过 __new__，但 __array_finalize__ 把 void 提升为 record
r = a.view(np.recarray)
print(r.dtype.type)            # <class 'numpy.record'>
print(r.f0)                    # [1. 1. 1. 1.]   属性访问可用

# 3) 裸 V8 字段不提升：names is None，命中条件 ③ 失败
v = np.zeros(3, dtype=[('blob', 'V8'), ('s', 'f4,f4')])
rv = v.view(np.recarray)
print(rv.blob.dtype.type)      # <class 'numpy.void'>   ← 没被提升
print(rv.s.dtype.type)         # <class 'numpy.record'> ← 嵌套结构被提升

# 4) 反向：recarray 再 view 回 ndarray，标量类型褪回 void
back = r.view(np.ndarray)
print(back.dtype.type)         # <class 'numpy.void'>
```

**需要观察的现象**：

- 第 2 步：`view(recarray)` 后 `dtype.type` 由 `void` 变成 `record`，且 `r.f0` 能用——这是 `__array_finalize__` 在 `__new__` 未参与的情况下补救的结果。
- 第 3 步：`blob`（`V8`）保持 `void`，因为它没有字段（`names is None`）；`s`（`f4,f4`）是嵌套结构，被提升为 `record`。
- 第 4 步：`view(np.ndarray)` 后 `dtype.type` 褪回 `void`，说明「换芯」是 recarray 专属行为，普通 ndarray 不做这件事。

**预期结果**：与上面注释一致。第 3 步是本讲最容易出错的地方，请务必亲手验证 `V8` 不提升而 `f4,f4` 提升。

> ⚠️ 若无法运行 NumPy，相关断言可参照 [`test_records.py:L225-L232`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_records.py#L225-L232) 的预期，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：把 `__array_finalize__` 的三个条件去掉任意一个，分别会造成什么问题？

> **参考答案**：
> - 去掉①（不判断 `is not record`）：对已经是 `record` 的数组重复执行 `(record, descr)`，虽不报错但做无用功，且在某些派生链里可能反复触发，浪费性能。
> - 去掉②（不判断 `issubclass(void)`）：会把 `float32`、`int64` 等非 void 标量也包进 `(record, ...)`，产生非法 dtype（`record` 只能搭配 flexible/void 类 dtype），报错。
> - 去掉③（不判断 `names is not None`）：会把裸 `V8` 这种无字段 void 也提升，但 `record` 的属性访问依赖 `dtype.fields`，对无字段类型毫无意义，徒增混乱。

**练习 2**：为什么 `__array_finalize__` 里用 `ndarray._set_dtype(self, ...)` 而不是 `self.dtype = ...`？

> **参考答案**：在新版 NumPy 中，直接给数组实例的 `.dtype` 赋值已被弃用（会触发 `DeprecationWarning: Setting the dtype`，见 `test_assign_dtype_attribute` 里 `with pytest.warns(DeprecationWarning, match="Setting the dtype")`）。`ndarray._set_dtype` 是底层的「就地改写 dtype」内部接口，绕开了弃用警告，正是为子类内部使用而保留的。

**练习 3**：`r = np.rec.array(np.ones(4, dtype='i4,i4'))` 之后，再做 `r.view('f4,f4')`。结果数组的 `type` 和 `dtype.type` 分别是什么？用本节原理解释。

> **参考答案**：`type(r.view('f4,f4'))` 仍是 `numpy.recarray`（view 保持子类类型），`dtype.type` 是 `numpy.record`（`'f4,f4'` 是带字段的 void 结构，`__array_finalize__` 把它再次提升为 record）。这与 [`test_records.py:L234-L235`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_records.py#L234-L235) 的断言一致。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「追踪一条 record array 的诞生」的小任务。

**任务**：用三种方式各造一个「字段为 `a:i4, b:f8`、长度 3」的 record array，分别对应「`__new__` 直接造」「`view` 派生」「`fromrecords` 装数据」三条路径，然后统一验证它们的 `dtype.type` 都是 `numpy.record`，并解释每条路径里「换芯」分别由谁完成。

**参考做法**：

```python
import numpy as np
dt = [('a', 'i4'), ('b', 'f8')]

# 路径 A：__new__ 直接造（4.2）
rA = np.recarray(3, dtype=dt)
rA[0], rA[1], rA[2] = (1, 1.1), (2, 2.2), (3, 3.3)

# 路径 B：view 派生（4.3）
base = np.array([(1, 1.1), (2, 2.2), (3, 3.3)], dtype=dt)
rB = base.view(np.recarray)

# 路径 C：fromrecords 装数据（u1-l3 + u4-l1）
rC = np.rec.fromrecords([(1, 1.1), (2, 2.2), (3, 3.3)], dtype=dt)

for name, r in [('A __new__', rA), ('B view', rB), ('C fromrecords', rC)]:
    print(name, type(r).__name__, r.dtype.type.__name__)
    # 预期三行都是： recarray record
```

**需要回答的问题**：

1. 路径 A 里，`dtype.type` 是谁设置的？—— 由 `__new__` 在 [`records.py:L397`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L397) 用 `(record, descr)` 直接设置。
2. 路径 B 里，`view` 不经过 `__new__`，为何 `dtype.type` 仍是 `record`？—— 由 `__array_finalize__` 在 [`records.py:L408-L413`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L408-L413) 事后提升。
3. 路径 C（`fromrecords`）最终也调用了 `recarray` 或 `.view(recarray)`，因此殊途同归——`dtype.type` 同样是 `record`。

> 完成后，建议把 `rA`、`rB`、`rC` 都 `view(np.ndarray)` 一遍，确认三者的 `dtype.type` 都褪回 `numpy.void`，进一步巩固「换芯是 recarray 专属」的认知。

---

## 6. 本讲小结

- `recarray` 是 `ndarray` 的子类，`record` 是 `nt.void` 的子类；二者在内存布局上与普通结构化数组完全一致，区别只在「属性访问」与「标量类型被换成 `record`」。
- 「换芯」的写法是二元 dtype `(record, descr)`：结构同 `descr`，但 `.type` 由 `void` 变为 `record`，使数组级 `r.x` 与标量级 `r[0].x` 同时可用。
- `recarray.__new__` 负责「从头造一个」：先按 `dtype` > `format_parser` 的优先级解析出 `descr`，再分 `buf=None`（新分配）/ `buf` 已给（复用缓冲）两条路径，统一以 `(record, descr)` 调用 `ndarray.__new__`。
- `__new__` 类似 `empty`：只造空壳，不装数据；装数据请走 `view(recarray)` / `buf` / `fromrecords`。
- `_set_dtype = None` 是向 NumPy C 层声明「dtype 变化交给 `__array_finalize__` 处理」。
- `__array_finalize__` 用三条 AND 条件（`is not record` 且 `issubclass(void)` 且 `names is not None`）判定是否需要把 `void` 提升为 `record`，并用 `ndarray._set_dtype` 就地改写——这覆盖了 `view` / 切片等绕过 `__new__` 的派生路径。

---

## 7. 下一步学习建议

- **下一篇 `u3-l2`：属性访问魔法 `__getattribute__` 与 `__setattr__`** —— 本讲只解决了「dtype.type 是 record」，但 `arr.x` 究竟怎么映射到字段，由 `recarray.__getattribute__` / `__setattr__` 实现，下一篇正是讲这套属性查找与 `getfield`/`setfield` 的回退逻辑。
- **`u3-l3`：`record` 标量类型** —— 深入 `record.__getattribute__`、`__getitem__`、`pprint`，理解标量级属性访问的细节。
- **延伸阅读源码**：[`numpy/_core/src/multiarray/convert.c`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/multiarray/convert.c) 中关于 `_set_dtype` / `__array_finalize__` 的三分支判定，能帮你把本讲的「钩子机制」从 Python 层沉到 C 层看透。
- **测试印证**：[`test_records.py`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_records.py) 的 `test_recarray_views`、`test_assign_dtype_attribute`、`test_nested_fields_are_records` 三个用例，是检验你是否真懂 `__array_finalize__` 的最佳试金石。
