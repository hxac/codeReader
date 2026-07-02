# 收敛判断与终止 check_termination

## 1. 本讲目标

学完本讲后，你应当能够：

- 说清 `derivative` 在每一轮迭代结束时，如何判断「某个元素是否该停下来了」。
- 写出三类终止条件的判别式：误差收敛 `error < atol + rtol*|df|`、非有限值检测、误差回升启发式 `error > 10*error_last`。
- 解释这三个条件在 `check_termination` 内部的**优先级顺序**，以及为什么用 `| stop` / `& ~stop` 来保护已经判停的元素。
- 区分哪些状态码（`0` / `-1` / `-3`）由 `check_termination` 设置，哪些（`-2` / `-4`）由通用框架 `eim._loop` 兜底设置。
- 能够构造输入，让一次 `derivative` 调用同时产生多种 `status`。

本讲是 `derivative` 白盒剖析的「收尾」一环：前置的 [u2-l3](u2-l3-pre-func-eval-stencil.md) 讲了每轮怎么生成求值点，[u2-l4](u2-l4-post-func-eval-estimate.md) 讲了怎么算出 `df` 和 `error`，本讲回答最后一个问题——**有了 `df` 和 `error` 之后，迭代何时停止**。

## 2. 前置知识

在进入源码前，先用通俗语言建立两个直觉。

### 2.1 为什么需要「主动终止」

`derivative` 是一个**自适应**算法：从大步长起步，每轮把步长缩小 `step_factor` 倍，重新估计导数。理论上步长越小，有限差分的截断误差（truncation error）越小。但浮点数有精度上限：当步长 \(h\) 小到一定程度，\(f(x+h)\) 与 \(f(x-h)\) 在浮点意义下几乎相等，二者相减会丢失大量有效位，这叫**消去误差**（subtractive cancellation error）。

于是误差随迭代呈现「先降后升」的 U 形：

\[

\text{总误差}(h) \;\approx\; \underbrace{C_1 \, h^{\text{order}}}_{\text{截断误差}} \;+\; \underbrace{C_2 \, \varepsilon_{\text{mach}} / h}_{\text{消去误差}}

\]

- 第一项随 \(h\) 减小而减小；
- 第二项随 \(h\) 减小而**增大**。

这意味着**不能一直迭代下去**：越过最优点后，越迭代越差。`check_termination` 的核心职责之一，就是在误差开始回升时及时刹车。

### 2.2 逐元素停止与「压缩」

`derivative` 是**逐元素**（elementwise）的：输入数组里的不同元素，可能在不同的迭代轮次收敛。框架 `eim._loop` 维护一个 `active` 索引数组，记录「还没判停的元素」。一旦某元素在 `check_termination` 里被判停（`stop=True`），它就会被移出 `active`，后续轮次不再对它求值（这就是 [u2-l6](u2-l6-elementwise-loop-framework.md) 会讲的 work 压缩）。

关键推论：`check_termination` 返回的 `stop` 是一个**布尔数组**，长度等于当前活跃元素数；框架据此更新 `active` 与结果对象。

### 2.3 来自前序讲义的事实

- [u2-l4](u2-l4-post-func-eval-estimate.md) 已说明：`work.error = |df - df_last|`，且首轮因 `df_last` 初值为 `NaN`，所以**首轮 `error` 为 `NaN`**。
- [u1-l3](u1-l3-result-object-and-status.md) 已给出全部状态码：`0` 收敛、`-1` 误差回升、`-2` 触达 `maxiter`、`-3` 非有限值、`-4` callback 叫停、`1` 进行中。本讲将精确说明它们各自在**哪一行代码**被写入。

## 3. 本讲源码地图

