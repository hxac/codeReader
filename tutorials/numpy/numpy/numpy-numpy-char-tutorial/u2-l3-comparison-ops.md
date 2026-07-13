# 字符串比较运算符与 compare_chararrays

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 `numpy.char` 里六个比较函数 `equal` / `not_equal` / `greater_equal` / `less_equal` / `greater` / `less` 的**统一实现模式**——它们的函数体都只有一行，全部委托给同一个 C 层入口 `compare_chararrays`；
- 解释为什么 `np.char.equal('aa', 'aa ')` 会返回 `True`，而 `numpy.equal` 对同样的输入返回 `False`——也就是 char 版独有的「**先剥离尾部空白再比较**」语义；
- 读懂 C 层 `compare_chararrays` 的参数（`a1, a2, cmp, rstrip`）和它支持的六种比较码（`'=='`、`'!='`、`'>='`、`'<='`、`'>'`、`'<'`），并理解它如何把这些字符串比较码映射成 Python 内部的富比较常量 `Py_EQ` / `Py_GT` 等；
- 理解「剥离尾部空白」这条语义的历史包袱（为兼容早已停更的 **numarray**），并知道 C 源码注释里已经把它标记为「应当被弃用」；
- 写脚本验证 char 版比较与 numpy 原生比较在尾部空白上的行为差异。

## 2. 前置知识

本讲承接 u1-l3（公共 API 与第一次向量字符串操作）建立的「元素级（element-wise）」语义，以及 u2-l2 揭示的「六个比较函数是 `defchararray` **本地覆盖**、并非 `numpy.strings` 同一对象」这一事实。在此基础上，还需要几个背景概念：

- **富比较（rich comparison）**：Python 里 `==`、`!=`、`<`、`<=`、`>`、`>=` 这六个运算符，底层分别对应 `__eq__` / `__ne__` / `__lt__` / `__le__` / `__gt__` / `__ge__` 六个特殊方法；CPython 内部又用六个常量 `Py_EQ` / `Py_NE` / `Py_LT` / `Py_LE` / `Py_GT` / `Py_GE` 来标记「到底是哪一种比较」。本讲的 C 代码就是把字符串比较码翻译成这六个常量。
- **定宽字符串数组**（u1-l2）：`str_` / `bytes_` 数组里每个元素是定宽的，短串用空字符 `\0`（或空格）在尾部**填充**到统一宽度。这正是「尾部空白」问题的根源——填充的字符到底算不算数？
- **numarray**：NumPy 的「前辈」之一（另一个是 Numeric）。NumPy 早期为了把 numarray 的用户平滑迁过来，保留了一些 numarray 的特殊行为；本讲讲的「比较前先剥尾部空白」就是其中之一，至今仍以「向后兼容」的名义留在代码里。
- **NEP-18 / `array_function_dispatch`**：比较函数上的 `@array_function_dispatch(...)` 装饰器是用来支持第三方数组类型（duck-typing）的，本讲只点一句、详细机制留到 u2-l4。

如果你已经清楚「六个比较运算符 ↔ 六个 `Py_*` 常量」和「定宽字符串的尾部填充」，本讲的重点就在第 4 节对 `compare_chararrays` 的逐行精读。

## 3. 本讲源码地图

本讲盯一条「从 Python 自由函数一路下到 C」的调用链，把它对着读：

| 文件 | 作用 | 本讲用到的部分 |
|------|------|------|
| `numpy/_core/defchararray.py` | `numpy.char` 的真正实现 | `compare_chararrays` 的导入（22 行）、分发器 `_binary_op_dispatcher`（57–58 行）、六个比较自由函数（61–263 行）、`chararray` 的比较运算符委托（606–664 行） |
| `numpy/_core/multiarray.py` | C 扩展模块的 Python 绑定 | `compare_chararrays` 出现在 `__all__`（39 行）、把它的 `__module__` 改写为 `numpy.char`（78 行） |
| `numpy/_core/src/multiarray/multiarraymodule.c` | C 层真正的实现 | `compare_chararrays` 函数体（3884 行起）、参数解析与比较码映射（3895–3937 行）、「只为 rstrip 而存在」的注释（3879–3883 行） |
| `numpy/_core/tests/test_defchararray.py` | 测试 | `TestComparisons` 类（164 行起），尤其是 `'123'` 与 `'123  '` 相等的断言（179–182 行） |

