# multiply / partition / rpartition 本地包装

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `numpy.char` 里 `multiply`、`partition`、`rpartition` 这三个函数和 `numpy.strings` 里同名函数的**关系**——它们不是「同一个对象」（不是纯再导出），而是 `defchararray` **本地新定义**的「薄包装（thin wrapper）」，内部委托给 `numpy.strings` 的原版；
- 解释 `defchararray` 为什么要用 `from numpy.strings import multiply as strings_multiply, partition as strings_partition, rpartition as strings_rpartition` 这种**别名导入**——因为紧接着它就要 `def multiply(...)` 把这个名字重新绑定到本地函数，必须先用别名「留一份原版的引用」，否则本地函数就没法再调到原版了；
- 读懂 `multiply` 的包装手法：`try: return strings_multiply(a, i) except TypeError: raise ValueError(...)`——它把 `numpy.strings.multiply` 在「乘数不是整数」时抛的 `TypeError` **翻译成 `ValueError`**，目的是保留 `char` 历史上的错误类型契约；
- 读懂 `partition` / `rpartition` 的包装手法：`return np.stack(strings_partition(a, sep), axis=-1)`——`numpy.strings` 版返回的是**三个数组的元组**（前段、分隔符、后段），`char` 版用 `np.stack` 把这三段沿**新增的最后一维**拼成**一个多一维的数组**，所以 `char` 版比 `strings` 版多出一个长度为 3 的维度；
- 写脚本对比 `np.char.partition(x, sep)` 与 `np.strings.partition(x, sep)` 在同一输入上的**返回类型与形状**，并用 `np.char.multiply(a, 2.0)` 验证它抛 `ValueError`、而 `np.strings.multiply(a, 2.0)` 抛 `TypeError`。

本讲承接 u2-l2 的「四类划分」。u2-l2 已经判定 `multiply`/`partition`/`rpartition` 属于「本地包装」这一类（既不是纯再导出，也不是 `chararray` 那种独有名字），本讲就钻进这三个函数的函数体，把「包装」二字拆成两种**具体手法**——错误类型转换、结果重组。

## 2. 前置知识

本讲紧接 u2-l2（「从 `numpy.strings` 再导出：现代委托关系」）。u2-l2 给出的关键结论是：

- `defchararray` 顶部用 `from numpy.strings import *` 整批再导出，所以 `np.char.capitalize is np.strings.capitalize` 为真——这是**纯再导出**；
- 但有少数名字被「本地覆盖」了，`multiply`、`partition`、`rpartition` 就在其中——它们在 `defchararray` 里有**自己的 `def`**，所以 `np.char.multiply is np.strings.multiply` 为假。本讲要回答的正是：**这层「自己的 `def`」到底多做了什么？**

在此基础上，还需要几个背景概念：

- **薄包装（thin wrapper）**：一个函数几乎不做事，只是把参数转发给另一个函数，但在转发前后做一点点「加工」（改错误类型、改返回形状）。本讲三个函数都是薄包装。
- **`try / except` 做错误类型翻译**：捕获一种异常、抛出另一种异常，是库在「迁移底层实现」时保持对外错误契约不变的常用手段。`multiply` 就是这么把 `TypeError` 翻成 `ValueError`。
- **`np.stack(arrays, axis=...)`**：把一组**形状相同**的数组沿一条**新轴**拼起来。若每个数组形状为 `S`、共有 `N` 个，`np.stack(..., axis=-1)` 的结果形状就是 `S + (N,)`——在末尾多出一条长度为 `N` 的维度。这是理解 `partition`/`rpartition` 形状差异的钥匙。
- **元组（tuple）返回 vs 数组返回**：一个函数可以「返回三个数组」有两种截然不同的形式——返回一个长度为 3 的**元组**（`return (a, b, c)`，调用方拿到 3 个对象），或返回**一个多一维的数组**（形状 `..., 3`，调用方拿到 1 个对象）。`numpy.strings.partition` 用前者，`numpy.char.partition` 用后者。
- **`__module__` 与 `@set_module`**：u2-l4 已讲透——`@set_module("numpy.char")` 把函数的 `__module__` 改写成 `'numpy.char'`，让「定义在 `_core` 里的函数」对外伪装成「char 模块的原住民」。本讲三个包装函数头上都顶着这个装饰器。

如果你已经清楚「薄包装就是转发 + 一点加工」和 `np.stack` 会新增一条维度，本讲的重点就是第 4 节对三段函数体的逐行精读，以及第 5 节把两种手法串起来的综合实践。

## 3. 本讲源码地图

