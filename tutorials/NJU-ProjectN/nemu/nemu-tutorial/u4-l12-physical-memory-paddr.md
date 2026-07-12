# 物理内存 paddr

## 1. 本讲目标

NEMU 是一台「全系统模拟器」，它要给客机程序（guest program）提供一段看得见、摸得着的「物理内存」。本讲只聚焦这一段物理内存是如何被模拟出来的。

学完本讲，你应当能够：

- 说清 **客机物理地址**（guest physical address）和 **宿主机虚拟地址**（host virtual address）的区别，并能用 `guest_to_host` 在两者间换算。
- 解释 `pmem` 这块物理内存「后端」的两种实现方式（全局数组 / `malloc`）以及它们的取舍。
- 读懂 `paddr_read` / `paddr_write` 的 **三分支路由**：物理内存、MMIO 设备、越界报错。
- 理解 `host_read` / `host_write` 如何按 1/2/4/8 字节宽度读写，以及 `len` 取其它值时的运行时检查。
- 掌握 `init_mem` 的初始化逻辑，特别是 `CONFIG_MEM_RANDOM` 为什么要用随机字节填满内存。
- 解释 `in_pmem` 为什么只用一次无符号减法 + 一次比较就能判断地址是否落在物理内存区间内。

本讲是整个「内存系统」单元（U4）的地基：下一讲 u4-l13 会在这之上叠加虚拟内存 `vaddr` 与 MMU 接口，而它们最终都会落到本讲的 `paddr_read` / `paddr_write` 上。

## 2. 前置知识

在进入源码前，先用大白话把几个概念讲清楚。

**客机与宿主。** NEMU 自己是一个跑在你电脑上的普通 Linux 程序（宿主程序）。而它内部模拟的那台「虚拟计算机」里运行的程序，叫做客机程序。客机程序以为自己在真正的硬件上运行，它看到的内存地址是「物理地址」；但对宿主机来说，这些地址不过是 NEMU 进程里某段普通字节数组的下标。

**内存就是一段连续的字节数组。** 真实 DRAM 的细节（刷新、行列地址、ECC）在 NEMU 里全部被抽象掉，物理内存被建模成一个 `uint8_t` 数组。读一个字节就是取数组元素，写一个字节就是赋值。多字节读写靠指针类型转换一次完成。

**物理地址有一个「基址」。** 以 RISC-V 为例，客机程序认为内存从 `0x80000000` 开始（这是许多 RISC-V 平台的约定，`0x80000000` 以上是 RAM）。但宿主机里的数组下标是从 `0` 开始的。所以存在一个固定的偏移 `CONFIG_MBASE`（memory base），把客机物理地址减去这个基址，才得到数组下标。

**MMIO（Memory-Mapped I/O，内存映射 I/O）。** 真实硬件里，有些物理地址并不对应 RAM，而是对应设备寄存器：读写这些地址等于在跟设备（串口、定时器、键盘、VGA）通信。这就是 MMIO。所以一次物理地址访问可能命中三种情况：真内存、设备、或者根本不存在（越界）。

**无符号整数减法会回绕。** C 语言里无符号整数（`uint32_t` 等）做减法，若结果为「负」并不会变成负数，而是回绕成一个巨大的正数（模运算）。本讲的 `in_pmem` 正是利用这一点，把「两次比较」压成「一次减法 + 一次比较」。后面会详细推导。

如果你还没读过 u1-l3（启动流程）和 u1-l4（ISA 抽象层），建议先看：本讲的 `init_mem` 是启动链的一环，`paddr_t` / `word_t` 这些类型宽度也来自 u1-l4 讲过的 `common.h`。

## 3. 本讲源码地图

本讲主要围绕三个文件，并涉及若干周边文件：

| 文件 | 作用 |
| --- | --- |
| [src/memory/paddr.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/paddr.c) | 物理内存的核心实现：`pmem` 后端、`guest_to_host`、`paddr_read/write`、`init_mem`。 |
| [include/memory/paddr.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/memory/paddr.h) | 物理内存的对外接口：区间常量 `PMEM_LEFT/RIGHT`、`RESET_VECTOR`、`in_pmem`、`guest_to_host` 声明。 |
| [include/memory/host.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/memory/host.h) | 在宿主机指针上按宽度读写：`host_read` / `host_write`。 |
| [src/device/io/mmio.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/io/mmio.c) | `mmio_read` / `mmio_write`，物理地址未命中内存时的设备分流去向（u6-l18 详讲）。 |
| [src/monitor/monitor.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/monitor.c) | `init_monitor` 调用 `init_mem()`，`load_img` 用 `guest_to_host(RESET_VECTOR)` 把镜像烧进内存。 |
| [src/isa/riscv32/init.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/init.c) | `init_isa` 把内置镜像 `memcpy` 到 `guest_to_host(RESET_VECTOR)`，演示了「客机地址 → 宿主指针」的典型用法。 |
| [src/memory/Kconfig](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/Kconfig) | 定义 `CONFIG_MBASE` / `CONFIG_MSIZE` / `CONFIG_PC_RESET_OFFSET` / `CONFIG_PMEM_MALLOC` / `CONFIG_MEM_RANDOM` 等开关。 |
| [include/common.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/common.h) | 定义 `word_t` / `paddr_t` 等基本类型宽度。 |

