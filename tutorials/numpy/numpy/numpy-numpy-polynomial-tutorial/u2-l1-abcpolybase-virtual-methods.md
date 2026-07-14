# ABCPolyBase 抽象基类与虚函数模式

## 1. 本讲目标

本讲是「核心抽象 ABCPolyBase」单元的第一篇。在第 1 单元里我们已经会**用**六大便捷类（`Polynomial`/`Chebyshev`/…）做创建、求值、算术、拟合，但一直把它们当成黑盒。本讲要打开这个黑盒，看清它们的共同骨架 `ABCPolyBase` 是怎么定义的。

学完本讲，你应该能够：

1. 说清 `ABCPolyBase` 为什么是一个「mixin + `abc.ABC`」的抽象基类，以及它给六大便捷类提供了哪些**统一行为**。
2. 逐个列出 `ABCPolyBase` 中用 `@staticmethod @abc.abstractmethod` 声明的 **12 个虚函数**（`_add`/`_mul`/`_val`/`_fit`/`_roots` 等），并说出每个虚函数的职责与命名约定。
3. 看懂子类如何用一行 `_add = staticmethod(polyadd)` 把抽象方法「绑定」到本模块的具体函数上，理解这条贯穿全包的委托链。
4. 解释 `__array_ufunc__ = None` 与 `__hash__ = None` 这两个「退出机制」存在的理由。
5. 预测「如果子类忘记实现某个虚函数（例如 `_roots`）」会发生什么。

本讲不涉及具体算法（Horner 法、求根、z-series 等留到后面），只关注**类层次结构与多态机制**。

## 2. 前置知识

本讲假设你已经掌握第 1 单元的内容。这里回顾三个关键点，再补充一个本讲要用到的新概念。

### 2.1 回顾：六大便捷类接口完全一致

`Polynomial`、`Chebyshev`、`Legendre`、`Laguerre`、`Hermite`、`HermiteE` 这六个类，对外暴露的方法几乎一模一样：都能 `p(x)` 求值、`p + q`、`p * q`、`p.fit(...)`、`p.deriv()`、`p.roots()`……差异只在**基函数**和**默认 domain**。

> 问题来了：六个类的行为高度雷同，难道每个类都把这套方法各自抄一遍？显然不该。本讲给出的答案就是：它们全部继承自同一个抽象基类 `ABCPolyBase`。

### 2.2 回顾：系数表示约定

系数数组 `c` 从低次到高次排列，`c[i]` 是第 `i` 次基函数 `P_i(x)` 的系数：

\[ p(x) = c_0\,P_0(x) + c_1\,P_1(x) + c_2\,P_2(x) + \cdots \]

同一组系数，在「标准幂基」\(P_i(x)=x^i\) 与「Chebyshev 基」\(P_i(x)=T_i(x)\) 下代表**不同的函数**。这正是「同一接口、不同基」的根本原因，也是虚函数模式要解决的问题。

### 2.3 回顾：三层委托

第 1 单元提到，便捷类内部把操作委托给本模块的函数式 API，函数式 API 又把通用部分委托给 `polyutils`。本讲要精确刻画第一层「便捷类 → 函数式 API」的委托是**如何**发生的：它不是手工调用，而是通过**虚函数赋值**。

### 2.4 新概念：什么是「抽象基类」与「虚函数」

- **抽象基类（Abstract Base Class, ABC）**：一种「只定规矩、不准直接实例化」的类。它声明若干**抽象方法**（只有签名没有实现），规定「任何子类必须实现这些方法，否则不许创建实例」。Python 标准库 `abc` 模块提供这个能力。
- **虚函数（virtual function）**：这是借用 C++ 的术语。在 NumPy 这里，它指 `ABCPolyBase` 里那些 `_` 开头、被子类用具体函数「填空」的抽象方法。父类只负责**在合适的时机调用它们**（比如 `__add__` 里调用 `self._add`），至于 `_add` 具体怎么算，由子类决定。

如果你用过 Java 的 `interface` 或 Python 的 `abc.ABC`，这就是同一件事。如果没用过也别担心——下面会用源码一步步带你看。

## 3. 本讲源码地图

