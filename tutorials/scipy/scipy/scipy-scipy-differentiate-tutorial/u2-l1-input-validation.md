# 输入校验 `_derivative_iv`

## 1. 本讲目标

在 u1 系列里，我们已经会把 `derivative` 当作「黑盒」来调用、调参，并能通过返回对象 `_RichResult` 的 `success`/`status` 判断结果是否可信。从本讲开始，我们正式打开黑盒，进入**白盒源码阅读**阶段。

`derivative` 的函数体在真正开始迭代求导之前，做的第一件事就是把所有用户输入交给一个专门的内部函数 `_derivative_iv` 来**校验和标准化**。本讲的目标是：

1. 理解 SciPy「先校验、再处理」（validate-then-process）的工程范式，以及为什么要把校验单独抽成一个 `_iv` 函数。
2. 逐段读懂 `_derivative_iv` 对 `f`、`args`、`tolerances`、`maxiter`、`order`、`step_factor`、`step_direction`、`initial_step`、`preserve_shape`、`callback` 的校验逻辑。
3. 掌握 `atol`/`rtol` 的「默认值」其实并不在 `_derivative_iv` 里给出，而是在主流程里按 dtype 延迟计算的精妙设计。
4. 理解 `x`、`step_direction`、`initial_step` 三者是如何广播到同一形状，并被重命名为 `x`/`hdir`/`h0` 的。
5. 能够根据源码分支预测各种非法输入会抛出哪一条 `ValueError`。

> 本讲只讲**校验层**。`derivative` 主流程里真正的迭代求值（差分模板、权重、误差估计、终止条件）由后续讲义 u2-l2 ~ u2-l6 逐层展开。

## 2. 前置知识

阅读本讲前，你应当已经了解（u1 系列已建立）：

