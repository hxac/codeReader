# 回调与通道：Handlers / Channels

## 1. 本讲目标

上一讲（u3-l1）我们写出了第一对 `Publisher`/`Subscriber`，订阅端用 `sub.recv_async().await` 一条条收 `Sample`。但你可能没注意到一个关键细节：**Zenoh 是怎么把网络线程收到的 `Sample` 递到你手里的？**

本讲就回答这个问题。读完本讲你应当能够：

1. 说清 Zenoh 取数的两种姿势——**回调（callback）** 与 **通道（channel）**——以及它们在底层其实是同一套机制。
2. 理解 `IntoHandler` trait：它如何把「一个通道」拆成「一个回调 + 一个取数句柄」。
3. 掌握 builder 上的 `.with(...)` 与 `.callback(...)` 方法，知道默认 handler 是什么。
4. 区分 `FifoChannel`（满了就**阻塞**，形成背压）与 `RingChannel`（满了就**丢弃最旧**数据）的语义差异，并能根据场景做出取舍。

本讲只看 `zenoh/src/api/handlers/` 这一个目录外加订阅 builder 的几行调用，不涉及网络层，属于「公开 API 机制」范畴。

## 2. 前置知识

- **生产者/消费者速度不匹配问题**：在网络应用里，数据产生（入站）和消费（你的业务逻辑）往往是两个不同步的节奏。入站快、消费慢时，中间需要一个「缓冲」来吸收时差，否则要么丢数据、要么阻塞发送方。
- **背压（backpressure）与丢帧（drop）的取舍**：当缓冲满时只有两条路——要么让发送方等一等（背压，保数据不丢但可能拖慢整个流水线），要么直接扔掉一些数据（丢帧，保流水线通畅但牺牲完整性）。这是本讲的核心取舍点。
- **回调函数（callback）**：把一个 `Fn(T)` 闭包交给框架，框架每来一条数据就调用一次。你写 GUI、事件循环时常这么做。
- **通道（channel）**：经典并发原语，一端 `send`、另一端 `recv`，中间有缓冲。Rust 生态里常用 `flume`、`std::sync::mpsc`、`tokio::mpsc` 等。
- **`Deref` 强制解引用**：Rust 里若类型 `A` 实现了 `Deref<Target=B>`，那么在 `A` 上可以直接调用 `B` 的方法。本讲中 `Subscriber<Handler>` 就 `Deref` 到 `Handler`，所以你能直接 `sub.recv_async()`。
- 建议先读完《u3-l1 Pub/Sub 基础》，知道 `Sample`、`declare_subscriber`、builder 必须 `.await`/`.wait()` 才 resolve。

## 3. 本讲源码地图

本讲涉及的关键文件如下：

| 文件 | 作用 |
| --- | --- |
| [zenoh/src/api/handlers/mod.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/handlers/mod.rs) | 定义 `IntoHandler` trait 与 `DefaultHandler`，是整个 handlers 机制的「接口中枢」。 |
| [zenoh/src/api/handlers/callback.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/handlers/callback.rs) | 定义 `Callback<T>`：一切取数方式在底层最终都变成它。 |
| [zenoh/src/api/handlers/fifo.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/handlers/fifo.rs) | `FifoChannel`：有界阻塞队列，默认 handler 的实现。 |
| [zenoh/src/api/handlers/ring.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/handlers/ring.rs) | `RingChannel`：有界丢弃队列，满了丢最旧数据。 |
| [zenoh/src/api/builders/subscriber.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/subscriber.rs) | `SubscriberBuilder`：`.with()` / `.callback()` 与 resolve 时调用 `into_handler` 的地方。 |
| [commons/zenoh-collections/src/ring_buffer.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-collections/src/ring_buffer.rs) | `RingBuffer`：`RingChannel` 底层依赖的环形缓冲，提供 `push_force`（丢弃最旧）。 |
| [zenoh/tests/handler.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/tests/handler.rs) | 官方集成测试，演示 `RingChannel` 丢弃最旧数据的行为，是本讲实践的真凭实据。 |

> 提示：`handlers` 目录里还有一个 `DefaultHandler`，它不是第四种通道，而是默认通道的一个「不透明包装」，下面会讲。

---

## 4. 核心概念与源码讲解

### 4.1 IntoHandler：统一「回调」与「通道」的抽象

