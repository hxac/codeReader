# 特殊通道：after / at / tick / never

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清楚 `after` / `at` / `tick` / `never` 这四个构造函数分别创建什么样的「只读通道」，以及它们和 `unbounded` / `bounded` 这类「真实通道」的本质区别。
- 解释「消息按需生成（materialized on demand）」的含义：特殊通道里没有任何 `Sender`，消息是在 `recv` 发生时才被计算/确认出来的。
- 读懂 `at` 如何用 `AtomicBool` 的 `swap` 保证「恰好投递一次」，`tick` 如何用 `AtomicCell` 的 `compare_exchange` 把投递时刻不断向后推。
- 理解 `never` 作为「零大小占位通道」在 `select!` 里的作用。
- 用 `after` 写出超时接收、用 `tick` 写出周期心跳。

本讲只讲**特殊通道本身**，不展开 `select!` 宏的内部实现（那是 u2-l9、u3-l1 的内容），也不重复 array/list/zero 的队列细节（u2-l5/l6/l7）。

## 2. 前置知识

本讲默认你已经掌握以下内容（来自前置讲义）：

- **flavor（风味）抽象**（u2-l1）：`crossbeam-channel` 是「一套统一 API + 六种可替换 flavor」。`Sender<T>` / `Receiver<T>` 只是一个外壳，内部持有一个 `flavor` 字段，所有方法都 `match` 这个字段后转发到底层实现——这套母题叫「按 flavor 分发」。
- **`ReceiverFlavor` 有六个变体**（u2-l1）：`Array` / `List` / `Zero` 是三种「真实通道」，`At` / `Tick` / `Never` 就是本讲要讲的三种「特殊通道」。`SenderFlavor` 只有三个变体，因为特殊通道**没有发送方**。
- **`transmute_copy` 换装技巧**（u2-l1）：`At` / `Tick` 通道的真实消息类型是 `Instant`，但对外要伪装成泛型 `T`。构造函数把 `T` 钉死为 `Instant`，所以 `transmute_copy` 是安全的类型换装。
- **三种阻塞模式**（u1-l3）：非阻塞（`try_recv`）、阻塞（`recv`）、带截止时间（`recv_deadline`）。
- **`Instant` / `Duration`**：Rust 标准库的时间类型。`Instant::now()` 是单调时钟的当前时刻，`Duration` 是一段时间间隔。

几个本讲新引入的术语：

- **惰性投递（lazy / on-demand delivery）**：消息不是被某个 `Sender` 提前塞进队列的，而是在 `recv` 被调用、且时刻已到时，才由通道「现场生成」。
- **会合/容量**：回顾 u1-l2，`capacity()` 返回 `Option<usize>`，零容量通道「既空又满」。
- **CAS（compare-and-swap）**：一种原子操作，「比较当前值是否等于预期，是则写成新值并返回成功」。是 lock-free 编程的基础构件。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| [`src/channel.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs) | 定义对外壳 `Sender` / `Receiver`、`ReceiverFlavor` 枚举，以及 `after` / `at` / `tick` / `never` 四个构造函数。本讲重点看构造函数与接收侧的 flavor 分发。 |
| [`src/flavors/at.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/at.rs) | 「到点投递一次」的通道实现。核心是 `AtomicBool`。`after` 和 `at` 共用这个 flavor。 |
| [`src/flavors/tick.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/tick.rs) | 「周期性投递」的通道实现。核心是 `AtomicCell<Instant>` 的 CAS 推进。 |
| [`src/flavors/never.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/never.rs) | 「永不投递」的占位通道。零大小类型（ZST）。 |

辅助引用（不展开）：

- [`src/utils.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/utils.rs) 的 `sleep_until`，是「睡到某个时刻」的工具函数，`at` / `never` 的阻塞路径都依赖它。
- [`src/select.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs) 的 `Token` 结构体，每种 flavor 在其中占一个字段（`at` / `tick` / `never`）。

## 4. 核心概念与源码讲解

### 4.1 特殊通道的公共设计：只读、惰性、永不（真正）断开

#### 4.1.1 概念说明

先建立一个总印象：`after` / `at` / `tick` / `never` 创建的都是**只有 `Receiver`、没有 `Sender`** 的通道。它们不能用 `send` 塞消息——消息是「按需生成」的。源码注释把这件事说得很直白，三个 flavor 文件开头都写着同一句话：

> Messages cannot be sent into this kind of channel; they are materialized on demand.

这句话是理解整个特殊通道家族的钥匙。对比一下：

| 通道 | 谁产生消息 | 消息存在哪 | 何时「生成」 |
| --- | --- | --- | --- |
| array / list / zero | 调用 `send` 的线程 | 真正写入队列/槽位 | `send` 时 |
| **at** | 通道自己 | 不存储，到点「现场算」 | `recv` 且时刻已到 |
| **tick** | 通道自己 | 不存储，CAS 推进时刻 | `recv` 且时刻已到 |
| **never** | （永远不产生） | — | （永不） |

因为消息是「现场生成」的，特殊通道**不需要 array/list/zero 那套队列、引用计数（`counter.rs`）、`Waker` 阻塞者队列**。它们的实现极其精简：`at.rs` 与 `never.rs` 各只有一百多行，`tick.rs` 稍长也只是因为 `Instant` 的 128-bit 原子对齐处理。

