# 属性访问魔法：`__getattribute__` 与 `__setattr__`

> 本讲承接 [u3-l1](u3-l1-recarray-class-constructor.md)。上一篇讲了 `recarray` 是如何被构造出来的、以及 `__array_finalize__` 如何把 `void` 提升为 `record`。本篇回答一个更日常的问题：**为什么 `r.x` 这种「点号取列」能工作？** 答案藏在两个魔法方法里。

## 1. 本讲目标

读完本讲，你应当能够：

- 说清楚 `r.x`（属性访问）和 `r['x']`（字典访问）背后走的是两条**不同的代码路径**。
- 复述 `recarray.__getattribute__` 的「先查对象属性、失败再查 `dtype.fields`」两级回退逻辑。
- 解释为什么「字段名与 `ndarray` 内置属性同名时，属性永远赢」，以及如何用 `r.field(...)` / `r['...']` 绕过它。
- 复述 `recarray.__setattr__` 的「先 `object.__setattr__`，若是字段名则撤销实例字典并改走 `setfield`」逻辑，并理解它为什么禁止「动态创建与字段同名的实例属性」。

## 2. 前置知识

- **属性查找（attribute lookup）**：Python 里 `obj.name` 默认会调用 `type(obj).__getattribute__(obj, 'name')`；`obj.name = v` 会调用 `type(obj).__setattr__(obj, 'name', v)`。子类重写这两个方法，就能拦截所有的点号读写。本讲的核心就是 `recarray` 重写了这两个方法。
- **`object.__getattribute__` / `object.__setattr__`**：这是 Python 内置的、**未被子类改写**的原始版本。`recarray` 在自定义逻辑里显式调用它们，是为了「先用正常规则找一次，找不到再退而求其次」，避免无限递归。
- **`dtype.fields` 与 `dtype.names`**：结构化 dtype 把每一「列」记在 `dtype.fields` 字典里，键是字段名，值是一个元组 `(子dtype, 字节偏移[, 标题])`；`dtype.names` 是字段名的有序元组。这一点是本讲的基石，下面 4.1 会展开。
- **`ndarray.getfield` / `ndarray.setfield`**：`ndarray` 原生的两个 C 层方法，按 `(子dtype, 偏移)` 切出/写入某一块内存。属性魔法最终都落在它们身上。

## 3. 本讲源码地图

本讲只涉及一个真实实现文件，但它在 `numpy/rec/` 子包里只被「再导出」。

| 文件 | 作用 |
| --- | --- |
| `numpy/_core/records.py` | `recarray`、`record` 的全部实现都在这里。本讲聚焦其中的 `recarray.__getattribute__`、`recarray.__setattr__`、`recarray.field` 三个方法。 |
| `numpy/rec/__init__.py` | 仅两行的再导出垫片，`from numpy._core.records import *`。本讲引用代码时一律指向 `_core/records.py`。 |

> 说明：任务给出的永久链接 base 指向 `numpy/rec/`，但真实代码位于 `numpy/_core/records.py`。为避免链接失效，下方所有源码链接都直接使用 `_core/records.py` 的绝对 GitHub 路径。

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：底层引擎（`dtype.fields` + `getfield`/`setfield`）、读取魔法（`__getattribute__`）、写入魔法（`__setattr__`）、名字冲突与逃生口（`field()`）。

### 4.1 底层引擎：`dtype.fields` 与 `getfield` / `setfield`

#### 4.1.1 概念说明

属性魔法 `r.x` 看起来像在访问对象属性，**本质却是在按字节偏移切内存**。这依赖两件 `ndarray` 原生能力：

1. **`dtype.fields`**：结构化 dtype 自带的「字段目录」。对每个字段，它记录了「这一列的数据是什么类型」以及「它在每一条记录的字节布局里从第几个字节开始」。普通 `ndarray` 的字典访问 `arr['x']` 也是基于它实现的——所以它并不是 `recarray` 独有的。
2. **`getfield` / `setfield`**：`ndarray` 的两个方法，给定 `(子dtype, 偏移)`，就能把整块内存重新解释成那个子 dtype 的数组（读）或写进去（写）。

