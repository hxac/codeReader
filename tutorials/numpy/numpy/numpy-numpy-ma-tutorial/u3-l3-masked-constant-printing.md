# masked 单例与掩码数组打印

## 1. 本讲目标

本讲是专家层第三讲，聚焦两件看似琐碎、实则关系到全局正确性的事情：

1. `np.ma.masked` 这个对象为什么是「全局唯一、不可复制、不可修改」的单例，源码用什么手段把它锁死。
2. 当你 `print` 一个掩码数组时，被屏蔽的元素为什么显示成 `--`、超大数组为什么会出现 `...` 省略号，这条从 `_data` 到屏幕字符串的完整链路是怎么实现的。

学完后你应该能：

- 说清 `MaskedConstant` 的单例机制：`__new__` 只构造一次、`__setattr__` 拦截写入、`copy` 是空操作。
- 解释为什么 `masked` 参与运算时不会被复制成第二个单例（`__array_finalize__` 的「降级」与 `__array_wrap__` 的转发）。
- 用 `masked_print_option.set_display` 自定义屏蔽符号，并理解它如何同时作用于数组元素和 `masked` 单例自身。
- 跟读 `_insert_masked_print` → `_recursive_printoption` → `__str__`/`__repr__` 的打印管线，理解大数组的「四角提取」截断优化。

## 2. 前置知识

本讲建立在你已经掌握的几个概念之上（若不熟请先复习对应讲义）：

- **掩码数组三件套**（u1-l4）：`data`（含坏值的原始数据）、`mask`（同形布尔数组，`True` 表示屏蔽）、`fill_value`（屏蔽位对外填充值）。
- **`nomask` 单例**（u2-l1）：代表「没有任何屏蔽」的省内存特殊值，恒等于 `False`，全库用 `is nomask` 做身份判断。
- **ndarray 子类化三钩子**（u2-l2）：`__new__`（分配内存并构造）、`__array_finalize__`（切片/视图/ufunc 后的「兜底默认传播」）、`__array_wrap__`（ufunc 结束时设置结果掩码）。本讲里 `MaskedConstant` 正是靠重写这三个钩子来锁住身份。
- **Python 的 `__str__` 与 `__repr__`**：`str(x)`/`print(x)` 调用 `__str__`（面向人），`repr(x)` 调用 `__repr__`（面向开发者，尽量能反推对象）。
- **NumPy 打印基础**：普通 ndarray 靠 `np.array2string` 渲染，超大数组会自动插入 `...` 省略号只显示首尾。掩码数组复用了这套机制。

一个关键直觉：`np.ma.masked` 不是「屏蔽」这个动作，而是一个**具体的对象**——当你从掩码数组里取出一个「正好被屏蔽」的标量位置时，得到的就是它：

```python
>>> import numpy as np
>>> a = np.ma.array([10, 20, 30], mask=[False, True, False])
>>> a[1] is np.ma.masked
np.True_
```

正因为它是「取屏蔽标量」的统一返回值，它必须全局唯一，否则 `a[i] is np.ma.masked` 这种判断就不可靠了。

## 3. 本讲源码地图

本讲全部源码集中在 `numpy/ma/core.py`，少量测试在 `numpy/ma/tests/test_core.py`：

| 代码位置 | 作用 |
|---|---|
| `core.py` 的 `class MaskedConstant`（L6741-L6852） | `masked` 单例类：单例构造、不可变保护、运算降级 |
| `core.py` 的 `masked = MaskedConstant()`（L6855） | 真正暴露给用户的单例实例 |
| `core.py` 的 `class _MaskedPrintOption`（L2441-L2486） | 屏蔽符号控制器：存显示字符串 + 启停开关 |
| `core.py` 的 `masked_print_option = _MaskedPrintOption('--')`（L2490） | 全局控制器实例，默认显示 `--` |
| `core.py` 的 `_recursive_printoption`（L2493-L2507） | 把屏蔽位替换成符号的递归工具（支持结构化 dtype） |
| `core.py` 的 `_insert_masked_print`（L4047-L4076） | 打印前的预处理：四角提取 + 转 object dtype + 替换 |
| `core.py` 的 `MaskedArray.__str__`（L4078-L4079） | `str()` 入口 |
| `core.py` 的 `MaskedArray.__repr__`（L4081-L4177） | `repr()` 入口，组装 `masked_array(data=..., mask=..., fill_value=...)` |
| `core.py` 的 `_print_width` / `_print_width_1d`（L2877-L2880） | 四角提取的截断阈值 |
| `core.py` 的 `mvoid.__str__`（L6604-L6615） | 结构化标量记录的打印 |
| `tests/test_core.py` 的 `TestMaskedConstant`（L5577-L5630） | 单例与运算传播的契约测试 |
| `tests/test_core.py` 的 `test_str_repr`（L617-L685） | 打印格式的契约测试 |

## 4. 核心概念与源码讲解

### 4.1 MaskedConstant 单例与不可变保护

#### 4.1.1 概念说明

