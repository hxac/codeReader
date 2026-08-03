# 软浮点内建函数

## 1. 本讲目标

学完本讲后，你应该能够：

1. 用整数位运算的视角描述 IEEE 754 浮点数（binary32/binary64）在内存中的编码方式。
2. 读懂 compiler-rt 中单精度加法 `__addsf3` 的「对齐阶码 → 尾数相加 → 规格化与舍入」三步实现。
3. 读懂整数与浮点互转（`__floatdidf`、`__fixdfdi`）的纯软件实现，并理解为什么这两个函数同时存在「硬件路径」与「软浮点路径」两套代码。
4. 说清楚编译器在什么场景下会插入这些软浮点内建调用。

本讲承接 [u2-l2](u2-l2-integer-builtins.md)：上一讲的「字分解」「分支无跳」「clz」手法在本讲会继续大量出现，只是操作对象从整数变成了「用整数表示的浮点数」。

## 2. 前置知识

### 2.1 什么是软浮点

CPU 上做 `1.5 + 2.5` 时，如果有浮点硬件（FPU），编译器会生成一条加法指令（如 x86 的 `addss`、ARM 的 `vadd.f32`）。但有些目标平台没有 FPU（或被关掉），例如裸机 ARM 用 `-mfloat-abi=soft`、RISC-V 不带 F/D 扩展、或显式 `-msoft-float`。此时编译器无法生成浮点加法指令，只能**插入一次函数调用**，调用一个由运行时库提供的、用纯整数运算模拟浮点运算的函数——这就是「软浮点内建函数」（soft-float builtins）。

`__addsf3` 里的命名规律延续上一讲：

- `add` = 加法
- `sf` = single float（单精度，binary32）
- `3` = 两个操作数（历史惯例，源自 libgcc）

所以 `__addsf3(a, b)` 就是「软浮点单精度加法」。同理 `__adddf3` 是双精度加法（`df` = double float）。

### 2.2 IEEE 754 极简回顾

一个 binary32（float，32 位）被切成三段：

| 段 | 位数 | 含义 |
|---|---|---|
| 符号 S | 1 位（bit 31） | 0 正 1 负 |
| 阶码 E | 8 位（bit 30–23） | 存的是「真实指数 + 偏置」，偏置 = 127 |
| 尾数 M | 23 位（bit 22–0） | 小数部分（不含隐含的最高位 1） |

规格化数的值：

\[
v = (-1)^S \times \left(1 + \frac{M}{2^{23}}\right) \times 2^{E-127}
\]

关键点：规格化数的尾数有一个**隐含的最高位 1**（implicit bit），它不占存储位但参与运算。几个特殊编码要记住：

- \(E=0, M=0\)：\(\pm 0\)。
- \(E=0, M\ne 0\)：**次正规数**（subnormal / denormal），值为 \((-1)^S \times 0.M \times 2^{-126}\)，没有隐含的 1。
- \(E=255, M=0\)：\(\pm\infty\)。
- \(E=255, M\ne 0\)：NaN（非数）。

binary64（double，64 位）结构完全平行：1 位符号 + 11 位阶码（偏置 1023）+ 52 位尾数。

### 2.3 为什么用整数操作浮点

软浮点的核心思想是：**把浮点数的 32/64 位当作普通整数，手动拆出符号、阶码、尾数，用整数加减移位重新组合，得到结果的整数位，再解释回浮点数。** 为此需要一个「把 float 的位看作 uint32_t」的安全手段——这正是 C 的 union 或 `memcpy`。

## 3. 本讲源码地图

本讲涉及的真实源码文件（注意：三个 `.c` 文件只是薄壳，真正的算法在 `.inc`/`.h` 头文件里，靠宏参数化复用）：

| 文件 | 作用 |
|---|---|
| [lib/builtins/addsf3.c](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/addsf3.c) | `__addsf3` 的对外入口，定义 `SINGLE_PRECISION` 后包含实现 |
| [lib/builtins/fp_add_impl.inc](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/fp_add_impl.inc) | 单/双/四精度加法的**真正算法**（参数化复用） |
| [lib/builtins/fp_lib.h](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/fp_lib.h) | 软浮点配置头：`rep_t`、`toRep`/`fromRep`、各常量、`normalize` |
| [lib/builtins/floatdidf.c](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/floatdidf.c) | `__floatdidf`（int64 → double），含硬/软两条路径 |
| [lib/builtins/int_to_fp_impl.inc](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/int_to_fp_impl.inc) | 整数转浮点的参数化算法 `__floatXiYf__` |
| [lib/builtins/int_to_fp.h](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/int_to_fp.h) | 为转换算法挑选源/目标类型（`SRC_I64`/`DST_DOUBLE` 等） |
| [lib/builtins/fixdfdi.c](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/fixdfdi.c) | `__fixdfdi`（double → int64），含硬/软两条路径 |
| [lib/builtins/fp_fixint_impl.inc](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/fp_fixint_impl.inc) | 浮点转整数的参数化算法 `__fixint` |
| [lib/builtins/int_types.h](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/int_types.h) | 整数类型别名（`di_int` 等）与 `float_bits`/`double_bits` union |

