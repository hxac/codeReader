# SelectHandle trait 与 flavor 对接

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 `SelectHandle` trait 的七个方法各自承担什么职责，以及它们如何被 `run_select`/`run_ready` 调度算法调用。
- 读懂 `src/channel.rs` 里 `Sender`/`Receiver` 如何把 `SelectHandle` 的调用「按 flavor 分发」到底层，并理解 `unsafe fn write/read` 如何完成一次被选中的操作。
- 区分两种对接风格：array/list 的「就绪驱动」（register 只登记、accept 再 CAS 抢占槽位）与 zero 的「packet 驱动」（register 预分配 packet、accept 取回对端递来的 packet）。
- 理解 at/tick/never 三个时间型 flavor 为何「不登记、靠 deadline 驱动」。
- 解释 `internal` 隐藏模块（`select`/`try_select`/`select_timeout`/`receiver_addr`/`sender_addr`）为何要 `#[doc(hidden)]` 暴露给 `select!` 宏使用。

本讲是上一讲（u3-l1 `run_select`/`run_ready`）的承接：上一讲讲清了「调度算法怎么调度」，本讲讲清「被调度的每一种 flavor 是如何实现这七个接口、把一个真实的收发操作交给算法驱动的」。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**契约（trait）的视角。** `run_select` 是一个「通用调度器」，它手里只有一堆 `&dyn SelectHandle`（对象指针），并不知道某个操作到底是 array 通道的发送、还是 zero 通道的接收。它只能通过 trait 规定的七个方法去问每个操作：「你现在能立刻完成吗（`try_select`）？」「你最早什么时候能就绪（`deadline`）？」「请把我登记为阻塞者，等等记得叫醒我（`register`）。」……这套七个方法就是 `SelectHandle` trait，是**算法与具体通道实现之间的唯一接口**。

**Token 的视角。** select 把「一次操作的完成」拆成两个阶段：**抢占**（reserve a slot）和**搬运**（write/read 数据）。抢占阶段只是用 CAS 在队列里「订座」，还没碰数据；搬运阶段才真正读写消息。两阶段之间需要一个「凭证」传递订座信息——这就是 `Token`。`Token` 是个联合体，每种 flavor 在里面有一个字段（`token.array`、`token.zero`、`token.at`…），抢占阶段由 flavor 往里写订座信息，搬运阶段由 flavor 再从里读出来完成操作。理解了「抢占 + Token + 搬运」三段式，就理解了本讲全部内容的主线。

**两种对接风格的直觉。**
- **就绪驱动（array/list）**：通道本体就是一条带槽位的队列，select 只要等到「有空槽 / 有消息」即可。所以 register 只做一件事——把自己挂到阻塞者队列 `Waker` 上；真正抢占槽位交给 `accept` 再跑一次 CAS。Token 的 `slot/stamp` 在 `accept` 时才算出来。
- **packet 驱动（zero）**：零容量通道没有队列、不存消息，发送与接收必须「人盯人」配对。所以 register 时会**预分配一个堆上的 `Packet`** 并随登记一并挂上；当对端来配对时，`Waker::try_select` 会把这个 packet 直接「递」到阻塞线程手里；`accept` 只负责把对端递来的 packet 取回来。Token 的 `zero.0`（packet 指针）在 `accept` 时才从对端拿到。

> 关键术语回顾：`SelectHandle` trait、`Token`、`Operation`（操作身份证）、`Selected`（四态状态机）、`Context`（线程本地阻塞上下文）、`Waker`（阻塞者队列）。这些在 u3-l1 与 u2-l4 已建立，本讲直接使用。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/select.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs) | 定义 `SelectHandle` trait、`Token`、`Operation`、`Selected`，以及 `run_select`/`run_ready` 调度算法、`internal` 暴露的 `select`/`try_select`/`select_timeout`/`sender_addr`/`receiver_addr`、`SelectedOperation`。 |
| [src/channel.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs) | 对外 `Sender`/`Receiver` 的 `SelectHandle` 实现（按 flavor 分发）、`unsafe fn write/read`、`addr()`。 |
| [src/flavors/array.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs) | 有界环形缓冲 flavor 的 `SelectHandle` 实现（就绪驱动）。 |
| [src/flavors/zero.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs) | 零容量会合 flavor 的 `SelectHandle` 实现（packet 驱动）。 |
| [src/flavors/list.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs) | 无界链表 flavor 的 `SelectHandle` 实现（接收侧就绪驱动，发送侧恒就绪）。 |
| [src/flavors/at.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/at.rs) / [src/flavors/tick.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/tick.rs) / [src/flavors/never.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/never.rs) | 时间型/空 flavor 的 `SelectHandle` 实现（靠 `deadline` 驱动，不登记）。 |
| [src/context.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs) | `Context`：`store_packet`/`wait_packet`——zero 配对时 packet 的「投递口」与「领取口」。 |
| [src/waker.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs) | `Waker`：`try_select`/`register_with_packet`/`can_select`——zero 配对的真正执行者。 |
| [src/lib.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs) | `internal` 隐藏模块，把 trait 与几个函数 re-export 给 `select!` 宏。 |
| [src/select_macro.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs) | `select!` 宏展开，是 `internal` 模块的唯一调用方。 |

## 4. 核心概念与源码讲解

### 4.1 SelectHandle trait：七个方法构成的契约

#### 4.1.1 概念说明

