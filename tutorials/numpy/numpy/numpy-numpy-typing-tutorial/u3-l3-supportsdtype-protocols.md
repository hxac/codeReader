# _SupportsDType 与 _HasDType 协议

## 1. 本讲目标

在 [u2-l2](u2-l2-dtypelike.md) 里，我们认识过公共别名 `DTypeLike` 的第四块拼图 `_SupportsDType[np.dtype]`——它代表「身上挂着一个 `dtype` 属性的对象」。但当时我们只说了「它由两个协议组成」，没有拆开看。

本讲就把这两个协议彻底拆开。读完本讲，你应该能够：

- 解释 `_HasDType` 与 `_HasNumPyDType` 各自刻画什么、为什么需要两个而不是一个。
- 理解为什么这两个协议用 `@property` 而不是普通属性注解来声明 `dtype`。
- 解释 `_SupportsDType` 作为两个协议的**联合别名**，如何让 dtype 元素类型 `DTypeT` 沿协议流动。
- 看懂公共 `DTypeLike` 与可参数化的私有 `_DTypeLike[ScalarT]` 是如何复用 `_SupportsDType` 的，并知道 `_HasNumPyDType` 为什么没有被 `numpy._typing` 再导出。

## 2. 前置知识

本讲建立在以下已经讲过的概念之上，不再重复细讲：

- **`Protocol` 与结构子类型**（来自 [u3-l1](u3-l1-supportsarray-protocols.md)）：`Protocol`（PEP 544）按「方法/属性的形状」匹配，不要求继承。一个类只要有协议要求的成员，就算「满足」该协议。
- **`@runtime_checkable` 的浅检查**（来自 u3-l1）：给协议加上 `@runtime_checkable` 后，运行时可以用 `isinstance(obj, 协议)` 判断，但只检查「成员名是否存在」，不看类型签名；静态检查器（mypy/pyright）才做完整签名匹配。
- **PEP 695 泛型协议语法**（来自 u3-l1/u3-l2）：`class _Proto[T: Bound](Protocol): ...` 把 `T` 限制为 `Bound` 的子类型，让类型参数沿协议流动。
- **`DTypeLike` 的外松内紧**（来自 u2-l2）：公共 `DTypeLike` 用宽松的 `str`，精确到 `Literal` 的窄化留给内部别名；`_SupportsDType` 是公共 `DTypeLike` 的构件之一。

本讲新增的关键问题是：**当协议要描述的是一个「属性」而不是「方法」时，该怎么写？** 答案是 `@property`，这正是本讲的第一个模块。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [numpy/_typing/_dtype_like.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_dtype_like.py) | **本讲核心**。定义 `_HasDType`、`_HasNumPyDType` 两个 `@runtime_checkable` 泛型协议，它们的联合 `_SupportsDType`，以及复用它的公共 `DTypeLike` 与私有 `_DTypeLike[ScalarT]`。 |
| [numpy/_typing/__init__.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/__init__.py) | 私有聚合中枢。注意它**只再导出 `_HasDType` 与 `_SupportsDType`**，没有再导出 `_HasNumPyDType`——后者是纯内部积木。 |
| [numpy/typing/tests/data/reveal/dtype.pyi](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/reveal/dtype.pyi) | 静态 reveal 夹具：用一个只挂 `__numpy_dtype__` 属性的类 `_D`，演示 `np.dtype(_D())` 的返回类型如何被精确推断为 `np.dtype[np.int8]`。 |
| [numpy/_core/tests/test_dtype.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_dtype.py) | 运行时测试 `TestFromDTypeProtocol`：验证「带 `dtype` / `__numpy_dtype__` 属性的对象」在运行时确实能被 `np.dtype(...)` 接受。 |
| [numpy/_core/src/multiarray/descriptor.c](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/multiarray/descriptor.c) | C 实现 `_try_convert_from_dtype_attr`：揭示运行时 `__numpy_dtype__` **优先于** `dtype` 的查找顺序。 |

## 4. 核心概念与源码讲解

### 4.1 property 协议：在 Protocol 里描述「属性」

#### 4.1.1 概念说明