一句话定位：用户调用 `np.char.equal(x, y)` →（u2-l1 的门面转发）→ `defchararray.equal(x, y)` → `compare_chararrays(x, y, '==', True)` →（C 层）`_umath_strings_richcompare(...)`。本讲就是把这条链上每一段都拆开看。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**比较运算（六个自由函数的统一模式）**、**compare_chararrays（C 层）**、**尾部空白剥离语义（numarray 兼容）**。前两个对应本讲规格要求的最小模块，第三个是对核心主题「先剥尾部空白」的集中深挖。

### 4.1 比较运算：六个自由函数的统一调用模式

#### 4.1.1 概念说明

`numpy.char` 提供六个比较函数：`equal`、`not_equal`、`greater_equal`、`less_equal`、`greater`、`less`。它们都是「元素级」的：输入两个字符串数组 `x1`、`x2`，逐元素比较，返回一个**同形状的布尔数组**。

它们和 `numpy.equal` / `numpy.greater` 等同名 ufunc 的**唯一区别**，就是比较前会先把每个字符串尾部的空白（whitespace）剥掉。u2-l2 已经从「`is` 身份」角度指出这六个函数是 `defchararray` 本地覆盖、并非 `numpy.strings` 的同一对象；本讲要回答的是：**本地覆盖到底覆盖了什么、怎么实现的？**

答案出奇的简单——这六个函数的函数体各自只有**一行**，唯一的差异是比较码字符串。

#### 4.1.2 核心流程

每个比较函数的执行流程完全一致：

1. `@array_function_dispatch(_binary_op_dispatcher)` 装饰器拦截调用，先用分发器 `_binary_op_dispatcher(x1, x2)` 把「公开签名」翻译成「操作数元组」——这里就是原样返回 `(x1, x2)`，为 NEP-18 的 duck-typing 留口子（详见 u2-l4）。
2. 进入真正的实现函数，函数体只有一行：`return compare_chararrays(x1, x2, '<比较码>', True)`。
3. 比较码是一个长度为 1 或 2 的字符串，对应六种关系；第四个参数 `True` 是 **rstrip 开关**——固定写死为 `True`，表示「比较前剥掉尾部空白」。

六个函数与比较码、C 层常量的对应关系：

| 自由函数 | 比较码字符串 | 长度 | 对应 Python 富比较 | C 层常量 |
|----------|-------------|------|-------------------|----------|
| `equal`         | `'=='` | 2 | `==` | `Py_EQ` |
| `not_equal`     | `'!='` | 2 | `!=` | `Py_NE` |
| `greater_equal` | `'>='` | 2 | `>=` | `Py_GE` |
| `less_equal`    | `'<='` | 2 | `<=` | `Py_LE` |
| `greater`       | `'>'`  | 1 | `>`  | `Py_GT` |
| `less`          | `'<'`  | 1 | `<`  | `Py_LT` |

也就是说，理解了 `equal` 这一个函数，另外五个只是把 `'=='` 换成别的比较码而已。

#### 4.1.3 源码精读

先看 `compare_chararrays` 是怎么进来的——它是从 `numpy._core.multiarray` 导入的 C 函数：

[numpy/_core/defchararray.py:22](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L22) —— 从 C 扩展模块 `numpy._core.multiarray` 导入 `compare_chararrays`。注意它是 **C 实现**，Python 这一层只是拿到一个引用。

分发器与装饰器工厂：

[numpy/_core/defchararray.py:53-58](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L53-L58) —— `array_function_dispatch` 是把 `overrides.array_function_dispatch` 的 `module` 参数固定为 `'numpy.char'` 后的偏函数（partial）；`_binary_op_dispatcher(x1, x2)` 只返回 `(x1, x2)`。分发器的作用是把「面向用户的参数」转成「真正参与运算的操作数」，这里两者一致。

