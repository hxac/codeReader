# channel 总览与基本使用

## 1. 本讲目标

本讲是「crossbeam-channel：消息传递」单元的第一篇。我们不进入任何内部算法，只解决一个问题：**作为使用者，crossbeam 的通道到底怎么用、有哪几种、什么时候阻塞、出错时返回什么**。

学完后你应该能够：

- 说清 `unbounded()`、`bounded(cap)`、`bounded(0)` 三类通道在语义上的本质区别；
- 解释 `send` / `recv` 的「三种阻塞风味」（非阻塞、阻塞、带超时）以及各自返回的错误类型；
- 用 `Receiver` 作为迭代器消费消息，并区分 `iter()`（阻塞）与 `try_iter()`（非阻塞）；
- 独立写出生产者-消费者小程序，并能依据「是否需要背压」选择有界还是无界通道。

## 2. 前置知识

本讲默认你已经读过 [u1-l3 主 crate 重导出门面](./u1-l3-reexport-facade.md)，知道两件事：

1. **门面路径**：主 crate `crossbeam` 以 facade 模式重导出了 `crossbeam_channel`。所以你可以用 `crossbeam::channel::unbounded()`，也可以在依赖 `crossbeam_channel = "0.5"` 时直接用 `crossbeam_channel::unbounded()`。两条路径指向**同一个实现**，本讲为了和源码一致，统一写成 `crossbeam_channel::...`。

2. **channel 只在 `std` 特性下可用**。回顾 `lib.rs` 里 `mod channel` 被 `#[cfg(feature = "std")]` 门控，因此通道必须开启 `std`（主 crate 的 default 特性已包含）。

下面用通俗语言补三个本讲要用到的概念：

- **通道（channel）**：线程间传递消息的「管道」。一端塞东西进去，另一端取出来，所有权（`T`）随消息一起流转。这是「消息传递（message passing）」并发模型的核心，区别于「共享内存 + 锁」。
- **生产者 / 消费者**：往通道里 `send` 的是生产者，从通道里 `recv` 的是消费者。多生产者多消费者（MPMC）即多个线程同时发、多个线程同时收。
- **阻塞 / 非阻塞**：阻塞操作会让当前线程「睡着」等条件满足再醒来；非阻塞操作条件不满足就**立刻**返回一个错误。

一个关键背景（u1-l1 已提及）：标准库的 `std::sync::mpsc` 是 **MPSC**（多生产者、单消费者），而 crossbeam-channel 是 **MPMC**——`Receiver` 可以 `clone()` 后分发给多个线程同时收。这是它相对标准库的第一个优势。crate 文档开头就点明了定位：

> Multi-producer multi-consumer channels for message passing. This crate is an alternative to `std::sync::mpsc` with more features and better performance.