`recarray` 做的事，说穿了就是：**把 `r.x` 这种点号写法，翻译成一次 `self.getfield(*dtype.fields['x'][:2])`**。理解了引擎，后面的两个魔法方法就只是「调度逻辑」。

#### 4.1.2 核心流程

设 `dt = r.dtype`，`dt.fields['x'] = (subdtype, offset)`。则对第 `i` 条记录读取字段 `'x'` 的内存起点为：

\[
\text{addr}(i,\,\text{'x'}) = \text{base} + i \cdot \text{itemsize} + \text{offset}
\]

其中 `itemsize = dt.itemsize` 是一条记录的总字节数。`getfield(subdtype, offset)` 返回的就是把「全部 N 条记录、各自从 `offset` 开始、按 `subdtype` 解读」拼成的一个新视图（不拷贝数据）。`setfield(val, subdtype, offset)` 是它的写对应版本。

- `dt.fields[name]` 的元组长度可能是 2（无标题）或 3（有标题），所以源码里一律取 `[:2]` 拿到 `(subdtype, offset)`。
- `getfield` 返回的视图其 `dtype.type` 会被继承（见 4.2），所以 `record` 标量类型能一路传下去。

#### 4.1.3 源码精读

字段目录在 `format_parser` 里被组装（参见 [u2-l1](u2-l1-format-parser.md)），最终落到 `dtype.fields`。`recarray` 内部并不直接碰 `fields` 字典的字节细节，而是把 `(subdtype, offset)` 整体交给 `getfield`/`setfield`。这两个方法继承自 `ndarray`，在 `records.py` 里被大量调用，例如 `field()` 方法就把它们当作底层原语：

[numpy/_core/records.py:539-554](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L539-L554) —— `field()` 先从 `dtype.fields` 取出 `[:2]`，再调用 `getfield`（读）或 `setfield`（写）：

```python
def field(self, attr, val=None):
    if isinstance(attr, int):
        names = ndarray.__getattribute__(self, 'dtype').names
        attr = names[attr]
    fielddict = ndarray.__getattribute__(self, 'dtype').fields
    res = fielddict[attr][:2]          # (subdtype, offset)
    if val is None:
        obj = self.getfield(*res)      # 读：按 (subdtype, offset) 切内存
        if obj.dtype.names is not None:
            return obj
        return obj.view(ndarray)
    else:
        return self.setfield(val, *res)  # 写：把 val 写进 (subdtype, offset)
```

注意它刻意用 `ndarray.__getattribute__(self, 'dtype')` 而不是 `self.dtype` 来取 dtype——目的是**绕过 `recarray.__getattribute__` 自定义逻辑**，直接拿到真正的 `dtype` 属性，避免任何歧义。

#### 4.1.4 代码实践

1. **目标**：亲眼看到 `dtype.fields` 的元组结构，确认属性魔法最终依赖的就是 `[:2]`。
2. **步骤**：

   ```python
   import numpy as np
   r = np.rec.array([(1, 2.0)], dtype=[('x', 'i4'), ('y', 'f8')])
   print(r.dtype.fields)        # 字段目录
   print(r.dtype.fields['x'])   # 某个字段的元组
   print(r.dtype.itemsize)      # 一条记录的总字节数
   ```
3. **预期现象**：`fields` 是一个字典视图；`fields['x']` 形如 `(dtype('int32'), 0)`，`fields['y']` 形如 `(dtype('float64'), 8)`（偏移 8 = `int32` 的 4 字节 + 对齐）。
4. **预期结果**：字段 `'x'` 的偏移为 `0`，字段 `'y'` 的偏移为 `8`，`itemsize` 为 `16`（或按对齐规则略有不同）。精确数值**待本地验证**，取决于机器上的对齐结果。
5. 用 `r.getfield(np.dtype('i4'), 0)` 直接读字段 `'x'`，结果应与 `r.x` 一致。

#### 4.1.5 小练习与答案

- **练习**：为什么源码里取字段时写 `fielddict[attr][:2]` 而不是直接 `fielddict[attr]`？
- **答案**：因为 `dtype.fields[name]` 的元组在有标题时是 3 元 `(子dtype, 偏移, 标题)`，无标题时是 2 元。`getfield`/`setfield` 只接受 `(子dtype, 偏移)`，`[:2]` 是兼容两种情况的安全切片。

