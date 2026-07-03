# 向量化与 preserve_shape 模式

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 `derivative` 对 `x`、`args`、`step_direction` 三组输入是**如何**做逐元素广播的，并能预测一次调用最终的输出形状。
- 准确描述 `preserve_shape=False`（默认）模式下 `f` 必须满足的**形状契约**——「输出形状必须等于输入形状」——并理解为何这个契约要求 `f` 能接受**任意**广播形状。
- 准确描述 `preserve_shape=True` 模式下的**另一种**形状契约——`f` 接受 `shape` 或 `shape + (n,)`——并理解它为何能容纳「向量值函数」（输出比输入多出若干维度）。
- 面对一个具体的 `f`，判断该用哪种模式，并能解释 `jacobian` 为什么硬编码 `preserve_shape=True`。

本讲是 **advanced** 层。它假定你已经读完 u2（算法内核与 `eim._loop` 框架）和 u3-l1（`jacobian` 实现）。本讲不再重复差分公式、权重推导或循环钩子的细节，而是聚焦于一个贯穿性的问题：**框架在每一轮究竟以什么形状调用 `f`？这个形状由谁决定？**

## 2. 前置知识

本讲频繁使用以下概念，这里用一句话回顾，避免歧义：

- **逐元素（elementwise）**：`derivative` 把「对一个标量点求导」独立地施加到输入数组的**每一个**元素上。输入数组里有多少个元素，就同时求解多少个独立的标量求导问题。
- **广播（broadcasting）**：NumPy 的规则——若干数组按各自的形状从末尾向前对齐，维度为 1 的轴可以被「复制拉伸」成更大的轴，从而让形状不同的数组一起参与运算。本讲说「`x` 和 `args` 可广播」就是指它们能被 `np.broadcast_arrays` 统一成同一个形状。
- **`shape`（广播形状）**：在本讲的语境里，`shape` 特指 `x` 与所有 `args` 元素一起广播后的形状。它是「问题的个数」与「结果数组的几何」。
- **活跃元素（active）与压缩（compression）**：u2-l6 讲过，`eim._loop` 用 `active` 索引数组跟踪「尚未收敛的元素」，并把已收敛的元素从 `work` 数组里**剔除**以省算力。本讲的关键洞察之一是：`preserve_shape` 正是用来**开关这个压缩机制**的。
- **stencil / 求值点数 `n`**：u2-l2、u2-l3 讲过，首轮新增 `order` 个求值点，其后每轮新增 2 个。本讲里出现的 `(4, 8)`、`(4, 2)` 这些形状的第二轴就是这个 `n`（首轮 `2*terms=8`，其后 `2`）。

一个直觉性的总览：`derivative` 在「上层」把三组输入（`x` / `args` / `step_direction`）广播成一个统一的 `shape`，在「下层」由 `eim._loop` 把这个 `shape` 摊平成一维、逐元素地跑迭代。`preserve_shape` 是「下层」的一个开关——它决定 `loop` 在调用 `f` 之前是否要把摊平的一维数组**重新撑回** `shape` 的形状。这个看似细小的开关，直接决定了 `f` 收到的输入长什么样，进而决定了 `f` 该怎么写。

## 3. 本讲源码地图

本讲涉及的关键文件只有一个主文件加一个共享框架：

| 文件 | 作用 |
| --- | --- |
| [_differentiate.py](_differentiate.py) | `derivative` 的全部实现，含 `preserve_shape` 参数的校验、docstring 中的两个对照示例，以及对 `eim` 框架的接线。 |
| `scipy/_lib/_elementwise_iterative_method.py` | 共享框架 `eim`。本讲重点读其中 `_initialize`（广播与 `preserve_shape` 包装）和 `_loop` / `_check_termination`（按 `preserve_shape` 决定是否压缩）三段。 |

下面所有跨目录的永久链接都以 `scipy/_lib/` 为基准构造；`_differentiate.py` 的链接以本子包目录为基准。

## 4. 核心概念与源码讲解

### 4.1 `x` / `args` / `step_direction` 的逐元素广播

#### 4.1.1 概念说明

`derivative` 的 docstring 开篇就声明它是向量化的：

> This function works elementwise when `x`, `step_direction`, and `args` contain (broadcastable) arrays.

这句话的意思是：你不需要写 `for` 循环。只要把 `x`、`args` 里每一项、以及 `step_direction` 三个都给成「彼此可广播」的数组，`derivative` 会把它们广播成同一个 `shape`，然后在该 `shape` 的每一个元素上独立求一次导，最后返回一个**同形状**的结果数组。

