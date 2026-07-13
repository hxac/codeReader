# 目录结构与文件分类：把约 30 多个文件理清楚

## 1. 本讲目标

上一讲（u1-l1）我们已经确认：`numpy.core` 早已不含任何实现，它只是 NumPy 2.0 为向后兼容保留的一个「垫片（shim）」。本讲的目标是把 `numpy/core` 这个目录**整体看一遍**，让你拿到一张「全局地图」。

学完本讲，你应当能够：

1. 说出 `numpy/core` 目录下文件扮演的**四种角色**：包入口、工具、纯转发垫片、类型存根。
2. 区分**纯转发垫片**（只有一个 `__getattr__`）和**带有 eager 绑定的特殊垫片**（在模块顶部还额外绑定了属性）。
3. 理解每个 `.py`（运行时模块）和它对应的 `.pyi`（类型存根）之间的关系，并能说出存根的三种写法。
4. 写一段脚本，自动判断任意一个文件属于哪一类。

> 本讲只建立**全局心智模型**，不深入每个机制的实现细节。模块级 `__getattr__` 的惰性转发细节在第 2 单元（u2-l1），`_raise_warning` 的 `stacklevel` 在 u2-l3，pickle / ABI 兼容在第 3 单元（u3-l1、u3-l2）。本讲遇到这些主题时只点到为止。

---

## 2. 前置知识

在动手分类之前，先用大白话过一遍几个关键概念。

### 2.1 Python 包与模块

一个目录里只要有 `__init__.py`，它就是一个**包（package）**，目录里的其他 `.py` 文件就是它的**子模块（submodule）**。例如 `numpy/core/__init__.py` 让 `numpy/core` 成为包，`numeric.py` 就是它的子模块，写作 `numpy.core.numeric`。

### 2.2 模块级 `__getattr__`（PEP 562）

从 Python 3.7 起（PEP 562），可以在**模块**里定义一个函数 `__getattr__(name)`。当有人访问「这个模块里本来不存在的属性」时，Python 会调用这个函数。这正是 `numpy.core` 实现「转发」的工具：访问 `numpy.core.numeric` 时，`__getattr__` 偷偷去 `numpy._core` 里把同名对象取出来返回。上一讲已经见过它的包级版本。

### 2.3 类型存根 `.pyi`

`.pyi` 是「类型存根（type stub）」文件。它只写**类型签名**（函数参数和返回值的类型），不写真正的实现逻辑，给 mypy / pyright 这类静态类型检查器用。一个 `.py` 可以配一个同名 `.pyi`，作为它的「静态镜像」。

### 2.4 DeprecationWarning 与 pickle

- `DeprecationWarning` 是 Python 标准库里用来提示「这个东西将来会删」的警告，默认只提示、不报错。
- `pickle` 是 Python 的对象序列化方案。它把一个对象存成字节流时，会记下「对象来自哪个模块、叫什么名字」；还原时再按这个「模块路径 + 名字」把对象找回来。这一点是本讲理解「为什么有些属性必须提前绑定」的关键铺垫（细节见 u3-l1）。

---

## 3. 本讲源码地图

先给结论：`numpy/core` 目录下共有 **36 个文件**（19 个 `.py` + 17 个 `.pyi`）。本讲涉及的关键文件如下。

| 文件 | 作用 | 本讲用来讲什么 |
|---|---|---|
| [`__init__.py`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/__init__.py) | 包入口 | 包级 `__all__` 如何强制惰性加载 |
| [`_utils.py`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_utils.py) | 工具 | 唯一的「工具文件」，统一弃用警告 |
| [`numeric.py`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/numeric.py) | 纯转发垫片 | 纯转发垫片的代表（用 sentinel） |
| [`umath.py`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/umath.py) | 纯转发垫片 | 纯转发垫片的另一写法（用 None） |
| [`_multiarray_umath.py`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_multiarray_umath.py) | 特殊垫片 | 带有 eager 绑定的特殊垫片 |
| [`multiarray.py`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/multiarray.py) | 特殊垫片 | 另一个 eager 绑定垫片（对照用） |
| [`__init__.pyi`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/__init__.pyi) | 类型存根 | 包入口存根 |
| [`numeric.pyi`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/numeric.pyi) | 类型存根 | 完整再导出式存根 |
| [`overrides.pyi`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/overrides.pyi) | 类型存根 | 省略再导出式存根（带 NOTE） |

> 本讲的分类表会覆盖全部 36 个文件，不必担心只看几个代表。

