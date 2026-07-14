# 目录结构、模块组成与导入方式

> 上一讲（[u1-l1](u1-l1-package-overview.md)）我们弄清了 `numpy.polynomial` 是什么、系数如何表示、有哪六大便捷类。但那些结论都集中在「门面文件」`__init__.py` 上。本讲我们走进门面背后的房间：整个 `numpy/polynomial/` 目录里到底有哪些文件？每个文件各管什么？为什么同一个 `Polynomial` 类会有 `np.polynomial.Polynomial` 和 `np.polynomial.polynomial.Polynomial` 两条导入路径，而官方只推荐第一条？又该用什么命令一键跑通整个子包的测试？把目录看懂、把导入路径理顺，后面阅读任何一篇源码讲义都不会迷路。

## 1. 本讲目标

读完本讲，你应当能够：

- 说出 `numpy/polynomial/` 目录下每个源文件（`__init__.py`、`_polybase.py`、`polyutils.py`、`polynomial.py`、`chebyshev.py` 等）各自的职责，以及它们之间的依赖关系。
- 区分**便捷类 API**（`np.polynomial.Polynomial`，推荐）与**函数式 API**（`np.polynomial.polynomial.polyval` 等），并解释为什么前者更被推荐。
- 知道如何用 `set_default_printstyle` 切换多项式的打印风格，以及如何用 `np.polynomial.test()` 运行整个子包的内置测试。

## 2. 前置知识

本讲承接 [u1-l1](u1-l1-package-overview.md)，假设你已经知道：

- **便捷类（convenience class）**：`Polynomial`、`Chebyshev`、`Legendre`、`Laguerre`、`Hermite`、`HermiteE`，六者接口一致，每个对应一种「基」。
- **系数约定**：一个 1-D 数组，下标即次数，从低次到高次。

本讲还会用到几个 Python 基础概念，先点一下：

- **模块（module）与包（package）**：一个 `.py` 文件就是一个模块；一个含 `__init__.py` 的目录就是一个包。`numpy/polynomial/` 是包，里面的 `polynomial.py`、`chebyshev.py` 是模块。
- **`__all__`**：一个模块里用列表声明的「公开导出名字」。它告诉 `from package import *` 该导入哪些名字，也是这个模块「对外承诺」的公开 API 表面。
- **`__name__`**：模块自己的名字（含点号分隔的包路径），例如 `numpy.polynomial.polynomial`。
- **抽象基类（abstract base class, ABC）**：只定义接口、不提供完整实现的父类，子类必须实现约定的方法才能实例化。本子包的 `ABCPolyBase` 就是它，详见后续 [u2-l1](u2-l1-abcpolybase-virtual-methods.md)。

## 3. 本讲源码地图

本讲围绕「目录与导入」展开，重点看这四个文件：

| 文件 | 作用 |
|------|------|
| [`__init__.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/__init__.py) | 子包入口。导入并导出六大便捷类，声明 `__all__`，提供 `set_default_printstyle` 打印开关与 `test` 测试入口。 |
| [`_polybase.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py) | 定义抽象基类 `ABCPolyBase`，所有六个便捷类的共同行为（算术、打印、domain/window 映射等）都集中在这里。 |
| [`polyutils.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py) | 全包共享的工具函数库：输入规整（`as_series`）、去尾零（`trimseq/trimcoef`）、区间映射（`getdomain/mapdomain/mapparms`）、浮点格式化（`format_float`）等。 |
| [`polynomial.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py) | 「幂级数（标准幂基）」模块。既提供 `poly*` 函数式 API，又定义便捷类 `Polynomial`；是其余五个家族模块的模板。 |

