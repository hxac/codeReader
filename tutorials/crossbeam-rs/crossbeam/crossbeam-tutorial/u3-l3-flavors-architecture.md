# flavors 架构与 SelectHandle trait

## 1. 本讲目标

在 u3-l1 里我们看到：`unbounded()` 造出无界通道、`bounded(cap)` 造出有界或零容量通道，而这些通道对外都暴露同一套 `send`/`recv`/`try_send`/`try_recv` 接口。那么——**明明底层是完全不同的数据结构（环形数组、链表、会合交接……），上层是怎么用一套统一接口把它们调度起来的？** 本讲就回答这个问题。

学完后你应该能够：

1. 看懂 `SenderFlavor` / `ReceiverFlavor` 这两个枚举如何作为「派发表」，把每一次 `send`/`recv` 路由到对应的 flavor 实现。
2. 说出 `SelectHandle` trait 的八个方法各自承担什么职责，以及 `select` 选择算法在哪几个阶段调用它们。
3. 理解 `Token` 这个「跨 flavor 的操作状态载体」是如何在一次选择中先被填充、再被 `write`/`read` 消费，把「选中」和「真正完成」两步解耦的。
4. 区分两类 flavor：缓冲/会合型（array/list/zero，靠对端唤醒）与时间/特殊型（at/tick/never，靠 deadline 阻塞）。

本讲只读三个文件：`channel.rs`（公共接口与派发外壳）、`flavors/mod.rs`（flavor 目录）、`select.rs`（trait 与选择算法）。具体 flavor 的缓冲算法、`Context`/`Waker` 的阻塞唤醒细节都留给后续讲义（u3-l4 ~ u3-l9）。

## 2. 前置知识

- **枚举即「标签 + 数据」**：Rust 的 `enum` 每个变体可以携带不同类型的数据。`match` 时按标签分派到对应分支。本讲里 flavor 枚举的每个变体就「装着」一种底层通道。
- **trait 与动态分派**：`&dyn Trait` 是一个「胖指针」，记录了具体值和一个虚表（vtable）。`Select` 把若干个 `&dyn SelectHandle` 收集到一个列表里统一调度，这就是 select 能同时等待多个通道的根基。
- **`Deref` 做单层透传**：`counter::Sender<C>` 实现了 `Deref<Target = C>`，所以在 `counter::Sender<flavors::array::Channel<T>>` 上调用 `.sender()` 会自动「穿透」到内部的 `Channel<T>::sender()`。这一点在本讲的源码里反复出现。
- **上一讲（u3-l2）的引用计数模型**：`Counter<C>` 是一块用 `Box::leak` 钉在堆上的共享账本，所有克隆端只持 `NonNull` 指针；`senders`/`receivers` 归零时触发 disconnect 与释放。本讲会看到 flavor 枚举的变体正是 `counter::Sender<flavors::xxx::Channel<T>>`。
- **flavor 的由来（u3-l1）**：`unbounded()` → `List`；`bounded(cap)` 中 `cap > 0` → `Array`，`cap == 0` → `Zero`。此外还有三个只读「时间/特殊」通道 `at` / `tick` / `never`。

## 3. 本讲源码地图

| 文件 | 作用 |
|---|---|
| [crossbeam-channel/src/channel.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs) | 公共接口层：`unbounded`/`bounded`/`after`/`at`/`never`/`tick` 构造函数，`Sender`/`Receiver` 及其 `send`/`recv` 等方法的 `match flavor` 派发外壳，以及 `write`/`read` 收尾函数。 |
| [crossbeam-channel/src/flavors/mod.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/mod.rs) | flavor 子模块总目录，声明六种 flavor。 |
| [crossbeam-channel/src/select.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs) | `SelectHandle` trait、`Token`/`Operation`/`Selected` 类型，`Select` 构建器与 `run_select`/`run_ready` 选择算法。 |

辅助理解（本讲引用但细节留待后续讲义）：

| 文件 | 作用 |
|---|---|
| [crossbeam-channel/src/counter.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs) | 引用计数 `Counter<C>`，`Sender`/`Receiver` 通过 `Deref` 透传到 flavor 通道。 |
| [crossbeam-channel/src/flavors/array.rs](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs) 等 | 六种 flavor 各自的实现与 `SelectHandle` impl。 |

---

## 4. 核心概念与源码讲解

### 4.1 SenderFlavor / ReceiverFlavor：枚举即派发表

#### 4.1.1 概念说明

「flavor」直译是「口味」，在 crossbeam-channel 里指**一种具体的底层通道实现**。全仓库一共六种 flavor，`flavors/mod.rs` 开头就列得很清楚：