`SelectHandle` 是一个**仅供 select 内部使用**的 trait，定义在 [src/select.rs:99-123](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L99-L123)。它把「一次通道操作」抽象成七个动作，调度算法（`run_select`/`run_ready`）只通过这七个动作驱动操作完成，而对操作背后的 flavor 一无所知。

七个方法可分两组：

**第一组：抢占式（服务于 `run_select`，返回的 `SelectedOperation` 必须完成）。**

| 方法 | 职责 | 调用时机 |
| --- | --- | --- |
| `try_select(&mut Token) -> bool` | 尝试立即抢占一个操作（订座），成功则把订座信息写进 `Token` | fast path、每次循环开头、被唤醒后 |
| `register(oper, &Context) -> bool` | 把当前线程登记为阻塞者；返回 `true` 表示「登记的瞬间操作就就绪了」 | 准备 park 之前 |
| `unregister(oper)` | 取消登记（park 醒来后或超时后清理） | 离开阻塞前 |
| `accept(&mut Token, &Context) -> bool` | 被唤醒后，对「胜出的那个操作」完成抢占/取回凭证 | `Selected::Operation` 分支 |
| `deadline() -> Option<Instant>` | 返回这个操作的最早就绪时间（仅时间型 flavor 非空） | 计算 park 截止时间 |

**第二组：就绪式（服务于 `run_ready`，只通知「就绪」，不占座）。**

| 方法 | 职责 |
| --- | --- |
| `is_ready() -> bool` | 不阻塞地查询操作是否可立刻完成 |
| `watch(oper, &Context) -> bool` | 登记为「就绪观察者」（observers 队列），返回 `true` 表示已就绪 |
| `unwatch(oper)` | 取消就绪观察 |

> 注意：`run_ready` 用 `is_ready`/`watch`/`unwatch`；`run_select` 用 `try_select`/`register`/`unregister`/`accept`。`deadline` 两者共用。这正是 u3-l1 所讲「select 有两套内核」的体现。

trait 还为 `&T: SelectHandle` 提供了一个 blanket impl（[src/select.rs:125-157](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L125-L157)），让所有 `&dyn SelectHandle` 都能被调度器当作对象使用——这是 `run_select` 用 `&[&dyn SelectHandle, …]` 切片驱动异构操作列表的基础。

#### 4.1.2 核心流程：七个方法如何被 run_select 串起来

回看 u3-l1 的 `run_select`，把七个方法的调用点对齐到流程上（行号对应 [src/select.rs:176-324](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L176-L324)）：

```text
1. shuffle(handle)                            # 公平性（非 biased）
2. for h in handles: h.try_select(token)      # fast path 抢占        [L207-L211]
3. loop:
   3a. for h: h.register(oper, cx)            # 登记，返回 true 视为就绪 [L229]
   3b. deadline = min(timeout, 各 h.deadline)  # 收集最早截止时间      [L256-L260]
   3c. sel = cx.wait_until(deadline)           # park 等待
   3d. for h: h.unregister(oper)              # 清理登记              [L267-L269]
   3e. match sel:
       Aborted     => 重试 try_select
       Disconnected=> {}
       Operation   => 找到胜者 h, h.accept(token, cx)   # 完成抢占   [L286-L295]
```

关键细节：`register` 的返回值 `bool` 是**「防丢失唤醒」**机制——如果在登记的瞬间操作恰好就绪，`register` 立即返回 `true`，调用方据此 abort 自己、回退去跑 fast path 的 `try_select`（[src/select.rs:228-239](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L228-L239)），避免「就绪事件发生在登记与 park 之间的窗口」造成永久阻塞。

而 `accept` **只在 `Selected::Operation(_)` 分支登场**（[src/select.rs:284-296](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L284-L296)）：当线程是被「别的线程帮它选中了某个 operation」叫醒时，它用 `accept` 去取回那个操作的订座信息（Token）。`Aborted` 分支则用 `try_select` 自己重抢。

#### 4.1.3 源码精读：trait 定义与 Operation 身份证

trait 本体（[src/select.rs:99-123](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L99-L123)）：

```rust
pub trait SelectHandle {
    fn try_select(&self, token: &mut Token) -> bool;
    fn deadline(&self) -> Option<Instant>;
    fn register(&self, oper: Operation, cx: &Context) -> bool;
    fn unregister(&self, oper: Operation);
    fn accept(&self, token: &mut Token, cx: &Context) -> bool;
    fn is_ready(&self) -> bool;
    fn watch(&self, oper: Operation, cx: &Context) -> bool;
    fn unwatch(&self, oper: Operation);
}
```

`Operation` 是操作的「身份证」，用线程本地某个引用的地址充当（[src/select.rs:38-52](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L38-L52)）。它必须保证 `> 2`，以避让 `Selected::{Waiting=0, Aborted=1, Disconnected=2}` 这三个保留值——这样 `Operation` 与「未就绪三态」可以共用同一个 `AtomicUsize` 状态槽而不冲突（详见 u3-l1 的 `Selected` 状态机）。

#### 4.1.4 代码实践：阅读 trait 与调度调用点

1. **实践目标**：把 trait 的七个方法与 `run_select` 的调用点一一对应。
2. **操作步骤**：打开 [src/select.rs:176-324](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L176-L324)，逐行标注每个 `try_select`/`register`/`unregister`/`accept`/`deadline` 调用。
3. **需要观察的现象**：注意 `accept` 只出现在 `Selected::Operation(_)` 分支；`try_select` 出现在 fast path、Aborted 分支、每次循环末尾共三处。
4. **预期结果**：你能用一句话总结「`accept` 处理『被别人选中』，`try_select` 处理『自己抢』」。

