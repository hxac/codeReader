# 线程、上下文切换与同步原语

> 单元 u12 · 内核与虚拟内存系统 · 第 3 讲（u12-l3，advanced）
> 依赖：u12-l1（内核启动与陷阱/系统调用）、u10-l2（多线程调度与挂起恢复）

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 Nyuzi 内核里「硬件线程」与「软件线程」的区别，以及 `struct thread` / `struct process` 各字段的含义。
- 画出内核调度器 `reschedule` 的工作流程：就绪队列、状态机、时钟抢占。
- 逐行解释 `context_switch.S` 如何保存全部向量寄存器与被调用者保存的标量寄存器、切换栈、再恢复，并理解「新线程第一次运行」是如何被造出来的。
- 说明自旋锁（spinlock）为何要用原子 CAS（即 LL/SC）+ 关中断来保护临界区。
- 说明读写锁（rwlock）的三条公平性规则，以及它与自旋锁在「忙等」与「阻塞」上的根本差异。

本讲只讲内核侧的软件线程抽象与同步原语，不重复 u10-l2 讲过的硬件级 `thread_en` / 挂起唤醒位图，也不重复 u12-l1 讲过的陷阱入口与系统调用派发。

## 2. 前置知识

在进入源码前，先建立四个直觉。

**(1) 硬件线程 vs 软件线程。** Nyuzi 每个核有固定数量的硬件线程（默认 `THREADS_PER_CORE=4`），它们由硬件轮询调度、各自带一套寄存器（见 u10-l2、u5-l1）。而内核在此基础上又造了一层「软件线程」：很多个 `struct thread` 复用同一段硬件时间，由内核的 `reschedule` 决定此刻哪个软件线程跑在哪个硬件线程上。每个硬件线程当前正跑哪个软件线程，记录在 `cur_thread[hwthread]` 里。硬件线程号由控制寄存器 `CR_CURRENT_HW_THREAD`（编号 0）给出。

**(2) 进程 vs 线程。** 进程（`struct process`）持有独立的地址空间（`vm_address_space`）和一组线程；线程（`struct thread`）属于某个进程，拥有自己的内核栈与（可选的）用户栈，共享进程的地址空间。这与 Linux 的 task_struct/mm_struct 关系类似。

**(3) 调度的本质是「换栈 + 换地址翻译」。** 切换软件线程要做两件事：把当前栈指针存回旧线程的 `current_stack`，从新线程的 `current_stack` 载入新栈指针；并切换页目录与 ASID（`switch_to_translation_map`）。寄存器现场则约定好压在各自内核栈顶的一个固定大小的「上下文帧」里。

**(4) 同步原语分两类。** 自旋锁是**忙等**锁，适合极短的临界区，持有期间不让本核被时钟抢占（靠关中断）。读写锁是**阻塞**锁，抢不到就让出 CPU（`reschedule` 切走），适合读多写少、临界区较长的场景。二者底层都依赖 u10-l1 讲过的 LL/SC 原子操作。

> 名词速查：CAS（compare-and-swap，比较并交换）、LL/SC（load-linked / store-conditional，链接加载/条件存储）、callee-saved / caller-saved（被调用者保存 / 调用者保存寄存器）、临界区（critical section）、活锁（livelock）、抢占（preemption）。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [software/kernel/thread.h](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/thread.h) | 定义 `struct thread` / `struct process`、线程状态枚举、线程相关 API 声明、`current_hw_thread()` 内联函数。 |
| [software/kernel/thread.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/thread.c) | 内核调度核心：就绪/死亡队列、`reschedule`、`spawn_thread_internal`、`timer_tick`、`thread_exit`、`grim_reaper`。 |
| [software/kernel/context_switch.S](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/context_switch.S) | 上下文切换的汇编实现：保存向量与被调用者保存标量寄存器、换栈、恢复。 |
| [software/kernel/spinlock.h](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/spinlock.h) | 自旋锁：`acquire_spinlock` / `release_spinlock` 及关中断变体 `_int`。 |
| [software/kernel/rwlock.h](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/rwlock.h) / [rwlock.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/rwlock.c) | 读写锁：`struct rwlock`、`rwlock_lock_read/unlock_read/lock_write/unlock_write` 及公平性规则。 |
| [software/kernel/trap_entry.S](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/trap_entry.S) | `disable_interrupts` / `restore_interrupts` 的实现，以及陷阱入口的栈切换。 |
| [software/kernel/vm_translation_map.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/vm_translation_map.c) | `switch_to_translation_map`：切换页目录基址与 ASID。 |
| [software/kernel/asm.h](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/asm.h) | 控制寄存器编号、标志位、`CONTEXT_FRAME_SIZE` 等共享常量。 |

---

## 4. 核心概念与源码讲解

### 4.1 线程结构与内核调度

#### 4.1.1 概念说明

内核要管理「很多个想跑的程序」，但物理上只有少数硬件线程。于是引入软件线程 `struct thread` 作为调度实体：每个 `thread` 有一份自己的内核栈，状态的取值反映它此刻在调度器眼中的位置。调度器维护两条全局链表——就绪队列 `ready_q` 与死亡队列 `dead_q`，并用一把自旋锁 `thread_q_lock` 保护它们。

理解本模块的关键，是分清「线程状态」与「线程位于哪条队列」的对应关系：

| 状态 | 含义 | 位于哪条队列 |
| --- | --- | --- |
| `THREAD_READY` | 可运行，等待被选中 | `ready_q` |
| `THREAD_RUNNING` | 正在某个硬件线程上执行 | 不在任何队列（在 `cur_thread[hw]`） |
| `THREAD_WAITING` | 阻塞中（如等读写锁） | 不在就绪队列，挂在某锁的等待链表 |
| `THREAD_DEAD` | 已退出，待清理 | `dead_q` |

