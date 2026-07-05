# Parker / Unparker 线程挂起

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `Parker` 的 **token（令牌）模型**与 `EMPTY / PARKED / NOTIFIED` 三态机各自代表的含义。
- 跟着 `Inner::park` 的源码走完一遍「快路径 → 加锁 → 状态机 → Condvar 等待」的全过程，并解释它如何同时避免 **虚假唤醒（spurious wakeup）** 与 **丢失唤醒（lost wakeup）**。
- 解释 `Inner::unpark` 为什么必须用 `swap(NOTIFIED)` 而非 CAS、为什么在 `notify_one` 之前要先抢一次 `lock`。
- 理解 `Parker` 与 `Unparker` 如何通过 `Arc<Inner>` 共享同一份状态，以及 `Unparker` 为何可 `Clone`、可跨线程使用。
- 用 `Parker + Unparker` 写出两个线程的「乒乓」交替打印程序。

## 2. 前置知识

### 2.1 为什么要「挂起」一个线程

在 [u2-l5 Backoff](u2-l5-backoff.md) 中我们见过：自旋等待时用 `Backoff` 做指数退避可以缓解 cache line 争用。但 `Backoff::is_completed()` 一旦返回 `true`，就意味着继续自旋已经不划算——此时更合理的是让线程**真正睡过去**，把 CPU 让给别人，直到有人主动把它叫醒。

`Parker` 就是干这件事的原语：让当前线程「停车（park）」阻塞，直到另一个线程通过 `Unparker` 把它「唤醒（unpark）」。它是 `Backoff` 退避用尽后的标准继任者。

### 2.2 两个必须解决的难题

直接用操作系统的「睡眠 / 唤醒」会遇到两个经典问题，理解它们是读懂本讲源码的钥匙：

- **丢失唤醒（lost wakeup）**：唤醒方发了信号，但睡眠方还没真正睡下去，信号就这么被丢了，睡眠方会永远睡死。`Parker` 用 `token` 模型 + 一把 `Mutex` 来根治它。
- **虚假唤醒（spurious wakeup）**：操作系统允许 `Condvar` 在没有显式 `notify` 的情况下把线程捞起来。`Parker` 的对策是：醒来后**用原子操作再次核对状态**，没轮到就继续睡。

### 2.3 token（令牌）模型

`Parker` 文档里说得很直白：每个 `Parker` 关联一个**初始不存在**的 token：

- `park()`：阻塞当前线程，直到 token 可用；返回时**自动消费掉** token。
- `unpark()`：原子地把 token 置为「可用」（如果还不是的话）。

由此得到一个关键性质：**`unpark()` 先于 `park()` 调用时，`park()` 会立刻返回**——因为 token 已经先放好了。正是这条性质让 `park/unpark` 比「先 sleep 再 notify」安全得多：唤醒信号不会因为时序错位而丢失。

### 2.4 Mutex + Condvar 协作

`Parker` 的内部状态由三件套组成：一个原子状态字、一把 `Mutex`、一个 `Condvar`。复习一下标准库的 `Condvar::wait` 语义：它**原子地**释放传入的 `MutexGuard` 并把当前线程挂起，被 `notify` 唤醒后**重新抢锁**才返回。这条「释放锁与睡眠不可分割」的原子性，正是 `unpark` 那把「多余」的锁能防 lost-wakeup 的根源（详见 4.3）。

## 3. 本讲源码地图

本讲只涉及两个文件：

| 文件 | 作用 |
| --- | --- |
| [src/sync/parker.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs) | `Parker`、`Unparker`、`UnparkReason` 与私有 `Inner` 的全部实现 |
| [src/sync/mod.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/mod.rs) | `sync` 模块门面，决定 `Parker / Unparker / UnparkReason` 的导出与 cfg |

模块层面的结论：`parker` 子模块**没有** `not(crossbeam_loom)` 门控，因此即便在 loom 模型测试下 `Parker` 仍然可用（它会经 `crate::primitive::sync` 抽象层退回到 loom 自己的 `Arc/Mutex/Condvar` 实现）。这与 `ShardedLock`、`once_lock` 在 loom 下被禁用形成对照（详见 [u1-l2 模块地图](u1-l2-module-map.md)）。