另一个共性：四种特殊通道**永不 `disconnect`**（或者说它们的「断开」语义对用户不可见）。看 `Receiver` 的 `Drop` 实现，`At` / `Tick` / `Never` 三个分支都是空的，什么都不做：

[`src/channel.rs:1184-1197`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1184-L1197) — `Receiver::drop` 中 `At` / `Tick` / `Never` 分支为空，说明它们没有「最后一个人负责销毁堆内存」的义务（`At` / `Tick` 靠 `Arc` 引用计数自动释放，`Never` 是零大小根本没堆内存）。

#### 4.1.2 核心流程：四个构造函数如何分流

四个构造函数都定义在 `src/channel.rs`，它们的分流逻辑可以画成下面这张表：

```
after(d):  Instant::now() ──checked_add(d)──┬── Some(deadline) => At(deadline)
                                            └── None          => never()   // 溢出退化

at(when):  直接 => At(when)                // when 是绝对时刻，调用方自负其责

tick(d):   Instant::now() ──checked_add(d)──┬── Some(t0) => Tick(t0, d)
                                            └── None    => never()         // 溢出退化

never():   const fn => Never<T>             // 零大小，任意 T
```

两个关键设计决策：

1. **`after` 是 `at` 的「相对时间」包装**。`after(duration)` 先把「当前时刻 + duration」换算成绝对 `deadline`，再复用 `at` 的底层 `flavors::at::Channel::new_deadline`。所以 `after` 和 `at` 共用同一个 flavor，只是入口不同。
2. **`after` 和 `tick` 都用 `checked_add` 防溢出**。`Instant::now() + duration` 在 `duration` 极大（接近时间尽头）时会溢出并 panic；`checked_add` 返回 `Option`，溢出时安全地**退化为 `never()`**——即「这个超时永远不会触发」，这是合理且优雅的降级。

#### 4.1.3 源码精读

先看四个构造函数本身：

[`src/channel.rs:181-188`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L181-L188) — `after(duration)`：把相对时间换算成绝对 `deadline`，`checked_add` 溢出时退化为 `never()`。

```rust
pub fn after(duration: Duration) -> Receiver<Instant> {
    match Instant::now().checked_add(duration) {
        Some(deadline) => Receiver {
            flavor: ReceiverFlavor::At(Arc::new(flavors::at::Channel::new_deadline(deadline))),
        },
        None => never(),
    }
}
```

[`src/channel.rs:232-236`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L232-L236) — `at(when)`：直接用绝对时刻构造，不经 `checked_add`（调用方负责给出合法 `Instant`）。

[`src/channel.rs:335-345`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L335-L345) — `tick(duration)`：与 `after` 同构，算出首次投递时刻 `delivery_time`，溢出退化为 `never()`；注意它把 `delivery_time` **和** `duration` 都存了下来（后者是周期）。

[`src/channel.rs:275-279`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L275-L279) — `never<T>()`：`const fn`，泛型 `T` 任意，构造零大小的 `Never` flavor。因为它是常量函数，可以用来定义全局 `static` 通道。

再看 `ReceiverFlavor` 枚举里这三个变体长什么样：

[`src/channel.rs:729-747`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L729-L747) — `At` / `Tick` 包在 `Arc<...>` 里（多 `Receiver` 克隆时共享同一份堆数据），`Never` 直接内联零大小值、不分配。

> 注意一个与 u2-l1 呼应的点：`At` / `Tick` 的真实消息类型是 `Instant`，但 `ReceiverFlavor<T>` 是泛型 `T` 的。在 `recv` / `try_recv` / `read` 里你会看到 `mem::transmute_copy` 把 `Result<Instant, _>` 换装成 `Result<T, _>`——因为 `at` / `tick` 构造函数已经把 `T` 钉死为 `Instant`，所以这是安全的。详见 [`src/channel.rs:836-853`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L836-L853)（`recv` 中的 `At` / `Tick` 分支）。

#### 4.1.4 代码实践：观察「溢出退化为 never」

**实践目标**：验证 `after` / `tick` 在 `duration` 极大时不会 panic，而是退化为 `never`。

**操作步骤**（示例代码，新建一个临时 bin 或在已有 example 里运行）：

```rust
// 示例代码：演示 after/tick 的溢出退化
use std::time::Duration;
use crossbeam_channel::{after, tick, never};

fn main() {
    // 一个接近时间尽头的 duration
    let huge = Duration::from_secs(u64::MAX / 2);

    let r_after = after(huge);   // 内部 checked_add 溢出 -> never()
    let r_tick  = tick(huge);    // 同上 -> never()

    // 这两个 Receiver 行为上等同于 never：try_recv 永远 Empty
    println!("after(huge) try_recv: {:?}", r_after.try_recv()); // 期待 Empty
    println!("tick(huge)  try_recv: {:?}", r_tick.try_recv());  // 期待 Empty

    // 对照：真正的 never
    let r_never = never::<i32>();
    println!("never()    try_recv: {:?}", r_never.try_recv());  // 期待 Empty
}
```

**需要观察的现象**：三条 `try_recv` 都应打印 `Err(Empty)`，且程序不会 panic。

