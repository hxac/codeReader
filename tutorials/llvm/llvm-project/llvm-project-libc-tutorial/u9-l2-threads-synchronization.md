# 线程与同步原语：raw_mutex、futex 与 pthread

## 1. 本讲目标

本讲带你进入 LLVM-libc 的并发世界。学完本讲，你应当能够：

- 说清 `SpinLock`（纯用户态自旋）与 `RawMutex`（自旋 + futex 阻塞回退）两种底层锁的差异，并看懂 `RawMutex` 用「三态字 + 先自旋后停车」换来「无竞争路径零 syscall」的设计。
- 读懂 `Futex` 封装如何把一条 Linux `futex` 系统调用包成 `wait`/`notify_one`/`notify_all`/`requeue_to` 四个原语，并理解它为何「直接调用 `syscall_impl`」而非走 `syscall_checked`（承接 [u8-l1 OSUtil](u8-l1-osutil-linux-syscalls.md)）。
- 看清 `Thread` 线程对象如何用 `clone` 系统调用创建线程、用 `clear_tid` futex 实现 `join`/`detach`，并理解 `ThreadAttributes` 三态 `detach_state` 的状态机。
- 画出一条完整调用链：`pthread_mutex_lock` → `Mutex::lock` → `RawMutex::lock` → `Futex::wait` → `futex` 系统调用，并说清每层各加了什么语义。

本讲是「内存管理与并发」单元的第二篇，直接承自 [u8-l1 OSUtil 与系统调用封装](u8-l1-osutil-linux-syscalls.md)（futex 与 clone 都通过 `syscall_impl` 进入内核）与 [u4-l3 错误处理](u4-l3-error-handling-errno.md)（`ErrorOr`/`MutexError` 与 `errno` 的翻译），并与上一讲 [u9-l1 内存分配器](u9-l1-memory-allocator.md) 形成互补——上一讲的 `freelist_heap` 是单线程实现，本讲给出让它变线程安全的锁。

## 2. 前置知识

在进入源码前，先用最朴素的语言回顾几个概念。

- **临界区与互斥（mutual exclusion）**：多线程程序里，有些代码段（如「读-改-写」一个共享计数器）同一时刻只能有一个线程执行，否则结果错乱。这段代码叫临界区，保证「一次只进一个」的机制叫互斥锁（mutex）。`lock()` 表示「我要进，别人先等着」，`unlock()` 表示「我出来了，下一个可以进」。
- **自旋（spin）与阻塞（block/park）**：当一个线程拿不到锁时，有两种等法。**自旋**是「在 CPU 上空转反复查锁」，响应极快但白费 CPU、而且会持续占用缓存线和内存总线；**阻塞**是「告诉内核我要睡，锁好了叫醒我」，不费 CPU 但一次「睡—叫醒」要走两次系统调用、开销大（微秒级）。好的锁策略是「先自旋一小会儿赌它马上就放，赌不中再阻塞」——本讲的 `RawMutex` 正是如此。
- **futex（fast userspace mutex）**：Linux 提供的一类系统调用，是几乎所有高效同步原语的基石。它的关键思想是「**无竞争时完全不进内核**」：锁的状态用一个普通用户态整数（futex word）表示，加锁/解锁只靠原子指令操作这个整数，无需 syscall；**只有当拿不到锁需要睡、或释放锁需要叫醒别人时**，才调用 `futex` 系统调用让内核介入。futex 的两个基本操作是 `FUTEX_WAIT`（「如果这个地址的值仍等于 expected，就把我挂到这个地址的等待队列上睡」）和 `FUTEX_WAKE`（「叫醒 n 个在这个地址上睡的线程」）。
- **入口点与不透明类型（opaque type）**：回顾 [u2-l1 入口点机制](u2-l1-entrypoint-mechanism.md)，每个公开函数是一个独立构建单元。回顾 [u8-l3 stdio FILE 模型](u8-l3-stdio-file-model.md)，`FILE` 是对不透明类型——调用者只拿到一个指针，实现内部用 `reinterpret_cast` 把它当成自己的 C++ 类。本讲的 `pthread_mutex_t`/`pthread_t` 沿用完全相同的套路。

> 名词澄清：futex 系统调用只关心「一个地址」和「一个期望值」。它**不规定**这个整数里每个比特代表什么含义——那是由上层的锁算法自己定义的。本讲你会看到 `RawMutex` 把这个 32 位整数解释成「三态」、`Thread` 把它解释成「清除标志」。

## 3. 本讲源码地图

本讲涉及的关键文件如下表。所有路径相对于仓库 `libc/` 目录。锁与线程的**内部实现**都位于 `src/__support/threads/`（私有工具库，回顾 [u4-l1](u4-l1-internal-support-overview.md)），**公开入口点**位于 `src/pthread/`。

| 文件 | 作用 | 关键符号 |
|------|------|----------|
| `src/__support/threads/spin_lock.h` | 纯用户态 TTAS 自旋锁 | `SpinLock` |
| `src/__support/threads/raw_mutex.h` | 三态自旋+futex 互斥锁（内部用） | `RawMutex`、`lock_slow` |
| `src/__support/threads/futex_utils.h` | futex 工具的**平台分派头**（选 linux/darwin） | —— |
| `src/__support/threads/linux/futex_utils.h` | Linux 的 `Futex` 封装 | `Futex::wait`/`notify_one`/`notify_all`/`requeue_to` |
| `src/__support/threads/linux/futex_word.h` | futex 字类型与系统调用号 | `FutexWordType`、`FUTEX_SYSCALL_ID` |
| `src/__support/threads/unix_mutex.h` | 在 `RawMutex` 之上加 POSIX 语义（递归/检错） | `Mutex` |
| `src/__support/threads/mutex.h` | `Mutex` 的平台/单线程分派 | `LIBC_THREAD_MODE` |
| `src/__support/threads/mutex_common.h` | 错误码枚举 | `MutexError` |
| `src/__support/threads/thread.h` | 线程对象与属性（平台无关部分） | `Thread`、`ThreadAttributes`、`self` |
| `src/__support/threads/linux/thread.cpp` | Linux 线程实现（clone/wait/exit） | `Thread::run`、`thread_exit` |
| `src/__support/threads/identifier.h` | 获取线程 id（owner 跟踪用） | `internal::gettid` |
| `src/pthread/pthread_mutex_{init,lock,unlock}.cpp` | 互斥锁入口点（薄壳） | `LLVM_LIBC_FUNCTION` |
| `src/pthread/pthread_create.cpp`、`pthread_spin_lock.cpp` | 线程创建/自旋锁入口点 | —— |

依赖层次（从底到顶）：

```
syscall_impl  (u8-l1 的 OSUtil)
   ↑
Futex  (linux/futex_utils.h) ── 一条 futex 系统调用包成 wait/wake/requeue
   ↑
SpinLock (spin_lock.h)        ── 纯原子，不碰 futex
RawMutex (raw_mutex.h)        ── 三态字 + 自旋 + futex 阻塞
   ↑
Mutex   (unix_mutex.h)        ── RawMutex + POSIX 递归/检错/owner 跟踪
   ↑
pthread_mutex_*  (src/pthread)── reinterpret_cast 薄壳 + errno 映射

Thread (thread.h + linux/thread.cpp) ── clone 创建 / clear_tid futex 等待
   ↑
pthread_create / pthread_join        ── reinterpret_cast 薄壳
```

