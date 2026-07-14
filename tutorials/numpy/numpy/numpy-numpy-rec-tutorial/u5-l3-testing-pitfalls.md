# 测试套件与常见陷阱

## 1. 本讲目标

本讲是 numpy.rec（record array）专题的收尾篇。前面几讲我们已经把 `format_parser`、`recarray`/`record` 两个核心类、以及 `fromarrays`/`fromrecords`/`fromstring`/`fromfile`/`array` 五个构造函数的**实现**讲透了。本讲换个角度，从**测试**出发，用 NumPy 官方测试套件 `numpy/_core/tests/test_records.py` 作为「行为说明书」，把 record array 在真实使用中最容易踩的几个陷阱讲清楚。

学完本讲，你应当能够：

- 说出 `test_records.py` 的测试组织方式（四个测试类 + 一个独立函数），并能独立运行它。
- 理解**字段名与 ndarray 内置属性/方法同名时会被「遮蔽」（shadowed）**这一最经典陷阱，并知道用 `ra['字段名']` 或 `ra.field('字段名')` 作为「逃生口」。
- 理解**只读缓冲**（来自 `fromstring` 的 `bytes` 视图，或手动 `flags.writeable=False`）下，任何字段写入都会抛 `ValueError`。
- 理解 `fromrecords` 如何处理**二维（多行多列）记录网格**。
- 理解 `recarray` 与 `record` 标量的 **pickle 往返**为什么能保持类型与数值不变。

---

## 2. 前置知识

本讲假设你已经学完 u1～u4，尤其需要以下概念（本讲会直接使用，不再重复推导）：

- **结构化 dtype 与字段**：`dtype.names` 给字段名元组，`dtype.fields` 给 `{字段名: (子dtype, 字节偏移[, 标题])}` 字典。
- **recarray 与 record**：`recarray` 是 `ndarray` 的子类，`record` 是 `nt.void` 的子类；它们的内存布局与普通结构化数组完全一致，区别仅在于「属性式取列」与「标量类型被改写为 `numpy.record`」。
- **二元 dtype** `dtype((record, descr))`：保持 `descr` 的字段结构、偏移、itemsize 不变，只把标量类型从 `void` 换成 `record`。
- **属性访问魔法**：`recarray.__getattribute__` 先查对象属性，找不到才查 `dtype.fields`（参见 u3-l2）。本讲的「字段名冲突陷阱」正是这条规则的直接后果。
- **setfield/getfield**：按字节偏移切/写内存的底层引擎，是 `ra.x = v`、`ra.field('x', v)` 最终落到的位置。

术语速查：

| 术语 | 含义 |
|---|---|
| 遮蔽（shadow） | 字段名与 ndarray 属性同名时，`ra.字段名` 返回的是属性而非字段 |
| 逃生口 | 绕开属性查找、直接按字段名访问的方式：`ra['字段名']` 或 `ra.field('字段名')` |
| 只读缓冲 | `fromstring` 用 `buf=` 共享的 `bytes` 视图，不可写 |
| pickle 往返 | `pickle.loads(pickle.dumps(obj))` 后对象保持等价 |

---

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
|---|---|
| [numpy/_core/tests/test_records.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f8c949fca4420bb/numpy/rec/../_core/tests/test_records.py) | **本讲主角**：record array 的官方测试套件，覆盖 fromrecords/fromstring/fromfile/record 属性/冲突/pickle 等 |
| [numpy/_core/records.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f8c949fca4420bb/numpy/rec/../_core/records.py) | record array 的真实实现（`numpy/rec/__init__.py` 只是再导出垫片） |

> 提醒：`numpy.rec` 子包的公开 API 物理上全部实现在 `numpy/_core/records.py`，`numpy/rec/__init__.py` 通过 `from numpy._core.records import *` 再导出。本讲的永久链接统一指向 `_core` 下的真实实现文件。

---

## 4. 核心概念与源码讲解

本讲按「先看测试组织 → 再逐个拆解陷阱」的顺序，拆成五个最小模块：

- **4.1** 测试套件的组织与运行方式
- **4.2** 字段名与 ndarray 属性冲突陷阱（最经典）
- **4.3** 只读缓冲与 setfield 写入陷阱
- **4.4** 二维 fromrecords：多行记录网格
- **4.5** pickle 往返与 record/void 标量

### 4.1 test_records.py 的组织与运行方式

#### 4.1.1 概念说明

`test_records.py` 是 record array 的「行为契约」。它把功能切成几组互不干扰的测试类，每组对应一类用法。理解它的组织方式，等于拿到一张「record array 能做什么、不能做什么」的速查表。

测试文件的结构（按源码出现顺序）：

| 位置 | 测试单元 | 聚焦主题 |
|---|---|---|
| `TestFromrecords`（约 21–345 行） | 大类 | 各构造函数的端到端行为：`fromrecords`/`array`/`fromarrays`/`fromfile`、repr 打印、视图转换、字段类型返回值 |
| `TestPathUsage`（约 347–360 行） | 小类 | `pathlib.Path` 能否作为 `fromfile` 的路径参数 |
| `TestRecord`（约 363–533 行） | 大类 | `record` 标量与 recarray 的字段赋值、只读、乱序字段、pickle、嵌套结构 |
| `test_find_duplicate`（约 536–547 行） | 独立函数 | `np.rec.find_duplicate` 查重行为 |
| `TestPatternMatching`（约 550–595 行） | 类 | PEP 634 结构化模式匹配（`match/case`）对 recarray 的支持 |

