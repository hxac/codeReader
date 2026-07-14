# Polynomial 便捷类快速上手

> 前两讲（[u1-l1](u1-l1-package-overview.md)、[u1-l2](u1-l2-directory-and-imports.md)）我们弄清了三件事：`numpy.polynomial` 是什么、系数如何表示（1-D 数组、下标即次数、从低到高）、以及目录里每个文件各管什么。其中 [u1-l2](u1-l2-directory-and-imports.md) 还点破了一个关键设计：六大便捷类的共同行为都集中在抽象基类 `ABCPolyBase`，每个便捷类只需把 `_add`、`_val`、`_fit` 等「虚函数」指向本模块的同名函数即可。但这些都是「认识」，还没「动手」。本讲我们以最常用的 `Polynomial` 类为对象，真正写代码：怎么创建一个多项式、怎么求值、怎么做加减乘除、怎么对带噪声数据做最小二乘拟合、怎么求导积分和求根。读完这篇，你就掌握了所有六大便捷类的通用用法——因为它们接口完全一致，只是「基」不同。

## 1. 本讲目标

读完本讲，你应当能够：

- 用 `Polynomial(coef)` 创建一个多项式，读懂它的打印串，并用 `p(x)` 在任意点求值。
- 对两个 `Polynomial` 实例做 `+ - * // % **` 与 `divmod()`，并解释为什么不同基的多项式不能直接相加。
- 用 `Polynomial.fit(x, y, deg)` 对带噪声数据做最小二乘拟合，并理解返回对象的 `domain` 是怎么来的。
- 用 `deriv()`、`integ()`、`roots()` 完成求导、积分、求根，并理解它们与 `domain/window` 线性映射的关系。

## 2. 前置知识

本讲承接 [u1-l1](u1-l1-package-overview.md) 与 [u1-l2](u1-l2-directory-and-imports.md)，假设你已经知道：

- **系数约定**：`Polynomial([1, 2, 3])` 表示 \(1 + 2x + 3x^2\)，系数从低次到高次排列。
- **便捷类与虚函数委托**：`Polynomial` 继承自 `ABCPolyBase`，通过 `_val = staticmethod(polyval)` 这类赋值，把「求值」交给模块里的 `polyval` 函数。六大便捷类接口一致，本讲学的用法可直接套用到 `Chebyshev`、`Legendre` 等。
- **domain / window**：每个便捷类有 `domain`（数据区间）和 `window`（基函数最稳定的计算区间）两个属性，二者之间通过线性映射 `off + scl·x` 换算。`Polynomial` 的默认 `domain` 和 `window` 都是 `[-1, 1]`。

本讲还会用到几个数学常识，先点一下：

- **Horner 法**：把 \(c_0 + c_1 x + c_2 x^2 + c_3 x^3\) 改写成 \(((c_3 x + c_2)x + c_1)x + c_0\)，把 \(n\) 次多项式的求值从 \(O(n^2)\) 次乘法降到 \(O(n)\)，且数值上更稳。
- **最小二乘拟合**：给定一组点 \((x_i, y_i)\)，找一个次数为 `deg` 的多项式 \(p\)，使残差平方和 \(\sum_i (y_i - p(x_i))^2\) 最小。标准做法是构造范德蒙矩阵（Vandermonde matrix）\(V\)，其中 \(V_{i,j} = x_i^j\)，再解超定方程 \(Vc = y\)。

## 3. 本讲源码地图

本讲围绕 `Polynomial` 类的典型用法展开，主要看这两个文件：

| 文件 | 作用 |
|------|------|
| [`polynomial.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py) | 「幂级数（标准幂基）」模块。文件下半部分定义便捷类 `Polynomial`（[polynomial.py:1615-1683](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L1615-L1683)），上半部分是 `polyval`、`polyfit`、`polydiv`、`polyroots` 等被委托的函数式实现。 |
| [`_polybase.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py) | 抽象基类 `ABCPolyBase`。`Polynomial` 的 `__call__`、`__add__`、`fit`、`deriv`、`roots` 等几乎所有「便捷」方法都实现在这里，再通过虚函数委托给 `polynomial.py` 的具体函数。 |

> 小贴士：本讲遇到一个方法，先问自己「它定义在哪」——如果是算术/求值/微积分/拟合，多半在 `_polybase.py`；如果是真正干活的系数运算，多半在 `polynomial.py`。这种「接口在上层、实现在下层」的分层，正是 [u1-l2](u1-l2-directory-and-imports.md) 讲过的依赖图。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**创建与求值**、**算术与幂运算**、**fit 拟合**、**deriv/integ/roots**。

