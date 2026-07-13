# 综合实践：为自己的模块设计一个兼容垫片

## 1. 本讲目标

本讲是整本手册的收尾。前面十讲我们把 `numpy.core` 这个兼容垫片拆成了零件：模块级 `__getattr__`、惰性转发、sentinel/None 两种委派写法、统一弃用警告 `_raise_warning`、pickle 的 eager 绑定、ABI 冲突的 ImportError 守卫、以及类型存根 `.pyi` 的取舍。

学完本讲，你应该能够：

- 把上述所有零件**重新组装**成一个完整的生产级兼容垫片包。
- 在一个真实改名场景里（`mymath.core` → `mymath._core`）独立判断：每一个名字该走「惰性报警」「eager 放行」还是「ImportError 硬拦」。
- 给废弃包配上 `.pyi`，并写出验收脚本同时验证三件事——访问触发弃用警告但功能正常、旧 pickle 可还原、类型检查通过。
- 评估一个垫片**何时可以被安全移除**。

## 2. 前置知识

本讲是综合实践，默认你已经读过以下讲义（概念不再重复，只做承接）：

- **u2-l1**：PEP 562 模块级 `__getattr__(name)`（无 `self`）只在正常属性查找失败时回调，每次失败都触发，不自动缓存。
- **u2-l2**：判断「属性真缺失 vs 废弃但存在」的两种写法——`sentinel = object()`（安全）与 `None`（对值为 `None` 的真属性会翻车）。
- **u2-l3**：`_raise_warning` 是「单一信息源」，固定 `stacklevel=3`，因为调用链「用户 → `__getattr__` → `_raise_warning` → `warn`」恒为 3 帧。
- **u2-l4**：包入口 `__init__.py` 用「只声明、不绑定」的 `__all__` 强制每次访问走 `__getattr__`，从而打印警告。
- **u3-l1**：pickle 的 `find_class(module, name)` 契约——反序列化靠「模块路径+名字」定位重建函数，路径被硬编码进字节流；重建函数等不起警告，必须 eager 绑定进 `__dict__` 绕开 `__getattr__`。
- **u3-l2**：NumPy 1.x/2.x 的 C-ABI 不兼容只能用 `ImportError` 硬拦，普通废弃属性用 `DeprecationWarning` 软报警；「绑不绑进 `__dict__`」即「放不放行」。
- **u3-l3**：类型存根运行时不执行，永不触发警告；按「可验证性原则」分完整再导出、省略再导出、近空三类。

一个贯穿全讲的通俗类比：兼容垫片是一座**电话总机**。旧号码（`mymath.core.xxx`）已经不存在了，但总机仍然接听，绝大多数电话会「转接给新号码 + 提醒一句这个旧号码要废弃」（惰性报警）；少数被老设备用固定格式反复拨打的分机（pickle 重建函数），总机直接**预先登记**好，来电不提醒直接接通（eager 绑定）；极少数会**烧毁设备**的来电（ABI 冲突），总机直接挂断并报火警（ImportError 硬拦）。

## 3. 本讲源码地图

本讲在综合实践中反复对照 `numpy/core` 的真实代码：

| 文件 | 作用 | 在本讲扮演的角色 |
|------|------|------------------|
| [numpy/core/__init__.py](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/__init__.py) | 包入口垫片 | 「包级惰性 + `_ufunc_reconstruct` eager」的范本 |
| [numpy/core/_utils.py](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_utils.py) | 唯一不做转发的工具 | `_raise_warning` 的范本 |
| [numpy/core/numeric.py](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/numeric.py) | sentinel 写法的纯转发子模块 | 「子模块垫片」范本 |
| [numpy/core/multiarray.py](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/multiarray.py) | 带 pickle eager 绑定的特殊垫片 | 「pickle 兼容」范本 |
| [numpy/core/_internal.py](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_internal.py) | 带 `_reconstruct` eager 的特殊垫片 | 「免警告导入」范本 |
| [numpy/core/_multiarray_umath.py](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_multiarray_umath.py) | 带 ufunc eager + ABI 守卫的特殊垫片 | 「致命路径硬拦」范本 |
| [numpy/core/numeric.pyi](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/numeric.pyi) | 完整再导出存根 | 存根范本 |
| [numpy/core/__init__.pyi](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/__init__.pyi) | 包入口全量 import 存根 | 存根范本 |

---

## 4. 核心概念与源码讲解

