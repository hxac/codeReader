# array / asarray 工厂函数源码精读

## 1. 本讲目标

上一讲（u3-l2）我们读了 `chararray` 的运算符与方法委托。本讲回到「`chararray` 从哪里被造出来」这个问题：在 `defchararray.py` 里，`chararray` 类本身的构造函数并不被推荐直接使用，官方入口是两个工厂函数 **`numpy.char.array`** 与 **`numpy.char.asarray`**。本讲只盯住它们。

学完本讲，你应该能够：

- 说清 `array()` 与 `asarray()` 的签名差异，以及 `asarray` 为何只是 `array(copy=False)` 的一行包装。
- 画出 `array()` 处理 `str` / `bytes` / `list` / `tuple` / `character` 型 `ndarray` / `object` 型 `ndarray` 这五类输入时的**分支流程**，并指出每条分支落到哪段源码。
- 解释 `itemsize` 与 `unicode` 在 `itemsize is None` 时的自动推断规则，尤其是 `str_` 输入为何要做 `itemsize //= 4`（把「字节数」换算回「字符数」）。
- 读懂「是否拷贝」的四条件判定，并能指出 `asarray` 在 `bytes_` 数组上可以真正做到零拷贝、而在 `str_` 数组上由于单位不一致通常仍会拷贝这一**容易踩坑的细节**。
- 用 `str` / `bytes` / `list` / `object` 数组分别调用 `np.char.array`，记录每条分支的 `dtype` 与 `itemsize`，并验证 `asarray` 的拷贝行为。

## 2. 前置知识

本讲承接前几讲的结论，不再重复推导：

- **三层关系**（u1-l1）：`numpy.char` 是门面，实现都在 `numpy/_core/defchararray.py`；访问 `np.char.array` / `np.char.asarray` 时，模块级 `__getattr__` 会先发 `DeprecationWarning` 再转发到底层 `defchararray.array` / `defchararray.asarray`。
- **字符串 dtype 与 itemsize**（u1-l2）：`str_`（kind `U`，UCS-4，4 字节/字符）、`bytes_`（kind `S`，1 字节/字符），共同基类 `character`。定宽公式
  \[
  \text{itemsize}_{\text{字节}} = \text{字符数}\times c,\quad c\in\{1\ (\text{bytes\_}),\ 4\ (\text{str\_})\}.
  \]
  `chararray.__new__` 收到的 `itemsize` 是**字符数**，`ndarray.itemsize` 属性却是**字节数**——本讲的 `÷4` 正是用来在这两种「单位」之间换算。
- **`chararray` 子类化**（u3-l1）：`chararray` 是 `ndarray` 子类，构造走 `__new__`，`obj.view(chararray)` 可以零拷贝地把普通 `character` 型数组「看作」`chararray`。
- **`@set_module("numpy.char")`**（u2-l4）：只改写 `__module__`，让定义在 `_core` 的函数对外伪装成 `numpy.char` 成员，不参与运算分发。
- **软弃用**（u2-l1）：`array` / `asarray` / `chararray` 同属 `__DEPRECATED` 集合，本讲所有动手实践都建议先用 `warnings.simplefilter("ignore", DeprecationWarning)` 或 `pytest` 的 `filterwarnings` 标记压制该警告。

本讲需要补充的新概念：

- **工厂函数（factory function）**：不直接暴露构造函数，而用一个「把各种杂乱输入都归一化成目标类型」的函数来创建对象。`array()` 就是 `chararray` 的工厂。
- **输入归一化（input normalization）**：调用方可能传入 Python 标量、列表、元组、各种 dtype 的 `ndarray`……工厂函数的第一职责是把它们都识别一遍、转成统一中间形态，再交给真正的构造逻辑。
- **零拷贝（zero-copy）**：在不复制底层内存的前提下返回结果（通常靠 `view()`）。`asarray` 的设计目标就是「能不拷贝就不拷贝」。

## 3. 本讲源码地图

| 文件 | 关键位置 | 作用 |
| --- | --- | --- |
| `numpy/_core/defchararray.py` | `array()`，L1220–L1364 | 主工厂函数，包含五条输入分支与拷贝判定 |
| `numpy/_core/defchararray.py` | `asarray()`，L1367–L1429 | 一行包装：`return array(obj, itemsize, copy=False, ...)` |
| `numpy/_core/defchararray.py` | 导入别名，L37–L38 | `narray`/`asnarray`/`ndarray` 与 `bytes_`/`character`/`str_` 的来源 |
| `numpy/_core/defchararray.py` | `chararray.__new__`，L550–L580 | 工厂函数在「标量字符串」分支里真正调用的构造器 |
| `numpy/char/__init__.py` | 模块级 `__getattr__`，L6–L25 | 访问 `np.char.array` 时注入 `DeprecationWarning` 的钩子 |
| `numpy/_core/tests/test_defchararray.py` | `TestBasic`，L22–L92 | 覆盖 object 数组、bytes 数组、unicode 数组、标量等输入的工厂测试 |

