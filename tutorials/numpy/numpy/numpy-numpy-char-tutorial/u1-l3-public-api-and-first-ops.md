# 公共 API 全貌与第一次向量字符串操作

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 `numpy.char` 暴露了哪些公共函数，并能按「比较 / 查询 / 变换 / 拼接 / 对齐 / 修剪 / 拆分 / 编码」等类别对它们归类。
- 理解 `numpy.char` 的公共 API 其实来自两个地方：`numpy._core.defchararray`（本地定义）和 `numpy.strings`（再导出）。
- 第一次亲手写出元素级（element-wise）的向量字符串调用：`upper`、`add`、`center`、`multiply`。
- 看懂这些函数的**返回 dtype** 是怎么算出来的（保持原宽度 / 求和 / 取最大宽度）。

本讲只要求你「会用」，不讲模块加载与弃用机制的内部细节——那是第二单元（u2）的事。

## 2. 前置知识

在开始前，请确认你已经理解下面几个概念（都在前两讲出现过，这里只做最短的复习）：

- **元素级（element-wise）**：函数作用在数组的**每一个元素**上，就像对单个字符串调用 Python 的 `str` 方法，但一次处理整个数组。例如 `upper` 会把 `['a', 'b']` 变成 `['A', 'B']`，而不是把整个数组拼成一个字符串。
- **dtype 与 itemsize**：NumPy 的字符串数组是「定宽」的，每个槽位一样长。`<U5` 表示 Unicode 字符串、每元素最多 5 个字符（每字符 4 字节，所以 itemsize = 20）；`|S3` 表示字节串、每元素最多 3 个字节（itemsize = 3）。详见 u1-l2。
- **三层关系（char → defchararray → strings）**：`numpy.char` 只是一个门面；它的函数绝大多数来自 `numpy._core.defchararray`，而后者又通过 `from numpy.strings import *` 把很多函数直接转发给现代层 `numpy.strings`。详见 u1-l1。
- **向量化（vectorized）**：不需要写 `for` 循环，一行调用就处理整个数组，底层是 C 实现的 ufunc 或 `_vec_string`，比 Python 循环快得多。

> 一个一句话心智模型：`numpy.char` 给你的是「对字符串数组做 `str` 方法的批量化版本」，返回值仍然是 NumPy 数组。

## 3. 本讲源码地图

本讲涉及的文件不多，但每个都扮演明确角色：

| 文件 | 在本讲中的作用 |
| --- | --- |
| `numpy/char/__init__.pyi` | 类型存根，列出了 `numpy.char` 对外公开的全部名字（`__all__`），是我们盘点「公共 API 全貌」的权威清单。 |
| `numpy/_core/defchararray.py` | 真正的实现层。它的 `__all__` 决定了门面暴露什么；其中 `multiply`、`partition`、`rpartition` 是本地定义的「包装函数」，其余大多从 `numpy.strings` 再导出。 |
| `numpy/_core/strings.py` | 现代层。`upper`、`center`、`add` 等函数的本体在这里，本讲会精读它们的返回 dtype 逻辑。 |

注意：后两个文件不在 `numpy/char/` 目录下，所以下面的永久链接会指向 `numpy/_core/` 路径，但都使用当前 HEAD 提交，保证可点击、可追溯。

## 4. 核心概念与源码讲解

本讲拆成两个最小模块：

- **4.1 公共 API 全貌与分类**：先把 `numpy.char` 到底有哪些函数看清楚。
- **4.2 第一次向量字符串操作**：再亲手调用 `upper`、`add`、`center`、`multiply`，并搞懂返回 dtype。

### 4.1 公共 API 全貌与分类（公共 API 表）

#### 4.1.1 概念说明

当你 `import numpy.char as char` 之后，能用哪些函数？这个问题最权威的答案是 `numpy.char.__all__`。它是一个字符串列表，列出了模块「对外承诺稳定」的名字。

