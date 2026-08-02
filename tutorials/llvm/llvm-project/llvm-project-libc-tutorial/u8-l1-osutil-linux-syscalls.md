# OSUtil 与 Linux 系统调用封装

## 1. 本讲目标

本讲带你进入 LLVM-libc 与操作系统打交道的最底层。学完后你应当能够：

- 说清 `__support/OSUtil` 作为「跨 OS 系统调用抽象层」的定位，以及它和入口点（entrypoint）、`__support` 的关系。
- 看懂一条 `#include "OSUtil/syscall.h"` 是如何经过「先选 OS、再选架构」两层分派，最终落到某条具体的汇编指令（如 x86_64 的 `syscall`、aarch64 的 `svc 0`、riscv 的 `ecall`）。
- 区分 `syscall_impl`（裸调用、不做错误检查）与 `syscall_checked`（封装、返回 `ErrorOr`）两种封装，理解 Linux 内核「负返回值即错误码」的约定如何被翻译成 `ErrorOr`。
- 读懂一个 syscall wrapper（如 `dup`/`open`/`write`）如何把 `ErrorOr` 再交给上层入口点。

本讲承自 u4-l1（`__support` 总览）与 u4-l3（`error_or` 与 `errno`），是后续 u8-l2（程序启动）、u8-l3（stdio FILE 模型）、u9（分配器与线程）的地基。

## 2. 前置知识

在进入源码前，先用通俗语言把四个概念讲清楚。

**系统调用（system call，简称 syscall）** 是用户态程序请求内核服务的唯一正规通道。`open`/`read`/`write`/`mmap` 这些动作 libc 自己做不了，必须让 CPU 切到内核态去执行。在 Linux 上，用户态把「系统调用号 + 最多 6 个参数」放进约定好的寄存器，然后执行一条「陷阱指令」让 CPU 陷入内核；内核干完活，把返回值放进一个约定好的寄存器，再返回用户态。

**陷阱指令与调用约定因架构而异**。同一段「读文件」逻辑，在 x86_64 上是 `syscall` 指令、参数走 `rdi/rsi/rdx/r10/r8/r9`；在 aarch64 上是 `svc 0` 指令、参数走 `x0~x5`；在 riscv 上是 `ecall` 指令、参数走 `a0~a5`。这正是 libc 必须为每个架构各写一份「发 syscall」代码的原因。

**Linux 的错误返回约定**：内核正常返回一个非负值（如文件描述符、已读写字节数）；出错时返回一个**负数**，其绝对值就是 `errno` 错误码（如 `-EBADF` = `-9`）。内核保证错误码的绝对值不超过 4095。因此「这个返回值是不是错误」可以靠一个范围判断搞定——这是 `syscall_checked` 的核心。

**预定义宏（predefined macros）** 是编译器根据「当前编译目标」自动定义的宏，例如目标平台是 Linux 时定义 `__linux__`，目标是 x86_64 时定义 `__x86_64__`。本讲的两层分派，第一层（选 OS）就靠 `__linux__` 这类宏在预处理期完成。关于 `ErrorOr<T>`/`Error` 这两个错误类型本身，u4-l3 已讲透，本讲只把它们当作「内核负返回值的 C++ 包装」来用。

## 3. 本讲源码地图

本讲涉及的关键文件如下表。所有路径相对于仓库 `libc/` 目录。

| 文件 | 作用 |
|------|------|
| `src/__support/OSUtil/syscall.h` | **第一层分派**：按 OS 预定义宏选 OS 头 |
| `src/__support/OSUtil/linux/syscall.h` | **第二层分派 + 封装**：按架构宏选架构头，并定义 `syscall_impl`、`syscall_checked` |
| `src/__support/OSUtil/linux/x86_64/syscall.h` | x86_64 的 `syscall_impl` 重载（`syscall` 指令 + 寄存器约束） |
| `src/__support/OSUtil/linux/aarch64/syscall.h` | aarch64 的 `syscall_impl`（`svc 0` 指令，`x0~x8` 寄存器） |
| `src/__support/OSUtil/linux/riscv/syscall.h` | riscv 的 `syscall_impl`（`ecall` 指令，`a0~a7` 寄存器） |
| `src/__support/macros/properties/architectures.h` | 把编译器预定义宏翻译成 `LIBC_TARGET_ARCH_IS_*` 宏 |
| `src/__support/OSUtil/linux/syscall_wrappers/{dup,open,write}.h` | 单个 syscall 的薄封装，返回 `ErrorOr` |
| `src/__support/error_or.h` | `ErrorOr<T>`/`Error` 类型别名（u4-l3 详述） |
| `src/__support/CPP/bit.h` | `bit_or_static_cast`，做安全类型转换 |
| `src/__support/OSUtil/CMakeLists.txt`、`src/__support/OSUtil/linux/CMakeLists.txt` | 构建侧的 OS/架构目录分派 |
| `cmake/modules/LLVMLibCArchitectures.cmake` | CMake 解析目标三元组，设 `LIBC_TARGET_OS`/`LIBC_TARGET_ARCHITECTURE` |

