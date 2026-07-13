# numpy.lib 的定位与整体架构

> 本讲是「numpy.lib 学习手册」的第一篇，面向**第一次接触 numpy 内部结构**的读者。
> 本讲不要求你已经读过任何 numpy 源码，但希望你写过 `import numpy as np` 并用过 `np.array`、`np.pad` 这类函数。

---

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清楚 **`numpy.lib` 是什么**：它在整个 numpy 里扮演什么角色、边界在哪里。
2. 打开 [`numpy/lib/__init__.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/__init__.py) 后，读懂三件事：
   - 模块开头的**说明性文档字符串**（docstring）；
   - 用 `__all__` 声明的**公开命名空间**；
   - 用 `__getattr__` 处理**已移除别名**的报错逻辑。
3. 区分两类源码文件：**对外暴露的薄再导出模块**（如 `npyio.py`、`format.py`）和**真正放实现的私有 `_impl` 模块**。
4. 写一段小脚本，遍历 `numpy.lib.__all__`，统计并核对公开成员的数量与来源。

本讲只读、不改源码。所有命令都是观察性的。

---

## 2. 前置知识

在进入源码前，先用大白话把几个 Python 包层面的概念讲清楚。如果你已经熟悉，可以跳过本节。

### 2.1 什么是包（package）和 `__init__.py`

把一个目录变成「Python 包」的关键，就是目录里有一个 `__init__.py` 文件。当你写 `import numpy.lib` 时，Python 实际执行的是 `numpy/lib/__init__.py` 这个文件。所以 **`__init__.py` 就是这个子包的「总入口」**：它决定了 `import numpy.lib` 之后，你能拿到哪些名字。

### 2.2 什么是 `__all__`

`__all__` 是一个**字符串列表**，它有两个作用：

- 控制 `from numpy.lib import *` 时，哪些名字会被导出；
- 充当一份「**这是公开 API**」的声明文档。

约定俗成：出现在 `__all__` 里的名字是**公开的**（public），用户可以放心用；以下划线 `_` 开头的名字是**私有的**（private），属于内部实现，随时可能变。

### 2.3 什么是模块级 `__getattr__`（PEP 562）

从 Python 3.7 起，模块也可以定义一个 `__getattr__` 函数。当你访问一个模块上**不存在**的属性时（例如 `numpy.lib.emath`），Python 不会立刻报错，而是先去调用这个模块的 `__getattr__('emath')`。

这是一个非常实用的钩子，常用于两件事：

- 给**已经移除/改名**的属性，返回一个**带迁移提示的错误信息**；
- 实现**懒加载**（用到时才真正导入）。

numpy.lib 用它来做第一件事：拦截那些在 NumPy 2.0 里被移除的旧别名，给出清晰的报错。

### 2.4 「再导出」是什么意思

有些模块自己不写实现，只写一行 `from ._xxx_impl import 某某函数`，把别处的函数「搬」到自己名下。这种模块叫**再导出模块**（re-export module）。它的好处是：实现可以藏在私有的 `_impl` 文件里，而对外只暴露一个干净、稳定的名字。

> 一句话总结本节：`__init__.py` 是总入口，`__all__` 是公开名单，`__getattr__` 是「找不到名字时的兜底处理」。本讲剩下的内容，就是看 numpy.lib 怎样具体实现这三样东西。

---

## 3. 本讲源码地图

本讲只聚焦两个文件，但会顺带提到它们和外部的关系：

| 文件 | 作用 | 本讲是否精读 |
| --- | --- | --- |
| `numpy/lib/__init__.py` | 子包总入口：文档说明、子模块导入、`__all__`、`__getattr__` | ✅ 精读 |
| `numpy/lib/__init__.pyi` | 类型存根（type stub），给静态类型检查器看的「公开 API 影子文件」 | ✅ 对比阅读 |
| `numpy/__init__.py`（顶层） | 把 lib 里的函数再往上搬到 `np.` 命名空间 | 仅作背景引用 |
| `numpy/lib/stride_tricks.py`、`numpy/lib/npyio.py` | 薄再导出模块的典型例子 | 仅举例，下一篇精读 |

> 阅读提示：本讲引用的行号都基于当前 HEAD `b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b`。每个关键代码点都会给出永久链接，点开即可对照。

---

## 4. 核心概念与源码讲解

本讲的最小模块有三个：

1. `__init__`——`__init__.py` 的整体结构与模块定位；
2. `__all__`——公开命名空间的声明；
3. `__getattr__`——已移除别名的兜底报错。

下面逐个展开。

### 4.1 `__init__.py` 与模块定位

#### 4.1.1 概念说明

`numpy.lib` 不是一个像 `linalg`（线性代数）或 `fft`（傅里叶变换）那样主题明确的子包。它更像一个**「杂项函数库」**：凡是「不属于 core、也不属于其它有明确用途子包」的函数，都收纳到这里。

这个定位不是我们猜的，而是源码**开头的文档字符串**白纸黑字写明的。读懂这段 docstring，是理解整个 lib 子包的第一步。

#### 4.1.2 核心流程

当解释器执行 `import numpy.lib` 时，`numpy/lib/__init__.py` 自上而下依次做这几件事：

1. 写一段 docstring，说明 lib 的定位（第 1–9 行）。
2. 从 numpy 底层 `_core` 引入两个工具：`add_docstring`、`tracemalloc_domain`，以及 `add_newdoc`（第 13–14 行）。
3. **批量导入一组子模块**（第 18–42 行），既包括私有的 `_xxx_impl`，也包括对外的 `format`、`npyio`、`stride_tricks` 等。这一步的真正目的不是「把名字搬到 lib 命名空间」，而是**触发这些子模块被加载**（因为有些副作用、注册逻辑需要在导入时完成）。
4. 把两个真正要放进 lib 命名空间的对象显式导入：`Arrayterator` 和 `NumpyVersion`（第 45–46 行）。
5. 声明 `__all__`（第 48–52 行，见 4.2）。
6. 注册测试入口 `test`（第 56–59 行）。
7. 定义 `__getattr__`（第 61–90 行，见 4.3）。

用一个伪代码流程图表示：

```text
import numpy.lib
   │
   ▼
执行 numpy/lib/__init__.py
   │
   ├── ① docstring：声明 lib 是「杂项函数库」
   ├── ② 引入 add_docstring / add_newdoc 等底层工具
   ├── ③ 批量导入子模块（副作用：加载实现代码）
   ├── ④ 显式导入 Arrayterator、NumpyVersion
   ├── ⑤ 定义 __all__（公开名单）
   ├── ⑥ 挂上 test() 测试入口
   └── ⑦ 定义 __getattr__（兜底报错）
```

#### 4.1.3 源码精读

先看**模块定位的 docstring**：

[numpy/lib/\_\_init\_\_.py:1-9](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/__init__.py#L1-L9)

```python
"""
``numpy.lib`` is mostly a space for implementing functions that don't
belong in core or in another NumPy submodule with a clear purpose
(e.g. ``random``, ``fft``, ``linalg``, ``ma``).

``numpy.lib``'s private submodules contain basic functions that are used by
other public modules and are useful to have in the main name-space.
"""
```

这段话把 lib 的定位讲得很直白：它是**「实现那些不属于 core、也不属于其它有明确用途子包（random/fft/linalg/ma）的函数」**的地方。第二段还点出一个重要事实：lib 的**私有子模块**里放了很多「被其它公开模块用到、且值得出现在主命名空间里」的基础函数。

接着看**批量导入子模块**这一段：

[numpy/lib/\_\_init\_\_.py:18-42](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/__init__.py#L18-L42)

```python
from . import (
    _arraypad_impl,
    _arraysetops_impl,
    _arrayterator_impl,
    _function_base_impl,
    _histograms_impl,
    _index_tricks_impl,
    _nanfunctions_impl,
    _npyio_impl,
    _polynomial_impl,
    _shape_base_impl,
    _stride_tricks_impl,
    _twodim_base_impl,
    _type_check_impl,
    _ufunclike_impl,
    _utils_impl,
    _version,
    array_utils,
    format,
    introspect,
    mixins,
    npyio,
    scimath,
    stride_tricks,
)
```

注意这里的两类成员：

- **以下划线开头**的 `_arraypad_impl`、`_function_base_impl` 等 —— 私有实现模块；
- **不以 `_` 开头**的 `format`、`npyio`、`stride_tricks`、`scimath`、`mixins`、`introspect`、`array_utils` —— 对外暴露的（薄）模块。

> 顺带一提：源码里有一句注释 `# Note: recfunctions is public, but not imported`，说明 `recfunctions`（结构化数组函数）虽然是公开模块，但**并不会在这里被自动导入**，需要用户自己 `import numpy.lib.recfunctions`。

再往下，是**真正放进 lib 命名空间的两个对象**：

[numpy/lib/\_\_init\_\_.py:45-46](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/__init__.py#L45-L46)

```python
from ._arrayterator_impl import Arrayterator
from ._version import NumpyVersion
```

这两行说明：`Arrayterator`（大数组分块迭代器）和 `NumpyVersion`（版本号比较工具）是 lib 想直接对外暴露的两个「类/对象」，所以单独导入。

#### 4.1.4 代码实践

**实践目标**：亲手确认 `import numpy.lib` 真的会执行上面这个 `__init__.py`，并能看到它的文档字符串与导入的子模块。

**操作步骤**：

1. 在能 `import numpy` 的环境里打开一个 Python 解释器（或写一个 `.py` 脚本）。
2. 依次执行下面三段代码。

```python
# 示例代码：观察 numpy.lib 的入口
import numpy.lib as lib

# (1) 打印模块定位文档
print(lib.__doc__)
```

```python
# (2) 看看 lib 命名空间里以 _impl 结尾的私有实现模块
impls = [n for n in dir(lib) if n.endswith("_impl")]
print(impls)
```

```python
# (3) 确认 Arrayterator 与 NumpyVersion 确实被导入了
print(lib.Arrayterator)
print(lib.NumpyVersion)
```

**需要观察的现象**：

- 第 (1) 步应打印出 4.1.3 里那段「mostly a space for implementing functions …」的英文说明。
- 第 (2) 步应能看到类似 `_arraypad_impl`、`_function_base_impl`、`_histograms_impl` 等私有模块名（因为第 18–42 行确实把它们 import 进来了）。
- 第 (3) 步应分别打印出 `Arrayterator` 类和 `NumpyVersion` 类的对象表示。

**预期结果**：以上三步都能正常输出，说明 `import numpy.lib` 触发了 `__init__.py`，且导入结构与源码一致。（本讲未在你机器上实际运行，如输出有出入请以你本地的为准。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `numpy.lib` 里既有 `_arraypad_impl`（带下划线）又有 `npyio`（不带下划线）两种命名？它们分别代表什么？

> **参考答案**：带下划线的 `_arraypad_impl` 是**私有实现模块**，用户一般不直接 import；不带下划线的 `npyio` 是**对外暴露的（薄）再导出模块**，是稳定 API 的一部分。这体现了 lib「实现藏在 `_impl`、对外只露薄模块」的分层设计。

**练习 2**：源码注释说 `recfunctions` 公开但不在 `__init__.py` 里导入。请写一行代码验证「它仍然可以被用户单独导入」。

> **参考答案**：执行 `import numpy.lib.recfunctions as rf`，再 `print(rf.merge_arrays)`，应能正常打印出函数对象。这说明「不在 `__init__` 自动导入」不等于「不可用」，只是不预加载。

---

### 4.2 `__all__` 公开命名空间

#### 4.2.1 概念说明

`__all__` 是 lib 子包对外宣称的**公开 API 清单**。它直接决定了 `from numpy.lib import *` 会把哪些名字带出来，也告诉类型检查器/IDE「哪些名字是稳定公开的」。

lib 的 `__all__` 有一个反直觉、但非常重要的事实：**它非常短**。绝大多数你熟悉的函数（`np.pad`、`np.histogram`、`np.percentile`……）都不在 `numpy.lib.__all__` 里。原因在 4.2.2 解释。

#### 4.2.2 核心流程

`__all__` 与「可访问名字」的关系：

```text
numpy.lib 命名空间里「能访问到的名字」
        │
        ├── 进入 __all__ 的：算公开 API，可被 `import *` 带出
        │     例：Arrayterator, NumpyVersion, format, npyio, ...
        │
        └── 没进 __all__ 但能访问的：多为私有子模块/工具
              例：_arraypad_impl, add_newdoc（虽然显式调了 __module__）, ...
```

关键认知：**lib 的 `__all__` ≠ numpy 顶层 `np.` 命名空间**。

- lib 把 pad、histogram 等函数的**实现**写在 `_arraypad_impl`、`_histograms_impl` 里；
- 顶层的 [`numpy/__init__.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/__init__.py) 再从这些 `_impl` 把函数往上搬到 `np.pad`、`np.histogram`。

例如顶层 `numpy/__init__.py` 里有这一行：

[numpy/\_\_init\_\_.py:456](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/__init__.py#L456)

```python
from .lib._arraypad_impl import pad
```

所以你平时用的 `np.pad`，**真正实现就在 `numpy/lib/_arraypad_impl.py`**，只是它不在 `numpy.lib.__all__` 里——它是「通过顶层 np. 暴露」而不是「通过 numpy.lib 暴露」。这一点是初学者最容易混淆的地方，务必记住。

#### 4.2.3 源码精读

lib 的 `__all__`：

[numpy/lib/\_\_init\_\_.py:48-52](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/__init__.py#L48-L52)

```python
__all__ = [
    "Arrayterator", "add_docstring", "add_newdoc", "array_utils",
    "format", "introspect", "mixins", "NumpyVersion", "npyio", "scimath",
    "stride_tricks", "tracemalloc_domain",
]
```

一共 **12 个名字**，可分为三类：

| 类别 | 成员 | 说明 |
| --- | --- | --- |
| 子模块 | `array_utils`、`format`、`introspect`、`mixins`、`npyio`、`scimath`、`stride_tricks` | 对外的（薄）模块 |
| 类/对象 | `Arrayterator`、`NumpyVersion` | 第 45–46 行单独导入的 |
| 底层工具 | `add_docstring`、`add_newdoc`、`tracemalloc_domain` | 文档/内存追踪相关辅助 |

紧跟着有一行**小但重要的代码**：

[numpy/lib/\_\_init\_\_.py:54](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/__init__.py#L54)

```python
add_newdoc.__module__ = "numpy.lib"
```

`add_newdoc` 其实来自 `numpy._core.function_base`，但 lib 把它的 `__module__` 属性**改写**成了 `"numpy.lib"`，让它对外看起来像是 lib 的成员。这是一种很常见的「改户口」技巧，用于让公开 API 的来源更整洁。

> 对比阅读：存根文件 `__init__.pyi` 里有一份**几乎相同**的 `__all__`：

[numpy/lib/\_\_init\_\_.pyi:39-52](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/__init__.pyi#L39-L52)

这份 `.pyi` 给静态类型检查器（如 mypy）看，它和运行时 `__all__` 保持一致，说明这 12 个名字就是 lib 的「官方公开名单」。

#### 4.2.4 代码实践

这正是本讲指定的实践任务：**遍历 `numpy.lib.__all__`，对每个成员打印其名称与 `__module__`，统计公开成员总数**。

**实践目标**：用一个脚本，把 lib 的公开名单逐个核对来源，加深「公开 API vs 私有实现」的直觉。

**操作步骤**：把下面的「示例代码」保存为 `inspect_lib_all.py` 并运行。

```python
# 示例代码：inspect_lib_all.py
import numpy.lib as lib

count = 0
for name in lib.__all__:
    obj = getattr(lib, name)              # 按名字取出对象
    mod = getattr(obj, "__module__", None)  # 取它的来源模块
    print(f"{name:20s} <- {mod}")
    count += 1

print("-" * 40)
print(f"公开成员总数: {count}")
```

**需要观察的现象**：

- 列表会逐行打印 12 个名字，每个名字后面跟它「真正来自哪个模块」。
- `NumpyVersion` 的 `__module__` 应是 `numpy.lib._version`（因为第 46 行从那里导入）。
- `Arrayterator` 的 `__module__` 应是 `numpy.lib._arrayterator_impl`。
- 子模块（如 `format`、`npyio`）的 `__module__` 一般是其自身路径（模块对象的 `__module__` 可能是 `None`，属正常现象，代码用 `getattr(..., None)` 做了兜底）。
- 末尾打印 `公开成员总数: 12`。

**预期结果**：脚本顺利打印 12 行明细和总数 12。（本讲未在你机器上实际运行，输出请以本地为准。）

#### 4.2.5 小练习与答案

**练习 1**：把 `np.pad` 是否在 `numpy.lib.__all__` 里？为什么我们仍能用 `np.pad`？

> **参考答案**：`pad` **不在** `numpy.lib.__all__` 里。能用 `np.pad` 是因为顶层 `numpy/__init__.py` 第 456 行用 `from .lib._arraypad_impl import pad` 把它直接搬到了顶层 `np` 命名空间。lib 的 `__all__` 只管「经 `numpy.lib` 暴露」的部分，不等于整个 numpy 的公开函数集。

**练习 2**：源码里为什么要写 `add_newdoc.__module__ = "numpy.lib"`？删掉这行会有什么可见影响？

> **参考答案**：`add_newdoc` 本来来自 `numpy._core.function_base`，默认 `__module__` 是那个底层模块。改写后，`numpy.lib.add_newdoc.__module__` 显示为 `numpy.lib`，公开 API 的来源更整洁，文档/帮助信息也更一致。删掉这行不影响函数行为，但会让「它的来源」对外暴露成底层模块。

---

### 4.3 `__getattr__` 与已移除别名的报错

#### 4.3.1 概念说明

NumPy 2.0 做了一次较大的 API 清理，把很多过去藏在 `numpy.lib` 下的子模块别名**移除**或**私有化**了。但直接「让旧名字消失」会让老代码报一些莫名其妙的错误。

于是 lib 用 **模块级 `__getattr__`**（PEP 562）做了一个聪明的兜底：当用户访问一个**已经不存在**的旧名字时，`__getattr__` 不会让 Python 报一个干巴巴的 `AttributeError`，而是**抛出一个带迁移指引的明确错误**，告诉你该怎么改。

#### 4.3.2 核心流程

访问 `numpy.lib.<某名字>` 时，Python 的查找顺序：

```text
访问 numpy.lib.emath
   │
   ▼
先在 lib 命名空间里找「emath」
   │
   ├── 找到 → 直接返回
   │
   └── 找不到 → 调用 lib.__getattr__('emath')
                    │
                    ├── 命中 emath 分支        → 报「已移除，请改用 numpy.emath」
                    ├── 命中「已私有化」名单   → 报「现在是私有，查迁移指南」
                    ├── 命中 arrayterator 分支 → 报「改用 numpy.lib.Arrayterator」
                    └── 都不命中               → 报标准「无此属性」
```

注意：这里**所有分支最终都 `raise AttributeError`**。也就是说，`__getattr__` 并不是把这些名字「偷偷还回去」，而是**把模糊的失败变成清晰的、带提示的失败**。这是一种很友好的废弃处理（deprecation）方式。

#### 4.3.3 源码精读

完整的 `__getattr__`：

[numpy/lib/\_\_init\_\_.py:61-90](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/__init__.py#L61-L90)

```python
def __getattr__(attr):
    # Warn for deprecated/removed aliases
    import warnings

    if attr == "emath":
        raise AttributeError(
            "numpy.lib.emath was an alias for emath module that was removed "
            "in NumPy 2.0. Replace usages of numpy.lib.emath with "
            "numpy.emath.",
            name=None
        )
    elif attr in (
        "histograms", "type_check", "nanfunctions", "function_base",
        "arraypad", "arraysetops", "ufunclike", "utils", "twodim_base",
        "shape_base", "polynomial", "index_tricks",
    ):
        raise AttributeError(
            f"numpy.lib.{attr} is now private. If you are using a public "
            "function, it should be available in the main numpy namespace, "
            "otherwise check the NumPy 2.0 migration guide.",
            name=None
        )
    elif attr == "arrayterator":
        raise AttributeError(
            "numpy.lib.arrayterator submodule is now private. To access "
            "Arrayterator class use numpy.lib.Arrayterator.",
            name=None
        )
    else:
        raise AttributeError(f"module {__name__!r} has no attribute {attr!r}")
```

逐段理解：

- **emath 分支**（第 65–71 行）：旧别名 `numpy.lib.emath` 在 2.0 移除，提示改用 `numpy.emath`。
- **「已私有化」名单分支**（第 72–82 行）：这一串名字（`histograms`、`type_check`、`nanfunctions`、`function_base`、`arraypad`、`arraysetops`、`ufunclike`、`utils`、`twodim_base`、`shape_base`、`polynomial`、`index_tricks`）正是过去那些**不带下划线、但现已私有化**的旧模块名。它们对应的实现现在搬到了带 `_impl` 后缀的私有模块里。提示语点明：公开函数去顶层 `numpy` 找，否则查迁移指南。
- **arrayterator 分支**（第 83–88 行）：旧 `numpy.lib.arrayterator` 子模块已私有化，但 `Arrayterator` 类仍可通过 `numpy.lib.Arrayterator` 访问（见 4.1.3 第 45 行）。
- **兜底分支**（第 89–90 行）：对任何其它不存在的属性，给出标准的「该模块无此属性」错误。

> 小知识：每个 `raise AttributeError(..., name=None)` 里的 `name=None` 是 Python 3.10+ 给 `AttributeError` 增加的参数，用来抑制「属性名回显」，避免错误信息里再叠加一个自动生成的名字，保证只显示我们写好的提示语。

#### 4.3.4 代码实践

**实践目标**：亲手触发 `__getattr__`，看到带迁移提示的报错信息。

**操作步骤**：

```python
# 示例代码：触发已移除别名的友好报错
import numpy.lib as lib

for alias in ["emath", "histograms", "arrayterator", "definitely_not_here"]:
    try:
        getattr(lib, alias)
    except AttributeError as e:
        print(f"[{alias}] -> {e}")
```

**需要观察的现象**：

- `emath` → 打印「…removed in NumPy 2.0. Replace usages of numpy.lib.emath with numpy.emath.」
- `histograms` → 打印「numpy.lib.histograms is now private. …」
- `arrayterator` → 打印「…submodule is now private. To access Arrayterator class use numpy.lib.Arrayterator.」
- `definitely_not_here` → 打印标准「module 'numpy.lib' has no attribute 'definitely_not_here'」。

**预期结果**：四个名字都触发 `AttributeError`，但前三条信息明显是**人写的迁移指引**，最后一条是**自动兜底**。这正是 `__getattr__` 的价值。（本讲未在你机器上实际运行，输出请以本地为准；若想进一步验证，可对照 `numpy.lib.__dict__` 确认这些名字确实不在命名空间里。）

#### 4.3.5 小练习与答案

**练习 1**：如果用户写 `numpy.lib.arraypad`，会得到什么提示？这条提示的核心建议是什么？

> **参考答案**：会命中第 72–82 行的「已私有化」名单分支，提示 `numpy.lib.arraypad is now private.`，核心建议是：如果是公开函数，应到**顶层 numpy 命名空间**找；否则查阅 **NumPy 2.0 迁移指南**。

**练习 2**：为什么 `__getattr__` 里所有分支最后都是 `raise AttributeError`，而不是 `return` 一个值？这样做有什么好处？

> **参考答案**：因为这些旧名字**真的已经被移除/私有化**，不能「偷偷还给用户」——否则旧代码会静默继续工作，永远停留在过时 API 上。统一抛出 `AttributeError` 既能让 `hasattr(...)` 之类判断正确返回 `False`，又能借由自定义错误信息**主动告诉用户该怎么迁移**，把「失败」变成「有指导的失败」。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个**「lib 公开边界探查器」**小任务。

**任务说明**：写一个脚本 `lib_boundary.py`，它要回答三个问题：

1. **lib 的定位**：打印 `numpy.lib.__doc__` 的第一句话，确认它是「杂项函数库」。
2. **公开名单核对**：遍历 `numpy.lib.__all__`，对每个名字打印「名字 + 来源模块」，并统计总数。
3. **边界识别**：选取一个你熟悉的函数名（例如 `pad`），分别测试「它在不在 `numpy.lib.__all__`」「能不能用 `numpy.lib.pad` 访问」「能不能用 `numpy.pad` 访问」，并用一句话总结结论。

**参考实现（示例代码）**：

```python
# 示例代码：lib_boundary.py
import numpy.lib as lib
import numpy as np

# (1) lib 的定位
first_line = (lib.__doc__ or "").strip().splitlines()[0]
print("定位:", first_line)

# (2) 公开名单核对
print("\n公开名单:")
for name in lib.__all__:
    obj = getattr(lib, name)
    print(f"  {name:18s} <- {getattr(obj, '__module__', None)}")
print(f"  总数: {len(lib.__all__)}")

# (3) 边界识别：以 pad 为例
name = "pad"
in_all = name in lib.__all__
try:
    getattr(lib, name); via_lib = True
except AttributeError:
    via_lib = False
via_np = hasattr(np, name)

print(f"\n边界识别({name}):")
print(f"  在 numpy.lib.__all__ 里? {in_all}")
print(f"  能用 numpy.lib.{name} 访问? {via_lib}")
print(f"  能用 numpy.{name} 访问?    {via_np}")
print("结论: pad 的实现在 numpy/lib/_arraypad_impl.py，但通过顶层 np.pad 暴露，"
      "不在 numpy.lib.__all__ 里。")
```

**预期现象**：

- (1) 打印 `mostly a space for implementing functions that don't ...` 这句定位。
- (2) 打印 12 行明细 + 总数 12。
- (3) `pad` 三问的答案应当是：**否 / 否 / 是**。

> 这个练习把本讲三个核心点（`__init__` 的定位、`__all__` 的公开名单、命名空间边界）一次性串起来。完成后，你就拥有了判断「某个函数到底归谁管」的基本方法。

---

## 6. 本讲小结

- **`numpy.lib` 是 numpy 的「杂项函数库」**：收纳不属于 core、也不属于 random/fft/linalg/ma 等有明确用途子包的函数，定位直接写在 [`__init__.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/__init__.py) 开头的 docstring 里。
- **`__init__.py` 的三件事**：导入底层工具、批量加载（私有 `_impl` + 对外薄）子模块、显式把 `Arrayterator` 与 `NumpyVersion` 放进命名空间。
- **`__all__` 只有 12 个名字**：主要是子模块、两个对象和几个底层工具；你常用的 `pad`、`histogram` 等都不在这里——它们通过顶层 `np.` 暴露。
- **lib 的 `__all__` ≠ 顶层 `np.` 命名空间**：实现写在 `numpy/lib/_xxx_impl.py`，再由顶层 `numpy/__init__.py` 往上搬，这是最容易混淆的一点。
- **`__getattr__` 做友好废弃**：对 NumPy 2.0 移除/私有化的旧别名（`emath`、`arraypad`、`arrayterator`…）抛出带迁移指引的 `AttributeError`，把模糊失败变成有指导的失败。

---

## 7. 下一步学习建议

本讲只看了「总入口」。下一篇 **u1-l2《模块组织与导入机制：再导出层与 dispatcher 模式》** 会往下钻一层，建议接着读：

1. **薄再导出模块**：打开 [`numpy/lib/stride_tricks.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/stride_tricks.py)（只有一行 `from ._stride_tricks_impl import ...`）和 [`numpy/lib/npyio.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/npyio.py)，体会「薄模块 + `_impl`」的分层。
2. **dispatcher 模式**：在 [`numpy/lib/_arraypad_impl.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_arraypad_impl.py) 里看 `_pad_dispatcher` 与 `@array_function_dispatch(_pad_dispatcher, module='numpy')` 装饰的 `pad`，理解「dispatcher + impl 双函数」写法。
3. **能力迁移**：学完本讲后，你可以用同样的方法（读 `__init__.py`、查 `__all__`、试 `__getattr__`）去快速摸清 numpy 的任何子包，例如 `numpy.fft`、`numpy.linalg`。

> 阅读顺序建议：先 u1-l2（模块组织与 dispatcher），再 u1-l3（测试入口与 `.pyi` 存根），把「认识 lib」这个入门单元补完整，再进入单元 2 的具体功能模块。