### 4.1 整体设计：从一个改名场景出发

#### 4.1.1 概念说明

我们虚构一个场景：你维护一个小库 `mymath`，它的底层实现在包 `mymath.core` 里。在 2.0 版本你决定把内部命名空间改成私有的 `mymath._core`（和 NumPy 2.0 一模一样的动机：把 internals 藏到下划线开头）。但是：

- 下游用户的代码里写满了 `from mymath.core.basic import add`。
- 磁盘上还躺着几年前 `pickle.dumps` 出来的文件，字节流里硬编码着 `mymath.core.basic._reconstruct`。
- 有人用 `mypy`/`pyright` 做静态检查，需要类型信息。

直接删掉 `mymath.core` 会让这三类用户瞬间崩溃。你需要一个**兼容垫片（shim）**：一个名字还是 `mymath.core`、但内容全部转发到 `mymath._core` 的薄壳包，并且：

1. 绝大多数访问 → 转发 + `DeprecationWarning`（提示用户迁移）。
2. 出现在旧 pickle 里的少数名字 → 静默转发（不能报警，否则污染 unpickle）。
3. 会引发崩溃的致命访问 → 直接 `raise`（如果你的库也有 C-ABI 这种问题）。
4. 配套 `.pyi`，让静态检查不报错。

#### 4.1.2 核心流程

垫片的本质是「**对每一个名字做分类决策**」。当你准备把一个真模块 `_core/basic.py` 包一层垫片时，逐个名字过一遍这张决策表：

```text
对一个「旧公开名字」name，问三个问题：

Q1: name 会不会出现在旧 pickle 字节流里（重建函数、被 __reduce 引用的类/函数）?
    └─ 是  → 顶部 eager 绑定：globals()[name] = getattr(_real, name)
              （进 __dict__，绕过 __getattr__，不报警，pickle 能直接命中）

Q2: name 是不是一个已知会引发段错误/崩溃的旧 C-ABI 符号?
    └─ 是  → 在 __getattr__ 里特判：raise ImportError（硬拦）

Q3: 都不是
    └─ 走 __getattr__ 惰性转发 + _raise_warning（软报警）
```

整包的文件布局如下（后面 4.2~4.5 会逐个实现）：

```text
mymath/
├── __init__.py            # 真实顶层包（不在本讲重点）
├── _core/                 # 【真实现，私有】
│   ├── __init__.py
│   └── basic.py           # 含 PI / add / Vec / _reconstruct
└── core/                  # 【我们要搭的兼容垫片】
    ├── __init__.py        # 包入口：__all__ + __getattr__（4.3）
    ├── _utils.py          # _raise_warning（4.2）
    ├── basic.py           # 纯转发 + pickle eager（4.4）
    ├── basic.pyi          # 类型存根（4.5）
    └── __init__.pyi       # 包入口存根（4.5）
```

#### 4.1.3 源码精读

NumPy 自己的 `numpy.core` 就是按这张决策表设计的，最直接的证据是包入口开头的模块文档字符串——它一上来就声明了垫片的命运：