### 4.1 创建与求值

#### 4.1.1 概念说明

「便捷类」的「便捷」体现在：你不再需要像函数式 API 那样把系数数组到处传，而是把系数「装」进一个对象，之后所有操作都以方法或运算符的形式挂在这个对象上。对一个 `Polynomial` 对象 `p`：

- `p.coef` 是系数数组（只读语义上，返回的是 ndarray）。
- `p.domain`、`p.window` 是两个区间端点数组。
- `p(x)` 把多项式当成函数来调用，返回 \(p(x)\) 的值。
- `print(p)` 给出人类可读的表达式。

#### 4.1.2 核心流程

创建与求值的流程可以用两步概括：

```text
Polynomial([1,2,3])
   └─ __init__: 用 pu.as_series([coef], trim=False) 把输入规整成 1-D ndarray
      存入 self.coef；domain/window/symbol 校验后存好
         ↓ 默认 domain == window == [-1, 1]

p(2)
   └─ __call__(2):
        1) arg = pu.mapdomain(2, domain, window)   # 默认下是恒等映射
        2) return self._val(arg, self.coef)        # = polyval(arg, [1,2,3])
                                                  # Horner 法 → 1 + 2*2 + 3*2**2 = 17
```

关键点：**求值前会先把自变量从 `domain` 映射到 `window`**。默认情况下 `domain == window == [-1, 1]`，映射是恒等的，所以看起来就像直接把 `x` 代入；但当 `domain != window`（例如 `fit` 的返回对象）时，这一步就至关重要，详见 4.3 节。

#### 4.1.3 源码精读

先看构造函数。它接受 `coef`，以及可选的 `domain`、`window`、`symbol`，用 `pu.as_series` 把系数规整成统一的 1-D 数组（注意 `trim=False`，**不去掉尾部零**），并校验 `domain`/`window` 必须恰好两个元素、`symbol` 必须是合法 Python 标识符：

[`_polybase.py:292-320`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L292-L320) —— `ABCPolyBase.__init__`：规整系数、校验 domain/window/symbol。

再看 `Polynomial` 类的类属性。`Polynomial` 把默认 `domain` 和 `window` 都设成 `polydomain`（即 `[-1, 1]`），`basis_name` 设为 `None`（因为标准幂基不需要像 Chebyshev 那样标注基名 \(T_n\)）：

[`polynomial.py:1656-1658`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L1656-L1658) —— `Polynomial` 的三个虚属性：`domain`、`window` 都指向 `polydomain`，`basis_name = None`。

求值的核心是 `__call__`，只有两行，但浓缩了整个 domain→window 映射思想：

[`_polybase.py:510-512`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L510-L512) —— `__call__`：先把 `arg` 从 `domain` 映射到 `window`，再调用 `_val`（对 `Polynomial` 就是 `polyval`）求值。

`_val` 实际指向 `polyval`，它用 Horner 法从最高次系数往低次「折叠」：

[`polynomial.py:756-759`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L756-L759) —— `polyval` 的 Horner 主循环：`c0 = c[-1] + x*0` 定初值与形状，再迭代 `c0 = c[-i] + c0*x`。

> 为什么 `c0 = c[-1] + x*0` 而不直接写 `c0 = c[-1]`？因为 `x*0` 的作用是让结果与 `x` 同形状、同 dtype——当 `x` 是数组时，结果也自动是数组；当 `c` 是多维系数时也能正确广播。

#### 4.1.4 代码实践

1. **实践目标**：亲手创建一个多项式并验证求值，确认「系数下标即次数」的约定。
2. **操作步骤**：

   ```python
   import numpy as np
   from numpy.polynomial import Polynomial

   p = Polynomial([1, 2, 3])   # 表示 1 + 2x + 3x^2
   print(p)                    # 打印表达式
   print(p.coef)               # 看系数数组
   print(p.domain, p.window)   # 看默认区间

   for x in (0, 1, 2):
       print(x, "->", p(x))    # 在 0,1,2 三点求值
   ```

3. **需要观察的现象**：`print(p)` 输出形如 `1.0 + 2.0·x + 3.0·x²`（unicode 风格，Linux/macOS 默认）或 `1.0 + 2.0 x + 3.0 x**2`（ascii 风格，Windows 默认）。
4. **预期结果**：`p(0)=1`、`p(1)=6`、`p(2)=17`，与手算 \(1+2x+3x^2\) 完全一致。
5. 数值结果是确定的，可在本地直接验证。

#### 4.1.5 小练习与答案