这里有一个容易忽略的细节：**三组输入必须两两可广播**。也就是说，`x` 不仅要能和 `args` 广播，还要能和 `step_direction` 广播；`step_direction` 也要能和 `args` 广播。最终 `shape` 是它们全体广播的结果。

为什么要把这三组放在一起广播？因为它们在语义上「按元素配对」：

- `x[i]` 是第 `i` 个求导点；
- `step_direction[i]` 决定第 `i` 个点用中心差分还是左/右单侧差分；
- `args[k][i]` 是第 `i` 个点上 `f` 的第 `k` 个额外参数。

三者必须在「第 `i` 个问题」上对齐，所以必须共享同一个 `shape`。

#### 4.1.2 核心流程

广播实际上**分两处**完成，理解这个分工很重要：

1. **校验层 `_derivative_iv`**：把 `x`、`step_direction`、`initial_step` 三个**步长相关**的输入广播到同形状（这一组里不含 `args`，因为 `args` 由框架统一处理）。
2. **框架层 `eim._initialize`**：把 `x`（即 `xs`）和**所有** `args` 一起广播，得到最终的 `shape`；接着做一次「试调用」确定函数值的 `dtype`，最后把所有数组摊平成一维，交给 `loop` 逐元素处理。

用伪代码概括：

```
shape = broadcast(x, step_direction, initial_step)        # _derivative_iv
shape = broadcast(x, *args)                               # _initialize（覆盖上面的，最终生效）
dtype = result_type(x, f(x,*args), 整数→float)
把 x、args、f(x) 全部 reshape 成 (prod(shape),)            # 摊平成一维
```

最终 `shape` 由 `_initialize` 决定，它等于「`x` 与所有 `args` 的广播形状」。注意 `step_direction` 和 `initial_step` 虽然 `_derivative_iv` 做了广播，但它们的最终形状也会在主流程里被重新广播到这个 `shape`（见 4.1.3）。

#### 4.1.3 源码精读

**第一处广播——校验层**，把三个步长参数广播到一起：

