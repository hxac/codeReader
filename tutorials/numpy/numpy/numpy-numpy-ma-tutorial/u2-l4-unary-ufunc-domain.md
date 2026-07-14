# 掩码一元运算与域(domain)

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `numpy.ma` 里 `sqrt`、`log`、`arctan` 这类一元函数是「怎么被改造成掩码版本」的，即 `_MaskedUnaryOperation` 这个包装器做了什么。
- 解释「域(domain)」这个概念：它如何把数学上非法的输入（如对负数开平方、对零取对数）在**运算之前**就转成 mask，从而让结果是被「屏蔽」而不是产生 `nan`/`inf`。
- 读懂 `_MaskedUnaryOperation.__call__` 的执行流程，并能指出最终结果 mask 由**三个来源**按位或得到。
- 解释 `core.py` 里随处可见的 `with np.errstate(...)` 为什么必须存在——即「屏蔽位上的垃圾值不应该刷屏 RuntimeWarning」。
- 区分两条调用路径：`ma.sqrt(a)` 走 `__call__`，而 `np.sqrt(a)`（`a` 是掩码数组）走 `__array_wrap__`，二者共用同一份 `ufunc_domain` / `ufunc_fills` 注册表。

## 2. 前置知识

本讲假设你已经掌握下面这些概念（来自前置讲义）：

- **掩码数组三件套**：`data`（全部原始值，含坏值）、`mask`（同形状布尔数组，`True` 表示屏蔽，无屏蔽时压缩为单例 `nomask`）、`fill_value`。见 u1-l4。
- **`nomask` 单例**：代表「无屏蔽」的省内存标记，就是 `False`；全库用 `is nomask` 做 O(1) 身份判断。见 u2-l1。
- **`getdata` / `getmask`**：模块级取值函数。`getmask(a)` 忠实返回内部 `_mask`（可能为 `nomask`）。见 u1-l4、u2-l1。
- **MaskedArray 是 ndarray 子类**，重写了 `__array_wrap__` 这个 ufunc 钩子；`__array_wrap__` 用 `mask_or` 合并输入 mask、用 `ufunc_domain` 做域屏蔽。见 u2-l2。

补充一个本讲会用到的、你可能不熟悉的术语：

- **ufunc（universal function）**：NumPy 里「对数组每个元素做相同运算」的统一抽象，如 `np.sqrt`、`np.add`。它既支持标量也支持多维数组，底层是 C 实现的 `umath` 模块。本讲讲的就是「如何把一个普通 ufunc 包成掩码版」。
- **定义域（domain of a function）**：一个函数「输入合法、能算出实数结果」的取值范围。比如实数平方根的定义域是 \([0, +\infty)\)，对数的定义域是 \((0, +\infty)\)。

## 3. 本讲源码地图

本讲全部内容集中在 `numpy/ma/core.py` 一个文件里：

| 代码位置 | 作用 |
|---|---|
| `ufunc_domain` / `ufunc_fills` 两个字典（core.py:844-845） | 全局注册表：记录每个 ufunc 对应的「域检查器」和「填充值」。`__call__` 与 `__array_wrap__` 都查它。 |
| `_DomainCheckInterval` / `_DomainTan` / `_DomainSafeDivide` / `_DomainGreater` / `_DomainGreaterEqual`（core.py:848-941） | 五个「域检查器」类，每个都是可调用对象，输入数据、返回「哪些位置非法」的布尔数组。 |
| `_MaskedUFunc` / `_MaskedUnaryOperation`（core.py:944-1026） | 包装器基类与一元包装器。`__call__` 是本讲的主角。 |
| 一元 ufunc 注册区（core.py:1250-1287） | 把 `umath.sqrt` 等逐个包成掩码版并赋值给模块级名字 `sqrt`/`log`/`arctan`… |
| `MaskedArray.__array_wrap__`（core.py:3143-3201） | 另一条调用路径：当你写 `np.sqrt(masked_array)` 时，NumPy 走这个钩子，同样查 `ufunc_domain`。 |

## 4. 核心概念与源码讲解

### 4.1 一元掩码 ufunc 的包装器：`_MaskedUnaryOperation.__call__`

#### 4.1.1 概念说明

在原生 NumPy 里，`np.sqrt(np.array([-1, 0, 1]))` 会返回 `[nan, 0., 1.]` 并顺带打一条 `RuntimeWarning: invalid value encountered in sqrt`。原因很简单：实数平方根在负数处没有定义，IEEE 754 只能用 `nan` 表示「算不出来」。

`numpy.ma` 的设计哲学是：**与其让一个坏值（`nan`）污染整组数据，不如在它产生的那一刻就贴上「屏蔽」标签**。于是 `ma` 把每个一元 ufunc 都用一个小类包起来，这个包装器在调用真正运算的同时，顺手把「算不出来的位置」标进 mask 里。这个包装器就是 `_MaskedUnaryOperation`，它的实例被赋值给模块级名字 `sqrt`、`log`、`arctan` 等——所以你 `from numpy.ma import sqrt` 拿到的并不是 `umath.sqrt`，而是它的掩码版包装器。

包装器要回答三个问题：