本讲盯一条「委托 + 加工」链路——从 `defchararray` 的别名导入，到三个本地函数体，再到它们各自委托的 `numpy.strings` 原版：

| 文件 | 作用 | 本讲用到的部分 |
|------|------|------|
| `numpy/_core/defchararray.py` | `numpy.char` 的真正实现 | 顶部的 `from numpy.strings import *` 与三条**别名导入**（30–35 行）、本地 `multiply`（266–315 行，重点是 312–315 行的 `try/except`）、本地 `partition`（318–357 行，重点是 357 行的 `np.stack`）、本地 `rpartition`（360–401 行，重点是 401 行的 `np.stack`）、`chararray.partition` 方法对本地 `partition` 的再委托（991–999 行） |
| `numpy/_core/strings.py` | `numpy.strings` 的实现（被委托方） | `_multiply_dispatcher`（144–145 行）、`multiply` 在「`i` 非整数」时 `raise TypeError`（190–194 行）、`partition` 返回 3-tuple 的文档（1556–1564 行）与函数体（1580–1602 行，`return _partition_index(...)` 返回三元组） |
| `numpy/_core/tests/test_defchararray.py` | `char`/`defchararray` 的测试 | `test_partition`（490–497 行）、`test_rpartition`（571–578 行）——它们的断言揭示了 `char` 版返回「每个元素是一个 3 元组」的多一维数组 |

一句话定位：用户调用 `np.char.partition(x, sep)` →（u2-l1 门面 `__getattr__`）→ `defchararray.partition` → 函数体只有一行 `np.stack(strings_partition(x, sep), axis=-1)` → 其中的 `strings_partition` 就是 `numpy.strings.partition`，它真正干活、返回**三个数组** → `np.stack` 把这三段拼成**一个多一维的数组**返回。`multiply` 同理，只是「加工」从「拼数组」换成了「翻译异常」。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块，对应一个「委托」问题与两个「包装」问题：

1. **4.1 strings 委托**：这三个本地函数凭什么能调到 `numpy.strings` 的原版？——别名导入。
2. **4.2 本地包装·错误转换**：`multiply` 如何把 `TypeError` 翻译成 `ValueError`。
3. **4.3 本地包装·结果重组**：`partition`/`rpartition` 如何用 `np.stack` 把三元组重组成多一维数组。

### 4.1 strings 委托：别名导入如何留住「原版」

#### 4.1.1 概念说明

先回忆一个 Python 基本事实：模块命名空间里，**后定义的名字会覆盖先导入的同名名字**。在 `defchararray` 里，事情是这样发生的：

1. 它先用 `from numpy.strings import *` 把一大批函数（包括 `capitalize`、`multiply`、`partition`、`rpartition` 等）拉进自己的命名空间——此时 `multiply` 这个名字指向 `numpy.strings.multiply`；
2. 紧接着，它**又 `def multiply(...)`**，定义了一个本地函数。这一步执行完，`multiply` 这个名字就被**重新绑定**到本地函数，原来指向 `numpy.strings.multiply` 的引用「丢了」。

这带来一个麻烦：本地 `multiply` 的函数体里想调原版 `numpy.strings.multiply`，可它自己的名字 `multiply` 现在指向自己——直接 `return multiply(a, i)` 会**无限递归**！

解决办法就是**别名导入**：在 `def` 之前，先用 `as` 给原版起一个**不会被覆盖**的名字（`strings_multiply`），本地函数体里去调这个别名。`partition`、`rpartition` 同理。

> 这也解释了 u2-l2 的判据：`np.char.multiply is np.strings.multiply` 为**假**——因为 `np.char.multiply` 指向的是本地 `def` 出来的**新函数对象**，而 `strings_multiply` 别名才指向 `numpy.strings.multiply`。两者是不同对象。

#### 4.1.2 核心流程

`defchararray` 顶部建立委托关系的流程：

```text
from numpy.strings import *                 # ① 整批拉进来（含 multiply/partition/rpartition）
from numpy.strings import (                 # ② 给三个「即将被覆盖」的原版留别名
    multiply    as strings_multiply,
    partition   as strings_partition,
    rpartition  as strings_rpartition,
)

@set_module("numpy.char")
def multiply(a, i):                         # ③ 名字 multiply 被重新绑定到本地函数
    ...
    return strings_multiply(a, i)           # ④ 用别名调原版，避免无限递归
```

关键点是第 ② 步：**别名必须在第 ③ 步 `def` 之前建立**，否则原版引用就保不住。三个本地函数都遵循同一个模式——「别名导入 → 本地 `def` → 函数体调别名」。

#### 4.1.3 源码精读

先看顶部的导入块：

