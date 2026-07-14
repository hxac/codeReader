# ArrayLike：一切可转为数组的对象

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 `ArrayLike` 这个公共别名到底由哪几块「积木」拼成，并能逐块解释它们的来源。
- 读懂 `_array_like.py` 里 `ArrayLike` / `_DualArrayLike` / `_ArrayLike` 三层别名的组合关系，理解为什么需要「双轨」参数化。
- 解释 `_SupportsArray` 这个 `@runtime_checkable` 协议在 `ArrayLike` 中扮演的角色（「会变出数组的对象」）。
- 理解 `_NestedSequence` 如何用一个**递归类型**描述「任意深度的嵌套序列」。
- 说清 NumPy 类型系统「刻意避开 object 数组」的设计取舍，并知道在确实需要 object 数组时该怎么「逃生」。

## 2. 前置知识

本讲承接 [u1-l2（公共 API 与目录结构）](u1-l2-public-api-and-layout.md)。你已经知道：

- `numpy.typing` 是一层极薄的公共壳，对外只暴露 `ArrayLike` / `DTypeLike` / `NDArray` / `NBitBase` 四个名字；
- 真正的实现藏在私有的 `numpy._typing` 包里，由 `numpy/typing/__init__.py` 用一行 `from numpy._typing import ...` 把名字「搬」过来。

本讲需要补充三个 Python 类型基础概念：

1. **`type` 语句（PEP 695 类型别名）**
   Python 3.12 起，可以用 `type Name[Params] = <表达式>` 定义一个**类型别名**。它和 `X = int | str` 这种普通赋值的区别在于：`type` 语句定义的别名是「一等」的、可参数化的、且对类型检查器更友好。本讲里几乎所有名字（`ArrayLike`、`_DualArrayLike` 等）都是用 `type` 语句定义的。

2. **`Protocol`（鸭子类型的类型版）**
   普通类型看「你是不是这个类的实例」；`Protocol` 看「你有没有这些方法」。一个对象只要长着协议规定的方法/属性，类型检查器就认为它「符合」该协议——这叫**结构子类型（structural subtyping）**，俗称「鸭子类型」。本讲的 `_SupportsArray`、`_NestedSequence` 都是协议。

3. **联合类型 `A | B`**
   读作「A 或 B」。`ArrayLike` 的本质就是一大串「或」：是缓冲区、或是带 `__array__` 的对象、或是若干内置标量、或这些的任意嵌套。

> 提示：`Buffer`（`collections.abc.Buffer`）是 Python 3.12 引入的、代表「缓冲区协议」的抽象类型，`bytes` / `bytearray` / `memoryview` / `array.array` / `ndarray` 本身都满足它。本讲会把它当作一个「已知积木」使用。

## 3. 本讲源码地图

本讲盯住「`ArrayLike` 是怎么拼出来的」这一件事，涉及的真实文件如下：

| 文件 | 角色 | 说明 |
| --- | --- | --- |
| `numpy/_typing/_array_like.py` | **主战场** | 定义 `ArrayLike` 及其内部积木 `_SupportsArray`、`_SupportsArrayFunc`、`_ArrayLike`、`_DualArrayLike`，以及一堆 `_ArrayLike*_co` 收窄别名。 |
| `numpy/_typing/_nested_sequence.py` | 嵌套序列协议 | 用递归类型定义 `_NestedSequence`，描述「任意深度嵌套的序列」。 |
| `numpy/typing/__init__.py` | 公共壳 | 用文档字符串正式说明「`ArrayLike` 刻意避开 object 数组」这一设计取舍，并把 `ArrayLike` 再导出给用户。 |
| `numpy/typing/tests/data/pass/array_like.py` | 正例测试 | 列出「合法的 `ArrayLike` 赋值」，可当作速查表。 |
| `numpy/typing/tests/data/fail/array_like.pyi` | 反例测试 | 列出「类型检查器应当拒绝的 `ArrayLike` 赋值」，是理解「避开 object 数组」最直接的证据。 |

> 注意路径：`ArrayLike` 的实现都在**私有**的 `numpy/_typing/` 下；公共壳 `numpy/typing/` 只负责转发与文档。这一「公共壳 + 私有实现」分层是 u1-l2 的结论，本讲直接沿用。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：`ArrayLike`（顶层别名）、`_SupportsArray`（带 `__array__` 的协议）、`_NestedSequence`（嵌套序列协议）、`_DualArrayLike`（双轨参数化引擎 + 避开 object 数组的取舍）。

