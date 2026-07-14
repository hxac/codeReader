# 三种字符串 dtype 与字符计数

> 本讲是 `numpy.strings` 系列第 2 篇，承接 [u1-l1 项目定位与门面架构](./u1-l1-overview-and-facade.md)。
> 上一篇我们已经知道：`np.strings` 是一个「门面」，真正的实现都在 `numpy/_core/strings.py`。
> 本篇我们就走进这个实现文件，先弄清一件最基础的事——`numpy.strings` 到底能处理哪几种字符串，以及「一个字符串有多长」是怎么算的。

## 1. 本讲目标

学完本讲，你应该能够：

- 区分 `numpy.strings` 操作的三种输入 dtype：变长 `StringDType`（`char == 'T'`）、定长 `bytes_`（`'S'`）、定长 `str_`（`'U'`）。
- 看懂私有辅助函数 `_get_num_chars` 为什么对 `str_` 数组要返回 `itemsize // 4`。
- 理解 `str_len` 是一个「真正的 ufunc」，并知道它和 `_get_num_chars` 的分工。
- 能够用 `np.array` 构造三种字符串数组，并解释它们的 `dtype`、`itemsize`、`str_len` 输出之间的数量关系。

## 2. 前置知识

在开始之前，请确保你理解下面几个概念（上一篇已经铺垫过的，这里只做一句话回顾）：

- **dtype（数据类型）**：NumPy 数组里每个元素的「类型与编码方式」，例如 `int64`、`float64`，以及本讲要讲的字符串类型。
- **itemsize**：一个 dtype「单个元素在内存里占多少字节」。
- **dtype.char**：每个 dtype 有一个单字符的简写标识，例如 `int64` 是 `'i'`，本讲会频繁用到字符串 dtype 的三个 char：`'T'`、`'S'`、`'U'`。
- **ufunc（universal function）**：NumPy 里「逐元素、可广播」的函数。本讲的 `str_len` 就是一个 ufunc。
- **向量化**：对一个数组整体操作，等价于对每个元素做同样的事，但比写 Python 循环快得多。

如果你对这些词还陌生，可以先回去看 u1-l1，再继续本讲。

## 3. 本讲源码地图

本讲只涉及一个核心源码文件，但它出现的「角色」有多个，我们分清楚：

| 文件 | 在本讲中的角色 |
| --- | --- |
| `numpy/_core/strings.py` | `numpy.strings` 的真正实现。本讲的两个最小模块 `_get_num_chars`、`str_len` 都定义/导入在这里。 |

需要注意一个容易混淆的点：

- `str_len` **不是**在 `numpy/_core/strings.py` 里「写出来的函数」，而是从 `numpy._core.umath` **导入**的一个底层 ufunc（由 C/C++ 实现）。`strings.py` 只负责把它「重新归属」到 `numpy.strings` 模块并对外暴露。
- `_get_num_chars` 则是 `strings.py` 内部一个**纯 Python 的私有辅助函数**。

所以本讲的源码精读会同时展示「导入 + 改写归属」和「就地定义」两种形态。

> 说明：本仓库里 `numpy/strings/__init__.py` 只是门面（详见 u1-l1），它本身没有任何实现。本讲引用的所有源码都在 `numpy/_core/strings.py`。

## 4. 核心概念与源码讲解

### 4.1 三种字符串 dtype：T / S / U

#### 4.1.1 概念说明

`numpy.strings` 的所有函数都要接收「字符串数组」。NumPy 目前有 **三种** 可以放进来的字符串 dtype，它们的本质区别在于「怎么存、能不能变长」：

| dtype | `dtype.char` | Python 端等价类型 | 存储 | 是否定长 | 典型构造 |
| --- | --- | --- | --- | --- | --- |
| `bytes_` | `'S'` | `bytes` | 每字符 1 字节（ASCII/拉丁1） | 定长 | `np.array([b'abc'])` |
| `str_` | `'U'` | `str` | 每字符固定 4 字节（UCS4） | 定长 | `np.array(['abc'])` |
| `StringDType` | `'T'` | `str` | 变长（内部用 UTF-8 等编码） | **变长** | `np.array(['abc'], dtype=np.dtypes.StringDType())` |

理解这三者的关键直觉是：