六个比较函数的开头——`equal`：

[numpy/_core/defchararray.py:61-92](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L61-L92) —— `equal(x1, x2)`。注意第 92 行：函数体只有 `return compare_chararrays(x1, x2, '==', True)`。docstring 里明确写了「Unlike `numpy.equal`, this comparison is performed by first stripping whitespace ... for backward-compatibility with numarray」，并给了 `np.char.equal('aa', 'aa ')` 返回 `array(True)` 的例子。

其余五个完全是同一个模子，只差比较码：

[numpy/_core/defchararray.py:95-126](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L95-L126) —— `not_equal`，函数体 `return compare_chararrays(x1, x2, '!=', True)`。

[numpy/_core/defchararray.py:129-161](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L129-L161) —— `greater_equal`，比较码 `'>='`。

[numpy/_core/defchararray.py:164-195](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L164-L195) —— `less_equal`，比较码 `'<='`。

[numpy/_core/defchararray.py:198-229](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L198-L229) —— `greater`，比较码 `'>'`（长度 1）。

[numpy/_core/defchararray.py:232-263](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L232-L263) —— `less`，比较码 `'<'`（长度 1）。

补充一笔：`chararray` 类的比较运算符 `==`、`!=`、`>=`、`<=`、`>`、`<` 也是直接调这六个自由函数，不走 numpy 默认的字符串比较：

[numpy/_core/defchararray.py:606-664](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L606-L664) —— `chararray.__eq__` / `__ne__` / `__ge__` / `__le__` / `__gt__` / `__lt__` 各自一行 `return equal(self, other)` 等。所以 `chararray` 上的 `==` 也带「剥尾部空白」语义——运算符与自由函数行为一致。（运算符重载的全貌留到 u3-l2。）

#### 4.1.4 代码实践

**实践目标**：亲手验证六个比较函数都返回布尔 `ndarray`，并对照各自的 docstring 例子确认结果。

**操作步骤**：

```python
import numpy as np

x = np.array(['a', 'b', 'c'])
# 六个函数逐一调用（比较码不同，但调用形式完全一致）
np.char.equal(x, 'b')          # 预期 array([False,  True, False])
np.char.not_equal(x, 'b')      # 预期 array([ True, False,  True])
np.char.greater_equal(x, 'b')  # 预期 array([False,  True,  True])
np.char.less_equal(x, 'b')     # 预期 array([ True,  True, False])
np.char.greater(x, 'b')        # 预期 array([False, False,  True])
np.char.less(x, 'b')           # 预期 array([ True, False, False])

# 验证返回类型是布尔 ndarray（对应测试 test_type）
out = np.char.equal(x, 'b')
print(type(out), out.dtype)    # 预期 <class 'numpy.ndarray'> bool
```

**需要观察的现象**：每次调用都返回一个与 `x` 同形状的布尔数组；六个函数的调用形式与返回类型完全一致，唯一不同是比较方向。

**预期结果**：上面注释里标注的数组（这些正是各函数 docstring 的 Examples 段落给出的结果，可放心对照）。

#### 4.1.5 小练习与答案

**练习 1**：为什么说「理解 `equal` 一个函数就够了」？

> **答案**：因为六个比较函数的实现是同一个模板——都用 `@array_function_dispatch(_binary_op_dispatcher)` 装饰、函数体都只有一行 `return compare_chararrays(x1, x2, '<比较码>', True)`，差异仅在于比较码字符串（`'=='`、`'!='`、`'>='`、`'<='`、`'>'`、`'<'`）。

**练习 2**：`_binary_op_dispatcher(x1, x2)` 为什么直接返回 `(x1, x2)`，而不是返回别的？

> **答案**：分发器的作用是「把面向用户的公开参数翻译成真正参与运算的操作数元组」，供 NEP-18 的 `__array_function__` 协议使用。比较是二元运算，两个操作数 `x1`、`x2` 都要参与，所以原样返回。它不负责实际计算，计算由被装饰的 `equal` 等实现函数完成。

