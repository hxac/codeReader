# u9-l1 libc 与裸机启动

> 本讲面向已经能用 `run_emulator` 跑通 hello_world（见 [[u1-l4]]）、并了解控制寄存器 `getcr`/`setcr`（见 [[u2-l4]]）的读者。我们要回答的问题是：**一句 `printf("Hello World\n")` 在 Nyuzi 上到底是怎么变成屏幕上的文字的？程序又是在哪里、由谁启动起来的？**

## 1. 本讲目标

学完本讲，你应当能够：

- 说清楚一个 Nyuzi 裸机程序从「复位」到「进入 `main`」之间发生了什么，以及为什么入口不是 `main` 而是 `_start`。
- 画出 `printf` 从格式化字符串到 UART 控制寄存器的完整函数调用链，并解释每一层做了什么。
- 解释 `malloc`/`free` 如何借助 dlmalloc 与 `sbrk` 在一块「没有操作系统」的内存上管理堆，以及堆的起始地址从哪里来。
- 理解「裸机（bare-metal）」与「有内核」两种运行方式在启动与 I/O 上的根本区别。

## 2. 前置知识

### 2.1 什么是「裸机」程序

在 PC 上写 C 程序时，你脚下有一整套「宿主环境」：操作系统负责把可执行文件加载进内存、设置栈、调用 C 运行时初始化代码，`printf` 最终通过操作系统的系统调用写到终端。

Nyuzi 的裸机程序没有这层操作系统。CPU 复位后，只有**线程 0** 从地址 `0` 开始取指执行，内存里直接放着你的程序镜像。这意味着：

- 必须有人**自己把栈指针设好**；
- 必须有人**自己把全局构造函数跑完**；
- `printf` 必须**自己找到一条通往外设的路**，而不能依赖 syscall。

这个「有人」就是 C 运行时启动代码 **crt0**（C Run-Time 0）。本讲的 libos 提供了裸机版本 `crt0.S`，kernel 那一侧（[[u12-l1]]）则提供另一套走陷阱的版本——两者职责相同，实现不同。

### 2.2 内存映射 I/O（MMIO）回顾

如 [[u1-l4]] 与 [[u8-l2]] 所述，Nyuzi 用「内存映射 I/O」与外设通信：访问物理地址 `0xffff0000` 以上的区域不会被当作普通内存，而是被路由到外设控制器。UART 的发送数据寄存器就在其中。本讲会看到 `printf` 最终把字符写到这个地址。

### 2.3 控制寄存器回顾

如 [[u2-l4]] 所述，`getcr`/`setcr` 是读写处理器内部状态（而非内存）的指令，需要 supervisor 权限。crt0 用 `getcr s0, 0` 读取「当前线程号」，用 `setcr s0, 20` 让线程停机。本讲会反复用到这两个动作。

## 3. 本讲源码地图

本讲涉及的文件分为两组：**libc**（纯 C 标准库实现）与 **libos-bare**（与硬件/外设打交道的裸机运行时）。

| 文件 | 所属库 | 作用 |
|------|--------|------|
| [software/libs/libos/bare-metal/crt0.S](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/crt0.S) | libos-bare | 启动入口 `_start`：设栈、跑构造函数、调用 `main`、退出 |
| [software/libs/libos/bare-metal/sbrk.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/sbrk.c) | libos-bare | 堆内存分配后端：原子推进 `next_alloc` 指针 |
| [software/libs/libos/bare-metal/uart.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/uart.c) | libos-bare | `_write_uart`/`write_console`：把字节写到 UART 寄存器 |
| [software/libs/libos/bare-metal/registers.h](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/registers.h) | libos-bare | 外设寄存器基地址与索引枚举 |
| [software/libs/libc/src/stdio.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libc/src/stdio.c) | libc | `printf`/`fputc`/`stdout` 等标准 I/O 接口 |
| [software/libs/libc/src/vfprintf.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libc/src/vfprintf.c) | libc | 格式化引擎：解析 `%d`/`%s`/`%x` 等 |
| [software/libs/libc/src/__stdio_internal.h](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libc/src/__stdio_internal.h) | libc | `FILE` 结构体定义 |
| [software/libs/libc/src/dlmalloc.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libc/src/dlmalloc.c) | libc | 第三方通用内存分配器，钩到 `sbrk` |
| [cmake/nyuzi.cmake](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/cmake/nyuzi.cmake) | 构建系统 | `add_nyuzi_executable`：编译、链接到地址 0、转 hex |

> **关键观察**：libc 与 libos-bare 是**两个独立的库**。libc 只含「与平台无关」的逻辑（格式化、分配算法），凡是真正碰硬件的（UART、堆起点）都放在 libos-bare。这就是为什么同一份 libc 既能给裸机用，又能给 kernel 用——只要换一个 libos 变体即可。hello_world 的 [CMakeLists.txt](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/apps/hello_world/CMakeLists.txt) 里 `target_link_libraries(hello_world c os-bare)` 正是同时链接这两者。

