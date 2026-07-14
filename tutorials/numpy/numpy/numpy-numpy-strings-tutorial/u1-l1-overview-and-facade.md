# 项目定位与门面架构

## 1. 本讲目标

本讲是整套 `numpy.strings` 学习手册的第一篇。读完本讲，你应当能够：

- 说清楚 `numpy.strings` 是什么、它解决了什么问题（NumPy 的向量化字符串运算官方命名空间）。
- 看懂「门面（facade）模式」：`numpy/strings/__init__.py` 如何用仅仅两行代码，把全部公共符号从 `numpy._core.strings` 转发出来。
- 理解 `.pyi` 类型存根（stub）在门面里扮演的角色。
- 解释 `np.strings` 为什么是「懒加载」的：顶层 `numpy/__init__.py` 的 `__getattr__` 在你第一次访问时才真正 import。
- 厘清 `numpy.strings`、`numpy._core.strings`、`numpy.char` 三者的分工与历史关系。

本讲**不**深入任何字符串函数的具体算法——那是后续讲义的主题。本讲只解决一个问题：**当你写下 `np.strings.upper(...)` 时，这个名字到底是从哪里冒出来的**。

## 2. 前置知识

阅读本讲前，最好已经具备：

- 基本的 Python 语法：模块、`import`、`__all__` 的含义。
- 用过 NumPy 的 `ndarray`，知道 `dtype` 是什么。
- 大致听说过「ufunc（universal function）」这个词——它是 NumPy 里对数组逐元素运算的统一抽象（例如 `np.add`、`np.equal`）。本讲不需要你懂 ufunc 的内部实现，只要知道有这么一类「逐元素运算的函数」即可。

几个会被反复用到的术语，先用大白话解释：

| 术语 | 通俗解释 |
|------|----------|
| 门面（facade） | 一个「只有壳、没有肉」的模块，它自己不实现任何逻辑，只是把别处实现的函数重新挂出来，给调用者一个干净的入口。 |
| 命名空间（namespace） | 这里指 `np.strings.xxx` 这种「点号访问的一组名字」。把字符串相关的函数都收拢到 `strings` 名下，避免污染顶层的 `numpy`。 |
| 向量化（vectorized） | 对一个数组里的每个元素批量执行同一种操作（例如把 `['a','b']` 全部变大写），而不是写 Python 循环。 |
| 懒加载（lazy import） | 模块不在 `import numpy` 时就立刻加载，而是等你第一次用到时才加载，用来加快启动速度。 |

## 3. 本讲源码地图

本讲涉及的关键文件如下。注意：门面文件本身极短，真正的「肉」在 `_core/strings.py`，而 `np.strings` 能被访问到，靠的是顶层 `__init__.py` 的懒加载。

| 文件 | 作用 | 本讲中的角色 |
|------|------|--------------|
| `numpy/strings/__init__.py` | 门面：2 行代码转发全部公共符号 | **主角**：被分析的核心对象 |
| `numpy/strings/__init__.pyi` | 门面的类型存根（type stub） | 显式列出对外暴露的全部名字 |
| `numpy/_core/strings.py` | 真正的 Python 实现层 | 门面「转发到这里」的目标 |
| `numpy/__init__.py` | NumPy 顶层包初始化 | 通过 `__getattr__` 懒加载 `strings` |
| `numpy/_core/defchararray.py` | 遗留模块 `numpy.char` 的实现 | 对比：它是门面的「老大哥」 |

## 4. 核心概念与源码讲解

### 4.1 门面文件：numpy/strings/__init__.py

#### 4.1.1 概念说明

`numpy.strings` 的全部 Python 代码，几乎都浓缩在一个文件里，而且这个文件**只有两行**：

