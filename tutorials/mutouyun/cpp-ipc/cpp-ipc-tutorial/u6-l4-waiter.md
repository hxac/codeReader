# detail::waiter：channel 的等待/通知核心

## 1. 本讲目标

学完本讲，你应该能够：

- 说清楚 `ipc::detail::waiter` 由哪三个成员组成，以及为什么要把 `condition`、`mutex`、`quit_` 打包在一起。
- 理解 `wait_if(pred, tm)` 的「持锁 → 谓词循环 → 条件变量阻塞 → 超时返回」语义，并能解释它如何被 `wait_for` 的自旋–阻塞退避所驱动。
- 解释 `notify`/`broadcast` 为何在真正唤醒前要先抢一次锁建立内存屏障，从而避免「丢唤醒」。
- 说明 `quit_waiting()` 如何让对端阻塞的 `recv` 优雅退出，以及它与 `clear_storage` 在资源清理上的分工。
- 认识 waiter 在共享内存中的命名后缀 `_WAITER_COND_` / `_WAITER_LOCK_`，并数清楚一个 channel 实际创建了几个 waiter。

本讲是把 u6-l1（自旋退避）、u6-l2（健壮互斥量）、u6-l3（条件变量/信号量）三块零件**组装**成 libipc 内部统一等待原语的关键一步，也是 u3-l4「三类 waiter 与渐进退避」的落地实现。

## 2. 前置知识

阅读本讲前，请先具备以下概念（均来自前序讲义）：

- **条件变量必须配对互斥量**（u6-l3）：`condition::wait(mtx)` 必须在持有 `mtx` 时调用，它「原子地释放锁并阻塞」，被唤醒后再重新抢锁，并用 `while` 复查条件以应对虚假唤醒。
- **命名同步对象**（u6-l2、u6-l3）：libipc 把 mutex/condition 放进**命名共享内存**，多个进程用同一个名字 `open`，就拿到同一把锁 / 同一个条件变量，从而实现跨进程的等待与唤醒。Windows 没有原生「一次全醒」原语，libipc 用「信号量 + 内部锁 + 计数器」三件套手搓条件变量。
- **`wait_for` 的自旋–阻塞退避**（u3-l4）：libipc 的 `wait_for(waiter, pred, tm)` 先用 `sleep(k)` 自旋若干轮反复重试带副作用的谓词 `pred`（即 `push`/`pop`），自旋够多次还不满足，才回调进入条件变量阻塞。
- **`sleep(k, F)` 退避阈值**（u6-l1）：`k < 32` 时只做 `this_thread::yield()`（轻量让步）并 `++k`；`k >= 32` 才执行回调 `F`（即真正的阻塞），执行后**立即 return 不再 ++k**。

如果对上面任意一点不熟，建议先回看对应讲义。本讲会直接使用这些结论。

> 名词速查：`ipc::sync::*` 是 PIMPL 公共门面（`condition`/`mutex`/`semaphore`），`ipc::detail::sync::*` 是它们在各平台下的实现（Linux a0、POSIX pthread、Windows Win32），由编译期宏 `LIBIPC_OS_*` 分派。本讲的 `waiter` 属于 `ipc::detail` 命名空间，是库内部使用的等待原语，**不对外暴露**。

## 3. 本讲源码地图

| 文件 | 作用 | 本讲关注点 |
|------|------|-----------|
| [src/libipc/waiter.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/waiter.h) | `waiter` 类的全部定义 | 三成员、`wait_if`、`notify`/`broadcast`、`quit_waiting`、命名后缀 |
| [src/libipc/sync/waiter.cpp](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/sync/waiter.cpp) | `waiter::init()` 静态函数实现 | 静态初始化为何只调 `mutex::init()` |
| [src/libipc/ipc.cpp](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp) | 核心：`conn_info_head`、`wait_for`、send/recv 主链路 | waiter 的实例化、命名前缀、配对唤醒、`quit_waiting` |
| [include/libipc/condition.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/condition.h) | 公共条件变量接口 | `wait`/`notify`/`broadcast` 的签名与返回值 |
| [include/libipc/rw_lock.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/rw_lock.h) | `sleep(k, F)` 退避回调 | `wait_for` 如何驱动 `wait_if` |
| [src/libipc/platform/linux/mutex.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/mutex.h) | Linux mutex 实现 | `mutex::init()` 静态初始化做了什么 |

## 4. 核心概念与源码讲解