**练习 1**：`Polynomial([5])` 表示什么？对它调用 `p(100)` 结果是多少？

**答案**：表示常数多项式 \(5\)（即 \(5\cdot x^0\)）。`p(100)` 仍是 `5`，因为没有任何含 \(x\) 的项。

**练习 2**：为什么 `Polynomial([1, 2, 0, 0]).degree()` 返回 `3` 而不是 `1`？如何得到「真正的」次数 1？

**答案**：`degree()` 返回 `len(coef) - 1`，它**不检查系数是否为零**（见 [`_polybase.py:670-702`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L670-L702) 的文档说明）。构造函数用 `trim=False` 保留了尾部零。要得到真正次数，先调用 `p.trim()` 去掉尾部零：`Polynomial([1, 2, 0, 0]).trim().degree()` 得到 `1`。

---

### 4.2 算术与幂运算

#### 4.2.1 概念说明

`Polynomial` 重载了 Python 的全部数值运算符，使你可以像写普通数学表达式一样写多项式运算：

| 运算符 | 含义 | 返回 |
|--------|------|------|
| `p + q`、`p - q`、`p * q` | 加、减、乘 | 新的 `Polynomial` |
| `p // q`、`p % q`、`divmod(p, q)` | 整除（商）、取余、同时返回商和余数 | 新的 `Polynomial` |
| `p / q` | 真除——**仅当 `q` 是标量（Number）时** | 新的 `Polynomial`（= 标量整除） |
| `p ** n` | 幂 | 新的 `Polynomial` |
| `p + 3`、`2 * p` | 与标量运算 | 新的 `Polynomial` |

一个核心约束：**只有「同类」多项式才能直接相加/相乘**——即类型相同、`domain` 相同、`window` 相同、`symbol` 相同。`Polynomial` 加 `Chebyshev` 会被拒绝并抛出 `TypeError`。

#### 4.2.2 核心流程

以加法为例，运算符的执行流程是统一的「三段式」：

```text
p + q
  1) othercoef = self._get_coefficients(q)
       └─ 若 q 是同类 ABCPolyBase 实例 → 返回 q.coef
       └─ 若 q 是标量/数组 → 原样返回 q
       └─ 否则（类型/域/窗/符号不同）→ raise TypeError / ValueError
  2) coef = self._add(self.coef, othercoef)   # = polyadd(c1, c2)
  3) return self.__class__(coef, domain, window, symbol)
```

除法稍有不同：`__truediv__` 只允许右操作数是标量（[u1-l1](u1-l1-package-overview.md) 讲过，多项式除多项式得到无穷级数，没有自然截断点），随后转交给 `__floordiv__`。真正的「多项式除多项式」要用 `//`、`%` 或 `divmod()`，它们都走 `__divmod__`，返回**商和余数**两个多项式。

幂运算 `**` 会带上一个安全阀：`maxpower`（默认 `100`），防止 \(p^{1000}\) 这种失控的高次膨胀。

#### 4.2.3 源码精读

先看加法重载，它正是上面「三段式」的样板：

[`_polybase.py:530-536`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L530-L536) —— `__add__`：取对方系数、委托 `_add`、再用相同 domain/window/symbol 构造新实例。`try/except` 捕获异常时返回 `NotImplemented`，让 Python 去尝试对方的反向运算符。

兼容性判定全在 `_get_coefficients` 里，这是「同类才能运算」规则的源头：

[`_polybase.py:256-290`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L256-L290) —— `_get_coefficients`：若对方也是 `ABCPolyBase`，逐一检查类型、domain、window、symbol，任一不符即抛错；若对方不是多项式对象（如标量），原样返回。

除法走 `__divmod__`，一次算出商和余数，`//` 与 `%` 都复用它：

[`_polybase.py:577-587`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L577-L587) —— `__divmod__`：委托 `_div` 得到 `(quo, rem)` 系数，分别包装成新实例返回。

`_div` 指向 `polydiv`，它实现了多项式长除法（逐次消去最高次项）：

[`polynomial.py:407-424`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L407-L424) —— `polydiv` 的长除法主循环：从高次往低次，每次用 `c1[i:j] -= c2 * c1[j]` 消去当前最高次项，最后分离出商与余数。

幂运算把 `maxpower` 作为护栏传进去：

[`_polybase.py:589-592`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L589-L592) —— `__pow__`：委托 `_pow(self.coef, other, maxpower=self.maxpower)`。

`maxpower` 是 `ABCPolyBase` 的类属性，所有便捷类共享默认值 `100`：

