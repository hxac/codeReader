# 运算符重载与方法委托

## 1. 本讲目标

本讲承接上一讲（u3-l1）对 `chararray` 这个 `ndarray` 子类的拆解，聚焦它**最像 Python 字符串**的一面：当你对两个 `chararray` 写 `==`、`+`、`*`、`%`，或者调用 `c.upper()`、`c.find(...)` 时，背后到底发生了什么。

学完后你应当能够：

- 说清 `chararray` 的六个比较运算符（`__eq__`/`__ne__`/`__ge__`/`__le__`/`__gt__`/`__lt__`）如何一行委托给本模块的 `equal`/`not_equal`/… 自由函数，以及为何它们**不**需要包裹返回值。
- 说清三个算术运算符（`__add__`/`__mul__`/`__mod__`）的元素级字符串语义，以及 `__mul__`/`__mod__` 为何要多套一层 `asarray(...)`。
- 看懂「方法委托」这一设计：`capitalize`/`center`/`find`/`upper` 等几十个方法几乎只是自由函数的转发，并理解源码顶部 `IMPLEMENTATION NOTE` 给出的「返回字符串的方法要包回 `chararray`」规则。
- 动手验证 `*` 乘非整数抛 `ValueError`、反向 `%` 抛 `TypeError` 等边界行为。

## 2. 前置知识

在进入源码前，先建立三个直觉。

**直觉一：`chararray` 是「会自动剥空白 + 自带字符串方法」的 ndarray 子类。**
普通 `str_`/`bytes_` 数组只有数据，没有方法；`chararray` 在其之上加了两层糖衣——取值时自动 `rstrip`（见 u3-l1 的 `__getitem__`），以及本讲要讲的运算符重载与方法委托。

**直觉二：Python 的运算符最终都会落到「双下划线方法」上。**
写 `a == b` 等价于调用 `type(a).__eq__(a, b)`；写 `a + b` 等价于 `type(a).__add__(a, b)`。所以「重载运算符」就是「在类里定义这些 `__xxx__` 方法」。`chararray` 正是这么做的：它把这些方法写成对本模块**自由函数**的一行调用。

**直觉三：「自由函数」与「方法」是同一套逻辑的两个入口。**
`np.char.upper(c)`（自由函数）和 `c.upper()`（方法）做的是同一件事。方法的实现往往就是 `return asarray(upper(self))`——把「自己」交给自由函数处理，再把结果包回 `chararray`。这种「方法委托给自由函数」的设计，让 NumPy 不必在两处重复实现字符串逻辑。

> 名词速查：
> - **富比较（rich comparison）**：Python 的六个比较运算符 `== != >= <= > <`，对应六个双下划线方法。
> - **元素级（element-wise）**：对数组每个元素独立套用同一操作，返回同形状数组。
> - **`__array_wrap__`**：ndarray 子类的钩子，ufunc 执行完毕后被调用，决定如何「包装」输出。u3-l1 已讲过 chararray 用它把字符串结果 `view` 回 chararray。

## 3. 本讲源码地图

本讲几乎全部锚定在一个文件上：

| 文件 | 作用 |
| --- | --- |
| [numpy/_core/defchararray.py](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/char/../_core/defchararray.py) | `chararray` 类、六个比较自由函数、`multiply`/`mod` 等自由函数、`array`/`asarray` 工厂的唯一实现。本讲的运算符与方法都在此文件的 `class chararray` 内。 |
| [numpy/_core/tests/test_defchararray.py](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/char/../_core/tests/test_defchararray.py) | 对应测试。本讲用 `TestComparisons`、`TestOperations` 两个测试类来佐证运算符行为，并借用 `ignore_charray_deprecation` 标记。 |

> 提醒：`numpy.char` 只是门面（见 u1-l1、u2-l1），它通过模块级 `__getattr__` 把这些名字转发给 `numpy._core.defchararray`。所以下文说的「本模块的自由函数」，在用户视角就是 `np.char.xxx`。

