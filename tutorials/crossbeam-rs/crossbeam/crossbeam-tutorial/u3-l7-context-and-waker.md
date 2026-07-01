# Context 与 Waker：阻塞与唤醒机制

## 1. 本讲目标

本讲承接 u3-l3 的「flavor 派发与 `SelectHandle` 契约」，钻入 crossbeam-channel 公共接口之下的两块「调度底座」：`context.rs` 与 `waker.rs`。

学完本讲，你应当能够：

- 说清当一个 `recv` 在空通道上阻塞时，线程到底「停在哪里」「靠什么被叫醒」。
- 理解 `Context` 如何用线程局部缓存 + 一把 `Arc<Inner>` 承载「select 状态 + packet + 线程句柄」。
- 解释 `Inner::try_select` 的 CAS 为何是整套「多路选择 / 防丢失唤醒」的核心仲裁器。
- 读懂 `Waker` 的 `selectors` / `observers` 双队列职责，以及 `SyncWaker` 如何用一把互斥锁 + 一个 `is_empty` 快速路径把它们安全地暴露给多线程。
- 画出一条完整的「接收者 park → 发送者 notify → 接收者被唤醒」调用链，并指出防丢失唤醒的关键顺序。

本讲不展开 `select.rs` 的完整选择算法（留待 u3-l9），也不展开各 flavor 的缓冲算法（已在 u3-l4/u3-l5 讲过）。

## 2. 前置知识

在进入源码前，先建立三个直觉。

### 2.1 「阻塞」到底阻塞什么

当线程调用 `Receiver::recv()` 而通道为空时，线程不能空转烧 CPU（busy-wait），也不能直接返回。它需要把执行权交还给操作系统「睡过去」，等到「有消息了」再被唤醒。Rust 标准库里这个「睡 / 醒」原语是 `thread::park()` / `Thread::unpark()`：

- `thread::park()` 让当前线程睡眠，直到有人对它的 `Thread` 句柄调用 `unpark()`，或超时，或发生稀有的「伪唤醒」（spurious wakeup）。
- `unpark()` 投递的是一个**二值令牌**：调用多次等于一次；「先 unpark 后 park」时，令牌会被记住，park 立即返回。这条性质我们在 u2-l5 的 `Parker` 里已经见过，本讲的唤醒机制同样依赖它。

### 2.2 为什么不能「先检查再 park」

朴素的阻塞写法是：

```text
1. 检查通道是否为空；
2. 若空，thread::park()；
```

这有一个经典的**丢失唤醒（lost wakeup）**漏洞：如果第 1 步检查完后、第 2 步 park 前，发送者正好写了一条消息并调用 `unpark()`，那么这个 unpark 发生在「目标线程还没真正 park」的时刻。这时是否还有救，取决于令牌是否被记住——`std::thread::park` 确实会记住一个令牌，所以这一种竞态安全。但**多生产者、多操作、带 select** 的场景下，仅靠单个令牌远远不够，需要一个更严谨的「登记 + 复查 + CAS 仲裁」协议。本讲的 `Context` + `Waker` 正是这个协议的实现。

### 2.3 CAS 仲裁：多线程只能有一个赢家

「select 状态」用一个 `AtomicUsize` 表示。多个线程可能同时想「选中」同一个等待者（例如两个发送者同时唤醒同一个接收者），但接收者只能完成**一次**操作。所以选中动作必须是一次原子的「比较并交换」（CAS）：只有当状态还是 `Waiting` 时才能改成 `Operation(...)`，CAS 失败者算输。这个 CAS 就是 `Context::try_select`，它是整个机制的「唯一裁判」。

> 关键术语速查：`Context`（线程阻塞上下文）、`Inner`（Context 的内层共享状态）、`try_select`（CAS 选中）、`wait_until`（带截止时间的阻塞睡眠）、`Waker`（被阻塞线程的登记表）、`Entry`（一条登记记录）、`SyncWaker`（加锁可共享版 Waker）、`selectors` / `observers`（两类登记队列）、`Selected`（状态枚举）、`Operation`（操作 id）。

## 3. 本讲源码地图

本讲只涉及两个文件，但会从 `select.rs` 和 `flavors/array.rs` 借用少量上下文作为「调用方」佐证。

