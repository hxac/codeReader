# 模块级 `__getattr__`（PEP 562）与惰性转发

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 PEP 562 给模块带来的 `__getattr__` 能力，以及它**何时被调用**。
- 理解 `numpy.core` 为什么把真正的 `import` 藏进 `__getattr__` 函数体里，从而实现「按属性」的**惰性转发**，并在 `import numpy.core` 时**不一次性加载所有子模块**。
- 逐行解释 [`numeric.py`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/numeric.py#L1-L12) 里 `__getattr__` 如何用「先 `import` 真模块、再 `getattr` 取属性」两步完成转发。

本讲承接 [u1-l2](u1-l2-directory-map.md)（目录分类）和 [u1-l3](u1-l3-run-and-observe-warning.md)（捕获 `DeprecationWarning`）：前两讲告诉你这些垫片文件**是什么**、**怎么观测**，本讲回答**它是怎么做到的**。

## 2. 前置知识

### 2.1 Python 的属性查找会先查 `__dict__`

当你写 `obj.x`，Python 会先去对象的命名空间里找 `x`。模块的命名空间就是它的 `__dict__`。如果你在一个模块文件顶部写了 `x = 1`，那么 `module.x` 就直接命中 `__dict__['x']`，不需要任何额外机制。

### 2.2 类里的 `__getattr__` 你可能见过

在自定义类里，你可以定义 `__getattr__(self, name)`：当正常查找（`__dict__`、类继承链）都找不到 `name` 时，Python 才会回调它。它的典型用途是「按需生成属性」「代理到别的对象」。

### 2.3 模块对象以前不能这样玩

在 Python 3.7 之前，模块对象**不能**像类一样自定义 `__getattr__`。如果你想让一个模块在访问不存在的名字时做点手脚（比如延迟加载），只能用各种 hack（把模块替换成某个类的实例等），既丑陋又脆弱。PEP 562 就是为解决这个问题而生的——它把同样的能力直接给了「模块」本身。

> 如果你已经熟悉以上三点，可以直接跳到第 4 节。

## 3. 本讲源码地图

本讲涉及的关键文件都在 `numpy/core/` 下，它们体量极小，适合整文件精读：

| 文件 | 行数 | 作用 |
| --- | --- | --- |
| [`__init__.py`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/__init__.py#L1-L33) | 约 33 行 | 包入口。顶层 `from numpy import _core`，再用**包级** `__getattr__` 把缺失名字转发到 `numpy._core`；并用 `__all__` 声明可惰性加载的子模块名。 |
| [`numeric.py`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/numeric.py#L1-L12) | 12 行 | **本讲主范例**。只有模块级 `__getattr__`，用「sentinel 哨兵 + 先 import 真 numeric、再 getattr」转发到 `numpy._core.numeric`。 |
| [`umath.py`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/umath.py#L1-L10) | 10 行 | 与 `numeric.py` 结构相同，只是用 `None` 代替 sentinel 判断缺失（详见 [u2-l2](u2-l2-delegation-patterns.md)）。 |
| [`arrayprint.py`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/arrayprint.py#L1-L10) | 10 行 | 同上，转发到 `numpy._core.arrayprint`。 |
| [`_utils.py`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_utils.py#L4-L21) | 21 行 | 提供 `_raise_warning`，负责拼装并抛出统一的 `DeprecationWarning`（详见 [u2-l3](u2-l3-raise-warning.md)）。本讲只把它当作「一个会报警的函数」使用。 |

一句话概括：这五个文件里，`__init__.py` 是**包级第一层**转发，`numeric.py` / `umath.py` / `arrayprint.py` 是**子模块级第二层**转发，`_utils.py` 是被它们共享的「报警器」。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，按「原理 → 机制 → 范例」递进：

- **4.1** PEP 562：让模块也能拦截属性访问（原理）
- **4.2** 模块级 `__getattr__` 的调用时机：惰性转发的关键（机制）
- **4.3** 精读 `numeric.py`：`import` 与 `getattr` 的两步配合（范例）

### 4.1 PEP 562：让模块也能拦截属性访问

#### 4.1.1 概念说明

**PEP 562**（Python 3.7 起生效，标题 *Customizing Module Attribute Access*）赋予了模块一项以前只有类才有的能力：在模块顶层定义一个名为 `__getattr__` 的函数，它会在「正常属性查找失败」时被 Python 回调。

定义方式非常朴素——直接在模块文件里写一个普通函数：

```python
# some_module.py
def __getattr__(name):          # 注意：没有 self
    ...
```

它的存在意义在于：模块也是一个对象（`types.ModuleType` 的实例），但在 PEP 562 之前，你无法定制「访问模块的某个属性时该干什么」。有了它，模块就能像代理一样，把对某个名字的访问**转发**到别处——这正是 `numpy.core` 垫片要做的事：把 `numpy.core.X` 转发到真正的 `numpy._core.X`。

> 注意：模块级 `__getattr__` **没有 `self` 参数**，签名是 `__getattr__(name)`。这是它和「类里的 `__getattr__(self, name)`」最显眼的区别——因为模块本身就是那个被访问的对象，不需要再传自身。

#### 4.1.2 核心流程

当你写 `module.foo` 时，Python 的处理流程是：

1. 在 `module.__dict__` 里找 `foo`。命中则直接返回，**不会**调用 `__getattr__`。
2. 再去模块的类型 `ModuleType` 上找（像 `__name__`、`__doc__` 这类）。命中也直接返回。
3. 都没找到——本该抛 `AttributeError`。但此时 Python 会检查 `module.__dict__` 里有没有 `__getattr__` 这个键。
4. 如果有，就调用 `module.__dict__["__getattr__"]("foo")`，用它的返回值作为 `module.foo` 的结果。
5. 如果 `__getattr__` 自己抛了 `AttributeError`，这个异常会原样冒出去，效果就等于「这个属性真的不存在」。

用伪代码表示 CPython 内部（[`Objects/module.c`](https://github.com/python/cpython) 里的 `module_getattro`，此处为示意）：

```
function module_getattro(module, name):
    if name in module.__dict__:           # 第 1 步
        return module.__dict__[name]
    attr = lookup_on_ModuleType(name)      # 第 2 步
    if attr found:
        return attr
    # —— 到这里说明「正常查找失败」——
    if "__getattr__" in module.__dict__:   # 第 3、4 步
        return module.__dict__["__getattr__"](name)
    raise AttributeError(name)             # 第 5 步
```

这条流程里有三个对后续极其重要的性质：

- **只在「找不到」时触发**：名字若已在 `__dict__` 里，`__getattr__` 永远不会被调用。
- **每次访问缺失属性都会触发**：Python **不会**把 `__getattr__` 的返回值自动缓存进 `__dict__`。所以反复访问同一个缺失名字，会反复调用 `__getattr__`。
- **可以用 `AttributeError` 表示「真没有」**：`__getattr__` 内部判断后主动抛 `AttributeError`，就能区分「我帮你转发」和「这个名字确实不存在」。

#### 4.1.3 源码精读

最干净的例子是包入口 `numpy/core/__init__.py` 的包级 `__getattr__`：

```python
# numpy/core/__init__.py
from numpy import _core          # L6：顶部就把真实现包引入
from ._utils import _raise_warning   # L8：引入报警器

__all__ = ["arrayprint", "defchararray", ..., "numeric", "umath"]  # L25-L28

def __getattr__(attr_name):      # L30-L33：模块级 __getattr__
    attr = getattr(_core, attr_name)
    _raise_warning(attr_name)
    return attr
```

- [numpy/core/__init__.py:L30-L33](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/__init__.py#L30-L33)：定义模块级 `__getattr__`。当你访问 `numpy.core.<某名字>` 而该名字不在包的 `__dict__` 里时，Python 会调用它。
- 它直接 `getattr(_core, attr_name)`：从真实现包 `numpy._core` 上取同名对象。**如果 `_core` 上也没有这个名字，`getattr` 会自然抛 `AttributeError`**——于是这个垫片「原汁原味」地保留了「真没有就报错」的语义，连手写 `AttributeError` 都省了。
- [numpy/core/__init__.py:L6-L8](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/__init__.py#L6-L8)：注意 `_core` 是在**模块顶部**就 import 好的，所以 `__getattr__` 里可以直接用这个名字。这和 4.3 节 `numeric.py` 里「把 import 写在 `__getattr__` 内部」是两种不同写法，本讲 4.2 节会专门对比。

#### 4.1.4 代码实践

**实践目标**：亲手验证 PEP 562 的三条性质——只在缺失时触发、每次缺失访问都触发、抛 `AttributeError` 等于「真没有」。

**操作步骤**：

1. 新建一个文件 `mod.py`：

```python
# mod.py  —— 示例代码（非 numpy 源码）
x = 1                       # 这是一个「正常存在」的属性

def __getattr__(name):      # 模块级 __getattr__
    print(f"  [__getattr__] called for {name!r}")
    if name == "missing":
        raise AttributeError(f"module 'mod' has no attribute {name!r}")
    return f"value-of-{name}"
```

2. 在同目录运行：

```python
import mod

print("--- 访问已存在的 x ---")
print(mod.x)            # 期望：不触发 __getattr__

print("--- 访问 lazy_attr ---")
print(mod.lazy_attr)    # 期望：触发一次
print(mod.lazy_attr)    # 期望：再触发一次（无缓存！）

print("--- 访问 missing ---")
try:
    mod.missing
except AttributeError as e:
    print("捕获到:", e)
```

**需要观察的现象**：

- `mod.x` 不会打印 `[__getattr__]`，证明**已存在的属性不经过** `__getattr__`。
- `mod.lazy_attr` 的两次访问**各打印一次** `[__getattr__]`，证明**没有自动缓存**，每次缺失访问都会触发。
- `mod.missing` 会抛 `AttributeError`，证明 `__getattr__` 内部主动抛错就等同于「真不存在」。

**预期结果**：

```
--- 访问已存在的 x ---
1
--- 访问 lazy_attr ---
  [__getattr__] called for 'lazy_attr'
value-of-lazy_attr
  [__getattr__] called for 'lazy_attr'
value-of-lazy_attr
--- 访问 missing ---
  [__getattr__] called for 'missing'
捕获到: module 'mod' has no attribute 'missing'
```

### 4.2 模块级 `__getattr__` 的调用时机：惰性转发的关键

#### 4.2.1 概念说明

理解了 4.1，再回头看 `numpy.core` 的设计目标就很清晰：它要为每一个**子模块**（`numeric`、`umath`、`arrayprint`…）各建一个「垫片」，垫片本身几乎不干活，只把属性访问转发到 `numpy._core` 下的同名真模块。

这里有一个关键选择：**真模块的 `import` 写在哪里？**

- **写法 A（eager，急切）**：在垫片文件顶部就 `from numpy._core import numeric`。
- **写法 B（lazy，惰性）**：把 `from numpy._core import numeric` 写进 `__getattr__` 函数体里，等首次访问属性时才执行。

`numpy.core` 的子模块垫片**全部采用写法 B**（见 `numeric.py` 第 2 行、`umath.py` 第 2 行）。这就是本讲标题里的「**惰性转发**」。

为什么这样做？因为 NumPy 的底层模块（`numpy._core.numeric`、`numpy._core.umath` 等）是体积庞大、依赖很重的 C 扩展。如果把它们的 `import` 放在垫片顶部，那么光是 `import numpy.core.numeric`（甚至 `import numpy.core` 再触发子模块链）就会把这些重模块一次性全部拉起来。而放进 `__getattr__` 之后：

- **垫片本身极轻**：`import numpy.core.numeric` 只是执行那 12 行 `__getattr__` 定义，不触发任何底层 C 扩展。
- **真模块按需加载**：直到用户**第一次访问** `numpy.core.numeric.<某属性>` 时，`__getattr__` 才被调用，才执行那行 `from numpy._core import numeric`。

> 重要区分（也是本讲综合实践的考点）：
> - **模块的顶层代码只执行一次**：Python 把已导入的模块缓存在 `sys.modules` 里。`from numpy._core import numeric` 第一次执行时会跑完 `numpy._core.numeric` 的全部顶层代码；之后无论你再触发多少次，都只是从 `sys.modules` 里取已存在对象，**不会**重新执行顶层代码。
> - **`__getattr__` 函数体每次访问都执行**：因为垫片从不把返回值塞回自己的 `__dict__`（4.1.2 的第二条性质），所以每次访问缺失属性都会重新调用 `__getattr__`——这正是「每次访问都打印一条 `DeprecationWarning`」的根本原因。
>
> 把这两条合起来：**「import 只发生一次，但 `__getattr__` 每次都跑」**。这是 numpy 垫片「既惰性、又每次都报警」的设计核心。

#### 4.2.2 核心流程

以访问 `numpy.core.umath.add` 为例（先看简单的 `None` 写法，sentinel 留到 4.3）：

```
用户代码: numpy.core.umath.add
        │
        ▼
1. 访问 numpy.core.umath（取子模块）
   └─ 走导入机制，加载 numpy/core/umath.py 这个垫片
        │
        ▼
2. 在「垫片 umath 模块」上访问 .add
   └─ add 不在垫片的 __dict__ 里（垫片只有 __getattr__）
   └─ Python 回调 umath.__getattr__("add")
        │
        ▼
3. __getattr__ 内部:
   ├─ from numpy._core import umath   # 取真模块（首次才真正 import，之后走 sys.modules 缓存）
   ├─ ret = getattr(真umath, "add", None)
   ├─ if ret is None: raise AttributeError(...)   # 真没有就报错
   ├─ _raise_warning("add", "umath")               # 抛 DeprecationWarning
   └─ return ret                                    # 把真模块的 add 返回给用户
```

注意第 3 步的「先 `import` 真模块、再 `getattr` 取属性」两步配合，正是 4.3 节要精读的 `numeric.py` 的结构。

#### 4.2.3 源码精读

看 `umath.py`（[`arrayprint.py`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/arrayprint.py#L1-L10)、[`records.py`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/records.py#L1-L10)、[`shape_base.py`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/shape_base.py#L1-L10) 与它逐字同构）：

```python
# numpy/core/umath.py
def __getattr__(attr_name):
    from numpy._core import umath          # L2：惰性 import，写在函数体内

    from ._utils import _raise_warning
    ret = getattr(umath, attr_name, None)  # L5：在真模块上取属性，缺失返回 None
    if ret is None:                         # L6：None 视为「真没有」
        raise AttributeError(
            f"module 'numpy.core.umath' has no attribute {attr_name}")
    _raise_warning(attr_name, "umath")      # L9：统一报警
    return ret
```

- [numpy/core/umath.py:L1-L10](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/umath.py#L1-L10)：整个文件只有一个函数。**第 2 行的 `from numpy._core import umath` 是惰性的**——它不在垫片被导入时执行，而在「访问 `numpy.core.umath.<属性>` 触发 `__getattr__`」时才执行。
- 第 5 行 `getattr(umath, attr_name, None)`：在**真模块** `numpy._core.umath` 上取属性；`None` 是 `getattr` 的「找不到时的默认值」。这种写法在面对 `0`、`False`、`None` 这类「假值但有效」的属性时有坑，留到 [u2-l2](u2-l2-delegation-patterns.md) 专门讨论。
- 第 9 行 `_raise_warning(attr_name, "umath")`：调 [`_utils._raise_warning`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_utils.py#L4-L21)，抛一条 `DeprecationWarning`。因为 `__getattr__` 每次都跑（4.2.1），所以**每次访问都会报警**。

对比 4.1.3 里 `__init__.py` 的包级 `__getattr__`：包级把 `_core` import 在**顶部**（因为包入口本来就要依赖它），子模块垫片把真模块 import 在**函数体内**（为了惰性）。两者都成立，选择取决于「这个 import 放顶部会不会带来我们不想要的加载开销」。

#### 4.2.4 代码实践

**实践目标**：直观对比「eager 急切 import」与「lazy 惰性 import」对加载时机的影响，体会 numpy 选择惰性的原因。

**操作步骤**：

1. 建两个包，结构如下：

```
eager_pkg/
  __init__.py        # from . import heavy
  heavy.py           # print("== heavy loaded ==")
lazy_pkg/
  __init__.py        # 见下方
  heavy.py           # print("== heavy loaded ==")
```

```python
# lazy_pkg/__init__.py  —— 示例代码
__all__ = ["heavy"]              # 声明可惰性加载的子模块名

def __getattr__(name):           # 包级 __getattr__
    if name == "heavy":
        print("  [lazy_pkg] 首次请求 heavy，现在才 import")
        from . import heavy
        return heavy
    raise AttributeError(name)
```

2. 分别导入两个包，**只导入包本身**：

```python
print(">>> import eager_pkg")
import eager_pkg                 # 期望：立即打印 "== heavy loaded =="

print(">>> import lazy_pkg")
import lazy_pkg                  # 期望：什么都不打印（heavy 还没被加载）
```

**需要观察的现象**：

- 导入 `eager_pkg` 时，`heavy` 的顶层 `print` **立刻**执行——急切 import 把重模块拖进了导入链。
- 导入 `lazy_pkg` 时，**没有任何输出**——因为 `heavy` 没有在顶部被 import，`__getattr__` 还没被触发。

**预期结果**：

```
>>> import eager_pkg
== heavy loaded ==
>>> import lazy_pkg
```

**结论**：这正是 `numpy/core/__init__.py` 不写 `from . import numeric, umath, ...`、而是用 `__all__` + `__getattr__` 的原因——避免 `import numpy.core` 时把十几个底层子模块一股脑拉起来。如果你接着访问 `lazy_pkg.heavy`，才会看到 `[lazy_pkg] 首次请求…` 与 `== heavy loaded ==` 各打印一次。

### 4.3 精读 `numeric.py`：`import` 与 `getattr` 的两步配合

#### 4.3.1 概念说明

`numeric.py` 是 14 个纯转发垫片里**唯一**用 sentinel（哨兵）写法的一个（其余都用 4.2 里的 `None` 写法，详见 [u1-l2](u1-l2-directory-map.md)）。它把「惰性转发」的所有要素集中在了 12 行里，是本讲的最佳精读对象。

它解决的问题：如何安全地把 `numpy.core.numeric.<任意属性>` 转发到 `numpy._core.numeric`，并且：

1. 不在垫片导入时加载真模块（惰性）。
2. 真模块里没有这个名字时，抛出正确的 `AttributeError`。
3. 即便真模块里某属性的值是「假值」（如 `0`、`False`、`None`），也不会被误判为「不存在」——这正是它用 sentinel 而非 `None` 的好处（完整对比留到 [u2-l2](u2-l2-delegation-patterns.md)）。

> **什么是 sentinel（哨兵）？** 一个「全局唯一、不可能和任何正常值相等」的对象，专门用来表示「还没有取到值」的占位状态。本讲你只需理解：`sentinel = object()` 造出一个谁也不会等于它的对象，`getattr(..., sentinel)` 找不到属性时返回它，再拿结果 `is sentinel` 一比，就能 100% 判断「到底找没找到」，不受 `0`/`None`/`False` 干扰。

#### 4.3.2 核心流程

以 `numpy.core.numeric.asarray` 为例：

```
1. 用户访问 numpy.core.numeric.asarray
2. asarray 不在「垫片 numeric」的 __dict__ 里 → 回调 numeric.__getattr__("asarray")
3. __getattr__ 内部（按源码顺序）:
   ├─ from numpy._core import numeric      # 惰性：取真模块（首次才真正加载）
   ├─ from ._utils import _raise_warning   # 取报警器
   ├─ sentinel = object()                   # 造一个唯一哨兵
   ├─ ret = getattr(真numeric, "asarray", sentinel)
   │       └─ 找到 → ret = 真 asarray
   │       └─ 没找到 → ret 就是 sentinel
   ├─ if ret is sentinel:                   # 用身份比较判断
   │       raise AttributeError(...)        # 真没有 → 报错
   ├─ _raise_warning("asarray", "numeric")  # 抛 DeprecationWarning
   └─ return ret                             # 返回真 asarray
```

注意第 3 步的顺序很讲究：**先判存在性，再报警，最后返回**。这意味着「名字确实不存在」时**不会**报警（直接抛 `AttributeError`），只有「名字存在但已废弃」才既报警又正常返回。这和 [u1-l3](u1-l3-run-and-observe-warning.md) 观测到的「报警与正常返回同时发生、但不中断程序」完全吻合。

#### 4.3.3 源码精读

逐行看 [`numeric.py`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/numeric.py#L1-L12)：

```python
# numpy/core/numeric.py
def __getattr__(attr_name):                 # L1：模块级 __getattr__（PEP 562）
    from numpy._core import numeric         # L2：惰性 import 真模块

    from ._utils import _raise_warning      # L4：惰性 import 报警器

    sentinel = object()                      # L6：造唯一哨兵
    ret = getattr(numeric, attr_name, sentinel)  # L7：在真模块上取属性，缺失则得 sentinel
    if ret is sentinel:                      # L8：身份比较，判断是否真缺失
        raise AttributeError(               # L9-L10：真缺失 → 抛错，且不报警
            f"module 'numpy.core.numeric' has no attribute {attr_name}")
    _raise_warning(attr_name, "numeric")    # L11：存在但废弃 → 报警
    return ret                               # L12：正常返回真属性
```

关键点逐一对应：

- [numpy/core/numeric.py:L2](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/numeric.py#L2)：`from numpy._core import numeric` 写在函数体内，是「惰性」的核心。首次访问任意属性时才执行；后续访问时 `numpy._core.numeric` 已在 `sys.modules` 里，这行只是个廉价的名字绑定（不重跑顶层代码）。
- [numpy/core/numeric.py:L6-L8](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/numeric.py#L6-L8)：sentinel 三连——`object()` 做哨兵、`getattr(..., sentinel)` 取值、`is sentinel` 判定。这一套保证即便真模块里有个值为 `0` 的属性（比如某个常量），`0 is sentinel` 也是 `False`，不会被误杀。
- [numpy/core/numeric.py:L9-L10](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/numeric.py#L9-L10)：`AttributeError` 抛在报警**之前**。即「真没有」不报警、「有但废弃」才报警——`_raise_warning`（[numpy/core/_utils.py:L4-L21](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_utils.py#L4-L21)）只会被「能正常返回」的路径调用。
- [numpy/core/numeric.py:L11-L12](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/numeric.py#L11-L12)：报警后**仍然 return**。这就是为什么访问废弃属性「能用、但会唠叨」。

对比 4.2 的 `umath.py`：两者只差「判缺失」这一步——`umath.py` 用 `None`，`numeric.py` 用 sentinel。`getattr(obj, name, None)` 在 `obj.name` 恰好等于 `0`/`False`/`None` 时会**误判为缺失**，而 sentinel 不会。NumPy 对 `numeric` 用更严格的写法，原因留作 [u2-l2](u2-l2-delegation-patterns.md) 的主线。

#### 4.3.4 代码实践（源码阅读型）

**实践目标**：不写新代码，而是把 `numpy.core.numeric` 当作黑盒，跟踪一条真实调用链，验证「import 一次、`__getattr__` 多次」以及「报警但正常返回」。

**操作步骤**：

1. 准备捕获脚本 `trace_numeric.py`：

```python
# trace_numeric.py  —— 示例代码
import warnings, sys

import numpy.core.numeric as shim   # 导入垫片；此时不应触发底层 numeric 的重活

with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")
    a = shim.asarray([1, 2, 3])     # 第一次访问 asarray
    b = shim.asarray([4, 5, 6])     # 第二次访问 asarray

print("返回值类型:", type(a), "内容:", a.tolist())
print("捕获到警告数:", len(caught))
for w in caught:
    print("  类别:", w.category.__name__)
    print("  消息含 numpy._core:", "numpy._core" in str(w.message))

print("numpy._core.numeric 是否已加载:", "numpy._core.numeric" in sys.modules)
```

2. 运行：`python trace_numeric.py`。

**需要观察的现象**：

- 两次 `shim.asarray(...)` 共产生**两条** `DeprecationWarning`（`__getattr__` 每次都跑），但 `numpy._core.numeric` 在 `sys.modules` 里**只有一条**记录（import 只发生一次）。
- 两次调用都**正常返回**了数组，没有中断——印证「报警与正常返回同时发生」。
- 警告类别是 `DeprecationWarning`，消息里包含 `numpy._core`。

**预期结果**：`捕获到警告数: 2`，两条消息都含 `numpy._core`；返回值正确为 `[1,2,3]` 与 `[4,5,6]`；`sys.modules` 检查为 `True`。

> 若你的环境过滤了 `DeprecationWarning` 导致「捕获到警告数: 0」，请确认 `simplefilter("always")` 写在了 `with` 块**内部**（进入 `catch_warnings` 会重置过滤器，见 [u1-l3](u1-l3-run-and-observe-warning.md)）。若仍不确定运行结果，请标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `numeric.py` 第 2 行的 `from numpy._core import numeric` 移到文件顶部（`__getattr__` 外面），垫片还「惰性」吗？功能会坏吗？

> **参考答案**：不再惰性——`import numpy.core.numeric` 就会立即触发 `numpy._core.numeric` 的加载。功能不会坏（`__getattr__` 内仍能拿到 `numeric` 这个名字），但失去了「垫片极轻、按需加载」的好处，正是 numpy 要避免的。

**练习 2**：为什么 `numeric.py` 先判断 `if ret is sentinel` 抛 `AttributeError`，**然后**才调 `_raise_warning`？如果调换这两步的顺序会怎样？

> **参考答案**：因为「名字真的不存在」应当表现为标准的 `AttributeError`，**不该**再叠加一条弃用警告（否则会误导用户以为是「废弃导致的报错」）。先抛错就提前退出了函数，根本走不到 `_raise_warning`。若调换顺序，则访问任何不存在的名字都会先报警再报错，既啰嗦又语义不清。

**练习 3**：`getattr(numeric, attr_name, sentinel)` 里的第三个参数 `sentinel` 作用是什么？为什么不用 `None`？

> **参考答案**：它是 `getattr` 的「找不到时的默认返回值」。用 sentinel（一个唯一对象）而非 `None`，是因为真模块里完全可能存在值为 `None`/`0`/`False` 的合法属性；用 `None` 当默认值会把「属性值恰好是 `None`」误判成「属性不存在」。sentinel 用 `is` 做身份比较，绝不会被任何正常值撞上。完整对比见 [u2-l2](u2-l2-delegation-patterns.md)。

## 5. 综合实践

把本讲三个最小模块串起来，完成规格里指定的实践任务：**亲手造一个用模块级 `__getattr__` 做惰性转发的包**。

**实践目标**：建一个 `mypkg`，其中的 `lazy.py` 用模块级 `__getattr__`，在**首次访问某属性时**才 import 一个「昂贵」子模块并返回其属性；用日志证明「第二次访问同一属性不会重新触发 import」。

**操作步骤**：

1. 建如下目录（在 `mypkg` 的**父目录**运行脚本）：

```
mypkg/
  __init__.py        # 空文件，或写一行注释即可
  lazy.py
  expensive.py
```

```python
# mypkg/expensive.py  —— 示例代码：模拟一个「昂贵」的子模块
print("[expensive] === 执行昂贵的顶层代码 ===")
VALUE = 42
def greet():
    return "hello from expensive"
```

```python
# mypkg/lazy.py  —— 示例代码：仿 numeric.py 的惰性转发
def __getattr__(name):
    print(f"[lazy] __getattr__ 被调用，请求 {name!r}，正在 import expensive ...")
    from . import expensive               # 惰性 import：写在函数体内
    sentinel = object()
    ret = getattr(expensive, name, sentinel)
    if ret is sentinel:
        raise AttributeError(f"module 'mypkg.lazy' has no attribute {name!r}")
    return ret                            # 注意：不报警、不缓存——纯粹演示惰性
```

2. 在 `mypkg` 的父目录运行：

```python
from mypkg import lazy

print("--- 第一次访问 lazy.VALUE ---")
print(lazy.VALUE)
print("--- 第二次访问 lazy.VALUE ---")
print(lazy.VALUE)
print("--- 访问 lazy.greet() ---")
print(lazy.greet())
```

**需要观察的现象（这是本实践的判分点）**：

- `[expensive] === 执行昂贵的顶层代码 ===` 这行**只出现一次**——在第一次访问 `lazy.VALUE` 时。第二次访问、以及 `lazy.greet()` 时都不再出现，证明「**import 只发生一次**」（`sys.modules` 缓存）。
- `[lazy] __getattr__ 被调用…` 这行**每次访问都出现**（三次），证明「**`__getattr__` 每次都跑**」（返回值没被缓存进 `lazy` 的 `__dict__`）。

**预期结果**：

```
--- 第一次访问 lazy.VALUE ---
[lazy] __getattr__ 被调用，请求 'VALUE'，正在 import expensive ...
[expensive] === 执行昂贵的顶层代码 ===
42
--- 第二次访问 lazy.VALUE ---
[lazy] __getattr__ 被调用，请求 'VALUE'，正在 import expensive ...
42
--- 访问 lazy.greet() ---
[lazy] __getattr__ 被调用，请求 'greet'，正在 import expensive ...
hello from expensive
```

**对照 numpy**：把 `mypkg.expensive` 想象成 `numpy._core.numeric`，把 `mypkg.lazy` 想象成 `numpy/core/numeric.py`。`lazy.py` 比 `numeric.py` 只少了两件事——一是没有 sentinel 的报警调用（本讲聚焦惰性，报警已在 4.3 里讲过），二是它转发到的是同级子模块而非 `_core`。如果你愿意，可以在 `__getattr__` 里再加一行 `_raise_warning` 风格的 `warnings.warn(...)`，把它升级成一个「又惰性、又每次报警」的真垫片。

> 若无法运行，请标注「待本地验证」并把上面的「预期结果」作为判断依据。

## 6. 本讲小结

- **PEP 562** 让模块能像类一样定义模块级 `__getattr__(name)`，在**正常属性查找失败**时被 Python 回调；它只在「找不到」时触发、每次缺失访问都触发（无自动缓存）、可用 `AttributeError` 表示「真没有」。
- `numpy.core` 的子模块垫片把真模块的 `import` 写在 `__getattr__` **函数体内**，实现**惰性转发**：垫片本身极轻，真模块按需加载，避免 `import numpy.core` 时一次性拉起十几个底层 C 扩展。
- 关键区分：**模块顶层代码只执行一次**（`sys.modules` 缓存），**但 `__getattr__` 每次访问都跑**——这正是垫片「既惰性、又每次都打印 `DeprecationWarning`」的根因。
- [`numeric.py`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/numeric.py#L1-L12) 把惰性转发浓缩在 12 行里：`from numpy._core import numeric`（惰性取真模块）→ `getattr(..., sentinel)`（取属性并判存在）→ `_raise_warning`（报警）→ `return`（正常返回）。
- 包入口 [`__init__.py`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/__init__.py#L30-L33) 提供第一层转发（`getattr(_core, name)`），子模块垫片提供第二层（`getattr(numpy._core.X, name)`）；两者都用 PEP 562，只是 import 放置位置不同。

## 7. 下一步学习建议

本讲建立了「PEP 562 模块级 `__getattr__` + 惰性转发」的完整心智模型，接下来按依赖顺序推荐：

- **[u2-l2 委派模式：sentinel 与 None 两种缺失属性处理](u2-l2-delegation-patterns.md)**：本讲点到的「`None` 写法面对假值属性有坑」是它的主线，建议紧接着读，把 `numeric.py` 的 sentinel 与 `umath.py` 的 `None` 彻底掰开。
- **[u2-l3 `_raise_warning`：统一弃用信息的生成与 stacklevel](u2-l3-raise-warning.md)**：本讲把 `_raise_warning` 当黑盒用了，下一讲拆开它，看 `stacklevel=3` 如何让警告指向你的代码而非 numpy 内部。
- **[u2-l4 包入口 `__init__.py`：`__all__`、`__getattr__` 与 `_ufunc_reconstruct`](u2-l4-package-init.md)**：本讲 4.1.3 略过的 `__all__`、`_ufunc_reconstruct` 等包级细节，在那里集中讲透。
- 进阶可选：学完本单元后，[u3-l1](u3-l1-pickle-compat.md) 会展示「为什么有些属性不能等惰性 `__getattr__`、必须在顶部 eager 绑定」——那是对本讲惰性设计的一个有意「例外」。
