# 健壮锁的崩溃恢复

> 本讲是专家层（U8）第二篇。前置讲义 **u6-l2（跨进程健壮互斥量）** 已介绍过 `ipc::sync::mutex` 的三套后端与「健壮锁」「一致性恢复」等概念；本讲把镜头拉近，逐行拆解「持有者进程崩溃 → 下一个进程感知 → 恢复一致性」这条链路在源码里到底怎么落地，并补上 u6-l2 没展开的 `sync_obj_impl` 模板与「首次引用初始化」机制。

## 1. 本讲目标

学完本讲，你应当能够：

1. 说清楚**跨进程锁为什么必须是 robust**：普通锁一旦被持有者崩溃带走，会发生什么灾难。
2. 说清楚三套平台后端**各自如何感知「持有者已死」**：Linux 的 robust futex 与 `EOWNERDEAD`、POSIX 的 `PTHREAD_MUTEX_ROBUST`、Windows 的 `WAIT_ABANDONED`。
3. 说出拿到「濒死之锁」后**`consistent` 恢复一致性**的必要步骤，以及**不做恢复会怎样**（锁永久报废）。
4. 读懂 `sync_obj_impl.h` 这个**把锁对象直接放进共享内存**的模板，理解「谁负责 `new` 这把锁」的首次引用初始化与引用计数逻辑。
5. 能够动手复现「进程持锁时被 `kill`，下一个进程仍能正常获得锁」的场景。

## 2. 前置知识

本讲默认你已经掌握 u6-l2 的内容。为避免你来回翻阅，这里用最短的篇幅回顾几个关键点：

