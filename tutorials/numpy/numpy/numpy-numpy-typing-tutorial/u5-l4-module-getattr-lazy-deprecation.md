# 模块级 `__getattr__` 与延迟弃用

> 本讲对应大纲 `u5-l4`，学习阶段：advanced。前置讲义：`u1-l2`（公共壳与私有实现）、`u4-l1`（NBitBase 精度层次及其 2.3 弃用）。

## 1. 本讲目标

学完本讲，你应该能够：

- 说清 **PEP 562** 给模块增加的 `__getattr__` / `__dir__` 语义，尤其是「`__getattr__` 是兜底（fallback）」这一关键规则。
- 逐行读懂 `numpy/typing/__init__.py` 里的 `__DIR` / `__DIR_SET` / `__dir__` / `__getattr__` 四段代码。
- 解释 NBitBase 的「延迟弃用」模式，并**判断在当前版本里运行时 `DeprecationWarning` 是否真的会触发**（这是本讲最关键的细节）。
- 区分两套弃用机制：「静态 `@deprecated`」（桩文件）与「运行时 `DeprecationWarning`」（`__getattr__`）。
- 用 `warnings.catch_warnings` 捕获 `DeprecationWarning`，并自己构造一个**真正会触发**警告的最小懒加载模块。

## 2. 前置知识

- **模块也是对象**：`import numpy.typing as npt` 拿到的 `npt` 是一个 module 对象，它有自己的 `__dict__`（模块全局命名空间）。
- **属性访问顺序**：访问 `npt.X` 时，解释器先在 `npt.__dict__` 里找 `X`，找到就直接返回；找不到才会考虑别的途径。本讲的全部细节都建立在这条规则之上。
- **`DeprecationWarning`**：Python 标准库里专门表示「这个功能将来会被移除」的警告类别。默认只对开发者可见（不在最终用户的应用里抛出）。
- **`stacklevel`**：`warnings.warn(..., stacklevel=N)` 的参数，决定警告「指向」调用栈的第几层，好让用户看到的是**他自己写的那一行**，而不是库内部的那一行。
- 承接 `u1-l2`：`numpy.typing` 是一层极薄的公共壳，靠 `from numpy._typing import ...` 把名字搬进来；连真实存在的 `test` 都被 `__dir__` 藏起来。
- 承接 `u4-l1`：`NBitBase` 自 NumPy 2.3（2025-05-01）起被弃用，官方改推 `@typing.overload` 或以标量类为上界的 `TypeVar`。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `numpy/typing/__init__.py` | 本讲主角。`__DIR` / `__DIR_SET` / `__dir__` / `__getattr__` 四段都定义在这里，是公共壳里唯一的「动态逻辑」。 |
| `numpy/_typing/_nbit_base.py` | NBitBase 的**运行时**定义。注意它只有 `@final` / `@set_module`，**没有**运行时弃用装饰器。 |
| `numpy/_typing/_nbit_base.pyi` | NBitBase 的**桩文件**。真正的静态 `@deprecated` 装饰器在这里。 |
| `numpy/typing/tests/test_runtime.py` | 运行时测试。NBitBase 这一条带有 `# type: ignore[deprecated]`，是判断「弃用信号来自静态还是运行时」的关键线索。 |
| `numpy/_typing/__init__.py` | 私有聚合层，再导出 `NBitBase`。 |

## 4. 核心概念与源码讲解

### 4.1 PEP 562：让模块拥有 `__getattr__` / `__dir__`

#### 4.1.1 概念说明

类早就支持 `__getattr__`（属性找不到时兜底）和 `__dir__`（控制 `dir()` 输出），但**模块**长期以来不支持。PEP 562（Python 3.7 起）补上了这个缺口：只要在模块顶层定义了名为 `__getattr__` 和 `__dir__` 的函数，解释器就会对它们「特殊对待」。

最常见的两个用途：

1. **懒加载（lazy import）**：访问某个名字时才真正去 `import`，加快启动速度、避免循环导入。
2. **延迟弃用（lazy deprecation）**：访问某个名字时才发出 `DeprecationWarning`，把「迁移提醒」精确推到「真正用到」的那一刻。

