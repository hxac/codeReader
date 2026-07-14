# numpy.rec 模块定位与目录结构

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 `numpy.rec` 在 NumPy 中的定位：它是一个 **record array（记录数组）** 子包，提供「按属性访问字段」的数组类型。
- 看懂 `numpy/rec/__init__.py` 这个只有两行的「垫片文件」，并解释它为什么这么短。
- 找到 `numpy.rec` 真正的实现代码在哪里（提示：不在 `numpy/rec/` 目录里，而在 `numpy/_core/records.py`）。
- 列出 `numpy.rec` 公开的 9 个 API，并理解每个 API 是干嘛的。
- 理解 `@set_module('numpy.rec')` 装饰器如何让一段物理上写在别处的代码「看起来」属于 `numpy.rec` 命名空间。

本讲是整个 `numpy.rec` 学习手册的**第一篇**，不写任何复杂逻辑，只帮你建立「代码到底在哪、入口长什么样」的地图。后续每一篇都会反复引用本讲提到的实现文件 `numpy/_core/records.py`。

---

## 2. 前置知识

在看源码之前，先用大白话过一遍需要的基础概念。

### 2.1 什么是 record array（记录数组）

普通 NumPy 数组里每个元素都是**同一种类型**的数字，比如一个全 `float64` 的数组。

但现实数据常常像一张表格：每一行有「姓名（字符串）、年龄（整数）、身高（浮点数）」这种**不同类型的列**。NumPy 用 **structured array（结构化数组）** 来表达这种数据，每一「列」叫一个 **字段（field）**。

访问字段有两种风格：

| 风格 | 写法 | 来自 |
| --- | --- | --- |
| 字典式 | `arr['x']` | 普通结构化数组 |
| 属性式 | `arr.x` | **record array**（本手册的主角） |

`record array` 就是一种「允许你用 `arr.x` 这种点号属性来访问字段」的结构化数组。它读起来更顺手，所以 NumPy 专门为它建了一个子包 `numpy.rec`。

### 2.2 Python 的 `import *` 与 `__all__`

在 Python 中，一个模块里写 `from 某模块 import *` 时，到底会导入哪些名字？答案是：

- 如果「某模块」定义了 `__all__` 列表，就只导入 `__all__` 里列出的名字；
- 如果没有 `__all__`，就导入所有不以 `_` 开头的名字。

所以 `__all__` 本质上是模块的「公开 API 清单」。本讲你会看到 `numpy.rec` 正是用 `__all__` 来精确控制对外暴露哪 9 个名字的。

### 2.3 子包与命名空间

NumPy 是一个很大的包，内部被拆成很多「子包（subpackage）」，比如 `numpy.rec`、`numpy.lib`、`numpy.random`。一个子包其实就是一个目录，目录里的 `__init__.py` 决定了 `import numpy.rec` 时你能拿到什么。

本讲会看到一个非常典型的设计：子包目录里**不放真正的实现**，只放一个「再导出（re-export）」的垫片，把别处写好的实现搬过来。这种做法在大项目里很常见，目的是：保持公开 API（`numpy.rec`）稳定，而把实现自由地搬来搬去。

---

## 3. 本讲源码地图

本讲只涉及 3 个文件（外加 1 个辅助文件），都属于「入口 / 组织」层面，不涉及算法逻辑：

| 文件 | 行数级别 | 作用 |
| --- | --- | --- |
| `numpy/rec/__init__.py` | 2 行 | `numpy.rec` 子包的入口，**只是一个再导出垫片** |
| `numpy/rec/__init__.pyi` | 24 行 | 类型存根（type stub），告诉静态类型检查器 `numpy.rec` 导出了哪些名字 |
| `numpy/_core/records.py` | 约 1090 行 | **真正的实现**，所有类和函数都在这里 |
| `numpy/_utils/__init__.py` | （辅助） | 定义 `set_module` 装饰器，用来「改写」对象的 `__module__` |

> 注意：本讲的目录是 `numpy/rec/`，但实现文件 `numpy/_core/records.py` 在**另一个目录**。这是本讲最想强调的一点。

---

## 4. 核心概念与源码讲解

本讲拆成 3 个最小模块：

- **4.1** `__all__` 导出列表 —— `numpy.rec` 到底对外暴露哪些 API。
- **4.2** `from ... import *` 再导出机制 —— 垫片文件是怎么把实现「搬」过来的。
- **4.3** `set_module` 装饰器与 `numpy.rec` 命名空间 —— 为什么搬过来的代码还能显示成「属于 numpy.rec」。