#### 4.1.2 核心流程

调度发生在 `reschedule()` 中，逻辑可概括为：

```text
reschedule(hwthread):
    assert 该硬件线程未被禁止抢占
    关中断并 acquire(thread_q_lock)
    old = cur_thread[hwthread]
    若 old.state == RUNNING:
        old.state = READY
        把 old 追加到 ready_q 尾部        # 让出 CPU，回到就绪队列
    next = 从 ready_q 头部取出
    next.state = RUNNING
    若 old != next:
        cur_thread[hwthread] = next
        trap_kernel_stack[hwthread] = next 的内核栈顶   # 给陷阱入口用
        switch_to_translation_map(next 的地址空间)
        context_switch(&old.current_stack, next.current_stack)
    release(thread_q_lock) 并恢复中断
```

抢占由时钟中断驱动：定时器中断（中断号 1）触发 `timer_tick`，后者在「本核未被禁止抢占」时调用 `reschedule`。于是只要时钟到了、且有其他就绪线程，当前线程就会被换下。`disable_preempt_count[]` 是每硬件线程一个的计数器，非零时禁止本核抢占——它和关中断一起，构成了临界区里「不被打断」的双重保险。

#### 4.1.3 源码精读

先看数据结构。`struct thread` 把调度所需的全部信息打包：线程号、内核栈顶 `kernel_stack_ptr`、**当前栈指针** `current_stack`（上下文切换的核心字段）、内核/用户栈对应的内存区、所属进程、入口函数、状态与名字。

