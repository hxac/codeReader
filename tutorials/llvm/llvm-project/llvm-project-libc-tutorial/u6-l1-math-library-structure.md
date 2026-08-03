# 数学库结构与通用算法

## 1. 本讲目标

本讲是「数学库与浮点」单元的第一篇，目标是带你看清 LLVM-libc 的数学函数（`round`、`sin`、`exp`……）在源码里是**怎样被分层组织**的。

学完后你应当能够：

1. 说清数学库的**三层（外加一层）结构**：入口点壳（`src/math`）→ 通用算法（`src/math/generic`）→ 内联实现（`src/__support/math`）→ 底层浮点工具（`src/__support/FPUtil`）。
2. 看懂 `round(double)` 是如何从公开入口 `LLVM_LIBC_FUNCTION` 一路**委托**到内部 `math::round`、再到 `fputil::round` 的。
3. 理解数学函数为什么按 **float / double / long double / float16 / float128 / bfloat16** 拆成 `round`、`roundf`、`roundl` 等一整套同族入口点。
4. 解释「为什么不把算法直接写进入口点」——也就是分层带来的复用、可测、可换平台三大收益。

本讲承接 [u4-l1](u4-l1-internal-support-overview.md) 建立的 `__support` 私有库认知：数学库正是「入口点很薄、真正算法下沉到 `__support`」这一设计哲学最典型的体现。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**第一，IEEE-754 浮点数是「带结构的位」。** 一个 `double` 并不是一串无意义的 64 位，而是按符号位、指数位、尾数位切分的编码。以双精度为例（规范化数）：

\[
\text{value} = (-1)^{s} \times 1.\text{mantissa} \times 2^{e},\quad e \in [-1022,\, 1023]
\]

`round` 这类「取整」函数的本质，就是把尾数里小数点之后的位清掉、再决定是否进位。一旦你能用位运算直接操作这些字段，就能写出**不依赖任何浮点硬件指令、纯整数逻辑**的取整算法——这正是 `FPUtil` 在做的事。后续 [u6-l2](u6-l2-fputil-floating-point.md) 会专门讲 `FPBits` 如何拆解这些字段。

**第二，`round` 的数学语义是「四舍五入、半值远离零」。** 用公式表达：

\[
\mathrm{round}(x) = \mathrm{sign}(x)\left\lfloor |x| + 0.5 \right\rfloor
\]

即 `0.5 → 1`、`1.5 → 2`、`-0.5 → -1`、`-1.5 → -2`（半值向「远离零」方向靠，而不是银行家舍入）。注意实现里通常**不会真的去做 `|x| + 0.5`**——那会引入浮点加法的舍入误差，反而可能错；正确做法是直接看那一位「半位（half bit）」是否为 1。

**第三，复用同一个 `round` 语义给所有浮点类型。** `double`、`float`、`long double` 的取整逻辑在数学上完全相同，只有「位宽」不同。如果每种类型都手写一份，就会有六份几乎重复的代码。LLVM-libc 的解法是：把算法写成 **C++ 模板** `template <typename T> T round(T x)`，各类型入口点只负责「换名字 + 换类型」再委托进去。理解了这一点，你就理解了为什么需要分层。

## 3. 本讲源码地图

本讲围绕 `round` 一族函数，涉及以下文件：

| 文件 | 所属层 | 作用 |
|------|--------|------|
| `src/math/round.h` | 入口点声明头 | 声明 `double round(double x);`，是 CMake 里 `HDRS` 指向的头 |
| `src/math/generic/round.cpp` | 入口点壳（通用实现） | 用 `LLVM_LIBC_FUNCTION` 定义公开符号，一行委托给 `math::round` |
| `src/math/amdgpu/round.cpp` | 入口点壳（GPU 特化） | AMDGPU 上绕过通用算法，直接用 `__builtin_round` |
| `src/math/CMakeLists.txt` | 构建分派 | `add_math_entrypoint_object`：机器特化优先、generic 兜底 |
| `src/math/generic/CMakeLists.txt` | 通用层注册 | 把 `round.cpp` 注册成入口点对象，`DEPENDS` 指向 `__support/math/round` |
| `src/__support/math/round.h` | 内联实现 | `math::round`：在内建函数与 `fputil::round` 间二选一 |
| `src/__support/FPUtil/NearestIntegerOperations.h` | 底层浮点工具 | `fputil::round` 模板：真正的位级取整算法 |
| `docs/headers/math/index.rst` | 文档 | 数学库的精度目标、源码位置、实现状态总表 |

