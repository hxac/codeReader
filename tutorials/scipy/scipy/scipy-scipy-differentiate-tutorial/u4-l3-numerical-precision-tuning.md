# 数值精度、消去误差与调参

## 1. 本讲目标

本讲聚焦 `scipy.differentiate.derivative` 在**真实浮点环境**下的三类典型精度问题，以及对应的调参对策。读完本讲你应该能够：

1. 说清楚有限差分里**截断误差**与**消去误差**此消彼长的 U 形关系，并能解释源码里 `status=-1`（误差回升）的物理来源。
2. 理解为什么步长是**绝对**的，以及当 `|x|` 很大（如 `1e20`）时默认 `initial_step=0.5` 会“无声失败”，知道该把 `initial_step` 调到多大。
3. 学会针对**真导数恰为零**的点（鞍点）显式设置 `atol`，理解默认容差为何在那里收敛不了。

> 本讲是 **u2-l5（check_termination）** 的延伸：u2-l5 讲的是终止判据“是什么、怎么判”，本讲讲的是“为什么误差会先降后升、为什么有些点死活不收敛、怎么调参救回来”。建议先完成 u2-l5 再读本讲。

## 2. 前置知识

### 2.1 截断误差（truncation error）

有限差分用差商代替微商，本质是丢弃了 Taylor 展开的高阶项，这部分被丢弃的项就是截断误差。对一个 `order` 阶中心差分公式，截断误差大致正比于 \(h^{\text{order}}\)（`h` 是步长）。`order` 越高、`h` 越小，截断误差越小。

### 2.2 消去误差 / 浮点舍入误差（cancellation / round-off error）

计算机里每个浮点数都带有相对误差 \(\varepsilon\)（float64 的 \(\varepsilon \approx 2.2\times10^{-16}\)）。当我们计算 \(f(x+h)-f(x-h)\) 时，两个很接近的数相减会**放大相对误差**；再除以很小的 \(h\)，噪声被进一步放大。`h` 越小，消去误差越大。

### 2.3 ULP：浮点数的“最小可分辨步长”

在数值 \(x\) 附近，相邻两个可表示浮点数的间距叫 **ULP**（unit in the last place）。一个经验近似是：

\[
\mathrm{ulp}(x) \approx \varepsilon\,|x|
\]

任何比 ULP 还小的增量 \(h\)，都会被舍入“吞掉”，即 \(x+h\) 在浮点意义下**等于** \(x\)。这是本讲第二模块的核心。

### 2.4 默认容差回顾（来自 u2-l5 / u1-l3）

`derivative` 在初始化时给出默认容差：

- `atol = finfo.smallest_normal`（float64 约 \(2.2\times10^{-308}\)，极小）
- `rtol = finfo.eps**0.5`（float64 约 \(1.5\times10^{-8}\)）

收敛判据是 `error < atol + rtol*abs(df)`。当 `|df|` 远离 0 时，`rtol*|df|` 主导；当真导数为 0 时，`|df|` 趋近 0，判据几乎退化成 `error < smallest_normal`——这正是第三模块的陷阱。

## 3. 本讲源码地图

本讲几乎全部围绕单个文件展开：

