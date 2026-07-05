# _basic.py(二):贝塞尔/开尔文函数的零点与导数

## 1. 本讲目标

本讲承接 [u4-l1](`_basic.py` 组合与阶乘类函数),继续精读同一个文件 `scipy/special/_basic.py`,但聚焦其中另一大类**序列型函数**——贝塞尔(Bessel)函数的零点、各阶导数、Riccati–Bessel 函数、Jahnke–Emden Lambda 函数,以及开尔文(Kelvin)函数的零点。

学完后你应该能够:

1. 说清楚「为什么 `jn_zeros`、`jvp`、`lmbda` 这些函数不做成 ufunc」,并能用「输出长度依赖参数、必然逐元素」这两条判据去判断同类函数。
2. 读懂 `jvp/yvp/ivp/kvp/h1vp/h2vp` 这六个导数函数,理解它们如何复用一个共同的递推公式 `_bessel_diff_formula`,以及 `kvp` 为何多一个 `(-1)**n` 因子。
3. 看懂 `jn_zeros` 这一类零点函数如何统一委托给 `jnyn_zeros`,再下钻到 `_specfun.jyzo`——也就是 Zhang & Jin《Computation of Special Functions》的内核经 Cython 暴露出来的接口。

## 2. 前置知识

在进入源码前,先用三段话补齐本讲需要的数学与工程直觉。

**(A) 贝塞尔函数的两套「阶」与「变量」。** 第一类贝塞尔函数 \(J_\nu(z)\)、第二类 \(Y_\nu(z)\)、修正第一类 \(I_\nu(z)\)、修正第二类 \(K_\nu(z)\)、两类 Hankel 函数 \(H_\nu^{(1,2)}(z)\),都带两个参数:阶 \(\nu\) 和自变量 \(z\)。其中 `jv/iv/yv/kv/hankel1/hankel2` 这些**值函数**在 scipy.special 里都是 ufunc(见 u2-l1),输入 \((\nu, z)\)、输出一个标量,可批量、可广播。本讲关心的是另外两类「派生」需求:

- **零点**:给定阶 \(n\),求 \(J_n(x)=0\) 的前 `nt` 个正根。根的**个数**由 `nt` 决定,不是输入数组形状决定的。
- **导数**:给定阶 \(\nu\) 与自变量 \(z\),求 \(d^n J_\nu/dz^n\)。这里 \(n\) 是「第几阶导数」,是一个附加的整数参数。

**(B) ufunc 的「形状契约」与序列函数的冲突。** 回顾 u2-l1:ufunc 必须「必然逐元素」,即输出的每个元素只依赖输入的对应元素,且输入输出形状一致(经广播后)。但:

- 零点函数输出 `nt` 个根,长度由一个**整数参数** `nt` 决定,跟输入数组的形状毫无关系。
- 序列函数(如 `lmbda(v, x)` 一次返回 \( \Lambda_{v_0}(x),\dots,\Lambda_v(x) \) 一串)输出长度由 `v` 的小数/整数部分决定。

这两类都违背 ufunc 契约,所以只能写成**纯 Python 函数**,在内部调用底层 ufunc 或 `_specfun` 内核来「凑」出序列。这正是 u2-l2 提到的「带 `_zeros`/`_seq` 后缀、按警告段落单独标记」的函数家族。

**(C) 相邻阶递推与高阶导数。** 贝塞尔函数满足两条相邻阶的关系(DLMF 10.6.1):

\[
J_{\nu-1}(z) + J_{\nu+1}(z) = \frac{2\nu}{z}J_\nu(z),\qquad
J_{\nu-1}(z) - J_{\nu+1}(z) = 2 J_\nu'(z).
\]

把第二条反复套用,就能把「第 \(n\) 阶导数」写成「阶在 \(\nu-n\) 到 \(\nu+n\) 之间的一组**值函数**的线性组合」。这是本讲导数函数的核心数学依据——它让 `jvp` 等函数不必自己实现求导,只需复用已有的 `jv` ufunc。

## 3. 本讲源码地图

本讲几乎全部代码都在一个文件里:

| 文件 | 角色 |
| --- | --- |
| [_basic.py](_basic.py) | 纯 Python 包装层。本讲涉及的零点、导数、序列函数都在这里。 |
| [_specfun.pyx](_specfun.pyx) | Cython 胶水层。把 Zhang & Jin 的 C++ 内核(`xsf/specfun/specfun.h` 等)`cimport` 进来,包装成 `_specfun.jyzo`、`_specfun.cyzo`、`_specfun.klvnzo`、`_specfun.lamn/lamv` 等 Python 可调用对象。 |
| [tests/test_basic.py](tests/test_basic.py) | 测试。其中 `test_jvp` 用一行漂亮的等价公式验证了导数的递推实现,本讲会拿它做佐证。 |

一句话调用链:

```
_basic.py (纯 Python)
   ├── 零点函数 ──► _specfun.jyzo / cyzo / klvnzo / jdzo  (Cython 胶水)
   │                    └──► xsf/specfun/specfun.h  (Zhang & Jin C++ 内核)
   ├── 导数函数 ──► _bessel_diff_formula ──► jv / yv / iv / kv / hankel1/2 (ufunc)
   └── riccati_* ──► _gufuncs._rctj / _rcty  (广义 ufunc)
```

