# matrixlib 全景与 np.matrix 的定位

## 1. 本讲目标

本讲是 numpy `matrixlib` 子包学习手册的第一篇。读完本讲，你应该能够：

- 说清楚 `numpy.matrixlib` 这个子包里到底有什么、它对外只暴露哪几个名字。
- 理解 `np.matrix` 是一个「强制二维」的 `ndarray` 子类，以及它为什么被官方标记为「不再推荐使用」。
- 知道每次创建 `matrix` 时，源码里发出的那个 `PendingDeprecationWarning` 是什么意思、为什么平时看不到它、以及怎么捕获它。

本讲只做「全景认识」和「定位」，**不**深入构造函数内部细节、字符串解析、运算符重写等内容——那些是后续进阶讲义的主题。

## 2. 前置知识

在读本讲之前，你最好已经具备：

- 基本的 Python 语法（`import`、类与继承、异常）。
- 会用 `numpy` 创建普通数组：`np.array([[1, 2], [3, 4]])`，知道 `ndarray`、`shape`、`ndim`（维度数）这些概念。
- 知道线性代数里的「矩阵」和「向量」大致是什么。

下面几个术语本讲会用到，先做个一句话解释：

| 术语 | 一句话解释 |
| --- | --- |
| `ndarray` | numpy 里最核心的多维数组类型，可以是 0/1/2/…任意维。 |
| 子类（subclass） | `matrix` 继承自 `ndarray`，所以它「是一个」`ndarray`，但改写了部分行为。 |
| 二维（2-D） | 形状只有两个轴，比如 `(2, 3)`。矩阵永远是二维的。 |
| 弃用（deprecation） | 某个功能官方不再建议使用，未来某个版本可能会删除。 |
| 警告（warning） | Python 里一种「不中断程序、只是提醒」的机制，用 `warnings` 模块发出。 |

如果你对「警告」机制不熟，不用慌，本讲第 4.3 节会专门讲。

## 3. 本讲源码地图

`matrixlib` 是一个很小的子包，本讲只涉及两个核心源码文件：

| 文件 | 作用 |
| --- | --- |
| `numpy/matrixlib/__init__.py` | 子包入口。定义模块文档字符串，并从 `defmatrix` 转发导出列表，还挂了一个测试入口。 |
| `numpy/matrixlib/defmatrix.py` | 子包的真正实现。定义了 `matrix` 类、`asmatrix`、`bmat`，以及一系列辅助函数。 |

为了对照，下面是这个子包在当前 HEAD 下的完整文件清单（来自 `git ls-files`）：

```
numpy/matrixlib/
├── __init__.py            # 本讲重点
├── __init__.pyi           # 类型存根（本讲不展开）
├── defmatrix.py           # 本讲重点
├── defmatrix.pyi          # 类型存根（本讲不展开）
└── tests/
    ├── __init__.py
    ├── test_defmatrix.py
    ├── test_interaction.py
    ├── test_masked_matrix.py
    ├── test_matrix_linalg.py
    ├── test_multiarray.py
    ├── test_numeric.py
    └── test_regression.py
```

可以看到：整个子包的「业务代码」几乎全集中在 `defmatrix.py` 一个文件里，`__init__.py` 只负责装配和导出。这也是为什么 `matrixlib` 是学习「`ndarray` 子类化」的一个非常干净的样本——代码体量小、自包含。

## 4. 核心概念与源码讲解

本讲围绕四个最小模块展开：**模块文档字符串**、**`__all__`**、**`matrix` 类**、**`PendingDeprecationWarning`**。为了叙述连贯，我把它们组织成三节：先看子包的边界（文档字符串 + `__all__`），再看 `matrix` 类的定位，最后看它发出的弃用警告。

### 4.1 子包的范围：模块文档字符串与 `__all__`

#### 4.1.1 概念说明

一个 Python 包对外暴露什么，通常由两样东西决定：

