# DTypeLike：一切可转为 dtype 的对象

## 1. 本讲目标

本讲拆解 NumPy 公共别名 `DTypeLike`——它能精确描述「所有可被 `np.dtype(...)` 接受的对象」。学完后你应该能够：

- 读懂 `_dtype_like.py` 中 `DTypeLike` 顶层别名的五大构件，并说出每一类对应哪种写法。
- 理解 `_SupportsDType` 协议如何把「带 `dtype` 属性」的对象翻译进类型系统。
- 理解 `_VoidDTypeLike` 与 `_DTypeDict` 如何描述结构化（void）dtype 的三种写法。
- 解释为什么 NumPy 类型系统**刻意排除** `{"field1": ..., "field2": ...}` 这种字段字典写法（运行时合法，但被类型系统拒绝）。
- 会用 mypy / pyright 验证 `DTypeLike` 相关的行为，并能读懂项目自带的 pass / reveal / fail 测试夹具。

## 2. 前置知识

本讲承接前几讲建立的认知：

- **静态类型检查 vs 运行时**（u1-l1）：类型检查器在运行前按注解推演，可能与运行时行为不一致；注解本身不影响运行时执行。
- **公共壳 + 私有实现**（u1-l2）：`numpy.typing` 只是一个极薄的公共壳，`DTypeLike` 真正定义在私有的 `numpy/_typing/_dtype_like.py`，再通过公共壳转发。
- **ArrayLike 的设计取舍**（u2-l1）：上一讲我们看到 `ArrayLike` 刻意避开 object 数组（拒绝生成器、裸对象、dict）。`DTypeLike` 沿用同样的哲学——「类型化 API 比运行时更严格」，主动拒绝一类被官方劝退的合法写法。

此外需要一点 Python 基础：

- `type` 类型对象：`int`、`float`、`bool` 既是「类」，也是可以传给 `np.dtype(...)` 的「type 对象」。
- PEP 695 `type` 语句：现代类型别名语法，如 `type DTypeLike = ...`。
- `TypedDict`：描述「键值都有固定类型」的字典；`Protocol`：描述「有某方法/属性」的鸭子类型。两者都会在用到时通俗解释。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [numpy/_typing/_dtype_like.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_dtype_like.py) | **本讲核心**。定义公共 `DTypeLike`，以及支撑它的 `_SupportsDType`、`_VoidDTypeLike`、`_DTypeDict` 等内部别名。 |
| [numpy/_typing/_char_codes.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_char_codes.py) | 用嵌套 `Literal` 枚举 dtype 字符串编码（`"float64"`、`"i8"`、`"<i2"` 等）。`_dtype_like.py` 导入它们来构建更精细的窄化别名。 |
| [numpy/typing/__init__.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py) | 公共壳，转发 `DTypeLike`；其文档串里有「字段字典被排除」的官方说明。 |
| [numpy/typing/tests/data/pass/dtype.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/pass/dtype.py) | 静态类型测试夹具：这些 `np.dtype(...)` 写法**必须**通过类型检查。 |
| [numpy/typing/tests/data/fail/dtype.pyi](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/fail/dtype.pyi) | 静态类型测试夹具：这些写法**必须**被类型检查器拒绝（靠 `# type: ignore` 标注期望错误）。 |
| [numpy/typing/tests/data/reveal/dtype.pyi](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/reveal/dtype.pyi) | 静态类型测试夹具：用 `assert_type` 锁定各种 `np.dtype(...)` 调用的精确返回类型。 |

> **关于永久链接**：本讲所有链接的 HEAD 均为 `9559a6b1ac93610711d8f1243f8c949fca4420bb`。`numpy/typing/` 下的文件直接使用该路径；`numpy/_typing/` 下的文件链接已写成完整正确路径（因为实现实际位于私有 `_typing` 包）。

---

## 4. 核心概念与源码讲解

### 4.1 DTypeLike：能转成 dtype 的所有对象

#### 4.1.1 概念说明

`np.dtype(...)` 是 NumPy 里最「宽容」的构造函数之一。它几乎吃下任何与数据类型相关的东西：

```python
np.dtype("float64")        # 字符串
np.dtype(int)              # Python 的 type 对象
np.dtype(np.float64)       # NumPy 的 scalar 类
np.dtype(np.dtype("f8"))   # 已有的 np.dtype 实例
np.dtype([("x", "i4"), ("y", "f8")])   # 字段列表（结构化 dtype）
np.dtype({"names": ["x"], "formats": ["i4"]})  # 字段字典
```

如果要把「函数参数能接受什么」用类型表达出来，就需要一个能涵盖上述**全部**形态的别名——这就是公共别名 `DTypeLike`。它和上一讲的 `ArrayLike` 是一对：`ArrayLike` 描述「能转成数组」，`DTypeLike` 描述「能转成 dtype」，两者是 NumPy 类型系统里最常用的两个公共别名。

#### 4.1.2 核心流程

`DTypeLike` 的定义只有一行，却是一个 **五选一** 的联合类型。我们先看它的全貌，再逐一拆解每个构件：