1. **真正算**：对 `data` 调用原始 ufunc 得到 `result`。
2. **算 mask**：哪些位置该屏蔽？
3. **收拾结果**：屏蔽位置的 `result.data` 该填什么？返回类型是什么？

#### 4.1.2 核心流程

`__call__(a, *args, **kwargs)` 的执行流程可以用下面这段伪代码概括：

```
d = getdata(a)                      # 取出原始数据（含坏值）
result = 原始ufunc(d)                # 先照常算，可能产生 nan/inf

if 该函数配了 domain:
    m = 结果里非有限值(~isfinite)      # 来源①：算出来是 nan/inf
    m |= domain(d)                  # 来源②：输入落在定义域外
else:
    m = 空
m |= getmask(a)                     # 来源③：输入本来就被屏蔽

if result 是标量:
    return masked if m else result

# result 是数组：
把 d 的值拷回 result 的被屏蔽位置   # 见 4.1.3 的 copyto
return result 包装成 MaskedArray，挂上 mask=m
```

最关键的一句是 mask 的三合一。对于一个「带域」的函数（如 `sqrt`），最终某个位置被屏蔽，当且仅当下面三条**至少满足一条**：

\[
m_i \;=\; \neg\,\mathrm{isfinite}\bigl(f(d_i)\bigr) \;\vee\; \mathrm{domain}(d_i) \;\vee\; \mathrm{inputmask}_i
\]

- 来源①「结果非有限」：兜底捕获任何漏网的 `nan`/`inf`。
- 来源②「输入越域」：在**运算之前**就知道这是非法输入（如负数开平方），提前屏蔽。
- 来源③「输入已屏蔽」：屏蔽性会传染——坏的进去，坏的出来。

#### 4.1.3 源码精读

先看包装器基类与一元包装器的定义：

[core.py:944-952 `_MaskedUFunc`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L944-L952) 把原始 ufunc 存到 `self.f`，并复制其 `__doc__`/`__name__`/`__qualname__`，让包装器对外看起来就像原函数。

[core.py:973-978 `_MaskedUnaryOperation.__init__`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L973-L978) 接收三个参数：被包装的 `mufunc`、`fill`（填充值，默认 0）、`domain`（域检查器，默认 `None`），并把后两者写进两张全局注册表 `ufunc_domain[mufunc]=domain` 和 `ufunc_fills[mufunc]=fill`。这两张表是「`ma.sqrt` 路径」与「`np.sqrt` 路径」共享的秘密通道——4.3 节会展开。

真正干活的是 `__call__`，分两支（带域 / 不带域）：

[core.py:980-1002 `__call__` 的「算结果 + 算 mask」](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L980-L1002)：

```python
d = getdata(a)
if self.domain is not None:
    with np.errstate(divide='ignore', invalid='ignore'):
        result = self.f(d, *args, **kwargs)
    m = ~umath.isfinite(result)   # ① 结果非有限
    m |= self.domain(d)           # ② 输入越域
    m |= getmask(a)               # ③ 输入已屏蔽
else:
    with np.errstate(divide='ignore', invalid='ignore'):
        result = self.f(d, *args, **kwargs)
    m = getmask(a)                # 不带域：只传染输入 mask
```

注意 `m = ~umath.isfinite(result)` 产生的是一个**新**数组，所以后面的 `m |= ...` 是原地按位或，安全无副作用。当 `getmask(a)` 返回 `nomask`（即 `False`）时，`m |= False` 什么也不做，正好。

[core.py:1004-1008 标量结果快路径](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1004-L1008)：当 `result` 是 0 维（标量）时，要么返回全局 `masked` 单例（被屏蔽），要么直接返回裸结果。这是 `ma.sqrt(ma.masked) is ma.masked` 成立的原因。

[core.py:1010-1026 数组结果的「回填 + 包装」](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1010-L1026)：

```python
if m is not nomask:
    try:
        np.copyto(result, d, where=m)   # 把输入值拷回被屏蔽位置
    except TypeError:
        pass
masked_result = result.view(get_masked_subclass(a))
masked_result._mask = m
masked_result._update_from(a)
return masked_result
```

`np.copyto(result, d, where=m)` 这一步很微妙：它把**原始输入** `d` 的值拷回到结果里被屏蔽的位置。也就是说，走 `ma.sqrt` 这条路径时，屏蔽位置的 `.data` 显示的是**原始输入值**（例如 `sqrt(-1)` 的结果 `data` 是 `-1`，而不是 `nan` 也不是 0）。源码注释也坦白承认这在 Python 层有点笨拙（C 里直接跳过就行）：

> `# We need to fill the invalid data back w/ the input Now, that's plain silly...`

最后用 `get_masked_subclass(a)` 决定返回类型（保证子类类型传播，见 u3-l2），挂上 mask，再 `_update_from(a)` 把 `fill_value` 等簿记属性搬过来。

