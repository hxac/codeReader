# 等待模型：三类 waiter 与渐进退避

> 前置承接：本讲建立在 [u3-l1](u3-l1-send-recv-data-path.md) 之上。你已经知道一条消息从 `send` 走到 `recv` 要经过 `detail_impl` 桥接层、分片循环与共享内存 `queue`；也知道 `conn_info_head` 里持有「三类 `waiter`」。但那篇讲义把「等待」当作黑盒——队列满时 `send` 为什么不立刻报错、队列空时 `recv` 为什么能一直阻塞又不烧满 CPU？本讲就拆开这个黑盒。

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `cc_waiter_` / `wt_waiter_` / `rd_waiter_` 三类等待器各自的职责，以及「谁等、谁唤醒」。
- 逐行讲清 `wait_for` 模板如何把「自旋」和「条件变量阻塞」缝在一起。
- 解释 `sleep` 与 `yield` 两套退避函数的分级阈值（4 / 16 / 32），并说明为什么不在第一次冲突就立即 `sleep`。
- 理解 `broadcast` 唤醒机制，以及 `disconnect` 如何通过 `quit_waiting` 让对端退出阻塞。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**共享内存 IPC 为什么需要「等待」。** libipc 的消息走的是共享内存里的无锁循环队列（[u4](u4-l1-queue-abstraction.md) 会详讲）。生产者 `send` 时若队列已满、消费者 `recv` 时若队列已空，不能像管道那样让操作系统帮你排队——库必须自己决定「接下来怎么办」。两个极端都不好：立刻返回失败会让上层频繁重试、浪费 CPU；死循环自旋（busy-wait）又会在真正空闲时把一个核跑满。libipc 的选择是「先轻量自旋若干次，再转入操作系统级阻塞」。

**忙等（busy-wait）与阻塞（block）的代价对比。** 忙等是线程在用户态反复读一个标志，检测状态变化在**纳秒级**，代价是持续占用 CPU；阻塞（条件变量 / 信号量）要陷入内核、做上下文切换，单次开销在**微秒级**，但不占 CPU。共享内存 IPC 的卖点就是低延迟，所以「短时忙等换低延迟、长时阻塞换省 CPU」是核心设计哲学。

**条件变量（condition variable）的最小心智模型。** 一个条件变量总要配一把互斥锁使用：等待方加锁、检查条件、若不满足则 `wait`（原子地「解锁 + 挂起」，被唤醒后再「加锁 + 返回」）；通知方加锁（或做个内存屏障）、改条件、`notify`/`broadcast` 唤醒等待方。本讲里的 `waiter` 就是把「条件变量 + 互斥锁 + 一个退出标志」打包成一个跨进程的小组件，底层实现见 [u6-l3](u6-l3-condition-semaphore.md) 与 [u6-l4](u6-l4-waiter.md)。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/libipc/ipc.cpp](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp) | 仓库主实现。本讲关注三类 `waiter` 的声明与命名、`wait_for` 模板、`send`/`recv` 中「等待 + 广播」的配对。 |
| [src/libipc/waiter.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/waiter.h) | `detail::waiter` 的全部实现：`open`/`wait_if`/`notify`/`broadcast`/`quit_waiting`。 |
| [include/libipc/rw_lock.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/rw_lock.h) | `yield` 与 `sleep` 两套退避函数、`IPC_LOCK_PAUSE_` 硬件暂停指令。注意：这个头文件其实同时定义了退避工具与自旋/读写锁。 |
| [src/libipc/sync/waiter.cpp](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/sync/waiter.cpp) | `waiter::init()` 的静态初始化入口。 |
| [include/libipc/condition.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/condition.h) | `condition` 的公共接口（`wait`/`notify`/`broadcast` 签名），是 `waiter` 的底层。 |
| [include/libipc/def.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/def.h) | `invalid_value`（无限等待哨兵）、`default_timeout`（100ms）等常量。 |

## 4. 核心概念与源码讲解

### 4.1 三类 waiter 的职责分工

#### 4.1.1 概念说明

`conn_info_head` 持有三个 `detail::waiter` 成员，对应通信生命周期里的三种「需要等」的场景：

| 等待器 | 共享内存命名前缀 | 谁会等它 | 谁唤醒它 | 触发条件 |
| --- | --- | --- | --- | --- |
| `cc_waiter_` | `CC_CONN__` | 等待**足够多的接收者上线**的发送方 | 新接收者 `connect` 成功时 | `conn_count() < r_count` |
| `wt_waiter_` | `WT_CONN__` | 队列**写满**时的发送方 | 接收者 `pop` 出一条消息后 | `que->push` 失败（队列满） |
| `rd_waiter_` | `RD_CONN__` | 队列**读空**时的接收方 | 发送者 `push` 进一条消息后 | `que->pop` 失败（队列空） |

记住一条对称性：**生产者等 `wt_waiter_`、消费者等 `rd_waiter_`**；生产者 push 完唤醒 `rd_waiter_`（通知消费者「有数据了」），消费者 pop 完唤醒 `wt_waiter_`（通知生产者「有空位了」）。这就是经典的生产者-消费者配对，`cc_waiter_` 则是额外加的「连接握手」等待器。

