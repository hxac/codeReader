# numpy.typing 是什么：静态类型与 NumPy

## 1. 本讲目标

本讲是整本《numpy.typing 学习手册》的第一篇，面向**零基础**读者。读完后你应当能够：

- 说清楚「静态类型检查」和「运行时行为」的区别。
- 知道 `numpy.typing` 这个子模块是干什么的、为什么 NumPy 要单独做一套类型子系统。
- 读懂 `numpy/typing/__init__.py` 顶部那段长长的模块文档，并理解文档里强调的「类型系统比运行时更严格」的若干取舍。
- 能写一个带类型注解的小函数，并用 `mypy`（或 `pyright`）跑一次、看懂它的输出。

本讲**只做全局认知**，不深入具体别名（`ArrayLike`/`DTypeLike`/`NDArray`）的内部构造——那是后续讲义的内容。本讲对应的最小模块只有两个：**`numpy.typing`** 与 **PEP 484**。

---

## 2. 前置知识

在开始之前，建议你已经具备以下几点基础（不熟也没关系，下面会用通俗的话再讲一遍）：

- **会写一点 Python**：知道 `def`、`import`、`list`、`tuple` 怎么用。
- **用过 NumPy**：至少知道 `np.array([1, 2, 3])` 能造一个数组，知道 `np.dtype` 大致表示「数组的元素类型」。
- **听说过「类型注解」**：比如 `def add(a: int, b: int) -> int:`。即使你只是见过、没深究，本讲也会带你入门。

### 什么是「静态类型检查」？

Python 默认是**动态类型**语言：你写 `x = 1`，`x` 就是整数；下一行写 `x = "hello"`，`x` 又变成字符串。解释器在**运行那一刻**才知道 `x` 到底是什么。

「静态类型检查」是指：**在程序还没运行之前**，用一个专门工具（如 `mypy`、`pyright`）去读你的代码和注解，提前发现像「把字符串传给了只接受整数的函数」这类错误。

> 类比：静态类型检查像「写作文前的语法检查器」，运行时行为像「上台演讲时观众的真实反应」。两者关注的时间点不同。

### 什么是 PEP 484？

PEP 是 Python Enhancement Proposal（Python 增强提案）的缩写，可以理解成 Python 官方的「设计规范文档」。**PEP 484** 就是那份规定了「Python 类型注解长什么样」的规范，比如：

- 函数参数和返回值怎么标注：`def f(x: int) -> str:`
- `list[int]`、`tuple[int, ...]` 这种泛型怎么写。
- `Optional`、`Union`、`Any` 等工具怎么用。

PEP 484 规定的是**注解的写法和语义**，它本身不强制你用某个检查工具。`numpy.typing` 就是 NumPy 按照 PEP 484 的规范，给自己写的「官方类型定义包」。

---

## 3. 本讲源码地图

本讲只涉及**一个**源码文件，但它信息量很大，尤其是顶部那一大段文档字符串。

| 文件 | 作用 |
| --- | --- |
| [numpy/typing/\_\_init\_\_.py](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L1-L217) | `numpy.typing` 子模块的入口。顶部是一段面向用户的说明文档，下面是 4 个公共别名的导入、模块级 `__getattr__`/`__dir__`、文档拼接和测试入口。本讲主要阅读它的「文档」与「公共 API 声明」部分。 |

> 提示：`numpy.typing` 对外只暴露 4 个名字，真正的实现藏在私有的 `numpy._typing` 里。这种「薄壳」结构会在下一篇讲义（u1-l2）里专门讲解，本讲先建立一个直觉即可。

---

## 4. 核心概念与源码讲解

本讲拆成三个最小模块来讲解：

1. **静态类型检查 vs 运行时行为**（对应最小模块「PEP 484」）
2. **`numpy.typing` 是什么**（对应最小模块「numpy.typing」）
3. **类型系统比运行时更严格的设计取舍**

---

### 4.1 静态类型检查 vs 运行时行为

#### 4.1.1 概念说明

先厘清一对最容易混淆的概念：