---

### 4.2 读取魔法：`recarray.__getattribute__` 的两级回退

#### 4.2.1 概念说明

`r.x` 为什么能取到字段？因为 `recarray` 重写了 `__getattribute__`。它的策略是一个**两级回退（fallback）**：

1. **第一级**：先用 `object.__getattribute__` 按「正常对象属性」找一遍。`shape`、`dtype`、`T`、各种方法……这些 `ndarray` 本来就有的属性，在这一级就被命中并返回。
2. **第二级**：只有第一级抛 `AttributeError` 时，才把名字当成字段名，去 `dtype.fields` 里查，命中则用 `getfield` 切出该列。

这个顺序有一个直接后果（也是源码注释点明的）：**当一个字段名恰好和某个 `ndarray` 内置属性同名时，第一级会先赢，字段永远无法用点号访问到。** 这正是后面 4.4 要解决的「冲突」问题。

#### 4.2.2 核心流程

伪代码（读 `r.attr`）：

```
try:
    return object.__getattribute__(self, attr)   # 第一级：正常属性
except AttributeError:
    pass                                         # 落到第二级
fielddict = dtype.fields
try:
    (subdtype, offset) = fielddict[attr][:2]
except (TypeError, KeyError):
    raise AttributeError("recarray has no attribute ...")
obj = self.getfield(subdtype, offset)            # 列视图
# 收尾：让返回值的类型“看起来对”
if obj 是结构化(dtype.names is not None):
    if obj.dtype.type 是 void 子类:
        return obj.view((self.dtype.type, obj.dtype))  # 保留 record 语义
    return obj
else:
    return obj.view(ndarray)                     # 非结构化字段 → 退回普通 ndarray
```

收尾分支的意义：

- 字段本身是标量类型（如 `'f8'`）时，`getfield` 返回的列视图本来会**继承 `recarray` 类型**，但它没有字段，保留 `recarray` 外壳没有意义反而误导，所以 `view(ndarray)` 退回普通数组。
- 字段本身是嵌套结构化类型时，需要用 `(self.dtype.type, obj.dtype)` 重新指定 `dtype.type`，把外层的 `record` 语义传播到嵌套字段（因为嵌套字段不会自动继承 `type`）。

#### 4.2.3 源码精读

[numpy/_core/records.py:415-443](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L415-L443) —— 完整的 `__getattribute__`，注释直接点明了冲突行为：

```python
def __getattribute__(self, attr):
    # See if ndarray has this attr, and return it if so. (note that this
    # means a field with the same name as an ndarray attr cannot be
    # accessed by attribute).
    try:
        return object.__getattribute__(self, attr)
    except AttributeError:  # attr must be a fieldname
        pass
    fielddict = ndarray.__getattribute__(self, 'dtype').fields
    try:
        res = fielddict[attr][:2]
    except (TypeError, KeyError) as e:
        raise AttributeError(f"recarray has no attribute {attr}") from e
    obj = self.getfield(*res)
    if obj.dtype.names is not None:
        if issubclass(obj.dtype.type, nt.void):
            return obj.view(dtype=(self.dtype.type, obj.dtype))
        return obj
    else:
        return obj.view(ndarray)
```

要点：

- 第 425 行用 `ndarray.__getattribute__(self, 'dtype')` 取 `dtype`，同样是为了绕过自身重写、避免歧义。
- `TypeError` 分支是为了应对 `fielddict` 为 `None`（非结构化 dtype）或键不可哈希的情况；`KeyError` 是字段名不存在。两种都统一转成 `AttributeError`，让外部表现为「没有这个属性」。

> 对照：`record` 标量类有**类似但更简单**的 `__getattribute__`，见 [numpy/_core/records.py:215-237](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L215-L237)。它对 `setfield`/`getfield`/`dtype` 三个名字做了短路（直接走 `nt.void.__getattribute__`），因为这三个名字既是方法又是字段查找的依赖，必须优先保证拿到方法本身。

#### 4.2.4 代码实践

