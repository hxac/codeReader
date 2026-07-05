# _mptestutils.py:基于 mpmath 的高精度参考验证

## 1. 本讲目标

学完本讲后,你应当能够:

- 说清楚为什么 `scipy.special` 要引入 `mpmath` 作为「黄金参考」,以及这套验证的整体思路(采样 → 高精度计算 → 折回双精度 → `rtol`/`atol` 比对)。
- 读懂 `_mptestutils.py` 中 `Arg`、`FixedArg`、`ComplexArg`、`IntArg` 四个采样类,理解它们如何「在实轴/复平面/整数轴上同时覆盖极小、中等、极大各数量级」。
- 读懂 `MpmathData` / `assert_mpmath_equal` 这条比对管线,理解 `dps` 管理、`mpf2float` 的精度折回技巧、以及 `rtol`/`atol` 的判定公式。
- 认识一组用来「驯服 mpmath 怪癖」的辅助装饰器(`exception_to_nan`、`inf_to_nan`、`time_limited`、`trace_args`),并能解释 `test_mpmath.py` 是如何把上述零件组装成上千个参数化数值比对的。

> 关于「Hyp 类」的说明:本讲的规划主题里提到一个处理超几何参数域的 `Hyp` 类,但**真实源码里并不存在 `Hyp` 类**。超几何族函数(如 `hyp1f1`、`hyp2f1`、`hyperu`)是通过向 `assert_mpmath_equal` 传入多个 `Arg`/`ComplexArg` 规格来采样的,见 4.2.3 的 `test_hyp1f1`。本讲以源码为准,不编造该类。

## 2. 前置知识