#### 4.1.1 概念说明

Zenoh 的所有「连续型」实体——`Subscriber`（连续收 `Sample`）、`Queryable`（连续收 `Query`）、`Query`/`get`（连续收 `Reply`）、`MatchingListener` 等——都面临同一个问题：**网络线程不断把数据递过来，用户想用自己的方式拿走。**

有人喜欢回调（来一条处理一条，零样板），有人喜欢通道（攒在队列里，自己控制节奏、能 `await`）。如果为这两种姿势各写一套 API，代码会非常臃肿。Zenoh 的做法是：

> **底层永远只有回调。** 通道只是「把数据塞进队列的回调 + 从队列取数据的句柄」这一对组合。

把「一个东西」拆成「回调 + 句柄」这个动作，被抽象成了 `IntoHandler` trait。于是无论用户传进来的是闭包、是 `flume` 通道、是 `RingChannel`，还是 `(回调, 自定义句柄)` 元组，统一都走 `IntoHandler::into_handler()`，框架拿到的永远是同一个 `Callback<T>`。

这是一个非常典型的「**收口**」设计：对外暴露多种灵活姿势，对内只维护一种实现。

#### 4.1.2 核心流程

```
用户传入的取数方式 (闭包 / FifoChannel / RingChannel / (Callback, H) / flume 元组 ...)
                    │
                    │  都实现 IntoHandler<Sample>
                    ▼
        .into_handler()  ──►  (Callback<Sample>, Handler)
                    │                │
   框架拿走 Callback：        用户拿走 Handler：
   每来一条 Sample 就         存进 Subscriber<Handler>，
   Callback::call(sample)     经 Deref 暴露 recv_async() 等
```

要点：

1. `into_handler` 返回一个**二元组** `(Callback<T>, Self::Handler)`，左边给框架、右边给用户。
2. `Handler` 是一个关联类型，随传入类型而变：传 `RingChannel` 得到 `RingChannelHandler`，传闭包得到 `()`（空，因为回调自己处理了数据，不需要再取）。
3. `Subscriber<Handler>` 内部就持有这个 `Handler`，并通过 `Deref` 让你能直接 `sub.recv_async()`，无需关心队列对象。

#### 4.1.3 源码精读

先看 trait 本体，它只有两个成员：一个关联类型、一个返回二元组的方法。