### 4.1 waiter 的三成员结构：condition + mutex + quit_

#### 4.1.1 概念说明

libipc 的 channel 在跨进程通信时，发送方与接收方都需要「等」：发送方等队列腾出空位，接收方等数据到来。这种「等」不能靠 `while(busy) ;` 死循环空耗 CPU，也不能直接裸用 `std::condition_variable`（它只在单进程内有意义）。库需要一个**跨进程**的、自带超时、自带「关闭开关」的等待原语——这就是 `ipc::detail::waiter`。

它的设计哲学是：把「**等什么**（条件变量）」「**用谁的锁保护谓词**（互斥量）」「**还要不要继续等**（退出标志）」三件事焊在一个类里，对外暴露一个统一的、安全的接口，让上层（`conn_info_head`）不必再操心这三者的配对关系。

#### 4.1.2 核心流程

一个 `waiter` 对象由三个成员构成：

```
┌─────────────────────────── waiter ───────────────────────────┐
│  ipc::sync::condition cond_;   // 跨进程条件变量：负责"睡"与"叫醒" │
│  ipc::sync::mutex     lock_;   // 跨进程互斥量：保护谓词与共享状态  │
│  std::atomic<bool>    quit_{false}; // 退出开关：置位后停止等待     │
└───────────────────────────────────────────────────────────────┘
```

- `cond_` 和 `lock_` 是**命名同步对象**，`open(name)` 时按名字连到共享内存里**同一份**对象，所以两个进程的两个 `waiter` 只要 `open` 同名就共享同一把锁、同一个条件变量。
- `quit_` 是**进程本地**的原子布尔（每个 `waiter` 对象各有一份），它不进共享内存，作用是让**本端**主动通知对端「别再等了，我要退出了」——具体机制见 4.4。

生命周期：`open` 建立连接 → `wait_if`/`notify`/`broadcast` 反复使用 → `close` 拆连接、`clear` 强制释放、`clear_storage` 按名字扫地。

#### 4.1.3 源码精读