- **`ipc::sync::mutex`（公共门面）** 是一个 PIMPL 类，对外只暴露 `void* native()` 不透明句柄。真正的逻辑在 `ipc::detail::sync::mutex` 里，由编译期宏 `LIBIPC_OS_*` 分派到三套后端：Linux（a0/futex）、POSIX（pthread）、Windows（Win32 内核对象）。分派发生在 [src/libipc/sync/mutex.cpp:8-16](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/sync/mutex.cpp#L8-L16)。

- **命名互斥量（named mutex）**：同名即同一把锁。Linux/POSIX 把锁对象本身放进命名共享内存（多进程共享同一块内存、同一个对象）；Windows 用内核命名对象（`CreateMutex` 传名字）。

- **健壮锁（robust）**：当持有锁的进程/线程意外终止时，系统能把锁标记为「持有者已死」，让后续申请者有机会接管，而不是永久死锁。

- **一致性恢复（consistent）**：拿到「濒死之锁」的新主人，必须显式声明「我已把共享数据修复到一致状态」，系统才会把锁从濒死态拉回正常态。

- **两个名字空间**：`ipc::sync::*` 是 PIMPL 公共门面，`ipc::detail::sync::*` 是平台实现。

如果你对「共享内存句柄 `ipc::shm::handle` 的引用计数」还不熟，建议先翻 u5-l1：它把一个 4 字节原子计数器塞在共享内存末尾，`ref()` 返回当前持有该内存映射的进程/句柄数。本讲会反复用到这个 `ref()`。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [src/libipc/platform/linux/mutex.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/mutex.h) | Linux 后端：`robust_mutex` + `mutex` | `lock` 里 `EOWNERDEAD` → `consistent` → `unlock` → 重抢的循环；句柄缓存与引用计数 |
| [src/libipc/platform/posix/mutex.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/mutex.h) | POSIX 后端（FreeBSD/QNX） | `open` 里设置 `PTHREAD_MUTEX_ROBUST` 属性；`lock` 里 `EOWNERDEAD` → `pthread_mutex_consistent` |
| [src/libipc/platform/linux/sync_obj_impl.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/sync_obj_impl.h) | 把同步对象放进共享内存的模板 `obj_impl<SyncT>` | `acquire_handle`、`open` 的首次引用初始化 |
| [src/libipc/platform/linux/a0/mtx.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/a0/mtx.h) / [mtx.c](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/a0/mtx.c) | a0 自研 robust futex 互斥量（纯 C） | robust_list、`FUTEX_OWNER_DIED`、`a0_mtx_consistent`、`ENOTRECOVERABLE` |
| [src/libipc/platform/win/mutex.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/mutex.h) | Windows 后端（对照） | `WAIT_ABANDONED` 的处理 |
| [test/test_mutex.cpp](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/test/test_mutex.cpp) | 互斥量单元测试 | 实践参考 |

> 小贴士：Linux 用的「a0」是 libipc 内嵌的 AlephZero 纯 C 库（见 [src/libipc/platform/linux/a0/](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/a0/README.md)），它直接用 Linux 的 robust futex 系统调用实现了 robust 互斥量。本讲的「内核如何感知崩溃」全在它的 `.c` 文件里。

## 4. 核心概念与源码讲解

### 4.1 为什么跨进程锁必须「健壮」：robust 属性与 EOWNERDEAD

#### 4.1.1 概念说明

设想一个朴素场景：进程 A 拿到了一把保护共享内存的锁，正在修改数据；就在临界区执行到一半时，进程 A 被 `kill -9` 杀死（或段崩溃、或断电）。

- 如果这是一把**普通锁**，那么「谁持有锁」这一信息只记在锁对象里（比如某个 TID 字段）。进程 A 死了，但字段还写着 A 的 TID，锁永远处于「被占用」状态。此后任何进程想拿这把锁，都会**永久阻塞**——这就是跨进程死锁灾难。
- 如果这是一把**健壮锁（robust lock）**，操作系统会和持有者线程/进程绑定一条「健壮链表（robust list）」。当持有者异常终止时，内核会遍历这条链表，把每把它持有的锁打上「持有者已死」的标记。下一个申请者会被成功放行（拿到锁），同时被告知「前主人死了」，由它决定如何恢复。

三套后端的「死亡标记」各不相同，但对外都收敛成同一个语义信号：

| 平台 | 死亡标记机制 | 申请者收到的信号 |
|------|-------------|-----------------|
| Linux（a0/robust futex） | 内核对 futex 字打 `FUTEX_OWNER_DIED` 位 | `EOWNERDEAD` |
| POSIX（pthread robust） | `PTHREAD_MUTEX_ROBUST` 属性 + 内核 robust list | `pthread_mutex_lock` 返回 `EOWNERDEAD` |
| Windows（内核互斥对象） | 持有者进程终止 → 对象进入 abandoned 态 | `WaitForSingleObject` 返回 `WAIT_ABANDONED` |

注意 `EOWNERDEAD` / `WAIT_ABANDONED` 的含义很微妙：**锁已经到手了**，只是「不干净」。拿到锁的人必须负责修复，否则锁会报废（见 4.2）。

#### 4.1.2 核心流程

先以 Linux a0 为例，看清「内核感知崩溃」这一步在底层是怎么发生的（这是理解后续一切的基础）：

1. **线程登记健壮链表**：a0 在每个线程首次用锁时，调用 `syscall(SYS_set_robust_list, ...)` 把一个 `robust_list_head` 登记给内核，并约定「futex 字在结构体里的偏移」。
2. **持锁时挂入链表**：拿到锁后，a0 把这把锁的节点插到线程的健壮链表头（`robust_op_add`）。
3. **线程/进程死亡**：内核回收该线程时，遍历它的健壮链表，把每把仍被它持有的锁的 futex 字打上 `FUTEX_OWNER_DIED` 位。
4. **下一个申请者**：用 CAS 抢锁时会发现 `FUTEX_OWNER_DIED` 已置位，于是返回 `EOWNERDEAD` 而非正常 `OK`。

用伪代码描述 a0 的抢锁核心（简化自 [a0/mtx.c](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/a0/mtx.c)）：

```
function a0_mtx_timedlock_robust(mtx, timeout):
    tid = 本线程TID
    while 被信号中断循环:
        if 锁已不可恢复(ENOTRECOVERABLE): return ENOTRECOVERABLE
        if CAS(mtx.ftx, 0, tid):           # 无竞争，直接拿到
            return OK
        syserr = futex_lock_pi(mtx.ftx)     # 有竞争，进内核排队
    if syserr == 0:                          # 拿到了
        if mtx.ftx 标记了 OWNER_DIED:
            return EOWNERDEAD                # 拿到了，但前主人死了
        return OK
    return syserr
```

futex 字 `ftx` 的位编码（来自 [a0/mtx.c:163-177](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/a0/mtx.c#L163-L177)）：

```
 bit:  31................30.......................0
       [ 保留 ] [OWNER_DIED] [      owner TID (FUTEX_TID_MASK)      ]

 FTX_NOTRECOVERABLE = FUTEX_TID_MASK | FUTEX_OWNER_DIED   # 全置 1 = 永久报废
```

- 低 30 位（`FUTEX_TID_MASK`）：当前持有者线程的 TID。
- 第 30 位（`FUTEX_OWNER_DIED`）：持有者已死。
- 两者同时满足（即 `FTX_NOTRECOVERABLE`）：这把锁已经无法挽回，`lock` 会返回 `ENOTRECOVERABLE`。

#### 4.1.3 源码精读

**（a）内核健壮链表的登记与挂载（a0 底层）**

[src/libipc/platform/linux/a0/mtx.c:97-116](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/a0/mtx.c#L97-L116) 定义了线程本地的 `a0_robust_head`，并在线程首次用锁时通过 `set_robust_list` 系统调用登记给内核；其中 `futex_offset = offsetof(a0_mtx_t, ftx)` 告诉内核「锁结构里 futex 字在哪」——这是内核后续能打 `OWNER_DIED` 位的前提。

[src/libipc/platform/linux/a0/mtx.c:137-160](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/a0/mtx.c#L137-L160) 的 `robust_op_add`/`robust_op_del` 在持锁/放锁时把锁节点插/摘出健壮链表。这正是 `a0_mtx_t` 结构体第一字段必须是 `next` 指针的原因（见 [a0/mtx.h:34-38](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/a0/mtx.h#L34-L38) 的注释「The first field MUST be a next pointer」）。

**（b）抢锁返回 EOWNERDEAD（a0 底层）**

[src/libipc/platform/linux/a0/mtx.c:179-207](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/a0/mtx.c#L179-L207)：`a0_mtx_timedlock_robust` 在无竞争 CAS 成功或内核放行后，检查 `ftx_owner_died(...)`，是则返回 `EOWNERDEAD`。注意 L185-188 的 `ftx_notrecoverable` 早退——锁已报废时直接返回 `ENOTRECOVERABLE`，不再尝试。

**（c）libipc 层把 EOWNERDEAD 透传出来**

[src/libipc/platform/linux/mutex.h:25-56](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/mutex.h#L25-L56)：`robust_mutex::lock` 在一个 `for(;;)` 里调用 a0 的 `a0_mtx_lock` / `a0_mtx_timedlock`，用 `switch` 区分四种返回。`EOWNERDEAD` 分支（L38-50）就是本讲的「主战场」——它不是直接成功，而是进入恢复流程（详见 4.2）。

**（d）Windows 后端的对照：WAIT_ABANDONED**

[src/libipc/platform/win/mutex.h:62-82](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/mutex.h#L62-L82)：Windows 没有显式的 robust 属性，靠内核互斥对象自带的 abandoned 语义。`WAIT_ABANDONED`（L71-76）同样表示「拿到锁但前主人没正常释放」，libipc 的处理是先 `unlock()` 再循环重抢。Windows **没有** `consistent` 这一步（见 4.2 末尾的对比）。

#### 4.1.4 代码实践

**实践目标**：通过阅读源码，确认「`EOWNERDEAD` 是一个『已经拿到锁』的成功状态，而不是失败」。

**操作步骤**：

1. 打开 [a0/mtx.c:179-207](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/a0/mtx.c#L179-L207)，找到 L191 的 `if (a0_cas(&mtx->ftx, 0, tid)) return A0_OK;`——这说明无竞争时 CAS 直接写 TID 成功即持锁。
2. 找到 L196 的 `a0_ftx_lock_pi`——这是有竞争时进内核排队抢锁（PI = priority inheritance，优先级继承）。
3. 找到 L200-202：`if (ftx_owner_died(...)) return EOWNERDEAD;`——注意它出现在 `if (!syserr)`（即抢锁成功）的分支内，证明 **`EOWNERDEAD` 本质是抢锁成功、只是附带「前主人死亡」信息**。

**需要观察的现象 / 预期结果**：`EOWNERDEAD` 只在「锁已被本线程拿到」之后才会返回；它不是「抢锁失败」。这一认知是理解 4.2 恢复流程的前提。

#### 4.1.5 小练习与答案

**练习 1**：`a0_mtx_t` 的结构体为什么必须以 `next` 指针开头，而且「不可移动（immovable）」？

> **参考答案**：因为它「继承」自内核的 `robust_list` 节点，内核遍历健壮链表时按 `next/prev` 指针串接这些节点；节点地址必须稳定（不能被 move），否则内核持有的指针会变成悬垂指针。注释见 [a0/mtx.h:29-33](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/a0/mtx.h#L29-L33)。

**练习 2**：Windows 后端为什么不需要像 Linux 那样调用 `set_robust_list` 系统调用？

> **参考答案**：Windows 的互斥量是内核对象，内核天然跟踪每个对象的持有者进程；持有者进程终止时内核自动把对象置为 abandoned 态，应用层无需（也无法）手动登记健壮链表。

---

### 4.2 consistent：让濒死之锁重获一致性

#### 4.2.1 概念说明

拿到 `EOWNERDEAD` 之后，新主人面临一个问题：**前主人在临界区里改数据改到一半就死了**，共享数据可能处于不一致状态（比如只更新了一半的字段）。这把锁此刻处于「濒死态（inconsistent）」。

新主人有两种合法选择：

1. **修复共享数据，然后调用 `consistent`**：告诉系统「我已经把数据修好了」，系统清除「濒死」标记，锁恢复正常可用。
2. **直接 `unlock`（放弃恢复）**：相当于承认「我不知道怎么修」。系统会把这把锁标记为**永久不可恢复（`ENOTRECOVERABLE`）**，此后任何人再 `lock` 都会失败——这是「宁可锁死也不让数据损坏」的安全保护。

所以 `consistent` 不是可有可无的仪式：**不调用 `consistent` 就 `unlock`，锁会永久报废。**

#### 4.2.2 核心流程

libipc 三套后端的恢复路径**并不一致**，这是本讲的要点之一：

- **Linux（a0）**：拿到 `EOWNERDEAD` → 调 `a0_mtx_consistent` 清除 `OWNER_DIED` 位 → 调 `a0_mtx_unlock` 释放 → **`break` 回到循环顶端重新抢锁**，第二次拿到时返回 `OK`（即「释放后重抢」，等价于拿到一把全新干净的锁）。
- **POSIX（pthread）**：拿到 `EOWNERDEAD` → 调 `pthread_mutex_consistent` → **直接返回 `true` 持有锁**（不释放、不重抢）。
- **Windows**：无 `consistent` 概念。`WAIT_ABANDONED` → 先 `unlock()` → 循环重抢。

底层 a0 的 `consistent` 做的事很简单——把 `ftx` 字里的 `FUTEX_OWNER_DIED` 位清掉，见 [a0/mtx.c:286-303](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/a0/mtx.c#L286-L303)：

```
function a0_mtx_consistent(mtx):
    val = mtx.ftx
    if 未标记 OWNER_DIED: return EINVAL    # 没坏，没必要修
    if 当前持有者不是我:    return EPERM     # 不是你的锁，你没资格修
    mtx.ftx &= ~FUTEX_OWNER_DIED           # 清掉濒死位
    return OK
```

而「不恢复就 unlock 会报废」的机制藏在 [a0/mtx.c:305-343](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/a0/mtx.c#L305-L343) 的 `a0_mtx_unlock` 里：若发现 `ftx_owner_died(val)` 仍为真（说明持有者拿到 `EOWNERDEAD` 后没调 `consistent` 就来 unlock），就把新值设成 `FTX_NOTRECOVERABLE`，锁从此报废。

#### 4.2.3 源码精读

**（a）Linux：consistent → unlock → 重抢**

[src/libipc/platform/linux/mutex.h:38-50](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/mutex.h#L38-L50) 是 `robust_mutex::lock` 的 `EOWNERDEAD` 分支：

```cpp
case EOWNERDEAD: {
        int eno2 = A0_SYSERR(a0_mtx_consistent(native())); // ① 清濒死位
        if (eno2 != 0) { ... return false; }
        int eno3 = A0_SYSERR(a0_mtx_unlock(native()));     // ② 释放
        if (eno3 != 0) { ... return false; }
    }
    break; // loop again                                   // ③ 回到 for(;;) 顶端重抢
```

注意三个动作的顺序：`consistent`（修复标记）→ `unlock`（释放锁）→ `break`（重新进循环抢锁）。第二次抢锁时 `ftx` 已干净，返回 `OK`，`lock` 返回 `true`。

**（b）POSIX：consistent 后直接持有**

[src/libipc/platform/posix/mutex.h:224-235](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/mutex.h#L224-L235) 是 POSIX 后端 `lock` 的 `EOWNERDEAD` 分支：

```cpp
case EOWNERDEAD: {
        // EOWNERDEAD means we have successfully acquired the lock,
        // but the previous owner died. We need to make it consistent.
        int eno2 = ::pthread_mutex_consistent(mutex_);
        if (eno2 != 0) { ... return false; }
        // After calling pthread_mutex_consistent(), the mutex is now in a
        // consistent state and we hold the lock. Return success.
        return true;                                      // 直接持有，不释放
    }
```

POSIX 走的是「consistent 后直接持锁」，省去了一次释放重抢。两段注释也讲清了语义。注意它依赖 `open` 时设置过 `PTHREAD_MUTEX_ROBUST` 属性（见 4.2 实践里的精读点）。

**（c）POSIX 如何让锁具备 robust 能力**

[src/libipc/platform/posix/mutex.h:135-143](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/mutex.h#L135-L143) 在初始化时设置两个关键属性：

```cpp
::pthread_mutexattr_setpshared(&mutex_attr, PTHREAD_PROCESS_SHARED); // 跨进程共享
...
::pthread_mutexattr_setrobust(&mutex_attr, PTHREAD_MUTEX_ROBUST);    // 健壮属性
```

没有这两行，`pthread_mutex_lock` 永远不会返回 `EOWNERDEAD`，也就谈不上崩溃恢复。

**（d）底层 consistent 与「不修就报废」**

[src/libipc/platform/linux/a0/mtx.c:286-303](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/a0/mtx.c#L286-L303) 是 `a0_mtx_consistent`，核心就是 L300 的 `a0_atomic_and_fetch(&mtx->ftx, ~FUTEX_OWNER_DIED)`。

[src/libipc/platform/linux/a0/mtx.c:321-324](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/a0/mtx.c#L321-L324) 在 `a0_mtx_unlock` 里：

```c
uint32_t new_val = 0;
if (ftx_owner_died(val)) {     // 拿到 EOWNERDEAD 却没 consistent 就来 unlock
    new_val = FTX_NOTRECOVERABLE;  // → 永久报废
}
```

这就是「不恢复就报废」的铁证。

#### 4.2.4 代码实践

**实践目标**：通过对比源码，彻底搞清三套后端恢复路径的差异。

**操作步骤**：

1. 在 [linux/mutex.h:38-50](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/mutex.h#L38-L50) 数一下 Linux 恢复用了几个动作（答案是 3 个：consistent、unlock、loop）。
2. 在 [posix/mutex.h:224-235](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/mutex.h#L224-L235) 数一下 POSIX 恢复用了几个动作（答案是 1 个：consistent 后直接 return true）。
3. 在 [win/mutex.h:71-76](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/mutex.h#L71-L76) 看 Windows 的 `WAIT_ABANDONED` 是否调用了 consistent（答案：没有，直接 unlock + 重抢）。
4. 在 [a0/mtx.c:321-324](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/a0/mtx.c#L321-L324) 确认「不修就报废」。

**需要观察的现象 / 预期结果**：三套后端对外都表现为「持有者崩溃后下一个申请者仍能拿到锁」，但内部路径不同。Linux 是「释放重抢」、POSIX 是「修完直持」、Windows 是「直接重抢无修复」。这是跨平台移植时最容易踩坑的差异点。

#### 4.2.5 小练习与答案

**练习 1**：Linux 后端在 `EOWNERDEAD` 后为什么要 `unlock` 再重新抢锁，而不是像 POSIX 那样直接持锁返回？

> **参考答案**：这是 a0/libipc 的实现选择：`a0_mtx_consistent` 清掉 `OWNER_DIED` 位后，当前线程仍记为持有者（ftx 里 TID 仍是自己）；libipc 选择 `unlock` 后回到循环顶端重新 CAS/`futex_lock_pi`，第二次拿到时 ftx 是「干净」状态（无 `OWNER_DIED`），等价于获得一把全新锁。两套写法语义等价（都成功持锁），只是路径不同。源码见 [linux/mutex.h:38-50](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/mutex.h#L38-L50)。

**练习 2**：假设你拿到 `EOWNERDEAD` 后直接 `unlock` 而不调 `consistent`，下一次别的进程 `lock` 会发生什么？

> **参考答案**：a0 的 `unlock` 会因 `ftx_owner_died` 仍为真而把锁置为 `FTX_NOTRECOVERABLE`（[a0/mtx.c:321-324](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/a0/mtx.c#L321-L324)）；之后任何进程 `lock` 都会在 `a0_mtx_timedlock_robust` 的 L185-188 早退返回 `ENOTRECOVERABLE`，这把锁永久不可用。POSIX 下等价行为是返回 `ENOTRECOVERABLE`。

---

### 4.3 sync_obj_impl：把锁对象放进共享内存的模板

#### 4.3.1 概念说明

前面两节都在讲「锁的逻辑」，但有一个基础问题没回答：**这把锁对象本身，放在哪里，才能被多个进程共享？**

答案在 [src/libipc/platform/linux/sync_obj_impl.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/sync_obj_impl.h)。它提供了一个模板 `obj_impl<SyncT>`，把任意同步对象类型 `SyncT`（比如 `a0_mtx_t`）**直接当成共享内存里的一段字节**来管理。

关键思路：用 `ipc::shm::handle` 申请一块 `sizeof(SyncT)` 大小的命名共享内存，把这块内存的起始地址 `reinterpret_cast` 成 `SyncT*`。于是多个进程映射同一块共享内存，就等于共享同一个 `SyncT` 对象——这就是「跨进程锁」的物理基础。

#### 4.3.2 核心流程

`obj_impl<SyncT>` 只做四件事：

1. **`acquire_handle(name)`**：调 `shm_.acquire(name, sizeof(SyncT))` 申请共享内存，返回强转后的 `SyncT*`。
2. **`open(name)`**：先 `acquire_handle`；若 `shm_.ref() > 1`（已有别的进程打开过），直接复用；否则（首次）把这块内存清零初始化（见 4.4）。
3. **`close()`**：`shm_.release()` 释放本进程的映射引用，把指针置空。
4. **`clear()` / `clear_storage()`**：强制清理共享内存对象本身。

层叠关系（Linux）：

```
ipc::sync::mutex            (公共 PIMPL 门面)
   └─ detail::sync::mutex   (持有 robust_mutex* + 引用计数 + curr_prog 句柄缓存)
        └─ robust_mutex     (lock/try_lock/unlock 的 EOWNERDEAD 恢复逻辑)
             └─ obj_impl<a0_mtx_t>  (把 a0_mtx_t 放进 sizeof(a0_mtx_t) 的共享内存)
```

#### 4.3.3 源码精读

**（a）模板骨架与成员**

[src/libipc/platform/linux/sync_obj_impl.h:12-28](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/sync_obj_impl.h#L12-L28)：

```cpp
template <typename SyncT>
class obj_impl {
protected:
    ipc::shm::handle shm_;        // 共享内存 RAII 句柄
    sync_t *h_ = nullptr;         // 指向共享内存里的 SyncT 对象

    sync_t *acquire_handle(char const *name) {
        if (!shm_.acquire(name, sizeof(sync_t))) {   // 申请 sizeof(sync_t) 字节
            ...return nullptr;
        }
        return static_cast<sync_t *>(shm_.get());    // 起始地址当 SyncT* 用
    }
```

注意 `shm_.acquire(name, sizeof(sync_t))`——申请的大小就是同步对象的大小，不多不少。`shm_.get()` 返回这块内存的起始指针，强转成 `sync_t*`。于是「同一块共享内存 = 同一个 `a0_mtx_t` 对象」。

**（b）robust_mutex 继承它**

[src/libipc/platform/linux/mutex.h:23-24](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/mutex.h#L23-L24)：`class robust_mutex : public sync::obj_impl<a0_mtx_t>`——`robust_mutex` 直接继承 `obj_impl<a0_mtx_t>`，于是它天然持有一块装着 `a0_mtx_t` 的共享内存，`native()` 返回的 `a0_mtx_t*` 就指向共享内存里的那把锁。

**（c）close / clear 的语义差别**

[src/libipc/platform/linux/sync_obj_impl.h:62-74](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/sync_obj_impl.h#L62-L74)：`close()` 只 `shm_.release()`（本进程礼貌退场，不动对象本身），`clear()` 调 `shm_.clear()`（强制清理存储）。这与 u5-l1 讲过的 `release` / `clear` / `clear_storage` 三档语义一致。

> 注意：POSIX 后端 [posix/mutex.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/mutex.h) **没有**用 `obj_impl` 模板，而是自己直接持有一个 `ipc::shm::handle *shm_` 和 `pthread_mutex_t *mutex_`，手动把 `pthread_mutex_t` 放进 `sizeof(pthread_mutex_t)` 的共享内存（见 [posix/mutex.h:50-71](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/mutex.h#L50-L71)）。思路完全相同，只是没抽成模板。

#### 4.3.4 代码实践

**实践目标**：验证「锁对象的大小 = 共享内存的大小」这一关系。

**操作步骤**：

1. 读 [sync_obj_impl.h:21-28](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/sync_obj_impl.h#L21-L28)，确认 `shm_.acquire(name, sizeof(sync_t))` 用的就是对象大小。
2. 读 [a0/mtx.h:34-38](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/a0/mtx.h#L34-L38)，数一下 `a0_mtx_s` 有几个字段（`next`、`prev`、`ftx`）。
3. 在本地写一行（**示例代码，非项目原有**）：`printf("%zu\n", sizeof(a0_mtx_t));` 打印实际大小。

**需要观察的现象 / 预期结果**：`sizeof(a0_mtx_t)` 是一个很小的值（3 个字段/指针级别），libipc 为每个命名互斥量申请的共享内存就这么大。这解释了为什么「共享内存 = 锁对象」开销极低。实际字节数**待本地验证**（受指针宽度影响）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `obj_impl` 用 `static_cast<sync_t*>(shm_.get())` 而不是 `new sync_t`？

> **参考答案**：因为这块内存是 `shm_.acquire` 从共享内存里划出来的（由 `mmap` 等系统调用提供），已经是可用的存储；`static_cast` 只是把这个起始地址「解释」成 `sync_t*`。如果用 `new`，会在进程私有堆上分配，别的进程就看不到这把锁了。共享内存里的对象只能用 placement 构造或零初始化，不能用普通 `new`。

**练习 2**：`a0_mtx_t` 结构体里为什么没有锁的状态字段（如「是否上锁」）？

> **参考答案**：锁状态全部编码在 `ftx` 这一个 32 位字段里（TID + OWNER_DIED 位等），由 futex 子系统解读。`next`/`prev` 是给内核健壮链表用的。所以 `a0_mtx_s` 三个字段里，`ftx` 一个就承担了全部锁语义。

---

### 4.4 首次引用初始化与引用计数：谁负责 new 这把锁

#### 4.4.1 概念说明

现在最后一个问题：多个进程同时 `open("my_lock")`，**谁来初始化这把锁？** 如果每个人都初始化一遍，后开的会把先开的覆盖，锁就乱了。

libipc 的方案是「**首次引用初始化 + 引用计数**」：

- 共享内存末尾有一个 4 字节原子计数器 `acc_`（u5-l1 讲过），`shm::handle::ref()` 返回当前有多少个句柄映射了这块内存。
- **第一个** `open` 它的进程（`ref()` 还很小时）负责把锁对象初始化（清零或 `init`）。
- 后续 `open` 的进程看到 `ref()` 已 > 1（说明别人建好了），**直接复用**，不再初始化。
- 每来一个 `open`，引用计数 `+1`；每来一个 `close`，`-1`；归零时才真正销毁/清理。

#### 4.4.2 核心流程

两套后端的「首次初始化」判定略有不同，但都基于 `ref()`：

- **Linux（`obj_impl::open`）**：`shm_.ref() > 1` → 复用；否则 `*h_ = A0_EMPTY`（零初始化 `a0_mtx_t`）。
- **POSIX（`mutex::open`）**：`shm_->ref() > 1 || self_ref > 0` → 复用；否则执行 `pthread_mutex_init`（带 robust 属性）。

为什么 POSIX 多判一个 `self_ref > 0`？因为 POSIX 后端还做了**进程内句柄去重**（`curr_prog` 缓存）：同一个进程多次 `open` 同名锁，会复用同一个底层句柄，只在第一次真正初始化。

#### 4.4.3 源码精读

**（a）Linux obj_impl 的首次引用初始化**

[src/libipc/platform/linux/sync_obj_impl.h:50-60](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/sync_obj_impl.h#L50-L60)：

```cpp
bool open(char const *name) noexcept {
    close();
    if ((h_ = acquire_handle(name)) == nullptr) return false;
    if (shm_.ref() > 1) {        // 已有别的进程打开过 → 复用
        return true;
    }
    *h_ = A0_EMPTY;              // 我是第一个 → 零初始化
    return true;
}
```

`A0_EMPTY` 是 [a0/empty.h:6-7](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/a0/empty.h#L6-L7) 定义的宏，在 C++ 里展开成 `{}`，即「值初始化/零初始化」。所以第一个进程把 `a0_mtx_t` 的 `next/prev/ftx` 全清 0——`ftx=0` 正是「未上锁」状态。

**（b）POSIX 的首次初始化与 robust 属性**

[src/libipc/platform/posix/mutex.h:115-150](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/mutex.h#L115-L150) 的 `open`：

```cpp
auto self_ref = ref_->fetch_add(1, std::memory_order_relaxed);
if (shm_->ref() > 1 || self_ref > 0) {
    return valid();              // 已被别的进程或本进程建过 → 复用
}
::pthread_mutex_destroy(mutex_); // 先清掉可能的残留
...
::pthread_mutexattr_setrobust(&mutex_attr, PTHREAD_MUTEX_ROBUST); // robust
*mutex_ = PTHREAD_MUTEX_INITIALIZER;
::pthread_mutex_init(mutex_, &mutex_attr);  // 真正初始化
```

注意判定条件 `shm_->ref() > 1 || self_ref > 0`：前者表示「跨进程已有别人」，后者表示「本进程内已有别的句柄」；只要满足任一，就跳过初始化。真正执行 `pthread_mutex_init` 的只有「跨进程首个 + 本进程首个」的那个句柄。

**（c）引用计数与 curr_prog 句柄缓存（Linux）**

[src/libipc/platform/linux/mutex.h:99-139](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/mutex.h#L99-L139) 的 `mutex` 类用 `curr_prog` 做进程内去重：`mutex_handles` 是一个 `map<名字, shm_data>`，`acquire_mutex` 先查表，没有才新建。`shm_data` 里同时存 `robust_mutex mtx` 和 `atomic<int32> ref`。

[src/libipc/platform/linux/mutex.h:176-196](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/mutex.h#L176-L196) 的 `open` / `close`：

```cpp
bool open(char const *name) noexcept {
    ...
    acquire_mutex(name);            // 查/建 curr_prog 缓存
    ...
    ref_->fetch_add(1, ...);        // 进程内引用计数 +1
    return true;
}
void close() noexcept {
    ...
    release_mutex(mutex_->name(), [this] {
        return ref_->fetch_sub(1, ...) <= 1;  // -1，归零时返回 true 触发擦除
    });
    ...
}
```

这样，同一进程内多次 `open` 同名锁，底层只建一个 `robust_mutex`（即一块共享内存），靠 `ref_` 计数管理生命周期；计数归零才从 `curr_prog` 表里擦除。

**（d）初始化时机为何要避免静态初始化顺序问题**

[src/libipc/platform/linux/mutex.h:159-162](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/mutex.h#L159-L162) 的 `init()` 注释明确说：「Avoid exception problems caused by static member initialization order order」——它只是触发 `curr_prog::get()` 这一个 Meyers 单例的首次构造，确保后续 `open` 时单例已就绪。POSIX 后端 [posix/mutex.h:96-100](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/mutex.h#L96-L100) 的 `init()` 同理。

#### 4.4.4 代码实践

**实践目标**：用单元测试理解「同名锁 = 同一把锁、引用计数管理生命周期」。

**操作步骤**：

1. 打开 [test/test_mutex.cpp:344-358](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/test/test_mutex.cpp#L344-L358) 的 `ReopenAfterClose`：先 `open` → `close` → 再 `open` 同名，验证可重复使用。
2. 读 [test/test_mutex.cpp:361-399](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/test/test_mutex.cpp#L361-L399) 的 `NamedMutexInterThread`：两个线程各自构造同名 `mutex`，验证它们保护的是同一把锁（`shared_data` 不会被打乱）。
3. 若你已构建测试（`LIBIPC_BUILD_TESTS=ON`），运行：`ctest -R MutexTest --output-on-failure`。

**需要观察的现象 / 预期结果**：两个线程用同名 `mutex` 保护的临界区互斥生效（`shared_data == 200`，无竞态）。这间接证明同名锁指向共享内存里的同一把锁对象。具体测试输出**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：Linux 的 `obj_impl::open` 用 `shm_.ref() > 1` 判定「是否首个」，为什么阈值是 1 而不是 0？

> **参考答案**：因为 `acquire_handle` 内部已经调过 `shm_.acquire`，此刻本进程的映射引用计数已经 `+1`。所以「只有我」时 `ref()` 刚好是 1；「还有别人」时 `ref() > 1`。阈值 1 正是「本进程自己」与「本进程+别人」的分界。

**练习 2**：POSIX 后端为什么在判定里多加一个 `self_ref > 0`？

> **参考答案**：因为 POSIX 后端有进程内 `curr_prog` 句柄缓存与本地引用计数 `ref_`。同一进程第二次 `open` 同名锁时，共享内存的 `shm_->ref()` 可能仍是 1（别的进程还没来），但本进程的 `self_ref` 已 > 0，说明本进程内部已经建过这个句柄，应复用而非重复 `pthread_mutex_init`。源码见 [posix/mutex.h:121-124](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/mutex.h#L121-L124)。

---

## 5. 综合实践：进程持锁被 kill，下一个进程如何接管

这是本讲规格里要求的核心实践：**追踪一个进程持锁时被 `kill` 的场景，说明下一个获取锁的进程如何感知并恢复互斥量状态。** 下面给出一个可在 Linux 上复现的多进程实验设计。

### 5.1 实践目标

亲手验证「健壮锁的崩溃恢复」端到端生效：持有者被强杀后，申请者不会永久阻塞，而是通过 `EOWNERDEAD → consistent → 重抢` 拿到锁。

### 5.2 操作步骤

**步骤 1：准备一个「持锁者」程序（示例代码，非项目原有）**

下面这个小程序打开一把命名锁 `my_robust_lock`，加锁后长时间睡眠，模拟「持锁做长任务」。把它保存为 `holder.cpp`：

```cpp
// holder.cpp —— 示例代码
#include "libipc/mutex.h"
#include <cstdio>
#include <thread>
#include <chrono>
int main() {
    ipc::sync::mutex mtx("my_robust_lock");
    if (!mtx.valid()) { std::printf("open fail\n"); return 1; }
    mtx.lock();                              // 拿到锁
    std::printf("[holder %d] locked, sleeping... kill me now\n", getpid());
    std::fflush(stdout);
    while (true) std::this_thread::sleep_for(std::chrono::seconds(1)); // 持锁不释放
    return 0;
}
```

**步骤 2：准备一个「申请者」程序（示例代码）**

```cpp
// waiter.cpp —— 示例代码
#include "libipc/mutex.h"
#include <cstdio>
int main() {
    ipc::sync::mutex mtx("my_robust_lock");
    if (!mtx.valid()) { std::printf("open fail\n"); return 1; }
    std::printf("[waiter %d] trying to lock (will recover if holder died)...\n", getpid());
    bool ok = mtx.lock(5000);                // 5 秒超时
    std::printf("[waiter %d] lock result = %d\n", getpid(), ok);
    if (ok) mtx.unlock();
    return 0;
}
```

**步骤 3：编译并复现**（前提：已按 u1-l2 用 `LIBIPC_BUILD_DEMOS`/或手动链接 `ipc` 库构建好 libipc）

```bash
g++ -std=c++17 holder.cpp -I<libipc>/include -L<libipc>/build/lib -lipc -lpthread -o holder
g++ -std=c++17 waiter.cpp -I<libipc>/include -L<libipc>/build/lib -lipc -lpthread -o waiter

# ① 启动持锁者（后台）
./holder &
HOLDER_PID=$!

# ② 等 holder 打印 "locked" 后，强杀它（模拟崩溃，锁没释放）
kill -9 $HOLDER_PID

# ③ 立即启动申请者
./waiter
```

### 5.3 需要观察的现象 / 预期结果

- **普通（非健壮）锁的对照组**（脑补）：`waiter` 会一直阻塞到 5 秒超时，`lock result = 0`（失败），因为锁永远被「死人」占着。
- **libipc 健壮锁的预期**：内核在 `kill -9` 回收 holder 线程时，遍历健壮链表，把这把锁的 `ftx` 打上 `FUTEX_OWNER_DIED`；`waiter` 的 `mtx.lock` 在底层 a0 收到 `EOWNERDEAD`，经 [linux/mutex.h:38-50](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/mutex.h#L38-L50) 的 `consistent → unlock → 重抢` 后很快返回 `true`。预期输出 `lock result = 1`。

> ⚠️ 实际运行输出（精确耗时、日志行）**待本地验证**。上述「申请者最终成功获得锁」的语义结论可由源码直接推出，是确定的；多进程 `kill` 时序带来的细节差异需在你本机观察。

### 5.4 源码阅读型补充（无法运行多进程时的替代方案）

如果你的环境不便跑多进程，可改做「调用链追踪」：

1. 假设 holder 持锁时被 `kill -9`。内核回收其线程 → 遍历 `a0_robust_head` 健壮链表（[a0/mtx.c:97-105](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/a0/mtx.c#L97-L105) 登记的链表）→ 对锁的 `ftx` 置 `FUTEX_OWNER_DIED`。
2. waiter 调 `mtx.lock` → [mutex.cpp:73-75](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/sync/mutex.cpp#L73-L75) → [linux/mutex.h:25-56](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/mutex.h#L25-L56) 的 `robust_mutex::lock` → a0 `a0_mtx_lock` → [a0/mtx.c:200-202](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/a0/mtx.c#L200-L202) 返回 `EOWNERDEAD`。
3. `switch` 命中 `EOWNERDEAD`（[linux/mutex.h:38](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/mutex.h#L38)）→ `a0_mtx_consistent` 清濒死位（[a0/mtx.c:300](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/a0/mtx.c#L300)）→ `a0_mtx_unlock`（[a0/mtx.c:305](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/a0/mtx.c#L305)）→ `break` 重抢 → 第二次返回 `OK` → `lock` 返回 `true`。

把这条链路画成时序图，你就完整复现了「崩溃 → 感知 → 恢复」全过程。

## 6. 本讲小结

- 跨进程锁**必须 robust**：持有者崩溃时，普通锁会永久死锁；健壮锁靠内核的 robust 机制把锁标记为「持有者已死」，让申请者有机会接管。
- 三套后端的死亡信号不同但语义一致：Linux a0 是 `FUTEX_OWNER_DIED`→`EOWNERDEAD`，POSIX 是 `PTHREAD_MUTEX_ROBUST`→`EOWNERDEAD`，Windows 是 `WAIT_ABANDONED`。`EOWNERDEAD`/`WAIT_ABANDONED` 表示**锁已到手、只是不干净**。
- 拿到濒死之锁后**必须 `consistent`**：Linux 路径是 `consistent → unlock → 重抢`，POSIX 是 `consistent → 直接持锁`，Windows 无此步骤直接重抢。**不调 `consistent` 就 `unlock` 会让锁永久报废（`ENOTRECOVERABLE`）。**
- `sync_obj_impl.h` 的 `obj_impl<SyncT>` 模板把锁对象（如 `a0_mtx_t`）**直接放进 `sizeof(SyncT)` 的命名共享内存**，多进程映射同一块内存即共享同一把锁——这是跨进程锁的物理基础。
- **首次引用初始化 + 引用计数**解决「谁负责建锁」：用共享内存末尾的原子计数 `ref()` 判定「我是不是第一个」，只有首个打开者才清零/`init` 锁对象，后续都复用；进程内还用 `curr_prog` 缓存 + `ref_` 计数做句柄去重。

## 7. 下一步学习建议

- **u8-l3（intrusive_stack 与 id_pool 无锁结构）**：`curr_prog` 的句柄表与 central cache 用到的无锁结构是下一步的自然延伸，你会看到 CAS 栈与空闲链表如何支撑本讲提到的资源管理。
- **回头精读 a0 库**：[src/libipc/platform/linux/a0/mtx.c](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/a0/mtx.c) 还实现了 robust 条件变量（`a0_cnd_*`，用 `futex_requeue_pi`），是 u6-l3「condition 与 semaphore」在 Linux 上的底层实现，值得对照阅读。
- **结合 u6-l4（detail::waiter）**：waiter 内部的 `mutex` 正是本讲的健壮锁，把它和 `condition` 组合起来，你就看懂了 channel 跨进程通知的完整安全模型——即便某端崩溃，通知机制也不会永久卡死。