详见 [crossbeam-channel/src/lib.rs:1-3](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs#L1-L3)。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 | 本讲怎么用 |
| --- | --- | --- |
| `crossbeam-channel/src/lib.rs` | crate 顶层文档（通道用法教程）+ 对外重导出 | 看三类通道的官方定义与公共 API 清单 |
| `crossbeam-channel/src/channel.rs` | 通道接口：`unbounded` / `bounded` 构造函数、`Sender` / `Receiver` 及其所有公共方法、迭代器、flavor 派发 | 本讲的主战场，逐方法精读 |
| `crossbeam-channel/src/err.rs` | 所有错误类型 | 建立「操作 × 错误」对照表 |
| `crossbeam-channel/examples/fibonacci.rs` | 可运行示例：用 `bounded(0)` 做斐波那契生成器 | 作为 `bounded(0)` 会合语义的活样本 |

> 说明：`channel.rs` 里出现的 `SenderFlavor::Array / List / Zero` 以及 `counter::new(...)`、`flavors::*::Channel` 属于内部架构（flavor 派发、引用计数），那是 [u3-l2](./u3-l2-counter-and-errors.md) 与 [u3-l3](./u3-l3-flavors-architecture.md) 的主题。本讲只在「它决定了哪些 API 行为」的层面提及，不展开实现。

## 4. 核心概念与源码讲解

### 4.1 三种基本通道：unbounded / bounded / bounded(0)

#### 4.1.1 概念说明

crossbeam-channel 只用**两个构造函数**就能造出三类语义截然不同的通道：

| 构造函数 | 容量 | 内部 flavor | 语义 |
| --- | --- | --- | --- |
| `unbounded()` | 无限（可无限增长） | `List`（链表） | 发送**永不阻塞**，有多少塞多少 |
| `bounded(cap)`，`cap > 0` | `cap` | `Array`（定长环形缓冲） | 缓冲满时 `send` 阻塞，形成**背压（backpressure）** |
| `bounded(0)` | 0 | `Zero` | 无缓冲，`send` 与 `recv` 必须**同时在场**才能配对交接（rendezvous，会合） |

理解这三类的关键是一个直觉：

- **无界**像一个**无底的桶**，生产者随倒随有，但若消费者跟不上，内存会被无限堆积——适合「生产速度可控 / 消费者迟早能跟上」的场景。
- **有界**像一个**固定大小的桶**，满了生产者就得「等」——这就是背压，它能强迫生产者降速，保护系统不被压垮。
- **零容量**像一个**必须手递手**的交接点，连一个消息都存不下。发送者把消息递出去的瞬间，必须有一个接收者伸手接住，否则发送者就卡住等。它天然实现「严格同步 / 一步一握手」。

#### 4.1.2 核心流程

三类通道对 `send` 的「阻塞条件」可以用下面这张状态表概括：

| 通道类型 | `send` 何时立即成功 | `send` 何时阻塞 |
| --- | --- | --- |
| `unbounded()` | 几乎总是（除非断开） | 几乎不阻塞 |
| `bounded(cap>0)` | 当前消息数 `< cap` | 消息数 `== cap`（桶满） |
| `bounded(0)` | 恰好此刻有一个 `recv` 在等着接 | 没有接收者在场 |

对 `recv` 则对称：消息可得就成功；通道空且未断开就阻塞；通道空且已断开（所有发送者被 drop）就立刻返回错误。

一个尤其值得记住的「反直觉」细节：**零容量通道永远既是空的、又是满的**——它存不下任何消息所以 `is_empty()` 恒真，它没有空位让发送立即成功所以 `is_full()` 也恒真。这一点直接写在源码注释里（见 4.3.3）。

#### 4.1.3 源码精读

先看顶层文档对三类通道的官方描述，其中零容量这一段最关键：

[crossbeam-channel/src/lib.rs:64-79](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs#L64-L79) —— 这段说明：零容量通道不能存任何消息，`send` 和 `recv` 必须「同时出现」才能配对并把消息交接过去（"send and receive operations must appear at the same time in order to pair up and pass the message over"）。

再看构造函数本身。`unbounded()` 把底层实现选成 `List` flavor：

[crossbeam-channel/src/channel.rs:50-59](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L50-L59) —— `unbounded` 用 `flavors::list::Channel::new()` 构造，包进 `SenderFlavor::List` / `ReceiverFlavor::List`。

而 `bounded()` 用一个 `if cap == 0` 分支把零容量和正容量分流到不同 flavor：

[crossbeam-channel/src/channel.rs:113-133](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L113-L133) —— `cap == 0` 走 `flavors::zero::Channel`；否则走 `flavors::array::Channel::with_capacity(cap)`。

注意这三个构造函数本身**不做任何阻塞判断**——它们只是「选好底层 flavor 并返回句柄」。阻塞与否完全由后续 `send` / `recv` 在对应 flavor 上的实现决定（详见后续 u3-l4 ~ u3-l6）。但即便不看实现，仅从「`bounded(0)` 单独用一个 `zero` flavor」这一点，就能体会到零容量在语义上是独立的一类。

`Sender` / `Receiver` 只是把 flavor 藏在私有字段里的薄封装：

[crossbeam-channel/src/channel.rs:366-380](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L366-L380) —— `Sender<T>` 内部就是一个 `SenderFlavor<T>` 枚举（`Array` / `List` / `Zero` 三选一）。这意味着每个公共方法都只是一层 `match` 派发。

#### 4.1.4 代码实践

**实践目标**：用身体感受三类通道在 `send` 上的阻塞差异。

**操作步骤**（新建一个临时 crate 或在 `examples/` 下加文件均可）：

```rust
// 示例代码：手动验证三类通道的阻塞行为
use std::thread;
use crossbeam_channel::{bounded, unbounded};

fn main() {
    // 1) unbounded：连发 1000 条不会阻塞
    let (s, r) = unbounded();
    for i in 0..1000 { s.send(i).unwrap(); }
    println!("unbounded 发完 1000 条，没卡");

    // 2) bounded(2)：第 3 条会阻塞，需要先消费
    let (s2, r2) = bounded(2);
    s2.send(1).unwrap();
    s2.send(2).unwrap();
    // s2.send(3).unwrap(); // 取消注释会死锁——单线程没人收
    r2.recv().unwrap();      // 腾出一个位置
    s2.send(3).unwrap();     // 现在能成功了
    println!("bounded(2) 通过背压协调成功");

    // 3) bounded(0)：send 必须等另一个线程在 recv
    let (s3, r3) = bounded(0);
    thread::spawn(move || { s3.send("hi").unwrap(); });
    assert_eq!(r3.recv(), Ok("hi"));
    println!("bounded(0) 会合成功");
}
```

**需要观察的现象**：把 `s2.send(3)` 那行取消注释，程序会**卡死**（单线程里没人 `recv`，桶满死锁），证明有界通道的背压；`bounded(0)` 那段若不 spawn 线程直接 `s3.send` 也会卡死，证明会合语义。

**预期结果**：注释态正常打印三行；取消 `s2.send(3)` 注释则挂起不退出。

> 如果在仓库内运行，最简方式是 `cargo run -p crossbeam-channel --example <名字>`；若你把上面的代码放进自己的小 crate，则直接 `cargo run`。本讲不预设具体运行结果数字，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：`bounded(0)` 通道，在没有接收者的情况下调用 `try_send(1)`，会返回什么？为什么不是「阻塞」？

**参考答案**：返回 `Err(TrySendError::Full(1))`（对零容量而言，"Full" 的含义是「此刻没有接收者可配对」）。因为 `try_send` 是**非阻塞**的——条件不满足就立刻报错，绝不等待。

**练习 2**：若你想限制一个高吞吐生产者不要把内存撑爆，应该选 `unbounded` 还是 `bounded`？容量设多少？

**参考答案**：选 `bounded(cap)`。容量取决于消费者能跟上的稳态积压量；典型值在几到几千之间。`bounded` 满了会让生产者阻塞，从而形成背压，天然防止无界堆积。

---

### 4.2 fibonacci 生成器：bounded(0) 会合的活样本

#### 4.2.1 概念说明

仓库自带一个绝佳的 `bounded(0)` 示例：`examples/fibonacci.rs`。它把零容量通道当成一个「异步生成器」：主线程**每要一个数**，生成线程才**算一个并递过来**——双方严格一递一接，节奏完全同步。这比抽象描述更能说明「会合」的实用价值。

#### 4.2.2 核心流程

```
主线程                       生成线程(fibonacci)
  |                              |
  |  r.iter().take(20)           |
  |  ---- recv 等待 -----------> |  send(0) 阻塞等接收
  |                              |  （配对成功，交接 0）
  |  <--- 收到 0 ---             |
  |  ---- recv 等待 -----------> |  send(1) ...
  |  ......重复 20 次......      |
  | (take(20) 结束，r 被 drop)   |
  |                              |  下一次 send 返回 Err(断开)
  |                              |  while 循环退出，线程结束
```

关键点：当主线程取够 20 个数后 `main` 返回，`r` 被 drop，通道「断开」。此时生成线程下一次 `send` 会返回 `Err`，`while sender.send(x).is_ok()` 条件为假，循环自然终止。**无需任何额外的「停止信号」**，断开本身就是停止信号。

#### 4.2.3 源码精读

生成函数用 `while sender.send(x).is_ok()` 驱动，把「发送失败」当作「该停了」：

[crossbeam-channel/examples/fibonacci.rs:8-15](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/examples/fibonacci.rs#L8-L15) —— `fibonacci` 不断把当前项 `x` 发出去，再推进数列。注意它没有循环计数器，完全靠 `send` 的成功与否控制生命周期。

主线程用 `bounded(0)` 配合迭代器消费：

[crossbeam-channel/examples/fibonacci.rs:17-25](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/examples/fibonacci.rs#L17-L25) —— `let (s, r) = bounded(0)` 建会合通道；生成线程在后台跑，主线程 `r.iter().take(20)` 只取前 20 个斐波那契数打印。

把这段和 4.1.3 的 `bounded()` 构造函数对照看：正因为 `bounded(0)` 走的是 `Zero` flavor，这里的每一次 `send` 都会阻塞到主线程的 `recv`（即 `iter().next()`）出现，从而实现「逐个同步生成」。

#### 4.2.4 代码实践

**实践目标**：亲手跑通这个官方示例，并验证「断开即停止」。

**操作步骤**：在仓库根目录执行

```bash
cargo run -p crossbeam-channel --example fibonacci
```

**需要观察的现象**：打印前 20 个斐波那契数（`0 1 1 2 3 5 8 ...`），且程序正常退出（不挂起）。

**预期结果**：屏幕上出现 20 行数字，最后一项是 `4181`（第 20 项，从 `0` 作为第 1 项算起）。生成线程因 `send` 失败而退出，主线程 `take(20)` 结束后返回，整个进程结束。

**延伸小改动（可选）**：把 `take(20)` 改成 `take(5)`，再在 `fibonacci` 函数里给 `send` 前加一行 `println!("生成: {}", x);`，你会清楚看到「生成一个、消费一个」的严格交替节奏。**待本地验证**实际交替顺序。

#### 4.2.5 小练习与答案

**练习 1**：如果把示例里的 `bounded(0)` 改成 `unbounded()`，行为会有什么不同？

**参考答案**：生成线程不再被接收节奏约束——它会**全速**把斐波那契数不断塞进无界缓冲（直到 `u64` 溢出或内存耗尽），而主线程仍只取前 20 个。失去了「同步逐个生成」的会合特性，也浪费了大量计算。这正是零容量相对无界的一个独特价值。

**练习 2**：为什么 `fibonacci` 函数不需要接收一个「停止」标志参数？

**参考答案**：因为通道断开时 `send` 返回 `Err`，`while sender.send(x).is_ok()` 自动结束循环。「断开」本身就是最自然的停止信号，无需额外协议。

---

### 4.3 Sender / Receiver 公共方法与错误类型概览

#### 4.3.1 概念说明

无论哪种通道，`Sender<T>` 和 `Receiver<T>` 的**公共方法签名完全一致**——这是 flavor 派发带来的统一体验。每个核心操作都有「三兄弟」：

- **非阻塞**版：`try_send` / `try_recv`，条件不满足立刻返回错误。
- **阻塞**版：`send` / `recv`，等到成功或通道断开。
- **带超时/截止时间**版：`send_timeout` / `send_deadline` / `recv_timeout` / `recv_deadline`，只等有限时间。

对应的错误类型也成体系：阻塞版错误最「精简」（只表达断开），非阻塞版多了「满 / 空」，超时版又多了「超时」。理清这张「操作 × 错误」对照表，是用好 channel 的关键。

#### 4.3.2 核心流程

发送侧与接收侧的错误对照：

| 操作 | 成功 | 阻塞失败 | 非阻塞失败（try） | 超时失败 |
| --- | --- | --- | --- | --- |
| 发送 | `Ok(())` | `SendError(T)`（仅「断开」） | `TrySendError::{Full(T), Disconnected(T)}` | `SendTimeoutError::{Timeout(T), Disconnected(T)}` |
| 接收 | `Ok(T)` | `RecvError`（仅「空且断开」） | `TryRecvError::{Empty, Disconnected}` | `RecvTimeoutError::{Timeout, Disconnected}` |

两条规律：

1. **失败一定带回原消息**：所有携带 `T` 的发送错误（`SendError(T)`、`TrySendError`、`SendTimeoutError`）都把没发出去的消息原封不动还给你，可用 `into_inner()` / 模式匹配取回，不丢数据。
2. **断开（disconnected）是终极状态**：当所有对端被 drop，通道断开，此后任何操作都**不再阻塞**，而是立刻返回 `Disconnected`（对发送侧）或 `Disconnected`（接收侧）。

此外 `Sender` / `Receiver` 还有一组「查询」方法：`is_empty()`、`is_full()`、`len()`、`capacity()`、`same_channel()`，以及共享相关的 `clone()`。`Sender` 可 clone（多生产者），`Receiver` 也可 clone（多消费者，这是相对 std::mpsc 的关键能力）。

#### 4.3.3 源码精读

先看 `send` 如何派发并把内部 `SendTimeoutError` 收敛成对外的 `SendError`：

[crossbeam-channel/src/channel.rs:446-456](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L446-L456) —— `send` 调用底层 `chan.send(msg, None)`（`None` 表示无截止时间，即纯阻塞），再把结果 `map_err`：只有 `Disconnected(msg)` 会映射成 `SendError(msg)`，而 `Timeout(_)` 分支用 `unreachable!()` 标注——因为传了 `None` 永远不可能超时。这正解释了「阻塞版只可能因断开失败」。

`try_send` 则把三种 flavor 一视同仁地派发：

[crossbeam-channel/src/channel.rs:410-416](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L410-L416) —— 直接 `match &self.flavor` 后调用对应 flavor 的 `try_send`，返回 `Result<(), TrySendError<T>>`。

接收侧同理，`recv` 把底层 `RecvTimeoutError` 收敛成无参数的 `RecvError`：

[crossbeam-channel/src/channel.rs:831-857](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L831-L857) —— 注意末尾 `.map_err(|_| RecvError)`：无论底层返回 `Timeout` 还是 `Disconnected`（阻塞调用只会是后者），对外都折叠成 `RecvError`，因为它不携带消息、无需区分。

而 `try_recv` 派发时，对 `At` / `Tick` 这类特殊 flavor 还用了 `mem::transmute_copy` 把 `Instant` 类型擦除回 `T`（这些是 [u3-l8](./u3-l8-flavor-tick-at-never.md) 的内容，本讲略过）：

[crossbeam-channel/src/channel.rs:778-801](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L778-L801)。

再看几个错误类型的定义。`SendError` 极简——只包一条消息：

[crossbeam-channel/src/err.rs:11-12](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/err.rs#L11-L12) —— `pub struct SendError<T>(pub T);`，元组结构体，唯一含义是「发送时通道已断开」。

`TrySendError` 多出一个 `Full` 变体：

[crossbeam-channel/src/err.rs:20-29](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/err.rs#L20-L29) —— `Full(T)` 表示「满了发不进」（零容量下意为「无接收者可配对」），`Disconnected(T)` 表示「通道已断开」。

接收侧的 `RecvError` 不带任何数据：

[crossbeam-channel/src/err.rs:53-54](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/err.rs#L53-L54) —— 因为接收失败本来就没拿到消息，无需返还。

`TryRecvError` 则区分「空」与「断开」：

[crossbeam-channel/src/err.rs:60-69](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/err.rs#L60-L69) —— `Empty`（暂时没消息，等等可能有）与 `Disconnected`（彻底没了）。

最后看「断开」是怎么被触发的。`Sender` 的 `Drop` 会释放引用计数并断开发送侧：

[crossbeam-channel/src/channel.rs:674-684](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L674-L684) —— drop 时调用 `chan.release(|c| c.disconnect_senders())`。当**最后一个**发送者被 drop，发送计数归零，触发断开，接收者随之被唤醒并收到 `Disconnected`。

`Clone` 则增加引用计数：

[crossbeam-channel/src/channel.rs:686-696](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L686-L696) —— `clone()` 走 `chan.acquire()`。`Sender` / `Receiver` 的 clone/borrow/drop 与计数销毁协议是 [u3-l2](./u3-l2-counter-and-errors.md) 的主题。

还有那个「零容量永远既空又满」的细节，明确写在查询方法的文档里：

[crossbeam-channel/src/channel.rs:549-552](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L549-L552)（`is_empty`：`Note: Zero-capacity channels are always empty.`）与 [crossbeam-channel/src/channel.rs:572-575](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L572-L575)（`is_full`：`Note: Zero-capacity channels are always full.`）。

#### 4.3.4 代码实践

**实践目标**：亲手制造 4.3.2 表格里的每一种错误，加深对错误体系的肌肉记忆。

**操作步骤**：

```rust
// 示例代码：复现各类错误
use crossbeam_channel::{bounded, unbounded, TryRecvError, TrySendError};

fn main() {
    // (a) TryRecvError::Empty
    let (s, r) = unbounded::<i32>();
    assert_eq!(r.try_recv(), Err(TryRecvError::Empty));

    // (b) TrySendError::Full —— 零容量下意为「无接收者」
    let (s2, r2) = bounded(0);
    assert_eq!(s2.try_send(1), Err(TrySendError::Full(1)));

    // (c) RecvError（阻塞版）：drop 所有发送者后 recv 立刻失败
    let (s3, r3) = unbounded();
    drop(s3);
    assert_eq!(r3.recv(), Err(crossbeam_channel::RecvError));

    // (d) SendError：drop 所有接收者后 send 失败，且原消息被带回
    let (s4, r4) = bounded::<i32>(1);
    drop(r4);
    assert_eq!(s4.send(42), Err(crossbeam_channel::SendError(42)));
}
```

**需要观察的现象**：四个断言全部成立，程序无 panic 退出。特别注意 (d)：`SendError(42)` 把没发出去的 `42` 完整带回。

**预期结果**：正常运行结束。若把 (b) 改成 `bounded(1)` 则 `s2.try_send(1)` 会成功（容量为 1，桶没满），可见 `Full` 与容量的关系。**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么阻塞版 `recv` 失败时只返回 `RecvError`，而 `try_recv` 失败时能区分 `Empty` 与 `Disconnected`？

**参考答案**：阻塞版 `recv` 会一直等到「有消息」或「断开」才返回，它**不会**在「暂时空」时返回，所以失败只剩唯一原因——断开，无需区分。`try_recv` 不等待，必须区分「现在空但以后可能有」（`Empty`）和「彻底没了」（`Disconnected`），好让调用者决定是否重试。

**练习 2**：`Sender::send` 失败时返回的 `SendError` 里包着原消息 `T`，这样设计有什么好处？

**参考答案**：保证「消息不丢失」。发送失败意味着消息没进通道，调用者可以从错误里取回 `T`（如 `err.into_inner()`），决定是改投到备用通道、落盘还是重试。错误即数据的模式让失败处理非常安全。

---

### 4.4 Receiver 作为迭代器消费消息

#### 4.4.1 概念说明

`Receiver<T>` 实现了迭代器接口，可以直接用 `for x in &r { ... }` 或 `for x in r { ... }` 消费消息。这大大简化了「持续接收」的代码。crossbeam 提供两种迭代器：

- `iter()` / `IntoIterator`：**阻塞**迭代器。每次 `next` 都调用 `recv`，没消息就**睡着等**，直到通道空**且断开**才返回 `None` 结束。
- `try_iter()`：**非阻塞**迭代器。每次 `next` 调用 `try_recv`，把此刻通道里**已有**的消息一次性抽干，没有就返回 `None`，**绝不等待**。

4.2 的 fibonacci 示例正是用 `r.iter().take(20)` —— 阻塞迭代器，逐个等、逐个取。

#### 4.4.2 核心流程

| 迭代器 | 底层调用 | `next` 行为 | 何时返回 `None` |
| --- | --- | --- | --- |
| `Iter`（`iter()`） | `recv()` | 阻塞等下一条 | 通道**空且已断开** |
| `TryIter`（`try_iter()`） | `try_recv()` | 立刻试取 | 此刻通道里没有消息（哪怕未断开） |

一个常见陷阱：阻塞迭代器**只有断开才会停**。如果发送者永远不 drop，`for x in r.iter()` 就永远不结束。所以「用 `iter()` 收集所有消息」时，必须确保发送者最终会被 drop（或所有发送线程结束），正如 4.2.2 里 fibonacci 那样。

#### 4.4.3 源码精读

`iter()` / `try_iter()` 只是构造对应的迭代器结构：

[crossbeam-channel/src/channel.rs:1103-1105](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1103-L1105)（`iter()` 返回 `Iter { receiver: self }`）和 [crossbeam-channel/src/channel.rs:1141-1143](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1141-L1143)（`try_iter()` 返回 `TryIter { receiver: self }`）。

真正的差异在各自的 `next`：

[crossbeam-channel/src/channel.rs:1272-1278](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1272-L1278) —— `Iter::next` 调用 `self.receiver.recv().ok()`：`recv` 阻塞，成功返回 `Some(T)`；只有断开时 `recv` 返回 `Err`，`.ok()` 转成 `None`，迭代结束。

[crossbeam-channel/src/channel.rs:1324-1330](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1324-L1330) —— `TryIter::next` 调用 `self.receiver.try_recv().ok()`：`try_recv` 不等待，空或断开都返回 `Err`，统一变 `None`。这就是它「抽干现有、绝不等待」的根源。

另外，`Iter` 和「消费 `Receiver` 所有权」的 `IntoIter` 都实现了 `FusedIterator`：

[crossbeam-channel/src/channel.rs:1270](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1270) —— `impl<T> FusedIterator for Iter<'_, T> {}`。`FusedIterator` 表示一旦返回过 `None`，之后永远返回 `None`，迭代器优化器可据此省略多余检查。

`lib.rs` 顶层文档的「Iteration」一节还给了官方用法对比：

[crossbeam-channel/src/lib.rs:210-252](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs#L210-L252) —— 用 `r.iter().collect()` 阻塞收集全部消息（注释强调 `collect` 会阻塞到 sender 被 drop），以及 `r.try_iter().collect()` 非阻塞抽干现有消息。

#### 4.4.4 代码实践

**实践目标**：对比 `iter()` 与 `try_iter()` 的阻塞差异。

**操作步骤**：

```rust
// 示例代码：对比阻塞与非阻塞迭代器
use std::thread;
use std::time::Duration;
use crossbeam_channel::unbounded;

fn main() {
    let (s, r) = unbounded();

    // 让生产者每 100ms 发一条，发完 3 条后 sleep 不 drop
    let h = thread::spawn(move || {
        for i in 0..3 { s.send(i).unwrap(); }
        thread::sleep(Duration::from_millis(500)); // 故意不 drop
        // 这里不 drop sender，模拟「生产者还活着」
    });

    thread::sleep(Duration::from_millis(50));

    // (1) try_iter：只抽干「此刻已有」的消息，立刻返回
    let now: Vec<_> = r.try_iter().collect();
    println!("try_iter 抽到: {:?}", now); // 多半是 [0,1,2]，不等待

    // (2) iter：会一直等到通道断开才停
    //     由于上面生产者没 drop s，下面这行若执行会一直挂着：
    // for _ in r.iter() { /* 永不结束，因为 s 没被 drop */ }

    h.join().unwrap();
}
```

**需要观察的现象**：`try_iter` 立刻打印出此刻通道里已有的消息（约 `[0, 1, 2]`），程序随即结束；而注释掉的 `r.iter()` 循环若启用则会**永久挂起**，因为 `s` 在被 `thread::spawn` 的闭包里 sleep 完后虽随线程结束而 drop，但 main 线程此时已卡在 `iter` 里——实际上要等 `s` drop 后才会解阻塞。

**预期结果**：`try_iter` 版正常打印并退出。仔细体会：**「想要持续消费到结束」用 `iter()`，且务必保证发送者最终 drop；「只处理积压、不等待」用 `try_iter()`**。具体打印顺序与时间相关，**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：写一段代码 `let v: Vec<_> = r.iter().collect();`，结果它卡住不动。最可能的原因是什么？

**参考答案**：`iter()` 是阻塞迭代器，只有通道「空**且**断开」才返回 `None`。卡住说明还有 `Sender` 没被 drop——通道没断开，`collect` 在等下一条消息。检查是否漏掉了发送者的 `drop(s)` 或发送线程是否还在运行。

**练习 2**：`try_iter()` 在一个尚未断开的空通道上调用 `next`，返回什么？在已断开的空通道上又返回什么？

**参考答案**：两种情况都返回 `None`——因为 `try_iter` 的 `next` 是 `try_recv().ok()`，而 `try_recv` 在「空」和「断开」时都返回 `Err`，`.ok()` 都变 `None`。所以 `try_iter` **不区分**「暂时没消息」和「彻底没了」，调用者若要区分需自己用 `try_recv`。

---

## 5. 综合实践

把本讲的三类通道、错误体系和迭代器串起来，完成下面这个生产者-消费者任务（对应本讲的总实践）。

**任务描述**

1. 用 `unbounded()` 实现：一个线程发送 `1..100`，主线程用 `for` 循环消费并求和，打印总和。
2. 改用 `bounded(8)` 重新实现：观察「背压」现象——当主线程消费较慢时，生产者会被阻塞在 `send` 上。
3. 思考并写下结论：什么场景下应该选 `bounded` 而不是 `unbounded`？

**参考实现（示例代码）**

```rust
// 示例代码：综合实践 —— 生产者消费者，对比 unbounded 与 bounded
use std::thread;
use std::time::Duration;
use crossbeam_channel::{bounded, unbounded};

fn main() {
    // ===== 第 1 步：unbounded 版 =====
    {
        let (s, r) = unbounded();
        thread::spawn(move || {
            for i in 1..100 { s.send(i).unwrap(); }
            // s 在此 drop，通道断开，r.iter() 才会结束
        });
        let sum: i64 = r.iter().sum();   // 阻塞迭代器，断开时停止
        println!("unbounded 总和 = {}", sum); // 期望 4950
    }

    // ===== 第 2 步：bounded(8) 版 + 背压 =====
    {
        let (s, r) = bounded(8);
        // 生产者：故意 sleep，让你能体感 send 在满时阻塞
        let producer = thread::spawn(move || {
            for i in 1..100 {
                // send 在缓冲满（8 条）时会阻塞，直到消费者取走一条
                println!("准备发送 {}", i);
                s.send(i).unwrap();
            }
        });
        // 消费者：主线程放慢消费
        let total: i64 = r.iter().sum();
        producer.join().unwrap();
        println!("bounded(8) 总和 = {}", total); // 期望 4950
    }
}
```

**需要观察的现象**

- 第 1 步的 `unbounded` 版：生产者几乎瞬间把 99 条全塞进缓冲，`sum` 很快算出 `4950`。
- 第 2 步的 `bounded(8)` 版：由于主线程在 `r.iter().sum()` 中消费（这里没加 sleep，但缓冲只有 8），生产者的 `send` 会在缓冲满时被阻塞——你可以在生产者里加 `thread::sleep(Duration::from_millis(10));` 并观察日志，会看到「准备发送」连续打印到 8 左右就被迫等待，等消费者取走才继续。

**预期结果**：两个版本都打印总和 `4950`（\( \sum_{i=1}^{99} i = 4950 \)）。`bounded(8)` 版的生产者日志会出现「块状」推进（每攒到约 8 条就停一下），这就是背压的直观表现。

**第 3 步结论参考**：

- 选 `bounded` 的场景：生产者可能远快于消费者、内存有限、需要用背压限流、或想显式控制积压量（如流水线节流、限速消费）。
- 选 `unbounded` 的场景：生产峰值短暂且消费者总能跟上、或你不关心内存占用、希望发送者永远不被阻塞（如日志收集、事件总线，丢弃阻塞风险换吞吐）。
- 选 `bounded(0)` 的场景：需要严格同步握手、强制双方节奏一致（如 4.2 的生成器、请求-应答一问一答）。

> 实际运行时，去掉/加上 sleep、调整容量，观察生产者日志的推进节奏，是理解背压最直接的方式。具体日志行数与时机与调度相关，**待本地验证**。

## 6. 本讲小结

- crossbeam-channel 用 `unbounded()` / `bounded(cap)` 两个构造函数产出**三类语义**通道：无界（永不阻塞发送）、有界（满则阻塞，背压）、零容量（会合，send/recv 必须同时在场）。`bounded(0)` 在源码里被单独分流到 `Zero` flavor。
- 它是 **MPMC** 通道：`Sender` 和 `Receiver` 都可 `clone()`，这是相对标准库 `std::sync::mpsc`（MPSC）的关键优势。
- 每个核心操作有「非阻塞 / 阻塞 / 带超时」三兄弟，对应一套递进的错误类型（`TryXxxError` 多一个「满/空」，`XxxTimeoutError` 多一个「超时」，阻塞版 `SendError` / `RecvError` 最精简）；发送失败总带回原消息，断开是终极状态。
- `Receiver` 可作迭代器：`iter()` 阻塞、直到断开才停；`try_iter()` 非阻塞、只抽干现有消息。fibonacci 示例展示了 `bounded(0)` + `iter()` 的会合式异步生成。
- 通道断开（所有对端 drop）会让阻塞操作立刻返回错误，常被用作天然的「停止信号」。
- 本讲只看了 `channel.rs` 的**公共接口层**：所有方法都是 `match &self.flavor` 的一层派发；底层 flavor 算法、引用计数、阻塞唤醒机制留给后续讲义。

## 7. 下一步学习建议

本讲只用了通道的「外壳」。接下来按依赖顺序深入：

1. **[u3-l2 counter 引用计数与 err 错误类型](./u3-l2-counter-and-errors.md)**：先搞清 `Sender` / `Receiver` 如何靠 `counter::Counter` 的原子计数共享同一通道、最后一个引用 drop 时如何销毁通道，并补全 select 相关的错误类型。
2. **[u3-l3 flavors 架构与 SelectHandle trait](./u3-l3-flavors-architecture.md)**：理解本讲反复出现的 `SenderFlavor` / `ReceiverFlavor` 枚举派发，以及统一的 `SelectHandle` trait 与 `Token`。
3. 之后可按需挑选：[u3-l4 array flavor](./u3-l4-flavor-array.md)（有界环形缓冲）、[u3-l5 list flavor](./u3-l5-flavor-list.md)（无界链表）、[u3-l6 zero flavor](./u3-l6-flavor-zero.md)（零容量会合）分别对应本讲三类通道的底层实现。

建议在进入 u3-l2 之前，先把本讲的「操作 × 错误」对照表（4.3.2）和三类通道语义表（4.1.1）记牢——它们是阅读所有 flavor 实现时的「行为契约」。
