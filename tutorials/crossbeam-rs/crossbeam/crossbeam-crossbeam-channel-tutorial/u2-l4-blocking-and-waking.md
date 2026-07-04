# 阻塞与唤醒机制 context.rs + waker.rs

## 1. 本讲目标

本讲深入到 crossbeam-channel 的「睡眠—唤醒」底层。学完后你应当能够：

- 说清楚一个线程在 `send`/`recv` 阻塞时，到底把什么状态存到了哪里、又被谁打醒。
- 解释 `Selected` 状态机 `Waiting / Aborted / Disconnected / Operation` 的四种取值与转换时机。
- 读懂 `Context`（线程本地、可被其他线程原子地「选中」）与 `Waker`/`SyncWaker`（阻塞者队列）的协作。
- 说清 `Waker::try_select` 为什么只挑「别的线程」的 entry，以及 `disconnect` 如何唤醒所有阻塞者。

本讲只讲「阻塞与唤醒」这一套机制本身，**不**展开 array/list/zero 各 flavor 的队列实现（那是 u2-l5、u2-l6、u2-l7 的内容），也**不**展开 `select!` 宏的调度算法（那是 u3-l1）。

## 2. 前置知识

在进入源码前，先用通俗语言建立三个直觉。

**(1) 阻塞不是「忙等」，而是「登记 + 睡觉 + 被叫醒」。**
当一个有界通道满了，`send` 不能空转消耗 CPU，也不能直接返回错误。它需要：

1. 把「我正在等这个操作完成」这件事**登记**到某个队列里；
2. 调用操作系统的 `park` 让自己真正睡眠；
3. 等别的线程让出空间后，把它 **unpark（唤醒）**。

`Context` 承担第 1、2 步里的「线程侧状态」，`Waker`/`SyncWaker` 承担「队列侧」。