- **运行时行为（runtime）**：代码真正跑起来时发生的事，由 Python 解释器和 NumPy 库决定。比如 `np.array([1, 2, 3])` 运行时一定真的会造出一个数组。
- **静态类型检查（static type checking）**：代码**还没运行**时，类型检查工具根据注解做的「推演」。它只看类型、不真正执行代码。

关键点：**这两者可能不一致**。一段代码在运行时完全合法，但类型检查器可能会报错——这往往不是 bug，而是作者**故意**让类型定义更严格，以避免写出危险的用法。`numpy.typing` 的核心设计理念之一，就是「类型比运行时更严格」。

#### 4.1.2 核心流程

一个典型的「使用 `numpy.typing` 做静态检查」的流程如下：

1. 你在代码里写类型注解，比如 `def f(x: npt.ArrayLike) -> npt.NDArray: ...`。
2. 你**不运行**这段代码，而是用 `mypy your_script.py` 让检查器去读。
3. 检查器加载 NumPy 提供的类型定义（来自 `numpy.typing`），按 PEP 484 规则推演。
4. 如果它发现「传入的类型」与「声明的类型」对不上，就报错或警告。
5. 真正运行时，Python 解释器**几乎不管**这些注解（注解主要供工具阅读，运行时不强制）。

用伪代码表示这个分工：

```
# 注解只给检查器看 ↓
def f(x: "只接受数组") -> "返回数组": ...
        │                          │
        └── mypy/pyright 在这里检查 ─┘

# 运行时 ↓
f([1, 2, 3])   # 解释器：注解是什么？我不关心，直接执行
```

#### 4.1.3 源码精读

`numpy/typing/__init__.py` 的模块文档开头就点明了它遵循 PEP 484：

[numpy/typing/\_\_init\_\_.py:8-10](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L8-L10) —— 说明「NumPy API 的大部分都有 PEP 484 风格的类型注解，并提供了一批类型别名」：

```
Large parts of the NumPy API have :pep:`484`-style type annotations. In
addition a number of type aliases are available to users, most prominently
the two below:
```

这段话是整本手册的「总纲」：它告诉你 NumPy 的类型注解是按 **PEP 484** 标准来的，并提供了 `ArrayLike`（可转为数组的对象）和 `DTypeLike`（可转为 dtype 的对象）两个最常用的别名。

[numpy/typing/\_\_init\_\_.py:6](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L6) 中的 `.. versionadded:: 1.20` 标注说明：`numpy.typing` 是从 **NumPy 1.20** 版本开始引入的。这也回答了学习目标里「自 1.20 引入」的背景。

#### 4.1.4 代码实践

> 这个实践帮你**亲眼看到**「注解影响的是检查器，而不是运行时」。

1. **实践目标**：感受注解只被类型检查器读取、运行时不强制。
2. **操作步骤**：
   - 新建文件 `demo_runtime.py`，写入下面的代码（**示例代码**，非项目原有代码）：
     ```python
     import numpy.typing as npt

     # 注解声明：只接受 ArrayLike，返回 NDArray
     def square(x: "npt.ArrayLike") -> "npt.NDArray":
         return x * 2  # 故意写得「不合注解」：传入标量也照算

     # 运行时：解释器完全不在意注解，直接执行
     print(square(3))
     ```
   - 直接运行：`python demo_runtime.py`。
3. **需要观察的现象**：程序**正常打印 `6`**，没有任何报错——即使我们把注解写得好像「只接受数组」，传一个整数 `3` 进去照样能跑。
4. **预期结果**：这恰好证明「注解不影响运行时」。整数 `3` 在运行时满足 `x * 2`，所以输出 `6`。
5. 是否会被类型检查器挑刺？这一步**待本地验证**：用 `mypy demo_runtime.py` 跑一次，记录它对 `square(3)` 的提示（具体报错文本依赖你本地的 numpy/mypy 版本，请以实际输出为准）。

#### 4.1.5 小练习与答案