> 说明：本讲永久链接全部指向当前 HEAD `4e7f3b33df3e5ed2e9f46f6febdee62364520c70`。注意 `array` / `asarray` 的真实定义在 `numpy/_core/defchararray.py`，`numpy/char/__init__.py` 只是转发门面。

## 4. 核心概念与源码讲解

### 4.1 工厂函数的定位：array / asarray 是 chararray 的官方入口

#### 4.1.1 概念说明

`chararray` 类虽然有自己的构造函数 `chararray(shape, itemsize=1, unicode=False, ...)`，但它的文档字符串明确写着：

> chararrays should be created using `numpy.char.array` or `numpy.char.asarray`, rather than this constructor directly.

也就是说，直接 `chararray((3,), itemsize=5)` 是「内部用法」，对外推荐用 `array()` / `asarray()`。原因有二：

1. `chararray.__new__` 的参数是底层的 `shape` / `itemsize` / `buffer`，对调用方不友好；而 `array()` 接受的是「自然」的输入——Python 字符串、列表、各种 `ndarray`。
2. `array()` 会自动推断 `itemsize` 和 `unicode`，省去手算字符宽度。

`array` 与 `asarray` 的唯一实质差异是**默认是否拷贝**：

| 函数 | `copy` 默认 | 语义 |
| --- | --- | --- |
| `array(obj, ...)` | `copy=True` | 默认返回一份新拷贝 |
| `asarray(obj, ...)` | （无 `copy` 形参）恒为 `copy=False` | 尽量不拷贝，能 `view` 就 `view` |

两者在 NumPy 2.5 都已被软弃用（与 `chararray` 同属 `__DEPRECATED`），官方推荐改用普通 `str_` / `bytes_` `ndarray`。

#### 4.1.2 核心流程

```text
调用 np.char.asarray(obj)
        │  （__init__.py 的 __getattr__ 先发 DeprecationWarning）
        ▼
defchararray.asarray(obj, itemsize, unicode, order)
        │
        ▼
return array(obj, itemsize, copy=False, unicode=unicode, order=order)
        │
        ▼
进入 array() 的五条分支（见 4.2）
```

`asarray` 不做任何独立逻辑，只是把 `copy` 钉死为 `False` 后复用 `array()`。所以理解了 `array()`，就理解了 `asarray()`。

#### 4.1.3 源码精读

先看两个函数的签名与装饰器：

[numpy/_core/defchararray.py:1220-1221](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L1220-L1221) —— `array(obj, itemsize=None, copy=True, unicode=None, order=None)`，`@set_module("numpy.char")` 把它伪装成 `numpy.char` 成员。注意 `copy` 默认 `True`、`unicode` 默认 `None`（表示「待自动推断」）。

[numpy/_core/defchararray.py:1367-1368](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L1367-L1368) —— `asarray(obj, itemsize=None, unicode=None, order=None)`。`asarray` **没有** `copy` 形参。

`asarray` 的整个函数体只有一行：

[numpy/_core/defchararray.py:1428-1429](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L1428-L1429) —— `return array(obj, itemsize, copy=False, unicode=unicode, order=order)`。这就是「`asarray` = `array(copy=False)`」的全部秘密。

再看导入别名，它们决定了工厂函数内部「真正干活」的底层是谁：

[numpy/_core/defchararray.py:37-38](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L37-L38) —— `from .numeric import array as narray, asarray as asnarray, ndarray`，`from .numerictypes import bytes_, character, str_`。`narray` 是「普通」的 `numpy._core.numeric.array`，`asnarray` 是对应的 `asarray`。工厂函数内部用这两个别名去构造/转换普通 `ndarray`，避免与自身重名。

最后是弃用钩子：