最关键的一条规则，和实例 `__getattr__` 完全一致：

> 模块 `__getattr__` 是**兜底**。只有当属性在模块 `__dict__` 里**找不到**时，解释器才会调用它。

这条规则决定了后面 NBitBase 的运行时警告在当前版本到底会不会触发——请先把它记住。

#### 4.1.2 核心流程

访问 `npt.NBitBase` 时，CPython 内部（`Objects/moduleobject.c` 的 `module_getattro`）大致按下面伪代码走：

```text
module_getattro(npt, "NBitBase"):
    attr = npt.__dict__.get("NBitBase")
    if attr 存在:                       # ① 命中字典 → 直接返回
        return attr                     #    __getattr__ 完全不参与！
    getter = npt.__dict__.get("__getattr__")
    if getter 存在:                     # ② 字典没有，才回头看 __getattr__
        return getter("NBitBase")       #    只有这里才会触发警告
    raise AttributeError
```

一句话：**只要名字在 `__dict__` 里，`__getattr__` 永远不会被调用。**

#### 4.1.3 源码精读

`numpy/typing/__init__.py` 确实在顶层定义了这两个函数，这就是 PEP 562 的入口：

- [numpy/typing/\_\_init\_\_.py:184-185](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L184-L185)：定义模块级 `__dir__()`，直接返回预计算好的 `__DIR`（下一个小节讲它怎么算）。
- [numpy/typing/\_\_init\_\_.py:187-204](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L187-L204)：定义模块级 `__getattr__(name)`，处理 NBitBase 的弃用警告与白名单兜底。

注意它们只是「被定义」在这里，会不会被调用，完全取决于被访问的名字是否已经在 `__dict__` 里（见 4.3）。

#### 4.1.4 代码实践

**目标**：亲眼验证「`__getattr__` 是兜底」这条规则。

**操作步骤**（示例代码，请另存为 `_probe_fallback.py` 自行实验）：

```python
# 示例代码：一个最小模块，演示 __getattr__ 只有在字典找不到时才被调用
import warnings

x = 42  # 这个名字会被「提前」放进模块 __dict__

def __getattr__(name):
    if name == "x":
        warnings.warn("访问了 x", DeprecationWarning, stacklevel=2)
        return x
    raise AttributeError(f"module has no attribute {name!r}")
```

```python
# 示例代码：测试脚本
import warnings, importlib.util, pathlib
spec = importlib.util.spec_from_file_location("m", pathlib.Path("_probe_fallback.py"))
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)

with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    print(m.x)          # 预期 42
print("警告数（x 在字典里）:", len(w))   # 预期 0
```

**需要观察的现象**：`m.x` 能正常拿到 `42`，但 `len(w)` 是 `0`——因为 `x` 已经在 `__dict__` 里，`__getattr__` 根本没被调用，警告也就没发出来。

**预期结果**：警告数为 `0`。

> 如果你想让上面的警告真的发出来，把模块里 `x = 42` 这一行删掉，改成在 `__getattr__` 内部 `return 42`——这时 `x` 不在字典里，访问就会触发兜底，`len(w)` 变成 `1`。这正是「懒加载弃用」能成立的前提。

#### 4.1.5 小练习与答案

**练习 1**：如果一个模块里既有 `import os`（于是 `os` 进入 `__dict__`），又定义了处理 `"os"` 的 `__getattr__`，访问 `mod.os` 会触发 `__getattr__` 吗？

**答案**：不会。`os` 已经在 `__dict__` 里，按规则 ① 直接返回，`__getattr__` 被跳过。

**练习 2**：为什么说模块 `__getattr__` 天然适合做「延迟弃用」而不是「即时弃用」？

**答案**：因为它只在「真正访问该名字」时才被调用，可以把警告精确推到使用点；而且只要名字还在 `__dict__` 里它就不触发，所以「是否真的警告」完全由「这个名字有没有被提前绑定」决定——库作者可以靠这一点平滑切换弃用阶段。

---

### 4.2 `__dir__` 与 `__DIR_SET`：`dir()` 的冻结白名单