1. **目标**：观察两级回退——「真属性」第一级命中，「字段」走第二级，「都不存在」抛 `AttributeError`。
2. **步骤**：

   ```python
   import numpy as np
   r = np.rec.array([(1, 2.0), (3, 4.0)], dtype=[('x', 'i4'), ('y', 'f8')])

   print(r.x)        # 字段：走第二级 → [1, 3]
   print(r.shape)    # 真属性：第一级命中 → (2,)
   print(type(r.x))  # 字段是标量列 → 退回 ndarray
   r.nope            # 既不是属性也不是字段
   ```
3. **预期现象**：`r.x` 打印 `[1 3]`；`r.shape` 打印 `(2,)`；`type(r.x)` 是 `numpy.ndarray`（而非 `recarray`，因为标量列被 `view(ndarray)` 退回）；`r.nope` 抛 `AttributeError: recarray has no attribute nope`。
4. **预期结果**：如上。`type(r.x)` 退回普通 `ndarray` 这一点，正好对应源码第 443 行的 `obj.view(ndarray)` 分支。

#### 4.2.5 小练习与答案

- **练习 1**：如果把一个字段命名为 `'T'`（`ndarray` 的转置属性），`r.T` 返回的是字段还是转置？
- **答案**：返回**转置**。第一级 `object.__getattribute__` 会命中 `ndarray.T`，根本不会落到字段查找。要读字段必须用 `r.field('T')` 或 `r['T']`。
- **练习 2**：为什么 `__getattribute__` 里捕获的是 `AttributeError`，而不是 `Exception`？
- **答案**：只拦截「找不到该属性」这一种正常回退信号；其他真正的异常（如内存错误）必须继续向上抛，不能被吞掉误当成「这是个字段名」。

---

### 4.3 写入魔法：`recarray.__setattr__` 的撤销与重路由

#### 4.3.1 概念说明

读有魔法，写也有。`r.x = v` 看起来像给对象属性赋值，但 `recarray.__setattr__` 会判断这个名字是不是字段名：

- **不是字段名**（如 `r.mytag = 1`）：按普通对象属性赋值，存进实例 `__dict__`。
- **是字段名**（如 `r.x = 5`）：**先撤销**刚才那次普通赋值（避免字段名以「实例属性」的形式残留在 `__dict__` 里），然后改走 `setfield` 把数据真正写进字段的字节里。

这套「撤销 + 重路由」是为了实现源码注释点明的一条规则：**你不能动态创建一个与字段同名的实例属性**。否则那个实例属性会污染第一级查找，让读魔法失效。

此外，`__setattr__` 还承担一个特殊职责：当被赋值的是 `dtype` 本身（发生在 view 等操作内部），若它是一个 `void` 结构化类型，就自动提升成 `(record, dtype)`，保证 `dtype.type` 始终是 `record`——这与 [u3-l1](u3-l1-recarray-class-constructor.md) 讲的 `__array_finalize__` 是同一条「保 record 语义」主线的另一只手。

#### 4.3.2 核心流程

伪代码（执行 `r.attr = val`）：

```
1. 特例：若 attr == 'dtype' 且 val 是 void 结构化类型
      → val = dtype((record, val))      # 提升 dtype.type 为 record
2. newattr = attr not in self.__dict__  # 记住：赋值前它是否不在实例字典里
3. try:
      ret = object.__setattr__(self, attr, val)   # 先按普通属性赋值
   except Exception:
      若 attr 不是字段名 → 原样 raise
      （是字段名 → 继续往下走 setfield）
   else:
      若 attr 不是字段名 → return ret（普通赋值成功，结束）
      若 attr 是字段名 且 newattr（刚被塞进 __dict__）:
          object.__delattr__(self, attr)          # 撤销那次实例字典写入
4. res = fielddict[attr][:2]
   return self.setfield(val, *res)                # 真正写字段
```

关键点：步骤 3 里 `object.__setattr__` 对一个「普通字段名」（如 `'x'`，不是 `ndarray` 真属性）会**成功**地把它放进实例 `__dict__`；但因为我们随后发现 `'x'` 是字段名，就必须把它从 `__dict__` 里删掉，否则它会变成一个真实例属性，永久遮挡字段读取。

#### 4.3.3 源码精读

[numpy/_core/records.py:449-484](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L449-L484) —— 完整的 `__setattr__`，注释说明了「撤销 + setfield」的设计意图：

