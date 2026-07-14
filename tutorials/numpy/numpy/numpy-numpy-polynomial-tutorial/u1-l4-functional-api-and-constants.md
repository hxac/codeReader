# 函数式 API 与内置常数

> [u1-l3](u1-l3-polynomial-class-quickstart.md) 里我们一直用「便捷类」写代码：`p = Polynomial([1,2,3])`，然后 `p(x)`、`p + q`、`p.deriv()`。可你也许注意到了——那些方法的「真正干活」部分，其实都被委托给了 `polynomial.py` 里的一堆小写函数：`polyval`、`polyadd`、`polyder`…… 本讲我们就把镜头转向这些**函数式 API**：它们怎么命名、分几类、内置了哪些「常数系数数组」，以及它们和便捷类之间那条「委托链」到底是怎么连起来的。读完这篇你会明白：便捷类只是「糖衣」，函数式 API 才是 `polynomial.py` 的本体；而那条 `Polynomial.__add__ → polyadd → pu._add` 的三层调用链，正是 [u1-l2](u1-l2-directory-and-imports.md) 讲过的依赖图在代码里的具象化。

## 1. 本讲目标

读完本讲，你应当能够：

- 说出 `polynomial.py` 里 `poly*` 函数族的**命名前缀规律**，把任意一个函数（如 `polyval3d`、`polyvalfromroots`）归到「算术 / 微积分 / 求值 / 构造 / 拟合 / 求根」某一类，并据此猜出它的用途。
- 知道 `polydomain`、`polyzero`、`polyone`、`polyx` 四个**内置常数**分别代表什么多项式，并能用它们与 `polyline` 解释「常数也是一种系数数组」。
- 理解 `polytrim = pu.trimcoef` 这个**别名**背后的模式，并把模式推广到整条三层委托链：`Polynomial.__add__`（在 `_polybase.py`）→ `polyadd`（在 `polynomial.py`）→ `pu._add`（在 `polyutils.py`）。

## 2. 前置知识

本讲承接 [u1-l1](u1-l1-package-overview.md)、[u1-l2](u1-l2-directory-and-imports.md)、[u1-l3](u1-l3-polynomial-class-quickstart.md)，假设你已经知道：

- **系数约定**：1-D 数组 `c`，下标即次数，从低到高，即 \(c_0 + c_1 x + c_2 x^2 + \dots\)。本模块里所有 `poly*` 函数的输入输出都遵循这个约定。
- **分层依赖**：`polyutils.py`（底层工具）→ `polynomial.py`（幂基的函数式 API + `Polynomial` 类）→ `__init__.py`（门面）。本讲会反复在 `polynomial.py` 和 `polyutils.py` 之间跳转。
- **虚函数委托**：`Polynomial` 通过 `_add = staticmethod(polyadd)` 这类类体赋值，把算术运算交给本模块的同名函数（见 [u1-l3](u1-l3-polynomial-class-quickstart.md) 4.1 节）。

再补两个本讲要用的小概念：

- **首一多项式（monic polynomial）**：最高次项系数为 1 的多项式。由根构造多项式时，\((x-r_0)(x-r_1)\cdots(x-r_n)\) 自然就是首一的。
- **模块级别名**：Python 里 `a = b` 把 `b` 当前的值绑定到名字 `a`。`polynomial.py` 用它把 `polyutils` 里的某些函数「换个名字」暴露成本模块的公共 API——这是函数式 API 与底层工具之间最薄的一层「胶水」。

## 3. 本讲源码地图

本讲几乎全部围绕 `polynomial.py`，少量跳转到 `polyutils.py`：

