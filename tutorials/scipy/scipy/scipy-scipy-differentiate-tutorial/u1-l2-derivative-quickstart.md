# derivative 快速上手与关键参数

## 1. 本讲目标

本讲承接 [u1-l1 子包概览](u1-l1-overview-and-setup.md)，我们已经知道 `scipy.differentiate` 对外暴露三个函数，而 `derivative` 是它们当中最基础的那个——`jacobian` 和 `hessian` 都建立在它之上。

学完本讲后，你应该能够：

1. 正确调用 `derivative(f, x)`，并知道它返回什么。
2. 说清楚 `order`、`initial_step`、`step_factor`、`maxiter`、`tolerances` 这几个核心参数各自控制什么，以及它们的默认值。
3. 从返回结果对象中读出导数估计 `df`、误差估计 `error`，并据此判断结果是否可信。

本讲是「黑盒使用」阶段：我们暂时不关心 `derivative` 内部是怎么算的（那是 u2 的任务），只要会用、会读结果。

---

## 2. 前置知识

### 2.1 什么是有限差分求导

如果你不知道函数 `f` 的解析表达式，但能计算它在任意点的函数值，就可以用「有限差分（finite difference）」来近似导数。最朴素的中心差分公式是：

\[
f'(x) \approx \frac{f(x+h) - f(x-h)}{2h}
\]

其中 \(h\) 是一个很小的步长。直觉上：导数就是「函数值随自变量的变化率」，我们在 \(x\) 左右各取一个点，用割线斜率去逼近切线斜率。

### 2.2 步长与误差的两难

步长 \(h\) 不是越小越好：

- \(h\) **太大**：割线和切线差得远，这叫**截断误差（truncation error）**，来源于公式本身只是近似。
- \(h\) **太小**：\(f(x+h)\) 和 \(f(x-h)\) 几乎相等，相减时浮点数的有效数字会大量抵消，这叫**消去误差（subtractive cancellation error）**。

所以存在一个「最佳步长」。`derivative` 的核心思想就是：**从较大的步长开始，逐步缩小步长，观察估计值何时稳定下来**——稳定的那一刻就是最佳步长附近。

### 2.3 差分公式的「阶数」

中心差分 \(\frac{f(x+h)-f(x-h)}{2h}\) 的截断误差是 \(O(h^2)\)，称为**二阶**公式。如果用更多点（比如 \(-2h, -h, h, 2h\)），可以构造出 \(O(h^4)\)、\(O(h^8)\) 等更高阶的公式。阶数越高，步长每缩小一次，误差下降得越快——但代价是第一轮要在更多个点上求值。

> 一个关键术语：`derivative` 里的参数 `order` 指的就是这个**差分公式的阶数**，不是「求几阶导数」。`derivative` 只算一阶导数。

---

## 3. 本讲源码地图

本讲只涉及一个源码文件，但我们会聚焦在它的**对外接口**部分，不深入实现细节。

| 文件 | 本讲关注的内容 |
|------|---------------|
| `scipy/differentiate/_differentiate.py` | `derivative` 函数的签名与默认值、各参数的 docstring 说明、`np.exp` 示例、返回对象 `df`/`error` 的定义 |

永久链接 base：

```
https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/
```

---

## 4. 核心概念与源码讲解

### 4.1 derivative 签名与默认值

#### 4.1.1 概念说明

`derivative` 是一个「逐元素（elementwise）」的数值求导函数：你可以传一个标量 `x`，也可以传一个数组 `x`，它会对数组里**每一个元素**独立地估计该点处的导数，并返回一个同样形状的结果。它的工作对象是「黑盒函数」——你只要给它一个能算函数值的 `callable`，不需要解析导数公式。

#### 4.1.2 核心流程

从用户视角看，一次调用只有三步：

1. **准备**：提供函数 `f` 和求导点 `x`，可选地调整 `order`、`initial_step` 等参数（不调就用默认值）。
2. **迭代**：函数内部从大步长开始，逐轮缩小步长、重新估计导数，直到满足收敛条件或达到 `maxiter`。
3. **返回**：返回一个类似字典的结果对象，最常用的两个属性是 `df`（导数估计）和 `error`（误差估计）。

#### 4.1.3 源码精读

先看函数签名和所有参数的默认值。注意 `f` 和 `x` 之后的所有参数都是**关键字参数**（`*` 之后），调用时必须用 `参数名=值` 的形式：

[_differentiate.py:L67-L69 — derivative 的完整签名与默认值](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L67-L69)

