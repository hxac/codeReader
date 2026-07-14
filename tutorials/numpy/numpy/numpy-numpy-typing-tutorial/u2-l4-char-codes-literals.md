# _char_codes：dtype 字符串编码的 Literal 体系

## 1. 本讲目标

上一讲（u2-l2）我们看到公共别名 `DTypeLike` 的字符串构件写成了**宽松的 `str`**——任何字符串都算合法 dtype。本讲就钻进这个「宽松」背后的**精确**世界：NumPy 用一个专门的私有文件 `_char_codes.py`，把 `np.dtype(...)` 接受的**每一种字符串编码**（`"float64"`、`"i8"`、`"<f8"`、`"|b1"`、`"datetime64[ns]"` …）逐一枚举成 `Literal` 字面量类型，并按数值类别层层嵌套成一个树状别名体系。

学完后你应该能够：

- 读懂 `dtype 字符串编码` 的「解剖结构」：人名 / C 风格别名 / 单字符码 / 尺寸码 / 字节序前缀，并能解释 `|`、`=`、`<`、`>` 四个字节序前缀的含义。
- 读懂 `_char_codes.py` 里从叶子（`_Float64Codes` 等）到根（`_GenericCodes`）的**嵌套 `Literal` 别名层次**，并能手动追踪 `"float64"` 的包含路径。
- 理解 `Literal` 这一类型原语（PEP 586）的作用，以及**嵌套 `Literal` 在运行时会被展平、去重**这一行为，知道它和 `Union` of `Literal` 的区别。
- 会用 `typing.get_args` / `__args__` / `__value__` 在运行时内省这些别名，验证「扁平 `__args__`」这一设计红利。

## 2. 前置知识

本讲承接前面建立的认知：

- **静态类型检查 vs 运行时**（u1-l1）：类型检查器在运行前按注解推演；注解本身通常不影响运行时执行。本讲的 `Literal` 编码是「给检查器看的」精确信息，运行时 `np.dtype` 并不依赖它。
- **公共壳 + 私有实现**（u1-l2）：`_char_codes.py` 位于私有包 `numpy/_typing/`，从不直接对外暴露；它只被 `_dtype_like.py` 导入，用来构建更精细的内部别名。
- **DTypeLike 的「外松内紧」**（u2-l2）：公共 `DTypeLike` 用宽松 `str`，而内部窄化别名 `_DTypeLikeFloat` / `_DTypeLikeInt` 等用精确的 `Literal` 编码。本讲正是把这套「内紧」的编码体系彻底拆开。

此外需要一点 Python 类型基础：

- **`Literal`（PEP 586）**：一个表示「取值只能是固定几个字面量之一」的类型。写 `x: Literal["a", "b"]` 表示 `x` 必须恰好是字符串 `"a"` 或 `"b"`，别的字符串在类型层面都不合法。它是把「魔法字符串」变成可检查类型的关键工具。
- **PEP 695 `type` 语句**：现代类型别名语法 `type _Foo = ...`。本讲所有 `_XxxCodes` 都用 `type` 定义，运行时它们是 `types.TypeAliasType` 实例，真正的类型表达式挂在它的 `__value__` 上（这点在 4.3 会用到，并和 `test_runtime.py` 的 `TypeTup.from_type_alias` 呼应）。
- **dtype 字符串编码**：`np.dtype` 可以用一串短小的字符串指定数据类型，例如 `np.dtype("f8")` 表示 8 字节浮点。这套字符串「方言」是本讲的全部研究对象。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [numpy/_typing/_char_codes.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_char_codes.py) | **本讲核心**。用嵌套 `Literal` 枚举所有 dtype 字符串编码，从单个类型的 `_Float64Codes` 一路聚合到总根 `_GenericCodes`。 |
| [numpy/_typing/_dtype_like.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_dtype_like.py) | `_char_codes` 的**消费方**：在文件头导入一组 `_XxxCodes`，用它们构建 `_DTypeLikeFloat` 等带精度信息的窄化别名（u2-l2 已讲）。 |
| [numpy/typing/tests/test_runtime.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/test_runtime.py) | 运行时测试：`TypeTup.from_type_alias` 展示了如何用 `__value__` + `get_args` 内省 PEP 695 别名，是本讲运行时实践的方法论依据。 |

> **关于永久链接**：本讲所有链接的 HEAD 均为 `9559a6b1ac93610711d8f1243f8c949fca4420bb`。`_char_codes.py` 与 `_dtype_like.py` 实际位于私有 `numpy/_typing/` 包，故链接写成完整正确路径（与 u2-l2 一致），而非 `permalink_base` 默认的 `numpy/typing/` 前缀。