1. **`bytes_`（'S'）**：老式的「字节串数组」。每个元素是定长的，**1 个字符 = 1 字节**，所以只适合纯 ASCII 或拉丁字符。
2. **`str_`（'U'）**：老式的「Unicode 字符串数组」。为了能表示世界上所有字符（包括中文、emoji），它**固定用 4 字节存 1 个字符**（UCS4 编码）。这就是为什么后面 `itemsize` 会是字符数的 4 倍。
3. **`StringDType`（'T'）**：NumPy 较新的「变长字符串」类型。它不预先固定长度，每个字符串按实际内容动态存储（内部多采用 UTF-8）。它专门用来解决「`str_` 对长字符串、对含大量 ASCII 的 Unicode 文本太浪费内存」的问题。

为什么要在代码里频繁区分这三种？因为同一个函数（比如 `multiply`、`center`）在三种 dtype 下走的**代码分支和底层 C 循环都不一样**——这是后续讲义（u2、u3）的主线。本讲只需要先认清「T / S / U」这三个 char 标识，并理解「字符数」和「字节数」在它们之间的换算差异。

#### 4.1.2 核心流程

判断一个输入数组属于哪种字符串 dtype，最直接的信号是 `array.dtype.char`：

```text
array.dtype.char  ==  'T'   →  变长 StringDType（走专用快速路径）
array.dtype.char  ==  'S'   →  定长 bytes_（1 字符 = 1 字节）
array.dtype.char  ==  'U'   →  定长 str_（1 字符 = 4 字节）
```

举个数量关系的例子（这是本讲最关键的一张表）：

| 数组 | dtype | `itemsize`（字节） | 字符数（`str_len`） | 说明 |
| --- | --- | --- | --- | --- |
| `np.array([b'abc'])` | `dtype('S3')` | 3 | 3 | 1 字符 = 1 字节 |
| `np.array(['abc'])` | `dtype('<U3')` | 12 | 3 | 1 字符 = 4 字节 |
| `np.array(['abc'], dtype=StringDType())` | `dtype=StringDType` | （变长，无固定 itemsize） | 3 | 按实际字符数 |

可以看到：对 `bytes_` 和 `StringDType`，「字节数」和「字符数」基本是一回事或与存储无关；唯独 `str_`，`itemsize` 是字符数的 **4 倍**。下一节的 `_get_num_chars` 就是为了消除这个差异而存在的。

#### 4.1.3 源码精读

我们看 `multiply` 函数里最典型的一段，它同时暴露了「按 `dtype.char` 分发」和「`str_len` 的真实用途」：

```python
    a = np.asanyarray(a)
    ...
    # delegate to stringdtype loops that also do overflow checking
    if a.dtype.char == "T":
        return a * i

    a_len = str_len(a)

    # Ensure we can do a_len * i without overflow.
    if np.any(a_len > sys.maxsize / np.maximum(i, 1)):
        raise OverflowError("Overflow encountered in string multiply")

    buffersizes = a_len * i
    out_dtype = f"{a.dtype.char}{buffersizes.max()}"
    out = np.empty_like(a, shape=buffersizes.shape, dtype=out_dtype)
    return _multiply_ufunc(a, i, out=out)
```

引用与说明：

- [`numpy/_core/strings.py:L198-L199`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L198-L199)：当输入是变长 `StringDType`（`dtype.char == "T"`）时，直接 `return a * i` 委托给 `StringDType` 自己的乘法循环（变长类型自带溢出检查，本讲不展开，详见 u3-l14）。
- [`numpy/_core/strings.py:L201`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L201)：对定长的 `bytes_`/`str_`，先用 `str_len(a)` 算出**每个字符串的字符数**（而不是字节数），后面要用它来算输出缓冲区该多大。
- [`numpy/_core/strings.py:L207-L210`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L207-L210)：用「字符数 × 重复次数」得到每个输出元素需要多大，再拼出一个新的定长 dtype 字符串（如 `"U6"`），最后调用底层 ufunc 写入。

注意 `out_dtype = f"{a.dtype.char}{buffersizes.max()}"` 这一行非常巧妙：它直接复用了输入的 `dtype.char`（`'S'` 或 `'U'`）作为输出 dtype 的前缀。也就是说，输入是 `str_` 输出就是 `str_`，输入是 `bytes_` 输出就是 `bytes_`——`str_len` 在这里保证了「不管 1 字符是 1 字节还是 4 字节，算出来的都是字符数」，于是这一行可以无差别地工作。

#### 4.1.4 代码实践

**实践目标**：亲手构造三种字符串数组，观察它们的 `dtype`、`dtype.char`、`itemsize` 的差异。

**操作步骤**（在你本机能 import numpy 的环境里运行）：

