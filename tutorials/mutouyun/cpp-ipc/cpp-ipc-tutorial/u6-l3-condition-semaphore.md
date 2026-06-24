# condition 与 semaphore

## 1. 本讲目标

本讲接着 [u6-l2 跨进程健壮互斥量](u6-l2-robust-mutex.md)（已经讲清楚了「锁」），把 libipc 同步原语里另一半——「等待/唤醒」——讲透。libipc 的等待模型（[u3-l4 等待模型：三类 waiter 与渐进退避](u3-l4-wait-model.md)）最终都要落到两个底层原语上：

- **condition（条件变量）**：让线程「释放某把锁、睡下去，直到别人通知它」。
- **semaphore（信号量）**：一个跨进程的计数器，用来做「发许可 / 收许可」。

学完本讲你应当能够：

1. 说出 condition 为什么**必须和 mutex 配对使用**，并解释 wait/notify/broadcast 的协议。
2. 读懂 **Windows 上用「信号量 + 计数器」手写实现条件变量**的经典算法，并解释 `broadcast` 为什么要把计数器读出来再 post 那么多次。
3. 看懂 POSIX **命名信号量** `sem_t` 的 `sem_open / sem_wait / sem_post` 用法，以及它与 Windows 内核信号量的对应关系。
4. 理解**超时等待**的实现（绝对时间 `timespec`），以及 condition / semaphore 是如何靠编译期宏被分派到 a0 / pthread / Win32 三套后端的。

## 2. 前置知识

在进入源码前，先用最朴素的语言建立两个直觉。

### 2.1 条件变量要解决什么问题

「锁」只能互斥（同一时刻只有一个线程进临界区），但它**不会等条件成立**。典型场景是：消费者拿到锁后发现队列是空的，它不能就地死循环占着锁（这会饿死生产者），而应该「**松手 + 睡觉 + 被叫醒后重新抢锁**」。这正是条件变量干的事：

```
消费者:
  lock(mtx)
  while (队列空):
      wait(cond, mtx)     // 原子地：释放 mtx，阻塞；被唤醒后重新持有 mtx
  取出一条消息
  unlock(mtx)

生产者:
  lock(mtx)
  放入一条消息
  notify(cond)            // 叫醒一个在 wait 的消费者
  unlock(mtx)
```

三个关键点，请记住，后面源码全在围绕它们打转：

- `wait` **必须**在持有 `mtx` 时调用，且它「释放锁 + 阻塞」必须是**原子**的——否则会丢唤醒（notify 发生在释放锁与阻塞之间，通知就没人接收）。
- 条件要用 `while` 循环重新检查，因为存在**虚假唤醒**（spurious wakeup）和**惊群**后的竞争。
- `notify` 叫醒一个，`broadcast` 叫醒全部。

### 2.2 信号量要解决什么问题

信号量可以理解成一个**带计数的令牌筐**：

- `post`（也叫 V / signal）：往筐里放令牌，计数 `+1`。
- `wait`（也叫 P / wait）：从筐里取令牌，筐空就阻塞，计数 `-1`。

互斥量其实是「计数恒为 1」的信号量。信号量更灵活之处在于：一个 `post` 可以**精确唤醒一个**阻塞者，多个 `post` 唤醒多个——这正是 Windows 条件变量算法的核心抓手。

### 2.3 libipc 的「统一接口、各自实现、编译期分流」

本讲会出现**两套名字空间**，请务必分清：

- `ipc::sync::condition` / `ipc::sync::semaphore`：**公共 API**（在 `include/libipc/`），用 PIMPL 把实现藏起来，对用户暴露不透明指针 `p_`。
- `ipc::detail::sync::condition` / `ipc::detail::sync::semaphore`：**真正的平台实现**（在 `src/libipc/platform/{win,posix,linux}/`），每个平台一份，签名一致。

公共 API 的 `.cpp` 用编译期宏 `LIBIPC_OS_*` 决定 `#include` 哪一份实现——这套机制在 [u5-l2 平台检测与后端分派](u5-l2-platform-detection.md) 已讲过，本讲直接套用。

## 3. 本讲源码地图