## 4. 核心概念与源码讲解

### 4.1 比较运算符的重载与委托

#### 4.1.1 概念说明

对两个普通 `str_` 数组写 `a == b`，走的是 NumPy 原生的逐元素比较，**不**剥空白。但对 `chararray` 写 `c1 == c2`，你会得到「先剥掉尾部空白再比较」的结果——这是为了兼容已停更的 numarray 而保留的历史语义（详见 u2-l3）。

这套语义并不是在运算符里现写的，而是通过**两层委托**实现：

```
c1 == c2
   │  Python 翻译为
   ▼
chararray.__eq__(c1, c2)
   │  方法体一行转发
   ▼
equal(c1, c2)                 ← 本模块的自由函数
   │
   ▼
compare_chararrays(c1, c2, '==', True)   ← C 层，rstrip=True
```

也就是说，**运算符 → 方法 → 自由函数 → C 层**，每一层都只做一件事。`chararray` 的六个比较运算符只是六个一行函数。

#### 4.1.2 核心流程

六个比较运算符与自由函数的对应关系是固定模板，只差函数名：

| 运算符 | 方法 | 委托给 | 比较码 |
| --- | --- | --- | --- |
| `==` | `__eq__` | `equal` | `'=='` |
| `!=` | `__ne__` | `not_equal` | `'!='` |
| `>=` | `__ge__` | `greater_equal` | `'>='` |
| `<=` | `__le__` | `less_equal` | `'<='` |
| `>`  | `__gt__` | `greater` | `'>'` |
| `<`  | `__lt__` | `less` | `'<'` |

注意返回类型：比较的结果是**布尔数组**（`dtype=bool`），不是 `chararray`。这一点决定了它们**不需要**（也不能）用 `asarray(...)` 包裹——4.3 节会解释原因。

#### 4.1.3 源码精读

先看六个运算符方法本身，它们集中在一起，结构完全一致：