```python
def derivative(f, x, *, args=(), kwargs=None, tolerances=None, maxiter=10,
               order=8, initial_step=0.5, step_factor=2.0,
               step_direction=0, preserve_shape=False, callback=None):
```

把这行默认值整理成一张表，本讲我们重点理解加粗的几个：

| 参数 | 默认值 | 一句话作用 |
|------|--------|-----------|
| `f` | （必填） | 待求导的函数，签名 `f(xi, *args) -> ndarray` |
| `x` | （必填） | 求导点（标量或数组） |
| `args` | `()` | 传给 `f` 的额外位置参数 |
| `tolerances` | `None` | 收敛容差字典 `{atol, rtol}` |
| **`maxiter`** | `10` | 最多迭代多少轮 |
| **`order`** | `8` | 差分公式的阶数 |
| **`initial_step`** | `0.5` | 第一轮的最大步长 |
| **`step_factor`** | `2.0` | 每轮步长缩小的倍数 |
| `step_direction` | `0` | 差分方向（0=中心，正/负=单侧） |
| `preserve_shape` | `False` | 控制 `f` 收到的数组形状契约 |
| `callback` | `None` | 每轮迭代后调用的回调 |

关于 `f` 必须满足的契约，docstring 里有明确要求——它是**逐元素**的，且不能修改输入数组：

[_differentiate.py:L81-L90 — f 的签名与逐元素契约](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L81-L90)

> 通俗理解：`f` 必须满足 `f(xi)[j] == f(xi[j])`，即「对整个数组调用」和「对每个元素单独调用」结果一致。`np.sin`、`np.exp`、`lambda x: x**3` 这类向量化函数天然满足。

#### 4.1.4 代码实践

**实践目标**：跑通最简单的一次调用，确认环境和返回值。

**操作步骤**：

```python
import numpy as np
from scipy.differentiate import derivative

f = np.exp          # 待求导函数
res = derivative(f, 1.0)   # 求 exp 在 x=1 处的导数
print(res.df)       # 导数估计
print(res.error)    # 误差估计
print(type(res))    # 看看返回的是什么类型
```

**需要观察的现象**：

- `res.df` 应该非常接近 `np.exp(1.0) ≈ 2.718281828`（因为 `exp` 的导数就是它本身）。
- `res.error` 是一个非常小的正数（量级约 `1e-11`）。
- `type(res)` 是 `scipy._lib._util._RichResult`，一个类似字典的对象，可以用 `res.df` 点取属性。

**预期结果**：`res.df ≈ 2.718281828...`，`res.error` 在 `1e-11` 量级。（精确数值待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：`derivative` 的参数里，`f` 和 `x` 之后为什么有个 `*`？它对调用方式有什么影响？

> **答案**：`*` 表示后面的参数都是**仅限关键字参数（keyword-only）**。这意味着调用时不能按位置传，必须写 `derivative(f, x, order=2)` 而不是 `derivative(f, x, 2)`。这样设计是为了避免用户记错参数顺序——这些参数太多，按位置传容易出错。

**练习 2**：如果不传任何可选参数，`derivative` 用的是几阶差分公式？

> **答案**：`order` 默认值是 `8`，所以默认用 8 阶差分公式。

---

### 4.2 关键参数含义

#### 4.2.1 概念说明

`derivative` 的「自适应」本质，是由几个参数共同驱动的：用多大的步长起步、每轮步长缩多少、最多迭代几轮、什么时候算收敛。理解这四个参数，你就掌握了调参的全部杠杆。

#### 4.2.2 核心流程

迭代过程（摘自 docstring 的 Notes）可以概括为：

1. **第 1 轮**：用最大步长 `initial_step`，套用一个 `order` 阶的差分公式估计导数。
2. **后续每轮**：把最大步长**除以** `step_factor`，重新估计导数。
3. **误差估计**：`error` = 当前估计与上一轮估计之差的绝对值。
4. **停止**：当 `error < atol + rtol * |df|`，或达到 `maxiter`，或遇到其他终止条件。

一个漂亮的性质：在步长还没小到触发消去误差之前，每轮误差大约缩小为上一轮的 \(1/\text{step\_factor}^{\text{order}}\)。例如 `step_factor=2`、`order=4` 时，每轮误差约乘以 \(1/2^4 = 0.0625\)。

\[ \text{每轮误差衰减比} \approx \frac{1}{\text{step\_factor}^{\,\text{order}}} \]

#### 4.2.3 源码精读

**`order`：差分公式阶数。** 奇数会被向上取整到偶数：

[_differentiate.py:L114-L116 — order 参数说明](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L114-L116)

