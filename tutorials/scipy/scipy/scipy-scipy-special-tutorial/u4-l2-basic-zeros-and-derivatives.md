# _basic.py(二):贝塞尔/开尔文函数的零点与导数

## 1. 本讲目标

`scipy.special` 里绝大多数函数都是 NumPy ufunc(回顾 u2-l1),可以逐元素、可广播、可批量。但在 `_basic.py` 里有一族函数「长得不像」ufunc:它们的名字常带 `_zeros` 或 `_p`(derivative)后缀,签名里有一个**标量整数**参数,返回的数组长度由这个标量决定。

本讲读完之后,你应该能:

1. 说清楚 `jn_zeros`/`yn_zeros`/`jnyn_zeros`/`kelvin_zeros` 等「零点函数」**为什么不是 ufunc**——它们的输出长度依赖标量整数 `nt`。
2. 读懂导数函数族 `jvp/yvp/ivp/kvp/h1vp/h2vp` 的统一实现 `_bessel_diff_formula`,理解它们如何**复用底层 `jv/yv/...` ufunc**、用递推关系给出任意阶导数,而非数值微分。
3. 认识 `_specfun`(Zhang & Jin《特殊函数计算》)和 `_gufuncs` 这两套 C/C++ 内核在序列型函数中的支撑作用,以及 `riccati_jn/yn`、`lmbda` 这类「一次返回所有阶」的函数为何也不是普通 ufunc。

承接 u4-l1:`_basic.py` 的纯 Python 函数之所以不是 ufunc,u4-l1 给出的理由是「精确整数结果位数无界」;本讲给出**另一组**互不相同的理由(输出结构由标量整数决定),两者合起来就是 `_basic.py` 的存在意义。

---

## 2. 前置知识

在进入源码前,先建立三个直觉。

### 2.1 什么是「零点函数」

很多特殊函数(贝塞尔、开尔文、Airy、误差函数等)在实轴或复平面上有无穷多个零点。「零点函数」就是给定阶数和个数,把前若干个零点算出来返回成数组。例如 `jn_zeros(3, 4)` 返回 3 阶第一类贝塞尔函数 \(J_3(x)\) 的前 4 个正零点。

### 2.2 贝塞尔函数与它的「同族」

贝塞尔方程有一族解,记号如下(都是 `_ufuncs` 里的 ufunc):

| 函数 | 含义 |
|---|---|
| `jv(v,z)` | 第一类贝塞尔 \(J_\nu(z)\) |
| `yv(v,z)` | 第二类贝塞尔 \(Y_\nu(z)\) |
| `iv(v,z)` | 第一类变形贝塞尔 \(I_\nu(z)\) |
| `kv(v,z)` | 第二类变形贝塞尔 \(K_\nu(z)\) |
| `hankel1(v,z)` / `hankel2(v,z)` | 第一/二类汉克尔函数 |

本讲的导数函数 `jvp` 就是「求 \(J_\nu(z)\) 对 \(z\) 的第 n 阶导数」,`yvp` 对应 \(Y_\nu\),以此类推。

### 2.3 ufunc 的一个硬约束:输出形状只能由广播决定

这是本讲的核心前置。NumPy ufunc 是**逐元素**的:输出数组的形状由输入数组广播后唯一确定,与任何标量参数无关(标量参数如 `out=`、`where=` 只能「筛选」输出,不能「改变形状」)。所以——**只要一个函数的输出长度依赖某个标量整数(而非输入数组形状),它就做不成 ufunc**。这正是零点函数与「返回所有阶」函数落回纯 Python 的根本原因。

---

## 3. 本讲源码地图

本讲只精读 `_basic.py`,但会牵出两个被它调用的内核模块。

| 文件 | 在本讲中的角色 |
|---|---|
| [`_basic.py`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_basic.py) | 本讲主角:零点函数、导数函数、`riccati_*`、`lmbda` 的纯 Python 实现 |
| [`_specfun.pyx`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_specfun.pyx) | Zhang & Jin 算法的 Cython 胶水层,提供 `jdzo`/`jyzo`/`klvnzo`/`lamv`/`lamn` 等内核 |
| [`_gufuncs`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_gufuncs.pyi) | 广义 ufunc(gufunc),提供 `_rctj`/`_rcty`,支撑 `riccati_jn/yn` |
| [`_input_validation.py`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_input_validation.py) | 提供 `_nonneg_int_or_fail`,校验「导数阶 `n` 必须是非负整数」 |

调用关系总览(自上而下):