另有四个同构的家族模块（本讲只点到为止，详见 [u4-l3](u4-l3-orthogonal-families-overview.md)）：[`chebyshev.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/chebyshev.py)、[`legendre.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/legendre.py)、[`laguerre.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/laguerre.py)、[`hermite.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/hermite.py)、[`hermite_e.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/hermite_e.py)。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**目录与文件职责**、**两种 API 的导入路径**、**`set_default_printstyle` 与 `test` 入口**。

### 4.1 目录与文件职责

#### 4.1.1 概念说明

打开 `numpy/polynomial/` 目录，你会看到三类文件：

1. **Python 源码（`*.py`）**：真正承担逻辑的文件。
2. **类型存根（`*.pyi`）**：只写函数/类的「类型签名」，给静态类型检查器（如 mypy）和 IDE 用，运行时不加载。例如 `__init__.pyi` 声明了 `test: Final[_PytestTester]`，但真正的 `test` 对象是在 `__init__.py` 里创建的。
3. **`tests/` 子目录**：测试代码，与源码分离。

源码文件按「职责分层」组织，可以画成一张依赖图（箭头表示「被谁导入」）：

```text
                 __init__.py  （门面：导出六大类 + 打印开关 + test）
                      │
                      ▼
   polynomial.py / chebyshev.py / legendre.py / laguerre.py / hermite.py / hermite_e.py
        （六个家族模块：各自一套 poly* 函数 + 一个便捷类）
                      │ 都继承
                      ▼
                  _polybase.py   （ABCPolyBase：所有便捷类的共同行为）
                      │ 都依赖
                      ▼
                  polyutils.py   （as_series / trimseq / getdomain 等共享工具）
```

这张图说明了几件事：

- **`polyutils.py` 是最底层**。它不依赖子包里的其它模块，只依赖 `numpy` 本身。所有家族模块都通过 `from . import polyutils as pu` 复用它的工具函数。
- **`_polybase.py` 依赖 `polyutils.py`**，定义抽象基类 `ABCPolyBase`。六个便捷类的算术、打印、domain/window 逻辑都写在这里。
- **六个家族模块同时依赖 `_polybase.py` 和 `polyutils.py`**。每个模块既提供一套「函数式 API」（如 `polyadd`、`polyval`、`chebadd`、`chebval`），又定义一个便捷类（如 `Polynomial`、`Chebyshev`）。
- **`__init__.py` 只依赖六个家族模块**，把它们的便捷类「提」到顶层命名空间。

#### 4.1.2 核心流程

当 Python 执行 `import numpy.polynomial` 时，发生的事情大致是：

1. 执行 `numpy/polynomial/__init__.py`。
2. 该文件第一段导入语句把六个便捷类依次「拉」进来，按依赖顺序触发各家族模块的加载。
3. 家族模块（例如 `polynomial.py`）先 `from . import polyutils as pu`、`from ._polybase import ABCPolyBase`，再定义自己的常量、函数式 API 与便捷类。
4. `__init__.py` 最后定义 `__all__`、`set_default_printstyle`，并挂上 `test` 入口。

#### 4.1.3 源码精读

先看门面 `__init__.py` 的导入段，它决定了顶层命名空间里有哪些名字：

[__init__.py:L117-L122](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/__init__.py#L117-L122) —— 把六个家族模块里的便捷类依次导入到 `numpy.polynomial` 命名空间。注意 `Polynomial` 来自 `.polynomial`，`Chebyshev` 来自 `.chebyshev`，依此类推。

再看家族模块 `polynomial.py` 的顶部，它体现了「家族模块依赖工具库 + 基类」的分层：

[polynomial.py:L87-L88](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L87-L88) —— `from . import polyutils as pu` 把共享工具库取个别名 `pu`；`from ._polybase import ABCPolyBase` 把抽象基类拿进来，下面定义 `Polynomial` 时要继承它。

[polynomial.py:L1615-L1616](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L1615-L1616) —— 便捷类的定义：`class Polynomial(ABCPolyBase)`。这行说明「便捷类」并不是凭空实现，而是继承自 `ABCPolyBase`，把通用行为（算术、打印等）直接继承下来。

[polynomial.py:L1642-L1653](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L1642-L1653) —— 类体里把一组「虚函数」绑定到具体的函数式实现，例如 `_add = staticmethod(polyadd)`、`_val = staticmethod(polyval)`、`_roots = staticmethod(polyroots)`。这正是 [u1-l1](u1-l1-package-overview.md) 提到的「一切操作即系数操作」：便捷类不自己实现加减求值，而是委托给本模块的 `polyadd`、`polyval` 等函数。这套「虚函数委托」机制会在 [u2-l1](u2-l1-abcpolybase-virtual-methods.md) 专门讲。

[polynomial.py:L1656-L1658](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L1656-L1658) —— 类属性 `domain`、`window`（默认 `[-1., 1.]`）与 `basis_name`（`Polynomial` 设为 `None`，表示标准幂基）。这与 [u1-l1](u1-l1-package-overview.md) 讲过的「四类属性」对应。

工具库 `polyutils.py` 的公开表面由 `__all__` 列出：

[polyutils.py:L27-L29](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L27-L29) —— `polyutils` 只对外暴露七个工具：`as_series`、`trimseq`、`trimcoef`、`getdomain`、`mapdomain`、`mapparms`、`format_float`。带下划线前缀的 `_add`、`_div`、`_fit`、`_vander_nd` 等是家族模块内部复用的私有助手，不在公开 API 里。

> 小结：源码文件「各司其职」——`polyutils.py` 提供地基工具，`_polybase.py` 在地基上搭出通用行为，六个家族模块各自补上「针对某种基」的具体算法，`__init__.py` 把成果统一摆上货架。

#### 4.1.4 代码实践

**实践目标**：亲手定位每个文件，并验证「便捷类继承 `ABCPolyBase`、委托家族函数」这一依赖关系。

**操作步骤**（在装好 numpy 的 Python 环境里）：

```python
import numpy.polynomial as P
import numpy.polynomial.polynomial as poly
import os

# 1) 找到包目录，确认目录里有哪些 .py 文件
pkg_dir = os.path.dirname(P.__file__)
print([f for f in os.listdir(pkg_dir) if f.endswith('.py')])

# 2) 查看 Polynomial 的继承链（方法解析顺序 MRO）
print(P.Polynomial.__mro__)

# 3) 验证便捷类的「虚函数」确实指向家族模块里的函数
print(P.Polynomial._add)      # 应显示 polyadd 的函数对象
```

**需要观察的现象**：

- 第 1 步打印的列表里应同时出现 `__init__.py`、`_polybase.py`、`polyutils.py`、`polynomial.py`、`chebyshev.py` 等源码文件。
- 第 2 步的 MRO 应包含 `ABCPolyBase` 与 `abc.ABC`，证明 `Polynomial` 继承自抽象基类。
- 第 3 步 `P.Polynomial._add` 应是一个 `<function polyadd ...>` 对象。

**预期结果**：MRO 链首项是 `Polynomial`，其后跟着 `ABCPolyBase`。若打印结果与你阅读源码的判断一致，说明你已经看懂了目录分层。

> 待本地验证：不同 numpy 版本目录文件列表可能有细微差异（如新增的私有模块），但 `__init__.py` / `_polybase.py` / `polyutils.py` / `polynomial.py` 这四个核心文件在当前 HEAD 必然存在。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `polyutils.py` 的 `__all__` 里没有 `_add`、`_fit`、`_vander_nd` 这些带下划线前缀的名字？

**参考答案**：下划线前缀是 Python 约定的「私有」标记，表示这些函数是给家族模块内部复用的实现细节，不属于稳定公开 API。`__all__` 只列对外承诺的名字，避免用户依赖随时可能变动的内部函数。

**练习 2**：`hermite.py` 和 `hermite_e.py` 是两个不同的文件，它们分别对应六大便捷类中的哪两个？为什么物理学家用的 Hermite 和概率论用的 HermiteE 要分开实现？

**参考答案**：分别对应 `Hermite`（物理型，权函数 \(e^{-x^2}\)）和 `HermiteE`（概率型，权函数 \(e^{-x^2/2}\)）。两者的递推关系、定义域、高斯积分点都不一样，所以各自独立成一个模块。它们的差异会在 [u4-l3](u4-l3-orthogonal-families-overview.md) 详细对比。

**练习 3**：打开 `chebyshev.py` 的顶部，找到它与 `polynomial.py` 对应的 `from . import ... as pu` 和 `from ._polybase import ...` 两行。这说明六个家族模块在「依赖关系」上有什么共同点？

**参考答案**：六个家族模块都依赖 `polyutils`（共享工具）和 `_polybase`（抽象基类），结构完全同构；区别只在于各自补上「针对自己那种基」的具体函数与便捷类。

### 4.2 两种 API 的导入路径

#### 4.2.1 概念说明

`numpy.polynomial` 为同一件事提供了两套「入口」：

- **便捷类 API（convenience class API）**：`np.polynomial.Polynomial`、`np.polynomial.Chebyshev` 等。面向对象，写法像操作普通 Python 对象（`p + q`、`p(x)`、`p.deriv()`），是官方**首选**。
- **函数式 API（functional API）**：`np.polynomial.polynomial.polyval`、`np.polynomial.polynomial.polyadd` 等。一组以基名做前缀的函数（`poly*`、`cheb*`、`leg*`、`lag*`、`herm*`、`herme*`），直接对系数数组操作。

关键区别在「命名空间位置」：

| 写法 | 是否可用 | 说明 |
|------|----------|------|
| `np.polynomial.Polynomial` | ✅ | 便捷类，顶层导出，推荐 |
| `np.polynomial.polynomial.Polynomial` | ✅ | 同一个类，但要钻进子模块 |
| `np.polynomial.polyval` | ❌ | 函数式 API **不在**顶层命名空间 |
| `np.polynomial.polynomial.polyval` | ✅ | 函数式 API，必须带子模块路径 |

也就是说：**类**被「提」到了顶层，而**函数**仍留在各自的子模块里。

#### 4.2.2 核心流程

为什么 `np.polynomial.Polynomial` 能用？看 `__init__.py` 的 `__all__`：

```python
__all__ = [
    "set_default_printstyle",
    "polynomial", "Polynomial",
    "chebyshev", "Chebyshev",
    "legendre", "Legendre",
    "hermite", "Hermite",
    "hermite_e", "HermiteE",
    "laguerre", "Laguerre",
]
```

注意每一对里**既有模块名（小写 `polynomial`）又有类名（大写 `Polynomial`）**。这意味着：

- `np.polynomial.Polynomial`（类）—— 可用。
- `np.polynomial.polynomial`（模块）—— 也可用，于是 `np.polynomial.polynomial.Polynomial`、`np.polynomial.polynomial.polyval` 都可用。
- 但顶层**没有** `np.polynomial.polyval`，因为函数式 API 的名字（`polyval`、`polyadd` 等）从未被导入到顶层命名空间。

一个容易混淆但很重要的点：`np.polynomial.Polynomial` 和 `np.polynomial.polynomial.Polynomial` 是**同一个类对象**，因为 `__init__.py` 用 `from .polynomial import Polynomial` 把它「原封不动」地搬了上来，并没有复制。两条路径只是「导航深浅」不同。

#### 4.2.3 源码精读

[`__init__.py` 模块文档字符串](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/__init__.py#L29-L54) 第 29–54 行明确写道：便捷类是「preferred interface」，从 `numpy.polynomial` 命名空间即可取用，免去「navigate to the corresponding submodules」的麻烦，并提供「a more consistent and concise interface」。它还举了一个对比例子：用 `Chebyshev.fit(...)` 比用 `chebfit(...)` 更被推荐。

[__init__.py:L29-L36](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/__init__.py#L29-L36) —— 官方原话：便捷类是首选接口，直接从 `numpy.polynomial` 取用即可，省去进入子模块的步骤。

[__init__.py:L124-L132](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/__init__.py#L124-L132) —— `__all__` 的内容。每一行成对出现「模块名 + 类名」，决定了上面表格里的可用性。

[polynomial.py:L76-L82](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L76-L82) —— 家族模块自己的 `__all__`，列出了 `polyval`、`polyadd`、`polyfit` 等函数式 API。这些名字**只在本模块命名空间里**，所以必须写成 `np.polynomial.polynomial.polyval`。

#### 4.2.4 代码实践

**实践目标**：亲手验证「两条导入路径拿到的是同一个类」「函数式 API 必须带子模块路径」这两件事。

**操作步骤**：

```python
import numpy as np

# 路径 A：便捷类，从顶层命名空间
from numpy.polynomial import Polynomial as A
# 路径 B：同一个类，从子模块
from numpy.polynomial.polynomial import Polynomial as B

print("A is B :", A is B)          # 期望 True：同一个类对象

# 顶层是否能直接拿到函数式 API？
try:
    np.polynomial.polyval([1, 2, 3], 0)
except AttributeError as e:
    print("顶层无 polyval ->", e)

# 必须带子模块路径
print(np.polynomial.polynomial.polyval([1, 2, 3], 0))   # 1 + 0 = 1
```

**需要观察的现象**：

- `A is B` 为 `True`，证明两条路径指向同一个类。
- `np.polynomial.polyval` 抛 `AttributeError`，而 `np.polynomial.polynomial.polyval` 正常返回。

**预期结果**：`A is B` 打印 `True`；顶层访问 `np.polynomial.polyval` 失败。结合 `__init__.py` 的 `__all__` 你就能解释：因为 `polyval` 从未被导入顶层。

> 待本地验证：上面 `polyval([1, 2, 3], 0)` 的返回值。按 `polyval(x, c)` 的签名，这里把 `x=0`、`c=[1,2,3]` 代入（参数顺序为 `polyval(x, c)`，故 `1*1+2*0+3*0**2 = 1`），应返回 `1`，但请自行运行确认。

#### 4.2.5 小练习与答案

**练习 1**：判断正误并说明依据——「`np.polynomial.Chebyshev` 与 `np.polynomial.chebyshev.Chebyshev` 是同一个类」。

**参考答案**：正确。`__init__.py` 里 `from .chebyshev import Chebyshev` 只是把子模块里已存在的类「引用」到顶层，并未复制。用 `np.polynomial.Chebyshev is np.polynomial.chebyshev.Chebyshev` 可验证为 `True`。

**练习 2**：如果你只想用「函数式 API」对一个幂级数数组做加法，正确的导入是 `from numpy.polynomial import polyadd` 吗？为什么？

**参考答案**：不正确。`polyadd` 不在顶层命名空间。正确写法是 `from numpy.polynomial.polynomial import polyadd`，或 `np.polynomial.polynomial.polyadd(...)`。顶层只导出了类名，没导出 `poly*` 函数名。

**练习 3**：官方为什么更推荐便捷类 API 而不是函数式 API？（结合 `__init__.py` 文档字符串）

**参考答案**：便捷类提供「更一致、更简洁」的接口（面向对象、运算符重载、统一的 `fit/deriv/integ/roots` 方法），且直接在顶层命名空间可用；函数式 API 需要记住每族函数的前缀和子模块路径，写法更冗长。两者底层算法相同，便捷类只是更友好的封装。

### 4.3 `set_default_printstyle` 与 `test` 入口

#### 4.3.1 概念说明

除了六大类，`__init__.py` 还对外暴露两个「工具」：

- **`set_default_printstyle(style)`**：全局设置多项式打印成字符串时用 `unicode`（带上下标符号）还是 `ascii`（纯 ASCII）风格。
- **`test`**：一个 `PytestTester` 对象，调用 `np.polynomial.test()` 即可用 pytest 跑通整个子包的测试。

这两个名字都在 `__all__` 里，是 `numpy.polynomial` 公开 API 的一部分。

#### 4.3.2 核心流程

`set_default_printstyle` 的逻辑很短：

```text
1. 校验 style ∈ {'unicode', 'ascii'}，否则抛 ValueError。
2. 令 _use_unicode = (style == 'unicode')。
3. 把 ABCPolyBase._use_unicode 设为该布尔值。
```

它修改的是**类属性** `ABCPolyBase._use_unicode`。由于六个便捷类都继承自 `ABCPolyBase`，改一次类属性，所有类的默认打印风格就同时生效。这背后是「类属性被子类共享」的 Python 机制。

`test` 入口更简单——`PytestTester(__name__)` 把「模块名」`numpy.polynomial` 交给 pytest 去收集并运行其下所有测试。

#### 4.3.3 源码精读

[__init__.py:L135-L181](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/__init__.py#L135-L181) —— `set_default_printstyle` 的定义与文档字符串。文档里给出了 `unicode`/`ascii` 两种风格的打印样例（`1.0 + 2.0·x + 3.0·x²` vs `1.0 + 2.0 x + 3.0 x**2`），并说明默认风格与平台有关：Unix 默认 `unicode`，Windows 默认 `ascii`（因为字体对上下标的支持不同）。

[__init__.py:L172-L181](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/__init__.py#L172-L181) —— 实现核心：先校验取值，再把布尔值写进 `ABCPolyBase._use_unicode`。注意这里 `from ._polybase import ABCPolyBase` 是「延迟导入」（写在函数体内），目的是避免在模块顶层产生循环依赖。

[__init__.py:L184-L187](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/__init__.py#L184-L187) —— `test` 入口的诞生：`from numpy._pytesttester import PytestTester`，`test = PytestTester(__name__)`，随后 `del PytestTester`（不让 `PytestTester` 这个名字污染命名空间，只留下 `test`）。`__name__` 在这里就是字符串 `"numpy.polynomial"`。

> 提示：`test` 是一个**对象**（不是函数），但它是「可调用」的——调用它 `np.polynomial.test()` 就触发 pytest。打印类型应为 `<class 'numpy._pytesttester.PytestTester'>`。运行完整测试需要环境里装了 `pytest`，且耗时可能较长。

#### 4.3.4 代码实践

**实践目标**：切换打印风格、用 `__format__` 临时格式化，并确认 `test` 入口的类型。

**操作步骤**：

```python
import numpy as np
from numpy.polynomial import Polynomial, Chebyshev

p = Polynomial([1, 2, 3])
c = Chebyshev([1, 2, 3])

np.polynomial.set_default_printstyle('unicode')
print("unicode:", p)        # 1.0 + 2.0·x + 3.0·x²

np.polynomial.set_default_printstyle('ascii')
print("ascii  :", p)        # 1.0 + 2.0 x + 3.0 x**2

# 用 __format__ 临时指定风格，不改全局默认
print("format :", f"{p:unicode}")

# 查看类型，确认 test 是 PytestTester 对象
print("test   :", type(np.polynomial.test))
```

**需要观察的现象**：

- 切换 `set_default_printstyle` 后，`print(p)` 的输出在 `unicode` 与 `ascii` 两种风格间变化。
- `f"{p:unicode}"` 即便全局设为 ascii，仍输出 unicode 风格。
- `type(np.polynomial.test)` 是 `numpy._pytesttester.PytestTester`。

**预期结果**：两行打印分别呈现上下标符号版本与纯 ASCII 版本；`test` 的类型是 `PytestTester`。这与 [u1-l1](u1-l1-package-overview.md) 里看到的六大类属性一致——打印行为由 `ABCPolyBase` 统一管，本讲只是从「目录/导入」角度确认它挂在 `__init__.py` 里。

> 待本地验证：若尝试非法取值 `np.polynomial.set_default_printstyle('latex')`，应抛出 `ValueError`（对应源码第 172–176 行的校验）。

#### 4.3.5 小练习与答案

**练习 1**：`set_default_printstyle('ascii')` 是怎么做到「改一次，六个类同时生效」的？

**参考答案**：它把布尔值写入类属性 `ABCPolyBase._use_unicode`。六个便捷类都继承自 `ABCPolyBase` 且自己没有覆盖这个属性，因此通过属性查找都能读到同一个最新值。这是 Python「类属性被子类共享」的标准用法。

**练习 2**：为什么 `from ._polybase import ABCPolyBase` 写在 `set_default_printstyle` 函数体内部，而不是写在 `__init__.py` 文件顶部？

**参考答案**：这是延迟导入，常用来避免循环导入。`_polybase.py` 与 `__init__.py` 之间若在顶层互相 import 可能产生导入顺序问题；把导入推迟到函数真正被调用时，能打破这种潜在循环，保证模块加载顺序安全。

**练习 3**：`np.polynomial.test` 到底是什么？调用 `np.polynomial.test()` 会发生什么？

**参考答案**：它是一个 `numpy._pytesttester.PytestTester` 实例，用 `__name__`（即 `"numpy.polynomial"`）构造。调用它会用 pytest 收集并运行整个 `numpy.polynomial` 子包（含 `tests/` 目录）下的全部测试。运行前需要确保环境装了 `pytest`。

## 5. 综合实践

**任务**：分别用 `np.polynomial.Polynomial` 与 `np.polynomial.polynomial.Polynomial` 两条路径创建同一个多项式 \( 1 + 2x + 3x^2 \)，对照 `__init__.py` 的 `__all__` 导出，解释为何推荐前者。

**操作步骤**：

```python
import numpy as np

# 路径 A（推荐）：顶层便捷类
p_a = np.polynomial.Polynomial([1, 2, 3])

# 路径 B：钻进子模块取同一个类
p_b = np.polynomial.polynomial.Polynomial([1, 2, 3])

print("同一对象 :", type(p_a) is type(p_b), "|", p_a.coef, p_b.coef)
print("顶层导出 :", np.polynomial.__all__)

# 对照：函数式 API 必须带子模块路径
coef_a = np.polynomial.polynomial.polyadd([1, 2, 3], [0, 0, 1])
print("polyadd :", coef_a)   # 期望 [1. 2. 4.]
```

**你需要回答的问题**：

1. `p_a` 与 `p_b` 的类型是否相同？为什么？
2. 看 `np.polynomial.__all__`：里面为什么成对出现 `"polynomial"` 和 `"Polynomial"`？这跟「顶层能用类、却用不到 `polyadd`」有什么关系？
3. 既然两条路径等价，官方为什么推荐 `np.polynomial.Polynomial` 而不是 `np.polynomial.polynomial.Polynomial`？

**参考解释**：

1. 类型相同。`__init__.py` 第 122 行 `from .polynomial import Polynomial` 只是把子模块里的类「引用」到顶层，没有复制，`type(p_a) is type(p_b)` 为 `True`。
2. `__all__`（第 124–132 行）里小写 `polynomial` 是**模块**，大写 `Polynomial` 是**类**。顶层只导入了类，没导入 `poly*` 函数，所以 `np.polynomial.Polynomial` 可用、`np.polynomial.polyval` 不可用，而 `np.polynomial.polynomial.polyval` 可用。
3. 推荐前者是因为它更短、更一致（六个类都在顶层），官方在文档字符串第 29–36 行明确把它列为「preferred interface」。

## 6. 本讲小结

- `numpy/polynomial/` 按「工具地基 `polyutils.py` → 抽象基类 `_polybase.py` → 六个家族模块 → 门面 `__init__.py`」分层组织，依赖关系单向、清晰。
- 每个家族模块（如 `polynomial.py`）既提供一套函数式 API（`polyval`、`polyadd`…），又定义一个便捷类（如 `Polynomial`），后者继承 `ABCPolyBase` 并把「虚函数」委托给前者。
- `__init__.py` 的 `__all__` 成对列出「模块名 + 类名」：类被提到顶层（`np.polynomial.Polynomial` 可用），函数仍留在子模块（`np.polynomial.polyval` 不可用，需写 `np.polynomial.polynomial.polyval`）。
- `np.polynomial.Polynomial` 与 `np.polynomial.polynomial.Polynomial` 是**同一个类对象**，两条路径只是导航深浅不同；官方推荐前者，因为它更简洁一致。
- `set_default_printstyle('unicode'|'ascii')` 通过改写类属性 `ABCPolyBase._use_unicode` 一次性切换六个类的打印风格；`np.polynomial.test` 是一个 `PytestTester` 对象，调用即可跑通整个子包的测试。

## 7. 下一步学习建议

本讲只把「目录和导入」看懂，还没有深入任何一个类的行为。接下来建议：

- 学 **[u1-l3 多项式类快速上手](u1-l3-polynomial-class-quickstart.md)**：用 `Polynomial` 类动手做创建、求值、算术、拟合、微积分，建立「便捷类怎么用」的直觉。
- 若想彻底搞懂「虚函数委托」这套机制（`_add = staticmethod(polyadd)` 到底怎么起作用），直接进 **[u2-l1 ABCPolyBase 抽象基类与虚函数模式](u2-l1-abcpolybase-virtual-methods.md)**。
- 想先吃透函数式 API 的命名规律与内置常数（`polyzero/polyone/polyx/polydomain`），可看 **[u1-l4 函数式 API 与内置常数](u1-l4-functional-api-and-constants.md)**。
