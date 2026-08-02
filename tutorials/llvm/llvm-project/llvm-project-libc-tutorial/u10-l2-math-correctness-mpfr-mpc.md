# 数学正确性验证：MPFR/MPC 高精度对照

## 1. 本讲目标

本讲承接 [u6-l2 FPUtil：浮点位运算与架构特化](u6-l2-fputil-floating-point.md)。在那里你已经看到 `FPBits` 如何按位拆解一个 IEEE-754 浮点数；本讲回答的是另一个同等重要的问题：**我怎么知道 `sin`、`exp`、`cargf` 这些数学函数算得对？**

LLVM-libc 的数学库追求「正确性优先于跨平台一致、再优先于性能」。要兑现这个承诺，光有单元测试不够——单元测试只能覆盖少量「人为挑出来的」输入。数学函数的输入域是连续的、近乎无穷的，真正可信的验证需要一个**绝对可信的参考答案**与之逐点比对。本讲讲清 LLVM-libc 如何用两个高精度多精度（multiple-precision）库充当这个「参考答案」：

- **MPFR**（实数域）：用任意精度做实数运算，给 `sin/exp/log/hypot/fma` 等函数当裁判。
- **MPC**（复数域，Multiple Precision Complex）：在 MPFR 之上叠加复数运算，给 `csin/cexp/carg/cpow` 等复变函数当裁判。

学完本讲你应当能够：

1. 说清楚「为什么需要 MPFR/MPC 当参考答案」这一验证思路，以及它与传统单元测试的本质区别。
2. 读懂 `MPFRNumber` / `MPCNumber` 这层封装、`Operation` 枚举如何把「数学函数」抽象成「可分派的运算」，以及 `MPFRMatcher` 如何把自己伪装成一个 gtest 风格的匹配器。
3. 理解 **ULP（units in the last place）误差**这一精度度量的定义，以及 `get_precision` 的「高精度中间计算」策略如何避免「双重舍入」造成的误报。
4. 看懂一次完整的「逐输入比对」是如何被驱动的——包括那个名字容易误导人的 `check_mpfr.cpp`（它其实是构建期探针），以及真正的逐点比对发生在 `EXPECT_MPFR_MATCH` 宏与穷举测试里，并知道误差超出容差时如何被报告。

## 2. 前置知识

阅读本讲前，建议你已经具备：

- **浮点数的基本表示**：符号位、指数位（带偏移 bias）、尾数位，以及次正规数（subnormal/denormal）。这些是 [u6-l2](u6-l2-fputil-floating-point.md) 的核心内容，本讲直接复用 `FPBits` 概念。
- **C++ 模板与 SFINAE**：`enable_if_t`、模板特化。封装层大量用它来区分 `float`/`double`/`long double`/`float16`/`bfloat16` 等输入类型。
- **LLVM-libc 的测试框架**（[u10-l1](u10-l1-unit-test-framework.md)）：`TEST`/`TEST_F`、`EXPECT_*`/`ASSERT_*` 宏，以及 `EXPECT_THAT(value, matcher)` 这种「匹配器」写法。本讲的 `MPFRMatcher` 正是一个自定义 `Matcher`。
- **构建模式**（[u1-l4](u1-l4-build-modes-overlay-vs-full.md)）：Full 构建下 LLVM-libc 不会自动提供 MPFR，这点会影响「哪些测试能跑」。

两个本讲用到、但需要先建立直觉的外部概念：

- **MPFR**：GNU 多精度浮点库。它用「精度位数」作为一等公民——你可以让一个数有 256 位、512 位尾数，于是舍入误差可以小到忽略不计，把它当成「精确值」。
- **ULP（units in the last place）**：衡量两个浮点数「差了几个最小可表示间距」。ULP = 0 意味着两者是同一个浮点数（即被正确舍入）；ULP = 0.5 是「恰好落在两个可表示数正中间」的临界。本讲的容差几乎都以 ULP 给出。

> 一个高频误区：很多人以为「用 `double` 算一遍再和 `float` 实现比」就是高精度验证。这是错的——`double` 只有 52 位尾数，自身也有舍入误差，不足以当 `float` 的「真值」。MPFR 的意义就是**精度可以任意大**，真值才足够「真」。

## 3. 本讲源码地图

本讲涉及的关键文件与各自职责：

| 文件 | 职责 |
| --- | --- |
| [utils/MPFRWrapper/check_mpfr.cpp](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPFRWrapper/check_mpfr.cpp) | **构建期探针**（不是比对驱动！）：8 行程序，验证宿主能否找到并链接 MPFR。 |
| [utils/MPFRWrapper/MPCommon.h](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPFRWrapper/MPCommon.h) | `MPFRNumber` 类、`ExtraPrecision`、`get_precision`、**ULP 计算** `ulp()`。MPFR 与 MPC 共用的底座。 |
| [utils/MPFRWrapper/MPFRUtils.h](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPFRWrapper/MPFRUtils.h) | `Operation` 枚举、`MPFRMatcher`、`get_mpfr_matcher`、`EXPECT_MPFR_MATCH` 宏家族。 |
| [utils/MPFRWrapper/MPFRUtils.cpp](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPFRWrapper/MPFRUtils.cpp) | `unary_operation`（按 `Operation` 分派到 `mpfrInput.sin()` 等）、`compare_*`（实际比对）、`explain_*`（失败时打印诊断）。 |
| [utils/MPCWrapper/MPCUtils.h](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPCWrapper/MPCUtils.h) | 复数版：`Operation` 枚举、`MPCMatcher`、`EXPECT_MPC_MATCH` 宏。 |
| [utils/MPCWrapper/MPCUtils.cpp](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPCWrapper/MPCUtils.cpp) | `MPCNumber`、复数比对（实部、虚部分别算 ULP）。 |
| [cmake/modules/LLVMLibCCheckMPFR.cmake](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/cmake/modules/LLVMLibCCheckMPFR.cmake) | 用 `try_compile` 编译 `check_mpfr.cpp`，把结果写入 `LIBC_TESTS_CAN_USE_MPFR`。 |
| [test/src/math/sin_test.cpp](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/src/math/sin_test.cpp) | 真实驱动逐点比对的样本：`TrickyInputs`（手挑难点）+ `InDoubleRange`（区间采样）。 |
| [test/src/math/exhaustive/sinf_test.cpp](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/src/math/exhaustive/sinf_test.cpp) 与 [test/src/math/exhaustive/exhaustive_test.h](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/src/math/exhaustive/exhaustive_test.h) | 穷举测试：枚举某段输入域内**每一个** `float` 位模式逐点比对。 |

