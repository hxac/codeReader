# 上手运行：导入、调用与第一个示例

## 1. 本讲目标

本讲是动手环节。读完前两讲你已经知道 `scipy.integrate`「是什么」「目录怎么摆」「怎么编译」，但还没有真正运行过它。本讲的目标是让你从**用户视角**跑通三个最具代表性的函数，建立起对返回结果与工作流的直观认识：

- 学会 `from scipy import integrate` 并调用典型函数；
- 理解 `quad`、`solve_ivp`、`trapezoid` 三个函数的基本调用签名与返回值结构；
- 能够独立运行一段「函数积分 + ODE 求解 + 样本积分」的完整脚本，并把结果与解析解对比。

这三个函数分别代表了 `integrate` 的三大主线任务（函数积分、ODE 初值问题、固定样本积分），是后续每一讲的「锚点」。本讲**只讲怎么用**，不深入算法内部——那是后面单元的事。

## 2. 前置知识

在动手之前，先用最朴素的语言统一几个概念。这些词在前两讲出现过，这里再确认一次：

- **积分（integration）**：在数学上就是求「曲线下的面积」。数值积分是用计算机把这块面积算出来。`integrate` 子包的核心使命就是这件事。
- **函数积分 vs 固定样本积分**：
  - 如果你手里有一个**可计算的函数** `f(x)`（例如 `np.sin`），可以「想在哪取点就在哪取点」，那么用 **`quad`** 这一类「函数积分」函数。它会自适应地选择采样位置。
  - 如果你手里只有**一堆已经采好的离散数据点** `(x_i, y_i)`（例如实验测量值），无法再回到函数去取点，那么用 **`trapezoid`** 这一类「固定样本积分」函数。它只能基于现有点用几何公式估算面积。
- **常微分方程（ODE）与初值问题（IVP）**：ODE 描述「某个量随时间怎么变化」，形如 \( \mathrm{d}y/\mathrm{d}t = f(t, y) \)。「初值问题」是说：已知起点 \( y(t_0) = y_0 \)，求未来的 \( y(t) \)。这就是 **`solve_ivp`** 要解决的问题。
- **Python 包的 `__init__.py`**：一个目录要被当成「包」导入，靠的就是这个文件。它负责把内部模块里的名字「搬到」包的顶层，让你能直接写 `integrate.quad` 而不是 `integrate._quadpack_py.quad`。前两讲已详细讲过，本讲会再次用到它的导出结果。

> 小贴士：本讲所有示例假设你已安装好 SciPy。`from scipy import integrate` 是最常见的写法。

## 3. 本讲源码地图

本讲涉及的关键文件及其作用：

| 文件 | 作用 | 本讲用它做什么 |
| --- | --- | --- |
| `scipy/integrate/__init__.py` | 子包入口，负责把内部模块的名字搬到顶层命名空间 | 确认 `quad`/`solve_ivp`/`trapezoid` 这三个名字分别从哪个模块导出 |
| `scipy/integrate/_quadpack_py.py` | 函数积分的 Python 包装层，定义 `quad`/`dblquad`/`tplquad`/`nquad` | 看 `quad` 的签名、参数校验，以及它如何把活儿交给底层 QUADPACK |
| `scipy/integrate/_quadrature.py` | 固定样本积分的纯 Python 实现，定义 `trapezoid`/`simpson`/`romb` 等 | 看 `trapezoid` 的复合梯形公式实现 |
| `scipy/integrate/_ivp/ivp.py` | ODE 初值问题的统一入口，定义 `solve_ivp` 及方法字典 | 看 `solve_ivp` 的签名、方法解析与主推进循环 |

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：`quad`（函数积分）、`solve_ivp`（ODE 初值问题）、`trapezoid`（固定样本积分）。每个模块都按「先讲直觉，再讲源码，最后动手」的顺序展开。

### 4.1 quad：给一个函数，自适应地算积分

#### 4.1.1 概念说明

`quad` 解决的问题是：给定一个 Python 可调用对象 `func` 和积分区间 `[a, b]`，计算定积分