```python
# Save the dictionary.
# If the attr is a field name and not in the saved dictionary
# Undo any "setting" of the attribute and do a setfield
# Thus, you can't create attributes on-the-fly that are field names.
def __setattr__(self, attr, val):
    # Automatically convert (void) structured types to records
    if (
        attr == 'dtype' and
        issubclass(val.type, nt.void) and
        val.names is not None
    ):
        val = sb.dtype((record, val))

    newattr = attr not in self.__dict__
    try:
        ret = object.__setattr__(self, attr, val)
    except Exception:
        fielddict = ndarray.__getattribute__(self, 'dtype').fields or {}
        if attr not in fielddict:
            raise
    else:
        fielddict = ndarray.__getattribute__(self, 'dtype').fields or {}
        if attr not in fielddict:
            return ret
        if newattr:
            try:
                object.__delattr__(self, attr)
            except Exception:
                return ret
    try:
        res = fielddict[attr][:2]
    except (TypeError, KeyError) as e:
        raise AttributeError(f"record array has no attribute {attr}") from e
    return self.setfield(val, *res)
```

要点解读：

- `fielddict = ... .fields or {}`：当 dtype 非结构化时 `fields` 为 `None`，用 `or {}` 兜底，避免后续 `attr not in fielddict` 报错。
- 第 460 行 `newattr` 记录「这次赋值是否新增了一个实例属性」。只有「新增」的才需要 `delattr` 撤销；如果它原本就在 `__dict__`（不太常见），则保留原状。
- 最终一切都收束到 `self.setfield(val, *res)`——写魔法和读魔法共享同一个底层引擎（4.1）。

> ⚠️ 一个边界情况：若字段名恰好是 `ndarray` 的真属性（如 `'shape'`，它是带 setter 的数据描述符），`object.__setattr__(self, 'shape', val)` 会触发 `ndarray.shape` 的 setter。这种名字冲突在写入侧行为复杂，**最佳实践是改用 `r['shape'] = val` 或 `r.field('shape', val)`**，它们走 `__setitem__`/`setfield`，完全绕开 `__setattr__`。

#### 4.3.4 代码实践

1. **目标**：验证 `r.x = v` 走的是 `setfield`（写字段），而不是创建实例属性；同时验证「字段名不能成为实例属性」。
2. **步骤**：

   ```python
   import numpy as np
   r = np.rec.array([(1, 2.0), (3, 4.0)], dtype=[('x', 'i4'), ('y', 'f8')])

   r.x = np.array([10, 30])      # 应写入字段 x
   print(r.x)                    # 期望 [10 30]
   print('x' in r.__dict__)      # 期望 False：x 不在实例字典里

   r.mytag = 99                  # mytag 不是字段 → 普通实例属性
   print('mytag' in r.__dict__)  # 期望 True
   print(r.mytag)                # 期望 99
   ```
3. **预期现象**：`r.x` 变成 `[10 30]`；`'x' in r.__dict__` 为 `False`（被撤销了）；`'mytag' in r.__dict__` 为 `True`。
4. **预期结果**：如上。这正对应源码里「字段名 → `delattr` 撤销 + `setfield`」「非字段名 → 普通赋值」两条路径。
5. 若想直接确认底层走的是 `setfield`，可在 `r.x = ...` 前后用 `r.field('x')` 读取，应与 `r.x` 一致。

#### 4.3.5 小练习与答案

- **练习**：假设没有第 471–477 行的 `object.__delattr__(self, attr)` 撤销逻辑，执行 `r.x = 5`（`x` 是字段名）后会出什么问题？
- **答案**：`object.__setattr__` 会把 `x` 放进实例 `__dict__`；之后读 `r.x` 时，`__getattribute__` 第一级 `object.__getattribute__` 会命中这个实例属性并返回 `5`（一个 Python 整数），而不是字段列视图。字段读写会被一个固定的实例属性「粘死」，读魔法彻底失效。撤销逻辑正是为了防止这种污染。

---

### 4.4 名字冲突与 `field()` / `[]` 逃生口

#### 4.4.1 概念说明

4.2 已经点明：字段名一旦和 `ndarray` 内置属性（`shape`、`T`、`dtype`、`size`、`data`、`real`、`imag`……）撞名，**点号访问永远拿到的是 `ndarray` 属性**，字段拿不到。这不是 bug，而是「第一级优先」这一明确设计的必然结果。