1. **模块文档字符串**（module docstring）：写在文件最开头的三引号字符串，是给人看的「这个模块是干嘛的」说明书。
2. **`__all__`**：一个列表，告诉 Python「执行 `from 模块 import *` 时，应该导入哪些名字」。它同时也充当「公开 API 清单」——不在 `__all__` 里的名字，约定上就是内部实现，使用者不应依赖。

对于 `matrixlib`，这两样东西合在一起回答了一个关键问题：**这个子包到底对外提供哪些东西？** 答案非常简洁：只有 `matrix`、`bmat`、`asmatrix` 三个名字。

#### 4.1.2 核心流程

`matrixlib` 的导出是「两级转发」的：

```
defmatrix.py 定义 __all__ = ['matrix', 'bmat', 'asmatrix']
        │
        │  （__init__.py 执行）
        ▼
__init__.py: from .defmatrix import *
__init__.py: __all__ = defmatrix.__all__   # 直接照搬
        │
        │  （顶层 numpy/__init__.py 执行）
        ▼
np.matrix / np.asmatrix / np.bmat 成为顶层名字
```

也就是说，`__init__.py` 自己并不重新声明公开名字，而是**原样转发** `defmatrix` 的 `__all__`。这保证了两处的公开清单永远一致，不会出现「子包导出了、入口却忘了」的脱节。

#### 4.1.3 源码精读

先看子包入口 `__init__.py` 的全部内容（它本身就只有十几行）：

