# 跨进程健壮互斥量

## 1. 本讲目标

本讲聚焦 libipc 的跨进程互斥量 `ipc::sync::mutex`，核心问题是：**当持有锁的进程在持锁期间崩溃（被 `kill -9`、段错误、掉电），其他进程还能不能继续拿到锁？**

普通互斥量在这种场景下会永久死锁——崩溃的进程再也不会调用 `unlock`，锁永远卡住。libipc 的 `mutex` 是**健壮（robust）**的：操作系统能感知到持有者已死，把锁交还给下一个等待者，并让后者有机会把状态「修复一致」。

学完本讲你应该能够：

- 说清楚「把互斥量放进共享内存 + 给个名字」为什么能让多个进程共用同一把锁。
- 区分三种死亡检测机制：Linux 的 robust futex（`EOWNERDEAD`）、POSIX(pthread) 的 `PTHREAD_MUTEX_ROBUST`（`EOWNERDEAD`）、Windows 的 abandoned mutex（`WAIT_ABANDONED`）。
- 解释 `consistent`（标记一致）这一步的作用，以及「不修复就释放」会让锁永久不可用（`ENOTRECOVERABLE`）的原因。
- 理解 Linux/POSIX 后端为何要用一张「进程本地句柄缓存 + 引用计数」的 `map`，而 Windows 后端不需要。

本讲承接 u6-l1：u6-l1 讲的是**进程内**用户态自旋锁（`spin_lock`/`rw_lock`），本讲讲的是**跨进程**内核态健壮锁，二者是不同层级。

## 2. 前置知识

### 2.1 进程内锁 vs 跨进程锁

- `std::mutex`、`spin_lock`、`rw_lock` 都是**进程内**锁：它们保护的是同一进程内多个线程的临界区，锁对象本身就在进程私有内存里。
- 跨进程锁要保护的是**多个进程**共享的数据（比如 libipc 的共享内存队列）。这要求锁对象本身也躺在共享内存里，所有进程映射同一块内存、看到同一把锁。

### 2.2 为什么普通锁跨进程会死锁

假设进程 A 拿到锁后正在修改共享数据，此时进程 A 崩溃了：

- 普通锁：A 永远不会执行 `unlock`，锁状态停留在「已占用」。进程 B 再去 `lock` 会**永远阻塞**——没有任何机制告诉它「持有者已经不在了」。
- 健壮锁：内核（或运行时）维护着「谁持有这把锁」的信息，能在持有者进程消亡时**主动改写锁状态**并唤醒等待者，让等待者拿到锁的同时收到一个「上一个主人死了」的信号。

### 2.3 一致性恢复（consistent）

健壮锁只是把锁「交给你」，并不能替你修复共享数据可能处于的半修改状态（崩溃发生在写一半）。拿到「死亡锁」的进程有责任：

1. 判断共享数据是否还能救（通常做法是丢弃或重新初始化）。
2. 调用 `consistent` 显式声明「我已经把状态收拾好了，这把锁可以正常继续用」。

**关键惩罚**：如果你拿到死亡锁后**没有**调用 `consistent` 就直接 `unlock`，系统会把这把锁标记为「不可恢复」（Linux/POSIX 返回 `ENOTRECOVERABLE`），之后**任何人**都再也锁不上了——这是一种「宁可废掉也不能用脏数据」的保护。

### 2.4 本讲会用到的前置概念

- 共享内存句柄 `ipc::shm::handle`、嵌入式跨进程引用计数（u5-l1）。
- PIMPL 不透明句柄 `handle_t`/`pimpl`（u2-l3）。
- 命名对象：Linux 下是 `/dev/shm` 里的文件 + `shm_open`；Windows 下是内核命名对象（可加 `Global\` 前缀跨会话，见 u5-l4）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| [include/libipc/mutex.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mutex.h) | 公共 API：`ipc::sync::mutex` 类声明，PIMPL 隐藏实现 |
| [src/libipc/sync/mutex.cpp](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/sync/mutex.cpp) | 编译期后端分派 + 把公共调用转发给 `detail::sync::mutex` |
| [src/libipc/platform/linux/mutex.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/mutex.h) | Linux 后端：基于内嵌 a0 库的 robust futex |
| [src/libipc/platform/linux/a0/mtx.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/a0/mtx.h) | a0 互斥量结构 `a0_mtx_t` 与 C 接口声明 |
| [src/libipc/platform/linux/a0/mtx.c](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/a0/mtx.c) | a0 互斥量实现：robust-list 注册、futex PI、死亡检测 |
| [src/libipc/platform/linux/sync_obj_impl.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/sync_obj_impl.h) | `obj_impl<SyncT>` 模板：把同步对象放进共享内存的通用骨架 |
| [src/libipc/platform/posix/mutex.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/mutex.h) | POSIX 后端（QNX/FreeBSD）：基于 `pthread_mutex_t` 的 ROBUST 属性 |
| [src/libipc/platform/win/mutex.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/mutex.h) | Windows 后端：`CreateMutex` + `WaitForSingleObject` 的 abandoned 检测 |

一句话定位：`mutex.h`（公共门面）→ `mutex.cpp`（按 OS 选后端）→ `linux/posix/win` 三个同名 `mutex.h`（各自实现），其中 Linux 的真正干活者是 `a0/mtx.c`。

## 4. 核心概念与源码讲解

### 4.1 命名互斥量与共享内存

#### 4.1.1 概念说明

「命名互斥量」=「一个有名字的锁对象」。这个名字是跨进程的契约：进程 A 用名字 `"my_lock"` 打开，进程 B 也用 `"my_lock"` 打开，底层就映射到**同一把锁**。

libipc 的做法分两类：

- **Linux / POSIX**：互斥量对象（`a0_mtx_t` / `pthread_mutex_t`）**直接躺在共享内存里**。名字对应一块共享内存（`/dev/shm` 下的文件），互斥量就是这块内存开头的那几个字节。谁映射这块内存，谁就摸得到这把锁。
- **Windows**：互斥量是**内核命名对象**，由 `CreateMutex(name)` 创建/打开。内核自己维护对象的生命周期，不需要 libipc 自己把它放进共享内存。

公共 API 在三者之上做了统一抽象，定义在 [include/libipc/mutex.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mutex.h)：

```cpp
class LIBIPC_EXPORT mutex {
public:
    mutex();
    explicit mutex(char const *name);
    bool open(char const *name) noexcept;
    void close() noexcept;
    void clear() noexcept;
    static void clear_storage(char const * name) noexcept;

