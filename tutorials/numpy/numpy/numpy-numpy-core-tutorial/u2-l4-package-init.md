# 包入口 __init__.py：__all__、__getattr__ 与 _ufunc_reconstruct

## 1. 本讲目标

前几讲我们看的都是「子模块垫片」——`numeric.py`、`umath.py` 这类文件，它们各自负责把 `numpy.core.numeric.<属性>`、`numpy.core.umath.<属性>` 转发到 `numpy._core`。本讲我们要看的是这些子模块的**宿主**：包入口 `numpy/core/__init__.py`。

它虽然只有短短三十几行，却回答了三个关键问题：

1. `__all__` 里列了一串子模块名，但这些子模块**根本没有在文件顶部被 import**——那它们怎么还能被访问到？这套「只声明、不绑定」的把戏到底起什么作用？
2. 包级的模块级 `__getattr__` 和子模块里的 `__getattr__` 长得几乎一样，却有一个关键差别：它把名字解析到**整个 `_core`**，而不是某个具体子模块。这意味着什么？
3. 文件里还藏着一个函数 `_ufunc_reconstruct`——它是整个 `__init__.py` 里**唯一一个不是转发逻辑**的东西。它为什么必须「提前绑好（eager 绑定）」，而不是和别的名字一样走懒加载？

学完本讲，你应当能：

- 说清 `__all__` + 顶部「不 import」+ `__getattr__` 三者如何协作，把子模块变成「按需加载」。
- 区分包级转发（`numpy.core.X` → `numpy._core.X`）与子模块级转发（`numpy.core.X.attr` → `numpy._core.X.attr`）的差别。
- 解释为什么 `_ufunc_reconstruct` 必须在 `import` 时就真实存在，而不能依赖 `__getattr__`。

## 2. 前置知识

本讲默认你已经学完：

- **u2-l1**：PEP 562 模块级 `__getattr__` 的触发时机（只在正常查找失败时回调、每次失败都触发、抛 `AttributeError` 表示「真不存在」）。
- **u2-l2**：子模块垫片用 `sentinel` 或 `None` 判断「属性是真缺失还是废弃但存在」的两种委派写法。
- **u2-l3**：`_utils._raise_warning(attr, submodule=None)` 如何拼装弃用信息、`stacklevel=3` 如何把警告归因到用户代码，以及 `submodule` 参数如何区分「包级」与「子模块级」警告。

再补三个本讲要用到的小概念：

- **包（package）与子模块（submodule）**：`numpy.core` 是一个**包**（目录里有 `__init__.py`）；`numpy.core.numeric` 是它的一个**子模块**（目录里的 `numeric.py`）。本讲的 `__init__.py` 描述的是「包」这一层。
- **模块名字空间**：访问 `numpy.core.X` 时，Python 先查 `numpy.core.__dict__`（这个包里到底绑定了哪些名字），再查模块类型本身，最后才回调 PEP 562 的 `__getattr__`。本讲的关键就在于「顶部故意不绑定子模块」，迫使查找走进 `__getattr__`。
- **eager 绑定 vs 懒加载**：在模块顶层直接 `def`/`import` 出来的名字，在 `import` 这个模块时就立刻存在（叫 eager 绑定）；而放在 `__getattr__` 里、要等访问时才去取的名字，叫懒加载。u1-l2 已经用 `ast` 区分过这两类文件，本讲我们要精确看 `__init__.py` 同时含**两者**。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲解读了它的什么 |
| --- | --- | --- |
| [`numpy/core/__init__.py`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/__init__.py) | 包入口，整个 `numpy.core` 的「门面」 | `__all__`、`__getattr__`、`_ufunc_reconstruct` 全部内容 |
| [`numpy/core/__init__.pyi`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/__init__.pyi) | 包入口的类型存根 | 与 `.py` 对照：存根为何「反而 eagerly import 所有子模块」，以及为何不提 `_ufunc_reconstruct` |
| [`numpy/core/numeric.py`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/numeric.py) | 子模块垫片（对照样本） | 用来对比「子模块级转发」与「包级转发」的差别 |
| [`numpy/core/_utils.py`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_utils.py) | 弃用信息生成器（u2-l3 精读过） | 只引用 `submodule` 参数如何改变旧模块名 |
| [`numpy/_core/tests/test_ufunc.py`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/tests/test_ufunc.py) | 仓库里的真实测试 | 第 212-215 行藏着一个字面量旧 pickle，证明 `_ufunc_reconstruct` 真的被旧 pickle 引用 |

## 4. 核心概念与源码讲解

### 4.1 `__all__`：声明一份「故意不绑定」的子模块清单

#### 4.1.1 概念说明

在一个普通模块里，`__all__` 通常只是「公开名字清单」，列出的名字都已经实实在在地 `def` 或 `import` 在文件里了。但在 `numpy/core/__init__.py` 里，`__all__` 列了 **17 个子模块名**，而文件顶部**一个都没有 import 它们**。