- **练习 1**：把 `square(3)` 改成 `square("abc")` 并运行，会发生什么？这能说明注解的作用吗？
  - **参考答案**：运行时会打印 `"abcabc"`（字符串乘 2 是重复），依然不报错。这说明**运行时**完全不管注解；注解的作用要靠 `mypy` 这类工具在运行前去检查。
- **练习 2**：PEP 484 规定的是「注解的写法」，还是「某个具体的检查工具」？
  - **参考答案**：PEP 484 规定的是**写法和语义规范**（怎么标注、泛型怎么用），它不指定具体工具。`mypy`、`pyright` 都是遵循这套规范的实现。

---

### 4.2 numpy.typing 是什么

#### 4.2.1 概念说明

`numpy.typing` 是 NumPy 官方提供的**类型子系统**。你可以把它理解成 NumPy 附带的一本「类型说明书」：

- 它告诉你：一个函数到底接受什么类型的参数、返回什么类型。
- 它提供一批**类型别名**（type alias），让你写注解时更省事。比如与其啰嗦地描述「任何能转成数组的东西」，不如直接写 `npt.ArrayLike`。

为什么科学计算库特别需要它？因为 NumPy 的 API **极其灵活**——同一个函数能接受列表、元组、标量、其它数组、甚至实现了 `__array__` 方法的自定义对象。这种灵活性对运行时很方便，但给「静态描述类型」带来了巨大挑战。`numpy.typing` 的任务就是把这些灵活的输入**归纳**成几个有意义的类型别名。

#### 4.2.2 核心流程

`numpy.typing` 对外只暴露 **4 个名字**：

| 名字 | 一句话作用 |
| --- | --- |
| `ArrayLike` | 「任何能被转成数组」的对象类型。 |
| `DTypeLike` | 「任何能被转成 dtype」的对象类型。 |
| `NBitBase` | 表示数值精度的类型层次（**已在 2.3 弃用**，见 4.3 节）。 |
| `NDArray` | 数组类型的便捷别名，可带形状与元素类型。 |

它们的工作方式可以概括为：

```
用户代码            numpy.typing（公共壳）        numpy._typing（私有实现）
npt.ArrayLike  ──>  从 __all__ 导出         <──>  真正的别名定义藏在这里
```

即：`numpy.typing` 是一层很薄的「公共壳」，真正的定义在私有的 `numpy._typing` 里。本讲只建立这个直觉，细节留给 u1-l2。

#### 4.2.3 源码精读

公共别名的「来源」就在这一行导入：

[numpy/typing/\_\_init\_\_.py:175](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L175) —— 从私有模块 `numpy._typing` 把 4 个别名导进来：

```
from numpy._typing import ArrayLike, DTypeLike, NBitBase, NDArray
```

紧接着用 `__all__` 声明对外只暴露这 4 个名字：

[numpy/typing/\_\_init\_\_.py:177](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L177)：

```
__all__ = ["ArrayLike", "DTypeLike", "NBitBase", "NDArray"]
```

`__all__` 是 Python 的约定：它列出的名字，是 `from numpy.typing import *` 时真正会被导出的名字。这就划清了「公共 API」与「内部实现」的边界——尽管实现来自私有的 `numpy._typing`，但用户只需要记住这 4 个公共名字。

文件末尾还有两处值得知道的工程细节（本讲只需了解，深入留到 u5）：

- [numpy/typing/\_\_init\_\_.py:207-211](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L207-L211)：把额外生成的文档字符串拼接到模块 docstring 上（因为 PEP 695 的 `type` 语句本身没法挂 `__doc__`，需要「手动」补文档）。
- [numpy/typing/\_\_init\_\_.py:213-216](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L213-L216)：挂载了一个 `test = PytestTester(__name__)`，使得 `numpy.typing.test()` 可以直接跑本模块的测试。

#### 4.2.4 代码实践

> 这个实践帮你确认「公共 API 只有 4 个名字」，并体会公共壳与私有实现的关系。

