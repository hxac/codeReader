# 包结构与导出关系

## 1. 本讲目标

上一篇我们认识了 `numpy.matrixlib` 子包的范围，以及 `np.matrix` 是一个「强制二维、即将弃用」的 `ndarray` 子类。本讲换一个视角，不看「`matrix` 能做什么」，而看「`matrix` 这个名字是怎么一路被搬运到 `np.matrix` 的」。

读完本讲你应该能够：

1. 读懂 `matrixlib/__init__.py` 如何把 `defmatrix.py` 里的名字转发出去。
2. 理解 `__all__` 作为「单一数据源」(single source of truth) 的作用，以及 `from .defmatrix import *` 实际导入了什么。
3. 明白为什么 `defmatrix` 这个以下划线相邻命名（其实是 `def`+`matrix`）的模块并不是私有模块。
4. 追踪 `np.matrix` / `np.asmatrix` / `np.bmat` 从子包到顶层名字的完整链路。
5. 解释一个看似矛盾的现象：`np.matrix`、`np.matrixlib.matrix`、`np.matrixlib.defmatrix.matrix` 三个名字指向同一个对象，且 `__module__` 全部显示为 `'numpy'`。

## 2. 前置知识

在进入源码前，先澄清几个 Python 包层面的基础概念。

### 2.1 子包(subpackage)与 `__init__.py`

在 Python 里，一个目录只要含有 `__init__.py` 就成为一个「包」(package)，目录名就是包名。`numpy/matrixlib/` 目录里有 `__init__.py`，所以它就是 `numpy` 包内部的子包 `numpy.matrixlib`。`__init__.py` 是子包的「门面」：当代码执行 `import numpy.matrixlib` 时，解释器会先运行这个文件。

### 2.2 `from .module import *` 与 `__all__`

`from .defmatrix import *` 表示「把同目录下 `defmatrix` 模块里所有公开名字都搬到当前命名空间」。这里的「公开」由两种规则决定：

- 如果被导入模块定义了 `__all__`，`import *` 就只导入 `__all__` 里列出的名字。
- 如果没有 `__all__`，则导入所有不以单下划线开头的名字。

所以 `__all__` 是模块作者主动声明的「对外公开清单」，它同时服务于 `import *` 和文档工具。

### 2.3 「私有」的命名约定

Python 没有真正的访问控制，只有约定：名字以单下划线 *开头*（如 `_foo`）通常表示「内部使用」。注意是开头——`defmatrix` 是 `def` + `matrix` 的拼接，并没有以下划线开头，所以它**不是**私有模块。这一点常被读者误读，本讲后面会专门解释。

### 2.4 子模块导入的副作用

当你写 `from . import matrixlib as _mat` 时，会发生两件事：一是把子模块绑定到本地名 `_mat`；二是把 `matrixlib` 子模块**作为属性**挂到父包 `numpy` 上（这就是导入子模块的副作用）。因此即便本地用了一个带下划线的别名 `_mat`，`np.matrixlib` 这个属性访问仍然成立——这是理解本讲「链路」的关键。

> 承接上一篇（u1-l1）：你已经知道子包只暴露 `matrix` / `asmatrix` / `bmat` 三个名字，并且 `matrix(...)` 会发出 `PendingDeprecationWarning`。本讲要回答的是：这三个名字在文件系统里是如何层层流转的。

## 3. 本讲源码地图

本讲涉及的文件按「从内到外」的顺序排列：

| 文件 | 作用 |
| --- | --- |
| [numpy/matrixlib/defmatrix.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py) | 子包**唯一**的业务实现文件，定义了 `matrix` 类、`asmatrix`、`bmat`，以及 `__all__`。 |
| [numpy/matrixlib/__init__.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/__init__.py) | 子包门面，负责转发 `defmatrix` 的名字并装配 `test` 入口。 |
| [numpy/matrixlib/__init__.pyi](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/__init__.pyi) | 子包的类型存根(stub)，给静态类型检查器看，与运行时行为对照。 |
| [numpy/__init__.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/__init__.py) | 顶层 `numpy` 包，把子包的名字提升为 `np.matrix` 等顶层 API。 |
| [numpy/_utils/__init__.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_utils/__init__.py) | 提供 `set_module` 装饰器，用来改写函数/类的 `__module__`。 |