```rust
// flavors/mod.rs:1-17（节选）
//! 1. `at`    - 到点投递一次
//! 2. `array` - 预分配数组的有界通道
//! 3. `list`  - 链表实现的无界通道
//! 4. `never` - 永不投递
//! 5. `tick`  - 周期性投递
//! 6. `zero`  - 零容量（会合）通道
pub(crate) mod array;
pub(crate) mod at;
pub(crate) mod list;
pub(crate) mod never;
pub(crate) mod tick;
pub(crate) mod zero;
```

关键设计问题是：**`Sender<T>` / `Receiver<T>` 是单一类型，却要包装六种截然不同的底层通道。** crossbeam 的做法是用一个私有枚举把「到底装的是哪一种」显式编码进去，再用 `match` 做分派。这个枚举就是 `SenderFlavor` / `ReceiverFlavor`。

#### 4.1.2 核心流程

一次 `s.send(msg)` 的派发过程可以概括为：

```text
Sender<T>::send(msg)
   └─ match self.flavor {            // 看 flavor 枚举装的是哪一种
        Array(chan) => chan.send(msg, None),   // chan: counter::Sender<array::Channel<T>>
        List(chan)  => chan.send(msg, None),
        Zero(chan)  => chan.send(msg, None),
      }
   └─ 三个分支都返回同一种 SendTimeoutError
   └─ map_err 成对外统一的 SendError
```

构造时则反向：构造函数根据容量/语义决定装哪个变体。

```text
unbounded()        ─► flavor = List
bounded(cap>0)     ─► flavor = Array
bounded(0)         ─► flavor = Zero
after(dur)/at(t)   ─► Receiver.flavor = At
tick(dur)          ─► Receiver.flavor = Tick
never()            ─► Receiver.flavor = Never
```

注意一个**不对称**：`SenderFlavor` 只有三种变体（Array/List/Zero），因为 `at`/`tick`/`never` 都是「只读」通道，根本不存在 `Sender`；而 `ReceiverFlavor` 有全部六种变体。

#### 4.1.3 源码精读

**两个枚举的定义。** 注意变体里装的是什么：

- `SenderFlavor` 的三个变体都是 `counter::Sender<flavors::xxx::Channel<T>>`（[channel.rs:370-380](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L370-L380)）。
- `ReceiverFlavor` 前三个变体同样是 `counter::Receiver<...>`；后三个变体直接是 `Arc<flavors::at::Channel>` / `Arc<flavors::tick::Channel>` / `flavors::never::Channel<T>`，**没有 counter 包裹**（[channel.rs:728-747](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L728-L747)）。

这揭示了一个重要的二分：

> **缓冲/会合型**（array/list/zero）是「双端、有发送方和接收方」的真实通道，需要引用计数管理多端的生命周期，所以套了一层 `counter`。
>
> **时间/特殊型**（at/tick/never）是「单端、只读」的，消息是到点「凭空产生」的，没有发送端，直接用 `Arc`（或零成本的空结构）共享即可。

**构造函数如何填 flavor。** 以 `bounded` 为例（[channel.rs:113-133](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L113-L133)）：`cap == 0` 时 `counter::new(flavors::zero::Channel::new())` 并包成 `Zero`；否则 `counter::new(flavors::array::Channel::with_capacity(cap))` 包成 `Array`。`unbounded` 类似地包成 `List`（[channel.rs:50-59](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L50-L59)）。`after`/`at`/`tick` 直接构造 `Receiver { flavor: ReceiverFlavor::At(...) }`（[channel.rs:181-188](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L181-L188)、[channel.rs:232-236](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L232-L236)、[channel.rs:335-345](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L335-L345)）。`never` 是 `const fn`，装一个零成本空结构（[channel.rs:275-279](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L275-L279)）。

**派发外壳是机械重复的 `match`。** 几乎 `Sender`/`Receiver` 的每个方法都是「三个或六个分支，每分支调同名方法」。例如 `try_send`（[channel.rs:410-416](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L410-L416)）：

```rust
pub fn try_send(&self, msg: T) -> Result<(), TrySendError<T>> {
    match &self.flavor {
        SenderFlavor::Array(chan) => chan.try_send(msg),
        SenderFlavor::List(chan)  => chan.try_send(msg),
        SenderFlavor::Zero(chan)  => chan.try_send(msg),
    }
}
```