### 4.1 `ArrayLike`：顶层别名与它的四块拼图

#### 4.1.1 概念说明

`np.array(...)` 能吃进形形色色的输入：一个 `list`、一个 `tuple`、一个标量、一个 `memoryview`、一个你自己写的「带 `__array__` 方法的对象」……这些「能被转成数组的东西」统称为 **array-like**。

`ArrayLike` 就是把这些「能转成数组的对象」在**类型层面**归纳出来的一个公共别名。它的定义只有一行：

```python
type ArrayLike = Buffer | _DualArrayLike[np.dtype, complex | bytes | str]
```

这一行把 `ArrayLike` 拆成了**两块大积木**，再用「或」连起来：

1. **`Buffer`** —— 满足缓冲区协议的对象（`bytes` / `memoryview` / `array.array` / `ndarray` 等）。
2. **`_DualArrayLike[np.dtype, complex | bytes | str]`** —— 一个可参数化的「双轨」别名，这里填了两个参数：
   - 第一轨 `DTypeT = np.dtype`：任意 dtype 的「带 `__array__` 的对象」（及其嵌套序列）；
   - 第二轨 `BuiltinT = complex | bytes | str`：若干内置标量（及其嵌套序列）。

为什么第二轨只写 `complex | bytes | str` 就够了？这是 Python 类型系统的一个特殊约定：在类型检查的世界里，`int`、`float`、`bool` 都被当作 `complex` 的子类型。所以 `complex` 一个词就同时涵盖了 `bool`、`int`、`float`、`complex` 四种数值标量，再加上 `bytes`、`str`，正好覆盖了「NumPy 能合理转成非 object 数组的全部内置标量」。

一句话直觉：**`ArrayLike` ≈ 缓冲区 ∪（带 `__array__` 的对象）∪（数值/字节/字符串标量），三者都允许任意层嵌套。**

#### 4.1.2 核心流程

判断一个对象 `x` 是不是 `ArrayLike`，等价于依次问四个问题（任一为「是」即可）：

```
x 是 ArrayLike 吗？
├─ 1. x 满足缓冲区协议（Buffer）吗？            → bytes / memoryview / array.array / ndarray …
├─ 2. x 有 __array__() 方法吗？（_SupportsArray） → ndarray、np 标量、自定义数组类
├─ 3. x 是 complex / bytes / str 之一吗？         → True/5/1.0/1j/"foo"/b"foo"
└─ 4. x 是 2 或 3 的「任意深度嵌套序列」吗？（_NestedSequence） → [1,2,3] / [[1.0]] / (1,(2,3)) …
```

注意第 4 步是递归的：嵌套序列里的元素，要么是叶子（标量 / 带 `__array__` 的对象），要么又是一个嵌套序列。这正是 `_NestedSequence` 要解决的问题（见 4.3）。

#### 4.1.3 源码精读

`ArrayLike` 的定义在私有实现文件里（注意是 `numpy/_typing/`，不是 `numpy/typing/`）：

