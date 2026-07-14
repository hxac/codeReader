# 平台精度 _nbit 与标量协变别名 _scalars

## 1. 本讲目标

学完本讲，读者应当能够：

- 看懂 `_nbit.py` 如何把「平台相关的标量精度」（如 C 的 `long` 在不同平台可能是 32 位或 64 位）翻译成静态类型里的**联合类型**（`_32Bit | _64Bit`）。
- 读懂 `_scalars.py` 中 `_XLike_co` 系列别名的构造规律，理解 `_co` 后缀在这里的真正含义是 **coercible（可被强制转换）**，并能在读源码时把它与 u3-l2 见到的协变 TypeVar `_T_co` 区分开。
- 解释 `_IntLike_co` 为什么「同时容纳 Python `bool` 与 NumPy `np.bool`」。
- 把 `_XLike_co` 的思想从「标量侧」推广到 `_array_like.py` 的 `_ArrayLike*_co`，理解它们与 u2-l1 的 `_DualArrayLike` 引擎如何组合。
- 说出 NumPy 五档 casting 规则中 `same_kind` 的位置，并据此理解 `_co` 别名的成员边界。

## 2. 前置知识

本讲承接前几讲已建立的术语，先做最简回顾：

- **静态类型 vs 运行时**（u1-l1）：类型检查器（mypy / pyright）按注解推演，与运行时行为可能不一致。
- **公共壳 / 私有实现**（u1-l2）：`numpy.typing` 是薄壳，真实实现藏在私有包 `numpy._typing`。本讲三份核心源码都在私有包里。
- **`_NBitBase` 与精度叶子**（u4-l1）：`NBitBase` 是一套「把精度当类型参数」的层次，叶子 `_8Bit < _16Bit < _32Bit < _64Bit < _96Bit < _128Bit` 是不可再分的精度原子。本讲直接把这些叶子当积木。
- **变型（variance）**（u3-l2）：协变 TypeVar 用 `_co` 后缀约定（`B <: A ⇒ F[B] <: F[A]`）。本讲会遇到同名后缀 `_co`，但含义不同，需要专门区分。
- **NumPy 标量类型层级**：`bool < integer < floating < complexfloating`，统称 `number`。判断「谁能转成谁」依赖这条层级与 casting 规则。

一个易混点先点明：**`_co` 这两个字母在 NumPy 源码里有两副面孔。**

| 出现位置 | 装饰对象 | `_co` 含义 |
|---|---|---|
| `_nested_sequence.py`（u3-l2） | TypeVar `_T_co` | **co**variant（协变） |
| `_scalars.py` / `_array_like.py`（本讲） | 联合别名 `_XLike_co` | **co**ercible（可被强制转换） |

两者只是恰好都用了 `co` 两个字母。本讲的 `_co` 一律指 **coercible**，依据是源码注释的明文（见 4.2.3、4.3.3）。

## 3. 本讲源码地图

| 文件 | 体量 | 作用 |
|---|---|---|
| `numpy/_typing/_nbit.py` | 约 17 行 | 把平台相关标量（`int_`/`long`/`longdouble`…）映射到 bit 精度叶子或其联合 |
| `numpy/_typing/_scalars.py` | 约 21 行 | 定义 `_BoolLike_co` … `_NumberLike_co` 等「可被强制转换」的标量别名 |
| `numpy/_typing/_array_like.py` | 第 57–92 行附近 | 把 `_co` 思想搬到数组侧，产出 `_ArrayLike*_co` 系列 |
| `numpy/__init__.pyi`（消费侧样本） | 第 6228 行附近等 | 顶层桩文件用 `_NBit*` 给 `np.int_`/`np.longdouble` 等平台标量标注精度 |

## 4. 核心概念与源码讲解

### 4.1 _nbit 平台精度别名：把"平台相关"翻译成"联合类型"

#### 4.1.1 概念说明

