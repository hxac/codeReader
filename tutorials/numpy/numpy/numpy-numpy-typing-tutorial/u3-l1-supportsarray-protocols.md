# _SupportsArray 与 \_\_array\_\_ / \_\_array_function\_\_ 协议

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 `Protocol` 与普通类的区别，并解释 `@runtime_checkable` 让协议「既能被类型检查器识别、又能被 `isinstance` 使用」做了什么——以及它的运行时检查有多「浅」。
- 读懂 `_SupportsArray[DTypeT: np.dtype](Protocol)` 这种 **PEP 695 泛型协议**语法，理解它如何把运行时的 `__array__` 鸭子协议「翻译」成类型，并让 dtype 元素类型沿协议流动。
- 读懂 `_SupportsArrayFunc(Protocol)` 如何刻画 `__array_function__`（NEP 18）协议，并知道它被用在哪里（`np.asarray` 的 `like=` 参数）。
- 解释这两个协议在 `ArrayLike` 的鸭子类型定义中扮演的角色：为什么 NumPy 用「结构子类型」而不是继承来描述「能变成数组的对象」。

## 2. 前置知识

本讲承接 [u2-l1（ArrayLike）](u2-l1-arraylike.md)。你已经知道：

- `ArrayLike = Buffer | _DualArrayLike[np.dtype, complex | bytes | str]`，其中 `_DualArrayLike` 把 `_SupportsArray`、`_NestedSequence`、若干内置标量用 `|` 拼到一起；
- `_SupportsArray` 是「带 `__array__` 方法的对象」的类型版，`_NestedSequence` 是「任意深度嵌套序列」的类型版，二者都是**协议（Protocol）**。

本讲需要补充四个基础概念：

1. **结构子类型（structural subtyping）**
   普通类型看「你是不是这个类、或它子类的实例」（nominal，按名字/继承）。`Protocol` 看「你长不长这个样子」——只要一个对象拥有协议规定的方法/属性，类型检查器就认为它**符合**该协议，**无需继承**。这就是「鸭子类型」的类型版本。

2. **`Protocol`（PEP 544）**
   一个继承自 `typing.Protocol` 的类就是协议。协议里只写「我要求你有这些方法签名」，本身通常不实现它们（用 `...` 占位）。

3. **`@runtime_checkable`（PEP 544）**
   默认情况下协议**不能**用于 `isinstance()`（结构子类型在运行时检查成本太高）。给协议加上 `@runtime_checkable` 装饰器后，它就**允许** `isinstance()` / `issubclass()` 了。但运行时检查是**浅层**的——详见 4.1.2。

4. **两个 NumPy 运行时鸭子协议**
   - `__array__(self, dtype=None)`：返回一个 `np.ndarray`。凡实现它的对象都能被 `np.asarray()` 转成数组（PEP 无关，是 NumPy 自有约定）。
   - `__array_function__(self, func, types, args, kwargs)`（NEP 18）：让第三方数组类型（如 CuPy、Dask、PyTorch 的 Tensor）能「接管」`np.add(x, y)` 这类顶层 NumPy 函数调用。

> 提示：本讲的主角 `_SupportsArray` / `_SupportsArrayFunc` 定义在私有文件 `_array_like.py`，由 `numpy._typing` 聚合后，被 `ArrayLike` 和各 `.pyi` 桩文件消费。它们不是公共 API，但理解它们是理解 `ArrayLike` 的关键。

## 3. 本讲源码地图

本讲只盯住「两个 runtime_checkable 协议」这一件事，涉及的真实文件如下：