## 4. 核心概念与源码讲解

### 4.1 物理内存后端 pmem：数组与 malloc 两种实现

#### 4.1.1 概念说明

物理内存需要一个「后端」，也就是真正存放字节的存储区。NEMU 提供了两种等价的后端：

- **全局数组（global array）**：直接声明一个 `uint8_t pmem[CONFIG_MSIZE]` 的静态全局数组，编译期就分配好。
- **malloc**：运行时用 `malloc(CONFIG_MSIZE)` 在堆上申请。

两者只是「这块字节从哪来」不同，对外的读写接口完全一样。选择哪种由 Kconfig 开关 `CONFIG_PMEM_GARRAY`（默认）与 `CONFIG_PMEM_MALLOC` 决定。

**为什么要有两种？** 全局数组放在 BSS 段，会被加载器自动清零，访问稳定但大小受限于编译期常量；`malloc` 更灵活（理论上可以模拟超大内存，只要宿主机内存够），但需要手动初始化。教学上，两种后端让学生理解「内存不过是一段连续字节」，实现细节可以替换。

#### 4.1.2 核心流程

1. 编译期：根据 `CONFIG_PMEM_MALLOC` 是否定义，二选一声明 `pmem`（指针或数组）。
2. 启动期：若用 `malloc` 后端，`init_mem` 调用 `malloc(CONFIG_MSIZE)` 申请并 `assert` 非空；若用数组后端，这一步什么都不做（数组已存在）。
3. 之后所有访问都通过 `pmem` 这个名字，无需关心它到底是数组还是指针。

#### 4.1.3 源码精读

后端声明用条件编译二选一：