| 文件 | 角色 |
|------|------|
| [include/libipc/condition.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/condition.h) | condition 公共 API（PIMPL 门面） |
| [include/libipc/semaphore.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/semaphore.h) | semaphore 公共 API（PIMPL 门面） |
| [src/libipc/sync/condition.cpp](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/sync/condition.cpp) | condition 公共 API 实现 + 编译期后端分派 |
| [src/libipc/sync/semaphore.cpp](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/sync/semaphore.cpp) | semaphore 公共 API 实现 + 编译期后端分派 |
| [src/libipc/platform/win/condition.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/condition.h) | Windows：用 semaphore+mutex+计数器手写 condition |
| [src/libipc/platform/win/semaphore.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/semaphore.h) | Windows：内核信号量（CreateSemaphore 等） |
| [src/libipc/platform/posix/condition.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/condition.h) | POSIX（QNX/FreeBSD）：pthread 条件变量 |
| [src/libipc/platform/posix/semaphore_impl.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/semaphore_impl.h) | POSIX/Linux：命名信号量 sem_t |
| [src/libipc/platform/linux/condition.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/condition.h) | Linux：a0（futex）条件变量 |
| [src/libipc/platform/posix/get_wait_time.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/get_wait_time.h) | 把「相对毫秒」换算成「绝对 timespec」（超时等待用） |
| [src/libipc/waiter.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/waiter.h) | 上层消费者：condition+mutex+quit 标志组合成 waiter |

> 注意一个**不对称**：semaphore 在 Linux / QNX / FreeBSD 上**全部走 POSIX 命名信号量**；而 condition 三平台各不相同（Win 自写、Linux 走 a0、QNX/FreeBSD 走 pthread）。原因见 4.4。

## 4. 核心概念与源码讲解

### 4.1 condition 接口与配对 mutex

#### 4.1.1 概念说明

`ipc::sync::condition` 是 libipc 对外暴露的条件变量。它的设计与 `ipc::sync::mutex`（[u6-l2](u6-l2-robust-mutex.md)）完全同构：可命名（同名即跨进程同一对象）、PIMPL 隐藏实现、提供 `clear / clear_storage` 两档清理。它的三个核心动作与 2.1 节的协议一一对应：

- `wait(mtx, tm)`：**必须在持有 `mtx` 时调用**，原子地释放 `mtx` 并阻塞，被唤醒后重新持有 `mtx`；`tm` 为超时（毫秒），默认 `invalid_value` 表示无限等待。
- `notify(mtx)`：唤醒一个等待者。
- `broadcast(mtx)`：唤醒全部等待者。

注意三个函数都**把用户那把 `mtx` 作为参数传进来**——这是条件变量协议的硬性要求：`wait` 需要知道要释放/重新获取哪把锁。

#### 4.1.2 核心流程

```
wait(mtx, tm):
  1. 校验 valid()
  2. 调底层 cond_.wait(mtx, tm)
       ├─ 原子释放 mtx + 阻塞
       └─ 被唤醒 / 超时后重新持有 mtx
  3. 返回是否成功（超时返回 false）

notify(mtx) / broadcast(mtx):
  1. 校验 valid()
  2. 调底层 cond_.notify/broadcast(mtx) 唤醒对端
```

#### 4.1.3 源码精读