为此 `recarray` 提供了两条**逃生口**，它们都绕开 `__getattribute__` 的两级回退：

1. **`r.field(name)`**：`recarray` 自定义的方法，直接查 `dtype.fields`，不经过第一级对象属性查找。它也支持整数下标和写入 `r.field(name, val)`。
2. **`r[name]`**：`__getitem__` 路径，对结构化数组，`ndarray` 原生就支持用字符串字段名取列，同样不走属性查找。

这两条是处理字段名冲突（以及「字段名不是合法 Python 标识符」，如带空格或以数字开头的名字）的标准做法。

#### 4.4.2 核心流程

读取冲突字段 `'shape'` 的三条路径对比：

| 写法 | 入口 | 命中什么 | 结果 |
| --- | --- | --- | --- |
| `r.shape` | `__getattribute__` 第一级 | `ndarray.shape` 属性 | 数组形状元组 |
| `r['shape']` | `__getitem__` → `ndarray` 字段索引 | 字段 | 该列（普通 ndarray） |
| `r.field('shape')` | `field()` 直接查 `fields` | 字段 | 该列（普通 ndarray） |

`field()` 的判定顺序：整数下标 → 用 `dtype.names` 换成字段名；查 `fields[attr][:2]`；有 `val` 则 `setfield`，无 `val` 则 `getfield`。

#### 4.4.3 源码精读

`field()` 已在 4.1.3 引用（[numpy/_core/records.py:539-554](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L539-L554)）。它和 `__getattribute__` 的根本区别在于：**`field()` 从不调用 `object.__getattribute__` 去找对象属性**，因此不会被 `shape` 这样的内置属性截胡。

`__getitem__` 则见 [numpy/_core/records.py:486-501](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L486-L501)，它先 `super().__getitem__(indx)`（即 `ndarray` 的实现，天然支持字符串字段名），再对返回值做与 `__getattribute__` 类似的收尾（结构化 → 保留 `recarray`/`record` 语义；标量列 → 退回 `ndarray`）：

```python
def __getitem__(self, indx):
    obj = super().__getitem__(indx)
    if isinstance(obj, ndarray):
        if obj.dtype.names is not None:
            obj = obj.view(type(self))
            if issubclass(obj.dtype.type, nt.void):
                return obj.view(dtype=(self.dtype.type, obj.dtype))
            return obj
        else:
            return obj.view(type=ndarray)
    else:
        return obj
```

#### 4.4.4 代码实践

1. **目标**：构造一个含字段 `'shape'` 的 record array，对比三种访问方式。
2. **步骤**：

   ```python
   import numpy as np
   r = np.rec.array([(1, 2), (3, 4)], dtype=[('shape', 'i4'), ('val', 'i4')])

   print(r.shape)              # A: ndarray 形状
   print(r['shape'])           # B: 字段（逃生口 1）
   print(r.field('shape'))     # C: 字段（逃生口 2）
   ```
3. **预期现象**：
   - A 打印 `(2,)`（两条记录，一维）——拿到的是 `ndarray.shape`，**不是**字段。
   - B、C 都打印 `[1 3]`——拿到的是字段 `'shape'` 这一列。
4. **预期结果**：如上。A 与 B/C 的差异，正是 4.2 「第一级优先」的直接体现。逃生口 B/C 绕开了 `__getattribute__`，所以能取到被遮蔽的字段。

#### 4.4.5 小练习与答案

- **练习**：若字段名是 `'123'`（不是合法标识符），`r.123` 显然语法非法。请说出两种仍能访问该字段的方法。
- **答案**：用 `r['123']`（`__getitem__`），或 `r.field('123')`（`field()`）。两者都不依赖点号属性语法，因此能处理任意字符串字段名。
- **延伸**：`field()` 还支持整数下标 `r.field(0)` 取第 0 个字段（源码第 540–542 行先把整数映射成 `dtype.names[0]`），这在字段名不便使用时很方便。

---

## 5. 综合实践

把四个模块串起来，做一个「冲突字段全流程」小任务。

**任务**：构造一个 record array，它的字段名故意叫 `'shape'`；分别用三种方式读它、用两种方式写它，并解释每种现象背后的源码路径。

