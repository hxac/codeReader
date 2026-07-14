# 算术运算符重载与类型校验

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `Polynomial` 等便捷类为什么能直接写 `p + q`、`p * 2`、`p ** 3`，以及这些运算符背后各自委托给了哪个虚函数。
- 读懂 `_get_coefficients` 的四道关卡（类型、`domain`、`window`、`symbol`），并能解释「为什么 `Polynomial + Chebyshev` 会直接抛 `TypeError`」。
- 区分 `/`、`//`、`%`、`divmod` 在多项式语境下的真实含义，知道为什么 `/` 只允许除以标量。
- 理解 `maxpower = 100` 这道安全阀在哪里被检查、检查的是「指数」还是「次数」，以及它如何防止系数数组失控膨胀。

本讲承接 u2-l1：你已经知道 `ABCPolyBase` 是 mixin + `abc.ABC`，子类用 `_add = staticmethod(polyadd)` 这样的「虚函数委托」提供基函数算法。本讲聚焦这些**虚函数是如何被 Python 运算符驱动起来的**——也就是「外壳方法」这一层。

## 2. 前置知识

阅读本讲前，你需要：

- 知道 `ABCPolyBase` 上有 12 个抽象静态方法（`_add`/`_sub`/`_mul`/`_div`/`_pow`/`_val`/…），子类通过赋值绑定具体实现（见 u2-l1）。
- 知道便捷类对象除 `coef` 外还携带 `domain`/`window`/`symbol`，且系数始终按 `window` 变量表达（见 u2-l2）。
- 了解 Python 的数据模型：`a + b` 会先尝试 `a.__add__(b)`，若返回 `NotImplemented` 再回退到 `b.__radd__(a)`。
- 知道「多项式长除法」会同时产生**商（quotient）**和**余数（remainder）**两个结果。

**一句话直觉**：`ABCPolyBase` 把 Python 的 `+ - * // % ** ()` 七个运算符**统一接管**，每个运算符的套路都一样——先把右操作数「翻译成系数数组」，再调用对应的虚函数算出新系数，最后用 `self.__class__(...)` 把新系数**重新封装**成一个同类型、同 `domain`/`window`/`symbol` 的对象返回。换句话说，**运算符不改变多项式的「身份」（类型与区间），只改变它的系数**。

## 3. 本讲源码地图

| 文件 | 本讲关注的内容 |
| --- | --- |
| [_polybase.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py) | 全部运算符重载（`__add__`/`__mul__`/`__divmod__`/`__pow__` 及反向版本）、`_get_coefficients` 兼容校验、`has_same*` 辅助方法、`maxpower` 类属性 |
| [polyutils.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py) | `_div`（长除法通用实现）、`_pow`（含 `maxpower` 检查点）两个底层工具 |
| [polynomial.py](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py) | `Polynomial` 类把 `_add`/`_div`/`_pow` 绑定到 `polyadd`/`polydiv`/`polypow` 的那几行委托 |

---

## 4. 核心概念与源码讲解

### 4.1 二元算术与反向算术

#### 4.1.1 概念说明

`ABCPolyBase` 的类文档开宗明义地承诺：它实现了 Python 的标准数值方法 `+`、`-`、`*`、`//`、`%`、`divmod`、`**` 和 `()`（函数调用）。这意味着你可以像写普通数学公式一样操作多项式对象：

```python
p = Polynomial([1, 2, 3])   # 1 + 2x + 3x^2
q = Polynomial([0, 1])      # x
print(p + q)                # 加法
print(p * 2)                # 与标量相乘
print(p(0.5))               # 函数调用求值
```

这里有两类运算符要分清：

- **正向（forward）运算符**：如 `__add__`、`__sub__`、`__mul__`，处理 `p <op> other`，即多项式在左边。
- **反向（reflected）运算符**：如 `__radd__`、`__rsub__`、`__rmul__`，处理 `other <op> p`，即多项式在右边。当左操作数（比如 `int`）不认识右边的多项式时，Python 才会回退到反向版本。

对加法和乘法，交换律成立，正向与反向差别不大；但**减法和除法不可交换**，所以 `__rsub__`/`__rdivmod__` 必须把操作数顺序颠倒过来。