记忆口诀：**头（`.h`）声明 → 壳（`.cpp`）委托 → 内联（`__support/math`）选路 → 工具（`FPUtil`）算数**。

## 4. 核心概念与源码讲解

### 4.1 入口点壳：声明头与薄实现

#### 4.1.1 概念说明

「入口点壳（entrypoint shell）」是数学函数在 LLVM-libc 里最外层的那一圈代码。它要做的事**非常少**，只有两件：

1. 用 `LLVM_LIBC_FUNCTION` 宏把一个 C++ 函数包装成**对外公开的 C 符号**（这部分机制在 [u2-l2](u2-l2-implementation-standard-and-macros.md) 已讲过：靠 `asm` 别名）。
2. 把实际计算**委托**给内部命名空间里的实现。

壳里**不写算法**。所有真正的数学逻辑都下沉到更内层。这和 [u4-l1](u4-l1-internal-support-overview.md) 里讲的 `isalpha` 是同一个套路：入口点越薄，共享与复用就越容易。

每个数学入口点由「一对文件」组成：一个声明头 `src/math/<func>.h` 和一个实现壳 `src/math/<某层>/<func>.cpp`。

#### 4.1.2 核心流程

以 `round(double)` 为例，入口点这一层的流程是：

```
公开调用 round(2.7)
      │  （链接器解析到 LLVM_LIBC_FUNCTION 产生的 asm 别名符号）
      ▼
src/math/generic/round.cpp 里的 LLVM_LIBC_FUNCTION(double, round, (double x))
      │  函数体只有一句：return math::round(x);
      ▼
交给内部命名空间 LIBC_NAMESPACE_DECL::math::round （见 4.3）
```

壳的「薄」体现在：函数体不关心 `x` 是正是负、也不关心如何取整，它只负责「把 `x` 递给 `math::round`，把结果原样返回」。

#### 4.1.3 源码精读

先看声明头。`src/math/round.h` 里只有一个**普通声明**（注意：声明头里**不**用 `LLVM_LIBC_FUNCTION` 宏，宏只出现在 `.cpp` 的定义处）：

