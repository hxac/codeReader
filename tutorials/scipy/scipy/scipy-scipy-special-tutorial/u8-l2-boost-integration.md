# boost_special_functions.h:Boost.Math 集成与策略化错误映射

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 Boost.Math 的 **policy（策略）** 机制是什么、`SpecialPolicy` 与 `StatsPolicy` 两个 typedef 各自关闭/打开了哪些行为。
- 解释为什么 `scipy.special` 里走 Boost 的函数要 **关闭类型提升**（`promote_float<false>` / `promote_double<false>`），以及它和 ufunc 类型分发的配合关系。
- 画出 **两条** 把 Boost 内部错误「翻译」成 Python 告警/异常的路径：`SpecialPolicy` 走 `try/catch` + `sf_error`，`StatsPolicy` 走 `user_overflow_error` / `user_evaluation_error` 直接抢 GIL 调 `PyErr_*`。
- 理解 **float/double 双内核**（`ibeta_float`/`ibeta_double`、`bdtrik_float`/`bdtrik_double`）以及 `bdtrik_wrap` 被实例化两遍（`SpecialPolicy` 给 `special.bdtrik`、`StatsPolicy` 给 `stats` 的 `binom_ppf`）的「双策略」设计。

本讲是 U8「C/C++ 后端深入」的第二讲，承接 [u8-l1](u8-l1-xsf-wrappers.md)（xsf 与 `extern "C"` 薄壳），把视角从「自研的 xsf」转向「外部依赖 Boost.Math」，并和 [u7-l1](u7-l1-sf-error-c-layer.md) 的 C→Python 错误桥接打通。

## 2. 前置知识

阅读本讲前，你需要先建立这些概念（前序讲义已覆盖）：

- **ufunc 与类型分发**（[u2-l1](u2-l1-ufunc-fundamentals.md)）：special 里几乎所有函数都是 NumPy ufunc，按输入 dtype 自动选择一个「类型环」（loop），类型码 `f`=float32、`d`=float64、`F`/`D`=复数。
- **声明式代码生成**（[u3-l1](u3-l1-functions-json.md)、[u3-l2](u3-l2-generate-pyx.md)）：`functions.json` 里每条声明形如 `"<头文件>": {"<内核名>": "<类型签名>"}`；头文件名以 `++` 结尾表示这是 **C++** 头文件，会被生成到 `_ufuncs_cxx.pyx` 这条 C++ 轨道，而不是纯 C 的 `_ufuncs.pyx`。
- **C→Python 错误桥**（[u7-l1](u7-l1-sf-error-c-layer.md)）：C 内核通过 `sf_error(func_name, code, ...)` 报错，它查 TLS 动作表后，若不是 IGNORE 就抢 GIL、按名 `getattr` 拿到 `SpecialFunctionWarning`/`SpecialFunctionError` 并发出。本讲的 Boost 路径会复用这座桥。
- **一点点 C++ 模板与异常**：函数模板 `template<typename Real> ...` 可以被 `float` 和 `double` 各实例化一份；`throw std::domain_error(...)` 与 `catch (const std::domain_error&)` 是 C++ 标准异常。

下面用到但会顺带解释的术语：**policy（策略）**、**类型提升（promotion）**、**离散分位数取整（discrete_quantile）**、**GIL（全局解释器锁）**。

## 3. 本讲源码地图

| 文件 | 角色 |
| --- | --- |
| `scipy/special/boost_special_functions.h` | 本讲主角。一个 **header-only**（只有 `.h`，没有 `.cpp`）的 C++ 文件，把 Boost.Math 的特殊函数/分布包成 `extern "C"` 友好的 `*_float`/`*_double` 入口，并把 Boost 的错误翻译成 Python 信号。约 84 处 `functions.json` 声明指向它。 |
| `scipy/special/functions.json` | 声明表。声明哪些 ufunc 走 Boost、用什么类型签名（如 `betainc` → `ibeta_float`/`ibeta_double`）。 |
| `scipy/special/sf_error.h` | C 层错误桥的入口声明（[u7-l1](u7-l1-sf-error-c-layer.md)），Boost 的 `try/catch` 路径最终也落到这里的 `sf_error()`。 |
| `scipy/special/_generate_pyx.py` | 代码生成器。把 `boost_special_functions.h++` 里的内核名翻译成 `_ufuncs_cxx.pyx` 中的函数指针导出。 |
| `scipy/special/meson.build` | 构建编排。`_ufuncs_cxx` 扩展模块 `dependencies: [boost_math_dep, ...]`，把 Boost.Math 链接进来。 |

