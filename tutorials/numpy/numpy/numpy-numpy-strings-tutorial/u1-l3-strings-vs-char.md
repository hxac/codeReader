# numpy.strings 与 numpy.char 的关系

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 `numpy.char`（实现在 `numpy/_core/defchararray.py`）是如何「站在 `numpy.strings` 的肩膀上」复用全部字符串函数的。
- 动手对比 `np.strings.__all__` 与 `np.char.__all__`，准确列出 `char` 比 `strings` 多出来的成员，以及 `strings` 比 `char` 多出来的成员。
- 理解 `_join` / `_split` / `_rsplit` / `_splitlines` 这四个「下划线私有函数」为什么只活在 `numpy._core.strings` 里、又被 `char` 借去当公共接口。
- 解释并验证 `np.char.equal` 会先「去掉尾部空白」再比较（numarray 兼容行为），而 `np.strings.equal` 不会。
- 明白为什么官方推荐新代码用 `numpy.strings`，而 `numpy.char`（含 `chararray` 类）只为向后兼容而保留、并在 2.5 起逐步废弃。

---

## 2. 前置知识

本讲承接 [u1-l1 项目定位与门面架构](u1-l1-overview-and-facade.md)，默认你已经知道：

- **门面（facade）**：`numpy.strings` 本身不实现函数，只靠 `from numpy._core.strings import *` 转发符号。
- **实现层**：真正干活的代码在 `numpy/_core/strings.py`，以及它向下委托的 C/C++ ufunc。
- **`__all__`**：Python 模块里用来声明「公开 API 名单」的列表；`from module import *` 只会导入 `__all__` 里的名字（若定义了 `__all__`）。

本讲还要补充两个基础概念：

- **numarray**：NumPy 的「前辈」库之一。早期用户从 numarray 迁移过来时，习惯了「字符串比较前自动去掉尾部空白」这一行为。`numpy.char` 为了不打破这些老代码，把这个奇怪的行为保留了下来——这正是 `char` 与 `strings` 最关键的行为差异。
- **rstrip**：把字符串**末尾**的空白字符（空格、制表符、换行等）删掉。例如 `"aa ".rstrip()` 得到 `"aa"`。注意它只删尾部，不删头部。

> 一句话定位：`numpy.strings` 是「干净、面向未来」的命名空间；`numpy.char` 是「带历史包袱、保兼容」的老命名空间，两者**共享同一份底层实现**，但 `char` 在外面包了一层不同的语义。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `numpy/_core/defchararray.py` | `numpy.char` 的全部实现。通过 `import *` 复用 `numpy.strings`，再补回 `join/split` 等历史函数、`chararray` 类、以及「比较前 strip」的兼容比较运算。 |
| `numpy/_core/strings.py` | `numpy.strings` 的实现层。本讲重点看它的 `__all__`（尤其被注释掉的 `join/split`）与四个私有函数 `_join/_split/_rsplit/_splitlines`。 |
| `numpy/_core/multiarray.py` | 把 C 函数 `compare_chararrays` 的 `__module__` 改写为 `numpy.char`，使其归属正确。 |
| `numpy/_core/src/multiarray/multiarraymodule.c` | C 层 `compare_chararrays` 的实现，其第 4 个参数 `rstrip` 就是「比较前删尾部空白」的开关。 |

永久链接统一使用当前 HEAD `9559a6b1ac93610711d8f1243f8c949fca4420bb`。

---

## 4. 核心概念与源码讲解

### 4.1 复用机制：defchararray 如何「站在 strings 肩上」

#### 4.1.1 概念说明

`numpy.char` 并没有把字符串函数重新实现一遍。它的策略是：

1. 用 `from numpy.strings import *` 把 `numpy.strings` 的**全部公共函数**一次性搬过来。
2. 对于「行为要和老 numarray 保持一致」的函数（比较运算），**重新定义同名函数**覆盖掉刚搬过来的版本。
3. 对于「签名/返回值要换一种形态」的函数（`multiply`/`partition`/`rpartition`），先把 `numpy.strings` 的版本**改名导入**（如 `multiply as strings_multiply`），再写一个薄薄的包装函数去调用它。
4. 对于 `numpy.strings` 还没正式公开的 `join/split/rsplit/splitlines`，直接去**实现层** `numpy._core.strings` 借那四个下划线私有函数，改名后当公共接口用。