#### 4.2.1 概念说明

`dir(模块)` 默认会列出模块 `__dict__` 里的**所有**名字——包括内部辅助函数、`test`、`PytestTester` 等用户不该关心的东西。PEP 562 的模块 `__dir__` 让库可以**收窄**这个列表，对外只暴露「承诺稳定」的名字。

`numpy.typing` 的做法很巧妙：它不是在每次 `dir()` 时去扫描全局变量，而是在模块加载的**某个特定时刻**把「白名单」**冻结**成一个列表 `__DIR`，之后 `dir()` 永远返回这个冻结值。选择「哪个时刻」至关重要——它决定了哪些名字会被藏起来。

#### 4.2.2 核心流程

模块加载时（按文件从上到下）：

```text
第 175 行: from numpy._typing import ArrayLike, DTypeLike, NBitBase, NDArray
          → 这 4 个名字进入 __dict__
第 177 行: __all__ = [这 4 个]
第 180 行: __DIR = __all__ + [此刻 globals() 里的所有 dunder]   ← 冻结点！
第 181 行: __DIR_SET = frozenset(__DIR)
第 184 行: def __dir__(): return __DIR                          ← 之后永远返回冻结值
第 215 行: test = PytestTester(__name__)                        ← 冻结点之后才绑定
```

关键在于第 180 行这个**冻结点**：此时 `__dir__`、`__getattr__`、`__DIR_SET`、`test` 都**还没定义**，所以它们统统不在 `__DIR` 里，也就不会出现在 `dir(npt)` 中。而 `test` 在第 215 行才绑定——但它仍然进入了 `__dict__`，所以 `npt.test` 仍可访问，只是 `dir()` 不列出它。

于是：

\[
\texttt{dir(npt)} = \texttt{\_\_DIR} = \texttt{\_\_all\_\_} \;\cup\; \bigl\{\,\text{标准 dunder}\,\bigr\}_{\text{冻结于第 180 行}}
\]

#### 4.2.3 源码精读

- [numpy/typing/\_\_init\_\_.py:175](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L175)：把四个公共名字提前导入，**使它们进入 `__dict__`**——这一点后面会反复用到。
- [numpy/typing/\_\_init\_\_.py:180-181](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L180-L181)：`__all__` 加上「此刻已存在的 dunder」冻结成 `__DIR`，再转成 `__DIR_SET`。注意这一刻 `test` / `__dir__` / `__getattr__` 都还没诞生。
- [numpy/typing/\_\_init\_\_.py:184-185](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L184-L185)：`__dir__` 不做任何动态计算，直接吐回冻结好的 `__DIR`。
- [numpy/typing/\_\_init\_\_.py:215](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L215)：`test = PytestTester(__name__)` 在冻结点**之后**才绑定，因此 `dir(npt)` 看不到它，但 `npt.test()` 仍可调用。

#### 4.2.4 代码实践

**目标**：亲手验证 `dir(npt)` 是一个被收窄的冻结白名单，而 `npt.test` 虽不在列表里却仍可访问。

**操作步骤**：

```python
import numpy.typing as npt

public = dir(npt)
print("NBitBase 在 dir 里:", "NBitBase" in public)   # 预期 True
print("ArrayLike 在 dir 里:", "ArrayLike" in public) # 预期 True
print("test 在 dir 里:", "test" in public)           # 预期 False（被藏起）
print("__getattr__ 在 dir 里:", "__getattr__" in public)  # 预期 False

# 但 test 仍可访问（它在 __dict__ 里）
print("npt.test 存在:", hasattr(npt, "test"))        # 预期 True
print("test 在 npt.__dict__ 里:", "test" in npt.__dict__)  # 预期 True
```

**需要观察的现象**：`dir(npt)` 里没有 `test` 和 `__getattr__`，但 `npt.test` 能正常拿到对象。

**预期结果**：`test` 不在 `dir()` 输出，却在 `__dict__` 里——`dir()` 被收窄、属性访问没有被收窄。

> 待本地验证：不同 NumPy 版本里 `dir(npt)` 的 dunder 集合可能略有差异，但 `test` / `__getattr__` / `__DIR_SET` 不在其中这一点是稳定的。

