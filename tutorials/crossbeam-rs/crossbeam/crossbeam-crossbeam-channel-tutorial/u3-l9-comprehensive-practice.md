# 综合实践与架构取舍

## 1. 本讲目标

本讲是整本手册的收尾篇。前面 24 篇讲义把 crossbeam-channel 从「怎么用」一路拆到了「select 内核、内存序、unsafe 正确性、no_std 编译」。本讲不再引入新的内部机制，而是把所学**串起来用一遍**，并退后一步讨论「在真实项目里该如何取舍」。

学完本讲，你应当能够：

- 读懂仓库自带的三个示例（fibonacci / matching / stopwatch），把每个示例里用到的 flavor、API、select 分支与前面讲过的内部实现对上号。
- 面对一个真实并发场景，能**独立选出**合适的 flavor（array / list / zero / at / tick / never）与合适的 select 方式（`select!` 宏 / `Select` 动态 API / 直接阻塞收发）。
- 说清「无锁 / 少锁」与「锁保护」两类实现各自的适用边界，理解 crossbeam-channel 在 array/list 与 zero 之间为何做出不同的取舍。
- 在二次开发或扩展时（例如加日志、换队列结构、做 no_std 移植）知道**该改哪一层、不该动哪一层**。
- 动手把三个示例融合成一个「多 worker 任务分发 + 超时 + 周期心跳」的小程序。

本讲强烈依赖 u2-l10（`Select` 动态 API）与 u3-l2（`SelectHandle` 与 flavor 对接）的认知：示例里的 `select!` 宏在编译期展开后，调用的正是 `internal` 模块里那几个函数。

## 2. 前置知识

在动手前，先用三段话把「怎么选通道」的直觉钉死。

**第一，先问「谁在生产、谁在消费、要不要缓冲」。** crossbeam-channel 的六种 flavor 可以分成两大类：

- **数据通道（array / list / zero）**：有真实的 `Sender`，消息被「写入」再「读出」。三者只差**容量策略**：`bounded(cap>0)` 是固定大小的环形数组（array），`unbounded()` 是无限增长的链表（list），`bounded(0)` 是零容量会合通道（zero，发收必须同时在场）。
- **时间通道（at / tick / never）**：**没有 `Sender`**，是只读的 `Receiver`，消息「按需生成」：`at` 在某个时刻投递一次、`tick` 周期性投递、`never` 永不投递。它们几乎只用来在 `select!` 里充当「超时分支」或「心跳分支」。

所以选 flavor 的第一步通常是：**主数据流选 array/list/zero，附属的「定时/超时」需求选 at/tick/never。**

**第二，再问「要不要背压、要不要让生产者等」。**

- 想让生产者在队列满时自动慢下来（背压），选 **bounded(cap>0)**（array）。
- 想让生产者永远不阻塞、能多快发多快，代价是内存可能无限涨，选 **unbounded**（list）。
- 想做严格的「一对一握手」（生产者和消费者必须同步会合，谁先到谁等），选 **bounded(0)**（zero）。

**第三，最后问「要在多个通道操作之间做选择吗」。** 如果主循环要同时等待「任务」「心跳」「超时」「关闭信号」等多条通道，就用 **`select!` 宏**（分支数编译期已知）或 **`Select` 动态 API**（分支数运行时可变）。如果只是单纯收一条，直接 `recv()` 即可，连 `select!` 都不必上——单 `recv` 分支的 `select!` 会被宏优化成普通 `recv()` 调用（见 u3-l3）。

> 关键术语回顾：flavor（风味）、会合（rendezvous）、背压（backpressure）、`select!`/`select_biased!`、`Select` 动态 API、`internal` 隐藏模块。这些在前序讲义已建立，本讲直接使用，不再重新定义。

## 3. 本讲源码地图

本讲引用的文件集中在「示例」与「外壳层分发」，不深入任何 flavor 内部实现。

| 文件 | 作用 |
| --- | --- |
| [examples/fibonacci.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/examples/fibonacci.rs) | 用 `bounded(0)` 会合通道驱动一个异步斐波那契生成器，演示「消费一停，生产就停」的背压。 |
| [examples/matching.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/examples/matching.rs) | 用 `bounded(1)` + `select!` 在同一个通道上「同时收发」，演示会合式随机配对（移植自 Go）。 |
| [examples/stopwatch.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/examples/stopwatch.rs) | 用 `tick` 心跳 + 信号通道 + `select!` 编排一个定时打印、Ctrl+C 退出的主循环。 |
| [src/channel.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs) | 所有构造函数（`unbounded`/`bounded`/`after`/`at`/`tick`/`never`）与 `Sender`/`Receiver`「按 flavor 分发」的外壳。 |
| [Cargo.toml](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/Cargo.toml) | `dev-dependencies` 里声明了 `signal-hook`（stopwatch 用）与 `crossbeam-utils`（matching 用 `thread::scope`），示例靠 `cargo run --example` 自动发现。 |