#### 4.1.2 核心流程

测试用 `pytest` 组织：每个以 `test_` 开头的方法是一个独立用例，`assert_equal`/`assert_raises`/`assert_` 是 NumPy 自带的测试断言工具（从 `numpy.testing` 导入）。运行方式与普通 pytest 一样，只是要带上完整的模块路径。

```text
pytest numpy/_core/tests/test_records.py            # 跑整个文件
pytest numpy/_core/tests/test_records.py::TestRecord # 只跑 TestRecord 这一组
pytest numpy/_core/tests/test_records.py::TestFromrecords::test_fromrecords_2d  # 单个用例
```

#### 4.1.3 源码精读

文件顶部的导入揭示了测试依赖的工具：[test_records.py:1-18](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f8c949fca4420bb/numpy/rec/../_core/tests/test_records.py#L1-L18) 引入 `pickle`、`pytest`、`numpy`，以及 `numpy.testing` 下的断言函数与 `temppath`（临时路径上下文）。

最简单的入门用例 `test_fromrecords` 体现了「行为契约」的写法——构造 → 断言类型/数值：[test_records.py:22-29](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f8c949fca4420bb/numpy/rec/../_core/tests/test_records.py#L22-L29)

```python
r = np.rec.fromrecords([[456, 'dbe', 1.2], [2, 'de', 1.3]],
                    names='col1,col2,col3')
assert_equal(r[0].item(), (456, 'dbe', 1.2))
assert_equal(r['col1'].dtype.kind, 'i')   # 整数列被探测为整数
assert_equal(r['col2'].dtype.kind, 'U')   # 字符串列被探测为 Unicode
assert_equal(r['col2'].dtype.itemsize, 12) # 'dbe'/'de' → U3 → 12 字节
assert_equal(r['col3'].dtype.kind, 'f')   # 浮点列被探测为浮点
```

这正是 u4-l1 讲过的「自动探测路径」：不指定 `dtype`/`formats` 时，`fromrecords` 把数据铺成 object 二维数组，再**逐列独立推断类型**（`'dbe'`/`'de'` 都是 3 字符 → `U3` → itemsize `4*3=12` 字节）。注意此处传入的是**行式 list of list**，但因为没有指定 dtype，走的是自动探测分支（行为与 list of tuple 一致），不会触发 FutureWarning——这是 u4-l1 强调过的关键区分点。

独立函数 `test_find_duplicate` 则验证公开 API `np.rec.find_duplicate` 的查重语义：[test_records.py:536-547](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f8c949fca4420bb/numpy/rec/../_core/tests/test_records.py#L536-L547)

```python
l3 = [1, 2, 1, 4, 1, 6, 2, 3]
assert_(np.rec.find_duplicate(l3) == [1, 2])  # 返回重复元素（去重后）
```

`find_duplicate` 基于 `collections.Counter`，返回「出现次数 > 1 的元素列表」（每个重复元素只出现一次），实现见 [records.py:46-53](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f8c949fca4420bb/numpy/rec/../_core/records.py#L46-L53)。

#### 4.1.4 代码实践

1. **实践目标**：在你的机器上把这套测试跑起来，确认环境可用。
2. **操作步骤**：
   - 进入 NumPy 仓库根目录。
   - 执行 `python -m pytest numpy/_core/tests/test_records.py -q`。
   - 再单独跑一个用例：`python -m pytest "numpy/_core/tests/test_records.py::TestFromrecords::test_fromrecords" -v`。
3. **需要观察的现象**：终端打印每个用例的通过状态（绿点或 `PASSED`），并给出总耗时。
4. **预期结果**：全部用例通过（如果你的 NumPy 版本与本仓库 HEAD 一致）。
5. 如果因依赖未装好而无法运行（例如本仓库构建未完成），明确记为「待本地验证」，但你可以**直接阅读源码**理解断言含义——本讲后续模块都以「读断言 + 手写等价代码」为主，不强依赖运行环境。

#### 4.1.5 小练习与答案

**练习 1**：`TestPatternMatching` 这一组测试验证的是 Python 哪个语法特性？为什么 recarray 能支持它？

> **答案**：PEP 634 的结构化模式匹配（`match/case` 序列模式）。recarray 继承自 `ndarray`，`ndarray` 是可迭代、可按位置索引的序列类型，因此能匹配 `[a, b, c]` 这类序列模式；取出的是 `record` 标量（如 `a.x == 1`）。

**练习 2**：`test_fromrecords` 中 `'dbe'` 和 `'de'` 都被推断为 `U3`，itemsize 为 12。请解释 12 这个数字怎么来的。

> **答案**：NumPy 的 Unicode 字符串 dtype 每个字符占 4 字节（UCS-4）。最长字符串 `'dbe'` 有 3 个字符，故 dtype 为 `<U3`，`itemsize = 4 × 3 = 12` 字节。

---

### 4.2 字段名与 ndarray 属性冲突陷阱

#### 4.2.1 概念说明

这是 record array **最经典也最容易踩**的陷阱。回顾 u3-l2：`recarray.__getattribute__` 在取属性时，**先查对象属性（含 ndarray 内置属性与方法），找不到才回退到 `dtype.fields`**。这意味着——如果某个字段名恰好与 ndarray 的内置属性或方法同名（如 `shape`、`mean`、`var`、`T`、`field`、`data` 等），那么 `ra.字段名` 永远返回 ndarray 的那个属性/方法，**字段值被「遮蔽」了**。

这不是 bug，而是设计权衡：ndarray 有几百个属性方法，若让字段名优先，会破坏大量既有行为。代价就是冲突字段必须用「逃生口」访问。

#### 4.2.2 核心流程

读写两条路径的不对称是关键：

```text
【读】 ra.x
  └─ recarray.__getattribute__
       └─ object.__getattribute__ 先找 → 命中 ndarray 属性/方法 → 直接返回（字段被遮蔽）
       └─ 找不到 → 查 dtype.fields → getfield 取列

【逃生口读】 ra['x']   或   ra.field('x')
  └─ 绕开 __getattribute__，直接按字段名从 dtype.fields 取列（永远命中字段）

【写】 ra.x = v
  └─ recarray.__setattr__
       └─ object.__setattr__ 先试写 → 若 'x' 是字段名，撤销实例写入，改走 setfield（写入字段）
       └─ 故「写」不会丢失，只有「读」会被遮蔽
```

要点：**冲突时「写」仍然落到字段（因为 `__setattr__` 显式检查 `fielddict`），但「读」会被遮蔽**。所以你会遇到「明明 `ra.x = 5` 写进去了，`ra.x` 读出来却不是 5」的怪象——因为你读到的是同名方法。

#### 4.2.3 源码精读

陷阱的「现场」就是这条用例：[test_records.py:265-280](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f8c949fca4420bb/numpy/rec/../_core/tests/test_records.py#L265-L280)

```python
ra = np.rec.array([(1, 'abc', 2.3), (2, 'xyz', 4.2), (3, 'wrs', 1.3)],
               names='field, shape, mean')
ra.mean = [1.1, 2.2, 3.3]                       # 'mean' 既是字段又是 ndarray 方法
assert_array_almost_equal(ra['mean'], [1.1, 2.2, 3.3])  # 字段值确实写进去了
assert_(type(ra.mean) is type(ra.var))          # 但 ra.mean 读出的是「方法」，和 ra.var 同类
ra = ra.reshape((1, 3))
assert_(ra.shape == (1, 3))
# gh-29536: 给 .shape 赋值被弃用
with pytest.warns(DeprecationWarning, match="Setting the shape"):
    ra.shape = ['A', 'B', 'C']
assert_array_equal(ra['shape'], [['A', 'B', 'C']])   # 字段 'shape' 被写入
ra.field = 5                                     # 'field' 既是字段又是 recarray.field 方法
assert_array_equal(ra['field'], [[5, 5, 5]])     # 字段被写入（广播）
assert_(isinstance(ra.field, collections.abc.Callable))  # 但 ra.field 读出的是「方法」
```

逐句对应实现：

1. **`ra.mean = [1.1, 2.2, 3.3]` 写入字段、但 `ra.mean` 读出方法**。写入走 [records.py:449-484](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f8c949fca4420bb/numpy/rec/../_core/records.py#L449-L484) 的 `__setattr__`：它先 `object.__setattr__` 试写，发现 `'mean'` 在 `fielddict` 里，于是**撤销实例写入**（`object.__delattr`）并改走 `self.setfield(val, *res)`（末行 L484），所以字段被写入。而读取 `ra.mean` 走 [records.py:415-443](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f8c949fca4420bb/numpy/rec/../_core/records.py#L415-L443) 的 `__getattribute__`，第一句 `object.__getattribute__(self, attr)`（L420）直接命中 ndarray 的 `mean` **方法**，于是返回方法——字段被遮蔽。`type(ra.mean) is type(ra.var)` 成立，正是因为二者都是 ndarray 的同类绑定方法。

2. **`ra['mean']` 命中字段**。`ra['mean']` 走的是 `__getitem__` 而非 `__getattribute__`，完全绕开属性查找，按字段名取列，所以读到字段值 `[1.1, 2.2, 3.3]`。

3. **`ra.field = 5` 写入字段、但 `ra.field` 读出方法**。`field` 是 recarray 自己定义的方法（[records.py:539-554](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f8c949fca4420bb/numpy/rec/../_core/records.py#L539-L554)），写入同样经 `__setattr__` 落到字段并广播为 `[[5,5,5]]`；而 `ra.field` 读出时 `object.__getattribute__` 命中类方法，返回绑定方法，故 `isinstance(ra.field, Callable)` 为真。

4. **`ra.shape = ['A','B','C']`**：`shape` 既是 ndarray 属性又是字段。`object.__setattr__(self,'shape',...)` 触发 NumPy 的 shape 赋值逻辑（gh-29536 后会发 `DeprecationWarning: Setting the shape`），随后 `__setattr__` 的 `except` 分支捕获异常、确认 `'shape'` 在 `fielddict` 中、最终走 `setfield` 把 `['A','B','C']` 写入字段（见 [records.py:463-466](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f8c949fca4420bb/numpy/rec/../_core/records.py#L463-L466)）。所以 `ra['shape']` 仍能读到字段值，但 `ra.shape` 读到的是数组的 shape 元组——**冲突字段的读必须走 `ra['shape']`**。

> 说明：`ra.shape = ['A','B','C']` 这条用例依赖较新的 NumPy 行为（gh-29536 弃用提示）。不同版本上警告文本/是否抛错可能有细微差异，标记「待本地验证」当前版本的确切表现；但「字段值最终写入、且需用 `ra['shape']` 读出」这一结论是稳定的。

「逃生口」方法 `recarray.field` 本身：[records.py:539-554](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f8c949fca4420bb/numpy/rec/../_core/records.py#L539-L554) 直接从 `dtype.fields` 取 `(子dtype, 偏移)`，调用 `getfield`/`setfield`，完全绕开 `__getattribute__`/`__setattr__` 的属性优先逻辑，是访问冲突字段最稳妥的方式。

#### 4.2.4 代码实践

1. **实践目标**：亲手复现「字段被遮蔽」现象，并验证逃生口。
2. **操作步骤**（示例代码）：
   ```python
   import numpy as np
   import collections.abc

   ra = np.rec.array([(1, 2.3), (2, 4.2)],
                     names='mean, data')   # 'mean' 与 ndarray 方法冲突，'data' 不冲突
   ra.mean = [9.9, 8.8]                     # 写：落到字段
   print('ra["mean"]  =', ra['mean'])       # 字段值 [9.9, 8.8]
   print('ra.mean     =', ra.mean)          # ndarray 的 mean 绑定方法（被遮蔽）
   print('ra.data     =', ra.data)          # 字段值（'data' 不冲突，正常读出）
   print('type(ra.mean) is type(ra.var):',
         type(ra.mean) is type(ra.var))     # True：二者都是方法
   ```
3. **需要观察的现象**：`ra['mean']` 是数值数组，`ra.mean` 是方法对象，二者完全不同；`ra.data` 正常返回字段值。
4. **预期结果**：`ra['mean']` 输出 `[9.9, 8.8]`；`type(ra.mean) is type(ra.var)` 为 `True`。
5. 如果环境未就绪，标记「待本地验证」，但读断言即可理解行为。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `ra.field = 5` 能把 5 写进字段，而 `ra.field` 读出来却是方法？请用 `__setattr__`/`__getattribute__` 的执行顺序解释。

> **答案**：写入走 `__setattr__`，它**显式检查** `attr in fielddict`，发现 `'field'` 是字段名后，撤销 `object.__setattr__` 刚写入的实例属性并改走 `setfield`，所以 5 落到字段。读取走 `__getattribute__`，**第一步** `object.__getattribute__` 就命中了类上定义的 `field` 方法（实例属性已被撤销），直接返回方法，根本到不了查 `fielddict` 的回退分支。所以「写进字段、读出方法」。

**练习 2**：给你的字段起名时，下列哪些名字会和 ndarray 属性冲突、应当避免？`x`、`shape`、`col1`、`T`、`mean`、`dtype`、`value`。

> **答案**：`shape`、`T`、`mean`、`dtype` 会冲突（都是 ndarray/recarray 的属性或方法）；`x`、`col1`、`value` 通常安全。`dtype` 尤其敏感——`__setattr__` 对 `attr == 'dtype'` 有专门处理（会把 void 结构化类型提升为 record），不要拿它当字段名。

---

### 4.3 只读缓冲与 setfield 写入陷阱

#### 4.3.1 概念说明

`fromstring`（u4-l2）会把 `bytes` 缓冲**零拷贝**地包成 recarray，数组与原缓冲共享内存。`bytes` 是不可变的，因此这样得到的 recarray **只读**（`flags.writeable == False`）。此时任何「写字段」操作——无论是 `ra.x = v`（经 `__setattr__` → `setfield`）还是直接 `ra.setfield(v, ...)`——都会在尝试写只读内存时抛 `ValueError`。

同样的陷阱也出现在你**手动**把一个数组设为只读（`ra.flags.writeable = False`）之后。测试 `test_nonwriteable_setfield`（gh-8171）就是为锁定这个行为而存在的。

#### 4.3.2 核心流程

```text
fromstring(bytes, dtype=...)  →  recarray(buf=bytes)  →  flags.writeable=False（继承缓冲）
                                                      ↓
   ra.x = v  ──__setattr__──►  setfield(v, 子dtype, 偏移)  ──写只读内存──►  ValueError
   ra.setfield(v, *dtype.fields['x'])  ─────────────────────────────────►  ValueError
```

结论：**只读 recarray 上不能写字段**。要改数据，得先 `.copy()` 出一份可写的，或用可写缓冲（`bytearray`）构建。

#### 4.3.3 源码精读

`fromstring` 的文档串明确标注了只读语义——返回值「will be readonly if `datastring` is readonly」：[records.py:778-781](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f8c949fca4420bb/numpy/rec/../_core/records.py#L778-L781)。它在末尾用 `recarray(shape, descr, buf=datastring, offset=offset)`（[records.py:825](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f8c949fca4420bb/numpy/rec/../_core/records.py#L825)）共享缓冲，`bytes` 只读 ⇒ 数组只读。

锁定行为的用例：[test_records.py:390-397](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f8c949fca4420bb/numpy/rec/../_core/tests/test_records.py#L390-L397)

```python
# gh-8171
r = np.rec.array([(0,), (1,)], dtype=[('f', 'i4')])
r.flags.writeable = False              # 手动设为只读
with assert_raises(ValueError):
    r.f = [2, 3]                       # __setattr__ → setfield → 写只读内存 → ValueError
with assert_raises(ValueError):
    r.setfield([2, 3], *r.dtype.fields['f'])  # 直接 setfield 同样 ValueError
```

两条写入路径最终都落到 `ndarray.setfield`，它按字节偏移写底层内存；内存只读时，C 层在写入瞬间抛 `ValueError`。注意第一条 `r.f = [2, 3]` 经过的 `__setattr__`（[records.py:449-484](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f8c949fca4420bb/numpy/rec/../_core/records.py#L449-L484)）末行正是 `return self.setfield(val, *res)`（L484），与第二条直接 `setfield` 殊途同归——所以二者抛同样的 `ValueError`。

#### 4.3.4 代码实践

1. **实践目标**：复现只读缓冲下写字段报错，并用 `.copy()` 解决。
2. **操作步骤**（示例代码）：
   ```python
   import numpy as np

   buf = np.array([(10, 1.0)], dtype='i4,f8').tobytes()   # bytes 只读
   r = np.rec.fromstring(buf, dtype='i4,f8', names='n,x')
   print('writeable =', r.flags.writeable)                # False
   try:
       r.n = [99]                                          # 期望 ValueError
   except ValueError as e:
       print('写入失败:', e)

   w = r.copy()                                            # 拷贝出可写副本
   w.n = [99]
   print('拷贝后 w.n =', w.n)                              # [99]
   ```
3. **需要观察的现象**：第一次写入抛 `ValueError`；`.copy()` 后 `writeable` 变 `True`，写入成功。
4. **预期结果**：`writeable = False`；`r.n = [99]` 抛 `ValueError`；`w.n` 输出 `[99]`。
5. 若环境未就绪，标记「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：把上面示例的 `buf = ...tobytes()` 换成 `bytearray(...)`，结果会怎样？为什么？

> **答案**：`bytearray` 可写，`fromstring` 返回的 recarray 也会 `writeable=True`，于是 `r.n = [99]` 不再报错，且修改会**回写到原 `bytearray`**（因为共享内存）。这正是 `fromstring` 与 `numpy.frombuffer` 一致的缓冲语义。

**练习 2**：`test_nonwriteable_setfield` 为什么用 `assert_raises(ValueError)` 而不是 `AttributeError`？

> **答案**：写入字段走 `__setattr__` → `setfield`，`setfield` 找到了字段（`'f'` 确实存在），不存在「属性找不到」的问题；失败发生在**物理写只读内存**这一步，由 C 层抛 `ValueError`。只有当字段名根本不存在时，才会抛 `AttributeError`（参见 4.5 练习与 `test_invalid_assignment`）。

---

### 4.4 二维 fromrecords：多行记录网格

#### 4.4.1 概念说明

`fromrecords`（u4-l1）默认处理的是**一维**记录列表（外层每项是一条记录）。但它的数据其实是「任意形状的、每个元素是一条记录」的嵌套结构——只要给 `dtype`（或 `formats`），它就能构建出**二维甚至更高维**的 recarray。这正是 `test_fromrecords_2d` 验证的能力：把一个「行 × 列」的记录网格装成一个二维 recarray，再用 `r['字段名']` 切出每个字段的二维列。

#### 4.4.2 核心流程

给定 dtype 的「直接构造」路径（u4-l1 的路径 B）：

```text
data = [[(a,b), (a,b), (a,b)],     ← 2×3 网格，每格是一条 (a,b) 记录
        [(a,b), (a,b), (a,b)]]
            │
            ▼  fromrecords(data, dtype=[('a',int),('b',int)])
   sb.array(recList, dtype=descr)   ← 一次性把 2×3 网格解析成 shape=(2,3) 的结构化 ndarray
            │
            ▼  retval.view(recarray)
   shape=(2,3) 的 recarray，dtype.type = numpy.record
            │
            ▼  r['a'] / r['b']
   切出每个字段的二维列（仍是 ndarray，去掉 record 身份）
```

关键：**数据的外层形状决定了 recarray 的形状**，内层 tuple 决定了每个元素的 dtype 字段。`r['a']` 取出所有元素的 `a` 分量，形状与原网格一致。

#### 4.4.3 源码精读

二维用例：[test_records.py:37-55](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f8c949fca4420bb/numpy/rec/../_core/tests/test_records.py#L37-L55)

```python
data = [
    [(1, 2), (3, 4), (5, 6)],
    [(6, 5), (4, 3), (2, 1)]
]
expected_a = [[1, 3, 5], [6, 4, 2]]
expected_b = [[2, 4, 6], [5, 3, 1]]

# 给 dtype
r1 = np.rec.fromrecords(data, dtype=[('a', int), ('b', int)])
assert_equal(r1['a'], expected_a)
assert_equal(r1['b'], expected_b)

# 给 names（让 fromrecords 自动探测类型）
r2 = np.rec.fromrecords(data, names=['a', 'b'])
assert_equal(r2['a'], expected_a)
assert_equal(r2['b'], expected_b)

assert_equal(r1, r2)
```

注意 `r1`（给 `dtype`）和 `r2`（给 `names`、自动探测）结果相等。但二者走的路径不同：

- **`r1`（给 dtype）**：进入 [records.py:716-721](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f8c949fca4420bb/numpy/rec/../_core/records.py#L716-L721) 的 `descr = sb.dtype((record, dtype))`，然后 L723-724 的 `sb.array(recList, dtype=descr)` 一次性把 2×3 网格解析成 shape=(2,3) 的结构化数组，L748 `.view(recarray)`。`r1['a']` 取出 `[[1,3,5],[6,4,2]]`。
- **`r2`（给 names、不給 dtype/formats）**：进入 L708-714 的**自动探测路径**——先 `sb.array(recList, dtype=object)` 铺成 object 数组，再逐列 `tolist()` + 独立类型推断，最后委托 `fromarrays` 装填。两条路径数值结果一致（`assert_equal(r1, r2)`），但自动探测更慢。

> 提醒：本例 `data` 是 list of list of tuple。若把它改成「给 dtype 的 list of list of **list**」，会触发 u4-l1 讲过的 `FutureWarning` 降级分支（[records.py:739-743](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f8c949fca4420bb/numpy/rec/../_core/records.py#L739-L743)）。本例用 tuple，走的是成功分支，无警告。

#### 4.4.4 代码实践

1. **实践目标**：复现 `test_fromrecords_2d`，验证二维网格的字段切片形状。
2. **操作步骤**（示例代码）：
   ```python
   import numpy as np

   data = [[(1, 2), (3, 4), (5, 6)],
           [(6, 5), (4, 3), (2, 1)]]
   r = np.rec.fromrecords(data, dtype=[('a', int), ('b', int)])
   print('shape     =', r.shape)        # (2, 3)
   print('dtype     =', r.dtype)
   print("r['a']    =\n", r['a'])       # [[1,3,5],[6,4,2]]
   print("r['b']    =\n", r['b'])       # [[2,4,6],[5,3,1]]
   print('r[0,0].a =', r[0, 0].a)      # 1（取单条记录的 a 字段）
   print('type     =', type(r[0,0]).__module__ + '.' + type(r[0,0]).__name__)  # numpy.record
   ```
3. **需要观察的现象**：recarray 形状为 `(2, 3)`；`r['a']`、`r['b']` 是同形状的二维普通数组；`r[0,0]` 是 `numpy.record` 标量，支持 `.a` 属性访问。
4. **预期结果**：`shape=(2,3)`；`r['a']` 为 `[[1,3,5],[6,4,2]]`；`r[0,0].a == 1`；标量类型为 `numpy.record`。
5. 若环境未就绪，标记「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：把上面的 `data` 再外面包一层，变成 `3 × 2 × 3` 的三维网格，`fromrecords` 还能处理吗？`r['a']` 的形状是什么？

> **答案**：可以。`fromrecords`（给定 dtype 时）最终用 `sb.array(recList, dtype=descr)` 解析，外层嵌套形状即数组形状。三维网格 `3×2×3` 会得到 shape=`(3,2,3)` 的 recarray，`r['a']` 形状也是 `(3,2,3)`，元素是所有记录的 `a` 分量。

**练习 2**：`r1`（给 dtype）和 `r2`（给 names）结果相等，但为何官方推荐给 dtype？

> **答案**：给 names 时 `formats`/`dtype` 都为 None，走**自动探测路径**——要把数据先铺成 object 数组再逐列推断，慢且可能推断出非预期类型（如把整数列因含缺失值而提升为 float）。给 dtype 走 `sb.array(recList, dtype=descr)` 一次性构造，快且类型确定。

---

### 4.5 pickle 往返与 record/void 标量

#### 4.5.1 概念说明

「pickle 往返」指 `obj2 = pickle.loads(pickle.dumps(obj))` 后 `obj2 == obj` 且类型保持。record array 经常需要被序列化（存盘、跨进程传递），所以测试套件用三个 `test_pickle_*` 用例 + 一个 `test_pickle_void` 把这条路径锁死。

关键点：`recarray` 是 `ndarray` 子类、`record` 是 `nt.void` 子类，它们的 pickle 机制**继承自 ndarray/void**（`__reduce_ex__`），会把 dtype（包括 `(record, descr)` 二元 dtype）一起序列化。因此往返后，标量类型仍是 `numpy.record`，数值完全相等。

#### 4.5.2 核心流程

```text
recarray / record 标量
        │  pickle.dumps
        ▼
__reduce_ex__()  →  (reconstruct_fn, args)   # 含 dtype、shape、原始字节
        │  pickle.loads
        ▼
重建 ndarray（dtype 带 record 身份）/ 重建 void 标量
        │
        ▼
往返后 == 原对象，且 dtype.type == numpy.record
```

`test_pickle_3` 还额外校验往返后的 `record` 标量是一份**全新、可写、C/F 连续、对齐**的内存（不是原数组的视图）——这对「反序列化后能自由修改」很重要。

#### 4.5.3 源码精读

三个基础 pickle 用例：[test_records.py:412-435](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f8c949fca4420bb/numpy/rec/../_core/tests/test_records.py#L412-L435)

```python
def test_pickle_1(self):                       # Issue #1529：含 0 长度子数组字段
    a = np.array([(1, [])], dtype=[('a', np.int32), ('b', np.int32, 0)])
    for proto in range(2, pickle.HIGHEST_PROTOCOL + 1):
        assert_equal(a, pickle.loads(pickle.dumps(a, protocol=proto)))
        assert_equal(a[0], pickle.loads(pickle.dumps(a[0], protocol=proto)))

def test_pickle_2(self):                       # recarray 本体 + record 标量
    a = self._create_data()                    # _create_data 返回 recarray
    for proto in range(2, pickle.HIGHEST_PROTOCOL + 1):
        assert_equal(a, pickle.loads(pickle.dumps(a, protocol=proto)))
        assert_equal(a[0], pickle.loads(pickle.dumps(a[0], protocol=proto)))

def test_pickle_3(self):                       # Issue #7140：往返后标量内存属性
    a = self._create_data()
    for proto in range(2, pickle.HIGHEST_PROTOCOL + 1):
        pa = pickle.loads(pickle.dumps(a[0], protocol=proto))
        assert_(pa.flags.c_contiguous)
        assert_(pa.flags.f_contiguous)
        assert_(pa.flags.writeable)
        assert_(pa.flags.aligned)
```

其中 `_create_data` 返回一个三列 recarray：[test_records.py:364-368](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f8c949fca4420bb/numpy/rec/../_core/tests/test_records.py#L364-L368)。`test_pickle_1` 里 `('b', np.int32, 0)` 是一个「0 长度子数组」字段（形状为 `(0,)` 的 int32），历史上曾让 pickle 出问题（Issue #1529），该用例锁定了修复。

`test_pickle_3` 的关键结论：反序列化得到的 `pa` **不是**指向原缓冲的视图，而是一份独立的、`writeable=True` 的标量——因为 pickle 重建时分配了新内存。

针对「含 object 字段的 void 标量」的 `test_pickle_void`（gh-13593）：[test_records.py:437-460](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f8c949fca4420bb/numpy/rec/../_core/tests/test_records.py#L437-L460)。它检查 `a[0].__reduce__()` 返回的构造器是 `np._core.multiarray.scalar`，且第二个参数 `obj` **不是 bytes**（即没有把裸内存地址 pickle 进去，而是序列化了安全的对象载荷）。这条用例同时验证往返 `a[0] == unpickled` 成立，并覆盖了「把不可能的对象标量喂给构造器」会抛 `TypeError`/`RuntimeError` 的边界。

之所以 `record` 能享有这一切，是因为它本质是 `nt.void` 子类（[records.py:196-198](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f8c949fca4420bb/numpy/rec/../_core/records.py#L196-L198)），pickle/比较/内存管理都复用 void 的 C 层实现，`record` 只是在其上叠加了属性访问与美化打印。

#### 4.5.4 代码实践

1. **实践目标**：验证 recarray 与 record 标量的 pickle 往返，并检查往返后的类型与可写性。
2. **操作步骤**（示例代码）：
   ```python
   import pickle
   import numpy as np

   r = np.rec.fromrecords([(1, 'a', 2.0), (2, 'b', 3.0)],
                          names='id,name,val')
   r2 = pickle.loads(pickle.dumps(r))
   print('数组往返相等 :', (r == r2).all() and r.dtype == r2.dtype)
   print('类型保持    :', type(r2).__name__, r2.dtype.type.__name__)  # recarray record

   rec = r[0]
   rec2 = pickle.loads(pickle.dumps(rec))
   print('标量往返相等 :', rec == rec2)
   print('标量类型    :', type(rec2).__module__ + '.' + type(rec2).__name__)  # numpy.record
   print('往返后可写  :', rec2.flags.writeable)
   ```
3. **需要观察的现象**：往返后数组与标量均与原对象相等；标量类型仍是 `numpy.record`；往返后的标量可写。
4. **预期结果**：三个 `True`，类型显示 `recarray record` / `numpy.record`，`writeable=True`。
5. 若环境未就绪，标记「待本地验证」。

#### 4.5.5 小练习与答案

**练习 1**：`test_pickle_3` 为什么要单独检查 `pa.flags.writeable`？如果反序列化得到的是原数组的视图，会有什么后果？

> **答案**：pickle 的语义是「得到一份等价的、独立的新对象」。如果反序列化得到的是原数组缓冲的视图，那么对 `pa` 的修改会波及原数据（或受其锁定影响），且 `writeable` 可能继承原数组的只读状态。检查 `writeable=True` 等标志，确保往返得到的是一块**独立、可自由修改**的新内存，符合序列化的直觉预期。

**练习 2**：`test_invalid_assignment`（[test_records.py:382-388](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f8c949fca4420bb/numpy/rec/../_core/tests/test_records.py#L382-L388)）里 `x[0].col5 = 1` 抛什么错？为什么？

> **答案**：抛 `AttributeError`。`x[0]` 是 `record` 标量，`record.__setattr__`（[records.py:239-249](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f8c949fca4420bb/numpy/rec/../_core/records.py#L239-L249)）在 `dtype.fields` 里查不到 `'col5'`（字段不存在），也不是已有属性，于是末行 `raise AttributeError("'record' object has no attribute 'col5'")`。注意这与 4.3 的 `ValueError` 不同：4.3 是字段**存在**但内存只读；这里字段**不存在**。

---

## 5. 综合实践

把本讲三个核心陷阱（字段冲突、只读缓冲、二维 fromrecords）串成一个小任务。

**任务背景**：你拿到一份「班级 × 学生」的成绩网格数据，每个单元格是 `(姓名, 分数)`。请用 record array 建模，并完成一系列安全操作。

```python
import numpy as np

# 2 个班，每班 3 个学生：(姓名, 分数)
grid = [
    [('Ann', 90), ('Bob', 85), ('Cy',  78)],
    [('Dan', 60), ('Eve', 95), ('Fae', 72)],
]

# —— 任务 1：二维 fromrecords ——
r = np.rec.fromrecords(grid, dtype=[('name', 'U8'), ('score', 'i4')])
print('shape        =', r.shape)              # (2, 3)
print('全部分数     =\n', r['score'])          # [[90,85,78],[60,95,72]]

# —— 任务 2：字段名冲突陷阱 ——
# 故意让字段名叫 'mean'，体验「写进字段、读出方法」
rm = np.rec.fromrecords([(90,), (85,), (78,)], names='mean')
rm.mean = [1, 2, 3]
print("逃生口 rm['mean'] =", rm['mean'])       # [1,2,3]  ← 字段值
print("被遮蔽 rm.mean   =", rm.mean)           # <method> ← ndarray 方法
print("类型同为方法    :", type(rm.mean) is type(rm.var))  # True

# —— 任务 3：只读缓冲陷阱 ——
buf = np.array([(90,), (85,)], dtype='i4').tobytes()
ro = np.rec.fromstring(buf, dtype=[('score', 'i4')], names='score')
print('只读？        =', ro.flags.writeable)   # False
try:
    ro.score = [0, 0]                           # 期望失败
except ValueError as e:
    print('只读写入失败 :', type(e).__name__)
writable = ro.copy()
writable.score = [0, 0]                         # copy 后可写
print('copy 后写入   =', writable.score)        # [0, 0]
```

**要观察与思考**：

1. 任务 1 中 `r['score']` 的形状为什么是 `(2,3)`？它和 `r` 是什么关系？（提示：取字段返回的是普通 ndarray，不再是 record 身份。）
2. 任务 2 中 `rm.mean = [1,2,3]` 到底写到哪里了？为什么 `rm.mean` 读出来的不是 `[1,2,3]`？
3. 任务 3 中为什么必须 `.copy()` 才能改值？如果原始缓冲换成 `bytearray` 会怎样？

> 预期输出（数值层面）：`shape=(2,3)`；`rm['mean']=[1,2,3]`；`type(rm.mean) is type(rm.var)` 为 `True`；`ro.flags.writeable` 为 `False`；只读写入抛 `ValueError`；`writable.score=[0,0]`。若本机 NumPy 版本与 HEAD 不一致，部分警告文本可能有差异，标记「待本地验证」。

---

## 6. 本讲小结

- **测试即文档**：`test_records.py` 分四组（`TestFromrecords`/`TestRecord`/`TestPathUsage`/`TestPatternMatching`）加独立函数 `test_find_duplicate`，每组锁定一类行为，是 record array 最可靠的行为说明书。
- **字段名冲突是头号陷阱**：字段名与 ndarray 属性/方法（`shape`/`mean`/`var`/`field`/`T` 等）同名时，`ra.字段名` 读出的是属性/方法（被遮蔽），但 `ra.字段名 = v` 仍会写入字段——读用 `ra['字段名']` 或 `ra.field('字段名')` 作逃生口。
- **只读缓冲**：`fromstring` 对 `bytes` 共享内存得到只读 recarray，写字段（无论 `ra.x=v` 还是 `setfield`）都抛 `ValueError`；解法是 `.copy()` 或用 `bytearray`。
- **二维 fromrecords**：给定 dtype 时 `sb.array(recList, dtype=descr)` 一次性解析任意形状网格，外层形状即 recarray 形状；给 names 则走较慢的自动探测。
- **pickle 往返稳定**：`recarray`/`record` 复用 ndarray/void 的 pickle 机制，往返后数值相等、类型保持 `numpy.record`，且标量是独立可写的新内存。
- **异常类型区分**：字段不存在抛 `AttributeError`；字段存在但内存只读抛 `ValueError`——两者根因不同，定位时别搞混。

---

## 7. 下一步学习建议

本讲是 numpy.rec 专题的最后一篇。建议你接下来：

1. **横向对比**：把 `numpy/_core/tests/test_records.py` 与 `numpy/_core/tests/test_multiarray.py`、`test_deprecations.py` 中涉及结构化数组/record 的部分对照阅读，理解 record array 在整个 ndarray 体系中的位置。
2. **回到实现**：若想再深入，重读 [numpy/_core/records.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f8c949fca4420bb/numpy/rec/../_core/records.py) 的 `recarray.__getattribute__`/`__setattr__` 与 `record.__getattribute__`，体会「两级/三级属性回退」与「逃生口」如何共同支撑字段冲突场景。
3. **动手扩展**：尝试给本讲的综合实践加上「含嵌套结构化字段」的记录（参考 `test_recarray_returntypes` 与 `test_nested_fields_are_records`），验证嵌套字段也会继承 `numpy.record` 身份。
4. **关注演进**：用 `git log -- numpy/_core/records.py numpy/_core/tests/test_records.py` 跟踪 record array 的近期改动（如 gh-29536 的 `.shape` 弃用、gh-8171 的只读 setfield 修复），理解这些陷阱为何被写成测试用例。

至此，从「模块定位 → dtype 描述 → 两个核心类 → 五个构造函数 → 视图/打印/测试陷阱」，你已经完整走完了 numpy.rec 的学习路线。