#### 4.1.2 核心流程

以一次「发送方先启动、等待接收方上线」为例：

```
发送方                        共享内存                      接收方
  |                              |                            |
  | send ... 但发现没有 receiver  |                            |
  | (或主动 wait_for_recv)        |                            |
  |------ 等 cc_waiter_ ------->|                            |
  |                              |<------ connect() --------|
  |                              |   que->connect() 成功      |
  |                              |------ cc_waiter_.broadcast()
  |<-------- 唤醒 ---------------|                            |
  | 现在 conn_count 达标，开始 push                            |
  |------ 等 wt_waiter_(若满) -->|                            |
  |                              |<------ pop ----------|
  |                              |------ wt_waiter_.broadcast()
  |<-------- 唤醒，再 push ----->|                            |
  |------ push 后 rd_waiter_.broadcast() -------- 唤醒 recv --|
```

#### 4.1.3 源码精读

三个 `waiter` 成员的声明位于 `conn_info_head`（[src/libipc/ipc.cpp:112-118](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L112-L118)）：

```cpp
struct conn_info_head {
    std::string prefix_;
    std::string name_;
    msg_id_t    cc_id_;
    ipc::detail::waiter cc_waiter_, wt_waiter_, rd_waiter_;  // 三类等待器
    ipc::shm::handle acc_h_;
    ...
```

它们在 `init()` 里被赋予跨进程可见的名字（[src/libipc/ipc.cpp:125-129](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L125-L129)）。注意每个 `waiter` 用不同的前缀 `CC_CONN__` / `WT_CONN__` / `RD_CONN__`，再经 `make_prefix` 拼上通道 `name_`，确保不同通道、不同用途的等待器互不干扰：

```cpp
void init() {
    if (!cc_waiter_.valid()) cc_waiter_.open(ipc::make_prefix(prefix_, "CC_CONN__", name_).c_str());
    if (!wt_waiter_.valid()) wt_waiter_.open(ipc::make_prefix(prefix_, "WT_CONN__", name_).c_str());
    if (!rd_waiter_.valid()) rd_waiter_.open(ipc::make_prefix(prefix_, "RD_CONN__", name_).c_str());
    ...
```

`cc_waiter_` 的典型用法是公共接口 `wait_for_recv`（[src/libipc/ipc.cpp:516-524](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L516-L524)）——发送方等待接收者数量达到 `r_count`：

```cpp
static bool wait_for_recv(ipc::handle_t h, std::size_t r_count, std::uint64_t tm) {
    auto que = queue_of(h);
    if (que == nullptr) return false;
    return wait_for(info_of(h)->cc_waiter_, [que, r_count] {
        return que->conn_count() < r_count;   // 谓词：接收者还不够多
    }, tm);
}
```

而 `cc_waiter_` 的唤醒发生在 `reconnect` 里——新接收者连上后立刻广播（[src/libipc/ipc.cpp:491-494](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L491-L494)）：

```cpp
if (que->connect()) { // wouldn't connect twice
    info_of(*ph)->cc_waiter_.broadcast();
    return true;
}
```

`wt_waiter_` 与 `rd_waiter_` 的等待/唤醒配对在 4.4 节展开。

#### 4.1.4 代码实践

**实践目标**：在源码里把「三类 waiter 的等待点与唤醒点」一一对应找出来。

**操作步骤**：

1. 在 `src/libipc/ipc.cpp` 中搜索 `cc_waiter_`、`wt_waiter_`、`rd_waiter_`，各出现于哪几行。
2. 对每个等待器，分别记录：谁调用了 `wait_for(...waiter_, ...)`（等待点）、谁调用了 `...waiter_.broadcast()`（唤醒点）。
3. 填一张「等待点 → 唤醒点」的对照表。

**预期结果**（可自行核对）：

- `cc_waiter_`：等待点在 `wait_for_recv`，唤醒点在 `reconnect`（新接收者连上）。
- `wt_waiter_`：等待点在 `send`/`try_send`（push 失败），唤醒点在 `recv`（pop 成功后）。
- `rd_waiter_`：等待点在 `recv`（pop 失败），唤醒点在 `send`/`try_send`（push 成功后）。

**待本地验证**：如果你给三个 `broadcast` 各加一行 `printf`（仅用于观察，不改逻辑），运行 `send_recv` demo 时应能看到「先 connect 广播 → 之后每收一条消息对应一次 rd/wt 广播」的交错顺序。

#### 4.1.5 小练习与答案

**练习 1**：为什么需要 `cc_waiter_`，而不能让发送方在 `conn_count == 0` 时直接返回失败？

**参考答案**：跨进程通信里，发送方与接收方的启动顺序不确定。若直接失败，上层就得自己轮询重试，既浪费又难用。`cc_waiter_` 提供了一个「阻塞等到对端就绪」的原语，让发送方可以优雅地等接收方上线，这正是 `ipc::channel::wait_for_recv` 的语义来源。