C 语言的整数和浮点类型宽度跟平台 / 编译器有关：

- `long` 在 Linux 64 位是 64 位，在 Windows 64 位却是 32 位（LLP64 模型）。
- 指针宽度的整数 `intptr_t`（对应 `np.intp`）在 32 位平台是 32 位、64 位平台是 64 位。
- `long double` 在 MSVC 是 64 位、x86 扩展精度是 80 位、某些平台是 128 位。

NumPy 把这些 C 类型包成 Python 标量（`np.int_`、`np.longdouble`…）。问题来了：**静态类型系统需要一个确定的类型来标注它们，但它们的精度在编译期并不确定。**

`_nbit.py` 的解法是：既然精度不确定，就让它的类型**同时列出所有可能**——用联合类型。`np.intp` 的精度是 `_32Bit | _64Bit`，读作「32 位或 64 位，二者居其一」。类型检查器据此把 `np.int_(5)` 推断为 `signedinteger[_32Bit | _64Bit]`，而不是某个固定精度。

#### 4.1.2 核心流程

精度从「叶子」到「平台标量类型」的装配链：

```
_nbit_base.py            _nbit.py                     numpy/__init__.pyi
──────────────           ─────────                    ──────────────────
_8Bit _16Bit … _128Bit   _NBitIntP = _32Bit | _64Bit   intp = signedinteger[_NBitIntP]
(精度原子, u4-l1)        (平台联合)                    int_ = intp
```

关键三点：

1. **平台无关**的标量（`int8`/`int16`/`float32`/`float64`…）在 `__init__.pyi` 里直接钉死成单一叶子：`int64 = signedinteger[_64Bit]`。
2. **平台相关**的标量用 `_NBit*` 联合：`intp = signedinteger[_NBitIntP]`。
3. `_NBit*` 只是「精度参数」，要包进 `signedinteger[...]` / `floating[...]` / `complexfloating[...]` 才成为完整的标量类型。

把「可能集合」写成联合的语义可形式化为：

\[
\text{precision}(T) \;=\; \bigcup_{p \,\in\, \text{platforms}} \text{bitwidth}_p(T)
\]

例如 \(\text{precision}(\text{intp}) = \{32\} \cup \{64\}\)，对应类型 `_32Bit | _64Bit`。

#### 4.1.3 源码精读

整个 `_nbit.py` 只有十几行，先导入精度叶子（u4-l1 讲过的 `_nbit_base`）：

