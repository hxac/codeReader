# `record` 标量类型与字段属性访问

> 本讲承接 [u3-l2](u3-l2-attribute-access-magic.md)。上一篇讲了 **数组级** 的属性魔法：`recarray.__getattribute__` / `__setattr__` 如何把 `r.x` 翻译成 `getfield`/`setfield`。本篇把镜头推到 **标量级**：当你写出 `r[0].x` 时，`r[0]` 取出来的是一个什么样的对象？为什么它也能用点号访问字段？答案就是本讲的主角——`numpy.record` 标量类型。

## 1. 本讲目标

读完本讲，你应当能够：

- 说清楚 `record` 是 `numpy.void` 的子类，是一条结构化记录在内存里的「单行」标量形态，并解释它为什么对外显示为 `numpy.record` 而不是 `numpy.rec.record`。
- 复述 `record.__getattribute__` 的「保留名优先、再查对象属性、最后查 `dtype.fields`」三级逻辑，并能指出它与数组级 `recarray.__getattribute__` 的关键差别。
- 解释 `record.__getitem__` 为什么要在结果是嵌套结构化 `void` 时再 `view((self.__class__, obj.dtype))`——也就是它如何把 `record` 身份「传递」到嵌套字段里。
- 说清楚 `record.pprint()` 按字段名右对齐美化输出的实现，并知道它 `return` 字符串、而非真的 `print`。

## 2. 前置知识

- **标量（scalar）与 0-d 视图**：在 NumPy 里，从一个结构化数组里取「第 0 条记录」`a[0]`，得到的不是 Python tuple，而是一个 **固定字节数的标量对象**——它就是这条记录在内存里的那 `itemsize` 个字节。对普通结构化数组，这个标量的类型是 `numpy.void`；对 record array，则是本讲的 `numpy.record`。`record` 继承自 `void`，所以底层字节布局完全相同，只是多了「点号取字段」的能力。
- **`nt.void`**：即 `numpy.void`，是所有「结构化/原始字节」标量的基类（`kind='V'`）。它自带 `getfield` / `setfield` / `item` / `.dtype` 等 C 层方法。`record` 不重写这些方法，而是直接继承复用。`getfield(dtype, offset)` 按 `(子dtype, 偏移)` 切出某字段的字节并重新解释；`setfield(val, dtype, offset)` 是写对应版本。这套引擎已在 [u3-l2](u3-l2-attribute-access-magic.md) 的 4.1 节讲透，本讲直接复用其结论。
- **`dtype.fields` / `dtype.names`**：字段目录与字段名元组。`dtype.fields[name]` 是 `(子dtype, 偏移[, 标题])`，源码一律取 `[:2]` 得到 `(子dtype, 偏移)`。本讲所有点号访问最终都落到这两个值上。
- **`view((type, dtype))` 二元 dtype**：`(record, descr)` 这种写法保持 `descr` 的结构不变，只把标量类型 `.type` 从 `void` 换成 `record`。这是 [u3-l1](u3-l1-recarray-class-constructor.md) 引入的核心机制，本讲会在「嵌套字段」里再次用到它。

## 3. 本讲源码地图

本讲的真实实现集中在一个文件里，`numpy/rec/` 子包只是它的再导出垫片。

| 文件 | 作用 |
| --- | --- |
| `numpy/_core/records.py` | `record` 类的全部实现都在这里（约 196–267 行）。本讲聚焦 `record.__getattribute__`、`record.__setattr__`、`record.__getitem__`、`record.pprint`，以及 `__repr__`/`__str__`。 |
| `numpy/_core/numerictypes.py` | 定义 `nt.void`（`numpy.void`）所在的数值类型体系。本讲用它说明 `record` 在类型层级里的位置。 |
| `numpy/_core/tests/test_records.py` | 用真实测试断言为「标量级」行为背书：`test_recarray_returntypes`、`test_record_scalar_setitem`、`test_assignment1`、`test_nested_fields_are_records`。 |
| `numpy/rec/__init__.py` | 再导出垫片，`from numpy._core.records import *`。引用代码时一律指向 `_core/records.py`。 |

