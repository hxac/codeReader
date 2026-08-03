# builtins 概览：libgcc 的替代品

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚 `compiler-rt/lib/builtins` 这组库是「做什么的」：它替编译器提供那些「编译器在生成机器码时偷偷插进去、但目标平台没有对应单条指令」的帮手函数。
- 理解它为什么是 GCC 工具链中 `libgcc` 的对应物，以及它为什么保持与 libgcc 完全一致的函数命名。
- 掌握 `int_types.h` 里 `si_int / di_int / ti_int` 这套统一整数类型，以及 `dwords` 等 union 如何让一份纯 C 代码在 32 位机器上操作 64 位整数。
- 看懂 `CMakeLists.txt` 如何用「通用实现 + 架构子目录」并存的方式，为每个架构各构建一个 `clang_rt.builtins-<arch>` 静态库，并理解架构优化版本如何「覆盖」通用版本。

本讲是整个 builtins 单元（u2）的总览与地基。后续 u2-l2（整数算术）、u2-l3（软浮点）、u2-l4（架构相关内建）会在这里建立的心智模型上继续深入。

## 2. 前置知识

在开始之前，你只需要具备以下直觉：

- **机器指令是有限的。** 一条机器指令能做的事由 CPU 架构决定。例如 32 位 x86 的 `divl` 指令只能做 32 位除法；如果程序里出现 `int64_t` 的除法，CPU 没有单条指令能完成它。
- **编译器会「调用函数」来补齐能力。** 当一条高级运算无法映射到一条机器指令时，编译器（这里是 Clang/LLVM）不会硬凑，而是生成一条「调用某个名字固定的帮手函数」的指令。这个帮手函数的实现在运行时库里。
- **运行时库有两种来源。** 用 GCC 编译的程序，这些帮手函数来自 `libgcc`；用 Clang/LLVM 编译的程序，可以来自 `compiler-rt` 的 `builtins` 子库。两者实现不同，但提供的符号名字必须一致，这样不管用哪个工具链，同一段机器码都能找到对应的帮手函数。
- **名词：ABI（应用二进制接口）。** 「函数叫什么名字、参数怎么传、返回值放哪」这些机器层面的约定就是 ABI。builtins 的函数名属于 ABI 的一部分，一旦定下就不能改，否则已有的程序链接时会找不到符号。

> 与上一讲的衔接：在 [u1-l2](u1-l2-directory-structure.md) 中我们把 `lib/` 下的子库做了分类，`builtins` 是其中「低级运算帮手」这一类。本讲就专门拆开这一类。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| `lib/builtins/README.txt` | builtins 的「说明书 + 函数清单」，明确写出它是 libgcc 的替代品，并列出全部帮手函数的签名。 |
| `lib/builtins/int_lib.h` | 整个 builtins 的公共配置头：定义跨编译器/跨平台的宏（如 `COMPILER_RT_ABI`）、声明公共假设（补码、算术右移等），并 `#include` 类型定义与工具函数。 |
| `lib/builtins/int_types.h` | 统一的整数类型定义：`si_int / di_int / ti_int` 等，以及把大整数拆成「高位/低位字」的 union。 |
| `lib/builtins/CMakeLists.txt` | 构建脚本：定义 `GENERIC_SOURCES`（通用纯 C 实现）和每个架构的源文件列表，按架构组装出 `clang_rt.builtins-<arch>`。 |
| `lib/builtins/clzdi2.c` | `__clzdi2`（64 位前导零计数）的实现——一个简短、典型的通用实现样例。 |
| `lib/builtins/divdi3.c` | `__divdi3`（64 位有符号除法）的实现——展示了「通用实现委托给共享算法」的写法。 |
| `lib/builtins/i386/udivdi3.S` | 32 位 x86 上 `__udivdi3`（64 位无符号除法）的汇编优化实现——架构子目录覆盖通用实现的典型例子。 |
| `cmake/Modules/CompilerRTUtils.cmake` | 提供 `filter_builtin_sources` 函数，实现「架构文件覆盖同名通用文件」的机制。 |

## 4. 核心概念与源码讲解

### 4.1 libgcc 替代关系与函数命名

#### 4.1.1 概念说明

先建立一个核心直觉：

> **编译器不是全能的。当它遇到目标 CPU 没有对应单条指令的运算时，它会生成「调用一个名字固定的帮手函数」的代码，而 builtins 就是提供这些帮手函数的库。**

举几个真实场景：