    bool lock(std::uint64_t tm = ipc::invalid_value) noexcept;
    bool try_lock() noexcept(false); // std::system_error
    bool unlock() noexcept;
private:
    class mutex_;
    mutex_* p_;   // PIMPL
};
```

注意几个设计要点（[include/libipc/mutex.h:12-39](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mutex.h#L12-L39)）：

- `lock` 的超时默认是 `ipc::invalid_value`（无限等待，见 u2-l1）。
- `try_lock` 失败时**抛 `std::system_error`**（和 `std::mutex::lock` 一致），而不是返回 bool——这是与 `lock` 的关键区别。
- 资源清理三件套与 u2-l3 同构：`close`（礼貌断连）、`clear`（强制释放）、`clear_storage`（按名字扫掉磁盘残留）。

#### 4.1.2 核心流程

后端分派发生在 [src/libipc/sync/mutex.cpp](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/sync/mutex.cpp)，靠编译期宏 `LIBIPC_OS_*` 选一份头文件包含进来（与 u5-l2 的 shm 后端分派完全同构）：

```text
LIBIPC_OS_WIN            → platform/win/mutex.h     (CreateMutex)
LIBIPC_OS_LINUX          → platform/linux/mutex.h   (a0 robust futex)
LIBIPC_OS_QNX/FREEBSD    → platform/posix/mutex.h   (pthread ROBUST)
```

公共 `ipc::sync::mutex` 只是个 PIMPL 壳，它内部持有一个 `detail::sync::mutex lock_`，所有调用都转发过去（[src/libipc/sync/mutex.cpp:21-83](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/sync/mutex.cpp#L21-L83)）。三份后端都叫 `ipc::detail::sync::mutex`，签名一致，差异被隔离在各自的 `.h` 里。

#### 4.1.3 源码精读

后端分派的关键代码（[src/libipc/sync/mutex.cpp:8-16](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/sync/mutex.cpp#L8-L16)）——注意它和 shm 的分派规则**不完全一样**：Linux 走 a0 专属后端，而 QNX/FreeBSD 走 pthread 后端：

```cpp
#if defined(LIBIPC_OS_WIN)
#include "libipc/platform/win/mutex.h"
#elif defined(LIBIPC_OS_LINUX)
#include "libipc/platform/linux/mutex.h"
#elif defined(LIBIPC_OS_QNX) || defined(LIBIPC_OS_FREEBSD)
#include "libipc/platform/posix/mutex.h"
#else
#   error "Unsupported platform."
#endif
```

Linux 后端把 a0 互斥量塞进共享内存，靠的是模板 `obj_impl<a0_mtx_t>`（[src/libipc/platform/linux/sync_obj_impl.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/sync_obj_impl.h)）。这个模板是「把任意同步对象放进共享内存」的通用骨架：

- 持有一个 `ipc::shm::handle shm_`（共享内存句柄）和一个 `sync_t *h_`（指向共享内存里的对象）。
- `acquire_handle` 申请 `sizeof(sync_t)` 大小的共享内存并返回首指针（[sync_obj_impl.h:21-28](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/sync_obj_impl.h#L21-L28)）。
- `open` 里有一个**「首次引用才初始化」**的关键判断（[sync_obj_impl.h:50-60](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/sync_obj_impl.h#L50-L60)）：

```cpp
bool open(char const *name) noexcept {
    close();
    if ((h_ = acquire_handle(name)) == nullptr) return false;
    if (shm_.ref() > 1) return true;   // 别人(或自己之前)已经映射过了，跳过初始化
    *h_ = A0_EMPTY;                    // 我是第一个，零初始化这把锁
    return true;
}
```

`shm_.ref()` 是 u5-l1 讲过的嵌入式跨进程引用计数：`> 1` 说明这块共享内存已经被别的进程（或本进程别的句柄）映射并初始化过了，当前进程不能再清零它，否则会把别人正在用的锁抹掉。`A0_EMPTY` 就是零初始化（`{}`，见 `a0/empty.h`）。

> 注意：Linux 后端有**两层**「首次引用」机制——这里是**跨进程**那一层（`shm_.ref()`）；4.5 节会讲**进程内**那一层（`curr_prog` 的 `ref_`）。别混淆。

#### 4.1.4 代码实践

**实践目标**：确认「同名 = 同一把锁」的契约，并理解 PIMPL 转发。

**操作步骤**：

1. 打开 [include/libipc/mutex.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/mutex.h)，确认公共类只有 `open/lock/try_lock/unlock/close/clear/clear_storage` 这几个方法，没有任何暴露锁内部结构的成员。
2. 打开 [src/libipc/sync/mutex.cpp:73-83](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/sync/mutex.cpp#L73-L83)，确认 `lock/try_lock/unlock` 都是一行转发 `impl(p_)->lock_.xxx()`。
3. 写一个两进程的最小程序（示例代码，非项目原有）：

```cpp
// 示例代码：mutex_holder.cpp 与 mutex_waiter.cpp 共用名字 "demo_mtx"
#include "libipc/mutex.h"
int main() {
    ipc::sync::mutex m;
    m.open("demo_mtx");   // 同名即同一把锁
    if (m.lock()) {       // 默认无限等待
        // ... 临界区 ...
        m.unlock();
    }
}
```

**需要观察的现象**：两个进程都用 `"demo_mtx"` 打开后，第二个进程的 `lock()` 会阻塞，直到第一个 `unlock()`。

**预期结果**：进程交替进入临界区。具体运行行为**待本地验证**（需要先按 u1-l2 构建出库）。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ipc::sync::mutex` 要用 PIMPL（`mutex_* p_`）而不是直接 `#include` 平台头？