## 4. 核心概念与源码讲解

### 4.1 Parker / Unparker 与 Inner：结构与共享状态

#### 4.1.1 概念说明

`Parker` 是「持有者本人用来 park 自己」的句柄；`Unparker` 是「别人用来把你 unpark」的句柄。两者**共享同一份内部状态** `Inner`——内含状态字、`Mutex`、`Condvar`。共享靠 `Arc<Inner>` 完成：`Parker` 内部就是包了一个 `Unparker`，而 `Unparker` 内部就是一个 `Arc<Inner>`。于是：

- `Unparker` 可以 `Clone`（只是克隆 `Arc`，引用计数 +1），从而把唤醒能力分发给任意多个线程。
- `Parker` **不能** `Clone`——一个 token 只对应一个停车者，否则语义就乱了。

#### 4.1.2 核心流程

`Parker` 的构造（`new` / `default`）做三件事：

1. 新建一个 `Inner`，其中 `state` 初始化为 `EMPTY`（token 不存在）。
2. 把 `Inner` 装进 `Arc`，包成 `Unparker`。
3. 把这个 `Unparker` 作为 `Parker` 的字段。

`Parker` 的公开方法都是**薄转发**：`park / park_timeout / park_deadline / unparker` 全部委托给内部的 `inner: Arc<Inner>` 对应方法。

#### 4.1.3 源码精读

先看 `Parker` 的结构定义。注意 `_marker: PhantomData<*const ()>`——它让 `Parker` 默认 `!Send` 且 `!Sync`，随后用 `unsafe impl Send` 只把 `Send` 加回来（线程可以把 `Parker` **移动**到另一个线程去用，但不能**共享引用**跨线程并发现 park）：

[src/sync/parker.rs:56-61](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L56-L61) —— `Parker` 结构与手动 `Send`。

`default()` 里完成 `Inner` 的初始装配，`state` 起手就是 `EMPTY`：

[src/sync/parker.rs:63-76](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L63-L76) —— 构造 `Inner { state: EMPTY, lock, cvar }` 并包进 `Arc`。

再看 `Unparker`。它同时 `unsafe impl Send` 与 `unsafe impl Sync`，因为唤醒方天然要在多个线程里被共享调用：

[src/sync/parker.rs:223-228](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L223-L228) —— `Unparker` 持有 `Arc<Inner>`，声明 `Send + Sync`。

`Unparker::clone` 仅克隆 `Arc`，多份句柄指向同一个 `Inner`：

[src/sync/parker.rs:309-315](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L309-L315) —— `Clone for Unparker`。

`Parker` 的几个 park 入口都是一行转发：

[src/sync/parker.rs:109-111](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L109-L111) —— `park()` 转发 `inner.park(None)`。

[src/sync/parker.rs:126-134](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L126-L134) —— `park_timeout` 把 `Duration` 折算成 `Instant` 截止时刻，再委托给 `park_deadline`；若 `checked_add` 溢出（超时极大），退化为无限期 `park()`。

[src/sync/parker.rs:150-152](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L150-L152) —— `park_deadline` 转发 `inner.park(Some(deadline))`，返回 `UnparkReason`。

`UnparkReason` 是一个简单枚举，让带超时的 `park_*` 能告诉调用方「我是被 unpark 弄醒的，还是超时到点的」：

[src/sync/parker.rs:320-327](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L320-L327) —— `UnparkReason::{Unparked, Timeout}`。

最后是私有 `Inner`，本讲真正的主角：

[src/sync/parker.rs:329-337](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L329-L337) —— 三个常量 `EMPTY=0 / PARKED=1 / NOTIFIED=2` 与 `Inner { state, lock, cvar }`。

> 关于 raw 指针转换：`Parker::into_raw / from_raw` 直接委托给 `Unparker::into_raw / from_raw`，后者又委托给 `Arc::into_raw / from_raw`（见 [src/sync/parker.rs:189-191](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L189-L191)、[src/sync/parker.rs:275-300](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L275-L300)）。它用于把句柄擦除成裸指针塞进 FFI 或某些回调里，`from_raw` 是 `unsafe` 的——只能喂 `into_raw` 产出的指针，否则引用计数会失衡。

