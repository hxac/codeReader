# 运行 numpy.core：触发并捕获 DeprecationWarning

## 1. 本讲目标

前两讲我们已经在「纸面」上确认：`numpy.core` 是一块向后兼容垫片，访问它的属性时会触发一条 `DeprecationWarning`（弃用警告）。但「纸面」和「实际跑起来」是两回事——很多读者第一次访问 `numpy.core` 时，会发现**屏幕上根本没看到任何警告**，于是怀疑自己理解错了。

本讲就把这条警告「跑出来给你看」。读完本讲，你应当能够：

- 解释 Python 里**警告（Warning）和异常（Exception）的区别**，明白为什么访问 `numpy.core` 的属性「会报警但仍然正常返回」。
- 说出 `DeprecationWarning` 为什么常常**看不见**——Python 的警告过滤器会把它静默掉。
- 用标准库 `warnings` 的 `catch_warnings(record=True)` + `simplefilter("always")` **可靠地捕获**这条警告。
- 写一个测试函数，对警告的**类别**和**消息文本**做断言。
- 看懂 `_raise_warning` 里 `stacklevel=3` 的作用，并验证它把警告的「发生位置」指向了你的调用代码。

本讲承接 u1-l1（垫片定位）和 u1-l2（文件分类），但**不重复**它们的结论，而是把焦点放在「如何观测、如何验证」上。`__getattr__` 的惰性转发细节仍是第 2 单元（u2-l1）的主题，本讲只用到「访问属性会触发 `__getattr__`」这一已建立的结论。

## 2. 前置知识

在动手捕获警告前，先用大白话把 Python 的「警告机制」讲清楚——这是本讲最容易踩坑的地方。

### 2.1 警告（Warning）和异常（Exception）有什么不同

它们长得像，都会用 `warnings.warn(...)` / `raise ...` 发出，但行为完全不同：

| | 异常（Exception） | 警告（Warning） |
| --- | --- | --- |
| 发出方式 | `raise SomeError(...)` | `warnings.warn(message, category)` |
| 是否中断程序 | **会**中断，必须捕获或程序崩溃 | **不会**中断，程序继续往下跑 |
| 典型用途 | 「出错了，必须处理」 | 「还能用，但建议你别这么用」 |
| 本讲例子 | 访问不存在的属性抛 `AttributeError` | 访问 `numpy.core.numeric.zeros` 抛 `DeprecationWarning` |

> 名词解释：**`DeprecationWarning`** 是 Python 内置的一种警告类别，专门用来提示「这个东西将来版本会删除」。它属于「还能用，但别依赖」。

这条「不中断」的特性正是 numpy 垫片想要的：**老代码照常拿到对象、正常运行，只是在旁边提醒一句「以后改用 `numpy._core` 或公开 API」**。所以「报警」和「功能正常返回」是同时发生的，不矛盾。

### 2.2 警告的分类（Warning categories）

`warnings.warn(msg, 类别)` 的第二个参数是警告类别，必须是 `Warning` 的子类。常见的有：

- `DeprecationWarning`：本讲的主角，表示「已废弃」。
- `FutureWarning`：面向终端用户的「将来行为会变」。
- `UserWarning`：`warn()` 不指定类别时的默认值。
- `RuntimeWarning`：运行时可疑行为。

类别很重要，因为 Python 的「警告过滤器」可以**按类别**决定显示还是隐藏。

### 2.3 警告过滤器：为什么有时候看不到警告

这是本讲最关键的认知。Python 启动时会装一套**默认警告过滤器**，对 `DeprecationWarning` 尤其苛刻：默认情况下，它**只**当代码位于 `__main__`（也就是你直接运行的那个脚本）时才显示，其他来源（比如来自第三方库内部）的 `DeprecationWarning` 会被**静默**。此外，「默认」动作还会让「同一位置」的警告只出现一次。

> 打个比方：警告过滤器就像图书馆的「广播系统」，默认只对「在本馆办过登记的读者」喊话（`__main__`），而且对同一句话只喊一遍。所以你访问 `numpy.core` 时，警告很可能被广播系统「过滤掉」，你压根听不见。