```python
import numpy as np

# 1) 构造：字段名 'shape' 与 ndarray.shape 冲突
r = np.rec.array([(10, 1.0), (20, 2.0), (30, 3.0)],
                 dtype=[('shape', 'i4'), ('w', 'f8')])

# 2) 读：三种路径
print("r.shape       ->", r.shape)         # 走 __getattribute__ 第一级 → ndarray 形状
print("r['shape']    ->", r['shape'])      # 走 __getitem__ → 字段列
print("r.field('shape') ->", r.field('shape'))  # 走 field() → 字段列

# 3) 写：两种路径，都绕开 __setattr__ 的 shape-setter 干扰
r['shape'] = np.array([100, 200, 300])
print("after []=     ->", r.field('shape'))
r.field('shape', np.array([7, 8, 9]))
print("after field=  ->", r.field('shape'))

# 4) 解释检查：'shape' 不应残留在实例字典里
print("'shape' in __dict__ ->", 'shape' in r.__dict__)
```

**需要解释**：

1. 为什么 `r.shape` 拿到的是 `(3,)` 而不是字段？——指向 `__getattribute__` 第一级（[records.py:419-420](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L419-L420)）。
2. 为什么 `r['shape']` 和 `r.field('shape')` 能拿到字段？——指向 `field()`（[records.py:539-554](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L539-L554)）与 `__getitem__`（[records.py:486-501](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L486-L501)），它们不经过第一级对象属性查找。
3. 写入为什么推荐用 `r['shape'] = ...` / `r.field('shape', ...)` 而不是 `r.shape = ...`？——因为 `__setattr__` 里 `object.__setattr__(self, 'shape', val)` 会触发 `ndarray.shape` 的 setter（[records.py:461-462](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L461-L462)），对冲突字段行为不可控。

**预期结果**：`r.shape` → `(3,)`；两次逃生口写入后字段列分别为 `[100 200 300]`、`[7 8 9]`；`'shape' in __dict__` → `False`。具体打印格式**待本地验证**。

## 6. 本讲小结

- `r.x` 与 `r['x']` 走**两条不同代码路径**：前者经过 `recarray.__getattribute__` 的两级回退，后者经过 `__getitem__`（底层 `ndarray` 字段索引）。
- 读写魔法的底层引擎是 `dtype.fields`（字段目录，元组 `(子dtype, 偏移[, 标题]`，取 `[:2]`）+ `ndarray.getfield`/`setfield`（按偏移切/写内存）。
- `__getattribute__` 先 `object.__getattribute__` 找对象属性，失败才查 `dtype.fields`；因此**字段名与 `ndarray` 内置属性同名时，属性永远赢**。
- `__setattr__` 先 `object.__setattr__`，发现是字段名就**撤销**那次实例字典写入并改走 `setfield`，从而**禁止动态创建与字段同名的实例属性**；对 `dtype` 赋值时还会把 `void` 结构化类型提升为 `record`。
- 冲突字段（以及非法标识符字段名）要用逃生口 `r.field(name)` 或 `r[name]` 访问；写冲突字段同理优先用 `r[name] = v` / `r.field(name, v)`。

## 7. 下一步学习建议

- 下一篇 [u3-l3 record 标量类型与字段属性访问](u3-l3-record-scalar.md) 会把同样的「属性魔法」下沉到**单条记录**层面：`record(nt.void)` 自己也重写了 `__getattribute__`/`__setattr__`（[records.py:215-249](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L215-L249)），并多了 `__getitem__`、`pprint` 等。建议对照本讲，体会「数组级 `recarray`」与「标量级 `record`」两套魔法的同与不同。
- 想验证本讲行为的最权威来源是测试套件 `numpy/_core/tests/test_records.py`（[u5-l3](u5-l3-testing-pitfalls.md) 会专门讲），其中 `TestRecord` 覆盖了字段名冲突与属性访问的典型用例。
- 若想深入「保 `record` 语义」这条主线，可重读 [u3-l1](u3-l1-recarray-class-constructor.md) 的 `__array_finalize__`，并把它与本讲 `__setattr__` 里对 `dtype` 的提升逻辑并排看——它们是同一目标的两只手。