> 说明：任务给出的永久链接 base 指向 `numpy/rec/`，但真实代码位于 `numpy/_core/records.py`。为避免链接失效，下方所有源码链接都直接使用 `_core/records.py` 的绝对 GitHub 路径。

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：标量本体与身份（`record(nt.void)` + `__repr__`/`__str__`）、标量级读写魔法（`__getattribute__`/`__setattr__` + `getfield`/`setfield`）、嵌套字段与索引（`__getitem__` 如何保住 `record` 身份）、美化打印（`pprint`）。

### 4.1 `record` 标量本体：结构化记录的「单行」形态

#### 4.1.1 概念说明

把一个 record array 想象成一张表：每一「行」是一条记录。`r[0]` 取出的就是**单独一行**——它在内存里就是 `dtype.itemsize` 个字节。这行字节在普通结构化数组里被包成 `numpy.void` 标量，在 record array 里被包成 `numpy.record` 标量：

```
record(nt.void)
   │  继承自 numpy.void（kind='V'）
   │  字节布局与 void 完全相同
   └─ 多出来的能力：r[0].x 这样的点号取字段、pprint 美化打印
```

为什么需要一个专门的 `record` 类，而不是直接用 `void`？因为 `void` 标量**只支持字典式访问** `a[0]['x']`，不支持点号 `a[0].x`。`record` 子类重写了 `__getattribute__`/`__setattr__`/`__getitem__`，把点号也接通到 `dtype.fields` 上。这样数组级 `r.x` 与标量级 `r[0].x` 才能同时可用——这正是 [u3-l1](u3-l1-recarray-class-constructor.md) 里「二元 dtype `(record, descr)`」要达成的目标。

#### 4.1.2 核心流程

`record` 标量的生命周期很短：它通常由 `recarray.__getitem__`（取一行）或 `record.__getattribute__`（取嵌套字段）按需生成，用完即弃。其身份由三件事决定：

1. **类型层级**：`issubclass(np.record, np.void)` 为真，`kind='V'`。
2. **对外名字**：手动设 `__name__='record'`、`__module__='numpy'`，所以 `type(r[0])` 显示为 `numpy.record`，公开地址是 `numpy.record`（**不是** `numpy.rec.record`）。这一点与 `recarray`、`format_parser` 等用 `@set_module('numpy.rec')` 装饰的符号不同——`record` 是唯一手动改 module 的例外。
3. **打印行为**：`__repr__`/`__str__` 受 legacy 打印模式影响，老模式（`<=113`）下走 `__str__`/`item()`，新模式下交给 `void` 父类。

#### 4.1.3 源码精读

`record` 类的定义与身份设置只有几行：

[numpy/_core/records.py:196-203](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L196-L203) —— `record` 继承自 `nt.void`（即 `numpy.void`），并手动把 `__name__`/`__module__` 改成 `numpy.record`：

```python
class record(nt.void):
    """A data-type scalar that allows field access as attribute lookup.
    """
    # manually set name and module so that this class's type shows up
    # as numpy.record when printed
    __name__ = 'record'
    __module__ = 'numpy'
```

注释明确说明了「手动设置 name 和 module，使得这个类的类型在打印时显示为 `numpy.record`」。注意它**没有** `@set_module('numpy.rec')` 装饰器——这是有意为之，让 `record` 的正式地址落在顶层 `numpy` 命名空间，而不是子包 `numpy.rec`。

`nt.void` 在类型层级里的位置：