> 注意区分两个名字相近的东西:`_specfun`(带下划线的 Cython 扩展模块)是本讲的内核入口;而 `specfun.py`(不带下划线)是一个**已弃用别名**,将在 v2.0.0 移除(见 u1-l4),不要混淆。

## 4. 核心概念与源码讲解

### 4.1 序列型函数:为什么零点与导数不做成 ufunc

#### 4.1.1 概念说明

在 u4-l1 我们已经见过一种「不是 ufunc」的函数——`comb`/`factorial`/`factorialk`,它们因为要支持任意精度大整数(`exact=True`)而必须用纯 Python。本讲遇到的是**第二种**「不是 ufunc」的原因:**输出形状不由输入数组决定**。

ufunc 的铁律(见 u2-l1)是「固定个数的输入 → 固定个数输出,逐元素求值,形状一致」。这条铁律排除了两类常见需求:

1. **「给我前 `nt` 个根」**:输出长度 = `nt`,是一个标量参数,不是输入数组里某个轴的长度。
2. **「给我从 0 阶到 n 阶的全部值」**:输出长度 = `n+1`,同样由整数参数决定。

只要输出长度脱离了输入形状,就没法塞进 ufunc 的「逐元素循环」里。于是 `_basic.py` 把它们写成普通 Python 函数:外层做参数校验和长度裁剪,内层要么调用底层 ufunc(`jv` 等),要么调用 `_specfun` 提供的专用内核。

#### 4.1.2 核心流程

判断一个 special 函数「为什么不是 ufunc」,可以套用这张决策表:

| 判据 | 例子 | 结论 |
| --- | --- | --- |
| 输出长度 = 整数参数 `nt` | `jn_zeros(n, nt)` 返回长度 `nt` 的数组 | 序列函数 |
| 输出长度 = 阶参数 + 常数 | `lmbda(v, x)` 返回长度 `int(v)+1` | 序列函数 |
| 多返回值且长度可变 | `y0_zeros(nt)` 返回 `(zeros, derivs)` | 序列函数 |
| 逐元素、形状一致 | `jv(v, z)` | ufunc ✓ |

我们也用代码确认一下:在类型桩 [_ufuncs.pyi](_ufuncs.pyi) 里**搜不到** `jvp`、`jn_zeros`、`lmbda`、`kelvin_zeros`(只能搜到无关的 `tklmbda`)——这从机器侧证明了它们不在 ufunc 名单里。

#### 4.1.3 源码精读