---

## 4. 核心概念与源码讲解

我们把 `numpy/core` 的文件按**运行时角色**分成四类，再单独看**类型存根**这一类。一句话总览：

> 这些文件几乎**都是垫片**。它们的差别只在于：是「纯转发」（只靠 `__getattr__`），还是「带 eager 绑定」（为了 pickle / ABI 兼容，在模块顶部额外绑死几个名字）。

### 4.1 包入口与工具文件：`__init__.py` 与 `_utils.py`

#### 4.1.1 概念说明

每个目录里最特殊的两个文件，往往不是垫片本身：

- **`__init__.py`** 是包的「入口」。导入 `numpy.core` 时，Python 最先执行的就是它。它负责声明「这个包对外有哪些子模块」（`__all__`）、提供包级的属性转发（`__getattr__`），以及保留一两个旧 pickle 需要的重建函数。
- **`_utils.py`** 是整个目录里唯一的「工具文件」。它只提供一个被所有垫片复用的函数 `_raise_warning`，自己**不做转发**，所以它没有 `__getattr__`，也**没有对应的 `.pyi`**——它是纯内部私有帮手。

#### 4.1.2 核心流程

包入口 `__init__.py` 的工作可以拆成三步：

1. 引入真正的实现包：`from numpy import _core`。
2. 引入工具函数：`from ._utils import _raise_warning`。
3. 声明 `__all__`（一串子模块名）并定义包级 `__getattr__`：当访问 `numpy.core.<某子模块>` 时，从 `numpy._core` 取出同名对象，并调用 `_raise_warning` 抛弃用警告。

#### 4.1.3 源码精读

先看入口文件顶部的「身份声明」与引入语句，这是上一讲已经引用过的依据：