- **有限差分与步长**：用 \(f(x+h)\) 与 \(f(x-h)\) 的组合逼近 \(f'(x)\)，`h` 称为步长。
- **`derivative` 的关键参数**：`order`（差分公式阶数，默认 8）、`initial_step`（绝对起始步长，默认 0.5）、`step_factor`（每轮步长缩减倍数，默认 2.0）、`maxiter`（最大迭代次数，默认 10）。
- **收敛判据**：当误差估计满足

  \[ \text{error} < \text{atol} + \text{rtol}\cdot|df| \]

  时判定收敛；默认 `rtol` 约为精度的平方根，真导数为 0 的点需要手动设 `atol`。
- **返回对象 `_RichResult`** 与状态码 `0/-1/-2/-3/-4/1` 的含义。

本讲还会用到几个 NumPy/Python 基础概念，先用一句话解释：

- **`callable(obj)`**：判断 `obj` 是否「可被调用」（像函数一样加括号调用）。
- **`np.iterable(obj)`**：判断 `obj` 是否「可迭代」（能否放进 `for` 循环，例如 list/tuple/ndarray 可迭代，标量数字不可迭代）。
- **广播（broadcasting）**：把若干形状不同的数组按规则对齐到同一个公共形状，例如标量 `0` 与 `(3,)` 数组广播后都变成 `(3,)`。
- **dtype**：数组元素的数据类型，如 `float64`、`float32`。本讲会看到 `atol`/`rtol` 的默认值依赖于 dtype。

## 3. 本讲源码地图

本讲几乎全部聚焦于同一个文件：

| 文件 | 作用 |
| --- | --- |
| [`scipy/differentiate/_differentiate.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py) | 子包核心实现。本讲重点是其中的 `_derivative_iv`（第 11–57 行），以及它在 `derivative` 里的调用点（第 386–389 行）和默认容差的延迟计算（第 400–402 行）。 |
| [`scipy/differentiate/tests/test_differentiate.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py) | 测试文件。`test_input_validation`（第 329–371 行）把每一种非法输入到错误信息的对应关系固化成了断言，是我们验证理解的最佳参照。 |

> 小提示：`_derivative_iv` 名字里的 `_iv` 就是 **i**nput **v**alidation（输入校验）的缩写。SciPy 里很多函数都遵循「公开函数 + `_xxx_iv` 校验函数」的成对写法。

---

## 4. 核心概念与源码讲解

我们先看一下 `_derivative_iv` 的完整签名和它在 `derivative` 中是如何被调用的，建立整体印象。

函数签名接收 `derivative` 几乎全部的用户参数：

[_differentiate.py:11-12](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L11-L12) —— `_derivative_iv` 的形参就是 `derivative` 的全部用户参数。

`derivative` 主流程一进来就先调用它，再用解包（unpacking）把「标准化后的」返回值取出来：

[_differentiate.py:386-389](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L386-L389) —— 调用 `_derivative_iv` 并解包；注意解包时 `initial_step→h0`、`step_factor→fac`、`step_direction→hdir` 发生了重命名。

这一「调用 + 解包」就是「校验 + 标准化」的入口。接下来我们按三个最小模块拆开讲。

### 4.1 函数对象 `f` 与附加参数 `args` 的校验

#### 4.1.1 概念说明

`derivative` 最核心的输入是被求导的函数 `f`。由于算法要反复在 `f` 的不同点处取值，`f` 必须是**可调用对象**（callable）。如果用户误传了一个数字、字符串或 `None`，算法根本没有办法执行，所以要在最开始就拦下。

`args` 是「传给 `f` 的额外位置参数」。文档约定 `args` 应该是一个 **tuple**（例如 `args=(p,)`）。但用户常常会图省事直接传一个标量或单个数组（如 `args=p`）。SciPy 的做法是**宽容地把它规范化**：凡是不可迭代的，就包成单元素元组。这体现了「校验函数既校验、又标准化」的双重职责。

#### 4.1.2 核心流程

```text
传入 f, args
  │
  ├─ callable(f)?  否 → 抛 ValueError("`f` must be callable.")
  │
  └─ np.iterable(args)?  否 → args = (args,)   # 标量包成元组
                            是 → 保持原样
```

要点：

- `f` 的校验是**硬性**的：不可调用就直接报错。
- `args` 的处理是**柔性**的：不是报错，而是自动包装成元组，让后续代码可以统一用 `*args` 解包。

#### 4.1.3 源码精读

第一步是确定数组后端 `xp`（Array API 抽象，决定后续用 NumPy 还是 Torch/JAX 等），随后立刻校验 `f`：

[_differentiate.py:14-17](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L14-L17) —— 由 `x` 推断后端 `xp`；若 `f` 不可调用则抛出 `` `f` must be callable. ``

接着是 `args` 的规范化：

[_differentiate.py:19-20](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L19-L20) —— 不可迭代的 `args` 被包成单元素元组。

> 一个细节：Python 里字符串也是「可迭代」的（`np.iterable("ab")` 为 `True`），所以如果用户误传 `args="hello"`，这里不会报错也不会包装。这是已知的边界行为，正常使用中应直接传元组。

#### 4.1.4 代码实践

**实践目标**：确认 `f` 的校验报错信息，并观察 `args` 的自动包装行为。

**操作步骤**：

```python
import numpy as np
from scipy.differentiate import derivative

# (1) f 不可调用
try:
    derivative(None, np.asarray(1.0))
except ValueError as e:
    print("case f=None   ->", e)

# (2) 对比 args=p 与 args=(p,) 结果是否一致（说明自动包装起作用）
def f(x, p):
    return x ** p

x = np.array([1.0, 2.0, 3.0])
p = np.array([2.0, 3.0, 4.0])
r1 = derivative(f, x, args=p)        # 直接传数组，期望被自动包成 (p,)
r2 = derivative(f, x, args=(p,))     # 标准写法
print("args 自动包装后两者一致?", np.allclose(r1.df, r2.df))
```

**需要观察的现象**：

- 第 (1) 步应抛出 `ValueError`，信息为 `` `f` must be callable. ``。
- 第 (2) 步应打印 `True`，说明 `args=p` 被自动等价处理成了 `args=(p,)`。

**预期结果**：报错信息与源码第 16–17 行的分支一一对应；自动包装使两种写法等价。（如本地未装 SciPy，则「待本地验证」。）

#### 4.1.5 小练习与答案

**练习 1**：如果把一个「可调用但签名错误」的对象传给 `f`，例如 `derivative(42, np.asarray(1.0))`，会在 `_derivative_iv` 里报错吗？

> **答案**：会。`42` 不是 callable，命中第 16 行的分支，抛出 `` `f` must be callable. ``。注意：如果传的是一个**签名错误的可调用对象**（比如要求两个参数的函数），`_derivative_iv` **不会**发现——它只检查「能不能调用」，签名是否匹配要等到主流程真正调用 `f` 时才会暴露。

**练习 2**：`derivative(f, x, args=5)` 中的 `5` 会被怎么处理？

> **答案**：`np.iterable(5)` 为 `False`，于是 `args` 被改写为 `(5,)`，之后 `f` 会被以 `f(xi, 5)` 的形式调用。

---

### 4.2 容差 `tolerances` 与步长/迭代参数的标量校验

#### 4.2.1 概念说明

这一段是 `_derivative_iv` 里**最巧妙**的部分，集中校验四类「应当是非负标量」的数值参数：

- `tolerances`：一个字典，合法键是 `atol`（绝对容差）和 `rtol`（相对容差）。
- `step_factor`：步长每轮缩减倍数。
- `maxiter`：最大迭代次数（必须是正整数）。
- `order`：差分公式阶数（必须是正整数）。

这里有两个反直觉但很重要的设计：

1. **`atol`/`rtol` 的「默认值」不在本函数里给出。** 如果用户没指定，它们在这里保持 `None`，真正的默认值（`smallest_normal` 与 `sqrt(eps)`）要到主流程里、知道 dtype 之后才计算（见 4.2.3 末尾）。原因很简单：默认容差**依赖于数据精度**，而精度要到输入被 `_initialize` 提升后才能确定。
2. **用一个 3 元数组统一校验三个标量。** 源码把 `atol`、`rtol`、`step_factor` 打包进一个长度为 3 的数组，一次性检查「是否数值、是否非负、是否标量」。这是用 NumPy 的向量化比较来简化多个 `if`。

#### 4.2.2 核心流程

```text
tolerances = {} if None else tolerances
atol = tolerances.get('atol', None)   # 未指定 -> None（默认值延后计算）
rtol = tolerances.get('rtol', None)

tols = np.asarray([
    atol if atol is not None else 1,   # None 用占位符 1 顶替，避免破坏 dtype
    rtol if rtol is not None else 1,
    step_factor,
])

若 tols 满足下列任一条件 -> 抛 ValueError:
    - dtype 不是数值型（例如传了字符串 / object）
    - 任一元素 < 0
    - 任一元素为 NaN
    - shape != (3,)          # 例如 atol 传成了数组

maxiter_int = int(maxiter)
若 maxiter != maxiter_int 或 maxiter <= 0 -> 抛 ValueError

order_int = int(order)
若 order_int != order 或 order <= 0 -> 抛 ValueError
```

数学上，收敛判据是

\[ \text{error} < \text{atol} + \text{rtol}\cdot|\text{df}|, \]

所以 `atol`、`rtol` 都必须是**非负标量**；`step_factor` 出现在步长公式 \(h_{k+1}=h_k/\text{step\_factor}\) 中，同样要求非负（实际还隐含不应为 0，但校验只拦 `< 0`）。把它们三者合一检查，逻辑紧凑。

> 关于「`maxiter != maxiter_int`」这招：`int(1.5)=1`，而 `1 != 1.5` 为真，于是非整数 `1.5` 被拦下；但 `int(2.0)=2` 且 `2 == 2.0`，所以**整数值的浮点**（如 `2.0`）是被允许的，会被规整成 `2`。`order` 用同样的手法。

#### 4.2.3 源码精读

先取字典、抽取两个容差（注意默认是 `None`）：

[_differentiate.py:22-24](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L22-L24) —— `tolerances` 为 `None` 时置空字典；`atol`/`rtol` 缺省为 `None`。

然后是关键的「三合一」标量校验：

[_differentiate.py:26-34](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L26-L34) —— 把 `atol`/`rtol`/`step_factor` 打包成 3 元数组做统一校验；通过后把 `step_factor` 转成 `float`。

理解这段的三个窍门：

- **占位符 `1`**：若 `atol`/`rtol` 为 `None`，用 `1` 顶替。否则 `np.asarray([None, None, 2.0])` 会得到 `object` dtype，直接被判为「非数值」而误报。占位符让 `None` 能顺利通过校验、把「给默认值」的事推迟到后面。
- **`tols.shape != (3,)`**：如果用户把 `atol` 传成数组（例如长度为 2），打包结果就不是 `(3,)` 形状，从而被拦下——这就实现了「必须标量」的约束。
- **`np.issubdtype(tols.dtype, np.number)`**：传字符串（如 `rtol='ekki'`）或 `object()` 会让 dtype 不是数值型，被拦下。

随后是 `maxiter` 与 `order` 的「正整数」校验：

[_differentiate.py:36-42](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L36-L42) —— `maxiter` 与 `order` 都要求是正整数；非整数或 `<=0` 即报错。

最后，关于「默认容差延后计算」——这是本模块最值得记住的一点。`_derivative_iv` 让 `atol`/`rtol` 以 `None` 的形式返回，真正的默认值在主流程里、dtype 确定后才填上：

[_differentiate.py:400-402](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L400-L402) —— 默认 `atol = finfo.smallest_normal`，默认 `rtol = finfo.eps**0.5`，均依赖 dtype。

> 也就是说：`float64` 下默认 `atol≈2.2e-308`（极小，几乎「能多严就多严」）、`rtol≈1.5e-8`（即 \(\sqrt{\varepsilon}\)，\(\varepsilon\approx2.2\times10^{-16}\)）；`float32` 下二者都更松。这正是 u1 讲到的「默认 `rtol` 约为精度平方根」的真正来源。

#### 4.2.4 代码实践

**实践目标**：用不同非法输入触发本模块的各个分支，核对错误信息与测试用例一致。

**操作步骤**：

```python
import numpy as np
from scipy.differentiate import derivative

one = np.asarray(1.0)

for name, kwargs, expected in [
    ("atol=-1",        dict(tolerances=dict(atol=-1)),        "Tolerances and step parameters must be non-negative scalars."),
    ("rtol='ekki'",    dict(tolerances=dict(rtol='ekki')),    "Tolerances and step parameters must be non-negative scalars."),
    ("step_factor=object()", dict(step_factor=object()),      "Tolerances and step parameters must be non-negative scalars."),
    ("maxiter=1.5",    dict(maxiter=1.5),                     "`maxiter` must be a positive integer."),
    ("maxiter=0",      dict(maxiter=0),                       "`maxiter` must be a positive integer."),
    ("order=1.5",      dict(order=1.5),                       "`order` must be a positive integer."),
    ("order=0",        dict(order=0),                         "`order` must be a positive integer."),
]:
    try:
        derivative(lambda x: x, one, **kwargs)
        print(f"{name:24s} -> 没有报错（意外！）")
    except ValueError as e:
        ok = expected in str(e)
        print(f"{name:24s} -> {e}   [{'符合预期' if ok else '不符合预期'}]")
```

**需要观察的现象**：每一行都应打印「符合预期」，且错误信息与源码分支一一对应。

**预期结果**：上面 7 个用例分别命中第 26–34 行（前 3 个）、第 36–38 行（maxiter 两个）、第 40–42 行（order 两个）。这些用例与官方测试 [`test_input_validation`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L345-L363) 完全对应。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `tolerances=dict(rtol='ekki')` 报的是「Tolerances and step parameters must be non-negative scalars」，而不是某个单独关于 `rtol` 的错误？

> **答案**：因为 `atol`/`rtol`/`step_factor` 是**打包成一个数组统一校验**的。`rtol='ekki'` 会让数组的 dtype 变成字符串型，命中 `not np.issubdtype(tols.dtype, np.number)`，所以共用同一条报错信息。

**练习 2**：`derivative(f, x, maxiter=2.0)` 会报错吗？为什么？

> **答案**：不会。`int(2.0)=2`，且 `2 == 2.0` 为真，所以 `maxiter != maxiter_int` 为假；`2.0 > 0` 也成立。于是 `2.0` 被规整成整数 `2` 通过校验。这是「整数值浮点被接受」的刻意设计。

**练习 3**：如果用户不传 `tolerances`，`atol`/`rtol` 在 `_derivative_iv` 内是什么值？默认数值在哪里被填上？

> **答案**：在 `_derivative_iv` 内均为 `None`（见第 23–24 行）。真正的默认数值 `finfo.smallest_normal` 与 `finfo.eps**0.5` 在主流程第 400–402 行、dtype 确定后才填上，因为它们依赖数据精度。

---

### 4.3 步长方向/初始步长的广播与 `preserve_shape` / `callback` 校验

#### 4.3.1 概念说明

本模块处理两类剩余的输入：

- **几何/数值类**：`step_direction`（步长方向）与 `initial_step`（起始步长）。它们都可以是**逐元素数组**，并且必须和 `x` 一起广播到同一形状。
- **布尔/回调类**：`preserve_shape`（必须是 `True`/`False`）与 `callback`（必须是 `None` 或可调用）。

广播的意义在于「逐元素自适应」。比如 `x` 是 `(3,)` 数组时，我们可能希望左、中、右三种差分方向**同时**计算——这时传 `step_direction=[-1,0,1]`，它和 `x` 广播后得到 `(3,)` 的方向数组。`_derivative_iv` 用 `xp.broadcast_arrays` 一次性把三者对齐。

> 命名约定：校验完成后，`initial_step` 被重命名为 **`h0`**（初始步长）、`step_direction` 被重命名为 **`hdir`**（步长方向）、`step_factor` 被重命名为 **`fac`**（缩减因子）。这些短名是后续整个迭代主流程里使用的内部记号。

#### 4.3.2 核心流程

```text
step_direction = xp.asarray(step_direction)
initial_step   = xp.asarray(initial_step)
(x, step_direction, initial_step) = xp.broadcast_arrays(x, step_direction, initial_step)
# 广播后三者形状一致

preserve_shape 必须是 True 或 False，否则报错
callback 为 None 或可调用，否则报错

返回标准化元组：
(f, x, args, kwargs, atol, rtol, maxiter_int, order_int,
 initial_step, step_factor, step_direction, preserve_shape, callback)
```

注意：`step_direction` 的**语义**（0=中心、负=非正步、正=非负步）在这里并不校验，只是把它变成数组并广播；符号化（`sign`）和分类成 `il/ic/ir`（左/中/右）是主流程里的事（第 411–425 行）。

#### 4.3.3 源码精读

广播三者为同形状：

[_differentiate.py:44-47](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L44-L47) —— `step_direction`/`initial_step` 转数组，再与 `x` 一起广播到公共形状。

`preserve_shape` 与 `callback` 的校验：

[_differentiate.py:49-54](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L49-L54) —— `preserve_shape` 必须属于 `{True, False}`；`callback` 非空时必须可调用。

> Python 细节：`preserve_shape not in {True, False}` 用的是集合成员判断。由于 `True==1`、`False==0`，传 `1` 或 `0` 会被当作合法值（不会报错）；传 `'herring'`、`2` 等才会报错。这是潜在的边界行为，实际使用请显式传布尔值。

最后是返回标准化后的元组：

[_differentiate.py:56-57](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L56-L57) —— 返回 13 个标准化后的值；调用方在第 388–389 行解包并完成 `initial_step→h0`、`step_factor→fac`、`step_direction→hdir` 的重命名。

补充一点上下文（属于主流程，不属于 `_derivative_iv`，但能帮助理解 `hdir`/`h0` 的去向）：主流程随后会把 `hdir`/`h0` 再按最终 `shape` 广播、展平，并对 `h0<=0` 的位置填 `NaN`（因为非正的初始步长无意义）：

[_differentiate.py:411-417](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L411-L417) —— 主流程对 `hdir`/`h0` 做最终的形状对齐与无效值处理。

#### 4.3.4 代码实践

**实践目标**：验证 `preserve_shape`/`callback` 的报错，并观察 `step_direction` 数组与 `x` 的广播效果。

**操作步骤**：

```python
import numpy as np
from scipy.differentiate import derivative

one = np.asarray(1.0)

# (1) preserve_shape 非法
try:
    derivative(lambda x: x, one, preserve_shape='x')
except ValueError as e:
    print("preserve_shape='x' ->", e)

# (2) callback 非法
try:
    derivative(lambda x: x, one, callback='not callable')
except ValueError as e:
    print("callback 非法      ->", e)

# (3) step_direction 数组与 x 广播：同时算 [-1,0,1] 三种方向
x = np.array([1.0])
hdir = np.array([-1, 0, 1])     # 与标量 x 广播 -> 形状 (3,)
res = derivative(np.exp, x, step_direction=hdir, order=4, maxiter=1)
print("step_direction 广播后 res.x.shape =", res.x.shape)   # 期望 (3,)
print("res.x =", res.x)                                       # 期望 [1. 1. 1.]
```

**需要观察的现象**：

- (1) 抛出 `` `preserve_shape` must be True or False. ``
- (2) 抛出 `` `callback` must be callable. ``
- (3) `res.x.shape` 为 `(3,)`，说明标量 `x` 被广播成了与 `step_direction` 一致的 3 元数组。

**预期结果**：报错信息与第 49–54 行的分支一一对应；广播使三种差分方向在一次调用里同时计算。（如本地未装 SciPy，则「待本地验证」。）

#### 4.3.5 小练习与答案

**练习 1**：`derivative(lambda x: x, one, preserve_shape=1)` 会报错吗？

> **答案**：不会。`1 in {True, False}` 在 Python 中为 `True`（因 `True==1`），所以 `preserve_shape not in {True, False}` 为假，不报错。这是集合成员判断的边界行为，但规范用法应传布尔值。

**练习 2**：为什么 `_derivative_iv` 不在这里把 `step_direction` 的 `0/-1/1` 转成 `sign` 并分类？

> **答案**：`_derivative_iv` 的职责是「校验 + 标准化形状」，`sign` 化与左/中/右分类（`il/ic/ir`）依赖于最终广播后的 `shape`，所以放到主流程第 411–425 行去做。这体现了「校验层只管合法性，语义处理交给主流程」的分层。

**练习 3**：`initial_step`、`step_factor`、`step_direction` 在解包后分别被重命名为什么？

> **答案**：分别重命名为 `h0`、`fac`、`hdir`（见第 388–389 行）。这些是后续迭代主流程使用的内部短名。

---

## 5. 综合实践

把本讲贯穿起来，做一个**「非法输入 → 源码分支」的对照实验**，并补一个「合法但需理解」的广播用例。

**实践目标**：验证你能根据 `_derivative_iv` 的源码，准确预测每种非法输入抛出的 `ValueError`；并理解 `step_direction`/`initial_step` 的逐元素广播。

**操作步骤**：

```python
import numpy as np
from scipy.differentiate import derivative

one = np.asarray(1.0)

# 第一部分：非法输入 -> 预测源码分支
cases = [
    # (描述, kwargs, 预期错误信息, 命中的源码行)
    ("step_factor=-1",     dict(step_factor=-1),       "Tolerances and step parameters must be non-negative scalars.", "26-34"),
    ("maxiter=0",          dict(maxiter=0),            "`maxiter` must be a positive integer.",                       "36-38"),
    ("order=1.5",          dict(order=1.5),            "`order` must be a positive integer.",                         "40-42"),
    ("preserve_shape='x'", dict(preserve_shape='x'),   "`preserve_shape` must be True or False.",                     "49-51"),
]
print("=== 非法输入对照 ===")
for name, kw, expected, line in cases:
    try:
        derivative(lambda x: x, one, **kw)
        print(f"{name:22s} 未报错（意外！）")
    except ValueError as e:
        print(f"{name:22s} 命中第 {line} 行 -> {e}  [{'OK' if expected in str(e) else 'MISMATCH'}]")

# 第二部分：合法的逐元素广播
print("\n=== 逐元素广播 ===")
x = np.array([1.0])                          # 标量点
h0 = np.array([0.5, 0.5, 0.5])              # 逐元素初始步长
hdir = np.array([-1, 0, 1])                  # 左/中/右三种方向
res = derivative(np.exp, x, initial_step=h0, step_direction=hdir, order=4)
print("res.df.shape =", res.df.shape)        # 期望 (3,)
print("res.df       =", res.df)              # 三者都应接近 e^1 ≈ 2.71828
```

**需要观察的现象**：

- 第一部分四条全部打印 `OK`，且每条错误信息精确对应到 `_derivative_iv` 的相应代码行。
- 第二部分 `res.df.shape` 为 `(3,)`，三个方向的导数估计都接近 \(e^1\approx 2.71828\)，说明 `x`、`initial_step`、`step_direction` 成功广播到 `(3,)` 并逐元素求导。

**预期结果**：非法输入分支一一对应；广播用例形状正确、数值合理。把第一部分的四条用例与官方测试 [`test_input_validation`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L329-L371) 对照，会发现它们是同一套校验逻辑的体现。（如本地未装 SciPy，则「待本地验证」。）

---

## 6. 本讲小结

- `_derivative_iv` 是 `derivative` 的**输入校验 + 标准化**函数，遵循 SciPy「先校验、再处理」的范式；主流程第一行就调用它（第 386–389 行）。
- 它**硬性校验**：`f` 必须可调用、`maxiter`/`order` 必须是正整数、`tolerances`/`step_factor` 必须是非负标量、`preserve_shape` 必须是布尔、`callback` 必须可调用。
- 它**柔性标准化**：把不可迭代的 `args` 包成元组；把 `x`/`step_direction`/`initial_step` 广播到同形状；把整数值浮点（如 `2.0`）规整为整数。
- 最巧妙的是「**3 元数组合一校验**」（第 26–34 行）以及用**占位符 `1`** 让 `None` 容差安全通过校验。
- `atol`/`rtol` 的**默认值不在本函数给出**：它们以 `None` 返回，真正默认值 `smallest_normal` 与 `sqrt(eps)` 依赖 dtype，延后到第 400–402 行才计算。
- 返回时完成重命名：`initial_step→h0`、`step_factor→fac`、`step_direction→hdir`，这些短名是后续迭代主流程的内部记号。

## 7. 下一步学习建议

校验层读完后，`derivative` 拿到的都是「干净、标准化」的输入。接下来建议：

1. **u2-l2 有限差分权重 `_derivative_weights`**：理解 `h0`/`fac`/`hdir` 是如何参与构造中心差分与单侧差分模板的，以及 Taylor 展开 / Vandermonde 方程组如何导出权重。
2. **u2-l3 迭代求值点生成 `pre_func_eval`**：看首轮 `order` 个新点、后续每轮 2 个新点的「嵌套 stencil」是如何用 `h0`/`fac` 生成的。
3. 顺带阅读官方测试 [`test_input_validation`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L329-L371)，它把本讲的所有分支都固化成了断言，是复习本讲的最佳材料。