关键源码点会在「源码精读」里逐个挂永久链接。

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：

- **4.1 Boost.Math policy 机制**：讲清 `SpecialPolicy` / `StatsPolicy` 两个策略 typedef。
- **4.2 错误策略桥接**：讲清 Boost 错误如何变成 Python 告警/异常的两条路径。
- **4.3 float/double 双内核设计**：讲清 `bdtrik`/`betainc` 的类型双内核 + 策略双实例。

### 4.1 Boost.Math policy 机制

#### 4.1.1 概念说明

Boost.Math 是一个 **header-only** 的 C++ 数学库，提供大量特殊函数（`ibeta`、`erf`、`tgamma`、`hypergeometric_1F1`…）和概率分布（`binomial_distribution`、`non_central_f_distribution`…）。它的一个核心设计是 **policy（策略）**：

> policy 是一个模板参数，挂在每个函数/分布上，用来 **在不改函数签名的前提下** 调整一系列「边角行为」——要不要把 `float` 提升成 `double`、域错误时抛异常还是返回 NaN、迭代求根最多多少次、离散分布的分位数向上取整还是取实数……

换句话说，Boost.Math 把「数学公式」和「工程行为」解耦：同一个 `ibeta(a, b, x)`，配上不同 policy，可以表现出完全不同的类型与错误行为。policy 用 `boost::math::policies::policy<...>` 这一串模板参数来描述，每一项是一条「规则」。

`scipy.special` 定义了两个自己的 policy：

- **`SpecialPolicy`**：给 `special` 命名空间里的函数用（如 `betainc`、`bdtrik`）。
- **`StatsPolicy`**：给 `scipy.stats` 友好的分布函数用（如 `binom_ppf`、`ncf_cdf` 等「原始统计函数」）。

#### 4.1.2 核心流程

两个 typedef 的定义都在文件开头，先看清楚每一项规则：

```cpp
// SpecialPolicy：给 special.* 用
typedef boost::math::policies::policy<
    boost::math::policies::promote_float<false >,        // 不把 float 提升成 double
    boost::math::policies::promote_double<false >,       // 不把 double 提升成 long double
    boost::math::policies::max_root_iterations<400 >,    // 求根最多迭代 400 次
    boost::math::policies::discrete_quantile<
        boost::math::policies::real >                    // 离散分位数返回实数（不取整）
    > SpecialPolicy;

// StatsPolicy：给 stats 友好的分布用
typedef boost::math::policies::policy<
    boost::math::policies::domain_error<
        boost::math::policies::ignore_error >,           // 域错误：静默忽略
    boost::math::policies::overflow_error<
        boost::math::policies::user_error >,             // 溢出：交给 user_overflow_error
    boost::math::policies::evaluation_error<
        boost::math::policies::user_error >,             // 求值失败：交给 user_evaluation_error
    boost::math::policies::promote_float<false >,
    boost::math::policies::promote_double<false >,
    boost::math::policies::discrete_quantile<
        boost::math::policies::integer_round_up >        // 离散分位数：向上取整
    > StatsPolicy;
```

两者的关键差异可以列表对比：

| 规则 | SpecialPolicy | StatsPolicy | 为什么这么选 |
| --- | --- | --- | --- |
| `promote_float` | `false` | `false` | 都关。ufunc 的 `fff->f` 类型环要求 float32 输入产出 float32，不能被 Boost 内部偷偷提升成 double。 |
| `promote_double` | `false` | `false` | 都关。避免在 `long double` 平台上把 double 结果变成 `long double`，破坏「输入输出同 dtype」契约。 |
| 离散分位数取整 | `real`（返回实数） | `integer_round_up`（向上取整） | `special.bdtrik` 要实数分位数；而 `scipy.stats` 的离散分布 ppf 必须 **向上取整** 才能保证 `ppf(cdf(x)) == x` 的往返一致性。 |
| 域错误 | Boost 默认（抛异常） | `ignore_error`（静默） | `special.*` 希望域错误能报出来；`stats` 分布在边界外的行为更「宽容」。 |
| 溢出/求值错误 | Boost 默认（抛异常） | `user_error`（走自定义函数） | 见 4.2，`stats` 路径要把这两类错误直接译成 Python 信号。 |

#### 4.1.3 源码精读

两个 typedef 的源码（与上面流程里的代码逐字对应）：