---

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**OS 抽象层**、**架构分派**、**syscall 封装**、**错误返回**。

### 4.1 OSUtil：跨 OS 的系统调用抽象层

#### 4.1.1 概念说明

`src/__support/OSUtil` 是 `__support` 下专门「跟操作系统对话」的子模块。它延续 u4-l1 讲过的 `__support` 哲学：是**所有入口点共享的私有工具库**，不对应任何公共头文件、不产生公开 C 符号，靠 `add_header_library`/`add_object_library` 声明为内部构建目标，由入口点经 CMake `DEPENDS` 引用。

但与 `ctype_utils`、`str_to_integer` 这种「纯算法」工具不同，OSUtil 必须处理一个根本难题：**不同操作系统的「请求内核服务」方式完全不同**。Linux 走 syscall 号 + 陷阱指令；Windows 走 NtAPI；baremetal（裸机）根本没有内核，无从 syscall；UEFI 走固件引导服务。如果把 OS 细节直接写进 `open`/`read` 这些入口点，代码就只能在一种 OS 上用。

OSUtil 的解法是**目录隔离 + 头文件分派**：每个 OS 一个子目录（`linux/`、`darwin/`、`freebsd/`、`windows/`、`baremetal/`、`uefi/`、`gpu/`），各自的实现互不可见；上层只 `#include "OSUtil/syscall.h"` 这一个统一入口，由这个入口在预处理期挑出当前 OS 的实现。注意一个细节：**不是所有 OS 都有 `syscall.h`**——`baremetal`、`uefi`、`windows`、`gpu` 这些没有「syscall」概念的目录里就没有 `syscall.h`，它们改用 `io.h`/`exit.cpp` 等其他抽象。

#### 4.1.2 核心流程

OSUtil 顶层统一入口的挑选逻辑可以画成：

```
#include "src/__support/OSUtil/syscall.h"
            │
            ▼  预处理期按 OS 预定义宏选择
   ┌────────┼─────────┐
 __APPLE__ __linux__ __FreeBSD__
   │        │         │
   ▼        ▼         ▼
darwin/   linux/    freebsd/      ← 各自的 syscall.h
syscall.h syscall.h syscall.h
```

关键点：这个选择发生在**预处理期**（`#ifdef`），不是运行期，没有任何分支开销——编译器只为当前目标编译一个分支。

#### 4.1.3 源码精读

OSUtil 的顶层分派头文件极其简短，全部内容就是一个 `#ifdef` 链：

