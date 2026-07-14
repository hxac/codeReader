# 持久化：pickle、重建与深拷贝

## 1. 本讲目标

本讲解决两个紧密相关的问题：

1. **保存与复活**：一个 `MaskedArray`（连同它的 data、mask、fill_value、子类身份）如何被 pickle 成字节流，再从字节流里「原样」还原回来。
2. **独立复制**：如何用 `copy.deepcopy` 得到一个与原数组**完全独立**的副本——改副本不影响原件，连内部 `_mask` 都不共享。

学完本讲，你应当能够：

- 说清 `__reduce__` / `__getstate__` / `__setstate__` 三者在掩码数组序列化中各自的职责。
- 解释 `_mareconstruct` 为什么是「先建空壳，再灌状态」，以及它如何保住子类身份与 `_baseclass`。
- 理解反序列化时 `make_mask_descr` 如何让 mask 的结构自动跟随 data 的 dtype（结构化 dtype 的关键）。
- 用 `copy.deepcopy` 正确复制掩码数组，并解释 object dtype 数组为何需要特殊处理。
- 对照阅读 mrecords 的 `_mrreconstruct`，理解字段级屏蔽记录数组的同构（但独立）实现。

---

## 2. 前置知识

### 2.1 三件套回顾

本讲默认你已经建立 u1-l4 与 u2-l2 的认知：一个 `MaskedArray` 由三件套组成——`_data`（含坏值的真实数据）、`_mask`（同形布尔数组，无屏蔽时为单例 `nomask`）、`_fill_value`（屏蔽位对外填充值）。它们是 pickle 时必须完整带走的「状态」。

### 2.2 Python pickle 协议

pickle 的核心是 `__reduce__`。一个对象的 `__reduce__` 应返回一个元组，最常见的是三元组：

```text
(callable, args, state)
```

反序列化时，pickle 会：

1. 调用 `obj = callable(*args)`，**先造一个空壳对象**；
2. 再调用 `obj.__setstate__(state)`（或若无 `__setstate__` 则把 `state` 当字典灌进 `__dict__`），**把状态灌进去**。

所以「建壳」与「灌态」是两步。理解这一点，是看懂掩码数组 pickle 的钥匙。

### 2.3 copy 模块

`copy.deepcopy(x)` 会递归复制 `x` 及其所有可变内容。为了正确处理循环引用与「同一对象被引用多次」，`deepcopy` 用一个 `memo` 字典记录 `id(原对象) → 副本` 的映射。自定义类型只需实现 `__deepcopy__(self, memo)` 即可接管深拷贝逻辑。

### 2.4 ndarray 自带的序列化

普通 ndarray 的 `__reduce__` 产出的状态是一个五元组：

```text
(version, shape, dtype, is_fortran_contiguous, raw_bytes)
```

掩码数组会**复用**这个五元组，再**追加**自己的 mask 与 fill_value，拼成七元组。这就是下文反复出现的「5 + 2 = 7」。

---

## 3. 本讲源码地图

| 文件 | 关键符号 | 作用 |
| --- | --- | --- |
| `numpy/ma/core.py` | `MaskedArray.__getstate__` / `__setstate__` / `__reduce__` | 把三件套序列化为七元组状态、还原状态 |
| `numpy/ma/core.py` | `_mareconstruct` | 反序列化时的「空壳构建器」，保住子类与 `_baseclass` |
| `numpy/ma/core.py` | `MaskedArray.__deepcopy__` | 完整深拷贝，含 object dtype 特例 |
| `numpy/ma/core.py` | `MaskedArray.__new__` | `_mareconstruct` 最终调用的构造入口 |
| `numpy/ma/core.py` | `MaskedConstant.__reduce__` / `__deepcopy__` | `masked` 单例的特殊持久化（恒返回自身） |
| `numpy/ma/mrecords.py` | `MaskedRecords.__getstate__` / `__setstate__` / `__reduce__` | 字段级屏蔽记录数组的同构实现 |
| `numpy/ma/mrecords.py` | `_mrreconstruct` | mrecords 版的空壳构建器 |
| `numpy/ma/tests/test_core.py` | `test_pickling` / `test_pickling_subbaseclass` / `test_deepcopy` / `test_deepcopy_2d_obj` | 持久化行为的可执行契约 |

---

## 4. 核心概念与源码讲解

### 4.1 pickle 协议与掩码数组的状态三件套

#### 4.1.1 概念说明

把一个 `MaskedArray` 存成字节流，难点不在于 data（那是 ndarray 已经解决的问题），而在于：**除了 data，还要把 mask 和 fill_value 一起带走，并且要保证还原后的对象和原来的类型完全一致**。

NumPy 的做法是「借力」：复用 ndarray 已经成熟的 `__reduce__` 产出的状态（含 shape、dtype、原始字节），再在它**尾部追加**两块掩码数组专属内容——mask 的字节、fill_value。这样状态就从五元组扩展成了七元组。

