# 综合实战：为自定义数组函数编写并测试类型注解

## 1. 本讲目标

本讲是手册的收官篇。前面十几讲我们分别拆解了 `ArrayLike`、`DTypeLike`、`NDArray` 三大别名，研究了 `_SupportsArray` / `_NestedSequence` / `_SupportsDType` 等协议，学过了精度协变体系与现代的 `@overload` / `TypeVar` 写法，也理解了 NumPy 如何用 mypy + reveal 夹具来测试类型本身。

但这些都是「零件」。本讲的目标是把它们**拼成一台完整的机器**：为一个**你自己写的**数组处理函数，设计一套从输入到输出都精确的静态类型注解，并用与 NumPy 官方同款的方式为它补一个可被测试断言固化的「reveal 夹具」。

学完本讲，你应当能够：

- 综合运用 `ArrayLike` / `DTypeLike` / `NDArray` 三个公共别名，为一个真实函数拼出输入与输出的类型。
- 用 `@overload` 写出一个「输入精度 → 输出精度」一一映射的「精度安全」函数，并补上兜底实现。
- 仿照 `numpy/typing/tests/data/reveal/` 目录的风格，为自己的函数写一个 `.pyi` 夹具，用 `assert_type` 把期望返回类型变成机器可校验的断言，并用 mypy 跑通。

## 2. 前置知识

本讲默认你已经读过下列讲义（它们建立了本讲直接复用的术语，此处只做一句话回顾，不再展开）：

- **u2-l1 ArrayLike**：`ArrayLike = Buffer | _DualArrayLike[np.dtype, complex | bytes | str]`，刻意避开 object 数组。
- **u2-l2 DTypeLike**：`DTypeLike = type | str | np.dtype | _SupportsDType[np.dtype] | _VoidDTypeLike`，五选一的联合类型。
- **u2-l3 NDArray**：`NDArray[ScalarT] = np.ndarray[_AnyShape, np.dtype[ScalarT]]`，形状留白、只暴露元素类型。
- **u4-l3 现代 @overload**：自 2.3 起 `NBitBase` 被弃用，官方改推「上界为标量类的 `TypeVar`」或「`@overload`」来表达精度关系。
- **u6-l1 静态类型测试方法论**：NumPy 把 mypy 当库在 pytest 进程内调用，对 `data/pass`、`fail`、`reveal`、`misc` 四类夹具批量检查；`reveal` 目录里用 `assert_type` 固化期望类型。

如果你对 `@overload` 的「多条存根签名 + 最后一条兜底实现」结构还不熟悉，建议先回看 u4-l3，再读本讲。

此外，本讲会用到三个 Python 类型系统的常识，先做最简交代：

- **`typing.overload`**：一个装饰器，允许你为同一个函数写多个「只含签名、不含实现」的存根（`...` 结尾），紧跟一个真正的实现。类型检查器只看那些 `@overload` 存根来推断调用结果的类型，而运行时只用最后那条实现。
- **`assert_type(value, T)`**：`typing` 模块的函数。它在运行时几乎什么都不做（直接返回 `value`），但在静态检查时要求「检查器为 `value` 推断出的类型」必须**正好等于** `T`，否则报错。它是把「我期望的返回类型」写成机器可校验断言的关键工具。
- **`type[X]`**（小写 `type`）：表示「`X` 这个类本身」。例如 `np.float32` 这个类对象的静态类型就是 `type[np.float32]`。注意它和「`X` 的实例」`X` 是两回事。

## 3. 本讲源码地图

本讲综合引用下列文件，它们在前序讲义中都已被详细拆解，这里只标注「在本讲里取哪一块」：

| 文件 | 在本讲中的作用 |
| --- | --- |
| [numpy/typing/__init__.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py) | 公共壳：用一行 `from numpy._typing import ...` 搬来四大别名；模块 docstring 里给出了 `@overload` 的官方范例 `phase`。 |
| [numpy/_typing/_array_like.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_array_like.py) | `ArrayLike`、`NDArray` 的真实定义，以及 `_DualArrayLike` 引擎和 `_ArrayLikeFloat_co` 等收窄别名。 |
| [numpy/_typing/_dtype_like.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_dtype_like.py) | `DTypeLike` 与可参数化的 `_DTypeLike[ScalarT]`、以及 `_DTypeLikeFloat` 等按种类收窄的别名。 |
| [numpy/_typing/_scalars.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_scalars.py) | `_FloatLike_co` / `_IntLike_co` 等标量侧「可强制转换」协变别名，是「精度映射」的判断依据。 |
| [numpy/typing/tests/data/reveal/arithmetic.pyi](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/reveal/arithmetic.pyi) | reveal 夹具的「标准像」：用大量 `assert_type` 把运算结果类型钉死，是我们仿写的样板。 |
| [numpy/typing/tests/data/reveal/nbit_base_example.pyi](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/reveal/nbit_base_example.pyi) | 一个「自定义泛型函数 + `assert_type` 校验」的最小完整范例，结构上与本讲实战几乎同构。 |
| [numpy/typing/tests/test_typing.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/test_typing.py) | `test_reveal` 把 `reveal/` 目录下每个 `.pyi` 喂给 mypy，文件不出现在错误清单里就算通过。 |

