# hessian 的实现

## 1. 本讲目标

本讲紧接 [u3-l1 jacobian 的实现](u3-l1-jacobian-implementation.md)，打开 `scipy.differentiate` 三件套的最顶层：`hessian`。学完后你应当能够：

1. 说清楚为什么「海森矩阵 = 梯度的雅可比」，并看懂源码里 `jacobian(df, x)` 这一行为什么就等于求二阶导。
2. 解释 `hessian` 为什么要对**内层** `jacobian` 的 `rtol` 收紧 100 倍、`rtol` 过小时为什么会触发 `RuntimeWarning`、以及它被「钳制」到 `rtol_min` 的逻辑。
3. 理解 `nfev` 是如何跨越「外层 + 内层」两次雅可比累计出来的，以及为什么最终结果把 `df` 改名成 `ddf`、并删掉了 `nit`。

本讲不再重复 `jacobian`/`derivative` 的内部机制（扰动注入、stencil、逐元素迭代框架），只聚焦 `hessian` 在它们之上做的**组合、容差收紧与记账**三件事。

## 2. 前置知识

### 2.1 海森矩阵是什么

对于一个二阶可导的多元标量函数 \(f: \mathbf{R}^m \rightarrow \mathbf{R}\)，它的**梯度**是一个向量：

\[
(\nabla f)_i = \frac{\partial f}{\partial x_i}, \qquad \nabla f \in \mathbf{R}^m
\]

而**海森矩阵（Hessian）**是二阶偏导数组成的 \(m \times m\) 方阵：

\[
H_{ij} = \frac{\partial^2 f}{\partial x_i\, \partial x_j}
\]

如果 \(f\) 足够光滑，由 Schwarz 定理（混合偏导对称性）有 \(H_{ij} = H_{ji}\)，即海森矩阵是对称的。

### 2.2 关键观察：海森 = 梯度的雅可比

梯度 \(\nabla f\) 本身可以看作一个「从 \(\mathbf{R}^m\) 到 \(\mathbf{R}^m\)」的向量值函数。对它再求一次雅可比，就得到：

\[
J(\nabla f)_{ij} = \frac{\partial (\nabla f)_i}{\partial x_j}
                 = \frac{\partial}{\partial x_j}\!\left(\frac{\partial f}{\partial x_i}\right)
                 = \frac{\partial^2 f}{\partial x_i\, \partial x_j}
                 = H_{ij}
\]

也就是说：

\[
\boxed{\;\text{Hessian}(f) \;=\; \text{Jacobian}(\text{gradient}(f)) \;=\; \text{Jacobian}(\text{Jacobian}(f))\;}
\]

这就是 `hessian` 实现的全部数学内核——**它不发明任何新的多元二阶差分格式，而是把二阶导翻译成「雅可比的雅可比」**，完全复用已经可靠的 `jacobian` 黑盒。这与 u3-l1 中「雅可比 = 若干个一元 `derivative`」的翻译思想一脉相承：每一层都只做组合，不重写底层算法。

### 2.3 复合数值微分的误差传播

把两次数值微分串起来，总误差大致是「外层误差 + 内层误差放大」。为了让外层看到的误差估计**主要反映外层本身**，一个标准技巧是**把内层做得远比外层精确**，使内层误差可忽略。本讲会看到源码正是用「内层 `rtol` 收紧 100 倍」来实现这一点。

## 3. 本讲源码地图

本讲几乎全部内容都集中在一个文件里：

| 文件 | 作用 |
| --- | --- |
| [`scipy/differentiate/_differentiate.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py) | `hessian` 函数（含 docstring）位于其中，调用同文件的 `jacobian`，后者再调用 `derivative`。 |

`hessian` 的公开导出由 [`scipy/differentiate/__init__.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/__init__.py) 的 `__all__ = ['derivative', 'jacobian', 'hessian']` 划定。本讲聚焦 `_differentiate.py` 中 `hessian` 的函数体（约第 951–1141 行）。

调用层次回顾（自底向上）：