---

### 4.1 `__all__` 导出列表

#### 4.1.1 概念说明

每个 Python 模块都可以定义一个名为 `__all__` 的列表，它是这个模块的「公开 API 目录」：

- 它决定了 `from 模块 import *` 会搬走哪些名字。
- 它也是给读者的「这里是公开接口」的提示：没在 `__all__` 里的名字，原则上属于内部实现，不应被外部使用。

`numpy.rec` 想要稳定地对外暴露一组固定的名字，所以它在实现文件里明确定义了 `__all__`。

#### 4.1.2 核心流程

`numpy.rec` 的公开 API 由实现文件 `numpy/_core/records.py` 顶部的 `__all__` 决定，一共 **9 个名字**：

```
record          结构化标量类型，单条记录，字段可属性访问
recarray        数组子类，整列字段可属性访问
format_parser   把 formats/names/titles 描述解析成 dtype 的工具类
fromarrays      按「列」方向，从一组数组构建 record array
fromrecords     按「行」方向，从一组记录（tuple/list）构建 record array
fromstring      从一段二进制 bytes 缓冲构建 record array
fromfile        从二进制文件读取并构建 record array
array           统一调度构造函数，根据输入类型自动分发到上面几个函数
find_duplicate  工具函数，找出列表里重复的名字（用于字段名校验）
```

可以这样分类记忆：

- 2 个**类型**：`record`（标量）、`recarray`（数组）。
- 1 个**解析器**：`format_parser`。
- 5 个**构造函数**：`fromarrays` / `fromrecords` / `fromstring` / `fromfile` / `array`。
- 1 个**工具函数**：`find_duplicate`。

#### 4.1.3 源码精读

实现文件的开头，先是一行模块说明，紧接着就是 `__all__` 列表（注意它正好列出 9 个名字）：

[numpy/_core/records.py:1-18](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L1-L18) —— 模块顶部：模块 docstring 与 `__all__` 公开 API 清单。

```python
"""
This module contains a set of functions for record arrays.
"""
...
# All of the functions allow formats to be a dtype
__all__ = [
    'record', 'recarray', 'format_parser', 'fromarrays', 'fromrecords',
    'fromstring', 'fromfile', 'array', 'find_duplicate',
]
```

上面这段说明：实现文件把 9 个名字登记为「公开 API」。后面你会看到，`numpy/rec/__init__.py` 正是依靠这个列表把它们整体搬过去。

类型存根文件 `__init__.pyi` 也维护了一份对应的清单（这里多写了显式 `import`，并给出同样的 `__all__`）：