[src/math/round.h:14-18](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/math/round.h#L14-L18) —— 把 `round` 关进 `LIBC_NAMESPACE_DECL` 命名空间，只声明、不定义。

```cpp
namespace LIBC_NAMESPACE_DECL {
double round(double x);
} // namespace LIBC_NAMESPACE_DECL
```

再看实现壳。整个 `src/math/generic/round.cpp` 一共只有一句有效逻辑：

[src/math/generic/round.cpp:9-16](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/math/generic/round.cpp#L9-L16) —— `LLVM_LIBC_FUNCTION` 产生公开符号，函数体委托给 `math::round`。

```cpp
#include "src/math/round.h"
#include "src/__support/math/round.h"

namespace LIBC_NAMESPACE_DECL {
LLVM_LIBC_FUNCTION(double, round, (double x)) { return math::round(x); }
} // namespace LIBC_NAMESPACE_DECL
```

注意第 10 行的 `#include "src/__support/math/round.h"`：壳**依赖**内联实现层。这条 `#include` 与 CMake 里的 `DEPENDS` 是一一对应的（回顾 [u4-l1](u4-l1-internal-support-overview.md) 的约定：每 include 一个 `__support` 头，CMake 里就要有对应的 `DEPENDS`）。

#### 4.1.4 代码实践

**实践目标**：确认「壳只是个转发器」，并亲眼看到 `LLVM_LIBC_FUNCTION` 产出的公开符号。

**操作步骤（源码阅读型，无需构建）**：

1. 打开 `src/math/generic/round.cpp`，数一数函数体的语句数——应只有一句 `return math::round(x);`。
2. 对比 [src/math/generic/roundf.cpp:9-16](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/math/generic/roundf.cpp#L9-L16)（`float` 版），确认它同样只有一句 `return math::roundf(x);`。

**操作步骤（可选，需先完成一次 Full 构建）**：

3. 若你已按 [u1-l3](u1-l3-build-and-run.md) 构建过，对产物里的某个含 `round` 的对象文件运行：
   ```bash
   nm -C <build>/lib/libc.a 2>/dev/null | grep ' round$'
   ```
   （`-C` 把 C++ 名字还原成可读形式。）

**需要观察的现象 / 预期结果**：

- 源码侧：壳的函数体只有一行转发。
- `nm` 侧：能看到一个名为 `round` 的公开符号（T 表示已定义），证明 `LLVM_LIBC_FUNCTION` 确实把它从 `LIBC_NAMESPACE_DECL` 命名空间「导」成了 C 链接名。若未构建，此项标注**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么声明头 `src/math/round.h` 里写的是 `double round(double x);` 而不是 `LLVM_LIBC_FUNCTION(double, round, (double x));`？

**参考答案**：宏只用在「定义」处，用来生成 `asm` 别名；「声明」只是告诉同翻译单元「有这个函数」，用普通声明即可。把宏放在声明里会重复生成别名指令、反而出错。

**练习 2**：如果把壳里的 `return math::round(x);` 改成 `return x;`（不取整直接返回），公开的 `round` 还能编译通过吗？行为会怎样？

**参考答案**：能编译通过（壳不关心算法），但 `round(2.7)` 会错误地返回 `2.7` 而不是 `3.0`——这正说明算法不在壳里，壳被「架空」后行为完全取决于内层实现。

---

### 4.2 generic 算法层与「机器优先、generic 兜底」分派

#### 4.2.1 概念说明

你也许会问：既然壳在 `src/math/generic/round.cpp`，那为什么路径里有个 `generic`？因为数学库为同一个函数准备了**多份壳**，按平台分目录存放：

- `src/math/generic/<func>.cpp`：**平台无关**的通用实现，是默认兜底。
- `src/math/<arch>/<func>.cpp`：**机器特化**实现，仅在该架构目录存在时才用（如 `src/math/amdgpu/round.cpp`）。

谁来决定「这个平台用哪一份」？是 `src/math/CMakeLists.txt` 里的一个分派函数 `add_math_entrypoint_object`。它的策略只有一句话：**机器特化优先，没有就用 generic，再没有就放个空占位**。这是入口点机制（[u2-l1](u2-l1-entrypoint-mechanism.md)）在数学库里的具体化：同一个公开入口点，背后实现可随平台替换。

#### 4.2.2 核心流程

构建系统对 `add_math_entrypoint_object(round)` 的处理流程：

```
注册 round 入口点
   │
   ├─① 该架构目录下有 libc.src.math.<arch>.round 吗？（如 amdgpu）
   │      是 → round 成为指向 <arch>.round 的 ALIAS，return（机器特化胜出）
   │
   ├─② 有 libc.src.math.generic.round 吗？
   │      是 → round 成为指向 generic.round 的 ALIAS，return（通用兜底）
   │
   └─③ 都没有 → 建一个 dummy（空占位）目标，
                 因不在平台 entrypoints 名单里，最终会被 SKIP（见 [u2-l3](u2-l3-cmake-build-rules.md)）
```

关键点：**ALIAS 入口点不重复编译代码**，它只是把公开名字 `round` 指向某个已存在的具体实现目标。换平台 = 换被指向的目标，公开名字不变。

#### 4.2.3 源码精读

分派函数的完整逻辑在：

[src/math/CMakeLists.txt:6-41](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/math/CMakeLists.txt#L6-L41) —— 注释直言「We prefer machine specific implementation if available」，先查机器特化、再查 generic。

机器特化分支（第 10–19 行）：

```cmake
get_fq_target_name("${LIBC_TARGET_ARCHITECTURE}.${name}" fq_machine_specific_target_name)
if(TARGET ${fq_machine_specific_target_name})
  add_entrypoint_object(${name} ALIAS DEPENDS .${LIBC_TARGET_ARCHITECTURE}.${name})
  return()
endif()
```

generic 兜底分支（第 21–30 行）：结构相同，只是把 `${LIBC_TARGET_ARCHITECTURE}` 换成 `generic`。

而 `round` 入口点本身在第 527 行被注册：

[src/math/CMakeLists.txt:527](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/math/CMakeLists.txt#L527) —— 一行 `add_math_entrypoint_object(round)`，触发上面的分派。

「机器特化」到底长什么样？看 AMDGPU 的 `round`：

[src/math/amdgpu/round.cpp:15](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/math/amdgpu/round.cpp#L15) —— GPU 上直接调硬件内建 `__builtin_round`，**不**走 `math::round`/`fputil::round` 那条位运算链。

```cpp
LLVM_LIBC_FUNCTION(double, round, (double x)) { return __builtin_round(x); }
```

对比 4.1 的 generic 版（委托 `math::round`），你能清楚看到：**同一个公开符号 `round`，在不同平台背后是完全不同的实现路径**，而这一切对调用者透明。

#### 4.2.4 代码实践

**实践目标**：亲手验证「x86_64 / aarch64 用 generic，AMDGPU 用特化」。

**操作步骤**：

1. 列出所有非 generic 的 `round` 实现：
   ```bash
   find src/math -name 'round.cpp' -not -path '*/generic/*'
   ```
2. 确认 x86_64、aarch64 目录下**没有** `round.cpp`（它们只能走 generic 兜底）。
3. 打开 `src/math/amdgpu/round.cpp`，对比它与 `src/math/generic/round.cpp` 的差异。

**需要观察的现象 / 预期结果**：

- 步骤 1 应只列出 `src/math/amdgpu/round.cpp`（及 `roundf.cpp`），说明目前只有 GPU 提供了 `round` 的机器特化。
- 步骤 2：x86_64/aarch64 没有 `round.cpp`，故按 4.2.2 的流程②走 generic。

**预期结果**：对 `round` 而言，AMDGPU 命中流程①（机器特化），其余架构命中流程②（generic）。这正是分层带来的「按平台换实现」能力。

#### 4.2.5 小练习与答案

**练习 1**：假设某新架构 `foo` 想给 `round` 一个手写汇编实现，应该把文件放哪？还需要改 `src/math/CMakeLists.txt` 里第 527 行那一句吗？

**参考答案**：把实现放进 `src/math/foo/round.cpp` 并在 `src/math/foo/CMakeLists.txt` 里注册成入口点对象即可；**不用**改第 527 行——分派函数会自动发现 `libc.src.math.foo.round` 这个目标并优先 ALIAS 过去。

**练习 2**：为什么 dummy（空占位）目标不会污染最终 `libc.a`？

**参考答案**：因为该函数短名不在平台 `entrypoints.txt` 推导出的名单里，`add_entrypoint_object` 会对其 SKIP（只造空壳、不真正编译），回顾 [u2-l3](u2-l3-cmake-build-rules.md) 的 SKIP 机制。

---

### 4.3 内联实现：__support/math 与 FPUtil

#### 4.3.1 概念说明

壳委托给的 `math::round`，住在 `src/__support/math/round.h` 里——这正是 [u4-l1](u4-l1-internal-support-overview.md) 讲的「所有入口点共享的私有标准库」在数学领域的体现。`__support/math` 里的函数有两个特点：

1. **`LIBC_INLINE`**：声明为内联，随调用方翻译单元一起编译，消除跨函数调用开销。
2. **`LIBC_CONSTEXPR`**：可参与编译期求值（`constexpr`），因此能在单元测试或常量表达式里直接使用。

但 `math::round` 还不是「最终算法」。它本身又是一个**选择器**：在「硬件内建 `__builtin_round`」和「纯 C++ 位运算算法 `fputil::round`」之间二选一。真正干活的位级算法住在更深一层的 `src/__support/FPUtil/`（Floating-Point Utilities）。

为什么要再分一层 `math::` 与 `fputil::`？因为 `fputil::round` 是一个 **C++ 模板**，所有浮点类型共用一份；而 `math::round` 负责为 `double` 这个具体类型「点名」该用哪个实现、是否走内建。前者是「能用在任何类型上的工具」，后者是「`math.h` 公开函数的内部对应物」。

#### 4.3.2 核心流程

`math::round(x)` 的内部决策：

```
math::round(double x)
   │
   ├─ 若定义了 __LIBC_USE_BUILTIN_ROUND 且不要求 constexpr：
   │      return __builtin_round(x);        ← 用编译器/硬件内建
   │
   └─ 否则（含 constexpr 模式）：
          return fputil::round(x);          ← 走纯 C++ 位运算算法
```

注意 `#if` 条件里特别有 `&& !defined(LIBC_USE_CONSTEXPR)`：因为 `__builtin_round` **不是 `constexpr`**，一旦开启 constexpr 模式就必须改走可常量求值的 `fputil::round`。

`fputil::round` 的算法（按指数分情况，纯位运算）：

```
fputil::round(x):
  把 x 拆成 FPBits：符号 s、指数 e、尾数 m
  若 x 是 ±∞/NaN/±0            → 原样返回 x
  若 e ≥ 尾数位数               → x 本身就是整数（无小数位）→ 返回 x
  若 e == -1                    → |x| ∈ [0.5, 1) → 返回 ±1.0
  若 e ≤ -2                     → |x| < 0.5      → 返回 ±0.0
  否则：
      trim = 尾数位数 - e          （小数部分的位数）
      看 trim-1 那一位（值为 0.5 的「半位」）
      把小数部分清零得到 trunc_value
      半位=0 → 返回 trunc_value
      半位=1 → 远离零进一：返回 trunc_value ± 1.0
```

这套逻辑完全用整数移位与位掩码实现，**不调用任何浮点运算或库函数**，因此在任何架构上都给出一致结果——这也呼应了文档里「跨平台一致」的要求（见 4.4.3）。

#### 4.3.3 源码精读

内联实现 `math::round`：

[src/__support/math/round.h:15-26](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/math/round.h#L15-L26) —— `LIBC_INLINE LIBC_CONSTEXPR`，在内建与 `fputil::round` 间二选一。

```cpp
namespace LIBC_NAMESPACE_DECL {
namespace math {
LIBC_INLINE LIBC_CONSTEXPR double round(double x) {
#if defined(__LIBC_USE_BUILTIN_ROUND) && !defined(LIBC_USE_CONSTEXPR)
  return __builtin_round(x);
#else
  return fputil::round(x);
#endif
}
} // namespace math
} // namespace LIBC_NAMESPACE_DECL
```

它 `#include` 的就是底层工具（第 12 行 `#include "src/__support/FPUtil/NearestIntegerOperations.h"`）。

真正的算法 `fputil::round`（模板，对所有浮点类型 `T` 通用）：

[src/__support/FPUtil/NearestIntegerOperations.h:108-154](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/FPUtil/NearestIntegerOperations.h#L108-L154) —— 用 `FPBits<T>` 做位级取整。这里摘关键分支：

```cpp
template <typename T, ...>
LIBC_INLINE constexpr T round(T x) {
  FPBits<T> bits(x);
  if (bits.is_inf_or_nan() || bits.is_zero()) return x;   // 特殊值
  int exponent = bits.get_exponent();
  if (exponent >= static_cast<int>(FPBits<T>::FRACTION_LEN)) return x; // 已是整数
  if (exponent == -1) return FPBits<T>::one(bits.sign()).get_val();    // |x|∈[0.5,1) → ±1
  if (exponent <= -2) return FPBits<T>::zero(bits.sign()).get_val();   // |x|<0.5    → ±0
  // 一般情况：看「半位」决定是否进位（位运算，省略细节）
  ...
}
```

它住在 `namespace fputil`（[第 22 行](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/FPUtil/NearestIntegerOperations.h#L22-L22) 打开，第 412 行关闭）。同一个文件里还有 `floor`、`ceil`、`trunc`、`round_using_specific_rounding_mode` 等一族「就近取整」工具，它们都建立在 `FPBits` 之上——这是 [u6-l2](u6-l2-fputil-floating-point.md) 的主题。

#### 4.3.4 代码实践

**实践目标**：理解「内建 vs 位运算」两条路为何都存在。

**操作步骤（源码阅读型）**：

1. 在仓库里搜索 `__LIBC_USE_BUILTIN_ROUND` 的定义处（提示：通常在 `config/` 或 `__support/macros/` 相关配置里由 CMake 注入）。说明它在什么条件下会被定义。
2. 阅读 `fputil::round` 的第 120 行附近：为什么 `exponent >= FRACTION_LEN` 就能断定「x 已是整数」？（提示：结合第 2 节的 IEEE-754 公式，指数足够大时，尾数最低位也 ≥ 1。）
3. 找到第 134–135 行的 `half_bit_set`，确认它取的是「值为 0.5 的那一位」。

**需要观察的现象 / 预期结果**：

- 步骤 1：`__LIBC_USE_BUILTIN_ROUND` 是构建期由 CMake 控制的开关，并非写死。开启时 `math::round` 走内建（更快），关闭或 constexpr 模式时走 `fputil::round`（可移植、可常量求值）。
- 步骤 2：当 \(e \geq \text{FRACTION\_LEN}\) 时，\(2^e\) 已经把所有尾数位都顶到了整数位，故无小数部分。

**预期结果**：能用自己的话讲清「为什么需要两条路」——内建追求性能与硬件支持，位运算追求可移植、可 `constexpr`、跨平台一致。具体开关取值若无法在本地确认，标注**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `math::round` 标了 `LIBC_CONSTEXPR`，而入口点壳 `LLVM_LIBC_FUNCTION` 没有？

**参考答案**：`LLVM_LIBC_FUNCTION` 会生成 `asm` 别名等副作用，不能用于编译期求值；而 `math::round`（以及它委托的 `fputil::round`）是纯计算，能 `constexpr`，于是能在常量表达式和编译期单元测试里直接调用。这也是算法必须下沉到 `__support` 的原因之一。

**练习 2**：假如某平台没有可靠的 `__builtin_round`，`math::round` 还能正常工作吗？

**参考答案**：能。只要 `__LIBC_USE_BUILTIN_ROUND` 未定义（或处于 constexpr 模式），它就走 `fputil::round` 这条纯 C++ 位运算路径，不依赖任何硬件取整指令。这正是「通用兜底」的底气。

---

### 4.4 多类型/多精度拆分

#### 4.4.1 概念说明

C 标准的 `math.h` 对每个数学函数都提供一整套**按类型命名**的变体：`round`（`double`）、`roundf`（`float`）、`roundl`（`long double`），C23 还加入了 `roundf16`（`float16`）、`roundf128`（`float128`）、`roundbf16`（`bfloat16`）。它们的**数学语义完全相同**，只是参数与返回值的浮点类型不同。

LLVM-libc 用两层手段消化这种「同义重复」：

- **外层**：每种类型一个独立入口点（`round`、`roundf`、`roundl`……），各自有自己的声明头、壳、内联头，名字带类型后缀。
- **内层**：算法 `fputil::round` 是 `template <typename T>`，**一份代码**服务所有类型。

于是「六个公开名字」共享「一个模板算法」，既满足 C 标准的命名约定，又避免了六份重复实现。

#### 4.4.2 核心流程

以 `double` 与 `float` 为例，两条平行链路最终汇入同一个模板：

```
round(double x)  → math::round ─┐
                                ├─→ fputil::round<T>  （T = double / float / ...）
roundf(float x)  → math::roundf─┘
```

注意 `math::round` 与 `math::roundf` 是**两个不同的非模板函数**（各自只接受一种类型），它们内部都调用同一个模板化的 `fputil::round`。这是一种「外层按类型显式分派、内层按类型统一实现」的常见 C++ 设计。

#### 4.4.3 源码精读

`float` 版的壳与 `double` 版几乎逐字对应，只是类型和名字换了：

[src/math/generic/roundf.cpp:9-16](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/math/generic/roundf.cpp#L9-L16) —— `roundf` 委托给 `math::roundf`。

```cpp
#include "src/math/roundf.h"
#include "src/__support/math/roundf.h"
namespace LIBC_NAMESPACE_DECL {
LLVM_LIBC_FUNCTION(float, roundf, (float x)) { return math::roundf(x); }
} // namespace LIBC_NAMESPACE_DECL
```

generic 层的 CMake 把每个变体分别注册成入口点对象，并各自 `DEPENDS` 对应的 `__support/math` 内联头：

[src/math/generic/CMakeLists.txt:679-687](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/math/generic/CMakeLists.txt#L679-L687) —— `round` 入口点：`SRCS` 是 `round.cpp`，`HDRS` 指向上一层的 `../round.h`，`DEPENDS` 是 `libc.src.__support.math.round`。

```cmake
add_entrypoint_object(
  round
  SRCS round.cpp
  HDRS ../round.h
  DEPENDS libc.src.__support.math.round
)
```

紧随其后的 `roundf`、`roundl`、`roundf16`、`roundf128`、`roundbf16` 块结构完全一致，只是名字/依赖换成各自类型——你可以把它们看作同一模板的六个实例。

这套设计的价值在文档里写得明明白白。`docs/headers/math/index.rst` 把数学库的目标排成三档：**正确性（力求对所有舍入模式都正确舍入）> 跨平台一致 > 性能**：

[docs/headers/math/index.rst:26-54](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/docs/headers/math/index.rst#L26-L54) —— 「The highest priority is to be as accurate as possible…」「Our next requirement is that the outputs are consistent across all platforms.」

而 [第 19–24 行](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/docs/headers/math/index.rst#L19-L24) 则直接点明了三层源码位置：实现 `libc/src/math`、测试 `libc/test/src/math`、浮点工具 `libc/src/__support/FPUtil`——正是本讲讲的分层。在 [实现状态总表](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/docs/headers/math/index.rst#L230-L230) 中，`round` 一行六个类型列全是勾（correctly rounded），说明这套「单模板 + 多入口」对六种类型都达到了正确舍入。

#### 4.4.4 代码实践

**实践目标**：体会「六个公开名字、一个模板算法」的复用。

**操作步骤（源码阅读型）**：

1. 列出 `round` 全家族的 generic 壳：
   ```bash
   ls src/math/generic/round*.cpp
   ```
2. 打开 `src/__support/math/round.h` 与 `src/__support/math/roundf.h`，对比两者：它们各自的 `math::round` / `math::roundf` 是否都最终落到 `fputil::round`？
3. 确认 `fputil::round`（[NearestIntegerOperations.h:108](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/FPUtil/NearestIntegerOperations.h#L108-L154)）是 `template <typename T>`，因此能同时被 `double`、`float` 等实例化。

**需要观察的现象 / 预期结果**：

- 步骤 1：能看到 `round.cpp`、`roundf.cpp`、`roundl.cpp`、`roundf16.cpp`、`roundf128.cpp`、`roundbf16.cpp`（以及 `roundeven*` 一族）。
- 步骤 2：两个内联头结构对称，都经 `fputil::round` 这同一个模板干活。
- 步骤 3：算法只有一份模板定义。

**预期结果**：复用关系成立——外层六个壳各自转发，内层一个模板算法统一支撑。若想确认编译期是否真的实例化出六份代码，需本地构建后用 `nm` 检查符号，标注**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：既然 `fputil::round` 是模板，为什么不让入口点壳直接写 `return fputil::round(x);`，而要在中间塞一层 `math::round`？

**参考答案**：`math::` 这一层封装了「是否走内建 `__builtin_round`」的取舍（见 4.3），以及「这个公开函数对应哪个内部入口」的命名约定。若壳直接调 `fputil::round`，就丧失了在内建与位运算之间切换的能力，也让 `__support/math` 与 `FPUtil` 的职责边界变得模糊。

**练习 2**：`long double` 在不同平台（x86_64 的 80-bit vs aarch64 的 64-bit）位宽不同，这套模板还能用吗？

**参考答案**：能用。`FPBits<T>` 会按 `T` 的实际位宽推导出 `StorageType`、`FRACTION_LEN` 等常量（见 4.3.3 代码里的 `typename FPBits<T>::StorageType`），算法逻辑与位宽无关。这正是「通用算法 + 类型参数化」带来的跨平台能力。

---

## 5. 综合实践

把本讲四节串起来，完成下面的**端到端调用链追踪**（这也是本讲的核心实践任务）。

**任务**：画出 `round(2.7)` 在 **Linux/x86_64** 上从公开调用到最终位运算的完整委托关系，并回答「为什么不直接在入口点写算法」。

**步骤**：

1. **起点**：调用者写 `round(2.7)`，链接器解析到由 `LLVM_LIBC_FUNCTION` 生成的公开符号 `round`。
2. **壳**：进入 [src/math/generic/round.cpp:14](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/math/generic/round.cpp#L14-L14)，函数体 `return math::round(x);`。（为什么是 generic 版而非 amdgpu 版？因为 x86_64 目录下没有 `round.cpp`，按 4.2 的分派走 generic 兜底。）
3. **内联选择**：进入 [src/__support/math/round.h:18](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/math/round.h#L18-L24)。在 x86_64 默认构建下判断走 `__builtin_round` 还是 `fputil::round`（取决于 `__LIBC_USE_BUILTIN_ROUND`）。
4. **算法**：若走位运算路径，进入 [src/__support/FPUtil/NearestIntegerOperations.h:108](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/FPUtil/NearestIntegerOperations.h#L108-L154)，按指数分情况、看半位进位，对 `2.7` 返回 `3.0`。

**产出 1：委托关系图**

```
round(2.7)                         [公开 C 符号]
   │  asm 别名
   ▼
LLVM_LIBC_FUNCTION (generic/round.cpp)   ← 4.1 入口点壳
   │  return math::round(x);
   ▼
math::round (__support/math/round.h)     ← 4.3 内联实现（选择器）
   │  __builtin_round  或  fputil::round(x)
   ▼
fputil::round<T> (FPUtil/...h)           ← 4.3 底层算法（模板，4.4 多类型共用）
   │  位运算：看指数与半位
   ▼
3.0
```

**产出 2：为什么不直接在入口点写算法？** 至少给出三条理由（结合本讲源码）：

1. **多类型复用**：`fputil::round` 是模板，`round`/`roundf`/`roundl`/… 六个入口点共享同一份算法（4.4）。若算法写死在 `round.cpp` 里，就得为每种类型复制一份。
2. **可常量求值 / 可测**：`math::round` 与 `fputil::round` 是 `LIBC_INLINE constexpr`，能在编译期与单元测试里直接调用（4.3）；而入口点壳带 `asm` 别名、不能 `constexpr`。
3. **可按平台换实现**：分层后，AMDGPU 可整体换成 `__builtin_round`（4.2），x86_64 可在「内建 vs 位运算」间切换，而公开符号 `round` 与调用者代码完全不用动。

**延伸（可选）**：在 [docs/headers/math/index.rst 的实现状态表](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/docs/headers/math/index.rst#L230-L230) 里找到 `round` 行，确认它的六种类型是否都标记为正确舍入（correctly rounded），作为「单模板、多类型」达成精度目标的佐证。

## 6. 本讲小结

- 数学库是**四层结构**：入口点声明头（`src/math/<func>.h`）→ 入口点壳（`src/math/generic` 或 `src/math/<arch>` 下的 `.cpp`）→ 内联实现（`src/__support/math`）→ 底层浮点工具（`src/__support/FPUtil`）。
- 入口点壳**极薄**：只用 `LLVM_LIBC_FUNCTION` 产生公开符号，把计算一行委托给 `math::<func>`，不写算法。
- `add_math_entrypoint_object` 实行**「机器特化优先、generic 兜底、否则空占位」**的分派，使同一公开符号在不同平台背后可挂不同实现（如 AMDGPU 的 `__builtin_round`）。
- `math::round` 是个**选择器**：在内建 `__builtin_round` 与纯位运算 `fputil::round` 间二选一；后者是 `LIBC_INLINE constexpr` 模板，可移植、可常量求值。
- 数学函数按类型拆成 `round`/`roundf`/`roundl`/`roundf16`/`roundf128`/`roundbf16`，**六个公开名字共享一个模板算法**，既符合 C 标准命名，又避免重复。
- 文档把目标定为「正确性 > 跨平台一致 > 性能」，分层正是为了同时满足这三者：算法一份（一致）、可换平台（性能）、可测可常量求值（正确性）。

## 7. 下一步学习建议

- 下一篇 [u6-l2 FPUtil：浮点位运算与架构特化](u6-l2-fputil-floating-point.md) 将深入本讲只触及表层的 `FPBits`——讲清它如何用位域表示符号/指数/尾数，以及 `FPUtil/` 下按架构分目录的硬件 intrinsic 实现。
- 想看「正确性如何被验证」的读者，可先跳读 [u10-l2 数学正确性验证：MPFR/MPC 高精度对照](u10-l2-math-correctness-mpfr-mpc.md)，了解 `round` 这类函数如何逐输入与高精度参考实现比对。
- 想动手加一个新数学函数的读者，可参考文档 `src/math/docs/add_math_function.md`（在 [docs/headers/math/index.rst:60](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/docs/headers/math/index.rst#L60-L60) 有链接），它会把你本讲学到的「壳 + 内联 + 模板」三件套串成一份检查清单。
```