---

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**crt0 启动**、**stdio/printf**、**堆分配**。

### 4.1 crt0 启动

#### 4.1.1 概念说明

「程序入口」这个词容易让人误以为是 `main`。其实对于 ELF 可执行文件，真正的入口是链接器写进 ELF 头里的一个符号——在 Nyuzi 上就是汇编里的 `_start`。`main` 只是 `_start` 在做完一堆准备工作后**调用**的一个普通 C 函数。

crt0 要做的准备工作，本质上就是在「让 C 代码能安全运行」之前把环境搭好。一个 C 程序要能跑，至少需要：

1. **一个合法的栈**：函数调用、局部变量都依赖栈指针 `sp` 指向可写内存。
2. **全局对象已初始化**：C++ 的全局对象构造函数、或用 `__attribute__((constructor))` 标注的函数，必须在 `main` 前执行。
3. **参数（argc/argv）**：虽然裸机程序通常没有命令行参数，但调用约定仍要求 `main` 被调用时寄存器状态正确。

另外，复位时**只有线程 0 在跑**，其余硬件线程处于挂起状态（见 [[u1-l4]]、[[u10-l2]]）。线程 0 负责做完所有初始化，之后如果程序需要并行（见 [[u9-l2]]），再由软件显式唤醒其他线程——那些线程被唤醒后也会从 `_start` 进入，但它们**不能再跑一遍初始化**（否则会重复构造全局对象、破坏堆状态）。

#### 4.1.2 核心流程

`_start` 的执行流程可以概括为：

```text
_start（所有线程的入口）
  │
  ├─ 1. 读取自己的线程号：getcr s0, 0
  ├─ 2. 按线程号计算并设置栈指针 sp = 0x200000 - 线程号 × 16KiB
  ├─ 3. 设置全局指针 gp（GOT 基址，用于位置无关寻址）
  │
  ├─ 4. 若线程号 != 0  ──► 跳过初始化，直接 do_main
  │
  ├─ 5.【仅线程 0】遍历 __init_array，逐个调用全局构造函数
  │
  ├─ do_main:
  │      ├─ argc 置 0
  │      └─ call main          ← 这里才进入你写的 C 代码
  │
  └─ main 返回后（退出流程）:
         ├─ 用 load_sync/store_sync 抢占 exit_flag 锁，保证只一个线程做清理
         ├─ 调用 call_atexit_functions（执行 atexit 注册的函数）
         ├─ 向 UART 发送 Ctrl-D（0x04），通知 FPGA 串口宿主程序结束
         └─ setcr -1, CR_SUSPEND_THREAD  ← 停掉本线程，最终所有线程停止
```

每线程栈大小的设计是这里的一个关键点，下面在源码精读中展开。

#### 4.1.3 源码精读

