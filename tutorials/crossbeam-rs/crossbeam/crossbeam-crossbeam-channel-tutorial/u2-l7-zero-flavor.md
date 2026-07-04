# 零容量会合：zero flavor

## 1. 本讲目标

本讲深入 `src/flavors/zero.rs`，拆解 `bounded(0)` 通道的底层实现。学完后你应该能够：

- 说清楚「会合（rendezvous）」通道为什么不缓冲任何消息，却能可靠地把一条消息从发送方「递」到接收方。
- 画出一次阻塞 `send` 与一次 `recv` 配对的完整时序：谁注册 packet、谁配对、谁置 `ready`、谁自旋等待。
- 解释 `Packet` 为什么有「栈上」和「堆上」两种分配路径，以及为什么普通 `send`/`recv` 用栈 packet、而 `select!`/`Select` 路径必须用堆 packet。
- 看懂 `Waker::try_select` 如何在「别的线程」的 entry 上完成 CAS 选中、递包、唤醒三件事。

本讲只覆盖 `zero.rs` 一个 flavor，不涉及 array/list（已在 u2-l5/u2-l6 讲过），也不展开 select 算法本身（那是 u3-l1 的内容）。

## 2. 前置知识

本讲建立在 u2-l4（阻塞与唤醒机制）之上，以下概念默认你已经掌握，这里只做一句话回顾：

- **会合（rendezvous）**：通道容量为 0，不存任何消息。发送和接收必须「同时在场」才能完成一次交接，就像两个人必须在同一时刻握手。单线程里直接 `s.send(x)` 会永远阻塞（死锁），因为没有接收方来配对。
- **`Context`**：线程本地的「被阻塞者」状态，持有一个 `Selected` 状态机（`Waiting/Aborted/Disconnected/Operation(n)`）、一个 `packet` 指针槽和线程句柄。线程通过 `Context::with` 复用线程本地缓存。
- **`Waker`**：「阻塞者队列」，存放 `Entry { oper, packet, cx }`。`try_select` 负责挑一个属于别的线程的 entry，在它的 `cx` 上 CAS 标记选中，再 `unpark` 唤醒它。
- **`Mutex`**：crossbeam-channel 在 `utils.rs` 里提供的「非毒」`Mutex`（panic 不会毒化锁），就是 `std::sync::Mutex` 的薄包装。zero flavor 用它保护整个内部状态。

一个关键区别先记在前面：**array/list 用 `SyncWaker`（自带 `Mutex<Waker> + AtomicBool` 快速路径），而 zero 直接用裸 `Waker`**。原因是 zero 把整个 `Inner`（含两个 `Waker`）放在一把 `Mutex` 里，访问已经被锁串行化，就不必再为每个 `Waker` 单独加锁了。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/flavors/zero.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs) | 零容量通道的全部实现：`Packet`、`Inner`、`Channel`、收发方法、`SelectHandle` 实现。本讲主角。 |
| [src/waker.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs) | `Waker`/`Entry` 定义，`try_select`/`can_select`/`disconnect` 等配对与广播逻辑。 |
| [src/context.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs) | `Context` 的 `store_packet`/`wait_packet`/`wait_until`，是 packet 指针在线程间中转的通道。 |
| [src/channel.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs) | `bounded(0)` 构造函数，把 `zero::Channel` 装进 `counter` 引用计数壳。 |

## 4. 核心概念与源码讲解

本讲拆成五个最小模块：

1. **`Packet`**：会合交接的「信封」。
2. **`Inner` / `Channel`**：一把锁管理两个 `Waker`。
3. **配对的枢纽**：`Waker::try_select` 与 packet 递包。
4. **抢占与搬运**：`start_send`/`start_recv` 与 `write`/`read`。
5. **阻塞版 `send`/`recv`**：register → notify → wait_until，以及栈 packet 与堆 packet 的分野。

### 4.1 Packet：会合交接的「信封」

#### 4.1.1 概念说明

zero 通道不缓冲消息，那消息在「发送方还没走、接收方还没到」或者「两者交接的那一瞬间」放在哪里？答案是一个临时的小容器，源码里叫 **`Packet`**。你可以把它想象成两人握手时中间递过去的一个信封：

- 发送方把消息塞进信封；
- 接收方从信封里取出消息；
- 信封用完即弃。

每个 `Packet` 只服务一次交接，用完就销毁。因为容量是 0，任意时刻通道里都不会积攒信封——要么正在被两个人共同使用，要么根本不存在。

#### 4.1.2 核心流程

一个 `Packet` 有三个字段，协同完成「写—就绪—读」的交接：

```text
Packet {
    on_stack: bool,          // 这个 packet 在栈上还是堆上？决定谁来销毁它
    ready: AtomicBool,       // 「我准备好了」的双向信号灯
    msg: UnsafeCell<Option<T>>, // 实际消息（UnsafeCell 允许「多线程看似共享」地读写）
}
```

`ready` 是个**双向复用**的信号灯，这是 zero 设计里很巧妙的一点：