[numpy/char/__init__.py:6-18](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/char/__init__.py#L6-L18) —— 模块级 `__getattr__` 命中 `__DEPRECATED = {"chararray", "array", "asarray"}` 时，先 `warnings.warn(..., DeprecationWarning, stacklevel=2)`，再照常返回底层对象。所以无论 `array` 还是 `asarray`，只要经 `np.char.` 访问就会先收到一条「The chararray class is deprecated …」警告，但功能不受影响。

#### 4.1.4 代码实践

1. **实践目标**：确认 `array` 与 `asarray` 的 `__module__` 都被改写成 `numpy.char`，且 `asarray` 内部确实委托给 `array`。
2. **操作步骤**（示例代码，需压制弃用警告）：

   ```python
   import warnings
   warnings.simplefilter("ignore", DeprecationWarning)
   import numpy as np

   print(np.char.array.__module__)     # 预期: numpy.char
   print(np.char.asarray.__module__)   # 预期: numpy.char
   print(np.char.array.__defaults__)   # 观察 copy 的默认值位置
   ```
3. **需要观察的现象**：两个函数的 `__module__` 都显示 `numpy.char`（尽管定义在 `numpy/_core/defchararray.py`）。
4. **预期结果**：`numpy.char` / `numpy.char`。
5. 若你的环境过滤了 `DeprecationWarning`，访问 `np.char.array` 可能不报警——这正常，不影响返回值。

#### 4.1.5 小练习与答案

- **练习 1**：为什么不直接写 `np.char.chararray((3,), itemsize=5)`，而要绕一道 `np.char.array`？
  - **答案**：`chararray.__new__` 的形参是底层的 `shape`/`itemsize`/`buffer`，需要调用方自己算字符宽度、自己准备数据；`array()` 接受自然的 Python 对象并自动推断 `itemsize` 与 `unicode`，更安全也更易用。官方文档也因此把 `array`/`asarray` 列为推荐入口。
- **练习 2**：`asarray` 没有 `copy` 参数，它是如何实现「尽量不拷贝」的？
  - **答案**：它在函数体里直接 `return array(obj, itemsize, copy=False, ...)`，把 `copy` 钉死为 `False`，由 `array()` 内部的拷贝判定（见 4.4）决定是否真的复制内存。

---

### 4.2 array() 的五条输入归一化分支

#### 4.2.1 概念说明

`array()` 的核心难点不在「构造 chararray」，而在「**识别五花八门的输入**」。调用方可能传入：

- 一个 Python `str` 或 `bytes`（标量字符串）；
- 一个 Python `list` 或 `tuple`；
- 一个 dtype 为 `str_` / `bytes_`（统称 `character`）的 `ndarray`；
- 一个 dtype 为 `object` 的 `ndarray`（元素是任意 Python 对象）。

`array()` 用一连串 `isinstance` 判断，把这些输入分流到五条分支，最终都产出 `chararray`。理解这条「归一化流水线」是读懂本函数的关键。

#### 4.2.2 核心流程

下面是 `array()` 的分支伪代码（保留判断顺序，省略细节）：

```text
def array(obj, itemsize=None, copy=True, unicode=None, order=None):
    # 分支 1：单个 Python str/bytes
    if isinstance(obj, (bytes, str)):
        推断 unicode；若 itemsize 为 None 取 len(obj)
        shape = len(obj) // itemsize        # 把字符串"切成" itemsize 宽的块
        return chararray(shape, itemsize, unicode, buffer=obj, order)

    # 分支 2：list/tuple —— 先转成普通 ndarray，再往下走
    if isinstance(obj, (list, tuple)):
        obj = asnarray(obj)                 # 可能变成 str_/bytes_/object 型 ndarray

    # 分支 3：character 型 ndarray（含 chararray）—— 主路径
    if isinstance(obj, ndarray) and issubclass(obj.dtype.type, character):
        若不是 chararray 则 obj.view(chararray)
        推断 itemsize（str_ 要 ÷4）、推断 unicode
        按 4 条件决定是否 astype（拷贝）
        return obj

    # 分支 4：object 型 ndarray 且未指定 itemsize —— 拍平成 list 再走默认分支
    if isinstance(obj, ndarray) and issubclass(obj.dtype.type, object):
        if itemsize is None:
            obj = obj.tolist()              # 交给底层自动定宽

    # 分支 5（默认）：用 narray 构造后 view 成 chararray
    if unicode: dtype = str_ else dtype = bytes_   # unicode 仍为 None 时按 bytes_ 处理
    val = narray(obj, dtype=dtype 或 (dtype, itemsize), order=order, subok=True)
    return val.view(chararray)
```

注意几个「往下穿透（fall-through）」的设计：

- 分支 2 不 `return`，它只把 `list/tuple` 转成 `ndarray`，再让后续分支处理。
- 分支 4 在 `itemsize is None` 时把 object 数组 `.tolist()`，然后**不 return**，继续落到默认分支 5。
- 分支 5 是兜底，负责 object 数组、以及任何前面没接住的输入。

#### 4.2.3 源码精读

**分支 1：标量 `str` / `bytes`**

[numpy/_core/defchararray.py:1296-1308](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L1296-L1308) —— 这一支最有特色的是「切块（chunking）」：`shape = len(obj) // itemsize`。当你给一个长度为 `N` 的字符串、并指定 `itemsize=k` 时，它会被切成 `N//k` 个宽 `k` 的元素。例如 `np.char.array(b'abcdef', itemsize=2)` 会得到 3 个元素 `[b'ab', b'cd', b'ef']`。若不指定 `itemsize`，则 `itemsize = len(obj)`、`shape = 1`，即整个字符串当作单个元素。最终调用 `chararray(shape, itemsize=..., unicode=..., buffer=obj, order=order)`，把字符串当作内存缓冲区交给构造器（见 4.3.3 引用的 `chararray.__new__`）。

**分支 2：`list` / `tuple`**

[numpy/_core/defchararray.py:1310-1311](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L1310-L1311) —— 仅一行 `obj = asnarray(obj)`。这一步的结果取决于列表内容：纯 `str` 列表 → `str_` 型 `ndarray`（进分支 3）；纯 `bytes` 列表 → `bytes_` 型 `ndarray`（进分支 3）；混合类型（如 `['abc', 2]`）→ `object` 型 `ndarray`（进分支 4）。

**分支 3：`character` 型 `ndarray`（主路径）**

[numpy/_core/defchararray.py:1313-1345](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L1313-L1345) —— 这是被命中频率最高的分支。它先把普通 `character` 数组 `obj.view(chararray)`（零拷贝地「看作」chararray），再推断 `itemsize`/`unicode`，最后按四条件决定是否 `astype`。这条分支的拷贝判定留到 4.4 精读。

**分支 4：`object` 型 `ndarray`**

[numpy/_core/defchararray.py:1347-1353](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L1347-L1353) —— object 数组没有固定字符宽度，所以在 `itemsize is None` 时先 `obj = obj.tolist()`，把它「拍平」回 Python 列表，**然后故意不 return**，让程序落到默认分支 5，由底层 `narray` 自动按最长元素定宽（测试 `test_from_object_array` 验证：最长元素 `'0123456789'` 决定了 `itemsize=10`）。

**分支 5（默认）：兜底构造**

[numpy/_core/defchararray.py:1355-1364](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L1355-L1364) —— 关键细节在 `if unicode:`：此时 `unicode` 可能仍是 `None`（例如从分支 4 穿透过来且调用方没传 `unicode=True`），而 `None` 是假值，于是 `dtype = bytes_`。**这就是「object 数组 / 混合列表默认产出 `bytes_`」的根源**。随后用 `narray(obj, dtype=..., order=order, subok=True)` 构造普通 `ndarray`，再 `val.view(chararray)` 包装成 `chararray`。`subok=True` 表示保留子类信息。

#### 4.2.4 代码实践

1. **实践目标**：用四类不同输入触发不同分支，记录各自的 `dtype` 与 `itemsize`，验证上面的分支归属。
2. **操作步骤**（示例代码）：

   ```python
   import warnings
   warnings.simplefilter("ignore", DeprecationWarning)
   import numpy as np

   # 分支 1：单个 str
   a = np.char.array("hello", itemsize=1)
   print("str  ->", a.dtype, a.dtype.itemsize, a.shape)   # 预期切块成 5 个 1 字符元素

   # 分支 1：单个 bytes
   b = np.char.array(b"abc")
   print("bytes->", b.dtype, b.dtype.itemsize, b.shape)

   # 分支 2 -> 3：纯 str 列表
   c = np.char.array(["hello", "world"])
   print("list ->", c.dtype, c.dtype.itemsize)

   # 分支 2 -> 4 -> 5：混合类型列表（变成 object 数组）
   d = np.char.array(["abc", 2])
   print("mixed->", d.dtype, d.dtype.itemsize)

   # 分支 4：显式 object 数组
   e = np.char.array(np.array([["abc", 2], ["long   ", "0123456789"]], dtype="O"))
   print("obj  ->", e.dtype, e.dtype.itemsize)
   ```
3. **需要观察的现象**：
   - `str` 输入按 `itemsize=1` 被切成 5 个元素，`shape==(5,)`，`str_` 型；
   - `bytes` 输入是单个元素，`bytes_` 型；
   - 纯 `str` 列表 → `str_`（`<U5`，最长 5 字符）；
   - 混合列表 → `bytes_`（走 object 分支，`unicode` 为 `None` 当假值处理）；
   - 显式 object 数组 → `bytes_`，`itemsize=10`（由最长元素决定，且尾部空格 `'long   '` 被截到 `'long'`）。
4. **预期结果**：与 `numpy/_core/tests/test_defchararray.py` 的 `test_from_object_array`（L23–L29）、`test_from_string`（L80–L84）、`test_from_unicode`（L86–L91）一致；混合列表的精确 `itemsize` 视你的 Python 对象而定，**待本地验证**。
5. 若想强制得到 `str_`，请显式传 `unicode=True`（如 `np.char.array(np.array([...], dtype="O"), unicode=True)`）。

#### 4.2.5 小练习与答案

- **练习 1**：为什么 `np.char.array(["abc", 2])` 得到的是 `bytes_` 而不是 `str_`？
  - **答案**：列表里混了 `int`，`asnarray(["abc", 2])` 只能造出 `object` 型 `ndarray`，落入分支 4 → 默认分支 5。默认分支里 `unicode` 仍是 `None`，`if None:` 为假，于是 `dtype = bytes_`。
- **练习 2**：分支 2 为什么没有 `return`？
  - **答案**：分支 2 的职责只是「把 `list/tuple` 归一化成 `ndarray`」，真正的构造逻辑（推断宽度、拷贝判定）要复用分支 3 或分支 5。如果它 `return`，就会重复实现一遍这些逻辑。

---

### 4.3 itemsize 与 unicode 的自动推断（含 str_ 的 ÷4）

#### 4.3.1 概念说明

当调用方不指定 `itemsize`（即 `itemsize is None`）和 `unicode`（即 `unicode is None`）时，`array()` 要替调用方「猜」出合理的字符宽度与字符串类型。这两件事都在分支 3 和分支 1 里完成。

最容易踩坑的是 `str_` 的 `itemsize` 换算：`ndarray.itemsize` 属性返回的是**字节数**，而 `chararray` 构造器与 `(dtype, itemsize)` 元组里的 `itemsize` 约定是**字符数**。对 `str_`（UCS-4，4 字节/字符）来说，二者差 4 倍，所以源码里有一行 `itemsize //= 4`。

#### 4.3.2 核心流程

**unicode 推断规则**（`unicode is None` 时）：

| 输入 | 推断出的 `unicode` | 结果 dtype |
| --- | --- | --- |
| Python `str` / `str_` 数组 | `True` | `str_` |
| Python `bytes` / `bytes_` 数组 | `False` | `bytes_` |
| object 数组 / 混合列表（落到默认分支） | 仍为 `None` → 当假值 | `bytes_` |

**itemsize 推断规则**（`itemsize is None` 时）：

\[
\text{itemsize}_{\text{字符}} = \frac{\text{obj.itemsize}_{\text{字节}}}{c},\qquad
c = \begin{cases}4 & \text{当 obj.dtype.type 是 } \text{str\_}\\[2pt] 1 & \text{当 obj.dtype.type 是 } \text{bytes\_}\end{cases}
\]

对 `bytes_`，`c=1`，字符数就等于字节数，无需调整；对 `str_`，必须 `÷4`。

#### 4.3.3 源码精读

**分支 3 里的 itemsize ÷4**

[numpy/_core/defchararray.py:1319-1325](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L1319-L1325) —— 注释写得很清楚：「itemsize is in 8-bit chars, so for Unicode, we need to divide by the size of a single Unicode character, which for NumPy is always 4」。即 `obj.itemsize` 是按 8 位（1 字节）单位计的字节数，对 Unicode 要除以 4 才得到字符数。换算后的 `itemsize`（字符数）随后被用在 `obj.astype((dtype, int(itemsize)))` 里（见 4.4.3）。

**分支 3 里的 unicode 推断**

[numpy/_core/defchararray.py:1327-1336](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L1327-L1336) —— `unicode is None` 时，按 `obj.dtype.type` 是否为 `str_` 决定；否则保持调用方显式传入的值。

**分支 1 里的标量推断**

[numpy/_core/defchararray.py:1296-1308](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L1296-L1308) —— 对单个 `str` 推断 `unicode=True`、对 `bytes` 推断 `False`；`itemsize is None` 时取 `len(obj)`（字符数）。

**`chararray.__new__` 接收的是「字符数」**

[numpy/_core/defchararray.py:550-580](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L550-L580) —— 工厂函数最终把推断出的「字符数」`itemsize` 传给 `chararray.__new__`，后者再 `ndarray.__new__(cls, shape, (dtype, itemsize), ...)`。这里 `(dtype, itemsize)` 中的 `itemsize` 是字符数（对 `str_` 会被内部放大 4 倍成字节数）。这一段也是上一讲 u3-l1 已经精读过的构造逻辑，本讲只确认「工厂推断的 itemsize 与构造器期望的单位一致」。

#### 4.3.4 代码实践

1. **实践目标**：直观验证 `str_` 的 `itemsize ÷4`，以及标量分支的宽度推断。
2. **操作步骤**（示例代码）：

   ```python
   import warnings
   warnings.simplefilter("ignore", DeprecationWarning)
   import numpy as np

   # str_ 数组：最长 10 字符
   u = np.array([["abc", "Sigma Σ"], ["long   ", "0123456789"]])  # <U10
   print("str_  obj.itemsize =", u.itemsize)          # 预期 40 (字节)
   cu = np.char.array(u)
   print("char  dtype        =", cu.dtype, "itemsize =", cu.dtype.itemsize)  # <U10, 40

   # 单个 unicode 字符
   one = np.char.array("Σ")
   print("one   itemsize     =", one.itemsize, "len[0] =", len(one[0]))   # 预期 itemsize=4, len=1
   ```
3. **需要观察的现象**：`<U10` 的 `obj.itemsize` 是 `40` 字节；经 `array()` 后仍是 `<U10`、`itemsize=40`（即 10 字符 × 4 字节）。单个 `'Σ'` 的 `itemsize=4`（1 字符 × 4 字节）。
4. **预期结果**：与 `test_from_unicode_array`（L57–L68）、`test_from_unicode`（L86–L91）一致：`one.itemsize == 4`。
5. 这一组结果是源码可推导的（`10 字符 × 4 = 40` 字节），但具体打印仍**待本地验证**。

#### 4.3.5 小练习与答案

- **练习 1**：一个 `<U7` 的 `str_` 数组，`obj.itemsize` 是多少？`array()` 推断出的「字符数 itemsize」是多少？
  - **答案**：`obj.itemsize = 7×4 = 28` 字节；推断出的字符数 `itemsize = 28//4 = 7`。
- **练习 2**：如果把 `itemsize //= 4` 这行删掉，对 `str_` 输入会发生什么？
  - **答案**：`itemsize` 会停留在字节数（如 28），随后 `obj.astype((str_, 28))` 会被解读成「28 个字符」，即 `<U28`（`itemsize=112` 字节），数组宽度被错误放大 4 倍——这正是该行存在的意义。

---

### 4.4 asarray()：复用 array(copy=False) 与拷贝判定

#### 4.4.1 概念说明

`asarray` 的语义是「能不拷贝就不拷贝」。但「是否拷贝」并非由 `asarray` 自己决定，而是由 `array()` 分支 3 末尾的一段四条件判定决定。本节精读这段判定，并指出一个**反直觉的细节**：由于 `itemsize`（字符数）与 `obj.itemsize`（字节数）单位不一致，`str_` 数组即便走 `asarray` 也常常仍会拷贝；只有 `bytes_` 数组在宽度匹配时才真正零拷贝。

#### 4.4.2 核心流程

分支 3 的拷贝判定（伪代码）：

```text
# 此时 obj 已被 view 成 chararray，itemsize/unicode 已推断
if ( copy                                              # ① 调用方要求拷贝
     or (itemsize != obj.itemsize)                     # ② 目标宽度 != 源字节宽度
     or (not unicode and isinstance(obj, str_))        # ③ 需要把 str_ 降成 bytes_
     or (unicode and isinstance(obj, bytes_))):        # ④ 需要把 bytes_ 升成 str_
    obj = obj.astype((dtype, int(itemsize)))           # 真正复制 + 转换
return obj                                             # 否则直接返回 view（零拷贝）
```

关键点：

- `asarray` → `copy=False`，条件 ① 不成立。
- 但条件 ② 比较的是**字符数 `itemsize`** 与**字节数 `obj.itemsize`**。对 `bytes_`（1 字节/字符）二者同量纲、可相等；对 `str_`（4 字节/字符）二者恒差 4 倍，**几乎必然不等**，于是触发 `astype` → 拷贝。
- 真正的「零拷贝」只发生在：`copy=False`（即 `asarray`）+ `bytes_` 数组 + 宽度匹配 + 不需要 str↔bytes 互转。此时直接返回第 1317 行 `obj.view(chararray)` 创建的视图，与原数组共享内存。

#### 4.4.3 源码精读

**view 成 chararray（零拷贝起点）**

[numpy/_core/defchararray.py:1316-1317](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L1316-L1317) —— `if not isinstance(obj, chararray): obj = obj.view(chararray)`。`view()` 不复制数据，只把同一段内存重新解释成 `chararray` 子类。这是 `asarray` 实现「不拷贝」的物理基础。

**拷贝四条件**

[numpy/_core/defchararray.py:1340-1345](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L1340-L1345) —— 四个条件任一成立就 `obj.astype((dtype, int(itemsize)))`（复制并按新宽度/新类型重建）；全部不成立则直接 `return obj`（视图）。条件 ② 的单位陷阱是本节重点：对 `str_`，`itemsize` 已被 `÷4` 成字符数，而 `obj.itemsize` 仍是字节数，二者几乎不可能相等。

**测试佐证：bytes_ 数组的零拷贝**

[numpy/_core/tests/test_defchararray.py:40-55](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/tests/test_defchararray.py#L40-L55) —— `test_from_string_array` 用 `bytes_` 数组 `A`：`np.char.array(A)`（`copy=True`）得到 `B`，改 `B[0,0]` 不影响 `A`（说明拷贝了）；而 `np.char.asarray(A)`（`copy=False`）得到 `C`，改 `C[0,0]` 后 `C[0,0] == A[0,0]`（说明 `C` 与 `A` 共享内存，零拷贝）。这正是条件 ② 在 `bytes_` 上不成立的体现。

#### 4.4.4 代码实践

1. **实践目标**：用「修改一方、看另一方是否跟着变」来判定是否共享内存（零拷贝），并对比 `bytes_` 与 `str_` 的差异。
2. **操作步骤**（示例代码）：

   ```python
   import warnings
   warnings.simplefilter("ignore", DeprecationWarning)
   import numpy as np

   # bytes_ 数组：预期 asarray 零拷贝
   A = np.array([[b"abc", b"foo"], [b"long   ", b"0123456789"]])  # |S10
   B = np.char.array(A)        # copy=True → 应拷贝
   C = np.char.asarray(A)      # copy=False → 应零拷贝
   B[0, 0] = b"X"; print("array  共享?", A[0, 0] == b"X")   # 预期 False（拷贝）
   C[0, 0] = b"Y"; print("asarray 共享?", A[0, 0] == b"Y")  # 预期 True（零拷贝）

   # str_ 数组：待验证是否仍拷贝（条件 ② 单位不一致）
   U = np.array([["abc", "foo"], ["long   ", "0123456789"]])  # <U10
   V = np.char.asarray(U)
   V[0, 0] = "Z"; print("str_ asarray 共享?", U[0, 0] == "Z")  # 待本地验证
   ```
3. **需要观察的现象**：`bytes_` 上 `asarray` 与原数组共享内存（修改 `C` 影响 `A`）；`str_` 上是否共享**需要你本地运行确认**。
4. **预期结果**：`bytes_`：`array 共享? False`、`asarray 共享? True`（与 `test_from_string_array` 一致）。`str_`：根据源码条件 ②（`10 != 40`）推断 `astype` 会触发、应**不共享**（即 `asarray` 仍拷贝）——这是源码可推导的结论，但请以本地运行结果为准。
5. 结论要点：`asarray` 的「零拷贝」承诺在 `bytes_` 上可靠兑现，在 `str_` 上因 `itemsize` 单位换算而常常失效。

#### 4.4.5 小练习与答案

- **练习 1**：为什么 `np.char.asarray(bytes_array)` 能零拷贝，而 `np.char.asarray(str_array)` 往往不能？
  - **答案**：拷贝判定条件 ② 比较 `itemsize`（字符数）与 `obj.itemsize`（字节数）。`bytes_` 下 1 字符 = 1 字节，二者量纲一致、可相等，条件 ② 不成立，直接返回 `view`；`str_` 下 `itemsize` 已 `÷4` 成字符数，与字节数恒差 4 倍，条件 ② 几乎必成立，触发 `astype` 复制。
- **练习 2**：如果不传 `itemsize`，但对 `bytes_` 数组显式传一个「与原宽度相同」的 `itemsize`，`asarray` 还会拷贝吗？
  - **答案**：只要 `itemsize` 等于 `obj.itemsize`（`bytes_` 下同量纲）、且无需 str↔bytes 互转、`copy=False`，四个条件都不成立，`asarray` 仍零拷贝。但若你传的 `itemsize` 与原宽度不同，条件 ② 成立，就会拷贝并按新宽度重建。

---

## 5. 综合实践

把本讲的「分支识别 + itemsize 推断 + 拷贝判定」串起来，完成下面这张「输入 → 分支 → 输出」对照表。请逐项运行并补全 `dtype`、`itemsize`、是否拷贝三列。

```python
import warnings
warnings.simplefilter("ignore", DeprecationWarning)
import numpy as np

cases = {
    "str标量":        ("hello", {}),
    "bytes标量":      (b"abc", {}),
    "str切块":        ("abcdef", {"itemsize": 2}),
    "纯str列表":      (["hello", "world"], {}),
    "纯bytes列表":    ([b"hi", b"hey"], {}),
    "混合列表":       (["abc", 2], {}),
    "str_数组":       (np.array(["abc", "defg"]), {}),
    "bytes_数组":     (np.array([b"abc", b"defg"]), {}),
    "object数组":     (np.array(["abc", 12345], dtype="O"), {}),
    "object+unicode": (np.array(["abc", "Σ"], dtype="O"), {"unicode": True}),
}

for name, (obj, kw) in cases.items():
    arr = np.char.array(obj, **kw)
    print(f"{name:14s} dtype={str(arr.dtype):8s} itemsize={arr.dtype.itemsize:3d}")
```

任务：

1. 对每一行，判断它走了哪条分支（1 / 2 / 3 / 4 / 5），并把推断依据写在该行旁边。提示：`"str切块"` 走分支 1 且 `shape = 6//2 = 3`；`"混合列表"` 经分支 2 变 object 后落到默认分支 5，`unicode` 为 `None` 当假值 → `bytes_`。
2. 对 `"bytes_数组"` 和 `"str_数组"` 各做一次 `asarray`，用第 4.4.4 节的「改值看是否联动」方法判定是否零拷贝，验证「`bytes_` 可零拷贝、`str_` 常拷贝」的结论。
3. 选一行（建议 `"object数组"`）显式加 `unicode=True`，对比不加时 `dtype` 的变化，体会分支 5 里 `if unicode:` 把 `None` 当假值的后果。

> 说明：本实践为「源码阅读 + 本地运行」混合型。表格中除已由 `test_defchararray.py` 覆盖的情形外，其余精确数值以你本地运行结果为准（部分标「待本地验证」）。

## 6. 本讲小结

- `array()` 与 `asarray()` 是 `chararray` 的官方工厂入口；`asarray` 只是 `return array(obj, itemsize, copy=False, ...)` 的一行包装，二者唯一实质差异是默认是否拷贝。两者在 NumPy 2.5 均被软弃用，访问 `np.char.array` / `np.char.asarray` 会触发 `DeprecationWarning`。
- `array()` 用五条 `isinstance` 分支做**输入归一化**：①标量 `str/bytes`（含「切块」语义 `shape=len//itemsize`）、②`list/tuple`（先转 `ndarray` 再穿透）、③`character` 型 `ndarray`（主路径，`view(chararray)` + 推断 + 拷贝判定）、④`object` 型 `ndarray`（`.tolist()` 后穿透到默认分支）、⑤默认兜底（`narray` + `view(chararray)`）。
- `itemsize`/`unicode` 在 `None` 时自动推断：`str`/`str_` → `unicode=True`、`bytes`/`bytes_` → `False`；object/混合列表落到默认分支时 `unicode` 仍为 `None`，被 `if unicode:` 当假值处理，故默认产出 `bytes_`。
- `str_` 的 `itemsize //= 4` 把 `ndarray.itemsize`（字节数）换算回 `chararray` 期望的字符数；这一步是 `array()` 与 `chararray.__new__` 单位约定一致的关键。
- 拷贝由四条件判定：`copy` / 宽度不等 / str_→bytes_ / bytes_→str_。`asarray` 的零拷贝承诺在 `bytes_` 数组上可靠兑现，但在 `str_` 数组上因「字符数 vs 字节数」单位不一致而常常失效。
- 所有动手实践都建议先压制 `DeprecationWarning`；新代码应直接用普通 `str_`/`bytes_` 数组搭配 `numpy.char` 或 `numpy.strings` 自由函数，而非这两个已弃用的工厂。

## 7. 下一步学习建议

- 下一篇 **u3-l4 弃用迁移：从 chararray 到 numpy.strings** 会把本讲的「工厂产出 chararray」与 u2-l1/u2-l2 的弃用机制接起来，给出把 `np.char.array(...).upper()` 改写为 `np.strings.upper(np.array(...))` 的实操路径，并提示如何手动补偿 chararray 特有的「尾部空白剥离」语义。
- 若想进一步确认本讲的运行时行为，建议本地 `pip install numpy` 后运行第 5 节的对照表，以及 `numpy/_core/tests/test_defchararray.py` 中的 `TestBasic`（可用 `pytest -k "TestBasic" -- ... ` 并参照 `ignore_charray_deprecation` 标记过滤弃用警告）。
- 继续阅读 `numpy/_core/defchararray.py` 的 `array()`（L1220–L1364）与 `asarray()`（L1367–L1429）全段，对照本讲的分支伪代码逐行印证；并可顺带阅读 `numpy/_core/numeric.py` 中 `array`/`asarray` 的 `subok` 参数文档，理解默认分支里 `subok=True` 的作用。