| 文件 | 关键位置 | 作用 |
| --- | --- | --- |
| `scipy/differentiate/_differentiate.py` | `check_termination`（L562–L587） | 本讲主角：三类终止条件的判别与状态码写入 |
| `scipy/differentiate/_differentiate.py` | `_EERRORINCREASE = -1`（L9） | 误差回升专用状态码常量 |
| `scipy/differentiate/_differentiate.py` | `work` 对象初始化（L434–L441） | `error` / `error_last` / `df_last` 的初值（均为 `NaN`） |
| `scipy/differentiate/_differentiate.py` | `atol` / `rtol` 默认值（L400–L402） | 收敛阈值在 dtype 已知后才计算 |
| `scipy/differentiate/_differentiate.py` | `post_func_eval` 末尾（L546–L560） | `df_last` / `error_last` / `error` 的更新顺序 |
| `scipy/_lib/_elementwise_iterative_method.py` | 状态码常量（L21–L27） | `_ECONVERGED=0`、`_EVALUEERR=-3`、`_ECONVERR=-2`、`_ECALLBACK=-4` 等 |
| `scipy/_lib/_elementwise_iterative_method.py` | `_loop` 末尾兜底（L278） | 给「从未判停」的活跃元素写入 `-2` 或 `-4` |
| `scipy/differentiate/tests/test_differentiate.py` | `test_flags`（L94–L117） | 一次调用同时产出四种 status 的经典测试 |

## 4. 核心概念与源码讲解

`check_termination` 全文只有二十余行，却承担了三件事。我们先用一张表总览它设置的三个状态码，再逐个拆解。

| 终止条件 | 判别式 | 写入状态码 | 触发时机 |
| --- | --- | --- | --- |
| 误差收敛 | `error < atol + rtol*|df|` | `0`（`_ECONVERGED`） | 每轮都判（含首轮，但首轮 error 为 NaN 不会中） |
| 非有限值 | `~((isfinite(x) & isfinite(df)) \| stop)` | `-3`（`_EVALUEERR`） | 仅当 `nit > 0` 时才判 |
| 误差回升 | `(error > error_last*10) & ~stop` | `-1`（`_EERRORINCREASE`） | 每轮都判（但需 `error_last` 为实数才可能中） |

> 注意：`-2`（触达 maxiter）与 `-4`（callback 叫停）**不在** `check_termination` 里设置，而是由 `eim._loop` 在循环结束时兜底（见 4.4）。

### 4.1 收敛判据：error < atol + rtol*|df|

#### 4.1.1 概念说明

这是最理想的一类终止：**估计值已经足够稳定，不需要再迭代了**。判据采用 SciPy 通用的「绝对容差 + 相对容差」混合形式：

\[

\text{error}_k \;<\; \text{atol} \;+\; \text{rtol} \cdot |df_k|

\]

其中 \(\text{error}_k = |df_k - df_{k-1}|\) 是相邻两轮估计之差（[u2-l4](u2-l4-post-func-eval-estimate.md) 已说明它是一个偏保守的上界估计）。

为什么是这种形式？

- **相对项** `rtol*|df|`：导数值越大，允许的绝对抖动也越大，这样收敛难度与导数量级无关。
- **绝对项** `atol`：当真导数**恰好为 0**（鞍点）时，`rtol*|df|` 趋于 0，光靠相对容差永远收不住，此时 `atol` 提供一个「地板」。这正是 [u1-l3](u1-l3-result-object-and-status.md) 提到的「真导数为 0 时需手动设 `atol`」的数学根源。

#### 4.1.2 核心流程

每一轮 `post_func_eval` 算出新的 `error` 后，框架调用 `check_termination`，其中收敛判定的伪代码为：

```
threshold = atol + rtol * abs(df)
i = (error < threshold)          # 布尔掩码：哪些元素收敛了
status[i] = 0                    # _ECONVERGED
stop[i] = True                   # 标记停机
```

阈值默认值（在主流程里、dtype 已知后才计算）：