## 4. 核心概念与源码讲解

本讲按「先把三大别名装进一个签名 → 再用 `@overload` 让它精度安全 → 最后用 reveal 夹具把期望类型变成测试」的顺序，拆成三个最小模块。

### 4.1 从需求到签名：ArrayLike / DTypeLike / NDArray 的协同

#### 4.1.1 概念说明

设想一个真实需求：写一个工具函数 `to_float(x, dtype=None)`，把任意「能转成数组的东西」转成一个**浮点** `ndarray`，并允许调用者指定浮点精度。这个需求里天然出现三个对外暴露的「类型面」：

1. **输入 `x`**：「能转成数组的东西」——这正是 `ArrayLike` 的定义。
2. **可选参数 `dtype`**：「能转成 dtype 的东西」——这正是 `DTypeLike` 的定义。
3. **返回值**：「一个浮点数组」——这正是 `NDArray[np.floating]` 的含义。

换句话说，前序讲义里的三大别名并不是孤立的学术名词，它们恰好对应一个数组函数的「入参 / 配置 / 出参」三件套。本模块的任务，是确认这三块积木分别从哪里来、为什么能直接拼在一起。

#### 4.1.2 核心流程

把三大别名装进签名，流程是「找别名 → 展开 → 拼装」：

1. **找别名**：三个名字都在公共壳 `numpy/typing/__init__.py` 的 `__all__` 里声明，再经一行 `from numpy._typing import ...` 从私有包搬来。
2. **展开**（心里要清楚每个别名展开成什么，但**签名里不要展开**，保持用别名）：
   - `ArrayLike` = `Buffer | _DualArrayLike[np.dtype, complex | bytes | str]`（缓冲区 / 带 `__array__` 的对象 / 嵌套序列 / 内置标量）。
   - `DTypeLike` = `type | str | np.dtype | _SupportsDType[np.dtype] | _VoidDTypeLike`（类对象 / 字符串编码 / 已有 dtype / 带 dtype 属性的对象 / 结构化 dtype）。
   - `NDArray[ScalarT]` = `np.ndarray[_AnyShape, np.dtype[ScalarT]]`（形状留白，只把元素类型 `ScalarT` 暴露出来）。
3. **拼装**：`def to_float(x: ArrayLike, dtype: DTypeLike = ...) -> NDArray[np.floating]: ...`。

一个关键约定：**签名里用公共别名，不要用私有展开式**。公共别名是 NumPy 承诺稳定的对外契约；私有的 `_DualArrayLike`、`_SupportsDType` 等随时可能变。这也是 u1-l2 讲过的「公共壳 + 私有实现」分层在实战中的直接体现。

#### 4.1.3 源码精读

先看公共壳怎么把三个名字搬出来：

[numpy/typing/__init__.py:175-177](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L175-L177) —— 一行 `from numpy._typing import ArrayLike, DTypeLike, NBitBase, NDArray` 把四个别名从私有包搬进公共壳，紧跟着 `__all__` 把对外暴露面收窄到这四个名字。我们在自己的代码里写 `import numpy.typing as npt` 后，`npt.ArrayLike` / `npt.DTypeLike` / `npt.NDArray` 就来自这里。

再看这三个别名的「真身」其实都在私有文件里。先看 `ArrayLike` 与 `NDArray`（同在一个文件）：