#### 4.1.4 代码实践

**目标**：体会 token 的「先放后取」语义与 `Unparker` 的可克隆性。

操作步骤（示例代码，非项目原有代码）：

```rust
// Cargo.toml: crossbeam-utils = { version = "0.8", features = ["std"] }
use crossbeam_utils::sync::Parker;

fn main() {
    let p = Parker::new();
    let u1 = p.unparker().clone();
    let u2 = p.unparker().clone(); // 同一个 Inner 的多份唤醒句柄

    u1.unpark(); // 先放 token
    p.park();    // 立即返回，消费 token
    println!("第一次 park 立即返回");

    // 再 park 一次：token 已被消费，此时会阻塞 —— 演示时不要真等
    // p.park(); // 会被永远挂起
    let _ = (u1, u2);
}
```

需要观察的现象：第一次 `park()` 不阻塞、立即返回；若取消注释第二次 `park()`，程序会挂起（因为没人再 `unpark`）。预期结果是看到「第一次 park 立即返回」打印后程序正常退出。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Parker` 不实现 `Sync`，而 `Unparker` 同时实现 `Send + Sync`？

**参考答案**：`Parker` 的语义是「**持有者本人** park 自己」，一份 token 只能对应一个停车者，若 `&Parker` 能跨线程共享，两个线程就可能同时 `park` 同一个 `Parker`，状态机无法表达；故只允许 `Send`（整体移动到另一个线程用）。`Unparker` 只做 `unpark`（幂等地置 token），多个线程并发 `unpark` 是合法且安全的，所以 `Send + Sync`。

**练习 2**：`Parker::new()` 创建后 `state` 是什么值？如果先调用 `unpark()` 再调用 `park()`，`state` 经历了哪些变化？

**参考答案**：初始为 `EMPTY`。`unpark()` 经 `swap(NOTIFIED)` 把它置为 `NOTIFIED`（token 已放）。随后 `park()` 在快路径用 `compare_exchange(NOTIFIED, EMPTY)` 消费 token，把它改回 `EMPTY` 并立即返回，全程不进入 `Condvar` 等待。

---

### 4.2 Inner::park：三态流转与 Condvar 等待

#### 4.2.1 概念说明

`Inner::park` 是把 token 模型落地的核心函数。它要在三种「入场态势」下都正确处理：

- 入场时 `state == NOTIFIED`：token 已就绪，应**立即消费并返回**，根本不睡。
- 入场时 `state == EMPTY`：当前没有 token，需要把自己登记为 `PARKED` 然后睡。
- 入场时 `state == PARKED`：这是**非法**的——一个 `Parker` 只有一个停车者，不会有两个线程同时处于 PARKED；遇到就 panic。

睡下去之后，还要能扛住**虚假唤醒**（醒来后若发现还没被 notify，继续睡）和**超时**两种情况。

#### 4.2.2 核心流程

`Inner::park(deadline)` 的执行过程可拆为四段：

```
1. 快路径（无锁）
   CAS(state: NOTIFIED -> EMPTY, SeqCst, SeqCst) 成功?
     是 -> 消费 token，立即返回 Unparked

2. 截止时间已到?
   deadline <= now -> 返回 Timeout（不睡）

3. 加锁并正式登记为 PARKED
   m = lock.lock()
   CAS(state: EMPTY -> PARKED) ->
     Ok           -> 进入第 4 段等待
     Err(NOTIFIED)-> 漏掉的 notify，swap(EMPTY) 消费后返回 Unparked
     Err(其他)    -> panic（不一致）

4. Condvar 等待循环
   loop:
     m = cvar.wait(m) 或 cvar.wait_timeout(m, 剩余)
       （超时到点 -> swap(state, EMPTY) 据 PARKED/NOTIFIED 返回 Timeout/Unparked）
     CAS(state: NOTIFIED -> EMPTY) 成功?
       是 -> 返回 Unparked
       否 -> 虚假唤醒，回 loop 继续睡