## 4. 核心概念与源码讲解

本讲按四个最小模块展开：**参考实现对照 → MPFR/MPC 封装 → 精度容差 → 比对驱动**。

### 4.1 参考实现对照：为什么需要 MPFR/MPC 当「参考答案」

#### 4.1.1 概念说明

数学函数的正确性验证面临一个根本困难：**我们没有「真值」**。`sin(1.0)` 到底等于多少？你用计算器算一次、用 `libm` 算一次、自己用泰勒展开算一次，三个答案都可能不一样——因为每一次都引入了舍入误差。如果拿「可能错」的答案去验证「可能错」的实现，等于盲人摸象。

「参考实现对照」的思路是：**找一个精度高到其误差可以忽略的实现，把它当作真值**。MPFR（实数）/MPC（复数）正是这样的库——它们允许你指定几百位尾数，计算结果的相对误差可以做到 \(10^{-70}\) 量级，相比之下 `double` 的机器精度约 \(2 \times 10^{-16}\)。于是验证流程变成：

1. 用 MPFR 以「远高于目标类型」的精度算出参考值；
2. 把参考值**舍入**到目标类型（如 `float`）；
3. 把它和 LLVM-libc 实现的输出做 ULP 比较；
4. ULP 误差若在容差内（典型 `0.5` ULP，即「正确舍入」）就算通过。

#### 4.1.2 核心流程

```
输入 x (float/double/...)
   │
   ├──► LLVM-libc 实现  ──► libc_result (目标类型)
   │
   └──► MPFR 高精度计算 (精度 = ExtraPrecision<T>)
                  │
                  ▼
           mpfr_result (高精度)
                  │  舍入到目标类型
                  ▼
           mpfr_result.as<T>()
                  │
                  ▼
         ULP(libc_result, mpfr_result) <= ulp_tolerance ?  PASS : FAIL
```

关键点：参考值不是「直接和 `libc_result` 比绝对值」，而是比 **ULP 距离**——因为浮点数是离散的，「差了几个最小间距」比「差了多少十进制」更贴合正确性语义。

#### 4.1.3 源码精读

ULP 的定义写在 `MPCommon.h` 的注释里，是整套机制的理论基础：