| 文件 | 作用 |
|------|------|
| [`polynomial.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py) | 幂基模块。文件开头 [polynomial.py:76-107](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L76-L107) 定义了 `__all__` 与四个内置常数；随后是 `polyline`/`polyadd`/`polyval`/`polyder`/`polyfit`/`polyroots` 等函数式 API；末尾 [polynomial.py:1615-1683](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L1615-L1683) 是便捷类 `Polynomial`，用一串 `_xxx = staticmethod(polyxxx)` 把函数接成方法。 |
| [`polyutils.py`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py) | 全包共享工具。本讲会用到 `trimcoef`（[polyutils.py:144-192](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L144-L192)）和 `_add`/`_fit`/`_fromroots` 等带下划线前缀的「真·实现」函数，看清委托链的终点。 |

> 阅读策略：本讲每见到一个 `polyxxx`，先在 `polynomial.py` 里看它是不是「三行委托」（返回 `pu._xxx(...)`），如果是，再去 `polyutils.py` 看那行真活儿怎么干。这种「顺着委托往下钻一层」的习惯，是后续读 [u3](#) 系列讲义的基础。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**`poly*` 命名规律与 `__all__`**、**内置常数与 `polyline`**、**`polytrim` 别名与三层委托关系**。

### 4.1 `poly*` 命名规律与 `__all__`

#### 4.1.1 概念说明

打开 `polynomial.py`，你会看到几十个公开名字：`polyadd`、`polysub`、`polymul`、`polydiv`、`polyval`、`polyder`、`polyfit`、`polyroots`、`polyvander`……它们看起来杂乱，其实遵循两条极简单的规律：

1. **前缀规律**：本模块的公开函数和常数**全部**以 `poly` 开头（唯一的类是 `Polynomial`，首字母大写以示区别）。这不是巧合——六大正交多项式族各自有对应前缀：`cheb*`（切比雪夫）、`leg*`（勒让德）、`lag*`（拉盖尔）、`herm*`（物理学家埃尔米特）、`herme*`（概率论埃尔米特）。记住「前缀 = 基」，你就能在任意一个族模块里「照葫芦画瓢」地找到等价函数。

2. **后缀规律**：前缀之后的部分描述「做什么」。`add/sub/mul/div/pow` 是算术，`val` 是求值，`der/int` 是微积分，`fit` 是拟合，`roots` 是求根，`vander` 是构造范德蒙矩阵，`line` 是构造一次多项式，`fromroots` 是由根构造。还有表示维度的后缀：`2d/3d/nd` 与 `grid2d/grid3d`。

把这两条规律一组合，遇到 `polyval3d` 就能立刻读出「幂基·三维求值」，遇到 `chebgrid2d` 就能读出「切比雪夫基·二维网格求值」。

#### 4.1.2 核心流程

`polynomial.py` 的模块文档字符串把全部公开名字按用途分了类（[polynomial.py:19-69](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L19-L69)），对应下面这张「速查表」：

| 类别 | 函数 | 作用（系数视角） |
|------|------|------------------|
| Constants（常数） | `polydomain, polyzero, polyone, polyx` | 预定义的系数数组 |
| Arithmetic（算术） | `polyadd, polysub, polymulx, polymul, polydiv, polypow` | 系数数组的 + − × ÷ 幂 |
| Evaluation（求值） | `polyval, polyval2d/3d/nd, polygrid2d/3d, polyvalfromroots` | 给定 x，算 \(p(x)\) |
| Calculus（微积分） | `polyder, polyint` | 系数的逐项求导 / 积分 |
| Construction（构造） | `polyline, polyfromroots` | 由参数 / 根造系数 |
| Fitting（拟合） | `polyfit` | 最小二乘求系数 |
| Roots（求根） | `polyroots, polycompanion` | 由系数求根（经友矩阵） |
| Basis matrix（基矩阵） | `polyvander, polyvander2d/3d` | 范德蒙矩阵 |
| Trimming（修剪） | `polytrim` | 去掉尾部的「小」系数 |

这张表基本就是 `__all__` 的内容（[polynomial.py:76-82](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L76-L82)）。`__all__` 的作用是：当用户写 `from numpy.polynomial.polynomial import *` 时，只有列在 `__all__` 里的名字会被导入。它既是「公共 API 清单」，也是「文档目录」。

#### 4.1.3 源码精读

`__all__` 把所有公开名字一网打尽：

```python
__all__ = [
    'polyzero', 'polyone', 'polyx', 'polydomain', 'polyline', 'polyadd',
    'polysub', 'polymulx', 'polymul', 'polydiv', 'polypow', 'polyval',
    'polyvalfromroots', 'polyder', 'polyint', 'polyfromroots', 'polyvander',
    'polyfit', 'polytrim', 'polyroots', 'Polynomial', 'polyval2d', 'polyval3d',
    'polyvalnd', 'polygrid2d', 'polygrid3d', 'polyvander2d', 'polyvander3d',
    'polycompanion']
```

这段就是 [polynomial.py:76-82](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L76-L82)，列出了 29 个公开名字（28 个 `poly*` + 一个 `Polynomial` 类）。

注意 `__all__` 把**便捷类 `Polynomial` 和函数式 API 放在同一个清单里**——这是有意的。本模块既是一个「函数库」（一堆 `polyxxx(c, ...)`），又是一个「类库」（`Polynomial`）。两者**操作的是同一套系数约定**，只是接口风格不同：

- 函数式：`polyadd(c1, c2)` —— 把系数数组当参数传来传去，无状态。
- 便捷类：`Polynomial(c1) + Polynomial(c2)` —— 把系数封装进对象，用运算符。

> ⚠️ **别和旧 API 混淆**：NumPy 还有一套**顶层**的 `numpy.polyadd`、`numpy.polyval`（legacy，系数**降幂**）。本讲讲的是 `numpy.polynomial.polynomial.polyadd`（系数**升幂**），两者完全不是一回事。这也是 [u1-l1](u1-l1-package-overview.md) 强调「新子包独立于 `numpy.poly1d`」的原因。导入时务必写全路径 `from numpy.polynomial import polynomial as P`，避免撞车。

#### 4.1.4 代码实践

**实践目标**：亲手验证「前缀 + 后缀」规律，并熟悉模块级导入约定。

操作步骤（待本地验证）：

```python
import numpy as np
from numpy.polynomial import polynomial as P   # 约定俗成的别名 P

# 1. 数一数公开名字
print(len(P.__all__))                 # 预期 29
print([n for n in P.__all__ if n.startswith('poly')])  # 28 个 poly* + 类

# 2. 用「后缀」猜用途：polyvalfromroots 是什么？
help(P.polyvalfromroots)              # 应显示 "Evaluate a polynomial specified by its roots"
```

需要观察的现象：

- `P.__all__` 的长度应为 29。
- `polyvalfromroots` 的帮助文档第一句应说明「由根给定的多项式求值」——印证 `val` + `fromroots` 的后缀组合。

预期结果：你能仅凭名字，把 29 个公开 API 全部归类到 4.1.2 的速查表里。

#### 4.1.5 小练习与答案

**练习 1**：`chebyshev.py` 里与 `polyval3d` 对应的函数叫什么名字？

**答案**：`chebval3d`（前缀 `cheb` 换掉 `poly`，后缀 `val3d` 不变）。

**练习 2**：下面三个名字分别属于哪一类？`polyint`、`polyvander2d`、`polyfromroots`。

**答案**：`polyint` 属于微积分（Calculus），`polyvander2d` 属于基矩阵（Basis matrix，二维范德蒙），`polyfromroots` 属于构造（Construction）。

---

### 4.2 内置常数与 `polyline`

#### 4.2.1 概念说明

`polynomial.py` 在文件顶部预定义了四个「常数系数数组」：`polydomain`、`polyzero`、`polyone`、`polyx`。它们不是魔法值，就是几个固定的 1-D `ndarray`，分别代表：

| 常数 | 值 | 代表的多项式 / 含义 |
|------|----|----------------------|
| `polydomain` | `np.array([-1., 1.])` | 默认 **domain 区间** `[-1, 1]`（注意是浮点） |
| `polyzero` | `np.array([0])` | 零多项式 \(0\) |
| `polyone` | `np.array([1])` | 恒等多项式 \(1\) |
| `polyx` | `np.array([0, 1])` | 恒等映射 \(x\)（即 \(0 + 1\cdot x\)） |

为什么要有这些常数？因为本包的理念是「**一切操作即系数操作**」（见 [u1-l1](u1-l1-package-overview.md)）。那么「零」「一」「x」这种最常见的多项式，自然也该有现成的系数数组，方便作为累加的初值、构造的种子、或区间的默认值。其中 `polydomain` 尤其重要：`Polynomial` 类的默认 `domain` 和 `window` 就是它的一份拷贝。

#### 4.2.2 核心流程

常数本身是静态的，但理解它们的关键是「**一个多项式 = 一个系数数组**」这条等式。以 `polyone` 和 `polyx` 为例：

\[
\text{polyone} = [1] \;\Longleftrightarrow\; p(x) = 1
\]

\[
\text{polyx} = [0, 1] \;\Longleftrightarrow\; p(x) = 0 + 1\cdot x = x
\]

把这两个常数喂给算术函数，就能像拼积木一样构造多项式。例如「\(1 + x\)」就是 `polyadd(polyone, polyx)`，结果是 `[1, 1]`。

此外，模块里还有一个**生成一次多项式系数**的小工厂 `polyline(off, scl)`，它返回 \(off + scl\cdot x\) 的系数。它和常数关系密切：

```text
polyline(off, scl) ──┐
   off, scl 都标量    ├── 若 scl != 0: 返回 [off, scl]
                      └── 若 scl == 0: 返回 [off]   # 退化为常数多项式

特例：
  polyline(0, 1) == polyx        # [0, 1]
  polyline(1, 0) == polyone      # [1]  （走 scl==0 分支）
  polyline(0, 0) == polyzero-ish # [0]
```

所以 `polyline` 是更一般的「一次多项式构造器」，而 `polyone`/`polyx` 是它的两个最常用特例被提前算好缓存了起来。

#### 4.2.3 源码精读

先看四个常数的定义与那段说明性注释（[polynomial.py:92-107](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L92-L107)）：

```python
# These are constant arrays are of integer type so as to be compatible
# with the widest range of other types, such as Decimal.
#

# Polynomial default domain.
polydomain = np.array([-1., 1.])

# Polynomial coefficients representing zero.
polyzero = np.array([0])

# Polynomial coefficients representing one.
polyone = np.array([1])

# Polynomial coefficients representing the identity x.
polyx = np.array([0, 1])
```

要点：

- `polyzero/polyone/polyx` 用**整型**数组（`np.array([0])` 默认 int），注释解释这是为了「与尽可能多的类型兼容，比如 `Decimal`」。整型在与浮点、`Decimal`、`Fraction` 等做运算时能保留对方类型。
- `polydomain` 是**浮点** `[-1., 1.]`，因为它表示一个连续区间端点，天然是浮点。

再看 `polyline`（[polynomial.py:114-149](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L114-L149)），核心只有这几行（[polynomial.py:146-149](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L146-L149)）：

```python
if scl != 0:
    return np.array([off, scl])
else:
    return np.array([off])
```

斜率为 0 时返回长度为 1 的数组（常数多项式），否则返回 `[截距, 斜率]`。

> 💡 **常数的真正用法**：`polydomain` 不只是个摆设。`Polynomial` 类体的「虚属性」段直接拿它当默认值（[polynomial.py:1655-1657](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L1655-L1657)）：
>
> ```python
> # Virtual properties
> domain = np.array(polydomain)
> window = np.array(polydomain)
> ```
>
> 也就是说，`Polynomial.domain`、`Polynomial.window` 的默认 `[-1, 1]` 就是 `polydomain` 的一份拷贝。这是常数与便捷类之间一条**真实的代码连接**。

#### 4.2.4 代码实践

**实践目标**：用常数和算术函数「拼」出多项式，体会「系数数组即多项式」。

操作步骤（待本地验证）：

```python
import numpy as np
from numpy.polynomial import polynomial as P

print(P.polyone)            # [1]
print(P.polyx)              # [0, 1]
print(P.polyadd(P.polyone, P.polyx))   # [1, 1]  即 1 + x
print(P.polymul(P.polyx, P.polyx))     # [0, 0, 1] 即 x * x = x^2

# 验证 polyline 的两个特例
print(P.polyline(0, 1) == P.polyx)     # 元素相等 → True
print(P.polyline(1, 0))                # [1] —— 走 scl==0 分支

# 看常数的 dtype
print(P.polyone.dtype, P.polydomain.dtype)   # int64  float64
```

需要观察的现象：

- `polyadd(polyone, polyx)` 得到 `[1, 1]`，与手算 \(1 + x\) 一致。
- `polyline(1, 0)` 返回 `[1]` 而非 `[1, 0]`，证明 `scl==0` 分支会把多项式「退化」成常数。
- `polyone` 是整型，`polydomain` 是浮点。

预期结果：所有打印与上述注释一致。

#### 4.2.5 小练习与答案

**练习 1**：用 `polyzero` 作为初值，写一个循环把 `polyone`、`polyx`、`polyx*? ` 累加成 \(1 + x + x^2\)。

**答案**：`acc = P.polyzero; acc = P.polyadd(acc, P.polyone); acc = P.polyadd(acc, P.polyx); acc = P.polyadd(acc, P.polymul(P.polyx, P.polyx))`，结果应为 `[1, 1, 1]`。

**练习 2**：`polydomain` 为什么是浮点而 `polyone` 是整型？

**答案**：`polydomain` 表示区间的两个端点，是连续几何量，用浮点；`polyone` 等是系数数组，用整型可在与 `Decimal`/`Fraction`/浮点混合运算时尽量保留对方类型（见源码注释），兼容面更广。

---

### 4.3 `polytrim` 别名与三层委托关系

#### 4.3.1 概念说明

本模块开头有一行看似平淡的赋值（[polynomial.py:90](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L90)）：

```python
polytrim = pu.trimcoef
```

这就是 `polytrim` 的**全部定义**——它只是 `polyutils.trimcoef` 的一个别名。`polytrim(c, tol=0)` 的作用是：从系数数组 `c` 的**高端（高次项）**开始，把绝对值不超过 `tol` 的「尾部小系数」砍掉，返回一个更短的系数数组。典型用途是清理拟合后产生的 \(10^{-16}\) 量级的「数值噪声」系数。

但这一行别名的意义不在功能本身，而在它揭示了 `polynomial.py` 的核心设计模式——**委托（delegation）**。本模块的很多 `poly*` 函数并不「自己干活」，而是把活儿转交给 `polyutils` 里更通用的 `_*` 辅助函数。理解了这条委托链，你就拿到了读后续 [u3](#) 源码精读讲义的钥匙。

#### 4.3.2 核心流程

`polytrim` 是最薄的一层委托（一个别名）。而算术函数的委托要「厚」一点，但模式相同。把一个加法运算从用户代码一路追到底层，会经过**三层**：

```text
第①层  便捷类（_polybase.py）        第②层  函数式 API（polynomial.py）       第③层  通用工具（polyutils.py）
┌──────────────────────────┐      ┌──────────────────────────┐      ┌──────────────────────────┐
│ Polynomial.__add__       │      │ def polyadd(c1, c2):     │      │ def _add(c1, c2):        │
│   return self._add(      │ ───▶ │     return pu._add(c1,c2)│ ───▶ │     [c1,c2]=as_series(..) │
│       self.coef,         │      │                          │      │     对齐长度后逐项相加      │
│       other.coef)        │      │ # _add = staticmethod(   │      │     return trimseq(ret)   │
│                          │      │ #   polyadd)             │      │                          │
└──────────────────────────┘      └──────────────────────────┘      └──────────────────────────┘
        p + q                          polyadd                          pu._add（真活儿）
```

- **第①层**：`Polynomial.__add__`（在 `_polybase.py`）调用虚函数 `self._add`。
- **第②层**：因为 `Polynomial._add = staticmethod(polyadd)`，调用落到 `polynomial.py` 的 `polyadd`。
- **第③层**：`polyadd` 又把球踢给 `pu._add`（在 `polyutils.py`），后者才真正做 `as_series` 规整、对齐、相加、`trimseq` 收尾。

`polytrim = pu.trimcoef` 就是这条链的「极简版」：第②层直接等于第③层，没有第①层。一旦你看懂这行，整条链的逻辑就通透了——它**到处都是同一个模式**。

#### 4.3.3 源码精读

**别名本身**（[polynomial.py:90](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L90)）：

```python
polytrim = pu.trimcoef
```

`pu` 是模块顶部的别名导入（[polynomial.py:87](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L87)）：`from . import polyutils as pu`。

**别名指向的真函数 `trimcoef`**（[polyutils.py:144-192](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L144-L192)），关键逻辑（[polyutils.py:184-192](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L184-L192)）：

```python
if tol < 0:
    raise ValueError("tol must be non-negative")

[c] = as_series([c])
[ind] = np.nonzero(np.abs(c) > tol)
if len(ind) == 0:
    return c[:1] * 0           # 全部都被视为「小」 → 返回 [0]
else:
    return c[:ind[-1] + 1].copy()
```

要点：找最后一个「绝对值大于 `tol`」的系数下标 `ind[-1]`，把它之前的所有系数保留（中间的小系数**不**被删，只删「尾部」）。注意与 `trimseq`（[polyutils.py:34-60](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L34-L60)，只删**精确为 0** 的尾部）的区别：`polytrim` 按**容差**删，`trimseq` 按**相等**删。

**委托链的完整对照表**——本模块哪些 `poly*` 把活儿交给了哪个 `pu._*`：

| `polynomial.py` 的函数 | 委托目标 | 说明 |
|------------------------|----------|------|
| `polytrim` | `pu.trimcoef` | 直接别名（[polynomial.py:90](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L90)） |
| `polyadd` / `polysub` | `pu._add` / `pu._sub` | `return pu._add(c1, c2)`（[polynomial.py:249](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L249)） |
| `polyfromroots` | `pu._fromroots(polyline, polymul, roots)` | 把「构造一次因子」和「相乘」两个能力作为参数传入（[polynomial.py:213](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L213)） |
| `polypow` | `pu._pow(np.convolve, c, pow, maxpower)` | 把卷积当作「乘法」传入（[polynomial.py:463](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L463)） |
| `polyfit` | `pu._fit(polyvander, x, y, deg, ...)` | 把范德蒙构造函数传入（[polynomial.py:1499](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L1499)） |
| `polyval2d/3d/nd` | `pu._valnd(polyval, c, ...)` | 把一维求值函数传入（[polynomial.py:904](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L904)） |
| `polygrid2d/3d` | `pu._gridnd(polyval, c, ...)` | 同上，但走「网格」广播（[polynomial.py:960](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L960)） |
| `polyvander2d/3d` | `pu._vander_nd_flat(...)` | 多维范德蒙（[polynomial.py:1271](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L1271)） |

注意一个**例外**：并非所有 `poly*` 都委托。`polyval` 自己用 Horner 法实现（[polynomial.py:756-759](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L756-L759)）；`polymul` 直接调 `np.convolve`（[polynomial.py:365](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L365)）；`polydiv` 甚至手写了一段比 `pu._div` 更高效的长除法（[polynomial.py:402-424](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L402-L424)，注释 [polynomial.py:407](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L407) 说明了原因）。所以委托是「能复用就复用，有性能优势就特化」的实用策略，不是教条。

**第①层到第②层的连接**——`Polynomial` 类体里的虚函数绑定（[polynomial.py:1641-1653](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L1641-L1653)）：

```python
# Virtual Functions
_add = staticmethod(polyadd)
_sub = staticmethod(polysub)
_mul = staticmethod(polymul)
_div = staticmethod(polydiv)
_pow = staticmethod(polypow)
_val = staticmethod(polyval)
_int = staticmethod(polyint)
_der = staticmethod(polyder)
_fit = staticmethod(polyfit)
_line = staticmethod(polyline)
_roots = staticmethod(polyroots)
_fromroots = staticmethod(polyfromroots)
```

每个 `_xxx` 都指向本模块的一个 `polyxxx`。这就是「便捷类 → 函数式 API」的胶水。`staticmethod(...)` 的包裹是为了让这些函数在作为「方法」被访问时不会把 `self` 当成第一个参数——它们是**无状态**的纯函数。

把三段代码连起来读：用户写 `Polynomial([1,2,3]) + Polynomial([0,1])` → `_polybase.py` 的 `__add__` 调 `self._add(c1, c2)` → 命中 `_add = staticmethod(polyadd)` → `polyadd` 调 `pu._add(c1, c2)` → `pu._add`（[polyutils.py:555-565](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polyutils.py#L555-L565)）做对齐相加并 `trimseq`。三行 `return`、三次转手，但每一层都有清晰职责。

> 🔑 **一句话总结这条链**：便捷类提供「**接口**」（运算符、方法、domain/window），函数式 API 提供「**幂基专属语义**」（具体用 `polyvander` 还是 Horner、用卷积还是长除法），`polyutils` 提供「**与基无关的通用算法**」（对齐、相加、最小二乘求解）。`polytrim = pu.trimcoef` 把这条链压缩成了一行。

#### 4.3.4 代码实践

**实践目标**：亲手验证三层委托链的两端，确认「便捷类默认 domain = polydomain」「polytrim 与 trimcoef 同一对象」。

操作步骤（待本地验证）：

```python
import numpy as np
from numpy.polynomial import polynomial as P
from numpy.polynomial import polyutils as pu
from numpy.polynomial import Polynomial

# 1. polytrim 就是 pu.trimcoef（同一函数对象）
print(P.polytrim is pu.trimcoef)         # True —— 别名，不是副本

# 2. trimcoef 按容差删尾部，中间的小系数不删
print(P.polytrim([0, 0, 3, 0, 5, 0, 0]))            # [0. 0. 3. 0. 5.]
print(P.polytrim([0, 0, 1e-3, 0, 1e-5, 0, 0], 1e-3))# [0.]

# 3. 便捷类的默认 domain 就是 polydomain 的拷贝
print(np.array_equal(Polynomial.domain, P.polydomain))  # True

# 4. 追一条委托链：Polynomial._add 指向 polyadd
print(Polynomial._add is P.polyadd)       # True
print(P.polyadd([1,2,3], [0,1]))          # [1. 3. 3.] —— 走 pu._add
```

需要观察的现象：

- `polytrim is pu.trimcoef` 为 `True`，证明是同一对象。
- `polytrim([0,0,3,0,5,0,0])` 得到 `[0,0,3,0,5]`：尾部两个 0 被删，但中间的 0（第 4 位）保留——这是 `trimcoef`「只删尾部」的关键特征。
- `Polynomial.domain` 与 `polydomain` 元素相等，印证 4.2 节的代码连接。
- `Polynomial._add is P.polyadd` 为 `True`，印证第①层→第②层的绑定。

预期结果：全部打印与注释一致。

> 📝 **如何确认委托而不靠猜**：在解释器里对任何 `polyxxx` 用 `inspect.getsource(P.polyxxx)` 看它的实现。如果函数体只有一句 `return pu._xxx(...)`，那就是「薄委托」；如果有真正的循环或数组运算，那就是「特化实现」。这是后续阅读 [u3](#) 源码精读讲义时的通用探查手法。

#### 4.3.5 小练习与答案

**练习 1**：`P.polytrim([1, 2, 1e-10, 1e-12], tol=1e-8)` 返回什么？为什么？

**答案**：返回 `[1. 2.]`。因为从尾部看，`1e-12` 和 `1e-10` 都 ≤ `1e-8`，被当作「小」删掉；最后保留到下标 1（值 2）。注意一旦遇到 `2 > tol` 就停止向前删，即便前面还有更小的系数也不会动。

**练习 2**：请按调用顺序排出 `Polynomial([1,2,3]).deriv()` 经过的三层。

**答案**：第①层 `_polybase.py` 的 `deriv` 方法调 `self._der(...)`；第②层命中 `_der = staticmethod(polyder)`，进入 `polynomial.py` 的 `polyder`（[polynomial.py:466-543](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/polynomial/polynomial.py#L466-L543)）；第③层这里 `polyder` **没有**再委托给 `pu`，而是自己实现了系数移位与阶乘乘法（属于「特化实现」）。

**练习 3**：为什么 `Polynomial` 类里写的是 `_add = staticmethod(polyadd)`，而不是 `_add = polyadd`？

**答案**：因为 `_add` 会通过实例 `self._add(...)` 调用。如果不包 `staticmethod`，Python 会把它当成普通方法，自动把 `self` 作为第一个参数传入，导致 `polyadd(self, c2)` 参数错位。`staticmethod` 关闭了这一绑定，使 `polyadd(c1, c2)` 按原签名被调用。

---

## 5. 综合实践

**任务**：仅用 `polynomial.py` 的**函数式 API**完成「由根构造 → 求值 → 求导」全流程，再用 `Polynomial` 便捷类写出**等价**代码并对比，亲手验证本讲讲的两条核心结论：① 函数式 API 与便捷类操作同一套系数；② 便捷类内部就是把运算委托给函数式 API。

给定根 \([1, 2, 3]\)，对应多项式为 \((x-1)(x-2)(x-3) = x^3 - 6x^2 + 11x - 6\)，系数（低到高）应为 `[-6, 11, -6, 1]`。

### 步骤一：函数式 API 实现

```python
import numpy as np
from numpy.polynomial import polynomial as P

roots = [1, 2, 3]

# (1) 由根构造系数：polyfromroots 内部调 pu._fromroots(polyline, polymul, roots)
c = P.polyfromroots(roots)
print(c)                       # 预期 [-6.  11. -6.  1.]

# (2) 在 [-1, 1] 上求值：polyval 用 Horner 法
x = np.linspace(-1, 1, 5)
y = P.polyval(x, c)
print(y)                       # 在 x=1 处应为 0（因为 1 是根）

# (3) 求导：polyder 做系数移位 + 阶乘乘法
dc = P.polyder(c)              # 3*x^2 - 12*x + 11 → [11, -12, 3]
print(dc)                      # 预期 [11. -12.   3.]
dy_at_1 = P.polyval(1, dc)     # 导数在 x=1 的值 = 3 - 12 + 11 = 2
print(dy_at_1)                 # 预期 2.0
```

### 步骤二：便捷类等价实现

```python
from numpy.polynomial import Polynomial

p = Polynomial.fromroots([1, 2, 3])
print(p.coef)                  # [-6. 11. -6. 1.] —— 与函数式结果一致

y2 = p(x)                      # 默认 domain==window==[-1,1]，映射恒等
print(np.allclose(y, y2))      # True

dp = p.deriv()
print(dp.coef)                 # [11. -12. 3.] —— 与 polyder 结果一致
print(dp(1))                   # 2.0
```

### 步骤三：对照与思考

请回答（待本地验证后填写）：

1. `p.coef` 与 `P.polyfromroots([1,2,3])` 是否完全相等？—— 这验证了**「函数式 API 与便捷类操作同一套系数」**。
2. `p(x)` 与 `P.polyval(x, c)` 是否在数值上相等（`np.allclose`）？—— 注意：只有当 `domain == window`（恒等映射）时二者才相等；否则 `p(x)` 会先做坐标映射（回顾 [u1-l3](u1-l3-polynomial-class-quickstart.md) 4.3 节）。本例默认满足。
3. 在 `p(x)` 处下断点或加日志：它最终会不会调到 `polyval`？—— 这验证了**「便捷类把求值委托给函数式 API」**。

> **进阶**：把第(2)步的 `polyval` 换成「范德蒙矩阵乘法」`np.dot(P.polyvander(x, len(c)-1), c)`，结果应与 `P.polyval(x, c)` 在舍入误差内相等。这正是 `polyfit` 用范德蒙矩阵做最小二乘的理论基础（将在 [u3-l5](u3-l5-vandermonde-leastsquares-fit.md) 详讲）。

## 6. 本讲小结

- `polynomial.py` 的公开 API 全部以 **`poly`** 开头（唯一的类是 `Polynomial`），后缀描述功能（`add/mul/val/der/fit/roots/vander/line/fromroots`）和维度（`2d/3d/nd/grid`）；这套「前缀=基、后缀=功能」的规律在六大正交族里通用。
- 四个内置常数都是固定系数数组：`polydomain=[-1.,1.]`（默认区间，浮点）、`polyzero=[0]`、`polyone=[1]`、`polyx=[0,1]`（整型，为兼容 `Decimal` 等）；`Polynomial.domain/window` 的默认值就是 `polydomain` 的拷贝。
- `polyline(off, scl)` 是一次多项式 \(off+scl\cdot x\) 的系数工厂，`polyx`、`polyone` 是它的特例。
- `polytrim = pu.trimcoef` 是「别名式委托」的最简例子，揭示了贯穿全模块的**三层委托链**：便捷类（`_polybase.py` 的 `__add__`/`__call__`/`deriv`…）→ 函数式 API（`polyadd`/`polyval`/`polyder`…）→ 通用工具（`pu._add`/Horner/`pu._fit`…）。
- 委托是「能复用就复用、有性能优势就特化」的实用策略：`polyadd/polyfromroots/polyfit` 是薄委托，而 `polyval`（Horner）、`polymul`（`np.convolve`）、`polydiv`（手写长除法）则是为了效率而特化。

## 7. 下一步学习建议

本讲把「函数式 API 长什么样、怎么和便捷类对应」讲清了，但每个函数**内部**怎么算还没展开。后续建议：

- **进入 [u3](#)「幂级数实现细节」单元**，逐篇精读函数式 API 的内部实现：先读 [u3-l1 polyutils 工具函数基石](u3-l1-polyutils-foundations.md)（`as_series`/`trimseq`/`trimcoef`/`getdomain`），因为本讲末尾的委托链终点都在那里；再读 [u3-l2 幂级数的创建与算术](u3-l2-power-series-creation-arithmetic.md) 看 `polyfromroots` 的分治乘法、`polydiv` 的长除法。
- **想理解求值/微积分/拟合/求根的算法**：分别看 [u3-l3 Horner 求值](u3-l3-evaluation-horner.md)、[u3-l4 求导与积分](u3-l4-derivative-integral.md)、[u3-l5 范德蒙与最小二乘](u3-l5-vandermonde-leastsquares-fit.md)、[u3-l6 伴随矩阵求根](u3-l6-companion-matrix-roots.md)。
- **想横向迁移到其他基**：[u4](#) 单元会把本讲的 `poly*` 规律直接套用到 `cheb*`/`leg*`/`lag*`/`herm*`/`herme*`，你会发现六族模块结构几乎同构。
- **马上能做的小练习**：用 `inspect.getsource(P.polyfit)` 看 `polyfit` 是不是一行 `return pu._fit(...)`，然后去 `polyutils.py` 读 `_fit` 的列归一化与 `lstsq`——这是本讲「顺着委托往下钻一层」手法的第一次实战。