记住这两张图，下面四个最小模块就是把它逐层展开。

## 4. 核心概念与源码讲解

本讲拆成四个最小模块：**底层锁原语**（`SpinLock` + `RawMutex`）、**futex 封装**（`Futex`）、**线程对象**（`Thread`）、**pthread 映射**。

### 4.1 底层锁原语：SpinLock 与 RawMutex

#### 4.1.1 概念说明

LLVM-libc 在 `__support/threads/` 下提供了**两个层次**的锁原语。它们的设计目的不同：

- **`SpinLock`** 是最轻量的锁：只有一个字节的状态，加锁解锁全靠原子指令，**完全不调用任何系统调用**。它适合「临界区极短、持锁时间远小于一次 syscall 开销」的场景（如 `fork_callbacks` 注册表）。代价是拿不到锁时死等、浪费 CPU，所以绝不能用来保护长任务。
- **`RawMutex`** 是「既能自旋又能阻塞」的混合锁，名字里的 *Raw* 指「它只管加锁解锁本身，**不**处理 POSIX 那套递归、检错、robust 等高阶语义」。它是 `__support` 内部用的计时锁，给上层 `Mutex` 当地基。

`RawMutex` 要解决的核心难题是：**如何让「无竞争」这一最常见情况不付出任何 syscall 代价，同时让「有竞争」时等待线程真正阻塞、不空烧 CPU？** 它的答案是一个三态 futex 字加上「先自旋、赌不中再 `futex_wait`」的策略，下面逐步拆开。

#### 4.1.2 核心流程

**SpinLock 的 TTAS（Test-and-Test-and-Set）策略**：

```
try_lock():  原子地把 flag 交换成 1，若旧值是 0 则成功
lock():      外层循环 { 调 try_lock()（发 xchg，有写流量）
             内层循环 { 只读 flag（RELAXED），等到它变 0 再回到外层 } }
unlock():    原子地把 flag 存成 0（RELEASE）
```

关键在「外层发原子交换、内层只读」的分层：拿不到锁时，线程**不再**反复对内存发起写操作（xchg 会触发缓存一致性总线流量），而是安静地读自己缓存里的副本，直到缓存一致性协议把「别人解锁了」的写传播过来，才回到外层真正再试一次。

**RawMutex 的三态 + 先自旋后阻塞策略**。futex 字有三种取值：

| 状态名 | 值 | 含义 |
|--------|----|----|
| `UNLOCKED` | `0b00` | 没人持锁 |
| `LOCKED` | `0b01` | 有人持锁，但**没有人在等待** |
| `IN_CONTENTION` | `0b10` | 有人持锁，**且有线程在 futex 上排队等待** |

加锁流程（伪代码）：

```
lock():
    if try_lock() 成功:            # 无竞争快路径，一次 CAS，零 syscall
        return
    return lock_slow(...)          # 进入慢路径

lock_slow():
    state = spin(spin_count)       # 先自旋若干次
    if spin 期间看到 UNLOCKED 且 CAS→LOCKED 成功: return  # 自旋赌中
    for (;;):                      # 持续竞争
        if state != IN_CONTENTION:
            old = exchange(IN_CONTENTION)   # 标记「有人在等」
            if old == UNLOCKED: return      # 抢到了
        futex.wait(IN_CONTENTION, timeout)  # 阻塞，被叫醒后回到 spin
        if 超时: return false
        state = spin(spin_count)            # 叫醒后再自旋一轮
```

解锁流程：

```
unlock():
    prev = exchange(UNLOCKED, RELEASE)   # 直接写成解锁
    if prev == IN_CONTENTION:            # 曾经有人在等
        wake()  → futex.notify_one()     # 必须叫醒一个
    return prev != UNLOCKED              # 检测「没锁就解」的错误
```

三态设计的妙处：`LOCKED`（有人锁、无人等）与 `IN_CONTENTION`（有人锁、有人等）的区分，让**解锁时一眼就知道要不要发 `futex_wake`**。无竞争场景下整个生命周期只有两条原子指令（CAS 加锁、exchange 解锁），绝不触碰 syscall——这正是 futex「无竞争不进内核」哲学的体现。

#### 4.1.3 源码精读

**SpinLock 的 TTAS 双层循环**——外层 `try_lock()` 发原子交换，内层只 `load(RELAXED)`：