#### 4.1.2 核心流程

正向算术 `__add__`/`__sub__`/`__mul__` 三者结构完全一致，可以抽象成一个模板：

```
def __add__(self, other):
    othercoef = self._get_coefficients(other)   # ① 把右操作数翻译成系数
    try:
        coef = self._add(self.coef, othercoef)  # ② 委托虚函数算新系数
    except Exception:
        return NotImplemented                    # ③ 算不动就交还 Python
    return self.__class__(coef, self.domain,    # ④ 重新封装成同类对象
                          self.window, self.symbol)
```

四步分别是：**翻译 → 委托 → 兜底 → 封装**。第 ④ 步刻意沿用 `self.__class__` 和原 `domain`/`window`/`symbol`，保证运算结果与操作数「同源」。

反向算术的差别只在第 ① 步——它**不调用** `_get_coefficients`（因为左操作数通常就是标量，没必要校验区间），直接把 `other` 喂给虚函数，并颠倒参数顺序：

```
__radd__:  self._add(other, self.coef)     # 等价于 other + p.coef
__rsub__:  self._sub(other, self.coef)     # 等价于 other - p.coef
__rmul__:  self._mul(other, self.coef)     # 等价于 other * p.coef
```

#### 4.1.3 源码精读

类文档承诺重载七个运算符的位置：

[_polybase.py:20-26](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L20-L26) —— `ABCPolyBase` 的类文档字符串明确列出 `'+', '-', '*', '//', '%', 'divmod', '**', '()'` 八种（`divmod` 与 `()` 是函数式写法）。

先看一元正负号，最简单：

[_polybase.py:522-528](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L522-L528) —— `__neg__` 对系数整体取反后重新封装；`__pos__` 直接返回 `self`（正号是幂等的，无需复制）：

```python
def __neg__(self):
    return self.__class__(
        -self.coef, self.domain, self.window, self.symbol
    )

def __pos__(self):
    return self
```

注意 `__pos__` 返回的是 `self` 本身而非副本——这与「便捷类不可变」的整体设计一致（所有变更操作都返回新对象，原对象永不改动），所以共享引用是安全的。

再看正向二元算术的三兄弟，结构如出一辙：

[_polybase.py:530-552](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L530-L552) —— `__add__`/`__sub__`/`__mul__` 都遵循「翻译→委托→兜底→封装」模板：

```python
def __add__(self, other):
    othercoef = self._get_coefficients(other)
    try:
        coef = self._add(self.coef, othercoef)
    except Exception:
        return NotImplemented
    return self.__class__(coef, self.domain, self.window, self.symbol)
```

第 534 行的 `except Exception: return NotImplemented` 是关键设计：它让「算不动」的情形（比如 `p + "一个字符串"`）优雅地交还 Python，由 Python 去尝试对方的反向运算符或最终抛 `TypeError`，而不是在多项式内部崩溃。

最后看反向版本：

[_polybase.py:594-613](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L594-L613) —— `__radd__`/`__rsub__`/`__rmul__` 直接把 `other` 放在虚函数的**第一个参数**位，颠倒顺序以处理不可交换的减法：

```python
def __rsub__(self, other):
    try:
        coef = self._sub(other, self.coef)   # other - self.coef
    except Exception:
        return NotImplemented
    return self.__class__(coef, self.domain, self.window, self.symbol)
```

之所以 `2 - p` 能算出正确结果，正是靠这一行颠倒——它等价于把标量 `2` 视作常数多项式 `[2]` 再做 `polysub([2], p.coef)`。

> 小贴士：反向版本为何不调用 `_get_coefficients`？因为反向版本的左操作数几乎总是标量（如 `2 * p` 里的 `2`），标量没有 `domain`/`window` 可言，直接交给 `polyadd`/`polymul` 经由 `as_series` 规整成 `[2]` 即可。

#### 4.1.4 代码实践