| 文件 | 作用 |
|------|------|
| [`scipy/differentiate/_differentiate.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py) | `derivative` 的全部实现：docstring 的 Notes 讲清了三类精度陷阱；默认容差、收敛判据、误差回升启发式都在此 |
| [`scipy/differentiate/tests/test_differentiate.py`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py) | `test_saddle_gh18811` 是官方对“零导数鞍点”的回归测试，目前标记为 `xfail`，是理解默认容差局限的最佳入口 |

## 4. 核心概念与源码讲解

### 4.1 截断误差与消去误差的权衡

#### 4.1.1 概念说明

`derivative` 的迭代策略是：从大步长起步，每轮把步长除以 `step_factor`（默认 2），逐步缩小。直觉上步长越小越精确，但这只在“无限精度”的世界里成立。真实浮点环境下，总误差由两部分叠加：

\[
E(h) \;\approx\; \underbrace{C\,h^{\text{order}}}_{\text{截断误差}} \;+\; \underbrace{\frac{\varepsilon\,|f|}{h}}_{\text{消去误差}}
\]

- 左项随 \(h\) 减小而**下降**（截断误差越小）。
- 右项随 \(h\) 减小而**上升**（相减除以更小的 \(h\)，噪声被放大）。

两者相加是一条 **U 形曲线**：先降后升。存在一个最优步长 \(h^\*\)，使总误差最小。对 \(p\) 阶公式，令 \(dE/dh=0\) 可得：

\[
h^\* \;\sim\; \varepsilon^{1/(p+1)}, \qquad E_{\min} \;\sim\; \varepsilon^{p/(p+1)}
\]

也就是说：**阶数越高，能逼近的极限精度越高，最优步长也越大**。对默认 `order=8`，\(h^\* \sim \varepsilon^{1/9}\approx 0.017\)，可达到的极限相对误差约 \(\varepsilon^{8/9}\approx 6\times10^{-15}\)。

#### 4.1.2 核心流程

`derivative` 不是去解析地求 \(h^\*\)，而是用一个朴素的启发式**自适应**地停在 U 形底部附近：

```
每一轮：
  1. pre_func_eval  : 按 work.h / step_factor 生成新求值点
  2. func           : 求值
  3. post_func_eval : 加权得到 df，error = |df - df_last|，然后 work.h /= fac
  4. check_termination:
       - error < atol + rtol*|df|        -> status=0  收敛
       - 出现非有限值                     -> status=-3
       - error > 10*error_last           -> status=-1  误差回升（U 形右臂！）
```

关键洞察：`error`（相邻两轮估计之差）在 U 形左臂随 \(h\) 减小而减小，越过底部进入右臂后开始**回升**。`check_termination` 里的 `error > 10*error_last` 判据，正是用来捕捉“误差开始回升、说明步长已经太小、继续缩小只会被消去误差吞噬”这一信号，及时收手并标记 `status=-1`。

#### 4.1.3 源码精读

docstring 的 Notes 直接点明了每轮误差的衰减规律，以及“有限精度会阻止进一步改善”：

> Each iteration, the step size is reduced by `step_factor`, so for sufficiently small initial step, each iteration reduces the error by a factor of `1/step_factor**order` until finite precision arithmetic inhibits further improvement.

对应源码与示例（示例用 `order=4`、`step_factor=2`，验证衰减因子 \(1/2^4=0.0625\)）：

- [`_differentiate.py:262-266`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L262-L266) — docstring 解释每轮误差按 \(1/\text{step\_factor}^{\text{order}}\) 衰减，直到浮点精度触底。
- [`_differentiate.py:290-291`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L290-L291) — 示例输出 `(0.06215…, 0.0625)`，实测衰减比与理论 \(1/2^4\) 高度吻合。

而 `check_termination` 里捕捉 U 形右臂的判据，配有一段很诚实的注释，说明这是“简单但有效”的启发式，而非理论上最优的最小误差步长检测：

- [`_differentiate.py:576-585`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L576-L585) — 注释解释“无限精度下总存在一个步长，更小的步长会持续降误差；但浮点下灾难性消去会令误差重新上升”，随后 `i = (work.error > work.error_last*10) & ~stop` 即误差回升判据，置 `status=_EERRORINCREASE(-1)`。

`post_func_eval` 里 `work.h /= work.fac`（步长每轮缩减）和 `work.error = xp.abs(work.df - work.df_last)`（误差取相邻两轮之差）是 U 形曲线得以形成的两个动作：

- [`_differentiate.py:551`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L551) — `work.h /= work.fac`：步长每轮除以 `step_factor`，沿 U 形从右向左移动。
- [`_differentiate.py:553-560`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L553-L560) — 误差估计注释 + `work.error = xp.abs(work.df - work.df_last)`。

> 关于 `step_factor`：docstring 还提到若设 `step_factor < 1`，后续步长会**变大**，可用于规避“步长小于某阈值会引入消去误差”的情形——这是手动把迭代推离 U 形右臂的另一种手段，见 [`_differentiate.py:120-125`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L120-L125)。

#### 4.1.4 代码实践

**实践目标**：亲手画出 `error` 随迭代的 U 形曲线，验证“先按 \(1/\text{step\_factor}^{\text{order}}\) 衰减、后被浮点消去误差抬升”。

**操作步骤**（复刻 docstring 示例的思路）：

```python
# 示例代码
import numpy as np
import matplotlib.pyplot as plt
from scipy.differentiate import derivative

f, df = np.exp, np.exp          # 真导数也是 exp
x   = 1.0
ref = df(x)
order, hfac = 4, 2              # order=4 让衰减段更长更易观察

true_err = []
for i in range(1, 12):
    res = derivative(f, x, maxiter=i, order=order, step_factor=hfac,
                     tolerances=dict(atol=0, rtol=0))   # 关掉终止，强制跑满
    true_err.append(abs(res.df - ref))
true_err = np.array(true_err)

plt.semilogy(range(1, 12), true_err, 'o-', label='true error')
plt.axhline(1 / hfac**order, ls='--', label=f'1/fac^order = {1/hfac**order}')
plt.xlabel('iteration'); plt.ylabel('|df - ref|'); plt.legend()
```

**需要观察的现象**：
- 前几轮误差近似按 \(1/2^4 = 0.0625\) 倍下降（与图中虚线吻合）。
- 到某几轮后误差不再下降，反而开始回升——这就是 U 形右臂、消去误差主导。
- 若把 `order` 改成默认的 8，衰减段会非常短（每轮 \(1/256\)），很快触底。

**预期结果**：与 docstring 示例一致，相邻两轮误差比约 `0.062`（`order=4`）。（具体触底与回升的轮次随机器略有差异，待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：把上面实践里的 `order` 从 4 改成 8，理论上每轮误差应按什么因子下降？触底后的极限相对精度大约是多少？

**参考答案**：每轮按 \(1/2^8 = 1/256 \approx 0.0039\) 下降；极限相对精度约 \(\varepsilon^{8/9}\approx 6\times10^{-15}\)。所以高阶公式收敛更快、极限更准，但可用步长范围更窄、更容易撞上消去误差。

**练习 2**：为什么 `check_termination` 里误差回升判据用的是 `error > 10*error_last`（放大 10 倍）而不是 `error > error_last`（刚一上升就停）？

**参考答案**：误差在 U 形底部附近会有正常的、轻微的随机抖动（浮点噪声），若“刚一上升就停”会被噪声误触发、过早终止在尚未到最优步长处；要求回升到 10 倍以上才认定“确实进入右臂”，给底部留出缓冲，是鲁棒性与最优性之间的折中（见源码注释 [`_differentiate.py:576-582`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L576-L582)）。

---

### 4.2 步长选择与大 |x|

#### 4.2.1 概念说明

`derivative` 的步长是**绝对**的，不是相对的：`initial_step=0.5` 意味着无论 `x` 是 1 还是 1e20，第一轮扰动都是 0.5 附近的绝对量。问题在于：当 `|x|` 很大时，0.5 远小于该处的 ULP，扰动会被浮点舍入完全吞掉。

以 `x = 1e20` 为例：

\[
\mathrm{ulp}(10^{20}) \;\approx\; \varepsilon\cdot10^{20} \;\approx\; 2.2\times10^{4}
\]

也就是说，在 1e20 附近，相邻两个可表示浮点数相距约两万多。于是 `1e20 + 0.5` 在浮点下**等于** `1e20`——扰动根本没有发生。所有求值点 `x±h` 全部塌缩到同一个 `x`，函数值完全相同，差商恒为 0，导数估计毫无意义。

> 注意：docstring 举例用的就是 `1e20`，但**不要**字面地用 `np.exp(1e20)`——它会先溢出成 `inf`（`e^{1e20}` 远超 float64 上限 ~1.8e308），掩盖掉步长可分辨性问题。要单独演示步长问题，应换一个在 1e20 处仍有限的函数（如 `x**2`、`sqrt(x)`）。

#### 4.2.2 核心流程

正确的做法是让 `initial_step` 与 `|x|` 同量级，经验法则：

\[
\text{initial\_step} \;\gtrsim\; \sqrt{\varepsilon}\,|x| \;\approx\; 1.5\times10^{-8}\,|x|
\]

对 `x=1e20`，即 `initial_step ≈ 1.5e12`。这样首轮最远求值点距 `x` 约 \(1.5\times10^{12}\)，远大于 ULP（\(2.2\times10^{4}\)），扰动可被正确分辨；而后续每轮除以 `step_factor` 缩小，仍能在 U 形左臂停留若干轮才触底。

更隐蔽的危险：步长塌缩往往**不会**触发 `status=-3`（非有限值），而是让函数“看起来常数”，所有估计都是 0、`error` 也是 0，于是算法“成功收敛”到 `df=0`——一个**静默的错误答案**（`success=True` 但值完全错）。这是大 `|x|` 下最需要警惕的失败模式。

#### 4.2.3 源码精读

docstring 的 Notes 用一整段警告了这一点：

- [`_differentiate.py:222-225`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L222-L225) — 明确“步长是绝对的；当步长远小于 `|x|` 时精度丢失，例如 `x=1e20` 时默认 `0.5` 无法分辨，建议对大 `|x|` 用更大的 `initial_step`”。

`pre_func_eval` 里有两处 TODO 表明作者也意识到这是未竟之业（当前版本**不会**自动修正）：

- [`_differentiate.py:463-468`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L463-L468) — TODO：考虑测量“实际生效的步长”（因为 `(x+h)-x` 在浮点下不一定等于 `h`），以及“当 `x` 太大、步长不可分辨时自动调整步长”。这正是本模块讨论的问题，目前需用户自行处理。

求值点的生成逻辑（步长 `h` 来自 `work.h`，首轮按几何级数排布、之后每轮除以 `fac`）：

- [`_differentiate.py:476-485`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L476-L485) — `hc`/`hr` 的计算；`h` 一旦小于 ULP，`work.x + hc` 会被舍入回 `work.x`，扰动失效。

#### 4.2.4 代码实践

**实践目标**：直观看到默认 `initial_step` 在大 `|x|` 下“静默失败”，并用更大的 `initial_step` 修正。

**操作步骤**：

```python
# 示例代码
import numpy as np
from scipy.differentiate import derivative

# 1) 先确认浮点事实：0.5 在 1e20 处不可分辨
x = 1e20
print("1e20 + 0.5 == 1e20 :", (x + 0.5) == x)        # 预期 True
print("ulp(1e20)          :", np.spacing(x))          # 预期 ~1.9e4
print("sqrt(eps)*|x|      :", np.finfo(float).eps**0.5 * abs(x))  # 预期 ~1.5e12

# 2) 用一个在 1e20 处不溢出的函数：f(x)=x**2，真导数 2x = 2e20
f = lambda x: x**2

res_default = derivative(f, x, initial_step=0.5)               # 默认步长
res_large   = derivative(f, x, initial_step=1e12)              # 放大步长

print("default : df =", res_default.df, " success =", res_default.success)
print("large   : df =", res_large.df,   " success =", res_large.success)
print("true    : 2x =", 2*x)
```

**需要观察的现象**：
- `1e20 + 0.5 == 1e20` 为 `True`，确认步长被吞。
- 默认步长：`df` 接近 **0**（很可能 `success=True`！），与真值 `2e20` 完全不符——典型的静默错误。
- 放大步长：`df` 接近 `2e20`，`success=True`，结果正确。

**预期结果**：默认步长得到错误的 ~0，放大步长得到正确的 ~2e20。（精确数值待本地验证；若想用 `np.exp`，请把 `x` 换到如 `1e3` 这类 `exp` 不溢出但仍需较大步长的点。）

#### 4.2.5 小练习与答案

**练习 1**：为什么步长塌缩时通常报 `success=True` 而不是 `status=-3`？

**参考答案**：`status=-3` 只在出现**非有限值**（NaN/inf）时触发（见 [`_differentiate.py:570-574`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L570-L574)）。步长塌缩时所有求值点相同、函数值是同一个有限数，差商为有限的 0，`error` 也为 0，于是满足 `error < atol + rtol*|df|` 而“收敛”到 0。这正是它危险的原因：失败是静默的。

**练习 2**：若函数定义域有界（如只在 `[0, 2]` 上有限），把 `initial_step` 调大时还要注意什么？

**参考答案**：首轮最远求值点距 `x` 恰为 `initial_step`（见 u4-l2），调大步长可能让求值点越出定义域、返回 NaN，转而触发 `status=-3`。此时需配合 `step_direction` 把扰动限制在域内（左边界用 `+1`、右边界用 `-1`），并保证 `initial_step` 不大于到最近边界的有向距离——大 `|x|` 的步长放大与边界处理可能需要同时考虑。

---

### 4.3 atol / rtol 调参与零导数陷阱

#### 4.3.1 概念说明

收敛判据是：

\[
\text{error} < \text{atol} + \text{rtol}\cdot|df|
\]

默认 `atol = smallest_normal ≈ 2.2e-308`（几乎为 0），`rtol = sqrt(eps) ≈ 1.5e-8`。当真导数**远离 0** 时，`rtol*|df|` 给出一个合理的绝对门槛，工作正常。

但当**真导数恰为 0**（鞍点、极值点，如 \((x-1)^3\) 在 \(x=1\) 处 \(f'=0\)），计算出的 `df` 只剩浮点噪声（~1e-16 量级），于是：

\[
\text{rtol}\cdot|df| \;\approx\; 1.5\times10^{-8}\times10^{-16} \;\approx\; 10^{-24}, \qquad
\text{atol} \;\approx\; 10^{-308}
\]

判据几乎退化成“`error` 必须小于 ~1e-24”。而 `error` 本身也是两个 1e-16 量级噪声估计之差，通常停在 1e-16 附近、远大于 1e-24，**永远收敛不了**——最终撞上 `maxiter`（`status=-2`）或误差回升（`status=-1`）。

#### 4.3.2 核心流程

对策是显式给一个**有物理意义的 `atol`**，比如 `1e-12`：

\[
\text{error} < 10^{-12} + \text{rtol}\cdot|df|
\]

这样 `df` 一旦稳定到 1e-16 量级的噪声，`error`（~1e-16）立即小于 1e-12，顺利收敛、`success=True`。docstring 给的经验值正是 `atol=1e-12`：

> If the derivative may be exactly zero, consider specifying an absolute tolerance (e.g. `atol=1e-12`) to improve convergence.

注意分寸：`atol` 给得太松（如 1e-2）会让非零导数也“假收敛”、损失精度；给得太紧（如 1e-16，逼近噪声地板）又可能重新收敛不了——官方针对此的回归测试 `test_saddle_gh18811` 正是用 `atol=1e-16`，目前仍标记为 **`xfail`**（预期失败），说明在该极限容差下行为尚不稳定。

#### 4.3.3 源码精读

默认容差的赋值（注意它依赖 `dtype`，所以在 `_initialize` 之后才计算，不在校验层）：

- [`_differentiate.py:400-402`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L400-L402) — `atol = finfo.smallest_normal`、`rtol = finfo.eps**0.5`；注释 `# keep same as hessian` 表明 `hessian` 沿用同一套默认值。

收敛判据本身（来自 u2-l5）：

- [`_differentiate.py:566`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L566) — `i = work.error < work.atol + work.rtol*abs(work.df)`：当真导数为 0 时右端塌缩到 `smallest_normal`，判据过严。

docstring Notes 对零导数陷阱的正式说明：

- [`_differentiate.py:227-230`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L227-L230) — “默认容差在真导数恰为零处很难满足；若导数可能为零，考虑指定 `atol`（如 1e-12）以改善收敛。”

tolerances 参数的文档（默认值说明）：

- [`_differentiate.py:102-111`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L102-L111) — 解释 `atol`/`rtol` 含义与“默认 `atol`=最小正规数、默认 `rtol`=精度平方根”。

官方回归测试（目前 `xfail`，是理解默认容差局限的最佳现实参照）：

- [`test_differentiate.py:427-440`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L427-L440) — `test_saddle_gh18811`：对 \((x-1)^3\)（及一个 \(C^2\) 不光滑变体）在 `x=1` 用 `atol=1e-16`、`step_direction=[-1,0,1]` 求 `derivative`，断言 `all(res.success)` 且 `df≈0`；测试标记 `@pytest.mark.xfail`，说明在 `1e-16` 这种贴近噪声地板的容差下尚不能保证稳定收敛。

#### 4.3.4 代码实践

**实践目标**：复现“真导数为 0 时默认容差收敛失败、设 `atol` 后恢复”的现象，并诚实地检验 `1e-16` 这一极限容差。

**操作步骤**：

```python
# 示例代码
import numpy as np
from scipy.differentiate import derivative

f = lambda x: (x - 1)**3      # 真导数 f'(1) = 0（鞍点 / 拐点）
x = 1.0

# (a) 默认容差：真导数为 0，预期收敛失败
res_def = derivative(f, x)
print("default : status =", res_def.status, " df =", res_def.df, " success =", res_def.success)

# (b) 松弛 atol（docstring 推荐量级）：预期收敛到 0
res_atol = derivative(f, x, tolerances=dict(atol=1e-12))
print("atol=1e-12: status =", res_atol.status, " df =", res_atol.df, " success =", res_atol.success)

# (c) 极限容差 1e-16（对应 test_saddle_gh18811，目前 xfail）
res_tight = derivative(f, x, tolerances=dict(atol=1e-16))
print("atol=1e-16: status =", res_tight.status, " df =", res_tight.df, " success =", res_tight.success)
```

**需要观察的现象**：
- (a) 默认容差：`status` 多半为 `-1`（误差回升）或 `-2`（触达 `maxiter`），`success=False`。
- (b) `atol=1e-12`：`status=0`、`success=True`、`df` 在 `1e-12` 以内接近 0。
- (c) `atol=1e-16`：结果**不稳定**——可能收敛也可能不收敛，这正是官方测试标 `xfail` 的原因。

**预期结果**：(a) 失败、(b) 成功收敛到 ~0；(c) 行为待本地验证（与 `test_saddle_gh18811` 的 `xfail` 现状一致，不建议在生产中依赖 `1e-16`）。

#### 4.3.5 小练习与答案

**练习 1**：为什么默认 `atol` 取 `smallest_normal`（~2e-308）而不是 0？

**参考答案**：取 0 会让判据完全由 `rtol*|df|` 决定，当 `df→0` 时门槛也→0，任何浮点噪声都无法满足；取 `smallest_normal` 是“比 0 大一点点、又不至于影响正常量级导数”的折中——对非零导数它可忽略（被 `rtol*|df|` 主导），对零导数它仍太小、救不了，所以才需要用户手动放大。见 [`_differentiate.py:400-402`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L400-L402)。

**练习 2**：把 `atol` 设成 `1e-2` 会让 \((x-1)^3\) 在 `x=1` 收敛，但为什么对一般函数这是个坏主意？

**参考答案**：`atol=1e-2` 意味着只要 `error` 小于 0.01 就算收敛。对真导数本就不为零、且需要高精度的函数，这会在 `df` 还差很远时就提前停机（“假收敛”），返回严重失真的导数。`atol` 应根据“该问题可接受的绝对导数误差”来定：零导数点用 1e-12 量级足够，绝不可一刀切地放宽到 1e-2。

**练习 3**：阅读 [`test_differentiate.py:429-431`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/tests/test_differentiate.py#L429-L431) 中两个被测函数，第二个 `np.where(x>1, (x-1)**5, (x-1)**3)` 为什么比纯 `(x-1)**3` 更难收敛？

**参考答案**：纯 `(x-1)**3` 是光滑多项式，8 阶中心差分对 3 次多项式**精确成立**（误差纯浮点噪声）。而 `where` 版本在 `x=1` 处只是 \(C^2\)（三阶导数从左侧的 6 跳到右侧的 0），中心差分点跨在间断两侧，截断误差不再按 \(h^8\) 衰减、表现异常，更难满足容差——这正是该测试用 `step_direction=[-1,0,1]` 多方向、并整体标 `xfail` 的原因。

## 5. 综合实践

把三个模块串起来，写一个小诊断脚本，对同一个函数 `f(x) = (x-1)**3` 在三个典型点评估 `derivative` 的“健康度”，并针对每个点给出调参建议：

1. **普通点 `x=2`**（真导数 `3*(2-1)^2 = 3`）：用默认参数，确认 `success=True` 且 `df≈3`。
2. **鞍点 `x=1`**（真导数 `0`）：默认参数应失败；改用 `tolerances=dict(atol=1e-12)`，确认收敛到 0。
3. **大 `|x|` 点 `x=1e20`**：注意 `(x-1)**3` 在 1e20 处值约 `1e60`（仍可表示），但默认 `initial_step=0.5` 会被吞；先验证 `1e20+0.5==1e20`，再用 `initial_step=1e12` 重算，对比 `df`（真导数 `3*(1e20-1)^2 ≈ 3e40`）。

要求：
- 对每个点打印 `res.status`、`res.success`、`res.df`、`res.error`。
- 对失败案例，用一句话诊断它属于“截断/消去 U 形右臂（status=-1）”“触达 maxiter（status=-2）”“非有限值（status=-3）”还是“静默错误（success=True 但值错）”中的哪一类，并给出对应调参对策（调 `atol` / 调 `initial_step` / 调 `step_direction`）。

这个任务综合运用了 U 形误差识别、大 `|x|` 步长放大、零导数 `atol` 设置三项技能，是判断“一次 `derivative` 调用是否可信”的实战 checklist。

## 6. 本讲小结

- 有限差分的总误差 = 截断误差（\(\propto h^{\text{order}}\)，随 \(h\) 减小而降）+ 消去误差（\(\propto \varepsilon|f|/h\)，随 \(h\) 减小而升），呈 **U 形**；`status=-1`（误差回升）就是 `check_termination` 捕捉到的 U 形右臂。
- 步长是**绝对**的。当 `|x|` 很大（如 `1e20`）时，默认 `initial_step=0.5` 小于 ULP（~2e4）会被舍入吞掉，扰动失效；经验法则 `initial_step ≳ sqrt(eps)*|x|`（1e20 处约 `1e12`）。注意大 `|x|` 失败常是**静默**的（`success=True` 但 `df` 错）。
- 默认 `atol=smallest_normal`、`rtol=sqrt(eps)`。真导数恰为 0 时 `rtol*|df|→0`、判据过严，收敛失败；对策是显式设 `atol`（如 `1e-12`）。`atol` 太松会假收敛、太紧（如 `1e-16`）又不稳定（`test_saddle_gh18811` 因此 `xfail`）。
- 每轮误差衰减因子为 \(1/\text{step\_factor}^{\text{order}}\)（docstring 示例 `order=4` 实测 ~0.062≈1/16）；`step_factor<1` 可手动把迭代推离 U 形右臂。
- 调参 checklist：先看 `success`/`status`；大 `|x|` 调 `initial_step`；零导数调 `atol`；定义域受限配合 `step_direction`。

## 7. 下一步学习建议

- **测试体系全景**：本讲多次引用了 `test_saddle_gh18811` 等 `xfail`/边界用例，建议下一站阅读 **u4-l5（测试体系与边界情况）**，系统了解 `test_differentiate.py` 的跨后端测试框架（`make_xp_test_case`）、状态标志测试与精度测试的组织方式。
- **跨后端实现**：本讲的精度讨论完全在 NumPy/float64 语境下；若你关心 single precision（float32，`eps≈1.2e-7`，U 形与最优步长都不同）或 Torch/JAX 后端下的相同问题，可接着读 **u4-l4（ArrayAPI 后端支持）**，理解 `array_namespace`/`xp_promote` 如何让同一套算法跑在不同精度与后端上。
- **进阶源码**：想更深入了解误差估计的改进空间，可阅读 [`_differentiate.py:504-518`](https://github.com/scipy/scipy/blob/5f09bd719ca35a5c4de9644a097d379e5b3b4165/scipy/differentiate/_differentiate.py#L504-L518) 中 `post_func_eval` 的 TODO（Richardson 外推、噪声容忍的多项式拟合等），它们正是为了更鲁棒地处理本讲讨论的精度极限。
