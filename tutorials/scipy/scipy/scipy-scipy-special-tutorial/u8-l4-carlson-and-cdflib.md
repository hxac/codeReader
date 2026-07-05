# Carlson 椭圆积分与 cdflib：专项内核

## 1. 本讲目标

本讲聚焦 `scipy.special` 中两套「与主流量函数库风格不同」的专项 C/C++ 内核：

- **Carlson 对称椭圆积分**（`ellint_carlson_cpp_lite` + `ellint_carlson_wrap.*`）：支撑 `elliprf / elliprd / elliprg / elliprj / elliprc` 五个 ufunc。
- **cdflib**（`cdflib.c` / `cdflib.h`）：一套由 Fortran 翻译而来的经典概率分布反函数库。

学完本讲你应当能够：

1. 说清 Carlson 对称形式 \(R_F, R_D, R_G, R_J, R_C\) 在 `ellipr*` 函数族中的核心地位，以及它们的「复制定理」迭代算法。
2. 理解 cdflib 的历史来源、它返回 `(结果, 状态码, 边界)` 元组的接口约定，以及它**在当前 SciPy 中只服务少数几个「对自由度求逆」的函数**这一精细事实。
3. 读懂这两套内核在 `meson.build` 里截然不同的两种依赖组织方式：Carlson 是**纯头文件 source 依赖**（`declare_dependency(sources=...)`），cdflib 是**编译成静态库再 link**（`static_library` + `link_with`）。
4. 把本讲与 [u3-l4](u3-l4-cpp-backend-landscape.md)（后端版图）、[u7-l1](u7-l1-sf-error-c-layer.md)（C 层错误机制）串起来：两套内核的错误最终都汇入同一个 `sf_error` 管线。

## 2. 前置知识

- **椭圆积分的两种写法**：传统写法是 Legendre 形式（第一/二/三类 \(F, E, \Pi\)，带振幅 \(\phi\) 与模 \(k\)），但它们对参数不对称、数值上对某些参数组合不稳定；Carlson 提出的**对称形式**用 \(R_F(x,y,z)\) 等对称函数表达，参数地位平等、收敛快，已成为现代数值库的首选。
- **「自由度」与「非中心分布」**：F 分布、t 分布有「自由度」参数；「非中心」版本（ncf、nct）多一个「非中心参数」。「对自由度求逆」意思是：给定概率和另外的参数，反解出自由度的值。
- **Meson 的两种「依赖」**：`link_with` 把一个**已编译**的库链接进来；`declare_dependency(sources=...)` 则只是把一组**源文件（典型是头文件）**登记为依赖，让使用方把它们一起编译，**不产生独立的可链接对象**。这是本讲构建部分的关键区分。
- **模板实例化**：C++ 函数模板（如 `rf<T>`）的代码在用具体类型（`double` / `std::complex<double>`）调用时才「实例化」生成真实机器码，因此模板库通常是 header-only，由使用方在自己的 `.cxx` 里实例化。
- 承接 [u7-l1](u7-l1-sf-error-c-layer.md)：C 内核通过 `sf_error(...)` 跨 GIL 触发 Python 告警/异常；本讲会反复看到这同一个出口。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| [`ellint_carlson_wrap.hh`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/ellint_carlson_wrap.hh) | C++ 包装头：`#include` Carlson 模板库、定义 `ELLINT_NO_VALIDATE_RELATIVE_ERROR_BOUND` 宏、`extern "C"` 声明 10 个导出函数（实/复各 5 个）。 |
| [`ellint_carlson_wrap.cxx`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/ellint_carlson_wrap.cxx) | C++ 包装实现：把 `ellint_carlson::rf/rd/rg/rj/rc` 模板实例化为 `double` 与 `std::complex<double>`，套 `extern "C"` 壳与 `sf_error` 桥。 |
| [`ellint_carlson_cpp_lite/ellint_carlson.hh`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/ellint_carlson_cpp_lite/ellint_carlson.hh) | Carlson 库的聚合头，`#include` 了 `_rc/_rd/_rf/_rg/_rj` 等子头。 |
| [`ellint_carlson_cpp_lite/_rf.hh`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/ellint_carlson_cpp_lite/_rf.hh) | \(R_F\) 的复制定理算法实现（含退化情形 `rf0`、主迭代、7 阶 Taylor 展开）。 |
| [`cdflib.h`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cdflib.h) | cdflib 的 C 头：历史出处注释、返回值元组结构体（`TupleDDID` 等）、4 个 `cdf*_which*` 函数声明。 |
| [`_cdflib_wrappers.pxd`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_cdflib_wrappers.pxd) | Cython 头：`cdef extern from "cdflib.h"` 拉进 C 函数；定义 `get_result` 状态码映射与 4 个 `cdef inline` 包装函数。 |
| [`meson.build`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build) | 构建编排：`cdflib_lib` 静态库、`ellint_files`/`ellint_dep` 头文件依赖、各扩展模块的链接关系。 |
| [`functions.json`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/functions.json) | 声明表：`ellipr*` 走 `ellint_carlson_wrap.hh++`（C++）；4 个 cdflib 反函数走 `_cdflib_wrappers.pxd`（Cython）。 |

