# C/C++ 后端版图：xsf、Boost、Cephes、cdflib、specfun

## 1. 本讲目标

学完本讲，你应当能够：

- 说出 `scipy.special` 背后到底有几套 C/C++ 数学后端，它们各自的历史定位是什么；
- 读懂 `functions.json` 中「头文件」字段的命名约定（`.h` / `.h++` / `.pxd`），并据此判断任意一个 ufunc 走的是哪条后端链路；
- 理解为什么同一个数学问题（比如不完全 Beta 函数）会被路由到 Boost 而不是 Cephes；
- 认识 `xsf` 作为 SciPy 自研的新一代统一 C++ 库，是如何逐步吸收 Cephes、specfun 等遗留库的，以及「双轨注册」（生成式 `.pyx` vs 直接 C++ 注册）的现状。

本讲是 U3 代码生成管线的收口讲：u3-l1 讲了「声明长什么样」，u3-l2 讲了「声明怎么变成代码」，u3-l3 讲了「代码怎么编成扩展模块」。本讲回答最后一个问题——**这些扩展模块里调用的 C/C++ 数学内核，到底来自哪里、如何被选择**。

## 2. 前置知识

阅读本讲前，你需要掌握以下概念（前几讲已建立）：

- **ufunc 与类型签名**：`scipy.special` 几乎都是 NumPy ufunc，用单字符类型码（`f`/`d`/`g` 实数、`F`/`D`/`G` 复数、`i`/`l`/`p` 整数）描述 `输入->返回` 的签名（见 u2-l1、u3-l1）。
- **`functions.json` 三层结构**：`函数名 → 头文件 → {内核函数名: 类型签名}`（见 u3-l1）。本讲重点关注中间那一层「头文件」。
- **生成式 vs 直注册两条路径**：`functions.json` 里登记的函数由 `_generate_pyx.py` 生成 `_ufuncs.pyx` / `_ufuncs_cxx.pyx`；而 `special_ufuncs` 名单内的函数改走 `_special_ufuncs.cpp` 直接用 C++ 注册 ufunc（见 u3-l2、u3-l3）。
- **Meson 扩展模块**：special 由 `_ufuncs`、`_ufuncs_cxx`、`_special_ufuncs`、`_gufuncs`、`_specfun`、`_ellip_harm_2`、`cython_special` 七个扩展模块 + 一个 `cdflib` 静态库组成（见 u3-l3）。

本讲不要求你懂 C++ 模板或 Boost 的细节——遇到时会就地解释。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 角色 |
|------|------|
| [`functions.json`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/functions.json) | 声明表。中间层「头文件」字段就是本讲的**调度总开关**。 |
| [`xsf_wrappers.h`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/xsf_wrappers.h) / [`xsf_wrappers.cpp`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/xsf_wrappers.cpp) | xsf 库的 C 可调用包装层（`extern "C"`），是 `_ufuncs.pyx` 调用 C++ 内核的桥。 |
| [`boost_special_functions.h`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/boost_special_functions.h) | Boost.Math 集成层：定义策略 + 把 Boost 异常桥接成 Python 告警/异常。 |
| [`_legacy.pxd`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_legacy.pxd) | Cephes 内核的 Cython 包装，处理「历史遗留的浮点→整数静默截断」。 |
| [`_cdflib_wrappers.pxd`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_cdflib_wrappers.pxd) | cdflib（概率分布 CDF/分位数）的 Cython 包装。 |
| [`ellint_carlson_wrap.hh`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/ellint_carlson_wrap.hh) | Carlson 对称椭圆积分的轻量 C++ 包装。 |
| [`_specfun.pyx`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_specfun.pyx) | Zhang & Jin specfun 库（现已迁入 xsf 命名空间）的序列型函数入口。 |
| [`meson.build`](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/meson.build) | 把上述后端链接进各扩展模块的构建编排。 |

> 提示：xsf 库的**实现**（`xsf/*.h`、`xsf/cephes/*.h`）不在 `special/` 目录内，而是位于 `scipy/_lib/xsf/`，通过 Meson 的 `xsf_dep` 依赖引入。本讲通过 `xsf_wrappers.cpp` 的 `#include <xsf/...>` 来观察它。

## 4. 核心概念与源码讲解

### 4.1 后端调度：functions.json 的「头文件」字段如何选择内核

#### 4.1.1 概念说明

当你在 Python 里写 `special.betainc(2, 3, 0.5)` 时，最终一定落在某个 C/C++ 函数上。问题是：**到底落在哪个库的哪个函数上？** 这不是运行时决定的，而是**构建时**由 `functions.json` 的「头文件」字段一次性钉死的。

回顾 u3-l1：`functions.json` 是三层嵌套——

```
函数名(顶层)  →  头文件(中层)  →  {内核函数名: 类型签名}(内层)
```

中层的「头文件」名同时编码了两条信息：