本讲的最小模块包括：`__init__.py` 导出、`__all__`、`PytestTester`、顶层 `numpy/__init__.py` 暴露。

## 4. 核心概念与源码讲解

### 4.1 子包目录结构与单一数据源 `__all__`

#### 4.1.1 概念说明

`matrixlib` 是一个「小而精」的子包：真正干活的只有 `defmatrix.py` 一个文件，其它文件都是配套（类型存根、测试）。为了让「对外暴露哪些名字」这件事只在一处定义、到处复用，numpy 采用了**单一数据源**的策略：公开清单只在 `defmatrix.py` 的 `__all__` 里写一次，上游所有地方都引用它，而不是各自重新列举。这避免了「在 `defmatrix` 里新增了一个函数，却忘了在 `__init__` 里导出」这类不一致。

#### 4.1.2 核心流程

公开清单的传播路径可以画成一条单向链：

```
defmatrix.__all__   （唯一权威定义，写在 defmatrix.py 第 1 行）
        │
        ├── matrixlib/__init__.py 里：__all__ = defmatrix.__all__  （子包门面引用）
        │
        └── numpy/__init__.py 里：__all__ 包含 set(_mat.__all__)  （顶层 __all__ 引用）
```

任何一处要列举「matrixlib 暴露了什么」时，都不重新写字符串，而是去读 `defmatrix.__all__`。

#### 4.1.3 源码精读

公开清单的唯一权威定义在 `defmatrix.py` 的第 1 行：

[defmatrix.py:L1-L1](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L1-L1) — 定义子包的公开清单，列出 `matrix`、`bmat`、`asmatrix` 三个名字，是整条导出链的源头。

```python
__all__ = ['matrix', 'bmat', 'asmatrix']
```

> 注意顺序是 `matrix`、`bmat`、`asmatrix`，这只是一份列表，顺序本身没有运行时含义，但和后面 `__init__.pyi` 里写出的顺序略有不同（见 4.2.3），这也是为什么不能靠「记住顺序」而要靠引用同一来源。

顺带看一下文件顶部还有哪些导入，这能帮我们理解 `matrix` 类依赖什么：

[defmatrix.py:L7-L9](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L7-L9) — `matrix` 继承自 `numpy._core.numeric`（即 `ndarray` 的别名 `N`），并引入了 `set_module` 装饰器，这是后面 `__module__` 谜题的关键。

```python
import numpy._core.numeric as N
from numpy._core.numeric import concatenate, isscalar
from numpy._utils import set_module
```

#### 4.1.4 代码实践

1. 实践目标：验证 `__all__` 确实是「单一数据源」，并体会它和 `dir()` 的区别。
2. 操作步骤：在 Python 里直接读取 `defmatrix` 模块的 `__all__`，再和 `dir()` 对比。
3. 需要观察的现象：`__all__` 只有三项；而 `dir()` 会列出一大堆名字（包括 `N`、`ast`、`warnings` 这些导入进来的名字）。
4. 预期结果：

```python
import numpy.matrixlib.defmatrix as d
print(d.__all__)        # ['matrix', 'bmat', 'asmatrix']
print(len([n for n in dir(d) if not n.startswith('_')]))  # 远大于 3
```

5. 说明：这解释了为什么公开清单必须显式用 `__all__` 声明——靠 `dir()` 无法区分「公开 API」和「实现细节里顺手导入的名字」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `defmatrix.py` 第 1 行的 `__all__` 改成 `['matrix']`（去掉 `bmat` 和 `asmatrix`），`np.bmat` 还能用吗？

**答案**：仍然能用。因为顶层 `numpy/__init__.py` 第 620 行是 `from .matrixlib import asmatrix, bmat, matrix`——这是**显式逐个导入**，不依赖 `__all__`。`__all__` 只影响 `import *` 行为和 `from numpy import *` 是否包含它们（顶层 `__all__` 经 `set(_mat.__all__)` 构造，所以这种情况下 `from numpy import *` 就不会带 `bmat`）。换言之：显式 `from ... import name` 永远比 `__all__` 更强。