```
Python 公共 API (_basic.py)
   ├── jnyn_zeros / kelvin_zeros / lmbda   ──►  _specfun (Zhang&Jin, .pyx)
   ├── jvp/yvp/ivp/kvp/h1vp/h2vp          ──►  _bessel_diff_formula ──► jv/yv/iv/kv/hankel1/2 (ufunc)
   └── riccati_jn / riccati_yn            ──►  _rctj / _rcty (_gufuncs)
```

---

## 4. 核心概念与源码讲解

### 4.1 Bessel/Kelvin 零点序列函数

#### 4.1.1 概念说明

「零点函数」回答的问题是:「请把某函数的前 `nt` 个零点一次算给我」。比如设计贝塞尔滤波器时,需要知道 \(J_n\) 在哪里穿过零轴。

为什么它**不是 ufunc**?因为输出是一个长度为 `nt` 的一维数组,而 `nt` 是一个**标量整数**——ufunc 无法根据标量参数改变输出数组长度(回顾前置 2.3)。此外,零点的计算不是逐元素的简单公式,而是「在一个区间上定号、求根」的迭代算法,这本身也不适合塞进 ufunc 的逐元素内层循环。因此这类函数只能写成纯 Python 入口 + C/C++ 内核(`_specfun`)的形式。

#### 4.1.2 核心流程

以 `jnyn_zeros(n, nt)` 为例,它一次返回 \(J_n\)、\(J_n'\)、\(Y_n\)、\(Y_n'\) 各自前 `nt` 个零点:

```
1. 校验 n、nt 都是标量整数,且 nt>0。
2. 调用 _specfun.jyzo(abs(n), nt) → 让 Zhang&Jin 内核算出四组零点。
3. 返回长度为 4 的元组 (Jn, Jnp, Yn, Ynp),每组长度均为 nt。
```

其余零点函数都是它的薄封装:`jn_zeros` 取元组第 0 项,`jnp_zeros` 取第 1 项,`yn_zeros` 取第 2 项,`ynp_zeros` 取第 3 项。

#### 4.1.3 源码精读