#### 4.1.5 小练习与答案

- **练习 1**：为什么 `register` 要返回 `bool`，而不直接 `-> ()`？
  - **答案**：返回 `true` 表示「登记瞬间操作恰好就绪」。调度器据此立刻 abort 自己并回退到 fast path 的 `try_select`，否则会错过这个就绪事件、白白 park。
- **练习 2**：`run_ready` 用到哪三个方法？为什么它返回的是 `index` 而不是 `SelectedOperation`？
  - **答案**：用到 `is_ready`/`watch`/`unwatch`。`run_ready` 只「通知就绪、不占座」，所以不返回必须完成的 `SelectedOperation`，调用方拿到 index 后要自己 `try_recv` 重试（可能虚假唤醒）。

---

### 4.2 channel.rs 的分发层：SelectHandle impl + unsafe write/read

#### 4.2.1 概念说明

`Sender<T>`/`Receiver<T>` 是对外暴露的「壳」，内部只持一个 flavor 枚举字段（`SenderFlavor`/`ReceiverFlavor`）。它们实现的 `SelectHandle`，本质就是**「match flavor，转发到底层 flavor 的 `sender()`/`receiver()` 句柄」**——这与 u1-l3 讲过的「按 flavor 分发」母题完全一致，只是这次分发的不是 `send`/`recv`，而是 select 的七个方法。

`Sender` 只有 3 个 flavor 变体（Array/List/Zero，因为 at/tick/never 没有发送方），`Receiver` 有 6 个变体（多出 At/Tick/Never 三个只读 flavor）。

#### 4.2.2 核心流程

```text
Sender::try_select(token)
   └─ match self.flavor { Array/Lst/Zero => chan.sender().try_select(token) }
                        └─ 转发到 flavors::xxx::Sender::try_select（真正的实现）
```

完成操作时，`SelectedOperation::recv`/`send` 会调用 `channel::read`/`channel::write` 这两个 `unsafe fn`，它们同样按 flavor 分发到底层的 `read`/`write`：

```text
SelectedOperation::recv(r)  =>  unsafe { channel::read(r, &mut token) }
                                └─ match r.flavor { Array/List/Zero => chan.read(token); At/Tick => transmute_copy; Never => chan.read }
```

#### 4.2.3 源码精读

**Sender 的 SelectHandle 分发**（[src/channel.rs:1386-1446](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1386-L1446)）——以 `register` 为例，三个 flavor 各转发一次：

```rust
fn register(&self, oper: Operation, cx: &Context) -> bool {
    match &self.flavor {
        SenderFlavor::Array(chan) => chan.sender().register(oper, cx),
        SenderFlavor::List(chan)  => chan.sender().register(oper, cx),
        SenderFlavor::Zero(chan)  => chan.sender().register(oper, cx),
    }
}
```

注意 `Sender::deadline` 恒返回 `None`（[src/channel.rs:1395-1397](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1395-L1397)）——发送方永远不靠时间驱动。

**Receiver 的 SelectHandle 分发**（[src/channel.rs:1448-1537](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1448-L1537)）——`try_select` 六分支（[src/channel.rs:1449-1458](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1449-L1458)），其中 Array/List/Zero 走 `chan.receiver().try_select`，At/Tick/Never 直接 `chan.try_select`（因为这些 flavor 本身就实现了 `SelectHandle`，没有内嵌的 sender/receiver 句柄）：

```rust
fn try_select(&self, token: &mut Token) -> bool {
    match &self.flavor {
        ReceiverFlavor::Array(chan) => chan.receiver().try_select(token),
        ReceiverFlavor::List(chan)  => chan.receiver().try_select(token),
        ReceiverFlavor::Zero(chan)  => chan.receiver().try_select(token),
        ReceiverFlavor::At(chan)    => chan.try_select(token),
        ReceiverFlavor::Tick(chan)  => chan.try_select(token),
        ReceiverFlavor::Never(chan) => chan.try_select(token),
    }
}
```

`Receiver::deadline` 则把 Array/List/Zero 返回 `None`、At/Tick/Never 返回各自的时间（[src/channel.rs:1460-1469](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1460-L1469)）——只有时间型 flavor 才会驱动 select 的定时唤醒。

**完成操作的两个 unsafe fn**（[src/channel.rs:1539-1565](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1539-L1565)）：

```rust
pub(crate) unsafe fn write<T>(s: &Sender<T>, token: &mut Token, msg: T) -> Result<(), T> {
    match &s.flavor {
        SenderFlavor::Array(chan) => chan.write(token, msg),
        SenderFlavor::List(chan)  => chan.write(token, msg),
        SenderFlavor::Zero(chan)  => chan.write(token, msg),
    }
}
```

它们是 `unsafe`，因为调用者必须保证：传入的 `token` 是由一次**成功的** `try_select`/`accept` 初始化的、且对应的 `Sender`/`Receiver` 与被选中的端是同一个。`read` 还要对 At/Tick 做 `mem::transmute_copy`（把底层真实的 `Result<Instant, ()>` 换装为泛型 `Result<T, ()>`，安全性靠构造函数钉死 `T == Instant`，见 u2-l1）。

