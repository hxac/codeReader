# Sender/Receiver API 与三种阻塞模式

## 1. 本讲目标

学完本讲后，你应该能够：

- 说出 `Sender`/`Receiver` 的完整方法清单，并按「非阻塞 / 阻塞 / 超时截止」三种模式给它们分类。
- 区分发送侧的 `try_send` / `send` / `send_timeout` / `send_deadline` 四个方法，以及接收侧的 `try_recv` / `recv` / `recv_timeout` / `recv_deadline` 四个方法，知道它们各自返回什么错误类型。
- 使用 `len` / `capacity` / `is_empty` / `is_full` / `same_channel` 这些状态查询方法，并理解它们返回值与 flavor 的关系。
- 在 `src/channel.rs` 里打开任意一个方法，看懂它「`match flavor` 转发到具体实现」的母题，并能标注一个方法到底转发到了哪个底层 flavor。

本讲依然停留在「怎么用 + 怎么读源码入口」这一层。我们只读 `src/channel.rs` 这个「外壳文件」，不深入任何 flavor 的内部实现（那是进阶层 u2 的内容）。

> 本讲承接 [u1-l2](u1-l2-unbounded-and-bounded.md)：你已经会用 `unbounded()` / `bounded()` 建通道、会调用 `send` / `recv`，也知道 `SenderFlavor` / `ReceiverFlavor` 是把公共类型壳和底层实现粘合起来的枚举。本讲就把这套壳上的**全部公共方法**系统讲一遍。

---

## 2. 前置知识

在开始前，先建立几个直觉。

### 2.1 三种阻塞模式

线程在调用收发方法时，「要不要停下来等」有三种选择。`src/lib.rs` 的文档开宗明义地把它们列了出来：