#### 4.1.2 核心流程

序列化（dump）与反序列化（load）的双向流程如下：

```text
┌─────────────── 序列化路径（pickle.dumps）───────────────┐
│                                                          │
│  __reduce__()                                            │
│    └─ 返回 (_mareconstruct, (cls, baseclass, (0,), 'b'), │
│             __getstate__())                              │
│                              │                           │
│  __getstate__() ◄────────────┘                           │
│    ├─ cf = 'CF'[self.flags.fnc]      # C 或 F 顺序       │
│    ├─ data_state = super().__reduce__()[2]   # 5 元组    │
│    └─ return data_state + (mask_bytes, fill_value)       │
│                                            # 拼成 7 元组 │
└──────────────────────────────────────────────────────────┘

┌─────────────── 反序列化路径（pickle.loads）──────────────┐
│                                                          │
│  第一步：建壳                                            │
│    obj = _mareconstruct(cls, baseclass, (0,), 'b')       │
│            └─ 造一个 dtype='b'、shape=(0,) 的空壳        │
│                                                          │
│  第二步：灌态                                            │
│    obj.__setstate__(state)        # state 是 7 元组      │
│       ├─ super().__setstate__((shp, typ, isf, raw))      │
│       │        # 还原 data                               │
│       ├─ self._mask.__setstate__((shp, mask_dtype, ...)) │
│       │        # 还原 mask                               │
│       └─ self.fill_value = flv    # 还原 fill_value      │
└──────────────────────────────────────────────────────────┘
```

七元组状态的结构：

| 位置 | 内容 | 来源 |
| --- | --- | --- |
| 0 | `version`（版本号） | ndarray 自带 |
| 1 | `shp`（形状） | ndarray 自带 |
| 2 | `typ`（dtype） | ndarray 自带 |
| 3 | `isf`（是否 Fortran 序） | ndarray 自带 |
| 4 | `raw`（data 的原始字节） | ndarray 自带 |
| 5 | `msk`（mask 的原始字节） | **掩码数组追加** |
| 6 | `flv`（fill_value） | **掩码数组追加** |

#### 4.1.3 源码精读

先看 `__getstate__` 如何拼出七元组：

[core.py:6483-6490](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L6483-L6490) —— `__getstate__` 复用 ndarray 的五元组状态，再追加 mask 字节与 fill_value。

关键三行：

- `cf = 'CF'[self.flags.fnc]`：`self.flags.fnc` 是布尔值（是否 Fortran 连续），`'CF'[True]` 得 `'F'`、`'CF'[False]` 得 `'C'`，记录数据存放顺序，还原时才能正确解包字节。
- `data_state = super().__reduce__()[2]`：`super()` 即 `ndarray`，`ndarray.__reduce__()` 返回 `(reconstruct, args, state)`，取 `[2]` 就是 ndarray 的状态五元组。
- `return data_state + (getmaskarray(self).tobytes(cf), self._fill_value)`：注意用的是 `getmaskarray(self)`（不是 `getmask`），它**永远返回一个同形布尔数组**（无屏蔽时是全 False 数组而非 `nomask`），从而保证即便原数组完全无屏蔽，mask 字节也能正确序列化与还原。

`__setstate__` 是 `__getstate__` 的镜像：

[core.py:6492-6507](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L6492-L6507) —— `__setstate__` 解包七元组，分别还原 data、mask、fill_value。

三步还原：

1. `(_, shp, typ, isf, raw, msk, flv) = state`：丢弃版本号 `_`，取出其余六项。
2. `super().__setstate__((shp, typ, isf, raw))`：交给 ndarray 还原 `_data`（重建 shape、dtype、字节序，并把 raw 字节填进去）。
3. `self._mask.__setstate__((shp, make_mask_descr(typ), isf, msk))`：**就地**把 mask 字节灌进已存在的 `_mask` 对象。这里的 `make_mask_descr(typ)` 是关键桥梁——详见 4.3 节。
4. `self.fill_value = flv`：恢复填充值。

注意第 3 步用的是 `self._mask.__setstate__(...)`，说明 `_mask` 这个对象在 `__setstate__` 被调用**之前就已经存在**（由建壳函数 `_mareconstruct` 创建），`__setstate__` 只是把它的内容**就地改写**。

`__reduce__` 把三者串成 pickle 期望的三元组：

[core.py:6509-6515](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L6509-L6515) —— `__reduce__` 返回 `(callable, args, state)` 三元组。

返回值拆解：

- 第 1 项 `_mareconstruct`：反序列化时用来「建壳」的可调用对象（见 4.2 节）。
- 第 2 项 `(self.__class__, self._baseclass, (0,), 'b',)`：传给 `_mareconstruct` 的参数。其中 `self.__class__` 是实际子类、`self._baseclass` 是底层 ndarray 子类（如 `recarray`）、`(0,)` 是占位形状、`'b'` 是占位 dtype。
- 第 3 项 `self.__getstate__()`：七元组状态。