- `atol` 默认为该 dtype 的 `smallest_normal`（最小的正规格化浮点数，约 `2.2e-308` for float64）；
- `rtol` 默认为 `sqrt(eps)`（`eps` 是机器精度，float64 下 `sqrt(2.2e-16) ≈ 1.5e-8`）。

这意味着默认门槛**非常严**——只有当两轮估计之差小到接近机器精度量级才算收敛。这也是为什么真导数为 0 的点容易「假失败」。

#### 4.1.3 源码精读

收敛判定在 `check_termination` 开头，先初始化全 `False` 的 `stop` 掩码，再做比较：

收敛判定代码（[_differentiate.py:564-568](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L564-L568)）——初始化 `stop` 并对满足 `error < atol + rtol*|df|` 的元素写入状态 `0`：

```python
stop = xp.astype(xp.zeros_like(work.df), xp.bool)

i = work.error < work.atol + work.rtol*abs(work.df)
work.status = xpx.at(work.status)[i].set(eim._ECONVERGED)
stop = xpx.at(stop)[i].set(True)
```

默认容差的计算位置（[_differentiate.py:400-402](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L400-L402)）——在 `eim._initialize` 拿到 `dtype` 之后，才把 `None` 容差替换为与 dtype 匹配的默认值：

```python
finfo = xp.finfo(dtype)
atol = finfo.smallest_normal if atol is None else atol
rtol = finfo.eps**0.5 if rtol is None else rtol  # keep same as `hessian`
```

> **细节**：`check_termination` 在循环开始前会被框架**先调用一次**（`nit==0`）。此时 `work.error` 仍是初值 `NaN`（见 [u2-l4](u2-l4-post-func-eval-estimate.md)），而 `NaN < 任何数` 恒为 `False`，所以这「首轮前置检查」不会误判收敛。这是算法「至少跑两轮才有资格判收敛」的另一层保障。

#### 4.1.4 代码实践

**实践目标**：观察 `atol` / `rtol` 如何影响收敛所需的迭代轮数 `nit`。

**操作步骤**（示例代码，待本地验证）：

```python
# 示例代码
import numpy as np
from scipy.special import ndtr
from scipy.differentiate import derivative

x = np.asarray(1., dtype=np.float64)
f = ndtr

for atol in [1e-3, 1e-6, 1e-12]:
    res = derivative(f, x, tolerances=dict(atol=atol), order=4)
    print(f"atol={atol:>1.0e}  nit={res.nit}  df={res.df:.10f}")
```

**需要观察的现象**：`atol` 越小，`nit` 越大（算法被迫多跑几轮以满足更严的门槛）；`df` 始终逼近真值 `stats.norm.pdf(1)`。

**预期结果**：三行的 `nit` 单调递增，`df` 越来越准。这印证了「收敛判据直接控制迭代成本」。

#### 4.1.5 小练习与答案

**练习 1**：若把 `rtol` 设为 0、`atol` 设为 0，收敛判定还能成立吗？
**答案**：门槛变为 `0 + 0*|df| = 0`，需要 `error < 0`，而 `error = |...| ≥ 0`，故**永不收敛**（除非 `error` 恰为 0，如线性函数）。这正是测试中「强制跑满 maxiter」的常用技巧（见 [test_differentiate.py:156](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L156)）。

**练习 2**：为什么 `rtol` 默认取 `sqrt(eps)` 而不是 `eps`？
**答案**：因为 `error` 本身是两个含噪估计之差，其精度大约只有 `sqrt(eps)` 量级。要求比 `sqrt(eps)` 更严没有意义——那是在追求「假精度」，反而容易触达 maxiter 或触发误差回升。

### 4.2 非有限值终止：检测 NaN / inf

#### 4.2.1 概念说明

如果某个元素的估计 `df` 或横坐标 `x` 变成 `NaN` / `inf`，继续迭代毫无意义——结果已经污染了。此时应立即停机，并把 `df` 显式置为 `NaN`，让用户一眼看出该元素失败。对应状态码 `-3`（`_EVALUEERR`）。