[numpy/_core/defchararray.py:23-35](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L23-L35) —— 第 30 行 `from numpy.strings import *` 整批再导出；第 31–35 行**单独**把 `multiply`/`partition`/`rpartition` 三个原版用 `as` 改名成 `strings_multiply`/`strings_partition`/`strings_rpartition` 导入。这三行就是「留原版引用」的全部秘密。注意第 23–28 行另一段 `from numpy._core.strings import _join as join, ...`——那是 u2-l2 讲过的「从私有模块捞 `join`/`split`」的另一条委托线，本讲不展开。

再看三个本地函数的「签名 + 装饰器」，它们结构完全一致：

- [numpy/_core/defchararray.py:266-267](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L266-L267) —— `@set_module("numpy.char")` + `def multiply(a, i):`。注意它**只**挂了 `set_module`，**没有** `@array_function_dispatch`——也就是说 `np.char.multiply` 是个**普通 Python 函数**，本身不直接参与 NEP-18 分发；分发是在它内部调 `strings_multiply(a, i)` 时，由 `numpy.strings.multiply`（那是个被 `array_function_dispatch` 装饰过的分发器对象）来完成的。`partition`、`rpartition` 同样如此。
- [numpy/_core/defchararray.py:318-319](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L318-L319) —— `partition` 的装饰器与签名。
- [numpy/_core/defchararray.py:360-361](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L360-L361) —— `rpartition` 的装饰器与签名。

最后看一眼「原版」长什么样，确认它确实是个被 `array_function_dispatch` 装饰过的分发器：