\[
\int_a^b \mathrm{func}(x)\,\mathrm{d}x
\]

它属于「函数积分」家族——你提供的是**函数本身**，而不是样本点。`quad` 内部会把 `func` 交给 Fortran 数值库 **QUADPACK**，由 QUADPACK 自适应地决定在哪里取点、取多少点：在被积函数「变化剧烈」或「有奇异」的地方多取点，在平缓的地方少取点，最终在满足误差容差的前提下尽量少地调用 `func`。

它的返回值至少是两元组 `(y, abserr)`：`y` 是积分近似值，`abserr` 是 QUADPACK 给出的**绝对误差估计**。这一点和后面的 `trapezoid` 很不一样——`trapezoid` 只返回一个数，不给出误差估计。

#### 4.1.2 核心流程

从用户视角，一次 `quad` 调用的流程是：

1. 用户调用 `quad(func, a, b, ...)`。
2. Python 包装层做参数整理：把 `args` 规整成元组；处理空区间（`a == b` 直接返回 0）；保证 `a < b`（否则记一个 `flip` 标记，最后给结果取负号）。
3. 根据是否带权重函数 `weight`，分发到 `_quad`（普通积分）或 `_quad_weight`（带权积分）。
4. `_quad` 再根据区间是否含无穷端点、是否提供奇异点 `points`，调用 QUADPACK 里不同的 Fortran 例程：
   - `_qagse`：有限区间自适应 Gauss-Kronrod（QAGS），是最常用的路径；
   - `_qagie`：含无穷端点的积分（QAGI）；
   - `_qagpe`：用户提供内部奇异点的积分（QAGP）。
5. QUADPACK 返回结果与状态码 `ier`。`ier == 0` 表示成功，包装层据此返回 `(y, abserr)`；非 0 则给出对应的警告信息。

伪代码（省略细节）：

```
def quad(func, a, b, ...):
    if a == b: return (0., 0.)
    flip, a, b = b < a, min(a,b), max(a,b)
    if weight is None:
        retval = _quad(...)        # 内部选 _qagse/_qagie/_qagpe
    else:
        retval = _quad_weight(...)
    if flip: retval = (-retval[0],) + retval[1:]
    ier = retval[-1]
    if ier == 0: return retval[:-1]   # 成功 → (y, abserr)
    # 否则按 ier 给出警告
```

#### 4.1.3 源码精读

`quad` 定义在 `_quadpack_py.py`，签名与默认容差如下。注意默认绝对/相对容差都是 `1.49e-8`，子区间上限 `limit=50`：