「单例（singleton）」是一种设计模式：某个类在整个进程里只允许存在一个实例，无论你「构造」多少次，拿到的都是同一个对象。`np.ma.masked` 就是这样一个单例。

为什么要做成单例？

- **身份判断要可靠**。库代码和用户代码都依赖 `value is np.ma.masked` 来判断「这个标量是不是屏蔽位」。如果存在两份拷贝，`is` 就会失效。
- **节省内存**。它代表「一个被屏蔽的标量」这个语义，本身没有可变状态，没必要重复创建。
- **避免误用**。如果用户能改它的值或掩码，全局所有引用它的数组都会被污染。

但 `MaskedConstant` 又是 `MaskedArray` 的子类——而 `MaskedArray` 是可变的 ndarray 子类。这就产生一个矛盾：**怎么让一个「本质可变」的子类实例变得不可变、且全局唯一？** 源码的答案是：用一组重写的方法把所有「可能产生第二个实例」或「可能修改它」的入口全部堵死。

#### 4.1.2 核心流程

单例的构造与保护可以用下面的流程概括：

```text
MaskedConstant()
   │
   ├─ __has_singleton()?  （类属性 __singleton 是否存在且类型正是 cls）
   │     ├─ 否（首次）：构造 data=np.array(0.)、mask=np.array(True)
   │     │                把两者 flags.writeable 置 False（只读）
   │     │                cls.__singleton = MaskedArray(data, mask).view(cls)
   │     └─ 是（后续）：直接返回 cls.__singleton
   │
   ├─ 任何写属性 → __setattr__ 抛 AttributeError（单例初始化完成后）
   ├─ copy()/__copy__/__deepcopy__ → 返回 self（空操作）
   ├─ 就地运算 __iadd__ 等 → 返回 self（就地运算无副作用）
   └─ pickle → __reduce__ 返回 (cls, ())，反序列化重新走 __new__，仍是同一个单例
```

注意 `__singleton` 是「双下划线开头」的名字，Python 会做名称改写（name mangling），它在类内部实际变成 `_MaskedConstant__singleton`，外部无法直接访问，从而避免被误改。

#### 4.1.3 源码精读

整个 `MaskedConstant` 类的定义见 [core.py:L6741-L6852](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L6741-L6852)。下面分段说明。

**单例构造 `__new__`**——[core.py:L6751-L6767](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L6751-L6767)：

```python
def __new__(cls):
    if not cls.__has_singleton():
        # We define the masked singleton as a float for higher precedence.
        data = np.array(0.)
        mask = np.array(True)

        # prevent any modifications
        data.flags.writeable = False
        mask.flags.writeable = False

        cls.__singleton = MaskedArray(data, mask=mask).view(cls)
    return cls.__singleton
```

要点：底层数据是浮点 `0.`，注释解释「用 float 是为了更高的运算优先级」（避免和整数运算时类型被意外降级）；`data` 与 `mask` 都被设成只读；最后用 `MaskedArray(...).view(cls)` 把一个普通掩码数组「视」成 `MaskedConstant` 类型存进类属性。第二次调用时 `__has_singleton()` 为真，直接返回缓存。

`__has_singleton` 还多了一个 `type(cls.__singleton) is cls` 的检查——[core.py:L6745-L6749](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L6745-L6749)，确保缓存的不是「父类的单例视图」而是确切的 `MaskedConstant` 实例。

**属性写拦截 `__setattr__`**——[core.py:L6842-L6852](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L6842-L6852)：

```python
def __setattr__(self, attr, value):
    if not self.__has_singleton():
        # allow the singleton to be initialized
        return super().__setattr__(attr, value)
    elif self is self.__singleton:
        raise AttributeError(
            f"attributes of {self!r} are not writeable")
    else:
        return super().__setattr__(attr, value)
```

三个分支：单例尚未初始化时允许写（否则 `__new__` 自己也建不起来）；自己是那个单例时一律拒绝；既不是初始化期、又不是单例的「可疑重复实例」时放行（这是给 `__array_finalize__` 降级时改 `__class__` 留的口子）。

**复制与就地运算全部空操作**——[core.py:L6817-L6840](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L6817-L6840)：

```python
def __iop__(self, other):
    return self
__iadd__ = __isub__ = __imul__ = ... = __iop__   # 所有就地运算统一返回 self

def copy(self, *args, **kwargs):
    return self          # 标量，无需复制
def __copy__(self):
    return self
def __deepcopy__(self, memo):
    return self
```

注释解释了 `copy` 为何是空操作：单例本质是一个标量，复制它没有意义，这与 `np.bool` 标量的处理方式一致（见 `test_copy`，gh-9328）。

最终，模块底部一行把单例暴露给用户——[core.py:L6855](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L6855)：

```python
masked = masked_singleton = MaskedConstant()
```

`np.ma.masked` 与 `np.ma.masked_singleton` 是同一个对象。

#### 4.1.4 代码实践

**实践目标**：亲手验证 `masked` 的「全局唯一」与「不可变」两条性质。

