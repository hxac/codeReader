# 第一个通道：unbounded 与 bounded

## 1. 本讲目标

学完本讲后，你应该能够：

- 用 `unbounded()` 和 `bounded(cap)` 两种方式创建一个多生产者多消费者（mpmc）通道。
- 调用 `Sender::send` / `Receiver::recv` 收发消息，并说出它们的阻塞行为。
- 区分「无界通道」「有界通道」「零容量会合通道」三者，并解释容量（capacity）这件事在源码里如何体现。
- 在 `src/channel.rs` 里找到构造函数，看懂它们是如何根据容量「分流」到不同底层实现（flavor）的。

本讲只覆盖「怎么用」这一层。我们会读到真实源码的入口，但不会深入 flavor 的内部实现——那是进阶层（u2）的内容。

---

## 2. 前置知识

在开始前，先建立几个直觉。

### 2.1 什么是通道（channel）

通道是一条「单方向的传送带」：一端放东西，另一端取东西。`crossbeam-channel` 把放东西的一端叫 `Sender<T>`，取东西的一端叫 `Receiver<T>`，`T` 是消息的类型。一条通道总是成对返回两端。

### 2.2 mpsc 与 mpmc

- `mpsc`（multi-producer single-consumer）：多个发送者，只有一个接收者。Rust 标准库的 `std::sync::mpsc` 就是这种。
- `mpmc`（multi-producer multi-consumer）：多个发送者，**多个接收者**。`crossbeam-channel` 属于这一类——它的 `Receiver` 可以被克隆，多个线程可以同时从同一条通道里取消息。

这是 `crossbeam-channel` 相对标准库最直观的能力差异之一。

### 2.3 阻塞（blocking）是什么

「阻塞」是指：当前线程停在那里，一直等到某个条件满足才继续往下走。比如接收一个空通道时，`recv()` 会卡住，直到有消息进来或通道被断开。理解「什么时候会阻塞、什么时候立即返回」是本讲的核心。

### 2.4 flavor（风味）这个词

`crossbeam-channel` 把不同结构的通道实现叫做不同的「flavor」（风味）。无界通道、有界通道、零容量通道，在底层分别是三种不同的 flavor。本讲你会第一次在构造函数里看到这种「按容量选 flavor」的分流逻辑。

> 本讲承接 u1-l1：你已经知道项目是 `no_std` 友好的、`src/` 下有 `channel`/`flavors` 等模块、对外 API 集中在 `src/lib.rs` 的 `pub use`。本讲就把注意力聚焦到 `src/channel.rs` 里的两个构造函数和 `src/lib.rs` 的文档示例。

---

## 3. 本讲源码地图

本讲只涉及两个文件：

| 文件 | 作用 |
| --- | --- |
| [`src/channel.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs) | 对外类型的「壳」：定义 `Sender`/`Receiver`，提供 `unbounded()`/`bounded()` 等构造函数，并把每个方法按 flavor 分发到底层实现。 |
| [`src/lib.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs) | crate 顶层文档（含大量可运行示例），以及把 `Sender`/`Receiver`/`unbounded`/`bounded` 等 `pub use` 出去的导出语句。 |

辅助参考（本讲只点到为止）：

| 文件 | 作用 |
| --- | --- |
| `src/flavors/mod.rs` | 列出六种 flavor 的总目录，注释里说明了每种 flavor 的定位。 |
| `examples/fibonacci.rs` | 一个真实例子，用 `bounded(0)`（会合通道）实现斐波那契流。 |

---

## 4. 核心概念与源码讲解

### 4.1 创建通道：unbounded() 与 bounded() 函数

#### 4.1.1 概念说明

`crossbeam-channel` 提供两个最基础的构造函数：

- `unbounded()`：创建一条**无界**通道。它有一个可以无限增长的缓冲区，理论上能装下任意多条消息，`send` 永远不会因为「满了」而阻塞。
- `bounded(cap)`：创建一条**有界**通道，缓冲区最多只能装 `cap` 条消息。`cap == 0` 是一个特例——「零容量通道」，它根本不存消息，发送方和接收方必须**同时在场**才能把消息直接交接过去，这种模式叫做**会合（rendezvous）**。