**`addr()`——完成操作时的身份校验依据**。`Select` 在注册操作时记录每个端的 `addr`（指针地址），完成时 `SelectedOperation::recv`/`send` 会 `assert!(r.addr() == self.addr)`（[src/select.rs:1310-1317](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L1310-L1317)），确保你传给 `op.recv()` 的 `Receiver` 恰好是被选中的那个。`addr()` 同样按 flavor 分发（Sender：[src/channel.rs:665-671](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L665-L671)；Receiver：[src/channel.rs:1172-1181](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1172-L1181)），其中 At/Tick 用 `Arc::as_ptr(chan) as usize`、Never 返回 `0`（ZST，无地址）。

#### 4.2.4 代码实践：用 Select API 走完一次完整对接

1. **实践目标**：亲手走完「注册 → select → 完成」全流程，验证 `SelectedOperation` 必须完成。
2. **操作步骤**：新建一个二进制（示例代码），用 `Select` 从一个就绪的 `bounded(1)` 通道接收：

   ```rust
   // 示例代码
   use crossbeam_channel::{bounded, Select};

   fn main() {
       let (s, r) = bounded(1);
       s.send(42).unwrap();

       let mut sel = Select::new();
       let i = sel.recv(&r);                 // 注册操作（内部记下 r.addr()）

       let op = sel.select();                // run_select → fast path try_select 成功
       assert_eq!(op.index(), i);
       let msg = op.recv(&r).unwrap();       // 必须 recv 完成（否则 drop panic）
       assert_eq!(msg, 42);
   }
   ```

3. **需要观察的现象**：删掉 `op.recv(&r)` 这一行（让 `op` 被 drop），程序会 **panic**：`dropped SelectedOperation without completing the operation`。
4. **预期结果**：理解 `SelectedOperation` 携带的 `token`/`index`/`addr` 三个字段如何分别对应「订座凭证 / 操作编号 / 端身份」，以及为何「不完成就 panic」（避免占座泄漏导致死锁）。

#### 4.2.5 小练习与答案

- **练习 1**：`Sender` 的 `SelectHandle` 为什么只有 3 个分支，而 `Receiver` 有 6 个？
  - **答案**：at/tick/never 是只读通道，没有 `Sender`；它们直接在 `Receiver` 的 flavor 枚举里实现 `SelectHandle`（`chan.try_select`），无需 `chan.receiver()` 中间句柄。
- **练习 2**：`channel::read` 为什么是 `unsafe`？调用者要保证什么？
  - **答案**：`token` 必须来自一次成功的 `try_select`/`accept`，且传入的端与被选中的端是同一个（`addr` 校验只是运行时断言，编译期无法保证）。底层 `read` 会据此解引用 `token` 里的裸指针，乱传会触发未定义行为。

---

### 4.3 array/list flavor：就绪驱动的对接

#### 4.3.1 概念说明

array（有界环形缓冲）和 list（无界链表）都是「**有队列、有槽位**」的真实通道。它们的 `SelectHandle` 采用**就绪驱动**风格：

- `register` 只做一件事——把自己挂到阻塞者队列（`Waker`）上，等对端 `notify`；不预分配任何数据结构。
- 真正抢占槽位（CAS 推进 `head`/`tail`）发生在 `try_select`（fast path）和 `accept`（被唤醒后）。`accept` 在这两个 flavor 里**就是再调一次 `try_select`**——因为抢占是无锁 CAS，重试一次即可。
- Token 的 `array.slot`/`array.stamp`（或 list 的等价信息）在 `try_select` 成功的那一刻被填入。

#### 4.3.2 核心流程

以 array 的**接收**为例（[src/flavors/array.rs:613-647](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L613-L647)）：

```text
try_select(token)  → start_recv(token)        # CAS 推进 head，抢占一个槽，写 token.array.{slot,stamp}
register(oper,cx)  → receivers.register(...)  # 挂到 Waker；返回 is_ready()（空且未断开则 false）
unregister(oper)   → receivers.unregister(...)
accept(token,cx)   → try_select(token)        # 醒后重抢同一个槽
is_ready()         → !is_empty() || is_disconnected()
watch/unwatch      → receivers.watch / unwatch（observers 队列）
```

`ArrayToken` 就是一个携带「槽指针 + 目标 stamp」的小结构（[src/flavors/array.rs:41-47](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L41-L47)），是 `start_send`/`start_recv`（抢占）与 `write`/`read`（搬运）之间传递的凭证。

#### 4.3.3 源码精读

**array 的 Sender `SelectHandle`**（[src/flavors/array.rs:649-683](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L649-L683)）：

```rust
fn try_select(&self, token: &mut Token) -> bool { self.0.start_send(token) }  // CAS 抢占
fn register(&self, oper: Operation, cx: &Context) -> bool {
    self.0.senders.register(oper, cx);   // 只登记
    self.is_ready()                       // 返回是否已就绪（满且未断开 → false）
}
fn accept(&self, token: &mut Token, _cx: &Context) -> bool {
    self.try_select(token)                // 醒后重抢
}
fn is_ready(&self) -> bool { !self.0.is_full() || self.0.is_disconnected() }
```

`start_send` 用 CAS 推进 `tail` 抢占槽位（[src/flavors/array.rs:143-212](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L143-L212)），断开时把 `token.array.slot` 置空（搬运时据此返回 `Err`，见 u2-l5）。