## 4. 核心概念与源码讲解

### 4.1 Carlson 对称椭圆积分与 ellipr* 函数族

#### 4.1.1 概念说明

椭圆积分出现于「椭圆弧长」「椭球引力势」「陀螺运动」等问题。Carlson 把所有这些表达成五个**对称**的不变函数，它们都有一个共同的积分定义。最重要的 \(R_F\) 定义为：

\[
R_F(x,y,z) = \tfrac{1}{2}\int_{0}^{\infty}\frac{dt}{\sqrt{(t+x)(t+y)(t+z)}}
\]

其余四个与它同源：

\[
\begin{aligned}
R_D(x,y,z) &= \tfrac{3}{2}\int_{0}^{\infty}\frac{dt}{\sqrt{(t+x)(t+y)}\,(t+z)^{3/2}} \\
R_J(x,y,z,p) &= \tfrac{3}{2}\int_{0}^{\infty}\frac{dt}{\sqrt{(t+x)(t+y)(t+z)}\,(t+p)} \\
R_C(x,y) &= R_F(x,y,y) \quad\text{（退化形式）}
\end{aligned}
\]

\(R_G\) 是一个对称的辅助组合（由 \(R_F, R_D\) 表达）。关键性质：**参数地位对称**、所有函数共享同一个**复制定理（duplication theorem）**迭代算法、收敛是二阶的（每步有效位数大致翻倍）。这就是为什么 `scipy.special` 的 `elliprf/rd/rg/rj/rc` 选择 Carlson 形式而非 Legendre 形式作为底层实现。

#### 4.1.2 核心流程

\(R_F\) 的计算不直接数值积分，而是利用 Carlson 复制定理：

\[
R_F(x,y,z) = R_F\!\left(\frac{x+\lambda}{4},\,\frac{y+\lambda}{4},\,\frac{z+\lambda}{4}\right),\quad
\lambda = \sqrt{x}\sqrt{y}+\sqrt{x}\sqrt{z}+\sqrt{y}\sqrt{z}
\]

反复套用该变换，三个变量会**二次收敛**到同一个值 \(A\)；当三者足够接近时，\(R_F\) 可用 \(A\) 处的 Taylor 展开一次性算出（DLMF 19.36.E1，一个 7 阶展开式）。伪代码：

```
function rf(x, y, z):
    参数合法性检查（ph_good：不能落在负实轴的「禁带」上）
    排序 (x,y,z) 取最小者 xm；若 xm 过小走退化分支 rf0
    A = (x+y+z)/3
    重复直到 max(|A-x|,|A-y|,|A-z|) 足够小:
        λ = √x·√y + √x·√z + √y·√z
        A,x,y,z ← (A+λ)/4, (x+λ)/4, (y+λ)/4, (z+λ)/4
    用 (A-x)/A、(A-y)/A 计算 E2、E3 项
    返回 (1 + 7阶Taylor展开) / √A
```

#### 4.1.3 源码精读

包装头先 `#include` 模板库并定义一个关掉「相对误差界校验」的宏（原因见下文），再以 `extern "C"` 暴露 10 个 C 链接符号：

