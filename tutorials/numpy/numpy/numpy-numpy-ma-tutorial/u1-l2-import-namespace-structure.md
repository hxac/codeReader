# 导入、命名空间与目录结构

> 第二讲（入门层 · u1-l2）
> 上一讲 [[u1-l1] 我们建立了「掩码数组 = data + mask + fill_value」的整体概念，并用 `np.ma.masked_invalid` 屏蔽 NaN、用 `np.mean` 跳过屏蔽值算出了均值。
> 本讲回答一个更底层的问题：**当你写下 `np.ma.xxx` 时，Python 到底去哪里找 `xxx`？** 理解这一点，是后面阅读任何 `numpy.ma` 源码的前提。

---

## 1. 本讲目标

学完本讲，你应该能够：

1. 用两种正确方式 `import` `numpy.ma`，并说出它们的区别。
2. 解释 `__init__.py` 是如何把 `core` 和 `extras` 两个模块的名字「搬」到 `np.ma` 命名空间里的（`from .core import *` + `__all__` 拼接）。
3. 准确判断 `array`、`masked_where`、`average`、`median` 这些名字分别来自 `core.py` 还是 `extras.py`。
4. 说出 `core.py / extras.py / mrecords.py / testutils.py` 各自的职责，以及 `tests/` 目录的组织方式。
5. 理解 `.pyi` 类型桩文件（stub）为什么存在、以及它如何「补全」`import *` 给静态类型检查器带来的盲区。

---

## 2. 前置知识

- **包（package）与模块（module）**：Python 里一个含 `__init__.py` 的目录就是一个「包」。`numpy/ma/` 是一个包，里面的 `core.py`、`extras.py` 是它的「子模块」。包本身也是一个模块（`numpy.ma`）。
- **命名空间（namespace）**：可以理解为「名字 → 对象」的字典。当你写 `np.ma.array` 时，Python 先在 `np.ma` 这个模块对象的命名空间里找 `array`；找不到再去它的包 `__init__.py` 里定义/导入的名字里找。
- **`from X import *`**：把模块 `X` 里「对外公开」的名字（由 `X.__all__` 决定，没有 `__all__` 则是所有不以 `_` 开头的名字）一股脑搬到当前命名空间。
- **`__all__`**：一个字符串列表，声明「这个模块对外公开哪些名字」。它同时控制 `import *` 的行为，也是一份给读者的「公开 API 清单」。

> 不熟悉上面任何一点也没关系，本讲会结合 `numpy.ma` 的真实源码边看边讲。

---

## 3. 本讲源码地图

本讲涉及的文件都在 `numpy/ma/` 目录下。先看一眼整个目录的职责分工：

| 文件 / 目录 | 行数级别 | 职责 | 是否被 `__init__.py` 自动 re-export |
| --- | --- | --- | --- |
| `__init__.py` | 约 54 行 | 包入口：把 `core`/`extras` 的公开名字搬到 `np.ma` 命名空间 | （它本身就是入口） |
| `core.py` | 约 8700 行 | 掩码数组核心：`MaskedArray` 类、`masked`/`nomask`、构造与索引、掩码 ufunc、fill_value 系统 | ✅ 是 |
| `extras.py` | 约 2600 行 | 掩码数组「附加件」：统计、集合、拼接、轴应用等工具函数 | ✅ 是 |
| `mrecords.py` | 约 700 行 | 字段级屏蔽的记录数组 `mrecarray` | ❌ 否，需显式 `import numpy.ma.mrecords` |
| `testutils.py` | 约 300 行 | 掩码感知的测试断言（`assert_equal` 等） | ❌ 否，需显式 `import numpy.ma.testutils` |
| `tests/` | 多个文件 | 测试套件，对应 `core`/`extras`/`mrecords`/子类化/回归等 | 由 `np.ma.test()` 驱动 |
| `*.pyi` | 5 个文件 | 类型桩（stub），给静态类型检查器用 | —— |

> 注意最后一列：**不是 `numpy/ma/` 下的所有模块都会出现在 `np.ma.` 命名空间里**。`mrecords` 和 `testutils` 虽然是包内文件，但 `__init__.py` 并没有 import 它们，所以你必须显式 `import numpy.ma.mrecords` 才能用。这是本讲后半段要讲清的一个关键细节。

---

## 4. 核心概念与源码讲解

本讲拆成 4 个最小模块：

- **4.1** `import numpy.ma` 的两条路径与 `__init__.py` 的 re-export 机制
- **4.2** `core / extras / mrecords / testutils` 四模块分工
- **4.3** `tests/` 测试目录组织
- **4.4** `.pyi` 类型桩的作用

---

### 4.1 `import numpy.ma` 的两条路径与 `__init__.py` 的 re-export 机制

#### 4.1.1 概念说明

日常用 NumPy 时，掩码数组几乎总是这样访问的：

```python
import numpy as np
np.ma.masked_invalid([1.0, np.nan, 3.0])
```

这里的 `np.ma` 就是 `numpy.ma` 这个包。要让它「开箱即用」地把 `masked_invalid`、`array`、`masked_where`、`average` 等几百个名字暴露出来，靠的就是包入口 `__init__.py` 里的 **re-export（再导出）**：把其他子模块里已经定义好的名字，搬进自己的命名空间，对外假装「这些名字都是我提供的」。

这带来两个等价的导入风格：

1. **子包风格**：`import numpy.ma`（或 `import numpy as np` 后用 `np.ma`）——访问所有公开 API 的主入口。
2. **直接子模块风格**：`from numpy.ma import core` 之后用 `core.array(...)`——当你想明确区分「这个函数到底来自哪个子模块」时很有用。

#### 4.1.2 核心流程

当你执行 `import numpy.ma` 时，Python 会运行 `numpy/ma/__init__.py`，发生的事情可以用下面这段伪流程描述：

```
1. 先把同目录下的两个子模块 core、extras 加载进来并绑定到本包命名空间：
       from . import core, extras        # 现在能 np.ma.core / np.ma.extras 访问
2. 把 core 里「对外公开」的名字全部搬到本包命名空间：
       from .core import *               # 按 core.__all__ 搬运
3. 把 extras 里「对外公开」的名字全部搬到本包命名空间：
       from .extras import *             # 按 extras.__all__ 搬运
4. 组装本包自己的 __all__（对外公开 API 清单）：
       __all__ = ['core', 'extras']      # 先放两个子模块名
       __all__ += core.__all__           # 拼上 core 的公开名
       __all__ += extras.__all__         # 拼上 extras 的公开名
5. 附赠一个 test() 入口（用于跑该包的测试套件）。
```

关键点：`np.ma` 的公开名字 **不是在 `__init__.py` 里逐个定义的**，而是通过 `import *` 从 `core` 和 `extras` 「批发」来的；最终对外清单由三方 `__all__` 拼接而成。

#### 4.1.3 源码精读

re-export 的全部魔法集中在这几行：

[`__init__.py:42-48`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/__init__.py#L42-L48)

```python
from . import core, extras       # 把子模块绑到包命名空间：np.ma.core / np.ma.extras 可用
from .core import *              # 按 core.__all__ 搬运 core 的公开名字
from .extras import *            # 按 extras.__all__ 搬运 extras 的公开名字

__all__ = ['core', 'extras']     # 对外公开清单的起点：两个子模块本身
__all__ += core.__all__          # 拼接 core 的公开名（array、masked_where、MaskedArray …）
__all__ += extras.__all__        # 拼接 extras 的公开名（average、median、ndenumerate …）
```

这三句 `import *` 决定了「谁能被 `np.ma.` 直接访问」。而**谁会被搬**，由两个子模块各自的 `__all__` 决定。`core.py` 的 `__all__`（节选）：

[`core.py:51-85`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L51-L85)

```python
__all__ = [
    'MAError', 'MaskError', 'MaskType', 'MaskedArray', 'abs', 'absolute',
    'add', 'all', 'allclose', 'allequal', 'alltrue', 'amax', 'amin',
    ...                                       # 数百个名字
    'masked', 'masked_array', 'masked_equal', 'masked_greater',
    ... 'masked_values', 'masked_where', ...   # masked_where 在 core
    'array', 'asanyarray', 'asarray', ...      # array 在 core
    ...
]
```

可以看到 `array`、`masked_where`、`MaskedArray`、`masked`、`nomask` 等都在 `core.__all__` 里——所以它们会出现在 `np.ma.` 命名空间。

而 `extras.py` 的 `__all__` 则是另一批名字：

[`extras.py:10-20`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L10-L20)

```python
__all__ = [
    'apply_along_axis', 'apply_over_axes', 'atleast_1d', 'atleast_2d',
    'atleast_3d', 'average', 'clump_masked', 'clump_unmasked', ...
    'masked_all', 'masked_all_like', 'median', 'mr_', 'ndenumerate',
    ...
]
```

`average`、`median`、`ndenumerate`、`apply_along_axis` 在这里——它们同样被搬到 `np.ma.` 命名空间，但**源头是 `extras`**。

`__init__.py` 最后还挂了一个测试入口（下一节讲 `tests/` 时会用到）：

[`__init__.py:50-53`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/__init__.py#L50-L53)

```python
from numpy._pytesttester import PytestTester
test = PytestTester(__name__)     # 于是 np.ma.test() 能跑该包的全部测试
del PytestTester
```

#### 4.1.4 代码实践

> **实践目标**：用源码里读到的结论，验证 `array`、`masked_where`、`average` 三个名字分别属于哪个子模块，并亲手看到 re-export 的搬运结果。

**操作步骤：**

1. 打开一个 Python 解释器（已安装 numpy）。
2. 执行下面的探索脚本：

```python
import numpy as np
import numpy.ma

# (a) 看看 __init__.py 拼出来的对外清单有多长
print("np.ma.__all__ 长度：", len(np.ma.__all__))
print("是否含 'core' 和 'extras'：", 'core' in np.ma.__all__, 'extras' in np.ma.__all__)

# (b) 追溯三个名字的「出生地」
for name in ['array', 'masked_where', 'average']:
    obj = getattr(np.ma, name)
    print(f"{name:15s} 定义在模块: {obj.__module__}")
```

3. （可选）分别从子模块直接取，确认是同一个对象：

```python
from numpy.ma import core, extras
print(np.ma.array is core.array)            # True：同一个函数对象
print(np.ma.masked_where is core.masked_where)  # True
print(np.ma.average is extras.average)      # True
```

**需要观察的现象：**

- `np.ma.__all__` 是一个很长的列表（上百个名字），开头含 `'core'`、`'extras'`。
- `obj.__module__` 会告诉你定义位置：`array` 和 `masked_where` 显示来自 `numpy.ma.core`；`average` 显示来自 `numpy.ma.extras`。
- `is` 比较为 `True`，说明 `np.ma.array` 与 `core.array` 是**同一个函数对象**，re-export 只是「换个名字暴露」，没有复制。

**预期结果（基于源码阅读）：**

| 名字 | `__module__` | 所属子模块 | 出处 |
| --- | --- | --- | --- |
| `array` | `numpy.ma.core` | core | [`core.py:6859`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L6859) `def array(...)` |
| `masked_where` | `numpy.ma.core` | core | [`core.py:1885`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1885) `def masked_where(...)` |
| `average` | `numpy.ma.extras` | extras | [`extras.py:536`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L536) `def average(...)` |

> 说明：上表「出处」一列的行号是直接阅读源码得到的，不是运行结果；你在自己机器上跑步骤 2 时，`__module__` 字符串应与表格一致。若 numpy 版本不同导致行号偏移，以 `__module__` 为准。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `np.ma.array` 和 `np.ma.core.array` 用 `is` 比较是 `True`？如果改成 `==` 呢？

> **答案**：`__init__.py` 用 `from .core import *` 把 `core.array` 这个**对象本身**搬进了 `np.ma` 命名空间，并没有重新定义，所以两个名字指向同一个函数对象，`is` 为 `True`。函数没有有意义的 `__eq__`，`==` 默认退化为身份比较，结果也是 `True`。

**练习 2**：`np.ma` 的 `__all__` 为什么以 `['core', 'extras']` 开头再 `+=` 拼接，而不是直接写一个超大列表？

> **答案**：因为公开名字的「真相」分散在两个子模块各自的 `__all__` 里。让 `__init__.py` 去 `+= core.__all__` / `+= extras.__all__`，可以保证「子模块改了 `__all__`，包入口的对外清单自动跟着变」，避免两处清单不同步。这是 re-export 模式的典型写法。

---

### 4.2 `core / extras / mrecords / testutils` 四模块分工

#### 4.2.1 概念说明

`numpy/ma/` 目录下有四个「实质模块」（`.py` 文件）。它们按职责分层：

- **`core.py`**：地基。掩码数组的一切基础——`MaskedArray` 类本身、`masked` 单例、`nomask`、各种 `masked_xxx` 构造函数、掩码 ufunc 包装、fill_value 系统。它「自带」一份独立的 `__all__`。
- **`extras.py`**：上层工具。统计（`average`/`median`/`cov`）、集合（`union1d`/`intersect1d`）、按轴应用（`apply_along_axis`）、拼接（`mr_`）等。它**依赖** `core`，开头就 `from .core import ...` 大量复用 core 的内部能力。
- **`mrecords.py`**：可选扩展。字段级屏蔽的记录数组，用于「每个字段都能单独屏蔽」的结构化数据。
- **`testutils.py`**：测试辅助。提供掩码感知的断言。

一个关键事实：**只有 `core` 和 `extras` 被 `__init__.py` re-export**；`mrecords` 和 `testutils` 是包内文件，但不在 `np.ma.` 的公开命名空间里，必须显式 import。

#### 4.2.2 核心流程

```
core.py  ←── 被依赖 ──┐
                     │
extras.py ───────────┘   (extras from .core import ... 复用 core)
   ↑ __init__.py 把 core、extras 都 import * 进 np.ma

mrecords.py  ── 不在 __init__.py 的 import 列表里 → 需 import numpy.ma.mrecords
testutils.py ── 不在 __init__.py 的 import 列表里 → 需 import numpy.ma.testutils
```

#### 4.2.3 源码精读

**(a) `core.py` 是地基**——模块文档直接说明了它的来历与定位：

[`core.py:1-21`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1-L21)

```python
"""
numpy.ma : a package to handle missing or invalid values.
...
In 2006, the package was completely rewritten by Pierre Gerard-Marchant
(University of Georgia) to make the MaskedArray class a subclass of ndarray,
and to improve support of structured arrays.
...
"""
```

这里承接上一讲的历史背景：2006 年重写，把 `MaskedArray` 改造成 `ndarray` 的子类。`core.py` 里 `MaskedArray` 的定义就在这里：

[`core.py:2770`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L2770) —— `class MaskedArray(ndarray):`（子类化细节是进阶层 u2-l2 的主题，本讲只需知道「它在 core 里」）。

**(b) `extras.py` 是上层工具，依赖 core**——它的导入语句清楚展示了这种依赖：

[`extras.py:32-55`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L32-L55)

```python
from . import core as ma                       # 把整个 core 当作 ma 别名
from .core import (                            # 直接复用 core 的内部能力
    MAError, MaskedArray, add, array, asarray,
    concatenate, count, dot, filled, get_masked_subclass,
    getdata, getmask, getmaskarray, make_mask_descr, mask_or,
    masked, masked_array, nomask, ones, sort, zeros,
)
```

`extras` 自己的定位在模块文档里也很直白：

[`extras.py:1-9`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L1-L9) ——「Masked arrays add-ons. A collection of utilities for `numpy.ma`.」（掩码数组的附加件 / 工具集）。

**(c) `mrecords.py` 是字段级屏蔽扩展**，且**不在公开命名空间**：

[`mrecords.py:1-10`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/mrecords.py#L1-L10)

```python
""":mod:`numpy.ma..mrecords`

Defines the equivalent of :class:`numpy.recarrays` for masked arrays,
where fields can be accessed as attributes.
Note that :class:`numpy.ma.MaskedArray` already supports structured datatypes
and the masking of individual fields.
"""
```

它有自己的 `__all__`，但**请注意**：回头对照 4.1.3 节的 `__init__.py`，里面只有 `from . import core, extras`，**没有 mrecords**。所以：

```python
import numpy.ma
numpy.ma.mrecords        # ❌ AttributeError，除非先 import numpy.ma.mrecords

import numpy.ma.mrecords
numpy.ma.mrecords         # ✅ 现在可用
```

`mrecords` 内部同样依赖 core（通过 `import numpy.ma as ma`），并定义了保留字段名列表：

[`mrecords.py:27-32`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/mrecords.py#L27-L32)

```python
__all__ = [
    'MaskedRecords', 'mrecarray', 'fromarrays', 'fromrecords',
    'fromtextfile', 'addfield',
]
reserved_fields = ['_data', '_mask', '_fieldmask', 'dtype']
```

**(d) `testutils.py` 是测试辅助，同样不在公开命名空间**，且它有一个「拼接式 `__all__`」的小细节：

[`testutils.py:21-42`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/testutils.py#L21-L42)

```python
from .core import filled, getmask, mask_or, masked, masked_array, nomask

__all__masked = [
    'almost', 'approx', 'assert_almost_equal', ... 'assert_mask_equal', ...
]

__some__from_testing = [
    'TestCase', 'assert_', 'assert_allclose', ...
]

__all__ = __all__masked + __some__from_testing   # 拼成最终 __all__
```

注意第 31-34 行的注释：`testutils` 故意 re-export 了一些普通测试函数（来自 `unittest`/`numpy.testing`），是为了不破坏那些「误把它当测试工具总入口」的下游项目（注释点名了 SciPy）。这是命名空间设计上「为了兼容性而妥协」的真实例子。

#### 4.2.4 代码实践

> **实践目标**：亲手验证「`mrecords` 和 `testutils` 不在 `np.ma.` 公开命名空间，而 `core`/`extras` 在」。

**操作步骤：**

```python
import numpy as np
import numpy.ma

# core / extras 是被 __init__.py 显式 import 的子模块，直接可达
print(hasattr(np.ma, 'core'))      # True
print(hasattr(np.ma, 'extras'))    # True

# mrecords / testutils 没有被 __init__.py import
print(hasattr(np.ma, 'mrecords'))  # 预期 False（除非之前显式 import 过）
print(hasattr(np.ma, 'testutils')) # 预期 False

# 显式 import 后才可达
import numpy.ma.testutils as tu
print(tu.__name__)                 # numpy.ma.testutils
print(hasattr(np.ma, 'testutils')) # 现在 True
```

**需要观察的现象：**

- 第一组 `hasattr` 为 `True`，对应 `__init__.py` 的 `from . import core, extras`。
- `mrecords`/`testutils` 初始 `hasattr` 为 `False`；显式 `import` 后变为 `True`，且会把模块对象注册到 `np.ma` 上。

**预期结果**：如上注释所示。这验证了「目录里有文件 ≠ 自动出现在 `np.ma.` 命名空间」——是否暴露，完全由 `__init__.py` 的 import 语句决定。

#### 4.2.5 小练习与答案

**练习 1**：`extras.py` 为什么要在开头写 `from . import core as ma`？直接 `import numpy.ma` 行不行？

> **答案**：`extras` 需要 `core` 提供的基础设施（`MaskedArray`、`masked`、`filled` 等）。用 `from . import core as ma` 是相对导入（`.` 代表当前包 `numpy.ma`），既能拿到 `core`，又把它别名为 `ma`，方便在 `extras` 内部写 `ma.something` 调用 core 的能力。直接 `import numpy.ma` 也能用，但会触发对整个包入口（包括 `extras` 自己）的加载，相对导入更直接、循环依赖风险更小。

**练习 2**：如果不 `import numpy.ma.mrecords`，能通过 `np.ma.MaskedRecords` 拿到字段级屏蔽的记录数组类吗？

> **答案**：不能。`MaskedRecords` 在 `mrecords.py` 的 `__all__` 里，但 `__init__.py` 没有 import `mrecords`，所以它不在 `np.ma.` 命名空间。必须先 `import numpy.ma.mrecords`，再用 `numpy.ma.mrecords.MaskedRecords` 或 `from numpy.ma.mrecords import MaskedRecords`。（字段级屏蔽本身是专家层 u3-l5 的主题。）

---

### 4.3 `tests/` 测试目录组织

#### 4.3.1 概念说明

`numpy/ma/tests/` 是该包的测试套件目录。它与源码模块一一对应：每个实质模块都有自己的测试文件，再加上跨切面的回归测试和旧实现兼容性测试。

#### 4.3.2 核心流程（目录布局）

```
numpy/ma/tests/
├── __init__.py            # 空，标记这是一个包
├── test_core.py           # 对应 core.py（最大的测试文件）
├── test_extras.py         # 对应 extras.py
├── test_mrecords.py       # 对应 mrecords.py
├── test_subclassing.py    # 子类化 MaskedArray 的专门测试
├── test_regression.py     # 回归测试（防止旧 bug 复现）
├── test_old_ma.py         # 与旧实现的兼容性对比测试
├── test_deprecations.py   # 弃用相关
└── test_arrayobject.py    # 一个最小骨架/历史遗留测试
```

这种「源码文件 ↔ test_同名.py」的命名约定，正是 `np.ma.test()` 能按模块收集测试的基础。

#### 4.3.3 源码精读

`tests/__init__.py` 是一个空文件（只有标记包的作用），所以我们重点看「如何运行这些测试」。回到 4.1.3 节看到的：

[`__init__.py:50-52`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/__init__.py#L50-L52)

```python
from numpy._pytesttester import PytestTester
test = PytestTester(__name__)     # np.ma.test() 即可运行 tests/ 下全部测试
```

`PytestTester(__name__)` 把当前包名（`numpy.ma`）注册进去，调用 `np.ma.test()` 时，它会用 pytest 去发现并运行该包下的所有 `test_*.py`。这就是为什么目录里每个文件都叫 `test_xxx.py`——pytest 默认只收集 `test_` 开头的文件和函数。

各测试文件的对应关系（无需读全文，知道分工即可）：

| 测试文件 | 测什么 |
| --- | --- |
| `test_core.py` | `MaskedArray`、掩码 ufunc、fill_value、索引等 core 全部功能（文件最大） |
| `test_extras.py` | `average`、`median`、集合运算、`apply_along_axis` 等 |
| `test_mrecords.py` | `mrecarray` 字段级屏蔽 |
| `test_subclassing.py` | 子类化 `MaskedArray` 时属性/类型的传播 |
| `test_regression.py` | 历史 issue 的回归用例 |
| `test_old_ma.py` | 新旧 `MaskedArray` 实现的兼容性对比 |

#### 4.3.4 代码实践

> **实践目标**：用 `np.ma.test()` 跑一次该包测试套件，并观察「按模块收集」的行为。

**操作步骤：**

```python
import numpy as np
np.ma.test()            # 运行 numpy.ma 包下全部 test_*.py
# 也可只跑某个文件（pytest 语法）：
# 在 shell 里：python -m pytest numpy/ma/tests/test_core.py -q
```

**需要观察的现象：**

- pytest 会收集 `numpy/ma/tests/` 下的所有 `test_*.py`，输出每个文件的收集数与通过/失败情况。
- `test_core.py` 通常用例最多（因为 core 功能最多）。

**预期结果**：测试套件整体通过（在你本地的 numpy 构建上）。若只想验证「命名约定生效」，可加 `-k` 关键字过滤，例如只跑名字含 `mask` 的用例。

> 若没有从源码构建 numpy，`np.ma.test()` 仍可运行已安装 numpy 自带的测试（取决于发行版是否打包 tests）。无法运行时，改为「源码阅读型实践」：打开 [`tests/test_core.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/tests/test_core.py)，找一个 `def test_...` 函数，对照本讲知识猜它测的是 core 还是 extras 的功能。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `tests/__init__.py` 是空文件仍然必要？

> **答案**：空 `__init__.py` 把 `tests/` 标记为一个 Python 包，使 pytest 能以包内模块的方式发现和导入 `test_*.py`，也使 `np.ma.tests` 可作为子包被引用。（现代 pytest 也支持无 `__init__.py` 的目录发现，但保留它是 numpy 的既定约定。）

**练习 2**：`np.ma.test()` 是怎么知道要跑哪些文件的？

> **答案**：`__init__.py` 里 `test = PytestTester(__name__)`，`__name__` 即 `numpy.ma`。`PytestTester` 内部调用 pytest，按默认规则收集该包目录下所有 `test_*.py` 里的 `test_*` 函数。

---

### 4.4 `.pyi` 类型桩的作用

#### 4.4.1 概念说明

你可能在目录里注意到一批「同名 `.pyi`」文件：

```
__init__.py   ↔  __init__.pyi
core.py       ↔  core.pyi
extras.py     ↔  extras.pyi
mrecords.py   ↔  mrecords.pyi
testutils.py  ↔  testutils.pyi
```

`.pyi` 叫做**类型桩（type stub）**。它的特点是：

- **只有类型签名，没有实现**：里面是函数/类的「形状」（参数类型、返回类型），函数体通常是 `...`。
- **运行时完全不被执行**：Python 解释器运行 `.py`，类型检查器（mypy、pyright、IDE 的类型提示）读 `.pyi`。
- **当 `.pyi` 与 `.py` 同名且同目录时，类型检查器优先用 `.pyi`**，把里面的签名当作该模块的「权威类型」。

> 类比：`.py` 是「能跑的真机器」，`.pyi` 是「贴在墙上的说明书」。说明书告诉工具「这个函数吃什么、吐什么」，但不参与运转。

#### 4.4.2 核心流程

```
开发者写 .pyi（手写类型签名）
       │
类型检查器（mypy/pyright/IDE）
   读取 .pyi（优先于 .py）
       │
据此对 np.ma.xxx 的调用做类型检查/补全
```

对 `numpy.ma` 而言，`.pyi` 还解决了一个**它特有的痛点**：`__init__.py` 用了 `from .core import *` / `from .extras import *`（星号导入），而类型检查器**无法可靠地跟踪 `import *` 到底搬了哪些名字**（尤其涉及 `__all__` 拼接时）。于是 numpy 手写了一份 `__init__.pyi`，把每个公开名字的来源写得清清楚楚。

#### 4.4.3 源码精读

看 `__init__.pyi` 的开头，它**不用 `import *`**，而是逐个显式列出：

[`__init__.pyi:1-2`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/__init__.pyi#L1-L2)

```python
from . import core, extras
from .core import (          # 逐个显式列出 core 的公开名（节选）
    MAError, MaskedArray, MaskError, MaskType, abs, absolute, add, ...
)
```

接着是来自 extras 的名字：

[`__init__.pyi:182-188`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/__init__.pyi#L182-L188)

```python
from .extras import (        # 逐个显式列出 extras 的公开名（节选）
    apply_along_axis, apply_over_axes, ...
    average, ...             # average 明确标注来自 extras
    ...
)
```

最后是一份与运行时 `__all__` 对应的字符串清单：

[`__init__.pyi:231-234`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/__init__.pyi#L231-L234)

```python
__all__ = [
    "core", "extras", "MAError", "MaskError", ...   # core 的名字
    ...                                              # 后面是 extras 的名字
]
```

对比 4.1 节的运行时 `__init__.py`：运行时靠 `import *` + `__all__ +=` 动态拼接；类型桩则把结果**静态地、显式地**写死。这就让类型检查器能准确知道：

- `np.ma.array` 来自 `core`（→ 用 `core.pyi` 里 `array` 的签名）；
- `np.ma.average` 来自 `extras`（→ 用 `extras.pyi` 里 `average` 的签名）。

而 `core.pyi` / `extras.pyi` / `mrecords.pyi` / `testutils.pyi` 则分别为对应模块提供了完整的类型签名（函数参数、重载、返回类型），供检查器查表。

#### 4.4.4 代码实践

> **实践目标**：体会「`.pyi` 给类型检查器看、`.py` 给解释器跑」的分工，并确认 `__init__.pyi` 把 `array` 归到 core、`average` 归到 extras。

**操作步骤（源码阅读型）：**

1. 打开 [`__init__.pyi`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/__init__.pyi)。
2. 在文件里搜索 `array,`（注意带逗号），确认它出现在 `from .core import (...)` 块里（约第 33 行）。
3. 搜索 `average,`，确认它出现在 `from .extras import (...)` 块里（约第 188 行）。
4.（可选，若装了 mypy）写一个小文件 `check_ma.py`：

```python
import numpy as np
reveal_type(np.ma.array)       # mypy 会揭示它是 core 的 array
reveal_type(np.ma.average)     # mypy 会揭示它是 extras 的 average
```

然后运行 `mypy check_ma.py`，观察 `reveal_type` 的输出。

**需要观察的现象：**

- 步骤 2/3：`array` 在 core 的 import 块、`average` 在 extras 的 import 块——与运行时 `__module__` 结论一致。
- 步骤 4（若执行）：`reveal_type` 输出的类型来自 `core.pyi` / `extras.pyi`，证明类型检查器读的是 `.pyi`。

**预期结果**：类型桩里 `array` 归 `core`、`average` 归 `extras`，与本讲 4.1 节运行时观察完全吻合——这正是 `.pyi` 与 `.py` 必须「对齐」的体现。若步骤 4 无 mypy 环境，则标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：既然 `.py` 已经能跑，为什么还要维护一份 `.pyi`？

> **答案**：`.pyi` 给静态类型检查器/IDE 用，能在**不运行代码**的前提下推断类型、做补全和错误检查。对 `numpy.ma` 这种用 `import *` 的包尤其重要：类型检查器难以跟踪星号导入搬运了哪些名字，手写的 `__init__.pyi` 显式列出全部公开名，补上了这个盲区。

**练习 2**：如果你改了 `core.py` 给 `array` 加了一个新参数，但忘了更新 `core.pyi`，会有什么后果？

> **答案**：运行时不受影响（解释器读 `.py`），但类型检查器仍按旧的 `core.pyi` 签名判断，可能对「使用新参数」的代码误报类型错误。这就是为什么 `.py` 与 `.pyi` 必须同步维护——`numpy.ma` 的 CI 会校验这种一致性。

---

## 5. 综合实践

把本讲四个模块串起来，完成下面这个「命名空间考古」小任务：

**任务**：给定一份候选名字清单，逐个判定它「能不能用 `np.ma.X` 直接访问」「来自哪个子模块」「`.pyi` 里写在哪里」。清单：`array`、`masked_where`、`average`、`median`、`MaskedRecords`、`assert_mask_equal`。

**操作步骤：**

1. 按 4.1.4 的脚本，用 `getattr(np.ma, name, None)` 判断「能否直接访问」；能访问的用 `obj.__module__` 判断来源。
2. 不能直接访问的（如 `MaskedRecords`、`assert_mask_equal`），回忆 4.2 节：它们在 `mrecords` / `testutils`，需要显式 import。
3. 在 [`__init__.pyi`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/__init__.pyi) 里搜索每个名字，确认它出现在 core 块、extras 块、还是根本没有（进一步印证是否在公开命名空间）。

**预期结果（表格，基于源码阅读）：**

| 名字 | `np.ma.X` 可达？ | 来源模块 | 在 `__init__.pyi` 哪个块 |
| --- | --- | --- | --- |
| `array` | ✅ | `core` | `from .core import (...)` |
| `masked_where` | ✅ | `core` | `from .core import (...)` |
| `average` | ✅ | `extras` | `from .extras import (...)` |
| `median` | ✅ | `extras` | `from .extras import (...)` |
| `MaskedRecords` | ❌（需 `import numpy.ma.mrecords`） | `mrecords` | 不在 `__init__.pyi`（见 `mrecords.pyi`） |
| `assert_mask_equal` | ❌（需 `import numpy.ma.testutils`） | `testutils` | 不在 `__init__.pyi`（见 `testutils.pyi`） |

> 这张表把本讲全部要点收口：re-export 机制（谁能直达）、模块分工（来自哪）、`.pyi` 静态视图（类型检查器看到什么）。完成后，你就建立了一份「`numpy.ma` 命名空间地图」，后续阅读任何 ma 源码都能随时定位。

---

## 6. 本讲小结

- `np.ma` 的公开名字**不是在入口定义的**，而是 `__init__.py` 用 `from .core import *` + `from .extras import *` 从两个子模块「批发」搬运来的，最终 `__all__` 由 `['core','extras']` 拼上两个子模块各自的 `__all__` 组成。
- 四个实质模块分工明确：`core.py` 是地基（`MaskedArray`、掩码 ufunc、fill_value），`extras.py` 是上层工具（统计/集合/拼接），`mrecords.py` 是字段级屏蔽扩展，`testutils.py` 是测试断言。
- **只有 `core` 和 `extras` 被自动 re-export**；`mrecords` 和 `testutils` 必须显式 `import numpy.ma.mrecords` / `import numpy.ma.testutils` 才能用——「目录里有文件」不等于「在 `np.ma.` 命名空间里」。
- `tests/` 与源码模块一一对应（`test_core.py` ↔ `core.py` …），通过 `__init__.py` 里的 `test = PytestTester(__name__)` 提供 `np.ma.test()` 入口。
- `.pyi` 类型桩只给静态类型检查器看、运行时不执行；`numpy.ma` 因为用了 `import *`，特意手写 `__init__.pyi` 把每个公开名显式归到 core/extras，补上类型检查器的盲区。
- 判断「一个名字来自哪」的权威方法：运行时看 `obj.__module__`，静态看 `__init__.pyi` 的 import 块。

---

## 7. 下一步学习建议

本讲解决的是「名字从哪来、目录怎么摆」，是纯结构视角。下一讲 [[u1-l3] 我们进入**用法**视角：如何用 `array`/`masked_array`/`masked_where`/`masked_invalid`/`masked_equal` 等多种方式创建掩码数组。

建议的后续阅读顺序：

1. **u1-l3 创建掩码数组的多种方式**——把本讲看到的 `array`、`masked_where`、`masked_invalid` 等构造函数真正用起来。
2. **u1-l4 读取与提取：data、mask、fill_value**——深入三件套的访问与 `filled`/`compressed`。
3. 想提前感受子模块分工的读者，可先浏览 [`core.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py) 顶部的 `__all__`（[`core.py:51-85`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L51-L85)）和 [`extras.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py) 的 `__all__`（[`extras.py:10-20`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/extras.py#L10-L20)），对照本讲的「命名空间地图」做一次自检。