- [boost_special_functions.h:18-22](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/boost_special_functions.h#L18-L22)：定义 `SpecialPolicy`——关闭 float/double 提升、迭代上限 400、离散分位数取实数。
- [boost_special_functions.h:25-32](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/boost_special_functions.h#L25-L32)：定义 `StatsPolicy`——域错误静默、溢出/求值错误交 `user_error`、离散分位数向上取整。注释「Round up to achieve correct ppf(cdf) round-trips for discrete distributions」点明了向上取整的目的。

文件顶部的 Boost 头文件包含说明了它用到的 Boost.Math 能力面：

- [boost_special_functions.h:8-16](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/boost_special_functions.h#L8-L16)：包含 `beta.hpp`、`erf.hpp`、`gamma.hpp`、`hypergeometric_1F1.hpp`、`hypergeometric_pFq.hpp`、`distributions.hpp`、`inverse_gaussian.hpp` 等。

#### 4.1.4 代码实践

实践目标：用 `betainc` 验证「float32 输入不被提升」。

1. 操作步骤（在装好 SciPy 的环境里）：

   ```python
   import numpy as np, scipy.special as sc
   x = np.float32(0.3)
   y = sc.betainc(np.float32(0.5), np.float32(0.5), x)
   print(y, y.dtype)        # 期望 float32
   y2 = sc.betainc(0.5, 0.5, 0.3)
   print(y2, type(y2))      # 标量 float64
   ```

2. 需要观察：第一个调用输出应是 `float32`，第二个是 `numpy.float64` 标量。
3. 预期结果：`betainc` 对 `float32` 入参命中 `ibeta_float`（`fff->f` 环），返回 float32；对 Python `float`（=double）命中 `ibeta_double`（`ddd->d`）。
4. 如果你的环境行为不同，记为「待本地验证」并检查 NumPy/SciPy 版本是否启用了某种全局 dtype 策略。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `SpecialPolicy` 必须同时设 `promote_float<false>` **和** `promote_double<false>`，只关一个不行吗？

> 参考答案：ufunc 的类型环（`fff->f` / `ddd->d`）要求「输入输出同 dtype」。若只关 `promote_float`，在 `long double` 比 `double` 宽的平台（如 x86 上的 80 位扩展精度），Boost 可能把 `double` 提升成 `long double`，导致 `ddd->d` 环产出的不是 `double`，破坏 ufunc 契约。两层都关才能保证结果 dtype 稳定。

**练习 2**：`discrete_quantile<real>` 与 `integer_round_up` 的差别，对 `bdtrik`（二项分布分位数）意味着什么？

> 参考答案：`real` 让 Boost 返回实数分位数（如 3.7），适合 `special.bdtrik` 这种「数学意义上的反函数」；`integer_round_up` 让结果向上取整到 4，适合 `scipy.stats.binom.ppf`，因为离散分布的分位数必须落在整数支撑点上，且向上取整才能保证 `ppf(cdf(k))` 往返一致。

### 4.2 错误策略桥接：把 Boost 错误译成 Python 信号

#### 4.2.1 概念说明

Boost.Math 在遇到数值错误时，行为由 policy 决定，常见的几种「动作」是：

- `throw_on_error`（Boost 默认）：抛一个 C++ 标准异常（`std::domain_error`、`std::overflow_error`、`std::underflow_error`…）。
- `ignore_error`：静默返回一个约定值（通常是 NaN）。
- `user_error`：调用 **用户自定义** 的 `boost::math::policies::user_<错误类型>_error(...)` 函数。

`scipy.special` 要把这些 C++ 世界的错误，最终变成 **Python** 世界的告警（`RuntimeWarning`/`SpecialFunctionWarning`）或异常（`OverflowError`/`SpecialFunctionError`）。难点在于：Boost 跑在 C++ 里，可能 **没有** 持有 Python 的 GIL，不能直接调 `PyErr_*`。

于是 `boost_special_functions.h` 用了 **两条** 桥接路径，对应两套 policy：

- **路径 A（间接，`SpecialPolicy` 走）**：policy 不改错误动作 → Boost 按默认 **抛异常** → 外层 `try/catch` 捕获 → 调 C 层 `sf_error(func_name, SF_ERROR_*, NULL)` → 复用 [u7-l1](u7-l1-sf-error-c-layer.md) 那座 GIL 桥发出 `SpecialFunctionWarning/Error`。
- **路径 B（直接，`StatsPolicy` 走）**：policy 把溢出/求值错误设成 `user_error` → Boost 直接调 `user_overflow_error` / `user_evaluation_error` → 这两个函数 **自己** 抢 GIL、调 `PyErr_WarnEx` / `PyErr_SetString`。

两条路径都跨过了 GIL 边界，但「谁来抢 GIL」不同：路径 A 借 `sf_error`（C 桥），路径 B 自己动手。

#### 4.2.2 核心流程

**路径 B 的两个 user 函数**（最直白的 GIL 桥）：

```cpp
template <class RealType>
RealType boost::math::policies::user_evaluation_error(
    const char* function, const char* message, const RealType& val) {
    // ...拼出错误信息 msg...
    PyGILState_STATE save = PyGILState_Ensure();   // 抢 GIL
    PyErr_WarnEx(PyExc_RuntimeWarning, msg.c_str(), 1);  // 发告警，不中断
    PyGILState_Release(save);                      // 放 GIL
    return val;                                    // 返回「最佳猜测」，继续算
}

template <class RealType>
RealType boost::math::policies::user_overflow_error(
    const char* function, const char* message, const RealType& val) {
    // ...拼出错误信息 msg...
    PyGILState_STATE save = PyGILState_Ensure();   // 抢 GIL
    PyErr_SetString(PyExc_OverflowError, msg.c_str());  // 设置异常（会中断）
    PyGILState_Release(save);                      // 放 GIL
    return 0;                                      // 返回值在异常路径下无关紧要
}
```

关键点：

- `PyGILState_Ensure()` / `Release()` 成对出现，保证无论当前线程是否持有 GIL，都能安全地调用 Python C API——这和 [u7-l1](u7-l1-sf-error-c-layer.md) 里 `sf_error_v` 的手法一模一样。
- `user_evaluation_error` 用 `PyErr_WarnEx`（**告警**），并 `return val`——「尽力而为，返回最佳猜测，但提醒用户」。
- `user_overflow_error` 用 `PyErr_SetString`（**设置异常**），下一次 Python 边界检查时会抛出 `OverflowError`。

**路径 A 的 try/catch**（以 `ibeta_wrap` 为例）：

```cpp
try {
    y = boost::math::ibeta(a, b, x, SpecialPolicy());   // 显式传 SpecialPolicy
} catch (const std::domain_error& e) {
    sf_error("betainc", SF_ERROR_DOMAIN, NULL);  y = NAN;     // 域错误→NaN
} catch (const std::overflow_error& e) {
    sf_error("betainc", SF_ERROR_OVERFLOW, NULL); y = INFINITY; // 溢出→inf
} catch (const std::underflow_error& e) {
    sf_error("betainc", SF_ERROR_UNDERFLOW, NULL); y = 0;       // 下溢→0
} catch (...) {
    sf_error("betainc", SF_ERROR_OTHER, NULL); y = NAN;         // 其他→NaN
}
```

这里 `sf_error` 的第一个参数 `"betainc"` 是 **SciPy 对外的函数名**（不是 Boost 的 `ibeta`），这样最终告警信息里显示的是用户认识的名字。`sf_error` 内部（见 [u7-l1](u7-l1-sf-error-c-layer.md)）会查 TLS 动作表：默认 `IGNORE` 就只是返回 NaN 不告警；若用户开了 `errstate(domain='raise')` 则抛 `SpecialFunctionError`。

> 注意「双保险」：路径 A 里 wrapper 自己 `try/catch` 捕获的是 **Boost 抛出的 C++ 异常**（因为 `SpecialPolicy` 没改错误动作，Boost 走默认 `throw_on_error`）；而 `sf_error` 负责把这些事件变成 **Python** 信号。这是「C++ 异常 ↔ Python 信号」的两次翻译。

#### 4.2.3 源码精读

- [boost_special_functions.h:36-50](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/boost_special_functions.h#L36-L50)：`user_evaluation_error`——拼信息、抢 GIL、`PyErr_WarnEx(RuntimeWarning)`、`return val`。注释明确「Raise a RuntimeWarning … but return the best guess」。
- [boost_special_functions.h:53-69](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/boost_special_functions.h#L53-L69)：`user_overflow_error`——抢 GIL、`PyErr_SetString(OverflowError)`、`return 0`。注释引用 Boost 文档说明溢出消息不含 `%1%`。
- [boost_special_functions.h:117-133](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/boost_special_functions.h#L117-L133)：`ibeta_wrap` 的 `try/catch`——四类异常分别映射到 `SF_ERROR_DOMAIN/OVERFLOW/UNDERFLOW/OTHER` 与对应的返回值 `NAN/INFINITY/0/NAN`，这是路径 A 的范本。
- [sf_error.h:21](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/sf_error.h#L21)：`void sf_error(const char *func_name, sf_error_t code, const char *fmt, ...);` 的声明，路径 A 的所有 `sf_error("betainc", ...)` 调用都指向它。

#### 4.2.4 代码实践

实践目标：用 `betainc` 触发域错误，对照观察路径 A 的「默认静默 → NaN」与「errstate 抬高 → 抛异常」。

1. 操作步骤：

   ```python
   import scipy.special as sc
   # (1) 默认 errstate：域错误静默，返回 NaN
   print(sc.betainc(-1.0, 0.5, 0.3))        # -1.0 让 a<0 → 域错误
   # (2) 抬高 domain 错误动作
   try:
       with sc.errstate(domain='raise'):
           sc.betainc(-1.0, 0.5, 0.3)
   except sc.SpecialFunctionError as e:
       print("caught:", e)
   ```

2. 需要观察：第 (1) 步应打印 `nan` 且 **不报警**（因为默认动作是 IGNORE）；第 (2) 步应抛出 `SpecialFunctionError`，信息里含函数名 `betainc`。
3. 预期结果：这与 `ibeta_wrap` 里 `sf_error("betainc", SF_ERROR_DOMAIN, NULL); return NAN;` 的行为一致——`sf_error` 默认只记录、由 `errstate` 决定要不要升级为告警/异常。
4. 想看 `user_evaluation_error` 的 `RuntimeWarning`（路径 B），需要走一个用 `StatsPolicy` 的分布函数，见 4.3.4。

#### 4.2.5 小练习与答案

**练习 1**：`user_evaluation_error` 返回 `val`（最佳猜测）而不是抛异常，这样设计有什么好处？

> 参考答案：分布函数（尤其 `stats` 路径）在边界、极端参数下，Boost 可能「算出一个不太准但可用的值」。直接抛异常会中断整个 ufunc 批量计算；返回最佳猜测 + 发 `RuntimeWarning`，让用户既拿到结果又被提醒，符合 NumPy「数值错误默认不中断、可配置」的哲学（见 [u2-l3](u2-l3-error-handling.md)）。

**练习 2**：路径 A 里为什么 `sf_error` 的第一个参数写 `"betainc"` 而不是 Boost 的 `"ibeta"`？

> 参考答案：`sf_error` 用这个名字去拼最终给 Python 用户看的告警/异常消息。用户调的是 `scipy.special.betainc`，不知道也不关心底层 Boost 函数叫 `ibeta`，所以要用 SciPy 对外的名字。

### 4.3 float/double 双内核设计

#### 4.3.1 概念说明

「双内核」在 `boost_special_functions.h` 里有 **两个维度**，容易混淆，先分开说清：

**维度一：类型双内核（float / double）**。几乎每个被 `functions.json` 注册的 Boost 函数，都提供两个 C 链接入口：`xxx_float` 和 `xxx_double`，对应 ufunc 的两个类型环 `fff->f`（float32）和 `ddd->d`（float64）。这是为了配合 `SpecialPolicy` 关闭类型提升：让 float32 输入 **真的** 走 float32 计算、产出 float32 结果，而不是被悄悄提升。实现上靠一个函数模板 `xxx_wrap<Real>`，再写两个薄壳分别用 `float`/`double` 实例化。

**维度二：策略双实例（SpecialPolicy / StatsPolicy）**。某些 Boost 内核（典型是二项分布的分位数）**同一份模板** 被实例化 **两遍**：一次配 `SpecialPolicy` 暴露给 `special.bdtrik`，一次配 `StatsPolicy` 暴露给 `stats` 的 `binom_ppf`。差别仅在离散分位数的取整规则（实数 vs 向上取整）。

以 `bdtrik` 为例，两个维度叠加，`bdtrik_wrap` 模板实际上被实例化了 `2(类型) × 2(策略) = 4` 份（`bdtrik_float/double` + `binom_ppf_float/double`）。

#### 4.3.2 核心流程

`bdtrik`（二项分布分位数）的双策略实例化是本模块最有代表性的范本。它的模板写成 **参数化 Policy**，再被两个不同策略实例化：

```cpp
// 模板：类型 Real + 策略 Policy 都可变
template<typename Real, typename Policy>
Real bdtrik_wrap(const Real x, const Real n, const Real p, const Policy& policy_) {
    // ...NaN/域检查...
    Real y;
    try {
        y = boost::math::quantile(
            boost::math::binomial_distribution<Real, Policy>(n, p), x);  // 把 Policy 传给分布
    } catch (const std::domain_error& e) { sf_error("bdtrik", SF_ERROR_DOMAIN, NULL);    y = NAN; }
    catch (const std::overflow_error& e) { sf_error("bdtrik", SF_ERROR_OVERFLOW, NULL);  y = INFINITY; }
    catch (const std::underflow_error& e){ sf_error("bdtrik", SF_ERROR_UNDERFLOW, NULL); y = 0; }
    catch (...) { sf_error("bdtrik", SF_ERROR_NO_RESULT, NULL); y = NAN; }
    if (y < 0 || y > n) { sf_error("bdtrik", SF_ERROR_NO_RESULT, NULL); y = NAN; }
    return y;
}

// 维度一×二：同一个 wrap，配不同类型 + 不同策略
float  bdtrik_float (float x, float n, float p)   { return bdtrik_wrap(x, n, p, SpecialPolicy()); } // → special.bdtrik
double bdtrik_double(double x, double n, double p){ return bdtrik_wrap(x, n, p, SpecialPolicy()); } // → special.bdtrik
float  binom_ppf_float (float x, float n, float p)  { return bdtrik_wrap(x, n, p, StatsPolicy()); }  // → stats binom.ppf
double binom_ppf_double(double x, double n, double p){ return bdtrik_wrap(x, n, p, StatsPolicy()); } // → stats binom.ppf
```

注释写得很直白：「Binomial distribution quantile is wrapped once for `special.bdtrik` and once for stats due to different rounding policies」。也就是说，**底层数学完全一样**（都是 `boost::math::quantile` 算二项分布分位数），只是 `SpecialPolicy` 让它返回实数、`StatsPolicy` 让它向上取整。

对应的 `functions.json` 声明（决定 ufunc 怎么挂这两个入口）：

```jsonc
"bdtrik": {
    "boost_special_functions.h++": {
        "bdtrik_double": "ddd->d",
        "bdtrik_float":  "fff->f"
    }
},
"_binom_ppf": {
    "boost_special_functions.h++": {
        "binom_ppf_double": "ddd->d",
        "binom_ppf_float":  "fff->f"
    }
}
```

注意几个细节：

- 头文件名 `boost_special_functions.h++` 末尾的 `++` 是「这是 C++」的标记（见 [u3-l1](u3-l1-functions-json.md)），生成器据此把这些内核放进 `_ufuncs_cxx.pyx` 轨道。
- 同样的内核函数（`ibeta`、二项分布分位数）被 **两个不同的 ufunc 名**（`bdtrik` 与 `_binom_ppf`）各自引用，靠的是「生成两套 float/double 入口」实现「同内核、不同策略、不同名字」。
- `betainc` 的声明完全同构：`ibeta_float`(`fff->f`) + `ibeta_double`(`ddd->d`)，但它只有 `SpecialPolicy` 一种策略（见 4.1.3 引用的 `ibeta_wrap`），所以是「纯类型双内核」。

生成器侧如何把这些 C++ 内核挂进 ufunc（[u3-l2](u3-l2-generate-pyx.md) 已讲，这里只点关键）：对 `++` 头文件，`_generate_pyx.py` 把内核函数指针 **导出** 为 `_ufuncs_cxx._export_<var>`，再用 `function_name_overrides` 让 `_ufuncs` 里的 ufunc 在构造时去 `_ufuncs_cxx` 取这个指针——从而把「C++ 重型代码」隔离在 `_ufuncs_cxx` 这一个扩展模块里（[meson.build:134-144](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L134-L144) 给它单独 `dependencies: [boost_math_dep, ...]`）。

#### 4.3.3 源码精读

- [functions.json:49-54](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/functions.json#L49-L54)：`bdtrik` 声明——`bdtrik_double`(`ddd->d`) + `bdtrik_float`(`fff->f`)，纯类型双内核，配 `SpecialPolicy`。
- [functions.json:55-60](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/functions.json#L55-L60)：`_binom_ppf` 声明——`binom_ppf_double/float`，与 `bdtrik` 同内核但配 `StatsPolicy`，给 `stats` 用。
- [functions.json:67-72](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/functions.json#L67-L72)：`betainc` 声明——`ibeta_float`(`fff->f`) + `ibeta_double`(`ddd->d`)，正则化不完全 Beta 函数 \( I_x(a,b) = B(x;a,b)/B(a,b) \)。
- [boost_special_functions.h:71-145](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/boost_special_functions.h#L71-L145)：`ibeta_wrap` 模板 + `ibeta_float`/`ibeta_double` 两个薄壳——范本式的「类型双内核」。`ibeta_wrap` 里还处理了大量 SciPy 特有的极限情形（如 `a=0,b=0`、`b=inf`），把 `betainc` 当作单变量 `x` 的函数族取点态极限，这是 SciPy 数值策略层（非 Boost 原生）的补充。
- [boost_special_functions.h:1824-1872](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/boost_special_functions.h#L1824-L1872)：`bdtrik_wrap`（带 `Policy` 模板参数）+ `bdtrik_float/double`（`SpecialPolicy`）——「类型双内核」。
- [boost_special_functions.h:1874-1884](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/boost_special_functions.h#L1874-L1884)：`binom_ppf_float/double`（`StatsPolicy`）——「策略双实例」，与上面 `bdtrik_*` 共用同一个 `bdtrik_wrap`。
- [_generate_pyx.py:855-876](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/_generate_pyx.py#L855-L876)：生成器对 `++` 头文件的处理——剥掉 `++`、把内核指针导出到 `_ufuncs_cxx`、用 `function_name_overrides` 重定向。
- [meson.build:134-144](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L134-L144)：`_ufuncs_cxx` 扩展模块链接 `boost_math_dep`，是所有 Boost 内核的编译归宿。

#### 4.3.4 代码实践

实践目标：对照 `bdtrik`（实数分位数）与 `stats.binom.ppf`（向上取整分位数），亲见「同内核、不同策略」的差异；并触发一次路径 B 的告警。

1. 操作步骤：

   ```python
   import numpy as np, scipy.special as sc, scipy.stats as st
   n, p = 10, 0.4
   q = 0.55  # 一个会让真实分位数落在两个整数之间的概率
   # (1) special.bdtrik：实数分位数（discrete_quantile<real>）
   print("bdtrik   :", sc.bdtrik(q, n, p))
   # (2) stats.binom.ppf：向上取整的分位数（integer_round_up）
   print("binom.ppf:", st.binom.ppf(q, n, p))
   ```

2. 需要观察：两者底层都是 Boost 的二项分布分位数，但 `bdtrik` 可能给出非整数（实数），而 `binom.ppf` 给出整数（向上取整）。当 `q` 落在「分位数正好介于 k 与 k+1」之间时差异最明显。
3. 预期结果：`bdtrik` 与 `binom.ppf` 在多数 `q` 上数值接近，但在取整边界上 `binom.ppf` 会向上跳一个整数，体现 `SpecialPolicy`(`real`) 与 `StatsPolicy`(`integer_round_up`) 的差异。具体取值「待本地验证」。
4. 触发路径 B 告警：找一个会让 Boost 在求值时报警的极端分布参数（例如极大自由度的非中心分布），在 `warnings.catch_warnings(record=True)` 下调用对应的 `stats` 分布函数，观察是否捕获到 `RuntimeWarning`（来自 `user_evaluation_error`）。若不易复现，记为「待本地验证」并改用 `errstate` 抬高动作来观察路径 A。

#### 4.3.5 小练习与答案

**练习 1**：`betainc` 为什么只有 `ibeta_float`/`ibeta_double` 两个入口，而 `bdtrik` 的同一内核却要 `bdtrik_*` 和 `binom_ppf_*` 四个入口？

> 参考答案：`betainc`（正则化不完全 Beta）只有一个使用场景（`special.betainc`），只需类型双内核 + 单一 `SpecialPolicy`。而二项分布分位数既被 `special.bdtrik` 用（要实数分位数），又被 `scipy.stats.binom.ppf` 用（要向上取整的整数分位数），两种用途要求不同 `discrete_quantile` 策略，于是同一 `bdtrik_wrap` 模板被 `SpecialPolicy`/`StatsPolicy` 各实例化一遍，再各配 float/double，共四个入口、对应两个 ufunc 名。

**练习 2**：如果把 `functions.json` 里 `betainc` 的 `"ibeta_float": "fff->f"` 这一行删掉，会发生什么？

> 参考答案：`betainc` 这个 ufunc 会失去 float32 类型环，只剩 `ddd->d`。于是 float32 数组输入会被 NumPy 提升成 float64 去走 double 环，返回 float64——这既违背了 `SpecialPolicy` 关闭类型提升的初衷，也让 `betainc(float32)` 的 `out` 形状/dtype 不再是 float32。所以类型双内核和 policy 关提升是 **两层配合**：policy 保证 Boost 内部不提升，双内核保证 ufunc 有对应的 float 环可走。

## 5. 综合实践

把本讲三个模块串起来：追踪一次 `special.betainc(np.float32(0.5), np.float32(0.5), np.float32(0.3))` 调用的完整链路，并对照一次域错误调用。

1. **类型分发**（4.1、4.3）：三个 `float32` 入参 → ufunc 命中 `fff->f` 环 → 取 `_export_ibeta_float` 函数指针（来自 `_ufuncs_cxx`）→ 进入 `ibeta_float` → `ibeta_wrap<float>` → `boost::math::ibeta(a, b, x, SpecialPolicy())`。因为 `SpecialPolicy` 关了提升，全程 float32。
2. **正常返回**：Boost 正常算出 \( I_{0.3}(0.5,0.5) \)，作为 float32 返回。
3. **错误路径**（4.2）：再调 `special.betainc(-1.0, 0.5, 0.3)`，`ibeta_wrap` 里 `a<0` 命中域检查 → `sf_error("betainc", SF_ERROR_DOMAIN, NULL)` → `return NAN`。默认 `errstate` 下 `sf_error` 静默（动作 IGNORE），用户只看到 `nan`；`with sc.errstate(domain='raise')` 时则抛 `SpecialFunctionError`。
4. **验证产出**：写一个小脚本，对同一组参数同时用 `betainc`（float32 与 float64 各一次）和 `mpmath` 高精度参考值（见 [u9-l2](u9-l2-mptestutils.md)）比对相对误差，确认两条类型环都数值正确且 dtype 正确。

要求：画出从 Python 调用到 `boost::math::ibeta` 的「函数指针链」，标出每一步的 dtype、policy、以及错误时走哪条桥（A 还是 B）。这等价于把本讲的 4.1–4.3 在一张图上重走一遍。

## 6. 本讲小结

- Boost.Math 用 **policy** 把「数学公式」和「工程行为」（类型提升、错误处理、迭代上限、离散分位数取整）解耦；`scipy.special` 定义了 `SpecialPolicy`（给 `special.*`，实数分位数）和 `StatsPolicy`（给 `stats`，向上取整）两套策略。
- **关闭类型提升**（`promote_float<false>`/`promote_double<false>`）是为了配合 ufunc 的 `fff->f`/`ddd->d` 类型环，保证 float32 输入产出 float32，不泄漏成 double/long double。
- Boost 错误到 Python 信号有 **两条桥**：`SpecialPolicy` 走「Boost 抛 C++ 异常 → `try/catch` → `sf_error`（C 桥，复用 [u7-l1](u7-l1-sf-error-c-layer.md)）」；`StatsPolicy` 走「`user_error` → `user_overflow_error`/`user_evaluation_error` 自己抢 GIL 调 `PyErr_*`」。前者发 `SpecialFunctionWarning/Error`，后者发 `OverflowError`/`RuntimeWarning`。
- **双内核有两个维度**：类型维度（`*_float`/`*_double`，配合类型环）和策略维度（同一模板配 `SpecialPolicy`/`StatsPolicy` 各实例化一遍，如 `bdtrik_*` vs `binom_ppf_*`）。
- 这些 Boost 内核都编译进 `_ufuncs_cxx` 扩展模块（链接 `boost_math_dep`），通过「函数指针导出 + name override」被 `_ufuncs` 里的 ufunc 引用，从而把 C++ 重型代码隔离在一个模块里（[meson.build:134-144](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/meson.build#L134-L144)）。
- header-only 的 `boost_special_functions.h` 同时承担三件事：暴露 C 链接入口、做 SciPy 特有的极限情形数值策略、把 Boost 错误翻译成 Python 信号。

## 7. 下一步学习建议

- 接着读 [u8-l3](u8-l3-special-ufuncs-registration.md)「`_special_ufuncs.cpp` / `_gufuncs.cpp`」，看新一代的 **纯 C++ 直注册** 路径如何绕开 `functions.json`，理解它与本讲的「JSON 声明 → `_ufuncs_cxx` 函数指针」旧路径的分工与迁移趋势。
- 回顾 [u7-l1](u7-l1-sf-error-c-layer.md)/[u7-l2](u7-l2-extra-code-pxi.md)，把本讲路径 A 的 `sf_error` 调用与 C 层 TLS 动作表、`seterr/errstate` 三件套完整连成一线。
- 想验证数值正确性时，参考 [u9-l2](u9-l2-mptestutils.md) 用 `mpmath` 高精度参考值比对 `betainc`、`bdtrik` 的输出。
- 进阶练习：在 `boost_special_functions.h` 里挑一个还没读过的分布函数（如 `ncf_cdf_wrap`，[boost_special_functions.h:1083-1109](https://github.com/scipy/scipy/blob/8e93e0478ca5b6e0b51652a1395f54160be2a672/scipy/special/boost_special_functions.h#L1083-L1109)），自己判断它用的是 `SpecialPolicy` 还是 `StatsPolicy`、错误走哪条桥，检验你是否真的掌握了本讲的判据。