> 说明：仓库没有 `examples/Cargo.toml`，`examples/` 是包内的扁平目录，Cargo 会自动把其中每个 `.rs` 当作一个独立 example 目标，运行命令为 `cargo run --example <名字>`（如 `cargo run --example fibonacci`）。

## 4. 核心概念与源码讲解

### 4.1 示例精读：fibonacci —— 会合通道驱动的生产者-消费者

#### 4.1.1 概念说明

`fibonacci.rs` 是三个示例里最短的一个，但它一次性展示了 crossbeam-channel 最优雅的一个特性：**通道本身可以作为生产者和消费者之间的节流阀**。

这里的「异步」不是 async/await，而是「另一个线程」。生成器线程不断把斐波那契数 `send` 进通道，主线程 `recv` 出来打印。关键在于通道用的是 `bounded(0)`——零容量会合通道。会合通道不缓冲任何消息，**每一次 `send` 都必须等一个 `recv` 与它配对才能完成**。于是主线程 `take(20)` 取走 20 个数后停止接收，生成器线程的下一次 `send` 就会永远阻塞，循环自然终止——这就是注释里说的 "until it becomes disconnected"。

#### 4.1.2 核心流程

```
主线程                         生成器线程
   |                               |
   |  r.iter().take(20)            |  while sender.send(x).is_ok()
   |  发起第 1 次 recv             |  发起第 1 次 send → 配对成功，消息过手
   |  <───────────会合────────────>|
   |  打印 0                       |  x,y 前进
   |  发起第 2 次 recv             |  发起第 2 次 send → 配对成功
   |  ...（重复 20 次）...         |  ...
   |  take 结束，r 被 drop         |  发起第 21 次 send → 永久阻塞
   |                               |  （主线程 return，进程退出，线程随之结束）
```

要点：

- **会合 = 强同步**：发送与接收在时间上对齐，没有任何消息在通道里「停留」。
- **背压天然存在**：消费者不收，生产者就被卡住，不会无限算下去。这恰好是我们想要的行为——只要前 20 个数。
- **drop 即停止**：`take(20)` 用完后，`for` 循环结束、`r`（`Receiver`）在 `main` 返回时被 drop；此时通道发送侧会发现接收侧已断开，`send` 返回 `Err`，`while` 条件为假，生成器线程也退出。

#### 4.1.3 源码精读

构造函数选用 `bounded(0)`，它内部走的是 **zero flavor**：

```rust
// examples/fibonacci.rs
let (s, r) = bounded(0);
thread::spawn(|| fibonacci(s));
for num in r.iter().take(20) {
    println!("{}", num);
}
```