> **与 `__array_wrap__` 的关系（承接 u2-l2）**：上面这条 `__call__` 路径，只有当你**显式调用掩码版**（如 `ma.sqrt(a)`、`from numpy.ma import sqrt`）时才走。如果你写的是 `np.sqrt(a)`（`a` 是 MaskedArray），NumPy 用的是另一条路：先对裸数据算 `umath.sqrt`，再回调 [`MaskedArray.__array_wrap__`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3143-L3201)。两条路用的是**同一份** `ufunc_domain`/`ufunc_fills` 注册表，所以域屏蔽行为一致；但 `__array_wrap__` 用 `fill` 值填充越域位置（见 4.3），而 `__call__` 用输入值回填——这是两路径在 `.data` 上的唯一实质差异。

#### 4.1.4 代码实践

**实践目标**：亲眼看到 `ma.sqrt` 把负数位置屏蔽掉，并验证屏蔽位置的 `.data` 是原始输入值。

**操作步骤**：

```python
import numpy as np
import numpy.ma as ma

a = ma.masked_array([-1, 0, 1, 2], mask=[0, 0, 0, 0])   # 全部未屏蔽
r = ma.sqrt(a)           # 走 _MaskedUnaryOperation.__call__
print(r)
print("data  :", r.data)   # 关注 [0] 位置
print("mask  :", r.mask)
```

**需要观察的现象**：

- `r` 打印出来第一个元素是 `--`（被屏蔽）。
- `r.data[0]` 是 `-1`（原始输入被 `copyto` 回填），**不是** `nan`、也**不是** `0.0`。
- `r.mask` 是 `[True, False, False, False]`：只有负数位置被屏蔽，`0` 和正数都正常。
- 整个过程**没有** `RuntimeWarning`（因为 `errstate` 抑制了，见 4.4）。

**预期结果**：

```
masked_array(data=[--, 0.0, 1.0, 1.4142135623730951],
             mask=[ True, False, False, False],
       fill_value=1e+20)
data  : [-1.  0.  1.  1.41421356]
mask  : [ True False False False]
```

如果 `r.data[0]` 不是 `-1`，请回头对照 [core.py:1019](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1019) 的 `np.copyto(result, d, where=m)`。

#### 4.1.5 小练习与答案

**练习 1**：把上面例子里的 `a` 第 3 个位置（值为 `2`）预先屏蔽（`mask=[0,0,1,0]`），再 `ma.sqrt(a)`。请问结果里哪些位置被屏蔽？为什么？

**参考答案**：位置 0（负数，来源②域）和位置 2（输入已屏蔽，来源③传染）都会被屏蔽。位置 2 的输入 `2` 本可以正常开平方，但因为输入被屏蔽，结果也跟着屏蔽——这正是 `m |= getmask(a)` 的作用。

**练习 2**：`ma.sqrt(ma.masked)` 返回什么？为什么？

**参考答案**：返回全局单例 `masked`。因为对单元素屏蔽标量求平方根，`result` 是 0 维、`m` 为真，命中 [core.py:1004-1008](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1004-L1008) 的标量快路径 `if m: return masked`。`tests/test_core.py:1300` 的 `assert np.sqrt(np.ma.masked) is np.ma.masked` 正是验证这一点。

---

### 4.2 域(domain)：把数学上的非法输入自动转为 mask

#### 4.2.1 概念说明

上一节里 `self.domain(d)` 是个「黑盒」——给它数据，返回「哪些位置非法」的布尔数组。本节把这个黑盒打开。

「域检查器」是一个**可调用对象**（实现了 `__call__` 的类实例），它编码了某个函数的**数学定义域**。设计成「对象」而不是「函数」的好处是：可以带参数（比如区间端点、容差），并统一塞进 `ufunc_domain` 注册表。

`core.py` 提供了五个域检查器类，覆盖 `ma` 里所有带域的一元函数：

| 类 | 判定「非法」的条件 | 用于 |
|---|---|---|
| `_DomainGreaterEqual(v)` | \(x < v\) | `sqrt`（\(x<0\)）、`arccosh`（\(x<1\)） |
| `_DomainGreater(v)` | \(x \leq v\) | `log`/`log2`/`log10`（\(x\leq 0\)） |
| `_DomainCheckInterval(a,b)` | \(x < a\) 或 \(x > b\) | `arcsin`/`arccos`（\([-1,1]\)）、`arctanh`（\((-1,1)\)） |
| `_DomainTan(eps)` | \(|\cos x| < \mathrm{eps}\) | `tan`（接近渐近线） |
| `_DomainSafeDivide(tol)` | \(|a|\cdot\mathrm{tol} \geq |b|\) | 二元除法（见 u2-l5） |

注意 `sqrt` 和 `log` 用了**不同**的类：`sqrt` 的定义域是 \([0,+\infty)\)（含 0），所以用 `_DomainGreaterEqual(0.0)` 判 \(x<0\)；`log` 的定义域是 \((0,+\infty)\)（不含 0），用 `_DomainGreater(0.0)` 判 \(x\leq 0\)。`log(0)` 在数学上是 \(-\infty\)，属于非法，应屏蔽。

#### 4.2.2 核心流程

每个域检查器的 `__call__` 都遵循同一个套路：**用 `umath` 的比较运算算出布尔数组，并用 `errstate` 包住**。以 `_DomainCheckInterval(a, b)` 为例，它返回 `True`（非法）当且仅当：

\[
x < a \;\;\text{或}\;\; x > b
\]

