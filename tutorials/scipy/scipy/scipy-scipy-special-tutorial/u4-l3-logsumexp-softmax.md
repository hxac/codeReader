# _logsumexp.py：数值稳定的 logsumexp / softmax

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `np.log(np.sum(np.exp(a)))` 为什么会溢出，以及 `scipy.special.logsumexp` 用 **max-shift（先减最大值再指数）** 是如何规避的。
- 读懂 `softmax`、`log_softmax` 与 `logsumexp` 三者的数学关系与各自的稳定实现。
- 理解 `logsumexp` 三个进阶特性：权重 `b`（可负）、`return_sign`（符号分离）、复数输入的相位包裹 `_wrap_radians`。
- 认识 Array API 机制：`array_namespace` / `xp_promote` 如何让同一个纯 Python 函数同时跑在 NumPy、PyTorch、JAX、CuPy、Dask 后端上。

本讲是 U4「纯 Python 包装层」的第三篇。与 U4-l1、U4-l2 不同，这里的三个函数**不是 ufunc**——因为它们跨元素聚合（求和），破坏了 ufunc「逐元素」的前提。但它们也并非简单转发 C 内核，而是用纯 Python 把数值稳定性技巧写得很讲究，是阅读「为什么不用 ufunc」的另一类范本。

## 2. 前置知识

### 2.1 浮点溢出与「大数吃小数」

`np.exp(1000)` 在 `float64` 下会得到 `inf`，因为结果远超双精度最大值（约 \(1.8\times10^{308}\)，对应指数上限约 709）。即使不溢出，`exp(700)` 量级的数相加也会让小项被「吃掉」，损失有效数字。这正是 `log(sum(exp(...)))` 的天然软肋。

### 2.2 log-sum-exp 的数学恒等式

定义：

\[
\mathrm{logsumexp}(a)=\log\!\left(\sum_i e^{a_i}\right)
\]

带权重 \(b_i\) 时：

\[
\mathrm{logsumexp}(a,b)=\log\!\left(\sum_i b_i\,e^{a_i}\right)
\]

关键恒等式（令 \(a_{\max}=\max_i a_i\)）：

\[
\log\!\left(\sum_i e^{a_i}\right)=a_{\max}+\log\!\left(\sum_i e^{a_i-a_{\max}}\right)
\]

由于 \(a_i-a_{\max}\le 0\)，所以 \(e^{a_i-a_{\max}}\in(0,1]\)，**指数绝不会溢出**；移出的 \(a_{\max}\) 在最后加回来即可。这就是贯穿本讲的 **max-shift 技巧**。

### 2.3 复对数的多值性与辐角约定

复对数是多值函数：对每个 \(x\)，存在无穷多个 \(z\) 满足 \(e^z=x\)。`logsumexp` 约定返回虚部落在 \((-\pi,\pi]\) 的那一支（见 [`_logsumexp.py` 文档串][_doc]）。复数求和后的辐角就是「符号」，这就是 `return_sign` 在复数下返回的不是 ±1 而是单位复数的原因。

### 2.4 Array API 是什么