[numpy/_typing/_array_like.py:15](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_array_like.py#L15) —— `type NDArray[ScalarT: np.generic] = np.ndarray[_AnyShape, np.dtype[ScalarT]]`。这是一个 PEP 695 的**参数化类型别名**：它自身带一个类型参数 `ScalarT`（上界 `np.generic`），把它填进 `np.dtype[ScalarT]` 作为数组的元素类型，形状则钉死为彻底留白的 `_AnyShape`。所以 `NDArray[np.floating]` 就是「元素类型是某种浮点、形状任意」的数组——正好是我们 `to_float` 的返回。

[numpy/_typing/_array_like.py:55](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_array_like.py#L55) —— `type ArrayLike = Buffer | _DualArrayLike[np.dtype, complex | bytes | str]`。这就是 `to_float` 第一个入参的类型。注意它**故意**不包含 `np.object_`，所以把生成器、裸 `object` 之类会造出 object 数组的东西喂进来，类型检查器会报错（u2-l1 已详述）。

[numpy/_typing/_array_like.py:48-53](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_array_like.py#L48-L53) —— `_DualArrayLike[DTypeT, BuiltinT]` 引擎，是 `ArrayLike` 背后的「双轨参数化」机制：一个类型参数 `DTypeT` 管带精度的类数组对象，另一个 `BuiltinT` 管 Python 内置标量。`ArrayLike` 把 `DTypeT` 放成最宽的 `np.dtype`、`BuiltinT` 放成 `complex | bytes | str`（`complex` 已隐含 `int/float/bool`）。

再看 `DTypeLike`：

[numpy/_typing/_dtype_like.py:101](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_dtype_like.py#L101) —— `type DTypeLike = type | str | np.dtype | _SupportsDType[np.dtype] | _VoidDTypeLike`。这是 `to_float` 第二个入参的类型。注意公共别名里字符串构件故意写成宽松的 `str`，精确到 `Literal` 编码的收窄是私有的 `_DTypeLikeFloat` 等别名的活儿（u2-l4）。

[numpy/_typing/_dtype_like.py:51-54](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_dtype_like.py#L51-L54) —— 可参数化的私有别名 `type _DTypeLike[ScalarT: np.generic] = type[ScalarT] | np.dtype[ScalarT] | _SupportsDType[np.dtype[ScalarT]]`。和公共 `DTypeLike` 仅一字之差但能带精度，是后续收窄别名（`_DTypeLikeFloat` 等）的基底。

现在把三块拼起来，就得到 `to_float` 的「零号版本」——还不会精度安全，但签名已经全用公共别名：

```python
# 示例代码：零号版本（不精度安全，但三大别名已就位）
import numpy as np
import numpy.typing as npt

def to_float(x: npt.ArrayLike, dtype: npt.DTypeLike = None) -> npt.NDArray[np.floating]:
    return np.asarray(x, dtype=dtype)
```

这个版本的问题是：调用 `to_float(data, np.float32)` 时，静态检查器只会按兜底签名告诉你返回 `NDArray[np.floating]`，**丢掉了「我明明指定了 float32」这条信息**。下一模块解决它。

#### 4.1.4 代码实践

**实践目标**：亲手确认三大别名「能拼、拼出来合法、且确实比裸 `Any` 严格」。

**操作步骤**：

1. 新建 `capstone.py`，粘贴上面「零号版本」的 `to_float`。
2. 在同目录建 `probe.py`，写几行调用：

```python
# 示例代码：probe.py
import numpy as np
from capstone import to_float

a = to_float([1, 2, 3])               # list -> 数组，合法
b = to_float((1.0, 2.0), dtype="f4")  # 字符串码也是合法 DTypeLike
c = to_float(x**2 for x in range(3))  # 生成器：运行时能跑出 object 数组
```

3. 安装 mypy 后运行：`mypy --strict capstone.py probe.py`。

**需要观察的现象**：

- `a`、`b` 两行不报错，说明 `list` / `tuple` / 字符串码都在 `ArrayLike` / `DTypeLike` 范围内。
- `c` 那一行（生成器）应当被 mypy 拒绝，错误大致是「`Generator[...]` 与 `ArrayLike` 不兼容」——这正是 `ArrayLike` 刻意避开 object 数组的表现（u2-l1）。

**预期结果**：mypy 对生成器那行报错，对前两行放行。如果检查器对三行都不报错，说明你没有开启足够严格的模式（如缺 `--strict`），请补上。

> 待本地验证：不同 mypy 版本报错措辞略有差异；以「生成器那行被拒、其余放行」为准。

#### 4.1.5 小练习与答案

**练习 1**：如果把返回类型从 `NDArray[np.floating]` 改成 `NDArray[np.float64]`，调用 `to_float([1,2,3], np.float32)` 在类型上还成立吗？为什么？

**参考答案**：不成立。`np.float32` 不是 `np.float64`，而 `NDArray` 的元素类型参数对精度是**不变（invariant）**的（u1-l1、u4-l1）。换言之 `NDArray[np.float32]` 既不是 `NDArray[np.float64]` 的子类型也不是父类型。这也正解释了为什么我们需要下一个模块的 `@overload`——单一签名无法表达「按你给的精度返回对应精度」。

**练习 2**：公共 `DTypeLike` 里的字符串构件为什么写成宽松的 `str`，而不是 `_FloatingCodes` 这类 `Literal` 联合？

**参考答案**：因为公共别名追求「外松」（能接纳所有 `np.dtype(...)` 接受的字符串），把字符串精确分类到 `Literal` 编码是「内紧」的私有收窄别名（`_DTypeLikeFloat` 等）的职责。后者供 NumPy 内部签名收窄用，不对外承诺稳定（u2-l2、u2-l4）。

### 4.2 精度安全：用 @overload 表达「输入精度 → 输出精度」映射

#### 4.2.1 概念说明

零号版本的缺陷在于「精度信息在签名里丢了」。修复它有两种官方推荐（u4-l3）的现代写法：

- **上界为标量类的 `TypeVar`**：适合「同精度进出」（输入和输出是同一种精度）。
- **`@overload`**：适合「不同输入精度一一映射到不同输出精度」。

我们的 `to_float` 属于后者——「你传 `np.float32` 当 dtype，我就返回 `NDArray[np.float32]`；传 `np.float64` 就返回 `NDArray[np.float64]`；什么都不传就返回最宽的 `NDArray[np.floating]`」。这种「输入分支 → 输出分支」的映射，正是 `@overload` 的主场。

`@overload` 的工作机制可以一句话概括：**类型检查器只看那些 `@overload` 存根来推断调用结果；运行时只用最后那条不带 `@overload` 的兜底实现**。因此我们可以为同一个函数名写好几条「精确分支」，再补一条「最宽入参、最宽返回」的实现把它们兜住。

NumPy 官方在模块 docstring 里给出的 `phase` 例子，就是这种映射的标准范式。

#### 4.2.2 核心流程

写一个精度安全的 `@overload` 函数，分四步：

1. **枚举关心的精度分支**：例如 `float32` / `float64` 两个具体分支，外加一个「其余情况」的兜底。
2. **为每个分支写一条 `@overload` 存根**：入参里把分支条件编码进类型（如 `dtype: type[np.float32]`），返回类型写成该分支对应的精确 `NDArray[...]`。每条存根以 `...` 结尾，**没有函数体**。
3. **写兜底实现**：一条不带 `@overload` 的真正实现，入参取**最宽**（`dtype: npt.DTypeLike = ...`），返回取**最宽**（`NDArray[np.floating]`）。它的函数体是真正干活的代码。
4. **靠声明顺序消歧**：把窄分支（具体精度）写在前面，宽分支（兜底）写在最后。类型检查器自上而下匹配，先命中先采用——和 u5-l2 讲 ufunc 重载时「窄分支前置、宽分支兜底」是同一套规则。

消歧的直觉可以用一个简单的不等式链来记：

\[
\text{type}[\text{np.float32}] \;\subsetneq\; \text{type}[\text{np.floating}] \;\subsetneq\; \text{npt.DTypeLike}
\]

具体精度的 `type[np.float32]` 最窄，必须先匹配；最宽的 `DTypeLike` 放最后兜底。若顺序反了，第一条宽分支会「吃掉」所有调用，后面的窄分支永远触发不到，精度信息又丢了。

#### 4.2.3 源码精读

先看官方 `phase` 范例——它就在公共壳的模块 docstring 里，是「不同输入精度 → 不同输出精度」的标准写法：

[numpy/typing/__init__.py:110-120](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L110-L120) —— 三条 `@overload` 把 `complex64 → float32`、`complex128 → float64`、`clongdouble → longdouble` 三种映射一一声明，最后一条不带 `@overload` 的实现 `def phase(x: np.complexfloating) -> np.floating` 用最宽的 `complexfloating` 入参和 `floating` 返回兜底。注意它的输入是**标量**（`np.complex64` 等），不是数组；本讲我们会把它扩展到数组侧。

接着看 NumPy 内部如何为「浮点 dtype-like」收窄——这正是我们 `to_float` 分支判断的依据：

[numpy/_typing/_dtype_like.py:82](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_dtype_like.py#L82) —— `type _DTypeLikeFloat = type[float] | _DTypeLike[np.floating] | _FloatingCodes`。它告诉我们「能被当作浮点 dtype 的对象」有哪些：Python 的 `float` 类、任意 `np.floating` 子类型的类对象、以及浮点字符串编码。我们的 `to_float` 分支用 `type[np.float32]` / `type[np.float64]`，恰好是 `_DTypeLike[np.floating]`（即 `type[ScalarT]`，`ScalarT=np.float32` 等）的特例。

再看标量侧的「可强制转换」协变别名，它解释了「为什么 bool/int 也能安全地变 float」：

[numpy/_typing/_scalars.py:14](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_scalars.py#L14) —— `type _FloatLike_co = float | np.floating | np.integer | np.bool`。后缀 `_co` 在标量侧指 **coercible（可强制转换）**（u4-l2），判据是 NumPy 五档 casting 里的 `same_kind`。这条别名说明 `bool` / `int` 在 `same_kind` 规则下都能被安全地提升为浮点——这正是 `to_float([True, False])` 或 `to_float([1, 2, 3])` 在语义上合理、且类型系统能接纳的根源。

把以上拼起来，就得到 `to_float` 的精度安全版本：

```python
# 示例代码：capstone.py（精度安全版本）
from typing import overload
import numpy as np
import numpy.typing as npt

@overload
def to_float(x: npt.ArrayLike, dtype: type[np.float32]) -> npt.NDArray[np.float32]: ...
@overload
def to_float(x: npt.ArrayLike, dtype: type[np.float64]) -> npt.NDArray[np.float64]: ...
@overload
def to_float(x: npt.ArrayLike, dtype: npt.DTypeLike = ...) -> npt.NDArray[np.floating]: ...
def to_float(x: npt.ArrayLike, dtype: npt.DTypeLike = None) -> npt.NDArray[np.floating]:
    """把任意 array-like 转成浮点数组；指定 dtype 时返回对应精度的数组。"""
    return np.asarray(x, dtype=dtype)
```

读法对照：

- 前两条 `@overload` 是「窄分支」：入参用具体的 `type[np.float32]` / `type[np.float64]`，返回是对应精度的 `NDArray[np.float32]` / `NDArray[np.float64]`。
- 第三条 `@overload` 与最后的实现是「宽分支 / 兜底」：入参放宽到 `npt.DTypeLike`（含字符串、已有 dtype、`None` 等），返回放宽到 `NDArray[np.floating]`。
- 因为 `type[np.float32]` 比 `DTypeLike` 窄，且窄分支写在前，调用 `to_float(x, np.float32)` 会命中第一条，返回类型被精确推断为 `NDArray[np.float32]`。

> 关于实现体的一个细节：mypy 会检查「实现体的返回类型是否兼容于实现签名声明的返回类型」。`np.asarray(x, dtype=dtype)` 在 `dtype` 为通用 `DTypeLike` 时，其自身重载推断出的类型未必恰好是 `NDArray[np.floating]`。若 mypy 在实现体报错，常见做法是把实现签名声明为各 `@overload` 返回的**联合**（如 `NDArray[np.float32] | NDArray[np.float64] | NDArray[np.floating]`），或在实现体内显式处理。本讲聚焦签名设计，实现体的细节留作练习观察。

#### 4.2.4 代码实践

**实践目标**：亲眼看到「指定 `dtype=np.float32` 时，返回类型被精确推断为 `NDArray[np.float32]`，而不是被稀释成 `NDArray[np.floating]`」。

**操作步骤**：

1. 用上面「精度安全版本」替换 `capstone.py` 里的 `to_float`。
2. 在 `probe.py` 里用 `reveal_type` 探查推断结果（`reveal_type` 只产生 `note:`，供人阅读）：

```python
# 示例代码：probe.py
from typing import reveal_type
import numpy as np
from capstone import to_float

reveal_type(to_float([1, 2, 3], np.float32))   # 期望看到 NDArray[numpy.float32]
reveal_type(to_float([1, 2, 3], np.float64))   # 期望看到 NDArray[numpy.float64]
reveal_type(to_float([1, 2, 3]))               # 期望看到 NDArray[numpy.floating]
```

3. 运行 `mypy --strict capstone.py probe.py`，阅读 `note:` 输出。

**需要观察的现象**：

- 前两行的 `note:` 分别精确到 `numpy.float32` / `numpy.float64`，说明窄分支被命中、精度信息被保留。
- 第三行回落到 `numpy.floating`，说明兜底分支生效。
- 若三条都显示 `numpy.floating`，多半是窄分支没命中——检查 `@overload` 顺序是否把窄分支写在了前面。

**预期结果**：三条 `reveal_type` 的 `note:` 分别为 `NDArray[numpy.float32]`、`NDArray[numpy.float64]`、`NDArray[numpy.floating]`。

> 待本地验证：mypy 输出的类型字符串里，形状部分可能显示为 `tuple[Any, ...]`（即 `_AnyShape`），元素类型部分以 `numpy.float32` / `numpy.floating` 为准。

#### 4.2.5 小练习与答案

**练习 1**：把三条 `@overload` 里「窄分支」和「兜底分支」的顺序对调（兜底放最前），重新跑 mypy，`reveal_type(to_float([1,2,3], np.float32))` 会变成什么？为什么？

**参考答案**：会变成 `NDArray[numpy.floating]`。因为兜底分支的入参 `DTypeLike` 最宽，写在最前会**先命中**并吃掉所有调用，后面的窄分支永远触发不到。这就是 u5-l2 强调「窄分支前置、宽分支兜底」的原因——`@overload` 的消歧完全靠声明顺序。

**练习 2**：如果需求改成「输入和输出永远是同一种浮点精度」（即不需要区分 float32/float64 的映射，只要保证进出同型），用 `@overload` 还是 `TypeVar` 更合适？

**参考答案**：用**上界为标量类的 `TypeVar`** 更合适（u4-l3）。写法大致是 `S = TypeVar("S", bound=np.floating)`，签名 `def to_float(x: ArrayLike, dtype: type[S]) -> NDArray[S]`。`@overload` 适合「一一映射」（不同输入精度对应不同输出精度）；`TypeVar` 适合「同型进出」。两者不可混淆：`TypeVar` 表达不了「complex64→float32」这种精度提升，那只能靠 `@overload` 逐条手写。

### 4.3 用 reveal fixture 给函数补静态测试

#### 4.3.1 概念说明

到目前为止，`to_float` 的类型对不对，我们是用 `reveal_type` 肉眼读 `note:` 来判断的。但 `reveal_type` 产生的 `note:` 只是「供人看」的，测试框架会丢弃它——它**不能**在 CI 里自动判定对错。

要把「我期望的返回类型」变成**机器可校验的断言**，需要把 `reveal_type` 换成 `assert_type`。`assert_type(value, T)` 在静态检查时要求检查器为 `value` 推断出的类型**正好等于** `T`，否则报一条 `error:`（而不是 `note:`）。有 `error:` 就会让测试失败。

这正是 NumPy 自己测试类型的方式：它在 `numpy/typing/tests/data/reveal/` 目录下放了一堆 `.pyi` 桩文件，每个文件里用大量 `assert_type` 把各种运算的返回类型钉死，再用 `test_typing.py` 里的 `test_reveal` 把整个目录喂给 mypy——只要某个 `.pyi` 出现任何 `error:`，对应测试就失败。

我们的目标：**为 `to_float` 仿写一个这样的 reveal 夹具**，让自己写的函数也享受和 NumPy 内置函数同款的「类型回归测试」。

#### 4.3.2 核心流程

写一个 reveal 夹具并让它被测试，分五步：

1. **选桩文件后缀**：reveal 夹具是 `.pyi`（桩），不是 `.py`。`.pyi` 只含类型信息、不会被执行，专门给检查器读。
2. **导入被测对象**：在 `.pyi` 顶部 `from capstone import to_float`，并 `import numpy as np`、`import numpy.typing as npt`、`from typing import assert_type`。
3. **声明输入变量**：给输入一个**带类型、无初值**的名字（如 `x: npt.ArrayLike`），模拟「任意 array-like 输入」。这是 reveal 夹具的惯用写法——不关心运行时取值，只关心类型。
4. **逐条写 `assert_type`**：`assert_type(to_float(x, np.float32), npt.NDArray[np.float32])`。第二个参数就是你**期望**的返回类型。
5. **交给 mypy 校验**：对 `.pyi` 跑 mypy（最好开 `--strict`）。若 mypy 推断出的类型与第二个参数不符，会报 `error:`，即「reveal mismatch」。

NumPy 的 `test_reveal` 做的正是第 5 步的自动化：它把 `reveal/` 下每个 `.pyi` 喂给 mypy，只要某文件**不出现在错误清单里**就算该文件通过。

#### 4.3.3 源码精读

先看 `test_reveal` 如何把 reveal 目录变成测试：

[numpy/typing/tests/test_typing.py:162-165](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/test_typing.py#L162-L165) —— `test_reveal` 用 `@pytest.parametrize("path", get_test_cases(REVEAL_DIR))` 为 `reveal/` 下每个 `.pyi` / `.py` 生成一个用例，用例 id 就是文件名。它的判定逻辑极简（见下条）：文件不在 mypy 错误清单里即通过。

[numpy/typing/tests/test_typing.py:178-187](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/test_typing.py#L178-L187) —— 遍历该文件所有错误行，整理成「reveal mismatch」失败信息。换言之，`assert_type` 不匹配会产出 `error:`，`error:` 汇总成失败。这是把「期望类型」变成「可失败断言」的完整链路。

再看 reveal 夹具的「标准像」——`arithmetic.pyi` 是 NumPy 算术运算的类型回归测试，写法就是我们要仿的：

[numpy/typing/tests/data/reveal/arithmetic.pyi:1-5](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/reveal/arithmetic.pyi#L1-L5) —— 顶部导入 `assert_type`、`numpy`、`numpy.typing`，以及（本文件需要的）私有精度叶子 `_64Bit` / `_128Bit`。注意它可以直接从 `numpy._typing` 导入私有名字——reveal 夹具是 NumPy 内部测试，不受「公共 API」约束。

[numpy/typing/tests/data/reveal/arithmetic.pyi:43-56](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/reveal/arithmetic.pyi#L43-L56) —— 一组「带类型、无初值」的数组声明，如 `AR_f: npt.NDArray[np.float64]`、`AR_Any: npt.NDArray[Any]`。它们是后续 `assert_type` 表达式的「素材」。这种「只声明类型、不赋初值」的写法是 `.pyi` 桩的常态。

[numpy/typing/tests/data/reveal/arithmetic.pyi:69](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/reveal/arithmetic.pyi#L69) —— `assert_type(AR_number - AR_number, npt.NDArray[np.number])`。这一行断言「两个 `NDArray[np.number]` 相减，结果仍是 `NDArray[np.number]`」。如果将来某次改动让减法结果变了类型，这行就会报 `error:`，测试失败——这就是类型回归测试的价值。

最后看一个和本讲实战**几乎同构**的最小范例——一个自定义泛型函数 + `assert_type` 校验：

[numpy/typing/tests/data/reveal/nbit_base_example.pyi:7](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/reveal/nbit_base_example.pyi#L7) —— 定义一个泛型函数 `def add[T1: npt.NBitBase, T2: npt.NBitBase](a: np.floating[T1], b: np.integer[T2]) -> np.floating[T1 | T2]: ...`（这是老式 `NBitBase` 写法，文件末尾标了 `# type: ignore[deprecated]`）。它的结构——「在夹具里定义一个函数存根 + 对它调用并 `assert_type`」——正是我们为 `to_float` 写夹具时要复用的骨架。

[numpy/typing/tests/data/reveal/nbit_base_example.pyi:14-17](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/reveal/nbit_base_example.pyi#L14-L17) —— 四条 `assert_type` 把 `add(f8, i8)` 等调用的返回类型精确钉死为 `np.floating[_64Bit]`、`np.floating[_32Bit | _64Bit]` 等。这告诉我们：reveal 夹具既能测「库函数」，也能测「你自己定义在夹具里的函数」。

把以上范式套到 `to_float` 上，就得到本模块的产物：

```python
# 示例代码：capstone_reveal.pyi（仿 reveal 目录风格的夹具）
from typing import assert_type

import numpy as np
import numpy.typing as npt

from capstone import to_float

# 带类型、无初值的「任意 array-like 输入」
x: npt.ArrayLike

# 三条 assert_type：分别钉死三个 @overload 分支的返回类型
assert_type(to_float(x, np.float32), npt.NDArray[np.float32])
assert_type(to_float(x, np.float64), npt.NDArray[np.float64])
assert_type(to_float(x),            npt.NDArray[np.floating])
```

读法对照：

- `x: npt.ArrayLike` 模拟「任意合法输入」，不关心运行时取值。
- 三条 `assert_type` 的第二个参数，正是 4.2 里三条 `@overload` 各自声明的返回类型。如果 `to_float` 的 `@overload` 写错（比如顺序反了导致都回落到兜底），第一、二条 `assert_type` 会因为实际推断成 `NDArray[np.floating]` 而报 `error:`。

> 关于「夹具里 `from capstone import to_float`」：NumPy 官方 reveal 夹具测的都是 numpy 自身对象，能直接 import；你测自己的模块时，需要让 mypy 能找到 `capstone`（同目录或配置好 `MYPYPATH`）。若不便，也可像 `nbit_base_example.pyi` 那样把被测函数的**存根**直接写在 `.pyi` 里再 `assert_type`。

#### 4.3.4 代码实践

**实践目标**：用一个 `.pyi` 夹具 + 一次 mypy 调用，把 `to_float` 的三个分支返回类型变成可失败断言；并亲手制造一次「故意写错期望类型 → 看到失败」来确认断言真的在生效。

**操作步骤**：

1. 把上面 `capstone_reveal.pyi` 保存在 `capstone.py` 同目录。
2. 确认 `to_float` 的 `@overload` 顺序正确（窄分支在前、兜底在后），然后运行：

```bash
mypy --strict capstone.py capstone_reveal.pyi
```

3. **反向验证**：把 `capstone_reveal.pyi` 里第一行的期望类型**故意**改错，例如把 `npt.NDArray[np.float32]` 改成 `npt.NDArray[np.float64]`，再跑一次 mypy。
4. （可选）把 `capstone_reveal.pyi` 复制一份到 NumPy 的 `reveal/` 目录（仅用于体验流程），设置 `NPY_RUN_MYPY_IN_TESTSUITE=1` 后跑 `pytest numpy/typing/tests/test_typing.py -k reveal`，观察它如何被 `test_reveal` 自动收纳为一个用例。

**需要观察的现象**：

- 步骤 2：mypy **无任何 `error:`**（可能有 `note:`，无关），说明三条断言全部命中——返回类型如你所料。
- 步骤 3：mypy 在改错那行报一条 `error:`，形如「error: assertion failed（assert_type）」或「assert_type mismatch」，指出实际类型 `NDArray[numpy.float32]` 与你写的 `NDArray[numpy.float64]` 不符。
- 步骤 4（若执行）：`test_reveal` 会为 `capstone_reveal` 生成一个用例；只要 `.pyi` 有 `error:`，该用例失败并打印「reveal mismatch」。

**预期结果**：步骤 2 干净通过；步骤 3 必报一条 assert_type 相关 `error:`。这「一通一断」恰好证明夹具的断言是活的——它不会对错误的类型放行。

> 待本地验证：mypy 对 `assert_type` 不匹配的错误措辞与是否需要 `--strict` 因版本而异；以「改错期望类型必产生 `error:`、不改则无 `error:`」为准。

#### 4.3.5 小练习与答案

**练习 1**：`reveal_type` 和 `assert_type` 都能「显示」一个表达式的类型，为什么 NumPy 在 reveal 夹具里几乎只用 `assert_type`？（提示：回想 `note:` 与 `error:` 的区别。）

**参考答案**：因为 `reveal_type` 只产出 `note:` 行，`note:` 在 u6-l1 讲过会被 `run_mypy` fixture 过滤丢弃（`if "note:" in i: continue`），无法被测试判定；而 `assert_type` 不匹配会产出 `error:`，`error:` 会让 `test_reveal` 把该文件判为失败。简言之：`reveal_type` 是「调试用的放大镜」，`assert_type` 是「会报警的锁」。目录虽叫 reveal，现代夹具几乎全用 `assert_type`。

**练习 2**：为什么 reveal 夹具写成 `.pyi` 而不是 `.py`？如果写成 `.py` 会丢失什么能力？

**参考答案**：`.pyi` 是纯类型桩，不会被运行时执行，专门给检查器读，允许写「带类型、无初值」的声明（如 `x: npt.ArrayLike`）和仅有签名的函数存根（`...` 结尾）。若写成 `.py`，这些无初值的声明和纯签名函数在运行时要么报错（引用未赋值变量）、要么需要补真实实现，反而把「只测类型」这件事掺进运行时语义。此外，`test_code_runs` 只对 `pass/` 目录（真 `.py`）做运行时回测，`reveal/` 目录只做静态检查——`.pyi` 正好匹配「只测类型、不跑代码」的定位。

## 5. 综合实践

把三个模块串起来，完成一个端到端的小任务：**为「按输入精度返回对应浮点数组」的工具函数，交付「实现 + 类型注解 + 类型测试」三件套**。

任务拆解：

1. **实现**（`capstone.py`）：写一个带 `@overload` 的 `to_float(x, dtype=None)`，满足：
   - 入参 `x: npt.ArrayLike`、`dtype: npt.DTypeLike = None`。
   - 至少两条窄分支：`dtype` 为 `type[np.float32]` / `type[np.float64]` 时，分别返回 `NDArray[np.float32]` / `NDArray[np.float64]`。
   - 一条兜底分支与实现：`dtype` 为 `npt.DTypeLike` 时返回 `NDArray[np.floating]`，实现体内调用 `np.asarray(x, dtype=dtype)`。
2. **夹具**（`capstone_reveal.pyi`）：仿 `reveal/` 风格，用 `assert_type` 把三个分支的返回类型钉死。
3. **校验**：`mypy --strict capstone.py capstone_reveal.pyi` 应无 `error:`；再故意改错一条期望类型，确认会报 `error:`。
4. **进阶（可选）**：把窄分支从两个扩到三个——加一条 `type[np.longdouble]` 分支，返回 `NDArray[np.longdouble]`，并在夹具里补对应 `assert_type`。注意 `np.longdouble` 是平台相关的（u4-l2），观察 mypy 对它的处理。
5. **反思**：在 `capstone.py` 里留一行注释，说明「为什么这里用 `@overload` 而不是 `TypeVar`」——把 u4-l3 的取舍判断写进你自己的代码。

验收标准：

- `mypy --strict` 对正确的实现 + 夹具：零 `error:`。
- 故意把 `to_float(x, np.float32)` 的期望类型改成 `NDArray[np.float64]`：必出一条 assert_type 相关 `error:`。
- 能口头解释：窄分支为什么必须写在兜底分支前面、`assert_type` 为什么能当测试用、三大别名分别对应函数的哪一面。

如果顺利完成，你其实已经复刻了 NumPy 为 `np.add`、`np.asarray` 等函数做类型建模与类型回归测试的最小骨架——只是把对象从「库函数」换成了「你自己的函数」。

## 6. 本讲小结

- **三大别名 = 函数的三面**：`ArrayLike` 管入参、`DTypeLike` 管配置、`NDArray[...]` 管出参；签名里用公共别名、不展开私有实现，是稳定性的前提。
- **`@overload` 修复「精度信息丢失」**：用「窄分支前置、宽分支兜底」的声明顺序，让 `to_float(x, np.float32)` 的返回被精确推断为 `NDArray[np.float32]`，而不是被稀释成 `NDArray[np.floating]`。
- **消歧靠声明顺序**：`type[np.float32] ⊊ type[np.floating] ⊊ DTypeLike`，必须从窄到宽排列，否则宽分支吃掉一切。
- **`@overload` vs `TypeVar`**：一一映射用 `@overload`，同型进出用上界为标量类的 `TypeVar`；精度提升（如 complex64→float32）只能用 `@overload`。
- **`assert_type` 把期望类型变成测试**：它产 `error:` 而非 `note:`，能被 `test_reveal` 判定失败；`reveal_type` 只是调试放大镜。
- **reveal 夹具是 `.pyi`**：纯类型桩、带类型无初值的声明、只做静态检查不跑代码，与 `pass/` 目录的「真 `.py` + 运行时回测」分工互补。

## 7. 下一步学习建议

走到这里，你已经完整走过了「认识别名 → 拆解协议 → 理解精度 → 掌握工程化 → 学会测试 → 综合实战」的全链路。后续可以朝三个方向深入：

- **阅读真实库的类型测试**：从 [numpy/typing/tests/data/reveal/ufuncs.pyi](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/reveal/ufuncs.pyi) 和 [numpy/typing/tests/data/reveal/ndarray_misc.pyi](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/reveal/ndarray_misc.pyi) 入手，看 NumPy 如何为最复杂的 ufunc 与 ndarray 运算写 `assert_type`，体会「外松内紧」在真实签名里的落地。
- **对比 pyright 与 mypy 的差异**：本讲引用的 `arithmetic.pyi` 里多处 `# type: ignore[assert-type]` 注释（如 [L416-L419](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/data/reveal/arithmetic.pyi#L416-L419)）记录了「mypy 推断错误、pyright 正确」的已知差异。尝试用两个检查器跑你的 `capstone_reveal.pyi`，观察结论是否一致——这是走向「跨检查器可移植类型」的必经之路。
- **把类型注解引入你自己的项目**：挑一个你维护的、涉及数值计算的函数，按本讲的「三件套」流程（别名签名 → `@overload` 精度分支 → reveal 夹具）为它补类型与类型测试，并在 CI 里加一道 mypy 关卡。当你能像 NumPy 一样「用测试保护类型不被悄悄改坏」时，本手册的目标就真正达成了。