- 当 **receiver 阻塞、sender 来配对** 时：sender 写完 `msg` 后把 `ready` 置 true，receiver 自旋等 `ready` 后再读 `msg`。此时 `ready` 表示「消息已就绪，可读」。
- 当 **sender 阻塞（带消息）、receiver 来配对** 时：receiver 直接读出 `msg`（消息一开始就在 packet 里），然后把 `ready` 置 true，sender 自旋等 `ready`。此时 `ready` 表示「消息已被取走，你可以销毁 packet 了」。

同一个布尔值，两种含义，由「谁带消息进入 packet」决定方向。

#### 4.1.3 源码精读

`Packet` 的定义与构造方法在 [src/flavors/zero.rs:41-87](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L41-L87)：

```rust
struct Packet<T> {
    on_stack: bool,
    ready: AtomicBool,
    msg: UnsafeCell<Option<T>>,
}
```

它有三个构造方法，分别对应三种使用场景（[zero.rs:52-78](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L52-L78)）：

- `empty_on_stack()`：栈上空信封，给**阻塞的 receiver** 用（等别人来填消息）。
- `empty_on_heap()`：堆上空信封，给 **select 路径** 用。
- `message_on_stack(msg)`：栈上带消息的信封，给**阻塞的 sender** 用（消息一开始就在里面）。

`wait_ready` 是自旋等待 `ready` 的方法（[zero.rs:80-86](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L80-L86)）：

```rust
fn wait_ready(&self) {
    let backoff = Backoff::new();
    while !self.ready.load(Ordering::Acquire) {
        backoff.snooze();
    }
}
```

读 `ready` 用 `Acquire`，与对端写 `ready` 时的 `Release` 配对（见 4.4），建立 happens-before，保证「信号灯亮起」之前对 `msg` 的写入对本线程可见。`Backoff::snooze()` 在自旋时逐步退避（先自旋几轮再让出 CPU），避免在锁即将释放时还上下文切换，又避免长时间空转。

#### 4.1.4 代码实践

**目标**：确认 zero 通道「既空又满」的怪异状态。

**步骤**：