**练习 2**：为什么 numpy 要坚持「`__all__` 只在一处定义」？如果允许 `__init__.py` 自己再写一份 `__all__ = ['matrix', 'bmat', 'asmatrix']`，会有什么坏处？

**答案**：会引入「两处真相」。一旦 `defmatrix.py` 新增了公开函数（比如某天加了 `asmatrix` 的别名），开发者必须同时记得改 `__init__.py` 里的列表，否则就会出现「模块里有、但 `import *` 拿不到」的静默不一致。引用 `defmatrix.__all__` 让这种漂移在结构上不可能发生。

### 4.2 `__init__.py` 的转发导出机制

#### 4.2.1 概念说明

子包的门面 `matrixlib/__init__.py` 一共只有 12 行，它的全部职责就是「转发」：把 `defmatrix` 里实现好的名字搬到子包命名空间，再装配一个测试入口。它本身不写任何业务逻辑。这种「门面只转发、实现集中在 `_impl`/具体模块」的写法在 numpy 里非常普遍（`numpy/lib` 下大量 `_xxx_impl.py` 也是同样思路）。

#### 4.2.2 核心流程

`__init__.py` 的四步工作：

```
1. from . import defmatrix             # 把 defmatrix 挂为子包属性（np.matrixlib.defmatrix 可用）
2. from .defmatrix import *            # 按 __all__ 把 matrix/bmat/asmatrix 搬进子包命名空间
3. __all__ = defmatrix.__all__         # 子包自身的 __all__ 直接引用同一份清单（单一数据源）
4. test = PytestTester(__name__)       # 装配子包级测试入口（见 4.3）
```

注意第 1 步有一个常被忽略的副作用：`from . import defmatrix` 会让 `defmatrix` 成为 `matrixlib` 包的一个属性，所以 `np.matrixlib.defmatrix.matrix` 这个三层访问路径是合法的——这正是后面综合实践要验证的三条路径之一。

#### 4.2.3 源码精读

子包门面的完整内容：