**入口与栈设置**（[software/libs/libos/bare-metal/crt0.S:42-51](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/crt0.S#L42-L51)）：

```asm
_start:
                    getcr s0, 0             // 读取本线程号
                    shl s0, s0, 14          // 左移 14 位 = ×16KiB
                    li sp, 0x200000         // 栈区基址
                    sub_i sp, sp, s0        // 算出本线程栈顶

                    movehi gp, hi(_GLOBAL_OFFSET_TABLE_)
                    or gp, gp, lo(_GLOBAL_OFFSET_TABLE_)
```

- `getcr s0, 0`：控制寄存器 0 即 `CR_THREAD_ID`（见 [[u2-l4]]），返回 `{核号, 线程号}`。在单核配置下，低位的线程号就是 0/1/2/3。
- `shl s0, s0, 14`：把线程号左移 14 位，相当于乘以 \(2^{14}=16384\) 字节，即每线程预留 **16KiB 栈**。
- `sp = 0x200000 - 线程号×16KiB`：四线程栈区从 `0x200000` 向下排布，线程 0 用 `0x1FC000~0x200000`，线程 1 用 `0x1F8000~0x1FC000`，互不重叠。这种「按线程号算栈地址」的方式让**每个硬件线程天生拥有独立栈，切换线程无需保存/恢复 sp**——和寄存器文件按线程分体的思路一致（见 [[u5-l1]]）。
- `gp`（global pointer）指向 GOT，用于访问全局变量。这是位置无关代码（PIC）的标准设置。

**只让线程 0 做初始化**（[software/libs/libos/bare-metal/crt0.S:53-66](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/crt0.S#L53-L66)）：

```asm
                    bnz s0, do_main         // 线程号非 0 直接跳 do_main

                    lea s24, __init_array_start
                    lea s25, __init_array_end
init_loop:          cmpeq_i s0, s24, s25
                    bnz s0, do_main
                    load_32 s0, (s24)       // 取一个构造函数指针
                    add_i s24, s24, 4
                    call s0                 // 调用它
                    b init_loop
```

- `__init_array_start` / `__init_array_end` 是**链接器生成的符号**，分别指向 `.init_array` 段（存放全局构造函数指针的数组）的起止。
- 这个循环就是把数组里每个函数指针取出来 `call` 一遍。对纯 C 的 hello_world，这个数组通常是空的，循环立即结束；对有 C++ 全局对象或 `__attribute__((constructor))` 的程序，这里负责调用它们。
- `bnz s0, do_main` 在最前面把非 0 线程直接放行，确保初始化只执行一次。

**调用 main 与退出**（[software/libs/libos/bare-metal/crt0.S:68-92](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/crt0.S#L68-L92)）：

```asm
do_main:            move s0, 0              // argc = 0
                    call main

                    // 抢占 exit_flag 锁，只让一个线程做清理
                    lea s0, exit_flag
1:                  load_sync s1, (s0)
                    bnz s1, 1b
                    move s1, 1
                    store_sync s1, (s0)
                    bz s1, 1b

                    call call_atexit_functions
                    move s0, 4              // Ctrl-D
                    call _write_uart

                    move s0, -1
                    setcr s0, CR_SUSPEND_THREAD   // CR 20，停机
1:                  b 1b
```

- `move s0, 0; call main`：以 `argc=0` 调用 `main`（无命令行参数）。
- `main` 返回后，用 `load_sync`/`store_sync`（即 LL/SC 原语，见 [[u10-l1]]）对全局 `exit_flag` 做一次原子「测试并置 1」。这样即使有多个线程同时从 `main` 返回，也**只有一个线程**能抢到锁去执行清理，其余线程会在第一个 `bnz s1, 1b` 处自旋——但它们随后也会走到 `setcr` 停机。
- `call_atexit_functions` 执行 `atexit` 注册的析构逻辑。
- `move s0, 4; call _write_uart`：发送 ASCII 0x04（Ctrl-D，END OF TRANSMISSION）。在 FPGA 上，宿主的串口程序读到 Ctrl-D 就知道程序结束了（模拟器环境下这条没有特别意义，但无害）。
- `setcr s0, CR_SUSPEND_THREAD`：`CR_SUSPEND_THREAD` 编号为 20（见文件顶部 [crt0.S:36](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/crt0.S#L36) 的宏定义），写入 -1 表示挂起本线程。这正是 [[u1-l4]] 讲过的停机机制：当所有线程都被挂起，模拟器/仿真器的 `thread_enable_mask` 归零，主循环退出，程序结束。

**内存布局**（[software/libs/libos/bare-metal/crt0.S:24-34](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/crt0.S#L24-L34) 的注释）给出了全局视角：代码/数据从地址 0 开始、栈区在 `0x1F0000~0x200000`、帧缓冲在 `0x200000`、堆在最上方。我们待会在堆分配模块会回到这张图。

#### 4.1.4 代码实践

**目标**：亲眼确认 `_start` 才是真入口，并验证每线程栈地址的计算。

**操作步骤**：

1. 按 [[u1-l2]] 构建 Nyuzi，然后在 `software/apps/hello_world` 构建目录下找到构建系统自动生成的反汇编清单 `hello_world.lst`（由 [cmake/nyuzi.cmake:58-60](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/cmake/nyuzi.cmake#L58-L60) 用 `llvm-objdump` 生成）。
2. 打开 `hello_world.lst`，搜索 `_start`，确认它的地址是 `0`（程序从地址 0 启动）。
3. 对照本讲源码精读，逐行标注：哪几行设栈、哪几行跑构造函数、哪一行 `call main`、哪一行停机。
4. 想要运行验证：执行 `run_emulator`（脚本由 [cmake/nyuzi.cmake:91-92](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/cmake/nyuzi.cmake#L91-L92) 生成），应看到 `Hello World` 输出后程序自动结束（因为 `setcr` 停机）。

**需要观察的现象**：`_start` 位于地址 0；`main` 是被 `_start` 用 `call` 调用的，地址在 `_start` 之后。

**预期结果**：`run_emulator` 打印一行 `Hello World` 后正常退出，无需手动中断。若看不到输出，多半是 libc/libos-bare 没链接进来——检查 `target_link_libraries` 是否含 `c` 与 `os-bare`。

> 说明：本实践为「源码阅读 + 运行验证」型；若本地未搭建工具链，构建与运行步骤标注为「待本地验证」，源码阅读部分可独立完成。

#### 4.1.5 小练习与答案

**练习 1**：为什么非 0 线程必须跳过 `__init_array` 的构造循环？如果不跳过会发生什么？

> **答案**：全局构造函数通常只应执行一次（例如初始化某个全局单例、注册析构钩子）。非 0 线程是在线程 0 已经完成初始化之后才被唤醒的，此时再跑一遍构造会**重复初始化**全局对象、甚至破坏堆或 atexit 链表。所以 crt0 用 `bnz s0, do_main` 让它们直接进 `main`。

**练习 2**：把每线程栈从 16KiB 改成 32KiB，需要改 crt0 里的哪一条指令？同时要注意什么？

> **答案**：把 `shl s0, s0, 14`（×16KiB）改成 `shl s0, s0, 15`（×32KiB）。需要注意栈区基址 `0x200000` 与栈区下限之间要容得下 `线程数 × 32KiB`，否则会与下方的代码/数据段重叠。四线程下需要 128KiB，而栈区只有 `0x1F0000~0x200000` 共 64KiB，会撞上数据段——不能简单改。

---

### 4.2 stdio/printf

#### 4.2.1 概念说明

`printf` 表面上是「把字符串打印出来」，实际上它做了两件性质完全不同的事：

1. **格式化**：把 `printf("x=%d", 42)` 里的 `%d` 占位符替换成 `"42"`，也就是把「多种类型的数据」统一变成「一串字节」。这部分与硬件无关，纯算法。
2. **输出**：把这一串字节送到某个目的地——终端、文件、字符串缓冲区。

标准 C 用 `FILE*` 这个抽象把「目的地」统一起来。Nyuzi 的 libc 把上面两件事拆成两层：`vfprintf` 负责格式化（逐字符产出），`fputc` 负责把单个字符送到 `FILE` 指定的目的地。`printf` 只是 `vfprintf(stdout, ...)` 的薄包装。

这里有一个设计上的巧妙之处：`stdout` 这个 `FILE*` 内部**没有任何缓冲区**（`write_buf = NULL`）。`fputc` 一旦发现目标是 `stdout`，就直接调用 libos 的 `write_console` 走 UART——也就是说，stdout 是「无缓冲、直通外设」的。而 `sprintf`/`snprintf` 则临时构造一个 `write_buf` 指向用户缓冲区的 `FILE`，让 `vfprintf` 把字符写进内存。**同一套 `vfprintf` + `fputc` 代码，因为 `FILE` 内容不同，就能服务三种完全不同的输出目的地。**

#### 4.2.2 核心流程

一次 `printf("a=%d s=%s\n", 42, "hi")` 的旅程：

```text
printf(fmt, ...)
  │  va_start 收集可变参数
  ├─ vfprintf(stdout, fmt, args)
  │     │  状态机扫描 fmt：
  │     │   kScanText   → 普通字符直接 fputc
  │     │   遇到 '%'   → kScanFlags → kScanWidth → kScanPrecision → kScanPrefix → kScanFormat
  │     │   kScanFormat → 按 d/s/x/f... 取一个 va_arg 并转成字符，逐个 fputc
  │     └─ 对每个字符调用 fputc(ch, stdout)
  │
  └─ fputc(ch, stdout)
        ├─ file == stdout ？
        │     是 → write_console(&ch, 1)        ← 进入 libos
        │           └─ 对每个字节 _write_uart(ch)
        │                  └─ REGISTERS[REG_UART_TX] = ch   ← 落到 MMIO 地址 0xffff0048
        │     否 → 写入 file->write_buf（sprintf 的情况）或 write(file->fd,...)
```

#### 4.2.3 源码精读

**printf 与 stdout**（[software/libs/libc/src/stdio.c:25-34](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libc/src/stdio.c#L25-L34) 与 [84-98](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libc/src/stdio.c#L84-L98)）：

```c
int printf(const char *fmt, ...)
{
    va_list arglist;
    va_start(arglist, fmt);
    vfprintf(stdout, fmt, arglist);
    va_end(arglist);
    return 0;
}
```

```c
static FILE __stdout = { .write_buf = NULL, .write_offset = 0, .write_buf_len = 0 };
FILE *stdout = &__stdout;
FILE *stderr = &__stdout;   // stderr 与 stdout 共用同一个对象
FILE *stdin  = &__stdin;
```

注意 `__stdout` 的 `write_buf` 是 `NULL`——这正是「无缓冲、直通外设」的标记，下一处的 `fputc` 会用到它。`stderr` 直接指向 `__stdout`，所以两者输出到同一处。

**fputc 的三路分发**（[software/libs/libc/src/stdio.c:116-132](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libc/src/stdio.c#L116-L132)）：

```c
int fputc(int ch, FILE *file)
{
    if (file == stdout) {                 // ① stdout：直通 UART
        char _ch = ch;
        write_console(&_ch, 1);
    } else if (file->write_buf) {         // ② sprintf/snprintf：写内存缓冲
        if (file->write_offset < file->write_buf_len)
            file->write_buf[file->write_offset++] = ch;
    } else {
        write(file->fd, &ch, 1);          // ③ 普通文件：走 fd（内核态才有效）
    }
    return 1;
}
```

- 分支 ①：目标是 `stdout`，调用 libos 的 `write_console`，最终到 UART。**注意这里用 `file == stdout` 做指针比较**，所以只有传入那个全局 `stdout` 对象才会走 UART。
- 分支 ②：目标有 `write_buf`（如 `sprintf` 构造的 `FILE`，见 [stdio.c:36-51](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libc/src/stdio.c#L36-L51)），就把字符写进缓冲区并推进 `write_offset`。
- 分支 ③：既不是 stdout 也没缓冲，就当作普通文件用 `write(fd, ...)`。这条路径在裸机下通常用不到（没有文件系统 syscall），主要服务 kernel 版本。

`FILE` 结构体本身非常精简（[software/libs/libc/src/__stdio_internal.h:22-28](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libc/src/__stdio_internal.h#L22-L28)）：

```c
struct __file {
    char *write_buf;
    int write_offset;
    int write_buf_len;
    int fd;
};
```

**vfprintf 格式化引擎**（[software/libs/libc/src/vfprintf.c:44-58](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libc/src/vfprintf.c#L44-L58)）是一个手写的状态机：

```c
int vfprintf(FILE *f, const char *format, va_list args)
{
    ...
    enum { kScanText, kScanFlags, kScanWidth,
           kScanPrecision, kScanPrefix, kScanFormat } state = kScanText;

    while (*format) {
        switch (state) {
            case kScanText:
                if (*format == '%') { state = kScanFlags; ... }
                else fputc(*format++, f);     // 普通字符直接输出
                break;
            ...
        }
    }
}
```

格式说明符的语法是 `% [flags] [width] [.precision] [prefix] format`，状态机依次走过 flags/width/precision/prefix，最后在 `kScanFormat` 里按 `d/i/u/x/X/o/s/c/f/g/p` 处理。以整数 `%d` 为例（[software/libs/libc/src/vfprintf.c:142-203](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libc/src/vfprintf.c#L142-L203)）：先用 `va_arg(args, int)` 取出值，处理负号，然后「**倒序**」把各位数字填进一个临时数组（`temp_string[index] = kHexDigits[value % radix]`），再正序 `fputc` 出来——这是把整数转字符串的经典写法。

> 字符串 `%s`（[vfprintf.c:210-228](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libc/src/vfprintf.c#L210-L228)）和浮点 `%f`（[vfprintf.c:230-283](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libc/src/vfprintf.c#L230-L283)）走类似流程。代码注释里坦言浮点实现「简单但有 bug，不处理 inf/NaN」——这与 Nyuzi 浮点硬件本身非完全 IEEE754 兼容（见 [[u5-l3]]）是一致的「够用就行」风格。

**落到 UART**（[software/libs/libos/bare-metal/uart.c:21-37](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/uart.c#L21-L37)）：

```c
void _write_uart(char ch)
{
    while ((REGISTERS[REG_UART_STATUS] & UART_TX_READY) == 0)
        ;                          // 等发送就绪
    REGISTERS[REG_UART_TX] = ch;   // 写发送寄存器
}

int write_console(const char *str, int length)
{
    for (int i = 0; i < length; i++)
        _write_uart(str[i]);
    return 0;
}
```

- `_write_uart` 先轮询 `REG_UART_STATUS` 的就绪位，就绪后把字节写到 `REG_UART_TX`。
- `REGISTERS` 与寄存器索引定义在 [registers.h:21](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/registers.h#L21) 与 [registers.h:33](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/registers.h#L33)：`REGISTERS = (volatile unsigned int*) 0xffff0000`，`REG_UART_TX = 0x0048 / 4`。所以 `REGISTERS[REG_UART_TX]` 实际访问的字节地址是 `0xffff0000 + 0x0048 = 0xffff0048`——这正是 [[u1-l4]] 与 [[u8-l2]] 提到的 UART 输出地址。
- `write_console` 的原型声明在 [software/libs/libos/nyuzi.h:39](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/nyuzi.h#L39)，使得 libc 的 `stdio.c` 能调用 libos-bare 的 `uart.c`。模拟器一侧把这个地址的写入转发到宿主终端的 `putc`（见 [[u8-l2]]），所以你在屏幕上看到了文字。

至此，`printf` 从一行 C 代码到 UART 寄存器的完整链路就闭合了。

#### 4.2.4 代码实践

**目标**：跟踪 `printf("%d %s %x\n", 42, "ok", 255)` 从 `vfprintf` 到 UART 的完整路径，并验证三种格式说明符。

**操作步骤**：

1. 复制 `hello_world.c` 为同目录下一个新文件（例如 `myprintf.c`，并在该 app 的 CMakeLists 里加一个 `add_nyuzi_executable`，链接 `c os-bare`），把 `main` 改成：
   ```c
   #include <stdio.h>
   int main() {
       printf("num=%d word=%s hex=%x\n", 42, "ok", 255);
       return 0;
   }
   ```
2. 构建后查看生成的 `*.lst` 反汇编，确认 `main` 里调用了 `printf`。
3. 在 `vfprintf.c` 的 `case 'd'` 处（[vfprintf.c:142](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libc/src/vfprintf.c#L142)）和 `_write_uart` 的写寄存器处（[uart.c:26](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/uart.c#L26)）各做一次「源码定位」，把它们串成一条链。
4. 运行 `run_emulator`（用新程序的 hex）观察输出。

**需要观察的现象**：输出应为 `num=42 word=ok hex=ff`。注意 `%x` 把 255 打成 `ff`，说明走了 [vfprintf.c:138-151](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libc/src/vfprintf.c#L138-L151) 的 `radix=16` 分支。

**预期结果**：终端打印 `num=42 word=ok hex=ff` 后程序自动结束。若工具链未就绪，构建/运行步骤为「待本地验证」，但源码跟踪可独立完成。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `stdout` 没有缓冲区，但 `sprintf` 写出的字符串却能正确存进用户缓冲区？

> **答案**：因为 `fputc` 用 `file == stdout` 做指针比较分流。`stdout` 指向的 `__stdout` 对象 `write_buf` 为 `NULL`，走 UART 分支；而 `sprintf` 在栈上临时构造一个 `write_buf` 指向用户缓冲区的 `FILE`（[stdio.c:39-43](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libc/src/stdio.c#L39-L43)），它不等于 `stdout`，于是走 `file->write_buf` 分支写内存。同一份 `vfprintf`+`fputc` 因 `FILE` 内容不同而殊途。

**练习 2**：`stderr` 和 `stdout` 是同一个对象吗？这意味着什么？

> **答案**：是。`stderr = &__stdout`（[stdio.c:97](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libc/src/stdio.c#L97)）。意味着向 `stderr` 输出和向 `stdout` 输出走完全相同的路径、到同一个 UART，没有「标准错误流」的分离。

---

### 4.3 堆分配

#### 4.3.1 概念说明

C 程序里 `malloc`/`free` 管理的内存叫**堆**。在托管环境里，堆是一段可以随需向操作系统申请扩展的内存；在 Nyuzi 裸机环境里没有操作系统，但内存是实实在在的 RAM——只要约定好「从哪个地址开始往上长」，就能自己造一个堆。

Nyuzi 的做法是经典的两层分工：

- **dlmalloc**（Doug Lea 的 malloc）是一个成熟的、与平台无关的通用分配器：它维护空闲块链表、处理合并/拆分/对齐，把「给我 N 字节」翻译成对底层内存的精细管理。它是第三方代码，放在 [software/libs/libc/src/dlmalloc.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libc/src/dlmalloc.c)。
- **sbrk** 是 dlmalloc 向平台要内存的**唯一接口**：dlmalloc 需要更多内存时调用 `sbrk(size)`，`sbrk` 返回一段新内存的起始地址。在 Unix 上 `sbrk` 会扩展进程的数据段；在 Nyuzi 上，`sbrk` 只是简单地推进一个全局指针。

这样，dlmalloc 这套复杂算法就被「移植」到了裸机——**移植一个 malloc 到新平台，通常只需要实现 `sbrk` 一个函数**。

#### 4.3.2 核心流程

```text
malloc(100)
  │
  ├─ dlmalloc 在空闲链表里找/切出一个 ≥100 字节的块
  │     ├─ 找到 → 直接返回（不碰 sbrk）
  │     └─ 不够 → 调用 sbrk(大块) 向后扩展
  │
  ├─ sbrk(size)                         ← libos-bare 提供
  │     ├─ old = next_alloc             （原子读取当前指针）
  │     ├─ next_alloc += size           （原子推进）
  │     ├─ memset(old, 0, size)         （清零新内存）
  │     └─ return old
  │
  └─ dlmalloc 用返回的内存补充空闲链表，切出用户要的那块返回
```

关键点：`next_alloc` 是一个全局的「bump pointer（撞针式指针）」，单调递增；dlmalloc 把 sbrk 返回的区域视为**连续（contiguous）**的。

#### 4.3.3 源码精读

**dlmalloc 的钩子**（[software/libs/libc/src/dlmalloc.c:531](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libc/src/dlmalloc.c#L531)）：

```c
#define MORECORE sbrk
```

dlmalloc 内部把「向系统要内存」的函数命名为 `MORECORE`。这一行宏把它定义为我们的 `sbrk`。于是 dlmalloc 每次需要扩容时调用的 `CALL_MORECORE(size)`（见 [dlmalloc.c:1735](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libc/src/dlmalloc.c#L1735)）就是 `sbrk(size)`。移植点仅此一处。

> 该文件版本为 2.8.6，是 Doug Lea 的原版第三方代码。项目在 [libc/CMakeLists.txt:38](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libc/CMakeLists.txt#L38) 用 `-w` 关掉了它的告警，承诺不修改它——所有平台适配都通过 `sbrk` 这个钩子完成。

**sbrk 实现**（[software/libs/libos/bare-metal/sbrk.c:20-29](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/sbrk.c#L20-L29)）：

```c
volatile unsigned int next_alloc = 0x500000;

void *sbrk(ptrdiff_t size)
{
    void *base_ptr = (void*) __sync_fetch_and_add(&next_alloc, size);
    if (size > 0)
        memset(base_ptr, 0, size);
    return base_ptr;
}
```

逐行解读：

- `next_alloc = 0x500000`：堆的起始地址硬编码为 `0x500000`（5 MiB 处）。回顾 crt0 的内存布局注释（代码/数据从 0 起、栈在 `0x1F0000~0x200000`、帧缓冲在 `0x200000` 起），堆放在它们之上的高地址区，互不干扰。每次 `sbrk` 调用，这个指针向前推进 `size` 字节。
- `__sync_fetch_and_add(&next_alloc, size)`：这是 GCC/Clang 内建的**原子「取旧值并加」操作**。它返回加之前的旧值（作为本次分配的基址），同时把 `next_alloc` 推进 `size`。原子性意味着即使多个硬件线程同时 `malloc`，也不会有两个线程拿到同一段内存。在 Nyuzi 上，这个内建会编译成 `load_sync`/`store_sync` 的 LL/SC 循环（见 [[u10-l1]]）——也就是说，sbrk 的线程安全直接建立在硬件同步原语之上。
- `memset(base_ptr, 0, size)`：把新分配的区域清零。这样 `malloc` 返回的内存（在 dlmalloc 未复用旧块、直接来自 sbrk 的情况下）是干净的，避免读到上一个程序的残留数据。
- 返回旧指针 `base_ptr`。

> **为什么是「bump allocator」而不是 mmap？** 裸机下整片 RAM 都是程序独占的，不需要向谁「申请」或「映射」内存，只要约定一个起点线性向上长即可，最简单也最快。代价是堆只增不减（`sbrk` 虽支持负数收缩，但 dlmalloc 在连续模式下很少真正归还）。

#### 4.3.4 代码实践

**目标**：写一个用 `malloc`/`free` 的程序，并推断每次分配如何推动 `next_alloc`。

**操作步骤**：

1. 新建一个链接 `c os-bare` 的程序，`main` 中写：
   ```c
   #include <stdio.h>
   #include <stdlib.h>
   int main() {
       char *p = malloc(100);
       printf("p=%p\n", p);      // 观察地址
       free(p);
       char *q = malloc(100);
       printf("q=%p\n", q);      // free 后再分配，地址可能复用
       return 0;
   }
   ```
2. 构建运行，记录 `p` 和 `q` 的地址。
3. 源码阅读：打开 `sbrk.c`，确认 `next_alloc` 初值是 `0x500000`，理解第一次 `malloc` 触发 `sbrk` 时会从 `0x500000` 附近返回一大块内存给 dlmalloc，dlmalloc 再从中切出 100 字节给 `p`（含 dlmalloc 自己的块头开销）。

**需要观察的现象**：
- `p` 的地址应在 `0x500000` 之上不远处（dlmalloc 首次会一次性向 sbrk 索取比 100 大得多的一块，所以 `p` 接近堆起点）。
- `free(p)` 后再 `malloc` 同样大小，`q` 很可能**等于** `p`——因为 dlmalloc 把刚释放的块放回空闲链表并立即复用，这次根本不调用 `sbrk`。

**预期结果**：两次地址都 ≥ `0x500000`，且第二次大概率复用第一次的地址。若环境未就绪，运行步骤为「待本地验证」；源码侧可独立确认 `next_alloc` 初值与原子推进逻辑。

#### 4.3.5 小练习与答案

**练习 1**：`sbrk` 为什么用 `__sync_fetch_and_add` 而不是普通的 `old = next_alloc; next_alloc += size;`？

> **答案**：为了线程安全。多个硬件线程可能同时 `malloc`（例如 [[u9-l2]] 的并行执行），普通的两条指令之间可能被打断，导致两个线程读到相同的旧值、拿到重叠的内存。`__sync_fetch_and_add` 是原子操作，保证「读旧值 + 加 size」不可分割，每个线程拿到的区间互不重叠。它编译为 LL/SC 原语（见 [[u10-l1]]）。

**练习 2**：如果把 `next_alloc` 的初值改成 `0x100000`（落在栈区/数据区），会发生什么？

> **答案**：堆会与栈或全局数据段重叠——`malloc` 返回的内存可能覆盖栈上的局部变量或全局变量，导致难以排查的内存损坏。这正说明堆起点 `0x500000` 不是随便选的，它必须在所有已用区域（代码/数据、栈、帧缓冲）之上。

---

## 5. 综合实践

把本讲三个模块串起来：写一个程序，**用 `malloc` 分配一个缓冲区，用 `sprintf` 把格式化结果写进缓冲区，再用 `printf` 经 UART 打印出来**，最后跟踪它经过的所有层。

示例代码（链接 `c os-bare`）：

```c
#include <stdio.h>
#include <stdlib.h>

int main()
{
    char *buf = malloc(64);             // ① 堆分配：经 dlmalloc → sbrk → next_alloc 推进
    if (!buf) {
        printf("alloc failed\n");
        return 1;
    }
    sprintf(buf, "val=%d ptr=%p\n", 42, buf);  // ② 格式化写内存：vfprintf→fputc 走 write_buf 分支
    printf("%s", buf);                  // ③ 打印：printf→vfprintf→fputc(stdout)→write_console→_write_uart→0xffff0048
    free(buf);
    return 0;                           // ④ crt0 接管：atexit、Ctrl-D、setcr 停机
}
```

跟踪任务：

1. **启动层**：在生成的 `*.lst` 里找到 `_start`，标注设栈（`getcr`/`shl`/`sub_i`）、构造循环、`call main`、`setcr` 停机各步，确认线程 0 的栈顶是 `0x1FC000`。
2. **格式化层**：对照 `vfprintf.c`，说明 `sprintf` 和 `printf` 都调用同一个 `vfprintf`，但前者构造了带 `write_buf` 的 `FILE`（走内存分支），后者用全局 `stdout`（走 UART 分支）。
3. **输出层**：在 `uart.c` 标出 `REGISTERS[REG_UART_TX]` 这一写操作对应的字节地址 `0xffff0048`，并说明模拟器如何把它转发到宿主终端（[[u8-l2]]）。
4. **堆层**：在 `sbrk.c` 标出 `next_alloc = 0x500000` 与 `__sync_fetch_and_add`，解释 `malloc(64)` 为何最终推动了这个指针。

**交付物**：一张标注了上面四个层次、函数名、关键行号与地址的「调用链图」。

> 提示：如果暂时无法本地构建，可以只做源码标注部分（步骤 1–4 的「标出」动作），所有结论都能从本讲引用的源码中直接得出，不依赖运行结果。

## 6. 本讲小结

- 一个 Nyuzi 裸机程序的真正入口是 crt0 里的 `_start` 而非 `main`；`_start` 负责**按线程号设栈、跑全局构造函数、以 `argc=0` 调用 `main`、返回后做 atexit 清理与停机**，且只让线程 0 执行一次初始化。
- `printf` 的链路是 **`printf → vfprintf（状态机格式化）→ fputc → write_console → _write_uart → REG_UART_TX（0xffff0048）`**；同一套 `vfprintf`+`fputc` 因 `FILE` 内容不同，既能输出到 UART（`stdout`），也能写进内存（`sprintf`）。
- `stdout` 是无缓冲、直通外设的；`fputc` 用 `file == stdout` 做指针比较来分流。
- 堆由 **dlmalloc（通用算法）+ `sbrk`（平台钩子）** 两层构成；移植 malloc 只需实现 `sbrk`。`sbrk` 是一个从 `0x500000` 起、用 `__sync_fetch_and_add` 原子推进的 bump allocator，天然多线程安全。
- **libc（平台无关）与 libos-bare（碰硬件）刻意分离**：换一个 libos 变体（如 kernel 版），同一份 libc 即可用于有操作系统的环境。

## 7. 下一步学习建议

- 想了解「其他硬件线程是怎么被唤醒参与工作的」？继续学习 [[u9-l2 libos 调度与并行执行]]，那里讲解 `parallelExecute` 如何用 `CR_SUSPEND_THREAD` 唤醒其余线程并行跑任务。
- 想了解「有内核时 `printf`/`malloc` 怎么走」？参见 [[u12-l1 内核启动与陷阱/系统调用]] 与 [[u12-l2 内核虚拟内存]]：kernel 版 libos 把 `write`/`sbrk` 实现成 syscall，经陷阱进入内核处理。
- 想深入「sbrk 的原子性依赖的硬件原语」？参见 [[u10-l1 同步内存操作 LL/SC 与 membar]]，那里讲解 `load_sync`/`store_sync` 的 LL/SC 语义。
- 想了解「`printf` 落到的那个 UART 地址在硬件一侧如何工作」？参见 [[u8-l2 模拟器设备与外设仿真]]（模拟器侧）与 [[u14-l2 外设控制器]]（FPGA 硬件侧的 UART 控制器）。