参见 [examples/fibonacci.rs:17-25](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/examples/fibonacci.rs#L17-L25)：`bounded(0)` 创建会合通道，`thread::spawn` 把 `Sender` 移交给生成器线程，主线程用 `r.iter().take(20)` 只消费前 20 个。

为什么 `bounded(0)` 一定是 zero flavor？看构造函数分流：

```rust
pub fn bounded<T>(cap: usize) -> (Sender<T>, Receiver<T>) {
    if cap == 0 {
        let (s, r) = counter::new(flavors::zero::Channel::new());
        // ... 包装成 SenderFlavor::Zero / ReceiverFlavor::Zero
    } else {
        let (s, r) = counter::new(flavors::array::Channel::with_capacity(cap));
        // ... 包装成 SenderFlavor::Array / ReceiverFlavor::Array
    }
}
```

参见 [src/channel.rs:113-133](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L113-L133)：`cap == 0` 走 `flavors::zero`，否则走 `flavors::array`。这正是 u2-l1 讲过的「按容量选 flavor」分流。

生成器函数靠 `send` 的返回值驱动循环：

```rust
fn fibonacci(sender: Sender<u64>) {
    let (mut x, mut y) = (0, 1);
    while sender.send(x).is_ok() {
        let tmp = x;
        x = y;
        y += tmp;
    }
}
```

参见 [examples/fibonacci.rs:8-15](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/examples/fibonacci.rs#L8-L15)：`sender.send(x)` 在会合通道上**阻塞到有人接收**才返回 `Ok(())`；一旦接收侧全部 drop，`send` 返回 `Err(SendError(_))`，`is_ok()` 为假，循环结束。这是把「通道断开」当作「自然终止信号」的惯用法。

#### 4.1.4 代码实践

1. **实践目标**：亲手体会会合通道的「发收同步」与「背压」。
2. **操作步骤**：
   - 在仓库根目录运行 `cargo run --example fibonacci`。
   - 观察输出：应是 `0 1 1 2 3 5 8 ...`（前 20 个斐波那契数）。
   - 把 `take(20)` 改成 `take(5)`，重新运行，确认只打印 5 个数。
3. **需要观察的现象**：程序正常退出，没有死锁、没有报错；生成器线程没有把 20 个之后的数继续算出来（背压生效）。
4. **预期结果**：`take(N)` 决定了最终打印的行数，多余的发送被永久阻塞、随进程退出而丢弃。
5. **进阶（待本地验证）**：把 `bounded(0)` 改成 `unbounded()`，再运行。预期会**先把整个（无限的）序列尽可能灌进无界队列**，主线程只取 20 个，但生成器线程不会很快停下来——体会「无界 = 无背压」的差别。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `thread::spawn(|| fibonacci(s));` 改成在主线程里直接调用 `fibonacci(s)`（不另起线程），会发生什么？

**参考答案**：会**死锁**。会合通道要求发送与接收同时在场；主线程既要去 `send`（在 `fibonacci` 里阻塞），又要去 `recv`（在 `for` 循环里），但只有一个线程，`send` 永远等不到 `recv`，整个程序卡死。这正是 u1-l2 强调的「零容量通道单线程直接 send 会死锁」。

**练习 2**：示例里 `r.iter().take(20)` 的 `iter()` 在底层调用的是什么？

**参考答案**：`iter()` 返回的 `Iter` 在 `next()` 里就是 `self.receiver.recv().ok()`（阻塞接收，出错时返回 `None`）。`take(20)` 限制只取 20 个 `Some`，之后即使通道还有数据也提前结束迭代。详见 u1-l4 关于迭代器的讲解。

---

### 4.2 示例精读：matching —— select! 同时收发的会合式配对

#### 4.2.1 概念说明

`matching.rs` 演示 crossbeam-channel 一个 `std::sync::mpsc` 做不到的能力：**用 `select!` 在同一个通道上同时尝试「收」和「发」**，哪个先就绪就执行哪个。

场景来自一段经典的 Go 并发教学程序（注释里贴了原 Go 代码与版权信息）：5 个人，每个人要么把自己的名字 `send` 进通道等别人收，要么从通道 `recv` 别人的名字。两两配对，最后可能剩一个人没人收他的消息。这个例子用 `select! { recv(r) => ...; send(s, name) => ... }` 写得极其自然——每个线程同时挂着「我发」和「我收」两个操作，先就绪的那个胜出。

#### 4.2.2 核心流程

```
people = [Anna, Bob, Cody, Dave, Eva]   (5 个人)
channel = bounded(1)   ← 关键：容量 1，留一个"未配对发送"的位

每个线程的 seek(name) 内部：
  select! {
      recv(r) -> peer => 打印 "X 收到了 Y 的消息"
      send(s, name) => 等待有人收走我的名字
  }
  ← 两个分支哪个先就绪执行哪个；另一个分支不执行

最终：5 个人两两配对，奇数剩 1 人 → 他的 send 残留在通道里
主循环结束后 try_recv 检查：若有残留 → 打印 "没人收 X 的消息"
```

要点：

- **`bounded(1)` 是刻意选的**：容量 1 允许「一个未配对的发送」先成功落地，让配对更灵活；如果用 `bounded(0)`（会合），5 个奇数线程很难都配对成功。
- **`select!` 的随机性带来公平**：当某个线程发现通道里已经有一个待收的名字（`recv` 就绪），同时自己又想 `send`（需要空位），`select!` 会**随机**选一个执行。这是 u2-l9 讲过的「就绪随机性」。
- **跨线程收发靠 `thread::scope`**：5 个线程各自持有 `s.clone()` 和 `r.clone()`，作用域结束前所有线程归队，主线程再检查残留。

#### 4.2.3 源码精读

核心是这段 `seek` 闭包里的 `select!`：

```rust
use crossbeam_channel::{bounded, select};
use crossbeam_utils::thread;

let people = vec!["Anna", "Bob", "Cody", "Dave", "Eva"];
let (s, r) = bounded(1); // Make room for one unmatched send.

let seek = |name, s, r| {
    select! {
        recv(r) -> peer => println!("{} received a message from {}.", name, peer.unwrap()),
        send(s, name) -> _ => {}, // Wait for someone to receive my message.
    }
};
```

参见 [examples/matching.rs:45-58](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/examples/matching.rs#L45-L58)：`select!` 同时挂了 `recv(r)` 与 `send(s, name)` 两个分支。这个 `select!` 在编译期会被 `crossbeam_channel_internal!` 宏展开成对 `internal::select` 的调用（无 `default` 分支 → 阻塞式 `Timeout::Never`），细节见 u3-l3。

线程分发用 `crossbeam_utils::thread::scope`：

```rust
thread::scope(|scope| {
    for name in people {
        let (s, r) = (s.clone(), r.clone());
        scope.spawn(move |_| seek(name, s, r));
    }
})
.unwrap();
```

参见 [examples/matching.rs:60-66](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/examples/matching.rs#L60-L66)：每个线程拿到的是**克隆出来的 `Sender`/`Receiver`**，它们共享同一个底层通道（克隆只把计数 +1，不复制消息流，见 u1-l4、u2-l2）。作用域保证所有线程在 `scope` 返回前 join 完毕，因此后面可以安全地 `try_recv`。

收尾检查残留的「未配对发送」：

```rust
if let Ok(name) = r.try_recv() {
    println!("No one received {}’s message.", name);
}
```

参见 [examples/matching.rs:69-71](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/examples/matching.rs#L69-L71)：5 个人是奇数，至少有一个人的 `send` 不会被 `recv` 配对；由于通道容量为 1，这个残留消息会留在通道里，主线程用非阻塞的 `try_recv` 把它捞出来。

#### 4.2.4 代码实践

1. **实践目标**：理解 `select!` 在「同通道收发」上的随机配对行为。
2. **操作步骤**：
   - 运行 `cargo run --example matching`，多运行几次。
   - 观察输出顺序：每次运行「谁配谁」「最后谁没被收」都可能不同（受 `select!` 随机性与线程调度影响）。
3. **需要观察的现象**：4 个人两两配对成功（4 行 "received a message from"），最后 1 行 "No one received X's message."。
4. **预期结果**：因为 5 是奇数，必然恰好剩 1 条未配对消息；配对组合每次运行可能不同。
5. **进阶（待本地验证）**：把 `bounded(1)` 改成 `bounded(0)`（会合通道），重新多次运行。预期会出现**死锁或卡住**的可能——因为会合通道不允许任何「未配对的发送」落地，奇数个线程很难全部配对成功，作用域无法结束。体会容量选择对配对算法的决定性影响。

#### 4.2.5 小练习与答案

**练习 1**：为什么这个例子必须用 `select!` 而不能写成「先 `try_recv`，没有再 `send`」的顺序逻辑？

**参考答案**：顺序逻辑会**改变语义**。如果线程 A 先 `try_recv` 失败（通道空）再去 `send`，而同时线程 B 也先 `try_recv` 失败再去 `send`，两个想发的人谁也不收，就会卡住或错失配对。`select!` 让「收」和「发」**同时挂起、原子地选一个就绪的**，这正是 Go 原版 `select { case <-match; case match <- name }` 的语义，也只有 mpmc 通道（`Receiver` 可克隆）能让多个线程同时 `recv` 同一个通道。

**练习 2**：把 `select!` 换成 `select_biased!`，行为会有什么变化？

**参考答案**：`select_biased!` 取消了随机性，改为**按分支书写顺序**优先选靠前者（见 u2-l9、u3-l3）。这里意味着每个线程都会**优先尝试 `recv`**，只有 `recv` 不就绪时才 `send`。这会让「倾向于收」的行为更稳定、可预测，但失去了无偏公平性。两个宏背后是同一个 `internal::select`，差别仅在 `_IS_BIASED` 开关是否触发 `shuffle`。

---

### 4.3 示例精读：stopwatch —— tick 心跳 + 信号驱动的 select! 主循环

#### 4.3.1 概念说明

`stopwatch.rs` 是最接近「真实服务」结构的示例：一个常驻主循环，每秒打印一次已运行时间，按 Ctrl+C 优雅退出。它把**数据通道**（信号通道）和**时间通道**（`tick`）组合在一个 `select!` 里，展示了 crossbeam-channel 作为「事件循环编排工具」的用法。

这个例子还展示了一个常见的工程模式：**把外部的「非通道」事件源（Unix 信号）桥接成一个通道**。`signal_hook` 在后台线程里把 SIGINT 转发进一个 `bounded(100)` 通道，主循环就能用统一的 `select!` 同时处理「定时心跳」和「Ctrl+C」。

> 注意：该示例在 Windows 上会被 `#[cfg(windows)]` 短路成一句「不支持」的打印（因为 `signal_hook::iterator` 在 Windows 上不工作），真实逻辑在 `#[cfg(not(windows))]` 的 `main` 里。

#### 4.3.2 核心流程

```
后台信号线程                      主循环
  signals.forever()               loop {
    收到 SIGINT                     select! {
    → s.send(())                       recv(update) => 每秒打印 elapsed    ← tick 通道
                                       recv(ctrl_c) => 打印 Goodbye; break ← 信号通道
                                     }
                                   }
```

两条通道：

- `update = tick(Duration::from_secs(1))`：**时间通道**，每秒就绪一次，主循环醒来打印耗时。
- `ctrl_c = bounded(100)`：**数据通道**，后台线程把 SIGINT 桥接进来，收到即退出。

`select!` 在两条只读 `Receiver` 之间等待，谁先就绪执行谁——典型的「事件循环」结构。

#### 4.3.3 源码精读

用 `tick` 创建周期心跳，用 `bounded(100)` 做信号缓冲：

```rust
use crossbeam_channel::{Receiver, bounded, select, tick};

let start = Instant::now();
let update = tick(Duration::from_secs(1));
let ctrl_c = sigint_notifier().unwrap();
```

参见 [examples/stopwatch.rs:15](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/examples/stopwatch.rs#L15) 的导入，与 [examples/stopwatch.rs:39-41](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/examples/stopwatch.rs#L39-L41) 的创建。`tick` 内部走的是 **tick flavor**：

```rust
pub fn tick(duration: Duration) -> Receiver<Instant> {
    match Instant::now().checked_add(duration) {
        Some(delivery_time) => Receiver {
            flavor: ReceiverFlavor::Tick(Arc::new(flavors::tick::Channel::new(
                delivery_time, duration,
            ))),
        },
        None => never(),
    }
}
```

参见 [src/channel.rs:335-345](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L335-L345)：`tick` 用 `checked_add` 算出第一个投递时刻，构造 `Tick` flavor；若 `duration` 大到 `Instant` 溢出，就退化成 `never()`。`tick` 是只读的 `Receiver<Instant>`，没有 `Sender`——消息「按需生成」，这正是 u2-l8 讲过的特殊通道设计。

信号桥接函数把外部事件源接成通道：

```rust
fn sigint_notifier() -> io::Result<Receiver<()>> {
    let (s, r) = bounded(100);
    let mut signals = Signals::new([SIGINT])?;
    thread::spawn(move || {
        for _ in signals.forever() {
            if s.send(()).is_err() {
                break;
            }
        }
    });
    Ok(r)
}
```

参见 [examples/stopwatch.rs:19-32](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/examples/stopwatch.rs#L19-L32)：选 `bounded(100)` 而非 `bounded(0)` 是为了**吸收短时间内多次 Ctrl+C 的信号突发**，避免后台线程在主循环还没消费时被阻塞；`send` 出错（接收侧已 drop）就退出循环，是「通道断开 = 收尾」的惯用法。

主循环用 `select!` 编排两条通道：

```rust
loop {
    select! {
        recv(update) -> _ => {
            show(start.elapsed());
        }
        recv(ctrl_c) -> _ => {
            println!();
            println!("Goodbye!");
            show(start.elapsed());
            break;
        }
    }
}
```

参见 [examples/stopwatch.rs:43-55](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/examples/stopwatch.rs#L43-L55)：每秒 `update` 就绪一次触发打印；Ctrl+C 时 `ctrl_c` 就绪触发退出。注意两个分支都是 `recv`——`select!` 完全可以只含 `recv` 分支，这里是「多源事件循环」的标准写法。

#### 4.3.4 代码实践

1. **实践目标**：跑通一个「定时心跳 + 外部信号退出」的真实事件循环。
2. **操作步骤**（需 Linux/macOS）：
   - 运行 `cargo run --example stopwatch`。
   - 观察每秒打印一行 `Elapsed: N.NNN sec`。
   - 按 Ctrl+C，观察打印 `Goodbye!` 后程序退出。
3. **需要观察的现象**：心跳准时每秒一次；Ctrl+C 后立即退出，不会错过信号。
4. **预期结果**：心跳与退出都按预期工作。
5. **进阶（待本地验证）**：在主循环里再加一个 `recv(after(Duration::from_secs(5))) -> _ => println!("5 秒到")` 分支，观察第 5 秒会额外打印一行——体会 `after`（一次性定时）与 `tick`（周期）在 `select!` 里的差别。

#### 4.3.5 小练习与答案

**练习 1**：如果把 `tick(Duration::from_secs(1))` 换成 `at(start + Duration::from_secs(1))`，主循环行为会怎样？

**参考答案**：`at` 是**一次性**定时通道（容量 1，只投递一次，见 u2-l8）。第一条消息会在第 1 秒到来，但**之后再也不投递**。于是第 2 秒起 `recv(update)` 分支永远不就绪，主循环只能靠 Ctrl+C 退出——心跳实际上只响了一次。`tick` 才是周期性心跳的正确选择。

**练习 2**：信号通道为什么用 `bounded(100)` 而不是 `unbounded()`？

**参考答案**：信号量是有上限的（人不可能在一秒内按几百次 Ctrl+C），`bounded(100)` 既足够吸收合理的突发，又提供了**背压上限**，防止无界队列在异常情况下无限堆积。这是「数据通道选 bounded 而非 unbounded」的一个典型工程判断：只要能估算出合理的容量上界，就优先 bounded。

---

### 4.4 架构取舍：如何选 flavor、选 select 方式、无锁 vs 锁、no_std

#### 4.4.1 概念说明

读完三个示例，我们应该能提炼出一张「决策表」。crossbeam-channel 不是「一个通道」，而是「一套统一 API 覆盖六种实现」，选型的本质是**为场景匹配实现策略**。这一节把前 24 篇讲义里的散点结论收拢成可操作的取舍指南，并讨论二次开发时「改哪一层」的问题。

#### 4.4.2 核心流程：flavor 选型决策表

| 场景需求 | 推荐 flavor | 构造函数 | 关键特性（出处） |
| --- | --- | --- | --- |
| 生产者永不阻塞、能多快发多快；接受内存可能涨 | **list**（无界链表） | `unbounded()` | 发送方恒就绪、分块链表惰性回收（u2-l6） |
| 固定容量、想要背压、高吞吐有界队列 | **array**（环形数组） | `bounded(cap>0)` | 无锁 CAS 环形队列、CachePadded 防伪共享（u2-l5） |
| 严格一对一握手、发收必须同步会合 | **zero**（会合） | `bounded(0)` | 零缓冲、packet 配对、每次进锁（u2-l7） |
| 一次性超时 / 某时刻触发 | **at** | `at(Instant)` / `after(Duration)` | AtomicBool 保证恰好投递一次（u2-l8） |
| 周期性心跳 | **tick** | `tick(Duration)` | AtomicCell CAS 推进投递时刻（u2-l8） |
| select! 里「可选」分支占位（条件性超时） | **never** | `never()` | ZST、try_select 恒 false、完全透明（u2-l8） |

选型口诀：**主数据流看容量（array/list/zero），附属定时看频率（at 一次 / tick 周期），可选分支用 never 占位**。

#### 4.4.3 源码精读：外壳层「按 flavor 分发」是选型的落点

不管你选哪个 flavor，对外都是同一套 `Sender`/`Receiver` API。这种「统一壳 + 多实现」靠的是 `SenderFlavor` / `ReceiverFlavor` 两个枚举：

```rust
enum SenderFlavor<T> {
    Array(counter::Sender<flavors::array::Channel<T>>),
    List(counter::Sender<flavors::list::Channel<T>>),
    Zero(counter::Sender<flavors::zero::Channel<T>>),
}
```

参见 [src/channel.rs:371-380](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L371-L380)。`ReceiverFlavor` 比 `SenderFlavor` 多三个只读变体 `At`/`Tick`/`Never`（因为时间通道没有发送方），参见 [src/channel.rs:729-747](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L729-L747)。

每个公共方法都是「match flavor 转发 + 错误归一化」，以 `send` 为例：

```rust
pub fn send(&self, msg: T) -> Result<(), SendError<T>> {
    match &self.flavor {
        SenderFlavor::Array(chan) => chan.send(msg, None),
        SenderFlavor::List(chan) => chan.send(msg, None),
        SenderFlavor::Zero(chan) => chan.send(msg, None),
    }
    .map_err(|err| match err {
        SendTimeoutError::Disconnected(msg) => SendError(msg),
        SendTimeoutError::Timeout(_) => unreachable!(),
    })
}
```

参见 [src/channel.rs:446-456](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L446-L456)：底层只有「带截止时间」一种原语 `chan.send(msg, None)`（`None` 表示永不超时），再用 `map_err` 把统一的 `SendTimeoutError` 归一化成对外的 `SendError`，其中 `Timeout` 分支用 `unreachable!()` 兜底（永不超时不可能超时）。这是 u1-l3 讲过的贯穿母题。**这层 match 就是选型决策在源码里的最终落点**——你选的 flavor 决定了 `chan` 具体是 array/list/zero 中的哪一个，从而走完全不同的内部实现。

#### 4.4.4 无锁 vs 锁的取舍

crossbeam-channel 在「并发策略」上做了**分而治之**的取舍，这是它性能与正确性的核心来源：

| flavor | 并发策略 | 取舍理由 |
| --- | --- | --- |
| **array** | 无锁（CAS 环形队列 + stamp 版本号） | 固定容量、槽位可寻址，适合纯 CAS；高吞吐，但实现复杂、内存序要求严苛（u2-l5、u3-l4） |
| **list** | 发送方无锁、接收方 CAS | 发送方永不阻塞、只追加新块；适合「生产不能停」的场景（u2-l6） |
| **zero** | `Mutex<Inner>` 锁保护 | 零容量、不存消息，配对逻辑（packet 投递）天然临界区，用锁最简单清晰（u2-l7） |

直觉是：**能用无锁 CAS 干净解决的（固定结构、可寻址槽位），就用无锁换取吞吐；逻辑本身是「人对人配对」这种临界区的，就用锁换取简单与正确性**。zero 每次 `send`/`recv` 都要进锁，这也是 u3-l7 基准测试里 `bounded0_*`（会合）柱子比 `bounded_*`/`unbounded_*` 长的原因——它不是为吞吐设计的，而是为「强同步会合」设计的。

此外，阻塞唤醒层（`context.rs` / `waker.rs`）也做了取舍：`SyncWaker` 用 `Mutex<Waker> + AtomicBool(is_empty)` 的 double-checked 锁，在「没有阻塞者」时一次原子 load 即返回（快速路径），只有真有阻塞者才进锁——这是「锁保护 + 无锁快速路径」的折中（u2-l4）。

#### 4.4.5 代码实践（源码阅读型）：画出三个示例的「flavor + select 分发」对照表

1. **实践目标**：把三个示例里用到的 flavor、API、`select!` 分支类型与本章决策表对上号。
2. **操作步骤**：
   - 填下面这张表（答案见文末）：

     | 示例 | 主数据通道 flavor | 时间通道 | select! 用法 | 关键 API |
     | --- | --- | --- | --- | --- |
     | fibonacci | ? | 无 | 无（直接 iter） | ? |
     | matching | ? | 无 | ? | clone + scope |
     | stopwatch | ?（信号通道） | ? | 两路 recv | ? |

   - 对每个示例，在 [src/channel.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs) 里找到对应构造函数，确认它 match 到了哪个 `SenderFlavor`/`ReceiverFlavor` 变体。
3. **需要观察的现象**：三个示例恰好覆盖了 array、zero、tick 三种 flavor，且 stopwatch 额外示范了「外部事件源桥接成通道」的工程模式。
4. **预期结果**：能说清「为什么 fibonacci 用 zero、matching 用 array(cap=1)、stopwatch 用 tick + bounded(100)」。
5. **参考答案**：fibonacci = `bounded(0)` → **zero**，用 `iter()`/`send`；matching = `bounded(1)` → **array**，用 `select!{recv; send}`（同通道收发）；stopwatch = 信号通道 `bounded(100)` → **array** + `tick(1s)` → **tick**，用 `select!{recv(update); recv(ctrl_c)}`。

#### 4.4.6 二次开发：改哪一层、不该动哪一层

如果要把 crossbeam-channel 拿来改造（加监控、换队列、做 no_std 移植），记住分层边界：

- **想加业务逻辑（日志、统计、限流）**：在**你自己的代码**或包一层 wrapper 里做，**不要改源码**。源码内部大量 `unsafe` 与内存序不变量（u3-l4），随手加日志可能破坏 happens-before。
- **想换队列结构（例如把 array 换成另一种有界队列）**：实现一个新 flavor，放在 `src/flavors/` 下，实现 `SelectHandle` 的七个方法（u3-l2），然后在 `SenderFlavor`/`ReceiverFlavor` 加一个变体、在 `channel.rs` 的每个 match 里加一支。**这是扩展点的设计意图**——七方法 trait 是算法与实现的唯一接口。
- **想做 `no_std` 移植**：当前 Cargo.toml 明确写「禁用 std 尚不支持」（u3-l6），因为阻塞唤醒（`context.rs`/`waker.rs`）依赖线程 park/unpark，这是 std 独有能力。移植的真正难点不在数据结构，而在「阻塞语义」如何替换（例如改成 async 或自旋）。
- **想理解 `select!` 宏的边界**：宏展开后调用的是 `internal` 模块里 `#[doc(hidden)]` 的 `select`/`try_select`/`select_timeout`（u3-l3、u3-l6），跨 crate 通过 `$crate::internal::...` 回引。自定义宏若要复用这套调度，可走同一路径。

## 5. 综合实践

**任务：融合三个示例，写一个「多 worker 任务分发 + 超时 + 周期心跳」的小程序。**

借鉴 fibonacci 的「生产者-消费者 + 会合/有界」、matching 的「克隆共享 + 多线程」、stopwatch 的「tick 心跳 + select! 主循环」，构建如下结构：

```
主线程（调度者）
  ├─ 创建 任务队列  = bounded(8)        ← array flavor，背压，防止任务堆积
  ├─ 创建 结果队列  = unbounded()      ← list flavor，结果不丢、生产者(worker)不阻塞
  ├─ 创建 心跳      = tick(1s)         ← tick flavor，周期打印进度
  ├─ spawn N 个 worker：每个持有 task_rx.clone() 和 result_tx.clone()
  │     loop { select! {
  │         recv(task_rx) -> task => 算 result -> result_tx.send(...)
  │         // worker 也可以加 default(超时) 让空闲 worker 自动退出
  │     }}
  ├─ 主线程投递 M 个任务到 task_tx
  ├─ drop(task_tx)                       ← 发送侧断开：worker recv 到 Disconnected 后退出
  └─ 主循环 select! {
         recv(result_rx) -> 累计结果
         recv(heartbeat) -> 打印进度
         recv(after(5s)) -> 打印"5秒检查点"   ← 一次性超时/检查点（at flavor）
     }
     直到收齐 M 个结果
```

**设计要点（对照本章决策表）**：

1. **任务队列用 `bounded(8)`**：给生产者（主线程）背压，避免一次性灌入海量任务占满内存；容量 8 让 worker 始终有活干。→ array flavor。
2. **结果队列用 `unbounded()`**：worker 算出结果必须立刻交出去、不能因为主线程来不及收而阻塞 worker；结果量可控（=M），无界风险可接受。→ list flavor。
3. **心跳用 `tick(1s)`**：周期性打印「已收 N/M 个结果」，借鉴 stopwatch。→ tick flavor。
4. **检查点用 `after(5s)`**：5 秒时额外打印一次诊断，借鉴 stopwatch 的进阶实践。→ at flavor。
5. **优雅退出靠 `drop(task_tx)`**：发送侧全部 drop 后，worker 的 `recv` 返回 `Err(RecvError)`，自然退出循环——这是 fibonacci 用过的「断开即停止」惯用法（u1-l4）。
6. **主循环用 `select!`**：分支数编译期已知（结果 / 心跳 / 检查点），用宏即可；若 worker 数量运行时才决定，注册 worker 结果用 `Select` 动态 API（u2-l10）。

**验证标准**：

- 程序打印 M 个结果，顺序不保证（mpmc）但数量正确。
- 每秒打印一次心跳进度，第 5 秒多打印一次检查点。
- `drop(task_tx)` 后所有 worker 自动退出，主线程收齐后程序正常结束，无死锁、无悬挂线程。
- 把任务队列容量从 8 改成 0（会合），观察主线程投递任务时会**阻塞到有 worker 来取**——体会会合通道作为「强同步任务派发」的效果。

> 这是一个完整的「读 + 改 + 验证」闭环：读三个示例学模式，按决策表选 flavor，用 `select!` 编排，最后用断开语义收尾。能独立完成它，说明你已把本手册的核心知识串通了。

## 6. 本讲小结

- crossbeam-channel 是「一套统一 API + 六种可替换 flavor」，选型的第一步是区分**数据通道**（array/list/zero，按容量策略选）与**时间通道**（at/tick/never，按定时频率选），附属分支用 never 占位。
- fibonacci 用 `bounded(0)` 会合通道实现了「背压即终止」的生产者-消费者；matching 用 `bounded(1)` + `select!` 实现了同通道收发的随机配对；stopwatch 用 `tick` + 信号桥接通道 + `select!` 实现了定时心跳的事件循环——三个示例恰好覆盖 zero/array/tick 三种 flavor 与三种典型用法。
- 公共 API 的「match flavor 转发 + 错误归一化」（[src/channel.rs:446-456](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L446-L456)）是选型决策在源码里的最终落点：你选的 flavor 决定了 `chan` 是哪一个变体，从而走完全不同的内部实现。
- 并发策略是**分而治之**：array/list 用无锁 CAS 换吞吐，zero 用锁换简单正确，`SyncWaker` 用「锁 + 无锁快速路径」折中——选 flavor 不仅是选容量，也是选并发策略。
- 二次开发要守分层边界：业务逻辑包 wrapper 不改源码；换队列结构就实现 `SelectHandle` 七方法加新 flavor 变体；`no_std` 移植的真正难点在阻塞语义（park/unpark）而非数据结构。
- 综合实践把「bounded 任务队列 + unbounded 结果队列 + tick 心跳 + after 检查点 + select! 主循环 + drop 优雅退出」串成一个完整的工程闭环。

## 7. 下一步学习建议

到这里，整本 crossbeam-channel 学习手册（u1→u3，共 25 篇）已读完。建议从以下方向继续：

- **横向对比**：把 crossbeam-channel 与 `std::sync::mpsc`、`flume`、`tokio::sync::mpsc`（async）对照阅读。仓库 `benchmarks/` 目录（u3-l7）已提供与 Go/flume/futures-channel/std 的对比脚本，可运行 `benchmarks/run.sh` 获得一手性能数据。
- **深入 async 化**：crossbeam-channel 是同步（阻塞）通道。若要做 async 版本，重点研究「如何用同一个 flavor 数据结构，把 park/unpark 换成唤醒一个 Future」——这正是 `async-channel` 等库的课题。
- **阅读姊妹 crate**：本仓库还有 `crossbeam-queue`（无锁队列）、`crossbeam-deque`（工作窃取队列）、`crossbeam-epoch`（基于 epoch 的内存回收）、`crossbeam-utils`（`CachePadded`/`Backoff`/`AtomicCell` 等并发原语）。它们共享同一套并发正确性方法论（u3-l4），读起来会非常亲切。
- **回顾源码地图**：若日后需要回查某个机制，按「外壳 `channel.rs` → 计数 `counter.rs` → 阻塞唤醒 `context.rs`/`waker.rs` → 各 flavor `flavors/*` → 调度 `select.rs` → 宏 `select_macro.rs`」的顺序定位即可，这张地图在 u1-l1 与 u2-l1 已建立。
- **动手改造**：挑一个 flavor（建议从 list 开始，结构相对直白），尝试为其加一个「发送计数」统计字段并暴露查询接口，完整走一遍「改 flavor 实现 → 加外壳方法 → 加测试」的二次开发流程，验证你对分层边界的理解。