```

注意第 3 段里 `Err(NOTIFIED)` 这条**夹缝分支**：在快路径（第 1 段）读取到 `NOTIFIED` 之后、加锁 CAS（第 3 段）之前，`unpark` 又被调用了一次，使状态再次变成 `NOTIFIED`。此时必须用 `swap(EMPTY)`（一次 acquire 读）与**最近一次** `unpark` 的 release 写同步，从而观察到那次 `unpark` 之前的所有写入（详见 4.2.3 中源码注释）。

#### 4.2.3 源码精读

快路径——消费提前到达的 token，避免任何加锁开销：

[src/sync/parker.rs:340-348](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L340-L348) —— `compare_exchange(NOTIFIED, EMPTY)` 成功即返回 `Unparked`。

截止时刻已过就直接返回 `Timeout`，不进入等待：

[src/sync/parker.rs:351-355](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L351-L355) —— `deadline <= Instant::now()` 时跳过阻塞。

加锁并把状态从 `EMPTY` 推进到 `PARKED`。注意 `Err(NOTIFIED)` 分支的注释：必须在这里 `swap(EMPTY)` 做一次 acquire 读，才能与「快路径之后又来的那次 unpark」同步：

[src/sync/parker.rs:358-374](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L358-L374) —— 抢锁 + `CAS(EMPTY -> PARKED)`，处理漏掉的 `NOTIFIED` 与非法状态。

Condvar 等待循环。带 `deadline` 时用 `wait_timeout`，并在 deadline 已过期时用 `swap(EMPTY)` 把状态还原，根据读到的 `PARKED` 还是 `NOTIFIED` 决定返回 `Timeout` 还是 `Unparked`：

[src/sync/parker.rs:376-396](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L376-L396) —— `cvar.wait` / `wait_timeout` 与超时收尾。

醒来后**再次核对状态**以过滤虚假唤醒——这正是「`Condvar` 可能假醒」的对策：

[src/sync/parker.rs:398-409](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L398-L409) —— `CAS(NOTIFIED -> EMPTY)` 成功才认账返回，否则回 `loop` 继续睡。

> 小结一句：`SeqCst` 在这里被「拉满」用，是因为 `state` 这一个原子字同时承担了「token 是否存在」「是否有人在等」「happens-before 同步」三重职责，用最强的排序最不容易出错；性能敏感的库才会去精打细算降序。

#### 4.2.4 代码实践

**目标**：通过阅读 [tests/parker.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/tests/parker.rs) 三个测试，验证三种返回路径。

操作步骤：

1. 打开 `tests/parker.rs`，定位 `park_timeout_unpark_before`、`park_timeout_unpark_not_called`、`park_timeout_unpark_called_other_thread` 三个测试。
2. 对照本节流程图，分别为每个测试标注它走的是「快路径返回 Unparked」「超时返回 Timeout」「跨线程 unpark 唤醒返回 Unparked」中的哪一条。
3. 运行 `cargo test --features std park`（在本 crate 根目录）。

需要观察的现象：三个测试全部通过。其中 `park_timeout_unpark_before` 验证的正是快路径——它先 `unpark()` 再 `park_timeout()`，每次都应立即返回 `Unparked`，10 轮循环都不会阻塞 500ms 以上。预期结果：测试输出 `3 passed`。若你修改本地源码想观察阻塞行为，可临时把 `park_timeout_unpark_not_called` 的超时改大并打印时间，验证它确实睡满。

#### 4.2.5 小练习与答案

**练习 1**：假设去掉第 4 段循环里醒来后的 `CAS(NOTIFIED -> EMPTY)` 检查，直接返回 `Unparked`，会有什么问题？

**参考答案**：`Condvar` 允许虚假唤醒——线程可能在**没有任何 `notify_one`** 的情况下醒来。若不核对 `state` 就返回，调用方会误以为收到了 token，但实际上 `state` 仍是 `PARKED`，后续逻辑就建立在一个并不存在的「唤醒」之上。核对 `CAS(NOTIFIED -> EMPTY)` 才能区分「真被 unpark 了」和「操作系统假醒」，假醒就回 `loop` 继续睡。

**练习 2**：第 3 段 `Err(NOTIFIED)` 分支为什么要用 `swap(EMPTY, SeqCst)` 而不是简单地「知道它是 NOTIFIED，直接当成功返回」？

**参考答案**：因为从快路径读到 `NOTIFIED` 之后，`unpark` 可能**又被调了一次**。`unpark` 的 `swap(NOTIFIED)` 是一次 release 写，它之前的所有写入必须被 park 线程观测到。`swap(EMPTY)` 是一次 acquire 读，专门用来与「最近一次 unpark」建立 synchronizes-with 关系；若只是「凭印象」当成功而不读 `state`，就缺少这次 acquire，可能看不到那次 unpark 之前写入的数据。

---

### 4.3 Inner::unpark：swap 与「那把多余的锁」

#### 4.3.1 概念说明

`unpark` 看似简单——把 token 置为可用并按需唤醒——但源码里有两处反直觉的设计，恰恰是防 lost-wakeup 的关键：

1. **用 `swap(NOTIFIED)` 而不是 CAS**：即便状态已经是 `NOTIFIED`，也必须再写一次 `NOTIFIED`，目的是产生一次 **release 写**，让消费方（park 线程）的 acquire 读能和**本次** unpark 同步、看到本次 unpark 之前的写入。
2. **`notify_one` 之前先 `lock.lock()` 再立刻 `drop`**：这把锁不是为了保护任何数据，而是为了**等 park 线程安全地进入 `Condvar::wait`**，从而杜绝 lost-wakeup。

#### 4.3.2 核心流程

```
unpark():
  old = state.swap(NOTIFIED, SeqCst)   // 必须是 swap，保证 release 写
  match old:
    EMPTY    -> 直接返回（没人等，token 已放好，下次 park 会立即返回）
    NOTIFIED -> 直接返回（token 早就在了，幂等）
    PARKED   -> 继续往下，需要把人叫醒

  drop(lock.lock().unwrap());          // 关键：等 park 线程进入 wait
  cvar.notify_one();                   // 现在叫醒它才不会丢
