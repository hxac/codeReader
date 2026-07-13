# 项目定位、目录结构与三层关系

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 `numpy.char` 这个模块在整个 NumPy 里扮演什么角色；
- 看懂 `numpy/char/` 目录下只有两个文件、却提供几十个字符串函数的原因；
- 理清 **char → defchararray → strings** 这三层的调用关系；
- 在本地成功 `import numpy.char` 并完成第一次向量化字符串调用；
- 知道 `chararray` 类在 NumPy 2.5 被弃用的背景，以及官方推荐的新方向。

## 2. 前置知识

- **Python 模块与包**：一个目录里有 `__init__.py` 就是一个包，`import numpy.char` 就是导入这个子包。
- **NumPy 数组基础**：`np.array([...])` 创建 ndarray，每个数组都有一个 `dtype`（数据类型），比如整数、浮点、字符串。
- **字符串方法**：Python 自带的 `str.upper()`、`str.center()` 等，NumPy 的字符模块把它们「向量化」——对数组里每一个元素同时执行。
- **模块级 `__getattr__`**：Python 3.7+ 允许在模块里定义一个 `__getattr__(name)` 函数，当访问 `模块.某名字` 且这个名字在模块里找不到时被调用。这是本讲「惰性加载」的关键，后面会详细讲。

如果你对前三条已经熟悉，本讲的重点在最后一条与第 4 节。

## 3. 本讲源码地图

本讲涉及的关键位置如下（注意一个反直觉的事实：`numpy/char/` 目录里**没有任何业务逻辑代码**，逻辑全在 `numpy/_core/defchararray.py`）：

| 文件 | 作用 | 层次 |
|------|------|------|
| `numpy/char/__init__.py` | 公共入口：惰性再导出 + 弃用包装，仅 31 行 | 第 1 层（薄门面） |
| `numpy/char/__init__.pyi` | 类型存根：静态类型检查器看到的「公开 API 清单」 | 第 1 层（类型视图） |
| `numpy/_core/defchararray.py` | 真正的实现：`__all__`、`__doc__`、比较函数、`multiply`/`partition` 包装、`chararray` 类、`array`/`asarray` 工厂 | 第 2 层（实现层） |
| `numpy/strings/__init__.py` | 现代字符串模块：再导出 `numpy._core.strings` 的 ufunc | 第 3 层（现代层） |

一句话概括：`char` 包只是一个「门面（facade）」，真正的活儿在 `defchararray`，而 `defchararray` 又把大部分活儿再外包给 `numpy.strings`。

## 4. 核心概念与源码讲解

### 4.1 numpy.char 公共入口

#### 4.1.1 概念说明

`numpy.char` 是 NumPy 里负责「向量化字符串操作」的子模块。所谓向量化，就是「对一个字符串数组里的每个元素，同时执行某个字符串操作」，而不需要写 for 循环。

例如把 `['hello', 'world']` 全部转大写，普通 Python 要写：

```python
[s.upper() for s in ['hello', 'world']]
```

而在 `numpy.char` 里只需：

```python
import numpy as np
np.char.upper(np.array(['hello', 'world']))
# array(['HELLO', 'WORLD'], dtype='<U5')
```

这个模块在历史上有两副面孔：

1. **一组「自由函数」（free functions）**：`np.char.upper`、`np.char.add` 等，可以直接对 ndarray 调用。这部分**仍然是推荐用法**。
2. **一个 `chararray` 类**：`ndarray` 的子类，把字符串方法绑成方法和运算符（如 `+`、`*`）。这部分在 **NumPy 2.5 被弃用（deprecated）**，官方建议改用普通字符串 ndarray + 自由函数。

本讲先把这两副面孔的存在交代清楚，细节留给后续讲义。

#### 4.1.2 核心流程

当你写下 `np.char.upper(...)` 时，Python 内部发生的事情是：

```
np.char.upper            # 访问 char 包里的 upper 名字
   ↓
char/__init__.py 里并没有直接定义 upper
   ↓
触发模块级 __getattr__("upper")
   ↓
__getattr__ 从 numpy._core.defchararray 里取出 upper 并返回
   ↓
你拿到的 upper 其实来自 defchararray（或它再从 numpy.strings 再导出）
```

而当你访问 `np.char.chararray` 时，会多一步——`__getattr__` 先发一个 `DeprecationWarning`，再返回。

#### 4.1.3 源码精读

先看入口文件的全部内容（只有 31 行），`numpy.char` 的全部入口逻辑都在这里：