1. **实践目标**：验证正向与反向算术的对称性，并亲手触发一次 `NotImplemented` 回退。
2. **操作步骤**：
   ```python
   import numpy as np
   from numpy.polynomial import Polynomial
   p = Polynomial([1, 2, 3])      # 1 + 2x + 3x^2

   # 正向与反向加法应相等（交换律）
   print((p + 2).coef)            # 正向
   print((2 + p).coef)            # 反向，触发 __radd__

   # 减法不可交换：2 - p 与 p - 2 应互为相反数
   print((2 - p).coef)
   print((p - 2).coef)
   ```
3. **需要观察的现象**：`p + 2` 与 `2 + p` 的 `coef` 完全相同；`(2 - p)` 与 `(p - 2)` 的系数互为相反数。
4. **预期结果**：`p + 2` 与 `2 + p` 都得到 `[3., 2., 3.]`；`2 - p` 得到 `[1., -2., -3.]`，`p - 2` 得到 `[-1., 2., 3.]`。
5. 若环境差异导致打印格式不同，以「系数数组数值」为准；本结果**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`__pos__` 为什么可以直接 `return self` 而不必返回副本？这对 `p = Polynomial(...)` 之后做 `q = +p; q = q + 1` 安全吗？

**参考答案**：因为便捷类是不可变设计——所有变更操作（`+`、`-`、`deriv` 等）都返回**新对象**，从不原地修改 `self.coef`。既然没有方法能改动 `self`，共享同一引用就不会产生意外的别名 bug，所以 `+p` 返回 `self` 既正确又省一次拷贝。

**练习 2**：`__add__` 里为什么要把 `self._add(...)` 包在 `try/except` 里返回 `NotImplemented`，而 `_get_coefficients(other)` 却放在 `try` 之外？

**参考答案**：`_get_coefficients` 抛出的是**明确的兼容性错误**（类型不同、域不同等），这些应当直接传达给用户；而 `_add` 可能因为「右操作数是个不认识的奇怪对象」而失败，这种情况应该让 Python 有机会尝试对方的反向运算符，所以用 `NotImplemented` 优雅回退。两类错误的处理意图不同，因此分开放置。

---

### 4.2 _get_coefficients 兼容校验

#### 4.2.1 概念说明

正向算术的第 ① 步「翻译」全部由 `_get_coefficients` 完成。它要回答一个问题：**右操作数 `other` 到底该怎么参与运算？**

答案分两种：

- 若 `other` 是**另一个多项式对象**（`ABCPolyBase` 实例），则只有当它与 `self` 「身份完全一致」时，才能直接取它的 `coef` 相加——否则抛错。因为不同基（幂基 vs Chebyshev 基）、不同 `domain`/`window` 的多项式，系数根本不在同一个「坐标系」里，直接相加毫无意义。
- 若 `other` **不是**多项式对象（比如标量 `2`、列表 `[1,2]`），则原样返回，交给底层 `polyadd` 经由 `as_series` 去规整。

这就是为什么 `p + 2` 合法（标量视为常数项），而 `Polynomial + Chebyshev` 非法（两种基的系数不可混加）。

#### 4.2.2 核心流程

`_get_coefficients` 的判定流程是一串短路检查：

```
若 other 不是 ABCPolyBase 实例:
    返回 other 本身（交给底层规整）
否则（other 是多项式对象）依次检查:
    ① 类型相同？    否 → TypeError("Polynomial types differ")
    ② domain 相同？ 否 → TypeError("Domains differ")
    ③ window 相同？ 否 → TypeError("Windows differ")
    ④ symbol 相同？ 否 → ValueError("Polynomial symbols differ")
    全部通过 → 返回 other.coef
```

注意第 ④ 步抛的是 `ValueError` 而非 `TypeError`——这是一个历史遗留的不一致，读源码时要留意。

#### 4.2.3 源码精读

`_get_coefficients` 的完整实现：

[_polybase.py:256-290](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L256-L290) —— 四道关卡依次检查类型、`domain`、`window`、`symbol`：

```python
def _get_coefficients(self, other):
    if isinstance(other, ABCPolyBase):
        if not isinstance(other, self.__class__):
            raise TypeError("Polynomial types differ")
        elif not np.all(self.domain == other.domain):
            raise TypeError("Domains differ")
        elif not np.all(self.window == other.window):
            raise TypeError("Windows differ")
        elif self.symbol != other.symbol:
            raise ValueError("Polynomial symbols differ")
        return other.coef
    return other
```

