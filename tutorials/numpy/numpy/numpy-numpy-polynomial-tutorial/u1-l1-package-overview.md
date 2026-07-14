# 项目定位与多项式表示约定

> 本讲是 numpy.polynomial 学习手册的第一篇。我们从最基本的问题开始：这个子包是做什么的？它用什么数据结构表示一个多项式？官方推荐我们用哪几个类？把这三件事弄清楚，后面所有的源码阅读才有落脚点。

## 1. 本讲目标

读完本讲，你应当能够：

- 说出 `numpy.polynomial` 子包的整体定位，以及它为什么独立于旧版的 `numpy.poly1d`。
- 掌握全包统一的**系数表示约定**：一个 1-D 数组，下标对应次数，从低次到高次排列，即 \( c_0 + c_1 P_1(x) + c_2 P_2(x) + \dots \)。
- 认识六大**便捷类（convenience class）**：`Polynomial`、`Chebyshev`、`Legendre`、`Laguerre`、`Hermite`、`HermiteE`，并知道它们的导入路径与各自代表的基。

## 2. 前置知识

本讲面向零基础读者，但有几个小概念先讲清楚会更顺：

- **多项式（polynomial）**：形如 \( c_0 + c_1 x + c_2 x^2 + \dots + c_n x^n \) 的表达式。\(c_i\) 叫系数，\(x\) 叫变量，最高次 \(n\) 叫次数（degree）。
- **基（basis）**：同样一个多项式函数，可以用不同的“积木”拼出来。最常见的是**标准幂基** \(1, x, x^2, \dots\)；也有**正交多项式基**，如切比雪夫（Chebyshev）基 \(T_0(x), T_1(x), T_2(x), \dots\)。换基只是换一种“记法”，函数本身不变。
- **1-D 数组**：NumPy 里的 `np.array([1, 2, 3])`，就是一个一维序列。本子包就是用一个 1-D 数组来“存”一个多项式。

> 如果上面这几句你都能接受，本讲就不会有阅读障碍。

## 3. 本讲源码地图

本讲只聚焦一个文件，它就是整个子包的“门面”：