#### 4.1.4 代码实践

**实践目标**：亲手验证七元组状态的真实结构，并确认 pickle 往返不丢任何信息。

**操作步骤**（在 Python 解释器中执行）：

```python
import pickle
import numpy as np

a = np.ma.array([1, 2, 3], mask=[0, 1, 0], dtype=float)
a.fill_value = -99.0

# 1) 直接观察 __getstate__ 返回的七元组
state = a.__getstate__()
print("状态长度：", len(state))          # 预期 7
print("version:", state[0])              # ndarray 版本号
print("shape  :", state[1])              # (3,)
print("dtype  :", state[2])              # dtype('float64')
print("isf    :", state[3])              # False (C 序)
print("raw    :", state[4])              # data 的字节
print("mask   :", state[5])              # mask 的字节
print("fill   :", state[6])              # -99.0

# 2) 完整往返
b = pickle.loads(pickle.dumps(a, protocol=pickle.HIGHEST_PROTOCOL))
print("data 一致 :", np.array_equal(b._data, a._data))
print("mask 一致 :", np.array_equal(b._mask, a._mask))
print("fill 一致 :", b.fill_value == a.fill_value)
print("类型一致 :", type(b) is type(a))
```

**需要观察的现象**：

- `state` 长度恰为 7。
- 往返后 `b` 的 data、mask、fill_value 全部与 `a` 一致，且类型仍为 `MaskedArray`。