到目前为止我们见过的协议（`_SupportsArray`、`_NestedSequence`）刻画的都是**方法**：`__array__`、`__iter__`、`__getitem__`。但 NumPy 的 dtype 协议要刻画的是一个**只读属性** `dtype`——你访问 `obj.dtype`，就拿到一个 `np.dtype` 实例。

PEP 544 允许在 `Protocol` 里声明属性，有两种写法：

```python
# 写法 A：普通属性注解
class P(Protocol):
    dtype: np.dtype

# 写法 B：用 @property
class P(Protocol):
    @property
    def dtype(self) -> np.dtype: ...
```

两者都表达「对象必须有一个可读的 `dtype`，读出来是 `np.dtype`」。区别在于：

- 写法 A 更简洁，但只能表达「有这么个属性」。
- 写法 B 用 `@property`，更显式地表达「这是一个**读取操作**」，并且能带上返回类型注解，让静态检查器做更精确的匹配。

NumPy 选的是**写法 B**，因为 `dtype` 在语义上就是一个「无参读取」（取属性 = 调用 property getter），用 `@property` 最贴切，也方便后面把返回类型换成带参数的 `DTypeT`。

#### 4.1.2 核心流程

一个具体类要满足「property 协议」，匹配过程是：

1. 协议声明了 `@property def dtype(self) -> DTypeT`。
2. 静态检查器去看具体类：它有没有一个 `dtype`？这个 `dtype` 是普通属性、还是 `@property`、还是带 `__get__` 的描述符？只要能「读出」一个类型兼容的值，就算匹配。
3. **不需要继承**协议——这就是结构子类型。`np.ndarray` 从不继承 `_HasDType`，但它有 `.dtype` 属性，所以在类型系统里它满足 `_HasDType`。
4. 运行时若加了 `@runtime_checkable`，`isinstance(obj, 协议)` 只检查「`obj` 身上有没有叫 `dtype` 的成员」，不检查返回类型。

#### 4.1.3 源码精读

`_dtype_like.py` 里第一个协议 `_HasDType` 就是用 `@property` 写的：