[spin_lock.h:23-25](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/spin_lock.h#L23-L25) `try_lock` 用 `exchange(1u, ACQUIRE)`：返回值是旧值，旧值为 0 表示此前未锁、本次成功抢到。

[spin_lock.h:50-53](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/spin_lock.h#L50-L53) `lock()` 的双层 `while`。源文件上方 [L28-L44](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/spin_lock.h#L28-L44) 的注释贴出了它在 armv9a 和 x86_64 上的汇编，直观说明「内层只用 load 语义指令、`swpab`/`xchg` 只在外层发」如何减少写流量。

[spin_lock.h:54](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/spin_lock.h#L54) `unlock` 只是一条 `store(0u, RELEASE)`——整把锁从头到尾没有任何 syscall。

**RawMutex 的三态常量**：

[raw_mutex.h:41-43](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/raw_mutex.h#L41-L43) 三个状态常量。注意它们是 `FutexWordType` 的位掩码（`0b00`/`0b01`/`0b10`），因为这是给上层语义用的编码，不是 futex 系统调用关心的内容。

**RawMutex 的自旋函数 `spin`**：

[raw_mutex.h:47-62](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/raw_mutex.h#L47-L62) 自旋循环只 `load(RELAXED)` 读状态，直到「解锁了」「进入竞争了」或「自旋次数耗尽」三者之一才返回；每次循环调 `sleep_briefly()`（即 [sleep.h:18-33](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/sleep.h#L18-L33) 里的 `pause`/`isb` 指令）让流水线歇一下，避免推测执行带来的额外内存操作。默认自旋次数见 [raw_mutex.h:29-31](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/raw_mutex.h#L29-L31)（`LIBC_COPT_RAW_MUTEX_DEFAULT_SPIN_COUNT` = 100）。

**RawMutex 的快路径 `lock` 与慢路径 `lock_slow`**：

[raw_mutex.h:108-116](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/raw_mutex.h#L108-L116) `lock()` 先 `LIBC_LIKELY(try_lock())` 抢一次（[L113](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/raw_mutex.h#L113)），失败才进 `lock_slow`。`try_lock` 用 `compare_exchange_strong`（[L102-L107](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/raw_mutex.h#L102-L107)）——这里特意选 *strong* 版本，因为 `try_lock` 通常只调一次，不容许伪失败。

[raw_mutex.h:66-93](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/raw_mutex.h#L66-L93) `lock_slow` 是慢路径核心：先 `spin` 一轮（[L68](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/raw_mutex.h#L68)），若自旋中看到 `UNLOCKED` 就 CAS 抢锁（[L70-L73](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/raw_mutex.h#L70-L73)）；否则进入 `for(;;)` 竞争循环：当状态不是 `IN_CONTENTION` 时用 `exchange(IN_CONTENTION)` 一次性「标记有人等 + 试探能否拿到」（[L82-L84](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/raw_mutex.h#L82-L84)），若交换前是 `UNLOCKED` 即抢到；竞争持续则 `futex.wait(IN_CONTENTION, timeout, is_pshared)` 把自己挂起（[L86](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/raw_mutex.h#L86)），超时返回 `false`（[L87-L88](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/raw_mutex.h#L87-L88)），被叫醒后回到 `spin`（[L91](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/raw_mutex.h#L91)）。

**RawMutex 的 `unlock` 与条件唤醒**：

[raw_mutex.h:117-124](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/raw_mutex.h#L117-L124) `exchange(UNLOCKED, RELEASE)` 先把锁写成解锁（[L118](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/raw_mutex.h#L118)）；只有当旧值是 `IN_CONTENTION`（曾有人在等）时才 `wake()`（[L120-L121](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/raw_mutex.h#L120-L121)）——即 `futex.notify_one`（见 [raw_mutex.h:95](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/raw_mutex.h#L95)）。`prev != UNLOCKED` 的返回值用于检测「没上锁就解锁」的错误。

#### 4.1.4 代码实践

**实践目标**：亲手跑一遍 `RawMutex` 的三态，观察「无竞争不解锁时不需要 wake」。

**操作步骤**：

1. 打开单元测试 [test/src/__support/threads/linux/raw_mutex_test.cpp:23-31](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/src/__support/threads/linux/raw_mutex_test.cpp#L23-L31) 的 `SmokeTest`。它在一个线程里依次 `lock`→`unlock`→`try_lock`→`try_lock`(失败)→`unlock`→`unlock`(失败)。
2. 对照源码标注每一步 futex 字的取值变化：`UNLOCKED →（lock 成功）→ LOCKED →（unlock）→ UNLOCKED →（try_lock 成功）→ LOCKED →（try_lock 失败，字保持 LOCKED）→（unlock）→ UNLOCKED`。注意：**整条用例没有任何一次进入 `IN_CONTENTION`**，因此整条用例**零 syscall**。
3. 想看「有等待者」的场景，读同文件 `Timeout` 用例 [L33-L52](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/src/__support/threads/linux/raw_mutex_test.cpp#L33-L52)：主线程持锁不放，对一个已过期的绝对超时再 `lock(*timeout)`，由于锁可直接拿到（[L113](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/raw_mutex.h#L113) 快路径优先），注释明确说「expired timeout will not count」。

**需要观察的现象**：`SmokeTest` 全程不进入内核；`Timeout` 在真正死锁时（删掉提前 `unlock` 模拟）才会因 `futex_wait` 超时返回 `false`。

**预期结果**：你能在纸上画出每一步的 futex 字值，并解释为什么 `LOCKED` 状态的解锁不触发 `wake`。本实践为源码阅读型，标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：`RawMutex::try_lock` 为什么用 `compare_exchange_strong` 而不是 `weak`？

**答案**：`try_lock` 通常只调用一次（[L102-L107](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/raw_mutex.h#L102-L107) 注释 "one-time operation"）。`weak` 版本允许伪失败（spurious failure，即值其实匹配却报告失败），适合包在循环里重试的场景；`strong` 保证匹配就成功，避免「只试一次却偶发失败」导致 `pthread_mutex_trylock` 误报 `EBUSY`。

**练习 2**：`SpinLock` 和 `RawMutex` 各自适合什么场景？

**答案**：`SpinLock` 不发任何 syscall、延迟最低，适合持锁时间极短（远小于一次 syscall）的临界区，但拿不到锁会白烧 CPU，不能等长任务。`RawMutex` 拿不到时先自旋再 `futex_wait` 阻塞、让出 CPU，适合持锁时间不确定或较长的临界区。代价是竞争时多一次 syscall。一般原则：**只有当你确信临界区「几条指令」且不该 sleep 时才用 `SpinLock`**。

### 4.2 futex 封装：Futex

#### 4.2.1 概念说明

`RawMutex` 的 `wait`/`wake` 最终要落到 Linux 的 `futex` 系统调用上。这一封装由 `__support/threads/linux/futex_utils.h` 里的 `Futex` 类提供。

`Futex` 的定位很纯粹：**它把「一个 32 位原子整数 + 一条 futex 系统调用」包成四个易用的原语**。它继承自 `cpp::Atomic<FutexWordType>`——也就是说，**一个 `Futex` 对象本身就是一个可被原子读写、又可作为 futex 等待地址的整数**。锁算法（如 `RawMutex`）把它作为成员，既用它的原子操作读写状态，又在需要时调它的 `wait`/`notify`。

> 前置提醒（承接 [u8-l1](u8-l1-osutil-linux-syscalls.md)）：`Futex` 调用的是 `syscall_impl`——**裸调用、不做错误检查**，而不是 `syscall_checked`。原因是 futex 的返回值语义由 `Futex` 自己按「`-EINTR`/`-EAGAIN`/`-ETIMEDOUT`」分类处理，不沿用「负值即 errno」的通用翻译；且 `wait` 期望循环重试 `EINTR`，这与 `syscall_checked` 的「失败即返回 `Error`」语义不符。

#### 4.2.2 核心流程

四个原语映射到 futex 系统调用的不同操作码：

| `Futex` 方法 | futex 操作码 | 语义 |
|--------------|--------------|------|
| `wait(expected, timeout, is_shared)` | `FUTEX_WAIT_BITSET[_PRIVATE]` | 若当前值仍等于 `expected`，阻塞当前线程直到被 wake、超时或值改变 |
| `notify_one(is_shared)` | `FUTEX_WAKE[_PRIVATE]`，wake_limit=1 | 叫醒至多 1 个等待者 |
| `notify_all(is_shared)` | `FUTEX_WAKE[_PRIVATE]`，wake_limit=INT_MAX | 叫醒所有等待者 |
| `requeue_to(other, ...)` | `FUTEX_CMP_REQUEUE[_PRIVATE]` | 把等待者从「我」搬到「另一个 futex」（条件变量广播用） |

`wait` 的处理逻辑（伪代码）：

```
wait(expected, timeout, is_shared):
    op = is_shared ? FUTEX_WAIT_BITSET : FUTEX_WAIT_BITSET_PRIVATE
    if timeout 且为 REALTIME: op |= FUTEX_CLOCK_REALTIME
    for (;;):
        if load(RELAXED) != expected: return 0   # 值已变，不用等了
        ret = syscall(FUTEX_SYSCALL_ID, this, op, expected, timeout_ptr, NULL, FUTEX_BITSET_MATCH_ANY)
        if ret == -EINTR:  continue              # 被信号打断，重试
        if ret == -EAGAIN or -EWOULDBLOCK: return 0  # 值已变（内核视角），正常返回
        if ret < 0:  return unexpected(-ret)     # 真错误（如 ETIMEDOUT）
        return ret
```

两个要点：

1. **「先检查值、再进内核」的双保险**。`wait` 进内核前先 `load()` 比对（[L55](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/linux/futex_utils.h#L55)），内核内部也会再比对一次 `expected`。这避免了「解锁方已 wake、等待方才 wait」的丢唤醒竞态——若值已变，`wait` 立即返回，不阻塞。
2. **用 bitset 变体（`FUTEX_WAIT_BITSET`）是为了支持绝对超时**。普通的 `FUTEX_WAIT` 用相对超时，而 `FUTEX_WAIT_BITSET` 配合 `FUTEX_CLOCK_REALTIME` 可指定「墙上时钟的绝对时刻」，这正是 `RawMutex::timed_lock` 与条件变量定时等待所需的语义；`FUTEX_BITSET_MATCH_ANY` 表示「匹配任意位」，退化为普通 wait。

#### 4.2.3 源码精读

**平台分派头**——延续 [u8-l1](u8-l1-osutil-linux-syscalls.md) 讲过的「目录隔离 + 头文件分派」模式：

[futex_utils.h:12-18](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/futex_utils.h#L12-L18) 顶层 `futex_utils.h` 按 `__linux__`/`__APPLE__` 选 OS 实现，其它平台直接 `#error`。

**futex 字类型与系统调用号**：

[linux/futex_word.h:17-18](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/linux/futex_word.h#L17-L18) `FutexWordType = uint32_t`——注释强调 futex 字在**所有平台（含 64 位）都是 32 位**，因为内核 futex 实现固定按 32 位操作。[L20-L25](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/linux/futex_word.h#L20-L25) 优先用 `SYS_futex_time64`（解决 2038 问题），回退到 `SYS_futex`。

**`Futex` 类继承自原子**：

[linux/futex_utils.h:37-41](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/linux/futex_utils.h#L37-L41) `class Futex : public cpp::Atomic<FutexWordType>`，所以 `Futex` 既是可原子读写的整数、其地址又能直接作为 futex 系统调用的等待地址——二者天然合一。[L161-L162](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/linux/futex_utils.h#L161-L162) 的 `static_assert(__is_standard_layout(Futex))` 保证它没有额外成员、布局就是那个 32 位整数，这样它才能被安全地放进共享内存、跨进程用同一地址等待。

**`wait` 的双保险与返回值分类**：

[linux/futex_utils.h:54-55](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/linux/futex_utils.h#L54-L55) 进内核前的 `load(RELAXED) != expected` 预检查。

[linux/futex_utils.h:83-91](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/linux/futex_utils.h#L83-L91) 真正的 `syscall_impl<int>` 调用，依次传 futex 地址、操作码 `op`、期望值、超时指针、`FUTEX_BITSET_MATCH_ANY`。

[linux/futex_utils.h:94-104](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/linux/futex_utils.h#L94-L104) 返回值三分支：`-EINTR` 重试（`continue`）；`-EAGAIN`/`-EWOULDBLOCK` 当作「值已变、正常结束」返回 0；其余负值 `unexpected(-ret)`（典型为 `-ETIMEDOUT`）。注意全程是 `syscall_impl`（裸调用），错误分类逻辑完全由 `Futex` 自己掌握。

**32 位平台的 64 位时间兼容**：

[linux/futex_utils.h:59-81](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/linux/futex_utils.h#L59-L81) 当用 `SYS_futex_time64` 时，内核要求 64 位 `__kernel_timespec`；在 32 位 `time_t` 平台上需要把 `timespec` 转换，避免结构体尺寸不匹配。配合 [L29-L34](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/linux/futex_utils.h#L29-L34) 的 `static_assert`：老的 `SYS_futex` 回退路径只在 `tv_nsec` 与寄存器等宽时才安全。

**`notify_one`/`notify_all`/`requeue_to`**：

[linux/futex_utils.h:107-119](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/linux/futex_utils.h#L107-L119) `notify_one` 用 `FUTEX_WAKE[_PRIVATE]`、wake_limit=1。[L120-L132](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/linux/futex_utils.h#L120-L132) `notify_all` 把 wake_limit 设成 `numeric_limits<int>::max()`。[L133-L158](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/linux/futex_utils.h#L133-L158) `requeue_to` 根据 `oldval` 是否提供在 `CMP_REQUEUE`（带比较，避免 ABA）与 `REQUEUE` 间选择，把等待者整体搬到另一个 futex——这是条件变量 `broadcast` 高效唤醒的关键（避免「叫醒后又立刻去抢另一把锁」的惊群）。

#### 4.2.4 代码实践

**实践目标**：验证 `Futex` 在「值已变」时 `wait` 立即返回、不阻塞。

**操作步骤**：

1. 读 [test/src/__support/threads/futex_utils_test.cpp:15-37](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/src/__support/threads/futex_utils_test.cpp#L15-L37) 的 `RequeueSmokeTest`。它构造 `source(1)`、`destination(2)`，然后对 `source.requeue_to(destination, ...)` 做几组断言，容忍 `ENOSYS`（某些内核/配置不支持 requeue）。
2. 在脑中（或本地）写一段最小实验（**示例代码**，非项目原有）：

   ```cpp
   // 示例代码：演示 wait 在值不匹配时立即返回
   LIBC_NAMESPACE::Futex f(1);
   // expected=5，但 f 当前是 1，wait 进内核前 load() != 5，立即返回 0，不会阻塞
   auto r = f.wait(5);
   // r.has_value() == true, *r == 0
   ```

3. 解释：为什么即使没有别的线程调 `notify`，这段代码也不会卡住？

**需要观察的现象**：`wait` 的预检查 [L54-L55](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/linux/futex_utils.h#L54-L55) 让「值不匹配」时直接返回，根本不进 `syscall`。

**预期结果**：你能说明「值已变」是 `wait` 的正常返回路径之一（对应内核侧的 `-EAGAIN`），不是错误。本实践含本地编写片段，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：`Futex::wait` 进内核前已经 `load()` 检查过值，为什么内核里还要再比对一次 `expected`？这不是重复吗？

**答案**：不是重复，是为了关闭「检查—等待」之间的竞态窗口。用户态 `load` 之后、`syscall` 之前，可能恰好有解锁方把值改了并已发过 `wake`。若无内核侧的二次比对，等待方会「错过那次 wake、又睡了」，永久挂起。内核在 `FUTEX_WAIT` 入口处用原子指令再比对一次 `expected`，若不符直接返回 `-EAGAIN`，确保「wake 一定有等待者可叫、或等待方根本没睡」。

**练习 2**：`Futex` 为什么用 `FUTEX_WAIT_BITSET` 而不是简单的 `FUTEX_WAIT`？

**答案**：`FUTEX_WAIT_BITSET` 支持**绝对超时**与**可选 `FUTEX_CLOCK_REALTIME`**（[L50-L53](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/linux/futex_utils.h#L50-L53)）。`RawMutex::timed_lock`、`pthread_cond_timedwait` 都需要「在某个绝对时刻超时」的语义；普通 `FUTEX_WAIT` 只支持相对超时，且时钟不可选。`FUTEX_BITSET_MATCH_ANY` 让 bitset 变体退化为「匹配所有位」，等价于普通 wait，没有副作用。

### 4.3 线程对象：Thread

#### 4.3.1 概念说明

锁解决了「多个线程如何安全访问共享数据」，而 `Thread` 解决的是「**如何创建、等待、回收一个线程**」。它对应 POSIX 的 `pthread_create`/`pthread_join`/`pthread_detach` 的内部实现。

`Thread` 的平台无关接口在 `thread.h`，Linux 的具体实现在 `linux/thread.cpp`。它的核心设计是：

- **线程本质是一次 `clone` 系统调用**。Linux 上创建线程就是 `clone(...)` 并传上一组 `CLONE_*` 标志，告诉内核「新任务和父任务共享内存、文件、信号处理、同属一个线程组」——共享的就是线程区别于进程的地方。
- **「线程结束」靠一个 futex 通知**。新线程创建时带 `CLONE_CHILD_CLEARTID` 标志和一个「clear_tid」地址；线程退出时内核自动把该地址清零并发起一次 `FUTEX_WAKE`。`join` 的本质就是「在这个 clear_tid futex 上 `wait`，直到被内核叫醒」。
- **`detach_state` 是个三态原子状态机**，协调 `join` 与 `detach`、回收时机的竞争。

#### 4.3.2 核心流程

**创建线程 `Thread::run`**（伪代码）：

```
run(style, runner, arg, stack, stacksize, guardsize, detached):
    if 没给 stack: mmap 分配一段 (stacksize + guardsize)，guard 区设 PROT_NONE（栈溢出即段错误）
    init_tls(tls)                        # 为新线程准备 TLS
    把 StartArgs + ThreadAttributes + Futex(clear_tid) 压到新栈顶
    clear_tid = CLEAR_TID_VALUE          # 非零初值
    clone(CLONE_VM|CLONE_THREAD|...|CLONE_CHILD_CLEARTID|CLONE_SETTLS, 新栈, &tid, &clear_tid, tls.tp)
    if 返回 0:                            # 子线程分支
        start_thread()                   # 读出栈上的 StartArgs，跑 runner，再 thread_exit
    else:                                 # 父线程分支
        return 0
```

**子线程入口 `start_thread`**：因为 `clone` 后子线程拿到的是空栈，父函数的局部变量都不可见，所以参数被预先压在栈上，子线程靠 `get_start_args_addr()`（按架构用 `__builtin_frame_address`）把它们「嗅探」出来，然后运行用户函数，最后 `thread_exit`。

**`thread_exit` 退出流程**：

```
thread_exit(retval, style):
    call_atexit_callbacks(attrib)        # 跑线程局部析构、TSS 析构（必须本线程自己跑）
    if CAS(detach: JOINABLE → EXITING) 成功:   # 有 joiner 在等
        SYS_exit(retval)                 # 内核退出时由 CLEARTID 自动 wake joiner
    else:                                # 已 detached
        cleanup_thread_resources(attrib) # 自己回收 TLS/栈
        SYS_set_tid_address(0)           # 阻止内核再 wake 不存在的 futex
        SYS_exit(0)
```

**`join`/`wait` 流程**：

```
wait():                                  # join 的等待部分
    clear_tid = attrib->platform_data
    while clear_tid.load() != 0:         # 内核退出时会把它清零
        clear_tid.wait(CLEAR_TID_VALUE, nullopt, is_shared=true)
                                         # 注意 is_shared=true：内核发的是 FUTEX_WAKE 而非 WAKE_PRIVATE
```

`detach_state` 三态状态机（[thread.h:61-65](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/thread.h#L61-L65)）：`JOINABLE`（可被 join）→ `EXITING`（正在退出，joiner 应等待清理）或 → `DETACHED`（无人 join，需自行回收）。`detach` 用 CAS 把 `JOINABLE` 改成 `DETACHED`；若 CAS 失败说明线程已 `EXITING`，detach 方代为清理。

#### 4.3.3 源码精读

**线程属性 `ThreadAttributes`**：

[thread.h:88-126](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/thread.h#L88-L126) 每个线程的运行期状态：`detach_state`（[L107](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/thread.h#L107)）、栈指针与尺寸、TLS 地址、线程 id `tid`、运行风格（POSIX 返回 `void*` 还是 STDC 返回 `int`）、`platform_data`（存 clear_tid futex）、`joiner`（[L119](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/thread.h#L119)，原子指针，用于检测重复/互斥 join）。注释 [L88-L87](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/thread.h#L81-L87) 强调它按架构 `STACK_ALIGNMENT` 对齐，因为它通常放在栈上。

**`Thread` 类的对外方法**：

[thread.h:152-248](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/thread.h#L152-L248) `run` 有两个重载（[L164-L172](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/thread.h#L164-L172) POSIX、[L174-L182](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/thread.h#L174-L182) STDC），都委托给 [L207-L208](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/thread.h#L207-L208) 的平台实现。`join`（[L184-L202](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/thread.h#L184-L202)）按风格取返回值。`wait` 的注释 [L219-L229](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/thread.h#L219-L229) 注明「仅供测试」。

[thread.h:250](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/thread.h#L250) `LIBC_THREAD_LOCAL Thread self`——每个线程的「自身」对象，`gettid`（[identifier.h:24-39](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/identifier.h#L24-L39)）正是优先读 `self.attrib->tid` 做缓存，避免每次都 `SYS_gettid`。

**clone 标志**：

[linux/thread.cpp:48-59](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/linux/thread.cpp#L48-L59) `CLONE_SYSCALL_FLAGS` 列出线程与进程的区别：`CLONE_VM`（共享内存）、`CLONE_FILES`（共享文件描述符表）、`CLONE_SIGHAND`（共享信号处理）、`CLONE_THREAD`（同线程组）、`CLONE_CHILD_CLEARTID`（退出时清 tid 地址并 wake）、`CLONE_SETTLS`（设线程指针）。

**`Thread::run` 的栈布局与 clone 调用**：

[linux/thread.cpp:250-295](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/linux/thread.cpp#L250-L295) 把 `StartArgs`+`ThreadAttributes`+`Futex(clear_tid)` 三件压到新栈顶（[L250-L251](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/linux/thread.cpp#L250-L251)），`clear_tid` 初值设为 `CLEAR_TID_VALUE`（[L292-L295](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/linux/thread.cpp#L292-L295)），并把它的地址记到 `attrib->platform_data`。

[linux/thread.cpp:301-320](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/linux/thread.cpp#L301-L320) 按架构传参顺序调用 `SYS_clone`，结果强制放进 `CLONE_RESULT_REGISTER`（x86_64 是 `rax`、aarch64 是 `x0`、riscv 是 `t0`）——因为子线程拿到全新空栈，本函数的栈变量对它不可见，必须靠寄存器分辨「我是父还是子」。

**子线程入口 `start_thread`**：

[linux/thread.cpp:180-197](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/linux/thread.cpp#L180-L197) 用 `get_start_args_addr()`（[L157-L178](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/linux/thread.cpp#L157-L178) 按架构用 `__builtin_frame_address`）从栈上嗅探参数，设 `self.attrib`，按 `style` 调 POSIX 或 STDC runner，最后 `thread_exit`。

**`wait` 在 clear_tid futex 上阻塞**：

[linux/thread.cpp:394-403](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/linux/thread.cpp#L394-L403) `wait` 取出 `platform_data` 里的 clear_tid futex，`while (load() != 0)` 循环 `wait(CLEAR_TID_VALUE, nullopt, true)`。注释 [L399-L400](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/linux/thread.cpp#L399-L400) 解释为何这里必须用 `is_shared=true`：内核 `CLONE_CHILD_CLEARTID` 触发的是 `FUTEX_WAKE`（非 PRIVATE），等待方若用 `FUTEX_WAIT_PRIVATE` 就匹配不上、永远等不到。

**`thread_exit` 与三态协作**：

[linux/thread.cpp:517-551](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/linux/thread.cpp#L517-L551) 先跑 atexit 回调（[L528](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/linux/thread.cpp#L528)），再 CAS `JOINABLE→EXITING`（[L530-L532](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/linux/thread.cpp#L530-L532)）：成功说明有 joiner，`SYS_exit` 让内核 `CLEARTID` 自动叫醒它；失败说明已 detached，自己清理（[L534](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/linux/thread.cpp#L534)）并 `set_tid_address(0)` 防止内核去 wake 一个不存在的 futex（[L538](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/linux/thread.cpp#L538)）。

#### 4.3.4 代码实践

**实践目标**：追踪「从 `clone` 到 `join` 返回」之间，clear_tid futex 的取值变化。

**操作步骤**：

1. 在 [linux/thread.cpp:292-295](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/linux/thread.cpp#L292-L295) 确认 clear_tid 初值为 `CLEAR_TID_VALUE`（非零，[L47](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/linux/thread.cpp#L47)）。
2. 在 [L394-L403](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/linux/thread.cpp#L394-L403) 看 `join` 方的 `wait`：它期望值是 `CLEAR_TID_VALUE`，循环条件是 `load() != 0`。
3. 在 [L546-L549](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/linux/thread.cpp#L546-L549) 看子线程 `SYS_exit`：由于创建时带了 `CLONE_CHILD_CLEARTID`（[L57-L58](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/linux/thread.cpp#L57-L58)），内核退出时自动把 clear_tid 地址**清零**并发 `FUTEX_WAKE`。
4. 串起来：clear_tid 经历 `CLEAR_TID_VALUE(非0) → 0(内核清零)`，join 方的 `wait` 被叫醒、`load()` 看到 0、跳出循环。

**需要观察的现象**：`join` 的等待完全建立在本讲的 `Futex::wait` 之上，且必须用 `is_shared=true`。

**预期结果**：你能画出 clear_tid 的取值时间线，并解释为什么 join 不需要应用层显式 `notify`——叫醒动作由内核借 `CLONE_CHILD_CLEARTID` 完成。本实践为源码阅读型，标注「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `Thread::wait` 要用 `while (load() != 0)` 循环，而不是「wait 一次就返回」？

**答案**：因为 `futex_wait` 可能有**伪唤醒**（spurious wakeup）——内核允许在没有 `wake` 的情况下叫醒等待者。`wait` 返回后必须重新检查条件（clear_tid 是否真被清零），若仍是 `CLEAR_TID_VALUE` 则继续等。注释 [L395-L397](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/linux/thread.cpp#L395-L397) 明确提到「spurious wake」。这是所有「futex + 条件」组合的标准用法（条件变量同理）。

**练习 2**：`thread_exit` 里，detached 分支为什么要 `SYS_set_tid_address(0)`？

**答案**：detached 线程在 [L534](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/linux/thread.cpp#L534) 已经自己释放了栈，而 clear_tid futex 恰好存在那块栈内存里。若不 `set_tid_address(0)`，内核退出时仍会按 `CLONE_CHILD_CLEARTID` 去写那个已释放的地址并 `FUTEX_WAKE`，造成 use-after-free。把 tid 地址清零，告诉内核「别再清这个地址了」。

### 4.4 pthread 映射：把内部原语暴露成 POSIX 接口

#### 4.4.1 概念说明

前面三节都是 `__support` 内部的私有工具。公开的 POSIX `pthread_*` 函数（入口点，回顾 [u2-l1](u2-l1-entrypoint-mechanism.md)）只是把它们**映射**出来。映射手法与 [u8-l3](u8-l3-stdio-file-model.md) 的 `FILE` 完全一致：

1. **公开不透明类型 ↔ 内部 C++ 类，靠 `reinterpret_cast` 互转**。如 `pthread_mutex_t*` 被直接重解释成 `Mutex*`。为保证二者布局完全相同，入口点用 `static_assert(sizeof(...) == sizeof(...) && alignof(...) == alignof(...))` 钉死。
2. **内部错误枚举 ↔ 公开 `errno`**。`Mutex` 返回 `MutexError`，入口点把它翻译成 `EDEADLK`/`EPERM` 等 `errno` 码返回。
3. **`Mutex`（unix_mutex.h）在 `RawMutex` 之上追加 POSIX 语义**。`RawMutex` 故意只做「加锁解锁」，递归、检错、owner 跟踪这些 POSIX 要求由 `Mutex` 用「`owner` 字段记录持锁 tid + `lock_count`」实现。

#### 4.4.2 核心流程

`pthread_mutex_lock` 的完整调用链（本讲规格指定的核心任务）：

```
pthread_mutex_lock(mutex)                       # src/pthread/pthread_mutex_lock.cpp
  → reinterpret_cast<Mutex*>(mutex)->lock()     # unix_mutex.h
      → lock_impl(do_lock):                     # 先查递归/检错（owner==self?）
          → do_lock(): RawMutex::lock(nullopt, pshared)   # raw_mutex.h
              → try_lock() [CAS UNLOCKED→LOCKED]          # 快路径，零 syscall
              → lock_slow():                              # 慢路径
                  → spin(100)
                  → exchange(IN_CONTENTION)
                  → Futex::wait(IN_CONTENTION, timeout, is_pshared)   # linux/futex_utils.h
                      → syscall_impl(FUTEX_SYSCALL_ID, ..., FUTEX_WAIT_BITSET)  # OSUtil
  → 若 DEADLOCK: return EDEADLK; 否则 return 0
```

解锁链 `pthread_mutex_unlock → Mutex::unlock → RawMutex::unlock → (若 IN_CONTENTION) Futex::notify_one → FUTEX_WAKE`。

`Mutex::lock_impl` 在调 `RawMutex` 之前先做 POSIX 检查：

```
lock_impl(do_lock):
    if 递归 且 owner==self:                  # 同线程再次加锁
        lock_count++（溢出返回 OVERFLOW）; return NONE
    if 检错 且 owner==self:                  # 同线程再次加锁但不允许
        return DEADLOCK
    res = do_lock()                           # 真正去抢 RawMutex
    if 成功: 记 owner=self（递归还记 lock_count=1）
    return res
```

#### 4.4.3 源码精读

**`Mutex`（unix_mutex.h）在 `RawMutex` 之上加 POSIX 语义**：

[unix_mutex.h:26-34](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/unix_mutex.h#L26-L34) `class Mutex final : private RawMutex`——**私有继承**，表示「我用 `RawMutex` 的实现，但不暴露它的接口」。位域 `recursive`/`robust`/`pshared`/`error_checking`/`priority_inherit` 编码属性；`owner`（[L37](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/unix_mutex.h#L37) `Atomic<pid_t>`）记录持锁线程，注释 [L36](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/unix_mutex.h#L36) 说明为何用 tid 而非 TLS 地址（fork 后 TLS 可能失效）。

[unix_mutex.h:43-65](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/unix_mutex.h#L43-L65) `lock_impl` 模板：先判递归/检错（[L45-L51](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/unix_mutex.h#L45-L51)），再调传入的 `do_lock`（[L53](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/unix_mutex.h#L53)），成功后登记 owner（[L55-L62](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/unix_mutex.h#L55-L62)）。`do_lock` 是个 lambda，由 `lock`/`try_lock`/`timed_lock` 分别提供（[L82-L90](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/unix_mutex.h#L82-L90) 的 `lock` 就传一个调 `RawMutex::lock(nullopt, pshared)` 的 lambda）。

[unix_mutex.h:101-121](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/unix_mutex.h#L101-L121) `unlock` 的递归计数递减与 owner 校验，最后才调 `RawMutex::unlock(pshared)`（[L118](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/unix_mutex.h#L118)）。

**`Mutex` 的平台/单线程分派**：

[mutex.h:15-74](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/mutex.h#L15-L74) 按 `LIBC_THREAD_MODE` 分派：`PLATFORM`（Linux/Darwin）包含 `unix_mutex.h`（[L43-L45](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/mutex.h#L43-L45)）；`SINGLE`（GPU/某些 baremetal，无真正并发）提供一个**直通 no-op 的 `Mutex`**（[L58-L66](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/mutex.h#L58-L66)）——注释 [L54-L56](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/mutex.h#L54-L56) 说明 GPU 上无法实现真锁，只要求「临界区里只有单线程在跑」。

**错误码枚举**：

[mutex_common.h:16-24](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/mutex_common.h#L16-L24) `MutexError`：`NONE`/`BUSY`/`DEADLOCK`/`TIMEOUT`/`UNLOCK_WITHOUT_LOCK`/`BAD_LOCK_STATE`/`OVERFLOW`，是内部错误到 `errno` 的中间层。

**入口点：`pthread_mutex_init`**：

[pthread_mutex_init.cpp:21-24](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/pthread/pthread_mutex_init.cpp#L21-L24) `static_assert(sizeof(Mutex) == sizeof(pthread_mutex_t) && alignof(...) == alignof(...))`——钉死布局一致，这是 `reinterpret_cast` 安全的前提。[L49-L50](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/pthread/pthread_mutex_init.cpp#L49-L50) 用 placement-new 在 `pthread_mutex_t` 内存里构造 `Mutex`。

**入口点：`pthread_mutex_lock`**：

[pthread_mutex_lock.cpp:20-27](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/pthread/pthread_mutex_lock.cpp#L20-L27) `reinterpret_cast<Mutex*>(mutex)->lock()`，把 `DEADLOCK` 翻成 `EDEADLK` 返回。这就是 4.4.2 调用链的入口。

**入口点：`pthread_mutex_unlock`**：

[pthread_mutex_unlock.cpp:22-30](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/pthread/pthread_mutex_unlock.cpp#L22-L30) `UNLOCK_WITHOUT_LOCK` 翻成 `EPERM`（POSIX 规定未持锁就解锁返回 `EPERM`）。

**入口点：`pthread_create`**：

[pthread_create.cpp:28-29](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/pthread/pthread_create.cpp#L28-L29) `static_assert(sizeof(pthread_t) == sizeof(Thread))`，[L80-L82](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/pthread/pthread_create.cpp#L80-L82) 把 `pthread_t` 重解释成 `Thread*` 并调 `thread->run(...)`。同样手法。

**入口点：`pthread_spin_lock`**：

[pthread_spin_lock.cpp:17-21](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/pthread/pthread_spin_lock.cpp#L17-L21) `static_assert` 钉死 `__lockword` 与 `SpinLock` 同尺寸对齐，[L30](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/pthread/pthread_spin_lock.cpp#L30) `reinterpret_cast<SpinLock*>(&lock->__lockword)`，还额外用 `__owner` 字段做自死锁检测（[L39-L40](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/pthread/pthread_spin_lock.cpp#L39-L40)）——这是入口点层在内部 `SpinLock` 之外补的 POSIX 语义。

**条件变量如何复用 `RawMutex` 的裸 futex**：

[CndVar.h:317](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/CndVar.h#L317) 与 [L323-L324](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/CndVar.h#L323-L324) `CndVar`（`RawMutex` 的 `friend`）通过 `mutex->get_raw_futex()`（[raw_mutex.h:128](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/raw_mutex.h#L128)）直接拿到 `Mutex` 内部的 futex 字，把等待者排到它上面——这正是「`pthread_cond_wait` 必须配 `pthread_mutex_t`」的底层原因：条件变量要复用互斥锁的 futex 字作为等待锚点。

#### 4.4.4 代码实践

**实践目标**：亲手画出从 `pthread_mutex_lock` 到 `futex` 系统调用的完整调用链（本讲规格指定的核心实践任务）。

**操作步骤**：

1. 从 [pthread_mutex_lock.cpp:21](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/pthread/pthread_mutex_lock.cpp#L21) 出发，记录第 1 跳：`reinterpret_cast<Mutex*>(mutex)->lock()`。
2. 跟到 [unix_mutex.h:82-90](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/unix_mutex.h#L82-L90) `Mutex::lock`，记录第 2 跳：`lock_impl` 内的 `RawMutex::lock(nullopt, pshared)`（[L87](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/unix_mutex.h#L87)）。
3. 跟到 [raw_mutex.h:108-116](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/raw_mutex.h#L108-L116)，记录第 3 跳：`try_lock()`（快路径）或 `lock_slow`（慢路径）。
4. 慢路径 [raw_mutex.h:86](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/raw_mutex.h#L86)，第 4 跳：`futex.wait(IN_CONTENTION, timeout, is_pshared)`。
5. [linux/futex_utils.h:83-91](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/linux/futex_utils.h#L83-L91)，第 5 跳：`syscall_impl(FUTEX_SYSCALL_ID, ..., FUTEX_WAIT_BITSET)`——这就是 u8-l1 的 OSUtil `syscall_impl`，最终发出陷阱指令进内核。
6. 用一张图把 5 跳连起来，并在每跳旁标注「这层加了什么」（入口点：errno 映射；Mutex：递归/检错/owner；RawMutex：三态/自旋；Futex：wait/wake 原语；syscall：进内核）。

**需要观察的现象**：链路上每一层职责单一、只加一种语义；最热的快路径（`try_lock` 成功）只到第 3 跳就返回，根本不触达 `Futex`/syscall。

**预期结果**：你能默写出这条 5 跳链路，并解释「无竞争时只有原子 CAS、零 syscall」。本实践为源码阅读型，标注「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `Mutex` 用 `private RawMutex`（私有继承）而不是 `public`？

**答案**：私有继承表示「**按实现编程**」（implementation in terms of）：`Mutex` 借用 `RawMutex` 的加锁解锁能力，但**不希望**外界把它当成 `RawMutex` 来用——`RawMutex` 的 `try_lock`/`spin` 等是实现细节。私有继承把这些细节对 `Mutex` 的使用者（入口点）隐藏，只通过 `Mutex` 自己定义的 `lock`/`unlock`/`try_lock`（返回 `MutexError`）对外。

**练习 2**：`pthread_mutex_init.cpp` 里的 `static_assert` 如果去掉会怎样？

**答案**：那将无法保证 `pthread_mutex_t`（公开类型，定义在生成的公共头里）与内部 `Mutex` 布局一致。一旦二者尺寸或对齐不同，`reinterpret_cast<Mutex*>(mutex)` 就会读到错位的内存，`lock`/`unlock` 操作错误的字段，导致数据损坏或崩溃。这个 `static_assert`（[L21-L24](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/pthread/pthread_mutex_init.cpp#L21-L24)）是把「不透明类型」模式钉死的安全栓。

---

## 5. 综合实践

把本讲四层串起来，做一个**「两个线程争一把锁」的端到端推演**，并把每一层的行为对上号。

**场景**：线程 A 先 `pthread_mutex_lock(&m)` 成功进入临界区；线程 B 紧接着也调 `pthread_mutex_lock(&m)`；A 在临界区里待一会儿后 `pthread_mutex_unlock(&m)`。

**任务**：请按时间顺序推演，并在每个关键点标注：① futex 字的取值（`UNLOCKED`/`LOCKED`/`IN_CONTENTION`）；② 当前停留在调用链的哪一跳（参考 4.4.4 的 5 跳）；③ 是否发生了 syscall。

参考推演（请先自己写，再对照）：

1. **A 调 `lock`**：经 `Mutex::lock`→`RawMutex::lock`→`try_lock` 的 CAS `UNLOCKED→LOCKED` 成功（[raw_mutex.h:102-107](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/raw_mutex.h#L102-L107)），`Mutex` 登记 `owner=A`。字=`LOCKED`，**无 syscall**。
2. **B 调 `lock`**：`try_lock` 的 CAS 失败（字是 `LOCKED`）→ `lock_slow`：先 `spin(100)` 仍见 `LOCKED` → `exchange(IN_CONTENTION)`（[L82-L84](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/raw_mutex.h#L82-L84)），交换前是 `LOCKED`（非 `UNLOCKED`），没抢到 → `futex.wait(IN_CONTENTION,...)` 发出第 1 个 syscall（`FUTEX_WAIT_BITSET`），B 阻塞。字=`IN_CONTENTION`。
3. **A 调 `unlock`**：`Mutex::unlock`→`RawMutex::unlock`，`exchange(UNLOCKED, RELEASE)`，旧值是 `IN_CONTENTION` → `wake()`→`futex.notify_one` 发出第 2 个 syscall（`FUTEX_WAKE`）（[raw_mutex.h:118-121](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/raw_mutex.h#L118-L121)）。字=`UNLOCKED`。
4. **B 被叫醒**：`wait` 返回 → `spin` → `exchange(IN_CONTENTION)`，交换前是 `UNLOCKED` → 抢到锁（[L82-L84](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/raw_mutex.h#L82-L84)），`Mutex` 登记 `owner=B`，`lock` 终于返回。字=`LOCKED`（注：B 抢到后写回 `LOCKED`，因为此时无其他等待者）。

**进阶**：对照 [raw_mutex_test.cpp:54-87](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/src/__support/threads/linux/raw_mutex_test.cpp#L54-L87) 的 `PSharedLock`——它用 `fork`（或 `clone`）造出第二个执行流，两边各 `lock`/`unlock` 一万次争同一把 `RawMutex`，最后断言 `data == 20000`，验证上述推演在跨进程（`is_shared=true`）下也成立。若条件允许，在 `lock_slow` 的 [L86](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/raw_mutex.h#L86) 前加一行调试日志（仅本地实验，勿提交），数一数一万次循环里到底发生了多少次 `futex_wait`——你会发现绝大多数轮次都被快路径 `try_lock` 直接消化了（标注「待本地验证」）。

## 6. 本讲小结

- **`SpinLock`（spin_lock.h）** 是最轻量的纯用户态锁：单字节 `flag`、TTAS 双层循环（外层 `exchange`、内层只 `load`），全程**零 syscall**，只适合极短临界区。
- **`RawMutex`（raw_mutex.h）** 是「自旋 + futex 阻塞」混合锁，核心是 `UNLOCKED`/`LOCKED`/`IN_CONTENTION` **三态字**：无竞争时 `try_lock` 一次 CAS 搞定、零 syscall；竞争时先自旋 100 次、赌不中再 `futex_wait` 阻塞；解锁时据旧值是否 `IN_CONTENTION` 决定要不要 `notify_one`。
- **`Futex`（linux/futex_utils.h）** 把一个 32 位原子整数 + 一条 `futex` 系统调用包成 `wait`/`notify_one`/`notify_all`/`requeue_to` 四个原语，用 `FUTEX_WAIT_BITSET` 支持绝对超时，靠「进内核前预检 + 内核二次比对」关闭丢唤醒竞态；它直接调 `syscall_impl`（裸调用），错误分类自己掌握。
- **`Mutex`（unix_mutex.h）** `private` 继承 `RawMutex`，在其上追加 POSIX 递归/检错/owner 跟踪（用 `Atomic<pid_t> owner` + `lock_count`）；`mutex.h` 再按 `LIBC_THREAD_MODE` 在真锁与 no-op 直通锁（GPU/baremetal）间分派。
- **`Thread`（thread.h + linux/thread.cpp）** 用 `clone`（带 `CLONE_VM`/`CLONE_THREAD`/`CLONE_CHILD_CLEARTID` 等）创建线程，靠 clear_tid futex 实现 `join`/`wait`，用三态 `detach_state` 状态机协调 join/detach/回收；子线程入口靠「栈上嗅探 StartArgs」拿到参数。
- **pthread 入口点** 全是 `reinterpret_cast` 薄壳：`pthread_mutex_t`↔`Mutex`、`pthread_t`↔`Thread`、`__lockword`↔`SpinLock`，靠 `static_assert` 钉死布局一致，内部 `MutexError` 翻译成 `errno` 返回——与 `FILE`（u8-l3）的手法完全同源。

## 7. 下一步学习建议

- 想看「锁之上」的高层原语，读 [`src/__support/threads/CndVar.h`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/CndVar.h)（条件变量）与 [`raw_rwlock.h`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/src/__support/threads/raw_rwlock.h)（读写锁）。它们都建立在 `RawMutex` 与 `Futex` 之上，可对照本讲的调用链理解 `pthread_cond_*`/`pthread_rwlock_*` 如何复用同一套底层。
- 想验证线程行为，跑集成测试目录 [`test/integration/src/__support/threads/`](https://github.com/llvm/llvm-project/blob/e7dd336e0f7884c34108a1e722205a16c3f5307b/libc/test/integration/src/__support/threads/)（如 `thread_detach_test.cpp`、`cndvar_test.cpp`、`futex_requeue_test.cpp`），它们覆盖了 `join`/`detach`/条件变量/requeue 的真实多线程场景。
- 回到 [u9-l1 内存分配器](u9-l1-memory-allocator.md) 思考：本讲学完后，如何用 `Mutex` 给 `FreeListHeap::allocate`/`free` 加锁把单线程堆变成线程安全堆？为什么 Scudo 自身的「每线程缓存」比「一把全局锁」更适合多线程 malloc。
- 若对 futex 系统调用本身的内核语义感兴趣，可结合 [u8-l1](u8-l1-osutil-linux-syscalls.md) 的 `syscall_impl`/`syscall_checked` 区分，思考为何 `Futex` 选用裸的 `syscall_impl` 而非 `syscall_checked`——这是「错误语义分层」的一个好例子。
