# 薄包装与装饰器：lambertw 与 spherical_bessel

## 1. 本讲目标

学完本讲，你应当能够：

- 说清「薄包装（thin wrapper）」函数的含义：它本身不是 ufunc，而是套在底层 ufunc 之外、只做一点点参数预处理的一层纯 Python 函数。
- 解释 `lambertw` 与 `spherical_jn/yn/in/kn` 各自「为什么要再包一层」，而不是直接把 ufunc 暴露给用户。
- 读懂 `use_reflection` 装饰器：它如何按 DLMF 10.47(v) 的奇偶反射公式，把负实轴输入翻到正半轴再补回符号。
- 掌握 `derivative` 布尔参数如何在一个 Python 入口背后，选择 `_xxx` 与 `_xxx_d` 两个不同的底层 ufunc。

本讲承接 u4-l1（`_basic.py` 中纯 Python 函数为何常常不是 ufunc），把视线从「整段算法用 Python 写」收窄到「算法仍在 ufunc 里、Python 只做收尾」的薄包装这一更轻、更常见的模式。

## 2. 前置知识

阅读本讲前，最好已经具备以下认知（来自 u1/u2/u4-l1）：

- **ufunc 的硬约束**：NumPy ufunc 的类型签名是固定的、必然逐元素、输入输出形状一致。因此 ufunc 无法承载「额外的标量配置参数」（如容差 `tol`），也无法承载「一个布尔开关去切换不同的 C 内核」。
- **scipy.special 的命名空间拼装**：`scipy.special` 顶层能调到的函数，不少来自 `_lambertw.py`、`_spherical_bessel.py` 这类「单职责小文件」，再被 `__init__.py` 汇聚进 `__all__`。
- **奇偶性与反射**：若函数满足 \(f(-z) = c \cdot f(z)\)（\(c\) 是 \(\pm 1\)），就说它具有确定的奇偶性。这是后面 `use_reflection` 的数学基础。
- **链式法则**：\(\frac{d}{dz}f(-z) = -f'(-z)\)。这一个小小的负号，是 `use_reflection` 在导数模式下要额外处理的关键。

如果对 ufunc 的逐元素约束还不太熟，建议先回看 u2-l1。

## 3. 本讲源码地图

本讲只涉及两个文件，它们都属于「Python 包装层」（见 u1-l2 的三层地图）：

| 文件 | 行数规模 | 角色 | 暴露的公共函数 |
| --- | --- | --- | --- |
| `scipy/special/_lambertw.py` | 约 150 行 | Lambert W 函数的薄包装 | `lambertw` |
| `scipy/special/_spherical_bessel.py` | 约 405 行 | 四个球 Bessel 函数的薄包装 + 一个共享反射装饰器 | `spherical_jn`、`spherical_yn`、`spherical_in`、`spherical_kn` |

两个文件都 `from ._ufuncs import ...` 拿到底层 ufunc（这些 ufunc 的 C/C++ 内核来自 U3 讲的代码生成管线），然后在 Python 侧做最小限度的加工。底层 ufunc 的「身份证明」可以在类型桩里查到：

- [_ufuncs.pyi:268](_ufuncs.pyi#L268)：`_lambertw: np.ufunc`
- [_ufuncs.pyi:282-287](_ufuncs.pyi#L282-L287)：`_spherical_jn(_d)`、`_spherical_yn(_d)`、`_spherical_in(_d)`、`_spherical_kn(_d)` 共 8 个 ufunc。

注意命名约定（承接 u1-l2）：下划线前缀的 `_spherical_jn` 是内部 ufunc，去掉前缀的 `spherical_jn` 才是用户调用的薄包装。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

1. **薄包装模式**：以 `lambertw` 为例，看「为什么要在 ufunc 之上再包一层」。
2. **derivative 分支**：看一个布尔参数如何选择两个底层 ufunc。
3. **use_reflection 装饰器**：看负实轴反射与符号规则，以及 `spherical_kn` 为什么走另一条路。

### 4.1 薄包装模式：为什么要在 ufunc 之上再包一层

#### 4.1.1 概念说明

「薄包装」是指：真正的数值算法已经写进底层 ufunc（C/C++ 内核）里，Python 函数只是在外面套一层壳，做以下三类轻量工作之一：

- **类型/形状规整**：把某个参数强制成 ufunc 期望的 dtype（如把整数阶强制成 `long`）。
- **承载 ufunc 装不下的参数**：ufunc 的类型签名是固定的，没法多带一个「容差」「分支号」之类的标量配置，于是放到 Python 包装层。
- **选择/分发**：根据一个布尔或枚举参数，挑不同的底层 ufunc 调用。

`lambertw` 同时体现了前两点，是理解薄包装的最佳样本。

#### 4.1.2 核心流程

`lambertw` 的调用链极短：

```text
lambertw(z, k=0, tol=1e-8)
   ├── k = np.asarray(k, dtype="long")   # 把分支号规整成 C 期望的 long 整数
   └── return _lambertw(z, k, tol)        # 把三个参数转交给底层 ufunc
```

为什么不能直接把 `_lambertw` 暴露成 `lambertw`？因为 `_lambertw` 这个 ufunc 虽然技术上能接受 `(z, k, tol)` 三个输入，但：

- 用户接口希望 `k` 和 `tol` 有默认值（`k=0`、`tol=1e-8`），而 ufunc 的位置参数没有「带默认值」的语义。
- `k` 应当是整数（分支索引），需要规整成 `long` 以稳定命中 C 内核的整数类型分发。
- 文档串（一段很长的 Sphinx docstring）挂在 Python 函数上更自然。

于是包一层纯 Python 是最干净的做法。

#### 4.1.3 源码精读

导入与函数定义见 [_lambertw.py:1-6](_lambertw.py#L1-L6)：第 1 行 `from ._ufuncs import _lambertw` 把底层 ufunc 拿进来，第 6 行定义带默认值的公开签名 `lambertw(z, k=0, tol=1e-8)`。

真正的「包装逻辑」只有两行，藏在长文档串之后：

[_lambertw.py:146-149](_lambertw.py#L146-L149) —— `k` 的 dtype 强制与三参数转发：

```python
# TODO: special expert should inspect this
# interception; better place to do it?
k = np.asarray(k, dtype=np.dtype("long"))
return _lambertw(z, k, tol)
```

这段代码做了两件事：

1. `np.asarray(k, dtype=np.dtype("long"))`：把分支号 `k`（可能是 Python `int`、NumPy 标量或数组）强制成 `long` 类型。`long` 在 C 层对应 `long int`，正是底层 ufunc 期望的「分支索引」类型；这样无论用户传 `0` 还是 `np.int32(0)`，都能稳定命中同一套 C 内核，避免类型分发歧义。
2. `return _lambertw(z, k, tol)`：把规整后的 `k` 连同 `z`、`tol` 一起喂给底层 ufunc。注意 `z` 不做任何加工——它的逐元素求值与广播完全交给 ufunc（回顾 u2-l1）。

> 小贴士：注意 `z` 没有被 `np.asarray`，是因为 ufunc 本身就会处理任意 array_like 输入；只有「需要固定 dtype 才能正确分发」的 `k` 才需要预先规整。这是薄包装的典型取舍：**只动必须动的参数，其余原样透传**。

#### 4.1.4 代码实践

**实践目标**：验证 `lambertw` 是「带默认值的薄包装」，且其底层 `_lambertw` 是真正的 ufunc。

**操作步骤**：

1. 在已安装 SciPy 的环境中运行下面脚本。
2. 用 `isinstance` 检查底层 `_lambertw` 的 ufunc 身份。
3. 验证 `lambertw` 确实把 `k` 规整成了 `long`。

```python
# 示例代码
import numpy as np
from scipy.special import lambertw
from scipy.special._lambertw import _lambertw   # 内部 ufunc

# 1) 底层 _lambertw 确实是 ufunc
print(isinstance(_lambertw, np.ufunc))           # 预期 True

# 2) lambertw 本身不是 ufunc，而是普通 Python 函数
print(isinstance(lambertw, np.ufunc))            # 预期 False

# 3) 默认参数：lambertw(1) 等价于主分支 k=0
w = lambertw(1)
print(w)                                         # 预期 (0.56714329...+0j)
print(w * np.exp(w))                             # 预期 (1+0j)，验证 W 满足 w*exp(w)=z

# 4) k 既可以是 Python int，也可以是数组，包装层会规整成 long
print(lambertw(1, k=3))                          # 第 3 分支
```

**需要观察的现象**：`_lambertw` 是 ufunc、`lambertw` 不是；默认调用 `lambertw(1)` 返回主分支解，且满足定义式 \(w e^w = z\)。

**预期结果**：`True` / `False` / `(0.5671...+0j)` / `(1+0j)` / `(-2.8535...+17.1135...j)`。若环境无 SciPy 则标记「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `lambertw` 签名里的 `k=0` 默认值去掉、改成必填参数，会给用户带来什么具体不便？

> **参考答案**：`lambertw` 最常用的就是主分支（\(k=0\)），例如解 \(w e^w = z\) 的主解。没有默认值意味着每次都要写 `lambertw(z, 0)`；而 ufunc `_lambertw` 的位置参数本身不支持「带默认值」，所以这个默认值必须由 Python 包装层提供——这正是薄包装的存在意义之一。

**练习 2**：为什么 `z` 不需要像 `k` 那样 `np.asarray(..., dtype=...)`？

> **参考答案**：`z` 是被逐元素求值的主输入，其 dtype 应当由用户输入和 ufunc 的类型分发共同决定（float/complex 自动选择 loop，见 u2-l1）。预先强制 dtype 反而会破坏复数输入等正常路径。只有「类型必须固定才能正确选择 C 内核」的整数阶/分支号才需要规整。

### 4.2 derivative 分支：用布尔参数选择 _xxx / _xxx_d 两个 ufunc

#### 4.2.1 概念说明

球 Bessel 函数族每个都有「函数值」和「导数值」两种需求。SciPy 没有把它们拆成 `spherical_jn` 与 `spherical_jn_derivative` 两个名字，而是用一个布尔参数 `derivative=False` 来切换。这就引出薄包装的第二类典型工作：**根据布尔参数选择不同的底层 ufunc**。

底层为每个球 Bessel 函数都准备了两个 ufunc：`_spherical_jn`（函数值）与 `_spherical_jn_d`（导数值），共 8 个，见导入语句 [_spherical_bessel.py:4-6](_spherical_bessel.py#L4-L6)：

```python
from ._ufuncs import (_spherical_jn, _spherical_yn, _spherical_in,
                      _spherical_kn, _spherical_jn_d, _spherical_yn_d,
                      _spherical_in_d, _spherical_kn_d)
```

#### 4.2.2 核心流程

以 `spherical_jn` 为例，函数体内的分支非常直白：

```text
spherical_jn(n, z, derivative=False)
   ├── n = np.asarray(n, dtype="long")     # 阶数规整成 long 整数
   └── if derivative: return _spherical_jn_d(n, z)   # 导数内核
       else:        return _spherical_jn(n, z)       # 函数值内核
```

为什么 ufunc 自己搞不定这个分支？因为 ufunc 的「类型分发」只看输入的 **dtype**，不看「某个参数的布尔值」。一个 ufunc 没法说「当第三个参数是 True 时跑 A 内核、False 时跑 B 内核」——它只能按 dtype 选 loop。所以「布尔开关切换内核」这件事天然属于 Python 层。

#### 4.2.3 源码精读

四个球 Bessel 函数的 derivative 分支写法完全一致，以 `spherical_jn` 为例：

[_spherical_bessel.py:123-127](_spherical_bessel.py#L123-L127) —— 阶数规整 + derivative 二选一：

```python
n = np.asarray(n, dtype=np.dtype("long"))
if derivative:
    return _spherical_jn_d(n, z)
else:
    return _spherical_jn(n, z)
```

三个要点：

1. `n = np.asarray(n, dtype=np.dtype("long"))`：与 `lambertw` 的 `k` 完全同理——阶数 `n` 必须是整数，且要稳定命中底层 C 内核期望的 `long` 类型。这里**必须**显式规整，因为用户很容易传 Python `int` 或 `np.arange(...)`（默认 int64/int32），不规整就可能触发错误的类型分发。
2. `if derivative:` 选 `_spherical_jn_d`，否则选 `_spherical_jn`：一个布尔开关，两个 ufunc，二选一。
3. `z` 同样原样透传，广播与逐元素交给 ufunc。

其余三个函数的对应位置：`spherical_yn` 在 [_spherical_bessel.py:214-218](_spherical_bessel.py#L214-L218)，`spherical_in` 在 [_spherical_bessel.py:304-308](_spherical_bessel.py#L304-L308)，`spherical_kn` 在 [_spherical_bessel.py:401-405](_spherical_bessel.py#L401-L405)，结构完全相同。

> 小贴士：导数本身有解析递推式（见各函数文档串的 Notes，如 [_spherical_bessel.py:70-77](_spherical_bessel.py#L70-L77) 给出 \(j_n'(z) = j_{n-1}(z) - \frac{n+1}{z} j_n(z)\)）。SciPy 选择把这个递推实现成独立的 C 内核 `_spherical_jn_d`，而不是在 Python 层用 `jv` 等函数拼——这样既快又数值稳定，Python 层只负责「选哪个内核」。

#### 4.2.4 代码实践

**实践目标**：验证 `derivative=True` 确实调用了另一个内核，且其结果与文档给出的递推关系一致。

**操作步骤**：

```python
# 示例代码
import numpy as np
from scipy.special import spherical_jn

x = np.arange(1.0, 2.0, 0.01)

# A) derivative=True 直接给导数
lhs = spherical_jn(3, x, derivative=True)

# B) 用文档递推式 j_n'(z) = j_{n-1}(z) - (n+1)/z * j_n(z)  手算导数
n = 3
rhs = spherical_jn(n - 1, x) - (n + 1) / x * spherical_jn(n, x)

print(np.allclose(lhs, rhs))   # 预期 True，说明 _spherical_jn_d 与递推一致
```

**需要观察的现象**：两条路径（专用导数内核 vs. 函数值递推）给出的曲线完全重合。

**预期结果**：`True`。这正是 [_spherical_bessel.py:105-108](_spherical_bessel.py#L105-L108) 文档示例所验证的同一关系。待本地验证。

#### 4.2.5 小练习与答案

**练习 1**：假如改成用一个 ufunc 同时返回函数值和导数（多输出 ufunc，回顾 u2-l1 的 `airy`/`sici`），相比现在的「布尔开关 + 两个 ufunc」方案，各有什么优缺点？

> **参考答案**：多输出 ufunc 的好处是一次调用拿到函数值与导数、省一次内核启动；缺点是即使用户只需要函数值，也得连带算导数（浪费），而且接口从「返回标量数组」变成「返回元组」，破坏了对 `spherical_jn(n, z)` 的简单调用体验。当前方案用 `derivative` 开关在 Python 层二选一，既保持简单签名，又让两种需求各走最优内核，是更合用的工程取舍。

**练习 2**：为什么 `n` 必须强制成 `long`，而 `z` 不强制？

> **参考答案**：与 4.1.5 练习 2 同理——`n` 是阶数，必须是整数且 C 内核按整数阶分发；用户传入的 `int`/`np.arange` 的默认整数宽度不固定，不规整会引发类型分发歧义。`z` 是逐元素浮点/复数主输入，其 dtype 应交给 ufunc 自动选择，强制反而出错。

### 4.3 use_reflection 装饰器：负实轴反射与符号规则

#### 4.3.1 概念说明

这是本讲最有意思的部分。球 Bessel 函数的底层 C 内核（`_spherical_jn` 等）对**实数输入**只保证 `z >= 0` 时给出可靠结果（文档串明言「For real arguments greater than the order, the function is computed using the ascending recurrence」，见 [_spherical_bessel.py:64-68](_spherical_bessel.py#L64-L68)）。那么用户传负实数怎么办？

数学上，球 Bessel 函数有确定的奇偶性（DLMF 10.47(v)）：

\[
j_n(-z) = (-1)^n j_n(z), \quad y_n(-z) = (-1)^{n+1} y_n(z), \quad i_n(-z) = (-1)^n i_n(z)
\]

也就是说，负实轴的值可以由正半轴的值「反射」出来，只需补一个取决于 \(n\) 奇偶性的符号。于是 `spherical_jn/yn/in` 都套上同一个装饰器 `use_reflection`，在调用真正内核之前，把负 `z` 翻成正 `z`、补好符号。

`spherical_kn` 是特例——它的反射关系不是简单符号翻转（会牵涉其他函数），所以装饰器给它单独留了一个「复数反射」的口子。

#### 4.3.2 核心流程

`use_reflection` 是一个**带参数的装饰器工厂**：调用 `@use_reflection(+1)` 时返回真正的 `decorator`，再用它去装饰 `spherical_jn`。整体流程：

```text
@use_reflection(sign_n_even=+1)        # 工厂：记下「n 为偶时的符号」
def spherical_jn(n, z, derivative=False):
    ...                                 # 函数体只管调底层 ufunc

# 调用 spherical_jn(3, -2.0) 时，实际执行的是 wrapper：
wrapper(n=3, z=-2.0, derivative=False)
   ├── z = np.asarray(z)
   ├── 若 z 是复数 dtype：直接 fun(n, z)          # 复数内核天然支持全平面
   └── 否则按 z.real >= 0 分两路：
         ├── z >= 0：fun(n, z)                     # 正半轴，直接算
         └── z <  0：standard_reflection(n, z)     # 翻到正半轴 + 补符号
```

「补符号」的核心是 `standard_reflection`：

\[
\text{sign}(n) = \begin{cases} s & n \text{ 为偶} \\ -s & n \text{ 为奇} \end{cases} = s \cdot (-1)^n
\]

其中 \(s\) 就是装饰器参数 `sign_n_even`。再由链式法则 \(\frac{d}{dz}f(-z) = -f'(-z)\)，导数模式要多乘一个 \(-1\)：

\[
\text{sign}_{\text{derivative}}(n) = -\,\text{sign}(n)
\]

最终 \(f_n(z)\)（\(z<0\)）= \(\text{sign} \cdot f_n(-z)\)，其中 \(-z>0\) 走底层内核。

#### 4.3.3 源码精读

装饰器工厂与内层 `standard_reflection` 见 [_spherical_bessel.py:9-35](_spherical_bessel.py#L9-L35)。逐段看：

**工厂签名**（[_spherical_bessel.py:9-13](_spherical_bessel.py#L9-L13)）：

```python
def use_reflection(sign_n_even=None, reflection_fun=None):
    # - If reflection_fun is not specified, reflects negative `z` and multiplies
    #   output by appropriate sign (indicated by `sign_n_even`).
    # - If reflection_fun is specified, calls `reflection_fun` instead of `fun`.
    # See DLMF 10.47(v) https://dlmf.nist.gov/10.47
```

两个参数二选一：

- `sign_n_even`：标准符号反射（jn/yn/in 用），给出「\(n\) 为偶时的符号」。
- `reflection_fun`：自定义反射函数（kn 用），完全绕开符号公式。

**符号计算**（[_spherical_bessel.py:15-21](_spherical_bessel.py#L15-L21)）：

```python
def standard_reflection(n, z, derivative):
    # sign_n_even indicates the sign when the order `n` is even
    sign = np.where(n % 2 == 0, sign_n_even, -sign_n_even)
    # By the chain rule, differentiation at `-z` adds a minus sign
    sign = -sign if derivative else sign
    # Evaluate at positive z (minus negative z) and adjust the sign
    return fun(n, -z, derivative) * sign
```

这三行就是把前面两个公式翻译成代码：

1. `np.where(n % 2 == 0, sign_n_even, -sign_n_even)`：实现 \(\text{sign}(n) = s\cdot(-1)^n\)。
2. `sign = -sign if derivative else sign`：导数模式补链式法则的负号。
3. `fun(n, -z, derivative) * sign`：注意此时传入的 `z` 是负数，`-z` 即正半轴点，交给底层内核安全求值，再乘符号。

**外层 wrapper 的分支**（[_spherical_bessel.py:24-34](_spherical_bessel.py#L24-L34)）：

```python
@wraps(fun)
def wrapper(n, z, derivative=False):
    z = np.asarray(z)

    if np.issubdtype(z.dtype, np.complexfloating):
        return fun(n, z, derivative)  # complex dtype just works

    f2 = standard_reflection if reflection_fun is None else reflection_fun
    return xpx.apply_where(z.real >= 0, (n, z),
                           lambda n, z: fun(n, z, derivative),
                           lambda n, z: f2(n, z, derivative))[()]
```

要点：

- 复数输入直接 `fun(n, z, derivative)`，注释「complex dtype just works」——复数内核能处理整个复平面，不需要反射。
- 实数输入用 `xpx.apply_where(z.real >= 0, ...)`：对 `z >= 0` 的元素走 `fun`（直接算），对 `z < 0` 的元素走 `f2`（反射）。这里**没有**用 `np.where(cond, fun(...), f2(...))`，因为后者会**先把两个分支都在整个数组上算一遍**再挑选——而 `fun` 在负实数上根本不可靠，必须只对正元素调用。`xpx.apply_where` 来自 `scipy._external.array_api_extra`（见 [_spherical_bessel.py:2](_spherical_bessel.py#L2)），它按掩码**只对相关元素求值**，且是 Array API 兼容写法（为 u10 的多后端支持铺路）。末尾 `[()]` 把 0 维数组解包成标量，保持「标量进标量出」。
- `f2` 的选择：`reflection_fun is None` 时用 `standard_reflection`（jn/yn/in），否则用传入的自定义函数（kn）。

**四个函数各自挂的装饰器**：

| 函数 | 装饰器调用 | \(s=\)sign_n_even | 含义 |
| --- | --- | --- | --- |
| `spherical_jn` | `@use_reflection(+1)`（[_spherical_bessel.py:38](_spherical_bessel.py#L38)） | \(+1\) | \(j_n(-z)=(-1)^n j_n(z)\) |
| `spherical_yn` | `@use_reflection(-1)`（[_spherical_bessel.py:130](_spherical_bessel.py#L130)） | \(-1\) | \(y_n(-z)=(-1)^{n+1} y_n(z)\) |
| `spherical_in` | `@use_reflection(+1)`（[_spherical_bessel.py:221](_spherical_bessel.py#L221)） | \(+1\) | \(i_n(-z)=(-1)^n i_n(z)\) |
| `spherical_kn` | `@use_reflection(reflection_fun=spherical_kn_reflection)`（[_spherical_bessel.py:318](_spherical_bessel.py#L318)） | — | 走复数反射 |

**符号差异的来源**：`spherical_jn` 与 `spherical_yn` 唯一的区别就是 `sign_n_even` 一正一负。这是因为 \(j_n\) 与 \(y_n\) 的奇偶性恰好相反——\(j_n\) 像 \(\cos\)（偶阶为偶函数），\(y_n\) 像 \(\sin\)（偶阶为奇函数）。这一个符号的差异，完全由装饰器参数 `+1` vs `-1` 表达，函数体本身（[_spherical_bessel.py:123-127](_spherical_bessel.py#L123-L127) 与 [_spherical_bessel.py:214-218](_spherical_bessel.py#L214-L218)）一字不差。

#### 4.3.4 spherical_kn 为什么改用复数反射

`spherical_kn` 没有走 `standard_reflection`，而是传了一个自定义的 `spherical_kn_reflection`：

[_spherical_bessel.py:311-315](_spherical_bessel.py#L311-L315)：

```python
def spherical_kn_reflection(n, z, derivative=False):
    # More complex than the other cases, and this will likely be re-implemented
    # in C++ anyway. Would require multiple function evaluations. Probably about
    # as fast to just resort to complex math, and much simpler.
    return spherical_kn(n, z + 0j, derivative=derivative).real
```

原因：修正球 Bessel 函数 \(k_n(z)\) 在 \(z\to -z\) 下的关系**不是**一个简单的 \(\pm 1\) 符号翻转，而是会牵涉 \(i_n\) 或 \(y_n\) 等其他函数的组合（柱面情形见 DLMF 10.34 的 \(K_\nu(ze^{m\pi i})\) 公式）。若要在实数域硬推这个组合，需要多次函数求值、写一大段易错的符号逻辑。开发者权衡后选择了一个更简单的等价做法：把 `z` 提升成复数 `z + 0j`，让底层复数内核做解析延拓（自动正确处理分支），然后取 `.real` 拿到实数结果。注释里也预告了这段「将来可能在 C++ 里重写」。

这正是装饰器预留 `reflection_fun` 参数的意义：**当符号翻转不够用时，允许注入一个完全自定义的反射实现**，而外层的 `apply_where` 调度框架（正半轴直接算、负半轴走反射）依然复用。

#### 4.3.5 代码实践

**实践目标**：亲手验证 `spherical_jn(3, -x)` 与反射公式 \(j_3(-x)=(-1)^3 j_3(x) = -j_3(x)\) 一致；并观察 `spherical_kn` 在负实轴上确实走了复数反射。

**操作步骤**：

```python
# 示例代码
import numpy as np
from scipy.special import spherical_jn, spherical_yn, spherical_kn

x = np.array([0.5, 1.0, 1.5, 2.0])

# 1) jn 反射：n=3 为奇，spherical_jn(3, -x) 应等于 -spherical_jn(3, x)
lhs = spherical_jn(3, -x)
rhs = -spherical_jn(3, x)
print("jn 一致:", np.allclose(lhs, rhs))            # 预期 True

# 2) yn 反射：用公式 y_n(-z) = (-1)^{n+1} y_n(z) 直接对照（n=2 -> (-1)^3 = -1）
lhs_y = spherical_yn(2, -x)
rhs_y = (-1)**(2 + 1) * spherical_yn(2, x)
print("yn 一致:", np.allclose(lhs_y, rhs_y))        # 预期 True

# 3) kn 在负实轴上：直接对负实数调用，返回值仍是实数（来自复数反射的 .real）
k_neg = spherical_kn(0, -1.0)
print("kn(-1) 类型:", type(k_neg), "值:", k_neg)     # 预期为实数 numpy 标量
```

**需要观察的现象**：

- `spherical_jn(3, -x)` 与 \(-spherical_jn(3, x)\) 完全吻合，验证了符号翻转。
- `spherical_kn(0, -1.0)` 返回的是实数（而非带虚部的复数），说明 `spherical_kn_reflection` 的 `.real` 生效。

**预期结果**：`jn 一致: True`；`yn 一致: True`；`kn(-1)` 为实数标量。若在你的环境里数值有出入，请标记「待本地验证」并重点检查 SciPy 版本。

> 说明：`yn` 的符号容易绕晕。最稳的对照方式是直接用公式 \(y_n(-z)=(-1)^{n+1}y_n(z)\) 写右端，而不是去推 `sign_n_even`。这也正是阅读装饰器源码的价值——把「奇偶符号」这种易错的细节集中到一处（`standard_reflection`），调用方只需给一个 `+1`/`-1`。

#### 4.3.6 小练习与答案

**练习 1**：`use_reflection` 的 wrapper 里，为什么对复数输入直接 `return fun(n, z, derivative)`，而不走反射？

> **参考答案**：复数内核（基于柱面 Bessel 函数的解析定义）能正确处理整个复平面，包括负实轴；反射奇偶公式只对**实数**输入有意义且必要。复数输入若也去翻符号，反而会破坏复平面上的正确解析延拓。所以只有 `np.issubdtype(z.dtype, np.complexfloating)` 为 False（即实数）时才需要反射。

**练习 2**：如果把 `xpx.apply_where(...)` 换成 `np.where(z.real >= 0, fun(n, z, d), f2(n, z, d))`，会出什么问题？

> **参考答案**：`np.where` 会**先 eagerly 求值两个分支再挑选**。于是 `fun(n, z, d)` 会在整个数组（含负元素）上被调用，而底层实数内核在 `z<0` 上不可靠，会返回错误值或 NaN——即便之后被 `where` 丢弃，错误已经发生（甚至可能触发告警/异常）。`apply_where` 只对各自相关的元素求值，规避了这个问题，同时保持 Array API 兼容。

**练习 3**：`spherical_kn` 为什么不像其余三个那样给一个 `sign_n_even` 就了事？

> **参考答案**：因为 \(k_n(-z)\) 与 \(k_n(z)\) 之间没有简单的 \(\pm(-1)^n\) 关系，反射会牵涉其他球 Bessel 函数的组合。硬实现需要多次函数求值且符号逻辑复杂；开发者选择更简单的等价路径——提升到复数做解析延拓再取实部——并用 `reflection_fun` 把这个自定义实现注入装饰器，复用外层的正/负半轴调度。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「读懂一个薄包装」的小任务：

1. **定位底层 ufunc**：打开 [_spherical_bessel.py:4-6](_spherical_bessel.py#L4-L6)，列出 `spherical_yn` 实际依赖的两个底层 ufunc（函数值 + 导数）。
2. **追踪一次调用的完整路径**：写下 `spherical_yn(2, -1.5, derivative=True)` 从 Python 入口到内核的完整步骤，需包含：(a) `n` 的 dtype 规整；(b) 装饰器 `wrapper` 接管；(c) 实数分支判定 `z.real >= 0`；(d) 负半轴走 `standard_reflection`，给出此时 `sign` 的具体数值（提示：\(n=2\) 偶、`sign_n_even=-1`、`derivative=True` 要再乘 \(-1\)）。
3. **用代码验证你的追踪**：算出 `standard_reflection` 在该点的「符号」与「翻正后的实参」，再用 `spherical_yn(2, +1.5, derivative=True)` 配合符号手动复现 `spherical_yn(2, -1.5, derivative=True)`，断言两者一致。
4. **对比 `spherical_kn`**：解释为什么同样的调用形式 `spherical_kn(2, -1.5)` 不会走 `standard_reflection`，而是走 `spherical_kn_reflection`（[_spherical_bessel.py:311-315](_spherical_bessel.py#L311-L315)）。

参考答案要点（步骤 2 的符号）：\(n=2\) 为偶 ⇒ 第一步符号 = `sign_n_even` = \(-1\)；`derivative=True` ⇒ 链式法则再乘 \(-1\) ⇒ 最终符号 = \(+1\)；翻正后的实参为 \(+1.5\)。所以 `spherical_yn(2, -1.5, derivative=True)` 应等于 \(+1 \cdot\) `spherical_yn(2, +1.5, derivative=True)`。可用 `np.isclose(...)` 验证。待本地验证。

## 6. 本讲小结

- **薄包装**是一层套在底层 ufunc 之外的纯 Python 函数，只做「ufunc 装不下」的收尾工作：带默认值的配置参数（`lambertw` 的 `k`/`tol`）、整数参数的 dtype 规整（`k`/`n` 强制成 `long`）。
- **derivative 分支**：一个布尔参数在 Python 层选择两个不同的底层 ufunc（`_xxx` 与 `_xxx_d`），因为 ufunc 的类型分发只看 dtype、看不到布尔开关。
- **use_reflection 装饰器**按 DLMF 10.47(v) 的奇偶反射，把实数负半轴输入翻到正半轴再补符号；符号由 `sign_n_even` 参数决定，`jn`/`in` 给 `+1`、`yn` 给 `-1`，函数体完全相同。
- **导数模式**通过链式法则多乘一个 \(-1\)（`sign = -sign if derivative else sign`）。
- **复数输入跳过反射**直接交给内核；实数输入用 `xpx.apply_where` 按 `z.real >= 0` 分两路求值，避免在负实数上误调内核。
- **spherical_kn 是特例**：它的反射关系不是简单符号翻转，故装饰器预留 `reflection_fun` 注入口，`spherical_kn_reflection` 用「提升复数 + 取实部」的解析延拓等效实现。

## 7. 下一步学习建议

- **往内核走**：本讲的 `_spherical_jn` 等底层 ufunc 是怎么从声明生成出来的？继续看 U3 代码生成管线，尤其是 u3-l2（`_generate_pyx.py`）和 u3-l4（C/C++ 后端版图），理解这些 `_xxx` ufunc 的来源。
- **往错误处理走**：薄包装层没有显式做错误处理，但底层 ufunc 在负实数、分支点附近可能触发 `sf_error`。建议接着读 u7-l1（sf_error 的 C→Python 桥）和 u7-l2，理解这些数值事件如何变成 Python 告警/异常。
- **往多后端走**：`use_reflection` 里特意用了 `xpx.apply_where` 而非 `np.where`，这是为 Array API 多后端铺路。学完 u10-l1（`_support_alternative_backends` 与 `_FuncInfo`）后再回看本讲，你会更清楚这层薄包装如何能在 PyTorch/JAX/CuPy 后端上同样工作。
- **对比阅读**：把本讲的「薄包装」与 u4-l1/u4-l2 的「重包装」（`_basic.py` 里整段算法用 Python 写的函数）对照，体会两种包装层在「Python 负责多少」上的取舍。