即 \(x\) 落在合法区间 \([a, b]\) 之外。构造时会自动把端点排好序（`if a > b: (a,b)=(b,a)`），所以传 `(1, -1)` 和 `(-1, 1)` 等价。

`_DomainTan(eps)` 稍特殊：`tan(x)` 在 \(x = \pi/2 + k\pi\) 处发散（趋向无穷），而发散点恰好是 \(\cos x = 0\)。所以它用 \(|\cos x| < \mathrm{eps}\) 来判定「太接近渐近线、结果不可信」，把这些点屏蔽掉。

#### 4.2.3 源码精读

[core.py:848-870 `_DomainCheckInterval`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L848-L870)：

```python
def __init__(self, a, b):
    if a > b:
        (a, b) = (b, a)
    self.a = a
    self.b = b

def __call__(self, x):
    with np.errstate(invalid='ignore'):
        return umath.logical_or(umath.greater(x, self.b),
                                umath.less(x, self.a))
```

返回的是「\(x > b\) 或 \(x < a\)」的布尔数组——即「落在 \([a,b]\) 之外」。

[core.py:912-925 `_DomainGreater`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L912-L925) 与 [core.py:928-941 `_DomainGreaterEqual`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L928-L941) 分别返回 `x <= v` 和 `x < v`。二者只差一个等号，却决定了 `log(0)` 被屏蔽而 `sqrt(0) = 0` 不被屏蔽。

[core.py:873-888 `_DomainTan`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L873-L888)：用 `umath.less(umath.absolute(umath.cos(x)), self.eps)` 判定「余弦绝对值太小」。

注意每个 `__call__` 都套了 `with np.errstate(...)`——这是因为被检查的数据里可能含有 `nan`（屏蔽位上的垃圾值），而 `nan` 参与比较会触发 `invalid value` 警告。详见 4.4 节。

#### 4.2.4 代码实践

**实践目标**：直接调用域检查器，观察它返回的布尔数组，建立「域 = 布尔谓词」的直觉。

**操作步骤**：

```python
import numpy as np
from numpy.ma.core import _DomainGreaterEqual, _DomainGreater, _DomainCheckInterval

x = np.array([-2, -1, 0, 0.5, 1, 2])

sqrt_domain = _DomainGreaterEqual(0.0)      # sqrt 合法域 [0, +inf)
log_domain  = _DomainGreater(0.0)           # log  合法域 (0, +inf)
asin_domain = _DomainCheckInterval(-1, 1)   # arcsin 合法域 [-1, 1]

print("sqrt 非法:", sqrt_domain(x))
print("log  非法:", log_domain(x))
print("asin 非法:", asin_domain(x))
```

**需要观察的现象**：

- `sqrt 非法`：`[True, True, False, False, False, False]`（负数非法，`0` 合法）。
- `log 非法`：`[True, True, True, False, False, False]`（负数**和 0** 都非法）。
- `asin 非法`：`[True, True, False, False, False, True]`（只有 \(|x|>1\) 非法）。

**预期结果**：与上面一致。重点对比 `sqrt` 和 `log` 在 `x=0` 处的差异——它正是 `_DomainGreaterEqual` 与 `_DomainGreater` 一字之差的体现。

> 说明：`_DomainGreaterEqual` 等是带下划线前缀的「内部」类，正常使用时你**不需要**直接 import 它；这里仅为教学演示。生产代码请直接用 `ma.sqrt`、`ma.log`。

#### 4.2.5 小练习与答案

**练习 1**：`ma.log(ma.array([0.0]))` 的结果是什么？mask 是 `True` 还是 `False`？

**参考答案**：结果被屏蔽（`mask=[True]`）。因为 `log` 配的是 `_DomainGreater(0.0)`，它判定 \(x \leq 0\) 为非法，而 `0.0 \leq 0` 成立，所以 `0` 被域屏蔽。对比 `ma.sqrt(ma.array([0.0]))` 不被屏蔽（`sqrt(0)=0` 合法）。

**练习 2**：为什么 `arctanh` 用的是 `_DomainCheckInterval(-1.0 + 1e-15, 1.0 - 1e-15)` 而不是 `(-1.0, 1.0)`？端点为什么要缩进 `1e-15`？

**参考答案**：`arctanh(x)` 的定义域是开区间 \((-1, 1)\)，在 \(x = \pm 1\) 处发散到 \(\pm\infty\)。理论上应用 `_DomainCheckInterval(-1, 1)` 判 \(|x|>1\)。但浮点数在 \(|x|\) 极接近 1 时，`arctanh` 的结果已经大到不可靠（数值上接近溢出），所以端点向内缩进 `1e-15`，把「极度接近发散」的点也一并屏蔽，避免返回无意义的巨大值。这是数值稳定性与数学严格性之间的工程取舍。

---

### 4.3 umath 包装清单：`sqrt` / `log` / `arctan` 是怎么注册的

#### 4.3.1 概念说明

知道了包装器（4.1）和域检查器（4.2），剩下的问题就是：`ma` 里到底有哪些一元函数被包成了掩码版？各自的 `fill` 和 `domain` 是什么？答案就在 `core.py` 的一段「注册表」里——一行一个，清晰明了。

