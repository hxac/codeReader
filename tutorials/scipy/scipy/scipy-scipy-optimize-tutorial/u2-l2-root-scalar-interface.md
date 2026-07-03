# 标量求根的统一接口 root_scalar

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 `scipy.optimize.root_scalar` 要解决的是哪一类问题（单变量标量函数 \(f(x)=0\) 的根），以及它和上一讲的 `minimize_scalar`（求最小值）的根本区别。
- 看懂 `root_scalar` 的完整参数列表，并掌握它如何根据你**是否提供 `bracket` / `fprime` / `fprime2`** 自动在 8 种方法里挑选「当下最优」的一种。
- 记住 `ROOT_SCALAR_METHODS` 这 8 个方法名，并能对着 `__init__.py` 里的「收敛保证 / 收敛阶」对照表，判断哪种方法**保证收敛**、哪种**收敛更快**。
- 理解 `MemoizeDer` 如何用一个极简的单值缓存，让「同时返回函数值与导数」的函数在一次求值里同时喂给 `newton` / `halley`，避免重复计算。

> 本讲承接 [u1-l3](u1-l3-minimize-dispatcher.md) 讲过的「统一调度入口」思想：`root_scalar` 本身**不做求根**，它只负责**选方法 + 把参数翻译成底层求解器要的形状**，真正的算法在 `_zeros_py.py` 里（那是下一讲 [u2-l3](u2-l3-zeros-algorithms.md) 的内容）。本讲只聚焦「接口与调度」这一层。

## 2. 前置知识

### 2.1 什么是「求根」

给定一个一元函数 \(f(x)\)，**求根（root-finding）**就是找满足

\[
f(x^\*) = 0
\]

的 \(x^\*\)。这和上一讲的「求最小值」不同：

| | 最小化（minimize_scalar） | 求根（root_scalar） |
|---|---|---|
| 目标 | 找 \(x^\*\) 使 \(f(x^\*)\) 最小 | 找 \(x^\*\) 使 \(f(x^\*)=0\) |
| 典型判据 | 导数 ≈ 0 | 函数值 ≈ 0 |
| 是否需要变号 | 不需要 | **很多方法要求区间两端函数值异号** |

### 2.2 两类求根策略

后续你会看到 8 种方法，但它们只分两大阵营：

1. **括号法（bracketing methods）**：你先给一个区间 \([a,b]\)，并保证 \(f(a)\) 与 \(f(b)\)**异号**（即 \(f(a)\cdot f(b)<0\)）。由连续函数的**介值定理**，区间内必有根。这类方法（`bisect`/`brentq`/`brenth`/`ridder`/`toms748`）**保证收敛**，但只能用在实数轴上。
2. **点法 / 导数法（point methods）**：你只给一个或两个初始点，不需要变号。靠**导数信息**（`newton`/`secant`/`halley`）迭代逼近。收敛可能很快，但**不保证收敛**，且可用于**复数**域。

> 记住这张「二分法」图景：你手里有什么样的「输入材料」，决定了你能用哪一营的方法——这正是 `root_scalar` 自动选法的核心依据。

### 2.3 收敛阶（convergence order）是什么

设第 \(n\) 步误差为 \(e_n=|x_n-x^\*|\)。若存在常数 \(C\) 使

\[
e_{n+1} \approx C\, e_n^{\,p}
\]

则称该方法具有**收敛阶** \(p\)。\(p=1\) 为线性收敛（最慢），\(p=2\) 为平方收敛，\(p\) 越大收敛越快。本讲会在第 4.3 节给出 SciPy 文档里的完整对照表。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|---|---|---|
| [_root_scalar.py](_root_scalar.py) | `root_scalar` 的全部实现：参数解析、方法自动选择、分发到底层 | 整篇核心 |
| [__init__.py](__init__.py) | 子包文档；含求根方法的**收敛保证/收敛阶对照表** | 第 4.3 节 |
| [_zeros_py.py](_zeros_py.py) | 底层算法实现 + `RootResults` 结果类 | 第 4.1、4.4 节引用其签名 |

> 心智模型（来自 [u1-l2](u1-l2-directory-build-and-backends.md)）：`root_scalar` 是**纯 Python 调度层**，它通过 `getattr(optzeros, ...)` 把请求转给同模块的 `_zeros_py.py`（别名 `optzeros`）。`_zeros_py.py` 里的 `bisect`/`brentq`/`newton`/`toms748` 才是真正干活的算法——这些会在下一讲精读。

## 4. 核心概念与源码讲解

### 4.1 root_scalar 函数：统一入口与方法分发

#### 4.1.1 概念说明

