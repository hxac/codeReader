# 架构相关内建与 cpu_model

## 1. 本讲目标

前三讲（u2-l1～u2-l3）我们读的都是「一份纯 C 代码，在所有架构上都能编译」的通用内建。本讲把视角抬高一档：compiler-rt 的 `lib/builtins` 并不只是「通用兜底实现」的集合，它还包含大量**只为某个 CPU 架构服务**的优化内建，以及一套**让程序在运行时知道当前 CPU 支持哪些特性**的探测机制。

读完本讲，你应当能够：

- 说清楚「通用实现」与「架构优化实现」为什么需要并存，以及它们各自解决什么问题。
- 读懂 `x86_64/chkstk.S`、`aarch64/chkstk.S` 这类**栈探测（stack probe）**例程，理解它为什么是汇编写的、为什么逐页触发。
- 读懂 `aarch64/lse.S` 这类**离线原子操作（out-of-line LSE atomics）**，理解它如何在「有 LSE」与「无 LSE」两条路径间运行时分发。
- 理解 `cpu_model/` 目录如何通过 CPUID（x86）或 HWCAP（AArch64/RISC-V）在程序启动早期把 CPU 特性填进 `__cpu_model` 等全局结构，并明白 `__builtin_cpu_supports` 与它的「生产者—消费者」协同关系。
- 掌握 CMake 是如何为每个目标架构挑出正确的源文件列表、并用同名覆盖规则让架构版替换通用版的。

## 2. 前置知识

### 2.1 回顾：builtins 的两层结构

[u2-l1](u2-l1-builtins-overview.md) 已经建立过这个心智模型：`lib/builtins` 顶层放的是 `GENERIC_SOURCES`——约 120 个**可移植的纯 C 兜底实现**，比如 `__divdi3.c`、`__clzdi2.c`。只要目标 CPU 没有对应的单条指令，编译器就会插入对这些函数的调用。

但「可移植」不等于「高效」。一台现代 x86_64 机器其实**有** 64 位除法指令、有原子指令；一台有 FPU 的 AArch64 也不需要软件浮点。更重要的是，有些帮手函数**根本无法用可移植的 C 写出来**——它们必须直接操作栈指针、必须发出某条特定指令、必须读取某个 CPU 寄存器。这类函数就只能用**汇编**实现，且只在特定架构上编译。

于是 builtins 自然分成两层：

| 层 | 位置 | 形态 | 例子 |
|---|---|---|---|
| 通用层 | `lib/builtins/*.c`（顶层） | 可移植 C | `__divdi3`、`__addsf3`、`__clzdi2` |
| 架构层 | `lib/builtins/<arch>/*.{c,S}` | C 或汇编，仅该架构编译 | `x86_64/chkstk.S`、`aarch64/lse.S` |

本讲的三个最小模块（架构优化内建、cpu_model 特性检测、构建系统按架构选择实现）正好对应这两层的「实践层」与「装配层」。

### 2.2 几个本讲会用到的术语

- **栈探测（stack probe）**：在分配大块栈空间前，按页「逐页触碰」即将使用的栈地址，确保一旦越界会撞上操作系统的**保护页（guard page）**，而不是直接跳过保护页。
- **LSE（Large System Extensions）**：ARMv8.1 引入的一组原子指令（如 `cas`、`ldadd`），比早期的 LL/SC（load-linked/store-conditional）循环更高效。
- **CPUID**：x86 的一条指令，软件用它向 CPU 询问「你是谁、支持哪些扩展」。
- **HWCAP / auxiliary vector**：Linux 内核在进程启动时，把当前 CPU 支持的硬件能力以位图形式放进一段「辅助向量」，用户态用 `getauxval(AT_HWCAP)` 读取。这是 AArch64/RISC-V 上等价于「问 CPU」的标准接口。
- **构造函数（constructor）**：用 `__attribute__((constructor))` 标注的函数，会在 `main` 之前由 C 运行时自动调用。cpu_model 正是靠它「抢跑」填充特性信息。

## 3. 本讲源码地图

本讲涉及的关键文件如下（均为实际存在的源码）：

| 文件 | 作用 |
|---|---|
| [lib/builtins/x86_64/chkstk.S](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/x86_64/chkstk.S) | x86_64 栈探测例程 `___chkstk_ms`（Windows/MinGW 路径） |
| [lib/builtins/aarch64/chkstk.S](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/aarch64/chkstk.S) | AArch64 栈探测例程 `__chkstk` |
| [lib/builtins/aarch64/lse.S](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/aarch64/lse.S) | AArch64 离线原子操作（LSE 与 LL/SC 回退两路） |
| [lib/builtins/cpu_model/cpu_model.h](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/cpu_model/cpu_model.h) | cpu_model 公共头：构造函数优先级、`CONSTRUCTOR_ATTRIBUTE` 宏 |
| [lib/builtins/cpu_model/x86.c](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/cpu_model/x86.c) | x86 特性检测：CPUID → `__cpu_model`，含 `__cpu_indicator_init` 构造函数 |
| [lib/builtins/cpu_model/aarch64.c](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/cpu_model/aarch64.c) | AArch64 特性检测：HWCAP → `__aarch64_cpu_features`、`__aarch64_have_lse_atomics` |
| [lib/builtins/cpu_model/aarch64.h](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/cpu_model/aarch64.h) | AArch64 cpu_model 内部头，引入 `AArch64CPUFeatures.inc` |
| [lib/builtins/cpu_model/aarch64/hwcap.inc](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/cpu_model/aarch64/hwcap.inc) | HWCAP 位定义（如 `HWCAP_ATOMICS`）与 `__init_cpu_features_constructor` |
| [lib/builtins/cpu_model/aarch64/lse_atomics/getauxval.inc](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/cpu_model/aarch64/lse_atomics/getauxval.inc) | Linux 上用 `getauxval` 判定 LSE 是否可用 |
| [lib/builtins/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/CMakeLists.txt) | builtins 的 CMake 装配：定义各架构源文件列表、生成 LSE 辅助文件、循环构建 `clang_rt.builtins-<arch>` |
| [cmake/Modules/CompilerRTUtils.cmake](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/cmake/Modules/CompilerRTUtils.cmake) | `filter_builtin_sources` 函数：实现「架构版覆盖同名通用版」 |

## 4. 核心概念与源码讲解

### 4.1 架构优化内建之一：栈探测 `chkstk`

#### 4.1.1 概念说明

考虑一个函数需要在栈上分配一个很大的局部数组，比如 `char buf[20000];`。编译器会生成一条 `sub %rsp, $20000`（或 AArch64 上的 `sub sp, sp, #...`）来抬高栈顶。