**(2) 唤醒的本质是「抢占一个原子状态」。**
crossbeam-channel 用一个 `AtomicUsize` 表示「这个线程的这次操作现在处于什么阶段」。它有四个可能的值（见 [select.rs 的 Selected 枚举](#)）。多个线程可能同时想「替某个睡眠线程做决定」（比如同时有两个 recv 让出了空间，都想唤醒同一个 sender），但只有一个能成功——靠的就是一次 `compare_exchange`。这正是「至多一个操作胜出」的并发正确性根基。

**(3) `park`/`unpark` 有「许可（permit）」语义。**
Rust 标准库的 `Thread::park` / `unpark` 不是严格的「先 park 后 unpark」：`unpark` 会先存一个许可，如果线程还没 park，下次 `park` 会立刻返回（消费掉许可）。这能缓解「检查状态」与「真正睡眠」之间的竞态。但许可只有一个且不计数，所以 crossbeam-channel 仍然在 `wait_until` 里用循环反复检查状态，以容忍**虚假唤醒（spurious wakeup）**。

> 关键术语：`park`/`unpark`（线程睡眠/唤醒）、`AtomicUsize`（无锁状态机）、`compare_exchange`（CAS，乐观并发）、线程本地存储（thread-local）、`Arc`（共享所有权）、`Mutex`（互斥锁，这里用的是非毒版本）。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [src/context.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs) | 线程本地的 `Context`：持有一个 `Selected` 原子状态 + 一个 packet 指针槽 + 线程句柄，提供 `try_select`/`wait_until`/`unpark` 等。 |
| [src/waker.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs) | 阻塞者队列：`Entry`（一个阻塞中的操作）、`Waker`（裸队列）、`SyncWaker`（加锁 + `is_empty` 快速路径的可共享封装）。 |
| [src/select.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs) | 定义 `Selected` 枚举、`Operation` id、`SelectHandle` trait。本讲只用到前两个。 |
| [src/flavors/array.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs) | 作为「真实调用方」示例：它的 `send`/`recv`/`disconnect_*` 把 `Context` 与 `SyncWaker` 串成一条完整流程。 |

> `Context` 和 `Waker` 是**互为表里**的一对：`Context` 是「被阻塞者」的视角（我睡了，谁来选我），`Waker` 是「阻塞者队列」的视角（我手里有一堆睡着的人，该叫醒谁）。

## 4. 核心概念与源码讲解

### 4.1 线程本地上下文 Context

#### 4.1.1 概念说明

`Context` 是一个线程在执行「可能阻塞的操作」时的随身挂件。它解决两个问题：

1. **别的线程怎么「替我做决定」？** —— 我把「当前状态」存成一个原子变量 `select`，别人通过 CAS 就能帮我从「等待中」翻到「已选中/已断开」。
2. **消息怎么跨线程递到我手里？** —— 在零容量会合（zero flavor）里没有缓冲区，消息本身要塞进一个 `packet` 指针槽里递过来。

`Context` 本身只是一个 `Arc<Inner>` 的薄壳：

[src/context.rs:L21-L23](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs#L21-L23) —— `Context` 持有一个 `Arc<Inner>`，因此可以被克隆后放进各个通道的阻塞者队列里，多个 `Entry` 共享同一个 `Inner`。

[src/context.rs:L27-L39](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs#L27-L39) —— `Inner` 的四个字段：`select`（状态机）、`packet`（跨线程递包槽）、`thread`（用于 unpark）、`thread_id`（用于识别「自己」）。

> 为什么用 `Arc` 而不是裸指针？因为同一个线程可能同时阻塞在多个操作上（`select!` 同时等待多个通道），它的 `Context` 会被**克隆**进多个通道的 `Waker` 队列。这些 `Entry` 都要能安全地访问同一个 `Inner`，引用计数是最自然的方式。

#### 4.1.2 核心流程

`Context` 的状态机就是 `Selected` 的四个取值：

[src/select.rs:L56-L68](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L56-L68) —— `Selected::{Waiting, Aborted, Disconnected, Operation(Operation)}`，并约定了到 `usize` 的数值编码：`Waiting=0`、`Aborted=1`、`Disconnected=2`、`Operation(n)=n`（`n>2`）。

状态转换关系如下：

```
                       ┌─────────────────────────────────────────────┐
                       │                                             │
                       ▼                                             │ 由别的线程经
                ┌───────────┐   自己 try_select(Aborted)   ┌─────────┴──┐
   new()/reset()│  Waiting  │ ──────────────────────────▶ │  Aborted   │
                └─────┬─────┘                              └────────────┘
                      │ 由别的线程经
                      │  Waker::try_select
                      │  try_select(Operation)
                      │
       ┌──────────────┴────────────────┐
       ▼                               ▼
┌────────────────┐               ┌──────────────────┐
│ Operation(op)  │               │   Disconnected   │ ← 由 Waker::disconnect
└────────────────┘               └──────────────────┘   try_select(Disconnected)
```

转换的**唯一手段**是 `Context::try_select`，它内部是一次 CAS，从 `Waiting` 翻到目标值：

- 成功：本线程（或替本线程做决定的别的线程）「赢得了」这次选择；
- 失败：说明已经有别人先一步选了，CAS 的失败值会把「别人选的那个状态」原样返回。

为保证 `Operation(op)` 的数值 `n` 永远不会和 `0/1/2` 撞车，`Operation::hook` 显式断言地址大于 2：

[src/select.rs:L44-L51](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L44-L51) —— 把一个栈上变量的地址转成 `Operation` id，并 `assert!(val > 2)` 防止与 `Waiting/Aborted/Disconnected` 的数值表示冲突。

> 这里有一个小而关键的工程取舍：用「栈变量地址」当操作 id，意味着每次阻塞操作都用一个**线程私有、且在操作期间存活**的栈变量作为唯一标识。这样不同线程、不同操作天然不会撞号，且不需要全局分配器。

「阻塞一个操作」的完整步骤（以 array flavor 的 `send` 为例）可以归纳为六步：

```
1. Context::with(|cx| { ... })        // 取出（或新建）线程本地的 Context，并 reset
2. oper = Operation::hook(token)      // 用栈变量地址生成操作 id
3. self.senders.register(oper, cx)    // 把 (oper, cx 克隆) 放进阻塞者队列
4. 若此刻通道已 ready：cx.try_select(Aborted)  // 抢占失败也没关系，让别人去选
5. sel = cx.wait_until(deadline)      // park 自己，醒来后读状态
6. 根据 sel 决定：Aborted/Disconnected → unregister；Operation → 收工
```

第 4 步是经典的「注册后再复查」：在 `register` 之前通道还满着，但 `register` 之后、`park` 之前可能正好有人 `recv` 让出了空间。如果不复查，就会漏掉这次唤醒。复查发现已 ready 就立刻 `try_select(Aborted)`，让自己别真睡、回到外层循环重试。

#### 4.1.3 源码精读

**(a) 线程本地缓存 + 复用。** `Context` 通过 `Context::with` 提供，它使用一个线程本地的 `Cell<Option<Context>>` 缓存，避免每次阻塞操作都重新分配 `Arc<Inner>`：

[src/context.rs:L44-L70](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs#L44-L70) —— `with` 先尝试从线程本地取缓存的 `Context`，取到就 `reset()` 后复用、用完放回；取不到（例如线程本地已被销毁）就临时 `new()` 一个。注意 `thread: thread::current()` 在 `new()` 里只取一次，之后这个 `Context` 就**绑定到当前线程**了——这也是它能被 `unpark` 的前提。

[src/context.rs:L74-L83](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs#L74-L83) —— `new()` 创建 `Inner`，初始状态为 `Waiting`，`packet` 为空指针，并记录当前线程句柄与 id。

[src/context.rs:L87-L92](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs#L87-L92) —— `reset()` 把 `select` 和 `packet` 都清回初始值（用 `Release` 写，保证后续在别的线程读到的也是干净状态）。

**(b) 抢占状态的 CAS。**

[src/context.rs:L98-L109](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs#L98-L109) —— `try_select`：`compare_exchange(Waiting → target, AcqRel/Acquire)`。成功返回 `Ok(())`；失败把 CAS 拿到的旧值作为 `Err(Selected)` 返回——调用方据此知道「别人已经替我选了什么」。

> 内存序选择：成功用 `AcqRel`（要看到别人之前对通道状态的写入，也要让自己对 packet 的写入对别人可见），失败用 `Acquire`（要读到别人写入的最终状态）。这是无锁代码里非常典型的「成对」序。

**(c) 睡眠与醒来。**

[src/context.rs:L144-L169](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs#L144-L169) —— `wait_until` 是阻塞的核心。循环：先读 `select`，不是 `Waiting` 就立刻返回（可能是被别人选了，也可能是「注册后复查」没睡就返回）；有 deadline 就 `park_timeout(end-now)`，且一旦超时就自己 `try_select(Aborted)`；没有 deadline 就 `park()`。注意整个函数是**循环**，天然容忍虚假唤醒。

[src/context.rs:L173-L175](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs#L173-L175) —— `unpark` 直接转发给内部线程句柄。别的线程就是靠这个把睡眠线程叫醒的。

**(d) 跨线程递包（packet）。** 这一路主要服务零容量会合通道（u2-l7 详讲），这里只看接口语义：

[src/context.rs:L121-L125](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs#L121-L125) —— `store_packet`：在 `try_select` 成功后，由「选中别人」的一方把消息包指针塞进被选线程的 packet 槽（`Release` 写）。

[src/context.rs:L129-L138](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs#L129-L138) —— `wait_packet`：被选线程醒来后，用 `Backoff::snooze()` 自旋等 packet 指针非空（`Acquire` 读）。这里用自旋而不是再 park，是因为 packet 写入和 unpark 几乎同时发生，自旋一两次就能拿到，比再次陷入内核更快。

#### 4.1.4 代码实践

**实践目标：** 把 array flavor 里一次「满队列阻塞 send」的完整六步走一遍，在源码上标出 `Context` 的每一步调用。

**操作步骤：**

1. 打开 [src/flavors/array.rs 的 `send` 方法](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L334-L384)。注意它外层是一个 `loop`：先反复尝试 `start_send`（自旋重试），失败后才进入 `Context::with` 的阻塞分支。
2. 在 [L362-L382](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L362-L382) 上做标记，把每一行对应到 4.1.2 的六步：
   - `Context::with(|cx| { ... })` → 步骤 1（取/复用线程本地 Context）；
   - `let oper = Operation::hook(token);` → 步骤 2（生成操作 id）；
   - `self.senders.register(oper, cx);` → 步骤 3（登记到 `senders` 阻塞者队列）；
   - `if !self.is_full() || self.is_disconnected() { let _ = cx.try_select(Selected::Aborted); }` → 步骤 4（注册后复查，避免漏唤醒）；
   - `let sel = cx.wait_until(deadline);` → 步骤 5（park 自己）；
   - `match sel { ... }` → 步骤 6（`Aborted`/`Disconnected` 要 `unregister`，`Operation` 直接收工）。
3. 思考：为什么步骤 4 用 `let _ = ...`（忽略 `try_select` 的返回值）？

**需要观察的现象 / 预期结果（源码阅读型，待本地验证）：**

- 你应当能解释：若一个 sender 在步骤 3 之后、步骤 5 之前被一个 `recv` 唤醒，那么步骤 4 的复查会让 `try_select(Aborted)` 成功，`wait_until` 里的第一次读 `select` 就不是 `Waiting` 而直接返回 `Aborted`，于是不会真正 `park`，转而回到外层 `loop` 重新 `start_send`。
- 你应当能说清：步骤 6 里 `Aborted` 和 `Disconnected` 都要 `unregister`（把自己的 entry 从队列里摘掉，保持队列干净），而 `Operation` 分支为空——因为此刻已经被对端「选中」，entry 由对端的 `try_select` 负责摘除（见 4.2.3）。

> 第三问的参考答案：`try_select(Aborted)` 失败只意味着「别人已经替我选了某个操作」，那正是我们想要的结果（操作已经就绪），不需要任何额外处理，所以用 `let _ =` 丢弃。

#### 4.1.5 小练习与答案

**练习 1.** `Context::with` 为什么要用线程本地缓存 + `reset()`，而不是每次都 `Context::new()`？

**参考答案：** `new()` 要分配一个 `Arc<Inner>` 并调用 `thread::current()`，开销不小；而阻塞操作在繁忙通道上会频繁发生。线程本地缓存让同一个线程反复复用同一个 `Inner`，`reset()` 只需两次原子写就能「清零」状态，避免了反复堆分配。

**练习 2.** 假如把 `wait_until` 里的 `loop` 改成单次 `park()` 后直接返回 `selected()`，会出什么问题？

**参考答案：** `park` 允许**虚假唤醒**（被唤醒但状态没变），也可能因为「许可被更早的某次操作消费」语义产生时序交错。单次返回会把一个仍处于 `Waiting` 的状态误报给上层，导致上层以为操作完成却拿不到消息。循环反复检查 `select` 才能保证「返回时状态一定不是 `Waiting`」。

---

### 4.2 阻塞者队列 Waker / SyncWaker

#### 4.2.1 概念说明

`Context` 是「我（被阻塞者）」的视角；`Waker` 是「通道（阻塞者队列）」的视角。每个真实 flavor 的 `Channel` 都持有**两个** `SyncWaker`：一个装「因通道满而阻塞的 sender」，一个装「因通道空而阻塞的 receiver」。例如 array flavor：

[src/flavors/array.rs:L88-L92](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L88-L92) —— `senders: SyncWaker` 与 `receivers: SyncWaker`，分别登记两侧的阻塞者。

队列里的一项是 `Entry`：

[src/waker.rs:L17-L26](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L17-L26) —— `Entry` 三个字段：`oper`（操作 id）、`packet`（可选的跨线程递包指针，零容量用）、`cx`（所属线程的 `Context` 克隆）。

`Waker` 把这些 entry 分成两类：

[src/waker.rs:L32-L38](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L32-L38) —— `selectors`（真正阻塞、等待被选中执行的操作）与 `observers`（只等「就绪通知」、由自己另行处理的操作，服务于 `select!` 的 `watch` 路径）。

> 为什么要有 `observers` 这一类？这是 `select!` 优化的一部分：当一个线程在多个操作间选择时，它不一定要被「选中执行」，有时只需要知道「某个操作就绪了」然后自己回头处理。`watch`/`notify` 就是这条轻量通知通路。本讲重点在 `selectors`，`observers` 在 u3-l1 的 `run_ready` 里会用到。

`SyncWaker` 则是 `Waker` 的「加锁可共享」封装：

[src/waker.rs:L182-L188](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L182-L188) —— `SyncWaker = Mutex<Waker> + AtomicBool(is_empty)`。`Mutex` 来自 `src/utils.rs`，是一个**非毒**（non-poisoning）的 `std::sync::Mutex` 包装（u3-l5 详讲），即使持锁线程 panic 也不会污染锁。

#### 4.2.2 核心流程

`Waker` 的核心方法可以分成三组：

| 方法 | 作用 | 调用方 |
| --- | --- | --- |
| `register` / `register_with_packet` | 把一个 entry 入队（`selectors`） | 阻塞中的线程自己 |
| `unregister` | 按 `oper` 把 entry 出队 | 阻塞者醒来后发现没被选中，自己摘除 |
| `try_select` | **挑一个「别的线程」的 entry，选中它并唤醒** | 让出空间的一方（如 `read` 后调 `senders.notify()`） |
| `disconnect` | 把**所有** entry 标记为 `Disconnected` 并唤醒 | 最后一个对端句柄 drop 时 |
| `watch` / `unwatch` / `notify` | observers 的登记/摘除/批量通知 | `select!` 的就绪通知路径 |

「让出空间 → 唤醒一个阻塞者」这条最常见路径的伪代码：

```
// receiver 成功 read 了一条消息，腾出了空位
self.senders.notify();
   └─ SyncWaker::notify()
        ├─ 若 is_empty == true：直接返回（快速路径，连锁都不加）
        └─ 否则加锁 → Waker::try_select()
             ├─ 遍历 selectors，找第一个 cx.thread_id() != 当前线程 的 entry
             ├─ 对它的 cx.try_select(Operation(oper))   // CAS 抢占
             ├─ 成功：cx.store_packet(packet) + cx.unpark()，并把 entry 摘除
             └─ 然后 Waker::notify() 顺便通知 observers
```

「断开 → 唤醒所有阻塞者」的伪代码：

```
// 最后一个 sender drop，触发 disconnect_senders
self.receivers.disconnect();
   └─ SyncWaker::disconnect() → 加锁 → Waker::disconnect()
        ├─ 遍历 selectors：cx.try_select(Disconnected)，成功就 unpark
        │   （注意：这里【不】摘除 entry，留给被唤醒线程自己 unregister）
        └─ 再调 notify() 通知 observers
```

#### 4.2.3 源码精读

**(a) 登记与摘除。**

[src/waker.rs:L52-L64](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L52-L64) —— `register` / `register_with_packet`：把 `cx.clone()`（bump `Arc` 引用计数）连同 `oper`、`packet` 一起 `push` 进 `selectors`。

[src/waker.rs:L68-L80](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L68-L80) —— `unregister`：线性查找 `oper` 相等的 entry 并 `remove`。返回 `Option<Entry>`，调用方据此决定是否还要处理 packet。

**(b) `try_select`——本讲最关键的方法。**

[src/waker.rs:L84-L111](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L84-L111) —— 队列空直接返回 `None`；否则取 `current_thread_id()`，在 `selectors` 里找**第一个**满足下列条件的 entry：

1. `selector.cx.thread_id() != thread_id` ——「不是我自己」；
2. `selector.cx.try_select(Selected::Operation(selector.oper)).is_ok()` —— CAS 成功地把对方从 `Waiting` 翻到 `Operation`；
3. 紧接着 `store_packet` + `unpark` —— 把消息包（若有）递过去并唤醒对方。

找到后把该 entry 从队列里 `remove`，保持队列干净、加速后续查找。

**为什么只挑「别的线程」的 entry？** 这里有三重理由：

- **逻辑前提**：执行 `notify` 的线程此刻是**醒着且正在运行**的（它刚完成一次 `write`/`read`）。它自己即使登记过 entry，也并不真的「阻塞」，自己的 `select` 循环马上会重新检查 `is_ready` 并自行决定下一步。
- **避免自我冲突**：如果允许选中自己的 entry，就会把自己的某个操作强行标记为 `Operation`，而与此同时自己的 `run_select`/阻塞循环可能正打算把这个操作 `abort` 或判定为超时——两个决策路径会打架，可能产生「同一个操作被处理两次」或「选错操作」。
- **会合正确性**：在零容量会合（zero flavor）里，`try_select` 实际上是在做 send 与 recv 的**配对**。自己跟自己配对没有意义（单线程会合就是死锁），必须配对到另一个线程。

附带一个对称的只读检查 `can_select`：

[src/waker.rs:L115-L125](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L115-L125) —— 只判断「是否存在可被当前线程选中的 entry」，用于 `select!` 的快速路径探测，同样要求 `thread_id() != 当前线程` 且状态仍为 `Waiting`。

**(c) `disconnect`——唤醒所有阻塞者。**

[src/waker.rs:L155-L168](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L155-L168) —— 遍历**所有** `selectors`，对每个 `cx.try_select(Selected::Disconnected)`，成功就 `unpark`。关键细节在注释里：**这里故意不 `remove` entry**。

为什么不摘除？因为被唤醒的线程需要**自己**回到阻塞点，做后续清理：

- 它可能还要从 packet 里回收/销毁一条消息（zero flavor）；
- 它的阻塞循环里有 `unregister(oper).unwrap()`，由它自己把 entry 摘掉，责任清晰。

最后调 `self.notify()`，让 observers 也收到断开通知。

对应的真实调用方在 array flavor：

[src/flavors/array.rs:L487-L496](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L487-L496) —— `disconnect_senders`：用 `tail.fetch_or(mark_bit)` 原子地置断开位，只有「第一个」看到该位原本为 0 的调用方才真正执行 `self.receivers.disconnect()`——这保证了断开只触发一次（与 u2-l2 讲的 `destroy` 标志同理）。

[src/flavors/array.rs:L506-L517](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L506-L517) —— `disconnect_receivers`：对称地 `self.senders.disconnect()` 唤醒所有阻塞的 sender，并 `discard_all_messages` 丢弃剩余消息（这正是 u1-l4 讲过的「接收侧断开时丢弃剩余消息」的落点）。

**(d) `SyncWaker` 的 `is_empty` 快速路径。**

[src/waker.rs:L225-L237](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L225-L237) —— `SyncWaker::notify` 是性能关键点（每次成功 `write`/`read` 都会调）。它先 `is_empty.load(SeqCst)`：若为 `true`，说明队列里根本没人等，**直接返回，连锁都不加**。这是绝大多数非竞争场景的常态。只有在 `is_empty == false` 时才加锁，并且加锁后**再读一次** `is_empty` 做二次确认（double-checked locking），防止「读 is_empty 与加锁之间」有人把队列清空。

[src/waker.rs:L202-L209](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L202-L209) —— 每次 `register` 后都重算 `is_empty = selectors.is_empty() && observers.is_empty()` 并以 `SeqCst` 写回，保证标志与受锁保护的真实状态一致。

[src/waker.rs:L263-L270](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L263-L270) —— `SyncWaker::disconnect` 不需要快速路径（断开是罕见事件），直接加锁委托给 `Waker::disconnect`，再更新 `is_empty`。

> 为什么 `is_empty` 全用 `SeqCst`？因为它是「锁外读取、锁内写入」的协调标志，需要一个全局一致的总序来避免「读到旧 is_empty=true 的同时、对方正在锁内加入 entry」的漏唤醒。`SeqCst` 虽贵，但 `load` 的成本远低于一次无谓的 `lock()`，整体仍是净赚。

**(e) 谁来叫醒阻塞者？** 把链路补全：array 的 `read` 成功后：

[src/flavors/array.rs:L306-L321](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L306-L321) —— `read` 末尾 `self.senders.notify();`：消费一条消息腾出空位 → 唤醒一个阻塞的 sender。对称地，`write` 末尾 `self.receivers.notify();`（[L228](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L228)）。这就是「生产唤醒消费、消费唤醒生产」的闭环。

[src/waker.rs:L282-L291](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L282-L291) —— `current_thread_id()`：同样用线程本地缓存，避免 `notify` 热路径上反复调 `thread::current().id()`。

#### 4.2.4 代码实践

**实践目标：** 用一段可运行的小程序，亲眼看到「满队列阻塞 sender → recv 唤醒 → drop receiver 触发 disconnect 唤醒」三种现象，并把它们对应到 `Waker` 的方法。

**操作步骤：**

1. 在仓库根目录建一个临时二进制（**示例代码，非项目原有文件**），例如 `examples/tmp_waker_demo.rs`：

   ```rust
   // 示例代码：仅用于本讲实践，不属于 crossbeam-channel 原有源码
   use std::thread;
   use std::time::Duration;
   use crossbeam_channel::bounded;

   fn main() {
       let (s, r) = bounded::<i32>(1); // 容量 1：放第二条就会满
       s.send(1).unwrap();             // 占满唯一槽位

       let h = thread::spawn(move || {
           // 这一条 send 会阻塞，进入 senders 队列等待被唤醒
           s.send(2).unwrap();
           println!("send(2) 完成");
       });

       thread::sleep(Duration::from_millis(200));
       println!("recv 到 {:?}", r.recv()); // read -> senders.notify() 唤醒 sender
       h.join().unwrap();

       // 现在 drop(r)：触发 disconnect_receivers -> senders.disconnect()
       drop(r);
       println!("已 drop(receiver)，阻塞者会被以 Disconnected 唤醒");
   }
   ```

2. 运行：`cargo run --example tmp_waker_demo`（运行前需确认 examples 目录的编译方式；若不便运行，转为下面的源码阅读型实践）。
3. 在 [src/flavors/array.rs 的 `write`/`read`/`disconnect_receivers`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L215-L321) 上标出三处 `notify()` / `disconnect()` 调用点。

**需要观察的现象 / 预期结果：**

- `s.send(2)` 在子线程里阻塞，主线程 `r.recv()` 拿到 `Some(1)` 后，子线程几乎立刻打印 `send(2) 完成`——对应 `read` 里的 `self.senders.notify()` → `SyncWaker::notify` → `Waker::try_select` 选中并 unpark。
- `drop(r)` 后程序正常结束，没有死锁——对应 `disconnect_receivers` 里的 `self.senders.disconnect()` 把（此刻已为空的）阻塞者队列整体唤醒。本例此刻已无阻塞者，所以重点是「不会卡住」。
- 若你想更直观地观察 disconnect 唤醒，可在 drop 前**再**让一个线程阻塞在 `s.send(3)` 上，drop(r) 后该线程会收到 `Err(SendTimeoutError::Disconnected(3))`（与 [channel.rs `send` 的错误归一化](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L446-L456) 一致）。

> 提示：若运行环境无法编译 examples，请改为纯阅读实践——在源码上画出「send 阻塞 → register → 被 read 的 notify/try_select 选中 → unpark → 醒来收工」与「drop(receiver) → disconnect_receivers → disconnect → 各阻塞者 try_select(Disconnected) → unpark」两条链路即可。

#### 4.2.5 小练习与答案

**练习 1.** `Waker::try_select` 找到目标 entry 后会 `remove(pos)`，而 `Waker::disconnect` 遍历时却**不** remove。为什么两者策略不同？

**参考答案：** `try_select` 是「配对成功」——对方已被选中执行，entry 的使命完成，由选中它的这一方顺手摘除最干净。`disconnect` 是「广播断开」——被唤醒的线程醒来后还要做自己的清理（回收 packet、决定返回什么错误、可能还要从队列里读取剩余消息），所以把 `unregister` 的责任交给被唤醒者自己，避免在断开路径上重复处理 packet。

**练习 2.** 如果把 `SyncWaker` 的 `is_empty` 字段删掉、`notify` 改成「每次都加锁再判空」，功能上仍正确。那保留 `is_empty` 的意义是什么？会不会带来漏唤醒？

**参考答案：** 意义是**性能**：大多数 `notify` 发生在没有阻塞者的时候（通道刚空/刚满但没人等），`is_empty` 让这些调用一次原子 `load` 就返回，免去 `lock()` 的系统调用代价。不会漏唤醒，因为 `register` 在锁内加入 entry 后会立刻以 `SeqCst` 把 `is_empty` 置 `false`，而 `notify` 采用「先读 is_empty、加锁后再读一次」的 double-checked 模式，保证了「加入 entry」与「置 false」之间的 happens-before 关系。

**练习 3.** `Selected` 用 `0/1/2` 表示 `Waiting/Aborted/Disconnected`，而 `Operation(n)` 用 `n>2`。说出一个 `Operation::hook` 里 `assert!(val > 2)` 想防止的具体 bug。

**参考答案：** 防止某个栈变量地址恰好等于 `0/1/2` 时，`Operation(id)` 与 `Waiting/Aborted/Disconnected` 数值撞车——那会让 `try_select` 的 CAS 把「选中某个操作」误判成「选中了 Aborted/Disconnected」，破坏状态机。断言地址 `>2`（实际上栈地址远大于 2）就杜绝了这种歧义。

## 5. 综合实践

**任务：** 把本讲两个最小模块串成一张「阻塞与唤醒时序图」，并对照真实源码自检。

请按下列顺序完成：

1. **画时序图**（文字版即可）。设有两个线程 T1（sender）、T2（receiver），有界通道容量 1。按时间轴标出：
   - T2 先 `recv`，通道空 → 调用 `Context::with` → `receivers.register` → `wait_until` 内 `park`；
   - T1 后 `send`，`start_send` 成功 → `write` 写入消息 → 末尾 `self.receivers.notify()` → `SyncWaker::notify`（`is_empty==false`）→ 加锁 → `Waker::try_select` 选中 T2 的 entry → `cx.store_packet`（array 路径 packet 为 null）→ `cx.try_select(Operation)` → `cx.unpark(T2)` → `remove(entry)`；
   - T2 醒来，`wait_until` 循环读到 `Operation`，返回，`send`/`recv` 收工。
2. **对照源码核色**：把上图的每个箭头标注成 [context.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs) 或 [waker.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs) 里的具体方法名与行号。
3. **断开分支**：再画一张「T1 drop(sender) 之外的另一个 sender 也 drop，最后一个 sender drop → `disconnect_senders` → `receivers.disconnect()` → T2 被 `try_select(Disconnected)` 唤醒 → T2 自己 `unregister`」的时序图，并解释为什么此时 `Waker::disconnect` 不摘除 entry。
4. **自检问题**：若 T1 的 `notify` 与 T2 的 `park` 存在竞态（T1 先 unpark、T2 后 park），为什么不会真的卡死 T2？提示：结合「park/unpark 的 permit 语义」与「array `send` 里 `register` 后的复查 `try_select(Aborted)`」两点回答。

**预期结果：** 你应当得到两张时序图，能毫无卡顿地说出每一步对应的方法名与行号，并能解释「为什么不死锁」「为什么至多一个操作胜出」「为什么断开能唤醒所有人」。这是后续阅读 u3-l1（`run_select` 核心算法）与 u2-l5/u2-l6/u2-l7（各 flavor 内部）的直接基础。

## 6. 本讲小结

- `Context` 是线程本地的「被阻塞者」状态：一个 `Selected` 原子状态机（`Waiting/Aborted/Disconnected/Operation`）+ 一个 packet 指针槽 + 线程句柄，通过 `Arc<Inner>` 被克隆进各通道的阻塞者队列。
- `Context::try_select` 用一次 CAS（`Waiting → 目标`）保证「至多一个线程/操作赢得选择」，失败时返回别人已选的值。
- `Context::wait_until` 用**循环 + park/park_timeout** 容忍虚假唤醒，超时则自己 `try_select(Aborted)`。
- `Waker`/`SyncWaker` 是「阻塞者队列」：`register`/`unregister` 维护 `selectors`，`try_select` 负责选中并唤醒一个**别的线程**的 entry，`disconnect` 负责把所有阻塞者以 `Disconnected` 唤醒（且不摘除 entry，留给被唤醒者自己清理）。
- `SyncWaker` 用 `Mutex<Waker> + AtomicBool(is_empty)` 提供「无阻塞者时一次原子 load 即返回」的快速路径，全 `SeqCst` 保证锁内外协调一致。
- 真实 flavor（以 array 为例）在 `write`/`read` 末尾调 `receivers/senders.notify()` 形成「生产唤醒消费、消费唤醒生产」的闭环，在 `disconnect_senders/receivers` 里触发广播唤醒。

## 7. 下一步学习建议

- **u2-l5（array flavor）**：本讲的 `start_send`/`start_recv`/`write`/`read` 都来自 array，下一讲会把它当成主角，讲环形缓冲 + stamp 版本号如何无锁实现「预留槽位」。
- **u2-l6（list flavor）**、**u2-l7（zero flavor）**：list 的阻塞者队列用法与 array 同构；zero 则会大量用到本讲的 `register_with_packet` / `store_packet` / `wait_packet` 这条**跨线程递包**链路，是理解会合（rendezvous）的关键。
- **u3-l1（`run_select` 核心算法）**：本讲的 `Selected` 状态机、`try_select`、`register`/`unregister`/`accept` 是 `select!` 调度算法的直接构件，届时会把它们组装成一个完整的多操作选择流程。
- **u3-l5（utils.rs）**：本讲提到的「非毒 `Mutex`」就定义在那里，届时会解释为什么通道库刻意不要 std 的中毒语义。