三成员的声明见 [src/libipc/waiter.h:16-31](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/waiter.h#L16-L31)，类名 `waiter`，三个私有成员 `cond_`/`lock_`/`quit_`，构造函数带名时调 `open`，析构调 `close`：

```cpp
class waiter {
    ipc::sync::condition cond_;
    ipc::sync::mutex     lock_;
    std::atomic<bool>    quit_ {false};
public:
    static void init();
    waiter() = default;
    waiter(char const *name) { open(name); }
    ~waiter() { close(); }
```

注意 `quit_` 是 `std::atomic<bool>` 而非普通 `bool`：因为 `quit_waiting()`（可能从另一个线程/另一个 `waiter` 视角调用）与 `wait_if` 里的读取存在数据竞争，必须用原子操作保证可见性（见 4.4 的内存序）。

`waiter` 真正被实例化的地方在 `conn_info_head`，一条 channel 持有**三个** waiter，各司其职（详见 [src/libipc/ipc.cpp:112-118](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L112-L118)）：

```cpp
struct conn_info_head {
    std::string prefix_;
    std::string name_;
    msg_id_t    cc_id_;
    ipc::detail::waiter cc_waiter_, wt_waiter_, rd_waiter_; // 三个 waiter
    ipc::shm::handle acc_h_;
    ...
```

这三类 waiter 的职责（承接 u3-l4）：

| waiter | 名字前缀 | 等什么 | 谁来唤醒 |
|--------|---------|--------|---------|
| `cc_waiter_` | `CC_CONN__` | 等接收者上线（connect 确认） | 接收方 `connect` 成功后 `cc_waiter_.broadcast()` |
| `wt_waiter_` | `WT_CONN__` | 发送方等队列**写满**腾位 | 接收方 `pop` 后 `wt_waiter_.broadcast()` |
| `rd_waiter_` | `RD_CONN__` | 接收方等**读空**有数据 | 发送方 `push` 后 `rd_waiter_.broadcast()` |

#### 4.1.4 代码实践

**实践目标**：数清楚一条 channel 在共享内存里创建了几个「等待相关」的命名对象。

**操作步骤**（源码阅读型）：
1. 打开 [src/libipc/ipc.cpp:125-129](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L125-L129)，看 `conn_info_head::init()` 如何 `open` 三个 waiter 和一个 `acc_h_`。
2. 对照 [src/libipc/waiter.h:37-47](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/waiter.h#L37-L47) 的 `open`：每个 waiter 内部又 `open` 了 2 个命名对象（`_WAITER_COND_` 和 `_WAITER_LOCK_`）。
3. 数一数：3 个 waiter × 2 = 6 个命名同步对象，外加 1 个 `acc_h_`（原子计数器共享内存）。

**预期结果**：一条 channel（忽略队列本体 `QU_CONN__`）会在共享内存里建立 **6 个命名同步对象 + 1 个计数器**。

**待本地验证**（可选运行实验）：在 Linux 上跑一个 route demo，用 `ls /dev/shm/` 观察通道名下出现的 `*_WAITER_COND_` / `*_WAITER_LOCK_` 文件数量，验证是否为 6 个。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `quit_` 用 `std::atomic<bool>` 而不是放进共享内存的 `std::atomic`？

**答案**：`quit_` 是「本端是否打算退出」的本地意图，只需在本进程内跨线程可见，无需跨进程共享；放进共享内存反而会让对端的退出污染本端语义。用进程本地原子量既满足多线程读写安全，又避免跨进程耦合。

**练习 2**：`cc_waiter_` / `wt_waiter_` / `rd_waiter_` 三者能否合并成一个？

**答案**：理论上不能。它们等待的条件完全不同（接收者上线 / 队列非满 / 队列非空），合并会导致一次唤醒惊动所有等待者（惊群），且无法区分「该谁醒」。libipc 故意拆成三个独立条件变量 + 独立互斥量，实现精确唤醒。

---

### 4.2 wait_if：谓词循环与超时

#### 4.2.1 概念说明

裸条件变量有一个经典陷阱：**丢唤醒（lost wakeup）**——如果你在 `notify` 之后再 `wait`，就会永远睡死。标准解法是「用锁保护一个布尔谓词，`wait` 时持锁检查谓词，不满足才睡」。`waiter::wait_if` 就是把这个标准模式封装成一行安全的调用：调用方传一个谓词 `pred()`（「我现在还要继续等吗」），`wait_if` 负责持锁、循环、睡、醒、超时全部细节。

#### 4.2.2 核心流程

`wait_if(pred, tm)` 的执行流程：

```
持锁 lock_（lock_guard）
while ( 还没 quit_  且  pred() 为真 ):     // pred()=真 表示"仍需继续等"
    cond_.wait(lock_, tm)
        └─ 原子释放 lock_ 并阻塞，被唤醒或超时后重新抢 lock_
        └─ 返回 false 表示超时/失败 → wait_if 立即返回 false
返回 true                                  // 要么 quit 了，要么 pred 变假（条件满足）
```

关键点：

- **持锁检查谓词**：`pred()` 在持有 `lock_` 时求值，保证读到的状态与「是否真的有人 notify」一致，杜绝丢唤醒。
- **while 循环防虚假唤醒**：被唤醒后必须**重新检查**谓词，因为可能是虚假唤醒或惊群竞争。
- **`quit_` 短路**：只要 `quit_` 被置位，循环立刻退出，这是优雅断连的入口（见 4.4）。
- **超时传播**：`cond_.wait(lock_, tm)` 在超时（`ETIMEDOUT`）时返回 `false`，`wait_if` 据此返回 `false`，上层据此判断「等失败了」。

#### 4.2.3 源码精读

`wait_if` 的完整实现见 [src/libipc/waiter.h:64-74](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/waiter.h#L64-L74)：

```cpp
template <typename F>
bool wait_if(F &&pred, std::uint64_t tm = ipc::invalid_value) noexcept {
    LIBIPC_UNUSED std::lock_guard<ipc::sync::mutex> guard {lock_};
    while ([this, &pred] {
                return !quit_.load(std::memory_order_relaxed)
                    && std::forward<F>(pred)();
          }()) {
        if (!cond_.wait(lock_, tm)) return false;
    }
    return true;
}
```

逐行解读：

- `std::lock_guard<ipc::sync::mutex> guard {lock_}`：进入即持锁，函数返回时自动释放。注意 `lock_` 是 libipc 自定义的跨进程 mutex，`std::lock_guard` 只要求类型有 `lock()`/`unlock()`，因此可直接复用。
- `!quit_.load(relaxed) && pred()`：先看本端是否要退出，再看谓词。`quit_` 用 `relaxed` 序读取——因为它在 4.4 的 `quit_waiting` 里配合 `broadcast` 使用，可见性由 `broadcast` 前的锁屏障保证（见 4.3），此处无需更强序。
- `if (!cond_.wait(lock_, tm)) return false`：`cond_.wait` 超时返回 false，`wait_if` 立即返回 false 通知上层「等失败了」。

`wait_if` 本身**不会自旋**——自旋是上层 `wait_for` 的职责。看 [src/libipc/ipc.cpp:378-391](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L378-L391)：

```cpp
template <typename W, typename F>
bool wait_for(W& waiter, F&& pred, std::uint64_t tm) {
    if (tm == 0) return !pred();                 // 非阻塞：求值一次就返回
    for (unsigned k = 0; pred();) {              // 自旋重试
        bool ret = true;
        ipc::sleep(k, [&k, &ret, &waiter, &pred, tm] {
            ret = waiter.wait_if(std::forward<F>(pred), tm);  // k>=32 才执行
            k   = 0;                             // 进入阻塞后清零 k
        });
        if (!ret) return false; // timeout or fail
        if (k == 0) break;      // k 被清零说明已阻塞过一次，退出循环
    }
    return true;
}
```

配合 [include/libipc/rw_lock.h:76-86](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/rw_lock.h#L76-L86) 的 `sleep(k, F)`：

```cpp
template <std::size_t N = 32, typename K, typename F>
inline void sleep(K& k, F&& f) {
    if (k < static_cast<K>(N)) { std::this_thread::yield(); }  // 自旋阶段：让步
    else { static_cast<void>(std::forward<F>(f)()); return; }  // 阻塞阶段：执行回调
    ++k;
}
```

把两者拼起来看：`wait_for` 前 32 轮只 `yield()` 反复重试 `pred`（即反复尝试 `push`/`pop`，乐观假设冲突瞬时），第 33 轮起才回调 `wait_if` 进入条件变量真正阻塞。这种「先自旋后阻塞」正是 u3-l4 讲过的退避哲学在代码里的体现，而 `wait_if` 就是那条「真正阻塞」的路径。

#### 4.2.4 代码实践

**实践目标**：理解 `wait_if` 在 send/recv 中的实际调用，看清「谓词」到底是什么。

**操作步骤**（源码阅读型）：
1. 打开 [src/libipc/ipc.cpp:595-607](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L595-L607)，看 `send` 中 `wait_for(info->wt_waiter_, [&]{ return !que->push(...); })`。
2. 注意谓词是 `!que->push(...)`：`push` 返回 true 表示**入队失败（队列满）**，取反为 true 表示「还要继续等」。这正是 `wt_waiter_`（等写满腾位）的语义。
3. 再看 [src/libipc/ipc.cpp:645-654](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L645-L654) 的 `recv`：谓词是 `!que->pop(msg)`——`pop` 返回 true 表示**出队失败（队列空）**，取反为 true 表示「还要继续等」，对应 `rd_waiter_`（等读空有数据）。

**需要观察的现象**：谓词既承担「判断是否要等」又承担「真正干活（push/pop）」的双重职责——这是 libipc 自旋设计的精髓：自旋的每一轮都顺手尝试一次真实操作，成功就立刻返回，省去唤醒开销。

**预期结果**：能用自己的话讲清「`wait_if` 的谓词返回 true = 还要继续等 = 操作没成功」这一约定。

#### 4.2.5 小练习与答案

**练习 1**：`wait_if` 里 `cond_.wait(lock_, tm)` 返回 false 时，`wait_if` 返回什么？上层 `wait_for` 会怎样？

**答案**：返回 false（超时或失败）。`wait_for` 中 `ret` 被赋为 false，`if (!ret) return false;` 立即向上层返回 false，表示「等失败了」。在 `send` 中这会触发 `force_push` 强制发送（见 [src/libipc/ipc.cpp:600-606](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L600-L606)）。

**练习 2**：如果 `wait_if` 不用 `while` 而用 `if`，会有什么问题？

**答案**：无法防御虚假唤醒（spurious wakeup）和惊群竞争——多个等待者被同时唤醒后只有一个能抢到锁推进状态，其余的谓词其实仍为真却会误以为条件满足而直接返回，导致错误。`while` 保证每次被唤醒后重新检查谓词。

---

### 4.3 notify / broadcast：内存屏障防丢唤醒

#### 4.3.1 概念说明

唤醒是条件变量的另一半。libipc 的 `waiter` 提供两个唤醒接口：`notify()`（叫醒一个）和 `broadcast()`（叫醒全部）。channel 的设计统一用 `broadcast`，因为广播模式下一条消息可能要唤醒所有在线接收者。

这里有一个**极易踩的坑**：在跨进程共享内存里，发送方写入数据后，必须保证「数据写入」对正在 `wait` 的接收方**可见**，然后才能唤醒它；否则接收方被唤醒后读不到新数据，谓词仍为真，又睡回去——表面上是「丢唤醒」。libipc 用一个巧妙的小技巧解决：唤醒前先抢一次锁。

#### 4.3.2 核心流程

`broadcast()` 的两步：

```
{
    lock_guard<mutex> barrier{lock_};   // 第一步：抢锁后立即释放（空临界区）
}                                       //   目的：建立 happens-before 内存屏障
cond_.broadcast(lock_);                 // 第二步：真正唤醒全部等待者
```

- 第一步那个**空的大括号临界区**看似无用，实则是关键：它强制当前线程与任何正在 `wait_if` 中持锁检查谓词的线程**同步**。因为 `wait_if` 持有同一把 `lock_`，发送方要拿到这把锁，必须等接收方进入 `cond_.wait`（此时锁已释放）。这样，发送方在抢到锁**之前**写共享内存、抢到锁**之后**才 broadcast，接收方被唤醒后重新抢锁、读到的必然是最新数据。
- 第二步 `cond_.broadcast(lock_)` 通知底层条件变量唤醒所有等待者（Linux/POSIX 一次唤醒全部；Windows 用计数器 `post(cnt)` 放够许可，见 u6-l3）。

`notify()` 结构完全相同，只是最后调 `cond_.notify`（唤醒一个）。

#### 4.3.3 源码精读

`notify` 与 `broadcast` 见 [src/libipc/waiter.h:76-88](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/waiter.h#L76-L88)：

```cpp
bool notify() noexcept {
    {
        LIBIPC_UNUSED std::lock_guard<ipc::sync::mutex> barrier{lock_}; // barrier
    }
    return cond_.notify(lock_);
}

bool broadcast() noexcept {
    {
        LIBIPC_UNUSED std::lock_guard<ipc::sync::mutex> barrier{lock_}; // barrier
    }
    return cond_.broadcast(lock_);
}
```

注释 `// barrier` 点明了意图：这是一个用于建立内存屏障的空临界区。注意它和 `wait_if` 共用的是**同一个 `lock_`**，这才是屏障成立的根本——两边用同一把锁串起 happens-before 关系。

`cond_` 的接口见 [include/libipc/condition.h:32-34](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/condition.h#L32-L34)：`wait`/`notify`/`broadcast` 都接收一个 `mutex&` 参数（唤醒时需要的内部协议），返回 `bool` 表示是否成功。

实际配对使用见 send 主链路 [src/libipc/ipc.cpp:595-607](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L595-L607)：发送方 `push` 成功后立刻 `info->rd_waiter_.broadcast()` 唤醒接收方；recv 链路 [src/libipc/ipc.cpp:645-654](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L645-L654) 则是接收方 `pop` 后 `inf->wt_waiter_.broadcast()` 唤醒发送方——形成「生产者等 `wt`、消费者等 `rd`、各自 `broadcast` 对端」的对称配对。

#### 4.3.4 代码实践

**实践目标**：验证 `broadcast` 前的锁屏障为何不可或缺。

**操作步骤**（推理型，可结合源码）：
1. 假设去掉 `broadcast()` 里那个空 `lock_guard` 屏障，画一个时序：发送方写共享内存 → 直接 `cond_.broadcast` → 此时接收方恰好在 `wait_if` 持锁检查谓词 `pop`。
2. 思考：没有屏障时，写内存与 broadcast 之间没有同步点，接收方可能读到旧数据（谓词为真）却又被唤醒，结果谓词仍为真，回到 `wait`——这就是「假唤醒 + 丢数据」。
3. 对比有屏障的版本：发送方必须等接收方释放 `lock_`（即已进入 `cond_.wait`），才能拿到锁再 broadcast，从而保证接收方醒来后读到最新数据。

**预期结果**：能口述「同一把 `lock_` 把写者与读者串成 happens-before 链，屏障保证了唤醒时数据已可见」。

#### 4.3.5 小练习与答案

**练习 1**：`broadcast` 和 `notify` 结构完全一样，为什么 channel 实际只用 `broadcast`？

**答案**：广播模式（route/channel 默认）下一条消息要送达所有在线接收者，必须唤醒全部等待者；`notify` 只唤醒一个会导致其余接收者漏消息。故 send/recv 链路统一用 `broadcast`。

**练习 2**：那个空 `lock_guard` 临界区里什么也没做，删掉它程序是否仍「看起来」能跑？

**答案**：多数情况下看起来能跑（因为自旋阶段也会重试），但在高并发或特定时序下会偶发「丢唤醒 / 读到旧数据」，属于典型的难以复现的并发 bug。这正是它必须存在的理由，也体现了并发编程「正确性优先于微观性能」的原则。

---

### 4.4 quit_waiting / clear_storage 与 init 静态初始化

#### 4.4.1 概念说明

本模块收尾三个相关问题：

1. **优雅退出**：当发送方 `disconnect()` 或程序退出时，对端可能正阻塞在 `recv` 的 `rd_waiter_` 上。必须有办法让对端「别再等了，赶紧返回」。`quit_waiting()` 就是这个开关。
2. **资源清理分层**：`clear`（强制释放本句柄）、`clear_storage`（按名字扫除磁盘残留）的分工，与 u5-l1 的共享内存清理语义一脉相承。
3. **静态初始化**：`waiter::init()` 是个静态函数，在创建任何 channel 前必须调用一次，目的是规避 C++ 静态成员初始化顺序的坑。

#### 4.4.2 核心流程

**quit_waiting 的关闭语义**：

```
quit_waiting():
    quit_.store(true, memory_order_release)   // 1. 置退出标志（release 序，保证可见）
    return broadcast()                         // 2. 广播唤醒所有阻塞的等待者
```

置位 `quit_` 后再 `broadcast`，对端被唤醒回到 `wait_if` 的 while 条件 `!quit_ && pred()`，此时 `quit_` 为真，循环立刻退出，`wait_if` 返回 true（注意：是 true 不是 false！表示「不再等了，正常返回」），上层据此返回空 `buff_t` 表示「对端断开」。`release` 内存序保证「置位」对被唤醒方可见。

`conn_info_head::quit_waiting()` 一次置位全部三个 waiter，见 [src/libipc/ipc.cpp:161-165](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L161-L165)：

```cpp
void quit_waiting() {
    cc_waiter_.quit_waiting();
    wt_waiter_.quit_waiting();
    rd_waiter_.quit_waiting();
}
```

它在 `disconnect_receiver()` 中被调用（[src/libipc/ipc.cpp:431-437](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L431-L437)），即断连时唤醒对端所有等待者。

**资源清理分层**：

| 方法 | 作用 | 落点 |
|------|------|------|
| `close()` | 礼貌拆映射（引用计数，最后一人清理） | `cond_.close()` / `lock_.close()` |
| `clear()` | 强制释放本句柄依赖的共享内存 | `cond_.clear()` / `lock_.clear()` |
| `clear_storage(name)` | 按名字扫除磁盘残留对象（专治崩溃泄漏） | 拼出 `_WAITER_COND_`/`_WAITER_LOCK_` 后调底层 `clear_storage` |

**init 静态初始化**：`waiter::init()` 只做一件事——触发 `ipc::detail::sync::mutex::init()`，确保 mutex 内部的进程级缓存表（`curr_prog`）在第一次使用前就构造好，避免多线程下静态局部对象初始化竞争与顺序问题。

#### 4.4.3 源码精读

`quit_waiting` 见 [src/libipc/waiter.h:90-93](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/waiter.h#L90-L93)：

```cpp
bool quit_waiting() {
    quit_.store(true, std::memory_order_release);
    return broadcast();
}
```

`open` 与命名后缀见 [src/libipc/waiter.h:37-47](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/waiter.h#L37-L47)：

```cpp
bool open(char const *name) noexcept {
    quit_.store(false, std::memory_order_relaxed);
    if (!cond_.open((std::string{name} + "_WAITER_COND_").c_str())) return false;
    if (!lock_.open((std::string{name} + "_WAITER_LOCK_").c_str())) return false;
    return valid();
}
```

关键：每个 waiter 把传入的名字拼接固定后缀 `_WAITER_COND_` 和 `_WAITER_LOCK_`，分别作为条件变量和互斥量的命名对象名。结合 [src/libipc/ipc.cpp:126-128](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L126-L128)，三个 waiter 的完整命名前缀是 `make_prefix(prefix_, "CC_CONN__"/"WT_CONN__"/"RD_CONN__", name_)`，再加上 `_WAITER_COND_`/`_WAITER_LOCK_`。`open` 时还会把 `quit_` 重置为 `false`（复用对象时清掉旧的退出标志）。

`clear`/`clear_storage` 见 [src/libipc/waiter.h:49-62](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/waiter.h#L49-L62)；`clear_storage` 是静态函数，按同样的后缀规则拼名后调底层清理，供 `conn_info_head::clear_storage`（[src/libipc/ipc.cpp:155-158](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L155-L158)）批量调用。

`waiter::init()` 的实现见 [src/libipc/sync/waiter.cpp:17-19](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/sync/waiter.cpp#L17-L19)：

```cpp
void waiter::init() {
    ipc::detail::sync::mutex::init();
}
```

它只在创建首个 channel 前被 `chan_impl<Flag>::init_first()` 调用一次（[src/libipc/ipc.cpp:752-756](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L752-L756)）。`mutex::init()` 的实现在 [src/libipc/platform/linux/mutex.h:159-162](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/mutex.h#L159-L162)：

```cpp
static void init() {
    // Avoid exception problems caused by static member initialization order.
    curr_prog::get();
}
```

注释直白：**避免静态成员初始化顺序导致的问题**。`curr_prog::get()` 返回一个函数内 `static curr_prog info;`（[src/libipc/platform/linux/mutex.h:117-120](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/mutex.h#L117-L120)），C++11 起函数内静态变量初始化是线程安全的（「魔法静态」）。提前触发它，保证后续 `mutex::open` 往 `mutex_handles` 表里插项时表已就绪。

#### 4.4.4 代码实践

**实践目标**：跟踪一次 `disconnect` 如何让对端 `recv` 收到空消息退出。

**操作步骤**（源码阅读型 + 可选运行）：
1. 发送方调用 `chan_wrapper::disconnect()` → 最终走到 `conn_info_t::disconnect_receiver()`（[src/libipc/ipc.cpp:431-437](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L431-L437)）→ `this->quit_waiting()`（[src/libipc/ipc.cpp:161-165](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L161-L165)）。
2. 三个 waiter 的 `quit_` 被置 true 并 `broadcast`。
3. 对端阻塞在 `recv` 的 `wait_for(rd_waiter_, ...)` → `wait_if` 的 while 条件 `!quit_ && pred()` 因 `quit_` 为真而退出，`wait_if` 返回 true。
4. 回到 `recv` 循环：`pop` 仍失败（无数据），`wait_for` 这次因 `pred()` 在自旋里变假……实际上由于 `quit_` 持续为真，`wait_if` 立即返回，最终 `recv` 返回空 `buff_t`（对端表现为收到空消息，与 u1-l4 讲的 `disconnect` 唤醒一致）。

**需要观察的现象**：对端原本永久阻塞的 `recv` 在发送方 `disconnect` 后**立即**返回空 `buff_t`，而不是傻等。

**预期结果 / 待本地验证**：运行 `demo/send_recv`，先启动 recv（阻塞），再启动 send 发几条后 `disconnect()`，观察 recv 是否在 send 退出后收到空消息并打印「对端断开」类提示。

#### 4.4.5 小练习与答案

**练习 1**：`quit_waiting()` 里 `quit_.store(true, release)` 为什么用 `release` 序，而 `wait_if` 里读 `quit_` 却用 `relaxed`？

**答案**：`release` 写保证「置位 quit_」之前的所有共享内存写（如断连标记）对随后通过 `broadcast` 唤醒的线程可见；而 `wait_if` 读 `quit_` 时用的是 `relaxed`，因为真正保证可见性的是 `broadcast` 前的锁屏障与 `cond_.wait` 内部的同步——`wait_if` 在持锁状态下读，读到的值已被屏障约束为最新。强弱搭配，既安全又不过度加序。

**练习 2**：`waiter::init()` 为什么是静态函数、且只调 `mutex::init()`？

**答案**：它不操作任何具体 waiter 实例，而是做「进程级一次性准备」——触发 `mutex` 内部 `curr_prog` 缓存表的线程安全初始化，规避静态成员初始化顺序陷阱。condition 不需要类似准备（它的状态完全由共享内存承载），所以只调 `mutex::init()`。它在创建首个 channel 时由 `init_first()` 调用一次即可。

---

## 5. 综合实践

**任务**：画一张「一条 channel 的等待/通知全景图」，把本讲的 `waiter` 与 u3-l4 的三类 waiter、u6-l3 的 condition/semaphore 串起来。

具体要求：

1. **数对象**：在图上标出一条 channel 创建了几个 `waiter`（3 个：`cc_`/`wt_`/`rd_`），每个 waiter 内部又有几个命名同步对象（2 个：`_WAITER_COND_` + `_WAITER_LOCK_`），合计 6 个命名对象 + 1 个 `acc_h_` 计数器。
2. **标命名**：写出三个 waiter 各自的完整前缀（`CC_CONN__`/`WT_CONN__`/`RD_CONN__`），并说明它们如何与 `_WAITER_COND_`/`_WAITER_LOCK_` 拼成最终的共享内存对象名（参考 [src/libipc/waiter.h:37-47](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/waiter.h#L37-L47) 与 [src/libipc/ipc.cpp:126-128](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L126-L128)）。
3. **画数据流**：画一条消息从 send 到 recv 的唤醒路径：`send` → `push` → `rd_waiter_.broadcast()`（含锁屏障）→ 对端 `rd_waiter_` 的 `wait_if` 被唤醒 → `pop` → `wt_waiter_.broadcast()` 唤醒发送方。
4. **画退出流**：画发送方 `disconnect()` → `quit_waiting()` → 三个 `quit_=true` + `broadcast` → 对端 `wait_if` 退出 → `recv` 返回空消息。

**可选运行验证**：用 `demo/send_recv` 跑两个进程，在 Linux 下 `ls /dev/shm/` 观察通道名相关的命名对象数量，验证你的「数对象」结论。

**验收标准**：图上能清晰看出「3 个 waiter × (1 cond + 1 lock) = 6 个跨进程同步对象」，并说清 `quit_waiting` 如何通过 `quit_=true + broadcast` 让对端 `wait_if` 的 while 循环退出。

## 6. 本讲小结

- `ipc::detail::waiter` 把**条件变量 `cond_` + 互斥量 `lock_` + 退出标志 `quit_`** 三者焊成一个类，对外提供安全的等待/唤醒接口，是 channel 通知的核心。
- `wait_if(pred, tm)` 遵循标准条件变量模式：持锁 → while 检查 `!quit_ && pred()` → 不满足才 `cond_.wait`；超时返回 false，正常/退出返回 true。它本身不自旋，自旋由上层 `wait_for` + `sleep(k,F)` 的 32 轮退避负责。
- `notify`/`broadcast` 在真正唤醒前先用一个**空 `lock_guard` 临界区**建立内存屏障（与 `wait_if` 共用同一把 `lock_`），杜绝跨进程共享内存的「丢唤醒 / 读旧数据」。
- `quit_waiting()` 用 `quit_.store(true, release)` + `broadcast()` 让对端 `wait_if` 的 while 循环退出，实现优雅断连；`conn_info_head::quit_waiting()` 一次置位三个 waiter。
- 每个 waiter 用固定后缀 `_WAITER_COND_`/`_WAITER_LOCK_` 命名其条件变量与互斥量；一条 channel 持有 `cc_`/`wt_`/`rd_` 三个 waiter，共 6 个命名同步对象。
- `waiter::init()` 是进程级一次性静态初始化，仅触发 `mutex::init()` 来规避静态成员初始化顺序问题，由首个 channel 的 `init_first()` 调用。

## 7. 下一步学习建议

本讲把 libipc 的同步原语（u6-l1～u6-l3）组装成了内部 `waiter`。接下来：

- **u7（内存管理子系统）**：进入 `mem/` 分配器架构，看 `new_delete_resource`、`bytes_allocator`、`monotonic_buffer_resource` 如何支撑库内部的内存分配。本讲涉及的 `ipc::map`、`std::atomic` 等都依赖这套分配器。
- **u8-l1（内存序与伪共享）**：如果想更深入理解本讲各处 `memory_order_release`/`relaxed` 的选择，以及 `cache_line_size` 对齐如何避免伪共享，可重点阅读 u8-l1。
- **回看 u3-l4**：本讲是 u3-l4「等待模型」的实现落地，建议两讲对照阅读，先看 u3-l4 建立「三类 waiter + 渐进退避」的全景，再用本讲填补 `wait_if`/`broadcast`/`quit_waiting` 的实现细节。
- **延伸阅读**：直接对照 [src/libipc/waiter.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/waiter.h) 与 [src/libipc/platform/linux/condition.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/condition.h)（或 `win/condition.h` 看 Windows 计数器算法），把 condition 的平台实现与本讲的封装对应起来。