[_differentiate.py:L44-L47](_differentiate.py#L44-L47)：把 `step_direction`、`initial_step` 转成数组，再与 `x` 一起广播。这里完成了「步长三件套彼此可广播」的检查与统一。

**第二处广播——框架层 `_initialize`**，把 `x` 和所有 `args` 一起广播并确定 `dtype`：

[`scipy/_lib/_elementwise_iterative_method.py:L97-L103`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_elementwise_iterative_method.py#L97-L103)：先算出「至少是浮点」的结果类型 `xat`；用 `xp.broadcast_arrays(*xs, *args)` 把 `x` 与所有 `args` 统一广播并重命名；然后对每个 `x` 做一次试调用 `func(x, *args)` 拿到函数值 `fs`，并记下输入形状 `shape` 与函数值形状 `fshape`。`shape` 就是最终贯穿全程的广播形状。

**摊平成一维**，供逐元素循环使用：

[`scipy/_lib/_elementwise_iterative_method.py:L131-L136`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_elementwise_iterative_method.py#L131-L136)：把 `xs`、`fs`、`args` 全部 `reshape` 成 `(-1,)`（一维），同时记下原始 `shape` 以便最后把结果撑回来。

**主流程把 `step_direction` / `initial_step` 重新广播到最终 `shape`**：

[_differentiate.py:L411-L417](_differentiate.py#L411-L417)：`hdir = broadcast_to(hdir, shape)` 再摊平、取符号（`sign`）；`h0` 同样广播、摊平，并把 `<=0` 的非法初值置为 `NaN`。注意这里依赖了 `_derivative_iv` 已经验证过 `hdir`、`h0` 能广播到 `shape`（注释 L407-L410 也说明了这一点）。

**docstring 中的三向广播示例**，最能说明问题：

[_differentiate.py:L293-L312](_differentiate.py#L293-L312)：`f(x, p) = x**p`，`x` 形状 `(4,)`、`p` 形状 `(5,1)`、`hdir` 形状 `(3,1,1)`，三者广播得到 `shape=(3,5,4)`，于是 `res.df.shape == (3,5,4)`。这个例子同时展示了 `x`、`args=(p,)`、`step_direction=hdir` 三组输入如何叠加成三维输出。

#### 4.1.4 代码实践

**实践目标**：亲手验证三向广播，并预测输出形状。

**操作步骤**（待本地验证）：

```python
import numpy as np
from scipy.differentiate import derivative

def f(x, p):
    return x ** p

def df(x, p):
    return p * x ** (p - 1)

x = np.arange(1, 5)                       # shape (4,)
p = np.arange(1, 6).reshape((-1, 1))      # shape (5, 1)
hdir = np.arange(-1, 2).reshape((-1, 1, 1))  # shape (3, 1, 1)

res = derivative(f, x, args=(p,), step_direction=hdir, maxiter=1)
```

**需要观察的现象**：

1. `res.df.shape` 应当是 `(3, 5, 4)`，即 `step_direction`、`p`、`x` 三者的广播结果。
2. `np.allclose(res.df, df(x, p))` 应为 `True`（注意 `df(x,p)` 也会广播成 `(3,5,4)`，因为它用的是同样的三组数组）。

**预期结果**：与 docstring 一致，`res.df.shape == (3, 5, 4)`。

#### 4.1.5 小练习与答案

**练习 1**：如果上例中 `p` 改成 `np.arange(1,6)`（形状 `(5,)`，不加 `reshape`），还能正常运行吗？为什么？

**答案**：不能。`x` 是 `(4,)`、`p` 是 `(5,)`，末尾维度 `4 ≠ 5` 且都不是 1，无法广播，`_initialize` 里的 `broadcast_arrays` 会抛 `ValueError`。`reshape((-1,1))` 的作用就是把 `p` 的那一维挪到前面、留一个长度为 1 的轴去对齐 `x`。

**练习 2**：为什么 `step_direction` 必须和 `args` 也可广播，而不只是和 `x` 可广播？

**答案**：因为最终 `shape = broadcast(x, *args)`，而 `step_direction` 要被 `broadcast_to(hdir, shape)` 拉伸到这个 `shape`（4.1.3 第四处源码）。若 `step_direction` 只能与 `x` 广播却与 `args` 冲突，这一步就会失败。三组输入两两可广播是必要条件。

---

### 4.2 `preserve_shape=False` 的形状契约

#### 4.2.1 概念说明

默认模式 `preserve_shape=False` 给 `f` 定下的契约是：

> When `preserve_shape=False` (default), `f` must accept arguments of *any* broadcastable shapes.

并且——这是更硬的约束——**每次调用 `f` 的输出形状必须严格等于输入形状**：

> the shape of the output is always the shape of the input `xi`.

为什么强调「any（任意）」？因为在这种模式下，框架会**压缩**已收敛的元素（见 u2-l6）。压缩意味着：每一轮 `f` 被调用时，它收到的数组沿「问题轴」的长度会**随收敛情况而变化**——某些元素收敛后被剔除，下一轮 `f` 就只收到剩下那些元素。`f` 不能假设自己每次都收到固定形状的输入，它必须能正确处理「任意广播形状」。

这就引出一个**强限制**：在默认模式下，`f` 的输出维度不能比输入多。比如一个「向量值函数」`f(x) = [x, sin(3x), ...]`，标量进、向量出，输出形状 `(4,)` ≠ 输入标量形状 `()`，**直接违反契约**，会在 `_initialize` 里被拒绝。这正是 `preserve_shape=True` 要解决的问题（见 4.3）。

#### 4.2.2 核心流程

默认模式下，一次 `derivative` 调用里 `f` 收到的形状序列，可以用 docstring 的例子刻画。设 `shape=(4,)`（4 个频率 `c=[1,5,10,20]`），`order=8`、首轮新增 8 点、其后每轮 2 点，且各频率在不同轮次收敛：

```
调用 0  (校验)   : shape (4,)     ← 单点试调用，确定 dtype
调用 1  (第1轮)  : shape (4, 8)   ← 首轮新增 8 个求值点
调用 2  (第2轮)  : shape (4, 2)   ← 之后每轮新增 2 个点；本轮 c=1 收敛
                                  ← 压缩：剔除 c=1，剩 3 个活跃元素
调用 3  (第3轮)  : shape (3, 2)   ← c=5 收敛；剔除，剩 2 个
调用 4  (第4轮)  : shape (2, 2)   ← c=10 收敛；剔除，剩 1 个
调用 5  (第5轮)  : shape (1, 2)   ← c=20 收敛；全部停止
```

注意**第二轴**（求值点数）从 8 变 2 后稳定不变，而**第一轴**（频率轴 / 问题轴）随收敛从 4 一路缩到 1。这条「问题轴收缩」的轨迹，就是压缩机制的直接体现。对应的函数调用次数 `nfev` 分别为 `[11, 13, 15, 17]`（校验算 1 次，第 1 轮算 8 次，之后每轮算 2 次，累加而得），与每个频率的收敛轮次一一对应。

为什么必须是「任意形状」？因为 `f` 无法预知自己在第 3 轮会收到 `(3,2)` 还是别的——这取决于**哪些元素恰好在这一轮收敛**，是运行期才能确定的信息。

#### 4.2.3 源码精读

**docstring 的默认模式示例与形状解释**：

[_differentiate.py:L318-L350](_differentiate.py#L318-L350)：`f(x, c) = sin(c*x)`，`args=(c,)`，`c=[1,5,10,20]`。运行得到 `shapes = [(4,), (4, 8), (4, 2), (3, 2), (2, 2), (1, 2)]`，并明确解释：后面的调用里「函数在更少的频率上被求值，因为对应导数已经收敛……这能节省函数调用以提升性能，但**要求函数能接受任意形状的参数**」。

**契约的硬性校验——输出形状必须等于 `shape`**：

[`scipy/_lib/_elementwise_iterative_method.py:L114-L120`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_elementwise_iterative_method.py#L114-L120)：比较每个试调用结果 `f.shape` 是否等于 `shape`，不等就抛 `ValueError`。这条检查正是默认模式下「向量值函数」被拒的源头——`f` 返回 `(4,)` 而 `x` 是标量 `shape=()`，`(4,) != ()`。

**压缩的发生地**：

[`scipy/_lib/_elementwise_iterative_method.py:L295-L308`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_elementwise_iterative_method.py#L295-L308)：`_check_termination` 里，`if not preserve_shape:` 分支遍历 `work` 的所有属性，用 `val[proceed]` 把已收敛元素（`proceed = ~stop` 为假的那些）从每个数组里剔除，`work.args` 也一并压缩。这一步就是「问题轴收缩」的来源——下一轮 `pre_func_eval` 看到的 `work.x`、`work.fs` 都已经变短了。

**对应的报错测试**：

见 `test_differentiate.py` 中 `test_special_cases`（约 [_differentiate 的测试文件](../tests/test_differentiate.py)）：`derivative(lambda x: [1, 2, 3], xp.asarray([-2, -3]))` 在默认模式下抛出 `"When preserve_shape=False, the shape of the array returned by..."`。

#### 4.2.4 代码实践

**实践目标**：亲手跑出那条「问题轴收缩」的形状序列，并亲眼看到默认模式拒绝向量值函数。

**操作步骤**（待本地验证）：

```python
import numpy as np
from scipy.differentiate import derivative

# (a) 默认模式的收缩形状序列
shapes = []
def f(x, c):
    shapes.append(np.broadcast_shapes(x.shape, np.shape(c)))
    return np.sin(c * x)

c = [1, 5, 10, 20]
res = derivative(f, 0, args=(c,))
print("shapes:", shapes)        # 期望 [(4,), (4, 8), (4, 2), (3, 2), (2, 2), (1, 2)]
print("nfev:  ", res.nfev)      # 期望 [11, 13, 15, 17]

# (b) 默认模式拒绝向量值函数
def g(x):
    return [x, np.sin(3*x), x + np.sin(10*x), np.sin(20*x)*(x-1)**2]

try:
    derivative(g, 0)            # 标量 x，但 g 返回 (4,)
except ValueError as e:
    print("ValueError:", e)
```

**需要观察的现象**：

1. (a) 的 `shapes` 第二轴先 8 后 2，第一轴从 4 缩到 1。
2. (a) 的 `res.nfev` 与各频率收敛早晚一致。
3. (b) 抛出 `ValueError`，提示信息以 `"When preserve_shape=False, the shape of the array returned by"` 开头。

**预期结果**：与上述一致。这正是 4.3 要用 `preserve_shape=True` 解决的痛点。

#### 4.2.5 小练习与答案

**练习 1**：在 (a) 的例子里，为什么校验调用（第一次）的形状是 `(4,)` 而不是 `(4, 8)`？

**答案**：校验调用发生在 `_initialize` 里（4.1.3 第二处源码），它对**原始** `x`（这里是标量 `0`）做单点试调用，与 `c` 广播后形状为 `(4,)`，不附加任何求值点轴。求值点轴 `(8,)`、`(2,)` 是后续 `loop` 里 `pre_func_eval` 加上去的。

**练习 2**：如果把 (a) 里所有 `c` 都设成同一个值 `c=[2,2,2,2]`，`shapes` 序列还会出现 `(3,2)`、`(2,2)` 吗？

**答案**：不会。四个频率相同意味着四个元素行为几乎一致，会在同一轮一起收敛，于是第一轴会从 4 直接跳到 0（或停留在 4 直到同轮全部收敛），不会出现中间的 `(3,2)`、`(2,2)`、`(1,2)`。形状序列的具体形态**取决于哪些元素在哪一轮收敛**。

---

### 4.3 `preserve_shape=True` 与向量值函数

#### 4.3.1 概念说明

`preserve_shape=True` 模式给 `f` 定下的契约是另一种：

> When `preserve_shape=True`, `f` must accept arguments of shape `shape` *or* `shape + (n,)`, where `(n,)` is the number of abscissae at which the function is being evaluated.

两个关键变化：

1. **不再压缩**。`shape`（问题轴）在全程保持不变，`f` 每一轮收到的输入都是「`shape` 或 `shape+(n,)`」这种**可预测**的形状。`f` 因此可以放心地按位置索引自己的输入（例如 `x[0]`、`x[1]`）。
2. **`shape` 被重新定义为「函数值形状与输入形状的广播」**。这是最微妙也最关键的一点：在 `preserve_shape=True` 下，`shape = broadcast(fshape, 原始 shape)`，其中 `fshape` 是试调用时 `f` 返回的形状。这意味着**输出可以比原始输入多出若干前导维度**——正是「向量值函数」所需要的。

回到那个被默认模式拒绝的向量值函数 `f(x) = [x, sin(3x), x+sin(10x), sin(20x)(x-1)^2]`。在 `preserve_shape=True` 下，我们传入 `x = np.zeros(4)`：

- 试调用 `f(np.zeros(4))` 返回 `(4,)`，`fshape=(4,)`；
- `shape = broadcast((4,), (4,)) = (4,)`；
- 之后每一轮，`f` 收到的形状都是 `(4,)` 或 `(4, n)`——`(4,)`、`(4,8)`、`(4,2)`、`(4,2)`……**第一轴恒为 4**。

这里的巧妙之处：我们把 4 个独立的标量函数「塞进」`x` 的 4 个分量里，让 `shape=(4,)` 同时充当「问题个数（4）」和「输入分量数（4）」。`f` 通过 `x0, x1, x2, x3 = x` 把这 4 个分量拆开，分别施加不同的函数，再把结果沿新的第 0 轴 `stack` 回 `(4, ...)`。因为第 0 轴恒为 4，`x[0]` 永远对应第 0 个分量，`f` 的索引逻辑才是合法的。

这正是 `jacobian` 内部使用的机制（见 u3-l1）：它构造 `(m,m,...)` 的对角扰动矩阵并硬编码 `preserve_shape=True`，让 `shape` 把「输出维度 `n`」也吸收进来。

#### 4.3.2 核心流程

`preserve_shape=True` 在三个地方改写了默认行为，串联起来才是完整图景：

```
_initialize:
  shape = broadcast(fshape, 原始 shape)        # 吸收 f 的前导维度
  把每个 x / arg broadcast_to(shape)            # 撑到新 shape
  包装 func：收到 (shape+(n,)) 时切掉多余前导维，再调用原始 f

_loop（每轮）:
  pre_func_eval 产出 x_eval (active, n)
  x_eval reshape 成 (shape + (n,))              # 撑回 shape，而非保持一维
  调用 func(x) → f 收到 (shape+(n,))

_check_termination:
  if preserve_shape: 不压缩 work 数组           # shape 全程不变
```

对应到那个 `(4,)` 的向量值例子，`f` 收到的形状序列就是 `[(4,), (4, 8), (4, 2), (4, 2), (4, 2), (4, 2)]`——注意与 4.2 的 `[...,(3,2),(2,2),(1,2)]` 相比，**第一轴始终是 4**，从不收缩。

用公式概括两种契约下 `shape` 的差别：

- `preserve_shape=False`：\(\; \text{shape} = \mathrm{broadcast}(\text{x}, *\text{args}) \;\)，且强制 \(f(\text{xi}).\text{shape} = \text{shape}\)。
- `preserve_shape=True`：\(\; \text{shape} = \mathrm{broadcast}(\underbrace{f(\text{x}).\text{shape}}_{\text{fshape}},\;\mathrm{broadcast}(\text{x}, *\text{args})) \;\)，\(f\) 接受 \(\text{shape}\) 或 \(\text{shape}+(n,)\)。

后者多出来的 \( \text{fshape} \) 正是「输出可以有额外前导维度」的数学根源。

#### 4.3.3 源码精读

**docstring 的向量值函数示例与形状解释**：

[_differentiate.py:L352-L377](_differentiate.py#L352-L377)：`f` 用 `x0, x1, x2, x3 = x` 拆分输入、返回长度 4 的列表，`x = np.zeros(4)`，`preserve_shape=True`。运行得到 `shapes = [(4,), (4, 8), (4, 2), (4, 2), (4, 2), (4, 2)]`，并解释：「`x` 的形状是 `(4,)`；在 `preserve_shape=True` 下，函数可能收到形状 `(4,)` 或 `(4, n)` 的参数 `x`，这正是我们观察到的」。

**`_initialize` 重新定义 `shape` 并包装 `func`**：

[`scipy/_lib/_elementwise_iterative_method.py:L105-L112`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_elementwise_iterative_method.py#L105-L112)：进入 `if preserve_shape:` 分支后，用一个闭包**包装** `func`——当 `f` 被传入 `shape+(n,)` 这种带额外尾随轴的数组时，先用 `x[i]`（`i` 是长度为 `len(fshape)-len(shape)` 的全零元组）切掉那部分「超出 `shape` 的前导轴」，再调用原始 `f`；同时 `shape = np.broadcast_shapes(fshape, shape)` 把 `fshape` 吸收进来，并把 `xs`、`args` 都 `broadcast_to` 到新 `shape`。这一段是「输出可多于输入」的全部秘密。

**`_loop` 在调用 `f` 前把 `x` 撑回 `shape`**：

[`scipy/_lib/_elementwise_iterative_method.py:L251-L259`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_elementwise_iterative_method.py#L251-L259)：`if preserve_shape: x = xp.reshape(x, shape + (-1,))`——把一维的 `x_eval (active, n)` 重排成 `(shape, n)` 再喂给 `func`，调用完再 reshape 回去记账。紧接着 `work.nfev += 1 if x.ndim == 1 else x.shape[-1]` 用最后一轴的长度（即本轮求值点数 `n`）累加 `nfev`。

**`_check_termination` 关闭压缩**：

[`scipy/_lib/_elementwise_iterative_method.py:L295-L308`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_elementwise_iterative_method.py#L295-L308)：`if preserve_shape:` 分支只做 `stop = stop[active]` 和 `active = active[proceed]` 的索引更新，**跳过** `for key, val in work.items(): work[key] = val[proceed]` 这段压缩。因此 `work` 数组全程保持原长，`shape` 不会收缩——这正是「第一轴恒为 4」的实现原因。

代价：已收敛的元素不会被剔除，每一轮仍会被重新求值（只是结果不再写回 `res`），所以 `preserve_shape=True` 在「元素收敛步调差异大」时会比默认模式多花算力。这是换取「`f` 可按固定形状索引」的必要代价。

**`jacobian` 硬编码 `preserve_shape=True`**：

[_differentiate.py:L942-L945](_differentiate.py#L942-L945)：`jacobian` 内部构造对角扰动矩阵 `wrapped` 后，委托 `derivative` 时**强制** `preserve_shape=True`。因为 `wrapped` 的输出形状（含 `n` 个输出维度）大于 `x` 的形状（`m` 个输入维度），只有 `preserve_shape=True` 才能让 `_initialize` 把这个多出来的前导维度合法地吸收进 `shape`（详见 u3-l1）。

#### 4.3.4 代码实践

**实践目标**：亲手跑出「第一轴恒定」的形状序列，并理解 `f` 为何能安全地按位置索引。

**操作步骤**（待本地验证）：

```python
import numpy as np
from scipy.differentiate import derivative

shapes = []
def f(x):
    shapes.append(x.shape)
    x0, x1, x2, x3 = x          # x 的第 0 轴恒为 4，所以这里总是能拆成 4 份
    return [x0, np.sin(3*x1), x2 + np.sin(10*x2), np.sin(20*x3)*(x3-1)**2]

x = np.zeros(4)
res = derivative(f, x, preserve_shape=True)
print("shapes:", shapes)        # 期望 [(4,), (4, 8), (4, 2), (4, 2), (4, 2), (4, 2)]
print("df:    ", res.df)        # 期望 [1, 3, 1, 0]（x=0 处的真导数）
```

**需要观察的现象**：

1. `shapes` 里**每一个**元组的第一轴都是 `4`，从不出现 `(3,2)`、`(2,2)`。
2. 正因为第一轴恒为 4，`x0, x1, x2, x3 = x` 在每次调用里都合法——`x` 沿第 0 轴恰好有 4 份。
3. `res.df` 是这 4 个分量函数在 `x=0` 处各自导数的列表：`[1, 3, 1, 0]`。

**预期结果**：`shapes = [(4,), (4, 8), (4, 2), (4, 2), (4, 2), (4, 2)]`，`res.df ≈ [1, 3, 1, 0]`。

> 说明：把 4.2 的 `(4,),(4,8),(4,2),(3,2),(2,2),(1,2)` 与本节的 `(4,),(4,8),(4,2),(4,2),(4,2),(4,2)` 并排对照——前者第一轴会收缩（默认模式、压缩开启），后者第一轴恒定（`preserve_shape=True`、压缩关闭）。这是本讲最核心的一组对照。

#### 4.3.5 小练习与答案

**练习 1**：在上例里，如果把 `x = np.zeros(4)` 改成 `x = np.zeros((4, 2))`，`f` 里 `x0, x1, x2, x3 = x` 还成立吗？`shape` 会变成什么？

**答案**：仍然成立——`x` 形状 `(4, 2)`，沿第 0 轴拆成 4 份，每份 `(2,)`。试调用 `f(x)` 返回 4 个 `(2,)` 组成的列表，`stack` 后 `fshape=(4,2)`；`shape = broadcast((4,2),(4,2)) = (4,2)`。结果 `res.df` 形状也是 `(4,2)`，即对 4 个分量函数各在 2 个点上求导。这展示了 `preserve_shape=True` 同样支持「在多个点同时求导」的向量化。

**练习 2**：为什么 `preserve_shape=True` 不能简单地用「关闭压缩」一句话概括？它还多做了哪件默认模式没有的事？

**答案**：关闭压缩只是「不让 `shape` 收缩」。`preserve_shape=True` 还多做了两件：(1) 在 `_initialize` 里把 `shape` 重定义为 `broadcast(fshape, 原 shape)`，从而允许输出有额外前导维度；(2) 包装 `func`，在调用前切掉尾随求值点轴、在 `_loop` 里把 `x` 撑回 `(shape, n)`。这三件事合起来才让「向量值函数」变得可用——光关闭压缩，`shape=()` 的标量 `x` 仍然喂不进返回 `(4,)` 的 `f`。

---

## 5. 综合实践

**任务**：对同一个数学对象——「向量值函数」\(F(x) = [\,x,\;\sin(3x),\;x+\sin(10x),\;\sin(20x)(x-1)^2\,]\)——用**两种**方式实现并求它在 \(x=0\) 处的导数向量，对照两种模式的形状契约。

**方式 A：`preserve_shape=True`（按位置索引）**

直接照搬 4.3.4 的实现，`x = np.zeros(4)`，`f` 用 `x0,x1,x2,x3 = x` 拆分。验证 `res.df ≈ [1, 3, 1, 0]`，并记录 `shapes` 序列第一轴恒为 4。

**方式 B：默认模式 `preserve_shape=False`（用 `args` 重参数化）**

把「第几个分量」做成一个广播参数 `idx`，让 `f` 接收 `(x, idx)` 并按 `idx` 选择不同的标量函数：

```python
import numpy as np
from scipy.differentiate import derivative

def f(x, idx):
    # idx 是与 x 同形状的整数数组，取值 0..3，决定该元素用哪个分量函数
    return (idx == 0) * x \
         + (idx == 1) * np.sin(3*x) \
         + (idx == 2) * (x + np.sin(10*x)) \
         + (idx == 3) * (np.sin(20*x)*(x-1)**2)

idx = np.arange(4)                         # shape (4,)，4 个「问题」对应 4 个分量
res = derivative(f, 0, args=(idx,))        # 标量 x，与 idx 广播成 (4,)
print("df:   ", res.df)                    # 期望 [1, 3, 1, 0]
```

**需要观察与解释的现象**：

1. 两种方式得到的 `res.df` 应当**完全一致**（都接近 `[1, 3, 1, 0]`），因为它们求的是同一个导数向量。
2. 方式 B 的 `f` 输出形状 `(4,)` **等于** `shape=(4,)`（标量 `x` 与 `idx` 广播），满足默认模式的等式契约，所以能跑通；方式 A 的 `f` 输出 `(4,)` 而 `x` 也是 `(4,)`（`preserve_shape` 把 `shape` 重定义为 `(4,)`），同样满足各自契约。
3. 方式 B 是「把向量值函数展平成 4 个标量问题、用 `args` 区分」，`f` 对每个元素做相同的选择逻辑；方式 A 是「把 4 个分量塞进 `x` 的 4 个槽位、`f` 按位置区分」。前者要求 `f` 能广播、后者要求 `f` 能索引——这正是两种模式各自适合的函数风格。

**预期结果**：两种方式 `res.df` 一致；方式 A 形状序列第一轴恒为 4，方式 B 第一轴会收缩（可自行 `append` 形状验证）。

> 这个综合实践把本讲三个最小模块串起来：方式 B 用到了 4.1 的 `args` 广播与 4.2 的等式契约，方式 A 用到了 4.3 的 `preserve_shape` 契约。能解释「为何同一个导数有两种合法写法、各自要求 `f` 具备什么能力」，就说明你真正掌握了本讲。

## 6. 本讲小结

- `derivative` 对 `x`、`args`、`step_direction` 三组输入做逐元素广播：校验层先统一步长三件套，框架层 `_initialize` 再把 `x` 与所有 `args` 广播成最终 `shape`，最后摊平成一维逐元素迭代。
- 默认模式 `preserve_shape=False` 的契约是「`f` 接受任意广播形状、且输出形状必须严格等于输入 `shape`」；因为框架会压缩已收敛元素，`f` 收到的「问题轴」长度每一轮都会变。
- `preserve_shape=True` 的契约是「`f` 接受 `shape` 或 `shape+(n,)`」；它**关闭压缩**使 `shape` 全程不变，`f` 因此能按位置安全索引输入。
- 两种模式的根本差别在 `shape` 的定义：默认模式 \( \text{shape}=\mathrm{broadcast}(x,*\text{args}) \)；`preserve_shape=True` 多吸收了函数值形状 \( \text{shape}=\mathrm{broadcast}(\text{fshape},\,\ldots) \)，从而允许「输出比输入多出前导维度」——这正是向量值函数与 `jacobian` 所需。
- 形状序列是最好的判据：`(4,),(4,8),(4,2),(3,2),(2,2),(1,2)`（第一轴收缩=默认模式）对照 `(4,),(4,8),(4,2),(4,2),(4,2),(4,2)`（第一轴恒定=`preserve_shape=True`）。
- 选型建议：`f` 是「标量进标量出、可广播」用默认模式（还能享受压缩带来的性能）；`f` 是「向量值、需按位置索引、或输出维度多于输入」用 `preserve_shape=True`。

## 7. 下一步学习建议

- **u4-l2 步长方向与边界处理**：本讲多次提到 `step_direction` 的广播，下一讲将聚焦它的语义（0 中心、负取非正步、正取非负步）与在受限定义域上的单侧差分用法。
- **u4-l3 数值精度与调参**：本讲的形状序列里 `nfev=[11,13,15,17]` 反映了收敛早晚；下一讲从「精度」角度解释为何有些元素收敛早、有些晚，以及 `atol`/`rtol` 如何影响这条轨迹。
- **重读 u3-l1 `jacobian`**：带着本讲对 `preserve_shape=True` 的理解重读 `jacobian` 的 `wrapped`（[_differentiate.py:L931-L945](_differentiate.py#L931-L945)），你会更清楚地看到 `(m,m,...)` 对角扰动矩阵为何**必须**配合 `preserve_shape=True`——它的输出形状 `(n,m,...)` 比 `x` 的 `(m,)` 多出了 `n` 这一前导维度。
- **横向扩展**：`eim._loop` 被多个子包复用（`tanhsinh`、`chandrupatla` 等）。本讲讲的 `preserve_shape` 机制是这套框架的通用能力，阅读 `scipy/integrate/_tanhsinh.py` 中对 `preserve_shape` 的使用可以巩固理解。