```python
import numpy as np

a_u = np.array(['abc', 'de'])                                   # str_
a_s = np.array([b'abc', b'de'])                                 # bytes_
a_t = np.array(['abc', 'de'], dtype=np.dtypes.StringDType())    # StringDType

for name, a in [('str_   (U)', a_u),
                ('bytes_ (S)', a_s),
                ('StringDType (T)', a_t)]:
    print(f"{name:18} dtype={a.dtype!r:30} char={a.dtype.char!r} itemsize={a.dtype.itemsize}")
```

**需要观察的现象**：

- `a_u` 的 dtype 形如 `dtype('<U3')`（最长元素 3 个字符），`itemsize == 12`（= 3 × 4）。
- `a_s` 的 dtype 形如 `dtype('S3')`，`itemsize == 3`（= 3 × 1）。
- `a_t` 的 dtype 形如 `dtype=StringDType`，`itemsize` 是变长类型内部的固定开销值（与字符数无关，不要拿它去推字符数）。

**预期结果**：三种数组的 `dtype.char` 分别是 `'U'`、`'S'`、`'T'`；只有 `str_` 的 `itemsize` 是「字符数的 4 倍」。`StringDType` 的 itemsize **不能**用来推字符数。

#### 4.1.5 小练习与答案

**练习 1**：如果用 `np.array(['中文'])` 构造数组，它的 dtype 和 itemsize 分别是什么？为什么？

**参考答案**：dtype 是 `dtype('<U2')`（2 个字符），itemsize 是 `8`（= 2 × 4）。因为 `str_` 用 UCS4 固定 4 字节存一个字符，无论是 ASCII 还是中文都是 4 字节/字符——这也是 `StringDType` 想要优化的痛点之一（中文用 UTF-8 只要 3 字节，而 `str_` 要花 4 字节）。

**练习 2**：为什么 `multiply` 在 `dtype.char == "T"` 时走的是「另一条路」（`return a * i`），而不是和 `bytes_`/`str_` 共用同一段代码？

**参考答案**：因为变长 `StringDType` 不需要预先算「输出缓冲区多大」——它的存储是动态的，乘法结果可以直接由 `StringDType` 自己的 C 循环按实际长度写入，并且该循环自带溢出检查；而定长 `bytes_`/`str_` 必须先用 `str_len` 算出最大字符数，再预先 `np.empty_like` 一块定长的输出内存。

---

### 4.2 `_get_num_chars`：从 itemsize 反推字符数

#### 4.2.1 概念说明

`_get_num_chars` 是 `strings.py` 里的一个**私有辅助函数**（名字以单下划线开头）。它解决一个很小但很烦人的问题：

> 给我一个定长字符串数组的 dtype，告诉我「它的每个字段最多能装多少个字符」。

这件事之所以需要「辅助」，正是因为 4.1 里讲的那条规则：**`str_` 数组的 `itemsize` 是字符数的 4 倍**。如果你天真地用 `itemsize` 当字符数，对 `str_` 就会算成 4 倍。

注意它的适用范围：它面向的是**定长** `bytes_`/`str_` 的「字段宽度」，**不是**「当前字符串实际有几个字符」。它常被用来**构造一个新的定长 dtype**（决定输出数组每段留多宽）。

#### 4.2.2 核心流程

`_get_num_chars` 的逻辑用一句话概括：

```text
若 a.dtype.type 是 np.str_ 的子类  →  返回 a.itemsize // 4
否则                              →  返回 a.itemsize
```

数学表达：

- 对 `str_`（UCS4）：字符数 = 字节数 / 4，即

\[
  \text{num\_chars} = \left\lfloor \frac{\text{itemsize}}{4} \right\rfloor
\]

- 对 `bytes_`（1 字节/字符）：

\[
  \text{num\_chars} = \text{itemsize}
\]

这里用整除 `//` 而不是普通除法，既保证结果是整数，也符合 dtype 里 itemsize 一定是 4 的倍数（对 `str_`）这一事实。

#### 4.2.3 源码精读

```python
def _get_num_chars(a):
    """
    Helper function that returns the number of characters per field in
    a string or unicode array.  This is to abstract out the fact that
    for a unicode array this is itemsize / 4.
    """
    if issubclass(a.dtype.type, np.str_):
        return a.itemsize // 4
    return a.itemsize
```

引用与说明：