**list 的差异——发送方恒就绪**。list 是无界通道，发送永不阻塞，所以它的 **Sender `register` 直接返回 `self.is_ready()`（恒为 `true`）**，甚至不登记（[src/flavors/list.rs:752-780](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L752-L780)）：

```rust
fn register(&self, _oper: Operation, _cx: &Context) -> bool { self.is_ready() }  // true
fn is_ready(&self) -> bool { true }
fn unregister(&self, _oper: Operation) {}                                        // 空操作
```

而 list 的 **Receiver** 仍需登记（[src/flavors/list.rs:716-750](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L716-L750)），因为接收方在通道空时要阻塞等待新消息。

> 对比要点：array/list 的 `accept` 都是 `self.try_select(token)` 的别名——重抢一次即可；而下一节的 zero flavor 的 `accept` 则完全不同。

#### 4.3.4 代码实践：阅读就绪驱动的 register/accept

1. **实践目标**：理解「register 只登记、accept 重抢」的就绪驱动模式。
2. **操作步骤**：对照 [src/flavors/array.rs:649-683](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L649-L683) 的 `Sender` impl，画出一次「满队列发送」在 select 中的方法调用序列：`try_select`(false) → `register`(返回 false，挂到 senders 队列) → park → 被 `recv` 侧 `notify` 唤醒 → `unregister` → `accept`(=`try_select`，这次 CAS 成功) → `channel::write`。
3. **需要观察的现象**：`register` 没有分配任何堆内存，只是 `self.0.senders.register(oper, cx)` 把一个 `Entry` 推进 `Waker::selectors`。
4. **预期结果**：你能解释为什么 array/list 不需要在 `register` 里准备 Token——因为抢占是「无状态的 CAS」，随时可以重做。

#### 4.3.5 小练习与答案

- **练习 1**：list 的 `Sender::register` 为什么连登记都不做、直接返回 `true`？
  - **答案**：无界通道发送永不阻塞（总有新 block 可分配），所以发送操作永远「就绪」。`register` 返回 `true` 会让 `run_select` 立刻 abort 并走 fast path `try_select`，发送当场完成。
- **练习 2**：array 的 `accept` 为什么可以直接等于 `try_select`？
  - **答案**：array 的抢占是幂等的 CAS——抢占成功就把 `token.array.{slot,stamp}` 填好，无论这次是 fast path 还是醒后的重试，逻辑完全一样。

---

### 4.4 zero flavor：packet 驱动的对接（与 array 对比）

#### 4.4.1 概念说明

zero（零容量会合）通道**没有队列、不缓冲任何消息**，发送与接收必须「人盯人」配对。它的 `SelectHandle` 不能像 array 那样「登记等通知、醒后再 CAS 抢槽」，因为根本没有槽可抢——它必须解决一个更难的问题：**如何让分处两个线程的 send 和 recv 共享同一个承载消息的 `Packet`？**

解法是 **packet 驱动**：

- `register` **预分配一个堆上的 `Packet`**，连同操作一起挂到阻塞者队列（用 `register_with_packet`）。
- 当对端（另一线程的 send/recv）来做 `try_select` 时，`Waker::try_select` 会 CAS 选中这个阻塞者、把它的 packet **直接递到阻塞线程的 `Context::packet` 槽**，并 `unpark` 唤醒它。
- 被唤醒的线程在 `accept` 里用 `cx.wait_packet()` **取回对端递来的 packet 指针**，写进 `token.zero.0`。后续 `channel::write`/`read` 就靠这个指针去搬运消息。

> 关键区别：array 的 Token 在**自己**的 `try_select` 里就算出来了；zero 的 Token（packet 指针）是在 `accept` 里**从对端手里拿**的——这是「会合」二字在源码层的落点。

#### 4.4.2 核心流程

以 zero 的**接收**为例（[src/flavors/zero.rs:393-441](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L393-L441)）：

```text
try_select(token) → start_recv(token)
                    └─ 锁 inner；若有等待的 sender → receivers.try_select() 配对，把 sender 的 packet 写进 token.zero.0
                       否则若断开 → token.zero.0 = null（搬运时返回 Err）
                       否则 → false
register(oper,cx) → ① Box::into_raw(空 packet)        # 预分配堆 packet
                    ② receivers.register_with_packet(oper, packet, cx)
                    ③ senders.notify()                 # 叫醒等待的 sender 来配对
                    ④ 返回 senders.can_select() || is_disconnected   # 是否已有 sender 可配对
accept(token,cx)  → token.zero.0 = cx.wait_packet()    # 阻塞等对端递来的 packet，返回 true
is_ready()        → senders.can_select() || is_disconnected   # 锁内查是否有等待的 sender
```

`start_recv`（[src/flavors/zero.rs:163-176](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L163-L176)）和 `start_send`（[src/flavors/zero.rs:134-147](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L134-L147)）里的 `inner.receivers.try_select()` / `inner.senders.try_select()` 就是配对枢纽——它返回一个 `Entry`，里面带着**对端预分配的 packet 指针**。

#### 4.4.3 源码精读

**zero 的 Receiver `register`——预分配 packet**（[src/flavors/zero.rs:402-411](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L402-L411)）：

```rust
fn register(&self, oper: Operation, cx: &Context) -> bool {
    let packet = Box::into_raw(Packet::<T>::empty_on_heap());   // ① 堆 packet
    let mut inner = self.0.inner.lock();
    inner.receivers.register_with_packet(oper, packet.cast::<()>(), cx);  // ② 挂上
    inner.senders.notify();                                     // ③ 叫醒等待的 sender
    inner.senders.can_select() || inner.is_disconnected        // ④
}
```

