# 字符串、格式化与 LaTeX 表示

> 承接：本讲建立在 [u2-l1 ABCPolyBase 抽象基类与虚函数模式](u2-l1-abcpolybase-virtual-methods.md) 之上。你已经知道六大便捷类共享 `ABCPolyBase` 这层「外壳」，算术、求值等行为都集中在此。本讲专门拆解这层外壳里的**打印（字符串表示）子系统**——它是 `ABCPolyBase` 里代码量最大、最值得细读的一块，也是「父类管流程、子类管算法」模式在非数值场景下的又一次精彩演绎。

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 `__str__`、`__repr__`、`__format__` 三个入口各自的职责，并能解释它们的**优先级**关系。
- 看懂 `_generate_string` 如何把一个系数数组逐项拼接成 `1.0 + 2.0·x + 3.0·x²` 这样的数学表达式，并理解其中的换行逻辑。
- 区分 **unicode**（`x²`、`T₁`）与 **ascii**（`x**2`、`T_1`）两套风格，理解上下标映射表的作用。
- 理解 `_repr_latex_` 如何产出 LaTeX 源码，让 Jupyter 把多项式渲染成数学公式。
- 用 `set_default_printstyle` 切换全局打印风格，用 `f"{p:ascii}"` 在不污染全局状态的前提下临时指定风格。

## 2. 前置知识

在进入源码前，先厘清三个 Python 协议层面的概念，它们决定了「打印一个对象」到底会走到哪段代码：

- **`__str__` 与 `str(p)` / `print(p)`**：面向「人类可读」的字符串。`print(p)`、`str(p)`、`f"{p}"`（空格式说明符）都会触发它。返回值应当是一句通顺的数学表达。
- **`__repr__` 与 `repr(p)`**：面向「开发者/机器」的字符串，理想情况下是一个能 `eval` 回原对象的表达式。在交互式终端里直接键入变量名 `p` 回车，看到的就是 `__repr__`。
- **`__format__` 与 `format(p, spec)` / `f"{p:spec}"`**：格式化协议。冒号后的 `spec` 是格式说明符，由对象自己解释。numpy.polynomial 把 `'ascii'` 和 `'unicode'` 两个字符串当作合法说明符，从而允许 `f"{p:unicode}"` 临时指定风格。