[`_quadpack_py.py:23-25`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadpack_py.py#L23-L25) —— `quad` 的函数签名，定义了 `func, a, b, args, full_output, epsabs, epsrel, limit, ...` 等参数。

它的「空区间快捷返回」是第一个值得注意的分支——当 `a == b` 时直接返回 0，连 Fortran 都不调用：

[`_quadpack_py.py:436-449`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadpack_py.py#L436-L449) —— `a == b` 时直接返回 `(0., 0.)`（或带 `infodict` 的形式）。

接着是分发逻辑：没有权重时走 `_quad`，有权重时走 `_quad_weight`：

[`_quadpack_py.py:478-487`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadpack_py.py#L478-L487) —— 根据 `weight` 是否为 `None` 分发到 `_quad` 或 `_quad_weight`。

`_quad` 是真正选择 Fortran 例程的地方。最常用的有限区间、无奇异点情形会调用 `_quadpack._qagse`（即 QUADPACK 的 QAGS 例程）：

[`_quadpack_py.py:624-626`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadpack_py.py#L624-L626) —— 有限区间且无奇异点时，调用底层 `_quadpack._qagse(func, a, b, args, full_output, epsabs, epsrel, limit)`，这就是 QUADPACK 的 QAGS 自适应积分。

最后，包装层根据返回的状态码 `ier` 决定是否成功返回。`ier == 0` 表示收敛，剥掉状态码后返回 `(y, abserr)`：

[`_quadpack_py.py:492-494`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadpack_py.py#L492-L494) —— `ier == 0` 时返回 `retval[:-1]`，即把状态码去掉，把 `(y, abserr)` 交给用户。

> 关于 `from scipy import integrate` 之后为什么能直接写 `integrate.quad`：这是 `__init__.py` 的功劳。它在 [`__init__.py:105`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/__init__.py#L105) 用 `from ._quadpack_py import *` 把 `quad` 搬到了顶层命名空间（`_quadpack_py.py` 的 `__all__` 里就声明了 `quad`，见 [`_quadpack_py.py:12`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadpack_py.py#L12)）。

#### 4.1.4 代码实践

**实践目标**：用 `quad` 计算 \( \int_0^{\pi} \sin(x)\,\mathrm{d}x \)，并与解析解对比。

解析解为

\[
\int_0^{\pi} \sin(x)\,\mathrm{d}x = \bigl[-\cos(x)\bigr]_0^{\pi} = -\cos(\pi) + \cos(0) = 1 + 1 = 2.
\]

**操作步骤**：

1. 新建一个 `demo_quad.py`，写入下面的示例代码（示例代码，非项目原有）：

   ```python
   # 示例代码
   import numpy as np
   from scipy import integrate

   # 1) 基本用法：返回 (y, abserr)
   result, err = integrate.quad(np.sin, 0, np.pi)
   print(f"quad 结果 = {result:.10f}, 误差估计 = {err:.2e}")
   print(f"解析解   = 2.0")
   print(f"真实偏差 = {abs(result - 2.0):.2e}")

   # 2) 打开 full_output，查看子区间等内部信息
   result2, err2, info = integrate.quad(np.sin, 0, np.pi, full_output=1)
   print(f"子区间数 (info['last']) = {info['last']}")
   ```

2. 运行 `python demo_quad.py`。

**需要观察的现象**：
- `result` 应该非常接近 `2.0`；
- `err` 是 QUADPACK 自报的误差估计，量级通常远小于 `1e-8`；
- `info['last']` 是 QUADPACK 实际使用的子区间数，对于 `sin` 这种平缓函数，这个数应该很小（可能只有 1）。

**预期结果**：`result ≈ 2.0`，`err` 在 `1e-14` 量级左右。具体数值待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：把积分区间改成含奇异的 \( \int_0^1 1/\sqrt{x}\,\mathrm{d}x \)（解析解为 2），用 `quad` 计算，观察 `abserr` 和 `full_output=1` 时的 `info['last']` 与 `sin` 例子的差别。

参考答案：调用 `integrate.quad(lambda x: 1/np.sqrt(x), 0, 1)`。由于 `x=0` 处有积分奇异，QUADPACK 会用更多子区间，`info['last']` 会明显大于 `sin` 的例子，但结果仍应接近 2。

**练习 2**：把上下限反过来写 `integrate.quad(np.sin, np.pi, 0)`，结果会是什么？为什么？

参考答案：结果约为 `-2.0`。因为 [`_quadpack_py.py:452`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadpack_py.py#L452) 记下了 `flip` 标志，内部统一按 `a < b` 计算，最后在 [`_quadpack_py.py:489-490`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadpack_py.py#L489-L490) 对结果取负号，体现了定积分「换限变号」的性质。

---

### 4.2 solve_ivp：给一个微分方程和初值，求未来的解

#### 4.2.1 概念说明

`solve_ivp` 解决的是**常微分方程初值问题（IVP）**：

\[
\mathrm{d}y/\mathrm{d}t = f(t, y), \qquad y(t_0) = y_0.
\]

这里的「时间」`t` 是自变量，`y` 是状态（可以是向量），`f` 是右端函数，描述状态如何随时间变化。「初值」就是已知起点 `(t0, y0)`，目标是求出到达 `tf` 时的状态轨迹。

`solve_ivp` 是新 API（相对于旧的 `ode`/`odeint`）。它把所有具体的求解算法统一封装成「求解器类」，用一个字符串 `method` 来选择，例如 `'RK45'`（默认，显式 5(4) 阶 Runge-Kutta）、`'Radau'`/`'BDF'`（隐式，适合刚性方程）、`'LSODA'`（自动刚度切换）。本讲只关心「怎么调用」，方法内部原理留到第 7–9 单元。

它返回一个 `OdeResult` 对象（继承自 `OptimizeResult`），常用字段有：`t`（时间点数组）、`y`（对应状态数组）、`sol`（当 `dense_output=True` 时是连续解）、`nfev`（右端函数被调用的次数，衡量开销）、`status`/`message`（终止状态）。

#### 4.2.2 核心流程

`solve_ivp` 的工作流可以概括为「解析方法 → 建求解器 → 循环推进 → 打包结果」：

1. **方法解析**：用户传入 `method`（字符串如 `'RK45'`，或直接是 `OdeSolver` 子类）。函数校验合法性后，把字符串查表换成具体的求解器类。
2. **建立求解器**：用 `solver = method(fun, t0, y0, tf, vectorized=..., **options)` 构造一个求解器实例。`options` 里可以放 `rtol`/`atol`/`max_step` 等。
3. **主推进循环**：反复调用 `solver.step()`，每一步把求解器从当前时间推进到下一个时间，直到到达 `tf`（`status == 'finished'`）或失败（`status == 'failed'`）。循环中按需把每步结果、`t_eval` 时刻、事件检测结果收集起来。
4. **打包结果**：把时间序列、状态序列、（可能的）连续解、统计量 `nfev`/`njev`/`nlu` 等组装成 `OdeResult` 返回。

伪代码（省略 `t_eval`/事件细节）：

```
def solve_ivp(fun, t_span, y0, method='RK45', ...):
    # 1. 校验并把字符串 method 换成类
    if method in METHODS:
        method = METHODS[method]
    # 2. 建立求解器
    solver = method(fun, t0, y0, tf, vectorized=vectorized, **options)
    # 3. 主循环
    while status is None:
        message = solver.step()
        if solver.status == 'finished': status = 0
        elif solver.status == 'failed':  status = -1; break
        # 收集 t, y, dense_output, events ...
    # 4. 打包
    return OdeResult(t=..., y=..., sol=..., nfev=solver.nfev, ...)
```

#### 4.2.3 源码精读

`solve_ivp` 定义在 `_ivp/ivp.py`，签名如下（默认方法 `RK45`）：

[`_ivp/ivp.py:161-162`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_ivp/ivp.py#L161-L162) —— `solve_ivp` 的签名：`fun, t_span, y0, method='RK45', t_eval=None, dense_output=False, events=None, vectorized=False, args=None, **options`。

合法的 `method` 字符串及其对应求解器类记录在 `METHODS` 字典里：

[`_ivp/ivp.py:13-18`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_ivp/ivp.py#L13-L18) —— `METHODS` 把字符串 `'RK23'/'RK45'/'DOP853'/'Radau'/'BDF'/'LSODA'` 映射到对应的求解器类。

函数开头先做合法性校验（`method` 必须是字典里的字符串，或是 `OdeSolver` 子类），然后把字符串换成类并建立求解器：

[`_ivp/ivp.py:578-580`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_ivp/ivp.py#L578-L580) —— 校验 `method` 合法性。

[`_ivp/ivp.py:623-626`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_ivp/ivp.py#L623-L626) —— 把字符串换成类后，用 `solver = method(fun, t0, y0, tf, vectorized=vectorized, **options)` 建立求解器实例。

核心是主推进循环，它反复 `solver.step()`，并根据 `solver.status` 判断是否到达终点或失败：

[`_ivp/ivp.py:658-666`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_ivp/ivp.py#L658-L666) —— 主推进循环：`while status is None: message = solver.step()`；`finished` 时置 `status=0`，`failed` 时置 `status=-1` 并 `break`。

最后把所有结果组装成 `OdeResult` 返回，注意它把求解器的统计量 `nfev`（函数调用次数）等也一并带上：

[`_ivp/ivp.py:758-759`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_ivp/ivp.py#L758-L759) —— 返回 `OdeResult(t=ts, y=ys, sol=sol, t_events=..., y_events=..., nfev=solver.nfev, njev=..., nlu=..., ...)`。

> 同样地，`integrate.solve_ivp` 这个名字能被直接访问，是因为 `__init__.py` 在 [`__init__.py:108-109`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/__init__.py#L108-L109) 用 `from ._ivp import (solve_ivp, ...)` 显式导入了它。

#### 4.2.4 代码实践

**实践目标**：求解指数衰减方程 \( \mathrm{d}y/\mathrm{d}t = -y,\ y(0) = 1 \) 到 \( t = 5 \)，并与解析解 \( y(t) = \mathrm{e}^{-t} \) 对比。

解析解为 \( y(5) = \mathrm{e}^{-5} \approx 0.006737947 \)。

**操作步骤**：

1. 新建 `demo_ivp.py`（示例代码）：

   ```python
   # 示例代码
   import numpy as np
   from scipy import integrate

   def decay(t, y):
       return -y

   sol = integrate.solve_ivp(decay, [0, 5], [1.0],
                             t_eval=np.linspace(0, 5, 6))
   print("时间点 t   =", sol.t)
   print("状态   y   =", sol.y[0])
   print("解析解     =", np.exp(-sol.t))
   print("nfev       =", sol.nfev)
   print("status/msg =", sol.status, sol.message)
   ```

2. 运行 `python demo_ivp.py`。

**需要观察的现象**：
- `sol.t` 应该正好是你给的 `t_eval`（6 个点：0,1,2,3,4,5）；
- `sol.y[0]` 与 `np.exp(-sol.t)` 在每个时刻都非常接近；
- `sol.nfev` 是右端函数被调用的总次数（注意 `solve_ivp` 内部步进的点数通常比你给的 `t_eval` 多，所以 `nfev` 一般大于 6）；
- `sol.status` 为 `0`，`sol.message` 为「The solver successfully reached the end of the integration interval.」（对应 [`_ivp/ivp.py:21`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_ivp/ivp.py#L21) 的 `MESSAGES[0]`）。

**预期结果**：`sol.y[0]` 末值约 `0.006738`，与 `np.exp(-5)` 吻合。具体数值待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：把 `method` 改成 `'Radau'` 再解一次同一个衰减方程，对比两者的 `nfev`。哪个更小？

参考答案：对于这种简单的非刚性衰减方程，显式 `RK45` 通常更高效（`nfev` 更小）；隐式 `Radau` 每步要解线性/非线性方程，开销更大，它的优势体现在**刚性**方程上。所以方法选择要看问题特性。

**练习 2**：不加 `t_eval`，直接 `integrate.solve_ivp(decay, [0, 5], [1.0])`，`sol.t` 会是什么样的？

参考答案：`sol.t` 只包含求解器**内部实际步进**的时间点（一般是数量不多的若干个），而不是均匀的网格。`t_eval` 的作用就是「在不干扰步进的前提下，额外记录你指定时刻的解」，相关对齐逻辑见主循环中 `t_eval` 的 `searchsorted` 处理（[`_ivp/ivp.py:711-728`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_ivp/ivp.py#L711-L728)）。

---

### 4.3 trapezoid：给一组样本，用梯形法算积分

#### 4.3.1 概念说明

`trapezoid` 属于「固定样本积分」。它处理的输入是**已经采好的离散点** `y`（以及对应的 `x`），而不是一个函数。它的几何直觉非常简单：把相邻两个样本点用直线连起来，求这些梯形的面积之和。

对于样本 \( (x_i, y_i) \)，复合梯形公式为

\[
\int y(x)\,\mathrm{d}x \;\approx\; \sum_{i=0}^{n-2} \frac{x_{i+1}-x_i}{2}\,(y_i + y_{i+1}).
\]

如果样本等距、间距为 `dx`，公式简化为经典的 \( \mathrm{dx}\cdot\bigl(\tfrac{y_0+y_{n-1}}{2} + \sum_{i=1}^{n-2} y_i\bigr) \)。

`trapezoid` 只返回**一个**数值（或沿某轴积分后的数组），**不提供误差估计**——因为它无法回到原函数去取更多点做对照。和 `quad` 相比，这是一个本质区别：`quad` 自适应取点并给出 `abserr`，`trapezoid` 只在给定样本上一次性算完。

#### 4.3.2 核心流程

`trapezoid` 的实现极其精炼，核心就两步：

1. **算相邻样本的「间距」`d`**：
   - 若给了 `x`：`d = x[1:] - x[:-1]`（非等距）；
   - 若没给 `x`：`d = dx`（等距，默认 1.0）。
2. **求和**：`ret = sum( d * (y[1:] + y[:-1]) / 2 )`，沿指定 `axis` 求和。

它还通过 `@xp_capabilities()` 装饰器支持 NumPy 以外的数组后端（数组 API 标准），但这不影响我们理解核心公式。

#### 4.3.3 源码精读

`trapezoid` 定义在 `_quadrature.py`，签名如下。注意它没有 `func` 参数——输入是样本 `y`：

[`_quadrature.py:22-23`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadrature.py#L22-L23) —— `trapezoid(y, x=None, dx=1.0, axis=-1)` 的签名，`@xp_capabilities()` 装饰器让它兼容数组 API。

间距 `d` 的计算分「给了 `x`」和「没给 `x`」两路（[`_quadrature.py:136-149`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadrature.py#L136-L149)）：

[`_quadrature.py:140-141`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadrature.py#L140-L141) —— 给了 `x` 时，`d = x[1:] - x[:-1]`，即相邻样本点的间距。

核心求和就是复合梯形公式的直接翻译：

[`_quadrature.py:150-154`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadrature.py#L150-L154) —— `ret = xp.sum(d * (y[1:] + y[:-1]) / 2.0, axis=axis, ...)`，这正是 \( \sum d_i\,(y_i+y_{i+1})/2 \)。

> `integrate.trapezoid` 这个名字来自 `__init__.py` 的 [`__init__.py:103`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/__init__.py#L103) `from ._quadrature import *`。

#### 4.3.4 代码实践

**实践目标**：对 \( y = \sin(x) \) 在 \( [0, \pi] \) 上的等距样本用 `trapezoid` 积分，观察样本数对精度的影响，并与解析解 2 对比。

**操作步骤**：

1. 新建 `demo_trapz.py`（示例代码）：

   ```python
   # 示例代码
   import numpy as np
   from scipy import integrate

   for n in [5, 21, 101]:
       x = np.linspace(0, np.pi, n)   # 等距样本
       y = np.sin(x)
       val = integrate.trapezoid(y, x)
       print(f"n={n:3d}: trapezoid={val:.10f}, 偏差={abs(val-2.0):.2e}")
   print("解析解 = 2.0")
   ```

2. 运行 `python demo_trapz.py`。

**需要观察的现象**：
- 样本数 `n` 越大，结果越接近 2；
- 偏差大致随 `n` 的增大按 \( O(1/n^2) \) 下降（梯形法的典型收敛阶）。

**预期结果**：`n=5` 时偏差可能在 `1e-2` 量级，`n=101` 时偏差降到 `1e-5` 量级。具体数值待本地验证。

#### 4.3.5 小练习与答案

**练习 1**：同样这组 `sin` 样本，比较 `trapezoid(y, x)` 和 `trapezoid(y, dx=x[1]-x[0])` 的结果。它们应该一样吗？

参考答案：一样。因为 `x` 是等距的，`x[1:]-x[:-1]` 每个元素都等于 `dx`，两条代码路径（[`_quadrature.py:140-141`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadrature.py#L140-L141) 与 [`_quadrature.py:136-137`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadrature.py#L136-L137)）算出的 `d` 完全相同。

**练习 2**：为什么 `trapezoid` 不返回误差估计，而 `quad` 会？

参考答案：`quad` 持有**函数本身**，可以自适应地多取点、用高低阶公式之差估计误差；`trapezoid` 只有**固定样本**，无法再回到函数取点，自然没有误差估计的依据。这也是「函数积分」与「固定样本积分」的本质分界。

---

## 5. 综合实践

把三个函数串起来，完成一个「一条龙」脚本，覆盖本讲全部内容：

> 用 `quad` 计算 \( \int_0^{\pi}\sin(x)\,\mathrm{d}x \)；用 `solve_ivp` 求解 \( \mathrm{d}y/\mathrm{d}t=-y,\ y(0)=1 \) 到 \( t=5 \)；用 `trapezoid` 对 \( \sin(x) \) 的样本积分。三者都与解析解对比。

参考实现（示例代码）：

```python
# 示例代码：u1-l3 综合实践
import numpy as np
from scipy import integrate

# (1) quad: 函数积分
q, q_err = integrate.quad(np.sin, 0, np.pi)
print(f"[quad]      结果={q:.10f}  解析=2.0            偏差={abs(q-2.0):.2e}")

# (2) solve_ivp: ODE 初值问题
sol = integrate.solve_ivp(lambda t, y: -y, [0, 5], [1.0],
                          dense_output=True)
y_end = sol.y[0, -1]
y_end_exact = np.exp(-5)
print(f"[solve_ivp] 末值={y_end:.10e}  解析={y_end_exact:.10e}  "
      f"偏差={abs(y_end-y_end_exact):.2e}  nfev={sol.nfev}")

# (3) trapezoid: 固定样本积分
x = np.linspace(0, np.pi, 101)
t_val = integrate.trapezoid(np.sin(x), x)
print(f"[trapezoid] 结果={t_val:.10f}  解析=2.0            偏差={abs(t_val-2.0):.2e}")
```

**运行后请检查**：
- 三个「偏差」都应该很小；
- 思考：为什么 `quad` 和 `solve_ivp` 都能给出很高的精度，而 `trapezoid` 的精度受样本数 `n` 限制？（提示：谁拥有「函数」，谁只拥有「样本」。）

## 6. 本讲小结

- `from scipy import integrate` 之后能直接用 `integrate.quad` / `integrate.solve_ivp` / `integrate.trapezoid`，是因为 [`__init__.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/__init__.py) 把内部模块的名字搬到了顶层。
- `quad(func, a, b)` 是**函数积分**：把函数交给 QUADPACK 自适应取点，返回 `(y, abserr)`；最常用的有限区间路径走 `_quadpack._qagse`（QAGS）。
- `solve_ivp(fun, t_span, y0, method='RK45')` 是 **ODE 初值问题**新 API：把 `method` 字符串查 `METHODS` 表换成求解器类，主循环反复 `solver.step()`，最后返回 `OdeResult`（含 `t/y/nfev` 等）。
- `trapezoid(y, x)` 是**固定样本积分**：直接套复合梯形公式 \( \sum d_i(y_i+y_{i+1})/2 \)，只返回数值、不返回误差估计。
- 「函数积分 vs 固定样本积分」的本质区别在于：前者持有函数、能自适应取点并估计误差；后者只有样本、一次性算完。
- 三个函数的返回结构差异（`(y, abserr)` vs `OdeResult` vs 单个数值）是后续深入学习各模块的「路标」。

## 7. 下一步学习建议

你已经能跑通三大主线函数了。接下来按兴趣和需要选择方向：

- 想深入**固定样本积分**：进入第 2 单元，学习 `trapezoid` 的累计版本 `cumulative_trapezoid`、更精确的 `simpson`，以及 `romb`/`newton_cotes`/`fixed_quad`。
- 想深入**函数积分**：进入第 3 单元，理解 `quad` 的自适应 Gauss-Kronrod 策略、容差参数 `epsabs/epsrel/limit`，以及 `dblquad`/`nquad` 等多重积分。
- 想深入 **ODE 求解**：从第 6 单元开始，系统学习 `solve_ivp` 的 `t_eval`/`dense_output`/事件检测，再到第 7–9 单元的求解器内部（`OdeSolver` 基类、Runge-Kutta、隐式刚性求解器、LSODA）。

建议下一步先读 [`scipy/integrate/_quadrature.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/integrate/_quadrature.py)（最贴近本讲的 `trapezoid` 实现），再进入对应单元。