[numpy/core/__init__.py:1-5](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/__init__.py#L1-L5) —— 模块文档字符串明确说 `numpy.core` 仅为向后兼容而存在，原 `core` 已改名为私有 `_core`，未来会移除。这五行就是我们要在 `mymath/core/__init__.py` 顶部照抄的「垫片宣言」。

三类名字在 numpy 里分别对应三件实物：

- **eager 绑定（pickle 兼容）**：`numpy/core/__init__.py:14-20` 的 `_ufunc_reconstruct`、`numpy/core/multiarray.py:5-6` 的 `_reconstruct`/`scalar` 循环、`numpy/core/_internal.py:9-11` 的 `_reconstruct`。
- **ImportError 硬拦（ABI）**：`numpy/core/_multiarray_umath.py:17-46` 对 `_ARRAY_API`/`_UFUNC_API` 的处理。
- **惰性报警（其余）**：`numpy/core/numeric.py:1-12` 的 `__getattr__`。

#### 4.1.4 代码实践

**实践目标**：动手前先用决策表把 `mymath._core.basic` 的每个名字归类。

**操作步骤**：

1. 假设真实模块 `mymath/_core/basic.py` 里有这些顶层名字：`PI`（常量 `3.14159…`）、`add`（函数）、`Vec`（类，`__reduce__` 引用了模块级 `_reconstruct`）、`_reconstruct`（函数）。
2. 对每个名字套用 4.1.2 的 Q1/Q2/Q3。
3. 填出下表（参考答案见 4.1.5）。

| 名字 | 是否出现在旧 pickle? | 决策 |
|------|----------------------|------|
| `PI` | ? | ? |
| `add` | ? | ? |
| `Vec` | ? | ? |
| `_reconstruct` | ? | ? |

**需要观察的现象**：你会发现「是否被 `__reduce__` / pickle 引用」是分类的唯一关键，和名字是否带下划线、是常量还是函数都无关。

**预期结果**：`_reconstruct` 进 eager，其余进惰性（本场景没有 ABI 符号）。

#### 4.1.5 小练习与答案

**练习 1**：如果你的 `Vec` 类没有自定义 `__reduce__`，而是依赖 Python 默认的实例 pickle（按 `(模块, 类名)` 引用类本身），那么分类表里需要 eager 绑定谁？

**答案**：需要 eager 绑定 `Vec` 本身。因为默认实例 pickle 会把类按 `(module, qualname)` 存进字节流，unpickle 时 `find_class("mymath.core.basic", "Vec")` 必须命中且不报警。本讲为了让 eager 列表更短，才用 `_reconstruct` + 自定义 `__reduce__` 把「唯一被 pickle 引用的名字」收敛成一个函数。

**练习 2**：为什么不能把**所有**名字都 eager 绑定，那样不就最省事了吗？

**答案**：那样就丧失了「报警」能力——eager 绑定的名字进 `__dict__`，访问时根本不会触发 `__getattr__`，也就不会打印 `DeprecationWarning`，用户永远收不到迁移提示。numpy 正是为了「既兼容旧 pickle、又对普通访问报警」才做了这种精细区分。

---

### 4.2 统一弃用警告工具：垫片的地基

#### 4.2.1 概念说明

垫片里会有十几个子模块，每个都要打同一套「弃用」文案。如果每个子模块各写一份 `warnings.warn(...)`，文案迟早会漂移（有的写「deprecated」有的写「removed」、stacklevel 各不相同）。所以 numpy 把这件事收敛到一个**单一信息源** `_raise_warning`，所有垫片都调用它。这是本讲要写的第一个文件 `mymath/core/_utils.py`。

#### 4.2.2 核心流程

`_raise_warning(attr, submodule=None)` 做三件事：

1. 根据 `submodule` 是否为 `None`，拼出旧/新模块全名（`mymath.core` vs `mymath.core.basic`）。
2. 拼一段统一的弃用文案，把旧名、新名、具体属性名都嵌进去。
3. 调 `warnings.warn(文案, DeprecationWarning, stacklevel=3)`。

`stacklevel` 的数法：从 `warnings.warn` 所在帧往上数，「用户代码 → 垫片 `__getattr__` → `_raise_warning` → `warn`」是 4 个帧，`warn` 自己算第 1 层，所以指向用户代码需要 `stacklevel=3`。这是整条链**恒定**的，因此可以硬编码。

#### 4.2.3 源码精读

[numpy/core/_utils.py:4-21](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_utils.py#L4-L21) —— `_raise_warning` 全文。注意三个要点：第 7-9 行用 `submodule` 切换包级/子模块级文案；第 10-21 行的 `warnings.warn` 把类别固定为 `DeprecationWarning`、`stacklevel` 硬编码为 `3`。这是我们要在 `mymath` 版本里照抄的骨架，只把 `numpy._core`/`numpy.core` 换成 `mymath._core`/`mymath.core`。

#### 4.2.4 代码实践

**实践目标**：写出 `mymath/core/_utils.py`。

**操作步骤**：新建文件 `mymath/core/_utils.py`，内容如下（**示例代码**，对照 numpy 改写）：

```python
# 示例代码：仿照 numpy/core/_utils.py 改写
import warnings


def _raise_warning(attr: str, submodule: str | None = None) -> None:
    new_module = "mymath._core"
    old_module = "mymath.core"
    if submodule is not None:
        new_module = f"{new_module}.{submodule}"
        old_module = f"{old_module}.{submodule}"
    warnings.warn(
        f"{old_module} is deprecated and renamed to {new_module}. "
        f"Use the public mymath API, or {new_module}.{attr} for internals.",
        DeprecationWarning,
        stacklevel=3,
    )
```

**需要观察的现象**：单独 import 这个模块不会打任何警告（它只**定义**函数，不调用）。

**预期结果**：函数返回 `None`，是个纯副作用函数。待本地验证：在 REPL 里 `import warnings; warnings.simplefilter("always"); from mymath.core._utils import _raise_warning; _raise_warning("add", "basic")` 应看到一条 `DeprecationWarning`。

#### 4.2.5 小练习与答案

**练习**：如果把 `stacklevel` 改成 `2`，警告的 `filename`/`lineno` 会指向哪里？改成 `4` 呢？

**答案**：`stacklevel=2` 指向调用 `_raise_warning` 的那一帧，也就是垫片的 `__getattr__`（`mymath/core/basic.py` 内部），用户看不到自己的代码；`stacklevel=4` 越过用户帧指向更上层（通常是 REPL 或 `import` 机制）。只有 `3` 才精确归因到用户访问废弃属性的那一行。这正是 numpy 写死 `3` 的原因，也是它「脆弱」之处——调用链多包一层就要同步加 1（见 u2-l3）。

---

### 4.3 包入口：__all__ + __getattr__ 的惰性骨架

#### 4.3.1 概念说明

`mymath/core/__init__.py` 是垫片包的「总机前台」。它要做两件事：声明有哪些子模块（`__all__`），以及对**任何**找不到的名字转发到真包 `mymath._core`（`__getattr__`）。关键技巧是「只声明、不绑定」——`__all__` 里写了子模块名，但顶部**不 import** 它们，从而迫使每次访问都走 `__getattr__`，每次都报警。

#### 4.3.2 核心流程

```text
import mymath.core               # 只执行 __init__.py 顶层 → 极轻，不拉起子模块
mymath.core.basic                # __dict__ 没有 → 触发 __getattr__("basic")
  └─ getattr(mymath._core, "basic")   # 拿到真子模块
  └─ _raise_warning("basic")          # 报警（包级，不带 submodule）
  └─ 返回真子模块
```

注意：包级 `__getattr__` 转发的目标是**整个 `_core`**，所以它既能解析子模块名（`basic`），也能解析顶层类型名（如果真包有顶层类）。缺失的名字由 `getattr` 自己抛 `AttributeError`，不报警——因为根本不是「废弃但存在」。

#### 4.3.3 源码精读

[numpy/core/__init__.py:23-33](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/__init__.py#L23-L33) —— 注释 `# force lazy-loading of submodules to ensure a warning is printed` 一句话点明设计意图；`__all__` 列出 17 个子模块名却一个都不 import；`__getattr__` 只有三行：`getattr(_core, name)` → `_raise_warning(name)` → `return`。第 25 行的 `# noqa: F822` 是为了压制 linter 对「`__all__` 里有名字却找不到定义」的误报，因为名字是运行时由 `__getattr__` 提供的。

#### 4.3.4 代码实践

**实践目标**：写出 `mymath/core/__init__.py`。

**操作步骤**：新建 `mymath/core/__init__.py`（**示例代码**）：

```python
# 示例代码：仿照 numpy/core/__init__.py
"""
The `mymath.core` submodule exists solely for backward compatibility.
The original `core` was renamed to `_core` and made private.
`mymath.core` will be removed in the future.
"""
from mymath import _core

from ._utils import _raise_warning

# 只声明、不绑定，强制每次访问走 __getattr__ 并打印警告
__all__ = ["basic"]  # noqa: F822


def __getattr__(attr_name):
    attr = getattr(_core, attr_name)
    _raise_warning(attr_name)
    return attr
```

**需要观察的现象**：`import mymath.core` 本身**不会**触发任何子模块加载、也不报警；只有访问 `mymath.core.basic` 时才报警并返回真子模块。

**预期结果**：待本地验证。把第 2 单元用过的捕获姿势搬来：

```python
import warnings, mymath.core
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    basic = mymath.core.basic        # 触发 __getattr__
    assert len(w) == 1
    assert issubclass(w[0].category, DeprecationWarning)
    assert "mymath._core" in str(w[0].message)
```

#### 4.3.5 小练习与答案

**练习**：如果你在 `__init__.py` 顶部多写一行 `from . import basic`，会发生什么？

**答案**：`import mymath.core` 时就立刻把 `basic` 子模块拉进了 `mymath.core.__dict__`，于是后续 `mymath.core.basic` 在 `__dict__` 直接命中，**永远不再触发 `__getattr__`、永远不再报警**。这违背了「每次访问都提示迁移」的设计目标，所以 numpy 故意只写 `__all__` 不写 import。

---

### 4.4 子模块垫片：纯转发与 pickle eager 绑定

#### 4.4.1 概念说明

`mymath/core/basic.py` 是垫片里最讲究的一层。它要同时满足两个看似矛盾的要求：

- 普通访问（`mymath.core.basic.add`）→ 转发 + 报警（让用户知道要迁移）。
- 旧 pickle 还原（`find_class("mymath.core.basic", "_reconstruct")`）→ **不报警**（pickle 容忍不了警告，详见 u3-l1）。

解法就是 4.1.2 的决策表：把 `_reconstruct` 在顶部 eager 绑定进 `__dict__`（绕过 `__getattr__`），其余名字走 `__getattr__` 惰性转发。这正是 numpy 区分「纯转发垫片」与「特殊垫片」的根本原因。

#### 4.4.2 核心流程

子模块垫片的骨架（None 写法版）：

```text
# 顶部（执行一次）：
from mymath._core import basic          # 惰性持有真模块引用
_reconstruct = basic._reconstruct        # eager：进 __dict__，pickle 免报警命中

# __getattr__(name)（每次失败访问）：
ret = getattr(basic, name, None)         # 取真属性，缺失给 None
if ret is None:                           # 注意：值为 None 的真属性会误判（见 u2-l2）
    raise AttributeError(...)
_raise_warning(name, "basic")             # 报警
return ret
```

> 选 None 还是 sentinel？若 `basic` 里可能出现「值恰为 `None` 的公开名字」就用 sentinel（`numeric.py` 写法）；否则 None 写法更短（`multiarray.py`/`_internal.py` 写法）。本讲的 `basic` 不含值为 `None` 的名字，为简洁用 None 写法，但 4.4.5 会让你改用 sentinel 对比。

#### 4.4.3 源码精读

**纯转发 + sentinel**：[numpy/core/numeric.py:1-12](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/numeric.py#L1-L12) —— 第 6-8 行用 `sentinel = object()` + `getattr(numeric, name, sentinel)` + `if ret is sentinel`，对任何取值都安全；第 11 行传 `submodule="numeric"`，让警告文案精确到子模块。

**eager 绑定（pickle 兼容）**：[numpy/core/multiarray.py:5-6](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/multiarray.py#L5-L6) —— 顶部循环 `for item in ["_reconstruct", "scalar"]: globals()[item] = getattr(multiarray, item)`，把两个被旧 pickle 硬编码引用的重建函数直接写进 `__dict__`，注释 `# these must import without warning or error from numpy.core.multiarray to support old pickle files` 一语道破动机。同样手法见 [numpy/core/_internal.py:9-11](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_internal.py#L9-L11) 与第 16 行的 `_dtype_from_pep3118` eager 绑定。

**致命路径硬拦（ABI）**：[numpy/core/_multiarray_umath.py:17-46](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_multiarray_umath.py#L17-L46) —— 当 `name` 是 `_ARRAY_API`/`_UFUNC_API` 时，用 `traceback.format_stack()[:-1]` 收集调用栈、跳过 `frozen importlib` 帧，拼成可读上下文后 `raise ImportError`。本讲的 `mymath` 没有 C 扩展，所以**不实现**这一支，但要记住：如果你的库也有二进制 ABI 问题，这是唯一的兜底姿势（详见 u3-l2）。

#### 4.4.4 代码实践

**实践目标**：写出 `mymath/core/basic.py`，并让它同时通过「普通访问报警」与「pickle 还原不报警」。

**操作步骤**：

1. 先写真实现 `mymath/_core/basic.py`（**示例代码**）：

```python
# 示例代码：真实实现（私有包内）
__all__ = ["PI", "add", "Vec"]

PI = 3.141592653589793


def add(a, b):
    return a + b


class Vec:
    def __init__(self, x):
        self.x = x

    def __reduce__(self):
        # 让 pickle 通过模块级 _reconstruct 重建（仿 numpy.ndarray）
        return (_reconstruct, (self.x,))

    def __repr__(self):
        return f"Vec({self.x})"


def _reconstruct(x):
    """被旧 pickle 按名字引用的重建函数，垫片必须免报警地提供它。"""
    return Vec(x)
```

2. 再写垫片 `mymath/core/basic.py`（**示例代码**，对照 `multiarray.py`）：

```python
# 示例代码：子模块垫片（None 写法 + pickle eager）
from mymath._core import basic

# 名字 mymath.core.basic._reconstruct 被硬编码进旧 pickle，
# 必须 import 即命中、不报警、不懒加载。
_reconstruct = basic._reconstruct


def __getattr__(attr_name):
    from mymath.core._utils import _raise_warning

    ret = getattr(basic, attr_name, None)
    if ret is None:
        raise AttributeError(
            f"module 'mymath.core.basic' has no attribute {attr_name}")
    _raise_warning(attr_name, "basic")
    return ret
```

**需要观察的现象**：

- `from mymath.core.basic import add; add(2,3)` → 一条 `DeprecationWarning` + 结果 `5`。
- `mymath.core.basic._reconstruct` → **无警告**（它在 `__dict__` 里，绕过了 `__getattr__`）。

**预期结果**：待本地验证。

```python
import warnings, mymath.core.basic as shim
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    assert shim.add(2, 3) == 5
    assert len(w) == 1 and "mymath._core" in str(w[0].message)

# _reconstruct 在 __dict__ 中 → 不报警
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    _ = shim._reconstruct
    assert len(w) == 0
```

#### 4.4.5 小练习与答案

**练习 1**：把 `mymath/core/basic.py` 的 None 写法改成 sentinel 写法（仿 `numeric.py`），写出关键三行。

**答案**：

```python
sentinel = object()
ret = getattr(basic, attr_name, sentinel)
if ret is sentinel:
    raise AttributeError(...)
```

差别只在用唯一哨兵代替 `None` 当默认值，从而避免「值为 `None` 的真属性被误判为缺失」（见 u2-l2）。

**练习 2**：如果在 `mymath._core.basic` 里新增一个 `nothing = None` 常量，当前 None 写法的垫片访问 `mymath.core.basic.nothing` 会怎样？

**答案**：会错误地 `raise AttributeError`——因为 `getattr(basic, "nothing", None)` 返回 `None`，`if ret is None` 把它当成「缺失」。这正是 None 写法的唯一盲点；改成 sentinel 写法即可正确转发并报警。

---

### 4.5 类型存根：废弃模块的 .pyi 配套

#### 4.5.1 概念说明

垫片的 `.py` 在运行时打警告，但 `.pyi`（类型存根）**运行时不执行**，类型检查器只读不跑，因此存根永远不会触发 `DeprecationWarning`（详见 u3-l3）。这意味着存根和运行时是**解耦**的两套策略：

- 存根的目标是「让静态检查看到正确类型」，不必复刻报警行为。
- 上游真模块有 `__all__` 时，存根用 `from 真模块 import *` 完整再导出最省事。
- 包入口存根走「全量 import 子模块」的相反路线，以保证类型精度。

#### 4.5.2 核心流程

按 u3-l3 的「可验证性原则」选策略：

```text
上游真模块有 __all__ / __dir__?
├─ 是  → 完整再导出：from 真模块 import *
│         并补 from 真模块 import __all__ as __all__
├─ 否，且废弃非公开 → 省略再导出（只留 NOTE 注释）
└─ 私有内部模块     → 近空/空存根（一行注释或零字节）
```

#### 4.5.3 源码精读

**完整再导出**：[numpy/core/numeric.pyi:1-4](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/numeric.pyi#L1-L4) —— 第 1 行注释 `# deprecated module`，第 3 行 `from numpy._core.numeric import *`，第 4 行单独把 `__all__` 拉出来（因为 `__all__` 以字母开头但 `import *` 默认只带公开名，而这里要显式保留 `__all__` 这个特殊名字给类型检查器）。

**包入口全量 import**：[numpy/core/__init__.pyi:5-22](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/__init__.pyi#L5-L22) —— 和运行时「只声明不绑定」相反，存根里把所有子模块一次性 `from . import (...)`，第 44-45 行对没有存根的 `_multiarray_umath` 用 `_multiarray_umath: ModuleType` 兜底（u3-l3 讲过）。

**近空存根**：[numpy/core/_internal.pyi:1](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_internal.pyi#L1) —— 只有一行 `# deprecated module`，因为 `_internal` 是私有内部模块、不值得维护签名。

#### 4.5.4 代码实践

**实践目标**：给 `mymath.core` 配 `basic.pyi` 和 `__init__.pyi`。

**操作步骤**：

1. `mymath/core/basic.pyi`（**示例代码**，完整再导出，因 `_core/basic.py` 有 `__all__`）：

```python
# deprecated module

from mymath._core.basic import *
from mymath._core.basic import __all__ as __all__
```

2. `mymath/core/__init__.pyi`（**示例代码**，包入口全量 import）：

```python
# deprecated module

from . import basic

__all__ = ["basic"]
```

**需要观察的现象**：把第 1 行的 `# deprecated module` 注释删掉不影响类型检查结果——再次印证存根和报警无关。但保留这行注释是对维护者的提示，numpy 也这么做。

**预期结果**：待本地验证。用 mypy（或 pyright）检查一段调用：

```python
from mymath.core.basic import add, Vec   # 静态检查应通过，类型为 (int, int) -> int / 类
```

#### 4.5.5 小练习与答案

**练习**：如果你的真模块 `_core/basic.py` **没有** `__all__`，`basic.pyi` 的 `import *` 还可靠吗？应该怎么办？

**答案**：不太可靠。没有 `__all__` 时 `import *` 带入的是「所有不以下划线开头的名字」，规则隐晦、容易漏（比如会带入 import 进来的其它模块名）。更稳妥的做法是显式列出再导出名：`from mymath._core.basic import PI, add, Vec`，或者干脆给真模块补一个 `__all__`。这正是 u3-l3 强调「可验证性原则」、并对没有 `__all__` 的 `overrides` 选择「省略再导出」的原因。

---

## 5. 综合实践：搭建完整的 mymath.core 兼容垫片

本任务把 4.2~4.5 的产物组装成一个完整包，并写一个验收脚本同时证明三件事。这是整本手册的毕业作业。

### 5.1 目录与文件

按 4.1.2 的布局创建：

```text
mymath/
├── __init__.py            # 可空，或写 __version__
├── _core/
│   ├── __init__.py        # 可空
│   └── basic.py           # 见 4.4.4 步骤 1
└── core/
    ├── __init__.py        # 见 4.3.4
    ├── _utils.py          # 见 4.2.4
    ├── basic.py           # 见 4.4.4 步骤 2
    ├── basic.pyi          # 见 4.5.4
    └── __init__.pyi       # 见 4.5.4
```

### 5.2 验收脚本（一次验证三件事）

把下面这段保存为 `verify_shim.py`（**示例代码**，**不要假装它已经跑过**——你需要自己执行并核对输出）：

```python
# 示例代码：垫片验收脚本
import pickle
import warnings

# ---- 事项 1：访问触发弃用警告，但功能正常 ----
import mymath.core.basic as shim

with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    assert shim.add(2, 3) == 5
    assert len(w) == 1, f"应恰好 1 条警告，实际 {len(w)}"
    assert issubclass(w[0].category, DeprecationWarning)
    assert "mymath._core" in str(w[0].message)
print("[1] OK: 访问触发 DeprecationWarning 且功能正常")

# ---- 事项 2：旧 pickle 可还原（且还原时不报警）----
import mymath._core.basic as real
v = real.Vec(42)                       # 用真模块构造一个对象
data = pickle.dumps(v)
# 模拟「改名前」的旧 pickle：把字节流里的新路径改回旧路径
old_data = data.replace(b"mymath._core.basic", b"mymath.core.basic")

with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    restored = pickle.loads(old_data)  # 走垫片的 _reconstruct（eager，免报警）
    assert restored.x == 42
    assert type(restored) is real.Vec
    assert len(w) == 0, f"pickle 还原不应报警，实际 {len(w)} 条"
print("[2] OK: 旧 pickle 还原成功且未触发警告")

# ---- 事项 3：类型检查通过（交给 mypy/pyright，见说明）----
print("[3] 类型检查请另存一个 .py 后用 mypy 或 pyright 运行验证")
```

### 5.3 三个验收点对应的设计原理

| 验收点 | 靠哪个机制保证 | 对应 numpy 源码 |
|--------|----------------|----------------|
| 访问报警但功能正常 | 模块级 `__getattr__` + `_raise_warning`（4.2/4.3/4.4） | `numeric.py` 的惰性转发 |
| 旧 pickle 还原、不报警 | `_reconstruct` 的 eager 绑定进 `__dict__`（4.4） | `multiarray.py:5-6` |
| 类型检查通过 | 完整再导出的 `basic.pyi` + 全量 import 的 `__init__.pyi`（4.5） | `numeric.pyi` / `__init__.pyi` |

事项 3 的命令行做法（**待本地验证**）：

```bash
mypy --strict mymath/core/basic.pyi verify_shim.py
# 或
pyright verify_shim.py
```

预期：对 `shim.add(2, 3)` 不报「unknown」、对 `from mymath.core.basic import add, Vec` 不报缺失。如果真模块签名变了，记得同步更新 `.pyi`——存根不会自动跟随运行时（这也是 u3-l3 的取舍）。

### 5.4 何时可以安全移除这个垫片

学习目标里要求「能评估一个垫片何时可以被安全移除」。把上面三件事倒过来看，就是移除的判定清单：

1. **弃用警告层面**：在你的发布说明里持续打了几个大版本（如 numpy 对 `numpy.core` 的处理），且抓取到的下游 issue 里不再有「正在迁移」的反馈。可以先用 `warnings.simplefilter("error", DeprecationWarning)` 在自己的测试里跑一轮，看还有多少内部代码仍在用旧路径。
2. **pickle 层面（最关键）**：旧 pickle 的最长存活期往往以「年」计。移除 `_reconstruct` 这类 eager 绑定前，必须确认「现实世界里还有人需要读这些旧文件吗？」——numpy 至今仍保留 `_ufunc_reconstruct`，就是因为它无法判断是否还有 1.20 前的 pickle 残留（见 [numpy/core/__init__.py:11-13](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/__init__.py#L11-L13) 的注释）。一个实用做法是：先在某一版把 `DeprecationWarning` 升级为 `FutureWarning`（默认对用户可见），观察一个完整 LTS 周期后再删。
3. **ABI 层面（若有）**：只要还可能有用旧版头文件编译的二进制扩展在野外运行，`_ARRAY_API` 的 ImportError 守卫就不能拆——它拦的是段错误，不是兼容性便利。
4. **类型存根层面**：`.pyi` 可以比 `.py` 更早或更晚移除，因为它不执行；但如果 `.py` 删了而下游还 import 它，类型检查会从「通过」变成「找不到模块」，所以要和 `.py` 同步下线。

一句话：**垫片能移除的时机 = (弃用提示已充分) ∧ (旧 pickle/ABI 的最长存活期已过) ∧ (类型存根同步下线)**。

---

## 6. 本讲小结

- 兼容垫片的本质是「**对每个旧名字分类决策**」：惰性报警、eager 放行、ImportError 硬拦三选一，决策依据是「是否出现在旧 pickle」与「是否致命」。
- `_raise_warning` 是所有垫片共享的**单一信息源**，固定 `stacklevel=3` 对应「用户 → `__getattr__` → `_raise_warning` → `warn`」这条恒定调用链。
- 包入口靠「只声明、不绑定」的 `__all__` + 包级 `__getattr__` 实现**惰性子模块加载**，确保每次访问都报警。
- 子模块垫片用 `__getattr__` 做纯转发（sentinel 或 None 写法），并把被旧 pickle 引用的重建函数**顶部 eager 绑定**进 `__dict__`，让 pickle 还原绕过 `__getattr__`、不报警。
- 类型存根 `.pyi` 与运行时解耦：完整再导出（上游有 `__all__`）、省略再导出（不可验证）、近空（私有模块）三种策略，按可验证性原则选择。
- 垫片能安全移除的时机，由「弃用提示是否充分」「旧 pickle/ABI 存活期是否已过」「存根是否同步下线」三者共同决定。

## 7. 下一步学习建议

本讲是手册收尾，整本手册已覆盖 `numpy.core` 兼容垫片的全部机制。继续深入的方向：

1. **横向对比真实的迁移案例**：去读 `numpy/_core/__init__.py` 和 `numpy/__init__.py`，看「被转发方」是如何组织公开 API 的，理解垫片提示语里「use the public NumPy API」的真正落脚点。
2. **把 pickle 兼容吃得更透**：在 Python 标准库里读 `pickle` 的 `find_class` 与 `copyreg.__newobj__`，自己造一个「类被改名」的旧 pickle，练习只改字节流还原对象。
3. **关注 ABI 演进**：阅读 NumPy 2.0 release notes 中关于 C-API 的章节，理解 `_ARRAY_API` 这类导出符号表（`PyCapsule`）的版本化机制，体会 u3-l2 里 ImportError 守卫为何不可省。
4. **回到你自己的项目**：挑一个你维护的、即将做重大改名的库，按本讲的决策表与验收脚本，亲手为它搭一个垫片，并在若干个版本周期后实践「安全移除」的判定。