[numpy/_core/numerictypes.py:70-76](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/numerictypes.py#L70-L76) —— `void` 属于 `flexible` 分支，`kind='V'`：

```
 +-> flexible
 |   +-> character
 |   |     bytes_                           (kind=S)
 |   |     str_                             (kind=U)
 |   |
 |   \\-> void                              (kind=V)
 \\-> object_ (not used much)               (kind=O)
```

`__repr__` / `__str__` 的 legacy 分支：

[numpy/_core/records.py:205-213](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L205-L213) —— 老打印模式（`<=113`）下，`__repr__` 退化为 `__str__`，`__str__` 退化为 `str(self.item())`；新模式交给父类 `void`：

```python
def __repr__(self):
    if _get_legacy_print_mode() <= 113:
        return self.__str__()
    return super().__repr__()

def __str__(self):
    if _get_legacy_print_mode() <= 113:
        return str(self.item())
    return super().__str__()
```

`_get_legacy_print_mode` 从 `arrayprint` 导入（见文件顶部 import）。`item()` 把标量转成等价的 Python 内置类型（如 tuple / int / float），所以老模式下 `print(r[0])` 看起来像一个普通 tuple。

#### 4.1.4 代码实践

1. **目标**：亲手取出一个 `record` 标量，确认它的类型、`__module__`、以及与 `void` 的继承关系。
2. **步骤**：

   ```python
   import numpy as np
   r = np.rec.fromrecords([(1, 'a', 1.1), (2, 'b', 2.2)], names='id,name,val')
   one = r[0]                 # 取出第 0 条记录
   print(type(one))           # 标量类型
   print(type(one).__module__, type(one).__name__)
   print(issubclass(np.record, np.void))   # 继承关系
   print(one)                 # 打印效果（受 legacy 模式影响）
   ```
3. **预期现象**：`type(one)` 为 `<class 'numpy.record'>`；`__module__`/`__name__` 分别是 `numpy` 和 `record`；`issubclass` 为 `True`。
4. **预期结果**：`one` 是一个 `numpy.record` 标量，是 `numpy.void` 的子类；打印形态在新模式下形如 `(1, 'a', 1.1)`，具体是否带括号/引号**待本地验证**（取决于打印模式）。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 `record` 要手动写 `__module__ = 'numpy'`，而不是像 `recarray` 那样用 `@set_module('numpy.rec')`？
- **答案**：因为 `record` 的公开地址被刻意安排在顶层 `numpy.record`（历史上一直如此，且测试里到处用 `np.record`），而不是子包 `numpy.rec.record`。`@set_module` 装饰器会把 `__module__` 改成参数值，这里要的目标就是 `numpy`，手动赋值与装饰器效果一致，且更直白地表达「例外」意图。
- **练习 2**：`np.record` 和 `np.void` 在内存布局上有区别吗？
- **答案**：没有。`record` 只重写了几个 Python 层魔法方法，字节布局、`itemsize`、字段偏移全部继承自 `void`。同一个结构化 dtype 下，`record` 标量和 `void` 标量占用的字节数完全相同。

---

### 4.2 标量级读写魔法：`record.__getattribute__` 与 `__setattr__`

#### 4.2.1 概念说明

`record` 的点号魔法与数组级 `recarray` 的点号魔法**思路同源，但实现更简洁**。它们都把 `obj.name` 翻译成一次按 `(子dtype, 偏移)` 的 `getfield`/`setfield`，区别在于：

- `recarray` 用 `object.__getattribute__` 先查对象属性（数组级属性多，要小心和字段名冲突）。
- `record` 用 `nt.void.__getattribute__` 先查标量属性，并额外**硬保留三个名字** `('setfield', 'getfield', 'dtype')`——这三个名字永远走真正的属性，绝不退化成字段查找。

为什么单独保留这三个？因为它们是整个字段访问机制的「地基」：`__getattribute__` 自身要靠 `nt.void.__getattribute__(self, 'dtype')` 拿到真正的 `dtype` 才能读 `fields`；读写最终要靠 `getfield`/`setfield`。如果允许某个字段名叫 `dtype`，属性访问就会和机制本身打架。所以这三个名字被「锁死」，字段若与之一名，只能用 `record['dtype']`（字典式）访问。

#### 4.2.2 核心流程

读取 `rec.name` 的伪代码：

```
if name in ('setfield', 'getfield', 'dtype'):   # 保留名：直接返回真属性
    return void.__getattribute__(self, name)
try:
    return void.__getattribute__(self, name)     # 1) 先按正常属性找
except AttributeError:
    pass                                          # 2) 找不到才认为是字段
res = self.dtype.fields.get(name)               # 3) 在字段目录里查
if res is None:
    raise AttributeError(...)
obj = self.getfield(*res[:2])                   # 4) 按 (子dtype, 偏移) 切内存
if 该字段本身是结构化的(dt.names is not None):
    return obj.view((record, obj.dtype))        # 5) 嵌套结构 → 再次包成 record
return obj                                       # 普通标量字段 → 原样返回
```

写入 `rec.name = val` 更短：保留名直接报错（禁止 set `dtype`/`getfield`/`setfield`）；是字段名就 `setfield(val, 子dtype, 偏移)`；是已存在的真属性就 `void.__setattr__`；都不是就 `AttributeError`。

注意一个与数组级不同的细节：标量级 `__getattribute__` 用的是 `fielddict.get(attr, None)` + `if res:` 的「软查找」，而数组级用的是 `fielddict[attr][:2]` 的「硬查找」。这意味着标量级对「字段值为空/ falsy」理论上更宽容——但因为 `res` 永远是一个非空元组 `(子dtype, 偏移[, 标题])`，实际效果一致。

#### 4.2.3 源码精读

[numpy/_core/records.py:215-237](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L215-L237) —— `record.__getattribute__`：保留名优先，再查对象属性，最后查 `dtype.fields` 并 `getfield`：

```python
def __getattribute__(self, attr):
    if attr in ('setfield', 'getfield', 'dtype'):
        return nt.void.__getattribute__(self, attr)
    try:
        return nt.void.__getattribute__(self, attr)
    except AttributeError:
        pass
    fielddict = nt.void.__getattribute__(self, 'dtype').fields
    res = fielddict.get(attr, None)
    if res:
        obj = self.getfield(*res[:2])
        # if it has fields return a record, otherwise return the object
        try:
            dt = obj.dtype
        except AttributeError:
            # happens if field is Object type
            return obj
        if dt.names is not None:
            return obj.view((self.__class__, obj.dtype))
        return obj
    else:
        raise AttributeError(f"'record' object has no attribute '{attr}'")
```

逐点解读：

- **保留名分支**（前两行）：`dtype`/`getfield`/`setfield` 永远返回真属性。这条捷径也保证了下面 `nt.void.__getattribute__(self, 'dtype')` 取到的一定是真正的 dtype。
- **`obj.dtype` 的 `try/except AttributeError`**：当字段类型是 `object`（`'O'`）时，`getfield` 返回的是一个任意 Python 对象，它**可能没有 `.dtype`** 属性，于是直接返回该对象。这正是为 `test_objview_record` 这类含 `object` 字段的场景兜底。
- **嵌套结构再 `view`**：`dt.names is not None` 表示该字段本身又是结构化的，于是 `obj.view((self.__class__, obj.dtype))` 把它重新包成 `record`——这就是 `record` 身份能传进嵌套字段的根本原因（详见 4.3）。

[numpy/_core/records.py:239-249](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L239-L249) —— `record.__setattr__`：保留名禁止写、字段名走 `setfield`、已存在属性走 `void.__setattr__`：

```python
def __setattr__(self, attr, val):
    if attr in ('setfield', 'getfield', 'dtype'):
        raise AttributeError(f"Cannot set '{attr}' attribute")
    fielddict = nt.void.__getattribute__(self, 'dtype').fields
    res = fielddict.get(attr, None)
    if res:
        return self.setfield(val, *res[:2])
    elif getattr(self, attr, None):
        return nt.void.__setattr__(self, attr, val)
    else:
        raise AttributeError(f"'record' object has no attribute '{attr}'")
```

注意它比 `recarray.__setattr__` 简单得多：标量没有「实例字典需要撤销」的复杂情况，所以不需要 `recarray` 那套 `object.__setattr__` 后再 `object.__delattr__` 回滚的逻辑。

#### 4.2.4 代码实践

1. **目标**：用点号读写标量字段，并复现「写字段会回写到父数组」「写不存在的字段会报错」。
2. **步骤**：

   ```python
   import numpy as np
   r = np.rec.fromrecords([(1, 'a', 1.1), (2, 'b', 2.2)], names='id,name,val')
   print(r[0].id, r[0].name, r[0].val)   # 标量级点号读

   r[0].id = 99                            # 标量级点号写
   print(r.id)                             # 回看整列，确认第 0 行被改

   try:
       r[0].col5 = 1                       # 不存在的字段
   except AttributeError as e:
       print('caught:', e)
   ```
3. **预期现象**：第一行打印出 `1`、某个表示 `'a'` 的标量、`1.1`；写入 `r[0].id = 99` 后 `r.id` 变成 `[99, 2]`；写 `col5` 抛 `AttributeError`。
4. **预期结果**：标量 `r[0]` 通过**基本整数索引**取出的 record 标量**与父数组共享内存**，所以 `r[0].id = 99` 会修改父数组——这正是 `test_assignment1` 验证的行为（`a[0].col1 = 0` 后 `a.col1[0]` 变 `0`）。不存在的字段名写访问抛 `AttributeError`，与 `test_invalid_assignment` 一致。`'a'` 的精确打印形态**待本地验证**。

#### 4.2.5 小练习与答案

- **练习 1**：假如某个 record array 真有一个字段名叫 `dtype`，`r[0].dtype` 返回的是字段还是真正的 dtype 属性？
- **答案**：返回真正的 `dtype` 属性。因为 `__getattribute__` 第一行就把 `'dtype'` 列入保留名，直接 `return nt.void.__getattribute__(self, 'dtype')`，根本不会走到字段查找。要访问那个字段只能用 `r[0]['dtype']`（字典式，走 `__getitem__`）。
- **练习 2**：`record.__setattr__` 里 `elif getattr(self, attr, None):` 这一行为什么用 `getattr(..., None)` 而不是直接判断 `attr in fielddict`？
- **答案**：到这一分支说明 `attr` 不是字段名（字段名已在上一条 `if res:` 处理掉了）。这里的 `elif` 是为了允许给「标量上真实存在的、非字段的属性」赋值（少数内部属性），用 `getattr` 探测它是否真存在且「真值」。若既不是字段、也不是已存在属性，就落到 `else` 抛 `AttributeError`，禁止凭空造属性。

---

### 4.3 嵌套字段与索引：`record.__getitem__` 如何保住 `record` 身份

#### 4.3.1 概念说明

字段可以嵌套：一个字段的子 dtype 本身又是结构化的，例如 `dtype=[('bar', [('A','i4'),('B','i4')])]`。这时 `r[0].bar` 取出来的是一个 **嵌套的 record 标量**，而不是普通 `void`。这件事由两处协作完成：

- `record.__getattribute__`（4.2）：发现字段 `dt.names is not None` 时，`obj.view((self.__class__, obj.dtype))` 再包一层。
- `record.__getitem__`：当用索引/字段名 `rec['bar']` 或 `rec[k]` 访问时，走的是 `__getitem__`，它同样会把结构化的返回值再 `view` 成 `record`。

换句话说，无论你用点号 `rec.bar` 还是方括号 `rec['bar']`，只要目标是嵌套结构化字段，得到的都是 `record`（而不是 `void`），于是 `r[0].bar.A` 这种连续点号才一路畅通。

#### 4.3.2 核心流程

`record.__getitem__` 的伪代码：

```
obj = void.__getitem__(self, indx)        # 先让父类按 indx 取
if obj 是 void 且 有字段名(obj.dtype.names is not None):
    return obj.view((record, obj.dtype))  # 嵌套结构 → 再包成 record
else:
    return obj                             # 普通元素 → 原样返回
```

它与 `__getattribute__` 末尾的 `view` 逻辑是**对称的**——源码注释直接写明 "copy behavior of record.__getattribute__"。这样点号路径和方括号路径在「嵌套字段返回 record」这一点上行为完全一致。

#### 4.3.3 源码精读

[numpy/_core/records.py:251-259](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L251-L259) —— `record.__getitem__`：先让 `void` 父类取，再把结构化结果重新 `view` 成 `record`：

```python
def __getitem__(self, indx):
    obj = nt.void.__getitem__(self, indx)

    # copy behavior of record.__getattribute__,
    if isinstance(obj, nt.void) and obj.dtype.names is not None:
        return obj.view((self.__class__, obj.dtype))
    else:
        # return a single element
        return obj
```

关键判据是 `isinstance(obj, nt.void) and obj.dtype.names is not None`：只有当取出来的是「带字段的结构化 void」时才再 `view`。取出来的若是普通标量元素（如一个 int、或一个子数组里的元素），就直接返回。

对照测试 `test_recarray_returntypes`，可以确证这条路径确实产出 `record`：

[numpy/_core/tests/test_records.py:321-329](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_records.py#L321-L329) —— 嵌套字段 `bar`（本身是 `[('A',int),('B',int)]`）无论用点号还是方括号、无论在数组级还是标量级，都是 `record`：

```python
assert_equal(type(a[0].bar), np.record)     # 标量级点号 → record（走 __getattribute__）
assert_equal(type(a[0]['bar']), np.record)  # 标量级方括号 → record（走 __getitem__）
assert_equal(a[0].bar.A, 1)                 # 嵌套字段再点号，一路畅通
assert_equal(a[0]['qux']['D'], b'fgehi')    # 方括号链式访问同样成立
```

`test_nested_fields_are_records` 进一步用参数化测试覆盖了 0/1/2 个字段的嵌套情形：

[numpy/_core/tests/test_records.py:516-518](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_records.py#L516-L518) —— 外层标量是 `record`，嵌套字段 `inner` 也是 `record`：

```python
data0 = data[0]
assert isinstance(data0, np.record)
assert isinstance(data0['inner'], np.record)   # __getitem__ 把 inner 也包成 record
```

#### 4.3.4 代码实践

1. **目标**：构造一个带嵌套结构化字段的 record array，验证 `r[0].bar` 和 `r[0]['bar']` 都是 `record`，并能继续用 `.A` / `['A']` 访问内层字段。
2. **步骤**：

   ```python
   import numpy as np
   dt = [('foo', 'S4'),
         ('bar', [('A', 'i4'), ('B', 'i4')]),   # 嵌套结构化字段
         ('baz', 'i4')]
   a = np.rec.array([(b'abc', (1, 2), 9)], dtype=dt)

   bar_attr = a[0].bar          # 走 __getattribute__
   bar_item = a[0]['bar']       # 走 __getitem__
   print(type(bar_attr), type(bar_item))
   print(bar_attr.A, bar_attr['A'])
   print(a[0]['bar'].B)
   ```
3. **预期现象**：两个 `type(...)` 都是 `<class 'numpy.record'>`；`bar_attr.A` 与 `bar_attr['A']` 都为 `1`；`a[0]['bar'].B` 为 `2`。
4. **预期结果**：嵌套字段经 `__getattribute__` 与 `__getitem__` 两条路径都得到 `record`，因此可继续用点号/方括号访问内层字段——与 `test_recarray_returntypes` 的断言一致。

#### 4.3.5 小练习与答案

- **练习 1**：`record.__getitem__` 里为什么还要判断 `isinstance(obj, nt.void)`？只看 `obj.dtype.names is not None` 不够吗？
- **答案**：不够。`__getitem__` 的 `indx` 可能取到的是某个子数组的一个**普通元素**（例如对一个 `(float, 5)` 的子数组字段做 `rec[0][2]`），这时 `obj` 是一个 float 标量，根本没有 `.dtype.names`（或含义不同）。先 `isinstance(obj, nt.void)` 保证只在「取出来的是结构化 void 标量」时才尝试 `view`，避免对普通元素误操作。
- **练习 2**：如果没有 `record.__getitem__` 里的那次 `view`，`a[0]['bar']` 会是什么类型？
- **答案**：会是普通 `numpy.void`。那样 `a[0]['bar'].A` 就不能用点号访问（`void` 不支持点号取字段），只能 `a[0]['bar']['A']`。`__getitem__` 的 `view` 正是为了让方括号路径也享有点号能力，与 `__getattribute__` 保持对称。

---

### 4.4 美化打印：`pprint` 方法

#### 4.4.1 概念说明

`record` 还提供一个 `pprint()` 方法，把一条记录的所有字段按「字段名右对齐」的格式排成多行文本，方便人眼阅读。它的实现非常短：算出最长字段名宽度 `maxlen`，再对每个字段用 `f"{name:>{maxlen}}: {value}"` 拼一行。

需要特别留意两点：

1. **方法名叫 `pprint`，但它并不 `print`，而是 `return` 一个字符串**。调用者要自己 `print(r[0].pprint())` 才看得到输出。
2. **取值用的是 `getattr(self, name)`**，即走 `record.__getattribute__`（4.2）。因此嵌套字段也会以它的 `record`/标量形态被格式化，行为与点号访问完全一致。

#### 4.4.2 核心流程

```
names  = self.dtype.names
maxlen = max(len(name) for name in names)              # 最长字段名宽度
rows   = [ f"{name:>{maxlen}}: {getattr(self, name)}"   # 右对齐 + 取值
           for name in names ]
return "\n".join(rows)                                  # 拼成多行字符串返回
```

其中 `f"{name:>{maxlen}}"` 是 Python 格式化里「右对齐到 `maxlen` 宽」的写法：若 `name` 比 `maxlen` 短，左侧补空格。例如字段名为 `id/name/val`、`maxlen=4` 时，`id` 会被渲染成 `  id`（左侧 2 个空格）。

#### 4.4.3 源码精读

[numpy/_core/records.py:261-267](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L261-L267) —— `pprint`：按最长字段名右对齐，逐字段拼接，返回字符串：

```python
def pprint(self):
    """Pretty-print all fields."""
    # pretty-print all fields
    names = self.dtype.names
    maxlen = max(len(name) for name in names)
    rows = [f"{name:>{maxlen}}: {getattr(self, name)}" for name in names]
    return "\n".join(rows)
```

注意 `getattr(self, name)` 而非 `self[name]`：前者走 `__getattribute__`，能把嵌套字段也按 `record` 取出（与点号一致）；后者走 `__getitem__`，在本场景下结果类型相同，但语义上 `pprint` 选择与点号路径对齐。

#### 4.4.4 代码实践

1. **目标**：调用 `pprint()`，观察右对齐的多行输出，并验证它返回的是字符串而非直接打印。
2. **步骤**：

   ```python
   import numpy as np
   r = np.rec.fromrecords([(1, 'a', 1.1)], names='id,name,val')
   s = r[0].pprint()        # 注意：拿到的是字符串，没有自动打印
   print(type(s))
   print(s)                 # 这才真正输出
   ```
3. **预期现象**：`type(s)` 为 `<class 'str'>`；`print(s)` 输出三行，字段名右对齐到最长名 `name`（4 字符）的宽度。
4. **预期结果**：输出大致形如（字段名右侧对齐）：

   ```
     id: 1
   name: a
    val: 1.1
   ```

   其中 `id` 左侧补 2 空格、`val` 左侧补 1 空格、`name` 恰好 4 字符不补。各字段值（`'a'` 是否带引号、`1.1` 的精度）的精确形态**待本地验证**，取决于标量的 `str()` 表现。

#### 4.4.5 小练习与答案

- **练习 1**：执行 `r[0].pprint()`（不套 `print`）在交互式终端里似乎也能看到输出，为什么还说它「不打印」？
- **答案**：交互式 REPL 会自动 `repr()` 上一个表达式的返回值，所以你看到的是 REPL 对返回字符串的回显，而不是 `pprint` 自己打印的。在脚本（非交互）里单独写一行 `r[0].pprint()` 不会有任何输出，必须 `print(r[0].pprint())`。
- **练习 2**：把 `pprint` 里的 `getattr(self, name)` 换成 `self[name]`，输出会不同吗？
- **答案**：对普通标量字段，两者取到的值相同，输出一致；但对嵌套结构化字段，`getattr` 走 `__getattribute__` 返回 `record`、`self[name]` 走 `__getitem__` 也返回 `record`，最终的 `str()` 也相同。所以在这个实现里两者等价，作者选 `getattr` 只是与点号语义保持一致、可读性更好。

---

## 5. 综合实践

把本讲的四个模块串起来：构造一个**带嵌套字段**和**子数组字段**的 record array，取出一条标量，分别用点号、方括号、`pprint` 三种方式查看它，再通过标量回写父数组。

1. **任务**：

   ```python
   import numpy as np

   # 外层：id + 嵌套结构 point + 子数组字段 scores
   dt = [('id', 'i4'),
         ('point', [('x', 'f4'), ('y', 'f4')]),
         ('scores', 'f8', 3)]                      # 3 元素子数组字段

   r = np.rec.array([(1, (1.5, 2.5), (9.0, 8.0, 7.0)),
                     (2, (3.0, 4.0), (6.0, 5.0, 4.0))], dtype=dt)

   one = r[0]
   # (a) 标量身份与继承
   print(type(one), issubclass(type(one), np.void))

   # (b) 嵌套字段：点号 vs 方括号，都应是 record
   print(type(one.point), type(one['point']))
   print(one.point.x, one['point']['y'])

   # (c) 子数组字段：取一个元素
   print(one.scores, one.scores[1])

   # (d) 美化打印
   print(one.pprint())

   # (e) 通过标量回写父数组（共享内存）
   one.id = 100
   one.point.x = -1.0
   print(r.id, r.point.x)   # 第 0 行应被改写
   ```
2. **要回答的问题**：
   - `type(one)` 是什么？它与 `np.void` 是什么关系？（→ 4.1）
   - `one.point` 和 `one['point']` 类型相同吗？为什么 `one.point.x` 能用点号？（→ 4.3）
   - `one.scores` 是什么形态？`one.scores[1]` 走的是哪条路径？（→ 4.2/4.3：`scores` 是子数组字段，`getfield` 返回一个子数组视图，`[1]` 是对它再索引）
   - `one.pprint()` 的输出里，字段名是如何对齐的？（→ 4.4）
   - `one.id = 100` 之后 `r.id[0]` 变了吗？为什么？（→ 4.2：基本索引取出的标量与父数组共享内存）
3. **预期结果**：`one` 是 `numpy.record`（`void` 子类）；`one.point` 与 `one['point']` 均为 `record`，`one.point.x` 取到 `1.5`；`one.scores` 是一个 3 元素 float 子数组，`one.scores[1]` 为 `8.0`；`pprint` 输出按最长字段名右对齐；回写后 `r.id[0]` 为 `100`、`r.point.x[0]` 为 `-1.0`。子数组字段与精确浮点打印形态**待本地验证**。

## 6. 本讲小结

- `numpy.record` 是 `numpy.void`（`kind='V'`）的子类，是结构化数组「单行」的标量形态；字节布局与 `void` 完全相同，多了点号取字段与 `pprint` 能力。它手动设 `__module__='numpy'`，公开地址是 `numpy.record`。
- 标量级点号读 `rec.name` 由 `record.__getattribute__` 实现：三个保留名 `('setfield','getfield','dtype')` 永远返回真属性；其余名字先查对象属性、失败再查 `dtype.fields` 并 `getfield(*res[:2])`。
- 标量级点号写 `rec.name = val` 由 `record.__setattr__` 实现：保留名禁止写、字段名走 `setfield`、已存在属性走 `void.__setattr__`，否则 `AttributeError`。它比 `recarray.__setattr__` 简单，无需回滚实例字典。
- `record.__getitem__` 与 `__getattribute__` 末尾对称：当结果是带字段的结构化 `void` 时，`view((record, obj.dtype))` 把嵌套字段也包成 `record`，于是 `a[0].bar.A`、`a[0]['bar']['A']` 一路畅通（`test_recarray_returntypes` / `test_nested_fields_are_records` 为此背书）。
- `pprint()` 按最长字段名右对齐、逐字段 `getattr` 取值，**返回字符串**而非打印；取值走 `__getattribute__`，故嵌套字段也按 `record` 格式化。
- 基本整数索引取出的 `record` 标量与父数组**共享内存**，写字段会回写父数组（`test_assignment1`）；`object` 类型字段在 `__getattribute__` 里通过 `try: obj.dtype except AttributeError` 兜底（`test_objview_record`）。

## 7. 下一步学习建议

- 本讲把 `record` 标量讲透了。下一步建议进入 **u4 单元**，看数据是如何被「装进」record array 的：先读 [u4-l1 fromrecords](u4-l1-fromrecords.md)，理解 `fromrecords` 的行式自动类型探测路径——它会调用 `fromarrays`，后者最终用 `recarray(shape, descr)` 分配内存，而本讲的 `record` 标量正是这条流水线的「最终产品」。
- 想巩固「视图/拷贝与二元 dtype」的读者，可以先跳到 [u5-l1 视图、拷贝与 record/void dtype 转换](u5-l1-view-copy-dtype-conversion.md)，那里会把 `(record, descr)` 二元 dtype、`view(recarray)` 与 `__array_finalize__` 的协作讲得更系统，与本讲 4.3 的「再 `view` 成 record」形成闭环。
- 继续阅读源码时，建议把 `numpy/_core/records.py:196-267`（整个 `record` 类）通读一遍，并对照 `numpy/_core/tests/test_records.py` 的 `TestRecord` / `TestFromrecords` 两组测试，把每条断言与本文引用的某一行源码对应起来——这是检验你是否真正理解「标量级字段访问」的最快方式。