为什么必须堆分配？因为 register 与 accept 分离：register 在「准备 park」时调，accept 在「被唤醒后」调，中间隔着 park；而 select 还可能 cancel（超时/被别的分支抢走）——packet 必须能在两个方法间存活、且能被任一方释放，所以只能在堆上（普通 send/recv 在同一栈帧则用栈 packet 零分配，见 u2-l7）。`unregister` 负责在取消时 `Box::from_raw` 回收这个 packet（[src/flavors/zero.rs:413-419](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L413-L419)）。

**zero 的 Receiver `accept`——取回对端递来的 packet**（[src/flavors/zero.rs:421-424](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L421-L424)）：

```rust
fn accept(&self, token: &mut Token, cx: &Context) -> bool {
    token.zero.0 = cx.wait_packet();   // 阻塞自旋直到对端把 packet 存进来
    true
}
```

`cx.wait_packet()`（[src/context.rs:129-138](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs#L129-L138)）自旋 `Acquire` 读 `Context::packet` 槽，直到非空。这个槽是由**对端线程**在对端的 `Waker::try_select` 里通过 `selector.cx.store_packet(selector.packet)` 填入的（[src/waker.rs:84-111](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L84-L111)）。

**配对枢纽 `Waker::try_select`**（[src/waker.rs:84-111](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L84-L111)）一次完成三件事：CAS 选中（`try_select(Selected::Operation)`）、递 packet（`store_packet`）、唤醒（`unpark`）。它只挑「别的线程」的 entry（`selector.cx.thread_id() != thread_id`），避免自己选自己。

**搬运阶段 `write`/`read`**（[src/flavors/zero.rs:150-202](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L150-L202)）就靠 `token.zero.0` 这个 packet 指针：`write` 写入消息并 `ready.store(true, Release)`；`read` 若是堆 packet 则 `wait_ready` 等可见、读完 `Box::from_raw` 释放（栈 packet 路径见 u2-l7）。null 指针表示断开，搬运时直接返回 `Err`。

#### 4.4.4 代码实践：对照 array 与 zero 的 register/accept

1. **实践目标**：亲手对照两种 flavor 的 `register`/`accept`，解释它们如何为 select 准备 Token。
2. **操作步骤**：
   - 打开 array 的 `register`（[src/flavors/array.rs:658-661](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L658-L661)）与 zero 的 `register`（[src/flavors/zero.rs:452-461](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L452-L461)），逐行比较。
   - 打开 array 的 `accept`（[src/flavors/array.rs:667-669](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L667-L669)）与 zero 的 `accept`（[src/flavors/zero.rs:471-474](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L471-L474)），逐行比较。
3. **需要观察的现象（填写下表）**：

   | 维度 | array Sender | zero Sender |
   | --- | --- | --- |
   | register 是否分配堆内存 | 否（只 `senders.register`） | 是（`Box::into_raw(Packet)`） |
   | Token 何时填好 | `try_select`（自己 CAS） | `accept`（`cx.wait_packet()` 取对端的） |
   | accept 的实现 | `self.try_select(token)`（重抢） | `token.zero.0 = cx.wait_packet()`（取回） |
   | 配对发生在哪 | 无配对（共享队列） | `Waker::try_select`（人盯人） |

4. **预期结果**：你能用一句话总结——「array 的 Token 是自己抢出来的，zero 的 Token 是对端递过来的」。这正是有界队列与会合通道在 select 对接上的根本分野。

#### 4.4.5 小练习与答案

- **练习 1**：zero 的 `register` 为什么要 `senders.notify()`（接收方登记却叫醒发送方）？
  - **答案**：接收方登记后，「等待发送方」变成了「我能配对了」。如果有发送方正阻塞在 select 里等待接收方，这条 `notify` 会唤醒它，让它来做 `try_select` 配对。
- **练习 2**：如果 zero 的 `register` 不预分配 packet，直接在 `accept` 里分配会怎样？
  - **答案**：会破坏配对协议。对端 `Waker::try_select` 在配对时需要把「被选中方的 packet」递过去（`store_packet(selector.packet)`）；如果 packet 在 `accept` 里才分配，对端配对时拿不到 packet，无法完成递包。packet 必须在登记时就和 Entry 绑定。

---

### 4.5 时间型 flavor 与 internal 隐藏模块

#### 4.5.1 概念说明

at/tick/never 三个 flavor 都是**只读、消息按需生成**的通道（u2-l8）。它们的 `SelectHandle` 实现有一个共同特征：**`register`/`unregister`/`watch`/`unwatch` 都是空操作或仅返回 `is_ready()`，从不真正把自己挂进 `Waker` 队列**。它们驱动 select 完全靠 `deadline()` 返回一个时间点——`run_select` 据此计算 park 截止时间，到点醒来后用 `try_select` 惰性生成消息。

而 `internal` 模块是 `select!` 宏的「后门」：宏展开后需要调用 `run_select`/`try_select`/`select_timeout` 这几个底层函数，还要用 `receiver_addr`/`sender_addr` 取端的地址——这些不是给用户调用的公开 API，但宏必须能按绝对路径 `$crate::internal::xxx` 引用到，所以用 `#[doc(hidden)]` 暴露但隐藏文档。

#### 4.5.2 核心流程

**时间型 flavor 的统一对接模式**（以 at 为例，[src/flavors/at.rs:143-194](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/at.rs#L143-L194)）：

```text
try_select(token) → try_recv() 取消息（到了则填 token.at，没到则 false）
deadline()        → Some(delivery_time)          # 驱动 select 定时唤醒
register(_,_)     → is_ready()                   # 空登记，仅报就绪
unregister(_,_)   → {}                           # 无事可清理
accept(token,_)   → try_select(token)            # 醒后再试一次取消息
is_ready()        → !is_empty()                  # 到点了就就绪
```

**internal 模块的导出**（[src/lib.rs:368-375](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs#L368-L375)）：

```rust
#[doc(hidden)]
#[cfg(feature = "std")]
pub mod internal {
    pub use crate::select::{
        SelectHandle, receiver_addr, select, select_timeout, sender_addr, try_select,
    };
}
```

#### 4.5.3 源码精读

**at 的 `deadline`——驱动定时唤醒的源头**（[src/flavors/at.rs:159-167](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/at.rs#L159-L167)）：

```rust
fn deadline(&self) -> Option<Instant> {
    if self.received.load(Ordering::Relaxed) { None }   // 已投递则不再有截止时间
    else { Some(self.delivery_time) }
}
```

`run_select` 收集所有 handle 的 `deadline` 取最小值（[src/select.rs:256-260](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L256-L260)），用这个时间 `park_timeout`。到点醒来后，`try_select` 调 `try_recv`，此刻 `Instant::now() >= delivery_time` 成立，消息被惰性生成并填进 `token.at`。tick（[src/flavors/tick.rs:163-209](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/tick.rs#L163-L209)）机制相同，只是 `deadline` 每次返回「下一个投递时刻」。

**never 的 `try_select` 恒 `false`**（[src/flavors/never.rs:76-112](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/never.rs#L76-L112)），`deadline` 恒 `None`、`is_ready` 恒 `false`——它在 select 中完全「透明」，永远不就绪、不驱动唤醒。它的作用是把 `default(timeout)` 之外的可选超时统一成一个固定结构的分支（u2-l8）。

**internal 模块：宏的唯一调用方**。`select!`/`select_biased!` 展开后会按 `$crate::internal::xxx` 绝对路径调用这些项（[src/select_macro.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs)）：

- `internal::select(&mut sel, _IS_BIASED)`（[L763](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L763)）——无 default 分支的阻塞 select；
- `internal::try_select(&mut sel, _IS_BIASED)`（[L785](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L785)）——`default` 立即分支；
- `internal::select_timeout(&mut sel, $timeout, _IS_BIASED)`（[L815](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L815)）——`default(timeout)` 分支；
- `internal::receiver_addr($var)` / `sender_addr($var)`（[L865](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L865)、[L897](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L897)）——往 `sel` 数组里填三元组的地址字段；
- `internal::SelectHandle`（[L698](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L698)）——把 `never::<()>()` 强制成 `&dyn SelectHandle` 当占位 handle。

#### 4.5.4 代码实践：解释 internal 模块为何 #[doc(hidden)]

1. **实践目标**：理解 `internal` 模块的导出策略——「公开给宏、隐藏于用户」。
2. **操作步骤**：
   - 阅读 [src/lib.rs:368-375](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/lib.rs#L368-L375)，注意它同时带 `pub mod`（对外可见）和 `#[doc(hidden)]`（文档隐藏）、且 `#[cfg(feature = "std")]`（无 std 不可用）。
   - 在 [src/select_macro.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs) 里 grep `internal::`，确认每个导出项都被宏用 `$crate::internal::` 绝对路径引用。
3. **需要观察的现象与思考题**：为何不把这些函数直接做成 `pub` 放在顶层？为何需要 `receiver_addr`/`sender_addr` 这种看似多余的包装（它们只是调 `s.addr()`，而 `addr()` 是 `pub(crate)`）？
4. **预期结果**（参考答案）：
   - **不能做成公开 API**：`select`/`try_select`/`select_timeout` 接受 `&mut [(&dyn SelectHandle, usize, usize)]`，`SelectedOperation` 必须手动完成否则 panic——这是给宏精确编排用的危险原语，普通用户应改用 `Select` 类型或 `select!` 宏。`#[doc(hidden)]` 让它们不出现在 rustdoc、不被当成稳定 API，保留随时重构的自由。
   - **`receiver_addr`/`sender_addr` 的必要性**：`addr()` 是 `pub(crate)`，跨 crate 的宏（宏在用户 crate 里展开）无法调用 `pub(crate)` 方法；`internal` 里的 `sender_addr`/`receiver_addr` 是 `pub fn`，把 `pub(crate) fn addr` 包了一层公开出去，**只给宏用**。这就是「内部细节必须经 internal 模块重新公开」的原因。

#### 4.5.5 小练习与答案

- **练习 1**：at flavor 的 `register` 不挂 `Waker`，那 select 怎么知道何时该醒？
  - **答案**：靠 `deadline()` 返回 `Some(delivery_time)`。`run_select` 把它纳入 park 截止时间的计算，到点 `park_timeout` 醒来后再 `try_select` 惰性生成消息。
- **练习 2**：为什么 `internal` 模块要 `#[cfg(feature = "std")]`？
  - **答案**：`select!` 宏依赖线程阻塞（`thread::park`）、`Instant` 等 std 能力，而整个 crate 是 `no_std` 友好的。无 std 时 select 不可用，宏的后门自然也不导出。

---

## 5. 综合实践

把本讲三条主线（trait 契约、两种对接风格、internal 后门）串起来，做一个**源码阅读 + 行为观察**的综合任务。

**任务**：用 `select!` 写一个「会合通道配对」程序（改编自 [examples/matching.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/examples/matching.rs)），多个线程在同一个 `bounded(0)` 通道上同时尝试 send 或 recv，然后**对照源码解释每一步发生了什么**。

```rust
// 示例代码（改自 examples/matching.rs）
use crossbeam_channel::{bounded, select};
use crossbeam_utils::thread;

fn main() {
    let people = vec!["Anna", "Bob", "Cody", "Dave", "Eva"];
    let (s, r) = bounded(0); // 零容量会合通道

    let seek = |name, s, r| {
        select! {
            recv(r) -> peer => println!("{} received from {}.", name, peer.unwrap()),
            send(s, name) -> _ => {}, // 等别人来取我的消息
        }
    };

    thread::scope(|scope| {
        for name in people {
            let (s, r) = (s.clone(), r.clone());
            scope.spawn(move |_| seek(name, s, r));
        }
    }).unwrap();
}
```

**结合源码解释（请逐项填写）**：

1. 宏展开后，`select!` 会调用 `internal::select`（[src/select_macro.rs:763](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L763)），它内部走 `run_select`。
2. 对每个 `recv(r)`/`send(s,_)` 分支，宏用 `internal::receiver_addr`/`sender_addr`（[L865](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L865)/[L897](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select_macro.rs#L897)）取地址，构建 handle 三元组。
3. 由于是 zero flavor，每个分支的 `register`（[src/flavors/zero.rs:402-411](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L402-L411)）会**预分配堆 packet** 并 `notify` 对端。
4. 当一个线程的 send 与另一线程的 recv 在 `Waker::try_select`（[src/waker.rs:84-111](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L84-L111)）配对成功，胜者的 `accept`（[src/flavors/zero.rs:421-424](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L421-L424)）用 `cx.wait_packet()` 取回 packet 指针。
5. 最后 `SelectedOperation::send`/`recv` 调 `channel::write`/`read`（[src/channel.rs:1539-1565](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1539-L1565)）完成消息搬运，并用 `mem::forget(self)` 阻止 Drop 的 panic（[src/select.rs:1276-1318](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L1276-L1318)）。

**运行与观察**：

```bash
cargo run --example matching
```

由于 5 个人是奇数，必有一人的 `send` 无人配对，会一直阻塞——这正是会合通道的特性。结合源码回答：这个「没人配对」的线程此刻卡在哪一行？（答案：`accept` 里的 `cx.wait_packet()` 自旋，或 `run_select` 的 `cx.wait_until` park。）

## 6. 本讲小结

- **`SelectHandle` 是算法与 flavor 的唯一接口**：七个方法（`try_select`/`register`/`unregister`/`accept`/`deadline` 与 `is_ready`/`watch`/`unwatch`）分别服务于 `run_select` 与 `run_ready` 两套内核。
- **channel.rs 只做分发**：`Sender`/`Receiver` 的 `SelectHandle` 与 `unsafe write/read` 都是「match flavor 转发」，外加 `addr()` 供完成操作时做端身份校验。
- **array/list 是就绪驱动**：`register` 仅登记 `Waker`、`accept` 就是重抢一次 `try_select`，Token 在自己抢占时算出；list 的发送方甚至恒就绪、不登记。
- **zero 是 packet 驱动**：`register` 预分配堆 packet，配对由 `Waker::try_select` 完成，`accept` 用 `cx.wait_packet()` 取回对端递来的 packet——Token 是「对端递过来的」。
- **at/tick/never 靠 deadline 驱动**：不登记 `Waker`，用 `deadline()` 让 `run_select` 定时唤醒，醒来后 `try_select` 惰性生成消息；never 在 select 中完全透明。
- **internal 是宏的后门**：`#[doc(hidden)]` 把 `select`/`try_select`/`select_timeout`/`sender_addr`/`receiver_addr`/`SelectHandle` 暴露给 `select!` 宏按 `$crate::internal::` 路径调用，既隐藏于用户文档、又绕过 `pub(crate)` 跨 crate 调用的限制。

## 7. 下一步学习建议

- **下一讲（u3-l3）**：进入 `select!` 宏的展开机制，看宏的解析阶段（`@list`/`@case`）与代码生成阶段（`@init`/`@count`/`@add`/`@complete`）如何把语法编译成本讲讲的 `internal::select` 调用与 handle 数组，以及单 `recv` 分支如何被优化为直接的 `recv()`。
- **巩固建议**：回头重读 u2-l5（array 的 `start_send`/`write`/`read`）与 u2-l7（zero 的栈/堆 packet），把「抢占—搬运两步走」与本讲的「try_select—accept—write/read 三步走」对应起来。
- **进阶阅读**：通读 [src/waker.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs) 的 `try_select`/`register_with_packet`/`can_select` 三个方法，体会 zero 配对协议中「CAS 选中—递包—唤醒」原子三连的设计；并对照 [tests/select_macro.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/tests/select_macro.rs) 中针对各 flavor 的 select 测试，验证你对对接机制的理解。