```
derivative   ← 逐元素一阶有限差分（u2 系列已剖析）
   ↑
jacobian     ← 对角扰动注入 + preserve_shape=True，委托 derivative（u3-l1）
   ↑
hessian      ← 嵌套两次 jacobian + 容差收紧 + nfev 记账（本讲）
```

## 4. 核心概念与源码讲解

### 4.1 嵌套 jacobian 求二阶导

#### 4.1.1 概念说明

「嵌套两次 jacobian」听起来抽象，落到代码上其实非常直白。`hessian` 定义一个内部函数 `df`，它就是对原函数 `f` 调一次 `jacobian`（也就是求梯度）；然后再对 `df` 调一次 `jacobian`。第一次 `jacobian` 把 \(f\) 变成 \(\nabla f\)，第二次 `jacobian` 把 \(\nabla f\) 变成 \(J(\nabla f) = H\)。

需要特别留意**形状契约**（参见 u3-l1）：

- 用户 `f: \mathbf{R}^m \rightarrow \mathbf{R}`，输入 `x` 形状 `(m,)`。
- 内层 `jacobian(f, x)` 返回梯度，`temp.df` 形状 `(m,)`（标量输出时 `n=1` 的轴被自然消去，结果就是梯度）。
- 这个 `(m,)` 的梯度被**外层 jacobian 重新解释**为「输出维度 `n = m`」的向量值函数，于是外层 `jacobian(df, x)` 的结果 `res.df` 形状为 `(m, m)`——正好是海森矩阵。

> 小贴士：梯度的 `m` 轴在外层被当作「输出轴 `n`」复用，这是嵌套能成立的关键。最终 `res.df[i, j] = ∂²f / (∂x_i ∂x_j)`，由 Schwarz 对称性，下标顺序无关紧要。

#### 4.1.2 核心流程

`hessian` 的主体可以用下面这段伪代码概括（省略容差与记账细节）：

```text
function hessian(f, x, 选项):
    定义 df(x):                          # df = 梯度函数
        temp = jacobian(f, x, 内层选项)    #   内层：f -> ∇f
        记账(temp.nfev)
        return temp.df                    #   返回梯度

    res = jacobian(df, x, 外层选项)        # 外层：∇f -> J(∇f) = H
    整理 res（改名 ddf、累计 nfev、删 nit）
    return res
```

关键特征：

1. **内外层共用同一套选项**（`maxiter`/`order`/`initial_step`/`step_factor`），唯一例外是内层 `rtol` 被收紧（见 4.2）。
2. **`df` 是闭包**：它捕获了外层的 `f`、`rtol`、`atol`、`kwargs`，以及一个跨调用共享的 `nfev` 列表（见 4.3）。
3. 外层 `jacobian` 完全不知道 `df` 内部其实又跑了一次 `jacobian`——对它而言 `df` 就是个普通的向量值黑盒。

#### 4.1.3 源码精读

先看 `hessian` 的签名（注意它**没有** `step_direction`、`args`、`preserve_shape`、`callback` 这些 `derivative`/`jacobian` 才有的参数）：

[`_differentiate.py:L953-L954`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L953-L954) —— `hessian` 的公开签名，选项比 `jacobian` 更少（无 `step_direction`）。

```python
def hessian(f, x, *, tolerances=None, maxiter=10,
            order=8, initial_step=0.5, step_factor=2.0):
```

把公共选项打包成 `kwargs`，方便内外层复用：

[`_differentiate.py:L1105-L1106`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L1105-L1106) —— 把四个与容差无关的选项打包，内外层 `jacobian` 共用。

```python
kwargs = dict(maxiter=maxiter, order=order, initial_step=initial_step,
              step_factor=step_factor)
```

核心的嵌套两行——内层 `df` 与外层 `jacobian`：

[`_differentiate.py:L1125-L1132`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L1125-L1132) —— 整个「雅可比的雅可比」就浓缩在这里：`df` 是梯度，外层对 `df` 再求一次雅可比得到海森矩阵。