- **32 位平台上的 64 位除法。** 32 位 x86 没有 64 位除法指令。你写 `int64_t a / int64_t b`，编译器会生成一条调用 `__divdi3`（有符号）或 `__udivdi3`（无符号）的指令，真正的除法逻辑在函数里用软件实现。
- **没有浮点硬件的平台上的浮点运算。** 某些 MCU（如 32 位 ARM 的 Thumb1、AVR、软浮点 RISC-V）没有 FPU，`float + float` 会被编译成对 `__addsf3` 的调用，加法在软件里用整数运算模拟。
- **位计数、前导零。** 没有 `clz` 指令的架构上，`__builtin_clz` 会落地成对 `__clzdi2` 的调用。

这套「帮手函数」的概念并非 LLVM 发明，它来自 GCC 的 `libgcc`。`lib/builtins/README.txt` 第一句就点明了这层关系：

[lib/builtins/README.txt:12](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/README.txt#L12)：这里是「This is a replacement library for libgcc」（这是 libgcc 的替代库）。后面还规定「Each function is contained in its own file」（每个函数独占一个文件）。它甚至直接指向 libgcc 的官方规范作为实现依据：

[lib/builtins/README.txt:21](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/README.txt#L21)：函数的行为规范来自 `http://gcc.gnu.org/onlinedocs/gccint/Libgcc.html#Libgcc`。

**为什么名字必须和 libgcc 完全一致？** 因为函数名是 ABI 的一部分。假设一段机器码里有一条 `call __udivdi3`，那么运行时库里就必须存在一个叫 `__udivdi3` 的符号，否则链接失败。GCC 和 Clang 都按 libgcc 的命名来生成调用，所以 builtins 也必须沿用同样的名字——这是一份「替代品」，连符号表都得对得上。

#### 4.1.2 核心流程：函数名怎么读

builtins 的函数名看起来像天书（`__udivmoddi4`、`__floatundixf`、`__fixunsdfdi`），但其实有规律。名字由几段拼接而成：

```
__  <操作>  <宽度或源类型>  <目标类型?>  <结尾数字?>
```

- **前缀 `__`**：C 标准保留给实现（编译器/库）的命名空间，避免和用户代码冲突。
- **操作**：`div`=除法、`mul`=乘法、`add`=加法、`clz`=count leading zeros（前导零计数）、`ctz`=count trailing zeros、`fix`=浮点转整数、`float`=整数转浮点、`ashl/ashr/lshr`=算术/算术/逻辑移位。
- **宽度（整数运算）**：`si`=single int（32 位）、`di`=double int（64 位）、`ti`=tertiary/quad int（128 位）。
- **类型（浮点运算）**：`sf`=single float、`df`=double float、`xf`=80 位扩展精度、`tf`=128 位四精度。
- **结尾数字**：来自 libgcc 历史命名，属于固定 ABI 符号名的一部分，直接整体记忆即可。

几个拆解示例：

| 函数名 | 拆解 | 含义 |
| --- | --- | --- |
| `__divdi3` | div + di + 3 | 64 位有符号除法 `a / b` |
| `__udivdi3` | u(无符号) + div + di + 3 | 64 位无符号除法 |
| `__clzdi2` | clz + di + 2 | 64 位整数的前导零个数 |
| `__addsf3` | add + sf + 3 | 单精度浮点加法 |
| `__fixdfdi` | fix(浮点转整) + df(源:double) + di(目标:64位整) | double 转 int64 |
| `__floatundidf` | float(整转浮点) + un(无符号) + di(源:64位整) + df(目标:double) | uint64 转 double |

最后两行的「转换类」函数特别值得注意：它们的名字里**同时编码了源类型和目标类型**，所以没有结尾数字。`__fixdfdi` 里 `df` 是源（double）、`di` 是目标（int64），一眼就能读出「double → int64」。

#### 4.1.3 源码精读

`README.txt` 从第 30 行开始是一整段「目录式」的函数清单，开头先列出了统一类型别名：

[lib/builtins/README.txt:30-34](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/README.txt#L30-L34)：声明 `si_int/su_int`（32 位）和 `di_int/du_int`（64 位）这几个别名。

接着按功能分组列出函数，例如「Integral bit manipulation」段的移位与计数：

[lib/builtins/README.txt:46-51](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/README.txt#L46-L51)：`__clzsi2/__clzdi2/__clzti2`（前导零）与 `__ctz*`（尾随零）。

以及「Integral arithmetic」段的除法：

[lib/builtins/README.txt:74-85](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/README.txt#L74-L85)：有符号 `__div*3` 与无符号 `__udiv*3`、取模 `__mod*3`/`__umod*3`，按 si/di/ti 三种宽度成套出现。

> 这份清单是你日后「想知道某个内建函数语义」时的第一手参考。比如要确认 `__udivmoddi4` 的参数含义，直接来这里看签名即可。

#### 4.1.4 代码实践

**实践目标**：亲手把两个函数（`__divdi3`、`__clzdi2`）从「文档清单」定位到「真实实现文件」，并验证它们确实是编译器会调用的帮手函数。

**操作步骤**：

1. 在 `README.txt` 中找到 `__divdi3` 与 `__clzdi2` 的声明（分别在算术段和位操作段），记下它们的签名。
2. 在源码树中定位实现：
   - `__divdi3` → `lib/builtins/divdi3.c`
   - `__clzdi2` → `lib/builtins/clzdi2.c`
3. 写一个最小 C 程序，在 **32 位 x86** 目标上做 64 位除法，用 `-S` 生成汇编，观察是否出现对 `__divdi3`/`__udivdi3` 的调用。示例命令（待本地验证，需要 32 位目标的支持）：

   ```bash
   # 生成 32 位 x86 汇编，观察 64 位除法被替换成函数调用
   clang -target i386-linux-gnu -O1 -S test.c -o test_32.s
   grep -E "__divdi3|__udivdi3|__clzdi2" test_32.s
   ```

4. 把同一个程序针对 **64 位 x86_64** 再生成一次汇编对照：

   ```bash
   clang -target x86_64-linux-gnu -O1 -S test.c -o test_64.s
   grep -E "__divdi3|__udivdi3" test_64.s
   ```

**需要观察的现象 / 预期结果**（待本地验证）：

- 32 位版本的汇编里应能搜到 `__udivdi3`（或 `__divdi3`）的调用符号——因为 32 位 x86 没有 64 位除法指令，编译器只能调用帮手函数。
- 64 位版本里**不应**出现这些调用——x86_64 有原生的 64 位 `div`/`idiv` 指令，编译器直接生成指令，不需要帮手函数。

这一步直观印证了本节的核心论点：「帮手函数只在该运算没有硬件指令支持时才会被生成调用」。

#### 4.1.5 小练习与答案

**练习 1**：函数名 `__mulodi4` 中，`mul`、`o`、`di`、`4` 分别可能代表什么？

**参考答案**：`mul`=乘法；`o`=overflow（带溢出检测）；`di`=64 位整数；`4` 是 libgcc 历史命名里该函数的固定结尾数字。结合 `README.txt` 第 120-122 行，它的语义是「64 位有符号乘法，若结果超出有符号范围则把 overflow 标志置 1」。

**练习 2**：为什么 builtins 必须沿用 libgcc 的函数名，而不能重新设计一套「更优雅」的命名？

**参考答案**：因为这些函数名是 ABI 的一部分。编译器（GCC 和 Clang）生成的机器码里直接写死了对这些符号的调用；如果 builtins 改名，已有程序的机器码就找不到对应符号，链接会失败。「替代品」必须在符号层面一一对应。

**练习 3**：`__fixunsdfdi` 这个名字怎么读？语义是什么？

**参考答案**：`fix`=浮点转整数；`un`=unsigned（结果按无符号处理）；`df`=源是 double；`di`=目标是 64 位整数。语义：把 `double` 转换为 `uint64_t`（按无符号解释）。

---

### 4.2 统一的整数类型基础：int_lib.h / int_types.h

#### 4.2.1 概念说明

builtins 要在「从 8 位 AVR 到 64 位 x86_64」的五花八门架构上都能编译运行。一个绕不开的矛盾是：**算法要操作 64 位甚至 128 位整数，但宿主机器可能只有 32 位（甚至更小）的寄存器，C 语言层面也可能没有 64 位整数类型。**

`int_types.h` 用两个手段解决了这个矛盾：

1. **统一类型别名**：用 `si_int/di_int/ti_int` 这样一组名字，把「逻辑宽度」和「具体 C 类型」解耦。所有算法只写 `di_int`，至于 `di_int` 在底层到底是什么，由这个头文件统一定义。
2. **「大数拆小」的 union**：提供 `dwords`/`udwords`/`twords` 等 union，让你能用「高位字 + 低位字」的方式访问一个 64/128 位整数——这样在 32 位机器上，算法也能逐「字（32 位）」地处理 64 位运算。

而 `int_lib.h` 是 builtins 里**每个 .c 文件都包含**的总配置头，它负责：定义跨编译器宏（MSVC vs Clang/GCC）、声明公共假设、再 `#include` 进 `int_types.h` 和工具函数。

#### 4.2.2 核心流程：一份代码如何同时跑在 32 位和 64 位机器上

以 64 位除法为例，整个套路是：

```text
        di_int a, b                 ← 调用者传进来的是 64 位整数
             │
             ▼
   dwords x; x.all = a;             ← 用 union 把 64 位拆成 high/low 两个 32 位字
             │
             ▼
   逐「字」做 32 位运算               ← 32 位机器上每一步都是原生 32 位操作
   （借 CPU 的 32 位除法指令）
             │
             ▼
   组合结果，返回 di_int              ← 拼回 64 位整数
```

关键在于：算法**只信赖 32 位运算**。即使运行它的 CPU 是 32 位的，代码也能正确工作；如果 CPU 是 64 位的，这些 32 位运算同样成立，只是没有发挥全部性能（这部分性能损失由「架构子目录的汇编优化版」补回，见 4.3 节）。

#### 4.2.3 源码精读

**（1）公共假设与配置头 `int_lib.h`**

`int_lib.h` 开头就声明了三条全局假设，builtins 的所有算法都建立在这三条之上：

[lib/builtins/int_lib.h:17-19](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/int_lib.h#L17-L19)：假设有符号整数是补码；有符号负数右移是算术右移（高位补符号位）；字节序要么小端要么大端。

这些假设一旦不成立（比如某架构用反码），整套算法就得重写。它还定义了跨编译器/跨平台的关键宏，例如 ARM 上用于指定 AAPCS 调用约定的 `COMPILER_RT_ABI`：

[lib/builtins/int_lib.h:23-31](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/int_lib.h#L23-L31)：在 ARM 非 hardfloat 目标上把函数标记为 `__pcs__("aapcs")` 调用约定，其他平台该宏为空。这保证了帮手函数的调用约定与调用方一致。

最后它把类型定义和工具函数引进来：

[lib/builtins/int_lib.h:99-102](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/int_lib.h#L99-L102)：`#include "int_types.h"` 与 `#include "int_util.h"`。所以「包含 `int_lib.h`」就等于拿到了全部类型基础。

**（2）统一整数类型 `int_types.h`**

核心类型别名定义在这里：

[lib/builtins/int_types.h:25-38](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/int_types.h#L25-L38)：`si_int=int32_t`、`su_int=uint32_t`、`di_int=int64_t`、`du_int=uint64_t`。

> 注意第 22-24 行有个小细节：Linux 的 `asm-generic/siginfo.h` 也定义了 `si_int` 这个名字，所以这里先 `#undef si_int` 再重新 typedef，避免宏冲突。这种「与系统头共处」的处理正是 builtins 要跨平台编译的体现。

**「大数拆小」的核心——`dwords` union**：

[lib/builtins/int_types.h:40-51](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/int_types.h#L40-L51)：定义 `dwords`，它既能作为整体 `all`（一个 `di_int`）访问，也能拆成 `.s.high` / `.s.low`（两个 32 位字）。`udwords` 是对应的无符号版本。字节序由 `_YUGA_LITTLE_ENDIAN` 宏控制 high/low 的排列。

这就是「32 位机器操作 64 位数」的关键武器。下面马上看一个真实用例。

**（3）`clzdi2.c`——一个完美的微型范例**

`__clzdi2`（64 位前导零计数）只有几行，却完整展示了「拆字 → 逐字处理 → 组合」的套路：

[lib/builtins/clzdi2.c:29-35](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/clzdi2.c#L29-L35)：

```c
COMPILER_RT_ABI int __clzdi2(di_int a) {
  dwords x;
  x.all = a;
  const si_int f = -(x.s.high == 0);
  return clzsi((x.s.high & ~f) | (x.s.low & f)) +
         (f & ((si_int)(sizeof(si_int) * CHAR_BIT)));
}
```

读法：把 64 位 `a` 装进 `dwords`，再读它的 `.s.high`（高 32 位）和 `.s.low`（低 32 位）。逻辑是「若高位非零，就对高位计数前导零；若高位为零，就在低位计数前导零并加上 32」。其中 `clzsi` 是个宏（见下文），映射到 32 位的 `__builtin_clz`。整段代码**只依赖 32 位运算**，因此在 32 位机器上直接可跑。

**（4）`clzsi` 宏与 32 位原语的桥接**

[lib/builtins/int_types.h:27-35](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/int_types.h#L27-L35)：`clzsi` 宏会根据系统上到底是 `int` 还是 `long` 是 32 位，自动映射到 `__builtin_clz` 或 `__builtin_clzl`。这样算法代码统一写 `clzsi(...)`，底层却能在不同 ABI 下选用正确的编译器内建。

**（5）128 位类型与 `make_tu`**

128 位运算（`ti_int/tu_int`）是按需启用的：

[lib/builtins/int_types.h:66-69](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/int_types.h#L66-L69)：只有目标支持 128 位整数（如 64 位指针、`__SIZEOF_INT128__` 等）时才定义 `CRT_HAS_128BIT`。

[lib/builtins/int_types.h:84-85](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/int_types.h#L84-L85)：在 128 位可用时，用 GCC 扩展 `__attribute__((mode(TI)))` 定义 `ti_int`（TImode = 128 位）。

配套的 `twords`/`utwords` union 把 128 位拆成两个 64 位字，并有拼装辅助函数：

[lib/builtins/int_types.h:120-125](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/int_types.h#L120-L125)：`make_tu(h, l)` 把高/低两个 `du_int` 拼成一个 `tu_int`。

#### 4.2.4 代码实践

**实践目标**：用 `dwords` union 复刻 `__clzdi2` 的「拆字」思想，直观体会「32 位机器如何操作 64 位数」。

**操作步骤**：

1. 阅读 `clzdi2.c` 第 29-35 行，理解 `dwords x; x.all = a;` 之后 `x.s.high` / `x.s.low` 分别代表什么。
2. 自己写一个小程序（示例代码，非项目原有代码），用 union 把一个 `uint64_t` 拆成高低两个 32 位字并打印：

   ```c
   // 示例代码：体会 dwords 的拆字思想
   #include <stdint.h>
   #include <stdio.h>

   typedef union {
       uint64_t all;
       struct { uint32_t low; uint32_t high; } s; // 假设小端
   } my_dwords;

   int main(void) {
       my_dwords x;
       x.all = 0x00000001FFFFFFFFULL; // 高 32 位几乎全 0
       printf("high=0x%08x low=0x%08x\n", x.s.high, x.s.low);
       // 高位为 0 → 前导零应集中在高位 + 低位前导部分
       return 0;
   }
   ```

3. 编译运行（待本地验证）：`clang demo.c -o demo && ./demo`。

**需要观察的现象 / 预期结果**：程序能正确拆出 `high=0x00000001`、`low=0xffffffff`。这说明「一个 64 位数 = 高 32 位字 + 低 32 位字」这件事可以用 union 无开销地完成，而这正是 builtins 通用算法的地基。

#### 4.2.5 小练习与答案

**练习 1**：`si_int`、`di_int`、`ti_int` 分别是多宽的整数？为什么不用 `int`、`long long` 直接写？

**参考答案**：分别是 32 / 64 / 128 位。不直接用 `int`、`long long` 是因为它们的实际宽度随平台变化（`long` 在 Linux 64 位是 64 位，在 Windows 64 位却是 32 位）。用固定语义的别名 + `stdint.h` 的定宽类型，才能保证算法逻辑在所有平台上一致。

**练习 2**：`dwords` 这个 union 解决了什么问题？为什么不用位运算（`>> 32`、`& 0xffffffff`）来拆字？

**参考答案**：`dwords` 让你「同时以整体和拆分两种视角」访问同一个 64 位数，可读性好且是零开销（union 不产生运行时代码）。位运算也能拆字，但在 32 位机器上 `>> 32` 对 64 位的操作本身又可能依赖帮手函数或多次移位，不如 union 直观高效。两者本质都在做「把 64 位拆成两个 32 位字」。

**练习 3**：`CRT_HAS_128BIT` 在什么情况下会被取消定义？

**参考答案**：见 `int_types.h` 第 74-81 行——当用 MSVC（且非 Clang）编译时，或目标是 SPIR-V 时，因为没有可用的 128 位整数类型，会 `#undef CRT_HAS_128BIT`，从而跳过所有 `ti` 系列 128 位实现。

---

### 4.3 通用实现与架构子目录：一函数一文件与按架构选择

#### 4.3.1 概念说明

理解了「功能」和「类型基础」后，最后一个问题是**工程组织**：这么多函数、这么多架构，源码是怎么摆放、又是怎么按架构挑出正确的一套来编译的？

builtins 的答案是两个原则：

1. **一函数一文件**：`README.txt` 明说「Each function is contained in its own file」。`__divdi3` 在 `divdi3.c`、`__clzdi2` 在 `clzdi2.c`……这种极致的拆分让构建系统能像点菜一样精确地「挑」出某个架构需要的函数，剔除不需要的。
2. **通用 + 架构并存**：
   - 顶层有一套 `GENERIC_SOURCES`——纯 C、可移植，保证「至少能正确运行」。
   - 每个架构子目录（`i386/`、`aarch64/`、`arm/`、`riscv/`、`x86_64/`、`ppc/`、`ve/`、`avr/` 等）存放针对该架构的优化实现（多为 `.S` 汇编）。
   - 构建某个架构时，CMake 先用「通用 + 该架构独有」拼出源文件清单，再让架构版本**覆盖**同名的通用版本。

这种「兜底的通用实现 + 追求性能的架构优化」并存，是 builtins 能同时兼顾「覆盖全架构」和「在主流架构上够快」的关键。

#### 4.3.2 核心流程：CMake 如何为每个架构拼出最终源文件清单

整个流程可以概括为「基础套餐 + 加菜 + 去重」：

```text
对每个被支持的架构 arch：
  1. 基础套餐：${arch}_SOURCES = GENERIC_SOURCES (+ GENERIC_TF_SOURCES)
  2. 加菜：    追加该架构子目录里的优化实现（如 i386/udivdi3.S）
  3. 去重：    filter_builtin_sources() 把「被架构版本覆盖的通用 .c」从清单里删掉
  4. 编译：    add_compiler_rt_runtime() 用最终清单编出 clang_rt.builtins-<arch>
```

第 3 步「去重」的规则很朴素：如果某个架构版本文件叫 `foo.S`，就去找同名的通用 `foo.c` 并删掉它（同时还会读取该文件标注的 `SUPERSEDES` 属性，额外覆盖它声明的那些函数）。

#### 4.3.3 源码精读

**（1）CMakeLists 顶部的总述**

[lib/builtins/CMakeLists.txt:1-3](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/CMakeLists.txt#L1-L3)：明确说本目录包含「核心运行时库的通用实现（generic implementations）以及各子目录里针对架构优化的代码（architecture-specific code）」。这一句话就是本节主题的官方陈述。

**（2）通用实现的基线 `GENERIC_SOURCES`**

[lib/builtins/CMakeLists.txt:85-206](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/CMakeLists.txt#L85-L206)：这是一长串纯 C 文件名（`divdi3.c`、`clzdi2.c`、`udivdi3.c`……），它们就是「兜底」的可移植实现。每个架构的源文件清单都以它为起点。

与之并列的还有 `GENERIC_TF_SOURCES`（128 位四精度浮点的通用实现）和 `x86_80_BIT_SOURCES`（x86 的 80 位扩展精度），按需叠加。

**（3）架构清单如何「加菜 + 覆盖」——以 i386 为例**

32 位 x86 的清单最能体现「覆盖」：

[lib/builtins/CMakeLists.txt:424-439](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/CMakeLists.txt#L424-L439)：`i386_SOURCES` 先放入 `${GENERIC_SOURCES}`，再追加一串 `i386/*.S` 汇编文件，其中就包括 `i386/udivdi3.S`、`i386/divdi3.S`、`i386/muldi3.S` 等。

也就是说：i386 上既有通用的 `udivdi3.c`（来自 GENERIC），又有汇编优化的 `i386/udivdi3.S`。最终谁生效，取决于下一步的「去重」。

对照看 x86_64：它**不**追加 `udivdi3.S` 这类文件，因为有原生 64 位除法指令，直接用通用 C 实现即可（通用实现最终会被编译成使用 `div`/`idiv` 指令的代码）。

**（4）「去重」机制 `filter_builtin_sources`**

真正把通用版剔除的逻辑在这里：

[cmake/Modules/CompilerRTUtils.cmake:484-504](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/cmake/Modules/CompilerRTUtils.cmake#L484-L504)：遍历每个架构专属文件，取其文件名（如 `udivdi3.S` → `udivdi3.c`），再加上它声明的 `crt_supersedes` 属性，只要这些通用 `.c` 文件存在，就从清单里 `REMOVE_ITEM` 删掉，并打印一条 `preferring <arch文件> to <通用.c>` 的提示。

它在 CMakeLists 里被这样调用：

[lib/builtins/CMakeLists.txt:1140](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/CMakeLists.txt#L1140)：`filter_builtin_sources(${arch}_SOURCES ${arch})`——为每个架构处理一遍。

例如 ARM 的 `arm/adddf3.S` 通过 `set_special_properties` 标注了 `SUPERSEDES subdf3.c`（见 CMakeLists 第 588-592 行附近），意味着这份汇编同时实现了加法和减法，所以连 `subdf3.c` 也要一并从该架构清单里删掉。

**（5）为每个架构编出独立的库**

[lib/builtins/CMakeLists.txt:1099-1173](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/CMakeLists.txt#L1099-L1173)：`foreach(arch ${BUILTIN_SUPPORTED_ARCH})` 循环对每个被支持的架构，调用 `add_compiler_rt_runtime` 编出一个静态库。关键调用在第 1155-1164 行：

[lib/builtins/CMakeLists.txt:1155-1164](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/CMakeLists.txt#L1155-L1164)：用该架构专属的 `${${arch}_SOURCES}` 清单，生成名为 `clang_rt.builtins` 的静态库（实际产物带架构后缀，如 `clang_rt.builtins-x86_64.a`）。

**（6）架构优化版长什么样——`i386/udivdi3.S`**

[lib/builtins/i386/udivdi3.S:7-18](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/i386/udivdi3.S#L7-L18)：注释直白地说明，这是「专针对 32 位 x86」的实现，因为 x86_64 上 64 位除法能直接用硬件完成；它的性能目标是「每次除法约 40 个周期，比在 x87 浮点单元上模拟整数除法更快」。

这正是「架构子目录」存在的意义：在通用 C 实现不够快的关键运算上，用手写汇编榨取硬件能力（这里用的是 `divl` 指令 + 位拼接，把 64 位除法拆成几次 32 位硬件除法）。

#### 4.3.4 代码实践

**实践目标**：验证「通用版被架构版覆盖」这件事，在 CMake 配置阶段真实可见。

**操作步骤**：

1. 配置一次 compiler-rt 的 builtins 构建（参考 [u1-l3](u1-l3-build-system.md) 的构建方式），例如只构建 builtins：

   ```bash
   cmake -B build -S . \
         -DCOMPILER_RT_BUILD_BUILTINS=ON \
         -DCOMPILER_RT_BUILD_SANITIZERS=OFF
   ```

2. 在 CMake 输出日志里搜索「preferring」字样（待本地验证，需要相应架构被启用）：

   ```bash
   cmake --build build --target builtins 2>&1 | grep -i "preferring"
   # 或在配置阶段：
   grep -i "preferring" build/CMakeFiles/CMakeOutput.log
   ```

**需要观察的现象 / 预期结果**（待本地验证）：若启用了 i386 之类的架构，应能看到类似 `For i386 builtins preferring i386/udivdi3.S to udivdi3.c` 的提示行。这正好对应 `filter_builtin_sources` 在第 497 行打印的那条信息，是「架构版覆盖通用版」的实证。

如果当前环境只构建了 x86_64（没有覆盖发生），则不会有这些提示——这本身也说明 x86_64 走的是通用实现。

#### 4.3.5 小练习与答案

**练习 1**：为什么 builtins 要坚持「一函数一文件」？把相关函数合到一个文件里不是更整洁吗？

**参考答案**：为了让构建系统能精确地「点菜」。不同架构需要的函数子集不同，覆盖关系也不同（某个架构可能只覆盖 `udivdi3` 不覆盖 `divdi3`）。一函数一文件让 CMake 能在源文件粒度上做增删（`list(REMOVE_ITEM)`、`list(APPEND)`），实现精细的按架构裁剪。合并文件会让裁剪失去最小粒度。

**练习 2**：假设你要给某架构写一个 `mydivdi3.S` 来替换通用 `divdi3.c`，把它放进 `i386/` 目录就够了么？还需要做什么？

**参考答案**：不够。还需要在 CMakeLists 的该架构源文件清单（如 `i386_SOURCES`）里追加 `i386/mydivdi3.S`。只要文件名与通用版同名（`divdi3`），`filter_builtin_sources` 会自动把通用的 `divdi3.c` 从该架构清单里删掉；如果你的汇编一个文件覆盖了多个通用函数，还要用 `set_special_properties(... SUPERSEDES ...)` 额外声明。

**练习 3**：x86_64 为什么不在 `x86_64_SOURCES` 里追加 `udivdi3.S`？

**参考答案**：因为 x86_64 有原生的 64 位除法指令（`div`/`idiv`），通用的 `udivdi3.c`（纯 C）经编译后已经能用上硬件除法，性能足够；没必要再手写汇编。`i386/udivdi3.S` 之所以存在，是因为 32 位 x86 没有 64 位除法指令，必须用汇编拆分成多次 32 位 `divl` 才能达到性能目标。「是否需要架构优化版」取决于该运算在该架构上有没有高效的硬件指令。

---

## 5. 综合实践

设计一个把本讲三块内容（替代关系、类型基础、通用 vs 架构）串起来的小任务。

**任务**：从「一个 64 位除法」出发，走通「编译器调用 → 通用实现 → 架构覆盖」的完整链路。

**操作步骤**：

1. **写测试程序** `divtest.c`（示例代码，非项目原有代码）：

   ```c
   // 示例代码
   #include <stdint.h>
   volatile uint64_t a = 1000000000000ULL;
   volatile uint64_t b = 7ULL;
   int main(void) { return (int)(a / b); }
   ```

2. **看编译器是否生成调用**。分别针对 32 位和 64 位 x86 生成汇编：

   ```bash
   clang -target i386-linux-gnu -O1 -S divtest.c -o div_32.s
   clang -target x86_64-linux-gnu -O1 -S divtest.c -o div_64.s
   grep -nE "__udivdi3|__divdi3" div_32.s div_64.s   # 待本地验证
   ```

   预期：32 位版本出现 `__udivdi3` 调用，64 位版本不出现（待本地验证）。

3. **找到通用实现**：在源码树定位 `lib/builtins/udivdi3.c`（或它委托的 `udivmoddi4.c`），确认它包含 `int_lib.h`、用 `du_int`/`udwords` 这类类型。

4. **找到架构优化实现**：定位 `lib/builtins/i386/udivdi3.S`，阅读其顶部注释（第 7-18 行），理解它为什么只针对 32 位 x86。

5. **画出链路图**（文字版即可）：

   ```text
   64位除法(无硬件指令时)
        │  编译器生成
        ▼
   call __udivdi3  ──(符号名来自 libgcc ABI 约定)──  builtins 必须提供同名符号
        │
        ├─ 通用实现: udivdi3.c (纯C, 用 du_int/udwords 拆字, 全架构兜底)
        └─ i386 优化: i386/udivdi3.S (汇编, 用 divl 指令)
                        └─ filter_builtin_sources 把通用 udivdi3.c 从 i386 清单剔除
   ```

6. **反思**：用一段话回答——「如果某个全新架构既没有 64 位除法指令、也没有人写汇编优化版，builtins 还能让它正确工作吗？」（答案：能，因为通用 `udivdi3.c` 是兜底实现，会自动被该架构使用。这正是「通用 + 架构」并存设计的价值。）

> 提示：本任务中若没有 32 位目标的交叉编译环境，第 2 步的运行结果标注为「待本地验证」即可，重点是理解链路而非一定要跑出结果。

## 6. 本讲小结

- builtins 是 GCC `libgcc` 的对应物，专门提供「编译器在生成机器码时插入、但目标 CPU 没有对应单条指令」的帮手函数（如 32 位机上的 64 位除法、无 FPU 机器上的浮点运算）。
- 它**必须**沿用 libgcc 的函数命名，因为这些符号名属于 ABI，编译器生成的调用指令里写死了这些名字。
- 函数名有规律：`__<操作><宽度/类型>[目标类型][数字]`，如 `__divdi3`=64 位有符号除法、`__fixdfdi`=double→int64。
- `int_lib.h` 是每个实现都包含的配置头（含跨编译器宏、三条公共假设）；`int_types.h` 定义统一的 `si_int/di_int/ti_int` 类型和 `dwords` 等「拆字」union，让一份纯 C 代码能在 32 位机器上操作 64/128 位整数。
- 工程上「一函数一文件」；`GENERIC_SOURCES` 是可移植兜底实现，各架构子目录放汇编优化版；`filter_builtin_sources` 负责让架构版本覆盖同名通用版本。
- CMake 为每个被支持的架构各编一个 `clang_rt.builtins-<arch>` 静态库，源文件清单 = 通用基线 + 该架构独有 − 被覆盖的通用文件。

## 7. 下一步学习建议

本讲建立了 builtins 的全局心智模型。接下来建议：

- **u2-l2（整数算术与位操作内建）**：挑 `__divdi3`/`__udivdi3`/`__clzdi2` 等函数，深入读它们如何用 32 位运算组合出 64 位结果。本讲已经读了 `clzdi2.c` 作为热身，u2-l2 会把它讲透。
- **u2-l3（软浮点内建）**：进入浮点世界，看 `__addsf3`/`__fixdfdi` 如何用整数运算模拟 IEEE 754 浮点运算。
- **u2-l4（架构相关内建与 cpu_model）**：系统了解各架构子目录里的优化实现（如 `chkstk`、ARM 的 `lse` 原子）以及 `cpu_model` 如何检测 CPU 特性——这是本讲「架构子目录」话题的延续。

如果想立刻动手，可以先用本讲「综合实践」里的链路图去 `lib/builtins/` 里多翻几个文件，感受一下「一函数一文件」的极致拆分风格，再带着具体问题进入 u2-l2。
