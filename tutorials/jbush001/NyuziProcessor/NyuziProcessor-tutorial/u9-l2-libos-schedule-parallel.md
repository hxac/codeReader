# libos 调度与并行执行

## 1. 本讲目标

上一讲（u9-l1）我们看清了一个裸机程序如何从 `_start` 走到 `main`、如何用 `printf` 把字符写到 UART、又如何用 `sbrk` 切出一块堆。但那是一个「单线程」的视角——复位时其实只有硬件线程 0 在跑，其余线程都在沉睡。

本讲要回答三个问题：

1. 软件**怎样唤醒**其余硬件线程让它们一起干活（并行执行）？
2. 多个线程同时抢一份工作时，**怎样不抢重、不漏干、还能安全汇合**（线程同步）？
3. 这套调度代码在**裸机（bare-metal）和有内核（kernel）两种环境**下有什么区别（两种变体）？

学完本讲，你应该能用 `parallel_execute` 写出一个把任务切片后分发给 4 个硬件线程并行计算的程序，并能解释任务分发、领取与汇合每一步背后的源码与原子操作。

## 2. 前置知识

在进入源码前，先建立几个直觉。

**硬件线程 vs. 软件线程。** Nyuzi 每核有 4 个硬件线程（`THREADS_PER_CORE=4`，见 u3-l3）。它们是真正的执行实体，由硬件轮询调度（见 u4-l3 的 `thread_select_stage`）。本讲说的「线程」默认指硬件线程。寄存器文件按线程分体（见 u5-l1），所以线程切换不需要软件保存/恢复寄存器——这点和传统 CPU 的软件线程不同，它让本讲的并发模型更轻量。

**复位只醒一个线程。** 复位后只有线程 0 的 `thread_enable_mask` 位为 1，其余线程被挂起（suspended）。要启用它们，软件必须向控制寄存器 `CR_RESUME_THREAD`（编号 21）写一个掩码。这是理解「主线程唤醒其余线程」的关键。

**任务并行的两种切法。** Nyuzi 程序里常见的并行模式有两种：

- **静态切分**：每个线程按自己的线程号认领固定的一份工作，互不通信。例如 Mandelbrot 按行交错切分（`row += NUM_THREADS`）。
- **共享任务池（job pool）**：把工作切成 N 个小任务放进一个共享计数器，谁空闲谁去抢一个，抢完再抢下一个。`parallel_execute` 就是这种模式。

本讲聚焦第二种，因为它把「分发—领取—汇合」的同步逻辑全部封装好了，是 librender 等库的并行骨架。

**原子操作。** 当多个线程同时读写同一个计数器时，普通的 `load → add → store` 会丢更新（两个线程同时读到旧值，各自加 1 后写回，结果只加了 1）。Nyuzi 提供 `load_sync`/`store_sync`（LL/SC，见 u2-l3）这对原语来解决这个问题。GCC 的 `__sync_bool_compare_and_swap` 和 `__sync_fetch_and_add` 内建函数在 Nyuzi 上会被编译成这对指令（详见 u10-l1）。本讲会看到它们如何被用来安全地分发任务和计数。

## 3. 本讲源码地图

本讲涉及的核心文件如下：

| 文件 | 作用 |
| --- | --- |
| [software/libs/libos/schedule.h](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/schedule.h) | 调度接口的公共头文件，定义任务函数指针类型与三个对外函数声明 |
| [software/libs/libos/bare-metal/schedule.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/schedule.c) | 裸机变体的调度实现：`parallel_execute`、`dispatch_job`、`worker_thread`、`start_all_threads` |
| [software/libs/libos/kernel/schedule.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/kernel/schedule.c) | 内核变体的调度实现，任务池逻辑与裸机相同，仅 `start_all_threads` 不同 |
| [software/libs/libos/bare-metal/crt0.S](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/crt0.S) | 裸机启动代码：所有线程都从 `_start` 进入，非 0 线程跳过初始化直接进 `main` |
| [software/libs/libos/bare-metal/nyuzi.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/nyuzi.c) | `get_current_thread_id` 实现，靠读控制寄存器 0 |
| [software/apps/mandelbrot/mandelbrot.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/apps/mandelbrot/mandelbrot.c) | 静态切分并行示例（对照） |
| [tests/render/fill/main.cpp](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/render/fill/main.cpp) | 任务池并行的典型用法：`main` 里分发 worker、调用渲染 |
| [software/libs/librender/RenderContext.cpp](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/RenderContext.cpp) | `parallel_execute` 的真实调用方，渲染管线的并行入口 |