还有一个 NumPy 全局概念要预先知道：[`np.get_printoptions()`](https://numpy.org/doc/stable/reference/generated/numpy.get_printoptions.html) 返回一组打印选项，其中的 `linewidth`（默认 75）和 `precision` 会被多项式打印管线读取。也就是说，调整 NumPy 的全局打印选项会间接影响多项式的换行和系数精度——这是后面 `_generate_string` 会用到的细节。

## 3. 本讲源码地图

| 文件 | 在本讲中的作用 |
|------|----------------|
| [_polybase.py](_polybase.py) | **绝对主角**。打印管线的几乎全部逻辑（`__str__`/`__repr__`/`__format__`/`_generate_string`/`_str_term_*`/`_repr_latex_*`）都集中在这个抽象基类里。 |
| [polynomial.py](polynomial.py) | 标准幂基便捷类 `Polynomial` 是一个**特例**：它的 `basis_name = None`，因此重写了 `_str_term_unicode`/`_str_term_ascii`/`_repr_latex_term`，把 `x²` 这种「变量自己带幂」的写法实现出来。 |
| [__init__.py](__init__.py) | 提供 `set_default_printstyle` 全局开关，本质是改写 `ABCPolyBase._use_unicode` 这个类属性。 |
| [polyutils.py](polyutils.py) | 提供 `format_float`，负责把单个浮点系数格式化成符合 NumPy 打印选项的字符串，被 `_generate_string` 反复调用。 |

## 4. 核心概念与源码讲解

### 4.1 打印管线的三个入口：__str__、__repr__ 与 __format__

#### 4.1.1 概念说明

同一个多项式对象 `p = Polynomial([1, 2, 3])`，在不同场合会以**三种面貌**出现：

| 触发方式 | 调用方法 | 典型输出 |
|----------|----------|----------|
| `print(p)` / `str(p)` | `__str__` | `1.0 + 2.0·x + 3.0·x²` |
| 终端里直接 `p` 回车 / `repr(p)` | `__repr__` | `Polynomial([1., 2., 3.], domain=[-1.,  1.], window=[-1.,  1.], symbol='x')` |
| `f"{p:unicode}"` / `format(p, 'ascii')` | `__format__` | 由冒号后的说明符决定，可临时覆盖全局风格 |

三者的分工很清晰：`__repr__` 忠实地记录「我是谁、系数是什么、domain/window/symbol 是什么」，原则上能重建对象；`__str__` 只关心「我表达的数学函数长什么样」；`__format__` 则是 `__str__` 的「可参数化版本」，让调用方在一次格式化里指定风格，而不必动全局开关。

#### 4.1.2 核心流程

三条路径汇合到一个统一的「逐项拼接引擎」上：

```
print(p) ──► __str__ ──┐
                       ├──► _generate_string(term_method) ──► 数学表达式字符串
f"{p:ascii}" ──► __format__ ──┘
```

- `__str__` 先读类属性 `_use_unicode`，决定用 unicode 还是 ascii 的「单项生成函数」，再交给 `_generate_string`。
- `__format__` 直接根据格式说明符选定单项生成函数，**完全跳过** `_use_unicode`——这就是「格式化优先级高于全局默认」的实现原因。
- `__repr__` 是独立的一条线，不走 `_generate_string`，而是把 `coef/domain/window` 三个数组的 `repr` 切片后拼成一个构造调用。

优先级口诀：**格式说明符（`f"{p:ascii}"`） > 全局开关（`set_default_printstyle`） > 平台默认（Unix 用 unicode，Windows 用 ascii）**。

#### 4.1.3 源码精读

先看最朴素的 `__repr__`：

[__repr__ 把三个数组的 repr 各砍掉头尾](_polybase.py#L322-L328)

```python
def __repr__(self):
    coef = repr(self.coef)[6:-1]
    domain = repr(self.domain)[6:-1]
    window = repr(self.window)[6:-1]
    name = self.__class__.__name__
    return (f"{name}({coef}, domain={domain}, window={window}, "
            f"symbol='{self.symbol}')")
```

`repr(np.array([1., 2., 3.]))` 得到 `'array([1., 2., 3.])'`，切片 `[6:-1]` 正好削掉开头的 `array(`（6 个字符）和结尾的 `)`，留下 `[1., 2., 3.]`。于是输出形如 `Polynomial([1., 2., 3.], domain=[-1.,  1.], window=[-1.,  1.], symbol='x')`——把这段字符串贴回 `Polynomial(...)` 即可重建对象，满足 `__repr__` 的「可重建」理想。注意它用 `self.__class__.__name__` 取类名，所以六大便捷类的 `__repr__` 都自动正确，无需各自重写。

再看两个真正产生「数学表达式」的入口：

[__str__：根据 _use_unicode 选单项函数](_polybase.py#L343-L346)

```python
def __str__(self):
    if self._use_unicode:
        return self._generate_string(self._str_term_unicode)
    return self._generate_string(self._str_term_ascii)
```

[__format__：格式说明符直接决定风格，并校验非法说明符](_polybase.py#L330-L341)

```python
def __format__(self, fmt_str):
    if fmt_str == '':
        return self.__str__()
    if fmt_str not in ('ascii', 'unicode'):
        raise ValueError(
            f"Unsupported format string '{fmt_str}' passed to "
            f"{self.__class__}.__format__. Valid options are "
            f"'ascii' and 'unicode'"
        )
    if fmt_str == 'ascii':
        return self._generate_string(self._str_term_ascii)
    return self._generate_string(self._str_term_unicode)
```

三个关键细节：

1. **空说明符回退到 `__str__`**：`f"{p}"`、`format(p)`、`str(p)` 三者结果一致。
2. **非法说明符抛 `ValueError`**：`f"{p:latex}"` 会报错，并贴心地把合法选项列在消息里。
3. **`__format__` 完全不读 `_use_unicode`**：这就是 `f"{p:ascii}"` 能在「全局设成 unicode」时仍然输出 ascii 的原因——格式说明符是「一次性的、显式的」，全局开关是「持久的、隐式的」，前者覆盖后者。

至于 `_use_unicode` 这个类属性的默认值，由平台决定：

[平台默认：Unix 用 unicode，Windows 用 ascii](_polybase.py#L104-L108)

```python
# Some fonts don't support full unicode character ranges necessary for
# the full set of superscripts and subscripts, including common/default
# fonts in Windows shells/terminals. Therefore, default to ascii-only
# printing on windows.
_use_unicode = not os.name == 'nt'
```

注释解释了原因：Windows 终端的默认字体常常缺失上下标的 unicode 字形，所以默认退回 ascii。注意这是**类属性**而非实例属性——所有六大类共享同一个 `ABCPolyBase._use_unicode`，这正是下一节 `set_default_printstyle` 能「一次切换、全部生效」的前提。

#### 4.1.4 代码实践

**实践目标**：亲手验证三个入口的分工与优先级。

**操作步骤**（待本地验证）：

```python
import numpy as np
from numpy.polynomial import Polynomial, Chebyshev

p = Polynomial([1, 2, 3])

# 1) 三个入口分别输出什么？
print(str(p))          # __str__
print(repr(p))         # __repr__
print(f"{p:ascii}")    # __format__ 显式指定 ascii
print(f"{p:unicode}")  # __format__ 显式指定 unicode

# 2) 把 __repr__ 的输出贴回去，能否重建等价对象？
q = eval(repr(p))
print(p == q)          # 期望 True（__eq__ 比较系数/domain/window/symbol）
```

**需要观察的现象**：

- `str(p)` 是数学表达式，`repr(p)` 是构造调用。
- `f"{p:ascii}"` 与 `f"{p:unicode}"` 的差异体现在 `x**2` 与 `x²`。
- `eval(repr(p)) == p` 应为 `True`，验证 `__repr__` 的可重建性。

**预期结果**：上述四行打印分别给出 `1.0 + 2.0·x + 3.0·x²`（或 ascii 版）、`Polynomial([1., 2., 3.], domain=[-1.,  1.], window=[-1.,  1.], symbol='x')`、`1.0 + 2.0 x + 3.0 x**2`、`1.0 + 2.0·x + 3.0·x²`，最后一行 `True`。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `__repr__` 用 `self.__class__.__name__` 而不是写死字符串 `"Polynomial"`？

**答案**：因为 `__repr__` 定义在 `ABCPolyBase`，被六大便捷类共同继承。用 `self.__class__.__name__` 取运行时实际类名，才能让 `Chebyshev`、`Legendre` 等子类都打印出正确的类名，无需各自重写 `__repr__`。

**练习 2**：执行 `f"{p:latex}"` 会发生什么？异常类型是什么？

**答案**：抛 `ValueError`，消息为 `Unsupported format string 'latex' passed to ...`。`__format__` 只接受 `''`、`'ascii'`、`'unicode'` 三种说明符。

---

### 4.2 逐项拼接引擎 _generate_string 与 ascii/unicode 双风格

#### 4.2.1 概念说明

`_generate_string` 是整个打印子系统的「心脏」。它接收一个**单项生成函数** `term_method`，把系数数组逐项拼成完整表达式。核心思想是模板分离：

- **每一项的「系数 + 正负号」部分**是通用的，与基无关，由 `_generate_string` 统一处理。
- **每一项的「基函数」部分**因基而异，委托给 `term_method`。

这样，`_generate_string` 只写一遍，就能同时服务幂基（`x²`）和 Chebyshev 基（`T₂(x)`）等所有家族——这正是「父类管流程、子类管算法」的又一次体现。

两种单项风格的差别：

| 风格 | 幂次表示 | 下标表示 | 乘法符号 | 示例（Chebyshev 2 次项） |
|------|----------|----------|----------|--------------------------|
| unicode | 上标字符 `²` | 下标字符 `₂` | 中点 `·` | `·T₂(x)` |
| ascii | `**2` | `_2` | 空格 | ` T_2(x)` |

unicode 更美观，但依赖字体支持；ascii 到处可显示，适合 Windows 终端或纯文本日志。

#### 4.2.2 核心流程

`_generate_string(term_method)` 的执行步骤：

1. 读取 `np.get_printoptions()['linewidth']`（默认 75）作为换行阈值。
2. 用 `format_float` 格式化**常数项** `coef[0]`，作为输出字符串 `out` 的起点。
3. 用 `mapparms()` 算出 domain→window 的线性映射参数 `off, scale`，再由 `_format_term` 构造「缩放后的自变量符号」（如 `0.5 + 0.1·x`，下讲详述；当 domain==window 时就是单纯的 `x`）。
4. 遍历 `coef[1:]` 的每一项：
   - 判定系数正负，拼出 `+ 2.0` 或 `- 3.0`（负号提出，用绝对值格式化）。
   - 调用 `term_method(power, scaled_symbol)` 拼出基函数部分。
   - 估算「加上本项后这一行的长度」，若超过 `linewidth`，就把本项开头的空格换成换行符，实现**逐项换行**。
5. 返回拼接好的字符串。

单项生成函数 `_str_term_unicode` / `_str_term_ascii` 的核心技巧是**上下标映射表**：把幂次（一个十进制字符串如 `"12"`）用 `str.translate` 一次性翻译成上标 `"¹²"` 或下标 `"₁₂"`，无需为每个幂次写特判。

#### 4.2.3 源码精读

先看两张映射表，它们是双风格的基石：

[上下标映射表：把 ASCII 数字翻译成 unicode 上下标](_polybase.py#L79-L103)

```python
# Unicode character mappings for improved __str__
_superscript_mapping = str.maketrans({
    "0": "⁰", "1": "¹", "2": "²", "3": "³", "4": "⁴",
    "5": "⁵", "6": "⁶", "7": "⁷", "8": "⁸", "9": "⁹"
})
_subscript_mapping = str.maketrans({
    "0": "₀", "1": "₁", "2": "₂", "3": "₃", "4": "₄",
    "5": "₅", "6": "₆", "7": "₇", "8": "₈", "9": "₉"
})
```

`str.maketrans` 构造一张「单字符替换」字典，`str.translate` 据此逐字符翻译。于是 `"12".translate(_superscript_mapping)` 直接得到 `"¹²"`，连两位数幂次也无需特判——这是一个干净利落的小设计。

再看通用单项生成函数（被 `Chebyshev`/`Legendre`/`Laguerre`/`Hermite`/`HermiteE` 这五个有 `basis_name` 的家族继承）：

[_str_term_unicode：basis_name + 下标幂次](_polybase.py#L394-L406)

```python
@classmethod
def _str_term_unicode(cls, i, arg_str):
    if cls.basis_name is None:
        raise NotImplementedError(
            "Subclasses must define either a basis_name, or override "
            "_str_term_unicode(cls, i, arg_str)"
        )
    return (f"·{cls.basis_name}{i.translate(cls._subscript_mapping)}"
            f"({arg_str})")
```

[_str_term_ascii：basis_name + 下划线下标](_polybase.py#L408-L419)

```python
@classmethod
def _str_term_ascii(cls, i, arg_str):
    if cls.basis_name is None:
        raise NotImplementedError(...)
    return f" {cls.basis_name}_{i}({arg_str})"
```

注意 `i` 是**幂次的字符串形式**（如 `"1"`、`"12"`），不是整数。所以 `i.translate(cls._subscript_mapping)` 直接生效。`basis_name` 为 `None` 时主动抛 `NotImplementedError`，提示子类「要么给 `basis_name`，要么重写本方法」——这是对 `Polynomial` 这种特例的明确约定。

`Polynomial` 正是那个特例。标准幂基的第 \(i\) 个基函数就是 \(x^i\) 本身，不需要字母前缀，于是它把 `basis_name` 留空并重写了三个方法：

`Polynomial` 把 [polynomial.py:1658](polynomial.py#L1658) 的 `basis_name` 留空为 `None`。正因如此，通用的 [_str_term_unicode](_polybase.py#L394-L406) 会抛 `NotImplementedError`，`Polynomial` 必须重写这三个单项生成函数（[polynomial.py:1660-1672](polynomial.py#L1660-L1672)）：

[Polynomial._str_term_unicode：变量自己带 unicode 上标](polynomial.py#L1660-L1665)

```python
@classmethod
def _str_term_unicode(cls, i, arg_str):
    if i == '1':
        return f"·{arg_str}"            # 一次项：·x
    else:
        return f"·{arg_str}{i.translate(cls._superscript_mapping)}"  # ·x²
```

[Polynomial._str_term_ascii：变量带 **幂](polynomial.py#L1667-L1672)

```python
@staticmethod
def _str_term_ascii(i, arg_str):
    if i == '1':
        return f" {arg_str}"            #  x
    else:
        return f" {arg_str}**{i}"       #  x**2
```

注意一次项（`i == '1'`）被单独处理：幂次为 1 时数学上不写出指数，所以 unicode 给 `·x`、ascii 给 ` x`。

最后是拼装一切的引擎：

[_generate_string：逐项拼装 + 缩放自变量 + 换行](_polybase.py#L348-L392)

```python
def _generate_string(self, term_method):
    linewidth = np.get_printoptions().get('linewidth', 75)
    if linewidth < 1:
        linewidth = 1
    out = pu.format_float(self.coef[0])           # 常数项

    off, scale = self.mapparms()
    scaled_symbol, needs_parens = self._format_term(pu.format_float, off, scale)
    if needs_parens:
        scaled_symbol = '(' + scaled_symbol + ')'

    for i, coef in enumerate(self.coef[1:]):
        out += " "
        power = str(i + 1)
        try:
            if coef >= 0:
                next_term = "+ " + pu.format_float(coef, parens=True)
            else:
                next_term = "- " + pu.format_float(-coef, parens=True)
        except TypeError:
            next_term = f"+ {coef}"               # 对象数组（如复数/字符串）兜底
        next_term += term_method(power, scaled_symbol)
        line_len = len(out.split('\n')[-1]) + len(next_term)
        if i < len(self.coef[1:]) - 1:
            line_len += 2                          # 预留下一项的 "+ "/"- "
        if line_len >= linewidth:
            next_term = next_term.replace(" ", "\n", 1)   # 触发换行
        out += next_term
    return out
```

几个要点：

- **正负号统一为加法**：负系数写成 `- 3.0` 而非 `+ -3.0`，更接近数学习惯；`format_float(-coef)` 保证传入的是正值。
- **对象数组兜底**：系数可能是字符串、复数等无法与 `0` 比较的类型，`coef >= 0` 会抛 `TypeError`，此时退化为 `+ {coef}` 原样输出。
- **换行算法**：`out.split('\n')[-1]` 取「当前行」的长度，加上新项长度（再加 2 预留下一项符号），若超过 `linewidth` 就把本项首个空格替换成换行。这是一种简单贪心的逐项换行。

底层的 `format_float` 负责把单个浮点数按 NumPy 打印选项（精度、浮点模式、特殊值字符串）格式化，这里只看它的签名和职责，细节留到下一讲：

[format_float：按 NumPy 打印选项格式化单个浮点系数](polyutils.py#L725-L759)

它读取 `np.get_printoptions()`，处理 `nan`/`inf`、科学计数法阈值、`floatmode` 等，并对 `parens=True` 的情况加括号（用于负系数或需要明确结合律的场合）。

#### 4.2.4 代码实践

**实践目标**：观察双风格差异，并验证「两位数幂次」也能正确翻译。

**操作步骤**（待本地验证）：

```python
import numpy as np
from numpy.polynomial import Chebyshev

c = Chebyshev.basis(12)          # 只有第 12 次项系数为 1
print(f"unicode: {c:unicode}")
print(f"ascii:   {c:ascii}")

# 对比 Polynomial 的两位数幂次
from numpy.polynomial import Polynomial
p = Polynomial.basis(12)
print(f"poly unicode: {p:unicode}")
print(f"poly ascii:   {p:ascii}")
```

**需要观察的现象**：

- `Chebyshev` 的 unicode 输出含 `T₁₂`（下标 `₁₂`），ascii 含 `T_12`。
- `Polynomial` 的 unicode 输出含 `x¹²`（上标 `¹²`），ascii 含 `x**12`。
- 两位数幂次无需特判，全靠 `str.translate` 翻译整串数字。

**预期结果**：unicode 行分别出现 `·T₁₂(x)` 与 `·x¹²`；ascii 行分别出现 ` T_12(x)` 与 ` x**12`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `_str_term_unicode` 接收的 `i` 是字符串而不是整数？

**答案**：因为 `i.translate(_subscript_mapping)` 需要字符串才能逐字符翻译成上下标。`_generate_string` 里 `power = str(i + 1)` 已经把它转成字符串再传入，使得两位数幂次（如 `"12"`）也能整体翻译成 `"₁₂"`。

**练习 2**：如果把 `Chebyshev` 类的 `basis_name` 改成 `None` 但不重写 `_str_term_unicode`，打印时会怎样？

**答案**：调用 `_str_term_unicode` 时会命中 `if cls.basis_name is None: raise NotImplementedError(...)` 分支，抛出 `NotImplementedError`，提示「要么定义 `basis_name`，要么重写该方法」。这正是 `Polynomial` 选择重写而非留空的原因。

---

### 4.3 LaTeX 表示：_repr_latex_ 与 Jupyter 渲染

#### 4.3.1 概念说明

在 Jupyter Notebook 里，如果一个对象的类提供了 `_repr_latex_` 方法，Notehub 会把该方法返回的 LaTeX 源码交给 MathJax 渲染成数学公式，作为该对象的「富文本表示」。numpy.polynomial 正是利用这个协议：在 Notebook 里对一个多项式求值，看到的不是一串 ASCII，而是一道排版漂亮的公式。

LaTeX 表示与字符串表示的设计目标不同：

- **字符串表示**（`__str__`）追求「在终端里也能看懂」，所以有 ascii 兜底。
- **LaTeX 表示**（`_repr_latex_`）追求「在富文本环境里最美观」，所以用 `\,`（细空格）、`{T}_{2}`（下标排版）、`\color{LightGray}`（零系数灰显）等数学排版命令。

#### 4.3.2 核心流程

`_repr_latex_` 的产出形如：

\[ x \mapsto 1.0\,{T}_{0}(x) + 2.0\,{T}_{1}(x) + 3.0\,{T}_{2}(x) \]

其中 `x \mapsto ...` 读作「\(x\) 映射到 ...」，表示这是一个函数。流程为：

1. 用 `mapparms()` 与 `_format_term` 构造「缩放后的自变量」LaTeX 片段（同 `_generate_string`，但用 `_repr_latex_scalar` 格式化系数）。
2. 逐项遍历系数，按正负号拼出 `+ 2.0` 或 `- 3.0`，并调用 `_repr_latex_term(i, ...)` 产出基函数的 LaTeX。
3. **零系数灰显**：若 `c == 0`，用 `\color{LightGray}{...}` 把该项包起来，视觉上「弱化」零项。
4. 各项用 `''` 拼接，整体包进 `$x \mapsto <body>$`。

其中 `_repr_latex_term` 是「单项生成函数」的 LaTeX 版，与 `_str_term_*` 平行：

- 通用版（有 `basis_name` 的家族）：产出 `{T}_{2}(x)`。
- `Polynomial` 重写版：产出 `x^{2}`，并对需要括号的缩放自变量加 `\left( ... \right)`。

#### 4.3.3 源码精读

先看单项 LaTeX 生成（通用版，被五大正交族继承）：

[_repr_latex_term：basis_name 用 LaTeX 下标排版](_polybase.py#L421-L428)

```python
@classmethod
def _repr_latex_term(cls, i, arg_str, needs_parens):
    if cls.basis_name is None:
        raise NotImplementedError(...)
    # since we always add parens, we don't care if the expression needs them
    return f"{{{cls.basis_name}}}_{{{i}}}({arg_str})"
```

注意 `i` 在这里是**整数**（不是字符串），因为 LaTeX 用 `_{{i}}` 自己处理排版，不需要预先翻译成下标字符。注释点出一个细节：通用版「总是加括号」`({arg_str})`，所以不关心 `needs_parens`；但 `Polynomial` 重写版会用到它。

系数的 LaTeX 格式化用一个静态方法，把数值包进 `\text{...}`：

[_repr_latex_scalar：用 \text{...} 包裹数值，避免触发数学模式格式化](_polybase.py#L430-L434)

```python
@staticmethod
def _repr_latex_scalar(x, parens=False):
    # TODO: we're stuck with disabling math formatting until we handle
    # exponents in this function
    return fr'\text{{{pu.format_float(x, parens=parens)}}}'
```

注释里的 TODO 说明：目前还没处理好指数的科学计数法 LaTeX 排版，所以暂用 `\text{...}` 把数值当纯文本显示，避免 `1e-08` 被 MathJax 误解析。

`Polynomial` 对 `_repr_latex_term` 的重写体现了幂基的特殊性：

[Polynomial._repr_latex_term：x^{i}，并对缩放自变量加 \left( \right)](polynomial.py#L1674-L1683)

```python
@staticmethod
def _repr_latex_term(i, arg_str, needs_parens):
    if needs_parens:
        arg_str = rf"\left({arg_str}\right)"
    if i == 0:
        return '1'
    elif i == 1:
        return arg_str
    else:
        return f"{arg_str}^{{{i}}}"
```

它返回 `1`、`x`、`x^{2}` 三种形态，并尊重 `needs_parens` 给缩放自变量加自适应大小的括号 `\left( ... \right)`。

最后是总装方法：

[_repr_latex_：逐项拼装 LaTeX，零系数灰显，整体包成 x \mapsto ...](_polybase.py#L455-L493)

```python
def _repr_latex_(self):
    off, scale = self.mapparms()
    term, needs_parens = self._format_term(self._repr_latex_scalar, off, scale)

    mute = r"\color{{LightGray}}{{{}}}".format

    parts = []
    for i, c in enumerate(self.coef):
        if i == 0:
            coef_str = f"{self._repr_latex_scalar(c)}"
        elif not isinstance(c, numbers.Real):
            coef_str = f" + ({self._repr_latex_scalar(c)})"   # 复数等加括号
        elif c >= 0:
            coef_str = f" + {self._repr_latex_scalar(c, parens=True)}"
        else:
            coef_str = f" - {self._repr_latex_scalar(-c, parens=True)}"

        term_str = self._repr_latex_term(i, term, needs_parens)
        if term_str == '1':                  # 常数项：不附加基函数
            part = coef_str
        else:
            part = rf"{coef_str}\,{term_str}"

        if c == 0:                           # 零系数灰显
            part = mute(part)

        parts.append(part)

    body = ''.join(parts) if parts else '0'
    return rf"${self.symbol} \mapsto {body}$"
```

要点：

- **常数项（`i==0`，`term_str=='1'`）**：只保留系数，不附加基函数——这呼应了「乘以 1 不写」的数学习惯。
- **零系数灰显**：`mute = r"\color{LightGray}{}".format`，给零项套上灰色，让读者一眼看出哪些项实际为零（这在拟合结果里尤其有用）。
- **`numbers.Real` 分支**：非实数（如复数系数）用括号包裹，避免符号与数值混淆。

#### 4.3.4 代码实践

**实践目标**：查看 `_repr_latex_` 的原始返回值，理解它为何能在 Jupyter 里渲染。

**操作步骤**（待本地验证）：

```python
import numpy as np
from numpy.polynomial import Chebyshev

c = Chebyshev([1, 0, 3])        # 中间项系数为 0
latex = c._repr_latex_()
print(latex)

# 对比：带零系数灰显的项
# 期望看到 0.0 那一项被 \color{LightGray}{...} 包裹
```

**需要观察的现象**：

- 返回值是纯字符串，以 `$` 开头、`$` 结尾，形如 `$x \mapsto 1.0\,{T}_{0}(x) + \color{LightGray}{0.0\,{T}_{1}(x)} + 3.0\,{T}_{2}(x)$`。
- 零系数项 `T_1` 被 `\color{LightGray}{...}` 包裹。

**预期结果**：终端打印出上述 LaTeX 源码字符串；若在 Jupyter 单元格里直接写 `c`（而非 `print`），则会看到渲染后的灰色零项公式。

**注**：若你想在 Markdown 里验证渲染，可把返回的字符串（去掉首尾 `$`）粘进支持 MathJax 的 Markdown 单元 `$ ... $` 之间。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `_repr_latex_scalar` 用 `\text{...}` 包裹数值，而不是直接输出 `1.0`？

**答案**：在 LaTeX 数学模式里，普通文本会被 MathJax 按数学符号规则排版（例如多个空格折叠、特殊字符有歧义）。用 `\text{...}` 把数值强制按纯文本显示，保证 `1.0`、`-3.0` 这样的字符串原样呈现，不被数学模式扭曲。

**练习 2**：常数项为什么在 `_repr_latex_` 里走 `term_str == '1'` 的特殊分支？

**答案**：因为 `_repr_latex_term(0, ...)` 返回 `'1'`（第 0 次基函数等于常数 1）。若不特判，常数项会变成 `1.0\,1` 这样无意义的拼接；特判后只保留系数 `1.0`，符合「乘以 1 省略」的数学惯例。

---

### 4.4 全局开关 set_default_printstyle 与优先级

#### 4.4.1 概念说明

`set_default_printstyle` 是 `numpy.polynomial` 顶层命名空间里的一个函数，用来**全局**切换所有多项式的默认打印风格。它解决的问题是：终端打印（`print(p)`）走的是 `__str__`，而 `__str__` 读 `_use_unicode` 这个共享类属性——于是「改一个属性，六大类全部跟着变」。

它定义在 `__init__.py` 里，而不是 `_polybase.py`，因为它是面向用户的「门面函数」，属于包级 API；但它真正干活的方式是回头去改 `ABCPolyBase._use_unicode`。

需要再次强调的优先级（已在 4.1 节给出，这里完整复述）：

1. **格式说明符**（`f"{p:unicode}"`、`f"{p:ascii}"`）——最高，临时、显式。
2. **`set_default_printstyle`** 设的全局值——次之，持久、隐式。
3. **平台默认**——最低，Unix 为 unicode、Windows 为 ascii。

#### 4.4.2 核心流程

`set_default_printstyle(style)`：

1. 校验 `style` 必须是 `'unicode'` 或 `'ascii'`，否则抛 `ValueError`。
2. 把 `style` 翻译成布尔值 `_use_unicode`（unicode→`True`，ascii→`False`）。
3. 延迟导入 `ABCPolyBase`（避免循环导入），把 `ABCPolyBase._use_unicode` 直接赋为新值。

由于 `_use_unicode` 是 `ABCPolyBase` 的类属性，而六大便捷类都继承自它且未各自遮蔽该属性，所以一次赋值就让所有实例的 `__str__` 在下次调用时读到新值。

#### 4.4.3 源码精读

[set_default_printstyle：校验 + 改写 ABCPolyBase._use_unicode](__init__.py#L172-L181)

```python
def set_default_printstyle(style):
    if style not in ('unicode', 'ascii'):
        raise ValueError(
            f"Unsupported format string '{style}'. Valid options are 'ascii' "
            f"and 'unicode'"
        )
    _use_unicode = True
    if style == 'ascii':
        _use_unicode = False
    from ._polybase import ABCPolyBase
    ABCPolyBase._use_unicode = _use_unicode
```

该函数定义于 [__init__.py:135-181](__init__.py#L135-L181)，上面是它的核心逻辑（[L172-L181](__init__.py#L172-L181)）。两个细节值得注意：

- **函数内延迟导入** `from ._polybase import ABCPolyBase`：`__init__.py` 在模块加载早期执行，此时 `_polybase` 可能尚未完全就绪；放在函数体内可规避循环导入问题。
- **赋值的是类属性**：`ABCPolyBase._use_unicode = _use_unicode`。因为 `__str__` 里写的是 `self._use_unicode`，而实例没有自己的 `_use_unicode`，Python 的属性查找会向上落到类属性上。所以改类属性即生效，无需遍历实例。

它的文档字符串给出了与 `f"{p:unicode}"` 的优先级对照（节选）：

[文档示例：先全局切换，再用格式说明符临时覆盖](__init__.py#L154-L170)

参见 [__init__.py:154-170](__init__.py#L154-L170)。该示例展示：`set_default_printstyle('unicode')` 后 `print(p)` 输出 unicode 版；再 `set_default_printstyle('ascii')` 后输出 ascii 版；但 `f"{p:unicode}"` 始终输出 unicode 版，「Formatting supersedes all class/package-level defaults」。

#### 4.4.4 代码实践（本讲综合实践之一）

**实践目标**：用 `set_default_printstyle` 切换全局风格，再用 `f"{p:ascii}"` 在不改动全局开关的前提下临时输出 ascii。这正是规格里指定的核心实践。

**操作步骤**（待本地验证）：

```python
import numpy as np
from numpy.polynomial import Chebyshev

c = Chebyshev([1, 2, 3])

# 1) 切到 unicode，打印
np.polynomial.set_default_printstyle('unicode')
print(c)              # 期望: 1.0 + 2.0·T₁(x) + 3.0·T₂(x)

# 2) 切到 ascii，打印
np.polynomial.set_default_printstyle('ascii')
print(c)              # 期望: 1.0 + 2.0 T_1(x) + 3.0 T_2(x)

# 3) 不动全局开关（仍是 ascii），临时用格式说明符输出 unicode
print(f"{c:unicode}")  # 期望: 1.0 + 2.0·T₁(x) + 3.0·T₂(x)

# 4) 验证全局开关没被格式说明符带偏
print(c)              # 期望仍是 ascii 版

# 5) 非法风格
try:
    np.polynomial.set_default_printstyle('latex')
except ValueError as e:
    print("ValueError:", e)
```

**需要观察的现象**：

- 步骤 1/2：全局开关直接影响 `print(c)` 的输出风格。
- 步骤 3：`f"{c:unicode}"` 在全局为 ascii 时仍输出 unicode。
- 步骤 4：步骤 3 的格式化是「一次性」的，不改变全局开关，`print(c)` 仍是 ascii。
- 步骤 5：非法风格抛 `ValueError`，消息列出合法选项。

**预期结果**：如上「期望」注释所示。

#### 4.4.5 小练习与答案

**练习 1**：`set_default_printstyle('ascii')` 之后，`f"{c:unicode}"` 的输出会变成 ascii 吗？

**答案**：不会。`__format__` 完全不读 `_use_unicode`，而是直接根据说明符 `'unicode'` 调用 `_str_term_unicode`。格式说明符的优先级高于全局开关。

**练习 2**：为什么 `set_default_printstyle` 要在函数体内 `from ._polybase import ABCPolyBase`，而不是在 `__init__.py` 顶部导入？

**答案**：为了避免循环导入。`__init__.py` 在包加载时执行，顶部导入 `_polybase` 可能与其自身的初始化顺序冲突；把导入放进函数体，推迟到 `set_default_printstyle` 被实际调用时（此时整个包早已加载完毕），就规避了这个问题。

---

## 5. 综合实践

把本讲四块内容串起来，完成一个小小的「打印风格探测器」：

**任务**：编写一个函数 `show(p)`，对任意多项式 `p` 同时输出四种表示，并标注每种走的是哪条路径：

```python
import numpy as np
from numpy.polynomial import Polynomial, Chebyshev

def show(p):
    print("=" * 50)
    print("repr   (__repr__) :", repr(p))
    print("str    (__str__)  :", str(p))                 # 受全局开关影响
    print("format ascii     :", f"{p:ascii}")            # 强制 ascii
    print("format unicode   :", f"{p:unicode}")          # 强制 unicode
    print("latex            :", p._repr_latex_())        # Jupyter 富文本源码
    print("=" * 50)

# 实验 1：标准幂基
show(Polynomial([1, -2, 0, 3]))

# 实验 2：Chebyshev，并切换全局开关，观察 str 行的变化
np.polynomial.set_default_printstyle('ascii')
show(Chebyshev([1, 2, 3]))
np.polynomial.set_default_printstyle('unicode')
show(Chebyshev([1, 2, 3]))   # 这次 str 行应与 format unicode 行一致
```

**验收要点**：

1. `repr` 行始终是可重建的构造调用，与全局开关无关。
2. `str` 行随 `set_default_printstyle` 改变；两次 `show(Chebyshev(...))` 的 `str` 行不同。
3. `format ascii` 与 `format unicode` 两行**永远**各自固定，不受全局开关影响。
4. `latex` 行以 `$x \mapsto ...$` 开头；含零系数的 `Polynomial([1, -2, 0, 3])` 中，`0` 那一项被 `\color{LightGray}{...}` 包裹。

完成后再思考：如果把 `linewidth` 调小（`np.set_printoptions(linewidth=20)`），`_generate_string` 的逐项换行会如何体现在 `str(p)` 上？（提示：回到 [4.2.3](_polybase.py#L348-L392) 的换行算法对照。）

## 6. 本讲小结

- 打印子系统有**三个入口**：`__repr__` 产出可重建的构造调用；`__str__` 产出人类可读的数学表达式；`__format__` 用 `'ascii'`/`'unicode'` 说明符临时指定风格。
- 优先级为：**格式说明符 > `set_default_printstyle` 全局开关 > 平台默认**（Unix unicode、Windows ascii）。
- 真正的拼装引擎是 [`_generate_string`](_polybase.py#L348-L392)，它把「系数+正负号」通用部分与「基函数」特化部分（由 `term_method` 提供）分离，体现「父类管流程、子类管算法」。
- 双风格靠两张 [`str.maketrans` 映射表](_polybase.py#L79-L103) 实现：unicode 用上下标字符（`x²`、`T₂`），ascii 用 `**` 与 `_`（`x**2`、`T_2`），两位数幂次也能整体翻译。
- `Polynomial` 是特例（`basis_name = None`），重写了三个单项生成函数，让变量自己带幂（`x²`），而非用字母下标。
- [`_repr_latex_`](_polybase.py#L455-L493) 产出 `$x \mapsto ...$` 的 LaTeX 源码供 Jupyter 渲染，零系数灰显，常数项省略基函数。
- `set_default_printstyle` 的实现本质是**改写类属性 `ABCPolyBase._use_unicode`**，因类属性被所有实例共享而一次生效。

## 7. 下一步学习建议

- **横向巩固**：阅读 [chebyshev.py:2053](chebyshev.py#L2053)（`basis_name = 'T'`）及其它四族的 `basis_name`（`P`/`L`/`H`/`He`），对照本讲理解它们如何复用通用的 `_str_term_*` 与 `_repr_latex_term`，从而「零额外代码」获得正确的打印。
- **纵向深入**：本讲刻意轻描淡写了 `_generate_string` 里 `mapparms` + `_format_term` 产生的「缩放自变量」（`off + scale·x`）。这部分与 domain/window 线性映射紧密相关，将在 **u5-l2 打印系统深入与 symbol 自定义** 中专题展开，包括括号处理、`linewidth` 换行细节与 `symbol` 标识符校验。
- **二次开发预热**：若你想自定义一种新的多项式基，本讲提示了「要么设 `basis_name`，要么重写 `_str_term_unicode`/`_str_term_ascii`/`_repr_latex_term`」的契约——这与 **u5-l4 测试体系与基于 ABCPolyBase 的二次开发** 直接衔接。
```