[numpy/strings/__init__.py:1-2](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/strings/__init__.py#L1-L2)

```python
from numpy._core.strings import *
from numpy._core.strings import __all__, __doc__
```

这就是一个教科书式的「门面模式（facade pattern）」。它的特点：

- **自己没有任何函数定义**：文件里没有一行 `def`，也没有任何业务逻辑。
- **只做转发**：第 1 行 `import *` 把 `numpy._core.strings` 里被 `__all__` 列出的公开函数，原封不动地搬到 `numpy.strings` 这个命名空间下。
- **额外搬运两个 dunder**：第 2 行单独把 `__all__` 和 `__doc__` 也带过来。之所以要单独写，是因为 Python 的 `import *` 默认**不会**导入以双下划线开头和结尾的名字（dunder），所以必须显式再 import 一次。

> 为什么要单独搞一个门面，而不是直接让大家用 `numpy._core.strings`？
> 因为 `_core` 是「内部实现」目录，名字里的下划线就是「这是私有的、可能随时变」的信号。NumPy 希望给用户一个稳定的、干净的公开入口 `numpy.strings`，同时保留在 `_core` 里自由重构内部实现的权利。门面模式正是用来「解耦公开接口和内部实现」的。

#### 4.1.2 核心流程

当你写 `from numpy.strings import upper` 时，背后发生的事可以画成一条单向链：

```
你的代码: from numpy.strings import upper
        │
        ▼
触发 numpy/strings/__init__.py 执行
        │  (from numpy._core.strings import *)
        ▼
加载 numpy/_core/strings.py  (真正的实现在这里)
        │  upper 等函数都在这里被 def / @set_module 装饰
        ▼
upper 这个函数对象被挂到 numpy.strings 命名空间
        │
        ▼
你拿到 upper —— 它和 numpy._core.strings.upper 是同一个对象
```

关键点：门面**不复制、不包装**函数，它只是让「同一个函数对象」多了一个可访问的名字。也就是说，`numpy.strings.upper` 和 `numpy._core.strings.upper` 是**同一个 Python 对象**（用 `is` 判断为 `True`）。

#### 4.1.3 源码精读

门面文件本身已经全部贴出（见 4.1.1）。我们再确认它「转发到的目标」确实是有内容的实现层。打开 `numpy/_core/strings.py`，第一句话就交代了它的职责：

[numpy/_core/strings.py:1-4](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L1-L4)

```python
"""
This module contains a set of functions for vectorized string
operations.
"""
```

紧接着，实现层从三个地方「进货」：

[numpy/_core/strings.py:10-21](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L10-L21)

```python
from numpy import (
    add,
    equal,
    greater,
    greater_equal,
    less,
    less_equal,
    multiply as _multiply_ufunc,
    not_equal,
)
from numpy._core.multiarray import _vec_string
from numpy._core.overrides import array_function_dispatch, set_module
```

这段说明 `numpy._core.strings` 自己也只是一个「Python 包装层」：它把比较/拼接类 ufunc（`add`/`equal`/...）从 NumPy 顶层直接拿来用，把逐元素调用 Python 字符串方法的通用桥 `_vec_string` 从 `multiarray` 拿来，并用 `set_module`/`array_function_dispatch` 这些装饰器把函数重新包装成「属于 `numpy.strings` 的公开函数」。这些细节属于后续讲义（u2 单元），本讲你只需要记住：**门面指向 `_core.strings`，而 `_core.strings` 再向下指向更底层的 ufunc 与 C 代码**。

#### 4.1.4 代码实践

**实践目标**：用代码证明「门面文件只是转发、没有任何自有实现」。

**操作步骤**：

新建一个 `facade_check.py`，写入：

```python
# 示例代码：验证 numpy.strings 是纯转发门面
import inspect
import numpy.strings as ns
import numpy._core.strings as core

# 1) 打开门面文件的源码，确认它只有两行 import
print("=== 门面文件源码 ===")
print(inspect.getsource(ns))

# 2) 门面里的每个公共名字，应当与 _core.strings 里的是「同一个对象」
mismatch = [name for name in ns.__all__
            if getattr(ns, name) is not getattr(core, name)]

print("=== 对象身份不一致的名字（应当为空）===")
print(mismatch)

# 3) 门面自己有没有定义任何函数？查它的 __dict__ 里有没有 def 出来的东西
own_defs = [k for k, v in vars(ns).items()
            if inspect.isfunction(v) and v.__module__ == ns.__name__]
print("=== 门面自己定义的函数（应当为空）===")
print(own_defs)
```

**需要观察的现象**：

1. 第 1 步打印出的源码，应当**正好就是**本讲 4.1.1 贴的那两行，没有任何 `def`。
2. 第 2 步的 `mismatch` 应当是空列表 `[]`——证明 `np.strings.upper is np._core.strings.upper`。
3. 第 3 步的 `own_defs` 应当也是空列表——证明门面没有自己的函数实现。

**预期结果**：三处都为空 / 仅含两行 import。

**待本地验证**：不同 Python/NumPy 版本下 `inspect.getsource` 的精确输出格式可能略有差异，但「两行 import + 三个空列表」的结论在当前 HEAD（`9559a6b1ac`）下成立。

#### 4.1.5 小练习与答案

**练习 1**：如果把门面文件第 2 行 `from numpy._core.strings import __all__, __doc__` 删掉，会发生什么？

**参考答案**：`import *` 不会导入 dunder 名字，所以 `numpy.strings.__all__` 会丢失（变成回退到默认值），`numpy.strings.__doc__` 也会变成 `None`。这会让 `help(numpy.strings)` 和 `dir()` 行为异常。这就是为什么门面要单独再 import 这两个名字。

**练习 2**：`numpy.strings.upper is numpy._core.strings.upper` 的结果是 `True` 还是 `False`？为什么？

**参考答案**：`True`。因为门面用的是 `import *`，它只是把**同一个函数对象**绑定到新名字，并不复制或重新定义。两者的 `id()` 也相同。

---

### 4.2 类型存根：numpy/strings/__init__.pyi

#### 4.2.1 概念说明

`.pyi` 文件叫「类型存根（type stub）」。它是给静态类型检查器（如 mypy、pyright）和 IDE 用的「接口说明书」：里面只声明「这个模块对外暴露哪些名字、它们的类型签名是什么」，不包含任何运行时逻辑。

对门面来说，`.pyi` 有一个额外的好处：它**显式、逐个地**列出了门面暴露的全部名字。这对我们读懂门面非常有帮助——因为 `__init__.py` 里只有一个模糊的 `import *`，你不知道到底转发了哪些；而 `.pyi` 把清单写得清清楚楚。

#### 4.2.2 核心流程

`.pyi` 用两条途径保证「门面对外接口」的清晰与一致：

```
运行时：__init__.py 的 import *      —— 决定 import numpy.strings 后能拿到什么
类型期：__init__.pyi 的显式 import   —— 决定 mypy / IDE 认为能拿到什么
```

两者必须保持一致：`.pyi` 里多写一个名字，类型检查会以为它存在而运行时却找不到；少写一个，类型检查会误报「这个名字不存在」。因此维护门面时，**改 `__init__.py` 通常要同步改 `.pyi`**。

#### 4.2.3 源码精读

`.pyi` 的开头是一个逐个列举的 import 语句：

[numpy/strings/__init__.pyi:1-48](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/strings/__init__.pyi#L1-L48)

```python
from numpy._core.strings import (
    add,
    capitalize,
    center,
    count,
    # ...（逐个列出）
    upper,
    zfill,
)
```

然后在文件末尾，它又用 `__all__` 给出了完整的公开名字清单：

[numpy/strings/__init__.pyi:50-97](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/strings/__init__.pyi#L50-L97)

```python
__all__ = [
    "equal",
    "not_equal",
    # ...（逐个列出）
    "translate",
    "slice",
]
```

数一下：这个清单在当前 HEAD 下共有 **46 个**公共符号（import 列表与 `__all__` 都是 46 个）。> 备注：本讲义规格里曾粗略提到「45 个名字」，那是规划阶段的近似值；以源码实际计数为准是 46 个。这也是为什么本讲的实践任务会让你**用脚本去数**，而不是死记一个数字。

这 46 个名字大致分四类（顺序与 `.pyi` 的 `__all__` 不完全一致，但类别清晰）：

| 类别 | 代表函数 |
|------|----------|
| 比较与拼接 | `equal` / `not_equal` / `less` / `greater` / `add` / `multiply` |
| 信息查询 | `str_len` / `find` / `index` / `count` / `startswith` / `endswith` |
| 形态变换 | `center` / `ljust` / `rjust` / `zfill` / `strip` / `expandtabs` |
| 大小写与编码 | `upper` / `lower` / `swapcase` / `capitalize` / `title` / `encode` / `decode` |

#### 4.2.4 代码实践

**实践目标**：用脚本统计 `numpy.strings` 真实暴露的公开符号数量，并与 `.pyi` 的清单逐一对照。

**操作步骤**：

```python
# 示例代码：统计并核对 numpy.strings 的公开符号
import numpy.strings as ns

# 过滤掉以下划线开头的 dunder / 私有名字
public = sorted(n for n in dir(ns) if not n.startswith("_"))
print("公开符号数量:", len(public))   # 预期 46
print(public)

# 与 .pyi 里 __all__ 的清单做差集
pyi_all = {
    "equal","not_equal","less","less_equal","greater","greater_equal",
    "add","multiply","isalpha","isdigit","isspace","isalnum","islower",
    "isupper","istitle","isdecimal","isnumeric","str_len","find","rfind",
    "index","rindex","count","startswith","endswith","lstrip","rstrip",
    "strip","replace","expandtabs","center","ljust","rjust","zfill",
    "partition","rpartition","upper","lower","swapcase","capitalize",
    "title","mod","decode","encode","translate","slice",
}
print("运行时多了:", set(public) - pyi_all)
print(".pyi 多了:", pyi_all - set(public))
```

**需要观察的现象**：

- `len(public)` 打印 **46**。
- 两个差集都为空集——运行时暴露的名字和 `.pyi` 声明的完全一致。

**预期结果**：数量为 46，两个差集均为 `set()`。

**待本地验证**：如果你安装的 NumPy 版本与当前 HEAD 不同，数量可能不同（例如未来 `join`/`split` 被重新加入命名空间时会变多）。以脚本实际输出为准。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `.pyi` 不直接写 `from numpy._core.strings import *`，而要逐个列出 46 个名字？

**参考答案**：类型存根的目标是「精确声明接口」。`import *` 在 stub 里会让类型检查器无法知道到底导出了哪些名字（尤其 `__all__` 的内容要到运行时才知道），从而丢失类型信息、也无法对外形成稳定的 API 契约。逐个列出虽然啰嗦，但接口一目了然，也方便 review 时发现「多了或少了一个名字」。

**练习 2**：如果有人在 `_core/strings.py` 新增了一个函数并加进 `__all__`，却忘了更新 `.pyi`，会有什么后果？

**参考答案**：运行时 `numpy.strings.new_func` 能用，但 mypy/IDE 会报「模块没有这个属性」，并且对外文档（基于 `.pyi` 生成的 API 参考）会漏掉它。这正是「改门面要同步改 stub」的原因。

---

### 4.3 顶层懒加载：numpy.__init__ 的 __getattr__

#### 4.3.1 概念说明

你会注意到：`import numpy` 之后，直接写 `np.strings` 就能用，但 `numpy/strings/__init__.py` 并**没有**被 `numpy/__init__.py` 在启动时 import。这是怎么做到的？

答案是一个 Python 3.7+ 的特性：**模块级 `__getattr__`**。当一个模块（这里是 `numpy` 这个包）被访问一个它自身没有的属性时，Python 会去调用这个模块里定义的 `__getattr__(attr)` 函数，由它决定返回什么。NumPy 用这个机制实现了「子模块懒加载」——只有当你真正用到 `np.strings` 时，才真正去 import 它，从而加快 `import numpy` 的启动速度。

#### 4.3.2 核心流程

```
import numpy        # 此时 numpy.strings 并未被加载
np.strings          # 访问一个 numpy 自己没有的属性
        │
        ▼
触发 numpy/__init__.py 的模块级 __getattr__("strings")
        │  命中 elif attr == "strings": 分支
        ▼
执行: import numpy.strings as strings
        │  这才真正触发 numpy/strings/__init__.py（门面）执行
        ▼
返回 strings 模块对象，并缓存到 sys.modules
        │
        ▼
后续 np.strings 直接命中缓存，不再重复加载
```

要点：

- **懒**：`import numpy` 时不加载 `strings`，省掉 C 字符串模块的初始化开销。
- **只触发一次**：第一次访问后，模块对象进入 `sys.modules` 缓存，之后访问零成本。
- **声明与实现分离**：`strings` 这个名字先被登记进 `__numpy_submodules__`（对外表示「我是合法子模块」），但真正的 import 逻辑写在 `__getattr__` 里。

#### 4.3.3 源码精读

在顶层 `numpy/__init__.py` 里，`strings` 先被登记为「公开子模块」之一：

[numpy/__init__.py:626-630](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/__init__.py#L626-L630)

```python
    __numpy_submodules__ = {
        "linalg", "fft", "dtypes", "random", "polynomial", "ma",
        "exceptions", "lib", "ctypeslib", "testing", "typing",
        "f2py", "test", "rec", "char", "core", "strings",
    }
```

注意这里的注释（见源码 622-625 行）：这些子模块都是**懒加载**的，因此通过 `__getattr__` 访问。`strings` 和 `linalg`、`fft`、`random` 等是同一待遇。

真正的 import 逻辑在模块级 `__getattr__` 里：

[numpy/__init__.py:749-751](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/__init__.py#L749-L751)

```python
        elif attr == "strings":
            import numpy.strings as strings
            return strings
```

这一段就是「按下 `np.strings` 这个按钮，机器才开始运转」的开关。它和上面 `linalg`/`fft`/`char` 等分支结构完全对称，是 NumPy 统一的懒加载套路。

#### 4.3.4 代码实践

**实践目标**：亲手验证「`np.strings` 是懒加载、且通过 `__getattr__` 进来的」。

**操作步骤**：

```python
# 示例代码：观察 np.strings 的懒加载
import numpy
import sys

# 访问前：检查 strings 模块是否已经在缓存里
print("访问前 sys.modules 里有 numpy.strings 吗:",
      "numpy.strings" in sys.modules)   # 预期 False（或 True，取决于环境是否预热）

# 第一次访问 np.strings —— 触发 __getattr__
m = numpy.strings
print("numpy.strings.__name__:", m.__name__)   # 预期 'numpy.strings'

# 访问后：现在缓存里一定有了
print("访问后 sys.modules 里有 numpy.strings 吗:",
      "numpy.strings" in sys.modules)   # 预期 True

# 再访问一次，对象应当和刚才完全相同（命中缓存）
print("两次访问是同一对象:", numpy.strings is m)   # 预期 True
```

进一步，做一个**源码阅读型**实践：打开 `numpy/__init__.py`，定位 `def __getattr__(attr):`（约 700 行），沿着 `if/elif` 链找到 `elif attr == "strings":` 分支，确认它和 `linalg`、`char` 等分支写法完全一致。

**需要观察的现象**：

- 访问前 `numpy.strings` 可能不在 `sys.modules`（纯净环境下为 `False`）；访问后必定为 `True`。
- 两次访问拿到的是同一个模块对象。

**预期结果**：如上。

**待本地验证**：如果你的 Python 进程在此之前已经间接 import 过 `numpy.strings`（例如某些 IDE 或测试框架预热），第一次检查可能就是 `True`。这不影响结论——重点看「访问后必然为 True 且对象相同」。

#### 4.3.5 小练习与答案

**练习 1**：如果删掉 `__getattr__` 里的 `elif attr == "strings":` 分支，`np.strings` 会发生什么？

**参考答案**：访问 `np.strings` 会抛 `AttributeError`（除非 `strings` 在别处被预先 import）。因为顶层 `__init__.py` 没有显式 `import numpy.strings`，全靠 `__getattr__` 这条分支把它接进来。

**练习 2**：懒加载为什么要和 `__numpy_submodules__` 这个集合配合？

**参考答案**：`__numpy_submodules__` 用来声明「哪些名字是合法的公开子模块」，它会被并入顶层 `__all__`（见源码 674-693 行），让 `from numpy import *` 和文档系统知道 `strings` 是官方公开成员。而真正的「按需加载」动作放在 `__getattr__`。两者分工：一个管「对外宣称」，一个管「实际加载」。

---

### 4.4 三者关系：numpy.strings / numpy._core.strings / numpy.char

#### 4.4.1 概念说明

现在把三个容易混淆的名字放在一起厘清：

- **`numpy._core.strings`**：真正的 Python 实现层。下划线开头表示「内部、可能变」。所有函数的 `def` 都在这里。
- **`numpy.strings`**：官方公开门面。只转发 `_core.strings` 的符号，是新代码**推荐**使用的入口。
- **`numpy.char`**：更古老的模块（实现在 `numpy/_core/defchararray.py`），为兼容 Numarray 历史接口而保留。它**反过来复用了 `numpy.strings`**，又额外补了几个历史函数。

一句话总结关系：**`_core.strings`（实现）→ `numpy.strings`（门面）→ `numpy.char`（在门面之上再加历史的兼容层）**。

#### 4.4.2 核心流程

```
numpy/_core/strings.py            真正 def 出所有函数（实现层）
        │
        │  from numpy._core.strings import *
        ▼
numpy/strings/__init__.py         门面：转发（新代码用这个）
        │
        │  from numpy.strings import *
        ▼
numpy/_core/defchararray.py       = numpy.char：门面 + 历史兼容函数（旧代码用这个）
```

`numpy.char` 在门面之上**多做了两件事**：

1. 补回几个 `numpy.strings` 暂时没有的函数（`join`/`split`/`rsplit`/`splitlines`）。
2. 提供 `chararray` 类和 `compare_chararrays`（Numarray 兼容的字符串数组类型）。

#### 4.4.3 源码精读

先看实现层 `__all__` 里几行非常有信息量的**分组注释**：

[numpy/_core/strings.py:73-90](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/strings.py#L73-L90)

```python
__all__ = [
    # UFuncs
    "equal", "not_equal", "less", ...  # 已经是 ufunc 的

    # _vec_string - Will gradually become ufuncs as well
    "upper", "lower", "swapcase", "capitalize", "title",

    # _vec_string - Will probably not become ufuncs
    "mod", "decode", "encode", "translate",

    # Removed from namespace until behavior has been crystallized
    # "join", "split", "rsplit", "splitlines",
]
```

这段注释是理解整个子系统演进方向的「藏宝图」：

- **UFuncs**：已经是高效 C 循环的函数（比较、查找、对齐、裁剪等）。
- **Will gradually become ufuncs**：目前用 `_vec_string`（逐元素调 Python 方法）实现，将来会逐步改成 ufunc（`upper`/`lower`/...）。
- **Will probably not become ufuncs**：因为语义复杂（`mod` 格式化、`encode`/`decode` 跨编码），几乎不会变 ufunc。
- **Removed from namespace**：`join`/`split`/`rsplit`/`splitlines` 行为尚未定型，**暂时从 `numpy.strings` 移除**。这正好解释了：为什么它们在 `.pyi` 的 46 个名字里**找不到**，却能从 `numpy.char` 用到。

再看 `numpy.char` 是如何复用门面的：

[numpy/_core/defchararray.py:23-35](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/_core/defchararray.py#L23-L35)

```python
from numpy._core.strings import (
    _join as join,
    _rsplit as rsplit,
    _split as split,
    _splitlines as splitlines,
)
from numpy._utils import set_module
from numpy.strings import *
from numpy.strings import (
    multiply as strings_multiply,
    partition as strings_partition,
    rpartition as strings_rpartition,
)
```

可以清楚看到：

- `from numpy.strings import *`：`char` 把门面的 46 个函数全部继承过来。
- 额外从 `_core.strings` 私有地导入 `_join/_split/_rsplit/_splitlines`，并改名为公开的 `join/split/...`——这就是 `char` 比门面「多」出来的部分。
- 它还特意把 `multiply/partition/rpartition` 改名再导入一份（`strings_multiply` 等），因为 `char` 要对这些函数做**行为微调**（例如 `char.multiply`/`char.partition` 在语义上和 `strings` 版本略有差异）。

这正是「新代码用 `numpy.strings`、旧代码继续用 `numpy.char`」的由来。

#### 4.4.4 代码实践

**实践目标**：对比 `numpy.strings` 与 `numpy.char` 的公开成员，找出 `char` 多出来的部分。

**操作步骤**：

```python
# 示例代码：对比 strings 与 char 的公开符号
import numpy.strings as ns
import numpy.char as nc

ns_names = {n for n in dir(ns) if not n.startswith("_")}
nc_names = {n for n in dir(nc) if not n.startswith("_")}

print("char 比 strings 多出的公开符号:")
print(sorted(nc_names - ns_names))
# 预期包含: join, split, rsplit, splitlines, chararray, compare_chararrays 等

print("strings 比 char 多出的公开符号:")
print(sorted(ns_names - nc_names))
# 预期为空或极少
```

**需要观察的现象**：

- `char` 比 `strings` 多出 `join`、`split`、`rsplit`、`splitlines`，以及 `chararray`、`compare_chararrays` 等历史成员。
- 反方向的差集基本为空（`strings` 是 `char` 的子集）。

**预期结果**：如上。

**待本地验证**：具体多出的名字会随版本变化，但 `join/split/rsplit/splitlines` 这四个是稳定的（对应源码里「Removed from namespace」的那几行注释）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `np.strings.join` 不存在，但 `np.char.join` 却能用？

**参考答案**：`_core.strings` 的 `__all__` 注释里写明 `join`/`split`/...「Removed from namespace until behavior has been crystallized」——行为尚未定型，所以从 `numpy.strings` 公开命名空间移除了。但底层私有函数 `_join` 仍然存在，`numpy.char` 通过 `from numpy._core.strings import _join as join` 把它接出来用。所以新代码暂不可用 `np.strings.join`，旧入口 `np.char.join` 仍保留。

**练习 2**：如果要给 `numpy.strings` 新增一个公开函数，至少要改哪几个文件？

**参考答案**：至少三处——(1) 在 `numpy/_core/strings.py` 实现，并加入它的 `__all__`；(2) 在门面 `numpy/strings/__init__.pyi` 的 import 列表和 `__all__` 里补上（门面 `__init__.py` 用 `import *`，通常不用改）；(3) 补测试。如果它取代了某个 `_vec_string` 函数，还要在分组注释里挪动它的归类。

---

## 5. 综合实践

把本讲的四条线索串起来，完成一个「门面考古」小任务：

**任务**：写一个脚本 `facade_audit.py`，对 `numpy.strings` 做一次完整审计，输出一份小报告，包含以下五个检查项：

1. **门面纯净度**：打印 `inspect.getsource(numpy.strings)`，确认只有 `import`，没有 `def`。
2. **对象一致性**：对 `numpy.strings.__all__` 里每个名字，断言 `getattr(np.strings, name) is getattr(np._core.strings, name)`，统计不一致数量（应为 0）。
3. **公开符号数**：用 `dir()` 过滤下划线后计数（应为 46），并与 `.pyi` 的 `__all__`（硬编码或读取文件解析）做差集。
4. **懒加载验证**：访问 `np.strings` 前后检查 `sys.modules`，确认它是按需加载的。
5. **与 char 的差异**：列出 `numpy.char` 比 `numpy.strings` 多出的公开符号。

参考框架（你需要自行补全）：

```python
# 示例代码：facade_audit.py 框架
import inspect, sys
import numpy as np
import numpy.strings as ns
import numpy._core.strings as core
import numpy.char as nc

print("【1】门面源码:")
print(inspect.getsource(ns))

mismatch = [n for n in ns.__all__ if getattr(ns, n) is not getattr(core, n)]
print("【2】对象不一致数量:", len(mismatch))

public = sorted(n for n in dir(ns) if not n.startswith("_"))
print("【3】公开符号数:", len(public))

before = "numpy.strings" in sys.modules
_ = np.strings
after = "numpy.strings" in sys.modules
print(f"【4】懒加载: 访问前={before}, 访问后={after}")

char_extra = sorted(set(dir(nc)) - set(dir(ns)))
print("【5】char 多出:", [n for n in char_extra if not n.startswith("_")])
```

**完成标准**：报告里第 2 项为 0、第 3 项为 46、第 4 项访问后为 `True`、第 5 项包含 `join`/`split` 等。能跑通这份报告，说明你已经真正理解了门面、懒加载与三模块关系。

> 提示：这是一份**源码阅读 + 运行验证**结合的综合实践。如果某些项在你的环境下数字略有出入，以实际输出为准并思考原因（版本差异、环境预热等）。

## 6. 本讲小结

- `numpy.strings` 是 NumPy 向量化字符串运算的**官方公开命名空间**，新代码应优先使用它。
- 它是一个**纯门面**：`numpy/strings/__init__.py` 只有 2 行，靠 `from numpy._core.strings import *` 转发全部符号，自己不实现任何函数。
- 门面里转发的函数对象，和 `numpy._core.strings` 里的是**同一个对象**（`is` 为真）。
- `.pyi` 类型存根**逐个显式**列出了 46 个公开符号，是读懂门面接口的最佳清单；改门面要同步改 stub。
- `np.strings` 是**懒加载**的：顶层 `numpy/__init__.py` 的模块级 `__getattr__` 在首次访问时才真正 import。
- `numpy.char`（`defchararray.py`）反过来 `from numpy.strings import *`，并额外补回 `join/split/...` 等历史函数，是兼容旧代码的「老大哥」。

## 7. 下一步学习建议

理解了门面之后，下一步应该**走进门面背后**，看实现层 `_core/strings.py` 到底是怎么把函数造出来的。建议：

- 先读本系列 **u1-l2《三种字符串 dtype 与字符计数》**：搞清楚 `numpy.strings` 操作的三种输入类型（变长 `StringDtype('T')`、定长 `bytes_('S')`、定长 `str_('U')`），这是后续所有函数分支判断的基础。
- 再读 **u1-l3《numpy.strings 与 numpy.char 的关系》**：深入 `defchararray.py`，弄清 `char` 额外提供的能力和「比较前 strip 尾部空白」等历史行为差异。
- 进阶时进入 **u2-l4《装饰器与分发机制》**，看 `@set_module` 与 `@array_function_dispatch` 如何把一个普通函数包装成「属于 `numpy.strings` 的公开 API」。

建议你在继续之前，先把本讲第 5 节的综合实践跑通——它会把本讲的所有概念固化成你可以随时复查的证据。
