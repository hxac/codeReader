# 使用 Select 动态 API

## 1. 本讲目标

学完本讲后，你应当能够：

- 用 `Select` 在**运行时**动态构建一个通道操作列表（数量可在运行期才确定），并从中选出一个「就绪」的操作执行。
- 区分 `Select` 的两套 API：`select` 家族（`try_select` / `select` / `select_timeout` / `select_deadline`）返回一个**必须完成**的 `SelectedOperation`；`ready` 家族（`try_ready` / `ready` / `ready_timeout` / `ready_deadline`）只返回一个 index、不强制完成。
- 掌握 `SelectedOperation::recv` / `send` 的「完成契约」：拿到它之后**必须**调用 `recv` 或 `send` 把它消费掉，否则 drop 时会 panic。
- 理解 `select!` 宏只是 `Select` API 的「编译期糖」：它最终调用的就是本讲讲的同一套 `internal::select` / `try_select` / `select_timeout`。

本讲只讲 `src/select.rs` 中 `Select` 与 `SelectedOperation` 的**使用层面**与公共流程；`run_select` 的并发算法细节、`SelectHandle` trait 与各 flavor 的对接属于下一讲（u3-l1、u3-l2）。

---

## 2. 前置知识

阅读本讲前，你需要先掌握：

- **通道基本用法**（u1-l2、u1-l3）：知道 `Sender::send` / `Receiver::recv` 以及 `try_send` / `try_recv` 的非阻塞语义。
- **`select!` 宏的使用**（u2-l9）：知道 `select!` 在一个块里组合多个 `recv` / `send`，并理解「就绪」与「公平/有偏」的概念。
- **阻塞与唤醒机制**（u2-l4）：知道 `Context`、`Waker`、`Selected` 状态机。本讲会用到 `Selected::Waiting / Aborted / Disconnected / Operation` 这些术语，但不展开其内部实现。

关键术语补充：

- **就绪（ready）**：一个操作若不需要阻塞即可完成（包括「会立即返回错误，比如通道已断开」），就算就绪。
- **完成（complete）**：把一个已经「开始」的操作真正执行掉（把消息写进通道 / 把消息读出通道）。
- **index**：每注册一个操作，`Select` 就给它分配一个从 0 递增的编号，用来在选定后回查「是哪个通道」。

---

## 3. 本讲源码地图

本讲几乎全部围绕一个文件：

| 文件 | 作用 |
| --- | --- |
| [`src/select.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs) | 定义 `Select`、`SelectedOperation`、`SelectHandle` trait，以及 `run_select` / `run_ready` 核心流程和供宏使用的自由函数 `select` / `try_select` / `select_timeout` |

仅在「完成操作」一节会点到：

| 文件 | 作用 |
| --- | --- |
| [`src/channel.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs) | 提供 `channel::read` / `channel::write`（完成操作时搬运消息）、`Sender::addr` / `Receiver::addr`（校验端身份）、以及 `Sender` / `Receiver` 的 `SelectHandle` 实现 |
| [`src/lib.rs`](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs) | `pub use` 导出 `Select` / `SelectedOperation`；`#[doc(hidden)] pub mod internal` 暴露给 `select!` 宏的后门 |

---

## 4. 核心概念与源码讲解

### 4.1 Select 容器：运行时构建操作列表

#### 4.1.1 概念说明

`select!` 宏很好用，但它有一个硬限制：分支数量在**编译期**就固定了。如果你的通道数量要到运行时才知道（比如一个 fan-in 合流器，接收端来自一个 `Vec<Receiver<T>>`），`select!` 就无能为力。

`Select` 就是为这个场景设计的：它是一个**可变的操作列表**，你在运行时往里 `send` / `recv` 注册任意多个操作，每注册一个拿回一个 `index`，最后从里面选一个就绪的操作来执行。文档原话：

> The [`select!`] macro is a convenience wrapper around `Select`. However, it cannot select over a dynamically created list of channel operations.

换句话说，`Select` 是「底层能力」，`select!` 是「编译期语法糖」。理解了 `Select`，就理解了 `select!` 的本质。

#### 4.1.2 核心流程

使用 `Select` 的标准四步走：

```text
1. Select::new()                       —— 创建空的操作列表
2. sel.recv(&r) / sel.send(&s)         —— 注册操作，每个返回一个 index
3. sel.select() / try_select() / ...   —— 选定一个就绪操作，拿到 SelectedOperation
4. op.recv(&r) / op.send(&s, msg)      —— 用对应的通道「完成」这个操作
```

`Select` 内部持有一个三元组向量，每个注册的操作对应一项：