什么情况会产生非有限值？

- 用户函数 `f` 本身返回 `NaN`（如对数函数在定义域外）；
- `initial_step=0` 导致除以零；
- `step_direction=NaN` 传入非法方向；
- 数值溢出（`inf`）。

#### 4.2.2 核心流程

```
if nit > 0:                                  # 关键守卫
    bad = ~((isfinite(x) & isfinite(df)) | stop)
    df[bad]  = NaN
    status[bad] = -3                          # _EVALUEERR
    stop[bad]  = True
```

两个精妙之处：

1. **`if nit > 0` 守卫**：在循环前的预检（`nit==0`）时，`work.df` 还是初始化时填的 `NaN`（见下文 4.2.3），**所有元素**的 `df` 都是 `NaN`。若没有这个守卫，预检会把全部元素误判为「非有限值」而整体失败。守卫确保这个检查只在「真正算过至少一轮」之后才生效。
2. **`| stop` 保护**：`stop` 此时已被收敛判定更新过。表达式 `(isfinite(x) & isfinite(df)) | stop` 意为「要么数值正常，要么已经判停」。取反后 `bad` 只会选中「数值异常 **且** 尚未判停」的元素。这保证了**已收敛的元素不会被后续检查重新归类**，维护了检查之间的优先级。

#### 4.2.3 源码精读

非有限值检测（[_differentiate.py:570-574](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L570-L574)）——仅在 `nit > 0` 时，把 `x` 或 `df` 非有限且未判停的元素标记为 `-3` 并把 `df` 置 `NaN`：

```python
if work.nit > 0:
    i = ~((xp.isfinite(work.x) & xp.isfinite(work.df)) | stop)
    work.df = xpx.at(work.df)[i].set(xp.nan)
    work.status = xpx.at(work.status)[i].set(eim._EVALUEERR)
    stop = xpx.at(stop)[i].set(True)
```

要理解 `nit > 0` 守卫的必要性，需要看 `df` 的初值。`work` 对象构造时（[_differentiate.py:405](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L405) 与 [L434-L441](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L434-L441)）——`df` 被初始化为全 `NaN`，`error` / `error_last` / `df_last` 也都是 `NaN`：

```python
df = xp.full_like(f, xp.nan)
...
work = _RichResult(x=x, df=df, fs=f[:, xp.newaxis], error=xp.nan, h=h0,
                   df_last=xp.nan, error_last=xp.nan, ...)
```

正因为初始 `df` 全是 `NaN`，所以预检（`nit==0`）必须跳过非有限值检查。

#### 4.2.4 代码实践

**实践目标**：复现 `test_special_cases` 中产生 `-3` 的两种典型输入。

**操作步骤**（示例代码，待本地验证）：

```python
# 示例代码
import numpy as np
from scipy.differentiate import derivative

# 情形 A：非法 step_direction
res = derivative(np.exp, np.asarray(1), step_direction=np.nan)
print("step_direction=nan ->", res.status, res.df)

# 情形 B：initial_step=0
res = derivative(np.exp, np.asarray(1), initial_step=0)
print("initial_step=0    ->", res.status, res.df)
```

**需要观察的现象**：两种情形下 `res.status` 都应为 `-3`，`res.df` 都应为 `NaN`。

**预期结果**：`status=-3`、`df=nan`。这与 [test_differentiate.py:388-394](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L388-L394) 的断言一致。

#### 4.2.5 小练习与答案

**练习 1**：如果把 `if work.nit > 0:` 这行删掉，会发生什么？
**答案**：循环前的预检会对**所有元素**触发非有限值检测（因为初始 `df` 全是 `NaN`），导致整个结果立即失败、`nit=0`，正常函数也无法求导。