[OSUtil/syscall.h:17-23](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/OSUtil/syscall.h#L17-L23) —— 按 `__APPLE__`/`__linux__`/`__FreeBSD__` 三个编译器预定义宏选择对应 OS 的 `syscall.h`。

这三行就是整个「跨 OS 抽象」的全部魔法。没有任何运行期逻辑：在 Linux 上编译时只有 `#include "linux/syscall.h"` 一行真正生效，其余两个分支被预处理器直接丢弃。值得注意的是，Windows、baremetal、UEFI 等目标在这里没有分支——它们根本不 `#include` 这个文件，而是各自直接用自己的 `io.h` 之类抽象。

#### 4.1.4 代码实践

**实践目标**：建立「每个 OS 一份实现」的直观认识。

**操作步骤**：
1. 打开 `src/__support/OSUtil/syscall.h`，确认它的 `#ifdef` 链只覆盖了 `__APPLE__`、`__linux__`、`__FreeBSD__` 三种 OS。
2. 对照本讲「源码地图」开头的目录树，确认 `darwin/`、`linux/`、`freebsd/` 三个目录下各有自己的 `syscall.h`。
3. 翻看 `baremetal/`、`uefi/`、`windows/`、`gpu/` 四个目录，确认它们**没有** `syscall.h`，而是用 `io.h`/`exit.cpp` 提供别的抽象。

**需要观察的现象**：`syscall.h` 这个文件名只出现在「有 syscall 概念」的 OS 目录里。

**预期结果**：你能列出三套「有 syscall.h」的 OS，并解释为什么 baremetal 没有——因为它运行在无内核的裸机上，没有「陷入内核」可言。

#### 4.1.5 小练习与答案

**练习 1**：为什么 OSUtil 用 `#ifdef` 而不是 `if/else` 来选 OS？

**参考答案**：因为 OS 的差异是 ABI 级别的（寄存器、指令、错误码约定都不同），不可能在同一个翻译单元里既编出 x86_64 Linux 的 `syscall` 指令、又编出 Windows 的 NtAPI 调用。`#ifdef` 保证每个目标只编译属于它自己的那一份代码，避免「为不存在平台生成代码」导致的编译/链接错误。

**练习 2**：如果要把 LLVM-libc 移植到一个全新的 OS（比如 `myos`），OSUtil 层最少要新增什么？

**参考答案**：在 `src/__support/OSUtil/` 下新建 `myos/` 目录，在其中提供 `myos/syscall.h`（或等价的 `io.h`），并在顶层 `syscall.h` 的 `#ifdef` 链里加一个 `#elif defined(__myos__)` 分支指向它。这是 u11-l1「移植到新平台」会展开的话题。

---

### 4.2 架构分派：双层选择机制

#### 4.2.1 概念说明

选定 OS（比如 Linux）之后，问题还没完：**同一种 Linux 还要分 x86_64、aarch64、riscv、arm、i386 等架构**，每种的陷阱指令和寄存器约束都不一样。所以「发 syscall」还需要第二层分派——按架构选实现。

本模块最重要、也最容易被忽略的一点是：**这套分派实际上有两条平行通道，一条在源码里、一条在构建系统里，两者必须对齐。**

- **源码通道（预处理期）**：`linux/syscall.h` 用 `#ifdef LIBC_TARGET_ARCH_IS_X86_64` 等宏挑选要 `#include` 哪个架构头。这些宏由 `architectures.h` 从编译器预定义宏（`__x86_64__` 等）推导出来。
- **构建通道（CMake 配置期）**：CMakeLists.txt 用 `LIBC_TARGET_OS`/`LIBC_TARGET_ARCHITECTURE` 变量决定要 `add_subdirectory` 进入哪个 OS/架构目录、注册哪些目标。

两条通道各自独立从「目标三元组」推出结论，因此天然一致：它们反映的是同一个编译目标，只是一个走编译器的预定义宏、一个走 CMake 的三元组解析。

#### 4.2.2 核心流程

以「在 x86_64 Linux 上发一次 syscall」为例，从一条 include 到最终汇编的完整链路：

```
#include "src/__support/OSUtil/linux/syscall.h"   ← 上层已含此头
        │
        │  ① 预处理：architectures.h 把 __x86_64__ → LIBC_TARGET_ARCH_IS_X86_64
        ▼
linux/syscall.h 的 #ifdef 链
   LIBC_TARGET_ARCH_IS_X86_64 成立
        │
        ▼  ② #include "x86_64/syscall.h"
linux/x86_64/syscall.h
   定义 syscall_impl(...) 重载（内联汇编 "syscall" 指令）
        │
        ▼  ③ 调用时编译器把参数绑入 rdi/rsi/rdx/r10/r8/r9
   生成机器码:  syscall  （陷入内核）
```

与之并行的 CMake 通道：

```
CMake 配置期:  LIBC_TARGET_OS=linux, LIBC_TARGET_ARCHITECTURE=x86_64
        │
OSUtil/CMakeLists.txt      → add_subdirectory(linux)
linux/CMakeLists.txt       → add_subdirectory(x86_64)  + add_subdirectory(syscall_wrappers)
linux/x86_64/CMakeLists.txt → 注册 header library linux_x86_64_util（暴露 syscall.h）
```

#### 4.2.3 源码精读

**① 架构宏从何而来。** `architectures.h` 把编译器预定义宏翻译成 libc 自己的 `LIBC_TARGET_ARCH_IS_*` 宏：

[macros/properties/architectures.h:37-39](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/macros/properties/architectures.h#L37-L39) —— 当编译器定义了 `__x86_64__`（且不是 GPU 虚拟机目标）时，定义 `LIBC_TARGET_ARCH_IS_X86_64`。

同文件还用类似方式定义 `LIBC_TARGET_ARCH_IS_AARCH64`（来自 `__aarch64__`，第 53-55 行）、`LIBC_TARGET_ARCH_IS_ANY_RISCV`（来自 `__riscv`，第 61-71 行）等。**注意它读的是编译器宏，不是 CMake 变量**——这保证源码分派反映的是「编译器实际在为谁生成代码」。

**② 第二层分派。** `linux/syscall.h` 顶部按这些宏挑选架构头：

[linux/syscall.h:19-29](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/OSUtil/linux/syscall.h#L19-L29) —— `X86_32`→i386、`X86_64`→x86_64、`AARCH64`→aarch64、`ARM`→arm、`ANY_RISCV`→riscv，各 include 对应的 `syscall.h`。

**③ x86_64 的实现长什么样。** 每个 `syscall_impl` 重载就是一段内联汇编。最简单的零参数版本：

[linux/x86_64/syscall.h:24-31](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/OSUtil/linux/x86_64/syscall.h#L24-L31) —— 把系统调用号放进 `rax`（约束 `"a"`），执行 `syscall` 指令，返回值从 `rax` 取出（`"=a"(retcode)`）。

`LIBC_INLINE_ASM` 就是 `__asm__ __volatile__`（见 [attributes.h:29](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/macros/attributes.h#L29)）。`SYSCALL_CLOBBER_LIST`（第 15 行）声明 `rcx`/`r11`/`memory` 会被内核改坏，提醒编译器不要假设它们的值还可用。

**④ 跨架构对比：指令与寄存器不同，骨架相同。** aarch64 与 riscv 都用宏把「绑寄存器」这件事参数化：

[linux/aarch64/syscall.h:43-44](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/OSUtil/linux/aarch64/syscall.h#L43-L44) —— `SYSCALL_INSTR` 展开为 `svc 0` 指令，系统调用号绑入 `x8`，参数绑入 `x0~x5`。

[linux/riscv/syscall.h:43-44](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/OSUtil/linux/riscv/syscall.h#L43-L44) —— 同样的骨架，指令换成 `ecall`，寄存器换成 `a7`（调用号）与 `a0~a5`（参数）。

i386 则用最朴素的 `int $128` 软中断（[i386/syscall.h:19](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/OSUtil/linux/i386/syscall.h#L19)）。三种架构、三种指令，但都被命名为 `syscall_impl`，对上层提供完全一致的 C++ 接口。

**⑤ 构建侧的对应分派。** CMake 用 `LIBC_TARGET_OS`/`LIBC_TARGET_ARCHITECTURE` 决定进入哪个目录：

[OSUtil/CMakeLists.txt:1-4](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/OSUtil/CMakeLists.txt#L1-L4) —— 当目标 OS 目录不存在时直接 `return()`（什么也不构建），否则 `add_subdirectory(${LIBC_TARGET_OS})` 进入对应 OS 目录。

[linux/CMakeLists.txt:1-6](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/OSUtil/linux/CMakeLists.txt#L1-L6) —— 同样的模式再分一层：进入 `${LIBC_TARGET_ARCHITECTURE}` 目录，再进入 `syscall_wrappers`。

而这两个 CMake 变量本身来自 [LLVMLibCArchitectures.cmake:169-196](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/cmake/modules/LLVMLibCArchitectures.cmake#L169-L196)——CMake 解析目标三元组得到的 `libc_arch`（如 `x86_64`）。

#### 4.2.4 代码实践（本讲核心实践，呼应任务要求）

**实践目标**：亲手追踪「x86_64 架构是如何被一层层选中、最终 include 到对应 syscall 实现的」，画出流程图。

**操作步骤**：
1. 从 `src/__support/OSUtil/syscall.h:19` 出发，确认在 `__linux__` 下进入 `linux/syscall.h`。
2. 在 `linux/syscall.h` 中找到第 19-29 行的 `#ifdef` 链，确认 `LIBC_TARGET_ARCH_IS_X86_64` 分支 `#include "x86_64/syscall.h"`。
3. 回溯 `LIBC_TARGET_ARCH_IS_X86_64` 的来源：打开 `src/__support/macros/properties/architectures.h`，确认它在 `__x86_64__` 被定义时由第 37-39 行定义。
4. 确认 `__x86_64__` 是「编译器在为 x86_64 目标编译时自动定义」的宏（不是 libc 自己 `#define` 的）。
5. 画出下面这张流程图：

```
源码通道（预处理期）:
  OSUtil/syscall.h: __linux__ ────────────────► linux/syscall.h
  linux/syscall.h:  LIBC_TARGET_ARCH_IS_X86_64 ─► x86_64/syscall.h
  x86_64/syscall.h: 内联汇编 "syscall" 指令     ─► 机器码

宏来源链:
  编译器为 x86_64 编译 → 预定义 __x86_64__
       └(architectures.h:37-39)→ LIBC_TARGET_ARCH_IS_X86_64

构建通道（CMake 配置期，与之平行）:
  目标三元组 x86_64-linux-...
       └(LLVMLibCArchitectures.cmake)→ LIBC_TARGET_ARCHITECTURE=x86_64, LIBC_TARGET_OS=linux
       └(OSUtil/CMakeLists.txt)→ add_subdirectory(linux)
       └(linux/CMakeLists.txt) → add_subdirectory(x86_64) → 注册 linux_x86_64_util
```

**需要观察的现象**：源码通道与构建通道各走各的，但都从「目标三元组」出发，结论一致。

**预期结果**：你能说出「为什么换架构只改构建目标、不用动源码」——因为分派完全由宏和目录约定自动完成。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `linux/syscall.h` 用的是 `LIBC_TARGET_ARCH_IS_X86_64`，而不是直接用编译器的 `__x86_64__`？

**参考答案**：`LIBC_TARGET_ARCH_IS_*` 是 libc 自己的薄封装层，它在 `architectures.h` 里统一处理了一些边界情况（比如 GPU 虚拟机目标 `LIBC_TARGET_ARCH_IS_VM` 会抑制 x86 宏的定义，见第 37 行的 `!defined(LIBC_TARGET_ARCH_IS_VM)` 条件）。用自己的一套宏名能让「架构判定逻辑」集中在一个文件里，其余源码只认这一套统一名字，便于维护。

**练习 2**：aarch64 的 `syscall_impl` 把系统调用号放在哪个寄存器？为什么不是和 x86_64 一样放在第一个参数寄存器？

**参考答案**：aarch64 把调用号放在 `x8`，参数放 `x0~x5`（见 [aarch64/syscall.h:16](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/OSUtil/linux/aarch64/syscall.h#L16)）；而 x86_64 把调用号放在 `rax`。这是 ARM/Linux 的 ABI 规定，不是 libc 的选择——libc 只是遵守硬件与内核约定的调用约定。

---

### 4.3 syscall_impl 与 syscall_checked：syscall 封装

#### 4.3.1 概念说明

有了各架构的 `syscall_impl`（直接发 syscall、返回原始 `long`），上层还需要两样东西：一是「把任意个参数统一转成 `long` 再调用」的便捷模板，二是「自动检查内核返回值、出错就包成 `ErrorOr`」的安全封装。`linux/syscall.h` 正好提供这两个层次：

- `syscall_impl<R>(number, args...)`：变参模板，把参数强转 `long` 后调用架构版的 `syscall_impl`，再做一次类型转换。**不做任何错误检查**。
- `linux_syscalls::syscall_checked<R>(number, args...)`：在 `syscall_impl` 之上加一层「返回值是否落在错误区间」的判断，返回 `ErrorOr<R>`。

绝大多数会失败的 syscall（`open`/`read`/`write`…）应当用 `syscall_checked` 或在 wrapper 里自己做等价检查；只有极少数「永远不会失败」的（如 `getpid`、`getuid`）才直接用 `syscall_impl`。

#### 4.3.2 核心流程

两层封装的调用关系：

```
入口点 / syscall_wrapper  (如 linux_syscalls::dup)
        │  调用 syscall_impl<int>(SYS_dup, fd)
        ▼
syscall_impl<R>(number, ts...)        ← linux/syscall.h 模板
   1. static_assert 参数 ≤ 6
   2. 把 ts... 强转 (long)
   3. 调用架构版 syscall_impl(number, long, long, ...)
        │
        ▼
x86_64/syscall_impl(number, long...)  ← 内联汇编，发出 "syscall"
        │  返回 long（内核返回值，可能为负错误码）
        ▼
（在 syscall_checked 里）判断 ret 是否在错误区间
   是 → return Error(-ret)     // 取反成正 errno
   否 → return bit_cast<R>(ret) // 包装成成功值
```

#### 4.3.3 源码精读

**① 变参模板 `syscall_impl`。**

[linux/syscall.h:35-39](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/OSUtil/linux/syscall.h#L35-L39) —— 用 `static_assert(sizeof...(Ts) <= 6)` 保证 Linux syscall 最多 6 参数，再把每个参数 `(long)ts...` 强转后委托给架构版同名函数，最后用 `bit_or_static_cast<R>` 转换返回类型。

这里有个精妙的「名字重载」：模板 `syscall_impl<R>` 内部调用的 `syscall_impl(__number, (long)ts...)`（不带模板参数）会解析到 `x86_64/syscall.h` 里那组 `long syscall_impl(long, long...)` 重载——因为后者就在同一个 `LIBC_NAMESPACE_DECL` 命名空间里，靠参数个数（重载）而非返回类型来区分。

**② 类型转换 `bit_or_static_cast`。** 它在大小相同时走 `bit_cast`（位级重解释，避免违反严格别名），否则退化为 `static_cast`：

[CPP/bit.h:286-293](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/CPP/bit.h#L286-L293) —— `if constexpr (sizeof(To)==sizeof(From))` 用 `bit_cast`，否则用 `static_cast`。

这让 `syscall_impl<int>(...)` 把 `long` 返回值安全地解释成 `int`（如文件描述符），或 `ssize_t` 解释成有符号字节数，而不会触发未定义行为。

**③ 带 errno 检查的 `syscall_checked`。**

[linux/syscall.h:42-56](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/OSUtil/linux/syscall.h#L42-L56) —— 定义 `MAX_ERRNO = 4095`，调用 `syscall_impl` 拿到 `unsigned long ret`，若 `ret >= -MAX_ERRNO` 则视为错误，返回 `Error((int)-ret)`，否则包装成成功值。

**④ wrapper 怎么用。** 单个 syscall 的薄封装统一放在 `linux/syscall_wrappers/`，每个返回 `ErrorOr`。最典型的 `dup`：

[syscall_wrappers/dup.h:26-31](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/OSUtil/linux/syscall_wrappers/dup.h#L26-L31) —— 调 `syscall_impl<int>(SYS_dup, fd)`，若 `ret < 0` 返回 `Error(-ret)`，否则返回新 fd。这就是 u4-l3 里讲过的「端到端错误传播」起点的真实代码。

注意 `dup.h` 没有用 `syscall_checked`，而是手动写了 `if (ret < 0) return Error(-ret)`——两者思路完全一致（一个用 `ret >= -MAX_ERRNO` 无符号比较、一个用 `ret < 0` 有符号比较），效果等价。`open.h`（[第 23-32 行](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/OSUtil/linux/syscall_wrappers/open.h#L23-L32)）也是同样写法，还额外处理了 `SYS_openat`/`SYS_open` 的历史兼容。

#### 4.3.4 代码实践

**实践目标**：对比「带检查」与「不带检查」两种风格的等价性。

**操作步骤**：
1. 打开 `src/__support/OSUtil/linux/syscall.h`，读懂 `syscall_checked`（第 49-56 行）如何用 `ret >= -MAX_ERRNO` 判错。
2. 打开 `src/__support/OSUtil/linux/syscall_wrappers/dup.h`，读懂它如何用 `if (ret < 0) return Error(-ret)` 判错。
3. 把 `dup` 的判错条件 `ret < 0`（`ret` 是 `int`）与 `syscall_checked` 的 `ret >= -MAX_ERRNO`（`ret` 是 `unsigned long`）放在一起对比。

**需要观察的现象**：`syscall_checked` 把返回值先转成 `unsigned long` 再比较，是为了把「负数」变成「很大的正数」落在 `[-4095, -1]` 对应的无符号区间；而 `dup` 因为 `ret` 已经是 `int`，直接 `< 0` 更直观。

**预期结果**：你能解释为什么两者数学上等价——见下一模块的公式。如果你要新增一个会失败的 syscall，应优先用 `syscall_checked` 以免漏写判错。

#### 4.3.5 小练习与答案

**练习 1**：`syscall_impl` 模板里的 `static_assert(sizeof...(Ts) <= 6)` 为什么是 6？

**参考答案**：Linux 的 syscall 调用约定最多支持 6 个参数寄存器（x86_64 是 `rdi/rsi/rdx/r10/r8/r9`，aarch64 是 `x0~x5`），超过 6 个参数的 syscall 在内核接口里不存在。`static_assert` 在编译期就拦住违规调用。

**练习 2**：`syscall_impl` 模板内部调用 `syscall_impl(__number, (long)ts...)`（不带 `<R>`），编译器怎么知道调用的是架构版而不是递归调用自己？

**参考答案**：因为架构版（如 `x86_64/syscall.h` 里的）是一组**非模板**的 `long syscall_impl(long, long...)` 重载，参数个数确定；而模板版本本身带模板参数 `R`。调用时不写 `<R>`、且实参全是 `long` 时，非模板重载的匹配优先级高于模板，所以解析到架构版。这是 C++ 重载决议的标准规则。

---

### 4.4 错误返回：负错误码与 ErrorOr

#### 4.4.1 概念说明

本模块把「内核的负返回值约定」与「libc 的 `ErrorOr` 类型」对接起来。这是 u4-l3 讲过的「端到端错误传播」在 syscall 层的具体落地。

回忆三件事：Linux 内核出错时返回 `-errno`（如 `-EBADF`）；内核保证错误码绝对值 ≤ 4095；libc 内部用 `ErrorOr<T>`（即 `cpp::expected<T, int>`）表示「值或错误」，`Error` 是 `cpp::unexpected<int>` 的别名。syscall 层的职责就是：把内核那个「可能是负错误码的 `long`」翻译成 `ErrorOr`——是错误就 `Error(-ret)` 取反成正 `errno`，是正常值就 `bit_cast` 成目标类型。

#### 4.4.2 核心流程

错误判定的数学原理。设内核返回值为有符号 `ret`，错误码常量 `errno` 满足 \(1 \leq errno \leq 4095\)。则：

\[ \text{出错} \iff ret \in [-4095,\,-1] \]

在 `syscall_checked` 里，`ret` 被先转成 `unsigned long`（记为 \(u\)）。一个负数 \(ret\) 转成 64 位无符号后变成 \(u = 2^{64} + ret\)，它是一个极大的值（接近 \(2^{64}\)）。而 `-MAX_ERRNO` 作为 `unsigned long` 同样是 \(2^{64} - 4095\)。于是判错条件 `ret >= -MAX_ERRNO` 在无符号语义下等价于：

\[ u \geq 2^{64} - 4095 \iff ret \in [-4095,\,-1] \]

这正是「错误区间」的无符号表达。用无符号比较的好处是单条机器指令、无分支预测成本，且天然涵盖了「最大合法返回值」与「最小错误返回值」之间没有歧义（合法返回值都是小正数，远小于 \(2^{64}-4095\)）。

完整传播链（承接 u4-l3）：

```
内核返回 long  (正常 ≥0，出错 ∈ [-4095,-1])
   │
   ▼  syscall_checked / wrapper 判错
ErrorOr<R>     (出错: Error(-ret) 即正 errno；成功: 值)
   │
   ▼  入口点 (如 dup.cpp)
if (!ret) { libc_errno = ret.error(); return -1; }
return ret.value();
```

#### 4.4.3 源码精读

**① `ErrorOr`/`Error` 的定义**（u4-l3 详述，此处只做锚点）：

[error_or.h:17-19](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/error_or.h#L17-L19) —— `ErrorOr<T>` 是 `cpp::expected<T, int>` 的别名，`Error` 是 `cpp::unexpected<int>` 的别名。syscall wrapper 返回的就是这个类型。

**② 判错与包装**（同 4.3.3 ③）：

[linux/syscall.h:49-56](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/OSUtil/linux/syscall.h#L49-L56) —— `unsigned long ret = syscall_impl(...)`；`if (ret >= -MAX_ERRNO) return Error((int)-ret);`；否则 `return bit_or_static_cast<R>(ret);`。

注意 `Error((int)-ret)`：`ret` 是无符号、值很大，`-ret` 在转 `int` 前先做无符号取负得到 \(2^{64}-u\)，对于错误区间这正好等于正 `errno`，再截断到 `int`。

**③ 一个更完整的 wrapper 例子：`write`。**

[syscall_wrappers/write.h:22-27](https://github.com/llvm/llvm-project/blob/036af906f355771a5c2b3bb51a88d995c6010219/libc/src/__support/OSUtil/linux/syscall_wrappers/write.h#L22-L27) —— `ssize_t ret = syscall_impl<ssize_t>(SYS_write, fd, buf, count);`，`if (ret < 0) return Error(-static_cast<int>(ret));`，否则返回写入字节数。

这里 `ret` 是有符号 `ssize_t`，所以用 `ret < 0` 判错；`-static_cast<int>(ret)` 把负错误码取反成正 `errno`。与 `syscall_checked` 是同一思想、不同写法。

#### 4.4.4 代码实践

**实践目标**：验证「无符号判错」与「有符号判错」的等价性。

**操作步骤**：
1. 设想内核返回 `ret = -9`（`EBADF`）。
2. 按 `syscall_checked` 走：`unsigned long u = (unsigned long)(-9)`，这个值等于 \(2^{64}-9\)，远大于 \(2^{64}-4095\)（即 `-MAX_ERRNO` 的无符号值），所以 `u >= -MAX_ERRNO` 成立 → 判为错误 → `Error((int)-u)` = `Error(9)`。
3. 按 `write.h` 走：`ssize_t ret = -9`，`ret < 0` 成立 → `Error(-static_cast<int>(-9))` = `Error(9)`。
4. 再设想正常返回 `ret = 5`：两条路径都判为「非错误」，返回 `5`。

**需要观察的现象**：两种写法在「错误返回 -9」「正常返回 5」两种情况下给出完全一致的结果。

**预期结果**：你能口头证明两者等价，并理解为何 `syscall_checked` 偏要用无符号比较——它把「判断」统一成一条无符号比较指令，不依赖返回值是否预先转成有符号类型，对任意 `R` 都通用。如果你不确定推导结果，请标注「待本地验证」并写个小程序打印 `(unsigned long)(-9)` 与 `-MAX_ERRNO`（`unsigned long`）的值对照。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `syscall_checked` 里要写 `(int)-ret` 取反，而不是直接把负 `ret` 当 errno？

**参考答案**：因为 `errno` 约定是正数（如 `EBADF = 9`），而内核返回的是 `-errno`（`-9`）。`-ret` 把 `-9` 变回 `9`，才是合法的正 errno 值，后续 `libc_errno = ret.error()` 才能设置正确的错误码。

**练习 2**：`syscall_checked` 的注释说「`getpid`、`getuid` 等少数 syscall 永不失败，例外地不应使用它」。为什么对这类 syscall 用 `syscall_checked` 反而可能出错？

**参考答案**：`syscall_checked` 把任何「看起来像负错误码」的返回值都判为错误。虽然 `getpid`/`getuid` 的合法返回值（pid/uid）通常是小正数，但理论上若某个返回值恰好落在 \([-4095,-1]\) 的无符号区间就会被误判为错误。更重要的是，这些调用根本不会出错，加判错纯属多余开销，也容易误导读者以为它可能失败。所以对「永不失败」的 syscall 应直接用 `syscall_impl`。

---

## 5. 综合实践

把本讲四个模块串起来，完成一次「端到端追踪」。

**任务**：选定一个 syscall wrapper（建议 `dup` 或 `write`），从它的公开入口点一直追到 CPU 发出的那条汇编指令，画出完整调用链并标注每层的职责。

**建议步骤**：

1. **选目标**：以 `dup` 为例。先找到它的 syscall wrapper `src/__support/OSUtil/linux/syscall_wrappers/dup.h`，确认它返回 `ErrorOr<int>`。
2. **追下层**：`dup.h` 调用 `syscall_impl<int>(SYS_dup, fd)`。打开 `src/__support/OSUtil/linux/syscall.h`，确认这个变参模板把 `fd` 强转 `long` 后调用架构版 `syscall_impl`，并用 `bit_or_static_cast<int>` 转换返回值。
3. **追架构层**：确认在 x86_64 上，架构版来自 `src/__support/OSUtil/linux/x86_64/syscall.h`，其 `syscall_impl(long, long)` 重载用 `LIBC_INLINE_ASM("syscall" ...)` 把 `SYS_dup` 放进 `rax`、`fd` 放进 `rdi`。
4. **追分派**：解释 `x86_64/syscall.h` 是怎么被 include 进来的——`linux/syscall.h:19-29` 因 `LIBC_TARGET_ARCH_IS_X86_64` 而选中它，而该宏由 `architectures.h` 从编译器预定义的 `__x86_64__` 推导。
5. **追错误**：回到 `dup.h`，确认 `ret < 0` 时返回 `Error(-ret)`，并说明这个 `ErrorOr` 会如何被上层 `dup` 入口点消费（`if (!ret) { libc_errno = ret.error(); return -1; }`，详见 u4-l3）。
6. **画一张总图**，包含五个层次：入口点 → syscall wrapper → `syscall_impl` 模板 → 架构版内联汇编 → 内核；并在旁边画出平行的「源码分派链」与「CMake 分派链」。

**交付物**：一张标注了文件路径、行号、每层职责的调用链图，以及一段说明「为什么这套设计让换 OS/换架构几乎不用改业务代码」。

> 说明：本实践为源码阅读型，不需要真正编译运行。若你想验证，可在本地按 u1-l3 的 runtimes 构建配好工具链后，用 `clang -E` 预处理一个包含 `OSUtil/syscall.h` 的小文件，观察最终展开出的是哪个架构头（**待本地验证**）。

## 6. 本讲小结

- **OSUtil 是跨 OS 的系统调用抽象层**：每个 OS 一个子目录，顶层 `syscall.h` 用 `#ifdef` 在预处理期挑出当前 OS 的实现，运行期零分支开销；没有 syscall 概念的目标（baremetal/uefi/windows/gpu）则改用 `io.h` 等别的抽象。
- **架构分派有两条平行通道**：源码通道（`#ifdef LIBC_TARGET_ARCH_IS_*`，源于编译器预定义宏）与构建通道（CMake 的 `LIBC_TARGET_OS`/`LIBC_TARGET_ARCHITECTURE`，源于目标三元组），两者各自独立却天然一致。
- **`syscall_impl` 与 `syscall_checked` 是两层封装**：前者是变参模板，把参数转 `long` 后调架构版内联汇编、不做错误检查；后者加一层「返回值是否落在错误区间」的判断并返回 `ErrorOr`。
- **错误返回靠「负返回值即错误码」约定**：内核出错返回 `-errno`（绝对值 ≤ 4095），syscall 层用 `ret >= -MAX_ERRNO`（无符号）或 `ret < 0`（有符号）判错，取反成正 `errno` 包进 `Error`，成功值用 `bit_or_static_cast` 安全转换。
- **syscall wrapper 是薄封装**：`linux/syscall_wrappers/*.h` 每个文件封装一个 syscall，返回 `ErrorOr`，被上层入口点（如 `dup.cpp`）消费，把 `ErrorOr` 翻译成「设 `libc_errno` + 返回 `-1`」的标准 C 语义。
- **设计收益**：分层 + 双通道分派使「换 OS/换架构」只需提供对应目录的实现与配置，业务代码（入口点、wrapper）几乎不动，这正是 u11-l1「移植到新平台」能渐进推进的根基。

## 7. 下一步学习建议

- **u8-l2 程序启动流程**：本讲的 syscall 封装是程序启动后「`do_start` 调内核」的基础，下一讲会看到 startup 代码如何用 OSUtil 设置运行时与 TLS。
- **u8-l3 stdio FILE 模型**：`fopen`/`fwrite` 等带缓冲 I/O 正是建立在本讲的 `open`/`write` syscall wrapper 之上，可以去 `src/stdio/` 验证它们如何 `DEPENDS` 到 `OSUtil.linux.syscall_wrappers.*`。
- **u9-l2 线程与同步原语**：`futex` 同步原语同样通过 OSUtil 的 syscall 机制陷入内核，可作为本讲内容的进阶应用。
- **延伸阅读**：想深入了解某个架构的调用约定，可对照阅读 Linux 内核源码的 `arch/<arch>/entry/syscalls/`；想看完整的 syscall wrapper 清单，浏览 `src/__support/OSUtil/linux/syscall_wrappers/` 目录与它的 `CMakeLists.txt`。