```text
handles: Vec<(&dyn SelectHandle, usize index, usize addr)>
                  │              │              │
                  │              │              └── 通道端地址（用于完成时校验身份）
                  │              └── 注册时分配的编号（回查「是哪个通道」）
                  └── 该操作的「执行句柄」（转发到具体 flavor）
```

`index` 由内部的 `next_index` 单调递增分配；`addr` 取自通道端的 `addr()`（指针地址）。这两个值是步骤 3→4 之间「把选定结果对应回某个具体通道」的桥梁。

#### 4.1.3 源码精读

先看 `Select` 结构体本身，它就三个字段：[src/select.rs:616-625](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L616-L625)

```rust
pub struct Select<'a> {
    /// A list of senders and receivers participating in selection.
    handles: Vec<(&'a dyn SelectHandle, usize, usize)>,
    /// The next index to assign to an operation.
    next_index: usize,
    /// Whether to use the index of handles as bias for selecting ready operations.
    biased: bool,
}
```

注意生命周期 `'a`：注册进来的 `&Sender` / `&Receiver` 借用必须活到 select 结束，所以 `Select<'a>` 持有引用而非所有权。

`new()` 预分配容量为 4 的向量，默认无偏（`biased = false`）：[src/select.rs:643-649](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L643-L649)

```rust
pub fn new() -> Self {
    Self {
        handles: Vec::with_capacity(4),
        next_index: 0,
        biased: false,
    }
}
```

`new_biased()` 把 `biased` 置 `true`，对应 `select_biased!` 宏——多个操作同时就绪时，选 index 最小（注册最早）的那个，而不是随机选：[src/select.rs:665-670](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L665-L670)

注册一个发送/接收操作，逻辑完全对称：取当前 `next_index` 作 index、取通道地址作 addr、把句柄塞进向量、计数器 +1，然后返回 index：[src/select.rs:686-714](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L686-L714)

```rust
pub fn send<T>(&mut self, s: &'a Sender<T>) -> usize {
    let i = self.next_index;
    let addr = s.addr();
    self.handles.push((s, i, addr));
    self.next_index += 1;
    i
}

pub fn recv<T>(&mut self, r: &'a Receiver<T>) -> usize {
    let i = self.next_index;
    let addr = r.addr();
    self.handles.push((r, i, addr));
    self.next_index += 1;
    i
}
```

> 这里出现的 `s.addr()` / `r.addr()` 在 [src/channel.rs:665-671](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L665-L671) 与 [src/channel.rs:1172-1181](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1172-L1181)，本质是把底层 flavor 的指针地址取出来当唯一身份。

还有个注册后能「反悔」的方法 `remove`：当某通道断开后，你可以把它从列表里摘掉、下次不再 select 它。注意它用 `swap_remove`（O(1) 但会打乱顺序），且 `next_index` **不会回退**——被删掉的 index 不会被复用，文档明确写了这一点：[src/select.rs:752-769](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L752-L769)

#### 4.1.4 代码实践

**实践目标**：体会「运行时构建操作列表」这件事——通道数量由命令行参数决定。

**操作步骤**：

1. 在 `examples/` 下新建一个二进制（示例代码，不是项目原有代码）：

```rust
// 示例代码：examples/fanin_index.rs
use crossbeam_channel::{unbounded, Select};

fn main() {
    // 通道数量在运行时才确定（这里用固定值代替命令行参数）
    let n: usize = 3;
    let mut senders = Vec::new();
    let mut receivers = Vec::new();
    for i in 0..n {
        let (s, r) = unbounded();
        s.send(format!("来自通道 {i} 的消息")).unwrap();
        senders.push(s);
        receivers.push(r);
    }

    // 1. 创建空列表
    let mut sel = Select::new();
    // 2. 动态注册：每次 recv 返回一个 index
    let indices: Vec<usize> = receivers.iter().map(|r| sel.recv(r)).collect();

    // 3. 选定一个就绪操作（此时所有通道都有消息，都会就绪）
    let op = sel.select();
    // 4. 根据 index 找回是哪个通道，并完成操作
    let idx = op.index();
    let msg = op.recv(&receivers[idx]).unwrap();
    println!("index={idx}, 对应注册序号={}, 收到: {msg}", indices[idx]);
}
```

2. 运行：`cargo run --example fanin_index`（需先把该文件加入仓库；若只想阅读，可直接对照 `tests/select.rs` 中已有的同类用法）。

**需要观察的现象**：多次运行，打印的 `idx` 可能不同——因为三个通道同时就绪，`select()` 会**随机**选一个（无偏模式）。这正好对应 u2-l9 讲过的「公平性」。

