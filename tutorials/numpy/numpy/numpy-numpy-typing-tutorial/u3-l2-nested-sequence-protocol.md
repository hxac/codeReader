# `_NestedSequence`：嵌套序列协议

## 1. 本讲目标

学完本讲，你应当能够：

- 读懂 `_NestedSequence` 这个 `@runtime_checkable` 协议，并解释它如何用**递归类型**（在 `__getitem__` / `__iter__` 的返回类型里出现 `_T_co | _NestedSequence[_T_co]`）刻画「任意深度嵌套的序列」——为什么 `list[int]`、`list[list[int]]`、`list[list[list[int]]]` 都能匹配同一个 `_NestedSequence[int]`。
- 说出**协变（covariant）TypeVar `_T_co`** 的含义：为什么 `_T_co` 只能出现在「输出位置」，以及协变所满足的子类型规则，理解 `_co` 后缀的约定。
- 区分 NumPy 类型系统里并存的**两种泛型协议语法**：旧式 `class _NestedSequence(Protocol[_T_co])`（本讲主角）与新式 `class _SupportsArray[DTypeT: np.dtype](Protocol)`（PEP 695，见上一篇），并能指出它们的等价关系。
- 读懂 [`numpy/_typing/_nested_sequence.py`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_nested_sequence.py) 开头那段 `if TYPE_CHECKING: ... else: ...` 的「双分支定义同一个 `_T_co`」技巧：为什么类型检查器看到一个带 `default=Any`（PEP 696）的 `typing_extensions.TypeVar`，而运行时却看到一个不带 `default` 的 `typing.TypeVar`。

## 2. 前置知识

本讲承接 [u2-l1（ArrayLike）](u2-l1-arraylike.md) 与 [u3-l1（`_SupportsArray` / `_SupportsArrayFunc`）](u3-l1-supportsarray-protocols.md)。你已经知道：

- `ArrayLike = Buffer | _DualArrayLike[np.dtype, complex | bytes | str]`，其中 `_DualArrayLike` 把 `_SupportsArray`、`_NestedSequence`、若干内置标量用 `|` 拼到一起；
- `_SupportsArray` 刻画「带 `__array__` 的对象」，`_NestedSequence` 刻画「任意深度嵌套序列」，二者都是**协议（Protocol）**；
- `Protocol`（PEP 544）提供**结构子类型**——按方法「形状」匹配，无需继承；`@runtime_checkable` 打开运行时 `isinstance` 开关，但运行时检查很「浅」。

本讲需要补充四个基础概念：

1. **递归类型（recursive type）**
   一个类型的定义里**引用了自己**。例如「一棵树要么是叶子、要么是一棵更小的树」。静态类型系统支持这种自引用，只要递归出现在「返回类型」这样的**协变位置**，就能描述任意深度结构。`_NestedSequence` 正是用它来表达「嵌套可深可浅」。

2. **变型（variance）：协变 / 逆变 / 不变**
   一个泛型容器 `C[T]`，当 `B` 是 `A` 的子类型时，`C[B]` 与 `C[A]` 的子类型关系由「变型」决定：

   | 变型 | 关系 | 典型场景 |
   | --- | --- | --- |
   | 协变（covariant） | \(B <: A \Rightarrow C[B] <: C[A]\) | `T` 只出现在**输出**位置（如返回类型） |
   | 逆变（contravariant） | \(B <: A \Rightarrow C[A] <: C[B]\) | `T` 只出现在**输入**位置（如参数） |
   | 不变（invariant） | 无关系 | `T` 同时出现在输入与输出（如可变容器 `list[T]`） |

   `TypeVar("_T_co", covariant=True)` 显式声明一个**协变**类型变量，约定名以 `_co` 结尾。

3. **泛型协议的两种写法**
   - **旧式（PEP 544 / 312 + TypeVar）**：先定义 `T_co = TypeVar("T_co", covariant=True)`，再 `class Proto(Protocol[T_co]): ...`——把 TypeVar 当「参数」塞进 `Protocol[...]`。
   - **新式（PEP 695，Python 3.12+）**：`class Proto[T_co](Protocol): ...`——方括号里直接声明类型参数，无需单独 `TypeVar`。
   两者等价。NumPy 在 `_nested_sequence.py` 用旧式，在 `_array_like.py` 用新式，并存对照。

4. **`TYPE_CHECKING`（PEP 563 的一部分）**
   `typing.TYPE_CHECKING` 在**运行时恒为 `False`**，只有**类型检查器**（mypy / pyright）分析代码时才把它当作 `True`。利用这一点，可以把「只有类型检查才需要、运行时不需要」的导入放进 `if TYPE_CHECKING:` 分支，避免运行时开销或运行时依赖。