**操作步骤**：

```python
import numpy as np
from numpy.ma.core import MaskedConstant

# 1) 全局唯一：多次「构造」拿到同一个对象
print(MaskedConstant() is MaskedConstant())          # 预期 True
print(np.ma.masked is MaskedConstant())              # 预期 True

# 2) 不可变：试图改属性会被拦下
try:
    np.ma.masked.fill_value = 5
except AttributeError as e:
    print("被拦截:", e)                                # 预期抛出 AttributeError

# 3) copy 是空操作：复制后仍是同一个对象
print(np.ma.masked.copy() is np.ma.masked)           # 预期 True
```

**需要观察的现象**：第 1、3 步打印 `True`；第 2 步打印「被拦截: attributes of masked are not writeable」。

**预期结果**：以上三条断言全部成立（与 `tests/test_core.py` 的 `TestMaskedConstant.test_copy`、`test_repr` 行为一致）。

#### 4.1.5 小练习与答案

**练习 1**：为什么不直接写 `masked = MaskedArray(np.array(0.), mask=np.array(True))`，而要专门做一个 `MaskedConstant` 子类？

**参考答案**：那样得到的是一个普通可变 `MaskedArray`，无法阻止用户或库代码修改它的 `data`/`mask`/`fill_value`，也无法保证「全局唯一」。`MaskedConstant` 通过重写 `__new__`（单例缓存）、`__setattr__`（写拦截）、`copy`/就地运算（空操作）把可变性彻底封死，并把「类型」本身当作「我是那个特殊屏蔽标量」的标记，供 `__array_finalize__` 等钩子识别。

**练习 2**：`__has_singleton` 为什么要额外判断 `type(cls.__singleton) is cls`，仅判断 `cls.__singleton is not None` 不够吗？

**参考答案**：不够。因为单例是用 `MaskedArray(...).view(cls)` 构造的，视图操作在某些路径下可能产出一个「类型并非 `MaskedConstant`」的对象（例如父类单例的视图）。加上类型严格相等判断，能保证缓存里存的确实是一个货真价实的 `MaskedConstant`，否则会重新构造，避免拿到一个名不副实的实例。

---

### 4.2 masked 单例的运算传播

#### 4.2.1 概念说明

让 `masked` 参与运算（如 `np.ma.masked + 1` 或 `np.ma.masked + np.array([1,2,3])`）会带来一个危险：ufunc 内部会调用 `__array_finalize__` 和 `__array_wrap__`，而这两个钩子原本可能「复制」出一个类型仍是 `MaskedConstant` 的新数组——这就破坏了「全局唯一」。

源码的对策是：**一旦发现自己不是那个正牌单例，就立刻把自己「降级」成普通 `MaskedArray`**。这样无论运算怎么传播，世界上始终只有一个 `MaskedConstant`。

#### 4.2.2 核心流程

两条钩子，两种降级手法：

```text
__array_finalize__(self, obj)        # 视图/切片/ufunc 创建新数组时触发
   ├─ 单例还没初始化       → 走父类正常逻辑（构造期）
   ├─ self 就是正牌单例     → 什么都不做（pass）
   └─ self 是「疑似重复」   → self.__class__ = MaskedArray   ← 关键：原地降级
                              再调 MaskedArray.__array_finalize__

__array_wrap__(self, obj, context, return_scalar=False)   # ufunc 结束时触发
   └─ return self.view(MaskedArray).__array_wrap__(obj, context)
        # 先 view 成 MaskedArray，再交给父类的 __array_wrap__ 处理掩码
        # 避免递归回 MaskedConstant 自己的逻辑
```

由此产生两条可观察的行为（由测试钉死）：

- `np.add(np.ma.masked, 1)` 结果 `is np.ma.masked`——标量对标量，结果仍是单例本身。
- `np.add(np.ma.masked, np.array([1,2,3]))` 结果**不是**单例，也不是 `MaskedConstant`，而是一个形状为 `(3,)`、全部被屏蔽的普通 `MaskedArray`。

#### 4.2.3 源码精读

`__array_finalize__` 的降级逻辑见 [core.py:L6769-L6781](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L6769-L6781)：

```python
def __array_finalize__(self, obj):
    if not self.__has_singleton():
        # this handles the `.view` in __new__
        return super().__array_finalize__(obj)
    elif self is self.__singleton:
        # not clear how this can happen, play it safe
        pass
    else:
        # everywhere else, we want to downcast to MaskedArray, to prevent a
        # duplicate maskedconstant.
        self.__class__ = MaskedArray
        MaskedArray.__array_finalize__(self, obj)
```

注释直白：除了正牌单例，其余情况一律降级成 `MaskedArray`，以防出现「第二个 MaskedConstant」。

`__array_wrap__` 的转发见 [core.py:L6783-L6784](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L6783-L6784)：

```python
def __array_wrap__(self, obj, context=None, return_scalar=False):
    return self.view(MaskedArray).__array_wrap__(obj, context)
```