#### 4.1.2 核心流程

```text
numpy/_core/strings.py        ← 真正的实现（含私有 _join/_split/...）
        ↑ import *
numpy/strings/__init__.py     ← 门面：转发 46 个公共名字
        ↑ import *  +  改名导入
numpy/_core/defchararray.py   ← numpy.char：复用 + 覆盖 + 包装 + 借私有
        ↑ 模块别名 numpy.char
用户代码 np.char.xxx / np.strings.xxx
```

#### 4.1.3 源码精读

文件顶部的导入区把上述四步暴露得很清楚。注意第 30 行的 `import *` 与第 23–35 行的两段「改名导入」是配合使用的：

[numpy/_core/defchararray.py:23-35](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/defchararray.py#L23-L35) —— 先从**实现层**借四个私有切分函数（去掉下划线当公共名），再用别名从**门面**导入三个待包装函数。

```python
from numpy._core.strings import (
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

要点解读：

- `from numpy.strings import *`（第 30 行）把 `numpy.strings.__all__` 里的全部函数搬进 `defchararray` 的命名空间。
- 第 23–28 行**绕过门面**，直接到 `numpy._core.strings` 借 `_join/_split/_rsplit/_splitlines`，并把名字前的下划线去掉——这是 `char` 能提供 `join/split` 而 `strings` 暂时不能的关键（详见 4.3）。
- 第 31–35 行用 `strings_multiply` 这样的别名保留住 `numpy.strings` 的原始实现，供 4.5 节的包装函数调用。如果直接写 `from numpy.strings import multiply`，就会和本文件后面自己定义的 `def multiply(...)` 撞名，所以必须改名。

#### 4.1.4 代码实践

**实践目标**：验证 `numpy.char` 里的普通函数（如 `upper`）和 `numpy.strings` 里的确实是同一个对象，而非两份拷贝。

**操作步骤**（写一个脚本运行）：

```python
import numpy as np

# upper 没有被 char 覆盖，应该是同一个函数对象
print(np.char.upper is np.strings.upper)   # 预期 True
print(np.char.upper.__module__)            # 预期 numpy.strings（它原属 strings）
```

**需要观察的现象**：第一行打印 `True`，说明 `char.upper` 与 `strings.upper` 是**同一个对象**，`char` 没有复制实现。

**预期结果**：`True` 与 `numpy.strings`。

> 说明：`char.equal` 则不同——它在 `defchararray.py` 里被重新定义了（见 4.4），所以 `np.char.equal is np.strings.equal` 预期为 `False`。

#### 4.1.5 小练习与答案

**练习 1**：为什么第 31–35 行要用 `multiply as strings_multiply` 这种别名导入，而不是直接 `from numpy.strings import multiply`？

**参考答案**：因为本文件稍后（4.5 节）要**自己定义一个** `def multiply(a, i)`，用来把 `TypeError` 转成 `ValueError`。如果直接 `import multiply`，这个名字会在定义新函数时被覆盖，原始的 `numpy.strings.multiply` 就再也引用不到了；用别名 `strings_multiply` 保留住原始实现，供包装函数内部调用。

**练习 2**：`from numpy.strings import *` 会把哪些名字搬进 `defchararray`？

**参考答案**：会搬进 `numpy.strings.__all__` 中列出的全部 46 个公共名字（因为门面文件定义了 `__all__`）。注意：它**不会**搬进 `_join/_split` 等下划线私有名字——所以 `char` 想用它们必须像第 23–28 行那样**显式到实现层去借**。

---

### 4.2 `__all__` 对比：char 多出什么、strings 多出什么

#### 4.2.1 概念说明

要精确回答「`char` 比 `strings` 多了哪些成员」，最可靠的办法是直接读两个文件的 `__all__` 列表做集合运算。结论是：

- `char` 比 `strings` **多 8 个**成员：
  - 切分族 4 个：`join`、`split`、`rsplit`、`splitlines`（`strings` 里被注释掉、尚未公开）。
  - `chararray` 专属 4 个：`array`、`asarray`、`compare_chararrays`、`chararray`。
- `strings` 比 `char` **多 1 个**成员：`slice`（`char` 没有跟进这个较新的函数）。

#### 4.2.2 核心流程

```text
char.__all__   =  共享部分(45)  ∪  {join,split,rsplit,splitlines,array,asarray,compare_chararrays,chararray}
strings.__all__ =  共享部分(45)  ∪  {slice}
```

即两者有 45 个公共名字；`char` 多 8 个，`strings` 多 1 个，分别对应 53 与 46 的总规模。

#### 4.2.3 源码精读

先看 `numpy.strings` 的公开名单，注意最后两行注释——`join/split/rsplit/splitlines` 是**故意被移出**的，原因是「行为还没定型」：

[numpy/_core/strings.py:73-90](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L73-L90) —— `strings` 的 `__all__`，末尾注释说明四个切分函数暂不公开。

```python
__all__ = [
    # UFuncs
    "equal", "not_equal", "less", ..., "zfill", "partition", "rpartition", "slice",

    # _vec_string - Will gradually become ufuncs as well
    "upper", "lower", "swapcase", "capitalize", "title",

    # _vec_string - Will probably not become ufuncs
    "mod", "decode", "encode", "translate",

    # Removed from namespace until behavior has been crystallized
    # "join", "split", "rsplit", "splitlines",
]
```

再看 `char` 的名单，它把这四个名字**正式列了进去**，并额外加上 `chararray` 专属的四个：

[numpy/_core/defchararray.py:40-50](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/defchararray.py#L40-L50) —— `char` 的 `__all__`，多了切分族与 `chararray` 专属成员。

```python
__all__ = [
    'equal', 'not_equal', ..., 'join', ..., 'rsplit',
    'rstrip', 'split', 'splitlines', 'startswith', 'strip', 'swapcase',
    'title', 'translate', 'upper', 'zfill', 'isnumeric', 'isdecimal',
    'array', 'asarray', 'compare_chararrays', 'chararray'
    ]
```

两张表对比，差集就一目了然了。注意 `slice` 只出现在 `strings.__all__`，所以 `np.char.slice` 并不存在。

#### 4.2.4 代码实践

**实践目标**：用代码算出 `char` 与 `strings` 的 `__all__` 差集，验证 4.2.1 的结论。

**操作步骤**：

```python
import numpy as np

s = set(np.strings.__all__)
c = set(np.char.__all__)

print("strings 总数:", len(s))          # 预期 46
print("char    总数:", len(c))          # 预期 53
print("char 多出的:", sorted(c - s))    # 预期含 join/split/rsplit/splitlines 等 8 个
print("strings 多出的:", sorted(s - c)) # 预期 ['slice']
```

**需要观察的现象**：`char 多出的` 应包含 8 个名字；`strings 多出的` 应只有 `slice`。

**预期结果**：

```text
char 多出的: ['array', 'asarray', 'chararray', 'compare_chararrays', 'join', 'rsplit', 'split', 'splitlines']
strings 多出的: ['slice']
```

#### 4.2.5 小练习与答案

**练习 1**：既然 `defchararray.py` 第 30 行 `from numpy.strings import *` 已经搬来了 `strings` 的全部公共名字，为什么 `char.__all__` 还要把它们**重新列一遍**？

**参考答案**：因为 `from X import *` 只是「把名字灌进当前模块的命名空间」，它**并不会**自动生成当前模块的 `__all__`。`char` 需要明确声明自己的公开 API（尤其要加上 `join/split/chararray` 等 `strings` 没有的名字），所以必须自己维护一份完整的 `__all__`，既覆盖被搬来的名字，也覆盖自有的名字。

**练习 2**：用户调用 `np.char.slice(...)` 会发生什么？为什么？

**参考答案**：会抛 `AttributeError`。因为 `slice` 既不在 `char.__all__` 里，`from numpy.strings import *` 也只搬 `strings.__all__` 里的名字（`slice` 在其中，会被搬进 `defchararray` 命名空间）——但 `numpy.char` 对外暴露的接口由它自己的 `__all__` 决定，而 `char.__all__` 没有 `slice`，所以它不是 `char` 的公共 API。若想用切片，应使用 `np.strings.slice`。（注：`slice` 是否能通过 `np.char.slice` 访问取决于门面如何对外公开 `__all__`，推荐一律用 `np.strings.slice`。）

---

### 4.3 私有函数 `_join` / `_split` / `_rsplit` / `_splitlines`

#### 4.3.1 概念说明

这四个函数在 `numpy/_core/strings.py` 里都以**下划线开头**命名（`_join`、`_split`……），表示「这是内部实现，还不是稳定的公共 API」。它们的共同特点是：**返回的是「长度不一的列表」数组**（object dtype），而定长字符串 dtype（`str_`/`bytes_`）无法用一个固定宽度装下「每行切出来的段数不同」的结果。

正因为输出形态「还没定型」，`numpy.strings` 暂时不敢把它们公开（见 4.2 的注释）；但 `numpy.char` 出于历史兼容**必须**提供 `split` 等接口，于是直接到实现层把这四个私有函数借来用。

#### 4.3.2 核心流程

以 `_split` 为例，它把每个字符串元素交给 Python 内置的 `str.split`，再把结果（一个 `list`）塞进 object 数组：

```text
_split(a, sep, maxsplit)
   └─ _vec_string(a, np.object_, 'split', [sep] + 干净化的参数)
        └─ 对每个元素调用 Python 的 str.split
   └─ 返回 dtype=object 的数组，每个元素是一个 list（长度可不同）
```

注意 `_clean_args(maxsplit)` 的作用：Python 的 `str.split(sep, maxsplit)` 不接受 `None` 表示「用默认值」，所以要把 `None` 及其后的参数整体砍掉。

#### 4.3.3 源码精读

[numpy/_core/strings.py:1399-1441](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1399-L1441) —— `_split` 的实现，注释解释了为什么只能返回 object 数组。

```python
@array_function_dispatch(_split_dispatcher)
def _split(a, sep=None, maxsplit=None):
    ...
    # This will return an array of lists of different sizes, so we
    # leave it as an object array
    return _vec_string(
        a, np.object_, 'split', [sep] + _clean_args(maxsplit))
```

另外三个结构完全对称：

- `_rsplit`（[numpy/_core/strings.py:1444-1487](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1444-L1487)）：从右侧切，调用 Python `str.rsplit`。
- `_splitlines`（[numpy/_core/strings.py:1494-1529](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1494-L1529)）：按行切，调用 `str.splitlines`。
- `_join`（[numpy/_core/strings.py:1358-1392](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1358-L1392)）：用分隔符把一个序列拼回字符串，调用 `str.join`，并通过 `_to_bytes_or_str_array` 还原回原 dtype。

`_clean_args` 的去 `None` 逻辑在这里：

[numpy/_core/strings.py:127-141](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L127-L141) —— 遇到第一个 `None` 就截断，因为 Python 字符串方法用「缺省」而非 `None` 表示默认值。

```python
def _clean_args(*args):
    newargs = []
    for chk in args:
        if chk is None:
            break
        newargs.append(chk)
    return newargs
```

#### 4.3.4 代码实践

**实践目标**：亲手看到「`split` 返回的是 object 数组、里面是长度不同的 list」，理解它为什么暂不进 `numpy.strings`。

**操作步骤**：

```python
import numpy as np

a = np.array(["Numpy is nice!", "hello world"])
r = np.char.split(a, " ")
print(r.dtype)        # 预期 object
print(list(r))        # 预期 [list(['Numpy', 'is', 'nice!']), list(['hello', 'world'])]
# 两个元素切出的段数不同（3 段 vs 2 段），无法存进定长 str_/bytes_
```

**需要观察的现象**：`r.dtype` 是 `object`；两个 list 长度分别是 3 和 2。

**预期结果**：`object`，以及两个长度不同的列表。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_split` 不像 `center` 那样返回定长的 `str_` 数组？

**参考答案**：`center` 的输出宽度可以提前用 `str_len` 算出一个最大值，所以能开出定长缓冲区；而 `split` 每个元素切出的段数和每段长度都不同，**没有一个统一的固定宽度**能装下所有结果，只能退而求其次用 object 数组装 `list`。这正是注释「will return an array of lists of different sizes」的含义。

**练习 2**：`_clean_args(None, 5)` 返回什么？为什么 `_split` 调用时要写 `[sep] + _clean_args(maxsplit)`？

**参考答案**：返回 `[]`（空列表），因为遇到第一个 `None` 就立即截断。写 `[sep] + _clean_args(maxsplit)` 是为了保证：当 `maxsplit` 为 `None` 时只把 `sep` 传给 `str.split`（让它走默认的「按空白切、不限次数」）；当 `maxsplit` 给了整数时，才把 `[sep, maxsplit]` 都传过去。

---

### 4.4 numarray 兼容行为：比较前先 strip 尾部空白

#### 4.4.1 概念说明

这是 `char` 与 `strings` **最容易被踩坑**的差异：

- `np.strings.equal(x, y)`：逐元素判断 `x == y`，**原样比较**，尾部空格算字符串的一部分。
- `np.char.equal(x, y)`：先把 `x`、`y` 的**尾部空白**去掉，再比较。

后者的目的是兼容 numarray 时代的老代码——那时候定长字符串数组常用空格右填充，用户期望 `'aa'` 和 `'aa '` 被视为相等。`numpy.strings` 作为面向未来的命名空间，去掉了这个隐式行为，要求显式比较。

这一行为对六个比较运算**全部生效**：`equal`、`not_equal`、`greater`、`greater_equal`、`less`、`less_equal`。

#### 4.4.2 核心流程

`char` 的每个比较函数都把工作转交给 C 函数 `compare_chararrays`，并把第 4 个参数 `rstrip` 恒为 `True`：

```text
np.char.equal(x1, x2)
   └─ compare_chararrays(x1, x2, '==', rstrip=True)
         └─ 对每对元素先 rstrip 尾部空白，再用 '==' 比较
         └─ 返回 bool 数组
```

而 `np.strings.equal` 直接是 NumPy 顶层的 `equal` ufunc（`strings.py` 第 12 行从 `numpy` 导入），不做任何 strip。

#### 4.4.3 源码精读

[numpy/_core/defchararray.py:61-92](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/defchararray.py#L61-L92) —— `char.equal` 的实现，最后一行把 `rstrip` 写死为 `True`。

```python
@array_function_dispatch(_binary_op_dispatcher)
def equal(x1, x2):
    """
    Return (x1 == x2) element-wise.

    Unlike `numpy.equal`, this comparison is performed by first
    stripping whitespace characters from the end of the string.  This
    behavior is provided for backward-compatibility with numarray.
    ...
    """
    return compare_chararrays(x1, x2, '==', True)
```

其余五个比较函数结构完全一样，只是把操作符字符串换成 `'!='`、`'>'`、`'>='`、`'<'`、`'<='`（见 [numpy/_core/defchararray.py:95-263](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/defchararray.py#L95-L263)）。

`compare_chararrays` 是 C 实现，其第 4 个参数在源码里就叫 `rstrip`：

[numpy/_core/src/multiarray/multiarraymodule.c:3884-3903](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/src/multiarray/multiarraymodule.c#L3884-L3903) —— C 函数签名，关键字参数表里第 4 项是 `rstrip`。

```c
static PyObject *
compare_chararrays(PyObject *NPY_UNUSED(dummy), PyObject *args, PyObject *kwds)
{
    ...
    npy_bool rstrip;
    ...
    static char *kwlist[] = {"a1", "a2", "cmp", "rstrip", NULL};

    if (!PyArg_ParseTupleAndKeywords(args, kwds, "OOs#O&:compare_chararrays",
                kwlist, &array, &other, &cmp_str, &strlength,
                PyArray_BoolConverter, &rstrip)) {
```

最后，为了让这个 C 函数对外显示成 `numpy.char` 的成员，`multiarray.py` 改写了它的 `__module__`：

[numpy/_core/multiarray.py:78](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/multiarray.py#L78) —— 把 `compare_chararrays` 归属到 `numpy.char`。

```python
compare_chararrays.__module__ = 'numpy.char'
```

#### 4.4.4 代码实践

**实践目标**：用同一个输入证明 `np.char.equal` 与 `np.strings.equal` 行为不同。

**操作步骤**：

```python
import numpy as np

x = np.array("aa")      # 2 个字符
y = np.array("aa ")     # 2 个字符 + 1 个尾部空格

print("strings.equal:", np.strings.equal(x, y))   # 预期 False（原样比较）
print("char.equal:   ", np.char.equal(x, y))      # 预期 True（先 rstrip 再比较）

# 验证原理：char.equal 等价于先 rstrip 再比
print("手动验证:     ", np.strings.equal(x, y.rstrip()))  # 预期 True
```

**需要观察的现象**：`strings.equal` 给 `False`，`char.equal` 给 `True`，且「手动 rstrip 后再比」与 `char.equal` 结果一致。

**预期结果**：

```text
strings.equal: False
char.equal:    True
手动验证:      True
```

> 该行为在 `defchararray.py` 的 `equal` 文档示例（第 82–86 行）中也有同样演示，可对照阅读。

#### 4.4.5 小练习与答案

**练习 1**：`np.char.not_equal("a ", "a")` 返回什么？解释原因。

**参考答案**：返回 `array(False)`。因为 `char` 的比较会先把两边尾部空白去掉，`"a "` rstrip 后变成 `"a"`，与 `"a"` 相等，所以「不相等」为假。

**练习 2**：如果想让 `np.strings` 的比较也具备「忽略尾部空白」的效果，应该怎么写？

**参考答案**：显式地先 strip 再比，例如 `np.strings.equal(np.strings.rstrip(a), np.strings.rstrip(b))`。这正是 `numpy.strings` 的设计哲学——把行为交给用户显式控制，而不是像 `char` 那样隐式地帮你做。

---

### 4.5 包装函数与 chararray 类：multiply / partition / chararray

#### 4.5.1 概念说明

除了「比较前 strip」，`char` 还在三个方面对 `strings` 做了包装或补充：

1. **`multiply`**：`char` 版本把「乘以非整数」的 `TypeError` 转成更友好的 `ValueError`，仅为兼容老接口。
2. **`partition` / `rpartition`**：`strings` 版本返回一个**三元组**（三段各一个数组）；`char` 版本用 `np.stack` 把三段沿新维度拼成一个**二维数组**（每个输入元素对应 3 个输出元素）。
3. **`chararray` 类 + `array`/`asarray` 工厂**：这是 numarray 时代的「字符串专用数组类型」，会在**取值时自动 rstrip**、在比较时自动 strip，并提供 `.upper()` 等方法和 `+ * %` 运算符。从 NumPy 2.5 起，`chararray` 与 `array`/`asarray` 均**已废弃**（deprecated）。

#### 4.5.2 核心流程

```text
char.multiply(a, i):
   try: strings_multiply(a, i)
   except TypeError: raise ValueError("Can only multiply by integers")

char.partition(a, sep):
   三元组 = strings_partition(a, sep)
   return np.stack(三元组, axis=-1)        # (N,3) 二维数组

chararray 对象:
   arr[i]      → 自动对取出的字符串做 .rstrip()
   arr == x    → 走 char.equal（自动 strip）
   arr + x     → 走 add；arr * n → multiply；arr % v → mod
```

#### 4.5.3 源码精读

`char.multiply` 把异常类型做了一次转译：

[numpy/_core/defchararray.py:266-315](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/defchararray.py#L266-L315) —— 调用别名保留的 `strings_multiply`，出错时转成 `ValueError`。

```python
@set_module("numpy.char")
def multiply(a, i):
    """...This is a thin wrapper around np.strings.multiply that raises
    `ValueError` when ``i`` is not an integer. ..."""
    try:
        return strings_multiply(a, i)
    except TypeError:
        raise ValueError("Can only multiply by integers")
```

`char.partition` 把三元组堆叠成二维：

[numpy/_core/defchararray.py:318-357](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/defchararray.py#L318-L357) —— 调用 `strings_partition` 后沿最后一维 `stack`。

```python
@set_module("numpy.char")
def partition(a, sep):
    """..."""
    return np.stack(strings_partition(a, sep), axis=-1)
```

`chararray` 类是 `ndarray` 的子类，并在 `__getitem__` 里偷偷 rstrip，这就是它「取值自动去尾空白」的来源：

[numpy/_core/defchararray.py:595-599](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/defchararray.py#L595-L599) —— 每次取出标量字符串都先 `rstrip()`。

```python
def __getitem__(self, obj):
    val = ndarray.__getitem__(self, obj)
    if isinstance(val, character):
        return val.rstrip()
    return val
```

它的比较运算符全部委托给本模块的 `equal`/`less` 等（因此也带 strip 行为）：

[numpy/_core/defchararray.py:606-614](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/defchararray.py#L606-L614) —— `chararray.__eq__` 直接调 `equal`。

```python
def __eq__(self, other):
    return equal(self, other)
```

类与工厂函数的废弃声明见文档字符串里的 `.. deprecated:: 2.5` 标记（[numpy/_core/defchararray.py:411-414](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/defchararray.py#L411-L414) 与 [numpy/_core/defchararray.py:1224-1227](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/defchararray.py#L1224-L1227)）。

#### 4.5.4 代码实践

**实践目标**：对比 `char.multiply` 的异常类型，以及 `partition` 在 `strings` 与 `char` 下返回形态的差异；并观察 `chararray` 的废弃警告。

**操作步骤**：

```python
import numpy as np
import warnings

# 1) multiply 异常类型不同
a = np.array(["ab"])
try:
    np.strings.multiply(a, "x")   # strings 直接抛 TypeError
except TypeError as e:
    print("strings 抛:", type(e).__name__)

try:
    np.char.multiply(a, "x")      # char 转成 ValueError
except ValueError as e:
    print("char    抛:", type(e).__name__, "->", e)

# 2) partition 返回形态不同
x = np.array(["Numpy is nice!"])
print("strings.partition 类型:", type(np.strings.partition(x, " ")).__name__)  # tuple
print("char.partition 形状:   ", np.char.partition(x, " ").shape)             # (1, 3)

# 3) chararray 已废弃
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    ca = np.char.array(["hi", "ok"])
    print("chararray 是否触发 DeprecationWarning:",
          any(issubclass(x.category, DeprecationWarning) for x in w))
```

**需要观察的现象**：

1. `strings.multiply` 抛 `TypeError`，`char.multiply` 抛 `ValueError`。
2. `strings.partition` 返回 `tuple`（三元组），`char.partition` 返回形状为 `(1, 3)` 的二维数组。
3. 创建 `chararray` 会触发 `DeprecationWarning`（**待本地验证**：取决于运行时的警告过滤默认设置；若未触发，可显式 `warnings.simplefilter("always")` 后再观察）。

**预期结果**：

```text
strings 抛: TypeError
char    抛: ValueError -> Can only multiply by integers
strings.partition 类型: tuple
char.partition 形状:    (1, 3)
chararray 是否触发 DeprecationWarning: True
```

#### 4.5.5 小练习与答案

**练习 1**：`np.strings.partition(x, " ")` 与 `np.char.partition(x, " ")` 各返回什么类型？内容上有什么联系？

**参考答案**：前者返回一个 **tuple**，含三个独立数组（分隔符前、分隔符本身、分隔符后）；后者返回一个 **二维 ndarray**（形状多了一维、大小为 3），内容上就是把前者那三个数组沿新维度 `np.stack(..., axis=-1)` 拼起来。所以 `char.partition` 的第 `i` 行就是 `strings.partition` 三元组的三个元素。

**练习 2**：为什么 `chararray.__getitem__` 要对取出的标量做 `rstrip()`？

**参考答案**：因为 `chararray` 的存储仍是定长 `str_`/`bytes_`，不够长的字符串会用空格在**尾部**填充。如果不 rstrip，`arr[0]` 取出的可能是 `"hi   "` 而不是 `"hi"`。`rstrip()` 让用户拿到的字符串「看起来」是去掉填充后的原始值——这是 numarray 时代的设计约定，也是 `chararray` 被废弃的原因之一（它隐藏了存储细节，容易让人困惑）。

---

## 5. 综合实践

把本讲知识串起来，完成下面这个小任务：

**任务**：写一份「`numpy.strings` 迁移指南」小脚本，帮一个正在把代码从 `np.char` 迁到 `np.strings` 的同事自动发现潜在的行为差异。

要求脚本至少完成三件事：

1. **API 缺失检查**：打印出 `np.char` 有、而 `np.strings` 没有的成员（提示：用 4.2 的集合差集），并逐个标注它属于「切分族」还是「chararray 专属」。
2. **比较行为差异演示**：构造一个带尾部空格的字符串数组，分别用 `np.strings.equal` 和 `np.char.equal` 比较，把两个结果的差异打印出来，并在注释里写明「差异来自 `compare_chararrays` 的 `rstrip=True`」。
3. **废弃提醒**：检测用户代码里是否用了 `np.char.array` / `np.char.asarray` / `np.char.chararray`，打印一句「自 NumPy 2.5 起已废弃，请改用普通 `ndarray` + `np.strings`」。

参考骨架（请你补全注释与输出解读）：

```python
import numpy as np

# 1) API 缺失检查
strings_names = set(np.strings.__all__)
char_names = set(np.char.__all__)
char_only = char_names - strings_names
split_family = {"join", "split", "rsplit", "splitlines"}
chararray_family = {"array", "asarray", "compare_chararrays", "chararray"}
# TODO: 把 char_only 分类打印

# 2) 比较行为差异
a = np.array(["cat", "dog "])
b = np.array(["cat", "dog"])
# TODO: 对比 np.strings.equal 与 np.char.equal，并解释差异来源

# 3) 废弃提醒（伪代码：扫描源文件里出现的关键字即可）
for name in ["np.char.array", "np.char.asarray", "np.char.chararray"]:
    print(f"提醒：若代码中使用了 {name}，自 NumPy 2.5 起已废弃。")
```

完成后，你应当能用一句话回答：「把代码从 `np.char` 搬到 `np.strings` 时，需要警惕哪三类问题？」（答案：① 用到了 `split/join` 等切分族时 `strings` 暂无对应公共函数；② 依赖了「比较忽略尾部空白」的隐式行为；③ 用了 `chararray`/`array`/`asarray` 这类已废弃类型。）

---

## 6. 本讲小结

- `numpy.char`（`defchararray.py`）通过 `from numpy.strings import *` **复用** `strings` 的全部公共函数，自己不重复实现。
- `char.__all__` 比 `strings.__all__` **多 8 个**成员（`join/split/rsplit/splitlines` + `array/asarray/compare_chararrays/chararray`），而 `strings` 多一个 `slice`。
- `join/split/rsplit/splitlines` 在实现层是**下划线私有**的 `_join/_split/...`，因为它们返回「长度不一的列表」object 数组，行为尚未定型，`strings` 暂不公开，只被 `char` 借用。
- 六个比较运算在 `char` 里会**先去掉尾部空白**（`compare_chararrays(..., rstrip=True)`），这是 numarray 兼容行为；`strings` 原样比较，不做 strip。
- `char` 对 `multiply`（TypeError→ValueError）、`partition`/`rpartition`（三元组→二维数组）做了薄包装。
- `chararray` 类、`array`/`asarray` 工厂自 NumPy 2.5 起**已废弃**；新代码应使用普通 `ndarray` + `numpy.strings`。

---

## 7. 下一步学习建议

- 进入第 2 单元，从 [u2-l4 装饰器与分发机制：set_module 与 array_function_dispatch](u2-l4-decorators-and-dispatch.md) 开始，搞清楚本讲反复出现的 `@set_module("numpy.char")`、`@array_function_dispatch(...)` 这两个装饰器到底在做什么。
- 想深入了解 `_join/_split` 背后的逐元素调用机制，可先读 [u2-l8 大小写转换与 _vec_string 委托](u2-l8-case-and-vecstring.md)，再到专家单元 [u3-l15 _vec_string 的 C 实现](u3-l15-vecstring-c-impl.md)。
- 若好奇 `compare_chararrays` 这类 C 函数如何注册到模块、`__module__` 改写发生在哪一步，可带着这个问题去读 `numpy/_core/src/multiarray/multiarraymodule.c` 的方法注册表（本讲引用的第 4759–4760 行附近）。