> 架构覆盖说明：上述 `.c` 都属于 `CMakeLists.txt` 的 `GENERIC_SOURCES`（可移植兜底实现，见 [lib/builtins/CMakeLists.txt:89-90](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/CMakeLists.txt#L89-L90) 的 `addsf3`/`adddf3`）。部分架构有汇编优化版（如 `arm/addsf3.S`、`i386/floatdidf.S`、`x86_64/floatdidf.c`），经 `filter_builtin_sources` 覆盖同名通用版。本讲只读通用实现。

## 4. 核心概念与源码讲解

### 4.1 IEEE 754 的整数表示

#### 4.1.1 概念说明

要做软浮点，第一步是**安全地把 float 的位当成 uint32_t 处理**。`fp_lib.h` 用一个 union 完成这件事，并据此推导出一组位掩码常量。理解这组常量是读懂后面所有算法的前提。

#### 4.1.2 核心流程

`fp_lib.h` 的设计是「用宏选择精度」。当 `addsf3.c` 顶部 `#define SINGLE_PRECISION` 后再包含它时，下列定义生效（[lib/builtins/fp_lib.h:30-48](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/fp_lib.h#L30-L48)）：

```c
typedef uint32_t rep_t;          // 「浮点位」的整数类型
typedef float fp_t;              // 对应的浮点类型
#define significandBits 23       // 尾数位数（不含隐含 1）
```

随后由这些「原始量」派生出全部位掩码（[lib/builtins/fp_lib.h:213-225](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/fp_lib.h#L213-L225)）。以单精度为例，代入 `significandBits=23`、`typeWidth=32`：

| 宏 | 表达式 | 单精度取值 | 含义 |
|---|---|---|---|
| `exponentBits` | `typeWidth - significandBits - 1` | 8 | 阶码位数 |
| `maxExponent` | `(1 << exponentBits) - 1` | 255 | 阶码全 1（无穷/NaN 标记） |
| `exponentBias` | `maxExponent >> 1` | 127 | 阶码偏置 |
| `implicitBit` | `1 << significandBits` | `0x00800000` | 隐含的最高位 1 |
| `significandMask` | `implicitBit - 1` | `0x007FFFFF` | 取尾数 23 位 |
| `signBit` | `1 << (significandBits+exponentBits)` | `0x80000000` | 符号位 |
| `absMask` | `signBit - 1` | `0x7FFFFFFF` | 清符号位（取绝对值位） |
| `infRep` | `exponentMask` | `0x7F800000` | 无穷的位模式 |
| `qnanRep` | `exponentMask` \| `quietBit` | `0x7FC00000` | 安静 NaN 位模式 |

位 ↔ 浮点的互转用 union（[lib/builtins/fp_lib.h:196-210](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/fp_lib.h#L196-L210)）：

```c
static __inline rep_t toRep(fp_t x)   { union { fp_t f; rep_t i; } rep = {.f = x}; return rep.i; }
static __inline fp_t  fromRep(rep_t x){ union { fp_t f; rep_t i; } rep = {.i = x}; return rep.f; }
```

> 一个有用的性质：把 IEEE 754 的位当作**无符号整数**比较，正数的数值大小与位模式大小顺序一致。这也是 `__addXf3__` 里能用 `aAbs`、`bAbs` 直接比大小来挑「绝对值较大者」的依据。

#### 4.1.3 源码精读

「浮点 ↔ 整数位」的 union，在测试头里也能看到等价实现 `toRep32`/`fromRep32`（用 `memcpy`，[test/builtins/Unit/fp_test.h:30-35](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/builtins/Unit/fp_test.h#L30-L35) 与 [test/builtins/Unit/fp_test.h:64-69](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/builtins/Unit/fp_test.h#L64-L69)）。`int_types.h` 里也有同款 union（[lib/builtins/int_types.h:137-146](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/int_types.h#L137-L146)）：

```c
typedef union { su_int u; float f; } float_bits;
typedef union { udwords u; double f; } double_bits;
```

整数类型别名（贯穿整个 builtins 库）定义在 [lib/builtins/int_types.h:25-38](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/int_types.h#L25-L38)：`si_int=int32_t`、`su_int=uint32_t`、`di_int=int64_t`、`du_int=uint64_t`。`__floatdidf` 名字里的 `di` 就是 `di_int`（64 位有符号整数）。

#### 4.1.4 代码实践

**目标**：亲手把几个浮点数拆成符号/阶码/尾数，验证 4.1.2 的常量表。

**操作步骤**（示例代码，需自行编译运行）：

```c
// softfloat_probe.c —— 示例代码（非项目原有文件）
#include <stdint.h>
#include <stdio.h>
#include <string.h>

static uint32_t toRep(float x){ uint32_t r; memcpy(&r, &x, 4); return r; }

int main(void) {
    float vals[] = {1.0f, 2.0f, 3.0f, 0.5f, -1.0f, 0.0f};
    for (int i = 0; i < 6; ++i) {
        uint32_t r = toRep(vals[i]);
        int sign = r >> 31;
        int exp  = (r >> 23) & 0xFF;
        uint32_t mant = r & 0x7FFFFF;
        printf("%6.1f -> %08X  S=%d E=%3d M=0x%06X\n", vals[i], r, sign, exp, mant);
    }
    return 0;
}
```

**需要观察的现象 / 预期结果**（待本地验证）：

- `1.0` → `3F800000`，E=127（=偏置，所以真实指数 0），M=0 —— 对应 \(1.0 \times 2^0\)。
- `2.0` → `40000000`，E=128（真实指数 +1），M=0 —— 对应 \(1.0 \times 2^1\)。
- `3.0` → `40400000`，E=128，M=0x400000 —— 对应 \(1.5 \times 2^1\)。
- `0.5` → `3F000000`，E=126（真实指数 -1）。
- `-1.0` → `BF800000`，符号位置 1。

把这些与 4.1.2 的常量对照，确认你理解了「阶码存的是真实指数 + 127」。

#### 4.1.5 小练习与答案

**练习 1**：`float` 的最大规格化正数位模式 `0x7F7FFFFF`，它的符号、阶码、尾数各是多少？为什么它不是无穷？
**答案**：S=0，E=254（≠255 所以不是无穷），M=`0x7FFFFF`（尾数全 1）。它是 \(1.111\dots_2 \times 2^{127}\)，约等于 `3.402e38`，即 `FLT_MAX`。

**练习 2**：`0x00000001` 作为 float 表示什么？属于哪一类？
**答案**：E=0、M=1，是**次正规数**，值为 \(2^{-23} \times 2^{-126} = 2^{-149}\)，即最小可表示正数。

---

### 4.2 软浮点加减实现：`__addsf3`

#### 4.2.1 概念说明

`__addsf3` 的入口只有一行（[lib/builtins/addsf3.c:13-16](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/addsf3.c#L13-L16)）：

```c
#define SINGLE_PRECISION
#include "fp_add_impl.inc"

COMPILER_RT_ABI float __addsf3(float a, float b) { return __addXf3__(a, b); }
```

真正的算法是 `fp_add_impl.inc` 里的 `__addXf3__`（[lib/builtins/fp_add_impl.inc:17](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/fp_add_impl.inc#L17)）。注意它**没有 `__SOFTFP__` 分支**——因为这个函数只在「没有浮点硬件」时被编译器调用，永远是纯软件实现。

加减法的难点不是「加」本身，而是为了**让最终结果舍入正确**，必须在尾数低位保留额外的「保护位」信息。这与十进制竖式「先对齐小数点再相加」同理：阶码不同的两个数相加前，必须先把小阶码的尾数右移对齐。

#### 4.2.2 核心流程（对齐阶码 → 尾数相加 → 规格化）

整个 `__addXf3__` 可以拆成三大步。下面用伪代码概述（以「绝对值较大者为 a」为前提）：

```
1. 特判：任一操作数为 0/∞/NaN → 直接返回对应特殊值。
2. 排序：保证 |a| >= |b|（结果符号取 a 的符号，阶码差 >= 0）。
3. 取出 a、b 的阶码与尾数；若是次正规数则先规格化。

─── 第一步：对齐阶码 ───
4. 给两个尾数左移 3 位，腾出最低 3 位给 round/guard/sticky；置上隐含位 1。
5. diff = aExp - bExp；把 b 的尾数右移 diff 位，
   被移出的位 OR 成一个 sticky 位放回最低位。

─── 第二步：尾数相加/相减 ───
6. 同号 → 尾数相加；若最高位进位，则右移 1 位、阶码 +1。
   异号 → 尾数相减；若发生抵消，则左移补位、阶码相应减小。

─── 第三步：规格化与舍入 ───
7. 阶码溢出(>=255) → 返回 ±∞。
8. 阶码 <= 0 → 结果是次正规数，右移尾数、阶码置 0。
9. 取出最低 3 位 round/guard/sticky，拼装结果位。
10. 按当前舍入模式(__fe_getround())做最终舍入；返回。
```

**round / guard / sticky 三件套**是软浮点正确舍入的关键（[lib/builtins/fp_add_impl.inc:85-91](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/fp_add_impl.inc#L85-L91) 把尾数左移 3 位正是为此）：

- **guard bit（保护位）**：舍入位置右侧第 1 位，决定是否「进半」。
- **round bit（舍入位）**：再右 1 位。
- **sticky bit（粘着位）**：所有更低位「或」成一个 bit——只要舍掉的部分非零它就是 1，用来打破「正好一半」的平局，实现「就近舍入、偶数优先」（round to nearest, ties to even）。

对齐阶码时被移出的尾数低位会被压缩进一个 sticky 位（[lib/builtins/fp_add_impl.inc:94-102](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/fp_add_impl.inc#L94-L102)）：

```c
const unsigned int align = (unsigned int)(aExponent - bExponent);
if (align) {
  if (align < typeWidth) {
    const bool sticky = (bSignificand << (typeWidth - align)) != 0;
    bSignificand = bSignificand >> align | sticky;
  } else {
    bSignificand = 1; // b 非零，全部移出 → 只剩一个 sticky 位
  }
}
```

#### 4.2.3 源码精读

**特判与排序**（[lib/builtins/fp_add_impl.inc:24-65](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/fp_add_impl.inc#L24-L65)）：先用一句精巧的比较把 0/∞/NaN 一次性筛出——`aAbs - 1 >= infRep - 1` 等价于「aAbs 为 0 或 ≥ infRep」。随后处理 NaN（或上 `quietBit` 静默化）、±∞、±0 的组合，并把 a、b 交换使 |a| 最大。

**取出阶码尾数 + 规格化次正规数**（[lib/builtins/fp_add_impl.inc:67-77](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/fp_add_impl.inc#L67-L77)）：

```c
int aExponent = aRep >> significandBits & maxExponent;
rep_t aSignificand = aRep & significandMask;
if (aExponent == 0)            // 次正规数：阶码字段为 0
  aExponent = normalize(&aSignificand);  // 左移到 1.x 形式，返回对应阶码
```

`normalize` 用 clz（上一讲讲过）把尾数左移到最高位为 1（[lib/builtins/fp_lib.h:227-231](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/fp_lib.h#L227-L231)）。

**第二步：尾数相加/相减**（[lib/builtins/fp_add_impl.inc:103-126](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/fp_add_impl.inc#L103-L126)）：注意异号相减时可能发生「相消」（如 \(1.0001 - 0.9999\)），需要左移结果并减小阶码（[lib/builtins/fp_add_impl.inc:111-115](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/fp_add_impl.inc#L111-L115)）；同号相加可能进位，需右移并加阶码（[lib/builtins/fp_add_impl.inc:121-125](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/fp_add_impl.inc#L121-L125)）。

**第三步：规格化、舍入**（[lib/builtins/fp_add_impl.inc:128-171](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/fp_add_impl.inc#L128-L171)）：先判溢出→∞（[L129-130](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/fp_add_impl.inc#L129-L130)），再判次正规（[L132-139](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/fp_add_impl.inc#L132-L139)），然后取低 3 位 `roundGuardSticky = aSignificand & 0x7`，拼装结果，最后按舍入模式调整（[L153-168](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/fp_add_impl.inc#L153-L168)）：

```c
switch (__fe_getround()) {
case CRT_FE_TONEAREST:
  if (roundGuardSticky > 0x4) result++;                 // 大于一半 → 进
  if (roundGuardSticky == 0x4) result += result & 1;    // 正好一半 → 偶数优先
  break;
case CRT_FE_DOWNWARD:  if (resultSign && roundGuardSticky) result++; break;
case CRT_FE_UPWARD:    if (!resultSign && roundGuardSticky) result++; break;
case CRT_FE_TOWARDZERO: break;                          // 直接截断
}
```

`0x4`（即二进制 `100`）正好表示「guard=1、round=0、sticky=0」——半个 ULP，于是套用「ties to even」。这与 IEEE 754 默认舍入模式一致。

#### 4.2.4 代码实践

**目标**：验证软浮点 `__addsf3` 与硬件 `float` 加法结果逐位一致，并画出三步流程。

**操作步骤**：项目里已有一个直接调用 `__addsf3` 并按位比较的测试 [test/builtins/Unit/addsf3_test.c](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/builtins/Unit/addsf3_test.c)，它用 `%librt` 链接 builtins 库（[addsf3_test.c:5-6](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/builtins/Unit/addsf3_test.c#L5-L6)）。仿照它写一个「软 vs 硬」对照程序（示例代码）：

```c
// soft_vs_hard.c —— 示例代码（非项目原有文件）
#include <stdint.h>
#include <stdio.h>
#include <string.h>

COMPILER_RT_ABI float __addsf3(float a, float b);  // 显式声明，强制链接软浮点实现
static uint32_t toRep(float x){ uint32_t r; memcpy(&r,&x,4); return r; }

int main(void) {
    float cases[][2] = {{1.0f,2.0f},{0.1f,0.2f},{1e30f,1e30f},{-1.0f,1.0f},
                        {1.5f,0.50000006f}};
    int bad = 0;
    for (int i = 0; i < 5; ++i) {
        float soft = __addsf3(cases[i][0], cases[i][1]); // 软浮点
        float hard = cases[i][0] + cases[i][1];          // 硬件 fadd（宿主机）
        uint32_t rs = toRep(soft), rh = toRep(hard);
        printf("%a + %a : soft=%08X hard=%08X %s\n",
               cases[i][0], cases[i][1], rs, rh, rs==rh?"OK":"DIFF");
        bad += (rs != rh);
    }
    return bad;
}
```

编译运行（需要 builtins 库；lit 中用 `%clang_builtins %s %librt`，手动等价命令待本地验证）：

```
clang soft_vs_hard.c -L<path-to-libdir> -lclang_rt.builtins -o soft_vs_hard && ./soft_vs_hard
```

**需要观察的现象 / 预期结果**（待本地验证）：在「就近舍入」下，所有用例的 `soft` 与 `hard` 位模式应当完全相同（`OK`）。若某条 `DIFF`，多半是宿主机默认舍入模式不同——这正是 `__fe_getround()` 存在的意义。

**流程图作业**：针对用例 `1.5f + 0.50000006f`，画出三步：
1. **对齐阶码**：两数阶码都是 127，diff=0，无需右移；写出各自尾数（含隐含位）。
2. **尾数相加**：左移 3 位后相加，记录是否进位。
3. **规格化/舍入**：取出低 3 位，判断是否触发 `>0x4` 或 `==0x4` 分支，写出最终结果位并与硬件对照。

#### 4.2.5 小练习与答案

**练习 1**：为什么对齐阶码时要把被移出的位压缩成一个 sticky 位，而不是直接丢弃？
**答案**：丢弃会丢失「舍掉部分是否非零」的信息，导致「正好一半」无法正确判定，从而违反「ties to even」。sticky 位用 1 个比特保留了「低位是否有非零」的事实，保证最终舍入正确。

**练习 2**：`1.0 + (-1.0)` 在算法里走的是「相加」还是「相减」分支？结果是什么？
**答案**：异号（`(aRep ^ bRep) & signBit` 非 0）走相减分支；由于绝对值相等，`aSignificand - bSignificand == 0`，直接 `return fromRep(0)`（[fp_add_impl.inc:106-107](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/fp_add_impl.inc#L106-L107)），返回 `+0.0`。

---

### 4.3 整数与浮点互转：`__floatdidf` 与 `__fixdfdi`

#### 4.3.1 概念说明

转换类函数与算术类有一个重要区别：它们**同时存在「硬件路径」和「软浮点路径」两套代码**，用 `#ifndef __SOFTFP__` 切换（见 [lib/builtins/fixdfdi.c:12](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/fixdfdi.c#L12)、[lib/builtins/floatdidf.c:23](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/floatdidf.c#L23)）。

为什么？因为**即便目标机有 FPU，也未必有「64 位整数 ↔ 双精度浮点」的单条指令**。典型场景：32 位 ARM hard-float、i386 等——它们能做 float 运算，但把 64 位整数转成 double 仍需一段辅助代码。于是：

- **硬件路径**（`__SOFTFP__` 未定义）：用 FPU 已有的加减指令巧妙拼出转换，并能顺带设置 FP 异常标志。
- **软浮点路径**（`__SOFTFP__` 已定义）：纯位运算，不依赖任何浮点指令，也不设置标志。

> 顺带一提：哪些函数有 `__SOFTFP__` 分支是源码里查得到的（`fixdfdi/fixsfdi/fixunsdfdi/fixunssfdi/floatdidf/floatundidf`），而 `__addsf3`/`__adddf3` 等纯算术没有——因为它们只在无 FPU 时被调用。

#### 4.3.2 核心流程

**`__floatdidf`（int64 → double）软浮点路径**，算法在 `__floatXiYf__`（[lib/builtins/int_to_fp_impl.inc:17](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/int_to_fp_impl.inc#L17)）：

```
1. a==0 → 返回 0.0。
2. 取绝对值：s = a >> 63（全 0 或全 1）；a = (a ^ s) - s。   // 经典无分支取绝对值
3. sd = 64 - clz(a)  // 有效位数；e = sd - 1 // 最高位的指数
4. 若 sd > 53（double 尾数+隐含位）：多出的低位需舍入。
     - 保留 53 位 + 1 位 guard + 1 位 sticky，做「就近、偶数优先」舍入；
     - 舍入可能进位使位数 +1，此时再右移并 e+=1。
   否则（sd <= 53）：左移补齐到 53 位，无损。
5. 拼装：sign | ((e + bias) << 52) | (a & 0xFFFFFFFFFFFFF)。
```

**`__fixdfdi`（double → int64）软浮点路径**，算法在 `__fixint`（[lib/builtins/fp_fixint_impl.inc:16](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/fp_fixint_impl.inc#L16)）：

```
1. 拆出 sign / unbiased_exponent (=存储阶码 - 1023) / 尾数(补上隐含位)。
2. exponent < 0 → |a| < 1 → 返回 0（向零截断）。
3. exponent >= 64 → 溢出 → 饱和到 INT64_MAX / INT64_MIN。
4. 若 exponent < 52：significand >> (52 - exponent)  // 尾数右移到整数位
   否则：            significand << (exponent - 52)  // 左移补零
5. 乘上 sign 得最终整数。
```

注意第 4 步的分支：因为补了隐含位后尾数固定占 bit 52，所以指数决定的是「小数点相对尾数的位置」——指数小则尾数要右移（丢掉小数部分），指数大则左移。

#### 4.3.3 源码精读

**`__floatdidf` 硬件路径**（[lib/builtins/floatdidf.c:27-41](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/floatdidf.c#L27-L41)）用一个经典的「\(2^{52}\) 技巧」：

```c
static const double twop52 = 4503599627370496.0; // 2^52
static const double twop32 = 4294967296.0;       // 2^32
union { int64_t x; double d; } low = {.d = twop52};
const double high = (int32_t)(a >> 32) * twop32;          // 高 32 位的贡献
low.x |= a & INT64_C(0x00000000ffffffff);                 // 低 32 位塞进 [2^52,2^53) 的 double
const double result = (high - twop52) + low.d;
```

原理：在 \([2^{52}, 2^{53})\) 区间内，double 的尾数恰好有 52 位可用，能**精确**表示每一个 64 位整数中的低 32 位（`low.d`）；高 32 位则乘 \(2^{32}\) 转成 double（`high`）。最后用一次浮点加法把两半合并，减去 `twop52` 抵消人为加的偏置。这段代码「用 FPU 的整数精确性」完成了转换，并能顺带置 inexact 标志。

**`__floatdidf` 软浮点路径**（[lib/builtins/floatdidf.c:48-52](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/floatdidf.c#L48-L52)）：

```c
#define SRC_I64
#define DST_DOUBLE
#include "int_to_fp_impl.inc"
COMPILER_RT_ABI double __floatdidf(di_int a) { return __floatXiYf__(a); }
```

这两个宏在 [lib/builtins/int_to_fp.h:19-27](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/int_to_fp.h#L19-L27)（`SRC_I64`→`src_t=int64_t`、`clzSrcT=__builtin_clzll`）和 [lib/builtins/int_to_fp.h:52-59](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/int_to_fp.h#L52-L59)（`DST_DOUBLE`→`dst_t=double`、`dstSigBits=52`）里被翻译成具体类型，于是同一份 `__floatXiYf__` 可服务 `int→float`、`int64→double`、`int128→quad` 等多种组合。

舍入细节（多出位数时）见 [lib/builtins/int_to_fp_impl.inc:40-57](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/int_to_fp_impl.inc#L40-L57)：先对齐到「53 位 + guard + sticky」，再 `a |= (a & 4) != 0`（把 guard 并入 sticky 判断）、`++a`（进位舍入）、`a >>= 2`（丢掉 guard/sticky）。若进位使位数增加则再右移并 `++e`（见 [int_to_fp_impl.inc:53-56](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/int_to_fp_impl.inc#L53-L56)）。

**`__fixdfdi`** 的两条路径在 [lib/builtins/fixdfdi.c:18-34](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/fixdfdi.c#L18-L34)：硬件路径直接委托给无符号版 `__fixunsdfdi` 并补符号（[L18-23](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/fixdfdi.c#L18-L23)）；软浮点路径包含 `fp_fixint_impl.inc` 并调用 `__fixint`（[L30-34](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/fixdfdi.c#L30-L34)）。`__fixint` 的移位分支见 [lib/builtins/fp_fixint_impl.inc:36-39](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/fp_fixint_impl.inc#L36-L39)。

> 旁注：ARM EABI 为这些函数额外提供 `__aeabi_*` 别名（如 `__aeabi_l2d`、`__aeabi_d2lz`），见 [floatdidf.c:55-61](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/floatdidf.c#L55-L61) 与 [fixdfdi.c:38-44](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/fixdfdi.c#L38-L44)。

#### 4.3.4 代码实践

**目标**：用一个具体值 `int64_t` → `double` → `int64_t` 走一遍 `__floatdidf`/`__fixdfdi`，并对照硬件结果。

**操作步骤**（示例代码）：

```c
// convert_probe.c —— 示例代码（非项目原有文件）
#include <stdint.h>
#include <stdio.h>
COMPILER_RT_ABI double __floatdidf(int64_t a);
COMPILER_RT_ABI int64_t __fixdfdi(double a);

int main(void) {
    int64_t in = 0x123456789ABCDEF0LL;       // 56 位有效，会被舍入
    double d_soft = __floatdidf(in);
    double d_hard = (double)in;              // 硬件（宿主机）
    printf("in   = %llx\n", (unsigned long long)in);
    printf("soft = %a (%.1f)\n", d_soft, d_soft);
    printf("hard = %a (%.1f)\n", d_hard, d_hard);
    printf("fixdfdi(soft) = %lld\n", (long long)__fixdfdi(d_soft));
    return 0;
}
```

同样用 `%librt` 思路链接 builtins 库运行。

**需要观察的现象 / 预期结果**（待本地验证）：

- `in` 有 56 个有效位 > double 的 53 位，`__floatdidf` 走舍入分支；`d_soft` 与 `d_hard` 位模式应一致。
- 反向 `__fixdfdi(d_soft)` 得到的整数会比原 `in` 丢失最低 3 位精度（因为 double 无法精确表示 56 位整数）。

**源码阅读型任务**（无需运行）：对照 [int_to_fp_impl.inc:30-31](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/int_to_fp_impl.inc#L30-L31)，对 `in = 0x123456789ABCDEF0` 算出 `sd`（有效位数）和 `e`（指数），判断它走「舍入」还是「左移补齐」分支，并解释为什么反向转换会丢精度。

#### 4.3.5 小练习与答案

**练习 1**：`__floatdidf` 的硬件路径里，为什么要把低 32 位塞进一个值在 \([2^{52},2^{53})\) 的 double？
**答案**：在该区间 double 恰有 52 位尾数可用，能逐位精确表示 32 位整数，没有任何舍入；这样「低 32 位」就成了一个可被 FPU 精确加减的量，避免在转换过程中引入额外误差。

**练习 2**：`__fixdfdi(0.9)` 与 `__fixdfdi(-0.9)` 分别返回什么？依据 `__fixint` 哪一行？
**答案**：都返回 0。两者的无符号绝对值 `< 1`，`exponent < 0`，命中 [fp_fixint_impl.inc:27-28](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/fp_fixint_impl.inc#L27-L28) 返回 0——这是「向零截断」语义。

**练习 3**：把一个 `1e300` 的 double 传给 `__fixdfdi` 会得到什么？
**答案**：溢出（指数远超 63），命中 [fp_fixint_impl.inc:31-32](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/fp_fixint_impl.inc#L31-L32)，返回 `INT64_MAX`（饱和）。

---

## 5. 综合实践

把本讲三块内容串起来，完成下面这个「迷你软浮点探针」任务：

1. **表示层**（对应 4.1）：写函数 `void decode(float x)` 打印 `x` 的符号、（无偏）阶码、（含隐含位的）尾数二进制串。用它验证 `1.0`、`2.0`、`1.5`、最小次正规数、`+∞`。
2. **加法层**（对应 4.2）：仿照 `addsf3_test.c` 写一个驱动，对一组 `(a,b)` 同时调用 `__addsf3(a,b)` 与硬件 `a+b`，按位比较并统计不一致数；再挑一个用例，用 `decode` 画出 a、b、`__addsf3(a,b)` 三者的位分解，手工核对「对齐阶码 → 尾数相加 → 规格化」三步。
3. **转换层**（对应 4.3）：取一个 60 位有效的大整数 `N`，比较 `__floatdidf(N)` 与 `(double)N`；再把结果 `__fixdfdi` 回去，观察精度损失，并用 `decode` 解释为什么（53 位尾数装不下 60 位整数）。

**验收标准**（待本地验证）：在就近舍入模式下，第 2 步的不一致数应为 0；第 3 步能解释丢失的低位数量 = `有效位数 - 53`。

> 进阶可选：用交叉编译为目标 `armv7em-none-eabi -mfloat-abi=soft` 编译一个含 `float` 运算的小程序，用 `objdump -d` 观察反汇编里出现的 `bl __addsf3`、`bl __floatsidf` 之类调用，亲眼看到「编译器插入软浮点内建调用」这件事。

## 6. 本讲小结

- 软浮点的本质是**把浮点位当整数，手动拆出符号/阶码/尾数再用整数运算重组**；`fp_lib.h` 的 `toRep`/`fromRep` 与一组派生位掩码（`implicitBit`、`significandMask`、`infRep`、`qnanRep` 等）是全部算法的地基。
- `__addsf3` 真正的实现是参数化的 `__addXf3__`，核心三步是**对齐阶码（含 sticky 位）→ 尾数相加/相减 → 规格化并按舍入模式取整**；左移 3 位腾出 round/guard/sticky 是正确舍入的关键。
- 转换类函数 `__floatdidf`/`__fixdfdi` 用 `__SOFTFP__` 区分**硬件路径**（用 \(2^{52}\) 技巧或委托无符号版，可设 FP 标志）与**软浮点路径**（纯位运算，参数化复用 `__floatXiYf__`/`__fixint`），因为即便有 FPU 也未必有 64 位整数互转的单条指令。
- 编译器何时插入这些调用：无 FPU/`-msoft-float` 时插入 `__addsf3` 等算术；缺乏 64 位整数互转指令的 32 位平台插入 `__floatdidf`/`__fixdfdi`。
- 大量手法（clz 规格化、无分支取绝对值、guard/sticky 舍入）与上一讲整数内建一脉相承；本讲把它们用到了「整数视角的浮点」上。

## 7. 下一步学习建议

- **横向扩展算术**：按本讲的读法去读 [lib/builtins/mulsf3.c](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/mulsf3.c)→`fp_mul_impl.inc`（注意它会用到 `fp_lib.h` 里的 `wideMultiply` 把两个尾数宽乘）和 [lib/builtins/divsf3.c](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/divsf3.c)→`fp_div_impl.inc`，巩固「参数化 + `.inc` 复用」的工程模式。
- **横向扩展转换**：对比 `__floatundidf`（无符号版）、`__fixunsdfdi`，看 `SRC_U64` 与有符号路径在「取绝对值」一步的差异。
- **走向更宽精度**：阅读 `fp_lib.h` 的 `DOUBLE_PRECISION`/`QUAD_PRECISION` 段，理解 `rep_t` 如何随精度变化、128 位 `wideMultiply` 如何用 4×4 个 32×32→64 乘积拼出 128×128→256。
- **架构覆盖视角**：本单元（u2）下一讲 [u2-l4](u2-l4-arch-specific-builtins.md) 将转向架构相关内建与 `cpu_model`，把「通用兜底实现 vs 架构优化实现」的构建选择讲透，作为 builtins 单元的收尾。
