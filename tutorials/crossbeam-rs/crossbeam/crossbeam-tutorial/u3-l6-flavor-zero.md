# zero flavor：零容量会合通道

## 1. 本讲目标

本讲剖析 `crossbeam-channel` 中最特殊的一种通道——零容量通道（zero-capacity channel），也叫**会合通道（rendezvous channel）**，由 `bounded(0)` 创建。

读完本讲你应当能够：

- 说清楚「零容量」为何等价于「无缓冲」，以及为什么发送者在接收者出现之前无法把消息「放下就走」。
- 理解 `Packet<T>` 这个一次性交接容器的作用，区分**栈上 packet**与**堆上 packet**两条路径。
- 读懂 `start_send` / `start_recv` 的配对协议，以及配对成功后双方如何通过 `ready` 标志与 `unpark` **相互唤醒**。
- 把本讲与 u3-l3（flavors 架构与 `SelectHandle`）、u3-l7（Context 与 Waker）串起来，理解 zero flavor 如何嵌入统一的 select 体系。

---

## 2. 前置知识

本讲假设你已经掌握（来自前置讲义）：

- **flavor 派发架构**（u3-l3）：`SenderFlavor` / `ReceiverFlavor` 枚举把 `send`/`recv` 路由到具体 flavor；所有 flavor 都实现统一的 `SelectHandle` trait；`Token` 是各 flavor 在一次操作中传递状态的「集装箱」。
- **引用计数与销毁**（u3-l2）：每个端持有一份 `Counter`，`release` 时调用 flavor 的 `disconnect_senders`/`disconnect_receivers`/`disconnect`。zero flavor 的两端都调用同一个 `disconnect()`。
- **阻塞唤醒的大致分工**（u3-l7 预习）：`Context` 是线程局部的「选择上下文」，`wait_until` 让线程睡下，`unpark` 唤醒它；`Waker` 管理等待中的操作队列。本讲会用到其中的 `try_select`、`register_with_packet`、`store_packet`、`wait_packet` 等方法，用到时会展开讲。

两个本讲会反复出现的术语：

- **会合（rendezvous）**：两个线程必须同时在场才能完成一次交接，像接力赛中「交棒」那一刻——交棒人和接棒人缺一不可。
- **交接容器（packet）**：临时承载一条消息的内存槽，消息从发送者的 packet 流向接收者，交接完即销毁。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `crossbeam-channel/src/flavors/zero.rs` | zero flavor 的全部实现：`Channel`、`Packet`、`ZeroToken`、`start_send`/`start_recv`、阻塞 `send`/`recv`、`SelectHandle` 实现。本讲主角。 |
| `crossbeam-channel/src/waker.rs` | 等待队列 `Waker`：`register_with_packet` 注册、`try_select` 配对并唤醒、`disconnect` 批量唤醒。zero 的 `Inner` 直接持两个 `Waker`。 |
| `crossbeam-channel/src/context.rs` | 线程局部 `Context`：`wait_until` 阻塞、`store_packet`/`wait_packet` 传递 packet 指针、`try_select` 原子选定操作。 |
| `crossbeam-channel/src/select.rs` | `Token`/`Operation`/`Selected` 与 `SelectHandle` trait 的定义。zero 的 `ZeroToken` 是 `Token` 的一个字段。 |
| `crossbeam-channel/src/channel.rs` | 公共入口：`bounded(0)` 构造 zero channel，`SenderFlavor::Zero`/`ReceiverFlavor::Zero` 派发。 |

---

## 4. 核心概念与源码讲解

### 4.1 零容量与会合语义：为什么发送者不能先把消息放进缓冲

#### 4.1.1 概念说明

先回忆有界通道（u3-l4 的 array flavor）：它有一个容量为 `cap` 的环形缓冲，发送者只要缓冲没满，就可以「放下消息就走」，不必等接收者。这是一种**解耦**——生产者和消费者在时间上不必同步。