这是一个刻意的工程决定。回顾 u1-l1 给出的 `numpy.core` 定位：它是「向后兼容垫片」，存在的唯一目的是**每次被访问都打印一条 `DeprecationWarning`**。如果在 `__init__.py` 顶部写 `from . import numeric, umath, ...`，那这些子模块在 `import numpy.core` 时就会全部加载、全部绑进包名字空间——之后用户访问 `numpy.core.numeric` 就会**直接命中 `__dict__`，不再经过 `__getattr__`，也就不会再报警**。

所以这套设计的核心矛盾是：**既要让这些子模块「看起来是公开 API」（出现在 `__all__` 里、能被 IDE 和 `from numpy.core import *` 识别），又绝不能在 import 时就把它们绑死。**

解决办法就是「只声明、不绑定」：把名字写进 `__all__`，但不真的 import。这样名字既属于公开表面，又只能在运行时通过 `__getattr__` 兑现——而每次兑现都伴随一次警告。

#### 4.1.2 核心流程

一个名字（比如 `numeric`）从被声明到被访问的完整生命周期：

```text
import numpy.core
   └─ 执行 __init__.py：
       └─ 顶部只 import _core 和 _raise_warning（不 import 任何子模块）
       └─ 定义 _ufunc_reconstruct（eager）
       └─ 定义 __all__ = [...numeric...]  ← 仅声明，未绑定
       └─ 定义 __getattr__

（此时 numpy.core.__dict__ 里没有 'numeric' 这个键）

访问 numpy.core.numeric
   └─ 查 __dict__  →  没有
   └─ 查模块类型   →  没有
   └─ 回调 __getattr__('numeric')  ← 报警 + 返回真模块
```

那么「`__all__` 不 import 任何东西，它到底起什么作用？」它有三件实事要做：

1. **文档化公开表面**：告诉读者、IDE、文档工具，这 17 个子模块是 `numpy.core` 的公开 API。
2. **驱动 `from numpy.core import *`**：星号导入会遍历 `__all__`，对每个名字调用 `getattr(numpy.core, name)`。因为名字没绑定，每次调用都会触发 `__getattr__` 并报警——恰好让 `import *` 这条「批量取用」路径也被铺上警告。
3. **配合类型存根 `__init__.pyi`**：类型检查器读 `.pyi` 而不是 `.py`，它需要知道这个包暴露了哪些子模块。

#### 4.1.3 源码精读

先看 `__all__` 本体，注意它紧跟着一行说明意图的注释，以及行尾的 `# noqa: F822`：