| 文件 | 角色 | 说明 |
| --- | --- | --- |
| `numpy/_typing/_array_like.py` | **主战场** | 定义 `_SupportsArray`（`__array__`）与 `_SupportsArrayFunc`（`__array_function__`），并把前者塞进 `ArrayLike`。 |
| `numpy/_typing/_nested_sequence.py` | 对照 | 另一个 runtime_checkable 协议 `_NestedSequence`，用来对比「泛型协议」与「带默认值的 TypeVar」语法。 |
| `numpy/typing/tests/data/pass/array_like.py` | 正例 | 自定义类 `A` 实现 `__array__` 后即被当作 `ArrayLike` 与 `_SupportsArray`。 |
| `numpy/typing/tests/data/fail/array_like.pyi` | 反例 | `__array__(dtype=...)` 调用被类型检查器拒绝，说明协议刻意只刻画「默认 dtype」情形。 |
| `numpy/typing/tests/test_runtime.py` | 运行时测试 | 断言 `isinstance(np.arange(10), _SupportsArray)` 等为真，是 `@runtime_checkable` 的活证据。 |
| `numpy/_core/_asarray.pyi` | 消费方 | `np.require` 的 `like: _SupportsArrayFunc | None` 参数，展示 `_SupportsArrayFunc` 的真实用途。 |

> 注意路径：所有定义都在**私有** `numpy/_typing/` 或 `numpy/_core/` 下。公共壳 `numpy/typing/` 不直接导出这两个协议。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：`Protocol` + `runtime_checkable`（地基）、`_SupportsArray`（`__array__` 协议）、`_SupportsArrayFunc`（`__array_function__` 协议）、二者在 `ArrayLike` / `like=` 中的鸭子类型作用。

### 4.1 `Protocol` 与 `runtime_checkable`：让鸭子类型进入类型系统

#### 4.1.1 概念说明

NumPy 的 `np.array(...)` 能接受五花八门的输入：`list`、`memoryview`、`np.ndarray`、甚至你自己写的「带 `__array__` 方法的类」。这些对象之间**没有继承关系**——你的类并没有继承 `np.ndarray`，却仍能被 NumPy 当数组用。这就是「鸭子类型」。

但是静态类型检查器默认只认**继承**。要让检查器也理解「鸭子类型」，需要 **`Protocol`（PEP 544）**：它定义一个「形状契约」，任何拥有对应方法的对象都自动符合该协议，无需继承。这种按结构而非按名字匹配的子类型关系，叫**结构子类型（structural subtyping）**。

`Protocol` 类本身默认**不能**用于运行时 `isinstance()`。原因很好理解：运行时要检查「一个对象是否拥有某组方法且签名兼容」，既慢又难做对。PEP 544 的折中是 `@runtime_checkable`：加上它，协议就**允许** `isinstance()` / `issubclass()`，但代价是运行时检查变得**很浅**。

#### 4.1.2 核心流程

一个被 `@runtime_checkable` 标记的协议，其 `isinstance(obj, Proto)` 的运行时行为如下：

```
isinstance(obj, Proto) 为真  ⟺  obj 拥有 Proto 中声明的全部「成员名」
```

关键点（务必记住的「浅」）：

- **只看名字在不在**：运行时只检查对象上是否存在协议声明的方法/属性名，**不**检查参数类型、返回类型、甚至不保证该成员**可调用**。
- **静态侧更严**：类型检查器（mypy/pyright）会做完整签名匹配。所以同一个对象，可能「运行时 `isinstance` 为真，但静态类型检查认为它不符合协议」——这正是「类型比运行时更严格」的又一处体现。
- **`None` 永远不满足**：`None` 缺少这些方法名，所以 `isinstance(None, Proto)` 为假。

可以把它总结成一张对照表：

| 维度 | 类型检查器（静态） | `isinstance`（运行时，`@runtime_checkable`） |
| --- | --- | --- |
| 依据 | 完整方法签名 | 仅成员名是否存在 |
| 强度 | 严格 | 浅层 |
| 可用于 `isinstance` | 否（普通 Protocol） | 是 |
| 用途 | 推导 `ArrayLike` 等别名 | 防御式检查、测试断言 |

#### 4.1.3 源码精读

导入处直接点明本讲用到的两件「法宝」：