> 阶数越高，单轮精度越高、收敛越快，但第 1 轮要在更多点（`order+1` 个）上求值。默认 `order=8` 是精度与开销的折中。

**`initial_step`：起始步长。** 默认 `0.5`：

[_differentiate.py:L117-L119 — initial_step 参数说明](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L117-L119)

> 这是**绝对步长**。一个重要陷阱（见 Notes）：当 \(|x|\) 很大时（比如 `1e20`），步长 `0.5` 相对 \(x\) 太小，浮点数根本分辨不出 `x` 和 `x+0.5`，这时必须**调大** `initial_step`。

**`step_factor`：每轮步长缩小倍数。** 默认 `2.0`：

[_differentiate.py:L120-L125 — step_factor 参数说明](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L120-L125)

> 注意：实际第 1 轮用的步长是 `initial_step / step_factor`（即 `0.5/2 = 0.25`）。如果 `step_factor < 1`，后续步长会**变大**而不是变小——这在「小步长会触发消去误差、想刻意避开小步长」时有用。

**`maxiter`：最大迭代轮数。** 默认 `10`：

[_differentiate.py:L112-L113 — maxiter 参数说明](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L112-L113)

> 这是兜底保护：即使没收敛，最多也只跑这么多轮。

**`tolerances`：收敛容差。** 这是一个字典：

[_differentiate.py:L102-L111 — tolerances 参数与收敛判据](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L102-L111)

> 收敛判据是 `error < atol + rtol * |df|`。默认值很有讲究：
> - `atol` 默认是「该 dtype 的最小正规数」（float64 约 `2.2e-308`，几乎为零）。
> - `rtol` 默认是「该 dtype 精度的平方根」（float64 约 \(\sqrt{2.2\times10^{-16}} \approx 1.5\times10^{-8}\)）。
>
> 也就是说，默认情况下 `atol` 几乎不起作用，主要由 `rtol * |df|` 把关，要求误差降到导数值的约 `1e-8` 量级。Notes 还提醒：**当真导数恰好为 0（鞍点）时**，`rtol * |df|` 趋近 0，默认容差极难满足，这时要手动给一个 `atol`（如 `1e-12`）。

#### 4.2.4 代码实践

**实践目标**：直观感受 `order` 对收敛速度的影响。

**操作步骤**：

```python
import numpy as np
from scipy.differentiate import derivative

f = np.exp
# 强制跑满 3 轮，不让它提前收敛，便于观察每轮误差
for order in [2, 4, 8]:
    res = derivative(f, 1.0, order=order, maxiter=3,
                     tolerances=dict(atol=0, rtol=0))
    print(f"order={order}: df={res.df:.10f}, error={res.error:.2e}")
```

**需要观察的现象**：`order` 越大，第 3 轮的 `error` 越小（因为每轮衰减比 \(1/\text{step\_factor}^{\text{order}}\) 更小）。注意，由于强制不收敛（`atol=0, rtol=0`），`res.status` 会是 `-2`（达到 maxiter），但 `df` 仍然是当前最好的估计。

**预期结果**：三个 `df` 都接近 `2.718281828`；`error` 随 `order` 增大而显著减小。（精确数值待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：默认情况下，`derivative(np.exp, 1.0)` 实际第一轮用的步长是多少？

> **答案**：`initial_step / step_factor = 0.5 / 2.0 = 0.25`。

**练习 2**：为什么 docstring 说「当真导数恰好为 0 时，默认容差很难满足」？

> **答案**：收敛判据是 `error < atol + rtol*|df|`。真导数为 0 时，估计值 `df` 会非常接近 0，于是 `rtol*|df|` 几乎为 0；而默认 `atol` 也几乎是 0。两边都接近 0，判据变得极其苛刻，很难满足。解决办法是显式给一个 `atol`，如 `tolerances=dict(atol=1e-12)`。

**练习 3**：如果把 `step_factor` 设成 `0.5`，后续轮次的步长会怎么变？为什么 docstring 说这「可能有用」？

> **答案**：`step_factor=0.5 < 1`，所以每轮步长 `h/step_factor` 会**变大**。这看似反直觉，但当函数在很小的步长下会因消去误差而失真时，刻意只用较大的步长、避免进入消去误差区，反而能得到更稳的结果。

---

### 4.3 第一个示例与结果读取

#### 4.3.1 概念说明

`derivative` 返回的不是单个数字，而是一个 `_RichResult` 对象。这个对象像字典又像命名元组：你可以用 `res.df` 点取属性，也可以用 `res['df']` 索引。本模块教你读懂它最重要的几个属性。

#### 4.3.2 核心流程