```
DTypeLike = type
          | str
          | np.dtype
          | _SupportsDType[np.dtype]
          | _VoidDTypeLike
```

对应到五种实际写法：

| 构件 | 接受的写法举例 | 含义 |
| --- | --- | --- |
| `type` | `np.dtype(int)`、`np.dtype(float)`、`np.dtype(bool)` | Python / NumPy 的「类对象」本身 |
| `str` | `np.dtype("float64")`、`np.dtype("i8")` | dtype 字符串编码 |
| `np.dtype` | `np.dtype(np.dtype("f8"))` | 已有的 dtype 实例 |
| `_SupportsDType[np.dtype]` | `np.dtype(obj)`，其中 `obj.dtype` 存在 | 带 `dtype` / `__numpy_dtype__` 属性的对象 |
| `_VoidDTypeLike` | `np.dtype([("x","i4")])`、字段字典 | 结构化（void）dtype |

注意一个**有意为之的宽松**：公共 `DTypeLike` 里的字符串构件写的是普通的 `str`，而不是某一组 `Literal` 字面量。也就是说，从类型层面**任何字符串**都算合法的 `DTypeLike`。这是因为字符串编码组合太多（大小、字节序、单位…），全部枚举会让公共别名既冗长又脆弱。NumPy 的做法是：公共 `DTypeLike` 用宽松的 `str`，而内部窄化别名（如 `_DTypeLikeFloat`）才精确到 `Literal` 编码——后者我们留到 4.1.3 末尾点一句，完整拆解见下一讲 u2-l4。

#### 4.1.3 源码精读

公共 `DTypeLike` 定义在私有实现文件里：