这段代码分成两组：

- **不带域**的（`domain=None`）：`exp`、`sin`、`cos`、`arctan`、`sinh`、`absolute`、`negative`、`floor`、`ceil`… 这些函数在整个实数域都有定义（或对 `nan` 自然产生 `nan`），不需要额外的域检查，只传染输入 mask。
- **带域**的：`sqrt`、`log`/`log2`/`log10`、`tan`、`arcsin`、`arccos`、`arccosh`、`arctanh`——这些函数在某些输入处没有实数定义，必须配域检查器。

#### 4.3.2 核心流程

注册的逻辑极其简单：构造一个 `_MaskedUnaryOperation` 实例，把它赋值给模块级名字。构造时传入三个东西：

```
名字 = _MaskedUnaryOperation(原始ufunc, fill, domain)
```

- `原始ufunc`：如 `umath.sqrt`，被存进 `self.f`，真正运算时调用它。
- `fill`：填充值，存进全局表 `ufunc_fills[umath.sqrt]`。
- `domain`：域检查器，存进全局表 `ufunc_domain[umath.sqrt]`。

`arctan` 是「不带域」的典型：它的定义域是全体实数，所以连 `fill` 和 `domain` 都不传，用默认值 `fill=0, domain=None`。而 `sqrt` 是「带域」的典型，三个参数都给齐。

#### 4.3.3 源码精读

[core.py:1250-1267 不带域的一元 ufunc](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1250-L1267)：

```python
# Unary ufuncs
exp = _MaskedUnaryOperation(umath.exp)
sin = _MaskedUnaryOperation(umath.sin)
cos = _MaskedUnaryOperation(umath.cos)
arctan = _MaskedUnaryOperation(umath.arctan)          # 不带域，定义域=全体实数
abs = absolute = _MaskedUnaryOperation(umath.absolute)
# ... 其余省略
```

[core.py:1269-1287 带域的一元 ufunc](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1269-L1287)：

```python
# Domained unary ufuncs
sqrt  = _MaskedUnaryOperation(umath.sqrt,  0.0, _DomainGreaterEqual(0.0))
log   = _MaskedUnaryOperation(umath.log,   1.0, _DomainGreater(0.0))
log2  = _MaskedUnaryOperation(umath.log2,  1.0, _DomainGreater(0.0))
log10 = _MaskedUnaryOperation(umath.log10, 1.0, _DomainGreater(0.0))
tan   = _MaskedUnaryOperation(umath.tan,   0.0, _DomainTan(1e-35))
arcsin  = _MaskedUnaryOperation(umath.arcsin,  0.0, _DomainCheckInterval(-1.0, 1.0))
arccos  = _MaskedUnaryOperation(umath.arccos,  0.0, _DomainCheckInterval(-1.0, 1.0))
arctanh = _MaskedUnaryOperation(umath.arctanh, 0.0,
                                _DomainCheckInterval(-1.0 + 1e-15, 1.0 - 1e-15))
```

现在可以回答本讲实践任务里那个问题了：**`sqrt = _MaskedUnaryOperation(umath.sqrt, 0.0, ...)` 里的 `fill=0.0` 是干什么的？**

它被 [`__init__`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L973-L978) 写进 `ufunc_fills[umath.sqrt] = 0.0`。注意：**在 `__call__` 里它根本没被用到**（`__call__` 回填用的是输入 `d`）。它真正的消费者是另一条路径 [`__array_wrap__`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3173-L3185)：

```python
if d.any():
    try:
        fill_value = ufunc_fills[func][-1]   # 二元域：取末位
    except TypeError:
        fill_value = ufunc_fills[func]       # 一元域：直接用（sqrt 就是 0.0）
    np.copyto(result, fill_value, where=d)   # 越域位置填 0.0
```

也就是说，当你写 `np.sqrt(masked_array)`（而不是 `ma.sqrt(...)`）时，NumPy 走 `__array_wrap__`，它会把**越域位置**（负数）的 `result.data` 填成 `0.0`，而不是留下 `nan`。`0.0` 就是 `sqrt` 这个函数的「官方安全占位值」——选 `0.0` 是因为它对平方根而言是个无害的、合法的值。类似地 `log` 选 `1.0`（因为 \(\log(1)=0\)，是个「干净」的占位）。

一句话总结 `fill` 的角色：**它是给 `np.sqrt(掩码数组)` 这条 numpy 原生派发路径，以及 reduce/accumulate 场景准备的填充值；`ma.sqrt` 这条 `__call__` 路径不用它。**两张全局表 `ufunc_domain`/`ufunc_fills` 是两条路径的共享契约。

#### 4.3.4 代码实践

**实践目标**：对比「不带域」的 `arctan` 与「带域」的 `sqrt`，体会域的区别；并验证 `fill` 参数在 `np.sqrt`（走 `__array_wrap__`）路径下确实生效。

**操作步骤**：

