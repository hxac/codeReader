# libunwind 是什么：项目定位与整体架构

## 1. 本讲目标

本讲是整本《libunwind 学习手册》的第一篇，目标是帮助你建立对 libunwind 的**宏观心智模型**。读完本讲，你应当能够：

- 说出 libunwind 在 LLVM 运行时体系中扮演的角色，以及它解决的「栈展开」问题到底是什么。
- 区分 libunwind 提供的**两层 API**：服务于 C++ 异常的高级 `_Unwind_*` 接口，与服务于通用回溯的低级 `unw_*` 接口。
- 读懂 libunwind 的源码目录结构，并知道用 CMake 如何构建它。
- 在大脑里画出它的「核心引擎」骨架：模板化的 `UnwindCursor` + `AddressSpace`，以及它是如何用一套代码分发到 Compact / SEH / TBTable / DWARF / EHABI 等多种不同展开机制的。

本讲只做「鸟瞰」，不深入任何一种机制的实现细节——那些会在后续讲义中逐一展开。

## 2. 前置知识

在进入源码之前，先用通俗语言铺垫三个概念。

### 2.1 什么是「栈」（Stack）

程序运行时，每调用一个函数，CPU 就会在一块叫「栈」的内存上压入一个**栈帧（stack frame）**，用来保存这个函数的局部变量、返回地址、以及被调用者保存的寄存器。函数返回时，这个栈帧被弹出。

所以「栈」本质上是一条**函数调用链**的轨迹：`main → foo → bar → baz`，越往栈底走，越是「最早被调用」的函数。

### 2.2 什么是「栈展开」（Stack Unwinding）

「栈展开」就是**从当前正在执行的指令出发，沿着栈一帧一帧往上回溯**，重建出每一帧的寄存器状态（尤其是返回地址 PC、栈指针 SP）的过程。它有两类典型用途：

1. **打印调用栈（backtrace）**：程序崩溃或调试时，把 `main → ... → 当前函数` 这条链打印出来，定位「是谁调用了谁」。
2. **C++ 异常处理**：`throw` 之后，运行时需要沿栈往上找哪个函数有 `catch`，并沿途调用析构函数清理对象——这本身就是一个「边展开边清理」的过程。

为什么这件事很难？因为「光看当前寄存器」并不能直接知道上一帧的寄存器。每个函数的栈帧布局各不相同（谁把 `rbp` 压栈了、谁用了帧指针、谁在栈上留了多少局部变量），CPU 不会自动记录这些。**展开器需要额外的「元数据」来描述每个函数如何恢复上一帧的状态**——这正是 libunwind 要处理的核心。

### 2.3 几种「展开元数据」的名字

不同平台/编译器用不同的元数据格式来描述「如何展开一个函数」。你会在源码里反复看到这些名字，先记住它们的大致含义即可：

| 名称 | 全称 / 含义 | 典型平台 |
| --- | --- | --- |
| DWARF CFI | Call Frame Information，最通用的展开信息，存放在 `.eh_frame` / `.debug_frame` 段 | Linux / macOS / FreeBSD 等 |
| Compact Unwind | 苹果设计的紧凑格式，用很少的字节描述一帧 | macOS |
| ARM EHABI | ARM 异常处理 ABI | 裸机 ARM、Linux ARM（非 DWARF EH 时）|
| SEH | Windows 结构化异常处理 | Windows |
| TBTable | AIX 的 traceback table | AIX |
| SjLj | setjmp/longjmp 风格，不依赖栈布局 | iOS ARM 等无表场景 |

本讲不要求你理解它们各自的格式，只需要知道：**libunwind 的复杂度，几乎全部来自「要同时支持这么多套机制」**。

## 3. 本讲源码地图