[numpy/_typing/_dtype_like.py:101](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_dtype_like.py#L101)

```python
# Anything that can be coerced into numpy.dtype.
type DTypeLike = type | str | np.dtype | _SupportsDType[np.dtype] | _VoidDTypeLike
```

这行注释「Anything that can be coerced into numpy.dtype」就是本讲的全部主题。注意它是 PEP 695 的 `type` 语句（不是 `TypeVar`，也不是 `TypedDict`），所以它是一个**不可参数化**的普通类型别名。

公共壳只是把它转发出去：

[numpy/typing/__init__.py:175](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L175)

```python
from numpy._typing import ArrayLike, DTypeLike, NBitBase, NDArray
```

紧跟在这行定义之后的，是 4.4 节要讲的「字段字典被排除」的注释（[L103-L108](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_dtype_like.py#L103-L108)），先记住它存在即可。

**关于 `str` 构件与 `_char_codes` 的关系**。`_dtype_like.py` 在文件开头导入了一组 `_XxxCodes` 别名（[L6-L19](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_dtype_like.py#L6-L19)），并用它们构建了一组**带精度信息的窄化别名**（[L79-L96](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_dtype_like.py#L79-L96)），例如：

```python
type _DTypeLikeFloat = type[float] | _DTypeLike[np.floating] | _FloatingCodes
```

这里的 `_FloatingCodes` 来自 [_char_codes.py:129](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_char_codes.py#L129)，是一组 `Literal` 字面量（`"float64"`、`"f8"`、`"<f8"` …）。也就是说：

- **公共 `DTypeLike`**：字符串构件 = 宽松 `str`，不限定具体编码。
- **内部 `_DTypeLikeFloat` 等**：字符串构件 = 精确的 `Literal` 编码，能告诉类型检查器「这是浮点」。

这种「外松内紧」的分层是 NumPy 类型系统的一贯风格。

#### 4.1.4 代码实践

> **实践目标**：亲手感受 `DTypeLike` 的五种构件，并对比「类型检查器视角」与「运行时视角」的差异。

把下面这段「示例代码」存为 `dt_demo.py`（注意：这不是项目原有代码，是为本讲编写的最小示例）：

```python
# 示例代码
import numpy as np
import numpy.typing as npt
from typing import reveal_type

def make_dtype(d: npt.DTypeLike) -> np.dtype:
    return np.dtype(d)

# 1. 字符串
reveal_type(make_dtype("float64"))
# 2. type 对象
reveal_type(make_dtype(int))
# 3. np.dtype 实例
reveal_type(make_dtype(np.dtype("f8")))
# 4. 字段列表（_VoidDTypeLike）
reveal_type(make_dtype([("x", "i4"), ("y", "f8")]))
# 5. 被排除的字段字典 —— 预期类型检查器报错
make_dtype({"field1": (float, 1), "field2": (int, 3)})
```

**操作步骤**：

1. 安装类型检查器之一：`pip install mypy`（或使用 pyright）。
2. 运行：`mypy dt_demo.py`（如用 pyright 则 `pyright dt_demo.py`）。

**需要观察的现象**：

- 前四行（字符串、type 对象、dtype 实例、字段列表）应**通过**检查；`reveal_type` 会打印出推断出的返回类型。
- 最后一行 `make_dtype({"field1": ...})` 应**报错**，错误大致是「没有匹配的重载 / 参数类型不符」（mypy 通常报 `call-overload`，pyright 报 `reportArgumentType`/`reportCallIssue`）。

**预期结果**：第 1～4 项通过、第 5 项被拒。这一「拒绝」正是 `DTypeLike` 设计的核心取舍（详见 4.4）。运行时（`python dt_demo.py`）这五种写法**全部**都能成功构造出 dtype，这恰好印证了「类型比运行时更严格」。

> 精确的错误码与 `reveal_type` 文本会随检查器版本变化，故具体措辞**待本地验证**；但「前 4 项通过、第 5 项被拒」这一结论是稳定的，且有项目自带夹具佐证（见 4.4.3）。

#### 4.1.5 小练习与答案

**练习 1**：`DTypeLike` 的联合类型里有哪五个构件？分别举一个真实写法。

参考答案：`type`（`int`）、`str`（`"float64"`）、`np.dtype`（`np.dtype("f8")`）、`_SupportsDType[np.dtype]`（带 `dtype` 属性的对象）、`_VoidDTypeLike`（`[("x","i4")]` 字段列表或字段字典）。

**练习 2**：为什么公共 `DTypeLike` 的字符串构件写成宽松的 `str`，而不是枚举所有合法编码？

参考答案：dtype 字符串编码组合极多（数据类型 × 大小 × 字节序前缀 × 时间单位…），全部枚举会让公共别名冗长、脆弱、难维护。NumPy 选择「公共宽松、内部 `_DTypeLikeXxx` 精确」的分层：只有需要携带精度信息时才用 `Literal` 编码。

---

### 4.2 _SupportsDType：用协议描述「带 dtype 属性」的对象

#### 4.2.1 概念说明

`DTypeLike` 的第四个构件 `_SupportsDType[np.dtype]` 解决一个有趣的问题：有些对象**本身不是 dtype、也不是类、也不是字符串**，但它「知道自己是什么 dtype」——它身上挂着一个 `dtype` 属性（或更新的 `__numpy_dtype__` 属性）。例如：

```python
class Image:
    @property
    def dtype(self):
        return np.dtype("uint8")
```

`np.dtype(Image())` 在运行时能识别这个 `dtype` 属性并据此构造。要把这种「鸭子类型」搬进静态类型系统，就需要一个 **Protocol**（协议）：只要某对象拥有形状正确的 `dtype` 属性，就算满足协议。这正是 `_SupportsDType`。

> **协议（Protocol）小科普**：Python 的 `Protocol` 是「结构化类型」——不看你是不是某个类的子类，只看你**有没有**指定的方法/属性。和 Java 的 interface 不同，协议是隐式满足的：你不需要 `class Image(SupportsDType)` 显式声明，只要属性对上就算。上一讲 `ArrayLike` 里的 `_SupportsArray`（认 `__array__`）也是同一套思路。

#### 4.2.2 核心流程

`_SupportsDType` 由两个更小的协议「或」起来：

```
_SupportsDType[DTypeT] = _HasDType[DTypeT]        # 有 .dtype 属性
                       | _HasNumPyDType[DTypeT]    # 有 .__numpy_dtype__ 属性
```

- `_HasDType`：要求对象有一个 **`dtype` 属性**（property），返回值类型由参数 `DTypeT` 决定。
- `_HasNumPyDType`：要求对象有一个 **`__numpy_dtype__` 属性**——这是 NumPy 新引入的协议，专门用于「把自定义类型暴露给 NumPy 的 dtype 系统」，语义比 `dtype` 更明确。

两者都带一个类型参数 `DTypeT`（上界为 `np.dtype`），这样 dtype 的元素类型信息可以沿协议传递（例如让类型检查器知道这个对象的 dtype 是 `np.dtype[np.uint8]`）。它们都用 `@runtime_checkable` 标记，意味着不仅能用于静态检查，还能用 `isinstance` 在运行时验证。

#### 4.2.3 源码精读

两个协议定义如下：

[numpy/_typing/_dtype_like.py:36-48](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_dtype_like.py#L36-L48)

```python
# A protocol for anything with the dtype attribute
@runtime_checkable
class _HasDType[DTypeT: np.dtype](Protocol):
    @property
    def dtype(self) -> DTypeT: ...

@runtime_checkable
class _HasNumPyDType[DTypeT: np.dtype](Protocol):
    @property
    def __numpy_dtype__(self, /) -> DTypeT: ...

type _SupportsDType[DTypeT: np.dtype] = _HasDType[DTypeT] | _HasNumPyDType[DTypeT]
```

注意语法 `class _HasDType[DTypeT: np.dtype](Protocol)`：这是 PEP 695/696 的**泛型协议**写法，`[DTypeT: np.dtype]` 表示「带一个类型参数 `DTypeT`，其上界是 `np.dtype`」。`@runtime_checkable` 让协议支持 `isinstance`。

这个 `_SupportsDType` 还被一个**可参数化的子集** `_DTypeLike[ScalarT]` 复用：

[numpy/_typing/_dtype_like.py:52-54](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_dtype_like.py#L52-L54)

```python
# A subset of `npt.DTypeLike` that can be parametrized w.r.t. `np.generic`
type _DTypeLike[ScalarT: np.generic] = (
    type[ScalarT] | np.dtype[ScalarT] | _SupportsDType[np.dtype[ScalarT]]
)
```

> **小心命名陷阱**：这里有**两个**名字几乎一样的别名——
> - 公共大写 `DTypeLike`（[L101](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_dtype_like.py#L101)）：不可参数化的顶层联合，对外暴露。
> - 内部带参数 `_DTypeLike[ScalarT]`（[L52](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_dtype_like.py#L52)）：能按标量类型参数化的子集，用于构建 `_DTypeLikeInt`、`_DTypeLikeFloat` 等窄化别名。
>
> 一字之差（前导下划线 + 是否带 `[ScalarT]`），含义完全不同。

项目自带的 reveal 夹具展示了 `__numpy_dtype__` 协议的真实用法：

[numpy/typing/tests/data/reveal/dtype.pyi:163-166](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/reveal/dtype.pyi#L163-L166)

```python
class _D:
    __numpy_dtype__: np.dtype[np.int8]

assert_type(np.dtype(_D()), np.dtype[np.int8])
```

类型检查器能从 `_D().__numpy_dtype__` 推断出 `np.dtype(_D())` 的返回类型是 `np.dtype[np.int8]`——这正是 `DTypeT` 参数让 dtype 信息「沿协议传递」的效果。

而 pass 夹具里 `np.dtype(Test())` 接受一个带 `dtype` 属性的普通对象：

[numpy/typing/tests/data/pass/dtype.py:34-38](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/pass/dtype.py#L34-L38)

```python
class Test:
    dtype = np.dtype(float)
np.dtype(Test())   # 通过：Test 满足 _HasDType 协议
```

#### 4.2.4 代码实践

> **实践目标**：实现一个自定义类，让它满足 `_SupportsDType` 协议，从而可作为 `DTypeLike` 使用。

```python
# 示例代码
import numpy as np
import numpy.typing  # 仅用于说明；_SupportsDType 是私有的，下面用运行时等效方式验证
from numpy._typing import _HasDType, _HasNumPyDType   # 私有导入，仅教学演示

class Pixel:
    """带 dtype 属性的自定义类型。"""
    @property
    def dtype(self) -> np.dtype:
        return np.dtype("uint8")

class NPixel:
    """带 __numpy_dtype__ 属性的自定义类型。"""
    @property
    def __numpy_dtype__(self) -> np.dtype:
        return np.dtype(np.int8)

p, n = Pixel(), NPixel()
print(isinstance(p, _HasDType))        # 预期 True
print(isinstance(n, _HasNumPyDType))   # 预期 True
print(np.dtype(p))                     # 运行时：dtype('uint8')
print(np.dtype(n))                     # 运行时：dtype('int8')
```

**操作步骤**：

1. 把上面代码存为 `proto_demo.py` 并运行 `python proto_demo.py`。
2. 再用 `mypy proto_demo.py`（或 pyright）检查。

**需要观察的现象与预期结果**：

- 运行时：`isinstance(p, _HasDType)` 为 `True`、`isinstance(n, _HasNumPyDType)` 为 `True`，证明 `@runtime_checkable` 协议可在运行时做鸭子类型判定；`np.dtype(p)` 返回 `dtype('uint8')`。
- 静态检查：若你把 `Pixel()` 传给一个标注为 `npt.DTypeLike` 的函数，类型检查器应**通过**（因为它满足 `_HasDType`，从而满足 `_SupportsDType[np.dtype]`）。

> 说明：`_HasDType` / `_HasNumPyDType` 位于私有包 `numpy._typing`，正式代码里不应直接 import 私有名；这里仅为教学演示其 `runtime_checkable` 行为。是否可 `isinstance` 校验私有协议，**待本地验证**（取决于 NumPy 版本是否导出该符号）。

#### 4.2.5 小练习与答案

**练习 1**：`_SupportsDType` 为什么要拆成 `_HasDType | _HasNumPyDType` 两个协议的联合，而不是只认 `dtype` 属性？

参考答案：因为 NumPy 新引入了 `__numpy_dtype__` 协议，专门用于把自定义 Python 类型暴露给 NumPy 的 dtype 系统，语义比复用 `dtype` 属性更明确、更不易与第三方库已有的 `dtype` 属性冲突。联合两个协议，既能兼容老的「带 `dtype` 属性」对象，也能接纳新的 `__numpy_dtype__` 对象。

**练习 2**：`class _HasDType[DTypeT: np.dtype](Protocol)` 里的 `[DTypeT: np.dtype]` 起什么作用？

参考答案：它声明了一个带**上界 `np.dtype`** 的类型参数 `DTypeT`，让协议可以把「这个对象的 dtype 具体是什么元素类型」沿类型链传递，例如让检查器推断 `np.dtype(_D())` 返回 `np.dtype[np.int8]` 而非笼统的 `np.dtype`。

---

### 4.3 _VoidDTypeLike：结构化（void）dtype 的类型描述

#### 4.3.1 概念说明

`DTypeLike` 的最后一个构件 `_VoidDTypeLike` 专门描述**结构化 dtype**（即元素类型为 `np.void`、由多个命名字段组成的 dtype，常用于表格/记录数据）。之所以单独拎出来，是因为结构化 dtype 的构造写法非常多、形态特殊，无法被 `type | str | np.dtype | _SupportsDType` 覆盖。

典型写法有三种：

```python
# 写法 A：字段列表，每个元素是 (名字, 类型[, 形状])
np.dtype([("name", "U16"), ("grades", "f8", (2,))])

# 写法 B：字段字典（规范形式，键是 names/formats 等关键字）
np.dtype({"names": ["a", "b"], "formats": [int, float]})

# 写法 C：嵌套元组
np.dtype(("U", 10))
```

`_VoidDTypeLike` 就是把这三种（以及它们的变体）汇总成一个联合类型。

#### 4.3.2 核心流程

`_VoidDTypeLike` 的定义结构（伪代码）：

```
_VoidDTypeLike = tuple[_DTypeLikeNested, _DTypeLikeNested]   # 嵌套元组
               | list[Any]                                    # 字段列表
               | _DTypeDict                                   # 字段字典（规范形式）
```

关键点：

- 嵌套部分用了一个占位别名 `_DTypeLikeNested`，它当前等于 `Any`。源码注释 `# TODO: wait for support for recursive types` 说明：结构化 dtype 可**递归嵌套**（一个字段的类型本身又可以是结构化 dtype），而 Python 类型系统对递归类型的支持尚不完善，故暂时用 `Any` 近似。这也是为什么代码里写「前两种元组形式是冗余的」——因为 `_DTypeLikeNested = Any` 已经覆盖了它们。
- `list[Any]` 故意写得很宽：NumPy 对字段列表里能放什么非常宽容（元组、字符串、嵌套…），故用 `Any` 兜底。
- `_DTypeDict` 是字段字典的精确规范（下一节 4.4 详解）。

#### 4.3.3 源码精读

先看递归占位别名：

[numpy/_typing/_dtype_like.py:21](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_dtype_like.py#L21)

```python
type _DTypeLikeNested = Any  # TODO: wait for support for recursive types
```

再看 `_VoidDTypeLike` 本体：

[numpy/_typing/_dtype_like.py:58-75](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_dtype_like.py#L58-L75)

```python
# Would create a dtype[np.void]
type _VoidDTypeLike = (
    # If a tuple, then it can be either:
    # - (flexible_dtype, itemsize)
    # - (fixed_dtype, shape)
    # - (base_dtype, new_dtype)
    # But because `_DTypeLikeNested = Any`, the first two cases are redundant
    tuple[_DTypeLikeNested, _DTypeLikeNested]

    # [(field_name, field_dtype, field_shape), ...]
    | list[Any]

    # {'names': ..., 'formats': ..., 'offsets': ..., 'titles': ..., 'itemsize': ...}
    | _DTypeDict
)
```

reveal 夹具锁定了两种写法的返回类型，都得到 `np.dtype[np.void]`：

[numpy/typing/tests/data/reveal/dtype.pyi:125-126](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/reveal/dtype.pyi#L125-L126)

```python
assert_type(np.dtype(("U", 10)), np.dtype[np.void])
assert_type(np.dtype({"formats": (int, "u8"), "names": ("n", "B")}), np.dtype[np.void])
```

pass 夹具展示了字段列表的多种合法形态：

[numpy/typing/tests/data/pass/dtype.py:18](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/pass/dtype.py#L18)

```python
np.dtype([("name", np.str_, 16), ("grades", np.float64, (2,)), ("age", "int32")])
```

#### 4.3.4 代码实践

> **实践目标**：用 `_VoidDTypeLike` 的三种写法构造结构化 dtype，并确认它们都被类型系统接受、运行时都产出 `np.void` dtype。

```python
# 示例代码
import numpy as np
import numpy.typing as npt

def void_dtype(d: npt.DTypeLike) -> np.dtype:
    return np.dtype(d)

# 写法 A：字段列表
d1 = void_dtype([("x", "i4"), ("y", "f8")])
# 写法 B：规范字段字典
d2 = void_dtype({"names": ["a", "b"], "formats": [int, float]})
# 写法 C：嵌套元组
d3 = void_dtype(("U", 10))

for d in (d1, d2, d3):
    print(d, "kind =", d.kind)   # kind 预期为 'V'（void）
```

**操作步骤**：

1. 运行 `python void_demo.py`。
2. 用 `mypy void_demo.py` 检查（应全部通过，无错误）。

**需要观察的现象与预期结果**：

- 运行时：三个 `d.kind` 均为 `'V'`（表示 void/结构化），印证它们都构造出 `np.void` dtype。
- 静态检查：三行均通过。这与 reveal 夹具（[reveal/dtype.pyi:125-126](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/reveal/dtype.pyi#L125-L126)）和 pass 夹具（[pass/dtype.py:18](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/pass/dtype.py#L18)）的断言一致。

> 字段列表/元组内部能放的元素非常自由（故 `list[Any]`），但只要你把整体传给 `npt.DTypeLike` 形参，类型检查器不会深究内部——这是「外层精确、内层宽松」的务实取舍。

#### 4.3.5 小练习与答案

**练习 1**：`_VoidDTypeLike` 的 `_DTypeLikeNested` 为什么被设成 `Any`？

参考答案：结构化 dtype 可递归嵌套（字段类型本身又可以是结构化 dtype），而 Python 类型系统对真正的递归类型支持尚不完善，因此暂用 `Any` 近似；源码用 `# TODO: wait for support for recursive types` 标注了这一点。

**练习 2**：`_VoidDTypeLike` 由哪三种形态构成？

参考答案：嵌套元组 `tuple[_DTypeLikeNested, _DTypeLikeNested]`、字段列表 `list[Any]`、以及规范字段字典 `_DTypeDict`。

---

### 4.4 _DTypeDict：字段字典的规范，以及 dict-of-fields 为何被排除

#### 4.4.1 概念说明

`_VoidDTypeLike` 的第三种形态 `_DTypeDict` 是一个 `TypedDict`，它精确描述了**规范字段字典**应有的键。但要理解它的价值，必须先看 NumPy 里**两种**看起来都像「字段字典」、命运却截然不同的写法：

```python
# 写法 B（规范，被类型系统接受）——键是 names/formats 等关键字
np.dtype({"names": ["a", "b"], "formats": [int, float]})

# 写法 A（dict-of-fields，被类型系统拒绝）——键是字段名，值是 (类型, 形状)
np.dtype({"field1": (float, 1), "field2": (int, 3)})
```

这两种写法在**运行时都能成功构造**出结构化 dtype，但 NumPy 官方**劝退**写法 A（参见 issue #16891）。因此类型系统只把写法 B 纳入 `_DTypeDict`，而对写法 A **故意不提供**任何匹配的 `TypedDict`，使类型检查器对它报错。这是 `DTypeLike` 设计里最经典的一处「比运行时更严格」。

> **TypedDict 小科普**：普通 `dict` 的键值类型是自由的；`TypedDict` 让你能规定「这个字典必须有 `names` 键且值是字符串序列、必须有 `formats` 键且值是类型序列……」。它常用于描述有固定结构的配置字典。

#### 4.4.2 核心流程

`_DTypeDict` 规定的字段（用表格更清晰）：

| 键 | 是否必需 | 值类型 | 含义 |
| --- | --- | --- | --- |
| `names` | **必需** | `Sequence[str]` | 字段名列表 |
| `formats` | **必需** | `Sequence[_DTypeLikeNested]` | 每个字段的类型 |
| `offsets` | 可选 (`NotRequired`) | `Sequence[int]` | 每个字段的字节偏移 |
| `titles` | 可选 (`NotRequired`) | `Sequence[Any]` | 字段的标题/别名 |
| `itemsize` | 可选 (`NotRequired`) | `int` | 整个 dtype 的字节大小 |
| `aligned` | 可选 (`NotRequired`) | `bool` | 是否内存对齐 |

关键设计：

- `names` 与 `formats` 是**必需键**——少了任一就不构成合法结构化 dtype。
- 其余四个用 `NotRequired[...]` 标记为可选。
- `titles` 的值类型是 `Sequence[Any]` 而非 `Sequence[str]`：注释说明「只有字符串元素能作为索引别名，但 `titles` 原则上可接受任意对象」，故取最宽。
- `_DTypeDict` 只描述写法 B 的结构；写法 A 的 `{"field1": (float,1), ...}` **没有任何对应的 TypedDict**，于是落入 `DTypeLike` 的「无人区」而被类型检查器拒绝。

#### 4.4.3 源码精读

`_DTypeDict` 的定义：

[numpy/_typing/_dtype_like.py:24-32](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_dtype_like.py#L24-L32)

```python
class _DTypeDict(TypedDict):
    names: Sequence[str]
    formats: Sequence[_DTypeLikeNested]
    # Only `str` elements are usable as indexing aliases,
    # but `titles` can in principle accept any object
    offsets: NotRequired[Sequence[int]]
    titles: NotRequired[Sequence[Any]]
    itemsize: NotRequired[int]
    aligned: NotRequired[bool]
```

而「写法 A 被排除」的取舍，被源码用一段**显式注释**记录在 `DTypeLike` 定义的正下方：

[numpy/_typing/_dtype_like.py:103-108](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_dtype_like.py#L103-L108)

```python
# NOTE: while it is possible to provide the dtype as a dict of
# dtype-like objects (e.g. `{'field1': ..., 'field2': ..., ...}`),
# this syntax is officially discouraged and
# therefore not included in the type-union defining `DTypeLike`.
#
# See https://github.com/numpy/numpy/issues/16891 for more details.
```

公共壳的文档串也把这条取舍写进了用户文档：

[numpy/typing/__init__.py:55-67](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L55-L67)

```python
# DTypeLike
# ~~~~~~~~~
# The `DTypeLike` type tries to avoid creation of dtype objects using
# dictionary of fields like below:
#     >>> x = np.dtype({"field1": (float, 1), "field2": (int, 3)})
# Although this is valid NumPy code, the type checker will complain about it,
# since its usage is discouraged.
```

项目用 fail 夹具**固化**了这一行为——下面这段**必须**触发类型错误（用 `# type: ignore[...]` 标注期望的错码）：

[numpy/typing/tests/data/fail/dtype.pyi:12-17](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/fail/dtype.pyi#L12-L17)

```python
np.dtype(  # type: ignore[call-overload]
    {
        "field1": (float, 1),
        "field2": (int, 3),
    }
)
```

相对地，写法 B 的规范字段字典在 pass 夹具里被当作合法用例（连同全部可选键一起）：

[numpy/typing/tests/data/pass/dtype.py:20-29](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/pass/dtype.py#L20-L29)

```python
np.dtype(
    {
        "names": ["a", "b"],
        "formats": [int, float],
        "itemsize": 9,
        "aligned": False,
        "titles": ["x", "y"],
        "offsets": [0, 1],
    }
)
```

> 这两份夹具是「类型系统对两种字典写法区别对待」的最硬证据：fail 夹具证明写法 A 必报错，pass 夹具证明写法 B（含 `itemsize`/`aligned`/`titles`/`offsets` 全部可选键）必通过。

#### 4.4.4 代码实践

> **实践目标**：对比「规范字段字典」与「被排除的 dict-of-fields」，亲眼看到类型检查器对两者反应不同，而运行时两者都成功。

```python
# 示例代码
import numpy as np
import numpy.typing as npt

def make_dtype(d: npt.DTypeLike) -> np.dtype:
    return np.dtype(d)

# 规范字段字典（写法 B）—— 应通过类型检查
ok = {"names": ["a", "b"], "formats": [int, float], "itemsize": 16, "aligned": True}
print(make_dtype(ok))

# dict-of-fields（写法 A）—— 运行时能跑，但应被类型检查器拒绝
bad = {"field1": (float, 1), "field2": (int, 3)}
print(make_dtype(bad))   # 注释掉这行后，mypy 才会干净通过
```

**操作步骤**：

1. 先运行 `python dict_demo.py`，确认两行都能打印出 dtype（运行时都成功）。
2. 再运行 `mypy dict_demo.py`（或 pyright）。

**需要观察的现象与预期结果**：

- 运行时：两个字典都成功构造出结构化 dtype（`kind == 'V'`）。
- 静态检查：`ok` 那行**通过**（匹配 `_DTypeDict`，属于 `_VoidDTypeLike`）；`bad` 那行**报错**（找不到匹配的联合分支，mypy 报 `call-overload`，pyright 报 `reportCallIssue`/`reportArgumentType`）。
- 把 `bad` 那行注释掉后再次 `mypy`，应干净通过。

**结论**：这一对比精确演示了 `DTypeLike` 的设计哲学——**不把所有运行时合法的写法都纳入类型**，而是用「有意的盲区」把被官方劝退的用法挡在类型安全之外。这与 fail 夹具 [fail/dtype.pyi:12-17](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/fail/dtype.pyi#L12-L17) 的断言一致。

> 具体错码措辞**待本地验证**（随检查器版本而变），但「写法 B 通过、写法 A 被拒」是稳定结论。

#### 4.4.5 小练习与答案

**练习 1**：`_DTypeDict` 里哪些键是必需的？为什么 `titles` 的值类型是 `Sequence[Any]` 而不是 `Sequence[str]`？

参考答案：`names` 与 `formats` 是必需键。`titles` 取 `Sequence[Any]` 是因为源码注释指出「只有字符串元素能作为索引别名，但 `titles` 原则上可接受任意对象」，故取最宽类型。

**练习 2**：为什么 `{"field1": (float, 1), "field2": (int, 3)}` 这种写法运行时合法、却被 `DTypeLike` 拒绝？

参考答案：这是 NumPy 官方劝退的 dict-of-fields 写法（issue #16891）。类型系统**故意不**为它定义任何 `TypedDict`，于是它无法匹配 `DTypeLike` 联合中的任何分支，从而被类型检查器拒绝。这是「类型化 API 比运行时更严格」的典型体现，并被 fail 夹具固化为断言。

---

## 5. 综合实践

把本讲四个最小模块串起来，完成一个**带类型注解的小工具 + 对应的静态测试夹具**。

**任务**：实现一个函数 `coerce_dtype`，它接受任意 `npt.DTypeLike`，返回 `np.dtype`；要求：

1. 内部对几类输入分别走不同分支（字符串、type 对象、带 `dtype` 属性的对象、结构化 dtype）。
2. 用一个带 `dtype` 属性的自定义类，验证 `_SupportsDType` 通路。
3. 仿照项目 `data/reveal` 风格，写一个 `.pyi`，用 `assert_type` 锁定若干调用的返回类型；并仿照 `data/fail` 风格，写一行 dict-of-fields 调用并标注 `# type: ignore[call-overload]`，固化「它应被拒绝」。

参考实现（「示例代码」）：

```python
# coerce_dtype.py —— 示例代码
import numpy as np
import numpy.typing as npt

class MyType:
    """满足 _HasDType 协议的自定义类型。"""
    @property
    def dtype(self) -> np.dtype:
        return np.dtype(np.int32)

def coerce_dtype(d: npt.DTypeLike) -> np.dtype:
    # 不管是 str / type / np.dtype / 带 dtype 的对象 / 结构化 dtype，
    # DTypeLike 已统一描述，这里直接交给 np.dtype。
    return np.dtype(d)

# 运行时自测
if __name__ == "__main__":
    print(coerce_dtype("float64"))                       # str 构件
    print(coerce_dtype(int))                             # type 构件
    print(coerce_dtype(np.dtype("f8")))                  # np.dtype 构件
    print(coerce_dtype(MyType()))                        # _SupportsDType 构件
    print(coerce_dtype([("x", "i4"), ("y", "f8")]))      # _VoidDTypeLike（列表）
    print(coerce_dtype({"names": ["a"], "formats": [int]}))  # _VoidDTypeLike（_DTypeDict）
```

配套 reveal 风格夹具（示例代码，文件名按惯例用 `.pyi`）：

```python
# reveal_coerce.pyi —— 示例代码
from typing import assert_type
import numpy as np
from coerce_dtype import coerce_dtype

assert_type(coerce_dtype("float64"), np.dtype)
assert_type(coerce_dtype([("x", "i4")]), np.dtype)
# dict-of-fields：应被拒绝，故标注期望错误
coerce_dtype({"field1": (float, 1)})  # type: ignore[call-overload]
```

**验收**：

- `python coerce_dtype.py`：6 行全部打印出 dtype，结构化写法的 `kind == 'V'`。
- `mypy coerce_dtype.py`：无错误。
- `mypy reveal_coerce.pyi`：只有那一处 `type: ignore` 被命中（即写法 A 确实被拒，且 `assert_type` 全部成立）。

> 若你对结构化字典写法做更细的运行时观察（如 `itemsize`/`offsets` 是否生效），可在 `coerce_dtype.py` 里多加几组 `{"names":..., "formats":..., "offsets":..., "aligned":...}` 并打印 `d.itemsize`、`d.fields`。具体数值**待本地验证**。

## 6. 本讲小结

- `DTypeLike` 是一个**五选一**的联合类型：`type | str | np.dtype | _SupportsDType[np.dtype] | _VoidDTypeLike`，覆盖所有能被 `np.dtype(...)` 接受的对象。
- 字符串构件在公共 `DTypeLike` 里是**宽松的 `str`**；精确到 `Literal` 编码的是内部窄化别名（`_DTypeLikeFloat` 等，下一讲 u2-l4 详解）。
- `_SupportsDType = _HasDType | _HasNumPyDType`，用两个 `@runtime_checkable` 泛型协议描述「带 `dtype` / `__numpy_dtype__` 属性」的对象，并能把 dtype 元素类型沿协议传递。
- `_VoidDTypeLike` 描述结构化（void）dtype 的三种写法：嵌套元组、字段列表、规范字段字典；递归部分因类型系统限制暂用 `Any` 近似。
- `_DTypeDict` 是一个 `TypedDict`：`names`/`formats` 必需，`offsets`/`titles`/`itemsize`/`aligned` 用 `NotRequired` 标可选。
- dict-of-fields 写法 `{"field1": ..., "field2": ...}` 运行时合法却被类型系统**刻意排除**（官方劝退），并由 fail 夹具固化为断言——这是「类型比运行时更严格」的范例。
- **命名陷阱**：公共大写 `DTypeLike`（不可参数化的顶层联合）与内部带参数 `_DTypeLike[ScalarT]`（可按标量参数化的子集）仅一字之差，含义不同。

## 7. 下一步学习建议

- **紧接 u2-l3**：学习 `NDArray` 与 `ndarray` 的双参数泛型（形状 + 元素类型），那里会再次遇到「dtype 元素类型如何作为类型参数流动」，与本讲的 `_DTypeLike[ScalarT]` 呼应。
- **然后 u2-l4**：深入 `_char_codes.py`，把本讲只点到为止的 `Literal` 字符串编码体系彻底拆解，理解「公共 `str` vs 内部 `Literal` 编码」的完整分层。
- **进阶 u3-l3**：本讲的 `_SupportsDType` / `_HasDType` 属于协议家族；u3 单元会系统讲解 NumPy 类型系统里的各类 Protocol（`_SupportsArray`、`_NestedSequence`、`_HasDType`），建议把协议视角打通。
- **源码延伸阅读**：直接打开 [numpy/_typing/_dtype_like.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_dtype_like.py) 通读 109 行全文，并对照 [pass/dtype.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/pass/dtype.py)、[reveal/dtype.pyi](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/reveal/dtype.pyi)、[fail/dtype.pyi](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/fail/dtype.pyi) 三份夹具，把「类型如何被测试」也一并看懂。