在 `numpy.char` 里，`__all__` 并不是在 `__init__.py` 里重新声明的，而是直接从实现层搬过来的：

```python
from numpy._core.defchararray import __all__, __doc__
```

[numpy/char/__init__.py:1](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/char/__init__.py#L1) —— 门面把 `__all__` 和 `__doc__` 整体转交，所以「`numpy.char` 有哪些公共名字」这个问题，答案 100% 由 `defchararray` 决定。

而 `defchararray` 里这个清单是这样写的（共 **53** 个名字）：

```python
__all__ = [
    'equal', 'not_equal', 'greater_equal', 'less_equal',
    'greater', 'less', 'str_len', 'add', 'multiply', 'mod', 'capitalize',
    'center', 'count', 'decode', 'encode', 'endswith', 'expandtabs',
    'find', 'index', 'isalnum', 'isalpha', 'isdigit', 'islower', 'isspace',
    'istitle', 'isupper', 'join', 'ljust', 'lower', 'lstrip', 'partition',
    'replace', 'rfind', 'rindex', 'rjust', 'rpartition', 'rsplit',
    'rstrip', 'split', 'splitlines', 'startswith', 'strip', 'swapcase',
    'title', 'translate', 'upper', 'zfill', 'isnumeric', 'isdecimal',
    'array', 'asarray', 'compare_chararrays', 'chararray'
    ]
```

[numpy/_core/defchararray.py:L40-L50](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L40-L50) —— 这 53 个名字就是 `len(numpy.char.__all__)` 的结果。

> 小提示：类型存根 `__init__.pyi` 里也有一份几乎一致的 `__all__`（同样 53 项），它主要给 IDE 和类型检查器用，但同样可以帮助你「一眼看全」整个 API：
> [numpy/char/__init__.pyi:L57-L111](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/char/__init__.pyi#L57-L111)

#### 4.1.2 核心流程：把 53 个名字归类

53 个函数记不住没关系。它们几乎都是 Python 内置 `str` / `bytes` 方法的「数组版」，可以按功能归成 8 类：

| 类别 | 函数 | 对应 Python 方法 |
| --- | --- | --- |
| 比较 | `equal`, `not_equal`, `greater`, `greater_equal`, `less`, `less_equal`, `compare_chararrays` | `==` `!=` `>` `>=` `<` `<=`（注意有「尾部空白剥离」特殊语义，见 u2-l3） |
| 长度与查找 | `str_len`, `count`, `find`, `rfind`, `index`, `rindex`, `startswith`, `endswith` | `len`, `count`, `find/rfind`, `index/rindex`, `startswith/endswith` |
| 字符类别判断 | `isalnum`, `isalpha`, `isdigit`, `islower`, `isspace`, `istitle`, `isupper`, `isnumeric`, `isdecimal` | `isalnum`, `isalpha`, … |
| 大小写变换 | `upper`, `lower`, `capitalize`, `swapcase`, `title` | `upper`, `lower`, `capitalize`, `swapcase`, `title` |
| 拼接与格式化 | `add`, `multiply`, `mod`, `join`, `replace`, `translate`, `expandtabs`, `zfill` | `+`, `*`, `%`, `join`, `replace`, `translate`, `expandtabs`, `zfill` |
| 对齐填充 | `center`, `ljust`, `rjust` | `center`, `ljust`, `rjust` |
| 修剪 | `strip`, `lstrip`, `rstrip` | `strip`, `lstrip`, `rstrip` |
| 拆分 | `split`, `rsplit`, `splitlines`, `partition`, `rpartition` | `split`, `rsplit`, `splitlines`, `partition`, `rpartition` |
| 编码转换 | `decode`, `encode` | `bytes.decode`, `str.encode` |
| 工厂与类（遗留） | `array`, `asarray`, `chararray` | 构造字符串数组 / `chararray` 类（NumPy 2.5 起弃用） |

归类后你会发现：除最后三类里的 `array`/`asarray`/`chararray` 是「构造工具」外，其余都是「对已存在的字符串数组做变换或查询」的自由函数。这正是 u1-l1 强调的官方建议——**新代码用普通 `str_`/`bytes_` 数组 + 自由函数**，而不要用 `chararray`。

#### 4.1.3 源码精读：这些函数从哪里来？

虽然都在 `__all__` 里，但这 53 个函数的「出生地」并不一样。看 `defchararray.py` 顶部的 import：

```python
from numpy._core.multiarray import compare_chararrays
from numpy.strings import (
    _join as join,
    _rsplit as rsplit,
    _split as split,
    _splitlines as splitlines,
)
from numpy._utils import set_module
from numpy.strings import *
from numpy.strings import (
    multiply as strings_multiply,
    partition as strings_partition,
    rpartition as strings_rpartition,
)
```

[numpy/_core/defchararray.py:L23-L35](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L23-L35) —— 关键点：

1. `from numpy.strings import *`：把 `numpy.strings` 里的绝大多数函数（`upper`、`center`、`add`、`capitalize`、`find`、`count` …）**原封不动**地搬进 `defchararray` 的命名空间。这就是为什么 `np.char.upper` 和 `np.strings.upper` 其实是**同一个对象**（u2-l2 会专门讲这条委托关系）。
2. `compare_chararrays` 来自 C 层 `numpy._core.multiarray`，是六个比较函数的底层。
3. `multiply`、`partition`、`rpartition` 在本地被「重新定义」（用 `strings_multiply` 等别名保留了原始版本），目的是做一点兼容性包装——这是 u2-l5 的主题，本讲只需知道它们存在。

> 对本讲而言，你只要记住一句话：**绝大多数 `np.char.xxx` 函数，真正的函数体在 `numpy.strings`（也就是 `numpy/_core/strings.py`）里。** 所以我们下一节会去 `strings.py` 看源码，而不是去 `defchararray.py`。

#### 4.1.4 代码实践：核对公共 API

1. **实践目标**：亲手确认 `numpy.char` 的公共 API 数量，并验证分类表里的名字真的都能访问到。
2. **操作步骤**：
   ```python
   import numpy.char as nchar
   print(len(nchar.__all__))            # 预期 53
   # 抽查几个不同类别的函数是否都存在
   for name in ["upper", "add", "center", "multiply", "compare_chararrays", "chararray"]:
       print(name, "->", hasattr(nchar, name))
   ```
3. **需要观察的现象**：`len(nchar.__all__)` 输出 `53`；六个 `hasattr` 检查全部为 `True`。
4. **预期结果**：`53`，且每个名字都可访问（访问 `chararray` 会触发 `DeprecationWarning`，但 `hasattr` 仍为 `True`——关于这点详见 u2-l1）。
5. 关于「访问 `chararray` 会报弃用警告」这一现象，本讲**待本地验证**你的 NumPy 版本是否 ≥ 2.5；若版本较低则不会触发警告。

#### 4.1.5 小练习与答案

**练习 1**：`numpy.char` 的 `__all__` 里有几个函数名同时出现在 Python 内置 `str` 的方法列表里？请举出 3 个「名字完全一样」的例子。

> **参考答案**：绝大多数。例如 `upper`、`center`、`strip`、`split`、`replace`、`find` 等，名字与 `str` 的方法一一对应；区别在于它们是**元素级**作用在数组上的。

**练习 2**：`compare_chararrays` 和 `str_len` 分别属于哪一类？它们在 `defchararray.py` 里是「本地定义」还是「再导出」？

> **参考答案**：`compare_chararrays` 属于「比较」类，来自 C 层 `numpy._core.multiarray`（见 `defchararray.py` 第 22 行 import）；`str_len` 属于「长度与查找」类，通过 `from numpy.strings import *` 再导出，本体是 `numpy/_core/umath` 里的 ufunc。

---

### 4.2 第一次向量字符串操作（向量化调用）

#### 4.2.1 概念说明

「会用」`numpy.char` 的核心，是理解**元素级（element-wise）语义**和**返回 dtype**。我们用四个最常用的函数建立直觉：

| 函数 | 作用 | 输入 | 返回 dtype 怎么定 |
| --- | --- | --- | --- |
| `upper(a)` | 每个元素转大写 | 一个字符串数组 | **保持输入的 dtype 与宽度**（如 `<U5` 进、`<U5` 出） |
| `add(x1, x2)` | 对应元素拼接 | 两个数组 | 宽度 = 两个输入宽度之**和**（如 `<U1` + `<U2` → `<U3`） |
| `center(a, width)` | 每个元素居中到 `width` 宽 | 数组 + 目标宽度 | 宽度 = `max(原长度, width)`（如 `width=9` → `<U9`） |
| `multiply(a, i)` | 每个元素自我重复 `i` 次 | 数组 + 整数（或整数数组） | 宽度按最大重复次数推算（如重复 3 次 → `<U3`） |

注意「返回 dtype」不是随便定的：定宽数组必须提前知道每个槽多长，所以 NumPy 会**根据最坏情况预留宽度**。`upper` 不改变长度所以宽度不变；`center`/`multiply` 会改变长度所以宽度要重算。这是 u1-l2 讲过的 itemsize 思想在函数层面的体现。

#### 4.2.2 核心流程：一次调用的内部步骤

以 `np.char.upper(a)` 为例，执行过程大致是：

1. `numpy.char.upper` 通过模块级 `__getattr__` 找到 `defchararray.upper`（u2-l1 详讲）。
2. `defchararray.upper` 其实就是 `numpy.strings.upper`（再导出）。
3. `numpy.strings.upper` 把输入 `np.asarray(a)` 转成数组，然后调用 C 层 `_vec_string(a_arr, a_arr.dtype, 'upper')`。
4. `_vec_string` 对每个元素调用对应的 Python `str.upper`，写回到一个同 dtype 的输出数组里。
5. 返回新数组，**dtype 与输入相同**。

伪代码（只示意，非项目原码）：

```
def upper(a):                      # 示例代码
    a_arr = np.asarray(a)
    return _vec_string(a_arr, a_arr.dtype, 'upper')   # 每个元素 .upper()，写回同 dtype 数组
```

#### 4.2.3 源码精读

**(1) `upper`：保持原 dtype。** 真实定义在 `numpy/_core/strings.py`：

```python
@set_module("numpy.strings")
@array_function_dispatch(_unary_op_dispatcher)
def upper(a):
    """..."""
    a_arr = np.asarray(a)
    return _vec_string(a_arr, a_arr.dtype, 'upper')
```

[numpy/_core/strings.py:L1084-L1119](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/strings.py#L1084-L1119) —— 重点看最后两行：传入的 dtype 是 `a_arr.dtype`（即输入自身的 dtype），所以输出宽度等于输入宽度。文档示例也印证了这一点：

```python
>>> c = np.array(['a1b c', '1bca', 'bca1']); c
array(['a1b c', '1bca', 'bca1'], dtype='<U5')
>>> np.strings.upper(c)
array(['A1B C', '1BCA', 'BCA1'], dtype='<U5')   # 仍是 <U5
```

[numpy/_core/strings.py:L1111-L1115](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/strings.py#L1111-L1115)

> 顺带一提：`@set_module("numpy.strings")` 把这个函数的 `__module__` 改写成 `numpy.strings`，于是它的文档里显示的模块就是 `numpy.strings`。`numpy.char` 的版本则通过另一套装饰器显示为 `numpy.char`——这是 u2-l4 的内容，本讲不用深究。

**(2) `center`：宽度取 `max(原长, width)`。** 同样来自 `strings.py`：

```python
    fillchar = fillchar.astype(a.dtype, copy=False)
    width = np.maximum(str_len(a), width)
    out_dtype = f"{a.dtype.char}{width.max()}"
    shape = np.broadcast_shapes(a.shape, width.shape, fillchar.shape)
    out = np.empty_like(a, shape=shape, dtype=out_dtype)

    return _center(a, width, fillchar, out=out)
```

[numpy/_core/strings.py:L751-L757](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/strings.py#L751-L757) —— 关键是这一行：

```python
out_dtype = f"{a.dtype.char}{width.max()}"
```

它用 `a.dtype.char`（例如 `'U'`）拼上「最终宽度」，得到形如 `'U9'` 的输出 dtype。而 `width` 已经被 `np.maximum(str_len(a), width)` 抬高过——意思是「如果原字符串比目标 `width` 还长，就不缩短，按原长居中（等于不变）」。文档示例：

```python
>>> c = np.array(['a1b2','1b2a','b2a1','2a1b'])   # dtype '<U4'
>>> np.strings.center(c, width=9)
array(['   a1b2  ', '   1b2a  ', '   b2a1  ', '   2a1b  '], dtype='<U9')
```

[numpy/_core/strings.py:L726-L729](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/strings.py#L726-L729) —— 输出是 `<U9`，正好等于 `max(4, 9) = 9`。

**(3) `add`：宽度求和，它本身是个 ufunc。** `add` 在 `strings.py` 里并不是用 `def` 定义的，而是直接从 `numpy` 导入的 C 级 ufunc：

```python
from numpy import (
    add,
    equal,
    ...
)
```

[numpy/_core/strings.py:L10-L19](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/strings.py#L10-L19) —— 也就是说 `np.char.add`（经由 `defchararray` → `numpy.strings` → `numpy`）最终落到 NumPy 通用的 `add` ufunc 上。对字符串 dtype，这个 ufunc 的语义是「逐元素拼接」，输出宽度是两个输入宽度之和。例如 `<U1` 的 `['a']` 与 `<U2` 的 `['bc']` 相加，结果是 `<U3` 的 `['abc']`。

**(4) `multiply`：本地包装，把异常翻译成 `ValueError`。** 这是少数在 `defchararray.py` 里**本地定义**的函数之一：

```python
@set_module("numpy.char")
def multiply(a, i):
    """..."""
    try:
        return strings_multiply(a, i)
    except TypeError:
        raise ValueError("Can only multiply by integers")
```

[numpy/_core/defchararray.py:L266-L315](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L266-L315) —— 真正干活的还是 `strings_multiply`（即 `numpy.strings.multiply`，见 import 时的 `multiply as strings_multiply`）。`char` 版只是把「乘以非整数」时的 `TypeError` 改写成更友好的 `ValueError`。它的文档给出了清晰的返回 dtype 示例：

```python
>>> a = np.array(["a", "b", "c"])
>>> np.strings.multiply(a, 3)
array(['aaa', 'bbb', 'ccc'], dtype='<U3')
>>> i = np.array([1, 2, 3])
>>> np.strings.multiply(a, i)
array(['a', 'bb', 'ccc'], dtype='<U3')
```

[numpy/_core/defchararray.py:L294-L300](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L294-L300) —— 注意：即使用数组 `i = [1, 2, 3]` 让每个元素重复不同次数，输出仍是定宽 `<U3`（按**最大**重复次数 3 预留），重复少的元素右侧用空格补齐。这正是「定宽数组」的本质。

> 小知识：`partition` 和 `rpartition` 也是本地包装函数，它们用 `np.stack(..., axis=-1)` 给结果多加一个长度为 3 的维度（前/分隔符/后）。本讲不展开，留给 u2-l5。

#### 4.2.4 代码实践（本讲主实践）

1. **实践目标**：给定一个字符串数组，分别用 `np.char.upper`、`np.char.add`、`np.char.center`、`np.char.multiply` 完成大写化、拼接、居中填充与重复拼接，**打印结果与 dtype**，并对照 `multiply` 文档示例验证多元素拼接的返回宽度。
2. **操作步骤**：
   ```python
   import numpy as np
   import numpy.char as nchar

   a = np.array(["a1b c", "1bca", "bca1"])          # dtype '<U5'

   # (1) 大写化：保持原 dtype
   up = nchar.upper(a)
   print("upper      :", up, up.dtype)               # 预期 <U5

   # (2) 拼接：宽度求和
   ad = nchar.add(a, np.array(["zz", "zz", "zz"]))   # <U5 + <U2 -> <U7
   print("add        :", ad, ad.dtype)

   # (3) 居中填充：宽度 = max(原长, width)
   ce = nchar.center(a, 9, fillchar="*")
   print("center(9,*) :", ce, ce.dtype)              # 预期 <U9

   # (4) 重复拼接：对照 multiply 文档示例
   m1 = nchar.multiply(np.array(["a", "b", "c"]), 3)
   print("multiply x3:", m1, m1.dtype)               # 预期 ['aaa' 'bbb' 'ccc'] <U3
   m2 = nchar.multiply(np.array(["a", "b", "c"]), np.array([1, 2, 3]))
   print("multiply vec:", m2, m2.dtype)              # 预期 ['a' 'bb' 'ccc'] <U3
   ```
3. **需要观察的现象**：
   - `upper` 输出仍是 `<U5`，内容全大写。
   - `add` 输出是 `<U7`（5 + 2）。
   - `center(..., 9, '*')` 输出是 `<U9`，两侧用 `*` 居中填充。
   - `multiply` 标量 `3` 与向量 `[1,2,3]` 两种写法，输出都是定宽 `<U3`。
4. **预期结果**（基于上文引用的源码文档示例）：
   ```
   upper      : ['A1B C' '1BCA' 'BCA1'] <U5
   add        : ['a1b czz' '1bcazz' 'bca1zz'] <U7
   center(9,*) : ['**a1b c**' '**1bca***' '**bca1***'] <U9
   multiply x3: ['aaa' 'bbb' 'ccc'] <U3
   multiply vec: ['a' 'bb' 'ccc'] <U3
   ```
   > 说明：`center` 在总填充为奇数时，左侧会比右侧多/少一个字符（与 Python `str.center` 的左右分配规则一致）；`add` 的拼接结果右侧可能出现填充空格，因为定宽槽位按最大长度预留。`center` 与 `add` 的**精确字符布局**建议在本地跑一遍确认。
5. **若无法运行**：明确标注「待本地验证」。你可以退而求其次，直接阅读上文引用的 `upper`（`<U5`）、`center`（`<U9`）、`multiply`（`<U3`）三条源码文档示例，它们已经把「返回 dtype 怎么算」这件事讲清楚了。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `np.char.upper(a)` 返回的 dtype 宽度和输入一样，而 `np.char.center(a, 9)` 返回的宽度变成了 9？

> **参考答案**：`upper` 不会改变字符串长度，所以 `_vec_string(a_arr, a_arr.dtype, 'upper')` 直接复用输入 dtype；`center` 会把字符串扩展到目标宽度，源码里 `out_dtype = f"{a.dtype.char}{width.max()}"` 会按 `max(原长, width)` 重算宽度，因此 `width=9` 时输出是 `<U9`。

**练习 2**：`np.char.multiply(np.array(['a','b','c']), np.array([1,2,3]))` 的结果是定宽的还是变长的？为什么 `'a'` 只重复 1 次却没有报错？

> **参考答案**：是**定宽**的，dtype 为 `<U3`。NumPy 按**最大重复次数**（这里是 3）预留宽度，重复次数少的元素（`'a'` 只重复 1 次）右侧用空格补齐到 3 个字符，所以不会报错也不会变长。这是定宽字符串数组的根本特性（见 u1-l2）。

**练习 3**：`np.char.add` 的函数体并不在 `defchararray.py` 里。请根据本讲源码，说出它真正的「身份」。

> **参考答案**：`np.char.add` 经由 `defchararray` 的 `from numpy.strings import *` 来到 `numpy.char`；而在 `numpy/_core/strings.py` 第 10–19 行，`add` 是直接 `from numpy import add` 导入的，所以它最终是 NumPy 通用的 C 级 `add` **ufunc**，对字符串 dtype 的语义是逐元素拼接。

## 5. 综合实践

把本讲的知识串起来，做一个「迷你数据清洗」小任务。

**任务**：有一组带噪声的字符串数据，要求一步把它「清洗 + 格式化」成统一宽度的大写标签。

输入（模拟数据，建议本地运行）：

```python
import numpy as np
import numpy.char as nchar

raw = np.array([" abc ", "xy", "def"])     # 注意第一个带空格、长度不一
```

要求：

1. 用 `nchar.strip` 去掉首尾空白。
2. 用 `nchar.upper` 转大写。
3. 用 `nchar.center` 把每条统一到宽度 6、用 `.` 填充。
4. 在每一步都 `print(..., arr.dtype)`，观察 dtype 是否变化。
5. 最后把结果与一个前缀数组用 `nchar.add` 拼接，例如前缀 `np.array(["ID-", "ID-", "ID-"])`，观察最终 dtype 宽度。

**思考要点**（对照本讲）：

- `strip` / `upper` 不改变最大长度，dtype 宽度应当**保持**；`center` 会把宽度抬到 6。
- 拼接前缀后，最终宽度应当 ≈ `6 + len("ID-")` = 9，你可以用源码里 `add` 是宽度求和的规律来预测。
- 如果你把 `raw` 换成 `np.char.chararray` 构造的数组，会看到 `DeprecationWarning`——这正是 u1-l1 提到的弃用信号，本任务刻意使用普通 `np.array` 来避开它。

> 字符布局的精确结果（尤其 `center` 的左右填充分配）请以本地运行为准，标注「待本地验证」亦可。

## 6. 本讲小结

- `numpy.char` 的公共 API 由 `defchararray.__all__` 决定，共 **53** 个名字；门面 `__init__.py` 只是把它整体转交。
- 这 53 个函数可按比较 / 查询 / 变换 / 拼接 / 对齐 / 修剪 / 拆分 / 编码 / 工厂九大类归类，绝大多数是 Python `str` 方法的「元素级数组版」。
- 这些函数大多通过 `from numpy.strings import *` 再导出，真正函数体在 `numpy/_core/strings.py`；只有 `multiply`、`partition`、`rpartition`（以及 `chararray`、`array`、`asarray`）在 `defchararray` 本地定义。
- 元素级调用的返回 dtype 有三种典型规则：保持原宽（`upper`）、宽度求和（`add`）、取最大宽度（`center`、`multiply`）。
- `multiply` 在 `char` 层是 `numpy.strings.multiply` 的薄包装，作用是把「乘以非整数」的 `TypeError` 翻译成 `ValueError`。
- 新代码应使用普通 `str_`/`bytes_` 数组 + `numpy.char`/`numpy.strings` 自由函数，避免已弃用的 `chararray`。

## 7. 下一步学习建议

你已经会「用」`numpy.char` 了。接下来：

- 想知道「`np.char.upper` 是怎么在 `import` 时被找到的」「为什么访问 `chararray` 会报警告」，请进入 **u2-l1（惰性加载 `__getattr__` 与弃用警告）**。
- 想彻底搞清 `char` 与 `strings` 的委托关系，确认 `np.char.upper is np.strings.upper`，请看 **u2-l2（从 numpy.strings 再导出）**。
- 想理解「比较函数为何要先剥离尾部空白」、以及 `compare_chararrays` 的比较码，请看 **u2-l3（字符串比较运算符与 compare_chararrays）**。
- 在那之前，建议你随手跑一遍本讲的代码实践，亲眼看到 `<U5`、`<U9`、`<U3` 这些 dtype，再读源码会顺畅很多。
