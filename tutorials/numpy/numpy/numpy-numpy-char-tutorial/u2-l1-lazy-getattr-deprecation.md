# 惰性加载 \_\_getattr\_\_ 与弃用警告

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 `numpy/char/__init__.py` 为什么只有 31 行，却能「凭空」提供 50 多个字符串函数；
- 讲清 **模块级 `__getattr__`** 的触发时机（属性 miss 才触发）、以及它为什么在这里被用来「拦截」访问；
- 读懂 `__DEPRECATED` 集合如何在访问 `chararray`/`array`/`asarray` 时发出 `DeprecationWarning`，而又不阻断访问；
- 解释 `stacklevel=2`、局部 `import`、`frozenset` 这些写法的用意；
- 说明 `__dir__` 为什么要委托给底层 `defchararray`，它和 `__getattr__` 是什么关系；
- 写出一段用 `warnings.catch_warnings(record=True)` 精确捕获弃用警告的测试代码。

## 2. 前置知识

本讲建立在第一单元（u1-l1）已经建立的「**char（门面）→ defchararray（实现）→ strings（现代 ufunc）**」三层模型之上。如果你还不清楚这三层，请先读 u1-l1。此外还需要：

- **Python 模块的属性查找顺序**：访问 `模块.名字` 时，解释器先查模块的 `__dict__`（模块命名空间），命中就直接返回；查不到时，才会走其它机制。
- **PEP 562：模块级 `__getattr__` 与 `__dir__`**（Python 3.7+）。和「类里的 `__getattr__`」不同，PEP 562 允许在一个 `.py` 文件里直接定义一个**模块级函数** `__getattr__(name)`：当某个名字在模块 `__dict__` 里找不到时，解释器就会调用它。同理可以定义 `__dir__()` 来控制 `dir(模块)` 的结果。本讲几乎全部内容都围绕这两个函数。
- **`warnings` 模块基础**：`warnings.warn(message, category, stacklevel=...)` 发出一条警告；`DeprecationWarning` 是「此功能将来会移除」的专用类别；`warnings.catch_warnings(record=True)` 配合 `warnings.simplefilter("always")` 可以在测试里可靠地捕获警告。一个关键坑：**Python 默认会过滤掉 `DeprecationWarning`**（除非它在 `__main__` 里触发），所以测试时必须显式 `simplefilter("always")` 才能稳定捕获。
- **海象运算符 `:=`**：Python 3.8+ 的「赋值表达式」，`(x := expr)` 在表达式内部完成赋值，本讲的 `__getattr__` 用它把「取值」和「判空」合并成一行。

如果你对前三条已经熟悉，本讲的重点在第 4 节。

## 3. 本讲源码地图

本讲几乎只盯一个文件，但需要和它「转发」的目标对照着看：

| 文件 | 作用 | 本讲用到的部分 |
|------|------|------|
| `numpy/char/__init__.py` | 公共入口：惰性转发 + 弃用拦截，仅 31 行 | **全部**（整篇精读） |
| `numpy/_core/defchararray.py` | 真正的实现：`__all__`、各字符串函数、`chararray` 类、`array`/`asarray` 工厂 | 仅看顶部的 `__all__`（被 char 转发的「名字清单」来源） |

一句话定位：`numpy/char/__init__.py` 是一个**薄门面（facade）**。它不定义任何字符串函数，只做两件事——

1. 把对 `np.char.<任意名字>` 的访问，**转发**给 `numpy._core.defchararray`；
2. 在转发时，对少数几个「已弃用」的名字**额外发一条 `DeprecationWarning`**。

这两件事，分别由模块级 `__getattr__`（负责「转发 + 拦截」）和 `__dir__`（负责「告诉外界有哪些名字」）完成。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**模块入口与惰性 `__getattr__`**、**弃用机制（`__DEPRECATED` + 警告）**、**`__dir__` 的委托**。

### 4.1 模块入口与惰性 `__getattr__`

#### 4.1.1 概念说明

一个 Python 模块要对外提供 `foo` 这个名字，最普通的写法是在文件顶部写 `from xxx import foo`（或 `def foo(...)`）。这样 `foo` 在 `import` 时就被绑定进模块的 `__dict__`，访问时直接命中。

但 `numpy/char/__init__.py` 反其道而行：它的顶部**只**导入了两个名字——`__all__` 和 `__doc__`：

```python
from numpy._core.defchararray import __all__, __doc__
```