1. **实践目标**：列出 `numpy.typing` 的公共名字，并与私有模块对比。
2. **操作步骤**：新建 `explore_api.py`（**示例代码**）：
   ```python
   import numpy.typing as npt
   import numpy._typing as _npt_priv

   print("公共 __all__:", npt.__all__)
   # 数一下私有模块里多出多少内部名字（以下划线开头的「内部」别名）
   priv = [k for k in dir(_npt_priv) if not k.startswith("__")]
   print("私有模块公开名数量:", len(priv))
   ```
3. **需要观察的现象**：`__all__` 恰好是那 4 个名字；私有模块里的名字明显更多（包含各种 `_` 开头的内部别名）。
4. **预期结果**：`npt.__all__` 输出 `['ArrayLike', 'DTypeLike', 'NBitBase', 'NDArray']`。
5. **待本地验证**：私有模块公开名的**具体数量**取决于你本地 numpy 版本，请以实际 `len(priv)` 为准。

#### 4.2.5 小练习与答案

- **练习 1**：`__all__` 不写会怎样？
  - **参考答案**：不写 `__all__` 时，`from module import *` 会导出所有不以 `_` 开头的全局名字，容易把内部实现泄漏出去。写 `__all__` 是显式锁定公共 API 的好习惯，`numpy.typing` 正是这样做的。
- **练习 2**：为什么 `ArrayLike` 的定义放在私有的 `numpy._typing`，而不是直接写在 `numpy/typing/__init__.py`？
  - **参考答案**：这是「公共壳 + 私有实现」的分层设计。公共入口保持稳定和简洁，复杂的别名构造细节藏在私有模块里，方便内部演进而不破坏用户接口（详见 u1-l2）。

---

### 4.3 类型系统比运行时更严格的设计取舍

#### 4.3.1 概念说明

这是 `numpy.typing` 最重要的一条理念，文档专门用一节来强调：

> The typed NumPy API is often **stricter than** the runtime NumPy API.

为什么会「更严格」？因为 NumPy 运行时太灵活了——很多「能跑但很危险/容易出 bug」的写法，如果都允许进类型系统，那类型定义会变得极其复杂、失去指导意义。所以 `numpy.typing` 选择**主动拒绝**一些运行时合法、但不推荐的写法。

文档列举了几个典型差异，本讲挑最直观的几个让你建立印象（每个差异的细节会在后续相关讲义展开）。

#### 4.3.2 核心流程

`numpy.typing` 「故意收紧」的几类典型情况：

| 运行时合法但类型系统拒绝的场景 | 为什么收紧 |
| --- | --- |
| 用生成器造数组（会得到 `object` 数组） | `object` 数组性能差、语义混乱，应避免 |
| 用「字段字典」造 dtype | 该写法已被官方劝退，易出错 |
| 把 `timedelta64` 当成 `signedinteger` 的子类 | 运行时是子类，但静态检查时不这么认为 |
| `recarray` 同时传 `dtype` 和 `formats` 等参数 | 两种指定方式互斥，混用会有 bug |

数值精度那块则用了另一套思路：把精度当成「**不变（invariant）的泛型参数**」来处理，从而能精确表达「输入精度与输出精度的关系」。本讲先记住这个结论，细节在 u4 系列。

#### 4.3.3 源码精读

文档明确给出了「更严格」的总纲：

[numpy/typing/\_\_init\_\_.py:17-24](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L17-L24) —— 解释 NumPy 太灵活，完整描述会让类型变得没用，所以类型化 API 通常更严格：

```
NumPy is very flexible. Trying to describe the full range of
possibilities statically would result in types that are not very
helpful. For that reason, the typed NumPy API is often stricter than
the runtime NumPy API.
```

**差异一：ArrayLike 避免 object 数组**——

[numpy/typing/\_\_init\_\_.py:26-53](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L26-L53)：文档给出一个运行时合法、但类型检查器会抱怨的例子——把生成器传给 `np.array` 会造出 0 维 `object` 数组：

```
>>> np.array(x**2 for x in range(10))
array(<generator object <genexpr> at ...>, dtype=object)
```