> 提示：本讲的主角 `_NestedSequence` 定义在私有文件 `_nested_sequence.py`，由 `numpy._typing` 聚合后，被 `ArrayLike`（经 `_DualArrayLike`）和若干 `.pyi` 桩文件消费。它是私有 API，但理解它是理解「为什么 `np.array([[1,2],[3,4]])` 这种任意嵌套输入能被精确标注」的关键。

## 3. 本讲源码地图

本讲只盯住「一个递归协议 + 它的类型参数定义」这一件事，涉及的真实文件如下：

| 文件 | 角色 | 说明 |
| --- | --- | --- |
| `numpy/_typing/_nested_sequence.py` | **主战场** | 定义 `_NestedSequence` 协议（递归返回类型）与协变 `_T_co`，并用 `TYPE_CHECKING` 双分支处理 `default=Any`。 |
| `numpy/_typing/_array_like.py` | 消费方 | `_ArrayLike` / `_DualArrayLike` 把 `_NestedSequence[...]` 当作描述「嵌套输入」的那块拼图。 |
| `numpy/linalg/_linalg.pyi` | 消费方 | 用 `_NestedSequence` 拼出 `_Sequence2ND` / `_Sequence3ND` / `_Sequence4ND`，描述 2/3/4 维嵌套序列。 |
| `numpy/_core/records.pyi` | 消费方 | `np.recarray.fromrecords` 的 `recList` 用 `_NestedSequence[tuple[object, ...]]` 描述「装着记录的嵌套列表」。 |
| `numpy/typing/tests/data/reveal/nested_sequence.pyi` | 正例 | `assert_type` 断言哪些类型匹配 `_NestedSequence[int]`（含 `list`、`tuple`、`range`、多层 `Sequence`）。 |
| `numpy/typing/tests/data/fail/nested_sequence.pyi` | 反例 | `reveal_type` 断言哪些类型**不**匹配 `_NestedSequence[int]`（如 `Sequence[float]`、`list[complex]`、`int`、`str`）。 |
| `numpy/typing/tests/test_runtime.py` | 运行时测试 | 断言 `isinstance([1], _NestedSequence)` 为真——`@runtime_checkable` 的活证据。 |

> 注意路径：所有定义都在**私有** `numpy/_typing/` 下；公共壳 `numpy/typing/` 不导出 `_NestedSequence`。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：`_NestedSequence`（递归类型主体）、协变 TypeVar `_T_co`、旧式 `Protocol[_T_co]` 泛型协议语法、`TYPE_CHECKING` 双分支与 `default=Any`。

### 4.1 `_NestedSequence`：用递归类型刻画任意深度嵌套

#### 4.1.1 概念说明

`np.array(...)` 能接受 `[[1.0]]`、`[[[1.0]]]`、`[[[[1.0]]]]`……嵌套多深都行。运行时这不成问题——解释器一层层拆开列表即可。但**静态类型**怎么表达「嵌套深度未知，但叶子都是 float」？

答案就是 `_NestedSequence`：它是一个协议，规定「你有 `__len__`、`__getitem__`、`__contains__`、`__iter__` …… 这些序列方法」。关键巧思在返回类型上：取一个元素，返回的**既可能是叶子，也可能是更深一层同样的嵌套序列**。这种「定义里引用自己」的写法就是**递归类型**，它让一个类型同时匹配 1 层、2 层、任意层嵌套。

#### 4.1.2 核心流程

把 `_NestedSequence[T]` 的语义写成数学化的递归定义（其中 \(\text{Seq}(\cdot)\) 表示「元素类型为……的序列」）：

\[
\text{Nested}[T] \;\triangleq\; \text{Seq}\bigl(\, T \;\cup\; \text{Nested}[T] \,\bigr)
\]

读作：「一个 `Nested[T]` 是一个序列，它的每个元素**要么**是一个 `T` 叶子，**要么**是另一个 `Nested[T]`」。这个递归方程的「不动点」就是任意深度嵌套。

匹配过程（以 `list[list[int]]` 对照 `_NestedSequence[int]` 为例）：

```
list[list[int]]
  └─ __getitem__(int) 返回 list[int]
        └─ list[int] 是不是一个「元素为 int | Nested[int] 的序列」？
              list[int].__getitem__(int) 返回 int  ✓（命中 T 分支）
        ⇒ list[int] 满足 Nested[int]  ⇒ list[list[int]] 满足 Nested[int]
```

无论外面套多少层 `list`，每一层都靠「返回值要么是叶子、要么是更深 `Nested`」这条规则被吸纳。1 层 `list[int]` 命中叶子分支终止；多层则逐层命中「更深 `Nested`」分支，最终在叶子处终止。

#### 4.1.3 源码精读

递归的「机关」藏在 `__getitem__`、`__iter__`、`__reversed__` 的**返回类型**里：

