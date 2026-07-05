# _basic.py(一):组合与阶乘类函数

## 1. 本讲目标

学完本讲后,你应该能够:

- 说清楚 `scipy.special` 中 `comb`、`perm`、`factorial`、`factorial2`、`factorialk`、`stirling2` 这些「组合与阶乘类」函数**为什么用纯 Python 实现而不是 ufunc**。
- 理解 `exact=True`(精确整数算术)与 `exact=False`(浮点近似)两条计算路径的分工,以及它们各自调用底层 ufunc(`binom`、`poch`、`gamma`)还是 Python 大整数。
- 掌握 `_factorialx_wrapper` 如何用「同一份代码 + 参数 `k`」统一实现 `factorial`/`factorial2`/`factorialk` 三个函数。
- 看懂 `_FACTORIALK_LIMITS_64BITS` / `_FACTORIALK_LIMITS_32BITS` 两张表如何驱动 `_factorialx_array_exact` 的「dtype 自动升级」机制,从而**在 int64/int32 即将溢出前安全切换到 Python 任意精度整数**。
- 顺手认识 `diric`、`sinc`、`bernoulli`、`euler`、`softplus` 这几个「便利函数」各自的实现策略(纯 Python 数值稳定化、薄包装转发、序列函数)。

## 2. 前置知识

本讲默认你已经读过:

- **u1-l4**:知道 `scipy.special` 的命名空间由 `_ufuncs`、`_basic`、`_orthogonal` 等子模块拼装而成;`_basic.py` 贡献的名字在 `__init__.py` 里被收进 `__all__`。
- **u2-l1**:理解 ufunc 的本质——按类型码分发的、**必然逐元素**的 C 循环,输入与输出形状一致、可批量。

下面用通俗语言补三个本讲要用到的小概念:

- **ufunc 的「形状契约」**:一个 ufunc 接受若干同形状数组、输出同形状数组。如果某个函数「输入一个标量 `n`、却要返回长度为 `n+1` 的数组」(输出长度依赖输入值),它就**做不成 ufunc**。这正是 `bernoulli`、`euler` 这类序列函数的现状。
- **Python 大整数(Python `int`)**:Python 的 `int` 是任意精度的,`2**100` 不会溢出;而 NumPy 的 `int64`/`int32` 是定宽的,超过上限就溢出回绕。`exact=True` 模式的全部意义,就是借助 Python `int` 给出**数学上完全正确**的大数结果,代价是不能再用 ufunc 的高速 C 循环。
- **`math.comb` / `math.perm` / `math.factorial`**:Python 3.8+ 标准库提供的、基于 Python 大整数的精确组合数与阶乘,`_basic.py` 的 `exact` 路径会直接复用它们。

## 3. 本讲源码地图

本讲几乎全部围绕一个文件:

| 文件 | 作用 |
| --- | --- |
| [_basic.py](_basic.py) | `scipy.special` 的纯 Python 包装层。本讲覆盖其中的组合/阶乘/便利函数;文件里还有大量贝塞尔零点、各阶导数等函数,留给 u4-l2。 |

文件内部的「函数 → 行号」索引(本讲涉及的部分,行号基于当前 HEAD `8e93e0478c`):

| 函数 / 符号 | 行号 | 角色 |
| --- | --- | --- |
| 顶部 import(`sinc`、`binom`、`poch`、`gamma` 等) | L10–L20 | 复用 NumPy 与 `_ufuncs` 的现成能力 |
| `_FACTORIALK_LIMITS_64BITS` / `_FACTORIALK_LIMITS_32BITS` | L84–L89 | int64/int32 溢出阈值表 |
| `diric` | L92–L192 | Dirichlet 核(周期 sinc),纯 Python 数值稳定化 |
| `bernoulli` / `euler` | L1806–L1906 | 序列函数,薄包装 `_specfun` |
| `comb` / `perm` | L2601–L2753 | 组合数 / 排列数,exact 双轨 |
| `_range_prod` | L2756–L2783 | 分治法求「等差数列连乘」,exact 路径核心 |
| `_factorialx_array_exact` | L2786–L2843 | 数组版 exact,含 dtype 升级 |
| `_factorialx_array_approx` / `_gamma1p` / `_factorialx_approx_core` | L2846–L2935 | 浮点近似核心 |
| `_factorialx_wrapper` | L2964–L3059 | `factorial`/`factorial2`/`factorialk` 的共享派发器 |
| `factorial` / `factorial2` / `factorialk` | L3062–L3258 | 三个公开函数,仅差参数 `k` |
| `stirling2` | L3261–L3394 | 第二类 Stirling 数,动态规划 + Temme 近似 |
| `softplus` | L3477–L3507 | 薄包装 `np.logaddexp` |

## 4. 核心概念与源码讲解

本讲拆成四个最小模块:4.1 组合数学函数(`comb`/`perm`/`stirling2`);4.2 `exact` 模式与统一的 `_factorialx_wrapper`;4.3 整数溢出保护(两张 LIMITS 表 + `_range_prod`);4.4 便利与序列函数(`diric`/`sinc`/`bernoulli`/`euler`/`softplus`)。