先 `self.view(MaskedArray)` 把自己当普通 `MaskedArray` 看，再调用父类版本去合并掩码（u2-l2 讲过父类 `__array_wrap__` 用 `mask_or` 合并各输入掩码、用 `ufunc_domain` 做域屏蔽）。这样就绕开了 `MaskedConstant` 自己被反复触发的递归风险。

这两条钩子的可观察行为，被 [test_core.py:L5577-L5596](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/tests/test_core.py#L5577-L5596) 的 `_do_add_test` 写成契约：

```python
def _do_add_test(self, add):
    assert_(add(np.ma.masked, 1) is np.ma.masked)        # 标量运算：仍是单例
    vector = np.array([1, 2, 3])
    result = add(np.ma.masked, vector)
    assert_(result is not np.ma.masked)                  # 数组运算：不是单例
    assert_(not isinstance(result, np.ma.core.MaskedConstant))  # 也不是该类型
    assert_equal(result.shape, vector.shape)             # 形状跟随数组
    assert_equal(np.ma.getmask(result), np.ones(vector.shape, dtype=bool))  # 全屏蔽
```

补充一点：构造路径也要防 duplicated 单例。`np.ma.array(np.ma.masked)` 不能造出一个新的 `MaskedConstant`，这一点由 [test_core.py:L5598-L5604](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/tests/test_core.py#L5598-L5604) 的 `test_ctor` 守护。

#### 4.2.4 代码实践

**实践目标**：复现「标量运算保单例、数组运算降级」两种行为。

**操作步骤**：

```python
import numpy as np
from numpy.ma.core import MaskedConstant

# A) 标量 + 标量：结果就是单例本身
r1 = np.add(np.ma.masked, 1)
print(r1 is np.ma.masked, repr(r1))              # 预期 True / masked

# B) 单例 + 数组：结果是全屏蔽的普通 MaskedArray
v = np.array([1, 2, 3])
r2 = np.ma.masked + v
print(r2 is np.ma.masked)                         # 预期 False
print(isinstance(r2, MaskedConstant))             # 预期 False（已降级）
print(r2.shape, np.ma.getmask(r2))                # 预期 (3,) / [ True  True  True]
```

**需要观察的现象**：A 打印 `True` 和 `masked`；B 的三行分别是 `False`、`False`、`(3,) [ True  True  True]`。

**预期结果**：与上述完全一致。关键体会——`r2` 虽然每个元素都被屏蔽，但它的类型已经是普通 `MaskedArray`，这正是 `__array_finalize__` 降级的结果。

#### 4.2.5 小练习与答案

**练习 1**：`__array_wrap__` 里为什么是 `self.view(MaskedArray).__array_wrap__(...)`，而不是直接 `MaskedArray.__array_wrap__(self, ...)`？

**参考答案**：`.view(MaskedArray)` 返回一个类型为 `MaskedArray` 的新视图，再调用其 `__array_wrap__` 时，该方法内部的 `self` 就是 `MaskedArray` 类型，逻辑（合并掩码、域屏蔽）能正确执行；同时避免了在 `MaskedConstant` 实例上直接操作可能引发的二次降级或递归。简言之：先「换个身份」再办事。

**练习 2**：如果删掉 `__array_finalize__` 里的 `self.__class__ = MaskedArray` 这一行，`np.ma.masked + np.array([1,2,3])` 会出什么问题？

**参考答案**：结果数组的类型会停留在 `MaskedConstant`，于是世界上多出一个「类型是 MaskedConstant、却不是正牌单例」的对象。这会破坏 `x is np.ma.masked` 的判断约定，也可能让后续运算、打印、pickle 行为偏离预期。降级这一行正是为了把这种「假单例」就地纠正回普通 `MaskedArray`。

---

### 4.3 _MaskedPrintOption / masked_print_option

#### 4.3.1 概念说明

被屏蔽的元素在屏幕上该显示成什么？NumPy 默认用两个短横 `--`。这个字符串并不是写死的常量，而是由一个叫 `masked_print_option` 的全局控制器管理。这个控制器还带一个「开关」：开启时把屏蔽位替换成符号，关闭时退回到用 `fill_value` 填充显示。

把显示字符串抽成全局可配置对象，好处是：

- 用户可以一行代码把 `--` 换成 `N/A`、`X` 或任何自定义符号。
- 同一个符号既用于数组里的屏蔽元素，也用于 `masked` 单例自身（见 4.1 节 `MaskedConstant.__str__` 直接读 `_display`），保持全局一致。

#### 4.3.2 核心流程

```text
masked_print_option = _MaskedPrintOption('--')     # 模块加载时创建，默认 '--'、开关默认开
        │
        ├─ display()          → 读取当前符号
        ├─ set_display(s)     → 改符号（影响全局所有打印）
        ├─ enabled()          → 查询开关状态
        └─ enable(shrink=1)   → 开/关替换功能

打印数组时：
   _insert_masked_print 先看 masked_print_option.enabled()
        ├─ 开：把屏蔽位替换成 masked_print_option（即 display 字符串）
        └─ 关：直接 filled(fill_value) 用填充值显示
```

#### 4.3.3 源码精读

控制器类完整定义见 [core.py:L2441-L2486](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L2441-L2486)。它本质上只有两个状态 `_display`（字符串）和 `_enabled`（布尔），配上读写它们的薄方法：

```python
class _MaskedPrintOption:
    def __init__(self, display):
        self._display = display
        self._enabled = True

    def display(self):        return self._display
    def set_display(self, s): self._display = s
    def enabled(self):        return self._enabled
    def enable(self, shrink=1): self._enabled = shrink

    def __str__(self):        return str(self._display)
    __repr__ = __str__
```

紧接着一行创建全局实例——[core.py:L2489-L2490](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L2489-L2490)，注释点明了它的用途：「if you single index into a masked location you get this object」（单点索引到屏蔽位时就显示它）：

```python
# if you single index into a masked location you get this object.
masked_print_option = _MaskedPrintOption('--')
```

注意 `MaskedConstant.__str__`（[core.py:L6786-L6787](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L6786-L6787)）直接读的是 `_display`：

```python
def __str__(self):
    return str(masked_print_option._display)
```

这就解释了一个有趣的联动效应：**改 `set_display` 会同时改变数组里屏蔽位的显示，以及 `masked` 单例本身的字符串形式**。例如 `masked_print_option.set_display('X')` 后，`str(np.ma.masked)` 也变成 `'X'`。

`masked_print_option` 是否生效，由 `_insert_masked_print` 的入口判断（见 4.4.3）。`enable(0)` 关闭后，打印退回 `filled(fill_value)`，屏蔽位会显示成填充值（如整数的 `999999`）而不是 `--`。

`test_mvoid_print`（[test_core.py:L1058-L1074](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/tests/test_core.py#L1058-L1074)）演示了改符号的标准写法——用 `try/finally` 保证测试结束后恢复原值（因为这是全局状态，所以该测试被标记 `thread_unsafe`）：

```python
ini_display = masked_print_option._display
masked_print_option.set_display("-X-")
try:
    assert_equal(str(mx[0]), "(1, -X-)")
    assert_equal(repr(mx[0]), "(1, -X-)")
finally:
    masked_print_option.set_display(ini_display)
```

#### 4.3.4 代码实践

**实践目标**：把屏蔽符号改成 `'X'`，观察它对数组元素和 `masked` 单例的双重影响。

**操作步骤**：

```python
import numpy as np

# 1) 改符号（务必保存原值，最后恢复，避免污染后续会话）
np.ma.masked_print_option.set_display('X')

a = np.ma.array([1, 2, 3], mask=[False, True, False])
print(str(a))                  # 预期 [1 X 3]
print(str(np.ma.masked))       # 预期 X（单例自身也变了）

# 2) 恢复
np.ma.masked_print_option.set_display('--')
print(str(a))                  # 预期 [1 -- 3]
```

**需要观察的现象**：第 1 步两行分别打印 `[1 X 3]` 与 `X`；恢复后变回 `[1 -- 3]`。

**预期结果**：与上述一致。这说明 `masked_print_option` 是全局共享的单一显示配置源。

#### 4.3.5 小练习与答案

**练习 1**：调用 `np.ma.masked_print_option.enable(0)` 后再打印一个含屏蔽位的数组，会看到什么？为什么？

**参考答案**：屏蔽位不再显示 `--`，而是显示填充值（如整数数组显示 `999999`）。因为 `enable(0)` 把 `_enabled` 置为 `False`，`_insert_masked_print` 走 `else` 分支，返回 `self.filled(self.fill_value)`，即用填充值替换屏蔽位后再打印。

**练习 2**：为什么 `test_mvoid_print` 会被打上 `@pytest.mark.thread_unsafe` 并用 `try/finally` 恢复 `_display`？

**参考答案**：`masked_print_option` 是进程级全局对象，`set_display` 改的是它的可变状态。如果不恢复，会污染同进程后续所有测试的打印断言；在并行（多线程）测试场景下，多个测试同时改这个全局状态会相互干扰，所以标记为线程不安全，并在 `finally` 里强制复原。

---

### 4.4 __str__/__repr__ 打印与截断

#### 4.4.1 概念说明

前面三节讲的是「屏蔽符号是什么」，本节回答「`print` 一个掩码数组时，从 `_data` 到屏幕字符串到底经过哪些步骤」。核心难点有两个：

1. **替换**：被屏蔽的位置要显示成符号字符串 `--`，而数组本身可能是整数、浮点等数值 dtype——一个数组不能同时存数值和字符串。NumPy 的做法是：打印前把数据**临时转成 object dtype**，再把屏蔽位替换成符号对象。
2. **截断**：超大数组全量转 object dtype 非常慢（要给每个元素装箱）。NumPy 的优化是：打印前先「提取四角」（只保留每条轴的首尾各一半），再对这部分做 object 转换，最后交给 `np.array2string` 渲染出带 `...` 的字符串。

`__str__` 与 `__repr__` 的分工和普通对象一致：`__str__` 给人看（紧凑的数据视图），`__repr__` 给开发者看（带 `masked_array(data=..., mask=..., fill_value=...)` 的结构化形式）。

#### 4.4.2 核心流程

`__str__` 链路：

```text
MaskedArray.__str__()
   └─ str(self._insert_masked_print())
         ├─ masked_print_option 关闭 → 返回 filled(fill_value)
         ├─ mask is nomask          → 返回 _data（原样）
         └─ 有真实 mask：
               1) 按每条轴做「四角提取」（见下）
               2) _replace_dtype_fields(dtype, "O") 把 dtype 递归换成 object
               3) data.astype(object_dtype)
               4) _recursive_printoption(res, mask, masked_print_option)
                    └─ np.copyto(res, 符号, where=mask)  # 屏蔽位填符号
```

四角提取（仅当某条轴长度 > print_width 时触发）：

```text
对每条 axis：
   若 data.shape[axis] > print_width:
       ind = print_width // 2
       把数组沿该轴 split 成 (前 ind, 中间, 后 ind) 三段
       只保留「前 ind」和「后 ind」拼接（丢弃中间）
       mask 同步做相同处理
```

阈值定义在类属性上——[core.py:L2877-L2880](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L2877-L2880)：

```python
# Maximum number of elements per axis used when printing an array. The
# 1d case is handled separately because we need more values in this case.
_print_width = 100        # 多维数组：每条轴最多保留 100 个
_print_width_1d = 1500    # 一维数组：最多保留 1500 个
```

也就是说，一维数组因为通常只占一行，允许保留更多元素（1500）；多维数组每条轴只保留 100。对一条长度为 \(N\) 的轴，保留首尾各 \(\lceil print\_width/2 \rceil\) 个，记 \(\text{ind} = \lfloor print\_width/2 \rfloor\)。

#### 4.4.3 源码精读

**`__str__` 入口**——[core.py:L4078-L4079](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L4078-L4079)，极其简短，全部交给 `_insert_masked_print`：

```python
def __str__(self):
    return str(self._insert_masked_print())
```

**`_insert_masked_print` 主体**——[core.py:L4047-L4076](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L4047-L4076)：

```python
def _insert_masked_print(self):
    if masked_print_option.enabled():
        mask = self._mask
        if mask is nomask:
            res = self._data                       # 无屏蔽：原样
        else:
            data = self._data
            # 大数组：为避免昂贵的 object 转换，先提取四角
            print_width = (self._print_width if self.ndim > 1
                           else self._print_width_1d)
            for axis in range(self.ndim):
                if data.shape[axis] > print_width:
                    ind = print_width // 2
                    arr = np.split(data, (ind, -ind), axis=axis)
                    data = np.concatenate((arr[0], arr[2]), axis=axis)
                    arr = np.split(mask, (ind, -ind), axis=axis)
                    mask = np.concatenate((arr[0], arr[2]), axis=axis)

            rdtype = _replace_dtype_fields(self.dtype, "O")
            res = data.astype(rdtype)
            _recursive_printoption(res, mask, masked_print_option)
    else:
        res = self.filled(self.fill_value)         # 关闭时退回填充值
    return res
```

三个细节值得注意：

- `np.split(data, (ind, -ind), axis=axis)` 把数组切成三段：`arr[0]` 是前 `ind` 个、`arr[1]` 是中间、`arr[2]` 是后 `ind` 个；`concatenate((arr[0], arr[2]))` 只留首尾、丢中间——这就是「四角」。
- `_replace_dtype_fields(self.dtype, "O")`（[core.py:L1363-L1374](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1363-L1374)）递归地把 dtype 的每个字段（含结构化与子数组）替换成 `object`，这样同一个数组里既能存数值又能存 `'--'` 字符串。
- `_recursive_printoption` 负责真正「按掩码填符号」——[core.py:L2493-L2507](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L2493-L2507)：

```python
def _recursive_printoption(result, mask, printopt):
    names = result.dtype.names
    if names is not None:
        for name in names:                 # 结构化：逐字段递归
            _recursive_printoption(result[name], mask[name], printopt)
    else:
        np.copyto(result, printopt, where=mask)   # 基本类型：屏蔽位 copyto 成符号
```

对基本类型数组，一行 `np.copyto(result, printopt, where=mask)` 就把所有 `mask` 为 `True` 的位置写成符号对象；对结构化数组则递归进每个字段。

**`__repr__` 入口**——[core.py:L4081-L4177](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L4081-L4177)。它做三件事：判断要不要显示 `dtype`、决定每个关键字（`data`/`mask`/`fill_value`/`dtype`）的缩进排版、用 `np.array2string` 渲染 `data` 与 `mask`。其中 `data` 的渲染同样调用 `_insert_masked_print()`——[core.py:L4143-L4152](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L4143-L4152)：

```python
reprs['data'] = np.array2string(
    self._insert_masked_print(),
    separator=", ",
    prefix=indents['data'] + 'data=',
    suffix=',')
reprs['mask'] = np.array2string(
    self._mask,
    separator=", ", prefix=indents['mask'] + 'mask=', suffix=',')
```

`dtype_needed` 的判定（[core.py:L4111-L4120](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L4111-L4120)）说明三种情况会额外显示 `dtype`：dtype 非默认隐含、整个数组全屏蔽、或数组为空。排版规则（[core.py:L4122-L4139](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L4122-L4139)）：单行（除最后一维外每维长度都是 1）时各关键字与首行对齐；多行时每个关键字独占一行、缩进 2 空格。

**结构化标量 `mvoid.__str__`**——[core.py:L6604-L6615](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L6604-L6615)，逻辑更简单：因为单条记录很小，不做四角提取，直接转 object 后调 `_recursive_printoption`：

```python
def __str__(self):
    m = self._mask
    if m is nomask:
        return str(self._data)
    rdtype = _replace_dtype_fields(self._data.dtype, "O")
    data_arr = super()._data
    res = data_arr.astype(rdtype)
    _recursive_printoption(res, self._mask, masked_print_option)
    return str(res)
__repr__ = __str__
```

最终输出格式由 `test_str_repr`（[test_core.py:L617-L685](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/tests/test_core.py#L617-L685)）钉成契约，例如一维含屏蔽位：

```python
a = array([0, 1, 2], mask=[False, True, False])
str(a)   # '[0 -- 2]'
repr(a)  # 'masked_array(data=[0, --, 2],\n'
         # '             mask=[False,  True, False],\n'
         # '       fill_value=999999)'
```

而一个 2000 元素、含屏蔽位的数组，`repr` 会显示首尾加 `...`：

```python
a = np.ma.arange(2000); a[1:50] = np.ma.masked
repr(a)
# masked_array(data=[0, --, --, ..., 1997, 1998, 1999],
#              mask=[False,  True,  True, ..., False, False, False],
#        fill_value=999999)
```

这里的 `...` 正是「四角提取 + `array2string` 省略」共同作用的结果。

#### 4.4.4 代码实践

**实践目标**：观察大数组的四角提取与省略号截断，对比 `__str__` 与 `__repr__` 的排版差异。

**操作步骤**：

```python
import numpy as np

# 1) 一维含屏蔽位：str / repr
a = np.ma.array([0, 1, 2], mask=[False, True, False])
print("str :", str(a))
print("repr:"); print(repr(a))

# 2) 大数组（2000 个）触发四角提取 + 省略号
big = np.ma.arange(2000)
big[1:50] = np.ma.masked
print(repr(big))          # 观察首尾 ..., 省略中间

# 3) 多行二维数组：每个关键字独占一行，dtype 显式
m2 = np.ma.array([[1, 2, 3], [4, 5, 6]], dtype=np.int8)
m2[1, 1] = np.ma.masked
print(repr(m2))
```

**需要观察的现象**：

- 第 1 步 `str` 为 `[0 -- 2]`，`repr` 为多行 `masked_array(data=[0, --, 2], ...)`。
- 第 2 步出现 `[0, --, --, ..., 1997, 1998, 1999]`，中间被省略。
- 第 3 步 `data`、`mask`、`fill_value`、`dtype` 各占一行、缩进 2 空格，屏蔽位显示 `--`。

**预期结果**：与 `test_str_repr` 的断言完全一致（见上引源码）。如果你把第 2 步换成 `print(str(big))`，同样会看到省略号——因为四角提取发生在 `_insert_masked_print` 里，`str` 与 `repr` 共用。

> 说明：以上输出在 NumPy 当前版本（HEAD `b21650c4f6`）下确定；若你本地版本不同，`repr` 的缩进/换行细节可能略有差异，但 `--` 符号与 `...` 截断行为稳定。

#### 4.4.5 小练习与答案

**练习 1**：为什么不直接在原数值数组上把屏蔽位改成 `'--'`，而要先 `astype(object)`？

**参考答案**：数值 dtype（如 int64、float64）的数组只能存数值，无法存字符串 `'--'`。必须先把 dtype 转成 `object`（每个元素是一个 Python 对象指针），才能让同一个数组里既有数值又有字符串符号。`_replace_dtype_fields(dtype, "O")` 负责递归地构造这个 object 版 dtype（结构化数组要把每个字段都换成 object）。

**练习 2**：把一个 5000 元素的一维掩码数组打印出来时，`_insert_masked_print` 实际转成 object dtype 的元素有多少个？写出推导。

**参考答案**：一维数组用 `_print_width_1d = 1500`。因为 `5000 > 1500`，触发四角提取：`ind = 1500 // 2 = 750`，保留前 750 和后 750，共 \(750 + 750 = 1500\) 个元素。所以真正做 object 转换的只有 1500 个元素，而非全部 5000 个——这正是该优化的目的。之后再由 `np.array2string` 在这 1500 个里进一步视觉截断显示首尾加 `...`。

---

## 5. 综合实践

把本讲四个模块串起来：自定义屏蔽符号、验证单例不可变、观察运算降级、并解释大数组打印。

```python
import numpy as np
from numpy.ma.core import MaskedConstant

# 任务：完整跑通「改符号 → 打印 → 运算降级 → 大数组截断」并逐项记录

# (1) 把屏蔽符号临时改成 "<M>"，确保最后恢复
saved = np.ma.masked_print_option.display()
np.ma.masked_print_option.set_display("<M>")
try:
    # (2) 构造含屏蔽位的数组，验证 str 与 repr 都用了新符号
    a = np.ma.array([10, 20, 30, 40], mask=[False, True, False, True])
    print("str(a)  =", str(a))           # 预期 [10 <M> 30 <M>]
    print("masked  =", str(np.ma.masked))  # 预期 <M>（单例也跟着变）

    # (3) 验证单例的运算传播：标量保单例、数组降级
    print("masked+1 is masked ?", (np.ma.masked + 1) is np.ma.masked)  # True
    r = np.ma.masked + np.array([1, 2])
    print("masked+[1,2] type   ", type(r).__name__)   # 预期 MaskedArray
    print("  is MaskedConstant?", isinstance(r, MaskedConstant))        # False

    # (4) 大数组截断：构造 3000 元素、屏蔽中段，观察四角 + 省略号
    big = np.ma.arange(3000)
    big[1000:2000] = np.ma.masked
    print(repr(big))   # 观察首尾出现 <M> 与 ...
finally:
    np.ma.masked_print_option.set_display(saved)   # 务必恢复全局状态
```

**检查清单**：

1. 第 (2) 步：`str(a)` 和 `str(np.ma.masked)` 是否都显示 `<M>`？（应都显示，因为二者共用 `masked_print_option`。）
2. 第 (3) 步：`masked+1 is masked` 是否为 `True`？数组运算结果类型是否为 `MaskedArray` 而非 `MaskedConstant`？
3. 第 (4) 步：`repr(big)` 是否在首尾看到 `<M>` 与 `...`？被屏蔽的中段是否被省略？
4. 最后：`finally` 是否把符号恢复成 `--`？（可再打印一次 `str(a)` 确认。）

> 提示：因 `masked_print_option` 是全局状态，务必用 `try/finally` 恢复，否则会影响同进程后续代码的打印——这也是 `test_mvoid_print` 标注 `thread_unsafe` 的原因。

## 6. 本讲小结

- `np.ma.masked` 是 `MaskedConstant` 的全局唯一实例：`__new__` 用类属性缓存、首次构造时把 `data`/`mask` 设为只读，再次「构造」直接返回同一个对象。
- 不可变性由多条重写共同保证：`__setattr__` 拦截写、`copy`/`__copy__`/`__deepcopy__` 与所有就地运算都返回 `self`、`__reduce__` 让 pickle 还原成同一个单例。
- 运算传播靠「降级」守护唯一性：`__array_finalize__` 把非正牌实例的 `__class__` 改回 `MaskedArray`，`__array_wrap__` 先 `view(MaskedArray)` 再转发；可观察结果是「标量运算保单例、数组运算降级为全屏蔽 `MaskedArray`」。
- 显示符号由全局 `masked_print_option`（`_MaskedPrintOption` 实例）管理，默认 `'--'`；`set_display` 同时改数组屏蔽位与 `masked` 单例的字符串形式，`enable(0)` 可退回填充值显示。
- 打印管线为 `__str__` → `_insert_masked_print` → `_recursive_printoption`：先做「四角提取」省去大数组的 object 转换开销，再转 object dtype 并用 `copyto(where=mask)` 填符号，最后交 `np.array2string` 渲染出带 `...` 的字符串。
- `__repr__` 复用 `_insert_masked_print` 渲染 `data`，并按单行/多行规则排版 `data`/`mask`/`fill_value`/`dtype`；结构化标量 `mvoid` 的打印走同一条 `_recursive_printoption` 但不做四角提取。

## 7. 下一步学习建议

- **下一讲（u3-l4）** 讲持久化：`__reduce__`/`__getstate__`/`__setstate__` 与 `_mareconstruct`。本讲提到的 `MaskedConstant.__reduce__` 返回 `(cls, ())` 正是单例可被安全 pickle 的关键，下一讲会把这套序列化机制展开。
- **延伸阅读**：对照 `numpy/_core/arrayprint.py`（`np.array2string`、`dtype_is_implied`、`dtype_short_repr`、`_get_legacy_print_mode`）理解 `__repr__` 借用的底层渲染与「legacy=1.13」旧版打印模板 `_legacy_print_templates`（[core.py:L2511-L2538](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L2511-L2538)）。
- **动手验证**：尝试写一个 `MaskedArray` 子类并重写 `__str__`，观察自定义打印与 `_insert_masked_print` 的协作；或阅读 `test_str_repr_legacy`（[test_core.py:L687](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/tests/test_core.py#L687)）理解新旧打印格式切换。