```python
def df(x):
    tolerances = dict(rtol=rtol/100, atol=atol)      # 内层 rtol 收紧 100 倍
    temp = jacobian(f, x, tolerances=tolerances, **kwargs)  # 内层：∇f
    nfev.append(temp.nfev if len(nfev) == 0 else temp.nfev.sum(axis=-1))
    return temp.df                                     # 返回梯度

nfev = []  # track inner function evaluations
res = jacobian(df, x, tolerances=tolerances, **kwargs)  # jacobian of jacobian
```

行尾注释 `# jacobian of jacobian` 是作者本人对这一行的概括。注意外层用的是**原始** `tolerances`（未收紧），而内层 `df` 里用的是 `rtol/100`（收紧）。这正是下一节的主题。

docstring 的 Notes 段也用作者的话点明了这套设计：

[`_differentiate.py:L1059-L1065`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L1059-L1065) —— docstring 说明 hessian 通过嵌套 `jacobian` 实现，且内层 `rtol` 收紧 100 倍。

> Currently, `hessian` is implemented by nesting calls to `jacobian`. All options passed to `hessian` are used for both the inner and outer calls with one exception: the `rtol` used in the inner `jacobian` call is tightened by a factor of 100 with the expectation that the inner error can be ignored.

#### 4.1.4 代码实践

**实践目标**：用最简例子亲眼确认「`hessian` 的结果 = 两次 `jacobian` 的结果」，从而验证嵌套思想。

**操作步骤**：

```python
# 示例代码：手动复现「雅可比的雅可比」
import numpy as np
from scipy.differentiate import jacobian, hessian

f = np.sin            # f: R -> R，单变量时 m=1，海森是 1x1 的二阶导
x = np.asarray([1.0]) # 形状 (1,)，满足 hessian 要求 x 至少 1 维

# 第 1 层：梯度（对 sin 来说是 cos）
g = jacobian(f, x)
print("gradient df.shape =", g.df.shape, " value =", g.df)

# 第 2 层：对梯度再求雅可比 = 海森（对 sin 来说是 -sin）
H_manual = jacobian(lambda xx: jacobian(f, xx).df, x)
print("manual Hessian =", H_manual.df)

# 直接调用 hessian 对照
res = hessian(f, x)
print("hessian ddf    =", res.ddf, " -sin(1) =", -np.sin(1.0))
```

**需要观察的现象**：

- `g.df` 形状为 `(1,)`，值约为 `cos(1) ≈ 0.5403`。
- `H_manual.df` 与 `res.ddf` 都约为 `-sin(1) ≈ -0.8415`，且两者数值接近（不严格相等，因为各自独立迭代、误差不同）。
- `res.ddf` 的形状为 `(1, 1)`——单变量时的 \(1\times1\) 海森矩阵。

**预期结果**：手动套两层 `jacobian` 与一次 `hessian` 给出近似相等的二阶导估计。具体打印数值待本地验证（受默认容差与浮点影响）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `hessian` 要求 `f: \mathbf{R}^m \rightarrow \mathbf{R}` 是**标量输出**？如果 `f` 输出是向量（`n>1`），嵌套还成立吗？

> **答案**：海森矩阵定义在标量函数上（二阶偏导 \(H_{ij}\)）。若 `f` 输出向量，则需要的是「张量雅可比」而非海森。源码里内层 `jacobian(f, x).df` 对向量 `f` 会得到 `(n, m)`，外层再求雅可比会得到 `(n, m, m)` 形状的对象——这不再是标准海森，`hessian` 也未对此做处理，故仅支持标量输出。

**练习 2**：`hessian` 的签名为什么没有 `step_direction` 参数（而 `jacobian` 有）？

> **答案**：`step_direction` 用于在定义域边界附近做单侧差分。`hessian` 通过嵌套 `jacobian` 实现，内层 `jacobian` 在 `df` 内部被调用时并未透传 `step_direction`（`kwargs` 里不含它），整套机制默认走中心差分。若你的函数在边界附近需要单侧差分，应直接使用 `jacobian` 而非 `hessian`。

---