[`numpy/_typing/_array_like.py:2`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_array_like.py#L2) 从 `typing` 引入 `Protocol` 与 `runtime_checkable`；同行 `Callable` / `Collection` 则是后面 `__array_function__` 签名要用到的（`from collections.abc import ...` 见 [`L1`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_array_like.py#L1)）。

NumPy 用运行时测试把「浅层 `isinstance` 真的能工作」这件事钉成断言：

[`numpy/typing/tests/test_runtime.py:88-L99`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/test_runtime.py#L88-L99) 中的 `PROTOCOLS` 字典把 `_SupportsArray` / `_SupportsArrayFunc` / `_NestedSequence` 三个协议分别配上一个真实对象（`np.arange(10)`、`np.arange(10)`、`[1]`），`test_isinstance` 断言 `isinstance(obj, cls)` 为真、`isinstance(None, cls)` 为假——这就是 `@runtime_checkable` 在项目里的「验收单」。

#### 4.1.4 代码实践

1. **目标**：亲手验证 `@runtime_checkable` 的「浅层」语义——构造一个方法名对、但签名完全不对的假对象，看 `isinstance` 是否仍返回 `True`。
2. **操作步骤**（示例代码，非项目原有代码）：
   ```python
   # pip install numpy
   from numpy._typing import _SupportsArray

   class Fake:
       # 名字对得上，但它根本不是合法的 __array__
       __array__ = "我只是一个字符串，不是方法"

   obj = Fake()
   print("isinstance:", isinstance(obj, _SupportsArray))   # 观察这行
   print("isinstance(None, ...):", isinstance(None, _SupportsArray))  # 观察
   ```
3. **需要观察的现象**：第一条 `isinstance` 是否打印 `True`（即便 `__array__` 不是可调用对象）；第二条是否打印 `False`。
4. **预期结果**：`isinstance` 只看成员名存在与否，因此第一条通常为 `True`、第二条为 `False`。这恰好印证「运行时很浅」。
5. 若结果与预期不符，请标注「待本地验证」并记录你的 Python / NumPy 版本（不同版本对非可调用成员的处理略有差异）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `_SupportsArray` 必须加 `@runtime_checkable`，而 `Protocol` 单独不够？
**答案**：`Protocol` 只提供「结构子类型」的静态契约，默认不允许 `isinstance()`。`@runtime_checkable` 才是「打开运行时 `isinstance` 开关」的装饰器，使 4.1.4 的实践成为可能。

**练习 2**：若一个类有 `__array__` 方法但返回 `int` 而非 `np.ndarray`，`isinstance(obj, _SupportsArray)` 在运行时为真还是假？类型检查器又会怎么看？
**答案**：运行时为**真**（`isinstance` 不看返回类型，只看方法名存在）；类型检查器会认为它**不符合**协议（返回类型不匹配）。这正是「类型比运行时更严格」的体现。

---

### 4.2 `_SupportsArray`：把 `__array__` 协议搬进类型系统

#### 4.2.1 概念说明

运行时，NumPy 用「你有没有 `__array__` 方法」来识别「能被 `np.asarray()` 转成数组的对象」。`_SupportsArray` 就是把这条鸭子约定**翻译成静态类型**：它是一个协议，凡拥有形如 `__array__(self) -> np.ndarray[...]` 方法的对象都符合它。

它的特别之处在于**带一个类型参数 `DTypeT`**：`_SupportsArray[np.dtype[np.float64]]` 描述的是「`__array__` 返回 float64 数组的对象」。于是 dtype 的元素类型可以沿协议**流动**——这正是它能塞进 `ArrayLike` 并参与类型推导的关键。

#### 4.2.2 核心流程

`_SupportsArray` 的定义用到了 **PEP 695 的泛型类语法**（Python 3.12+）：

```python
@runtime_checkable
class _SupportsArray[DTypeT: np.dtype](Protocol):
    def __array__(self) -> np.ndarray[Any, DTypeT]: ...
```

逐字拆解：

- `class _SupportsArray[DTypeT: np.dtype](Protocol)`：方括号里声明类型参数 `DTypeT`，冒号后的 `np.dtype` 是它的**上界（bound）**——`DTypeT` 只能是 `np.dtype[...]` 的某种形态。这是 PEP 695 引入的「类型参数语法」，等价于旧写法 `class _SupportsArray(Protocol[DTypeT])` + `DTypeT = TypeVar("DTypeT", bound=np.dtype)`，但更紧凑。
- `def __array__(self) -> np.ndarray[Any, DTypeT]`：协议要求的方法。返回类型里的 `DTypeT` 把「调用 `__array__` 得到的数组的元素类型」与协议参数绑定在一起。

> 关于 PEP 695 / 696 的边界：`_SupportsArray` 用的是 **PEP 695**（类型参数语法）。**PEP 696**（类型参数的**默认值**）在本包里出现在 `_NestedSequence` 的协变 TypeVar 上——见 [`numpy/_typing/_nested_sequence.py:9`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_nested_sequence.py#L9) 的 `TypeVar("_T_co", covariant=True, default=Any)`，那个 `default=Any` 才是 PEP 696 的产物。两者常被一并提及，但分属不同机制。

把 `_SupportsArray` 代入类型推导，效果是：

```
obj: _SupportsArray[np.dtype[np.float64]]
obj.__array__()   ──类型检查器推导──▶  np.ndarray[Any, np.dtype[np.float64]]
```

注意一个**刻意的设计**：协议里的 `__array__` **没有 `dtype` 参数**。源码注释明确解释了原因。

#### 4.2.3 源码精读

[`numpy/_typing/_array_like.py:17-L24`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_array_like.py#L17-L24) 定义 `_SupportsArray`。上方的注释（L17–L21）说明：「这个协议只关心**默认 dtype**（即 `dtype=None` 或根本不带 `dtype` 参数）的返回数组；具体实现负责补充其余重载。」换句话说，协议为了简单，**只刻画最常见的无参 `__array__()`**。

这条「只刻画默认 dtype」的取舍，在反例夹具里被固化成断言：

[`numpy/typing/tests/data/fail/array_like.pyi:10-L13`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/fail/array_like.pyi#L10-L13) 对 `np.int64(1)` / `np.array([1])` 调用 `.__array__(dtype=np.float64)` 被标 `# type: ignore[call-overload]`，说明在类型视角下，`__array__` 的「带 dtype 重载」并不被这套协议/桩默认接受。

正例夹具则展示 `_SupportsArray` 的「参数化」用法，把 dtype 元素类型精确锁定：

[`numpy/typing/tests/data/pass/array_like.py:25-L31`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/pass/array_like.py#L25-L31) 中 `scalar: _SupportsArray[np.dtype[np.int64]] = np.int64(1)`、`array: _SupportsArray[np.dtype[np.int_]] = np.array(1)`，以及把自定义类 `A` 标注为 `_SupportsArray[np.dtype[np.float64]]`（见 `A` 的定义 [`L18-L23`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/pass/array_like.py#L18-L23)，其 `__array__` 返回 `NDArray[np.float64]`）。注意 `A` **没有继承** `_SupportsArray`，仅凭「拥有签名兼容的 `__array__`」就被类型检查器接纳——这就是结构子类型。

#### 4.2.4 代码实践

1. **目标**：写一个带 `__array__` 的类，让它在**静态**和**运行时**同时满足 `_SupportsArray`，并作为 `ArrayLike` 传入函数。
2. **操作步骤**（示例代码）：
   ```python
   import numpy as np
   from numpy._typing import _SupportsArray   # 私有，仅供学习
   import numpy.typing as npt

   class MyFrame:
       """一个极简的「类数组」对象，只实现 __array__。"""
       def __array__(self, dtype=None) -> np.ndarray:
           return np.array([1.0, 2.0, 3.0], dtype=dtype)

   frame = MyFrame()

   # (a) 运行时：runtime_checkable 让 isinstance 可用
   print("isinstance:", isinstance(frame, _SupportsArray))

   # (b) 运行时：真的能被 np.asarray 转换
   print("asarray:", np.asarray(frame))

   # (c) 作为 ArrayLike 传入函数
   def sum_of(x: npt.ArrayLike) -> np.floating:
       return np.asarray(x).sum()

   print("sum:", sum_of(frame))
   ```
3. **需要观察的现象**：(a) `isinstance` 是否为 `True`；(b) 转换出的数组形状/数值；(c) `sum_of` 的返回值。
4. **预期结果**：(a) `True`（`MyFrame` 有 `__array__`）；(b) `[1. 2. 3.]`；(c) `6.0`。
5. 静态侧可选：把脚本交给 `mypy`（`mypy --strict your_script.py`）或 pyright，确认**没有**关于 `sum_of(frame)` 的报错——因为 `MyFrame` 结构上满足 `ArrayLike` 里的 `_SupportsArray` 分支。若你用的检查器版本对私有 `_SupportsArray` 报告有差异，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：把 `MyFrame.__array__` 的返回类型从 `np.ndarray` 改成 `int`，静态检查会怎样？运行时 `isinstance` 会怎样？
**答案**：静态检查器会认为 `MyFrame` **不再满足** `_SupportsArray`（返回类型不兼容），进而 `sum_of(frame)` 报错；运行时 `isinstance(frame, _SupportsArray)` 仍为 `True`（只看方法名）。再次印证 4.1 的「浅」。

**练习 2**：`_SupportsArray[np.dtype[np.float64]]` 里的 `DTypeT` 被「钉死」成了什么？
**答案**：`np.dtype[np.float64]`。于是该类型下 `obj.__array__()` 的返回类型被推导为 `np.ndarray[Any, np.dtype[np.float64]]`。

---

### 4.3 `_SupportsArrayFunc`：把 `__array_function__` 协议搬进类型系统

#### 4.3.1 概念说明

`__array_function__`（NEP 18）是 NumPy 更高级的协议：它让第三方数组类型能**接管**对 `np.add(x, y)`、`np.concatenate(...)` 这类**顶层函数**的调用。例如 `x` 是 CuPy 数组时，`np.add(x, y)` 不会被 NumPy 执行，而是转交给 CuPy 的 `__array_function__`，从而返回 CuPy 数组。

`_SupportsArrayFunc` 就是把「实现了 `__array_function__`」这件事刻画成一个协议。与 `_SupportsArray` 不同，它**不带类型参数**——因为它不关心 dtype，只关心「这个对象能不能参与函数派发」。

#### 4.3.2 核心流程

`__array_function__` 的标准签名（NEP 18 规定）有四个参数：

```python
def __array_function__(
    self,
    func,      # 被调用的 NumPy 函数，例如 np.add
    types,     # 参与本次调用的、都实现了 __array_function__ 的参数类型集合
    args,      # 位置参数元组
    kwargs,    # 关键字参数字典
) -> object: ...
```

类型层面，`_SupportsArrayFunc` 把它们一一标出。它的设计要点是：

- **非参数化**：`class _SupportsArrayFunc(Protocol)` 没有 `[...]`，因为它无需传递 dtype 信息。
- **有 docstring**：与 `_SupportsArray`（靠上方注释说明）不同，它把说明写进了类文档字符串。

它最典型的**消费场景**是 `np.asarray` / `np.array` / `np.require` 等函数的 `like=` 参数：

```python
np.asarray(data, like=other_array)   # 创建一个与 other_array 同类型的数组
```

`like=` 期望的正是「实现了 `__array_function__`」的对象——这样 NumPy 才能把创建工作委托给 `other_array` 所属的库（NEP 35）。

#### 4.3.3 源码精读

[`numpy/_typing/_array_like.py:27-L36`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_array_like.py#L27-L36) 定义 `_SupportsArrayFunc`：`@runtime_checkable` + `class _SupportsArrayFunc(Protocol)`，签名把 `func` 标为 `Callable[..., Any]`、`types` 标为 `Collection[type[Any]]`、`args` 标为 `tuple[Any, ...]`、`kwargs` 标为 `dict[str, Any]`，返回 `object`。

它被聚合进 `numpy._typing` 后，在桩文件里成为 `like=` 的类型：

[`numpy/_core/_asarray.pyi:4`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/_asarray.pyi#L4) `from numpy._typing import ..., _SupportsArrayFunc`；[`numpy/_core/_asarray.pyi:24`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/_asarray.pyi#L24) 中 `np.require` 的重载把 `like` 标为 `_SupportsArrayFunc | None`。这是 `_SupportsArrayFunc` 在真实 API 里的落脚点：只有「带 `__array_function__`」的对象才允许放进 `like=`。

#### 4.3.4 代码实践

1. **目标**：验证「普通 `np.ndarray` 同时满足 `_SupportsArray` 和 `_SupportsArrayFunc`」，并理解 `like=` 为何只认后者。
2. **操作步骤**（示例代码）：
   ```python
   import numpy as np
   from numpy._typing import _SupportsArray, _SupportsArrayFunc

   a = np.arange(10)

   print("SupportsArray     :", isinstance(a, _SupportsArray))
   print("SupportsArrayFunc :", isinstance(a, _SupportsArrayFunc))

   # like= 期望 _SupportsArrayFunc；用一个 ndarray 当 like
   b = np.asarray([1, 2, 3], like=a)
   print("type(b):", type(b))
   ```
3. **需要观察的现象**：两个 `isinstance` 是否都为 `True`；`type(b)` 是否为 `numpy.ndarray`。
4. **预期结果**：均为 `True`（`ndarray` 同时实现 `__array__` 与 `__array_function__`）；`type(b)` 为 `numpy.ndarray`。这与 [`test_runtime.py:89-L90`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/test_runtime.py#L89-L90) 用 `np.arange(10)` 同时检验两个协议一致。
5. 若你想看到 `like=` 真正「跨库」的效果，需要安装第三方库（如 CuPy/Dask），标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`_SupportsArrayFunc` 与 `_SupportsArray` 都用 `@runtime_checkable`，但一个带类型参数、一个不带。为什么 `_SupportsArrayFunc` 不需要 `DTypeT`？
**答案**：`__array_function__` 关注的是「能否参与顶层函数派发」，与 dtype 无关；而 `__array__` 的返回数组带具体元素类型，需要 `DTypeT` 把这层信息传出去参与推导。

**练习 2**：`np.require` 的 `like=` 标注成 `_SupportsArrayFunc | None`，而不是 `_SupportsArray | None`。这意味着什么？
**答案**：`like=` 需要的是「能接管函数派发」的对象（NEP 35 的委托创建），即必须有 `__array_function__`；仅有 `__array__`（`_SupportsArray`）不够，因为委托创建走的是 `__array_function__` 通道。

---

### 4.4 两个协议在 `ArrayLike` 中的鸭子类型作用

#### 4.4.1 概念说明

回到本单元的主线：`ArrayLike` 为什么要把 `_SupportsArray` 当作一块「拼图」？因为 `np.array(...)` 接受的一大类输入，正是「实现了 `__array__` 的对象」——你的自定义类、pandas 的某些对象、第三方数组封装等。它们没有共同基类，唯一的共性是「长着 `__array__`」。用结构子类型（`Protocol`）刻画这种共性，是**唯一**能在静态类型里精确表达的办法。

`_SupportsArrayFunc` 不直接出现在公共 `ArrayLike` 里（它服务的是 `like=` 这类参数），但它和 `_SupportsArray` 共享同一套「把运行时鸭子协议翻译成静态类型」的设计思路。理解了这两个，你就理解了 NumPy 类型系统对「类数组对象」的整体建模哲学：**用协议描述形状，而不是用继承描述血统**。

#### 4.4.2 核心流程

`ArrayLike` 把 `_SupportsArray` 嵌入 `_DualArrayLike`，再与 `Buffer` 取或：

```
ArrayLike
  = Buffer                                      # 缓冲区协议（bytes/memoryview/ndarray...）
  | _DualArrayLike[np.dtype, complex|bytes|str]
        = _SupportsArray[DTypeT]                # ← 本讲的 __array__ 协议
        | _NestedSequence[_SupportsArray[DTypeT]]
        | BuiltinT                              # complex/bytes/str 等内置标量
        | _NestedSequence[BuiltinT]
```

可以看到 `_SupportsArray` 出现在 `_DualArrayLike` 的**第一条**分支——它是「自定义类数组对象」进入 `ArrayLike` 的唯一入口。`_NestedSequence[_SupportsArray[...]]` 则进一步覆盖「装着类数组对象的嵌套序列」。

#### 4.4.3 源码精读

[`numpy/_typing/_array_like.py:40-L55`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_array_like.py#L40-L55) 一气给出 `_ArrayLike`（L40–L43，只用 `_SupportsArray` + `_NestedSequence`，最严格）、`_DualArrayLike`（L48–L53，双轨参数化引擎）和顶层公共 `ArrayLike`（L55）。注意 `_DualArrayLike` 的第一块就是 `_SupportsArray[DTypeT]`——这是 4.2 的协议在别名里的落脚点。

对照 `_NestedSequence` 这个「同样是 runtime_checkable 协议、但用旧式 `Protocol[_T_co]` 语法」的伙伴：

[`numpy/_typing/_nested_sequence.py:19-L20`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_nested_sequence.py#L19-L20) 的 `class _NestedSequence(Protocol[_T_co])` 用的是**旧式** `Protocol[_T_co]`（带协变 TypeVar）。两相对比：`_SupportsArray` 走 PEP 695 新语法 `class _SupportsArray[DTypeT: np.dtype](Protocol)`，`_NestedSequence` 走经典 `Protocol[_T_co]`。项目里两种写法并存，理解时只要知道「它们都是泛型协议」即可。

#### 4.4.4 代码实践

1. **目标**：用一个**同时实现** `__array__` 与 `__array_function__` 的类，分别检验它对两个协议的归属，并把它同时喂给「吃 `ArrayLike`」和「吃 `like=`」的两处。
2. **操作步骤**（示例代码）：
   ```python
   import numpy as np
   from collections.abc import Callable, Collection
   from numpy._typing import _SupportsArray, _SupportsArrayFunc
   import numpy.typing as npt

   class Both:
       def __array__(self, dtype=None) -> np.ndarray:
           return np.arange(5, dtype=dtype)

       def __array_function__(
           self,
           func: Callable, types: Collection[type],
           args: tuple, kwargs: dict,
       ) -> object:
           return NotImplemented   # 示例占位，演示类型即可

   obj = Both()

   # 两个协议都满足
   print("SupportsArray     :", isinstance(obj, _SupportsArray))
   print("SupportsArrayFunc :", isinstance(obj, _SupportsArrayFunc))

   # 作为 ArrayLike 输入
   def head(x: npt.ArrayLike) -> np.ndarray:
       return np.asarray(x)[:2]
   print("head:", head(obj))

   # 作为 like= 输入（需要 _SupportsArrayFunc）
   print("like:", np.asarray([1, 2, 3], like=obj))
   ```
3. **需要观察的现象**：两个 `isinstance` 是否都为 `True`；`head(obj)` 是否返回 `array([0, 1])`；`like=` 调用是否报 `TypeError`（注意：示例里 `__array_function__` 返回 `NotImplemented`，`np.asarray` 的 `like=` 委托若得不到有效结果可能抛错）。
4. **预期结果**：两个 `isinstance` 为 `True`；`head(obj)` 返回 `array([0, 1])`；`like=` 的具体表现取决于 NumPy 版本对 `NotImplemented` 的处理——若报错属正常，标注「待本地验证」并阅读报错信息（这正是理解 NEP 18/35 派发失败的好机会）。
5. 若想让 `like=` 成功，把 `__array_function__` 实现成真正返回一个 ndarray（例如直接调用 `func(*args, **kwargs)`），再观察结果。

#### 4.4.5 小练习与答案

**练习 1**：为什么 NumPy 不直接让所有「类数组对象」继承自一个公共基类（比如 `np.ndarray`），而要用 `_SupportsArray` 协议？
**答案**：因为类数组对象来自四面八方（pandas、CuPy、用户自定义类），无法也不应强制它们继承 NumPy 的类。协议提供**结构子类型**：只要形状（方法）对，就算同类，零侵入。

**练习 2**：在 `_DualArrayLike` 里，如果把第一块 `_SupportsArray[DTypeT]` 删掉，`ArrayLike` 会失去什么能力？
**答案**：会失去「接受实现了 `__array__` 的自定义/第三方类数组对象」的能力——你的 `MyFrame` / `Both` 之类将不再被当作合法 `ArrayLike`，只能用 `list` / `tuple` / 标量 / `Buffer`。

---

## 5. 综合实践

把本讲的三件事——`@runtime_checkable` 的浅层语义、`_SupportsArray` 的参数化协议、`_SupportsArrayFunc` 在 `like=` 中的用途——串成一个任务。

**任务：实现一个「可被 NumPy 识别」的最小二维点集类型，并验证它在三个层面的可用性。**

要求：

1. 写一个类 `Point2D`，内部用 `np.ndarray` 存储若干个 `(x, y)` 点。
2. 实现 `__array__(self, dtype=None) -> np.ndarray`，返回内部数组。
3. 用运行时 `isinstance(pt, _SupportsArray)` 验证它满足协议；并说明这一检查为何不能保证「签名正确」（结合 4.1 的浅层语义作答）。
4. 写一个函数 `def bbox(p: npt.ArrayLike) -> np.ndarray`，计算点集的最小/最大坐标，把 `Point2D` 实例作为 `ArrayLike` 传入。
5. 把脚本交给 mypy 或 pyright，确认 4 中 `bbox(pt)` **无类型错误**——这等价于在静态侧「证明」`Point2D` 结构上满足 `ArrayLike`。
6. 在结论里用一句话对比：`isinstance`（运行时、浅层、看名字）与类型检查器（静态、严格、看签名）分别保证了什么。

预期：运行时 `isinstance` 为 `True`，`bbox` 返回正确坐标范围，静态检查通过。若 mypy 对私有 `_SupportsArray` 的报告有版本差异，记录现象并标注「待本地验证」。这个任务把「鸭子类型如何被搬进类型系统」从概念落到了可运行、可类型检查的代码上。

## 6. 本讲小结

- `Protocol`（PEP 544）提供**结构子类型**：按「形状」（方法）匹配，而非按继承匹配——这是刻画「类数组对象」的唯一合适手段。
- `@runtime_checkable` 给协议**打开 `isinstance` 开关**，但运行时检查**很浅**：只看成员名是否存在，不看签名/类型，也不保证可调用。
- `_SupportsArray[DTypeT: np.dtype](Protocol)` 用 **PEP 695 泛型协议语法**把运行时 `__array__` 翻译成静态类型，并通过 `DTypeT` 让 dtype 元素类型沿协议流动；它刻意只刻画「默认 dtype」的无参重载。
- `_SupportsArrayFunc(Protocol)` 刻画 `__array_function__`（NEP 18），不带类型参数，是 `np.asarray` / `np.require` 的 `like=` 参数的真实类型（NEP 35 委托创建）。
- 在 `ArrayLike` 里，`_SupportsArray` 是「自定义/第三方类数组对象」进入别名的**唯一入口**（`_DualArrayLike` 的第一块拼图）。
- `np.ndarray` 同时满足这两个协议，故 `isinstance(np.arange(10), _SupportsArray)` 与 `... _SupportsArrayFunc` 均为真，由 `test_runtime.py` 固化为断言。

## 7. 下一步学习建议

- 下一篇 [u3-l2（`_NestedSequence`：嵌套序列协议）](u3-l2-nested-sequence-protocol.md) 讲解 `ArrayLike` 的另一块拼图：用**递归类型**与**协变 TypeVar `_T_co`**（PEP 696 默认值）描述任意深度嵌套序列，并对比新旧两种泛型协议语法。
- 之后 [u3-l3（`_SupportsDType` 与 `_HasDType` 协议）](u3-l3-supportsdtype-protocols.md) 把同样的「协议化鸭子类型」思路迁移到 dtype 侧，对应 `DTypeLike`。
- 想深入运行时侧的协议，可阅读 [`numpy/typing/tests/test_runtime.py`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/test_runtime.py) 的 `TestRuntimeProtocol`，以及 NEP 18（`__array_function__`）/ NEP 35（`__array__` 与 `like=` 的正式化）。