**练习 2**：发送方等的是 `wt_waiter_` 而接收方等的是 `rd_waiter_`，这个命名是否反直觉？

**参考答案**：略有，但可这样记：`wt_` = **w**ai**t**er for **w**ri**t**e side（写方在等队列腾出空间），`rd_` = **r**ea**d** side（读方在等数据到来）。或者按「唤醒者」记：接收者读完会唤醒 `wt_waiter_`，发送者写完会唤醒 `rd_waiter_`。

---

### 4.2 wait_for 模板：连接自旋与阻塞的桥梁

#### 4.2.1 概念说明

`wait_for` 是 `send`/`recv` 与底层 `waiter` 之间的中间层，只有短短 14 行，却是整个等待模型的核心。它解决一个问题：**何时该自旋、何时该真正阻塞**。它的策略是——先用 `sleep(k, F)` 自旋若干轮（不拿锁、不进内核），自旋够了（计数器 `k` 达到阈值）才执行传入的回调 `F`，而 `F` 就是真正会阻塞的 `waiter.wait_if(...)`。

#### 4.2.2 核心流程

把 `wait_for` 的执行过程拆成「阶段」（设 `pred()` 表示「还需要继续等」，即条件尚未满足）：

```
wait_for(waiter, pred, tm):
  若 tm == 0:                 # 非阻塞尝试路径（try_recv 等）
      直接返回 !pred()         # 成功拿到就 true，拿不到就 false，不等
  否则循环（k 从 0 开始）:
      若 pred() 为假: 退出循环，返回 true   # 条件已满足，根本没等
      调 sleep(k, F):
          若 k < 32: yield()，k++           # 阶段① 忙等，不进内核
          若 k >= 32: 执行 F():             # 阶段② 真正阻塞
              F = wait_if(pred, tm)          #   加锁 + 循环 + cond_.wait
              F 返回后把 k 重置为 0
      若 F 超时: 返回 false
      若 k 被重置为 0: 跳出循环              # 已经阻塞过一轮，交给 wait_if 兜底
  返回 true
```

关键在于：**`pred()` 是带副作用的**。在 `recv` 里，谓词其实是 `!que->pop(msg)`——它一边判断一边真的去 `pop`。所以「自旋」阶段并不只是空转，而是反复重试 `pop`，一旦成功就立刻返回，连一次阻塞都不发生。

#### 4.2.3 源码精读

完整实现见 [src/libipc/ipc.cpp:378-391](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L378-L391)：

```cpp
template <typename W, typename F>
bool wait_for(W& waiter, F&& pred, std::uint64_t tm) {
    if (tm == 0) return !pred();                       // (A) 非阻塞
    for (unsigned k = 0; pred();) {                    // (B) 还需要等就循环
        bool ret = true;
        ipc::sleep(k, [&k, &ret, &waiter, &pred, tm] {
            ret = waiter.wait_if(std::forward<F>(pred), tm);  // (D) 真正阻塞
            k   = 0;                                            // (E) 阻塞后重置
        });
        if (!ret) return false; // timeout or fail     // (F) 超时
        if (k == 0) break; // k has been reset         // (G) 阻塞过一轮就退出
    }
    return true;
}
```

逐行对照：

- **(A)** `tm == 0`：`try_recv` 就是这么调用的（见 [src/libipc/ipc.cpp:739-741](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L739-L741) 的 `return recv(h, 0);`）。此时只求值一次 `pred()` 并取反：`pred` 为假（条件满足）返回 `true`，为真返回 `false`，绝不等待。
- **(B)** 循环条件就是谓词本身：只要「还需要等」就继续。注意每次循环顶部都会重新求值 `pred()`，这正是自旋阶段反复重试队列操作的地方。
- **(C)/(D)** `sleep(k, F)` 在 `k < 32` 时只 `yield` 不执行 `F`；`k >= 32` 时才执行 `F`，而 `F` 就是 `wait_if`。`wait_if` 内部是「加锁 + `while(!quit_ && pred()) cond_.wait()`」，是真正会挂起线程的内核级阻塞。
- **(E)** 阻塞返回后把 `k` 清零。
- **(G)** 因为 `k` 被清零（说明确实走过阻塞分支），直接 `break` 退出循环。`wait_if` 内部已经反复重试过谓词，外层不必再转圈。