`root_scalar` 是一个**调度器（dispatcher）**，和 `minimize` 一样「自己不求解，只负责派单」。它做三件事：

1. **解析参数**：把 `f`、`bracket`、`x0`、`fprime`、`fprime2`、容差等整理好；
2. **选方法**：如果你没显式给 `method`，它根据你提供的「材料」自动挑一个；
3. **派发**：把翻译好的参数传给 `_zeros_py.py` 里对应的底层函数，再把结果包成 `RootResults` 返回。

#### 4.1.2 核心流程

```text
root_scalar(f, method=None, bracket=None, fprime=None, fprime2=None, x0=None, x1=None, ...)
        │
        ├─ ① 若 fprime/fprime2 是 True(布尔)，用 MemoizeDer 包装 f
        │
        ├─ ② method 为空？→ 按「bracket > x0+导数」优先级自动选 (见 4.1.3)
        │
        ├─ ③ meth = method.lower()；halley/secant 都映射到底层 newton
        │     methodc = getattr(optzeros, 映射后的名字)
        │
        ├─ ④ 按 meth 分四类调用底层：
        │     • 括号法(bisect/ridder/brentq/brenth/toms748) → methodc(f, a, b, ...)
        │     • secant  → methodc(f, x0, x1=x1, fprime=None, ...)
        │     • newton  → 若无 fprime 则用有限差分补一个，再 methodc(f, x0, fprime=...)
        │     • halley  → 强制要求 fprime 与 fprime2，methodc(f, x0, fprime=, fprime2=)
        │
        └─ ⑤ 若启用了 MemoizeDer，用真实求值次数覆盖 sol.function_calls
```

#### 4.1.3 源码精读

**函数签名**——这是整个接口的「契约」，注意 `method` 默认是 `None`（即「自动选」）：