[matrixlib/__init__.py:L1-L12](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/__init__.py#L1-L12) — 子包门面，做两件事：转发 `defmatrix` 的名字，装配 `test` 入口。

```python
"""Sub-package containing the matrix class and related functions.

"""
from . import defmatrix
from .defmatrix import *

__all__ = defmatrix.__all__

from numpy._pytesttester import PytestTester

test = PytestTester(__name__)
del PytestTester
```

逐行解读：

- 第 4 行 `from . import defmatrix`：相对导入，`.` 代表当前包（`matrixlib`）。这一行的副作用是把 `defmatrix` 挂为子包属性。
- 第 5 行 `from .defmatrix import *`：依据 `defmatrix.__all__`，把 `matrix`、`bmat`、`asmatrix` 搬进 `matrixlib` 命名空间，于是 `np.matrixlib.matrix` 可用。
- 第 7 行 `__all__ = defmatrix.__all__`：把同一份清单对象赋给子包的 `__all__`，注意这是**同一个列表对象的引用**，不是拷贝。
- 第 9–12 行：装配测试入口后立刻 `del PytestTester`，避免 `PytestTester` 这个工具类本身污染子包命名空间（它只是个「脚手架」，不该出现在 `np.matrixlib.PytestTester`）。

再看类型存根 `__init__.pyi`，它给静态类型检查器看的版本更直白：

[matrixlib/__init__.pyi:L1-L3](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/__init__.pyi#L1-L3) — 存根把运行时的 `from .defmatrix import *` 展开成显式的三个名字，并直接写死 `__all__`。

```python
from .defmatrix import asmatrix, bmat, matrix

__all__ = ["matrix", "bmat", "asmatrix"]
```

存根之所以要「展开」，是因为类型检查器（pyright/mypy）不会真正执行 `import *`，它需要看到具体的名字。这里 `__all__` 的顺序是 `["matrix", "bmat", "asmatrix"]`，和 `defmatrix.py` 第 1 行一致，但请把它当作「文档」而非「运行时来源」——运行时来源永远是 `defmatrix.__all__`。

#### 4.2.4 代码实践

1. 实践目标：验证「`from . import defmatrix` 让 `defmatrix` 成为子包属性」，以及子包命名空间里确实只有三个公开名字。
2. 操作步骤：

```python
import numpy.matrixlib as m
print('matrix' in dir(m), 'bmat' in dir(m), 'asmatrix' in dir(m))  # True True True
print('defmatrix' in dir(m))   # True —— 来自 from . import defmatrix
print('PytestTester' in dir(m))  # False —— 被 del 了
print(m.matrix is m.defmatrix.matrix)  # True —— 同一个对象
```

3. 需要观察的现象：`defmatrix` 出现在 `dir(m)` 里，但 `PytestTester` 不出现；`m.matrix` 和 `m.defmatrix.matrix` 是同一个对象（`is` 比较）。
4. 预期结果：上面四个表达式分别打印 `True True True`、`True`、`False`、`True`。
5. 说明：这印证了「门面只搬运、不创造」——所有名字都来自 `defmatrix`，门面只是把它们重新摆放到子包顶层。

#### 4.2.5 小练习与答案

**练习 1**：为什么第 12 行要写 `del PytestTester`？不写会怎样？

**答案**：不写的话，`PytestTester` 这个工具类就会留在子包命名空间里，出现在 `dir(np.matrixlib)` 中。它只是装配 `test` 的脚手架，不是面向用户的 API，留着会污染公开命名空间、误导用户以为 `np.matrixlib.PytestTester` 是稳定接口。`del` 是 numpy 控制公开表面的一种轻量手段。

**练习 2**：`matrixlib/__init__.pyi` 里写的是 `from .defmatrix import asmatrix, bmat, matrix`（显式三个名字），而运行时 `__init__.py` 写的是 `from .defmatrix import *`。为什么不让两边写法一致？

**答案**：类型检查器（pyright/mypy）不执行代码，无法像 CPython 那样根据 `__all__` 动态展开 `import *`，所以存根必须把名字写死、写全。运行时则偏好 `import *` + 引用 `__all__`，以维持单一数据源。这是「静态视图」与「运行动态视图」天然存在的表达差异。

### 4.3 `PytestTester`：每个子包自带的 `test()` 入口

#### 4.3.1 概念说明

numpy 的每个子包都挂了一个 `test` 对象，调用 `np.matrixlib.test()` 就能用 pytest 跑该子包自己的测试。背后的实现类是 `numpy._pytesttester.PytestTester`。它的设计目的：让用户无需记住测试文件路径，一行命令就能验证子包是否正常。这虽然不是「导出名字」的一部分，却和 `__init__.py` 的装配职责紧密相关，也是本讲最小模块之一。

#### 4.3.2 核心流程

```
PytestTester(__name__)
        │
        └── 构造时记住"要测哪个模块"（这里是 'numpy.matrixlib'）
        │
调用 test(label='fast', verbose=0, extra_argv=None)
        │
        └── 用 pytest 找到 numpy/matrixlib/tests/ 下的测试并运行
```

`__name__` 在 `matrixlib/__init__.py` 里就是 `'numpy.matrixlib'`，`PytestTester` 据此定位到 `numpy/matrixlib` 目录及其 `tests` 子目录。

#### 4.3.3 源码精读

装配测试入口的两行：

[matrixlib/__init__.py:L9-L12](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/__init__.py#L9-L12) — 导入 `PytestTester`，用当前包全名构造 `test` 对象，然后删除 `PytestTester` 名字。

```python
from numpy._pytesttester import PytestTester

test = PytestTester(__name__)
del PytestTester
```

- `test` 是一个**可调用对象**（`PytestTester` 实例实现了 `__call__`），所以 `np.matrixlib.test()` 实际是调用它的 `__call__`。
- 传入 `__name__`（即 `'numpy.matrixlib'`）而不是硬编码字符串，保证即使子包被改名或被重定位，测试入口仍然正确。这是「用模块自身身份代替魔法字符串」的好习惯。

#### 4.3.4 代码实践

1. 实践目标：确认 `test` 是一个可调用对象，并尝试触发它（只看是否能进入 pytest，不必跑完）。
2. 操作步骤：

```python
import numpy.matrixlib as m
print(callable(m.test))          # True
print(type(m.test).__module__, type(m.test).__name__)  # numpy._pytesttester PytestTester
```

3. 需要观察的现象：`callable(m.test)` 为 `True`，类型来自 `numpy._pytesttester`。
4. 预期结果：打印 `True`，以及 `numpy._pytesttester PytestTester`。
5. 运行测试（可选，耗时）：在命令行执行 `python -c "import numpy; numpy.matrixlib.test()"`，预期会看到 pytest 收集并运行 `numpy/matrixlib/tests/` 下的用例。如果环境里没有安装 pytest，这一步会报 `ModuleNotFoundError`，属于正常现象——这是「待本地验证」的部分。

#### 4.3.5 小练习与答案

**练习 1**：`test` 是函数还是对象？为什么写 `test = PytestTester(__name__)` 而不是 `def test(...): ...`？

**答案**：`test` 是 `PytestTester` 的**实例对象**，不是函数。它能像函数一样被调用，是因为 `PytestTester` 实现了 `__call__` 方法。用对象而非函数的好处是：可以在实例里保存「要测哪个模块」等状态，并把 `label`、`verbose` 等参数的默认值和文档集中在一个类里管理，多个子包共用同一套实现。

**练习 2**：为什么传 `__name__` 而不是写字符串 `'numpy.matrixlib'`？

**答案**：`__name__` 是模块运行时的真实全名，自动随包的导入路径变化。硬编码字符串一旦子包被重命名或重组就会失效，而 `__name__` 始终正确。这是一种去魔法字符串、降低维护成本的写法。

### 4.4 顶层 `numpy/__init__.py` 的暴露链路与 `@set_module`

#### 4.4.1 概念说明

到目前为止，`matrix` 只在 `np.matrixlib.matrix` 这个位置可用。但用户最常用的是 `np.matrix`——一个不带 `matrixlib` 的顶层名字。把子包里的名字「提升」到顶层命名空间，就是 `numpy/__init__.py` 的工作。

这里有个有趣的设计：顶层 numpy **既想用** `matrix`/`asmatrix`/`bmat` 这三个名字，**又想隐藏** `matrixlib` 这个子包本身（因为 `matrix` 已被官方劝退，不想让 `np.matrixlib` 出现在 `dir(np)` 里继续吸引注意）。本模块会看到这两件事是如何同时实现的。

最后还要解开一个谜题：`matrix` 类明明定义在 `numpy.matrixlib.defmatrix` 里，为什么它的 `__module__` 却是 `'numpy'`？答案是 `@set_module('numpy')` 装饰器——它故意改写了 `__module__`，让错误信息、文档、反序列化都把 `matrix` 呈现为「来自 numpy 顶层」。

#### 4.4.2 核心流程

顶层暴露的完整链路（注意本地图中的行号都来自 `numpy/__init__.py`）：

```
第 454 行: from . import lib, matrixlib as _mat
        │  （副作用：np.matrixlib 属性成立；本地别名是 _mat）
        ▼
第 620 行: from .matrixlib import asmatrix, bmat, matrix
        │  （把三个名字显式搬进 numpy 顶层命名空间 → np.matrix 可用）
        ▼
第 674–693 行: __all__ = ... | set(_mat.__all__) | ...
        │  （顶层 __all__ 引用 _mat.__all__，保证 from numpy import * 带上它们）
        ▼
第 771–779 行: __dir__() 里把 "matrixlib" 从可见名里减掉
           （np.matrixlib 仍可访问，但不出现在 dir(np)）
```

而 `__module__` 的改写发生在更早的地方——`defmatrix.py` 里类定义处：

```
@set_module('numpy')          # defmatrix.py 第 73 行
class matrix(N.ndarray): ...
        │
        └── set_module 把 matrix.__module__ 从 'numpy.matrixlib.defmatrix' 改写成 'numpy'
           （同时把原始出处存到 matrix._module_source 以备追溯）
```

#### 4.4.3 源码精读

第一步，顶层把子包导入为带下划线的别名 `_mat`：

[numpy/__init__.py:L454-L454](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/__init__.py#L454-L454) — 把 `matrixlib` 子包导入为本地别名 `_mat`（`lib` 同理）。注意：`from . import matrixlib` 的副作用会让 `np.matrixlib` 作为属性可用，即便本地用的是 `_mat` 这个名字。

```python
from . import lib, matrixlib as _mat
```

- 这里特意用 `_mat` 而不是 `matrixlib`，表示「顶层 numpy 内部只把它当作一个实现细节来引用」。
- 但因为「导入子模块会把子模块挂为父包属性」的副作用，`np.matrixlib` 这个属性访问**仍然成立**。这正是为什么 `_mat`（下划线、看似私有）和 `np.matrixlib`（公开可达）能同时为真。

第二步，把三个名字显式搬进顶层命名空间：

[numpy/__init__.py:L620-L620](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/__init__.py#L620-L620) — 显式从子包导入三个名字到顶层，这是 `np.matrix` / `np.asmatrix` / `np.bmat` 的直接来源。

```python
from .matrixlib import asmatrix, bmat, matrix
```

- 这是**显式逐个导入**，不经过 `__all__`，所以即使有人改了 `__all__`，这三个顶层名字依然可用（见 4.1.5 练习 1）。

第三步，顶层 `__all__` 引用子包清单：

[numpy/__init__.py:L674-L693](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/__init__.py#L674-L693) — 顶层 `__all__` 由多个子包清单并集而成，其中 `set(_mat.__all__)` 贡献了 `matrix`/`bmat`/`asmatrix`，保证 `from numpy import *` 会带上它们。

```python
__all__ = list(
    __numpy_submodules__ |
    set(_core.__all__) |
    set(_mat.__all__) |          # ← matrixlib 的三个名字从这里进入顶层 __all__
    set(lib._histograms_impl.__all__) |
    ...
)
```

- 再次体现「单一数据源」：顶层并不重新写 `['matrix','bmat','asmatrix']`，而是引用 `_mat.__all__`。

第四步，刻意把 `matrixlib` 从 `dir(np)` 中隐藏：

[numpy/__init__.py:L771-L779](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/__init__.py#L771-L779) — 自定义 `__dir__()`，把 `matrixlib` 等名字从可见符号里减去。注意这只是「隐藏可见性」，并非删除属性——`np.matrixlib` 仍可访问。

```python
def __dir__():
    public_symbols = (
        globals().keys() | __numpy_submodules__
    )
    public_symbols -= {
        "matrixlib", "matlib", "tests", "conftest", "version",
        "array_api"
    }
    return list(public_symbols)
```

- `globals()` 里其实有 `matrixlib`（导入副作用挂上去的），但 `__dir__()` 主动把它减掉，于是 `dir(np)` 看不到它。这是一个「软隐藏」：属性还在，只是不出现在目录里，配合 `matrix` 的弃用背景，降低它的曝光。

最后，解开 `__module__` 谜题的装饰器：

[defmatrix.py:L73-L74](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L73-L74) — `matrix` 类用 `@set_module('numpy')` 装饰，这正是它的 `__module__` 显示为 `'numpy'` 而非 `'numpy.matrixlib.defmatrix'` 的原因。`asmatrix` 在第 36 行也用了同样的装饰（[defmatrix.py:L36-L37](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L36-L37)）。

```python
@set_module('numpy')
class matrix(N.ndarray):
```

`set_module` 的实现：

[numpy/_utils/__init__.py:L17-L38](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/_utils/__init__.py#L17-L38) — 装饰器把传入的模块名字符串写入对象的 `__module__`；对类还会先把真实出处存到 `_module_source`，以免彻底丢失溯源信息。

```python
def set_module(module):
    def decorator(func):
        if module is not None:
            if isinstance(func, type):
                try:
                    func._module_source = func.__module__   # 备份真实出处
                except (AttributeError):
                    pass
            func.__module__ = module                         # 改写对外显示的模块名
        return func
    return decorator
```

- 对类（`isinstance(func, type)` 为真），先把原始 `__module__`（即 `'numpy.matrixlib.defmatrix'`）存到 `_module_source`，再覆盖成 `'numpy'`。这样既对用户呈现「来自 numpy」的整洁外观，又保留了「它真正定义在哪里」的追溯线索。
- 因此 `np.matrix.__module__ == 'numpy'`，而 `np.matrix._module_source == 'numpy.matrixlib.defmatrix'`。

> 小结这一节的三层「可见性」设计：①名字 `matrix/asmatrix/bmat` 显式提升到顶层并进入 `__all__`（完全公开）；②子包 `matrixlib` 作为属性可达但被 `__dir__` 软隐藏（半公开）；③类的 `__module__` 被改写为 `'numpy'`，真实出处存进 `_module_source`（呈现层重映射）。

#### 4.4.4 代码实践

1. 实践目标：验证「三条访问路径指向同一对象」与「`__module__` 被改写」这两件事，并对照 `set_module` 源码解释。
2. 操作步骤：

```python
import numpy as np

# 三条路径
a = np.matrix
b = np.matrixlib.matrix
c = np.matrixlib.defmatrix.matrix

print(a is b is c)                 # True —— 同一个类对象
print(a.__module__)                # 'numpy'
print(b.__module__)                # 'numpy'
print(c.__module__)                # 'numpy' —— 连最内层路径看到的也是 'numpy'
print(getattr(c, '_module_source', None))  # 'numpy.matrixlib.defmatrix' —— 真实出处
```

3. 需要观察的现象：三者 `is` 相等为 `True`；三个 `__module__` 全部是 `'numpy'`；`_module_source` 才是真实出处 `'numpy.matrixlib.defmatrix'`。
4. 预期结果：依次打印 `True`、`numpy`、`numpy`、`numpy`、`numpy.matrixlib.defmatrix`。
5. 解释：因为门面 `__init__.py` 只是搬运同一个类对象（没有重新定义），所以三条路径指向同一个 `matrix`；而 `@set_module('numpy')` 在类创建时就改写了 `__module__`，所以无论从哪条路径取，看到的都是 `'numpy'`。`_module_source` 是 `set_module` 为类特意保留的「真实出处」备份。
6. 顺带验证软隐藏：`'matrixlib' in dir(np)` 预期为 `False`，但 `hasattr(np, 'matrixlib')` 预期为 `True`——属性在，只是 `__dir__()` 不展示它。

#### 4.4.5 小练习与答案

**练习 1**：既然顶层第 454 行导入时用了别名 `_mat`，为什么 `np.matrixlib` 还能访问？

**答案**：因为「导入一个子模块」会让解释器把该子模块作为属性挂到父包上，这是导入机制的副作用，与本地用什么别名无关。`as _mat` 只影响顶层 `numpy/__init__.py` 内部用什么名字引用它，不影响 `np.matrixlib` 这个属性的存在。

**练习 2**：`np.matrix.__module__` 是 `'numpy'`，那 `repr(np.matrix)` 或文档里显示的「定义位置」会误导用户吗？numpy 用什么机制弥补？

**答案**：会在一定程度上让用户以为 `matrix` 定义在顶层 numpy。numpy 的弥补手段正是 `set_module` 对类额外保存的 `_module_source` 属性——它记录了真实出处 `'numpy.matrixlib.defmatrix'`，方便调试和内部工具溯源。这是一种「对外呈现简洁、对内保留真相」的折中。

**练习 3**：`from numpy import *` 之后，`matrix`、`asmatrix`、`bmat` 这三个名字在不在当前命名空间？为什么？

**答案**：在。因为顶层 `numpy/__init__.py` 的 `__all__`（第 674–693 行）通过 `set(_mat.__all__)` 把这三个名字纳入了顶层 `__all__`，而 `from package import *` 正是按 `__all__` 导入的。

## 5. 综合实践

把本讲的所有线索串起来：完成「三路径同源 + `__module__` 改写 + 软隐藏」的一次性验证脚本，并对每一行的输出给出基于源码的解释。

**任务**：编写并运行下面这段脚本，然后逐行用本讲引用的源码行号解释结果。

```python
import numpy as np

# 1) 三条访问路径是否同源？
paths = {
    'np.matrix':                   np.matrix,
    'np.matrixlib.matrix':         np.matrixlib.matrix,
    'np.matrixlib.defmatrix.matrix': np.matrixlib.defmatrix.matrix,
}
objs = list(paths.values())
print('同源:', all(o is objs[0] for o in objs))   # 预期 True

# 2) __module__ 与真实出处
print('module:', np.matrix.__module__)            # 预期 'numpy'
print('source:', np.matrix._module_source)        # 预期 'numpy.matrixlib.defmatrix'

# 3) 单一数据源：三层 __all__ 是否一致？
import numpy.matrixlib.defmatrix as d
print('defmatrix.__all__:', d.__all__)                        # ['matrix','bmat','asmatrix']
print('matrixlib.__all__ :', np.matrixlib.__all__)            # 同上（引用同一对象）
print('顶层包含三者      :',
      {'matrix','bmat','asmatrix'} <= set(np.__all__))        # 预期 True

# 4) 软隐藏：属性在，但 dir 看不到
print('hasattr(np, "matrixlib"):', hasattr(np, 'matrixlib'))  # 预期 True
print('"matrixlib" in dir(np) :', 'matrixlib' in dir(np))     # 预期 False
```

**需要观察与解释的要点**：

1. 第 1 步的 `True` → 因为 `__init__.py` 第 4–5 行只是搬运同一个类对象，没有重定义。
2. 第 2 步 `'numpy'` vs `'numpy.matrixlib.defmatrix'` → 前者是 `@set_module('numpy')`（见 [defmatrix.py:L73-L74](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L73-L74)）改写后的对外呈现，后者是 `set_module` 存进 `_module_source` 的真实出处；而 `_mat` 别名导入见 [numpy/__init__.py:L454-L454](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/__init__.py#L454-L454)。
3. 第 3 步的一致性 → 顶层 `__all__` 经 `set(_mat.__all__)` 引用子包清单，子包 `__all__` 又引用 `defmatrix.__all__`，三者本就是同一份数据。
4. 第 4 步的「属性在、目录不在」 → [numpy/__init__.py:L771-L779](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/__init__.py#L771-L779) 的 `__dir__()` 主动减去了 `matrixlib`。

> 如果你的 numpy 环境里 `matrix(...)` 会发出 `PendingDeprecationWarning`（见 u1-l1），上述脚本只读取类对象而不构造实例，因此不会触发警告。

## 6. 本讲小结

- `matrixlib` 子包的业务实现全部集中在 `defmatrix.py`，`__init__.py` 只做转发和装配，本身不含业务逻辑。
- `__all__` 作为**单一数据源**定义在 `defmatrix.py:1`，子包门面和顶层 `__all__` 都引用它，避免多处重复列举导致不一致。
- 门面用 `from . import defmatrix` + `from .defmatrix import *` 把名字搬到子包命名空间，同时让 `np.matrixlib.defmatrix` 这条路径可达；`PytestTester(__name__)` 为子包装配了一行式测试入口 `test()`。
- 顶层 `numpy/__init__.py` 通过 `from .matrixlib import asmatrix, bmat, matrix` 把三个名字提升为 `np.matrix` 等顶层 API，并把它们纳入顶层 `__all__`；同时用 `__dir__()` 对 `matrixlib` 做了「软隐藏」。
- `@set_module('numpy')` 装饰器把 `matrix`/`asmatrix` 的 `__module__` 改写为 `'numpy'`，并用 `_module_source` 保留真实出处——这解释了「三路径同源、`__module__` 全是 `numpy`」的现象。
- `defmatrix` 并非私有模块（它不以单下划线开头），以下划线开头的 `_mat` 只是顶层 numpy 内部使用的导入别名。

## 7. 下一步学习建议

到这里你已经彻底搞清楚了「`matrix` 这个名字从哪里来、怎么被搬运到顶层」。接下来的讲义建议：

- **u1-l3 快速上手 matrix/asmatrix/bmat 与运行测试**：从「名字链路」切换到「实际使用」，动手构造矩阵、体会 `asmatrix`（不复制）与 `matrix`（默认复制）的视图语义差异，并真正运行一次 `np.matrixlib.test()`。
- 进入进阶层后，**u2-l1 matrix 构造函数 `__new__` 与二维强制** 会带你打开 `defmatrix.py` 的 `matrix.__new__`，看它如何处理数组/字符串/嵌套列表三类输入——那是本讲提到的 `PendingDeprecationWarning` 的发出位置，也是「强制二维」规则的实现现场。
- 如果你对「装饰器改写类属性」这种元编程手法感兴趣，可以提前扫一眼 `numpy/_utils/__init__.py` 里的 `_rename_parameter` 等其它工具，它们和 `set_module` 是同一套「用装饰器维护公开 API 一致性」的思路。