#### 4.2.5 小练习与答案

**练习 1**：如果把第 180 行移到第 215 行之后（即先定义 `test` 再算 `__DIR`），`dir(npt)` 会发生什么变化？

**答案**：`test` 会出现在 `dir(npt)` 里，因为冻结时它已经进入 `globals()`。这反过来说明「冻结点选得早」是藏起 `test` 的关键。

**练习 2**：`__DIR_SET` 在 `__getattr__` 里被用到（4.3）。既然它的成员都已经在 `__dict__` 里，这个用法是不是「死代码」？

**答案**：在当前版本基本是「死分支」——因为白名单里的名字都能在 `__dict__` 直接命中，`__getattr__` 根本到不了 `__DIR_SET` 那一行。它的意义更偏向「声明意图」与「为将来名字不再提前绑定时的兜底」。

---

### 4.3 `__getattr__` 与 NBitBase 的延迟弃用模式（含「当前为何不触发」的关键细节）

#### 4.3.1 概念说明

`__getattr__` 里写了一段针对 NBitBase 的「延迟弃用」逻辑：一旦访问 `NBitBase`，就发一条 `DeprecationWarning`，再把 NBitBase 返回。这是教科书式的「懒加载弃用」写法。

**但是**——这里有一个必须讲透的细节：因为第 175 行已经把 NBitBase **提前**导入进了 `__dict__`，访问 `npt.NBitBase` 会**命中字典**（规则 ①），于是 `__getattr__` **根本不会被调用**，这条运行时 `DeprecationWarning` 在当前版本里是**休眠的**。

换句话说：

- 这段 `__getattr__` 代码描述的是「延迟弃用」的**模式与意图**；
- 它**真正会触发**的前提是：NBitBase 不再被提前绑定，而是改由 `__getattr__` 按需返回（即真正的懒加载）。
- 当前 NBitBase 真正生效的弃用信号其实来自**静态**装饰器（见 4.4），而不是这条运行时警告。

理解这一点，才算真正读懂了这段代码，而不是被「有警告代码就等于会报警」的表象误导。

#### 4.3.2 核心流程

`__getattr__(name)` 被调用（仅当 `name` 不在 `__dict__`）时的分支：

```text
if name == "NBitBase":
    warnings.warn(DeprecationWarning, stacklevel=2)   # 指向调用方那一行
    return NBitBase                                    # 返回提前导入的那个类
if name in __DIR_SET:                                  # 白名单兜底
    return globals()[name]
raise AttributeError(...)                              # 既不在字典、也不在白名单 → 报错
```

- `stacklevel=2`：让警告指向「写 `npt.NBitBase` 的那一行」（调用栈往上一层），而不是 `__getattr__` 内部，这样用户一眼能看到该改哪里。
- `return NBitBase`：这里的 `NBitBase` 是模块顶层（第 175 行）绑定的那个全局变量——即使走 `__getattr__` 这条路，返回的仍是同一个类对象。

#### 4.3.3 源码精读