[_root_scalar.py:62-66](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_root_scalar.py#L62-L66) —— `root_scalar` 的完整参数：`f`、`args`、`method`、`bracket`、`fprime`/`fprime2`、`x0`/`x1`、`xtol`/`rtol`/`maxiter`、`options`。

**自动选方法**——这是「括号 vs 导数」决策的核心（也是本讲最重要的代码块）：

[_root_scalar.py:252-269](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_root_scalar.py#L252-L269) —— 当 `method` 为空时的优先级链：

```python
if not method:
    if bracket is not None:          # ① 有括号 → brentq（默认最好的括号法）
        method = 'brentq'
    elif x0 is not None:             # ② 有初值才考虑点法
        if fprime:
            if fprime2:              #    一阶+二阶导 → halley（三阶收敛）
                method = 'halley'
            else:                    #    仅一阶导 → newton（二阶收敛）
                method = 'newton'
        elif x1 is not None:         # ③ 无导数但有两个点 → secant
            method = 'secant'
        else:                        # ④ 啥导数都没有 → newton(内部退化为 secant)
            method = 'newton'
if not method:
    raise ValueError('Unable to select a solver as neither bracket '
                     'nor starting point provided.')
```

> 读懂这段就抓住了本讲的灵魂：**「有 bracket 优先 brentq；否则看导数有多少，导数越多收敛阶越高」**。注意第 ④ 支——`method='newton'` 但没有 `fprime`，底层 `newton` 会自动退化成 **secant 法**（见 [_zeros_py.py:131-134](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_zeros_py.py#L131-L134) 的说明）。

**方法名归一化与底层映射**——`root_scalar` 暴露 `halley`/`secant` 两个「别名方法」，但底层只有 `newton` 一个函数：

[_root_scalar.py:271-277](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_root_scalar.py#L271-L277) —— `meth = method.lower()`（大小写不敏感），`map2underlying = {'halley': 'newton', 'secant': 'newton'}` 把两个别名都指向 `optzeros.newton`，括号法则各指向同名函数。

**容差参数的翻译**——这是初学者最容易踩坑的地方。底层 `newton` 用参数名 `tol`，而括号法用 `xtol`/`rtol`，所以 `root_scalar` 在分发前要改名：

[_root_scalar.py:299-337](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_root_scalar.py#L299-L337) —— 对 `secant`/`newton`/`halley` 都执行 `kwargs['tol'] = kwargs.pop('xtol')`；并且 `newton` 在缺 `fprime` 时**用 `approx_derivative` 的 2-point 有限差分自动补一个导数**（[_root_scalar.py:306-327](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_root_scalar.py#L306-L327)），而 `halley` 则**强制要求** `fprime` 与 `fprime2`，缺一不可。

**返回类型**——结果统一包成 `RootResults`：

[_zeros_py.py:35-77](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_zeros_py.py#L35-L77) —— `RootResults` 携带 `root`、`iterations`、`function_calls`、`converged`、`flag`、`method` 字段，其中 `converged` 由 `flag == _ECONVERGED`（即 0）推导。

#### 4.1.4 代码实践

**目标**：观察「括号法」与「点法」所需输入的差异，并验证返回对象。

```python
# 示例代码
import numpy as np
from scipy.optimize import root_scalar

# f(x) = cos(x) - x，根约在 0.7390851...（著名的 Dottie 数）
f = lambda x: np.cos(x) - x

# ① 括号法 brentq：只需给一个异号区间
sol_b = root_scalar(f, bracket=[0, 1], method='brentq')
print("brentq :", sol_b.root, "iter =", sol_b.iterations,
      "feval =", sol_b.function_calls, "conv =", sol_b.converged)

# ② 点法 newton：需要初值 x0 和导数 fprime
fp = lambda x: -np.sin(x) - 1
sol_n = root_scalar(f, x0=0.0, fprime=fp, method='newton')
print("newton :", sol_n.root, "iter =", sol_n.iterations,
      "feval =", sol_n.function_calls, "conv =", sol_n.converged)
```

**操作步骤**：把上面脚本存为 `rs_demo.py` 并运行 `python rs_demo.py`。

**预期结果**：两种方法都收敛到 `0.7390851332151607`。注意：

- `brentq` 只需要你给出**异号区间** `[0,1]`（`cos(0)-0=1>0`，`cos(1)-1≈-0.4597<0`，确实异号），**保证收敛**。
- `newton` 需要你**额外提供导数** `fprime`，迭代步数通常更少，但**不保证收敛**（取决于初值好坏）。

**需要观察的现象**：两者的 `iterations` 与 `function_calls` 数值不同——括号法的 `function_calls` 通常比 `iterations` 多 1（多算一次端点）；`newton` 的 `function_calls` 约为 `iterations` 的 2 倍（每步要算一次函数值 + 一次导数）。精确的 `nit`/`nfev` **待本地验证**。

> 你还可以试一下 `method` 留空，让它自动选：`root_scalar(f, bracket=[0,1])` 应自动落到 `brentq`；`root_scalar(f, x0=0.0, fprime=fp)` 应自动落到 `newton`。

#### 4.1.5 小练习与答案

**练习 1**：调用 `root_scalar(f, x0=0.0)`（不给 `method`、不给 `bracket`、不给 `fprime`），它最终会落到哪个底层方法？

**答案**：根据 [_root_scalar.py:263-266](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_root_scalar.py#L263-L266)，`method` 被选为 `'newton'`；但因为没有 `fprime`，分发分支会进一步用有限差分补一个 `fprime`（[_root_scalar.py:309-322](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_root_scalar.py#L309-L322)），底层 `newton` 拿到导数后走标准牛顿步。

**练习 2**：调用 `root_scalar(f)`（什么都不给）会怎样？

**答案**：`bracket` 为 `None`、`x0` 为 `None`，`method` 始终为空，于是抛出 `ValueError('Unable to select a solver as neither bracket nor starting point provided.')`（[_root_scalar.py:267-269](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_root_scalar.py#L267-L269)）。

---

### 4.2 ROOT_SCALAR_METHODS：八种方法与「括号 vs 导数」选法表

#### 4.2.1 概念说明

`ROOT_SCALAR_METHODS` 是一个**字符串常量列表**，列出 `root_scalar` 支持的全部 8 种方法。它的作用有三：

1. 作为「合法方法名」的**事实清单**（文档与代码共用）；
2. 帮你一眼看清「5 个括号法 + 3 个点法」的阵营划分；
3. 给后续的校验逻辑提供参照（虽然 `root_scalar` 主要靠 `getattr` 探测，但这个列表是权威命名）。

#### 4.2.2 核心流程

8 种方法按输入需求可整理成下表（`x`=必需，`o`=可选，空白=不适用）。这张表直接抄自 `root_scalar` 的 docstring，是判断「我能用哪种方法」的最快工具：

| method | f | args | bracket | x0 | x1 | fprime | fprime2 | 所属阵营 |
|---|:-:|:-:|:-:|:-:|:-:|:-:|:-:|---|
| `bisect`  | x | o | **x** |  |  |  |  | 括号 |
| `brentq`  | x | o | **x** |  |  |  |  | 括号 |
| `brenth`  | x | o | **x** |  |  |  |  | 括号 |
| `ridder`  | x | o | **x** |  |  |  |  | 括号 |
| `toms748` | x | o | **x** |  |  |  |  | 括号 |
| `secant`  | x | o |  | **x** | o |  |  | 点 |
| `newton`  | x | o |  | **x** |  | o |  | 点 |
| `halley`  | x | o |  | **x** |  | **x** | **x** | 点 |

**判读规则**：

- 只要你给了 `bracket` → 只能从**前 5 个括号法**里选；它们的收敛都**有保证**。
- 只要你想用**后 3 个点法** → 必须给 `x0`；导数给得越多（无→`secant`，一阶→`newton`，一阶+二阶→`halley`），潜在收敛阶越高。

#### 4.2.3 源码精读

**常量定义**：

[_root_scalar.py:16-17](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_root_scalar.py#L16-L17) —— `ROOT_SCALAR_METHODS = ['bisect', 'brentq', 'brenth', 'ridder', 'toms748', 'newton', 'secant', 'halley']`。注意顺序：前 5 个是括号法，后 3 个是点法（恰好与上一节的阵营划分一致）。

**括号法的输入校验**——括号法在分发前会强制检查 `bracket` 是否存在：

[_root_scalar.py:279-283](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_root_scalar.py#L279-L283) —— 对 `['bisect', 'ridder', 'brentq', 'brenth', 'toms748']` 任一方法，若 `bracket` 不是 list/tuple/ndarray 就抛 `ValueError(f'Bracket needed for {method}')`，随后取 `a, b = bracket[:2]` 作为区间端点。

**点法的输入校验**——`x0` 是点法的硬性要求：

[_root_scalar.py:299-337](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_root_scalar.py#L299-L337) —— `secant`/`newton`/`halley` 分支开头都有 `if x0 is None: raise ValueError(...)`；此外 `halley` 还额外要求 `fprime` 与 `fprime2` 必须同时给出。

> docstring 里那张大表（[ _root_scalar.py:150-168](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_root_scalar.py#L150-L168)）与本节的精简表内容一致，是权威来源；`show_options('root_scalar', 'brentq')` 等查阅也读自这里。

#### 4.2.4 代码实践

**目标**：用代码探测哪些参数组合合法、哪些会被拒绝。

```python
# 示例代码
from scipy.optimize import root_scalar
import numpy as np

f = lambda x: np.cos(x) - x

# 合法：括号法 + bracket
print(root_scalar(f, bracket=[0, 1], method='brentq').converged)

# 非法：括号法但没给 bracket → 抛 ValueError("Bracket needed for bisect")
try:
    root_scalar(f, x0=0.5, method='bisect')
except ValueError as e:
    print("被拒绝:", e)

# 非法：halley 但缺 fprime2 → 抛 ValueError
try:
    root_scalar(f, x0=0.0, fprime=lambda x: -np.sin(x)-1, method='halley')
except ValueError as e:
    print("被拒绝:", e)
```

**预期结果**：第一行打印 `True`；后两行分别打印「Bracket needed for bisect」「fprime2 must be specified for halley」（见 [_root_scalar.py:333-334](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_root_scalar.py#L333-L334)）。错误信息的**确切措辞待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：你只有函数值（没有导数，也无法保证区间端点异号），但能给出两个不同的初始点 `x0`、`x1`。该用哪种方法？

**答案**：`secant`。它只需要 `x0`（必需）和 `x1`（可选的第二个点），不需要导数，也不需要 bracket。

**练习 2**：为什么 `halley` 要求 `fprime` 和 `fprime2` 同时给出，而 `newton` 只要求 `fprime`？

**答案**：Halley 法用到**二阶导数**来加速牛顿步（收敛阶可达 3），所以底层 `newton` 调用时必须传入 `fprime2`（[_root_scalar.py:328-337](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_root_scalar.py#L328-L337)）；而牛顿法只用一阶导数。注意 `newton` 缺 `fprime` 时还能用有限差分自动补，但 `halley` 不做这种自动补全，因此 `fprime`/`fprime2` 都是硬性要求。

---

### 4.3 收敛保证与收敛阶：__init__.py 的对照表

#### 4.3.1 概念说明

`scipy.optimize` 的官方文档在 `__init__.py` 里维护着一张**求根方法对照表**，回答两个关键问题：

1. **保证收敛吗？**（Guaranteed?）——只有括号法保证收敛，点法不保证。
2. **收敛多快？**（Rate）——给出每种方法的**每步收敛阶** \(p\)，括号里还给出**每次函数求值**的等效收敛阶。

这张表是「在 8 种方法里做选择」时最实用的决策依据——比单纯记方法名更直观。

#### 4.3.2 核心流程

把对照表（来自源码）整理如下。`Rate` 列格式为「每步收敛阶（每次求值的等效阶）」：

| Solvers | Guaranteed? | 每步阶 \(p\)（每次求值） | 域 | 需要括号？ | 需要导数？ |
|---|:-:|:-:|:-:|:-:|:-:|
| `bisection` | **Yes** | 1（线性） | R | Yes | 无 |
| `brentq`    | **Yes** | 介于 1 与 1.62 | R | Yes | 无 |
| `brenth`    | **Yes** | 介于 1 与 1.62 | R | Yes | 无 |
| `ridder`    | **Yes** | 2.0（1.41） | R | Yes | 无 |
| `toms748`   | **Yes** | 2.7（1.65） | R | Yes | 无 |
| `secant`    | No | 1.62（1.62） | R 或 C | No | 无 |
| `newton`    | No | 2.00（1.41） | R 或 C | No | 一阶 |
| `halley`    | No | 3.00（1.44） | R 或 C | No | 一阶+二阶 |

**两个直觉**（来自文档原文）：

- **二分法最慢**：每次求值只增加约 1 bit 精度，但**保证收敛**。
- **其余括号法**每次求值大约把精度位数提升约 50%。
- **点法收敛可能极快**——前提是初值离根足够近；并且它们**可用于复平面**。

#### 4.3.3 源码精读

**对照表在源码中的位置**：

[__init__.py:181-208](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/__init__.py#L181-L208) —— 这就是文档里那张「Domain of f / Bracket? / Derivatives? / Solvers / Convergence」对照表，含逐方法的「Guaranteed?」与「Rate(s)」两列。

**对照表的解释性文字**（紧挨表格上方）：

[__init__.py:181-190](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/__init__.py#L181-L190) —— 关键三句：① 表格列出的是「渐近」收敛阶；② 二分法最慢但保证收敛；③ 其余括号法每次求值约提升 50% 精度位数；④ 导数法都构建在 `newton` 之上、收敛快但不保证、可用于复平面。

**收敛阶的数学含义**（以牛顿法为例）——牛顿法在根附近满足

\[
e_{n+1} \;\approx\; \frac{f''(x^\*)}{2 f'(x^\*)}\, e_n^{\,2}
\]

故每步收敛阶 \(p=2\)（平方收敛）。Halley 法进一步用二阶信息修正，达到 \(p=3\)（立方收敛）。括号法的「阶」介于 1 与约 1.62 之间，是因为它们混合了**稳健但慢的二分段**与**快速的插值段**——下一讲会拆开讲 `brentq` 与 `toms748` 是如何混合的。

> 关于「每次求值的等效阶」：括号法每步往往要算 1~2 次函数值，所以「每次求值」的等效阶比「每步」阶低；而 `secant` 每步只需 1 次新求值，故两者相同（都是 1.62，即黄金比例相关的常数）。

#### 4.3.4 代码实践

**目标**：用实验感受「保证收敛」与「收敛快」的权衡——同一个问题，慢的稳，快的不一定稳。

```python
# 示例代码
import numpy as np
from scipy.optimize import root_scalar

g = lambda x: np.arctan(x)        # 根在 x=0；导数 g'(0)=1，但远离 0 时很平
# 牛顿迭代 x_{n+1}=x_n - atan(x)/（1/(1+x^2))，当初值很大时会发散（经典反例）

print("=== 括号法 brentq（保证收敛）===")
print(root_scalar(g, bracket=[-10, 10], method='brentq').root)

print("=== newton，初值离根较近 ===")
print(root_scalar(g, x0=1.0, fprime=lambda x: 1/(1+x*x), method='newton').root)

print("=== newton，初值离根很远（可能不收敛）===")
try:
    sol = root_scalar(g, x0=10.0, fprime=lambda x: 1/(1+x*x),
                      method='newton', maxiter=200)
    print("root =", sol.root, "converged =", sol.converged, "flag =", sol.flag)
except RuntimeError as e:
    print("不收敛：", e)
```

**需要观察的现象**：

- `brentq` 无论区间多宽，只要端点异号就**一定收敛**到 0。
- `newton` 在 `x0=1.0` 时收敛很快；但在 `x0=10.0` 这类「远离根、函数很平」的初值下，经典的 `arctan` 反例会让迭代**来回振荡甚至发散**，最终 `converged=False` 或抛 `RuntimeError`。

**预期结果**：前两次都返回接近 `0.0`；第三次 `converged` 为 `False`。**精确行为待本地验证**（取决于 `maxiter` 与容差）。

> 这个实践正好印证对照表的核心结论：**括号法 = 稳但不快；点法 = 快但不稳**。工程上常常先用括号法保底，或给点法一个好的初值。

#### 4.3.5 小练习与答案

**练习 1**：你需要在复数域上求一个多项式的根，应排除哪些方法？

**答案**：排除 5 个括号法（`bisect`/`brentq`/`brenth`/`ridder`/`toms748`）——它们只能用于实数轴 `R`，因为「异号」在复数上无定义。只能在 `secant`/`newton`/`halley` 里选（对照表「Domain of f」列允许 `R 或 C`）。

**练习 2**：在「只给异号区间、不要导数」的前提下，按收敛阶从快到慢给括号法排序。

**答案**：`toms748`（2.7）> `ridder`（2.0）> `brentq`/`brenth`（≤1.62）> `bisection`（1，线性）。所以 `root_scalar` 在自动选法时把 `bracket` 默认派给 `brentq`（[_root_scalar.py:255-256](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_root_scalar.py#L255-L256)），是在「稳健 + 实测通常很快」之间的均衡选择，而非纯粹追最高阶。

---

### 4.4 MemoizeDer：值与导数的共享缓存

#### 4.4.1 概念说明

当你想让 `f` **一次调用同时返回函数值、一阶导、二阶导**时（例如 `def f_p_pp(x): return x**3-1, 3*x**2, 6*x`），你会把 `fprime=True`、`fprime2=True` 传给 `root_scalar`。问题来了：底层 `newton` 在一次迭代里会**分别**调用 `f(x)`、`fprime(x)`、`fprime2(x)` 三个函数对象。如果它们各自独立求值，那么 `f` 这同一个函数会在同一个 `x` 上被计算 3 遍。

`MemoizeDer` 就是为了消除这种重复：它把 `f` 包成一个**带缓存的对象**，`f(x)` / `fprime(x)` / `fprime2(x)` 三个入口共享同一份「最近一次求值」的结果。同一点 `x` 上，真正的计算**只发生一次**。

> 名字里的 `Der` = Derivative（导数）。`Memoize` = 记忆化（缓存）。合起来：**带导数的记忆化包装器**。

#### 4.4.2 核心流程

`MemoizeDer` 的缓存非常「朴素（simplistic）」——它**只记最近一个 `x`**，而不是一张历史表。这对求根场景刚好合适：迭代点很少重复，但「同一个 `x` 上的 `f` 和 `f'`」几乎总是成对需要。

```text
MemoizeDer(fun)              # fun(x) 返回 (f, f', f'') 元组
   │
   ├─ __call__(x)   → 若 x != self.x：调用 fun(x)，缓存 vals，n_calls+=1
   │                  返回 vals[0]  （函数值）
   ├─ fprime(x)     → 若缓存缺失或 x 变了：先触发 __call__(x)
   │                  返回 vals[1]  （一阶导）
   ├─ fprime2(x)    → 同上，返回 vals[2]  （二阶导）
   └─ ncalls()      → 返回真实求值次数 self.n_calls
```

**命中规则**：只要当前请求的 `x` 等于上一次缓存的 `self.x`，就直接返回缓存，不调用 `fun`。

#### 4.4.3 源码精读

**类定义与状态**：

[_root_scalar.py:20-34](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_root_scalar.py#L20-L34) —— `MemoizeDer.__init__` 保存被包装的 `fun`，并维护三个状态：`self.vals`（最近一次的返回元组）、`self.x`（最近一次的输入）、`self.n_calls`（真实求值次数，初始为 0）。

**缓存逻辑（最关键的三段）**：

[_root_scalar.py:36-56](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_root_scalar.py#L36-L56) —— 三个方法都遵循同一条「按需触发」规则：

```python
def __call__(self, x, *args):
    # 注意注释：导数可能在函数值之前被请求，所以每次都要检查
    if self.vals is None or x != self.x:
        fg = self.fun(x, *args)   # 真正计算一次
        self.x = x
        self.n_calls += 1
        self.vals = fg[:]
    return self.vals[0]           # 返回函数值

def fprime(self, x, *args):
    if self.vals is None or x != self.x:
        self(x, *args)            # 缓存缺失才触发 __call__
    return self.vals[1]           # 直接读缓存里的一阶导

def fprime2(self, x, *args):
    if self.vals is None or x != self.x:
        self(x, *args)
    return self.vals[2]           # 直接读缓存里的二阶导
```

> 注意源码注释提到的细节：**「导数可能在函数值之前被请求」**，所以每个方法都不能假设缓存已就绪，必须先自检。

**何时被启用**——`root_scalar` 在解析阶段决定是否包装：

[_root_scalar.py:221-236](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_root_scalar.py#L221-L236) —— 当 `fprime2`/`fprime` 是**布尔 `True`**（而非可调用对象）时，用 `f = MemoizeDer(f)` 包装原函数，并把 `fprime`/`fprime2` 重定向到包装器的 `f.fprime`/`f.fprime2`，同时置 `is_memoized = True`。如果传的是 `False`，则把对应项**清成 `None`**（即不使用该阶导数）。

**修正求值计数**——这是「为什么用 `MemoizeDer` 后 `function_calls` 会变小」的原因：

[_root_scalar.py:341-345](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_root_scalar.py#L341-L345) —— 若启用了 memoize，求解结束后用 `sol.function_calls = f.n_calls` **覆盖**底层报告的 `function_calls`。因为底层 `newton` 会把 `f`、`fprime`、`fprime2` 的调用**分别累计**，导致同一个 `x` 上的「一次真实计算」被重复计成 2~3 次；用 `n_calls` 覆盖后就还原成了真实次数。

> 这解释了 docstring 例子里那个有趣的现象（[_root_scalar.py:200-210](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_root_scalar.py#L200-L210)）：对同一个 `f_p_pp`，用 `fprime=True, method='newton'` 时 `function_calls=11`（与 `iterations=11` 相等），而分别提供独立 `f` 与 `fprime` 时 `function_calls` 会翻倍。

#### 4.4.4 代码实践

**目标**：直观看到 `MemoizeDer` 的缓存命中——对同一个 `x` 连续调用值与导数，真实计算只发生 1 次。

```python
# 示例代码
from scipy.optimize._root_scalar import MemoizeDer

n_evals = 0
def f_p_pp(x):
    global n_evals
    n_evals += 1                       # 统计「真正调用 fun」的次数
    return x**3 - 1, 3*x**2, 6*x       # 同时返回值、一阶导、二阶导

mf = MemoizeDer(f_p_pp)

# 在同一个 x=0.5 上分别取值、一阶导、二阶导
v  = mf(0.5)
d1 = mf.fprime(0.5)
d2 = mf.fprime2(0.5)

print("value     =", v, " f'(0.5) =", d1, " f''(0.5) =", d2)
print("真实 fun 调用次数 =", n_evals, "（期望 1，因为三次访问共享同一次求值）")
print("MemoizeDer.n_calls =", mf.ncalls())

# 换一个新 x=0.7，应当触发一次新计算
mf.fprime(0.7)
print("访问 0.7 后 n_calls =", mf.ncalls(), "（期望 2）")
```

**操作步骤**：运行 `python memo_demo.py`。

**预期结果**：第一组对 `0.5` 的三次访问后，`n_evals` 与 `mf.ncalls()` 都为 `1`；访问 `0.7` 后变为 `2`。这印证了 [_root_scalar.py:39](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_root_scalar.py#L39) 的命中判定 `x != self.x`。

> **进阶对照**：把上面 `mf` 直接喂给 `root_scalar`：`root_scalar(f_p_pp, x0=0.2, fprime=True, fprime2=True, method='halley')`，观察返回的 `sol.function_calls` 与未启用 memoize 时的差异——这正是 [_root_scalar.py:341-345](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_root_scalar.py#L341-L345) 修正逻辑的效果。

#### 4.4.5 小练习与答案

**练习 1**：`MemoizeDer` 只缓存「最近一个 `x`」。如果底层求解器在两次迭代中返回了**同一个** `x`（比如收敛后再次求值），会发生什么？这是好事还是坏事？

**答案**：会命中缓存（`x == self.x`），直接返回 `vals[1]`/`vals[2]` 而不重新计算 `fun`，`n_calls` 不增加。这是好事——避免了无意义的重复求值。源码注释（[_root_scalar.py:27-29](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_root_scalar.py#L27-L29)）也说明：求根中 `x` 极少重复，但一旦重复，缓存正好派上用场。

**练习 2**：如果不传 `fprime=True` 而是传一个**独立的可调用** `fprime=fp`，`root_scalar` 还会用 `MemoizeDer` 包装吗？

**答案**：不会。[_root_scalar.py:230-236](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_root_scalar.py#L230-L236) 只在 `fprime` **非可调用**（即布尔）时才触发包装；传可调用对象时 `f` 与 `fprime` 是两个独立函数，底层会分别调用、分别计费，`function_calls` 自然更高。这也解释了为什么「单函数返回值+导数」的模式（`fprime=True`）更省求值次数。

## 5. 综合实践

**任务**：求解「Dottie 数」\(x^\*=\cos(x^\*)\)（即 \(f(x)=\cos(x)-x\) 的根），并用三种不同输入策略对比 `root_scalar` 的行为，最后验证 `MemoizeDer` 的省算效果。

```python
# 示例代码
import numpy as np
from scipy.optimize import root_scalar
from scipy.optimize._root_scalar import MemoizeDer

f      = lambda x: np.cos(x) - x
fp     = lambda x: -np.sin(x) - 1
fpp    = lambda x: -np.cos(x)
f_all  = lambda x: (np.cos(x) - x, -np.sin(x) - 1, -np.cos(x))   # 同时返回值、f'、f''

# 策略 A：括号法（保证收敛，无需导数）
A = root_scalar(f, bracket=[0, 1], method='brentq')

# 策略 B：newton（需一阶导，自动选中）
B = root_scalar(f, x0=0.0, fprime=fp)   # method 留空 → 自动 newton

# 策略 C：halley + MemoizeDer（fprime=True 触发包装，省算）
C = root_scalar(f_all, x0=0.0, fprime=True, fprime2=True, method='halley')

for name, s in [("A brentq", A), ("B newton", B), ("C halley+memo", C)]:
    print(f"{name:16s} root={s.root:.15f} iter={s.iterations} "
          f"feval={s.function_calls} conv={s.converged}")

# 验证 MemoizeDer 的省算效果：直接用包装器统计
m = MemoizeDer(f_all)
for _ in range(3):           # 模拟底层在一次迭代里分别取值/一阶/二阶
    m(0.5); m.fprime(0.5); m.fprime2(0.5)
print("对 x=0.5 连续取值/一阶/二阶各 3 次，真实 fun 调用次数 =", m.ncalls(), "（期望 1）")
```

**需要观察与思考**：

1. 三种策略的 `root` 是否一致（都应约为 `0.7390851332151607`）？
2. `feval` 排序：理论上 `C`（halley + memo）每步真实求值最少，`B`（独立 fprime）每步要算 2 次，`A`（括号法）求值次数介于二者行为不同——**具体数字待本地验证**。
3. 最后一行应打印 `1`，证明 `MemoizeDer` 让「同一点上的值与各阶导数」共享同一次 `fun` 调用。

**延伸**：把策略 C 的 `fprime=True/fprime2=True` 改成 `fprime=fp/fprime2=fpp`（独立可调用），再对比 `feval`——你会看到 `feval` 明显变大，从而亲手验证 [_root_scalar.py:341-345](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_root_scalar.py#L341-L345) 那段「计数修正」存在的必要性。

## 6. 本讲小结

- `root_scalar` 是**调度器**而非求解器：它解析参数、按需自动选方法、翻译容差（如 `xtol→tol`），再把请求派发给 `_zeros_py.py` 里的底层函数，最后把结果包成 `RootResults` 返回。
- 方法自动选择的优先级是 **`bracket`→`brentq`；否则按导数多少选 `halley`>`newton`>`secant`>退化`newton`**（[_root_scalar.py:252-269](https://github.com/scipy/scipy/blob/de190e7fde9d3d34400dbfe1eeacc9fc6d29cede/scipy/optimize/_root_scalar.py#L252-L269)），核心判据就是「你手里有括号还是有点 + 导数」。
- `ROOT_SCALAR_METHODS` 列出 8 种方法，分两大阵营：**5 个括号法**（保证收敛、仅实数）+ **3 个点法**（不保证收敛、可用复数、阶更高）。`halley`/`secant` 都映射到底层同一个 `newton` 函数。
- `__init__.py` 的对照表给出**收敛保证**与**收敛阶**：括号法 `toms748`(2.7)>`ridder`(2.0)>`brentq`/`brenth`(≤1.62)>`bisection`(1)；点法 `halley`(3)>`newton`(2)>`secant`(1.62)。「稳」与「快」往往不可兼得。
- `MemoizeDer` 用「只记最近一个 `x`」的朴素缓存，让「单函数同时返回值与各阶导」时，同一点只计算一次；求解结束后用它修正 `function_calls`，避免底层把一次真实求值计成 2~3 次。

## 7. 下一步学习建议

- **下一讲 [u2-l3](u2-l3-zeros-algorithms.md)**：本讲只讲了「怎么选、怎么分发」，下一讲会打开 `_zeros_py.py`，精读 `bisect`/`brentq`/`ridder`/`toms748`/`newton`/`_array_newton` 的**真正算法实现**——届时你会看到 `brentq` 是怎么把「抛物线插值」和「二分保护」缝在一起的。
- **横向迁移**：本讲的「调度器 + 自动选法」模式与 [u1-l3](u1-l3-minimize-dispatcher.md) 的 `minimize` 完全同构，学完后可以回头对照，加深对 scipy.optimize 统一设计哲学的理解。
- **进阶预告**：当你需要在参数数组上**批量**求根时，`root_scalar` 是标量的；批量场景请看 [u10-l2](u10-l2-elementwise.md) 的 elementwise API（`find_root`/`bracket_root`），它们建立在类似的底层算法之上。
