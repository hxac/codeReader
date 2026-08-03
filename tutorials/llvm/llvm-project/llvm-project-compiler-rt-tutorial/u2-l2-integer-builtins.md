# 整数算术与位操作内建函数

## 1. 本讲目标

本讲承接 [u2-l1 builtins 概览](u2-l1-builtins-overview.md)，从「名字与类型」深入到「算法实现」。

读完本讲，你应该能够：

- 说出在「没有 64 位除法/乘法/移位/位计数单条指令」的 CPU 上，编译器插入的 `__udivdi3`、`__muldi3`、`__ashldi3`、`__clzdi2` 分别是怎么用 32 位运算「拼」出 64 位结果的。
- 看懂「二进制长除法（shift-and-subtract）」「字分解乘法」「分支无跳（branchless）减法」「二分式位计数」这几类经典位运算技巧在真实源码里的写法。
- 学会借助项目自带的单元测试（如 `divdi3_test.c`）去阅读和验证一个内置函数的正确性，并能动手写一个对照验证小程序。

本讲不要求你熟悉汇编或具体某个架构，只需要对 C 语言的位运算、整数表示有基本了解。

## 2. 前置知识

### 2.1 为什么要软件实现这些运算？

现代 x86-64 / ARM64 CPU 大多有 64 位整数除法、乘法、移位指令，也有前导零计数（如 x86 的 `lzcnt`/`bsr`、ARM 的 `clz`）。但 builtins 库要服务大量目标，其中不乏：

- 32 位 CPU（如 32 位 ARM、RISC-V32、i386）：硬件寄存器只有 32 位，做 64 位运算需要多条指令或干脆没有对应指令。
- 没有 FPU/除法器的精简核心。
- 任何架构上，编译器遇到「目标指令不支持」的操作时，会生成一次库函数调用。

于是 builtins 用**纯 C 写出可移植的兜底实现**，再由各架构子目录用汇编覆盖优化（见 u2-l1）。本讲解读的就是这些「通用兜底」实现。

### 2.2 把 64 位拆成两个 32 位「字」

这是贯穿全讲的唯一技巧。`int_types.h` 里用一个联合体（union）把一个 64 位整数同时当作「整体」和「高位字 + 低位字」两种视角：