**预期结果**：每次运行随机打印 0/1/2 中的一个，且消息内容与 index 对应正确。

> 项目自带的 `tests/select.rs` 第 28-35 行就是几乎一模一样的「多个 recv + sel.select() + 按 index 分发」模式，可直接阅读对照。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Select` 要存 `addr`，而不是只存 `index` 就够了？

> **答案**：`index` 是 `Select` 自己分配的编号，但步骤 4 完成操作时，用户传入的是一个具体的 `&Receiver` / `&Sender` 引用。`Select` 需要确认「用户传进来的端，正是当初注册的那个端」，防止用户传错端去完成别人的操作。这个校验靠比对 `addr`（指针地址）实现（见 4.3 节）。

**练习 2**：`new_biased()` 与 `new()` 在字段上的唯一区别是什么？它如何影响多个操作同时就绪时的选择？

> **答案**：唯一区别是 `biased` 字段：`new()` 为 `false`，`new_biased()` 为 `true`。`biased = true` 时跳过 `run_select` 开头的 `shuffle`（见 4.2 节），改为按 `handles` 的顺序优先选 index 小的（注册早的）操作；`biased = false` 时先打乱顺序，从而在同时就绪的操作中均匀随机选择。

---

### 4.2 选定一个操作：四种调用与 run_select 流程

#### 4.2.1 概念说明

注册完操作后，怎么「选」出一个？`Select` 提供了两类共八个方法，本节讲 `select` 家族的四个——它们与单条收发 API 的三种阻塞模式一一对应：

| 方法 | 阻塞模式 | 对应单条 API | 失败时返回 |
| --- | --- | --- | --- |
| `try_select()` | 非阻塞 | `try_recv` / `try_send` | `TrySelectError` |
| `select()` | 无限阻塞 | `recv` / `send` | 不会失败（空列表时 panic） |
| `select_timeout(dur)` | 限时 | `recv_timeout` / `send_timeout` | `SelectTimeoutError` |
| `select_deadline(t)` | 限截止时刻 | `recv_deadline` / `send_deadline` | `SelectTimeoutError` |

和 u1-l3 讲过的母题完全一致：**底层只有「带截止时间」一种原语**，`select` = 永不超时、`try_select` = 立刻超时、`*_timeout` = 把 `Duration` 换算成 `Instant` 后委托给截止时刻版本。

成功时，这四个方法都返回 `SelectedOperation<'a>`——一个「已经抢占成功、但还没搬运数据」的半成品操作，必须接着完成它（4.3 节）。

#### 4.2.2 核心流程

四个 `Select` 方法都只是薄包装，真正干活的是自由函数 `run_select`，它对每个操作走一遍下面这个流程（`Timeout` 三态：`Now` / `Never` / `At(Instant)`）：[src/select.rs:160-170](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L160-L170)

```text
run_select(handles, timeout, is_biased):
  1. 若 handles 为空：按 timeout 直接睡到超时（Now 立即返回 None）
  2. 若非 biased：shuffle(handles)            ← 公平性：打乱尝试顺序
  3. fast path：逐个 handle.try_select(token)
        └─ 任一成功 → 立即返回 (token, index, addr)
  4. 否则进入阻塞循环：
     a. register 所有操作（期间若某操作变就绪则中止注册）
     b. 计算最早截止时刻 deadline（取 timeout 与各 handle.deadline 的最小值）
     c. cx.wait_until(deadline)             ← 线程 park，被唤醒后容错复查
     d. unregister 已注册的操作
     e. 根据唤醒后的 Selected 状态：
          - Operation(op) → 找到对应 handle，accept(token) 确认抢占
          - Aborted       → 有操作在注册期间变就绪，回 fast path 重试
          - Disconnected  → 通道断开也算就绪，回 fast path 重试
     f. 若本轮没选中 → 回到第 3 步重试 fast path
```

关键直觉：「抢占（select）」和「搬运（read/write）」被故意拆成两步，是为了让 select 算法能在多个操作之间公平仲裁——它先靠 CAS 在某个操作的内部状态里「占座」（写入一个 `Token`），占座成功后再由调用者用这个 `Token` 去把数据真正搬走。`Token` 就是占座凭证，它随后会被 `SelectedOperation` 带给 4.3 节的 `recv` / `send`。

公平性的数学含义：当 `n` 个操作同时就绪时，非 biased 模式下 `shuffle` 把它们排成一个均匀随机排列，于是被选中的是排列中第一个就绪者，每个操作被选中的概率为

\[
P(\text{某操作被选中}) = \frac{1}{n}
\]