### 4.2 内层 rtol 收紧与告警机制

#### 4.2.1 概念说明

把两次数值微分串起来后，外层 `jacobian` 看到的「函数值」其实是内层 `jacobian` 算出的梯度——而梯度本身带有数值误差。如果内层误差和外层容差同量级，外层的误差估计就会被内层噪声污染，变得不可信。

解决办法是一个经典的「误差预算」分配：**让内层远比外层精确**。源码让内层 `rtol` 只有外层的 \(1/100\)，这样内层误差大约比外层容差小两个数量级，可以忽略不计。

但这带来一个下限：内层 `rtol = rtol/100` 不能小于机器精度 `eps`，否则内层 `jacobian` 根本无法达到（数值上无意义）。反推可得：用户传入的 `rtol` 不应小于 `100 * eps`。源码据此设置了阈值 `rtol_min = 100 * eps`，并在用户违反时发出 `RuntimeWarning`、同时把 `rtol` 钳制到 `rtol_min`。

#### 4.2.2 核心流程

1. 取出用户 `tolerances` 里的 `atol`、`rtol`；若 `rtol` 为 `None`，默认 `rtol = sqrt(eps)`（与 `derivative` 保持一致）。
2. 计算 `rtol_min = 100 * eps`。
3. 若 `0 < rtol < rtol_min`：发出 `RuntimeWarning`，并把 `rtol` 钳制为 `rtol_min`。
4. 内层 `df` 使用 `rtol/100`；外层 `jacobian` 使用（可能已钳制的）`rtol`。

一个量级例子（float64，`eps ≈ 2.22e-16`）：

| 量 | 表达式 | float64 约值 |
| --- | --- | --- |
| 默认外层 `rtol` | \(\sqrt{\varepsilon}\) | \(1.49\times10^{-8}\) |
| 内层 `rtol` | \(\sqrt{\varepsilon}/100\) | \(1.49\times10^{-10}\) |
| `rtol_min`（下限） | \(100\varepsilon\) | \(2.22\times10^{-14}\) |

可见默认情况下内层 `rtol ≈ 1.5e-10` 远大于 `eps`，可正常达到；而内层误差 `\sim 1.5e-10` 比外层容差 `\sim 1.5e-8` 小 100 倍，可忽略——这正是收紧 100 倍的设计意图。

#### 4.2.3 源码精读

默认 `rtol` 的计算（注释明确写「与 `derivative` 保持一致」）：

[`_differentiate.py:L1107-L1115`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L1107-L1115) —— 解包容差并给出默认 `rtol = sqrt(eps)`，与 `derivative` 一致。

```python
tolerances = {} if tolerances is None else tolerances
atol = tolerances.get('atol', None)
rtol = tolerances.get('rtol', None)

xp = array_namespace(x)
x0 = xp_promote(x, force_floating=True, xp=xp)

finfo = xp.finfo(x0.dtype)
rtol = finfo.eps**0.5 if rtol is None else rtol  # keep same as `derivative`
```

阈值与告警：

[`_differentiate.py:L1117-L1123`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L1117-L1123) —— 若 `0 < rtol < 100*eps`，发出 `RuntimeWarning` 并把 `rtol` 钳制到下限。

```python
# tighten the inner tolerance to make the inner error negligible
rtol_min = finfo.eps * 100
message = (f"The specified `{rtol=}`, but error estimates are likely to be "
           f"unreliable when `rtol < {rtol_min}`.")
if 0 < rtol < rtol_min:  # rtol <= 0 is an error
    warnings.warn(message, RuntimeWarning, stacklevel=2)
    rtol = rtol_min
```

几个要点：

- `f"{rtol=}"` 是 Python 3.8+ 的「自显示」f-string，会把变量名和值一起输出，例如 `rtol=1e-15`，便于用户在告警里直接看到自己传的值。
- 条件是 `0 < rtol < rtol_min`，只处理「过小但为正」的情形。注释 `# rtol <= 0 is an error` 表示 `rtol <= 0`（尤其是负数）不在本处处理——它们会被下游 `jacobian`/`derivative` 的输入校验（参见 u2-l1：容差必须非负）以 `ValueError` 拒绝。
- 钳制后 `rtol = rtol_min`，于是内层 `rtol/100 = eps`，刚好退到机器精度边界——不再继续收紧，避免无意义的迭代。