```python
import numpy as np
import numpy.ma as ma

a = ma.masked_array([-1, 0, 1], mask=[0, 0, 0])

# (1) 带域 vs 不带域
print("ma.sqrt :", ma.sqrt(a))      # -1 被屏蔽
print("ma.arctan:", ma.arctan(a))   # 全部有值，arctan 无域限制

# (2) fill 参数：用 np.sqrt 走 __array_wrap__ 路径
r_np = np.sqrt(a)                   # numpy 原生派发 -> __array_wrap__
print("np.sqrt data:", r_np.data)   # 关注 [0]：应是 fill=0.0，而非 -1 或 nan
print("np.sqrt mask:", r_np.mask)
```

**需要观察的现象**：

- `ma.arctan([-1,0,1])` 三个位置都有值（`-π/4, 0, π/4`），无屏蔽——因为 `arctan` 没配域。
- `ma.sqrt` 与 `np.sqrt` 的 **mask 完全一致**（都是 `[True, False, False]`）——两条路径共用 `ufunc_domain`。
- 但二者 `.data[0]` **不同**：`ma.sqrt`（`__call__`）回填输入 `-1`；`np.sqrt`（`__array_wrap__`）填 `fill=0.0`。

**预期结果**：

```
ma.sqrt :  [--, 0.0, 1.0]              # data[0] = -1（输入回填）
ma.arctan: [-0.78539816, 0.0, 0.78539816]
np.sqrt data: [0.  0.  1.]             # data[0] = 0.0（fill 填充）
np.sqrt mask: [ True False False]
```

> 两条路径 mask 一致、`.data` 在屏蔽位不同的现象，若你在本地复现时数值与本表有出入（例如不同 NumPy 版本对 `np.sqrt` 派发的实现差异），以源码 [core.py:1019](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1019)（`__call__` 回填 `d`）与 [core.py:3185](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3185)（`__array_wrap__` 填 `fill_value`）为准。`mask` 一致这一点是稳定的，可作为验证锚点。

#### 4.3.5 小练习与答案

**练习 1**：`ma.arcsin(ma.array([0.5, 2.0, -2.0]))` 的结果是什么？哪些位置被屏蔽？

**参考答案**：`arcsin` 配的是 `_DomainCheckInterval(-1.0, 1.0)`，判定 \(|x|>1\) 为非法。`0.5` 合法（\(\arcsin(0.5)=\pi/6\)），`2.0` 和 `-2.0` 越域被屏蔽。结果形如 `[0.52359877, --, --]`，mask `[False, True, True]`。

**练习 2**：如果我想新增一个掩码版的 `umath.cbrt`（立方根，定义域为全体实数，无需域），照注册表的风格应该怎么写？

**参考答案**：立方根处处有定义，属于「不带域」一族，仿照 `arctan` 那行即可：

```python
cbrt = _MaskedUnaryOperation(umath.cbrt)
```

不传 `fill` 和 `domain`，用默认 `fill=0, domain=None`。注意这是**示例代码**（NumPy 当前 `ma` 命名空间并未导出 `cbrt`），仅用于说明注册风格。

---

### 4.4 errstate：屏蔽位为何不会刷屏 RuntimeWarning

#### 4.4.1 概念说明

如果你直接对含 `nan`/负数的普通数组开平方，NumPy 会打印 `RuntimeWarning: invalid value encountered in sqrt`。但在 `ma` 里，即使输入有负数，`ma.sqrt` 也**静悄悄**地返回结果。本节解释这个「静悄悄」是怎么做到的，以及为什么必须这么做。

关键工具是 `np.errstate`——NumPy 的浮点错误处理上下文管理器。`with np.errstate(invalid='ignore'):` 包住的代码块里，「非法值」类警告被临时关闭，离开代码块后恢复原设置。

#### 4.4.2 核心流程

`ma` 在两类地方用了 `errstate`：

1. **算 ufunc 结果时**（`__call__` 的 991、1000 行）：算 `sqrt(负数)`、`log(0)` 必然触发 `invalid`/`divide` 警告。但我们**正要**把这些位置屏蔽掉，警告纯属噪音，所以用 `with np.errstate(divide='ignore', invalid='ignore'):` 压住。
2. **域检查时**（各 `_Domain*.__call__`）：被检查的数据里，屏蔽位上可能躺着 `nan`（因为屏蔽位的 `data` 不保证干净），而 `nan` 参与大小比较（`>`、`<`）也会触发 `invalid` 警告。同样压住。

源码注释把理由写得明明白白：

> `# nans at masked positions cause RuntimeWarnings, even though they are masked. To avoid this we suppress warnings.`

设计要点：**先放任运算产生 `nan`（反正一会儿要屏蔽），同时用 `errstate` 把警告静音**。这是「以 mask 为单一事实来源」哲学的延伸——只要 mask 正确，中间过程的 `nan`/警告都不重要。

#### 4.4.3 源码精读

[core.py:986-996 `__call__` 中的 errstate](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L986-L996)：

```python
if self.domain is not None:
    # nans at masked positions cause RuntimeWarnings, even though
    # they are masked. To avoid this we suppress warnings.
    with np.errstate(divide='ignore', invalid='ignore'):
        result = self.f(d, *args, **kwargs)
    m = ~umath.isfinite(result)
    m |= self.domain(d)
    m |= getmask(a)
```

注意 `errstate` 只包住 `self.f(d, ...)` 这一句（真正可能报警的运算），不影响外面的 mask 合并逻辑。