### 4.2 compare_chararrays（C 层）：比较码与 rstrip 开关

#### 4.2.1 概念说明

上一节看到，六个 Python 自由函数都把活儿交给了 `compare_chararrays`。这个函数是 **C 实现**，绑定在 `numpy._core.multiarray` 里，但它对外的「户籍」被改写成了 `numpy.char`。它接受四个参数：`a1, a2, cmp, rstrip`——两个数组、一个比较码字符串、一个是否剥尾部空白的布尔开关。

C 源码里有一段很坦白的注释：这个函数**存在的唯一理由**，就是为了那个 `rstrip` 开关；维护者 @seberg 甚至认为它「应当被弃用」。换言之，除了「能在比较前剥掉尾部空白」这一点，`compare_chararrays` 没有别的本事是 numpy 现代字符串 ufunc 做不到的。这也解释了为什么官方推荐新代码直接用 `numpy.strings`——那里没有这条历史包袱。

#### 4.2.2 核心流程

`compare_chararrays` 在 C 层做的事：

1. **解析参数**：用 `PyArg_ParseTupleAndKeywords` 按 `a1, a2, cmp, rstrip` 取出四个值。其中 `cmp` 用 `s#` 格式拿到「字符串指针 + 长度」，`rstrip` 用 `O&` 配合 `PyArray_BoolConverter` 转成布尔。
2. **校验比较码长度**：要求 `cmp` 长度是 1 或 2，否则报错 `comparison must be '==', '!=', '<', '>', '<=', '>='`。
3. **把比较码映射成 C 常量**：长度 2 的（`==`、`!=`、`<=`、`>=`）要求第二个字符必须是 `'='`，再看第一个字符决定 `Py_EQ` / `Py_NE` / `Py_LE` / `Py_GE`；长度 1 的（`<`、`>`）直接映射成 `Py_LT` / `Py_GT`。
4. **把输入转成数组**：对 `a1`、`a2` 各调一次 `PyArray_FROM_O`，得到两个 `ndarray`。
5. **校验 dtype 并真正比较**：要求两个数组都是字符串 dtype（`PyArray_ISSTRING`），然后调底层 `_umath_strings_richcompare(newarr, newoth, cmp_op, rstrip)` 完成逐元素比较，`rstrip` 透传下去。

可以把它抽象成一行公式（`rstrip=True` 时）：

\[
\texttt{compare\_chararrays}(x, y, \text{op}, \texttt{True}) \;=\; \text{op}\big(\,\text{rstrip}(x),\; \text{rstrip}(y)\,\big) \quad \text{（逐元素）}
\]

其中 `op` 是六种比较之一，`rstrip` 表示剥掉每个元素尾部的空白字符。

#### 4.2.3 源码精读

先看 C 函数上方那段「自白式」注释：