[`numpy/core/__init__.py:23-28`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/__init__.py#L23-L28) —— 注释「强制惰性加载子模块以确保打印警告」，并声明了 17 个子模块名：

```python
# force lazy-loading of submodules to ensure a warning is printed

__all__ = ["arrayprint", "defchararray", "_dtype_ctypes", "_dtype",  # noqa: F822
           "einsumfunc", "fromnumeric", "function_base", "getlimits",
           "_internal", "multiarray", "_multiarray_umath", "numeric",
           "numerictypes", "overrides", "records", "shape_base", "umath"]
```

两处要点：

- **`# force lazy-loading of submodules`**：这条注释点明了整体设计意图。注意它说的是「强制惰性加载」这个**效果**，而不是说 `__all__` 本身会去 import——真正造成惰性的是「顶部没有 `from . import ...`」，`__all__` 只是配合演戏、维持公开表面。
- **`# noqa: F822`**：`F822` 是 pyflakes 的错误码，含义是「`__all__` 里的名字在当前模块未定义」。正常情况下这是一个该修的 bug；但这里它是**故意**的——这些名字就是留给 `__getattr__` 去懒加载的，所以用 `noqa` 显式压制，并把这个「为什么压制」的意图留在注释里。

再对比看一眼顶部 import 区，确认它**确实没有** import 任何子模块：

[`numpy/core/__init__.py:6-8`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/__init__.py#L6-L8) —— 顶部只引入真实现包和警告工具，子模块一个都没碰：

```python
from numpy import _core

from ._utils import _raise_warning
```

最后看类型存根，它走的是**完全相反**的路子：

[`numpy/core/__init__.pyi:5-22`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/__init__.pyi#L5-L22) —— 存根用 `from . import (...)` 把 16 个子模块**显式**全量导入：

```python
from . import (
    _dtype,
    _dtype_ctypes,
    _internal,
    arrayprint,
    defchararray,
    einsumfunc,
    fromnumeric,
    function_base,
    getlimits,
    multiarray,
    numeric,
    numerictypes,
    overrides,
    records,
    shape_base,
    umath,
)
```

为什么 `.pyi` 敢这么做？因为**类型存根根本不会在运行时执行**——它只供 mypy/pyright 静态读取，既不会触发 import 副作用，也不会触发 `DeprecationWarning`。所以存根可以、也应当把所有公开子模块老老实实地列出来（u3-l3 会专门讲存根策略）。

还有个细节值得注意：存根里 `__all__` 一共 17 个名字，与 `.py` 一致，但 `from . import` 只导了 16 个——唯独漏了 `_multiarray_umath`，并在末尾单独给它一行裸注解：

[`numpy/core/__init__.pyi:44-45`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/__init__.pyi#L44-L45) —— 因为真模块 `numpy._core._multiarray_umath` 是没有存根的 C 扩展，没法 `import *`，只能退而求其次标注成 `ModuleType`：

```python
# `numpy._core._multiarray_umath` has no stubs, so there's nothing to re-export
_multiarray_umath: ModuleType
```

#### 4.1.4 代码实践

**实践目标**：亲眼确认 `numpy.core` 把 17 个子模块名写进了 `__all__`，但其中**没有任何一个**在 `import` 之后立刻出现在包的 `__dict__` 里——它们只能靠 `__getattr__` 兑现。

**操作步骤**（示例代码，需在已安装 numpy 的环境运行）：

```python
# verify_all_lazy.py
import numpy.core as core

# 1) __all__ 里声明了多少个子模块？
print("len(__all__) =", len(core.__all__))
print("numeric in __all__:", "numeric" in core.__all__)

# 2) import 之后，这些名字真的还没被绑定吗？
print("numeric in core.__dict__:", "numeric" in core.__dict__)
print("umath   in core.__dict__:", "umath"   in core.__dict__)
```

**需要观察的现象**：

- `len(__all__)` 应为 17。
- `numeric in core.__dict__` 应为 `False`——这正是「声明了却没绑定」的直接证据。

**预期结果**：名字在 `__all__` 里为 `True`，在 `__dict__` 里为 `False`。

> ⚠️ 一个微妙之处（**待本地验证**）：在某些 Python 版本下，只要别的代码（或解释器启动链）已经触发过 `import numpy.core.numeric`，那么 `numeric` 就会被绑进 `core.__dict__`。如果你看到 `True`，多半是「被别处提前 import 过」造成的；可以在一个全新解释器里、且只 `import numpy.core`（不要先 `import numpy`）时再试一次。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `__init__.py` 里的 `__all__` 整段删掉，`numpy.core.numeric` 还能正常访问吗？报警还会发生吗？

> **答案**：能访问、也会报警。`__getattr__` 的触发**不依赖** `__all__`——只要名字不在 `__dict__`，就会回调 `__getattr__`。删掉 `__all__` 真正影响的是两件事：`from numpy.core import *` 不再知道该导出哪些名字；以及类型检查器/IDE 失去了公开表面的声明。

**练习 2**：`# noqa: F822` 在这里压制的是哪一类静态检查错误？为什么 numpy 必须显式压制它？

> **答案**：F822 = "`__all__` 里的名字在模块中未定义"。因为这里的 17 个名字是**故意**留给运行时 `__getattr__` 懒加载的，静态分析看不到它们的定义，会误报为 bug；numpy 用 `noqa` 告诉 linter「我知道，这是设计」。

---

### 4.2 `__getattr__`：包级转发到整个 `numpy._core`

#### 4.2.1 概念说明

`numpy/core/__init__.py` 里的 `__getattr__` 只有 4 行，是整个文件的核心。它做的事情很朴素：**任何在包名字空间里找不到的名字，都去 `numpy._core` 上取同名属性，取到后报警并返回。**

它和子模块垫片（如 `numeric.py`）里的 `__getattr__` 在结构上几乎一样，但有一个决定性的差别，对应「**包级 vs 子模块级**」的分层：

| 维度 | 包级 `__init__.__getattr__` | 子模块级 `numeric.__getattr__` |
| --- | --- | --- |
| 解析目标 | `getattr(_core, name)`——在整个 `numpy._core` 上找 | `getattr(numpy._core.numeric, name)`——只在具体子模块上找 |
| 能命中的名字 | 子模块名（`numeric`、`umath`…）**和** `_core` 顶层类型（`ndarray`、`float64`…）都行 | 只能命中 `numeric` 这一个子模块里的属性（如 `asarray`） |
| 警告的「旧模块名」 | `numpy.core`（不传 `submodule`） | `numpy.core.numeric`（传 `submodule="numeric"`） |
| 缺失处理 | 交给 `getattr` 自己抛 `AttributeError`，不报警 | 用 sentinel/None 自己判缺失，抛自定义 `AttributeError`，不报警 |

关键直觉是：**包级 `__getattr__` 是「第一跳」**，它把 `numpy.core.X` 兑现成「真模块 `_core` 上那个叫 X 的东西」；因为 `_core` 这个包本身既暴露子模块、也暴露顶层类型，所以这一跳什么名字都能接。而**子模块垫片是「第二跳」**，专门把 `numpy.core.X.attr` 里的 `attr` 兑现成 `numpy._core.X.attr`。

#### 4.2.2 核心流程

包级访问 `numpy.core.<名字>` 时的执行流程：

```text
用户访问 numpy.core.<名字>
  ├─ __dict__ 命中？  →（极少发生，例如 _ufunc_reconstruct 这种 eager 名字）直接返回，不报警
  └─ 未命中 → __getattr__(<名字>)
       ├─ attr = getattr(_core, <名字>)
       │     ├─ _core 有这个名字 → 得到对象（可能是子模块，也可能是 ndarray 这样的类型）
       │     └─ _core 没有        → getattr 直接抛 AttributeError（此时不报警，正确）
       ├─ _raise_warning(<名字>)        ← 注意：不传 submodule → 报「numpy.core 已弃用」
       └─ return attr
```

和子模块级的对照（回顾 u2-l2 的 `numeric.py`）：

```text
用户访问 numpy.core.numeric.<属性>   （前提：numeric 这个垫片模块已被加载）
  └─ numeric.__getattr__(<属性>)
       ├─ from numpy._core import numeric   ← 只导入这一个真子模块
       ├─ ret = getattr(numeric, <属性>, sentinel)
       ├─ ret is sentinel → AttributeError（不报警）
       └─ _raise_warning(<属性>, "numeric")  ← 传 submodule → 报「numpy.core.numeric 已弃用」
```

两个流程形状一致，差别就在「解析目标」和「要不要传 `submodule`」这两处——它们共同决定了警告文本里写的是 `numpy.core` 还是 `numpy.core.numeric`。

#### 4.2.3 源码精读

包级 `__getattr__` 本体：

[`numpy/core/__init__.py:30-33`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/__init__.py#L30-L33) —— 包级转发：把名字解析到整个 `_core`，报警时不带子模块名：

```python
def __getattr__(attr_name):
    attr = getattr(_core, attr_name)
    _raise_warning(attr_name)
    return attr
```

逐行看它的三个决定：

1. **`getattr(_core, attr_name)`**：注意 `_core` 是一个**包**（由顶部 [`__init__.py:6`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/__init__.py#L6) 的 `from numpy import _core` 引入）。对包做 `getattr` 既能取到它的子模块（`_core.numeric`），也能取到它顶层定义的类型（`_core.ndarray`）。所以这一行同时支撑了 `numpy.core.numeric` 和 `numpy.core.ndarray` 两类访问。
2. **没有默认值、没有显式 `try/except`**：如果 `_core` 上不存在这个名字，`getattr` 自己就会抛 `AttributeError`，而且此时 `_raise_warning` 这一行**根本不会执行**——所以「真不存在的名字」既会得到正确的 `AttributeError`，又**不会**误报警。这是一个简洁但容易看漏的正确性细节。
3. **`_raise_warning(attr_name)` 不传 `submodule`**：回顾 u2-l3，[`_utils.py:7-9`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_utils.py#L7-L9) 里 `submodule is None` 时旧模块名就是 `numpy.core`，于是最终警告文本会写成「`numpy.core` is deprecated…use `numpy._core.<attr_name>`」。

再看子模块级，作为对照：

[`numpy/core/numeric.py:1-12`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/numeric.py#L1-L12) —— 子模块级转发：解析目标限定到 `numpy._core.numeric`，报警时带上 `"numeric"`：

```python
def __getattr__(attr_name):
    from numpy._core import numeric

    from ._utils import _raise_warning

    sentinel = object()
    ret = getattr(numeric, attr_name, sentinel)
    if ret is sentinel:
        raise AttributeError(
            f"module 'numpy.core.numeric' has no attribute {attr_name}")
    _raise_warning(attr_name, "numeric")
    return ret
```

把两段并排看，差异点就一目了然：

- 包级 `getattr(_core, …)` ↔ 子模块级 `getattr(numeric, …)`：**解析目标的范围**不同。
- 包级 `_raise_warning(attr_name)` ↔ 子模块级 `_raise_warning(attr_name, "numeric")`：**是否传 `submodule`**，决定警告里写 `numpy.core` 还是 `numpy.core.numeric`。
- 包级依赖 `getattr` 自带 `AttributeError` ↔ 子模块级用 `sentinel` 自判：缺失处理的写法不同（这与 u2-l2 的两种委派模式一脉相承，包级相当于「省略了缺失判定，因为 `getattr` 不给默认值时本来就抛错」）。

#### 4.2.4 代码实践

**实践目标**：验证包级 `__getattr__` 能同时解析「子模块名」和「`_core` 顶层类型名」，并且对「真不存在的名字」只会抛 `AttributeError`、不会报警。

**操作步骤**（示例代码）：

```python
# verify_package_getattr.py
import warnings
import numpy.core as core

def grab(name):
    """访问 core.<name>，返回 (取到的对象, 捕获到的弃用警告文本 or None)。"""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        try:
            obj = getattr(core, name)
        except AttributeError as e:
            return (None, [str(w.message) for w in caught], f"AttributeError: {e}")
    return (obj, [str(w.message) for w in caught], None)

# 1) 子模块名：numeric
obj, warns, err = grab("numeric")
print("numeric ->", obj, "| 警告数:", len(warns), "| 错误:", err)
print("   警告含 'numpy._core':", any("numpy._core" in w for w in warns))

# 2) _core 顶层类型名：ndarray
obj, warns, err = grab("ndarray")
print("ndarray ->", obj.__name__, "| 警告数:", len(warns))

# 3) 真不存在的名字：应该只抛 AttributeError，且不报警
obj, warns, err = grab("this_does_not_exist")
print("missing -> 警告数:", len(warns), "| 错误:", err)
```

**需要观察的现象**：

- `numeric` 和 `ndarray` 都能取到对象，且各产生 1 条警告，警告文本里含 `numpy._core`。
- `this_does_not_exist` 取不到对象、抛 `AttributeError`，且**警告数为 0**——证明 `_raise_warning` 在缺失分支没被执行。

**预期结果**：前两行警告数 ≥ 1；第三行警告数为 0、`err` 为 `AttributeError`。如不确定你环境下的确切返回对象（`numeric` 可能解析到真模块也可能解析到垫片，取决于是否被预先 import），相关现象标注为「**待本地验证**」，重点只看「是否报警 / 是否抛错」这两条结论。

#### 4.2.5 小练习与答案

**练习 1**：为什么包级 `__getattr__` 不需要像 `numeric.py` 那样写 `sentinel` 判缺失？

> **答案**：因为包级用的是 `getattr(_core, attr_name)`，**没传默认值**。当名字不存在时，`getattr` 本身就会抛 `AttributeError`，而且是在 `_raise_warning` 之前抛出，所以「真缺失」自动得到 `AttributeError`、自动不报警，无需自判。`numeric.py` 用 sentinel 是因为它的 `getattr` 传了默认值（要把「缺失」映射成一个可检测的值），需要自己再把这种「缺失」翻译回 `AttributeError`。

**练习 2**：访问 `numpy.core.numeric` 时，包级 `__getattr__` 会把它解析到什么？这条警告里写的是 `numpy.core` 还是 `numpy.core.numeric`？

> **答案**：解析到 `getattr(_core, "numeric")`，即真实的 `numpy._core.numeric` 子模块。因为调用的是 `_raise_warning("numeric")`、**没传** `submodule`，所以警告文本里写的是 `numpy.core`（提示改用 `numpy._core.numeric`），而不是 `numpy.core.numeric`。

---

### 4.3 `_ufunc_reconstruct`：为旧版 pickle 保留的 eager 绑定

#### 4.3.1 概念说明

整个 `__init__.py` 里，`__getattr__` 和 `__all__` 负责「懒加载 + 报警」，但还有一个函数显得格格不入：`_ufunc_reconstruct`。它**直接 `def` 在模块顶层**（eager 绑定），既不转发、也不报警，看起来和「垫片」的主旋律毫无关系。

它存在的原因和 **pickle** 有关。pickle 在保存一个对象时，并不会保存对象的字节码，而是记下「**这个对象住在哪个模块、叫什么名字**」（模块路径 + 名字）。还原时，pickle 用这两个信息重新「找回」对象。

NumPy 1.20 之前，序列化一个 ufunc（通用函数，比如 `np.add`、`np.cos`）时，pickle 里写下的「重建函数」是 `numpy.core._ufunc_reconstruct`。也就是说，全世界存在无数个**几年前存的旧 pickle**，它们字节流里硬编码着 `numpy.core._ufunc_reconstruct` 这个路径。如果 NumPy 2.0 把这个名字删了，这些旧 pickle 就再也打不开了。

所以 `_ufunc_reconstruct` 必须满足一个硬约束：**`import numpy.core` 之后，`numpy.core._ufunc_reconstruct` 必须立刻、确实地存在，而且访问它时不能报警、不能走懒加载。** 这正是把它 `def` 在顶层的理由——顶层 `def` 的名字在 `import` 完成后就躺在 `__dict__` 里，访问时**直接命中**，既绕过 `__getattr__`，也绕过 `_raise_warning`。

> 这就是 u1-l2 引入的「**eager 绑定**」思想的具体落地：有些名字等不起懒加载与警告，必须在 import 时就绑死。本讲的 `_ufunc_reconstruct` 是 `__init__.py` 里的唯一一例；更系统、更完整的 pickle 兼容机制（`_internal._reconstruct`、ufunc 全量绑定）留到 u3-l1 展开。

#### 4.3.2 核心流程

旧 pickle 还原一个 ufunc 的全过程（以「存了 `np.cos`」为例）：

```text
旧 pickle 字节流（1.20 之前）大致结构：
   GLOBAL  numpy.core  _ufunc_reconstruct   ← 重建函数，按路径取
   STRING  'numpy._core.umath'              ┐
   STRING  'cos'                            ┘ 作为参数传给重建函数
   TUPLE + REDUCE                            ← 调用重建函数(模块, 名字)

pickle.loads 还原时：
   1) 用路径 numpy.core._ufunc_reconstruct 找到函数
        └─ 因为它被 eager 绑定在 __dict__，直接命中，不报警 ✓
   2) 调用 _ufunc_reconstruct('numpy._core.umath', 'cos')
        └─ mod = __import__('numpy._core.umath', fromlist=['cos'])  → 拿到最内层模块
        └─ return getattr(mod, 'cos')                              → 返回 np.cos
```

注意第 2 步里 `__import__` 带的 `fromlist=[name]`：这是 Python 内置 `__import__` 的一个历史怪癖——**不给 `fromlist`，`__import__('a.b.c')` 返回的是顶层包 `a`**；给了 `fromlist`，才返回最内层的 `a.b.c`。注释专门提到，这让 `scipy.special.expit` 这类「非顶层」的 ufunc 也能被正确找回。

#### 4.3.3 源码精读

`_ufunc_reconstruct` 的完整定义，连同注释一起看：

[`numpy/core/__init__.py:11-20`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/__init__.py#L11-L20) —— 顶层 eager 定义的 pickle 重建函数，注释说明它纯粹为兼容旧 pickle 而保留：

```python
# We used to use `np.core._ufunc_reconstruct` to unpickle.
# This is unnecessary, but old pickles saved before 1.20 will be using it,
# and there is no reason to break loading them.
def _ufunc_reconstruct(module, name):
    # The `fromlist` kwarg is required to ensure that `mod` points to the
    # inner-most module rather than the parent package when module name is
    # nested. This makes it possible to pickle non-toplevel ufuncs such as
    # scipy.special.expit for instance.
    mod = __import__(module, fromlist=[name])
    return getattr(mod, name)
```

四个理解点：

1. **注释「This is unnecessary」**：对新版 NumPy 而言，序列化 ufunc 早就不需要这个函数了——现代 pickle 直接记录 ufunc 的模块路径即可。它之所以还在，**纯粹**是为了能加载 1.20 之前的旧 pickle（「there is no reason to break loading them」）。
2. **`def` 在顶层 = eager 绑定**：与 `numeric` 这类名字不同，`_ufunc_reconstruct` 在 `import numpy.core` 后就**确实**存在于 `numpy.core.__dict__`。所以 `numpy.core._ufunc_reconstruct` 这个访问会**直接命中 `__dict__`**，既不进 `__getattr__`，也不会触发 `_raise_warning`。这正是它「不报警」的机制原因。
3. **`__import__(module, fromlist=[name])`**：用内置 `__import__` 按 `module` 路径导入真模块；`fromlist` 保证拿到的是最内层模块（详见 4.3.2）。
4. **`return getattr(mod, name)`**：从真模块上取出真正的 ufunc 对象并返回。整个过程**不碰 `numpy.core` 的任何转发逻辑**，直接走 `_core` 的真名字空间。

这套「旧 pickle 还能加载」的承诺不是空话——仓库里有一条真实测试，把一个字面量旧 pickle 喂给 `pickle.loads`，断言它还原出来就是 `np.cos`：

[`numpy/_core/tests/test_ufunc.py:212-215`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/tests/test_ufunc.py#L212-L215) —— 真实测试：旧 pickle 字节流里硬编码了 `numpy.core` / `_ufunc_reconstruct`，加载后应等于 `np.cos`：

```python
def test_pickle_withstring(self):
    astring = (b"cnumpy.core\n_ufunc_reconstruct\np0\n"
               b"(S'numpy._core.umath'\np1\nS'cos'\np2\ntp3\nRp4\n.")
    assert_(pickle.loads(astring) is np.cos)
```

读懂这串 pickle 操作码（不必全记，感受一下「路径被硬编码」即可）：

- `cnumpy.core\n_ufunc_reconstruct\n` —— `c` 是 GLOBAL 操作码，意思是「按 `numpy.core._ufunc_reconstruct` 这个路径取回一个对象」。**这就是旧 pickle 把 `numpy.core` 写死的铁证。**
- `S'numpy._core.umath'`、`S'cos'` —— 两个字符串参数。
- `(`…`t` —— 把它们打包成元组 `('numpy._core.umath', 'cos')`。
- `R` —— REDUCE：用前面取到的函数调用这个元组，即 `_ufunc_reconstruct('numpy._core.umath', 'cos')`，返回 `np.cos`。

最后顺带看一个对照细节：类型存根 `__init__.pyi` 里**完全没有** `_ufunc_reconstruct`。因为它是内部 pickle 助手，不属于公开类型表面，类型检查器不需要知道它。这也再次印证「`.py` 与 `.pyi` 描述的是两套东西」：运行时为了兼容旧 pickle 必须保留它，但类型表面可以隐去它。

#### 4.3.4 代码实践

**实践目标**：亲手调用 `_ufunc_reconstruct`，复现旧 pickle 还原 ufunc 的那一步；并验证它确实是 eager 绑定（在 `__dict__` 里、访问不报警）。

**操作步骤**（示例代码）：

```python
# verify_ufunc_reconstruct.py
import warnings
import numpy.core as core

# 1) 它确实被 eager 绑定在 __dict__（而不只是 __all__）：
print("_ufunc_reconstruct in __dict__:",
      "_ufunc_reconstruct" in core.__dict__)

# 2) 访问它不报警（对照：访问 numeric 会报警）
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    fn = core._ufunc_reconstruct
print("访问 _ufunc_reconstruct 的警告数:", len(caught))   # 预期 0

# 3) 复现旧 pickle 的还原步骤：按 (模块, 名字) 取回 np.cos
cos_again = core._ufunc_reconstruct("numpy._core.umath", "cos")
print("取到的对象就是 np.cos:", cos_again is __import__("numpy").cos)
```

**需要观察的现象**：

- `_ufunc_reconstruct` 在 `core.__dict__` 中为 `True`（eager 绑定的直接证据）。
- 访问它**不产生任何警告**（因为它直接命中 `__dict__`，不进 `__getattr__`）。
- 第 3 步取回的对象与 `np.cos` 是同一个（`is` 判定为 `True`）。

**预期结果**：三步依次为 `True`、`0`、`True`。

> 💡 进阶验证（**待本地验证**）：把第 3 步的实参换成 `__import__("numpy._core.umath")`（**不带** `fromlist`），观察会发生什么。预期：`__import__` 会返回顶层包 `numpy` 而不是 `numpy._core.umath`，于是 `getattr(numpy, "cos")` 很可能抛 `AttributeError`——这正是源码注释强调 `fromlist` 不可省的原因。

#### 4.3.5 小练习与答案

**练习 1**：如果改成把 `_ufunc_reconstruct` 从顶层 `def` 删掉，改成塞进 `__getattr__` 里懒加载，旧 pickle 还能正常加载吗？为什么？

> **答案**：仍然能「找到」它（`__getattr__` 会兜底），但会**额外触发一次 `DeprecationWarning`**——因为 `__getattr__` 里会调 `_raise_warning`。更要命的是，`pickle.loads` 内部对「找不到名字」有时会直接报错，依赖具体调用路径；即便能加载，对「静默还原旧数据」这个场景来说，「每次加载都报警」也是不可接受的退化。所以它必须是 eager、不报警的顶层绑定。

**练习 2**：`_ufunc_reconstruct` 里的 `__import__(module, fromlist=[name])` 如果去掉 `fromlist` 参数，对哪一类 ufunc 会出问题？

> **答案**：对「**非顶层**」的 ufunc 会出问题，比如 `scipy.special.expit`。因为不带 `fromlist` 时，`__import__('scipy.special')` 返回的是顶层 `scipy` 包，而不是 `scipy.special`，随后 `getattr(scipy, 'expit')` 就找不到对象。带 `fromlist=[name]` 才能确保拿到最内层模块。

---

## 5. 综合实践

**综合目标**：把本讲三个最小模块串起来——亲手造一个「**故意只声明、不绑定**」的惰性包 `mymath`，复刻 `numpy.core.__init__.py` 的 `__all__` + `__getattr__` 协作，并验证「import 包本身不加载任何子模块」。

**任务背景**：假设你有一个真实现包 `_real`，里面有两个「昂贵」子模块 `heavy_a` 和 `heavy_b`（用一个全局标志模拟「加载即有副作用」）。你要给它们做一个垫片包 `mymath`：子模块名进 `__all__`，但顶部不 import，访问时才懒加载并打印日志。

**目录结构**（示例代码，需自行创建这些文件）：

```text
mymath/
├── __init__.py        # 垫片包入口
└── _real/             # 真实现
    ├── __init__.py
    ├── heavy_a.py
    └── heavy_b.py
```

`mymath/_real/__init__.py`（空文件即可）。

`mymath/_real/heavy_a.py`（真实现，加载时打印日志）：

```python
# mymath/_real/heavy_a.py
print("[加载] _real.heavy_a 被导入")   # 模拟「昂贵的加载副作用」

VALUE_A = "I am heavy_a"
```

`mymath/_real/heavy_b.py`：

```python
# mymath/_real/heavy_b.py
print("[加载] _real.heavy_b 被导入")

VALUE_B = "I am heavy_b"
```

`mymath/__init__.py`（垫片包入口，复刻 `numpy.core.__init__.py` 的手法）：

```python
# mymath/__init__.py
from . import _real   # 引入真实现包（仅引入包本身，不引入其子模块）

# 只声明、不绑定：复刻 numpy/core/__init__.py 的 __all__ 设计
__all__ = ["heavy_a", "heavy_b"]  # noqa: F822

# 加载日志，用来证明「按需加载」
_load_log = []

def __getattr__(attr_name):
    # 复刻 numpy.core 的包级转发：从整个 _real 上取同名子模块
    attr = getattr(_real, attr_name)  # 注意：缺失时 getattr 自己抛 AttributeError
    _load_log.append(attr_name)        # 模拟 numpy 里的 _raise_warning 副作用
    print(f"[转发] 首次访问 mymath.{attr_name}，触发加载")
    return attr
```

**验证脚本** `verify_mymath.py`（放在 `mymath` 的**上级目录**运行）：

```python
# verify_mymath.py
import mymath

# 断言 1：import 包本身不应触发任何子模块加载（stdout 里不应出现 [加载] ...）
print("--- 刚 import 完 mymath ---")
print("heavy_a in mymath.__dict__:", "heavy_a" in mymath.__dict__)  # 期望 False
print("加载日志:", mymath._load_log)                                # 期望 []

# 断言 2：首次访问 heavy_a 才触发加载
print("\n--- 访问 mymath.heavy_a ---")
a = mymath.heavy_a
print("取到:", a.VALUE_A)
print("加载日志:", mymath._load_log)                                # 期望 ['heavy_a']

# 断言 3：再次访问不再触发（因为已绑进 _real，但本垫片每次仍走 __getattr__；
#         可观察加载日志是否重复）
print("\n--- 再次访问 mymath.heavy_a ---")
a2 = mymath.heavy_a
print("加载日志:", mymath._load_log)                                # 观察 heavy_a 是否重复
```

**需要观察并思考的现象**：

1. `import mymath` 之后，控制台**不应**出现 `[加载] _real.heavy_a`——证明顶部 `from . import _real` 只引入了 `_real` 包本身，没有波及其子模块，惰性目标达成。
2. 首次 `mymath.heavy_a` 时，`_real.heavy_a` 才被加载，日志里多出 `heavy_a`。
3. 思考题：在这个简化版里，第二次访问 `mymath.heavy_a` 时，`_load_log` 是否会再次追加 `heavy_a`？把它和「真 numpy」对比——真 numpy 的包级 `__getattr__` **每次都会**报警（因为 `_raise_warning` 无条件执行），而 `getattr(_real, name)` 每次都返回同一个已加载的子模块对象。也就是说「**转发动作每次都发生（报警/打日志），但真模块只加载一次（被 sys.modules 缓存）**」。请用你的加载日志验证这一点，并解释为什么这不会造成性能问题。

**预期结果**：断言 1、2 如上；断言 3 的结论是「`_load_log` 会重复追加 `heavy_a`，但 `_real.heavy_a` 的模块体（那行 `print`）只执行一次」——因为 Python 的 `sys.modules` 缓存保证真模块只初始化一次，而 `__getattr__` 的转发逻辑每次访问都跑。

> ⚠️ 如果你观察到「import 包本身时就打印了 `[加载]`」，请检查 `mymath/__init__.py` 顶部是否误写了 `from ._real import heavy_a`——那会把惰性破坏掉，正好反证「顶部不能 import 子模块」这条铁律。

## 6. 本讲小结

- `numpy/core/__init__.py` 的 `__all__` 列了 17 个子模块名，但顶部**一个都没 import**——这是「只声明、不绑定」，目的是让子模块只能经 `__getattr__` 兑现，从而**每次访问都打印 `DeprecationWarning`**；`# noqa: F822` 压制的正是「`__all__` 里有未定义名字」的 linter 误报。
- 包级 `__getattr__` 把任何找不到的名字解析到**整个 `_core`**（`getattr(_core, name)`），因此它同时能接子模块名（`numeric`）和 `_core` 顶层类型名（`ndarray`）；缺失时由 `getattr` 自己抛 `AttributeError`、且不报警。
- 包级与子模块级转发的差别在两点：**解析目标的范围**（`_core` 整体 vs `numpy._core.numeric` 具体）、**是否给 `_raise_warning` 传 `submodule`**（决定警告写 `numpy.core` 还是 `numpy.core.numeric`）。
- `_ufunc_reconstruct` 是整个 `__init__.py` 里**唯一的 eager 绑定**：它直接 `def` 在顶层，import 后就躺在 `__dict__`，访问时**直接命中、不报警、不懒加载**——因为 1.20 之前的旧 pickle 在字节流里硬编码了 `numpy.core._ufunc_reconstruct`，它必须随时可用；`test_ufunc.py:212-215` 的真实测试就是这条兼容承诺的活证据。
- 类型存根 `__init__.pyi` 走相反路线：它**显式全量 import** 子模块（存根不执行、不报警），且**不提** `_ufunc_reconstruct`（内部 pickle 助手不属于公开类型表面）——这再次说明 `.py` 与 `.pyi` 描述的是两套关注点。

## 7. 下一步学习建议

本讲把 `__init__.py` 这一层的机制讲透了，接下来：

- **继续往下走（u3-l1 pickle 兼容）**：本讲的 `_ufunc_reconstruct` 只是 pickle 兼容的一个引子。u3-l1 会系统讲 `_internal._reconstruct`、`_multiarray_umath` 里 ufunc 的**全量** eager 绑定，回答「为什么 pickle 场景下，一大片对象都必须 eager、必须不报警」。
- **继续往下走（u3-l2 ABI 守卫）**：本讲看到的包级 `__getattr__` 只处理「普通名字」，但 `_multiarray_umath` 这个子模块里还有一段更激烈的 `__getattr__`——访问 `_ARRAY_API` 时会直接抛 `ImportError`，硬拦「NumPy 1.x 编译的二进制跑在 2.x 上」的崩溃。
- **回头看（u3-l3 类型存根）**：本讲对 `__init__.pyi` 的对比只是点了一下。u3-l3 会系统比较「完整再导出 / 省略并写 NOTE / 近空」三种 `.pyi` 策略，解释废弃模块的存根取舍。
- **本讲的实践延伸**：把第 5 节的 `mymath` 垫片继续扩展——给每个子模块也加上「子模块级 `__getattr__`」和「`_raise_warning` 式的统一报警函数」，你就已经在向 u3-l4「亲手设计一个生产级兼容垫片」靠近了。
