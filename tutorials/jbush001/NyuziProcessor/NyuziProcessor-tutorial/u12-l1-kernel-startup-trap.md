# 内核启动与陷阱/系统调用

## 1. 本讲目标

本讲从「裸机程序」切换到「有内核的程序」，讲解 `software/kernel/` 这个微型操作系统内核的三件事：

1. **启动加载**：内核自己如何被引导起来、如何打开 MMU、如何把用户程序 `program.elf` 装进一个独立地址空间并跳进去运行。
2. **陷阱入口**：当用户程序触发系统调用、缺页、中断或非法操作时，硬件把控制权交给唯一的陷阱向量 `trap_entry`，它如何保存现场、切到内核栈、调用 C 派发函数 `handle_trap`，再无损返回。
3. **系统调用派发**：`syscall` 指令如何变成一次陷阱，`handle_syscall` 如何用一个 `switch` 把系统调用号分派到具体处理函数，并把返回值送回用户态。

学完本讲，你应该能在源码里画出「一次 `printf` 从用户态走到内核再回到用户态」的完整调用链，并理解每一步发生在哪个文件、哪几行。

## 2. 前置知识

本讲建立在前两篇讲义之上，请先回忆两个关键认知：

**与 u9-l1（裸机启动）的对照。** 在裸机环境下（`libos/bare-metal`），`crt0.S` 直接把栈设好就 `call main`，程序运行在 **supervisor 模式、MMU 关闭**，可以任意读写硬件寄存器，没有任何权限隔离。而本讲的内核环境下：