这里藏着一个陷阱：很多操作系统（典型如 Windows，以及 Linux 配合栈保护页）只在线程栈的**末端**放一页不可访问的**保护页（guard page）**，用来检测栈溢出。如果一次 `sub` 直接把栈指针往前挪了 20000 字节，**跳过了**这一页保护页，那么后续对 `buf` 的访问就可能落在**已分配、但尚未触碰**的栈页上——这些页还没被实际映射或还没触发缺页，于是栈溢出就**检测不到**了，程序可能悄无声息地踩坏相邻内存或栈。

解决办法是**栈探测（stack probing）**：在一次性挪动栈指针之前，先**逐页**触碰即将使用的地址区间。这样一旦区间覆盖到保护页，就会立刻触发缺页异常（Windows 上即 `STATUS_STACK_OVERFLOW`），把溢出暴露出来。负责做这件事的小函数就叫 `__chkstk`（或 `___chkstk_ms`、`_chkstk`）。

为什么它必须是汇编？因为它要**直接读写栈指针附近**、要**精确控制每条指令的副作用**（哪些寄存器会被破坏、不能破坏、要不要调整 `sp/rsp`），还要**避免自己再用到栈**——这些约束用 C 写不出来，也没有可移植的「标准」实现，所以它落在架构子目录里，每个架构一份。

#### 4.1.2 核心流程

调用约定（以 `___chkstk_ms` 为例）：调用者把**需要的字节数放进 `%rax`**，然后 `call ___chkstk_ms`。例程**不调整 `%rsp`**（由调用者随后自己 `sub`），只负责「逐页读」一遍，需要的探测页数为：

\[
\text{probe 页数} = \left\lceil \frac{N}{4096} \right\rceil
\]

其中 \(N\) 是请求字节数，4096 是页大小。每一页用一个**读操作**（`test %rcx,(%rcx)` / `ldr xzr,[x17]`）去「触碰」，读的目的不是取数据，而是触发该地址所在页的缺页。流程伪代码：

```
// x86_64 ___chkstk_ms，N 在 %rax，从 rsp 附近的地址开始往低地址探
rcx = rsp + 24            // 起始探测地址（留出 call 压栈的空间）
while N > 4096:
    rcx -= 4096
    触碰 [rcx]            // test %rcx,(%rcx)  ← 读一页，触发缺页
    N    -= 4096
rcx -= N                  // 处理剩余不足一页的尾巴
触碰 [rcx]
返回（%rax 保持原值，供调用者 sub %rsp,%rax）
```

#### 4.1.3 源码精读