[numpy/_core/strings.py:144-145](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/strings.py#L144-L145) —— `_multiply_dispatcher(a, i)` 只返回 `(a,)`（只有字符串数组 `a` 才是「相关数组参数」，乘数 `i` 不是）。它会被 `@array_function_dispatch(_multiply_dispatcher)` 用在 `numpy.strings.multiply` 头上（见 strings.py 第 149–150 行）。所以「分发能力」属于 strings 层，char 层只是个会调它的薄壳。

#### 4.1.4 代码实践

**实践目标**：用 `is` 验证三个本地函数与 strings 原版「不是同一个对象」，并与一个纯再导出函数（`capitalize`）对比，亲手确认 u2-l2 的「四类划分」。

**操作步骤**：

```python
import numpy as np

# (1) 纯再导出：同一个对象
print("capitalize:", np.char.capitalize is np.strings.capitalize)   # 预期 True

# (2) 本地包装：不是同一个对象
print("multiply   :", np.char.multiply    is np.strings.multiply)    # 预期 False
print("partition  :", np.char.partition   is np.strings.partition)   # 预期 False
print("rpartition :", np.char.rpartition  is np.strings.rpartition)  # 预期 False

# (3) 但它们的 __module__ 都被 set_module 改写成 'numpy.char'
for name in ("multiply", "partition", "rpartition"):
    fn = getattr(np.char, name)
    print(f"{name:10s} module =", fn.__module__)   # 预期都是 numpy.char
```

**需要观察的现象**：`capitalize` 那行打印 `True`，其余三行打印 `False`；三个函数的 `__module__` 都是 `numpy.char`。

**预期结果**：`True` / `False` / `False` / `False`，且 `__module__` 全为 `numpy.char`。这与 u2-l2 的判据一致——「`is` 身份」为假说明是本地定义，而 `__module__` 为 `numpy.char` 说明被 `set_module` 伪装过。

> 若本地 NumPy 版本较老（2.5 之前）`numpy.strings` 尚未公开这些名字，第 (2) 步可能报 `AttributeError`——那说明你跑的不是本讲所基于的版本，请升级到 ≥ 2.5 再验证；现象不确定时记为「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果把第 31–35 行的别名导入删掉，只保留 `from numpy.strings import *`，本地 `def multiply` 的函数体里写 `return multiply(a, i)` 会发生什么？

**参考答案**：会**无限递归**直到 `RecursionError`。因为 `def multiply` 执行后，模块命名空间里的 `multiply` 已指向本地函数本身，`multiply(a, i)` 调的就是自己。别名 `strings_multiply` 的作用正是保留一条「指向原版」、不会被 `def` 覆盖的引用。

**练习 2**：`np.char.multiply.__module__` 和 `np.strings.multiply.__module__` 分别是什么？为什么不同？

**参考答案**：前者是 `'numpy.char'`（本地函数被 `@set_module("numpy.char")` 改写），后者是 `'numpy.strings'`（strings 版被 `@set_module("numpy.strings")` 改写）。两者是**不同函数对象**、定义在不同模块、各自被各自的 `set_module` 标注，所以 `__module__` 不同——这正是 `is` 判定为假的内在原因。

---

### 4.2 本地包装·错误转换：multiply 的 TypeError → ValueError

#### 4.2.1 概念说明

`multiply(a, i)` 的语义是「把字符串数组 `a` 的每个元素重复 `i` 次、元素级拼接」（如 `'a' * 3 == 'aaa'`）。这里 `i` 必须是**整数**——重复次数不可能是「2.5 次」。

问题来了：当用户传了非整数（比如浮点数）的 `i` 时，该报什么错？

- `numpy.strings.multiply` 的选择是抛 **`TypeError`**——「类型不对」；
- 但历史上的 `numpy.char.multiply` 抛的是 **`ValueError`**——「值不被接受」。

当 char 的实现迁移到「委托 strings」之后，如果直接转发，错误类型会从 `ValueError` 变成 `TypeError`，**可能破坏那些写了 `except ValueError` 来兜底的老代码**。于是 char 在中间加了一层 `try/except`，把 strings 的 `TypeError` **翻译回** char 历史上的 `ValueError`，以保持对外契约不变。`multiply` 的 docstring 也直言：「This is a thin wrapper around np.strings.multiply that raises `ValueError` when `i` is not an integer. It only exists for backwards-compatibility.」

> 这是一种典型的「**适配器模式**」用法：底层换了（strings 抛 TypeError），但对外接口（char 抛 ValueError）维持不变，靠中间层做类型翻译。

#### 4.2.2 核心流程

`numpy.char.multiply(a, i)` 的执行流程：

```text
np.char.multiply(a, i)
   │
   ▼  defchararray.multiply（本地薄包装，仅 @set_module）
   try:
       return strings_multiply(a, i)        # ← 委托给 numpy.strings.multiply
   except TypeError:                        # ← strings 因 i 非整数抛 TypeError
       raise ValueError("Can only multiply by integers")
```

而在被委托方 `numpy.strings.multiply` 内部，触发 `TypeError` 的判定是：

```text
i = np.asanyarray(i)                        # 把乘数变成数组（标量也行）
if not np.issubdtype(i.dtype, np.integer):  # 不是整数 dtype？
    raise TypeError(f"unsupported type {i.dtype} for operand 'i'")
```

所以「`i` 是浮点」→ `np.asanyarray(2.0).dtype` 是 `float64` → `issubdtype(float64, integer)` 为假 → strings 抛 `TypeError` → char 捕获 → 抛 `ValueError`。两条路径，同一个输入，**不同异常类型**。

#### 4.2.3 源码精读

先看 char 本地 `multiply` 的函数体——全部逻辑只有 4 行：

[numpy/_core/defchararray.py:312-315](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L312-L315) —— `try: return strings_multiply(a, i)` 正常情况直接把 strings 的结果透传出去；`except TypeError: raise ValueError("Can only multiply by integers")` 把异常类型翻译掉。注意 `raise ValueError(...)` 是**不带 `from`** 的新抛出，对外只看到一个干净的 `ValueError`。完整签名与文档见 [numpy/_core/defchararray.py:266-315](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L266-L315)。

再看被委托方「抛 `TypeError`」的那几行：

[numpy/_core/strings.py:190-194](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/strings.py#L190-L194) —— 第 192 行 `i = np.asanyarray(i)` 把乘数归一化成数组；第 193 行 `if not np.issubdtype(i.dtype, np.integer):` 判定它是否整数 dtype；第 194 行 `raise TypeError(f"unsupported type {i.dtype} for operand 'i'")` 就是 char 那个 `except TypeError` 真正捕获到的东西。把这两段对着读，`TypeError → ValueError` 的翻译链就完全闭合了。

#### 4.2.4 代码实践

**实践目标**：用同一个非整数乘数，分别调用 `np.char.multiply` 与 `np.strings.multiply`，验证前者抛 `ValueError`、后者抛 `TypeError`，且错误消息各自对应源码里的字符串。

**操作步骤**：

```python
import numpy as np

a = np.array(['a', 'b', 'c'])

# (1) 正常路径：整数乘，char 与 strings 行为一致（见 multiply docstring 示例）
print(np.char.multiply(a, 3))          # 预期 ['aaa' 'bbb' 'ccc']  dtype '<U3'

# (2) 非整数乘：char 抛 ValueError
try:
    np.char.multiply(a, 2.0)
except ValueError as e:
    print("char     ->", type(e).__name__, ":", e)   # 预期 ValueError: Can only multiply by integers

# (3) 同一输入，strings 直接抛 TypeError
try:
    np.strings.multiply(a, 2.0)
except TypeError as e:
    print("strings  ->", type(e).__name__, ":", e)   # 预期 TypeError: unsupported type float64 for operand 'i'
```

**需要观察的现象**：第 (1) 步正常输出拼接结果；第 (2) 步只可能进入 `except ValueError` 分支（不会进 TypeError）；第 (3) 步只可能进入 `except TypeError` 分支。

**预期结果**：`['aaa' 'bbb' 'ccc']`；`char -> ValueError : Can only multiply by integers`；`strings -> TypeError : unsupported type float64 for operand 'i'`。两条消息分别精确对应 [defchararray.py:315](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L315) 与 [strings.py:194](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/strings.py#L194) 里写死的字符串。

> 这是「源码阅读型 + 可运行」实践：错误消息的字面值就是源码里的字面量，跑一遍即可双向印证。若本地环境现象与此不符，记为「待本地验证」并核对 NumPy 版本。

#### 4.2.5 小练习与答案

**练习 1**：如果传 `np.char.multiply(a, np.array([1, 2, 3]))`（整数数组）会走 `except` 分支吗？

**参考答案**：**不会**。`np.array([1,2,3]).dtype` 是整数 dtype，`issubdtype(..., integer)` 为真，strings 正常返回，char 直接透传结果（见 multiply docstring 里 `i = np.array([1, 2, 3])` 的示例，得到 `['a' 'bb' 'ccc']`）。`except TypeError` 只在 `i` 非整数时才命中。

**练习 2**：为什么 char 的 `raise ValueError(...)` 没有写 `from`（比如 `raise ValueError(...) from None` 或 `from exc`）？这会有什么副作用？

**参考答案**：不写 `from` 时，Python 会自动把当前正在处理的 `TypeError` 作为 `__context__`（隐式链）挂上去，traceback 里会出现一句「During handling of the above exception, another exception occurred」。好处是调试时还能看到底层 strings 的 `TypeError`；代价是错误链更长。若想对外完全隐藏底层异常，应显式写 `raise ValueError(...) from None`。char 这里选择了保留隐式链。

---

### 4.3 本地包装·结果重组：partition / rpartition 用 np.stack 增加一维

#### 4.3.1 概念说明

`partition(a, sep)` 模仿 Python 的 `str.partition`：把每个字符串在**第一个** `sep` 处切成三段——「分隔符之前」「分隔符本身」「分隔符之后」；找不到 `sep` 时三段为「整个字符串」「空」「空」。`rpartition` 类似，只是从**最后一个** `sep` 切。

关键差异在**返回形式**：

- `numpy.strings.partition(a, sep)` 返回一个**长度为 3 的元组** `(before, sep_arr, after)`，其中每个元素都是**与输入同形状**的数组；
- `numpy.char.partition(a, sep)` 返回**一个数组**，形状是「输入形状 `+ (3,)`」——也就是在末尾多出一条长度为 3 的维度，沿这条维依次存放 before / sep / after。

为什么 char 要「换一种返回形式」？因为历史上 `chararray.partition`（以及 `np.char.partition`）就返回**单个数组**，每个元素是个 3 字段结构。迁移到委托 strings 后，strings 给的是三元组，char 必须把它**重组**回「单数组」形态才不破坏老代码。重组工具就是 `np.stack`。

> 形象地说：strings 把三段**并排摆在桌面上**（三个对象）；char 把它们**叠成一摞**（一个对象，多一维）。

#### 4.3.2 核心流程

设输入 `a` 的形状为 `S`（比如 `(1,)` 或 `(3, 2)`）。两条路径的形状演变：

```text
numpy.strings.partition(a, sep)
   └─ 返回 (before, sep_arr, after)   # 三个数组，每个形状都是 S
                                       #   （三段经广播后形状一致）

numpy.char.partition(a, sep)
   └─ return np.stack( (before, sep_arr, after), axis=-1 )
                                       # np.stack 把 N=3 个形状为 S 的数组
                                       #   沿一条新轴拼起来 → 形状 S + (3,)
```

`np.stack(arrays, axis=-1)` 的形状规则可用公式表达。若每个被堆叠数组形状为 \(S\)、共 \(N\) 个，则结果形状为：

\[
\text{shape}_{\text{out}} = S + (N,)
\]

本场景 \(N = 3\)，所以 char 版比 strings 版**多出一条长度为 3 的末维**。例如：

- 输入 `a` 形状 `(1,)` → strings 返回 3 个 `(1,)` 数组；char 返回形状 `(1, 3)` 的数组；
- 输入 `a` 形状 `(3, 2)` → strings 返回 3 个 `(3, 2)` 数组；char 返回形状 `(3, 2, 3)` 的数组。

`rpartition` 与 `partition` 的包装手法**完全相同**（都是 `np.stack(..., axis=-1)`），区别只在被委托的 strings 版是从「最后一个」`sep` 切。

#### 4.3.3 源码精读

先看 char 本地 `partition` 的函数体——同样只有一行：

[numpy/_core/defchararray.py:357](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L357) —— `return np.stack(strings_partition(a, sep), axis=-1)`。`strings_partition(a, sep)` 返回三元组，`np.stack` 把这三段沿 `axis=-1`（最末维）堆叠。完整函数与文档（含返回「an extra dimension with 3 elements per input element」的说明）见 [numpy/_core/defchararray.py:318-357](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L318-L357)。

`rpartition` 完全对称：

[numpy/_core/defchararray.py:401](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L401) —— `return np.stack(strings_rpartition(a, sep), axis=-1)`。完整函数见 [numpy/_core/defchararray.py:360-401](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L360-L401)。

再看被委托方「返回三元组」的契约，确认 `np.stack` 的输入确实「是 3 个形状相同的数组」：

[numpy/_core/strings.py:1556-1564](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/strings.py#L1556-L1564) —— `numpy.strings.partition` 的 `Returns` 段写明返回 **3-tuple**：before / separator / after 三段，每段都是字符串 dtype 数组。它的 docstring 示例（strings.py 第 1572–1577 行）也显示返回值是 `(array(['Numpy'],...), array([' '],...), array(['is nice!'],...))` 这样三个 `(1,)` 数组组成的元组。

[numpy/_core/strings.py:1580-1602](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/strings.py#L1580-L1602) —— 函数体：先把 `a`、`sep` 都 `np.asanyarray`，算出每段的缓冲区宽度，构造一个带 3 个字段（`f0/f1/f2`）的结构化数组 `out`，最后 `return _partition_index(a, sep, pos, out=(out["f0"], out["f1"], out["f2"]))`——返回的正是 `(f0, f1, f2)` 这个**三元组**，三段形状一致（都按广播后的 `shape`）。这正是 `np.stack` 能直接吃下的输入。

最后，看测试如何**断言** char 版「多一维」的形态：

[numpy/_core/tests/test_defchararray.py:490-497](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/tests/test_defchararray.py#L490-L497) —— `test_partition`：对一个 `chararray` 调 `.partition([b'3', b'M'])`，断言结果是嵌套列表 `tgt`，其中**每个最内层元素都是一个 3 元组**（如 `(b'12', b'3', b'45')`）。这正说明 char 版把 before/sep/after 收拢成了「末维长度为 3」的数组——若它像 strings 那样返回三元组，`assert_array_equal(P, tgt)` 就不可能用一个**三层嵌套**的 `tgt` 比对成功。`rpartition` 的对应断言见 [numpy/_core/tests/test_defchararray.py:571-578](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/tests/test_defchararray.py#L571-L578)。

> 补一条调用链：`chararray.partition` 方法（[defchararray.py:991-999](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L991-L999)）只是 `return asarray(partition(self, sep))`——它再委托给本讲的本地 `partition`，然后 `asarray` 包一层保证返回 `chararray`。所以「stack 出多一维」的真正发生地，仍是本讲的本地 `partition`。

#### 4.3.4 代码实践

**实践目标**：用 `partition` 文档里的官方示例输入，**对比** `np.char.partition` 与 `np.strings.partition` 的返回类型与形状，亲眼看到「char 版多出一个长度为 3 的维度」。

**操作步骤**：

```python
import numpy as np

x = np.array(["Numpy is nice!"])          # 形状 (1,)

# (1) strings 版：返回 3-tuple，每段是 (1,) 数组（见 strings.py docstring 示例）
sp = np.strings.partition(x, " ")
print("strings type :", type(sp).__name__, " len =", len(sp))   # tuple, 3
for i, arr in enumerate(sp):
    print(f"  seg{i} shape =", arr.shape, "value =", arr)        # 每段 (1,)

# (2) char 版：返回单个数组，形状 (1, 3)
cp = np.char.partition(x, " ")
print("char    type :", type(cp).__name__)
print("char    shape:", cp.shape, " dtype:", cp.dtype)           # (1, 3)
print(cp)                                                        # [['Numpy' ' ' 'is nice!']]

# (3) 形状公式自检：换一个 (2,3) 输入再看末维
y = np.array([["a-1", "b-2", "c-3"], ["d-4", "e-5", "f-6"]])     # 形状 (2,3)
print("char (2,3) ->", np.char.partition(y, "-").shape)          # 预期 (2, 3, 3)
```

**需要观察的现象**：第 (1) 步 `sp` 是 `tuple`、长度 3，三段形状都是 `(1,)`；第 (2) 步 `cp` 是 `numpy.ndarray`、形状 `(1, 3)`，与公式 \(S + (3,) = (1,) + (3,) = (1, 3)\) 吻合；第 (3) 步 `(2,3)` 输入得到 `(2, 3, 3)`。

**预期结果**：strings → `tuple len=3`，三段 `(1,)`；char → `shape (1, 3)`，值 `[['Numpy', ' ', 'is nice!']]`（dtype `<U8`，与 docstring 一致）；`(2,3)` 输入 → char 形状 `(2, 3, 3)`。char 版始终比 strings 版多一条长度为 3 的末维——这就是 `np.stack(..., axis=-1)` 的直接后果。

> 第 (1)(2) 步的预期值直接取自 `numpy.strings.partition` 与 `numpy.char.partition` 的 docstring 示例（项目内 doctest），属权威依据；可自行运行确认。第 (3) 步若现象不符记为「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：如果输入 `a` 形状为 `(4,)`，`np.char.partition(a, sep)` 和 `np.strings.partition(a, sep)` 的「结果形状」分别如何描述？

**参考答案**：strings 版返回一个三元组，其中**每个**数组形状都是 `(4,)`（共 3 个 `(4,)` 数组）；char 版返回**单个**数组，形状 `(4, 3)`——沿新增的末维存放 before/sep/after。用公式：char 形状 = \(S + (3,) = (4,) + (3,) = (4,3)\)。

**练习 2**：`np.char.partition` 用的是 `axis=-1`。若改成 `np.stack(..., axis=0)`，结果形状会变成什么？为什么 char 偏偏选 `axis=-1`？

**参考答案**：`axis=0` 时新增维度加在**最前**，形状会变成 `(3,) + S`，例如 `(1,)` 输入得到 `(3, 1)`——访问 `result[i]` 取到的是「第 i 段、全体元素」，语义不如「每个元素各自的三段」自然。选 `axis=-1` 使新增维度在**最末**，`result[k]` 直接给出「第 k 个输入元素的 before/sep/after 三段」，与「每个元素是一个 3 字段结构」的历史语义吻合（对照 `test_partition` 里每个最内层元素都是 3 元组的断言）。

**练习 3**：`partition`（找第一个 `sep`）与 `rpartition`（找最后一个 `sep`）在「找不到 `sep`」时，三段分别是什么？

**参考答案**：依 strings.py 的文档——`partition` 找不到时为「(整个字符串, 空, 空)」；`rpartition` 找不到时为「(空, 空, 整个字符串)」（注意整体字符串落在第**三**段）。char 版只是把这三种段 stack 起来，每段内容不变。

---

## 5. 综合实践

把本讲的两种包装手法串起来，做一个「解析 `key=value` 字符串」的小任务。

**任务**：给定一个形如 `["name=claude", "ver=2.5", "ok"]` 的字符串数组，用 `np.char.partition` 把每条按 `=` 切成 `(key, '=', value)`；再对 `value` 段用 `np.char.multiply` 重复若干次。要求：

1. 观察 `np.char.partition` 返回数组的形状与每行三段内容，解释为什么「找不到 `=` 的 `"ok"`」三段是 `('ok', '', '')`；
2. 取出 `value` 段（即 partition 结果的末维下标 `2`），用 `np.char.multiply(values, 2)` 把它重复两遍；
3. 全程**只**用 `np.char.*`；然后**改写**一遍：把 `np.char.partition` 换成 `np.strings.partition`、`np.char.multiply` 换成 `np.strings.multiply`，对比两版代码的返回形状与（当传错乘数时的）异常类型，亲手验证本讲两个核心结论。

**参考框架（示例代码，非项目原有）**：

```python
import numpy as np

a = np.array(["name=claude", "ver=2.5", "ok"])

# (1) char.partition：形状 (3, 3)，每行 (key, '=', value)
parts = np.char.partition(a, "=")
print(parts)                  # 形状 (3,3)
# 'ok' 找不到 '=' → ('ok', '', '')  （见 4.3.5 练习 3）

# (2) 取 value 段并重复两遍
values = parts[..., 2]        # 末维下标 2 → 形状 (3,)
print(np.char.multiply(values, 2))   # ['claudeclaude' '2.52.5' '']

# (3) 改用 strings 版对比
sp = np.strings.partition(a, "=")     # 返回三元组，每段 (3,)
print(type(sp), [s.shape for s in sp])  # tuple, [(3,), (3,), (3,)]
# strings.multiply 在非整数时抛 TypeError（char 版抛 ValueError）
```

**需要观察与解释**：

- `parts.shape == (3, 3)`——输入 `(3,)` + 末维 3，符合 \(S + (3,)\)；
- `parts[..., 2]`（`...` 代表前所有维）干净地取出 value 段，正是 `axis=-1` 带来的便利（呼应 4.3.5 练习 2）；
- `np.strings.partition` 返回三元组、每段 `(3,)`，而 `np.char.partition` 返回单数组 `(3,3)`——两种返回形式的差异一目了然；
- 若把第 (2) 步的乘数故意写成 `2.0`：`np.char.multiply(values, 2.0)` 抛 `ValueError`，`np.strings.multiply(values, 2.0)` 抛 `TypeError`——呼应 4.2 的错误类型转换。

> 综合实践里的代码是「示例代码」，目的是把本讲的形状公式与异常翻译串联验证；请实际运行并记录你环境下的输出，与上述预期对照，不符处记为「待本地验证」。

## 6. 本讲小结

- `multiply`、`partition`、`rpartition` 是 `defchararray` 里**本地定义**的薄包装，**不是** `numpy.strings` 同名函数的纯再导出——`np.char.multiply is np.strings.multiply` 为假，三者都只挂 `@set_module("numpy.char")`（无 `array_function_dispatch`），NEP-18 分发发生在被委托的 strings 层。
- 这三个函数能调到 strings 原版，靠的是顶部的**别名导入** `from numpy.strings import multiply as strings_multiply, ...`——「先留别名、再 `def` 同名、函数体调别名」，避免本地 `def` 覆盖原版后陷入无限递归。
- `multiply` 的包装手法是**错误类型转换**：`try: return strings_multiply(a, i) except TypeError: raise ValueError("Can only multiply by integers")`，把 strings 在「乘数非整数」时抛的 `TypeError` 翻译成 char 历史上的 `ValueError`，仅为向后兼容。
- `partition`/`rpartition` 的包装手法是**结果重组**：`return np.stack(strings_partition(a, sep), axis=-1)`，把 strings 返回的三元组（三个形状为 `S` 的数组）沿新增末维堆成单个形状 `S + (3,)` 的数组——这就是 char 版比 strings 版「多一个长度为 3 的维度」的根本原因。
- 两种手法可叠加理解：**委托 strings → 加工（改异常 / 改形状）→ 返回**。char 版对外维持的是「单数组返回 + ValueError 契约」的老接口，strings 版则是「三元组返回 + TypeError 契约」的现代接口。
- 测试侧，`test_partition`/`test_rpartition` 用「每个最内层元素是 3 元组」的嵌套断言，正好印证了 char 版「末维长度为 3」的形态。

## 7. 下一步学习建议

本讲把 u2-l2 标记的「本地包装」类彻底讲透，第二单元（模块机制与核心源码）到此结束。建议接下来进入第三单元：

- **u3-l1 chararray 的 ndarray 子类化机制**：本讲多次提到的 `chararray.partition` 方法（`asarray(partition(self, sep))`）只是 `chararray` 大量「方法委托」中的一员。u3-l1 会从 `__new__`/`__array_finalize__`/`__array_wrap__`/`__getitem__` 入手，讲清 `chararray` 作为 `ndarray` 子类的「取值自动 rstrip」「dtype 校验」「ufunc 输出包装」三件套——那是这些 `asarray(...)` 包裹之所以必要的根因。
- **u3-l2 运算符重载与方法委托**：系统看 `__add__`/`__mul__`/`__mod__` 等如何委托给本模块的自由函数（包括本讲的 `multiply`），把「方法委托设计」补全。
- **u3-l4 弃用迁移：从 chararray 到 numpy.strings**：本讲反复强调 char 版的「单数组返回 / ValueError」是为**向后兼容**而保留；u3-l4 给出把这些老接口**迁移**到 `numpy.strings`（三元组返回 / TypeError）的实操路径，并提示哪些差异需要在迁移时手动补偿（例如把 `np.char.partition` 的单数组结果改写成解包三元组）。

如果想立刻巩固本讲，可重读 [defchararray.py:312-315](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L312-L315) 与 [defchararray.py:357](https://github.com/numpy/numpy/blob/4e7f3b33df3e5ed2e9f46f6febdee62364520c70/numpy/_core/defchararray.py#L357) 两处函数体，确认你能不看书复述「try/except 翻译异常」与「np.stack 增加一维」这两个动作。