- 内核运行在 **supervisor 模式、MMU 打开**，并且把自己链接到高虚拟地址 `0xc0000000`（见 [software/kernel/CMakeLists.txt:21](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/CMakeLists.txt#L21)）。
- 用户程序运行在 **user 模式**，不能直接读写硬件，也不能直接执行特权指令；它要获得任何内核服务，都必须主动触发一次 **陷阱（trap）**。
- 用户程序输出文本不再直接写 MMIO 地址 `0xffff0048`，而是调用 `write_console` 系统调用，由内核代劳。

**与 u7-l3（硬件侧陷阱与回滚）的衔接。** u7-l3 讲的是硬件：检测到异常后，硬件把异常原因写进控制寄存器 `CR_TRAP_CAUSE`，把返回 PC 写进 `CR_TRAP_PC`，保存原标志位到 `CR_SAVED_FLAGS`，并 **回滚取指** 到 `CR_TRAP_HANDLER` 指向的地址。本讲讲的正是那个被指向的地址——内核用汇编写的 `trap_entry`。本讲是 u7-l3 的「软件下集」。

你需要记住的控制寄存器编号（来自 [software/kernel/asm.h:21-41](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/asm.h#L21-L41)）：

| 编号 | 名字 | 作用 |
|---|---|---|
| 0 | `CR_CURRENT_HW_THREAD` | 当前硬件线程号 |
| 1 | `CR_TRAP_HANDLER` | 陷阱向量地址（硬件回滚到这里） |
| 2 | `CR_TRAP_PC` | 陷阱返回 PC（`eret` 跳到这里） |
| 3 | `CR_TRAP_CAUSE` | 陷阱原因 |
| 4 | `CR_FLAGS` | 当前标志位 |
| 8 | `CR_SAVED_FLAGS` | 陷阱时保存的原标志位 |
| 10 | `CR_PAGE_DIR_BASE` | 页目录物理地址 |
| 11/12 | `CR_SCRATCHPAD0/1` | 两个 32 位暂存槽（陷阱入口的救命稻草） |
| 19 | `CR_SYSCALL_INDEX` | `syscall` 指令的立即数 |
| 21 | `CR_RESUME_THREAD` | 唤醒硬件线程 |

标志位（[software/kernel/asm.h:44-46](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/asm.h#L44-L46)）：`FLAG_INTERRUPT_EN=1`、`FLAG_MMU_EN=2`、`FLAG_SUPERVISOR_EN=4`。

陷阱类型（[software/kernel/asm.h:49-60](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/asm.h#L49-L60)）：`TT_SYSCALL=4`、`TT_PAGE_FAULT=6`、`TT_TLB_MISS=7`、`TT_INTERRUPT=3` 等。

一个贯穿全讲的灵魂指令是 **`eret`**：它不是普通跳转，而是「同时」从 `CR_TRAP_PC` 恢复 PC、从 `CR_SAVED_FLAGS` 恢复标志位（从而切换特权级与开关中断/MMU）、从 `CR_SAVED_SUBCYCLE` 恢复子周期状态。本讲你会看到 `eret` 被用在两个截然不同的场合：**从陷阱返回** 和 **作为启动跳板**。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [software/kernel/start.S](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/start.S) | 内核真入口 `_start`：线程 0 建页表、登记陷阱/TLB 处理函数、用 `eret` 跳板打开 MMU 进入 `kernel_main`；其余线程进入 `thread_n_main` |
| [software/kernel/main.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/main.c) | `kernel_main`：初始化虚拟内存/堆/线程子系统，唤醒其余线程，`exec_program("program.elf")` 加载用户程序，进入空闲调度循环 |
| [software/kernel/trap_entry.S](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/trap_entry.S) | 陷阱向量 `trap_entry`（保存/切换栈/恢复/`eret`）、`jump_to_user_mode`、`tlb_miss_handler`、`enable/disable_interrupts` |
| [software/kernel/trap.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/trap.c) | C 派发器 `handle_trap`：按 `CR_TRAP_CAUSE` 分派到缺页处理、系统调用、中断或崩溃 |
| [software/kernel/syscall.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/syscall.c) | `handle_syscall`：用 `switch` 把系统调用号分派到各处理分支 |
| [software/kernel/syscalls.h](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/syscalls.h) | 系统调用号表（`SYS_write_console=10` 等） |
| [software/kernel/asm.h](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/asm.h) | `CR_*`/`TT_*`/`FLAG_*` 常量与 `TRAP_FRAME_SIZE` |

辅助但重要的文件：[software/kernel/thread.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/thread.c) 的 `exec_program`、[software/kernel/loader.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/loader.c) 的 `load_program`（ELF 加载）、[software/kernel/user_copy.S](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/user_copy.S)（安全拷贝用户内存）、以及用户侧的 [software/libs/libos/kernel/syscall.S](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/kernel/syscall.S)（系统调用桩）。

---

## 4. 核心概念与源码讲解

### 4.1 启动加载

#### 4.1.1 概念说明

「启动加载」要回答两个问题：内核自己怎么跑起来？用户程序怎么被装进来？

内核不是凭空出现的。在 FPGA 上，片上 ROM（`software/bootrom/`）里的串口加载器先把 `kernel.hex` 拉进内存；在模拟器/Verilator 上，测试框架直接把 `kernel.hex` 作为内存镜像加载。无论哪种方式，处理器复位后都从地址 0 取第一条指令，而 `kernel.hex` 的入口 `_start` 就在地址 0。

但内核被链接在 `0xc0000000` 这个高虚拟地址。复位时 MMU 是关的，地址 0 是物理地址；内核最终想跑在 `0xc0000000` 这个虚拟地址上。这就需要一个 **跳板（trampoline）**：先在物理地址上建好页表、登记好处理函数，再用一条 `eret` 同时「打开 MMU + 切换到高虚拟地址 + 切换到内核栈」。

至于用户程序，内核并不在启动时把它一起加载，而是在 `kernel_main` 里调用 `exec_program("program.elf")`：从一个只读文件系统（模拟器里是虚拟 SD 卡镜像）读出 ELF，把可加载段映射进一个新建的地址空间，然后创建一个线程，用 `jump_to_user_mode` 把它「弹」到用户态。

#### 4.1.2 核心流程

内核启动的主干（线程 0）：

```
_start (物理地址 0)
 ├─ 设 gp（全局偏移表）
 ├─ getcr s0, 0  读线程号
 ├─ if 线程号 != 0:  跳 start_thread_n (其余线程)
 ├─ 线程0:
 │    ├─ call boot_setup_page_tables   建临时页表
 │    ├─ setcr CR_TLB_MISS_HANDLER = tlb_miss_handler   登记 TLB 缺失处理
 │    ├─ setcr CR_TRAP_HANDLER    = trap_entry          登记陷阱向量
 │    ├─ setcr CR_TRAP_PC         = kernel_main         设返回 PC
 │    └─ b trampoline
 ├─ trampoline (线程0 与 其余线程汇合):
 │    ├─ setcr CR_PAGE_DIR_BASE = 页目录物理地址
 │    ├─ setcr CR_SAVED_FLAGS = MMU|SUPERVISOR|INTERRUPT
 │    ├─ 设内核栈 (0xffff0000 基址, 每线程 16KiB)
 │    └─ eret   ← 同时开 MMU、进高地址、切栈
 ↓
kernel_main (高虚拟地址, supervisor, MMU 开)
 ├─ vm_page_init / vm_translation_map_init / 堆 / vm_address_space_init ...
 ├─ setcr CR_RESUME_THREAD = 0xffffffff   唤醒其余硬件线程
 ├─ spawn_kernel_thread(grim_reaper)       内核垃圾回收线程
 ├─ exec_program("program.elf")            加载并运行用户程序
 └─ for(;;) reschedule()                   空闲: 无线程可调度则停机
```

关键点：

- **线程 0 与其余线程分叉**。复位后只有线程 0 醒着（u9-l2 已讲），它独占完成「建页表 + 登记处理函数」这些只能做一次的初始化；其余线程在被 `CR_RESUME_THREAD` 唤醒后从 `_start` 重新进入，但通过 `getcr 0` 发现自己不是线程 0，直接走 `start_thread_n` 跳到 `thread_n_main`，跳过初始化。
- **`eret` 当跳板用**。`trampoline` 把目标 PC（`kernel_main` 或 `thread_n_main`）放进 `CR_TRAP_PC`，把目标标志位放进 `CR_SAVED_FLAGS`，然后 `eret`——这一条指令同时完成「打开 MMU、跳到高虚拟地址、切换特权级」。这是 `eret` 的非典型用法：不是从陷阱返回，而是借它的「原子换 PC+标志」能力完成模式切换。

#### 4.1.3 源码精读

`_start` 的分叉与初始化（线程 0 部分）：

- [software/kernel/start.S:27-58](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/start.S#L27-L58)：先设 `gp`，读线程号；线程 0 建页表、登记 `tlb_miss_handler` 到 `CR_TLB_MISS_HANDLER`、登记 `trap_entry` 到 `CR_TRAP_HANDLER`，并把 `kernel_main` 写进 `CR_TRAP_PC`。

登记这两个处理函数是本讲的根基——后续所有陷阱和 TLB 缺失都会跳到这两个地址：

```
lea s0, tlb_miss_handler
and s0, s0, 0xffffff        // 屏蔽高位，转成物理地址（此时 MMU 还没开）
setcr s0, CR_TLB_MISS_HANDLER

lea s0, trap_entry
setcr s0, CR_TRAP_HANDLER
```

其余线程的捷径：

- [software/kernel/start.S:60-62](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/start.S#L60-L62)：把 `CR_TRAP_PC` 设成 `thread_n_main`，然后同样落入 `trampoline`。

`eret` 跳板：

- [software/kernel/start.S:67-87](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/start.S#L67-L87)：设 `CR_PAGE_DIR_BASE`、设 `CR_SAVED_FLAGS = MMU|SUPERVISOR|INTERRUPT`、按线程号算出内核栈地址（基址 `0xffff0000`，每线程 16KiB），最后 `eret` 进入高地址的内核入口。

`kernel_main` 的初始化与加载：

- [software/kernel/main.c:38-69](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/main.c#L38-L69)：依次初始化物理页管理、翻译表、内核堆、地址空间、页缓存、内核进程、线程；用 `CR_RESUME_THREAD` 唤醒其余线程；起 `grim_reaper` 回收线程；调用 `exec_program("program.elf")`；进入「无线程可调度就停机、否则一直 `reschedule`」的空闲循环。

用户程序是如何被加载的——`exec_program` 的内部：

- [software/kernel/thread.c:337-366](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/thread.c#L337-L366)：分配 `process`、创建独立地址空间 `create_address_space()`、调用 `load_program` 把 ELF 段映射进去、再 `spawn_thread_internal` 创建首个线程。
- [software/kernel/loader.c:28-81](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/loader.c#L28-L81)：打开文件、校验 ELF 魔数与 `e_machine == EM_NYUZI`、读段表，随后把每个 `PT_LOAD` 段映射为一段虚拟内存区域。

注意 `exec_program` 并不直接跳进用户态。它创建的线程被调度器选中后，会从 `new_process_start` 这个内核态「着陆点」执行：

- [software/kernel/thread.c:323-335](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/thread.c#L323-L335)：调用 `jump_to_user_mode`，用 `eret` 把新线程弹到用户入口地址。

`jump_to_user_mode` 本身只有 5 行，是「`eret` 当跳板」的又一实例：

- [software/kernel/trap_entry.S:163-168](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/trap_entry.S#L163-L168)：把入口 PC 写进 `CR_TRAP_PC`，把 `MMU|INTERRUPT`（**不含 SUPERVISOR**）写进 `CR_SAVED_FLAGS`，切到用户栈，`eret`——于是下一条指令取自用户入口、特权级降为 user。

#### 4.1.4 代码实践

**目标**：在模拟器上跑通一个内核用户态程序，亲眼看「内核加载 ELF → 用户态 `printf` → 内核代为输出」的链路。

**操作步骤**：

1. 进入测试目录构建并运行内核测试 `hello.c`（它是一个 `image_type='user'` 的用户程序）：

   ```bash
   cd tests/kernel
   python3 runtest.py emulator -k hello
   ```

   若想单跑一个文件，可参考 [tests/kernel/runtest.py:24-27](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/kernel/runtest.py#L24-L27)：它先用 `build_program(..., image_type='user')` 编译出 `program.elf`，再交给 `run_kernel`。
2. `run_kernel` 的内部逻辑见 [tests/test_harness.py:361-397](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/test_harness.py#L361-L397)：先用 `mkfs` 把 `program.elf` 打包成虚拟 SD 卡镜像 `fsimage.bin`，再用 `kernel.hex` 作为内核镜像启动，并把该镜像挂为块设备——这正是 `exec_program("program.elf")` 能读到用户程序的原因。

**需要观察的现象 / 预期结果**：终端打印 `Hello World`（见 [tests/kernel/hello.c:21-22](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/kernel/hello.c#L21-L22) 的 `// CHECK: Hello World`）。这条文本是用户态 `printf` 经系统调用由内核的 `kprintf` 写到 UART 的——这条链路将在 4.3 节完整拆开。

> 若本地未装好工具链，运行命令的具体输出标注为「待本地验证」，但上述文件与行号可据以静态阅读整条链路。

#### 4.1.5 小练习与答案

**练习 1**：为什么线程 0 必须在 `eret` 跳板 **之前** 完成 `boot_setup_page_tables`、登记 `CR_TRAP_HANDLER` 和 `CR_TLB_MISS_HANDLER`，而不能放到 `kernel_main` 里再做？

> **答案**：`eret` 一旦打开 MMU，此后所有取指与访存都走虚拟地址，若此时页表未建好会立刻触发未映射地址；而一旦发生陷阱或 TLB 缺失，硬件会回滚到 `CR_TRAP_HANDLER` / `CR_TLB_MISS_HANDLER`，若它们尚未登记，处理器将跳到未定义地址而崩溃。所以这些「地基」必须在切到虚拟地址世界之前铺好。

**练习 2**：`jump_to_user_mode` 写入 `CR_SAVED_FLAGS` 时用的是 `FLAG_MMU_EN | FLAG_INTERRUPT_EN`，**故意不加** `FLAG_SUPERVISOR_EN`。这一步在做什么？

> **答案**：`eret` 会用 `CR_SAVED_FLAGS` 恢复标志位。不含 `SUPERVISOR_EN` 意味着返回后处于 user 模式——这正是「从内核降到用户态」的权限下降。同时保留 `MMU_EN` 让用户程序继续受地址翻译保护。

---

### 4.2 陷阱入口

#### 4.2.1 概念说明

`trap_entry` 是整个内核唯一的陷阱向量（4.1 已看到它被登记进 `CR_TRAP_HANDLER`）。无论系统调用、缺页、非法指令还是中断，硬件都会回滚到这里。它是一个纯汇编函数，职责明确：

1. **保存现场**：把所有标量寄存器存到一个「陷阱帧（trap frame）」里。
2. **切到内核栈**：如果陷阱来自用户态，要换成内核栈；如果本来就来自内核态，沿用当前栈。
3. **调用 C 派发器** `handle_trap(frame)`。
4. **恢复现场** 并用 `eret` 返回。

这里有两个精妙之处值得专门讲：

**鸡生蛋问题与 scratchpad。** 要保存寄存器，你总得先有几个寄存器可用——但所有通用寄存器都装着用户程序的活数据，不能随便覆盖。`trap_entry` 的解法是用两个控制寄存器暂存槽 `CR_SCRATCHPAD0/1`：先把 `s0`、`s1` 塞进去腾出两个可用寄存器，再用这两个寄存器去算地址、做判断。

**不保存向量寄存器。** 注释明说：`trap_entry` 只存标量寄存器，不存向量寄存器（[software/kernel/trap_entry.S:22-26](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/trap_entry.S#L22-L26)）。代价是 **内核不能使用向量指令**；若某线程要用向量寄存器，必须在上下文切换时另行保存。这是一个有意识的设计取舍：绝大多数陷阱只发生一次、要求极低延迟，不值得每次都存 512 位的向量寄存器组。

#### 4.2.2 核心流程

陷阱帧的内存布局（`TRAP_FRAME_SIZE = 192` 字节，[software/kernel/asm.h:66](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/asm.h#L66)）：

| 栈偏移 | 内容 | 对应 C 字段 |
|---|---|---|
| 0 … 111 | `s0 … s27`（28 个） | `frame->gpr[0..27]` |
| 112 | `gp` | `frame->gpr[28]` |
| 116 | `fp` | `frame->gpr[29]` |
| 120 | 用户/旧 `sp` | `frame->gpr[30]` |
| 124 | `ra` | `frame->gpr[31]` |
| 128 | `CR_TRAP_PC`（返回 PC） | `frame->pc` |
| 132 | `CR_SAVED_FLAGS` | `frame->flags` |
| 136 | `CR_SAVED_SUBCYCLE` | `frame->subcycle` |

C 侧的 `struct interrupt_frame`（[software/kernel/trap.c:24-30](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/trap.c#L24-L30)）正是这张表的 C 视图：`gpr[32]`（128 字节）后接 `pc`、`flags`、`subcycle`。汇编负责按偏移摆放，C 负责按字段读写，二者靠同一份布局约定对齐。

`trap_entry` 的执行流程：

```
trap_entry:
 ├─ s0 → CR_SCRATCHPAD0,  s1 → CR_SCRATCHPAD1     腾出 2 个寄存器
 ├─ 读 CR_SAVED_FLAGS, 测 SUPERVISOR_EN
 │   ├─ user 模式: 按 CR_CURRENT_HW_THREAD 查内核栈表, 存用户 sp, 切到内核栈
 │   └─ supervisor 模式: 沿用当前栈, 存旧 sp
 ├─ 恢复 s0/s1,  sp -= 192                          预留陷阱帧
 ├─ 存 s0..s27, gp, fp, ra; 存 CR_TRAP_PC/SAVED_FLAGS/SAVED_SUBCYCLE
 ├─ s0 = sp (帧指针), 设 gp,  call handle_trap      ← 进入 C
 ├─ 恢复 s0..s27/gp/fp/ra
 ├─ 把帧里的 pc/flags/subcycle 写回 CR_TRAP_PC/SAVED_FLAGS/SAVED_SUBCYCLE
 ├─ 恢复 s0, 恢复 sp
 └─ eret                                             ← 原子返回用户态
```

一个关键时序细节：**进入陷阱时硬件已自动关中断、进特权态**（u7-l2 已讲）。因此 `trap_entry` 本体运行在中断关闭状态。但 C 派发器 `handle_trap` 在处理可能很耗时的工作（缺页、系统调用）前会主动 `enable_interrupts()`，处理完再 `disable_interrupts()`，以保持系统对中断的响应。返回前 `trap_entry` 会把 **原始** 的 `CR_SAVED_FLAGS` 写回——所以无论 C 代码中途如何开关中断，`eret` 总能把用户态的原始标志位（含原始中断使能）还原回去。

#### 4.2.3 源码精读

**入口的救命稻草与模式判定**：

- [software/kernel/trap_entry.S:30-48](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/trap_entry.S#L30-L48)：先把 `s0`/`s1` 存进 scratchpad；读 `CR_SAVED_FLAGS` 与 `FLAG_SUPERVISOR_EN` 相与，用 `bnz` 判定进入陷阱前是否已是 supervisor。若是 user 模式，则读 `CR_CURRENT_HW_THREAD`、乘 4、从 `trap_kernel_stack_addr` 数组取出本线程的内核栈指针，把用户 `sp` 存到帧内偏移 120 处，再 `move sp, s1` 切栈。

这里 `trap_kernel_stack_addr` 是一个「指向指针数组的指针」（[software/kernel/trap_entry.S:150](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/trap_entry.S#L150)），数组里每个硬件线程一个内核栈基址，所以两次 `load_32`（取数组基址、取栈指针）。

**预留帧并批量保存**：

- [software/kernel/trap_entry.S:55-97](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/trap_entry.S#L55-L97)：恢复 `s0`/`s1`，`sp -= TRAP_FRAME_SIZE`；然后一连串 `store_32` 把 `s0..s27`、`gp`、`fp`、`ra` 存入帧，再把 `CR_TRAP_PC`、`CR_SAVED_FLAGS`、`CR_SAVED_SUBCYCLE` 读出存入帧的 128/132/136 偏移。

**进入 C 派发器**：

- [software/kernel/trap_entry.S:99-105](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/trap_entry.S#L99-L105)：`s0 = sp`（把帧指针作为第一个参数；Nyuzi C ABI 用 `s0` 传首参），设置 `gp` 后 `call handle_trap`。

**恢复与返回**：

- [software/kernel/trap_entry.S:107-146](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/trap_entry.S#L107-L146)：重新加载 `s0..s27/gp/fp/ra`；把帧里的 `pc/flags/subcycle` 写回三个 `CR_SAVED_*`；恢复 `s0`、恢复 `sp`；`eret`。

**TLB 缺失处理**（软件管理 TLB 的内核侧，承接 u7-l1）：

- [software/kernel/trap_entry.S:197-239](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/trap_entry.S#L197-L239)：读 `CR_PAGE_DIR_BASE`，按虚拟地址的高 10 位查页目录、低 10 位查页表，得到页表项后用 `dtlbinsert`/`itlbinsert` 插入 TLB，再 `eret` 重试原访问。

**开关中断的三个小函数**：

- [software/kernel/trap_entry.S:242-256](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/trap_entry.S#L242-L256)：`disable_interrupts` 读 `CR_FLAGS` 并清掉 `INTERRUPT_EN`（保留 MMU/SUPERVISOR）后写回；`enable_interrupts` 则或上 `INTERRUPT_EN`；`restore_interrupts` 直接把传入值写回 `CR_FLAGS`。注意它们操作的是 `CR_FLAGS`（当前运行标志），而陷阱返回用的是 `CR_SAVED_FLAGS`（原始标志），二者分工不同。

**C 派发器 `handle_trap`**：

- [software/kernel/trap.c:114-158](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/trap.c#L114-L158)：读 `CR_TRAP_CAUSE`，取低 4 位（`& 0xf`）作为类型，用 `switch` 分派：
  - `TT_PAGE_FAULT` / `TT_ILLEGAL_STORE`（[trap.c:122-136](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/trap.c#L122-L136)）：读 `CR_TRAP_ADDR`，开中断后调 `handle_page_fault`；处理失败则按是否设了 `fault_handler` 决定跳到用户拷贝的容错点或调用 `bad_fault` 杀线程。
  - `TT_SYSCALL`（[trap.c:138-149](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/trap.c#L138-L149)）：详见 4.3。
  - `TT_INTERRUPT`（[trap.c:151-153](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/trap.c#L151-L153)）：调 `handle_interrupt`。
  - 其余类型走 `default` → `bad_fault`（[trap.c:155-156](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/trap.c#L155-L156)）。

注意 `trap_cause & 0x10` 是「store 标志」、`& 0x20` 是「dcache 标志」（见 [trap.c:173-175](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/trap.c#L173-L175)），这正是 u7-l3 讲的「`trap_cause_t` 额外携带 dcache/store 两个标志位」，让软件能区分异常来自取指还是访存、读还是写。

`bad_fault` 决定生死（[software/kernel/trap.c:95-112](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/trap.c#L95-L112)）：若崩溃发生在 supervisor 模式（`flags & FLAG_SUPERVISOR_EN`），那是内核 bug，直接 `panic`；否则是用户程序越界，杀掉该线程（`thread_exit(1)`），不影响内核与其他进程。

#### 4.2.4 代码实践

**目标**：用一个故意触发缺页的用户程序，观察「陷阱入口 → C 派发 → 杀线程」的全过程。

**操作步骤**：

1. 运行内核测试 `crash.c`：

   ```bash
   cd tests/kernel
   python3 runtest.py emulator -k crash
   ```

2. 阅读 [tests/kernel/crash.c:17-27](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/kernel/crash.c#L17-L27)：用户程序向地址 `4` 写一个字。地址空间最低一页被故意留空以捕获空指针，故这是一次 store 缺页。

**需要观察的现象 / 预期结果**：输出三行（见 `// CHECK` 注释）：

```
user space thread 5 crashed
Page Fault @00000004 dcache store
init process has exited, shutting down
```

**解读**：`dcache store` 说明硬件判定这是一次数据访问（非取指）、写操作（非读）——对应 `trap_cause` 的 `0x20`（dcache）与 `0x10`（store）两个标志位。`user space thread 5 crashed` 来自 `bad_fault` 的 user 分支（[trap.c:108-110](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/trap.c#L108-L110)），它调用 `thread_exit` 而非 `panic`，所以内核继续运行，最终在空闲循环发现 init 进程已无线程而 `CR_SUSPEND_THREAD` 停机（[main.c:61-65](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/main.c#L61-L65)）。

> 输出标注为「待本地验证」（取决于是否本地装好工具链）；但 `crash.c` 的 `// CHECK` 行即为预期断言。

#### 4.2.5 小练习与答案

**练习 1**：`trap_entry` 为什么不保存向量寄存器？如果内核某条路径非用向量指令不可，该怎么办？

> **答案**：陷阱很频繁，每次都存 16 个 512 位向量寄存器代价过高，故只存标量；代价是内核默认不能用向量指令。若必须用，应在「上下文切换」时把当前线程的向量寄存器存到该线程的控制块里（这正是 `context_switch.S` 的职责，留待 u12-l3），而不能依赖 `trap_entry`。

**练习 2**：`trap_entry` 末尾把 `CR_SAVED_FLAGS` 从帧里写回，然后才 `eret`。但 `handle_trap` 中途明明用 `enable_interrupts`/`disable_interrupts` 改过 `CR_FLAGS`。这两者会冲突吗？

> **答案**：不会。`enable/disable_interrupts` 改的是 `CR_FLAGS`（当前运行标志），只影响内核处理期间的实时中断开关；而 `eret` 用的是 `CR_SAVED_FLAGS`（陷阱进入瞬间硬件保存的原始标志）。`trap_entry` 返回前把帧里的原始 `SAVED_FLAGS` 写回，所以无论 C 代码怎么拨弄 `CR_FLAGS`，`eret` 都把用户态原始标志位（含原始中断使能）原样还原。

---

### 4.3 系统调用派发

#### 4.3.1 概念说明

系统调用（syscall）是用户态向内核「合法地」请求服务的唯一正门。在 Nyuzi 上它由一条 `syscall imm` 指令触发：硬件把它当作一次 `TT_SYSCALL` 陷阱，并把指令里的立即数 `imm` 放进控制寄存器 `CR_SYSCALL_INDEX`（编号 19）。于是「请求哪种服务」就编码在这个立即数里——也就是系统调用号。

整条系统调用链路跨用户态与内核态两侧：

- **用户侧桩**（[software/libs/libos/kernel/syscall.S](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/kernel/syscall.S)）：每个系统调用包装成一行 `syscall SYS_xxx; ret`，参数和返回值都走标量寄存器 `s0..s5`。
- **内核侧派发**（[software/kernel/syscall.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/syscall.c)）：`handle_syscall` 用 `switch(index)` 分派。

一个必须理解的设计：**参数与返回值都借用通用寄存器**。Nyuzi 的 C 调用约定用 `s0..s5` 传前 6 个标量参数、`s0` 存返回值、`s31` 存返回地址。由于用户侧桩就是 `syscall; ret`，参数天然已在 `s0..s5`，内核只需从陷阱帧的 `frame->gpr[0..5]`（即 `s0..s5`）取参，把返回值写回 `frame->gpr[0]`（即 `s0`），用户侧 `ret` 后调用者就能从 `s0` 拿到结果——无需任何额外拷贝。

另一个要点是 **`user_copy` 的容错机制**：内核绝不能直接解引用用户传来的指针（那可能非法，会让内核自己崩溃）。`user_copy` 在拷贝前先在本线程的 `fault_handler` 槽登记一个容错地址；若拷贝途中触发缺页，`handle_trap` 的 `TT_PAGE_FAULT` 分支发现 `fault_handler` 非空，就把 `frame->pc` 改成那个容错地址（[trap.c:129-130](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/trap.c#L129-L130)），于是 `eret` 后跳到 `uc_fault` 返回 `-1`，而不是杀线程。这样内核面对恶意/越界指针也能优雅返回 `EFAULT`。

#### 4.3.2 核心流程

一次 `printf("Hello World\n")` 的完整链路（这是本讲串起三模块的「主线」）：

```
用户态 hello.c: printf("Hello World\n")
 │  (libc, 平台无关)
 ├─ vfprintf → fputc(ch, stdout)
 ├─ fputc 见 file==stdout → write_console(&_ch, 1)     [stdio.c:118-121]
 │  (libos/kernel/syscall.S)
 ├─ write_console:  syscall SYS_write_console(=10)      [syscall.S:51]
 │      ↓ 触发 TT_SYSCALL 陷阱, 立即数 10 → CR_SYSCALL_INDEX
 │      ↓ 硬件回滚到 trap_entry, 保存现场, 切内核栈
 ├─ trap_entry → handle_trap(frame)                     [trap_entry.S:105]
 ├─ handle_trap: case TT_SYSCALL                         [trap.c:138-149]
 │    ├─ index = getcr CR_SYSCALL_INDEX        // = 10
 │    ├─ enable_interrupts()                    // 系统调用期间允许中断
 │    ├─ frame->gpr[0] = handle_syscall(10, gpr[0..5])
 │    ├─ frame->pc += 4                         // 跳过 syscall 指令本身
 │    └─ disable_interrupts()
 ├─ handle_syscall: case SYS_write_console              [syscall.c:40-55]
 │    ├─ 校验 length 上限
 │    ├─ user_copy(tmp, 用户指针, length)        // 安全拷出用户内存
 │    └─ kprintf("%s", tmp)                      // 内核输出到 UART
 │      ↓ 返回 0 → frame->gpr[0]
 ├─ trap_entry 恢复现场, frame->pc 已 +4, eret
 ↓
用户态 write_console 桩: ret  (s0 = 0)  → 继续 fputc 循环
```

要点提炼：

- **`syscall` 指令本身要被跳过**。陷阱返回的 PC 默认指向触发指令。但系统调用是「主动请求」，返回后应执行 `syscall` 的下一条指令，所以 `handle_trap` 显式 `frame->pc += 4`（[trap.c:147](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/trap.c#L147)）。这与缺页/非法指令不同——后者返回时要 **重新执行** 触发指令。
- **系统调用期间开中断**。缺页处理与系统调用都可能耗时，故 `handle_trap` 在调 `handle_page_fault`/`handle_syscall` 前开中断、后关中断（[trap.c:125-135](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/trap.c#L125-L135)、[trap.c:141-148](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/trap.c#L141-L148)），让定时器中断等仍能触发调度。

#### 4.3.3 源码精读

**用户侧桩与 errno 转换**：

- [software/libs/libos/kernel/syscall.S:21-60](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/syscall.S#L21-L60)：`SYSCALL(name)` 宏展开为 `name: syscall SYS_##name; ret`；`SYSCALL_WITH_ERRNO` 额外在返回值 `s0 < 0` 时把它取反存进按线程索引的 `__errno_array`，并让函数返回 `-1`（POSIX 风格）。`write_console` 用的是不带 errno 的简单版（[syscall.S:51](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/kernel/syscall.S#L51)）。

注意它 `#include "../../../kernel/syscalls.h"`（[syscall.S:18](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/kernel/syscall.S#L18)）——用户态库与内核共享同一份系统调用号表，保证两侧编号一致。

**系统调用号表**：

- [software/kernel/syscalls.h:19-28](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/syscalls.h#L19-L28)：`SYS_spawn_thread=1`、`SYS_get_current_thread_id=2`、`SYS_exec=3`、`SYS_thread_exit=4`、`SYS_init_vga=5`、`SYS_create_area=6`、`SYS_set_perf_counter=7`、`SYS_read_perf_counter=8`、`SYS_get_cycle_count=9`、`SYS_write_console=10`。新增系统调用只需在此加一个宏，并在 `handle_syscall` 加一个 `case`。

**内核派发 `handle_syscall`**：

- [software/kernel/syscall.c:29-131](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/syscall.c#L29-L131)：签名 `handle_syscall(int index, int arg0..arg5)`，用 `switch(index)` 分派。

`SYS_write_console` 是最适合精读的一条，因为它最能体现「内核代用户做事 + 安全拷贝」：

- [software/kernel/syscall.c:40-55](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/syscall.c#L40-L55)：先校验长度 `arg1` 不超过栈缓冲 `tmp` 容量；再 `user_copy(tmp, (void*)arg0, arg1)` 把用户内存安全拷进内核 `tmp`；拷贝失败返回 `-EFAULT`；成功则 `kprintf("%s", tmp)` 输出，返回 `0`。

其他几条体现不同模式：

- `SYS_get_current_thread_id`（[syscall.c:64-65](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/syscall.c#L64-L65)）：直接返回当前线程 id，无需用户内存交互。
- `SYS_thread_exit`（[syscall.c:83-84](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/syscall.c#L83-L84)）：调 `thread_exit`，**不返回**。
- `SYS_create_area`（[syscall.c:92-109](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/syscall.c#L92-L109)）：用 `user_strlcpy` 安全拷出用户传入的 area 名字串，再调 `create_area` 建虚拟内存区域（承接 u12-l2）。
- `default`（[syscall.c:128-130](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/syscall.c#L128-L130)）：未知系统调用号打印告警并返回 `-EINVAL`。

**`user_copy` 的容错实现**：

- [software/kernel/user_copy.S:29-57](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/user_copy.S#L29-L57)：进入时把本线程 `fault_handler` 槽设为 `uc_fault` 标号；逐字节拷贝；若途中缺页，`handle_trap` 会把返回 PC 改写成 `uc_fault`，`eret` 后即执行 `move s0, -1` 返回错误；正常结束则把 `fault_handler` 槽清零再返回。这是「把异常当控制流」的典型手法。

#### 4.3.4 代码实践

**目标**：亲手验证「系统调用号 → `handle_syscall` 分派」的对应关系，并观察每次系统调用的派发。

**操作步骤**：

1. 在 `handle_syscall` 入口（[software/kernel/syscall.c:29](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/syscall.c#L29) 的函数体开头）加一行临时日志：

   ```c
   kprintf("<<syscall %d>>\n", index);
   ```
2. 重新构建内核（在仓库根目录 `cmake . && make`，产物会重新生成 `kernel.hex`）。
3. 重新运行 `tests/kernel/hello.c`（命令同 4.1.4）。

**需要观察的现象 / 预期结果**：在 `Hello World` 之前/之中会穿插大量 `<<syscall 10>>`——因为 `printf` 对每个字符都调用一次 `write_console`（`fputc` 逐字符调用，见 [stdio.c:121](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libc/src/stdio.c#L121)），每次都触发 `SYS_write_console=10`。这印证了「立即数 10 经 `CR_SYSCALL_INDEX` 到达 `handle_syscall` 的 `switch`」。

> 这一步需要修改源码并重新构建，属于建议读者自行尝试的学习操作（你作为读者可改、可还原）；本讲义本身不修改源码。若未本地构建环境，结果标注为「待本地验证」，但上述对应关系可由静态阅读确认。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `handle_trap` 在 `TT_SYSCALL` 分支里要做 `frame->pc += 4`，而 `TT_PAGE_FAULT` 分支 **不做** 这个加法？

> **答案**：`syscall` 是用户主动发起、已被成功「完成」的请求，返回后应执行它的下一条指令，故 PC 要越过它（+4）。缺页则不同：触发指令尚未真正完成（它要访问的页刚被装入 TLB/页表），返回后必须 **重新执行同一条指令** 才能完成原本的访存，故 PC 保持不变。

**练习 2**：若用户程序在 `write_console` 里传入一个完全非法的指针（例如 `0x10`），内核会崩溃吗？为什么？

> **答案**：不会。`SYS_write_console` 调 `user_copy`，而 `user_copy` 已把本线程 `fault_handler` 设为 `uc_fault`。非法指针触发缺页后，`handle_trap` 的 `TT_PAGE_FAULT` 分支发现 `fault_handler` 非空，于是把 `frame->pc` 改成 `uc_fault`（[trap.c:129-130](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/trap.c#L129-L130)），`eret` 后返回 `-1`，`handle_syscall` 据此返回 `-EFAULT`。内核始终不直接解引用用户指针，故不会被用户态拖垮。

**练习 3**：系统调用号在用户态库和内核两处都有定义，它们如何保证一致？

> **答案**：用户态桩 [syscall.S:18](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/kernel/syscall.S#L18) 用 `#include "../../../kernel/syscalls.h"` 直接复用内核那份 [syscalls.h](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/syscalls.h) 头文件，单一事实来源，不会失配。

---

## 5. 综合实践

把本讲三个模块串起来：**跟踪一次用户态系统调用从触发到返回的完整往返路径**。

以 `tests/kernel/hello.c` 的 `printf("Hello World\n")` 为对象，按下表逐格填写每一步对应的 **文件:行号** 与 **关键变量/寄存器**。第一格已示范：

| 步骤 | 发生地（文件:行号） | 关键状态 |
|---|---|---|
| 1. `fputc` 判定 `stdout`，调 `write_console` | [stdio.c:118-121](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libc/src/stdio.c#L118-L121) | `&_ch`→s0, 1→s1 |
| 2. 用户桩执行 `syscall 10` | （自行填写） | `CR_SYSCALL_INDEX`=? |
| 3. 硬件触发 `TT_SYSCALL`，回滚到 `trap_entry` | （自行填写，参见 u7-l3） | `CR_TRAP_CAUSE`=? |
| 4. `trap_entry` 存 s0/s1、判模式、切内核栈、存帧 | （自行填写） | 帧偏移 120 放什么？ |
| 5. `call handle_trap` | （自行填写） | 首参（帧指针）走哪个寄存器？ |
| 6. `handle_trap` 读 `CR_TRAP_CAUSE`，走 `TT_SYSCALL` 分支 | （自行填写） | 为何 `frame->pc += 4`？ |
| 7. 调 `handle_syscall(10, ...)`，走 `SYS_write_console` | （自行填写） | `user_copy` 失败返回什么？ |
| 8. `kprintf` 输出，返回 0 写回 `frame->gpr[0]` | （自行填写） | gpr[0] 对应哪个标量寄存器？ |
| 9. `trap_entry` 恢复现场，写回 `CR_SAVED_FLAGS`，`eret` | （自行填写） | eret 同时恢复了哪三样？ |
| 10. 用户桩 `ret`，调用者从 s0 取返回值，继续下一字符 | [syscall.S:51](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/kernel/syscall.S#L51) | 循环直到 `\n` |

完成后，你应当能解释：为什么这条链路上「参数不经过任何内存拷贝就能在用户与内核间传递」「为什么返回时 `pc` 要 +4 而 `flags` 要从 `CR_SAVED_FLAGS` 还原」「为什么内核不会因为用户指针非法而崩溃」。如果每一格你都能填出文件与行号，本讲就真正贯通了。

## 6. 本讲小结

- 内核入口是 `start.S` 的 `_start`：线程 0 建页表、登记 `trap_entry` 与 `tlb_miss_handler`，再用 `eret` 当跳板「同时打开 MMU + 跳到高虚拟地址 + 切内核栈」，进入 `kernel_main`。
- `kernel_main` 完成虚拟内存/堆/线程子系统初始化后，唤醒其余线程、起 `grim_reaper`，再用 `exec_program("program.elf")` 从虚拟文件系统加载用户程序并投运。
- `trap_entry` 是唯一陷阱向量：用 `CR_SCRATCHPAD0/1` 解决「保存寄存器前没有空闲寄存器」的鸡生蛋问题，按来自 user/supervisor 决定是否切内核栈，存标量寄存器（不存向量）成陷阱帧，调 `handle_trap`，再用 `eret` 原子返回。
- `handle_trap` 按 `CR_TRAP_CAUSE & 0xf` 分派：缺页/非法写走 `handle_page_fault`（失败按 `fault_handler` 容错或杀线程）、系统调用走 `handle_syscall`、中断走 `handle_interrupt`、其余走 `bad_fault`（内核态 panic，用户态杀线程）。
- 系统调用经 `syscall imm` 触发 `TT_SYSCALL`，立即数进 `CR_SYSCALL_INDEX`；`handle_syscall` 用 `switch` 分派；参数与返回值都借用 `s0..s5`，无需额外拷贝；返回时 `frame->pc += 4` 跳过 `syscall` 指令。
- 内核绝不直接解引用用户指针：`user_copy`/`user_strlcpy` 借 `fault_handler` 机制把缺页转成 `-EFAULT`，保证用户态的非法指针不会拖垮内核。

## 7. 下一步学习建议

- **u12-l2 内核虚拟内存管理**：本讲多次提到 `handle_page_fault`、`create_address_space`、`CACHE_DTLB_INSERT`，它们的具体实现（`vm_address_space.c`/`vm_translation_map.c`/`vm_page.c`/`vm_cache.c`/`slab.c`）是下一讲的主题。学完你就能补全「缺页如何被内核处理并返回重试」的下半段。
- **u12-l3 线程、上下文切换与同步原语**：本讲的 `trap_entry` 不保存向量寄存器、`thread_exit`、`reschedule`、`CR_RESUME_THREAD` 都指向内核的线程子系统。下一讲讲 `thread.c`、`context_switch.S`、`rwlock.c`/`spinlock.h`，解释多线程如何在内核中调度与同步。
- 想加深对硬件侧的理解，可回头重读 **u7-l3（陷阱处理与回滚）**，对照本讲看「硬件搭便车到 `CR_TRAP_HANDLER`」与「软件 `trap_entry` 接住」是如何无缝衔接的。