[numpy/char/\_\_init\_\_.py:1](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/char/__init__.py#L1) —— 仅导入 `__all__`（公开名字清单）与 `__doc__`（模块文档字符串），**不**导入 `upper`/`add`/`chararray` 这些函数本身。

那么 `np.char.upper` 是怎么拿到的？答案就是下一行下面定义的模块级 `__getattr__`：当你访问 `np.char.upper` 时，解释器发现 `upper` 不在 `__dict__` 里（因为顶部没导入它），于是转而调用 `__getattr__('upper')`，由它去底层 `defchararray` 里把 `upper` 取出来返回。

这里有一个**容易被误解的点**：这种「惰性」写法在这里**主要不是为了省导入开销**。因为顶部那一行 `from numpy._core.defchararray import __all__, __doc__` 已经把整个 `defchararray` 模块（连带它 `from numpy.strings import *` 引入的现代层）加载进来了。所以 `import numpy.char` 时，`defchararray` 早就躺在 `sys.modules` 里了。

那为什么还要用 `__getattr__`？真正的动机有两个：

1. **单一事实来源（single source of truth）**：char 的命名空间不需要手工维护一份「转发清单」，而是**永远镜像** `defchararray` 导出了什么。`defchararray` 加一个函数，char 立刻就能访问；删一个，char 也跟着没有。
2. **拦截（interception）**：这是更关键的动机。如果用普通的 `from defchararray import chararray`，那么 `np.char.chararray` 会直接命中、毫无机会插入警告。只有把所有访问都收拢进 `__getattr__` 这个「咽喉要道」，才能在「返回对象之前」对特定名字打一个日志或发一条 `DeprecationWarning`。

换句话说，`__getattr__` 在这里扮演的是**访问钩子（access hook）**，而不仅仅是「懒加载器」。

#### 4.1.2 核心流程

访问 `np.char.<name>` 的完整流程（伪代码 + 文字流程图）：

```
np.char.<name>
   │
   ▼
在模块 __dict__ 里找 <name> 吗？
（__dict__ 里有：__all__, __doc__, __DEPRECATED, __getattr__, __dir__, 以及 __name__ 等 dunder）
   │ 找到 ──────────────────────► 直接返回（根本不调用 __getattr__）
   │ 没找到
   ▼
调用模块级 __getattr__(name)
   │
   ├─ name ∈ __DEPRECATED ?
   │      是 ─► warnings.warn(..., DeprecationWarning, stacklevel=2)
   │
   ├─ import numpy._core.defchararray as char   # 绑定到已在 sys.modules 中的模块
   ├─ (export := getattr(char, name, None))     # 用海象运算符取值并判空
   │
   ├─ export is not None ─► return export       # 即便上面 warn 过，也照常返回对象
   └─ export is None      ─► raise AttributeError(...)
```

几个要点先记在心里，下面逐条对着源码讲：

- `__getattr__` **只在 miss 时触发**。`np.char.__all__`、`np.char.__doc__` 这类名字已经在 `__dict__` 里，不会进 `__getattr__`，也不会有任何弃用判断。
- 即使命中了弃用分支，函数也**不 `raise`**，而是**继续往下把对象返回**。这是一种「软弃用」。
- 不存在的名字最终 `raise AttributeError`，所以 `hasattr(np.char, 'xxx')` 能正确返回 `False`。

#### 4.1.3 源码精读

先看 `__getattr__` 的全貌（21 行，是整个文件的主体）：

```python
def __getattr__(name: str):
    if name in __DEPRECATED:
        # Deprecated in NumPy 2.5, 2026-01-07
        import warnings

        warnings.warn(
            (
                "The chararray class is deprecated and will be removed in a future "
                "release. Use an ndarray with a string or bytes dtype instead."
            ),
            DeprecationWarning,
            stacklevel=2,
        )

    import numpy._core.defchararray as char

    if (export := getattr(char, name, None)) is not None:
        return export

    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
```

[numpy/char/\_\_init\_\_.py:6-25](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/char/__init__.py#L6-L25) —— 模块级 `__getattr__`：先判断是否弃用，再转发给 `defchararray`，取不到就 `raise AttributeError`。

逐段拆解：

**① 弃用判断（4.2 节细讲）**

[numpy/char/\_\_init\_\_.py:7-8](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/char/__init__.py#L7-L8) —— `if name in __DEPRECATED:` 命中才进警告分支；注意这里**只 warn，不 return**，warn 完会继续往下走，保证对象依然被返回。

**② 转发：取底层模块的属性**

```python
import numpy._core.defchararray as char

if (export := getattr(char, name, None)) is not None:
    return export
```

[numpy/char/\_\_init\_\_.py:20-23](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/char/__init__.py#L20-L23) —— 这是「转发」的核心。

值得注意的细节：

- `import numpy._core.defchararray as char` 写在函数**内部**。因为顶部那行已经触发过 `defchararray` 的导入，这里只是把 `sys.modules` 里已有的模块**绑定到一个局部名** `char`，开销几乎为零。把它放在函数内、而非文件顶部，更多是**风格上的「显式」**——让读者一眼看出「转发目标是谁」，也让 `__getattr__` 自包含、便于阅读。
- `getattr(char, name, None)`：第三个参数 `None` 是「找不到时的默认值」。所以当用户访问一个不存在的名字（如 `np.char.nope`）时，这里不会抛 `AttributeError`，而是安静地返回 `None`。
- 海象运算符 `(export := ...)`：把「取值」和「判空」合二为一——既拿到 `export`，又顺便判断它是不是 `None`。
- 用 `is not None` 而非真值判断（`if export:`）：这是更稳健的写法。即便底层某个属性恰好是「假值但存在」（比如 `0`、`""`、`False`），`is not None` 也能正确放行；只有「确实没有」时才落到下面的 `raise`。

**③ 兜底：抛 AttributeError**

```python
raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
```

[numpy/char/\_\_init\_\_.py:25](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/char/__init__.py#L25) —— 当底层也没有这个名字时，主动 `raise AttributeError`，并带上友好的错误信息。这一步至关重要：正是它让 `hasattr(np.char, 'nope')` 返回 `False`，让 `np.char.nope` 抛出符合 Python 惯例的 `AttributeError`，而不是返回一个莫名其妙的 `None`。

> 小贴士：`__getattr__` 里**没有**把取到的对象写回 `globals()[name] = export`。这意味着每次访问 `np.char.upper` 都会重跑一遍 `__getattr__`。这是一种「惰性但不缓存」的设计。由于内部 `import` 命中的是 `sys.modules` 缓存、`getattr` 也是字典查找，单次开销极低，所以这个取舍是可以接受的。

#### 4.1.4 代码实践

**实践目标**：亲手验证「`__getattr__` 只在 miss 时触发」，并看清它在「正常名 / 不存在的名」上的不同行为。

**操作步骤**：

新建一个 `probe_getattr.py`（示例代码，你可以放在任意目录运行）：

```python
# 示例代码：探查 numpy.char 的 __getattr__ 行为
import numpy.char as ch

# 1) 这三个名字已经在模块 __dict__ 里，访问它们【不会】调用 __getattr__
print("__all__ 在 __dict__ 里吗:", "__all__" in ch.__dict__)      # True
print("__doc__ 在 __dict__ 里吗: ", "__doc__" in ch.__dict__)     # True

# 2) upper 没有在 __dict__ 里 —— 它必须靠 __getattr__ 转发才能拿到
print("upper 在 __dict__ 里吗:   ", "upper" in ch.__dict__)      # False
print("np.char.upper 是:", ch.upper)                              # <function ...>

# 3) 访问一个【不存在】的名字：__getattr__ 取不到，抛 AttributeError
try:
    ch.totally_not_a_function
except AttributeError as e:
    print("AttributeError:", e)

# 4) 因为第 3 步抛了 AttributeError，hasattr 返回 False（没有误报警告）
print("hasattr(ch, 'totally_not_a_function'):", hasattr(ch, "totally_not_a_function"))
```

**需要观察的现象**：

- 第 1、2 步印证：`__all__`/`__doc__` 命中 `__dict__`，`upper` 不命中、靠转发。
- 第 3 步抛出的 `AttributeError` 文本应是 `module 'numpy.char' has no attribute 'totally_not_a_function'`，且信息里出现的是 `'numpy.char'`（因为 `__name__` 在 char 这一层）。
- 第 4 步 `hasattr` 返回 `False`，且全程**没有任何 `DeprecationWarning`**（因为 `totally_not_a_function` 不在 `__DEPRECATED` 里）。

**预期结果**：第 4 步输出 `False`，控制台不打印任何 `DeprecationWarning`。

> 待本地验证：不同 NumPy 小版本的 `__doc__` 文本可能略有差异，但上述布尔结果与异常类型是稳定的。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `np.char.__all__` 的访问不会进入 `__getattr__`？如果在文件里把第 1 行的 `from ... import __all__, __doc__` 删掉，`np.char.__all__` 还能正常拿到吗？

> **参考答案**：因为 `__all__` 已通过第 1 行绑定进模块 `__dict__`，属性查找直接命中、不会触发「只在 miss 时调用」的 `__getattr__`。删掉那行后，`__all__` 不在 `__dict__` 里，会改由 `__getattr__` 转发——底层 `defchararray` 里有 `__all__`，所以仍能拿到，但走的是另一条路径（且若该名字在 `__DEPRECATED` 中还会触发警告，当然 `__all__` 不在其中）。

**练习 2**：`np.char.upper` 每次访问都要重跑 `__getattr__`，这是否会成为性能瓶颈？为什么 NumPy 仍然这么设计？

> **参考答案**：不会成为有意义瓶颈。`__getattr__` 内部是一次命中 `sys.modules` 缓存的 `import` 加一次属性字典查找，开销极低。NumPy 选择「不缓存」换取的是命名空间**永远与底层 `defchararray` 一致**（单一事实来源）以及对访问的**拦截能力**（注入弃用警告），这两点比「省一次字典查找」重要得多。

---

### 4.2 弃用机制：`__DEPRECATED` 与 `DeprecationWarning`

#### 4.2.1 概念说明

NumPy 2.5（注释写明的日期是 2026-01-07）决定把 `chararray` 类以及 `array`/`asarray` 两个工厂函数标记为**弃用（deprecated）**——它们仍能用，但官方建议改用「普通 `str_`/`bytes_` dtype 数组 + 自由函数」或直接用 `numpy.strings`。

要在「访问属性」这个动作上挂一条警告，需要一个**统一的拦截点**。这正是上一节 `__getattr__` 的「第二动机」。具体怎么标记哪些名字弃用？答案是文件顶部的这个常量：

```python
__DEPRECATED = frozenset({"chararray", "array", "asarray"})
```

[numpy/char/\_\_init\_\_.py:3](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/char/__init__.py#L3) —— 用 `frozenset` 存放三个已弃用的名字，作为一份不可变的「弃用清单」。

几个设计选择值得一讲：

- **为什么用 `frozenset` 而不是 `set` 或 `tuple`？** 这是一份**配置常量**，不希望被运行时意外修改——`frozenset` 不可变，语义上更准确；同时 set 的成员判定 `name in __DEPRECATED` 是 O(1)，比遍历 `tuple`/`list` 更快。命名上加了前导双下划线 `__`，表示这是模块「私有」常量，不该被外部依赖。
- **为什么是「软弃用」？** 看 4.1.3 的源码：即便 `name in __DEPRECATED` 命中，函数也只是 `warn`，**并不 `return`、也不 `raise`**，随后照常把对象返回。所以 `np.char.chararray` 仍然能正常拿到这个类、甚至用它构造数组——只是控制台会多一条警告。这样旧代码不会立刻崩，给社区留出迁移时间。
- **警告类别选 `DeprecationWarning`**：这是 Python 标准库里「此功能将来会移除」的专用类别。注意 Python 默认会**隐藏** `DeprecationWarning`（除非触发点在 `__main__`），所以普通脚本里你可能看不到它——测试时必须显式打开（见 4.2.4）。

#### 4.2.2 核心流程

弃用警告的触发流程：

```
__getattr__(name)
   │
   ├─ name ∈ __DEPRECATED ?   （O(1) 集合查找）
   │      │ 否 ─► 跳过警告
   │      │ 是
   │      ▼
   │   import warnings              # 函数内局部导入（见下）
   │   warnings.warn(
   │       "The chararray class is deprecated ...",
   │       DeprecationWarning,
   │       stacklevel=2             # 让警告指向调用方那一行
   │   )
   │
   └─（继续往下转发并 return 对象，不阻断访问）
```

关于 `stacklevel=2`：`warnings.warn` 默认 `stacklevel=1`，会把警告的「文件名:行号」指向 `warn()` 调用本身——也就是 `numpy/char/__init__.py` 里那一行。这对用户毫无帮助。设成 `stacklevel=2` 则向上抬一帧，指向**调用 `__getattr__` 的地方**，也就是用户代码里写 `np.char.chararray` 的那一行。这样用户一眼就能看到「是我这行代码用了弃用功能」。

关于局部 `import warnings`：和 4.1.3 里 `import ... defchararray` 类似，`warnings` 是标准库、基本总会被提前加载，这里的局部 import 几乎零成本；放在 `if name in __DEPRECATED` 分支**内部**，意味着**只有走弃用路径时**才会绑定这个名字——非弃用路径（绝大多数日常访问）连这一步都省了，代码意图也更清晰。

#### 4.2.3 源码精读

```python
if name in __DEPRECATED:
    # Deprecated in NumPy 2.5, 2026-01-07
    import warnings

    warnings.warn(
        (
            "The chararray class is deprecated and will be removed in a future "
            "release. Use an ndarray with a string or bytes dtype instead."
        ),
        DeprecationWarning,
        stacklevel=2,
    )
```

[numpy/char/\_\_init\_\_.py:7-18](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/char/__init__.py#L7-L18) —— 弃用判断与发警告。注意三个细节：注释标明弃用版本与日期；`import warnings` 在分支内；`stacklevel=2` 指向调用方。

读这段源码时，注意一个**警告文案上的小瑕疵**（但符合预期）：不管访问的是 `chararray`、`array` 还是 `asarray`，文案统一写的是 *"The chararray class is deprecated..."*。也就是说，访问 `np.char.array` 时弹出的也是「chararray 类」字样。这是为了只用一条文案覆盖三个名字而做的简化，读者知道即可。

另外注意：`array` 和 `asarray` 这两个名字在 `numpy.strings` 里**并不存在**（它们是 `defchararray` 本地定义的工厂函数，详见 u3-l3），所以它们只会走「本地」+「弃用」这条路径；而 `chararray` 同样是 `defchararray` 本地的类。这解释了为什么 `__DEPRECATED` 恰好是这三个名字——它们都是「chararray 体系」的遗留物，而其余从 `numpy.strings` 转发来的函数并不弃用。

#### 4.2.4 代码实践

**实践目标**：用 `warnings.catch_warnings(record=True)` 精确捕获并断言——`np.char.upper` **不**触发 `DeprecationWarning`，而 `np.char.chararray` / `np.char.array` / `np.char.asarray` **各触发一次**。

**操作步骤**：

新建 `probe_deprecation.py`（示例代码）：

```python
# 示例代码：精确捕获 numpy.char 的弃用警告
import warnings
import numpy.char as ch


def access(attr: str):
    """访问 ch.<attr>，返回 (对象, 命中的 DeprecationWarning 列表)。"""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")          # 关键：默认会过滤 DeprecationWarning
        obj = getattr(ch, attr)
    deprecations = [w for w in caught
                    if issubclass(w.category, DeprecationWarning)]
    return obj, deprecations


# 1) upper 不在 __DEPRECATED 中：期望 0 条警告
_, upper_warns = access("upper")
assert upper_warns == [], "upper 不应触发任何弃用警告"
print(f"upper        -> 警告数 {len(upper_warns)}")

# 2) chararray / array / asarray 在 __DEPRECATED 中：期望各 1 条警告
for name in ("chararray", "array", "asarray"):
    obj, warns = access(name)
    assert len(warns) == 1, f"{name} 应触发一次 DeprecationWarning"
    w = warns[0]
    print(f"{name:11s} -> {w.category.__name__}: {str(w.message)[:48]}...")
    # 额外观察：stacklevel=2 是否让警告指向【本文件】而非 numpy/char/__init__.py
    print(f"             警告来源文件: {w.filename.split('/')[-1]}  行号: {w.lineno}")

print("全部断言通过 ✅")
```

**需要观察的现象**：

- `upper` 那行打印「警告数 0」；
- `chararray`/`array`/`asarray` 各打印一条 `DeprecationWarning`，文案都以 *"The chararray class is deprecated..."* 开头；
- 「警告来源文件」应**是本文件**（`probe_deprecation.py`）而不是 `__init__.py`，行号应指向 `obj = getattr(ch, attr)` 那一行——这正是 `stacklevel=2` 的效果。

**预期结果**：脚本最后打印 `全部断言通过 ✅`。如果去掉 `warnings.simplefilter("always")` 这一行，在某些入口下 `caught` 可能为空、断言失败——这恰好印证了「`DeprecationWarning` 默认被过滤」这一坑。

> 待本地验证：`w.lineno` 的确切数值取决于你脚本里的行排布，但「来源文件是调用方文件」这一结论是稳定的。

#### 4.2.5 小练习与答案

**练习 1**：为什么上面的测试脚本必须写 `warnings.simplefilter("always")`？不写会怎样？

> **参考答案**：Python 默认的警告过滤器会把 `DeprecationWarning`（以及 `PendingDeprecationWarning`）静默，除非它由 `__main__` 中的代码触发。虽然本脚本在 `__main__` 运行时通常能看到，但「同一条警告只显示一次」的机制也会让第二次访问静默。`simplefilter("always")` 强制「每次都记录」，保证 `record=True` 的列表可靠地收集到每一次警告，断言才稳定。

**练习 2**：访问 `np.char.chararray` 之后，还能用这个类创建数组吗？「弃用」和「移除」有什么区别？

> **参考答案**：能。`__getattr__` 只发警告、不阻断，依然 `return export`，所以 `np.char.chararray(["a", "b"])` 仍可工作（只是带一条 `DeprecationWarning`）。这是「软弃用（deprecated）」——功能还在、只是不推荐、将来会移除；而「移除（removed）」是彻底删掉、访问会直接 `AttributeError`。NumPy 现在处于前者，给社区留迁移窗口。

---

### 4.3 `__dir__` 的委托作用

#### 4.3.1 概念说明

`__getattr__` 解决了「**取**某个名字」的问题，但还有另一个问题：当用户敲 `dir(np.char)`、或者 IDE 做自动补全时，Python 怎么知道这个模块「**有哪些**名字」？

默认情况下，`dir(模块)` 返回的是模块 `__dict__` 里的键。但本模块的 `__dict__` 里**根本没有** `upper`/`add`/`center` 这些名字（它们靠 `__getattr__` 转发才存在）。所以如果不做处理，`dir(np.char)` 会是一份「残缺」的清单——只有 `__all__`、`__doc__`、`__DEPRECATED`、`__getattr__`、`__dir__` 和一些 dunder，看不到几十个字符串函数。这对自动补全和探索式编程极不友好。

PEP 562 同样提供了模块级 `__dir__()` 来补救：定义它之后，`dir(模块)` 会返回 `__dir__()` 的结果。本模块的做法非常直白——**直接返回底层的 `dir()`**：

```python
def __dir__() -> list[str]:
    import numpy._core.defchararray as char

    return dir(char)
```

[numpy/char/\_\_init\_\_.py:28-31](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/char/__init__.py#L28-L31) —— `__dir__` 把「有哪些名字」整体委托给底层 `defchararray`。

这样设计的妙处在于**一致性**：`__getattr__` 从 `defchararray`「取」名字，`__dir__` 从 `defchararray`「列」名字，两者指向**同一个源头**，所以「能 `dir` 出来的」和「能 `getattr` 到的」永远吻合，不会出现「列表里有但取不到」或「取得到但列表里没有」的尴尬。

`__dir__` 和 `__getattr__` 是一对分工明确的搭档：

| 函数 | 回答的问题 | 触发时机 | 本模块实现 |
|------|-----------|----------|-----------|
| `__getattr__(name)` | 「把这个名字给我」 | 属性在 `__dict__` 中 miss 时 | 转发 + 弃用拦截 |
| `__dir__()` | 「你都有哪些名字？」 | `dir()` / 自动补全时 | 直接返回 `dir(defchararray)` |

#### 4.3.2 核心流程

```
dir(np.char)
   │
   ▼
模块定义了 __dir__ ?   是
   ▼
调用 __dir__()
   │
   ├─ import numpy._core.defchararray as char
   └─ return dir(char)        # 返回底层模块的全部公开名 + dunder 等
```

注意 `dir(char)` 返回的列表会**包含** `chararray`/`array`/`asarray` 这三个弃用名（因为它们仍是 `defchararray` 的合法属性），也包含 `upper` 等所有转发名。所以「弃用」并不等于「从 `dir()` 里隐藏」——这一点在 4.4 综合实践里会用到。

#### 4.3.3 源码精读

`__dir__` 只有 3 行有效代码，已经在上文贴出（[numpy/char/\_\_init\_\_.py:28-31](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/char/__init__.py#L28-L31)）。要点：

- 返回类型标注 `list[str]`，与 PEP 562 对模块 `__dir__` 「返回字符串列表」的要求一致；
- `import ... as char` 同样是「绑定到已缓存模块」，零成本；
- 没有做任何过滤——既不过滤 dunder，也不过滤弃用名。这是一份「忠实镜像」。

对比一下 `defchararray` 自己的 `__all__`（被 char 第 1 行转发的「官方公开清单」）：

```python
__all__ = [
    'equal', 'not_equal', 'greater_equal', 'less_equal',
    'greater', 'less', 'str_len', 'add', 'multiply', 'mod', 'capitalize',
    ...
    'array', 'asarray', 'compare_chararrays', 'chararray'
    ]
```

[numpy/_core/defchararray.py:40-50](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L40-L50) —— `defchararray` 的 `__all__`，共 53 个公开名，结尾的 `array`/`asarray`/`chararray` 正是 char 里被标记弃用的三个。

可以看到 `__DEPRECATED` 的三个名字恰恰出现在 `__all__` 的尾部——它们是 `defchararray` 公开 API 的一部分，所以 `dir(np.char)` 也能列出它们；char 只是在「访问」时（而非「列举」时）给它们贴一条警告。

#### 4.3.4 代码实践

**实践目标**：对比「有 `__dir__`」和「假设没有 `__dir__`」两种情况下 `dir(np.char)` 的差异，体会 `__dir__` 的补全作用。

**操作步骤**：

```python
# 示例代码：观察 __dir__ 的作用
import numpy.char as ch

# 1) 实际的 dir(np.char)：因为定义了 __dir__，会返回底层 defchararray 的全部名字
public = [n for n in dir(ch) if not n.startswith("_")]
print("dir(np.char) 中非下划线名字数量:", len(public))
print("upper 在 dir 里吗:", "upper" in public)        # True
print("chararray 在 dir 里吗:", "chararray" in public)  # True（弃用 ≠ 隐藏）

# 2) 对照：模块 __dict__ 里实际只有极少数名字
in_dict = [n for n in ch.__dict__ if not n.startswith("_")]
print("模块 __dict__ 中非下划线名字:", in_dict)
# 期望大致是 ['__DEPRECATED', ...]（很少），upper / chararray 都不在里面
```

**需要观察的现象**：

- 第 1 步：`upper`、`chararray` 都出现在 `dir()` 结果里；
- 第 2 步：`ch.__dict__` 里的非下划线名字非常少（基本只有 `__DEPRECATED` 之类），远少于 `dir()` 的结果。两者之间的「差额」，正是靠 `__getattr__` 转发、靠 `__dir__` 暴露出来的那 50 多个名字。

**预期结果**：`dir(np.char)` 的非下划线名字数量明显大于 `__dict__` 的非下划线名字数量，且前者包含 `upper`/`add`/`chararray` 等，后者不包含。

> 待本地验证：`dir()` 返回的具体列表会随 NumPy 版本变化，但「`dir()` 比 `__dict__` 多」这一关系是稳定的。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `__dir__` 整个删掉，`dir(np.char)` 还会包含 `upper` 吗？对 IDE 自动补全有什么影响？

> **参考答案**：不会。没有 `__dir__` 时，`dir(np.char)` 只反映模块 `__dict__`，里面没有 `upper`，所以它不会出现。IDE 的自动补全通常依赖 `dir()`（或类型存根 `__init__.pyi`），删掉 `__dir__` 会导致补全丢失这些名字——尽管 `np.char.upper` 仍然能用（因为 `__getattr__` 还在）。这也是为什么本模块同时维护了 `__init__.pyi` 类型存根（见 u1-l1）作为静态侧的补充。

**练习 2**：为什么 `chararray` 仍然出现在 `dir(np.char)` 里？「弃用」为什么没有把它从列表里移除？

> **参考答案**：`__dir__` 直接返回 `dir(defchararray)`，而 `chararray` 仍是 `defchararray` 的合法属性，所以它仍被列出。本模块的弃用策略是「软弃用」：在**访问**时（`__getattr__`）发警告，而非在**列举**时隐藏。这样既保留了可发现性（用户能 `dir` 到、能查文档），又在真正使用时给出迁移提醒。

---

## 5. 综合实践

把本讲三个模块（`__getattr__` 转发、`__DEPRECATED` 拦截、`__dir__` 镜像）串起来，写一个**「numpy.char 弃用名自动分类器」**：用 `__dir__()` 列出所有公开名，逐个访问并捕获警告，自动把名字分成「弃用名」和「安全名」两组，并验证「弃用名」恰好等于 `__DEPRECATED`。

```python
# 示例代码：综合实践 —— 自动分类 numpy.char 的弃用名 / 安全名
import warnings
import numpy.char as ch


def classify():
    deprecated, safe = [], []
    # 用 __dir__() 拿到底层暴露的全部名字（4.3 节）
    for name in ch.__dir__():
        if name.startswith("__"):
            continue
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")          # 稳定捕获 DeprecationWarning
            try:
                getattr(ch, name)                    # 触发 __getattr__（4.1 节）
            except AttributeError:
                continue                             # 个别名字可能取不到，跳过
        if any(issubclass(w.category, DeprecationWarning) for w in caught):
            deprecated.append(name)                  # 命中 __DEPRECATED（4.2 节）
        else:
            safe.append(name)
    return deprecated, safe


deprecated, safe = classify()
print("弃用名:", sorted(deprecated))
print("安全名数量:", len(safe))

# 断言：弃用名集合应与 __DEPRECATED 完全一致
assert set(deprecated) == ch.__DEPRECATED, "分类结果与 __DEPRECATED 不一致！"
print("断言通过：弃用名集合 == __DEPRECATED ✅")
```

**预期结果**：`弃用名` 应为 `['array', 'asarray', 'chararray']`（顺序可能不同），且断言通过——这同时验证了三件事：

1. `__dir__()` 能列出这三个名字（4.3）；
2. 访问它们时 `__getattr__` 会发 `DeprecationWarning`（4.1 + 4.2）；
3. 访问其它名字时不发警告，分类正确（4.2）。

> 待本地验证：`safe` 的确切数量随版本浮动（取决于 `dir(defchararray)` 返回多少非下划线名），但 `deprecated` 恰为这三个名字、且等于 `__DEPRECATED` 是稳定结论。

**进阶思考**：如果你正在维护一个依赖 `numpy.char` 的老项目，可以把上面的 `classify()` 改造成一个简易「迁移扫描器」——扫描项目源码里所有 `np.char.<name>` 形式的调用，凡是命中 `__DEPRECATED` 的就标红提醒。这就把本讲的源码理解直接转化成了一个工程工具（迁移到 `numpy.strings` 的完整路径见 u3-l4）。

## 6. 本讲小结

- `numpy/char/__init__.py` 只有 31 行，顶部仅导入 `__all__` 和 `__doc__`，其余名字全部靠模块级 `__getattr__` **转发**给 `numpy._core.defchararray`。
- 模块级 `__getattr__`（PEP 562）**只在属性 miss 时触发**；它在这里的主要价值不是「省导入」，而是提供一个**统一的访问拦截点**——既能保证命名空间单一事实来源，又能对特定名字注入弃用警告。
- `__DEPRECATED = frozenset({"chararray", "array", "asarray"})` 标记了 NumPy 2.5（2026-01-07）弃用的三个名字；命中时发 `DeprecationWarning`，但**只 warn 不阻断**，对象照常返回（软弃用）。
- `stacklevel=2` 让警告指向**调用方那一行**；局部 `import warnings`/`import ... defchararray` 命中的是已缓存模块，开销可忽略，更多是表达意图。
- `__dir__()` 直接返回 `dir(defchararray)`，与 `__getattr__` 指向同一源头，保证「能列出来的」=「能取到的」，并让 IDE 自动补全正常工作。
- 不存在的名字会落到 `raise AttributeError`，使 `hasattr(...)` 行为符合 Python 惯例；测试捕获警告时必须用 `warnings.simplefilter("always")`，因为 `DeprecationWarning` 默认被过滤。

## 7. 下一步学习建议

本讲搞清楚了「char 这个门面如何转发访问、如何对遗留名字发警告」。接下来的学习路径：

- **u2-l2（从 numpy.strings 再导出）**：顺着本讲的「转发」往上游走，看 `defchararray` 里的 `from numpy.strings import *`，弄清哪些 char 函数其实就是 `numpy.strings` 的同一对象、哪些是 char 本地独有。
- **u2-l3（字符串比较运算符与 compare_chararrays）**：进入 `defchararray` 本地定义的函数细节，先看一组有「历史包袱」的比较函数。
- **u3-l4（弃用迁移：从 chararray 到 numpy.strings）**：本讲的弃用机制告诉你「哪些东西被弃用了」，u3-l4 则给出**怎么把老代码改写掉**的实操路径，是本讲弃用主题的自然归宿。
- 继续阅读建议：对照 [numpy/char/\_\_init\_\_.py](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/char/__init__.py) 这 31 行，把本讲讲到的每一条结论在源码里找到对应位置——这是巩固「门面 + 拦截 + 镜像」三件套最快的方法。