| 文件 | 角色 |
| --- | --- |
| [crossbeam-channel/src/context.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs) | 线程阻塞上下文 `Context` / `Inner`：承载 select 状态、packet 槽、线程句柄；提供 `try_select`（CAS 仲裁）、`wait_until`（阻塞睡眠）、`unpark`（唤醒）、`store_packet` / `wait_packet`（跨线程交接数据）。 |
| [crossbeam-channel/src/waker.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs) | 被阻塞线程的登记表 `Waker`（单线程内的 `selectors` / `observers` 双队列）与其加锁可共享版本 `SyncWaker`；提供 `register` / `try_select` / `notify` / `disconnect` / `watch` 等。 |
| [crossbeam-channel/src/select.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs) | 借用：`Selected` / `Operation` / `Token` 的定义，以及 `run_select` 如何调用 `Context::with`。 |
| [crossbeam-channel/src/flavors/array.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs) | 借用：有界通道的阻塞 `send` / `recv` 与 `write` 中的 `notify()`，作为「调用方」佐证完整调用链。 |

## 4. 核心概念与源码讲解

本讲拆为三个最小模块：

1. **Context/Inner**：select 状态 + packet + 线程句柄（线程局部缓存）。
2. **try_select CAS + wait_until 阻塞 + unpark**（阻塞与唤醒的核心协议）。
3. **Waker 的 selectors/observers 双队列 + SyncWaker 互斥**（被阻塞线程的登记与通知）。

---

### 4.1 Context/Inner：select 状态 + packet + 线程句柄

#### 4.1.1 概念说明

每个**正在执行 select 或阻塞 send/recv 的线程**都需要一份「我是谁、我在等什么、别人怎么叫醒我」的上下文。crossbeam 把它抽象成 `Context`。

`Context` 本身只是一个 `Arc<Inner>` 的薄包装，真正的状态在 `Inner` 里。`Inner` 只有三样东西：

- **select**：一个 `AtomicUsize`，编码当前 select 状态（`Waiting` / `Aborted` / `Disconnected` / `Operation(op)`）。这是被多线程 CAS 争夺的「裁判字段」。
- **packet**：一个 `AtomicPtr<()>`，是一个「别人可以往里塞指针」的槽。用于在唤醒时顺便把数据/凭证交给被唤醒者（u3-l6 的零容量会合通道就靠它交接 `Packet`）。
- **thread / thread_id**：当前线程的 `Thread` 句柄（用于 `unpark`）和 `ThreadId`（用于区分「自己唤醒自己」）。

为什么是 `Arc<Inner>` 而不是 `Arc<Context>`？因为 `Context` 还要做线程局部缓存复用（见 4.1.3），但登记到 `Waker` 里的「句柄」需要是一份可以独立 clone 的轻量副本——`Arc<Inner>` 的 clone 只增引用计数，多份副本指向同一份状态，正好满足「登记一份、自己留一份」的需求。

#### 4.1.2 核心流程

`Context` 的生命周期围绕一次 select / 阻塞操作展开：

```text
Context::with(|cx| {        // ① 取出（或新建）线程局部 Context
    cx.reset();              // ② 清空上次遗留的 select / packet
    ...                      // ③ 把 cx 登记进 Waker（register）
    cx.try_select(...);      // ④ 复查就绪，必要时 CAS 标记
    sel = cx.wait_until(d);  // ⑤ 阻塞睡眠，被唤醒后返回 Selected
    ...                      // ⑥ unregister、accept 完成
})                           // ⑦ Context 归还线程局部缓存
```

`Selected` 是一个小型状态机，用一个 `usize` 编码，这正是它能塞进 `AtomicUsize` 的原因：

```text
0           => Waiting       （还在等，没人选中）
1           => Aborted       （本次放弃阻塞，例如复查发现就绪/超时）
2           => Disconnected  （通道断开被选中）
其它 usize  => Operation(op)  （某个操作 op 被别人选中了）
```

#### 4.1.3 源码精读