并给出两种「绕过」方式：加 `# type: ignore` 注释，或显式把变量标注为 `typing.Any`。

**差异二：DTypeLike 避免字段字典**——

[numpy/typing/\_\_init\_\_.py:55-67](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L55-L67)：用字典定义字段（如 `np.dtype({"field1": (float, 1), ...})`）运行时合法，但因「不推荐使用」而被类型系统拒绝。

**差异三：数值精度作为不变泛型参数**——

[numpy/typing/\_\_init\_\_.py:69-88](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L69-L88)：说明 `numpy.number` 子类的精度被当作**不变泛型参数**，并给出示例：

```
T = TypeVar("T", bound=npt.NBitBase)
def func(a: np.floating[T], b: np.floating[T]) -> np.floating[T]:
    ...
```

同时还点出一个微妙差异：`float16`/`float32`/`float64` 运行时是 `floating` 的子类，但在静态类型检查里**不一定**被当成子类。

**重要：NBitBase 已在 2.3 弃用**——

[numpy/typing/\_\_init\_\_.py:90-104](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L90-L104)：文档用 `.. deprecated:: 2.3` 明确标注 `NBitBase` 已弃用，推荐改用「以具体标量类为上界的 `TypeVar`」来表达精度关系：

```
S = TypeVar("S", bound=np.floating)
def func(a: S, b: S) -> S:
    ...
```

> 这是本讲的「时效性提醒」：你在网上看到的许多旧教程仍用 `NBitBase`，但当前版本已不推荐。访问 `npt.NBitBase` 时还会触发 `DeprecationWarning`（见 [numpy/typing/\_\_init\_\_.py:187-199](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L187-L199) 的模块级 `__getattr__`）。

**差异四：Timedelta64**——

[numpy/typing/\_\_init\_\_.py:122-127](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L122-L127)：`timedelta64` 在静态检查时**不被**视为 `signedinteger` 的子类（运行时则是），它只继承自 `generic`。

**差异五：Record array 的两种 dtype 指定方式互斥**——

[numpy/typing/\_\_init\_\_.py:129-143](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L129-L143)：`recarray` 可以用 `dtype=` 直接指定，也可以用 `formats`/`names`/`titles`/`aligned`/`byteorder` 这组参数（经 `numpy.rec.format_parser`）指定。类型系统把它们标注为**互斥**——指定了 `dtype` 就不能再给 `formats`，因为运行时混用会出 bug。

#### 4.3.4 代码实践

> 这个实践让你**亲眼看到**类型系统如何拒绝一个「运行时合法」的写法。

1. **实践目标**：复现文档里「生成器造 object 数组」的例子，对比运行时与类型检查器的反应。
2. **操作步骤**：新建 `strict_demo.py`（**示例代码**）：
   ```python
   import numpy as np
   import numpy.typing as npt

   def make_arr(x: "npt.ArrayLike") -> "npt.NDArray":
       return np.array(x)

   # 运行时合法：会造出 object 数组
   g = (i ** 2 for i in range(10))
   print(make_arr(g))
   ```
   - 先运行：`python strict_demo.py`，确认它能跑通并打印出 `object` 数组。
   - 再检查：`mypy strict_demo.py`，记录类型检查器对 `make_arr(g)` 的提示。
3. **需要观察的现象**：**运行时**一切正常（打印出 object 数组）；但**类型检查器**会对「把生成器当作 `ArrayLike`」提出警告/报错——这正是「类型比运行时更严格」的体现。
4. **预期结果**：运行时打印类似 `array(<generator object ...>, dtype=object)` 的内容（与文档 [L34-L35](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L34-L35) 一致）。
5. **待本地验证**：`mypy` 的**具体报错文本**依赖本地版本，请以实际输出为准。若你想让检查器「闭嘴」，可按文档 [L44](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L44) 的指引加 `# type: ignore`。

#### 4.3.5 小练习与答案