biased 模式跳过 shuffle，总是选就绪者中 index 最小的，概率不再均匀。

#### 4.2.3 源码精读

四个 `Select` 方法各自委托给同名自由函数，传入 `self.biased`：[src/select.rs:809-811](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L809-L811)、[src/select.rs:860-862](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L860-L862)、[src/select.rs:911-916](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L911-L916)、[src/select.rs:967-972](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L967-L972)。例如：

```rust
pub fn try_select(&mut self) -> Result<SelectedOperation<'a>, TrySelectError> {
    try_select(&mut self.handles, self.biased)
}
pub fn select(&mut self) -> SelectedOperation<'a> {
    select(&mut self.handles, self.biased)
}
```

三个自由函数把 `Timeout` 三态映射到 `run_select`：[src/select.rs:455-489](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L455-L489)

```rust
pub fn try_select<'a>(handles, is_biased) -> Result<SelectedOperation<'a>, TrySelectError> {
    match run_select(handles, Timeout::Now, is_biased) {      // 立刻超时
        None => Err(TrySelectError),
        Some((token, index, addr)) => Ok(SelectedOperation { token, index, addr, .. }),
    }
}

pub fn select<'a>(handles, is_biased) -> SelectedOperation<'a> {
    if handles.is_empty() {
        panic!("no operations have been added to `Select`");
    }
    let (token, index, addr) = run_select(handles, Timeout::Never, is_biased).unwrap(); // 永不超时
    SelectedOperation { token, index, addr, .. }
}
```

`select_timeout` 用 `Instant::now().checked_add(timeout)` 把 `Duration` 换算成截止时刻，**溢出时退化为永不超时**的 `select`（与 u1-l3 讲的 `recv_timeout` 溢出处理一致）：[src/select.rs:493-521](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L493-L521)

```rust
pub fn select_timeout<'a>(handles, timeout: Duration, is_biased)
    -> Result<SelectedOperation<'a>, SelectTimeoutError>
{
    match Instant::now().checked_add(timeout) {
        Some(deadline) => select_deadline(handles, deadline, is_biased),
        None => Ok(select(handles, is_biased)),   // 溢出 → 当作永不超时
    }
}
```

`run_select` 本体较长，这里只看它的骨架（fast path + 阻塞循环）：[src/select.rs:176-211](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L176-L211)

```rust
fn run_select(handles: &mut [(&dyn SelectHandle, usize, usize)],
              timeout: Timeout, is_biased: bool) -> Option<(Token, usize, usize)> {
    if handles.is_empty() { /* 按 timeout 睡/返回 */ }
    if !is_biased { utils::shuffle(handles); }          // 公平性
    let mut token = Token::default();

    // fast path：不阻塞地试一遍
    for &(handle, i, addr) in handles.iter() {
        if handle.try_select(&mut token) {
            return Some((token, i, addr));
        }
    }
    loop {
        // 阻塞循环：register → wait_until → unregister → accept（见 4.2.2 流程图）
        ...
    }
}
```

`run_select` 返回的 `Option<(Token, usize, usize)>` 三元组，正是构造 `SelectedOperation` 所需的三样东西（占座凭证、index、addr）。

#### 4.2.4 代码实践

**实践目标**：体会「三个操作都阻塞」时 `select()` 的等待与唤醒。

**操作步骤**：

1. 阅读并运行 `tests/select.rs` 中的 `select` / `select_timeout` 相关测试（项目已有，例如第 80-105 行那段：一个线程延迟发消息，主线程用 `sel.select_timeout(ms(1000))` 等待）。
2. 自己写一个最小版（示例代码）：两个空 `unbounded` 通道，另起一个线程睡 200ms 后向其中一个发消息，主线程 `sel.select()` 阻塞等待，打印收到消息和 index。

**需要观察的现象**：主线程会**阻塞**约 200ms，直到子线程发出消息被唤醒；唤醒后 `op.index()` 告诉你是哪个通道就绪了。

**预期结果**：约 200ms 后打印出对应通道的消息；若把发送线程的 `sleep` 改得比 `select_timeout` 的超时长，则会观察到 `Err(SelectTimeoutError)`。

#### 4.2.5 小练习与答案

**练习 1**：`select()` 在什么情况下会 panic？`try_select()` 会吗？

> **答案**：`select()` 在 `handles` 为空（没注册任何操作）时 panic，对应自由函数 `select` 里的 `panic!("no operations have been added to `Select`")`（[src/select.rs:478-480](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L478-L480)）。`try_select()` 不会 panic——空列表时直接返回 `Err(TrySelectError)`，因为它走 `Timeout::Now` 分支。