返回对象的主要属性（标量 `x` 时是标量，数组 `x` 时是同形数组）：

| 属性 | 含义 |
|------|------|
| `df` | 导数估计（最核心的输出） |
| `error` | 误差估计（当前估计与上轮估计之差） |
| `success` | 布尔，是否成功收敛（`status==0`） |
| `status` | 整数状态码（0=收敛，-1=误差回升，-2=达 maxiter，-3=遇非有限值，-4=回调终止） |
| `nit` | 实际迭代轮数 |
| `nfev` | `f` 被求值的点数 |
| `x` | 求导点（广播后） |

> 状态码的完整含义会在 [u1-l3 结果对象与状态码](u1-l3-result-object-and-status.md) 详讲，本讲你只要知道「`success=True` 就可信」即可。

#### 4.3.3 源码精读

先看 docstring 给出的官方示例——对 `np.exp` 在一组点上求导，并对比「误差估计」与「真实误差」：

[_differentiate.py:L243-L260 — np.exp 示例：df、error 与真实误差](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L243-L260)

```python
>>> f = np.exp
>>> df = np.exp  # true derivative
>>> x = np.linspace(1, 2, 5)
>>> res = derivative(f, x)
>>> res.df          # approximation of the derivative
>>> res.error       # estimate of the error
>>> abs(res.df - df(x))  # true error
```

这个例子同时打印了三样东西，值得仔细体会：

1. `res.df` —— 算出来的导数估计，长度 5（因为 `x` 长度 5）。
2. `res.error` —— **算法自己估的**误差，来源于「相邻两轮估计之差」。
3. `abs(res.df - df(x))` —— **真实误差**（因为我们知道 `exp` 的真导数就是 `exp`）。

关键观察：`res.error`（约 `1e-11`）通常比真实误差（约 `1e-14`）**大一两个量级**——这是正常的，因为误差估计偏保守才安全。它保证「真实误差几乎总是小于 `error`」，所以用 `error` 做收敛判据是稳妥的。

关于 `df` 和 `error` 这两个返回属性的官方定义：

[_differentiate.py:L183-L189 — df 与 error 的返回定义](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L183-L189)

> 注意 `error` 的定义原文：*"the magnitude of the difference between the current estimate of the derivative and the estimate in the previous iteration"*——即当前与上一轮估计之差的绝对值。

关于「向量化」的说明也很重要——`derivative` 可以同时对一个数组 `x` 求导，并且每个元素**独立**收敛（精度需求高的元素会多算几轮）：

[_differentiate.py:L293-L295 — 实现对 x/step_direction/args 向量化](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L293-L295)

#### 4.3.4 代码实践（本讲主实践任务）

**实践目标**：用 `derivative` 计算 \(f(x)=x^3\) 在 \(x=2\) 处的导数，分别用 `order=2` 和 `order=8`，比较精度并读懂返回对象。

**前置计算**：\(f'(x)=3x^2\)，所以 \(f'(2)=3\times 4 = 12\)。这就是用来对照的解析值。

**操作步骤**：

```python
import numpy as np
from scipy.differentiate import derivative

f = lambda x: x**3
true = 12.0  # 解析导数 3*x**2 在 x=2

for order in [2, 8]:
    res = derivative(f, 2.0, order=order)
    print(f"--- order={order} ---")
    print(f"df      = {res.df}")            # 导数估计
    print(f"true    = {true}")              # 解析值
    print(f"|df-12| = {abs(float(res.df) - true):.2e}")  # 真实误差
    print(f"error   = {float(res.error):.2e}")  # 算法自估误差
    print(f"success = {res.success}, status={res.status}, nit={res.nit}, nfev={res.nfev}")
```

**需要观察的现象**：

1. 两个 `order` 的 `df` 都应该非常接近 `12`（比如 `11.9999...` 或 `12.0000...`）。
2. `order=8` 通常 `nit`（迭代轮数）更少就能收敛，因为高阶公式每轮精度提升更快。
3. `error`（自估）一般略大于真实误差 `|df-12|`，体现了保守估计。
4. 正常收敛时 `success=True`、`status=0`。

**预期结果**：两种 `order` 下 `df` 都 ≈ `12.0`，真实误差为很小的数；`order=8` 收敛所需轮数不多于 `order=2`。（各数值的精确位数待本地验证。）

**延伸思考**：把 `order=2` 改成 `order=3`（奇数），观察结果是否与 `order=2` 接近——验证 docstring 所说「奇数阶会被向上取整到偶数」。再试试把 `x` 换成数组 `np.array([1.0, 2.0, 3.0])`，看 `res.df` 的形状是否也是长度 3。