一个要点：`bare-metal/schedule.c` 与 `kernel/schedule.c` 的任务池逻辑（`dispatch_job`、`parallel_execute`、`worker_thread`）**逐字相同**，只有 `start_all_threads` 不同。理解了裸机版，内核版就懂了一大半。

## 4. 核心概念与源码讲解

### 4.1 并行执行：parallel_execute 与共享任务池

#### 4.1.1 概念说明

`parallel_execute` 解决的问题是：**把一个长度为 N 的工作数组，分发给所有可用线程并行处理，并在全部完成前阻塞调用者**。

它的签名在公共头文件里：

```c
typedef void (*parallel_func_t)(void *context, int index);
void parallel_execute(parallel_func_t func, void *context, int num_elements);
```

[software/libs/libos/schedule.h:20](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/schedule.h#L20) 定义了任务函数指针类型：每个任务带一个共享 `context` 和一个自己的 `index`（0 到 num_elements-1）。使用者只需写「处理第 index 个元素」的函数，剩下的分发、并行、汇合全交给 `parallel_execute`。

它的设计思想是**共享任务池 + 自取（self-scheduling）**：

- 所有任务被抽象成一个从 0 递增的索引区间 `[0, num_elements)`。
- 一个全局计数器 `current_index` 指向「下一个待领取的任务」。
- 任何线程（包括主线程自己）空闲时就调用 `dispatch_job()` 去抢一个：成功就把 `current_index` 加 1 并执行该任务；失败（任务领完了）就返回。
- 这样天然做到**动态负载均衡**：快的线程多干、慢的少干，不会出现「某个线程分到一大块难活、其他线程干完闲着」的失衡。

#### 4.1.2 核心流程

`parallel_execute` 的整体流程可以画成：

```
主线程调用 parallel_execute(func, ctx, N):
 ├─ 设置共享状态: current_func=func, context=ctx, current_index=0, max_index=N
 ├─ while (还有任务) dispatch_job();     // 主线程自己也下场抢活
 └─ while (active_jobs != 0) ;           // 等其余线程把手头的活干完

工作线程一直在 worker_thread() 里循环:
 └─ while (1):
     ├─ while (current_index == max_index) ;   // 没活就空转等
     ├─ active_jobs++
     ├─ dispatch_job();                         // 抢一个任务执行
     └─ active_jobs--
```

注意一个精妙之处：**主线程不是单纯地等待，而是自己也参与抢活**（第一段 `while`）。只有当任务全被领光后，主线程才进入第二段 `while` 等所有在途任务收尾。这样主线程在任务充足时不会浪费算力。

`dispatch_job()` 的领取逻辑用「读—比较—交换（CAS）」自旋：

```
do:
    this_index = current_index            // 1. 读取当前可用索引
    if this_index == max_index: return 0  // 2. 领完了，返回「没活」
while (!CAS(current_index, this_index, this_index+1))  // 3. 原子地占住它
func(context, this_index)                 // 4. 执行第 this_index 个任务
return 1
```

步骤 3 是关键：如果在我读取和写入之间，别的线程已经把 `current_index` 改了，CAS 会失败，循环重试。这保证**每个 index 恰好被一个线程领取**——既不重复也不遗漏。

#### 4.1.3 源码精读

先看共享状态。这些全局变量是所有线程通信的「公告板」：

```c
static parallel_func_t current_func;
static volatile int current_index;
static volatile int max_index;
static volatile int active_jobs;
static void * volatile context;
```

[software/libs/libos/bare-metal/schedule.c:23-27](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/schedule.c#L23-L27)。`current_index`/`max_index`/`active_jobs`/`context` 都声明为 `volatile`，强制编译器每次循环重新从内存读取，不去缓存到寄存器——否则工作线程的 `while (current_index == max_index)` 会读成死循环。（关于 `volatile` 与原子性的区别，见 4.2.1。）

接着是领取一个任务的 `dispatch_job`：

```c
static int dispatch_job(void)
{
    int this_index;
    do {
        this_index = current_index;
        if (this_index == max_index)
            return 0;	// No more jobs in this batch
    }
    while (!__sync_bool_compare_and_swap(&current_index, this_index, this_index + 1));

    current_func(context, this_index);
    return 1;
}
```

[software/libs/libos/bare-metal/schedule.c:29-44](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/schedule.c#L29-L44)。`__sync_bool_compare_and_swap(p, old, new)` 的语义是：若 `*p == old` 则把 `*p` 写成 `new` 并返回真，否则什么都不做并返回假。这一步在硬件上是 `load_sync`+`store_sync` 的原子序列（见 u2-l3、u10-l1），是「领号牌」的安全保证。

再看主入口 `parallel_execute`：

```c
void parallel_execute(parallel_func_t func, void *_context, int num_elements)
{
    current_func = func;
    context = _context;
    current_index = 0;
    max_index = num_elements;

    while (current_index != max_index)
        dispatch_job();

    while (active_jobs)
        ; // Wait for threads to finish
}
```

[software/libs/libos/bare-metal/schedule.c:46-58](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/schedule.c#L46-L58)。前四行布置任务（这四行由调用 `parallel_execute` 的主线程独占执行，此时工作线程要么还没被唤醒，要么在 `worker_thread` 里空转）。随后主线程自己抢活直到领光，最后自旋等 `active_jobs` 归零。

工作线程的循环体：

```c
void worker_thread(void)
{
    while (1) {
        while (current_index == max_index)
            ;
        __sync_fetch_and_add(&active_jobs, 1);
        dispatch_job();
        __sync_fetch_and_add(&active_jobs, -1);
    }
}
```

[software/libs/libos/bare-metal/schedule.c:60-71](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/schedule.c#L60-L71)。工作线程永不返回（头文件里 `worker_thread` 带有 `__attribute__((noreturn))`，见 [schedule.h:31](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/schedule.h#L31)）。它先空转到「有任务」状态，再用原子加把 `active_jobs` 加 1 表示「我开始干了」，领一个任务执行，干完再原子减 1。`active_jobs` 正是主线程汇合用的「在途任务计数」。

最后是「唤醒其余线程」的 `start_all_threads`（裸机版）：

```c
#define CR_RESUME_THREAD 21

void start_all_threads(void)
{
    __builtin_nyuzi_write_control_reg(CR_RESUME_THREAD, 0xffffffff);
}
```

[software/libs/libos/bare-metal/schedule.c:21](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/schedule.c#L21) 与 [software/libs/libos/bare-metal/schedule.c:73-76](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/schedule.c#L73-L76)。`0xffffffff` 这个全 1 掩码意味着「恢复所有被挂起的线程」。`CR_RESUME_THREAD` 在硬件里的编号确实是 21（见 [hardware/core/defines.svh:187](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L187)），与它配对的是 `CR_SUSPEND_THREAD`（编号 20，程序结束时用它停机，见 u9-l1）。`__builtin_nyuzi_write_control_reg` 编译成 `setcr` 指令。

#### 4.1.4 代码实践

**实践目标：** 看懂一个真实调用方如何用 `parallel_execute` 把渲染任务分块并行。

**操作步骤：**

1. 打开 [software/libs/librender/RenderContext.cpp:98-129](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/librender/RenderContext.cpp#L98-L129) 的 `RenderContext::finish()`。
2. 观察它连续四次调用 `parallel_execute`，分别分发：顶点着色 `_shadeVertices`、三角形装配 `_setUpTriangle`、像素阶段 `_fillTile`（或线框 `_wireframeTile`）。每次的 `num_elements` 都是把工作量按 16（向量通道数）或按 tile 数切片。
3. 注意 `parallel_execute` 的第二个实参是 `this`（即 `RenderContext*` 作为 `context`），第三个是元素个数。

**需要观察的现象：**

- `_shadeVertices` 接收 `(void *_castToContext, int index)`，其中 `index` 是顶点的「组号」（每组 16 个顶点），它内部用 `index` 算出自己负责的顶点段。
- 整个 `finish()` 是**串行调用多次 `parallel_execute`**：一次并行做完所有顶点着色、汇合，再做三角形装配、汇合，再做像素阶段。每两个阶段之间天然有一个屏障（barrier），因为 `parallel_execute` 会等 `active_jobs` 归零才返回。

**预期结果：** 你能解释「为什么渲染管线每个阶段都要单独 `parallel_execute` 一次」——因为阶段间有数据依赖（像素阶段要读装配好的三角形），必须用 `parallel_execute` 的内置汇合点把前一阶段全部做完，再进入下一阶段。

**待本地验证：** 若你有模拟器环境，可运行任意一个 render 测试（如 `tests/render/fill`），观察 4 个线程是否都参与了渲染（用 `+profile` 或 `-v` 可看线程活动，详见 u11-l2、u8-l1）。

#### 4.1.5 小练习与答案

**练习 1：** `parallel_execute` 里主线程为什么不直接 `while (active_jobs) ;` 等待，而要先 `while (current_index != max_index) dispatch_job();`？

**参考答案：** 让主线程也参与干活，避免任务充足时主线程空等浪费算力；同时这也能加快整体吞吐。只有任务全被领光后，主线程才退居二线等待收尾。

**练习 2：** 如果 `num_elements` 传入 0，`parallel_execute` 会怎样？

**参考答案：** `current_index=0` 且 `max_index=0`，主线程的 `while (current_index != max_index)` 一次都不进入；工作线程的 `while (current_index == max_index)` 恒真、继续空转。主线程随即进入 `while (active_jobs)`，而此刻没有任何线程来得及把 `active_jobs` 加 1（因为 `dispatch_job` 立即返回 0），所以 `active_jobs` 仍是 0，主线程直接返回。结论：空批次是安全的。

---

### 4.2 线程同步：原子操作与汇合

#### 4.2.1 概念说明

任务池要正确工作，必须解决三个并发问题：

1. **互斥领取**：同一个 `index` 不能被两个线程同时领走（否则任务重复），也不能谁都领不到（否则任务漏掉）。
2. **准确计数**：`active_jobs` 必须准确反映「在途任务数」，多个线程同时增减时不能丢更新。
3. **可见性**：一个线程对 `current_index` 的写入，必须被另一个线程的自旋循环及时看见。

这三个问题分别由三样东西解决：

- **问题 1** 用 `__sync_bool_compare_and_swap`（CAS）—— 它编译成 `load_sync`/`store_sync`（LL/SC）原语，是一个不可被打断的「条件写」。
- **问题 2** 用 `__sync_fetch_and_add` —— 同样编译成 LL/SC，是「原子读-改-写」。
- **问题 3** 用 `volatile` 关键字 —— 它**只**保证可见性（每次访问都真正读写内存，不被缓存到寄存器），**不**保证原子性，**也不**保证内存序。

> ⚠️ 常见误区：以为把变量声明成 `volatile` 就线程安全了。在 Nyuzi 上 `volatile int x; x++;` **不是**原子操作（它仍是 load→add→store 三步，可被打断）。必须用 `__sync_*` 内建函数才能拿到原子性。本讲的 `current_index` 虽是 `volatile`，但它每次的修改都套在 CAS 里，正是这个道理。

#### 4.2.2 核心流程

**CAS 领取的时序**（两个线程同时抢 index = 5）：

```
线程 A: this_index = 5
线程 B: this_index = 5            （都读到同一个值）
线程 A: CAS(idx, 5, 6) → 成功, idx 变 6, 返回真
线程 B: CAS(idx, 5, 6) → 失败(idx 已是 6), 返回假 → 重试
线程 B: this_index = 6, CAS(idx, 6, 7) → 成功
```

最终 A 处理 index 5，B 处理 index 6，互不冲突。

**汇合计数**：`active_jobs` 是主线程判断「能不能返回」的唯一依据。工作线程领活前 `+1`、干完 `-1`。只要还有任何线程在执行任务，`active_jobs > 0`，主线程就继续等。当所有任务执行完毕，每个线程成对地 +1/-1，`active_jobs` 回到 0，主线程安全返回。

从数学上看，一次 `parallel_execute` 执行期间，所有 `dispatch_job` 成功的次数之和等于 `num_elements`，而 `active_jobs` 的增减总是一比一配对，故汇合时：

\[
\text{最终 active\_jobs} = \text{初始 active\_jobs} = 0
\]

这便是「不重不漏」的不变量。

#### 4.2.3 源码精读

领取处的 CAS 循环已在 4.1.3 引用，这里聚焦汇合计数：

```c
void worker_thread(void)
{
    while (1) {
        while (current_index == max_index)
            ;
        __sync_fetch_and_add(&active_jobs, 1);   // 进临界：在途+1
        dispatch_job();
        __sync_fetch_and_add(&active_jobs, -1);   // 出临界：在途-1
    }
}
```

[software/libs/libos/bare-metal/schedule.c:65-69](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/schedule.c#L65-L69)。`__sync_fetch_and_add(p, n)` 原子地「返回旧值并把 `*p` 加 `n`」。即便 4 个线程同时执行它，`active_jobs` 也会被正确累加，不会丢更新。

值得对照的是 Mandelbrot 的「手动汇合」——它没用任务池，而是各线程按行交错算完，再用一个共享计数器等齐：

```c
__sync_fetch_and_add(&stop_count, 1);
while (stop_count != NUM_THREADS)
    ;
```

[software/apps/mandelbrot/mandelbrot.c:97-99](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/apps/mandelbrot/mandelbrot.c#L97-L99)。这是静态切分模式下的自建屏障，原理与 `active_jobs` 相同：用原子计数 + 自旋等待实现汇合。注释也点明了为何要等：「returning from main will kill all of them」——裸机下 `main` 返回会触发停机（见 u9-l1 的 `CR_SUSPEND_THREAD`），所以必须先等所有线程算完。

> 关于 `load_sync`/`store_sync` 的硬件实现细节（监视粒度是缓存行、`store_sync` 失败时如何回滚、为什么发射间要屏蔽中断），本讲只点到为止，完整剖析见 u10-l1。

#### 4.2.4 代码实践

**实践目标：** 直观体会「为什么 CAS 不可省」。

**操作步骤：**

1. 想象把 [dispatch_job](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/schedule.c#L29-L44) 里的 CAS 换成朴素写法（如下，**仅作思考实验，请勿真改源码**）：

   ```c
   // 危险的反例（示例代码，非项目原有）
   this_index = current_index;
   if (this_index == max_index) return 0;
   current_index = this_index + 1;   // 没有 CAS
   ```

2. 推演 4 线程同时进入这段代码、`current_index` 当前为 5 时的执行结果。
3. 对比真实 CAS 版本在同样场景下的结果。

**需要观察的现象：**

- 反例中，多个线程可能都读到 `this_index = 5`，然后都把 `current_index` 写成 6，于是多个线程都执行了 index 5 的任务，而 index 6、7… 被跳过——任务既重复又遗漏。
- 真实 CAS 版本里，只有第一个 CAS 成功者拿到 5，其余失败重试后拿到 6、7…，每个 index 恰好被执行一次。

**预期结果：** 你能用一句话说明 CAS 的价值——它把「读旧值、判定、写新值」这三步收敛成一个原子步骤，从而把竞争条件消解掉。

**待本地验证：** 若在模拟器里把反例编译运行（用多线程并发调用），应能观察到任务被重复执行或部分元素未被处理；用 `-v` 跟踪各线程领取的 index 即可对照。

#### 4.2.5 小练习与答案

**练习 1：** `current_index` 已经是 `volatile` 了，为什么领取它还要套 CAS？

**参考答案：** `volatile` 只保证「每次访问都真正读写内存」（可见性、不被缓存到寄存器），不保证「读-改-写」的原子性。两个线程仍可能先后读到同一个旧值。CAS（底层 LL/SC）才把「比较并交换」做成不可打断的原子步骤，确保互斥领取。

**练习 2：** 为什么 `active_jobs` 的 +1 和 −1 一定要用 `__sync_fetch_and_add`，而不能用 `active_jobs++` / `active_jobs--`？

**参考答案：** `++`/`--` 是「读-改-写」三步，可被其他线程的同类操作打断，导致丢失更新（例如两线程同时减 1，结果只减了 1）。`__sync_fetch_and_add` 原子地完成增减，保证 `active_jobs` 准确，主线程的汇合判断才可靠。

---

### 4.3 两种变体：bare-metal 与 kernel

#### 4.3.1 概念说明

libos 提供两套实现，由 [software/libs/libos/CMakeLists.txt](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/CMakeLists.txt) 通过两个子目录 `bare-metal/` 与 `kernel/` 组织。它们的任务池逻辑（`dispatch_job`/`parallel_execute`/`worker_thread`）**完全相同**，差别只在「如何让其它线程进入 `worker_thread`」，也就是 `start_all_threads` 和启动代码。

两套变体对应两种运行环境：

- **bare-metal（裸机）**：没有内核，程序直接跑在硬件上，拥有全部特权。线程的启停靠直接写控制寄存器。典型用于 render 测试、sceneview、benchmarks。
- **kernel（有内核）**：用户程序跑在内核之上，处于非特权态。线程的启停要经系统调用。典型用于 `software/kernel` 之上的用户进程。

之所以能把任务池逻辑写成两份相同的副本，是因为这套「共享计数器 + CAS 领取 + active_jobs 汇合」的算法与环境无关——它只依赖原子操作和共享内存，而这两种环境都提供。这正是 u9-l1 强调的「libc 平台无关、libos 碰硬件」分层的好处。

#### 4.3.2 核心流程

**bare-metal 的线程启动路径：**

```
复位 → 只有线程 0 醒着，PC=0 (_start)
线程0: _start → 建栈 → 全局构造 → main
              main 里: start_all_threads()   // setcr CR_RESUME_THREAD, 0xffffffff
线程1~3: 被唤醒，PC=0 → _start → 建栈 → (跳过构造) → main
              main 里: get_current_thread_id()!=0 → worker_thread()  // 永不返回
线程0: 继续执行真正的程序逻辑，调用 parallel_execute 分发任务
```

关键点：**所有线程都从同一个 `_start` 进入**，但只有线程 0 跑全局构造；其余线程被 `start_all_threads` 唤醒后也从 `_start` 重新开始，靠读自己的线程号决定「我是主线程还是工作线程」。

**kernel 的线程启动路径：**

```
用户 main 里: start_all_threads()
              → 调 spawn_thread() 系统调用 3 次
              → 内核创建 3 个新线程，入口都是 __other_thread_start
新线程: __other_thread_start → worker_thread() → thread_exit()
```

这里线程不是「被唤醒的硬件线程」，而是内核**新建**的软件线程（最终也映射到硬件线程上执行）。入口由 `spawn_thread` 的参数 `__other_thread_start` 指定，该符号定义在 kernel 版 crt0 里。

#### 4.3.3 源码精读

先看裸机启动代码如何让「所有线程都进 `main`」：

```asm
_start:
    getcr s0, 0             // 读自己的线程号
    shl  s0, s0, 14         // 每线程栈 16KiB
    li   sp, 0x200000       // 栈基址
    sub_i sp, sp, s0        // 算出本线程栈顶
    ...
    bnz  s0, do_main        // 非 0 线程跳过全局构造
    ; (线程0) 调用 __init_array 里的构造函数
do_main:
    move s0, 0
    call main               // 所有线程都来到这里
```

[software/libs/libos/bare-metal/crt0.S:42-69](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/crt0.S#L42-L69)。`getcr s0, 0` 读控制寄存器 0（即 `CR_THREAD_ID`，编号 0，见 [hardware/core/defines.svh:166](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/hardware/core/defines.svh#L166)），拿到线程号。`bnz s0, do_main` 让非 0 线程跳过构造函数直接进 `main`——因为此时线程 0 早已完成全局构造，不必重复。

`get_current_thread_id` 就是把这个控制寄存器读出来封装成 C 函数：

```c
int get_current_thread_id(void)
{
    return __builtin_nyuzi_read_control_reg(0);
}
```

[software/libs/libos/bare-metal/nyuzi.c:19-22](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/nyuzi.c#L19-L22)。`__builtin_nyuzi_read_control_reg(0)` 编译成 `getcr ...0`。

于是裸机程序的 `main` 头部固定写成「工作线程分流」：

```cpp
int main()
{
    if (get_current_thread_id() != 0)
        worker_thread();        // 工作线程进死循环
    // 只有主线程走到这里
    ...初始化...
    start_all_threads();        // 唤醒其余线程(它们也会进 main→worker_thread)
    ...主逻辑，调用 parallel_execute...
}
```

这是 [tests/render/fill/main.cpp:50-60](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/tests/render/fill/main.cpp#L50-L60) 的真实写法（sceneview、shadow_map、quakeview 等都一致）。注意 `start_all_threads()` 通常在分流判断**之后**调用——因为唤醒的线程一旦跑起来，第一件事就是进 `main`、判定自己是工作线程、钻进 `worker_thread`。主线程必须在那时已经布置好共享状态（或至少保证 `parallel_execute` 内部才设置 `current_index`/`max_index`），否则工作线程会在 `worker_thread` 里空转——这正好是安全的：初始 `current_index == max_index == 0`，工作线程空转等待，直到主线程调用 `parallel_execute` 改变 `max_index`。

再看 kernel 变体的不同之处。`start_all_threads` 不再写控制寄存器，而是发系统调用创建线程：

```c
extern int __other_thread_start();

void start_all_threads(void)
{
    for (int i = 0; i < 3; i++)
        spawn_thread("thread", __other_thread_start, NULL);
}
```

[software/libs/libos/kernel/schedule.c:71-77](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/kernel/schedule.c#L71-L77)。`spawn_thread` 的签名是 `int spawn_thread(const char *name, int (*start)(void*), void *param)`（见 [software/libs/libos/nyuzi.h:37](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/nyuzi.h#L37)），它在内核里是 `SYS_spawn_thread`（编号 1，见 [software/kernel/syscalls.h:19](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/kernel/syscalls.h#L19)）。注意硬编码创建 3 个新线程（加上原来的主线程共 4 个），与裸机「4 个硬件线程」的数量对齐。

新线程的入口 `__other_thread_start` 在 kernel 版 crt0 里：

```asm
.globl __other_thread_start
__other_thread_start:
    movehi gp, hi(_GLOBAL_OFFSET_TABLE_)
    or     gp, gp, lo(_GLOBAL_OFFSET_TABLE_)
    call   worker_thread
    call   thread_exit
```

[software/libs/libos/kernel/crt0.S:41-47](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/kernel/crt0.S#L41-L47)。它设好 `gp`（全局指针，用于位置无关代码访问全局变量），然后直接调 `worker_thread`——因为内核线程不需要走 `_start` 那套栈/构造初始化（内核在创建线程时已布置好栈）。`worker_thread` 理论上不返回，但出于稳健性后面跟了一个 `thread_exit`。

#### 4.3.4 代码实践

**实践目标：** 对比两种变体的启动差异，确认任务池逻辑的相同性。

**操作步骤：**

1. 并排打开 [bare-metal/schedule.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/bare-metal/schedule.c) 与 [kernel/schedule.c](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/kernel/schedule.c)。
2. 用 diff 思维比较：除了 `start_all_threads` 与多了 `#include "nyuzi.h"`、`extern int __other_thread_start();` 之外，`dispatch_job`/`parallel_execute`/`worker_thread` 是否完全一致？
3. 阅读 kernel 版的 [crt0.S](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/kernel/crt0.S)，对比裸机版：kernel 版的 `_start` 不再读线程号、不再分栈，为什么？

**需要观察的现象：**

- 两份 `schedule.c` 的任务池函数体逐字相同——印证「分发算法与环境无关」。
- kernel 版 `start_all_threads` 创建线程数硬编码为 3（循环 `i < 3`），主线程 + 3 个工作线程 = 4，与硬件 4 线程对齐。
- kernel 版 `_start` 只做全局构造后直接 `call main`（[crt0.S:18-33](https://github.com/jbush001/NyuziProcessor/blob/ed5c1a50b77af80e54800e21bd8b62822c3f496a/software/libs/libos/kernel/crt0.S#L18-L33)），没有 `getcr`/分栈逻辑——因为内核线程的栈由内核分配，且用户态也无法直接写 `CR_RESUME_THREAD` 这种特权控制寄存器。

**预期结果：** 你能总结出一张对照表：

| 维度 | bare-metal | kernel |
| --- | --- | --- |
| 任务池逻辑 | 相同 | 相同 |
| `start_all_threads` | `setcr CR_RESUME_THREAD, 0xffffffff`（唤醒硬件线程） | `spawn_thread` 系统调用 ×3（新建线程） |
| 工作线程入口 | 经 `_start` → `main` → 按线程号分流进 `worker_thread` | `__other_thread_start` → 直接 `worker_thread` |
| 栈/构造 | 各线程在 `_start` 自行分栈、跳过构造 | 内核分配栈、构造由主线程完成 |
| 特权 | 直接碰控制寄存器 | 经系统调用 |

**待本地验证：** kernel 变体需要在内核之上运行用户程序，环境搭建较重；裸机变体可直接用模拟器跑 render 测试验证。本步以源码阅读为主。

#### 4.3.5 小练习与答案

**练习 1：** 为什么 bare-metal 版的工作线程「会」经过 `main`，而 kernel 版的工作线程「不」经过 `main`？

**参考答案：** 裸机下所有硬件线程都从复位向量 `_start` 进入，共用同一套启动代码，因此必然走到 `call main`，靠 `get_current_thread_id` 在 `main` 内部分流。内核下，工作线程是 `spawn_thread` 新建的，入口由调用方指定为 `__other_thread_start`，它直接调 `worker_thread`，无需也不应再走用户 `main`（`main` 只应由最初的主线程执行一次）。

**练习 2：** 如果把 kernel 版 `start_all_threads` 的循环改成 `i < 10`，会发生什么？

**参考答案：** 会向内核请求创建 10 个新线程。能否真正并发取决于内核线程调度与底层硬件线程数（4 个硬件线程时，10 个软件线程会被时分复用到这 4 个硬件线程上）。任务池逻辑仍正确，但并行度不会超过硬件线程数，且线程过多会增加调度开销。这个 3 是与默认 4 硬件线程（1 主 + 3 工）匹配的选择。

---

## 5. 综合实践

**任务：** 用 `parallel_execute` 编写一个多线程并行数组求和程序，把以上三个最小模块串起来。

**要求做到：**

1. **唤醒**：主线程在 `main` 里调用 `start_all_threads()`，让其余硬件线程进入 `worker_thread`。
2. **分发与领取**：把 N 个元素的求和切成若干任务（每个任务处理一段），用 `parallel_execute` 分发；每个任务通过 CAS 领取自己的 `index`（任务池已经替你做了这步，你要理解它）。
3. **汇合**：所有任务完成后 `parallel_execute` 返回，主线程汇总各部分和。

**参考骨架（示例代码，非项目原有文件）：**

```c
#include <stdio.h>
#include "schedule.h"
#include "nyuzi.h"

#define N 1024
#define CHUNK 64
#define NUM_CHUNKS (N / CHUNK)

static int data[N];
static int partial[NUM_CHUNKS];   // 每个 chunk 的部分和

static void sum_chunk(void *ctx, int index)
{
    int base = index * CHUNK;
    int s = 0;
    for (int i = 0; i < CHUNK; i++)
        s += data[base + i];
    partial[index] = s;           // 不同 index 写不同位置，无竞争
}

int main(void)
{
    if (get_current_thread_id() != 0)
        worker_thread();          // 工作线程分流

    for (int i = 0; i < N; i++) data[i] = i;
    start_all_threads();          // 唤醒其余线程

    parallel_execute(sum_chunk, NULL, NUM_CHUNKS);  // 并行求和 + 内置汇合

    int total = 0;
    for (int i = 0; i < NUM_CHUNKS; i++) total += partial[i];
    printf("total=%d\n", total);
    return 0;
}
```

**说明你应该能解释的每一步：**

- `if (get_current_thread_id() != 0) worker_thread();`：工作线程被唤醒后从 `_start` 进 `main`，在此分流钻进 `worker_thread` 的死循环，等待任务（4.3）。
- `start_all_threads()`：写 `CR_RESUME_THREAD` 唤醒其余线程（4.1）。
- `parallel_execute` 内部：设置 `current_index=0`、`max_index=NUM_CHUNKS`；主线程与工作线程都通过 CAS 抢 `index` 调用 `sum_chunk`；每个线程领活前 `active_jobs+1`、干完 `active_jobs-1`；全部完成后主线程的 `while (active_jobs)` 结束、返回（4.1、4.2）。
- 汇合后主线程串行累加 `partial[]`——此时已无并发，无需原子操作。
- `sum_chunk` 里各任务写各自的 `partial[index]`，互不踩踏，是任务并行里常见的「按 index 分区写」无竞争写法。

**待本地验证：** 在模拟器中编译运行，对照单线程求和结果是否一致；用 `-v` 观察不同线程领取了哪些 `index`，验证「每个 index 恰被执行一次」。

## 6. 本讲小结

- `parallel_execute(func, ctx, N)` 把 N 个任务放进共享任务池，靠一个全局 `current_index` 让所有线程（含主线程）自取任务，实现动态负载均衡的并行。
- 任务领取用 `__sync_bool_compare_and_swap`（CAS，底层 LL/SC）保证「每个 index 恰被一个线程领取」；汇合用 `__sync_fetch_and_add` 维护的 `active_jobs` 计数，主线程自旋至其归零才返回。
- `volatile` 只保证可见性（不缓存到寄存器），**不**保证原子性；原子性必须靠 `__sync_*` 内建函数。这是本讲最易踩的坑。
- 裸机版 `start_all_threads` 写控制寄存器 `CR_RESUME_THREAD`（编号 21）唤醒硬件线程，所有线程从 `_start` 进入、按线程号分流进 `worker_thread`。
- 内核版 `start_all_threads` 发 `spawn_thread` 系统调用新建 3 个线程，入口 `__other_thread_start` 直接调 `worker_thread`；两套变体的任务池逻辑逐字相同。
- 工作线程永不返回（`worker_thread` 标记 `noreturn`），常驻等待任务；`main` 返回会触发停机，故需先汇合（参见 Mandelbrot 的 `stop_count` 屏障）。

## 7. 下一步学习建议

- **深入原子原语的硬件实现**：本讲的 `__sync_*` 内建最终落到 `load_sync`/`store_sync`。下一站读 u10-l1（同步内存操作 LL/SC 与 membar），看清 store 队列里同步状态的成功/失败与回滚机制。
- **进入渲染管线**：本讲看到 `RenderContext::finish` 多次调用 `parallel_execute`。接着读 u9-l3（librender 渲染库基础）与 u13-l1（tile-based 渲染架构），理解几何阶段与像素阶段如何用这套并行骨架分发三角形与 tile。
- **理解内核线程**：若对 kernel 变体的 `spawn_thread` 背后机制感兴趣，可预习 u12-l3（线程、上下文切换与同步原语），看内核如何创建、调度、切换线程。
- **追踪一条调用链**：从 `parallel_execute` → `dispatch_job` → `__sync_bool_compare_and_swap` → `load_sync`/`store_sync`（u2-l3）→ `l1_store_queue`（u6-l1、u10-l1），把「软件并行」与「硬件同步」两层串成一条完整链路。