[zenoh/src/api/handlers/mod.rs:32-36](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/handlers/mod.rs#L32-L36) —— 这是「回调+句柄」抽象的源头：

```rust
pub trait IntoHandler<T> {
    type Handler;
    fn into_handler(self) -> (Callback<T>, Self::Handler);
}
```

再看框架一侧如何使用它。订阅 builder 在 `.wait()`（同步 resolve）时，把用户传入的 `handler` 调一次 `into_handler`，拿到回调交给会话、拿到句柄塞进 `Subscriber`：

[zenoh/src/api/builders/subscriber.rs:213-237](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/subscriber.rs#L213-L237) —— 注意第 217 行的 `into_handler()` 调用，以及第 234 行把 `receiver` 存进返回的 `Subscriber`：

```rust
fn wait(self) -> <Self as Resolvable>::To {
    let mut key_expr = self.key_expr?;
    // ...
    let (callback, receiver) = self.handler.into_handler();   // ← 收口点
    // ...
    session.declare_subscriber_inner(&key_expr, self.origin, callback, /*..*/)
        .map(|sub_state| Subscriber {
            inner: /* .. */,
            handler: receiver,     // ← 句柄存这里
            callback_sync_group,
        })
}
```

而 builder 上供用户选择的两个方法，本质都是「换 `Handler` 类型参数」。`.callback(f)` 其实就是 `.with(Callback::from(f))` 的语法糖：

[zenoh/src/api/builders/subscriber.rs:86-91](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/subscriber.rs#L86-L91)：

```rust
pub fn callback<F>(self, callback: F) -> SubscriberBuilder<'a, 'b, Callback<Sample>>
where F: Fn(Sample) + Send + Sync + 'static,
{
    self.with(Callback::from(callback))
}
```

[zenoh/src/api/builders/subscriber.rs:139-155](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/subscriber.rs#L139-L155) —— `.with` 接受任意 `IntoHandler<Sample>`，丢弃旧的默认 handler、换上你给的：

```rust
pub fn with<Handler>(self, handler: Handler) -> SubscriberBuilder<'a, 'b, Handler>
where Handler: IntoHandler<Sample>,
{
    let SubscriberBuilder { session, key_expr, origin, handler: _ } = self;
    SubscriberBuilder { session, key_expr, origin, handler }
}
```

至于 `Callback<T>` 本身，它就是一个被 `Arc` 包起来、可 `Clone` 的可调用对象（对任何 `Fn(T)+Send+Sync` 自动实现），外加一个可选的 drop 钩子：

[zenoh/src/api/handlers/callback.rs:72-75](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/handlers/callback.rs#L72-L75)：

```rust
pub struct Callback<T> {
    callable: Arc<dyn CallbackImpl<T>>,
    drop: Option<Arc<dyn DropperTrait + Send + Sync>>,
}
```

正因为它 `Clone` 又廉价（只增一个 `Arc` 引用），框架内部可以把同一条回调扇出给多处。

最后看几种现成的 `IntoHandler` 实现，体会「万物皆可 into_handler」：

- 闭包：[zenoh/src/api/handlers/callback.rs:124-131](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/handlers/callback.rs#L124-L131) 的 `From<F> for Callback<T>`，配合 [callback.rs:133-138](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/handlers/callback.rs#L133-L138) 的 `IntoHandler for Callback<T>`（`Handler = ()`，因为数据已被回调处理）。
- `(Callback<T>, H)` 元组：[zenoh/src/api/handlers/callback.rs:151-157](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/handlers/callback.rs#L151-L157)，让你「自带回调 + 自定义句柄」。
- `flume::(Sender, Receiver)` 元组：[zenoh/src/api/handlers/callback.rs:159-173](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/handlers/callback.rs#L159-L173)，直接复用第三方 channel。

#### 4.1.4 代码实践

**实践目标**：亲眼看到「回调」和「通道」两条路最终都走到 `into_handler`，并理解 `.callback` 是 `.with` 的语法糖。

**操作步骤（源码阅读型 + 最小调用型）**：

1. 打开 [zenoh/src/lib.rs:730-780](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L730-L780)，这是 crate 顶层文档对「通道 vs 回调」的官方讲解，明确写道「底层总是回调，`IntoHandler` 只是把通道拆成回调+句柄」。
2. 阅读官方集成测试 [zenoh/tests/handler.rs:19-41](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/tests/handler.rs#L19-L41)：它用 `.with(RingChannel::new(3))` 声明订阅，再连发 10 条 `put`，最后断言只能收到最后 3 条（`put7`/`put8`/`put9`）。这就是「通道姿势」的活样本。
3. 写一个最小示例，对比两种姿势（**示例代码**，需放在启用 `unstable`/example 的工程里，或直接参照 `examples/` 改）：

   ```rust
   // 姿势 A：通道（可 await）
   let sub = session.declare_subscriber("demo/**").await.unwrap();
   while let Ok(s) = sub.recv_async().await { println!("{:?}", s); }

   // 姿势 B：回调（handler 类型推断为 ()，没有 recv_async）
   let _sub = session.declare_subscriber("demo/**")
       .callback(|s| println!("{:?}", s))
       .await.unwrap();
   ```

**需要观察的现象**：姿势 A 的 `sub` 上能点出 `recv_async`（因为 `Handler = FifoChannelHandler` 且 `Subscriber` `Deref` 到它）；姿势 B 的 `sub` 上**点不出** `recv_async`（因为 `Handler = ()`）。

**预期结果**：编译器在姿势 B 上对 `sub.recv_async()` 报「no method named `recv_async`」，正说明 `Handler` 类型随姿势而变。若你不确定能否本地编译运行，请标注「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 Zenoh 不直接为「通道」和「回调」各写一套内部逻辑，而要统一到 `IntoHandler`？

**参考答案**：统一后内部只维护一条「每来一条数据就 `Callback::call(t)`」的代码路径，避免双份逻辑分叉与不一致；同时对外仍能通过不同的 `IntoHandler` 实现提供灵活姿势（闭包、各类 channel、自定义元组），是「对外灵活、对内单一」的收口设计。

**练习 2**：传入一个 `Fn(Sample)` 闭包时，`Handler` 关联类型是什么？为什么？

**参考答案**：是 `()`（单元类型）。因为数据在回调里就被消费掉了，不需要再额外返回一个「取数句柄」；`Subscriber<()>` 因此也就没有 `recv_async` 这类方法。

---

### 4.2 FifoChannel：有界阻塞队列（背压）

#### 4.2.1 概念说明

当你**不指定** handler 时，Zenoh 默认就用 `FifoChannel`（经过一个叫 `DefaultHandler` 的不透明包装）。它的语义是：

> 一个**有界的先进先出队列**。队列没满就立刻塞入；**满了就阻塞发送方**，直到消费者腾出空位。

这是一种「**背压**」策略：宁可让 Zenoh 的网络线程等一等，也**不丢任何一条数据**。

这听起来很美好，但代价是：如果你的订阅端处理很慢（比如每条 `Sample` 要跑几秒业务逻辑），队列很快填满，阻塞会回传到 Zenoh 内部线程，进而拖慢整个会话的入站——这就是 `fifo.rs` 文档注释里警告的「slow subscriber could block the underlying Zenoh thread」。

#### 4.2.2 核心流程

`FifoChannel` 的 `into_handler` 用第三方库 `flume` 建一个有界通道：

```
FifoChannel { capacity }                      默认 capacity = API_DATA_RECEPTION_CHANNEL_SIZE = 256
        │  into_handler()
        ▼
flume::bounded(capacity)  ──►  (Sender, Receiver)
        │                          │
   包成 Callback：               包成 FifoChannelHandler：
   每条数据 sender.send(t)       recv / recv_async / try_recv / iter / stream ...
        │
   队列满时 sender.send 阻塞  ←── 这就是背压来源
```

容量是受配置控制的「可调静态变量」。设默认值为 256：

[zenoh/src/api/session.rs:123-127](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L123-L127)：

```rust
zconfigurable! {
    pub(crate) static ref API_DATA_RECEPTION_CHANNEL_SIZE: usize = 256;
    // ...
}
```

> 术语 `zconfigurable!`：Zenoh 自家的宏，把变量注册成「运行时可被环境变量覆盖」的配置项（类似可调常量）。默认 256，但能通过环境变量在启动时改。

背压的「强度」可以用一个粗略公式描述：当入站速率 \( r_{in} \)（条/秒）持续大于消费速率 \( r_{out} \)，缓冲填满的时间约为

\[
t_{fill} \approx \frac{capacity}{r_{in} - r_{out}}
\]

填满之后，`FifoChannel` 不再吸收新数据，而是把入站「卡住」，相当于用队列容量换取 \( t_{fill} \) 秒的喘息时间；若消费始终跟不上，背压就会传导到网络线程。

#### 4.2.3 源码精读

先看 `DefaultHandler`——它就是 `FifoChannel` 的一层不透明包装，存在的原因是「将来想换默认实现时不必破坏 API」：

[zenoh/src/api/handlers/mod.rs:38-56](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/handlers/mod.rs#L38-L56)：

```rust
#[repr(transparent)]
pub struct DefaultHandler(FifoChannel);

impl<T: Send + 'static> IntoHandler<T> for DefaultHandler {
    type Handler = <FifoChannel as IntoHandler<T>>::Handler;
    fn into_handler(self) -> (Callback<T>, Self::Handler) {
        self.0.into_handler()   // 直接委托给 FifoChannel
    }
}
```

`FifoChannel` 本身只存一个 `capacity`：

[zenoh/src/api/handlers/fifo.rs:34-50](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/handlers/fifo.rs#L34-L50)：

```rust
pub struct FifoChannel { capacity: usize }

impl FifoChannel {
    pub fn new(capacity: usize) -> Self { Self { capacity } }
}

impl Default for FifoChannel {
    fn default() -> Self { Self::new(*API_DATA_RECEPTION_CHANNEL_SIZE) }  // 256
}
```

关键在 `into_handler`：它建一个 `flume::bounded(capacity)`，把 `Sender` 包进回调、`Receiver` 包成 `FifoChannelHandler`：

[zenoh/src/api/handlers/fifo.rs:57-71](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/handlers/fifo.rs#L57-L71)：

```rust
fn into_handler(self) -> (Callback<T>, Self::Handler) {
    let (sender, receiver) = flume::bounded(self.capacity);
    (
        Callback::from(move |t| {
            if let Err(error) = sender.send(t) {   // ← 满了就在此阻塞（背压）
                tracing::error!(%error)
            }
        }),
        FifoChannelHandler(receiver),
    )
}
```

> 注意 `flume::bounded` 的 `send` 在队列满时是**阻塞**的（同步 `send`），这正是「背压」的物理来源：Zenoh 的入站回调线程会卡在这行，直到订阅端 `recv` 腾出位置。

消费侧的 `FifoChannelHandler` 包装了 `flume::Receiver`，提供一整套同步/异步取数方法。异步入口是：

[zenoh/src/api/handlers/fifo.rs:252-257](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/handlers/fifo.rs#L252-L257)：

```rust
impl<T> FifoChannelHandler<T> {
    pub fn recv_async(&self) -> RecvFut<'_, T> {
        RecvFut(self.0.recv_async())
    }
    // ...
}
```

此外它还提供 `try_recv`（非阻塞，空则返回 `Ok(None)`）、`recv_timeout`、`iter`/`try_iter`/`drain`、以及 `stream()`（`futures::Stream`）等多种姿势，覆盖几乎所有消费模式。

#### 4.2.4 代码实践

**实践目标**：感受 `FifoChannel` 的「阻塞」语义与默认容量来源。

**操作步骤（源码阅读型 + 参数调整型）**：

1. 在 `FifoChannel::default`（[fifo.rs:46-50](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/handlers/fifo.rs#L46-L50)）确认默认容量来自 `API_DATA_RECEPTION_CHANNEL_SIZE`，再到 [session.rs:124](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L124) 确认其值为 256。
2. 故意把容量改到很小，模拟「慢消费者撞满队列」：

   ```rust
   // 示例代码：用极小容量的 FifoChannel，并在消费端每条 sleep 0.5s
   let sub = session.declare_subscriber("demo/**")
       .with(zenoh::handlers::FifoChannel::new(2))   // 容量仅 2
       .await.unwrap();
   for _ in 0..50 {
       session.put("demo/a", "x").await.unwrap();   // 快速连发
   }
   while let Ok(s) = sub.recv_async().await {
       println!("got {:?}", s.payload());
       tokio::time::sleep(std::time::Duration::from_millis(500)).await; // 慢消费
   }
   ```

**需要观察的现象**：发布端的 `put` 不会「瞬间」全部返回——当队列（容量 2）被填满后，后续 `put` 会变慢/卡住，因为发送回调被阻塞，背压传导到 `put`。这正是 `fifo.rs` 文档注释警告的「slow subscriber blocks the underlying Zenoh thread」。

**预期结果**：发布端的打印节奏被订阅端的 `sleep` 拖慢，证明数据没有丢失、但也没有被快速吞下。若本地环境不便运行，标注「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1**：默认 handler 是 `FifoChannel` 还是直接就是 `FifoChannel`？为什么要多包一层 `DefaultHandler`？

**参考答案**：默认是 `DefaultHandler`，它内部是 `FifoChannel`（`#[repr(transparent)]` 零开销包装）。多包一层是为了**解耦默认实现与公开类型**：将来若官方想把默认从 FIFO 换成别的策略，只需改 `DefaultHandler` 内部，用户代码的 `DefaultHandler` 类型不变，不构成破坏性变更。

**练习 2**：`FifoChannel` 满了会怎样？这是优点还是缺点？

**参考答案**：会阻塞发送回调（`flume::bounded::send` 在满时阻塞），形成背压，传导到 Zenoh 内部线程甚至 `put` 调用方。它是「不丢数据」的优点，也是「慢消费者会拖慢整条入站链路」的缺点——适合**不能丢数据**的场景（如金融事件、控制指令），不适合**容忍丢帧、要求低延迟**的场景（如视频流、高频遥测，那应换 `RingChannel`）。

---

### 4.3 RingChannel：有界丢弃队列（丢帧）

#### 4.3.1 概念说明

`RingChannel` 走另一条路：

> 一个**固定容量**的环形队列，遵循 FIFO 顺序；当队列满时，**丢弃最旧的一条**，为新数据腾位置——发送方**永不阻塞**。

这是典型的「**丢帧**」策略：保证入站线程永远不被拖慢，代价是消费跟不上时会**丢掉历史数据**，只保留最新的若干条。

它非常适合「**只关心最新值**」的场景：传感器上报、股票行情、鼠标位置、日志尾部预览等——这些场景下，一条过时的读数毫无价值，宁可丢掉也要保住实时性。官方的 `z_pull` 示例就用它实现「按自己的节奏拉取最新数据」。

#### 4.3.2 核心流程

`RingChannel` 的实现比 `FifoChannel` 巧妙一些：它不是简单地用 `flume::bounded`，而是「**环形缓冲 + 一个信号量**」的组合：

```
RingChannel { capacity }
        │  into_handler()
        ▼
  Arc<RingChannelInner<T>> { ring: Mutex<RingBuffer<T>>, not_empty: flume::Receiver<()> }
        │                                                    │
   Callback 持有 Arc 的强引用：                 Handler 持有 Weak 引用：
   每条数据 ring.push_force(t)                  recv 时先 Weak::upgrade()，
   （满了自动 pop_front 最旧），                 再 ring.pull() 取一条；
   然后 sender.try_send(()) 通知                  为空则在 not_empty 上等待信号
   「有数据可取」
```

两个关键设计：

1. **`flume::bounded(1)` 只当「通知信号」用**，不存真实数据。真实数据在 `RingBuffer` 里。发送方 `try_send(())` 永不阻塞（最多丢掉一个多余的「通知」，反正数据已落环形缓冲）。
2. **Handler 用 `Weak`**：当订阅被 undeclare（回调及其持有的 `Arc` 被释放）后，`Weak::upgrade()` 失败，`recv` 返回 `bail!("The ringbuffer has been deleted.")`，干净地表示「通道已关闭」。

#### 4.3.3 源码精读

`RingChannel` 与 `FifoChannel` 一样只存 `capacity`，默认同样取自 `API_DATA_RECEPTION_CHANNEL_SIZE`（256）：

[zenoh/src/api/handlers/ring.rs:29-49](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/handlers/ring.rs#L29-L49)：

```rust
/// [`RingChannel`] implements FIFO semantics with a dropping strategy when full.
/// The oldest elements will be dropped when newer ones arrive.
pub struct RingChannel { capacity: usize }

impl RingChannel {
    pub fn new(capacity: usize) -> Self { Self { capacity } }
}

impl Default for RingChannel {
    fn default() -> Self { Self::new(*API_DATA_RECEPTION_CHANNEL_SIZE) }
}
```

核心在 `into_handler`：建一个容量为 **1** 的 `flume` 通道仅作信号用，真实数据放 `RingBuffer`：

[zenoh/src/api/handlers/ring.rs:151-176](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/handlers/ring.rs#L151-L176)：

```rust
fn into_handler(self) -> (Callback<T>, Self::Handler) {
    let (sender, receiver) = flume::bounded(1);                 // 仅作「非空」信号
    let inner = Arc::new(RingChannelInner {
        ring: std::sync::Mutex::new(RingBuffer::new(self.capacity)),
        not_empty: receiver,
    });
    let receiver = RingChannelHandler { ring: Arc::downgrade(&inner) };  // Handler 持 Weak
    (
        Callback::from(move |t| match inner.ring.lock() {
            Ok(mut g) => {
                g.push_force(t);            // ← 满了丢最旧，永不阻塞
                drop(g);
                let _ = sender.try_send(()); // 通知消费者「有数据」
            }
            Err(e) => tracing::error!("{}", e),
        }),
        receiver,
    )
}
```

「丢了最旧」的真正实现来自 `RingBuffer::push_force`，它先尝试 `push`，若返回 `Some(elem)` 说明已满，就把队首 `pop_front` 丢掉、把新元素塞到队尾：

[commons/zenoh-collections/src/ring_buffer.rs:44-51](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-collections/src/ring_buffer.rs#L44-L51)：

```rust
pub fn push_force(&mut self, elem: T) -> Option<T> {
    self.push(elem).and_then(|elem| {
        let ret = self.buffer.pop_front();   // 丢弃最旧
        self.buffer.push_back(elem);         // 新元素入尾
        ret
    })
}
```

消费侧 `recv` 是一个「先试取、取不到就等信号」的循环，且开头先用 `Weak::upgrade()` 判断通道是否还在：

[zenoh/src/api/handlers/ring.rs:62-76](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/handlers/ring.rs#L62-L76)：

```rust
pub fn recv(&self) -> ZResult<T> {
    let Some(channel) = self.ring.upgrade() else {
        bail!("The ringbuffer has been deleted.");     // 通道已关闭
    };
    loop {
        if let Some(t) = channel.ring.lock()?.pull() { // 先试取一条
            return Ok(t);
        }
        channel.not_empty.recv()?;                      // 空了就等信号
    }
}
```

异步版 `recv_async`（[ring.rs:123-137](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/handlers/ring.rs#L123-L137)）和带超时版 `recv_timeout`/`try_recv` 同构，区别只是「等待信号」用异步或限时版本。

> 一句话总结差异：`FifoChannel` 用「满则阻塞 `send`」保数据不丢；`RingChannel` 用「满则 `push_force` 丢最旧 + `try_send(())` 信号」保发送方永不阻塞。

#### 4.3.4 代码实践

**实践目标**：验证 `RingChannel` 在快速连发后只保留「最新 N 条」，并对比 `FifoChannel` 的行为。

**操作步骤（运行官方测试 + 自建对比）**：

1. 直接读官方测试 [zenoh/tests/handler.rs:19-41](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/tests/handler.rs#L19-L41)：容量 3、连发 10 条 `put0..put9`，断言只能 `recv` 到 `put7`/`put8`/`put9`。这是「丢最旧、保最新」的铁证。你可以直接运行它：

   ```bash
   cargo test --package zenoh --test handler pubsub_with_ringbuffer
   ```

2. 自己写一个对比小程序（**示例代码**），用本讲规格要求的方式——`RingChannel::new(10)` 连发 50 条：

   ```rust
   use zenoh::{handlers::RingChannel, Config};

   #[tokio::main]
   async fn main() {
       let session = zenoh::open(Config::default()).await.unwrap();
       let sub = session.declare_subscriber("demo/ring")
           .with(RingChannel::new(10))           // 容量 10
           .await.unwrap();

       // 先不消费，快速连发 50 条
       for i in 0..50u32 {
           session.put("demo/ring", format!("m{i}")).await.unwrap();
       }
       // 给一点时间让回调把数据推进环形缓冲
       tokio::time::sleep(std::time::Duration::from_millis(200)).await;

       // 现在才开始读，应当只能拿到最新的若干条
       while let Ok(s) = sub.try_recv().unwrap() {
           println!("{}", s.payload().try_to_string().unwrap());
       }
   }
   ```

   然后把 `.with(RingChannel::new(10))` 换成 `.with(zenoh::handlers::FifoChannel::new(10))` 再跑一次（注意：FIFO 在慢消费时会对 `put` 产生背压，50 条可能不会「瞬间」全部入库）。

**需要观察的现象**：
- `RingChannel` 版：打印出的是**编号最大的那批**（接近 `m49`，至多 10 条），中间的小编号被丢弃。
- `FifoChannel` 版：不会丢数据，但发布阶段的 `put` 会因背压而变慢，最终能收到全部 50 条（按顺序）。

**预期结果**：`RingChannel` 体现「保最新、丢历史」；`FifoChannel` 体现「全保留、有背压」。两种通道的取舍一目了然。运行结果依赖本机调度，若无法稳定复现，标注「待本地验证」。

> 提示：官方还有一个生产级示例 `z_pull`，正是用 `RingChannel` 实现「订阅端按自己的节奏拉取最新值」，代码见 [examples/examples/z_pull.rs:30-35](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_pull.rs#L30-L35)，可作扩展阅读。

#### 4.3.5 小练习与答案

**练习 1**：`RingChannel::into_handler` 里建的 `flume::bounded(1)` 通道，容量为什么是 1？它存的是真实数据吗？

**参考答案**：容量为 1 是因为它**不存真实数据**，只当「有新数据可取」的信号。真实数据在 `RingBuffer` 里；`flume` 通道只负责唤醒阻塞在 `recv` 的消费者。即便信号因 `try_send` 多发而被丢弃也无妨，因为消费者醒来后会循环 `pull` 把环形缓冲里所有数据取空。

**练习 2**：为什么 `RingChannelHandler` 用 `Weak` 而非 `Arc` 持有 `RingChannelInner`？

**参考答案**：回调（`Callback`）持有 `Arc` 强引用，是「数据生命周期」的真正主人。当订阅被 undeclare、回调被释放时，`Arc` 计数归零、`RingChannelInner` 被销毁。此时仍可能存在的 `RingChannelHandler` 持有的是 `Weak`，`upgrade()` 返回 `None`，`recv` 据此返回 `bail!("The ringbuffer has been deleted.")`，干净地表示通道已关闭，避免了「Handler 比 inner 活得久」导致的悬垂引用。

---

## 5. 综合实践

设计一个贯穿本讲的小任务：**为同一条 key 实现两种订阅策略并对比，用数据回答「我该选 FIFO 还是 Ring？」**

任务步骤：

1. **准备发布源**：写一个发布端，向 `sensor/temp` 高频发布带序号的温度（例如每 10ms 一条，共 100 条，payload 为 `t000`..`t099`）。
2. **慢消费者 A（FIFO）**：订阅端用 `.with(FifoChannel::new(16))`，每收到一条 `sleep 50ms`。统计：最终收到多少条？是否丢？发布端总耗时多少（背压体现在哪）？
3. **慢消费者 B（Ring）**：订阅端改用 `.with(RingChannel::new(16))`，同样每条 `sleep 50ms`。统计：最终收到多少条？收到的是哪些序号（是否是最新的一批）？发布端总耗时是否显著缩短（因为无背压）？
4. **结论**：把两组数据填进下表，并写一段中文结论，说明在「不能丢数据」和「只要最新值」两种需求下分别该选哪个。

   | 策略 | 收到条数 | 收到的序号范围 | 发布端总耗时 | 是否丢数据 |
   | --- | --- | --- | --- | --- |
   | FifoChannel(16) | ？ | ？ | ？ | ？ |
   | RingChannel(16) | ？ | ？ | ？ | ？ |

5. **加分项**：再试一次默认 handler（不写 `.with`），观察它等价于哪一种，印证「默认即 `FifoChannel`」。

> 提示：若高频发布与背压在本机不易稳定复现，可把发布间隔调小、消费 `sleep` 调大来放大效应；并始终保留对真实运行结果的「待本地验证」标注，不要编造数字。

---

## 6. 本讲小结

- Zenoh 取数有「**回调**」和「**通道**」两种姿势，但底层**只有回调**；通道经 `IntoHandler::into_handler()` 拆成「塞数据的回调 + 取数据的句柄」二元组，这是「对外灵活、对内单一」的收口设计。
- builder 的 `.with(any IntoHandler)` 是通用入口，`.callback(f)` 只是 `.with(Callback::from(f))` 的语法糖；不指定时用 `DefaultHandler`（内部即 `FifoChannel`）。
- `FifoChannel`（默认）：基于 `flume::bounded`，**满了阻塞发送方**，形成**背压**，保证不丢数据，但慢消费者会拖慢 Zenoh 入站线程。
- `RingChannel`：基于 `RingBuffer`（`push_force` 丢最旧）+ 容量为 1 的 `flume` 信号通道，**满了丢最旧**，发送方**永不阻塞**，适合「只关心最新值」的场景。
- 默认容量统一来自可配置静态变量 `API_DATA_RECEPTION_CHANNEL_SIZE`（默认 256）；`RingChannelHandler` 用 `Weak` 引用，订阅 undeclare 后 `recv` 会返回「ringbuffer 已删除」错误。
- `Subscriber<Handler>` 通过 `Deref` 暴露 `Handler` 的方法，所以通道姿势下可直接 `sub.recv_async()`；而回调姿势下 `Handler = ()`，没有这些方法。

## 7. 下一步学习建议

- **横向推广**：`IntoHandler` 不只服务 `Subscriber`。`Queryable`、`get`/`Query`（收 `Reply`）、`MatchingListener` 都用同一套 `.with()` / `.callback()`，建议读 [zenoh/src/api/builders/](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/) 下各 builder，验证「同一机制、不同数据类型」。
- **下一讲 u3-l3（QoS）**：从「怎么取数据」转向「数据怎么传」——可靠性（Reliable/BestEffort）、拥塞控制（Block/Drop）、优先级（Priority）。你会发现 QoS 的 `CongestionControl::Drop/Block` 与本讲的 `Ring/Fifo` 在哲学上遥相呼应：一个在传输层、一个在应用层，都在「丢 vs 等」之间做选择。
- **深入内部**：本讲的 `Callback` 最终由 `net` 层的入站回调线程触发。学完 QoS 后可进入第 7 单元（Session 内部与 Runtime），看 `Callback::call` 是如何从网络层一路传到这里的。
