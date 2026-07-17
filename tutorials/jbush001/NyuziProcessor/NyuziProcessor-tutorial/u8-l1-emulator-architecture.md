# 模拟器架构与指令执行

## 1. 本讲目标

前面几讲我们一直在读 **硬件**：`core.sv` 的 14 级流水线、各级缓存、TLB、写回仲裁。本讲换一个视角，进入 Nyuzi 仓库的 `tools/emulator/`——一个用 **C 语言写成**的「指令集模拟器」(instruction set simulator, ISS)。

学完本讲你应该能够：

1. 说清「指令集模拟器」与「周期精确仿真器」（Verilator 生成的 `nyuzi_vsim`）的本质区别，理解模拟器为何能作为**功能金标准**。
2. 看懂 `main.c` 的命令行解析与三种运行模式，知道一条命令各选项如何影响模拟器行为。
3. 画出 `processor.c` 里「处理器 → 核 → 线程」三层结构，并解释线程如何被**轮询调度**执行。
4. 沿 `execute_instruction` 的派发链路，解释一条 32 位指令如何被翻译成对线程状态的直接修改。
5. 理解 `instruction-set.h` 与硬件 `defines.svh` **数值同构**的设计，明白这正是协同仿真（下一讲 u8-l3）能逐条比对的基础。

---

## 2. 前置知识

本讲默认你已建立以下认知（来自前置讲义，这里只做最简提醒，不重复展开）：

- **ISA 数据模型**（u2-l1）：32 位定长指令；32 个标量寄存器、32 个向量寄存器；向量 16 通道（`vector_t` 共 512 位）；指令按最高几位分为 R / I / M / C / B 五种格式。
- **单核流水线全景**（u3-l2）：硬件 `core.sv` 把一条指令走过取指→解码→线程选择→操作数 fetch→执行→写回，并有缓存缺失、记分牌冒险、三条不同长度执行路径等机制。
- **控制寄存器**（u2-l4）：`CR_THREAD_ID`、`CR_SUSPEND_THREAD`、`CR_FLAGS` 等编号，以及 getcr/setcr 实为 M 格式访存指令。

两个本讲要反复用到的关键直觉：

- **架构状态 vs 微架构状态**。架构状态是「程序员可见、决定程序正确性」的状态：寄存器、内存、PC、控制寄存器、TLB 表项。微架构状态是「硬件为提速而引入、对程序不可见」的状态：流水线寄存器、各级缓存、记分牌、LRU、store 队列。**指令集模拟器只维护前者**，这正是它又快又能当金标准的根本原因。
- **解释执行**。模拟器没有「取指→解码→执行」这种流水线节拍；它在一个普通 C 函数 `execute_instruction` 里读一个 32 位字、按最高几位分支、直接改写线程结构体里的字段，一条指令就是一次函数调用。

> 一个常见误解：以为模拟器「更快地跑了流水线」。其实它**根本没有流水线**——既没有缓存缺失带来的停顿，也没有分支预测失败的惩罚。所以模拟器报告的「指令数」有参考价值，但它给不出「周期数」。

---

## 3. 本讲源码地图

本讲围绕四个文件展开，它们都在 `tools/emulator/` 下：

| 文件 | 作用 | 本讲用来讲 |
|------|------|-----------|
| [tools/emulator/main.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/main.c) | 程序入口、命令行解析、三种运行模式主循环 | 入口与命令行 |
| [tools/emulator/processor.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c) | 处理器/核/线程数据结构、线程调度、全部指令的 C 实现 | 线程调度、指令执行 |
| [tools/emulator/processor.h](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.h) | 对外接口声明、核心常量（寄存器数、向量通道数、缓存行长） | 数据结构常量 |
| [tools/emulator/instruction-set.h](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/instruction-set.h) | ISA 编码定义（操作码、格式、控制寄存器、trap 类型） | 共享 ISA 定义 |

辅助阅读（本讲会点到，但不深入）：`util.h`（位提取与轮询位扫描）、`device.h`（外设地址与中断位定义）、`cosimulation.h`（留给下一讲）。

---

## 4. 核心概念与源码讲解

### 4.1 入口与命令行（main.c）

#### 4.1.1 概念说明

`main.c` 是宿主程序的入口，它本身**不是**被模拟的代码——被模拟的是你加载进来的 hex 镜像。`main.c` 负责「搭台」：解析命令行、分配并初始化处理器、加载内存镜像、根据模式进入不同的主循环、收尾时打印统计。

理解 `main.c` 的关键是抓住**三种运行模式**，它们对应模拟器的三大用途：

- **normal（默认）**：一直跑到所有线程停机为止。这是日常跑程序、调试软件的模式。
- **cosim**：协同仿真模式，逐条把硬件模型的副作用与模拟器比对——这是下一讲 u8-l3 的主题。
- **gdb**：在 8000 端口监听，等待 LLDB/GDB 远程连接，做源码级调试。

#### 4.1.2 核心流程