**练习 2**：为什么 `run_select` 要在阻塞循环末尾、被唤醒之后，再跑一遍 fast path（`try_select`）？

> **答案**：因为唤醒可能来自多种原因（虚假唤醒、别的线程抢先选中、断开通知等）。被唤醒后必须**重新自检**所有操作是否真的能 `try_select` 成功，避免错过「在 unregister 期间刚变就绪」的操作，也避免在被别人抢先的情况下空转返回。这是无锁选择算法里典型的「乐观重试」。

---

### 4.3 完成操作：SelectedOperation 与「必须完成」契约

#### 4.3.1 概念说明

`select` 家族返回的 `SelectedOperation` 是一个**已经抢占成功、但尚未搬运数据**的半成品。它内部带着 `run_select` 写入的 `Token`（占座凭证）。你必须调用它的 `recv(&r)` 或 `send(&s, msg)` 来真正完成这次收发。

这条「必须完成」的契约是强制的：**如果让一个 `SelectedOperation` 被 drop 而没有完成，程序会 panic**。文档原文：

> Forgetting to complete the operation is an error and might lead to deadlocks. If a `SelectedOperation` is dropped without completion, a panic occurs.

为什么要这么严格？因为 select 算法已经在底层「占了座」（比如在环形队列里预留了一个槽位、或在会合通道里配好了一次交接）。如果你不完成，这个占座可能永远卡着，导致别的线程死锁。所以库用 panic 这种「显式失败」逼你走完流程。

#### 4.3.2 核心流程

完成一个接收操作的标准写法：

```text
op = sel.select()        // 抢占成功，拿到 SelectedOperation
idx = op.index()         // 查：是第几个注册的操作
msg = op.recv(&r[idx])   // 完成：用对应通道把消息读出来（op 被 consume，不会再 drop）
```

`op.recv(self, ...)` 的内部流程：

```text
1. assert r.addr() == self.addr        ← 校验：传入的端必须是当初注册的那个
2. channel::read(r, &mut self.token)   ← 用占座凭证把数据真正读出（unsafe，分发给 flavor）
3. mem::forget(self)                   ← 阻止 Drop 跑（操作已完成，不能再 panic）
4. 把 Result 映射成 Result<T, RecvError>
```

`op.send(self, s, msg)` 完全对称，调 `channel::write`，失败时返回携带原消息的 `SendError(msg)`。

#### 4.3.3 源码精读

`SelectedOperation` 携带三样东西（占座凭证 `token`、`index`、`addr`）加一个生命周期标记：[src/select.rs:1208-1221](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L1208-L1221)

```rust
#[must_use]
pub struct SelectedOperation<'a> {
    token: Token,            // 占座凭证，run_select 写入、read/write 消费
    index: usize,            // 选中的操作编号
    addr: usize,             // 选中端的地址（完成时校验身份用）
    _marker: PhantomData<&'a ()>,
}
```

`index()` 就是字段直返：[src/select.rs:1248-1250](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L1248-L1250)

完成接收的 `recv`：先断言地址匹配，再调 `channel::read` 搬运，最后 `mem::forget(self)` 阻止 Drop：[src/select.rs:1310-1318](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L1310-L1318)

```rust
pub fn recv<T>(mut self, r: &Receiver<T>) -> Result<T, RecvError> {
    assert!(r.addr() == self.addr, "passed a receiver that wasn't selected");
    let res = unsafe { channel::read(r, &mut self.token) };
    mem::forget(self);          // 已完成，不要让 Drop 再 panic
    res.map_err(|_| RecvError)
}
```

`send` 对称：[src/select.rs:1276-1284](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L1276-L1284)。注意它接收 `msg: T`，失败时返回 `SendError(msg)` 把消息原样还给你。

「不完成就 panic」的强制力来自这个 `Drop` 实现：[src/select.rs:1327-1331](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L1327-L1331)

```rust
impl Drop for SelectedOperation<'_> {
    fn drop(&mut self) {
        panic!("dropped `SelectedOperation` without completing the operation");
    }
}
```

正因为有这个 `Drop`，正常的 `recv` / `send` 才必须用 `mem::forget(self)` 把自己「吃掉」，否则完成之后 Drop 还会触发 panic。

搬运消息的 `channel::read` / `channel::write` 是 `unsafe` 的内部函数，按 flavor 分发：[src/channel.rs:1539-1564](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1539-L1564)。它们之所以 `unsafe`，是因为正确性依赖于「调用者已经通过 select 拿到了合法的 `Token`」这个前提——这正是 `SelectedOperation` 把 `Token` 藏在私有字段、只通过 `recv` / `send` 暴露的原因。