两个函数都返回一对 `(Sender<T>, Receiver<T>)`。

#### 4.1.2 核心流程

两条构造函数的内部步骤几乎一样，区别只在「选哪种 flavor」：

1. 调用对应 flavor 的构造器，得到一个底层通道对象。
2. 用 `counter::new(...)` 把它包进引用计数里（这样多个 `Sender`/`Receiver` 才能共享同一条通道）。
3. 把计数化的发送端/接收端塞进 `Sender`/`Receiver` 的 `flavor` 字段，返回两端。

`bounded` 的特殊之处在于它**先判断 `cap == 0`**：零容量走 `flavors::zero`，正容量走 `flavors::array`。`unbounded` 则永远走 `flavors::list`。这正是「按容量选 flavor」的分流。

#### 4.1.3 源码精读

先看 `unbounded()`，它选择 `flavors::list`（链表实现的无界队列）：

[src/channel.rs:50-59](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L50-L59) — 创建无界通道：用 `counter::new(flavors::list::Channel::new())` 建出计数化的 list 通道，再分别包成 `Sender`/`Receiver` 返回。

```rust
pub fn unbounded<T>() -> (Sender<T>, Receiver<T>) {
    let (s, r) = counter::new(flavors::list::Channel::new());
    let s = Sender { flavor: SenderFlavor::List(s) };
    let r = Receiver { flavor: ReceiverFlavor::List(r) };
    (s, r)
}
```

再看 `bounded()`，注意开头那个 `if cap == 0` 的分流：

[src/channel.rs:113-133](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L113-L133) — 创建有界通道：`cap == 0` 时用 `flavors::zero`（会合通道），否则用 `flavors::array::Channel::with_capacity(cap)`（预分配数组的有界通道）。

```rust
pub fn bounded<T>(cap: usize) -> (Sender<T>, Receiver<T>) {
    if cap == 0 {
        let (s, r) = counter::new(flavors::zero::Channel::new());
        // ... SenderFlavor::Zero / ReceiverFlavor::Zero
    } else {
        let (s, r) = counter::new(flavors::array::Channel::with_capacity(cap));
        // ... SenderFlavor::Array / ReceiverFlavor::Array
    }
}
```

这里的 `SenderFlavor` / `ReceiverFlavor` 是把「公共类型壳」和「多种底层实现」粘合起来的枚举：

[src/channel.rs:371-380](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L371-L380) — `SenderFlavor` 三种变体，正好对应 `unbounded`/`bounded(>0)`/`bounded(0)` 三条路径。

```rust
enum SenderFlavor<T> {
    Array(counter::Sender<flavors::array::Channel<T>>),  // bounded(cap>0)
    List(counter::Sender<flavors::list::Channel<T>>),    // unbounded
    Zero(counter::Sender<flavors::zero::Channel<T>>),    // bounded(0)
}
```

而六种 flavor 的全貌在 `flavors/mod.rs` 顶部注释里写得很清楚：