[`_polybase.py:76-77`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L76-L77) —— 注释说明 `T_n^m` 的次数是 \(n\cdot m\)，因此需要一个上限防止失控。

#### 4.2.4 代码实践

1. **实践目标**：用运算符完成多项式的加减乘除与幂运算，并用 `divmod` 验证「商 × 除数 + 余数 = 被除数」。
2. **操作步骤**：

   ```python
   import numpy as np
   from numpy.polynomial import Polynomial

   p = Polynomial([1, 2, 3])      # 1 + 2x + 3x^2
   q = Polynomial([-1, 1])        # x - 1

   print("p + q =", (p + q).coef)
   print("p * q =", (p * q).coef)
   print("p ** 2 =", (p ** 2).coef)

   quo, rem = divmod(p, q)        # 多项式带余除法
   print("商 =", quo.coef, "余数 =", rem.coef)

   # 验证：商 * 除数 + 余数 应当等于 p
   reconstructed = quo * q + rem
   print("还原误差 =", (reconstructed - p).coef)

   # 与标量运算
   print("p + 10 =", (p + 10).coef)
   ```

3. **需要观察的现象**：`divmod` 返回的 `quo`、`rem` 都是 `Polynomial` 对象；`reconstructed` 与 `p` 的系数差应为 0（舍入误差量级）。
4. **预期结果**：`p * q` 系数为 `[-1, -1, 1, 3, 3]`（即 \((1+2x+3x^2)(x-1)\) 展开）；还原误差各项约为 `0`。可在本地直接验证。
5. **额外实验**：执行 `Polynomial([1, 2]) + Chebyshev([1, 2])`，观察抛出的 `TypeError: Polynomial types differ`，并对照 [`_polybase.py:280-282`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L280-L282) 理解错误来源（需先 `from numpy.polynomial import Chebyshev`）。

#### 4.2.5 小练习与答案

**练习 1**：`Polynomial([1, 2, 3]) / Polynomial([2])`（右操作数是「系数为 `[2]` 的多项式」，也是标量语义）和 `Polynomial([1, 2, 3]) / 2` 结果一样吗？

**答案**：不一样。`__truediv__` 用 `isinstance(other, numbers.Number)` 判断（见 [`_polybase.py:554-563`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L554-L563)）。`2` 是 `Number`，可以除；但 `Polynomial([2])` 是 `ABCPolyBase` 实例、**不是** `Number`，会抛 `TypeError`。要用多项式除多项式，请改用 `//`、`%` 或 `divmod()`。

**练习 2**：`Polynomial([1, 1]) ** 100` 能正常计算吗？`** 101` 呢？为什么要有这个上限？

**答案**：`** 100` 可以正常计算；`** 101` 会触发护栏报错。`maxpower` 默认是 `100`（见 [`_polybase.py:77`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L77)），`__pow__` 把它传给 `_pow`，超过上限即被拒绝。这样设计是因为 \(n\) 次多项式的 \(m\) 次幂次数是 \(n\cdot m\)，不加限制可能产生系数数组失控膨胀的多项式。> 注：精确的抛错位置与异常文案以本地实测为准（待本地验证具体异常文案）。

---

### 4.3 fit 拟合

#### 4.3.1 概念说明

`Polynomial.fit(x, y, deg)` 是便捷类最实用的方法之一：给定一组数据点 \((x_i, y_i)\) 和目标次数 `deg`，返回一个**最小二乘意义下**最贴合数据的 `Polynomial` 对象。它比函数式 `polyfit` 多做了两件「贴心」的事：

1. **自动推断 `domain`**：默认（`domain=None`）会用数据 `x` 的最小/最大值构造一个刚好覆盖数据的区间，作为返回对象的 `domain`。
2. **把数据映射到 `window` 再拟合**：拟合前先把 `x` 从 `domain` 线性映射到 `window`（默认 `[-1, 1]`），在「好条件数」的区间上求解，再把得到的系数连同原始 `domain`、`window` 一起封装成对象返回。

第二点是 `numpy.polynomial` 相对旧版 `numpy.polyfit` 的一大改进：在 `[-1, 1]` 上拟合通常数值更稳，而 `domain` 又记住了原始数据范围，调用 `p(x)` 时会自动把 `x` 映射回去，用户无需手动换算。

#### 4.3.2 核心流程

`fit` 的执行流程：