libunwind 是一个体量很小的运行时库，源码集中在 `src/` 和 `include/` 两个目录。本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [`docs/index.md`](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/libunwind/docs/index.md) | 项目官方说明，明确了两层 API 的定位与支持的平台矩阵。 |
| [`docs/BuildingLibunwind.md`](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/libunwind/docs/BuildingLibunwind.md) | 如何用 CMake 构建 libunwind。 |
| [`include/unwind.h`](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/libunwind/include/unwind.h) | 高级 `_Unwind_*` API 的公共头文件（C++ ABI 标准）。 |
| [`include/libunwind.h`](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/libunwind/include/libunwind.h) | 低级 `unw_*` API 的公共头文件（兼容 HP libunwind）。 |
| [`include/__libunwind_config.h`](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/libunwind/include/__libunwind_config.h) | 编译期配置宏：根据架构选择目标、定义寄存器数量与游标大小。 |
| [`src/libunwind.cpp`](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/libunwind/src/libunwind.cpp) | 低级 `unw_*` 函数的实现入口。 |
| [`src/UnwindLevel1.c`](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/libunwind/src/UnwindLevel1.c) | 高级 `_Unwind_RaiseException` 等的实现（C++ ABI Level 1）。 |
| [`src/UnwindCursor.hpp`](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/libunwind/src/UnwindCursor.hpp) | **核心引擎**：游标对象，记录展开过程中的全部状态，并分发到各种机制。 |
| [`src/AddressSpace.hpp`](https://github.com/llvm/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/libunwind/src/AddressSpace.hpp) | 地址空间抽象：负责「给定一个 PC，帮我找到它对应的展开元数据」。 |
| [`src/CMakeLists.txt`](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/libunwind/src/CMakeLists.txt) | 列出全部编译单元，是理解「到底有几个源文件、怎么拼成库」的最佳入口。 |
| [`test/libunwind_01.pass.cpp`](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/libunwind/test/libunwind_01.pass.cpp) | 低级 API 的端到端测试，本讲代码实践将基于它来理解。 |

> 提示：以后想快速搞清「这个库到底由哪些文件组成」，永远先看 [`src/CMakeLists.txt`](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/libunwind/src/CMakeLists.txt)，它比目录浏览更准确。

## 4. 核心概念与源码讲解

本讲按 4 个最小模块拆分。

### 4.1 libunwind 的定位：它是谁、解决什么问题

#### 4.1.1 概念说明

libunwind 是 LLVM 项目下的一个**运行时子项目（runtime）**，专门实现「栈展开」。它的来源和定位，官方文档讲得很清楚：

> libunwind is an implementation of the interface defined by the HP libunwind project. It was contributed by Apple as a way to enable clang++ to port to platforms that do not have a system unwinder. It is intended to be a small and fast implementation of the ABI.

翻译过来有四个要点：

1. **它实现的是一套既有接口**：高级接口来自 Itanium C++ ABI 的异常处理章节；低级接口来自 HP 公司当年的 libunwind 项目。所以它不是「另起炉灶」，而是「把业界标准接口实现得又小又快」。
2. **它最初由 Apple 贡献**，动机是让 clang++ 能移植到「没有系统级展开器」的平台上。
3. **它是 C++ 异常能工作的底层基石**：你写 `throw`、`catch`，最终都会落到 libunwind（或系统等价物）身上。
4. **它的设计取向是「小而快」**，主动砍掉了一些 HP libunwind 里没真正落地的特性（例如远程展开别的进程）。

在 LLVM 的整体布局里，libunwind 和 `libc++`、`libc++abi`、`compiler-rt` 同属「runtimes」一族。一个典型的关系是：

```
   你的 C++ 程序 (throw / catch / backtrace)
            │
            ▼
   libc++abi   ──►  __cxa_throw / __cxa_begin_catch
            │
            ▼
        libunwind   ◄── 本讲义的主角：真正干「栈展开」的活
            │
            ▼
      操作系统 / CPU 寄存器
```

即：`libc++abi` 负责 C++ 异常的「语义」（对象生命周期、异常对象分配），而真正「沿栈走、定位 catch、恢复寄存器并跳转」的苦力活，由 libunwind 完成。

#### 4.1.2 核心流程

从「为什么需要它」的角度，可以这样描述它存在的意义：

1. 程序在某条指令处触发了 `throw`（或需要 backtrace）。
2. 运行时需要一个工具，能从当前 PC 出发，逐帧重建调用链。
3. 重建每一帧，需要查到「该函数对应的展开元数据」。
4. 不同平台元数据格式不同 → 需要一个统一的展开器适配它们。
5. libunwind 就是这个统一展开器。

#### 4.1.3 源码精读

官方文档直接点明了它的两层 API 划分，这是理解整个项目的钥匙：

```text
The unwinder has two levels of API. The high level APIs are the `_Unwind_*`
functions which implement functionality required by `__cxa_*` exception
functions. The low level APIs are the `unw_*` functions which are an interface
defined by the old HP libunwind project.
```

参见 [docs/index.md:13-16](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/libunwind/docs/index.md#L13-L16)。这段话就是本讲后面所有内容的总纲——记住「两层 API」，你就抓住了 libunwind 的骨架。

文档同时给出了它支持的平台矩阵（哪些 OS × 架构 × 编译器 × 展开信息组合），见 [docs/index.md:37-49](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/libunwind/docs/index.md#L37-L49)。这张表印证了前面说的：「复杂度来自要支持太多平台」。

#### 4.1.4 代码实践

**实践目标**：用眼睛「读」完官方定位，建立第一印象。

**操作步骤**：

1. 打开 [docs/index.md](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/libunwind/docs/index.md)，阅读 Overview 段落。
2. 在仓库本地执行 `git log --oneline -5 -- docs/index.md`，看这个文档最近改过什么（验证它是「活文档」）。

**需要观察的现象**：Overview 里出现的两个关键字 `_Unwind_*` 与 `unw_*`，在后续每一讲都会遇到。

**预期结果**：你能用一句话向别人解释「libunwind 干什么用的」。

#### 4.1.5 小练习与答案

**练习 1**：libunwind 和 libc++abi 在 C++ 异常处理中各负责什么？

> **参考答案**：libc++abi 负责异常的「语义」层——异常对象分配、构造、析构、`catch` 匹配的协调（`__cxa_throw`/`__cxa_begin_catch` 等）；libunwind 负责「机制」层——真正沿调用栈逐帧展开、定位 landing pad、恢复并跳转到寄存器状态。

**练习 2**：官方文档说 libunwind「small and fast」，并主动砍掉了某些特性。被砍掉的典型特性是什么？

> **参考答案**：远程展开（remote unwinding，即展开另一个进程的栈）。文档明确写道 low level API 当初设计成既能 local 又能 remote，但目前只实现了 local 路径，remote 仍是 future work。

---

### 4.2 两层 API：高级 `_Unwind_*` 与低级 `unw_*`

#### 4.2.1 概念说明

libunwind 对外暴露**两套完全不同**的接口，理解它们的分工是读懂源码的前提。

- **高级 API（`_Unwind_*`）**：来自 [Itanium C++ ABI 的异常处理章节](https://itanium-cxx-abi.github.io/cxx-abi/abi-eh.html)。它面向「C++ 异常」这个具体场景，定义了 `_Unwind_RaiseException`、`_Unwind_Resume`、personality function（人格函数）等概念。这套接口被 `libc++abi` 直接调用。声明在 [`include/unwind.h`](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/libunwind/include/unwind.h)。
- **低级 API（`unw_*`）**：来自 HP libunwind 项目，是一套**通用的、面向「游标（cursor）」的回溯接口**。它的典型用法是「拿到一个游标，反复 `unw_step` 走栈，每走一步读寄存器/函数名」。它不绑定 C++ 异常，能用于打印 backtrace、性能采样、崩溃收集等任何需要看调用栈的场合。声明在 [`include/libunwind.h`](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/libunwind/include/libunwind.h)。

一句话区分：**高级 API 服务于「抛异常」，低级 API 服务于「看栈」**。

#### 4.2.2 核心流程

低级 API 的典型使用流程（也是几乎所有 backtrace 工具的套路）：

```
unw_getcontext(&ctx)        # 1. 把当前寄存器快照保存到 ctx
       │
       ▼
unw_init_local(&cursor,&ctx) # 2. 用快照初始化一个游标 cursor
       │
       ▼
   ┌───┴───────────────────┐
   │ unw_step(&cursor) > 0 │  # 3. 每调用一次，游标向上走一帧
   │  读 unw_get_reg /      │  #    可以读取该帧的 PC/SP 等
   │  unw_get_proc_name     │  #    也可以读函数名
   └────────────────────────┘
       │ 走到栈顶返回 0
       ▼
     结束
```

高级 API 的典型流程（C++ `throw` 触发）则是一个**两阶段（two-phase）协议**，本讲只需知道它「分搜索阶段和清理阶段」即可，细节留待后续异常处理讲义：

```
__cxa_throw
   └─► _Unwind_RaiseException
          ├─ phase 1（搜索）：沿栈找哪个帧能 catch，找到返回 _URC_HANDLER_FOUND
          └─ phase 2（清理）：再次沿栈，对每个帧调 personality，沿途执行析构，最终跳到 landing pad
```

#### 4.2.3 源码精读

**（a）低级 API 的声明**。`include/libunwind.h` 里集中声明了 `unw_*` 函数，这是一份极简的「游标操作」接口清单：

```c
extern int unw_getcontext(unw_context_t *) LIBUNWIND_AVAIL;
extern int unw_init_local(unw_cursor_t *, unw_context_t *) LIBUNWIND_AVAIL;
extern int unw_step(unw_cursor_t *) LIBUNWIND_AVAIL;
extern int unw_get_reg(unw_cursor_t *, unw_regnum_t, unw_word_t *) LIBUNWIND_AVAIL;
...
extern int unw_get_proc_name(unw_cursor_t *, char *, size_t, unw_word_t *) LIBUNWIND_AVAIL;
extern unw_addr_space_t unw_local_addr_space;
```

参见 [include/libunwind.h:213-239](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/libunwind/include/libunwind.h#L213-L239)。注意 `unw_local_addr_space`——它代表「本进程的地址空间」，是低级 API 的全局入口。

**（b）低级 API 的实现**。这些函数的实现集中在 `src/libunwind.cpp`，文件头注释一句话点题：

```cpp
//  Implements unw_* functions from <libunwind.h>
```

见 [src/libunwind.cpp:8](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/libunwind/src/libunwind.cpp#L8)。其中 `__unw_init_local` 是把「上下文快照」变成「可走栈的游标」的关键函数——它用 placement new 把一个 `UnwindCursor` 对象直接构造在调用者提供的 `cursor` 缓冲区里：

```cpp
_LIBUNWIND_HIDDEN int __unw_init_local(unw_cursor_t *cursor, unw_context_t *context) {
  ...
  new (reinterpret_cast<UnwindCursor<LocalAddressSpace, REGISTER_KIND> *>(cursor))
      UnwindCursor<LocalAddressSpace, REGISTER_KIND>(
          context, LocalAddressSpace::sThisAddressSpace);
  AbstractUnwindCursor *co = (AbstractUnwindCursor *)cursor;
  co->setInfoBasedOnIPRegister();
  return UNW_ESUCCESS;
}
```

参见 [src/libunwind.cpp:43-94](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/libunwind/src/libunwind.cpp#L43-L94)。这里有一个非常重要的设计：游标的真实类型是 `UnwindCursor<LocalAddressSpace, 某个寄存器类>`，但对外通过基类 `AbstractUnwindCursor` 暴露——我们会在 4.4 节解释为什么这么做。注意 `REGISTER_KIND` 是一个根据当前编译架构（`__x86_64__`、`__aarch64__` 等）在编译期选定的宏，见 [src/libunwind.cpp:48-84](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/libunwind/src/libunwind.cpp#L48-L84)。

**（c）高级 API 的实现**。`src/UnwindLevel1.c` 的注释点明它实现的是 C++ ABI Level 1：

```c
// Implements C++ ABI Exception Handling Level 1 as documented at:
//      https://itanium-cxx-abi.github.io/cxx-abi/abi-eh.html
```

见 [src/UnwindLevel1.c:8-11](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/libunwind/src/UnwindLevel1.c#L8-L11)。其核心入口 `_Unwind_RaiseException` 由 `__cxa_throw` 调用，结构上清晰地分为两阶段：

```c
_Unwind_RaiseException(_Unwind_Exception *exception_object) {
  unw_context_t uc;
  unw_cursor_t cursor;
  __unw_getcontext(&uc);
  ...
  // phase 1: the search phase
  _Unwind_Reason_Code phase1 = unwind_phase1(&uc, &cursor, exception_object);
  if (phase1 != _URC_NO_REASON) return phase1;
  // phase 2: the clean up phase
  return unwind_phase2(&uc, &cursor, exception_object);
}
```

参见 [src/UnwindLevel1.c:466-485](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/libunwind/src/UnwindLevel1.c#L466-L485)。注意：**高级 API 内部复用了低级 API 的 `unw_context_t` / `unw_cursor_t`**——也就是说，两层 API 在底层共用同一套游标机制。这是 libunwind 设计上很优雅的一点。

#### 4.2.4 代码实践

**实践目标**：亲手用低级 API 写一个最小 backtrace，建立「游标」的肌肉记忆。

**操作步骤**：

1. 阅读官方测试 [test/libunwind_01.pass.cpp](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/libunwind/test/libunwind_01.pass.cpp) 里的 `backtrace()` 函数（[第 21-47 行](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/libunwind/test/libunwind_01.pass.cpp#L21-L47)），它就是低级 API 的标准用法模板。
2. 仿照它，写一个独立的 `bt.cpp`（示例代码，非项目原有文件）：

```cpp
// 示例代码：最小 backtrace，使用 libunwind 低级 API
#include <libunwind.h>
#include <stdio.h>

void print_backtrace() {
  unw_context_t ctx;            // 1. 寄存器快照
  unw_cursor_t  cur;            // 2. 游标
  unw_getcontext(&ctx);         // 3. 保存当前寄存器
  unw_init_local(&cur, &ctx);   // 4. 初始化游标

  int n = 0;
  while (unw_step(&cur) > 0) {  // 5. 每步走一帧
    char name[256];
    unw_word_t off = 0;
    if (unw_get_proc_name(&cur, name, sizeof(name), &off) == 0)
      printf("#%d %s+0x%lx\n", n++, name, (long)off);
    else
      printf("#%d ??\n", n++);
  }
}

__attribute__((noinline)) void f() { print_backtrace(); }
__attribute__((noinline)) void g() { f(); }

int main() { g(); }
```

3. 编译链接（需系统装有 libunwind 或用你按 4.3 节构建出的产物）：

```bash
clang++ -O0 -g bt.cpp -lunwind -o bt && ./bt
```

**需要观察的现象**：输出应形如 `#0 print_backtrace+... → #1 f+... → #2 g+... → #3 main+...`，即沿调用链向上回溯。`noinline` 是为了防止编译器把 `f`/`g` 内联进 `main`，导致栈帧被合并、看不到完整的链。

**预期结果**：打印出 3~5 帧调用栈，且顺序与你写的调用关系一致。

> **待本地验证**：实际帧数与符号名取决于你的工具链、链接方式（动态/静态）以及是否启用 `-g`。若看到 `??`，通常是该帧所在函数没有在符号表里（例如动态库内部），并非程序错误。

#### 4.2.5 小练习与答案

**练习 1**：高级 API `_Unwind_*` 和低级 API `unw_*` 分别对应哪个头文件？它们由谁调用？

> **参考答案**：高级 API 在 `include/unwind.h`，由 `libc++abi` 的 `__cxa_*` 调用，服务 C++ 异常；低级 API 在 `include/libunwind.h`，由需要回溯栈的应用代码直接调用，服务通用 backtrace。

**练习 2**：`_Unwind_RaiseException` 的执行分哪两个阶段？为什么需要两个阶段而不是一次走完？

> **参考答案**：分 phase 1（搜索阶段，沿栈找出能 catch 的帧）和 phase 2（清理阶段，再次沿栈，对每帧调 personality 并执行析构，最后跳到 landing pad）。分两阶段是因为：搜索阶段不能真的改变寄存器/栈，只有确认存在 catch 之后，才能安全地开始「边展开边析构」——否则一旦发现没人 catch 就无法回头。

---

### 4.3 源码目录结构与构建方式

#### 4.3.1 概念说明

理解一个库，最快的方式之一是看它的「构建脚本说了它由哪些文件组成」。libunwind 的构建系统是 CMake（唯一支持的配置系统），且它属于 LLVM 的 runtimes，需要通过 `-DLLVM_ENABLE_RUNTIMES=libunwind` 从 monorepo 的 `runtimes` 目录来配置。

libunwind 体量很小，源码全部在 `src/` 下，可粗分为四类：

| 类别 | 代表文件 | 说明 |
| --- | --- | --- |
| C++ 源（`.cpp`）| `libunwind.cpp`、`Unwind-EHABI.cpp`、`Unwind-seh.cpp`、`Unwind_AIXExtras.cpp` | 低级 API 实现、平台特定展开逻辑 |
| C 源（`.c`）| `UnwindLevel1.c`、`UnwindLevel1-gcc-ext.c`、`Unwind-sjlj.c`、`Unwind-wasm.c` | 高级 ABI 接口与 SjLj/Wasm 机制 |
| 汇编（`.S`）| `UnwindRegistersSave.S`、`UnwindRegistersRestore.S` | 寄存器的保存与恢复（无法用 C 完成） |
| C++ 头（`.hpp`）| `UnwindCursor.hpp`、`AddressSpace.hpp`、`DwarfInstructions.hpp` 等 | 核心引擎，几乎全是模板 |

#### 4.3.2 核心流程

libunwind 的标准构建流程（来自官方文档）：

```
git clone https://github.com/llvm/llvm-project.git
cd llvm-project
mkdir build && cd build
cmake -G <generator> -DLLVM_ENABLE_RUNTIMES=libunwind ../runtimes
make unwind          # 构建库
make check-unwind    # 跑测试套件
```

构建产物 `libunwind.so` / `libunwind.a` 会出现在 `build/lib` 下。

#### 4.3.3 源码精读

`src/CMakeLists.txt` 明确列出了全部编译单元，是「源码地图」最权威的来源。C++ 源、C 源、汇编源三组分别定义：

```cmake
set(LIBUNWIND_CXX_SOURCES
    libunwind.cpp
    Unwind-EHABI.cpp
    Unwind-seh.cpp
    )

set(LIBUNWIND_C_SOURCES
    UnwindLevel1.c
    UnwindLevel1-gcc-ext.c
    Unwind-sjlj.c
    Unwind-wasm.c
    )

set(LIBUNWIND_ASM_SOURCES
    UnwindRegistersRestore.S
    UnwindRegistersSave.S
    )
```

参见 [src/CMakeLists.txt:3-33](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/libunwind/src/CMakeLists.txt#L3-L33)。注意一个微妙但重要的设计（注释里专门强调）：

```cmake
# NOTE: avoid implicit dependencies on C++ runtimes.  libunwind uses C++ for
# ease, but does not rely on C++ at runtime.
set(CMAKE_CXX_IMPLICIT_LINK_LIBRARIES "")
```

见 [src/CMakeLists.txt:121-123](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/libunwind/src/CMakeLists.txt#L121-L123)。**libunwind 虽然用 C++ 写，却刻意不依赖 C++ 运行时（libc++）**——因为它自己就是运行时的底层，不能反过来依赖上层。这是阅读它源码时要始终记住的一条铁律（也解释了为什么你会看到自定义的 placement delete、避免使用会抛异常的特性等写法）。

构建选项在 [`docs/BuildingLibunwind.md`](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/libunwind/docs/BuildingLibunwind.md)，常用的有 `LIBUNWIND_ENABLE_SHARED`/`LIBUNWIND_ENABLE_STATIC`（是否构建动态/静态库，默认都 ON）、`LIBUNWIND_ENABLE_THREADS`（线程支持，默认 ON）、`LIBUNWIND_ENABLE_ASSERTIONS`（断言，默认 ON）。

`include/__libunwind_config.h` 是另一块「编译期配置」拼图：它根据当前 CPU 架构（`__x86_64__`、`__aarch64__` 等）定义 `_LIBUNWIND_TARGET_*` 宏、寄存器上限 `_LIBUNWIND_HIGHEST_DWARF_REGISTER`、以及游标/上下文的缓冲区大小 `_LIBUNWIND_CURSOR_SIZE` / `_LIBUNWIND_CONTEXT_SIZE`。例如 x86_64：

```c
# elif defined(__x86_64__)
#  define _LIBUNWIND_TARGET_X86_64 1
...
#  define _LIBUNWIND_CONTEXT_SIZE 21
#  define _LIBUNWIND_CURSOR_SIZE 33
```

参见 [include/__libunwind_config.h:47-63](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/libunwind/include/__libunwind_config.h#L47-L63)。这意味着 `unw_cursor_t` 在不同架构下占用的字节数不同——这正是为什么低级 API 让调用者自己提供 `cursor` 缓冲区，而不是由库内部分配。

#### 4.3.4 代码实践

**实践目标**：用「只读」方式搞清「这个库由几个文件组成、属于 LLVM 哪一部分」。

**操作步骤**：

1. 在仓库本地执行 `git ls-files src/ | wc -l`，统计 `src/` 下的源文件数量（本讲环境下约 20 个文件）。
2. 执行 `git ls-files src/` 浏览全部源文件名，对照本节表格分类。
3. 阅读 [src/CMakeLists.txt:3-33](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/libunwind/src/CMakeLists.txt#L3-L33) 与 [docs/BuildingLibunwind.md:9-40](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/libunwind/docs/BuildingLibunwind.md#L9-L40)。

**需要观察的现象**：你会看到源文件按「C++ / C / 汇编」严格分组；每组文件都对应一种或一类机制（如 `Unwind-seh.cpp` 对应 Windows SEH，`Unwind-sjlj.c` 对应 SjLj）。

**预期结果**：你能在不看答案的情况下，说出 libunwind 大约由哪些文件组成、用 CMake 如何构建。

> **待本地验证**：是否真正执行 `make unwind` 取决于你的环境是否已装好 CMake 与 Clang；本实践以「读懂构建脚本」为主，不强求实际编译。

#### 4.3.5 小练习与答案

**练习 1**：libunwind 用 C++ 编写，却为什么刻意 `set(CMAKE_CXX_IMPLICIT_LINK_LIBRARIES "")`？

> **参考答案**：为了避免对 C++ 运行时（如 libc++、libc++abi）产生隐式链接依赖。libunwind 处于运行时栈的最底层（连 C++ 异常都靠它实现），不能反过来依赖上层运行时，否则会形成循环依赖。所以它「用 C++ 的便利，但不依赖 C++ 的运行时」。

**练习 2**：`UnwindRegistersSave.S` 和 `UnwindRegistersRestore.S` 为什么必须用汇编，而不是 C？

> **参考答案**：保存/恢复全部寄存器（包括那些调用约定规定可被破坏的寄存器）需要逐个寄存器操作，C 语言无法在不破坏自身寄存器的前提下完成「把当前所有寄存器原样写入内存」或「从内存原样恢复并跳转」这类动作——这些只能用汇编精确控制。这也是 `jumpto()`、`unw_getcontext` 等底层函数落在汇编文件里的原因。

---

### 4.4 核心引擎：模板化 UnwindCursor 与多机制分发

#### 4.4.1 概念说明

如果说本讲只能带走一个知识点，那就是这一节：**libunwind 的「脊椎」是 `UnwindCursor`**。

「游标（cursor）」是展开过程中的全部状态：当前走到哪一帧、这一帧每个寄存器的值、对应的展开信息是什么。无论你用高级 API 抛异常，还是用低级 API 走栈，最终都是在操作一个游标。

libunwind 面对的难题是：它要支持 15+ 种 CPU 架构、5 套完全不同的展开机制。如果为每种组合单独写一份代码，代码量会爆炸。它的解法是 **C++ 模板**：把「与架构相关的部分」（寄存器集合 `R`）和「与地址空间相关的部分」（`A`）抽成模板参数，于是核心逻辑只写一份：

```
UnwindCursor<地址空间 A, 寄存器集合 R>
```

例如 `UnwindCursor<LocalAddressSpace, Registers_x86_64>` 就是一个「在本进程地址空间里、针对 x86_64 寄存器」的游标。`Registers_arm64`、`Registers_riscv` 等都是 `R` 的不同实例。

#### 4.4.2 核心流程

游标向上走一帧（`step()`）是整个库最核心的动作。它的主分发逻辑可以用伪代码概括：

```text
step(cursor):
    若已找不到展开信息(到栈底): 返回 END
    根据「编译期选定的机制」分发：
        选了 COMPACT  → stepWithCompactEncoding()   # 苹果紧凑格式
        选了 SEH      → stepWithSEHData()           # Windows
        选了 TBTAB    → stepWithTBTableData()       # AIX
        选了 DWARF    → stepWithDwarfFDE()          # 通用 .eh_frame
        选了 EHABI    → stepWithEHABI()             # ARM
    若成功: 更新 PC 信息，准备走下一帧
```

而在「走一帧」之前，必须先知道「当前这一帧的展开信息在哪」——这正是 `setInfoBasedOnIPRegister()` 的职责：给定当前 PC，去地址空间里查它对应的展开段（`.eh_frame` 等）。

整个展开可以浓缩成一个再细一层的概念公式。对每一帧，展开器都要算出一个**规范帧地址 CFA（Canonical Frame Address）**，它就是「调用者栈指针在被调用者里的位置」：

\[
\mathrm{CFA}_{\text{当前帧}} = f(\text{寄存器}, \mathrm{PC})
\]

其中 \(f\) 完全由该函数的展开元数据（DWARF CFI 等）描述。得到 CFA 后，调用者的每个寄存器都可表达为「相对 CFA 的某个偏移」或「等于当前某个寄存器」，于是上一帧的全部状态可被恢复。本讲只需建立这个直觉，具体 \(f\) 的计算（CFA 规则、寄存器恢复规则）会在 DWARF 专题讲义中详解。

#### 4.4.3 源码精读

**（a）抽象基类**。游标对外的统一接口由抽象基类 `AbstractUnwindCursor` 定义，它声明了一组虚函数（`step`、`getReg`、`setReg`、`jumpto`、`setInfoBasedOnIPRegister` 等），默认实现都是「abort」——强制子类去 override：

```cpp
class _LIBUNWIND_HIDDEN AbstractUnwindCursor {
  ...
  virtual int step(bool = false) { _LIBUNWIND_ABORT("step not implemented"); }
  ...
  virtual void setInfoBasedOnIPRegister(bool = false) {
    _LIBUNWIND_ABORT("setInfoBasedOnIPRegister not implemented");
  }
  ...
};
```

参见 [src/UnwindCursor.hpp:454-515](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/libunwind/src/UnwindCursor.hpp#L454-L515)。回顾 4.2 节 `libunwind.cpp` 里把 `cursor` 强转为 `AbstractUnwindCursor *` 来调用——就是这个基类在起作用：**对外只暴露抽象接口，对内用模板特化提供具体实现**。

**（b）模板化具体游标**。`UnwindCursor` 是模板类，并且根据平台有**两个特化版本**——一个用于 Windows SEH（`_LIBUNWIND_SUPPORT_SEH_UNWIND && _WIN32`），一个用于其余所有平台（DWARF / Compact / EHABI / TBTable）：

```cpp
// 版本一：Windows SEH
template <typename A, typename R>
class UnwindCursor : public AbstractUnwindCursor { ... };   // 见 :522

// 版本二：其余平台（DWARF/Compact/EHABI/TBTable）
template <typename A, typename R>
class UnwindCursor : public AbstractUnwindCursor { ... };   // 见 :966
```

参见 [src/UnwindCursor.hpp:522](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/libunwind/src/UnwindCursor.hpp#L522) 与 [src/UnwindCursor.hpp:966](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/libunwind/src/UnwindCursor.hpp#L966)。第二个版本里你能看到 `step`、`setInfoBasedOnIPRegister` 等都作为虚函数被重新声明（[第 978、984 行](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/libunwind/src/UnwindCursor.hpp#L978-L984)）。

**（c）`step()` 主分发**。这是整本手册最重要的函数之一，它根据编译期宏分发到不同机制：

```cpp
template <typename A, typename R> int UnwindCursor<A, R>::step(bool stage2) {
  (void)stage2;
  // Bottom of stack is defined when unwind info cannot be found.
  if (_unwindInfoMissing) return UNW_STEP_END;
  int result;
  ...
  {
#if defined(_LIBUNWIND_SUPPORT_COMPACT_UNWIND)
    result = this->stepWithCompactEncoding(stage2);
#elif defined(_LIBUNWIND_SUPPORT_SEH_UNWIND)
    result = this->stepWithSEHData();
#elif defined(_LIBUNWIND_SUPPORT_TBTAB_UNWIND)
    result = this->stepWithTBTableData();
#elif defined(_LIBUNWIND_SUPPORT_DWARF_UNWIND)
    result = this->stepWithDwarfFDE(stage2);
#elif defined(_LIBUNWIND_ARM_EHABI)
    result = this->stepWithEHABI();
#else
  #error Need ... COMPACT ... SEH ... DWARF ... EHABI
#endif
  }
  ...
}
```

参见 [src/UnwindCursor.hpp:3420-3451](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/libunwind/src/UnwindCursor.hpp#L3420-L3451)。这段 `#if/#elif` 就是「一套代码适配多机制」的总开关。`#error` 兜底也很有意思：编译期就保证你至少选定了一种机制，否则直接编译失败。

**（d）`setInfoBasedOnIPRegister()`：查展开段**。走帧前要先定位展开信息。该函数取出当前 PC，做必要的修正（如作为返回地址时回退一字节、信号帧加一、ARM 去掉 thumb 位），然后委托「地址空间」去查找展开段：

```cpp
void UnwindCursor<A, R>::setInfoBasedOnIPRegister(bool isReturnAddress) {
  ...
  typename R::reg_t rawPC = this->getReg(UNW_REG_IP);
  ...
  if (isReturnAddress)
    --pc;                       // 返回地址回退一字节，落到所属函数内
  ...
  // Ask address space object to find unwind sections for this pc.
  UnwindInfoSections sects;
  if (_addressSpace.template findUnwindSections<R>(pc, sects)) {
#if defined(_LIBUNWIND_SUPPORT_COMPACT_UNWIND)
    // 若有 compact 表，先查 compact ...
```

参见 [src/UnwindCursor.hpp:2889-2948](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/libunwind/src/UnwindCursor.hpp#L2889-L2948)。注意第 2942 行的 `findUnwindSections`——它就是「地址空间」抽象的入口。

**（e）地址空间抽象**。`AddressSpace.hpp` 定义了 `LocalAddressSpace` 和 `UnwindInfoSections`，职责是「给定 PC，定位它所在的 `.eh_frame` / compact 表等段」。`findUnwindSections` 在 Linux 上借助 `dl_iterate_phdr` 遍历已加载的共享对象来定位段：

```cpp
struct UnwindInfoSections { ... };          // 描述找到的各展开段
...
bool findUnwindSections(... targetAddr, UnwindInfoSections & ...);
```

参见 [src/AddressSpace.hpp:126-127](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/libunwind/src/AddressSpace.hpp#L126-L127)（结构体定义）、[src/AddressSpace.hpp:209](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/libunwind/src/AddressSpace.hpp#L209)（声明）以及 `dl_iterate_phdr` 辅助 [src/AddressSpace.hpp:456](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/libunwind/src/AddressSpace.hpp#L456)。`AbstractUnwindCursor` ↔ `UnwindCursor<A,R>` ↔ `AddressSpace` 三者的协作，就是 libunwind 的全部核心。

#### 4.4.4 代码实践

**实践目标**：跟踪一条「主分发」调用链，确认 4.4 节的架构描述与真实代码一致。

**操作步骤**：

1. 在仓库本地用搜索工具定位 `UnwindCursor<A, R>::step` 的定义（应在 `src/UnwindCursor.hpp` 约 3420 行）。
2. 阅读其 `#if/#elif` 分发块（[3435-3450 行](https://github.com/llvm/llvm-project/blob/abe5aa5cb2336f32dca8e765710ec212a926476c/libunwind/src/UnwindCursor.hpp#L3435-L3450)）。
3. 接着定位 `setInfoBasedOnIPRegister`（约 2889 行），找到它调用 `findUnwindSections` 的那一行（约 2942 行）。
4. 跳到 `src/AddressSpace.hpp` 看 `findUnwindSections` 的实现，确认它最终会调用 `dl_iterate_phdr`（Linux）等系统机制来定位段。

**需要观察的现象**：你会清楚地看到「游标 → 地址空间 → 操作系统加载器信息」这条依赖链，以及 `step()` 如何在不同机制间分发。

**预期结果**：你能画出下面这张依赖图：

```
unw_step / _Unwind_RaiseException
        │ （都是 AbstractUnwindCursor 虚函数）
        ▼
UnwindCursor<A, R>            ← 模板核心，A=地址空间, R=寄存器集合
   ├── setInfoBasedOnIPRegister() ──► AddressSpace::findUnwindSections() ──► OS (dl_iterate_phdr)
   └── step()
         ├── stepWithCompactEncoding()  ──► CompactUnwinder
         ├── stepWithDwarfFDE()         ──► DwarfInstructions / CFI_Parser
         ├── stepWithSEHData()          ──► Windows SEH
         ├── stepWithTBTableData()      ──► AIX TBTable
         └── stepWithEHABI()            ──► ARM EHABI
```

#### 4.4.5 小练习与答案

**练习 1**：`UnwindCursor` 为什么被设计成模板 `UnwindCursor<A, R>`，而不是普通类？

> **参考答案**：因为不同 CPU 架构的寄存器集合（`R`）完全不同，不同运行环境（本进程 / 未来的远端进程）的地址空间访问方式（`A`）也不同。用模板把这两者参数化，可以让「走帧、查信息」这套与具体架构无关的核心逻辑只写一份，由编译器在编译期为每种 `A×R` 组合生成一份高效的特化代码，既复用又零运行期开销（符合「小而快」的目标）。

**练习 2**：`AbstractUnwindCursor` 的虚函数默认实现为什么都是 `_LIBUNWIND_ABORT(...)`？

> **参考答案**：这是一种「接口契约」的强制手段：抽象基类只定义接口，不提供真正实现；任何未被具体子类 override 的方法被调用时，都会触发 abort，从而在开发期立即暴露「某个具体游标忘了实现某个接口」的错误，而不是悄悄返回错误结果。

**练习 3**：`setInfoBasedOnIPRegister` 在把 PC 交给地址空间查找前，对 `isReturnAddress==true` 的 PC 做了什么修正？为什么？

> **参考答案**：执行了 `--pc`（AIX 下是 `pc -= 4`）。原因：当 PC 是「函数调用后的返回地址」时，它指向 `call` 指令的**下一条**指令，可能恰好是下一个函数的起始地址，导致查到错误的展开信息。回退一字节能让 PC 落回「发起调用的那个函数」内部，从而正确匹配到它对应的展开表项。

## 5. 综合实践

把本讲的四个模块串起来，完成下面这个「全链路阅读 + 动手」任务：

**任务**：用本讲学到的「两层 API + 核心引擎」知识，解释一个真实的 backtrace 是如何发生的。

1. **写代码**：基于 4.2.4 节的示例，写一个调用层次为 `main → a() → b() → print_bt()` 的程序，在 `print_bt()` 里用低级 API 打印调用栈。
2. **对应源码**：对着你的程序，逐条对应 libunwind 内部发生了什么：
   - `unw_getcontext` → 对应寄存器快照保存（汇编文件 `UnwindRegistersSave.S`）。
   - `unw_init_local` → 对应 `src/libunwind.cpp` 的 `__unw_init_local`，构造 `UnwindCursor` 并调 `setInfoBasedOnIPRegister`。
   - 每次 `unw_step` → 对应 `UnwindCursor::step` 的 `#if/#elif` 分发，并经 `AddressSpace::findUnwindSections` 定位展开段。
3. **画出依赖图**：把你这个具体程序，套进 4.4.4 节那张依赖图，标出「我的 `b()` 这一帧」会走到哪条分支（绝大多数 Linux/macOS x86_64 走 DWARF；macOS 可能先走 Compact）。
4. **验证**：运行程序，确认输出的帧序与 `main → a → b → print_bt` 一致。

> **待本地验证**：如果你的平台是 macOS 且函数位于系统库，可能优先命中 Compact Unwind 而非 DWARF；如果你的程序开了优化，部分帧可能因尾调用/内联而缺失。这些都是真实展开器要处理的复杂性，后续讲义会逐一覆盖。

完成本任务后，你就拥有了「从一个 backtrace 现象，一路对应到 libunwind 源码」的能力——这正是后续深入学习每一种机制（DWARF、Compact、SEH、EHABI……）的起点。

## 6. 本讲小结

- libunwind 是 LLVM 的运行时子项目，专门做**栈展开**；它是 C++ 异常能工作、以及各种 backtrace/采样工具能拿到调用栈的底层基石。
- 它对外有**两层 API**：高级 `_Unwind_*`（在 `unwind.h`，服务 C++ 异常，由 libc++abi 调用）和低级 `unw_*`（在 `libunwind.h`，服务通用回溯），二者在底层共用同一套游标机制。
- 它**用 C++ 写但不依赖 C++ 运行时**，源码体量很小（`src/` 约 20 个文件），用 CMake 构建，源码清单最权威的来源是 `src/CMakeLists.txt`。
- 它的**核心引擎是模板化的 `UnwindCursor<A, R>`**：通过模板参数 `A`（地址空间）和 `R`（寄存器集合）适配 15+ 架构，通过 `step()` 里的编译期 `#if/#elif` 分发到 Compact / SEH / TBTable / DWARF / EHABI 等多套机制。
- `AbstractUnwindCursor` 提供统一对外接口，`AddressSpace` 负责「给定 PC 定位展开段」，二者加上 `UnwindCursor` 构成 libunwind 的「脊椎」。
- 平台/架构的编译期选择集中在 `include/__libunwind_config.h`（`_LIBUNWIND_TARGET_*`、游标大小等）。

## 7. 下一步学习建议

本讲是「鸟瞰」，接下来建议沿**主链路**纵向深入。推荐的下一步：

1. **先吃透本地回溯主链路**：阅读 `src/libunwind.cpp` 的 `__unw_init_local` → `src/UnwindCursor.hpp` 的 `setInfoBasedOnIPRegister` 与 `step` → `src/AddressSpace.hpp` 的 `findUnwindSections`，把本讲的依赖图在源码里走通一遍。对应后续「UnwindCursor 主链路」相关讲义。
2. **再深入最常见的 DWARF CFI 机制**：从 `stepWithDwarfFDE` 进入 `src/DwarfInstructions.hpp` 与 `src/DwarfParser.hpp`，理解 CIE/FDE 如何描述 CFA 规则与寄存器恢复。这是理解整个库的「脊椎」级知识。
3. **横向对比其他机制**：在掌握 DWARF 后，再去看 Compact（`src/CompactUnwinder.hpp`）、ARM EHABI（`src/Unwind-EHABI.cpp`）、Windows SEH（`src/Unwind-seh.cpp`）的差异，体会「同一套模板引擎如何适配不同元数据格式」。

> 阅读源码时，随时回头对照本讲的依赖图与两层 API 划分——它们是你不迷路的地图。