内层使用收紧后的 `rtol`：

[`_differentiate.py:L1126`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L1126) —— 内层 `df` 把 `rtol` 再除以 100，使内层误差可忽略。

```python
    tolerances = dict(rtol=rtol/100, atol=atol)
```

#### 4.2.4 代码实践

**实践目标**：触发并读懂 `RuntimeWarning`，验证「`rtol` 过小 → 告警 + 钳制」。

**操作步骤**：

```python
# 示例代码：观察过小 rtol 触发的告警
import warnings
import numpy as np
from scipy.differentiate import hessian

with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    res = hessian(np.sin, [1.0], tolerances=dict(rtol=1e-15))
    for wi in w:
        print("category :", wi.category.__name__)
        print("message  :", wi.message)
```

**需要观察的现象**：

- 会捕获到一条 `RuntimeWarning`，其 message 形如：
  `The specified 'rtol=1e-15', but error estimates are likely to be unreliable when 'rtol < 2.220446049250313e-14'.`
  其中 `2.22e-14` 正是 float64 下的 `rtol_min = 100 * eps`。
- 改成 `tolerances=dict(rtol=1e-12)`（大于 `rtol_min`）再跑，应当**没有**告警。

**预期结果**：`rtol=1e-15` 触发告警并被钳制；`rtol=1e-12` 不触发。该告警行为与 [`test_small_rtol_warning`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L700-L703) 的断言一致。

#### 4.2.5 小练习与答案

**练习 1**：用户传 `rtol=None`（默认）时，内层实际使用的 `rtol` 是多少？它是否大于 `eps`？

> **答案**：默认 `rtol = sqrt(eps)`，内层 `rtol = sqrt(eps)/100`。对 float64，约为 `1.49e-10`，远大于 `eps ≈ 2.22e-16`，可正常达到，故不会触发告警。

**练习 2**：把 `rtol` 从 `1e-8` 减小到 `1e-12`，结果一定会更精确吗？

> **答案**：不一定。`rtol` 减小意味着外层容差更严，外层会迭代更多轮；但步长缩到一定程度后会进入「消去误差主导」区（参见 u2-l5），误差不再下降反而回升，触发 `status=-1` 提前终止。此外过小的 `rtol` 还可能让内层误差不再可忽略。精度受「截断误差 vs 消去误差」的 U 形曲线制约，不是越小越好。

---

### 4.3 nfev 累计与属性重命名

#### 4.3.1 概念说明

这是 `hessian` 里最巧妙的一段「记账」。问题背景如下：

- 用户关心的是「我的原函数 `f` 一共被求值了多少次」，即 `nfev`。
- 但外层 `jacobian(df, x)` 返回的 `res.nfev` 统计的是**外层**调用了多少次 `df`（梯度函数），而 `df` 内部每一次调用都跑了**整整一次** `jacobian`（对 `f` 的许多次求值）。
- 也就是说，外层的 `nfev` 完全不能反映 `f` 的真实求值次数——它低估了。

因此 `hessian` 必须自己**拦截并累计内层** `jacobian` 报告的 `nfev`，才能给用户一个有意义的函数求值计数。办法是用一个跨 `df` 调用存活的列表 `nfev`（闭包变量），每次 `df` 被调用时把内层的 `temp.nfev` 追加进去，最后再汇总。

至于「属性重命名」：外层 `jacobian` 返回的对象把导数叫 `df`，但这是**二阶**导数（海森），沿用 `df` 会误导用户。所以源码在末尾把 `res.df` 改名为 `res.ddf`（double derivative），并删除了只反映外层迭代次数的 `res.nit`。

#### 4.3.2 核心流程

记账与整理的全过程：

