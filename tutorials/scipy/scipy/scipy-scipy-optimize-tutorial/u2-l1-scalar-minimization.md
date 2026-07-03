# 标量函数最小化：brent / bounded / golden

## 1. 本讲目标

本讲聚焦 `scipy.optimize` 中**一维（标量）函数**的局部最小化。读完本讲，你应当能够：

1. 说清楚 `minimize_scalar` 这个统一入口是如何根据 `method` / `bounds` 把请求分发到三种底层算法的。
2. 理解 **Brent 方法**为什么用「抛物线插值 + 黄金分割保护」组合，以及它比纯黄金分割快在哪里。
3. 区分**有界 Brent（bounded）**对带上下界问题的处理方式，以及它和经典 Brent 的差异。
4. 看懂**黄金分割法（golden）**这一最朴素的一维搜索，并理解它的收敛速率。
5. 理解 `bracket`（括号）的概念，以及当括号构造失败时 `_recover_from_bracket_error` 的「优雅退化」策略。

本讲是单元 2「标量优化与一维求根」的第一讲，承接 [u1-l3](u1-l3-minimize-dispatcher.md) 讲过的统一调度入口 `minimize` / `minimize_scalar`，把视角从「多元调度」收缩到「一维求解」这一最简单、也最基础的情形。

## 2. 前置知识

在进入源码前，先用直觉建立三个概念。

### 2.1 什么是一维最小化

给定一个一元函数 \( f(x) \)，我们想找一个点 \( x^\* \)，使得 \( f(x^\*) \) 在某个局部范围内最小。这里**不假设我们能算导数**——这正是本讲三种方法的共同特点：它们都是**无导数（derivative-free）**方法，只靠「在若干个点上求函数值」来逼近最小点。

### 2.2 括号（bracket）：一维搜索的地基

在求一维最小值时，有一个比「初值点」更强的起点概念——**括号**。一个合法的括号是三个点 \( x_a < x_b < x_c \)，满足

\[
f(x_b) \le f(x_a) \quad \text{且} \quad f(x_b) \le f(x_c),
\]

且两条不等式中至少一条严格成立（中间点比两端都不高）。直观上，这保证 \( [x_a, x_c] \) 区间内**一定存在一个局部极小**（函数从 \( x_a \) 下降到 \( x_b \)，再从 \( x_b \) 上升到 \( x_c \)）。只要握住一个合法括号，就可以不断**收缩**它，把极小点夹得越来越紧。

本讲的 `brent` 和 `golden` 都依赖括号；`bounded` 则直接拿用户给的上下界当括号用，不需要单独构造。

### 2.3 黄金比例

三种方法反复出现一个常数族，都来自**黄金比例**：

\[
\varphi = \frac{1+\sqrt{5}}{2} \approx 1.618034,\qquad \frac{1}{\varphi} \approx 0.618034,\qquad 1-\frac{1}{\varphi} = \frac{3-\sqrt{5}}{2} \approx 0.381966.
\]

你会看到源码里 `golden_mean = 0.5*(3.0-sqrt(5.0))`（≈0.381966）、`Brent` 类里的 `self._cg = 0.3819660`、`golden` 里的 `_gR = 0.61803399`、`bracket` 里的 `_gold = 1.618034`——它们都是黄金比例家族的成员，只是在不同公式里以不同形态出现。记住这一点，源码里的「魔数」就不再神秘。

## 3. 本讲源码地图

本讲只涉及两个文件，但它们各自承担不同角色：