**预期结果**：`after(huge)` 与 `tick(huge)` 行为等同于 `never()`——`checked_add` 返回 `None` 后构造函数走的就是 `never()` 分支。具体在某些平台上 `Instant` 的实际可表示范围不同，是否真的溢出取决于运行环境，**待本地验证**确切边界；但只要溢出发生，降级为 `never` 是确定的。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `after` 需要 `checked_add`，而 `at` 不需要？

**参考答案**：`after(duration)` 内部要做 `Instant::now() + duration`，当 `duration` 极大时这个加法会溢出并 panic，所以必须用 `checked_add` 安全降级。`at(when)` 直接接收调用方给定的 `Instant`，加法由调用方负责，构造函数只是把它存起来，不涉及加法运算，因此不需要 `checked_add`。

**练习 2**：`never::<T>()` 为什么可以是 `const fn`，而 `after` 不是？

**参考答案**：`never` 的底层 `flavors::never::Channel::new()` 只是构造一个零大小的 `PhantomData<T>`，没有任何运行时计算，也不调用 `Instant::now()`，所以能在常量上下文里求值。`after` 必须调用 `Instant::now()`（运行时才能知道「当前时刻」），而 `Instant::now()` 不是 `const fn`，因此 `after` 不可能是 `const fn`。

---

### 4.2 at flavor：用 `AtomicBool` 保证「恰好投递一次」

#### 4.2.1 概念说明

`at`（以及 `after`）通道的语义是：在指定时刻 `delivery_time` 投递**恰好一条**消息，消息内容就是「投递时刻」这个 `Instant` 本身；投递完之后，通道永久为空，但**不会断开**。

关键挑战是「恰好一次」：如果有多个线程持有克隆的 `Receiver` 并同时 `recv`，必须保证**只有一个线程**能拿到这条消息，其他线程拿不到。这不能用普通的 `bool` 标志，因为「检查 + 设置」两步之间会有并发竞争。`at` 的解法极其简洁：一个 `AtomicBool` + 一次 `swap`。

#### 4.2.2 核心流程