[ellint_carlson_wrap.hh:8-9](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/ellint_carlson_wrap.hh#L8-L9) 引入 Carlson 库并关掉误差界校验。

[ellint_carlson_wrap.hh:15-28](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/ellint_carlson_wrap.hh#L15-L28) 声明实数版 `fellint_*` 与复数版 `cellint_*` 两组函数。命名约定：`f` = float/实数（返回 `double`）、`c` = complex（返回 `npy_cdouble`）。

包装实现里每个函数都是同一个骨架——把 `ellint_carlson::rf` 模板实例化、取回 `ExitStatus`、`static_cast` 成 `sf_error_t` 后交给 `sf_error`：

[ellint_carlson_wrap.cxx:62-85](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/ellint_carlson_wrap.cxx#L62-L85) 实/复两版 \(R_F\) 包装：实数版直接传 `double`；复数版先把两个 `npy_cdouble` 构造为 `std::complex<double>`，算完再 `npy_cpack` 拆回。最后一行 `sf_error("elliprf (real/complex)", status, NULL)` 是通往 [u7-l1](u7-l1-sf-error-c-layer.md) 错误管线的咽喉。

注意第 5 行的相对误差常数：

[ellint_carlson_wrap.cxx:5](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/ellint_carlson_wrap.cxx#L5) `ellip_rerr = 5e-16` 是 SciPy 要求的极紧容差。Carlson 库自带的校验会拒绝小于 `3.0e-4` 的容差（见 `_rf.hh` 的 `argcheck::invalid_rerr(rerr, 3.0e-4)`），因此包装头必须定义 `ELLINT_NO_VALIDATE_RELATIVE_ERROR_BOUND` 关掉这道校验、直接信任紧容差。

真正的复制定理算法在模板库里：

[_rf.hh:40-66](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/ellint_carlson_cpp_lite/_rf.hh#L40-L66) 退化情形 `rf0`（当某参数为 0 时 \(R_F\) 退化为 \(R_C\)），用 `agm_update`（算术-几何平均迭代）求解。

[_rf.hh:123-151](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/ellint_carlson_cpp_lite/_rf.hh#L123-L151) 主迭代循环，正是上文伪代码里「\(\lambda\) 更新 + 四分之一缩放」的 C++ 实现，每轮 `++m`，超过 `config::max_iter` 报 `ExitStatus::n_iter`。

[_rf.hh:159-173](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/ellint_carlson_cpp_lite/_rf.hh#L159-L173) 收敛后用 `E2/E3` 项做 7 阶 Taylor 展开（对应 DLMF 19.36.E1），返回 `s / std::sqrt(Am)`。

最后看注册侧：`functions.json` 把 5 个 `ellipr*` 都挂在 C++ 包装头下：

[functions.json:141-146](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/functions.json#L141-L146) `elliprf` 同时声明实数核 `fellint_RF: ddd->d` 与复数核 `cellint_RF: DDD->D`，头文件 `ellint_carlson_wrap.hh++` 的 `++` 后缀告诉生成器「这是 C++，走 `_ufuncs_cxx` 路径」（详见 [u3-l1](u3-l1-functions-json.md)）。

#### 4.1.4 代码实践

**实践目标**：用对称形式的一个简单恒等式验证 `elliprf` 的正确性——当三个参数相等时：

\[
R_F(x,x,x) = \frac{1}{\sqrt{x}}
\]

**操作步骤**：

```python
import numpy as np
from scipy.special import elliprf, elliprc, elliprg

# 恒等式 1：三参数相等
x = 3.0
print(elliprf(x, x, x))      # 应为 1/sqrt(x)
print(1.0 / np.sqrt(x))

# 恒等式 2：退化形式 R_C(x,y) = R_F(x,y,y)
print(elliprc(2.0, 3.0))
print(elliprf(2.0, 3.0, 3.0))

# 它们都是 ufunc，支持广播
xs = np.array([1.0, 2.0, 4.0])
print(elliprf(xs, xs, xs))   # 同形状数组输出
```

**需要观察的现象**：前两组 `print` 各自的两行数值应当几乎完全相等（误差约 \(10^{-16}\) 量级）；第三组输出是与 `xs` 同形状的数组。

**预期结果**：`elliprf(3,3,3)` ≈ 0.57735（即 \(1/\sqrt{3}\)）；`elliprc(2,3)` 与 `elliprf(2,3,3)` 相等。若结果不符，说明环境中的 `ellipr*` 未正确编译链接 Carlson 内核。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ellint_carlson_wrap.cxx` 要为每个函数同时提供 `fellint_*`（实）与 `cellint_*`（复）两个版本，而不是用一个统一函数？

**答案**：因为 `functions.json` 为每个 `ellipr*` 声明了 `ddd->d` 与 `DDD->D` 两条类型环（实数环 + 复数环），ufunc 的多类型分发需要两个独立的 C 入口符号；同时实数路径用 `double`、复数路径用 `std::complex<double>`，模板实例化类型不同，必须由两个 `extern "C"` 函数分别承接，再由 ufunc 层按输入 dtype 选择。

**练习 2**：阅读 [_rf.hh:79-85](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/ellint_carlson_cpp_lite/_rf.hh#L79-L85)。为什么这段 `#ifndef ELLINT_NO_VALIDATE_RELATIVE_ERROR_BOUND` 守卫的校验在 SciPy 中被关掉？

**答案**：SciPy 通过包装传入极紧的 `ellip_rerr = 5e-16`，远小于 Carlson 库内置校验的下限 `3.0e-4`；若不关掉校验，所有调用都会因 `invalid_rerr` 直接返回 `bad_rerr`。包装头用宏关掉这道防御性校验，转而信任紧容差并靠后续 `ExitStatus` 兜底。

### 4.2 cdflib：经典概率分布反函数内核

#### 4.2.1 概念说明

`cdflib` 是一套由 Fortran 翻译成 C 的概率分布库，源出 Netlib（见头注释）。它的覆盖面在原始 Fortran 里很广（Beta、Binomial、卡方、非中心卡方、F、非中心 F、Gamma、负二项、正态、Poisson、Student's t），核心算法是 **Bus–Dekker 零点查找**配合 TOMS 算法与 Abramowitz & Stegun 近似。

但**在当前 SciPy 中**，`cdflib` 只承担了一个很窄的角色——**只提供少数几个「对自由度求逆」的函数**。这一点与「分布正向 CDF/分位数」的直觉不同：`ncfdtr`（非中心 F 的 CDF）、`nctdtr`（非中心 t 的 CDF）这类**正向函数实际上走 Boost.Math**（见 [functions.json:390-393](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/functions.json#L390-L393)），而 cdflib 只在「给定概率反解自由度/非中心参数」这种 Boost 当时不够稳的场景里被调用。

经 `_cdflib_wrappers.pxd` 暴露的 cdflib 函数恰好 4 个：

| SciPy 函数 | 含义 | cdflib 内核 |
| --- | --- | --- |
| `fdtridfd` | F 分布：给定 p、f 反解 dfn | `cdff_which4` |
| `ncfdtridfd` | 非中心 F：给定 p、nc、f 反解 dfn | `cdffnc_which4` |
| `ncfdtridfn` | 非中心 F：给定 p、nc、f 反解 dfd | `cdffnc_which3` |
| `stdtridf` | Student's t：给定 p、t 反解 df | `cdft_which3` |

#### 4.2.2 核心流程

cdflib 的 C 函数不通过返回值直接给结果，而是统一返回一个「元组结构体」`(result, status, bound)`（类型为 `TupleDID` = double, int, double）。`status` 编码了求解状态：

```
status < 0            → 第 |status| 个输入参数越界（ARG 错误）
status == 0           → 成功，结果在 result
status == 1 / 2       → 答案低于/高于搜索边界（bound 字段给出边界值）
status == 3 / 4       → 内部两参数未和为 1（数值问题）
status == 10          → 计算错误
```

Cython 包装层 `get_result` 把这些状态码翻译成 `sf_error.error(...)` 调用（错误类别 `ARG` 或 `OTHER`），出错时返回 `NAN`：

```
function get_result(name, argnames, result, status, bound, return_bound):
    if status < 0:  sf_error(name, ARG, "参数 %s 越界", argnames[-status-1]); return NAN
    if status == 0: return result
    if status == 1: sf_error(..., "低于下界 %g", bound); return bound if return_bound else NAN
    if status == 2: sf_error(..., "高于上界 %g", bound); return bound if return_bound else NAN
    ... (3/4/10 → OTHER 错误，返回 NAN)
```

每个具体反函数包装（如 `ncfdtridfd`）就是：把 SciPy 参数按 cdflib 的 `(p, q, f, dfn, nc)` 顺序排好 → 调对应 `cdf*_which*` → 拆开元组 → 喂给 `get_result`。

#### 4.2.3 源码精读

头文件的注释明确交代了 cdflib 的血脉：

[cdflib.h:1-14](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cdflib.h#L1-L14) 说明这是「Fortran 翻译」，列出覆盖的分布与所用算法（TOMS、A&S、Bus–Dekker 零点查找），并指向 Netlib 原始代码。

返回元组结构体是 cdflib 的接口契约：

[cdflib.h:89-102](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cdflib.h#L89-L102) `TupleDID` 与 `TupleDDID` 结构体——cdflib 用结构体而非多返回值，因为 C 只能返回单值。

[cdflib.h:108-111](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/cdflib.h#L108-L111) 仅暴露 4 个 `cdf*_which*` 函数声明，对应上表的 4 个反函数。这是「cdflib 在 SciPy 里只服务 4 个函数」的硬证据——头文件里就只有这 4 个。

Cython 头把 C 函数拉进来并提供状态码翻译：

[_cdflib_wrappers.pxd:5-25](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_cdflib_wrappers.pxd#L5-L25) `cdef extern from "cdflib.h" nogil` 重新声明 4 个 C 函数（`nogil` 表示可在无 GIL 段调用）。

[_cdflib_wrappers.pxd:28-61](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_cdflib_wrappers.pxd#L28-L61) `get_result` 把整数状态码翻译为 `sf_error.ARG` / `sf_error.OTHER` 告警并返回 `NAN`，正是上文流程图的实现。

[_cdflib_wrappers.pxd:85-104](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_cdflib_wrappers.pxd#L85-L104) `ncfdtridfd` 包装：排参数 → 调 `cdffnc_which4` → 拆 `TupleDID` → `get_result`。注意它是 `cdef inline ... noexcept nogil`，会被内联进生成的 ufunc 内层循环。

注册侧，`functions.json` 把这 4 个函数挂在 Cython 头下（注意与 `ellipr*` 的 `hh++` 区别——这里是 `.pxd`，走纯 C 的 `_ufuncs` 路径）：

[functions.json:402-410](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/functions.json#L402-L410) `ncfdtridfd` 与 `ncfdtridfn` 都用 `_cdflib_wrappers.pxd` 头、只有 `dddd->d` 一条类型环（无 float32 双胞胎，也无复数）。

对照 [functions.json:390-399](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/functions.json#L390-L399)：正向 `ncfdtr`（CDF）与 `ncfdtri`（分位数）走 `boost_special_functions.h++`，配 `ncf_cdf_float/double` 双内核。**正向走 Boost、对自由度求逆走 cdflib**，这就是当前 SciPy 的分工。

#### 4.2.4 代码实践

**实践目标**：通过「正向 CDF → 对自由度求逆」的往返（round-trip）验证 cdflib 反函数，并直观感受它只负责「反解 df」这一窄角色。

**操作步骤**：

```python
from scipy import special

# 1) 正向：ncfdtr（Boost）给定 (dfn, dfd, nc, f) 算概率 p
dfn, dfd, nc, f = 3.0, 10.0, 5.0, 2.0
p = special.ncfdtr(dfn, dfd, nc, f)
print("p =", p)

# 2) 反解 dfd：ncfdtridf（Boost，反非中心参数/部分反解）
#    注意 ncfdtridfd / ncfdtridfn 才是 cdflib 提供的「对自由度求逆」
dfd_back = special.ncfdtridfn(p, dfn, nc, f)   # 反解 dfd
print("dfd_back =", dfd_back, "  原 dfd =", dfd)

# 3) 对比：stdtr（Boost 正向）与 stdtridf（cdflib 反解 df）
t, df = 1.5, 8.0
p2 = special.stdtr(df, t)
print("df_back =", special.stdtridf(p2, t), "  原 df =", df)
```

**需要观察的现象**：`dfd_back` 应当非常接近原 `dfd`（误差约 \(10^{-7}\) 量级，cdflib 的反解精度比正向 CDF 低）；同样 `df_back` 接近原 `df`。

**预期结果**：往返值与原值在前几位有效数字上一致。若偏差极大，先用 `special.geterr()` 确认错误未被静默吞掉。若想确认这些函数底层确实不同，可运行下面「待本地验证」片段。

**待本地验证**（确认符号所在扩展模块）：在已构建 SciPy 源码树中，可尝试 `python -c "import scipy.special._ufuncs as m; print([n for n in dir(m) if 'fdtri' in n])"` 观察哪些反函数符号出现在 `_ufuncs` 扩展里（cdflib 反函数应在此）；具体符号名依赖构建产物，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：`cdflib.h` 里只声明了 4 个 `cdf*_which*` 函数，但原始 Fortran cdflib 覆盖了十余种分布。结合 [functions.json:390-399](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/functions.json#L390-L399) 说明为什么 SciPy 只保留了这 4 个。

**答案**：因为大部分分布的正向 CDF / 分位数 / 反非中心参数都已被 Boost.Math 接管（更现代、有 float32 双内核、错误经 policy 桥接，见 [u8-l2](u8-l2-boost-integration.md)）；cdflib 只在「对自由度 df 求逆」这一 Boost 当时不够成熟的方向上保留优势，所以 SciPy 仅暴露这 4 个 `which` 函数，其余 cdflib 代码随静态库存在但不被直接调用。

**练习 2**：阅读 [_cdflib_wrappers.pxd:38-61](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_cdflib_wrappers.pxd#L38-L61)。为什么 `status==1/2`（答案越界）时有时返回 `bound`、有时返回 `NAN`？

**答案**：`get_result` 有一个 `return_bound` 布尔参数。某些反函数在越界时返回边界值仍有物理意义（如 `fdtridfd` 把 df 钳到边界），此时 `return_bound=1`；另一些则宁可返回 `NAN` 表示「无解」。调用方按语义选择，这是 cdflib 风格「软失败」与 NumPy 风格「硬失败」之间的折中。

### 4.3 专项内核在 meson.build 中的依赖组织

#### 4.3.1 概念说明

两套专项内核虽然都是「`functions.json` 声明 → 代码生成 → 编译成 ufunc」，但它们在 `meson.build` 里的**依赖组织方式截然不同**，原因是它们的语言与编译模型不同：

- **Carlson（C++ 模板库）**：header-only，函数都是模板 `rf<T>`，必须在使用方用具体类型实例化。Meson 里用 `declare_dependency(sources=ellint_files)` 把头文件登记为**源依赖**，让 `_ufuncs_cxx` 在编译 `ellint_carlson_wrap.cxx` 时一起把这些头当输入编译。**没有独立的可链接库**。
- **cdflib（纯 C）**：是一个完整的 `.c` 翻译单元，预先编译成**静态库 `cdflib_lib`**，再 `link_with` 进需要它的扩展模块。可链接，符号经 `extern "C"` / C 链接解析。

这是 Meson 工程里「header-only 库」与「传统编译库」两种范式的对照样本。

#### 4.3.2 核心流程

```
Carlson 路径：
  ellint_files（一组 .hh 头）
    → declare_dependency(sources=ellint_files)   # 不编译，只登记
    → 作为 dependencies 进入 _ufuncs_cxx
    → ellint_carlson_wrap.cxx 在 _ufuncs_cxx 内 #include 这些头并实例化模板
    → 经 functions.json 的 ellint_carlson_wrap.hh++ 注册成 ellipr* ufunc

cdflib 路径：
  cdflib.c
    → static_library('cdflib', 'cdflib.c', ...)  # 预编译成静态库 cdflib_lib
    → link_with: cdflib_lib 进入 _ufuncs 与 cython_special
    → _cdflib_wrappers.pxd 内联调用库里的 cdf*_which*
    → 经 functions.json 的 _cdflib_wrappers.pxd 注册成 4 个反函数 ufunc
```

注意归属：Carlson 编进 `_ufuncs_cxx`（C++ 扩展），cdflib 链接进 `_ufuncs`（C 扩展）与 `cython_special`。

#### 4.3.3 源码精读

cdflib 静态库定义在 meson.build 顶部：

[meson.build:26-30](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L26-L30) `static_library('cdflib', 'cdflib.c', ...)` 把 `cdflib.c` 编译成静态库，`gnu_symbol_visibility: 'hidden'` 表示默认不对外暴露符号（只供本构建内部 link）。

[meson.build:92-108](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L92-L108) `_ufuncs` 扩展模块：注意 `link_with: cdflib_lib`（第 105 行）把 cdflib 静态库链接进来，但它的 `dependencies` 里**没有** `ellint_dep`——因为 Carlson 是 C++，不属于这个 C 扩展。

[meson.build:157-173](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L157-L173) `cython_special` 同样 `link_with: cdflib_lib`（第 170 行），因为 cython_special 的标量 API 也需要能调到 cdflib 反函数。

Carlson 头文件依赖定义在中段：

[meson.build:110-123](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L110-L123) `ellint_files` 列出包装头 + `ellint_carlson_cpp_lite/` 下 11 个实现头（`_rc/_rd/_rf/_rg/_rj` + 算术/类型/校验辅助头）。

[meson.build:125](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L125) `ellint_dep = declare_dependency(sources: ellint_files)`——注意是 `sources` 而非 `link_with`，这就是 header-only 库依赖的 Meson 写法：头文件被登记为依赖项参与变更追踪，但不产生独立可链接对象。

[meson.build:134-144](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L134-L144) `_ufuncs_cxx` 扩展：`dependencies` 里同时有 `boost_math_dep`、`xsf_dep`、`ellint_dep`；而 `ellint_carlson_wrap.cxx`（在 [meson.build:21-24](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L21-L24) 定义的 `ufuncs_cxx_sources` 里）正是在这个扩展内编译并实例化 Carlson 模板的。

最后看一个易被忽略的衔接：`_cdflib_wrappers.pxd` 必须被复制进构建目录，生成的 `_ufuncs.pyx` 才能 `cdef extern` 引用它：

[meson.build:1-12](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L1-L12) `_ufuncs_pxi_pxd_sources` 用 `fs.copyfile('_cdflib_wrappers.pxd')` 把它列入构建期复制的 `.pxd/.pxi` 清单，供生成的 `_ufuncs.pyx` 内联使用。

#### 4.3.4 代码实践

**实践目标**：在 `meson.build` 中定位 `ellint_files` / `ellint_dep` 与 `cdflib_lib`，用一句话说明 Carlson 头文件为何是 source 依赖、cdflib 为何是静态库依赖，并把两套内核的「归属扩展模块」整理成对照表。

**操作步骤**：

1. 打开 [`scipy/special/meson.build`](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build)。
2. 搜索 `cdflib_lib`（第 26 行定义），再搜索它被 `link_with` 的两处（`_ufuncs` 第 105 行、`cython_special` 第 170 行）。
3. 搜索 `ellint_files`（第 110 行）与 `ellint_dep`（第 125 行），确认 `ellint_dep` 出现在 `_ufuncs_cxx` 的 `dependencies`（第 141 行），且**不**出现在 `_ufuncs` 里。
4. 验证 `ellint_carlson_wrap.cxx` 属于 `ufuncs_cxx_sources`（第 21-24 行），因此编译进 `_ufuncs_cxx`。
5. 整理对照表：

| 内核 | 语言 | 依赖形式 | 定义位置 | 归属扩展 | 注册头字段 |
| --- | --- | --- | --- | --- | --- |
| Carlson | C++ 模板 | source 依赖（`declare_dependency(sources=...)`） | `meson.build:110-125` | `_ufuncs_cxx` | `ellint_carlson_wrap.hh++` |
| cdflib | 纯 C | 静态库（`static_library` + `link_with`） | `meson.build:26-30` | `_ufuncs`、`cython_special` | `_cdflib_wrappers.pxd` |

**需要观察的现象**：你会看到 Carlson 用 `sources`、cdflib 用 `static_library`，两者泾渭分明；且 Carlson 只在 `_ufuncs_cxx`、cdflib 只在 `_ufuncs`/`cython_special`，正好对应各自的语言。

**预期结果**：你能用一句话回答——「Carlson 是 header-only 模板库，必须在使用方实例化，故登记为 source 依赖编进 `_ufuncs_cxx`；cdflib 是完整 C 翻译单元，预编译成静态库 `cdflib_lib` 再 link 给 `_ufuncs`/`cython_special`」。

最后做一次可用性验证（实践任务要求的硬验证）：

```python
from scipy.special import elliprf
print(elliprf(1.0, 2.0, 0.0))   # Carlson RF，应返回约 1.31103
```

**预期结果**：返回一个有限正数（约 1.31103）。若抛 `ImportError` 或返回异常，说明 Carlson 内核未随 `_ufuncs_cxx` 正确构建。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `ellint_dep` 从 `_ufuncs_cxx` 的 `dependencies` 里删掉、改为在 `ellint_carlson_wrap.cxx` 里直接 `#include "ellint_carlson_cpp_lite/ellint_carlson.hh"`，构建还能正常工作吗？为什么 Meson 仍坚持用 `declare_dependency(sources=...)`？

**答案**：理论上只要头文件在包含路径里就能编译通过。但 `declare_dependency(sources=ellint_files)` 的真正价值在**依赖追踪**：Meson 会把这 11 个头登记为 `_ufuncs_cxx` 的输入，任何头文件被修改都会触发 `_ufuncs_cxx` 重新编译；若仅靠 `#include` 而不登记，增量构建可能用过期的 `.so`。此外 `sources` 还保证头文件被正确拷贝/可见。这是 Meson 推荐的 header-only 库工程化写法。

**练习 2**：`cdflib_lib` 用了 `gnu_symbol_visibility: 'hidden'`。结合 cdflib 在 SciPy 中的角色，这有什么好处？

**答案**：`hidden` 让 cdflib 的符号默认不进入动态符号表，只在 Meson 构建内部 `link_with` 时可见。好处是：这些经典 Fortran 风格的短名（如 `cdff_which4`）不会污染最终 `scipy.special` 扩展模块的导出符号空间，避免与其他库的符号冲突，也减小 `.so` 体积；SciPy 只通过 `_cdflib_wrappers.pxd` 的内联包装暴露需要的那 4 个函数。

## 5. 综合实践

**任务**：把本讲三块知识串起来——验证 Carlson 恒等式、验证 cdflib 往返、并用构建知识预测每个函数「住在哪个扩展模块」。

**步骤**：

1. **Carlson 自洽性**。用一个非平凡恒等式同时验证 5 个 `ellipr*` 中的至少两个。已知完全椭圆积分 \(K(k)\) 与 Carlson 形式的关系：

   \[
   K(k) = R_F(0,\,1-k^2,\,1)
   \]

   ```python
   import numpy as np
   from scipy.special import elliprf, ellipk
   k = 0.7
   print(elliprf(0.0, 1 - k**2, 1.0))   # Carlson 形式
   print(ellipk(k))                      # 传统第一类完全椭圆积分
   ```

   两行应几乎相等，证明 `elliprf` 与 `ellipk` 共享同一套底层（`ellipk` 在内部也会归结到 Carlson 或等价实现）。

2. **cdflib 往返**。用 `stdtr`（Boost 正向）→ `stdtridf`（cdflib 反解 df）验证往返，并故意给一个越界输入观察 `sf_error` 的 `ARG` 告警：

   ```python
   from scipy import special
   df, t = 10.0, 2.0
   p = special.stdtr(df, t)
   print(special.stdtridf(p, t), df)          # 应接近 10.0
   # 越界：概率必须在 [0,1]
   with np.errstate(all='ignore'):
       print(special.stdtridf(2.0, t))         # 触发 ARG 错误，返回 nan
   ```

   把 `np.errstate(all='ignore')` 换成 `special.errstate(arg='raise')`（见 [u2-l3](u2-l3-error-handling.md)）应抛出 `SpecialFunctionError`，证明 cdflib 的状态码确经 `sf_error` 管线转成 Python 信号。

3. **构建预测**。基于本讲第 4.3 节，回答：
   - `elliprf` 的内核符号 `fellint_RF` 编译进哪个扩展模块？（答：`_ufuncs_cxx`）
   - `stdtridf` 调用的 `cdft_which3` 由哪个静态库提供、被链接进哪个扩展模块？（答：`cdflib_lib`，链接进 `_ufuncs`）

**验收标准**：步骤 1 两数差约 \(10^{-15}\)；步骤 2 往返值与原值前几位一致、越界输入返回 `nan`；步骤 3 两个回答与 4.3.4 的对照表一致。

## 6. 本讲小结

- `scipy.special` 的 `elliprf/rd/rg/rj/rc` 底层是 **Carlson 对称椭圆积分**，五个函数共享「复制定理」迭代 + 7 阶 Taylor 展开的统一算法（[DLMF 19](https://dlmf.nist.gov/19)），由 header-only 的 C++ 模板库 `ellint_carlson_cpp_lite` 实现，经 `ellint_carlson_wrap.*` 的 `extern "C"` 壳暴露给 ufunc。
- **cdflib** 是 Fortran 翻译而来的经典分布库，但在当前 SciPy 中**只服务 4 个「对自由度求逆」的函数**（`fdtridfd`/`ncfdtridfd`/`ncfdtridfn`/`stdtridf`）；正向 CDF 如 `ncfdtr`/`nctdtr` 实际走 Boost。它返回 `(结果, 状态码, 边界)` 元组，由 `_cdflib_wrappers.pxd` 翻译成 `sf_error` 信号。
- 两套内核在 `meson.build` 里用了**截然不同**的依赖范式：Carlson 是 `declare_dependency(sources=...)` 的**源（头文件）依赖**，编进 `_ufuncs_cxx`；cdflib 是 `static_library('cdflib', ...)` 的**静态库**，`link_with` 进 `_ufuncs` 与 `cython_special`。这对应「C++ 模板需实例化」与「纯 C 可预编译」的本质差异。
- 两者的错误最终都汇入 [u7-l1](u7-l1-sf-error-c-layer.md) 的同一个 `sf_error` 管线：Carlson 把 `ExitStatus` `static_cast` 为 `sf_error_t`；cdflib 经 `get_result` 把整数状态码映射为 `sf_error.ARG/OTHER`。
- `functions.json` 的头文件字段决定了路由：`ellint_carlson_wrap.hh++`（`++` ⇒ C++ ⇒ `_ufuncs_cxx`）与 `_cdflib_wrappers.pxd`（`.pxd` ⇒ Cython 头 ⇒ `_ufuncs`）。

## 7. 下一步学习建议

- 若想看「另一条不经 `functions.json` 的 ufunc 注册路径」如何处理专项内核，回看 [u8-l3](u8-l3-special-ufuncs-registration.md)（`_special_ufuncs.cpp` / `_gufuncs.cpp` 的 C++ 直注册）。
- 若想深入 Carlson 公式的数学与历史，直接读源码引用的 [DLMF Chapter 19](https://dlmf.nist.gov/19) 与 Carlson 1995 论文（`_rf.hh` 顶部注释给出 arXiv 链接）。
- 若想看更多「header-only 库 + `declare_dependency`」的工程实践，可对比本仓库 `scipy/_lib` 或其他 header-only 依赖在各自 `meson.build` 中的写法。
- 下一站建议进入 [u9-l1](u9-l1-testutils-funcdata.md)：看 `FuncData` 如何用 `tests/data/` 下的参考数据校验这些专项内核（含椭圆积分与非中心分布）的数值正确性。