1. **语言**：以 `.h` 结尾是 C 头，以 `.h++`（或 `.hh++`）结尾是 C++ 头，以 `.pxd` 结尾是 Cython 头。
2. **后端库**：文件名本身（`xsf_wrappers.h`、`boost_special_functions.h++`、`_legacy.pxd`、`_cdflib_wrappers.pxd`、`ellint_carlson_wrap.hh++`、`orthogonal_eval.pxd`）直接对应一套数学后端。

生成器 `_generate_pyx.py` 读到这个头文件名后，会据此决定把内核函数 `cimport` 进哪个 `.pyx`、链接哪个依赖（见 u3-l2 的 C/C++ 分离）。所以「头文件」字段就是后端调度的总开关。

#### 4.1.2 核心流程

一条 ufunc 从声明到后端的调度链路：

```text
functions.json 条目
   │  中层 key = 头文件名
   ├─ 若以 "++" 结尾（C++） → 内核进 _ufuncs_cxx.pyx → 编进 _ufuncs_cxx 扩展
   │     ├─ boost_special_functions.h++   → Boost.Math（链接 boost_math_dep）
   │     └─ ellint_carlson_wrap.hh++      → Carlson 轻量 C++（依赖 ellint_dep）
   └─ 否则（C 或 Cython）→ 内核进 _ufuncs.pyx → 编进 _ufuncs 扩展
         ├─ xsf_wrappers.h               → xsf 库（依赖 xsf_dep，且 link cdflib_lib）
         ├─ _legacy.pxd                  → Cephes（经遗留截断包装）
         ├─ _cdflib_wrappers.pxd         → cdflib（link cdflib_lib）
         └─ orthogonal_eval.pxd          → Cython 自实现的正交多项式求值
```

关键判据只有一句：**看头文件名是否以 `++` 结尾，就知道这个内核是 C 还是 C++**（这是 u3-l1 已建立的规则，本讲把它落到具体后端库上）。

#### 4.1.3 源码精读

看几个真实的「一个函数、多个后端」的条目，理解头文件如何同时调度多个内核。

`bdtr`（二项分布 CDF）同时声明了两个来源——`_legacy.pxd`（遗留安全包装）和 `xsf_wrappers.h`（xsf/Cephes 内核）：