1. **采集**：在 `df` 内，每次内层 `jacobian` 结束后，把 `temp.nfev` 追加到列表 `nfev`。
   - 第 1 次（外层 `_initialize` 的预检调用）：`temp.nfev` 形状已是 `(m, m)`，直接追加。
   - 后续每次（外层每轮迭代）：外层 `wrapped` 给 `df` 传入的扰动矩阵多出一个「求值点」尾轴，使 `temp.nfev` 形状变成 `(m, m, n_abscissae)`，需沿最后一轴 `sum` 塌缩回 `(m, m)`。
2. **累计**：外层结束后，把列表 `stack` 成 `(n_calls, m, m)`，沿 `axis=0` 做 `cumulative_sum`，得到「截至第 t 次外层调用，每个 `[i,j]` 元素累计的 `f` 求值数」。
3. **取值**：用外层 `res.nit`（每个元素收敛时所经历的外层迭代数）做 `take_along_axis`，取出「收敛那一刻」的累计 `nfev`，写回 `res.nfev`。
4. **改名/清理**：`res.ddf = res.df`，删除 `res.df` 与 `res.nit`。

形状推演（以单点 `x` 形状 `(m,)`、标量 `f` 为例）：

| 阶段 | 数组 | 形状 |
| --- | --- | --- |
| 内层首次 `temp.nfev` | 每元素 `f` 求值数 | `(m, m)` |
| 内层后续 `temp.nfev` | 多一个求值点轴 | `(m, m, n_abscissae)` → `sum(-1)` → `(m, m)` |
| `stack` + `cumsum` | 每次外层调用一行 | `(n_calls, m, m)` |
| `take_along_axis(res.nit)` | 取收敛时刻 | `(m, m)` = 海森形状 |

#### 4.3.3 源码精读

`df` 内的采集（注意 `if len(nfev) == 0` 区分首次与后续）：

[`_differentiate.py:L1127-L1128`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L1127-L1128) —— 把内层 `nfev` 追加到共享列表；首次保持原形状，后续沿求值点轴求和。

```python
        temp = jacobian(f, x, tolerances=tolerances, **kwargs)
        nfev.append(temp.nfev if len(nfev) == 0 else temp.nfev.sum(axis=-1))
```

累计、取值、改名、清理：

[`_differentiate.py:L1134-L1141`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L1134-L1141) —— 把逐次内层 `nfev` 累加，按外层 `nit` 取出收敛时刻的总求值数；再把 `df` 改名 `ddf`，删除误导性的 `nit`。

```python
nfev = xp.cumulative_sum(xp.stack(nfev), axis=0)
res_nit = xp.astype(res.nit[xp.newaxis, ...], xp.int64)  # appease torch
res.nfev = xp.take_along_axis(nfev, res_nit, axis=0)[0]
res.ddf = res.df
del res.df  # this is renamed to ddf
del res.nit  # this is only the outer-jacobian nit

return res
```

逐行说明：

- `xp.stack(nfev, axis=0)`：把每次外层调用采集到的 `(m, m)` 堆成 `(n_calls, m, m)`。
- `xp.cumulative_sum(..., axis=0)`：沿「调用次序」轴累加，得到运行总数。用的是数组 API 的 `cumulative_sum`（而非 NumPy 专有的 `cumsum`），保证跨后端可用（详见 u4-l4）。
- `res.nit[xp.newaxis, ...]`：把 `(m, m)` 的外层迭代数变成 `(1, m, m)`，以匹配 `take_along_axis` 的索引维度；注释 `# appease torch` 指出 Torch 要求索引为 `int64`，故 `xp.astype(..., xp.int64)`。
- `xp.take_along_axis(nfev, res_nit, axis=0)[0]`：用每个元素的 `nit` 在累计数组里「按行取值」，得到该元素收敛时的累计 `nfev`；`[0]` 去掉插进去的那一维，回到 `(m, m)`。
- `res.ddf = res.df` + `del res.df`：把「雅可比结果 `df`」语义化改名为「海森结果 `ddf`」。
- `del res.nit`：外层 `nit` 只数外层迭代，与真实工作量不符，删去以免误导。最终 `hessian` 的返回对象只含 `success`/`status`/`ddf`/`error`/`nfev`（参见 docstring 的 Returns 段）。