`Context` 与 `Inner` 的字段定义（[crossbeam-channel/src/context.rs:21-23](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs#L21-L23) 与 [crossbeam-channel/src/context.rs:27-39](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs#L27-L39)）——注意 `Context` 只是 `Arc<Inner>` 包装，三个核心字段都在 `Inner`：

```rust
pub struct Context { inner: Arc<Inner> }
struct Inner {
    select: AtomicUsize,      // select 状态：CAS 仲裁字段
    packet: AtomicPtr<()>,    // 别人可往里塞指针的槽
    thread: Thread,           // 线程句柄（unpark 用）
    thread_id: ThreadId,      // 区分自己/他人
}
```

`Selected` 与 `Operation` 的定义在 [crossbeam-channel/src/select.rs:54-92](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L54-L92)，四态枚举与 `usize` 的双向 `From` 实现是「塞进 AtomicUsize」的物理基础。`Operation` 本身就是一个 `usize`（[crossbeam-channel/src/select.rs:35-36](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L35-L36)），由 `Operation::hook` 用一个栈变量的地址当 id（[crossbeam-channel/src/select.rs:45-51](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L45-L51)），并用 `assert!(val > 2)` 防止地址恰好等于 `Waiting/Aborted/Disconnected` 的数值表示。

线程局部缓存复用在 [crossbeam-channel/src/context.rs:44-70](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs#L44-L70)：每个线程首次用 `Context::with` 时通过 `Context::new()`（[crossbeam-channel/src/context.rs:74-83](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs#L74-L83)，标了 `#[cold]`）建一个 `Context` 存进 `thread_local!`；后续每次 `with` 取出来 `reset()`（[crossbeam-channel/src/context.rs:87-92](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs#L87-L92)，把 `select` 和 `packet` 复位为 `Waiting` / null）后复用，闭包返回前再 `cell.set(Some(cx))` 放回缓存。这样高频的 send/recv 不会反复分配 `Arc<Inner>`。

#### 4.1.4 代码实践

**实践目标**：确认 `Context` 是「每线程一份、可复用」的，并理解它的字段含义。

**操作步骤**：

1. 打开 [crossbeam-channel/src/context.rs:44-70](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs#L44-L70)，找到 `thread_local! { static CONTEXT: Cell<Option<Context>> }`。
2. 回答：为什么用 `Cell<Option<Context>>` 而不是 `RefCell`？提示——`with` 把 Context「拿走」用完再「放回」，借用是独占且短暂的，`Cell` 足够且更便宜。
3. 在 `Context::new` 的 `thread: thread::current()` 一行旁，脑内推演：若线程 A 的 `Context` 被线程 B 拿去用，`unpark` 会叫醒谁？（结论：会叫醒 A 而非 B，所以 Context 绝不能跨线程流转——它只在线程局部缓存里活动，跨线程传递的是 clone 出来的另一份 `Arc<Inner>` 副本，那份副本的 `thread` 仍指向原属主线程。）

**预期结果**：能口头说明「`Context` 的 `thread` 字段永远指向它的创建线程，登记到 Waker 后被 `unpark` 的也是这个创建线程」。

**待本地验证**：本实践为源码阅读型，无需运行。

#### 4.1.5 小练习与答案

**练习 1**：`Inner` 里有 `thread` 为什么还要单独存一个 `thread_id`？

> **答案**：`unpark` 需要 `Thread` 句柄，而「判断某个登记项是不是我自己」只需要 `ThreadId`。`ThreadId` 比较 cheap 且可 `Copy`；`Waker::try_select` 在遍历 `selectors` 时先用 `thread_id` 跳过自己的登记项（避免自唤醒），见 [crossbeam-channel/src/waker.rs:94](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L94)。

**练习 2**：`packet` 字段为什么用 `AtomicPtr<()>` 而不是泛型？

> **答案**：`Context` 是 select 的通用底座，对携带的数据类型完全无知；用类型擦除的裸指针 `*mut ()` 配合 `store_packet` / `wait_packet`，让任何 flavor（zero flavor 的 `Packet<T>` 等）都能借用这个槽交接数据，类型还原交给具体 flavor 在读取端完成（见 [crossbeam-channel/src/context.rs:121-138](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs#L121-L138)）。

---

### 4.2 try_select CAS + wait_until 阻塞 + unpark

#### 4.2.1 概念说明

这是整个阻塞唤醒机制的「心跳」。三个方法各司其职：

- **`try_select(sel)`**：尝试把状态从 `Waiting` CAS 成 `sel`。成功说明「我抢到了这次选中权」；失败说明「别人已经抢先选中了别的」，返回当前被选中的值。它是**唯一的状态写入点**（除了 `reset`），所有「选中」「放弃」「断开」都从这里过。
- **`wait_until(deadline)`**：阻塞睡眠，直到状态不再是 `Waiting`（被 `try_select` 改掉）、截止时间到达、或被唤醒后复查。它返回最终的 `Selected`。
- **`unpark()`**：叫醒本 Context 所属线程。供「让别人醒来」的一方调用。

这三者配合 `Waker`（4.3 节）构成「登记 → 复查 → 阻塞 → 被通知唤醒」的完整闭环。

#### 4.2.2 核心流程

完整的防丢失唤醒闭环（以阻塞 `recv` 为例）如下，重点在「**先登记，后复查**」的顺序：

```text
接收者线程                          发送者线程
─────────────                       ─────────────
1. start_recv 失败（空）            （并发）
2. spin+snooze 退避几轮仍未就绪
3. Context::with(|cx| {
4.     receivers.register(oper, cx)   ← 关键：先把自己登记进 Waker
5.     复查: if !is_empty() {
6.         cx.try_select(Aborted)     ← 复查发现就绪，主动放弃阻塞
        }
7.     sel = cx.wait_until(deadline)
8.        └─ load(select)==Waiting → thread::park()  睡眠
                                     A. start_send 成功
                                     B. write(msg) + 改 stamp
                                     C. receivers.notify()
                                        └─ Waker::try_select 找到接收者
                                           ├─ cx.try_select(Operation) CAS 成功
                                           ├─ cx.store_packet(...)
                                           └─ cx.unpark()           ← 唤醒
9.        └─ (被 unpark) 再次 load(select)==Operation → 返回
10.    match sel { Operation(_) => {} }   ← 已被选中，无需 unregister
   })
11. 回到外层 loop，start_recv 这次成功，read 出消息
```

防丢失唤醒的关键有三道保险，对应图中三个位置：

- **保险 1（步骤 4 在 5 之前）**：先登记后复查。若发送者在复查前就写了，复查（步骤 5）会看到 `is_empty()==false`，主动 `try_select(Aborted)`，于是 `wait_until` 不再阻塞。
- **保险 2（步骤 8 的 load 在 park 之前）**：`wait_until` 每轮先 `load(select)`，若已被 `Operation` 改写（发送者的 notify 已经 CAS 成功），立即返回，不 park。
- **保险 3（unpark 的令牌语义）**：即便发送者的 `unpark` 恰好落在接收者「load 之后、park 之前」的窗口，`std::thread::park` 会记住这一个 unpark 令牌，park 立即返回。

CAS 仲裁的语义可形式化为：状态转移 \(\text{Waiting} \xrightarrow{\text{CAS}} S\) 只允许从 `Waiting` 出发，且全程原子；多线程并发尝试时，恰有一个 CAS 成功。即对 \(n\) 个竞争者：

\[
\Pr(\text{恰好一个赢家}) = 1, \quad \sum_{i=1}^{n} \mathbf{1}[\text{线程 } i \text{ CAS 成功}] = 1
\]

#### 4.2.3 源码精读

`try_select` 是一次 CAS（[crossbeam-channel/src/context.rs:98-109](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs#L98-L109)）：

```rust
pub fn try_select(&self, select: Selected) -> Result<(), Selected> {
    self.inner.select.compare_exchange(
        Selected::Waiting.into(),   // 期望：还在等
        select.into(),              // 改成：目标状态
        Ordering::AcqRel,           // 成功：AcqRel
        Ordering::Acquire,          // 失败：Acquire（读到别人写的结果）
    ).map(|_| ()).map_err(|e| e.into())
}
```

`AcqRel` 成功路径保证「CAS 之前的写入（如 packet）对读取该状态的线程可见」，与 `store_packet` 的 `Release`（[crossbeam-channel/src/context.rs:121-125](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs#L121-L125)）形成 release/acquire 配对。

`wait_until` 是阻塞主循环（[crossbeam-channel/src/context.rs:144-169](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs#L144-L169)）：每轮先 `load(Acquire)` 检查状态，非 `Waiting` 立即返回；有截止时间则 `park_timeout`，否则 `park`；到点时 `try_select(Aborted)` 抢占式放弃。注意它**没有显式处理伪唤醒**——因为 `park` 返回后会回到循环顶端重新 `load(select)`，只有状态真的变了才退出，伪唤醒只是白白多转一圈。

`unpark` 直接转发给 `Thread` 句柄（[crossbeam-channel/src/context.rs:173-175](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs#L173-L175)）：

```rust
pub fn unpark(&self) { self.inner.thread.unpark(); }
```

`wait_packet`（[crossbeam-channel/src/context.rs:129-138](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs#L129-L138)）用于「选中后等对方把 packet 塞进来」，用 `Backoff::snooze()` 自旋等指针非空——这是零容量会合通道交接数据时用的（u3-l6）。

调用方佐证——有界通道的阻塞 `recv`（[crossbeam-channel/src/flavors/array.rs:422-444](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L422-L444)）严格遵循「register → 复查 → wait_until → 按 sel 收尾」：

```rust
Context::with(|cx| {
    let oper = Operation::hook(token);
    self.receivers.register(oper, cx);              // 先登记
    if !self.is_empty() || self.is_disconnected() { // 后复查
        let _ = cx.try_select(Selected::Aborted);
    }
    let sel = cx.wait_until(deadline);              // 阻塞
    match sel {
        Selected::Aborted | Selected::Disconnected => {
            self.receivers.unregister(oper).unwrap();
        }
        Selected::Operation(_) => {}  // 被 notify 选中，项已被移除，无需 unregister
    }
});
```

发送者侧的唤醒点在 `write` 末尾的 `self.receivers.notify()`（[crossbeam-channel/src/flavors/array.rs:215-230](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L215-L230)）——这正是上面流程图中的步骤 C。

#### 4.2.4 代码实践

**实践目标**：亲手复现「阻塞 recv 被延迟发送者唤醒」，并把读到的调用链对上号。

**操作步骤**：

1. 新建一个临时二进制（示例代码，非项目原有文件）：

   ```rust
   // 示例代码：演示阻塞 recv 被唤醒
   use crossbeam_channel::{bounded, thread};
   use std::time::Duration;

   fn main() {
       let (s, r) = bounded::<i32>(1);
       thread::scope(|scope| {
           scope.spawn(|_| {
               println!("[recv] 准备阻塞，此刻通道为空");
               let v = r.recv();                       // 进入 array.rs 的 recv 阻塞路径
               println!("[recv] 被唤醒，收到 {:?}", v);
           });
           scope.spawn(|_| {
               thread::sleep(Duration::from_secs(1));  // 制造「先阻塞后发送」
               println!("[send] 现在发送");
               s.send(42);                             // write() 内调用 receivers.notify()
           });
       }).unwrap();
   }
   ```

2. 运行 `cargo run`（依赖 `crossbeam = "0.8"`）。观察输出顺序：`[recv] 准备阻塞` → （1 秒）→ `[send] 现在发送` → `[recv] 被唤醒，收到 Ok(42)`。

3. 对照源码画出调用链（这正是本讲的「综合实践」之一）：

   - 接收者：`recv` → 自旋退避失败 → `Context::with` → `receivers.register` → 复查 `is_empty` 仍真 → `cx.wait_until` → `thread::park`。
   - 发送者：`send` → `start_send` 成功 → `write` 写槽改 stamp → `receivers.notify()` → `SyncWaker::notify` → `Waker::try_select` → 找到接收者 `Entry` → `cx.try_select(Operation)` CAS 成功 → `cx.unpark`。

**需要观察的现象**：接收者确实在发送者发送后才打印「被唤醒」，证明 park/unpark 生效；若把 `bounded(1)` 改成 `bounded(0)`（零容量），路径会切到 `zero.rs`，但「register → notify → unpark」的骨架不变。

**预期结果**：能完整复述这条调用链上每个函数所在的文件与行号。

#### 4.2.5 小练习与答案

**练习 1**：`wait_until` 里 `park` 返回后为什么不直接返回，而要回到循环顶端重新 `load(select)`？

> **答案**：因为 `std::thread::park` 允许**伪唤醒**（spurious wakeup），返回并不保证状态已变。回到循环顶端复查 `select`，只有状态真的离开 `Waiting` 才返回，伪唤醒只是多转一圈。这与 u2-l5 `Parker` 用 `while` 循环复查条件是同一思路。

**练习 2**：`try_select` 成功用 `AcqRel`、失败用 `Acquire`，为什么不对称地都用 `AcqRel`？

> **答案**：CAS 失败时不发生写入，没有「release 自己写入」的需要，只需 `Acquire` 读出赢家写入的状态即可；`AcqRel` 留给成功路径，保证本线程之前的写入（如先 `store_packet(Release)` 再 `try_select`）对后来读取该状态的线程可见。见 [crossbeam-channel/src/context.rs:101-106](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs#L101-L106)。

---

### 4.3 Waker selectors/observers 双队列 与 SyncWaker 互斥

#### 4.3.1 概念说明

`Context` 是「我（一个线程）的阻塞上下文」，而 `Waker` 是「**一群**正在阻塞的线程的登记表」。每个 flavor 内部各持若干个 `SyncWaker`：有界通道 `Array` 有 `senders` 和 `receivers` 两个（[crossbeam-channel/src/flavors/array.rs:89-92](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L89-L92)），分别登记「等着发送」和「等着接收」的线程。

`Waker` 维护两类队列：

- **`selectors`**：登记「我要完成一次 send/recv」的线程。一次 `notify` 会从中**挑一个**（`try_select`）唤醒——因为一个新消息只能满足一个等待的接收者。
- **`observers`**：登记「我只想知道通道是否就绪、自己不一定要动手」的线程（select 的 `ready`/`watch` 路径用）。一次 `notify` 会把 `observers` **全部**唤醒，让它们各自复查。

一条登记记录是 `Entry`（[crossbeam-channel/src/waker.rs:17-26](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L17-L26)）：携带 `oper`（操作 id）、`packet`（要交接的指针）、`cx`（对方线程的 `Context` 副本）。注意 `cx: Context` 是 `cx.clone()` 存进来的——clone 只是 `Arc<Inner>` 增引用，所以登记项握有的是对方线程 Context 的共享句柄。

`Waker` 本身**不是** `Sync`（它就是两个 `Vec<Entry>`，无锁），只能在单线程内使用。真正被各 flavor 跨线程共享的是 `SyncWaker`：它用一把 `Mutex<Waker>` 保护，外加一个 `is_empty: AtomicBool` 做快速路径。

#### 4.3.2 核心流程

**notify（唤醒一个 selector + 全部 observers）** 是最关键的操作。`SyncWaker::notify` 的快速路径设计很巧妙：

```text
SyncWaker::notify():
  1. load(is_empty, SeqCst)；若为 true → 直接返回，不抢锁   ← 快速路径
  2. lock(inner)
  3.   再次 load(is_empty, SeqCst)；若仍 true → 返回          ← 双重检查
  4.   inner.try_select()   ← 从 selectors 挑一个唤醒
  5.   inner.notify()       ← 唤醒全部 observers
  6.   store(is_empty, ...) ← 按剩余队列更新
```

`is_empty` 的意义：通道绝大多数 `notify` 发生在「没有人在等」的时候（典型如先 send 后 recv 的顺序使用）。用一个原子布尔挡在锁前面，可以避免绝大多数情况下的锁竞争——这是高吞吐通道的关键优化。

`Waker::try_select`（被 `SyncWaker::notify` 调用）遍历 `selectors`，找到**第一个属于别的线程**、且能被 `cx.try_select(Operation)` CAS 成功的项，`store_packet` + `unpark` 后把它移出队列（[crossbeam-channel/src/waker.rs:84-111](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L84-L111)）。移除是为了「保持队列干净、提升性能」，也保证了被选中的接收者醒来后无需再 unregister 自己（呼应 4.2.3 的 `Selected::Operation(_) => {}`）。

`disconnect`（通道断开）则遍历 `selectors` 把每一项 CAS 成 `Disconnected` 并唤醒，但**不移除**项——因为被唤醒的线程醒来后要自己 unregister 并可能取回 packet 销毁（[crossbeam-channel/src/waker.rs:155-168](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L155-L168)）。

#### 4.3.3 源码精读

`Entry` 与 `Waker` 字段（[crossbeam-channel/src/waker.rs:17-38](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L17-L38)）：

```rust
pub(crate) struct Entry { oper: Operation, packet: *mut (), cx: Context }
pub(crate) struct Waker {
    selectors: Vec<Entry>,  // 想完成操作的等待者
    observers: Vec<Entry>,  // 只想知道就绪的观察者
}
```

`register` / `register_with_packet`（[crossbeam-channel/src/waker.rs:52-64](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L52-L64)）：把 `cx.clone()` 连同 oper/packet 推入 `selectors`。

`try_select`（[crossbeam-channel/src/waker.rs:84-111](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L84-L111)）——这是「唤醒一个 selector」的核心：

```rust
self.selectors.iter().position(|selector| {
    selector.cx.thread_id() != thread_id         // 不是自己
        && selector.cx.try_select(Selected::Operation(selector.oper)).is_ok()  // CAS 抢中
        && {
            selector.cx.store_packet(selector.packet); // 交接数据
            selector.cx.unpark();                      // 唤醒
            true
        }
}).map(|pos| self.selectors.remove(pos))              // 移出队列
```

`notify`（observers 全员通知，[crossbeam-channel/src/waker.rs:145-151](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L145-L151)）：`drain(..)` 全部 observers，逐个 `try_select(Operation)` 成功才 `unpark`——CAS 失败说明该观察者已被别的操作选中，不重复唤醒。

`SyncWaker` 字段（[crossbeam-channel/src/waker.rs:182-188](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L182-L188)）：

```rust
pub(crate) struct SyncWaker {
    inner: Mutex<Waker>,
    is_empty: AtomicBool,   // 快速路径：无人等待时 notify 不抢锁
}
```

`SyncWaker::notify` 的双重检查快速路径（[crossbeam-channel/src/waker.rs:225-237](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L225-L237)）：锁外先查 `is_empty`，锁内再查一次（防止「锁外读到非空、拿锁前又被清空」的竞态导致无谓加锁，也防止反向竞态）。每个变更方法（`register`/`unregister`/`watch`/`unwatch`/`disconnect`/`notify`）收尾都用 `selectors.is_empty() && observers.is_empty()` 重写 `is_empty`（`SeqCst`），见 [crossbeam-channel/src/waker.rs:202-209](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L202-L209) 等。

> 注：这里用的 `Mutex` 是 `crate::utils::Mutex`（[crossbeam-channel/src/waker.rs:13](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L13)），是 crossbeam-channel 内部的封装，与 `std::sync::Mutex` 行为一致但便于 loom 测试（见 u7-l3）。

`Waker::try_select` 里用 `current_thread_id()`（[crossbeam-channel/src/waker.rs:282-291](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L282-L291)）取当前线程 id——同样是线程局部缓存，避免每次 `notify` 都调 `thread::current().id()`。

#### 4.3.4 代码实践

**实践目标**：观察「多接收者争抢，notify 只唤醒一个」与「disconnect 唤醒全部」的差异。

**操作步骤**：

1. 写一个示例程序（示例代码，非项目原有文件）：用 `bounded(1)` 通道，spawn 3 个接收者线程各自 `recv`（都会阻塞并登记进 `receivers` 这个 SyncWaker），主线程 sleep 0.5s 后发送 1 条消息，再 sleep 0.5s 后 drop 发送端（触发 disconnect）。

   ```rust
   // 示例代码：观察 notify 只唤醒一个、disconnect 唤醒全部
   use crossbeam_channel::{bounded, thread};
   use std::time::Duration;

   fn main() {
       let (s, r) = bounded::<i32>(1);
       thread::scope(|scope| {
           for i in 0..3 {
               let r = r.clone();
               scope.spawn(move |_| {
                   match r.recv() {
                       Ok(v) => println!("[recv {}] 收到 {}", i, v),
                       Err(_) => println!("[recv {}] 断开，无消息", i),
                   }
               });
           }
           thread::sleep(Duration::from_millis(500));
           s.send(1);                 // notify：只唤醒 1 个接收者
           thread::sleep(Duration::from_millis(500));
           drop(s);                   // disconnect：唤醒剩余全部接收者
       }).unwrap();
   }
   ```

2. 运行并观察：只有 1 个接收者打印「收到 1」，另外 2 个打印「断开，无消息」。

3. 对照 [crossbeam-channel/src/waker.rs:84-111](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L84-L111) 解释：`send` 走 `notify` → `try_select` 用 `position` 找到第一个可唤醒者后 `remove`，只唤醒一个；`drop(s)` 走 `disconnect` → 遍历剩余 `selectors` 全部 CAS 成 `Disconnected` 并 `unpark`。

**需要观察的现象**：1 个 Success + 2 个 Disconnected，印证「notify 选一、disconnect 全选」。

**预期结果**：能说清 `Waker::try_select`（选一）与 `Waker::disconnect`（全选，不移除项）在行为与实现上的差异。

#### 4.3.5 小练习与答案

**练习 1**：`SyncWaker::notify` 为什么要做两次 `is_empty` 检查（锁外一次、锁内一次）？

> **答案**：锁外那次是**快速路径**，绝大多数 notify 发生在无人等待时，用一个原子读挡掉锁竞争；锁内那次是**正确性兜底**，防止「锁外读到非空、等到拿到锁时队列已被清空」的反向竞态导致无谓加锁与多余 `try_select`。两次都用 `SeqCst` 保证与 `register`/`unregister` 的 `is_empty` 写入间有严格顺序。见 [crossbeam-channel/src/waker.rs:225-237](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L225-L237)。

**练习 2**：`Waker::try_select` 唤醒一个 selector 后会 `remove`，而 `disconnect` 唤醒时却不 `remove`，为什么？

> **答案**：`try_select` 选中的线程醒来后走 `Selected::Operation(_) => {}` 分支，不再访问登记项，所以必须由 `try_select` 替它移除；`disconnect` 选中的是 `Disconnected`，被唤醒线程醒来后还要 `unregister` 取回自己的 `Entry`（可能要销毁内嵌的 packet），所以不能替它移除。见 [crossbeam-channel/src/waker.rs:155-168](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L155-L168) 与 array.rs 的 `Selected::Aborted | Selected::Disconnected => self.receivers.unregister(oper)` 分支。

**练习 3**：`selectors` 和 `observers` 分别服务 select 的哪两种模式？

> **答案**：`selectors` 服务「必须完成一次操作」的 `select`（阻塞到选中一个 send/recv 并执行它）；`observers` 服务「只问就绪、不执行」的 `ready`（`SelectHandle::watch`/`unwatch`）。前者 notify 时只挑一个，后者 notify 时全部唤醒。对照 select.rs 的 `run_select`（用 register/unregister，[crossbeam-channel/src/select.rs:225-269](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L225-L269)）与 `run_ready`（用 watch/unwatch，[crossbeam-channel/src/select.rs:386-427](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L386-L427)）。

---

## 5. 综合实践

把本讲三个模块串起来：**手绘一条端到端的「阻塞 recv 被唤醒」调用链时序图，并加日志验证。**

任务：

1. **画时序图（纸笔）**：双列时序图，左列接收者线程、右列发送者线程，中间标注共享的 `receivers: SyncWaker` 与接收者的 `Inner`。把下面这些节点按时间顺序连起来，每个节点标注源码位置：

   - 接收者 `recv`（[array.rs:398](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L398)）→ 自旋退避失败 → `Context::with`（[array.rs:422](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L422)）→ `receivers.register`（[array.rs:425](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L425)）→ 复查 `is_empty`（[array.rs:428](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L428)）→ `wait_until` → `thread::park`（[context.rs:166](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs#L166)）。
   - 发送者 `send` → `start_send` 成功 → `write` 写槽改 stamp（[array.rs:224-225](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L224-L225)）→ `receivers.notify()`（[array.rs:228](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L228)）→ `SyncWaker::notify`（[waker.rs:225](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L225)）→ `Waker::try_select`（[waker.rs:84](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L84)）→ `cx.try_select` CAS（[context.rs:98](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs#L98)）→ `cx.unpark`（[context.rs:173](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs#L173)）。
   - 接收者被唤醒 → `wait_until` 复查 `select==Operation` 返回（[context.rs:147-149](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs#L147-L149)）→ `Selected::Operation(_) => {}`（[array.rs:442](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L442)）→ 回外层 loop `start_recv` 成功 → `read`。

2. **加日志验证**：在 4.2.4 的示例程序基础上，在 `recv` 前后与 `send` 前后打印时间戳（用 `std::time::Instant`），确认「接收者 park 的时间段」与「发送者 notify 的时间点」吻合。由于 `context.rs`/`waker.rs` 是私有内部模块，你无法直接在它们里面加日志——请改用「外部行为时序」来间接验证（这是源码阅读型实践的常见约束）。

3. **回答一个开放问题**：如果把 4.2.3 中 `array.rs` 阻塞 recv 的「先 register 后复查」颠倒成「先复查后 register」，会在什么场景下丢失唤醒？用你画的时序图指出那个危险窗口。

**预期结果**：一张完整的双列时序图 + 一段对「先登记后复查」必要性的论证。

## 6. 本讲小结

- `Context` 是「单个线程在一次 select / 阻塞操作中的上下文」，本质是 `Arc<Inner>`，`Inner` 持 select 状态、packet 槽、线程句柄；它用线程局部缓存复用，避免高频 send/recv 反复分配。
- `Inner::select` 是一个 `AtomicUsize` 编码的 `Selected` 状态机（`Waiting`/`Aborted`/`Disconnected`/`Operation`），是整套机制唯一的仲裁字段。
- `try_select` 用 CAS 把 `Waiting` 改成目标状态（成功 `AcqRel`、失败 `Acquire`），是多线程「抢选中权」的唯一入口；`wait_until` 在其上做「复查状态 → park/timeout」循环，天然容忍伪唤醒。
- 防丢失唤醒靠「先 register 进 Waker，后复查就绪」的顺序 + CAS 仲裁 + `unpark` 令牌语义三重保险。
- `Waker` 是被阻塞线程的登记表，分 `selectors`（要完成操作，notify 只挑一个并移除）和 `observers`（只问就绪，notify 全员唤醒）双队列。
- `SyncWaker` 用 `Mutex<Waker>` + `is_empty: AtomicBool` 快速路径把 `Waker` 安全暴露给多线程，绝大多数 notify 因 `is_empty` 挡在锁外而不抢锁。
- 通道断开时 `disconnect` 把所有 selectors CAS 成 `Disconnected` 并唤醒但不移除，留待被唤醒线程自己 unregister 取回/销毁 packet。

## 7. 下一步学习建议

本讲把「阻塞与唤醒」的底座讲透了，接下来可以：

- **u3-l8（tick/at/never）**：看三类只读 flavor 如何只用 `deadline()` 与本讲的 `wait_until` 配合，而不依赖 `selectors`/`register`。
- **u3-l9（select 动态选择算法）**：本讲的 `Context::with` + `register`/`unregister`/`accept` 在 `run_select` 里被编排成完整的五阶段算法（try_select 全部 → register → wait_until → unregister → accept），届时你会看到本讲每一块积木的精确位置。
- **u3-l10（select! 宏）**：看声明式宏如何展开成对 `run_select` 的调用，把本讲的内部 API 包装成用户友好的语法。
- 复习建议：重读 [crossbeam-channel/src/context.rs:144-169](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs#L144-L169) 的 `wait_until` 与 [crossbeam-channel/src/waker.rs:84-111](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L84-L111) 的 `Waker::try_select`，这两段是整个 channel 阻塞机制的「心脏」。