#### 4.3.4 代码实践

**实践目标**：写一个完整的「动态 fan-in 合流器」——从运行时才知道数量的多个 `Receiver` 中持续接收，正确处理通道断开，并体现「必须完成 SelectedOperation」。

**操作步骤**：

1. 编写示例代码（非项目原有）：

```rust
// 示例代码：examples/fanin_loop.rs
use crossbeam_channel::{unbounded, Select};
use std::thread;

fn main() {
    let n = 3;
    let mut senders = Vec::new();
    let mut receivers = Vec::new();
    for i in 0..n {
        let (s, r) = unbounded();
        senders.push(s);
        receivers.push((i, r));
    }

    // 每个生产者发几条就退出
    for (i, s) in senders.iter().enumerate() {
        for k in 0..3 {
            s.send((i, k)).unwrap();
        }
    }
    drop(senders); // 全部发送完毕并断开

    // 用 Select 循环接收，直到所有通道都断开且排空
    while !receivers.is_empty() {
        let mut sel = Select::new();
        // 记录 sel 中的 index → receivers 向量下标的映射
        let mut slots: Vec<usize> = Vec::new();
        for (vec_idx, (_, r)) in receivers.iter().enumerate() {
            let sel_idx = sel.recv(r);
            slots.push(sel_idx); // sel_idx == vec_idx（注册顺序一致）
        }

        let op = sel.select();          // 抢占一个就绪操作
        let vec_idx = op.index();       // 就是注册时的 index，对应 receivers 下标
        let (_, r) = &receivers[vec_idx];

        // 必须完成 SelectedOperation：用对应通道读出消息
        match op.recv(r) {
            Ok(msg) => println!("收到 {msg:?}"),
            Err(_) => {
                // 该通道断开且排空：从列表摘掉，避免下次再 select 它
                println!("通道 {} 断开", receivers[vec_idx].0);
                receivers.remove(vec_idx);
            }
        }
    }
    println!("全部完成");
}
```

2. 运行 `cargo run --example fanin_loop`。

**需要观察的现象**：会打印出 9 条 `(i, k)` 消息（3 个通道 × 3 条），随后每个通道报「断开」并被摘除，最后打印「全部完成」。注意循环里**每次都新建 `Select`**——这是最简单稳健的写法；若要复用，可在断开时用 `sel.remove(vec_idx)` 摘除该操作（见 4.1.3）。

**预期结果**：程序正常退出，不 panic。**关键检查点**：`op.recv(r)` 那一行绝对不能省略或替换成 `let _ = op;`——一旦你拿到了 `op` 却不调用 `recv`/`send`，它 drop 时就会 panic（见上面的 `Drop` 实现）。

#### 4.3.5 小练习与答案

**练习 1**：假如把 4.3.4 示例里的 `match op.recv(r) { ... }` 改成 `let _ = op;`（拿到 op 后什么都不做），会发生什么？为什么？

> **答案**：程序会 panic，报 `dropped `SelectedOperation` without completing the operation`。因为 `op` 被赋值给 `_` 后立即 drop，触发了 `SelectedOperation::drop` 里的 panic。这也说明：拿到 `SelectedOperation` 后，唯一安全的出路就是调用 `recv` 或 `send`（它们内部 `mem::forget(self)` 阻止了 Drop）。

**练习 2**：`op.recv(&r)` 里的断言 `r.addr() == self.addr` 是为了防什么？传错端会发生什么？

> **答案**：防止你用一个**不是当初注册的** `Receiver` 去完成操作（比如注册了 r1 却用 r2 去完成）。`addr` 是通道端的指针地址，相当于身份证。传错端时断言失败、直接 panic（`passed a receiver that wasn't selected`）。更关键的是，`channel::read` 依赖 `Token` 与端匹配才能正确搬运数据，传错端会破坏 unsafe 的前提。

---

### 4.4 就绪模式：ready 家族（不强制完成）

#### 4.4.1 概念说明

`Select` 还有另一套 API——`ready` 家族：`try_ready` / `ready` / `ready_timeout` / `ready_deadline`。它们和 `select` 家族一一对应，但行为不同：

| | `select` 家族 | `ready` 家族 |
| --- | --- | --- |
| 返回值 | `SelectedOperation`（带 Token） | `usize`（仅 index） |
| 是否开始操作 | **是**，已抢占，必须完成 | **否**，只报告「谁就绪了」 |
| 不处理后果 | drop 会 **panic** | 无任何强制，可直接丢弃 index |
| 取数据方式 | `op.recv(&r)` | 自己再调 `r.try_recv()` |