#### 4.3.4 代码实践

**实践目标**：验证 `res.nfev[i, j]` 确实等于「只为算 `ddf[i, j]` 这一个二阶偏导所需的原函数求值次数」。

**操作步骤**：

```python
# 示例代码：对照「整体 hessian」与「单元素 hessian」的 nfev
import numpy as np
from scipy.differentiate import hessian

z = np.asarray([0.5, 0.25])

def f1(z):
    x, y = np.broadcast_arrays(*z)
    return np.sin(x) * y ** 3

# 整体求海森
res = hessian(f1, z, initial_step=10)

# 只把 x0 当变量、固定 x1，单独算 ddf[0,0]
res00 = hessian(lambda x: f1([x[0], z[1]]), z[0:1], initial_step=10)

print("res.nfev      =", res.nfev)
print("res.nfev[0,0] =", res.nfev[0, 0])
print("res00.nfev[0,0]=", res00.nfev[0, 0])
```

**需要观察的现象**：

- `res.nfev` 是 `(2, 2)` 的矩阵，每个位置给出对应二阶偏导的 `f` 求值次数。
- `res.nfev[0, 0]` 与「只算单元素」的 `res00.nfev[0, 0]` **相等**——说明 `nfev` 的累计是按元素独立计数的，而非笼统总数。

**预期结果**：`res.nfev[0,0] == res00.nfev[0,0]`（数值相等）。这正是 [`test_nfev`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L674-L695) 断言的内容（该测试还额外用一个 `f1.nfev` 计数器直接数原函数被调次数，三方对照）。

#### 4.3.5 小练习与答案

**练习 1**：为什么采集 `nfev` 时，第一次调用用 `temp.nfev` 原样，而后续调用要 `temp.nfev.sum(axis=-1)`？

> **答案**：第一次发生在 `_initialize` 的预检阶段，外层 `wrapped` 传给 `df` 的扰动矩阵形状是 `(m, m)`，内层 `temp.nfev` 形状已是 `(m, m)`，与海森形状一致，直接用。后续每次是外层迭代中的批量求值，扰动矩阵多出一个尾轴（多个新求值点），内层 `temp.nfev` 形状为 `(m, m, n_abscissae)`，必须沿该尾轴求和才能塌缩回 `(m, m)`。

**练习 2**：`hessian` 返回对象里为什么没有 `nit`，而 `jacobian`/`derivative` 都有？

> **答案**：`hessian` 的 `nit` 只反映**外层** `jacobian` 的迭代次数，但每个外层迭代内部又跑了完整的一次内层 `jacobian`（含多次内层迭代），所以外层 `nit` 既不等于总迭代数、也无法直观换算成工作量，留着会误导用户。源码因此 `del res.nit`。函数求值工作量改由重新累计的 `nfev` 表达。

---

## 5. 综合实践

把本讲三个模块串起来，完成一个端到端的小任务：用 `hessian` 计算 Rosenbrock 函数在随机点的海森矩阵，与解析参考值 `rosen_hess` 对照；再故意传入过小的 `rtol`，触发并解释告警。

```python
# 示例代码：综合实践
import warnings
import numpy as np
from scipy.differentiate import hessian
from scipy.optimize import rosen, rosen_hess

rng = np.random.default_rng(4589245925010)
m = 3
x = rng.random(m)            # 随机点，形状 (m,)

# 任务 1：数值海森 vs 解析海森
res = hessian(rosen, x)
ref = rosen_hess(x)          # 解析参考值，形状 (m, m)
print("ddf shape      :", res.ddf.shape)
print("max |ddf - ref|:", np.max(np.abs(res.ddf - ref)))
print("res.success    :", res.success)
print("res.nfev       :\n", res.nfev)
print("res has 'nit'? :", hasattr(res, "nit"), " has 'df'?", hasattr(res, "df"))

# 任务 2：故意传入过小 rtol，触发告警并解释
with warnings.catch_warnings(record=True) as w:
    warnings.simplefilter("always")
    res2 = hessian(rosen, x, tolerances=dict(rtol=1e-15))
    print("warnings       :", [str(wi.message) for wi in w if issubclass(wi.category, RuntimeWarning)])
```