1. 阅读 [src/flavors/zero.rs:366-384](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L366-L384)，注意 `len()` 永远返回 `0`、`is_empty()` 和 `is_full()` 都返回 `true`、`capacity()` 返回 `Some(0)`。
2. 运行 `cargo test --test zero len_empty_full -- --nocapture`（对应 [tests/zero.rs:34-57](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/tests/zero.rs#L34-L57)）。

**预期现象**：测试通过。这印证了「容量为 0」的语义——通道永远没有存货（`len==0`、`is_empty`），但也永远没有空位能再放一条（`is_full`）。

#### 4.1.5 小练习与答案

**练习 1**：`Packet` 用 `UnsafeCell<Option<T>>` 而不是普通 `Option<T>`，为什么？

**答案**：`UnsafeCell` 是 Rust 里「告诉编译器：这个内存位置可能被跨线程看似共享地读写」的唯一合法方式。普通 `Cell`/`Option` 在 `Sync` 类型里会导致编译失败；而 zero 需要一个 `&Packet<T>` 被 sender 写、receiver 读（在锁与 `ready` 信号保护下实际不并发），所以必须用 `UnsafeCell` 把「可变性」从类型系统里「逃逸」出来，由手写的原子操作与锁来保证安全。

**练习 2**：如果 `wait_ready` 里把 `Acquire` 换成 `Relaxed`，会发生什么问题？

**答案**：会丢失 happens-before。比如 receiver 路径下，sender 先 `write(msg)` 再 `ready.store(Release)`；receiver 若用 `Relaxed` 读 `ready`，可能看到 `ready==true` 但还没看到 `msg` 的写入，从而读到未初始化或旧值。`Acquire`/`Release` 配对保证了「先写消息、后亮灯」的顺序对等待方可见。

### 4.2 Inner 与 Channel：一把锁管理两个 Waker

#### 4.2.1 概念说明

zero 通道的「内部状态」其实很简单：谁在等发送被配对、谁在等接收被配对、通道断开了没。源码把它封装成 `Inner`，再用一把 `Mutex` 包成 `Channel`。

注意它用两个独立的 `Waker` 队列：

- `senders`：登记「我正在阻塞 `send`，等一个 receiver 来配对」的线程。
- `receivers`：登记「我正在阻塞 `recv`，等一个 sender 来配对」的线程。

当一个 receiver 到达，它去 `senders` 队列里挑一个等待中的 sender 配对；反之亦然。两个队列对称，方向相反。

#### 4.2.2 核心流程

```text
Channel<T>
  └── Mutex<Inner>
        ├── senders:   Waker   ← 等待中的 sender 们（每个带一个 packet 指针）
        ├── receivers: Waker   ← 等待中的 receiver 们（每个带一个 packet 指针）
        └── is_disconnected: bool
```

所有收发操作的第一步都是 `self.inner.lock()`，拿到锁后才能查看 / 修改队列和断开标志。这把锁是 zero flavor 的**唯一同步原语**（除了 `ready`/`packet` 这两个用于唤醒后细同步的原子量）。正因如此，里面的 `Waker` 用不着自带锁的 `SyncWaker`，直接用裸 `Waker` 即可。

断开（disconnect）时，置 `is_disconnected = true`，然后对两个队列都调 `disconnect()` 广播，唤醒所有阻塞者。

#### 4.2.3 源码精读

`Inner` 与 `Channel` 的定义在 [src/flavors/zero.rs:89-108](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L89-L108)：

```rust
struct Inner {
    senders: Waker,
    receivers: Waker,
    is_disconnected: bool,
}

pub(crate) struct Channel<T> {
    inner: Mutex<Inner>,
    _marker: PhantomData<T>,
}
```

`_marker: PhantomData<T>` 只是告诉编译器「销毁 `Channel<T>` 时可能会 drop `T` 类型的值」，本身不占空间。

`disconnect` 方法在 [zero.rs:353-364](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L353-L364)：

```rust
pub(crate) fn disconnect(&self) -> bool {
    let mut inner = self.inner.lock();
    if !inner.is_disconnected {
        inner.is_disconnected = true;
        inner.senders.disconnect();
        inner.receivers.disconnect();
        true
    } else {
        false
    }
}
```

它用 `is_disconnected` 做幂等保护——发送侧 drop 和接收侧 drop 都会经 `counter::release` 触发 `disconnect`（见 [channel.rs:680](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L680) 和 [channel.rs:1190](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1190)，zero 的回调都是 `|c| c.disconnect()`），但只有第一个真正广播唤醒。注意它**不区分**发送断开还是接收断开——因为零容量通道不存消息，两侧断开的语义都是「告诉所有阻塞者：别等了」。

`Waker::disconnect` 的广播逻辑见 [waker.rs:155-168](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L155-L168)：它对每个 entry 调 `cx.try_select(Selected::Disconnected)`，成功就 unpark，但**不从队列摘除** entry——留给被唤醒的线程自己 `unregister` 清理（因为它们可能还要处理 packet 的回收）。

#### 4.2.4 代码实践

**目标**：观察断开后阻塞操作立即返回错误。

**步骤**：

1. 阅读 [tests/zero.rs:59-77](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/tests/zero.rs#L59-L77) 的 `try_recv` 测试：一个线程先 `try_recv` 得到 `Empty`，睡一觉后 `try_recv` 得到 `Ok(7)`（配对成功），再睡一觉得到 `Disconnected`（发送端 drop 了）。
2. 运行 `cargo test --test zero try_recv -- --nocapture`。

**预期现象**：测试通过，说明 `disconnect` 的广播让仍在等的 receiver 立刻收到 `Disconnected`，而不是永远阻塞。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `Inner` 里用的是 `Waker` 而不是 `SyncWaker`？

**答案**：因为 `Inner` 已经被外层 `Mutex<Inner>` 保护，所有访问 `senders`/`receivers` 的代码都必须先拿锁，访问天然串行化。`SyncWaker` 自带的 `Mutex` + `AtomicBool(is_empty)` 快速路径是为「没有全局锁、需要无阻塞快速判断有没有等待者」的场景（如 array）准备的；zero 既然已经有锁，再套一层就是重复开销。

**练习 2**：`disconnect` 返回 `bool`，这个返回值被谁用到？

**答案**：它返回「是否是本次调用真正触发了断开」。`counter::release` 用它来决定是否要进一步销毁堆上的 `Counter`（参见 u2-l2）。对 zero 而言，第一次 disconnect 返回 true 并广播唤醒，之后另一侧 drop 再调 disconnect 只会返回 false，不会重复广播。

### 4.3 配对的枢纽：Waker::try_select 与 packet 递包

#### 4.3.1 概念说明

「会合」的核心动作是**配对**：一个新来的 receiver 要找到一个正在等的 sender（或反过来），然后两人完成交接。这个「找到对方、标记选中、把 packet 指针递过去、唤醒对方」的一连串动作，全部集中在 `Waker::try_select` 这一个方法里。它是 zero flavor 的心脏。

理解它的关键是分清两个角色：

- **发起配对的一方**（比如新来的 receiver）：它持锁，调用 `inner.senders.try_select()`，去「对方的队列」里挑人。
- **被选中的 entry**：属于**另一个线程**（正在阻塞的那个 sender），它的 `cx` 和 `packet` 都在那个线程手里。

`try_select` 做的三件事，全部作用在「被选中的 entry 的 `cx`」上：

1. `cx.try_select(Selected::Operation(oper))`：CAS 把对方的状态从 `Waiting` 改成「被你的这次操作选中」。
2. `cx.store_packet(entry.packet)`：把对方注册时留下的 packet 指针，存进对方自己的 `Context.packet` 槽。
3. `cx.unpark()`：唤醒对方线程。

为什么把 packet 存回对方自己的 context？因为对方被唤醒后，要能拿到「该用哪个 packet 完成交接」。阻塞路径里对方其实直接用栈上的 packet 变量；但 select 路径里 `register` 和 `accept` 是两次独立调用，必须靠 `Context.packet` 这个中转槽把指针传回去（见 4.5）。

#### 4.3.2 核心流程

```text
receiver 线程（发起配对）            sender 线程（已在 senders 队列里阻塞）
─────────────────────────            ──────────────────────────────────
lock(Inner)
inner.senders.try_select():
  遍历 senders 队列
  找到 entry(e.cx 是 sender 的 cx)
  e.cx.try_select(Operation)  ──CAS──►  select: Waiting → Operation
  e.cx.store_packet(e.packet) ────────► packet 槽被填上 sender 的 packet 指针
  e.cx.unpark()               ──唤醒──► (sender 醒来，从 wait_until 返回 Operation)
  从队列摘除 entry
unlock(Inner)
```

关键约束：`try_select` 只挑**别的线程**的 entry（`selector.cx.thread_id() != thread_id`），避免一个线程选中自己造成自激。

#### 4.3.3 源码精读

`Waker::try_select` 在 [src/waker.rs:84-111](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L84-L111)：

```rust
pub(crate) fn try_select(&mut self) -> Option<Entry> {
    if self.selectors.is_empty() {
        None
    } else {
        let thread_id = current_thread_id();
        self.selectors.iter().position(|selector| {
            selector.cx.thread_id() != thread_id          // 必须是别的线程
                && selector.cx.try_select(Selected::Operation(selector.oper)).is_ok()  // CAS 选中
                && {
                    selector.cx.store_packet(selector.packet); // 递包
                    selector.cx.unpark();                       // 唤醒
                    true
                }
        }).map(|pos| self.selectors.remove(pos))            // 摘除 entry
    }
}
```

注意三步是用 `&&` 短路串联的：只有 CAS 成功（说明对方还处于 `Waiting`，没被别的线程抢先选中、也没超时 abort）才会递包和唤醒，保证一次配对「至多一个赢家」。

`store_packet` 与 `wait_packet` 是 `Context` 上的一对方法，在 [src/context.rs:121-138](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs#L121-L138)：

```rust
pub fn store_packet(&self, packet: *mut ()) {
    if !packet.is_null() {
        self.inner.packet.store(packet, Ordering::Release);
    }
}

pub fn wait_packet(&self) -> *mut () {
    let backoff = Backoff::new();
    loop {
        let packet = self.inner.packet.load(Ordering::Acquire);
        if !packet.is_null() { return packet; }
        backoff.snooze();
    }
}
```

`store_packet` 用 `Release`、`wait_packet` 用 `Acquire`，又是一对 happens-before：递包方的所有 prior 写入（比如 CAS 选中状态）对等待方可见。

辅助方法 `can_select`（[waker.rs:114-125](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L114-L125)）只判断「有没有可被当前线程选中的 entry」，不真的选中——它被 select 路径的 `is_ready`/`register` 用来快速判断就绪状态。

#### 4.3.4 代码实践

**目标**：确认「try_select 只挑别的线程」这条规则。

**步骤**：

1. 在 [src/waker.rs:84-111](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L84-L111) 中找到 `selector.cx.thread_id() != thread_id` 这一行。
2. 思考：如果同一个线程在 `select!` 里同时注册了对同一通道的 send 和 recv（合法用法），它们会不会互相配对？

**预期结果**：不会互相配对——因为 `thread_id` 相等会被跳过。同线程的 send/recv 必须各自等待**其他线程**来配对。这是 zero 避免自激死锁的关键。

#### 4.3.5 小练习与答案

**练习 1**：`try_select` 为什么要从队列里 `remove` 掉选中的 entry？

**答案**：保持队列干净、提升后续查找性能；更重要的是避免同一个 entry 被二次选中——entry 一旦被摘除，别的线程再调 `try_select` 就找不到它了，配合 `cx.try_select` 的 CAS，双保险保证「至多一个赢家」。

**练习 2**：`store_packet` 里有 `if !packet.is_null()` 判断。什么时候 packet 会是 null？

**答案**：`Waker::register`（不带 packet 的版本，[waker.rs:52-54](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L52-L54)）注册的 entry 的 packet 是 null。这种 entry 只需要被唤醒、不需要递包（比如 observer）。zero 的收发路径都用 `register_with_packet` 带 packet，但 `Waker` 作为通用结构要兼容 null 情况。

### 4.4 抢占与搬运：start_send / start_recv 与 write / read

#### 4.4.1 概念说明

和 array/list 一样，zero 的收发也拆成「抢占（`start_*`）」和「搬运（`write`/`read`）」两步。这种拆分是为配合 select：select 算法需要先「试探性地抢占一个配对资格」，再决定要不要真正完成这次操作。

- `start_send` / `start_recv`：持锁，尝试配对一个等待中的对端。成功就把「对方的 packet 指针」写进 `token`，返回 true。
- `write` / `read`：根据 `token` 里的 packet 指针，真正搬运消息。

注意 zero 的「抢占」不像 array 那样抢占一个槽位（它没有槽），而是直接**配对一个对端**——抢占和配对在 zero 里是同一件事。

#### 4.4.2 核心流程

以 receiver 为例（sender 对称）：

```text
start_recv(token):
  lock(Inner)
  if let Some(op) = inner.senders.try_select():   # 有等待的 sender？配对它
      token.zero.0 = op.packet                    # 记下 sender 的 packet 指针
      return true
  if inner.is_disconnected:                        # 没人等，但通道断了
      token.zero.0 = null                          # 用 null 表示「断开」
      return true
  return false                                     # 没人等也没断 → 没准备好

read(token):
  if token.zero.0.is_null(): return Err(())        # 断开
  packet = token.zero.0 指向的 Packet
  if packet.on_stack:                              # 对方是阻塞路径（栈 packet）
      msg = 取出 packet.msg
      packet.ready.store(Release, true)            # 告诉对方「我读完了，可销毁」
      return Ok(msg)
  else:                                            # 对方是 select 路径（堆 packet）
      packet.wait_ready()                          # 等对方把消息写进来
      msg = 取出 packet.msg
      Box::from_raw 销毁堆 packet
      return Ok(msg)
```

`token.zero.0` 是个裸指针，三种取值：`null`（断开）、栈 packet 地址、堆 packet 地址。`read`/`write` 靠 `on_stack` 标志区分后两者。

#### 4.4.3 源码精读

`start_send` 与 `write` 在 [src/flavors/zero.rs:133-160](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L133-L160)：

```rust
fn start_send(&self, token: &mut Token) -> bool {
    let mut inner = self.inner.lock();
    if let Some(operation) = inner.receivers.try_select() {   // 配对一个 receiver
        token.zero.0 = operation.packet;
        true
    } else if inner.is_disconnected {
        token.zero.0 = ptr::null_mut();
        true
    } else {
        false
    }
}

pub(crate) unsafe fn write(&self, token: &mut Token, msg: T) -> Result<(), T> {
    if token.zero.0.is_null() { return Err(msg); }            // 断开 → 退回消息
    let packet = unsafe { &*(token.zero.0 as *const Packet<T>) };
    unsafe { packet.msg.get().write(Some(msg)) }              // 写消息
    packet.ready.store(true, Ordering::Release);              // 通知 receiver 可读
    Ok(())
}
```

`start_recv` 与 `read` 在 [zero.rs:162-202](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L162-L202)。`read` 是理解栈/堆分野的最佳入口：

```rust
pub(crate) unsafe fn read(&self, token: &mut Token) -> Result<T, ()> {
    if token.zero.0.is_null() { return Err(()); }
    let packet = unsafe { &*(token.zero.0 as *const Packet<T>) };
    if packet.on_stack {
        // 对方一早就把消息放进 packet 了，不用等；但读完后要置 ready，让对方能销毁 packet
        let msg = unsafe { packet.msg.get().replace(None).unwrap() };
        packet.ready.store(true, Ordering::Release);
        Ok(msg)
    } else {
        // 堆 packet：等对方写好消息，读完销毁堆内存
        packet.wait_ready();
        let msg = unsafe { packet.msg.get().replace(None).unwrap() };
        drop(unsafe { Box::from_raw(token.zero.0.cast::<Packet<T>>()) });
        Ok(msg)
    }
}
```

注意两个分支的 `ready` 语义正好相反（呼应 4.1）：栈分支里 receiver **读完才置** `ready`（告诉 sender「可销毁」）；堆分支里 receiver **先等** `ready`（等 sender 写完）。

非阻塞的 `try_send` / `try_recv` 就是「只做抢占+搬运、不阻塞」的版本，见 [zero.rs:204-296](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L204-L296)：抢不到配对且没断开时，`try_send` 返回 `Full(msg)`、`try_recv` 返回 `Empty`。

#### 4.4.4 代码实践

**目标**：验证非阻塞路径在「无人配对」时的返回值。

**步骤**：

1. 阅读 [tests/zero.rs:20-25](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/tests/zero.rs#L20-L25) 的 `smoke` 测试：刚创建的 `bounded(0)` 通道，`try_send(7)` 立刻返回 `Err(TrySendError::Full(7))`，`try_recv()` 返回 `Err(TryRecvError::Empty)`。
2. 运行 `cargo test --test zero smoke -- --nocapture`。

**预期结果**：通过。因为单线程下没有对端可配对，`start_send`/`start_recv` 走到 `false` 分支，`try_*` 据此返回 `Full`/`Empty`。

#### 4.4.5 小练习与答案

**练习 1**：`write` 里写 `msg` 用的是 `packet.msg.get().write(Some(msg))`（`UnsafeCell::get` + 指针 `write`），而不是 `*packet.msg.get() = Some(msg)`。两者区别是什么？

**答案**：`write` 会先 drop 旧值再写入新值；直接解引用赋值也会 drop 旧值，但 `write` 是更明确的「覆盖一个 MaybeUninit/未初始化内存」的语义。这里 packet 的 msg 槽在 `empty_*` 构造时已经是 `None`，写 `Some(msg)` 是安全的覆盖。两种写法在这里效果相近，但 `ptr::write` 语义上不读旧值，与「我知道这块内存当前状态」的 unsafe 心智模型更一致。

**练习 2**：`read` 的堆分支最后用 `Box::from_raw` 销毁 packet。如果忘了这一步会怎样？

**答案**：内存泄漏。堆 packet 是 `register` 时 `Box::into_raw` 创建的（见 4.5），没有任何其他所有者；`read` 是它「被消费」的唯一时机，必须在这里 `Box::from_raw` 回收。栈 packet 则不需要，因为它会随持有它的栈帧自动销毁。

### 4.5 阻塞 send/recv：register → notify → wait_until，以及栈 packet 与堆 packet 的分野

#### 4.5.1 概念说明

阻塞版的 `send`/`recv`（即用户调用的 `s.send(x)` / `r.recv()`）在没有对端时要挂起当前线程，等对端来了再被唤醒。流程是 u2-l4 讲过的三段式：

1. **register**：创建一个 packet，把自己登记进对应的 `Waker` 队列。
2. **notify**：叫醒对端队列里可能存在的等待者（让它来配对我）。
3. **wait_until**：park 自己，醒来后根据 `Selected` 的四种状态决定返回什么。

本模块要重点讲清楚两个设计决策：

**(A) `ready` 同步的方向**——在 4.4 已铺垫，这里结合完整流程再串一遍。

**(B) 为什么普通 `send`/`recv` 用栈 packet，而 select 路径用堆 packet？**

- 普通 `send`/`recv`：register 和「使用 packet」发生在**同一个栈帧**里（整个逻辑在 `send`/`recv` 方法的一次调用内）。packet 可以是局部变量，写在栈上，随方法返回自动销毁，**零堆分配**。
- select 路径：`register`（创建并登记 packet）和 `accept` + `read`/`write`（使用 packet）是 select 算法在**不同时机分别调用**的两个独立方法，中间隔着 select 的调度循环，没有共同栈帧能持有 packet；而且 select 可能 cancel（`unregister`）需要独立释放。所以 packet 必须在堆上，由 `register` 分配、由 `read`/`write`（成功）或 `unregister`（取消）释放。

换句话说：**栈 packet 的前提是「创建者和使用者是同一段同步代码」；select 把这两步拆开了，就只能上堆。**

#### 4.5.2 核心流程：阻塞 send 的完整时序

下面是「sender 先阻塞、receiver 后到」的完整时序（这是本讲综合实践要你画的图）：

```text
sender 线程                            receiver 线程
─────────────                          ─────────────
s.send(msg):
  lock(Inner)
  receivers.try_select()? → 队列空，无人可配对
  is_disconnected? → 否
  Context::with(|cx| {
    packet = message_on_stack(msg)     # 栈 packet，msg 已在内
    senders.register_with_packet(      # 把自己登记进 senders 队列
        oper, &packet, cx)
    receivers.notify()                 # 通知 observer（select 用）
    unlock(Inner)                       # ⚠ 释放锁后再阻塞
    sel = cx.wait_until(deadline) ──park──►
                                              r.recv():
                                                lock(Inner)
                                                senders.try_select():
                                                  选中 sender 的 entry
                                                  sender.cx.try_select(Operation) ✅
                                                  sender.cx.store_packet(&packet)
                                                  sender.cx.unpark() ──唤醒────►
                                                token.zero.0 = sender 的 packet
                                                unlock(Inner)
                                                read(token):
                                                  packet.on_stack == true
                                                  msg = 取出 packet.msg
                                                  packet.ready.store(Release,true)
                                                  return Ok(msg)
    sel == Operation(_):
      packet.wait_ready() ◄──Acquire──  (读到 ready==true，说明 msg 已被取走)
      return Ok(())                      # packet 随栈帧销毁
  })
```

两个关键同步点：

1. **receiver 取 msg 的可见性**：sender 在持锁期间（register_with_packet 前）已把 msg 写进 packet；receiver 也持锁（start_recv 内）拿到 packet 指针。锁的释放/获取保证 receiver 看到 packet 时 msg 已写入。
2. **sender 销毁 packet 的安全性**：receiver 读 msg 后用 `Release` 置 ready，sender 用 `Acquire` 读 ready。于是「receiver 读 msg」happens-before 「sender 销毁栈 packet」。sender 在 `wait_ready` 返回前绝不会销毁 packet，receiver 的 `read` 此时早已完成。

receiver 先阻塞、sender 后到的情况对称：receiver 用 `empty_on_stack`（空 packet），sender `write` 写 msg 并置 ready，receiver 醒来后 `wait_ready` 等到 ready 再读 msg。`ready` 方向反过来，但机制一致。

#### 4.5.3 源码精读

阻塞 `send` 在 [src/flavors/zero.rs:225-279](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L225-L279)，关键段落：

```rust
Context::with(|cx| {
    let oper = Operation::hook(token);
    let mut packet = Packet::<T>::message_on_stack(msg);        // 栈 packet
    inner.senders.register_with_packet(oper, &mut packet as *mut _ as *mut (), cx);
    inner.receivers.notify();
    drop(inner);                                                  // 释放锁
    let sel = cx.wait_until(deadline);                            // 阻塞
    match sel {
        Selected::Waiting => unreachable!(),
        Selected::Aborted => {                                    // 超时
            self.inner.lock().senders.unregister(oper).unwrap();
            let msg = unsafe { packet.msg.get().replace(None).unwrap() };
            Err(SendTimeoutError::Timeout(msg))
        }
        Selected::Disconnected => {                               // 断开
            self.inner.lock().senders.unregister(oper).unwrap();
            let msg = unsafe { packet.msg.get().replace(None).unwrap() };
            Err(SendTimeoutError::Disconnected(msg))
        }
        Selected::Operation(_) => {                               // 配对成功
            packet.wait_ready();                                  // 等消息被取走
            Ok(())
        }
    }
})
```

几个要点：

- **`message_on_stack`**：msg 在注册前就放进 packet（所以 receiver 的 `read` 栈分支注释说「消息一开始就在 packet 里，不必等」）。
- **`Aborted`/`Disconnected` 分支**：醒来发现不是被配对，而是超时或断开，要主动 `unregister` 把自己从队列摘除，并取回 msg（因为没人会来读它了）。这印证了 u2-l4 提到的「disconnect 不摘 entry，留给被唤醒者自清理」。
- **`Operation` 分支的 `wait_ready`**：被配对唤醒后，还要等 receiver 把 msg 取走、置 ready，才能安全销毁栈 packet。

阻塞 `recv` 在 [zero.rs:298-348](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L298-L348)，结构完全对称，只是用 `empty_on_stack()`，`Operation` 分支是 `packet.wait_ready()` 后 `Ok(packet.msg.get().replace(None).unwrap())`。

select 路径用堆 packet，见 `Receiver` 的 `SelectHandle::register` 与 `accept`，在 [zero.rs:402-424](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L402-L424)：

```rust
fn register(&self, oper: Operation, cx: &Context) -> bool {
    let packet = Box::into_raw(Packet::<T>::empty_on_heap());     // 堆 packet
    let mut inner = self.0.inner.lock();
    inner.receivers.register_with_packet(oper, packet.cast::<()>(), cx);
    inner.senders.notify();
    inner.senders.can_select() || inner.is_disconnected          // 返回是否已就绪
}

fn accept(&self, token: &mut Token, cx: &Context) -> bool {
    token.zero.0 = cx.wait_packet();                              // 等对端把 packet 递回来
    true
}
```

`register` 用 `Box::into_raw` 创建堆 packet（无人会在 `register` 的栈帧里持有它），`accept` 用 `wait_packet` 从自己的 `Context.packet` 槽里取回 packet 指针（这个指针是配对时由 `try_select → store_packet` 递回来的）。后续 select 算法调 `read`/`write` 完成交接，堆 packet 在 `read` 的堆分支里被 `Box::from_raw` 销毁；若 select 改主意 cancel 了，则由 `unregister`（[zero.rs:413-419](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L413-L419)）里的 `Box::from_raw` 销毁。

#### 4.5.4 代码实践

**目标**：亲手验证「普通 send/recv 用栈 packet、select 用堆 packet」造成的可观测差异，并画出时序图。

**步骤**：

1. 编写一个小程序（示例代码）：

   ```rust
   use std::thread;
   use crossbeam_channel::{bounded, select, after};
   use std::time::Duration;

   fn main() {
       // 场景一：普通阻塞 send/recv（栈 packet）
       let (s, r) = bounded::<String>(0);
       let h = thread::spawn(move || s.send("hello".to_string()));
       assert_eq!(r.recv(), Ok("hello".to_string()));
       h.join().unwrap();

       // 场景二：select 路径（堆 packet）+ 超时
       let (s2, r2) = bounded::<String>(0);
       select! {
           recv(r2) -> msg => println!("收到: {:?}", msg),
           recv(after(Duration::from_millis(50))) -> _ => println!("超时"),
       }
       let _ = s2;
   }
   ```

2. 在 4.5.2 的时序模板基础上，分别画出场景一（sender 先阻塞、receiver 配对）和场景二（receiver 在 select 里注册堆 packet、50ms 内无 sender 到达 → 走超时分支 → `unregister` 销毁堆 packet）的时序图。
3. 运行 `cargo run`（需把它放进 `examples/` 或一个临时 binary）观察输出。

**需要观察的现象**：场景一成功收到 `"hello"`；场景二因为 50ms 内没有 sender，打印「超时」——此时 select 算法会 cancel 掉 receiver 的注册，`unregister` 把堆 packet 用 `Box::from_raw` 回收。

**预期结果**：场景一 `Ok("hello")`；场景二打印「超时」，无内存泄漏（`unregister` 已回收堆 packet）。

**待本地验证**：场景二的时序细节（`register` → `notify` → `wait_until` → 超时 `Aborted` → `unregister`）建议对照 [src/select.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs) 的 `run_select` 阅读确认（select 算法本身是 u3-l1 的内容，本讲只关注 zero 这一头如何配合）。

#### 4.5.5 小练习与答案

**练习 1**：阻塞 `send` 在 `register_with_packet` 之后、`wait_until` 之前，为什么要先 `drop(inner)`（释放锁）？

**答案**：如果持着锁再去 `wait_until` park，被配对的对端将永远拿不到锁——它需要锁才能调 `start_recv` → `senders.try_select()` 来唤醒你。持锁阻塞会造成死锁。所以必须先释放锁，再 park。

**练习 2**：假如把阻塞 `send` 里的 `message_on_stack(msg)` 改成 `empty_on_heap()`（像 select 那样），程序还能正确工作吗？

**答案**：不能正确工作。`message_on_stack` 把 msg 一开始就放进 packet，使得 receiver 的 `read` 走 `on_stack` 分支（不用等 ready、直接读）。若改成 `empty_on_heap`，receiver 会走堆分支 `wait_ready()`，而 sender 在阻塞 `send` 里**不会**去 `write` 消息（消息还在 sender 的 `msg` 变量里），receiver 会永远自旋等一个永远不会到来的 `ready`。这印证了 packet 的「构造方式」与「读/写分支」是严格配套的。

**练习 3**：select 的 `register` 返回 `inner.senders.can_select() || inner.is_disconnected`，这个返回值如何影响 select 算法？

**答案**：返回 true 表示「已经有对端在等，或者通道已断开」，即本次操作**立刻就绪**。select 算法据此走 fast path：直接调 `accept` 完成操作，跳过「注册后 park 等待」的慢路径。返回 false 则 select 会继续尝试其他操作，最终可能 park 等待。

## 5. 综合实践

把本讲的知识串起来，完成下面这个「会合握手 + 超时」的小任务。

**任务背景**：你要实现一个主循环，它要么从一个 `bounded(0)` 通道接收一个工作请求（会合式），要么在 100ms 内等不到就打印心跳。同时有一个 worker 线程随机延迟后发送请求。

**要求**：

1. 用 `bounded(0)` 创建会合通道 `(s, r)`。
2. 主循环用 `select!` 在 `recv(r)` 和 `recv(after(100ms))` 之间选择，循环 5 次。
3. worker 线程用 `thread::sleep` 模拟随机延迟后调 `s.send(req)`——体会它必须等主循环的 `recv` 同时在场才能完成。
4. 在代码注释里，针对某一次成功的交接，画出 4.5.2 那样的时序图，标出：worker 创建栈 packet（`message_on_stack`）、register、释放锁、park；主循环 `start_recv` → `try_select` → `store_packet` → `unpark`；主循环 `read`（栈分支，置 ready）；worker `Operation` 分支 `wait_ready`。
5. 思考：如果把 worker 的 `send` 也放进一个 `select!`（比如同时 select 一个取消信号），它的 packet 会从栈变成堆——在注释里说明这对内存回收路径的影响（成功时 `read` 的堆分支 `Box::from_raw`，取消时 `unregister` 的 `Box::from_raw`）。

**验证**：运行 `cargo test --test zero`（[tests/zero.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/tests/zero.rs) 全集）应全部通过，作为你实现的语义参照。如果自行实现，可在小程序里用 `assert!` 检查每次 `recv` 的结果是否符合预期；时序图部分无法自动验证，请对照本讲 4.5.2 的模板自查。

## 6. 本讲小结

- zero 是**会合通道**：容量为 0，不缓冲任何消息，发送与接收必须同时在场，靠一个一次性的 `Packet` 完成单次交接。
- 内部状态是 `Mutex<Inner>`，里面装着两个**裸 `Waker`**（`senders`/`receivers`）和一个 `is_disconnected` 标志——因为已有全局锁，不必再用 `SyncWaker`。
- 配对的心脏是 `Waker::try_select`：在**别的线程**的 entry 上完成「CAS 选中 → `store_packet` 递包 → `unpark` 唤醒」三连，保证至多一个赢家。
- 收发拆成 `start_*`（持锁配对、把对端 packet 指针写进 token）与 `write`/`read`（搬运消息）两步；`token.zero.0` 用 `null` 表示断开。
- `ready` 是双向复用信号：谁带消息进 packet，对方就等 `ready`；谁后到，谁置 `ready`。`Release`/`Acquire` 配对保证消息可见与 packet 销毁安全。
- **栈 packet** 用于普通阻塞 `send`/`recv`（创建与使用在同一栈帧，零分配）；**堆 packet** 用于 select 路径（`register` 与 `accept` 分离，且需支持 cancel 回收）。

## 7. 下一步学习建议

- 想看 select 算法如何驱动 zero 的 `register`/`accept`/`read`/`write`，继续学 **u3-l1（select 核心算法 run_select）** 和 **u3-l2（SelectHandle trait 与 flavor 对接）**。
- 想系统对比三种「真实」flavor 的同步策略差异，回头对照 **u2-l5（array）** 和 **u2-l6（list）**：array/list 用原子游标 + `SyncWaker` 实现少锁/无锁，而 zero 因为不存消息、只做一次性配对，用一把锁反而最简单。
- 对 `ready` 这类「单原子量承载双向 happens-before」的范式感兴趣，可在 **u3-l4（内存序与 unsafe 的正确性）** 中看到更系统的总结。