[numpy/_core/defchararray.py:606-664](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/char/../_core/defchararray.py#L606-L664) —— `chararray` 的六个比较运算符，每个方法体只有一行 `return xxx(self, other)`，把工作整体交给同名自由函数：

```python
def __eq__(self, other):
    return equal(self, other)
def __ne__(self, other):
    return not_equal(self, other)
def __ge__(self, other):
    return greater_equal(self, other)
def __le__(self, other):
    return less_equal(self, other)
def __gt__(self, other):
    return greater(self, other)
def __lt__(self, other):
    return less(self, other)
```

再看被委托的自由函数长什么样。以 `equal` 为例：

[numpy/_core/defchararray.py:61-92](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/char/../_core/defchararray.py#L61-L92) —— `equal` 被 `@array_function_dispatch(_binary_op_dispatcher)` 装饰（支持 NEP-18，见 u2-l4），函数体也只有一行，调用 C 层 `compare_chararrays` 并固定传 `rstrip=True`：

```python
@array_function_dispatch(_binary_op_dispatcher)
def equal(x1, x2):
    ...
    return compare_chararrays(x1, x2, '==', True)
```

其余五个自由函数（`not_equal`/`greater_equal`/`less_equal`/`greater`/`less`）位于 [numpy/_core/defchararray.py:95-263](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/char/../_core/defchararray.py#L95-L263)，结构与 `equal` 完全相同，唯一的差别是比较码字符串（`'!='`、`'>='`、`'<='`、`'>'`、`'<'`）。这就是 u2-l3 总结过的「六函数同构」模式。

最后用测试佐证「先剥尾部空白再比较」的行为：

[numpy/_core/tests/test_defchararray.py:143-156](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/char/../_core/tests/test_defchararray.py#L143-L156) —— `TestWhitespace` 构造带尾部空白的 `A` 与不带空白的 `B`，断言 `A == B` 全为 `True`、`A != B` 全为 `False`，直接验证运算符确实走了「剥空白」的 `equal`：

```python
A = np.array([['abc ', '123  '], ['789 ', 'xyz ']]).view(np.char.chararray)
B = np.array([['abc', '123'],   ['789', 'xyz']]).view(np.char.chararray)
assert_(np.all(A == B))      # 走 __eq__ → equal(..., rstrip=True)
assert_(not np.any(A != B))
```

#### 4.1.4 代码实践

**实践目标**：亲手确认 `chararray` 的 `==` 走的是「剥空白」路径，而普通 `str_` 数组的 `==` 不剥。

**操作步骤**（示例代码，待本地验证）：

```python
import warnings
warnings.simplefilter("ignore", DeprecationWarning)  # chararray 在 2.5 已弃用

import numpy as np

# 用 .view(chararray) 把普通 str_ 数组“升级”成 chararray（与测试同款写法）
c = np.array(['aa', 'bb']).view(np.char.chararray)
d = np.array(['aa ', 'bb']).view(np.char.chararray)   # 注意 d 带尾部空格

print('chararray  == :', c == d)                       # 走 __eq__ → equal → rstrip
print('plain str_ == :', np.array(['aa', 'bb']) == np.array(['aa ', 'bb']))
```

**需要观察的现象**：
- 第一行 `chararray ==` 应输出 `[True True]`（剥了空白）。
- 第二行普通 `str_` 比较应输出 `[False True]`（`'aa' != 'aa '`，不剥空白）。

**预期结果**：两组结果不同，证明 `chararray` 的 `==` 委托给了带 `rstrip=True` 的 `equal`。可与 `test_defchararray.py::TestWhitespace` 对照。

#### 4.1.5 小练习与答案

**练习 1**：若把 `__eq__` 改成 `return compare_chararrays(self, other, '==', False)`（关掉 rstrip），`TestWhitespace` 里的 `assert_(np.all(A == B))` 还会通过吗？

> **答案**：不会。关闭 rstrip 后 `'abc ' == 'abc'` 为 `False`，断言失败。这反向印证了 `True` 这个参数才是兼容 numarray 行为的关键。

**练习 2**：`chararray.__eq__` 返回的是 `chararray` 还是普通 `ndarray`？为什么它不像 `__mul__` 那样套 `asarray(...)`？

> **答案**：返回普通布尔 `ndarray`。因为比较结果是 `bool` dtype，**不是**字符串；若强行用 `asarray(...)` 包裹，会触发 `chararray.__array_finalize__` 的「只能由字符串数据构造」校验（见 u3-l1）而抛 `ValueError`。所以比较运算符刻意不包裹。

---

### 4.2 算术运算符的元素级字符串语义

#### 4.2.1 概念说明

`chararray` 把 `+`、`*`、`%` 三个算术运算符重载成了**元素级的字符串操作**，语义与 Python 原生 `str` 完全一致，只是作用在每个数组元素上：

- `a + b`：逐元素拼接，\( w_{\text{out}} = w_a + w_b \)（输出宽度等于两边宽度之和）。
- `a * i`：逐元素重复，\( w_{\text{out}} = w_a \times i \)（`i` 为整数；负数按 0 处理）。
- `a % b`：逐元素做旧式 `%` 字符串格式化（`"%d" % 3 → "3"`）。

和比较运算符一样，它们也委托给自由函数 `add`/`multiply`/`mod`。但有一个**关键不对称**：`__add__` 直接返回自由函数的结果，而 `__mul__`/`__mod__` 多套了一层 `asarray(...)`。理解这个不对称是本节的核心。

#### 4.2.2 核心流程

```
c1 + c2  →  __add__ → add(c1, c2)                    ← 直接返回（已是 chararray）
c * i    →  __mul__  → asarray( multiply(c, i) )     ← 多套一层 asarray
c % i    →  __mod__  → asarray( mod(c, i) )          ← 多套一层 asarray
```

为何如此？因为 `add` 是一个**核心 ufunc**（`np.strings.add` 即 `np.add`），当它作用于 `chararray` 时，NumPy 的 ufunc 协议会自动调用 `chararray.__array_wrap__`，把字符串输出 `view` 回 `chararray`（见 u3-l1）。所以 `__add__` 拿到的结果**已经是 chararray**，无需再包。

而 `multiply` 是本模块的**本地薄包装**（一个普通 Python 函数，见 u2-l5），`mod` 来自 `numpy.strings`。为了让调用方**稳定**地拿到 `chararray`（无论内层调用是否触发 `__array_wrap__`），作者用模块自己的 `asarray(...)` 显式地把字符串结果「重新认领」为 `chararray`。这正是下一节 `IMPLEMENTATION NOTE` 所定的规矩。

#### 4.2.3 源码精读

先看三个算术运算符：

[numpy/_core/defchararray.py:666-723](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/char/../_core/defchararray.py#L666-L723) —— `__add__`/`__radd__` 直接返回 `add(...)`；`__mul__`/`__rmul__` 与 `__mod__` 则套了 `asarray(...)`；`__rmod__` 直接返回 `NotImplemented`：

```python
def __add__(self, other):
    return add(self, other)
def __radd__(self, other):
    return add(other, self)          # 反向：把 other 放前面
def __mul__(self, i):
    return asarray(multiply(self, i))
def __rmul__(self, i):
    return asarray(multiply(self, i))
def __mod__(self, i):
    return asarray(mod(self, i))
def __rmod__(self, other):
    return NotImplemented            # 反向 % 不支持 → 交给 Python 抛 TypeError
```

再看被委托的 `multiply`。它是**本地定义**（不是 `np.strings.multiply` 的纯再导出），价值在于把乘非整数时的 `TypeError` 翻译成 chararray 历史上的 `ValueError`：

[numpy/_core/defchararray.py:312-315](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/char/../_core/defchararray.py#L312-L315) —— `multiply` 的函数体，`try/except` 把底层 `TypeError` 转成 `ValueError`（详见 u2-l5）：

```python
try:
    return strings_multiply(a, i)
except TypeError:
    raise ValueError("Can only multiply by integers")
```

`__rmod__` 返回 `NotImplemented` 的后果，由测试清楚标出：

[numpy/_core/tests/test_defchararray.py:782-785](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/char/../_core/tests/test_defchararray.py#L782-L785) —— 反向 `%`（如 `42 % chararray`）：`int` 那边不会处理 `chararray`，`chararray.__rmod__` 又返回 `NotImplemented`，Python 于是抛 `TypeError`：

```python
for ob in [42, object()]:
    with assert_raises_regex(
            TypeError, "unsupported operand type.* and 'chararray'"):
        ob % A
```

正向 `*` 乘非整数的 `ValueError` 同样有测试兜底：

[numpy/_core/tests/test_defchararray.py:743-746](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/char/../_core/tests/test_defchararray.py#L743-L746) —— `A * object()` 与 `A * 'qrs'` 都应抛 `ValueError("Can only multiply by integers")`：

```python
for ob in [object(), 'qrs']:
    with assert_raises_regex(ValueError, 'Can only multiply by integers'):
        A * ob
```

最后看 `%` 的元素级语义样例：

[numpy/_core/tests/test_defchararray.py:760-766](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/char/../_core/tests/test_defchararray.py#L760-L766) —— `F % C` 对格式串数组逐元素做 `%` 格式化：

```python
F = np.array([['%d', '%f'], ['%s', '%r']]).view(np.char.chararray)
C = np.array([[3, 7], [19, 1]], dtype=np.int64)
FC = np.array([['3', '7.000000'], ['19', 'np.int64(1)']]).view(np.char.chararray)
assert_array_equal(FC, F % C)
```

#### 4.2.4 代码实践

**实践目标**：分别触发 `+`/`*`/`%` 三条路径，记录每个运算符实际调用的自由函数，并验证 `*` 的 `ValueError` 与反向 `%` 的 `TypeError`。

**操作步骤**（示例代码，待本地验证）：

```python
import warnings
warnings.simplefilter("ignore", DeprecationWarning)
import numpy as np

A = np.array([['abc', '123'], ['789', 'xyz']]).view(np.char.chararray)
B = np.array([['efg', '456'], ['051', 'tuv']]).view(np.char.chararray)

# 1) + : __add__ → add            （宽度求和：3+3=6）
print('+  →', A + B)

# 2) * : __mul__ → asarray(multiply(self, i))   （宽度 = 3*2 = 6）
print('*  →', A * 2)

# 3) % : __mod__ → asarray(mod(self, i))
F = np.array(['%d', '%s']).view(np.char.chararray)
print('%  →', F % np.array([3, 'hi']))

# 4) 边界：* 乘非整数 → ValueError（来自本地 multiply 的翻译）
for bad in (object(), 'qrs', 1.5):
    try:
        _ = A * bad
    except ValueError as e:
        print('*  ValueError:', e)

# 5) 边界：反向 % → __rmod__ 返回 NotImplemented → Python 抛 TypeError
try:
    _ = 42 % A
except TypeError as e:
    print('%  TypeError :', e)
```

**需要观察的现象**：
- `+` 与 `*` 的结果都是 `chararray`（repr 显示 `chararray(...)`），且宽度符合 \( w_a+w_b \) 与 \( w_a\times i \)。
- `*` 乘 `object()`/`'qrs'` 抛 `ValueError: Can only multiply by integers`。
- `42 % A` 抛 `TypeError: unsupported operand type(s) for %: ... and 'chararray'`。

**预期结果**：与 `test_defchararray.py::TestOperations` 的 `test_add`/`test_mul`/`test_mod`/`test_rmod` 完全一致。

> 如何「记录每个运算符调用的自由函数」？最可靠的方式是**读源码**：每个 `__xxx__` 的函数体只有一行 `return <自由函数>(...)`，且其 docstring 的 `See Also` 也指向同一自由函数。你也可以临时 `print(chararray.__add__.__doc__)` 看到 `See Also: add`。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `__add__` 写 `return add(self, other)`，而 `__mul__` 要写 `return asarray(multiply(self, i))`？

> **答案**：`add` 是核心 ufunc，作用于 `chararray` 时会自动触发 `__array_wrap__` 把字符串结果 `view` 回 `chararray`，故结果已是 `chararray`；而 `multiply` 是本模块的普通 Python 包装函数，作者用 `asarray(...)` 显式保证返回类型稳定为 `chararray`（见 4.3 的 `IMPLEMENTATION NOTE`）。

**练习 2**：`A * 1.5` 会抛 `TypeError` 还是 `ValueError`？依据是哪一行源码？

> **答案**：抛 `ValueError("Can only multiply by integers")`。依据是 [numpy/_core/defchararray.py:312-315](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/char/../_core/defchararray.py#L312-L315)：底层 `strings_multiply` 对非整数抛 `TypeError`，被本地 `multiply` 的 `except TypeError` 翻译成 `ValueError`。

---

### 4.3 方法委托与 `asarray(...)` 包裹策略

#### 4.3.1 概念说明

`chararray` 上挂着几十个字符串方法（`upper`/`lower`/`capitalize`/`center`/`find`/`count`/`replace`/…）。这些方法**几乎没有自己的逻辑**——它们把 `self` 交给本模块同名（或相近）的自由函数，再把结果交还。这就是「方法委托（delegation）」。

但「交还结果」这一步有个必须想清楚的问题：**返回类型该是什么？**

- 若返回的是**字符串数组**：调用方期望拿回一个 `chararray`（否则链式调用 `c.upper().center(...)` 就断了）。
- 若返回的是**整数或布尔数组**（如 `find` 的索引、`startswith` 的真假）：绝不能包成 `chararray`，否则会撞上 `__array_finalize__` 的「只能由字符串数据构造」校验。

源码顶部有一段 `IMPLEMENTATION NOTE` 把这条规矩写得明明白白，是理解整个 `chararray` 类的钥匙。

#### 4.3.2 核心流程

按返回类型，方法分两类，包裹策略相反：

```
返回字符串的方法（upper/center/capitalize/...）
   → asarray( 自由函数(self, ...) )     ← 包回 chararray
返回 int/bool 的方法（find/count/startswith/...）
   → 自由函数(self, ...)                ← 直接返回，不包
```

`asarray` 在这里的作用不是「拷贝」，而是「**重新认定类型**」：它最终走到 `array(obj, copy=False)`，对一个已经是字符串 dtype 的 `ndarray` 做 `.view(chararray)`（详见 u3-l3），把普通字符串数组「升级」成 `chararray`，且尽量不复制数据。

#### 4.3.3 源码精读

先读那段总纲：

[numpy/_core/defchararray.py:601-604](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/char/../_core/defchararray.py#L601-L604) —— `IMPLEMENTATION NOTE`：大多数方法直接委托自由函数；但「返回字符串数组」的方法必须返回 `chararray`，因此需要额外包裹：

```python
# IMPLEMENTATION NOTE: Most of the methods of this class are
# direct delegations to the free functions in this module.
# However, those that return an array of strings should instead
# return a chararray, so some extra wrapping is required.
```

**第一类：返回字符串 → 套 `asarray(...)`**。以 `capitalize`/`center`/`upper` 为例：

[numpy/_core/defchararray.py:749-770](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/char/../_core/defchararray.py#L749-L770) —— `capitalize`、`center` 都用 `asarray(...)` 包裹自由函数的返回，确保结果是 `chararray`：

```python
def capitalize(self):
    return asarray(capitalize(self))
def center(self, width, fillchar=' '):
    return asarray(center(self, width, fillchar))
```

[numpy/_core/defchararray.py:1171-1181](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/char/../_core/defchararray.py#L1171-L1181) —— `upper` 同理：`return asarray(upper(self))`。

**第二类：返回 int/bool → 不包裹**。以 `count`/`find` 为例：

[numpy/_core/defchararray.py:772-782](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/char/../_core/defchararray.py#L772-L782) —— `count` 直接返回 `count(self, ...)`（整数数组），不加 `asarray`：

```python
def count(self, sub, start=0, end=None):
    return count(self, sub, start, end)
```

[numpy/_core/defchararray.py:830-840](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/char/../_core/defchararray.py#L830-L840) —— `find` 直接返回 `find(self, ...)`（索引数组），同样不包。

**为何 int/bool 不能包？** 因为 `asarray` 对非字符串 ndarray 最终会尝试构造 `chararray`，触发 u3-l1 讲过的 `__array_finalize__` 校验：

[numpy/_core/defchararray.py:590-593](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/char/../_core/defchararray.py#L590-L593) —— 只接受字符串 dtype，否则抛 `ValueError`：

```python
def __array_finalize__(self, obj):
    if self.dtype.char not in 'VSUbc':
        raise ValueError("Can only create a chararray from string data.")
```

所以「不包裹」不是疏忽，而是**必须**：把整数/布尔结果包成 `chararray` 会直接抛错。

最后看 `asarray` 自身——它正是「重新认定类型」的实现：

[numpy/_core/defchararray.py:1367-1429](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/char/../_core/defchararray.py#L1367-L1429) —— `asarray` 只是 `array(obj, copy=False)` 的别名，尽量不拷贝地把输入「认定」为 `chararray`：

```python
def asarray(obj, itemsize=None, unicode=None, order=None):
    ...
    return array(obj, itemsize, copy=False, unicode=unicode, order=order)
```

> 旁注：少数字符串方法（如 `replace`、`lstrip`、`rstrip`、`strip`、`decode`、`join`）**没有**显式套 `asarray`，它们依赖底层 ufunc 在作用于 `chararray` 时经 `__array_wrap__` 自动 `view` 回 `chararray`——这与 `__add__` 不套 `asarray` 是同一个道理。无论走哪条路，对外契约都是「返回 `chararray`」。

#### 4.3.4 代码实践

**实践目标**：用 `type()` 直接观察「返回字符串的方法」与「返回 int/bool 的方法」在类型上的差异，并验证字符串方法的结果确实是 `chararray`，因而可以链式调用。

**操作步骤**（示例代码，待本地验证）：

```python
import warnings
warnings.simplefilter("ignore", DeprecationWarning)
import numpy as np

c = np.array(['abc', 'xy']).view(np.char.chararray)

# 返回字符串 → 被 asarray 包回 chararray
print('upper      type:', type(c.upper()).__name__)        # chararray
print('center     type:', type(c.center(6)).__name__)      # chararray

# 返回 int/bool → 不包，是普通 ndarray
print('find       type:', type(c.find('b')).__name__)      # ndarray
print('count      type:', type(c.count('a')).__name__)     # ndarray
print('isalpha    type:', type(c.isalpha()).__name__)      # ndarray

# 因为 upper() 仍是 chararray，所以能继续链式调用 chararray 方法：
print('链式       :', c.upper().center(6))                 # chararray → chararray
```

**需要观察的现象**：
- `upper`/`center` 的结果 `type` 为 `chararray`。
- `find`/`count`/`isalpha` 的结果 `type` 为 `ndarray`（且 dtype 分别为 int/int/bool）。
- 链式 `c.upper().center(6)` 正常工作——正因为每一步返回的都是 `chararray`。

**预期结果**：与源码 `IMPLEMENTATION NOTE` 的规则一一吻合；若把 `find` 的返回强行 `.center(...)`，会因 `ndarray` 无 `center` 方法而抛 `AttributeError`，反向印证「字符串方法只挂在 `chararray` 上」。

#### 4.3.5 小练习与答案

**练习 1**：假如把 `def upper(self): return asarray(upper(self))` 改成 `return upper(self)`（去掉 `asarray`），`c.upper()` 还能链式调用 `.center(...)` 吗？

> **答案**：通常**仍能**，因为 `upper` 是 ufunc，作用于 `chararray` 时经 `__array_wrap__` 会把结果 `view` 回 `chararray`。但这是「碰巧」成立——`IMPLEMENTATION NOTE` 用 `asarray` 是为了**显式保证**契约，不依赖 ufunc 的自动包装行为，代码更健壮、意图更清晰。

**练习 2**：为什么 `find` 方法不写成 `return asarray(find(self, ...))`？

> **答案**：`find` 返回整数索引数组。`asarray` 会尝试把它构造成 `chararray`，命中 `__array_finalize__` 的 `dtype.char not in 'VSUbc'` 校验而抛 `ValueError`。所以返回非字符串的方法**必须**直接返回、不包裹。

---

## 5. 综合实践

把本讲的「比较运算符 + 算术运算符 + 方法委托」串起来，完成一个小任务：用 `chararray` 实现一段「数据清洗 + 拼接 + 格式化」的迷你流水线，并对照源码解释每一步的类型与自由函数。

**任务**：给定带尾部空白的名字数组，先比较去重，再拼接问候语，再做 `%` 格式化输出。

**操作步骤**（示例代码，待本地验证）：

```python
import warnings
warnings.simplefilter("ignore", DeprecationWarning)
import numpy as np

names = np.array(['alice ', 'bob', 'carol  ']).view(np.char.chararray)
ref   = np.array(['alice',  'bob', 'carol']).view(np.char.chararray)

# 步骤 1：比较（__eq__ → equal，剥空白），用于核对数据
print('相等？', names == ref)                       # [ True  True  True]

# 步骤 2：算术 +（__add__ → add）拼接前缀
greeting = 'Hi, ' + names                           # __radd__ → add('Hi, ', names)
print('拼接 :', greeting)                           # chararray

# 步骤 3：方法委托 + 链式（upper → asarray(upper)；center → asarray(center)）
pretty = greeting.strip().upper().center(14)
print('美化 :', pretty)

# 步骤 4：% 格式化（__mod__ → asarray(mod)）
fmt = np.array(['名字: %-10s']).view(np.char.chararray)
print('格式 :', fmt % ref)
```

**需要观察与记录的**（填表）：

| 步骤 | 用到的运算符/方法 | 实际委托的自由函数 | 返回类型 |
| --- | --- | --- | --- |
| 1 | `==` | `equal`（`compare_chararrays(..., True)`） | `ndarray`(bool) |
| 2 | `+`（反向） | `add` | `chararray` |
| 3 | `.strip().upper().center(...)` | `strip`→`upper`→`center`（均经 `asarray`/`__array_wrap__`） | `chararray` |
| 4 | `%` | `mod`（经 `asarray`） | `chararray` |

**预期结果**：整条流水线不报错，输出对齐的字符串；类型列与上表一致。若某一步 `type` 不符，回头对照 4.1–4.3 的源码定位原因。

## 6. 本讲小结

- `chararray` 把六个比较运算符 `__eq__`/`__ne__`/`__ge__`/`__le__`/`__gt__`/`__lt__` 全部写成一行，分别委托给 `equal`/`not_equal`/`greater_equal`/`less_equal`/`greater`/`less`，最终落到 C 层 `compare_chararrays(..., rstrip=True)`，因此比较**先剥尾部空白**，返回布尔 `ndarray`。
- 算术运算符 `+`/`*`/`%` 是元素级字符串操作：拼接、重复、旧式 `%` 格式化；宽度满足 \( w_{\text{out}}=w_a+w_b \)（加）与 \( w_{\text{out}}=w_a\times i \)（乘）。
- `__add__` 直接返回（`add` 是 ufunc，靠 `__array_wrap__` 自动 `view` 回 `chararray`）；`__mul__`/`__mod__` 则显式套 `asarray(...)` 以**稳定**返回 `chararray`。`__rmod__` 返回 `NotImplemented`，使反向 `%` 抛 `TypeError`。
- 几十个字符串方法采用「方法委托」设计：把 `self` 交给本模块自由函数处理。
- 源码顶部 `IMPLEMENTATION NOTE` 定下规矩——**返回字符串数组的方法要包回 `chararray`**（用 `asarray(...)`），而返回 int/bool 的方法（`find`/`count`/`isalpha`…）**刻意不包**，否则会撞 `__array_finalize__` 的字符串 dtype 校验。
- `asarray` 在这里的作用是「重新认定类型」而非复制：它对一个字符串 `ndarray` 做 `.view(chararray)`，使链式调用（`c.upper().center(...)`）成为可能。

## 7. 下一步学习建议

- 下一讲 **u3-l3（array / asarray 工厂函数源码精读）** 会深入 `asarray`/`array` 对 `str`/`bytes`/`list`/`object ndarray` 等各种输入的分支处理与 `itemsize` 自动推断，正好补全本讲反复出现的 `asarray(...)` 的内部细节。
- 若想彻底搞清「为何 `add` 不用包、`multiply` 要包」，建议顺带阅读 `numpy/_core/overrides.py` 中 ufunc 与 `__array_function__` 协议的关系（u2-l4 已铺垫）。
- 阅读建议：把本讲的 [numpy/_core/defchararray.py:601-723](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/char/../_core/defchararray.py#L601-L723) 这一百多行连起来通读一遍，你会看到 `chararray` 类「运算符 + 方法 + 委托 + 包裹」的整体骨架，是理解整个模块最划算的一段源码。