配套的四个公开辅助方法 `has_samecoef`/`has_samedomain`/`has_samewindow`/`has_sametype`：

[_polybase.py:189-254](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L189-L254) —— 它们是给用户做「查询」用的便捷方法（返回布尔值），但 `_get_coefficients` 并没有调用它们，而是**内联**了同样的判定——目的是能给出不同的、精准的错误消息。

相等的完整定义则在 `__eq__` 里：

[_polybase.py:643-650](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L643-L650) —— `__eq__` 比 `_get_coefficients` 更严格：除了类型、`domain`、`window`、`symbol`，还要求 `coef` 的**形状**与**数值**完全一致：

```python
def __eq__(self, other):
    res = (isinstance(other, self.__class__) and
           np.all(self.domain == other.domain) and
           np.all(self.window == other.window) and
           (self.coef.shape == other.coef.shape) and
           np.all(self.coef == other.coef) and
           (self.symbol == other.symbol))
    return res
```

> 设计要点：能相加不等于相等。`p1 = Polynomial([1,2,3])` 与 `p2 = Polynomial([1,2,3,0])` 数学上代表同一个多项式（末尾的 `0` 是零次项以上的零），它们可以相加（`_get_coefficients` 只管身份），但 `p1 == p2` 为 `False`（形状不同）。这是「运算兼容」与「值相等」两个层级的区别。

#### 4.2.4 代码实践

1. **实践目标**：逐一触发四道关卡，对照源码确认每条错误消息来自哪一行。
2. **操作步骤**：
   ```python
   import numpy as np
   from numpy.polynomial import Polynomial, Chebyshev

   p = Polynomial([1, 2, 3])
   c = Chebyshev([1, 2, 3])                  # 同系数、同区间，但基不同
   p + c                                      # 预期: TypeError("Polynomial types differ")

   p2 = Polynomial([1, 2, 3], domain=[0, 10])
   p + p2                                     # 预期: TypeError("Domains differ")

   ps = Polynomial([1, 2, 3], symbol='t')
   p + ps                                     # 预期: ValueError("Polynomial symbols differ")
   ```
3. **需要观察的现象**：三段代码分别抛出 `TypeError`、`TypeError`、`ValueError`，且消息文本与源码第 282、284、288 行一一对应。
4. **预期结果**：如上所述。注意 `symbol` 不一致抛的是 `ValueError`，与其余三处不同。
5. 本结果**待本地验证**（取决于 numpy 版本，但消息文本在当前 HEAD 固定）。

#### 4.2.5 小练习与答案

**练习 1**：`Polynomial([1,2,3]) + Polynomial([1,2,3,0])` 能否成功？结果是什么？这两个对象 `==` 判定如何？

**参考答案**：能成功。`_get_coefficients` 只校验类型/域/窗/符号，不校验系数长度，所以末尾多一个 `0` 仍可相加；底层 `polyadd` 经 `as_series` 对齐长度后得到 `[2., 4., 6., 0.]`（或经 `trimseq` 裁成 `[2., 4., 6.]`）。但 `Polynomial([1,2,3]) == Polynomial([1,2,3,0])` 为 `False`，因为 `__eq__` 额外要求 `coef.shape` 相同。

**练习 2**：为什么 `p + 2` 不需要校验 `domain`/`window`？

**参考答案**：因为 `2` 不是 `ABCPolyBase` 实例，`_get_coefficients` 在第一个 `if` 就走 `return other` 分支，根本不会进入四道关卡。标量被视为「与任何基都兼容的常数项」，由 `polyadd`→`as_series` 规整成 `[2]` 后参与运算。

---

### 4.3 divmod / floordiv / mod / truediv

#### 4.3.1 概念说明

除法一族有四个相关运算符，它们都建立在同一个底层操作上——**多项式带余除法**，即给定 `p`、`q`，求商 `quo` 和余数 `rem` 满足：

\[
p(x) = q(x)\cdot \text{quo}(x) + \text{rem}(x), \quad \deg(\text{rem}) < \deg(q)
\]

四个运算符只是从这一对 `(quo, rem)` 里取不同部分：