[`numpy/_typing/_nested_sequence.py:63-L65`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_nested_sequence.py#L63-L65) 定义 `__getitem__`，返回 `"_T_co | _NestedSequence[_T_co]"`——这就是 4.1.2 公式里 \(T \cup \text{Nested}[T]\) 的直接翻译：取一个元素，要么是叶子 `_T_co`，要么是更深一层 `_NestedSequence[_T_co]`。

[`numpy/_typing/_nested_sequence.py:71-L77`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_nested_sequence.py#L71-L77) 的 `__iter__` / `__reversed__` 返回 `"Iterator[_T_co | _NestedSequence[_T_co]]"`，同样的递归形状——迭代出的每个元素也是「叶子或更深嵌套」。

协议其余方法不参与递归，只是凑齐「序列」的标准形状：[`__len__ -> int`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_nested_sequence.py#L59-L61)、[`__contains__(x: object) -> bool`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_nested_sequence.py#L67-L69)、[`count` / `index`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_nested_sequence.py#L79-L85)。

这套设计被正例夹具固化成断言：

[`numpy/typing/tests/data/reveal/nested_sequence.pyi:6-L25`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/reveal/nested_sequence.pyi#L6-L25) 对 `func(a: _NestedSequence[int])` 依次传入 `Sequence[int]`、`Sequence[Sequence[int]]`、三层、四层嵌套、`tuple[int, ...]`、`list[int]`、`Sequence[Any]`、甚至 `range(15)`，全部 `assert_type(..., None)` 通过——证明任意深度嵌套（以及 `tuple` / `list` / `range` 这些序列）都结构匹配 `_NestedSequence[int]`。

反例夹具则划清边界：

[`numpy/typing/tests/data/fail/nested_sequence.pyi:5-L17`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/fail/nested_sequence.pyi#L5-L17) 对同样的 `func` 传入 `Sequence[float]`、`list[complex]`、`tuple[str, ...]`、`int`、`str`，每行都标 `# type: ignore[arg-type, misc]`——说明叶子类型不对（float/complex/str ≠ int）、或根本不是序列（`int`）时，类型检查器**拒绝**。`str` 尤其值得注意：`str` 在运行时是「字符的序列」，但它被当作 `_NestedSequence[int]` 仍报错，体现「类型比运行时更严格」。

#### 4.1.4 代码实践

1. **目标**：写一个接受 `_NestedSequence[float]` 的函数，分别传入 1/2/3/4 层嵌套的 `list`，观察运行时 `np.asarray(...).dtype` 与静态 `reveal_locals()` 的推断结果。
2. **操作步骤**（直接复刻源码 docstring 里的官方示例）：
   ```python
   # pip install numpy mypy
   from typing import TYPE_CHECKING
   import numpy as np
   from numpy._typing import _NestedSequence   # 私有，仅供学习

   def get_dtype(seq: _NestedSequence[float]) -> np.dtype[np.float64]:
       return np.asarray(seq).dtype

   a = get_dtype([1.0])
   b = get_dtype([[1.0]])
   c = get_dtype([[[1.0]]])
   d = get_dtype([[[[1.0]]]])

   print(a, b, c, d)   # 运行时：都是 float64

   if TYPE_CHECKING:
       reveal_locals()  # 静态：交给 mypy 看
   ```
3. **需要观察的现象**：
   - 运行时四个 `print` 是否都打印 `float64`（无论嵌套多深）。
   - 把脚本交给 `mypy --strict your_script.py`，看 `reveal_locals()` 输出的 `a/b/c/d` 类型。
4. **预期结果**：
   - 运行时：四行均为 `float64`。
   - 静态（mypy）揭示的局部类型与源码 docstring 记录的一致（见 [`_nested_sequence.py:52-L55`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_nested_sequence.py#L52-L55)）：`a/b/c/d` 全是 `numpy.dtype[numpy.floating[numpy._typing._64Bit]]`，**与嵌套深度无关**——这正是递归类型的威力：四层嵌套和一层嵌套在类型上「坍缩」成同一个 `_NestedSequence[float]`。
5. 若你传 `_NestedSequence[float]` 以外的东西（如 `[1.0j]`，叶子是 complex），mypy 会报 `arg-type`——这与反例夹具的判定一致；不同 mypy 版本措辞可能略异，标注「待本地验证」并记录版本。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `list[list[list[int]]]` 能匹配 `_NestedSequence[int]`？请用 4.1.2 的递归规则说清楚每一层。
**答案**：最内层 `list[int]` 的 `__getitem__` 返回 `int`，命中叶子分支 `T=int`，故 `list[int]` 满足 `Nested[int]`；进而 `list[list[int]]` 的 `__getitem__` 返回 `list[int]`（满足 `Nested[int]`），命中「更深 `Nested`」分支，故它也满足 `Nested[int]`；再套一层同理。每层都靠「返回值是叶子或更深 Nested」被吸纳。

**练习 2**：`np.array("abc")` 运行时是长度为 3 的字符串数组（`str` 是「字符序列」）。为什么 `str` 仍不被 `_NestedSequence[int]` 接受？
**答案**：`str.__getitem__(int)` 返回的是 `str`（单字符也是 `str`），叶子不是 `int`，结构上不匹配 `_NestedSequence[int]`。这再次体现「类型比运行时更严格」：运行时把 `str` 当序列，但静态类型按叶子类型严格判定。

---

### 4.2 协变 TypeVar `_T_co`：为什么是协变

#### 4.2.1 概念说明

`_NestedSequence` 是带类型参数的泛型协议：`_NestedSequence[int]`、`_NestedSequence[float]` 各是不同类型。这个参数 `_T_co` 被声明为**协变（covariant）**——名字里的 `_co` 就是这个意思。

为什么必须协变？因为 `_T_co` 在协议里**只出现在输出位置**（`__getitem__` / `__iter__` / `__reversed__` 的返回类型）。一个「只产出 `T`、从不消费 `T`」的容器，天然满足协变：能产出更具体类型的地方，当然也能被当作「产出更宽泛类型」的地方使用。

#### 4.2.2 核心流程

协变的子类型规则为：

\[
B <: A \;\Longrightarrow\; \text{Nested}[B] <: \text{Nested}[A]
\]

即「叶子类型更窄，整个嵌套序列类型也更窄」。例如 `bool <: int`（Python 类型视角下布尔是整数的子类型），所以 `_NestedSequence[bool] <: _NestedSequence[int]`——一个「嵌套布尔列表」可以被安全地当作「嵌套整数列表」使用（产出布尔的地方当然产出了整数）。

判定协变是否合法的关键，是检查 `_T_co` 出现的**位置**：

| `_T_co` 出现位置 | 合法变型 | 违反时的 mypy 报错 |
| --- | --- | --- |
| 仅返回类型（输出） | 协变 ✓ | — |
| 仅参数类型（输入） | 逆变 | `covariant type variable used in contravariant position` |
| 输入 + 输出 | 不变 | 同上 |

`_NestedSequence` 的所有 `_T_co` 都在返回类型里（见 4.1.3），其余方法的「输入」用的是 `object` / `Any` 而非 `_T_co`（如 `__contains__(self, x: object, /)`、`count(self, value: Any, /)`）——这正是为了让 `_T_co` 保持在「纯输出」位置，从而协变合法。

#### 4.2.3 源码精读

`_T_co` 的协变声明就在文件开头：

[`numpy/_typing/_nested_sequence.py:9`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_nested_sequence.py#L9) 的 `TypeVar("_T_co", covariant=True, default=Any)`——`covariant=True` 就是协变声明（`default=Any` 见 4.4）。

核对「纯输出」约束：[`__contains__(self, x: object, /)`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_nested_sequence.py#L67-L69)、[`count(self, value: Any, /)`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_nested_sequence.py#L79-L81)、[`index(self, value: Any, /)`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_nested_sequence.py#L83-L85) 这几个「输入」方法的参数都不是 `_T_co`，而是 `object` / `Any`。试想若 `__contains__` 写成 `__contains__(self, x: _T_co)`，`_T_co` 就同时出现在输入位置，协变立刻非法——NumPy 用 `object` / `Any` 规避了这一点。

> 旁证：正例夹具 [`reveal/nested_sequence.pyi:10`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/reveal/nested_sequence.pyi#L10) 让 `e: Sequence[bool]` 也匹配 `_NestedSequence[int]`，这正是协变的体现——`bool` 比 `int` 窄，`Nested[bool]` 可被当作 `Nested[int]`。

#### 4.2.4 代码实践

1. **目标**：亲手验证协变规则——`_NestedSequence[bool]` 是否能被当作 `_NestedSequence[int]` 使用。
2. **操作步骤**（示例代码，非项目原有代码）：
   ```python
   from typing import TYPE_CHECKING, assert_type
   from collections.abc import Sequence
   from numpy._typing import _NestedSequence

   def wants_int_nested(s: _NestedSequence[int]) -> None: ...

   bool_nested: Sequence[Sequence[bool]] = [[True, False]]
   wants_int_nested(bool_nested)   # 协变：bool <: int ⇒ Nested[bool] <: Nested[int]

   if TYPE_CHECKING:
       reveal_type(wants_int_nested(bool_nested))  # 期望: None（调用合法）
   ```
3. **需要观察的现象**：`mypy --strict` 是否对 `wants_int_nested(bool_nested)` 报错。
4. **预期结果**：**不报错**。因为 `bool <: int` 且 `_NestedSequence` 协变，`_NestedSequence[bool]` 是 `_NestedSequence[int]` 的子类型，调用合法。
5. 把 `bool_nested` 换成 `Sequence[Sequence[str]]` 再试，mypy 应报 `arg-type`（`str` 不是 `int` 的子类型）。若你的检查器行为与此不符，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `__contains__` 的签名从 `(self, x: object, /)` 改成 `(self, x: _T_co, /)`，mypy 会怎么报？为什么？
**答案**：mypy 会报 `covariant type variable "_T_co" used in contravariant position`。因为参数是「输入」位置，协变 TypeVar 出现在输入位置即非法。NumPy 用 `object` 而非 `_T_co` 正是为了保住协变。

**练习 2**：`_NestedSequence[bool] <: _NestedSequence[int]` 成立的依据是什么？反过来 `_NestedSequence[int] <: _NestedSequence[bool]` 成立吗？
**答案**：依据是协变规则 \(B <: A \Rightarrow \text{Nested}[B] <: \text{Nested}[A]\) 与 `bool <: int`。反过来不成立——一个「可能产出任意 int」的序列不能保证产出的都是 `bool`。

---

### 4.3 旧式 `Protocol[_T_co]`：泛型协议的经典语法

#### 4.3.1 概念说明

`_NestedSequence` 是一个**泛型协议**——它能带类型参数。Python 表达「泛型协议」有两套等价语法，NumPy 代码库两种都在用：

- **旧式**：`class _NestedSequence(Protocol[_T_co])`——先把协变 TypeVar `_T_co` 定义在模块级，再把它当「参数」塞进基类 `Protocol[...]` 的方括号里。
- **新式（PEP 695）**：`class _SupportsArray[DTypeT: np.dtype](Protocol)`——方括号直接写在类名后，类型参数及其约束（上界）一并声明，无需单独 `TypeVar`。

本讲主角 `_NestedSequence` 用旧式；上一篇的主角 `_SupportsArray` 用新式。理解时只要知道「两者都是泛型协议」即可，但能互译很有用。

#### 4.3.2 核心流程

新旧语法的对应关系：

```
# 旧式（PEP 544，本文件用法）
_T_co = TypeVar("_T_co", covariant=True)
@runtime_checkable
class _NestedSequence(Protocol[_T_co]):
    def __getitem__(self, index: int, /) -> "_T_co | _NestedSequence[_T_co]": ...

# 等价的新式（PEP 695，_SupportsArray 的写法）
@runtime_checkable
class _NestedSequence[_T_co](Protocol):     # covariant 需在 PEP 695 里用=_co 约定/变型标记
    def __getitem__(self, index: int, /) -> "_T_co | _NestedSequence[_T_co]": ...
```

要点：

- 旧式必须先有一个**模块级 `TypeVar`**（`_T_co`），再 `Protocol[_T_co]`；新式把声明内联进类头。
- 旧式通过 `TypeVar(..., covariant=True)` 表达变型；新式用专门的变型语法。
- `_NestedSequence` 内部用**字符串注解**（`"_T_co | _NestedSequence[_T_co]"`）实现递归——因为类体在执行时 `_NestedSequence` 这个名字尚未绑定完成，字符串延迟到类型检查时才解析，规避了「自引用尚未定义」的问题。

#### 4.3.3 源码精读

[`numpy/_typing/_nested_sequence.py:19-L20`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_nested_sequence.py#L19-L20) 用旧式 `@runtime_checkable` + `class _NestedSequence(Protocol[_T_co])` 声明协议。`Protocol[_T_co]` 里的 `_T_co` 就是 [`L9`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_nested_sequence.py#L9) / [`L13`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_nested_sequence.py#L13) 定义的协变 TypeVar。

对照新式写法（上一篇已读）：[`numpy/_typing/_array_like.py:22-L24`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_array_like.py#L22-L24) 的 `class _SupportsArray[DTypeT: np.dtype](Protocol)`——同样的「泛型协议」，只是把类型参数 `DTypeT`（带上界 `np.dtype`）直接写进类名后的方括号。两种写法在类型检查器眼里**完全等价**，项目并存只是历史/风格原因。

注意 `Protocol` 与 `runtime_checkable` 是**无条件导入**的（见 [`L3`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_nested_sequence.py#L3)），因为 `class _NestedSequence(Protocol[_T_co]):` 这一行在**运行时真的要执行**（要创建类对象），必须运行时可用。这与 4.4 里「只供类型检查的 `Iterator` 放进 `TYPE_CHECKING`」形成对照。

#### 4.3.4 代码实践

1. **目标**：用**旧式** `Protocol[_T_co]` 语法手写一个最小泛型协议，确认它与「直觉」一致。
2. **操作步骤**（示例代码）：
   ```python
   from typing import Protocol, TypeVar, runtime_checkable

   T_co = TypeVar("T_co", covariant=True)

   @runtime_checkable
   class MyBox(Protocol[T_co]):
       def get(self) -> T_co: ...

   class IntBox:
       def get(self) -> int:
           return 1

   print("isinstance:", isinstance(IntBox(), MyBox))   # 结构匹配：有 get()->int
   ```
3. **需要观察的现象**：`isinstance` 是否为 `True`。
4. **预期结果**：`True`。`IntBox` 没有继承 `MyBox`，但拥有签名兼容的 `get` 方法，结构上满足 `MyBox[int]`；`@runtime_checkable` 让 `isinstance` 可用（运行时只看成员名 `get` 是否存在）。
5. 把脚本交给 mypy，标注 `box: MyBox[int] = IntBox()` 应无错误；若改 `IntBox.get` 返回 `str`，mypy 会认为它不再满足 `MyBox[int]`。不同检查器版本对旧式 `Protocol[T_co]` 的报告一致性好，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：把 `_NestedSequence` 的旧式定义 `class _NestedSequence(Protocol[_T_co])` 改写成等价的 PEP 695 新式，应该长什么样？
**答案**：`class _NestedSequence[T_co](Protocol):`（PEP 695 把类型参数挪到类名后的方括号，并移除模块级 `TypeVar`；协变在新语法里有对应的变型声明方式）。两者类型等价。

**练习 2**：为什么 `__getitem__` 的返回类型写成**字符串** `"_T_co | _NestedSequence[_T_co]"`，而不是直接 `_T_co | _NestedSequence[_T_co]`（不带引号）？
**答案**：类体执行到 `__getitem__` 时，`_NestedSequence` 这个名字还没绑定完成（类正在定义中）。用字符串注解可把解析推迟到类型检查阶段（此时类已定义），从而合法地实现「返回类型里引用自己」的递归。直接写不带引号会在运行时触发 `NameError`。

---

### 4.4 `TYPE_CHECKING` 双分支与 `default=Any`

#### 4.4.1 概念说明

文件开头有一段看似奇怪的代码：用 `if TYPE_CHECKING: ... else: ...` **定义了两次同一个 `_T_co`**，两份还略有不同——类型检查器看到的是 `typing_extensions.TypeVar("_T_co", covariant=True, default=Any)`，运行时看到的是 `typing.TypeVar("_T_co", covariant=True)`（**没有** `default`）。

这段代码同时解决三件事：

1. **PEP 696 默认值 `default=Any`**：让「裸用」`_NestedSequence`（不带类型参数）等价于 `_NestedSequence[Any]`，避免「缺少类型参数」报错。
2. **运行时不依赖 `typing_extensions`**：`typing_extensions` 是第三方包，NumPy 运行时不想强依赖它；类型检查时却需要它来「回填」PEP 696 支持。
3. **运行时兼容老 Python**：标准库 `typing.TypeVar` 直到 Python 3.13 才接受 `default` 关键字；运行时分支去掉 `default` 即可在老版本上正常构造 TypeVar，而 `default` 在运行时本就无意义（它只影响静态推导）。

#### 4.4.2 核心流程

`TYPE_CHECKING` 的取值决定哪一分支「生效」：

```
                ┌─────────────────────────┐
类型检查器视角   │  TYPE_CHECKING == True   │
(mypy/pyright)  │  _T_co = typing_extensions.TypeVar(
                │      "_T_co", covariant=True, default=Any)   ← 带 PEP 696 默认值
                └─────────────────────────┘
                ┌─────────────────────────┐
运行时视角       │  TYPE_CHECKING == False  │
(CPython 执行)  │  _T_co = typing.TypeVar(
                │      "_T_co", covariant=True)               ← 不带 default，兼容老版本
                └─────────────────────────┘
```

两个分支产出的 `_T_co` 在「运行时行为」上**没有差别**（`default` 不影响运行时），但在「静态语义」上有差别——只有带 `default=Any` 的那份，类型检查器才认 `_NestedSequence`（裸用）合法。

同一段 `if TYPE_CHECKING:` 里还顺带导入了 `Iterator`：

```python
if TYPE_CHECKING:
    from collections.abc import Iterator
```

因为 `Iterator` 只出现在 [`__iter__` / `__reversed__`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_nested_sequence.py#L71-L77) 的**字符串**返回注解里（`"Iterator[...]"`），运行时从不被求值，所以只在类型检查时导入即可，省去一次运行时导入。

#### 4.4.3 源码精读

[`numpy/_typing/_nested_sequence.py:5-L13`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_nested_sequence.py#L5-L13) 是整段双分支：

- `if TYPE_CHECKING:`（L5–L9）：导入 `Iterator` 与 `typing_extensions.TypeVar`，构造**带 `default=Any`** 的 `_T_co`——这是 PEP 696（类型参数默认值）的产物。
- `else:`（L10–L13）：运行时用标准库 `typing.TypeVar` 构造**不带 `default`** 的 `_T_co`——保证在 Python 3.13 之前的运行时也能正常 `TypeVar("_T_co", covariant=True)`（老版本不认 `default` 关键字）。

`Any` 则在 [`L3`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_nested_sequence.py#L3) 无条件导入（`default=Any` 只在类型检查分支用到，但无条件导入 `Any` 无害且廉价）。

这条「双分支定义同名对象」的手法，本质是利用 `TYPE_CHECKING` 让**静态侧与运行时侧看到不同的定义**——差异只在静态语义上有效，运行时毫无影响。它是本包「既要在老运行时跑、又要支持最新类型特性」这一工程约束的典型解法（与 [u5-l1（.py / .pyi 双轨制）](u5-l1-py-pyi-dual-track.md) 的「让检查器与运行时看到不同内容」思路一脉相承）。

#### 4.4.4 代码实践

1. **目标**：验证 `TYPE_CHECKING` 在运行时恒为 `False`，并理解「裸用 `_NestedSequence`」靠 `default=Any` 才合法。
2. **操作步骤**（示例代码）：
   ```python
   from typing import TYPE_CHECKING
   import numpy._typing._nested_sequence as ns

   print("TYPE_CHECKING (runtime):", TYPE_CHECKING)   # 运行时恒为 False
   print("_T_co:", ns._T_co)                            # 运行时构造的那份（无 default）
   ```
   再写一个 `.pyi` 风格的静态检查片段（示例代码，交给 mypy）：
   ```python
   from numpy._typing import _NestedSequence

   def f(x: _NestedSequence) -> None: ...   # 裸用，不带类型参数
   ```
3. **需要观察的现象**：
   - 运行时脚本打印 `TYPE_CHECKING (runtime): False`。
   - 把 `.pyi` 片段交给 `mypy --strict`，看 `f(x: _NestedSequence)`（裸用）是否报「缺少类型参数」。
4. **预期结果**：运行时 `TYPE_CHECKING` 为 `False`；静态侧因为类型检查器读到的是带 `default=Any` 的定义，**裸用 `_NestedSequence` 合法**（等价于 `_NestedSequence[Any]`）。若你在运行时手动 `TypeVar("X", covariant=True, default=Any)` 且 Python < 3.13，会抛 `TypeError`——这正是运行时分支去掉 `default` 的原因。
5. 不同 mypy / pyright 版本对 PEP 696 的支持程度不同；若裸用仍被警告，标注「待本地验证」并升级 mypy。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `typing_extensions.TypeVar` 只在 `if TYPE_CHECKING:` 里导入，而运行时用 `typing.TypeVar`？
**答案**：`typing_extensions` 是第三方包，NumPy 不希望它成为运行时依赖；同时标准库 `typing.TypeVar` 在 Python 3.13 前不接受 `default` 关键字。类型检查器需要 PEP 696 的 `default=Any`（由 `typing_extensions` 回填），运行时既不需要 `default`、也不能在老版本上传它，故分两支。

**练习 2**：把运行时分支改成 `_T_co = TypeVar("_T_co", covariant=True, default=Any)`，在 Python 3.12 上运行会怎样？
**答案**：会抛 `TypeError`（标准库 `typing.TypeVar` 在 3.13 前不接受 `default`）。这就是运行时分支刻意去掉 `default` 的原因——`default` 只对静态推导有意义，运行时省略它既兼容老版本又不改变行为。

---

## 5. 综合实践

把本讲四件事——递归类型、协变 `_T_co`、旧式 `Protocol[_T_co]` 语法、`TYPE_CHECKING` 双分支——串成一个任务。

**任务：自己实现一个 `MyNested[T]` 协议，复刻 `_NestedSequence` 的核心设计，并验证它在三个层面的行为。**

要求：

1. 用**旧式**语法定义一个协变 TypeVar 与协议：
   ```python
   from typing import Protocol, TypeVar, runtime_checkable, TYPE_CHECKING

   T_co = TypeVar("T_co", covariant=True)

   @runtime_checkable
   class MyNested(Protocol[T_co]):
       def __getitem__(self, index: int, /) -> "T_co | MyNested[T_co]": ...
   ```
2. 写一个递归函数 `def first_leaf(s: "MyNested[float]") -> float`，不断 `s[0]` 直到拿到 `float`（运行时用 `isinstance(s[0], (list, tuple))` 判断是否继续下钻；注意它**不**用协议本身判断）。
3. 分别传入 `[1.0, 2.0]`、`[[3.0], [4.0]]`、`[[[5.0]]]`，确认运行时都返回正确的最内层 `float`。
4. 验证协变：把一个 `MyNested[bool]` 的值（如 `[[True]]`）传给一个期望 `MyNested[int]` 的函数，mypy 是否放行？用一句话解释依据（结合 4.2 的规则）。
5. 触发源码 docstring 记录的「警告」：写 `def bad[T](s: MyNested[T]) -> T: ...`（函数级 TypeVar 搭配协议），观察 mypy 是否报错，并对照 [`_nested_sequence.py:23-L26`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_nested_sequence.py#L23-L26) 的 Warning 说明原因。
6. 在结论里用一句话回答：`TYPE_CHECKING` 在你这个脚本的**运行时**取值是什么？为什么 `MyNested` 仍能在运行时被 `isinstance` 使用？

预期：步骤 3 返回 `1.0` / `3.0` / `5.0`；步骤 4 放行（协变 + `bool <: int`）；步骤 5 报错或无法正确推导（协议目前不支持把叶子类型沿函数级 TypeVar 传出，这是递归协议的已知限制）；步骤 6 `TYPE_CHECKING` 运行时为 `False`，但 `@runtime_checkable` 让 `MyNested` 类本身在运行时已创建，故 `isinstance` 可用。若某些步骤的静态行为与预期不符，记录 mypy / pyright 版本并标注「待本地验证」。这个任务把「递归类型如何刻画嵌套、协变如何放宽子类型、双语法如何互译、TYPE_CHECKING 如何让静态与运行时分家」从概念落到了可运行、可类型检查的代码上。

## 6. 本讲小结

- `_NestedSequence` 用**递归类型**刻画任意深度嵌套：`__getitem__` / `__iter__` / `__reversed__` 的返回类型里出现 `_T_co | _NestedSequence[_T_co]`，使 `list[int]`、`list[list[int]]`、任意层嵌套都坍缩成同一个 `_NestedSequence[int]`。
- 协变 TypeVar `_T_co`（`covariant=True`）成立的前提是它**只出现在输出位置**；NumPy 把 `__contains__` / `count` / `index` 的输入参数写成 `object` / `Any` 而非 `_T_co`，正是为保住协变。
- 协变规则 \(B <: A \Rightarrow \text{Nested}[B] <: \text{Nested}[A]\) 让 `bool` 叶子序列能被当作 `int` 叶子序列使用，由正例夹具 `Sequence[bool]` 匹配 `_NestedSequence[int]` 印证。
- 泛型协议有新旧两套等价语法：本讲 `_NestedSequence` 用旧式 `class _NestedSequence(Protocol[_T_co])`（+ 模块级 `TypeVar`），上一篇 `_SupportsArray` 用新式 `class _SupportsArray[DTypeT: np.dtype](Protocol)`（PEP 695），项目并存。
- `if TYPE_CHECKING: ... else: ...` 让静态侧看到带 `default=Any`（PEP 696）的 `typing_extensions.TypeVar`、运行时看到不带 `default` 的 `typing.TypeVar`，兼顾「PEP 696 裸用合法」「运行时不依赖 typing_extensions」「老 Python 兼容」三重约束。
- 已知限制：`_NestedSequence` 目前**不能**与函数级 TypeVar 搭配（`def f(a: _NestedSequence[T]) -> T`），无法把叶子类型沿函数传出——源码 docstring 的 Warning 明确告警。

## 7. 下一步学习建议

- 下一篇 [u3-l3（`_SupportsDType` 与 `_HasDType` 协议）](u3-l3-supportsdtype-protocols.md) 把同样的「协议化鸭子类型」思路迁移到 dtype 侧：用 `_HasDType` / `_HasNumPyDType` 描述「带 dtype 属性的对象」，支撑 `DTypeLike`。
- 想看 `_NestedSequence` 在真实 API 里如何被消费，阅读 [`numpy/_typing/_array_like.py`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_array_like.py)（`_DualArrayLike` 里两处 `_NestedSequence[...]`）、[`numpy/linalg/_linalg.pyi`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/linalg/_linalg.pyi) 的 `_Sequence2ND/3ND/4ND` 与 [`numpy/_core/records.pyi`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.pyi) 的 `fromrecords`。
- 想深入「静态 vs 运行时双轨」，可接着读 [u5-l1（.py / .pyi 双轨制）](u5-l1-py-pyi-dual-track.md)，理解类型检查器读 `.pyi`、运行时跑 `.py` 的全局机制——本讲的 `TYPE_CHECKING` 双分支是同一思想在单文件内的微缩版。
- 想亲手跑这些类型断言，阅读 [u6-l1（静态类型测试方法论）](u6-l1-static-typing-test-methodology.md) 了解 `reveal` / `fail` / `pass` 夹具如何被 mypy 自动校验——本讲引用的 `reveal/nested_sequence.pyi` 与 `fail/nested_sequence.pyi` 正是其中一员。