x86_64 版本定义在 [lib/builtins/x86_64/chkstk.S:20-38](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/x86_64/chkstk.S#L20-L38)。开头两行 `push %rcx` / `push %rax` 是为了保护这两个寄存器（调用约定要求不破坏它们），随后的循环正是上面伪代码的逐页触碰：

```asm
DEFINE_COMPILERRT_FUNCTION(___chkstk_ms)
        push   %rcx
        push   %rax
        cmp    $0x1000,%rax        ; 0x1000 = 4096，先判断要不要整页循环
        lea    24(%rsp),%rcx       ; 起始探测地址（扣除两次 push + 返回地址）
        jb     1f                  ; 若 < 4096，直接跳到尾巴处理
2:
        sub    $0x1000,%rcx        ; 往低地址退一页
        test   %rcx,(%rcx)         ; ← 关键：读这一页，触发缺页
        sub    $0x1000,%rax        ; 剩余字节数减一页
        cmp    $0x1000,%rax
        ja     2b                  ; 还剩 > 4096 就继续
1:
        sub    %rax,%rcx           ; 尾巴（不足一页）
        test   %rcx,(%rcx)
        pop    %rax
        pop    %rcx
        ret
```

注意头部注释明确说明这是 **Windows 专用**例程（`___chkstk_ms` 是 cygwin/mingw 的命名），且**不调整 `%rsp`、不破坏 `%rax`**——这与 MSVC 的 `__chkstk` 语义一致。

AArch64 版本在 [lib/builtins/aarch64/chkstk.S:26-41](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/aarch64/chkstk.S#L26-L41)，思路相同但调用约定不同：尺寸通过 `x15` 以 **16 字节为单位**传入，例程先 `lsl x16, x15, #4` 换算成字节数，再以 `PAGE_SIZE`(4096) 为步长、用 `ldr xzr, [x17]` 逐页触碰：

```asm
#define PAGE_SIZE 4096
DEFINE_COMPILERRT_FUNCTION(CHKSTK_FUNC)     ; __chkstk 或 __chkstk_arm64ec
        lsl    x16, x15, #4                 ; 字节数 = x15 * 16
        mov    x17, sp
1:
        sub    x17, x17, #PAGE_SIZE         ; 退一页
        subs   x16, x16, #PAGE_SIZE         ; 剩余字节数
        ldr    xzr, [x17]                   ; ← 读一页，触发缺页
        b.gt   1b                           ; 还有剩余就继续
        ret
```

它的注释同样强调「**不修改任何内存或栈指针**」，只破坏 `x16`、`x17` 这两个被定义为临时寄存器的寄存器。

#### 4.1.4 代码实践

> 这是一个**源码阅读型 + 构造型**实践。在大多数 Linux x86_64 桌面环境，`chkstk` 不会被默认链入（它主要用于 Windows/MinGW 与某些裸机场景），所以我们不追求「跑起来」，而是「构造 + 阅读验证」。

1. **实践目标**：亲手模拟 `chkstk` 的逐页探测逻辑，验证它能正确算出探测页数与触碰地址序列。
2. **操作步骤**：
   - 写一个 C 小程序 `simulate_chkstk.c`，用普通循环复刻 x86_64 版的逻辑（`N` 字节、页大小 4096、起始地址设为某个虚拟值 `base`），打印每一次 `test` 的目标地址。
   - 对照 [lib/builtins/x86_64/chkstk.S:23-34](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/x86_64/chkstk.S#L23-L34) 的循环，确认你打印的地址序列与汇编里 `rcx` 的取值一致。

   示例代码（**非项目原有代码，仅为模拟**）：

   ```c
   #include <stdio.h>
   // 模拟 ___chkstk_ms 的逐页探测地址序列，N 为请求字节数
   void simulate_chkstk(unsigned long long base, unsigned long long N) {
       unsigned long long rcx = base + 24;   // 对应 lea 24(%rsp),%rcx
       printf("request %llu bytes\n", N);
       while (N > 0x1000) {                  // 0x1000 = 4096
           rcx -= 0x1000;
           printf("  probe page at %#llx\n", rcx);
           N   -= 0x1000;
       }
       rcx -= N;                             // 尾巴
       printf("  probe tail  at %#llx\n", rcx);
   }
   int main(void) {
       simulate_chkstk(0x7fff00000000ULL, 10000);  // ~2.5 页
       return 0;
   }
   ```
3. **需要观察的现象**：请求 10000 字节时，应打印 **2 个整页探测 + 1 个尾巴**，即 3 次触碰，恰好对应 \(\lceil 10000/4096\rceil = 3\) 页。
4. **预期结果**：输出形如 `probe page at ...` 两次、`probe tail at ...` 一次，地址每次递减 4096。这与汇编循环体一一对应，说明逐页探测「跳不过保护页」。
5. **若想真正链接到 `___chkstk_ms`**：需在 Windows/MinGW 目标上、用触发大栈帧的代码配合编译。在普通 Linux 上该符号默认不存在，属正常现象——可标注「待本地验证（需 Windows/MinGW 工具链）」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `chkstk` 用「读（`test`/`ldr`）」而不是「写」来触碰页面？

> **参考答案**：读操作同样能触发缺页异常，且不会破坏该页上可能已有的数据（对栈而言是未初始化区域，但保持「只读探测」语义更安全、更清晰）。`test %rcx,(%rcx)` 把 `[rcx]` 与 `rcx` 做 AND 并丢弃结果，纯粹是为了产生一次访存。

**练习 2**：AArch64 版为什么先 `lsl x16, x15, #4`？

> **参考答案**：调用约定要求尺寸以 **16 字节为单位**经 `x15` 传入（`mov x15, #256; bl __chkstk` 表示 256×16=4096 字节）。`lsl #4` 等价于乘以 16，把它换算成字节数后再按 4096 字节页探测。

---

### 4.2 架构优化内建之二：AArch64 离线原子操作 `lse.S`

#### 4.2.1 概念说明

AArch64 的原子操作经历过一次重大演进：

- **ARMv8.0**：只能用 **LL/SC**（load-linked / store-conditional）循环实现原子操作，比如 compare-and-swap 要写成「`ldxr` 读 → 比较 → `stxr` 写、失败了重试」的循环。
- **ARMv8.1（LSE）**：新增了一组**单条**原子指令（`cas`、`swp`、`ldadd`、`ldclr`、`ldeor`、`ldset`），无需循环，性能更好。

问题来了：同一段程序可能跑在「有 LSE」的新核上，也可能跑在「无 LSE」的老核上。如果编译器在原子操作处**内联**了某条指令，那么换一台机器就可能直接「非法指令」崩溃。

GCC/Clang 的解决方案是 **out-of-line atomics（离线原子操作）**：编译器在原子操作处不内联具体指令，而是生成一条 `bl __aarch64_cas4_acq_rel`（举例）这样的**函数调用**。这些函数由 compiler-rt 在 [lib/builtins/aarch64/lse.S](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/aarch64/lse.S) 提供，函数体内**先运行时判断当前 CPU 有没有 LSE**：

- 有 LSE → 直接发那条单指令，立即返回；
- 无 LSE → 跳转到 LL/SC 回退循环。

这样**同一份二进制**就能在两代核上都正确运行。这也是为什么它必须用汇编：要发出 LSE 指令、要写 LL/SC 循环、要读一个全局标志位——纯 C 做不到。

#### 4.2.2 核心流程

整个分发的枢纽是一个全局布尔量 `__aarch64_have_lse_atomics`（由 cpu_model 在启动时填充，见 4.3）。每个原子函数体的开头都用一个宏 `JUMP_IF_NOT_LSE` 检查它：

```
__aarch64_casN_ORDER(expected, desired, ptr):
    if (__aarch64_have_lse_atomics == 0)  goto fallback   ; JUMP_IF_NOT_LSE 8f
    ; —— LSE 路径：一条 cas 指令 ——
    cas{A}{L}{S}  expected, desired, [ptr]
    ret
8:  ; —— LL/SC 回退路径 ——
    tmp = expected
loop:
    old = ldxr [ptr]            ; load-linked
    if (old != tmp) goto done
    ok = stxr desired, [ptr]    ; store-conditional
    if (ok == 失败) goto loop
done:
    <BARRIER 若是 _sync>
    ret
```

参数组合爆炸：6 种操作（`cas/swp/ldadd/ldclr/ldeor/ldset`）× 多种宽度（1/2/4/8，`cas` 还有 16）× 5 种内存序（`relax/acq/rel/acq_rel/sync`）= 上百个符号。源码用 **C 预处理 + CMake 生成**的方式，让**同一份 `lse.S`** 在不同 `-D` 宏下编译出不同符号（详见 4.4）。

#### 4.2.3 源码精读

文件开头设定目标架构、并声明那个枢纽标志为隐藏符号：[lib/builtins/aarch64/lse.S:23-35](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/aarch64/lse.S#L23-L35)。

```asm
#ifdef HAS_ASM_LSE
.arch armv8-a+lse
#else
.arch armv8-a
#endif

#if !defined(__APPLE__)
HIDDEN(__aarch64_have_lse_atomics)        ; 这个标志是隐藏符号，不导出
#else
HIDDEN(___aarch64_have_lse_atomics)       ; macOS 名字多一个下划线
#endif
```

分发宏 `JUMP_IF_NOT_LSE` 在 [lib/builtins/lse.S:132-142](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/aarch64/lse.S#L132-L142)（为 macOS/ELF 两种重定位格式分别取地址），它读出标志字节，为 0 就跳到回退标号 `8f`：

```asm
.macro JUMP_IF_NOT_LSE label
        adrp    x(tmp0), __aarch64_have_lse_atomics
        ldrb    w(tmp0), [x(tmp0), :lo12:__aarch64_have_lse_atomics]
        cbz     w(tmp0), \label          ; 标志为 0 → 跳到 LL/SC 回退
.endm
```

以 `cas`（compare-and-swap）为例，[lib/builtins/lse.S:144-197](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/aarch64/lse.S#L144-L197) 完整呈现了两条路径：

```asm
DEFINE_COMPILERRT_OUTLINE_FUNCTION_UNMANGLED(NAME(cas))
        JUMP_IF_NOT_LSE 8f                ; 没 LSE → 跳到 8: 的 LL/SC
        CAS    // s(0), s(1), [x2]        ; 有 LSE：一条 cas 指令搞定
        ret
8:                                       ; LL/SC 回退
        UXT    s(tmp0), s(0)
0:
        LDXR   s(0), [x2]                 ; load-linked 读旧值
        cmp    s(0), s(tmp0)
        bne    1f                         ; 不等于期望值 → 直接返回旧值
        STXR   w(tmp1), s(1), [x2]        ; store-conditional 写新值
        cbnz   w(tmp1), 0b                ; 写失败（被别人抢）→ 重试
1:
        BARRIER                            ; 若是 _sync，补一条 dmb ish
        ret
```

注意宏 `CAS` 的取值：当编译器支持 LSE 汇编助记符（`HAS_ASM_LSE`）时直接写 `cas...` 指令；否则用 `.inst <编码>` 手工编码同一条指令（[lib/builtins/lse.S:148-152](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/aarch64/lse.S#L148-L152)）——这是为了兼容旧版汇编器。`DEFINE_COMPILERRT_OUTLINE_FUNCTION_UNMANGLED` 这个宏本身（定义在 [lib/builtins/assembly.h:311-321](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/assembly.h#L311-L321)）会顺带插入 `CFI_START` 与 `BTI_C`，让这些被频繁调用的函数也具备异常处理与分支目标识别（BTI）信息。

而那个枢纽标志 `__aarch64_have_lse_atomics` 是在哪里被置位的？答案就在 4.3 的 cpu_model 里（Linux 上靠 `getauxval(AT_HWCAP)` 检查 `HWCAP_ATOMICS` 位）。

#### 4.2.4 代码实践

1. **实践目标**：在 AArch64 目标上，观察离线原子函数的两条路径；若没有 AArch64 机器，则做源码追踪型实践。
2. **操作步骤（有 AArch64 Linux 机器时）**：
   - 写一个用了 `std::atomic` compare-and-swap 的 C++ 小程序，用 `clang++ --target=aarch64-linux-gnu -moutline-atomics` 编译（注意是 **outline**，让编译器生成函数调用而非内联）。
   - 用 `objdump -d` 查看生成的 `.o`，搜索 `bl __aarch64_cas` 之类的调用。
   - 把它静态链接 compiler-rt builtins，再用 `objdump -d` 找到 `__aarch64_cas4_acq_rel` 的函数体，确认开头有一条读取 `__aarch64_have_lse_atomics` 的 `ldrb`，以及 `cbz` 到 LL/SC 回退。
3. **操作步骤（仅源码阅读，本机为 x86_64 时）**：
   - 在 [lib/builtins/lse.S](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/aarch64/lse.S) 中分别找到 `cas`、`ldadd` 两个分支，确认它们**都以 `JUMP_IF_NOT_LSE 8f` 开头**，且回退标号 `8:` 之后都是「`ldxr` → 比较/运算 → `stxr` → `cbnz` 重试」的同款循环骨架。
4. **需要观察的现象**：所有 LSE 函数体结构高度一致——前半段是「一条原子指令 + ret」，后半段是「LL/SC 循环 + 可选 barrier + ret」。这正是「同一份模板 + 不同宏」的产物。
5. **预期结果**：你能画出任意一个 `__aarch64_<op><size>_<order>` 符号的两段式控制流图。
6. 若手头无 AArch64 工具链，标注「待本地验证（需 AArch64 目标与 `-moutline-atomics`）」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `__aarch64_have_lse_atomics` 要标记为 `HIDDEN`（隐藏可见性）？

> **参考答案**：它是 compiler-rt builtins 内部的实现细节，不属于 ABI 对外承诺的接口。隐藏可见性既能避免被其它共享库意外覆盖（符号不导出），也允许链接器做更激进的优化（局部符号绑定）。

**练习 2**：LSE 路径只有一条指令就 `ret`，为什么不干脆让编译器直接内联它？

> **参考答案**：内联意味着把「是否有 LSE」的判断**提前到编译期**，需要为每种 CPU 编译不同版本。离线方案把判断**推迟到运行时**，让一份二进制跨两代核运行——代价只是多一次函数调用与一次标志读取，对原子操作这种本就有同步开销的场景而言开销可忽略。

---

### 4.3 cpu_model：运行时 CPU 特性检测

#### 4.3.1 概念说明

`chkstk` 和 `lse.S` 都依赖一个前提：「程序知道当前 CPU 支持什么」。负责建立这个前提的，就是 `lib/builtins/cpu_model/` 目录。它解决一个核心问题：

> 同一个二进制要在不同代际、不同厂商的 CPU 上运行，并且能**在运行时**回答「当前这颗 CPU 支不支持 AVX2？支不支持 LSE？支不支持 RISC-V 的 Vector？」。

这里有一个非常关键的**协同关系（生产者—消费者）**，本讲的实践任务正是围绕它展开：

- **生产者**：`cpu_model/x86.c`（或 `aarch64.c`、`riscv.c`）在程序启动早期用 CPUID / HWCAP 探测特性，把结果填进几个**固定布局的全局结构**（如 x86 的 `__cpu_model`、`__cpu_features2`，AArch64 的 `__aarch64_cpu_features`）。
- **消费者**：编译器（Clang）把源码里的 `__builtin_cpu_supports("avx2")`、`__builtin_cpu_is(...)
` 以及**函数多版本化（FMV）**的 `ifunc` 解析器，**翻译成对上述全局结构的位查询**。

也就是说，`__builtin_cpu_supports` 和 `cpu_model/x86.c` 是**同一个 ABI 契约的两端**：前者读位、后者写位。`__cpu_model` 结构的尺寸被 `_Static_assert(sizeof(__cpu_model) == 16)` 钉死，就是为了保证「编译器生成的读取代码」与「compiler-rt 填充的写入代码」永远对得上。

#### 4.3.2 核心流程

**x86 路径**（基于 CPUID）：

```
程序加载
  ↓
C 运行时调用构造函数 __cpu_indicator_init   （CONSTRUCTOR_ATTRIBUTE）
  ↓
若 __cpu_model.__cpu_vendor != 0 → 已初始化，直接返回   （幂等保护）
  ↓
CPUID leaf 0 → MaxLeaf + 厂商签名（GenuineIntel/AuthenticAMD/...）
CPUID leaf 1 → EAX(型号族号) / ECX,EDX(特性位)
  ↓
detectX86FamilyModel(EAX) → Family, Model
getAvailableFeatures(ECX, EDX, MaxLeaf) → Features[0..3] 位图
        内部还会查 leaf 7 子叶、xgetbv(XCR0)、扩展 leaf 0x80000001 等
  ↓
__cpu_model.__cpu_features[0] = Features[0]      （特性 0–31）
__cpu_features2[0..2]        = Features[1..3]    （特性 32–123）
  ↓
按厂商调用 getIntel/getAMD/getHygonProcessorTypeAndSubtype
        → 填 __cpu_type / __cpu_subtype
  ↓
返回；此后 __builtin_cpu_supports 即可直接读这些位
```

**AArch64 路径**（基于 HWCAP）：构造函数 `__init_cpu_features`（Linux 上见 [aarch64/fmv/getauxval.inc](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/cpu_model/aarch64/fmv/getauxval.inc)）调用 `getauxval(AT_HWCAP)` 与 `AT_HWCAP2` 取到位图，按 [aarch64/hwcap.inc](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/cpu_model/aarch64/hwcap.inc) 里的位定义把特性写进 `__aarch64_cpu_features`。LSE 标志 `__aarch64_have_lse_atomics` 则由 [aarch64/lse_atomics/getauxval.inc](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/cpu_model/aarch64/lse_atomics/getauxval.inc) 单独的构造函数置位。

#### 4.3.3 源码精读

**公共头与构造函数优先级。** [lib/builtins/cpu_model/cpu_model.h:31-47](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/cpu_model/cpu_model.h#L31-L47) 定义了 `CONSTRUCTOR_ATTRIBUTE`，把探测函数挂到构造函数优先级 **90**（Windows 上因要早于 ifunc 解析而用 9）：

```c
// We're choosing init priority 90 to force our constructors to run before any
// constructors in the end user application (starting at priority 101).
#ifdef _WIN32
#define CONSTRUCTOR_PRIORITY 9
#else
#define CONSTRUCTOR_PRIORITY 90
#endif
#define CONSTRUCTOR_ATTRIBUTE __attribute__((constructor(CONSTRUCTOR_PRIORITY)))
```

注释说明：用户应用的构造函数从优先级 101 起，所以这里用 90 抢在它们之前跑——保证任何用户代码（包括依赖特性的 FMV 解析）启动时，`__cpu_model` 已经填好。

**x86 的 `__cpu_model` ABI 结构。** [lib/builtins/cpu_model/x86.c:251-259](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/cpu_model/x86.c#L251-L259) 就是上面说的「契约」：

```c
struct __processor_model {
  unsigned int __cpu_vendor;
  unsigned int __cpu_type;
  unsigned int __cpu_subtype;
  unsigned int __cpu_features[1];     // 注意：只放特性 0–31
} __cpu_model = {0, 0, 0, {0}};

_Static_assert(sizeof(__cpu_model) == 16,
               "Wrong size of __cpu_model will result in ABI break");
```

特性总数超过 32 个，多出来的部分放到 [x86.c:1249](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/cpu_model/x86.c#L1249) 的 `__cpu_features2` 数组里。厂商签名则是 CPUID leaf 0 在 EBX 中返回的魔数（[x86.c:36-40](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/cpu_model/x86.c#L36-L40)）：

```c
enum VendorSignatures {
  SIG_INTEL = 0x756e6547, // "Genu"（GenuineIntel 的前 4 字节）
  SIG_AMD = 0x68747541,   // "Auth"（AuthenticAMD）
  SIG_HYGON = 0x6f677948, // "Hygo"（HygonGenuine）
};
```

**CPUID 的跨编译器封装。** x86 上「问 CPU」靠 `cpuid` 指令，但 GCC/Clang 用 `__get_cpuid`，MSVC 用 `__cpuid` 内建。[x86.c:264-282](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/cpu_model/x86.c#L264-L282) 把它们统一封装成 `getX86CpuIDAndInfo`：

```c
static bool getX86CpuIDAndInfo(unsigned value, unsigned *rEAX, unsigned *rEBX,
                               unsigned *rECX, unsigned *rEDX) {
#if (defined(__GNUC__) || defined(__clang__)) && !defined(_MSC_VER)
  return !__get_cpuid(value, rEAX, rEBX, rECX, rEDX);   // GCC/Clang
#elif defined(_MSC_VER)
  int registers[4];
  __cpuid(registers, value);                             // MSVC
  ...
#else
  return true;                                           // 无法执行 cpuid
#endif
}
```

带子叶的 `getX86CpuIDAndInfoEx`（用于 leaf 7 等扩展信息）在 [x86.c:287-307](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/cpu_model/x86.c#L287-L307) 中同理。

**特性位图组装。** [x86.c:933-938](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/cpu_model/x86.c#L933-L938) 是 `getAvailableFeatures` 的开头，两个宏把「特性编号」当做一个大位图来读写：

```c
static void getAvailableFeatures(unsigned ECX, unsigned EDX, unsigned MaxLeaf,
                                 unsigned *Features) {
  ...
#define hasFeature(F) ((Features[F / 32] >> (F % 32)) & 1)
#define setFeature(F) Features[F / 32] |= 1U << (F % 32)
```

随后它逐位检查 CPUID 的各个返回位并 `setFeature(...)`——从 leaf 1 的 CMOV/MMX/SSE，到 leaf 7 子叶 0 的 AVX2/BMI/SHA，再到扩展 leaf 的 AMD 专有特性，以及 x86-64 微架构级别（`BASELINE`/`V2`/`V3`/`V4`）判定。

**构造函数主入口。** 把上述步骤串起来的是 [x86.c:1257-1307](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/cpu_model/x86.c#L1257-L1307) 的 `__cpu_indicator_init`，关键片段：

```c
int CONSTRUCTOR_ATTRIBUTE __cpu_indicator_init(void) {
  ...
  if (__cpu_model.__cpu_vendor)      // 幂等：已初始化就直接返回
    return 0;

  if (getX86CpuIDAndInfo(0, &MaxLeaf, &Vendor, &ECX, &EDX) || MaxLeaf < 1) {
    __cpu_model.__cpu_vendor = VENDOR_OTHER;
    return -1;
  }
  getX86CpuIDAndInfo(1, &EAX, &EBX, &ECX, &EDX);
  detectX86FamilyModel(EAX, &Family, &Model);
  getAvailableFeatures(ECX, EDX, MaxLeaf, &Features[0]);

  __cpu_model.__cpu_features[0] = Features[0];   // ← 写入 ABI 结构
  __cpu_features2[0] = Features[1];
  __cpu_features2[1] = Features[2];
  __cpu_features2[2] = Features[3];

  if (Vendor == SIG_INTEL)      getIntelProcessorTypeAndSubtype(...);
  else if (Vendor == SIG_AMD)   getAMDProcessorTypeAndSubtype(...);
  else if (Vendor == SIG_HYGON) getHygonProcessorTypeAndSubtype(...);
  ...
}
```

**AArch64 / RISC-V 路径。** AArch64 的入口定义在 [aarch64.c:61-94](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/cpu_model/aarch64.c#L61-L94)：它声明了 `__aarch64_cpu_features` 全局，并按操作系统 `#include` 不同的「取 HWCAP」实现（Linux 用 `getauxval`、Android/Fuchsia/BSD/Windows 各有 `.inc`）。LSE 标志同理（[aarch64.c:32-59](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/cpu_model/aarch64.c#L32-L59)）。Linux 上 LSE 判定只有 4 行，见 [aarch64/lse_atomics/getauxval.inc:1-4](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/cpu_model/aarch64/lse_atomics/getauxval.inc#L1-L4)：

```c
static void CONSTRUCTOR_ATTRIBUTE init_have_lse_atomics(void) {
  unsigned long hwcap = getauxval(AT_HWCAP);
  __aarch64_have_lse_atomics = (hwcap & HWCAP_ATOMICS) != 0;
}
```

其中 `HWCAP_ATOMICS` 是第 8 位（[aarch64/hwcap.inc:28-30](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/cpu_model/aarch64/hwcap.inc#L28-L30)），由内核在启动时据 CPUID 寄存器 `ID_AA64ISAR0_EL1` 置位。RISC-V 则用 `hwprobe` 系统调用填 `__riscv_feature_bits`（[riscv.c:15-25](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/cpu_model/riscv.c#L15-L25)）。三者机制不同（CPUID 指令 vs 内核位图 vs 系统调用），但目标一致：在 `main` 之前填好一张特性表。

#### 4.3.4 代码实践

这是本讲的**主实践**，目标正是体会「`__builtin_cpu_supports`（消费者）↔ `cpu_model/x86.c`（生产者）」的协同。

1. **实践目标**：编写程序调用 `__builtin_cpu_supports`，把它的输出与「CPUID 直查」、`/proc/cpuinfo` 三方对照，确认它们其实读的是同一份特性表。
2. **操作步骤**：

   ```c
   /* cpu_features_demo.c —— 示例代码，非项目原有 */
   #include <stdio.h>

   int main(void) {
       /* __builtin_cpu_supports 由 clang 翻译成对 __cpu_model/__cpu_features2 的位查询 */
       struct { const char *name; } feats[] = {
           {"cmov"}, {"mmx"}, {"sse"}, {"sse2"}, {"sse3"},
           {"ssse3"}, {"sse4.1"}, {"sse4.2"}, {"avx"}, {"avx2"},
           {"bmi"}, {"bmi2"}, {"aes"}, {"pclmul"}, {"rdrnd"},
       };
       printf("== __builtin_cpu_supports 报告的特性 ==\n");
       for (unsigned i = 0; i < sizeof(feats)/sizeof(feats[0]); ++i)
           printf("  %-10s : %s\n", feats[i].name,
                  __builtin_cpu_supports(feats[i].name) ? "yes" : "no");
       return 0;
   }
   ```

   编译运行（本机通常是 Linux x86_64）：

   ```bash
   clang cpu_features_demo.c -o cpu_features_demo
   ./cpu_features_demo
   grep -o 'avx2\|sse4_2\|bmi2\|aes' /proc/cpuinfo | sort -u
   ```

3. **需要观察的现象**：
   - `__builtin_cpu_supports("avx2")` 返回 `yes` 当且仅当 `/proc/cpuinfo` 的 `flags` 行里出现 `avx2`。
   - 二者一致，因为它们最终都源自同一颗 CPU 的 CPUID 结果——区别只在于 `/proc/cpuinfo` 是内核导出的文本，而 `__builtin_cpu_supports` 读的是 `__cpu_indicator_init` 用 CPUID 填好的 `__cpu_model` 位图。
4. **预期结果**：两张特性清单完全吻合。
5. **进一步验证「协同」（可选，需 compiler-rt 构建产物）**：对 `cpu_features_demo` 做 `objdump -d`，找到 `__builtin_cpu_supports` 展开后的代码，能看到它去读 `__cpu_model`（及 `__cpu_features2`）的某个位；再在 compiler-rt 构建产物 `libclang_rt.builtins-x86_64.a` 里确认存在 `__cpu_indicator_init` 与 `__cpu_model` 符号（`nm` / `objdump -t`）。这就把「读的一端」和「写的一端」对上了。
6. **若想确认构造函数确实抢跑**：在 `cpu_features_demo.c` 里加一个 `__attribute__((constructor(101))) void myctor(){...}` 打印一行，再在 `main` 里也打印；运行顺序应是「`myctor`（101）在 `__cpu_indicator_init`（90）之后」。具体输出依赖宿主 CPU，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `__cpu_model` 要用 `_Static_assert(sizeof(__cpu_model) == 16)` 钉死大小？

> **参考答案**：因为 `__cpu_model` 是 Clang 生成的「读位代码」与 compiler-rt 的「写位代码」之间的 ABI。一旦结构尺寸变了，旧二进制读到的字段就会错位，导致 `__builtin_cpu_supports` 误判。钉死大小就是把这个 ABI 显式固化。

**练习 2**：x86 用 CPUID 指令、AArch64 用 `getauxval`，为什么后者不直接读 CPU 的某个寄存器？

> **参考答案**：AArch64 的特性寄存器（如 `ID_AA64ISAR0_EL1`）大多只在 EL1/EL2（内核态）可读，用户态直接读会异常。内核在进程启动时已经替你读好并以 HWCAP 位图放进辅助向量，用户态用 `getauxval` 取这份「安全快照」即可，既可移植又不需要特权。

**练习 3**：`__cpu_indicator_init` 开头为什么有 `if (__cpu_model.__cpu_vendor) return 0;`？

> **参考答案**：构造函数在某些链接场景（如静态库被多次初始化、或同时存在 ifunc 解析器路径）可能被触发多次。这个守卫让填充逻辑**幂等**——只第一次真正执行 CPUID，后续直接返回，避免重复工作与潜在竞争。

---

### 4.4 构建系统如何按架构选择实现

#### 4.4.1 概念说明

前三个模块都在讲「架构相关源码长什么样」，但还有一个绕不开的问题：**CMake 怎么把这些文件正确地装配进每个架构的库里？** 答案落在 `lib/builtins/CMakeLists.txt`，它的核心策略可以概括成一句话：

> 每个架构的源文件列表 = `GENERIC_SOURCES`（全部通用兜底）＋ 该架构专属文件；若专属文件与某个通用文件**同名**，则**用专属版覆盖通用版**。

这套「同名覆盖」规则解释了为什么 [u2-l2](u2-l2-integer-builtins.md) 里讲到的 `__udivdi3` 在 32 位 x86 上用的是 `i386/udivdi3.S`（汇编优化版），而在没有同名专属文件的架构上才用顶层的 `udivdi3.c`。`cpu_model/*.c` 与 `chkstk`、`lse.S` 的纳入也都在这份 CMake 里完成。

#### 4.4.2 核心流程

```
CMake 配置阶段
  ↓
定义 GENERIC_SOURCES（~120 个可移植 .c）            CMakeLists.txt:85
定义各架构源列表 x86_64_SOURCES / i386_SOURCES /
   aarch64_SOURCES / arm_SOURCES / riscv_SOURCES ...
   （每个都以 ${GENERIC_SOURCES} 起头，再追加本架构子目录文件）
  ↓
对 AArch64：用三重 foreach 从 lse.S 生成上百个
   outline_atomic_<pat><size><model>.S 辅助文件     CMakeLists.txt:811-841
  ↓
builtin-config-ix.cmake 探测出 BUILTIN_SUPPORTED_ARCH
  ↓
foreach (arch IN BUILTIN_SUPPORTED_ARCH):           CMakeLists.txt:1099
    filter_builtin_sources(<arch>_SOURCES, <arch>)  CMakeLists.txt:1140
        → 删掉被同名架构版覆盖的通用 .c            CompilerRTUtils.cmake:484
    add_compiler_rt_runtime(clang_rt.builtins,
        STATIC, ARCHS ${arch}, SOURCES ${${arch}_SOURCES})  CMakeLists.txt:1155
        → 产出 libclang_rt.builtins-<arch>.a
  ↓
把实际用到的源文件清单写进 .sources.txt（供测试用） CMakeLists.txt:1170
```

#### 4.4.3 源码精读

**通用兜底清单。** [lib/builtins/CMakeLists.txt:85](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/CMakeLists.txt#L85) 起定义的 `GENERIC_SOURCES` 列出了约 120 个可移植 `.c` 文件，是所有架构共享的底座。

**架构专属清单的拼装。** 以 x86_64 与 AArch64 为例：

- x86 家族把 `cpu_model/x86.c` 抽成共享片段 [x86_ARCH_SOURCES（CMakeLists.txt:370-372）](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/CMakeLists.txt#L370-L372)，然后 `x86_64_SOURCES` = 通用 + TF 浮点 + `x86_ARCH_SOURCES` + `x86_64/floatdidf.c` 等专属文件。`chkstk.S` 只在 Windows 目标下追加：[CMakeLists.txt:417-422](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/CMakeLists.txt#L417-L422)。
- AArch64 的清单在 [CMakeLists.txt:763-768](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/CMakeLists.txt#L763-L768)，明确把 `cpu_model/aarch64.c` 与 `aarch64/fp_mode.c` 加进来：

```cmake
set(aarch64_SOURCES
  ${GENERIC_TF_SOURCES}
  ${GENERIC_SOURCES}
  cpu_model/aarch64.c
  aarch64/fp_mode.c
)
```

**LSE 辅助文件的批量生成。** 这是本单元最精巧的一段：[CMakeLists.txt:811-841](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/CMakeLists.txt#L811-L841) 用三层循环（操作 × 宽度 × 内存序）为每个组合「复制一份 `lse.S`」并打上不同的 `-D` 宏：

```cmake
foreach(pat cas swp ldadd ldclr ldeor ldset)
  foreach(size 1 2 4 8 16)
    foreach(model 1 2 3 4 5)
      if(pat STREQUAL "cas" OR NOT size STREQUAL "16")
        set(helper_asm "${OA_HELPERS_DIR}/outline_atomic_${pat}${size}_${model}.S")
        add_custom_command(
          OUTPUT "${helper_asm}"
          COMMAND ${CMAKE_COMMAND} -E ${COMPILER_RT_LINK_OR_COPY} "${source_asm}" "${helper_asm}"
          DEPENDS "${source_asm}")
        set_source_files_properties("${helper_asm}"
          PROPERTIES
          COMPILE_DEFINITIONS "L_${pat};SIZE=${size};MODEL=${model}"   # ← 关键
          INCLUDE_DIRECTORIES "${CMAKE_CURRENT_SOURCE_DIR}")
        list(APPEND aarch64_SOURCES "${helper_asm}")
      endif()
    endforeach(model)
  endforeach(size)
endforeach(pat)
```

也就是说，磁盘上只有**一份** `lse.S`，构建时通过 `L_cas/SIZE=4/MODEL=2` 这类宏组合「实例化」出 `__aarch64_cas4_acq` 等上百个符号——这就是 4.2 里「同一份模板 + 不同宏」的来源。

**同名覆盖函数。** [cmake/Modules/CompilerRTUtils.cmake:484](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/cmake/Modules/CompilerRTUtils.cmake#L484) 定义的 `filter_builtin_sources` 是「架构版覆盖通用版」的执行者：它遍历每个带子目录的源文件，把 `.S` 后缀换成 `.c`（并读取 `crt_supersedes` 属性得到额外要替换的名字），若发现同名通用 `.c` 存在，就把它从列表里删掉，并打印一条 `preferring ... to ...` 日志：

```cmake
function(filter_builtin_sources inout_var name)
  set(intermediate ${${inout_var}})
  foreach(_file ${intermediate})
    get_filename_component(_file_dir ${_file} DIRECTORY)
    if (NOT "${_file_dir}" STREQUAL "")
      get_filename_component(_name ${_file} NAME)
      string(REGEX REPLACE "\\.S$" ".c" _cname "${_name}")     ; i386/udivdi3.S → udivdi3.c
      get_property(_cnames SOURCE ${_file} PROPERTY crt_supersedes)
      set(_cnames ${_cname} ${_cnames})
      foreach(_cname ${_cnames})
        if(EXISTS "${CMAKE_CURRENT_SOURCE_DIR}/${_cname}")
          message(STATUS "For ${name} builtins preferring ${_file} to ${_cname}")
          list(REMOVE_ITEM intermediate ${_cname})              ; 删掉被覆盖的通用版
        endif()
      endforeach()
    endif()
  endforeach()
  set(${inout_var} ${intermediate} PARENT_SCOPE)
endfunction()
```

「额外要替换的名字」由 `set_special_properties(... SUPERSEDES ...)` 设置，让一个汇编文件能一次顶掉多个通用 `.c`（例如 ARM 的 `addsf3.S` 同时顶掉 `addsf3.c` 和 `subsf3.c`）。

**按架构循环构建。** 主循环在 [CMakeLists.txt:1099](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/CMakeLists.txt#L1099)，对 `BUILTIN_SUPPORTED_ARCH` 里每个架构先调 `filter_builtin_sources`（[CMakeLists.txt:1140](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/CMakeLists.txt#L1140)），再用 `add_compiler_rt_runtime` 产出 `clang_rt.builtins-<arch>`（[CMakeLists.txt:1155-1164](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/CMakeLists.txt#L1155-L1164)），最后把实际源文件清单落盘成 `.sources.txt`（[CMakeLists.txt:1170](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/CMakeLists.txt#L1170)），后者正是 [u1-l4](u1-l4-testing-infrastructure.md) 里「按库内容生成 `librt_has_<函数>` 特性」的数据来源。

#### 4.4.4 代码实践

1. **实践目标**：在一次真实配置中，亲眼看到「同名覆盖」与「LSE 批量生成」发生。
2. **操作步骤**：参照 [u1-l3](u1-l3-build-system.md) 的步骤，配置一个 compiler-rt 构建（目标可只含 x86_64）：
   ```bash
   cmake -S . -B build -DCOMPILER_RT_BUILD_BUILTINS=ON \
         -DCOMPILER_RT_BUILD_SANITIZERS=OFF -G Ninja
   ```
   然后在构建目录里查看：
   ```bash
   grep -i "preferring" build/CMakeFiles/CMakeOutput.log 2>/dev/null \
     || grep -ri "preferring" build 2>/dev/null | head
   ls build/lib/builtins/outline_atomic_helpers.dir 2>/dev/null | head
   cat $(find build -name 'clang_rt.builtins-x86_64.sources.txt' | head -1)
   ```
3. **需要观察的现象**：
   - 日志里会出现形如 `For x86_64 builtins preferring x86_64/floatdidf.c to floatdidf.c` 的覆盖提示（`floatdidf.c` 同时存在于顶层与 `x86_64/`）。
   - 若配置了 AArch64 目标，`outline_atomic_helpers.dir` 下会有大量 `outline_atomic_*.S` 文件，对应 4.2 的符号家族。
   - `.sources.txt` 里能看到 `cpu_model/x86.c` 确实被编进了 `x86_64` 这份库。
4. **预期结果**：以上三类证据各自命中，即可印证「通用兜底＋架构覆盖＋cpu_model 纳入」的装配模型。
5. 若当前环境缺少完整 LLVM 工具链无法完成配置，标注「待本地验证（需可引导的 clang）」。

#### 4.4.5 小练习与答案

**练习 1**：`i386/udivdi3.S` 和顶层 `udivdi3.c` 同时存在时，最终编进 `clang_rt.builtins-i386` 的是哪一个？为什么？

> **参考答案**：是 `i386/udivdi3.S`。`filter_builtin_sources` 发现带子目录的 `i386/udivdi3.S` 后，按规则把 `.S` 换成 `.c` 得到 `udivdi3.c`，确认该通用文件存在后把它从列表移除。所以 32 位 x86 用的是汇编优化版，而其它架构（无同名专属文件）继续用通用 `.c` 版。

**练习 2**：为什么 LSE 要用 CMake「复制一份 `lse.S`＋打宏」而不是直接写上百个独立文件？

> **参考答案**：这些符号的函数体结构完全一致（都是 4.2 里那段两段式控制流），差异只在「操作类型/宽度/内存序」几个宏参数上。用模板＋宏实例化可以**让逻辑只写一遍**，避免上百份几乎相同的代码在维护时漏改一处——这是「用构建系统消灭重复」的典型做法。

## 5. 综合实践

把本讲四个模块串起来，做一次「**从二进制符号反查到 CMake 装配**」的端到端追踪。以本机（Linux x86_64）为例：

1. **符号侧（4.3）**：编译 4.3.4 的 `cpu_features_demo`，用 `nm cpu_features_demo | grep -E '__cpu_model|__cpu_indicator_init'` 找到未定义引用——它们由 compiler-rt builtins 提供。这说明你的程序**已经隐式依赖** cpu_model。
2. **实现侧（4.3）**：在 [lib/builtins/cpu_model/x86.c](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/cpu_model/x86.c) 中定位 `__cpu_indicator_init`（[x86.c:1257](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/cpu_model/x86.c#L1257)），画出它「CPUID → `getAvailableFeatures` → 写 `__cpu_model`」的链路，并解释 `__builtin_cpu_supports` 正是消费这里的写入。
3. **装配侧（4.4）**：在 [lib/builtins/CMakeLists.txt](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/CMakeLists.txt) 里追踪 `cpu_model/x86.c` 是怎么通过 `x86_ARCH_SOURCES`（[L370](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/CMakeLists.txt#L370)）进入 `x86_64_SOURCES`、再经 `foreach`+`add_compiler_rt_runtime`（[L1155](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/CMakeLists.txt#L1155)）编进 `clang_rt.builtins-x86_64` 的。
4. **横向对照（4.1/4.2）**：再读 [aarch64/lse.S](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/aarch64/lse.S) 与 [aarch64/chkstk.S](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/aarch64/chkstk.S)，归纳出「架构优化内建」的共同特征：必须用汇编、依赖运行时探测（`__aarch64_have_lse_atomics`）、靠 CMake 按架构/操作系统条件编译。

最终产出一张关系图：**CMakeLists（装配） → clang_rt.builtins-\<arch\>（产物） → 构造函数 `__cpu_indicator_init`/`init_have_lse_atomics`（启动时填表） → `__builtin_cpu_supports` / `lse.S` 分发（运行时查表）**。这张图就是本讲的全部主线。

## 6. 本讲小结

- builtins 分两层：顶层 `GENERIC_SOURCES` 是可移植纯 C 兜底；`<arch>/` 子目录放架构优化或必须用汇编的实现。
- `chkstk`（[x86_64/chkstk.S](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/x86_64/chkstk.S)、[aarch64/chkstk.S](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/aarch64/chkstk.S)）做**栈探测**，逐页触碰以防一次性大分配跳过保护页。
- `aarch64/lse.S` 实现**离线原子操作**，靠 `__aarch64_have_lse_atomics` 在「LSE 单指令」与「LL/SC 回退」间运行时分发，让一份二进制跨两代核运行。
- `cpu_model/` 在 `main` 之前用 CPUID（x86）或 HWCAP（AArch64/RISC-V）填充 `__cpu_model`/`__aarch64_cpu_features` 等全局结构；`__builtin_cpu_supports` 与 FMV 是这些结构的**消费者**，二者是同一个 ABI 契约的两端。
- 构造函数优先级 90（[cpu_model.h](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/cpu_model/cpu_model.h)）保证 cpu_model 抢在用户构造函数之前完成。
- CMake 用 `GENERIC_SOURCES`＋架构清单＋`filter_builtin_sources`（[CompilerRTUtils.cmake:484](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/cmake/Modules/CompilerRTUtils.cmake#L484)）实现同名覆盖，并用三层循环从单份 `lse.S` 批量实例化上百个原子符号。

## 7. 下一步学习建议

- **横向**：进入 sanitizer_common（u3 单元）后，你会看到 `__aarch64_have_lse_atomics` 这类「运行时探测」思路在更高层的复用——例如 `sanitizer_procmaps_common` 也会按平台分流。本讲的「平台 `.inc` 分发」是它的简化预演。
- **纵向**：若你对 FMV（函数多版本化）感兴趣，可以追 Clang 侧 `TargetParser` 与 `clang::CodeGen` 如何把 `__attribute__((target_clones(...)))` 展开成 ifunc，并在运行时调用本讲的 `__init_cpu_features_resolver`（[aarch64/fmv/getauxval.inc](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/compiler-rt/lib/builtins/cpu_model/aarch64/fmv/getauxval.inc)）。这会补齐「消费者」那一侧的完整图景。
- **动手**：按 4.4.4 真正跑一次 compiler-rt 配置，观察 `preferring ... to ...` 日志与 `outline_atomic_helpers.dir` 目录，把本讲的「装配模型」从纸面变成肌肉记忆。
- builtins 单元到此结束。下一单元 u3 将从这些「最底层的帮手函数」上升到所有 sanitizer 共享的「公共地基」`sanitizer_common`，那是 compiler-rt 体量最大、也最核心的复用层。