```text
Polynomial.fit(x, y, deg)
  1) 确定 domain：
       None  → domain = pu.getdomain(x)          # 用 x 的范围（退化时撑开 1）
       []    → domain = cls.domain               # 用类默认 [-1, 1]
  2) window 默认 = cls.window = [-1, 1]
  3) xnew = pu.mapdomain(x, domain, window)      # 把数据映射到 window
  4) res = cls._fit(xnew, y, deg, w, rcond, full)
            └─ polyfit → 构造 polyvander(xnew) → np.linalg.lstsq 求解
  5) return cls(coef, domain=domain, window=window, symbol=symbol)
```

**一个容易踩的坑**：返回对象的 `p.coef` 是「在 `window` 变量下的系数」。正确求值用 `p(x)`（会自动映射），**不要**直接用 `polyval(x, p.coef)`。`polyval` 的文档字符串里专门写了这条警告（见 [`polynomial.py:719-721`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L719-L721)）。如果确实想要「未缩放、未平移」的标准基系数，用 `p.convert().coef`。

#### 4.3.3 源码精读

`fit` 是定义在 `ABCPolyBase` 上的 `classmethod`，关键逻辑集中在末尾：

[`_polybase.py:1014-1034`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L1014-L1034) —— `fit` 的主逻辑：推断 domain、把 x 映射到 window、调用 `_fit`、再用原始 domain/window 封装返回。退化情况 `domain[0] == domain[1]`（所有 x 相同）时把区间撑开 1，避免映射除零。

`_fit` 指向 `polyfit`，后者构造范德蒙矩阵并交给 NumPy 的最小二乘求解器：

[`polynomial.py:1499`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L1499) —— `polyfit` 一行委托：`return pu._fit(polyvander, x, y, deg, rcond, full, w)`，真正的列归一化与 `lstsq` 在 `polyutils._fit` 里。

范德蒙矩阵的定义 \(V_{i,j} = x_i^j\) 由 `polyvander` 构造，它与求值的等价关系 \(\text{dot}(V, c) = \text{polyval}(x, c)\) 是最小二乘能work的基石：

[`polynomial.py:1183-1192`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L1183-L1192) —— `polyvander` 主循环：`v[0]=1`，`v[1]=x`，逐列 `v[i]=v[i-1]*x`，最后把「幂次」轴移到末尾。

> 列归一化（`_fit` 内部按列除以列范数）能显著改善范德蒙矩阵的条件数。关于条件数、`rcond` 截断奇异值等数值细节，本讲先建立直觉，深入分析留到 [u3-l5](u3-l5-vandermonde-leastsquares-fit.md) 与 [u5-l1](u5-l1-numerical-stability-tradeoffs.md)。

#### 4.3.4 代码实践

1. **实践目标**：对一组带噪声的二次数据做拟合，验证 `p(x)` 正确而 `polyval(x, p.coef)` 错误，体会「自动 domain」的作用。
2. **操作步骤**：

   ```python
   import numpy as np
   from numpy.polynomial import Polynomial
   from numpy.polynomial.polynomial import polyval

   rng = np.random.default_rng(0)
   x = np.linspace(10, 20, 40)            # 注意：数据在 [10, 20]，远离 [-1,1]
   y = 1 + 2*x + 3*x**2 + rng.normal(scale=50, size=x.size)

   p = Polynomial.fit(x, y, 2)
   print("domain =", p.domain)            # 应 ≈ [10, 20]（自动推断）
   print("window =", p.window)            # [-1, 1]

   # 正确求值：用 p(x)
   yhat_obj = p(x)
   # 错误求值：直接拿 p.coef 喂 polyval（忘记映射）
   yhat_raw = polyval(x, p.coef)

   print("p(x) 残差均方根     :", np.sqrt(np.mean((yhat_obj - y)**2)))
   print("polyval(x,p.coef) 误差:", np.sqrt(np.mean((yhat_raw - y)**2)))
   ```

3. **需要观察的现象**：`p.domain` 约为 `[10, 20]`；`p(x)` 的残差与噪声量级（约 50）相符，而 `polyval(x, p.coef)` 的「误差」会是天文数字（因为系数是 window 变量下的，直接代入原始 x 完全错位）。
4. **预期结果**：`p(x)` 残差约几十；`polyval(x, p.coef)` 误差远大于数据量级。可在本地直接验证。
5. **修复**：若一定要用 `polyval`，请改用 `polyval(x, p.convert().coef)`，或最简单地直接 `p(x)`。

#### 4.3.5 小练习与答案

**练习 1**：`Polynomial.fit(x, y, 3, domain=[])` 和 `Polynomial.fit(x, y, 3)`（`domain=None`）在 `x` 跨度很大时，哪个拟合系数的量级更小、更稳？为什么？