### 4.1 组合数学函数:comb 与 perm

#### 4.1.1 概念说明

`comb(N, k)` 是「从 N 个里取 k 个的组合数」\( \binom{N}{k} \);`perm(N, k)` 是「排列数」\( P(N,k)=N!/(N-k)! \)。它们都有一个关键开关 `exact`:

- `exact=False`(默认):用**浮点**快速算。组合数走 `binom` ufunc,排列数走 `poch` ufunc,可以吃数组、可批量、快,但结果是 float64 近似值,大数会丢精度。
- `exact=True`:用**精确整数**算。组合数直接调 Python 标准库 `math.comb`,排列数手写一个连乘循环。结果是无损的 Python `int`,但**只接受标量整数**(不支持数组)。

这正是「为什么不是 ufunc」的第一个理由:**`exact=True` 要返回任意大的 Python 大整数,且只接受标量,这两条都违反 ufunc 的形状契约与固定类型契约**。所以 `comb`/`perm` 只能是纯 Python 函数,在内部按需调用 ufunc。

#### 4.1.2 核心流程

`comb` 的判定流程(伪代码):

```
若 repetition=True(允许重复的组合,即"stars and bars"):
    若 exact: 返回 math.comb(N + k - 1, k)        # C(N+k-1, k)
    否则:      vals = binom(N + k - 1, k);   # ufunc,可批量
              把 C(·,0)=1 的边界修正为 1.0
若 repetition=False:
    若 exact: 校验 N,k 是整数 → math.comb(N, k);   越界返回 0
    否则:      vals = binom(N, k);  cond = (k<=N)&(N>=0)&(k>=0);
              把不满足 cond 的位置置 0
```

`perm` 同构:`exact=True` 时连乘 `range(N-k+1, N+1)`;`exact=False` 时调 `poch(N-k+1, k)`(ufunc)再掩码。

#### 4.1.3 源码精读