```

#### 4.3.3 源码精读

`swap` + 三态分流。注释明确解释了「为什么是 swap 而不是 CAS」——必须无条件做一次 release 写：

[src/sync/parker.rs:412-422](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L412-L422) —— `state.swap(NOTIFIED)`，按 `EMPTY / NOTIFIED / PARKED` 分别处理。

防 lost-wakeup 的关键一把锁。源码注释把窗口讲得很清楚：park 线程从「把 state 设成 PARKED」到「真正在 cvar 上 wait」之间有一段空隙，若 `notify_one` 落在这段空隙里就会被忽略，随后 park 线程睡下去就再也醒不来。好在 park 线程在这段空隙里**一直持着 `lock`**——`Condvar::wait` 会原子地释放锁并睡眠。所以 `unpark` 抢一次 `lock`，就能保证拿到锁时 park 线程**已经**调用了 `wait`（锁已被释放），此时再 `notify_one` 就一定不会丢：

[src/sync/parker.rs:424-433](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-utils/src/sync/parker.rs#L424-L433) —— 先 `drop(lock.lock())` 等待 park 线程就绪，再 `notify_one`。

> 把 `lock.lock()` 拿到后立刻 `drop` 掉，等价于「等持锁者（park 线程）释放锁这一刻过去」。注释还顺带解释了为什么要在 `notify_one` **之前**释放锁：否则 park 线程被 `wait` 唤醒后还得反过来等 unpark 线程释放锁，凭空多一次无谓的上下文切换。

#### 4.3.4 代码实践

**目标**：用「时序图」理解 `unpark` 那把锁如何闭合 lost-wakeup 窗口。本实践为**源码阅读型**，无需运行。

操作步骤：

1. 画两条时间轴，分别代表 park 线程 T1 与 unpark 线程 T2。
2. 在 T1 上标出关键动作：`CAS(EMPTY->PARKED)` → 持有 `lock` → `cvar.wait(m)`（原子释放 lock 并睡）。
3. 在 T2 上标出：`swap(NOTIFIED)` 读到 `PARKED` → `lock.lock()`（**阻塞**，直到 T1 进入 wait 释放锁）→ `drop` → `notify_one`。
4. 思考：如果 T2 没有 `lock.lock()` 这一步，在 T1「设了 PARKED 但还没进 wait」的瞬间 T2 调用 `notify_one`，会发生什么？

需要观察的现象（推理结论）：去掉那把锁后，T2 的 `notify_one` 可能落在 T1 还没真正 `wait` 的空隙里被丢弃；T1 随后 `wait` 就再无人唤醒，永远阻塞——这就是 lost-wakeup。加上 `lock.lock()` 后，T2 必须等到 T1 已经把锁释放（即 T1 已在 `wait` 中），`notify_one` 才被发出，窗口被闭合。

预期结果：你能用自己的话写出一句话总结——「`unpark` 里的 `lock.lock()` 不是为了互斥数据，而是为了与 `Condvar::wait` 的『释放锁 + 睡眠』原子性配合，把唤醒信号精准地投递到 T1 已经能接收它的时刻」。

#### 4.3.5 小练习与答案

**练习 1**：如果 `unpark` 改成 `if state.compare_exchange(EMPTY, NOTIFIED).is_ok() { ... }`（看到 `NOTIFIED` 就直接返回、不写），会有什么正确性问题？

**参考答案**：当 `state` 已经是 `NOTIFIED` 时，CAS 失败、不做任何写。这意味着**第二次** `unpark` 调用没有产生 release 写。park 线程随后在 4.2 的 `Err(NOTIFIED)` 分支做 acquire 读时，只能和**第一次** unpark 同步，从而看不到第二次 unpark 之前写入的数据——出现「unpark 返回了但写操作对 park 线程不可见」的内存可见性 bug。`swap` 保证每次 unpark 都有一次 release 写，根治此问题。

**练习 2**：`unpark` 在 `swap` 后看到 `EMPTY` 就直接 `return` 了，根本没碰 `lock` 和 `cvar`。这安全吗？

**参考答案**：安全。`EMPTY` 表示「没有人在等」——park 线程要么还没开始 park，要么已经返回了。此时只需把 token 放好（`swap` 已经把状态从 `EMPTY` 变成 `NOTIFIED`），将来 park 线程调用 `park()` 时会在快路径消费掉这个 token 立即返回。既然没有线程睡在 `cvar` 上，自然不需要 `notify_one`，也就不需要那把锁。

---

## 5. 综合实践

**任务**：用 `Parker + Unparker` 实现两个线程的「乒乓」交替打印，主线程打印 `B 0..4`，子线程打印 `A 0..4`，最终输出严格交替为 `B0 A0 B1 A1 ... B4 A4`。

为什么这个任务能贯穿本讲？因为它同时依赖：

- **token 模型**（4.1）：`unpark` 先于 `park` 时 `park` 立即返回，保证无论两线程谁先就绪都不会丢信号。
- **三态机的快路径**（4.2）：每次唤醒都是「消费一个 token」。
- **`Unparker` 可克隆 + `Arc<Inner>` 共享**（4.1）：主线程持有对方的 `Unparker` 来叫醒对方。

示例代码（非项目原有代码）：

```rust
// Cargo.toml: crossbeam-utils = { version = "0.8", features = ["std"] }
use crossbeam_utils::sync::Parker;
use std::thread;