Python Array API 是一个**数组库的统一接口标准**（[data-apis.org](https://data-apis.org/array-api/)）。`xp = array_namespace(a)` 会返回 `a` 所属数组库的「命名空间」（如 `numpy`、`torch`、`jax.numpy`），之后所有 `xp.sum`、`xp.exp` 调用都自动落到同一后端。这样一份纯 Python 代码就能跨后端工作。详见本讲 4.4。

[_doc]: https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_logsumexp.py#L72-L74

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [scipy/special/_logsumexp.py][main] | 本讲主角。纯 Python 实现 `logsumexp`/`softmax`/`log_softmax`，含 max-shift、符号分离、复数相位包裹与 Array API 适配。 |
| [scipy/special/__init__.py][init] | 第 798 行 `from ._logsumexp import logsumexp, softmax, log_softmax` 把三者挂进 `scipy.special` 命名空间；第 829–831 行登记进 `__all__`。 |
| [scipy/_lib/_array_api.py][xpa] | 提供 `xp_promote`、`xp_float_to_complex`、`xp_capabilities` 等跨后端辅助函数。 |
| [scipy/_lib/_array_api_override.py][override] | `array_namespace` 的实现，含全局开关 `SCIPY_ARRAY_API`。 |
| [scipy/special/tests/test_logsumexp.py][test] | 数据驱动测试，可作为本讲实践的「标准答案」参考。 |

[main]: https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_logsumexp.py
[init]: https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/__init__.py#L798
[xpa]: https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/_lib/_array_api.py
[override]: https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/_lib/_array_api_override.py
[test]: https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/tests/test_logsumexp.py

## 4. 核心概念与源码讲解

### 4.1 数值稳定性：max-shift 技巧

#### 4.1.1 概念说明

`logsumexp` 要解决的核心问题：直接按定义 `log(sum(exp(a)))` 计算，当某个 \(a_i\) 较大（比如 1000）时，`exp(a_i)` 立刻溢出成 `inf`，`log(inf)` 给出 `inf`，结果完全失去意义——尽管真正的答案只是略大于 \(a_{\max}\) 的一个有限数。

max-shift 技巧利用第 2.2 节的恒等式，把指数整体平移 \(a_{\max}\)，让所有指数变成非正数，从而把溢出风险消灭在源头。这是整个文件最根本的设计动机。

#### 4.1.2 核心流程

**朴素（不稳定）实现**：

```text
s = 0
for ai in a: s += exp(ai)        # ← exp(1000) = inf，溢出
return log(s)                    # log(inf) = inf
```

**稳定实现**：

```text
m = max(a)                       # 找最大值做平移基准
s = 0
for ai in a: s += exp(ai - m)    # 指数恒 ≤ 0，exp ∈ (0,1]，不溢出
return m + log(s)                # 把 m 加回来还原
```

`softmax` 用同样的平移（因为分子分母同除 \(e^{m}\) 可约去）：

\[
\sigma(x)_j=\frac{e^{x_j}}{\sum_k e^{x_k}}=\frac{e^{x_j-x_{\max}}}{\sum_k e^{x_k-x_{\max}}}
\]

`log_softmax` 则写成 `tmp - log(sum(exp(tmp)))`，其中 `tmp = x - x_max`，直接复用平移结果、不再回头算 `log(softmax(x))`，避免饱和时的精度损失。

#### 4.1.3 源码精读

**`softmax` 的 max-shift 最简洁**（共 5 行核心逻辑）：

[scipy/special/_logsumexp.py:343-347][softmax] —— 先取 `x_max`，再用 `x - x_max` 做平移后指数，最后归一化：

```python
xp = array_namespace(x)
x = xp.asarray(x)
x_max = xp.max(x, axis=axis, keepdims=True)
exp_x_shifted = xp.exp(x - x_max)
return exp_x_shifted / xp.sum(exp_x_shifted, axis=axis, keepdims=True)
```

`keepdims=True` 是关键：让 `x_max` 形状保持可广播回 `x`，从而 `x - x_max` 按指定 `axis` 正确对齐。

[softmax]: https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_logsumexp.py#L343-L347

**`log_softmax` 同样平移，但多处理「最大值非有限」的边界**：

[scipy/special/_logsumexp.py:405-420][logsoftmax] —— 当 `x_max` 是 `±inf` 或 `nan` 时把它强制改为 0，避免 `inf - inf = nan` 污染；最终 `out = tmp - log(s)`：

```python
x_max = xp.max(x, axis=axis, keepdims=True)
if x_max.ndim > 0:
    x_max = xpx.at(x_max, ~xp.isfinite(x_max)).set(0)
elif not xp.isfinite(x_max):
    x_max = 0
tmp = x - x_max
exp_tmp = xp.exp(tmp)
with np.errstate(divide='ignore'):
    s = xp.sum(exp_tmp, axis=axis, keepdims=True)
    out = xp.log(s)
return tmp - out
```

`xpx.at(...).set(0)` 是 array-api-extra 提供的「原地掩码赋值」语法糖，跨后端等价于 `np.where(~isfinite(x_max), 0, x_max)`。

[logsoftmax]: https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_logsumexp.py#L405-L420

**`logsumexp` 内部 `_logsumexp`：更精确的 Blanchard–Higham 变体**。直接 `m + log(sum(exp(a-m)))` 在某些情形仍会损失精度（参考 `softmax` 文档串引用的论文 [1]，即 Blanchard, Higham & Higham, *Accurately computing the log-sum-exp and softmax functions*, IMA J. Numer. Anal. 2021）。`_logsumexp` 把最大项**单独抽出来**用 `log1p` 处理：

[scipy/special/_logsumexp.py:220-223][shift] —— 平移、指数、加权求和，并对 `m`（最大项的权重贡献）做归一化：

```python
# Shift, exponentiate, scale, and sum
exp = b * xp.exp(a - a_max) if b is not None else xp.exp(a - a_max)
s = xp.sum(exp, axis=axis, keepdims=True, dtype=exp.dtype)
s = xp.where(s == 0, s, s/m)
```

[scipy/special/_logsumexp.py:241][log1p] —— 用 `log1p(s)`（即 \(\log(1+s)\)，在 \(s\approx0\) 时比 `log(1+s)` 精确得多）还原平移：

```python
out = xp.log1p(s) + xp.log(m) + a_max
```

注意这里 `a_max` 已被从 `a` 中扣掉（见 [第 213 行][sep]：`a = xpx.at(a, i_max).set(-xp.inf, ...)`），所以 `s` 是「去掉最大项后的归一化余项」，`m` 是最大项的权重。三项相加还原出完整的 `log(Σ b_i e^{a_i})`。这正是 max-shift 思想的精化版：平移基准不仅是标量 `max`，而是把最大项的整体贡献单独提出来用 `log1p` 高精度计算。

[shift]: https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_logsumexp.py#L220-L223
[log1p]: https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_logsumexp.py#L241
[sep]: https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_logsumexp.py#L213

#### 4.1.4 代码实践

**实践目标**：亲眼看到朴素实现的溢出，并用 `logsumexp` / `softmax` 验证 max-shift 的效果。

**操作步骤**：

```python
import numpy as np
from scipy.special import logsumexp, softmax, log_softmax

a = np.array([1000., 1001.])

# 1) 朴素实现：溢出
naive = np.log(np.sum(np.exp(a)))
print("naive  :", naive)          # 预期 inf（exp(1000) 已溢出）

# 2) logsumexp：稳定
stable = logsumexp(a)
print("stable :", stable)         # 预期 1001.31326168...（≈ 1001 + log(1+e^-1)）

# 3) 解析验证：手算 a_max + log(1 + exp(a_min - a_max))
import math
hand = 1001.0 + math.log1p(math.exp(-1.0))
print("hand   :", hand)

# 4) softmax：两种写法应一致，且都和为 1
print("softmax:", softmax(a), softmax(a).sum())

# 5) log_softmax vs log(softmax(x)) 在饱和输入下的差别
x = np.array([1000., 1.0])
print("log_softmax      :", log_softmax(x))                 # 预期 [0, -999]
with np.errstate(divide='ignore'):
    print("log(softmax(x)) :", np.log(softmax(x)))          # 预期 [0, -inf]，丢精度
```

**需要观察的现象**：

- `naive` 输出 `inf`，而 `stable` 给出有限值。
- `stable` 与 `hand` 几乎完全相等（差异在 1e-12 量级），证明实现就是 `a_max + log1p(...)` 这一恒等式。
- `softmax(a).sum() == 1.0`，验证归一化。
- `log_softmax([1000, 1])` 的第二项是 `-999`（有限），而 `log(softmax(...))` 是 `-inf`——后者因 `softmax` 饱和到 0 取对数而失真，这正是 `log_softmax` 单独存在的理由。

**预期结果**：上述五条全部符合注释中的预期。若你环境里 `naive` 不是 `inf`，请检查输入是否真的为 `1000.`。

> 本实践若无法在本地运行，标注「待本地验证」；但结论可直接在 [test_logsumexp.py 的 `test_array_like`][_test_like] 中看到佐证：`logsumexp([1000, 1000])` 期望值正是 `1000.0 + math.log(2.0)`。

[_test_like]: https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/tests/test_logsumexp.py#L175-L178

#### 4.1.5 小练习与答案

**练习 1**：为什么不写成 `m + log(sum(exp(a))) - m + m`（即先算整体再减/加 `m`），而一定要 `exp(a - m)`？

**参考答案**：因为 `exp(a)` 在指数阶段就已经溢出成 `inf`，后续再 `log` 救不回来——`log(inf)=inf`。必须在**指数之前**就减掉 `m`，让参与 `exp` 的自变量都 ≤ 0，从源头杜绝溢出。

**练习 2**：`softmax` 文档说它是 `logsumexp` 的梯度，请用上面 `a=[1000,1001]` 的例子验证 `softmax(a)` 等于 `exp(a - logsumexp(a))`。

**参考答案**：`exp(a - logsumexp(a)) = exp(a - 1001.313...)`，对 `a=[1000,1001]` 给出 `≈[0.2689, 0.7311]`，与 `softmax(a)` 完全一致，且和为 1。这正是「softmax 是 logsumexp 的梯度」的数值体现。

---

### 4.2 符号分离与权重 b：return_sign 与复数相位

#### 4.2.1 概念说明

带权重 `b` 时，被求和的可以是**带符号甚至复数**的量：

\[
S=\sum_i b_i\,e^{a_i},\qquad \mathrm{result}=\log(S)
\]

当某些 \(b_i<0\) 时，\(S\) 可能为负或零。`\log(负数)` 在实数域无定义（得 `nan`），`\log(0)=-inf`。`logsumexp` 给出两种应对：

1. **默认（`return_sign=False`）**：把 \(S<0\) 的结果置为 `nan`（见 [第 245–246 行][nan]），`S=0` 给 `-inf`。
2. **`return_sign=True`**：把结果拆成 `(log|S|, sign(S))` 一对返回，实数下 `sign ∈ {+1,0,-1}`，复数下 `sign` 是单位复数（辐角）。

这样用户想恢复真正的带符号值时，可以 `sgn * exp(res)`。

#### 4.2.2 核心流程

`_logsumexp` 内部用「分离符号与幅度」的策略，让 `log` 始终作用在正数上：

```text
m = Σ b_i·1{i 是最大项位置}        # 最大项的「带权贡献」，可能为负
s = Σ b_i·exp(a_i - a_max) / m    # 归一化余项
sgn = sign(s+1) * sign(m)         # 合成总符号
若实数且 s < -1：把 s 镜像到 [−1, +1] 区间（s := -s - 2），m := |m|
out = log1p(s) + log(|m|) + a_max # log 只见正数
若 return_sign：返回 (real(out), sgn)
否则若实数且 sgn<0：把 out 置 nan
```

把最大项单独拎出来、用 `sign(s+1)·sign(m)` 合成符号、用 `log1p` 处理余项，三者合起来既保证精度又支持负权重。对复数输入，符号还需吸收 `a_max` 的虚部相位（见 4.3）。

#### 4.2.3 源码精读

[scipy/special/_logsumexp.py:230][sgn] —— 合成符号，`sign(s+1)` 用 `+1` 偏移是为了让 `s=0`（纯零项）也能得到确定的 +1 符号而非 0：

```python
sgn = xp.sign(s + 1) * xp.sign(m)
```

[scipy/special/_logsumexp.py:232-235][realbranch] —— 实数分支：把 \(s<-1\) 的情况镜像进 `log1p` 的定义域，并对 `m` 取绝对值，确保 `log` 拿到正数：

```python
if xp.isdtype(s.dtype, "real floating"):
    # The log functions need positive arguments
    s = xp.where(s < -1, -s - 2, s)
    m = xp.abs(m)
```

[scipy/special/_logsumexp.py:243-246][ret] —— 按 `return_sign` 分流：要符号就取实部返回，否则把负结果标 `nan`：

```python
if return_sign:
    out = xp.real(out)
elif xp.isdtype(out.dtype, 'real floating'):
    out = xpx.at(out)[sgn < 0].set(xp.nan)
```

[sgn]: https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_logsumexp.py#L230
[realbranch]: https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_logsumexp.py#L232-L235
[ret]: https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_logsumexp.py#L243-L246
[nan]: https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_logsumexp.py#L245-L246

还有一条「无穷结果」的回退路径在公共 `logsumexp` 里：当稳定算法给出非有限值时，用一次**直接（不平移）**计算 `out_inf = log(sum(b*exp(a)))` 来替换，目的是把边界行为（如复数 `±inf`）交给后端 `xp.log`/`xp.exp` 的 C99 标准语义处理：

[scipy/special/_logsumexp.py:116-135][infpath] —— 先算一份直接结果 `out_inf`/`sgn_inf`，再用 `xp.where(out_finite, out, out_inf)` 只在非有限处替换：

```python
if xp_size(a) != 0:
    with np.errstate(divide='ignore', invalid='ignore', over='ignore'):
        b_exp_a = xp.exp(a) if b is None else b * xp.exp(a)
        sum_ = xp.sum(b_exp_a, axis=axis, keepdims=True)
        sgn_inf = xp.sign(sum_) if return_sign else None
        sum_ = xp.abs(sum_) if return_sign else sum_
        out_inf = xp.log(sum_)
    ...
    out_finite = xp.isfinite(out)
    out = xp.where(out_finite, out, out_inf)
    sgn = xp.where(out_finite, sgn, sgn_inf) if return_sign else sgn
```

（注释明说：这里宁可重复算一次直接结果，也不强行用当时仅支持逐元素的 `apply_where`，是个工程折中。）

[infpath]: https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_logsumexp.py#L116-L135

#### 4.2.4 代码实践

**实践目标**：用负权重 `b` 和 `return_sign` 验证符号分离逻辑。

**操作步骤**：

```python
import numpy as np
from scipy.special import logsumexp

# 场景 A：负权重，默认模式 → 结果为 nan（因为 sum(b*exp(a)) < 0）
a = np.array([1., 2.])
b = np.array([1., -1.])
print("default :", logsumexp(a, b=b))                 # 预期 nan（S = e - e² < 0）

# 场景 B：return_sign=True → 拆成 (log|S|, sign)
res, sgn = logsumexp(a, b=b, return_sign=True)
print("res,sgn:", res, sgn)                           # 预期 (≈1.541, -1.0)
print("recover :", sgn * np.exp(res))                 # 预期 ≈ e - e² ≈ -3.086（带符号还原）

# 场景 C：完全抵消 → S=0
a2 = np.array([1., 1.])
b2 = np.array([1., -1.])
res2, sgn2 = logsumexp(a2, b=b2, return_sign=True)
print("zero    :", res2, sgn2)                        # 预期 (-inf, 0)，S 恰为 0
```

**需要观察的现象**：

- 场景 A：默认返回 `nan`（对应代码 `sgn<0 → set nan`）。
- 场景 B：`res` 是 `log|S|` 的有限值，`sgn=-1.0`；`sgn*exp(res)` 还原出真正的负数 \(e-e^2\)。
- 场景 C：`S` 恰为 0，`res=-inf`、`sgn=0`。这与 [test_logsumexp.py 的 `test_logsumexp_sign_zero`][_test_sz] 断言一致：`r` 非有限但非 nan 且 `r<0`，`s==0`。

**预期结果**：三条均符合。`return_sign` 的价值在于：默认模式会丢信息（负值变 nan），而 `return_sign` 让你完整拿到「幅度 + 符号」。

[_test_sz]: https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/tests/test_logsumexp.py#L117-L125

#### 4.2.5 小练习与答案

**练习 1**：为什么 `sign(s + 1)` 要加那个 `+1`，而不是直接 `sign(s)`？

**参考答案**：当某个归一化余项 `s=0`（即除了最大项外其余项完全抵消）时，`sign(0)=0`，会让最终符号被错误地拉到 0。用 `sign(s+1)` 偏移后，`s=0` 仍得到 `+1`，符号由 `sign(m)` 单独决定，更稳健。

**练习 2**：默认模式下 `logsumexp([1,1], b=[1,-1])` 为什么是 `nan` 而不是 `-inf`？

**参考答案**：因为被求和的 \(S=e-e=0\) 在 `return_sign=False` 路径里，符号 `sgn` 为 0（< 0 不成立，但代码用 `sgn<0` 判 nan）……更准确地说：当 `s` 的合成符号为负（`sgn<0`）时置 `nan`；而这里真正 `S=0` 走的是「无穷回退路径」给出 `-inf`。建议你打印确认：`logsumexp([1,1], b=[1,-1])` 实际返回 `-inf`（S=0），而 `logsumexp([1,2], b=[1,-1])` 返回 `nan`（S<0）。请用本地运行核对「S<0 → nan」「S=0 → -inf」的边界，这正是 4.2.3 两条路径的分工。

---

### 4.3 复数输入与相位包裹 _wrap_radians

#### 4.3.1 概念说明

当 `a` 是复数数组时，\(e^{a_i}=e^{\Re a_i}\cdot e^{i\,\Im a_i}\)，求和后 \(S\) 是个复数。此时 `logsumexp` 返回 \(\log S=\log|S|+i\arg(S)\)，其中 \(\arg(S)\)（辐角）就是「符号」。由于复对数多值，约定把辐角（结果虚部）归一到 \((-\pi,\pi]\)。

注意：复数下的「符号」不再是 ±1，而是模为 1 的复数（即 \(e^{i\arg(S)}\)）。这正是文档串里 `return_sign` 对复数输入「返回 a complex phase」的含义。

#### 4.3.2 核心流程

```text
若 a 是复数：
    a_max 也会带虚部（取「实部最大」的那个元素，见 _elements_and_indices_with_max_real）
    sgn 吸收 a_max 的虚部相位：sgn *= exp(i·imag(a_max))
最终结果的虚部（辐角）经 _wrap_radians 折回 (-π, π]
```

`_wrap_radians` 的目标是：把任意弧度映射到半开半闭区间 \((-\pi,\pi]\)，同时尽量保留原本就在区间内的值的精度（避免无谓的取模运算引入误差）。

#### 4.3.3 源码精读

[scipy/special/_logsumexp.py:236-238][cpxbranch] —— 复数分支把 `a_max` 的虚部相位乘进符号（因为最大项被单独提出，它的复数相位要补到符号里）：

```python
else:
    # `a_max` can have a sign component for complex input
    sgn = sgn * xp.exp(xp.imag(a_max) * 1.0j)
```

[cpxbranch]: https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_logsumexp.py#L236-L238

[scipy/special/_logsumexp.py:142-150][cpxwrap] —— 在公共 `logsumexp` 末尾，若结果是复数，把虚部（辐角）经 `_wrap_radians` 折回主值区间；`return_sign` 时对 `sgn` 做同样处理：

```python
if xp.isdtype(out.dtype, 'complex floating'):
    if return_sign:
        real = xp.real(sgn)
        imag = xp_float_to_complex(_wrap_radians(xp.imag(sgn), xp=xp), xp=xp)
        sgn = real + imag*1j
    else:
        real = xp.real(out)
        imag = xp_float_to_complex(_wrap_radians(xp.imag(out), xp=xp), xp=xp)
        out = real + imag*1j
```

`xp_float_to_complex` 的作用是把实浮点（`float32`/`float64`）提升为对应的复数类型（`complex64`/`complex128`），以便与实部拼回复数（见 [xp_float_to_complex 实现][f2c]）。

[cpxwrap]: https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_logsumexp.py#L142-L150
[f2c]: https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/_lib/_array_api.py#L608-L619

[scipy/special/_logsumexp.py:161-166][wrap] —— `_wrap_radians` 本体：先用取模公式折回，再用 `where` 保留原本就在 \((-\pi,\pi]\) 内的值以「preserve relative precision」（取模会损失有效数字，故仅对越界值做）：

```python
def _wrap_radians(x, *, xp):
    # Wrap radians to (-pi, pi] interval
    wrapped = -((-x + xp.pi) % (2 * xp.pi) - xp.pi)
    # preserve relative precision
    no_wrap = xp.abs(x) < xp.pi
    return xp.where(no_wrap, x, wrapped)
```

取模公式 `wrapped = -((-x + π) % (2π) - π)` 把任意角映到 \((-\pi,\pi]\)：
- 例：\(x=3\pi/2\) → \(-x+\pi=-\pi/2\)，对 \(2\pi\) 取模得 \(3\pi/2\)，减 \(\pi\) 得 \(\pi/2\)，取负得 \(-\pi/2\)。即 \(3\pi/2\equiv-\pi/2\)，正确落入区间。

[wrap]: https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_logsumexp.py#L161-L166

#### 4.3.4 代码实践

**实践目标**：用复数输入验证 `return_sign` 返回单位复数，并理解相位包裹。

**操作步骤**：

```python
import numpy as np
from scipy.special import logsumexp

a = np.array([1 + 1j, 2 - 1j, -2 + 3j])
res, sgn = logsumexp(a, return_sign=True)

expected_sumexp = np.sum(np.exp(a))
print("res :", res)
print("sgn :", sgn)                          # 单位复数（|sgn|≈1）
print("|sgn|:", np.abs(sgn))                 # 预期 ≈ 1.0

# 关键性质：sgn * exp(res) 应还原出 Σ exp(a)
print("recover :", sgn * np.exp(res))
print("direct  :", expected_sumexp)

# 验证辐角落在 (-π, π]
print("imag(res) in (-pi,pi]?", -np.pi < res.imag <= np.pi)
```

**需要观察的现象**：

- `sgn` 是模长为 1 的复数（`|sgn|≈1`），即「complex phase」，而非 ±1。
- `sgn * exp(res)` 与直接 `np.sum(np.exp(a))` 几乎相等——这就是 `return_sign` 在复数下的意义：把幅度（`res`）与相位（`sgn`）分开存放，乘回去即可还原。
- `res.imag` 落在 \((-\pi,\pi]\)，证明 `_wrap_radians` 生效。

**预期结果**：上述均成立。该用例直接对应 [test_logsumexp.py 的 `test_logsumexp_complex_sign`][_test_cpx]：它断言 `sgn == Σexp(a)/|Σexp(a)|` 且 `sgn*exp(res) == Σexp(a)`。

[_test_cpx]: https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/tests/test_logsumexp.py#L137-L147

#### 4.3.5 小练习与答案

**练习 1**：`_wrap_radians` 为什么不直接对每个值取模，而要用 `where(no_wrap, x, wrapped)` 保留原值？

**参考答案**：取模运算 `%` 在浮点下会引入舍入，对原本就在 \((-\pi,\pi]\) 内的值（绝大多数情况）会造成不必要的相对精度损失。先用 `abs(x) < π` 判断，仅对越界值做折叠，其余原样返回，从而「preserve relative precision」。

**练习 2**：复数输入下 `sgn` 的模长为什么是 1？

**参考答案**：因为 `sgn` 代表的是求和结果 \(S\) 的辐角 \(e^{i\arg(S)}\)，这是单位圆上的复数，模长恒为 1。真正的幅度信息存在 `res = log|S|` 里，二者相乘 `sgn*exp(res) = e^{i\arg(S)}\cdot|S| = S` 还原复数 \(S\)。

---

### 4.4 Array API 兼容：array_namespace 与 xp_promote

#### 4.4.1 概念说明

`logsumexp`/`softmax`/`log_softmax` 是纯 Python，但它们要能跑在 PyTorch 张量、JAX 数组、CuPy 数组上。秘诀是不直接用 `np.xxx`，而是先拿到输入数组所属后端的「命名空间」`xp`，再用 `xp.sum`/`xp.exp`/`xp.max`。这套机制由两个支柱撑起：

1. **`array_namespace(a, b)`**：从输入数组推断出统一命名空间。
2. **`xp_promote(a, b, ...)`**：把多个输入提升到公共 dtype 并广播到同一形状，让后续运算类型一致。

此外，`@xp_capabilities()` 装饰器负责把「该函数在各后端上的支持程度」自动写进文档串、并在测试里生成 SKIP/XFAIL 标记（详见 U10）。

#### 4.4.2 核心流程

```text
1. xp = array_namespace(a, b)              # 推断后端（numpy/torch/jax/...）
2. a, b = xp_promote(a, b, broadcast=True,
                     force_floating=True,  # 强制浮点（log/exp 不能用整数）
                     xp=xp)
3. 用 xp.atleast_nd / xp.max / xp.exp / xp.sum ... 完成计算
4. 用 xp.isdtype / xp.where / xp.squeeze 收尾
5. 0-D 数组转标量（out[()]）以匹配 NumPy 约定
```

当全局开关 `SCIPY_ARRAY_API` 关闭时，`array_namespace` 直接返回 NumPy 命名空间（跳过所有合规检查），行为退化为「纯 NumPy」。

#### 4.4.3 源码精读

[scipy/special/_logsumexp.py:110-114][ns] —— `logsumexp` 开头四行就是跨后端的标准起手式：取命名空间、强制浮点提升并广播、至少 1 维、归一化 `axis`：

```python
xp = array_namespace(a, b)
a, b = xp_promote(a, b, broadcast=True, force_floating=True, xp=xp)
a = xpx.atleast_nd(a, ndim=1, xp=xp)
b = xpx.atleast_nd(b, ndim=1, xp=xp) if b is not None else b
axis = tuple(range(a.ndim)) if axis is None else axis
```

`force_floating=True` 很关键：`log`/`exp` 对整数无定义，这里把整数输入提升为浮点（如 `int64 → float64`），避免后端报错。

[ns]: https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_logsumexp.py#L110-L114

[scipy/_lib/_array_api_override.py:111-113][sw] —— `array_namespace` 的全局开关：`SCIPY_ARRAY_API` 未设时直接返回 NumPy 命名空间，等于「默认只跑 NumPy」：

```python
if not SCIPY_ARRAY_API:
    # here we could wrap the namespace if needed
    return np_compat
```

[sw]: https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/_lib/_array_api_override.py#L111-L113

[scipy/_lib/_array_api.py:540-605][promote] —— `xp_promote` 干三件事：(a) 把可迭代输入转成 `xp` 数组；(b) 用 `xp_result_type(force_floating=True)` 算出公共 dtype；(c) 按需广播到统一形状。返回提升后的数组元组：

```python
dtype = xp_result_type(*args, force_floating=force_floating, xp=xp)
args = [(_asarray(arg, dtype=dtype, subok=True, xp=xp) if arg is not None else arg)
        for arg in args]
...
if arg.shape != shape:
    arg = xp.broadcast_to(arg, shape, **kwargs)
```

注意它特意用 SciPy 自己的 `xp_result_type` 而非 `xp.result_type`，因为各后端的类型提升规则不统一，SciPy 要一套自洽规则。

[promote]: https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/_lib/_array_api.py#L540-L605

[scipy/special/_logsumexp.py:15-16][deco] & [scipy/_lib/_array_api.py:864-879][capdoc] —— `@xp_capabilities()` 装饰器有两个效果：(1) 把函数登记进能力表，供测试生成 SKIP/XFAIL 标记；(2) 自动在文档串追加「各后端支持矩阵」表格。注释明说它「同时驱动文档矩阵与测试标记」：

```python
def xp_capabilities(*, capabilities_table=None, skip_backends=(), xfail_backends=(),
                    cpu_only=False, np_only=False, ...):
    """Decorator for a function that states its support among various
    Array API compatible backends.
    This decorator has two effects:
    1. It allows tagging tests ... to automatically generate SKIP/XFAIL markers ...
    2. It automatically adds a note to the function's docstring ...
```

[deco]: https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_logsumexp.py#L15-L16
[capdoc]: https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/_lib/_array_api.py#L864-L879

#### 4.4.4 代码实践

**实践目标**：验证同一个 `logsumexp` 既能吃 NumPy 数组，也能在 `SCIPY_ARRAY_API` 开启后吃其它后端；并理解 `force_floating` 的作用。

**操作步骤**（NumPy 部分，本地即可跑）：

```python
import numpy as np
from scipy.special import logsumexp

# force_floating 的效果：整数输入被提升为浮点，不会报错
print(logsumexp(np.array([0, 1, 2])))      # 整数数组也能算，返回 float

# array_namespace 默认返回 numpy 命名空间（SCIPY_ARRAY_API 未设）
from scipy._lib._array_api import array_namespace
print(array_namespace(np.array([1.0])))    # 预期 <module 'numpy'>
```

**多后端部分**（需安装 torch/jax，可选；若本地无则标注「待本地验证」）：

```bash
# 在 shell 中开启 Array API 开关后再跑 torch
SCIPY_ARRAY_API=1 python -c "
import numpy as np, torch
from scipy.special import logsumexp
t = torch.arange(10, dtype=torch.float64)
print(logsumexp(t))                       # 预期：返回 torch 张量，值≈9.4586
from scipy._lib._array_api import array_namespace
print(array_namespace(t))                 # 预期：torch 命名空间
"
```

**需要观察的现象**：

- 整数数组 `np.array([0,1,2])` 不报错，返回浮点结果——这是 `force_floating=True` 的功劳。
- 默认 `array_namespace(np.array(...))` 返回 numpy 命名空间。
- 开启 `SCIPY_ARRAY_API=1` 后，传入 `torch` 张量，`logsumexp` 返回 **torch 张量**（而非先转 numpy），且 `array_namespace(t)` 返回 torch 命名空间——证明计算确实发生在 torch 后端。

**预期结果**：NumPy 部分必现；多后端部分取决于是否安装了对应库。这一「同一份代码、多个后端」的能力，正是 Array API 的核心价值，也是 U10 的主题。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `xp_promote` 要加 `force_floating=True`？不加会怎样？

**参考答案**：`logsumexp` 内部要算 `exp`/`log`，它们对整数类型无定义（多数后端会报错或行为未定义）。`force_floating=True` 把整数提升为浮点（如 `int64 → float64`），保证后续指数/对数运算合法。

**练习 2**：`SCIPY_ARRAY_API` 关闭时，`logsumexp(torch_tensor)` 会发生什么？

**参考答案**：`array_namespace` 在开关关闭时直接返回 NumPy 命名空间（[第 111–113 行][sw]），后续 `xp_promote` 等会尝试把 torch 张量当 NumPy 处理，大概率报错或被规约为 NumPy 行为。也就是说，**多后端支持只有显式开启 `SCIPY_ARRAY_API` 才生效**，默认行为是纯 NumPy。这既保证向后兼容，又让有需要的用户能 opt-in 多后端。

---

## 5. 综合实践

把本讲四块知识串起来，完成下面这个「手写一个最小 logsumexp 并与官方对照」的任务：

**任务**：

1. 写一个 `my_logsumexp_basic(a)`，只用 `np.max`/`np.exp`/`np.sum`/`np.log` 实现最朴素的 max-shift（不平移最大项、不用 `log1p`）。
2. 用 `a = np.array([1000., 1001.])` 同时跑你的版本和 `scipy.special.logsumexp(a)`，确认两者都给出有限值且数值接近。
3. 再用 `a = np.array([1000., 1000.])` 验证结果等于 `1000.0 + np.log(2.0)`（这是 [`test_array_like`][_test_like] 的断言）。
4. 接着用复数 `a = np.array([1+1j, 2-1j])` 调用官方 `logsumexp(a, return_sign=True)`，验证 `sgn * np.exp(res)` 还原 `np.sum(np.exp(a))`，并检查 `res.imag` 落在 \((-\pi,\pi]\)。
5. 最后，回答一个理解性问题：你的朴素版本在 `a=[1000,1001]` 上能与官方几乎一致，那官方版本里「把最大项单独提出来用 `log1p` 计算」的复杂度（[第 213、241 行][log1p]）到底换来了什么？提示：构造一个「除最大项外其余项都很小」的输入，比较两者的相对误差。

**参考框架代码**：

```python
import numpy as np
from scipy.special import logsumexp

def my_logsumexp_basic(a):
    m = np.max(a)
    return m + np.log(np.sum(np.exp(a - m)))

a1 = np.array([1000., 1001.])
print(my_logsumexp_basic(a1), logsumexp(a1))

a2 = np.array([1000., 1000.])
print(my_logsumexp_basic(a2), 1000.0 + np.log(2.0))

ac = np.array([1+1j, 2-1j])
res, sgn = logsumexp(ac, return_sign=True)
print(sgn * np.exp(res), np.sum(np.exp(ac)))
print(-np.pi < res.imag <= np.pi)
```

**预期**：第 1、2 步两边吻合；第 4 步 `sgn*exp(res)` 还原出 `Σexp(ac)`；第 5 步你会发现在「一个大项 + 若干极小项」的极端情形下，朴素版因 `log1p` 缺失而相对误差更大——这就是 Blanchard–Higham 精化的价值。

## 6. 本讲小结

- `logsumexp`/`softmax`/`log_softmax` 都是**纯 Python**实现，且**不是 ufunc**——因为它们跨元素求和，破坏了 ufunc「逐元素」的前提（这与 U4-l1「整数位数无界」、U4-l2「输出形状由标量参数决定」共同构成「为何不用 ufunc」的三类理由）。
- **max-shift**（先减最大值再指数）是它们共同的数值稳定基石，把溢出风险消灭在指数之前；`_logsumexp` 进一步用「单独提出最大项 + `log1p`」的 Blanchard–Higham 精化换取更高精度。
- 负权重 `b` 与 `return_sign` 走「符号/幅度分离」：默认把负结果标 `nan`，`return_sign=True` 则拆成 `(log|S|, sign(S))`，复数下 `sign` 是单位复数（辐角）。
- 复数输入的辐角经 `_wrap_radians` 折回 \((-\pi,\pi]\)，且用 `where(no_wrap,...)` 保留区间内原值以减少精度损失。
- 跨后端能力由 `array_namespace` + `xp_promote(force_floating=True)` + `@xp_capabilities()` 三件套提供；默认 `SCIPY_ARRAY_API` 关闭即纯 NumPy，开启后才分发到 PyTorch/JAX/CuPy/Dask。

## 7. 下一步学习建议

- **横向**：阅读 U4-l4「薄包装与装饰器」，对比 `lambertw`/`spherical_bessel` 这类「在底层 ufunc 上做轻量预处理」的包装，与本讲「纯 Python 完整算法」的差异。
- **纵向（Array API 深挖）**：直接进入 U10-l1「`_FuncInfo` 与后端分发」与 U10-l2「`@xp_capabilities` 能力标注」，弄清 `_logsumexp.py` 里这个装饰器背后的整套多后端分发与测试标记体系。
- **数值验证方法**：本讲多次引用 `test_logsumexp.py` 作为「标准答案」，可顺道预习 U9-l1「`_testutils.py`：FuncData 与数据驱动测试」，看 special 子模块如何系统性地用参考数据校验数值函数。
- **源码延伸**：想看 max-shift 在统计/机器学习里的更多应用，可阅读 NumPy 的 `np.logaddexp`（两参数版 logsumexp，文档串 [`Notes`][_doc] 第 67–70 行专门提到它与本函数的关系）。