**你需要回答的问题（把三个模块的知识用上）**：

1. `res.ddf.shape` 为什么是 `(3, 3)`？请用「梯度的 `m` 轴被外层当作输出轴 `n`」解释。（对应 4.1）
2. `res.nfev` 为什么远大于 `derivative`/`jacobian` 通常的 `nfev`？请用「外层每个迭代都内嵌一次完整 jacobian」解释。（对应 4.3）
3. `hasattr(res, "nit")` 与 `hasattr(res, "df")` 应分别为 `False`/`False`，为什么？（对应 4.3）
4. 任务 2 触发的告警里，`rtol < ???` 的阈值是多少？它等于 `100 * eps` 还是 `sqrt(eps)`？为什么内层 `rtol` 不能低于 `eps`？（对应 4.2）

**预期结果**：`np.allclose(res.ddf, ref)` 为 `True`（默认 float64 下精度很高）；`res.nit` 与 `res.df` 均不存在；告警阈值为 `100 * eps ≈ 2.22e-14`。具体打印数值待本地验证。

## 6. 本讲小结

- `hessian` 的数学内核是 **海森 = 雅可比的雅可比**：内层 `jacobian(f, x)` 求梯度，外层 `jacobian(df, x)` 对梯度再求雅可比，得到 `(m, m)` 海森矩阵；它不发明新的二阶差分格式。
- 内层 `rtol` 被**收紧 100 倍**（`rtol/100`），使内层误差相对外层容差可忽略；默认外层 `rtol = sqrt(eps)`，与 `derivative` 一致。
- 用户 `rtol` 过小时（`0 < rtol < 100*eps`）会触发 `RuntimeWarning` 并被**钳制**到 `rtol_min = 100*eps`，因为内层 `rtol/100` 不能低于机器精度 `eps`。
- 外层 `jacobian` 的 `nfev` 只数对 `df` 的调用，不能反映原函数 `f` 的真实求值次数；`hessian` 用一个闭包列表跨调用**累计内层 `nfev`**，再用 `cumulative_sum` + `take_along_axis(res.nit)` 取出每个元素收敛时的总求值数。
- 结果对象做了一次**改名清理**：`res.df → res.ddf`（语义化为二阶导），并 `del res.nit`（外层迭代数具有误导性）；最终只暴露 `success`/`status`/`ddf`/`error`/`nfev`。
- `hessian` 的接口比 `jacobian`/`derivative` 更窄：**没有** `step_direction`、`args`、`preserve_shape`、`callback`，默认全程中心差分；若需边界单侧差分或额外参数，应回退使用 `jacobian`。

## 7. 下一步学习建议

- 阅读 [`test_differentiate.py` 的 `TestHessian`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L640-L703)，重点关注 `test_nfev`（按元素计数）与 `test_small_rtol_warning`（告警契约），这是反推实现细节的好材料（对应大纲 u4-l5）。
- 回头对照 `_differentiate.py` 中 `derivative` 的 `rtol = finfo.eps**0.5` 与 `hessian` 的同一行，体会两者为何要「保持一致」，以及 u2-l5 中默认容差过严导致鞍点 `gh-18811` 需要 `atol` 的问题（对应大纲 u4-l3）。
- 若想了解 `hessian` 用到的 `xp.cumulative_sum`/`xp.take_along_axis`/`xpx.at` 等如何跨 NumPy/Torch/JAX 后端工作，继续进入 u4-l4（Array API 后端支持）。
- 至此 `derivative → jacobian → hessian` 的完整层次已讲完。建议从「黑盒使用」（u1）、「单层白盒」（u2）、「组合层」（u3）三个视角各挑一篇复盘，把「翻译 + 组合」的设计哲学吃透。