[lib/builtins/int_types.h:40-51](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/int_types.h#L40-L51) 定义了 `dwords`，它把 `di_int`（64 位有符号）拆成 `s.low`（低 32 位无符号）和 `s.high`（高 32 位有符号）；[lib/builtins/int_types.h:53-64](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/int_types.h#L53-L64) 定义了纯无符号版本 `udwords`。大端/小端由 `_YUGA_LITTLE_ENDIAN` 自动切换字段顺序，所以同一份代码两种字节序都正确。

```c
typedef union {
  di_int all;            // 64 位整体视角
  struct { su_int low; si_int high; } s;  // 高低两「字」视角
} dwords;
```

有了它，对 64 位数 `x` 的高/低 32 位操作就是 `x.s.high` / `x.s.low`，写起来像在操作「数组里的两个元素」。

### 2.3 clz 是什么，为什么到处用它

`clz(x)` = count leading zeros，统计一个数从最高位起有多少个连续的 0。例如 32 位的 `0x00010000`，clz = 15。它最大的用途是**快速估计一个数的「有效位数」**：一个非零 `x` 的有效位数约为 \(N - \text{clz}(x)\)（\(N\) 为位宽）。长除法算法正是用「被除数与除数的 clz 之差」来判断商大约有多少位、需要循环多少次。

[lib/builtins/int_types.h:27-35](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/int_types.h#L27-L35) 把 32 位 clz 抽象成宏 `clzsi`，在 `int` 为 32 位的平台上指向 `__builtin_clz`，在 `long` 为 32 位的平台上指向 `__builtin_clzl`。这样算法代码统一写 `clzsi(x)` 即可。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [lib/builtins/divdi3.c](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/divdi3.c) | 64 位**有符号**除法 `__divdi3`，仅做符号处理，核心转发给 `__udivmoddi4` |
| [lib/builtins/udivdi3.c](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/udivdi3.c) | 64 位**无符号**除法 `__udivdi3`，转发给内联的 `__udivXi3` |
| [lib/builtins/int_div_impl.inc](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/int_div_impl.inc) | 除法算法核心：无符号 `__udivXi3`/`__umodXi3` 与有符号 `__divXi3`（被多个 `.c` 用宏参数复用） |
| [lib/builtins/udivmoddi4.c](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/udivmoddi4.c) | 64 位无符号「除 + 取余」一体实现，**显式按 32 位字分解**，不依赖 64 位移位 |
| [lib/builtins/muldi3.c](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/muldi3.c) | 64 位乘法 `__muldi3`，用 16 位分解实现 32×32→64 再组合 |
| [lib/builtins/ashldi3.c](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/ashldi3.c) | 64 位算术左移 `__ashldi3` |
| [lib/builtins/lshrdi3.c](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/lshrdi3.c) | 64 位逻辑右移 `__lshrdi3`（左移的镜像，帮助对照理解） |
| [lib/builtins/clzdi2.c](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/clzdi2.c) | 64 位前导零计数 `__clzdi2`，分支无跳地选择高/低字 |
| [lib/builtins/clzsi2.c](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/clzsi2.c) | 32 位前导零计数 `__clzsi2`，二分式实现，是 `__clzdi2` 的底层 |
| [test/builtins/Unit/divdi3_test.c](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/builtins/Unit/divdi3_test.c)、[clzdi2_test.c](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/builtins/Unit/clzdi2_test.c) | 官方单元测试，示范如何验证这些函数 |

一条贯穿线索：所有 64 位运算都靠「拆成两个 32 位字」+「用 clz 估计规模」这两招完成。

---

## 4. 核心概念与源码讲解

### 4.1 64 位除法：从 `__udivdi3` 到二进制长除法

#### 4.1.1 概念说明

除法是本讲最复杂的运算。CPU 通常用一个「移位再减」（shift-and-subtract）的迭代算法做除法，软件也可以照搬——这就是**二进制长除法**：和十进制竖式除法完全类比，只是「每一位」是二进制位，每一位商不是 0 就是 1。

直觉上，计算 \(n / d\)（\(n\) 被除数、\(d\) 除数，均为无符号）：

1. 维护一个「部分余数」\(r\)，初始为 0。
2. 从 \(n\) 的最高位开始，一位一位地「移入」\(r\)。
3. 每移入一位后，比较 \(r\) 与 \(d\)：若 \(r \ge d\)，则 \(r \leftarrow r - d\)，并在商里「写下 1」；否则「写下 0」，\(r\) 不变。
4. 处理完 \(n\) 的所有位，商就凑齐了，剩下的 \(r\) 就是余数。

这和手算竖式除法一模一样，只是进制从 10 换成 2。关键优化是：先用 clz 算出「商大约有几位」，从而**只循环必要的次数**，而不是死板地循环 64 次。

#### 4.1.2 核心流程

`__udivdi3` 的入口非常薄：[lib/builtins/udivdi3.c:21-23](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/udivdi3.c#L21-L23) 只是把两个 `du_int` 参数交给 `__udivXi3`。真正的算法在 [lib/builtins/int_div_impl.inc:16-42](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/int_div_impl.inc#L16-L42) 的 `__udivXi3`（这段代码改编自《PowerPC Compiler Writer's Guide》Figure 3-40，注释里写明了出处）。流程：

```text
输入 n（被除数）, d（除数），位宽 N=64
1. sr = clz(d) - clz(n)        # 估计商的位数 - 1
2. 若 sr > N-1：n < d，商 = 0，直接返回
3. 若 sr == N-1：d == 1，商 = n，直接返回
4. sr += 1                     # 现在循环次数 = 商的位数
5. 预热：r = n >> sr；把 n 左移，使要处理的位对齐到最高位
6. 循环 sr 次：
     a. r 左移 1 位，并从 n 的最高位「移入」一个比特到 r
     b. 若 r >= d：r -= d，记 carry=1（这一位商为 1）
              否则        记 carry=0
     c. carry 被移入「正在组装的商」
7. 最后把 carry 拼入商，返回
```

步骤 6b 是核心。源码用一个**分支无跳（branchless）**技巧来同时完成「比较 + 条件减法」，避免 `if` 引起的跳转（在无分支预测器的精简 CPU 上更稳，也更难被攻击者用作侧信道）：

```c
const fixint_t s = (fixint_t)(d - r - 1) >> (N - 1);
carry = s & 1;
r -= d & s;
```

原理：当 \(r \ge d\) 时，\(d - r - 1 < 0\)，转成有符号数右移 \(N-1\) 位（算术右移）后 `s` 全是 1（即 `-1`）；于是 `carry = 1`，`r -= d & (-1) = d`。当 \(r < d\) 时 \(d - r - 1 \ge 0\)，`s = 0`，什么都不减。用数学语言：

\[
s = \begin{cases} -1 & \text{若 } r \ge d \\ 0 & \text{若 } r < d \end{cases}
\]

> 数学注记：这里利用了补码下「算术右移 \(N-1\) 位 = 把符号位铺满全部位」的性质。`d & s` 因此要么等于 `d`，要么等于 0，把一个 `if` 变成了一次按位与。

#### 4.1.3 源码精读

`__udivdi3` 本体（[lib/builtins/udivdi3.c:15-23](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/udivdi3.c#L15-L23)）：先用 `typedef` 把宏 `fixuint_t` 设为 `du_int`、`fixint_t` 设为 `di_int`，再 `#include "int_div_impl.inc"`，于是那个 `.inc` 里的模板函数 `__udivXi3` 就被「实例化」成 64 位无符号版本，最后 `__udivdi3` 一行调用它。

无符号除法核心（[lib/builtins/int_div_impl.inc:16-42](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/int_div_impl.inc#L16-L42)）：注意第 19 行用 `clz` 之差算 `sr`，第 21–24 行处理两种特例（被除数小于除数、除数为 1），第 30–39 行是移位-比较-减法主循环，第 36–38 行就是上面解释的分支无跳减法。

**有符号除法走的是另一条路**。`__divdi3`（[lib/builtins/divdi3.c:17-22](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/divdi3.c#L17-L22)）第 19 行定义了宏 `COMPUTE_UDIV(a,b) → __udivmoddi4(a,b,0)`，所以它不是调用内联的 `__udivXi3`，而是调用**单独编译**的 `__udivmoddi4`。这个区别值得记住：

| 函数 | 实际调用 | 实现 |
| --- | --- | --- |
| `__udivdi3` | 内联 `__udivXi3` | `int_div_impl.inc`，紧凑版，对整个 64 位整数操作 |
| `__divdi3` | `__udivmoddi4` | `udivmoddi4.c`，**按 32 位字显式分解**，同时给出余数 |

有符号除法本身只是一层薄薄的符号包装：[lib/builtins/int_div_impl.inc:73-81](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/int_div_impl.inc#L73-L81) 的 `__divXi3` 先抽出两数的符号位（`a >> (N-1)` 得到全 0 或全 -1 的掩码），用 `(a ^ s_a) + (-s_a)` 取绝对值，做无符号除法，再按「商的符号」决定是否把结果取反。取绝对值的技巧：

\[
\text{若 } a \ge 0,\ s_a = 0:\ (a \oplus 0) + 0 = a;\qquad
\text{若 } a < 0,\ s_a = -1:\ (a \oplus (-1)) + 1 = (\sim a) + 1 = -a
\]

至于「按字分解」的 `__udivmoddi4`，它的主体（[lib/builtins/udivmoddi4.c:27-196](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/udivmoddi4.c#L27-L196)）前半段是一大堆特例判断（被除数/除数的高位字是否为 0、除数是否为 2 的幂等，目的是走快路），后半段 [lib/builtins/udivmoddi4.c:175-191](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/udivmoddi4.c#L175-L191) 是和 `__udivXi3` 完全同构的移位-减法循环，只不过这里把 64 位的 `r` 和 `q` 拆成 `r.s.high/r.s.low` 两个 32 位字，手工完成「跨字移位」（第 178–181 行那一串 `<< 1 | >> 31` 就是在两字之间搬运进位）。这版不依赖任何 64 位移位指令，因此能在最简的 32 位目标上运行。

#### 4.1.4 代码实践

**目标**：亲手验证 `__udivdi3` 与编译器原生 `/` 运算在随机输入上结果一致。

**操作步骤**（示例代码，非项目原有代码）：

```c
// verify_udiv.c —— 示例代码：对照验证 __udivdi3
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

typedef int32_t si_int;
typedef uint32_t su_int;
typedef int64_t di_int;
typedef uint64_t du_int;

// 声明 builtins 里的函数（链接时由 libclang_rt.builtins 提供）
extern du_int __udivdi3(du_int a, du_int b);

int main(void) {
    // 简单伪随机：用 LCG，避免依赖系统随机源
    du_int state = 0x123456789abcdef0ULL;
    int bad = 0;
    for (int i = 0; i < 1000000; ++i) {
        state = state * 6364136223846793005ULL + 1442695040888963407ULL;
        du_int a = state;
        state = state * 6364136223846793005ULL + 1442695040888963407ULL;
        du_int b = (state & 0xffffffffffffffffULL) | 1ULL; // 避免 b==0（除零未定义）

        du_int got = __udivdi3(a, b);
        du_int want = a / b;          // 原生除法
        if (got != want) {
            printf("MISMATCH: %llu / %llu = got %llu, want %llu\n",
                   (unsigned long long)a, (unsigned long long)b,
                   (unsigned long long)got, (unsigned long long)want);
            bad++;
        }
    }
    printf("%s\n", bad == 0 ? "ALL OK" : "FAILED");
    return bad != 0;
}
```

编译并链接 builtins 库（路径依你本机的 Clang 资源目录而定，参考 u1-l3/u1-l4）：

```bash
clang verify_udiv.c -o verify_udiv \
  -lclang_rt.builtins-<arch>   # 或直接指明 libclang_rt.builtins.a 的完整路径
./verify_udiv
```

**需要观察的现象**：程序打印 `ALL OK`，说明 `__udivdi3` 与原生除法在 100 万组随机输入上完全一致。

**预期结果**：`ALL OK`。

> 待本地验证：本机若为 64 位 CPU，`a / b` 会被编译成硬件除法指令，**不会**调用 `__udivdi3`；而我们 `extern` 声明后**显式**调用它，所以两者走的是不同实现路径，对照才有意义。若想观察「编译器自动插入调用」的现象，可用 `clang -target i386-...` 之类的 32 位目标编译一个含 `uint64_t` 除法的小程序，再用 `objdump -d` 或 `llvm-objdump -d` 查看反汇编里是否出现 `call __udivdi3`。

如果想跑项目自带测试，可参照 [test/builtins/Unit/divdi3_test.c:1](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/builtins/Unit/divdi3_test.c#L1) 的 `RUN` 行（`%clang_builtins %s %librt -o %t && %run %t`），在 lit 环境里执行 `check-compiler-rt-builtins`。

#### 4.1.5 小练习与答案

**练习 1**：在 `__udivXi3` 中，为什么 `sr > N-1` 就能断定「商为 0」？

**参考答案**：`sr = clz(d) - clz(n)`。`clz(d) - clz(n) > N-1` 意味着 `d` 的最高有效位比 `n` 的最高有效位靠左超过 `N-1` 位，即 `d` 比 `n`「长」得多，等价于 `n < d`，所以商为 0、余数为 `n`。

**练习 2**：把 `__divXi3` 取绝对值的写法 `(a ^ s_a) + (-s_a)` 换成普通的 `a < 0 ? -a : a`，功能上是否等价？为什么源码偏要用位运算？

**参考答案**：功能等价。源码用位运算是为了**完全无分支**——避免 `if`/三元运算可能生成的条件跳转，既适配无分支预测的精简 CPU，也避免潜在的时序侧信道。此外这种「掩码 XOR + 加」是补码取负的标准无分支惯用法，统一了「取绝对值」和「恢复符号」两步的实现。

---

### 4.2 乘法：`__muldi3` 的 16 位分解

#### 4.2.1 概念说明

32 位 CPU 通常有 32×32→32 的乘法指令，但没有 64×64→64。要做 64 位乘法，最稳的办法是**回到小学竖式**：把每个 64 位数拆成两个 32 位「半字」，再像十进制那样分块相乘、错位相加。更进一步，32×32 的乘法本身也可以拆成 16×16 来保证只用到「半字乘半字→字」的硬件能力。

设两个 64 位无符号数：

\[
a = a_H \cdot 2^{32} + a_L,\qquad b = b_H \cdot 2^{32} + b_L
\]

其中 \(a_H, a_L, b_H, b_L\) 都是 32 位。那么：

\[
a \cdot b = \underbrace{a_H b_H}_{\text{溢出 }64\text{ 位，丢弃}} \cdot 2^{64} + (a_H b_L + a_L b_H)\cdot 2^{32} + a_L b_L
\]

`__muldi3` 只要低 64 位结果，所以最左边的 \(a_H b_H \cdot 2^{64}\) 直接丢弃，只需算 \(a_L b_L\)（一次 32×32→64）再加上两个交叉项左移 32 位。

#### 4.2.2 核心流程

源码分两层：

1. `__muldsi3`（[lib/builtins/muldi3.c:17-34](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/muldi3.c#L17-L34)）：实现 32 位 × 32 位 → 64 位。把每个 32 位操作数再拆成两个 16 位半字（`lower_mask` 取低 16 位），用四次「16×16→32」乘法和移位累加拼出 64 位积。
2. `__muldi3`（[lib/builtins/muldi3.c:38-47](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/muldi3.c#L38-L47)）：先把两个 64 位入参塞进 `dwords` 联合体，调用 `__muldsi3(x.s.low, y.s.low)` 得到 \(a_L b_L\)（64 位），再加上交叉项 `x.s.high * y.s.low + x.s.low * y.s.high` 左移 32 位的作用（这里通过 `r.s.high +=` 直接累加到高字）。

```text
__muldi3(a, b):
  x.all = a;  y.all = b
  r.all = __muldsi3(x.low, y.low)        # = aL*bL，占满 r.low 与部分 r.high
  r.high += x.high * y.low + x.low * y.high   # 交叉项落在第 32 位之上
  return r.all
```

注意 `x.high * y.low` 这种「32 位乘 32 位」在 32 位平台上的结果会被截断到 32 位——但没关系，因为它们要左移 32 位进入积的高字，超过 32 位的部分本就该丢掉（贡献给已丢弃的 \(a_H b_H 2^{64}\) 项）。

#### 4.2.3 源码精读

[lib/builtins/muldi3.c:17-34](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/muldi3.c#L17-L34) 的 `__muldsi3` 是经典的「拆半字」乘法。第 19–20 行算出半字位数（16）和低 16 位掩码；第 21 行先算低半字 × 低半字；第 22–25 行处理低×高、把进位搬到高半字；第 27–31 行处理高×低；第 32 行补上高×高。每一小步都用 `>> bits_in_word_2` 和 `& lower_mask` 在 16 位与 32 位之间搬运数据，确保中间结果不溢出 32 位。

[lib/builtins/muldi3.c:38-47](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/muldi3.c#L38-L47) 的 `__muldi3` 极其简短：第 44 行的 `__muldsi3(x.s.low, y.s.low)` 负责低 64 位的主体，第 45 行的 `r.s.high +=` 把交叉项叠到高字。

末尾 [lib/builtins/muldi3.c:49-51](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/muldi3.c#L49-L51) 用 `COMPILER_RT_ALIAS(__muldi3, __aeabi_lmul)` 给 ARM EABI 提供了别名 `__aeabi_lmul`——这就是 u2-l1 提到的「同一函数多个 ABI 名字」的实例。

#### 4.2.4 代码实践

**目标**：用纸笔走一遍 `__muldi3`，再用一个小程序验证交叉项的正确性。

**操作步骤**（示例代码）：

```c
// verify_mul.c —— 示例代码：验证 __muldi3
#include <stdint.h>
#include <stdio.h>
typedef int64_t di_int;
extern di_int __muldi3(di_int a, di_int b);
int main(void) {
    // 选两个高字、低字都不为 0 的数，确保交叉项被触发
    di_int a = (di_int)0x0000000200000003LL;
    di_int b = (di_int)0x0000000500000007LL;
    di_int got = __muldi3(a, b);
    di_int want = a * b;
    printf("got  = 0x%llx\nwant= 0x%llx\n%s\n",
           (unsigned long long)got, (unsigned long long)want,
           got == want ? "OK" : "MISMATCH");
    return got != want;
}
```

**需要观察的现象**：`got` 与 `want` 相等。

**预期结果**：两者都是 `0xA00000011LL` 附近的值（具体按上面常数算：\(2\cdot5\cdot2^{64}\) 丢弃，剩下 \((2\cdot7+3\cdot5)\cdot2^{32}+3\cdot7\)），打印 `OK`。

**手动验证（源码阅读型实践）**：对照 [lib/builtins/muldi3.c:44-45](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/muldi3.c#L44-L45)，写下：\(a_L=3, a_H=2, b_L=7, b_H=5\)，于是 `__muldsi3(3,7)=21`，交叉项 \(a_H b_L + a_L b_H = 2\cdot7+3\cdot5=29\)，结果 = \(21 + 29\cdot2^{32}\)。把十六进制写出来，与程序输出对齐。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `__muldi3` 里可以直接丢弃 \(a_H b_H\cdot 2^{64}\) 这一项？

**参考答案**：函数返回 64 位结果，而该项最低位在第 64 位，完全落在结果之外，属于「自然的 64 位溢出」，C 语言无符号乘法本就定义为对 \(2^{64}\) 取模，所以丢弃它正好等价于取低 64 位。

**练习 2**：`__muldsi3` 为什么要把 32 位数拆成 16 位半字，而不是直接写 `(su_int)a * (su_int)b`？

**参考答案**：在 32 位平台上 `su_int` 相乘默认产生 32 位结果（高位被截断），拿不到 64 位积。拆成 16×16→32 后，每个部分积都装得进一个 32 位字，再用移位累加就能可靠地拼出 64 位结果——这正是「软件实现」的意义所在。在 64 位平台上编译器会把这套运算优化成更少的指令，但语义不变。

---

### 4.3 移位：`__ashldi3` 的跨字搬运

#### 4.3.1 概念说明

左移 `a << b` 在 32 位 CPU 上对 64 位数也要软件实现。难点只有一个：**当移位量超过 32，低位字的内容要「搬」到高位字去**。把 64 位数看成 `[high : low]` 两个 32 位字：

- 若 `b >= 32`：低位字整个搬到高位字的低 `b-32` 位，新的低位字清零。
- 若 `b < 32`：低位字的高 `b` 位溢出到高位字，剩余部分留在低位字。即 `new_high = (high << b) | (low >> (32-b))`，`new_low = low << b`。

注意那个 `low >> (32-b)`——它正是「从低字溢出到高字」的那 `b` 个比特。

#### 4.3.2 核心流程

[lib/builtins/ashldi3.c:19-35](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/ashldi3.c#L19-L35) 正是按上面两段逻辑写的，并用 `if (b & bits_in_word)` 来判断 `b` 是否 ≥32（`bits_in_word` = 32，`b & 32` 在 `b>=32` 时非零）。它还有一个贴心特例：`b == 0` 时直接返回原值（第 28–29 行），避免 `low >> (32-0)` 这种「移位量等于位宽」的未定义行为。

逻辑右移 `__lshrdi3`（[lib/builtins/lshrdi3.c:19-34](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/lshrdi3.c#L19-L34)）是镜像：方向反过来，`b>=32` 时高字搬向低字、高字清零；`b<32` 时 `new_low = (high << (32-b)) | (low >> b)`。两者放在一起对照阅读，能很好地理解「跨字搬运」这一通用模式。

#### 4.3.3 源码精读

跨字分支（[lib/builtins/ashldi3.c:24-26](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/ashldi3.c#L24-L26)）：

```c
if (b & bits_in_word) {           // 32 <= b < 64
  result.s.low = 0;
  result.s.high = input.s.low << (b - bits_in_word);
}
```

字内分支（[lib/builtins/ashldi3.c:30-32](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/ashldi3.c#L30-L32)）：

```c
result.s.low  = input.s.low << b;
result.s.high = ((su_int)input.s.high << b) | (input.s.low >> (bits_in_word - b));
```

注意 `input.s.high` 被强转成 `su_int`（无符号）再左移，避免有符号左移的未定义行为——这是 builtins 代码里反复出现的安全习惯。函数前置条件 `0 <= b < bits_in_dword`（第 17 行注释）由编译器保证：编译器只在移位量合法时才会生成对 `__ashldi3` 的调用。同样，末尾 [lib/builtins/ashldi3.c:37-39](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/ashldi3.c#L37-L39) 也提供了 ARM EABI 别名 `__aeabi_llsl`。

#### 4.3.4 代码实践

**目标**：手算一个跨字左移，再用程序核对。

设 `a = 0x00000001FFFFFFFF`，`b = 4`。手算：

- `low = 0xFFFFFFFF`，`high = 0x00000001`。
- `new_low = 0xFFFFFFFF << 4 = 0xFFFFFFF0`。
- `new_high = (1 << 4) | (0xFFFFFFFF >> 28) = 0x10 | 0xF = 0x1F`。
- 结果 `0x0000001FFFFFF0`。

**操作步骤**（示例代码）：

```c
#include <stdint.h>
#include <stdio.h>
typedef int64_t di_int;
extern di_int __ashldi3(di_int a, int b);
int main(void) {
    di_int a = 0x00000001FFFFFFFFLL;
    printf("0x%llx\n", (unsigned long long)__ashldi3(a, 4));
    return 0;
}
```

**需要观察的现象**：输出 `0x1fffffff0`，与上面手算一致。

**预期结果**：`0x1fffffff0`。

**源码阅读型实践**：把 `b` 改成 `33`，对照 [lib/builtins/ashldi3.c:24-26](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/ashldi3.c#L24-L26) 预测：此时走跨字分支，`new_low=0`，`new_high = low << (33-32) = low << 1`。验证程序输出是否吻合。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `__ashldi3` 在 `b==0` 时要单独返回，不直接走通用公式？

**参考答案**：因为通用公式里有 `low >> (bits_in_word - b)`，当 `b==0` 就变成 `low >> 32`，而对 32 位int 右移 32 位在 C 里是**未定义行为**。单独返回既避免 UB，又是一个常见的快路径优化。

**练习 2**：`__ashldi3`（算术左移）和 `__lshrdi3`（逻辑右移）为何没有单独的「算术右移」兄弟函数在这里？算术右移通常叫什么名字？

**参考答案**：算术右移有专门函数 `__ashrdi3.c`（本讲未展开，但与 `__lshrdi3` 几乎同构，只是高位用符号位填充而非 0）。左移不区分算术/逻辑（左侧统一补 0），所以 `__ashldi3` 一个就够；右移则要分「补 0」和「补符号位」两种，分别对应 `__lshrdi3` 和 `__ashrdi3`。

---

### 4.4 位计数：`__clzdi2` 与 `__clzsi2`

#### 4.4.1 概念说明

前导零计数（clz）除了解释过的「估商位数」用途，本身也是编译器常插入的调用（如 `__builtin_clz` 在没有硬件指令时会落到 `__clzsi2`/`__clzdi2`）。问题：怎么用 32 位运算算出一个 64 位数的 clz？

思路分两层：

1. **64 位 → 32 位**：64 位数的 clz = 「高字的 clz」；若高字为 0，则 = 「32 + 低字的 clz」。只需挑出那个「有效的字」再调用 32 位 clz。
2. **32 位 clz 本身**：用**二分查找**。先看高 16 位是否有 1，再看 8 位、4 位、2 位，逐级缩小范围，每步累计位数。这是无循环、纯算术的经典实现。

#### 4.4.2 核心流程

`__clzdi2`（[lib/builtins/clzdi2.c:29-35](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/clzdi2.c#L29-L35)）用一个掩码 `f = -(high == 0)` 无分支地完成「选字 + 加偏移」：

- 若 `high != 0`：`f = 0`，选 `high`，不加偏移。
- 若 `high == 0`：`f = -1`，选 `low`，并加 32。

```text
f = -(high == 0)                         # 全 0 或全 1 的掩码
sel = (high & ~f) | (low & f)            # 高字非0选高字，否则选低字
return clzsi(sel) + (f & 32)             # 选中低字时额外 +32
```

`__clzsi2`（[lib/builtins/clzsi2.c:19-48](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/clzsi2.c#L19-L48)）则是 5 步二分：依次检查高 16 位、高 8 位、高 4 位、高 2 位是否为 0，用 `t = ((x & mask) == 0) << shift` 产生 0 或 `shift` 的步进，并相应右移 `x`，把尚不确定的区间逐步压到最低 2 位，最后用一个无分支式子收尾。

#### 4.4.3 源码精读

`__clzdi2` 的选字（[lib/builtins/clzdi2.c:32-34](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/clzdi2.c#L32-L34)）：第 32 行 `f = -(x.s.high == 0)` 利用「布尔值取负」得到掩码；第 33 行 `(x.s.high & ~f) | (x.s.low & f)` 在两字间二选一；第 34 行 `(f & 32)` 仅在选低字时补 32。前置条件 `a != 0`（第 27 行注释）——对 0 求 clz 是未定义的（编译器也不会对 0 插入此调用）。

注意文件顶部的递归保护（[lib/builtins/clzdi2.c:17-25](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/clzdi2.c#L17-L25)）：在某些 64 位架构（sparc64、mips64、riscv64）上，`__builtin_clz` 在 32 位字上也会被编译成对 `__clzdi2` 的调用，从而陷入无限递归；这里通过 `#define __builtin_clz(a) __clzsi2(a)` 把它重定向到底层 `__clzsi2`，是 builtins 里非常典型的一种「绕开编译器自身 lowering 递归」的技巧。

`__clzsi2` 的二分（[lib/builtins/clzsi2.c:19-48](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/clzsi2.c#L19-L48)）：以第 21–23 行为例，`t = ((x & 0xFFFF0000) == 0) << 4` 在「高 16 位全 0」时得 16，否则 0；接着 `x >>= 16 - t` 把有效区间压到低 16 位；`r += t` 累计前导零。后面 8 位、4 位、2 位同理，最后第 47 行用 `((2 - x) & -((x & 2) == 0))` 对剩下 0~3 的小值收尾。

#### 4.4.4 代码实践

**目标**：用项目自带的 `clzdi2_test.c` 风格写一个验证，并对照 `__clzsi2` 的二分过程手算一例。

**操作步骤**：阅读 [test/builtins/Unit/clzdi2_test.c:13-19](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/test/builtins/Unit/clzdi2_test.c#L13-L19) 的 `test__clzdi2`，可以看到它就是「调用函数、比较期望值、不一致就打印」。仿照它（示例代码）：

```c
#include <stdint.h>
#include <stdio.h>
typedef int64_t di_int;
extern int __clzdi2(di_int a);
int main(void) {
    // 高字非 0：clz 只看高字
    printf("%d\n", __clzdi2(0x720005008000000ALL)); // 预期 1
    // 高字为 0：clz = 32 + 低字 clz
    printf("%d\n", __clzdi2(0x0000000100000000LL)); // 预期 32+31=63
    return 0;
}
```

**手算（源码阅读型实践）**：对 32 位值 `0x00010000`，按 [lib/builtins/clzsi2.c:19-48](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/clzsi2.c#L19-L48) 走：高 16 位 `0x0001` 非 0 ⇒ `t=0`，`x` 不变；高 8 位 `0x00` 为 0 ⇒ `t=8`，`x>>=8` 得 `0x0100`，`r=8`；高 4 位（`0x0`）为 0 ⇒ `t=4`，`x>>=4` 得 `0x10`，`r=12`；`0x10 & 0xF0` 非 0 ⇒ `t=0`；`0x10 & 0xC` 为 0 ⇒ `t=2`，`x>>=2` 得 `0x04`，`r=14`；收尾：`x=4`，返回 `r + ((2-4)&...)` = 15。与「`0x00010000` 的 clz = 15」一致。

**需要观察的现象**：上面程序输出 `1` 和 `63`；手算得到 15。

**预期结果**：程序输出与预期一致；手算 15 成立。若本地未链接 builtins 库，则作为纯源码阅读练习完成，标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：`__clzdi2` 中 `f = -(x.s.high == 0)`，这里的「负号」起什么作用？为什么不直接用 `if`？

**参考答案**：`(x.s.high == 0)` 是 0 或 1 的整数；取负后变成 0 或 `-1`（补码全 1），即一个「全 0 / 全 1」的位掩码。后续用 `& f` / `& ~f` 在高字与低字之间无分支二选一。避免 `if` 是为了无分支化，和前面除法、乘法里的思路一致。

**练习 2**：为什么文件顶部 [lib/builtins/clzdi2.c:17-25](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/clzdi2.c#L17-L25) 要在 sparc64/mips64/riscv64 上把 `__builtin_clz` 重定义为 `__clzsi2`？

**参考答案**：在这些 64 位架构上，编译器把 32 位的 `__builtin_clz` 也 lower 成调用 `__clzdi2`（而不是 `__clzsi2`），而 `__clzdi2` 内部又用 `clzsi`（最终展开为 `__builtin_clz`），于是会无限递归。把它重定向到自建的 `__clzsi2` 就切断了这条环。这说明 builtins 的实现必须时刻留意「编译器对内建函数的 lowering 方式」，有时需要主动规避自指。

---

## 5. 综合实践

把本讲的「字分解 + 无分支 + clz 估位」三招串起来，完成下面这个综合小任务。

**任务**：写一个极简的 64 位「无符号除法并打印」演示器，**只链接 builtins**，让它分别调用 `__udivdi3`、`__muldi3`、`__ashldi3`、`__clzdi2`，并把这些函数用于一个真实的小计算——例如实现「把一个 64 位整数除以一个 32 位常数，再把商左移若干位，统计结果的前导零」。

步骤建议：

1. 声明这四个函数为 `extern`（参考 4.1.4 和 4.4.4 的写法）。
2. 编写主流程：`q = __udivdi3(n, d);` → `p = __muldi3(q, k);` → `s = __ashldi3(p, sh);` → `z = __clzdi2(s);`，打印每一步结果。
3. 用编译器原生运算（`/`、`*`、`<<`、`__builtin_clzll`）做一份并行对照，断言两者完全相等。
4. 链接 `libclang_rt.builtins`，运行（若本地无法链接，则改为逐函数手算对照，并标注「待本地验证」）。
5. 用 `llvm-objdump -d` 查看可执行文件里确实引用了这四个符号（`nm` 或 `objdump -t` 亦可），体会「编译器插入的调用」与「我们显式调用」在符号表里的统一性。

**需要观察的现象**：每一步的 builtins 结果都与原生运算一致；符号表里能看到 `__udivdi3` 等符号。

**预期结果**：所有断言通过，打印 `ALL CONSISTENT`（或等价信息）。

---

## 6. 本讲小结

- builtins 的 64 位整数运算统一靠**把 64 位数拆成两个 32 位「字」**（`dwords`/`udwords` 联合体）完成，这是阅读 `lib/builtins` 任何文件的基础视角。
- **除法**用二进制长除法（shift-and-subtract）：`__udivdi3` 走内联 `__udivXi3`，`__divdi3` 走按字分解的 `__udivmoddi4`；两者都先用 clz 估商的位数，再用「分支无跳减法」逐位确定商。
- **乘法** `__muldi3` 用「拆 16 位半字」的 `__muldsi3` 实现 32×32→64，再用交叉项组合出 64×64→64 的低 64 位。
- **移位** `__ashldi3`/`__lshrdi3` 的核心是「跨字搬运」：移位量 ≥32 时整字搬家，<32 时用 `>>` 与 `<<` 配合搬运溢出比特，并注意规避「移位量等于位宽」的未定义行为。
- **位计数** `__clzdi2` 用掩码无分支地选高/低字并加偏移，底层 `__clzsi2` 用 5 步二分；文件顶部还展示了如何重定义 `__builtin_clz` 来切断编译器 lowering 的自指递归。
- 「分支无跳」（用算术右移、XOR、掩码代替 `if`）和「用 clz 估计规模」是贯穿整个 builtins 的两大反复出现的技巧。

## 7. 下一步学习建议

- 顺着本讲的「软件运算」主题，下一讲 [u2-l3 软浮点内建函数](u2-l3-soft-float-builtins.md) 会把同样的思路搬到 IEEE 754 浮点上（`__addsf3` 等），难度更进一层。
- 想看「带余数」的完整除法，可直接精读 [lib/builtins/udivmoddi4.c](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/udivmoddi4.c)，并对照 `__udivXi3` 体会两份实现的取舍。
- 想练习「按字分解」，可阅读 `__ashrdi3.c`、`__lshrdi3.c`、`__mulodi4.c`（带溢出检测的乘法）等同族文件，并在 `test/builtins/Unit/` 下找到对应的 `_test.c` 验证理解。
- 对「架构如何用汇编覆盖通用实现」感兴趣，可预习 [u2-l4 架构相关内建与 cpu_model](u2-l4-arch-specific-builtins.md)，看 x86_64/aarch64 等子目录如何提供更快的同类函数。