| 文件 | 作用 |
|------|------|
| [`__init__.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/__init__.py) | 子包入口。模块文档字符串写明了全包的系数约定与设计目标；这里导入并导出六大便捷类，还提供 `set_default_printstyle` 打印开关与 `test()` 测试入口。 |

补充参考（用于解释“为何独立于 `poly1d`”，位于子包之外但同属本仓库）：

| 文件 | 作用 |
|------|------|
| `numpy/lib/_polynomial_impl.py` | 旧版 `numpy.poly1d` 类的实现所在，其文档字符串注明它是 legacy API。 |
| `doc/source/reference/routines.polynomials.rst` | 官方“旧 API → 新 API 过渡指南”，明确两者系数顺序的差异与推荐关系。 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**包定位与设计目标**、**系数表示约定**、**六大便捷类一览**。

### 4.1 包定位与设计目标

#### 4.1.1 概念说明

`numpy.polynomial` 是 NumPy 内部一个专门“高效处理多项式”的子包。一句话定位就写在它的模块文档字符串第一行：

> A sub-package for efficiently dealing with polynomials.

它要解决的核心问题是：**把对多项式的所有操作（求值、加减乘除、求导积分、拟合、求根……）统一转化为对系数数组的操作**。这样做的好处是性能好（直接用数组运算，不走对象调度）、接口一致（六大类共享同一套方法名）、并且能稳定地支持多种基。

#### 4.1.2 核心流程

这个子包的整体设计可以概括成三步：

1. **选一种基**：标准幂基 `Polynomial`，或某个正交基 `Chebyshev/Legendre/...`。
2. **用 1-D 系数数组表示多项式**：系数顺序“从低次到高次”，与旧 `poly1d` 相反。
3. **所有操作都作用在系数上**：求值、算术、微积分、拟合、求根都是数组运算。

```text
选基 ──► 系数数组 (1-D, 低→高) ──► 对系数做运算 ──► 得到结果
```

#### 4.1.3 源码精读

定位写在模块文档字符串开篇：

[__init__.py:1-13](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/__init__.py#L1-L13) —— 子包自我介绍：它是一个“高效处理多项式”的子包，并预告了“用 1-D 系数数组、从低次到高次”这一统一约定。其中关键一句是：

> all operations on polynomials, including evaluation at an argument, are implemented as operations on the coefficients.

这正是本子包的设计纲领：**一切操作即系数操作**。

那它和旧版 `numpy.poly1d` 是什么关系？答案在 legacy 类的文档字符串里。`poly1d` 类位于 `numpy/lib/_polynomial_impl.py`：

[numpy/lib/_polynomial_impl.py:1096-1118](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/lib/_polynomial_impl.py#L1096-L1118) —— `poly1d` 的类文档字符串明确写着：自 1.4 版起，定义在 `numpy.polynomial` 中的新 API 才是首选（`the new polynomial API defined in numpy.polynomial is preferred`），`poly1d` 属于“旧的多项式 API”。注意它的系数是**降幂**排列的：`poly1d([1,2,3])` 表示 \(x^2 + 2x + 3\)。

官方过渡指南把两者放在一起对比：

[doc/source/reference/routines.polynomials.rst:79-85](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/doc/source/reference/routines.polynomials.rst#L79-L85) —— 说明 `numpy.polynomial` 的系数“从零次项向上排”，**与 poly1d 惯例相反（reverse order）**，并给出一条好记的规则：**`coef[i]` 就是第 \(i\) 次项的系数**。

因此可以小结：`numpy.polynomial` 并不是 `poly1d` 的小修小补，而是一套**系数顺序相反、支持多基、更适合新代码**的并行 API。后续本子包所有源码都遵循“系数即第 i 次项”这一约定。

#### 4.1.4 代码实践

**实践目标**：亲手确认子包的导入路径，并验证它确实独立于 `poly1d`。

**操作步骤**：

1. 在能 `import numpy` 的环境里，分别尝试两种导入：
   ```python
   import numpy as np
   print(np.polynomial.Polynomial)   # 新 API 的便捷类
   print(np.poly1d)                  # 旧 API 的类
   ```
2. 阅读本讲的 [`__init__.py` 文档字符串](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/__init__.py#L1-L13)，确认“一切操作即系数操作”这句话。

**需要观察的现象**：

- `np.polynomial` 是一个子包（package），而 `np.poly1d` 是单个类，二者来自不同的模块层级。

**预期结果**：

- `np.polynomial.Polynomial` 打印为 `<class 'numpy.polynomial.polynomial.Polynomial'>`。
- `np.poly1d` 打印为 `<class 'numpy.poly1d'>`。
- 若环境较新，`np.poly1d` 可能伴随 `DeprecationWarning`（视 NumPy 版本而定，**待本地验证**具体告警文案）。

#### 4.1.5 小练习与答案

**练习 1**：`np.poly1d([1, 2, 3])` 和 `np.polynomial.Polynomial([1, 2, 3])` 表示的是同一个多项式吗？

**参考答案**：不是。`poly1d` 是降幂，`poly1d([1,2,3])` 表示 \(x^2 + 2x + 3\)；而 `Polynomial` 是升幂，`Polynomial([1,2,3])` 表示 \(1 + 2x + 3x^2\)。两者互为系数倒序。

**练习 2**：为什么官方说新代码应优先用 `numpy.polynomial`？

**参考答案**：因为 `poly1d` 属于 legacy API，官方过渡指南（`routines.polynomials.rst`）明确说明 `numpy.polynomial` 接口更一致、行为更稳定，并推荐用于新代码。

---

### 4.2 系数表示约定

#### 4.2.1 概念说明

这是本讲**最重要**的一节。整个子包——六大类、上百个函数——全部建立在同一条约定上：

> 一个多项式用一个 **1-D 数组**表示，元素**从最低次项到最高次项**排列，且 `coef[i]` 就是第 \(i\) 次项 \(P_i(x)\) 的系数。

也就是说，给定系数数组 \(c = [c_0, c_1, \dots, c_n]\)，它代表的多项式是

\[
p(x) = c_0 P_0(x) + c_1 P_1(x) + c_2 P_2(x) + \dots + c_n P_n(x)
\]

其中 \(P_i(x)\) 是“当前这个模块对应的第 \(i\) 阶基函数”。注意：**同一个系数数组，在不同的基下代表不同的多项式函数**。例如 `array([1,2,3])`：

- 在**标准幂基**下（`polynomial` 模块，\(P_i(x)=x^i\)）：

\[
1\cdot x^0 + 2\cdot x^1 + 3\cdot x^2 = 1 + 2x + 3x^2
\]

- 在**切比雪夫基**下（`chebyshev` 模块，\(T_0=1, T_1=x, T_2=2x^2-1\)）：

\[
1\cdot T_0(x) + 2\cdot T_1(x) + 3\cdot T_2(x) = 1 + 2x + 3(2x^2-1) = 6x^2 + 2x - 2
\]

这正是文档里那句 `array([1,2,3]) represents P_0 + 2*P_1 + 3*P_2` 的含义。

#### 4.2.2 核心流程

把“系数数组 → 多项式”的对应关系画成流程：

```text
系数数组 c = [c0, c1, c2, ..., cn]
        │
        │  下标 i 对应次数 i：coef[i] = 第 i 次项系数
        ▼
p(x) = c0·P0(x) + c1·P1(x) + ... + cn·Pn(x)
        │
        │  P_i 由“当前模块选定的基”决定
        ▼
   standard 基:  P_i(x) = x^i
   chebyshev 基: P_i(x) = T_i(x)
   legendre  基: P_i(x) = L_i(x)
   ...
```

两条必须记住的规则：

1. **下标即次数**：`c[i]` 永远是第 `i` 次项的系数（与 `poly1d` 相反）。
2. **基随模块而变**：同样的数组，在不同模块里含义不同。

#### 4.2.3 源码精读

这条约定由模块文档字符串权威定义：

[__init__.py:1-13](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/__init__.py#L1-L13) —— 明确写道：多项式由一个 1-D numpy 数组表示，系数“从最低次项到最高次项”排列；并给出例子 `array([1,2,3]) represents P_0 + 2*P_1 + 3*P_2`，其中 `P_n` 是“当前相关模块适用的第 n 阶基函数”，并特别点名 `polynomial`（包装“标准”基）和 `chebyshev` 两个例子。

> 这段是整个子包的“宪法”。后面阅读任何模块源码，只要看到对系数数组的循环，都可以默认“下标 = 次数”。

#### 4.2.4 代码实践

**实践目标**：用函数式 API 验证“下标即次数”这条约定。

**操作步骤**：

```python
import numpy as np
from numpy.polynomial import polynomial as P

c = np.array([1, 2, 3])          # 约定：1 + 2x + 3x^2
x = 2.0
print(P.polyval(x, c))           # 在 x=2 处求值
# 手算预期：1 + 2*2 + 3*2**2 = 1 + 4 + 12 = 17
```

**需要观察的现象**：

- 把 `c` 当作升幂系数，`polyval(2, c)` 的结果应该正好等于手算的 \(1+2\cdot2+3\cdot2^2=17\)。
- 如果误把它当成 `poly1d` 的降幂（即 \(x^2+2x+3\)），会算成 \(4+4+3=11\)，与实际输出不符——这就反证了“升幂”约定。

**预期结果**：

- `P.polyval(2.0, np.array([1,2,3]))` 输出 `17.0`，与升幂手算一致。

#### 4.2.5 小练习与答案

**练习 1**：写出 `array([0, 0, 5])` 在标准幂基下表示的多项式。

**参考答案**：\(0\cdot x^0 + 0\cdot x^1 + 5\cdot x^2 = 5x^2\)。注意前导的零系数不改变函数，但会占用数组长度。

**练习 2**：若把 `array([1,2,3])` 交给 `poly1d` 和 `Polynomial`，它们在 \(x=2\) 处的值分别是多少？

**参考答案**：`poly1d` 视为 \(x^2+2x+3\)，\(x=2\) 时为 \(4+4+3=11\)；`Polynomial` 视为 \(1+2x+3x^2\)，\(x=2\) 时为 \(1+4+12=17\)。两者不等，再次说明两套 API 系数顺序相反。

---

### 4.3 六大便捷类一览

#### 4.3.1 概念说明

子包为**六种不同的多项式基**各提供了一个**便捷类（convenience class）**。这些类接口一致（创建、算术、拟合、微积分、求根的方法名都相同），是官方推荐的入口：

| 便捷类 | 模块 | 提供的级数 |
|--------|------|-----------|
| `Polynomial` | `polynomial` | 幂级数（标准基） |
| `Chebyshev` | `chebyshev` | 切比雪夫级数 |
| `Legendre` | `legendre` | 勒让德级数 |
| `Laguerre` | `laguerre` | 拉盖尔级数 |
| `Hermite` | `hermite` | 埃尔米特级数（物理型） |
| `HermiteE` | `hermite_e` | 埃尔米特级数（概率型） |

文档里强调：便捷类是**首选接口**，并且都暴露在 `numpy.polynomial` 命名空间下，所以推荐写 `np.polynomial.Polynomial`，而不是再深入一层写 `np.polynomial.polynomial.Polynomial`。

每个类还自带四个常量（后续讲义会逐一展开）：`domain`（默认域）、`window`（默认窗口）、`basis_name`（基的符号）、`maxpower`（允许的最大幂次）。

#### 4.3.2 核心流程

便捷类的使用套路：

```text
np.polynomial.<ClassName>(coef)
        │
        ├── 创建：ClassName(coef) / ClassName.fit(x,y,deg) / ClassName.fromroots(...)
        ├── 求值：p(x)
        ├── 算术：p + q, p * q, p ** 3
        ├── 微积分：p.deriv(), p.integ()
        ├── 拟合：ClassName.fit(x, y, deg)
        └── 求根：p.roots()
```

六大类的差异，主要体现在三处：**基函数本身**、**默认 domain/window**、**打印时用的 `basis_name` 符号**。

#### 4.3.3 源码精读

六大类在 `__init__.py` 里集中导入：

[__init__.py:117-122](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/__init__.py#L117-L122) —— 从各子模块导入六大便捷类：`Chebyshev`、`Hermite`、`HermiteE`、`Laguerre`、`Legendre`、`Polynomial`。

[__init__.py:124-132](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/__init__.py#L124-L132) —— `__all__` 把这六个类（连同同名小写模块名和 `set_default_printstyle`）一并导出，这就是为什么 `from numpy.polynomial import Polynomial` 能直接可用。

文档里的类总览表与“首选接口”说明：

[__init__.py:15-27](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/__init__.py#L15-L27) —— 列出六大便捷类及其“提供的级数”。

[__init__.py:29-38](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/__init__.py#L29-L38) —— 说明便捷类提供一致接口，是首选入口，且可直接从 `numpy.polynomial` 命名空间取用（无需下钻到子模块）。

各类的关键常量（下面这张表里的每一项都来自源码定义，可直接对照阅读）：

| 类 | 定义位置 | `basis_name` | 默认 `domain` | 默认 `window` |
|----|----------|--------------|---------------|---------------|
| `Polynomial`  | [polynomial.py:1656-1658](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L1656-L1658) | `None`（打印为 `x`） | `[-1, 1]` | `[-1, 1]` |
| `Chebyshev`   | [chebyshev.py:2051-2053](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/chebyshev.py#L2051-L2053) | `'T'` | `[-1, 1]` | `[-1, 1]` |
| `Legendre`    | [legendre.py:1653-1655](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/legendre.py#L1653-L1655) | `'P'` | `[-1, 1]` | `[-1, 1]` |
| `Laguerre`    | [laguerre.py:1727-1729](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/laguerre.py#L1727-L1729) | `'L'` | `[0, 1]` | `[0, 1]` |
| `Hermite`     | [hermite.py:1792-1794](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/hermite.py#L1792-L1794) | `'H'` | `[-1, 1]` | `[-1, 1]` |
| `HermiteE`    | [hermite_e.py:1690-1692](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/hermite_e.py#L1690-L1692) | `'He'` | `[-1, 1]` | `[-1, 1]` |

> 两个细节先留个印象：① `Polynomial` 的 `basis_name` 是 `None`，所以它打印时自变量直接写成 `x`（其它类写成 `T_n(x)`、`P_n(x)` 等）；② 只有 `Laguerre` 的默认 domain 是 `[0, 1]`，其余五族都是 `[-1, 1]`——这和各正交多项式的天然定义区间有关，后续讲义会展开。

#### 4.3.4 代码实践

**实践目标**：实例化六大类，观察它们的 `basis_name` 与默认 `domain`。

**操作步骤**：

```python
import numpy as np
from numpy.polynomial import (Polynomial, Chebyshev, Legendre,
                              Laguerre, Hermite, HermiteE)

for cls in (Polynomial, Chebyshev, Legendre, Laguerre, Hermite, HermiteE):
    print(cls.__name__, "| basis_name =", cls.basis_name,
          "| domain =", cls.domain)
```

**需要观察的现象**：

- 六个类的 `basis_name` 分别是 `None / 'T' / 'P' / 'L' / 'H' / 'He'`。
- 除 `Laguerre` 的 domain 是 `[0. 1.]` 外，其余都是 `[-1. 1.]`。

**预期结果**：

- 输出与上一节的表格完全一致（`Polynomial` 的 `basis_name` 显示为 `None`）。
- 这些常量是**类属性**（挂在类上，不是实例上），无需创建实例即可读取。

#### 4.3.5 小练习与答案

**练习 1**：为什么推荐写 `np.polynomial.Chebyshev` 而不是 `np.polynomial.chebyshev.Chebyshev`？

**参考答案**：因为 `__init__.py` 已把 `Chebyshev` 导出到 `numpy.polynomial` 命名空间（见 `__all__`）。两种写法得到的是同一个类，前者更短、更符合官方“首选接口”的推荐。

**练习 2**：六大类里，哪一族的默认 `domain` 与其它不同？是什么？

**参考答案**：`Laguerre`。它的默认 `domain` 与 `window` 都是 `[0, 1]`，而其它五族都是 `[-1, 1]`。

---

## 5. 综合实践

把本讲三个模块串起来，完成下面这个贯穿任务（即本讲指定的实践任务）。

**任务**：阅读 [`__init__.py` 的模块文档字符串](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/__init__.py#L1-L13)，写出 `array([1, 2, 3])` 在**标准基**和 **Chebyshev 基**下分别代表的表达式，再用 `np.polynomial.Polynomial([1, 2, 3])` 与 `np.polynomial.Chebyshev([1, 2, 3])` 创建对象，验证打印结果。

**操作步骤**：

```python
import numpy as np

# 1) 先在纸面上写出两种基下的表达式（见下方“预期结果”）

# 2) 创建对象并打印（unicode 风格）
np.polynomial.set_default_printstyle('unicode')
p = np.polynomial.Polynomial([1, 2, 3])
c = np.polynomial.Chebyshev([1, 2, 3])
print(p)
print(c)

# 3) 也可以切换 ascii 风格对照
np.polynomial.set_default_printstyle('ascii')
print(p)
print(c)
```

**需要观察的现象**：

- 同样的系数 `[1, 2, 3]`，在 `Polynomial` 下打印成 `x` 的幂次，在 `Chebyshev` 下打印成 `T_n(x)`——**同一组系数，不同基，含义不同**。
- 系数被存成了浮点（`1.0` 而非 `1`），说明便捷类内部会把系数规整为统一的浮点 dtype。

**预期结果**（这些输出直接来自 `set_default_printstyle` 的 doctest 示例，见 [`__init__.py:154-167`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/__init__.py#L154-L167)）：

```text
# unicode 风格
1.0 + 2.0·x + 3.0·x²            # Polynomial：1 + 2x + 3x²
1.0 + 2.0·T₁(x) + 3.0·T₂(x)     # Chebyshev：1·T0 + 2·T1 + 3·T2

# ascii 风格
1.0 + 2.0 x + 3.0 x**2
1.0 + 2.0 T_1(x) + 3.0 T_2(x)
```

纸面表达式对照：

- 标准基：\(1 + 2x + 3x^2\)
- Chebyshev 基（写成级数形式）：\(1\cdot T_0(x) + 2\cdot T_1(x) + 3\cdot T_2(x)\)；若展开成标准多项式则为 \(6x^2 + 2x - 2\)

> 说明：上面 unicode/ascii 两段打印输出与 `__init__.py` 中 `set_default_printstyle` 文档字符串里的 doctest 完全一致，因此可作为可靠预期；但仍建议本地运行一遍亲手确认。

## 6. 本讲小结

- `numpy.polynomial` 是一个“高效处理多项式”的子包，核心理念是**把对多项式的所有操作都实现为对系数数组的操作**。
- 它独立于旧版 `numpy.poly1d`：`poly1d` 是 legacy、系数**降幂**；新 API 自 NumPy 1.4 起为首选、系数**升幂**。
- 全包统一约定：多项式 = 1-D 系数数组，**下标即次数**，`coef[i]` 就是第 \(i\) 次项 \(P_i(x)\) 的系数。
- 同一组系数在不同基下含义不同：`array([1,2,3])` 在标准基是 \(1+2x+3x^2\)，在 Chebyshev 基是 \(T_0+2T_1+3T_2\)。
- 子包提供六大便捷类 `Polynomial/Chebyshev/Legendre/Laguerre/Hermite/HermiteE`，接口一致、从 `numpy.polynomial` 命名空间可直接取用，是首选入口。
- 各类自带 `domain/window/basis_name/maxpower` 四个类属性；其中只有 `Laguerre` 的默认 domain 是 `[0,1]`，其余为 `[-1,1]`。

## 7. 下一步学习建议

你已经掌握了“系数约定”和“六大类总览”，接下来的自然顺序是：

- **下一篇（u1-l2）**：进入目录结构层面，看清 `_polybase.py / polyutils.py / polynomial.py / chebyshev.py` 等文件各司什么职，以及“便捷类 API”与“函数式 API”两条导入路径的区别，并学会用 `np.polynomial.test()` 跑包内测试。
- **随后（u1-l3）**：以最常用的 `Polynomial` 类做快速上手（创建、求值、算术、拟合、微积分、求根）。
- 阅读源码时，可先打开 [`__init__.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/__init__.py) 当作总目录，再按需点进各子模块。