因此在测试或脚本里，光触发警告还不够。要可靠地观测它，必须临时**关掉过滤**：

```python
import warnings
warnings.simplefilter("always")
```

`"always"` 的意思是「不管类别、不管来源、不管是不是重复，每次都让我看见」。本讲的捕获实践都会用到它。

### 2.4 承接前两讲

u1-l1 已经指出：访问 `numpy.core.<名字>` 会进入包级 [`__getattr__`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/__init__.py#L30-L33)，它调用 [`_utils._raise_warning`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_utils.py#L4-L21) 发出弃用警告，再把对象返回。u1-l2 进一步把目录里的文件分了类。本讲不再重复这些结论，而是回答一个新的问题：**这条警告到底怎么被发出来、又怎么被我们抓住？**

## 3. 本讲源码地图

本讲只涉及三个运行时文件，都是前两讲见过的老朋友，但本讲看它们的视角不同——关注「警告的发出与捕获」。

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| [`_utils.py`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_utils.py) | 工具文件，提供统一的弃用警告函数 | `warnings.warn(...)` 的调用方式、消息文本、`stacklevel` |
| [`numeric.py`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/numeric.py) | 纯转发垫片 | 子模块层的 `__getattr__` 如何「转发 + 报警 + 仍然返回」 |
| [`__init__.py`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/__init__.py) | 包入口 | 包级 `__getattr__`（对照用，u1-l1 已精读） |

此外会用到 Python 标准库 [`warnings`](https://docs.python.org/3/library/warnings.html)（不属于 numpy 源码，但本讲的核心工具）。

## 4. 核心概念与源码讲解

本讲围绕三个最小模块展开：**`warnings` 标准库**、**`_raise_warning`**、**`numeric.__getattr__`**。

### 4.1 `warnings` 标准库：警告的发出、过滤与捕获

#### 4.1.1 概念说明

Python 内置的 `warnings` 模块是「警告机制」的总开关，它同时管三件事：

1. **发出**：`warnings.warn(message, category, stacklevel=...)` 发出一条警告。
2. **过滤**：根据「类别 + 来源模块 + 消息正则」决定这条警告是被显示、被忽略，还是抛成异常。
3. **捕获**：在一段代码里临时把警告「录下来」，而不是打印到屏幕——这正是测试里要用的。

本讲最常用的三个工具：

- `warnings.simplefilter("always")`：临时关掉过滤，让（被静默的）`DeprecationWarning` 也能被看见。
- `warnings.catch_warnings(record=True)`：一个上下文管理器（`with` 块），进入时**重置**过滤策略、把「显示函数」换成一个「记录到列表」的函数；`record=True` 时它返回这个列表，列表里每个元素是一条 `warnings.WarningMessage`。
- `WarningMessage` 对象的常用属性：`.category`（类别，如 `DeprecationWarning`）、`.message`（警告对象，`str(...)` 得到文本）、`.filename` / `.lineno`（警告指向的代码位置）。

> 易错点：`catch_warnings()` 进入 `with` 块时会**重置**过滤器到默认状态。所以哪怕你在 `with` 外面调过 `simplefilter("always")`，进入 `with` 后它也被还原了。**`simplefilter("always")` 必须写在 `with` 块内部**才有效。这是初学者最常踩的坑。

#### 4.1.2 核心流程

一条 `DeprecationWarning` 从「发出」到「被你抓住」的全过程：

```
代码：numpy.core.numeric.zeros
        │  访问属性，进入 numeric.__getattr__("zeros")
        ▼
__getattr__ 内部：_raise_warning("zeros", "numeric")
        │  内部调用 warnings.warn(msg, DeprecationWarning, stacklevel=3)
        ▼
warnings 模块：按当前过滤器决定怎么处理这条警告
        │
        ├── 日常运行（默认过滤）：DeprecationWarning 往往被「忽略」或「只显示一次」 → 屏幕看不到
        │
        └── 测试中（with catch_warnings(record=True) + simplefilter("always")）
                ▼
            警告被包装成 WarningMessage，追加到返回的列表里
                ▼
            离开 with 块，过滤器自动还原
```

关键点：**同一个 `warnings.warn` 调用，在不同过滤策略下表现完全不同**。日常看不见，不代表它没发；测试里抓得到，是因为我们临时改了策略。

#### 4.1.3 源码精读

numpy 自己并不重新发明警告机制，它直接调用标准库。看 [`_raise_warning`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_utils.py#L4-L21) 里发出警告的那几行：

[`_utils.py:L10-L21`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_utils.py#L10-L21) —— 调用 `warnings.warn`，第二个参数 `DeprecationWarning` 指定类别，第三个参数 `stacklevel=3` 指定警告「算在谁的头上」：

```python
    warnings.warn(
        f"{old_module} is deprecated and has been renamed to {new_module}. "
        ...
        f"use {new_module}.{attr}.",
        DeprecationWarning,
        stacklevel=3
    )
```

逐个参数对照 4.1.1 的三件事：

- 第一参数：消息字符串（怎么拼出来的，见 4.2）。
- `DeprecationWarning`：这就是 4.1.2 里过滤器要「按类别」判断的对象。默认过滤器对它特别严，所以日常常常看不见。
- `stacklevel=3`：告诉 `warnings`「这条警告不要算在 `warn()` 这一行，也不要算在 `_raise_warning` 里，而要往上数 3 层栈帧，算在**真正访问属性的用户代码**上」。它的效果会在 4.2 和综合实践里亲眼看到。

而「捕获」这一侧，用的完全是标准库的习惯写法（不属于 numpy 源码）：

```python
with warnings.catch_warnings(record=True) as caught:
    warnings.simplefilter("always")   # 必须在 with 内部
    # ... 在这里触发警告 ...
# caught 是一个 list，元素是 WarningMessage
```

#### 4.1.4 代码实践

我们先做最小的一步：**直接访问 `numpy.core` 的属性，观察屏幕上到底有没有警告**，从而体会「过滤器会把它藏起来」。

1. **实践目标**：亲身感受「默认情况下 `DeprecationWarning` 可能根本不显示」。
2. **操作步骤**：新建 `see_warning.py`：

   ```python
   import numpy.core.numeric     # 加载 numeric 这个垫片子模块
   print("准备访问 numpy.core.numeric.zeros ...")
   func = numpy.core.numeric.zeros
   print("拿到了：", func)
   ```

   用 `python see_warning.py` 运行。

3. **需要观察的现象**：
   - 程序**正常结束**，打印出 `拿到了：<built-in function zeros>` 之类——证明「报警不阻断」。
   - 屏幕上**可能**完全看不到 `DeprecationWarning`（取决于你的 Python / 环境是否重定向了过滤策略），也可能看到一条。
4. **预期结果**：你大概率看不到警告。这不是 bug，而是 2.3 说的默认过滤器把它静默了。下一节的实践会把它「逼」出来。
5. **待本地验证**：少数环境（如开启了 `-Wd` 参数、或在 pytest 下）会显示该警告；是否显示以你本地实际输出为准，重点是「程序不会因警告而中断」。

#### 4.1.5 小练习与答案

**练习 1**：用命令行参数 `python -W error::DeprecationWarning see_warning.py` 运行上面的脚本，会发生什么？为什么？

> **参考答案**：`-W error::DeprecationWarning` 表示「把 `DeprecationWarning` 当作错误（error）处理」。于是访问 `numpy.core.numeric.zeros` 时，那条本应「只提示」的警告会**升级成异常**并使程序崩溃。这恰好演示了 2.1 的对照表：同一个警告，在「error」过滤下行为像异常、在默认/always 过滤下行为像提示。它也说明「警告不阻断」是**默认过滤策略**下的结论，而非警告本身的硬性保证。

**练习 2**：为什么必须把 `simplefilter("always")` 写在 `with warnings.catch_warnings(record=True)` 的**内部**，写在外面不行？

> **参考答案**：因为 `catch_warnings()` 在进入 `with` 块时会**保存当前过滤状态并重置**，离开时再恢复。如果你在外面调用 `simplefilter("always")`，进入 `with` 时它会被重置掉，等于没设。所以必须在 `with` 内部、触发警告之前重新设置。

---

### 4.2 `_raise_warning`：弃用信息怎么拼出来

#### 4.2.1 概念说明

[`_utils._raise_warning`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_utils.py#L4-L21) 是整本手册里唯一一个「不做转发、专门负责报警」的函数（u1-l2 把它归类为「工具文件」）。它的职责很纯粹：**根据「被访问的名字」和「它属于哪个子模块」，拼出一段固定的弃用提示，并以 `DeprecationWarning` 发出**。

它有两个参数：

- `attr`：被访问的属性名，例如 `"zeros"`。
- `submodule`：可选，表示这个属性来自哪个子模块，例如 `"numeric"`。不传时，提示针对的是「包级」名字（如 `numpy.core` 本身）。

#### 4.2.2 核心流程

`_raise_warning(attr, submodule)` 的执行步骤：

1. 设定基础名字：`new_module = "numpy._core"`，`old_module = "numpy.core"`。
2. 如果传了 `submodule`，把两边都「加后缀」，变成 `numpy._core.<submodule>` 与 `numpy.core.<submodule>`。
3. 拼出消息：`"<旧名> is deprecated and has been renamed to <新名>. ... use <新名>.<attr>."`。
4. `warnings.warn(消息, DeprecationWarning, stacklevel=3)`。

#### 4.2.3 源码精读

[`_utils.py:L4-L21`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_utils.py#L4-L21) —— 整个函数全文：

```python
def _raise_warning(attr: str, submodule: str | None = None) -> None:
    new_module = "numpy._core"
    old_module = "numpy.core"
    if submodule is not None:
        new_module = f"{new_module}.{submodule}"
        old_module = f"{old_module}.{submodule}"
    warnings.warn(
        f"{old_module} is deprecated and has been renamed to {new_module}. "
        "The numpy._core namespace contains private NumPy internals and its "
        "use is discouraged, as NumPy internals can change without warning in "
        "any release. In practice, most real-world usage of numpy.core is to "
        "access functionality in the public NumPy API. If that is the case, "
        "use the public NumPy API. If not, you are using NumPy internals. "
        "If you would still like to access an internal attribute, "
        f"use {new_module}.{attr}.",
        DeprecationWarning,
        stacklevel=3
    )
```

关注三个要点：

- **子模块分支**（[`L7-L9`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_utils.py#L7-L9)）：传了 `submodule`，消息里就出现 `numpy.core.numeric` / `numpy._core.numeric`；不传就是 `numpy.core` / `numpy._core`。这决定了「消息文本里一定包含 `numpy._core`」——这是综合实践里要断言的事实。
- **类别固定为 `DeprecationWarning`**（[`L19`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_utils.py#L19)）：所以捕获后 `w.category is DeprecationWarning` 一定成立。
- **`stacklevel=3`**（[`L20`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_utils.py#L20)）：这是「往上数 3 层」。从 `warnings.warn` 这一帧开始数：第 1 层是 `_raise_warning` 自己，第 2 层是调用它的 `__getattr__`，第 3 层是**调用 `__getattr__` 的用户代码**。于是警告的 `.filename` / `.lineno` 会指向**你写的 `numpy.core.numeric.zeros` 那一行**，而不是 numpy 内部。综合实践会验证这一点。

> 小贴士：`stacklevel` 的具体数值是和「调用链有几层」绑死的。如果将来 numpy 在 `__getattr__` 和 `_raise_warning` 之间多套了一层函数，`stacklevel` 就得同步加 1，否则警告位置会指错。这是一个「看起来不起眼、改错就误导用户」的细节，第 2 单元 u2-l3 会专门讲。

#### 4.2.4 代码实践

我们来验证 `_raise_warning` 的「子模块分支」对消息的影响：传 `submodule` 和不传，消息里的旧/新名字不一样。

1. **实践目标**：直接调用 `_raise_warning`，对比传与不传 `submodule` 时消息文本的差异，并确认消息里一定有 `numpy._core`。
2. **操作步骤**：新建 `inspect_message.py`：

   ```python
   import warnings
   from numpy.core._utils import _raise_warning   # 直接借用 numpy 的工具函数

   with warnings.catch_warnings(record=True) as w:
       warnings.simplefilter("always")
       _raise_warning("zeros", "numeric")          # 子模块层
       _raise_warning("numeric")                   # 包级层（不传 submodule）

   for i, msg in enumerate(w):
       text = str(msg.message)
       print(f"--- 警告 {i} ---")
       print("类别:", msg.category.__name__)
       print("开头:", text.split('.')[0])          # 只看第一句，聚焦新旧名字
       print("包含 'numpy._core':", "numpy._core" in text)
   ```

3. **需要观察的现象**：两条警告类别都是 `DeprecationWarning`；第一条开头提到 `numpy.core.numeric` 与 `numpy._core.numeric`，第二条开头提到 `numpy.core` 与 `numpy._core`；两者都包含 `numpy._core`。
4. **预期结果**：与 `_utils.py` 的字符串拼接完全吻合——子模块分支确实给名字加了 `.numeric` 后缀。这印证了 4.2.3 的源码分析。
5. **待本地验证**：`_raise_warning` 是私有工具，直接 `import` 它仅用于学习观察；不同 numpy 小版本消息措辞可能微调，以你本地实际文本为准，但「包含 `numpy._core`」应当稳定成立。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_raise_warning` 把 `new_module` / `old_module` 拼成带 `submodule` 后缀的形式，而不是在消息里直接写死 `numpy.core.numeric`？

> **参考答案**：因为它要被**所有**子模块垫片复用（`numeric.py`、`umath.py`、`records.py` ……）。把「子模块名」参数化，同一个函数就能为任意子模块生成对应的消息，避免在每个垫片里复制粘贴一段几乎相同的文本。这就是把它抽成「工具文件」的意义（见 u1-l2 的分类）。

**练习 2**：如果有人误把 `stacklevel=3` 改成 `stacklevel=1`，捕获到的警告的 `.filename` 会指向哪里？

> **参考答案**：会指向 `_utils.py` 里 `warnings.warn(...)` 那一行（即 numpy 自己的源码），而不是用户代码。这样用户看到警告时，定位不到「是我哪一行触发的」，弃用提示的可用性就大打折扣。这正是 `stacklevel` 存在的意义。

---

### 4.3 `numeric.__getattr__`：转发并报警，且「只提示不阻断」

#### 4.3.1 概念说明

4.2 讲了「警告怎么发」，但真正在访问 `numpy.core.numeric.zeros` 时**触发** `_raise_warning` 的，是子模块垫片 [`numeric.py`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/numeric.py) 里的 `__getattr__`。它是 u1-l2 里「纯转发垫片」的代表（用 sentinel 写法）。

这里要重点理解两件事：

1. **两层转发**（承接 u1-l1）：访问 `numpy.core.numeric.zeros` 涉及两次属性查找。
   - 先找 `numpy.core` 上的 `numeric`：由包级 [`__getattr__`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/__init__.py#L30-L33) 处理，返回 `numpy._core.numeric`（真实现），并报警一次（`_raise_warning("numeric")`，不带 submodule）。
   - 再找 `numeric` 上的 `zeros`：如果你事先 `import numpy.core.numeric` 把**垫片**加载进了 `sys.modules`，那么这里的 `numeric` 是垫片 `numeric.py`，`.zeros` 会触发**垫片**的 `__getattr__`，报警一次（`_raise_warning("zeros", "numeric")`，带 submodule）。
2. **非阻断**：`__getattr__` 在 `_raise_warning(...)` 之后**仍然 `return ret`**。报警只是「顺便」，不会替代返回值。

> 名词解释：**sentinel（哨兵）** 是一个「独一无二、绝不与任何真实值相等」的对象，这里用 `sentinel = object()` 现场造一个。它比用 `None` 当默认值更安全——因为真实属性里万一有值为 `None` 的，用 `None` 就会误判「不存在」。`numeric.py` 用 sentinel、其他 13 个垫片用 `None`，这个差异是 u2-l2 的主题。

#### 4.3.2 核心流程

垫片 `numeric.__getattr__(attr_name)` 的执行步骤（假定垫片已被 `import numpy.core.numeric` 加载）：

1. 惰性 `from numpy._core import numeric`：拿到真实现子模块（写在函数体内，按需加载）。
2. 惰性 `from ._utils import _raise_warning`：拿到报警工具。
3. `sentinel = object()`，`ret = getattr(numeric, attr_name, sentinel)`：去真实现里取属性，取不到就用哨兵占位。
4. `if ret is sentinel:` 抛 `AttributeError`（属性确实不存在，这是「真错误」，不是警告）。
5. 否则 `_raise_warning(attr_name, "numeric")` 报弃用警告，然后 `return ret`。

#### 4.3.3 源码精读

[`numeric.py:L1-L12`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/numeric.py#L1-L12) —— 整个垫片就这一个函数：

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

逐段对应 4.3.2 的流程：

- [`L2`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/numeric.py#L2) / [`L4`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/numeric.py#L4)：两个 `import` 都写在函数体内，所以是惰性的（只有真正访问属性才执行）。这正是「每次访问都会重新走一遍 `__getattr__`、都会报警」的关键。
- [`L6-L7`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/numeric.py#L6-L7)：sentinel 写法。
- [`L8-L10`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/numeric.py#L8-L10)：属性不存在时抛 **`AttributeError`（异常）**。注意这里用的是异常而非警告——「真不存在」属于「出错」，要按异常处理，和「废弃但存在」的警告形成对照（见 2.1 的表格）。
- [`L11-L12`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/numeric.py#L11-L12)：**报警 + 仍然返回**。这两行是「只提示不阻断」的代码级体现。

#### 4.3.4 代码实践

验证「报警不阻断」：在发出 `DeprecationWarning` 的同时，`numpy.core.numeric.zeros` 仍然是一个**可调用**的真实函数。

1. **实践目标**：证明警告和「正常返回」是同时发生的——`zeros` 被取到，而且能被调用。
2. **操作步骤**：新建 `non_blocking.py`：

   ```python
   import warnings
   import numpy.core.numeric          # 加载垫片，确保 .zeros 走垫片 __getattr__

   with warnings.catch_warnings(record=True) as caught:
       warnings.simplefilter("always")
       zeros = numpy.core.numeric.zeros      # 这里会触发弃用警告
       result = zeros(3)                     # 但仍然能正常调用

   print("捕获到", len(caught), "条警告")
   print("zeros 类型:", type(zeros).__name__)
   print("调用结果:", result)
   ```

3. **需要观察的现象**：捕获到 1 条 `DeprecationWarning`；同时 `zeros` 是一个 `builtin_function_or_method`，`zeros(3)` 返回 `array([0., 0., 0.])`。
4. **预期结果**：警告与功能**同时**成立——`[DeprecationWarning]` 出现，`result` 是全零数组。这就是「垫片只提示、不阻断」的含义。
5. **待本地验证**：若你只写了 `import numpy`（没有 `import numpy.core.numeric`），那么 `.zeros` 访问到的可能是真实现模块、不再触发垫片的 `__getattr__`，捕获到的 1 条警告会来自包级 `__getattr__`（消息是关于 `numpy.core.numeric` 整个子模块的）。两种情况下「类别是 `DeprecationWarning`、消息含 `numpy._core`」都成立，综合实践会专门讨论这个区别。

#### 4.3.5 小练习与答案

**练习 1**：访问一个 `numpy._core.numeric` 里**不存在**的名字（比如 `numpy.core.numeric.no_such_thing`），会发生什么？是警告还是异常？

> **参考答案**：是 **`AttributeError`（异常）**。因为 [`numeric.py:L8-L10`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/numeric.py#L8-L10) 在 `ret is sentinel` 时直接 `raise AttributeError`，**不会**走到 `_raise_warning`。这体现了 2.1 的对照：「真不存在」按异常处理，「废弃但存在」才按警告处理。

**练习 2**：为什么 `numeric.py` 把 `from numpy._core import numeric` 写在 `__getattr__` 函数体内部，而不是写在文件顶部？

> **参考答案**：为了**惰性**和**每次报警**。如果写在顶部，那么 `import numpy.core.numeric` 就会立刻把真实现 `numpy._core.numeric` 拉进来；更重要的是，把对象提前绑定成模块属性后，访问 `.zeros` 就**不再**落入 `__getattr__`，弃用警告就被「吃掉」了。写在函数体内，才能保证「每次访问都重新走 `__getattr__`、每次都报警」。（这一动机也是 u1-l2 讲过的「`__all__` 强制惰性加载」的子模块版体现。）

---

## 5. 综合实践：写一个捕获并断言 `DeprecationWarning` 的测试函数

把 4.1–4.3 串起来，完成本讲规格要求的实践：**写一个测试函数，用 `warnings.catch_warnings(record=True)` 捕获访问 `numpy.core.numeric.zeros` 时产生的 `DeprecationWarning`，断言它属于 `DeprecationWarning` 类别，并且消息文本里包含 `"numpy._core"`。**

在此基础上，我们再加两个「进阶断言」，把本讲讲过的关键点都验证一遍。

1. **实践目标**：用一段可复用的测试代码，一次性验证四件事——
   - 访问 `numpy.core.numeric.zeros` 会产生警告；
   - 警告类别是 `DeprecationWarning`；
   - 消息文本包含 `numpy._core`；
   - （进阶 a）`stacklevel=3` 让警告的 `filename` 指向**本测试文件**；
   - （进阶 b）警告「只提示不阻断」，`zeros` 仍可正常调用。
2. **操作步骤**：新建 `test_u1_l3_warning.py`：

   ```python
   # 示例代码：test_u1_l3_warning.py
   import warnings
   import numpy.core.numeric   # 关键：先加载垫片，确保 .zeros 走 numeric.__getattr__

   # 记录“触发警告的那一行”所在的文件名，供 stacklevel 断言使用
   THIS_FILE = __file__

   def test_zeros_triggers_deprecation_warning():
       with warnings.catch_warnings(record=True) as caught:
           warnings.simplefilter("always")          # 必须在 with 内部
           zeros = numpy.core.numeric.zeros         # ← 触发警告的那一行
           result = zeros(3)                        # 验证非阻断

       # —— 基础断言 1：至少捕获到一条警告 ——
       assert len(caught) >= 1, "没有捕获到任何警告"

       # 只看 DeprecationWarning 这一类
       deps = [w for w in caught if issubclass(w.category, DeprecationWarning)]
       assert len(deps) >= 1, f"捕获到的都不是 DeprecationWarning: {[w.category for w in caught]}"
       w = deps[0]

       # —— 基础断言 2：消息里包含 numpy._core ——
       msg = str(w.message)
       assert "numpy._core" in msg, msg

       # —— 进阶断言 a：stacklevel=3 应让警告指向本文件 ——
       assert w.filename == THIS_FILE, f"警告位置异常: {w.filename}"

       # —— 进阶断言 b：非阻断，zeros 仍可调用 ——
       assert result.tolist() == [0.0, 0.0, 0.0], result

       # 打印出来供肉眼观察
       print("类别:", w.category.__name__)
       print("位置:", f"{w.filename}:{w.lineno}")
       print("消息前 80 字:", msg[:80], "...")
       print("调用结果:", result)

   if __name__ == "__main__":
       test_zeros_triggers_deprecation_warning()
       print("ALL CHECKS PASSED")
   ```

   运行 `python test_u1_l3_warning.py`。

3. **需要观察的现象**：
   - 没有任何 `AssertionError`，最后打印 `ALL CHECKS PASSED`。
   - 「位置」一行打印的是 `test_u1_l3_warning.py` 加某个行号，且该行号应**指向 `zeros = numpy.core.numeric.zeros` 那一行**（而不是 numpy 内部文件）——这正是 `stacklevel=3` 的效果。
   - 「消息前 80 字」里能看到 `numpy.core.numeric is deprecated and has been renamed to numpy._core.numeric` 之类的内容。
4. **预期结果**：四组断言全部通过，印证——
   - （基础）警告存在、类别正确、消息含 `numpy._core`；
   - （进阶 a）`stacklevel=3` 把警告归因到调用者代码；
   - （进阶 b）报警与功能并存，垫片「只提示不阻断」。
5. **待本地验证**：
   - 若你**省略** `import numpy.core.numeric` 那一行（只写 `import numpy`），`numpy.core.numeric` 会经包级 `__getattr__` 解析成真实现模块，于是 `.zeros` 不再触发**垫片** `__getattr__`。此时「基础断言 1/2」**仍然通过**（包级 `__getattr__` 也会发一条含 `numpy._core` 的 `DeprecationWarning`），但「进阶 a 的位置」和「消息前 80 字」会变成关于**子模块整体**的那条警告。你可以故意删掉那一行再跑一次，对比两种情形，加深对 4.3.1「两层转发」的理解。
   - 具体行号、消息措辞依 NumPy 版本而定，以本地实际输出为准。

> 思考题（不必写代码）：如果某天 numpy 的 `numeric.__getattr__` 在调用 `_raise_warning` 之前多包了一层内部辅助函数，综合实践里「进阶断言 a」会怎样失败？该如何修复？（提示：与 [`_utils.py:L20`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_utils.py#L20) 的 `stacklevel` 有关，详见 u2-l3。）

## 6. 本讲小结

- 警告（Warning）和异常（Exception）不同：警告**不中断**程序，访问 `numpy.core` 的属性时「报警」和「正常返回」是同时发生的。
- `DeprecationWarning` 在 Python 默认警告过滤器下**常常被静默**（尤其不在 `__main__` 时），所以「没看到警告」不等于「没触发」。
- 可靠捕获的标准姿势是 `with warnings.catch_warnings(record=True) as caught:`，并且**在 `with` 内部**调用 `warnings.simplefilter("always")`——这一点最容易写错。
- [`_raise_warning`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_utils.py#L4-L21) 把弃用信息拼成固定文本，类别固定为 `DeprecationWarning`，消息里**一定包含 `numpy._core`**；它的 `stacklevel=3` 让警告指向用户代码而非 numpy 内部。
- [`numeric.__getattr__`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/numeric.py#L1-L12) 是「转发 + 报警 + 仍然返回」的子模块层；属性**真不存在**时它抛 `AttributeError`（异常），**废弃但存在**时才走 `_raise_warning`（警告）。
- 综合实践里的测试函数，把「类别断言」「消息断言」「stacklevel 位置断言」「非阻断断言」四件事一次性验证清楚，可作为日后观测任何弃用警告的模板。

## 7. 下一步学习建议

本讲你已经能把弃用警告「跑出来、抓得住、断言得了」，第 1 单元的「建立全局认知与可观测性」就完成了。接下来进入第 2 单元，深入机制内部：

- **u2-l1（模块级 `__getattr__` 与惰性转发）**：本讲一直说「访问属性会进入 `__getattr__`」，但 Python **何时、为何**调用模块级 `__getattr__`？惰性导入如何避免一次性加载全部子模块？这些原理在 u2-l1 展开。
- **u2-l2（sentinel 与 None 两种委派写法）**：本讲提到 `numeric.py` 用 sentinel、其他 13 个垫片用 `None`。`None` 写法在面对「假但有效」的属性（如 `0`、`""`）时有什么陷阱？u2-l2 会用对比实验讲透。
- **u2-l3（`_raise_warning` 与 `stacklevel`）**：本讲只是「用」了 `stacklevel=3`，u2-l3 会深入讲解它的计算逻辑、为什么是 3 而不是 2，以及改错会怎样误导用户。

> 阅读提示：第 2 单元会频繁回到 [`_utils.py`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/_utils.py) 和各垫片 [`__getattr__`](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/core/numeric.py#L1-L12)。本讲建立的「警告可被 `catch_warnings` 捕获、可按类别与消息断言」的能力，是后续验证任何机制改动的实验基础。