[src/lib.rs:173-179](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs#L173-L179) — 收发操作有三种模式：非阻塞、阻塞、带超时的阻塞。

| 模式 | 行为 | 发送侧方法 | 接收侧方法 |
| --- | --- | --- | --- |
| 非阻塞（non-blocking） | 立即返回，成功就成功、失败就报错，**不等** | `try_send` | `try_recv` |
| 阻塞（blocking） | 一直等到操作能完成，或通道断开 | `send` | `recv` |
| 带超时的阻塞（timeout / deadline） | 只等一段时间，到点还没成就报超时 | `send_timeout` / `send_deadline` | `recv_timeout` / `recv_deadline` |

「超时（timeout）」和「截止（deadline）」是同一件事的两种表达：`timeout` 给的是「从现在起再等多久」（一个 `Duration`），`deadline` 给的是「最晚等到哪个时刻」（一个 `Instant`）。`send_timeout` 内部其实就是先把 `Duration` 换算成 `Instant` 截止时间，再转交给 `send_deadline`。这一点我们在 4.2 节会从源码里直接看到。

### 2.2 Result 与错误类型

`crossbeam-channel` 的收发方法几乎全都返回 `Result`。每种「模式 + 方向」对应一个**专门的错误类型**，它们都定义在 [`src/err.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/err.rs)。本讲会用到的有：

| 方法 | 返回的 `Result` | 错误类型的两种失败 |
| --- | --- | --- |
| `try_send` | `Result<(), TrySendError<T>>` | `Full(T)`（满了）/ `Disconnected(T)`（断开） |
| `send` | `Result<(), SendError<T>>` | 只有 `SendError(T)`（断开，因为 `send` 没有超时） |
| `send_timeout` / `send_deadline` | `Result<(), SendTimeoutError<T>>` | `Timeout(T)`（超时）/ `Disconnected(T)`（断开） |
| `try_recv` | `Result<T, TryRecvError>` | `Empty`（空）/ `Disconnected`（断开） |
| `recv` | `Result<T, RecvError>` | 只有 `RecvError`（断开） |
| `recv_timeout` / `recv_deadline` | `Result<T, RecvTimeoutError>` | `Timeout`（超时）/ `Disconnected`（断开） |

注意一个小规律：发送侧的错误（`TrySendError` / `SendTimeoutError`）都**携带原始消息** `T`，因为发送失败时你需要把消息「拿回来」；接收侧的错误不携带消息，因为本来就没有拿到消息。

### 2.3 「match flavor 转发」母题

在 u1-l2 我们已经见过一次：`send` 的实现就是 `match &self.flavor`，按 `Array` / `List` / `Zero` 把调用转发给底层，再做一次错误归一化。本讲你会发现**几乎每一个公共方法都是这个套路**——这是 `channel.rs` 贯穿全文件的「架构母题」。读源码时，记住一句话就够了：

> 公共方法只做两件事：① 把调用按 flavor 转发下去；② 把底层返回的错误归一化成对外的错误类型。

具体的阻塞、唤醒、CAS、配对等逻辑都不在 `channel.rs` 里，而在 `src/flavors/` 下对应的 flavor 实现里。

---

## 3. 本讲源码地图

本讲只深入一个文件，并参考一个错误定义文件：

| 文件 | 作用 |
| --- | --- |
| [`src/channel.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs) | 对外类型的「壳」：`impl<T> Sender<T>` 与 `impl<T> Receiver<T>` 两个大块就在这里，包含本讲要讲的全部方法。 |
| [`src/err.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/err.rs) | 收发方法返回的所有错误类型（`SendError` / `TrySendError` / `SendTimeoutError` / `RecvError` / `TryRecvError` / `RecvTimeoutError`）的定义。 |

辅助参考（本讲只点到为止）：

| 文件 | 作用 |
| --- | --- |
| [`src/lib.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs) | crate 顶层文档里 `# Blocking operations` 一段，是三种模式的官方表述。 |

---

## 4. 核心概念与源码讲解

### 4.1 三种阻塞模式与对应的错误类型

#### 4.1.1 概念说明

把 2.1 节那张表再展开成「直觉」：

- **非阻塞** `try_send` / `try_recv`：像「探头看一眼」。能成就成，不成就立刻带回一个「满 / 空」或「断开」的结果。适合做轮询、做事件循环里的快速路径。
- **阻塞** `send` / `recv`：像「排队等」。线程会停在这里，直到对端配合（发送方等消费者取走、接收方等生产者投递）或通道被断开。适合「我就要这条消息，没别的可干」的场景。
- **超时/截止** `send_timeout` / `recv_timeout` 等：像「排队等，但只等 N 秒」。介于两者之间——大多数情况下它愿意等，但不愿意无限等。

为什么要把同一个操作做成三种模式？因为不同调用方对「卡多久」的容忍度不同：UI 线程不能卡死、超时控制任务不能无限等、而后台 worker 通常可以无限等。库把三种策略都提供出来，让你按场景挑。

#### 4.1.2 核心流程

三种模式在源码层面是这样衔接的：

1. **底层实现只有「带截止时间」这一种**。每个 flavor 的发送/接收都接受一个 `Option<Instant>` 作为截止时间。
2. **`send`** 把截止时间传 `None`（表示无限等待），再把底层返回的、最通用的 `SendTimeoutError` 映射成更窄的 `SendError`。
3. **`send_deadline`** 直接把传入的 `Instant` 透传给底层，返回 `SendTimeoutError`。
4. **`send_timeout`** 先用 `Instant::now().checked_add(timeout)` 把 `Duration` 换算成 `Instant`，再交给 `send_deadline`。

接收侧 `recv` / `recv_deadline` / `recv_timeout` 完全对称。

换句话说，**「带截止时间」是最底层的原语，其它两种模式都是在它之上做的封装**。这解释了为什么 `send` 的源码里会出现一个看起来很奇怪的 `unreachable!()`——后面 4.2.3 会解释。

#### 4.1.3 源码精读

先看错误类型本身。发送侧的 `TrySendError`：

[src/err.rs:19-29](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/err.rs#L19-L29) — `TrySendError` 两种变体：`Full(T)` 表示通道已满（零容量通道时表示当前没有接收方），`Disconnected(T)` 表示通道已断开。两者都保留原始消息 `T`。

```rust
pub enum TrySendError<T> {
    Full(T),
    Disconnected(T),
}
```

带超时的 `SendTimeoutError`，注意它的 `Timeout` 变体：

[src/err.rs:36-46](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/err.rs#L36-L46) — `SendTimeoutError` 两种变体：`Timeout(T)` 表示等到超时、`Disconnected(T)` 表示通道已断开。

接收侧的 `TryRecvError`（不带消息）：

[src/err.rs:59-69](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/err.rs#L59-L69) — `TryRecvError::Empty`（空，零容量时表示当前没有发送方）与 `Disconnected`（空且断开）。

这几种错误之间还有 `From` 转换关系，比如 `SendError<T>` 可以转成 `TrySendError::Disconnected`：

[src/err.rs:172-178](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/err.rs#L172-L178) — `From<SendError<T>> for TrySendError<T>`：把「断开」这一种失败在两种错误类型之间搬运。错误体系的完整讲解见 u2-l3。

#### 4.1.4 代码实践

**实践目标**：用一段程序把三种模式「同框」对比，观察它们返回值的不同。

**操作步骤**（这是「示例代码」，不是项目原有文件）：

```rust
// 示例代码：存成 src/bin/modes.rs，然后 cargo run --bin modes
use std::time::Duration;
use crossbeam_channel::{bounded, TryRecvError, TrySendError};

fn main() {
    let (s, r) = bounded(1); // 容量 1 的有界通道

    // 1) 非阻塞 try_send：第一条成功，第二条报 Full
    assert_eq!(s.try_send("a"), Ok(()));
    assert_eq!(s.try_send("b"), Err(TrySendError::Full("b")));

    // 2) 非阻塞 try_recv：取出已有的 "a"
    assert_eq!(r.try_recv(), Ok("a"));
    //    通道现在是空的，try_recv 报 Empty
    assert_eq!(r.try_recv(), Err(TryRecvError::Empty));

    // 3) 带超时 send_timeout：通道已空，可以再投一条；这里演示“等待接收方”的零容量更直观
    let (sz, rz) = bounded(0); // 零容量：必须等接收方
    assert_eq!(
        sz.send_timeout("x", Duration::from_millis(50)),
        Err(crossbeam_channel::SendTimeoutError::Timeout("x"))
    );
    // ↑ 50ms 内没有接收方出现，于是超时，原始消息 "x" 被带回
}
```

**需要观察的现象**：

- `try_send` 第二次返回 `Err(TrySendError::Full("b"))`，且消息 `"b"` 被完好地放在错误里。
- `try_recv` 在空通道上立即返回 `Err(TryRecvError::Empty)`，不卡顿。
- `send_timeout` 在零容量通道上 50ms 后返回 `Err(SendTimeoutError::Timeout("x"))`。

**预期结果**：所有 `assert_eq!` 通过，程序正常退出。

> 是否能本地运行：「待本地验证」——取决于你把文件放进 `src/bin/` 并配置好依赖。逻辑与库文档示例一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `try_send` 的错误里要保留消息 `T`，而 `try_recv` 的错误里不带任何数据？

**参考答案**：发送失败时，消息没有进入通道，调用方需要把它「拿回来」重新处置（重试、丢弃或转交），所以错误必须保留 `T`。接收失败时本来就没拿到任何消息，没什么可保留的，所以 `TryRecvError` 是个普通枚举（`Empty` / `Disconnected`），不携带数据。

**练习 2**：`send` 没有「超时」概念，它的错误类型 `SendError` 也只有一个变体。那它**唯一**的失败原因是什么？

**参考答案**：通道断开（所有接收者都被 drop）。`send` 会无限等待，不会因「等太久」失败；只有当它发现「通道已经断开、永远不可能投递成功」时才返回 `SendError(msg)`。

---

### 4.2 Sender 的方法族：try_send / send / send_timeout / send_deadline

#### 4.2.1 概念说明

`Sender<T>` 上有四个投递方法，正好覆盖三种模式（超时和截止共用一套底层，算同一类）：

| 方法 | 模式 | 入参 | 返回 |
| --- | --- | --- | --- |
| `try_send(msg)` | 非阻塞 | 消息 | `Result<(), TrySendError<T>>` |
| `send(msg)` | 阻塞 | 消息 | `Result<(), SendError<T>>` |
| `send_timeout(msg, timeout)` | 超时（Duration） | 消息 + `Duration` | `Result<(), SendTimeoutError<T>>` |
| `send_deadline(msg, deadline)` | 截止（Instant） | 消息 + `Instant` | `Result<(), SendTimeoutError<T>>` |

另外 `Sender` 上还有几个**不涉及投递**的方法：`is_empty` / `is_full` / `len` / `capacity` / `same_channel`，我们放到 4.4 节统一讲。

#### 4.2.2 核心流程

四个投递方法的依赖关系是一个清晰的「漏斗」：

```text
send_timeout(msg, Duration)
        │  (把 Duration 换算成 Instant)
        ▼
send_deadline(msg, Instant)  ──► match flavor 转发到底层 chan.send(msg, Some(deadline))
                                      （底层只有这一种「带截止时间」的原语）

send(msg)  ──► match flavor 转发到底层 chan.send(msg, None)   （None = 无限等待）
                  再把 SendTimeoutError 归一化为 SendError

try_send(msg) ──► match flavor 转发到底层 chan.try_send(msg)  （底层自己实现“立即返回”语义）
```

关键洞察：**`send` 和 `send_deadline` 调用的是底层的同一个 `chan.send(msg, deadline)` 方法**，区别只在传 `None` 还是 `Some(deadline)`。这就是「底层只有带截止时间一种原语」的体现。

#### 4.2.3 源码精读

先看非阻塞的 `try_send`，它的分发结构最干净：

[src/channel.rs:410-416](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L410-L416) — `try_send` 按 `Array` / `List` / `Zero` 三种 flavor 转发，直接返回底层的 `TrySendError`，不做任何错误转换。

```rust
pub fn try_send(&self, msg: T) -> Result<(), TrySendError<T>> {
    match &self.flavor {
        SenderFlavor::Array(chan) => chan.try_send(msg),
        SenderFlavor::List(chan) => chan.try_send(msg),
        SenderFlavor::Zero(chan) => chan.try_send(msg),
    }
}
```

再看阻塞的 `send`。注意它传给底层的是 `None`，然后用 `map_err` 把通用的 `SendTimeoutError` 收窄成 `SendError`：

[src/channel.rs:446-456](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L446-L456) — `send` 按 flavor 转发到底层 `chan.send(msg, None)`，再把 `SendTimeoutError::Disconnected` 映射成 `SendError`；`Timeout` 分支因为不可能发生（传的是 `None`，永远不会超时），用 `unreachable!()` 兜底。

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

> 为什么 `Timeout` 分支用 `unreachable!()`？因为传给底层的截止时间是 `None`（无限等待），底层根本不可能返回 `Timeout`。这是用 `match` 把「类型上存在、但运行时不可能」的状态显式排除，既满足穷尽匹配，又能在万一逻辑出错时立刻 panic 暴露问题。

接着看 `send_timeout`，它纯粹是「Duration → Instant」的换算，然后委托给 `send_deadline`：

[src/channel.rs:495-500](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L495-L500) — `send_timeout` 用 `Instant::now().checked_add(timeout)` 算出截止时刻：算得出来就交给 `send_deadline`；算不出来（`timeout` 大到溢出）就退化为 `send`（无限等待）。

```rust
pub fn send_timeout(&self, msg: T, timeout: Duration) -> Result<(), SendTimeoutError<T>> {
    match Instant::now().checked_add(timeout) {
        Some(deadline) => self.send_deadline(msg, deadline),
        None => self.send(msg).map_err(SendTimeoutError::from),
    }
}
```

> 注意 `checked_add` 的兜底：如果传入一个天文数字般的 `Duration`，`Instant::now() + timeout` 可能溢出。这时库选择「退化为无限等待」（调用 `send`），再用 `From` 把 `SendError` 转成 `SendTimeoutError::Disconnected`。这是个很务实的设计——反正你都愿意等那么久了，等下去也无妨。

最后是真正干活的 `send_deadline`，它把 `Instant` 透传给底层：

[src/channel.rs:541-547](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L541-L547) — `send_deadline` 按 flavor 转发到底层 `chan.send(msg, Some(deadline))`，把「截止时间」原样交给底层。

```rust
pub fn send_deadline(&self, msg: T, deadline: Instant) -> Result<(), SendTimeoutError<T>> {
    match &self.flavor {
        SenderFlavor::Array(chan) => chan.send(msg, Some(deadline)),
        SenderFlavor::List(chan) => chan.send(msg, Some(deadline)),
        SenderFlavor::Zero(chan) => chan.send(msg, Some(deadline)),
    }
}
```

到这里你应该看明白了：**四个投递方法，本质上是「同一个底层 `chan.send(msg, Option<Instant>)` + 不同错误归一化」的三种包装**。这就是 `Sender` 这一面「match flavor 转发」母题的全貌。

#### 4.2.4 代码实践

**实践目标**：亲手触发 `TrySendError::Full` 和 `Disconnected`，并用 `send_timeout` 触发超时，标注每个方法在源码里 match 到哪个 flavor。

**操作步骤**（这是「示例代码」）：

```rust
// 示例代码：src/bin/sender_api.rs，然后 cargo run --bin sender_api
use std::thread;
use std::time::Duration;
use crossbeam_channel::{bounded, TrySendError, SendTimeoutError};

fn main() {
    let (s, r) = bounded(1); // 有界 array flavor

    // ① Full：第二条投不进去
    s.try_send(1).unwrap();                          // → 转发到 Array::try_send，Ok
    let err = s.try_send(2).unwrap_err();            // → 转发到 Array::try_send，返回 Full
    assert_eq!(err, TrySendError::Full(2));
    assert_eq!(err.into_inner(), 2);                 // 把消息“拿回来”

    // ② 超时：零容量 zero flavor，等不到接收方
    let (sz, _rz) = bounded::<i32>(0);
    let err = sz.send_timeout(9, Duration::from_millis(30)).unwrap_err();
    //  send_timeout → checked_add → send_deadline → Zero::send(msg, Some(deadline)) → Timeout
    assert!(err.is_timeout());
    assert_eq!(err.into_inner(), 9);

    // ③ Disconnected：drop 掉唯一的接收者后，通道断开
    let (s2, r2) = bounded::<i32>(1);
    drop(r2);
    let err = s2.try_send(7).unwrap_err();           // → 转发到 Array::try_send，返回 Disconnected
    assert_eq!(err, TrySendError::Disconnected(7));

    // 顺便：让一个阻塞 send 成功完成，需要另一个线程来消费
    let (s3, r3) = bounded(0);
    let h = thread::spawn(move || s3.send("hi").unwrap()); // 阻塞至主线程 recv
    assert_eq!(r3.recv(), Ok("hi"));
    h.join().unwrap();
}
```

**需要观察的现象**：每个 `assert` 都通过；尤其注意 `into_inner()` 能从 `Full` / `Disconnected` / `Timeout` 里把原始消息取回。

**预期结果**：程序无 panic 正常退出。

> 是否能本地运行：「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：在 `bounded(1)` 通道上，调用 `s.send(2)`（注意是阻塞版 `send`，不是 `try_send`）会发生什么？结合源码说明。

**参考答案**：第一条 `send(1)` 成功后通道已满，第二条 `send(2)` 会**阻塞当前线程**，直到有接收方取走消息腾出位置、或通道断开。从源码看，`send` 转发到底层 `chan.send(msg, None)`，`None` 表示无限等待，所以它会一直卡着。如果这时 `drop(r)`，底层会返回 `SendTimeoutError::Disconnected`，被 `send` 映射成 `Err(SendError(2))` 并唤醒。

**练习 2**：如果给 `send_timeout` 传一个极大、导致 `Instant::now() + timeout` 溢出的 `Duration`，会发生什么？

**参考答案**：`checked_add` 返回 `None`，于是走 `None => self.send(msg).map_err(SendTimeoutError::from)` 分支，等价于调用无限等待的 `send`。也就是说，它不会 panic、不会立刻返回，而是「退化为阻塞 `send`」。

---

### 4.3 Receiver 的方法族：try_recv / recv / recv_timeout / recv_deadline

#### 4.3.1 概念说明

`Receiver<T>` 上的四个接收方法，和发送侧**完全对称**：

| 方法 | 模式 | 入参 | 返回 |
| --- | --- | --- | --- |
| `try_recv()` | 非阻塞 | 无 | `Result<T, TryRecvError>` |
| `recv()` | 阻塞 | 无 | `Result<T, RecvError>` |
| `recv_timeout(timeout)` | 超时（Duration） | `Duration` | `Result<T, RecvTimeoutError>` |
| `recv_deadline(deadline)` | 截止（Instant） | `Instant` | `Result<T, RecvTimeoutError>` |

但接收侧有一个发送侧没有的复杂性：`ReceiverFlavor` 比 `SenderFlavor` 多三种变体——`At` / `Tick` / `Never`（对应 `after` / `at` / `tick` / `never` 这几个只读特殊通道，u2-l8 会专门讲）。所以接收方法的 `match` 要处理**六个**分支。

#### 4.3.2 核心流程

接收侧的结构和发送侧如出一辙：

```text
recv_timeout(Duration)
        │  (Duration → Instant)
        ▼
recv_deadline(Instant) ──► match flavor 转发到底层 chan.recv(Some(deadline))

recv() ──► match flavor 转发到底层 chan.recv(None)
              再把 RecvTimeoutError 归一化为 RecvError

try_recv() ──► match flavor 转发到底层 chan.try_recv()
```

一个值得注意的细节：对 `At` / `Tick` 两个特殊 flavor，底层返回的是 `Result<Instant, _>`（它们投递的消息永远是 `Instant` 类型），但公共 API 要把它伪装成 `Result<T, _>`。源码里用了一段 `unsafe { mem::transmute_copy(...) }` 来做这个「类型擦除」。本讲只标注它的存在，不展开 unsafe 细节（那是 u3-l4 的内容）。

#### 4.3.3 源码精读

非阻塞的 `try_recv`：

[src/channel.rs:778-801](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L778-L801) — `try_recv` 按六个 flavor 分支转发。`Array` / `List` / `Zero` / `Never` 直接调用底层 `try_recv`；`At` / `Tick` 因消息类型固定为 `Instant`，用 `transmute_copy` 转成 `Result<T, _>`。

```rust
pub fn try_recv(&self) -> Result<T, TryRecvError> {
    match &self.flavor {
        ReceiverFlavor::Array(chan) => chan.try_recv(),
        ReceiverFlavor::List(chan) => chan.try_recv(),
        ReceiverFlavor::Zero(chan) => chan.try_recv(),
        ReceiverFlavor::At(chan) => {
            let msg = chan.try_recv();
            unsafe { mem::transmute_copy::<Result<Instant, TryRecvError>, Result<T, TryRecvError>>(&msg) }
        }
        ReceiverFlavor::Tick(chan) => { /* 同上 */ }
        ReceiverFlavor::Never(chan) => chan.try_recv(),
    }
}
```

阻塞的 `recv`，注意末尾 `.map_err(|_| RecvError)` 把两种失败都归一化：

[src/channel.rs:831-857](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L831-L857) — `recv` 按六个 flavor 转发到底层 `chan.recv(None)`（`At`/`Tick` 同样走 `transmute_copy`），最后把任何失败都映射成单个 `RecvError`。

```rust
pub fn recv(&self) -> Result<T, RecvError> {
    match &self.flavor {
        ReceiverFlavor::Array(chan) => chan.recv(None),
        ReceiverFlavor::List(chan) => chan.recv(None),
        ReceiverFlavor::Zero(chan) => chan.recv(None),
        // ... At / Tick / Never 分支
    }
    .map_err(|_| RecvError)
}
```

> 为什么 `recv` 把超时和断开都映射成 `RecvError`？因为 `recv` 没有「超时」概念，底层用 `None` 调用，理论上不会返回 `Timeout`，唯一可能的失败就是「通道空且断开」，正好对应 `RecvError`。`.map_err(|_| RecvError)` 一刀切掉所有失败变体，对外只暴露「收不到了」这一个信号。

`recv_timeout` 同样是「Duration → Instant」的换算：

[src/channel.rs:896-901](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L896-L901) — `recv_timeout` 用 `checked_add` 把 `Duration` 换成 `Instant`，算不出就退化为 `recv`。

```rust
pub fn recv_timeout(&self, timeout: Duration) -> Result<T, RecvTimeoutError> {
    match Instant::now().checked_add(timeout) {
        Some(deadline) => self.recv_deadline(deadline),
        None => self.recv().map_err(RecvTimeoutError::from),
    }
}
```

真正干活的 `recv_deadline`，把 `Instant` 透传给底层（六个分支）：

[src/channel.rs:944-969](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L944-L969) — `recv_deadline` 按六个 flavor 转发到底层 `chan.recv(Some(deadline))`，是接收侧「带截止时间」的最终落点。

`recv_deadline` 的文档里还点出了一个比 `send_deadline` 多一条的语义：**即使截止时间已到，只要通道里还有未消费的消息，`recv_deadline` 仍会返回下一条消息**（而不是立刻超时）。这和「先到期的截止时间」与「已在缓冲区里的消息」之间的优先级有关，详见源码注释。

#### 4.3.4 代码实践

**实践目标**：用 `try_recv` 触发 `Empty`，用 `recv_timeout` 触发 `Timeout`，并对比「断开」后的行为。

**操作步骤**（这是「示例代码」）：

```rust
// 示例代码：src/bin/receiver_api.rs，然后 cargo run --bin receiver_api
use std::time::Duration;
use crossbeam_channel::{unbounded, TryRecvError, RecvTimeoutError};

fn main() {
    let (s, r) = unbounded::<i32>(); // 无界 list flavor

    // ① Empty：通道为空时 try_recv 立即返回 Empty
    assert_eq!(r.try_recv(), Err(TryRecvError::Empty));   // → List::try_recv → Empty

    // ② Timeout：recv_timeout 等不到消息就超时
    let err = r.recv_timeout(Duration::from_millis(30)).unwrap_err();
    //  recv_timeout → checked_add → recv_deadline → List::recv(Some(deadline)) → Timeout
    assert_eq!(err, RecvTimeoutError::Timeout);

    // ③ 投一条进去再取
    s.send(42).unwrap();
    assert_eq!(r.recv_timeout(Duration::from_secs(1)), Ok(42));

    // ④ Disconnected：drop 发送端后，剩余消息仍可收，收完后报 Disconnected
    s.send(7).unwrap();
    drop(s);
    assert_eq!(r.try_recv(), Ok(7));                       // 断开后剩余消息仍能收到
    assert_eq!(r.try_recv(), Err(TryRecvError::Disconnected));
}
```

**需要观察的现象**：

- 步骤 ① 中 `try_recv` **立即**返回，没有任何等待。
- 步骤 ② 中 `recv_timeout` 大约等了 30ms 才返回 `Timeout`。
- 步骤 ④ 中，即使发送端已 drop，之前投进去的 `7` 依然能被 `try_recv` 取回——**断开不等于清空**。

**预期结果**：所有断言通过。

> 是否能本地运行：「待本地验证」。

#### 4.3.5 小练习与答案

**练习 1**：`recv()` 在「通道空但未断开」和「通道空且已断开」两种情况下，行为有什么不同？

**参考答案**：前者会**阻塞**等待，直到有消息进来或通道被断开；后者会**立即**返回 `Err(RecvError)`。换句话说，`recv` 只在「既没消息、又没断开」时才卡住，一旦断开就一定立刻返回错误，不会再等。

**练习 2**：为什么 `Receiver` 的 `match` 要处理六个分支，而 `Sender` 只要三个？

**参考答案**：因为 `ReceiverFlavor` 比 `SenderFlavor` 多出 `At` / `Tick` / `Never` 三个变体——它们对应 `after` / `at` / `tick` / `never` 这几个**只读**特殊通道（只有接收端、没有发送端），所以 `SenderFlavor` 里没有它们。这是接收侧独有的复杂性。

---

### 4.4 状态查询方法：len / capacity / is_empty / is_full / same_channel

#### 4.4.1 概念说明

除了投递和接收，`Sender` / `Receiver` 上还有一组「只看不动」的查询方法，它们让你在不改变通道状态的情况下了解通道现状：

| 方法 | 含义 | 注意点 |
| --- | --- | --- |
| `capacity()` | 有界通道返回 `Some(cap)`，无界返回 `None` | 零容量返回 `Some(0)` |
| `len()` | 通道里当前有多少条消息 | 零容量永远返回 `0` |
| `is_empty()` | 通道里是否没有消息 | 零容量**永远为空** |
| `is_full()` | 通道是否已满 | 零容量**永远为满** |
| `same_channel(&other)` | 两个端点是否连着**同一条**通道 | 克隆出来的端点返回 `true` |

这里有一个反直觉但很重要的点：**零容量通道（`bounded(0)`）的 `is_empty()` 和 `is_full()` 同时永远为 `true`**。因为它存不下任何消息，所以「永远是空的」；又因为它的语义是「发送方必须等待接收方」，从发送方视角看「永远是满的」。`channel.rs` 里这两个方法的文档注释明确写了这一点。

> 注意：`len()` / `is_empty()` 在并发环境下返回的是**某一瞬间的快照**，等你拿到返回值时状态可能已经变了。不要用它们做关键的同步决策，只适合做监控、日志或启发式判断。

#### 4.4.2 核心流程

这组方法的实现是「match flavor 转发」母题最纯粹的形式——**没有任何错误归一化**，因为它们不会失败：

```text
查询方法() ──► match flavor 转发到底层 chan.len() / chan.capacity() / ...
                 直接返回 usize / bool / Option<usize>
```

唯一的例外是 `same_channel`，它要同时看**两个**端点的 flavor：

```text
same_channel(other) ──► match (self.flavor, other.flavor)
                          同 flavor 且底层相等 → true
                          不同 flavor / 底层不同 → false
```

#### 4.4.3 源码精读

先看 `Sender` 的 `len` / `capacity`，纯粹的转发：

[src/channel.rs:609-615](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L609-L615) — `Sender::len` 按三种 flavor 转发，返回当前消息数。

[src/channel.rs:633-639](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L633-L639) — `Sender::capacity`：无界（`List`）返回 `None`，有界（`Array`，以及 `Zero` 返回 `Some(0)`）返回 `Some(cap)`。

`is_empty` 与 `is_full`，注意源码注释里对零容量的说明：

[src/channel.rs:564-570](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L564-L570) — `Sender::is_empty`：注释「Note: Zero-capacity channels are always empty.」点明零容量恒为空。

[src/channel.rs:587-593](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L587-L593) — `Sender::is_full`：注释「Note: Zero-capacity channels are always full.」点明零容量恒为满。

`same_channel` 的二元 `match`，这是发送侧的版本：

[src/channel.rs:656-663](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L656-L663) — `Sender::same_channel`：只有两端 flavor 相同且底层计数句柄相等（`a == b`）才返回 `true`，不同 flavor 直接 `false`。

```rust
pub fn same_channel(&self, other: &Self) -> bool {
    match (&self.flavor, &other.flavor) {
        (SenderFlavor::Array(ref a), SenderFlavor::Array(ref b)) => a == b,
        (SenderFlavor::List(ref a), SenderFlavor::List(ref b)) => a == b,
        (SenderFlavor::Zero(ref a), SenderFlavor::Zero(ref b)) => a == b,
        _ => false,
    }
}
```

接收侧的 `same_channel` 多了 `At` / `Tick` / `Never` 三个分支，比较方式也各有不同：

[src/channel.rs:1160-1170](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1160-L1170) — `Receiver::same_channel`：`Array` / `List` / `Zero` 用计数句柄相等比较；`At` / `Tick` 用 `Arc::ptr_eq` 比较底层指针；`Never` 因为是零大小类型、所有实例都「等价」，直接返回 `true`。

```rust
pub fn same_channel(&self, other: &Self) -> bool {
    match (&self.flavor, &other.flavor) {
        (ReceiverFlavor::Array(a), ReceiverFlavor::Array(b)) => a == b,
        // ... List / Zero 同理
        (ReceiverFlavor::At(a), ReceiverFlavor::At(b)) => Arc::ptr_eq(a, b),
        (ReceiverFlavor::Tick(a), ReceiverFlavor::Tick(b)) => Arc::ptr_eq(a, b),
        (ReceiverFlavor::Never(_), ReceiverFlavor::Never(_)) => true,
        _ => false,
    }
}
```

> 这里能看出不同 flavor 的「身份」是怎么判定的：`Array` / `List` / `Zero` 共享一个堆上的计数对象（`counter`），靠句柄相等判定；`At` / `Tick` 是 `Arc` 包裹的，靠指针相等判定；`Never` 没有可区分的状态，任何两个 `Never` 接收端都算「同一条（不存在的）通道」。这些差异背后是 `counter.rs` 和各 flavor 的存储模型，将在 u2-l2、u2-l8 展开。

接收侧的 `is_empty` / `is_full` / `len` / `capacity` 是六个分支的转发，结构完全平行，例如 `Receiver::is_empty`：

[src/channel.rs:986-995](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L986-L995) — `Receiver::is_empty` 按六个 flavor 转发，注意它比 `Sender::is_empty` 多出 `At` / `Tick` / `Never` 三个分支。

#### 4.4.4 代码实践

**实践目标**：用查询方法观察一条 `bounded(1)` 通道从空到满、再到空的状态变化，并验证 `same_channel`。

**操作步骤**（这是「示例代码」）：

```rust
// 示例代码：src/bin/queries.rs，然后 cargo run --bin queries
use crossbeam_channel::{bounded, unbounded};

fn main() {
    let (s, r) = bounded(1);

    // 初始：空、不满、容量 1、0 条消息
    assert!(s.is_empty());          // → Array::is_empty → true
    assert!(!s.is_full());          // → Array::is_full  → false
    assert_eq!(s.capacity(), Some(1));
    assert_eq!(s.len(), 0);

    s.send(0).unwrap();

    // 投一条后：不空、满、1 条消息
    assert!(!s.is_empty());
    assert!(s.is_full());
    assert_eq!(s.len(), 1);

    r.recv().unwrap();
    assert!(s.is_empty());          // 取走后又变空

    // same_channel：克隆出来的端连着同一条通道
    let s2 = s.clone();
    assert!(s.same_channel(&s2));
    let (s3, _r3) = bounded::<i32>(1);
    assert!(!s.same_channel(&s3));  // 不同通道

    // 零容量的“既空又满”
    let (sz, _rz) = bounded::<i32>(0);
    assert_eq!(sz.capacity(), Some(0));
    assert!(sz.is_empty());
    assert!(sz.is_full());          // 同时为 true！
    assert_eq!(sz.len(), 0);

    // 无界容量是 None
    let (su, _ru) = unbounded::<i32>();
    assert_eq!(su.capacity(), None);
}
```

**需要观察的现象**：`bounded(0)` 的 `is_empty()` 和 `is_full()` 同时为 `true`；`unbounded` 的 `capacity()` 是 `None`。

**预期结果**：所有断言通过。

> 是否能本地运行：「待本地验证」。

#### 4.4.5 小练习与答案

**练习 1**：为什么不能用 `if !r.is_empty() { r.recv() }` 来「安全地非阻塞接收」？应该用什么？

**参考答案**：因为 `is_empty()` 返回的是某一瞬间的快照，存在 TOCTOU（check-then-act）竞态——你判断完「不空」之后、调用 `recv()` 之前，别的线程可能已经把消息取走了，于是 `recv()` 会意外阻塞。要「非阻塞接收」应该直接用 `r.try_recv()`，它在底层是原子的「试一次」操作。同理，要「非阻塞发送」就用 `try_send`，而不是先 `is_full` 再 `send`。

**练习 2**：`same_channel` 判断「两个端连着同一条通道」。如果你克隆了一个 `Sender`，原端和克隆端 `same_channel` 会返回什么？为什么？

**参考答案**：返回 `true`。因为 `Sender::clone` 走的是 `counter::Sender::acquire`（增加引用计数、共享同一个底层通道对象），所以原端和克隆端的 `flavor` 变体相同、底层计数句柄也相等，`same_channel` 判定为同一条通道。这正是 u1-l2 讲过的「克隆是共享，不是复制」。

---

## 5. 综合实践

把本讲的「三种阻塞模式 + 状态查询 + match flavor 母题」串起来，完成下面这个贯穿任务。

**任务**：写一个「带超时的有界队列监控」小程序，完成以下行为。

1. 建一个 `bounded(2)` 通道。
2. 在主线程里：
   - 用 `try_send` 投两条消息（成功），再投第三条（应得到 `TrySendError::Full`，并用 `into_inner` 取回消息）。
   - 用 `send_timeout` 投第四条，设 100ms 超时（应得到 `SendTimeoutError::Timeout`，因为没有人消费）。
   - 用 `try_recv` 取出前两条（成功），第三次 `try_recv`（应得到 `TryRecvError::Empty`）。
   - 用 `recv_timeout` 再等 50ms（应得到 `RecvTimeoutError::Timeout`）。
3. 全程用 `capacity()` / `len()` 在每一步打印通道状态，验证快照与你的预期一致。
4. 在源码注释里标注：每个方法分别 match 到了 `SenderFlavor::Array` 还是 `ReceiverFlavor::Array`（因为 `bounded(2)` 走 array flavor）。

**参考实现**（「示例代码」）：

```rust
// 示例代码：src/bin/u1_l3_practice.rs，然后 cargo run --bin u1_l3_practice
use std::time::Duration;
use crossbeam_channel::{bounded, TrySendError, TryRecvError,
                        SendTimeoutError, RecvTimeoutError};

fn main() {
    let (s, r) = bounded(2); // → Array flavor（SenderFlavor::Array / ReceiverFlavor::Array）
    println!("capacity = {:?}", s.capacity()); // Some(2)

    // 1) try_send 两条成功
    s.try_send(1).unwrap();                 // → Array::try_send
    s.try_send(2).unwrap();
    println!("after 2 sends, len = {}", s.len()); // 2

    // 第三条：Full
    match s.try_send(3) {                   // → Array::try_send → Full
        Err(TrySendError::Full(m)) => {
            println!("try_send(3) Full, 拿回 {}", m);
            assert_eq!(m, 3);
        }
        other => panic!("意料之外: {:?}", other),
    }

    // 2) send_timeout 第四条：超时
    match s.send_timeout(4, Duration::from_millis(100)) {
        // send_timeout → checked_add → send_deadline → Array::send(_, Some(deadline)) → Timeout
        Err(SendTimeoutError::Timeout(m)) => {
            println!("send_timeout(4) 超时, 拿回 {}", m);
            assert_eq!(m, 4);
        }
        other => panic!("意料之外: {:?}", other),
    }

    // 3) try_recv 取两条
    assert_eq!(r.try_recv(), Ok(1));        // → Array::try_recv
    assert_eq!(r.try_recv(), Ok(2));
    println!("after 2 recvs, len = {}", r.len()); // 0
    // 第三次：Empty
    assert_eq!(r.try_recv(), Err(TryRecvError::Empty));

    // 4) recv_timeout 再等一下：Timeout
    let now = std::time::Instant::now();
    match r.recv_timeout(Duration::from_millis(50)) {
        // recv_timeout → checked_add → recv_deadline → Array::recv(Some(deadline)) → Timeout
        Err(RecvTimeoutError::Timeout) => println!("recv_timeout 超时, 耗时 {:?}", now.elapsed()),
        other => panic!("意料之外: {:?}", other),
    }
}
```

**完成标准**：

- 程序正常退出，所有 `assert` 通过；`recv_timeout` 的耗时约为 50ms 量级。
- 你能口述：`try_send` 失败时为什么能拿回消息？`send_timeout` 内部经过了哪两步才到达底层？
- 你能在 `src/channel.rs` 里指出 `try_send`（410 行起）、`send`（446 行起）、`send_timeout`（495 行起）、`send_deadline`（541 行起）四个方法，并说出它们的「match flavor 转发」分别转发到了哪个底层方法。
- 你能解释为什么 `bounded(2)` 通道上的所有方法都只会走 `Array` 这一个分支。

---

## 6. 本讲小结

- `Sender`/`Receiver` 的收发方法分为三种模式：非阻塞（`try_send`/`try_recv`）、阻塞（`send`/`recv`）、带超时截止（`send_timeout`/`send_deadline`、`recv_timeout`/`recv_deadline`）。
- 每种模式对应一个专门的错误类型，定义在 `src/err.rs`；发送侧错误携带原始消息 `T`（便于拿回），接收侧错误不携带数据。
- 底层只有「带截止时间」一种原语：`send`/`send_deadline` 调同一个底层 `chan.send(msg, Option<Instant>)`，区别只传 `None` 还是 `Some(deadline)`；`send_timeout` 先用 `checked_add` 把 `Duration` 换算成 `Instant` 再委托给 `send_deadline`，溢出时退化为 `send`。接收侧完全对称。
- 「`match flavor` 转发 + 错误归一化」是 `channel.rs` 贯穿全文件的母题：`send` 把 `SendTimeoutError::Timeout` 用 `unreachable!()` 排除、`recv` 用 `.map_err(|_| RecvError)` 一刀切。
- `Receiver` 的 `match` 要处理六个分支（`Array`/`List`/`Zero`/`At`/`Tick`/`Never`），比 `Sender` 多出三个只读特殊通道；`At`/`Tick` 因消息类型固定为 `Instant`，用 `transmute_copy` 伪装成 `T`。
- 状态查询方法 `len`/`capacity`/`is_empty`/`is_full`/`same_channel` 是纯转发（`same_channel` 是二元 `match`）；零容量通道「既空又满」，且这些方法返回的是瞬间快照，不能用于关键同步决策（要非阻塞就老老实实用 `try_send`/`try_recv`）。

---

## 7. 下一步学习建议

- **接着学共享与断开**：下一讲 [u1-l4](u1-l4-clone-sharing-disconnect.md) 会讲 `Sender`/`Receiver` 的 `Clone`/`Drop`（克隆共享同一通道、drop 时引用计数与断开），以及 `iter`/`try_iter`/`into_iter` 三个迭代器。它们都建立在本讲的 `try_recv`/`recv` 之上（比如 `Iter::next` 就是调用 `recv().ok()`）。
- **推荐阅读的源码**：
  - 把 `src/channel.rs` 里 `impl<T> Sender<T>`（388 行起）和 `impl<T> Receiver<T>`（755 行起）两个大块从头扫一遍，体会「每个方法都是 match flavor」。
  - 翻 `src/err.rs`，对照本讲那张错误类型表，找到 `into_inner` / `is_full` / `is_disconnected` / `is_timeout` 等辅助方法。
  - 读 `src/lib.rs` 的 `# Blocking operations`（173 行起）和 `# Iteration`（210 行起）两段文档。
- **可选的延伸阅读**：错误类型的 `From` 转换关系（如 `SendError` → `TrySendError::Disconnected`）会在 u2-l3 系统讲解；如果你想现在就理解「为什么 `send` 能用 `SendTimeoutError::from` 兜底」，可以先扫一眼 `src/err.rs` 里那几个 `impl From`。