| 运算符 | 方法 | 返回 |
| --- | --- | --- |
| `divmod(p, q)` | `__divmod__` | 元组 `(quo, rem)` |
| `p // q` | `__floordiv__` | 仅商 `quo` |
| `p % q` | `__mod__` | 仅余数 `rem` |
| `p / q` | `__truediv__` | 仅商，且**只允许 `q` 是标量** |

`/`（真除法）最特殊：它**拒绝多项式右操作数**，只接受标量。源码注释解释了原因——多项式除多项式一般会得到无穷级数，没有自然的截断点，所以库选择不支持 `poly / poly`，只把 `/` 用作「系数整体缩放」。

#### 4.3.2 核心流程

除法族的调用关系是一棵「委托树」，根节点是 `__divmod__`：

```
__truediv__(other):   校验 other 是标量 → __floordiv__(other)
__floordiv__(other):  res = __divmod__(other);  返回 res[0]   (商)
__mod__(other):       res = __divmod__(other);  返回 res[1]   (余数)
__divmod__(other):    othercoef = _get_coefficients(other)
                      quo, rem = self._div(self.coef, othercoef)
                      把 quo、rem 各自封装成多项式对象返回
```

底层 `_div` 的算法是经典的**长除法（反复消元）**：从被除数的最高次项开始，每次用除数乘以一个适当的单项消去当前最高次，循环直到余数次 数低于除数。

#### 4.3.3 源码精读

`__truediv__` 的标量校验：

[_polybase.py:554-563](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L554-L563) —— 只有 `other` 是 `numbers.Number` 且**不是 `bool`** 时才放行，否则直接抛 `TypeError`，然后转交 `__floordiv__`：

```python
def __truediv__(self, other):
    if not isinstance(other, numbers.Number) or isinstance(other, bool):
        raise TypeError(
            f"unsupported types for true division: "
            f"'{type(self)}', '{type(other)}'"
        )
    return self.__floordiv__(other)
```

> 小贴士：单独排除 `bool` 是因为 Python 里 `True`/`False` 技术上是 `int` 的子类（`isinstance(True, numbers.Number)` 为 `True`）。`p / True` 几乎肯定是笔误，库选择直接报错而非悄悄把系数除以 1。

`__floordiv__` 与 `__mod__` 都是 `__divmod__` 的薄包装：

[_polybase.py:565-575](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L565-L575) —— 各取元组的第 `[0]` 或 `[1]` 个元素：

```python
def __floordiv__(self, other):
    res = self.__divmod__(other)
    if res is NotImplemented:
        return res
    return res[0]

def __mod__(self, other):
    res = self.__divmod__(other)
    if res is NotImplemented:
        return res
    return res[1]
```

真正的核心 `__divmod__`：

[_polybase.py:577-587](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L577-L587) —— 调用虚函数 `_div` 得到 `(quo, rem)` 两个系数数组，**分别**封装成多项式对象返回：

```python
def __divmod__(self, other):
    othercoef = self._get_coefficients(other)
    try:
        quo, rem = self._div(self.coef, othercoef)
    except ZeroDivisionError:
        raise
    except Exception:
        return NotImplemented
    quo = self.__class__(quo, self.domain, self.window, self.symbol)
    rem = self.__class__(rem, self.domain, self.window, self.symbol)
    return quo, rem
```

注意第 581 行单独 `except ZeroDivisionError: raise`——除以零多项式时，`ZeroDivisionError` 必须原样向上抛，不能被下面的 `except Exception` 吞成 `NotImplemented`。这是一个精心的异常分流。

底层长除法 `pu._div`（被 `polydiv` 复用）：

[polyutils.py:519-552](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L519-L552) —— `for` 循环从高次往低次逐项消元，每次算出一个商系数 `q` 并从余数里减去 `q` 乘以移位后的除数：

```python
for i in range(lc1 - lc2, - 1, -1):
    p = mul_f([0] * i + [1], c2)     # 把 c2 左移 i 位
    q = rem[-1] / p[-1]              # 当前最高次项的商
    rem = rem[:-1] - q * p[:-1]      # 从余数中消去最高次
    quo[i] = q
return quo, trimseq(rem)
```

这正是手算多项式长除法的机械步骤。