零容量通道走另一个极端：缓冲大小为 0。这意味着**根本没有地方暂存消息**。于是发送者只有在一个接收者愿意当场接手时，才能完成 `send`；反之亦然。这就是「会合」语义：`send` 和 `recv` 必须在时间上**对齐**才能成交。

这带来一个直接推论：**发送者不能先把消息放进缓冲然后返回**，因为缓冲不存在。它必须等到接收者真正把消息取走（或至少确认接收者在场并已接手）之后才能返回。这一点是 zero flavor 全部设计的出发点。

#### 4.1.2 核心流程

zero flavor 的「永远」不变量，直接编码在源码里：

- `len()` 永远返回 `0`——通道里从不存留消息。
- `capacity()` 返回 `Some(0)`。
- `is_empty()` 永远返回 `true`。
- `is_full()` 永远返回 `true`——既然容量是 0，「已有 0 条」就等于「满了」，所以任何没有接收者在场的 `try_send` 都会得到 `Full`。

于是 `try_send` 的语义被压缩成两种：要么恰好有接收者在等（立刻成交），要么返回 `Full`（没有缓冲可放）或 `Disconnected`。

```
try_send(msg):
  若有等待中的 receiver  → 配对，写入对方 packet，返回 Ok
  否则若已断开           → 返回 Disconnected(msg)
  否则                   → 返回 Full(msg)   ← 关键：绝不暂存
```

#### 4.1.3 源码精读

先看构造入口。`bounded(0)` 在 `channel.rs` 里就是一个分支判断，`cap == 0` 时创建 zero channel：

[channel.rs:113-122](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L113-L122) — `bounded(0)` 走 `flavors::zero::Channel::new()`，包进 `SenderFlavor::Zero` / `ReceiverFlavor::Zero`。

再看 zero 自己那四个「永远」方法，它们用最直白的方式宣告「无缓冲」：

[zero.rs:366-384](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L366-L384) — `len()` 返回 `0`、`capacity()` 返回 `Some(0)`、`is_empty()` 与 `is_full()` 都返回 `true`。

最后看 `try_send`，它把上面的流程图一字不差地写成代码：

[zero.rs:205-222](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L205-L222) — 先 `inner.receivers.try_select()` 找等待中的接收者；找不到且未断开时返回 `TrySendError::Full(msg)`，**原消息带回**（错误类型内嵌消息的设计见 u3-l2）。

#### 4.1.4 代码实践

1. **目标**：亲眼看到 zero 通道「没有缓冲」。
2. **步骤**：写一段小程序，创建 `(s, r) = bounded::<i32>(0)`，在不启动任何接收线程的情况下调用 `s.try_send(1)`。
3. **观察**：返回值是 `Err(TrySendError::Full(1))`。换成 `bounded(1)`（array flavor），同样的 `try_send` 会返回 `Ok(())`。
4. **预期结果**：zero 永远 `Full`，array 在未满时成功。这验证了「零容量 = 无暂存」。

```rust
// 示例代码：对照 zero 与 array 的 try_send 行为
use crossbeam_channel::{bounded, TrySendError};

fn main() {
    let (zs, _zr) = bounded::<i32>(0);
    let (as_, _ar) = bounded::<i32>(1);

    match zs.try_send(1) {
        Err(TrySendError::Full(1)) => println!("zero: 无缓冲，消息被带回"),
        other => println!("zero: 意外结果 {:?}", other),
    }
    match as_.try_send(1) {
        Ok(()) => println!("array: 已放入缓冲"),
        other => println!("array: 意外结果 {:?}", other),
    }
}
```

> 说明：以上为示例代码，运行结果待本地验证。

#### 4.1.5 小练习与答案

**练习 1**：`is_empty()` 和 `is_full()` 都返回 `true`，这矛盾吗？
**答案**：不矛盾。容量为 0 时，「当前消息数 0」既满足「为空（0 == 0）」也满足「已满（0 >= 0）」。这是一个边界真值，不是 bug。