先看 [_basic.py 顶部的导入](_basic.py#L13-L20),它揭示了三类内部依赖:

```python
from . import _ufuncs
from ._ufuncs import (mathieu_a, mathieu_b, iv, jv, gamma, rgamma,
                      psi, hankel1, hankel2, yv, kv, poch, binom,
                      _stirling2_inexact)

from ._gufuncs import _lqn, _lqmn, _rctj, _rcty
from ._input_validation import _nonneg_int_or_fail
from . import _specfun
```

- `jv/yv/iv/kv/hankel1/hankel2` 是**值函数 ufunc**,导数函数会复用它们。
- `_rctj/_rcty` 来自 `_gufuncs`(广义 ufunc),供 `riccati_jn/yn` 使用。
- `_specfun` 是 Cython 内核模块,零点函数和 `lmbda` 会用。
- `_nonneg_int_or_fail` 是公共的「非负整数校验」工具,导数函数用它来卡参数 `n`。

#### 4.1.4 代码实践

**实践目标**:用机器手段确认本讲的函数都不是 ufunc。

**操作步骤**:在已安装 SciPy 的环境里运行(示例代码):

```python
import numpy as np
import scipy.special as sc
for name in ["jv", "jvp", "jn_zeros", "lmbda", "kelvin_zeros", "yvp"]:
    f = getattr(sc, name)
    print(f"{name:14s} isinstance(ufunc)={isinstance(f, np.ufunc)}")
```

**需要观察的现象**:`jv` 是 ufunc(`True`),而 `jvp / jn_zeros / lmbda / kelvin_zeros / yvp` 全是 `False`。

**预期结果**:只有 `jv` 一行为 `True`,其余为 `False`。若环境未装 SciPy,标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**:用 `sc.jv.types` 看 `jv` 这个 ufunc 支持哪些类型环;再想一下,为什么 `jvp` 没有 `.types` 属性?

**参考答案**:`jv.types` 返回形如 `['dd->d', 'DD->D', ...]` 的类型签名(双精度实/复等)。`jvp` 是普通 Python 函数,内部把工作转交给 `jv`,自己并不向 NumPy 注册任何类型环,所以没有 `.types` 属性。

---

### 4.2 Bessel 零点序列:jnyn_zeros 与 _specfun.jyzo

#### 4.2.1 概念说明

工程上经常需要贝塞尔函数的零点:例如圆膜振动方程的本征频率就由 \(J_n\) 的零点决定,光纤/波导的截止条件也依赖它们。但「求第 \(k\) 个零点」不像「在 \(x\) 处求值」那样能逐元素算——它需要用渐近近似给一个初始猜测,再用牛顿法迭代精化,是**算法密集**而非**逐元素**的工作。这类算法已由 Zhang & Jin 在《Computation of Special Functions》(1996) 第 5 章实现,scipy 把它编译进 `xsf/specfun`,再用 Cython 暴露成 `_specfun.jyzo` 等接口。

scipy.special 提供了一组「零点函数」,它们的设计哲学是:**只留一个真正干活的入口,其余全是取下标**。

#### 4.2.2 核心流程

四个「常被用到」的零点函数

- `jn_zeros(n, nt)`:\(J_n(x)\) 的前 `nt` 个正零点。
- `jnp_zeros(n, nt)`:\(J_n'(x)\)(对 \(x\) 求导)的前 `nt` 个正零点。
- `yn_zeros(n, nt)`:\(Y_n(x)\) 的前 `nt` 个正零点。
- `ynp_zeros(n, nt)`:\(Y_n'(x)\) 的前 `nt` 个正零点。

它们**全都委托**给 `jnyn_zeros(n, nt)`,后者一次性算出 \(J_n,J_n',Y_n,Y_n'\) 四族零点并返回一个四元组;每个具体函数只取其中的某一列:

```
jnyn_zeros(n, nt)  ──► _specfun.jyzo(abs(n), nt)
                          └─► xsf::specfun::jyzo  (Zhang & Jin 内核)
   返回 (rj0, rj1, ry0, ry1)   # Jn, Jn', Yn, Yn' 四族前 nt 个零点

jn_zeros   = jnyn_zeros(...)[0]   # rj0
jnp_zeros  = jnyn_zeros(...)[1]   # rj1
yn_zeros   = jnyn_zeros(...)[2]   # ry0
ynp_zeros  = jnyn_zeros(...)[3]   # ry1
```

这样「四个 API 共享一次计算」,既省了重复调用内核,又对外保持了直观的「一个函数一件事」接口。

另外还有一组**复零点**函数 `y0_zeros` / `y1_zeros`(以及 `y1p_zeros`),它们针对 \(Y_0(z)\)、\(Y_1(z)\) 在**复平面**上的零点(实零点是子集),走的是另一条内核 `_specfun.cyzo`(modified Newton 迭代)。

#### 4.2.3 源码精读

真正的「中央入口」是 [`jnyn_zeros`](_basic.py#L236-L318)。去掉文档字符串后,它的实现只有参数校验 + 一行委托:

```python
def jnyn_zeros(n, nt):
    ...
    if not (isscalar(nt) and isscalar(n)):
        raise ValueError("Arguments must be scalars.")
    if (floor(n) != n) or (floor(nt) != nt):
        raise ValueError("Arguments must be integers.")
    if (nt <= 0):
        raise ValueError("nt > 0")
    return _specfun.jyzo(abs(n), nt)
```

注意两点:① `n`、`nt` 必须是**标量整数**(再次印证「不是 ufunc」——ufunc 不会这样卡标量);② 传给内核的是 `abs(n)`,因为零点分布对阶的符号不敏感。

四个「取下标」的薄包装极其简洁,以 [`jn_zeros`](_basic.py#L321-L381) 为例,其函数体只有一行 [`return jnyn_zeros(n, nt)[0]`](_basic.py#L381)。`jnp_zeros`、`yn_zeros`、`ynp_zeros` 同理,分别取 `[1]`、`[2]`、`[3]`(见 [jnp_zeros 实现](_basic.py#L444)、[yn_zeros 实现](_basic.py#L502)、[ynp_zeros 实现](_basic.py#L569))。

复零点函数 [`y0_zeros`](_basic.py#L572-L645) 走另一条路:

```python
def y0_zeros(nt, complex=False):
    ...
    kf = 0
    kc = not complex          # kc=True 只取实零点,kc=False 只取复零点
    return _specfun.cyzo(nt, kf, kc)
```

`kf` 是「函数选择码」(0 选 \(Y_0\),`y1_zeros` 里 [kf=1](_basic.py#L725) 选 \(Y_1\)),`kc` 控制返回实零点还是复零点。这正是 Zhang & Jin 用一个 C++ 例程同时服务多个变体的典型写法。

再往下看 Cython 胶水层 [`_specfun.jyzo`](_specfun.pyx#L240-L264):

```python
def jyzo(int n, int nt):
    """...wrapper for the function 'specfun_jyzo'."""
    ...
    rj0 = cnp.PyArray_ZEROS(1, dims, cnp.NPY_FLOAT64, 0)
    ...
    specfun_jyzo(n, nt, rrj0, rrj1, rry0, rry1)
    return rj0, rj1, ry0, ry1
```

它的职责很机械:**预分配 4 个长度为 `nt` 的 double 数组 → 把裸指针交给 C++ 内核 `xsf::specfun::jyzo` → 把填好的数组返回**。C++ 内核的声明在文件顶部的 [`cdef extern from "xsf/specfun/specfun.h"`](_specfun.pyx#L26-L41) 区块,例如 `void specfun_jyzo 'xsf::specfun::jyzo'(...)`(第 34 行)。这种「Cython 只做指针搬运、C++ 干算法」的分工,正是 u1-l2 所讲「Cython 胶水层」的典型样貌。

> 旁注:还有一个 [`jnjnp_zeros(nt)`](_basic.py#L195-L233) 函数,它把 \(J_n\) 与 \(J_n'\) 的零点**按数值大小混合排序**后一次性返回(还附带每个零点对应的阶 `n`、序号 `m`、以及 `t` 标志「这是 \(J_n\) 的零点还是 \(J_n'\) 的零点」)。它走 `_specfun.jdzo` 而非 `jyzo`,且对 `nt` 有 `<=1200` 的硬上限——这是内核内部缓冲区策略决定的(见 `jdzo` 文档串里那张「nt 与所需数组大小」对照表)。

#### 4.2.4 代码实践

**实践目标**:验证「四个零点函数确实是 `jnyn_zeros` 的列切片」,并复算一个零点的正确性。

**操作步骤**(示例代码):

```python
import numpy as np
import scipy.special as sc

n, nt = 1, 3
# 一次性算四族
rj0, rj1, ry0, ry1 = sc.jnyn_zeros(n, nt)
# 单独算一族,应当完全相等
assert np.allclose(rj0, sc.jn_zeros(n, nt))
assert np.allclose(rj1, sc.jnp_zeros(n, nt))

# 复算验证:在 J1 的零点处,J1 应当(近似)为 0
z = sc.jn_zeros(1, 1)[0]
print("zero =", z, " J1(z) =", sc.jv(1, z))
```

**需要观察的现象**:`assert` 不报错;`J1(z)` 应是一个极接近 0 的数(量级 \(10^{-16}\))。

**预期结果**:`jn_zeros(1, ...)` 的第一个零点约 `3.8317`,`jv(1, 3.8317...) ≈ 0`。若环境未装 SciPy,标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**:`jn_zeros(n, nt)` 的文档特别强调「区间 \((0,\infty)\) 排除了 \(n>0\) 时位于 \(x=0\) 的那个零点」。结合 `jyzo` 只返回 `nt` 个**正**零点的事实,解释为什么不把 0 也算进去。

**参考答案**:0 是否为 \(J_n\) 的零点取决于阶 \(n\):\(J_n(0)=0\) 当且仅当 \(n>0\)。如果内核把 0 一律算进去,对不同阶 \(n\) 的「前 `nt` 个正零点」语义就会不一致(有时含 0 有时不含),调用者反而难用。所以内核只返回严格为正的零点,把「0 也是根」这件数学事实留给文档说明。

**练习 2**:`y0_zeros(4, complex=False)` 与 `y0_zeros(4, complex=True)` 返回的数组长度是否相同?为什么?

**参考答案**:长度相同(都是 `nt=4`),但内容不同:`complex=False` 返回 4 个**实**零点(虚部为 0),`complex=True` 返回 4 个**复**零点(实部为负、虚部为正)。它们是同一函数在不同区域的两批零点,由内核 `cyzo` 的参数 `kc` 切换。

---

### 4.3 导数函数家族:_bessel_diff_formula 与 jvp/yvp/ivp/kvp/h1vp/h2vp

#### 4.3.1 概念说明

`jvp/yvp/ivp/kvp/h1vp/h2vp` 六个函数分别计算 \(J,Y,I,K,H^{(1)},H^{(2)}\) 对**自变量 \(z\)** 的第 \(n\) 阶导数。注意:这里的 `n` 是「几阶导数」,不是贝塞尔函数的「阶 \(\nu\)」——函数签名是 `jvp(v, z, n=1)`,其中 `v` 是阶、`z` 是自变量、`n` 是导数阶数。

它们**完全可以**做成逐元素 ufunc——输入 \((\nu, z)\) 形状一致,输出同形状。但实现者选择了纯 Python,原因有二:① 多了一个**整数**参数 `n`(导数阶),不同 `n` 对应不同的递推展开,塞进 ufunc 的固定类型环里不自然;② 可以用一个**统一公式**复用现有的值函数 ufunc,代码极简、零重复。

#### 4.3.2 核心流程

核心是一个把「第 \(n\) 阶导数」化为「相邻阶值函数线性组合」的恒等式(源自 DLMF 10.6.7,代码注释里写 `from AMS55`):

\[
\frac{d^n}{dz^n} L_\nu(z) = \frac{1}{2^n}\sum_{i=0}^{n} c_i\, L_{\nu - n + 2i}(z),\qquad
c_i = (\text{phase})^{\,i}\binom{n}{i}.
\]

其中 `phase` 区分两类函数:

- **phase = -1** 适用于 \(J, Y, H^{(1)}, H^{(2)}\):系数正负交替 \((+,-,+,-,\dots)\)。\(n=1\) 时退化为熟知的
  \[
  J_\nu'(z) = \tfrac{1}{2}\bigl[J_{\nu-1}(z) - J_{\nu+1}(z)\bigr].
  \]
- **phase = +1** 适用于 \(I\) 与「带上 \(e^{\nu\pi i}\) 的 \(K\)」:系数全正。

对 \(K_\nu(z)\) 单独多一个整体因子 \((-1)^n\)(因为 \(K\) 的递推关系与 \(I\) 差一个符号),见 `kvp` 的实现。

于是六个导数函数的实现**完全同构**,差别只在两处:传给公式的「值函数 \(L\)」与「phase」,以及 `kvp` 的额外 \((-1)^n\):

| 函数 | \(L\) | phase | 额外因子 |
| --- | --- | --- | --- |
| `jvp` | `jv` | -1 | — |
| `yvp` | `yv` | -1 | — |
| `h1vp` | `hankel1` | -1 | — |
| `h2vp` | `hankel2` | -1 | — |
| `ivp` | `iv` | +1 | — |
| `kvp` | `kv` | +1 | \((-1)^n\) |

还有一条捷径:`n=0` 时「零阶导数」就是函数自身,直接返回 `jv(v,z)` 等,不必走公式。

#### 4.3.3 源码精读

公共公式的实现 [`_bessel_diff_formula`](_basic.py#L803-L814) 只有 12 行,但值得逐行读:

```python
def _bessel_diff_formula(v, z, n, L, phase):
    # from AMS55.
    # L(v, z) = J(v, z), Y(v, z), H1(v, z), H2(v, z), phase = -1
    # L(v, z) = I(v, z) or exp(v*pi*i)K(v, z), phase = 1
    v = asarray(v)
    p = 1.0
    s = L(v-n, z)                      # i=0 项:L_{ν-n}, 系数 p=1
    for i in range(1, n+1):
        p = phase * (p * (n-i+1)) / i   # 滚动计算组合数 C(n,i) 并乘上 phase
        s += p*L(v-n + i*2, z)          # 叠加 L_{ν-n+2i} 项
    return s / (2.**n)                  # 整体除以 2^n
```

要点:

- 变量 `p` 在循环里**滚动地**算出 \(c_i = \text{phase}^{\,i}\binom{n}{i}\),避免每次都从头算阶乘——利用了 \(\binom{n}{i}=\binom{n}{i-1}\cdot(n-i+1)/i\)。
- `L` 是个**函数参数**,把 `jv`、`yv` 等值函数 ufunc 当一等公民传进来。这正是「六个导数函数共享一份实现」的关键:把「变的部分」(用哪个值函数、phase 符号)参数化。
- `v = asarray(v)` 把阶参数转成数组,后续 `v - n + i*2` 的广播运算才成立——这样 `jvp([0,1,2], z, 1)` 一次算多阶,本质是借了底层 `jv` ufunc 的广播能力。
- 最后 `/ (2.**n)` 对应公式里的 \(1/2^n\)。

六个导数函数的函数体几乎是「复制粘贴」。以 [`jvp`](_basic.py#L817-L892) 为例,去掉文档串后:

```python
def jvp(v, z, n=1):
    n = _nonneg_int_or_fail(n, 'n')        # n 必须是非负整数
    if n == 0:
        return jv(v, z)                     # 零阶导数 = 函数本身
    else:
        return _bessel_diff_formula(v, z, n, jv, -1)
```

`yvp`(实现在 [_basic.py#L895-L974](_basic.py#L895-L974))、`ivp`([_basic.py#L1058-L1136](_basic.py#L1058-L1136))、`h1vp`([_basic.py#L1139-L1205](_basic.py#L1139-L1205))、`h2vp`([_basic.py#L1208-L1274](_basic.py#L1208-L1274)) 结构完全一致,只是把 `jv` 换成对应值函数、`-1` 换成对应 phase。

唯一的「特例」是 [`kvp`](_basic.py#L977-L1055),它的非零分支多了一个 \((-1)^n\):

```python
    else:
        return (-1)**n * _bessel_diff_formula(v, z, n, kv, 1)
```

这是因为修正贝塞尔函数 \(K_\nu\) 满足的递推 \(K_\nu'(z) = -\tfrac12[K_{\nu-1}(z)+K_{\nu+1}(z)]\),相比 \(I\) 多了一个负号;高阶导数则累计成 \((-1)^n\)。

最后看测试如何「反向佐证」这套公式。[tests/test_basic.py 的 `test_jvp`](tests/test_basic.py#L3514-L3517) 用 \(n=1\) 的特例直接对比:

```python
def test_jvp(self):
    jvprim = special.jvp(2,2)
    jv0 = (special.jv(1,2)-special.jv(3,2))/2
    assert_allclose(jvprim, jv0, atol=1.5e-10, rtol=0)
```

把 \(n=1,\text{phase}=-1\) 代入公式:\(J_2'(2)=\tfrac12[J_1(2)-J_3(2)]\),正好就是 `(jv(1,2)-jv(3,2))/2`。这条断言用三行代码就把「递推公式」和「公式的代码实现」对上了。

#### 4.3.4 代码实践

**实践目标**(对应任务规格):阅读 `jvp` 的实现,说明它如何对 `n>0` 用递推公式计算第 \(n\) 阶导数;再用有限差分 `np.diff` / `np.gradient` 在小区间上验证 `jvp(0, x, 1)` ≈ 数值导数。

**操作步骤**:

1. 打开 [_basic.py:817](_basic.py#L817),确认 `jvp` 在 `n==0` 时直接返回 `jv(v,z)`,在 `n>0` 时调用 `_bessel_diff_formula(v, z, n, jv, -1)`。
2. 打开 [_basic.py:803](_basic.py#L803),手算 `n=1` 的展开:`s = L(v-1,z) + (-1)*L(v+1,z)`,再除以 2,即 \(\tfrac12[J_{\nu-1}-J_{\nu+1}]\)。
3. 用有限差分独立验证(示例代码):

```python
import numpy as np
import scipy.special as sc

# 解析一阶导数(jvp) vs 中心差分数值导数
x = np.linspace(0.05, 3.0, 601)
h = x[1] - x[0]
J0 = sc.jv(0, x)

ana = sc.jvp(0, x, 1)                     # 解析:J0'(x)
num = np.gradient(J0, h)                  # 数值:中心差分

print("max |解析 - 数值| =", np.max(np.abs(ana - num)))
```

**需要观察的现象**:`jvp(0, x, 1)` 与 `np.gradient(jv(0,x))` 两条曲线几乎重合;最大误差应远小于 `J0` 的振幅(量级 \(10^{-5}\)),受限于有限差分的二阶截断误差,而非解析公式。

**预期结果**:最大绝对差约在 \(10^{-5}\sim10^{-6}\)(随 `h` 变化)。把 `h` 减半,误差应约下降到 1/4——这是中心差分二阶收敛的标志,反证 `jvp` 给的是「真」导数。若环境未装 SciPy,标注「待本地验证」。

> 说明:本实践用的是源码阅读 + 数值对照,未修改任何源码。

#### 4.3.5 小练习与答案

**练习 1**:把 `_bessel_diff_formula(v, z, n=2, L=jv, phase=-1)` 的求和手算展开,写出 \(J_\nu''(z)\) 的表达式。

**参考答案**:`i=0` 系数 \(+1\),`i=1` 系数 \(-2\),`i=2` 系数 \(+1\),整体除以 \(2^2=4\):
\[
J_\nu''(z)=\tfrac14\bigl[J_{\nu-2}(z)-2J_\nu(z)+J_{\nu+2}(z)\bigr].
\]

**练习 2**:为什么 `kvp` 比 `jvp` 多一个 `(-1)**n`,而 `ivp` 没有?

**参考答案**:`ivp` 用 \(I_\nu\),它的一阶导数递推 \(I_\nu'=\tfrac12[I_{\nu-1}+I_{\nu+1}]\) 系数全正(phase=+1),高阶导数没有额外符号。`kvp` 用 \(K_\nu\),其一阶导数递推 \(K_\nu'=-\tfrac12[K_{\nu-1}+K_{\nu+1}]\) 多一个负号;每次求导都多乘一个 \(-1\),\(n\) 阶导数累计成 \((-1)^n\)。

---

### 4.4 其他序列函数与 _specfun 内核:riccati、lmbda、kelvin_zeros

#### 4.4.1 概念说明

除了 4.2 的零点族和 4.3 的导数族,`_basic.py` 里还有几类「一次返回一串」的序列函数,它们同样不是 ufunc,但实现策略各有不同,值得对比:

- **`riccati_jn(n, x)` / `riccati_yn(n, x)`**:Riccati–Bessel 函数 \(x j_n(x)\) 及其导数,一次返回从 0 阶到 \(n\) 阶的全部值与一阶导数。用**球贝塞尔**的向后/向前递推(DLMF 10.51.1)计算,内核是 `_gufuncs` 里的 `_rctj` / `_rcty`——注意这里走的是**广义 ufunc(gufunc)** 而非 `_specfun`。
- **`lmbda(v, x)`**:Jahnke–Emden Lambda 函数 \(\Lambda_\nu(x)=\Gamma(\nu+1)J_\nu(x)/(x/2)^\nu\),一次返回从 \(v_0=v-\lfloor v\rfloor\) 到 \(v\) 的所有值与导数。整数阶走 `_specfun.lamn`,非整数阶走 `_specfun.lamv`。
- **`kelvin_zeros(nt)`**:开尔文函数 \((ber,bei,ker,kei)\) 及其导数共 8 个函数的前 `nt` 个零点,内核 `_specfun.klvnzo`。

#### 4.4.2 核心流程

这几类函数的共同工程模式是:**Python 层做长度/类型校验与切片,Cython/C++ 内核一次性把整串算出来**。

```
riccati_jn(n, x) ──► 预分配长度 n+1 的数组 ──► _rctj(out=(jn, jnp))  (gufunc)
lmbda(v, x)      ──► 判 v 是否整数 ──► _specfun.lamn 或 _specfun.lamv
kelvin_zeros(nt) ──► 调 8 次 _specfun.klvnzo(nt, kd),kd=1..8
```

注意 `riccati_*` 与其余两者的区别:它把内核当成「填入预分配 `out` 数组」的 gufunc(`_rctj(x, out=(jn, jnp))`),而 `lmbda`/`kelvin_zeros` 则让 `_specfun` 内核**自己**分配并返回数组。这两种风格在 `_basic.py` 里并存。

#### 4.4.3 源码精读

[`riccati_jn`](_basic.py#L1277-L1330) 的实现(去掉文档串):

```python
def riccati_jn(n, x):
    if not (isscalar(n) and isscalar(x)):
        raise ValueError("arguments must be scalars.")
    n = _nonneg_int_or_fail(n, 'n', strict=False)
    if (n == 0):
        n1 = 1
    else:
        n1 = n

    jn = np.empty((n1 + 1,), dtype=np.float64)
    jnp = np.empty_like(jn)

    _rctj(x, out=(jn, jnp))
    return jn[:(n+1)], jnp[:(n+1)]
```

要点:① 即使请求 `n=0`,也至少算到 `n1=1` 再切片到 `[:1]`——因为底层 gufunc 至少需要两个点才能启动递推;② 用 `out=(jn, jnp)` 把**预分配**的数组交给内核填充,避免内核内部再分配;③ `_rctj` 来自 [`from ._gufuncs import ... _rctj, _rcty`](_basic.py#L18),是广义 ufunc。`riccati_yn`(实现在 [_basic.py#L1333-L1387](_basic.py#L1333-L1387))结构相同,只是用 `_rcty` 并按文档说明采用**向前递推**(而 `jn` 用向后递推,这是球贝塞尔函数数值稳定性的经典选择)。

[`lmbda`](_basic.py#L2095-L2142) 展示了「按阶是否整数分两条内核」的模式:

```python
def lmbda(v, x):
    ...
    if not (isscalar(v) and isscalar(x)):
        raise ValueError("arguments must be scalars.")
    if (v < 0):
        raise ValueError("argument must be > 0.")
    n = int(v)
    v0 = v - n
    ...
    if (v != floor(v)):
        vm, vl, dl = _specfun.lamv(v1, x)      # 非整数阶
    else:
        vm, vl, dl = _specfun.lamn(v1, x)      # 整数阶
    return vl[:(n+1)], dl[:(n+1)]
```

整数阶用 `lamn`(整数递推),非整数阶用 `lamv`(任意阶)。两者都是 `_specfun` 的 Cython 包装,背后同样是 Zhang & Jin 的 C++ 例程(见 [`_specfun.pyx` 中 lamn/lamv 的 wrapper](_specfun.pyx#L281-L318) 及顶部 extern 声明 `specfun_lamn`/`specfun_lamv`)。

[`kelvin_zeros`](_basic.py#L2496-L2526) 则用「一个内核配选择码」的模式连续调用 8 次:

```python
def kelvin_zeros(nt):
    ...
    return (_specfun.klvnzo(nt, 1),
            _specfun.klvnzo(nt, 2),
            _specfun.klvnzo(nt, 3),
            _specfun.klvnzo(nt, 4),
            _specfun.klvnzo(nt, 5),
            _specfun.klvnzo(nt, 6),
            _specfun.klvnzo(nt, 7),
            _specfun.klvnzo(nt, 8))
```

第二个参数 `kd=1..8` 分别对应 `(ber, bei, ker, kei, ber', bei', ker', kei')` 八个函数的零点——和 4.2 里 `cyzo` 的 `kf`、`kc` 选择码是同一套设计哲学:一个通用 C++ 例程服务多个变体,Python 侧用循环或枚举去取。

#### 4.4.4 代码实践

**实践目标**:对比 `riccati_jn` 与 `kelvin_zeros` 两种序列函数的返回形状,直观体会「输出长度由参数决定」。

**操作步骤**(示例代码):

```python
import numpy as np
import scipy.special as sc

# riccati_jn:返回长度 n+1 的 (jn, jnp)
jn, jnp = sc.riccati_jn(3, 1.0)
print("riccati_jn(3, 1.0): jn.shape =", jn.shape, " jnp.shape =", jnp.shape)

# kelvin_zeros:返回 8 个长度 nt 的数组组成的元组
z = sc.kelvin_zeros(4)
print("kelvin_zeros(4): len(tuple) =", len(z),
      " each shape =", z[0].shape)
```

**需要观察的现象**:`riccati_jn(3, 1.0)` 返回的 `jn`、`jnp` 形状都是 `(4,)`(0..3 阶);`kelvin_zeros(4)` 返回 8 个形状 `(4,)` 的数组。输入都是标量,输出长度完全由整数参数(`n` 或 `nt`)决定。

**预期结果**:打印 `jn.shape=(4,), jnp.shape=(4,)`;`len(tuple)=8, each shape=(4,)`。若环境未装 SciPy,标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**:`riccati_jn(0, x)` 在代码里为何要先令 `n1=1` 再切片 `jn[:1]`,而不是直接令 `n1=0`?

**参考答案**:底层 gufunc `_rctj` 基于球贝塞尔的递推关系,至少需要两个相邻阶(0 和 1)才能启动计算。若 `n1=0` 则数组长度为 1,递推无法进行;所以代码在 `n==0` 时也先把 `n1` 抬到 1,算完再用 `[:1]` 截取用户真正要的那一阶。

**练习 2**:`lmbda` 在 `v` 为整数与非整数时分别调用 `lamn` 和 `lamv` 两个不同内核。从「整数阶可整段递推、任意阶不能」的角度,解释为什么要分两条路。

**参考答案**:整数阶 \(\Lambda_0,\Lambda_1,\dots,\Lambda_n\) 之间有规范的三项递推,`lamn` 可以从一个初值出发把整串高效地递推出来。非整数阶序列里的阶是 \(v_0, 1+v_0, \dots, n+v_0\)(步长仍为 1),起点是任意实数,递推初值与稳定性处理都不同,需要 `lamv` 这套为任意阶准备的算法。两条路对应两种数值特性,故分开实现。

---

## 5. 综合实践

把本讲三块内容(零点、导数、`_specfun` 内核)串起来,做一个「自洽的数值小实验」:

> **任务**:对第一类贝塞尔函数 \(J_0(x)\),同时用**零点函数**和**导数函数**两条独立路径,确认它们描述的是同一条曲线的几何特征。

**步骤**(示例代码):

```python
import numpy as np
import scipy.special as sc

# (A) 零点路径:jn_zeros 直接给出 J0 的零点
zeros = sc.jn_zeros(0, 4)
print("J0 的前 4 个正零点:", zeros)

# (B) 导数路径:jvp(0, zeros, 1) 应等于 J0'(零点),非零
deriv_at_zeros = sc.jvp(0, zeros, 1)
print("J0' 在这些零点处的值:", deriv_at_zeros)

# (C) 交叉验证:用 jvp 找 J0 的极值点(导数为零)≈ jnp_zeros 给的零点
crit = sc.jnp_zeros(0, 4)
print("J0' 的前 4 个正零点(极值点):", crit)
print("在这些极值点处 J0 的值:", sc.jv(0, crit))

# (D) 调用链回顾:确认 jn_zeros 与 jnp_zeros 同源
rj0, rj1, _, _ = sc.jnyn_zeros(0, 4)
assert np.allclose(rj0, zeros) and np.allclose(rj1, crit)
print("OK: jn_zeros / jnp_zeros 都是 jnyn_zeros 的列切片")
```

**需要观察的现象与预期结果**:

- (A) `J0` 的前 4 个正零点约为 `[2.4048, 5.5201, 8.6537, 11.7915]`。
- (B) `J0'` 在零点处不为 0(约为 \(\mp 0.5191,\pm 0.3403,\dots\),正负交替),说明零点处曲线斜率非零——符合「简单零点」。
- (C) `J0'` 的前 4 个零点(即 `J0` 的极值点)约为 `[3.8317, 7.0156, 10.1735, 13.3237]`,在这些点处 `J0` 的值正负交替且振幅递减。
- (D) 最后的 `assert` 通过,验证「四朵姐妹花共用 `jnyn_zeros`」的调用链。

> 这个任务把「零点函数(4.2)」「导数函数(4.3)」「统一入口 `jnyn_zeros`(4.2)」三件事用同一条曲线串了起来,并间接体现了 `_specfun.jyzo`(与 4.4 同类的 `_specfun` 内核)在背后的一次性计算。若环境未装 SciPy,全部标注「待本地验证」。

## 6. 本讲小结

- `jn_zeros / jnp_zeros / yn_zeros / ynp_zeros` 这类零点函数**不是 ufunc**,因为输出长度由整数参数 `nt` 决定,违背 ufunc 的「形状一致、逐元素」契约。
- 四个零点函数共享一个中央入口 [`jnyn_zeros`](_basic.py#L236-L318),后者一次性算出 \(J_n,J_n',Y_n,Y_n'\) 四族零点,具体函数只取列切片;底层委托 `_specfun.jyzo`(Zhang & Jin 内核)。
- `jvp/yvp/ivp/kvp/h1vp/h2vp` 六个导数函数共享一个递推公式 [`_bessel_diff_formula`](_basic.py#L803-L814),把「第 \(n\) 阶导数」化为相邻阶值函数的线性组合 \(\frac{1}{2^n}\sum(\text{phase})^i\binom{n}{i}L_{\nu-n+2i}(z)\),复用 `jv` 等值函数 ufunc。
- `kvp` 是唯一特例,多一个 \((-1)^n\) 因子,源于 \(K_\nu\) 递推的符号差异;`n=0` 时所有导数函数直接返回值函数本身。
- `riccati_jn/yn`(走 gufunc `_rctj/_rcty`)、`lmbda`(走 `_specfun.lamn/lamv`)、`kelvin_zeros`(走 `_specfun.klvnzo`)是另三类序列函数,共同模式是「Python 校验+切片,Cython/C++ 内核一次算出整串」,常以选择码(如 `kd=1..8`)复用同一内核。
- 贯穿全讲的判据:**输出长度是否依赖参数而非输入形状**——若是,则只能写成纯 Python 序列函数,即便内部复用 ufunc。

## 7. 下一步学习建议

- **横向对比「不是 ufunc」的两种原因**:把本讲(输出长度依赖参数)与 u4-l1(`exact=True` 要任意精度大整数)并排复习,你就掌握了 special 里所有非 ufunc 函数的两条根因,以后看到新函数能立刻归类。
- **下钻 `_specfun` 与 gufunc 内核**:本讲多次提到 `_specfun.jyzo`、`_gufuncs._rctj`。下一阶段可读 [`_specfun.pyx`](_specfun.pyx) 顶部的 [`cdef extern from "xsf/specfun/specfun.h"`](_specfun.pyx#L19-L41) 区块,以及 u3-l4 提到的 `xsf/specfun/`(Zhang & Jin 的现代 C++ 转写),看 Cython 如何用 `PyArray_DATA` 拿裸指针、把工作交给 `nogil` 的 C++ 例程。
- **接续 `_basic.py` 之旅**:u4-l3 会转向 `_logsumexp.py` 的数值稳定实现(Array API 兼容),与本讲的「序列函数」形成对照——前者是「逐元素但需精心重排」的纯 Python 函数,可对照体会两种「不做成 ufunc」的工程动机。
- **数学背景延伸**:想深入贝塞尔递推与零点算法的读者,推荐 DLMF 第 10 章(代码注释里反复引用的 10.6.7、10.29.5、10.51.1)以及 Zhang & Jin《Computation of Special Functions》第 5、6 章(零点求解的牛顿迭代初值策略)。