**答案**：`domain=[]` 强制使用类默认 `[-1, 1]`，但数据 `x` 可能远在 `[-1,1]` 之外，范德蒙矩阵的列（\(x^0, x^1, x^2, x^3\)）量级悬殊，条件数很差，系数会很大且不稳。`domain=None` 自动取数据范围并映射到 `window=[-1,1]`，拟合实际发生在 `[-1,1]` 上，条件数好得多，系数量级也更合理。这正是 `fit` 默认行为的设计动机。

**练习 2**：`fit` 返回对象的 `p.coef` 与 `p.convert().coef` 有什么区别？

**答案**：`p.coef` 是在 `window` 变量（默认 `[-1,1]` 上的线性变换后的自变量）下的系数；`p.convert()` 把对象转成 `domain == window` 的等价表示（见 [`_polybase.py:779-814`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L779-L814)），其 `.coef` 才是「未缩放、未平移」的标准基系数。

---

### 4.4 deriv / integ / roots

#### 4.4.1 概念说明

`Polynomial` 还提供三个常用的数学方法：

- `p.deriv(m=1)`：求 `m` 阶导数，返回新的 `Polynomial`。
- `p.integ(m=1, k=[], lbnd=None)`：求 `m` 次积分，`k` 是各次积分的常数项，`lbnd` 是定积分下界，返回新的 `Polynomial`。
- `p.roots()`：求多项式的全部根，返回 ndarray（**不是** `Polynomial`）。

三者都「继承」了 `domain/window` 线性映射的语义：因为 \(p(x)\) 实际是某个「window 多项式」\(P\) 在 \(u = \text{off} + \text{scl}\cdot x\) 下的复合，所以求导要乘 `scl`（链式法则），积分要乘 `1/scl`（换元 \(du = \text{scl}\,dx\)），而求出来的根在 window 坐标下，要映射回 domain 坐标。默认 `domain == window` 时 `scl=1`，这些缩放都退化为 1，看起来就是「普通的」微积分与求根。

#### 4.4.2 核心流程

```text
p.deriv(m)
   off, scl = p.mapparms()                       # 默认 scl=1
   coef = self._der(self.coef, m, scl)           # = polyder(coef, m, scl)
   return Polynomial(coef, domain, window, ...)  # domain/window 不变

p.integ(m, k, lbnd)
   off, scl = p.mapparms()
   lbnd' = off + scl*lbnd                         # 把下界映射到 window 坐标
   coef = self._int(self.coef, m, k, lbnd', 1./scl)   # = polyint(...)
   return Polynomial(coef, ...)

p.roots()
   roots = self._roots(self.coef)                 # = polyroots(coef)：友矩阵特征值
   return pu.mapdomain(roots, self.window, self.domain)   # 根从 window 映射回 domain
```

求根的底层是**友矩阵（companion matrix）法**：把多项式求根问题转化成一个矩阵的特征值问题，再用 `np.linalg.eigvals` 求解。

#### 4.4.3 源码精读

`deriv` 非常简洁，关键在于把 `scl` 传进去：

[`_polybase.py:878-898`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L878-L898) —— `deriv`：取 `mapparms` 的 `scl`，委托 `_der`（=`polyder`）。

`integ` 多了对积分下界 `lbnd` 的映射，以及把缩放因子取倒数 `1./scl`：

[`_polybase.py:845-876`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L845-L876) —— `integ`：把 `lbnd` 映射到 window 坐标，用 `1./scl` 作为积分缩放，委托 `_int`（=`polyint`）。

`roots` 求完特征值后，要把根从 window 映射回 domain（与 `__call__` 的映射方向相反）：

[`_polybase.py:900-913`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L900-L913) —— `roots`：`_roots` 求根，再 `mapdomain(roots, window, domain)` 映射回原始区间。

底层 `polyroots` 构造友矩阵并求其特征值：

[`polynomial.py:1601-1607`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L1601-L1607) —— `polyroots` 主干：`m = polycompanion(c)` 构造友矩阵，`r = np.linalg.eigvals(m)` 求特征值作为根，排序后尽可能化简为实数。