文档字符串和转发导出：
[__init__.py:1-7](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/__init__.py#L1-L7) —— 第 1-3 行是模块文档字符串（「Sub-package containing the matrix class and related functions.」），第 5 行 `from .defmatrix import *` 把 `defmatrix` 里的公开名字引入子包命名空间，第 7 行 `__all__ = defmatrix.__all__` 把公开清单原样转发。

再挂一个测试入口（后续讲义会用到）：
[__init__.py:9-12](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/__init__.py#L9-L12) —— 引入 `PytestTester` 并创建 `test = PytestTester(__name__)`，所以你能用 `np.matrixlib.test()` 直接跑这个子包的测试。`del PytestTester` 是为了不把这个工具类本身留在子包命名空间里。

真正的公开清单定义在 `defmatrix.py` 的第一行：
[defmatrix.py:1-1](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L1-L1) —— `__all__ = ['matrix', 'bmat', 'asmatrix']`。这一行就是整个子包对外的「全部承诺」：三个名字，没有更多。

> 小提示：`defmatrix` 这个名字以下划线开头吗？并没有——它叫 `defmatrix` 而非 `_defmatrix`。这是历史遗留：这个模块虽然名字看着像「definition of matrix」，但 numpy 一直把它当作可被 `from numpy.matrixlib.defmatrix import matrix` 这样引用的模块。我们遵循 `__all__` 来判断公开 API 即可。

#### 4.1.4 代码实践

**实践目标**：亲眼确认 `matrixlib` 子包只暴露三个名字，并且它们就是 `matrix`/`asmatrix`/`bmat`。

**操作步骤**：

```python
import numpy as np

# 1. 看子包的 __all__
print(np.matrixlib.__all__)
# 预期: ['matrix', 'bmat', 'asmatrix']

# 2. 看这三个名字在顶层是否都能访问到
for name in ['matrix', 'asmatrix', 'bmat']:
    print(name, '->', getattr(np, name))
```

**需要观察的现象**：第 1 步打印出的列表恰好是三个名字；第 2 步说明这三个名字不仅是子包内部的名字，也被挂到了顶层 `np` 命名空间（这一步是因为顶层 `numpy/__init__.py` 里有一行 `from .matrixlib import asmatrix, bmat, matrix`，把子包的名字抬到了顶层）。

**预期结果**：

```
['matrix', 'bmat', 'asmatrix']
matrix -> <class 'numpy.matrix'>
asmatrix -> <built-in function asmatrix>
bmat -> <built-in function bmat>
```

（函数/类的具体 repr 文本可能因 numpy 版本略有差异，但 `matrix` 是一个 class、`asmatrix`/`bmat` 是 function 这一点是稳定的。）

#### 4.1.5 小练习与答案

**练习 1**：如果在 `defmatrix.py` 里有人新加了一个公开函数 `foo`，但忘了把它写进 `__all__`，那么 `from numpy.matrixlib import foo` 还能成功吗？

**参考答案**：能成功。`__all__` 只影响 `from 模块 import *`（星号导入）以及「哪些算公开 API」的约定，**不影响**显式写出名字的 `from ... import foo`。但按惯例，`foo` 不在 `__all__` 里就意味着它不算公开承诺，使用者不应依赖它。

**练习 2**：为什么 `__init__.py` 要写 `__all__ = defmatrix.__all__`，而不是自己再手写一遍 `['matrix', 'bmat', 'asmatrix']`？

**参考答案**：为了避免「两处清单不同步」。转发赋值保证只要 `defmatrix.py` 的 `__all__` 更新了，子包入口的公开清单自动跟着更新，永远不会脱节。这是一种「单一数据源（single source of truth）」的写法。

---

### 4.2 `matrix` 类：强制二维的 `ndarray` 子类

#### 4.2.1 概念说明

`np.matrix` 的本质，是一个**继承自 `np.ndarray`、并且强制保持二维**的子类。这句话有两个关键点：

1. **它「是一个」ndarray**：因为继承关系，`matrix` 拥有 `ndarray` 的绝大部分能力（索引、广播、`dtype`、各种 `ufunc`……）。
2. **它强制二维**：不管你怎么造它，结果永远是 `(行, 列)` 两个轴。即使是单个数字，也会变成 `(1, 1)`；一个一维列表会变成 `(1, N)` 的一行；超过二维（比如三维）会直接报错。

为什么要有这样一个类？历史上，`matrix` 是为了让从 MATLAB 迁移过来的用户感到熟悉：在 MATLAB 里 `*` 就是矩阵乘法。所以 `matrix` 还顺手把 `*` 重定义成了矩阵乘法、把 `**` 重定义成了矩阵幂。这些重写是后续进阶讲义的重点，这里你只需要知道「它和普通数组的行为不一样」。

而今天，numpy 官方**不再推荐**使用 `matrix`，类文档里明确写着一段 `.. note::`：

> It is no longer recommended to use this class, even for linear algebra. Instead use regular arrays. The class may be removed in the future.

原因后面 4.3 节会展开。先记住结论：**`matrix` = 一个被官方劝退的、强制二维的 `ndarray` 子类**。

#### 4.2.2 核心流程

`matrix` 的「强制二维」是在构造时（`__new__`）和每次产生新视图时（`__array_finalize__`）共同保证的。本讲只看构造时最直观的那一段：

```
输入 data
  │
  ├─ 已经是 matrix?  → 处理 dtype/copy，直接返回
  ├─ 是 ndarray?     → 用 .view(matrix) 转成 matrix
  ├─ 是字符串?       → 先解析成嵌套列表
  │
  ▼
转成数组 arr，读取 ndim
  │
  ├─ ndim > 2  → 抛 ValueError("matrix must be 2-dimensional")
  ├─ ndim == 0 → shape 强制为 (1, 1)
  ├─ ndim == 1 → shape 强制为 (1, N)
  └─ ndim == 2 → 保持 (M, N)
```

也就是说，「二维」不是请求、不是建议，而是**硬约束**：超过二维直接拒绝，低于二维自动补成二维。

#### 4.2.3 源码精读

类的定义和继承关系：
[defmatrix.py:73-74](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L73-L74) —— `@set_module('numpy')` 是个装饰器，把 `matrix.__module__` 改写成 `'numpy'`（这样它对外显示成 `numpy.matrix` 而不是 `numpy.matrixlib.defmatrix.matrix`）；`class matrix(N.ndarray):` 这一行就是「`matrix` 继承自 `ndarray`」的铁证。

类文档里的弃用提示：
[defmatrix.py:84-86](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L84-L86) —— 这段 `.. note::` 就是官方劝退语，建议改用普通数组，并预告「未来可能移除」。

类属性 `__array_priority__`：
[defmatrix.py:117-117](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L117-L117) —— `__array_priority__ = 10.0` 比 `ndarray` 默认的优先级高，这让 `matrix` 在和普通数组做二元运算时「获胜」，结果仍是 `matrix`。这一点进阶讲义会细讲，本讲先留个印象。

构造函数里「强制二维」的判定：
[defmatrix.py:153-160](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L153-L160) —— `ndim > 2` 时 `raise ValueError("matrix must be 2-dimensional")`；`ndim == 0` 时把形状改成 `(1, 1)`；`ndim == 1` 时改成 `(1, shape[0])`。这就是「永远二维」在构造阶段的实现。

> 注意：`__new__` 的完整内部逻辑（`copy` 标志、三种输入分支、`buffer` 构造）属于「进阶层：构造函数」讲义的主题。本讲你只要抓住「它会发出警告 + 会强制二维」这两点即可。

#### 4.2.4 代码实践

**实践目标**：用三种不同维度的输入，验证 `matrix` 的「永远二维」约束。

**操作步骤**：

```python
import warnings
import numpy as np

# 临时关掉 4.3 节要讲的弃用警告，免得干扰本练习的观察
warnings.simplefilter("ignore", PendingDeprecationWarning)

print(np.matrix(5).shape)              # 标量 -> (1, 1)
print(np.matrix([1, 2, 3]).shape)      # 一维 -> (1, 3)
print(np.matrix([[1, 2], [3, 4]]).shape)  # 二维 -> (2, 2)

# 故意造一个三维输入
try:
    np.matrix(np.arange(8).reshape(2, 2, 2))
except ValueError as e:
    print("三维输入报错:", e)
```

**需要观察的现象**：标量和一维列表都被「补」成了二维；三维输入直接抛 `ValueError`，且错误信息正是源码里的 `"matrix must be 2-dimensional"`。

**预期结果**：

```
(1, 1)
(1, 3)
(2, 2)
三维输入报错: matrix must be 2-dimensional
```

#### 4.2.5 小练习与答案

**练习 1**：`np.matrix([1, 2, 3])` 和 `np.array([1, 2, 3])` 的 `shape` 分别是什么？为什么不同？

**参考答案**：前者是 `(1, 3)`（被强制补成一行），后者是 `(3,)`（一维，原样保留）。区别的根源就是 `matrix` 在构造时执行了 [defmatrix.py:159-160](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L159-L160) 的 `shape = (1, shape[0])`，而普通 `ndarray` 没有这个步骤。

**练习 2**：下面哪种输入能让 `np.matrix(...)` 成功？为什么？
- A. `np.matrix(np.arange(24).reshape(2, 3, 4))`
- B. `np.matrix('1 2; 3 4')`

**参考答案**：只有 B 成功。A 是三维数组（`ndim == 3`），命中 [defmatrix.py:155-156](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L155-L156) 的 `ndim > 2` 分支，抛 `ValueError`。B 是字符串 `'1 2; 3 4'`，会被解析成二维（2 行 2 列），正常构造。字符串解析的具体规则是另一篇讲义的主题。

---

### 4.3 PendingDeprecationWarning：「即将被弃用」的提醒信号

#### 4.3.1 概念说明

你可能已经发现：上面 4.2.4 的实践里，我特意加了一行 `warnings.simplefilter("ignore", PendingDeprecationWarning)` 来「关掉警告」。这是因为**每次创建 `matrix` 都会发出一个警告**，而这个警告平时你根本看不见。

要理解它，先理清 Python 警告体系里的三个相邻类别（从轻到重）：

| 类别 | 含义 | 默认是否显示 |
| --- | --- | --- |
| `FutureWarning` | 面向**终端用户**的「行为将来会变」提示 | 默认显示 |
| `PendingDeprecationWarning` | 「**将来**会被弃用，但**现在还没**真正弃用」 | 默认**不显示**（被忽略） |
| `DeprecationWarning` | 「已经弃用了，将来会删」 | 默认只对 `__main__` 代码显示 |

关键区别：

- `DeprecationWarning` = 已经决定弃用，通常有明确的移除路线图。
- `PendingDeprecationWarning` = 弃用「在计划中」，但还没下定决心，也没有承诺具体哪个版本删除。它比 `DeprecationWarning` 更「软」。

numpy 对 `matrix` 用的是 **`PendingDeprecationWarning`**，这反映了官方的真实态度：**「我们知道它该退休了，但我们还没定下哪天真的删掉它。」** 这也是为什么你在日常使用 `np.matrix(...)` 时几乎看不到任何提示——这个类别默认被 Python 过滤掉了。

#### 4.3.2 核心流程

发出警告的代码位于 `matrix.__new__` 的**最开头**，且**无条件**执行：

```
调用 np.matrix(...)
        │
        ▼
进入 matrix.__new__(cls, data, dtype, copy)
        │
        ▼  （第一件事，在任何构造逻辑之前）
warnings.warn("the matrix subclass is not the recommended way ...",
              PendingDeprecationWarning, stacklevel=2)
        │
        ▼
才开始处理 data / copy / 强制二维 ...
```

因为 `asmatrix` 内部就是调用 `matrix(...)`、`bmat` 最终也会构造 `matrix`，所以**只要创建出 `matrix` 对象，就一定会触发这条警告**（前提是警告没有被过滤掉）。

要在代码里「抓到」这条警告，标准做法是用 `warnings.catch_warnings(record=True)` 上下文管理器，它会把所有警告捕获成一个列表返回，而不是打印出来。

#### 4.3.3 源码精读

发出警告的那几行：
[defmatrix.py:119-125](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L119-L125) —— `def __new__(cls, data, dtype=None, copy=True):` 之后，第一件事就是 `warnings.warn(...)`，消息里直说「the matrix subclass is not the recommended way to represent matrices or deal with linear algebra」，类别是 `PendingDeprecationWarning`，`stacklevel=2` 是为了让警告指向「调用 `matrix(...)` 的那一行」而不是 `warn` 本身所在行。注意它在任何 `if` 判断**之前**，所以对每一次构造都生效。

为什么官方不推荐 `matrix`？类文档的 note 已经说了「改用普通数组」（见 4.2.3 引用的 [defmatrix.py:84-86](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L84-L86)）。更具体的原因是：

- `matrix` 把 `*` 重定义成了矩阵乘法，这和普通数组「逐元素相乘」的 `*` 不一致；当代码里 `matrix` 与 `ndarray` 混用时，很容易出现「以为在做逐元素运算、实际却在做矩阵乘」的隐蔽 bug。
- `matrix` 只能二维，无法表达 numpy 广泛支持的高维数据。
- 如今普通 `ndarray` 配合 `@` 运算符（矩阵乘）和 `numpy.linalg` 函数，已经能覆盖 `matrix` 的全部用途，而且行为更一致、更通用。

一句话总结：**`matrix` 的运算符重载制造了和普通数组不一致的行为，而普通数组 + `@` + `linalg` 已经是更通用、更安全的替代，所以官方把它标记为「待弃用」。**

#### 4.3.4 代码实践（本讲主实践）

**实践目标**：亲手捕获 `np.matrix('1 2; 3 4')` 发出的警告，断言它确实是 `PendingDeprecationWarning`，并解释为什么官方推荐改用普通 `ndarray`。

**操作步骤**：

```python
import warnings
import numpy as np

# record=True 会把捕获到的 warning 对象放进列表 w
with warnings.catch_warnings(record=True) as w:
    # 关键：让所有警告都被记录，而不是被默认过滤器丢弃
    warnings.simplefilter("always")

    m = np.matrix('1 2; 3 4')

    # 1. 至少捕获到一条警告
    assert len(w) >= 1, "没有捕获到任何警告"

    # 2. 它的类别正是 PendingDeprecationWarning（用 issubclass 更稳）
    assert issubclass(w[0].category, PendingDeprecationWarning), \
        f"警告类别不对，实际是 {w[0].category}"

    print("捕获到的警告类别:", w[0].category.__name__)
    print("警告消息前 40 个字:", w[0].message.args[0][:40])
    print("matrix 内容:\n", m)
    print("matrix 形状:", m.shape)
```

**需要观察的现象**：

- 没有加 `simplefilter("always")` 时，由于 `PendingDeprecationWarning` 默认被过滤，`w` 可能是空的——这正是平时看不到警告的原因。
- 加上 `simplefilter("always")` 后，`w[0].category` 是 `PendingDeprecationWarning`，断言通过。
- 同时能看到 `m` 是 `matrix([[1, 2], [3, 4]])`，形状 `(2, 2)`（字符串 `'1 2; 3 4'` 被「分号分行、空格分列」解析成 2×2 矩阵；字符串解析细节是后续讲义的内容，本讲不必深究）。

**预期结果**：

```
捕获到的警告类别: PendingDeprecationWarning
警告消息前 40 个字: the matrix subclass is not the recommended
matrix 内容:
 [[1 2]
 [3 4]]
matrix 形状: (2, 2)
```

> 说明：本实践基于对 [defmatrix.py:119-125](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L119-L125) 的源码阅读推断出预期行为，未由本讲义作者在本机实跑；不同 numpy 版本的警告消息措辞可能略有差异，但类别 `PendingDeprecationWarning` 是稳定的。

**一句话解释（任务要求）**：官方推荐改用普通 `ndarray`，是因为 `matrix` 把 `*` 重定义为矩阵乘、与普通数组的逐元素语义冲突，混用时极易出错，而 `ndarray` 配合 `@` 运算符和 `numpy.linalg` 已经能更通用、更一致地完成所有线性代数任务。

#### 4.3.5 小练习与答案

**练习 1**：把上面的 `warnings.simplefilter("always")` 这一行**删掉**，再跑一次。`len(w)` 还大于 0 吗？为什么？

**参考答案**：很可能变成 `0`（取决于 Python 的默认警告过滤器）。因为 `PendingDeprecationWarning` 在默认配置下是被忽略的，`catch_warnings(record=True)` 只记录「通过了过滤器」的警告。删掉 `simplefilter("always")` 后，这条警告被默认过滤器丢弃，于是 `w` 为空。这正好解释了「为什么平时用 `np.matrix` 看不到任何提示」。

**练习 2**：`np.asmatrix(x)` 和 `np.bmat('...')` 会不会触发 `PendingDeprecationWarning`？

**参考答案**：都会。因为 [defmatrix.py:70](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L70) 里 `asmatrix` 内部就是 `return matrix(data, dtype=dtype, copy=False)`；`bmat` 的字符串/序列分支最终也会调用 `matrix(...)`（见 [defmatrix.py:1105](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L1105-L1105) 等处）。只要最终构造出 `matrix` 对象，就会经过 `__new__` 顶部的 `warnings.warn`，所以三个公开 API 都会触发该警告。

**练习 3**：为什么 numpy 选 `PendingDeprecationWarning` 而不是 `DeprecationWarning`？

**参考答案**：因为官方**还没有**承诺在某个具体版本移除 `matrix`，只是「计划弃用」。`DeprecationWarning` 通常意味着更明确的移除路线图；`PendingDeprecationWarning` 更软，表达了「迟早要退，但暂无时间表」的语义。

---

## 5. 综合实践

把本讲的三个知识点（子包只导出三个名字、`matrix` 永远二维、构造时发 `PendingDeprecationWarning`）串成一个小任务：

写一段脚本，完成下面四件事，并对每一件给出断言或观察说明：

1. 打印 `np.matrixlib.__all__`，确认它正好是 `['matrix', 'bmat', 'asmatrix']`。
2. 在捕获警告的上下文里，用**字符串**、**嵌套列表**、**标量**三种方式各创建一个 `matrix`，分别断言它们的 `shape` 是 `(2, 2)`、`(2, 2)`、`(1, 1)`。
3. 断言这三次创建一共捕获到的警告中，至少有一条类别是 `PendingDeprecationWarning`。
4. 用一句话注释说明：如果改用普通 `ndarray` + `@` 运算符，能怎样替代 `matrix` 的矩阵乘法用途。

参考框架：

```python
import warnings
import numpy as np

# 1. 公开清单
assert np.matrixlib.__all__ == ['matrix', 'bmat', 'asmatrix']

with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")

    m_str = np.matrix('1 2; 3 4')          # 字符串
    m_list = np.matrix([[1, 2], [3, 4]])   # 嵌套列表
    m_scalar = np.matrix(7)                # 标量

    # 2. 永远二维
    assert m_str.shape == (2, 2)
    assert m_list.shape == (2, 2)
    assert m_scalar.shape == (1, 1)

    # 3. 发出了 PendingDeprecationWarning
    cats = {x.category for x in w}
    assert any(issubclass(c, PendingDeprecationWarning) for c in cats), cats

# 4. 替代方案：普通 ndarray + @
a = np.array([[1, 2], [3, 4]])
b = np.array([[5, 6], [7, 8]])
# @ 就是矩阵乘法，等价于 matrix 语境下的 *
print("ndarray 矩阵乘:\n", a @ b)
# 说明：用普通数组 + @ 即可完成矩阵乘法，无需、也不推荐再用 np.matrix。
```

> 预期：所有断言通过，最后打印出 `a @ b` 的 2×2 矩阵乘结果 `[[19, 22], [43, 50]]`。结果数值可由线性代数定义验证，本讲义作者未在本机实跑。

## 6. 本讲小结

- `numpy.matrixlib` 是一个极小的子包，对外只暴露 **`matrix`、`asmatrix`、`bmat`** 三个名字，公开清单由 [defmatrix.py:1](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L1-L1) 的 `__all__` 决定，并由 `__init__.py` 原样转发。
- `np.matrix` 是一个**继承自 `ndarray`、强制保持二维**的子类：标量变 `(1,1)`、一维变 `(1,N)`、超过二维直接 `ValueError`（[defmatrix.py:153-160](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L153-L160)）。
- 官方**不再推荐**使用 `matrix`（[defmatrix.py:84-86](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L84-L86)），推荐改用普通 `ndarray` 配合 `@` 运算符和 `numpy.linalg`。
- 每次 `matrix(...)` 构造都会在 `__new__` 开头无条件发出一个 **`PendingDeprecationWarning`**（[defmatrix.py:119-125](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/matrixlib/defmatrix.py#L119-L125)）。
- `PendingDeprecationWarning` 表示「计划弃用但尚未承诺移除时间」，比 `DeprecationWarning` 更软，且默认被 Python 过滤掉，所以日常使用看不到它——需要用 `warnings.catch_warnings(record=True)` + `simplefilter("always")` 才能抓到。
- `asmatrix`/`bmat` 因为最终都会调用 `matrix(...)`，所以同样会触发该警告。

## 7. 下一步学习建议

本讲只是「认识」了 `matrixlib` 和它的弃用状态。建议接下来：

- 读 **u1-l2（包结构与导出关系）**：深入 `__init__.py` 的转发机制、`@set_module('numpy')` 的作用，以及顶层 `np.matrix` 是怎么从子包「抬」到顶层的。
- 读 **u1-l3（快速上手 matrix/asmatrix/bmat 与运行测试）**：动手把三种构造方式、`asmatrix` 的视图语义、`np.matrixlib.test()` 都跑一遍。
- 之后再进入进阶层（u2）和专家层（u3），逐步拆解 `__new__` 内部、字符串解析、运算符重写、子类化机制（`__array_finalize__`）等真正的实现细节。

如果想立刻动手巩固本讲，建议先把第 5 节的「综合实践」完整敲一遍——它把本讲四个最小模块全部串起来了。