```
解析命令行选项 (getopt)
   ├─ 确定模式 mode、verbose、threads_per_core、num_cores、memory_size …
init_processor(...)              # 分配内存、创建核与线程
load_hex_file(proc, 镜像)         # 把 $readmemh 格式镜像读到地址 0
init_device(proc)                # 挂上虚拟外设
switch (mode)
   ├─ normal → enable_tracing(若 -v)；循环 execute_instructions(proc, 1000000)
   ├─ cosim  → run_cosimulation(...)
   └─ gdb    → remote_gdb_main_loop(...)
dump_instruction_stats(proc)     # 打印总指令数（及可选分类统计）
```

normal 模式下的核心是一个 `while` 循环：每次批量执行 100 万条指令就返回一次，让模拟器有机会轮询宿主输入（串口输入、命名管道中断）。

#### 4.1.3 源码精读

命令行由 `getopt` 解析，支持的选项与默认值在 [tools/emulator/main.c:142-329](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/main.c#L142-L329)。几个与后续讲解紧密相关的默认值：

```c
uint32_t threads_per_core = 4;   // -t
uint32_t num_cores = 1;          // -p
uint32_t memory_size = 0x1000000;// -c，默认 16 MiB
```

模式用一个匿名枚举表示，三选一：

```c
enum { MODE_NORMAL, MODE_COSIMULATION, MODE_GDB_REMOTE_DEBUG } mode = MODE_NORMAL;
```

初始化处理器时，第四个参数 `randomize_memory` 控制是否把内存填随机值。注意它在 **cosim 模式下被刻意关闭**——因为协同仿真要把内存逐字节与硬件比对，随机初值反而没意义：

```c
proc = init_processor(memory_size, num_cores, threads_per_core,
                      mode != MODE_COSIMULATION, shared_memory_file);
```

随后加载镜像并进入主循环（[tools/emulator/main.c:398-433](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/main.c#L398-L433)）。normal 模式分「有图形窗口」和「无窗口」两条路径，无窗口时就是：

```c
case MODE_NORMAL:
    if (verbose)
        enable_tracing(proc);                 // -v：打开副作用跟踪
    dbg_set_stop_on_fault(proc, false);
    ...
    while (execute_instructions(proc, 1000000))   // 每次 100 万条
        poll_inputs(proc);                        // 轮询串口/管道输入
```

`execute_instructions` 返回 `false` 表示「该停了」（线程全停或崩溃或命中断点），循环随之结束。最后无论哪种模式都会打印统计：

```c
dump_instruction_stats(proc);   // 至少打印总指令数
```

#### 4.1.4 代码实践

**实践目标**：确认命令行选项与默认值，建立「选项 → 内部变量」的映射。

**操作步骤**：

1. 不带参数运行模拟器，观察 usage 输出：
   ```bash
   bin/nyuzi_emulator
   ```
   对照 [tools/emulator/main.c:45-65](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/main.c#L45-L65) 的 `usage()`，把每个选项对应到 `getopt` 串 `"f:d:vm:b:t:p:c:r:s:i:o:a"` 里的一个字母。
2. 阅读 `-t`（线程数）的分支 [tools/emulator/main.c:252-260](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/main.c#L252-L260)，确认合法范围是 1–32。

**需要观察的现象**：usage 里 `-v` 描述为「print register transfer traces」；`-t` 默认 4、`-p` 默认 1、`-c` 默认 16 MiB。

**预期结果**：你能口头说出「`-v` 打开跟踪、`-m cosim` 进协同仿真、`-t` 改每核线程数」三件事。若本地未安装工具链无法运行，标记「待本地验证」，但选项映射可纯靠读源码完成。

#### 4.1.5 小练习与答案

**练习 1**：为什么协同仿真模式下 `init_processor` 的 `randomize_memory` 要传 `false`？
> **答**：协同仿真要把模拟器的内存内容与硬件模型逐字节比对。随机初值在两边不一致，会引入虚假的比对失败；清零后两边起点一致，差异只来自程序自身的写操作。

**练习 2**：`-v` 选项最终调用了哪个函数？它在处理器结构里置了哪个标志？
> **答**：调用 `enable_tracing(proc)`（[tools/emulator/main.c:401](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/main.c#L401)），它把 `proc->enable_tracing` 置为 `true`（见 4.3 节）。

---

### 4.2 线程调度（processor.c）

#### 4.2.1 概念说明

`processor.c` 是模拟器的心脏。它先定义了「处理器 → 核 → 线程」三层结构体，再用一个调度循环决定**每个时刻让哪个线程前进一条指令**。

硬件里（u3-l2、u4-l3）多线程是为了**隐藏延迟**：一个线程缓存缺失时，硬件切换到另一个线程继续喂流水线。模拟器里没有缓存、没有延迟，但**仍然保留多线程轮询**——目的是 faithfully 复现 ISA 的多线程语义：哪些线程「活着」、trap 与中断按线程独立维护、`CR_SUSPEND_THREAD` 能让线程自我退出。换句话说，模拟器复现的是「架构可见的调度结果」，而非「硬件的调度时序」。

调度依赖一个关键变量 `thread_enable_mask`：一个 32 位位图，第 i 位为 1 表示线程 i 仍在运行。线程通过写控制寄存器 `CR_SUSPEND_THREAD` 把自己的位清掉；当位图归零，程序就「结束」了（这正是 u1-l4 里 hello_world 停机的软件侧对应物）。

#### 4.2.2 核心流程

```
execute_instructions(proc, N)            # 要执行 N 条指令
  循环 N 次:
    若 thread_enable_mask == 0 → 返回 false（程序结束）
    若 crashed               → 返回 false（崩溃）
    next_thread = next_set_bit(mask, (next_thread+31) & 31)   # 轮询选下一个活线程
    execute_instruction(该线程)           # 前进一条指令（见 4.4）
    timer_tick(proc)                     # 软件定时器倒计时
  返回 true（这批 N 条跑完，还可以继续）
```

`(next_thread + 31) & 31` 其实是 `(next_thread - 1) mod 32` 的无符号写法，配合 `next_set_bit`「向下扫描、到底回绕」的语义，就构成了轮询。

#### 4.2.3 源码精读

先看三层结构体。线程是基本单位（[tools/emulator/processor.c:54-86](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L54-L86)），它持有该线程全部的架构状态：

```c
struct thread
{
    struct core *core;
    uint32_t id;
    uint32_t pc;
    uint32_t asid;
    uint32_t page_dir;
    uint32_t interrupt_mask;
    uint32_t latched_interrupts;
    bool enable_interrupt;
    bool enable_mmu;
    bool enable_supervisor;
    uint32_t subcycle;                         // scatter/gather 的逐通道计数器
    uint32_t scalar_reg[NUM_REGISTERS];        // 32 个标量寄存器
    uint32_t vector_reg[NUM_REGISTERS][NUM_VECTOR_LANES]; // 32×16 向量寄存器
    struct { ... } saved_trap_state[TRAP_LEVELS]; // 2 级 trap 现场
};
```

注意 `vector_reg` 是 `[32][16]` 的二维数组——模拟器用「16 个普通 `uint32_t`」来表示一个向量寄存器，SIMD 的并行性在这里体现为一次 `for (lane=0; lane<16; lane++)` 循环，而不是硬件里的 16 份并行 ALU。

核（[tools/emulator/processor.c:95-107](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L95-L107)）持有一组线程和**核级**共享状态：trap handler 地址、TLB miss handler 地址、ITLB/DTLB 表项数组。处理器（[tools/emulator/processor.c:109-136](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L109-L136)）持有多核、**全局** `thread_enable_mask`、一整块平坦内存 `uint32_t *memory`：

```c
struct processor
{
    uint32_t total_threads;
    uint32_t thread_enable_mask;     // 谁还活着
    uint32_t num_cores;
    uint32_t threads_per_core;
    struct core *cores;
    uint32_t *memory;                // 注意：没有 cache，直接一块大数组
    ...
    int64_t total_instructions;      // 全局指令计数
};
```

`memory` 是一块 `malloc` 出来的连续数组——**模拟器没有 L1/L2 缓存**，所有访存直接读写这片内存。这正是「非周期精确、无缓存」的字面体现。

初始化在 `init_processor`（[tools/emulator/processor.c:186-281](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L186-L281)）里完成。两处值得记：一是容量断言 `num_cores * threads_per_core <= 32`（受 32 位掩码限制）；二是复位时**只使能线程 0**：

```c
assert(num_cores * threads_per_core <= 32);
...
core->threads[thread_id].id = core_id * threads_per_core + thread_id;
core->threads[thread_id].enable_supervisor = true;   // 复位即在特权态
...
proc->thread_enable_mask = 1;   // 复位：只有线程 0 能跑
```

线程号 `id = core_id * threads_per_core + thread_id` 这个线性编号，与 `get_thread` 的反推一致（[tools/emulator/processor.c:588-592](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L588-L592)）。

调度循环 `execute_instructions`（[tools/emulator/processor.c:400-453](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L400-L453)）有两个变体：随机调度（`-a`，用于压力测试暴露竞态）和默认轮询。默认轮询的核心三行：

```c
if (proc->thread_enable_mask == 0) { printf("thread enable mask is now zero\n"); return false; }
if (proc->crashed) return false;
next_thread = next_set_bit(proc->thread_enable_mask, ((next_thread + 31) & 31));
if (!execute_instruction(get_thread(proc, next_thread)))
    return false;  // 命中断点
timer_tick(proc);
```

`next_set_bit`（[tools/emulator/util.h:118-126](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/util.h#L118-L126)）在位图里「从给定下标向下找下一个置位位，找不到就回绕到最高位」，从而在所有使能线程间公平轮询。被挂起（位被清）的线程不会被选中，于是自然让出执行权。

线程自我挂起靠写 `CR_SUSPEND_THREAD`，实现在 `write_control_register`（[tools/emulator/processor.c:1770-1777](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L1770-L1777)）：

```c
case CR_SUSPEND_THREAD:
    thread->core->proc->thread_enable_mask &= ~value;       // 清掉指定线程位
    break;
case CR_RESUME_THREAD:
    thread->core->proc->thread_enable_mask |= value & ((1ull << ...total_threads) - 1);
    break;
```

#### 4.2.4 代码实践

**实践目标**：用「源码阅读型实践」验证调度是轮询、且复位只使能线程 0。

**操作步骤**：

1. 在 [tools/emulator/processor.c:400](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L400) 的 `execute_instructions` 里找到默认（非随机）分支，确认它每次循环只调用一次 `execute_instruction`。
2. 在 [tools/emulator/processor.c:278](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L278) 确认 `thread_enable_mask = 1`。
3. 思考：裸机程序（如 hello_world）启动时只有线程 0 在跑，其余线程是谁、何时唤醒的？（提示：回顾 u9-l2 的 `parallelExecute` / crt0，它读 `CR_THREAD_ID` 后用 `CR_RESUME_THREAD` 唤醒其余线程。）

**需要观察的现象**：调度循环对线程「一视同仁」地轮询，没有优先级、没有时间片量化——每线程每次只走一条指令。

**预期结果**：你能解释「`thread_enable_mask` 归零 ⇒ `execute_instructions` 返回 false ⇒ `main` 的 while 循环退出 ⇒ 程序结束」这条链路。运行验证标记「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`(next_thread + 31) & 31` 等价于什么？为什么不用 `next_thread - 1`？
> **答**：等价于 `(next_thread - 1) mod 32`。用 `+31` 再 `&31` 是为了避免 `next_thread` 为 0 时减成 `-1`（无符号下变成巨大的数）；`&31` 把结果限制在 0–31。

**练习 2**：默认配置（1 核 4 线程）下，复位后有几个线程在跑？写 `CR_SUSPEND_THREAD` 传什么值能让线程 0 自己停掉？
> **答**：只有线程 0 在跑（`thread_enable_mask = 1`）。线程 0 写入值 `1`（即 `1 << 0`）即可清掉自己的位；hello_world 的 crt0 实际写 `-1`（全 1），一次性清掉所有位，使位图归零、程序结束。

---

### 4.3 指令执行与副作用跟踪（processor.c 续）

#### 4.3.1 概念说明

调度循环选出的线程，由 `execute_instruction` 前进一条指令。这个函数是整个模拟器的「译码 + 执行」合体：它读出 32 位指令字，**只看最高几位**判断属于哪一大类，然后调用对应的 `execute_*_inst` 处理函数。处理函数直接读写 `thread` 结构体里的寄存器字段——没有「写回级」，没有「记分牌」，写就是写。

这里要特别理解「副作用跟踪」(`enable_tracing`)：当 `-v` 打开后，所有**架构可见的写操作**（标量寄存器写、向量寄存器写、内存写）都会经 `set_scalar_reg` / `set_vector_reg` / 各 store 路径打印一行。这既是给人看的调试踪迹，也是给协同仿真比对的「事件流」。注意一个细节：踪迹里的 PC 是 `thread->pc - 4`，因为取指后 PC 已被预先加 4，减回去才是这条指令自己的地址。

#### 4.3.2 核心流程

```
execute_instruction(thread):
    fetch_pc = thread->pc;  thread->pc += 4          # PC 预先前进
    对齐检查（PC 必须 4 字节对齐，否则 TT_UNALIGNED_ACCESS）
    translate_address(fetch_pc)                        # 查 ITLB（MMU 开时）
    instruction = memory[physical_pc]                  # 取出 32 位指令字
    total_instructions++
    按最高几位分支:
       110xxxxxxxxxxxx → execute_register_arith_inst   # R 格式
       0xxxxxxxxxxxxxx → execute_immediate_arith_inst # I 格式（含断点/NOP 特判）
       10xxxxxxxxxxxxx → execute_memory_access_inst   # M 格式
       1111xxxxxxxxxxx → execute_branch_inst          # B 格式
       1110xxxxxxxxxxx → execute_cache_control_inst   # C 格式
```

五大类的最高位特征与 u2-l1 讲的 R/I/M/C/B 五种格式、以及 u4-l2 硬件解码器的格式识别**完全对应**——这是模拟器与硬件「实现同一套 ISA」的直接体现。

#### 4.3.3 源码精读

派发主体在 [tools/emulator/processor.c:2019-2096](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L2019-L2096)。取指与对齐：

```c
unsigned int fetch_pc = thread->pc;
thread->pc += 4;                              // 预先前进
if ((fetch_pc & 3) != 0)
    raise_trap(thread, thread->pc, TT_UNALIGNED_ACCESS, false, false, 0);
if (!translate_address(thread, fetch_pc, &physical_pc, false, false))
    return true;                              // 进了 TLB miss handler
instruction = *UINT32_PTR(thread->core->proc->memory, physical_pc);
thread->core->proc->total_instructions++;
```

派发的 if-else 链（注意判断顺序与位掩码）：

```c
if ((instruction & 0xe0000000) == 0xc0000000)      execute_register_arith_inst(...); // 110xxxxx
else if ((instruction & 0x80000000) == 0)  { ... execute_immediate_arith_inst(...); } // 0xxxxxxx
else if ((instruction & 0xc0000000) == 0x80000000) execute_memory_access_inst(...);   // 10xxxxxx
else if ((instruction & 0xf0000000) == 0xf0000000) execute_branch_inst(...);          // 1111xxxx
else if ((instruction & 0xf0000000) == 0xe0000000) execute_cache_control_inst(...);   // 1110xxxx
else printf("Bad instruction @%08x\n", thread->pc - 4);
```

每个处理函数内部会按更细的字段（操作码、格式、寄存器号）再分支。以寄存器-寄存器算术为例（[tools/emulator/processor.c:1000-1130](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L1000-L1130)），它先抽出操作码 `op`、格式 `fmt`、各寄存器号，再依格式做「标量-标量 / 向量-标量 / 向量-向量」三种运算，向量情况就是一个 16 次的 `for` 循环，每次调用 `scalar_arithmetic_op`。

`scalar_arithmetic_op`（[tools/emulator/processor.c:887-979](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L887-L979)）是一个对 `enum arithmetic_op` 的大 `switch`，它是 u2-l2 里 `alu_op_t` 的**功能参考实现**。例如整数加减和比较：

```c
case OP_ADD_I: return value1 + value2;
case OP_SUB_I: return value1 - value2;
case OP_CMPEQ_I: return (uint32_t)value1 == value2;     // 比较结果恒为 0/1
...
case OP_CMPGT_I: return (uint32_t)((int32_t)value1 > (int32_t)value2);
```

浮点运算则借助 C 的 `float` 直接算（`value_as_float` 把整数位模式重解释为 float），这正好呼应 u5-l3 讲的「硬件浮点是 5 级流水线、且非完全 IEEE 兼容」——模拟器用宿主 `float` 作为参考，二者在绝大多数情况下一致，边界情况（subnormal、特定 NaN）的差异正是协同仿真要捕获的对象。

副作用跟踪集中在三个写回入口。标量寄存器写（[tools/emulator/processor.c:635-647](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L635-L647)）：

```c
static void set_scalar_reg(struct thread *thread, uint32_t reg, uint32_t value)
{
    if (thread->core->proc->enable_tracing)
        printf("%08x [th %u] s%u <= %08x\n", thread->pc - 4, thread->id, reg, value);
    if (thread->core->proc->enable_cosim)
        cosim_check_set_scalar_reg(thread->core->proc, thread->pc - 4, reg, value);
    thread->scalar_reg[reg] = value;
}
```

向量寄存器写（[tools/emulator/processor.c:649-672](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L649-L672)）打印 `v%u{%04x}`——`%04x` 就是 16 位掩码，体现「被掩码的通道不写入」。内存写则在 store 路径里直接 printf，例如标量 store（[tools/emulator/processor.c:1407-1418](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L1407-L1418)）：

```c
printf("%08x [th %u] memory store size %u %08x %02x\n",
       thread->pc - 4, thread->id, access_size, virtual_address, value_to_store);
```

> 提示：`tools/emulator/README.md` 的踪迹示例里写的是旧的 `writeMemWord` 文本，与当前代码的 `memory store size ...` 格式**不一致**——以源码为准。

内存访问派发在 `execute_memory_access_inst`（[tools/emulator/processor.c:1781-1820](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L1781-L1820)），按 `memory_op`（见 4.4）分流到标量 load/store、控制寄存器、block 向量、scatter/gather 四条路径。其中 scatter/gather（[tools/emulator/processor.c:1511-1595](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L1511-L1595)）复现了 u2-l3 讲的「16 个 subcycle 串行」语义——每次只处理一个 lane，未到 16 就把 PC 减 4 重发同一指令：

```c
if (++thread->subcycle == NUM_VECTOR_LANES)
    thread->subcycle = 0;        // 完成
else
    thread->pc -= 4;             // 重发当前指令，下一轮处理下一个 lane
```

这正是「模拟器忠实复现架构可见行为」的又一例：硬件用 subcycle 状态机串行访存，模拟器用「PC 回退 + subcycle 计数」达到同样的可观测效果，但同样不耗费「16 个周期」。

#### 4.3.4 代码实践

**实践目标**：跟踪一条 store 指令在模拟器里的完整执行路径。

**操作步骤**：

1. 假设有一条 `store_32 s0, 0(s1)`（M 格式，`memory_op = MEM_LONG`，`is_load = 0`）。它在 [tools/emulator/processor.c:2086](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L2086) 因最高位 `10` 落入 `execute_memory_access_inst`。
2. 在 [tools/emulator/processor.c:1798-1801](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L1798-L1801) 看到 `MEM_LONG` 走 `execute_scalar_load_store_inst`。
3. 在该函数的 store 分支（[tools/emulator/processor.c:1358-1373](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L1358-L1373)）确认：地址 `>= DEVICE_BASE_ADDRESS (0xffff0000)` 时走 `write_device_register`（外设），否则直接写 `memory` 数组。

**需要观察的现象**：一次 store 没有任何「缓存查询」「写缓冲」「snoop」——直接落到 `memory` 数组或外设寄存器。

**预期结果**：你能画出 `execute_instruction → execute_memory_access_inst → execute_scalar_load_store_inst → memory[...] = value` 这条调用链。若地址是 UART 输出寄存器 `0xffff0048`，则等价于 hello_world 的 `printf` 出口（呼应 u1-l4）。

#### 4.3.5 小练习与答案

**练习 1**：踪迹行里的 PC 为什么是 `thread->pc - 4` 而不是 `thread->pc`？
> **答**：`execute_instruction` 一开始就执行了 `thread->pc += 4`，取指后 PC 已指向下一条指令。踪迹要显示「产生该副作用的那条指令」的地址，所以打印时要减回 4。

**练习 2**：向量寄存器写踪迹里的 `{%04x}` 是什么？
> **答**：16 位写掩码。被掩码位为 0 的通道不会被写入（`set_vector_reg` 里 `if (mask & (1 << lane))` 才写），踪迹把掩码打出来，让人一眼看出哪些通道实际更新了。

**练习 3**：模拟器执行一条向量加法要「几个周期」？
> **答**：无法用「周期」衡量——模拟器不建模周期。它用一次 16 次迭代的 `for` 循环完成 16 个通道的运算，但这只对应「一条指令、一次 `execute_instruction` 调用、`total_instructions` 加 1」。

---

### 4.4 共享 ISA 定义（instruction-set.h）

#### 4.4.1 概念说明

`instruction-set.h` 是一份**纯枚举/宏**的头文件，没有可执行代码。它定义了 Nyuzi ISA 的全部编码：算术操作码、指令格式、访存类型、分支类型、控制寄存器号、trap 类型、缓存控制操作。

它的真正重要性在于：**这些数值与硬件 `hardware/core/defines.svh` 里的对应定义是同一套**。硬件用 SystemVerilog 的 `typedef enum`，模拟器用 C 的 `enum`，但每个符号的数值都相同。这就是为什么 4.3 节里那些位掩码（`0xc0000000` 等）能同时描述硬件解码器和模拟器派发——它们解码的是同一种二进制格式。

这种「一份编码、两个实现」的设计是 Nyuzi 验证体系的基石：模拟器因此可以作为**功能金标准**，与硬件逐条比对（u8-l3 协同仿真）。如果模拟器和硬件对某条指令的执行结果不一致，那一定是其中一方有 bug。

#### 4.4.2 核心流程

```
instruction-set.h 定义的几组关键编码:
  enum arithmetic_op     OP_OR=0 ... OP_BREAKPOINT=62      （6 位操作码，含整数/浮点/比较）
  enum register_arith_format   FMT_RA_SS/VS/VS_M/VV/VV_M   （寄存器-寄存器算术的格式）
  enum immediate_arith_format  FMT_IMM_S/V/MOVEHI/VM        （立即数算术的格式）
  enum memory_op         MEM_BYTE ... MEM_SCGATH_MASK       （4 位访存类型）
  enum branch_type       BRANCH_REGISTER ... BRANCH_ERET    （分支子类型）
  enum control_register  CR_THREAD_ID=0 ... CR_RESUME_THREAD=21
  enum trap_type         TT_RESET ... TT_BREAKPOINT         （异常类型）
  enum cache_control_op  CC_DTLB_INSERT ... CC_ITLB_INSERT  （缓存/TLB 控制）
```

#### 4.4.3 源码精读

算术操作码（[tools/emulator/instruction-set.h:29-73](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/instruction-set.h#L29-L73)）覆盖 u2-l2 讲过的全部 ALU 操作。注意一个编码规律：浮点操作从 `OP_ADD_F = 32` 开始，即第 5 位置 1——这与 u2-l2 讲的「浮点操作码最高位为 1」一致，使硬件一行 `alu_op[5]` 就能把指令分流到浮点流水线：

```c
enum arithmetic_op {
    OP_OR = 0, OP_AND = 1, ...
    OP_ADD_I = 5, OP_SUB_I = 6, OP_MULL_I = 7, ...
    OP_CMPEQ_I = 16, ... OP_CMPLE_U = 25,    // 比较，结果恒 0/1
    ...
    OP_ADD_F = 32, OP_SUB_F = 33, OP_MUL_F = 34,   // 浮点（bit5=1）
    ...
    OP_BREAKPOINT = 62
};
```

访存类型（[tools/emulator/instruction-set.h:92-105](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/instruction-set.h#L92-L105)）是 u2-l3 讲的 4 位 `memory_op_t`，控制寄存器号（[tools/emulator/instruction-set.h:118-142](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/instruction-set.h#L118-L142)）是 u2-l4 讲的 `control_register_t`：

```c
enum control_register {
    CR_THREAD_ID = 0, CR_TRAP_HANDLER = 1, CR_TRAP_PC = 2, ...
    CR_FLAGS = 4, CR_CURRENT_ASID = 9, CR_PAGE_DIR = 10,
    CR_INTERRUPT_ENABLE = 14, CR_INTERRUPT_ACK = 15, CR_INTERRUPT_PENDING = 16,
    CR_SYSCALL_INDEX = 19, CR_SUSPEND_THREAD = 20, CR_RESUME_THREAD = 21
};
```

trap 类型（[tools/emulator/instruction-set.h:144-158](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/instruction-set.h#L144-L158)）与 u7-l3 讲的 `trap_type_t` 对应，`raise_trap`（[tools/emulator/processor.c:732-796](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L732-L796)）就是消费这些类型、把现场压入 `saved_trap_state`、把 PC 改到 handler 的实现。TLB 相关标志（[tools/emulator/instruction-set.h:23-27](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/instruction-set.h#L23-L27)）则是 u7-l1 讲的 `tlb_entry_t` 属性位：

```c
#define TLB_PRESENT 1
#define TLB_WRITE_ENABLE 2
#define TLB_EXECUTABLE 4
#define TLB_SUPERVISOR 8
#define TLB_GLOBAL 16
```

核心常量则放在 `processor.h`（[tools/emulator/processor.h:23-27](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.h#L23-L27)）：`NUM_REGISTERS 32`、`NUM_VECTOR_LANES 16`、`CACHE_LINE_LENGTH 64`。注意 `CACHE_LINE_LENGTH` 在模拟器里**只用于 sync load/store 的缓存行粒度判定**（`last_sync_load_addr = addr / 64`），并不代表模拟器真有缓存行——它只是在复现 LL/SC 的「监视粒度」这一架构可见语义。

#### 4.4.4 代码实践

**实践目标**：用 `instruction-set.h` 的枚举，手工「解码」一条踪迹对应的指令。

**操作步骤**：

1. 假设 `-v` 踪迹里有这么一行（PC 与硬件反汇编对得上）：
   ```
   00001020 [th 0] s5 <= 00000007
   ```
   这是一条「把标量寄存器 s5 写成 7」的副作用。最可能的指令是立即数算术 `move_i s5, 7`（`OP_MOVE = 15`）。
2. 查 [tools/emulator/instruction-set.h:45](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/instruction-set.h#L45) 确认 `OP_MOVE = 15`，再查 [tools/emulator/processor.c:887](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tools/emulator/processor.c#L887) 的 `scalar_arithmetic_op` 里 `case OP_MOVE: return value2;`，确认它就是把立即数 7 透传给目标寄存器。
3. 对照一条反汇编（用 `llvm-objdump -d`）确认该 PC 处确实是 `move_i`。

**需要观察的现象**：踪迹行的 PC 能在反汇编里定位到唯一一条指令；该指令的操作码字段与 `instruction-set.h` 的某个 `OP_*` 数值吻合。

**预期结果**：你建立了「二进制指令字 → `instruction-set.h` 枚举 → `scalar_arithmetic_op`/`execute_*` 处理函数 → 踪迹副作用」的闭环。运行/反汇编步骤若本地无工具链，标记「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `instruction-set.h` 和硬件 `defines.svh` 要用相同的数值？
> **答**：这样硬件解码器和模拟器派发面对同一种二进制格式，任何一方对某条指令的解释都可以用另一方来交叉验证。这是协同仿真（u8-l3）能逐条比对副作用的前提——若编码不同，比对前还得先做一层翻译。

**练习 2**：`OP_ADD_F = 32`，`32` 的二进制有什么特点？硬件如何利用它？
> **答**：`32 = 0b100000`，第 5 位（bit 5）为 1，而所有整数算术操作码 bit 5 都为 0。硬件因此能用 `alu_op[5]` 这一位把指令分流到浮点 5 级流水线或整数单周期流水线（见 u2-l2、u5-l3）。模拟器则统一在 `scalar_arithmetic_op` 里处理，不做这种硬件分流。

---

## 5. 综合实践

把四个最小模块串起来，完成一次「踪迹驱动的指令解读」。这个任务既是本讲的代码实践任务，也直接衔接下一讲的协同仿真。

**任务**：在模拟器里用 `-v` 跑通 hello_world，从踪迹里定位一条「标量 store 到 UART 输出寄存器」的事件，逐层解释它从二进制到副作用的全过程。

**步骤**：

1. **构建并运行**（沿用 u1-l2 / u1-l4 的脚本，由 CMake 自动生成）：
   ```bash
   cd software/apps/hello_world && cmake . && make
   # 或在构建树中用 run_emulator：
   bin/nyuzi_emulator -v software/apps/hello_world/hello_world.hex
   ```
2. **捕获踪迹**：`-v` 会让模拟器把每条指令的副作用打到 stdout。hello_world 主体就是 `printf("Hello World\n")`，最终会落到对 `0xffff0048`（`REG_SERIAL_OUTPUT`，见 `device.h`）的 store。
3. **定位 UART 写事件**：在踪迹里找形如
   ```
   0000xxxx [th 0] memory store size 4 ffff0048 00000048
   ```
   的一行（`0x48` 是字符 `'H'` 的 ASCII）。`ffff0048` 即串口输出寄存器地址。
4. **回溯调用链**（模块 4.3）：该 store 因最高位 `10` 进入 `execute_memory_access_inst` → `MEM_LONG` 分支 → `execute_scalar_load_store_inst` → 因地址 `>= DEVICE_BASE_ADDRESS` 走 `write_device_register`，从而把字符送到宿主终端。
5. **解码指令**（模块 4.4）：用 `instruction-set.h` 的 `enum memory_op` 确认这是 `MEM_LONG`，对照反汇编确认操作码字段。
6. **理解调度**（模块 4.2）：整条踪迹只出现 `[th 0]`，因为复位时只有线程 0 使能；hello_world 的 crt0 在 `main` 返回后写 `CR_SUSPEND_THREAD`（编号 20，见 `enum control_register`）让线程 0 自停，`thread_enable_mask` 归零，模拟器退出。
7. **解读停机统计**：模拟器退出前会打印 `N total instructions`（来自 `dump_instruction_stats`），这个 N 是功能指令数，**不是周期数**。

**需要观察的现象 / 预期结果**：

- stdout 先打印一连串 `s0 <= ...` / `memory store ...` 踪迹行，最后打印 `Hello World`（这是踪迹里的 UART store 经宿主转发后出现的程序输出）与 `N total instructions`。
- 你能用一句话说清：「模拟器把 `printf` 解释成一串标量 store，其中写 `0xffff0048` 的那一条经设备分发变成宿主终端字符；整个过程没有缓存、没有流水线、没有周期概念。」

> 若本地尚未安装 Nyuzi 工具链（`/usr/local/llvm-nyuzi` 下的 `clang`/`elf2hex`）或未构建出 `bin/nyuzi_emulator`，步骤 1–2 标记「待本地验证」；但步骤 3–7 的调用链与编码解读可纯靠本讲引用的源码完成。

---

## 6. 本讲小结

- **模拟器 = 功能模型，非周期精确**。它只维护架构状态（寄存器、内存、PC、控制寄存器、TLB），不建模微架构（流水线、缓存、记分牌、LRU）。`processor.c` 里 `memory` 就是一块平坦数组，没有 L1/L2。
- **入口与三种模式**。`main.c` 解析命令行后按 normal / cosim / gdb 三模式进入不同主循环；normal 模式靠 `while (execute_instructions(proc, 1000000))` 批量执行并周期性轮询宿主输入。
- **三层结构 + 轮询调度**。处理器→核→线程三层结构体；`thread_enable_mask` 位图决定谁活着，`execute_instructions` 用 `next_set_bit` 在使能线程间轮询，每线程每次走一条指令；复位只使能线程 0。
- **解释式派发**。`execute_instruction` 取一个 32 位字、按最高几位（`110`/`0`/`10`/`1111`/`1110`）分流到五大处理函数，直接改写线程状态；踪迹 PC 用 `pc-4` 还原为本指令地址。
- **副作用跟踪是调试与协同仿真的接口**。`-v` 经 `set_scalar_reg`/`set_vector_reg`/store 路径打印标量写、向量写（带 16 位掩码）、内存写；同一套钩子在 cosim 模式下变成比对事件。
- **共享 ISA 是验证基石**。`instruction-set.h` 的枚举数值与硬件 `defines.svh` 同构，使模拟器成为可逐条比对的功能金标准。

---

## 7. 下一步学习建议

- **下一讲 u8-l2（模拟器设备与外设仿真）**：本讲的 store 到 `0xffff0048` 是怎么变成宿主字符、帧缓冲窗口与虚拟 SD/MMC 设备如何工作——进入 `device.c` / `fbwindow.c` / `sdmmc.c`。
- **u8-l3（协同仿真验证机制）**：本讲反复提到的「副作用比对」正式展开——看 `cosimulation.c` 如何读硬件 `+trace` 事件，与 `set_scalar_reg` 等钩子逐一对照。
- **回看硬件对照**：读 `hardware/core/instruction_decode_stage.sv`（u4-l2）和 `writeback_stage.sv`（u7-l3），把硬件解码/写回与本讲的 `execute_instruction` 派发对照，体会「同一 ISA、两个实现」的取舍。
- **性能剖析伏笔**：`dump_instruction_stats` 只在编译时定义 `DUMP_INSTRUCTION_STATS` 才输出分类统计（向量/load/store/分支/立即数算术/寄存器算术），这为 u11-l2 的性能计数与 profiling 埋下线索。