这里的 `chan` 是 `counter::Sender<array::Channel<T>>`，调 `chan.try_send(msg)` 时先经 `counter::Sender` 的 `Deref`（[counter.rs:84-90](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs#L84-L90)）穿透到 `array::Channel::try_send`。`send`/`send_deadline`/`is_empty`/`len`/`capacity` 等方法（[channel.rs:446-456](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L446-L456)、[channel.rs:541-547](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L541-L547)）都是同一套模式。

**`Drop` 和 `Clone` 也走派发**，且清楚暴露了「时间/特殊型不需要计数」的事实。`Drop for Sender` 对三个缓冲型变体调 `chan.release(|c| c.disconnect_xxx())`，没有 at/tick/never 分支（[channel.rs:674-684](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L674-L684)）。`Clone for Receiver` 里，At/Tick 用 `Arc::clone`，而 `Never` 干脆 `Channel::new()` 造个新的空结构——因为它本来就没有状态（[channel.rs:1199-1212](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1199-L1212)）。

> **小贴士：为什么不用 trait object（`Box<dyn Flavor>`）代替枚举？** 因为枚举的 `match` 是静态分派，可被内联优化，且栈上存放无需堆分配；而通道的 `send`/`recv` 是极热的路径，每一次都经虚表开销不可接受。此外枚举变体类型在编译期完全已知，能保证零成本抽象。

#### 4.1.4 代码实践

**实践目标**：亲手把「派发表」从源码里抄一遍，建立 flavor 分支与具体文件的映射直觉。

**操作步骤**：

1. 打开 [channel.rs:410-416](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L410-L416)（`try_send`），把三个 `match` 分支与文件对应起来：
   - `SenderFlavor::Array(chan)` → `flavors/array.rs`
   - `SenderFlavor::List(chan)` → `flavors/list.rs`
   - `SenderFlavor::Zero(chan)` → `flavors/zero.rs`
2. 打开 [channel.rs:778-801](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L778-L801)（`try_recv`），它有**六个**分支。注意 At/Tick 分支里有一段 `unsafe { mem::transmute_copy(...) }`——这是因为 at/tick 的 `try_recv` 返回 `Result<Instant, _>`，而对外 `Receiver<T>::try_recv` 名义上返回 `Result<T, _>`；由于 at/tick 的 `T` 实际就是 `Instant`，这里用一次布局等价的位拷贝把类型对齐回来（这是不安全代码，初学者了解「有这么个类型对齐技巧」即可，不必深究）。
3. 打开 [channel.rs:674-684](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L674-L684)（`Drop for Sender`）和 [channel.rs:1184-1197](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1184-L1197)（`Drop for Receiver`），对比分支数量，确认 at/tick/never 的 drop 是空操作。

**需要观察的现象**：几乎所有方法的 `match` 都是「同构」的——分支数等于变体数，每个分支只是换一个底层方法名。这种机械重复正是「派发表」的代价，换来的是上层 API 的统一。

**预期结果**：你能凭记忆画出 `Sender`/`Receiver` 每个方法的分支数（Sender 的方法都是 3 分支，Receiver 的查询类方法是 6 分支）。

#### 4.1.5 小练习与答案

**练习 1**：`same_channel`（[channel.rs:656-663](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L656-L663)）对 `Sender` 用了 `a == b`，而对 `Receiver` 的 At/Tick 用了 `Arc::ptr_eq`（[channel.rs:1160-1170](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1160-L1170)）。为什么 Never 分支直接返回 `true`？

> **答案**：`counter::Sender`/`counter::Receiver` 实现了 `PartialEq`（比较内部 `NonNull` 指针，见 [counter.rs:92-96](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/counter.rs#L92-L96)），所以缓冲型可直接 `==`。At/Tick 是 `Arc` 共享，要比指针地址。Never 是无状态空结构，任何两个 `never()` 通道在语义上等价，故恒为 `true`。

**练习 2**：为什么 `SenderFlavor` 没有 At/Tick/Never 变体？

> **答案**：这三种 flavor 都是「只读、消息凭空产生」的通道，没有发送端概念，自然不存在 `Sender`。它们只出现在 `ReceiverFlavor` 中。

---

### 4.2 SelectHandle trait：统一的选择接口

#### 4.2.1 概念说明

派发表解决了「单通道的 send/recv 怎么落到具体 flavor」的问题。但 crossbeam-channel 还有一个杀手锏：**`select`，能同时等待多个通道，谁先就绪就执行谁**。要让 select 能平等地对待「往 array 通道发消息」「从 list 通道收消息」「等 tick 到点」这些截然不同的操作，就需要一个**统一的抽象接口**——这就是 `SelectHandle` trait。

`SelectHandle` 是一个 `pub` 但「半私有」的 trait（注释里说是给 `select!` 宏用的内部 API，通过 `crossbeam_channel::internal` 模块暴露）。用户通常不直接调它，但它是整个选择机制的契约。

#### 4.2.2 核心流程

`SelectHandle` 有八个方法，可按用途分成三组。`select` 的两条主路径——**选择提交（select/try_select/select_timeout）** 和 **就绪查询（ready/try_ready/ready_timeout）**——分别只用到其中一部分：

```text
选择提交路径（run_select）：
   try_select ──► register ──► (阻塞) ──► unregister ──► accept
   作用：尝试→注册等通知→醒来→注销→正式提交这次操作

就绪查询路径（run_ready）：
   is_ready ──► watch ──► (阻塞) ──► unwatch
   作用：轮询/订阅「就绪」事件，但不提交，返回的就绪索引仍需用户自己 try
```

每个方法的一句话语义（来自 [select.rs:99-123](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L99-L123) 的文档注释）：

| 方法 | 语义 |
|---|---|
| `try_select(token)` | 尝试立即完成操作，成功则把操作状态写入 `token` 并返回 `true`。 |
| `register(oper, cx)` | 把操作登记为「等通知」，返回 `true` 表示登记时发现已经就绪。 |
| `unregister(oper)` | 取消登记。 |
| `accept(token, cx)` | 线程被唤醒后，正式提交被选中的操作，成功返回 `true`。 |
| `is_ready()` | 不阻塞地查询当前是否就绪。 |
| `watch(oper, cx)` | 登记为「就绪通知」订阅，返回 `true` 表示已就绪。 |
| `unwatch(oper)` | 取消订阅。 |
| `deadline()` | 返回该操作建议的阻塞截止时刻（用于 at/tick 这类时间型通道）。 |

#### 4.2.3 源码精读

**trait 定义**见 [select.rs:99-123](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L99-L123)。注意还有一个 blanket 实现 `impl<T: SelectHandle> SelectHandle for &T`（[select.rs:125-157](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L125-L157)），这样 `&Sender`/`&Receiver`（引用）也自动满足 `SelectHandle`，可以被收进 `Select` 的 `Vec<&dyn SelectHandle>`。

**`Sender<T>` / `Receiver<T>` 的实现就是再分派一次。** 这很有趣：派发到 flavor 之后，还要把 `SelectHandle` 的调用转给 flavor 自己的 `SelectHandle` impl。对 `Sender`：

```rust
// channel.rs:1386-1446（节选 try_select / register / accept）
impl<T> SelectHandle for Sender<T> {
    fn try_select(&self, token: &mut Token) -> bool {
        match &self.flavor {
            SenderFlavor::Array(chan) => chan.sender().try_select(token),
            SenderFlavor::List(chan)  => chan.sender().try_select(token),
            SenderFlavor::Zero(chan)  => chan.sender().try_select(token),
        }
    }
    // register / unregister / accept / is_ready / watch / unwatch 同样三路分派
}
```

完整实现见 [channel.rs:1386-1446](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1386-L1446)（Sender）和 [channel.rs:1448-1536](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1448-L1536)（Receiver）。注意这里出现了一个新方法 `chan.sender()` / `chan.receiver()`——这是 flavor 的 `Channel` 提供的，返回一个**轻量的、带生命周期的句柄**（如 `array::Sender<'a, T>`），真正实现 `SelectHandle` 的是这个句柄，而不是 `Channel` 本身（缓冲型如此；时间型则直接由 `Channel` 实现，见下表）。`chan.sender()` 仍靠 `counter::Sender` 的 `Deref` 穿透到 `array::Channel::sender()`（[array.rs:132-140](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L132-L140)）。

**六种 flavor 的 `SelectHandle` 行为对比表**（这是本讲的核心交付物，综合各 flavor 文件的 impl 段：array.rs:613-683、list.rs:716-780、zero.rs:393-491、at.rs:143-194、tick.rs:163-209、never.rs:76-112）：

| flavor | 实现 `SelectHandle` 的类型 | `try_select` 做什么 | `register` 把操作挂到哪 | `deadline()` | `is_ready()` | 如何被唤醒 |
|---|---|---|---|---|---|---|
| **array** | `array::Sender/Receiver`（句柄） | CAS 占一个槽，写 `token.array` | `senders`/`receivers`（`SyncWaker`） | `None` | 发送：`!is_full \|\| is_disconnected`；接收：`!is_empty \|\| is_disconnected` | 对端 `notify()` |
| **list** | `list::Sender/Receiver`（句柄） | CAS 推进 tail/head，写 `token.list` | 接收：`receivers`（`SyncWaker`）；**发送：不挂**（list 永不阻塞发送） | `None` | 接收同上；**发送恒 `true`** | 接收侧对端 `notify()` |
| **zero** | `zero::Sender/Receiver`（句柄） | 锁内配对，写 `token.zero`（包指针） | `senders`/`receivers`（`Waker`，`register_with_packet` 还会分配堆 `Packet`） | `None` | `can_select() \|\| is_disconnected` | 对端 `notify()` 后 `can_select` |
| **at** | `Channel`（直接 impl） | 到点则 `try_recv` 写 `token.at` | **不挂队列**，仅返回 `is_ready()` | `Some(delivery_time)` | 到点且未读：`!is_empty` | 靠 `deadline` 让 select 阻塞到点，无对端唤醒 |
| **tick** | `Channel`（直接 impl） | CAS 推进周期写 `token.tick` | **不挂队列** | `Some(delivery_time)` | 到点：`!is_empty` | 靠 `deadline` 阻塞到点 |
| **never** | `Channel<T>`（直接 impl） | 恒 `false` | **不挂队列** | `None` | 恒 `false` | 永不就绪，只能靠外部 timeout 退出 |

这张表把本讲的两大主轴都串起来了：

1. **二分对照**：缓冲/会合型（array/list/zero）靠「对端 notify」唤醒，所以 `register` 会把操作挂进 `Waker`/`SyncWaker` 队列；时间/特殊型（at/tick/never）没有对端，靠 `deadline()` 让选择循环自己算出「该阻塞到几点」（见 `run_select` 中收集 deadline 的逻辑，[select.rs:251-260](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L251-L260)），`never` 连 deadline 都没有，只能阻塞到外部 timeout。
2. **`register` vs `watch` 的对称**：`register` 配套选择提交路径，`watch` 配套就绪查询路径。对缓冲型，二者都调用底层 `Waker` 的 `register`/`watch`；对时间型，二者都退化成「返回 `is_ready()`」。

**选择算法 `run_select` 如何调用这些方法**（[select.rs:176-324](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L176-L324)）的五阶段（完整算法留待 u3-l9，这里只标注 trait 方法的落点）：

1. **尝试阶段**：对所有 handle 调 `try_select`，任一成功就立即返回（[select.rs:207-211](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L207-L211)）。
2. **注册阶段**：对每个 handle 调 `register(oper, cx)`，若返回 `true`（登记时就绪）则尝试抢占（[select.rs:224-246](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L224-L246)）。
3. **决策/阻塞阶段**：汇总各 `deadline()` 取最早值，`cx.wait_until(deadline)` 阻塞（[select.rs:248-264](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L248-L264)）。
4. **注销阶段**：对已注册的 handle 调 `unregister`（[select.rs:266-269](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L266-L269)）。
5. **提交阶段**：若是被某个 `Operation` 唤醒，找到它并调 `accept` 正式提交（[select.rs:284-296](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L284-L296)）。

#### 4.2.4 代码实践

**实践目标**：验证上面的对比表，并用一段最小程序感受「不同 flavor 共享同一个 select 接口」。

**操作步骤**：

1. 逐一打开各 flavor 的 `impl SelectHandle` 段落，核对表格：
   - array：[array.rs:613-683](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L613-L683)
   - list：[list.rs:716-780](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L716-L780)
   - zero：[zero.rs:393-491](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L393-L491)
   - at：[at.rs:143-194](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/at.rs#L143-L194)
   - tick：[tick.rs:163-209](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/tick.rs#L163-L209)
   - never：[never.rs:76-112](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/never.rs#L76-L112)
2. 重点比较 at 的 `register`（[at.rs:169-172](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/at.rs#L169-L172)）与 zero 的 `register`（[zero.rs:402-411](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L402-L411)）：前者只有一行 `self.is_ready()`，后者要分配堆 `Packet`、`register_with_packet`、`notify`。这就是「靠 deadline 阻塞」与「靠对端唤醒」两种策略的代码级差异。
3. 运行下面这段示例（依赖 `crossbeam-channel`，示例代码，非项目原有）：

```rust
// 示例代码：用一个 list 通道 + 一个 at 通道做 select
use std::time::Duration;
use crossbeam_channel::{unbounded, at, select};
use std::time::Instant;

let (s, r) = unbounded::<i32>();        // List flavor
let timeout = at(Instant::now() + Duration::from_millis(200)); // At flavor

select! {
    recv(r) -> msg => println!("收到: {:?}", msg),
    recv(timeout) -> _ => println!("超时"),
}
// 没人发消息，~200ms 后会打印「超时」—— At flavor 的 deadline 让 select 准时醒来。
```

**需要观察的现象**：尽管 `r`（List）和 `timeout`（At）底层实现完全不同，`select!` 对二者一视同仁；最终是 At 的 `deadline()` 让线程在 200ms 后醒来。

**预期结果 / 待本地验证**：程序应在约 200ms 后打印「超时」。计时精度受平台调度影响，**待本地验证**确切延迟。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `list` 的 `Sender` 的 `register` 体只有 `self.is_ready()` 一行，而不像 `Receiver` 那样调 `self.0.receivers.register(oper, cx)`？（见 [list.rs:752-780](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L752-L780)）

> **答案**：无界链表通道「永远不满」，发送操作永不阻塞，`is_ready()` 恒为 `true`。既然发送总能立即成功，就根本不需要把自己挂进任何等待队列等唤醒。

**练习 2**：`run_select` 第 3 阶段为什么要把所有 `handle.deadline()` 取 `min`？（[select.rs:256-260](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L256-L260)）

> **答案**：select 要等「任意一个」操作就绪。at/tick 通过 deadline 告诉循环「我几点会就绪」，取最早的 deadline 阻塞，才能保证不错过最先就绪的那个；同时这个 deadline 也会与外部传入的 timeout 取 min，确保超时也能及时触发。

---

### 4.3 Token / Operation / Selected：跨 flavor 的状态载体

#### 4.3.1 概念说明

选择算法把「**选中**」和「**真正完成读写**」拆成了两步：`run_select` 先决定「执行第 i 个操作」，返回一个 `SelectedOperation`；之后用户调用 `SelectedOperation::send`/`recv` 才真正搬数据。问题来了——**两步之间隔着用户的代码，底层 flavor 怎么记住「我刚才在哪个槽/哪个块/哪个包里占好了位」？**

答案就是 `Token`：一个**能容纳所有 flavor 操作状态的「胖联合」结构**。选中时由 `try_select`/`accept` 把对应 flavor 的字段填好，完成时由 `write`/`read` 读出来用。配套的两个小类型：

- `Operation`：一个操作的「身份证」，本质是栈上某个变量的地址。
- `Selected`：这次选择/阻塞操作当前处于什么状态（等待/中止/断开/已选中某操作）。

#### 4.3.2 核心流程

`Token` 在一次选择中的生命周期：

```text
        Token::default()                      // 全字段默认值（空指针/None）
              │
   ┌──────────▼───────────┐
   │ try_select / accept  │  命中的 flavor 只写自己那一个字段：
   │   （选中阶段）        │   array 写 token.array.{slot,stamp}
   │                      │   list  写 token.list.{block,offset}
   │                      │   zero  写 token.zero（包指针）
   │                      │   at    写 token.at = Some(instant)
   │                      │   tick  写 token.tick = Some(instant)
   └──────────┬───────────┘
              │  Token 被装进 SelectedOperation
   ┌──────────▼───────────┐
   │ channel::write/read  │  按 flavor 取出对应字段，
   │   （完成阶段）        │  真正把消息写进/读出那个槽/块/包
   └──────────────────────┘
```

`Operation` 与 `Selected` 的流转则发生在 `Context`（线程局部阻塞上下文）里：

```text
register(oper, cx) ──► cx 把 oper 与线程句柄关联，挂入 Waker
                        ...
对端 notify ──► cx.try_select(Selected::Operation(oper)) ──► 唤醒线程
                        ...
醒来后 cx.selected() 返回 Selected ──► 据此决定 accept 哪个 oper
```

#### 4.3.3 源码精读

**Token 是一个「每 flavor 一个字段」的结构体**（[select.rs:23-32](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L23-L32)）：

```rust
#[derive(Debug, Default)]
pub struct Token {
    pub(crate) at:    flavors::at::AtToken,      // = Option<Instant>
    pub(crate) array: flavors::array::ArrayToken, // { slot: *const u8, stamp: usize }
    pub(crate) list:  flavors::list::ListToken,   // { block: *const u8, offset: usize }
    pub(crate) never: flavors::never::NeverToken, // = ()
    pub(crate) tick:  flavors::tick::TickToken,   // = Option<Instant>
    pub(crate) zero:  flavors::zero::ZeroToken,   // (*mut ())
}
```

各 flavor 的 token 类型定义在各自文件里：`AtToken = Option<Instant>`（[at.rs:16](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/at.rs#L16)）、`ArrayToken { slot, stamp }`（[array.rs:39-47](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L39-L47)）、`ListToken { block, offset }`（[list.rs:150-158](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L150-L158)）、`NeverToken = ()`（[never.rs:15-16](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/never.rs#L15-L16)）、`TickToken = Option<Instant>`（[tick.rs:16-17](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/tick.rs#L16-L17)）、`ZeroToken(*mut ())`（[zero.rs:25-32](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L25-L32)）。

> **为什么用「胖联合」而不是 `enum`？** 因为一次操作只可能命中一种 flavor，但写成「所有 flavor 字段并排」的结构体后，`Token` 的类型与具体 flavor 无关——`run_select` 只要持有一个 `Token` 即可，无须知道将来会命中哪种 flavor；选中的 flavor 自己去读写自己的字段。这避免了在 `Token` 上再做一次 `match` 分派，也让「选中阶段」和「完成阶段」能各自独立地只触碰自己关心的字段。代价是几个无用字段（如 `never: ()`）占点空间，但都极小。

**选中 → 完成的衔接**就在 `SelectedOperation` 上。`send`/`recv` 把保存的 `token` 喂给 `channel::write`/`channel::read`（[select.rs:1276-1318](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L1276-L1318)）：

```rust
pub fn send<T>(mut self, s: &Sender<T>, msg: T) -> Result<(), SendError<T>> {
    assert!(s.addr() == self.addr, "passed a sender that wasn't selected");
    let res = unsafe { channel::write(s, &mut self.token, msg) };
    mem::forget(self);          // 完成了，跳过 Drop 的 panic
    res.map_err(SendError)
}
```

注意两点：(1) `assert!` 用 `addr()` 校验「用户传进来的 `Sender` 确实是当初被选中的那一个」，防止张冠李戴；(2) `mem::forget(self)` 是因为 `SelectedOperation` 的 `Drop` 会 panic——**忘记完成一个被选中的操作是严重错误，会导致死锁**（[select.rs:1327-1331](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L1327-L1331)）。

**`channel::write`/`read` 又是 flavor 派发**（[channel.rs:1538-1565](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1538-L1565)），它们取出 token 对应字段完成真正的搬运。例如 array 的 `write` 用 `token.array.slot/stamp` 把消息写进占好的槽（[array.rs:215-230](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L215-L230)），zero 的 `read` 用 `token.zero.0` 找到交接包（[zero.rs:179-202](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/zero.rs#L179-L202)）。于是「Token 作为状态载体」的闭环就形成了。

**`Operation` 是栈变量的地址**（[select.rs:34-52](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L34-L52)）。`Operation::hook(r)` 把 `&mut T` 的地址转成 `usize` 当 id，并断言 `val > 2`——因为 0/1/2 被 `Selected` 的三个特殊状态占用了：

```rust
pub enum Selected {   // select.rs:54-68
    Waiting,          // = 0
    Aborted,          // = 1
    Disconnected,     // = 2
    Operation(Operation), // = Operation(usize > 2)
}
```

`Selected` 与 `usize` 的双向转换（[select.rs:70-92](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L70-L92)）是为了把这些状态存进一个原子字里——`Context` 正是用一个 `AtomicUsize` 来 CAS 抢占「这次阻塞到底选了哪个操作」，这是 u3-l7 的内容，这里只要知道 `Selected` 的编码即可。

#### 4.3.4 代码实践

**实践目标**：跟踪一次 select 中 `Token` 的流转，把「选中」与「完成」两步在源码里连起来。

**操作步骤（源码阅读型）**：

1. 在 [select.rs:204-211](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L204-L211) 看 `run_select` 创建 `let mut token = Token::default();`，然后对每个 handle 调 `handle.try_select(&mut token)`。
2. 假设命中了一个 array 接收操作，进入 [array.rs:613-616](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L613-L616)（`Receiver::try_select` → `start_recv`），看它如何写 `token.array.slot`/`token.array.stamp`（[array.rs:267-269](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L267-L269)）。
3. `token` 被装进 `SelectedOperation`（[select.rs:462-468](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L462-L468)）。
4. 用户调 `oper.recv(&r)` → [select.rs:1310-1318](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L1310-L1318) → `channel::read(r, &mut self.token)` → [channel.rs:1550-1565](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/channel.rs#L1550-L1565) → array 分支 `chan.read(token)` → [array.rs:306-321](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/array.rs#L306-L321)，用 `token.array.slot` 取出消息。

**需要观察的现象**：`token` 从被 `start_recv` 填充，到被 `read` 消费，中间始终是同一个 `Token` 值在传递；不同 flavor 只读写自己那个字段，互不干扰。

**预期结果**：你能在源码里画出 `token` 从「创建→填充→装入 SelectedOperation→被 write/read 消费」的完整链路。

#### 4.3.5 小练习与答案

**练习 1**：`Operation::hook` 为什么要 `assert!(val > 2)`（[select.rs:49](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L49)）？

> **答案**：`Selected` 把 `Waiting/Aborted/Disconnected` 编码为 0/1/2（[select.rs:70-80](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L70-L80)），`Operation(val)` 的 `val` 必须大于 2 才不会与这三个特殊状态混淆。由于栈变量的地址几乎不可能 ≤ 2，这只是一个防呆断言。

**练习 2**：如果用户拿到 `SelectedOperation` 后既不调 `send` 也不调 `recv`，而是直接丢弃，会发生什么？

> **答案**：`SelectedOperation::drop` 会 panic（`"dropped SelectedOperation without completing the operation"`，[select.rs:1327-1331](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L1327-L1331)）。因为「选中」可能已经让某个对端停止等待，不完成会破坏通道状态甚至死锁，所以强制必须完成。

---

## 5. 综合实践

**任务**：把本讲的三条主线——flavor 枚举派发、`SelectHandle` 统一接口、`Token` 状态流转——用一段程序和一次源码追踪串起来。

**步骤 1：运行一个混合 flavor 的 select。** 下面这段示例代码综合了 list（unbounded）、array（bounded）、at 三种 flavor：

```rust
// 示例代码（非项目原有）
use std::time::{Duration, Instant};
use crossbeam_channel::{unbounded, bounded, at, select};

let (s_list, r_list) = unbounded::<&str>();   // List
let (s_arr,  r_arr)  = bounded::<i32>(2);     // Array
let deadline = at(Instant::now() + Duration::from_millis(500)); // At

s_list.send("hello").unwrap();   // 立即成功（list 永不满）
s_arr.send(42).unwrap();         // 立即成功（容量 2）

select! {
    recv(r_list) -> m => println!("从 list 收到: {:?}", m),
    recv(r_arr)  -> m => println!("从 array 收到: {:?}", m),
    recv(deadline) -> _ => println!("超时"),
}
```

**步骤 2：源码追踪。** 对照源码回答：

1. `r_list`、`r_arr`、`deadline` 三个 `Receiver`，它们的 `flavor` 字段分别装的是哪个 `ReceiverFlavor` 变体？（答：`List(counter::Receiver<list::Channel<&str>>)`、`Array(counter::Receiver<array::Channel<i32>>)`、`At(Arc<at::Channel>)`。）
2. `select!` 展开后会对这三个 `&Receiver` 调 `try_select`。三个分支会分别落到哪个文件的 `SelectHandle::try_select`？（答：list.rs:716、array.rs:613、at.rs:143。）
3. 假设最先就绪的是 `r_list`，被选中后 `token.list` 字段被谁填充？最终由哪个函数消费？（答：由 [list.rs:288-289](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L288-L289) 的 `start_send` 写入 `token.list.block/offset`；由 [list.rs:406-430](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/flavors/list.rs#L406-L430) 的 `read` 消费。）

**步骤 3：观察与验证。** 由于 `r_list` 和 `r_arr` 一开始都已有消息，select 会「随机」选一个先打印（见 [select.rs:196-199](https://github.com/crossbeam-rs/crossbeam/blob/6195355ef1862f2c6172365d00645cb6f77417dc/crossbeam-channel/src/select.rs#L196-L199) 的 `shuffle` 保证公平性）。多运行几次，**观察**打印的通道会变化。具体先打印哪一个**待本地验证**（受随机与调度影响）。

> 提示：如果你想看清 `select!` 展开后的代码，可以用 `cargo expand`（`cargo +nightly expand`）。展开后你会看到它调用的正是 `crossbeam_channel::internal::select` 系列——它们最终落到本讲分析的 `run_select`。

## 6. 本讲小结

- **flavor 枚举是派发表**：`SenderFlavor`（3 变体）/`ReceiverFlavor`（6 变体）把每一次 `send`/`recv`/`Drop`/`Clone` 等操作用 `match` 路由到对应 flavor；这是静态分派、零成本，热路径上不经虚表。
- **两大类 flavor**：缓冲/会合型（array/list/zero）双端、套 `counter`、`SelectHandle` 由 `Channel::sender()/receiver()` 返回的轻量句柄实现，靠对端 `notify` 唤醒；时间/特殊型（at/tick/never）只读、直接 `Arc`/空结构、`SelectHandle` 由 `Channel` 直接实现，靠 `deadline()` 让选择循环阻塞到点。
- **`SelectHandle` 是统一契约**：八个方法分两组——`try_select/register/unregister/accept` 服务「选择提交」路径，`is_ready/watch/unwatch` 服务「就绪查询」路径，外加 `deadline`。
- **`Token` 是跨 flavor 的状态载体**：一个「每 flavor 一字段」的胖结构体，选中阶段由 flavor 自己填字段，完成阶段由 `channel::write/read` 取字段，把「选中」与「完成」解耦。
- **`Operation`/`Selected` 编码身份与状态**：`Operation` 用栈地址当 id（且 >2），`Selected` 把「等待/中止/断开/已选中」编码进可与 `usize` 互转的枚举，供 `Context` 原子抢占。
- **门面里藏着一层 `transmute_copy`**：at/tick 的 `try_recv` 返回 `Instant`，对外却要表现为 `T`，靠一次布局等价位拷贝对齐类型——这是统一 `Receiver<T>` 接口的代价。

## 7. 下一步学习建议

本讲建立了「flavor 派发 + `SelectHandle` 契约 + `Token` 流转」的骨架，后续讲义往三个方向填肉：

1. **先攻具体 flavor 的缓冲算法**：u3-l4（array 的 Vyukov 环形缓冲与 stamp 协议）、u3-l5（list 的分块链表）、u3-l6（zero 的会合交接与 `Packet`）——你会看到 `try_select`/`register` 在每种 flavor 里到底怎么 CAS、怎么挂队列。
2. **再攻阻塞唤醒机制**：u3-l7（`Context` 与 `Waker`/`SyncWaker`）——本讲里反复出现的 `register(oper, cx)`、`notify()`、`can_select()`、`cx.wait_until()` 都在那里展开。
3. **最后攻选择算法与宏**：u3-l8（at/tick/never 的时间语义）、u3-l9（`run_select`/`run_ready` 的完整五阶段与公平性）、u3-l10（`select!` 宏如何展开成对 `SelectHandle` 的调用）。

建议阅读时带着本讲的对比表对照看：每读一个 flavor，就在表里把它的那一行补全，最终你会得到一张完整的 crossbeam-channel 架构地图。
