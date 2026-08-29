# 原子抽象与 mi_lock：无锁基础设施

## 1. 本讲目标

学完本讲，你应该能够：

1. 说出 `mi_atomic_*` 系列宏如何通过「宏名拼接」同时映射到 C11 `stdatomic.h`、C++ `std::atomic` 与 MSVC 的 `Interlocked` 内建函数。
2. 区分 `relaxed`、`acquire`、`release`、`acq_rel` 四种内存序，并能对照 mimalloc 的真实调用点（thread_free 链表、page map、arena_pages 缓存）说明每处为什么选这个序。
3. 区分 `mi_atomic_cas_weak`（允许伪失败，必须放循环里）与 `mi_atomic_cas_strong`（只试一次）的使用惯例。
4. 掌握 `mi_lock_t` / `mi_atomic_guard` / `mi_atomic_do_once` 三个同步原语的实现与典型使用位置，并理解 `mi_lock` 在各平台的真实形态。
5. 会用 `MI_DEBUG_TSAN=ON` 构建并运行 `mimalloc-test-stress` 验证无数据竞争告警。

本讲是单元八的地基：u8-l2 的 free list 分片设计、u8-l3 的原子位图，全部建立在这一层原语之上。

## 2. 前置知识

### 2.1 三个层级的内存操作

在多线程程序里，对同一块内存的读写分三个层级（u5-l1 已引入，这里复习并扩展）：

| 层级 | 例子 | 特点 |
|---|---|---|
| 普通读写 | `p->used++` | 最快，但两个线程同时普通读写同一位置是**数据竞争（data race）**，C 标准中是未定义行为 |
| 原子 load/store | `mi_atomic_load_relaxed(&x)` | 保证读写不可分割（不会读到半截值），但默认不提供跨线程的「先后顺序」承诺 |
| 原子 RMW / CAS | `mi_atomic_cas_weak_acq_rel(...)` | 读-改-写（read-modify-write）整体原子，多个线程同时做也只有一个成功 |

mimalloc 的分配快路径（u4-l1）之所以能做到零原子操作，靠的是**所有权契约**：非原子字段只允许属主线程写。而一旦两个线程必须碰同一个字段，就要落到本讲的原子层。

### 2.2 内存序：relaxed / acquire / release

现代 CPU 与编译器都会重排内存访问。内存序（memory order）就是你在一次原子操作上附加的「重排约束」与「可见性承诺」：

- **relaxed**：只保证这次操作本身原子，不约束任何其他访问的顺序。适合计数器、统计累加——你只关心最终总数，不关心谁先谁后。
- **release**（用于写）：这次原子写之前的所有普通写，对「以 acquire 读到这次写入值的线程」全部可见。像**发布**：先把东西（普通写）摆好，再把牌子（原子写）挂出去。
- **acquire**（用于读）：读到某个 release 写入的值之后，后续读取必然能看到那次发布之前的所有普通写。像**消费**：先看到牌子，再去看东西，东西一定已经摆好。
- **acq_rel**（用于 RMW/CAS）：同时具备读端 acquire 与写端 release 两重语义。

release/acquire 配对建立的关系叫 **happens-before**：发布线程发布前的所有写，对消费线程读到的后续代码可见。

### 2.3 weak CAS 与 strong CAS

`compare_exchange` 有两个变体：

- **weak**：允许**伪失败**（spurious failure）——即使值完全匹配也可能返回失败。因此必须放在 `do { ... } while (!cas_weak(...))` 循环里使用，循环体里要用失败时被更新的 `expected` 重新计算。伪失败换来的是在某些架构（如 ARM 的 LL/SC）上更快的指令序列。
- **strong**：不伪失败。适合「只试一次，失败就算了」的场合，比如试探性地把计数加一。

另外，C11 的 `compare_exchange_*_explicit` 接受**两个**内存序参数：成功时用哪个、失败时用哪个。规则是失败序不能强于成功序，且不能是 release/acq_rel（失败的 CAS 没有真正写入，谈不上发布）。mimalloc 把这层细节也封进了宏（见 4.2.3）。

### 2.4 与上一讲的衔接

u5-l2 已经讲过跨线程 free 的核心：`mi_free_block_mt` 用一次 CAS 把块头插进页的 `xthread_free` 原子链表。本讲不再重复那条链路的业务逻辑，而是**向下挖一层**：那次 CAS 用的宏 `mi_atomic_cas_weak_acq_rel` 到底展开成什么、为什么成功序是 acq_rel 而失败序是 acquire、以及作者本人在注释里留下的那句 `// todo: release is enough?` 说明什么。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| [include/mimalloc/atomic.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/atomic.h) | **本讲主角**。全部 `mi_atomic_*` 宏、`mi_lock_t`、`mi_atomic_guard`、`mi_atomic_do_once` 的定义，一个头文件管三种编译环境 |
| [include/mimalloc/bits.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/bits.h) | 机器抽象：指针宽度、CPU 架构识别、popcount/ctz/clz/rotate 位运算原语的编译器分发 |
| [src/arena.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c) | 原子与锁的最大用户：arena 保留锁、arena_pages 发布、purge 守卫 |
| [src/libc.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/libc.c) | `_mi_atomic_once_enter/_release` 的实现（atomic.h 只留声明） |
| [src/free.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c) | `mi_free_block_mt` 的 CAS 使用点 |
| [src/page-map.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-map.c) | relaxed→acquire 双重检查、子表原子发布的范例 |
| [include/mimalloc/internal.h](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h) | `mi_page_set_theap` 的 CAS weak release、所有权位 `mi_atomic_or_acq_rel` |
| [src/theap.c](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c) | 锁反转（lock inversion）场景下 `mi_lock_try_acquire` 的让步重试模式 |

## 4. 核心概念与源码讲解

### 4.1 bits.h：机器抽象与位运算原语

#### 4.1.1 概念说明