[core.py:864-870 `_DomainCheckInterval.__call__` 中的 errstate](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L864-L870)：

```python
def __call__(self, x):
    with np.errstate(invalid='ignore'):
        return umath.logical_or(umath.greater(x, self.b),
                                umath.less(x, self.a))
```

这里 `x` 若含 `nan`，`greater`/`less` 会产生 `invalid` 警告，故需 `errstate`。

`__array_wrap__` 路径同样如此——[core.py:3166-3171](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L3166-L3171) 在算域 mask 时也套了 `with np.errstate(divide='ignore', invalid='ignore'):`。

#### 4.4.4 代码实践

**实践目标**：对比「不开 errstate」与 `ma` 内部「开 errstate」的警告差异，直观感受它的作用。

**操作步骤**：

```python
import warnings
import numpy as np
import numpy.ma as ma

a = np.array([-1.0, 0.0, 1.0])

# (1) 原生 numpy：会报警
print("--- 原生 np.sqrt ---")
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    _ = np.sqrt(a)
    print("警告条数:", len(w))
    for warning in w:
        print("  ", warning.category.__name__, warning.message)

# (2) ma.sqrt：静默
print("--- ma.sqrt ---")
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    _ = ma.sqrt(a)
    print("警告条数:", len(w))
```

**需要观察的现象**：

- 原生 `np.sqrt` 会触发 1 条 `RuntimeWarning: invalid value encountered in sqrt`。
- `ma.sqrt` 触发 **0** 条警告，尽管它内部也算了 `sqrt(-1)`。

**预期结果**：

```
--- 原生 np.sqrt ---
警告条数: 1
   RuntimeWarning invalid value encountered in sqrt
--- ma.sqrt ---
警告条数: 0
```

**手动验证 errstate 的效果**（可选）：把 `np.errstate` 去掉对比——

```python
# 模拟「不抑制」的情况：
with np.errstate(invalid='warn'):
    with warnings.catch_warnings(record=True) as w:
        warnings.simplefilter("always")
        _ = np.sqrt(np.array([-1.0]))
        print("不抑制时警告条数:", len(w))   # 应为 1
```

> 如果你在本地运行时警告条数与上述不一致（例如全局已设置 `np.seterr(all='ignore')` 或环境变量 `NPY_NUM_THREADS` 等影响），以「`ma.sqrt` 不产生 `RuntimeWarning`」这一**相对结论**为准，绝对条数取决于你的警告过滤器初始状态。

#### 4.4.5 小练习与答案

**练习 1**：如果把 `__call__` 里的 `with np.errstate(divide='ignore', invalid='ignore'):` 这行删掉，`ma.sqrt(masked_array([-1,0,1]))` 的**结果**会变吗？会有什么副作用？

**参考答案**：**结果不变**（mask、data 都一样），因为 `errstate` 只影响「是否打印警告」，不影响数值计算。副作用是：每次对负数开平方都会打印一条 `RuntimeWarning`，对用户造成噪音干扰，尤其在循环/大数据量下会刷屏。所以 `errstate` 是「用户体验」层面的必要措施，而非正确性所需。

**练习 2**：为什么 `_DomainSafeDivide.__call__` 用的是 `np.errstate(all='ignore')`（全部忽略），而 `_DomainCheckInterval` 只用 `np.errstate(invalid='ignore')`？

**参考答案**：`_DomainSafeDivide` 用于除法的域检查，内部会算 `umath.absolute(a) * tolerance >= umath.absolute(b)`，当 `a`/`b` 含 `nan`/`inf` 时既可能触发 `invalid` 也可能触发 `divide`/`overflow` 等多种浮点异常，所以干脆用 `all='ignore'` 全压。而 `_DomainCheckInterval` 只做大小比较（`greater`/`less`），最坏只会触发 `invalid`（由 `nan` 引起），所以只压 `invalid` 就够了。这是「按需抑制」的精细处理。

---

## 5. 综合实践

把本讲四个模块串起来，做一个「自制带域掩码 ufunc」的小任务。

**任务**：`numpy.ma` 当前没有掩码版的 `cbrt`（立方根）。请你仿照 `core.py` 的注册风格，**手动构造**一个带域的掩码一元运算，并验证它按预期工作。具体地：

1. **阅读** [core.py:1270-1271](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1270-L1271) 的 `sqrt` 注册行，理解三参数含义。
2. **构造**一个「倒数」的掩码版：数学上 \(f(x) = 1/x\)，定义域是 \(x \neq 0\)，即「非法」当 \(x = 0\)。用 `_DomainGreaterEqual` 无法精确表达「等于零」，但你可以用一个简单的自定义域类：

   ```python
   import numpy as np
   from numpy.ma.core import _MaskedUnaryOperation

   class _DomainNonZero:
       """非法当 x == 0。"""
       def __call__(self, x):
           with np.errstate(invalid='ignore'):
               return umath.equal(x, 0)   # 注意需要先拿到 umath

   import numpy.core.umath as umath
   ma_reciprocal = _MaskedUnaryOperation(umath.reciprocal, 0.0,
                                         _DomainNonZero())
   ```

   （上面是**示例代码**，仅为说明如何把包装器+域拼起来；`umath` 的导入路径请以你本地 NumPy 版本为准，也可以直接用 `np.equal` 替换 `umath.equal`。）