[thread.h:38-59 — struct thread 定义](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/thread.h#L38-L59)：注意 `current_stack` 字段，上下文切换时会把它在两个线程间倒换。`state` 是一个匿名枚举，取值即上表四种。

[thread.h:28-36 — struct process 定义](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/thread.h#L28-L36)：进程持有引用计数 `ref_count`（归零才销毁）、自旋锁 `lock`、线程链表 `thread_list` 与地址空间 `space`。

[thread.h:91-94 — current_hw_thread()](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/thread.h#L91-L94)：直接读控制寄存器 0（`CR_CURRENT_HW_THREAD`）拿到当前硬件线程号，所有「每硬件线程一份」的状态都用它做下标。

再看调度器全局状态与核心函数：

[thread.c:37-49 — 调度器全局状态](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/thread.c#L37-L49)：`cur_thread[MAX_HW_THREADS]` 是「硬件线程 → 当前软件线程」的映射；`disable_preempt_count[MAX_HW_THREADS]` 每核一个禁止抢占计数；`ready_q`/`dead_q` 是两条链表；`trap_kernel_stack[MAX_HW_THREADS]` 存每核当前线程的内核栈顶，供陷阱入口切换栈用（见 u12-l1）。

[thread.c:277-311 — reschedule 调度核心](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/thread.c#L277-L311)：这就是 4.1.2 伪代码的真实实现。几个要点：
- 第 284 行 `assert(!disable_preempt_count[hwthread])`——禁止抢占时绝不能调度，否则会破坏临界区。
- 用 `acquire_spinlock_int` 一次完成「关中断 + 上锁」（4.3 会讲为何必须关中断）。
- 第 298 行 `list_remove_head` 从就绪队头取下一个线程（FIFO，先来先服务）。
- 只有 `old_thread != next_thread` 才真正切换；切换时更新 `trap_kernel_stack` 与地址翻译，最后调用 `context_switch`。注意 `context_switch` 返回时，**对 old 线程而言**是「将来某次被切回来」，对 next 线程而言是「现在开始跑」——同一份代码两种语义，见 4.2。

[thread.c:147-153 — timer_tick 时钟抢占](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/thread.c#L147-L153)：定时器中断处理函数：重设定时器、确认中断（`ack_interrupt(1)`），并在允许抢占时调用 `reschedule`。这就是抢占式调度的触发点。

[thread.c:94-145 — spawn_thread_internal 创建线程](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/thread.c#L94-L145)：分配 `struct thread`（经 slab，见 u12-l2）、用 `create_area` 申请内核栈（`KERNEL_STACK_SIZE`）、把 `current_stack` 指到「栈顶 − `CONTEXT_FRAME_SIZE`」处，再在第 116 行把入口函数地址写到栈帧的 `0x818` 偏移——这正是 `context_switch` 恢复 `ra`（返回地址）的位置。这条语句是「新线程如何第一次跑起来」的关键，4.2 会细讲。随后加进进程线程链表与就绪队列。

[thread.c:368-387 — thread_exit 退出](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/thread.c#L368-L387)：线程退出时把自己挂到 `dead_q`、状态置 `THREAD_DEAD`，再 `reschedule` 切走。注释「Never will return」与随后的 `panic` 说明：死线程绝不该被再次调度。

[thread.c:204-230 — grim_reaper 收尸线程](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/thread.c#L204-L230)：为什么退出线程不直接释放自己？因为释放自己的内核栈会导致当前正跑在这套栈上的代码崩溃（[thread.c:168-170](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/thread.c#L168-L170) 的注释说得很直白）。所以由专门的 `grim_reaper` 内核线程从 `dead_q` 取出死线程、释放其栈与结构体。这是「把回收推迟到另一个执行上下文」的经典手法。

[thread.c:389-401 — make_thread_ready 唤醒](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/thread.c#L389-L401)：把一个 `THREAD_WAITING` 的线程置为 `THREAD_READY` 并放回就绪队列。读写锁「唤醒等待者」时就靠它。

#### 4.1.4 代码实践（源码阅读型）

**目标：** 跟踪一个软件线程从「创建 → 就绪 → 运行 → 退出 → 被回收」的完整状态流转。

**步骤：**

1. 打开 [thread.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/thread.c)，找到 `spawn_thread_internal`（L94）、`reschedule`（L277）、`thread_exit`（L368）、`grim_reaper`（L204）、`make_thread_ready`（L389）。
2. 对每个函数，在纸上记录它把 `thread->state` 改成了什么、把线程挪到了哪条队列（`ready_q` / `dead_q` / 某锁的等待链表 / 都不在）。
3. 画出状态转移图：`READY ⇄ RUNNING`（由 reschedule 驱动）、`RUNNING → WAITING`（由 rwlock 的 `wait` 驱动，4.4 讲）、`WAITING → READY`（由 `make_thread_ready` 驱动）、`* → DEAD → 被 grim_reaper 回收`。

**需要观察的现象：** `THREAD_RUNNING` 的线程**不在**任何链表里（它只活在 `cur_thread[hw]`），所以 `reschedule` 必须显式把它「放回」`ready_q`；而 `THREAD_WAITING` 的线程既不在就绪队列、也不在运行，只有 `make_thread_ready` 能救它——若忘了唤醒就会永久泄漏。

**预期结果：** 你应当得到一张「四状态 + 五个触发函数」的状态机图，并能解释为何 `reschedule` 在切换前要先 `assert(old_thread->state != THREAD_READY)`（L290）——因为一个还在就绪队列里的线程不该被当成「正在运行」的线程再次入队。

#### 4.1.5 小练习与答案

**练习 1：** 为什么 `cur_thread`、`disable_preempt_count`、`trap_kernel_stack` 都是按硬件线程号（`MAX_HW_THREADS=32`）而非软件线程号索引的数组？

**参考答案：** 因为「当前哪个软件线程跑在哪个硬件线程上」是动态变化的，而硬件线程数量固定。调度器在任何时刻都只需要知道「**这个硬件线程**现在跑的是哪个软件线程」「**这个硬件线程**是否禁止抢占」「**这个硬件线程**的当前内核栈顶」。用硬件线程号做下标，就能用 `current_hw_thread()`（一条 `getcr`）瞬间定位，无需加锁查表。

**练习 2：** `reschedule` 末尾对 `old_thread != next_thread` 才调用 `context_switch`。若就绪队列里只有当前线程自己（取出来的 next 还是 old），会发生什么？这样设计有什么好处？

**参考答案：** 不会发生真正的切换：跳过 `context_switch`，直接释放锁返回，当前线程继续运行。好处是避免一次昂贵的「保存全部向量寄存器 + 换栈 + 恢复」的空转——当系统中只有空闲线程可调度时，省掉这次无谓的上下文切换开销。

---

### 4.2 上下文切换

#### 4.2.1 概念说明

「上下文切换」就是让硬件线程从执行线程 A 改为执行线程 B。由于 Nyuzi 的软件线程共享硬件线程，切换时必须把 A 的寄存器现场存到 A 的内核栈、把 B 之前存在它内核栈的现场恢复出来，再把栈指针从 A 的栈换到 B 的栈。

这里有两个反直觉但关键的点：

1. **必须保存全部 32 个向量寄存器。** 陷阱入口 `trap_entry` 为了省时间**不保存向量寄存器**（[trap_entry.S:22-26](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/trap_entry.S#L22-L26) 的注释明说），内核代码也不使用向量寄存器。可是用户线程的向量寄存器内容必须跨调度保留。因此 `context_switch` 成了全系统里**唯一**负责保存/恢复向量寄存器的地方。
2. **只保存被调用者保存（callee-saved）的标量寄存器。** Nyuzi 的调用约定里 `s0–s23` 是调用者保存（由调用方自己 spilled），`s24–s27` 以及 `gp/fp/ra` 是被调用者保存。`context_switch` 作为一个函数，按 ABI 只需保证返回时被调用者保存寄存器不变即可——所以它只存后者。

#### 4.2.2 核心流程

`context_switch` 的执行可分四相（注意它对「旧线程」和「新线程」语义不同）：

```text
context_switch(old_sp_ptr=&A.current_stack, new_sp=B.current_stack):  # 参数经 s0, s1 传入
  1. sub_i sp, sp, CONTEXT_FRAME_SIZE            # 在 A 的栈上开辟上下文帧
  2. 把 v0..v31 存到 sp+0x00 .. sp+0x7c0          # 32 个向量，每个 0x40 字节
  3. 把 s24,s25,s26,s27,gp,fp,ra 存到 sp+0x800..0x818
  4. store_32 sp, (s0)    # 把当前 sp 写回 *old_sp_ptr，即 A.current_stack = sp
     move   sp, s1        # sp = B.current_stack，换栈！从此操作的是 B 的帧
  5. 从「新 sp」依次 load 回 v0..v31 与 s24..ra
  6. add_i sp, sp, CONTEXT_FRAME_SIZE            # 回收帧
     ret                  # 跳到刚恢复的 ra
```

对旧线程 A：第 1–4 步把它的现场封存进自己的栈、`current_stack` 被更新为封存后的栈顶；执行流随后「消失」在 B 的世界里（A 此刻暂停）。

对新线程 B：第 4 步之后所有的 load 都从 B 的栈读。当 B 是「之前被切走的旧线程」时，读出的就是它当年存的现场，`ret` 回到它当年调用 `context_switch` 之后的那条指令——对 B 而言 `context_switch` 像是「刚才才返回」。当 B 是「从未运行过的全新线程」时，它的栈帧是 `spawn_thread_internal` 预先伪造的，`ra` 被设成入口函数，于是 `ret` 直接跳到线程入口。

#### 4.2.3 源码精读

[context_switch.S:27-80 — 保存现场与换栈](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/context_switch.S#L27-L80)：
- 第 29 行 `sub_i sp, sp, CONTEXT_FRAME_SIZE` 开辟帧，`CONTEXT_FRAME_SIZE` 定义在 [asm.h:63](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/asm.h#L63) 为 `0x840`（2112 字节）。
- 第 34–65 行存 32 个向量寄存器，每个 64 字节（`0x40`），共占 `0x800` 字节（0x00–0x7c0）。这正是 u2-l1 里 `vector_t` 512 位 = 64 字节的体现。
- 第 70–76 行存 6 个被调用者保存标量：`s24..s27`、`gp`、`fp`、`ra`，偏移 `0x800..0x818`。注释解释了为何不存 `s0–s23`。
- 第 79–80 行是换栈的灵魂：`store_32 sp, (s0)` 把当前 `sp` 写到 `s0` 所指地址——而 `s0` 正是第一个参数 `&old_thread->current_stack`；`move sp, s1` 把 `sp` 设为第二个参数 `new_thread->current_stack`。**两条指令之后，我们已在另一条栈上。**

[context_switch.S:82-126 — 恢复现场并返回](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/context_switch.S#L82-L126)：从新栈把向量与标量寄存器依次 load 回来，第 124 行回收帧，第 126 行 `ret`。注意 Nyuzi 的 `ret` 是「跳到 `ra`（s31）」，而 `ra` 刚刚从新栈的 `0x818` 处恢复。

**新线程的「伪造栈帧」技巧。** 回看 [thread.c:111-116](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/thread.c#L111-L116)：新建线程时 `current_stack` 被设为「栈顶 − `CONTEXT_FRAME_SIZE`」，正好等于 `context_switch` 第 29 行 `sub_i` 之后的 `sp`；再把入口函数地址写到偏移 `0x818`（即 `ra` 槽位）。于是当这个新线程首次被 `context_switch` 选中、走完恢复相后，`ret` 会跳到那个入口函数。入口函数（如 [thread.c:255-267 的 kernel_thread_kernel_start](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/thread.c#L255-L267)）的第一件事是 `current_thread()`（靠控制寄存器，不依赖未初始化的 `s0–s23`）、释放调度锁、恢复中断标志，然后才调用真正的 `start_func`。这套设计让「创建新线程」与「切换到老线程」复用同一段 `context_switch` 代码，无需特判。

上下文帧的布局可图示如下（栈向低地址增长，`sp` 指向帧底）：

```text
sp + 0x000: v0                       ┐
sp + 0x040: v1                       │
   ...                               │ 32 个向量寄存器，共 0x800 字节
sp + 0x7c0: v31                      ┘
sp + 0x800: s24  (callee-saved)
sp + 0x804: s25
sp + 0x808: s26
sp + 0x80c: s27
sp + 0x810: gp
sp + 0x814: fp
sp + 0x818: ra   ← 新线程入口被写在这里
sp + 0x840: （帧顶，即 sub_i 之前的 sp）
```

这与 `CONTEXT_FRAME_SIZE = 0x840`、`ra` 偏移 `0x818` 完全吻合（`0x818 / 4 = 0x206`，正是 [thread.c:116](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/thread.c#L116) 写入的数组下标）。

地址翻译的切换发生在 `context_switch` 之前：[thread.c:306](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/thread.c#L306) 调用 `switch_to_translation_map`，其实现 [vm_translation_map.c:292-299](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/vm_translation_map.c#L292-L299) 只是写两个控制寄存器——`CR_PAGE_DIR_BASE`（页目录基址）与 `CR_CURRENT_ASID`（地址空间标识，见 u7-1）。

#### 4.2.4 代码实践（源码阅读型）

**目标：** 验证「新线程首次运行」确实完全复用了 `context_switch` 的恢复路径。

**步骤：**

1. 读 [thread.c:106-116](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/thread.c#L106-L116)，确认 `current_stack = kernel_stack_ptr - CONTEXT_FRAME_SIZE`，且 `ra` 槽（`0x818`）被写成 `init_func`（对内核线程是 `kernel_thread_kernel_start`，对用户线程是 `user_thread_kernel_start`）。
2. 读 [context_switch.S:116-126](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/context_switch.S#L116-L126) 的恢复相：`ra` 从 `0x818(sp)` 读回，`ret` 即跳到 `init_func`。
3. 读 [thread.c:255-267](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/thread.c#L255-L267)：入口函数为何要 `release_spinlock(&thread_q_lock)`？因为它是「从 `reschedule` 内部的 `context_switch` 返回」的，而 `reschedule` 进入时已持有该锁（[thread.c:288](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/thread.c#L288)）；新线程绕过了 `reschedule` 的正常返回路径，必须自己补上这次释放。

**需要观察的现象：** 入口函数 `restore_interrupts(FLAG_INTERRUPT_EN | FLAG_MMU_EN | FLAG_SUPERVISOR_EN)`（[thread.c:262](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/thread.c#L262)）——注意它恢复的中断是「打开」状态，因为调度期间中断被关掉了。

**预期结果：** 你能向别人讲清「为什么新建线程不用单独的启动汇编 stub，而是靠往栈帧里塞一个假的 `ra`」。如果你想本地验证，可在 `context_switch` 的恢复相末尾、`ret` 前加一条把 `ra` 打印到串口的指令（仅调试用，勿提交），观察新建线程首次调度时 `ra` 是否等于 `kernel_thread_kernel_start`。**待本地验证。**

#### 4.2.5 小练习与答案

**练习 1：** `context_switch` 为什么不保存 `s0–s23`？如果省略它们，正确性会出问题吗？

**参考答案：** 不会。按 Nyuzi 调用约定，`s0–s23` 是调用者保存寄存器，调用方（这里是 `reschedule`）在调用 `context_switch` 之前已经把自己需要跨调用保留的那些 `s0–s23` 溢出到自己的栈帧。`context_switch` 只需保证被调用者保存寄存器（`s24–s27, gp, fp, ra`）在返回时不变，而这正是它显式保存恢复的那些。因此当 old 线程将来被切回来时，它的 `s0–s23` 由 `reschedule` 自己的栈帧负责恢复，上下文切换依然正确。

**练习 2：** 假设把 `CONTEXT_FRAME_SIZE` 调小，只够放标量寄存器、不放向量寄存器，会出什么问题？

**参考答案：** 用户线程的向量寄存器内容会丢失。因为陷阱入口不保存向量、内核也不用向量，全系统只有 `context_switch` 保存它们。一旦这里不存，线程 A 跑到一半的 `v0–v31`（比如 SIMD 渲染中间结果）会在被切走时被线程 B 覆盖，切回来后拿到错误的值，导致用户程序计算结果错乱。这也解释了为什么帧要大到 `0x840`（其中 `0x800` 全是向量）。

---

### 4.3 同步原语：自旋锁

#### 4.3.1 概念说明

临界区是一段「同一时刻只能有一个执行上下文进入」的代码，通常用于保护共享数据结构（如调度器的就绪队列）。自旋锁用「忙等」实现互斥：抢不到锁的线程在原地反复尝试，直到成功。

Nyuzi 自旋锁的底层原子原语是 CAS（`__sync_bool_compare_and_swap`），它会被编译成 u10-l1 讲过的 `load_sync`/`store_sync`（LL/SC）指令序列。CAS 语义是「当 `*sp == 0` 时把它写成 1 并返回成功，否则返回失败」——这正是「尝试拿锁」。

但只靠 CAS 还不够。Nyuzi 是**抢占式多线程**内核：如果线程 A 拿了锁后被时钟中断抢占、切到也想要这把锁的线程 B，B 会永远自旋（A 永远没机会释放）。所以自旋锁必须配合「关中断」使用——这正是 `_int` 后缀变体存在的理由。

#### 4.3.2 核心流程

```text
acquire_spinlock(sp):
    do:
        while (*sp) ;        # 本地读：锁被持有时纯本地自旋，少打搅 L2
    while !CAS(sp, 0, 1)     # 一旦看起来空闲，用原子 CAS 真正抢占

acquire_spinlock_int(sp):
    old_flags = disable_interrupts()   # 先关中断，防本核被抢占
    acquire_spinlock(sp)
    return old_flags                    # 调用方释放时用它恢复中断

release_spinlock(sp):
    *sp = 0                 # 直接写 0（只有持锁者会写，无需原子）
    __sync_synchronize()    # 内存屏障：保证上面的写对其他核立即可见
```

两个优化点：① 锁被持有时先用普通读 `while(*sp)` 自旋，而不是反复发 CAS——因为 CAS 会触发 LL/SC，在共享 L2 上产生一致性流量；普通读则可能命中 L1 的 snoop 副本，省带宽（这正是源码注释的意思）。② 释放时 `*sp=0` 之后跟一个 `__sync_synchronize()` 全内存屏障，确保临界区内的写在锁释放前对其他核全部可见。

#### 4.3.3 源码精读

[spinlock.h:21-35 — acquire_spinlock](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/spinlock.h#L21-L35)：`spinlock_t` 就是 `volatile int`（第 21 行），`volatile` 保证编译器每次都真去读内存而不是缓存到寄存器。`do/while` 嵌套：内层 `while(*sp)` 纯本地自旋，外层 `__sync_bool_compare_and_swap(sp, 0, 1)` 是原子抢占。

[spinlock.h:38-43 — acquire_spinlock_int](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/spinlock.h#L38-L43)：先 `disable_interrupts()` 再 `acquire_spinlock`，返回旧标志位。这是内核里保护调度队列等结构时最常用的形式（如 `reschedule` 用的就是它）。

[spinlock.h:45-49 — release_spinlock](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/spinlock.h#L45-L49)：写 0 + 内存屏障。

[spinlock.h:53-57 — release_spinlock_int](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/spinlock.h#L53-L57)：先释放锁，再用 `old_flags` 恢复中断。注意「恢复」而非「打开」：`restore_interrupts` 只在原来中断是开的时候才重新打开，避免嵌套临界区里误开中断。

关中断的实现极简：[trap_entry.S:242-256](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/trap_entry.S#L242-L256)。`disable_interrupts` 读 `CR_FLAGS`，用 `and` 清掉 `FLAG_INTERRUPT_EN` 位（保留 MMU/supervisor 位）写回——三行汇编、原子地关掉本核中断。`restore_interrupts` 直接把传入的标志值写回 `CR_FLAGS`。这呼应 u2-l4/u7-2：中断使能位是 `CR_FLAGS` 的第 0 位。

#### 4.3.4 代码实践（源码阅读型 + 思辨）

**目标：** 论证「为什么自旋锁临界区里必须关中断」。

**步骤：**

1. 假设我们改用 `acquire_spinlock`（不关中断版）来保护 `ready_q`，在 [thread.c:288](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/thread.c#L288) 处拿锁。
2. 推演：线程 A 拿到 `thread_q_lock` 后、还没释放时，时钟中断触发 `timer_tick`（[thread.c:147](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/thread.c#L147)）→ `reschedule` → 切到线程 B。
3. 若 B 也调用 `reschedule`（或任何上 `thread_q_lock` 的路径），它会在 `acquire_spinlock` 里死等 A 释放——但 A 此时不在运行（被切走了），永不会释放 → **死锁**。

**需要观察的现象：** 用 `acquire_spinlock_int` 时，第 1 步关掉了本核中断，第 2 步的时钟中断被屏蔽（pending 但不触发 `timer_tick`），于是 A 不会被切走，能安全释放锁后再由 `release_spinlock_int` 恢复中断。

**预期结果：** 你能得出结论——在**单核抢占**视角下，关中断是自旋锁正确性的必要条件；在**多核**视角下，关中断还不够（别的核仍会争抢），还需要 CAS/LL-SC 提供硬件原子性。Nyuzi 二者兼备：`_int` 变体关中断（防本核自抢），CAS 提供跨核原子（见 u10-l1 的 LL/SC）。

> 进一步思考（不必运行）：`timer_tick` 里有 `if (disable_preempt_count[...] == 0) reschedule()`（[thread.c:151-152](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/thread.c#L151-L152)）。这说明还有第二道闸门 `disable_preempt_count`：即便中断误开，只要计数非零也不会调度。两套机制互为兜底。

#### 4.3.5 小练习与答案

**练习 1：** 释放锁时为什么可以直接 `*sp = 0`，而不用 CAS？

**参考答案：** 因为只有当前持有锁的那个执行上下文才会执行释放，不存在多个上下文同时写 `*sp = 0` 的竞争；而想拿锁的上下文用的是 CAS（读到非 0 就不写）。所以「写 0」这一侧是单写者，无需原子指令。真正需要原子性的是「拿锁」一侧的读-改-写，那里用了 CAS。

**练习 2：** `acquire_spinlock` 内层用 `while(*sp)` 本地自旋，相比「每次都 CAS」有什么好处？在多核下会不会有问题？

**参考答案：** 好处是减少共享总线流量：CAS 每次都发 LL/SC，会在 L2 一致性网络上产生流量并可能互相使对方的 SC 失败；而本地读 `*sp` 往往命中 L1（经 snoop 维护的副本），锁被持有时几乎不打搅 L2。多核下的潜在问题是「公平性/活锁」——多个核同时发现 `*sp==0` 一起 CAS，只有一个赢，其余继续转，但这对短临界区通常可接受。Nyuzi 的注释明确接受这一取舍以换吞吐。

---

### 4.4 同步原语：读写锁

#### 4.4.1 概念说明

自旋锁是「互斥」的：哪怕十个线程都只是**读**共享数据，也得排队一个个来。读多写少的场景下这很浪费。读写锁（rwlock）放宽了语义：允许多个**读者**同时进入临界区，但**写者**独占（写者进入时既不能有其他写者、也不能有读者）。

Nyuzi 的 rwlock 与 spinlock 还有一处根本差异：**它是阻塞锁**。抢不到锁的线程不会忙等，而是把自己的状态置为 `THREAD_WAITING` 并调用 `reschedule` 让出 CPU（见 4.1 的 `make_thread_ready` 负责唤醒）。因此 rwlock 适合临界区较长、不值得空转 CPU 的场景；spinlock 适合极短临界区。

读写锁的经典难题是「公平性」：若读者源源不断，写者可能永远等不到机会（写者饥饿）；反之亦然。Nyuzi 用三条规则解决，写在 [rwlock.c:22-29](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/rwlock.c#L22-L29) 的注释里。

#### 4.4.2 核心流程

读写锁内部仍用一把 spinlock 保护自己的计数字段与等待链表。三个核心量：`write_locked`（是否被写者独占）、`active_read_count`（当前在临界区里的读者数）、两条等待链表 `reader_wait_list` / `writer_wait_list`。

三条公平性规则：

1. **若有写者在等，则不让新读者进入。**（防写者饥饿）
2. **最后一个读者退出时，唤醒一个等待的写者。**
3. **写者退出时，若有读者在等，全部唤醒。**（被唤醒的这批读者可进，但之后的新读者受规则 1 阻挡，直到这批读完）

阻塞与唤醒的通用原语是 `wait()`：把当前线程标为 `THREAD_WAITING`、释放内部 spinlock、`reschedule()` 切走；将来被 `make_thread_ready` 唤醒后重新拿回 spinlock 继续。

```text
rwlock_lock_read:
    上内部 spinlock（关中断）
    若 有写者在等 或 write_locked:
        把自己挂到 reader_wait_list; wait()        # 阻塞
    否则 active_read_count++
    解锁（恢复中断）

rwlock_unlock_read:
    上内部 spinlock（关中断）
    active_read_count--
    若 减到 0 且 有写者在等:           # 规则 2
        write_locked = 1; 唤醒一个写者
    解锁

rwlock_lock_write:
    上内部 spinlock（关中断）
    若 有读者 或 write_locked:
        挂到 writer_wait_list; wait()
    write_locked = 1
    解锁

rwlock_unlock_write:
    上内部 spinlock（关中断）
    若 有读者在等:                     # 规则 3
        全部唤醒并计入 active_read_count; write_locked = 0
    否则 若 有写者在等:                # 交给下一个写者，保持 write_locked
        唤醒一个写者
    否则 write_locked = 0
    解锁
```

#### 4.4.3 源码精读

[rwlock.h:26-33 — struct rwlock](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/rwlock.h#L26-L33)：内含一把 `spinlock`、`write_locked`、`active_read_count` 与两条等待链表。注意它把 spinlock 作为成员嵌入，rwlock 的所有操作都先上这把 spinlock（且用 `_int` 变体），保证对计数字段的修改是原子的、且不被本核抢占。

[rwlock.c:39-45 — wait 阻塞原语](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/rwlock.c#L39-L45)：这就是「阻塞锁」的核心——`current_thread()->state = THREAD_WAITING`、释放内部 spinlock（让别的线程能进 rwlock 操作）、`reschedule()` 切走（CPU 让给别人）、回来后再重新 `acquire_spinlock`。把这段和 4.1 的状态机对照：`WAITING` 状态的线程只有别人对它 `make_thread_ready` 才能重回 `READY`。

[rwlock.c:47-63 — rwlock_lock_read](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/rwlock.c#L47-L63)：第 52 行的条件 `!list_is_empty(&m->writer_wait_list) || m->write_locked` 正是规则 1——只要有写者在等或正在写，新读者就得等。否则 `active_read_count++` 直接进。

[rwlock.c:65-82 — rwlock_unlock_read](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/rwlock.c#L65-L82)：第 75 行 `--active_read_count == 0 && !list_is_empty(writer_wait_list)` 是规则 2——最后一个读者负责唤醒一个写者，并立即把 `write_locked` 置 1（把锁「递」给那个写者）。

[rwlock.c:84-100 — rwlock_lock_write](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/rwlock.c#L84-L100)：写者要等到「没有读者且没人写」才能进，否则挂到 `writer_wait_list` 阻塞。

[rwlock.c:102-132 — rwlock_unlock_write](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/rwlock.c#L102-L132)：写者退出时的三选一，对应规则 3 与「写者接力」：
- 有读者在等（L111）：`while` 循环把它们**全部**唤醒，每个唤醒时 `active_read_count++`（L117），最后 `write_locked = 0`（L120）。这批读者随后能并发读。
- 否则有写者在等（L122）：只唤醒一个写者，**保持** `write_locked = 1`（L124-127 注释明说），相当于把独占权直接交给下一个写者。
- 否则（L128）：无人在等，`write_locked = 0` 完全开放。

[rwlock.c:134-184 — TEST_RWLOCK 自测代码](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/rwlock.c#L134-L184)：用 `spawn_kernel_thread` 起 10 个读者 + 2 个写者，验证读者并发、写者独占、且互不饥饿。注释里给出了期望输出样例（一串读者 id 夹在两段写者输出之间）。

#### 4.4.4 代码实践（源码阅读型）

**目标：** 用三条公平性规则解释一段并发序列。

**步骤：**

1. 假设初始 `active_read_count=2`（读者 R1、R2 在读），无写者。
2. 写者 W1 调用 `rwlock_lock_write`：因 `active_read_count > 0`，W1 挂入 `writer_wait_list` 并 `wait`（[rwlock.c:91-95](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/rwlock.c#L91-L95)）。
3. 此时新读者 R3 想进：因 `writer_wait_list` 非空（规则 1），R3 也挂入 `reader_wait_list` 等待（[rwlock.c:52-57](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/rwlock.c#L52-L57)）。
4. R1、R2 陆续退出。最后一个退出者（假设 R2）执行 [rwlock.c:75-79](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/rwlock.c#L75-L79)：`active_read_count` 减到 0 且有 W1 在等 → `write_locked=1`，唤醒 W1。W1 独占写入。
5. W1 写完调用 `rwlock_unlock_write`：`reader_wait_list` 里有 R3（规则 3，[rwlock.c:111-121](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/rwlock.c#L111-L121)）→ 唤醒 R3、`active_read_count=1`、`write_locked=0`。R3 开始读。

**需要观察的现象：** W1 没有被「源源不断的读者」饿死（因为规则 1 一旦 W1 在等就挡住新读者）；R3 也没有被「连续写者」饿死（因为规则 3 写者退出会放行等待的读者）。

**预期结果：** 你能指出步骤 4 里「最后一个读者」的判定（`--count == 0`）为何必须放在持锁临界区内——否则两个读者可能同时读到非零计数而都不唤醒写者。若想本地验证，可编译 `rwlock.c` 时定义 `TEST_RWLOCK` 宏并调用 `test_rwlock()`（[rwlock.c:174](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/rwlock.c#L174)），观察输出是否符合注释里的样例。**待本地验证。**

#### 4.4.5 小练习与答案

**练习 1：** rwlock 内部那把 spinlock 为何全程用 `acquire_spinlock_int`（关中断版），而不是普通 `acquire_spinlock`？

**参考答案：** 与 4.3 同理：rwlock 操作会修改 `active_read_count`、操作等待链表，若中途被本核时钟中断抢占并切到也想操作同一 rwlock 的线程，就会自旋死锁或破坏计数。关中断确保 rwlock 的「读改计数 + 唤醒」这一小段临界区在本核上原子完成。跨核的并发安全则由内部 spinlock 的 CAS 保证。

**练习 2：** `wait()` 里先 `release_spinlock` 再 `reschedule()`，最后又 `acquire_spinlock`。为什么唤醒后要重新拿锁、而不是直接返回？

**参考答案：** 因为被唤醒的线程是在「它当初拿到内部 spinlock 之后」调用 `wait` 的——从它视角看，自己仍处在 rwlock 操作的临界区中段。`wait` 释放 spinlock 只是为了让 `reschedule` 能切走、让别的线程推进；当它被 `make_thread_ready` 唤醒并重新被调度回来时，必须重新拿回那把 spinlock 才能安全地读改 rwlock 状态并最终解锁返回，否则就破坏了「rwlock 内部状态只在持 spinlock 时访问」的不变式。

---

## 5. 综合实践

**任务：把四个最小模块串起来，追踪一次「读写锁阻塞唤醒」里完整的线程流转。**

场景：硬件线程 H0 上读者线程 R 持有读锁（`active_read_count=1`），写者线程 W（也在 H0 上就绪）尝试拿写锁。

请按顺序回答并定位源码：

1. **W 如何阻塞？** W 在 [rwlock.c:91-95](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/rwlock.c#L91-L95) 发现 `active_read_count > 0`，把自己挂入 `writer_wait_list`，调用 `wait`（[rwlock.c:39-45](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/rwlock.c#L39-L45)）→ 状态置 `THREAD_WAITING` → `reschedule`（[thread.c:277](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/thread.c#L277)）。
2. **reschedule 如何切到另一个线程？** 它把当前线程（W）放回 `ready_q`？——不，W 是 `WAITING` 不是 `RUNNING`，所以 [thread.c:292-296](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/thread.c#L292-L296) 的「放回就绪队列」分支不会执行（`state != THREAD_RUNNING`），W 直接从运行集合里消失；调度器从 `ready_q` 取出 R 继续跑，并 `context_switch`（[thread.c:307](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/thread.c#L307)）。
3. **context_switch 保存了什么？** 参照 4.2：W 的 32 个向量寄存器 + `s24..ra` 被压进 W 的内核栈，`sp` 换到 R 的栈，R 的现场恢复，`ret` 回到 R 当年调用 `context_switch` 之后。
4. **R 读完如何唤醒 W？** R 调用 `rwlock_unlock_read`，[rwlock.c:75-79](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/rwlock.c#L75-L79) 检测到自己是最后一个读者且有 W 在等 → `write_locked=1`，`make_thread_ready(W)`（[thread.c:389](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/thread.c#L389)）把 W 置回 `THREAD_READY` 放进 `ready_q`。
5. **W 如何恢复？** 下次 `reschedule` 选中 W，`context_switch` 恢复 W 的现场，W 从 `wait` 里的 `reschedule` 调用返回，重新 `acquire_spinlock`（[rwlock.c:44](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/rwlock.c#L44)），继续 `rwlock_lock_write` 的后半段——此刻 `write_locked` 已被置 1，W 跳过等待、拿到独占权。

完成本实践后，你应当能画出一张包含「rwlock 等待链表 ↔ ready_q ↔ 内核栈 ↔ context_switch」四者交互的时序图，并指出每一步分别由 4.1/4.2/4.3/4.4 的哪条机制负责。

## 6. 本讲小结

- Nyuzi 在硬件线程之上又叠了一层**软件线程** `struct thread`，四态状态机（READY/RUNNING/WAITING/DEAD）配合 `ready_q`/`dead_q` 两条队列构成调度模型；`reschedule` 是调度核心，由时钟中断 `timer_tick` 驱动抢占。
- **上下文切换** `context_switch.S` 保存全部 32 个向量寄存器（因为陷阱入口不存、内核不用，全系统只此一处）与被调用者保存标量寄存器，靠「存旧 sp、载新 sp」两条指令换栈；新线程靠 `spawn_thread_internal` 预先往栈帧 `0x818` 处塞入口函数地址，从而复用同一段切换代码首次启动。
- **自旋锁**用 CAS（LL/SC）+ 关中断实现：CAS 提供跨核原子，关中断（`_int` 变体）防本核在持锁时被时钟抢占而死锁；本地自旋 + 释放时内存屏障是两个吞吐优化。
- **读写锁**是阻塞锁，靠 `wait`/`make_thread_ready` 让出与恢复 CPU，用三条公平性规则（有写者等则挡新读者、末位读者唤醒一个写者、写者退出放行全部等待读者）避免读者/写者互相饥饿；内部仍用一把 spinlock 保护自身计数。
- 两类锁的取舍：spinlock = 忙等 + 极短临界区；rwlock = 阻塞 + 读多写少/较长临界区。`disable_preempt_count` 与关中断共同构成「临界区不被打断」的双重保险。

## 7. 下一步学习建议

- **横向对比硬件侧调度：** 回头重读 u10-l2 的 `thread_select_stage` 与 `thread_en`/挂起唤醒位图，对比「硬件轮询调度」与本章「软件优先级调度」的层次关系——前者隐藏延迟，后者提供抽象。
- **顺着调用链往下：** 读 [software/kernel/slab.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/slab.c) 看 `MAKE_SLAB`/`slab_alloc` 如何服务于线程结构分配（u12-l2 已铺垫）；读 [vm_address_space.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/vm_address_space.c) 看 `create_area` 如何为内核栈/用户栈分配内存。
- **下一单元 u13（图形渲染管线）：** librender 的 `parallel_execute`（u9-l2）会唤醒硬件线程并行渲染，那里用到的 `__sync_*` 原子内建与本讲的 LL/SC、spinlock 同源；学完本讲再读渲染线程同步会更顺。
- **若想动手：** 在模拟器里跑内核（参考 u12-l1 的启动流程），用 `dump_process_list()`（[thread.c:403](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/thread.c#L403)）打印进程/线程表，观察调度器实际创建了哪些线程、它们的状态如何随时间变化。**待本地验证。**