> [numpy/_typing/_nbit.py:L1-L3](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_nbit.py#L1-L3) —— 模块定位为「平台相关 `number` 的精度」，并从 `_nbit_base` 取出六个精度叶子。

随后逐个把 C 类型名映射到叶子或叶子联合：

> [numpy/_typing/_nbit.py:L5-L11](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_nbit.py#L5-L11) —— 整数族：`_NBitByte=_8Bit`、`_NBitIntP=_32Bit | _64Bit`、`_NBitLong=_32Bit | _64Bit`、`_NBitLongLong=_64Bit`。

其中最关键的一行：

> [numpy/_typing/_nbit.py:L8-L9](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_nbit.py#L8-L9) —— `_NBitIntP = _32Bit | _64Bit`；`_NBitInt` 只是它的别名。

浮点族同理，`long double` 的三态联合最典型：

> [numpy/_typing/_nbit.py:L13-L16](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_nbit.py#L13-L16) —— `_NBitLongDouble = _64Bit | _96Bit | _128Bit`（64 / 80 / 128 三档）。

这些别名最终在顶层桩文件 `numpy/__init__.pyi` 被消费，给平台标量定型。先看导入（只取了一个子集）：

> [numpy/__init__.pyi:L55-L69](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/__init__.pyi#L55-L69) —— 顶层桩从 `numpy._typing` 导入 `_NBitByte`、`_NBitIntP`、`_NBitLong`、`_NBitLongDouble` 等。注意它**没有**导入 `_NBitHalf`/`_NBitSingle`/`_NBitDouble`/`_NBitInt`，因为这些名字对应的标量（`half`/`single`/`double`）精度其实固定，桩里直接写成 `half = float16` 等。

然后是装配现场：

> [numpy/__init__.pyi:L6228-L6234](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/__init__.pyi#L6228-L6234) —— `intp = signedinteger[_NBitIntP]`、`int_ = intp`、`long = signedinteger[_NBitLong]`、`longlong = signedinteger[_NBitLongLong]`。

> [numpy/__init__.pyi:L6796-L6799](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/__init__.pyi#L6796-L6799) —— `half=float16`、`single=float32`、`double=float64`（固定精度），`longdouble = floating[_NBitLongDouble]`（平台相关）。

把 4.1.3 的映射整理成表：

| NumPy 标量 | C 类型 | `_NBit*` 别名 | 展开后的精度 |
|---|---|---|---|
| `np.byte` | `char` | `_NBitByte` | `_8Bit` |
| `np.intc` | `int` | `_NBitIntC` | `_32Bit` |
| `np.intp` = `np.int_` | `intptr_t` / `long` | `_NBitIntP` | `_32Bit \| _64Bit` |
| `np.longlong` | `long long` | `_NBitLongLong` | `_64Bit` |
| `np.longdouble` | `long double` | `_NBitLongDouble` | `_64Bit \| _96Bit \| _128Bit` |

#### 4.1.4 代码实践

**目标**：追踪 `np.int_` 的精度如何由 `_NBitIntP` 表达，并亲手看到这条链。

**操作步骤**：

1. 打开 `numpy/__init__.pyi:6232`，确认 `int_ = intp`；上一行 `intp = signedinteger[_NBitIntP]`。
2. 打开 `numpy/_typing/_nbit.py:8`，确认 `_NBitIntP = _32Bit | _64Bit`。
3. 写一个最小脚本（**示例代码**，非项目原有）：

```python
# 示例代码
from typing import TYPE_CHECKING
import numpy as np

x: np.int_ = np.int_(5)  # 等价于 np.intp(5)

if TYPE_CHECKING:
    reveal_type(x)
```

4. 用 mypy 或 pyright 检查该脚本。

**需要观察的现象**：类型检查器把 `x` 的精度显示成包含 `_32Bit` 与 `_64Bit` 的联合，而不是单一精度。

**预期结果**：`reveal_type` 大致输出形如 `numpy.signedinteger[numpy._typing._32Bit | numpy._typing._64Bit]`（确切文本随检查器与版本而异，**待本地验证**）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `np.float64` 的类型是 `floating[_64Bit]`，而 `np.longdouble` 却是 `floating[_NBitLongDouble]`？

**答案**：`float64` 是 IEEE-754 双精度，在所有平台都是 64 位，精度确定，直接用叶子 `_64Bit`；`longdouble` 映射到 C 的 `long double`，宽度随平台变化（64 / 80 / 128），所以必须用联合 `_64Bit | _96Bit | _128Bit`。

**练习 2**：`_nbit.py` 里同时有 `_NBitHalf = _16Bit` 和 `_NBitSingle = _32Bit`，但顶层桩 `__init__.pyi` 没有导入它们。请说明原因。

**答案**：`np.half`/`np.single` 对应 `float16`/`float32`，精度与平台无关，桩里直接写成 `half = float16`、`single = float32`，不需要 `_NBitHalf`/`_NBitSingle`。这两个别名留在 `_nbit.py` 主要是为了与整数 / 浮点族命名对称，供下游或未来使用。

---

### 4.2 _scalars 的 _XLike_co：可被强制转换的标量集合

#### 4.2.1 概念说明

很多 NumPy 函数的入参并不要求「精确是某种标量」，而要求「能安全转成某种标量」。例如 `np.arange(stop)` 的 `stop` 接受 `int`，也接受 `np.int64`，甚至接受 `np.bool`（`True` 当 1）。类型系统需要一种别名来表达「**所有能被强制转换成 X 的标量**」。

`_scalars.py` 用 `_XLike_co` 命名这一族别名，`_co` = **coercible**（可强制转换）。源码注释写得很直白：

> [numpy/_typing/_scalars.py:L9-L10](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_scalars.py#L9-L10) —— 明文说明 `<X>Like_co` 表示「能以 `same_kind` 规则被强制转换成 `<X>` 的全部标量」。

⚠️ 别和 u3-l2 的协变 TypeVar `_T_co` 搞混：那个 `_co` 是 covariant，这个 `_co` 是 coercible。

#### 4.2.2 核心流程

每个 `_XLike_co` 别名，本质是同一个集合在「目标种类 X 不同」时的不同实例。它的设计判据可写成：

\[
\text{XLike\_co} \;\approx\; \{\, t \;\mid\; t \text{ 可按 same\_kind 规则转换成 } X \,\}
\]

随着目标 X 沿 `bool → uint → int → float → complex` 升级，能被安全提升进来的源类型越来越多，联合也层层变宽：

```
_BoolLike_co    = bool | np.bool
_UIntLike_co    = bool | np.unsignedinteger | np.bool
_IntLike_co     = int | np.integer | np.bool
_FloatLike_co   = float | np.floating | np.integer | np.bool
_ComplexLike_co = complex | np.number | np.bool
_NumberLike_co  = _ComplexLike_co
```

两条规律：目标越宽，联合越大；`bool` 与 `np.bool` 几乎处处出现（布尔可被安全转换到任意数值种类，自然也满足更宽松的 same_kind）。注意 `_IntLike_co` **不含** `float`——浮点转整数要截断小数、有信息损失，不在此列。

> 说明：`_XLike_co` 是手工编写的联合，注释里的 `same_kind` 表达的是设计意图。个别跨数值种类（如整数 ↔ 浮点）的归属以源码定义为权威、以运行时 `np.can_cast` 为参考（见 4.4）。

#### 4.2.3 源码精读

> [numpy/_typing/_scalars.py:L5-L7](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_scalars.py#L5-L7) —— 注释解释 `_StrLike_co`/`_BytesLike_co` 没必要定义，因为 `np.str_`/`np.bytes_` 本就是内置 `str`/`bytes` 的子类；随后给出 `_CharLike_co = str | bytes`。

> [numpy/_typing/_scalars.py:L11-L20](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_scalars.py#L11-L20) —— 全部 `_XLike_co` 别名，底部还有 `_VoidLike_co` 与 `_ScalarLike_co`。

聚焦本讲实践任务要解释的那一行：

> [numpy/_typing/_scalars.py:L13](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_scalars.py#L13) —— `_IntLike_co = int | np.integer | np.bool`。

这行只显式列了 `int | np.integer | np.bool`，没有 `bool`。但 Python 的 `bool` 是 `int` 的子类（`bool <: int`），所以内置 `bool` 被 `int` **隐式覆盖**；而 NumPy 的 `np.bool`（源码里的写法，运行时即 `np.bool_`）在 NumPy 类型层级里**不是** `np.integer` 的子类（bool 与 integer 平级、并不隶属于 `np.number`），必须**显式**写出来。这就是「`_IntLike_co` 同时容纳 `bool` 与 `np.bool`」的全部原因——一个靠继承隐式进来，一个靠显式列举。

#### 4.2.4 代码实践

**目标**：亲手验证 `_IntLike_co` 为何同时包含 `bool` 与 `np.bool`。

**操作步骤**：

1. 用 NumPy 运行时确认两类布尔与整数的关系（**示例代码**）：

```python
# 示例代码
import numpy as np
print(issubclass(bool, int))                 # Python bool 是 int 子类 -> True
print(issubclass(np.bool_, np.integer))      # np.bool_ 不是 np.integer 子类 -> False
print(np.can_cast(np.bool_, np.int64, casting="same_kind"))  # True：bool 可安全转 int
```

> 注：源码别名里写作 `np.bool`，运行时用 `np.bool_`，二者在 NumPy ≥ 2.0 指向同一个标量类型。

2. 用类型检查器观察（**示例代码**）：

```python
# 示例代码
import numpy as np
from numpy._typing import _IntLike_co

def takes_int_co(x: _IntLike_co) -> int: ...

takes_int_co(True)         # OK：bool <: int，被 int 隐式覆盖
takes_int_co(np.bool_(1))  # OK：np.bool 被显式列入联合
takes_int_co(3.14)         # 预期报错：float 不在 _IntLike_co
```

**需要观察的现象**：前两个调用通过，第三个 `3.14` 被类型检查器拒绝。

**预期结果**：`bool` 与 `np.bool` 都被接受，`float` 报「类型不兼容」类错误。（`_IntLike_co` 是私有别名，仅供阅读 / 实验；正式代码请用公共的 `npt.ArrayLike` 等接口。）

#### 4.2.5 小练习与答案

**练习 1**：`_FloatLike_co` 同时含 `np.floating` 和 `np.integer`，而 `_IntLike_co` 不含 `np.floating`。请从「数值提升方向」解释这一非对称。

**答案**：在实际运算里，整数与浮点混合会被提升为浮点（整数「向上」变成浮点是自然且无歧义的），所以 `_FloatLike_co` 把整数收进来表达「能转成浮点」；反过来浮点转整数要截断小数、有信息损失且歧义，故 `_IntLike_co` 排除浮点。这种「方向性」与 NumPy 的类型提升规则一致：int → float → complex 逐级提升。

**练习 2**：`_NumberLike_co` 的定义是 `_NumberLike_co = _ComplexLike_co`，为什么不直接展开写？

**答案**：复数是数值种类的「最大」目标，能被转换成复数的集合就等于全部数值；用别名复用 `_ComplexLike_co` 既避免重复，又把「数值 = 可转复数」这层语义表达在名字上。

---

### 4.3 _ArrayLike*_co：把 _co 思想搬到数组侧

#### 4.3.1 概念说明

`_XLike_co` 描述的是「标量侧」的可转换集合。但函数常接受的是**数组或类数组对象**。`_array_like.py` 把同样的 `_co` 思想套到数组上，产出 `_ArrayLikeBool_co`、`_ArrayLikeInt_co`、`_ArrayLikeFloat_co`… 一族别名。

它复用 u2-l1 讲过的双参数引擎 `_DualArrayLike[DTypeT, BuiltinT]`：`DTypeT` 描述「带 `__array__` 对象的 dtype 集合」，`BuiltinT` 描述「嵌套序列里的内置标量集合」。`_co` 在这里的作用，就是把这两个参数换成「能转换成 X」的集合。

#### 4.3.2 核心流程

```
_XLike_co（标量集合）              _DualArrayLike 引擎（u2-l1）
       │                                  │
       └──────────► _ArrayLikeX_co ◄──────┘
                   = _DualArrayLike[ np.dtype[ <X 的同族 dtype 联合> ],  <内置标量> ]
```

例如 `_ArrayLikeInt_co` 把「整数族」的 dtype 联合 `np.dtype[np.bool | np.integer]` 塞进 `DTypeT`，把内置 `int` 塞进 `BuiltinT`。于是「元素可转换成整数」的数组、嵌套 int 列表、单个 Python int，都被同一别名接纳。

#### 4.3.3 源码精读

> [numpy/_typing/_array_like.py:L57-L58](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_array_like.py#L57-L58) —— 注释明确 `ArrayLike<X>_co` = 「能以 `same_kind` 规则强制转换成 X 的类数组对象」，与 `_scalars.py` 的 `_co` 语义一致。

> [numpy/_typing/_array_like.py:L59-L73](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_array_like.py#L59-L73) —— 从 `_ArrayLikeBool_co` 到 `_ArrayLikeObject_co`。

逐行看代表样本：

> [numpy/_typing/_array_like.py:L61](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_array_like.py#L61) —— `_ArrayLikeInt_co = _DualArrayLike[np.dtype[np.bool | np.integer], int]`。对照 `_IntLike_co`：`np.bool | np.integer` 正是「可转换成整数」的 dtype 联合，`int` 是对应的内置标量。

有两个「特例」值得注意：

> [numpy/_typing/_array_like.py:L72-L73](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_array_like.py#L72-L73) —— `_ArrayLikeDT64_co = _ArrayLike[np.datetime64]`、`_ArrayLikeObject_co = _ArrayLike[np.object_]`。注意它们用的是单参数 `_ArrayLike`（u2-l1）而非 `_DualArrayLike`：`datetime64` 和 `object_` 没有对应的内置标量，不需要第二轨。

还有一对用于「提升到 float64 / complex128」提升规则的别名（带**双下划线**前缀的是私有中间构件）：

> [numpy/_typing/_array_like.py:L84-L89](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_array_like.py#L84-L89) —— `__Float64_co`、`__Complex128_co`（私有中间别名）与 `_ArrayLikeFloat64_co`、`_ArrayLikeComplex128_co`。它们刻画「与 float64 / complex128 运算后会被提升」的输入集合，被顶层桩里的 `__matmul__` 等运算符重载使用（见 `numpy/__init__.pyi:4165` 附近的 `NDArray[floating[_64Bit]]` 配 `_ArrayLikeFloat64_co` 重载）。

#### 4.3.4 代码实践

**目标**：观察 `_ArrayLikeInt_co` 如何被真实函数消费，并理解它比公共 `ArrayLike` 窄在哪。

**操作步骤**：

1. 阅读 `numpy/_core/multiarray.pyi` 中 `unravel_index` 的两个重载：

> [numpy/_core/multiarray.pyi:L625-L627](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/multiarray.pyi#L625-L627) —— 传 `_IntLike_co` 返回标量元组；传 `_ArrayLikeInt_co` 返回 `NDArray[intp]` 元组。同一函数用「标量版 / 数组版」两个 `_co` 别名区分重载。

2. 思考：为什么这里不直接用公共 `npt.ArrayLike`？因为 `unravel_index` 只接受「可转成整数」的输入，传浮点数组应在类型检查期就被拒；`_ArrayLikeInt_co` 正好把这个收紧。

**需要观察的现象**：理解 `_co` 别名是「按目标种类收紧的 ArrayLike」，比公共 `ArrayLike`（接受任意元素）更窄、更精确。

**预期结果**：能口述「`_ArrayLikeInt_co` ⊂ `ArrayLike`，多出的限制是元素必须可转换成整数」。

#### 4.3.5 小练习与答案

**练习 1**：`_ArrayLikeDT64_co` 为什么用 `_ArrayLike[np.datetime64]` 而不是 `_DualArrayLike[...]`？

**答案**：`datetime64` 没有对应的 Python 内置标量（没有「日期时间字面量」这种 builtin），第二轨 `BuiltinT` 无意义，所以只用单参数的 `_ArrayLike`。

**练习 2**：`_ArrayLikeFloat64_co` 和 `_ArrayLikeFloat_co` 有何区别？

**答案**：`_ArrayLikeFloat_co` 接受「能转成任意浮点」的输入；`_ArrayLikeFloat64_co` 刻画的是「与 float64 运算后会被提升到 float64」的更具体集合（含 `float16`/`float32`/整数/布尔等会被提升的元素），专门服务于提升规则明确的运算符重载（如 `__matmul__`）。

---

### 4.4 same_kind casting：_co 别名的依据

#### 4.4.1 概念说明

整讲反复出现的 `same_kind`，是 NumPy 五档 casting 规则之一。从严到松：

| 规则 | 含义 | 例 |
|---|---|---|
| `no` | 完全不转换 | `int64 → int64` |
| `equiv` | 仅字节序变化 | `>i4 → <i4` |
| `safe` | 无精度损失 | `int32 → int64`、`bool → float64` |
| **`same_kind`** | **safe 的全部，加上同族内的降精度** | **`int64 → int32`、`float64 → float32`** |
| `unsafe` | 任意转换 | `float64 → int64`（截断） |

`_co` 别名锚定的就是 **same_kind** 这一档：把「同属一个数值种类、可以互相转换」的类型打包。`np.can_cast(a, b, "same_kind")` 为真，是 `a` 进入「目标为 b 的 `_co` 别名」的设计判据。

#### 4.4.2 核心流程

判断某标量 `t` 是否「应当」属于 `XLike_co` 的近似算法：

```
def in_xlike_co(t, X):
    return np.can_cast(t, X, casting="same_kind")
```

据此可核对 `_scalars.py` 的多数成员。例如对 `_IntLike_co`（目标 `int`）：

- `can_cast(np.bool_, np.int64, "same_kind")` → 真（布尔转整数属于 safe）→ `np.bool` 进集合，且 Python `bool` 经 `bool <: int` 也进。
- `can_cast(np.float64, np.int64, "same_kind")` → 假（跨种类且会截断）→ 浮点**不**进集合。

> 说明：`same_kind` 是 `_co` 别名的**设计意图**；`_XLike_co` 毕竟是手工联合，个别跨数值种类（如整数 ↔ 浮点）的精确归属以源码定义为权威，必要时以本地 `np.can_cast` 结果为参考。

#### 4.4.3 源码精读

`same_kind` 的明文证据在两处注释：

> [numpy/_typing/_scalars.py:L9-L10](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_scalars.py#L9-L10) —— 标量侧：`<X>Like_co` = 可按 `same_kind` 转换成 `<X>` 的标量。

> [numpy/_typing/_array_like.py:L57-L58](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_array_like.py#L57-L58) —— 数组侧：`ArrayLike<X>_co` = 可按 `same_kind` 转换成 `X` 的类数组对象。

两侧用词完全一致，说明 `_co` 是跨文件统一的约定。

#### 4.4.4 代码实践

**目标**：用运行时 `np.can_cast` 复核几个边界明确的 `_co` 别名成员。

**操作步骤**（**示例代码**）：

```python
# 示例代码
import numpy as np

# 布尔与整数：same_kind（其实更严格地属于 safe）之内
print(np.can_cast(np.bool_, np.int64, "same_kind"))   # True  -> np.bool 进 _IntLike_co

# 跨种类 / 会截断：不在 same_kind 之内
print(np.can_cast(np.float64, np.int64, "same_kind"))         # False -> float 不进 _IntLike_co
print(np.can_cast(np.complex128, np.float64, "same_kind"))    # False

# 跨数值种类的边界（整数↔浮点）以源码定义为准：
print(np.can_cast(np.int64, np.float64, "same_kind"))  # 待本地验证；_FloatLike_co 仍显式收录 np.integer
```

**需要观察的现象**：布尔、整数与浮点之间的「能否转换」与 `_XLike_co` 联合的成员方向一致。

**预期结果**：前三项依次为 `True / False / False`，与 `_IntLike_co`（收纳布尔、排除浮点）吻合；第四项请以本地结果为准，并对照源码定义理解 `_FloatLike_co` 为何仍含 `np.integer`。

#### 4.4.5 小练习与答案

**练习 1**：`int64 → int32` 是 `same_kind` 还是 `safe`？这说明了 `_IntLike_co` 的什么取舍？

**答案**：是 `same_kind` 但不是 `safe`（可能丢高位）。说明 `_IntLike_co` 收纳的是「同族」而非「无损」，只要是整数族即可，不在乎位宽差异。

**练习 2**：如果把 `_co` 别名的判据从 `same_kind` 收紧成 `safe`，会有什么后果？

**答案**：集合会变窄，例如 `_IntLike_co` 里 `int64 → int32` 这类「可降精度」的同族输入会被排除；许多当前合法的同族转换会在类型检查期被误拒，与 NumPy「same_kind 即允许」的运行时行为脱节。

---

## 5. 综合实践

把本讲四块串起来，完成一个小型「精度与可转换性」调研：

1. **平台精度追踪**：在 `numpy/__init__.pyi` 中找到 `np.int_`、`np.longdouble`、`np.byte` 三者的定义，画出各自的精度链（叶子 / `_NBit*` 联合 → 完整标量类型）。
2. **可转换集合构造**：仿照 `_scalars.py`，自己写一个 `_MyFloatLike_co`，让它包含 `float | np.floating | np.integer | np.bool`，并对照源码说明每个成员为何「能转成浮点」。
3. **数组侧推广**：仿照 `_ArrayLikeInt_co`，用 `_DualArrayLike` 写一个 `_MyArrayLikeFloat_co`，参数取 `np.dtype[np.bool | np.integer | np.floating]` 与 `float`，并解释为何这样写就等价于「元素可转换成浮点的类数组对象」。
4. **类型检查回测**（可选）：把你写的别名用在一个示例函数签名上，传 `True`、`np.float32(1)`、`3` 观察是否都通过，传 `"abc"` 观察是否被拒；若结果不确定标「待本地验证」。

完成后，你应能回答：`np.int_` 在类型系统里到底是什么形状？为什么一个标量别名能同时容纳 `bool` 与 `np.bool`？`_co` 别名和 `same_kind` 是什么关系？

## 6. 本讲小结

- `_nbit.py` 把**平台相关**的标量精度翻译成精度叶子的**联合**：`np.intp = signedinteger[_NBitIntP] = signedinteger[_32Bit | _64Bit]`，顶层桩 `numpy/__init__.pyi` 消费这些别名给平台标量定型。
- `_scalars.py` 的 `_XLike_co` 别名，`_co` = **coercible**（可强制转换），锚定 NumPy 的 `same_kind` casting 规则；它与 u3-l2 协变 TypeVar 的 `_co`（covariant）只是同形不同义。
- `_IntLike_co = int | np.integer | np.bool`：Python `bool` 靠 `bool <: int` 隐式纳入，NumPy `np.bool` 因不属 `np.integer` 而被显式列举——故二者同时在场。
- `_array_like.py` 的 `_ArrayLike*_co` 把同样的 `_co` 思想通过 `_DualArrayLike` 引擎搬到数组侧，产出「按目标种类收紧」的 ArrayLike 子集。
- 消费这些私有别名的是遍布 numpy 的 `.pyi` 桩（`_core/multiarray.pyi`、`linalg/_linalg.pyi`、`polynomial/_polytypes.pyi`、`random/*.pyi` 等），它们让函数签名在类型检查期就能拒绝「不可转换」的输入。

## 7. 下一步学习建议

- 下一讲 **u4-l3 现代精度表达：TypeVar 与 @overload**：`NBitBase` 自 2.3 弃用后，官方推荐用「以标量类为上界的 TypeVar」或 `@overload` 表达精度关系。本讲看到的 `_32Bit | _64Bit` 联合是「枚举式」精度建模，u4-l3 将讲「关系式」建模，二者可对照学习。
- 继续阅读 `numpy/__init__.pyi` 中运算符重载（如 `__add__` / `__matmul__`）如何组合 `_ArrayLike*_co` 与精度叶子，体会「精度 + 可转换性」如何在真实签名里协同。
- 想深入 casting：阅读 NumPy 文档的 `numpy.can_cast` 与「Casting kinds」，把本讲的 `same_kind` 放回五档 casting 的整体语境。