3. **测试**：对 `ma.array([-2, 0, 4])` 调用你的 `ma_reciprocal`，预期结果是 `[-0.5, --, 0.25]`，即 `0` 被屏蔽。
4. **验证三来源**：再构造 `ma.array([-2, 0, 4], mask=[0, 0, 1])`（末位预先屏蔽），调用 `ma_reciprocal`，预期屏蔽位为 `[False, True, True]`——位置 1 来自域（\(x=0\)），位置 2 来自输入 mask 传染。
5. **观察 `.data`**：检查屏蔽位置的 `data` 是否是原始输入（走 `__call__` 回填），对照 [core.py:1019](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1019) 解释原因。

**验收标准**：

- 能说清楚「域检查器返回 `True`」与「结果被屏蔽」之间的因果链（经 `m |= self.domain(d)`）。
- 能指出你的 `_DomainNonZero.__call__` 里为什么也要加 `errstate`（防止 `nan` 输入触发比较警告）。
- 能区分 `ma_reciprocal(a)`（走 `__call__`，回填输入）与 `np.reciprocal(a)`（走 `__array_wrap__`，填 `fill=0.0`）在 `.data` 上的差异——注意：你自定义的包装器**没有**注册进 `ufunc_domain`/`ufunc_fills` 之外的 numpy 全局派发，所以 `np.reciprocal(masked_array)` 不一定走你的域；这一对比仅在 `ma` 命名空间内成立，可标注「待本地验证」。

> 这个任务不要求你修改 `core.py`（本讲严禁改源码）；全部在自己的脚本里用 `from numpy.ma.core import _MaskedUnaryOperation` 复用现成包装器即可。

## 6. 本讲小结

- `numpy.ma` 把每个一元 ufunc 包成 `_MaskedUnaryOperation` 实例，赋值给模块级名字（`sqrt`/`log`/`arctan`…），所以 `from numpy.ma import sqrt` 拿到的是包装器而非 `umath.sqrt`。
- `__call__` 的核心是「算结果 + 算 mask + 回填包装」。带域函数的结果 mask 由**三个来源**按位或得到：结果非有限、输入越域、输入已屏蔽。
- **域(domain)** 是一个可调用对象，编码函数的数学定义域，返回「哪些位置非法」的布尔数组。`sqrt` 用 `_DomainGreaterEqual(0.0)`（判 \(x<0\)），`log` 用 `_DomainGreater(0.0)`（判 \(x\leq 0\)）——一字之差决定 `sqrt(0)=0` 合法而 `log(0)` 被屏蔽。
- 注册表 [core.py:1269-1287](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1269-L1287) 一行一个地把 `umath.*` 包成掩码版；`fill` 参数写进全局 `ufunc_fills` 表，供 `__array_wrap__` 路径（`np.sqrt(掩码数组)`）填充越域位置，而 `__call__` 路径（`ma.sqrt`）回填的是原始输入。
- 两条路径（`ma.sqrt` 走 `__call__`、`np.sqrt` 走 `__array_wrap__`）共用同一份 `ufunc_domain`/`ufunc_fills` 注册表，因此 mask 行为一致；差异仅在屏蔽位的 `.data` 取值。
- `errstate` 用来静音「屏蔽位上的垃圾值参与运算/比较」产生的 `RuntimeWarning`，是用户体验层面的必要措施，不影响计算正确性。

## 7. 下一步学习建议

- **下一讲 u2-l5（掩码二元运算与除法域）**：本讲只讲了一元。二元运算（`add`/`subtract`/`divide`）要合并**两侧** mask，机制更复杂；`divide` 还用 `_DomainSafeDivide` 屏蔽除零，是 `_DomainedBinaryOperation` 的典型例子，与本讲的 `_DomainSafeDivide` 直接衔接。
- **回看 u2-l2（`__array_wrap__`）**：本讲多次提到「另一条路径」。如果你对 `np.sqrt(masked_array)` 为何走 `__array_wrap__`、`context` 参数怎么传递还不清楚，建议重读 u2-l2 的 `__array_wrap__` 一节。
- **延伸阅读源码**：
  - [`_DomainedBinaryOperation`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1175-L1242)（core.py:1175-1242）——二元版的域机制，是下一讲的主菜，可提前浏览。
  - [二元 ufunc 注册区](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/core.py#L1289-L1322)（core.py:1289-1322）——对照本讲的一元注册区，体会两类包装器的参数差异。
- **测试参考**：`tests/test_core.py` 里 [`TestUfuncs.test_testUfuncRegression`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/tests/test_core.py#L2643-L2645) 遍历了一长串一元/二元 ufunc 名字做回归测试，是验证你理解的好素材；同一文件中的 [`test_ndarray_mask`](https://github.com/numpy/numpy/blob/b21650c4f6eb53c30eef3508c0c27ce5c51ccd6b/numpy/ma/tests/test_core.py#L2693-L2701) 则直接展示了 `np.sqrt(masked_array)` 的预期 mask。