- [`numpy/_core/strings.py:L99-L107`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L99-L107)：`_get_num_chars` 的完整定义。注意判断用的是 `issubclass(a.dtype.type, np.str_)`，即看「dtype 的 Python 类型」而不是 `dtype.char`——这是一种更「面向类型」的写法。
- 它的 docstring 直接点明了存在的原因：**「for a unicode array this is itemsize / 4」**（对 unicode 数组，字符数 = itemsize / 4）。

它被谁调用？一个典型用法在 `_to_bytes_or_str_array` 里：

```python
    return ret.astype(type(output_dtype_like.dtype)(_get_num_chars(ret)))
```

- [`numpy/_core/strings.py:L124`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L124)：把一个（可能用 object 数组做中间结果的）结果，按「目标 dtype 的类型 + 用 `_get_num_chars` 算出的字符数」重新转回定长字符串数组。这里 `_get_num_chars(ret)` 给出的就是「每个字段该留几个字符」，再交给 `str_`/`bytes_` 的类型构造器生成形如 `np.str_(3)` 的宽度说明。

> 重要区分：`_get_num_chars` 返回的是「**字段能容纳的字符数**」（来自 itemsize），而下一节的 `str_len` 返回的是「**字符串实际有几个字符**」。两者并不相同：一个 `dtype('<U5')` 的元素实际存了 `"ab"`，`_get_num_chars` 给 5，`str_len` 给 2。

#### 4.2.4 代码实践

**实践目标**：手动 import 这个私有函数，验证它对 `str_`/`bytes_` 的换算，并把它和 `itemsize` 对比。

**操作步骤**：

```python
import numpy as np
from numpy._core.strings import _get_num_chars   # 私有函数，仅用于学习验证

a_u = np.array(['abc', 'de'])          # dtype('<U3'), itemsize 12
a_s = np.array([b'abc', b'de'])        # dtype('S3'),  itemsize 3

print('str_   itemsize =', a_u.dtype.itemsize, ' _get_num_chars =', _get_num_chars(a_u))
print('bytes_ itemsize =', a_s.dtype.itemsize, ' _get_num_chars =', _get_num_chars(a_s))
```

**需要观察的现象**：

- `str_` 数组：`itemsize = 12`，`_get_num_chars = 3`（12 // 4）。
- `bytes_` 数组：`itemsize = 3`，`_get_num_chars = 3`（直接返回 itemsize）。

**预期结果**：`_get_num_chars` 把两种 dtype「对齐」成同一套「字符数」语义——这正是它存在的意义。

> 提示：因为 `_get_num_chars` 是私有函数，正式代码不应依赖它；这里仅为理解原理而 import。它面向定长 dtype，**不要**对 `StringDType`（'T'）数组使用——变长类型没有「字段宽度」的概念。

#### 4.2.5 小练习与答案

**练习 1**：一个 `dtype('<U10')` 的数组，`_get_num_chars` 返回多少？`itemsize` 是多少？

**参考答案**：`_get_num_chars` 返回 `10`；`itemsize = 40`（= 10 × 4）。

**练习 2**：如果把 `_get_num_chars` 里的 `//`（整除）改成 `/`（真除法），对 `str_` 会出什么问题？

**参考答案**：`/` 会返回浮点数（如 `12 / 4 == 3.0`），而下游构造 dtype 宽度需要的是整数（`np.str_(3.0)` 这样的调用会报错或语义错误）。`//` 保证结果是 `int`，是对「字符数必为整数」这一约束的正确表达。

---

### 4.3 `str_len`：向量化字符计数 ufunc

#### 4.3.1 概念说明

`str_len` 对应 Python 字符串的 `len(s)`，但它作用在整个 NumPy 数组上——逐元素返回每个字符串的**字符数**（不是字节数）。

它在本系列里有一个很重要的身份：它是 **ufunc**（universal function），也就是说它的真正实现是 C/C++ 写的逐元素循环，由 `numpy._core.umath` 提供；`numpy/_core/strings.py` 只是把它**导入并改写模块归属**，让它对外看起来属于 `numpy.strings`。

为什么强调它是 ufunc？因为后续讲义会反复出现两类函数：

- **已经是 ufunc 的**（如 `str_len`、`find`、`count`、各种 `is*`）：直接走高效的 C 循环。
- **还不是 ufunc、只能逐元素调用 Python 方法的**（如 `upper`、`mod`，走 `_vec_string`）：较慢。

`str_len` 属于第一类，是「字符计数」这一操作的高效实现。

#### 4.3.2 核心流程

`str_len` 作为 ufunc 的执行流程非常直接：