**练习 2**：为什么 zero 的 `try_send` 在没有接收者时返回 `Full` 而不是阻塞？
**答案**：`try_send` 的契约就是「绝不阻塞」。zero 没有缓冲可放，又不能等，于是只能如实报告「满了（没法接收）」。

---

### 4.2 Packet 与 ZeroToken：一次性交接容器

#### 4.2.1 概念说明

既然没有缓冲，那消息在「成交」的一瞬间放在哪？答案是放在一个叫 **Packet** 的临时容器里。你可以把它想象成接力赛里的那根接力棒：交棒人把棒握在手里（packet 里装着消息），接棒人接过棒（读出消息），然后棒就完成了使命。

`Packet<T>` 有三个字段：

- `on_stack: bool`——这个 packet 是在某个线程的**栈**上，还是在**堆**上。
- `ready: AtomicBool`——交接的就绪标志，是双方同步的「信号灯」。
- `msg: UnsafeCell<Option<T>>`——真正装消息的槽。

为什么区分栈和堆？因为这关系到「谁负责销毁 packet」以及「读消息前要不要等」：

- **栈 packet**：由阻塞中的 `send`/`recv` 直接创建在自己的栈帧上。它的生命周期由创建它的那个线程的栈帧天然保证——线程从函数返回前 packet 一定还活着。
- **堆 packet**：由 select 宏路径（`SelectHandle::register`）创建，因为 select 机制是通用的，packet 要跨多个抽象层传递，必须堆分配并由明确的一方 `Box::from_raw` 回收。

而 `ZeroToken` 就是 packet 的指针（`*mut ()`），作为 `Token` 结构体的一个字段，在一次操作中把「成交的那个 packet」从配对阶段带到读写阶段：

[zero.rs:26-32](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L26-L32) — `ZeroToken(*mut ())`，默认空指针。它是 `Token.zero` 字段的类型。

`Token` 是所有 flavor 共用的「集装箱」，zero 只占用其中的 `zero` 字段：

[select.rs:24-32](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L24-L32) — `Token` 各字段对应不同 flavor，`pub(crate) zero: flavors::zero::ZeroToken`。

#### 4.2.2 核心流程

`Packet` 提供三种构造方式，分别对应不同场景：

| 构造方法 | 位置 | 是否含消息 | 用途 |
| --- | --- | --- | --- |
| `message_on_stack(msg)` | 栈 | 是 | 阻塞 `send`：发送者先到，带着消息等 |
| `empty_on_stack()` | 栈 | 否 | 阻塞 `recv`：接收者先到，空手等 |
| `empty_on_heap()` | 堆 | 否 | select 宏路径：通用交接 |

`ready` 标志是交接同步的核心，配合 `wait_ready()` 形成一个**自旋等待**：

```
wait_ready():
  while !ready.load(Acquire):   # 用 Backoff::snooze 退避（见 u2-l1）
    snooze()
```

写方写完消息后 `ready.store(true, Release)`；读方读到消息后（栈路径）也 `ready.store(true, Release)` 来通知对方「我取走了，你可以销毁 packet 了」。`Release`/`Acquire` 配对保证消息写入对另一侧可见。

#### 4.2.3 源码精读

`Packet` 结构与构造方法：

[zero.rs:41-78](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L41-L78) — 三个字段与三种构造。注意 `msg` 用 `UnsafeCell` 提供内部可变性，因为同一 packet 会被发送线程写、接收线程读（受 `ready` 与互斥锁保护）。

`wait_ready` 的自旋等待（复用了 u2-l1 的 `Backoff::snooze`）：

[zero.rs:80-86](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L80-L86) — 用 `Acquire` 读 `ready`，配合写方的 `Release` 建立 happens-before。

两个写入/读出方法 `write` 与 `read`，它们操作的都是「对方」的 packet（指针由 token 带入）：