| 文件 | 在本讲的作用 |
| --- | --- |
| [`_polybase.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py) | **本讲主角**。定义抽象基类 `ABCPolyBase`，集中承载六大便捷类共用的算术、求值、拟合、打印等行为，并用 `abc` 声明 12 个虚函数 + 3 个虚属性。 |
| [`polynomial.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py) | 幂级数模块。其中的 `Polynomial(ABCPolyBase)` 子类用 `_add = staticmethod(polyadd)` 等 12 行赋值，把虚函数绑定到本模块的函数式实现上，是「子类赋值委托」的标准范例。 |
| [`polyutils.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py) | 通用工具层（`_add`/`_fit`/`as_series`/`trimseq` 等）。本讲只需知道它是委托链的最底层，具体函数留到第 3 单元精读。 |

记忆要点：`_polybase.py` 定契约、`polynomial.py` 做绑定、`polyutils.py` 干底层脏活。

## 4. 核心概念与源码讲解

### 4.1 abc 抽象方法契约：mixin + abc.ABC

#### 4.1.1 概念说明

`ABCPolyBase` 这个名字拆开看就是「Abstract Base Class for Poly（nomial）」。它的定位是**mixin（混入类）**：自己不对应任何一种具体的多项式基，只负责把六大便捷类**共有的**行为（算术、求值、打印、domain/window 映射、fit、roots……）一次性写好，让子类「混入」即可复用。

模块文档字符串直接点明了这一点：

> The ABCPolyBase class provides the methods needed to implement the common API for the various polynomial classes. It operates as a mixin, but uses the abc module from the stdlib.

它继承自 `abc.ABC`（Python 标准库的抽象基类支持），因此天然拥有「抽象方法未实现就不能实例化」的能力。

关键设计意图是**「契约 + 共用骨架」**：
- **契约**：声明一组抽象方法，规定「想当一种多项式类，必须告诉我怎么加、怎么乘、怎么求值、怎么求根……」。
- **共用骨架**：把调用这些抽象方法的「外壳」全写好——`__add__`、`__call__`、`roots()`、`fit()` 等。子类只要填空，外壳自动复用。

#### 4.1.2 核心流程

可以用下面这张「契约—绑定—使用」的关系图来理解：

```
            ┌────────────── ABCPolyBase (abc.ABC) ──────────────┐
            │  契约层：12 个 @abstractmethod 静态方法            │
            │          (_add, _mul, _val, _fit, _roots, ...)    │
            │  虚属性：domain / window / basis_name              │
            │  骨架层：__add__/__call__/roots()/fit() 等外壳     │
            │           内部调用 self._add / cls._fit ...        │
            └───────────────────────┬───────────────────────────┘
                                    │ 继承
                ┌───────────────────┼───────────────────┐
        Polynomial              Chebyshev            Legendre ...
        _add = polyadd          _add = chebadd       _add = legadd
        _mul = polymul          _mul = chebmul       _mul = legmul
        _roots = polyroots      _roots = chebroots   _roots = legroots
        domain = [-1,1]         domain = [-1,1]      domain = [-1,1]
        basis_name = None       basis_name = 'T'     basis_name = 'Pn'
                └───────────────────┴───────────────────┘
```

- 父类定义**抽象方法签名**和**调用它们的外壳**。
- 每个子类**继承外壳**、**提供具体实现**（通过赋值）。
- 用户调用外壳 `p + q`，外壳在内部触发 `self._add(...)`，实际跑的是子类绑定的那个函数。这就是多态。

#### 4.1.3 源码精读

类定义本身就两行，但信息量很大：

[_polybase.py:L20-L21](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L20-L21) — 继承 `abc.ABC`，确立「我是抽象基类」的身份。

模块开头导入标准库 `abc`：

[_polybase.py:L9-L9](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L9) — `import abc`，提供 `@abstractmethod` 与 `ABC`。

除了 12 个虚函数，`ABCPolyBase` 还声明了 **3 个抽象属性**（abstract property），分别规定每种多项式必须自带默认区间和基名：

[_polybase.py:L114-L127](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L114-L127) — `domain`、`window`、`basis_name` 三个抽象属性。子类必须把它们赋成具体的类属性（如 `domain = np.array([-1., 1.])`）。

注意：`symbol` 看起来也是个 property，但它是**具体实现**（返回 `self._symbol`），不是抽象的——所有子类共用同一套 symbol 逻辑：

[_polybase.py:L110-L112](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L110-L112) — `symbol` 是具体 property，子类无需覆盖。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「抽象基类不能直接实例化」。

**操作步骤**：

1. 在 Python 中尝试直接创建 `ABCPolyBase` 实例：
   ```python
   from numpy.polynomial._polybase import ABCPolyBase
   ABCPolyBase([1, 2, 3])   # 期望：抛 TypeError
   ```
2. 写一个「只继承、什么虚函数都不实现」的空子类，再尝试实例化：
   ```python
   class Empty(ABCPolyBase):
       pass
   Empty([1, 2, 3])   # 期望：同样抛 TypeError，并列出未实现的抽象方法
   ```

**需要观察的现象**：两次调用都会抛 `TypeError: Can't instantiate abstract class ... without an implementation for abstract method '_add'`（或类似），错误信息会点出第一个未实现的抽象方法名。

**预期结果**：这就是 `abc.ABC` 的强制力——只要还有任何 `@abstractmethod` 没被子类覆盖，Python 就拒绝创建实例。`Polynomial` 之所以能用，正是因为它把 12 个虚函数和 3 个虚属性全填满了。

#### 4.1.5 小练习与答案

**练习 1**：`ABCPolyBase` 继承 `abc.ABC` 与「mixin」这两个身份矛盾吗？为什么？

> **答案**：不矛盾。`abc.ABC` 提供「强制实现抽象方法」的机制，mixin 指它的角色是「把共用代码混入子类、自己不独立使用」。二者结合正好实现「定契约 + 提供共用骨架」。

**练习 2**：下面这行代码合法吗？为什么？
> ```python
> p = ABCPolyBase([1, 2, 3])
> ```

> **答案**：不合法。`ABCPolyBase` 含有未实现的抽象方法，`abc` 会在 `__call__`（实例化）时抛 `TypeError`。

---

### 4.2 12 个虚函数清单（核心契约）

#### 4.2.1 概念说明

`ABCPolyBase` 用 12 个 `@staticmethod @abc.abstractmethod` 声明了「想当一种多项式基，必须会做的 12 件事」。它们全部以下划线开头（`_add`、`_mul`、`_val`……），表示**这是给框架内部用的接口**，不是给最终用户的公开 API。最终用户调用的是不带下划线的外壳（`+`、`*`、`p(x)`、`p.roots()` 等）。

这 12 个虚函数按用途可分为三组：

| 组 | 虚函数 | 做什么 | 用户对应的外壳 |
| --- | --- | --- | --- |
| **算术** | `_add`、`_sub`、`_mul`、`_div`、`_pow` | 系数级加/减/乘/带余除/幂 | `+ - * // % divmod **` |
| **微积分与求值** | `_val`、`_int`、`_der`、`_roots` | 求值/积分/求导/求根 | `p(x)`、`p.integ()`、`p.deriv()`、`p.roots()` |
| **构造与拟合** | `_fit`、`_line`、`_fromroots` | 最小二乘拟合/一次多项式/由根构造 | `Polynomial.fit`、`identity`、`fromroots` |

命名约定很规整：子类绑定时，把前缀 `_` 换成对应基的缩写即可。例如幂级数 `_add` 绑到 `polyadd`，Chebyshev 的 `_add` 绑到 `chebadd`，Legendre 的绑到 `legadd`——前缀随基变化、后缀（功能）保持一致。

#### 4.2.2 核心流程

每个虚函数都遵循统一的调用约定：**父类在「外壳方法」里调用 `self._xxx(...)`，把当前系数 `self.coef` 作为第一个实参传入**。以三个典型外壳为例：

```
用户写 p + q
   └─> __add__(self, other)              # _polybase.py 的外壳
          ├─ othercoef = self._get_coefficients(other)   # 校验同类、取出 q 的系数
          ├─ coef = self._add(self.coef, othercoef)      # 触发虚函数
          └─ return self.__class__(coef, ...)            # 用结果重建同类对象

用户写 p(2.5)
   └─> __call__(self, arg)
          ├─ arg = pu.mapdomain(arg, self.domain, self.window)  # 先做坐标映射
          └─ return self._val(arg, self.coef)                   # 触发虚函数求值

用户写 p.roots()
   └─> roots(self)
          ├─ roots = self._roots(self.coef)            # 触发虚函数求根（window 坐标下）
          └─ return pu.mapdomain(roots, self.window, self.domain)  # 映回 domain 坐标
```

注意一个重要细节：**父类不知道、也不需要知道系数是在哪种基下定义的**。它只负责「把系数数组塞进 `self._xxx`，拿回新系数数组」。至于 `_val` 用 Horner 还是递推、`_roots` 用友矩阵还是别的方法，全是子类（及其绑定的函数）的内部细节。这正是虚函数模式的价值：**父类管流程，子类管算法**。

#### 4.2.3 源码精读

12 个虚函数在源码里是连续一段，写法高度一致。这里列出全部 12 个，每行就是「虚函数 → 它的职责」：

[_polybase.py:L129-L187](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L129-L187) — 12 个 `@staticmethod @abc.abstractmethod`，逐个声明虚函数契约。

逐个解读（签名即文档）：

- [_add / _sub](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L129-L137) — `_add(c1, c2)`、`_sub(c1, c2)`：把两个系数数组装/减成新系数数组。
- [_mul](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L139-L142) — `_mul(c1, c2)`：系数级乘法（幂级数即卷积）。
- [_div](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L144-L147) — `_div(c1, c2)`：带余除法，返回 `(商, 余数)` 两个系数数组。
- [_pow](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L149-L152) — `_pow(c, pow, maxpower=None)`：幂运算。注意第三个参数 `maxpower`——父类 `__pow__` 会把 `self.maxpower` 传进来，用于「防止指数爆炸」。
- [_val](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L154-L157) — `_val(x, c)`：在点 `x` 处求值（参数顺序是 **x 在前、c 在后**，与多数函数不同）。
- [_int](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L159-L162) — `_int(c, m, k, lbnd, scl)`：积分 `m` 次，积分常数 `k`，下界 `lbnd`，变量缩放 `scl`。
- [_der](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L164-L167) — `_der(c, m, scl)`：求导 `m` 次，缩放 `scl`。
- [_fit](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L169-L172) — `_fit(x, y, deg, rcond, full)`：最小二乘拟合，返回系数（`full=True` 时附带诊断信息）。
- [_line](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L174-L177) — `_line(off, scl)`：返回一次多项式 `off + scl·x` 的系数（用于 `identity`）。
- [_roots](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L179-L182) — `_roots(c)`：求根（返回 **window 坐标系下**的根，外壳再映回 domain）。
- [_fromroots](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L184-L187) — `_fromroots(r)`：由给定根反构造首一多项式系数。

再看父类如何调用这些虚函数，三个最具代表性的外壳：

[_polybase.py:L510-L512](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L510-L512) — `__call__` 调用 `self._val(arg, self.coef)`。注意 `domain→window` 的坐标映射在外壳里先做掉，虚函数拿到的 `arg` 已经是 window 坐标。

[_polybase.py:L530-L536](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L530-L536) — `__add__` 调用 `self._add(self.coef, othercoef)`，外面包了 `try/except`，失败时返回 `NotImplemented` 让 Python 去试对方的反射运算符。

[_polybase.py:L589-L592](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L589-L592) — `__pow__` 把 `self.maxpower` 透传给 `self._pow(...)`，这是防指数爆炸的关键一环。

#### 4.2.4 代码实践（本讲主任务）

这是本讲规格要求的核心实践。我们用 Python 自省把契约和绑定**打印出来对照**，再用一个「残缺子类」验证 `abc` 的强制力。

**实践目标**：
1. 列出 `ABCPolyBase` 中全部 `@abstractmethod` 静态方法。
2. 对照 `Polynomial` 类体，写出每个虚函数绑定的具体函数名。
3. 解释「若子类忘记实现 `_roots` 会发生什么」并用实验验证。

**操作步骤**：

```python
import inspect
from numpy.polynomial._polybase import ABCPolyBase
from numpy.polynomial.polynomial import Polynomial

# 步骤 1：用 __abstractmethods__ 列出所有未实现契约（包括属性）
print(sorted(ABCPolyBase.__abstractmethods__))
# 期望看到 12 个 _xxx 虚函数 + domain/window/basis_name

# 步骤 2：对照 Polynomial，逐个打印绑定
for name in ['_add','_sub','_mul','_div','_pow','_val',
             '_int','_der','_fit','_line','_roots','_fromroots']:
    fn = getattr(Polynomial, name)
    print(f"{name:12s} -> {fn.__name__}")
# 期望：_add -> polyadd, _mul -> polymul, _roots -> polyroots, ...

# 步骤 3：验证“忘记实现 _roots 会怎样”
class Broken(ABCPolyBase):
    # 故意只实现 _add，其余全部缺席
    from numpy.polynomial.polynomial import polyadd as _add
    _add = staticmethod(_add)
    domain = __import__('numpy').array([-1., 1.])
    window = __import__('numpy').array([-1., 1.])
    basis_name = None

Broken([1, 2, 3])   # 期望：TypeError，提示还缺 _sub/_mul/.../_roots 等
```

**需要观察的现象**：
- 步骤 1 会输出一个集合，里面**正好 15 个名字**：12 个 `_xxx` 虚函数 + `domain`、`window`、`basis_name`。
- 步骤 2 的输出可直接和 4.2.1 的表格对照，11 行（除 `_add` 外）应一一对应到 `polysub`/`polymul`/`polydiv`/`polypow`/`polyval`/`polyint`/`polyder`/`polyfit`/`polyline`/`polyroots`/`polyfromroots`。
- 步骤 3 抛出 `TypeError: Can't instantiate abstract class 'Broken' without an implementation for abstract method '_sub'`（Python 会挑一个仍未实现的抽象方法点名）。

**预期结果 / 结论**：
- 「忘记实现 `_roots`」**不会**在 `class Broken:` 定义时报错，而是在**实例化** `Broken(...)` 时被 `abc` 拦截，抛 `TypeError`。这是因为 `abc` 把检查推迟到 `__call__`（即创建实例）那一刻。
- 这意味着：六大便捷类之所以「都能求根」，不是因为父类给了默认实现，而是因为它们各自把 `_roots` 绑到了 `polyroots`/`chebroots`/`legroots`/…。父类 `roots()` 外壳（[_polybase.py:L900-L913](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L900-L913)）只管调用 `self._roots(self.coef)` 再做坐标映射。

> 说明：步骤 3 的运行结果依赖你的 Python 版本对 `__abstractmethods__` 的展示顺序；但「抛 TypeError 且点名某个未实现抽象方法」这一行为在 Python 3 一致。若环境不便执行，可标记「待本地验证」后直接阅读源码得出结论。

#### 4.2.5 小练习与答案

**练习 1**：`_fit` 的签名里有 `rcond`、`full` 两个参数，但没有 `w`（权重）。但 `Polynomial.fit` 是支持权重 `w` 的。这矛盾吗？

> **答案**：不矛盾。`fit` 是 `ABCPolyBase` 的 classmethod 外壳（[_polybase.py:L945-L1034](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L945-L1034)），它接收 `w`，但在调用 `cls._fit(...)` 时以**关键字参数** `w=w` 透传（见 [_polybase.py:L1026](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L1026)）。抽象方法签名只写**位置参数契约**，`w` 通过关键字传递。

**练习 2**：为什么 `_val(x, c)` 把 `x` 放在 `c` 前面，而 `_add(c1, c2)` 把系数放前面？

> **答案**：这是历史约定。`_val` 是「在点 x 处求值」，语义上以 x 为主；算术运算则是「系数与系数」运算，以系数为主。语义决定参数顺序，不强制统一。

---

### 4.3 子类赋值委托：_add = staticmethod(polyadd)

#### 4.3.1 概念说明

抽象方法定下契约，但真正「干活」的是子类绑定的具体函数。NumPy 这里没用 `def _add(self, ...): return polyadd(...)` 这种重写方式，而是用更简洁的**直接赋值**：

```python
_add = staticmethod(polyadd)
```

这一行的含义是：**把模块级函数 `polyadd` 直接拿来当 `Polynomial._add` 的实现**。`staticmethod(...)` 是必须的——它告诉 Python「这是个静态方法，别按绑定方法处理」，否则 `self` 会被错误地当成第一个参数塞进去。

为什么能这么写？因为父类的 `@staticmethod @abc.abstractmethod _add` 已经把 `_add` 声明为**静态方法**契约。只要子类提供一个**可静态调用**、签名匹配的 `staticmethod`，`abc` 就认为契约已履行。

这种「绑定」写法有几个好处：
- **零样板**：不用写一堆一行 `def` 透传。
- **可见性强**：12 行赋值放在一起（4.3.3 会看到），一眼就能看清「这个类把哪些函数绑成了虚函数」。
- **解耦**：`polyadd` 本身既能被函数式 API 直接用（`P.polyadd(c1, c2)`），也能被类用，一份实现两份用途。

#### 4.3.2 核心流程

以 `p + q` 为例，完整的委托链横跨三个文件：

```
用户代码:        p + q                  (p, q 都是 Polynomial 实例)
   │
   ▼  _polybase.py
外壳:     __add__  ──>  self._add(self.coef, q.coef)
                              │  self._add 就是 Polynomial._add
   │                          ▼  polynomial.py
绑定:                 Polynomial._add  ==  polyadd(c1, c2)
                              │  polyadd 内部
   │                          ▼  polynomial.py L249
函数式:              polyadd  ──>  return pu._add(c1, c2)
                              │
   │                          ▼  polyutils.py
通用层:              pu._add  ──>  对齐长度后逐项相加，trim 尾零
                              │
   ▼
返回新系数数组 → __add__ 用它重建一个 Polynomial 对象
```

三层委托：**外壳（`_polybase.py`）→ 函数式 API（`polynomial.py`）→ 通用工具（`polyutils.py`）**。每一层都可独立使用、独立测试。本讲只关心第一层「外壳 → 绑定的函数」是怎么连起来的。

#### 4.3.3 源码精读

`Polynomial` 子类体里有一段被注释为 `# Virtual Functions` 的连续赋值，正是绑定所在：

[polynomial.py:L1615-L1616](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L1615-L1616) — `class Polynomial(ABCPolyBase)`，继承抽象基类。

[polynomial.py:L1641-L1653](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L1641-L1653) — 12 行赋值，把 12 个虚函数一一绑定到本模块的 `poly*` 函数。这就是「子类填空」的全部内容。

紧接着是被注释为 `# Virtual properties` 的三行，覆盖 3 个抽象属性：

[polynomial.py:L1655-L1658](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L1655-L1658) — `domain`/`window` 都设为 `polydomain`（即 `[-1., 1.]`），`basis_name = None`。

> `basis_name = None` 有特殊含义：它告诉打印系统「这是标准幂基，直接用 `symbol`（默认 `x`）当基名，而不是像 Chebyshev 那样打印成 `T₀(x)`」。详见第 2 单元第 4 讲（打印系统）。

再看被绑定的函数本身，它们大多只是「薄委托」到 `polyutils`。最典型的 `polyadd`：

[polynomial.py:L249-L249](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L249-L249) — `polyadd` 的函数体只有 `return pu._add(c1, c2)`，把真正干活的事交给通用层。

拟合函数 `polyfit` 也遵循同样模式，只是多传一个「用哪个 vander 函数构造矩阵」的信息：

[polynomial.py:L1499-L1499](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L1499-L1499) — `polyfit` 把 `polyvander`（构造范德蒙矩阵的函数）作为第一个参数传给 `pu._fit`，让通用层知道「这次拟合用幂基的 vander」。其他正交族会传 `chebvander`/`legvander`/…，复用同一套最小二乘骨架。

#### 4.3.4 代码实践

**实践目标**：验证「`Polynomial._add` 与模块级 `polyadd` 是同一个函数对象」，亲手追一遍委托链。

**操作步骤**：

```python
from numpy.polynomial import polynomial as P
from numpy.polynomial.polynomial import Polynomial

# 1. 它们是同一个函数对象吗？
print(Polynomial._add is P.polyadd)        # 期望 True
print(Polynomial._add.__name__)            # 期望 'polyadd'

# 2. 直接用绑定的虚函数，绕过外壳
c = Polynomial._add([1, 2, 3], [0, 0, 0, 4])
print(c)   # 期望 [1, 2, 3, 4]

# 3. 对比：用外壳 + 用函数式 API，结果应一致
p1 = Polynomial([1, 2, 3]); p2 = Polynomial([0, 0, 0, 4])
print((p1 + p2).coef)          # 期望 [1, 2, 3, 4]
print(P.polyadd([1,2,3], [0,0,0,4]))  # 期望 [1, 2, 3, 4]
```

**需要观察的现象**：三条路径（`Polynomial._add`、`p1 + p2`、`P.polyadd`）的系数结果完全一致，证明它们最终走的是同一个函数。

**预期结果**：`Polynomial._add is P.polyadd` 为 `True`，三处输出都是 `[1. 2. 3. 4.]`。这印证了 4.3.2 的委托链——外壳只是包装，真正计算的是被绑定的 `polyadd`。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `polynomial.py` 里的 `_add = staticmethod(polyadd)` 改成 `_add = polyadd`（去掉 `staticmethod`），调用 `p1 + p2` 会怎样？

> **答案**：会出错。没有 `staticmethod` 包装时，`_add` 会被当成绑定方法，`self._add(self.coef, othercoef)` 中的 `self.coef` 会被当成实例绑定的第一个参数，实际等价于 `polyadd(p1, othercoef)`（少了 `self.coef`），参数错位、结果错误甚至抛 `TypeError`。父类把 `_add` 声明为 `@staticmethod`，子类覆盖时也必须用 `staticmethod` 保持一致。

**练习 2**：`Chebyshev` 类体里会有 `_add = staticmethod(chebadd)` 吗？

> **答案**：会。六大正交族模块结构同构，`Chebyshev` 把 `_add` 绑到 `chebadd`、`_mul` 绑到 `chebmul`、`_roots` 绑到 `chebroots`……前缀换成对应基的缩写，后缀（功能）不变。这正是「前缀=基、后缀=功能」规律在类绑定层面的体现。

---

### 4.4 退出机制：`__array_ufunc__` 与 `__hash__`

#### 4.4.1 概念说明

除了虚函数契约，`ABCPolyBase` 还在类体顶部设了两个看似低调、实则重要的类属性，它们都是「主动退出某种默认行为」的开关：

- `__array_ufunc__ = None`：**退出 NumPy 的 ufunc 机制**。
- `__hash__ = None`：**声明本类不可哈希**。

为什么要「退出」？

**关于 `__array_ufunc__`**：NumPy 的 ufunc（通用函数，如 `np.add`、`np.multiply`）遇到 ndarray 时，会尝试对元素逐个运算。`Polynomial` 实例**不是数字、不是 ndarray**，但它内部装着 ndarray（`coef`）。如果不主动退出，`np.add(p, q)` 或 `p + np.array([...])` 时 NumPy 可能把多项式对象「拆开」按数组语义处理，得到毫无意义的结果甚至静默错误。设 `__array_ufunc__ = None` 是 NumPy 官方推荐的「我不要参与 ufunc」信号，会让 NumPy 直接放弃介入，把运算交还给 Python 运算符（从而走我们自己的 `__add__`）。

**关于 `__hash__`**：Python 规定，**如果类定义了 `__eq__` 且想让对象可哈希（能放进 `set`/当 dict 的 key），就必须自定义 `__hash__`**。`ABCPolyBase` 定义了 `__eq__`（比较系数、域、窗、symbol），但多项式内部含 ndarray，而 ndarray 默认不可哈希，所以把整个对象设成不可哈希是最安全的选择。设 `__hash__ = None` 后，`hash(p)` 或把 `p` 放进 `set` 会立即抛 `TypeError`，避免「以为能哈希、实际行为奇怪」。

#### 4.4.2 核心流程

```
np.add(p, q)                     hash(p) / {p}
      │                                │
      ▼                                ▼
NumPy 发现 p.__array_ufunc__ is None   Python 发现 p.__hash__ is None
      │                                │
      ▼                                ▼
NumPy 放弃，返回 NotImplemented        直接抛 TypeError: unhashable type
      │
      ▼
Python 回退到 p.__add__(q)
（走我们定义的多项式加法）
```

两个机制的共同点：**与其让默认行为给出错误或意外结果，不如显式声明「我不支持这个」，让错误尽早、清晰地暴露**。

#### 4.4.3 源码精读

这两个属性紧挨着写在类体最前面，注释也很清楚：

[_polybase.py:L70-L74](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L70-L74) — `__hash__ = None`（不可哈希）与 `__array_ufunc__ = None`（退出 ufunc）。

配套地，`__eq__` 定义了相等语义（这也是必须把 `__hash__` 显式处理的原因）：

[_polybase.py:L643-L650](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L643-L650) — `__eq__` 同时比较类型、domain、window、系数形状与值、symbol。因为重写了 `__eq__`，Python 默认会把 `__hash__` 设为 `None`，但这里仍然显式写出 `__hash__ = None` 以表意清晰。

#### 4.4.4 代码实践

**实践目标**：触发这两个「退出机制」，观察它们如何把潜在的错误转成清晰报错。

**操作步骤**：

```python
import numpy as np
from numpy.polynomial import Polynomial

p = Polynomial([1, 2, 3])

# 1. 退出 ufunc：np.add 会回退到 p.__add__
try:
    r = np.add(p, p)          # 期望：得到 2*p（回退到我们的 __add__）
    print(r)                  # 期望：2.0 + 4.0·x + 6.0·x²
except TypeError as e:
    print("TypeError:", e)

# 2. 不可哈希
try:
    hash(p)
except TypeError as e:
    print("TypeError:", e)    # 期望：unhashable type: 'Polynomial'

try:
    {p}                       # 把 p 放进 set
except TypeError as e:
    print("TypeError:", e)    # 期望：unhashable type: 'Polynomial'
```

**需要观察的现象**：
- `np.add(p, p)` 没有把 `p` 当数组拆开，而是回退成了多项式加法（结果系数翻倍）。
- `hash(p)` 和 `{p}` 都抛 `TypeError: unhashable type: 'Polynomial'`。

**预期结果**：与上述一致。这正说明两个 `None` 起到了「护栏」作用——既不破坏正常的 `+`/`*` 运算（它们走 Python 运算符协议），又拦住了 ufunc 误用和哈希误用。

#### 4.4.5 小练习与答案

**练习 1**：如果删掉 `__array_ufunc__ = None`，`np.add(p, np.array([1,2,3]))` 可能出什么问题？

> **答案**：NumPy 的 ufunc 机制可能尝试把 `Polynomial` 对象「当数组处理」，要么试图逐元素调用、要么返回形状/语义都不对的结果，甚至静默给出错误数据。设 `__array_ufunc__ = None` 是 NumPy 文档明确推荐的「拒绝参与 ufunc」方式，能保证运算要么走我们的运算符重载，要么明确报错。

**练习 2**：`__eq__` 定义后，为什么 `__hash__` 不能省略为「不写」？

> **答案**：Python 规定，一旦类定义了 `__eq__`，若未显式定义 `__hash__`，则 `__hash__` 被自动设为 `None`（对象变不可哈希）。所以「不写」和「写 `= None`」效果相同，但显式写出更能表达设计意图、提高可读性。本类含 ndarray（本身不可哈希），设成不可哈希是正确选择。

---

## 5. 综合实践

把本讲四个最小模块串起来，完成一个「自检式」小任务：**手工复刻一个最小的多项式类骨架，验证契约、绑定、退出三件事**。

**任务**：定义一个只实现部分虚函数的「极简幂级数类」，观察缺哪些虚函数会报错、补齐哪些后能用。

**操作步骤**：

```python
import numpy as np
from numpy.polynomial._polybase import ABCPolyBase
from numpy.polynomial import polynomial as P

class MiniPoly(ABCPolyBase):
    # 只绑定三个虚函数，其余故意留空
    _add = staticmethod(P.polyadd)
    _val = staticmethod(P.polyval)
    _roots = staticmethod(P.polyroots)
    # 虚属性
    domain = np.array([-1., 1.])
    window = np.array([-1., 1.])
    basis_name = None

# (1) 现在实例化，会报缺哪些虚函数？
#     期望：TypeError 列出 _sub/_mul/_div/_pow/_int/_der/_fit/_line/_fromroots 之一
```

**你需要回答的三个问题**：

1. **契约**：上面的 `MiniPoly` 还差几个虚函数？用 `MiniPoly.__abstractmethods__` 打印确认。期望集合里应只剩你没绑的那些（共 9 个）。
2. **绑定**：把 `_mul` 也绑成 `staticmethod(P.polymul)` 后，`MiniPoly([1,2]) * MiniPoly([1,2])` 能算出 `[1, 4, 4]`（即 \((1+2x)^2\)）吗？为什么外壳 `__mul__` 能直接复用？（提示：外壳在父类，[_polybase.py:L546-L552](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L546-L552)。）
3. **退出**：对 `MiniPoly` 实例调用 `np.add(m, m)` 和 `hash(m)`，是否和 `Polynomial` 表现一致？为什么？（提示：两个 `None` 定义在父类，子类自动继承。）

**预期结论**：
1. 缺 9 个，`abc` 拦截实例化。补齐全部 12 个 + 3 个属性后才能 `MiniPoly([1,2,3])`。
2. 能。`__mul__` 外壳在 `ABCPolyBase`，只要 `_mul` 被绑定，外壳就能工作——这就是「父类管流程、子类管算法」。
3. 一致。`__array_ufunc__ = None` 和 `__hash__ = None` 是类属性，子类天然继承。

> 这是一个「源码阅读 + 自省验证」型实践。若你不想自己补齐 12 个绑定，可直接对照 [polynomial.py:L1641-L1658](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L1641-L1658) 抄过来，体会「标准子类就是这么写的」。

## 6. 本讲小结

- `ABCPolyBase` 是一个 **mixin + `abc.ABC`** 抽象基类，集中承载六大便捷类的**共用外壳**（算术、求值、拟合、roots 等），自己不实例化。
- 它用 12 个 `@staticmethod @abc.abstractmethod` 声明**虚函数契约**（`_add/_sub/_mul/_div/_pow/_val/_int/_der/_fit/_line/_roots/_fromroots`），外加 3 个抽象属性 `domain/window/basis_name`。
- 父类只管「在合适的外壳里调用 `self._xxx(...)`，把系数数组传进去、把新系数数组拿回来」；具体算法由子类决定。这就是「**父类管流程，子类管算法**」。
- 子类用一行 `_add = staticmethod(polyadd)` **直接赋值**完成绑定（[polynomial.py:L1641-L1653](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L1641-L1653)），形成「外壳 → 函数式 API → `polyutils`」三层委托链。
- 若子类忘记实现某个虚函数（如 `_roots`），`abc` 会在**实例化**时抛 `TypeError` 并点名缺失的抽象方法——这是契约的强制力。
- `__array_ufunc__ = None` 退出 NumPy ufunc、`__hash__ = None` 声明不可哈希，两者都是「**把潜在错误转成清晰报错**」的护栏（[_polybase.py:L70-L74](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L70-L74)）。

## 7. 下一步学习建议

本讲只搞清了「契约与绑定」的机制，但还有一个谜团没解：外壳在做算术、求值、roots 时，频繁出现 `domain`/`window` 和坐标映射（`mapdomain`、`mapparms`）。**下一讲 u2-l2「域 domain、窗口 window 与线性映射」**专门讲清这套双区间机制——为什么求值要先映射坐标、为什么根要从 window 映回 domain、为什么 `fit` 要做自动 domain。

读完 u2-l2 后，建议结合本讲的 [4.2.2](#422-核心流程) 再看一遍 `__call__`/`roots` 的源码，你会发现自己同时理解了「虚函数怎么被调用」和「坐标怎么被映射」两条线索，对 `ABCPolyBase` 的设计就彻底通透了。

若你想提前感受「子类绑定」的多样性，可以快速浏览 [`chebyshev.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/chebyshev.py) 中 `class Chebyshev(ABCPolyBase)` 的虚函数赋值段，对照本讲 4.3，体会「前缀=基、后缀=功能」的规律——第 4 单元会正式精读它。