[src/memory/paddr.c:L21-L25](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/paddr.c#L21-L25) —— `pmem` 的两种后端：`CONFIG_PMEM_MALLOC` 时是指针，否则是 `PG_ALIGN` 对齐的全局数组。

注意数组分支的 `PG_ALIGN`：

[include/macro.h:L93](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/macro.h#L93) —— `PG_ALIGN` 展开为 `__attribute((aligned(4096)))`，让数组起始地址按 4KB 页边界对齐。这在后续做分页（u7-l22）时能让物理内存的页边界与宿主机页边界对齐，便于理解和调试。

内存大小由 `CONFIG_MSIZE` 控制：

[src/memory/Kconfig:L8-L10](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/Kconfig#L8-L10) —— `CONFIG_MSIZE` 默认 `0x8000000`，即 128 MB。后端选择则在：

[src/memory/Kconfig:L17-L25](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/Kconfig#L17-L25) —— `PMEM_GARRAY`（默认，全局数组）与 `PMEM_MALLOC` 二选一；注意全局数组分支还 `depends on !TARGET_AM`（AM 模式必须用 malloc，因为 AM 运行时环境不保证大段 BSS 可用）。

#### 4.1.4 代码实践

**目标：** 直观感受「内存就是一段字节数组」。

**步骤：**

1. 打开 [src/memory/paddr.c:L21-L25](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/paddr.c#L21-L25)，确认当前默认走的是 `#else` 分支（全局数组）。
2. 在 `init_mem` 的 `Log(...)` 之前临时加一行打印（**示例代码，仅供观察，验证后请还原**）：

   ```c
   printf("[debug] pmem host addr = %p, MSIZE = 0x%x\n", (void *)pmem, CONFIG_MSIZE);
   ```

3. 重新 `make` 并运行内置镜像（注意先按 u1-l3 的要求删掉 `welcome()` 里的 `assert(0)`），观察打印的宿主机地址。

**需要观察的现象：** `pmem` 是一个具体的宿主机虚拟地址，`MSIZE = 0x8000000`。

**预期结果：** 你会看到类似 `pmem host addr = 0x55xxxx, MSIZE = 0x8000000` 的输出，说明这块 128 MB 的字节数组确实存在于 NEMU 进程的地址空间里。

> 本地验证提示：实际地址因 ASLR 每次不同。验证完记得删除调试打印，避免污染后续讲义的行为。

#### 4.1.5 小练习与答案

**练习 1：** 为什么 `PMEM_GARRAY` 要 `depends on !TARGET_AM`，而 `PMEM_MALLOC` 不需要？

**参考答案：** AM（抽象机）模式下，NEMU 被编译成跑在 AM 运行时上的应用，AM 不一定提供大段静态 BSS 的可靠清零与映射；而 `malloc` 走 AM 提供的堆分配接口（`klib`），更可控。所以在 AM 模式只能用 `malloc` 后端。

**练习 2：** 全局数组后端时，未初始化的 `pmem` 内容是什么？为什么？

**参考答案：** 是全 0。因为它是带 `= {}` 初始化的静态存储期对象，按 C 标准会被零初始化，并且放在 BSS 段由加载器清零。这一点和 `malloc` 后端（内容未定义）不同，正是 `CONFIG_MEM_RANDOM` 存在的原因之一（见 4.5）。

---

### 4.2 guest_to_host / host_to_guest：客机物理地址 ↔ 宿主机指针

#### 4.2.1 概念说明

`pmem` 数组的下标从 `0` 开始，但客机程序以为内存从 `CONFIG_MBASE`（如 `0x80000000`）开始。于是需要一对换算函数：

- `guest_to_host(paddr)`：给定客机物理地址，返回对应的宿主机指针（指向 `pmem` 数组里的某个字节）。
- `host_to_guest(haddr)`：反向，给定 `pmem` 内部的指针，算出它对应的客机物理地址。

客机物理地址空间中，真正对应 RAM 的区间是 \([\text{CONFIG\_MBASE},\ \text{CONFIG\_MBASE}+\text{CONFIG\_MSIZE})\)。换算关系就是简单的一次减法/加法。

#### 4.2.2 核心流程

地址换算的数学关系（以无符号算术表示）：

\[ \text{haddr} = \text{pmem} + (\text{paddr} - \text{CONFIG\_MBASE}) \]

\[ \text{paddr} = (\text{haddr} - \text{pmem}) + \text{CONFIG\_MBASE} \]

示意（以 RISC-V 默认 `CONFIG_MBASE = 0x80000000` 为例）：

```
客机物理地址空间            宿主机 pmem 数组
0x80000000  (MBASE)  ───►  pmem[0]
0x80000001           ───►  pmem[1]
   ...                          ...
0x87ffffff  (RIGHT)  ───►  pmem[CONFIG_MSIZE-1]
```

注意：这个换算**不检查区间**。调用者必须保证 `paddr` 落在 RAM 区间内（由 4.4 的 `in_pmem` 把关），否则算出的指针会越界。

#### 4.2.3 源码精读

换算函数本身极其简短：

[src/memory/paddr.c:L27-L28](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/paddr.c#L27-L28) —— `guest_to_host` 把客机物理地址减去 `CONFIG_MBASE` 得到数组下标；`host_to_guest` 是其逆运算。

接口声明在头文件：

[include/memory/paddr.h:L25-L28](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/memory/paddr.h#L25-L28) —— 两个函数的声明与注释，注释明确点出「guest physical address」与「host virtual address」的转换语义。

它的两个典型用法都在启动阶段。一是把镜像烧到复位向量：

[src/isa/riscv32/init.c:L39](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/init.c#L39) —— `init_isa` 用 `memcpy(guest_to_host(RESET_VECTOR), img, sizeof(img))` 把内置镜像写到客机地址 `RESET_VECTOR` 处。

[src/monitor/monitor.c:L64](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/monitor.c#L64) —— `load_img` 用 `fread(guest_to_host(RESET_VECTOR), ...)` 把用户镜像直接读入客机内存。

#### 4.2.4 代码实践

**目标：** 跟踪一次「客机地址 → 宿主指针」的换算，亲手验证公式。

**步骤：**

1. 默认配置下 `CONFIG_MBASE = 0x80000000`，`RESET_VECTOR = 0x80000000`（`PC_RESET_OFFSET = 0`）。
2. 阅读 [src/isa/riscv32/init.c:L21-L27](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/init.c#L21-L27) 的内置镜像 `img[]`：第一条指令 `0x00000297`（`auipc t0, 0`）会被写到客机地址 `0x80000000`。
3. 手算：`guest_to_host(0x80000000) = pmem + 0x80000000 - 0x80000000 = pmem + 0`，即 `pmem[0..3]` 存的就是 `0x00000297`（小端序：字节序为 `97 02 00 00`）。

**需要观察的现象：** 内置镜像的第 0 个 32 位字落在 `pmem` 数组最开头。

**预期结果：** `pmem[0] == 0x97`、`pmem[1] == 0x02`、`pmem[2] == 0x00`、`pmem[3] == 0x00`（小端序）。

> 待本地验证：可在 `init_isa` 的 `memcpy` 之后临时打印 `pmem[0..3]` 验证字节序。

#### 4.2.5 小练习与答案

**练习 1：** 若把 `CONFIG_MBASE` 改成 `0x10000`，`guest_to_host(0x10000)` 应该返回什么？

**参考答案：** 返回 `pmem + 0x10000 - 0x10000 = pmem`，即数组首地址。可见 `guest_to_host` 只关心「相对基址的偏移」，基址本身只是平移量。

**练习 2：** 为什么 `host_to_guest` 在 NEMU 源码里很少被调用？

**参考答案：** 因为 NEMU 绝大多数访问是「已知客机地址、想读写内存」，方向是 guest→host。反向（host→guest）只在极少数诊断或 difftest 场景需要，所以使用频率低，但作为对称接口保留。

---

### 4.3 host_read / host_write：按宽度读写宿主机内存

#### 4.3.1 概念说明

`guest_to_host` 只给出「首字节指针」，但一次访存可能读 1、2、4 或 8 个字节。`host_read` / `host_write` 就是干这件事的：在宿主机指针上，按 `len` 选合适的整数类型做一次指针解引用。

这里有一个隐含假设：**宿主机是小端序（little-endian）**。因为代码直接把字节指针 cast 成 `uint32_t *` 再解引用，没有做字节序转换。NEMU 通常在 x86-64 / RISC-V 等 小端序 Linux 上构建，客机（x86/RISC-V/MIPS/LoongArch）也按小端处理，所以直接复用宿主的字节序即可。

#### 4.3.2 核心流程

```
host_read(addr, len):
  switch len:
    1 → 读 *(uint8_t*)addr
    2 → 读 *(uint16_t*)addr
    4 → 读 *(uint32_t*)addr
    8 → 读 *(uint64_t*)addr   （仅 CONFIG_ISA64）
    其它 → 运行时检查：开启 RT_CHECK 则 assert(0)，否则返回 0
```

`host_write` 同构，只是把「读」换成「写」。

#### 4.3.3 源码精读

[include/memory/host.h:L21-L29](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/memory/host.h#L21-L29) —— `host_read`：按 `len` 选类型解引用；8 字节分支用 `IFDEF(CONFIG_ISA64, ...)` 仅在 64 位客机时编译进来，非法长度由 `MUXDEF(CONFIG_RT_CHECK, assert(0), return 0)` 处理。

[include/memory/host.h:L31-L39](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/memory/host.h#L31-L39) —— `host_write`：与 `host_read` 对称。

两个细节值得注意：

- `8` 字节分支被 `IFDEF(CONFIG_ISA64, ...)` 包住，因为 32 位客机的 `word_t` 是 `uint32_t`，`*(uint64_t*)` 写回时会截断，编译进来既无意义还可能告警。
- `default` 分支的 `MUXDEF(CONFIG_RT_CHECK, assert(0), return 0)`：`CONFIG_RT_CHECK`（runtime checking，默认 `y`）开启时，非法 `len` 直接 `assert(0)` 崩溃，便于发现译码 bug；关闭时静默返回 0（追求性能的发布构建）。`RT_CHECK` 的定义见 [Kconfig:L213-L215](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Kconfig#L213-L215)。

这两个函数是 `static inline`，且只在 `paddr.c` 内部被 `pmem_read` / `pmem_write` 调用：

[src/memory/paddr.c:L30-L37](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/paddr.c#L30-L37) —— `pmem_read`/`pmem_write` 是对 `host_read`/`host_write` 的薄封装，先 `guest_to_host` 换指针再读写。

#### 4.3.4 代码实践

**目标：** 理解 `len` 的合法取值，以及非法 `len` 在不同配置下的行为差异。

**步骤：**

1. 阅读 [include/memory/host.h:L21-L29](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/memory/host.h#L21-L29)，确认 32 位客机（riscv32）下合法 `len` 只有 1/2/4。
2. 在 menuconfig → Miscellaneous 里找到 `RT_CHECK`（[Kconfig:L213-L215](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/Kconfig#L213-L215)），分别尝试开启与关闭两种构建。
3. （源码阅读型）跟踪译码路径：`len` 来自 `INSTPAT` / `decode_operand` 中 `s->width`，正常 RISC-V 访存指令只会产生 1/2/4，因此 `default` 分支在正确实现里永远不该命中。

**需要观察的现象：** 若译码正确，`default` 不会触发；一旦某条指令误把 `len` 算成 3、5 等值，开启 `RT_CHECK` 时会立刻 `assert` 失败。

**预期结果：** `RT_CHECK=y` 时非法 `len` → 进程 abort 并打印 assert 信息；`RT_CHECK=n` 时静默读出 0，bug 被掩盖。这解释了为何调试期默认开 `RT_CHECK`。

#### 4.3.5 小练习与答案

**练习 1：** 为什么 `case 8` 要用 `IFDEF(CONFIG_ISA64, ...)` 包起来，而 `case 1/2/4` 不用？

**参考答案：** 1/2/4 字节访问在 32 位和 64 位客机里都合法；但 8 字节访问只在 64 位客机（`word_t` 为 `uint64_t`）才有意义。32 位客机下若保留 `case 8`，向 `word_t`（`uint32_t`）写 8 字节会截断且告警，故用条件编译排除。

**练习 2：** 假设宿主机是大端序，这段代码还能正确模拟小端客机吗？

**参考答案：** 不能。代码直接复用宿主字节序，没有转换层。若宿主是大端，客机小端程序读写多字节数据会得到反转的字节序。NEMU 隐含假设「宿主小端」，这也是它通常只在 x86-64 / 小端 Linux 上构建的原因之一。

---

### 4.4 paddr_read / paddr_write：pmem / mmio / 越界 三分支路由

#### 4.4.1 概念说明

`paddr_read` / `paddr_write` 是物理内存对外的统一入口。给定任意一个客机物理地址，它要判断这地址到底归谁管：

1. **落在 RAM 区间** → 走 `pmem_read` / `pmem_write`，读写真实内存。
2. **落在 MMIO 区间**（且开启了设备支持）→ 走 `mmio_read` / `mmio_write`，交给设备回调。
3. **都不在** → 越界，`out_of_bound` 直接 `panic`。

这就是「物理总线」的模拟：真实硬件上，CPU 给出一个物理地址，地址译码器（decoder）决定它发给 DRAM 还是给某个设备。NEMU 用一段 `if-else` 把这件事做掉了。

#### 4.4.2 核心流程

```
paddr_read(addr, len):
  if likely(in_pmem(addr)):    return pmem_read(addr, len)   # 1. 命中 RAM（绝大多数情况）
  else if CONFIG_DEVICE:       return mmio_read(addr, len)   # 2. 命中 MMIO 设备
  else:                        out_of_bound(addr)            # 3. 越界 → panic
```

`paddr_write` 同构。

`in_pmem` 是分支判断的核心，它的实现只用了一次无符号减法和一次比较：

\[ \text{in\_pmem}(addr) \iff \big(addr - \text{CONFIG\_MBASE}\big) \bmod 2^{W} < \text{CONFIG\_MSIZE} \]

其中 \(W\) 是 `paddr_t` 的位宽（32 或 64）。为什么这样写就对？分三种情况（以 `CONFIG_MBASE=0x80000000`、`CONFIG_MSIZE=0x8000000` 为例）：

- **正常区间内**（`0x80000000 ≤ addr ≤ 0x87ffffff`）：`addr - 0x80000000` 落在 \([0, 0x8000000)\)，`< CONFIG_MSIZE` 成立 → `true`。
- **低于基址**（如 `addr = 0x00001000`）：无符号减法 `0x00001000 - 0x80000000` 发生下溢回绕，得到一个接近 \(2^{32}\) 的巨大值（`0x80001000` 视作无符号 `> CONFIG_MSIZE`）→ `false`。
- **高于区间**（如 `addr = 0xa00003f8`，这正好是某设备 MMIO 地址）：`addr - 0x80000000 = 0x200003f8 ≥ CONFIG_MSIZE` → `false`，于是流向 mmio 分支。

对比朴素的写法 `addr >= CONFIG_MBASE && addr <= PMEM_RIGHT`，无符号减法版**只需一次减法 + 一次比较**，并且天然规避了 `CONFIG_MBASE + CONFIG_MSIZE` 可能溢出的问题。

#### 4.4.3 源码精读

核心路由函数：

[src/memory/paddr.c:L53-L58](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/paddr.c#L53-L58) —— `paddr_read`：`likely(in_pmem(addr))` 暗示命中 RAM 是热路径；mmio 分支被 `IFDEF(CONFIG_DEVICE, ...)` 包住，未开设备时直接退化为「RAM 或越界」。

[src/memory/paddr.c:L60-L64](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/paddr.c#L60-L64) —— `paddr_write`：与 `paddr_read` 对称，注意 mmio 分支里 `mmio_write(...); return;` 是「宏展开后包含 return」的写法（`IFDEF` 展开为语句）。

`in_pmem` 的定义在头文件里，是个 `static inline`：

[include/memory/paddr.h:L30-L32](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/memory/paddr.h#L30-L32) —— `in_pmem`：一次无符号减法 + 一次比较判断区间，无分支短路。

越界处理：

[src/memory/paddr.c:L39-L42](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/paddr.c#L39-L42) —— `out_of_bound`：打印越界地址、合法区间 `[PMEM_LEFT, PMEM_RIGHT]` 和触发时的 `cpu.pc`，然后 `panic`（即 `assert(0)` 族的致命错误）。

mmio 侧的接口（u6-l18 详讲）：

[src/device/io/mmio.c:L57-L63](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/io/mmio.c#L57-L63) —— `mmio_read` / `mmio_write`：在已注册的设备映射表 `maps[]` 里按地址查找（`fetch_mmio_map` → `find_mapid_by_addr`），找到就回调对应设备的 handler。

**谁调用 `paddr_read` / `paddr_write`？** 目前虚拟内存层直接转发：

[src/memory/vaddr.c:L19-L29](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/vaddr.c#L19-L29) —— `vaddr_ifetch` / `vaddr_read` / `vaddr_write` 当前就是 `paddr_read` / `paddr_write` 的透传（因为还没实现分页，虚拟地址 = 物理地址）。这是下一讲 u4-l13 的起点。

#### 4.4.4 代码实践

**目标：** 亲手验证 `in_pmem` 的无符号减法判断，并触发一次越界。

**步骤：**

1. **手算验证**：取 `addr = 0xa00003f8`（默认串口 MMIO 地址），手算 `0xa00003f8 - 0x80000000 = 0x200003f8`，它 `≥ 0x8000000 (CONFIG_MSIZE)`，故 `in_pmem` 返回 `false` → 该地址走 mmio 分支而非 RAM。这与 [src/device/Kconfig:L25-L27](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/device/Kconfig#L25-L27) 中 `SERIAL_MMIO = 0xa00003f8` 的设定吻合。
2. **触发越界**：在不开启 `CONFIG_DEVICE` 的配置下，让程序访问一个既不在 RAM 也不在任何设备的地址（例如在 SDB 里用 `p *(0x100)` 之类读一个低地址，或写一段访问非法地址的小镜像），观察 `out_of_bound` 的输出。

**需要观察的现象：** 越界时打印形如 `address = 0x00000100 is out of bound of pmem [0x80000000, 0x87ffffff] at pc = 0x...`，随后进程 abort。

**预期结果：** 看到 `out_of_bound` 的 panic 信息，其中 `PMEM_LEFT = 0x80000000`、`PMEM_RIGHT = 0x87ffffff`，并附带当时 `cpu.pc`。

> 待本地验证：具体触发方式取决于你已实现了哪些 SDB 命令；若 `p` 命令尚未支持内存解引用，可改为「阅读 `out_of_bound` 源码 + 手算边界」的源码阅读型实践。

#### 4.4.5 小练习与答案

**练习 1：** 把 `in_pmem` 改写成 `return addr >= CONFIG_MBASE && addr < CONFIG_MBASE + CONFIG_MSIZE;` 有什么潜在问题？

**参考答案：** 两个隐患：一是 `CONFIG_MBASE + CONFIG_MSIZE` 在 32 位 `paddr_t` 下可能溢出（`common.h` 正是为应对这一点才引入 `PMEM64` 与 64 位 `paddr_t`，见 [include/common.h:L34-L44](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/common.h#L34-L44)）；二是两次比较比一次减法+一次比较略慢。原实现用减法规避了上界溢出。

**练习 2：** `paddr_read` 里 mmio 分支写成 `return mmio_read(addr, len);`，但 `paddr_write` 里写成 `mmio_write(addr, len, data); return;`。为什么？

**参考答案：** `IFDEF(CONFIG_DEVICE, return mmio_read(...));` 在未开设备时整个语句被宏替换为空，函数会继续走到 `out_of_bound(addr); return 0;`。`paddr_read` 用 `return` 把控制流交回调用者；`paddr_write` 返回 `void`，没有返回值，但仍需要 `return;` 来阻止它在 mmio 命中后继续执行 `out_of_bound`。两者都是为了让宏在「展开为空」时自然 fall-through 到越界分支。

---

### 4.5 init_mem 与地址区间常量

#### 4.5.1 概念说明

`init_mem` 是物理内存的初始化函数，在启动链中由 `init_monitor`（native 模式）或 `am_init_monitor`（AM 模式）调用。它做两件事：

1. 若是 `malloc` 后端，申请内存并 `assert` 非空。
2. 可选地用随机字节填满整块物理内存（`CONFIG_MEM_RANDOM`）。

第 2 点是 NEMU 的一个**教学利器**：真实硬件上电后内存内容是随机的，依赖「内存默认为 0」的程序其实是在踩 UB（undefined behavior，未定义行为）。NEMU 默认用全局数组后端（BSS 自动清零），会把这种 bug 隐藏掉；开启 `CONFIG_MEM_RANDOM` 后，未初始化内存读出的是垃圾值，bug 立刻暴露。

地址区间常量 `PMEM_LEFT` / `PMEM_RIGHT` / `RESET_VECTOR` 是物理内存的「坐标参照系」，被 `out_of_bound`、`load_img`、`restart` 等多处复用。

#### 4.5.2 核心流程

```
init_mem():
  if CONFIG_PMEM_MALLOC:  pmem = malloc(MSIZE); assert(pmem);
  if CONFIG_MEM_RANDOM:   memset(pmem, rand(), MSIZE);   # 全填随机字节
  Log("physical memory area [PMEM_LEFT, PMEM_RIGHT]");
```

区间常量定义：

\[ \text{PMEM\_LEFT} = \text{CONFIG\_MBASE} \]

\[ \text{PMEM\_RIGHT} = \text{CONFIG\_MBASE} + \text{CONFIG\_MSIZE} - 1 \]

\[ \text{RESET\_VECTOR} = \text{PMEM\_LEFT} + \text{CONFIG\_PC\_RESET\_OFFSET} \]

以 RISC-V 默认配置（`MBASE=0x80000000`、`MSIZE=0x8000000`、`PC_RESET_OFFSET=0`）为例：`PMEM_LEFT=0x80000000`、`PMEM_RIGHT=0x87ffffff`、`RESET_VECTOR=0x80000000`。x86 则是 `MBASE=0x0`、`PC_RESET_OFFSET=0x100000`，故 `RESET_VECTOR=0x100000`。

#### 4.5.3 源码精读

[include/memory/paddr.h:L21-L23](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/memory/paddr.h#L21-L23) —— `PMEM_LEFT` / `PMEM_RIGHT` / `RESET_VECTOR` 三个区间常量的定义。

[src/memory/paddr.c:L44-L51](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/paddr.c#L44-L51) —— `init_mem`：`malloc` 后端申请内存 + `assert`；`IFDEF(CONFIG_MEM_RANDOM, memset(pmem, rand(), CONFIG_MSIZE))` 用 `rand()` 的低字节填满；最后打印区间。注意 `rand()` 返回 `int`，`memset` 取其低 8 位逐字节填充，因此内存呈「重复的随机字节」模式。

`init_mem` 的调用点：

[src/monitor/monitor.c:L113-L114](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/monitor.c#L113-L114) —— native 模式下，`init_monitor` 在「参数解析、随机种子、日志」之后调用 `init_mem()`，顺序很关键：必须先有内存，后面 `init_isa` / `load_img` 才能 `memcpy` 进内存。

[src/monitor/monitor.c:L147-L148](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/monitor/monitor.c#L147-L148) —— AM 模式 `am_init_monitor` 同样在 `init_rand` 之后调用 `init_mem()`。

`MEM_RANDOM` 的启用条件：

[src/memory/Kconfig:L27-L32](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/Kconfig#L27-L32) —— `CONFIG_MEM_RANDOM` 默认 `y`，但 `depends on MODE_SYSTEM && !DIFFTEST && !TARGET_AM`：差分测试时必须关掉，否则 DUT（NEMU）和 REF（如 spike）的内存初值不同会立刻误报；AM 模式也关掉。

`MBASE` / `PC_RESET_OFFSET` 的 ISA 相关默认值：

[src/memory/Kconfig:L3-L14](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/Kconfig#L3-L14) —— x86 的 `MBASE` 为 `0x0`、`PC_RESET_OFFSET` 为 `0x100000`，其它 ISA 的 `MBASE` 默认 `0x80000000`、`PC_RESET_OFFSET` 默认 `0`。

#### 4.5.4 代码实践（本讲主实践）

**目标：** 切换到 `malloc` 后端并开启 `MEM_RANDOM`，观察「未初始化内存访问」的潜在问题。

**步骤：**

1. 运行 `make menuconfig`，进入 **Memory Configuration**：
   - 把 **Physical memory definition** 从 `Using global array` 改成 **`Using malloc()`**（即开启 `CONFIG_PMEM_MALLOC`，见 [src/memory/Kconfig:L17-L25](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/Kconfig#L17-L25)）。
   - 确认 **Initialize the memory with random values**（`CONFIG_MEM_RANDOM`）是开启的（system mode 下默认 `y`，见 [src/memory/Kconfig:L27-L32](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/Kconfig#L27-L32)）。
2. 保存退出，`make` 重新编译。此时 [src/memory/paddr.c:L45-L49](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/paddr.c#L45-L49) 走的就是 `malloc + memset(rand())` 分支。
3. 运行内置镜像（先确保已删除 `welcome()` 里的 `assert(0)`），观察是否能正常 `HIT GOOD TRAP`。
4. **思考实验**：内置镜像（[src/isa/riscv32/init.c:L21-L27](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/isa/riscv32/init.c#L21-L27)）先 `sb zero, 16(t0)` 再 `lbu a0, 16(t0)`，读的是「刚写过的」字节，所以即便周围内存是随机的也不受影响。但假设有一条指令读的是**从未写过的**地址，对比两种后端：
   - 全局数组后端（BSS 清零）：读出 `0x00`，程序可能「碰巧」跑对，bug 被掩盖。
   - `malloc` + `MEM_RANDOM`：读出随机字节，程序行为变得不确定，bug 显形。

**需要观察的现象：** 内置镜像仍能 `HIT GOOD TRAP`（因为它读写的是自己初始化过的字节）；启动日志里会打印 `physical memory area [0x80000000, 0x87ffffff]`。

**预期结果：** 切到 `malloc` 后端后，若程序有「读未初始化内存」的 bug，行为会与全局数组后端不同（更可能出错），这正是 `MEM_RANDOM` 的价值——把依赖「内存默认 0」的隐患逼出来。

> 待本地验证：内置镜像不依赖未初始化内存，故切换后端后行为不变；要看到 `MEM_RANDOM` 的效果，需要一段真正读未初始化地址的程序（例如你自己写一段 RISC-V 汇编，`lw` 一个从未 `sw` 过的地址，对比两种后端下 `a0` 的值）。

**解释 `in_pmem` 为何用无符号减法判断区间：** `paddr_t` 是无符号类型，`addr - CONFIG_MBASE` 在 `addr < CONFIG_MBASE` 时会下溢回绕成一个远大于 `CONFIG_MSIZE` 的值，使比较自然为假，从而用「一次减法 + 一次比较」同时覆盖「低于基址」「高于上界」两种越界，且避免了 `MBASE + MSIZE` 的上溢风险。详见 4.4.2 的三种情况推导。

#### 4.5.5 小练习与答案

**练习 1：** 为什么 `CONFIG_MEM_RANDOM` 在 `DIFFTEST` 开启时必须关掉？

**参考答案：** 差分测试要求 DUT（NEMU）与 REF（参考实现，如 spike）在每条指令后状态完全一致。若两边内存初值不同（一边随机、一边是 REF 的初值），第一条读未初始化内存的指令就会产生不同结果，立刻误报「不一致」。所以差分测试时必须禁用随机初值，保证双方起点一致。

**练习 2：** `memset(pmem, rand(), CONFIG_MSIZE)` 中 `rand()` 返回 `int`，最终内存里每个字节都一样吗？为什么？

**参考答案：** 是的，每个字节都相同（都等于本次 `rand()` 返回值的低 8 位）。因为 `memset` 是「逐字节填充」，它只取 `rand()` 的最低字节，复制到 `CONFIG_MSIZE` 个位置。所以内存不是「每个字节独立随机」，而是「一种随机字节重复 MSIZE 次」。这对暴露「读未初始化」的 bug 已足够（读出非 0 即可），但不要误以为是高斯白噪声式随机。

---

## 5. 综合实践

把本讲的五个最小模块串起来，完成一个「物理内存观察器」小任务。

**任务：** 给 NEMU 加一个临时的内存诊断功能，验证从「客机物理地址」到「宿主机字节」的整条链路。**以下为示例代码，仅供观察，验证后请还原，勿提交。**

1. **切换后端**：在 `make menuconfig` 中切到 `Using malloc()` 并保留 `MEM_RANDOM`，重新编译。
2. **加诊断打印**：在 [src/memory/paddr.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/paddr.c) 的 `init_mem` 末尾（`Log(...)` 之后）加入（**示例代码**）：

   ```c
   /* 示例代码：验证地址映射链路，验证后请删除 */
   uint8_t *h0 = guest_to_host(PMEM_LEFT);          /* 应等于 pmem + 0 */
   printf("[diag] pmem=%p h0=%p diff=0x%lx\n",
       (void*)pmem, (void*)h0, (unsigned long)(h0 - pmem));
   printf("[diag] pmem[0]=0x%02x (MEM_RANDOM fill)\n", pmem[0]);
   printf("[diag] in_pmem(0x80000000)=%d in_pmem(0x100)=%d in_pmem(0xa00003f8)=%d\n",
       in_pmem(0x80000000), in_pmem(0x100), in_pmem(0xa00003f8));
   ```

3. **运行并核对**：
   - `h0 - pmem` 应为 `0`（验证 4.2 的映射公式）。
   - `pmem[0]` 应为某个非 0 随机字节（验证 4.5 的 `MEM_RANDOM` 填充）。
   - 三个 `in_pmem` 应分别输出 `1, 0, 0`（验证 4.4 的无符号减法判断：基址本身在区间内；低地址下溢为假；MMIO 地址高于上界为假）。
4. **触发越界**：在 SDB 里用 `p` 命令读一个越界地址（若你已实现内存解引用），或阅读 [src/memory/paddr.c:L39-L42](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/paddr.c#L39-L42) 确认 `out_of_bound` 会带上 `cpu.pc` 打印。
5. **还原**：删除所有诊断打印，切回默认的全局数组后端，确保不污染后续讲义。

**通过标准：** 你能口头解释「一次 `paddr_read(0x80000000, 4)` 在源码里经过了 `in_pmem → pmem_read → guest_to_host → host_read(case 4)` 这条调用链」，并能说清 `in_pmem` 在三个测试地址上为何得到 `1/0/0`。

## 6. 本讲小结

- 物理内存 `pmem` 就是一段 `uint8_t` 字节数组，有 **全局数组** 和 **malloc** 两种等价后端，由 `CONFIG_PMEM_MALLOC` / `CONFIG_PMEM_GARRAY` 切换。
- `guest_to_host(paddr) = pmem + paddr - CONFIG_MBASE` 是客机物理地址到宿主机指针的核心换算；它只平移、不校验，区间合法性交给 `in_pmem`。
- `host_read` / `host_write` 在宿主指针上按 1/2/4/8 字节宽度做一次类型转换解引用，隐含「宿主小端」假设；非法 `len` 由 `CONFIG_RT_CHECK` 决定是否 `assert`。
- `paddr_read` / `paddr_write` 是物理总线模拟，做 **pmem → mmio → 越界** 三分支路由；`likely(in_pmem(addr))` 标记 RAM 为热路径。
- `in_pmem` 利用 **无符号减法回绕**，用「一次减法 + 一次比较」同时判定上下界，并规避了 `MBASE + MSIZE` 的上溢。
- `init_mem` 负责 `malloc` 申请与 `CONFIG_MEM_RANDOM` 随机填充；随机填充能把「读未初始化内存」的 UB 逼出来，故差分测试时必须关闭。
- `PMEM_LEFT` / `PMEM_RIGHT` / `RESET_VECTOR` 是物理内存的坐标参照系，由 `CONFIG_MBASE` / `CONFIG_MSIZE` / `CONFIG_PC_RESET_OFFSET` 计算，随 ISA 不同而不同。

## 7. 下一步学习建议

物理内存只是内存系统的最底层。下一讲 **u4-l13 虚拟内存 vaddr 与 MMU 接口** 会讲解 `vaddr.c` 如何在 `paddr` 之上叠加一层：

- 当前 `vaddr_ifetch/read/write` 只是 `paddr` 的透传（本讲已看到，[src/memory/vaddr.c:L19-L29](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/vaddr.c#L19-L29)）。
- `isa.h` 定义的 `isa_mmu_check` / `isa_mmu_translate`、`MMU_DIRECT/TRANSLATE/FAIL`、`MEM_RET_CROSS_PAGE` 等返回值，是为分页预留的接缝。

建议阅读顺序：

1. 先读本讲 4.4 的 `paddr_read/write` 三分支，建立「物理总线」直觉。
2. 读 [src/memory/vaddr.c](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/src/memory/vaddr.c) 全文（只有 30 行），体会「目前直接转发」的状态。
3. 读 [include/isa.h](https://github.com/NJU-ProjectN/nemu/blob/8e7a0fecc95b5b5d2c1f6be0ec1b703da93356c0/include/isa.h) 中 MMU 相关接口声明，为 u4-l13 和 u7-l22（分页实现）做铺垫。

进一步可选：跳到 u6-l18 设备框架，看 `mmio_read` / `mmio_write` 背后的 `IOMap` 与设备回调机制，理解物理地址是如何被分发到串口、键盘、VGA 等具体外设的。