`ready` 像一个「就绪通知器」：它告诉你「现在 r2 大概可以收了」，但**不替你收**，你需要自己用 `try_recv` 去收。好处是你不必受「必须完成」的约束；代价是它**可能虚假唤醒（spurious）**——通知你「就绪」之后、等你真的去 `try_recv` 时，消息可能已经被别的线程抢走，于是收到 `Empty`。所以文档反复强调：

> these methods might return with success spuriously, so it's a good idea to always double check if the operation is really ready.

#### 4.4.2 核心流程

`ready` 家族走的是另一条核心函数 `run_ready`，它和 `run_select` 结构相似，但用 `is_ready` / `watch` / `unwatch` 而非 `try_select` / `register` / `accept`，**完全不碰 `Token`**：

```text
run_ready(handles, timeout, is_biased):
  loop {
    1. 自旋：逐个 handle.is_ready()，谁就绪返回谁的 index
    2. 检查 timeout（Now/At 到点就返回 None）
    3. watch 所有操作（注册「就绪通知」）
    4. cx.wait_until(deadline) 阻塞
    5. unwatch 已注册的操作
    6. 根据唤醒后的 Selected.Operation 找到对应 index 返回
  }
```

因为没有 `Token`、没有「占座」，所以拿到的只是 index，后续 `try_recv` 可能失败，需要 `loop` + 重试。

#### 4.4.3 源码精读

四个 `ready` 方法同样按三种阻塞模式映射到 `run_ready`：[src/select.rs:1009-1014](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L1009-L1014)（`try_ready`）、[src/select.rs:1062-1068](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L1062-L1068)（`ready`）、[src/select.rs:1114-1119](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L1114-L1119)（`ready_timeout`）、[src/select.rs:1167-1172](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L1167-L1172)（`ready_deadline`）。例如 `ready`：

```rust
pub fn ready(&mut self) -> usize {
    if self.handles.is_empty() {
        panic!("no operations have been added to `Select`");
    }
    run_ready(&mut self.handles, Timeout::Never, self.biased).unwrap()
}
```

`run_ready` 的自旋阶段——逐个查 `is_ready`，谁就绪立刻返回其 index：[src/select.rs:352-367](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L352-L367)

```rust
loop {
    let backoff = Backoff::new();
    loop {
        for &(handle, i, _) in handles.iter() {
            if handle.is_ready() {
                return Some(i);          // 只返回 index，不带 Token
            }
        }
        if backoff.is_completed() { break; } else { backoff.snooze(); }
    }
    // ... timeout 检查、watch、wait_until、unwatch ...
}
```

对比 `run_select` 的 fast path 用的是 `handle.try_select(&mut token)`（占座 + 写 Token），这里只问 `is_ready()`（只读判断），这就是「开始操作」与「只报告就绪」的分水岭。

#### 4.4.4 代码实践

**实践目标**：用 `ready` 写一个「带重试」的接收，体会虚假唤醒的处理。

**操作步骤**：项目文档注释里就给了一段标准范式（`src/select.rs` 第 580-608 行的 doc 示例），核心是 `loop { let index = sel.ready(); let res = r[index].try_recv(); if 空 continue; ... }`。自己复现这段逻辑（示例代码）：

```rust
// 示例代码
use crossbeam_channel::{unbounded, Select, RecvError};

fn recv_multiple<T>(rs: &[crossbeam_channel::Receiver<T>]) -> Result<T, RecvError>
where T: Clone + Send + 'static {} // 仅示意，省略具体类型约束
```

由于 `ready` 需要泛型写法较繁琐，建议直接阅读 `src/select.rs` 第 580-608 行库自带的 `recv_multiple` 文档示例，它正是「`ready` + `try_recv` + 空 continue 重试」的权威范式。

**需要观察的现象**：在并发场景下，`try_recv` 偶尔会返回 `Empty`（消息被别人抢走），此时 `continue` 回去重新 `ready`，这就是「双重检查」。

**预期结果**：最终能稳定收到消息；不会因为虚假唤醒而漏收。

#### 4.4.5 小练习与答案

**练习 1**：什么场景下你会优先选 `ready` 家族而不是 `select` 家族？

> **答案**：当你**不想被「必须完成」束缚**、或想自己掌控真正的收发调用时。比如你想在「收到就绪通知」后再决定要不要真的收（条件不满足就放弃），`ready` 只给 index、不占座，放弃也没有副作用。代价是要自己处理虚假唤醒和重试。

**练习 2**：`run_ready` 和 `run_select` 都先做一遍 `shuffle`（非 biased 时）和一次「不阻塞的自检」，但自检用的方法不同。分别是哪两个方法？这个差异意味着什么？