---

## 4. 核心概念与源码讲解

### 4.1 dtype 字符串编码：一串短字符串的解剖学

#### 4.1.1 概念说明

`np.dtype(...)` 最「亲民」的用法是传一个字符串：

```python
np.dtype("float64")   # 8 字节浮点
np.dtype("i8")        # 8 字节有符号整数
np.dtype("<f4")       # 小端序 4 字节浮点
np.dtype("?")         # 布尔
```

问题在于：同一个数据类型往往有**好几种**等价写法。比如「8 字节浮点」至少有 `"float64"`、`"double"`、`"d"`、`"f8"`、`"<f8"`、`"=f8"`、`">f8"`、`"|f8"` 这么多说法，运行时它们都构造出同一个 `dtype('float64')`。

`DTypeLike` 的公共定义里把这些统统塞进宽松的 `str`，让类型检查器「不深究」。但很多内部场景需要**精确**知道「这是不是浮点」「这是不是整数」——这就要求把每一种合法字符串都**显式列出来**，做成一个 `Literal`。`_char_codes.py` 干的就是这件事：它是 NumPy dtype 字符串方言的**权威同义词词典**。

要读懂这本词典，先得学会「解剖」一串 dtype 编码。一串编码通常由以下几个零件拼成：

| 零件 | 例子 | 含义 |
| --- | --- | --- |
| 人名（human name） | `float64`、`int8`、`uint32` | NumPy 标量类的名字，最直观 |
| C 风格别名 | `double`、`byte`、`short`、`single` | 对应 C 类型名，便于跨语言迁移 |
| 单字符码 | `d`、`i`、`f`、`?` | 一个字母的极简写法（来自 array protocol typestr） |
| 尺寸码 | `f8`、`i4`、`u1` | 「类型字母 + 字节数」，如 `f8`=8 字节 float |
| 字节序前缀 | `\|`、`=`、`<`、`>` | 写在尺寸码前面，指定字节序 |

#### 4.1.2 核心流程：字节序前缀的四个符号

字节序前缀是本讲最容易困惑的部分，单独说清。NumPy 用四个字符表达「这组字节在内存里怎么排」：

| 前缀 | 含义 | 典型用途 |
| --- | --- | --- |
| `<` | 小端序（little-endian，低位字节在前） | 跨平台数据交换、x86/ARM 原生序 |
| `>` | 大端序（big-endian，高位字节在前） | 网络字节序、部分历史数据格式 |
| `=` | 本机原生序（native） | 跟随当前 CPU 的字节序 |
| `\|` | 不适用（not applicable） | 单字节类型（如 `bool`、`int8`），字节序无意义 |

于是同一个 8 字节浮点，前缀不同就得到不同编码：`<f8`（强制小端）、`>f8`（强制大端）、`=f8`（本机序）、`|f8`（声明「字节序无关」）。它们在 `_Float64Codes` 里被全部列出。

> 注意：`|` 的本意是「字节序无关」，单字节类型用它最自然；但对多字节类型，`_char_codes` 也照样列出了 `|f8`、`|i4` 等写法。这些是否在所有 NumPy 版本都被运行时接受、其 `.byteorder` 属性具体取何值，**待本地验证**；类型层面它们已被 `Literal` 枚举为合法。

#### 4.1.3 源码精读

以 `_Float64Codes` 为「解剖标本」——它把 8 字节浮点的所有等价字符串一次性列全：