[zero.rs:150-160](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L150-L160) — `write`：若指针为空说明已断开（带回消息）；否则写入消息并置 `ready=true`。

[zero.rs:179-202](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L179-L202) — `read`：按 `on_stack` 分两条路。栈路径「消息一开始就在」，立即读出并置 `ready=true`（通知发送者可销毁）；堆路径先 `wait_ready()`（等 select 宏路径的写方写入），读出后 `Box::from_raw` 销毁堆 packet。

#### 4.2.4 代码实践

1. **目标**：理解「先到者带 packet 等待」的两种姿态。
2. **步骤**：只读源码——在 `send`（[zero.rs:250](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L250)）里找到 `message_on_stack(msg)`；在 `recv`（[zero.rs:319](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L319)）里找到 `empty_on_stack()`。
3. **观察**：发送者先到时带的是「装着消息的 packet」；接收者先到时带的是「空 packet」。这正是「谁先到谁提供 packet」的对称设计。
4. **预期结果**：你能用自己的话说明：为什么发送者的 packet 里要有消息，而接收者的 packet 是空的。
   - 参考答案：发送者手里有消息要交，所以 packet 装着消息等接收者来取；接收者手里没东西，只是「占个位」等发送者往里放，所以 packet 是空的。

#### 4.2.5 小练习与答案

**练习 1**：栈 packet 为什么不需要像堆 packet 那样在 `read` 里 `Box::from_raw` 销毁？
**答案**：栈 packet 的生命周期由创建它的线程的栈帧管理——函数返回时自动回收。`read` 在栈路径只需置 `ready=true` 通知对方「我读完了，你可以返回了」，对方一返回栈帧自然销毁 packet。

**练习 2**：`ready` 标志为什么必须用 `AtomicBool` 而不是普通 `bool`？
**答案**：因为 `ready` 被两个线程访问——写方 store、读方 load。普通 `bool` 并发读写是数据竞争（UB）。`Release`/`Acquire` 配对还顺带保证了 `msg` 的写入对另一侧可见。

---

### 4.3 start_send / start_recv：配对协议

#### 4.3.1 概念说明

zero 通道的成交，本质是「在两条等待队列里找一个人配对」。`Channel` 内部持有一个被互斥锁保护的 `Inner`，里面有两份 `Waker`（等待队列）：

- `senders`：正在等待接收者的**发送者**队列。
- `receivers`：正在等待发送者的**接收者**队列。

`start_send` 与 `start_recv` 是配对的「探测 + 取人」操作，主要用于 select 体系的 `try_select` 路径（即「不阻塞，看现在能不能立刻成交」）。它们的逻辑高度对称：

- `start_send`：到 `receivers` 队列里挑一个等待中的接收者，把它的 packet 指针写进 token；若没有接收者但已断开，token 置空返回成功（表示「成交了，但是成交对象是『断开』」）。
- `start_recv`：到 `senders` 队列里挑一个等待中的发送者，同理。

#### 4.3.2 核心流程

```
start_send(token):
  lock(inner)
  if receivers.try_select() 拿到某个 operation:
      token.zero = operation.packet     # 对方(接收者)的 packet 指针
      return true                       # 配对成功
  else if is_disconnected:
      token.zero = null                 # 用空指针表示「断开成交」
      return true
  else:
      return false                      # 暂时无法成交，调用方需阻塞等待
```

关键点：**配对发生在持锁期间**。`receivers.try_select()` 会在队列里原子地选定一个属于**别的线程**的操作、唤醒对方、并返回对方的 `Entry`（含 packet 指针）。锁保证「一个等待者不会被两个到达者同时配对」。

`token.zero` 的三种取值含义：
- 一个非空 packet 指针 → 正常成交，后续 `write`/`read` 操作这个 packet。
- `null` → 通道已断开，`write` 会 `Err(msg)` 带回消息、`read` 会 `Err(())`。
- `start_*` 返回 `false` → 还没成交，调用方走阻塞路径。

#### 4.3.3 源码精读