| 文件 | 在本讲中的角色 |
|------|--------------|
| [`_minimize.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_minimize.py) | 提供**统一入口** `minimize_scalar`，负责方法分发、默认选择、容差翻译、与括号回退包装。**不含任何算法逻辑**。 |
| [`_optimize.py`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py) | 提供**三种算法的真正实现**：`Brent` 类、`_minimize_scalar_brent`、`_minimize_scalar_bounded`、`_minimize_scalar_golden`，以及支撑它们的 `bracket`、`BracketError`、`_recover_from_bracket_error`。 |

这个分工正是 [u1-l3](u1-l3-minimize-dispatcher.md) 强调过的「调度层 vs 实现层」：`_minimize.py` 只决定「用哪个方法」，`_optimize.py` 才真正「跑算法」。本讲凡引用 `_minimize.py` 都在讲分发逻辑，引用 `_optimize.py` 都在讲算法本身。

---

## 4. 核心概念与源码讲解

### 4.1 minimize_scalar 分发：从入口到三种方法

#### 4.1.1 概念说明

`minimize_scalar` 是一维版的最小化入口。它和多元的 `minimize` 同构（详见 [u1-l3](u1-l3-minimize-dispatcher.md)）：**自己不做优化**，只按 `method` 字符串把请求派发到底层 `_minimize_scalar_*` 函数。一维世界只有三种方法，由一个常量列表枚举：

[_minimize.py:52](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_minimize.py#L52) 定义了 `MINIMIZE_SCALAR_METHODS = ['brent', 'bounded', 'golden']`——这就是 `method` 可选的全部取值。

#### 4.1.2 核心流程

`minimize_scalar` 的处理顺序可以拆成五步：

1. **确定方法名**：若 `method` 是可调用对象 → 自定义（`_custom`）；若为 `None` → 看有没有 `bounds`：有界选 `bounded`，否则选 `brent`。
2. **互斥校验**：`brent` / `golden` 不接受 `bounds`，传了直接报 `ValueError`。
3. **容差翻译**：把通用 `tol` 翻译成各方法私有容差（`bounded` 用 `xatol`，其余用 `xtol`）。
4. **分发**：按 `meth` 走 `if/elif` 链，调用对应实现。
5. **结果整形**：把 `res.x`、`res.fun` 的形状对齐，方便未来向量化。

#### 4.1.3 源码精读

第一步——默认方法选择。注意「有界 → bounded」的优先级：

[_minimize.py:988-993](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_minimize.py#L988-L993) 处理 `method`：可调用→`_custom`；`None` 时 `bounds is None` 选 `brent`，否则选 `bounded`；否则取 `method.lower()`（大小写不敏感）。

第二步——互斥校验。这是初学者常踩的坑：不能既给 `bounds` 又用 `brent`/`golden`：

[_minimize.py:997-999](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_minimize.py#L997-L999) 当 `bounds` 非 `None` 且方法是 `brent`/`golden` 时，抛出 `ValueError("Use of `bounds` is incompatible with 'method=...'.")`。

第三步——容差翻译。`bounded` 只支持**绝对**容差（一维有界搜索没有「相对 x」概念），所以会发 `RuntimeWarning` 并改写成 `xatol`：

[_minimize.py:1001-1011](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_minimize.py#L1001-L1011) 对 `bounded` 设 `xatol`，对 `_custom` 设 `tol`，其余方法设 `xtol`。

第四步——真正的分发。注意 `brent` 和 `golden` 都被包了一层 `_recover_from_bracket_error`，而 `bounded` 没有（因为它不用 `bracket`）：

[_minimize.py:1018-1032](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_minimize.py#L1018-L1032) 分发链：`brent` → `_recover_from_bracket_error(_minimize_scalar_brent, ...)`；`bounded` → 校验 bounds 非空后调 `_minimize_scalar_bounded`；`golden` → `_recover_from_bracket_error(_minimize_scalar_golden, ...)`。

第五步——结果整形（修复 gh-16196 的形状不一致问题）：

[_minimize.py:1037-1038](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_minimize.py#L1037-L1038) 把 `res.fun` 转成标量，并把 `res.x` reshape 成与 `res.fun` 同形状。

> **承接提示**：这套「默认选择 + 互斥校验 + 容差翻译 + if/elif 分发」的模式，和 [u1-l3](u1-l3-minimize-dispatcher.md) 讲的多元 `minimize` 完全一致，只是方法更少、更简单。

#### 4.1.4 代码实践

**实践目标**：直观验证「有 bounds 默认走 bounded，无 bounds 默认走 brent」，以及 bounds 与 brent/golden 的互斥关系。

**操作步骤**（保存为 `scalar_dispatch_demo.py`）：

```python
# 示例代码
from scipy.optimize import minimize_scalar

def f(x):
    return (x - 2) ** 2 + 1

# 1) 不给 method、不给 bounds -> 默认 brent
r1 = minimize_scalar(f)
print("默认:", r1.x, r1.fun)

# 2) 不给 method、给 bounds -> 默认 bounded
r2 = minimize_scalar(f, bounds=(0, 5))
print("带 bounds 默认:", r2.x, r2.fun)

# 3) 显式给 bounds 又指定 brent -> 应当报错
try:
    minimize_scalar(f, bounds=(0, 5), method='brent')
except ValueError as e:
    print("预期报错:", e)
```

**需要观察的现象**：前两次调用都能得到 `x≈2.0`、`fun≈1.0`；第三次抛出 `ValueError`，信息提示 `bounds` 与 `method='brent'` 不兼容。

**预期结果**：`r1.x` 与 `r2.x` 都非常接近 `2.0`；第三次进入 `except` 分支并打印错误信息。具体 `nfev`/`nit` 数值「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `method` 写成大写 `'BRENT'`，会报错吗？
**答案**：不会。因为 [_minimize.py:993](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_minimize.py#L993) 做了 `method.lower()`，方法名大小写不敏感。

**练习 2**：调用 `minimize_scalar(f, method='bounded')` 但**不**给 `bounds` 会怎样？
**答案**：抛出 `ValueError('The bounds parameter is mandatory for method bounded.')`，见 [_minimize.py:1024-1026](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_minimize.py#L1024-L1026)。

---

### 4.2 Brent 类：抛物线插值 + 黄金分割保护

#### 4.2.1 概念说明

Brent 方法（这里指 *Numerical Recipes* 风格的无导数 Brent 极小化）是 `minimize_scalar` 的**默认方法**，也是三者中精度与效率的折中最佳者。它的核心思想是一个「双保险」组合：

- **首选抛物线插值（inverse parabolic interpolation）**：在当前最佳点附近，用已知的三个点拟合一条抛物线，直接跳到这条抛物线的**顶点**。当函数形状接近二次时，这一步能带来**超线性收敛**，远快于黄金分割。
- **黄金分割兜底（golden section safeguard）**：抛物线顶点可能落到括号外、或者步长不再收缩，这时就退回最稳的黄金分割步，保证每一步都把括号缩小一个固定比例。

这种「能加速就加速、不能加速就稳妥」的组合，使 Brent 既快又稳健。这也是 `minimize_scalar` 文档里 [Notes](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_minimize.py#L907-L910) 那句「Uses inverse parabolic interpolation when possible to speed up convergence of the golden section method」的含义。

#### 4.2.2 核心流程

`Brent` 类的算法循环（在 `optimize` 方法里）每一步做：

1. 算容差 `tol1`、`tol2`，检查是否已收敛（当前最佳点是否接近括号中点）。
2. **尝试抛物线步**：用当前点 `x` 和两个历史点 `w`、`v` 构造抛物线，算出候选步长 `rat`。
3. **校验抛物线步是否可接受**：候选点必须在括号 `(a,b)` 内，且步长比上一次的步长更短（保证在收缩）。可接受→用抛物线步；否则→走黄金分割步 `rat = _cg * deltax`。
4. 计算新点函数值，更新 `x/w/v` 与括号端点 `a/b`，迭代计数加一。

抛物线插值的顶点公式（给定三点 \(x, w, v\)，函数值 \(f_x, f_w, f_v\)，\(x\) 为当前最佳点）为：

\[
u = x - \frac{(x-w)^2\,[f(x)-f(v)] - (x-v)^2\,[f(x)-f(w)]}{2\,\{(x-w)[f(x)-f(v)] - (x-v)[f(x)-f(w)]\}}.
\]

> 说明：源码用了一个**符号归一化**的等价写法（先令 `tmp2 = 2*((x-v)(fx-fw) - (x-w)(fx-fv))`，再 `if tmp2>0: p=-p; tmp2=abs(tmp2)`，最后 `rat = p/tmp2`），数学上与上式给出的步长等价；理解时抓住「拟合抛物线→跳到顶点」即可。

#### 4.2.3 源码精读

`Brent` 类的构造与黄金分割常数。`_cg` 就是前文说的 \(1-1/\varphi\approx 0.381966\)：

[_optimize.py:2441-2453](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L2441-L2453) 构造函数保存 `func/tol/maxiter`，置 `self._cg = 0.3819660`（黄金分割内分点比例）与 `self._mintol = 1.0e-11`（最小步长下限，防止步长被压到机器精度以下）。

括号信息获取——决定括号从哪来（无参自动搜索 / 两点 downhill / 三点直接用）：

[_optimize.py:2459-2495](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L2459-L2495) `get_bracket_info`：`brack` 为 `None` 时调 `bracket(func)` 自动找；长度 2 时以这两点为起点做 downhill 搜索；长度 3 时直接校验并使用，要求严格 `xa<xb<xc` 且 \(f(x_b)\) 同时小于两端。

核心算法循环——抛物线步的计算与可接受性判据：

[_optimize.py:2532-2563](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L2532-L2563) 先判断 `|deltax|<=tol1`（上一步足够小）→ 黄金分割；否则算抛物线候选 `p/tmp2`，并用三重判据 `(p>tmp2*(a-x)) and (p<tmp2*(b-x)) and (|p|<|0.5*tmp2*dx_temp|)` 决定是否采纳（在括号内 + 步长在收缩）；不满足则退回黄金分割 `rat=_cg*deltax`。

新点求值与括号/历史点更新：

[_optimize.py:2565-2598](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L2565-L2598) 强制步长不小于 `tol1`，求 `fu`；若 `fu>fx`（新点更差）则收紧括号端点并把 `u` 记入次优历史点 `w/v`；若 `fu<=fx`（新点更好）则把旧 `x` 降级为 `w`、`u` 升为新 `x`，并移动括号。

薄包装 `_minimize_scalar_brent`——把 `Brent` 类包成返回 `OptimizeResult` 的标准函数：

[_optimize.py:2700-2754](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L2700-L2754) 校验 `xtol>=0`，构造 `Brent` 对象，跑 `optimize()`，取结果，组装 `OptimizeResult(fun, x, nit, nfev, success, message)`。`success` 判据是「未超 `maxiter` 且无 NaN」。

> 公开的 [`brent`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L2620-L2697) 函数就是这个 `_minimize_scalar_brent` 的历史封装（返回裸 `x` 而非 `OptimizeResult`）。

#### 4.2.4 代码实践

**实践目标**：体会抛物线插值带来的加速——对同一个二次型函数，Brent 应明显比 golden 用的函数求值次数少。

**操作步骤**：

```python
# 示例代码
from scipy.optimize import minimize_scalar

def f(x):
    return (x - 2) ** 2 + 1   # 最小点 x=2, f=1

for m in ('brent', 'golden'):
    r = minimize_scalar(f, method=m)
    print(f"{m:7s} x={r.x:.10f} fun={r.fun:.3e} nfev={r.nfev} nit={r.nit}")
```

**需要观察的现象**：两种方法都收敛到 `x≈2`、`fun≈1`；但 `brent` 的 `nfev` 通常**显著小于** `golden` 的 `nfev`，因为抛物线插值让二次型函数几乎一步到位。

**预期结果**：`brent` 的 `nfev` 远小于 `golden`（对纯二次型，差距非常明显）。精确数值「待本地验证」，但方向（brent 更省）是确定的。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Brent` 要保留三个点 `x/w/v` 的历史，而不是只用最近的两个？
**答案**：抛物线插值需要**三个**点才能唯一确定一条抛物线并求其顶点。`x` 是当前最佳点，`w` 是上一次的最佳点，`v` 是再上一次的，三者一起支撑公式 [_optimize.py:2539-2542](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L2539-L2542)。

**练习 2**：抛物线步被拒绝、退回黄金分割的三个条件中，`(|p| < |0.5*tmp2*dx_temp|)` 这一条在防什么？
**答案**：它要求当前抛物线步长小于「上一步步长的一半」，防止步长不收缩、反复横跳；只有当步长确实在持续缩小时（超线性收敛的迹象），才相信抛物线，否则用稳妥的黄金分割。

---

### 4.3 有界 Brent（bounded）：Forsythe–Malcolm–Moler 算法

#### 4.3.1 概念说明

当变量有上下界 \( x \in [x_1, x_2] \) 时，用 `method='bounded'`。它实现的是经典的 **Forsythe–Malcolm–Moler（FMM）/ Brent `fmin`** 算法：在固定区间内做带保护的 Brent 搜索。它和 4.2 的「无界 Brent」**不是同一段代码**——二者思想相近（都用抛物线 + 黄金分割），但实现各自独立，且 `bounded` 版本**永远把点限制在 `[a,b]` 内**，从区间的黄金分割内分点出发。

公开入口 [`fminbound`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L2195-L2286) 就是 `_minimize_scalar_bounded` 的薄封装。

#### 4.3.2 核心流程

1. **校验边界**：`bounds` 必须是两个**有限**标量，且下界 ≤ 上界。
2. **初始化**：把区间端点记为 `a, b`；在区间内用黄金分割比例 `golden_mean` 放一个内分点 `fulc` 作为「远点」，并在此求首次函数值。
3. **主循环**：每步先尝试**抛物线拟合**（用近点 `nfc`、远点 `fulc`、当前点 `xf`），若抛物线顶点落在 `(a,b)` 内且步长在收缩则采纳，否则走黄金分割步；新点永远由 `si * max(|rat|, tol1)` 保证不越界、不被压过容差。
4. **收敛判据**：当 `|xf - xm| <= tol2 - 0.5*(b-a)`（`xm` 是区间中点）时停止，即当前最佳点已贴近中点、区间已足够窄。

这里 `golden_mean = 0.5*(3.0 - sqrt(5.0)) ≈ 0.381966`，正是 \(1-1/\varphi\)。

#### 4.3.3 源码精读

边界校验：

[_optimize.py:2314-2323](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L2314-L2323) 要求 `len(bounds)==2`、两端都是有限标量（用 `is_finite_scalar`）、且 `x1<=x2`，否则抛 `ValueError`。

初始化与黄金分割内分点：

[_optimize.py:2329-2344](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L2329-L2344) 令 `golden_mean=0.5*(3-sqrt(5))`，远点 `fulc=a+golden_mean*(b-a)`，在 `xf=fulc` 处求值；容差 `tol1=sqrt(eps)*|xf|+xatol/3`，`tol2=2*tol1`。

主循环收敛判据与抛物线/黄金分割抉择：

[_optimize.py:2351-2385](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L2351-L2385) 循环条件 `|xf-xm| > tol2-0.5*(b-a)`；若 `|e|>tol1` 则尝试抛物线步 `rat=p/q`，并用 `(a-xf)<p/q<(b-xf)` 限制其落在区间内，否则置 `golden=1` 走 `rat=golden_mean*e`。

新点更新——把 `xf/nfc/fulc` 当作「近点/中点/远点」三件套维护：

[_optimize.py:2387-2416](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L2387-L2416) 新点 `x` 强制 `max(|rat|, tol1)`；若 `fu<=fx` 则新点更好，旧 `xf` 降为近点 `nfc`、`x` 升为新 `xf` 并移动区间端点；否则只收紧包含新点的端点，并按需更新 `nfc/fulc`。

结果组装——注意 `nfev == nit == num`（有界法每次迭代恰好一次求值）：

[_optimize.py:2429-2434](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L2429-L2434) 返回 `OptimizeResult(fun, status, success, message, x=xf, nfev=num, nit=num)`，`status` 编码 0=成功、1=达函数调用上限、2=出现 NaN。

#### 4.3.4 代码实践

**实践目标**：体会「有界搜索把极小点夹在给定区间内」——当真实极小点在区间外时，结果会贴到边界上。

**操作步骤**：

```python
# 示例代码
from scipy.optimize import minimize_scalar

def f(x):
    return (x - 2) ** 2 + 1   # 真实极小在 x=2

# 区间把极小点包含在内
r1 = minimize_scalar(f, bounds=(0, 5), method='bounded')
print("包含极小:", r1.x, r1.fun)

# 区间把极小点排除在外（极小在 2，但区间是 [3,5]）-> 结果贴到下界 3
r2 = minimize_scalar(f, bounds=(3, 5), method='bounded')
print("排除极小:", r2.x, r2.fun)
```

**需要观察的现象**：第一次 `r1.x≈2`；第二次 `r2.x≈3`（贴在下界），`r2.fun` 是 `f(3)=2`，因为区间内最小值在边界。

**预期结果**：如上。具体小数位「待本地验证」，但 `r2.x` 会非常接近下界 `3.0`。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `bounded` 不需要、也不接受 `bracket` 参数？
**答案**：用户给的 `bounds=(x1,x2)` 本身就是一个合法的初始括号（区间内必有极小或边界极小），所以无需再用 `bracket()` 去搜索。这也是 [_minimize.py:1023-1027](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_minimize.py#L1023-L1027) 中 `bounded` 分支**不**包 `_recover_from_bracket_error` 的原因。

**练习 2**：`maxiter` 和 `nfev` 在 `bounded` 方法里是什么关系？
**答案**：相等。代码里 `maxfun = maxiter`，且每次循环恰好一次 `func` 调用（见 [_optimize.py:2313](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L2313) 与 [_optimize.py:2418-2420](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L2418-L2420)），结果里 `nfev=nit=num`。

---

### 4.4 黄金分割法（golden）：最朴素的一维搜索

#### 4.4.1 概念说明

黄金分割法是三者中最简单、也最慢的方法。它完全不用抛物线插值，只用一个固定策略：每次在括号内按黄金比例取一个内点，比较函数值后**丢弃一端**，把括号缩小为原来的 \(1/\varphi \approx 0.618\)。它是一维版「二分法」的类比——二分法每次把区间减半，黄金分割每次把区间乘以 0.618。

文档里说 [It is usually preferable to use the Brent method](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_minimize.py#L912-L915)：通常应优先用 Brent。`golden` 主要作为对照和保底手段存在。

#### 4.4.2 核心流程

1. **获取括号**：和 Brent 一样，先靠 `bracket()` 或用户给的括号得到 \(x_a<x_b<x_c\)。
2. **在内侧布两个黄金分割点** `x1`、`x2`，分别求值。
3. **循环收缩**：比较 `f1`、`f2`，丢掉离较大值更近的那一端，并在新括号内补一个新的黄金分割点，如此反复。
4. **收敛判据**：当括号宽度 `|x3-x0| <= xtol*(|x1|+|x2|)` 时停止。

每步把括号乘以常数 \( \frac{1}{\varphi} \approx 0.618 \)，因此是**线性收敛**（速率约 0.618/步），比抛物线插值的超线性慢。

#### 4.4.3 源码精读

获取括号（与 Brent.get_bracket_info 同源逻辑）：

[_optimize.py:2857-2881](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L2857-L2881) 同样支持 `None`/两点/三点三种括号输入，三点时要严格有序且中点最低，否则抛 `ValueError`。

黄金分割常数与初始内点：

[_optimize.py:2883-2895](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L2883-L2895) `_gR=0.61803399`（\(1/\varphi\)），`_gC=1-_gR`（≈0.382）；在较宽的一侧布内点 `x1`/`x2`，分别求 `f1`/`f2`。

收缩循环——比较 f1/f2 决定丢哪端：

[_optimize.py:2902-2925](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L2902-L2925) 若 `|x3-x0|<=xtol*(|x1|+|x2|)` 则收敛退出；若 `f2<f1` 则把括号左端 `x0<-x1` 并在右侧补新点 `_gR*x1+_gC*x3`，否则把右端 `x3<-x2` 并在左侧补新点；每轮一次新求值。

> 公开的 [`golden`](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L2757-L2831) 函数是 `_minimize_scalar_golden` 的薄封装。注意它的 `maxiter` 默认 **5000**（比 brent 的 500 大得多），因为线性收敛需要更多步。

#### 4.4.4 代码实践

**实践目标**：定量感受黄金分割的线性收敛——它需要的 `nfev` 会显著多于 Brent。

**操作步骤**：复用 4.2.4 的脚本即可，重点对比 `golden` 与 `brent` 的 `nfev`。若想更直观，可把目标函数换成更「平」的：

```python
# 示例代码
from scipy.optimize import minimize_scalar

def f(x):
    return (x - 2) ** 2 + 1

for m in ('brent', 'golden'):
    r = minimize_scalar(f, method=m)
    print(f"{m:7s} x={r.x:.10f} fun={r.fun:.3e} nfev={r.nfev} nit={r.nit}")
```

**需要观察的现象**：`golden` 的 `nfev`/`nit` 明显大于 `brent`；两者精度都达到 `fun≈1`。

**预期结果**：`golden.nfev > brent.nfev`，差距随函数越接近二次型而越大。精确数值「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：黄金分割每步把括号缩小到原来的多少？
**答案**：约 \(1/\varphi \approx 0.618\)，对应源码常量 `_gR=0.61803399`，见 [_optimize.py:2883](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L2883)。

**练习 2**：既然 Brent 也内置了黄金分割兜底，为什么还要单独保留 `golden` 方法？
**答案**：作为最简单可靠的参照基线；当函数**极不光滑或带噪声**、抛物线插值反而误导时，纯黄金分割更稳健；同时它也是教学和理解「括号收缩」思想的最小例子。

---

### 4.5 括号构造与回退：bracket / BracketError / _recover_from_bracket_error

#### 4.5.1 概念说明

`brent` 和 `golden` 都依赖一个合法括号。当用户**不**提供括号时，`bracket()` 会自动从默认两点 `xa=0, xb=1` 出发，沿「下坡」方向搜索，试图找到三点括号。但有些函数天生括不住（例如**常数函数**没有唯一下坡方向，或**单调函数**一路下坡到无穷）。历史上 `bracket` 不检查结果合法性，会把「垃圾括号」悄悄传给上层，导致 `brent`/`golden` 返回错误结果而无警告（gh-14858）。

修复方案分两层：

- **`bracket()` 现在会校验**：若三个有效性条件不满足，抛 `BracketError`，并把已算出的信息挂在异常对象的 `.data` 上。
- **`minimize_scalar` 用 `_recover_from_bracket_error` 包装** `brent`/`golden`：捕获 `BracketError`，**不再抛错**，而是返回一个 `success=False` 的 `OptimizeResult`（取三点中函数值最小的那个作为 `x`，`nit=0`），让用户拿到「最好的退化结果」而不是崩溃。

#### 4.5.2 核心流程

`bracket()` 的搜索策略：

1. 在 `xa`、`xb` 两点求值；若 `fa < fb` 则交换，保证 `fa > fb`（即 `xb` 是下坡方向）。
2. 沿下坡方向按黄金比 `_gold=1.618034` 外推，得第三点 `xc`；只要 `fc < fb` 就继续外推。
3. 外推途中尝试用抛物线插值加速（类似一维线搜索的 «Brent» 外推），并受 `grow_limit` 约束。
4. 退出循环后，校验三个**有效性条件**（见下）；任一不满足则抛 `BracketError(.data=...)`。

三个有效性条件（ [_optimize.py:3099-3102](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L3099-L3102) ）：

\[
\text{cond1: } (f_b<f_c \land f_b\le f_a)\ \lor\ (f_b<f_a \land f_b\le f_c) \quad(\text{中点是最低之一})
\]

\[
\text{cond2: } x_a<x_b<x_c \ \lor\ x_c<x_b<x_a \quad(\text{三点严格有序})
\]

\[
\text{cond3: } x_a,x_b,x_c \text{ 均有限}
\]

#### 4.5.3 源码精读

`bracket()` 主循环——下坡外推 + 抛物线加速：

[_optimize.py:3031-3097](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L3031-L3097) 先保证 `fa>fb`，再用 `_gold=1.618034` 外推 `xc`，循环 `while(fc<fb)` 内用抛物线插值候选 `w` 并受 `wlim=grow_limit` 约束；命中即跳出。

有效性校验与抛错：

[_optimize.py:3099-3108](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L3099-L3108) 计算 `cond1/cond2/cond3`，若不同时满足则构造 `BracketError(msg)`，把 `(xa,xb,xc,fa,fb,fc,funcalls)` 挂到 `e.data` 上后抛出。

`BracketError` 本身只是 `RuntimeError` 的别名：

[_optimize.py:3113-3114](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L3113-L3114) `class BracketError(RuntimeError): pass`——特意定义一个独立类型，方便上层精确捕获，而不会误伤别的 `RuntimeError`。

回退包装——把异常翻译成失败的 `OptimizeResult`：

[_optimize.py:3117-3148](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L3117-L3148) `_recover_from_bracket_error` 先尝试调 `solver`；若捕到 `BracketError`，则从 `e.data` 取出三点，若含 NaN 则 `x=fun=nan`，否则取 `argmin(fa,fb,fc)` 对应的点作为 `x`，返回 `OptimizeResult(fun, nfev=funcalls, x, nit=0, success=False, message=msg)`。

> 这段注释（ [_optimize.py:3118-3134](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L3118-L3134) ）解释了为什么用「异常携带数据 + 上层拦截」这种略显取巧的手法：要让 `bracket`、`brent`、`golden` 在括号非法时**报错**，又要让 `minimize_scalar` **不报错而返回 success=False**，传统做法需要层层改返回值、改动面太大；用异常对象挂载信息、在最外层一次性拦截，是最小侵入的方案。

#### 4.5.4 代码实践

**实践目标**：观察 `BracketError` 的回退行为——用常数函数（无唯一下坡方向，必然括不住）触发它，看 `minimize_scalar` 如何优雅返回 `success=False`。

**操作步骤**：

```python
# 示例代码
from scipy.optimize import minimize_scalar

# 常数函数：三点函数值相等，bracket 无法满足 cond1，必然抛 BracketError
r = minimize_scalar(lambda x: 0.0)
print("success:", r.success)
print("message:", r.message)
print("x:", r.x, " fun:", r.fun, " nfev:", r.nfev, " nit:", r.nit)
```

**需要观察的现象**：调用**不会抛异常**；`r.success` 为 `False`；`r.message` 提示 "The algorithm terminated without finding a valid bracket..."；`r.nit` 为 `0`（回退的标志——一次迭代都没真正做）；`r.nfev` 为 `3`（`bracket()` 至少求了三个点）。

**预期结果**：如上。`x` 取三点中函数值最小者（常数函数三点都是 0，取第一个）。精确取值「待本地验证」，但 `success=False`、`nit=0`、`nfev=3` 是确定的回退签名。

> 进阶观察：对**单调函数**（如 `lambda x: x`，无下界极小），`bracket()` 会一路下坡外推，最终可能因达到 `maxiter` 而抛 `RuntimeError`（注意：这不是 `BracketError`，不会被 `_recover_from_bracket_error` 捕获，会直接向上抛出）。你可以本地试一试，观察报错类型的差异——这正好说明回退包装**只针对括号构造阶段的 `BracketError`**。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `_recover_from_bracket_error` 在回退时取 `nit=0` 而不是某个正数？
**答案**：因为它根本没能开始真正的极小化迭代——`BracketError` 发生在「获取括号」阶段，主循环一次都没跑。`nit=0` 准确反映了这一点，见 [_optimize.py:3146-3147](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L3146-L3147)。

**练习 2**：如果想让 `brent`/`golden` 更容易成功括住，用户能做什么？
**答案**：显式提供一个**合法的括号**（三点且中点最低），或一个**好的两点起点** `bracket=(xa, xb)`，让 `bracket()` 从更接近极小的位置出发搜索；也可调大 `bracket()` 的 `maxiter`/`grow_limit`（见 [_optimize.py:2954](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L2954) 的参数）。实在括不住时，文档建议改用多元 `minimize` 或全局优化。

---

## 5. 综合实践

把本讲的三种方法和回退机制串起来，完成下面这个对比实验。

**任务**：对目标函数 \( f(x)=(x-2)^2+1 \)（真实极小 \(x^\*=2, f=1\)），系统对比 `brent`、`golden`、`bounded` 三种方法，并演示一次括号回退。

```python
# 示例代码
from scipy.optimize import minimize_scalar
import math

def f(x):
    return (x - 2) ** 2 + 1

print("== 三种方法对比 ==")
for m, kw in [("brent", {}),
              ("golden", {}),
              ("bounded", {"bounds": (0, 5)})]:
    r = minimize_scalar(f, method=m, **kw)
    err = abs(r.x - 2)
    print(f"{m:8s} x={r.x:.8f}  err={err:.2e}  fun={r.fun:.3e}  "
          f"nfev={r.nfev}  nit={r.nit}  success={r.success}")

print("\n== 容差对 brent 的影响 ==")
for tol in (1e-3, 1e-6, 1e-10):
    r = minimize_scalar(f, method='brent', options={'xtol': tol})
    print(f"xtol={tol:.0e}  x={r.x:.10f}  err={abs(r.x-2):.2e}  nfev={r.nfev}")

print("\n== 括号回退（常数函数）==")
r = minimize_scalar(lambda x: 0.0)
print(f"success={r.success}, nit={r.nit}, nfev={r.nfev}")
print(f"message={r.message}")
```

**你要回答的问题**（把结论写在注释里）：

1. 在精度相近的前提下，三种方法谁最省 `nfev`？谁最费？（预期：brent 最省，golden 最费。）
2. `xtol` 收紧 1000 倍（从 1e-6 到 1e-10），`nfev` 大约增加多少？这是否符合「Brent 收敛快」的直觉？
3. 回退结果的 `nit` 与 `nfev` 是否符合 4.5 节分析的「`nit=0`、`nfev=3`」签名？

**预期结果**：三方法都收敛到 `x≈2`；`brent` 的 `nfev` 最小，`golden` 最大；括号回退给出 `success=False, nit=0, nfev=3`。精确数值「待本地验证」，但上述定性结论是确定的。

---

## 6. 本讲小结

- `minimize_scalar`（[_minimize.py:831](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_minimize.py#L831)）是一维版调度入口，自身不含算法，按 `method` 分发到 `brent`/`bounded`/`golden`；默认规则是「有 bounds→bounded，否则→brent」，且 `bounds` 与 `brent`/`golden` 互斥。
- **Brent 方法**（`Brent` 类，[_optimize.py:2439](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L2439)）= 抛物线插值（超线性加速）+ 黄金分割保护（稳健兜底），是默认且通常最优的选择。
- **有界 Brent**（`_minimize_scalar_bounded`，[_optimize.py:2289](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L2289)）实现 FMM/Brent `fmin`，把搜索严格限制在用户给的 `[x1,x2]` 内，公开入口是 `fminbound`。
- **黄金分割**（`_minimize_scalar_golden`，[_optimize.py:2834](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L2834)）最朴素，每步把括号乘以 0.618，线性收敛，通常作为对照/保底。
- **括号（bracket）** 是一维搜索的地基；`bracket()`（[_optimize.py:2954](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L2954)）自动构造，失败时抛 `BracketError`。
- `_recover_from_bracket_error`（[_optimize.py:3117](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L3117)）用「异常携带数据 + 外层拦截」把括号失败翻译成 `success=False` 的结果，避免静默返回垃圾值（gh-14858）。
- 三种方法都是**无导数**的，只靠函数求值；黄金比例常数族（0.382/0.618/1.618）贯穿全部代码。

## 7. 下一步学习建议

本讲建立了一维**最小化**的完整图景。单元 2 的后续两讲自然延伸到一维**求根**：

- **下一讲 [u2-l2](u2-l2-root-scalar-interface.md)「标量求根的统一接口 root_scalar」**：从「求最小」转向「求零点」。`root_scalar` 同样是一个按条件分发的统一入口，但它的选择依据是「有没有括号 / 有没有导数」，覆盖 bisect/brentq/ridder/toms748/newton 等 8 种方法。你会发现 `brentq`、`brenth` 等求根算法和本讲的 Brent 极小化在底层思想上一脉相承（都是插值 + 保护）。
- **再下一讲 [u2-l3](u2-l3-zeros-algorithms.md)「一维求根算法实现」**：深入 `_zeros_py.py`，看 bisect/newton/brentq/ridder/toms748 各自的迭代细节。
- 如果你对**多元**无导数方法感兴趣，可以跳到单元 4 的 [u4-l2](u4-l2-derivative-free.md)「Nelder-Mead 与 Powell」——它们是本讲一维思想在多维的推广（Powell 法本质就是沿一系列方向反复做一维最小化，复用了本讲的线搜索思想）。
- 建议继续精读的源码：`_optimize.py` 中 `Brent.optimize`（[_optimize.py:2497](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L2497)）和 `bracket`（[_optimize.py:2954](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_optimize.py#L2954)）是本讲最值得逐行读懂的两段代码。