反向除法 `__rtruediv__` 直接返回 `NotImplemented`：

[_polybase.py:615-618](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L615-L618) —— 因为「标量除以多项式」一般得到无穷级数，没有定义，库选择不支持：

```python
def __rtruediv__(self, other):
    # An instance of ABCPolyBase is not considered a Number.
    return NotImplemented
```

#### 4.3.4 代码实践

1. **实践目标**：用 `divmod` 一次性拿到商与余数，并验证除法恒等式 \(p = q\cdot\text{quo} + \text{rem}\)。
2. **操作步骤**：
   ```python
   import numpy as np
   from numpy.polynomial import Polynomial
   p = Polynomial([1, 2, 3, 4])     # 1 + 2x + 3x^2 + 4x^3
   q = Polynomial([1, 1])           # 1 + x

   quo, rem = divmod(p, q)
   print("商:", quo.coef)
   print("余:", rem.coef)

   # 验证 p == q*quo + rem（在 domain 内求值比对）
   x = np.linspace(-1, 1, 5)
   print(np.allclose(p(x), (q * quo + rem)(x)))   # 应为 True

   print((p / 2).coef)              # 标量真除法 → 系数减半
   # p / q                          # 取消注释：预期 TypeError（不支持 poly/poly）
   ```
3. **需要观察的现象**：`divmod` 返回两个多项式对象；恒等式重建后 `allclose` 为 `True`；`p / 2` 把每个系数除以 2；`p / q` 抛 `TypeError`。
4. **预期结果**：商约为 `[2., 1., 3.]`、余约为 `[-1., 1.]`（具体以手算长除法为准）；`p / 2` 得到 `[0.5, 1., 1.5, 2.]`。
5. 本结果**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `__divmod__` 要单独写一行 `except ZeroDivisionError: raise`，而不是让它和别的异常一起被 `except Exception` 捕获？

**参考答案**：因为除以零多项式（如 `divmod(p, Polynomial([0]))`）是**真实的错误**，必须以 `ZeroDivisionError` 明确告知用户。若被 `except Exception` 捕获转成 `NotImplemented`，Python 会尝试右操作数的反向除法，最终可能给出一个含糊的 `TypeError`，掩盖了「除以零」这个真正原因。

**练习 2**：`p / True` 会发生什么？为什么？

**参考答案**：抛 `TypeError("unsupported types for true division...")`。因为 `__truediv__` 显式排除了 `bool`——尽管 `True` 在 Python 里等于 `1`，`p / True` 几乎肯定是写错，库宁愿报错也不悄悄做一次无意义的「除以 1」。

---

### 4.4 __pow__ 与 maxpower

#### 4.4.1 概念说明

幂运算 `p ** n` 把多项式自乘 `n` 次。这里潜伏着一个工程问题：**多项式的次数会爆炸**。一个 `d` 次多项式自乘 `n` 次后，次数变成：

\[
\deg(p^n) = n \cdot \deg(p)
\]

于是 `p ** 1000`（哪怕 `p` 只是 `1 + x`）也会产生一个 1000 次的多项式，系数数组长达 1001。若用户不小心写了 `p ** 10**6`，内存会被瞬间撑爆。

为此 `ABCPolyBase` 设了一道安全阀：类属性 `maxpower`，限制允许的最大指数。默认 `maxpower = 100`——超过这个指数就直接拒绝运算。

> 重要区分：`maxpower` 限制的是**指数 `n`**，不是结果多项式的**次数**。它检查的是 `power > maxpower`，其中 `power` 就是 `__pow__` 的右操作数（指数）。

#### 4.4.2 核心流程

幂运算的链路比算术多一步「传递 `maxpower`」：

```
__pow__(self, other):
    coef = self._pow(self.coef, other, maxpower=self.maxpower)
              ↑ 把 self.maxpower(=100) 作为参数传下去
    返回 self.__class__(coef, ...)

_pow(mul_f, c, pow, maxpower):     # polyutils 里的通用实现
    power = int(pow)
    校验 power 是非负整数            否则 → ValueError
    若 maxpower 非空 且 power > maxpower:
        raise ValueError("Power is too large")   ← 检查点
    若 power == 0: 返回 [1]         (任何多项式的 0 次幂是常数 1)
    若 power == 1: 返回 c           (1 次幂就是自身)
    否则: 循环自乘 power-1 次
```