[comb 的 repetition 与 exact 分支 — _basic.py:L2649-L2676](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_basic.py#L2649-L2676):注意 `exact=True` 时直接 `return math.comb(N, k)`,把精确整数算术整个外包给标准库;对非整数输入则抛 `ValueError`。

[comb 的浮点(ufunc)分支 — _basic.py:L2677-L2685](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_basic.py#L2677-L2685):核心只有一行 `vals = binom(N, k)`——`binom` 是从 `_ufuncs` 导入的 ufunc(见 [_basic.py:L14-L16](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_basic.py#L14-L16))。剩下的 `cond` 掩码是为了满足「N<0 或 k>N 时返回 0」的语义约定,因为 ufunc 版的 `binom` 本身并不保证这一点。

[perm 的 exact 连乘 — _basic.py:L2726-L2744](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_basic.py#L2726-L2744):`for i in range(floor_N - floor_k + 1, floor_N + 1): val *= i`,纯 Python 连乘得到精确整数。浮点分支则 `vals = poch(N - k + 1, k)`([L2745-L2753](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_basic.py#L2745-L2753))。

> 一个工程细节:`perm` 用 `np.squeeze(N)[()]` 来兼容「大小为 1 的数组」作为标量输入([L2727-L2728](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_basic.py#L2727-L2728)),这是为向后兼容留下的「薄垫片」。

[stirling2 的双路径 — _basic.py:L3336-L3347](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_basic.py#L3336-L3347):`exact=False` 时把 `(N,K)` 转成 float 后交给 `_stirling2_inexact` 这个 ufunc(基于 Temme 渐近近似);`exact=True` 时则用堆 + 动态规划逐行递推 `S(n,k) = k·S(n-1,k) + S(n-1,k-1)`。后者是一个**非逐元素**的算法,所以 `stirling2` 整体绝不是 ufunc——`_stirling2_inexact` 只在它内部被当作一个工具来调。

#### 4.1.4 代码实践

**实践目标**:亲手看到 `exact` 两条路径在「精度」与「输入类型」上的差异。

**操作步骤**:

```python
import numpy as np
from scipy.special import comb, perm

# 1) 小数:exact 与 float 结果一致
print(comb(10, 3, exact=True))    # 120 (int)
print(comb(10, 3, exact=False))   # 120.0 (float64)

# 2) 大数:float 开始丢精度,exact 仍精确
N, k = 60, 30
print(comb(N, k, exact=True))     # 118264581564861424 (精确整数)
print(comb(N, k, exact=False))    # 1.1826458156486142e+17 (float64 近似)

# 3) 数组输入:只有 exact=False 支持
print(comb(np.array([10, 10]), np.array([3, 4]), exact=False))  # [120. 210.]
try:
    comb(np.array([10, 10]), np.array([3, 4]), exact=True)      # 抛 ValueError
except ValueError as e:
    print("exact=True 不支持数组:", e)
```

**需要观察的现象**:`comb(60,30)` 的 `exact=True` 给出一个 18 位的整数,而 `exact=False` 给出科学计数法的 float,末几位已经被舍入。

**预期结果**:`comb(60,30,exact=True) = 118264581564861424`;`comb(60,30,exact=False) ≈ 1.1826458156486142e+17`(末三位 `424` 在 float 里变成 `142`,印证精度损失)。`exact=True` 配数组输入的精确行为(是否抛错)**待本地验证**。

#### 4.1.5 小练习与答案

**Q1**:为什么 `comb` 在 `exact=False` 时还要写一段 `cond = (k<=N)&(N>=0)&(k>=0)` 掩码,而不是直接 `return binom(N, k)`?
**A**:`binom` ufunc 只负责「把 Gamma 函数解析延拓到实数」的纯数值计算,它并不强制 `comb` 的组合语义(比如 `k>N` 应回到 0)。掩码负责把「不构成有效组合」的位置强制置 0,这是语义层补丁,不在 ufunc 职责内。

**Q2**:`comb(10, 3, repetition=True, exact=True)` 应该等于多少?为什么?
**A**:等于 `C(10+3-1, 3) = C(12,3) = 220`。「允许重复取 k 个」的组合数由「stars and bars」给出 \( \binom{N+k-1}{k} \),所以代码里 `repetition` 分支递归调用 `comb(N+k-1, k)`。

**Q3**:`perm` 的浮点分支为什么用 `poch(N-k+1, k)` 而不是 `binom(N,k) * factorial(k)`?
**A**:`poch(a, b) = Γ(a+b)/Γ(a)` 是上升阶乘,正好等于 `(N-k+1)(N-k+2)…N = N!/(N-k)! = P(N,k)`,一次 ufunc 调用搞定,比分两次再相乘更省、数值也更稳。

### 4.2 exact 模式:factorial 家族的统一实现 _factorialx_wrapper

#### 4.2.1 概念说明

`factorial`、`factorial2`(双阶乘)、`factorialk`(多阶乘,`n!(k)`)三个函数,数学上是同一个东西的特例:

\[
\text{factorial}(n) = \text{factorialk}(n, 1),\qquad
\text{factorial2}(n) = \text{factorialk}(n, 2)
\]

其中多阶乘定义为(从 `n` 开始每次减 `k`,连乘到正数为止):

\[
n!(k) = n\,(n-k)\,(n-2k)\,\cdots,\qquad \text{factorialk}(17,4)=17\cdot 13\cdot 9\cdot 5\cdot 1
\]

既然三者同构,源码就把它们合并成一个共享派发器 `_factorialx_wrapper(fname, n, k, exact, extend)`,三个公开函数只是用不同的 `k` 调用它:

- `factorial(n)` → `k=1`
- `factorial2(n)` → `k=2`
- `factorialk(n, k)` → 用户给的 `k`

这能成立,是因为「连乘步长」`k` 是唯一的差异点;`exact` 与 `extend` 两个开关的语义对三者完全一致。

#### 4.2.2 核心流程

`_factorialx_wrapper` 的派发逻辑(伪代码):

```
1. 校验 extend ∈ {'zero','complex'};且 exact 与 'complex' 互斥
2. 若 fname=='factorialk':额外校验 k 的类型/取值(k≥1, k≠0)
3. 区分 scalar 与 array:
   scalar n:
     - None/NaN → 返回 nan(float 或 complex)
     - extend='zero' 且 n<0 → 0
     - n∈{0,1} → 1
     - exact 且整数 → _range_prod(1, n, k=k)        # 精确连乘
     - 否则(近似) → _factorialx_approx_core(...)   # 用 Gamma
   array n:
     - exact → _factorialx_array_exact(n, k)         # 带 dtype 升级
     - 近似  → _factorialx_array_approx(n, k, extend)
```

`extend` 参数控制「负数怎么处理」:`'zero'`(默认)对 `n<0` 返回 0;`'complex'` 用 Gamma 函数做解析延拓,得到复数值。后者会改变某些正整数点的值(如 `factorial2` 在偶数点会被 `sqrt(2/π)` 重新标定),所以是一个需要显式 opt-in 的开关。

#### 4.2.3 源码精读

[三个公开函数仅差 k — _basic.py:L3062-L3116 (factorial)](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_basic.py#L3062-L3116)、[L3119-L3173 (factorial2)](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_basic.py#L3119-L3173)、[L3176-L3258 (factorialk)](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_basic.py#L3176-L3258):每个函数体都只有一行,例如 `factorial2` 就是 `return _factorialx_wrapper("factorial2", n, k=2, exact=exact, extend=extend)`。这是「同一份代码 + 参数 `k`」去重的典型写法。

[scalar 的 exact 路径 — _basic.py:L3032-L3034](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_basic.py#L3032-L3034):`return _range_prod(1, int(n), k=k)`——把精确连乘交给 4.3 节要讲的分治函数。

[scalar 的近似路径 — _basic.py:L3038-L3039](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_basic.py#L3038-L3039):`return _factorialx_approx_core(n, k=k, extend=extend)`,核心对 `k=1` 就是 `gamma(n+1)`([_gamma1p — L2866-L2878](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_basic.py#L2866-L2878) 用 `_ufuncs.gamma`,并把 `n=-1` 处的 `inf` 改成 `nan`)。对 `k>1` 用多阶乘的 Gamma 近似公式。

[fname 差异化校验 — _basic.py:L2997-L3013](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_basic.py#L2997-L3013):`extend='complex'` 的「副作用说明」按 `fname` 不同分别追加(`factorial2` 会重标偶数、`factorialk` 会扰动多数正整数),并且只有 `factorialk` 需要**校验参数 `k`** 的类型与取值。这就是「统一实现里仍保留少量分支」的地方。

> 多阶乘的 Gamma 近似(支撑 `_factorialx_approx_core`,见 [_basic.py:L2908-L2935](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_basic.py#L2908-L2935)):令 \( r=n\bmod k \),有
> \[ n!(k)=k^{(n-r)/k}\,\frac{\Gamma(n/k+1)}{\Gamma(r/k+1)}\,\max(r,1). \]
> 代码把「与余数 `r` 无关」的因子 `k^(n/k)·Γ(n/k+1)` 先算一次,再按不同余数类乘上修正项 `corr(k,r)`,避免重复求 Gamma。

#### 4.2.4 代码实践

**实践目标**:验证三个函数确实是「同一份代码、不同 `k`」,并观察 `extend` 的副作用。

**操作步骤**:

```python
from scipy.special import factorial, factorial2, factorialk

# 1) 三者一致性:factorialk(n, k) 分别退化成 factorial2 / factorial
n = 9
print(factorial(n), factorial2(n), factorialk(n, 1), factorialk(n, 2))
# 预期: 362880  945  362880  945
#   即 9!=362880; 9!!=9*7*5*3*1=945; factorialk(9,1)=9!; factorialk(9,2)=9!!

# 2) extend 的副作用:factorial2 在偶数点会被 sqrt(2/pi) 重标
print(factorial2(8, exact=True))                       # 384  (= 8*6*4*2)
print(factorial2(8, extend='zero', exact=False))       # 384.0
print(factorial2(8, extend='complex'))                 # 被 sqrt(2/pi) 缩放,变小
```

**需要观察的现象**:`factorial(9)`、`factorial2(9)` 与 `factorialk(9,1)`、`factorialk(9,2)` 完全相等,印证「同一实现」;`factorial2(8, extend='complex')` 比默认值 `384` 小(乘了 `sqrt(2/π)≈0.798`)。

**预期结果**:`9! = 362880`、`9!! = 945` 两组分别相等;`factorial2(8, exact=True)=384`,`extend='complex'` 约 `384·\sqrt{2/\pi} ≈ 306`(精确小数**待本地验证**)。

#### 4.2.5 小练习与答案

**Q1**:为什么不直接写三个独立函数,而要共用 `_factorialx_wrapper`?
**A**:三者的差异**只有连乘步长 `k`**;校验逻辑、scalar/array 分支、dtype 升级、Gamma 近似几乎完全相同。合并成 `fname + k` 的派发器可以避免三份近乎复制的代码,降低维护成本(改一处即三处生效)。

**Q2**:`factorial2(8)` 的默认结果是 384,而 `extend='complex'` 会把它缩小,这对吗?
**A**:对。双阶乘默认用「偶数分支」定义 \(n!!=2^{n/2}(n/2)!\);而复数延拓统一用奇数分支的解析式 \(n!!=2^{n/2}\Gamma(n/2+1)\sqrt{2/\pi}\),它对偶数 `n` 会多乘一个 \(\sqrt{2/\pi}\) 的倒数(即缩小)。这是为什么文档把 `extend='complex'` 标为「会改变偶数点的值」,需要用户显式 opt-in。

**Q3**:`exact=True` 与 `extend='complex'` 同时传会怎样?
**A**:抛 `ValueError("Incompatible options: exact=True and extend='complex'")`(见 [_basic.py:L2972-L2973](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_basic.py#L2972-L2973))。因为精确整数算术只对整数 `n` 有意义,而 `extend='complex'` 恰恰是为了处理非整数/负数的复数延拓,二者互斥。

### 4.3 整数溢出保护:_FACTORIALK_LIMITS_* 与 _range_prod

#### 4.3.1 概念说明

这是本讲最有工程味的一节。问题如下:`exact=True` 要给出精确整数,但当输入是**数组**时,函数会用 NumPy 数组来存结果以求速度;可 NumPy 的定宽整数(`int64`/`int32`)一旦结果超过上限就会**静默溢出回绕**,得到完全错误的「负数」或「小数」。

`_basic.py` 的解法是:预存两张阈值表 `_FACTORIALK_LIMITS_64BITS` / `_FACTORIALK_LIMITS_32BITS`,它们记录「对每个步长 `k`,最大的 `n` 使得 `factorialk(n,k)` 仍能装进 `int64`/`int32`」。在算之前先查表,**根据输入最大值自动把输出 dtype 升级成 `int64` 或 Python `object`(任意精度)**,从而在溢出发生之前就切换到安全类型。

支撑这套机制的还有 `_range_prod`:一个**分治连乘**函数,用「折半相乘」代替「顺序连乘」,让大整数运算的中间乘积更平衡、更快,并对 `k>1` 的多阶乘保证每个子区间都落在正确的步长格点上。

#### 4.3.2 核心流程

dtype 升级决策(`_factorialx_array_exact` 内,伪代码):

```
un = np.unique(n)             # 排序去重后的输入
if k in _FACTORIALK_LIMITS_64BITS:
    if un[-1] > LIMITS_64BITS[k]:   dt = object    # 超过 int64 → 用 Python 大整数
    elif un[-1] > LIMITS_32BITS[k]: dt = int64     # 超过 int32 但未超 int64
    else:                            dt = long      # 平台默认 long(int32/int64)
else:  # k>=10 等未列表化的情况
    dt = object                                 # 一律用大整数,最保险
```

分治连乘(`_range_prod(lo, hi, k)`,伪代码):

```
若 lo==1 且 k==1: return math.factorial(hi)     # 特例:直接用标准库
若区间够长(lo+k < hi):
    mid = (hi+lo)//2 ; 若 k>1 把 mid 调到"距 hi 为 k 的倍数"处
    return _range_prod(lo, mid, k) * _range_prod(mid+k, hi, k)   # 折半
若区间恰好两元素(lo+k==hi): return lo*hi
否则: return hi                                  # 单元素
```

之所以折半:`a*b*c*...*z` 顺序乘会让中间值先变得很大、再继续乘更大;折半成 `(a*b)*(c*d)` 这种「平衡二叉」的乘法树,大整数乘法的代价更低(大整数乘法对位数敏感)。对 `k>1`,折半点 `mid` 还必须对齐到步长格点,否则会把 `n!(k)` 的因子切错。

阈值表的取值含义(源码注释):`_FACTORIALK_LIMITS_64BITS = {1: 20, 2: 33, 3: 44, 4: 54, 5: 65, 6: 74, 7: 84, 8: 93, 9: 101}`,即 `20!`、`33!!`、`44!!!`…… 分别是各自能装进 `int64` 的「最末一个 `n`」。

#### 4.3.3 源码精读

[两张阈值表 — _basic.py:L84-L89](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_basic.py#L84-L89):注释点明「`k` → 使 `factorialk(n,k) < int64.max` 的最末 `n`」。`k=1:20` 因为 `20!=2.43e18<9.22e18=int64.max`,而 `21!=5.11e19` 溢出;`k=2:33` 因为 `33!!=6.33e18<int64.max`,而 `35!!=2.22e20` 溢出。

[dtype 升级判定 — _basic.py:L2802-L2813](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_basic.py#L2802-L2813):三级升级(`long`→`int64`→`object`)全部由两张表驱动,`k≥10` 直接走 `object`。

[分治连乘 _range_prod — _basic.py:L2756-L2783](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_basic.py#L2756-L2783):注意 [L2778](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_basic.py#L2778) 那句 `mid = mid - ((mid - hi) % k)`——把折半点强制对齐到「距 `hi` 为 `k` 的倍数」,这是多阶乘能正确分治的关键。

[数组 exact 的「车道」复用 — _basic.py:L2822-L2842](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_basic.py#L2822-L2842):对 `k>1`,把输入按「模 `k` 的余数」分成 `k` 条车道分别连乘(因为 `n!(k)` 只连接同余类的数);每条车道内只算「从上一个 `n` 到当前 `n`」的增量连乘,避免重复计算。这是「数组版只算一次最大值、其余顺带得出」的优化(见 `factorial` 文档串 L3094–L3096)。

#### 4.3.4 代码实践

**实践目标**:亲手验证「`33!!` 刚好不溢出 `int64`」,并看清 `exact` 与浮点模式在「是否溢出/丢精度」上的差别。

**操作步骤**:

```python
import numpy as np
from scipy.special import factorialk

INT64_MAX = np.iinfo(np.int64).max     # 9223372036854775807

# 1) 33!! 应当刚好落在 int64 内
v33 = factorialk(33, 2, exact=True)    # k=2 即双阶乘
print("33!! =", v33, " 超过 int64?", v33 > INT64_MAX, " 类型?", type(v33))

# 2) 35!! 超过 int64,exact=True 必须返回 Python 大整数
v35 = factorialk(35, 2, exact=True)
print("35!! =", v35, " 超过 int64?", v35 > INT64_MAX, " 类型?", type(v35))

# 3) exact=False 走浮点,大数不会"溢出回绕"但会丢精度/变 inf
print("33!! (float) =", factorialk(33, 2, exact=False))
print("35!! (float) =", factorialk(35, 2, exact=False))

# 4) 数组输入:触发 dtype 升级
arr = factorialk(np.array([33, 35]), 2, exact=True)
print("array dtype =", arr.dtype, " values =", arr)
```

**需要观察的现象**:
- `33!!` 是一个 19 位的正整数,**小于** `int64.max`,类型是 Python `int`(标量)。
- `35!!` 是一个 21 位的正整数,**大于** `int64.max`,仍是一个**正确**的正整数(因为已升级到 Python 大整数,没有回绕成负数)。
- 数组版 `[33, 35]` 的 `dtype` 应当是 `object`(因为最大值 `35>33=LIMITS_64BITS[2]`,触发升级到 `object`),两个值都正确。

**预期结果**(手工核算值,供你核对):
- `33!! = 6332659870762850625`(`6.33e18 < 9.22e18`,不溢出)
- `35!! = 221643095476699771875`(`2.22e20`,溢出 `int64`,但 `exact=True` 仍给出正确正值)
- 数组 `factorialk([33,35], 2, exact=True)` 的 `dtype` 为 `object`
- 由于本环境未能实际运行以上命令,`exact=False` 下 `35!!` 是否显示为 `inf` 等具体打印格式标记为**待本地验证**。

#### 4.3.5 小练习与答案

**Q1**:`_FACTORIALK_LIMITS_64BITS[2] = 33`,但 `34!!`(偶数双阶乘)其实也装得下 `int64`(它比 `33!!` 小得多)。为什么阈值填的是 33 而不是 34?
**A**:对 `k=2`,**奇数分支**增长远快于偶数分支(`33!!≈6.3e18` 而 `34!!≈4.7e16`)。表里的 `33` 是「奇数同余类」能装下的最末 `n`(`35!!` 就溢出了)。代码用 `un[-1] > 33` 作判据是一种**保守**阈值:只要输入里有 `n>33`,就不区分奇偶一律升级到 `object`,宁可对 `34!!` 这种「其实能装下」的情况也多花点代价用大整数,换取「绝不静默溢出」的安全性。

**Q2**:`_range_prod` 为什么不直接 `for i in range(...): prod *= i` 顺序乘?
**A**:顺序连乘会让中间结果「先变大、再继续乘更大」,大整数乘法对**位数**敏感,顺序乘的总代价偏高。折半成分治乘法树能让左右两半的位数大致均衡,降低总的大整数乘法开销(注释 `_range_prod(2,9)=((2*3)*(4*5))*((6*7)*(8*9))` 就在演示这种平衡二叉结构)。

**Q3**:`k=10` 时,两张表里都没有对应条目,代码会怎样?
**A**:走 [_basic.py:L2811-L2813](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_basic.py#L2811-L2813) 的 `else` 分支,直接 `dt = object`。即对未列表化的 `k≥10`,一律用 Python 大整数,牺牲速度换绝对安全——不预计算所有 `k` 的精确阈值,是一种「以不变应万变」的稳妥策略。

### 4.4 便利与序列函数:diric、sinc、bernoulli、euler、softplus

#### 4.4.1 概念说明

`_basic.py` 还收了一批「不便归入某大类、但常用」的便利函数,它们的实现策略各异,正好补全「为什么不是 ufunc」的全景:

- **`diric(x, n)`(Dirichlet 核,周期 sinc)**:定义 \( \mathrm{diric}(x,n)=\frac{\sin(nx/2)}{n\sin(x/2)} \)。它在分母 `sin(x/2)≈0` 处有可去奇点,需要**逐点判断、定点补救**(否则直接做除法会得到 `nan`/`inf`)。这种「按阈值分支」的逻辑塞不进 ufunc 的统一 loop,故用纯 Python + 掩码实现。
- **`sinc`**:注意它**不是 `_basic.py` 自己实现的**!文件顶部 `from numpy import (..., sinc)` 直接把 NumPy 的 `sinc` 转发出来([_basic.py:L10-L11](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_basic.py#L10-L11)),只是为了让 `scipy.special.sinc` 这个名字可用。这是「薄到不能再薄」的包装。
- **`bernoulli(n)` / `euler(n)`(伯努利数 / 欧拉数)**:输入标量 `n`、返回**长度 n+1 的数组** `[B(0),...,B(n)]`。输出长度依赖输入值,违反 ufunc 形状契约,所以是序列函数;实现上把重活外包给 `_specfun.bernob` / `_specfun.eulerb`(Zhang & Jin 的 Fortran 内核,见 u3-l4)。
- **`softplus(x)`**:定义为 \( \log(1+e^x) \),大 `x` 时 `exp` 会溢出。直接转发到 `np.logaddexp(0, x)`,后者是 NumPy 已经做好的、数值稳定的实现。又一个「转发型」薄包装。

#### 4.4.2 核心流程

`diric` 的数值稳定化(伪代码):

```
y = zeros(...)
mask1 = (n<=0) | (n 不是整数)   → 这些点置 nan
denom = sin(x/2)
mask2 = (合法) & (|denom| < minval)  → 可去奇点区,用极限 ±1 填充
                                (符号由 round(x/2/π) 与 n 决定)
mask  = (合法) & (非奇点)       → 正常计算 sin(n*x/2)/(n*denom)
```

其中 `minval` 按 `ytype` 的浮点精度(128/64/32 位)取不同阈值,避免「分母极小但非零」时的灾难性相消。

`softplus` 一行:`return np.logaddexp(0, x, **kwargs)`——把 `**kwargs`(如 `out=`)透传给底层 ufunc,所以它**看起来像 ufunc 一样可用 `out=`**,但本体只是一个转发函数。

#### 4.4.3 源码精读

[diric 的掩码分层 — _basic.py:L176-L191](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_basic.py#L176-L191):三层掩码 `mask1`(非法→nan)/`mask2`(奇点→极限值)/`mask`(正常→公式),典型「数值稳定化」套路。`minval` 阈值在 [L169-L174](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_basic.py#L169-L174) 按浮点精度选取。

[sinc 仅为转发 — _basic.py:L10-L11](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_basic.py#L10-L11):`from numpy import (..., sinc)`。`sinc` 在 `__all__` 里([L71](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_basic.py#L71)),但函数体就是 NumPy 那个。

[bernoulli / euler 转发 _specfun — _basic.py:L1846-L1854 (bernoulli)](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_basic.py#L1846-L1854)、[L1898-L1906 (euler)](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_basic.py#L1898-L1906):都是「校验 `n` 是非负标量整数 → 调 `_specfun.bernob(n1)`/`_specfun.eulerb(n1)` → 切片 `[:n+1]`」。注意为避免 `n<2` 的边界问题,内部把 `n` 抬到至少 2 再算、最后切片(见 [L1850-L1853](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_basic.py#L1850-L1853))。

[softplus 一行实现 — _basic.py:L3507](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_basic.py#L3507):`return np.logaddexp(0, x, **kwargs)`。文档串特别提示 `**kwargs` 走「ufunc 通用参数」(如 `out=`、`where=`),让这个转发函数具备 ufunc 般的调用体验。

#### 4.4.4 代码实践

**实践目标**:看清三类策略——`diric` 的奇点补救、`sinc` 的纯转发、`softplus` 的稳定转发。

**操作步骤**:

```python
import numpy as np
from scipy import special

# 1) diric 在可去奇点处不应得到 nan,而应得到 ±1
x = np.linspace(0, 2*np.pi, 9)        # 含 0, 2π 等 sin(x/2)=0 的点
print("diric at x=0 :", special.diric(np.array([0.0]), 3))   # 期望 +1(极限)

# 2) sinc 就是 numpy.sinc
print("special.sinc is np.sinc ?", special.sinc is np.sinc)  # True
print(special.sinc(np.array([0.0, 0.5])))                    # [1.  0.6366...]

# 3) softplus 大 x 不溢出(对比朴素 log(1+exp(x)))
print("softplus(1000) =", special.softplus(1000))            # 1000.0, 不溢出
import math
try:
    math.log(1 + math.exp(1000))                             # OverflowError
except OverflowError as e:
    print("朴素实现溢出:", e)

# 4) bernoulli/euler 返回长度 n+1 的数组(序列函数)
print("bernoulli(4) =", special.bernoulli(4))   # [1, -0.5, 0.1667, 0, -0.0333]
print("euler(6)     =", special.euler(6))       # [1, 0, -1, 0, 5, 0, -61]
```

**需要观察的现象**:`diric` 在 `x=0` 处返回 `1` 而非 `nan`(奇点补救生效);`special.sinc is np.sinc` 为 `True`(纯转发);`softplus(1000)=1000` 而朴素实现抛 `OverflowError`;`bernoulli(4)` 返回长度 5 的数组。

**预期结果**:`diric(0,3)=1.0`;`sinc is np.sinc` 为 `True`;`softplus(1000)=1000.0`;`bernoulli(4)=[1,-0.5,0.1667,0,-0.0333]`、`euler(6)=[1,0,-1,0,5,0,-61]`(均与各自文档串示例一致)。`diric` 在 `x=0` 的精确极限值标记为**待本地验证**。

#### 4.4.5 小练习与答案

**Q1**:`special.sinc` 是 ufunc 吗?它的代码在哪?
**A**:`numpy.sinc` 本身**不是** ufunc(它是一个普通 NumPy 函数,内部用 `sin(pi*x)/(pi*x)` 实现)。`special.sinc` 只是 `_basic.py` 顶部 `from numpy import sinc` 的转发,没有任何额外实现,纯粹为了让 `sinc` 出现在 `scipy.special` 命名空间里。

**Q2**:`bernoulli(n)` 为什么不能做成 ufunc?
**A**:它输入一个标量 `n`、输出**长度为 n+1** 的数组。ufunc 要求输入与输出形状一致(可广播),而这里的输出长度依赖输入**值**,违反 ufunc 形状契约。这是「序列函数」的共同特征(回想 u2-l1:输出长度依赖参数的做不成 ufunc)。

**Q3**:`softplus` 为什么用 `np.logaddexp(0, x)` 而不是 `np.log(1 + np.exp(x))`?
**A**:对大 `x`,`exp(x)` 会溢出成 `inf`,`log(1+inf)=inf`,结果虽然「碰巧」对但中间过程溢出报错/丢精度;而 `np.logaddexp(0, x)=log(e^0+e^x)`,NumPy 内部用 `max-shift` 技巧先减去最大值再做指数,全程不溢出、精度也更好。这正是 u4-l3 会专门讲的 `logsumexp` 数值稳定思想的最简版本。

## 5. 综合实践

把本讲四条主线串起来,完成下面这个「组合计数小工具」任务:

> 用 `scipy.special` 实现一个函数 `lottery_odds(total, pick)`,返回「从 `total` 个里中恰好 `pick` 个」所需购买的「覆盖所有组合」的注数,并额外给出:
> 1. 该组合数的**精确值**(`exact=True`);
> 2. 该组合数的**浮点近似**(`exact=False`);
> 3. 取一个演示用的 `n`(夹到不超过 `33`),用 `factorialk(n, 2, exact=True)` 算它的双阶乘,并对照 `_FACTORIALK_LIMITS_64BITS[2]=33` 判断该双阶乘是否超过 `int64` 上限。

参考实现骨架(示例代码,非项目原有代码):

```python
import numpy as np
from scipy.special import comb, factorialk

INT64_MAX = np.iinfo(np.int64).max
DOUBLE_FACT_LIMIT_N = 33   # 来自 _FACTORIALK_LIMITS_64BITS[2]

def lottery_odds(total, pick):
    exact   = comb(total, pick, exact=True)          # 精确整数
    approx  = comb(total, pick, exact=False)         # 浮点近似
    n_demo  = min(int(exact), 33)                    # 演示用,夹到双阶乘安全区
    df      = factorialk(n_demo, 2, exact=True)      # n_demo!!
    return {
        "comb_exact":  exact,
        "comb_approx": approx,
        "demo_df_n":   n_demo,
        "demo_df":     df,
        "df_exceeds_int64": df > INT64_MAX,
        "df_n_at_limit":    n_demo >= DOUBLE_FACT_LIMIT_N,
    }

print(lottery_odds(49, 6))
# comb_exact 应为 13983816;demo_df 取 33, 33!!=6332659870762850625, df_n_at_limit=True
```

**验收点**:
- `comb_exact` 是精确整数、`comb_approx` 是 float,二者在数值上接近但前者无精度损失。
- 你能解释为什么 `n_demo` 被夹到 33(因为 `_FACTORIALK_LIMITS_64BITS[2]=33`,超过它 `factorialk` 内部会把 dtype 升级到 `object`)。
- 你能说清 `df_exceeds_int64` 在 `n_demo=33` 时为 `False`、若强行取 `n_demo=35` 会变为 `True` 且 `exact=True` 仍返回正确的 21 位正整数。

## 6. 本讲小结

- `comb`/`perm`/`factorial`/`factorial2`/`factorialk`/`stirling2` 都是**纯 Python 函数**而非 ufunc;核心原因是 `exact=True` 要返回任意精度 Python 大整数、要按 dtype/分支条件做大量判断,这些都违反 ufunc 的「固定类型 + 必然逐元素 + 形状一致」契约。
- `exact` 双轨:`exact=False` 委托给底层 ufunc(`binom`、`poch`、`gamma`),快但浮点;`exact=True` 用 Python 整数算术(`math.comb`、`_range_prod`),精确但更慢且常只支持标量。
- `factorial`/`factorial2`/`factorialk` 共享同一个派发器 `_factorialx_wrapper`,差异仅是连乘步长 `k`;`extend` 参数控制负数处理(`'zero'` 返回 0、`'complex'` 走 Gamma 延拓)且与 `exact=True` 互斥。
- 整数溢出保护由两张表 `_FACTORIALK_LIMITS_64BITS`/`_32BITS` 驱动:`_factorialx_array_exact` 据此把输出 dtype 三级升级(`long`→`int64`→`object`),在溢出**之前**切到 Python 大整数,绝不静默回绕;`_range_prod` 用分治连乘加速大整数运算并对齐多阶乘步长格点。
- 便利函数策略各异:`diric` 纯 Python + 三层掩码做奇点补救;`sinc` 直接转发 `numpy.sinc`;`bernoulli`/`euler` 是外包给 `_specfun` 的序列函数;`softplus` 转发 `np.logaddexp` 借力数值稳定实现。
- 判断「某个 special 函数是不是 ufunc」的通用方法仍是 u2-l1 教的 `isinstance(f, np.ufunc)` 或查 `.types`;本讲的函数都应返回「不是 ufunc」。

## 7. 下一步学习建议

- **继续 `_basic.py`**:u4-l2 会讲同一文件里的**贝塞尔/开尔文函数零点与各阶导数**(`jn_zeros`、`jvp`/`yvp`/`kvp`、`riccati_jn`、`lmbda` 等),它们是「序列型/递推型」纯 Python 函数的又一代表,并大量复用 `_specfun` 与底层 ufunc。
- **对比薄包装**:u4-l4 会讲 `_lambertw.py` 与 `_spherical_bessel.py`,看更复杂的「薄包装 + 装饰器」如何在底层 ufunc 之上做参数预处理(如 `use_reflection` 装饰器的负实轴反射)。
- **数值稳定性专题**:本讲提到的 `softplus`/`logaddexp` 思想,在 u4-l3 的 `logsumexp`/`softmax` 里被发挥到极致(max-shift、符号分离、Array API 兼容),建议紧接着读。
- **回看声明层**:如果你好奇这些纯 Python 函数所委托的 `binom`、`poch`、`gamma`、`_stirling2_inexact` 等 ufunc 是怎么从 C/C++ 内核「声明→生成→注册」出来的,可以回到 U3(尤其 u3-l1 的 `functions.json` 与 u3-l2 的 `_generate_pyx.py`)对照阅读。