- [functions.json:25-32](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/functions.json#L25-L32) —— `bdtr` 把 `bdtr_unsafe`（遗留）和 `cephes_bdtr_wrap`（Cephes 经 xsf 暴露）并列。生成器会按类型签名 `ddd->d` / `dpd->d` 生成多个 loop，运行时按输入 dtype 选择。

`hyp1f1`（合流超几何函数）则把 Boost（实数双精度）和 xsf（复数）两条后端拼在一起：

- [functions.json:296-302](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/functions.json#L296-L302) —— `hyp1f1` 同时挂 `boost_special_functions.h++` 的 `hyp1f1_double`（`ddd->d`）和 `xsf_wrappers.h` 的 `chyp1f1_wrap`（`ddD->D`）。**实数走 Boost，复数走 xsf**——这就是「同一函数、按 dtype 分发到不同后端」的典型例子。

`elliprf`（Carlson 椭圆积分）走的是 C++ 但非 Boost 的专项后端：

- [functions.json:141-146](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/functions.json#L141-L146) —— 头文件 `ellint_carlson_wrap.hh++`，内核 `fellint_RF`（实 `ddd->d`）与 `cellint_RF`（复 `DDD->D`）。

> 小结：**头文件名 = 后端路由表**。记住这张「名→库」对照，你就掌握了 special 的整个后端版图。

#### 4.1.4 代码实践

**实践目标**：亲手验证「头文件名决定后端」这条规则。

**操作步骤**：

1. 打开 `functions.json`，定位 `betainc`、`erfinv`、`sici` 三个条目，记下它们各自的中层头文件名。
2. 对照本讲 4.1.2 的路由表，判断这三个函数分别走哪条后端链路、进哪个扩展模块。
3. 用下面这条命令统计每个后端头文件在 `functions.json` 中被引用的次数：

```bash
cd scipy/special
for h in boost_special_functions.h++ xsf_wrappers.h _legacy.pxd \
         _cdflib_wrappers.pxd ellint_carlson_wrap.hh++ orthogonal_eval.pxd; do
    printf '%-32s %s\n' "$h" "$(grep -c "\"$h\"" functions.json)"
done
```

**预期结果**（参考统计，作为待本地验证的核对值）：

| 头文件 | 后端 | 出现次数 |
|--------|------|----------|
| `boost_special_functions.h++` | Boost.Math | 约 84 |
| `xsf_wrappers.h` | xsf / Cephes | 约 18 |
| `orthogonal_eval.pxd` | Cython 正交多项式 | 约 15 |
| `_legacy.pxd` | Cephes 遗留截断包装 | 约 16 |
| `ellint_carlson_wrap.hh++` | Carlson C++ | 约 5 |
| `_cdflib_wrappers.pxd` | cdflib | 约 4 |

> 注意：这个统计**只覆盖 `functions.json`（生成式路径）**。大量 xsf 函数其实通过 `_special_ufuncs.cpp` 直接 C++ 注册，根本不在 `functions.json` 里（见 4.2.4）。所以「约 18 个 xsf」绝不代表 xsf 在 special 中的真实占比。

#### 4.1.5 小练习与答案

**练习 1**：`functions.json` 里 `fdtridfd` 的头文件是 `_cdflib_wrappers.pxd`，而 `fdtri` 的头文件是 `boost_special_functions.h++`。这两个都是 F 分布相关函数，为什么分别走不同后端？

**参考答案**：`fdtri` 是求 F 分布的 CDF 反函数（ppf），Boost.Math 有高质量实现且能干净映射错误；而 `fdtridfd` 是「给定概率反求自由度 dfd」，这是 cdflib 擅长的「反解某个参数」类问题（cdflib 的 `which` 机制专门干这个），所以交给 cdflib。后端选择遵循「用最合适的工具」而非「统一用一个库」。

**练习 2**：仅凭头文件后缀，如何判断一个内核会被编进 `_ufuncs` 还是 `_ufuncs_cxx`？

**参考答案**：看头文件名是否以 `++` 结尾——是则 C++ 内核（Boost、Carlson），进 `_ufuncs_cxx`；否则 C/Cython 内核（xsf、Cephes、cdflib、正交多项式），进 `_ufuncs`。原因见 u3-l2：把 Boost 这类重型 C++ 隔离编译。

---

### 4.2 xsf 库：现代统一 C++ 特殊函数库

#### 4.2.1 概念说明

**xsf**（extended special functions）是 SciPy 自研的现代化 C++ 特殊函数库，其实现位于 `scipy/_lib/xsf/`，所有函数位于 `xsf::` 命名空间下。它是 special 后端版图里**最年轻、最核心**的一套库，承担两重角色：

1. **新函数的首选实现**：新加入 special 的特殊函数，优先用 xsf 的 C++ 实现。
2. **遗留库的统一收容所**：Cephes（经典 C 库）已被整体迁入 xsf 作为 `xsf/cephes/` 子目录；Zhang & Jin 的 specfun 也以 `xsf::specfun` 形式被收编。也就是说，xsf 正在把历史上散落的几套库「统一口径」。

但 xsf 是 C++ 库，而 ufunc 的内层循环需要 **C 链接**（`extern "C"`）的可调用符号。`xsf_wrappers.cpp` / `xsf_wrappers.h` 就是这层桥——它把 `xsf::` 的 C++ 函数包成带 C 链接的包装函数，再由 `_ufuncs.pyx` 调用。

#### 4.2.2 核心流程

xsf 内核从 C++ 到 ufunc 的桥接链路：

```text
xsf::agm(a,b)  [C++ 实现, scipy/_lib/xsf/agm.h]
        │  被 xsf_wrappers.cpp 调用
        ▼
special_agm(double,double)  [extern "C" 包装, 暴露在 xsf_wrappers.h]
        │  被 functions.json 的 "xsf_wrappers.h" 条目引用
        ▼
_generate_pyx.py 生成 _ufuncs.pyx 的内层循环
        ▼
编进 _ufuncs 扩展模块 (依赖 xsf_dep)
```

复数类型有个额外动作：xsf 用 `std::complex<double>`，而 NumPy 用 `npy_cdouble`，两者内存布局一致但类型不同，wrapper 里要做一次 `to_complex`/`to_ccomplex` 转换（详见 u8-l1）。

#### 4.2.3 源码精读

先看 `xsf_wrappers.h` 的整体面貌——它是一个纯声明头，全部包在 `extern "C"` 里，函数名带前缀区分来源：

- [xsf_wrappers.h:1-8](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/xsf_wrappers.h#L1-L8) —— 文件头注释点明历史：原本是 Zhang & Jin 的 Fortran 库包装，后来演化为统一的 xsf 桥。
- [xsf_wrappers.h:18-20](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/xsf_wrappers.h#L18-L20) —— `#ifdef __cplusplus extern "C"` 块开始，保证这些符号以 C 链接暴露，可被 Cython `cimport`。
- [xsf_wrappers.h:97](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/xsf_wrappers.h#L97) —— `special_agm` 声明：`double special_agm(double a, double b)`，算术-几何平均，典型 xsf 新函数。
- [xsf_wrappers.h:22](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/xsf_wrappers.h#L22) —— `chyp1f1_wrap` 声明：复数合流超几何，参数用 `npy_cdouble`（NumPy 复数），返回 `npy_cdouble`。
- [xsf_wrappers.h:165](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/xsf_wrappers.h#L165) —— `xsf_sici` 声明：正弦/余弦积分，返回 `int` 状态码（多输出经指针参数）。

函数名前缀反映了三股来源的融合：`xsf_*` 是 xsf 原生 C++ 实现（如 `xsf_sici`），`cephes_*` 是迁入 xsf 的 Cephes 经典实现（如 `cephes_bdtr_wrap`），`special_*` 是统一样式的包装命名（如 `special_agm`、`special_cyl_bessel_j`）。

再看 `xsf_wrappers.cpp` 的 `#include` 区，能直观看到 xsf 库的覆盖面，以及它对 Cephes 的收编：

- [xsf_wrappers.cpp:4-44](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/xsf_wrappers.cpp#L4-L44) —— 一长串 `#include <xsf/*.h>`（agm、airy、bessel、gamma、erf、hyp2f1……），说明 xsf 已覆盖绝大部分函数族。
- [xsf_wrappers.cpp:46-60](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/xsf_wrappers.cpp#L46-L60) —— `#include <xsf/cephes/*.h>`：**Cephes 已被整体搬进 xsf 命名空间下**（`xsf/cephes/cbrt.h`、`xsf/cephes/expn.h`、`xsf/cephes/igam.h` 等）。这正是「xsf 作为统一收容所」的实证。

最后看构建侧如何把 xsf 接入：

- [meson.build:14-19](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/meson.build#L14-L19) —— `ufuncs_sources` 含 `xsf_wrappers.cpp`，即 xsf 包装层是 `_ufuncs` 扩展的源码之一。
- [meson.build:100-102](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/meson.build#L100-L102) —— `_ufuncs` 的 `dependencies` 含 `xsf_dep`，即链接外部 xsf C++ 库。

#### 4.2.4 代码实践

**实践目标**：体会 xsf 的「双轨」存在——既在 `functions.json` 里被引用，又在 `_special_ufuncs.cpp` 里被直接 C++ 注册。

**操作步骤**：

1. 在 `functions.json` 中确认 `sici`、`hyp1f1` 走 `xsf_wrappers.h`（生成式路径）。
2. 打开 `_special_ufuncs.cpp`，搜索 `xsf::numpy::ufunc`，看 xsf 函数如何不经 `functions.json` 直接注册为 ufunc。

**源码阅读型实践**——观察直接注册语法：

- [_special_ufuncs.cpp:285](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_special_ufuncs.cpp#L285) —— `xsf::numpy::ufunc({static_cast<xsf::numpy::f_f>(xsf::cospi), ...}, ...)` 直接把 `xsf::cospi` 注册成一个 ufunc，**整条路径不经过 `functions.json`、不经过 `_generate_pyx.py`**。

**需要观察的现象**：`functions.json` 里找不到 `cospi`（它已被改造成直接注册），但 `hyp1f1` 仍在 `functions.json` 里。这说明 xsf 函数正处在「从生成式路径迁移到直接 C++ 注册路径」的过程中——这是学习目标里「xsf 作为新一代统一库的趋势」的具体表现。迁移趋势的完整机制留待 u8-l3 详解。

**预期结果**：你能指出 `sici` 属于生成式路径（在 `functions.json`），而 `cospi` 属于直接注册路径（在 `_special_ufuncs.cpp`）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 xsf 的 C++ 函数不能被 `_ufuncs.pyx` 直接 `cimport`，而要先过 `xsf_wrappers.h` 这一层？

**参考答案**：ufunc 内层循环要求符号是 **C 链接**（`extern "C"`）且名字不经过 C++ name mangling；xsf 函数是 C++ 的、在 `xsf::` 命名空间里，符号名会被 mangle。`xsf_wrappers.h` 用 `extern "C"` 暴露一组稳定的 C 符号，屏蔽 mangling 与 `std::complex`/`npy_cdouble` 类型差异，使 Cython 层能稳定调用。

**练习 2**：Cephes 现在算 xsf 的一部分吗？依据是什么？

**参考答案**：算。依据是 `xsf_wrappers.cpp` 里 `#include <xsf/cephes/*.h>`——Cephes 的源码已被搬进 xsf 目录树下作为子模块。所以 `functions.json` 里指向 `xsf_wrappers.h` 的 `cephes_*_wrap` 函数，本质是「xsf 里的 Cephes 子库」经包装后暴露。

---

### 4.3 Boost.Math：策略、错误映射与 float/double 双内核

#### 4.3.1 概念说明

**Boost.Math** 是 Boost C++ 库家族里的数学组件，以**高精度、强健壮性**著称，尤其擅长概率分布的分位数（ppf/反 CDF）和不完全 Beta/Gamma 积分。special 把一批「对精度和边界条件要求高」的函数交给 Boost。

接入 Boost 有两个工程难点，`boost_special_functions.h` 就是来解决它们的：

1. **类型提升策略**：Boost 默认会把 `float` 提升成 `double` 再算，这与 special「float 输入应返回 float」的 ufunc 契约冲突。需要用 policy 关掉提升。
2. **错误映射**：Boost 检测到错误时抛 C++ 异常，而 special 需要的是 Python 告警/异常（`SpecialFunctionWarning`/`OverflowError`）。需要把 Boost 的错误策略改写成「调 Python C API」。

此外，Boost 函数普遍提供 **float + double 双内核**（如 `ibeta_float` / `ibeta_double`），这样 ufunc 才能同时挂 `f` 和 `d` 两个 loop。

#### 4.3.2 核心流程

Boost 内核从 C++ 到 Python 错误信号的链路：

```text
special.betainc(a,b,x)  [Python]
   ▼ ufunc 内层循环调
ibeta_double(a,b,x)  [boost_special_functions.h]
   ▼
ibeta_wrap<Real>(a,b,x)  [模板: 边界检查 + try/catch]
   ▼ 调
boost::math::ibeta(a,b,x, SpecialPolicy())  [Boost 真正实现]
   │  若 Boost 内部检测到上溢/下溢/求值困难
   ▼ 触发 user_*_error 策略
user_overflow_error / user_evaluation_error
   ▼ PyGILState_Ensure + PyErr_SetString/PyErr_WarnEx
Python 层收到 OverflowError 或 RuntimeWarning
```

`SpecialPolicy` 关掉了类型提升（`promote_float<false>`、`promote_double<false>`），保证 float 留 float、double 留 double。

#### 4.3.3 源码精读

先看策略定义：

- [boost_special_functions.h:18-22](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/boost_special_functions.h#L18-L22) —— `SpecialPolicy` typedef：`promote_float<false>` + `promote_double<false>` 关掉类型提升；`max_root_iterations<400>` 给反函数求根上限；`discrete_quantile<real>` 控制离散分位数行为。

再看错误桥接的两个 `user_*` 模板：

- [boost_special_functions.h:36-50](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/boost_special_functions.h#L36-L50) —— `user_evaluation_error`：求值困难时，**获取 GIL**（`PyGILState_Ensure`）后发 `PyErr_WarnEx(RuntimeWarning, ...)`，并返回当前最优猜测值（不中断）。
- [boost_special_functions.h:53-69](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/boost_special_functions.h#L53-L69) —— `user_overflow_error`：上溢时获取 GIL 后 `PyErr_SetString(PyExc_OverflowError, ...)`，把 Boost 的上溢变成 Python 的 `OverflowError`（中断）。注意「获取 GIL」是必须的——因为 ufunc 内层循环可能在 `nogil` 状态下运行。

接着看 `betainc` 的实际包装——它是「为何选 Boost」的最佳样本：

- [boost_special_functions.h:71-133](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/boost_special_functions.h#L71-L133) —— `ibeta_wrap<Real>` 模板：先做 NaN/定义域检查，再**手工处理一系列极限情形**（`(a,b)->(0,0)`、`a->0`、`b->inf` 等，见注释 86-115 行），最后 `try` 块里调 `boost::math::ibeta(a, b, x, SpecialPolicy())`，并用 `catch` 把 `domain_error`/`overflow_error`/`underflow_error` 分别翻译成 `sf_error` 的 DOMAIN/OVERFLOW/UNDERFLOW。这种精细的边界处理与统一的错误翻译，正是 Cephes 老实现难以提供的。
- [boost_special_functions.h:135-145](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/boost_special_functions.h#L135-L145) —— `ibeta_float` / `ibeta_double` 两个具现化函数，分别对应 ufunc 的 `fff->f` 和 `ddd->d` 两个 loop。

最后在 `functions.json` 里确认双内核声明：

- [functions.json:67-72](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/functions.json#L67-L72) —— `betainc` 挂 `boost_special_functions.h++`，含 `ibeta_float`（`fff->f`）与 `ibeta_double`（`ddd->d`）。

构建侧：

- [meson.build:127-144](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/meson.build#L127-L144) —— `_ufuncs_cxx` 的 `cpp_args` 含 `-DBOOST_MATH_STANDALONE=1`（用独立版 Boost.Math），`dependencies` 含 `boost_math_dep`、`xsf_dep`、`ellint_dep`。

#### 4.3.4 代码实践

**实践目标**：理解「为何 betainc 选 Boost 而非 Cephes」。

**操作步骤**：

1. 阅读 [boost_special_functions.h:71-133](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/boost_special_functions.h#L71-L133) 的 `ibeta_wrap`，数一数它手工处理了多少种极限/边界情形。
2. 在 Python 端验证 betainc 在边界上的健壮行为：

```python
import scipy.special as sc
# 极限情形：a=0 时 Beta 分布退化为 x=0 处的点分布
print(sc.betainc(0, 3, 0.5))   # 走 Boost 的边界分支，应稳定返回而非崩溃
print(sc.betainc(2, 3, 1.0))   # 正常情形
print(sc.betainc(2, 3, -0.1))  # 定义域外，应返回 NaN（DOMAIN error 静默）
```

**需要观察的现象**：

- `betainc(0, 3, 0.5)` 不报错、不崩溃，返回一个稳定值——这正是 Boost 包装里 86-115 行边界分支的功劳。
- `betainc(2, 3, -0.1)` 返回 `nan`——对应 `ibeta_wrap` 里 `sf_error("betainc", SF_ERROR_DOMAIN, NULL)` 后返回 NAN（见 81-84 行）。

**预期结果**：你能用一句话解释「betainc 选 Boost 的原因」——Boost 的 `ibeta` 精度高、边界情形处理完整（代码里有专门的极限分支），且能通过 `user_*_error` 策略干净地把错误映射成 Python 信号；相比之下 Cephes 的老实现边界覆盖与错误报告都更粗糙。

> 关于具体数值输出，标注「待本地验证」——不同 SciPy 版本对个别极限分支的约定可能微调。

#### 4.3.5 小练习与答案

**练习 1**：`user_overflow_error` 里为什么要先 `PyGILState_Ensure()` 再 `PyErr_SetString`？

**参考答案**：ufunc 的内层循环可能在 `nogil` 块中执行（释放了 GIL）。任何 Python C API 调用（包括设置异常）都必须持有 GIL。`PyGILState_Ensure()` 重新获取 GIL，`PyGILState_Release()` 释放，保证线程安全。

**练习 2**：为什么 Boost 函数普遍提供 `*_float` 和 `*_double` 两个内核，而 xsf 函数常常只有一个 `double` 版？

**参考答案**：Boost 通过 `SpecialPolicy` 关掉了类型提升，所以必须显式提供 float 和 double 两个具现化版本，才能让 ufunc 同时挂 `f->f` 和 `d->d` 两个 loop（float 输入直接得到 float 输出，避免无谓提升）。这与 Boost「按精度分发」的设计哲学一致。xsf 则视函数而定，部分函数只提供 double。

---

### 4.4 遗留数学库：Cephes、cdflib、specfun（Zhang & Jin）

#### 4.4.1 概念说明

除了 xsf 和 Boost，special 还依赖三套**历史更久**的数学库，它们通过各自的 Cython/C++ 包装层接入：

- **Cephes**：Stephen L. Moshier 编写的经典 C 数学库（1980s 起），覆盖 Gamma、Bessel、椭圆、误差函数等。在 special 中已**整体迁入 xsf**（`xsf/cephes/`），对外通过 `xsf_wrappers.h` 的 `cephes_*_wrap` 系列暴露；另有 `_legacy.pxd` 一层「遗留截断包装」处理历史 API。
- **cdflib**：Brown & Lovato 的概率分布 CDF/分位数库，专长是「非中心分布」（如非中心 F、非中心 t）和「反解分布参数」。它被编成静态库 `cdflib_lib`，经 `_cdflib_wrappers.pxd` 调用。
- **specfun（Zhang & Jin）**：Shanjie Zhang 与 Jianming Jin 的《Computation of Special Functions》配套库，擅长 Mathieu 函数、Kelvin 函数、各种特殊函数的零点与序列。它已迁入 xsf 命名空间为 `xsf::specfun`，由 `_specfun.pyx` 调用（用于序列型/零点函数）。

这三套库的共同点是「**老但准**」：经过数十年验证，数值可靠；但接口风格各异，所以每套都需要自己的包装层来适配 ufunc 体系。

#### 4.4.2 核心流程

三套遗留库各自的接入路径（互不相同）：

```text
Cephes:
  xsf/cephes/*.h  →  cephes_*_wrap (xsf_wrappers.h)  →  _ufuncs.pyx
  （历史 API 还有一层 _legacy.pxd 的 *_unsafe 截断包装）

cdflib:
  cdflib.c  →  static_library('cdflib')  →  cdff*/cdft* (cdflib.h)
           →  _cdflib_wrappers.pxd  →  _ufuncs.pyx   (link_with: cdflib_lib)

specfun (Zhang & Jin):
  xsf/specfun/specfun.h (namespace xsf::specfun)  →  _specfun.pyx  →  _specfun 扩展
```

Cephes 的 `_legacy.pxd` 之所以存在，是因为**历史兼容**：早期 SciPy 会静默地把浮点参数截断成整数（如 `bdtr(k, n, p)` 的 `n` 传成 `5.7` 会截成 `5`）。如今这种行为不被允许，但为保兼容，`_legacy.pxd` 提供带 `_unsafe` 后缀的包装：先检查、必要时发 `DeprecationWarning`，再截断调用底层。

#### 4.4.3 源码精读

**Cephes 经 `_legacy.pxd`**：

- [_legacy.pxd:2-8](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_legacy.pxd#L2-L8) —— 模块文档串说明用途：为「原本静默把 double 截成 int」的旧函数定义安全包装。
- [_legacy.pxd:15-29](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_legacy.pxd#L15-L29) —— `cdef extern from "xsf_wrappers.h"` 引入一批 `cephes_*_wrap`（Cephes 经 xsf 暴露的 C 符号）。
- [_legacy.pxd:38-43](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_legacy.pxd#L38-L43) —— `_legacy_cast_check`：若 `<int>x != x`（说明 x 本身不是整数，截断会丢精度），获取 GIL 发 `RuntimeWarning`。
- [_legacy.pxd:59-64](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_legacy.pxd#L59-L64) —— `bdtr_unsafe`：发 `DeprecationWarning`（提示非整数 `n` 已弃用），处理 NaN/Inf，最后调 `cephes_bdtr_wrap(k, <int>n, p)`。

**cdflib 经 `_cdflib_wrappers.pxd`**：

- [_cdflib_wrappers.pxd:5-25](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_cdflib_wrappers.pxd#L5-L25) —— `cdef extern from "cdflib.h"` 引入 `cdff_which4` / `cdffnc_which3/4` / `cdft_which3`，并用 `TupleDID` 结构体承接 cdflib 的「(结果, 状态, 边界)」三元组返回。
- [_cdflib_wrappers.pxd:28-61](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_cdflib_wrappers.pxd#L28-L61) —— `get_result`：把 cdflib 的整数 `status` 翻译成 `sf_error.ARG` / `OTHER` 等错误并返回 NaN 或 bound。这是「把 C 库的整数状态码翻译成 special 统一错误」的典型适配。
- [_cdflib_wrappers.pxd:64-82](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_cdflib_wrappers.pxd#L64-L82) —— `fdtridfd`：F 分布反求自由度，调 `cdff_which4(p, q, f, dfn)` 拿 `TupleDID`，再用 `get_result` 归约。

**specfun（Zhang & Jin）经 `_specfun.pyx`**：

- [_specfun.pyx:6-44](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_specfun.pyx#L6-L44) —— 一批 `cdef extern from "xsf/...h"`，把原 specfun 函数以 `xsf::` 命名空间形式引入（如 `xsf::airyzo`、`xsf::fcszo`、`xsf::klvnzo`），并在 [第 19-39 行](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_specfun.pyx#L19-L39) 引入 `xsf::specfun::` 子命名空间（Mathieu 系数、零点等）。这印证了 Zhang & Jin 的库已被收编进 xsf。

**构建侧——cdflib 静态库**：

- [meson.build:26-30](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/meson.build#L26-L30) —— `static_library('cdflib', 'cdflib.c', ...)`：把 `cdflib.c` 编成静态库 `cdflib_lib`。
- [meson.build:105](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/meson.build#L105) —— `_ufuncs` 扩展 `link_with: cdflib_lib`：所以凡调用 cdflib 的 ufunc（经 `_cdflib_wrappers.pxd`）都编进 `_ufuncs` 并链接这个静态库。

#### 4.4.4 代码实践

**实践目标**：追踪一个 cdflib 函数的完整链路，体会「整数状态码 → sf_error」的适配。

**操作步骤**：

1. 读 `_cdflib_wrappers.pxd` 的 `get_result`（[28-61 行](https://github.com/scipy/scipy/blob/c3a772bd1344d4b95beb76fd8340a0c067be92e7/scipy/special/_cdflib_wrappers.pxd#L28-L61)），记下 `status` 各取值对应的处理。
2. 在 Python 端触发一个 cdflib 路径的函数，观察边界行为：

```python
import scipy.special as sc
# stdtridf 走 cdflib (cdft_which3)：给定概率反求 t 分布自由度
print(sc.stdtridf(0.5, 0.0))   # 正常
# fdtridfd 走 cdflib (cdff_which4)：给定概率反求 F 分布 dfd
print(sc.fdtridfd(1.0, 0.5, 2.0))
```

**需要观察的现象**：这些函数返回有限数值；若传入越界参数（如 `stdtridf` 给非法概率），会因 `get_result` 里的 `sf_error(... ARG ...)` 触发 domain 错误并返回 NaN。

**预期结果**：你能画出 `Python 调用 → _ufuncs.pyx 内层循环 → _cdflib_wrappers.pxd 的 fdtridfd/stdtridf → cdflib.h 的 cdft_which4/cdff_which4 → get_result 翻译 status → sf_error` 这条链。

> 具体返回值标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：Cephes 既已迁入 xsf，为什么还需要 `_legacy.pxd` 这一层？

**参考答案**：`_legacy.pxd` 不是为了「调用 Cephes」，而是为了「**兼容历史 API 语义**」——早期 SciPy 允许把浮点参数静默截断成整数，新版本要废弃这一行为但仍需平滑过渡。`_legacy.pxd` 的 `*_unsafe` 包装负责发弃用告警、做截断检查，再调底层 Cephes 内核。这是 API 兼容层，不是数学实现层。

**练习 2**：cdflib 为什么被编成 `static_library` 再 `link_with`，而不像 xsf 那样做成 `dependency`？

**参考答案**：cdflib 是单一 `.c` 文件编译的传统 C 库，编成静态库 `cdflib_lib` 后可被 `_ufuncs` 等扩展模块链接复用，是 C 构建的自然做法。xsf 是 header-heavy 的 C++ 模板库（大量 `.h`），更适合以 `declare_dependency`（头文件包含路径）形式作为 `xsf_dep` 依赖引入。两者形态不同，接入方式也不同。

---

## 5. 综合实践

**任务**：为 `scipy.special` 画一张完整的「函数 → 后端」溯源图，并写一份后端分布统计报告。

**步骤**：

1. **统计后端分布**：运行 4.1.4 的脚本，得到 `functions.json` 中各头文件的引用次数，制成表格。
2. **抽样溯源**：从下表任选 5 个函数，逐个追踪其完整链路（Python 名 → `functions.json` 头文件 → 包装层文件 → 底层库 → 所属扩展模块）：

   | 函数 | 提示（先自己查，再核对） |
   |------|--------------------------|
   | `betainc` | Boost，`ibeta_double` |
   | `erfinv` | Boost，`erfinv_double` |
   | `sici` | xsf，`xsf_sici` |
   | `bdtr` | Cephes 经 `_legacy.pxd` + `xsf_wrappers.h` |
   | `fdtridfd` | cdflib |
   | `elliprf` | Carlson C++ |
   | `eval_chebyc` | Cython（`orthogonal_eval.pxd`） |

3. **回答三个判断题**（写在报告里）：
   - `hyp1f1` 为什么同时挂 Boost 和 xsf 两个头文件？（提示：实数 vs 复数）
   - 为什么 `_ufuncs` 要 `link_with: cdflib_lib` 而 `_ufuncs_cxx` 不用？
   - `cospi` 在 `functions.json` 里找不到，却能在 `special.cospi` 调用——它走的是哪条路径？（提示：`_special_ufuncs.cpp`）

4. **延伸观察**：浏览 `xsf_wrappers.cpp` 的 `#include` 区，统计 `xsf/*.h`（原生）与 `xsf/cephes/*.h`（迁入的 Cephes）各覆盖哪些函数族，体会 xsf「统一收容」的版图。

**验收标准**：你能不查资料，对着一张空表把「函数名 → 后端库 → 扩展模块」三列填出来，且能解释每条选择背后的理由（精度需求 / 边界处理 / 历史兼容 / 反解参数）。

## 6. 本讲小结

- `functions.json` 中层的「头文件」字段是**后端调度的总开关**：文件名指明后端库，后缀（`.h` / `.h++` / `.pxd`）指明语言，从而决定内核进哪个扩展模块。
- **xsf** 是 SciPy 自研的现代 C++ 库，是新函数的首选，且已把 Cephes（`xsf/cephes/`）和 specfun（`xsf::specfun`）收编为子模块；它通过 `xsf_wrappers.h`（`extern "C"`）桥接给 ufunc。
- **Boost.Math** 承担高精度/强健壮性需求（如 `betainc`、各分布 ppf），靠 `SpecialPolicy` 关掉类型提升、靠 `user_*_error` 策略把 C++ 异常桥接成 Python 告警/异常，并提供 float+double 双内核。
- **遗留三库**各走各的包装：Cephes 经 `xsf_wrappers.h` 暴露、另有 `_legacy.pxd` 处理历史截断兼容；cdflib 编成静态库 `cdflib_lib` 经 `_cdflib_wrappers.pxd` 调用；specfun 经 `_specfun.pyx` 暴露序列/零点函数。
- 同一函数可挂多个后端（如 `hyp1f1` 实数走 Boost、复数走 xsf），运行时按 dtype 分发到不同内核。
- 后端版图正处在「从生成式 `.pyx` 路径向 `_special_ufuncs.cpp` 直接 C++ 注册路径迁移」的过程中，这是 xsf 作为新一代统一库趋势的具体体现。

## 7. 下一步学习建议

- **深入 xsf 包装层**：本讲只看了 `xsf_wrappers.h` 的声明。想理解复数类型桥接（`to_complex`/`to_ccomplex`）与具体 wrapper 实现，请读 u8-l1（xsf 与 xsf_wrappers）。
- **Boost 错误映射深挖**：本讲概览了 `SpecialPolicy` 与 `user_*_error`。完整的 policy 机制与错误策略桥接在 u8-l2 详解。
- **直接 C++ 注册路径**：本讲提到的 `_special_ufuncs.cpp` / `xsf::numpy::ufunc` 这条「不经 functions.json」的新路径，在 u8-l3 系统讲解。
- **Carlson 椭圆积分与 cdflib 专项**：u8-l4 专讲 `ellint_carlson_cpp_lite` 与 cdflib 的封装细节与构建依赖。
- **错误贯通**：本讲多次出现 `sf_error`、`PyGILState_Ensure` 等。C 层错误如何贯通到 Python 告警/异常，在 U7（sf_error 的 C→Python 桥）完整拆解。