[numpy/rec/__init__.pyi:1-23](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/rec/__init__.pyi#L1-L23) —— 类型存根：静态类型检查器看到的 `numpy.rec` 公开清单。

> 小提示：`.py` 文件给解释器看，`.pyi` 文件给类型检查器（如 mypy）看。两者要保持一致，所以你会看到同样的 9 个名字出现了两次。

#### 4.1.4 代码实践

1. **实践目标**：亲眼看到 `numpy.rec` 的 9 个公开名字。
2. **操作步骤**：在装好 numpy 的环境里，打开 Python / IPython，运行：

   ```python
   import numpy as np
   print(np.rec.__all__)
   print(len(np.rec.__all__))
   ```
3. **需要观察的现象**：终端应打印出那 9 个名字的列表，长度为 9。
4. **预期结果**（依据源码，待本地验证）：

   ```text
   ['record', 'recarray', 'format_parser', 'fromarrays', 'fromrecords',
    'fromstring', 'fromfile', 'array', 'find_duplicate']
   9
   ```

#### 4.1.5 小练习与答案

**练习 1**：如果在 `numpy/_core/records.py` 里新加了一个内部辅助函数 `_helper`，它会被 `from numpy._core.records import *` 搬走吗？

> **答案**：不会。一来它以下划线开头，二来更重要的是它不在 `__all__` 里。`import *` 只搬 `__all__` 中登记的名字。

**练习 2**：数一下，`numpy.rec` 一共有几个「构造类函数」（负责造出 record array 的）？

> **答案**：5 个，分别是 `fromarrays`、`fromrecords`、`fromstring`、`fromfile` 和统一调度入口 `array`。

---

### 4.2 `from ... import *` 再导出机制

#### 4.2.1 概念说明

「再导出（re-export）」指的是：A 文件里没有定义某个东西，而是从 B 文件 `import` 进来，于是对使用者来说，看起来 A 文件也「提供」了它。

`numpy/rec/__init__.py` 就是一个纯再导出垫片：它本身**没有任何业务逻辑**，全部内容就是两行 import。它存在的意义是：让 `import numpy.rec` 这条公开入口长期稳定，哪怕有一天实现代码搬家了，公开入口也不用改。

这种「公开入口 / 内部实现」分离的设计，是把易变的实现藏在 `_core`（带下划线，表示内部）里、把稳定的 API 暴露在 `numpy.rec` 里。

#### 4.2.2 核心流程

垫片的工作流程可以画成：

```
使用者: import numpy.rec
   │
   ▼
加载 numpy/rec/__init__.py
   │
   ├── from numpy._core.records import *        # 搬走 __all__ 里的 9 个名字
   └── from numpy._core.records import __all__, __doc__   # 顺带搬走「API清单」和「文档」
   │
   ▼
numpy.rec 命名空间里出现: record, recarray, fromarrays, ...
```

两行 import 各自的职责：

- 第 1 行 `from numpy._core.records import *`：按 `__all__` 把 9 个名字搬进 `numpy.rec`。
- 第 2 行 `from numpy._core.records import __all__, __doc__`：把「公开清单」和「模块文档字符串」也搬过来。这样 `numpy.rec.__all__` 和 `numpy.rec.__doc__` 就和实现文件保持一致。

#### 4.2.3 源码精读

垫片文件的全部内容，就这两行：

[numpy/rec/__init__.py:1-2](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/rec/__init__.py#L1-L2) —— `numpy.rec` 的真实入口：仅由两行再导出语句组成。

```python
from numpy._core.records import *
from numpy._core.records import __all__, __doc__
```

你可以把这两行理解为：「`numpy.rec` 把自己整个外包给了 `numpy._core.records`」。所以今后读 `numpy.rec` 的任何行为，都要去 `numpy/_core/records.py` 找答案。

#### 4.2.4 代码实践

1. **实践目标**：确认 `numpy.rec` 里的符号确实来自 `numpy._core.records.py`。
2. **操作步骤**：

   ```python
   import inspect, numpy as np

   # 1) 看模块文档（应该等于实现文件顶部那句 docstring）
   print(repr(np.rec.__doc__))

   # 2) 看函数所在文件（应该指向 .../numpy/_core/records.py）
   print(inspect.getfile(np.rec.fromarrays))

   # 3) 看 recarray 类的定义文件
   print(inspect.getfile(np.rec.recarray))
   ```
3. **需要观察的现象**：
   - `__doc__` 应为 `'This module contains a set of functions for record arrays.\n    '`（对应实现文件第 1–3 行）。
   - `inspect.getfile(...)` 返回的路径应以 `numpy/_core/records.py` 结尾。
4. **预期结果**：函数和类的源码文件都指向 `.../numpy/_core/records.py`（具体绝对前缀取决于你 numpy 的安装位置，待本地验证）。

#### 4.2.5 小练习与答案

**练习 1**：为什么不直接把实现写在 `numpy/rec/__init__.py` 里，而要搞一个再导出垫片？

> **答案**：为了让公开入口 `numpy.rec` 与内部实现 `numpy._core.records` 解耦。实现可以自由重构、搬家，只要垫片不变，使用者的 `import numpy.rec` 就不会受影响。带下划线的 `_core` 也明确告诉读者「这是内部实现，别直接依赖」。

**练习 2**：第 2 行 `from numpy._core.records import __all__, __doc__` 如果删掉，会发生什么？

> **答案**：`from ... import *` 默认不会搬走 `__all__`、`__doc__` 这类「模块元信息」（尤其 `__all__` 自己一般不会被 `*` 带走）。删掉第 2 行后，`numpy.rec.__all__` 可能就拿不到正确的 9 元素列表，`numpy.rec.__doc__` 也会丢失。所以这行是专门用来补齐模块元信息的。

---

### 4.3 `set_module` 装饰器与 `numpy.rec` 命名空间

#### 4.3.1 概念说明

这里有一个反直觉的小细节，值得单独讲。

`from numpy._core.records import *` 把函数搬进了 `numpy.rec`，但这些函数对象的「出生地」（`__module__` 属性）仍然记着 `numpy._core.records`。也就是说：

```python
np.rec.fromarrays.__module__   # 如果不做处理，会是 'numpy._core.records'
```

这会带来两个小问题：

1. 在交互式环境里打印函数、或者看报错栈，会显示 `numpy._core.records.fromarrays`，而不是用户熟悉的 `numpy.rec.fromarrays`。
2. 文档工具也会以为这些函数来自内部模块。

NumPy 用一个叫 `set_module` 的装饰器来「改写」对象的 `__module__`，让它显示成期望的公开模块名。这样物理上住在 `_core` 的代码，对外却显示为「属于 `numpy.rec`」。

#### 4.3.2 核心流程

`set_module` 的逻辑非常简单——它就是一个改写 `__module__` 的装饰器：

```
@set_module('numpy.rec')
def some_func(...):        # 先在 numpy._core.records 里定义
    ...
# 装饰器执行后: some_func.__module__ 被改成 'numpy.rec'
```

装饰器内部做的事：

1. 收到目标函数/类 `func`。
2. 把 `func.__module__` 直接赋值为传入的字符串（如 `'numpy.rec'`）。
3. 对类，还会顺手记下原来的真实来源到 `_module_source`，便于追溯。
4. 返回改好的 `func`。

效果：`np.rec.fromarrays.__module__ == 'numpy.rec'`，显示和文档里都干干净净地写着 `numpy.rec`。

> 一个例外：`record` 标量类**没有**用 `@set_module`，而是手动写了 `__module__ = 'numpy'`。所以它的「正式地址」是 `numpy.record`（全包级），再被 `numpy.rec` 重新导出。这一点后面讲 `record` 类时会再提到。

#### 4.3.3 源码精读

先看装饰器本体，它定义在通用工具模块里：

[numpy/_utils/__init__.py:17-38](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_utils/__init__.py#L17-L38) —— `set_module` 装饰器：把函数/类的 `__module__` 改写为指定公开模块名。

```python
def set_module(module):
    def decorator(func):
        if module is not None:
            if isinstance(func, type):
                try:
                    func._module_source = func.__module__   # 留底真实来源
                except (AttributeError):
                    pass
            func.__module__ = module                         # 改写为公开模块
        return func
    return decorator
```

再看它在实现文件里被大量使用。实现文件先把它 import 进来：

[numpy/_core/records.py:9-11](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L9-L11) —— 引入 `set_module` 与同包的 `numeric`、`numerictypes`。

```python
from numpy._utils import set_module

from . import numeric as sb, numerictypes as nt
```

然后几乎每个公开对象都戴上这个装饰器，统一指向 `numpy.rec`。下面是几个代表性位置：

[numpy/_core/records.py:278-279](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L278-L279) —— `recarray` 类用 `@set_module("numpy.rec")` 标注。

```python
@set_module("numpy.rec")
class recarray(ndarray):
```

[numpy/_core/records.py:569-570](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L569-L570) —— `fromarrays` 函数同样标注为 `numpy.rec`。

```python
@set_module("numpy.rec")
def fromarrays(arrayList, dtype=None, shape=None, formats=None,
```

一个值得专门看的例外是 `record` 标量类——它**不**用装饰器，而是手动设置 `__module__`：

[numpy/_core/records.py:196-203](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/records.py#L196-L203) —— `record` 类手动把名字和模块改成 `numpy.record`，所以打印时显示为 `numpy.record`。

```python
class record(nt.void):
    """A data-type scalar that allows field access as attribute lookup.
    """

    # manually set name and module so that this class's type shows up
    # as numpy.record when printed
    __name__ = 'record'
    __module__ = 'numpy'
```

> 对照一下：`recarray` 的「正式地址」是 `numpy.rec.recarray`（靠 `@set_module`）；`record` 的「正式地址」是 `numpy.record`（靠手动 `__module__ = 'numpy'`）。两者策略不同，但目的都是「让对象显示在用户最熟悉的公开位置」。

#### 4.3.4 代码实践

1. **实践目标**：观察 `set_module` 的效果，理解「物理位置」与「显示模块」的分离。
2. **操作步骤**：

   ```python
   import numpy as np

   print(np.rec.fromarrays.__module__)   # 显示模块
   print(np.rec.recarray.__module__)     # 显示模块
   print(np.rec.record.__module__)       # 注意：这里是 'numpy'，不是 'numpy.rec'
   ```
3. **需要观察的现象**：
   - 前两个应是 `'numpy.rec'`（装饰器改写的结果）。
   - 第三个应是 `'numpy'`（手动改写的结果，因为 `record` 的正式身份是 `numpy.record`）。
4. **预期结果**（依据源码，待本地验证）：

   ```text
   numpy.rec
   numpy.rec
   numpy
   ```

#### 4.3.5 小练习与答案

**练习 1**：去掉某个函数上的 `@set_module("numpy.rec")` 后，`np.rec.该函数.__module__` 会变成什么？

> **答案**：会变回它物理所在的模块名，即 `'numpy._core.records'`。功能不受影响，但显示、文档和报错栈里会出现内部路径 `numpy._core.records.xxx`，对用户不友好。

**练习 2**：`set_module` 对类做了一件「额外的小事」（函数对象不会做），是什么？

> **答案**：它在改写 `__module__` 之前，会把原来的真实模块名存到 `func._module_source` 里留底，方便日后追溯这个类到底定义在哪里。函数对象不做这一步。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个小任务：**画出从 `import numpy.rec` 到拿到 `fromarrays` 这条链路上，每个环节分别「住」在哪个文件里。**

操作步骤：

1. 运行下面这段「侦察脚本」，让它把三件事一次性打印出来：

   ```python
   import inspect, numpy as np

   print("== 1. numpy.rec 的公开 API 清单 ==")
   print(np.rec.__all__)

   print("\n== 2. 入口垫片文件的真实内容 ==")
   print(inspect.getsource(np.rec))

   print("\n== 3. fromarrays 的显示模块 / 物理文件 ==")
   print("module :", np.rec.fromarrays.__module__)
   print("file   :", inspect.getfile(np.rec.fromarrays))
   ```

2. 根据输出，用笔（或注释）回答 3 个问题：
   - `np.rec.__all__` 里有几个名字？是不是 4.1 节列出的那 9 个？
   - `inspect.getsource(np.rec)` 打印出来的，是不是 4.2 节那两行再导出语句？
   - `np.rec.fromarrays.__module__` 和 `inspect.getfile(np.rec.fromarrays)` 一个指向「显示模块」、一个指向「物理文件」，两者是否不同？（这正是 `set_module` 的作用。）

3. **预期结果**（依据源码，待本地验证）：
   - 清单长度为 9。
   - 垫片内容只有两行 `from numpy._core.records import ...`。
   - 显示模块为 `numpy.rec`，物理文件路径以 `numpy/_core/records.py` 结尾——「显示」与「物理位置」确实分离了。

完成这个练习后，你就建立起了本手册最重要的心智地图：**读 `numpy.rec` 的任何行为，都去 `numpy/_core/records.py` 找。**

---

## 6. 本讲小结

- `numpy.rec` 是 NumPy 的 **record array（记录数组）** 子包，核心卖点是「用 `arr.x` 这种属性方式访问结构化数组的字段」。
- `numpy/rec/__init__.py` 只有 2 行，是一个**再导出垫片**，把 `numpy._core.records` 的东西搬过来。
- **真正的实现全部在 `numpy/_core/records.py`**（约 1090 行），不在 `numpy/rec/` 目录里。
- `numpy.rec` 通过 `__all__` 精确对外暴露 **9 个 API**：2 个类型（`record`/`recarray`）、1 个解析器（`format_parser`）、5 个构造函数（`fromarrays`/`fromrecords`/`fromstring`/`fromfile`/`array`）、1 个工具（`find_duplicate`）。
- `@set_module('numpy.rec')` 装饰器改写对象的 `__module__`，让物理上住在 `_core` 的代码对外显示为「属于 `numpy.rec`」，分离了「物理位置」与「显示模块」。

---

## 7. 下一步学习建议

本讲只解决了「代码在哪、入口长什么样」。接下来：

- **下一篇（u1-l2）**：先建立直觉——讲清「普通结构化数组」与「record array」到底差在哪，以及 `record` 标量类型是怎么回事。建议同时去 `numpy/_core/numerictypes.py` 里瞟一眼 `nt.void`（`record` 的父类）。
- **再下一篇（u1-l3）**：动手用 `np.rec.array` / `np.rec.fromrecords` 真正造出你的第一个 record array。
- 想提前摸实现代码的话，可以直接打开 `numpy/_core/records.py` 的 `__all__`（第 15–18 行）和 `recarray` 类（第 278 行起）扫一眼，建立大致印象即可，细节后续讲义会逐个拆解。