- [utils/MPFRWrapper/MPCommon.h:249-272](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPFRWrapper/MPCommon.h#L249-L272) —— 用中文说明这段注释做了什么：它给出了 ULP 差的数学定义。当两个数指数相同时，\( \text{ULP}(a,b) = |a-b| / \text{eps}(b) \)，即差值除以「b 的最后一位单位」；指数不同时则分段累加。注释第 1 条还点明「ULP = 0 即正确舍入」，这正是容差常取 `0.5` 的由来。

「指数相同」分支的实现：

- [utils/MPFRWrapper/MPCommon.h:294-301](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPFRWrapper/MPCommon.h#L294-L301) —— 这段做：先用 `mpfr_sub` 求差、`mpfr_abs` 取绝对值，再用 `mpfr_mul_2si` 乘以 \(2^{-\text{thisExponent} + \text{FRACTION\_LEN}}\)，把差值「对齐」到最后一位的单位上，得到 ULP。

而把 ULP 落到 `double` 的入口：

- [utils/MPFRWrapper/MPCommon.h:343-347](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPFRWrapper/MPCommon.h#L343-L347) —— `ulp(input)` 把上面算出的 MPFR 数 `.as<double>()` 取回成 `double`，供 `compare_*` 做 `ulp <= ulp_tolerance` 的判断。

#### 4.1.4 代码实践

1. **实践目标**：建立「ULP 是离散间距度量、而非绝对误差」的直觉。
2. **操作步骤**：打开 [utils/MPFRWrapper/MPCommon.h:249-272](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPFRWrapper/MPCommon.h#L249-L272)，逐字阅读注释。
3. **需要观察的现象**：注意第 3 条「指数差不超过 1 时，ULP = N 意味着两个数在位层面相距 N 位」；以及第 4 条「+0.0 与 -0.0 视为相等」（在 [MPCommon.h:277-278](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPFRWrapper/MPCommon.h#L277-L278) 实现）。
4. **预期结果**：你能用自己的话解释「为什么容差 0.5 ULP 等价于要求正确舍入」——因为两个相邻可表示浮点数之间，最近的那个总是 ≤ 0.5 ULP。
5. 这是源码阅读型实践，无需运行命令。

#### 4.1.5 小练习与答案

**练习 1**：为什么不直接用系统 `libm` 的 `sin` 当参考答案来验证 LLVM-libc 的 `sin`？

> **参考答案**：系统 `libm` 自身只有 `double` 精度且实现质量参差不齐，它本身就「可能错」。参考答案必须精度足够高、可信度足够大，MPFR 用几百位尾数算出的结果其误差远小于 1 ULP，才能当「真值」。用「可能错的」去验证「可能错的」无法确立正确性。

**练习 2**：ULP = 0 和「两个数十进制相等」是一回事吗？

> **参考答案**：基本是，但有细节：`+0.0` 与 `-0.0` 十进制都显示 `0`，位模式不同，但 ULP 视为相等（见注释第 4 条与代码 [MPCommon.h:277-278](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPFRWrapper/MPCommon.h#L277-L278)）。所以 ULP=0 严格说是指「位模式相同，或互为 ±0」。

---

### 4.2 MPFR/MPC 封装：MPFRNumber、Operation 枚举与 Matcher

#### 4.2.1 概念说明

MPFR/MPC 是 **C 库**，API 是裸的 `mpfr_t`、`mpfr_sin(...)`、`mpc_sin(...)` 调用，直接在测试里用既啰嗦又容易泄漏内存（每个 `mpfr_t` 都要 `mpfr_init`/`mpfr_clear` 配对）。LLVM-libc 用两层封装把它们包装成「现代 C++ 可用」的形态：

- **值对象层**：`MPFRNumber`（[MPCommon.h](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPFRWrapper/MPCommon.h)）和 `MPCNumber`（[MPCUtils.cpp](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPCWrapper/MPCUtils.cpp)）。RAII 管理内存，构造时按类型选精度，提供 `.sin()/.exp()/.ulp()` 等链式方法。
- **运算分派层**：`Operation` 枚举把「数学函数」抽象成枚举值，`unary_operation(op, ...)` 用 `switch` 把枚举映射到对应的 `MPFRNumber` 方法。
- **测试集成层**：`MPFRMatcher`/`MPCMatcher` 继承自 `testing::Matcher`，伪装成 gtest 匹配器，让测试用 `EXPECT_MPFR_MATCH` 一行写完「算 + 比 + 报错」。

复数版（MPC）结构完全平行，只是 `MPCNumber` 内部持有一个 `mpc_t`，并在比对时把结果拆成实部、虚部**分别**算 ULP。

#### 4.2.2 核心流程

```
测试代码:  EXPECT_MPFR_MATCH(Operation::Sin, x, LIBC_NAMESPACE::sin(x), 0.5)
                         │  (宏展开)
                         ▼
   get_mpfr_matcher<Operation::Sin>(x, result, 0.5, RoundingMode::Nearest)
                         │  返回 MPFRMatcher 对象
                         ▼
   EXPECT_THAT(result, matcher)  ──► matcher.match(result)
                         │
                         ▼
   compare_unary_operation_single_output(Operation::Sin, x, result, 0.5, Nearest)
                         │
                         ▼
   unary_operation(Operation::Sin, x, precision, rounding)
          ── switch ──►  case Operation::Sin: return mpfrInput.sin();
                         │
                         ▼
   mpfr_result.ulp(result) <= 0.5 ?  true : false
```

#### 4.2.3 源码精读

**`Operation` 枚举**——把几十个数学函数抽象成可分派的标签，并按「输入/输出元数」分组：

- [utils/MPFRWrapper/MPFRUtils.h:22-72](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPFRWrapper/MPFRUtils.h#L22-L72) —— 用中文说明这段做了什么：定义 `enum class Operation`，用 `BeginUnaryOperationsSingleOutput ... EndUnaryOperationsSingleOutput` 这类哨兵把 `Sin/Cos/Exp/Log...`（一元单输出）、`Frexp`（一元双输出）、`Add/Atan2/Hypot/Pow`（二元单输出）、`RemQuo`（二元双输出）、`Fma`（三元单输出）分成五组；`is_valid_operation()`（[MPFRUtils.h:318-356](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPFRWrapper/MPFRUtils.h#L318-L356)）正是靠这些区间做编译期类型校验。

**运算分派**——枚举如何落到具体 MPFR 调用：

- [utils/MPFRWrapper/MPFRUtils.cpp:104-105](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPFRWrapper/MPFRUtils.cpp#L104-L105) —— 在 `unary_operation` 的 `switch` 里，`case Operation::Sin: return mpfrInput.sin();`。其余几十个函数同理，每个 case 都是「委托给 `MPFRNumber` 的同名方法」。

**Matcher 伪装成 gtest 匹配器**——让验证逻辑融入 `EXPECT_THAT`：

- [utils/MPFRWrapper/MPFRUtils.h:229-252](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPFRWrapper/MPFRUtils.h#L229-L252) —— `MPFRMatcher` 继承 `testing::Matcher<OutputType>`，存住 `input`、`ulp_tolerance`、`rounding`；`match(libcResult)` 调用重载决议后的 `compare_*` 返回 bool；失败时框架会回调 `explainError()` 打印诊断；`is_silent()` 控制是否跳过诊断（用于「先静默统计失败数、再放大容差找最大误差」的模式）。

**工厂函数**——模板 + SFINAE 强校验：

- [utils/MPFRWrapper/MPFRUtils.h:358-366](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPFRWrapper/MPFRUtils.h#L358-L366) —— `get_mpfr_matcher<op>` 用 `cpp::enable_if_t<is_valid_operation<...>>` 做返回类型 SFINAE：如果输入/输出类型与该 `op` 的分组不匹配（比如给一元单输出的 `Sin` 传了一个 `BinaryInput`），**编译期就报错**，而不是运行期 mysteriously 失败。还带 `__attribute__((no_sanitize("address")))`，因为 ULP 计算会做位重解释。

**复数版对照**——结构平行，差异在「实虚分别」：

- [utils/MPCWrapper/MPCUtils.h:25-62](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPCWrapper/MPCUtils.h#L25-L62) —— `mpc::Operation` 把 `Csin/Ccos/Cexp/Clog/Csqrt`（复→复）、`Carg/Cabs`（复→实）、`Cpow`（复,复→复）分组。
- [utils/MPCWrapper/MPCUtils.cpp:162-196](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPCWrapper/MPCUtils.cpp#L162-L196) —— 复数比对核心：先用 `mpc_real` 取实部、`mpc_imag` 取虚部，再分别 `mpfr_real.ulp(...)` / `mpfr_imag.ulp(...)`，最后 `(ulp_real <= tol) && (ulp_imag <= tol)`——**实部和虚部必须同时达标**才算通过。

#### 4.2.4 代码实践

1. **实践目标**：看清「一个数学函数」如何从 `Operation` 枚举一路走到 MPFR 调用。
2. **操作步骤**：
   - 在 [MPFRUtils.h:64](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPFRWrapper/MPFRUtils.h#L64) 找到 `Sin` 这个枚举值；
   - 跳到 [MPFRUtils.cpp:104-105](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPFRWrapper/MPFRUtils.cpp#L104-L105) 看 `case Sin` 委托给 `mpfrInput.sin()`；
   - 在 [MPCommon.h:233](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPFRWrapper/MPCommon.h#L233) 看到 `MPFRNumber::sin()` 的声明（实现在 MPCommon.cpp，内部调 MPFR 的 `mpfr_sin`）。
3. **需要观察的现象**：三处形成一条干净链路 `Operation::Sin → unary_operation switch → MPFRNumber::sin()`，没有任何「函数名硬编码字符串」。
4. **预期结果**：你能说出「新增一个被 MPFR 支持的函数 `foo`，需要在枚举里加 `Foo`、在 switch 里加 `case Foo: return mpfrInput.foo();`、在 `MPFRNumber` 里加 `foo()` 方法」三处触点。
5. 源码阅读型实践，待本地验证（若想确认 `MPFRNumber::sin()` 确实调用 `mpfr_sin`，可阅读 MPCommon.cpp）。

#### 4.2.5 小练习与答案

**练习 1**：`get_mpfr_matcher` 为什么用 `enable_if_t<is_valid_operation<...>>` 做返回类型，而不是在函数体里 `assert`？

> **参考答案**：为了让类型不匹配在**编译期**就失败。比如误把 `BinaryInput` 传给一元运算 `Sin`，`is_valid_operation` 为假，SFINAE 剔除该重载、无匹配重载即编译报错；这比运行期 assert 更早暴露问题，也符合「数学函数的输入输出元数」是静态可知的这一事实。

**练习 2**：复数函数（如 `csinf`）的 ULP 容差判断为什么是 `(ulp_real <= tol) && (ulp_imag <= tol)` 而不是「实虚取平均」？

> **参考答案**：复数的实部和虚部是两个独立的浮点结果，各自都可能被正确或不正确舍入。「取平均」会让一个完全错误的分量被另一个正确分量掩盖；要求「两者都 ≤ 容差」才是对每个分量都正确舍入的严格刻画（见 [MPCUtils.cpp:195](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPCWrapper/MPCUtils.cpp#L195)）。

---

### 4.3 精度容差：ULP 度量与「高精度中间计算」策略

#### 4.3.1 概念说明

「用 MPFR 当参考答案」有一个微妙陷阱：**双重舍入（double rounding）**。设想你用「比 `float` 只高一点点」的精度（比如 40 位）去算 `sin(x)`，再把结果舍入回 `float`。如果真值恰好落在两个 `float` 的正中间，高精度计算可能把它算成「略偏左」，舍入时取左边的 `float`；但真正的无穷精度值其实略偏右，本应取右边——于是参考答案本身就舍入错了，用它去判 LLVM-libc 的实现就会**误报**。

防御办法是：让 MPFR 的中间精度**远高于**目标类型，使「高精度结果再舍入一次」几乎不可能改变最终舍入方向。LLVM-libc 用 `ExtraPrecision<T>` 给每种类型定一个「足够富余」的精度（如 `double` 用 256 位，是 53 位尾数的近 5 倍），并用 `get_precision()` 根据容差决定到底用高精度还是「与输入同精度」。

#### 4.3.2 核心流程

```
get_precision<T>(ulp_tolerance):
   if ulp_tolerance <= 0.5:        // 要求"正确舍入"
       return FRACTION_LEN + 1     //   用与输入同精度（避免 double-rounding 的另一面：见 4.3.5）
   else:                           // 容差较宽松
       return ExtraPrecision<T>::VALUE   //   用高精度（128/256/512 位）
```

精度选择 → `MPFRNumber(input, precision)` → `unary_operation` 以该精度计算 → `.ulp(result)` 比对。

> 注意：`ulp_tolerance <= 0.5` 时反而用**低**精度，乍看反直觉。原因见 [MPCommon.h:72-74](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPFRWrapper/MPCommon.h#L72-L74) 的注释：当容差就是「正确舍入」（0.5 ULP）时，我们要检验的恰恰是「按当前舍入模式、用输入同精度算一次是否得到正确舍入结果」，因此要用**同精度**复现该语义；`Fma` 更极端——[MPFRUtils.cpp:186-194](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPFRWrapper/MPFRUtils.cpp#L186-L194) 的注释明确说 FMA 必须用同精度，否则会因 double-rounding 误判。

#### 4.3.3 源码精读

**`ExtraPrecision` 表**——给每种浮点类型定「富余精度」：

- [utils/MPFRWrapper/MPCommon.h:38-70](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPFRWrapper/MPCommon.h#L38-L70) —— `float`/`float16` 用 128 位、`double` 用 256 位、`long double`（若为 80-bit 扩展）用 256、若为 128-bit 四精度则用 512、`bfloat16` 用 64。位数都远大于类型本身的尾数长度，确保参考值足够「真」。

**精度选择逻辑**：

- [utils/MPFRWrapper/MPCommon.h:75-82](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPFRWrapper/MPCommon.h#L75-L82) —— `get_precision<T>(ulp_tolerance)`：`<= 0.5` 返回 `FRACTION_LEN+1`（即「目标类型自身的精度」），否则返回 `ExtraPrecision<T>::VALUE`。

**实际比对**——把精度、运算、ULP 串起来：

- [utils/MPFRWrapper/MPFRUtils.cpp:524-534](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPFRWrapper/MPFRUtils.cpp#L524-L534) —— `compare_unary_operation_single_output`：① 按 `get_precision` 选精度；② `unary_operation` 算出 MPFR 结果；③ `mpfr_result.ulp(libc_result)` 算 ULP；④ `return ulp <= ulp_tolerance`。整个判据就这一行。

#### 4.3.4 代码实践

1. **实践目标**：理解「容差数值」与「精度策略」的对应关系。
2. **操作步骤**：对照 [MPCommon.h:75-82](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPFRWrapper/MPCommon.h#L75-L82)，分别跟踪两个真实用例：
   - [sin_test.cpp:55-56](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/src/math/sin_test.cpp#L55-L56) 的 `TOLERANCE + 0.5`，其中 `TOLERANCE` 在 [sin_test.cpp:16-20](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/src/math/sin_test.cpp#L16-L20) 定义为 0 或 1；
   - 穷举测试 [exhaustive_test.h:60-61](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/src/math/exhaustive/exhaustive_test.h#L60-L61) 用的 `Tolerance + 0.5`。
3. **需要观察的现象**：当容差 `<= 0.5` 时走「同精度」分支（检验正确舍入）；当容差 `> 0.5`（如 `1.5`）时走高精度分支。
4. **预期结果**：你能解释为什么 `sin` 的 `TrickyInputs` 用 `0.5`（要求正确舍入），而某些更难实现准确的函数会放宽到 `1.5` 或更大。
5. 待本地验证（如需观察实际 ULP 值，可参考 4.4 的诊断输出）。

#### 4.3.5 小练习与答案

**练习 1**：为什么容差 `<= 0.5` 时反而用「与输入同精度」而不是更高的精度？

> **参考答案**：当容差就是「正确舍入」时，验证目标变为「在**指定舍入模式**下，用输入同精度计算能否得到正确舍入结果」。用更高精度反而会引入 double-rounding：高精度结果再舍入一次，可能与「同精度一次舍入」得到不同结果，从而对正确实现产生误报（注释见 [MPCommon.h:72-74](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPFRWrapper/MPCommon.h#L72-L74)，FMA 的同类说明见 [MPFRUtils.cpp:186-194](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPFRWrapper/MPFRUtils.cpp#L186-L194)）。

**练习 2**：`ExtraPrecision<double>` 为什么是 256 而不是「53 的两倍 = 106」？

> **参考答案**：要可靠避免 double-rounding，参考精度需要远超目标精度，使中间结果与真值的差远小于「半个目标 ULP」。256 位是 53 位的约 5 倍，留出了充分余量；若只用 106 位，在某些边界输入上仍可能因中间舍入翻转最终方向。

---

### 4.4 比对驱动：探针、宏与逐点循环

#### 4.4.1 概念说明

这是本讲最需要澄清直觉的一节，因为它涉及一个**名字极具误导性**的文件。

任务规格里写道「阅读 `check_mpfr.cpp`，说明它如何对某个数学函数（如 sin）在一段输入区间内逐点与 MPFR 结果比对」。**这个前提是错的**——`check_mpfr.cpp` 只有 8 行，根本不做任何逐点比对。它的真实角色是 **构建期探针（probe）**：CMake 用 `try_compile` 编译它，单纯验证「宿主环境能不能找到 `<mpfr.h>` 并链接 `-lmpfr -lgmp`」。能编过 → `LIBC_TESTS_CAN_USE_MPFR=TRUE`，MPFR 测试才会被启用；编不过 → 数学测试被跳过。

真正的「逐点比对驱动」分两层：

1. **宏层**：`EXPECT_MPFR_MATCH` / `ASSERT_MPFR_MATCH` 把「算 + 比 + 报错」封装成一行，用于手挑的难点输入（见 `sin_test.cpp` 的 `TrickyInputs`）。
2. **穷举层**：`exhaustive_test.h` 的 `test_full_range` 把某段输入域切成多线程子区间，对**每一个**位模式调用 `TEST_MPFR_MATCH_ROUNDING_SILENTLY` 比对（见 `sinf_test.cpp`）。

误差超出容差时的报告也分两种：宏层失败时 `MPFRMatcher::explainError()` 打印输入十进制/位、libc 结果、MPFR 结果、MPFR 舍入后位、**实际 ULP 误差**；穷举层则先静默统计失败数与最大 ULP，最后只对最差点调用一次 `EXPECT_MPFR_MATCH` 输出诊断。

> MPC 版完全平行：`check_mpc.cpp` 是探针、`EXPECT_MPC_MATCH` 是宏、`ASSERT_MPC_MATCH_ALL_ROUNDING` 遍历四种舍入模式。

#### 4.4.2 核心流程

```
┌─ 构建期（CMake 配置时）─────────────────────────────┐
│  try_compile(check_mpfr.cpp, -lmpfr -lgmp -latomic) │
│        成功?  ──► LIBC_TESTS_CAN_USE_MPFR           │
│        失败?  ──► 数学测试被跳过 + WARNING          │
└──────────────────────────────────────────────────────┘
                       │ (决定是否构建 libcMPFRWrapper)
                       ▼
┌─ 测试运行期 ─────────────────────────────────────────┐
│  方式 A（手挑输入）:                                  │
│     for x in TrickyInputs:                           │
│        ASSERT_MPFR_MATCH_ALL_ROUNDING(Sin, x, sin(x), 0.5) │
│              └─ 内部 for 4 种舍入模式                │
│                                                       │
│  方式 B（穷举区间）:                                  │
│     test_full_range_all_roundings(START, STOP)       │
│        └─ 多线程, 对每个 float 位模式:               │
│              TEST_MPFR_MATCH_ROUNDING_SILENTLY(...)  │
│              失败? 计入 failed, 记录最差点           │
└───────────────────────────────────────────────────────┘
```

#### 4.4.3 源码精读

**「探针」真面目**——`check_mpfr.cpp` 全部内容：

- [utils/MPFRWrapper/check_mpfr.cpp:1-8](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPFRWrapper/check_mpfr.cpp#L1-L8) —— 整个文件就 `mpfr_init`/`mpfr_clear` 一对调用。它不调用任何 `sin`、不循环、不比对——纯粹是「能否编译链接 MPFR」的可执行证明。

**探针如何被消费**——`try_compile`：

- [cmake/modules/LLVMLibCCheckMPFR.cmake:10-20](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/cmake/modules/LLVMLibCCheckMPFR.cmake#L10-L20) —— 用中文说明：当用户没显式给 `LLVM_LIBC_MPFR_INSTALL_PATH`、且不是 GPU/Full 构建时，用 `try_compile` 编译 `check_mpfr.cpp` 并链 `-lmpfr -lgmp -latomic`，结果直接写入缓存变量 `LIBC_TESTS_CAN_USE_MPFR`。
- [cmake/modules/LLVMLibCCheckMPFR.cmake:5-8](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/cmake/modules/LLVMLibCCheckMPFR.cmake#L5-L8) —— 注意 Full 构建（`LLVM_LIBC_FULL_BUILD`）和 GPU 构建被直接判为 `FALSE`：注释说「Full 模式下尚不能用自有设施构建 MPFR」，所以 Full 构建下基于 MPFR 的数学测试默认不跑。

**变量如何控制库的构建**：

- [utils/MPFRWrapper/CMakeLists.txt:31-35](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPFRWrapper/CMakeLists.txt#L31-L35) —— `if(LIBC_TESTS_CAN_USE_MPFR)` 才构建 `libcMPFRWrapper` 静态库；否则（且非 GPU、非 Full）打印 `WARNING "Math tests using MPFR will be skipped."`。

**真正的比对驱动——宏层**：

- [utils/MPFRWrapper/MPFRUtils.h:539-563](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPFRWrapper/MPFRUtils.h#L539-L563) —— `ASSERT_MPFR_MATCH_ALL_ROUNDING` 用四个 `ForceRoundingMode` 局部对象依次把硬件舍入模式设为 Nearest/Upward/Downward/TowardZero，每种模式下都调用一次 `ASSERT_MPFR_MATCH`；若硬件不支持某模式（`__r.success` 为假）则跳过。
- [utils/MPFRWrapper/MPFRUtils.h:416-419](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPFRWrapper/MPFRUtils.h#L416-L419) —— `EXPECT_MPFR_MATCH` 用 `GET_MPFR_MACRO` 计数宏参数，在「带舍入模式」和「默认最近舍入」两个版本间分派（variadic macro 技巧）。

**失败诊断**——误差超出容差时打印什么：

- [utils/MPFRWrapper/MPFRUtils.cpp:204-230](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPFRWrapper/MPFRUtils.cpp#L204-L230) —— `explain_unary_operation_single_output_error` 用 `cpp::StringStream` 拼一条完整诊断，包含：输入的十进制与位串、libc 结果的十进制与位串、**MPFR 结果**与**MPFR 舍入到目标类型后的位串**、以及关键的 `ULP error:`——即实际 ULP 误差值。注释（[MPFRUtils.cpp:200-203](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPFRWrapper/MPFRUtils.cpp#L200-L203)）说明它特意先把整条消息攒齐再一次性输出，避免并发测试时消息交错。

**真正的逐点循环——穷举测试**：

- [test/src/math/exhaustive/exhaustive_test.h:50-69](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/src/math/exhaustive/exhaustive_test.h#L50-L69) —— `UnaryOpChecker::check`：对 `[start, stop]` 内**每一个**位模式 `bits`，还原成浮点 `x`，调用 `TEST_MPFR_MATCH_ROUNDING_SILENTLY(Op, x, Func(x), Tolerance+0.5, rounding)` 比对，失败则计数；若想看具体失败值，取消注释 [exhaustive_test.h:64-66](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/src/math/exhaustive/exhaustive_test.h#L64-L66) 的 `EXPECT_MPFR_MATCH_ROUNDING` 即可打印诊断。
- [test/src/math/exhaustive/exhaustive_test.h:145-211](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/src/math/exhaustive/exhaustive_test.h#L145-L211) —— `test_full_range` 把区间切成 `Increment` 大小的子块，用 `std::thread::hardware_concurrency()` 个线程并行 `check`，实时打印进度百分比，最后 `ASSERT_EQ(failed, 0)`。文件顶部注释（[exhaustive_test.h:24-36](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/src/math/exhaustive/exhaustive_test.h#L24-L36)）给出了「如何自定义 Checker」的使用说明。
- [test/src/math/exhaustive/sinf_test.cpp:15-33](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/src/math/exhaustive/sinf_test.cpp#L15-L33) —— 一行 `using` 把 `sinf`、`float`、`Operation::Sin` 绑成 `LlvmLibcSinfExhaustiveTest`，然后两个 `TEST_F` 分别穷举正数域 `[0, +Inf]` 与负数域 `[-Inf, 0]` 的**全部 2^32 个 `float` 位模式**。

**区间采样的折中**——`double` 无法穷举（2^64 太多），故采样：

- [test/src/math/sin_test.cpp:60-123](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/src/math/sin_test.cpp#L60-L123) —— `InDoubleRange`：在 `[0x1.0p-50, 0x1.0p200]` 内取 `COUNT=1231` 个等距采样点，对每个 `x` 先用 `TEST_MPFR_MATCH_ROUNDING_SILENTLY(..., 0.5, ...)` 静默判；失败则不断 `tol *= 2` 放大容差找到「能让它通过的最小容差」，记录最差点 `mx/mr`，循环结束后用 `EXPECT_MPFR_MATCH` 对最差点打印一次完整诊断（[sin_test.cpp:104-109](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/src/math/sin_test.cpp#L104-L109)），并报 `Max ULPs is at most: <tol>`。

**复数版的等价物**：

- [test/src/complex/cargf_test.cpp:23-35](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/src/complex/cargf_test.cpp#L23-L35) —— 对一组手挑的复数，用 `EXPECT_MPC_MATCH_ALL_ROUNDING(Carg, val, cargf(val), 0.5)` 一次断言四种舍入模式。

#### 4.4.4 代码实践

本实践对应任务规格的要求，但**纠正其前提**：`check_mpfr.cpp` 不做逐点比对，逐点比对发生在穷举测试与 `EXPECT_MPFR_MATCH` 宏里。

1. **实践目标**：分清「构建期探针」与「运行期比对驱动」，并说清误差超出容差时的报告路径。
2. **操作步骤**：
   - 阅读 [check_mpfr.cpp:1-8](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPFRWrapper/check_mpfr.cpp#L1-L8)，确认它只做 `mpfr_init/mpfr_clear`，无任何数学函数调用与循环；
   - 阅读 [LLVMLibCCheckMPFR.cmake:10-20](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/cmake/modules/LLVMLibCCheckMPFR.cmake#L10-L20)，确认它被 `try_compile` 当探针用、结果存入 `LIBC_TESTS_CAN_USE_MPFR`；
   - 要看「`sin` 在区间内逐点比对」的真实代码，请转到 [exhaustive/sinf_test.cpp:15-33](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/src/math/exhaustive/sinf_test.cpp#L15-L33) 与 [exhaustive_test.h:50-69](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/src/math/exhaustive/exhaustive_test.h#L50-L69)；
   - 要看「误差超出容差如何报告」，转到 [MPFRUtils.cpp:204-230](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPFRWrapper/MPFRUtils.cpp#L204-L230)（宏层诊断）与 [sin_test.cpp:104-109](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/src/math/sin_test.cpp#L104-L109)（采样层只报最差点）。
3. **需要观察的现象**：
   - 探针的成败**只影响测试是否启用**，不参与任何数值比对；
   - 逐点比对循环在 `UnaryOpChecker::check` 里，对每个位模式调用 `TEST_MPFR_MATCH_ROUNDING_SILENTLY`；
   - 失败报告分两级——静默统计（找最差 ULP）+ 对最差点打印完整诊断（输入位、MPFR 结果位、ULP 误差）。
4. **预期结果**：你能用一句话准确描述——「`check_mpfr.cpp` 是 CMake 探针，验证 MPFR 可用；真正的 `sin` 逐点比对由 `sinf_test.cpp` 经 `UnaryOpChecker::check` 驱动，误差超限时由 `explain_unary_operation_single_output_error` 打印输入/结果/ULP 诊断」。
5. **待本地验证**：若你已按 [u1-l3](u1-l3-build-and-run.md) 配好 Overlay 构建且系统装有 MPFR/GMP，可运行
   `ninja libc.test.src.math.exhaustive.sinf_test.__unit__`（目标名以实际生成为准）
   观察进度百分比与最终 `Test PASSED`；若 MPFR 不可用，配置阶段会出现 `Math tests using MPFR will be skipped.` 警告——这正好印证探针的作用。请勿假装已运行。

#### 4.4.5 小练习与答案

**练习 1**：假如你在某台机器上配置 LLVM-libc 后看到 `WARNING "Math tests using MPFR will be skipped."`，最可能的原因是什么？应如何让测试跑起来？

> **参考答案**：`try_compile(check_mpfr.cpp)` 失败了，即宿主缺少 MPFR 或 GMP 的头文件/库。可安装系统包（如 Debian 的 `libmpfr-dev`、`libgmp-dev`），或在配置时指定 `LLVM_LIBC_MPFR_INSTALL_PATH=/path/to/mpfr/install`（见 [LLVMLibCCheckMPFR.cmake:1-4](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/cmake/modules/LLVMLibCCheckMPFR.cmake#L1-L4)）。注意 Full 构建下即便装了 MPFR 也不会启用（[LLVMLibCCheckMPFR.cmake:5-8](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/cmake/modules/LLVMLibCCheckMPFR.cmake#L5-L8)）。

**练习 2**：`sin_test.cpp` 的 `InDoubleRange` 为什么对失败点要先 `tol *= 2` 不断放大容差，而不是直接报失败？

> **参考答案**：`double` 域太大无法穷举，只能采样；采样到的失败点未必是「实现真的错」，可能只是落在已知较难的输入上。先把容差放大到「刚好能让它通过」，能得到「这段区间上最大 ULP 误差的上界」，最后只对最差点打印一次诊断（[sin_test.cpp:104-109](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/src/math/sin_test.cpp#L104-L109)），既控制了输出噪声，又给出了可量化的精度画像。

**练习 3**：`ASSERT_MPFR_MATCH_ALL_ROUNDING` 与 `EXPECT_MPFR_MATCH` 在「失败是否终止当前测试」上有什么区别？

> **参考答案**：`ASSERT_*` 失败会立即从当前测试函数返回（[u10-l1](u10-l1-unit-test-framework.md) 讲过 `ASSERT_*` 与 `EXPECT_*` 的差别），适合「这个点错了后面没意义」的场景；`EXPECT_*` 失败仅记录、继续执行。此外 `_ALL_ROUNDING` 变体还会遍历四种硬件舍入模式（[MPFRUtils.h:539-563](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPFRWrapper/MPFRUtils.h#L539-L563)）。

---

## 5. 综合实践

把四个最小模块串起来：**模拟「给一个新数学函数加上 MPFR 对照验证」的完整心智流程**。

> 说明：本实践以源码阅读与设计为主，不要求你真的提交一个新函数；若要真实贡献，请结合 [u11-l3](u11-l3-contribute-new-function.md) 的端到端流程。

**任务**：假设 LLVM-libc 刚实现了 `cbrtf`（立方根，`float` 版），你要为它设计 MPFR 对照验证。请完成以下设计（不写代码，只写「该动哪些文件、为什么」）：

1. **探针层**：不需要改 `check_mpfr.cpp`——它是通用 MPFR 可用性探针，与具体函数无关。确认它在你的构建里返回 `LIBC_TESTS_CAN_USE_MPFR=TRUE` 即可。
2. **运算分派层**：对照 [MPFRUtils.h:50-51](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPFRWrapper/MPFRUtils.h#L50-L51)——`Cbrt` **早已在枚举里**；再到 [MPFRUtils.cpp:50-51](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPFRWrapper/MPFRUtils.cpp#L50-L51) 确认 `case Cbrt: return mpfrInput.cbrt();` 已存在、[MPCommon.h:197](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPFRWrapper/MPCommon.h#L197) 确认 `MPFRNumber::cbrt()` 已声明。所以你**无需改动封装层**——这正是把它做成「枚举 + switch」抽象的回报。
3. **测试层**：仿照 [sin_test.cpp:28-58](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/src/math/sin_test.cpp#L28-L58)，写一个 `TEST_F` 用 `ASSERT_MPFR_MATCH_ALL_ROUNDING(mpfr::Operation::Cbrt, x, LIBC_NAMESPACE::cbrtf(x), 0.5)` 覆盖难点输入；再仿照 [sinf_test.cpp:15-33](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/src/math/exhaustive/sinf_test.cpp#L15-L33) 写一个穷举版本（`Operation::Cbrt` + `LIBC_NAMESPACE::cbrtf` + `float`）。
4. **精度与容差**：决定容差。若实现追求正确舍入，用 `0.5`（走 [MPCommon.h:75-82](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPFRWrapper/MPCommon.h#L75-L82) 的「同精度」分支）；若某些区间暂时做不到，放宽到 `1.5` 等（走高精度分支）。
5. **失败可观测性**：确认一旦有失败点，[MPFRUtils.cpp:204-230](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPFRWrapper/MPFRUtils.cpp#L204-L230) 会自动打印输入位、MPFR 结果位与 ULP 误差，便于定位实现缺陷。

**自检问题**：如果第 2 步发现 `Cbrt` 不在枚举里，你需要依次改动哪几个文件？（答案：`MPFRUtils.h` 枚举、`MPFRUtils.cpp` 的 `switch`、`MPCommon.h`/`MPCommon.cpp` 的 `MPFRNumber::cbrt()`，以及若需穷举则再加一个 `exhaustive/` 测试。）

## 6. 本讲小结

- **参考实现对照**是数学正确性验证的核心思路：用精度高到误差可忽略的 MPFR（实数）/MPC（复数）当「真值」，把参考值舍入到目标类型后与实现输出做 **ULP** 比较，而非比绝对值。
- **封装分三层**：`MPFRNumber`/`MPCNumber`（RAII 值对象）→ `Operation` 枚举 + `unary_operation` `switch`（运算分派）→ `MPFRMatcher`/`MPCMatcher`（伪装成 gtest 匹配器）；`is_valid_operation` 用 SFINAE 在编译期校验输入输出元数。
- **精度容差**用 ULP 度量；`get_precision` 在「容差 ≤ 0.5」时用与输入同精度（检验正确舍入、避免 double-rounding 误判），否则用 `ExtraPrecision`（128/256/512 位）的高精度中间计算；复数比对要求实部、虚部**同时**达标。
- **`check_mpfr.cpp` 是构建期探针**，不是比对驱动：CMake 用 `try_compile` 编译它来设定 `LIBC_TESTS_CAN_USE_MPFR`，Full/GPU 构建下默认禁用。
- **真正的逐点比对**由宏（`EXPECT_MPFR_MATCH`/`ASSERT_MPFR_MATCH_ALL_ROUNDING`，覆盖手挑难点）与穷举测试（`exhaustive_test.h` 多线程枚举每一位模式）驱动。
- **误差超限的报告**分两级：宏层经 `explain_*_error` 打印输入/结果/MPFR 舍入位/ULP 误差的完整诊断；采样层先静默统计最大 ULP 再对最差点打印一次诊断，兼顾信息量与噪声控制。

## 7. 下一步学习建议

- **横向对照**：阅读 [utils/MPFRWrapper/MPCommon.cpp](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/utils/MPFRWrapper/MPCommon.cpp) 中 `MPFRNumber::sin()` 等方法的实现，确认它们确实只是对 `mpfr_sin` 等 C API 的薄封装，加深「封装层不引入数值逻辑」的理解。
- **向性能方向延伸**： correctness 之外还有 performance——阅读 [benchmarks/](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/benchmarks/) 与本手册的 [u10-l3 模糊测试与基准测试](u10-l3-fuzzing-and-benchmarks.md)，看数学函数如何做微基准。
- **向穷举的极限延伸**：阅读 [test/src/math/exhaustive/](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/src/math/exhaustive/) 目录下其它函数的穷举测试，体会「`float` 可穷举、`double` 只能采样」这一工程取舍。
- **端到端贡献**：当你想真正添加一个新数学函数时，回到 [u11-l3 贡献一个完整新函数](u11-l3-contribute-new-function.md)，把本讲的「探针不动、枚举加项、测试写对照」三步并入它的六步流程。