`wait_if` 本身定义在 `waiter` 类里（[src/libipc/waiter.h:64-74](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/waiter.h#L64-L74)）：

```cpp
template <typename F>
bool wait_if(F &&pred, std::uint64_t tm = ipc::invalid_value) noexcept {
    LIBIPC_UNUSED std::lock_guard<ipc::sync::mutex> guard {lock_};
    while ([this, &pred] {
                return !quit_.load(std::memory_order_relaxed)
                    && std::forward<F>(pred)();
            }()) {
        if (!cond_.wait(lock_, tm)) return false;   // 超时返回 false
    }
    return true;
}
```

它的循环条件是「没退出 **且** 谓词为真」——只要 `quit_` 被置位或 `pred()` 变假，就立刻返回 `true`；`cond_.wait` 返回 `false`（超时）则返回 `false`。`cond_.wait(lock_, tm)` 的接口见 [include/libipc/condition.h:32-34](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/condition.h#L32-L34)，`tm` 默认 `invalid_value` 表示无限等待。

#### 4.2.4 代码实践

**实践目标**：手工演算一次 `wait_for` 的执行轨迹，理解「自旋 32 次后才阻塞」。

**操作步骤**：

1. 假设 `recv` 调用 `wait_for(rd_waiter_, pred=`「pop 失败」`, tm=invalid_value)`，而队列此刻为空、对端迟迟不发包。
2. 逐轮填写下表，记录进入循环时 `k` 的值、`sleep` 做了什么：

| 循环轮次 | 进入时 k | pred() | sleep 行为 | 循环结束后 k |
| --- | --- | --- | --- | --- |
| 第 1 轮 | 0 | true（空） | k<32 → yield，k++ | 1 |
| 第 2 轮 | 1 | true | yield，k++ | 2 |
| … | … | … | … | … |
| 第 32 轮 | 31 | true | yield，k++ | 32 |
| 第 33 轮 | 32 | true | k>=32 → 执行 F=`wait_if`（阻塞）→ 唤醒后 k=0 | 0 → break |

**需要观察的现象**：前 32 轮从不获取 `waiter` 的锁、也不进内核；直到第 33 轮才真正调用 `cond_.wait` 把线程挂起。

**预期结果**：如果在第 32 轮之内对端恰好 push 了一条消息，那么 `pred()`（即 `pop`）会在某轮成功，循环直接退出、返回 `true`，**全程零阻塞**。这正是低延迟路径。

#### 4.2.5 小练习与答案

**练习 1**：把 `wait_for` 里的 `sleep(k, F)` 直接换成 `waiter.wait_if(pred, tm)`（去掉自旋阶段），功能上还能工作吗？代价是什么？

**参考答案**：功能仍正确（`wait_if` 内部有谓词循环），但每次条件未满足都会立刻进内核阻塞。在消息高频流转的场景下，状态往往在纳秒内就被对端解除，立即阻塞会引入大量上下文切换，显著抬高延迟。自旋阶段正是为了在「马上就能拿到」的常见情况下避开系统调用。

**练习 2**：`if (k == 0) break;` 这一行能不能去掉？为什么它必须和 `k = 0;`（E 行）配合？

**参考答案**：不能简单去掉。`k = 0` 是在「已经执行过 `wait_if` 阻塞」之后设置的标记，`break` 据此判断「这一轮已经真正阻塞过、`wait_if` 内部已反复重试过谓词」，从而退出外层循环。若去掉 `break`，外层 `for` 又会从 `k=0` 重新开始自旋 32 轮——在谓词已满足时白白多 spin 一轮；二者配合保证「至多阻塞一轮，其余交给 `wait_if`」。

---

### 4.3 sleep / yield 的渐进退避策略

#### 4.3.1 概念说明

libipc 在 [include/libipc/rw_lock.h](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/rw_lock.h) 里提供了**两套**退避函数，分别服务两类场景：

- `yield(k)`：给**纯自旋锁**（`spin_lock`、`rw_lock`、CAS 循环）用。这类场景没有条件变量可用，退避只能「空转 → 暂停指令 → 让出 CPU → 睡眠」，最终靠 `sleep_for` 挂起。
- `sleep(k, F)`：给 `wait_for` 用。它先自旋若干轮，之后**执行回调 `F`**（即条件变量阻塞），把「最终退避」交给操作系统。

二者共享同一个关键阈值 `32`，体现的是一致的退避哲学：**先轻后重、逐级升级**。

#### 4.3.2 核心流程

`yield(k)` 是四级阶梯（[include/libipc/rw_lock.h:62-74](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/rw_lock.h#L62-L74)）。用分段函数表示：

\[
\text{yield}(k) =
\begin{cases}
\text{什么都不做}, & k < 4 \\
\text{PAUSE 指令（硬件暂停）}, & 4 \le k < 16 \\
\text{std::this\_thread::yield（让出时间片）}, & 16 \le k < 32 \\
\text{sleep\_for(1ms) 后 return（重置）}, & k \ge 32
\end{cases}
\]

前三级结束后都会 `++k`，最后一级直接 `return`（不再自增，相当于把控制权交还调用方的下一轮循环）。`sleep(k, F)` 则是两级（[include/libipc/rw_lock.h:76-86](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/rw_lock.h#L76-L86)）：

\[
\text{sleep}(k, F) =
\begin{cases}
\text{yield(); } k\text{++}, & k < 32 \\
F();\ \text{return（不再 }k\text{++）}, & k \ge 32
\end{cases}
\]

二者的对比：

| 维度 | `yield(k)` | `sleep(k, F)` |
| --- | --- | --- |
| 适用场景 | 纯自旋锁 / CAS 循环 | `wait_for`（配合条件变量） |
| 阈值前的动作 | 4 级：空转/PAUSE/yield | 1 级：`yield` |
| 达到阈值后 | `sleep_for(1ms)`（继续忙等，只是睡 1ms） | 执行 `F`（条件变量阻塞，彻底挂起） |
| 是否依赖操作系统阻塞原语 | 否（自己用 `sleep_for` 模拟） | 是（依赖 `condition`） |

为什么 `yield` 用了 4 级而 `sleep` 只用 1 级？因为 `sleep` 的「重退避」是真正的条件变量阻塞，由操作系统在事件到来时精准唤醒；而 `yield` 没有「事件」，只能靠自己估算睡多久（1ms），所以需要更细的前期阶梯来减少无谓的睡眠。

#### 4.3.3 源码精读

最低一级的 `IPC_LOCK_PAUSE_` 是给处理器的「自旋提示」指令（[include/libipc/rw_lock.h:18-47](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/rw_lock.h#L18-L47)）：x86 上是 `pause`、ARM 上是 `yield` 指令。它告诉 CPU「我在自旋等待」，从而降低功耗、避免与超线程兄弟核争抢执行资源，并减少流水线乱序带来的内存序违规惩罚。

`yield` 与 `sleep` 的实现（[include/libipc/rw_lock.h:62-93](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/rw_lock.h#L62-L93)）：

```cpp
template <typename K>
inline void yield(K& k) noexcept {
    if (k < 4)  { /* Do nothing */ }
    else if (k < 16) { IPC_LOCK_PAUSE_(); }
    else if (k < 32) { std::this_thread::yield(); }
    else {
        std::this_thread::sleep_for(std::chrono::milliseconds(1));
        return;                 // 注意：这里直接 return，不再 ++k
    }
    ++k;
}

template <std::size_t N = 32, typename K, typename F>
inline void sleep(K& k, F&& f) {
    if (k < static_cast<K>(N)) {
        std::this_thread::yield();
    }
    else {
        static_cast<void>(std::forward<F>(f)());   // 执行阻塞回调 F
        return;                                    // 不再 ++k
    }
    ++k;
}
```

两个细节值得注意：

1. `sleep` 的模板参数 `N` 默认 32，也就是「自旋 32 轮才阻塞」。这和 `yield` 的最后一级阈值 `k < 32` 对齐。
2. 两者在「最终退避」分支都 `return` 而不 `++k`，所以计数器不会无限增长——进入重退避后行为稳定。

#### 4.3.4 代码实践

**实践目标**：理解阈值 4 / 16 / 32 的取舍。

**操作步骤**：

1. 阅读 `yield(k)`，回答：为什么不在 `k == 0`（第一次冲突）就立即 `sleep_for`？
2. 假设把第一个阈值从 4 改成 0（即第一次冲突就 `PAUSE`），或把睡眠阈值从 32 改成 2，分别会带来什么后果？

**需要观察的现象（推理）**：

- 第一次冲突就 `sleep_for(1ms)`：在锁竞争只持续几十纳秒的常见情况下，会平白睡 1ms，延迟暴涨约 1000 倍。
- 睡眠阈值改成 2：自旋窗口太短，轻微的瞬时竞争也会被升级成 `sleep_for`，同样抬高延迟并增加上下文切换。

**预期结果**：阈值设计反映「乐观假设」——认为冲突大概率在很短时间内解除，所以先用最便宜的手段（空转/PAUSE）探一探；只有连续探了 32 次都没成功，才承认「这是真竞争」，升级到 `sleep_for` 或条件变量阻塞。

**待本地验证**：若你在 `spin_lock::lock` 里对 `yield(k)` 的四级分支各加计数器，在高竞争的 benchmark 中应能看到绝大多数冲突在前两级（空转/PAUSE）就解决，进入 `sleep_for` 的比例很低。

#### 4.3.5 小练习与答案

**练习 1**：`spin_lock` 用 `yield(k)`、而 `wait_for` 用 `sleep(k, F)`。为什么不能统一用一种？

**参考答案**：`spin_lock` 是非常短临界区的轻量锁，且不持有任何「事件源」，只能靠 `yield` 的四级退避（最终 `sleep_for`）来避免 CPU 空烧；`wait_for` 背后有条件变量，能在「对端广播」时被精准唤醒，所以用 `sleep` 自旋若干轮后转入条件变量阻塞，比 `sleep_for(1ms)` 的轮询更高效、更省 CPU。

**练习 2**：`sleep(k, F)` 在 `k >= 32` 后执行 `F()` 并 `return`，不再 `++k`。`wait_for` 又在 `F` 之后把 `k` 显式置 0。这两处「不增长 / 清零」为什么重要？

**参考答案**：它们共同保证「一次 `wait_for` 调用至多进入一次真正的阻塞」。若 `k` 继续增长或不清零，外层 `for` 循环的行为会变得依赖历史轮次，甚至永远进不了阻塞分支或反复阻塞。清零后配合 `if (k == 0) break`，语义干净：自旋够数 → 阻塞一轮 → 退出。

---

### 4.4 broadcast 唤醒与 disconnect 解阻塞

#### 4.4.1 概念说明

「等」只是半件事，另半件是「唤醒」。libipc 的唤醒一律用 `broadcast`（唤醒所有等待者）而非 `notify`（只唤醒一个）。原因有二：一是广播模式下一条消息要被多个接收者各读一次，需要唤醒所有 relevant 等待者；二是实现简单、避免漏唤醒。`waiter::broadcast` 在真正 `cond_.broadcast` 之前，会先用 `lock_` 做一个空的作用域——这是一个**内存屏障**（barrier），用于规避「通知先于等待进入 `cond_.wait`」的丢失唤醒问题。

此外，`disconnect` 需要一种「强制叫醒对端并让它不要再等」的机制，这就是 `quit_waiting`：把 `waiter` 内的原子标志 `quit_` 置 `true` 再 `broadcast`，等待方的 `wait_if` 循环条件 `!quit_ && pred()` 会因为 `quit_` 为真而立刻退出。

#### 4.4.2 核心流程

完整的「发送→接收」等待与唤醒往返：

```
send 路径（队列满时）:
  push 失败 → wait_for(wt_waiter_, pred="!push")
     ├─ 自旋重试 push 若干轮（多数情况在此成功）
     └─ 仍失败 → wait_if 阻塞在 wt_waiter_
                 ← 被 recv 的 wt_waiter_.broadcast() 唤醒（对端 pop 腾出空位）
  push 成功后 → rd_waiter_.broadcast()   # 通知等待数据的接收方

recv 路径（队列空时）:
  pop 失败 → wait_for(rd_waiter_, pred="!pop")
     ├─ 自旋重试 pop 若干轮
     └─ 仍失败 → wait_if 阻塞在 rd_waiter_
                 ← 被 send 的 rd_waiter_.broadcast() 唤醒（对端 push 送来数据）
  pop 成功后 → wt_waiter_.broadcast()    # 通知等待空位的发送方

disconnect 路径:
  quit_waiting() → 三类 waiter 都 quit_=true + broadcast
     → 对端任何阻塞中的 wait_if 立刻返回 → recv 收到空 buff_t 优雅退出
```

#### 4.4.3 源码精读

`send` 中的等待与广播成对出现（[src/libipc/ipc.cpp:591-611](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L591-L611)）：

```cpp
static bool send(ipc::handle_t h, void const * data, std::size_t size, std::uint64_t tm) {
    return send([tm, &log](auto *info, auto *que, auto msg_id) {
        return [tm, &log, info, que, msg_id](std::int32_t remain, void const * data, std::size_t size) {
            if (!wait_for(info->wt_waiter_, [&] {            // 等：队列满
                    return !que->push(/*...*/, info->cc_id_, msg_id, remain, data, size);
                }, tm)) {
                // 超时：send 走 force_push 强发；try_send 直接返回 false
                if (!que->force_push(/*...*/)) return false;
            }
            info->rd_waiter_.broadcast();                    // 唤醒：通知接收方有数据
            return true;
        };
    }, h, data, size);
}
```

注意谓词 `return !que->push(...)`：`push` 成功返回 `true`，取反得 `false`（不用等）；`push` 失败返回 `false`，取反得 `true`（需要等队列腾位）。`try_send` 的区别仅在于超时分支直接 `return false` 而非 `force_push`（[src/libipc/ipc.cpp:613-627](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L613-L627)）。

`recv` 是对称的（[src/libipc/ipc.cpp:629-654](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L629-L654)）：

```cpp
for (;;) {
    typename queue_t::value_t msg {};
    if (!wait_for(inf->rd_waiter_, [que, &msg, &h] {        // 等：队列空
            if (!que->connected()) reconnect(&h, true);
            return !que->pop(msg);                          // pop 成功→false→不等
        }, tm)) {
        return {};                                          // 超时返回空 buff_t
    }
    inf->wt_waiter_.broadcast();                            // 唤醒：通知发送方有空位
    ...
}
```

`waiter::broadcast` 的实现（[src/libipc/waiter.h:76-88](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/waiter.h#L76-L88)），注意那个看似多余的空 `lock_guard` 作用域：

```cpp
bool broadcast() noexcept {
    {
        LIBIPC_UNUSED std::lock_guard<ipc::sync::mutex> barrier{lock_}; // barrier
    }
    return cond_.broadcast(lock_);
}
```

它的作用是建立一条 happens-before 关系：确保广播方在此之前对共享内存（队列状态）的写入，对被唤醒后重新加锁的等待方可见，避免「被唤醒却看到旧状态」的险境。

`quit_waiting` 则是优雅退出的钥匙（[src/libipc/waiter.h:90-93](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/waiter.h#L90-L93)）：

```cpp
bool quit_waiting() {
    quit_.store(true, std::memory_order_release);
    return broadcast();
}
```

它被 `conn_info_head::quit_waiting` 一次性作用于三类等待器（[src/libipc/ipc.cpp:161-165](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L161-L165)），而该函数在 `disconnect_receiver` 里被调用。于是发送方 `disconnect()` 会让对端阻塞中的 `recv` 经由 `rd_waiter_` 被唤醒，`wait_if` 因 `quit_` 为真返回，最终 `recv` 收到空 `buff_t`——这正是 [u1-l4](u1-l4-first-ipc-program.md) 所说的「发送方 disconnect 会唤醒对端阻塞的 recv 使其收到空消息」的底层机制。

最后，`waiter` 的三件套（条件变量 + 互斥锁 + `quit_` 原子标志）见 [src/libipc/waiter.h:16-20](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/waiter.h#L16-L20)；而它的静态初始化入口 `waiter::init()`（[src/libipc/sync/waiter.cpp:17-19](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/sync/waiter.cpp#L17-L19)）在 `chan_impl::init_first` 里被调用一次（[src/libipc/ipc.cpp:753-756](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L753-L756)），用于初始化底层 `sync::mutex` 子系统。

#### 4.4.4 代码实践

**实践目标**：把本讲布置的实践任务讲清楚——`send` 时 `wt_waiter_`（队列满）和 `recv` 时 `rd_waiter_`（队列空）的等待与唤醒路径，并说明「前若干次为何忙等而非立即阻塞」。

**操作步骤（源码阅读型）**：

1. 打开 [src/libipc/ipc.cpp:591-611](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L591-L611)（`send`）。指出：谓词是 `!que->push(...)`，等待的是 `wt_waiter_`，成功后 `rd_waiter_.broadcast()`。
2. 打开 [src/libipc/ipc.cpp:629-654](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L629-L654)（`recv`）。指出：谓词是 `!que->pop(msg)`，等待的是 `rd_waiter_`，成功后 `wt_waiter_.broadcast()`。
3. 回到 [src/libipc/ipc.cpp:378-391](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L378-L391)（`wait_for`），结合 [include/libipc/rw_lock.h:76-86](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/include/libipc/rw_lock.h#L76-L86)（`sleep`），回答下面的问题。

**需要回答的问题与预期结论**：

- **唤醒路径**：发送方 push 成功 → `rd_waiter_.broadcast()` → 唤醒阻塞在 `rd_waiter_` 的接收方；接收方 pop 成功 → `wt_waiter_.broadcast()` → 唤醒阻塞在 `wt_waiter_` 的发送方。二者互为因果。
- **前若干次为何忙等**：共享内存 IPC 下，队列满/空的状态通常在纳秒到微秒内被对端解除。`wait_for` 经 `sleep(k, F)` 先 `yield` 自旋 32 轮，期间每轮都重新求值谓词（即重试 `push`/`pop`），一旦对端腾出空间或送来数据就立即成功，完全避开系统调用。只有连续 32 次都没成功（说明是真正的长时间资源紧张），才转入 `wait_if` 的条件变量阻塞，把 CPU 让给操作系统。这是「短时忙等换低延迟、长时阻塞换省 CPU」的折中。

**待本地验证**：构造一个「接收方故意 sleep 100ms 再 pop」的场景，观察发送方在 `wt_waiter_` 上的阻塞行为；再用 `perf` 或 `strace` 观察是否发生了 `futex` 系统调用（阻塞分支应当能看到，而高频正常收发时应当几乎看不到）。

#### 4.4.5 小练习与答案

**练习 1**：`waiter::broadcast` 里那段空的 `lock_guard` 作用域删掉会怎样？

**参考答案**：从「互斥」角度看它是空的（加锁后立刻解锁，临界区里什么都没做），但它建立了一条 acquire/release 内存屏障，保证广播方此前对队列状态的写入对等待方可见。删掉后，在弱内存序架构上可能出现「等待方被唤醒却读到旧状态、误以为条件仍不满足」的罕见竞态。详细原理见 [u8-l1](u8-l1-memory-ordering.md)。

**练习 2**：为什么唤醒一律用 `broadcast`（唤醒全部）而不是 `notify`（唤醒一个）？

**参考答案**：一是广播模式下一条消息要被多个接收者各读一次，只唤醒一个会导致其他接收者漏读；二是 `notify` 需要「知道有谁在等」，而 libipc 的等待者是跨进程的，精确计数成本高。`broadcast` 的代价（多唤醒几次）相对可接受，且 `wait_if` 的谓词循环会让「其实不该醒」的等待者重新睡回去，正确性有保障。

**练习 3**：发送方调用 `disconnect()` 后，接收方正阻塞在 `recv` 里。它靠什么、经过哪几步才退出？

**参考答案**：`disconnect` → `disconnect_receiver` → `quit_waiting()` 把 `rd_waiter_`（及其它两类）的 `quit_` 置 `true` 并 `broadcast`。接收方的 `wait_if` 循环条件是 `!quit_ && pred()`，`quit_` 为真使其立刻返回 `true`；`wait_for` 随之 `break` 返回 `true`，但此时 `pop` 并未成功，`recv` 在后续处理中返回空 `buff_t`，上层据此判断「对端已断开」。

## 5. 综合实践

把本讲四个模块串起来，完成一次「端到端等待链路」的源码追踪与推理。

**任务**：假设有一条名为 `"ipc"` 的 `ipc::channel`，接收方先启动并阻塞在 `recv`（默认无限等待），发送方 1 秒后才启动并 `send` 一条 10 字节消息。请按时间线回答下列问题，**每一条都要引用具体源码行**：

1. 接收方启动后，`recv` 走到 [src/libipc/ipc.cpp:645](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L645) 的 `wait_for(rd_waiter_, ...)`。队列此时为空，请描述它如何「自旋 32 轮 → 阻塞在 `rd_waiter_`」，并指出阻塞发生在 [src/libipc/waiter.h:71](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/waiter.h#L71) 的 `cond_.wait`。
2. 发送方启动、`connect` 成功后，是否会广播 `cc_waiter_`？为什么？（提示：见 [src/libipc/ipc.cpp:491-494](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L491-L494)，注意 `connect` 的角色与「wouldn't connect twice」。）
3. 发送方 `send` 时 `push` 成功（队列不满），随后在 [src/libipc/ipc.cpp:607](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L607) 调用 `rd_waiter_.broadcast()`。请说明这次广播如何唤醒第 1 步里阻塞的接收方。
4. 接收方被唤醒、`pop` 成功后，在 [src/libipc/ipc.cpp:654](https://github.com/mutouyun/cpp-ipc/blob/2e28547cd32b22c2e1f2c85d22d0882810838503/src/libipc/ipc.cpp#L654) 调用 `wt_waiter_.broadcast()`。此时并没有发送方在等 `wt_waiter_`，这次广播是否多余？为什么库仍然这么做？

**参考要点**：

- 第 1 步：`wait_for` 先 `sleep(k,F)` 自旋（`k<32` 仅 `yield`，每轮重试 `pop`），32 轮后执行 `wait_if`，在 `cond_.wait(lock_, invalid_value)` 处无限阻塞。
- 第 2 步：发送方若以 receiver 身份首次 `connect`，会广播 `cc_waiter_`；若以 sender 身份连接则不会触发 `que->connect()`。需结合 `mode & receiver`（见 [u2-l3](u2-l3-channel-handle-lifecycle.md)）判断。
- 第 3 步：广播使接收方的 `cond_.wait` 返回，`wait_if` 重新求值谓词 `pop(msg)` 成功 → 返回 `true` → `wait_for` 因 `k==0` 而 `break` → `recv` 拿到消息。
- 第 4 步：本次确实没有发送方在等，广播是「无害冗余」。库这样写是为了对称与简单：pop 成功一律广播 `wt_waiter_`，不额外维护「当前是否有发送方在阻塞」的状态——多一次空广播的代价远低于维护该状态的复杂度。

## 6. 本讲小结

- `conn_info_head` 持有三类 `waiter`：`cc_waiter_`（等接收者上线）、`wt_waiter_`（发送方等队列腾位）、`rd_waiter_`（接收方等数据到来），分别用 `CC_CONN__` / `WT_CONN__` / `RD_CONN__` 前缀命名以隔离。
- `wait_for` 是连接「自旋」与「阻塞」的桥梁：`tm==0` 时非阻塞求值一次；否则先经 `sleep(k,F)` 自旋 32 轮（每轮重试谓词），仍不满足才执行 `wait_if` 做条件变量阻塞，阻塞后清零退出。
- 退避分两套：`yield(k)`（4 级：空转/PAUSE/yield/sleep_for）服务纯自旋锁，`sleep(k,F)`（2 级：yield/执行 F）服务 `wait_for`，二者共享阈值 32，体现「先轻后重」哲学。
- 唤醒一律用 `broadcast`，且在 `cond_.broadcast` 前用一段空 `lock_guard` 建立内存屏障，防止丢失唤醒与读到旧状态。
- 生产者与消费者互为唤醒者：`send` 成功后 `rd_waiter_.broadcast()`，`recv` 成功后 `wt_waiter_.broadcast()`。
- `disconnect` 经 `quit_waiting` 把 `quit_` 置真再广播，让对端阻塞的 `recv` 优雅退出并收到空 `buff_t`。

## 7. 下一步学习建议

- 本讲把 `waiter` 内部的 `condition` / `mutex` 当作黑盒。它们的跨平台实现（Windows 用 `SignalObjectAndWait`+计数器、POSIX 用命名信号量与 robust mutex）在 [u6-l2](u6-l2-robust-mutex.md)、[u6-l3](u6-l3-condition-semaphore.md)、[u6-l4](u6-l4-waiter.md) 详讲，建议接着读。
- 本讲频繁提到「谓词就是 `push`/`pop` 的成败」。这两个操作背后是无锁循环队列，建议进入 [u4-l1](u4-l1-queue-abstraction.md) 与 [u4-l3](u4-l3-prod-cons-unicast.md) 理解 `push`/`pop` 为何能无锁地成败。
- 关于内存序（`broadcast` 里的屏障、`quit_` 的 release、CAS 的 acquire/release）的深入分析，见 [u8-l1](u8-l1-memory-ordering.md)。