[src/flavors/mod.rs:1-17](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/mod.rs#L1-L17) — 声明六种 flavor：`at`/`array`/`list`/`never`/`tick`/`zero`。本讲只用到 `list`、`array`、`zero` 三种。

> 小提示：本讲不展开 flavor 内部。你只要记住这张映射表就够了。

| 构造函数 | 选择的 flavor | 含义 |
| --- | --- | --- |
| `unbounded()` | `flavors::list` | 无界，链表实现 |
| `bounded(cap)`，`cap > 0` | `flavors::array` | 有界，预分配数组 |
| `bounded(0)` | `flavors::zero` | 零容量会合 |

#### 4.1.4 代码实践

**实践目标**：亲手验证「构造函数返回的是一对两端，且两端连着同一条通道」。

**操作步骤**（这是「示例代码」，不是项目原有文件）：

```rust
// 示例代码：把下面存成 src/bin/peek.rs，然后 cargo run --bin peek
use crossbeam_channel::{unbounded, bounded};

fn main() {
    let (s, r) = unbounded::<i32>();
    println!("unbounded -> capacity = {:?}", s.capacity()); // 期望 None

    let (s2, r2) = bounded::<i32>(3);
    println!("bounded(3) -> capacity = {:?}", s2.capacity()); // 期望 Some(3)

    let (s3, _r3) = bounded::<i32>(0);
    println!("bounded(0) -> capacity = {:?}", s3.capacity()); // 期望 Some(0)

    // 两端共享同一条通道：发一条，能从另一端收到
    s.send(42).unwrap();
    assert_eq!(r.recv(), Ok(42));
}
```

**需要观察的现象**：`capacity()` 对无界返回 `None`，对有界返回 `Some(cap)`（包括 `Some(0)`）。

**预期结果**：打印 `None` / `Some(3)` / `Some(0)`，且 `assert_eq!` 通过。

> 是否能本地运行：「待本地验证」——取决于你是否把文件放进 `src/bin/` 并配置好 workspace。逻辑上与库文档示例一致。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `bounded(0)` 不复用 `flavors::array`，而要单独用 `flavors::zero`？

**参考答案**：`array` flavor 是「预分配固定大小数组」的有界缓冲区，至少要能存 1 条消息才有意义；`cap == 0` 时根本没有缓冲位，它的语义是「发送方与接收方直接会合交接」，实现机制完全不同（需要双方互相配对、唤醒），所以单独用 `zero` flavor。

**练习 2**：`unbounded()` 没有参数，它的「容量」是多少？

**参考答案**：没有上限（无界）。`capacity()` 返回 `None` 表示「容量不受限」。

---

### 4.2 Sender 与 Receiver：发送与接收的基本用法

#### 4.2.1 概念说明

拿到两端之后，最常用的两个方法是：

- `Sender::send(msg)`：把 `msg` 发进通道，返回 `Result<(), SendError<T>>`。
- `Receiver::recv()`：从通道取一条消息，返回 `Result<T, RecvError>`。

它们都是**阻塞式**的：

- `send` 在「通道满」时会卡住，直到腾出位置或通道断开。
- `recv` 在「通道空」时会卡住，直到有消息或通道断开。

如果不想阻塞，还有对应的非阻塞版本 `try_send` / `try_recv`，本讲先用它们做现象观察，详细的错误体系留到 u2-l3。

#### 4.2.2 核心流程

以 `send` 为例，它的实现是「按 flavor 分发 + 错误归一化」：

1. `match &self.flavor` 把调用转发给 `Array`/`List`/`Zero` 中对应的那一个。
2. 底层返回的是更通用的 `SendTimeoutError`（因为发送端内部复用了带截止时间的实现）。
3. `send` 把它再映射成对外的 `SendError`：`Disconnected` 保留、`Timeout` 分支理论上走不到（因为 `send` 没有超时），用 `unreachable!()` 标注。

`recv` 同理：分发到底层后，把底层返回的 `RecvTimeoutError` 映射成对外的 `RecvError`。

#### 4.2.3 源码精读

`Sender::send` 的实现：

[src/channel.rs:446-456](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L446-L456) — `send` 按 flavor 分发到底层的 `chan.send(msg, None)`（`None` 表示没有截止时间，即一直阻塞），再把 `SendTimeoutError` 映射为 `SendError`。

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

`Receiver::recv` 的实现，注意末尾的 `.map_err(|_| RecvError)`：

[src/channel.rs:831-857](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L831-L857) — `recv` 同样按 flavor 分发；对 `Array/List/Zero` 调用 `chan.recv(None)`，超时与断开两种失败都被归一化成单个 `RecvError`。

```rust
pub fn recv(&self) -> Result<T, RecvError> {
    match &self.flavor {
        ReceiverFlavor::Array(chan) => chan.recv(None),
        ReceiverFlavor::List(chan) => chan.recv(None),
        ReceiverFlavor::Zero(chan) => chan.recv(None),
        // ... At / Tick / Never 分支（本讲暂不涉及）
    }
    .map_err(|_| RecvError)
}
```

非阻塞版本 `try_recv` 的分发结构完全一样：

[src/channel.rs:778-801](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L778-L801) — `try_recv` 立即返回，要么 `Ok(msg)`，要么 `Err(TryRecvError::Empty)` / `Err(TryRecvError::Disconnected)`。

这种「公共方法 = match flavor 转发」的模式会贯穿整个 `channel.rs`，你会在 `len`/`capacity`/`is_empty`/`is_full` 等几乎所有方法上看到同样的结构。这是本系列讲义反复出现的「架构母题」之一。

#### 4.2.4 代码实践

**实践目标**：用 `lib.rs` 里的官方示例体会 send/recv 的阻塞与立即返回。

阅读并运行 crate 文档里这段「Hello, world」（它就是 `cargo test` 会执行的文档测试）：

[src/lib.rs:7-18](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs#L7-L18) — 用 `unbounded()` 建通道，`send` 投递字符串，`recv` 取回。

```rust
use crossbeam_channel::unbounded;
let (s, r) = unbounded();
s.send("Hello, world!").unwrap();
assert_eq!(r.recv(), Ok("Hello, world!"));
```

**操作步骤**：

1. 在仓库根目录执行 `cargo test --doc`。
2. 观察这段文档测试是否通过。

**需要观察的现象**：因为 `send` 之后立即 `recv`，没有任何阻塞现象——消息已经在缓冲区里。

**预期结果**：文档测试全部通过，无 panic。

#### 4.2.5 小练习与答案

**练习 1**：`send` 返回 `Err(SendError(msg))` 的唯一触发条件是什么？

**参考答案**：通道已断开（所有接收者都被 drop）。注意 `send` 没有「超时」概念，所以它不会因为「等太久」而失败，只会因为「没人接收且通道断开」而失败。

**练习 2**：为什么 `send` 内部映射错误时，对 `SendTimeoutError::Timeout` 用了 `unreachable!()`？

**参考答案**：因为传给底层的截止时间是 `None`（无限等待），底层不可能返回 `Timeout`。这是用类型系统把「不可能发生的状态」在运行期兜底排除。

---

### 4.3 容量与零容量会合语义

#### 4.3.1 概念说明

「容量」决定了通道能暂存多少条还没被消费的消息，进而决定了发送方的阻塞行为：

- **无界（unbounded）**：容量无限。`send` 基本不会因为「满」而阻塞（只可能因断开失败）。适合「生产快、消费慢」但不想丢消息的场景，代价是内存可能被无限占用。
- **有界（bounded, cap > 0）**：容量固定为 `cap`。满了之后 `send` 会阻塞，直到消费者取走一条。天然具备「背压（backpressure）」——强迫生产者慢下来。
- **零容量（bounded(0)）**：容量为 0，不暂存任何消息。发送和接收必须**同时在场**才能完成一次交接，这种「握手即传递」的模式叫**会合（rendezvous）**。

会合通道的一个直观后果：在单线程里直接写 `s.send(x)` 紧跟 `r.recv()` 是行不通的——`send` 会先把当前线程卡死，永远到不了 `recv` 那一行。所以会合通道必须在多线程（或用 `select!`）中使用。

#### 4.3.2 核心流程

把三种通道放进同一张时序图里对比：

```text
无界 bounded(∞):
  线程A: send(1) send(2) send(3) ...   ← 全部立即成功，缓冲区堆积
  线程B:                                recv recv recv ...   ← 慢慢消费

有界 bounded(2):
  线程A: send(1) send(2) send(3)──阻塞──→（等 B 取走一条）
  线程B:                       recv(1)──唤醒 A──→

会合 bounded(0):
  线程A: send(1)──阻塞（等接收方）─────────→ 成功
  线程B:            recv()──配对，A 被唤醒──→ Ok(1)
```

关键点：会合通道里 `send` 与 `recv` 谁先到，谁就先阻塞等待另一方。

#### 4.3.3 源码精读

`lib.rs` 的文档同时给了无界和会合两个对比例子。先看无界：

[src/lib.rs:52-62](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs#L52-L62) — 无界通道连发 1000 条消息也不阻塞。

```rust
let (s, r) = unbounded();
for i in 0..1000 {
    s.send(i).unwrap();
}
```

再看零容量会合的官方示例，它**必须**把 `send` 放到另一个线程里：

[src/lib.rs:67-79](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs#L67-L79) — `bounded(0)` 会合通道：发送在新线程里阻塞等待，主线程 `recv` 出现时双方配对。

```rust
let (s, r) = bounded(0);
thread::spawn(move || s.send("Hi!").unwrap()); // 子线程会卡在这里等
assert_eq!(r.recv(), Ok("Hi!"));                // 主线程接收，配对成功
```

容量相关的查询方法 `capacity()` 也是「按 flavor 分发」，注意它返回 `Option<usize>`：

[src/channel.rs:633-639](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L633-L639) — `Sender::capacity` 对无界返回 `None`，对有界（含 0）返回 `Some(cap)`。

真实项目里也有「会合通道」的实战例子。`examples/fibonacci.rs` 就用 `bounded(0)` 把斐波那契生成器做成「按需生产」的流——消费者取一条，生产者才生成下一条，天然不会堆积内存：

[examples/fibonacci.rs:17-24](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/examples/fibonacci.rs#L17-L24) — 主线程 `bounded(0)` 后 spawn 生成器，用 `r.iter().take(20)` 取前 20 项；每一次 `iter` 内部的 `recv` 都会和生成器的 `send` 会合一次。

```rust
let (s, r) = bounded(0);
thread::spawn(|| fibonacci(s));
for num in r.iter().take(20) {
    println!("{}", num);
}
```

#### 4.3.4 代码实践

**实践目标**：亲手体会「会合通道里 send/recv 必须同时在场」。

**操作步骤**（这是「示例代码」）：

```rust
// 示例代码
use std::thread;
use std::time::Duration;
use crossbeam_channel::bounded;

fn main() {
    let (s, r) = bounded(0);

    // 把发送放到另一个线程，否则 send 会卡死主线程
    let h = thread::spawn(move || {
        println!("子线程：准备 send...");
        s.send("payload").unwrap(); // 会阻塞，直到主线程 recv
        println!("子线程：send 完成");
    });

    thread::sleep(Duration::from_millis(200)); // 故意让发送方先等一会儿
    println!("主线程：准备 recv...");
    let msg = r.recv().unwrap();
    println!("主线程：recv 到 {:?}", msg);

    h.join().unwrap();
}
```

**需要观察的现象**：日志顺序大致是

```text
子线程：准备 send...
主线程：准备 recv...      （主线程睡 200ms 后才来）
子线程：send 完成         （配对成功后被唤醒）
主线程：recv 到 "payload"
```

**预期结果**：程序正常退出，说明 `send` 与 `recv` 完成了会合。

**进阶观察**：把 `thread::spawn(...)` 改成在主线程直接调用 `s.send("payload").unwrap()`（即去掉子线程），编译能过，但**运行时会死锁**——这正是会合通道在单线程里无法使用的原因。你可以试着注释掉子线程版本验证这一点（注意这会让程序挂起，需要手动 Ctrl+C）。

> 是否能本地运行：「待本地验证」——建议在单独的 binary 里运行，避免在 CI 里触发死锁。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `bounded(0)` 的 `is_empty()` 永远是 `true`、`is_full()` 永远是 `true`？

**参考答案**：它的容量是 0，永远存不下任何消息，所以「空」恒成立；同时它的语义是「发送方总得等待接收方」，所以从发送方视角看它「永远是满的」。这两条在 `channel.rs` 里对应方法的文档注释中也明确写出了（参见 `Sender::is_empty`/`is_full` 的 `Note`）。

**练习 2**：如果你的生产者可能瞬间灌入海量数据、而消费者处理较慢，选 `unbounded` 还是 `bounded`？为什么？

**参考答案**：通常选 `bounded` 并设一个合理上限。`unbounded` 不会阻塞生产者，但会把消息全堆在内存里，极端情况下可能 OOM；`bounded` 能形成背压，让生产者主动等待，保护系统不被冲垮。只有当你明确不希望生产者被阻塞、且能接受内存占用时，才用 `unbounded`。

---

## 5. 综合实践

把本讲的三个知识点串起来，完成下面这个贯穿任务。

**任务**：写一个程序，完成两件事。

1. 用 `unbounded()` 发送 `0..1000` 共 1000 个数字，然后在主线程接收并求和，断言总和等于 `0 + 1 + ... + 999 = 499500`。
2. 再用 `bounded(0)` 在两个线程之间传递**一条**消息，体会「发送与接收必须同时出现」的会合阻塞。

**参考实现**（「示例代码」）：

```rust
// 示例代码：src/bin/u1_l2_practice.rs，然后 cargo run --bin u1_l2_practice
use std::thread;
use crossbeam_channel::{unbounded, bounded};

fn main() {
    // 第 1 部分：unbounded 批量发送
    let (s, r) = unbounded();
    for i in 0..1000 {
        s.send(i).unwrap(); // 无界，不会因“满”而阻塞
    }
    drop(s); // 断开发送端，r.iter() 才会结束

    let sum: i64 = r.iter().sum();
    assert_eq!(sum, (0..1000i64).sum()); // 期望 499500
    println!("unbounded 求和 = {}", sum);

    // 第 2 部分：bounded(0) 会合
    let (s0, r0) = bounded(0);
    let h = thread::spawn(move || {
        s0.send("hello-from-thread").unwrap(); // 阻塞至主线程 recv
    });
    let msg = r0.recv().unwrap();
    h.join().unwrap();
    println!("会合收到 = {:?}", msg);
}
```

**完成标准**：

- 程序正常退出，打印 `499500` 和 `hello-from-thread`。
- 你能口述：为什么第 1 部分不需要多线程、而第 2 部分必须有另一个线程？
- 你能在 `src/channel.rs` 里指出 `unbounded`、`bounded(0)`、`bounded(>0)` 分别对应哪个 `SenderFlavor` 变体。

> 第 1 部分里 `drop(s)` 是关键：`r.iter()` 是阻塞迭代器，只有在「通道空且已断开」时才会结束（返回 `None`）。不断开发送端的话 `iter()` 会一直等下去。这背后的 `Iter` 实现见 `src/channel.rs` 中 `Iter::next` 调用 `self.receiver.recv().ok()`，将在 u1-l4 详细讲解。

---

## 6. 本讲小结

- `crossbeam-channel` 的两个基础构造函数是 `unbounded()` 和 `bounded(cap)`，都返回 `(Sender<T>, Receiver<T>)`。
- `bounded` 内部会先判断 `cap == 0`：零容量走 `flavors::zero`，正容量走 `flavors::array`；`unbounded` 走 `flavors::list`。
- `Sender::send` / `Receiver::recv` 是阻塞式 API，源码里是「`match flavor` 转发 + 错误归一化」的统一模式。
- 无界通道不限制容量（`capacity() == None`），`send` 不会因为「满」而阻塞；有界通道提供背压。
- 零容量通道（`bounded(0)`）是会合语义：发送和接收必须同时在场，单线程里直接 `send` 会死锁，必须放到另一个线程或用 `select!`。
- `src/lib.rs` 的文档示例（Hello world、`0..1000`、`bounded(0)`）本身就是可运行的文档测试，是最好的入门练习材料。

---

## 7. 下一步学习建议

- **接着学 API 全貌**：下一讲 [u1-l3](u1-l3-sender-receiver-api.md) 会把 `Sender`/`Receiver` 的全部方法（`try_send`/`send_timeout`/`send_deadline`、`try_recv`/`recv_timeout`/`recv_deadline`，以及 `len`/`capacity`/`is_empty`/`is_full`/`same_channel`）系统讲一遍，重点是「三种阻塞模式」。
- **推荐阅读的源码**：把 `src/channel.rs` 从头扫一遍 `impl<T> Sender<T>` 和 `impl<T> Receiver<T>` 两个块，体会「每个方法都是 match flavor」的母题；再翻 `src/lib.rs` 顶部的 `# Blocking operations` 文档段，对照三种阻塞模式。
- **可选的延伸阅读**：`examples/matching.rs` 展示了如何用 `select!` 在会合通道上「既发送又接收」，可以作为你理解「为什么 bounded(0) 常和 select 配合」的预告——`select!` 的正式讲解在 u2-l9。