**预期结果**：四处断言全部为 `True`。这与 [test_core.py:718-738](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/tests/test_core.py#L718-L738) 中 `test_pickling` 的断言一致（该测试遍历 `int/float/str/object` 四种 dtype、三种 mask 情形、多种 pickle 协议，逐一核对 `_mask`、`_data`、`fill_value`）。

> 若在精简环境运行，结果以本地实际输出为准（待本地验证）。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `a.fill_value` 改成 `None`，pickle 往返后还能保留「使用默认填充值」的行为吗？

**参考答案**：能。`__getstate__` 把 `self._fill_value`（可能是 `None`）作为第 7 项写入；`__setstate__` 执行 `self.fill_value = flv` 时，`None` 会触发 fill_value 的 property setter 回到「默认」路径，因此行为一致。

**练习 2**：为什么 `__getstate__` 用 `getmaskarray(self)` 而不是 `self._mask`？

**参考答案**：`self._mask` 在无屏蔽时是单例 `nomask`（即 `False`），它没有 `.tobytes()` 也无法表达「全 False 的同形数组」。`getmaskarray` 保证永远返回一个真实的同形布尔数组，使 mask 字节总是可序列化的，反序列化时也能正确还原成「全 False 的 mask」而非丢失掩码结构。

---

### 4.2 _mareconstruct：空壳构建器与子类还原

#### 4.2.1 概念说明

pickle 在调用 `__setstate__` 之前，必须**先有一个对象**来接收状态。这个对象由 `__reduce__` 三元组的第 1 项（callable）配合第 2 项（args）创建。

对掩码数组来说，这个 callable 就是 `_mareconstruct`。它的职责很有限：**只造一个类型正确、内部为空的壳子**，把所有真实数据留给 `__setstate__` 去填。这种「建壳 + 灌态」分离的设计，是为了同时满足两个要求：

1. **保住子类身份**：用户自定义的 `MaskedArray` 子类经 pickle 往返后，类型不能退化成基类。
2. **保住 `_baseclass`**：一个包裹在 `recarray` 之上的掩码数组，还原后其 `_data` 仍应是 `recarray`。

#### 4.2.2 核心流程

```text
_mareconstruct(subtype, baseclass, baseshape=(0,), basetype='b')
   │
   ├─ _data = ndarray.__new__(baseclass, baseshape, basetype)
   │          # 用 baseclass 造一个空 ndarray（保住 recarray 等）
   │
   ├─ _mask = ndarray.__new__(ndarray, baseshape, make_mask_descr(basetype))
   │          # 造一个同形的空 mask
   │
   └─ return subtype.__new__(subtype, _data, mask=_mask, dtype=basetype)
              # 调用 MaskedArray.__new__，把壳子「升级」为子类实例
```

注意 `baseshape=(0,)` 和 `basetype='b'` 只是占位符——建壳阶段不关心真实形状与类型，那些信息全在随后到来的 `__setstate__` 里。`_mareconstruct` 唯一要在意的是**用谁的 `__new__`**。

#### 4.2.3 源码精读

[core.py:6534-6541](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L6534-L6541) —— `_mareconstruct` 是反序列化的建壳函数。

逐行解读：

- `_data = ndarray.__new__(baseclass, baseshape, basetype)`：刻意用 `baseclass`（而非硬编码 `ndarray`）来 `__new__`。这样当 `baseclass` 是 `np.recarray` 时，`_data` 本身就是 `recarray`，字段访问能力得以保留。这正是 `test_pickling_subbaseclass` 要验证的点（见下文）。
- `_mask = ndarray.__new__(ndarray, baseshape, make_mask_descr(basetype))`：mask 用普通 `ndarray` 即可，dtype 由 `make_mask_descr(basetype)` 决定。
- `return subtype.__new__(subtype, _data, mask=_mask, dtype=basetype)`：进入 `MaskedArray.__new__`。

这里调用的 `MaskedArray.__new__` 签名见 [core.py:2882-2884](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L2882-L2884)，其中第一参数 `data=_data`、`mask=_mask`、`dtype=basetype`。在 `__new__` 内部有至关重要的一行：

[core.py:2897](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L2897) —— `_baseclass = getattr(data, '_baseclass', type(_data))`：从传入的 `_data` 推断 `_baseclass`。由于 `_mareconstruct` 用 `baseclass` 造了 `_data`，这里的 `type(_data)` 正是 `recarray`（或用户自定义的 ndarray 子类），`_baseclass` 被正确继承。

**子类还原的契约**：[test_core.py:740-749](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/tests/test_core.py#L740-L749) —— `test_pickling_subbaseclass` 构造一个 `recarray` 视图、包成掩码数组，pickle 往返后断言 `isinstance(a_pickled._data, np.recarray)` 为真，验证 `_mareconstruct` 的 `baseclass` 参数确实生效。

#### 4.2.4 代码实践

**实践目标**：验证 `_mareconstruct` 能保住**自定义子类**的身份（这是本讲规格要求的子类还原实验）。

**操作步骤**：

```python
import pickle
import numpy as np

# 1) 定义一个带额外属性的 MaskedArray 子类
class MyMA(np.ma.MaskedArray):
    def __new__(cls, data, mask=np.ma.nomask, **kw):
        obj = super().__new__(cls, data, mask=mask, **kw)
        obj.tag = "my-subclass"          # 额外属性（放进 __dict__）
        return obj
    def __array_finalize__(self, obj):
        super().__array_finalize__(obj)
        # 子类属性靠 _optinfo 才能跨 pickle 存活，这里简化：直接重建
        self.tag = getattr(obj, 'tag', "my-subclass")

a = MyMA([1.0, 2.0, 3.0], mask=[0, 1, 0])
a.fill_value = -1.0

# 2) pickle 往返
b = pickle.loads(pickle.dumps(a, protocol=pickle.HIGHEST_PROTOCOL))

print("类型还原 :", type(b) is MyMA)            # 预期 True
print("data    :", b._data.tolist())
print("mask    :", b._mask.tolist())
print("fill    :", b.fill_value)
```

**需要观察的现象**：

- `type(b) is MyMA` 为 `True`，证明 `__reduce__` 把 `self.__class__`（即 `MyMA`）传给了 `_mareconstruct`，建壳时用的是 `MyMA.__new__` 而非基类。

**预期结果**：类型断言为 `True`，data/mask/fill_value 全部保留。这正是 `_mareconstruct` 把 `subtype` 一路传递到 `subtype.__new__(subtype, ...)` 的功劳。

> 若自定义子类的额外属性未放进 `_optinfo`（见 u3-l2），pickle 往返后该属性可能丢失或需在 `__array_finalize__` 中重建——这是子类化的细节，本实践用 `__array_finalize__` 兜底重建 `tag`。

#### 4.2.5 小练习与答案

**练习 1**：`_mareconstruct` 的第 3、4 参数 `(0,)` 和 `'b'` 为什么可以是「占位符」？

**参考答案**：因为建壳阶段只需要一个**类型与形状合法**的空 ndarray，以便随后能 `.view(subtype)` 并挂上 `_mask` 属性。真实的 shape 与 dtype 在 `__setstate__` 的 `super().__setstate__((shp, typ, ...))` 中被**就地改写**，占位值会被完全覆盖。

**练习 2**：如果把 `_mareconstruct` 里的 `ndarray.__new__(baseclass, ...)` 改成 `ndarray.__new__(ndarray, ...)`，`test_pickling_subbaseclass` 会怎样？

**参考答案**：`_data` 会退化为普通 `ndarray`，`getattr(data, '_baseclass', type(_data))` 取到 `ndarray` 而非 `recarray`，于是 `isinstance(a_pickled._data, np.recarray)` 断言失败。可见 `baseclass` 参数不是装饰，而是保住 `_baseclass` 链的关键。

---

### 4.3 反序列化时 mask 与 fill_value 的恢复（make_mask_descr 的桥梁作用）

#### 4.3.1 概念说明

本模块单独聚焦七元组里第 5、6 项（mask 字节、fill_value）的恢复过程，因为这里藏着掩码数组 pickle 最精巧的一环：**mask 的 dtype 不是直接序列化的，而是从 data 的 dtype 推导出来的**。

回想 u2-l1：掩码数组用 `make_mask_descr(dtype)` 把任意 dtype（含结构化、嵌套、子数组）「翻译」成对应的布尔 dtype。例如结构化 dtype `[('a', int), ('b', float)]` 对应的 mask dtype 是 `[('a', bool), ('b', bool)]`。

这一机制在 pickle 里被巧妙复用：七元组里存的是 **data 的 dtype**（`typ`），还原 mask 时用 `make_mask_descr(typ)` 重新算出 mask dtype。好处是省存储、且 mask 结构永远与 data 自洽。

#### 4.3.2 核心流程

```text
__setstate__(state):
   (_, shp, typ, isf, raw, msk, flv) = state
   │
   ├─ super().__setstate__((shp, typ, isf, raw))
   │      # ndarray 用 (shp, typ, isf) 重建 _data 的形状/类型，
   │      # 再用 raw 字节填充内容
   │
   ├─ self._mask.__setstate__((shp, make_mask_descr(typ), isf, msk))
   │      │   # 关键：mask_dtype = make_mask_descr(typ)
   │      │   #  - 普通 dtype → bool
   │      │   #  - 结构化 dtype → 逐字段 bool
   │      └─ # ndarray.__setstate__ 就地改写已存在的 _mask
   │
   └─ self.fill_value = flv
          # 走 fill_value 的 property setter，
          # 经 _check_fill_value 归一化后存入 self._fill_value
```

值得强调：第 2 步是对**已经存在的** `self._mask` 对象做就地改写（调用它的 `__setstate__`），而不是新建一个 mask。这个 `_mask` 对象由 `_mareconstruct` 在建壳阶段创建。

#### 4.3.3 源码精读

[core.py:6504-6507](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L6504-L6507) —— `__setstate__` 的核心三行：还原 data、就地改写 mask、设 fill_value。

最值得品味的是：

```python
self._mask.__setstate__((shp, make_mask_descr(typ), isf, msk))
```

- `typ` 是 data 的 dtype（从七元组第 3 项取出）。
- `make_mask_descr(typ)` 重新算出 mask 的 dtype——**不依赖 pickle 里单独存的 mask dtype**。
- 对 `self._mask`（建壳阶段已造的占位 mask）就地灌入 `(shp, mask_dtype, isf, msk)`，ndarray 的 `__setstate__` 会重塑它、改字节。

这解释了为什么结构化 dtype 的掩码数组能正确往返：`test_pickling_wstructured`（[test_core.py:760-767](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/tests/test_core.py#L760-L767)）用 `dtype=[('a', int), ('b', float)]` 构造数组并部分屏蔽字段 `b`，往返后断言 `_mask` 与原数组一致——靠的就是 `make_mask_descr` 把结构化 dtype 翻译成结构化 mask dtype。

而 `self.fill_value = flv` 走的是 property setter（见 u2-l3），内部经 `_check_fill_value` 归一化，因此即便 `flv` 是标量，最终也会被存成 0d 数组，与正常运行时一致。

#### 4.3.4 代码实践

**实践目标**：验证结构化 dtype 的掩码数组在 pickle 往返后，**逐字段** mask 结构完整恢复。

**操作步骤**：

```python
import pickle
import numpy as np

a = np.ma.array([(1, 1.5), (2, 2.5)],
                mask=[(0, 0), (0, 1)],
                dtype=[('a', int), ('b', float)])

b = pickle.loads(pickle.dumps(a, protocol=pickle.HIGHEST_PROTOCOL))

print("data dtype :", b.dtype)
print("mask dtype :", b._mask.dtype)   # 预期 [('a','?'),('b','?')]
print("mask       :", b._mask.tolist()) # 预期 [(False,False),(False,True)]
print("字段 b 的屏蔽 :", b['b'].mask.tolist())
```

**需要观察的现象**：

- `b._mask.dtype` 是结构化布尔 dtype `[('a', '?'), ('b', '?')]`，这正是 `make_mask_descr(a.dtype)` 的产物，证明 mask dtype 是**从 data dtype 推导**而非单独存储。
- 只有第二条记录的字段 `b` 被屏蔽，结构化屏蔽信息完整保留。

**预期结果**：与上述注释一致。该行为由 `test_pickling_wstructured` 守护。

#### 4.3.5 小练习与答案

**练习 1**：七元组状态里有没有单独存 mask 的 dtype？为什么？

**参考答案**：没有。mask dtype 在反序列化时用 `make_mask_descr(typ)` 从 data 的 dtype `typ` 重新推导。这样既省存储，又保证 mask 结构永远跟随 data，不会出现「data 是结构化、mask 却是普通 bool」的不一致。

**练习 2**：为什么 `__setstate__` 写 `self._mask.__setstate__(...)` 而不是 `self._mask = ...`（重新赋值）？

**参考答案**：因为建壳函数 `_mareconstruct` 已经为这个实例造好了 `_mask` 对象，`__setstate__` 只需就地把它的形状与字节改对即可，避免再分配一个新数组。同时，就地改写能保持对象身份与已有的引用关系稳定。

---

### 4.4 __deepcopy__：完整深拷贝与 object dtype 特例

#### 4.4.1 概念说明

`copy.deepcopy` 与 pickle 走的是**不同的路径**：pickle 经过「建壳 + 灌态」，而 deepcopy 直接调用对象的 `__deepcopy__(self, memo)` 方法。`MaskedArray` 实现了自己的 `__deepcopy__`，目标是得到一个**与原件彻底独立**的副本——不仅 `_data` 不共享，连 `_mask`、`_fill_value` 以及 `__dict__` 里的所有簿记属性都不共享。

这里还有一个 NumPy 特有的坑：**object dtype 数组的元素是任意 Python 对象**（比如嵌套 list），普通 ndarray 的 copy 语义只复制「容器」不深复制「元素」。对掩码数组做 deepcopy 时必须额外处理这一情况，否则副本里改一个嵌套对象会污染原件。

#### 4.4.2 核心流程

```text
__deepcopy__(self, memo=None):
   │
   ├─ copied = MaskedArray.__new__(type(self), self, copy=True)
   │      # 用 copy=True 造同子类副本：_data 与 _mask 被复制（不共享）
   │
   ├─ memo[id(self)] = copied
   │      # 登记 id→副本，正确处理循环引用与多次引用
   │
   ├─ for (k, v) in self.__dict__.items():
   │      copied.__dict__[k] = deepcopy(v, memo)
   │      # 深拷贝所有簿记属性（_fill_value、_hardmask、_optinfo 等）
   │
   └─ if self.dtype.hasobject:
          copied._data[...] = deepcopy(copied._data)
          # object dtype 特例：深拷贝每个元素（含嵌套可变对象）
```

#### 4.4.3 源码精读

[core.py:6517-6531](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L6517-L6531) —— `MaskedArray.__deepcopy__` 的完整实现。

逐段解读：

1. `from copy import deepcopy`：函数内导入，避免顶层循环依赖。
2. `copied = MaskedArray.__new__(type(self), self, copy=True)`：注意是 `MaskedArray.__new__`（而非 `type(self).__new__`），避免触发用户子类可能重写的 `__new__`；`type(self)` 保证副本仍是同一个子类。`copy=True` 让 `__new__` 内部 `np.array(self, copy=True, ...)` 复制 `_data`，同时 mask 也被复制。这一步已经让 data 与 mask **物理独立**。
3. `memo[id(self)] = copied`：登记进 `memo`，这是 `copy.deepcopy` 协议要求的，保证若 `self` 在自身属性里被再次引用时不会无限递归，且多次引用解析为同一副本。
4. `for (k, v) in self.__dict__.items(): copied.__dict__[k] = deepcopy(v, memo)`：对 `__dict__` 里**每一个**属性做深拷贝。这覆盖了 `_fill_value`（0d 数组）、`_hardmask`、`_sharedmask`、`_baseclass`、`_optinfo` 等——全部独立。
5. `if self.dtype.hasobject: copied._data[...] = deepcopy(copied._data)`：object dtype 特例。源码注释（[core.py:6525-6529](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L6525-L6529)）明确写道：对于可能含复合类型的 object 数组，不能依赖普通 copy 语义，必须直接 deepcopy。

**契约验证**：[test_core.py:585-600](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/tests/test_core.py#L585-L600) —— `test_deepcopy` 断言 `id(a._mask) != id(copied._mask)`（mask 不共享），且改副本的 mask 或被屏蔽位不影响原件。

**object dtype 契约**：[test_core.py:5991-6005](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/tests/test_core.py#L5991-L6005) —— `test_deepcopy_2d_obj` 构造含嵌套 list 的 object dtype 掩码数组，deepcopy 后对副本里 `deepcopy[2, 0]` 做 `extend`，断言原件 `source[2, 0]` 长度不变（仍为 2），副本长度变为 4。这正是第 5 步特例要保证的。

#### 4.4.4 代码实践

**实践目标**：验证 deepcopy 产生**完全独立**的副本，并用 object dtype 复现「特例」的必要性。

**操作步骤**：

```python
import copy
import numpy as np

# 1) 普通 deepcopy：mask 与 fill_value 都不共享
a = np.ma.array([0, 1, 2], mask=[False, True, False])
c = copy.deepcopy(a)

c[1] = 1                     # 给副本的屏蔽位赋值
print("副本 mask :", c.mask.tolist())   # 预期 [False, False, False]（解除屏蔽）
print("原件 mask :", a.mask.tolist())   # 预期 [False, True, False]（不变）
print("mask 独立 :", id(a._mask) != id(c._mask))  # 预期 True

# 2) object dtype 特例：嵌套对象必须深拷贝
src = np.ma.array([[0, "x"], [[1, 2], "y"]],
                   mask=[[0, 1], [0, 0]], dtype=object)
dup = copy.deepcopy(src)
dup[1, 0].append("leak")     # 只改副本里的嵌套 list
print("原件嵌套长度 :", len(src[1, 0]))  # 预期 2（未受污染）
print("副本嵌套长度 :", len(dup[1, 0]))  # 预期 3
```

**需要观察的现象**：

- 普通 deepcopy：改副本 mask 不影响原件，且两个 `_mask` 对象身份不同。
- object dtype：修改副本里的嵌套 list 完全不污染原件，证明元素级深拷贝生效。

**预期结果**：与注释一致，对应 `test_deepcopy` 与 `test_deepcopy_2d_obj` 的断言。

> object dtype 深拷贝较慢（需逐元素递归），生产中对大 object 数组慎用 deepcopy。

#### 4.4.5 小练习与答案

**练习 1**：如果删掉 `__deepcopy__` 里的 `memo[id(self)] = copied`，对普通（非 object）掩码数组会有什么影响？

**参考答案**：对树形无环的普通数组，结果通常仍正确，但当数组之间或属性之间存在**循环引用**、或同一对象被多次引用时，缺少 memo 登记会导致递归复制甚至无限递归，或副本里「同一对象」变成多个不一致的拷贝。memo 是 `copy.deepcopy` 协议保证一致性的关键。

**练习 2**：为什么 object dtype 数组需要额外的 `copied._data[...] = deepcopy(copied._data)`？

**参考答案**：因为 object dtype 数组的元素是对任意 Python 对象的**引用**。前面 `MaskedArray.__new__(..., copy=True)` 只复制了 ndarray 容器（新的指针数组），但指针指向的还是**同一批** Python 对象。对嵌套可变对象（如 list）做就地修改会经由共享引用污染原件，因此必须递归 deepcopy 每个元素，再用 `[...] =` 就地写回。

---

### 4.5 mrecords 的并行实现：_mrreconstruct

本模块作为对照，简要说明 mrecords（字段级屏蔽记录数组）如何用**同构但独立**的一套函数实现持久化。它不改变「建壳 + 灌态」的范式，只是细节不同。

#### 4.5.1 概念说明

`MaskedRecords`（别名 `mrecarray`）继承自 `MaskedArray`，但其 `_data` 是 `recarray`、`_mask` 是「字段级」结构化布尔数组（见 u3-l5）。出于历史与结构原因，它**没有复用** core 的 `_mareconstruct`，而是自带一套 `_mrreconstruct` / `__getstate__` / `__setstate__` / `__reduce__`。

#### 4.5.2 源码精读与对照

[mrecords.py:453-460](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/mrecords.py#L453-L460) —— `_mrreconstruct`：mrecords 版的空壳构建器。

与 core 的 `_mareconstruct` 对照：

| 方面 | core `_mareconstruct` | mrecords `_mrreconstruct` |
| --- | --- | --- |
| 建 `_data` | `ndarray.__new__(baseclass, ...)` 再经 `subtype.__new__` | `np.ndarray.__new__(baseclass, ...).view(subtype)` 直接 view |
| 建 `_mask` | `ndarray.__new__(ndarray, ..., make_mask_descr(basetype))` | `np.ndarray.__new__(np.ndarray, ..., 'b1')`（直接用 `'b1'`） |
| 收尾 | `subtype.__new__(subtype, _data, mask=_mask, dtype=basetype)` | 同左 |

[mrecords.py:407-421](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/mrecords.py#L407-L421) —— `MaskedRecords.__getstate__`：直接手写七元组（含显式版本号 `1`），存 `self.dtype`、`self._data.tobytes()`、`self._mask.tobytes()`。

[mrecords.py:423-441](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/mrecords.py#L423-L441) —— `MaskedRecords.__setstate__`：还原时手动构造 mask dtype：

```python
mdtype = np.dtype([(k, np.bool) for (k, _) in self.dtype.descr])
```

这等价于 core 里的 `make_mask_descr(typ)`，只是 mrecords 手写了一遍逐字段布尔展开（u3-l5 会详述字段级屏蔽）。

[mrecords.py:443-450](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/mrecords.py#L443-L450) —— `MaskedRecords.__reduce__`：与 core 完全同形，只是 callable 换成 `_mrreconstruct`。

> 教学要点：core 与 mrecords 的持久化是**同构**的（都遵循建壳 + 灌态），mrecords 的独立实现更多是历史遗留与对 recarray 结构的适配，并非本质上不同的机制。理解了 core 这套，mrecords 自然可读。

---

## 5. 综合实践

把本讲四个模块串起来，完成一个「持久化体检」小任务。

**任务**：编写一个函数 `roundtrip(obj)`，它对一个掩码数组（或其子类、或 mrecarray）做 pickle 往返与 deepcopy，并返回一份「一致性报告」字典，检查 data、mask、fill_value、类型、`_baseclass` 是否一致，以及 deepcopy 的 `_mask` 是否独立。

**参考实现**（示例代码，非项目原有代码）：

```python
import pickle, copy
import numpy as np

def roundtrip(obj):
    pk = pickle.loads(pickle.dumps(obj, protocol=pickle.HIGHEST_PROTOCOL))
    dc = copy.deepcopy(obj)
    return {
        "pickle.data_ok"      : np.array_equal(pk._data, obj._data),
        "pickle.mask_ok"      : np.array_equal(getmaskarray(pk), getmaskarray(obj)),
        "pickle.fill_ok"      : (pk.fill_value == obj.fill_value),
        "pickle.type_ok"      : type(pk) is type(obj),
        "pickle.baseclass_ok" : pk._baseclass is obj._baseclass,
        "deepcopy.mask_indep" : id(dc._mask) != id(obj._mask),
    }

def getmaskarray(a):
    return np.ma.getmaskarray(a)

# 自测三种对象
a1 = np.ma.array([1.0, 2.0, 3.0], mask=[0, 1, 0])
a2 = np.array([(1.0, 2), (3.0, 4)],
              dtype=[('x', float), ('y', int)]).view(np.recarray)
a2 = np.ma.masked_array(a2, mask=[(True, False), (False, True)])
a3 = np.ma.array([[0, "x"], [[1, 2], "y"]],
                 mask=[[0, 1], [0, 0]], dtype=object)

for name, a in [("普通", a1), ("recarray 子基类", a2), ("object dtype", a3)]:
    print(name, roundtrip(a))
```

**预期结果**：三组对象的报告里所有键均为 `True`。其中 `a2` 检验 `_mareconstruct` 对 `recarray` 基类的还原（依赖 `baseclass` 参数），`a3` 检验 object dtype 的 deepcopy 独立性。

> 若 `a2` 的 `baseclass_ok` 在某些版本显示 `False`，请对照 `_mareconstruct` 与 `__new__` 中 `_baseclass` 的推断逻辑排查（待本地验证）。

---

## 6. 本讲小结

- **三件套都要序列化**：掩码数组的状态是七元组——前 5 项借自 ndarray（version/shape/dtype/字节序/raw），后 2 项是追加的 mask 字节与 fill_value。
- **建壳与灌态分离**：`__reduce__` 返回 `(_mareconstruct, args, state)`；反序列化时先由 `_mareconstruct` 造一个类型正确的空壳，再由 `__setstate__` 灌入真实状态。
- **mask dtype 是推导的**：`__setstate__` 用 `make_mask_descr(typ)` 从 data 的 dtype 重新算出 mask dtype，不单独存储，保证结构化屏蔽信息自洽往返。
- **子类与 `_baseclass` 都保住**：`_mareconstruct` 用 `self.__class__` 作 `subtype`、用 `self._baseclass` 造 `_data`，使自定义子类与 recarray 基类都能原样还原。
- **deepcopy 走另一条路**：`__deepcopy__` 用 `MaskedArray.__new__(..., copy=True)` 造独立 data/mask，再深拷贝 `__dict__` 全部簿记属性，并对 object dtype 额外逐元素深拷贝。
- **mrecords 是同构实现**：`_mrreconstruct` 与 core 的 `_mareconstruct` 范式相同，只是细节（view 方式、手写 mask dtype）不同，属历史与结构适配。

---

## 7. 下一步学习建议

- **u3-l5（mrecords 与字段级屏蔽）**：本讲 4.5 节已铺垫 `_mrreconstruct`，下一讲深入 `_fieldmask` / `recordmask` 的字段级屏蔽语义，可对照阅读 `MaskedRecords.__getstate__` 里 mask 字节如何承载字段信息。
- **u3-l2（子类化与 mvoid）**：若你对「自定义子类的额外属性如何跨 pickle 存活」感兴趣，回看 `_optinfo` 机制——它是 `__dict__` 深拷贝在 u3-l4 能正确复制额外属性的基础。
- **延伸阅读**：对照阅读 `numpy/core/_methods.py` 与 ndarray 的 `__reduce__`，理解被复用的五元组状态在普通 ndarray 层面的含义；并可阅读 CPython `copy` 模块源码，确认 `memo` 协议与本讲 `__deepcopy__` 的配合。