[_dtype_like.py:L36-L39](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_dtype_like.py#L36-L39) —— 用 `@property` 把「带 `dtype` 属性」翻译成协议；`[DTypeT: np.dtype]` 限制类型参数必须是 `np.dtype` 的子类型（如 `np.dtype[np.int8]`）。

```python
# A protocol for anything with the dtype attribute
@runtime_checkable
class _HasDType[DTypeT: np.dtype](Protocol):
    @property
    def dtype(self) -> DTypeT: ...
```

要点：

- `@runtime_checkable` 打开了 `isinstance` 开关。
- `[DTypeT: np.dtype]` 是 PEP 695 泛型协议语法，`DTypeT` 被约束为 `np.dtype` 的子类型。
- `@property def dtype(self) -> DTypeT`：读 `obj.dtype` 得到 `DTypeT`。这个 `DTypeT` 就是后面让 dtype 元素类型「流动」的钩子。

#### 4.1.4 代码实践

实践目标：体会「property 协议」按形状匹配，且与继承无关。

```python
# 示例代码
import numpy as np
from numpy._typing import _HasDType

class WithPlainAttr:
    dtype = np.dtype("int8")          # 普通类属性

class WithProperty:
    @property
    def dtype(self) -> np.dtype:      # 真正的 property
        return np.dtype("int8")

class Without:
    pass

print(isinstance(WithPlainAttr(), _HasDType))   # 预期 True
print(isinstance(WithProperty(), _HasDType))    # 预期 True
print(isinstance(Without(), _HasDType))         # 预期 False
```

操作步骤：把上面代码存成脚本运行（`_HasDType` 是 NumPy 私有别名，但运行时可直接从 `numpy._typing` 导入）。

需要观察的现象：前两个为 `True`，第三个为 `False`。这说明无论对象用普通属性还是 `@property` 实现 `dtype`，只要「形状对」就满足协议；而 `Without` 没有该成员，故为 `False`。

预期结果：`True / True / False`。若运行时实际输出与此不同，标注「待本地验证」并记下实际值。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `WithProperty.dtype` 改成 `def dtype(self)`（去掉 `@property`，变成普通方法），`isinstance(WithProperty(), _HasDType)` 运行时还是 `True` 吗？静态检查器会怎么看？

参考答案：运行时仍是 `True`——`@runtime_checkable` 的浅检查只看「有没有叫 `dtype` 的成员」，方法也算成员。但静态检查器会判它**不满足** `_HasDType`，因为协议要求的是「读属性得到 `DTypeT`」，而这里 `dtype` 变成了「调用方法得到 `DTypeT`」，形状不匹配。这正是「运行时为真、静态不符」的典型差异。

**练习 2**：为什么 NumPy 用 `@property def dtype(self) -> DTypeT` 而不是 `dtype: DTypeT` 这种属性注解？

参考答案：`@property` 更显式地表达「这是一个无参读取操作」，并能把返回类型注解成带参数的 `DTypeT`，便于静态检查器精确匹配；同时与运行时 `obj.dtype` 真正触发 property getter 的语义一致。属性注解写法虽然更短，但表达力弱一些。

---

### 4.2 _HasDType 与 _HasNumPyDType：两个「带 dtype」协议

#### 4.2.1 概念说明

`_dtype_like.py` 里其实定义了**两个**长得几乎一样的协议：

- `_HasDType`：要求对象有一个 `dtype` 属性。
- `_HasNumPyDType`：要求对象有一个 `__numpy_dtype__` 属性。

为什么要两个？因为「`dtype`」这个名字在 Python 生态里太通用——Pandas 的 `DataFrame`、各种第三方数组库都有自己的 `.dtype`，语义并不一定等于「NumPy dtype」。NumPy 为了**明确**地表达「这个属性就是给 NumPy 用的 dtype」，引入了带前后双下划线、名字专属的 `__numpy_dtype__` 协议，语义比复用 `.dtype` 更清晰、更不容易和第三方冲突。

所以策略是：**新代码优先用 `__numpy_dtype__`，老代码（已有 `.dtype`）靠 `dtype` 兜底**。两个协议并存，正是为了同时接纳这两类对象。

#### 4.2.2 核心流程

运行时 `np.dtype(obj)` 遇到一个「既不是字符串、也不是类、也不是 dtype 实例」的任意对象时，会走「属性协议」路径（C 函数 `_try_convert_from_dtype_attr`）：

1. 先找 `obj.__numpy_dtype__`。
2. 若不存在，再退而求其次找 `obj.dtype`。
3. 找到的属性值必须是合法的 dtype（实例或可转 dtype），否则报错。
4. 若两个属性都没有，返回 `NotImplemented`，交给下一条转换规则。

类型系统里，这两个运行时分支被建模成两个独立协议，再由 `_SupportsDType` 用联合把它们合并（见 4.3）。

#### 4.2.3 源码精读

第二个协议 `_HasNumPyDType` 与第一个几乎对称，只是属性名换成 `__numpy_dtype__`：

[_dtype_like.py:L42-L45](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_dtype_like.py#L42-L45) —— 与 `_HasDType` 对称，刻画带 `__numpy_dtype__` 属性的对象；注意 `self` 后的 `/` 把 `self` 标记为**仅按位置**。

```python
@runtime_checkable
class _HasNumPyDType[DTypeT: np.dtype](Protocol):
    @property
    def __numpy_dtype__(self, /) -> DTypeT: ...
```

运行时优先级在 C 实现里写得很清楚：先 `__numpy_dtype__`，缺失才查 `dtype`。

[descriptor.c:L87-L119](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/multiarray/descriptor.c#L87-L119) —— `_try_convert_from_dtype_attr` 先尝试 `__numpy_dtype__`，只有它不存在时才回退查 `dtype`（注释里明说「This should be removed in the future」，即未来 `dtype` 兜底可能被移除）。

```c
int res = PyObject_GetOptionalAttr(obj, npy_interned_str.numpy_dtype, &attr);
...
else if (res == 0) {
    /*  When "__numpy_dtype__" does not exist, also check "dtype". ... */
    used_dtype_attr = 1;
    int res = PyObject_GetOptionalAttr(obj, npy_interned_str.dtype, &attr);
    ...
}
```

运行时测试 `test_not_a_dtype` 用一个「同时挂了两个属性」的类印证了这个优先级：

[test_dtype.py:L1586-L1594](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/tests/test_dtype.py#L1586-L1594) —— 对象同时有 `__numpy_dtype__ = None` 和 `dtype = np.dtype("f8")`；因为先查 `__numpy_dtype__` 且它不是合法 dtype，运行时抛 `ValueError` 并在信息里点名 `__numpy_dtype__`，证明它优先。

```python
class ArrayLike:
    __numpy_dtype__ = None
    dtype = np.dtype("f8")

with pytest.raises(ValueError, match=".*__numpy_dtype__.*"):
    np.dtype(ArrayLike())
```

#### 4.2.4 代码实践

实践目标：亲手验证「`__numpy_dtype__` 优先于 `dtype`」的运行时行为。

```python
# 示例代码
import numpy as np

class Both:
    __numpy_dtype__ = np.dtype("int8")   # 新协议，优先
    dtype = np.dtype("float64")          # 老协议，被忽略

class OldOnly:
    dtype = np.dtype("float64")          # 只有老协议

print(np.dtype(Both()))      # 预期 int8（取自 __numpy_dtype__）
print(np.dtype(OldOnly()))   # 预期 float64（回退到 dtype）
```

操作步骤：运行脚本，对比两次 `np.dtype(...)` 的输出。

需要观察的现象：`Both()` 转出 `int8`（用了 `__numpy_dtype__`），`OldOnly()` 转出 `float64`（回退用了 `dtype`）。

预期结果：`dtype('int8')` 与 `dtype('float64')`。该行为与上一节的 `test_not_a_dtype` 一致，可放心验证。

#### 4.2.5 小练习与答案

**练习 1**：`_HasNumPyDType.__numpy_dtype__` 的签名里有个 `(self, /)`，这个 `/` 起什么作用？对结构匹配有影响吗？

参考答案：`/` 表示它左边的参数（这里就是 `self`）是**仅位置参数**（positional-only）。这是 NumPy 新协议的风格统一写法。对结构匹配几乎没有实际影响——因为 property getter 只会被「`obj.__numpy_dtype__`」这种位置访问触发，没人会用关键字传 `self`。它更多是「明确表达 self 是位置的」的信号。

**练习 2**：假设你给一个类同时定义了合法的 `__numpy_dtype__` 和合法的 `dtype`（且二者指向不同 dtype），运行时 `np.dtype(obj)` 会用哪一个？为什么类型系统要把它们拆成两个协议？

参考答案：运行时用 `__numpy_dtype__`（int8 那个），因为 C 端先查它。类型系统拆成两个协议，是为了在静态层面精确区分「这个对象靠哪个属性暴露 dtype」——两套属性的语义来源不同（`dtype` 可能是第三方库的、`__numpy_dtype__` 是 NumPy 专属），分别建模后再用联合合并，既覆盖现实又不丢失这一区分。

---

### 4.3 _SupportsDType：两个协议的联合，让 DTypeT 流动

#### 4.3.1 概念说明

`_SupportsDType` 不是一个 `class`，而是一个 **PEP 695 `type` 别名**——它把上面两个协议用 `|` 联合成一个：

> `_SupportsDType[DTypeT] = _HasDType[DTypeT] | _HasNumPyDType[DTypeT]`

含义是：「凡是带 `dtype` 属性、或带 `__numpy_dtype__` 属性的对象，都算 `_SupportsDType`」。它是 `_dtype_like.py` 给「带 dtype 属性的对象」起的统一名字，也是 `DTypeLike` 里复用的那块拼图。

关键在于 `DTypeT` 这个类型参数：两个协议都是 `[DTypeT: np.dtype]` 的泛型，联合后 `_SupportsDType` 也带上了 `DTypeT`。这意味着 dtype 的**元素类型**可以沿协议流动——你声明 `_SupportsDType[np.dtype[np.int8]]`，类型系统就知道「这个对象的 dtype 属性读出来是 `np.dtype[np.int8]`」，从而把 `np.int8` 这个精度信息一路传到下游。

#### 4.3.2 核心流程

类型信息流动的链条：

1. 定义一个类，让它的 `dtype`/`__numpy_dtype__` 返回 `np.dtype[np.int8]`。
2. 在类型系统里，它满足 `_HasDType[np.dtype[np.int8]]`（或 `_HasNumPyDType[...]`）。
3. 因此它也满足 `_SupportsDType[np.dtype[np.int8]]`。
4. 当它出现在 `_DTypeLike[ScalarT]` 或 `DTypeLike` 中时，类型参数把 `np.int8` 传出去，下游 `np.dtype(obj)` 的返回类型被精确推断为 `np.dtype[np.int8]`。

用集合语言描述联合：

\[
\mathrm{SupportsDType}[D] \;=\; \mathrm{HasDType}[D] \;\cup\; \mathrm{HasNumPyDType}[D]
\]

即对象只要落入**任一**协议，就落入 `_SupportsDType`。

#### 4.3.3 源码精读

联合别名只有一行，却是本讲的枢纽：

[_dtype_like.py:L48](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_dtype_like.py#L48) —— `_SupportsDType` 是两个协议的联合别名，本身也带类型参数 `DTypeT`，使元素类型可沿协议传递。

```python
type _SupportsDType[DTypeT: np.dtype] = _HasDType[DTypeT] | _HasNumPyDType[DTypeT]
```

静态 reveal 夹具展示了 `DTypeT` 流动的实际效果。注意夹具里的 `_D` 只声明了 `__numpy_dtype__`，且精确到 `np.dtype[np.int8]`：

[reveal/dtype.pyi:L163-L166](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/reveal/dtype.pyi#L163-L166) —— 类 `_D` 只挂 `__numpy_dtype__`（满足 `_HasNumPyDType`），`assert_type` 断言 `np.dtype(_D())` 的返回类型被精确推断为 `np.dtype[np.int8]`，正是 `DTypeT` 流动的结果。

```python
class _D:
    __numpy_dtype__: np.dtype[np.int8]

assert_type(np.dtype(_D()), np.dtype[np.int8])
```

这个夹具由 [u6-l1](u6-l1-static-typing-test-methodology.md) 会讲到的 `test_reveal` 用 mypy 校验：若类型检查器推不出 `np.dtype[np.int8]`，测试就会失败。

#### 4.3.4 代码实践

实践目标：用 `reveal_type`/`assert_type` 直观看到 `DTypeT` 如何从对象的属性注解流到 `np.dtype(...)` 的返回类型。

```python
# 示例代码（用 mypy 或 pyright 检查）
# 放到一个 .py 或 .pyi 里运行：mypy --strict demo.py
from typing import assert_type
import numpy as np

class MyInt8:
    @property
    def dtype(self) -> np.dtype[np.int8]:
        return np.dtype(np.int8)

obj = MyInt8()
reveal_type(np.dtype(obj))            # 期望: numpy.dtype[numpy.int8]
assert_type(np.dtype(obj), np.dtype[np.int8])
```

操作步骤：把代码存为 `demo.py`，运行 `mypy demo.py`（或 `pyright demo.py`）。

需要观察的现象：`reveal_type` 报告 `numpy.dtype[numpy.int8]`，`assert_type` 不报错。把 `MyInt8.dtype` 的返回类型改成 `np.dtype[np.int16]`，`assert_type` 应当立刻报不匹配。

预期结果：类型检查器能从 `dtype` 属性的注解 `np.dtype[np.int8]` 反推出 `np.dtype(obj)` 的返回类型。若本地无 mypy，可标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`_SupportsDType` 为什么用 `type ... = ... | ...`（联合别名）而不是再定义一个 `class _SupportsDType(Protocol)`？

参考答案：因为「带 `dtype`」和「带 `__numpy_dtype__`」是**两条独立的达标路径**（对象满足任一即可），这天然是「或」关系，用联合 `|` 表达最直接。若写成单个 Protocol 类，要么只能描述其中一个属性，要么得在一个类里把两个属性都标成可选——都不如「两个协议取并集」干净。`type` 别名也保留了 `DTypeT` 参数，让元素类型照样流动。

**练习 2**：reveal 夹具里的 `_D` 用的是**类属性** `__numpy_dtype__: np.dtype[np.int8]`（不是 `@property`）。它为什么也能匹配 `_HasNumPyDType` 协议？

参考答案：因为协议里用 `@property` 声明，表达的是「能读出一个值」；结构子类型允许具体类用**普通属性**、**property**、甚至**描述符**来实现，只要读出来的类型兼容即可。`_D` 用类属性注解 `__numpy_dtype__: np.dtype[np.int8]`，读 `_D().__numpy_dtype__` 得到 `np.dtype[np.int8]`，形状与类型都匹配，故满足协议（这与 4.1 里 `WithPlainAttr` 用普通属性匹配 `_HasDType` 是同一回事）。

---

### 4.4 DTypeLike 如何复用 _SupportsDType

#### 4.4.1 概念说明

`_SupportsDType` 不是孤立的——它是 `DTypeLike` 体系的「属性入口」。NumPy 在 `_dtype_like.py` 里同时维护**两层**：

- 公共 `DTypeLike`（不可参数化）：把 `_SupportsDType[np.dtype]` 作为五选一联合的一个分支。
- 私有 `_DTypeLike[ScalarT]`（可参数化）：把 `_SupportsDType[np.dtype[ScalarT]]` 嵌进去，从而能按标量类型 `ScalarT` 收窄。

这一层复用，使得「带 dtype 属性的对象」既能进入最宽松的公共 `DTypeLike`，也能进入按精度收窄的内部别名（如 `_DTypeLikeFloat`）——同一套协议服务两层别名。

还有一个容易被忽略的工程细节：`numpy._typing.__init__` 只再导出了 `_HasDType` 和 `_SupportsDType`，**没有再导出 `_HasNumPyDType`**。也就是说 `_HasNumPyDType` 是纯内部积木，外部用户只需要知道 `_SupportsDType` 这个统一名字。

#### 4.4.2 核心流程

两层别名的复用关系：

1. 公共层：`DTypeLike = type | str | np.dtype | _SupportsDType[np.dtype] | _VoidDTypeLike`。这里 `_SupportsDType[np.dtype]` 把 `DTypeT` 钉死成最宽的 `np.dtype`（不关心元素精度）。
2. 私有层：`_DTypeLike[ScalarT] = type[ScalarT] | np.dtype[ScalarT] | _SupportsDType[np.dtype[ScalarT]]`。这里把 `DTypeT` 钉成 `np.dtype[ScalarT]`，元素精度 `ScalarT` 透出给用户参数化。
3. 收窄别名（如 `_DTypeLikeFloat = type[float] | _DTypeLike[np.floating] | _FloatingCodes`）再复用 `_DTypeLike[...]`，`_SupportsDType` 通过它渗透到所有收窄别名里。
4. 再导出边界：`numpy/_typing/__init__.py` 只暴露 `_HasDType`、`_SupportsDType`，藏起 `_HasNumPyDType`。

#### 4.4.3 源码精读

公共 `DTypeLike` 把 `_SupportsDType[np.dtype]` 当作一个分支：

[_dtype_like.py:L101](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_dtype_like.py#L101) —— 公共 `DTypeLike` 的第四个分支就是 `_SupportsDType[np.dtype]`，把「带 dtype 属性的对象」纳入公共面（`DTypeT` 钉成最宽的 `np.dtype`）。

```python
type DTypeLike = type | str | np.dtype | _SupportsDType[np.dtype] | _VoidDTypeLike
```

私有可参数化的 `_DTypeLike[ScalarT]` 则把 `DTypeT` 钉成带精度的 `np.dtype[ScalarT]`：

[_dtype_like.py:L51-L54](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_dtype_like.py#L51-L54) —— `_DTypeLike[ScalarT]` 是「可按标量类型收窄」的子集，其第三分支复用 `_SupportsDType[np.dtype[ScalarT]]`，让带 dtype 属性的对象也能参与精度收窄。

```python
# A subset of `npt.DTypeLike` that can be parametrized w.r.t. `np.generic`
type _DTypeLike[ScalarT: np.generic] = (
    type[ScalarT] | np.dtype[ScalarT] | _SupportsDType[np.dtype[ScalarT]]
)
```

再导出边界（注意 `_HasNumPyDType` 缺席）：

[__init__.py:L89-L90](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/__init__.py#L89-L90) —— 私有聚合中枢只再导出 `_HasDType` 与 `_SupportsDType`；`_HasNumPyDType` 留作内部积木，不对外暴露。

```python
    _HasDType as _HasDType,
    _SupportsDType as _SupportsDType,
```

#### 4.4.4 代码实践

实践目标：实现一个「带 dtype property 的类」，让它在公共 `DTypeLike` 位置上被接受，并体会公共面与私有面的精度差异。

```python
# 示例代码
import numpy as np
import numpy.typing as npt

class MyDType:
    """带 dtype 属性的自定义类型，作为 DTypeLike 使用。"""
    @property
    def dtype(self) -> np.dtype:
        return np.dtype("float32")

def make_array(x: npt.DTypeLike) -> np.dtype:
    return np.dtype(x)

d = MyDType()
print(make_array(d))            # 预期 dtype('float32')，d 作为 DTypeLike 被接受
print(isinstance(d, np.dtype))  # 预期 False——d 本身不是 dtype 实例
```

操作步骤：

1. 把脚本存为 `use_dtypelike.py` 并运行。
2. 再用 mypy 检查：`mypy use_dtypelike.py`，确认 `make_array(d)` 不报类型错误（`MyDType` 满足 `_SupportsDType`，进而满足 `DTypeLike`）。
3. 进阶：把 `MyDType.dtype` 的返回注解改成更精确的 `np.dtype[np.float32]`，再把 `make_array` 的签名换成私有收窄别名（仅做阅读，私有别名不直接对外），思考精度信息如何在 `_DTypeLike[np.floating]` 里流动。

需要观察的现象：运行时打印 `dtype('float32')`；mypy 不报错，说明自定义对象确实被当作 `DTypeLike` 接受。

预期结果：`dtype('float32')` 与 `False`。mypy 通过。若本地未安装 mypy，类型检查部分标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：公共 `DTypeLike` 写的是 `_SupportsDType[np.dtype]`，私有 `_DTypeLike[ScalarT]` 写的是 `_SupportsDType[np.dtype[ScalarT]]`。这两处方括号里的内容为何不同？分别牺牲/保留了什么？

参考答案：公共面用 `_SupportsDType[np.dtype]`，把 `DTypeT` 钉成最宽的 `np.dtype`——它**牺牲了元素精度信息**（不关心是 int8 还是 float64），换得「接受一切带 dtype 属性的对象」的宽松；私有面用 `_SupportsDType[np.dtype[ScalarT]]`，把 `DTypeT` 钉成带精度的 `np.dtype[ScalarT]`，**保留了元素精度**，以便收窄别名（如 `_DTypeLikeFloat`）能按精度筛选。这正是「外松内紧」在协议层的体现。

**练习 2**：为什么 `numpy._typing.__init__` 再导出 `_HasDType`、`_SupportsDType`，却不导出 `_HasNumPyDType`？

参考答案：因为对外只需要一个统一名字 `_SupportsDType`（「带 dtype 属性的对象」），用户既不需要、也不应该关心内部是用 `dtype` 还是 `__numpy_dtype__` 达标的。`_HasNumPyDType` 是构造 `_SupportsDType` 联合的内部积木，把它藏起来能保持公共面简洁，也避免用户依赖一个可能随版本演进的实现细节（C 注释已暗示 `dtype` 兜底未来可能移除）。

## 5. 综合实践

把本讲的四个模块串起来，完成一个小任务：**亲手造一个「NumPy 友好」的自定义类型，并验证它在静态与运行时两端的协议达标情况。**

要求：

1. 定义一个类 `TemperatureColumn`，它内部存一组数据，并通过 `@property` 暴露 `dtype`（返回 `np.dtype[np.float64]`）。再定义一个兄弟类 `LegacyColumn`，用旧式 `__numpy_dtype__` 属性暴露 `np.dtype[np.float32]`。
2. 写一个函数 `def to_dtype(x: npt.DTypeLike) -> np.dtype: ...`，把两个类的实例都传进去，确认运行时都能得到正确 dtype。
3. 用 mypy 检查整个脚本，确认两个类都被当作 `DTypeLike` 接受；再用 `reveal_type` 观察 `np.dtype(TemperatureColumn())` 是否被推断出 `np.dtype[np.float64]`。
4. 写一段 `isinstance` 检查：分别用 `numpy._typing._HasDType` 验证 `TemperatureColumn`、用 `numpy._typing` 暴露的 `_SupportsDType` 验证两者，记录运行时 `True/False`，并解释为何 `_HasNumPyDType` 没法从 `numpy._typing` 直接导入（需要从 `numpy._typing._dtype_like` 导）。

参考思路：

- 第 2 步应分别得到 `dtype('float64')` 与 `dtype('float32')`（注意 `LegacyColumn` 靠 `__numpy_dtype__` 达标，且该属性优先）。
- 第 3 步的 `reveal_type` 结果取决于你给 property 标注的精度；若标了 `np.dtype[np.float64]`，则推断为 `numpy.dtype[numpy.float64]`。
- 第 4 步：`_HasNumPyDType` 没有被 `numpy/_typing/__init__.py` 再导出，故 `from numpy._typing import _HasNumPyDType` 会失败；要验证 `LegacyColumn` 的运行时协议达标，可改用 `from numpy._typing._dtype_like import _HasNumPyDType`。

## 6. 本讲小结

- `_HasDType` 与 `_HasNumPyDType` 是两个对称的 `@runtime_checkable` 泛型协议，分别刻画「带 `dtype` 属性」与「带 `__numpy_dtype__` 属性」的对象；后者是 NumPy 为避免与第三方 `.dtype` 冲突而引入的专属协议。
- 两个协议都用 `@property` 声明属性，表达「无参读取」语义；结构子类型允许具体类用普通属性、property 或描述符来满足，不要求继承。
- 运行时 `np.dtype(obj)` **先查 `__numpy_dtype__`、缺失才回退 `dtype`**（C 端 `_try_convert_from_dtype_attr`），类型系统用「两个协议取并集」镜像了这一优先级。
- `_SupportsDType[DTypeT]` 是这两个协议的 PEP 695 联合别名，借 `DTypeT` 让 dtype 元素类型沿协议流动——reveal 夹具 `_D` 正是靠它把 `np.dtype(_D())` 精确推断为 `np.dtype[np.int8]`。
- 公共 `DTypeLike`（`_SupportsDType[np.dtype]`，宽松）与私有 `_DTypeLike[ScalarT]`（`_SupportsDType[np.dtype[ScalarT]]`，带精度）复用同一套协议，体现「外松内紧」。
- 再导出边界：`numpy._typing.__init__` 只暴露 `_HasDType` 与 `_SupportsDType`，`_HasNumPyDType` 留作内部积木。

## 7. 下一步学习建议

- 本讲把「带 dtype 属性」这条路径讲透了，但 dtype 协议还有一条结构化分支 `_VoidDTypeLike`（嵌套元组、字段列表、`_DTypeDict`），可回到 [u2-l2](u2-l2-dtypelike.md) 复习其与 `_SupportsDType` 的并列关系。
- 想看这些协议如何被「批量验证」？下一讲进入单元 6，建议先读 [u6-l1 静态类型测试方法论](u6-l1-static-typing-test-methodology.md)：你会看到本讲引用的 `reveal/dtype.pyi` 是如何被 mypy + `test_reveal` 自动校验的，并学会为自己的函数补 reveal 夹具。
- 若想继续钻研协议机制，可对比 [u3-l1](u3-l1-supportsarray-protocols.md) 的 `_SupportsArray`（方法协议）与本讲的 `_HasDType`（属性协议），体会 PEP 544 对「方法」与「属性」两类形状的统一处理。