#### 4.4.3 源码精读

类属性 `maxpower` 的定义与注释：

[_polybase.py:76-77](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L76-L77) —— 注释点明设计动机 `T_n^m has degree n*m`：

```python
# Limit runaway size. T_n^m has degree n*m
maxpower = 100
```

`__pow__` 把它传给虚函数 `_pow`：

[_polybase.py:589-592](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L589-L592) —— 注意这里**没有** `try/except`，`ValueError` 会直接向上传播：

```python
def __pow__(self, other):
    coef = self._pow(self.coef, other, maxpower=self.maxpower)
    res = self.__class__(coef, self.domain, self.window, self.symbol)
    return res
```

真正的检查点在 `polyutils._pow`：

[polyutils.py:685-700](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L685-L700) —— 先校验指数是非负整数，再比对 `maxpower`，最后循环自乘：

```python
power = int(pow)
if power != pow or power < 0:
    raise ValueError("Power must be a non-negative integer.")
elif maxpower is not None and power > maxpower:
    raise ValueError("Power is too large")      # ← maxpower 检查点
elif power == 0:
    return np.array([1], dtype=c.dtype)
elif power == 1:
    return c
else:
    prd = c
    for i in range(2, power + 1):
        prd = mul_f(prd, c)                      # 反复卷积
    return prd
```

`Polynomial` 类把 `_pow` 绑定到 `polypow`：

[polynomial.py:1642-1646](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L1642-L1646) —— `polypow` 再委托给 `pu._pow`，把乘法函数换成 `np.convolve`：

```python
_add = staticmethod(polyadd)
_sub = staticmethod(polysub)
_mul = staticmethod(polymul)
_div = staticmethod(polydiv)
_pow = staticmethod(polypow)
```

[polynomial.py:427-463](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L427-L463) —— `polypow` 是个薄包装，注意它**自身**的 `maxpower` 默认是 `None`（不限制），但被 `__pow__` 调用时一定带着 `self.maxpower=100`：

```python
def polypow(c, pow, maxpower=None):
    ...
    return pu._pow(np.convolve, c, pow, maxpower)
```

> 一个微妙之处：直接调用函数式 `P.polypow(c, 1000)` 不会触发限制（因为 `maxpower` 默认 `None`），但写成 `Polynomial(c) ** 1000` 走运算符路径就一定会被拦。安全阀只对面向对象的 `**` 运算符生效。

#### 4.4.4 代码实践（本讲核心实验）

1. **实践目标**：亲手触发 `maxpower`，并对照源码定位「检查发生在哪一行」。
2. **操作步骤**：
   ```python
   import numpy as np
   from numpy.polynomial import Polynomial
   p = Polynomial([0, 1])          # x

   print((p ** 100).degree())      # 刚好等于上限：100 次，合法
   p ** 101                        # 超过 maxpower，预期 ValueError
   ```
3. **需要观察的现象**：`p ** 100` 成功，得到一个 100 次多项式；`p ** 101` 抛 `ValueError("Power is too large")`。
4. **预期结果**：如上。随后阅读 [polyutils.py:688-689](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L688-L689) 确认异常正是从 `elif maxpower is not None and power > maxpower: raise ValueError("Power is too large")` 这一行抛出——这就是检查点。
5. **进阶观察**：试试 `from numpy.polynomial import polynomial as P; P.polypow([0,1], 1000)`，它**不会**报错——因为函数式 `polypow` 的 `maxpower` 默认 `None`，印证了「安全阀只保护运算符路径」这一结论。本结果**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：`Polynomial([1, 2]) ** 0` 和 `Polynomial([1, 2]) ** 1` 分别返回什么？为什么源码要为这两种情况单独写分支？

**参考答案**：`** 0` 返回常数多项式 `[1]`（任何非零多项式的 0 次幂为 1），`** 1` 返回自身的副本。单独写分支是为了避免进入 `for` 循环做无用功——`power == 0` 时循环根本不会执行却要初始化 `prd`，`power == 1` 时循环也只走零次，特判既清晰又省时。