#### 4.3.5 小练习与答案

**练习 1**：在 `np.exp` 示例里，为什么 `res.error`（约 `1e-11`）比真实误差（约 `1e-14`）大？

> **答案**：`error` 是算法用「相邻两轮估计之差」做的**保守上界估计**，故意偏大以保证安全；真实误差只有在我们知道解析导数时才能算出来。算法在不知道真值的情况下，宁可高估误差也不会低估。

**练习 2**：返回对象里 `nit` 和 `nfev` 有什么区别？

> **答案**：`nit` 是**迭代轮数**（算法缩小步长、重新估计的次数）；`nfev` 是 `f` 被**求值的点数**（函数调用涉及的总采样点）。由于「嵌套模板」复用旧函数值，`nfev` 的增长通常慢于「每轮全新采样」的情况（详见 u2）。

**练习 3**：调用 `derivative(f, np.array([1.0, 2.0, 3.0]))` 后，`res.df` 的形状是什么？

> **答案**：形状是 `(3,)`。`derivative` 逐元素工作，输出形状与输入 `x` 一致。

---

## 5. 综合实践

把本讲学的「参数 + 结果读取」串起来：写一个小脚本，对同一个函数 `f(x) = x * np.sin(x)` 在 `x = np.linspace(1, 5, 5)` 上求导，**系统地对比不同参数组合**的效果。

```python
import numpy as np
from scipy.differentiate import derivative

# 解析导数：f'(x) = sin(x) + x*cos(x)
f = lambda x: x * np.sin(x)
df_true = lambda x: np.sin(x) + x * np.cos(x)
x = np.linspace(1, 5, 5)

print(f"{'order':>5} {'maxiter':>8} {'max|true_err|':>14} {'max_error':>12} {'all_success':>12}")
for order in [2, 4, 8]:
    for maxiter in [3, 10]:
        res = derivative(f, x, order=order, maxiter=maxiter)
        true_err = np.abs(res.df - df_true(x))
        print(f"{order:>5} {maxiter:>8} {true_err.max():>14.2e} "
              f"{np.max(res.error):>12.2e} {bool(np.all(res.success)):>12}")
```

**你要回答的问题**：

1. 在 `maxiter=3` 时，`order=8` 的真实误差是否明显小于 `order=2`？这验证了什么？
2. 把 `maxiter` 从 3 提到 10，`order=2` 的误差是否显著下降？`success` 是否都变成 `True`？
3. 观察 `res.error` 这一列与真实误差列的大小关系，是否符合「自估误差偏保守」？

> 这个练习同时调用了 `order`、`maxiter`，并练习了读取 `df`、`error`、`success`，覆盖了本讲全部最小模块。（运行输出的精确数值待本地验证。）

---

## 6. 本讲小结

- `derivative(f, x)` 对（向量化）黑盒函数逐元素估计一阶导数，返回 `_RichResult` 对象。
- 关键参数：`order`（差分公式阶数，默认 8）、`initial_step`（起始步长，默认 0.5）、`step_factor`（每轮步长缩小倍数，默认 2.0）、`maxiter`（最多迭代轮数，默认 10）、`tolerances`（收敛容差字典 `{atol, rtol}`）。
- 实际第一轮步长 = `initial_step / step_factor`；在步长未触发消去误差前，每轮误差约衰减为 \(1/\text{step\_factor}^{\text{order}}\)。
- 收敛判据是 `error < atol + rtol*|df|`；默认 `atol`≈0、`rtol`≈精度平方根；真导数为 0 时需手动设 `atol`。
- 读结果：`res.df` 是导数估计，`res.error` 是保守的误差上界估计（相邻两轮估计之差），`res.success`/`status` 表示是否收敛。
- 大 \(|x|\) 时默认 `initial_step=0.5` 太小无法分辨，需调大起始步长。

---

## 7. 下一步学习建议

本讲你已经会「黑盒」使用 `derivative` 并读懂返回对象。接下来：

1. **[u1-l3 结果对象与状态码](u1-l3-result-object-and-status.md)**：系统学习 `status` 的 5 种取值（0/-1/-2/-3/-4）和 `success` 的判断，学会诊断「为什么我的求导没收敛」。
2. 在进入 u2 白盒剖析之前，建议先用本讲的参数组合多跑几个函数（三角函数、指数、多项式），积累「什么参数适合什么函数」的直觉。
3. u2 起我们将打开 `_differentiate.py` 的内部实现，从输入校验、差分权重推导、求值点生成，一直到收敛判断，逐步讲清 `derivative` 是怎么把这些参数用起来的。