[numpy/_core/src/multiarray/multiarraymodule.c:3879-3883](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/multiarraymodule.c#L3879-L3883) —— 注释直言：这个函数的唯一目的就是提供 `rstrip`；维护者认为它应当被弃用。这是理解整条「剥尾部空白」语义为何是历史包袱的关键。

函数签名、错误信息与参数关键字：

[numpy/_core/src/multiarray/multiarraymodule.c:3884-3902](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/multiarraymodule.c#L3884-L3902) —— `compare_chararrays` 的定义。`kwlist = {"a1", "a2", "cmp", "rstrip"}` 给出了四个参数名；解析格式串 `"OOs#O&"` 表示：两个 PyObject、一个带长度的字符串、一个用转换函数处理的值。错误信息固定为 `comparison must be '==', '!=', '<', '>', '<=', '>='`。

比较码 → C 常量的映射：

[numpy/_core/src/multiarray/multiarraymodule.c:3904-3937](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/multiarraymodule.c#L3904-L3937) —— 先校验长度（1 或 2）；长度 2 时要求第二个字符是 `'='`，再按首字符分派到 `Py_EQ`/`Py_NE`/`Py_LE`/`Py_GE`；长度 1 时按字符分派到 `Py_LT`/`Py_GT`。这正是上节那张对应表的来源。

转为数组、校验字符串 dtype、调用底层比较：

[numpy/_core/src/multiarray/multiarraymodule.c:3939-3949](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/multiarraymodule.c#L3939-L3949) —— `PyArray_FROM_O` 把两个输入转成 `ndarray`；`PyArray_ISSTRING` 校验都是字符串数组；最后 `_umath_strings_richcompare(newarr, newoth, cmp_op, rstrip != 0)` 完成真正的逐元素比较，把 `rstrip` 标志原样透传下去。

最后看 Python 绑定层如何给这个 C 函数「改户籍」：

[numpy/_core/multiarray.py:39](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/multiarray.py#L39) —— `compare_chararrays` 被列入 `multiarray` 的 `__all__`，所以能被 `defchararray` 导入。

[numpy/_core/multiarray.py:78](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/multiarray.py#L78) —— `compare_chararrays.__module__ = 'numpy.char'`，把它的模块归属从 `numpy._core.multiarray` 改写成 `numpy.char`。所以 `np.char.compare_chararrays.__module__` 显示为 `numpy.char`，文档与 `help()` 里也归在 char 名下。

#### 4.2.4 代码实践

**实践目标**：绕过六个自由函数，直接调用 `compare_chararrays`，亲手控制「比较码」和「rstrip 开关」，观察两者的作用。

**操作步骤**：

```python
import numpy as np

# 1) 同样的输入，比较码 '=='，分别开/关 rstrip
np.char.compare_chararrays('aa', 'aa ', '==', True)   # 预期 array(True)  —— 剥掉尾部空格后相等
np.char.compare_chararrays('aa', 'aa ', '==', False)  # 预期 array(False) —— 不剥，'aa' != 'aa '

# 2) 换比较码：'<' / '>'（长度 1）
np.char.compare_chararrays('abc', 'abd', '<', True)   # 预期 array(True)
np.char.compare_chararrays('abc', 'abd', '>', True)   # 预期 array(False)

# 3) 非法比较码，触发 C 层的报错信息
try:
    np.char.compare_chararrays('a', 'b', '~', True)
except ValueError as e:
    print(e)  # 预期：comparison must be '==', '!=', '<', '>', '<=', '>='
```

**需要观察的现象**：`rstrip=True` 与 `rstrip=False` 在 `'aa'` 对 `'aa '` 上结果相反；非法比较码抛出的 `ValueError` 文案与 C 源码里的 `msg` 字符串一字不差。

**预期结果**：如上注释。其中非法比较码的报错文案可直接对照 [multiarraymodule.c:3895](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/multiarraymodule.c#L3895) 的 `msg`。其余几项建议本地复核。

#### 4.2.5 小练习与答案

**练习 1**：`compare_chararrays` 的 C 注释说「这个函数的唯一目的是 rstrip」。请解释这句话——如果没有 rstrip 需求，它还有什么存在价值？

> **答案**：基本没有。把比较码字符串映射成 `Py_EQ` 等 C 常量、做 dtype 校验、调用底层 `_umath_strings_richcompare`，这些 numpy 现代字符串 ufunc（`numpy.strings.equal` 等）都能做。`compare_chararrays` 多出来的、且唯一多出来的能力，就是那个 `rstrip` 形参——这正是六个 char 比较函数固定传 `True` 的那个开关。

**练习 2**：比较码 `'='`（单个等号）会发生什么？

> **答案**：报 `ValueError: comparison must be '==', '!=', '<', '>', '<=', '>='`。因为长度 1 的比较码只接受 `'<'` 和 `'>'`（映射成 `Py_LT`/`Py_GT`）；单独的 `'='` 既不是合法的长度 1 码，也不满足长度 2 码「第二个字符必须是 `=`」的规则（它根本没有第二个字符），于是落入 `goto err`。

### 4.3 尾部空白剥离语义：numarray 兼容与行为差异

#### 4.3.1 概念说明

前两节反复出现的「剥尾部空白」，是本讲真正的主角。它的含义是：在比较两个字符串元素之前，**先把两边元素尾部的空白字符都剥掉**，再比。于是 `'aa'` 和 `'aa '`（尾部一个空格）会被当成 `'aa'` 和 `'aa'` 来比，结果相等。

这条语义不是 bug，而是**为了兼容 numarray 故意保留的**——六个函数的 docstring 都写了同一句「This behavior is provided for backward-compatibility with numarray」。numarray 是 NumPy 的前辈之一，当年它的字符串比较就会忽略尾部空白；为了让老代码迁过来行为不变，NumPy 把这条规则原样搬进了 `char`。

代价是：`char` 版比较与「正常」的字符串比较（`numpy.equal`、Python 原生 `==`）**语义不一致**，容易踩坑。这也是 C 注释里 @seberg 说「应当被弃用」、官方推荐改用 `numpy.strings` 的根本原因——`numpy.strings` 里的比较函数**不剥**尾部空白，行为更可预测。

#### 4.3.2 核心流程

以 `np.char.equal('aa', 'aa ')` 为例，完整链路是：

1. `defchararray.equal('aa', 'aa ')` 被调用；
2. 它执行 `return compare_chararrays('aa', 'aa ', '==', True)`；
3. C 层 `compare_chararrays` 因 `rstrip=True`，比较前对两个元素各做一次尾部空白剥离：`'aa' → 'aa'`、`'aa ' → 'aa'`；
4. 再按 `'=='` 比较 `'aa'` 与 `'aa'`，得 `True`。

对比 `numpy.equal`（不剥尾部空白）：`'aa'` 与 `'aa '` 的底层定宽表示里，尾部一个是空字符填充、一个是真实空格，逐字节不等，故得 `False`。

用公式概括差异（`rstrip` 仅在 char 版生效）：

\[
\text{char.equal}(x, y) = (\,\text{rstrip}(x) == \text{rstrip}(y)\,), \qquad
\text{numpy.equal}(x, y) = (x == y)
\]

#### 4.3.3 源码精读

docstring 里的「向后兼容」声明（六处措辞基本一致，以 `equal` 为例）：

[numpy/_core/defchararray.py:66-68](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L66-L68) —— 「Unlike `numpy.equal`, this comparison is performed by first stripping whitespace characters from the end of the string. This behavior is provided for backward-compatibility with numarray.」这是「剥尾部空白 + numarray 兼容」语义在源码里的权威出处。

docstring 自带的对照例子：

[numpy/_core/defchararray.py:82-86](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L82-L86) —— `np.char.equal('aa', 'aa ')` 返回 `array(True)`，正是 rstrip 生效的铁证。

测试里的关键断言——`'123'` 与带尾部空格的 `'123  '` 相等：

[numpy/_core/tests/test_defchararray.py:164-182](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/tests/test_defchararray.py#L164-L182) —— `TestComparisons` 类。`A()` 含 `'123'`、`B()` 含 `'123  '`（两个尾部空格），`test_equal` 断言 `A == B` 在该位置为 `True`。这条测试专门锁定「剥尾部空白」行为，一旦有人误改 rstrip，它会立刻失败。

C 层把 `rstrip` 透传给真正干活的函数：

[numpy/_core/src/multiarray/multiarraymodule.c:3948-3949](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/src/multiarray/multiarraymodule.c#L3948-L3949) —— `PyArray_ISSTRING` 校验后，调用 `_umath_strings_richcompare(newarr, newoth, cmp_op, rstrip != 0)`。`rstrip != 0` 把 Python 布尔转成 C 整数开关，由底层比较循环在逐元素比较时决定是否先剥尾部空白。

#### 4.3.4 代码实践

**实践目标**（本讲规格指定的主实践）：用 `np.char.equal` 比较 `'aa'` 与 `'aa '`（尾部带空格），再对比 numpy 原生比较的行为，验证 char 版尾部空白被剥离；随后测试 `not_equal`、`greater`、`less` 并记录差异。

**操作步骤**：

```python
import numpy as np
import warnings
warnings.simplefilter("ignore", DeprecationWarning)  # 本实践不关心 chararray 弃用警告

# 1) char 版 equal：先剥尾部空白 → 相等
print(np.char.equal('aa', 'aa '))   # 预期 array(True)

# 2) numpy 原生 equal / ==：不剥尾部空白 → 不等
x = np.array('aa',  dtype=np.str_)
y = np.array('aa ', dtype=np.str_)
print(np.equal(x, y))               # 预期 array(False)（请本地复核）
print(x == y)                       # 预期 array(False)（请本地复核）

# 3) 其它三个比较函数，同样体会「先剥再比」
print(np.char.not_equal('aa', 'aa '))   # 预期 array(False)  —— 剥后相等，故 not_equal 为 False
print(np.char.greater('ab ', 'aa'))     # 预期 array(True)   —— 'ab' > 'aa'
print(np.char.less('aa', 'ab '))        # 预期 array(True)   —— 'aa' < 'ab'

# 4) 直接用 compare_chararrays 对比 rstrip 开/关（把上面的差异定位于这一个开关）
print(np.char.compare_chararrays('aa', 'aa ', '==', True))   # 预期 True
print(np.char.compare_chararrays('aa', 'aa ', '==', False))  # 预期 False
```

**需要观察的现象**：第 1 步与第 2 步结果相反——同一个 `'aa'` 对 `'aa '`，char 版说「相等」，原生版说「不等」；第 3 步的 `not_equal` 因为先剥再比、剥后相等，反而返回 `False`（容易让人意外）；第 4 步证明这个差异完全来自 `rstrip` 这一个开关。

**预期结果**：如注释所标。第 1、4 步来自 docstring 与 C 源码，可放心对照；第 2、3 步建议本地复核并记录你实际看到的布尔值。

**待本地验证**：第 2 步中 `np.array('aa') == np.array('aa ')` 的确切结果取决于 numpy 对定宽字符串尾部的处理细节；请在本地确认，并用自己的话写下「为什么 char 版与原生版在这里不同」。

#### 4.3.5 小练习与答案

**练习 1**：`np.char.not_equal('aa', 'aa ')` 为什么返回 `False`？

> **答案**：因为 `not_equal` 内部调 `compare_chararrays('aa', 'aa ', '!=', True)`，`rstrip=True` 先把两边都剥成 `'aa'`，再比 `'aa' != 'aa'` 为假，故返回 `False`。直觉上「带空格和不带空格应该不相等」在这里不成立，正是因为先剥了尾部空白。

**练习 2**：如果你正在写新代码，需要在数组里做精确的字符串相等比较（尾部空格也要算数），应该用 `np.char.equal` 还是 `numpy.strings.equal`？为什么？

> **答案**：用 `numpy.strings.equal`。因为 `np.char.equal` 会先剥尾部空白，把 `'aa'` 和 `'aa '` 当成相等，不符合「精确比较」的要求；`numpy.strings.equal` 不剥尾部空白，行为可预测，也是官方推荐的现代路径（见 u2-l2）。如果数据里确实需要忽略尾部空格，应当显式调 `numpy.strings.rstrip` 再比，而不是依赖 char 的隐式剥离——这样代码意图更清晰。

## 5. 综合实践

把本讲的三条线索（统一调用模式、C 层比较码与 rstrip、numarray 兼容语义）串起来，完成下面这个「行为对照表」任务：

**任务**：写一个脚本，构造一个带尾部空格的字符串数组，分别用「char 版六函数」「`compare_chararrays` 直调（`rstrip=True`/`False`）」「numpy 原生 `equal`/`greater` 等」三类方式做比较，把结果整理成一张表，直观看出「剥尾部空白」到底改变了哪些比较结果。

**参考骨架**：

```python
import numpy as np
import warnings
warnings.simplefilter("ignore", DeprecationWarning)

x = np.array(['aa', 'ab', 'ab '])      # 注意第三个元素尾部带空格
y = np.array(['aa ', 'ab', 'ac'])      # 注意第一个元素尾部带空格

ops = [
    ('==', np.char.equal,         'equal'),
    ('!=', np.char.not_equal,     'not_equal'),
    ('>=', np.char.greater_equal, 'greater_equal'),
    ('<=', np.char.less_equal,    'less_equal'),
    ('>',  np.char.greater,       'greater'),
    ('<',  np.char.less,          'less'),
]

for cmp_code, char_fn, name in ops:
    char_result   = char_fn(x, y)
    c_strip       = np.char.compare_chararrays(x, y, cmp_code, True)   # 与 char_fn 应一致
    c_no_strip    = np.char.compare_chararrays(x, y, cmp_code, False)  # 不剥尾部空白
    print(f"{name:14s} char={char_result.tolist()}  C_strip={c_strip.tolist()}  C_no_strip={c_no_strip.tolist()}")
```

**要回答的问题**：

1. `char_fn(x, y)` 与 `compare_chararrays(x, y, cmp_code, True)` 在每一行是否完全一致？为什么？（提示：前者就是后者的薄包装。）
2. 哪些位置上 `C_strip` 与 `C_no_strip` 结果不同？这些位置对应的元素是否恰好是带尾部空格的 `'ab '` / `'aa '`？
3. 如果要把这段代码迁移到不带历史包袱的 `numpy.strings`，哪些结果会变？需要在迁移时显式补上什么调用？（提示：`numpy.strings.rstrip`。）

**预期收获**：你会亲眼看到「剥尾部空白」只在「元素尾部确实有空格」的位置改变结果，并理解为什么官方把它视为应当淘汰的历史语义。

## 6. 本讲小结

- `numpy.char` 的六个比较函数 `equal` / `not_equal` / `greater_equal` / `less_equal` / `greater` / `less` 是**统一模式**：都用 `@array_function_dispatch(_binary_op_dispatcher)` 装饰，函数体都只有一行 `return compare_chararrays(x1, x2, '<比较码>', True)`，差异仅在比较码。
- 真正干活的是 C 层 `compare_chararrays`，参数为 `a1, a2, cmp, rstrip`；它把长度 1–2 的比较码字符串映射成 `Py_EQ` / `Py_NE` / `Py_LE` / `Py_GE` / `Py_LT` / `Py_GT`，校验字符串 dtype 后调底层 `_umath_strings_richcompare`。
- 六个函数固定传 `rstrip=True`，语义是「比较前先剥掉两边元素尾部空白」，因此 `np.char.equal('aa', 'aa ')` 返回 `True`，与 `numpy.equal` 的 `False` 相反。
- 这条语义是为**兼容 numarray** 而保留的（docstring 明示），C 注释里 @seberg 已把它标记为「应当被弃用」，这也是官方推荐新代码改用 `numpy.strings` 的原因。
- `compare_chararrays` 的 `__module__` 被改写为 `numpy.char`，所以它对外归在 char 名下；`chararray` 的 `==` 等六个运算符也直接委托给这六个自由函数，行为一致。
- 实践中若需要「精确比较（尾部空格也算数）」，应使用 `numpy.strings` 的比较函数，或显式 `rstrip` 后再比，不要依赖 char 的隐式剥离。

## 7. 下一步学习建议

- 下一讲 **u2-l4（array_function_dispatch 与 set_module 装饰器）** 会补齐本讲只点了一句的装饰器机制：`@array_function_dispatch(_binary_op_dispatcher)` 到底怎么支持 NEP-18 的 `__array_function__` 协议、分发器（dispatcher）与实现函数（implementation）如何分工，以及 `__module__` 改写对文档与 duck-typing 的意义。
- 想从「行为」转向「类内部」的读者，可继续读 `defchararray.py` 中 `chararray` 的运算符重载（606–720 行），看 `__eq__`/`__add__`/`__mul__`/`__mod__` 如何委托给本讲的比较函数与 `add`/`multiply`/`mod`——这会在 **u3-l2（运算符重载与方法委托）** 系统讲解。
- 建议顺手运行 [numpy/_core/tests/test_defchararray.py](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/tests/test_defchararray.py) 里的 `TestComparisons` 与 `TestComparisonsMixed1`/`TestComparisonsMixed2`（164–227 行），它们正是为锁定「剥尾部空白」语义而写的回归测试。