fn main() {
    let pa = Parker::new(); // 子线程 A 用
    let pb = Parker::new(); // 主线程 B 用
    let ua = pa.unparker().clone(); // 用来叫醒 A
    let ub = pb.unparker().clone(); // 用来叫醒 B

    let handle = thread::spawn(move || {
        for i in 0..5 {
            pa.park();        // A 等 B 叫醒自己
            println!("A: {}", i);
            ub.unpark();      // A 叫醒 B
        }
    });

    for i in 0..5 {
        println!("B: {}", i);
        ua.unpark();          // B 叫醒 A
        pb.park();            // B 等 A 叫醒自己
    }
    handle.join().unwrap();
}
```

操作步骤：

1. 新建一个 binary crate，加入上面的依赖与 `main`。
2. `cargo run` 观察输出顺序。
3. 把 `pa.park()` 改成 `pa.park_timeout(std::time::Duration::from_secs(1))`，观察返回的 `UnparkReason` 是否始终为 `Unparked`（正常交替下应当如此）。
4. 思考：首轮循环里，A 可能比 B 先执行到 `pa.park()`，也可能后执行——两种情况下输出顺序为何都是 `B0 A0`？

需要观察的现象：输出严格交替 `B0 A0 B1 A1 ... B4 A4`，没有错序、没有死锁。改成 `park_timeout` 后每次返回 `UnparkReason::Unparked`，说明唤醒都来自对方而非超时。

预期结果：稳定交替打印。第 4 步的推理要点——若 A 先到 `pa.park()`：状态 EMPTY→PARKED，A 睡；B 打印 `B0` 后 `ua.unpark()` 看到 PARKED → 叫醒 A；A 醒来打印 `A0`。若 A 后到：B 先打印 `B0` 并 `ua.unpark()` 看到 EMPTY → 仅置 token（NOTIFIED）；A 随后 `pa.park()` 走**快路径**消费 token 立即返回，打印 `A0`。两种竞态都被 token 模型收敛成相同结果。

> 进阶（待本地验证）：尝试**故意**把 `ub.unpark()` 注释掉，程序应在 `B0` 之后卡死（A 永远等不到 `ub`，但 B 也卡在 `pb.park()`）——这能直观验证 token 的「不会重复消费」与「丢了就阻塞」两条性质。

## 6. 本讲小结

- `Parker` 基于 token 模型：`park` 阻塞并消费 token，`unpark` 幂等地放置 token；`unpark` 先于 `park` 时后者立即返回，这是不丢信号的根源。
- 状态用单个 `AtomicUsize` 表示三态：`EMPTY`（无人等、无 token）、`PARKED`（有线程在等）、`NOTIFIED`（token 已放、待消费）。
- `Inner::park` 走「快路径消费 NOTIFIED → 加锁 CAS 登记为 PARKED → Condvar 等待循环」四段流程，醒来后**再次核对状态**以过滤虚假唤醒。
- `Inner::unpark` 用 `swap(NOTIFIED)` 而非 CAS，确保每次调用都有一次 release 写，保证消费方能与之同步、看到 unpark 前的写入。
- `unpark` 在 `notify_one` 前先 `lock.lock()` 再 `drop`，与 `Condvar::wait`「释放锁即睡眠」的原子性配合，闭合了「设 PARKED 到真正 wait」之间的 lost-wakeup 窗口。
- `Parker` 与 `Unparker` 通过 `Arc<Inner>` 共享状态；`Unparker` 可 `Clone`、`Send + Sync`，`Parker` 仅 `Send`，反映「一个停车者、多个唤醒者」的语义。`parker` 子模块无 loom 门控，在 loom 下仍可用。

## 7. 下一步学习建议

- **下一讲 [u3-l2 WaitGroup](u3-l2-waitgroup.md)**：WaitGroup 用同一套「原子计数 + Mutex + Condvar」组合解决「等一批任务全部完成」的问题，本讲建立的「先减后锁防漏通知」直觉在那里会再次出现，可对照阅读。
- **[u3-l3 ShardedLock](u3-l3-shardedlock.md)** 与 **[u3-l4 OnceLock](u3-l4-oncelock.md)**：继续 `sync` 模块的其他原语，分别涉及读多写少的分片锁与惰性初始化。
- **回看 [u2-l5 Backoff](u2-l5-backoff.md)**：体会「自旋退避 → `is_completed()` → 改用 `Parker` 阻塞」这条混合等待曲线在真实并发原语里是如何衔接的。
- **想动手深挖**：阅读标准库 `std::thread::park` / `unpark` 的语义，与本讲的 `Parker` 对比——标准库版绑定线程句柄，而 `crossbeam_utils::Parker` 是独立的、可任意移动的 token 容器，适用场景更灵活。