[numpy/_typing/_char_codes.py:27-29](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_char_codes.py#L27-L29)

```python
type _Float64Codes = Literal[
    "float64", "float", "double", "d", "f8", "|f8", "=f8", "<f8", ">f8"
]
```

逐个对号入座：

| 编码 | 零件归类 |
| --- | --- |
| `"float64"` | 人名 |
| `"float"`、`"double"` | C 风格别名 |
| `"d"` | 单字符码（`d` = double） |
| `"f8"` | 尺寸码（`f` + 8 字节） |
| `"|f8"`、`"=f8"`、`"<f8"`、`">f8"` | 尺寸码 + 四种字节序前缀 |

这就是「8 字节浮点」的全部合法字符串说法。整个文件就是**对每种 dtype 重复这套列举**，例如布尔：

[numpy/_typing/_char_codes.py:3](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_char_codes.py#L3)

```python
type _BoolCodes = Literal["bool", "bool_", "?", "b1", "|b1", "=b1", "<b1", ">b1"]
```

布尔多了一个单字符码 `?` 和尺寸码 `b1`（`b` = boolean，1 字节）。注意布尔只有 1 字节，所以 `|b1`（字节序无关）是最自然的写法，而 `<b1`/`>b1` 虽被列出但运行时意义不大（**待本地验证**）。

datetime64 / timedelta64 是最「啰嗦」的叶子，因为它们还带**时间单位**后缀（`[Y]` 年、`[D]` 天、`[s]` 秒、`[ns]` 纳秒…），每个单位再配四种字节序前缀，于是单个类型的等价字符串能膨胀到几十个：

[numpy/_typing/_char_codes.py:64-69](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_char_codes.py#L64-L69)

```python
type _DT64Codes_int = Literal[
    "datetime64[ns]", "M8[ns]", "|M8[ns]", "=M8[ns]", "<M8[ns]", ">M8[ns]",
    "datetime64[ps]", "M8[ps]", "|M8[ps]", "=M8[ps]", "<M8[ps]", ">M8[ps]",
    ...
]  # fmt: skip
```

这里 `M8[ns]` 就是 datetime64（`M`）的 8 字节、纳秒单位写法。`# fmt: skip` 注释是告诉格式化工具「别动这几行的换行」。

#### 4.1.4 代码实践

> **实践目标**：亲手验证 `_Float64Codes` 里列出的 9 个字符串在运行时**确实都等价于 float64**，并观察字节序前缀如何反映到 `.byteorder` 属性。

```python
# 示例代码
import numpy as np

# 取自 _char_codes.py 第 27-29 行的 9 个等价编码
float64_codes = [
    "float64", "float", "double", "d", "f8", "|f8", "=f8", "<f8", ">f8",
]

for code in float64_codes:
    dt = np.dtype(code)
    print(f"{code:>10} -> {dt!r}  byteorder={dt.byteorder!r}  itemsize={dt.itemsize}")
```

**操作步骤**：

1. 把上面代码存为 `codes_demo.py`，运行 `python codes_demo.py`。

**需要观察的现象与预期结果**：

- 9 行的 `dtype` 都应打印为 `dtype('float64')`，`itemsize` 都为 `8`——证明它们运行时等价。
- `.byteorder`：`"<f8"` 应为 `"<"`、`">f8"` 应为 `">"`；`"f8"`、`"=f8"` 在小端机器上通常为 `"="`；`"|f8"` 的取值**待本地验证**（`|` 通常表示「字节序无关」）。

> 这 9 个字符串的「等价性」是 `_Float64Codes` 这个 `Literal` 存在的全部依据；类型检查器相信它们都代表 float64，正是因为它们在这里被逐一列举。`.byteorder` 的精确取值随平台/版本，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`_Float64Codes` 里的 `"d"`、`"f8"`、`"<f8"` 分别属于哪一类「零件」？

参考答案：`"d"` 是单字符码（`d` = double）；`"f8"` 是尺寸码（`f` + 8 字节）；`"<f8"` 是「尺寸码 + 小端序前缀」。

**练习 2**：四个字节序前缀 `|`、`=`、`<`、`>` 各代表什么？哪个最适合单字节类型？

参考答案：`<` 小端序、`>` 大端序、`=` 本机原生序、`|` 字节序不适用（not applicable）。单字节类型（如 bool、int8）字节序无意义，用 `|` 最自然。

---

### 4.2 _char_codes 的别名层次：从叶子到 _GenericCodes 的树

#### 4.2.1 概念说明

如果 `_char_codes.py` 只是把每种类型的编码平铺成一百多个独立 `Literal`，文件会既难读又难用。NumPy 的做法是**把它们组织成一棵分类树**：叶子是单个类型的编码（`_Float64Codes`、`_Int8Codes`…），中间节点按数值类别聚合（所有浮点 → `_FloatingCodes`，所有整数 → `_IntegerCodes`…），最终汇成一个总根 `_GenericCodes`（代表「任意 dtype 字符串」）。

为什么要建这棵树？两个理由：

1. **复用**：`_dtype_like.py` 需要的不是「所有 dtype」，而是「只要浮点」「只要整数」这样的**子集**来构建 `_DTypeLikeFloat`、`_DTypeLikeInt` 等窄化别名（见 u2-l2）。分类树的中间节点正好提供这些子集。
2. **可读性**：嵌套 `Literal` 让「float64 属于浮点、浮点属于不精确数、不精确数属于数」这种 is-a 关系一目了然，而不是把上百个字符串摊成一锅。

#### 4.2.2 核心流程：包含树与 float64 的路径

整棵树的骨架（只画关键路径）：

```
_GenericCodes                         # 任意 dtype 字符串（根）
├── _BoolCodes                        # 布尔
├── _NumberCodes                      # 所有数值
│   ├── _IntegerCodes                 #   所有整数
│   │   ├── _UnsignedIntegerCodes     #     无符号（uint8/16/32/64…）
│   │   └── _SignedIntegerCodes       #     有符号（int8/16/32/64…）
│   └── _InexactCodes                 #   所有不精确数（浮点 + 复数）
│       ├── _FloatingCodes            #     浮点（float16/32/64/longdouble）
│       │   └── _Float64Codes         #       ← "float64" 在这里
│       └── _ComplexFloatingCodes     #     复数（complex64/128…）
├── _FlexibleCodes                    # 可变长度（字符 + void）
├── _DT64Codes                        # datetime64
├── _TD64Codes                        # timedelta64
└── _ObjectCodes                      # object
```

于是 `"float64"` 的**包含路径**清晰可见：

\[
\texttt{\_Float64Codes} \;\subset\; \texttt{\_FloatingCodes} \;\subset\; \texttt{\_InexactCodes} \;\subset\; \texttt{\_NumberCodes} \;\subset\; \texttt{\_GenericCodes}
\]

即：`"float64"` 是浮点 → 浮点是不精确数 → 不精确数是数值 → 数值是泛型 dtype。这条路径意味着：任何接受 `_FloatingCodes` 的地方都接受 `"float64"`，任何接受 `_GenericCodes`（或更宽松的）的地方也都接受它。

#### 4.2.3 源码精读

叶子节点 `_Float64Codes` 已在 4.1.3 看过。它被聚合进 `_FloatingCodes`：

[numpy/_typing/_char_codes.py:129-134](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_char_codes.py#L129-L134)

```python
type _FloatingCodes = Literal[
    _Float16Codes,
    _Float32Codes,
    _Float64Codes,
    _LongDoubleCodes,
]
```

注意这里的写法：`Literal[_Float16Codes, _Float32Codes, ...]` 把**别的 `Literal` 别名**作为成员再塞进一个新的 `Literal`。这就是「嵌套 `Literal`」——它不是 `Union`，而是 `Literal of Literals`（4.3 会讲它俩在运行时的关键差别）。

继续往上，浮点和复数合成「不精确数」，不精确数和整数合成「数值」：

[numpy/_typing/_char_codes.py:141-142](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_char_codes.py#L141-L142)

```python
type _InexactCodes = Literal[_FloatingCodes, _ComplexFloatingCodes]
type _NumberCodes = Literal[_IntegerCodes, _InexactCodes]
```

而 `_IntegerCodes` 本身又是「无符号 + 有符号」两层叶子的聚合：

[numpy/_typing/_char_codes.py:140](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_char_codes.py#L140)

```python
type _IntegerCodes = Literal[_UnsignedIntegerCodes, _SignedIntegerCodes]
```

最终，所有大类汇成总根 `_GenericCodes`：

[numpy/_typing/_char_codes.py:147-156](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_char_codes.py#L147-L156)

```python
type _GenericCodes = Literal[
    _BoolCodes,
    _NumberCodes,
    _FlexibleCodes,
    _DT64Codes,
    _TD64Codes,
    _ObjectCodes,
    # TODO: add `_StringCodes` once it has a scalar type
    # _StringCodes,
]
```

注意末尾的 `TODO`：`_StringCodes`（对应新的可变长 `StringDType`）暂时**没有**加入 `_GenericCodes`。原因写在文件靠前：

[numpy/_typing/_char_codes.py:99-101](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_char_codes.py#L99-L101)

```python
# NOTE: `StringDType' has no scalar type, and therefore has no name that can
# be passed to the `dtype` constructor
type _StringCodes = Literal["T", "|T", "=T", "<T", ">T"]
```

`_char_codes` 里的每个 `_XxxCodes` 都默认「这个 dtype 有一个同名的标量类、可作 `np.dtype("名字")` 的人名」；而 `StringDType` 没有标量类型，无法用名字构造，所以即便定义了 `_StringCodes`，也暂不并入总根。这是一个反映「编码体系假设」的有趣边界。

datetime64 / timedelta64 也在叶子层就用了嵌套：按单位族（任意单位 / 日期单位 / 时间单位 / 整数单位）拆成几个子 `Literal`，再合成一个 `_DT64Codes`：

[numpy/_typing/_char_codes.py:70-75](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_char_codes.py#L70-L75)

```python
type _DT64Codes = Literal[
    _DT64Codes_any,
    _DT64Codes_date,
    _DT64Codes_datetime,
    _DT64Codes_int,
]
```

这套嵌套不只是为了好看——它正是 4.3 要讲的「扁平 `__args__`」红利的基础。

**消费方**。`_dtype_like.py` 在文件头导入了一组分类节点（不是全部叶子，而是按需取中间层）：

[numpy/_typing/_dtype_like.py:6-19](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_dtype_like.py#L6-L19)

```python
from ._char_codes import (
    _BoolCodes,
    _BytesCodes,
    _ComplexFloatingCodes,
    _DT64Codes,
    _FloatingCodes,
    _NumberCodes,
    ...
)
```

然后用它们拼出带精度信息的窄化别名，例如 `_DTypeLikeFloat` 复用 `_FloatingCodes`、`_DTypeLikeComplex_co` 复用 `_BoolCodes | _NumberCodes`：

[numpy/_typing/_dtype_like.py:82](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_dtype_like.py#L82)

```python
type _DTypeLikeFloat = type[float] | _DTypeLike[np.floating] | _FloatingCodes
```

[numpy/_typing/_dtype_like.py:86-88](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_dtype_like.py#L86-L88)

```python
type _DTypeLikeComplex_co = (
    type[complex] | _DTypeLike[np.bool | np.number] | _BoolCodes | _NumberCodes
)
```

这就把 4.2 的分类树和 u2-l2 的窄化别名接上了：树上的中间节点（`_FloatingCodes`、`_NumberCodes`…）就是窄化别名的「字符串构件」。

#### 4.2.4 代码实践

> **实践目标**：在源码里**手动追踪** `"float64"` 的包含路径，并用类型检查器验证「接受上层节点 = 接受下层叶子」。

```python
# 示例代码
import numpy as np
import numpy.typing as npt
from typing import assert_type, reveal_type

# 一个只接受「浮点 dtype 字符串」的函数（_FloatingCodes 的等价效果由 npt 间接提供）
def only_float(code: str) -> np.dtype[np.float64]:
    return np.dtype(code)

reveal_type(only_float("float64"))   # 上层 _FloatingCodes 接受 _Float64Codes 的成员
reveal_type(only_float("f8"))
```

**操作步骤**：

1. 先**纯阅读源码**，在 [`_char_codes.py`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_char_codes.py) 里按顺序点开：`_Float64Codes`（L27）→ `_FloatingCodes`（L129）→ `_InexactCodes`（L141）→ `_NumberCodes`（L142）→ `_GenericCodes`（L147），亲手确认 `"float64"` 出现在这条链的每一层里。
2. 把上面代码存为 `path_demo.py`，用 `mypy path_demo.py` 或 pyright 检查 `reveal_type` 的推断。

**需要观察的现象与预期结果**：

- 源码追踪：`"float64"` 字面量确实出现在 `_Float64Codes`（L28）；`_Float64Codes` 被列在 `_FloatingCodes`（L131）；`_FloatingCodes` 在 `_InexactCodes`（L141）；`_InexactCodes` 在 `_NumberCodes`（L142）；`_NumberCodes` 在 `_GenericCodes`（L149）。
- 类型检查：`only_float("float64")` 与 `only_float("f8")` 的返回类型应为 `np.dtype[np.float64]`；若你把参数标注换成更窄的内部 `_FloatingCodes`（需从 `numpy._typing` 导入，仅供实验），非法字符串如 `"int8"` 应被拒绝。

> 注：`_FloatingCodes` 等是私有名，正式代码不应直接导入；上面的 `only_float` 用公共 `str` 标注只是为了演示「字符串构件」的概念。是否能在你的环境导入私有 `_FloatingCodes` 做 isinstance/get_args 实验，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：写出 `"float64"` 从叶子到根 `_GenericCodes` 的完整包含路径。

参考答案：`_Float64Codes` ⊂ `_FloatingCodes` ⊂ `_InexactCodes` ⊂ `_NumberCodes` ⊂ `_GenericCodes`（对应源码 L27 → L129 → L141 → L142 → L147）。

**练习 2**：为什么 `_StringCodes` 没有被加入 `_GenericCodes`？

参考答案：`_char_codes` 的每个 `_XxxCodes` 都假设该 dtype 有同名标量类、可用名字传给 `np.dtype(...)` 构造；而 `StringDType` 没有标量类型，无法用名字构造（源码 L99-L100 注释）。因此即便定义了 `_StringCodes`，也暂不并入 `_GenericCodes`，留有 `TODO`（L154）等它有了标量类型再加。

---

### 4.3 Literal 原语：嵌套展平、去重与扁平 __args__

#### 4.3.1 概念说明

前两节都在讲「列了什么、怎么组织」，这一节讲「为什么用嵌套 `Literal` 而不是别的」。关键在于 `Literal` 这个类型原语（PEP 586）的一个运行时特性：**嵌套的 `Literal` 会被自动展平并去重**。

先回顾 `Literal` 本身：`Literal["a", "b"]` 是一个「取值只能是 `"a"` 或 `"b"`」的类型。当你写 `Literal[Literal["a","b"], Literal["b","c"]]` 时，类型检查器把它当成「取值是 `a`/`b`/`c`」——这是**类型层面**的等价。但在**运行时**，`typing` 模块构造嵌套 `Literal` 时会真的把它**展平**成一个单层 `Literal["a","b","c"]`，并且**去重**（`"b"` 只出现一次）。

`_char_codes.py` 用一段注释明确点出了这一设计取舍：

> Nested literals get flattened and de-duplicated at runtime, which isn't the case for a `Union` of `Literal`s. … they always have a "flat" `Literal.__args__`, which is a tuple of *literally* all its literal values.

翻译过来有三点：

1. 嵌套 `Literal` 在运行时**会**展平 + 去重。
2. `Union[Literal[...], Literal[...]]`（据源码所述）在运行时**不会**这样展平去重。
3. 二者在**类型检查**时等价，但**运行时**不同；嵌套的好处是始终拥有一个「扁平的 `__args__`」——一个包含**所有**字面值的元组。

第 3 点的实用价值是：任何时候你都能拿到 `_GenericCodes` 的扁平 `__args__`，遍历到「NumPy 认可的每一个 dtype 字符串」，方便做运行时校验、文档生成、测试覆盖等。

#### 4.3.2 核心流程：展平的直觉与 PEP 695 别名的内省

展平的直觉很直白：构造 `Literal[A, B]`（其中 `A`、`B` 也是 `Literal`）时，`typing` 递归地把 A、B 的成员「倒」进外层，合并、去重，最终外层只剩一个扁平的 `Literal`。结果就是：

- `Literal["a","b"].__args__` → `("a", "b")`
- `Literal[Literal["a","b"], Literal["b","c"]].__args__` → `("a", "b", "c")`（`"b"` 去重）

不过 `_char_codes.py` 用的是 PEP 695 `type` 语句，每个 `_XxxCodes` 在运行时是一个 `types.TypeAliasType`，真正的类型表达式挂在它的 `__value__` 上。所以要拿到扁平 `__args__`，得先「剥一层壳」：`_XxxCodes.__value__.__args__`，或用 `typing.get_args(_XxxCodes.__value__)`。

这个「剥壳」手法正是项目运行时测试用的套路——`test_runtime.py` 里的 `TypeTup.from_type_alias` 就是对 PEP 695 别名做 `alias.__value__` 再 `get_args`：

[numpy/typing/tests/test_runtime.py:25-30](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/test_runtime.py#L25-L30)

```python
@classmethod
def from_type_alias(cls, alias: TypeAliasType, /) -> Self:
    # PEP 695 `type _ = ...` aliases wrap the type expression as a
    # `types.TypeAliasType` instance with a `__value__` attribute.
    tp = alias.__value__
    return cls(typ=tp, args=get_args(tp), origin=get_origin(tp))
```

#### 4.3.3 源码精读

那段关于展平/去重的注释，紧跟在 `_StringCodes` 之后、所有聚合别名之前，相当于整棵树的「设计说明」：

[numpy/_typing/_char_codes.py:103-107](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_char_codes.py#L103-L107)

```python
# NOTE: Nested literals get flattened and de-duplicated at runtime, which isn't
# the case for a `Union` of `Literal`s.
# So even though they're equivalent when type-checking, they differ at runtime.
# Another advantage of nesting, is that they always have a "flat"
# `Literal.__args__`, which is a tuple of *literally* all its literal values.
```

正因为有这个保证，后面的 `_SignedIntegerCodes`、`_FloatingCodes`、`_GenericCodes` 等才敢于大量嵌套（见 4.2.3）——它们知道无论嵌多深，最终 `__args__` 都是扁平的。若改用 `Union[Literal[...], Literal[...]]`，据注释所述就享受不到这个扁平 `__args__`。

datetime64 的 `_DT64Codes`（L70-75）也是同款嵌套：四个按单位族拆分的子 `Literal` 合成一个，运行时展平成「所有 datetime64 字符串」的扁平元组——这正是 4.1 里 datetime64 叶子那么长的原因，它们需要被聚拢成一个可遍历的整体。

#### 4.3.4 代码实践

> **实践目标**：用 `typing.get_args` / `__args__` / `__value__` 在运行时观察「嵌套 `Literal` 被展平去重」这一行为，并复刻 `test_runtime.py` 的「剥壳」手法。

先用**最干净的直接 `Literal`**（不涉及 PEP 695 别名，行为稳定、版本无关）验证注释的核心论断：

```python
# 示例代码
from typing import Literal, Union, get_args

A = Literal["a", "b"]
B = Literal["b", "c"]          # 注意 "b" 与 A 重复，用来观察去重

nested = Literal[A, B]         # 嵌套 Literal
print("nested args:", get_args(nested))   # 预期 ('a', 'b', 'c') —— 展平且去重

u = Union[A, B]                # 对照：Union of Literals
print("union args :", get_args(u))        # 据源码注释所述不会同样展平去重
```

**操作步骤**：

1. 把上面代码存为 `flat_demo.py`，运行 `python flat_demo.py`。
2. 然后对真实的 `_char_codes` 别名复刻「剥壳」手法（私有导入，仅供实验）：

```python
# 示例代码
from typing import get_args
from numpy._typing._char_codes import _Float64Codes, _FloatingCodes

# PEP 695 别名：先 __value__ 剥壳，再取扁平 __args__
leaf_args   = get_args(_Float64Codes.__value__)
parent_args = get_args(_FloatingCodes.__value__)
print("leaf  :", leaf_args)      # 预期 9 个 float64 等价字符串
print("parent:", parent_args)    # 预期：所有浮点类型的字符串被展平进一个元组
print("leaf ⊆ parent?", set(leaf_args).issubset(parent_args))
```

**需要观察的现象与预期结果**：

- 第一段：`get_args(Literal[A, B])` 返回 `('a', 'b', 'c')`——三个值，`"b"` 只出现一次，证明「展平 + 去重」。
- 第一段的 `Union[A, B]`：据源码注释它不会同样展平去重；具体 `get_args` 返回形式随 Python 版本可能不同，**待本地验证**。
- 第二段：`_Float64Codes.__value__` 的扁平 `__args__` 应包含 4.1 列出的 9 个字符串；`_FloatingCodes.__value__` 的扁平 `__args__` 应是所有浮点（float16/32/64/longdouble）字符串的并集；`leaf ⊆ parent` 应为 `True`。

> 关于 PEP 695 `TypeAliasType` 成员在 `Literal[...]` 内是否在**所有** Python 版本都被完全解引用展平，存在版本细节；上面以「直接 `Literal`」演示的展平/去重是稳定结论，对 `_char_codes` 别名的精确 `__args__` 内容请以本地运行结果为准（**待本地验证**）。

#### 4.3.5 小练习与答案

**练习 1**：用一句话解释 `_char_codes.py` L103-L107 注释的核心论断。

参考答案：嵌套 `Literal` 在运行时会被展平并去重，并始终保持一个扁平的 `Literal.__args__`（包含所有字面值）；而 `Union` of `Literal`（据注释）不会这样。二者在类型检查时等价，运行时不同——这正是 `_char_codes` 选择「嵌套 `Literal`」而非「`Union`」的原因。

**练习 2**：为什么对 PEP 695 `type _Foo = Literal[...]` 定义的别名，直接 `get_args(_Foo)` 拿不到字面值？该怎么办？

参考答案：因为 PEP 695 的 `_Foo` 运行时是一个 `types.TypeAliasType`「壳」，真正的类型表达式在 `_Foo.__value__` 上；`get_args` 对壳本身返回空。需要先「剥壳」：`get_args(_Foo.__value__)` 或 `_Foo.__value__.__args__`。这正是 `test_runtime.py` 里 `TypeTup.from_type_alias` 先 `alias.__value__` 再 `get_args` 的原因。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成一个「dtype 字符串编码侦察器」小任务：

**任务**：写一个脚本，完成三件事——

1. **追踪路径**：在源码里确认 `"float64"` 的包含路径 `_Float64Codes → _FloatingCodes → _InexactCodes → _NumberCodes → _GenericCodes`，并打印这条路径上每层的源码行号。
2. **列等价串**：从 `_Float64Codes` 取出 9 个等价字符串，运行时逐一 `np.dtype(...)`，确认它们都得到 `dtype('float64')`。
3. **看展平**：用 `__value__` + `get_args` 观察 `_FloatingCodes` 的扁平 `__args__`，确认它把四种浮点（float16/32/64/longdouble）的字符串展平进了一个元组，且 `_Float64Codes` 的成员是其子集。

参考实现（「示例代码」）：

```python
# scout_codes.py —— 示例代码
from typing import get_args
import numpy as np
from numpy._typing._char_codes import (
    _Float64Codes, _FloatingCodes, _InexactCodes, _NumberCodes, _GenericCodes,
)

# 1) 包含路径（行号来自 _char_codes.py）
path = [
    ("_Float64Codes",  _Float64Codes,  "L27-29"),
    ("_FloatingCodes", _FloatingCodes, "L129-134"),
    ("_InexactCodes",  _InexactCodes,  "L141"),
    ("_NumberCodes",   _NumberCodes,   "L142"),
    ("_GenericCodes",  _GenericCodes,  "L147-156"),
]
print("== 包含路径 ==")
for name, alias, where in path:
    print(f"  {name:<16} ({where})")

# 2) 等价字符串
leaf = get_args(_Float64Codes.__value__)
print("\n== _Float64Codes 等价串 ==", leaf)
for code in leaf:
    assert np.dtype(code) == np.dtype("float64"), f"{code} 不等价！"
print("全部等价于 float64 ✓")

# 3) 展平去重
parent = get_args(_FloatingCodes.__value__)
print("\n== _FloatingCodes 扁平 __args__ 个数 ==", len(parent))
print("leaf ⊆ parent ?", set(leaf).issubset(parent))
```

**验收**：

- 第 1 部分打印的 5 个节点与 4.2.2 的树一致，行号与本讲引用的源码行号吻合。
- 第 2 部分 9 个字符串断言全部通过。
- 第 3 部分 `_FloatingCodes` 的扁平 `__args__` 个数大于 `_Float64Codes` 的 9（因为还含 float16/32/longdouble），且 `leaf ⊆ parent` 为 `True`。

> 第 2 部分的「9 个等价串」与第 3 部分的「扁平去重」是稳定结论；具体的 `__args__` 个数与顺序请以本地运行结果为准（**待本地验证**）。

## 6. 本讲小结

- **dtype 字符串编码**有可解剖的零件结构：人名（`float64`）、C 风格别名（`double`）、单字符码（`d`）、尺寸码（`f8`）、字节序前缀（`|`/`=`/`<`/`>`）。`_Float64Codes` 用一个 `Literal` 把 8 字节浮点的 9 种等价写法列全。
- 字节序前缀四符号：`<` 小端、`>` 大端、`=` 本机序、`|` 不适用（单字节类型最自然）。
- `_char_codes.py` 把上百个字符串编码组织成一棵**嵌套 `Literal` 分类树**：叶子是单类型（`_Float64Codes`…），中间节点按类别聚合（`_FloatingCodes`/`_IntegerCodes`/`_NumberCodes`…），根是 `_GenericCodes`。`"float64"` 的路径是 `_Float64Codes ⊂ _FloatingCodes ⊂ _InexactCodes ⊂ _NumberCodes ⊂ _GenericCodes`。
- 这棵树的中间节点正是 `_dtype_like.py` 构建窄化别名（`_DTypeLikeFloat` 等）的字符串构件，把「外松（公共 `str`）内紧（内部 `Literal`）」的分层落地。
- `Literal` 原语（PEP 586）的运行时红利：**嵌套 `Literal` 会被展平并去重**，始终保持扁平 `Literal.__args__`；源码 L103-107 注释说明这优于 `Union` of `Literal`。
- 内省 PEP 695 `type` 别名要先「剥壳」`alias.__value__`，再 `get_args`——这是 `test_runtime.py` 的 `TypeTup.from_type_alias` 的套路。
- 边界细节：`StringDType` 因无标量类型，`_StringCodes` 暂未并入 `_GenericCodes`（L99-101、L154 TODO）；datetime64/timedelta64 因带时间单位后缀而拥有最长的叶子编码列表。

## 7. 下一步学习建议

- **横向回到 u2-l2**：带着本讲的「分类树」重看 `_dtype_like.py` 的 `_DTypeLikeFloat` / `_DTypeLikeComplex_co`，你会一眼看出它们的字符串构件就是树上的 `_FloatingCodes` / `_NumberCodes`，两层设计彻底打通。
- **纵向进入 u3 单元（协议）**：本讲的 `Literal` 编码描述「字符串怎么说 dtype」，而 `_SupportsDType`/`_HasDType` 协议描述「对象怎么自带 dtype」，二者共同构成 `DTypeLike` 的精确内部表达。
- **运行时测试视角（u6-l2）**：本讲 4.3 的「剥壳 + get_args」手法在 `test_runtime.py` 被系统化用于校验公共别名；学完 u6-l2 你会更理解 NumPy 如何保证这些 PEP 695 别名在运行时可内省。
- **源码延伸阅读**：直接通读 [`numpy/_typing/_char_codes.py`](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_char_codes.py) 全文（157 行），重点对比叶子（L3-L101）、聚合层（L109-L156）与那段设计注释（L103-L107）；再对照 [`_dtype_like.py` 的导入块](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_dtype_like.py#L6-L19)看哪些分类节点被消费。