- [numpy/typing/\_\_init\_\_.py:175](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L175)：NBitBase 在此处被**提前导入** → 进入 `__dict__`。这是运行时警告休眠的根因。
- [numpy/typing/\_\_init\_\_.py:187-199](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L187-L199)：`__getattr__` 的 NBitBase 分支——发 `DeprecationWarning` 并 `return NBitBase`。**在当前版本里，因为 NBitBase 已在 `__dict__`，这一分支不会被触发。**
- [numpy/typing/\_\_init\_\_.py:191](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L191)：注释 `# Deprecated in NumPy 2.3, 2025-05-01`，标记弃用时间点。
- [numpy/typing/\_\_init\_\_.py:201-204](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L201-L204)：白名单兜底与 `AttributeError`。
- [numpy/\_typing/\_nbit\_base.py:7-10](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_nbit_base.py#L7-L10)：运行时 NBitBase 类只有 `@final` / `@set_module("numpy.typing")`，**没有**运行时弃用装饰器——所以光访问这个类本身不会报警。

#### 4.3.4 代码实践

**目标**：(1) 用 `warnings.catch_warnings` 观察访问 `npt.NBitBase` 是否真的报警；(2) 构造一个**真正会触发**的懒加载模块，对应实践任务。

**操作步骤一**（观察真实模块，预期为 0）：

```python
import warnings, numpy.typing as npt

with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    x = npt.NBitBase
print("访问 npt.NBitBase 的警告数:", len(w))
print("NBitBase 在 npt.__dict__ 里:", "NBitBase" in npt.__dict__)
```

**预期结果**：根据本讲分析，`len(w)` 应为 `0`，因为 NBitBase 已在 `npt.__dict__` 里，`__getattr__` 被绕过。请本地运行确认；若你使用的版本已把 NBitBase 从第 175 行的导入里移除（改成懒加载），则会变成 `1`。

**操作步骤二**（构造一个稳定触发警告的最小模块）。新建 `_lazy_deprecated.py`（示例代码）：

```python
# 示例代码：真正的「懒加载弃用」——NBitBase 不提前导入，只在 __getattr__ 里按需返回
import warnings

def __getattr__(name):
    if name == "NBitBase":
        warnings.warn(
            "`NBitBase` is deprecated ... (deprecated in NumPy 2.3)",
            DeprecationWarning,
            stacklevel=2,
        )
        from numpy._typing import NBitBase   # 真正的懒加载：用到才 import
        globals()["NBitBase"] = NBitBase     # 可选：缓存进字典，避免重复警告
        return NBitBase
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
```

```python
# 示例代码：此时访问会稳定触发警告
import warnings, importlib.util, pathlib
spec = importlib.util.spec_from_file_location("ld", pathlib.Path("_lazy_deprecated.py"))
ld = importlib.util.module_from_spec(spec); spec.loader.exec_module(ld)

with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    x = ld.NBitBase
print("懒加载模块访问 NBitBase 的警告数:", len(w))   # 预期 1
for wi in w:
    print(" ", wi.category.__name__)
```

**需要观察的现象**：步骤一警告数为 `0`（名字在字典里），步骤二警告数为 `1` 且类别是 `DeprecationWarning`（名字不在字典里，走兜底）。两者对比，正是「提前绑定 vs 懒加载」的区别。

**预期结果**：步骤二稳定捕获到 `DeprecationWarning`。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `numpy/typing/__init__.py` 第 175 行改成 `from numpy._typing import ArrayLike, DTypeLike, NDArray`（删掉 NBitBase），其它代码不动，访问 `npt.NBitBase` 会发生什么？

**答案**：NBitBase 不再在 `__dict__` 里，访问会走到 `__getattr__`，发出 `DeprecationWarning`，然后返回第 175 行外……注意此时模块顶层已没有全局 `NBitBase` 名字，`return NBitBase` 会抛 `NameError`——所以「真正懒加载」还需要在 `__getattr__` 内部去 `import`（就像步骤二的示例那样）。这说明 numpy 当前的写法是「过渡形态」：保留提前导入以保证可用，同时铺好 `__getattr__` 这条将来的迁移路径。

**练习 2**：为什么 `warnings.warn` 要传 `stacklevel=2`？传 `1` 或不传会怎样？

**答案**：`stacklevel=1`（默认）指向 `warn()` 自己所在的那一行，即 `numpy/typing/__init__.py` 内部，对用户毫无帮助；`stacklevel=2` 往上一层，指向用户写 `npt.NBitBase` 的那一行，让迁移提醒落在正确的位置。

---

### 4.4 两套弃用的分工：静态 `@deprecated` vs 运行时 `__getattr__`

#### 4.4.1 概念说明

既然 4.3 说当前运行时警告是休眠的，那 NBitBase 的弃用信号到底从哪来？答案是**静态检查**。

NumPy 同时维护了两条互相独立的「弃用通道」：

| 通道 | 位置 | 谁会看到 | 当前是否生效 |
| --- | --- | --- | --- |
| 静态 `@deprecated` | 桩文件 `_nbit_base.pyi`（`typing_extensions.deprecated`） | 运行 mypy / pyright 的开发者 | **生效** |
| 运行时 `DeprecationWarning` | `numpy/typing/__init__.py` 的 `__getattr__` | 运行程序的任何人（默认仅开发者可见） | **休眠**（因提前导入） |

`typing_extensions.deprecated`（即将进入标准库的 PEP 702）是一个**装饰器**：贴在类/函数上后，mypy、pyright 等静态检查器会在任何「使用」该符号的地方报 `deprecated` 诊断。它**不影响运行时**——运行时 NBitBase 仍是个普通类。

#### 4.4.2 核心流程

- **类型检查时**：检查器读 `_nbit_base.pyi`，看到 `@deprecated` → 凡是引用 NBitBase 的地方都报弃用 → 开发者用 `# type: ignore[deprecated]`（mypy）/ `# pyright: ignore[reportDeprecated]` 显式承认并压制。
- **运行时**：CPython 执行 `_nbit_base.py`，NBitBase 是普通类；访问 `npt.NBitBase` 命中 `__dict__`，无警告。

两条通道各自独立，互不依赖。这解释了一个看似矛盾的现象：测试代码里访问 `npt.NBitBase` 需要 `# type: ignore[deprecated]`（静态报警），却完全不需要 `pytest.warns(DeprecationWarning)`（运行时根本没报警）。

#### 4.4.3 源码精读

- [numpy/\_typing/\_nbit\_base.pyi:9-15](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_nbit_base.pyi#L9-L15)：桩文件里 NBitBase 带 `@deprecated(...)`（来自 `typing_extensions`）——**这是当前真正生效的弃用来源**。
- [numpy/typing/tests/test\_runtime.py:36](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/tests/test_runtime.py#L36)：测试里访问 `npt.NBitBase` 需要同时压两条静态诊断 `# type: ignore[deprecated]` + `# pyright: ignore[reportDeprecated]`，且**没有** `pytest.warns`——佐证弃用信号来自静态、而非运行时。
- [numpy/\_typing/\_\_init\_\_.py:110-112](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/__init__.py#L110-L112)：私有聚合层再导出 NBitBase 时也带 `# type: ignore[deprecated]`，再次印证静态弃用的传递性。

#### 4.4.4 代码实践

**目标**：亲手感受「静态 `@deprecated` 报警，运行时不报警」的分工。

**操作步骤**（源码阅读型 + 可选运行）：

1. 阅读 [numpy/\_typing/\_nbit\_base.pyi:9-15](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_typing/_nbit_base.pyi#L9-L15)，确认装饰器来自 `typing_extensions.deprecated`。
2. 写一个最小脚本（示例代码）：

   ```python
   # 示例代码：静态会被标记为弃用，运行时无警告
   import warnings, numpy.typing as npt
   with warnings.catch_warnings(record=True) as w:
       warnings.simplefilter("always")
       T = npt.NBitBase        # 静态检查器会在此处报 deprecated
   print("运行时警告数:", len(w))   # 预期 0
   ```

3. 若本地装了 mypy 或 pyright，对该脚本跑一次静态检查，观察是否在 `npt.NBitBase` 处报 `deprecated`（与第 2 步运行时 `0` 形成对比）。

**需要观察的现象**：运行时 `len(w)` 为 `0`；静态检查器（若有）在该行报弃用。

**预期结果**：运行时不报警、静态报警——这正是「两套机制分工」的直观体现。若本地未装类型检查器，静态部分标注为「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `test_runtime.py` 里访问 `npt.NBitBase` 不需要 `pytest.warns(DeprecationWarning)`？

**答案**：因为运行时这条警告是休眠的（NBitBase 在 `__dict__` 里，`__getattr__` 不触发），运行时根本不会有警告可捕获；需要的只是压制静态 `deprecated` 诊断。

**练习 2**：如果未来 NumPy 想让运行时也报警，最小改动是什么？

**答案**：把 NBitBase 从第 175 行的导入里去掉，改在 `__getattr__` 内部按需 `import` 并 `warn`（4.3 的示例代码即是）。这样访问才会走到兜底、发出运行时 `DeprecationWarning`。

---

## 5. 综合实践

**任务**：给一个假想的小型库里某个「即将移除」的函数 `old_compute`，设计一套**双通道弃用**方案，并验证两种警告各在何时出现。

要求：

1. 写一个模块 `legacy.py`（示例代码），其中：
   - 用 `typing_extensions.deprecated`（或注释说明）给 `old_compute` 贴上静态弃用标记；
   - 用模块级 `__getattr__` 实现「访问 `old_compute` 时发 `DeprecationWarning`」的**懒加载**版本——即 `old_compute` **不要**提前定义在模块顶层，只在 `__getattr__` 里按需返回。
2. 写测试脚本，分别验证：
   - `dir(legacy)` 是否列出了你想对外暴露的白名单（用 `__dir__` + 冻结 `__DIR` 收窄）；
   - 访问 `legacy.old_compute` 时 `warnings.catch_warnings` 能稳定捕获到 `DeprecationWarning`（数量为 `1`）；
   - 访问一个「未在白名单、也未提供」的名字时抛 `AttributeError`。

**自检要点**：

- 你的 `__getattr__` 是否真的被调用了？（关键看 `old_compute` 有没有提前进入 `__dict__`。）
- `stacklevel` 是否指向调用方那一行？
- 你的 `dir()` 白名单是否在「正确的冻结点」计算（早于其它内部名字的定义）？

> 这是把本讲四块（PEP 562 兜底规则、`__dir__` 白名单、`__getattr__` 懒加载弃用、静态 vs 运行时分工）串起来的综合练习。完成后，你应能解释：为什么 numpy 当前用「静态生效 + 运行时休眠」的过渡形态，以及怎样把它推进到「运行时也报警」。

## 6. 本讲小结

- PEP 562 让模块也能定义 `__getattr__` / `__dir__`；其中 `__getattr__` 是**兜底**，只有名字不在模块 `__dict__` 时才会被调用——这是本讲的总钥匙。
- `numpy.typing` 用 `__dir__` + 在第 180 行冻结的 `__DIR` / `__DIR_SET` 收窄 `dir()`，把 `test`、`__getattr__` 等内部名字藏起来，但它们仍可通过属性访问拿到（因为在 `__dict__`）。
- `__getattr__` 里写了 NBitBase 的「延迟弃用」**模式**，但因为第 175 行已把 NBitBase 提前导入进 `__dict__`，这条运行时 `DeprecationWarning` 在当前版本是**休眠的**。
- NBitBase 当前真正生效的弃用信号来自**静态**：桩文件 `_nbit_base.pyi` 上的 `typing_extensions.deprecated`，由 mypy / pyright 报出（故测试里到处是 `# type: ignore[deprecated]`）。
- 「静态 `@deprecated`」与「运行时 `__getattr__` 警告」是两条**独立**通道，前者改类型检查、后者改运行时；把它们分清，才不会误以为「有警告代码就一定报警」。
- 把 NBitBase 从提前导入里移除、改由 `__getattr__` 按需 `import` 并 `warn`，即可让运行时弃用真正生效——这是理解整段代码演进方向的落脚点。

## 7. 下一步学习建议

- **`u6-l1`（静态类型测试方法论）**：本讲提到弃用信号靠 mypy / pyright 报出，下一单元会系统讲 NumPy 怎么用 mypy 的 `pass` / `fail` / `reveal` / `misc` 四类 fixture 把「期望的类型检查行为」固化为测试，`# type: ignore[deprecated]` 正是其中会被校验的对象。
- **`u6-l2`（运行时类型测试与打包完整性测试）**：承接本讲的 `test_runtime.py`，看 `get_args` / `get_origin` / `get_type_hints` 如何在运行时内省 PEP 695 别名，以及 `__all__` 与 `TYPES` 如何保持同步。
- **延伸阅读源码**：可对照 `numpy/__init__.py` 是否也用了模块级 `__getattr__` 做懒加载/弃用，比较大型包对同一机制的不同用法；以及阅读 PEP 562、PEP 702（`typing.deprecated`）的原文，把本讲的机制放进 Python 演进的全局里理解。