`bits.h` 回答两个问题：「我跑在什么机器上」和「这台机器上最快的位运算是哪条指令」。它是纯头文件工具箱，被 [types.h:32](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/types.h#L32) 和 [internal.h:18](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L18) 包含，位于所有头文件的最底层。u8-l3 要讲的原子位图 `bitmap.c` 也直接依赖它（[src/bitmap.c:14](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/bitmap.c#L14)）。

它的分发思路与 atomic.h 一脉相承：**一个统一的函数名，内部按「编译器内建 → 平台 intrinsic → 纯 C 兜底」三级选择**，调用方完全无感。

#### 4.1.2 核心流程

以 `mi_ctz`（count trailing zeros，从最低位起连续 0 的个数）为例，选择顺序是：

1. x64 + BMI1 指令集 → 内联汇编 `tzcnt`（对 0 也有定义，无需特判）。
2. MSVC x64 + BMI1 → `_tzcnt_u64`。
3. MSVC 通用 → `_BitScanForward`。
4. GCC/Clang 内建 → `__builtin_ctz`（对 0 未定义，需要 `x!=0` 特判）。
5. 都没有 → 用 `mi_popcount(x^(x-1))-1` 这类纯 C 算法兜底，并定义 `MI_HAS_FAST_BITSCAN 0` 让上层知道这是慢路径。

#### 4.1.3 源码精读

**指针宽度探测**——用 `<stdint.h>` 的极限宏推断指针是几字节，全程无 configure：

```c
#if INTPTR_MAX > INT64_MAX
# define MI_INTPTR_SHIFT (4)  // assume 128-bit  (as on arm CHERI for example)
#elif INTPTR_MAX == INT64_MAX
# define MI_INTPTR_SHIFT (3)
#elif INTPTR_MAX == INT32_MAX
# define MI_INTPTR_SHIFT (2)
```

[include/mimalloc/bits.h:L33-L41](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/bits.h#L33-L41)：由 `INTPTR_MAX` 推出 `MI_INTPTR_SHIFT`，再组合出 `MI_INTPTR_SIZE`/`MI_INTPTR_BITS`（L65-L66）。全库的「字」概念都建立在这里。

**架构识别**——定义 `MI_ARCH_X64`/`MI_ARCH_ARM64` 等宏，并从指令集宏反推包含哪个头：

[include/mimalloc/bits.h:L80-L104](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/bits.h#L80-L104)：识别 x64/ARM64/x86/ARM32/RISC-V；L97-L101 按 `__AVX2__`/`MI_OPT_SIMD` 决定是否包含 `<immintrin.h>` 或 `<arm_neon.h>`。注意 L106-L114 的「指令集蕴含」技巧：AVX2 蕴含 BMI2/BMI1/LZCNT，只开 `-mavx2` 也能用到位指令。

**位扫描函数**——`mi_ctz`/`mi_clz` 的多级分发：

[include/mimalloc/bits.h:L217-L239](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/bits.h#L217-L239)：`mi_ctz` 的五级选择链。L230 的注释特意指出 `bsf` 在参数为 0 时「目标寄存器内容不确定」，所以要预置 `r = MI_SIZE_BITS` 再用 `"+r"` 约束——这是对编译器/assembler 细节一丝不苟的典型例子。

[include/mimalloc/bits.h:L275-L299](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/bits.h#L275-L299)：`mi_bsf`/`mi_bsr` 把「找到置位位」封装成返回 bool 的接口。L277-L279 的 x64 汇编版利用 `tzcnt` 会置进位标志的特性，一条指令同时得到「是否为零」和「下标」，代码生成更优。

**地址空间宽度**——page map 与 arena 的尺寸都由它决定：

[include/mimalloc/bits.h:L120-L137](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/bits.h#L120-L137)：`MI_MAX_VABITS`（用户态指针最大虚拟地址位数，x64 取 47）与 `MI_MIN_VABITS`（page map 至少常驻 commit 的位数，取 43 即 8 TiB）。u3-l4 讲过 page map 顶层 4 MiB 的推导，源头就是这两个宏。

#### 4.1.4 代码实践

1. **实践目标**：确认你机器上 `mi_ctz` 实际走的是哪个分支。
2. **操作步骤**：写一个只包含 `#include <stdio.h>` 与 `#include "mimalloc/bits.h"` 的小程序（示例代码，非项目原有）：

   ```c
   // ctz-probe.c —— 示例代码
   #include <stdio.h>
   #include "mimalloc/bits.h"
   int main(void) {
     printf("INTPTR_BITS=%zu, ctz(8)=%zu\n",
            (size_t)MI_INTPTR_BITS, mi_ctz(8));
     #if defined(__BMI1__)
     printf("BMI1 path\n");
     #else
     printf("generic/builtin path\n");
     #endif
     return 0;
   }
   ```

   分别用 `gcc -O2` 与 `gcc -O2 -march=x86-64-v3` 编译运行（`-march=x86-64-v3` 会打开 BMI1）。
3. **需要观察的现象**：两种编译都输出 `ctz(8)=3`；后者的 `BMI1 path` 被打印。
4. **预期结果**：指令集宏改变时，`mi_ctz` 的内部分支切换但结果不变——这就是「分发」的含义。
5. 无法在本地编译时，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `mi_ctz` 的兜底分支写成 `x!=0 ? mi_builtinz(ctz)(x) : MI_SIZE_BITS`，而不是直接调用内建函数？
**答案**：GCC/Clang 的 `__builtin_ctz(0)` 是未定义行为。bits.h 在 L227-L228 用条件表达式显式处理 0，返回 `MI_SIZE_BITS`（全 1 的位数）作为哨兵值，让调用方（如 bitmap 扫描）能用统一的 `>= 某阈值` 判断「没有置位位」。

**练习 2**：`mi_popcount` 在 MSVC x64 上为什么要运行时判断 `_mi_cpu_has_popcnt`（L198-L199）？
**答案**：`POPCNT` 指令 2008 年后才进入 x86，比 x64 本身晚。编译期开启的指令集宏只反映编译目标基线，不能保证用户 CPU 支持；MSVC 又没有 GCC 那样的 `__builtin_popcount` 自动降级，所以用函数级运行时探测，不支持时退回 `_mi_popcount_generic`（注释引用 issue #1291）。

### 4.2 atomic.h（上）：跨编译器的原子抽象与宏映射机制

#### 4.2.1 概念说明

mimalloc 必须同时以三种身份编译：C11 的 `.c`、C++ 的 `.cpp`、以及 MSVC 纯 C 模式（没有 `<stdatomic.h>`）。atomic.h 顶部的设计注释说明了三条自我约束：

> We need to be portable between C, C++, and MSVC. ... This is why we try to use only `uintptr_t` and `<type>*` as atomic types. To gain better insight in the range of used atomics, we use explicitly named memory order operations instead of passing the memory order as a parameter.

（[include/mimalloc/atomic.h:L23-L30](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/atomic.h#L23-L30)）

翻译成三条设计决策：

1. **原子类型只用两种**：`_Atomic(uintptr_t)` 和 `_Atomic(T*)`。整数统一 `uintptr_t`，指针靠宏转换。这样 MSVC 纯 C 模式只需要为一种宽度实现包装。
2. **内存序写进函数名**，不做运行时参数。全库实际只用了 relaxed/acquire/release/acq_rel 四种（没有 seq_cst），一眼 grep 就能审计。
3. **一个中间层宏 `mi_atomic(name)`** 负责把统一的名字粘贴成三个环境各自的真实名字。

#### 4.2.2 核心流程

宏映射的机制是「拼接」：

```
mi_atomic(cas_strong_acq_rel)(p, &e, d)
   │
   ├─ C11 环境:  mi_atomic(name) = atomic_##name
   │             → atomic_cas_strong_acq_rel??  ← 不对，name 只拼到"基础操作名"
   │
   实际两层:  mi_atomic_cas_strong_acq_rel 先展开为
             mi_atomic(compare_exchange_strong_explicit)(p,e,d, acq_rel, acquire)
   ├─ C11:    → atomic_compare_exchange_strong_explicit(...)
   ├─ C++:    → std::atomic_compare_exchange_strong_explicit(...)
   └─ MSVC C: → mi_atomic_compare_exchange_strong_explicit(...)  ← 手写包装
```

也就是说：**带内存序的名字（`mi_atomic_cas_strong_acq_rel`）先被「基础操作 + 显式内存序」展开（L80-L85），再由 `mi_atomic(name)`/`mi_memory_order(name)` 两个拼接宏落到三个环境**。

#### 4.2.3 源码精读

**三分支的环境选择**：

```c
#if defined(__cplusplus)
// Use C++ atomics
#include <atomic>
#define  _Atomic(tp)              std::atomic<tp>
#define  mi_atomic(name)          std::atomic_##name
#define  mi_memory_order(name)    std::memory_order_##name
#elif defined(_MSC_VER)
// Use MSVC C wrapper for C11 atomics
#define  _Atomic(tp)              tp
#define  mi_atomic(name)          mi_atomic_##name
...
#else
// Use C11 atomics
#include <stdatomic.h>
#define  mi_atomic(name)          atomic_##name
#define  mi_memory_order(name)    memory_order_##name
```

[include/mimalloc/atomic.h:L32-L63](https://github.com/microsoft/m/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/atomic.h#L32-L63)：注意 C++ 分支把 `_Atomic(tp)` 重定义为 `std::atomic<tp>`——这样结构体字段声明 `_Atomic(uintptr_t) tid;` 在 C 和 C++ 里都合法。MSVC 分支里 `_Atomic(tp)` 直接变成裸 `tp`，原子性完全靠包装函数里的 `Interlocked` 指令保证。

**带双内存序的 CAS 基宏**：

```c
#define mi_atomic_cas_weak(p,expected,desired,mem_success,mem_fail)  \
  mi_atomic(compare_exchange_weak_explicit)(p,expected,desired,mem_success,mem_fail)

#define mi_atomic_cas_strong_acq_rel(p,exp,des)  mi_atomic_cas_strong(p,exp,des,mi_memory_order(acq_rel),mi_memory_order(acquire))
```

[include/mimalloc/atomic.h:L66-L70](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/atomic.h#L66-L70) 与 [L80-L85](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/atomic.h#L80-L85)：基础宏暴露成功/失败两个内存序参数；组合宏把常用搭配固化。`acq_rel` 版的失败序是 **acquire**——因为失败的 CAS 也要把 `expected` 更新成当前值，若想用这个值继续重试，读端必须 acquire 才能看到别的线程发布的内容。

**读/写/交换的显式命名**：

[include/mimalloc/atomic.h:L72-L78](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/atomic.h#L72-L78)：`mi_atomic_load_acquire` / `mi_atomic_store_release` / `mi_atomic_exchange_acq_rel` 等。全库**没有** `mi_atomic_load_seq_cst`——不需要，也就不提供。

**指针版宏**：

[include/mimalloc/atomic.h:L105-L133](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/atomic.h#L105-L133)：C++/C11 下指针操作与整数操作同构，宏直接透传（L124-L132）；C++ 里只为 `NULL` 参数补一个到 `tp*` 的强制转换（L114-L122）。MSVC 纯 C 下则真正做 `T*` ↔ `uintptr_t` 的双向转换（见下面 L357-L368）。

**MSVC 纯 C 包装（已标记 deprecated）**：

[include/mimalloc/atomic.h:L160-L183](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/atomic.h#L160-L183)：L162 注释明说「建议 MSVC 用户始终以 C++ 模式编译」。这段用 `Interlocked*` 家族模拟 C11 语义，自定义了 `mi_memory_order` 枚举（L176-L183）——但**大多数包装直接 `(void)(mo)` 忽略内存序**：

```c
static inline uintptr_t mi_atomic_fetch_add_explicit(_Atomic(uintptr_t)*p, uintptr_t add, mi_memory_order mo) {
  (void)(mo);
  return (uintptr_t)MI_MSC_64(_InterlockedExchangeAdd)((volatile msc_intptr_t*)p, (msc_intptr_t)add);
}
```

[include/mimalloc/atomic.h:L185-L200](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/atomic.h#L185-L200)：`_Interlocked*` 本身是全屏障 RMW，天然满足各内存序，所以忽略参数在正确性上站得住。但 load/store 没有 Interlocked 对应物，只能按架构手写：

[include/mimalloc/atomic.h:L220-L261](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/atomic.h#L220-L261)：x86/x64 分支注释「strong memory model，任何 load 都是 acquire」，直接 `__iso_volatile_load`；ARM64 分支则按序选择 `__ldar`（acquire load）/`__stlr`（release store）/`__dmb`（全屏障）——**同一份代码承认两个架构内存模型强弱不同**，这正是抽象层要吸收的东西。

[include/mimalloc/atomic.h:L357-L368](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/atomic.h#L357-L368)：MSVC 的指针宏把 `T**` 强转 `(_Atomic(uintptr_t)*)` 再来回转 `uintptr_t`——这就是「只用 uintptr_t 和指针两种原子类型」约束换来的简化。

**统计专用的 max 更新**：

```c
static inline void mi_atomic_maxi64_relaxed(volatile int64_t* p, int64_t x) {
  int64_t current = mi_atomic_load_relaxed((_Atomic(int64_t)*)p);
  while (current < x && !mi_atomic_cas_weak_release((_Atomic(int64_t)*)p, &current, x)) { /* nothing */ };
}
```

[include/mimalloc/atomic.h:L145-L148](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/atomic.h#L145-L148)：无锁求峰值的标准写法——CAS 失败时 `current` 已被更新为别人写入的更大值，循环条件 `current < x` 自然退出。`stats.c` 用它维护每个 bin 的 peak（[src/stats.c:L28](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/stats.c#L28)）。全程 relaxed：峰值只是统计，不需要 happens-before。

#### 4.2.4 代码实践

1. **实践目标**：亲眼看到宏拼接的结果，理解「一层名字、三个后端」。
2. **操作步骤**：

   ```
   gcc -E -dD -Iinclude -DCAN_I_USE=1 -x c /dev/null <<'EOF' 2>/dev/null | grep -A2 "compare_exchange_strong_explicit"
   ```
   更简单的办法是准备一个 `expand.c`（示例代码）：

   ```c
   #include "mimalloc/atomic.h"
   void probe(_Atomic(uintptr_t)* p) {
     uintptr_t e = 0;
     mi_atomic_cas_strong_acq_rel(p, &e, 1);
   }
   ```

   然后 `gcc -E -Iinclude expand.c | tail -5` 查看 C11 展开；`g++ -E -Iinclude -x c++ expand.c | tail -5` 查看 C++ 展开。
3. **需要观察的现象**：C11 下出现 `atomic_compare_exchange_strong_explicit(p, &e, 1, memory_order_acq_rel, memory_order_acquire)`；C++ 下是 `std::atomic_...` 与 `std::memory_order_...`。
4. **预期结果**：两个环境的展开只差命名空间前缀，语义完全一致。
5. 若本机无编译器，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 mimalloc 不提供 `mi_atomic_load_seq_cst`？
**答案**：全库没有任何调用点需要全序（total order）。分配器的同步都是点对点的发布-消费（页元数据、链表头、arena 指针），acquire/release 足够；seq_cst 在 x86 上会插入更贵的 `mfence` 或 `xchg`。显式命名风格让「没用到」直接表现为「不存在」。

**练习 2**：MSVC 纯 C 分支中 `_Atomic(tp)` 被定义为裸 `tp`，那结构体里的 `_Atomic(uintptr_t) tid` 字段还是原子的吗？
**答案**：字段本身退化成普通整数，原子性完全由「所有访问都经过 `mi_atomic_*` 包装函数」保证——包装内部使用 `_Interlocked*` 指令或带屏障的内建。这是一种约定式原子：编译器不再帮你检查误用，这也是注释推荐改用 C++ 模式编译的原因之一。

### 4.3 atomic.h（下）：内存序选型的真实调用点

#### 4.3.1 概念说明

抽象层本身不回答「这里该用哪个序」。本模块把仓库里最有代表性的调用点按内存序归类，逐个回答「为什么是它」。这些例子覆盖了学习目标里点名的两处：page map 与 thread_free。

先给一张总表（均已在源码中核实）：

| 内存序 | 典型调用点 | 一句话理由 |
|---|---|---|
| relaxed | 统计峰值、`heap_count` 递减、锁已互斥时的指针读写、CAS 循环里重读链表头 | 只要原子性，不要顺序承诺 |
| release（store） | 发布 `arena_pages`、`submaps[idx]`、`page->self`、`heap_main` | 普通写完成后挂牌 |
| acquire（load） | 读上述指针、`committed_count` 慢路径复查、`once->tid` | 读到牌子后享受挂牌前的写 |
| acq_rel（RMW） | `xthread_free` 的 CAS、`mi_page_claim_ownership` 的 OR、bitmap 置位 | 既要发布自己的写、又要消费别人的写 |

#### 4.3.2 核心流程

「发布-消费」是贯穿所有调用点的同一个模式：

```
生产者                                     消费者
------                                     ------
初始化对象（普通写）                        
mi_atomic_store_release(&slot, obj)        obj2 = mi_atomic_load_acquire(&slot)
                                           → 此后访问 obj2 必然看到初始化内容
```

若发布端降级为 relaxed：消费者可能看到指针却看到未初始化的字段。若消费端降级为 relaxed：同样。若两端都升为 seq_cst：正确但更慢。

#### 4.3.3 源码精读

**(a) thread_free：CAS weak + acq_rel/acquire —— 本讲的旗舰例子**

```c
  // push atomically on the page thread free list
  mi_thread_free_t tf_new;
  mi_thread_free_t tf_old = mi_atomic_load_relaxed(&page->xthread_free);
  do {
    mi_block_set_next(page, block, mi_tf_block(tf_old));     // 普通写：块内 next
    const bool new_owned = (allow_collect ? true : mi_tf_is_owned(tf_old));
    tf_new = mi_tf_create(block, new_owned);
  } while (!mi_atomic_cas_weak_acq_rel(&page->xthread_free, &tf_old, tf_new)); // todo: release is enough?
```

[src/free.c:L80-L87](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L80-L87)：跨线程 free 的 Treiber 栈 push（u5-l2 讲过业务）。逐个元素拆解为什么用这些序：

- 首次 `mi_atomic_load_relaxed`：只是拿个初值当 CAS 的 expected，错了会失败重读，relaxed 足够。
- `mi_block_set_next` 是对**即将发布出去的内存**的普通写；CAS 成功序含 release，保证消费者（收割链表的属主线程）顺着链表读 next 时看到完整内容。
- CAS 失败序是 acquire：失败后 `tf_old` 被硬件更新为最新链表头，下一轮 `mi_block_set_next` 要以它为基础重新链接——不 acquire 就可能基于过期视图构造出断链。
- weak 而非 strong：在 do-while 循环里，weak 的伪失败无害，换更快的指令。

注意那行作者注释 `// todo: release is enough?`——连作者也在怀疑成功序里的 acquire 是否多余。答案留给练习 2。

**(b) page map：relaxed→acquire 双重检查 + 子表原子发布**

```c
mi_decl_nodiscard static bool mi_page_map_ensure_committed(mi_page_map_t* pmap, size_t idx, mi_submap_t* submap) {
  if mi_unlikely(idx >= mi_atomic_load_relaxed(&pmap->committed_count)) {
    if (idx >= mi_atomic_load_acquire(&pmap->committed_count)) {
      if (!mi_page_map_commit_entries(pmap,idx)) return false;
      ...
    }
  }
  *submap = mi_atomic_load_ptr_acquire(mi_page_t*, &pmap->submaps[idx]);
  return true;
}
```

[src/page-map.c:L259-L269](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-map.c#L259-L269)：**同两个字段，两种序**。第一层 relaxed 是快路径过滤——绝大多数查询的 idx 都远小于已提交数，relaxed 的可见性延迟无害（committed_count 单调递增，最坏多走一层慢路径）；第二层 acquire 才与提交端的 `mi_atomic_store_release(&pmap->committed_count, commit_count)`（[src/page-map.c:L254](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-map.c#L254)）配对，建立 happens-before，保证读到「已提交」后访问 `submaps[idx]` 那块内存不会踩到未提交页。

子表本身的跨线程创建用「锁 + acquire 复查 + CAS strong acq_rel 发布」三件套：

```c
  mi_lock(&pmap->lock)
  {
    sub = mi_atomic_load_ptr_acquire(mi_page_t*, &pmap->submaps[idx]); // reload
    if (sub==NULL) {
      ...
      sub = (mi_submap_t)_mi_os_zalloc(subproc, submap_size, &memid);
      ...
        mi_submap_t expect = NULL;
        if (!mi_atomic_cas_ptr_strong_acq_rel(mi_page_t*, &pmap->submaps[idx], &expect, sub)) {
          _mi_os_free(subproc, sub, submap_size, memid);   // 别人赢了，丢弃自己的
          sub = expect;
        }
    }
  }
```

[src/page-map.c:L386-L412](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-map.c#L386-L412)：这是教科书级的「锁内二次检查 + 原子发布」。CAS strong（不是 weak）因为锁外还有无锁读路径（[include/mimalloc/internal.h:L742-L744](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L742-L744) 用 `mi_atomic_load_ptr_acquire` 读 `submaps[idx]`），发布动作只做一次、失败即让位，不需要循环。成功序 acq_rel 保证新 submap 的内容（`_mi_os_zalloc` 的零初始化）对读者可见。

**(c) arena_pages：锁 + release 发布 / 无锁 acquire 消费**

```c
    mi_lock(&heap->arena_pages_lock) {
      arena_pages = mi_atomic_load_ptr_acquire(mi_arena_pages_t, &heap->arena_pages[arena->arena_idx]);
      if (arena_pages == NULL) {  // still NULL?
        ...
        arena_pages = mi_arena_pages_alloc(arena);
        mi_atomic_store_ptr_release(mi_arena_pages_t, &heap->arena_pages[arena->arena_idx], arena_pages);
      }
    }
```

[src/arena.c:L705-L719](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L705-L719)：与 (b) 同构的双检模式。锁外的快路径读（[src/arena.c:L681](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L681)）用 acquire 消费。`mi_arena_pages_alloc` 内部有大量普通写（分配结构、初始化位图），release 保证它们一起可见。

对比一个**反例**——堆销毁路径全部用 relaxed：

```c
    mi_lock(&heap->arena_pages_lock) {
      for (size_t i = 0; i < MI_MAX_ARENAS; i++) {
        mi_arena_pages_t* arena_pages = mi_atomic_load_ptr_relaxed(mi_arena_pages_t, &heap->arena_pages[i]);
        if (arena_pages!=NULL) {
          mi_atomic_store_ptr_relaxed(mi_arena_pages_t, &heap->arena_pages[i], NULL);
```

[src/heap.c:L194-L201](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/heap.c#L194-L201)：为什么这里 relaxed 就够？因为此时锁已经提供互斥，且 `heap` 的生命周期到释放指针为止——这些字段的读写者都被锁（或堆销毁协议）串行化，不需要原子操作再提供可见性，只要不被撕裂即可。**内存序的功力在于敢用 relaxed，而不是到处 acquire。**

**(d) 所有权位：一次 `mi_atomic_or_acq_rel` 认领整页**

```c
// get ownership; returns true if the page was not owned before.
static inline bool mi_page_claim_ownership(mi_page_t* page) {
  const uintptr_t old = mi_atomic_or_acq_rel(&page->xthread_free, (uintptr_t)1);
  return ((old&1)==0);
}
```

[include/mimalloc/internal.h:L1115-L1119](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L1115-L1119)：u6-l4 讲过认领被遗弃页。OR 把最低所有权位置 1，返回旧值判断「是不是我抢到的」。acq_rel 两侧都有意义：成功侧 release 发布「这页现在归我」之前对页的任何准备；失败/读侧 acquire 保证看到前主人留下的状态。与之配套的 `mi_tf_block`/`mi_tf_is_owned`/`mi_tf_create` 位编码见 [include/mimalloc/internal.h:L1089-L1097](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L1089-L1097)。

对照页标志的设置则只 CAS release：

```c
  // we need to use an atomic cas since a concurrent thread may still set the MI_PAGE_HAS_INTERIOR_POINTERS flag (see `alloc_aligned.c`).
  mi_threadid_t xtid_old = mi_page_xthread_id(page);
  mi_threadid_t xtid;
  do {
    xtid = tid | (xtid_old & MI_PAGE_FLAG_MASK);
  } while (!mi_atomic_cas_weak_release(&page->xthread_id, &xtid_old, xtid));
```

[include/mimalloc/internal.h:L1014-L1019](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/internal.h#L1014-L1019)：设置属主线程 id 时要保住低 2 位页标志（u3-l2 的位打包）。这里用 `cas_weak_release`（成功 release、失败 relaxed）而非 acq_rel——失败后只需重读再试，不需要消费别的线程发布的数据结构，失败序 relaxed 即可。

#### 4.3.4 代码实践

这就是本讲规格里指定的统计任务。

1. **实践目标**：亲手建立「调用点 → 内存序 → 理由」的映射，形成自己的审计能力。
2. **操作步骤**：

   ```
   cd <仓库根目录>
   # CAS weak 的使用点（应看到约 7 处，全部在循环里）
   grep -rn "mi_atomic_cas_weak\|mi_atomic_cas_ptr_weak" src/ include/ | grep -v atomic.h
   # CAS strong 的使用点（应看到约 10 处，多为单次尝试）
   grep -rn "mi_atomic_cas_strong\|mi_atomic_cas_ptr_strong" src/ include/ | grep -v atomic.h
   # acquire 读指针 / release 写指针 的配对
   grep -rn "load_ptr_acquire" src/ | head
   grep -rn "store_ptr_release" src/ | head
   ```
3. **需要观察的现象与记录**：填写下面三张表（参考答案见 4.3.5 之后）。

   | 调用点 | weak/strong | 为什么 |
   |---|---|---|
   | `src/free.c:87` `xthread_free` push | weak（在 do-while 内） | 伪失败由循环吸收，换更快指令 |
   | `src/page-map.c:403` submap 发布 | strong（单次尝试） | 失败即采用别人的结果，无需循环 |
   | `src/libc.c:127` once 的 tid | strong | 只判一次「我是不是第一个」 |

   | acquire 调用点 | 配对的 release | 配对保护了什么 |
   |---|---|---|
   | `include/mimalloc/internal.h:743` 读 `submaps[idx]` | `src/page-map.c:403` CAS 发布 / `:379` store | 新建子表的零初始化内容 |
   | `src/arena.c:681` 读 `arena_pages[...]` | `src/arena.c:717` store | `mi_arena_pages_alloc` 的全部初始化 |
   | `src/free.c:194` 读 `page->self` | `src/arena.c:1075` store | 对齐反查得到的页，其 `block_size` 等字段可用 |

   | relaxed 调用点 | 为什么不需要更强的序 |
   |---|---|
   | `src/heap.c:212` `heap_count` 递减 | 纯计数，销毁顺序由别的锁保证 |
   | `src/free.c:82` 初读 `xthread_free` | 只是 CAS 的初值，错了会重读 |
   | `src/stats.c:28` `maxi64` 峰值 | 统计用途，无 happens-before 需求 |
4. **预期结果**：每条 grep 结果都能归入上表某一格；若发现表中没有的模式，记下来带到 u8-l3（bitmap 大量使用 acq_rel 的 RMW）。
5. 行号基于当前 HEAD，如果你在更新的代码上操作，行号可能漂移，以 grep 结果为准。

#### 4.3.5 小练习与答案

**练习 1**：`src/page-map.c:261-262` 为什么先 relaxed 再 acquire 查同一个 `committed_count`，而不是直接一次 acquire？
**答案**：性能。该函数在每次注册页时都会走，绝大多数时候 `idx < committed_count` 成立。relaxed 读在弱内存序架构（ARM64）上可以省掉 load-acquire 指令的开销；即便因可见性延迟误判为「未提交」，也只是多走一层慢路径，最终由第二层 acquire 给出权威答案——`committed_count` 单调递增，晚看到没有危害。这是「乐观快路径 + 权威慢路径」在内存序上的体现。

**练习 2**：`src/free.c:87` 注释写着 `// todo: release is enough?`。假设把成功序从 acq_rel 降为 release（失败序随之降为 relaxed），会破坏什么？
**答案**：破坏失败重试路径。CAS 失败后 `tf_old` 被更新为当前链表头，下一轮 `mi_block_set_next(page, block, mi_tf_block(tf_old))` 要读取**别的线程刚刚发布**的那个块并写它的 next 字段；如果失败序是 relaxed，本线程可能看不到对方对链表内容（甚至对方 CAS 本身）的完整发布，基于过期链表头构造出的新链会丢失节点。成功序的 release 不覆盖失败路径，所以失败序的 acquire 是必要的——这正是宏定义 `acq_rel, acquire` 组合（atomic.h L82）的用意。（进一步的细化讨论可参见是否能把「读 next 再写 next」整体挪出循环，但那是另一个重构话题。）

**练习 3**：`mi_atomic_cas_ptr_strong_acq_rel`（page-map.c:403）失败后为什么直接 `sub = expect` 采用别人的子表，而不是重试？
**答案**：目标不是「一定要由我创建」，而是「这个槽位有一个可用的子表」。锁 + 二次检查已把并发窗口压到极小；失败意味着别人刚创建好并发布了同一个 submap，直接拿来用即可。若坚持重试反而引入无意义的竞争。这是 strong CAS「单次尝试」语义的典型用法。

### 4.4 锁与一次性原语：mi_lock / mi_atomic_guard / mi_atomic_do_once

#### 4.4.1 概念说明

mimalloc 的锁只出现在**低频管理路径**——atomic.h L406-L407 注释写明："Only used for reserving arena's and to maintain the abandoned list."（仅用于保留 arena 和维护 abandoned 列表。）分配/释放热路径上没有锁，这是 u4/u5 已经建立的结论；本模块补上「冷路径用什么」。

一个必须先澄清的事实：**`mi_lock_t` 并不是无锁（lock-free）数据结构，也不是自旋锁**。它在三个主流平台分别就是操作系统的互斥体——Windows 的 SRWLOCK、Unix 的 `pthread_mutex_t`、纯 C++ 环境的 `std::mutex`；只有当三者都不可用（如单线程的 WASI）时，才退化为「CAS 自旋 + yield」的兜底实现。而且兜底实现是**固定上限 10000 次的 yield 重试，并非指数退避**——仓库中 grep 不到任何 backoff 逻辑（与 backoff 相关的命中只有「arena 尺寸指数扩张」等无关注释）。学习手册大纲中「基于 CAS 的自旋锁（含指数退避）」的描述与当前 HEAD 源码不符，以源码为准。

真正的「无锁」体现在两处小型原语上：

- `mi_atomic_guard`：一个 `_Atomic(uintptr_t)` 的微型临界区，同一时刻至多一个线程进入。
- `mi_atomic_do_once`：进程级「只执行一次」原语。

#### 4.4.2 核心流程

**`mi_lock` 宏——用 for 循环模拟 RAII**。C 语言没有析构函数，mimalloc 的惯用法是把「释放」塞进 for 循环的增量表达式：

```c
#define mi_lock(lock)   for(bool _mi_go = (mi_lock_acquire(lock),true); _mi_go; (mi_lock_release(lock), _mi_go=false) )
```

展开后 `mi_lock(&L) { body }` 的执行序列是：`acquire(L)` → 进入循环体 → 增量表达式 `release(L)` 并把 `_mi_go` 置 false → 条件失败退出。中途 `break`/`return` 也会先执行增量表达式，锁总是被释放。`mi_lock_maybe(lock, acquire)` 是条件版：`acquire==false` 时整个块无锁直通，用于「外层已经拿过锁就不要再拿」的场景。

**`mi_atomic_do_once` 的三态协议**：

1. 先 `load_acquire(&once->tid)`：值为 1 表示「已执行完」，直接返回 false——这是最快路径，一次原子读。
2. 值非 1 时 `mi_lock_acquire(&once->lock)`，再 CAS `tid: 0 → current_tid`：成功者是第一个进入者，执行动作，完毕后 `_mi_atomic_once_release` 把 tid 置 1 并放锁。
3. 后来者在锁上等待，拿到锁后 CAS 失败（tid 已非 0），得知动作已被执行，返回 false。
4. 特判：`tid == current_tid` 说明同线程递归调用，直接返回 false，避免自己等自己（死锁）。

#### 4.4.3 源码精读

**锁的三平台实现**：

```c
#if defined(_WIN32) || defined(__CYGWIN__)
typedef struct mi_lock_s {
  SRWLOCK mutex;    // slim reader-writer lock
} mi_lock_t;
...
#elif defined(MI_USE_PTHREADS)
typedef struct mi_lock_s {
  pthread_mutex_t mutex;
} mi_lock_t;
...
#elif defined(__cplusplus)
typedef struct mi_lock_s {
  std::mutex mutex;
} mi_lock_t;
...
#else
// fall back to poor man's locks.
typedef struct mi_lock_s {
  _Atomic(uintptr_t) mutex;
} mi_lock_t;
```

[include/mimalloc/atomic.h:L417-L516](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/atomic.h#L417-L516)：四个分支的类型定义。注意 L422/L450/L484 三个 `MI_LOCK_INITIALIZER`——静态初始化的锁不需要运行时构造，这正是 u7-l1「静态自举」能工作的前提之一。

pthread 分支有个值得注意的初始化细节：

```c
static inline void mi_lock_init(mi_lock_t* lock) {
  if(lock==NULL) return;
  // use this instead of pthread_mutex_init since that can cause allocation on some platforms (and recursively initialize mimalloc!)
  const pthread_mutex_t mutex = PTHREAD_MUTEX_INITIALIZER;
  memcpy(&lock->mutex,&mutex,sizeof(mutex));
}
```

[include/mimalloc/atomic.h:L464-L469](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/atomic.h#L464-L469)：不用 `pthread_mutex_init` 而用 memcpy 拷贝静态初始值，因为前者在个别平台会分配内存——在分配器自己的初始化路径里调它会造成**递归初始化**。这是「分配器代码不能依赖分配器」这一自举约束在锁层的投影。

兜底分支（单线程环境）：

```c
static inline void mi_lock_acquire(mi_lock_t* lock) {
  size_t ticks = 0;
  for (int i = 0; i < 10000; i++) {  // for at most 10000 tries?
    if (mi_lock_try_acquire(lock)) return;
    _mi_prim_thread_yield();
  }
  _mi_error_message(EFAULT, "internal error: lock cannot be acquired (due to lack of native lock primitives)\n");
}
```

[include/mimalloc/atomic.h:L519-L539](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/atomic.h#L519-L539)：`mi_lock_try_acquire` 是一次 `mi_atomic_cas_strong_acq_rel(&lock->mutex, &expected, 1)`（L520-L521），释放是 `mi_atomic_store_release(&lock->mutex, 0)`（L532）。acquire/release 语义完整；重试上限 10000 次，超时报内部错误。注意它是固定次数 + yield，没有指数退避（4.4.1 已澄清）。

**arena 保留锁：双检避免重复保留**

```c
  const size_t arena_count = mi_arenas_get_count(subproc);
  mi_lock(&subproc->arena_reserve_lock) {
    if (arena_count == mi_arenas_get_count(subproc)) {
      // we are the first to enter the lock, reserve a fresh arena
      mi_arena_id_t arena_id = _mi_arena_id_none();
      mi_arena_reserve(subproc, mi_size_of_slices(slice_count), allow_large, &arena_id);
    }
    else {
      // another thread already reserved a new arena
    }
  }
```

[src/arena.c:L551-L563](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L551-L563)：u6-l3 讲过 arena 保留。这里值得看的是**锁前先读一次计数**：如果拿锁前计数已经变了（别的线程刚保留过新 arena），进锁后发现不等式不成立，就白白跳过保留——每个新 arena 只被保留一次，且大多数线程根本不必进锁（拿锁前后计数相同才进）。注释里作者的 todo「allow 2 or 4 to reduce contention?」显示这是刻意的单点串行化。

**锁反转：try_acquire + 让步重试**

```c
// Due to lock-inversion we need to use `mi_lock_try_acquire` and if that fails
// retry (releasing our own lock first)
  mi_lock(&heap->theaps_lock) {
    ...
          if (mi_lock_try_acquire(&tld->theaps_lock)) {
```

[src/theap.c:L377-L395](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/theap.c#L377-L395)：u7-l3 提过 theap 双向拆除的锁反转问题。两把锁（`heap->theaps_lock` 与 `tld->theaps_lock`）在不同代码路径里以相反顺序获取，若都用阻塞 `mi_lock_acquire` 必然死锁。解法：持有第一把锁时对第二把只用**非阻塞**的 `mi_lock_try_acquire`，失败就放弃整个外层重来。这是无死锁的经典模式（try-and-retry），代价是最坏时的活锁（由重试上界兜住）。

**`mi_atomic_guard`：一个原子的微型临界区**

```c
typedef _Atomic(uintptr_t) mi_atomic_guard_t;

// Allows only one thread to execute at a time (without blocking anyone)
#define mi_atomic_guard(guard) \
  uintptr_t _mi_guard_expected = 0; \
  for(bool _mi_guard_once = true; \
      _mi_guard_once && mi_atomic_cas_strong_acq_rel(guard,&_mi_guard_expected,(uintptr_t)1); \
      (mi_atomic_store_release(guard,(uintptr_t)0), _mi_guard_once = false) )
```

[include/mimalloc/atomic.h:L390-L401](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/atomic.h#L390-L401)：注释的关键词是 "without blocking anyone"（不阻塞任何人）。CAS 失败的线程**不等待**，直接跳过整个块——它是「多个线程都来做这件事，谁抢到谁做，其余人直接走」的过滤器，而 `mi_lock` 是「抢不到就睡等」。语义差别在实践模块里看得最清楚：

```c
  // allow only one thread to purge at a time (todo: allow concurrent purging?)
  static mi_atomic_guard_t purge_guard;
  mi_atomic_guard(&purge_guard)
  {
    ...
    for (size_t _i = 0; _i < max_arena; _i++) {
      ...
        const int purged = mi_arena_try_purge(arena, now, force);
```

[src/arena.c:L2403-L2418](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/arena.c#L2403-L2418)：purge（u6-l2 的延迟归还）允许被任何慢路径触发，但同一时刻只让一个线程实际去扫 arena。没抢到的线程不等待、直接返回——purge 不是必须由「我」完成，谁做都一样。这就是 guard 与锁的选择标准：**工作可放弃 → guard；工作必须完成 → lock**。

**`mi_atomic_do_once`：声明与实现**

[include/mimalloc/atomic.h:L544-L557](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/include/mimalloc/atomic.h#L544-L557)：`mi_atomic_once_t` 只是「一个原子 tid + 一把锁」的组合；宏在调用点生成 `static` 局部变量，所以**每个 `mi_atomic_do_once` 调用点有独立的一次性状态**（u7-l1 讲初始化时提过「每调用点独立」的出处就在这里）。

实现：

```c
bool _mi_atomic_once_enter(mi_atomic_once_t* once) {
  const uintptr_t once_tid = mi_atomic_load_acquire(&once->tid);
  if mi_likely(once_tid == 1) {
    return false; // already executed
  }
  const mi_threadid_t current_tid = _mi_thread_id();
  if (once_tid == current_tid) {
    return false; // recursive invocation; don't block on ourselves
  }

  mi_lock_acquire(&once->lock);
  uintptr_t expected = 0;
  if (mi_atomic_cas_strong_acq_rel(&once->tid, &expected, current_tid)) {  // could use atomic_load/store as well
    return true;  // should execute and release
  }
  else {
    mi_lock_release(&once->lock);
    return false; // already another thread entered and released
  }
}
```

[src/libc.c:L115-L134](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/libc.c#L115-L134)：4.4.2 描述的三态协议。注意首行的 `load_acquire` 快路径：初始化完成后的每次调用只花一次原子读；只有首波竞争者才会碰锁。释放端（[src/libc.c:L136-L141](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/libc.c#L136-L141)）用 `store_release(&once->tid, 1)` 收尾——1 是「已完成」的哨兵值，比任何真实 tid 都便于判断。使用实例见 page map 初始化（[src/page-map.c:L360-L364](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page-map.c#L360-L364)）。

#### 4.4.4 代码实践

1. **实践目标**：数清仓库里锁与 guard 的全部使用点，验证「锁只出现在冷路径」的说法。
2. **操作步骤**：

   ```
   grep -rn "mi_lock(\|mi_lock_maybe(\|mi_lock_acquire(\|mi_atomic_guard(" src/ | grep -v atomic.h
   ```

   对每一处命中，打开上下文判断它位于哪条路径：分配快路径 / 分配慢路径 / free 路径 / 线程退出 / 进程初始化 / 堆销毁 / purge。
3. **需要观察的现象**：约 30 处命中，分布在 `arena.c`、`init.c`、`heap.c`、`subproc.c`、`theap.c`、`threadlocal.c`、`options.c`、`page-map.c`、`free.c`；`alloc.c` 的快路径函数（`mi_theap_malloc_small_zero` 等）中一处也没有。
4. **预期结果**：得出结论——所有锁都在「每千次分配至多一次」的管理路径上；`mi_atomic_guard` 全仓库只有 1 个实例（arena.c 的 purge）。
5. 行号随版本漂移，以 grep 实时结果为准。

#### 4.4.5 小练习与答案

**练习 1**：`mi_lock` 宏靠什么保证 `break` 跳出循环体时锁仍被释放？
**答案**：for 循环的增量表达式 `(mi_lock_release(lock), _mi_go=false)` 在**每轮循环体结束、条件重估之前**无条件执行。`break` 跳出的是循环体的本次执行，控制流仍会先走增量表达式再离开 for 结构；`return` 则直接跳出函数——注意这意味着**在 `mi_lock` 块内 `return` 会跳过释放**，这是该宏的已知限制（atomic.h L409-L410 还特意关掉了 MSVC 的 26110 告警）。所以代码约定：临界区内不要 `return`。

**练习 2**：`mi_atomic_guard` 与 `mi_lock` 都能保证「同时至多一个线程」，如何选择？
**答案**：看被保护的工作是否必须由发起线程完成。purge 谁做都行、没抢到的线程直接继续自己的分配——用 guard，还免去了睡眠/唤醒成本。而「保留一个新 arena 并返回它的 id」必须由请求线程亲自完成——用 lock，抢不到就等。此外 guard 只是单个 `_Atomic(uintptr_t)`，可以做成 `static` 局部变量（arena.c:2404），无需初始化。

**练习 3**：`_mi_atomic_once_enter` 里为什么需要 `once_tid == current_tid` 的特判？
**答案**：防同线程递归死锁。若初始化动作内部（间接）再次经过同一个 `mi_atomic_do_once` 调用点，此时 tid 已被 CAS 成本线程 id 但锁还没放；不加特判的话第二次进入会在自己的锁上睡眠，永远醒不来。特判直接返回 false，语义是「本线程已经在执行初始化，嵌套调用无事可做」。

## 5. 综合实践

把本讲三块知识（宏映射、内存序审计、锁分布）串成一个可复现的实验，这就是规格指定的完整任务。

### 5.1 用 TSAN 验证无数据竞争

1. **实践目标**：用 ThreadSanitizer 机器验证 mimalloc 的原子层使用没有数据竞争告警。
2. **操作步骤**：

   ```
   # 需要 clang（TSAN 只支持 clang，见 CMakeLists.txt L468-L475 的判断）
   clang --version
   mkdir -p out/tsan && cd out/tsan
   cmake ../.. -DMI_DEBUG_TSAN=ON -DMI_BUILD_TESTS=ON -DCMAKE_C_COMPILER=clang
   make -j4 mimalloc-test-stress
   ./mimalloc-test-stress
   ```

   要点：`MI_DEBUG_TSAN=ON` 会定义 `MI_TSAN=1` 并加上 `-fsanitize=thread -g -O1`（[CMakeLists.txt:L468-L476](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L468-L476)）；`mimalloc-test-stress` 由根 [CMakeLists.txt:L950-L956](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/CMakeLists.txt#L950-L956) 从 `test/test-stress.c` 构建，TSAN 下静态链接被跳过、需共享库。`test-stress` 是多线程反复分配/释放/ realloc 的压力测试，正是原子层最密集的锻炼场。
3. **需要观察的现象**：
   - 配置阶段输出 `Build with thread sanitizer (MI_DEBUG_TSAN=ON)`；若用 gcc 会得到 `WARNING: Can only use thread sanitizer with clang`。
   - 运行时可用 `MIMALLOC_VERBOSE=1` 确认输出里出现 `thread santizer enabled`（[src/options.c:L248-L250](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/options.c#L248-L250) 的打印）。
   - TSAN 不报 `WARNING: ThreadSanitizer: data race` 即通过。
4. **预期结果**：无告警，程序正常退出。注意两处刻意配合 TSAN 的源码细节：[src/free.c:L73](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/free.c#L73) 在 `MI_TSAN` 下跳过对已释放内存的 debug 涂写（TSAN 无法跟踪复用内存的所有权转移，涂写会制造假阳性）；[src/page.c:L109](https://github.com/microsoft/mimalloc/blob/cd69707c3ca01a4c5fb358e8b92a710554f15356/src/page.c#L109) 同理。**被测代码为验证工具让路**本身就是值得记住的工程现象。
5. 若本机无 clang 或运行环境受限，标注「待本地验证」。

### 5.2 完成内存序审计表

执行 4.3.4 的三条 grep，把结果填进三张表（weak/strong、acquire 配对、relaxed 理由），并为**每一行**写一句「为什么是这个序」。检验标准：不看本讲义，你能对任意一处新调用点（比如 u8-l3 将要讲的 `mi_bitmap` 系列）在 10 秒内说出它的序应该是什么。

## 6. 本讲小结

- `atomic.h` 用两层宏拼接（`mi_atomic(name)` + `mi_memory_order(name)`）把统一名字映射到 C11 `stdatomic.h`、C++ `std::atomic` 与 MSVC `Interlocked` 三个后端；约束「只用 uintptr_t 与指针两种原子类型」让 MSVC 纯 C 包装可行（该分支已标记 deprecated，推荐 C++ 编译）。
- 内存序被编码进函数名：全库只用 relaxed/acquire/release/acq_rel 四种，没有 seq_cst。典型配对——thread_free 的 CAS 用 `weak_acq_rel`（成功发布块内 next，失败 acquire 重读链表头）；page map 与 arena_pages 用「relaxed 快查 + acquire 权威复查 + 锁内 release 发布」；纯计数与锁内指针操作大胆用 relaxed。
- `mi_atomic_cas_weak` 一律出现在 do-while 循环里（伪失败由循环吸收），`mi_atomic_cas_strong` 用于单次尝试（once 判定、子表发布、计数递增）。
- `mi_lock_t` 在主流平台就是 OS 互斥体（SRWLOCK / pthread_mutex / std::mutex），兜底分支才是「CAS strong acq_rel 抢占 + 有界 yield 重试」的自旋锁；**源码中没有指数退避**。锁只服务于 arena 保留、abandoned 列表等冷路径，分配快路径零锁。
- `mi_atomic_guard` 是「抢不到就跳过」的单原子过滤器（全库唯一实例：arena purge）；`mi_atomic_do_once` 用「tid 哨兵 + 锁 + CAS」实现每调用点独立的进程级一次性执行，首路径之后只剩一次 acquire 读。
- MSVC 的 ARM64 分支按内存序选 `__ldar`/`__stlr`/`__dmb`，x64 分支直接用普通 volatile 访问——抽象层吸收的正是这种平台内存模型差异。

## 7. 下一步学习建议

- **u8-l2（free list 分片与多分片设计）**：把本讲的原子原语当作乐高积木，看设计层面如何用「每页独立链表」让跨线程 free 只花一次 CAS。你会重新用到 4.3.3(a) 的 Treiber 栈分析。
- **u8-l3（原子位图内部）**：`src/bitmap.c` 是 `mi_atomic_cas_weak_acq_rel` 与 `mi_atomic_or_acq_rel` 最大的消费方，bits.h 的 `mi_bsf`/`mi_ctz`（4.1 模块）在那里成为查找连续空闲区间的引擎。
- **延伸阅读**：C11 标准第 7.17 节（Atomics）与 `<stdatomic.h>` 的双内存序 CAS 语义；Paul McKenney 的《Memory Barriers: a Hardware View for Software Hackers》解释为什么 x64「任何 load 都是 acquire」而 ARM64 必须显式屏障——对照 atomic.h L223-L237 的两个分支阅读效果最好。
- **动手方向**：给 4.3.4 审计表写一个脚本自动生成（grep + 上下文抓取），在后续版本升级时用它快速重审内存序是否被无意改动。