> `mapparms` 返回的线性映射 `off + scl·x` 由 `pu.mapparms(domain, window)` 计算，见 [`_polybase.py:816-843`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/_polybase.py#L816-L843)。其数学定义见 [`u2-l2`](u2-l2-domain-window-mapping.md)。

#### 4.4.4 代码实践

1. **实践目标**：验证「积分再求导回到原多项式」，并体会 `lbnd` 与 `k` 的关系。
2. **操作步骤**：

   ```python
   import numpy as np
   from numpy.polynomial import Polynomial

   p = Polynomial([1, 2, 3])           # 1 + 2x + 3x^2

   # 积分一次再求导一次，应当回到 p
   round_trip = p.integ().deriv()
   print("往返误差:", (round_trip - p).coef)   # 应约为 [0,0,0]

   # deriv: d/dx (1 + 2x + 3x^2) = 2 + 6x
   print("一阶导系数:", p.deriv().coef)         # [2., 6.]
   # integ: ∫(1 + 2x + 3x^2)dx = 0 + x + x^2 + x^3
   print("积分系数:  ", p.integ().coef)         # [0., 1., 1., 1.]

   # 求根: 1 + 2x + 3x^2 = 0 的两个根
   print("根:", p.roots())

   # lbnd 与 k 的关系：∫ 在下界 lbnd 处取值为 0 ⟺ 额外加了常数 k
   a = p.integ(lbnd=-2)
   b = p.integ(k=6)
   print("integ(lbnd=-2) 系数:", a.coef)
   print("integ(k=6)    系数:", b.coef)
   ```

3. **需要观察的现象**：`round_trip` 与 `p` 系数差约为 0；`integ(lbnd=-2)` 与 `integ(k=6)` 系数相同。
4. **预期结果**：`deriv` 给 `[2., 6.]`；`integ` 给 `[0., 1., 1., 1.]`；`integ(lbnd=-2)` 与 `integ(k=6)` 给出相同系数。可在本地直接验证。为什么 `lbnd=-2` 与 `k=6` 等价？因为 `polyint` 在每次积分后通过 `tmp[0] += k[i] - polyval(lbnd, tmp)` 把「在下界处的值」强制清零，这等价于加上一个合适的积分常数——详见 [u3-l4](u3-l4-derivative-integral.md)。
5. 求根结果的精度受友矩阵特征值算法影响，高次或重根时误差会增大（参见 [`polynomial.py:1572-1580`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L1572-L1580) 的 Notes）。

#### 4.4.5 小练习与答案

**练习 1**：对 `Polynomial([0, 0, 1])`（即 \(x^2\)）调用 `deriv(3)`，结果是什么？

**答案**：\(x^2\) 的三阶导数是 0。`deriv` 返回一个系数为 `[0.]` 的 `Polynomial`（`polyder` 在 `cnt >= n` 时返回 `c[:1] * 0`，见 [`polynomial.py:532-533`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L532-L533)）。

**练习 2**：`Polynomial.fromroots([1, 2, 3]).roots()` 能否精确还原 `[1, 2, 3]`？

**答案**：在低次（这里 3 次）且根分布良好时，结果非常接近 `[1, 2, 3]`，但通常**不是精确相等**——友矩阵特征值是数值算法，会有舍入误差，且 `roots()` 返回排序后的数组。多次或重根情况下误差会明显增大。

---

## 5. 综合实践

本任务把本讲的四个模块串起来：**拟合 → 求值 → 求导**，并画图对比。

> **实践目标**：用 `Polynomial.fit` 对 \(\sin(x)\) 在 \([0, 2\pi]\) 上的带噪声采样做 5 阶最小二乘拟合，画出「原始 sin、带噪声点、拟合曲线」三者的对比图，再对拟合结果求一阶导数，与真实的 \(\cos(x)\) 对比。

**操作步骤**：

```python
# 需要：numpy + matplotlib（绘图部分；若未装 matplotlib 可跳过画图，仅打印数值）
import numpy as np
from numpy.polynomial import Polynomial
import matplotlib.pyplot as plt

# 1) 造数据：sin 在 [0, 2π] 上的带噪声采样
rng = np.random.default_rng(42)
x = np.linspace(0, 2 * np.pi, 60)
y_clean = np.sin(x)
y = y_clean + rng.normal(scale=0.1, size=x.size)   # 加高斯噪声

# 2) 5 阶最小二乘拟合
p = Polynomial.fit(x, y, 5)
print("拟合对象 domain:", p.domain)    # 应 ≈ [0, 6.28]
print("拟合对象 window:", p.window)    # [-1, 1]
print("打印拟合多项式:\n", p)

# 3) 求值（用 p(x)，自动处理 domain→window 映射）
xf = np.linspace(0, 2 * np.pi, 300)
y_fit = p(xf)

# 4) 求一阶导数，与真实 cos 对比
dp = p.deriv()
print("\n导数对象:", dp)
y_deriv = dp(xf)

# 5) 画图
fig, ax = plt.subplots(1, 2, figsize=(11, 4))

ax[0].plot(x, y, 'o', ms=4, label='带噪声采样点')
ax[0].plot(xf, y_clean, '-', lw=2, label='真实 sin(x)')
ax[0].plot(xf, y_fit, '--', lw=2, label='5 阶 Polynomial.fit')
ax[0].set_title('拟合：sin(x) on [0, 2π]')
ax[0].legend()

ax[1].plot(xf, np.cos(xf), '-', lw=2, label='真实 cos(x)（sin 的导数）')
ax[1].plot(xf, y_deriv, '--', lw=2, label="拟合多项式的 deriv()")
ax[1].set_title('导数对比')
ax[1].legend()

plt.tight_layout()
plt.show()

# 6) 数值检查：拟合残差
print("\n拟合残差均方根:", np.sqrt(np.mean((p(x) - y) ** 2)))
```

**需要观察的现象**：

- `p.domain` 约为 `[0, 6.283...]`（自动推断为数据范围），`p.window` 为 `[-1, 1]`。
- 左图：拟合曲线（虚线）紧贴真实 sin（实线），并穿过带噪声点云。
- 右图：拟合多项式的导数（虚线）与真实 cos（实线）形状接近，但在端点附近因 5 阶截断会有偏差。
- 拟合残差均方根应与噪声标准差 `0.1` 同量级。

**预期结果**：残差均方根约为 `0.1` 量级；导数曲线在区间中部与 cos 吻合较好。> 图像外观与具体残差数值「待本地验证」（取决于随机种子与 matplotlib 环境）。

**延伸思考**：

- 把 `deg=5` 改成 `deg=15`，观察拟合曲线是否出现端点振荡（这是高次多项式拟合的典型现象，[u4-l2](u4-l2-chebyshev-interpolation-gauss.md) 会用 Chebyshev 采样点缓解）。
- 把 `Polynomial.fit` 换成 `Chebyshev.fit`（同样接口），观察高次时是否更稳——这正预告了第 4 单元正交多项式族的主题。

## 6. 本讲小结

- **创建即封装**：`Polynomial(coef)` 把系数、`domain`、`window`、`symbol` 打包成一个对象；默认 `domain == window == [-1, 1]`，`basis_name = None`。
- **求值会先映射**：`p(x)` 先用 `mapdomain` 把 `x` 从 `domain` 映到 `window`，再用 Horner 法（`polyval`）求值；默认下是恒等映射。
- **算术有「同类」约束**：`+ - * // % **` 通过 `_get_coefficients` 校验类型/域/窗/符号一致后，委托 `_add/_mul/_div/_pow` 完成；不同基相加会抛 `TypeError`。
- **fit 自动选 domain**：`Polynomial.fit` 默认用数据范围作 `domain`，映射到 `window` 后求解最小二乘，既稳又无需手动换算；求值务必用 `p(x)` 而非 `polyval(x, p.coef)`。
- **微积分与求根尊重映射**：`deriv` 乘 `scl`、`integ` 乘 `1/scl`、`roots` 把根从 window 映回 domain；默认下这些缩放都是 1。
- **六大类接口一致**：本讲所有用法对 `Chebyshev`、`Legendre`、`Laguerre`、`Hermite`、`HermiteE` 同样适用，区别只在「基」与默认 `domain`。

## 7. 下一步学习建议

- 想了解 `Polynomial` 背后那套 `polyval`、`polyfit`、`polydiv`、`polyroots` **函数式 API** 的命名规律与内置常数（`polyzero/polyone/polyx/polydomain`）？继续读 [u1-l4 函数式 API 与内置常数](u1-l4-functional-api-and-constants.md)。
- 想弄清 `domain/window` 线性映射的数学、`mapparms` 怎么算、`__call__` 与 `roots` 为何要反向映射？进入第 2 单元，先读 [u2-l2 域 domain、窗口 window 与线性映射](u2-l2-domain-window-mapping.md)。
- 想理解 `__add__`/`__pow__` 背后的虚函数委托机制、`maxpower` 护栏与 `_get_coefficients` 的兼容性判定？读 [u2-l1 ABCPolyBase 抽象基类与虚函数模式](u2-l1-abcpolybase-virtual-methods.md)。
- 想深入 Horner 求值、长除法 `polydiv`、友矩阵求根 `polyroots` 的逐行实现？进入第 3 单元的 [u3-l3 多项式求值与 Horner 法](u3-l3-evaluation-horner.md) 与 [u3-l6 伴随矩阵与多项式求根](u3-l6-companion-matrix-roots.md)。