公共 API 在 [include/libipc/condition.h:L12-L39](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/condition.h#L12-L39) 声明，注意它禁用拷贝、只有一个不透明指针 `condition_* p_`（PIMPL），三个动作 `wait/notify/broadcast` 都把 `mutex&` 作为入参：

```cpp
bool wait(ipc::sync::mutex &mtx, std::uint64_t tm = ipc::invalid_value) noexcept;
bool notify(ipc::sync::mutex &mtx) noexcept;
bool broadcast(ipc::sync::mutex &mtx) noexcept;
```

[include/libipc/condition.h:L32-L34](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/condition.h#L32-L34) —— 这三行就是条件变量「释放锁、阻塞、唤醒」三件套的对外契约。

实现侧，[src/libipc/sync/condition.cpp:L73-L83](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/sync/condition.cpp#L73-L83) 把公共调用**原样转发**给底层 `ipc::detail::sync::condition cond_`：

```cpp
bool condition::wait(ipc::sync::mutex &mtx, std::uint64_t tm) noexcept {
    return impl(p_)->cond_.wait(mtx, tm);
}
```

也就是说公共类只是个薄壳；真正干活的是按平台 `#include` 进来的 `detail::sync::condition`。而 `detail::sync::condition` 三个后端（Win / Linux / POSIX）的 `wait/notify/broadcast` 签名完全一致，只是内部系统调用不同——这就是「统一接口、各自实现」。

> **承接 u3-l4**：[u3-l4](u3-l4-wait-model.md) 讲过的 `wait_if` 谓词循环，底层正是调用 `condition::wait`。稍后在 [src/libipc/waiter.h:L64-L74](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/waiter.h#L64-L74) 会看到它如何与 mutex 配套使用。

#### 4.1.4 代码实践（源码阅读型）

1. **实践目标**：验证「wait 必须在持有 mutex 时调用」这一协议在 libipc 内部是被严格遵守的。
2. **操作步骤**：打开 [src/libipc/waiter.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/waiter.h)，定位 `wait_if`。
3. **需要观察的现象**：函数体第一行就是 `std::lock_guard<ipc::sync::mutex> guard {lock_};`（即先持有锁），随后才在 `while` 循环里调用 `cond_.wait(lock_, tm)`。
4. **预期结果**：你会确认 `wait` 调用点一定处于 `lock_` 被持有的区间内——这正是 2.1 节协议的落地。
5. 结论：待本地确认（你只需阅读，无需运行）。

#### 4.1.5 小练习与答案

**Q1**：为什么 `condition::wait` 要把 `mutex` 作为参数，而不能像某些语言那样把锁和条件变量绑定在一起？

**参考答案**：因为同一把锁可能配合多个条件变量、或一个条件变量配合不同锁。把锁作为参数传入，让调用方在每次 `wait` 时显式声明「我要释放并重新获取的是哪把锁」，既灵活又避免隐式状态。

**Q2**：`broadcast` 之后被唤醒的多个线程，是否就一定能立刻推进？

**参考答案**：不一定。它们被唤醒后还要**重新竞争**同一把 `mtx`，所以是「排队」逐个进入临界区的；并且重新进入后必须再次用 `while` 检查条件（可能条件已被先醒的线程消费），这正是「惊群后竞争」与防虚假唤醒的体现。

---

### 4.2 Windows condition：信号量 + 计数器算法（本讲重点）

#### 4.2.1 概念说明

Windows 并没有「跨进程、可与健壮互斥量配套」的现成条件变量（`CONDITION_VARIABLE` 虽存在，但不便与 libipc 的跨进程 robust 互斥量组合）。于是 libipc 在 [src/libipc/platform/win/condition.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/condition.h) 用**三个零件**手搓了一个：

1. 一个**信号量** `sem_`：真正的阻塞/唤醒靠它。
2. 一把**内部互斥量** `lock_`：只用来保护下面这个计数器。
3. 一块**共享内存** `shm_`：里面放一个 `int32_t` 计数器 `cnt`，记录「当前有多少个线程阻塞在 `sem_` 上」。

这套算法来自微软研究院的经典论文（源码注释里给了链接）。它的精髓是：**因为信号量只能「发一个许可唤醒一个」，所以要靠计数器知道到底要发几个许可**。

#### 4.2.2 核心流程

```
wait(mtx, tm):                  # mtx 是用户传入的锁
  lock(lock_)                   # 拿内部锁，改计数器
    cnt = (cnt < 0) ? 1 : cnt + 1   # 在床的等待者 +1
  unlock(lock_)
  SignalObjectAndWait(mtx, sem_, tm)   # 原子地：释放用户的 mtx，阻塞在 sem_ 上
  mtx.lock()                    # 醒来后重新持有用户的 mtx
  if (超时/失败):                 # 没真正消费到一个许可
     lock(lock_); cnt -= 1; unlock(lock_)
  return 成功?

notify(mtx):
  lock(lock_)
    if (cnt > 0):               # 有等待者才发
       sem_.post(1)             # 只发 1 个许可 → 只唤醒 1 个
       cnt -= 1
  unlock(lock_)

broadcast(mtx):
  lock(lock_)
    if (cnt > 0):
       sem_.post(cnt)           # 发 cnt 个许可 → 唤醒全部
       cnt = 0
  unlock(lock_)
```

注意对称美：`notify` 消费 1 个许可所以 `cnt -= 1`；`broadcast` 消费掉全部许可所以 `cnt = 0`。

#### 4.2.3 源码精读

三个成员 + 计数器访问器在 [src/libipc/platform/win/condition.h:L25-L31](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/condition.h#L25-L31)：

```cpp
ipc::sync::semaphore sem_;
ipc::sync::mutex lock_;
ipc::shm::handle shm_;          // 放 int32 计数器
std::int32_t &counter() {       // 计数器就放在共享内存首地址
    return *static_cast<std::int32_t *>(shm_.get());
}
```

`open` 用三个不同后缀把三个零件挂到同一名字下，见 [src/libipc/platform/win/condition.h:L49-L65](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/condition.h#L49-L65)（`_COND_SEM_` / `_COND_LOCK_` / `_COND_SHM_`），并用 `scope_guard` 保证「部分失败时回滚已打开的零件」。

`wait` 是算法的核心，见 [src/libipc/platform/win/condition.h:L84-L104](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/condition.h#L84-L104)。关键是这一句：

```cpp
bool rs = ::SignalObjectAndWait(mtx.native(), sem_.native(), ms, FALSE) == WAIT_OBJECT_0;
```

[src/libipc/platform/win/condition.h:L97-L97](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/condition.h#L97) —— `SignalObjectAndWait` 是一个**内核级原子操作**：它先释放第一个对象（用户的 `mtx`），再在第二个对象（`sem_`）上阻塞。两步之间不会被notify 插入，从根上杜绝了 2.1 节说的「丢唤醒」。返回后用 `mtx.lock()` 重新持有用户锁（[L98](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/condition.h#L98)）；若 `rs` 为假（超时/失败），说明没拿到许可，要把刚才 `+1` 的计数补回 `-1`（[L99-L102](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/condition.h#L99-L102)）。

`notify` 见 [src/libipc/platform/win/condition.h:L106-L116](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/condition.h#L106-L116)：`cnt > 0` 时 `sem_.post(1)` 只唤醒一个。

`broadcast` 见 [src/libipc/platform/win/condition.h:L118-L128](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/condition.h#L118-L128)：

```cpp
if (cnt > 0) {
    ret = sem_.post(cnt);   // 关键：post 的次数 = 当前等待者数
    cnt = 0;
}
```

这就是本讲实践任务要解释的要点——**`broadcast` 先读出计数器 `cnt`，再 `sem_.post(cnt)` 放入 `cnt` 个许可**。因为信号量「一个许可唤醒一个阻塞者」，只有放的许可数等于等待者数，才能保证全部被唤醒；放少了会漏醒，放多了会让许可残留到下一次 `wait` 造成虚假唤醒。

#### 4.2.4 代码实践（本讲主任务）

> 任务原文：阅读 Windows `condition::broadcast`，说明它如何用计数器决定 post 多少次信号量以唤醒全部等待者。

1. **实践目标**：亲手验证 `wait / notify / broadcast` 三者如何协同维护计数器 `cnt`，从而让 `broadcast` 精确唤醒全部等待者。
2. **操作步骤**：
   - 打开 [src/libipc/platform/win/condition.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/condition.h)。
   - 在 `wait`（[L84-L104](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/condition.h#L84-L104)）里找到「`cnt = (cnt<0)?1:cnt+1`」这一行，确认每个等待者入床时计数 `+1`。
   - 在 `notify`（[L106-L116](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/condition.h#L106-L116)）里确认它只 `post(1)` 并 `cnt -= 1`。
   - 在 `broadcast`（[L118-L128](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/condition.h#L118-L128)）里确认它 `post(cnt)` 然后 `cnt = 0`。
3. **需要观察的现象**：三个函数对 `cnt` 的修改构成一个守恒的会计系统——`wait` 贷记（+1），`notify`/`broadcast` 借记（−1 或清零），且都受同一把内部 `lock_` 保护。
4. **预期结果**：你能向别人讲清楚「为什么 `broadcast` 必须读 `cnt` 再 post 那么多次」——因为信号量是按许可数唤醒的，等待者有几个就得发几个许可。设想若 `broadcast` 只 `post(1)`：3 个等待者里只有 1 个会醒，另外 2 个永久阻塞（这在 channel 的 `disconnect` 唤醒场景下是致命 bug）。
5. 平台说明：本实践为源码阅读型，Windows 以外平台可对照 [src/libipc/platform/posix/condition.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/condition.h) 的 `pthread_cond_broadcast`（它由内核一次唤醒全部，**不需要**计数器）。

#### 4.2.5 小练习与答案

**Q1**：`wait` 里 `cnt = (cnt < 0) ? 1 : cnt + 1` 的 `cnt < 0` 分支在什么情况下会触发？

**参考答案**：正常守恒情况下 `cnt` 不会为负。这是一个**防御性**分支：万一因异常路径（如 `wait` 超时回滚时计数错位、或共享内存初值非 0）导致 `cnt` 变成负数，直接把它修正为 1（即「认为当前只有我这一个等待者」），避免负数越滚越大让 `notify/broadcast` 永远以为「没人等」。

**Q2**：为什么需要一个**单独的内部锁** `lock_`，而不是直接用用户传入的 `mtx` 来保护 `cnt`？

**参考答案**：因为 `wait` 在调用 `SignalObjectAndWait` 时已经**释放**了用户的 `mtx`，此后它对 `cnt` 的任何读写（比如超时回滚的 `cnt -= 1`）都不再持有 `mtx`。所以必须有一把**独立**的、贯穿「入床计数 → 阻塞 → 醒来回滚」全过程的锁来保护 `cnt`，这就是 `lock_`。

---

### 4.3 semaphore：命名信号量的跨平台后端

#### 4.3.1 概念说明

`ipc::sync::semaphore` 是 libipc 对外暴露的信号量，接口极简——`wait(tm)` 取许可（可超时）、`post(count)` 放许可。它和 condition 一样可命名、可跨进程、PIMPL 隐藏实现。

它有两层意义：

1. 它是独立的同步原语，用户可直接用。
2. **它是 Windows condition 的一个零件**（4.2 节的 `sem_` 就是它），所以本讲必须把它讲清楚。

#### 4.3.2 核心流程

```
open(name, count):
  Win:  CreateSemaphore(初始=count, 最大=LONG_MAX, name)
  POSIX:shm 引用计数 + sem_open(name, O_CREAT, 0666, count)

wait(tm):
  Win:  WaitForSingleObject(sem, tm)      # 取一个许可，超时返回 WAIT_TIMEOUT
  POSIX:tm 无限 → sem_wait(sem)
        tm 有限 → sem_timedwait(sem, 绝对时间)

post(count):
  Win:  ReleaseSemaphore(sem, count)       # 放 count 个许可
  POSIX:循环 count 次 sem_post(sem)
```

#### 4.3.3 源码精读

公共 API 在 [include/libipc/semaphore.h:L31-L32](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/semaphore.h#L31-L32)：`wait(tm=invalid_value)` 与 `post(count=1)`。

**Windows 后端** [src/libipc/platform/win/semaphore.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/semaphore.h) 直接用内核对象：

- `open` 用 `CreateSemaphore`，初值 `count`、最大值 `LONG_MAX`（[L38-L40](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/semaphore.h#L38-L40)）。
- `wait` 用 `WaitForSingleObject`，按返回值区分成功 / 超时（`WAIT_TIMEOUT`）/ 被弃（`WAIT_ABANDONED`，呼应 [u6-l2](u6-l2-robust-mutex.md)）（[L61-L74](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/semaphore.h#L61-L74)）。
- `post` 用 `ReleaseSemaphore`，一次放 `count` 个许可（[L76-L83](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/semaphore.h#L76-L83)）。

**POSIX 后端** [src/libipc/platform/posix/semaphore_impl.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/semaphore_impl.h) 用命名信号量 `sem_t`，注意它的**双对象**设计：

```cpp
ipc::shm::handle shm_;     // ① 仅当引用计数用，承载跨进程「谁是最后一人」
sem_t *h_ = SEM_FAILED;    // ② 真正的命名信号量句柄
std::string sem_name_;     // ③ 实际 sem_open 用的名字
```

`open` 先 `shm_.acquire(name, 1)` 拿一个 1 字节共享内存做引用计数（[L39-L42](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/semaphore_impl.h#L39-L42)），再把名字加工成 `"/" + name + "_sem"`（保证 POSIX 要求的 `/` 前缀，[L45-L49](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/semaphore_impl.h#L45-L49)），最后 `sem_open(..., O_CREAT, 0666, count)`（[L50](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/semaphore_impl.h#L50)）。

`wait` 分两路（[L102-L120](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/semaphore_impl.h#L102-L120)）：无限等待走 `sem_wait`，超时走 `sem_timedwait`（注意它要的是**绝对时间**，由 4.4 节的 `make_timespec` 算出）。

`close`（[L58-L73](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/semaphore_impl.h#L58-L73)）体现了「最后一人负责拆」：`shm_.release()` 返回值 `<= 1` 时才 `sem_unlink` 真正删除内核信号量，否则只 `sem_close` 关闭本进程句柄——这套引用计数套路与 [u5-l1 shm::handle](u5-l1-shm-handle-api.md) 完全一致。

`post`（[L122-L132](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/semaphore_impl.h#L122-L132)）因为 POSIX `sem_post` 只能 `+1`，所以放 `count` 个许可要**循环** `count` 次——与 Windows 的 `ReleaseSemaphore(count)` 一步到位形成对比。

#### 4.3.4 代码实践（源码阅读型）

1. **实践目标**：理解 POSIX semaphore 为何需要「shm + sem_t」两个对象，而 Windows 只要一个 `HANDLE`。
2. **操作步骤**：对照阅读 [src/libipc/platform/posix/semaphore_impl.h:L36-L56](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/semaphore_impl.h#L36-L56)（`open`）与 [src/libipc/platform/win/semaphore.h:L35-L46](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/semaphore.h#L35-L46)（`open`）。
3. **需要观察的现象**：POSIX 版用 `shm_` 只为拿到跨进程引用计数（决定何时 `sem_unlink`），真正的计数语义在内核的 `sem_t` 里；Windows 版的内核对象自带命名与生命期管理，所以不需要额外 `shm_`。
4. **预期结果**：你能解释「POSIX 的 `shm_.release() <= 1` 才 unlink」这一行的作用——避免还有进程持有信号量时就把它删掉。
5. 结论：待本地确认。

#### 4.3.5 小练习与答案

**Q1**：`sem_post(count)` 在 POSIX 上为什么要写成循环？

**参考答案**：POSIX `sem_post` 的语义是计数 `+1` 且只能 `+1`，没有「一次加 N」的重载；所以要放 `count` 个许可必须循环调用 `count` 次。Windows 的 `ReleaseSemaphore` 原生支持一次加 N，所以一步到位。

**Q2**：POSIX `open` 里把名字改成 `"/" + name + "_sem"`，加 `/` 前缀的目的是什么？

**参考答案**：POSIX 命名信号量要求名字以 `/` 开头、且不含其它 `/`（FreeBSD 等平台强制）。libipc 内部名字不一定带 `/`，所以统一补一个前导 `/`；同时加 `_sem` 后缀是为了与同名的共享内存对象区分，避免命名冲突（注释里特别点出这一点）。

---

### 4.4 超时等待与编译期后端分派

#### 4.4.1 概念说明

本模块把两个横切所有后端的要点讲清楚：

1. **超时怎么算**：libipc 对外用「相对毫秒」（`std::uint64_t tm`，`invalid_value` 表示无限），但 POSIX/Linux 的 `pthread_cond_timedwait` / `sem_timedwait` 要的是**绝对时间点**（从 1970 起的 `timespec`），所以要做一次换算。
2. **三套后端怎么选**：condition 走 Win / Linux(a0) / POSIX(pthread) 三选一；semaphore 走 Win / POSIX 二选一（Linux 也用 POSIX）。

#### 4.4.2 核心流程

```
相对毫秒 tm  ──make_timespec──▶  绝对 timespec（当前时间 + tm）
                                  │
              ┌───────────────────┼───────────────────┐
              ▼                   ▼                   ▼
     pthread_cond_timedwait   sem_timedwait     Windows 用相对 ms（INFINITE）
     (POSIX condition)        (POSIX sem)       （SignalObjectAndWait / WaitForSingleObject）

后端分派（编译期 #if defined(LIBIPC_OS_*)）：
  condition: WIN→win/condition.h  LINUX→linux/condition.h  QNX/FREEBSD→posix/condition.h
  semaphore: WIN→win/semaphore.h  LINUX/QNX/FREEBSD→posix/semaphore_impl.h
```

#### 4.4.3 源码精读

**超时换算**在 [src/libipc/platform/posix/get_wait_time.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/get_wait_time.h)。`calc_wait_time`（[L16-L28](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/get_wait_time.h#L16-L28)）用 `gettimeofday` 取当前时间，再叠加上 `tm` 毫秒，处理好纳秒进位：

\[ \text{ts.tv\_sec} = \text{now.tv\_sec} + \lfloor \text{tm}/1000 \rfloor + \lfloor \text{nsec}/10^9 \rfloor \]

`make_timespec`（[L30-L38](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/get_wait_time.h#L30-L38)）包装它并在失败时抛 `system_error`。换算结果喂给 `pthread_cond_timedwait`（[src/libipc/platform/posix/condition.h:L122-L131](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/condition.h#L122-L131)）或 `sem_timedwait`（[src/libipc/platform/posix/semaphore_impl.h:L110-L118](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/semaphore_impl.h#L110-L118)）。注意两者都对 `ETIMEDOUT` 静默返回 false、其它错误才打日志——超时是「正常业务结果」而非错误。

**后端分派**在两个公共 `.cpp` 顶部。condition 的分派见 [src/libipc/sync/condition.cpp:L8-L16](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/sync/condition.cpp#L8-L16)：

```cpp
#if defined(LIBIPC_OS_WIN)
#include "libipc/platform/win/condition.h"
#elif defined(LIBIPC_OS_LINUX)
#include "libipc/platform/linux/condition.h"
#elif defined(LIBIPC_OS_QNX) || defined(LIBIPC_OS_FREEBSD)
#include "libipc/platform/posix/condition.h"
#else
#   error "Unsupported platform."
#endif
```

semaphore 的分派见 [src/libipc/sync/semaphore.cpp:L8-L14](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/sync/semaphore.cpp#L8-L14)，注意它把 **Linux 也归到 POSIX 那一支**（`LIBIPC_OS_LINUX || LIBIPC_OS_QNX || LIBIPC_OS_FREEBSD` → `posix/semaphore_impl.h`）。

**三套 condition 后端对照**（同一份接口 `wait/notify/broadcast`）：

| 后端 | 文件 | 阻塞/唤醒实现 | 是否需要手写计数器 |
|------|------|--------------|------------------|
| Win32 | [win/condition.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/win/condition.h) | `SignalObjectAndWait` + 信号量 + 计数器 | **是**（4.2 节） |
| Linux(a0) | [linux/condition.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/condition.h) | futex，`a0_cnd_wait/signal/broadcast` | 否（库内部处理） |
| POSIX(pthread) | [posix/condition.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/condition.h) | `pthread_cond_wait/signal/broadcast`（`PTHREAD_PROCESS_SHARED`） | 否（内核处理） |

Linux 版（[src/libipc/platform/linux/condition.h:L21-L41](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/linux/condition.h#L21-L41)）继承 `sync::obj_impl<a0_cnd_t>`，把 `a0_cnd_t` 放进共享内存（机制同 [u6-l2](u6-l2-robust-mutex.md) 的 a0 互斥量），`broadcast` 一次调用 `a0_cnd_broadcast` 唤醒全部，**不需要** Windows 那套计数器。POSIX 版同理（[src/libipc/platform/posix/condition.h:L148-L157](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/condition.h#L148-L157) 的 `pthread_cond_broadcast`），并且用 `pthread_condattr_setpshared(PTHREAD_PROCESS_SHARED)` 让条件变量跨进程共享（[L69-L72](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/condition.h#L69-L72)）。

> **为什么 Windows 要手写而 POSIX/Linux 不用？** 因为 pthread 的 `pthread_cond_broadcast` 和 a0 的 `a0_cnd_broadcast` 由底层（内核/futex）一次性唤醒所有等待者；而 Windows 的信号量是「一许可唤醒一人」，没有「一次全醒」的原语，只能靠应用层计数器补足这个缺口。这正是 4.2 节算法存在的根本原因。

#### 4.4.4 代码实践（源码阅读型）

1. **实践目标**：追踪一次「超时 50ms 的 `semaphore::wait`」从公共 API 到系统调用的完整路径。
2. **操作步骤**：
   - 入口 [src/libipc/sync/semaphore.cpp:L71-L73](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/sync/semaphore.cpp#L71-L73) 转发到底层。
   - POSIX 分支 [src/libipc/platform/posix/semaphore_impl.h:L110-L118](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/semaphore_impl.h#L110-L118)：`tm != invalid_value` → `make_timespec(tm)` → `sem_timedwait`。
   - 时间换算 [src/libipc/platform/posix/get_wait_time.h:L16-L28](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/platform/posix/get_wait_time.h#L16-L28)。
3. **需要观察的现象**：相对的 `50ms` 在到达系统调用前被换算成了「当前墙钟时间 + 50ms」的绝对 `timespec`。
4. **预期结果**：你能说清为何换算成绝对时间——因为 `sem_timedwait` / `pthread_cond_timedwait` 的 API 契约就是绝对时间点（这样即使函数被信号中断后重启，超时点仍稳定）。
5. 结论：待本地确认。

#### 4.4.5 小练习与答案

**Q1**：为什么 POSIX/Linux 的 timedwait 用**绝对时间**而不是像 Windows 那样用**相对毫秒**？

**参考答案**：绝对时间让「等待 + 被信号中断 + 重新等待」的总时长仍准确——每次重启都用同一个固定的截止时刻重新计算剩余时间，不会因为中断次数多而累计变长。Windows 的 `WaitForSingleObject` 等用的是相对毫秒，由内核保证单次等待的语义。

**Q2**：如果要在 libipc 新增一个平台（比如某实时 OS），需要改哪几处？

**参考答案**：(1) 在 `detect_plat.h` 加该平台的 `LIBIPC_OS_*` 检测；(2) 新建 `src/libipc/platform/<new>/condition.h` 与 `semaphore.h`，实现同名同签名的 `ipc::detail::sync::condition` / `semaphore`；(3) 在 `src/libipc/sync/condition.cpp` 与 `semaphore.cpp` 的 `#elif` 链里加一条 `#include`。公共 API 与上层 `waiter` 完全不用动——这正是「统一接口、各自实现、编译期分流」的好处。

---

## 5. 综合实践

把本讲三块知识（condition 协议、Windows 计数器算法、semaphore 语义）串起来，做一个**纸面推演**（源码阅读型，无需运行）：

**场景**：在某 Windows 机器上，3 个线程同时对同一个 `condition c` 调用 `wait(mtx, invalid_value)`（无限等待），随后主线程调用 `c.broadcast(mtx)`。

请按下面步骤推演并填空：

1. 三个 `wait` 各自在内部 `lock_` 保护下修改计数器 `cnt`。共享内存初值为 0，三个线程依次进入后 `cnt = ?`。
2. 主线程 `broadcast` 读到 `cnt = ?`，于是 `sem_.post(?)`，并把 `cnt` 置为 `?`。
3. 信号量被放了这么多许可后，3 个阻塞在 `SignalObjectAndWait` 上的线程会怎样？它们醒来后各自还要做什么（提示：`mtx.lock()`）？
4. **反向验证**：假如实现写错了，`broadcast` 里误写成 `sem_.post(1)`，会发生什么？（提示：剩余线程会永久阻塞，这正是 channel `disconnect` 无法唤醒所有 receiver 的故障表现，呼应 [u3-l4](u3-l4-wait-model.md) 的 `quit_waiting`。）

**参考答案**：

1. `cnt` 依次变为 1、2、3。
2. `broadcast` 读到 `cnt = 3`，`sem_.post(3)`，`cnt = 0`。
3. 3 个线程各消费 1 个许可而被唤醒；各自执行 `mtx.lock()` 重新竞争用户锁，于是排队逐个返回。这就是「broadcast 全唤醒 + 重新抢锁」。
4. 只 `post(1)` 只能唤醒 1 个，另外 2 个永远阻塞——`cnt` 还停在 2，后续的 `notify/broadcast` 行为也会错乱。这证明了计数器守恒的必要性。

> 进阶（可运行，可选）：在 Linux 上写一个最小程序，用 `ipc::sync::semaphore s{"my-sem", 0}`，一个线程 `s.wait()`，另一线程 `s.post(3)` 后连写日志，验证「post 多次 → 可 wait 多次」。这需要先按 [u1-l2](u1-l2-build-and-run.md) 编出 libipc。

## 6. 本讲小结

- **condition 必须配对 mutex**：`wait(mtx)` 在持有 `mtx` 时调用，靠「原子释放锁 + 阻塞」防丢唤醒；`notify/broadcast` 唤醒后等待者还要重新抢锁并 `while` 复查条件。
- **Windows condition 是手搓的**：用「信号量 `sem_` + 内部锁 `lock_` + 共享内存计数器 `cnt`」三件套，核心是 `SignalObjectAndWait` 做原子释放-阻塞。
- **计数器守恒**：`wait` 让 `cnt + 1`，`notify` 消费 1 个许可 `cnt -= 1`，`broadcast` 消费全部许可 `cnt = 0`；`broadcast` 之所以 `post(cnt)`，是因为信号量「一许可唤醒一人」，必须放够数才能全唤醒。
- **semaphore 两套后端**：Windows 用内核 `CreateSemaphore/WaitForSingleObject/ReleaseSemaphore`；POSIX/Linux 用命名 `sem_t`（`sem_open/sem_wait/sem_post`），并额外用一块 `shm` 做引用计数决定何时 `sem_unlink`。
- **超时换算**：对外是相对毫秒，POSIX/Linux 内部用 `make_timespec` 换算成绝对 `timespec`（`gettimeofday` + 进位），因为 `pthread_cond_timedwait` / `sem_timedwait` 要绝对时间；`ETIMEDOUT` 被当作正常业务结果静默返回 false。
- **编译期分派**：condition 走 Win/Linux(a0)/POSIX(pthread) 三选一，semaphore 走 Win/POSIX 二选一；Windows 没有原生「一次全醒」原语是它必须手写计数器的根本原因。

## 7. 下一步学习建议

- 进入 **[u6-l4 detail::waiter：channel 的等待/通知核心](u6-l4-waiter.md)**：本讲的 `condition` + `mutex` 在那里被组装成 libipc 内部的 `waiter`（再加一个原子 `quit_` 标志），它是 [u3-l4](u3-l4-wait-model.md) 三类 waiter（`cc_/wt_/rd_`）的真正实体，把「condition 协议」落地成 channel 的通知机制。
- 若想深究健壮性：回到 [u6-l2](u6-l2-robust-mutex.md) 与本讲的 Linux a0 后端对照，理解 a0 如何用一套 futex 同时实现 robust mutex 与 condition。
- 若关心并发正确性细节：可预习 **[u8-l1 内存序、伪共享与缓存行](u8-l1-memory-ordering.md)**，本讲计数器的并发安全目前靠 `lock_` 互斥保证，而 u8 会讲更轻量的原子序方案。