> **答案**：`run_select` 用 `handle.try_select(&mut token)`——会**占座并写 Token**，成功就意味着操作已开始、后续必须完成；`run_ready` 用 `handle.is_ready()`——只是**只读判断**是否就绪，不占座、不产生 Token，所以返回 index 后你仍可能 `try_recv` 失败。这个差异是两套 API「强制完成 vs 自由重试」的根本来源。

---

## 5. 综合实践

把本讲的「动态注册 + select + 必须完成 + 断开处理」串起来，完成一个**带超时的多源合流器**。

**任务**：维护一个 `Vec<Receiver<String>>`（数量运行时决定，模拟多个日志源）。在一个循环里：

1. 用 `Select::new()` 注册所有 receiver。
2. 用 `select_timeout(Duration::from_millis(500))` 等待，最多等 500ms。
3. 成功时用 `op.index()` 找回通道，调 `op.recv()` 完成操作并打印日志；若 `recv` 返回 `Err`（通道断开），用 `receivers.remove(idx)` 摘掉该源。
4. 超时（`Err(SelectTimeoutError)`）时打印「心跳：暂无日志」并继续。
5. 当 `receivers` 为空时退出循环。

**进阶**：把其中一个源换成 `tick(Duration::from_secs(1))`（u2-l8 讲过的周期通道），用同一个 `Select` 同时 select 普通消息源和心跳源，观察 `op.index()` 如何区分二者。

**验收标准**：

- 程序运行不 panic（关键是每个 `SelectedOperation` 都被 `recv` 完成了）。
- 通道全部断开后程序能正常退出。
- 超时分支能被触发（把所有发送线程 sleep 调长即可观察）。

> 这个任务综合了 4.1（动态注册）、4.2（`select_timeout` 的限时模式）、4.3（完成契约与断开处理）。完成后，你应当能体会到：`select!` 宏能做的事，`Select` API 都能做，而且还能处理「通道数量运行时变化」这种宏做不到的场景。

---

## 6. 本讲小结

- `Select` 是 `select!` 宏的底层：它在**运行时**动态构建操作列表，弥补了宏「分支数编译期固定」的限制。
- 注册用 `sel.recv(&r)` / `sel.send(&s)`，每个返回一个递增的 `index`；`Select` 内部存 `(&dyn SelectHandle, index, addr)` 三元组，`addr` 用于完成时校验端身份。
- 选定操作有 `select` 家族四个方法（`try_select` / `select` / `select_timeout` / `select_deadline`），对应非阻塞/阻塞/限时/限截止四种模式，底层统一走 `run_select`（`Timeout::Now/Never/At` 三态），公平性靠非 biased 时的 `shuffle`。
- `select` 家族返回 `SelectedOperation`——一个**已抢占、必须完成**的半成品：必须调 `op.recv(&r)` 或 `op.send(&s, msg)` 把它消费掉，否则 drop 时 panic；完成时靠 `mem::forget(self)` 阻止 Drop。
- `ready` 家族（`try_ready` / `ready` / `ready_timeout` / `ready_deadline`）只返回 index、不占座、不强制完成，走 `run_ready`（`is_ready` / `watch` / `unwatch`），代价是可能虚假唤醒、需自己 `try_recv` 重试。
- `select!` 宏展开后调用的就是 `internal` 模块里的 `select` / `try_select` / `select_timeout`（[src/lib.rs:369-375](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs#L369-L375)），与 `Select` 方法是同一套自由函数——宏确实是 `Select` API 的「编译期糖」。

---

## 7. 下一步学习建议

- **u3-l1 select 核心算法 run_select / run_ready**：本讲把 `run_select` 当黑盒用了，下一讲会逐行拆解它的并发状态机（`Selected::Waiting/Aborted/Disconnected/Operation`、`register`/`accept`/`wait_until` 如何协作防丢失唤醒）。
- **u3-l2 SelectHandle trait 与 flavor 对接**：本讲里 `handle.try_select` / `is_ready` / `register` / `accept` 这些方法来自 `SelectHandle` trait，下一讲讲每种 flavor（array/list/zero）如何实现它，以及 `channel::read` / `channel::write` 如何配合 `Token` 完成搬运。
- **u3-l3 select! 宏展开机制**：想知道 `select!` 到底怎么编译成对 `internal::select` 的调用、单分支如何优化为 `recv()`，就继续读宏的内部分析。
- **延伸阅读**：`tests/select.rs` 与 `tests/select_macro.rs` 覆盖了几乎所有边界场景（断开、超时、空 select、单操作优化），是验证你理解的最佳参照。