**练习 2**：为什么检测条件里要同时检查 `isfinite(x)` 而不只是 `isfinite(df)`？
**答案**：`x` 非有限（如用户传入了 `NaN` 横坐标，或 `step_direction=nan` 污染了求值点）同样意味着结果无意义。只查 `df` 会漏掉「`x` 异常但凑巧 `df` 还没变 `NaN`」的情况；同时查两者更稳健。

### 4.3 误差回升启发式：error > 10 * error_last

#### 4.3.1 概念说明

回到 §2.1 的 U 形误差曲线：当步长缩小到越过最优点后，消去误差开始主导，`error` 不降反升。`check_termination` 用一个简单启发式捕捉这一拐点——**一旦本轮误差比上一轮大 10 倍以上，就认定已经过了最优点，立即停机**，采用上一轮（更准）的估计。

对应状态码 `-1`，由模块顶部的常量 `_EERRORINCREASE`（[_differentiate.py:9](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L9)）定义：

```python
_EERRORINCREASE = -1  # used in derivative
```

> **一个易混淆点**：在通用框架里 `_ESIGNERR` 也等于 `-1`（见 [_elementwise_iterative_method.py:21](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_elementwise_iterative_method.py#L21)）。但 `derivative` 复用了 `-1` 这个数值，语义重定义为「误差回升」。`derivative` 的 docstring 明确把 `-1` 解释为误差回升，所以**以本子包的语义为准**。

#### 4.3.2 核心流程

判别式为：

\[

\text{error}_k \;>\; 10 \cdot \text{error}_{k-1}

\]

```
i = (error > error_last * 10) & ~stop
status[i] = -1            # _EERRORINCREASE
stop[i]   = True
```

**为什么是 10 倍？** 在正常收敛阶段，每轮误差约缩减为 \(1/\text{fac}^{\text{order}}\)（默认 `fac=2, order=8` 即 `1/256`）。10 倍的回升远超正常波动，是一个明确的「出问题了」信号；同时又不像「只要不降就停」那么敏感，能容忍数值上的小抖动。源码注释也坦言这只是个「simple and effective」的启发式，并非理论最优。

**`& ~stop` 的作用**：与 4.2 的 `| stop` 异曲同工——已收敛或已判非有限值的元素不再被重新归类为「误差回升」，保证优先级：收敛 > 非有限值 > 误差回升。

**何时才可能触发？** 这是最隐蔽的一点。`error_last` 在前两轮都是 `NaN`：

| 轮次（`check_termination` 时的 `nit`） | `error` | `error_last` | 误差回升能否触发 |
| --- | --- | --- | --- |
| 预检 `nit=0` | `NaN` | `NaN` | 否（`NaN > NaN` 为 `False`） |
| `nit=1` | `NaN`（首轮 `df_last` 为 `NaN`） | `NaN` | 否 |
| `nit=2` | 实数 | `NaN` | 否（`实数 > NaN` 为 `False`） |
| `nit=3` 及以后 | 实数 | 实数 | **可能** |

所以要到**第 3 轮**（`nit==3`）`error_last` 才首次成为实数，误差回升启发式才真正生效。`error_last` 的更新发生在 `post_func_eval` 末尾——先保存旧 `error` 到 `error_last`，再计算新 `error`（[_differentiate.py:552-560](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L552-L560)）：

```python
work.error_last = work.error          # 旧 error 存入 error_last
...
work.error = xp.abs(work.df - work.df_last)  # 计算新 error
```

#### 4.3.3 源码精读

误差回升启发式（[_differentiate.py:576-585](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L576-L585)）——对「误差相比上轮放大 10 倍且尚未判停」的元素写入 `-1`。注释解释了浮点消去误差是其物理来源：

```python
# With infinite precision, there is a step size below which
# all smaller step sizes will reduce the error. But in floating point
# arithmetic, catastrophic cancellation will begin to cause the error
# to increase again. This heuristic tries to avoid step sizes that are
# too small. ...
i = (work.error > work.error_last*10) & ~stop
work.status = xpx.at(work.status)[i].set(_EERRORINCREASE)
stop = xpx.at(stop)[i].set(True)
```

注意 `check_termination` 在末尾 `return stop`，把布尔掩码交还给框架 `eim._loop`，由后者完成「压缩活跃元素 + 写入结果对象」的工作。

#### 4.3.4 代码实践

**实践目标**：构造一个「带随机噪声」的函数，让消去误差主导、触发 `-1`。

**操作步骤**（示例代码，待本地验证）：

```python
# 示例代码
import numpy as np
from scipy.differentiate import derivative

rng = np.random.default_rng(0)

def noisy_exp(x):
    # 每次调用都乘一个随机数，使 f 在不同求值点之间不一致
    return np.exp(x) * rng.random()

res = derivative(noisy_exp, np.asarray(1.), tolerances=dict(rtol=1e-14))
print("status =", res.status, "(期望 -1)")
print("nit    =", res.nit, "nfev =", res.nfev)
```

**需要观察的现象**：随着步长缩小，相邻两轮 `df` 估计之差（即 `error`）被随机噪声主导而显著放大，触发 `error > 10*error_last`，从而 `status=-1`。

**预期结果**：`status` 多为 `-1`（受随机种子影响，偶有差异；固定种子可稳定复现）。这正是 [test_differentiate.py:101](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L101) 用 `xp.exp(x)*rng.random()` 制造 `-1` 的原理。

#### 4.3.5 小练习与答案

**练习 1**：把 `10` 改成 `2`，会对算法行为有什么影响？
**答案**：阈值变松，更容易触发 `-1`。正常收敛过程中的小抖动（本该继续迭代）也可能被误判为「误差回升」而过早停机，牺牲精度。所以 `10` 是在「灵敏」与「抗噪」之间的折中。

**练习 2**：为什么误差回升判据用的是 `error`（两轮估计之差），而不是直接看 `df` 是否变差？
**答案**：因为我们不知道真导数，无法判断 `df` 本身「变差没变差」。`error` 作为相邻估计之差，是唯一可观测的稳定性指标：它稳定变小代表收敛，开始放大代表越过最优点。

### 4.4 状态码归属：check_termination 与框架的分工

#### 4.4.1 概念说明

`check_termination` 只负责**主动**判停的三类（`0` / `-1` / `-3`）。还有两类终止——**触达 `maxiter`**（`-2`）和**被 callback 叫停**（`-4`）——是「被动」的：它们发生在迭代循环**自然结束**时，由通用框架 `eim._loop` 统一兜底赋值，而不是 `derivative` 自己写的。

理解这条分工线，才能解释为什么 `test_flags` 里第 3 个元素（`exp(x)` 配 `order=2`、严苛 `rtol`）会得到 `-2`：它在整个循环里**从未**满足 `check_termination` 的任何条件，只是一直没收敛，直到 `maxiter` 用尽。

#### 4.4.2 核心流程

`eim._loop` 的主循环在以下任一条件满足时退出：

- `work.nit >= maxiter`（用尽迭代次数）；
- `active` 为空（所有元素都已判停）；
- `cb_terminate`（callback 抛了 `StopIteration`）。

循环退出后，对**仍活跃**（即从未被 `check_termination` 判停）的元素，统一写入状态码：

```
work.status[:] = _ECALLBACK if cb_terminate else _ECONVERR
                  # -4 (callback)        # -2 (maxiter)
```

关键点：

- 此刻 `work` 已被压缩，只含**未判停**的活跃元素；这些元素的 `status` 仍停留在初始值 `_EINPROGRESS`（`1`）。
- 已经判停（`0` / `-1` / `-3`）的元素早已被 `_update_active` 拷进结果对象 `res`，不受这行影响。
- 因此最终结果里：判停元素保留各自状态，未判停元素统一为 `-2` 或 `-4`。

#### 4.4.3 源码精读

状态码常量定义（[_elementwise_iterative_method.py:21-27](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_elementwise_iterative_method.py#L21-L27)）——注意 `_ECONVERR=-2`（maxiter）、`_ECALLBACK=-4`（callback）、`_EVALUEERR=-3`（非有限值）、`_ECONVERGED=0`：

```python
_ESIGNERR = -1
_ECONVERR = -2
_EVALUEERR = -3
_ECALLBACK = -4
_EINPUTERR = -5
_ECONVERGED = 0
_EINPROGRESS = 1
```

`_loop` 退出时的兜底赋值（[_elementwise_iterative_method.py:278](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/_lib/_elementwise_iterative_method.py#L278)）——把所有仍活跃元素的 `status` 一次性改成 `-4`（callback 叫停）或 `-2`（maxiter 用尽）：

```python
work.status = xpx.at(work.status)[:].set(_ECALLBACK if cb_terminate else _ECONVERR)
```

而 `derivative` 初始化时，所有元素的 `status` 都是 `_EINPROGRESS`（[_differentiate.py:419](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L419)），等待被 `check_termination` 或循环兜底改写：

```python
status = xp.full_like(x, eim._EINPROGRESS, dtype=xp.int32)  # in progress
```

#### 4.4.4 代码实践

**实践目标**：用一个返回 `NaN` 的函数验证 `check_termination` 能在**第 1 轮**就抓住它（`nit=1`，状态 `-3`），从而区分「主动判停」与「maxiter 兜底」。

**操作步骤**（示例代码，待本地验证）：

```python
# 示例代码
import numpy as np
from scipy.differentiate import derivative

res = derivative(lambda x: np.full_like(x, np.nan), np.asarray(1.))
print("status =", res.status, " nit =", res.nit, " (期望 status=-3, nit=1)")
```

**需要观察的现象**：`status=-3` 且 `nit=1`——说明非有限值检测在第 1 轮（`nit` 已自增为 1）就生效，**没有**走到 maxiter 兜底（否则会是 `-2` 且 `nit=maxiter=10`）。

**预期结果**：`status=-3`，`nit=1`。对比：若改用 `order=2` 对 `np.exp` 求导且 `rtol=1e-14`，则应得 `status=-2`、`nit=10`（maxiter 兜底）。这正是 `test_flags` 同时检验的两类情形。

#### 4.4.5 小练习与答案

**练习 1**：一个元素如果同时「数值正常」但「一直没收敛」，最终 `status` 是多少？由谁设置？
**答案**：`-2`（`_ECONVERR`），由 `eim._loop` 在 L278 兜底设置，**不是** `check_termination` 设的。

**练习 2**：为什么 callback 叫停（`-4`）和 maxiter（`-2`）可以共用同一行兜底代码，而收敛/非有限值/误差回升却要分开写？
**答案**：`-2` 与 `-4` 的本质都是「循环结束了仍未主动判停」，区别仅在退出原因（callback 与否），用 `cb_terminate` 一个布尔即可区分；而前三类是**不同**的主动判停条件，判别式和写入的状态码各不相同，必须在 `check_termination` 里逐条表达。

## 5. 综合实践

`test_flags` 是本讲最精妙的设计：它用**一次** `derivative` 调用，让 4 个不同元素同时产出 4 种 `status`。请把它改写成一个可独立运行的脚本，并为每个元素解释它落入对应状态的原因。

**操作步骤**（示例代码，待本地验证）：

```python
# 示例代码
import numpy as np
from scipy.differentiate import derivative
import scipy._lib._elementwise_iterative_method as eim
from scipy.differentiate._differentiate import _EERRORINCREASE

rng = np.random.default_rng(5651219684984213)

def f(xs, js):
    funcs = [
        lambda x: x - 2.5,                       # 0: 线性函数，2 阶公式精确命中 -> 收敛 0
        lambda x: np.exp(x) * rng.random(),      # 1: 随机噪声 -> 误差回升 -1
        lambda x: np.exp(x),                     # 2: order=2 收敛太慢 -> 触达 maxiter -2
        lambda x: np.full_like(x, np.nan),       # 3: 返回 NaN -> 非有限值 -3
    ]
    res = [funcs[int(j)](x) for x, j in zip(xs, np.reshape(js, (-1,)))]
    return np.stack(res)

args = (np.arange(4, dtype=np.int64),)
res = derivative(f, np.ones(4, dtype=np.float64),
                 tolerances=dict(rtol=1e-14), order=2, args=args)

print("status:", res.status)
print("期望  :", [eim._ECONVERGED, _EERRORINCREASE, eim._ECONVERR, eim._EVALUEERR])
```

**你需要能回答的问题**（把本讲四节串起来）：

1. 元素 0 为什么在第 2 轮就收敛？（提示：线性函数的 2 阶中心差分**精确**，`df_2 == df_1`，故 `error_2 = 0 < tol`。）
2. 元素 1 为什么触发 `-1`？为什么不会更早？（提示：误差回升需 `error_last` 为实数，最早第 3 轮才生效，见 §4.3.2 的表。）
3. 元素 2 为什么是 `-2` 而不是 `-1`/`-3`？（提示：`order=2` 收敛慢但**单调**，既没回升也没变 NaN，只是没在 `maxiter` 内达到 `rtol=1e-14`，由 L278 兜底成 `-2`。）
4. 元素 3 为什么 `nit=1` 就停？（提示：第 1 轮 `post_func_eval` 算出 `df=NaN`，`nit>0` 守卫放行，非有限值检测立即命中 `-3`。）

**预期结果**：`res.status == [0, -1, -2, -3]`，与 [test_differentiate.py:113-117](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L113-L117) 的断言一致。

## 6. 本讲小结

- `check_termination` 是 `derivative` 的「停机裁判」，返回布尔掩码 `stop` 给框架，由框架完成活跃元素的压缩与结果写入。
- **三类主动终止**：收敛 `error < atol + rtol*|df|`（`0`）、非有限值 `~(isfinite(x) & isfinite(df) | stop)`（`-3`）、误差回升 `error > 10*error_last`（`-1`）。
- **优先级**靠 `| stop` / `& ~stop` 维持：先判收敛，再判非有限值，最后判误差回升；先判停的元素不被后续检查重新归类。
- **两个守卫**：非有限值检查有 `nit > 0` 守卫（避开初始 `df` 全 `NaN` 的预检）；误差回升需 `error_last` 为实数，最早第 3 轮才生效。
- **两类被动终止** `-2`（maxiter）与 `-4`（callback）**不在**本函数设置，而由 `eim._loop` 在 L278 兜底，只作用于「循环结束时仍未判停」的活跃元素。
- 默认容差（`atol=smallest_normal`、`rtol=sqrt(eps)`）非常严苛，真导数为 0 的点需手动设 `atol` 才能收敛——这是收敛判据的数学直接推论。

## 7. 下一步学习建议

- 本讲把 `derivative` 的内部钩子（`pre_func_eval` / `post_func_eval` / `check_termination`）全部讲完。下一步进入 [u2-l6 逐元素迭代框架 eim._loop 与 _initialize](u2-l6-elementwise-loop-framework.md)，看这些钩子是如何被 `_loop` 串联起来、`stop` 掩码如何驱动活跃元素压缩与结果组装的。
- 想从「测试」反推边界行为，可先读 [u4-l5 测试体系与边界情况](u4-l5-testing-edge-cases.md)，其中的 `test_flags`、`test_special_cases`、`test_saddle_gh18811` 都与本讲强相关。
- 对「真导数为 0」的鞍点收敛陷阱感兴趣的读者，建议结合 [u4-l3 数值精度、消去误差与调参](u4-l3-numerical-precision-tuning.md) 深入理解 `atol` 的作用。