**答案**：因为 `detail::sync::mutex` 在三个平台是三个完全不同的类（一个含 `HANDLE`、一个含 `a0_mtx_t*`、一个含 `pthread_mutex_t*`），成员和大小都不同。PIMPL 把这个差异藏在 `.cpp` 里，公共头 `mutex.h` 不依赖任何平台类型，ABI 稳定，用户代码一次编写三平台编译。

**练习 2**：`obj_impl::open` 里 `if (shm_.ref() > 1) return true;` 这行删掉会怎样？

**答案**：每个新映射的进程都会把共享内存首部的 `a0_mtx_t` 清零，破坏正在持锁的进程的状态（锁的所有者 TID 被抹掉），导致锁逻辑彻底错乱。这行是「只让第一个到达者初始化」的保护。

---

### 4.2 Linux a0 健壮锁：robust futex 与 EOWNERDEAD

#### 4.2.1 概念说明

Linux 后端不用 `pthread_mutex_t`，而是用 libipc 内嵌的 a0（AlephZero）纯 C 库自研的健壮互斥量 `a0_mtx_t`。原因在 a0 头文件的注释里写得很清楚（[src/libipc/platform/linux/a0/mtx.h:19-33](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/a0/mtx.h#L19-L33)）：它等价于一个固定了若干标志的 pthread mutex（进程共享、健壮、错误检查、优先级继承），但用 `CLOCK_BOOTTIME` 计时（不受系统调时影响，更适合 IPC 超时）。

a0 健壮锁的死亡检测依赖 **Linux 内核的 robust futex 机制**，核心是两件事：

1. **robust list（健壮链表）**：每个线程在内核里登记一张「我当前持有的 robust 锁」链表。线程/进程死亡时，内核会遍历这张链表，把链表上每把锁的 futex 字标记上 `FUTEX_OWNER_DIED` 位。
2. **futex 字（ftx）**：锁里那一个 32 位整数，编码了「谁持有 / 是否已死 / 有无等待者」。

`a0_mtx_t` 的结构（[a0/mtx.h:34-38](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/a0/mtx.h#L34-L38)）正是按 robust list 的硬性要求设计的：

```c
struct a0_mtx_s {
  a0_mtx_t* next;   // 必须是第一个字段：robust list 节点指针
  a0_mtx_t* prev;
  a0_ftx_t  ftx;    // futex 字：编码 TID / OWNER_DIED / WAITERS
};
```

注释强调两点约束：**第一个字段必须是 `next` 指针**（内核靠它遍历链表）；**必须有一个 futex**（这让对象不可移动，所以它得固定躺在共享内存里）。

futex 字 `ftx` 是一个 32 位整数，按位编码（低 30 位是持有者 TID，第 30 位是 `OWNER_DIED`）：

\[ \text{ftx} = \underbrace{\text{TID}}_{\text{低 30 位，FUTEX\_TID\_MASK}}\ \big|\ \underbrace{\text{OWNER\_DIED}}_{\text{第 30 位}}\ \big|\ \underbrace{\text{WAITERS}}_{\text{第 31 位}} \]

- TID = 0 表示未上锁；非 0 表示持有者的线程号。
- `OWNER_DIED` 位被内核置位 = 持有者已死、锁被「遗赠」给下一个获取者。
- 解码函数见 [a0/mtx.c:162-177](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/a0/mtx.c#L162-L177)：`ftx_tid` 取低 30 位、`ftx_owner_died` 测第 30 位、`ftx_notrecoverable` 判断是否已永久报废（`FUTEX_TID_MASK | FUTEX_OWNER_DIED` 同时成立）。

#### 4.2.2 核心流程

**注册 robust list（每个线程首次用锁时做一次）**：

```text
线程首次拿锁 → init_thread()
  → robust_init()
    → 把 a0_robust_head 注册进内核: syscall(SYS_set_robust_list, ...)
    → 记录 futex 在结构里的偏移: futex_offset = offsetof(a0_mtx_t, ftx)
```

之后该线程每拿一把 a0 锁，就把这把锁挂到自己的 `a0_robust_head` 链表上（`robust_op_add`）；释放时摘下来（`robust_op_del`）。这样线程一旦死亡，内核就能顺着链表把所有它持有的锁都标上 `OWNER_DIED`。

**加锁与死亡检测**（`a0_mtx_timedlock_robust`，[a0/mtx.c:179-207](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/a0/mtx.c#L179-L207)）：

```text
1. 若 ftx 已 notrecoverable → 直接返回 ENOTRECOVERABLE（锁已报废）
2. CAS 尝试把 ftx 从 0 改成自己的 TID → 成功则拿到锁
3. 否则调内核 futex PI 排队等待: a0_ftx_lock_pi
4. 内核返回后，若 ftx 带 OWNER_DIED 位 → 返回 EOWNERDEAD
   （此时锁已经在你手里，但提示「上一个主人死了」）
```

**修复一致性**（`a0_mtx_consistent`，[a0/mtx.c:286-303](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/a0/mtx.c#L286-L303)）：用原子 AND 清掉 `OWNER_DIED` 位，声明「状态已收拾好」。

**报废惩罚**（`a0_mtx_unlock`，[a0/mtx.c:305-343](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/a0/mtx.c#L305-L343)）：如果解锁时发现 ftx 还带着 `OWNER_DIED`（说明拿到死亡锁的人没调 consistent 就想跑），就把 ftx 写成 `FTX_NOTRECOVERABLE`，这把锁从此谁都锁不上。

#### 4.2.3 源码精读

libipc 的 C++ 包装层是 `robust_mutex`（继承 `obj_impl<a0_mtx_t>`），它处理 a0 返回的错误码。最核心的是 `lock` 里对 `EOWNERDEAD` 的处理（[src/libipc/platform/linux/mutex.h:25-56](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/mutex.h#L25-L56)）：

```cpp
bool lock(std::uint64_t tm) noexcept {
    if (!valid()) return false;
    for (;;) {
        auto ts = linux_::detail::make_timespec(tm);
        int eno = A0_SYSERR((tm == invalid_value) ? a0_mtx_lock(native())
                                                  : a0_mtx_timedlock(native(), {ts}));
        switch (eno) {
        case 0:           return true;     // 正常拿到
        case ETIMEDOUT:   return false;    // 超时
        case EOWNERDEAD: {
                int eno2 = A0_SYSERR(a0_mtx_consistent(native())); // ① 修复一致
                if (eno2 != 0) { ...return false; }
                int eno3 = A0_SYSERR(a0_mtx_unlock(native()));    // ② 先释放
                if (eno3 != 0) { ...return false; }
            }
            break; // loop again                          // ③ 重新去抢一次干净的锁
        default: ...return false;
        }
    }
}
```

注意 libipc 在 Linux 上的恢复策略是三步：**①`consistent` 修复 → ②`unlock` 释放 → ③循环重新 `lock`**。它没有在 `EOWNERDEAD` 那一次直接返回成功持锁，而是先把「死亡锁」修好、放掉，再去抢一把「干净」的锁（下一轮命中 `case 0` 返回 true）。这是一种偏保守的实现——保证交给用户的永远是状态正常的锁。

`A0_SYSERR` 宏把 a0 的错误码统一翻译成 `errno` 值（`0`/`ETIMEDOUT`/`EOWNERDEAD`/`ENOTRECOVERABLE`…），所以上层能像处理标准 `pthread` 错误码一样 `switch`。

#### 4.2.4 代码实践

**实践目标**：跟踪「持有者崩溃 → 下一个进程感知并恢复」的完整路径。

**操作步骤**：

1. 读 [a0/mtx.c:99-116](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/a0/mtx.c#L99-L116)，确认 robust list 是 thread-local 的（`A0_THREAD_LOCAL robust_list_head_t a0_robust_head`），且 `init_thread` 用 `pthread_once` 保证每个线程只注册一次。
2. 读 [a0/mtx.c:179-207](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/a0/mtx.c#L179-L207)，回答：拿到锁后是通过什么判断「上一个主人死了」？（提示：第 200 行 `ftx_owner_died`。）
3. 读 [a0/mtx.c:317-324](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/a0/mtx.c#L317-L324)，回答：解锁时如果发现锁还带着 `OWNER_DIED` 位，会发生什么？

**需要观察的现象**：进程 A 持锁时被 `kill -9`，进程 B 的 `lock()` 不再永久阻塞，而是内部经历一次 `EOWNERDEAD → consistent → unlock → 重锁`，最终返回 true。

**预期结果**：B 能正常拿到锁并继续。注意 [a0/mtx.c:345](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/a0/mtx.c#L345) 有一行 `TODO: Handle ENOTRECOVERABLE`——目前 `ENOTRECOVERABLE`（锁永久报废）尚未被上层专门处理，会落到 `lock` 的 `default` 分支返回 false。**待本地验证**实际崩溃恢复行为。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `a0_mtx_t` 的第一个字段必须是 `next` 指针？

**答案**：这是 Linux 内核 robust-list 协议的硬性要求。内核遍历线程的 robust 链表时，默认把每个节点的起始地址当作「指向下一个节点的指针」来解引用。只有 `next` 在偏移 0 处，内核才能正确走链。a0 头文件注释（[mtx.h:29-31](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/a0/mtx.h#L29-L31)）也明确写了这条约束。

**练习 2**：libipc 在 Linux 上拿到 `EOWNERDEAD` 后为什么不直接返回 true 持锁，而要 `consistent → unlock → 重锁`？

**答案**：为了让交给用户的锁状态「干净」。`EOWNERDEAD` 表示锁虽在你手里，但上一个主人死在临界区中途，共享数据可能不一致。libipc 选择先 `consistent` 把锁标记为可用、再 `unlock` 主动放掉，然后循环重新竞争一次。这样用户拿到的永远是 `case 0`（正常获取）的锁，把「死亡恢复」的细节完全藏在库内部。

---

### 4.3 POSIX pthread 健壮锁：ROBUST 属性

#### 4.3.1 概念说明

QNX 和 FreeBSD（以及任何有 pthread robust 支持的 POSIX 系统）走的是标准 `pthread_mutex_t`，靠两个属性把它变成跨进程健壮锁：

- `PTHREAD_PROCESS_SHARED`：允许这把锁被不同进程共享（默认是 `PTHREAD_PROCESS_PRIVATE`，跨进程用是未定义行为）。
- `PTHREAD_MUTEX_ROBUST`：开启健壮语义——持有者死亡时，下一个 `pthread_mutex_lock` 返回 `EOWNERDEAD` 而不是永久阻塞。

这和 a0 的本质机制是同源的（glibc/musl 的 pthread robust 底层也是 robust futex），区别在于 pthread 把「robust list 注册、futex 编码、consistent」这些细节封装进了标准 API，libipc 直接调用即可。

#### 4.3.2 核心流程

**初始化**（首个引用者执行）：销毁旧锁 → 设属性（SHARED + ROBUST）→ `pthread_mutex_init`。

**加锁与死亡检测**：`pthread_mutex_lock` / `pthread_mutex_timedlock` 返回 `EOWNERDEAD` 表示「锁给你了，但上一个主人死了」。

**恢复**：`pthread_mutex_consistent(mutex)` 标记一致。**注意：与 Linux a0 不同，这里恢复后直接返回 true 持锁**，不再 unlock 重抢。

#### 4.3.3 源码精读

初始化代码在 [src/libipc/platform/posix/mutex.h:115-150](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/mutex.h#L115-L150)，关键几行：

```cpp
if ((eno = ::pthread_mutexattr_setpshared(&mutex_attr, PTHREAD_PROCESS_SHARED)) != 0) { ... }
if ((eno = ::pthread_mutexattr_setrobust(&mutex_attr, PTHREAD_MUTEX_ROBUST)) != 0) { ... }
*mutex_ = PTHREAD_MUTEX_INITIALIZER;
if ((eno = ::pthread_mutex_init(mutex_, &mutex_attr)) != 0) { ... }
```

这里的「首个引用者」判断是 `shm_->ref() > 1 || self_ref > 0`（[posix/mutex.h:122-124](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/mutex.h#L122-L124)）：跨进程引用计数 `shm_->ref() > 1`（别的进程已映射）或进程内引用 `self_ref > 0`（本进程已有人初始化过），都说明锁已就绪，直接 `return valid()` 跳过初始化。

加锁的死亡检测（[src/libipc/platform/posix/mutex.h:211-241](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/mutex.h#L211-L241)）：

```cpp
case EOWNERDEAD: {
    // EOWNERDEAD means we have successfully acquired the lock,
    // but the previous owner died. We need to make it consistent.
    int eno2 = ::pthread_mutex_consistent(mutex_);
    if (eno2 != 0) { ...return false; }
    // After calling pthread_mutex_consistent(), the mutex is now in a
    // consistent state and we hold the lock. Return success.
    return true;   // ← 与 Linux a0 不同：直接持锁返回
}
```

注释清楚说明了语义：`EOWNERDEAD` 时**锁已经成功获取**，只需 `consistent` 修复后即可正常使用。与 4.2 节 a0 后端的「unlock 后重抢」形成对照——同一套概念（robust + consistent），两个后端的恢复策略不同。

还有一个 FreeBSD 专属细节在 `close()` 里（[posix/mutex.h:159-173](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/mutex.h#L159-L173)）：销毁 robust mutex 前要先 `pthread_mutex_unlock`，因为 FreeBSD 维护**每线程的 robust list**，如果在锁还挂着时 `pthread_mutex_destroy`，会留下悬垂指针导致后续段错误。注释专门解释了这个坑。

#### 4.3.4 代码实践

**实践目标**：对比 pthread 后端与 a0 后端在「拿到死亡锁后」的行为差异。

**操作步骤**：

1. 并排打开 [linux/mutex.h:38-50](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/mutex.h#L38-L50)（a0）和 [posix/mutex.h:224-235](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/mutex.h#L224-L235)（pthread）的 `EOWNERDEAD` 分支。
2. 在两张表里分别填：①是否调用 consistent？②是否调用 unlock？③之后是 return 还是 loop？

**需要观察的现象**：a0 后端三步走（consistent→unlock→重抢），pthread 后端两步走（consistent→直接返回持锁）。

**预期结果**：两套后端对外都「最终给用户一把可用的锁」，但内部路径不同。这属于源码阅读型实践，**待在 QNX/FreeBSD 环境验证**实际崩溃恢复。

#### 4.3.5 小练习与答案

**练习**：`PTHREAD_PROCESS_SHARED` 和 `PTHREAD_MUTEX_ROBUST` 分别解决什么问题？只设 robust 不设 shared 行不行？

**答案**：`SHARED` 解决「跨进程可见」（让放在共享内存里的锁能被多进程正确操作，默认 private 跨进程是 UB）；`ROBUST` 解决「持有者崩溃不死锁」。只设 robust 不设 shared 不行——锁在跨进程场景下行为未定义，robust 检测可能根本不生效。两者必须同时设置，[posix/mutex.h:135-142](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/mutex.h#L135-L142) 两行缺一不可。

---

### 4.4 Windows abandoned 互斥量：WAIT_ABANDONED

#### 4.4.1 概念说明

Windows 没有显式的「robust mutex」属性，但它的内核命名互斥量**天然就是健壮的**——靠的是 abandoned（废弃）语义：

- `CreateMutex(name)` 创建/打开一个内核命名互斥量对象。
- 当持有该互斥量的进程**异常终止**（崩溃、被结束）时，内核自动把它标记为 abandoned。
- 下一个 `WaitForSingleObject` 不会再永久阻塞，而是返回特殊值 `WAIT_ABANDONED`，**同时把所有权交给等待者**。

关键区别：Windows 的 `WAIT_ABANDONED` 和 Linux 的 `EOWNERDEAD` 都是「拿到锁但提示前主人死了」，但 Windows **没有 `consistent` 这一步**——内核不区分你是否修复了状态，它只负责通知「这锁是被废弃着交到你手里的」。要不要修复共享数据，完全由应用自己决定。

#### 4.4.2 核心流程

```text
WaitForSingleObject(h_, ms) 返回值:
  WAIT_OBJECT_0  → 正常拿到锁
  WAIT_TIMEOUT   → 超时
  WAIT_ABANDONED → 拿到锁，但前主人崩溃了 → libipc: unlock + 重试
```

libipc 在 Windows 上的恢复策略（[win/mutex.h:62-82](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/mutex.h#L62-L82)）：遇到 `WAIT_ABANDONED` 时先 `ReleaseMutex`（`unlock`）放掉，再循环重新 `WaitForSingleObject`，直到拿到一次干净的 `WAIT_OBJECT_0`。这和 Linux a0 后端的「unlock + 重抢」思路一致。

#### 4.4.3 源码精读

打开与加锁（[src/libipc/platform/win/mutex.h:38-47](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/mutex.h#L38-L47) 与 [62-82](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/mutex.h#L62-L82)）：

```cpp
bool open(char const *name) noexcept {
    close();
    h_ = ::CreateMutex(detail::get_sa(), FALSE, detail::to_tchar(name).c_str());
    if (h_ == NULL) { ...return false; }
    return true;
}

bool lock(std::uint64_t tm) noexcept {
    DWORD ret, ms = (tm == invalid_value) ? INFINITE : static_cast<DWORD>(tm);
    for(;;) {
        switch ((ret = ::WaitForSingleObject(h_, ms))) {
        case WAIT_OBJECT_0:  return true;
        case WAIT_TIMEOUT:   return false;
        case WAIT_ABANDONED:
            log.warning("...WAIT_ABANDONED, try again.");
            if (!unlock()) return false;   // ReleaseMutex 放掉废弃锁
            break; // loop again            // 重新等一次干净的
        default: ...return false;
        }
    }
}
```

几个 Windows 专属点：

- `CreateMutex` 的第二个参数 `FALSE` 表示「创建时不立即占有」（初始未锁定）。
- `detail::get_sa()` 提供安全属性（决定跨会话可见性，与 u5-l4 的 `Global\` 前缀相关）。
- Windows 后端**没有 `curr_prog` 缓存、没有引用计数**（见 4.5 节对比）——`open` 直接 `CreateMutex`，`close` 直接 `CloseHandle`，因为内核命名对象的生命周期由内核的引用计数管理，libipc 不必自己维护。
- `clear_storage` 在 Windows 上是**空操作**（[win/mutex.h:59-60](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/mutex.h#L59-L60)）：内核对象随最后一个 HANDLE 关闭而自动消失，没有需要扫的「磁盘残留」（对照 POSIX 的 `shm_unlink`）。

#### 4.4.4 代码实践

**实践目标**：理解 Windows abandoned 检测与 POSIX/Linux 的差异。

**操作步骤**：

1. 读 [win/mutex.h:71-76](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/mutex.h#L71-L76)，确认 `WAIT_ABANDONED` 分支调用的是 `unlock()`（即 `ReleaseMutex`）。
2. 对比三个后端「拿到死亡锁后是否需要 consistent」：Linux a0 需要（`a0_mtx_consistent`）、POSIX pthread 需要（`pthread_mutex_consistent`）、Windows **不需要**（无此概念，只能靠 unlock+重抢）。

**需要观察的现象**：进程 A 持锁崩溃后，进程 B 的 `WaitForSingleObject` 返回 `WAIT_ABANDONED` 而非永久阻塞。

**预期结果**：B 经一次「abandoned → unlock → 重等」后拿到 `WAIT_OBJECT_0`。**待在 Windows 环境验证**。

#### 4.4.5 小练习与答案

**练习**：为什么 Windows 后端不需要 `consistent`，而 Linux/POSIX 需要？

**答案**：Windows 的 abandoned 语义只负责「通知 + 交权」，内核不跟踪锁的状态是否被修复，应用自己决定如何处理脏数据，所以没有「标记一致」这一步。Linux/POSIX 的 robust 协议更强：它要求拿到死亡锁的进程**必须**调用 `consistent` 声明状态已修复，否则一旦未修复就解锁，锁会被永久标记为 `ENOTRECOVERABLE` 报废——这是一种强制性的数据安全保护。Windows 没有这层强制，代价是脏数据风险由应用自负。

---

### 4.5 进程本地句柄缓存与引用计数

#### 4.5.1 概念说明

Linux 和 POSIX 后端都有一个 Windows 后端没有的结构：`curr_prog`——一个**进程内**的「同名字柄缓存表」。它解决的问题是这样的：

假设同一个进程里有 3 个 `ipc::sync::mutex` 对象都用 `"my_lock"` 打开。如果没有缓存，每个对象都会各自 `shm.acquire` 一次、各自映射一次共享内存、各自维护一份引用计数，既浪费又容易让引用计数算错。`curr_prog` 的做法是：**进程内按名字去重**，同一个名字只映射一次共享内存，多个 `mutex` 对象共享这一份映射，用一个原子引用计数 `ref` 记录「当前有几个对象在用它」。

注意这是**进程内**的缓存（一个 `static curr_prog` 单例 + 一个 `std::mutex` 保护），和 u5-l1 讲的**跨进程**引用计数（嵌在共享内存末尾的 `acc_`）是两码事，别混淆。

#### 4.5.2 核心流程

```text
open(name):
  加锁 curr_prog::lock
  在 mutex_handles 这个 map 里查 name
    找不到 → emplace 一条新记录（首次会真正 acquire 共享内存 + 初始化锁）
  mutex_ = 指向这条记录里的锁对象
  ref_   = 指向这条记录里的引用计数
  ref_->fetch_add(1)          // 本进程又多一个用户

close():
  加锁 curr_prog::lock
  ref_->fetch_sub(1)
    若旧值 <= 1（我是最后一个用户）→ 从 map 里 erase 这条记录
      （erase 会触发记录析构 → 释放/销毁底层共享内存与锁）
```

#### 4.5.3 源码精读

Linux 后端的 `curr_prog` 定义（[src/libipc/platform/linux/mutex.h:103-121](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/mutex.h#L103-L121)）：

```cpp
struct curr_prog {
    struct shm_data {
        robust_mutex mtx;                  // 真正的锁（持有共享内存句柄）
        std::atomic<std::int32_t> ref;     // 进程内引用计数
        struct init { char const *name; };
        shm_data(init arg) : mtx{}, ref{0} { mtx.open(arg.name); }
    };
    ipc::map<std::string, shm_data> mutex_handles;   // 名字 → 记录
    std::mutex lock;                                 // 保护这张表
    static curr_prog &get() { static curr_prog info; return info; } // 单例
};
```

`acquire_mutex` 负责查表/建表（[linux/mutex.h:123-139](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/mutex.h#L123-L139)），`open` 在拿到记录后 `ref_->fetch_add(1)`（[linux/mutex.h:176-184](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/mutex.h#L176-L184)）：

```cpp
bool open(char const *name) noexcept {
    close();
    acquire_mutex(name);
    if (!valid()) return false;
    ref_->fetch_add(1, std::memory_order_relaxed);
    return true;
}
```

`close` 用 `release_mutex` 配合一个 lambda 判断是否该删记录（[linux/mutex.h:186-196](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/mutex.h#L186-L196)）：

```cpp
void close() noexcept {
    if ((mutex_ != nullptr) && (ref_ != nullptr)) {
        if (mutex_->name() != nullptr) {
            release_mutex(mutex_->name(), [this] {
                return ref_->fetch_sub(1, std::memory_order_relaxed) <= 1; // 我是最后一个？
            });
        } else mutex_->close();
    }
    mutex_ = nullptr; ref_ = nullptr;
}
```

`fetch_sub` 返回的是**旧值**，旧值 `<= 1` 说明减之前只有 1 个用户（就是我自己），减完归零，于是 `release_mutex` 里 `info.mutex_handles.erase(it)` 删掉记录。POSIX 后端（[posix/mutex.h:29-48](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/mutex.h#L29-L48)）结构完全同构，只是 `shm_data` 里装的是 `ipc::shm::handle shm` + `pthread_mutex_t*`。

最后看 `init()` 静态方法（[linux/mutex.h:159-162](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/mutex.h#L159-L162)）：

```cpp
static void init() {
    // Avoid exception problems caused by static member initialization order.
    curr_prog::get();
}
```

它只是**提前触发** `curr_prog` 单例的构造，避免「静态成员初始化顺序」导致的潜在问题（注释明说）。这个 `init()` 被 `detail::waiter::init()` 调用（[src/libipc/sync/waiter.cpp:17-19](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/sync/waiter.cpp#L17-L19)），在通道初始化时就把缓存表建好。

#### 4.5.4 代码实践

**实践目标**：理解进程内引用计数如何让多个 `mutex` 对象共享一份映射。

**操作步骤**：

1. 读 [linux/mutex.h:176-196](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/mutex.h#L176-L196)，跟踪 `open` 时 `ref_` 加 1、`close` 时 `fetch_sub` 判断旧值 `<=1` 的逻辑。
2. 思考：同一进程里先后 `mutex a; a.open("L"); mutex b; b.open("L");`，第二次 `open` 会不会重新 `shm.acquire`？会不会重新初始化锁？

**需要观察的现象**：第二次 `open` 命中 map 已有记录，`ref_` 从 1 变 2，不重新映射、不重新初始化（`shm_data` 构造函数只在 emplace 时跑一次）。

**预期结果**：两个对象 `a`、`b` 的 `mutex_`/`ref_` 指向同一条 `curr_prog` 记录，`native()` 返回同一个锁地址。`a.close()` 后 `ref_` 减到 1，记录仍在；`b.close()` 后 `ref_` 减到 0，记录被 erase。**待本地验证**。

#### 4.5.5 小练习与答案

**练习 1**：`fetch_sub` 返回旧值 `<= 1` 才删记录，为什么不是 `== 0`？

**答案**：`fetch_sub` 返回的是**减之前**的旧值。旧值 `<= 1` 意味着减之前最多 1 个用户（我自己），减完就是 0，此时该删。如果写成判断「减完之后 `== 0`」也可以，但用旧值 `<= 1` 是原子的「compare」一气呵成，避免在 `fetch_sub` 和后续读之间被并发打断。

**练习 2**：为什么 Windows 后端没有 `curr_prog` 这套缓存？

**答案**：Windows 的互斥量是内核命名对象，`CreateMutex(name)` 本身就是「按名字打开/创建」的原子内核调用，内核内部维护对象的引用计数（每个 HANDLE 算一次），`CloseHandle` 即可。libipc 没必要再在用户态维护一张去重表——多次 `CreateMutex` 同名只是多拿几个 HANDLE 指向同一个内核对象，开销可接受，且不会出现 POSIX 那种「重复 acquire 共享内存 + 重复初始化锁」的问题。

---

## 5. 综合实践

**任务**：用一张表把三个后端的「崩溃恢复」机制对照清楚，并设计一个验证实验。

**第一步（源码阅读，必做）**：填写下表（答案见后）。

| 维度 | Linux a0 | POSIX pthread | Windows |
|------|----------|---------------|---------|
| 锁对象放哪 | 共享内存里的 `a0_mtx_t` | ? | 内核命名对象 |
| 死亡由谁检测 | 内核 robust list + futex `OWNER_DIED` 位 | ? | ? |
| 检测信号（返回值） | `EOWNERDEAD` | ? | `WAIT_ABANDONED` |
| 是否需要 consistent | 是（`a0_mtx_consistent`） | ? | ? |
| 拿到死亡锁后 | consistent → unlock → 重抢 | ? | ? |
| 进程内句柄缓存 | 有（`curr_prog` map + ref） | ? | ? |

**第二步（实验设计）**：写一个最小的两进程程序，进程 A 打开锁 `"crash_test"`、`lock` 成功后 `while(true) sleep`；进程 B 打开同名锁、`lock`（默认无限等待）。然后 `kill -9` 掉进程 A，观察进程 B 是否能继续。

**第三步（分析）**：根据你填的表，说明进程 B 内部会经历哪几个错误码/返回值，最终如何拿到锁。重点回答：libipc 在哪个后端会把「死亡恢复」过程对用户完全隐藏？

**参考答案表**：

| 维度 | Linux a0 | POSIX pthread | Windows |
|------|----------|---------------|---------|
| 锁对象放哪 | 共享内存 `a0_mtx_t` | 共享内存 `pthread_mutex_t` | 内核命名对象 |
| 死亡由谁检测 | 内核 robust list + futex `OWNER_DIED` | pthread robust 属性（底层亦 robust futex） | 内核 abandoned 标记 |
| 检测信号 | `EOWNERDEAD` | `EOWNERDEAD` | `WAIT_ABANDONED` |
| 是否需要 consistent | 是 | 是 | 否 |
| 拿到死亡锁后 | consistent→unlock→重抢 | consistent→直接持锁返回 | unlock→重抢 |
| 进程内句柄缓存 | 有 | 有 | 无 |

三个后端都把死亡恢复藏在 `lock` 内部，用户只看到「`lock()` 最终返回 true」——但恢复路径不同：Linux/Windows 走「释放重抢」，POSIX 走「consistent 后直接持锁」。

> 实验的运行行为（`kill -9` 后 B 是否真的恢复）**待本地验证**，且依赖你的平台：Linux 用 a0 后端，FreeBSD/QNX 用 pthread 后端，Windows 用 abandoned 后端。

## 6. 本讲小结

- `ipc::sync::mutex` 是**跨进程健壮互斥量**，靠 PIMPL + 编译期宏分派到 `linux`(a0) / `posix`(pthread) / `win`(Win32) 三套后端，公共 API 统一。
- 「同名即同一把锁」：Linux/POSIX 把锁对象（`a0_mtx_t`/`pthread_mutex_t`）直接放进命名的共享内存；Windows 用内核命名对象。`obj_impl<SyncT>` 的 `shm_.ref() > 1` 实现「跨进程首引用才初始化」。
- **健壮性的核心**是死亡检测：Linux a0 用内核 robust-list 给 futex 字置 `OWNER_DIED` 位；POSIX 用 `PTHREAD_MUTEX_ROBUST`；Windows 用内核 abandoned 标记。三者都让崩溃持有者的锁不再永久死锁。
- **一致性恢复**：Linux/POSIX 拿到 `EOWNERDEAD` 后**必须** `consistent` 修复，否则未修复就解锁会让锁永久报废（`ENOTRECOVERABLE`）；Windows 无此概念，靠 `unlock`+重抢。
- 三后端恢复策略有差异：Linux a0 与 Windows 都是「释放后重新抢一次干净锁」，POSIX pthread 是「consistent 后直接持锁返回」。
- Linux/POSIX 后端用进程内 `curr_prog` map + 原子 `ref` 做同名句柄去重与引用计数，Windows 后端因内核命名对象自管理而无需此结构。

## 7. 下一步学习建议

- **u6-l3（condition 与 semaphore）**：条件变量必须**配对一把 mutex** 使用（`wait` 内部要 unlock+lock），本讲讲的健壮锁正是那里 `wait` 期间保护状态一致性的基石。重点看 Windows condition 如何用信号量+计数器模拟，以及它如何处理持锁者崩溃。
- **u6-l4（detail::waiter）**：`waiter` 把 `condition` + `mutex` 封装成 channel 通知核心，并调用本讲的 `mutex::init()` 预热缓存表。学完 u6-l2 再看 u6-l4，就能理解 `wait_if` 谓词循环里那把锁的健壮性从何而来。
- **u8-l2（健壮锁的崩溃恢复·进阶）**：本讲侧重「机制与 API」，u8-l2 会更深入 robust-list 的内核协议细节、`sync_obj_impl` 模板如何复用于 condition/semaphore、以及 `ENOTRECOVERABLE` 这类边界情况，建议作为进阶阅读。
- 若想亲手验证崩溃恢复，可参照 `demo/` 目录的风格，写一个进程持锁后 `kill -9`、另一个进程观察恢复的小程序（先按 u1-l2 构建库）。