```text
输入数组 a  ──→  str_len（C 层逐元素循环）──→  int64 数组，每个元素 = 对应字符串的字符数
```

它对三种 dtype 的语义统一为「字符数」：

- 对 `bytes_`（'S'）：返回字节数（因为 1 字符 = 1 字节，二者相等）。
- 对 `str_`（'U'）：返回字符数（自动把 4 字节折算成 1 字符）。
- 对 `StringDType`（'T'）：返回字符数（按实际内容，如 `"中文"` 返回 2）。

这就是为什么在 4.1 里 `multiply` 能放心用 `str_len(a)` 算「字符数」——它已经替你处理好了「字节数 vs 字符数」的差异。

#### 4.3.3 源码精读

`str_len` 在 `strings.py` 里没有函数体，它是导入来的。我们分三处看：

**第一处：从底层 umath 模块导入。**

```python
from numpy._core.umath import (
    _center,
    ...
    rindex as _rindex_ufunc,
    startswith as _startswith_ufunc,
    str_len,
)
```

- [`numpy/_core/strings.py:L57`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L57)：`str_len` 从 `numpy._core.umath` 导入。注意同组里 `_center`、`_find_ufunc` 等都被改了别名（加下划线前缀，表示「这是内部用的底层 ufunc」），唯独 `str_len` **保留原名**——因为它要**原样**对外暴露，不经过任何 Python 包装。

**第二处：把它的 `__module__` 改写成 `numpy.strings`。**

```python
def _override___module__():
    for ufunc in [
        isalnum, isalpha, isdecimal, isdigit, islower, isnumeric, isspace,
        istitle, isupper, str_len,
    ]:
        ufunc.__module__ = "numpy.strings"
        ufunc.__qualname__ = ufunc.__name__


_override___module__()
```

- [`numpy/_core/strings.py:L61-L70`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L61-L70)：`str_len` 实际上来自 `numpy._core.umath`，但通过把这个 ufunc 的 `__module__` 属性改写成 `"numpy.strings"`，使得在交互式环境里 `np.strings.str_len` 显示为属于 `numpy.strings`。这是门面/命名空间管理的一致性手段（关于 `set_module` 和模块归属的更系统讲解见 u2-l4）。

**第三处：列入对外暴露的 `__all__`。**

```python
__all__ = [
    # UFuncs
    "equal", "not_equal", "less", "less_equal", "greater", "greater_equal",
    "add", "multiply", "isalpha", "isdigit", "isspace", "isalnum", "islower",
    "isupper", "istitle", "isdecimal", "isnumeric", "str_len", "find",
    ...
```

- [`numpy/_core/strings.py:L73-L90`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L73-L90)：`__all__` 把 `str_len` 列在「UFuncs」分组里，明确标注它是 ufunc；同时这份 `__all__` 的注释还把函数分成「Will gradually become ufuncs」「Will probably not become ufuncs」「Removed until behavior crystallized」几类，是判断某个函数演进方向的好线索（详见 u3-l16）。

把三处连起来读，结论很清晰：`np.strings.str_len` 和 `numpy._core.umath.str_len` 是**同一个 ufunc 对象**，只是 `__module__` 被改写了。这也呼应了 u1-l1 讲过的「门面不复制、只转发」。

#### 4.3.4 代码实践

**实践目标**：验证 `np.strings.str_len` 是一个 ufunc，并观察它在三种 dtype 下的输出。

**操作步骤**：

```python
import numpy as np

a_u = np.array(['abc', 'de'])                                   # str_
a_s = np.array([b'abc', b'de'])                                 # bytes_
a_t = np.array(['abc', '中文'], dtype=np.dtypes.StringDType())   # StringDType

print('type:', type(np.strings.str_len).__name__)   # 期望: numpy.ufunc
print('module:', np.strings.str_len.__module__)     # 期望: numpy.strings
print('str_   ->', np.strings.str_len(a_u).tolist())
print('bytes_ ->', np.strings.str_len(a_s).tolist())
print('T      ->', np.strings.str_len(a_t).tolist())
```

**需要观察的现象**：

- `type(np.strings.str_len)` 是 `numpy.ufunc`，证明它确实是一个 ufunc，而不是普通 Python 函数。
- `np.strings.str_len.__module__` 显示为 `numpy.strings`，正是源码里 `_override___module__` 改写的结果。
- 三种 dtype 的 `str_len` 都返回**字符数**：`[3, 2]`、`[3, 2]`、`[3, 2]`（`'中文'` 是 2 个字符）。