数据结构只有两个字段（[`src/flavors/at.rs:19-25`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/at.rs#L19-L25)）：

- `delivery_time: Instant` —— 目标投递时刻，构造后**不可变**。
- `received: AtomicBool` —— 这条消息是否已被取走。

`try_recv` 的三段式判定的伪代码：

```
try_recv():
    if received.load(Relaxed):        # 快路径：已被取走
        return Empty
    if Instant::now() < delivery_time: # 还没到点
        return Empty
    # 到点了，原子地「抢占」这条消息
    if !received.swap(true, SeqCst):   # 旧值 false => 我抢到了
        return Ok(delivery_time)
    else:                              # 旧值 true => 被别人抢先
        return Empty
```

这里有一个**内存序的层次设计**值得品味：

- 第一步 `load(Relaxed)` 只是个**乐观优化**——如果它说「已取走」，那肯定已取走（`true` 是单调的，再不会变回 `false`），用最轻的 `Relaxed` 就够，注释明确写了 "this is just an optional optimistic check"。
- 第三步 `swap(true, SeqCst)` 是**真正的同步点**——它必须保证「检查旧值 + 写入 true」是原子的，所以用最强的 `SeqCst`。`swap` 返回旧值：`false` 表示「在我之前没人改过，我赢了」，`true` 表示「有人抢先了」。这样无论多少线程并发，`swap` 的原子性保证全局只有一次返回 `false`。

阻塞 `recv` 的流程（[`src/flavors/at.rs:63-98`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/at.rs#L63-L98)）多了一层「睡觉等到点」：

```
recv(deadline):
    if received.load(Relaxed):         # 已被别人取走
        sleep_until(deadline);          # 没希望了，等到超时就返回 Timeout（无 deadline 则永久睡）
        return Timeout
    loop:
        取 now 与 delivery_time、外部 deadline 中较早的时刻 sleep
        到点或超时则跳出
    if !received.swap(true, SeqCst):
        return Ok(delivery_time)
    else:
        sleep_until(None); unreachable!()   # 被抢了且不可能再有，永久阻塞
```

注意一个**反直觉但符合语义**的点：阻塞 `recv()`（无超时）在消息已被别的线程取走时会**永久阻塞**，因为 `at` 永不断开、再也不会有新消息。文档里也明说了「never gets disconnected」。

#### 4.2.3 源码精读

[`src/flavors/at.rs:38-59`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/at.rs#L38-L59) — `try_recv`：先 `Relaxed` 快查、再比时刻、最后 `SeqCst` 的 `swap` 抢占。注释点明了两种 `Ordering` 的分工。

```rust
pub(crate) fn try_recv(&self) -> Result<Instant, TryRecvError> {
    if self.received.load(Ordering::Relaxed) {
        return Err(TryRecvError::Empty);
    }
    if Instant::now() < self.delivery_time {
        return Err(TryRecvError::Empty);
    }
    if !self.received.swap(true, Ordering::SeqCst) {
        Ok(self.delivery_time)
    } else {
        Err(TryRecvError::Empty)
    }
}
```

[`src/flavors/at.rs:100-104`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/at.rs#L100-L104) — `read`：从 `Token.at`（类型是 `Option<Instant>`）里取出消息。这是给 `select!` 完成阶段用的——抢占阶段（`try_select`）成功时已经把消息塞进了 `token.at`，`read` 只是取出来。

`SelectHandle` 的实现（[`src/flavors/at.rs:143-194`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/at.rs#L143-L194)）有一个对理解 `select` 很关键的细节：

- `deadline()` 返回 `Some(delivery_time)`（除非已 `received`），告诉 select 算法「这个通道在这个时刻会就绪，到时候来叫醒我」。
- `register` / `watch` 直接返回 `is_ready()`，**不做任何注册**。这与 array/list/zero 完全不同——`at` 不需要在 `SyncWaker` 阻塞者队列里排队，因为它的「就绪」纯粹由时间决定，没有「别的线程来唤醒我」这件事。select 算法靠 `deadline()` 安排定时唤醒即可。

> 这也是为什么 u2-l4 讲的那套 `Context` / `Waker` 机制在本讲几乎不出现：特殊通道是「时间驱动」而非「事件驱动」，不需要阻塞者队列。

#### 4.2.4 代码实践：两线程争抢一条 at 消息

**实践目标**：直观验证 `AtomicBool::swap` 保证「恰好一次投递」——多个接收者并发 `recv`，只有一人成功。

**操作步骤**（示例代码）：

```rust
// 示例代码：观察 at 的「恰好一次」语义
use std::sync::Arc;
use std::thread;
use std::time::{Duration, Instant};
use crossbeam_channel::at;

fn main() {
    let deadline = Instant::now() + Duration::from_millis(100);
    // 用 at 直接构造；多线程通过克隆 Receiver 共享同一个 Arc<at::Channel>
    let r1 = at(deadline);
    let r2 = r1.clone(); // 克隆：见 channel.rs Clone 的 At 分支，Arc::clone

    let t1 = thread::spawn(move || r1.recv());
    let t2 = thread::spawn(move || r2.recv());

    let a = t1.join().unwrap();
    let b = t2.join().unwrap();
    println!("t1 got {:?}", a); // 其中一个是 Ok(deadline)
    println!("t2 got {:?}", b); // 另一个是 Err(RecvError) —— 永久阻塞直到被取空？不，是 Ok/Err 各一
}
```

**需要观察的现象**：两个线程的 `recv()` 中，**恰好一个**返回 `Ok(deadline)`，另一个返回 `Err(RecvError)`（因为 `recv()` 在消息被取走后会因「永久阻塞」而……实际上这里要注意：失败的线程会**永久阻塞**，因为它走的是 `sleep_until(None) + unreachable!()` 路径）。

> ⚠️ **重要提醒**：上面这段代码里，失败的那个 `recv()` 会**永久阻塞**（`at` 不会断开、也不会再有消息）。所以 `join` 实际上会挂住。这是 `at` 的真实行为，不是 bug。要避免实验卡死，应改用 `recv_timeout`，让失败方在超时后返回 `Timeout`，例如 `r1.recv_timeout(Duration::from_millis(300))`。改用 `recv_timeout` 后可观察到：一人 `Ok`、一人 `Err(Timeout)`。

**预期结果**（改用 `recv_timeout` 后）：两条结果一 `Ok(deadline)` 一 `Err(Timeout)`，绝不会两条都 `Ok`——这正是 `swap(SeqCst)` 的原子性在守护的不变量。具体哪个线程赢非确定（取决于调度），**待本地验证**确切分配。

#### 4.2.5 小练习与答案

**练习 1**：`try_recv` 里第一步为什么敢用 `Relaxed`，而第三步必须用 `SeqCst`？

**参考答案**：第一步 `load(Relaxed)` 只是优化：`received` 一旦变 `true` 就单调不会再变 `false`，所以「读到 `true` ⇒ 已被取走」这个推断对正确性无害，读 `false` 也不做最终决策（后面还有 `swap` 兜底），用最轻的序即可。第三步 `swap(true, SeqCst)` 是真正「决定谁能拿走消息」的同步点，必须保证「读旧值 + 写 `true`」原子完成，否则两个线程可能都看到旧值 `false` 都返回 `Ok`，所以用最强的 `SeqCst`。

**练习 2**：阻塞 `recv()`（无 deadline）在消息已被别人取走时会怎样？为什么？

**参考答案**：会**永久阻塞**（源码走 `utils::sleep_until(None)` 后跟 `unreachable!()`）。因为 `at` 通道永不断开、消息只投递一次，被取走后既不会有新消息也不会有 `Disconnected`，阻塞 `recv` 没有「醒来的理由」。这是用户需要注意的语义陷阱——对可能已被消费的 `at` 通道，应该用 `recv_timeout` 或在 `select!` 里配合其他分支。

---

### 4.3 tick flavor：用 `AtomicCell` 的 CAS 周期性推进

#### 4.3.1 概念说明

`tick` 通道做的是「周期性心跳」：每隔 `duration` 投递一次「投递时刻」消息，**永不停止、永不断开**。它和 `at` 的根本区别在于——`at` 投递一次就「锁死」（`AtomicBool` 翻成 `true` 永久为空），而 `tick` 每投递一次就把下一次的投递时刻**往后推一个周期**，于是能反复投递。

推进动作的核心是 **CAS（compare_exchange）**：读到当前的 `delivery_time`，把它和「当前时刻 + 周期」一起原子地「比较并替换」。CAS 成功表示「我抢到了这一拍并把下一拍安排好了」，失败表示「有人抢先改了」，重试即可。

#### 4.3.2 核心流程

数据结构（[`src/flavors/tick.rs:60-67`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/tick.rs#L60-L67)）：

- `delivery_time: AtomicCell<Align<Instant>>` —— **可变**的「下一次投递时刻」，靠 CAS 推进。
- `duration: Duration` —— 周期长度，构造后不可变。

> 这里有个工程细节：`Instant` 在许多平台上是 16 字节（128-bit），标准库的 `AtomicU128` 不一定稳定可用。`crossbeam-utils` 的 `AtomicCell` 能对 128-bit 类型提供 lock-free 原子操作，但需要合适的对齐，所以才包了一层 `Align<Instant>`（[`src/flavors/tick.rs:19-36`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/tick.rs#L19-L36) 在支持的架构上用 `repr(align(16))`）。同文件的 `is_lock_free` 测试（[`src/flavors/tick.rs:38-58`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/tick.rs#L38-L58)）正是在守护「`AtomicCell<Instant>` 确实是 lock-free」这件事。

`try_recv` 的 CAS 循环（[`src/flavors/tick.rs:80-98`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/tick.rs#L80-L98)）：

```
try_recv():
    loop:
        delivery_time = load()
        if now < delivery_time: return Empty      # 还没到下一拍
        if compare_exchange(delivery_time, now + duration).is_ok():
            return Ok(delivery_time)              # 抢到了，返回「本拍时刻」
        # CAS 失败 => 别人改了 delivery_time，回到 loop 重试
```

注意 `try_recv` 推进的新值是 `now + duration`（从「现在」起算下一拍）。CAS 成功后**返回的是旧的 `delivery_time`**——也就是「这一拍本该投递的时刻」，而不是 `now`。这一点很关键：即便接收方晚到了，拿到的消息仍是「这拍的原定时刻」，便于用户知道「这是第几拍」。

阻塞 `recv` 多了一个巧妙的 `.max(now)`（[`src/flavors/tick.rs:116-123`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/tick.rs#L116-L123)）：

```
recv(deadline):
    loop:
        delivery_time = load(); now = Instant::now()
        if 外部 deadline < delivery_time: 睡到 deadline; return Timeout
        if compare_exchange(delivery_time, Align(delivery_time.max(now) + duration)).is_ok():
            if now < delivery_time: sleep(delivery_time - now)   # 提前抢到，等到点
            return Ok(delivery_time)
        # CAS 失败 => 重试
```

`.max(now)` 解决的是「**追赶/补发**」问题。设想接收方因某种原因很久没来 `recv`（比如被别的任务卡住），期间理论上有好几拍错过了。如果每次都从原 `delivery_time` 线性往后推，通道会试图「补发」一串历史 tick；但 `.max(now)` 让推进的新值从「当前时刻」起算，等于**主动跳过**错过的拍子，避免堆积。这对「心跳/看门狗」场景非常合适——你要的是「下一个最近的拍」，不是「补历史」。

用文档里的例子（[`src/channel.rs:307-334`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L307-L334)）能把这件事看得很清楚：周期 100ms，起播后第一次 `recv` 拿到 `start+100`；接着 `sleep(500ms)`；第二次 `recv` 拿到的是 `start+200`（那一拍原定时刻，已严重过期），随后 CAS 把下一拍推到约 `start+700`（而不是 `start+300`）；第三次 `recv` 在 `start+700` 拿到 `start+700`。中间的 300/400/500/600 这些拍被**跳过**了。

#### 4.3.3 源码精读

[`src/flavors/tick.rs:80-98`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/tick.rs#L80-L98) — `try_recv` 的 CAS 循环：

```rust
pub(crate) fn try_recv(&self) -> Result<Instant, TryRecvError> {
    loop {
        let now = Instant::now();
        let delivery_time = self.delivery_time.load();
        if now < delivery_time.0 {
            return Err(TryRecvError::Empty);
        }
        if self.delivery_time
            .compare_exchange(delivery_time, Align(now + self.duration))
            .is_ok()
        {
            return Ok(delivery_time.0);
        }
    }
}
```

[`src/flavors/tick.rs:101-130`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/tick.rs#L101-L130) — 阻塞 `recv`，注意第 120 行 `delivery_time.0.max(now) + self.duration` 这处 `.max(now)` 是「跳过历史拍」的关键。

`SelectHandle` 实现（[`src/flavors/tick.rs:163-208`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/tick.rs#L163-L208)）与 `at` 同构：`deadline()` 永远返回 `Some(当前 delivery_time)`（[`src/flavors/tick.rs:179-182`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/tick.rs#L179-L182)），让 select 算法每次都能拿到「下一拍时刻」来安排定时唤醒；`register` / `watch` 直接返回 `is_ready()`，不在阻塞者队列里排队。

> 与 `at` 对比记忆：`at` 的 `deadline()` 在 `received` 后返回 `None`（再无将来），`tick` 的 `deadline()` 永远返回 `Some`（永远有下一拍）。

#### 4.3.4 代码实践：周期心跳 + 观察「跳过历史拍」

**实践目标**：用 `tick` 实现周期打印，并验证接收方滞后时通道会「跳过」错过的拍而非补发。

**操作步骤**（示例代码）：

```rust
// 示例代码：tick 周期心跳与「跳过」行为
use std::thread;
use std::time::{Duration, Instant};
use crossbeam_channel::tick;

fn main() {
    let start = Instant::now();
    let ticker = tick(Duration::from_millis(100));

    // 第 1 拍：约 start+100 收到，消息内容 == start+100
    let m1 = ticker.recv().unwrap();
    println!("recv1: msg={:?}, elapsed={:?}", m1, start.elapsed());

    // 故意睡 500ms，错过中间几拍
    thread::sleep(Duration::from_millis(500));

    // 第 2 次 recv：拿到的是 start+200（原定时刻），但此时 elapsed≈600ms
    let m2 = ticker.recv().unwrap();
    println!("recv2: msg={:?}, elapsed={:?}", m2, start.elapsed());

    // 第 3 次 recv：因为 .max(now)，下一拍被推到 ≈ start+700，不再补 300/400/500/600
    let m3 = ticker.recv().unwrap();
    println!("recv3: msg={:?}, elapsed={:?}", m3, start.elapsed());
}
```

**需要观察的现象**：

- `recv1` 的 `msg` ≈ `start+100ms`，`elapsed` ≈ 100ms。
- `recv2` 的 `msg` ≈ `start+200ms`（这一拍原定时刻），但 `elapsed` ≈ 600ms（说明它「迟到」地收到了已过期的那一拍）。
- `recv3` 的 `msg` ≈ `start+700ms`，`elapsed` ≈ 700ms——证明通道**没有**试图补发 300/400/500/600 这些拍，而是从「现在」重新计周期。

**预期结果**：三次消息时刻大致为 `+100` / `+200` / `+700`，与 [`src/channel.rs:307-334`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L307-L334) 文档示例的断言一致。具体毫秒数受调度与平台时钟精度影响，**待本地验证**精确数值（GitHub 的 macOS runner 还专门用 `cfg!(gha_macos_runner)` 跳过这类计时断言，可见时间敏感测试容差较大）。

#### 4.3.5 小练习与答案

**练习 1**：`tick` 的 `try_recv` 用 `compare_exchange` 把 `delivery_time` 从「旧值」改成 `now + duration`。为什么这里用「`now + duration`」而不是「`旧 delivery_time + duration`」？

**参考答案**：`try_recv` 是非阻塞调用，能走到 CAS 说明「现在（`now`）已经到/过了 `delivery_time`」。用 `now + duration` 表示「从当下起再等一个周期」就是下一拍，语义直观且不会让下一拍落在「过去」。注意阻塞 `recv` 里用的是 `delivery_time.max(now) + duration`——若接收方准时或提前（`now < delivery_time`），从原 `delivery_time` 推进，保持节拍稳定；若已严重滞后（`now > delivery_time`），则从 `now` 起算，跳过历史拍。两处都在「不补发历史」与「保持节拍」间取了合理的平衡。

**练习 2**：`tick` 通道的 `capacity()` 返回什么？「满」意味着什么？

**参考答案**：返回 `Some(1)`（[`src/flavors/tick.rs:158-160`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/tick.rs#L158-L160)）。`is_full()` 等价于「当前时刻已到/过 `delivery_time`」（即至少有一拍待取），是 `!is_empty()`。所以「满」不是说队列里堆了一堆 tick，而是「现在有一拍可以取」。

---

### 4.4 never flavor：永不投递的零大小占位通道

#### 4.4.1 概念说明

`never` 通道的语义极其简单：**永不投递任何消息，永不断开**。它的真正价值不在自身，而在 `select!` 里做**占位分支**。

典型场景（也是 [`src/channel.rs:243-274`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L243-L274) 文档示例）：你的程序**可能**需要一个超时，也可能不需要（取决于运行时配置）。如果直接写 `select! { recv(r) => ..., recv(after(d)) => ... }`，那 `after(d)` 这个超时分支就**强制的**。用 `never` 可以把它变成「可选」：

```rust
let timeout = duration.map(after).unwrap_or_else(never);
select! {
    recv(r) => ...,
    recv(timeout) => println!("timed out"),  // duration 为 None 时这里是 never，永不触发
}
```

`never` 让「有无超时」这种条件分支统一成「总是有两个分支的 `select!`」，避免了为两种情况写两套 select 代码。

#### 4.4.2 核心流程

数据结构（[`src/flavors/never.rs:19-21`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/never.rs#L19-L21)）：

```rust
pub(crate) struct Channel<T> {
    _marker: PhantomData<T>,
}
```

这是一个**零大小类型（ZST）**——`PhantomData<T>` 不占内存，`T` 只是用来满足「这是一个 `Receiver<T>`」的类型签名，运行时什么都不存。`new()` 也是 `const fn`（[`src/flavors/never.rs:25-30`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/never.rs#L25-L30)），所以可以定义全局 `static NEVER: Receiver<MyType> = never();`。

所有方法的语义都对应「永不」：

| 方法 | 行为 |
| --- | --- |
| `try_recv` | 恒返回 `Err(Empty)` |
| `recv(deadline)` | `sleep_until(deadline)` 后返回 `Err(Timeout)`；无 deadline 则永久阻塞 |
| `read` | 恒返回 `Err(())` |
| `is_empty` | 恒 `true` |
| `is_full` | 恒 `true`（**既空又满**！与零容量通道一致） |
| `len` | 恒 `0` |
| `capacity` | 恒 `Some(0)` |
| `is_ready`（select） | 恒 `false` |
| `deadline`（select） | 恒 `None`（没有「将来就绪」的时刻） |

`SelectHandle` 里 `try_select` 恒返回 `false`（[`src/flavors/never.rs:77-80`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/never.rs#L77-L80)），意味着 `never` 在 select 里**永远不可能被选中**——它是个纯粹的「陪跑」分支，既不影响公平性也不主动触发。

#### 4.4.3 源码精读

[`src/flavors/never.rs:32-43`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/never.rs#L32-L43) — `try_recv` / `recv`：一个恒 `Empty`，一个睡到 deadline 后返回 `Timeout`。

```rust
pub(crate) fn try_recv(&self) -> Result<T, TryRecvError> {
    Err(TryRecvError::Empty)
}
pub(crate) fn recv(&self, deadline: Option<Instant>) -> Result<T, RecvTimeoutError> {
    utils::sleep_until(deadline);
    Err(RecvTimeoutError::Timeout)
}
```

这里的 `utils::sleep_until(deadline)`（定义于 [`src/utils.rs:43-55`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/utils.rs#L43-L55)）是「睡到某个时刻」的统一工具：`None` 时睡很久（`Duration::from_secs(1000)`）近似永久阻塞，`Some(d)` 时睡到 `d`。`at` 的「已被取走」分支和 `never` 的 `recv` 都复用它。

[`src/flavors/never.rs:76-112`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/never.rs#L76-L112) — 完整的 `SelectHandle` 实现：`try_select` 恒 `false`、`deadline` 恒 `None`、`is_ready` 恒 `false`。这套「全否」的实现让 `never` 在 select 算法里完全透明。

再回头看 `Receiver` 外壳对 `Never` 的几处特殊处理：

- **`same_channel`**（[`src/channel.rs:1167`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1167)）：两个 `Never` 通道被判为 `same_channel == true`——因为它们都没有身份（零大小、无数据），视为「同一个」是合理的。
- **`Clone`**（[`src/channel.rs:1207`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1207)）：克隆 `Never` 直接 `flavors::never::Channel::new()` 造一个新的零大小值，而不是引用计数共享。
- **`addr`**（[`src/channel.rs:1179`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1179)）：返回 `0`，与 `At` / `Tick` 用 `Arc` 指针地址不同。

#### 4.4.4 代码实践：用 never 做可选超时占位

**实践目标**：复刻文档示例的核心思路，验证 `never` 在 `select!` 里作为「禁用的超时分支」时确实永不触发。

**操作步骤**（示例代码）：

```rust
// 示例代码：never 作为可选超时占位
use std::thread;
use std::time::Duration;
use crossbeam_channel::{after, never, select, unbounded};

fn main() {
    let (s, r) = unbounded();

    thread::spawn(move || {
        thread::sleep(Duration::from_millis(50));
        s.send(1).unwrap();
    });

    // 假设这个 duration 来自配置，可能是 Some 也可能是 None
    for duration in [Some(Duration::from_millis(100)), None] {
        let timeout = duration.map(after).unwrap_or_else(never);
        select! {
            recv(r) -> msg => println!("收到: {:?} (duration={:?})", msg, duration),
            recv(timeout) -> _ => println!("超时! (duration={:?})", duration),
        }
    }
}
```

**需要观察的现象**：两轮循环都应该打印「收到: Ok(1)」。即使第二轮 `duration = None` 导致 `timeout` 是 `never()`，`recv(r)` 仍能正常收到消息——`never` 分支既不干扰也不触发。

**预期结果**：两次都走 `recv(r)` 分支。如果把第一轮的 `after(100ms)` 改成 `after(1ms)`（比 `s.send` 的 50ms 更早），第一轮就会走「超时」分支，而第二轮（`never`）仍会等到 `recv(r)` 成功——这正体现了 `never`「永不触发」的特性。**待本地验证**（`select!` 宏的细节在 u2-l9 详述，本实践只关注 `never` 的占位效果）。

#### 4.4.5 小练习与答案

**练习 1**：`never` 通道的 `is_empty()` 和 `is_full()` 都返回 `true`，这是否矛盾？

**参考答案**：不矛盾，它是「零容量」通道的共有特性（回顾 u1-l3 的零容量语义）。`is_empty == true` 表示「没有消息可取」，`is_full == true` 表示「容量已满（容量是 0，自然时刻刻满）」。对 `never` 来说这两个状态同时成立：它容量为 0 且永远没有消息。

**练习 2**：为什么 `never` 的 `same_channel` 对任何两个 `never` 通道都返回 `true`？

**参考答案**：`never` 是零大小类型，没有任何运行时状态或身份（没有堆分配、没有指针地址、没有计数）。两个 `never` 通道在运行时不可区分，「视为同一个」既无害又符合直觉——它们行为完全一致（都永不投递）。所以 [`src/channel.rs:1167`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1167) 直接对 `Never` 变体返回 `true`，而不像 `At` / `Tick` 那样比较 `Arc` 指针。

---

## 5. 综合实践

把三种特殊通道串起来，写一个「带超时与周期心跳的任务等待器」。

**任务**：你有一个可能长时间不产出消息的工作通道 `r: Receiver<String>`。你希望：

1. 每秒打印一次心跳（用 `tick`），证明主循环还活着。
2. 如果某条消息等了超过 3 秒还没来，打印「看门狗超时」（用 `after`）。
3. 让「是否启用看门狗」可配置——关闭时用 `never` 占位。

**参考实现骨架**（示例代码）：

```rust
// 示例代码：综合实践 — tick 心跳 + after 看门狗 + never 占位
use std::time::Duration;
use crossbeam_channel::{after, never, select, tick, unbounded};

fn main() {
    let (s, r) = unbounded::<String>();

    // 模拟一个慢生产者（可选：在另一线程 s.send(...) ）

    let heartbeat = tick(Duration::from_secs(1));
    let watchdog_on = true; // 改成 false 试试 never 占位
    let watchdog = if watchdog_on {
        after(Duration::from_secs(3))
    } else {
        never()
    };

    loop {
        select! {
            recv(r) -> msg => {
                println!("工作通道: {:?}", msg);
                if msg.is_err() { break; } // 断开，退出
            }
            recv(heartbeat) -> _ => println!("  ❤ heartbeat"),
            recv(watchdog) -> _ => {
                println!("⏰ 看门狗超时！");
                break;
            }
        }
    }
}
```

**思考题（结合本讲源码）**：

- `tick` 的心跳为什么不会因为某次 `recv` 卡住而「补发」一串心跳？（答：`.max(now)` 让 CAS 推进从当前时刻起算，跳过历史拍。）
- 把 `watchdog_on` 设为 `false` 后，`select!` 里多出的 `recv(watchdog)` 分支为何不会永远让 select 卡死？（答：`never` 的 `try_select` 恒 `false`、`is_ready` 恒 `false`，它在 select 算法里完全透明，其他就绪分支照常被选中。）
- 若把 `after(3s)` 换成 `at(某个过去时刻)`，第一次进入 select 会发生什么？（答：时刻已过，`at` 立即就绪，`try_select` 成功，看门狗分支会被立即选中。）

> 这个综合实践把本讲的三个最小模块（at / tick / never）和「构造函数分流」全部用上了。`select!` 宏的内部展开机制本身是 u2-l9、u3-l1、u3-l3 的主题，本实践只把它当工具使用。

## 6. 本讲小结

- `after` / `at` / `tick` / `never` 创建的都是**只读、无 `Sender`** 的通道，消息「按需生成」而非被 `send` 写入，因此不需要 array/list/zero 的队列、`counter.rs` 引用计数、`Waker` 阻塞者队列，实现极简。
- 四个构造函数（[`src/channel.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs)）按时间分流：`after` = `Instant::now() + duration` → `At` flavor；`at` 直接用绝对时刻 → `At`；`tick` → `Tick`；三者 `checked_add` 溢出都退化为 `never()`；`never` 是 `const fn` 零大小通道。
- `at` 用一个 `AtomicBool` + `swap(SeqCst)` 保证「恰好投递一次」：`Relaxed` 的 `load` 仅作乐观快查，`SeqCst` 的 `swap` 才是真正的同步决策点；阻塞 `recv` 在消息被取走后会永久阻塞。
- `tick` 用 `AtomicCell<Instant>` 的 `compare_exchange` 把投递时刻周期性向后推，能反复投递；阻塞 `recv` 里的 `delivery_time.max(now)` 让通道在接收方滞后时**跳过历史拍**而非补发。
- `never` 是零大小占位通道，所有方法都返回「永不」（`try_select` 恒 `false`、`is_ready` 恒 `false`），在 `select!` 里完全透明，用于把「可选超时」这类条件分支统一成固定结构的 select。
- 三者都不走 `counter.rs`：`At` / `Tick` 靠 `Arc` 共享，`Never` 是零大小；`Drop` 对这三个 flavor 都是空操作，永不真正断开。

## 7. 下一步学习建议

- **学 `select!` 宏的使用**（u2-l9）：本讲的 `at` / `tick` / `never` 最常见的归宿就是 `select!` 的某个分支。学完 u2-l9 你会理解「`recv(after(timeout)) -> _ => ...`」这种超时惯用法是怎么被宏编译的。
- **学 `SelectHandle` 的对接细节**（u3-l2）：本讲提到 `at` / `tick` / `never` 的 `register` / `watch` 直接返回 `is_ready()`、不在阻塞者队列排队——这套机制如何与 select 核心算法（u3-l1 的 `run_select`）协作，将在 u3-l2 详细剖析。
- **深入内存序**（u3-l4）：本讲点到 `at` 用 `Relaxed` / `SeqCst` 的分工、`tick` 用 `AtomicCell` 的 lock-free 保证。如果你对「为什么这里能用 Relaxed」「128-bit 原子如何保证 lock-free」感兴趣，u3-l4 会系统讲并发正确性。
- **建议继续阅读的源码**：把 [`src/flavors/at.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/at.rs) 和 [`src/flavors/tick.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/tick.rs) 的 `SelectHandle` 实现与 [`src/select.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs) 的 `Token`（L24-L32）对照看，理解每种 flavor 如何在 `Token` 里占一个字段、`deadline()` 如何驱动 select 的定时唤醒。