- **练习 1**：为什么 `numpy.typing` 要主动拒绝「生成器 → object 数组」这种运行时合法的写法？
  - **参考答案**：因为 `object` 数组性能差、行为容易让人困惑（它装的是 Python 对象而非数值），属于「能跑但不推荐」。类型系统主动拒绝，是为了在写代码阶段就把这类隐患挡住，而不是等运行时出问题。
- **练习 2**：`NBitBase` 在当前版本（2.3 起）处于什么状态？官方推荐用什么替代？
  - **参考答案**：已**弃用（deprecated）**。官方推荐改用以具体标量类为上界的 `TypeVar`（如 `S = TypeVar("S", bound=np.floating)`）或 `typing.overload` 来表达精度关系。

---

## 5. 综合实践

把本讲的三个知识点串起来，完成下面这个小任务：

**任务**：写一个「带类型注解的归一化函数」，并体会「注解 vs 运行时」「公共 API」「类型更严格」三件事。

1. 新建 `normalize.py`（**示例代码**）：
   ```python
   from typing import reveal_type
   import numpy as np
   import numpy.typing as npt

   def normalize(x: "npt.ArrayLike") -> "npt.NDArray":
       arr = np.asarray(x, dtype=np.float64)
       return (arr - arr.min()) / (arr.max() - arr.min())

   data = [0.0, 5.0, 10.0]
   out = normalize(data)
   reveal_type(out)
   print(out)
   ```
2. **运行** `python normalize.py`，记录打印结果（应为归一化后的数组 `[0. , 0.5, 1. ]`）。
3. **检查** `mypy normalize.py`：
   - 观察 `reveal_type(out)` 被 mypy 解析出的类型（预期与 `npt.NDArray` 相关）——这体现「注解被检查器读取」。
   - 故意把 `data` 换成生成器 `(i for i in data)` 再跑一次 mypy，观察检查器是否抱怨——这体现「类型比运行时更严格」。
4. **反思**（写在你的学习笔记里）：
   - 这段代码运行时和静态检查分别关注了什么？
   - `npt.ArrayLike` / `npt.NDArray` 这两个公共名字，是从哪个私有模块导入的？（提示：回看 [L175](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L175)）。
5. **预期结果 / 待本地验证**：归一化数值可手算确认无误；mypy 对 `reveal_type` 的具体输出文本依赖本地版本，以实际为准。

---

## 6. 本讲小结

- **静态类型检查**在代码运行前进行，关注类型是否匹配；**运行时行为**是真正执行时发生的事。两者可能不一致。
- `numpy.typing` 是 NumPy 自 **1.20** 引入的官方类型子系统，遵循 **PEP 484** 规范。
- 它对外只暴露 **4 个公共名字**：`ArrayLike`、`DTypeLike`、`NBitBase`、`NDArray`（声明在 [L177](https://github.com/numpy/numpy/blob/9559a6b1ac93610711d8f1243f8c949fca4420bb/numpy/typing/__init__.py#L177) 的 `__all__`）。
- 真正的实现藏在私有的 `numpy._typing` 里，`numpy.typing` 只是一层「公共壳」。
- 核心理念：**类型化 API 比运行时 API 更严格**——它会主动拒绝「能跑但不推荐」的写法（如 object 数组、字段字典 dtype 等）。
- **时效提醒**：`NBitBase` 自 **2.3** 起已弃用，推荐改用以标量类为上界的 `TypeVar` 或 `@overload`。

---

## 7. 下一步学习建议

本讲只建立了全局认知，建议接下来：

1. **u1-l2《公共 API 与目录结构：public 壳与 private 实现》**：深入 `numpy._typing` 私有模块，搞清楚公共壳与私有实现的分层与聚合导入。
2. **u1-l3《PEP 561 类型分发：py.typed 与 .pyi 桩文件》**：了解 NumPy 如何通过 `py.typed` 标记和 `.pyi` 桩文件让类型定义随包安装、被检查器识别。
3. 在阅读后续讲义前，建议先把本讲的综合实践跑通，确认你已能区分「注解影响检查器」与「运行时不强制」这两件事。