**预期结果**：`str_len` 对三种 dtype 输出一致语义的「字符数」，输出数组是整型数组。输出 dtype 一般是 `int64`（C 层循环决定）。

#### 4.3.5 小练习与答案

**练习 1**：用 `str_len` 验证「`str_` 的 itemsize 是字符数 4 倍」：对一个 `dtype('<U7')` 的数组，`str_len` 和 `itemsize` 分别是多少？

**参考答案**：若数组元素都是 7 个字符（填满宽度），`str_len` 返回 7，`itemsize` 是 28（= 7 × 4）。若元素短于 7，`str_len` 返回实际长度（如 `"ab"` → 2），但 `itemsize` 仍是 28（定长 dtype 的宽度不随内容变）。

**练习 2**：`str_len` 和 `_get_num_chars` 都和「字符数」有关，它们的根本区别是什么？

**参考答案**：`str_len` 统计**字符串实际内容**的字符数（运行时数据，逐元素不同）；`_get_num_chars` 读取 **dtype 的字段宽度**（类型元数据，对定长数组整个数组相同），且仅用于定长 `bytes_`/`str_`。一个是「这个字符串多长」，一个是「这个槽位最多能放几个字符」。

---

## 5. 综合实践

把本讲的三个要点串起来，完成下面这个「三 dtype 对照实验」：

1. 构造三个内容相同（`['abc', 'de']`，其中 `bytes_` 版用 `b'abc', b'de'`）的数组，dtype 分别为 `str_`、`bytes_`、`StringDType`。
2. 对每个数组，打印一张表，包含：`dtype`、`dtype.char`、`dtype.itemsize`、`str_len` 的输出。
3. 对 `str_` 和 `bytes_` 两个**定长**数组，额外用 `_get_num_chars`（`from numpy._core.strings import _get_num_chars`）算出字段宽度，并验证：
   - `str_`：`_get_num_chars(a) == a.dtype.itemsize // 4 == 最大元素字符数`（当元素填满宽度时）。
   - `bytes_`：`_get_num_chars(a) == a.dtype.itemsize`。
4. 用一句话解释：为什么 `str_len` 可以在 `multiply` 里被无差别地用于 `str_` 和 `bytes_`，而 `_get_num_chars` 必须特判 `np.str_`？

**参考结论**：因为 `str_len` 在 C 层已经按 dtype 的编码把字节数折算成了字符数（对 `str_` 自动除以 4），所以 Python 层无需关心 1 字符是几个字节；而 `_get_num_chars` 是纯 Python 函数，只能拿到 `itemsize`（原始字节数），所以必须自己对 `np.str_` 做一次 `// 4`。

## 6. 本讲小结

- `numpy.strings` 处理三种字符串 dtype：变长 `StringDType`（`char='T'`）、定长 `bytes_`（`'S'`，1 字符 = 1 字节）、定长 `str_`（`'U'`，1 字符 = 4 字节）。
- 代码里用 `array.dtype.char == 'T'` 来给变长 `StringDType` 走专用快速路径（如 `multiply` 的 `return a * i`）。
- `str_len` 是一个**真正的 ufunc**（来自 `numpy._core.umath`），逐元素返回「字符数」，对三种 dtype 语义统一，且 `__module__` 被改写为 `numpy.strings`。
- `_get_num_chars` 是纯 Python 私有辅助函数，面向定长 dtype，返回「字段能容纳的字符数」；对 `str_` 必须 `itemsize // 4`，这正是 docstring 强调的关键。
- `str_len` = 字符串实际长度（运行时）；`_get_num_chars` = dtype 字段宽度（类型元数据），二者不可混淆。
- 这套「按 `dtype.char` 分发 + 用 `str_len` 统一字符数」的套路，会在后续 `center`/`strip`/`partition` 等函数里反复出现。

## 7. 下一步学习建议

- **下一讲 u1-l3** 将对比 `numpy.strings` 与遗留模块 `numpy.char`（`defchararray.py`），看后者如何复用本讲的实现，并额外提供 `join/split` 等行为尚未定型的函数。
- 如果你想提前理解「`__module__` 改写」之外更系统的装饰器机制（`set_module`、`array_function_dispatch`），可以直接跳到 **u2-l4**。
- 想看 `str_len` 这个 ufunc 的 **C/C++ 循环**到底怎么逐元素计数的，可以留到 **u3-l12（C++ ufunc 注册）** 和 **u3-l13（string_buffer / fastsearch）** 再深入。