[`numpy/_typing/_array_like.py:55`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_array_like.py#L55) 定义了公共别名 `ArrayLike`，由 `Buffer` 与 `_DualArrayLike[np.dtype, complex | bytes | str]` 取「或」构成——这就是「四块拼图」的顶层入口。

[`numpy/_typing/_array_like.py:1`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_array_like.py#L1) 顶部 `from collections.abc import Buffer, ...` 证实第一块积木 `Buffer` 来自标准库（Python 3.12+）。

公共壳再把这个名字转发给用户，并正式记录设计取舍：

[`numpy/typing/__init__.py:175`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L175) 公共壳用一行 `from numpy._typing import ArrayLike, ...` 把私有实现搬成公共名字；

[`numpy/typing/__init__.py:26-L53`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L26-L53) 模块文档的 `ArrayLike` 小节，用 `np.array(x**2 for x in range(10))` 这个生成器例子，正式声明「`ArrayLike` 刻意避开 object 数组」（详见 4.4）。

#### 4.1.4 代码实践

**目标**：用一张「正例表」建立对 `ArrayLike` 的直觉。

**步骤**：

1. 打开测试夹具 [`numpy/typing/tests/data/pass/array_like.py:4-L15`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/pass/array_like.py#L4-L15)，它把 `True`、`5`、`1.0`、`1+1j`、`np.int8(1)`、`np.array([1,2,3])`、`[1,2,3]`、`(1,2,3)`、`"foo"`、`memoryview(b'foo')` 全部赋值给 `ArrayLike` 类型的变量，且类型检查器全部放行。
2. 自己新建一个脚本 `explore_arraylike.py`（示例代码，非项目原有文件），把其中几行抄进去：

   ```python
   # 示例代码
   import numpy as np
   from numpy._typing import ArrayLike  # 与公共 npt.ArrayLike 等价

   samples: list[ArrayLike] = [True, 5, 1.0, 1 + 1j, b"foo", "foo",
                               memoryview(b"foo"), [1, 2, 3], (1, 2, 3),
                               np.array([1, 2, 3])]
   ```

3. 用 `mypy explore_arraylike.py`（或 pyright）检查。

**需要观察的现象**：以上每一项都不报错。

**预期结果**：`Success` / 无类型错误。这正说明 `complex` 一词覆盖了 `bool/int/float/complex`，`Buffer` 覆盖了 `memoryview`/`bytes`，而 `list`/`tuple` 走的是嵌套序列通道。若你的检查器对某一项报错，多半是 NumPy 版本与 stub 不匹配，建议核对安装的 NumPy 是否 ≥ 2.x 且为 `py.typed` 安装。

#### 4.1.5 小练习与答案

**练习 1**：`ArrayLike` 的顶层定义里，`complex | bytes | str` 为什么不单独列出 `int`、`float`、`bool`？

**参考答案**：因为在 Python 类型检查的约定里，`int`/`float`/`bool` 都被当作 `complex` 的（结构）子类型，写 `complex` 即可一并涵盖；再补 `bytes`、`str` 就覆盖了全部能合理转成非 object 数组的内置标量。

**练习 2**：`np.float64(1)` 是 `ArrayLike` 吗？走的是哪一块积木？

**参考答案**：是。`np.float64` 是 NumPy 标量，本身是 `ndarray` 的近亲，走的是 `_SupportsArray`（带 `__array__`）那块，而不是内置标量那块。`pass/array_like.py` 里 `x6: ArrayLike = np.float64(1)` 正是此例。

---

### 4.2 `_SupportsArray` 协议：带 `__array__` 的对象

#### 4.2.1 概念说明

NumPy 长期支持一个「鸭子协议」：任何一个对象，只要实现了 `__array__(self)` 方法并返回一个 `ndarray`，就被视作「可以变成数组」。这就是 `np.asarray()` 能接受 Pandas 的 `Series`、CuPy 的数组、xarray 的 DataArray 等第三方数组对象的根本原因。

`_SupportsArray` 把这个运行时协议**翻译成类型层面的 `Protocol`**：一个对象只要在类型上「长得像」（声明了符合签名的 `__array__`），类型检查器就认它符合 `_SupportsArray`。

注意它带一个类型参数 `DTypeT`：表示「这个对象变出来的数组，dtype 是什么」。这样就能把「精度/dtype」信息沿着类型系统传下去（与 u4 的精度协变体系呼应）。

#### 4.2.2 核心流程

`_SupportsArray` 在 `ArrayLike` 里起到「接纳一切自定义数组对象」的作用：

```
第三方数组对象（实现了 __array__）
        │  类型上符合 _SupportsArray[DTypeT]
        ▼
   被 ArrayLike 接纳（走 _DualArrayLike 的 DTypeT 轨）
        │
        ▼
   也能被「任意嵌套」：_NestedSequence[_SupportsArray[DTypeT]]
```

源码上方有一段注释特别说明：这个协议**只关心默认 dtype**（即 `dtype=None` 或不带 `dtype` 参数时返回什么）；至于「传不同 `dtype` 得到不同结果」的更多重载，要由具体实现自己去补。

#### 4.2.3 源码精读

[`numpy/_typing/_array_like.py:17-L24`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_array_like.py#L17-L24) 定义 `_SupportsArray`。关键几行：

```python
@runtime_checkable
class _SupportsArray[DTypeT: np.dtype](Protocol):
    def __array__(self) -> np.ndarray[Any, DTypeT]: ...
```

逐点拆解：

- `class _SupportsArray[DTypeT: np.dtype](Protocol)` —— 这是 **PEP 695 泛型协议**语法：方括号里的 `DTypeT: np.dtype` 声明一个「以上界为 `np.dtype` 的类型参数」。等价的旧写法是 `Protocol[DTypeT]` + 顶部一个 `TypeVar("DTypeT", bound=np.dtype)`（u3 会专门对比这两种写法）。
- `def __array__(self) -> np.ndarray[Any, DTypeT]` —— 协议要求的方法：无参（默认 dtype），返回 `ndarray`，其 dtype 由 `DTypeT` 参数化。
- `@runtime_checkable` —— 允许 `isinstance(x, _SupportsArray)` 在运行时工作（只检查方法是否存在，不检查签名）。

紧挨着它还有一个「姊妹协议」：

[`numpy/_typing/_array_like.py:27-L36`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_array_like.py#L27-L36) 定义 `_SupportsArrayFunc`，对应运行时的 `__array_function__` 协议（决定对象能否拦截 `np.add(x, y)` 这类全局函数调用）。它**不参与** `ArrayLike`，是另一条线索，u3-l1 会专门讲它。

> 关于 `__array__` 只认默认 dtype 这一点，`fail/array_like.pyi` 里有反例佐证：对 `_SupportsArray` 调用 `__array__(dtype=np.float64)` 会被标记 `# type: ignore[call-overload]`，因为协议里根本没声明「带 `dtype` 参数」这个重载。详见 [`numpy/typing/tests/data/fail/array_like.pyi:10-L13`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/fail/array_like.pyi#L10-L13)。

#### 4.2.4 代码实践

**目标**：亲手实现一个「符合 `_SupportsArray`」的自定义类，并验证它在运行时和类型层都被 `ArrayLike` 接纳。

**步骤**：

1. 阅读项目自带的正例 [`numpy/typing/tests/data/pass/array_like.py:18-L23`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/pass/array_like.py#L18-L23)，里面定义了一个带 `__array__` 的类 `A`，并把 `A()` 赋值给 `ArrayLike`。
2. 仿写一个最小版（示例代码）：

   ```python
   # 示例代码
   import numpy as np
   from numpy._typing import ArrayLike, _SupportsArray

   class MyArray:
       def __array__(self) -> np.ndarray:        # 满足 _SupportsArray 协议
           return np.array([1.0, 2.0, 3.0])

   def to_ndarray(x: ArrayLike) -> np.ndarray:
       return np.asarray(x)

   a: ArrayLike = MyArray()        # 类型检查应放行
   print(to_ndarray(a))            # 运行时：[1. 2. 3.]
   print(isinstance(MyArray(), _SupportsArray))  # True，因为 @runtime_checkable
   ```

**需要观察的现象**：

- 类型检查器对 `a: ArrayLike = MyArray()` 不报错；
- 运行时 `isinstance(...)` 打印 `True`。

**预期结果**：类型层放行；运行时打印数组和 `True`。`isinstance` 能成立，完全依赖 `@runtime_checkable`——去掉它，`isinstance` 会直接 `TypeError`。

#### 4.2.5 小练习与答案

**练习 1**：如果 `MyArray.__array__` 改成 `def __array__(self, dtype=None)`（带参数），它还算 `_SupportsArray` 吗？

**参考答案**：仍然算。协议只要求「存在一个可无参调用的 `__array__`」；给方法加默认参数不影响无参调用，类型检查器仍认可。这也是为什么真实世界里很多 `__array__` 都写成带 `dtype=None` 默认值。

**练习 2**：`_SupportsArray` 为什么是**参数化**的（带 `DTypeT`），而不是一个固定类型？

**参考答案**：为了把「变出来的数组是什么 dtype」这一信息保留在类型系统里，供下游（如 `_ArrayLikeFloat_co` 这类收窄别名，见 4.4）做精度判断。一个不带 dtype 信息的 `_SupportsArray` 只能说「它是数组」，却说不出「它是 float64 还是 int8」。

---

### 4.3 `_NestedSequence`：描述任意深度嵌套的递归协议

#### 4.3.1 概念说明

`np.array([[1.0], [2.0, 3.0]])` 能接受任意层级的嵌套序列。问题来了：**如何在类型里表达「嵌套任意层」？** 数组的形状是固定的两层 `list`，还是三层、四层，事先并不知道。

`_NestedSequence` 的答案是一个**递归类型**：一个嵌套序列的元素，要么是叶子值 `_T_co`，要么又是一个 `_NestedSequence[_T_co]`——自己引用自己，于是无论嵌套多深都能展开。

它独立放在 `_nested_sequence.py`，是 `ArrayLike` 之所以能接纳 `list` / `tuple` / 多层嵌套的关键。

#### 4.3.2 核心流程

递归性体现在 `__getitem__`（即 `seq[i]`）的返回类型上：

```
seq[i] 的返回类型 = _T_co | _NestedSequence[_T_co]
                      ↑叶子            ↑又一层嵌套
```

于是：

- `[1.0, 2.0]` —— 取元素得到 `_T_co`（叶子）；
- `[[1.0], [2.0]]` —— 取元素得到 `_NestedSequence[_T_co]`（再取一层才是叶子）；
- `[[[1.0]]]` —— 再多一层，递归一次，依然合法。

无论几层，类型展开都不会穷尽——这正是「任意深度」的表达方式。

`_NestedSequence` 还要求实现一组序列通用方法（`__len__` / `__contains__` / `__iter__` / `count` / `index` / `__reversed__`），让它表现得像一个标准的「可索引、可迭代」序列。

#### 4.3.3 源码精读

[`numpy/_typing/_nested_sequence.py:19-L20`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_nested_sequence.py#L19-L20) 声明协议，用的是**旧式** `Protocol[_T_co]` 语法（与 `_SupportsArray` 的新式 PEP 695 语法形成对照，u3-l2 会细讲）：

```python
@runtime_checkable
class _NestedSequence(Protocol[_T_co]):
```

[`numpy/_typing/_nested_sequence.py:63`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_nested_sequence.py#L63) 是「递归」的灵魂所在——`__getitem__` 的返回类型写作字符串 `"_T_co | _NestedSequence[_T_co]"`（前向引用，因为类型在定义中引用了自身）：

```python
def __getitem__(self, index: int, /) -> "_T_co | _NestedSequence[_T_co]": ...
```

`_T_co` 在文件顶部定义，是一个**协变** TypeVar：

[`numpy/_typing/_nested_sequence.py:5-L13`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_nested_sequence.py#L5-L13) 用 `TYPE_CHECKING` 分支分别从 `typing_extensions`（带 `default=Any`，面向未来）和 `typing`（运行时）导入 `_T_co = TypeVar("_T_co", covariant=True, ...)`。协变意味着：若 `Dog` 是 `Animal` 的子类型，则 `_NestedSequence[Dog]` 也是 `_NestedSequence[Animal]` 的子类型——符合「一组狗也是一组动物」的直觉。

> `_NestedSequence` 与 `ArrayLike` 的连接发生在 `_array_like.py` 里：`_DualArrayLike` 的每一轨都被 `_NestedSequence[...]` 包了一层（见 4.4），这就把「带 `__array__` 的对象」和「内置标量」都升级成了「可以任意嵌套」。

#### 4.3.4 代码实践

**目标**：直观感受 `_NestedSequence` 如何「吃下任意深度」。

**步骤**：参考 `_nested_sequence.py` docstring 里给出的官方示例 [`numpy/_typing/_nested_sequence.py:41-L55`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_nested_sequence.py#L41-L55)，仿写如下（示例代码）：

```python
# 示例代码
from typing import TYPE_CHECKING
import numpy as np
from numpy._typing import _NestedSequence

def first_dtype(seq: _NestedSequence[float]) -> np.dtype:
    return np.asarray(seq).dtype

a = first_dtype([1.0])
b = first_dtype([[1.0]])
c = first_dtype([[[1.0]]])

if TYPE_CHECKING:
    reveal_locals()
```

**需要观察的现象**：用 mypy 跑（`mypy --reveal-type` 或直接依赖 `reveal_locals`），观察 `a/b/c` 的推断类型。

**预期结果**：三者都被 `reveal_locals` 报告为同一类型（`numpy.dtype[numpy.floating[numpy._typing._64Bit]]` 之类），证明「嵌套层数」不影响标量 dtype 的推断。具体输出文本依 mypy/NumPy 版本而异——**待本地验证**确切字符串。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `__getitem__` 的返回类型要写成**字符串** `"_T_co | _NestedSequence[_T_co]"`，而不是直接写裸表达式？

**参考答案**：因为类型在定义体内部引用了正在定义的名字 `_NestedSequence` 自身。直接写裸表达式在某些 Python 版本下会触发 `NameError`（名字还没定义完）；写成字符串作为「前向引用」，等类定义完成后再解析，就能正常自引用。

**练习 2**：`_NestedSequence` 协议里规定了好几个方法（`__len__`/`__iter__`/`count`/`index`…）。如果某个类只实现了 `__getitem__`，它还符合协议吗？

**参考答案**：不完全符合。协议要求**全部**列出的方法都存在；只实现 `__getitem__` 的类在严格类型检查下不算 `_NestedSequence`。不过 Python 的 `list`/`tuple` 天然实现了全部这些方法，所以日常用的内建序列都满足。

---

### 4.4 `_DualArrayLike` 与「刻意避开 object 数组」的取舍

#### 4.4.1 概念说明

回到顶层那行 `ArrayLike = Buffer | _DualArrayLike[np.dtype, complex | bytes | str]`。`_DualArrayLike` 才是真正的「参数化引擎」：它带**两个**类型参数，因此叫 *Dual*（双轨）。

```python
type _DualArrayLike[DTypeT: np.dtype, BuiltinT] = (
    _SupportsArray[DTypeT]
    | _NestedSequence[_SupportsArray[DTypeT]]
    | BuiltinT
    | _NestedSequence[BuiltinT]
)
```

两条轨分别是：

- **DTypeT 轨**：`_SupportsArray[DTypeT]` 及其嵌套——「带 `__array__`、且 dtype 已知的对象」；
- **BuiltinT 轨**：`BuiltinT` 及其嵌套——「内置标量」。

公共 `ArrayLike` 把两条轨都开到最宽（`DTypeT=np.dtype` 任意 dtype，`BuiltinT=complex|bytes|str` 全部数值/字节/字符串标量）。

为什么费力气搞「双轨」？因为还有一类**私有的收窄别名**要用同一个引擎：比如 `_ArrayLikeFloat_co`（只接受能安全转成浮点的输入）、`_ArrayLikeInt_co`（只接受能转成整数的输入）。它们共用 `_DualArrayLike`，只是把两个参数收得更紧。这样，一个引擎同时服务「最宽松的公共 `ArrayLike`」和「最严格的内部精度别名」。

#### 4.4.2 核心流程：为什么 `ArrayLike` 刻意避开 object 数组

`np.array(...)` 在运行时非常宽容：连「生成器」「任意对象」都能吞，只不过会默默造出一个 `dtype=object` 的数组——这几乎总是 bug（性能差、语义混乱）。

`ArrayLike` 的设计哲学是：**类型系统应当比你更严格，把「合法但不推荐」的写法挡在门外。** 它的做法不是「加一个黑名单」，而是「只把安全的东西放进白名单」。生成器既不是 `Buffer`，也不是 `_SupportsArray`，更不是 `complex|bytes|str` 或它们的嵌套序列——于是自然被排除。

```
                  ┌──────────── 公共 ArrayLike（白名单）────────────┐
运行时 np.array：  │ Buffer ∪ 带__array__ ∪ (complex|bytes|str) ∪ 嵌套 │
非常宽容           └────────────────────────────────────────────────┘
                                  ✗ 生成器、任意对象、dict 都不在白名单内
                                  ✗ → 类型检查器报错（即便运行时能跑出 object 数组）

                  ┌──── 私有 _ArrayLikeObject_co（隔离区）────┐
object 数组支持：  │ _ArrayLike[np.object_]：只给真正需要它的 API 用 │
                  └────────────────────────────────────────────┘
```

如果你**确实**要造 object 数组，文档给出两条「逃生通道」：加 `# type: ignore`，或先把变量显式标成 `Any`。

#### 4.4.3 源码精读

[`numpy/_typing/_array_like.py:45-L53`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_array_like.py#L45-L53) 定义 `_DualArrayLike`，注释点明它「由两个 typevar 组成：一个可按 `np.dtype` 参数化，另一个容纳其余（内置标量）」。注意四项的 `|` 正好是「两轨 ×（单层 / 嵌套）」的笛卡尔展开。

紧挨着上方还有一个**单轨**版本，专供「按标量类型参数化」的场景：

[`numpy/_typing/_array_like.py:39-L43`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_array_like.py#L39-L43) 定义 `_ArrayLike[ScalarT]`，只保留 `_SupportsArray` 那条轨（按 `np.generic` 标量参数化），不带内置标量轨——它主要被 object 相关的收窄别名复用。

「隔离区」就在下面几行：

[`numpy/_typing/_array_like.py:73`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_array_like.py#L73) 定义 `_ArrayLikeObject_co = _ArrayLike[np.object_]`。这是全文件里**唯一**显式接纳 object 数组的别名，且它是**私有**的、带 `_co` 后缀的——只服务于少数确实要处理 object 数组的内部 API，绝不进入公共 `ArrayLike`。

`_co` 后缀的含义写在注释里：

[`numpy/_typing/_array_like.py:57-L58`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_array_like.py#L57-L58) 说明 `_ArrayLike<X>_co` 表示「可在 `same_kind` 转换规则下被强转为 `X` 的 array-like」。例如 [`numpy/_typing/_array_like.py:61`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_array_like.py#L61) 的 `_ArrayLikeInt_co = _DualArrayLike[np.dtype[np.bool | np.integer], int]`，把 DTypeT 轨收窄到「布尔/整数 dtype」、BuiltinT 轨收窄到 `int`，于是 `bool`/`int`/整数数组都能进，`1.5` 这种浮点则被挡下。

「避开 object 数组」最有力的证据是反例测试：

[`numpy/typing/tests/data/fail/array_like.pyi:6-L8`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/fail/array_like.pyi#L6-L8) 三行故意写出会被拒绝的赋值，每行都带 `# type: ignore[assignment]`：

```python
x1: ArrayLike = (i for i in range(10))   # 生成器 → 拒绝
x2: ArrayLike = A()                       # 无 __array__ 的裸类 → 拒绝
x3: ArrayLike = {1: "foo", 2: "bar"}      # dict → 拒绝
```

这正是公共 `ArrayLike` 与「运行时 `np.array`」最鲜明的分歧：运行时三者都能跑（多半产出 object 数组），类型系统却一律说不。

#### 4.4.4 代码实践

**目标**：亲手触发「类型系统拒绝、运行时却放行」的分歧，并验证两条逃生通道。

**步骤**（示例代码）：

```python
# 示例代码
from typing import Any
import numpy as np
import numpy.typing as npt

def make_array(x: npt.ArrayLike) -> np.ndarray:
    return np.asarray(x)

# (A) 生成器：运行时造出 object 数组，类型检查器应拒绝
g = (i * i for i in range(5))
# make_array(g)            # ← 类型检查器会在此报错

# 逃生通道 1：# type: ignore
print(make_array((i*i for i in range(5)) if False else g))  # type: ignore[arg-type]

# 逃生通道 2：先把变量标成 Any
anything: Any = (i * i for i in range(5))
print(make_array(anything))
```

**需要观察的现象**：

1. 不加 `# type: ignore` / 不标 `Any` 时，类型检查器对 `make_array(g)` 报「类型不兼容 / argument ... not assignable to `ArrayLike`」之类的错误；
2. 运行时（用 `python` 直接跑）却能正常打印出 `array([0, 1, 4, 9, 16], dtype=object)` 这类 object 数组。

**预期结果**：类型层拒绝、运行时通过——两者分歧正是设计意图。逃生通道 1/2 任选其一即可让类型检查器闭嘴。

> 若你手头没有 mypy/pyright，可以退化为「源码阅读型实践」：对照 `fail/array_like.pyi` 的三行，逐行解释「它为什么不满足 `ArrayLike` 的四块积木中任何一块」。

#### 4.4.5 小练习与答案

**练习 1**：`_DualArrayLike` 之所以叫 *Dual*，是因为它有两条「轨」。请分别说出这两条轨的名字与作用。

**参考答案**：DTypeT 轨（`_SupportsArray[DTypeT]` 及其嵌套）接纳「带 `__array__`、dtype 已知的对象」；BuiltinT 轨（`BuiltinT` 及其嵌套）接纳「内置标量」。两条轨各自独立参数化，让同一个引擎既能撑起最宽松的公共 `ArrayLike`，又能撑起收窄的 `_co` 别名。

**练习 2**：既然 NumPy 运行时支持 object 数组，为什么 `_ArrayLikeObject_co` 是**私有**的、而不并入公共 `ArrayLike`？

**参考答案**：因为造 object 数组几乎总是一个意外（性能差、语义容易出错）。把它放进公共 `ArrayLike` 等于鼓励这种写法，违背「类型系统应更严格」的设计取舍。NumPy 选择把 object 数组支持隔离到一个私有别名里，只让真正需要它的少数内部 API 使用，从而在公共面上把这种「合法但不推荐」的用法挡住。

**练习 3**：`_ArrayLikeInt_co` 为什么把 `BuiltinT` 设成 `int`，却能接受 `True`（布尔）？

**参考答案**：因为 `bool` 是 `int` 的子类型，`True` 在类型上可赋值给 `int`；同时它的 DTypeT 轨显式包含了 `np.bool | np.integer`。所以布尔值能从两条轨之一被接纳，符合「`same_kind` 转换规则下 bool 可安全转成整数」的事实。

---

## 5. 综合实践

把本讲四个最小模块串起来，完成下面这个迷你任务。

**任务背景**：你要给团队写一个工具函数 `normalize`，它接收「任何能转成数组的输入」，转成 `NDArray` 后做归一化。你希望类型注解既能「来者不拒」（接受 list/tuple/np 数组/标量），又能让类型检查器替你挡住「生成器造 object 数组」这类坑。

**要求**：

1. 用公共别名 `npt.ArrayLike` 作入参、`npt.NDArray` 作返回值，写出函数；
2. 为 4 种输入各写一行调用：`list`、`tuple`、`np.array(...)`、**生成器**；
3. 用 mypy 或 pyright 检查，记录哪几行被拒；
4. 对被拒的生成器调用，分别用「`# type: ignore`」和「`Any` 逃生」两种方式让它通过，并说明你更推荐哪一种、为什么。

**参考骨架**（示例代码）：

```python
# 示例代码
from typing import Any
import numpy as np
import numpy.typing as npt

def normalize(x: npt.ArrayLike) -> npt.NDArray[np.float64]:
    a = np.asarray(x, dtype=np.float64)
    return (a - a.min()) / (a.max() - a.min() + 1e-12)

normalize([1, 2, 3])               # list   → 放行
normalize((4, 5, 6))               # tuple  → 放行
normalize(np.array([7.0, 8.0]))    # array  → 放行
# normalize(x for x in range(3))   # 生成器 → 应被拒！

# 逃生通道（二选一）：
# normalize((x for x in range(3)), )   # type: ignore[arg-type]
g: Any = (x for x in range(3))
normalize(g)
```

**自检要点**：

- 能否说清生成器被拒的原因（四块积木无一命中）？
- `_SupportsArray` 与 `_NestedSequence` 在这个例子里分别承接了哪些输入？（提示：`np.array` 走 `_SupportsArray`；`list`/`tuple` 走 `_NestedSequence`。）
- 你写出的 `normalize` 是否真的「比运行时更严格」？这正是 `ArrayLike` 的设计初衷。

## 6. 本讲小结

- `ArrayLike` 是「能转成数组的对象」在类型层面的公共别名，顶层只有一行：`Buffer | _DualArrayLike[np.dtype, complex | bytes | str]`。
- 它由四块积木拼成：**缓冲区**、**带 `__array__` 的对象**（`_SupportsArray`）、**内置标量**（`complex|bytes|str`，其中 `complex` 隐含 `int/float/bool`）、以及让前三者「任意嵌套」的 **`_NestedSequence`**。
- `_SupportsArray` 是把运行时 `__array__` 鸭子协议翻译成类型的 `@runtime_checkable` 泛型协议，带 `DTypeT` 参数以传递 dtype 信息。
- `_NestedSequence` 用递归返回类型 `_T_co | _NestedSequence[_T_co]` 表达「任意深度嵌套」，是 `list`/`tuple`/多层嵌套得以被接纳的关键。
- `_DualArrayLike` 是带「DTypeT / BuiltinT」两条轨的参数化引擎，一个引擎同时服务最宽松的公共 `ArrayLike` 与最严格的内部 `_co` 收窄别名。
- 设计取舍：`ArrayLike` **刻意避开 object 数组**——生成器、裸对象、dict 在运行时能跑出 object 数组，却被类型系统拒绝；object 数组支持被隔离到私有的 `_ArrayLikeObject_co`，需要时可用 `# type: ignore` 或 `Any` 逃生。

## 7. 下一步学习建议

- **下一讲 [u2-l2 DTypeLike](u2-l2-dtypelike.md)**：本讲只解决了「什么是数组」，下一讲解决「什么是 dtype」。你会看到 `DTypeLike` 如何用 `_SupportsDType` 协议、`_char_codes` 字符串编码来描述「能转成 dtype 的对象」，并把「字段字典 dtype」这一合法但不推荐的写法同样挡在门外——与本章「避开 object 数组」是同一套设计哲学。
- **横向到 [u3 协议单元](u3-l1-supportsarray-protocols.md)**：本讲里 `_SupportsArray` 用了 PEP 695 新式泛型协议语法，而 `_NestedSequence` 用了 `Protocol[_T_co]` 旧式语法。u3 会系统对比这两种写法，并深入 `__array_function__` 协议（本讲只点名的 `_SupportsArrayFunc`）。
- **想动手验证**：装好 mypy（`pip install mypy`）后，把本讲的示例脚本与 `tests/data/{pass,fail}/array_like.*` 一起跑一遍——`fail` 夹具里每一条 `# type: ignore` 都是一道理解 `ArrayLike` 边界的练习题。