- **特殊函数与 ufunc**:承接 [u1-l1](u1-l1-project-overview.md),`scipy.special` 绝大多数函数是逐元素求值的双精度(IEEE-754 `float64`/`complex128`)ufunc。
- **数据驱动测试**:承接 [u9-l1](u9-l1-testutils-funcdata.md),`_testutils.py` 的 `assert_func_equal` / `FuncData` 提供了「在点集上比对函数值」的基础设施(`rtol` 默认 `5*eps`、`atol` 默认 `5*tiny`)。本讲的 `MpmathData.check()` 最终正是委托给它。
- **任意精度算术(mpmath)**:`mpmath` 是一个纯 Python 的任意精度数学库,可用 `mpmath.mp.dps`(decimal places,十进制有效位数)设定精度,把 `erf`、`gamma`、`hyp2f1` 等算到几十甚至上百位。它比双精度慢得多,所以只在测试里当「裁判」,不进运行时。
- **`rtol`/`atol` 判定**:一个结果 `res` 相对参考值 `std` 被判为通过,当且仅当
  \[ |\,\text{res} - \text{std}\,| \;\le\; \text{atol} + \text{rtol}\cdot|\text{std}| \]
  即绝对容差加相对容差。这一公式同时出现在 NumPy 的 `assert_func_equal` 与本讲的 `mp_assert_allclose` 中。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [_mptestutils.py:L1-L454](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_mptestutils.py#L1-L454) | 本讲主角。定义采样类(`Arg`/`FixedArg`/`ComplexArg`/`IntArg`)、比对管线(`MpmathData`/`assert_mpmath_equal`)、mpmath 怪癖工具(`mpf2float`/`exception_to_nan`/`time_limited` 等)与高精度比对 `mp_assert_allclose`。 |
| [tests/test_mpmath.py:L1-L2141](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/tests/test_mpmath.py#L1-L2141) | 消费方。两千余行测试,把 `_mptestutils` 的零件组装成对 `airy`、`bessel*`、`hyp*`、`gamma` 等数百个函数的系统比对。本讲重点读其中的 `TestSystematic` 类与若干代表性用例。 |
| [_testutils.py:L55-L85](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_testutils.py#L55-L85) | 被委托方。`assert_func_equal`/`FuncData` 提供「点集 + 容差」的最终数值断言;`check_version`/`MissingModule` 让 mpmath 可选。 |

## 4. 核心概念与源码讲解

### 4.1 mpmath 作为「黄金参考」的验证思路

#### 4.1.1 概念说明

`scipy.special` 的内核(Cephes、xsf、Boost.Math,见 [u3-l4](u3-l4-cpp-backend-landscape.md))输出的是双精度浮点数,有效数字约 15–17 位。要回答「这个双精度结果到底对不对」,最稳妥的办法是找一个**精度远高于双精度**的独立实现来当裁判——这就是 `mpmath`。这种「用一个更精确的独立来源校验」的做法,在数值库领域常被称为「黄金参考(golden reference)」验证。

整体思路三步走:

1. **采样**:在被测函数的定义域里生成一批「有代表性」的输入点。
2. **高精度计算**:用 `mpmath` 在很高精度(如 50 位、120 位)下算出参考值。
3. **折回 + 比对**:把高精度参考值折回双精度,与 SciPy 的双精度输出在 `rtol`/`atol` 下比对。

> 为什么 mpmath 是「可选依赖」?它慢、且不是 SciPy 运行时所必需。所以测试代码用 `try: import mpmath except ImportError: ...` 包起来,并通过 `check_version(mpmath, 'x.y')` 装饰器在版本不够或缺失时**跳过**而非报错(见 [tests/test_mpmath.py:L25-L32](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/tests/test_mpmath.py#L25-L32))。

#### 4.1.2 核心流程

`MpmathData.check()` 是这条管线的总控,伪代码如下:

```text
check():
    np.random.seed(1234)                 # 固定随机种子,保证可复现
    argarr = get_args(arg_spec, n)       # 1) 采样:n 个输入点
    保存 mpmath 当前 dps/prec
    for dps in dps_list(默认 [20]):      # 2) 逐档精度尝试
        mpmath.mp.dps = dps
        把 mpmath 输出经 pytype() 折回 double  # 见 4.1.3
        try:
            assert_func_equal(           # 3) 委托 _testutils 做容差比对
                scipy_func,
                lambda *a: pytype(mpmath_func(*map(mptype, a))),
                argarr, rtol=..., atol=..., ...)
            break                        # 通过则不再提高精度
        except AssertionError:
            若已是最高档精度,则重抛
    恢复 mpmath 原 dps/prec
```

三个要点:(a) 用 `try/finally` 包住 dps 修改,保证测试结束后全局精度被还原;(b) 默认只试 `dps=20` 一档,失败即失败,除非调用方显式给了更高 `dps` 列表;(c) 真正的逐点比对并不在本文件实现,而是委托给 `_testutils.assert_func_equal`——本模块只负责「造点 + 喂 mpmath + 折回」。

#### 4.1.3 源码精读

`dps` 的保存/恢复与默认值,以及 `dps_list` 的取法([_mptestutils.py:L231-L237](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_mptestutils.py#L231-L237)):

```python
old_dps, old_prec = mpmath.mp.dps, mpmath.mp.prec
try:
    if self.dps is not None:
        dps_list = [self.dps]
    else:
        dps_list = [20]
```

把 mpmath 结果折回双精度的关键——**复数与实数两套转换函数**([_mptestutils.py:L243-L256](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_mptestutils.py#L243-L256)):

```python
if np.issubdtype(argarr.dtype, np.complexfloating):
    pytype = mpc2complex
    def mptype(x):
        return mpmath.mpc(complex(x))
else:
    def mptype(x):
        return mpmath.mpf(float(x))
    def pytype(x):
        if abs(x.imag) > 1e-16*(1 + abs(x.real)):
            return np.nan        # 参考值虚部不为零 → 视为 NaN
        else:
            return mpf2float(x.real)
```

注意两点工程细节:**输入也走 `mptype` 转换**,注释说「用原生 mpmath 类型作输入,某些情况下精度更好」;**实数参考值若带了可观虚部就返回 `NaN`**,这等价于声明「该点不在实值定义域内,跳过」。

`mpf2float` 是个非平凡的小函数([_mptestutils.py:L308-L317](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_mptestutils.py#L308-L317)):

```python
def mpf2float(x):
    """
    Convert an mpf to the nearest floating point number. Just using
    float directly doesn't work because of results like this:
    with mp.workdps(50):
        float(mpf("0.99999999999999999")) = 0.9999999999999999
    """
    return float(mpmath.nstr(x, 17, min_fixed=0, max_fixed=0))
```

直接 `float(mpf(...))` 会因为 `mpf` 内部二进制表示的舍入而得到「看起来少一位」的结果;改为先转成 17 位十进制字符串(`nstr`,关闭定点记法),再 `float()`,才能拿到最接近的 `float64`。这正是「黄金参考」必须严谨对待的细节——参考值本身不能先输一程。

委托给 `assert_func_equal` 的最终断言([_mptestutils.py:L262-L275](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_mptestutils.py#L262-L275)):

```python
assert_func_equal(
    self.scipy_func,
    lambda *a: pytype(self.mpmath_func(*map(mptype, a))),
    argarr,
    vectorized=False, rtol=self.rtol, atol=self.atol,
    ignore_inf_sign=self.ignore_inf_sign,
    distinguish_nan_and_inf=self.distinguish_nan_and_inf,
    nan_ok=self.nan_ok, param_filter=self.param_filter)
```

其中 `vectorized=False` 很关键:`mpmath_func` 是逐标量的纯 Python 函数,不能像 ufunc 那样吃数组,所以告诉 `assert_func_equal`「别试图向量化,逐点调」。

#### 4.1.4 代码实践

**实践目标**:亲手跑通「mpmath 高精度 → 折回 double → 比对 SciPy」的最小闭环,验证 `special.erf(1.5)`。

**操作步骤**(示例代码,非项目原有):

```python
# 示例代码:用 mpmath 作高精度参考,校验 special.erf(1.5)
import mpmath
import scipy.special as sc

# 1) 用 workdps 上下文管理器临时切到 50 位精度(退出自动还原,等价 check() 的 try/finally)
with mpmath.workdps(50):
    ref = mpmath.erf(mpmath.mpf('1.5'))          # 50 位精度的参考值
    # 模拟 mpf2float:转 17 位十进制串再 float,避免直接 float() 的舍入瑕疵
    ref_double = float(mpmath.nstr(ref, 17, min_fixed=0, max_fixed=0))

# 2) SciPy 的双精度结果
val = sc.erf(1.5)

# 3) 相对误差(黄金参考已折回 double,故误差应贴近机器精度)
rel_err = abs(val - ref_double) / abs(ref_double)
print(f"special.erf(1.5) = {val!r}")
print(f"mpmath (50d)      = {ref_double!r}")
print(f"相对误差          = {rel_err:.3e}")
```

**需要观察的现象**:`special.erf(1.5)` 与 mpmath 50 位结果折回 double 后,前 15–16 位有效数字应当完全一致。

**预期结果**:`erf(1.5) ≈ 0.9661051464753108`;相对误差应在 `1e-16` 量级(双精度机器精度附近)。若你把 `mpmath.workdps(50)` 改成直接 `float(mpmath.erf(...))`(不经 `nstr`),在某些值上能观察到「末位差 1」的瑕疵,这正是 `mpf2float` 存在的理由。精确到具体末位的相对误差值**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**:为什么 `check()` 里要 `np.random.seed(1234)`?`Arg`/`ComplexArg` 采样里用到随机数了吗?

> **答案**:`assert_func_equal`/`FuncData` 内部在处理 `nan_ok`、参数过滤等逻辑时可能依赖确定性的点集顺序,固定种子保证测试可复现;采样类本身是确定性的(`linspace`/`logspace`),种子主要是给下游比对基础设施兜底。

**练习 2**:把上面示例里的 `with mpmath.workdps(50)` 删掉(即用 mpmath 默认精度)再算一次,相对误差会变差吗?为什么仍然很小?

> **答案**:mpmath 默认 `dps=15`,与 double 接近,参考值本身精度有限,但 `erf(1.5)` 这种「好算」的值即便 15 位也足够;关键是 `mpf2float` 折回时只取 17 位,所以只要 mpmath 在该点收敛良好,默认精度也够。提高 `dps` 主要在病态点(大参数、剧烈振荡)上才显出价值。

---

### 4.2 Arg 类族:覆盖各数量级的测试点采样

#### 4.2.1 概念说明

数值函数的 bug 往往藏在「极端数量级」里:`x=1e-300` 处的欠采样、`x=1e100` 处的溢出、`x≈0` 处的相消。若只取 `[0, 1]` 上的均匀点,这些 bug 全都测不到。`Arg` 类的设计目标正是:**给定区间 `[a, b]` 与点数 `n`,产出一组「既覆盖各数量级(对数采样)、又不丢中等区间(线性采样)」的实数点**。

围绕 `Arg` 还有三个伙伴类:

- `FixedArg`:不采样,直接返回调用方给定的一组固定值(用于回归点、边界点)。
- `ComplexArg`:复平面采样,实部、虚部各用一个 `Arg`,再做网格外积。
- `IntArg`:整数轴采样,给阶数 `n`、`v` 这类整数参数用。

> 再次澄清:规划主题提到的 `Hyp` 类**不存在**。超几何函数的参数域是用「多个 `Arg`/`ComplexArg` 拼成的多元规格」处理的,见 4.2.3 的 `test_hyp1f1`。

#### 4.2.2 核心流程

`Arg.values(n)` 的整体策略是「正负轴分开,正半轴交给 `_positive_values`,再对称翻折」:

```text
values(n):
    若 a==b:           返回 n 个 0
    若 a>=0:           pospts = _positive_values(a, b, n);   negpts = []
    若 b<=0:           negpts = -_positive_values(-b, -a, n); pospts = []
    否则(跨 0):        pospts = _positive_values(0, b, n1)
                       negpts = -_positive_values(0, -a, n2+1)[1:]  # 去掉重复的 0
    拼接:返回 [负轴降序, 正轴升序]
```

`_positive_values(a, b, n)` 的精髓是**「一半线性、一半对数」**,并按 `a`、`b` 落在哪个区间分五种情况:

```text
_positive_values(a, b, n):           # a 已保证 >= 0
    nlogpts, nlinpts = n//2, n//2     # 各拿一半点数
    若 a >= 10:        纯 logspace(log10 a, log10 b)        # 大数区,只对数
    若 0<a 且 b<10:    纯 linspace(a, b)                    # 小数区,只线性
    若 a > 0:          linspace(a,10) ∪ logspace(1, log10 b) # 跨 10
    若 a==0 且 b<=10:  logspace(-30, ·) ∪ linspace(0, b)     # 含 0 的小区间
    否则(跨 0 与 10): logspace(-30,·) ∪ linspace(0,10) ∪ logspace(1, log10 b)
```

直觉是:**线性段负责「中等大小」的均匀覆盖,对数段负责「极小 / 极大」的数量级覆盖**,二者拼接保证 `1e-30`、`1e-1`、`1`、`1e1`、`1e100` 都被采到。

#### 4.2.3 源码精读

`Arg` 的文档字符串点明了设计意图([_mptestutils.py:L22-L26](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_mptestutils.py#L22-L26)):

```python
class Arg:
    """Generate a set of numbers on the real axis, concentrating on
    'interesting' regions and covering all orders of magnitude."""
```

`_positive_values` 的五种分支([_mptestutils.py:L39-L93](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_mptestutils.py#L39-L93)),其中「跨 0 与 10」的通用分支最能体现拼接思想([_mptestutils.py:L74-L91](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_mptestutils.py#L74-L91)):

```python
linpts = np.linspace(0, 10, nlinpts, endpoint=False)
if linpts.size > 1:
    right = np.log10(linpts[1])
else:
    right = -30
logpts1 = np.logspace(-30, right, nlogpts1, endpoint=False)   # 极小正数段
logpts2 = np.logspace(1, np.log10(b), nlogpts2)               # 大数段
pts = np.hstack((logpts1, linpts, logpts2))
```

注意 `logspace(-30, ...)` 刻意从 `1e-30` 起步,专门覆盖「极小正数」这一容易藏 bug 的数量级。

`ComplexArg` 用 `floor(sqrt(n))` 把点数在实部、虚部间分配,做网格外积([_mptestutils.py:L141-L150](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_mptestutils.py#L141-L150)):

```python
class ComplexArg:
    def __init__(self, a=complex(-inf,-inf), b=complex(inf,inf)):
        self.real = Arg(a.real, b.real)
        self.imag = Arg(a.imag, b.imag)
    def values(self, n):
        m = int(np.floor(np.sqrt(n)))
        x = self.real.values(m)
        y = self.imag.values(m + 1)
        return (x[:,None] + 1j*y[None,:]).ravel()
```

`IntArg` 借用 `Arg` 生成浮点再转 int,并额外并入 `arange(-5,5)` 与去重过滤([_mptestutils.py:L153-L163](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_mptestutils.py#L153-L163))。

多元参数的「广播」由 `get_args` 完成([_mptestutils.py:L166-L179](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_mptestutils.py#L166-L179)):它给 `ComplexArg` 分配 1.5 倍权重(`n**(ms/sum(ms))`),再用 `np.ix_` 做外积、`broadcast_arrays` 展平成一张 `(N, nargs)` 的参数表。这就是「多个 `Arg` 组成超几何参数域」的实现机制。

消费侧最干净的例子是 Airy 函数:实轴用 `Arg`,复平面用 `ComplexArg`([tests/test_mpmath.py:L686-L699](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/tests/test_mpmath.py#L686-L699)):

```python
def test_airyai(self):
    assert_mpmath_equal(lambda z: sc.airy(z)[0],
                        mpmath.airyai,
                        [Arg(-1e8, 1e8)], rtol=1e-5)
    ...
def test_airyai_complex(self):
    assert_mpmath_equal(lambda z: sc.airy(z)[0],
                        mpmath.airyai, [ComplexArg()])
```

整数阶用 `IntArg`([tests/test_mpmath.py:L760-L764](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/tests/test_mpmath.py#L760-L764)):

```python
def test_bernoulli(self):
    assert_mpmath_equal(lambda n: sc.bernoulli(int(n))[int(n)],
                        lambda n: float(mpmath.bernoulli(int(n))),
                        [IntArg(0, 13000)], rtol=1e-9, n=13000)
```

而超几何函数的「多参数域」,就是给一个 `arg_spec` 列表里塞多个 `Arg`,并不需要专门的 `Hyp` 类——`test_hyp1f1` 用三个 `Arg` 分别描述 `a, b, x`,并对 `b` 用 `inclusive_a=False` 排除非正整数极点([tests/test_mpmath.py:L1487-L1500](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/tests/test_mpmath.py#L1487-L1500)):

```python
def test_hyp1f1(self):
    ...
    assert_mpmath_equal(
        sc.hyp1f1, mpmath_hyp1f1,
        [Arg(-50, 50), Arg(1, 50, inclusive_a=False), Arg(-50, 50)],
        n=500, nan_ok=False)
```

#### 4.2.4 代码实践

**实践目标**:用 `Arg` 实际生成一批点,直观验证它同时覆盖了极小、中等、极大三个数量级。

**操作步骤**(示例代码,非项目原有):

```python
# 示例代码:观察 Arg 的采样分布
import numpy as np
from scipy.special._mptestutils import Arg, ComplexArg, IntArg, get_args

# 1) 默认区间(全实轴)采 25 个点
x = Arg().values(25)
print("最小正数:", np.min(np.abs(x[x != 0])))
print("最大值  :", np.max(np.abs(x)))
print("中位量级:", np.median(np.log10(np.abs(x[x != 0]))))

# 2) 多元参数表:get_args 把两个 Arg 外积成 (N, 2)
pts = get_args([Arg(-1e3, 1e3), Arg(0, np.inf)], 200)
print("参数表形状:", pts.shape)

# 3) IntArg:整数采样
print("IntArg 样本:", IntArg(-5, 5).values(10))
```

**需要观察的现象**:`Arg().values(25)` 里应同时出现 `~1e-30` 量级(对数小段)、`O(1)` 量级(线性段)和接近 `0.5*maxfloat` 量级(对数大段)的点;`get_args` 的输出是 `(N, 2)` 形状的二维数组,每一行是一组 `(v, z)` 参数。

**预期结果**:最小正数约 `1e-30` 量级;最大值约 `0.5 × np.finfo(float).max` 即 `~9e307`;`IntArg(-5,5).values(10)` 返回 `[-5,-4,...,4]` 一类整数。具体样本**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**:`_positive_values` 在 `a >= 10` 时为什么「只返回 logspace,不要 linspace」?

> **答案**:`a>=10` 已远离原点,线性段若仍放在 `[a, 10]` 会是空区间(`a>10`)或几乎无覆盖;此区间里「数量级跨度」远比「段内均匀」重要,故全部点数投入对数采样更划算。

**练习 2**:`ComplexArg.values(n)` 为什么用 `floor(sqrt(n))` 而不是 `n//2` 分配实/虚部点数?

> **答案**:复平面是二维网格,`m×m ≈ n` 才能总共得到约 `n` 个复数点;若各分 `n/2` 会得到 `(n/2)×(n/2) = n²/4` 个点,远超预算。开方是把「点数预算」正确映射到「边长」。

**练习 3**:看 `test_hyp1f1` 里 `Arg(1, 50, inclusive_a=False)` 的第二个参数 `b`,为什么要把下界 `1` 设为「不包含」?

> **答案**:`hyp1f1(a,b,x)` 在 `b` 为非正整数(0, -1, -2, …)时分母的 Γ 函数发散,是奇点。下界从 1 起且不含 1,配合默认下界策略,是为了避开 `b=0` 这个非正整数极点。

---

### 4.3 MpmathData 与 assert_mpmath_equal:rtol/atol 比对管线

#### 4.3.1 概念说明

`assert_mpmath_equal` 是面向测试编写者的「一行式」入口;它只是构造一个 `MpmathData` 并调 `.check()`([_mptestutils.py:L293-L295](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_mptestutils.py#L293-L295)):

```python
def assert_mpmath_equal(*a, **kw):
    d = MpmathData(*a, **kw)
    d.check()
```

`MpmathData` 是把「被测函数、参考函数、参数规格、容差」打包在一起的数据对象。它的核心参数:
- `scipy_func` / `mpmath_func`:被测与参考函数。
- `arg_spec`:一个 `Arg`/`ComplexArg`/... 的列表(或一个现成数组),描述各参数的采样域。
- `dps`/`prec`:mpmath 精度档位(默认 `dps=20`)。
- `n`:采样点数(默认 500,XSLOW 下 5000)。
- `rtol`/`atol`:相对/绝对容差(默认 `rtol=1e-7, atol=1e-300`)。
- 一组布尔开关:`nan_ok`(允许 NaN)、`ignore_inf_sign`(允许 ±inf 符号差异)、`distinguish_nan_and_inf`(是否区分 NaN 与 inf)、`param_filter`(逐参数过滤)。

#### 4.3.2 核心流程

见 4.1.2 给出的 `check()` 伪代码。这里补两条与本节主题(`rtol`/`atol`)直接相关的判定规则:

- **最终逐点判定**由 `_testutils.assert_func_equal` 完成,公式即第 2 节的 \(|\text{res}-\text{std}|\le\text{atol}+\text{rtol}\cdot|\text{std}|\)。
- **更高精度的直接比对** `mp_assert_allclose` 不折回 double,而是用 mpmath 的 `fabs` 直接比较高精度 `mpf`/`mpc`,公式同为 `atol + rtol*|std|`(见 4.3.3)。

另外,`MpmathData.__init__` 里有个测试规模开关:mpmath 太慢,默认只算 500 点,设了环境变量 `SCIPY_XSLOW` 才回到老的 5000 点([_mptestutils.py:L188-L197](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_mptestutils.py#L188-L197))。

#### 4.3.3 源码精读

`MpmathData.__init__` 的默认容差与 XSLOW 规模([_mptestutils.py:L183-L197](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_mptestutils.py#L183-L197)):

```python
def __init__(self, scipy_func, mpmath_func, arg_spec, name=None,
             dps=None, prec=None, n=None, rtol=1e-7, atol=1e-300,
             ignore_inf_sign=False, distinguish_nan_and_inf=True,
             nan_ok=True, param_filter=None):
    ...
    if n is None:
        try:
            is_xslow = int(os.environ.get('SCIPY_XSLOW', '0'))
        except ValueError:
            is_xslow = False
        n = 5000 if is_xslow else 500
```

`check()` 里 dps 重试与最终委托(关键段已在 4.1.3 引用,这里看「失败重抛」的细节,[_mptestutils.py:L276-L282](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_mptestutils.py#L276-L282)):

```python
except AssertionError:
    if j >= len(dps_list)-1:
        # reraise the Exception
        tp, value, tb = sys.exc_info()
        if value.__traceback__ is not tb:
            raise value.with_traceback(tb)
        raise value
```

当 `dps_list` 只有一档(默认),这段相当于「失败立即重抛」;只有调用方给了多档精度时,才会在低精度失败后尝试更高精度。

`mp_assert_allclose` 是「不折回 double」的高精度比对,用于需要超过双精度分辨率的场合([_mptestutils.py:L427-L453](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_mptestutils.py#L427-L453)):

```python
def mp_assert_allclose(res, std, atol=0, rtol=1e-17):
    failures = []
    for k, (resval, stdval) in enumerate(zip_longest(res, std)):
        ...
        if mpmath.fabs(resval - stdval) > atol + rtol*mpmath.fabs(stdval):
            failures.append((k, resval, stdval))
    ...
    if nfail > 0:
        ...  # 逐个打印「实测值 != 参考值 (rdiff ...)」
        assert_(False, "\n".join(msg))
```

注意它默认 `rtol=1e-17`,比 `MpmathData` 的 `1e-7` 严得多——因为它的输入本身就是高精度 `mpf`,不需要为「折回 double 的损失」预留余量。失败时它会按 `rtol` 推算打印位数(`ndigits = int(abs(np.log10(rtol)))`),给出可读的诊断信息。

消费侧一个典型用例 `test_besseli`,展示了 `atol=1e-270`(对极小结果放宽绝对容差)与大区间 `Arg(-1e100, 1e100)` 的搭配([tests/test_mpmath.py:L766-L772](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/tests/test_mpmath.py#L766-L772)):

```python
def test_besseli(self):
    assert_mpmath_equal(
        sc.iv,
        exception_to_nan(lambda v, z: mpmath.besseli(v, z, **HYPERKW)),
        [Arg(-1e100, 1e100), Arg()],
        atol=1e-270,
    )
```

其中 `HYPERKW = dict(maxprec=200, maxterms=200)`([tests/test_mpmath.py:L679](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/tests/test_mpmath.py#L679))是给 mpmath 超几何/贝塞尔内核的「耐心」参数:允许它多算项、多提精度以求收敛。

#### 4.3.4 代码实践

**实践目标**:体会 `rtol` 与 `atol` 的差别——当一个函数的真值极小(接近 0)时,为什么光靠 `rtol` 不够。

**操作步骤**(示例代码,非项目原有):

```python
# 示例代码:观察 atol 在「接近零」结果上的作用
import numpy as np
from numpy.testing import assert_allclose

std = 1e-200          # 假想的真值(极小)
res = 0.0             # 被测函数在该点返回 0(完全相消)

# 只用 rtol:|res-std| = 1e-200  vs  rtol*|std| = 1e-7*1e-200 = 1e-207 → 不通过
try:
    assert_allclose([res], [std], rtol=1e-7, atol=0)
    print("仅 rtol: 通过")
except AssertionError:
    print("仅 rtol: 失败(这正是 MpmathData 默认 atol=1e-300 的意义)")

# 加上 atol:|res-std| = 1e-200  vs  atol+rtol*|std| = 1e-300+1e-207 ≈ 1e-207 → 仍可能失败
# 故 test_besseli 才把 atol 放宽到 1e-270 来容忍这类相消
```

**需要观察的现象**:当真值极小时,`rtol*|std|` 也变得极小,几乎任何误差都会判失败;只有放宽 `atol` 才能放过「绝对值已无关紧要」的相消。

**预期结果**:仅用 `rtol=1e-7, atol=0` 比对 `0` 与 `1e-200` 会抛 `AssertionError`;这与 `_mptestutils` 默认 `atol=1e-300`、`test_besseli` 用 `atol=1e-270` 的设计动机一致。具体是否通过取决于你设的 `atol`,**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**:`MpmathData` 默认 `rtol=1e-7` 远大于双精度机器精度 `~2e-16`,为什么?

> **答案**:默认 `dps=20`,mpmath 参考值本身只有约 20 位精度,折回 double 后与 SciPy 双精度结果相比,余量不能设得太严;`1e-7` 是个对大多数特殊函数都「既抓得到 bug、又不误报」的经验阈值。需要更严的比对时,调用方显式给 `dps` 与更小的 `rtol`(如 `test_airyai_prime` 在 `[0,1e3]` 段用 `rtol=1e-12`)。

**练习 2**:`mp_assert_allclose` 默认 `rtol=1e-17`,但 `MpmathData` 默认 `rtol=1e-7`,为什么差 10 个数量级?

> **答案**:`mp_assert_allclose` 直接在 `mpf` 高精度域比较,不折回 double,所以可以用接近双精度极限的严容差;`MpmathData` 会把参考值折回 double(损失到 ~17 位)再比对,必须留出折回余量。

**练习 3**:为什么 `check()` 把真正的逐点断言委托给 `_testutils.assert_func_equal`,而不是自己写循环比 `rtol`/`atol`?

> **答案**:复用。`assert_func_equal`/`FuncData`(见 [u9-l1](u9-l1-testutils-funcdata.md))已经实现了「点集解析、参数过滤、NaN/inf 符号处理、向量/标量分发」等通用逻辑;mpmath 路径只需贡献「造点 + 喂参考值」,不必重造轮子。

---

### 4.4 处理 mpmath 怪癖的工具与装饰器

#### 4.4.1 概念说明

mpmath 作为「裁判」虽准,但有几个工程上的坏脾气:

- **会抛异常**:某些点它算不出(不收敛、除零),直接抛 `ZeroDivisionError`/`NoConvergence`。
- **会返回 inf**:另一些点它返回 `±inf`。
- **会慢得离谱**:个别点可能卡几秒甚至不收敛。
- **行为难追踪**:出问题时想知道「到底是哪个输入触发的」。

`_mptestutils.py` 在文件后半部分提供了一组小工具装饰器来驯服这些脾气:`exception_to_nan`、`inf_to_nan`、`time_limited`、`trace_args`。它们都是「函数套壳」:包住 mpmath 函数,把异常/超时统一翻译成 `NaN`,从而让外层的 `assert_func_equal` 用既有的「NaN 视为跳过/允许」逻辑处理,而不是让整个测试崩掉。

#### 4.4.2 核心流程

四个装饰器的职责概览:

```text
exception_to_nan(f):  f 抛任何异常 → 返回 nan
inf_to_nan(f):        f 返回非有限值(±inf) → 返回 nan
time_limited(t)(f):   f 在 t 秒内不返回 → 返回 nan(POSIX 用 SIGALRM,否则用 settrace)
trace_args(f):        调用前把参数、返回值打到 stderr(调试定位用)
```

`time_limited` 是其中最复杂的,分两路实现:

```text
time_limited(timeout, return_val=nan):
    if POSIX 且 use_sigalrm:
        注册 SIGALRM 处理函数 → 抛 TimeoutError
        用 setitimer 定时;超时被捕获 → 返回 return_val;finally 关定时器、还原旧处理函数
    else:
        用 sys.settrace 注册回调,每进一个函数检查是否超时 → 超时抛 TimeoutError
        (只能跟踪当前线程,且会放慢约 10 倍)
```

#### 4.4.3 源码精读

`exception_to_nan` 与 `inf_to_nan` 都极短([_mptestutils.py:L407-L424](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_mptestutils.py#L407-L424)):

```python
def exception_to_nan(func):
    """Decorate function to return nan if it raises an exception"""
    def wrap(*a, **kw):
        try:
            return func(*a, **kw)
        except Exception:
            return np.nan
    return wrap

def inf_to_nan(func):
    """Decorate function to return nan if it returns inf"""
    def wrap(*a, **kw):
        v = func(*a, **kw)
        if not np.isfinite(v):
            return np.nan
        return v
    return wrap
```

注意 `exception_to_nan` 捕获的是 `Exception`(不含 `KeyboardInterrupt` 等系统退出),避免误吞 Ctrl-C。

`time_limited` 的 POSIX 分支用 `signal.setitimer` 设软件定时器([_mptestutils.py:L370-L385](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_mptestutils.py#L370-L385)):

```python
if POSIX and use_sigalrm:
    def sigalrm_handler(signum, frame):
        raise TimeoutError()
    def deco(func):
        def wrap(*a, **kw):
            old_handler = signal.signal(signal.SIGALRM, sigalrm_handler)
            signal.setitimer(signal.ITIMER_REAL, timeout)
            try:
                return func(*a, **kw)
            except TimeoutError:
                return return_val
            finally:
                signal.setitimer(signal.ITIMER_REAL, 0)
                signal.signal(signal.SIGALRM, old_handler)
        return wrap
```

`trace_args` 用作调试,把每次调用的入参和返回值打到 stderr([_mptestutils.py:L324-L341](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_mptestutils.py#L324-L341)),出问题时能精确定位是哪个输入触发的失败。

消费侧,几乎所有限制条件苛刻的 mpmath 参考函数都被 `exception_to_nan` 包了一层——例如 Kelvin 函数 `bei`/`ber`、贝塞尔 `iv`/`jv`、超几何 `hyp0f1` 等,把 mpmath 的「这点我不收敛」翻译成 NaN,交给 `assert_func_equal` 的 `nan_ok` 处理([tests/test_mpmath.py:L750-L758](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/tests/test_mpmath.py#L750-L758)):

```python
def test_bei(self):
    assert_mpmath_equal(sc.bei,
        exception_to_nan(lambda z: mpmath.bei(0, z, **HYPERKW)),
        [Arg(-1e3, 1e3)])
```

#### 4.4.4 代码实践

**实践目标**:验证 `exception_to_nan` / `inf_to_nan` 如何把 mpmath 的失败「软化」为 NaN,使批量比对不被单个坏点中断。

**操作步骤**(示例代码,非项目原有):

```python
# 示例代码:观察装饰器如何软化 mpmath 的失败
import numpy as np
from scipy.special._mptestutils import exception_to_nan, inf_to_nan

# 1) 模拟一个「有时抛异常、有时返回 inf」的参考函数
def flaky_ref(x):
    if x < 0:
        raise ValueError("mpmath 在该点不收敛(模拟)")
    return np.inf if x == 0 else float(x) * 2

soft = exception_to_nan(flaky_ref)
print("x=-1 →", soft(-1.0))   # 抛异常 → nan
print("x= 0 →", soft(0.0))    # 返回 inf → 仍是 inf(没被 inf_to_nan 包)
print("x= 2 →", soft(2.0))    # 正常 → 4.0

soft2 = inf_to_nan(exception_to_nan(flaky_ref))
print("套 inf_to_nan 后 x=0 →", soft2(0.0))  # inf → nan
```

**需要观察的现象**:套了 `exception_to_nan` 后,负数点不再抛异常而是返回 `nan`;再套 `inf_to_nan` 后,`x=0` 的 `inf` 也变成 `nan`。这样在批量比对里,这些点会被当作「跳过/允许 NaN」处理。

**预期结果**:三次输出依次为 `nan`、`inf`、`4.0`;再套 `inf_to_nan` 后 `x=0` 输出 `nan`。**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**:`time_limited` 在 POSIX 下为什么用 `setitimer` 而不是 `signal.alarm`?

> **答案**:`alarm` 的粒度是整秒,对「0.5 秒超时」这类子秒阈值无能为力;`setitimer(ITIMER_REAL, timeout)` 支持浮点秒,能设 0.5s 这类精细超时。

**练习 2**:`time_limited` 的非 POSIX 分支(用 `sys.settrace`)注释里说「放慢约 10 倍」,为什么仍然要提供它?

> **答案**:为了在不支持 SIGALRM 的平台(如某些非 POSIX 环境)上也能跑测试,即使慢。注释也警告它「只能跟踪当前线程、不要和线程混用」,是个保底实现。

**练习 3**:为什么 `test_bei` 把 mpmath 参考函数包成 `exception_to_nan(...)` 而不是直接传 `mpmath.bei`?

> **答案**:mpmath 在某些点(如大参数、振荡剧烈处)可能不收敛而抛异常,若不软化,一个坏点就会让整个测试崩;`exception_to_nan` 把这些点翻译成 NaN,配合 `assert_func_equal` 的 `nan_ok`/跳过逻辑,让测试聚焦在「能算的点」上。

## 5. 综合实践

把本讲的采样、管线、容差、装饰器四件套串起来,自己写一个最小的 mpmath 校验(示例代码,非项目原有):

```python
# 综合实践:仿照 assert_mpmath_equal,自校验 special.gamma 在实轴上的正确性
import numpy as np
import mpmath
import scipy.special as sc
from scipy.special._mptestutils import Arg, exception_to_nan

# 1) 用 Arg 采样(覆盖极小/中等/极大,避开非正整数极点)
pts = Arg(-5, 5).values(500)
# 排除 gamma 的极点 0 与负整数附近(简单起见用 param_filter 思路手算)
mask = np.abs(pts - np.round(pts)) > 1e-9   # 粗略避开整数
mask &= np.abs(pts) > 1e-3                    # 避开 0
pts = pts[mask]

# 2) mpmath 高精度参考(50 位),用 exception_to_nan 软化失败
with mpmath.workdps(50):
    ref_fn = exception_to_nan(lambda x: float(mpmath.nstr(mpmath.gamma(x), 17,
                                                          min_fixed=0, max_fixed=0)))
    ref = np.array([ref_fn(float(x)) for x in pts])

# 3) SciPy 双精度结果
val = sc.gamma(pts)

# 4) 用 rtol/atol 判定
ok = np.abs(val - ref) <= 1e-7 * np.abs(ref)   # 简化版:仅 rtol
print(f"通过率: {np.sum(ok & np.isfinite(ref))}/{np.sum(np.isfinite(ref))}")
bad = pts[~ok & np.isfinite(ref)]
print("最差的几个点:", bad[:5] if bad.size else "无")
```

**任务**:运行上述脚本,观察通过率是否接近 100%;然后故意把 `rtol` 从 `1e-7` 收紧到 `1e-13`,看看 `gamma` 在哪些点(通常是大 `|x|` 处)开始「掉精度」,并联系 4.3 讨论为什么 `MpmathData` 默认 `rtol=1e-7` 是个稳妥的工程取值。

## 6. 本讲小结

- `scipy.special` 用 **mpmath 作为任意精度的「黄金参考」**校验双精度内核;整体管线是「采样 → mpmath 高精度计算 → `mpf2float` 折回 double → `rtol`/`atol` 比对」。
- **采样靠 `Arg` 类族**:`Arg` 在实轴上用「半线性 + 半对数」策略同时覆盖极小/中等/极大各数量级;`ComplexArg` 做复平面网格;`IntArg` 采整数阶;`FixedArg` 给固定回归点;多元参数由 `get_args` 外积成参数表。规划主题里的 `Hyp` 类**并不存在**,超几何参数域就是多个 `Arg` 的组合。
- **比对管线 `MpmathData`/`assert_mpmath_equal`** 负责精度档位管理(`dps` 重试 + `try/finally` 还原)、类型折回(实/复两套、`mpf2float` 用 `nstr` 避免直接 `float()` 的舍入瑕疵),最终逐点断言委托给 `_testutils.assert_func_equal`。
- **判定公式** \(|\text{res}-\text{std}|\le\text{atol}+\text{rtol}\cdot|\text{std}|\) 同时用于 NumPy 路径与 mpmath 高精度路径 `mp_assert_allclose`(后者默认 `rtol=1e-17` 远严于前者的 `1e-7`,因不折回 double)。
- **驯服 mpmath 怪癖**靠一组装饰器:`exception_to_nan`(异常→NaN)、`inf_to_nan`(inf→NaN)、`time_limited`(SIGALRM/settrace 超时→NaN)、`trace_args`(调试定位),保证单个坏点不致整测试崩溃。
- **消费侧 `test_mpmath.py`** 用 `@check_version` 处理 mpmath 可选性、用 `HYPERKW` 给 mpmath 加耐心、用 `thread_unsafe` 标记规避 gmpy2 后端的线程安全问题,把上述零件组装成对数百个函数的系统比对。

## 7. 下一步学习建议

- 接着读 [u9-l3](u9-l3-precompute.md):`_precompute/` 目录是「离线用 mpmath 高精度预计算系数/参考数据并固化」的工程实践,与本讲的「在线比对」互补——一个是「造黄金参考数据」,一个是「运行时用黄金参考校验」。
- 回看 [_testutils.py](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_testutils.py) 中 `FuncData` 与 `assert_func_equal` 的完整实现,理解本讲 `MpmathData.check()` 所委托的「点集解析、参数过滤、NaN/inf 符号处理」细节,补全 u9-l1 缺失的讲义内容。
- 在 `tests/test_mpmath.py` 里挑一个 `@pytest.mark.slow` 的 `TestSystematic` 用例(如 `test_besseli`),打开 `trace_args` 装饰参考函数,观察 mpmath 在哪些点最慢、最容易不收敛,体会 `time_limited`/`exception_to_nan` 的实际用武之地。