[char/__init__.py:1-31](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/char/__init__.py#L1-L31) —— 第 1 行直接导入 `__all__` 和 `__doc__`，第 3 行定义弃用名单，第 6-25 行是惰性 `__getattr__`，第 28-31 行是 `__dir__`。

第 1 行很关键：

[char/__init__.py:1](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/char/__init__.py#L1) —— 把实现层的 `__all__`（公开 API 清单）和 `__doc__`（模块文档）原样搬到 `numpy.char` 名下。

```python
from numpy._core.defchararray import __all__, __doc__
```

这一行告诉我们两件事：`numpy.char` 的「公开 API 到底有哪些」并不由它自己决定，而是**完全继承自 `defchararray`**；`help(numpy.char)` 看到的文档也来自 `defchararray` 顶部的 docstring。

再看弃用名单与 `__getattr__`：

[char/__init__.py:3-25](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/char/__init__.py#L3-L25) —— 定义 `__DEPRECATED` 集合，并在 `__getattr__` 里判断：访问 `chararray` / `array` / `asarray` 这三个名字时先发 `DeprecationWarning`。

```python
__DEPRECATED = frozenset({"chararray", "array", "asarray"})

def __getattr__(name: str):
    if name in __DEPRECATED:
        # Deprecated in NumPy 2.5, 2026-01-07
        import warnings
        warnings.warn(
            "The chararray class is deprecated ...",
            DeprecationWarning,
            stacklevel=2,
        )

    import numpy._core.defchararray as char
    if (export := getattr(char, name, None)) is not None:
        return export
    raise AttributeError(...)
```

注意第 8 行的注释 `# Deprecated in NumPy 2.5, 2026-01-07`，这正是 `chararray` 弃用时间线的出处。`stacklevel=2` 让警告指向「调用者」那一行，而不是 `__getattr__` 内部，方便定位。

最后是 `__dir__`：

[char/__init__.py:28-31](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/char/__init__.py#L28-L31) —— 让 `dir(np.char)` 能补全所有可用的名字，直接委托给实现层。

```python
def __dir__() -> list[str]:
    import numpy._core.defchararray as char
    return dir(char)
```

> 小知识：为什么说 `__getattr__` 是「惰性」的？因为它**只在真正访问某个名字时**才去 `import numpy._core.defchararray`。平时 `import numpy.char` 不会把整个实现层加载进来，能加快启动、减少循环导入风险。第 1 行只导入 `__all__` / `__doc__` 这两个轻量名字，真正的几十个函数是「用到才取」。

#### 4.1.4 代码实践

**实践目标**：亲手确认 `numpy.char` 的入口是一个「薄门面」，并感知弃用警告。

**操作步骤**（在装好 NumPy 的环境里运行）：

```python
import numpy.char
import warnings

# 1. 看入口文件到底在哪
print(numpy.char.__file__)

# 2. 看模块文档从哪来（应来自 defchararray 顶部 docstring）
print((numpy.char.__doc__ or "")[:80])

# 3. 公开 API 有多少个
print(len(numpy.char.__all__))
```

**需要观察的现象**：
- `__file__` 指向 `.../numpy/char/__init__.py`（而不是 `defchararray.py`）；
- `__doc__` 开头是 `This module contains a set of functions for vectorized string operations...`，与 `defchararray.py` 顶部一致；
- `__all__` 长度在当前源码里是 **53**（待本地验证：你装的 NumPy 版本可能略有不同）。

**进阶**：再运行下面这段，验证只有「弃用三件套」会触发警告：

```python
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    _ = numpy.char.upper          # 不应报警
    _ = numpy.char.chararray      # 应报警
print("捕获到的警告数:", len(w))
print("警告类别:", w[0].category.__name__ if w else "无")
```

**预期结果**：捕获到 1 个警告，类别为 `DeprecationWarning`，内容提到 `chararray` 弃用。访问 `upper` 不会报警，因为它不在 `__DEPRECATED` 集合里。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `np.char.upper` 能用，但 `numpy/char/__init__.py` 里并没有写 `upper` 这个名字？

> **参考答案**：因为模块级 `__getattr__` 在你访问 `upper` 时被触发，它通过 `getattr(numpy._core.defchararray, "upper")` 把实现层的 `upper` 取出来返回。这是一种「惰性再导出」。

**练习 2**：`stacklevel=2` 如果改成 `stacklevel=1`，会有什么可观察的区别？

> **参考答案**：`stacklevel` 决定警告指向哪一层调用栈。`stacklevel=1` 指向 `__getattr__` 内部，`stacklevel=2` 指向调用者（写 `np.char.chararray` 的那一行）。改大或改小只影响报错定位的准确性，不影响「是否报警」。

**练习 3**：`__DEPRECATED` 用 `frozenset` 而不是 `list`，有什么好处？

> **参考答案**：`name in __DEPRECATED` 是高频操作（每次属性访问都查一次），`frozenset` 的成员测试是 O(1) 且不可变，比 `list` 的 O(n) 更快，也能防止集合被意外修改。

---

### 4.2 defchararray 实现层

#### 4.2.1 概念说明

`numpy._core.defchararray` 才是「真正干活」的地方。`char` 包的门面把所有请求都转发给它。这个文件里大致有四类内容：

1. **模块文档与 `__all__`**：定义对外契约（公开哪些名字）；
2. **比较函数**：`equal` / `not_equal` / `greater` / `less` 等，调用 C 层 `compare_chararrays`；
3. **本地包装函数**：`multiply` / `partition` / `rpartition`，在 `numpy.strings` 对应函数上做小幅改造；
4. **`chararray` 类与 `array` / `asarray` 工厂**：遗留的子类化方案。

本讲只需建立一张「地图」，第 2、3、4 类的细节留给后续讲义。

#### 4.2.2 核心流程

这个文件最关键的一行在第 30 行——它把「现代字符串函数」整体搬进来：

```
defchararray.py 顶部
   ↓
from numpy.strings import *        # 第 30 行：批量再导出现代 ufunc
   ↓
defchararray 拥有了 upper / lower / add / center ... 等几十个函数
   ↓
char 包通过 __getattr__ 转发
   ↓
用户写 np.char.upper(...) 实际执行的是 numpy.strings.upper
```

换句话说，你今天用 `np.char.upper`，背后大概率就是 `numpy.strings.upper`。这也是官方鼓励大家「直接用 `numpy.strings`」的原因——少一层转发，意图更清晰。

#### 4.2.3 源码精读

模块文档（开头就是弃用提示）：

[defchararray.py:1-17](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L1-L17) —— 模块 docstring，明确说 `chararray` 仅为兼容旧版 Numarray 而存在，新代码应改用 `object_` / `bytes_` / `str_` dtype 的数组 + `numpy.char` 自由函数。

其中第 6-10 行尤其重要：

[defchararray.py:5-10](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L5-L10) —— `note` 提示：`chararray` 不推荐用于新开发，推荐用 `str_`/`bytes_`/`object_` dtype 数组配合自由函数。

再导出与现代层委托：

[defchararray.py:23-35](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L23-L35) —— 第 23-28 行从 `numpy._core.strings` 取 4 个带下划线的「内部」函数并改名（`_join` → `join` 等）；第 30 行 `from numpy.strings import *` 批量搬入现代函数；第 31-35 行专门把 `multiply` / `partition` / `rpartition` 以别名导入，因为下面要本地重写它们。

```python
from numpy.strings import *
from numpy.strings import (
    multiply as strings_multiply,
    partition as strings_partition,
    rpartition as strings_rpartition,
)
```

公开 API 清单：

[defchararray.py:40-50](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L40-L50) —— `__all__`，共 53 个名字，涵盖比较、变换、查询、拆分、工厂函数与 `chararray` 类。这就是第 1 层 `char` 包 `__all__` 的真正来源。

以 `multiply` 为例看「本地包装」长什么样：

[defchararray.py:266-315](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L266-L315) —— `multiply` 用 `try/except` 把底层 `strings_multiply` 抛出的 `TypeError` 转成更友好的 `ValueError`，docstring 第 288-290 行明说「这只是个薄包装，为向后兼容而存在」。

```python
@set_module("numpy.char")
def multiply(a, i):
    try:
        return strings_multiply(a, i)
    except TypeError:
        raise ValueError("Can only multiply by integers")
```

`chararray` 类的弃用标注：

[defchararray.py:404-414](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L404-L414) —— `class chararray(ndarray)`，docstring 里直接写 `.. deprecated:: 2.5`，并说明它相对普通字符串 ndarray 多出的三点功能（取值自动 rstrip、比较自动 rstrip、运算符重载）。

工厂函数：

[defchararray.py:1220-1221](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L1220-L1221) 与 [defchararray.py:1367-1368](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L1367-L1368) —— `array()` 与 `asarray()` 两个工厂函数，用来构造 `chararray`。它们和 `chararray` 一起进了第 1 层的 `__DEPRECATED` 名单。

> 这里出现的 `@set_module("numpy.char")` 是 NumPy 内部工具，作用是「把这个函数的 `__module__` 改写成 `numpy.char`」，这样 `help(np.char.multiply)` 显示的归属是 `numpy.char`，而不是它真正定义所在的 `numpy._core.defchararray`。它和 `@array_function_dispatch` 一起是第 2 单元的重点。

#### 4.2.4 代码实践

**实践目标**：用源码确认「defchararray 是实现层、char 是门面」，并观察函数归属的差异。

**操作步骤**：

```python
import numpy as np

# 看 upper 的真实归属
print(np.char.upper.__module__)

# 看一个本地包装函数的归属（应显示 numpy.char，因为被 @set_module 改写过）
print(np.char.multiply.__module__)
```

**需要观察的现象**：
- `multiply.__module__` 显示 `numpy.char`（因为被 `@set_module("numpy.char")` 显式改写，这是确定的现象）；
- `upper.__module__` 很可能是 `numpy.strings`（它通过 `from numpy.strings import *` 搬进来，归属取决于上游如何标注，具体取值待本地验证）。

> 这是个有趣的对比：同样是 `np.char.xxx`，有的归属是 `numpy.strings`，有的是 `numpy.char`。这就是「再导出」与「本地定义 + `set_module`」的差别，第 2 单元会深入。

**预期结果**：两个 `__module__` 不一致，直观体现「三层关系」。

#### 4.2.5 小练习与答案

**练习 1**：`defchararray.py` 第 30 行 `from numpy.strings import *` 之后，第 31-35 行为什么还要单独再导入一次 `multiply` / `partition` / `rpartition`？

> **参考答案**：因为下面要**本地重写**这三个函数（做错误类型转换、结果 `np.stack` 等）。用别名 `strings_multiply` 等把「原始版本」保留下来，方便本地函数内部调用原始实现。如果只靠第 30 行的 `*`，本地 `def multiply` 一旦定义就会把导入进来的同名名字覆盖掉，就拿不到原始版本了。

**练习 2**：`__all__` 里同时有 `multiply`（本地包装）和 `upper`（从 strings 再导出），它们在「是否为本地定义」上有何不同？

> **参考答案**：`multiply` 是 `defchararray` 自己用 `def multiply(...)` 定义的函数，而 `upper` 没有本地 `def`，是 `from numpy.strings import *` 带进来的。前者多一层兼容逻辑（`try/except`），后者直接转发。

---

### 4.3 numpy.strings 现代层

#### 4.3.1 概念说明

`numpy.strings` 是 NumPy 较新的字符串子模块（基于 ufunc 实现，性能更好、支持 `StringDType`）。`numpy.char` 里大部分函数其实最终来自它。

可以这样理解三者的定位：

| 模块 | 角色 | 状态 |
|------|------|------|
| `numpy.char` | 历史门面，向 `defchararray` 转发 | 自由函数仍可用，`chararray` 已弃用 |
| `numpy._core.defchararray` | 实现层，含遗留 `chararray` 类 | 维护中，逐步「瘦身」 |
| `numpy.strings` | 现代层，基于 ufunc | **官方推荐的新方向** |

#### 4.3.2 核心流程

`numpy.strings` 自己也很薄，只有 2 行：

```
numpy/strings/__init__.py
   ↓
from numpy._core.strings import *        # 真正的 ufunc 在 C 层 _core/strings
   ↓
defchararray 又 from numpy.strings import *
   ↓
char 门面再转发
```

所以完整的转发链是：

```
np.char.upper
  → defchararray.upper（再导出）
  → numpy.strings.upper
  → numpy._core.strings 的 C ufunc
```

#### 4.3.3 源码精读

[strings/__init__.py:1-2](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/strings/__init__.py#L1-L2) —— 整个现代层入口只有两行，把 `numpy._core.strings` 的 `__all__`、`__doc__` 和全部函数原样暴露。

```python
from numpy._core.strings import *
from numpy._core.strings import __all__, __doc__
```

对比 `defchararray.py` 第 30 行 `from numpy.strings import *`，你能看到一条清晰的「上游 → 下游」关系：`strings` 是上游（现代实现），`defchararray` 是下游（带遗留兼容的转发层）。

> 一个验证两者关系的直觉方法：`np.char.capitalize is np.strings.capitalize`。如果返回 `True`，说明 `char` 版就是 `strings` 版的同一个对象——这正是「再导出」的本质。这个验证留作下面的实践。

#### 4.3.4 代码实践

**实践目标**：确认「char 的多数函数 = strings 的同一个对象」，并找出 char 独有的名字。

**操作步骤**：

```python
import numpy as np

# 同名函数是否是同一个对象？
print(np.char.capitalize is np.strings.capitalize)
print(np.char.upper is np.strings.upper)

# 列出 char 独有、strings 没有的名字
char_only = set(np.char.__all__) - set(np.strings.__all__)
print("char 独有:", sorted(char_only))
```

**需要观察的现象**：
- 两条 `is` 判断大概率都是 `True`（说明是再导出，同一个函数对象）；
- `char 独有` 应包含 `chararray`、`array`、`asarray`、`compare_chararrays`（这些是 `defchararray` 本地定义的，`strings` 没有）。

**预期结果**：`char` 把 `strings` 的现代函数几乎全部转发，只额外多出几个遗留/本地名字。具体独有集合待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：既然 `np.char.upper` 和 `np.strings.upper` 是同一个对象，为什么 NumPy 还要保留 `numpy.char`？

> **参考答案**：为了**向后兼容**。大量历史代码用了 `np.char.xxx`，直接删掉会破坏这些代码。`numpy.char` 作为一个稳定的门面保留下来，但内部逐步把实现委托给 `numpy.strings`，并弃用遗留的 `chararray`。

**练习 2**：如果想写「面向未来」的新代码，应该用 `np.char.upper` 还是 `np.strings.upper`？

> **参考答案**：用 `np.strings.upper`。它是官方推荐的现代层，少一层转发、意图清晰，且不与已弃用的 `chararray` 产生关联。

---

## 5. 综合实践

把本讲的知识串起来：**画出 `np.char` 一次调用的「转发链」并验证它。**

任务：

1. 在本地运行下面的代码，记录每个观察值。
2. 用文字画一张「调用链」图，标出每一段归属。

```python
import numpy as np
import numpy.char
import warnings

print("1) 入口文件:", numpy.char.__file__)
print("2) 公开 API 数:", len(numpy.char.__all__))
print("3) upper 归属:", np.char.upper.__module__)
print("4) multiply 归属:", np.char.multiply.__module__)
print("5) upper 同源 strings:", np.char.upper is np.strings.upper)

with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    np.char.asarray(np.array(["a", "b"]))   # 触发弃用警告
print("6) asarray 警告:", w[0].category.__name__ if w else "无")
```

预期你能写出类似这样的结论：

> `numpy.char` 是一个只有 31 行的门面包；它的 `__all__`/`__doc__` 来自 `numpy/_core/defchararray.py`；访问普通函数时由 `__getattr__` 惰性转发，访问 `chararray`/`array`/`asarray` 时额外发 `DeprecationWarning`；而 `defchararray` 里的多数函数又来自 `numpy.strings`（最终是 `numpy._core.strings` 的 C ufunc）。所以 **char → defchararray → strings** 三层关系成立。

## 6. 本讲小结

- `numpy.char` 是向量化字符串操作的子模块，它的包目录 `numpy/char/` 里**没有业务逻辑**，只有 `__init__.py` 和类型存根 `__init__.pyi`。
- 真正的实现全在 `numpy/_core/defchararray.py`，`char` 通过模块级 `__getattr__` 惰性转发。
- 三层关系：**char（门面）→ defchararray（实现/遗留）→ strings（现代 ufunc）**。
- `chararray` 类及 `array`/`asarray` 工厂在 **NumPy 2.5（2026-01-07）** 被弃用，访问时触发 `DeprecationWarning`。
- `defchararray` 通过 `from numpy.strings import *` 把现代函数整体搬入，再本地重写 `multiply`/`partition`/`rpartition` 做兼容处理。
- 官方推荐的新方向是直接用 `numpy.strings` + 普通 `str_`/`bytes_` dtype 的 ndarray。

## 7. 下一步学习建议

本讲建立了「三层关系」的地图，后续讲义会逐层下钻：

- **下一讲（u1-l2）**：补齐字符串 dtype 基础——`str_` / `bytes_` / `itemsize` / 定宽存储，理解 char 操作的数据底座。
- **再下一讲（u1-l3）**：浏览完整公共 API 表，并完成第一次元素级向量字符串操作（`upper`/`add`/`center`/`multiply`）。
- **第 2 单元**：进入模块机制——精读 `__getattr__` 的弃用逻辑、`array_function_dispatch` / `set_module` 装饰器、本地包装函数。
- **第 3 单元**：深入 `chararray` 子类化、`array`/`asarray` 工厂、弃用迁移与测试体系。

建议你现在先完成本讲的「综合实践」，确认三层关系在你的本地 NumPy 版本里也成立，再继续。