`Inner` 与 `Channel` 的结构：

[zero.rs:89-108](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L89-L108) — `Inner{senders, receivers, is_disconnected}`，外层 `Mutex<Inner>`。注意这里用的是非 `Sync` 的普通 `Waker`，靠外层互斥锁保护并发（zero 是唯一把全部状态塞进一把锁的 flavor，因为它没有缓冲可以做无锁优化，唯一的共享状态就是这两条等待队列）。

`start_send`（`start_recv` 完全对称，只是把 `receivers` 换成 `senders`）：

[zero.rs:134-147](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L134-L147) — 配对或断开两条成功路径，否则返回 `false`。

再看 `Waker::try_select` 究竟怎么「挑人 + 唤醒」：

[waker.rs:84-111](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L84-L111) — 它遍历 `selectors`，挑一个 `thread_id != 当前线程` 且能 `cx.try_select(Operation(...))` 成功的项；成功后 `cx.store_packet(packet)`（把 packet 指针塞进对方 Context）+ `cx.unpark()`（唤醒对方），然后从队列移除该项并返回。`cx.try_select` 内部是 CAS（[context.rs:98-109](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs#L98-L109)），保证一个等待者只被选定一次。

`SelectHandle` 实现把 `try_select` 委托给 `start_send`/`start_recv`：

[zero.rs:443-446](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L443-L446) — `Sender::try_select` 调 `start_send`；接收端同理（[zero.rs:393-396](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L393-L396)）。

#### 4.3.4 代码实践

1. **目标**：把「配对发生在持锁期间」这条不变性看明白。
2. **步骤**：对照 [zero.rs:134-147](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L134-L147) 的 `start_send` 与 [waker.rs:84-111](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L84-L111) 的 `try_select`，画出「到达者持 `inner` 锁 → 在 `receivers` 里 CAS 选定一个等待者 → store_packet + unpark → 放锁」的时序。
3. **观察**：`try_select` 整个过程都在 `start_send` 持有的 `inner` 锁内执行。
4. **预期结果**：你能解释——假如没有这把锁，两个发送者同时到达、同时 `try_select` 同一个接收者，会发生什么？（答：可能两次都 CAS 失败或一方成功；但有锁 + CAS 双保险后，每个接收者恰好被一个发送者配对。）
   - 说明：CAS 本身已能防止重复选定，外层锁进一步把「遍历队列」这一非原子步骤串行化，简化正确性推理。

#### 4.3.5 小练习与答案

**练习 1**：`start_send` 返回 `true` 但 `token.zero` 是 `null`，代表什么？
**答案**：代表通道已断开。这是一个「成交」——只不过成交对象是「断开状态」，所以后续 `write` 看到 null 会立刻 `Err(msg)` 把消息带回，返回 `Disconnected`。

**练习 2**：为什么 `Waker::try_select` 要跳过 `thread_id == 当前线程` 的项？
**答案**：因为 select 允许同一个线程同时在同一通道上 `send` 和 `recv`（自交）。如果允许和「自己」配对，会导致自己唤醒自己却无法真正完成交接（自己还阻塞着）。跳过自身保证配对一定发生在两个不同线程之间。

---

### 4.4 阻塞 send/recv 与相互唤醒

#### 4.4.1 概念说明

`start_send`/`start_recv` 只解决「现在能不能立刻成交」。当返回 `false`（没等到对方，也没断开）时，调用方就得**阻塞等**。本模块讲阻塞 `send`/`recv` 的完整流程，以及成交后双方如何**相互唤醒**。

核心是「先到者注册进队列并睡下，后到者把先到者挑出来并唤醒」。这正好呼应 u2-l5 提出的「谁置位谁唤醒」配对原则——这里「置位」就是 `try_select` 选定操作，「唤醒」就是 `cx.unpark()`。

#### 4.4.2 核心流程

以**发送者先到**为例（`send` 的慢路径）：

```
send(msg) 慢路径:
  1. 快路径失败（无等待接收者，未断开）
  2. Context::with: 取线程局部上下文 cx
  3. packet = message_on_stack(msg)        # 栈 packet，装着消息
  4. senders.register_with_packet(oper, &packet, cx)  # 把自己挂进 senders 队列
  5. receivers.notify()                     # 唤醒任何「关注接收端就绪」的观察者
  6. 放锁；sel = cx.wait_until(deadline)    # 自己睡下
  --- 此时发送者阻塞，等待接收者来配对 ---
  7. 被唤醒后按 sel 分支:
     Operation(_) → packet.wait_ready()     # 等接收者读完置 ready
                    返回 Ok
     Disconnected → unregister，取回 msg，返回 Disconnected
     Aborted      → unregister，取回 msg，返回 Timeout
```

而**接收者后到**时，它的 `recv`（或 `try_recv`）会调用 `senders.try_select()`，后者在持锁期间：

- `cx.try_select(Operation(...))` 把发送者的状态从 `Waiting` CAS 成 `Operation`；
- `store_packet(packet)` 把发送者的栈 packet 指针塞进发送者的 Context（发送者慢路径其实不读它，但 select 宏路径要用，代码统一）；
- `unpark()` 唤醒发送者；
- 返回该 `Entry`，接收者拿到 `operation.packet`，进入 `read` 的栈路径：**立即读出消息**，并 `ready.store(true, Release)`。

发送者被唤醒后走到 `Operation(_)` 分支，执行 `packet.wait_ready()`——由于接收者已经置了 `ready`，这里通常立刻通过——然后返回 `Ok(())`。**发送者此时才允许返回并销毁自己的栈 packet**。这就是会合保证：发送者绝不会在接收者读走消息之前返回（否则接收者会读到已被销毁的栈内存）。

接收者先到、发送者后到的情况完全对称：接收者挂 `empty_on_stack()` 进 `receivers` 队列睡下；发送者后到时 `receivers.try_select()` 唤醒接收者，并 `write` 把消息写进接收者的空 packet + 置 `ready`；接收者醒来 `wait_ready` 后读自己的 packet。这种情况下**发送者无需等待**——因为消息落在了接收者自己的 packet 里，发送者写完即可返回。

归纳一句：**谁先到、谁提供 packet 并在成交后等待；消息落在谁的 packet 里，谁就负责在读/写完成后让对方通过 `ready` 得知「可以收尾」。**

#### 4.4.3 源码精读

阻塞 `send` 全貌（快路径 + 慢路径）：

[zero.rs:225-279](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L225-L279) — 注意 [zero.rs:272-276](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L272-L276) 的 `Operation(_)` 分支：`packet.wait_ready()` 后才 `Ok(())`，这正是「发送者等接收者读完」的会合点。

阻塞 `recv` 全貌（与 `send` 对称）：

[zero.rs:299-348](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L299-L348) — 接收者先到时挂 `empty_on_stack()`，成交后在 [zero.rs:341-345](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L341-L345) `wait_ready` 后读自己的 packet。

线程如何睡下与被唤醒（`Context`）：

[context.rs:144-169](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs#L144-L169) — `wait_until` 循环检查 `select` 状态，仍在 `Waiting` 则 `thread::park()`（或带截止时间 `park_timeout`）。
[context.rs:171-175](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/context.rs#L171-L175) — `unpark` 唤醒该 Context 所属线程。

断开时如何把所有等待者一次性唤醒：

[zero.rs:353-364](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L353-L364) — `disconnect` 置位后调用 `senders.disconnect()` 与 `receivers.disconnect()`。
[waker.rs:155-168](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/waker.rs#L155-L168) — `Waker::disconnect` 把每个等待者的状态 CAS 成 `Selected::Disconnected` 并 `unpark`。等待者醒来后在 `send`/`recv` 里走 `Disconnected` 分支，`unregister` 并取回消息。

#### 4.4.4 代码实践

1. **目标**：体会会合的「严格同步」——发送者真的会被卡住，直到接收者出现。
2. **步骤**：运行下面这段示例代码。主线程先 `send`，再启动接收线程；观察时间戳。
3. **观察**：`send` 调用会阻塞约 1 秒（直到接收线程 `recv`），然后主线程才打印「send 完成」。这证明发送者无法在接收者出现前返回。
4. **预期结果**：输出顺序为「开始 send →（间隔约 1s）→ send 完成: Ok(())」。运行结果待本地验证。

```rust
// 示例代码：验证会合语义
use crossbeam_channel::bounded;
use std::thread;
use std::time::{Duration, Instant};

fn main() {
    let (s, r) = bounded(0);
    let start = Instant::now();

    // 主线程立即发送——但没有接收者，应阻塞
    println!("开始 send ...");
    let h = thread::spawn(move || {
        s.send(42).unwrap(); // 阻塞到接收者出现
        println!("send 完成: 距开始 {:?}ms", start.elapsed().as_millis());
    });

    thread::sleep(Duration::from_secs(1)); // 故意延迟 1 秒才接收
    let _ = r.recv();

    h.join().unwrap();
}
```

#### 4.4.5 小练习与答案

**练习 1**：发送者先到、接收者后到时，为什么发送者必须 `wait_ready()`，而「接收者先到、发送者后到」时发送者却不用等？
**答案**：发送者先到时，消息在**发送者自己**的栈 packet 里，发送者若不等接收者读走就返回，栈帧销毁会让接收者读到悬垂内存；故必须等 `ready`。接收者先到时，消息被写进**接收者自己**的 packet，发送者写完即可返回，packet 的存活由接收者的栈帧保证，与发送者无关。

**练习 2**：`disconnect` 唤醒等待的发送者后，发送者如何避免消息丢失？
**答案**：发送者在 `Disconnected` 分支先 `unregister`（从队列摘除自己），再 `packet.msg.replace(None).unwrap()` 把消息从自己的栈 packet 里取回，然后返回 `SendTimeoutError::Disconnected(msg)`——错误内嵌消息，保证不丢（见 u3-l2 错误体系）。

---

## 5. 综合实践

**任务：用 `bounded(0)` 搭一条严格同步的两级流水线，并解释「为什么发送者不能先把消息放进缓冲」。**

要求：

1. 创建两个 zero 通道 `(s1, r1)` 与 `(s2, r2)`，构成 `生产者 → 中转 → 消费者` 的两级流水线。
2. 启动三个线程：生产者循环 `s1.send(i)`；中转者循环 `let v = r1.recv()?; s2.send(v)?;`；消费者循环 `r2.recv()`。
3. 在中转者的 `r1.recv()` 与 `s2.send(v)` 之间各打印一条带时间戳的日志。
4. 观察日志的**严格交替**：每一次 `recv` 之后必然紧跟一次 `send`，且生产者的下一次 `send` 一定等在中转者 `recv` 完成之后才发生。

代码骨架（示例代码）：

```rust
use crossbeam_channel::bounded;
use std::thread;
use std::time::Instant;

fn main() {
    let (s1, r1) = bounded(0);
    let (s2, r2) = bounded(0);
    let t0 = Instant::now();

    let p = thread::spawn(move || {
        for i in 0..3 {
            println!("{:4}ms 生产者 send({})", t0.elapsed().as_millis(), i);
            s1.send(i).unwrap(); // 无接收者时阻塞
        }
    });
    let m = thread::spawn(move || {
        for _ in 0..3 {
            let v = r1.recv().unwrap();
            println!("{:4}ms   中转 recv({})", t0.elapsed().as_millis(), v);
            s2.send(v).unwrap(); // 消费者没就绪时阻塞
            println!("{:4}ms   中转 send({}) 完成", t0.elapsed().as_millis(), v);
        }
    });
    let c = thread::spawn(move || {
        for _ in 0..3 {
            let v = r2.recv().unwrap();
            println!("{:4}ms     消费者 recv({})", t0.elapsed().as_millis(), v);
        }
    });

    p.join().unwrap();
    m.join().unwrap();
    c.join().unwrap();
}
```

**需要回答的问题**（即本讲核心实践题）：

- 阅读源码 [zero.rs:225-279](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L225-L279) 的 `send`，解释为什么发送者不能像 array flavor 那样「把消息放进缓冲就返回」。
- 参考答案：zero 的 `capacity()` 是 0（[zero.rs:372-374](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L372-L374)），根本没有缓冲槽。发送者若在接收者读走消息前返回并销毁栈 packet，接收者就会读到已释放的栈内存（UB）。因此发送者必须 `packet.wait_ready()` 等到接收者置 `ready=true` 才能返回——这正是会合语义的安全保障。array flavor 有环形缓冲槽，消息写进堆上的槽位后由通道持有，发送者无需等待即可返回。

> 说明：以上为示例代码，运行结果待本地验证。

---

## 6. 本讲小结

- `bounded(0)` 创建零容量（会合）通道：`len()` 恒为 0、`is_empty()` 与 `is_full()` 恒为 `true`，**没有缓冲**，`send`/`recv` 必须同时在场才能成交。
- 消息在成交瞬间放在一次性的 **`Packet<T>`** 容器里；分**栈 packet**（阻塞 `send`/`recv` 用，随栈帧回收）与**堆 packet**（select 宏路径用，显式 `Box::from_raw` 回收）两条路径，由 `on_stack` 标志区分。
- `ZeroToken(*mut ())` 是 packet 指针，作为统一 `Token` 的一个字段，把「成交的那个 packet」从配对阶段带到 `write`/`read` 阶段；`null` 表示「断开成交」。
- 配对发生在持 `inner` 锁期间：`start_send`/`start_recv` 到对端 `Waker` 队列里 `try_select` 一个属于别的线程的等待者，CAS 选定、`store_packet`、`unpark`，返回对方 packet。
- 阻塞路径遵循「谁先到谁提供 packet 并等待」：先到者注册进 `senders`/`receivers` 队列并 `wait_until` 睡下；后到者挑出先到者并唤醒。`ready` 标志（`Release`/`Acquire`）是收尾同步信号——发送者先到时必须 `wait_ready()` 确保接收者已读走消息，避免返回后销毁尚在被读的栈 packet。
- 断开时 `disconnect` 用 `Waker::disconnect` 把所有等待者 CAS 成 `Disconnected` 并批量 `unpark`；等待者醒来后 `unregister` 并取回消息，错误内嵌消息不丢失。

---

## 7. 下一步学习建议

- **u3-l7（Context 与 Waker）**：本讲只用到了 `Context` 的 `wait_until`/`store_packet`/`wait_packet`/`try_select`/`unpark` 与 `Waker` 的 `try_select`/`register_with_packet`/`disconnect`。下一讲会完整剖析 `Context` 的线程局部缓存与 `Waker`/`SyncWaker` 的 `selectors`/`observers` 双队列，帮你把本讲里「为什么 store_packet 对栈路径像多余」「notify 的观察者是谁」等细节彻底补齐。
- **u3-l9（select 动态选择算法）**：本讲的堆 packet 路径（`register`/`accept`/`wait_packet`）是 select 体系的一部分。学完 select 的五阶段算法后，再回看 zero 的 `SelectHandle` 实现（[zero.rs:393-491](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L393-L491)）会非常顺畅。
- **对照阅读**：把本讲的 zero flavor 与 u3-l4（array flavor）对照——array 用环形缓冲 + stamp 状态机做有界 MPMC，zero 则用「无缓冲 + 双等待队列 + packet 交接」做会合。两者的 `SelectHandle` 实现骨架相同，但缓冲策略截然不同，是理解 channel 设计取舍的好样本。