**练习 2**：如果把 `maxpower` 检查从 `_pow` 移到 `__pow__` 里（即直接在 `_polybase.py` 判断 `other > self.maxpower`），会有什么问题？

**参考答案**：那样会破坏「父类管流程、子类管算法」的分层——`maxpower` 的语义会和 `_pow` 的实现细节（如 `power == 0/1` 的特判、`int(pow)` 转换）耦合在一起。当前设计让 `_pow` 完整负责「幂运算的一切」（含校验与计算），`__pow__` 只负责传递参数与封装结果，职责更清晰，也方便函数式 `polypow` 复用同一套校验逻辑。

---

## 5. 综合实践

把本讲的四块知识串起来，完成下面这个「写一个安全的幂运算包装器」的小任务。

**任务**：写一个函数 `safe_pow(p, n, limit=None)`，它对任意便捷类多项式 `p` 做幂运算，但要满足：

1. 若 `limit` 不为 `None` 且 `n > limit`，抛 `ValueError`（提示「超过自定义上限」），不调用 `p ** n`。
2. 否则返回 `p ** n`。
3. 用 `Polynomial([0, 1])` 测试：`safe_pow(p, 50, limit=40)` 应抛你的 `ValueError`；`safe_pow(p, 50, limit=None)` 应返回 50 次多项式（不触发 numpy 自带的 `maxpower=100`）。

**参考实现**：

```python
def safe_pow(p, n, limit=None):
    if limit is not None and n > limit:
        raise ValueError(f"指数 {n} 超过自定义上限 {limit}")
    return p ** n
```

**验证思路**：

- 你的 `limit` 检查发生在调用 `p ** n` **之前**，所以它和 numpy 的 `maxpower` 是**两层独立**的防护——你可以在 numpy 允许的 100 之内再设更严的限制。
- 阅读 [_polybase.py:589-592](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L589-L592) 与 [polyutils.py:688-689](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L688-L689) 后应能解释：numpy 的 `maxpower` 检查在 `_pow` 内部，你的 `limit` 检查在它之前，两者串联，先到先生效。

这个任务综合了「运算符如何委托虚函数」（4.1）、「幂运算的检查点位置」（4.4），并复习了「异常分流」的思想（4.3 里 `ZeroDivisionError` 的处理）。

---

## 6. 本讲小结

- `ABCPolyBase` 接管了 `+ - * // % ** ()` 七类运算符，套路统一：**翻译右操作数 → 委托虚函数 → 兜底返回 `NotImplemented` → 用 `self.__class__` 重新封装**。
- 正向算术 `__add__`/`__sub__`/`__mul__` 先走 `_get_coefficients` 校验，反向算术 `__radd__`/`__rsub__`/`__rmul__` 则直接颠倒参数顺序、不校验区间。
- `_get_coefficients` 设四道关卡——类型、`domain`、`window`、`symbol`——前三个不通过抛 `TypeError`，最后一个抛 `ValueError`；标量（非 `ABCPolyBase`）则原样放行。
- 除法族以 `__divmod__` 为根：`//` 取商、`%` 取余、`/` 只允许标量；`ZeroDivisionError` 被单独放行，不被 `NotImplemented` 吞掉。
- `__pow__` 把 `maxpower=100` 传给 `_pow`，检查点在 [polyutils.py:688-689](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L688-L689)，限制的是**指数**而非结果次数；且只对运算符 `**` 生效，函数式 `polypow` 默认不限制。

## 7. 下一步学习建议

- 下一讲 **u2-l4（字符串、格式化与 LaTeX 表示）** 会转向 `ABCPolyBase` 的另一套外壳方法 `__str__`/`__repr__`/`__format__`/`_repr_latex_`，与本讲的运算符重载同属「外壳层」，可对照阅读。
- 若想深入除法的数值细节，可继续阅读 [polynomial.py:369-424](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L369-L424) 的 `polydiv`，它是比 `pu._div` 更高效的特化长除法实现。
- 若对「为什么不同基不能直接相加」想从数学层面理解，可预习 u4-l4（不同基之间的相互转换），那里会讲 `convert` 如何通过「求值重表达」在基之间切换。