[`__init__.py:1-8`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/__init__.py#L1-L8) — 模块文档字符串直说 `numpy.core` 只为向后兼容而存在、未来会移除；随后 `from numpy import _core` 引入真正实现。

重点看 `__all__`：

[`__init__.py:23-28`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/__init__.py#L23-L28) — 这里把所有子模块名写进 `__all__`。代码注释 `# force lazy-loading of submodules to ensure a warning is printed` 是关键：**故意不在这里 `import` 这些子模块**，只把它们列在 `__all__` 里。这样每次访问某个子模块时，都得走 `__getattr__`，从而每次都会打印弃用警告。如果把它们在顶部一次性 `import`，第一次访问就不会触发 `__getattr__`，警告就被「吃掉」了。

> 注意 `__all__` 里还含一个 `# noqa: F822` 标注。这是因为列表里的名字在模块里「看似未定义」（linter 会报「使用了未定义的名字」），加这个注释告诉 linter：这是故意的，这些名字靠 `__getattr__` 动态提供。

再看包级 `__getattr__` 和一个为旧 pickle 保留的重建函数：

[`__init__.py:30-33`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/__init__.py#L30-L33) — 包级转发：从 `numpy._core` 取出属性，调用 `_raise_warning` 抛警告，再返回。这一层只覆盖「包级」名字（如 `numpy.core.numeric` 整个子模块）；至于子模块**内部**的属性（如 `numeric.zeros`）如何转发，是 4.2 的主题。

[`__init__.py:14-20`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/__init__.py#L14-L20) — `_ufunc_reconstruct` 是为旧版 pickle（1.20 之前）保留的重建函数。它的存在说明：有些东西**不能只靠 `__getattr__` 懒加载**，必须在 `import` 时就能被直接取到。这是下一节「eager 绑定」思想的雏形（细节见 u3-l1）。

最后看工具文件 `_utils.py` 的全貌：

[`_utils.py:4-21`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_utils.py#L4-L21) — 整个文件就一个函数 `_raise_warning(attr, submodule=None)`：它把弃用信息拼成一段固定文本，根据是否传入 `submodule` 决定提示 `numpy._core` 还是 `numpy._core.<子模块>`，最后 `warnings.warn(..., stacklevel=3)`。它没有任何转发逻辑，所以 `_utils.py` 属于「工具」类，而不是垫片。

#### 4.1.4 代码实践

**实践目标**：亲眼确认 `__all__` 强制惰性加载的效果——访问列表里的子模块名时，一定触发一次弃用警告。

**操作步骤**（示例代码，需要本地已安装 numpy）：

```python
# 示例代码：observe_lazy_loading.py
import warnings
import numpy.core as core

print("__all__ 共列了", len(core.__all__), "个子模块名")

with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    _ = core.numeric          # 访问 __all__ 里列出的子模块名
    print("捕获到", len(w), "条警告")
    print("类别:", w[0].category.__name__ if w else "无")
    print("消息含 'numpy._core':", "numpy._core" in str(w[0].message) if w else False)
```

**需要观察的现象**：第一次访问 `core.numeric` 时会触发一条警告；警告类别是 `DeprecationWarning`，消息文本里包含 `numpy._core`。

**预期结果**：输出显示 `__all__` 列了 17 个名字，捕获到 1 条 `DeprecationWarning`，消息含 `numpy._core`。（具体条数与 numpy 版本有关，若与本讲描述不一致，请以本地实际输出为准。）

#### 4.1.5 小练习与答案

**练习 1**：如果把 `__init__.py` 里的 `__all__` 改成空列表，访问 `numpy.core.numeric` 还会触发警告吗？为什么？

**参考答案**：会。`__all__` 只影响 `from numpy.core import *` 的行为和 `dir()` 的显示，**不影响**属性访问。访问 `numpy.core.numeric` 时，因为模块里没有 `numeric` 这个属性，照样会落入 `__getattr__`，照样触发警告。`__all__` 在这里的真正作用是配合「不在顶部 import 子模块」，确保子模块**只能**通过 `__getattr__` 被加载。

**练习 2**：为什么 `_utils.py` 没有对应的 `_utils.pyi`？

**参考答案**：因为 `_utils.py` 是**私有内部工具**（名字以下划线开头），不在公开 API 里，外部代码不该静态引用它，所以没必要为它写类型存根。整个目录里它是唯一一个「既不是垫片、又没有存根」的 `.py` 文件。

---

### 4.2 纯转发垫片：`numeric.py` 与它的 13 个同类

#### 4.2.1 概念说明

目录里**绝大多数** `.py` 文件都是「纯转发垫片」。它们长得几乎一模一样：整个文件**只有一个 `__getattr__` 函数**，没有任何模块顶部的额外绑定。访问 `numpy.core.<这个子模块>.<某属性>` 时，`__getattr__` 就去 `numpy._core.<同名子模块>` 里把属性取出来、抛个警告、返回。

这样的文件共有 **14 个**：`numeric.py`、`umath.py`、`arrayprint.py`、`defchararray.py`、`einsumfunc.py`、`fromnumeric.py`、`function_base.py`、`getlimits.py`、`numerictypes.py`、`overrides.py`、`records.py`、`shape_base.py`、`_dtype.py`、`_dtype_ctypes.py`。

它们虽然结构相同，但在判断「属性到底存不存在」时用了**两种写法**：

- **`None` 写法**（13 个）：`getattr(目标, 名字, None)`，找不到就返回 `None`，再用 `if ret is None` 判断。
- **sentinel 写法**（只有 `numeric.py` 1 个）：造一个独一无二的「哨兵对象」`sentinel = object()`，找不到返回它，再用 `if ret is sentinel` 判断。

> 这两种写法的差异（以及 `None` 写法在面对 `0`、`""`、`None` 等「假但有效」的属性时的陷阱）是 u2-l2 的主题，本节只识别它们都属于「纯转发垫片」。

#### 4.2.2 核心流程

一个纯转发垫片的 `__getattr__`（以 `umath.py` 为例）执行流程：

1. 按需 `from numpy._core import umath`（注意：`import` 写在函数**内部**，所以是惰性的，只有真正访问时才加载）。
2. `ret = getattr(umath, attr_name, None)`：去真实现里取属性，找不到给 `None`。
3. `if ret is None:` 抛 `AttributeError`（属性确实不存在）。
4. 否则 `_raise_warning(attr_name, "umath")` 抛弃用警告，`return ret`。

#### 4.2.3 源码精读

先看 `None` 写法的代表 `umath.py` 全文（11 行）：

[`umath.py:1-10`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/umath.py#L1-L10) — 整个文件就是这一个函数。第 2 行 `from numpy._core import umath` 写在函数体内（惰性）；第 5 行用 `None` 作默认值；第 6-8 行判断 `ret is None` 抛 `AttributeError`。其余 12 个同类（`arrayprint.py`、`records.py`、`shape_base.py` 等）和它逐字同构，只把模块名换掉。

再看唯一的 sentinel 写法 `numeric.py`：

[`numeric.py:1-12`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/numeric.py#L1-L12) — 第 6 行 `sentinel = object()` 造一个独有对象；第 7 行 `getattr(numeric, attr_name, sentinel)` 找不到时返回它；第 8-10 行 `if ret is sentinel:` 抛 `AttributeError`。注意它仍然**只有 `__getattr__`**、没有顶部额外绑定，所以分类上和 `umath.py` 一样属于「纯转发垫片」——sentinel 只是更安全的「缺失判断」写法，不改变分类。

#### 4.2.4 代码实践

**实践目标**：用最简单的方式确认「纯转发垫片」的判定准则——文件里**只有** `__getattr__`。

**操作步骤**：

```python
# 示例代码：check_pure_forwarder.py
import ast
from pathlib import Path

p = Path("numpy/core/umath.py")      # 按实际路径修改
tree = ast.parse(p.read_text())

top_level = [type(n).__name__ for n in tree.body]
print("umath.py 的顶层语句类型：", top_level)

# 一个「纯转发垫片」的顶层应只有 FunctionDef(__getattr__)，没有别的
has_only_getattr = (
    len(tree.body) == 1
    and isinstance(tree.body[0], ast.FunctionDef)
    and tree.body[0].name == "__getattr__"
)
print("是否只有 __getattr__：", has_only_getattr)
```

**需要观察的现象**：`umath.py` 的顶层语句类型只有 `['FunctionDef']`，且它就是 `__getattr__`。把脚本里的文件名换成 `numeric.py`、`arrayprint.py`、`records.py` 等，结果都一样。

**预期结果**：14 个纯转发垫片的顶层都**只有一个 `FunctionDef` 且名为 `__getattr__`**。这就是「纯转发垫片」的判定依据。

#### 4.2.5 小练习与答案

**练习 1**：`umath.py` 第 2 行的 `from numpy._core import umath` 为什么写在函数**内部**而不是文件顶部？

**参考答案**：为了**惰性加载**。如果写在文件顶部，那么只要 `import numpy.core.umath`（哪怕只是被 `__init__` 的 `__all__` 间接牵连）就会立刻触发对 `numpy._core.umath` 的导入和（在真实现里的）潜在副作用。写在函数内部，则只有真正有人访问属性时才执行一次，符合「按需加载」的设计。

**练习 2**：给定一个只有 `__getattr__` 的垫片文件，你能否不读函数体、只看顶层结构就判断它是「纯转发垫片」？依据是什么？

**参考答案**：能。依据是「顶层除 `__getattr__` 外没有其他实质语句」。用 `ast` 解析后，若 `tree.body` 里除了 `Import`/`ImportFrom`、文档字符串（`Expr`）、`__getattr__` 这个 `FunctionDef`、以及 `del` 语句之外没有别的东西，就是纯转发垫片；只要多出哪怕一个顶层 `for` 循环或赋值，就属于下一节的「特殊垫片」。

---

### 4.3 特殊垫片：`_multiarray_umath.py` 的 eager 绑定与 ABI 守卫

#### 4.3.1 概念说明

目录里有 **3 个文件**不是纯转发：`_internal.py`、`multiarray.py`、`_multiarray_umath.py`。它们**除了 `__getattr__` 之外，在模块顶部还额外「提前绑定（eager binding）」了一些属性**。

为什么要「提前绑定」？因为有两类东西**等不起 `__getattr__` 的懒加载 + 警告**：

1. **pickle 重建对象**：旧版 pickle 字节流里写死了 `numpy.core._multiarray_umath.<某 ufunc>` 这样的路径。反序列化时，pickle 必须能**不报警、不出错**地从这条路径取到对象。所以这些 ufunc 要在模块顶部就被绑成真实属性。
2. **C 扩展的 ABI 符号**：像 `_ARRAY_API` 这种是给用 NumPy 1.x 编译的二进制扩展用的。访问它意味着「你正试图把 1.x 编译的模块跑在 2.x 上」，会崩溃，所以要用 `ImportError`（而不是温和的 `DeprecationWarning`）直接拦截。

本节聚焦最复杂的 `_multiarray_umath.py`；另外两个（`multiarray.py`、`_internal.py`）思路相同，规模更小。（三者的完整对比见 u3-l1。）

#### 4.3.2 核心流程

`_multiarray_umath.py` 的执行流程分两块：

- **模块顶部（import 时立即执行）**：遍历真实现 `_multiarray_umath` 的全部公开名字，凡是 `ufunc` 类型的，就 `globals()[item] = attr` 写进本模块——这样它们就成了本模块的真实属性，pickle 能无警告取到。
- **`__getattr__`（访问其他名字时）**：
  - 如果访问的是 `_ARRAY_API` 或 `_UFUNC_API`：拼一段「NumPy 1.x 编译的模块不能在 2.x 运行」的错误信息，附带调用栈，`raise ImportError`。
  - 否则：走和纯转发一样的 `getattr(..., None)` + 抛警告逻辑。

#### 4.3.3 源码精读

先看顶部的 eager 绑定循环：

[`_multiarray_umath.py:1-9`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_multiarray_umath.py#L1-L9) — 第 4 行 `for item in _multiarray_umath.__dir__():` 遍历真实现所有名字；第 7-9 行若是 `ufunc` 就 `globals()[item] = attr` 绑到本模块。注释点明原因：ufunc 在 pickle 里以 `numpy.core._multiarray_umath` 为路径，必须能无警告、无错误地导入。

再看 `__getattr__` 里对 `_ARRAY_API` / `_UFUNC_API` 的特殊拦截：

[`_multiarray_umath.py:17-46`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_multiarray_umath.py#L17-L46) — 第 17 行判断名字是否属于 `{"_ARRAY_API", "_UFUNC_API"}`；若是，用 `textwrap.dedent` 拼出一段说明，再用 `traceback.format_stack()` 收集调用栈（第 36-39 行跳过 `frozen importlib` 帧），最后 `sys.stderr.write` 打印并 `raise ImportError`。注意这里**故意用 `ImportError` 而不是 `DeprecationWarning`**，因为这是会真的崩溃的 ABI 冲突，必须硬拦。

最后是兜底的普通转发分支：

[`_multiarray_umath.py:48-54`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_multiarray_umath.py#L48-L54) — 不是 ABI 符号就走 `getattr(..., None)` + `_raise_warning`，和纯转发垫片一样。

对照看一个更小的特殊垫片 `multiarray.py`：

[`multiarray.py:5-11`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/multiarray.py#L5-L11) — 第 5-6 行 eager 绑定 `_reconstruct`、`scalar`（为旧 pickle）；第 11 行 eager 绑定 `_ARRAY_API`（为 pybind11 初始化，必须无警告可导入）。这两行就是它「不纯」、属于特殊垫片的铁证。

> `_internal.py` 同理：顶部本地定义 `_reconstruct`、并 eager 绑定 `_dtype_from_pep3118`（见 [`_internal.py:9-16`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_internal.py#L9-L16)），同样属于特殊垫片。

#### 4.3.4 代码实践

**实践目标**：用 `ast` 自动识别「特殊垫片」——它和纯转发垫片的唯一差别，就是顶层多出了 `__getattr__` 以外的实质语句。

**操作步骤**：

```python
# 示例代码：detect_eager_binding.py
import ast
from pathlib import Path

def has_eager_binding(path: Path) -> bool:
    """顶层除 __getattr__/import/docstring/del 之外，是否还有别的语句。"""
    tree = ast.parse(path.read_text())
    for n in tree.body:
        if isinstance(n, (ast.Import, ast.ImportFrom, ast.Expr, ast.Delete)):
            continue
        if isinstance(n, ast.FunctionDef) and n.name == "__getattr__":
            continue
        return True          # 多出来的就是 eager 绑定（for / 赋值 / 另一个函数等）
    return False

for name in ["numeric.py", "umath.py", "multiarray.py", "_internal.py", "_multiarray_umath.py"]:
    p = Path("numpy/core") / name
    print(f"{name:24s} -> {'eager 绑定(特殊垫片)' if has_eager_binding(p) else '纯转发垫片'}")
```

**需要观察的现象**：`numeric.py`、`umath.py` 输出「纯转发垫片」；`multiarray.py`、`_internal.py`、`_multiarray_umath.py` 输出「eager 绑定(特殊垫片)」。

**预期结果**：脚本能 100% 正确区分 14 个纯转发垫片与 3 个特殊垫片。这正是本讲分类表的判定核心。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_multiarray_umath.py` 顶部要用 `for` 循环 + `globals()[item] = attr` 来绑定 ufunc，而不是写成 `from numpy._core._multiarray_umath import *`？

**参考答案**：因为只要绑定**类型为 `ufunc` 的对象**，而不是全部公开名字。`import *` 会把所有名字都搬过来（包括非 ufunc 的函数、常量），那就丧失了「其余名字仍走 `__getattr__` 抛警告」的设计——访问非 ufunc 名字就不会再提示弃用了。用 `isinstance(attr, ufunc)` 精确筛选，既满足 pickle 需求，又保留了对其余名字的弃用提示。

**练习 2**：访问 `_ARRAY_API` 抛 `ImportError`，而访问普通属性只抛 `DeprecationWarning`。为什么这里要「重报」？

**参考答案**：因为二者后果不同。普通属性只是「建议你别用」，功能照常返回；而 `_ARRAY_API` 是 NumPy 1.x 和 2.x 之间**不兼容的 C-ABI 符号**，访问它意味着「用 1.x 编译的二进制模块跑在 2.x 上」，接下来大概率会**直接崩溃**。用 `ImportError` 立刻终止，比让程序带病继续运行更安全。（细节见 u3-l2。）

---

### 4.4 类型存根 `.pyi`：三种写法

#### 4.4.1 概念说明

每个**运行时**垫片模块 `.py`，通常配一个**类型存根** `.pyi`，给静态检查器用。目录里有 17 个 `.pyi`。它们不在运行时执行，**不会**触发弃用警告——类型检查只看签名。

按内容写法，存根可分为三大类（外加两个「没有存根」的特殊情况）：

| 存根写法 | 代表文件 | 特征 |
|---|---|---|
| 完整再导出 | `numeric.pyi`、`umath.pyi` 等 12 个 | `from numpy._core.X import *` |
| 省略再导出（带 NOTE） | `overrides.pyi` | 只写一段注释说明为何不导出 |
| 近空 / 空 | `_internal.pyi`、`_dtype.pyi`、`_dtype_ctypes.pyi` | 仅一行注释或 0 字节 |
| 包入口存根 | `__init__.pyi` | 显式 `import` 子模块 + `__all__` |

特殊情况：`_multiarray_umath.py` 和 `_utils.py` **没有**对应的 `.pyi`。

#### 4.4.2 核心流程

存根的核心取舍是：**这个动态模块的导出内容，类型检查器能否验证？**

- 能验证（真实现有 `__all__` 或 `__dir__`，导出集合明确）→ 写「完整再导出」存根。
- 不能验证（动态转发，没有 `__all__`/`__dir__`，类型检查器无从知道到底导出了什么）→ 要么「省略再导出」并写 NOTE 解释，要么干脆「近空」。

#### 4.4.3 源码精读

完整再导出的代表 `numeric.pyi`：

[`numeric.pyi:1-4`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/numeric.pyi#L1-L4) — `from numpy._core.numeric import *` 把真实现 `numpy._core.numeric` 的全部公开签名搬过来，并显式带上 `__all__`。这样 `numpy.core.numeric` 在类型检查器眼里就和真实现一致。`umath.pyi`、`arrayprint.pyi`、`multiarray.pyi` 等共 12 个都是这种写法。

省略再导出的代表 `overrides.pyi`：

[`overrides.pyi:1-7`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/overrides.pyi#L1-L7) — 整个文件只有一段 NOTE 注释：运行时它动态再导出 `numpy._core.overrides` 的成员，但由于**没有 `__dir__` 或 `__all__`**，签名「无法验证」，而且这个模块本就废弃、不在公开 API 里，所以**故意省略**再导出。这是一种「明知可以写、但选择不写」的工程取舍。

近空存根的代表 `_internal.pyi`：

[`_internal.pyi:1`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_internal.pyi#L1) — 只有一行 `# deprecated module`。`_dtype.pyi` 和 `_dtype_ctypes.pyi` 更极端，是 **0 字节空文件**。它们的作用更多是「占位」，表明这是废弃模块。

包入口存根 `__init__.pyi`，以及「无存根」的说明：

[`__init__.pyi:5-22`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/__init__.pyi#L5-L22) — 显式 `from . import (子模块...)` 逐个导入子模块，再在第 24-42 行写完整的 `__all__`。第 44-45 行有一句关键注释：

[`__init__.pyi:44-45`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/__init__.pyi#L44-L45) — `numpy._core._multiarray_umath has no stubs`，所以只能把它声明成 `ModuleType`。这就解释了为什么 `_multiarray_umath.py` **没有** `.pyi`：它的真实现是一个 C 扩展模块，本就没有 Python 签名可导出。`_utils.py` 则因为是私有工具，也不需要存根。

#### 4.4.4 代码实践

**实践目标**：统计 17 个 `.pyi` 各属于哪种写法，验证「三种写法 + 包入口」的分类。

**操作步骤**：

```python
# 示例代码：classify_stubs.py
from pathlib import Path

CORE = Path("numpy/core")       # 按实际路径修改
for p in sorted(CORE.glob("*.pyi")):
    text = p.read_text().strip()
    if p.name == "__init__.pyi":
        kind = "包入口存根"
    elif "import *" in text:
        kind = "完整再导出"
    elif not text:
        kind = "近空(0 字节)"
    elif text.startswith("#"):
        kind = "省略/近空(仅注释)"
    else:
        kind = "其他"
    print(f"{p.name:24s} -> {kind}")
```

**需要观察的现象**：12 个文件输出「完整再导出」；`overrides.pyi` 输出「省略/近空」；`_internal.pyi` 输出「省略/近空」，`_dtype.pyi`、`_dtype_ctypes.pyi` 输出「近空(0 字节)」；`__init__.pyi` 输出「包入口存根」。

**预期结果**：统计应为——完整再导出 12 个、近空/省略 4 个、包入口 1 个，共 17 个。同时确认 `_multiarray_umath.pyi` 和 `_utils.pyi` **不存在**。（若本地 numpy 版本不同，存根内容可能微调，以本地为准。）

#### 4.4.5 小练习与答案

**练习 1**：为什么 `numeric.pyi` 用 `import *` 完整再导出，而 `overrides.pyi` 却选择省略？

**参考答案**：因为可验证性不同。`numpy._core.numeric` 有明确的 `__all__`，类型检查器能确定它导出了哪些名字，所以 `numeric.pyi` 用 `import *` 安全地镜像这些签名。而 `numpy._core.overrides` 的导出靠动态转发、没有 `__all__`/`__dir__`，类型检查器无法验证到底导出了什么；又因为整个模块已废弃、非公开 API，所以作者选择省略，并写 NOTE 说明理由，避免误导。

**练习 2**：`.pyi` 文件会在运行时触发 `DeprecationWarning` 吗？

**参考答案**：不会。`.pyi` 只在**静态类型检查**时被读取，Python 解释器在运行程序时**根本不执行**它。弃用警告是运行时 `.py` 里 `__getattr__` → `_raise_warning` 才会触发的。这也是为什么 `overrides.pyi` 可以「不导出任何东西」却不影响运行时功能。

---

## 5. 综合实践：自动给全部 36 个文件归类

把前面四个模块的方法合并成一个脚本：遍历 `numpy/core` 全部文件，按 **纯转发 / eager 绑定 / 存根 / 工具 / 包入口** 给每个文件归类，输出一张 Markdown 表格，并注明判断依据。

**实践目标**：用一个脚本一次性产出本讲那张「全局地图」，把分类准则固化成可复用的判断逻辑。

**操作步骤**（示例代码，把 `CORE_DIR` 改成你本地 `numpy/core` 的实际路径）：

```python
# 示例代码：classify_core_files.py
import ast
from pathlib import Path

CORE_DIR = Path("numpy/core")   # 按实际路径修改

def classify_py(path: Path):
    """返回 (类别, 判断依据)。"""
    tree = ast.parse(path.read_text())
    body = tree.body

    if path.name == "__init__.py":
        return "包入口", "文件名 == __init__.py（定义 __all__/__getattr__）"

    if any(isinstance(n, ast.FunctionDef) and n.name == "_raise_warning" for n in body):
        return "工具", "定义了 _raise_warning，且无 __getattr__"

    has_getattr = any(isinstance(n, ast.FunctionDef) and n.name == "__getattr__" for n in body)
    def boilerplate(n):
        if isinstance(n, (ast.Import, ast.ImportFrom, ast.Expr, ast.Delete)):
            return True
        if isinstance(n, ast.FunctionDef) and n.name == "__getattr__":
            return True
        return False
    extras = [n for n in body if not boilerplate(n)]

    if has_getattr and extras:
        return "eager 绑定(特殊垫片)", "顶层除 __getattr__ 外还有 for/赋值/函数"
    if has_getattr and not extras:
        return "纯转发垫片", "顶层只有 __getattr__，无额外绑定"
    return "其他", "—"

def classify_pyi(path: Path):
    if path.name == "__init__.pyi":
        return "存根·包入口", "显式 import 子模块 + __all__"
    text = path.read_text().strip()
    if "import *" in text:
        return "存根·完整再导出", "from numpy._core.X import *"
    if not text:
        return "存根·近空", "0 字节"
    if text.startswith("#"):
        return "存根·省略/近空", "仅注释(如 NOTE)"
    return "存根·其他", "—"

rows = []
for p in sorted(CORE_DIR.iterdir()):
    if p.suffix == ".py":
        cat, why = classify_py(p)
    elif p.suffix == ".pyi":
        cat, why = classify_pyi(p)
    else:
        continue
    rows.append((p.name, cat, why))

print("| 文件 | 类别 | 判断依据 |")
print("|---|---|---|")
for name, cat, why in rows:
    print(f"| {name} | {cat} | {why} |")
```

**需要观察的现象**：脚本应输出约 36 行表格。重点核对三件事：

1. `__init__.py` → 包入口；`_utils.py` → 工具。
2. `numeric.py`、`umath.py` 等 14 个 → 纯转发垫片；`_internal.py`、`multiarray.py`、`_multiarray_umath.py` 这 3 个 → eager 绑定。
3. 所有 `.pyi` → 存根（其中 12 个完整再导出、4 个省略/近空、1 个包入口），且 `_multiarray_umath.pyi`、`_utils.pyi` 不在列表里（因为它们不存在）。

**预期结果**：`.py` 文件 19 个 = 1 包入口 + 1 工具 + 14 纯转发 + 3 eager 绑定；`.pyi` 文件 17 个 = 12 完整再导出 + 4 省略/近空 + 1 包入口。合计 36 个。若你的 numpy 版本与本讲 HEAD（`4e7f3b33`）不同，个别文件可能增减，以本地实际输出为准。

> 进阶玩法：把脚本里的「boilerplate」判定改成更严格的版本（例如只允许 `Import`/`ImportFrom`/`FunctionDef(__getattr__)`，连 `Expr`/`Delete` 都算作「额外」），观察哪些文件的分类会变化，并思考这种更严格判定是否合理。

---

## 6. 本讲小结

- `numpy/core` 共 36 个文件（19 个 `.py` + 17 个 `.pyi`），按运行时角色可分四类：**包入口**、**工具**、**纯转发垫片**、**特殊垫片（eager 绑定）**，外加**类型存根**这一类静态镜像。
- **包入口** `__init__.py` 用 `__all__` 故意「只列子模块名、不在顶部 import」，强制每次访问都走 `__getattr__` 从而打印弃用警告；**工具** `_utils.py` 只提供 `_raise_warning`，是唯一不做转发的 `.py`。
- **纯转发垫片**共 14 个，顶层只有一个 `__getattr__`；其中 13 个用 `None` 判缺失，只有 `numeric.py` 用 sentinel。
- **特殊垫片**共 3 个（`_internal.py`、`multiarray.py`、`_multiarray_umath.py`），在顶部还 eager 绑定了 pickle 重建函数 / ufunc / ABI 符号，原因是这些对象「等不起」懒加载和警告。
- 类型存根 `.pyi` 有三种写法：**完整再导出**（12 个，`import *`）、**省略并写 NOTE**（`overrides.pyi`）、**近空/空**（`_internal.pyi` 等）；`_multiarray_umath.py`、`_utils.py` 没有存根。
- 判定一个 `.py` 是纯转发还是特殊垫片，最可靠的依据是：**用 `ast` 看顶层是否只有 `__getattr__`**——这就是综合实践脚本的核心逻辑。

---

## 7. 下一步学习建议

本讲只建立了「是什么、怎么分」的全局地图。接下来应该深入「怎么工作」：

- **u1-l3**：亲手触发并用 `warnings.catch_warnings` 捕获 `DeprecationWarning`，把本讲的「分类」变成可观测的行为。
- **u2-l1**：深入 PEP 562 模块级 `__getattr__` 的调用时机与惰性转发原理（本讲 4.2 只是点到）。
- **u2-l2**：精读 `None` 写法与 sentinel 写法的差异——为什么 `None` 在面对 falsy 属性时有陷阱（本讲 4.2 埋下的伏笔）。
- **u3-l1 / u3-l2**：分别搞清楚 pickle 向后兼容（为什么必须 eager 绑定）和 C-API/ABI 守卫（为什么 `_ARRAY_API` 要抛 `ImportError`）——本讲 4.3 的两个动机在那里展开。

建议在进入第 2 单元前，先把本讲「综合实践」的脚本跑通，确保你能对任意一个文件脱口而出它的类别与判定依据。