`jnyn_zeros` 是这一族函数的「总入口」,真正的求根在 `_specfun.jyzo`([_basic.py:312-318](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_basic.py#L312-L318)):

```python
if not (isscalar(nt) and isscalar(n)):
    raise ValueError("Arguments must be scalars.")
if (floor(n) != n) or (floor(nt) != nt):
    raise ValueError("Arguments must be integers.")
if (nt <= 0):
    raise ValueError("nt > 0")
return _specfun.jyzo(abs(n), nt)
```

注意三点:① 入口做的是「标量 + 整数」校验,完全不做逐元素广播;② 调用 `abs(n)`,因为零点位置关于阶数正负号是对称处理的;③ 一行 `_specfun.jyzo` 把全部数值工作下放给内核。

四个薄封装各只占一行([_basic.py:381](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_basic.py#L381)、[_basic.py:444](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_basic.py#L444)、[_basic.py:502](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_basic.py#L502)、[_basic.py:569](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_basic.py#L569)):

```python
def jn_zeros(n, nt):  return jnyn_zeros(n, nt)[0]
def jnp_zeros(n, nt): return jnyn_zeros(n, nt)[1]
def yn_zeros(n, nt):  return jnyn_zeros(n, nt)[2]
def ynp_zeros(n, nt): return jnyn_zeros(n, nt)[3]
```

`_specfun.jyzo` 本身是 Cython 胶水,把工作交给 C 函数 `specfun_jyzo`([_specfun.pyx:240-264](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_specfun.pyx#L240-L264)):

```python
def jyzo(int n, int nt):
    ...
    dims[0] = nt
    rj0 = cnp.PyArray_ZEROS(1, dims, cnp.NPY_FLOAT64, 0)   # 预分配 nt 长输出
    rj1 = cnp.PyArray_ZEROS(1, dims, cnp.NPY_FLOAT64, 0)
    ry0 = cnp.PyArray_ZEROS(1, dims, cnp.NPY_FLOAT64, 0)
    ry1 = cnp.PyArray_ZEROS(1, dims, cnp.NPY_FLOAT64, 0)
    ...
    specfun_jyzo(n, nt, rrj0, rrj1, rry0, rry1)             # 真正的求根
    return rj0, rj1, ry0, ry1
```

可以看到「输出长度由 `nt` 决定」这件事在这里被**显式编码**:`dims[0] = nt` 后预分配四组长度为 `nt` 的数组,再让 C 内核填值。这正是 ufunc 做不到、而 `_specfun` 专门补上的能力。

开尔文(Kelvin)函数零点的套路完全相同,只是把单一函数换成了八合一:`kelvin_zeros(nt)` 一次性返回 \((ber, bei, ker, kei, ber', bei', ker', kei')\) 八组零点,每组调用一次 `_specfun.klvnzo(nt, kd)`,`kd` 用 1~8 区分是哪种开尔文函数([_basic.py:2517-2526](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_basic.py#L2517-L2526)):

```python
return (_specfun.klvnzo(nt, 1), _specfun.klvnzo(nt, 2), ...,
        _specfun.klvnzo(nt, 7), _specfun.klvnzo(nt, 8))
```

而 `ber_zeros`/`bei_zeros`/... 八个单函数([_basic.py:2279-2281](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_basic.py#L2279-L2281))各自只返回 `kelvin_zeros` 的某一组,与 `jn_zeros` 相对于 `jnyn_zeros` 的关系一致。

#### 4.1.4 代码实践

**实践目标**:验证 `jn_zeros` 返回的确实是 \(J_n\) 的零点(函数在这些点上取值接近 0),并直观感受「输出长度由 `nt` 决定、无法逐元素广播」。

**操作步骤**:

```python
# 示例代码
import numpy as np
from scipy.special import jn_zeros, jv

z = jn_zeros(3, 4)              # 求 J_3 的前 4 个正零点
print(z)                        # 长度恰为 4,与输入数组无关
print(jv(3, z))                 # 在这些零点处 J_3 应 ≈ 0

# 感受「不能逐元素」:试着给 n 传数组会怎样
try:
    jn_zeros([3, 4], 4)         # n 不是标量
except ValueError as e:
    print("预期报错:", e)
```

**需要观察的现象**:`jv(3, z)` 的四个值都应在 \(10^{-15}\) 量级(浮点误差),确认它们是真零点;给 `n` 传数组会触发 `ValueError: Arguments must be scalars.`。

**预期结果**:第一组约为 `[6.3801619, 9.76102313, 13.01520072, 16.22346616]`(与 `jn_zeros` 文档示例一致);`jv(3, z)` 接近 `[0, 0, 0, 0]`。零点位置的精确值「待本地验证」,但量级与正零点序列单调递增的特性是确定的。

#### 4.1.5 小练习与答案

**练习 1**:`jnjnp_zeros` 与 `jnyn_zeros` 都依赖 `_specfun`,但前者返回 4 个数组 `zo, n, m, t`,后者返回 4 个零点数组。请说明 `t` 数组的含义。

**参考答案**:`t[l-1]` 标记第 `l` 个零点是 \(J_n\) 的零点(取 0)还是 \(J_n'\) 的零点(取 1);`n` 是对应的阶,`m` 是该阶下的零点序号。`jnjnp_zeros` 把所有阶的零点按**模长排序**混在一起返回,所以需要 `n/m/t` 三个「标签」来还原每个零点的身份(见 [_basic.py:229-233](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_basic.py#L229-L233));而 `jnyn_zeros` 固定阶 `n`,只返回四种函数各自的零点,不需要标签。

**练习 2**:为什么 `kelvin_zeros` 必须返回「8 组」而不是「1 组」?

**参考答案**:开尔文函数本身就是一族 8 个(ber/bei/ker/kei 及其导数),它们的零点相互独立、由不同 `kd`(1~8)区分。把 8 个单函数的零点合并成一个 `kelvin_zeros` 是为了「一次算完」,但每组的长度仍由 `nt` 决定——输出结构依然是「标量 `nt` → 定长数组」,所以同样做不成 ufunc。

---

### 4.2 导数函数族:jvp / yvp / ivp / kvp / h1vp / h2vp

#### 4.2.1 概念说明

`jvp(v, z, n=1)` 计算 \(J_\nu(z)\) 对 \(z\) 的第 `n` 阶导数。同理 `yvp/ivp/kvp/h1vp/h2vp` 分别对应 \(Y_\nu, I_\nu, K_\nu, H^{(1)}_\nu, H^{(2)}_\nu\)。

为什么它们**不是 ufunc**?这次的理由与零点函数不同:`v` 和 `z` 确实可以传数组、确实能逐元素求值,真正破坏 ufunc 资格的是参数 `n`(导数阶)。`n` 是一个**标量整数**,它决定的是「用哪条递推公式、把多少个相邻阶的贝塞尔值组合起来」——这是一个**算法层面的选择**,而非逐元素的数值计算。ufunc 的内层循环是固定的、对每个元素执行的同一份 C 代码,无法让一个标量整数去「改写循环结构」。因此这些函数被实现为:Python 入口负责按 `n` 选择算法,数值工作则下放给已经存在的 `jv/yv/...` ufunc。

关键洞察:`jvp` 不做数值微分!它利用贝塞尔函数的**精确导数递推**,把「第 n 阶导数」表达成「若干个相邻阶贝塞尔函数的线性组合」,然后调用 `jv` ufunc 把这些值逐元素算出来。这样既精确(解析公式,无截断误差)又快(复用批量 ufunc)。

#### 4.2.2 核心流程

统一的导数公式(DLMF 10.6.7)对所有贝塞尔族成立,差别只在「相位」`phase`。对第一/二类与汉克尔函数 `phase=-1`,对变形贝塞尔 `phase=+1`:

\[
L_\nu^{(n)}(z)=\frac{1}{2^{n}}\sum_{k=0}^{n}(\text{phase})^{k}\binom{n}{k}L_{\nu-n+2k}(z)
\]

特例 `n=1`:

\[
J_\nu'(z)=\tfrac{1}{2}\bigl(J_{\nu-1}(z)-J_{\nu+1}(z)\bigr),\qquad
I_\nu'(z)=\tfrac{1}{2}\bigl(I_{\nu-1}(z)+I_{\nu+1}(z)\bigr)
\]

这正是大家熟悉的贝塞尔导数恒等式。`K` 因为定义里带符号,`kvp` 额外乘一个 \((-1)^n\)。

实现流程:

```
1. _nonneg_int_or_fail(n) 校验 n 是非负整数。
2. 若 n==0:直接返回底函数本身(如 jv(v,z)),零开销短路。
3. 若 n>0:调用 _bessel_diff_formula(v, z, n, 底函数, phase),
   它用 for 循环累加若干次「底函数在 v±k 阶」的值。
```

#### 4.2.3 源码精读

整个导数族共享一个核心函数 `_bessel_diff_formula`([_basic.py:803-814](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_basic.py#L803-L814)):

```python
def _bessel_diff_formula(v, z, n, L, phase):
    # from AMS55.
    # L(v, z) = J(v, z), Y(v, z), H1(v, z), H2(v, z), phase = -1
    # L(v, z) = I(v, z) or exp(v*pi*i)K(v, z), phase = 1
    # For K, you can pull out the exp((v-k)*pi*i) into the caller
    v = asarray(v)
    p = 1.0
    s = L(v-n, z)                       # k=0 项:binom(n,0)*phase^0 * L(v-n)
    for i in range(1, n+1):
        p = phase * (p * (n-i+1)) / i   # 递推更新 p = binom(n,i)*phase^i
        s += p*L(v-n + i*2, z)          # k=i 项:L(v-n+2i)
    return s / (2.**n)                  # 末尾除以 2^n
```

逐行对照公式:

- 注释开头 `# from AMS55`(指《Handbook of Mathematical Functions》即 Abramowitz & Stegun)点明出处;
- `s = L(v-n, z)` 初始化求和为 `k=0` 的项 \(L_{\nu-n}(z)\);
- 循环里 `p` 用递推 \(p_i = \text{phase}\cdot p_{i-1}\cdot(n-i+1)/i\) 维护 \(\binom{n}{i}\cdot\text{phase}^i\),避免每次重算组合数;
- 每轮把 \(L_{\nu-n+2i}(z)\) 加进去,索引 `v-n + i*2` 正是公式里的 \(\nu-n+2k\);
- 最后 `s / (2.**n)` 对应公式里的 \(1/2^n\)。

注意 `L` 是被当成 ufunc 使用的:`L(v-n, z)` 里 `v` 已经 `asarray`,所以这一行是对整个 `v`/`z` 数组**逐元素、批量**求值的——这是导数函数虽非 ufunc、却仍享受 ufunc 速度的原因。

六个公共导数函数都只是这个公式的薄分支,差别仅在「底函数」与「相位」([_basic.py:888-892](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_basic.py#L888-L892) 是 `jvp`,其余同构):

```python
def jvp(v, z, n=1):
    n = _nonneg_int_or_fail(n, 'n')
    if n == 0:
        return jv(v, z)                          # 零阶导 = 原函数
    else:
        return _bessel_diff_formula(v, z, n, jv, -1)   # phase=-1
```

其余五个的分支(均位于 [_basic.py:895-1274](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_basic.py#L895-L1274)):

| 函数 | 底函数 | 相位 | 额外处理 |
|---|---|---|---|
| `jvp`  | `jv`      | -1 | — |
| `yvp`  | `yv`      | -1 | — |
| `h1vp` | `hankel1` | -1 | — |
| `h2vp` | `hankel2` | -1 | — |
| `ivp`  | `iv`      | +1 | — |
| `kvp`  | `kv`      | +1 | 整体乘 `(-1)**n`([_basic.py:1055](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_basic.py#L1055)) |

`kvp` 的那一行 `return (-1)**n * _bessel_diff_formula(v, z, n, kv, 1)` 正是注释所说「For K, you can pull out the exp((v-k)*pi*i) into the caller」的体现——把 \(K\) 定义里的相位因子提到调用点统一处理。

#### 4.2.4 代码实践(本讲主实践)

**实践目标**:阅读 `jvp` 与 `_bessel_diff_formula`,说明 `n>0` 时第 n 阶导数如何用递推 + 调用 `jv` ufunc 算出;再用有限差分 `np.diff` 验证 `jvp(0, x, 1)` 确实等于 \(J_0\) 的数值导数。

**操作步骤**:

```python
# 示例代码
import numpy as np
from scipy.special import jvp, jv

# 1) 思路分析:n=1 时 _bessel_diff_formula(v,z,1,jv,-1)
#    = ( jv(v-1,z) - jv(v+1,z) ) / 2
#    即 J_v'(z) = 0.5*(J_{v-1} - J_{v+1}),标准的贝塞尔导数恒等式。

# 2) 用有限差分验证 jvp(0, x, 1) ≈ d/dx J_0(x)
x = np.linspace(0.1, 3.0, 50)
dx = x[1] - x[0]

analytic = jvp(0, x, 1)                 # 解析一阶导(递推得到)
forward  = np.diff(jv(0, x)) / dx       # 前向差分,长度 49,对齐到 x[:-1]

# 比较(前向差分有 O(dx) 截断误差,这里 dx≈0.059,应很接近)
max_err = np.max(np.abs(analytic[:-1] - forward))
print("最大误差:", max_err)
print("是否在 1e-2 以内:", max_err < 1e-2)

# 额外:验证恒等式 J_0'(x) = -J_1(x)
print("jvp(0,x,1) 与 -jv(1,x) 是否一致:", np.allclose(analytic, -jv(1, x)))
```

**需要观察的现象**:
- `max_err` 应在 \(10^{-2}\) 量级(因为前向差分是 \(O(dx)\),`dx≈0.059`),说明 `jvp` 给出的解析导数与数值导数吻合;
- 把 `np.diff` 换成中心差分(如 `np.gradient(jv(0,x), dx)`,误差 \(O(dx^2)\))误差会再降几个数量级——这反向证明 `jvp` 是**解析**结果而非数值微分;
- `jvp(0, x, 1)` 与 `-jv(1, x)` 应 `allclose` 成立,印证恒等式 \(J_0'=-J_1\)。

**预期结果**:`max_err < 1e-2` 成立,`allclose(analytic, -jv(1, x))` 成立;`jvp(0, 1, 1)` 的值应为 `-0.44005058574493355`(与 `jvp` 文档示例一致)。不同 NumPy 版本下 `allclose` 的默认容差可能略有差异,「待本地验证」精确位数,但定性结论稳定。

#### 4.2.5 小练习与答案

**练习 1**:为什么 `jvp` 在 `n==0` 时直接 `return jv(v, z)`,而不是统一走 `_bessel_diff_formula`?

**参考答案**:`n=0` 意为「零阶导数 = 原函数」,直接返回 `jv` 是零开销短路,既避免无意义的循环与组合数计算,也避免在 `n=0` 时 `_bessel_diff_formula` 里 `L(v-n, z)` 退化(此时 `2.**0=1`、循环不执行,结果恰好是 `L(v,z)`,虽然数学上等价,但多了一次 `asarray` 和函数调用)。这是典型的「公共快路径单独优化」。

**练习 2**:对 `kvp`,为什么需要 `(-1)**n` 而 `ivp` 不需要?

**参考答案**:`I` 与 `K` 虽都是变形贝塞尔,但 \(K_\nu\) 在解析定义中含 \(e^{\pm i\nu\pi}/2\) 因子,其导数递推相比 \(I\) 多一个相位翻转。代码注释明确写道对 `K` 要「把 \(\exp((v-k)\pi i)\) 提到调用者」处理,具体表现就是 `kvp` 整体乘 \((-1)^n\)。`ivp` 用同样的 `phase=+1` 公式但不加额外符号。

**练习 3**:给 `jvp` 的 `n` 传一个浮点数(如 `1.5`)会发生什么?为什么?

**参考答案**:`_nonneg_int_or_fail` 在 `strict=True`(默认)下用 `operator.index(n)`,对非整数抛 `TypeError`,提示「n must be a non-negative integer」。这正是把「导数阶」挡在 ufunc 之外的同一道闸门——`n` 必须是标量整数,因为它选择的是递推公式的项数,不存在「1.5 阶导数」的解析定义。

---

### 4.3 _specfun 内核与「一次返回所有阶」的序列函数

#### 4.3.1 概念说明

除了零点函数,`_basic.py` 还有一类「序列函数」:`riccati_jn/yn`、`lmbda` 等。它们的特点是:给定最大阶 `n`,**一次性返回 0..n 所有阶**的函数值(及导数)。例如 `riccati_jn(n, x)` 返回 \(x\cdot j_0(x), x\cdot j_1(x), \dots, x\cdot j_n(x)\) 及其导数两个数组,长度均为 `n+1`。

为什么不是普通 ufunc?和零点函数同源:**输出多了一条「阶」轴,其长度 `n+1` 由标量整数 `n` 决定**,超出 ufunc「输出形状=输入广播形状」的能力。但与零点函数不同的是,这类「逐点返回定长向量」的语义**可以**用 NumPy 的广义 ufunc(gufunc)表达——`riccati_*` 正是走 gufunc 路线;而 `lmbda` 走的是更传统的 `_specfun` 路线。两条路线对照阅读,正好展现 special 对「序列输出」的两种工程化处理。

`_specfun` 模块是 Zhang & Jin《Computation of Special Functions》(1996)书附 Fortran 程序的 Cython 移植,`_basic.py` 里凡注释提到 `Zhang, Shanjie and Jin, Jianming` 的函数,几乎都最终落到这里(回顾 u3-l4 的后端版图)。

#### 4.3.2 核心流程

`riccati_jn(n, x)`(gufunc 路线):

```
1. 校验 n、x 是标量;n 为非负整数(允许 0)。
2. 预分配两个长度 n+1 的 float64 数组 jn、jnp。
3. 调用 gufunc _rctj(x, out=(jn, jnp)),用向后递推(DLMF 10.51.1)一次填满所有阶。
4. 截取并返回 (jn[:n+1], jnp[:n+1])。
```

`lmbda(v, x)`(`_specfun` 路线):

```
1. 校验 v、x 是标量,v>0。
2. 拆出整数阶 n=int(v) 与小数残量 v0=v-n。
3. 若 v 非整数:调 _specfun.lamv(v1, x);否则调 _specfun.lamn(v1, x)。
4. 截取并返回 (vl[:n+1], dl[:n+1])。
```

两者都用「内核把一个超长数组填满,Python 层再切片 [:n+1]」的模式来处理「长度由标量决定」这件事——与 4.1.3 里 `_specfun.jyzo` 的 `dims[0]=nt` 异曲同工。

#### 4.3.3 源码精读

`riccati_jn`([_basic.py:1277-1330](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_basic.py#L1277-L1330))走 gufunc 路线,关键在 `_rctj(x, out=(jn, jnp))`:

```python
def riccati_jn(n, x):
    ...
    n = _nonneg_int_or_fail(n, 'n', strict=False)
    if (n == 0):
        n1 = 1
    else:
        n1 = n

    jn = np.empty((n1 + 1,), dtype=np.float64)
    jnp = np.empty_like(jn)

    _rctj(x, out=(jn, jnp))              # 广义 ufunc:标量 x → 向量 (jn, jnp)
    return jn[:(n+1)], jnp[:(n+1)]
```

注意 `n1 = max(n, 1)`:递推内核至少需要 1 阶才能稳定启动(向后递推从高阶往低阶算),所以 `n=0` 时仍算到 1 阶再切片成 `[:1]`。`_rctj`/`_rcty` 来自 `_gufuncs`(在文件头 [_basic.py:18](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_basic.py#L18) 导入)。`riccati_yn`([_basic.py:1333-1387](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_basic.py#L1333-L1387)) 结构完全对称,只是用 `_rcty` 且按 DLMF 注释走「向前递推」。

`lmbda`([_basic.py:2095-2142](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_basic.py#L2095-L2142))走 `_specfun` 路线,核心是按「v 是否整数」分派两个不同内核:

```python
def lmbda(v, x):
    ...
    n = int(v)
    v0 = v - n
    if (n < 1):
        n1 = 1
    else:
        n1 = n
    v1 = n1 + v0
    if (v != floor(v)):
        vm, vl, dl = _specfun.lamv(v1, x)     # 非整数阶:lamv
    else:
        vm, vl, dl = _specfun.lamn(v1, x)     # 整数阶:lamn
    return vl[:(n+1)], dl[:(n+1)]
```

Jahnke-Emden Λ 函数定义为 \(\Lambda_\nu(x)=\Gamma(\nu+1)\,J_\nu(x)/(x/2)^\nu\)。这里 `lamv`/`lamn` 两个内核分别处理非整数与整数阶(`lamn` 用序列递推更稳),Python 层只做标量校验、分派与切片。

把 4.1 与 4.3 合起来看:`_specfun`(及 `_gufuncs`)在 special 里承担的角色,正是**补齐 ufunc 做不到的「标量整数 → 定长输出」语义**——无论是返回 `nt` 个零点,还是返回 `n+1` 个阶的函数值。

#### 4.3.4 代码实践

**实践目标**:对比 `riccati_jn`(gufunc)与 `lmbda`(`_specfun`)两条序列输出路径,观察它们都返回「长度由标量阶决定的数组」,并用球贝塞尔恒等式交叉验证。

**操作步骤**:

```python
# 示例代码
import numpy as np
from scipy.special import riccati_jn, spherical_jn

# riccati_jn: 一次返回 0..n 阶的 x*j_n(x) 及其导数
jn_arr, jnp_arr = riccati_jn(4, 2.0)
print("长度:", jn_arr.shape)            # (5,) = n+1
# 验证 jn_arr[k] = x * spherical_jn(k, x)
x = 2.0
print("与 x*spherical_jn 一致:", np.allclose(jn_arr, x * spherical_jn(range(5), x)))
```

**需要观察的现象**:`jn_arr` 长度为 `n+1=5`,与输入数组形状无关;`x*spherical_jn(...)` 与 `riccati_jn` 的第一路输出一致(因 Riccati-Bessel 定义为 \(x\,j_n(x)\))。

**预期结果**:`jn_arr.shape == (5,)`,`allclose` 成立。`lmbda` 因依赖 Gamma/Bessel 复合,具体数值「待本地验证」,但「输出长度 = n+1」的结构性结论是确定的。

#### 4.3.5 小练习与答案

**练习 1**:`riccati_jn` 里为什么 `n==0` 时仍令 `n1=1`,而不是直接 `n1=0`?

**参考答案**:向后递推算法(DLMF 10.51.1)需要一个非零的起始高阶才能稳定地往低阶递推;若 `n=0` 就只算 0 阶,递推无法启动。所以代码在 `n=0` 时仍按 `n1=1` 算到 1 阶,最后用 `jn[:(n+1)]` 切成只含 0 阶的 `[:1]`。这是「数值稳定性优先于极小优化」的典型取舍。

**练习 2**:`riccati_*` 用 gufunc(`_rctj/_rcty`),而 `lmbda` 用 `_specfun`。请说明两者的共同点与差异。

**参考答案**:共同点是输出都含一条「阶」轴,长度由标量 `n` 决定,因此都不是普通 ufunc,都靠「内核填满超长数组 + Python 切片 [:n+1]」实现。差异在于:`_rctj/_rcty` 是 NumPy 广义 ufunc(gufunc),能用 `out=` 直接写入预分配数组、理论上可对多个 `x` 广播;`lamv/lamn` 是传统 `_specfun` 内核,只接受标量 `x`,需要 Python 层循环才能处理多点。这正反映 special 对「序列输出」的两种工程化路线。

---

## 5. 综合实践

把本讲三个模块串起来,完成一个「贝塞尔滤波器零点 + 导数曲线 + 序列可视化」的小任务。

**任务**:对 3 阶第一类贝塞尔函数 \(J_3(x)\):

1. 用 `jn_zeros` 与 `jnp_zeros` 分别取前 4 个零点和前 4 个极值点(导数零点);
2. 用 `jvp(3, x, 1)` 与 `jvp(3, x, 2)` 画出 \(J_3'\) 与 \(J_3''\) 曲线,验证 \(J_3\) 的零点恰好是 `jvp(3, x, 0)` 的零点、而 \(J_3\) 的极值恰好是 `jvp(3, x, 1)` 的零点;
3. 用 `riccati_jn(3, x)` 对比「逐阶 ufunc `spherical_jn`」与「一次返回所有阶的 gufunc」两种拿数据方式的差异。

**参考框架**:

```python
# 示例代码
import numpy as np
from scipy.special import jn_zeros, jnp_zeros, jvp, riccati_jn, spherical_jn

roots  = jn_zeros(3, 4)      # J_3 的零点
peaks  = jnp_zeros(3, 4)     # J_3' 的零点 = J_3 的极值
x = np.linspace(0, 18, 400)

# 任务 2:零点对应 jvp(3,x,0)=jv(3,x) 过零;极值对应 jvp(3,x,1) 过零
print("J_3 在 roots 处 ≈ 0:", np.allclose(jvp(3, roots, 0), 0, atol=1e-10))
print("J_3' 在 peaks 处 ≈ 0:", np.allclose(jvp(3, peaks, 1), 0, atol=1e-10))

# 任务 3:同一 x,两种方式取 0..3 阶球贝塞尔
x0 = 2.5
rj, rjp = riccati_jn(3, x0)                       # gufunc,一次出 4 阶
uf = np.array([spherical_jn(k, x0) for k in range(4)])  # ufunc,Python 循环 4 次
print("Riccati/b x 路径与逐阶 ufunc 一致:",
      np.allclose(rj, x0 * uf))
```

**验收**:两个 `allclose` 应为 `True`;`riccati_jn` 返回长度 4 的数组(`n+1=4`),与逐阶调用 `spherical_jn` 结果一致(差一个 `x0` 因子,因 Riccati 定义含 `x`)。这个任务把「零点(4.1)」「导数(4.2)」「一次返回所有阶(4.3)」三件事在同一条 \(J_3\) 曲线上联动起来。精确数值「待本地验证」,但三条结构性结论(零点/极点对齐、长度=n+1、两种取数等价)是确定的。

---

## 6. 本讲小结

- `_basic.py` 里的零点函数(`jn_zeros`/`yn_zeros`/`jnyn_zeros`/`kelvin_zeros`)做不成 ufunc,因为**输出长度由标量整数 `nt` 决定**,而 ufunc 的输出形状只能由输入广播决定;真正求根在 `_specfun.jyzo`/`klvnzo`。
- 导数函数族 `jvp/yvp/ivp/kvp/h1vp/h2vp` 共享 `_bessel_diff_formula`,用 DLMF 10.6.7 的解析递推把第 n 阶导数表达成相邻阶贝塞尔的线性组合,**复用底层 `jv/yv/...` ufunc 批量求值**,既精确(无截断误差)又快;破坏其 ufunc 资格的是标量整数 `n`——它选的是算法,不是逐元素运算。
- 公式 \(L_\nu^{(n)}(z)=\frac{1}{2^n}\sum_{k=0}^{n}(\text{phase})^k\binom{n}{k}L_{\nu-n+2k}(z)\),`phase=-1` 用于 J/Y/H,`+1` 用于 I/K,`kvp` 额外乘 `(-1)**n`。
- 序列函数 `riccati_jn/yn` 走 gufunc(`_rctj/_rcty`),`lmbda` 走 `_specfun`(`lamv/lamn`);两者都返回「长度 `n+1`、由标量阶决定」的输出,印证 special 用两种路线补齐 ufunc 的能力空白。
- 贯穿本讲的设计模式:「Python 入口做标量校验与分派 + C/C++ 内核(`_specfun`/`_gufuncs`)填满超长数组 + Python 层切片 [:n+1]」——这是 special 处理「标量整数决定输出结构」问题的通用套路。

---

## 7. 下一步学习建议

- 顺读 **u4-l3 `_logsumexp.py`**:那里是「跨元素聚合而做不成 ufunc」的另一个典型(`logsumexp` 需要先求最大值再求和),与本讲「输出形状由标量决定」合起来,你就集齐了 `_basic.py` 系列纯 Python 函数脱离 ufunc 的全部理由。
- 想深入 `_specfun` 的算法来源,可读 **u3-l4 C/C++ 后端版图**,了解 Zhang & Jin 代码与 xsf 的关系;想了解 gufunc 注册,可顺 **u5-l3 `_multiufuncs.py`** 看 `MultiUFunc` 如何把多输出聚合。
- 数值验证方面,这些零点/导数函数的参考值都用 mpmath 高精度比对,可配合 **u9-l2 `_mptestutils.py`** 阅读测试方法。
