# AsyncClient：统一的异步客户端接口

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清 `AsyncClient` trait 存在的原因：让「内部客户端」（嵌入 Broker 的进程内客户端）和「外部客户端」（通过 socket 连接代理的 IPC 客户端）在同一套接口下可互换使用。
- 逐个说出 trait 里每个方法的语义，并能按「消息投递 / 订阅控制 / 连接与生命周期」三类给方法归类。
- 解释 `OpConfirm` 这个「确认通道」是什么、它为什么是 `Option`、以及 `QoS::needs_ack()` 如何决定它是否为 `Some`。
- 看懂 `get_connected_beacon`、`is_connected`、`get_timeout` 等连接状态接口，并明白它们在两种实现里为何取值不同。

本讲是第四单元（IPC 客户端）的起点。在进入 `ipc.rs` 的连接、握手、帧编解码细节之前，我们先把「客户端长什么样」这件事用 trait 钉死，这样后续读到任何客户端实现都能对号入座。

## 2. 前置知识

本讲默认你已经掌握前几讲的内容，尤其：

- **三种通信模式**（点对点 `send` / 广播 `send_broadcast` / 发布订阅 `publish`）以及它们在 broker 内的分发差异（见 u3-l3）。
- **`QoS` 的两个位**：低位 `needs_ack()`（是否要代理回 ACK），高位 `is_realtime()`（是否立即刷新出站）（见 u2-l1）。
- **`borrow::Cow`** 作为所有发送方法的载荷类型（见 u2-l2），以及 `Frame = Arc<FrameData>`、`EventChannel = async_channel::Receiver<Frame>`（见 u1-l3、u2-l1）。
- **`broker::Client`** 是内部客户端句柄，没有连接概念（见 u3-l1）。

补充两个 Rust 基础概念，初学者可能不熟：

- **`async_trait`**：Rust 原生的 trait 里不能直接写 `async fn`（在当前稳定版下 trait 里的 async fn 有诸多限制），`#[async_trait]` 宏会把 `async fn foo(&self)` 改写成返回 `Pin<Box<dyn Future + Send>>` 的普通方法，从而让 trait 可以拥有异步方法。
- **`tokio::sync::oneshot`**：一个「一次性」的单生产者单消费者通道，只能发一个值。`Sender::send` 一次后通道关闭，`Receiver`（通过 `.await`）拿到那唯一一个值。BUS/RT 用它来做「确认」（confirm）。

## 3. 本讲源码地图

本讲只涉及三个文件，且核心都在前两个：

| 文件 | 作用 | 本讲关注点 |
| --- | --- | --- |
| `src/client.rs` | 定义 `AsyncClient` trait | trait 的全部方法签名与默认实现 |
| `src/lib.rs` | 库根，公共类型 | `OpConfirm` 与 `EventChannel` 两个类型别名 |
| `src/broker.rs` | Broker 与内部客户端 | `impl AsyncClient for Client`（内部客户端实现，作为参照） |

此外，本讲的「对照实践」会引用 `src/ipc.rs` 中 `impl AsyncClient for Client`（外部客户端实现）的关键几行，用来和内部客户端做对比——但 ipc 的完整精读留给下一讲（u4-l2）。

## 4. 核心概念与源码讲解

### 4.1 AsyncClient trait 全景与两个公共类型

#### 4.1.1 概念说明

BUS/RT 有两种截然不同的「客户端」：

- **内部客户端**（`broker::Client`）：你把 Broker 嵌进自己的进程，注册一个客户端，通信走进程内异步通道，没有 socket、没有握手、没有断线。
- **外部客户端**（`ipc::Client`）：你的程序通过 Unix socket / TCP / WebSocket 连到一个独立运行的代理，通信走真实的网络字节流，有握手、有心跳、会断线。

如果这两种客户端各自一套 API，那么上层的 RPC 框架、用户业务代码就得为每种客户端写一遍。`AsyncClient` trait 的存在就是为了消除这种重复：**它把「一个客户端能做什么」抽象成一组统一的异步方法**，任何实现了该 trait 的类型——不管是进程内的还是跨网络的——都可以被同一份调用代码使用。这也是 BUS/RT 能让 RPC 层（`RpcClient`）不关心底层是内部客户端还是 IPC 客户端的关键。

trait 里频繁出现两个来自 `lib.rs` 的类型别名，先记住它们的形状：

- `EventChannel`：客户端**接收**消息的通道接收端，类型是 `async_channel::Receiver<Frame>`（`Frame = Arc<FrameData>`）。客户端被创建时，broker / ipc 都会给你一个 `EventChannel`，你从它 `.recv()` 就能拿到每一帧入站消息。
- `OpConfirm`：一次发送操作的「确认凭据」，类型是 `Option<tokio::sync::oneshot::Receiver<Result<(), Error>>>`。它要么是 `None`（不需要确认），要么是一个 oneshot 接收端（`await` 它就能拿到「代理是否处理成功」的结果）。4.3 节会专门讲它。

#### 4.1.2 核心流程

一个客户端实现的「轮廓」由 trait 规定：

```text
AsyncClient
├── 消息投递（出站）
│   ├── send / zc_send          点对点
│   ├── send_broadcast          广播
│   └── publish / publish_for   发布订阅
├── 订阅控制（出站，告诉代理自己的订阅意愿）
│   ├── subscribe / unsubscribe / *_bulk
│   └── exclude / unexclude / *_bulk
├── 入站通道
│   └── take_event_channel      拿走 EventChannel 交给自己写的消费循环
└── 连接与生命周期（同步方法）
    ├── ping
    ├── is_connected
    ├── get_connected_beacon
    ├── get_timeout
    └── get_name
```

注意一个重要的设计：**所有消息投递方法都返回 `Result<OpConfirm, Error>` 而不是 `Result<(), Error>`**。也就是说，「发送」本身不等待结果，而是把「能否拿到结果」的选择权交给调用方——调用方可以选择 `.await` 那个 `OpConfirm` 来等待代理确认，也可以直接丢弃它（fire-and-forget）。这个设计兼顾了高吞吐（不强制等确认）和可靠性（需要时能确认）。

#### 4.1.3 源码精读

trait 定义在 [src/client.rs:10-66](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/client.rs#L10-L66)，整个 trait 用 `#[async_trait]` 标注，要求实现者是 `Send + Sync`。开头几行已经能看到两个公共类型的使用：

```rust
use crate::borrow::Cow;
use crate::{Error, EventChannel, OpConfirm, QoS};
// ...
#[async_trait]
pub trait AsyncClient: Send + Sync {
    fn take_event_channel(&mut self) -> Option<EventChannel>;
    async fn send(
        &mut self,
        target: &str,
        payload: Cow<'async_trait>,
        qos: QoS,
    ) -> Result<OpConfirm, Error>;
    // ... 其余方法
}
```

载荷类型是 `Cow<'async_trait>`——这里的 `'async_trait` 是 `#[async_trait]` 宏注入的生命周期，让 `Cow` 的借用期与本次异步调用绑在一起（见 u2-l2）。所有发送方法签名高度一致，区别只在参数个数（是否带 `header`、是否带 `receiver`、是否是批量）。

两个公共类型别名定义在 [src/lib.rs:69-72](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L69-L72)：

```rust
#[cfg(any(feature = "rpc", feature = "broker", feature = "ipc"))]
pub type OpConfirm = Option<tokio::sync::oneshot::Receiver<Result<(), Error>>>;
pub type Frame = Arc<FrameData>;
pub type EventChannel = async_channel::Receiver<Frame>;
```

注意 `OpConfirm` 自身被 `#[cfg(any(feature = "rpc", feature = "broker", feature = "ipc"))]` 守卫——也就是只要有这三个 feature 之一，它就会被编译出来；而 `EventChannel` 和 `Frame` 没有守卫，始终编译（见 u1-l3 的「始终编译的公共契约」）。`client` 模块本身的声明 [src/lib.rs:520-521](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L520-L521) 也用同一个 `any(...)` 守卫，保证 `AsyncClient` trait 与 `OpConfirm` 同进退。

#### 4.1.4 代码实践

**实践目标**：建立对 trait 规模与结构的整体印象。

**操作步骤**：

1. 打开 [src/client.rs:10-66](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/client.rs#L10-L66)。
2. 数一下 trait 里一共有多少个方法（含默认实现）。
3. 把它们按 4.1.2 的「投递 / 订阅控制 / 入站 / 连接」四类分别列出来。

**需要观察的现象**：你会发现 `publish_for` 是唯一带**默认实现**的方法（默认返回 `Err(Error::not_supported("publish_for"))`），其余都是必须实现的抽象方法。

**预期结果**：共 18 个方法（`take_event_channel` + 5 个投递 + 8 个订阅控制 + `ping` + `is_connected` + `get_connected_beacon` + `get_timeout` + `get_name`），其中 `publish_for` 有默认实现。这个「默认实现」意味着：一个客户端实现可以**选择不支持** `publish_for` 而不必留空。对照 4.4 节你会发现 `broker::Client` 覆盖了它（支持），而 trait 的默认实现则留给那些暂不支持定向发布的实现兜底。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `AsyncClient` 要标注 `Send + Sync`？
**答案**：因为它会被用于 Tokio 的异步运行时，客户端对象常常需要在多个异步任务之间共享（例如一个任务发送、另一个任务接收），`Send + Sync` 才能安全地跨线程/跨任务传递。

**练习 2**：`take_event_channel` 返回 `Option<EventChannel>`，为什么是 `Option` 而不是直接 `EventChannel`？
**答案**：因为事件通道只能被「拿走」一次（`take` 语义）。第一次调用返回 `Some(rx)`，之后再调用就返回 `None`，避免同一个入站通道被两个消费循环同时读取。

---

### 4.2 三类方法语义逐一拆解

#### 4.2.1 概念说明

把 trait 的方法按职责分成三组来记，比平铺 18 个方法清晰得多：

**第一组：消息投递**（出站，把数据发出去）

| 方法 | 模式 | 是否带 header | 说明 |
| --- | --- | --- | --- |
| `send(target, payload, qos)` | 点对点 | 否 | 发给指定对端，未命中返回 `not_registered` |
| `zc_send(target, header, payload, qos)` | 点对点 | 是 | 零拷贝版本，附带 header（线程内/RPC 携带元数据） |
| `send_broadcast(target, payload, qos)` | 广播 | 否 | 按对端名掩码一对多 |
| `publish(topic, payload, qos)` | 发布订阅 | 否 | 按主题掩码投递给订阅者 |
| `publish_for(topic, receiver, payload, qos)` | 发布订阅 | 否 | 定向发布，只在订阅者里挑出 `receiver` 主名匹配的（有默认实现） |

**第二组：订阅控制**（出站，声明自己对哪些主题感兴趣）

- `subscribe(topic, qos)` / `unsubscribe(topic, qos)`：订阅/退订单个主题。
- `subscribe_bulk(topics, qos)` / `unsubscribe_bulk(...)`：批量订阅/退订，一次操作多个主题。
- `exclude(topic, qos)` / `unexclude(topic, qos)`：排除/取消排除某主题——排除后即便订阅了也不会收到（见 u3-l3 的排除机制）。
- `exclude_bulk` / `unexclude_bulk`：批量版本。

注意源码里这些方法上方的注释 [src/client.rs:51-60](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/client.rs#L51-L60) 给了一个重要建议：**先 exclude 再 subscribe**，否则可能在排除生效前已经收到不想要的消息。

**第三组：连接与生命周期**（多为同步方法，不异步）

- `take_event_channel`：拿走入站通道（已在 4.1 讲）。
- `ping()`：探活。异步方法，返回 `Result<(), Error>`。
- `is_connected() -> bool`：当前是否连着。
- `get_connected_beacon() -> Option<Arc<AtomicBool>>`：返回一个「连接信标」——一个共享的原子布尔，连接状态变化时会被翻转，外部可以轮询或等待它而不必反复调用 `is_connected`。
- `get_timeout() -> Option<Duration>`：这个客户端的操作超时，`None` 表示没有超时（即永远不会因超时而失败）。
- `get_name() -> &str`：客户端的名字（注册时给的唯一名）。

#### 4.2.2 核心流程

一个典型客户端的使用流程，体现了三组方法的协作：

```text
1. 创建客户端（broker::register_client 或 ipc::Client::connect）
2. take_event_channel()  -> 拿到 rx，单独 spawn 一个任务循环 rx.recv()
3. subscribe("#", qos)   -> 声明订阅（订阅控制组）
4. send/publish(...)     -> 业务发消息（投递组），拿到 OpConfirm
5. （可选）op_confirm.await -> 等代理确认（仅 QoS::Processed 才有意义）
6. 循环里 is_connected() / get_connected_beacon() 监测连接
7. ping() 周期性探活
```

第 4 步和第 5 步的分离，正是 BUS/RT 「发送不等结果、确认可选」设计的体现。

#### 4.2.3 源码精读

订阅控制组的注释和签名见 [src/client.rs:47-60](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/client.rs#L47-L60)：

```rust
async fn subscribe(&mut self, topic: &str, qos: QoS) -> Result<OpConfirm, Error>;
async fn unsubscribe(&mut self, topic: &str, qos: QoS) -> Result<OpConfirm, Error>;
async fn subscribe_bulk(&mut self, topics: &[&str], qos: QoS) -> Result<OpConfirm, Error>;
async fn unsubscribe_bulk(&mut self, topics: &[&str], qos: QoS) -> Result<OpConfirm, Error>;
/// exclude a topic. it is highly recommended to exclude topics first, then call subscribe
/// operations to avoid receiving unwanted messages. ...
async fn exclude(&mut self, topic: &str, qos: QoS) -> Result<OpConfirm, Error>;
```

连接与生命周期组见 [src/client.rs:61-65](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/client.rs#L61-L65)：

```rust
async fn ping(&mut self) -> Result<(), Error>;
fn is_connected(&self) -> bool;
fn get_connected_beacon(&self) -> Option<Arc<atomic::AtomicBool>>;
fn get_timeout(&self) -> Option<Duration>;
fn get_name(&self) -> &str;
```

注意 `ping` 的返回类型是 `Result<(), Error>`，**不是** `Result<OpConfirm, Error>`——它和投递方法不同，不需要确认凭据，因为 ping 本身就是一次往返探活，成功或失败直接体现在 `Result` 里。

#### 4.2.4 代码实践

**实践目标**：用真实源码验证三类划分。

**操作步骤**：

1. 打开 [src/client.rs:10-66](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/client.rs#L10-L66)。
2. 逐行把每个方法归入「投递 / 订阅控制 / 入站 / 连接」四类。
3. 特别留意返回类型：哪些返回 `OpConfirm`，哪些返回 `()`，哪些返回 `bool` / `Option`。

**需要观察的现象**：只有投递组和订阅控制组的方法返回 `Result<OpConfirm, Error>`；`ping` 返回 `Result<(), Error>`；`is_connected` 返回裸 `bool`；`get_*` 系列返回 `Option` 或 `&str`。

**预期结果**：你会得出与 4.2.1 表格一致的结论，并且能解释「为什么 `ping` 不返回 `OpConfirm`」——因为探活本身就是要拿到一个明确的成败结果，不需要再套一层可选确认。

#### 4.2.5 小练习与答案

**练习 1**：`subscribe_bulk(topics: &[&str], qos)` 里的 `topics` 为什么是 `&[&str]` 而不是 `Vec<String>`？
**答案**：因为调用方通常手里已有若干字符串字面量或借用，传切片借用可以避免无谓的堆分配和所有权转移；trait 方法只读这些主题名，不需要拥有它们。

**练习 2**：注释为什么建议「先 exclude 再 subscribe」？
**答案**：exclude 是在订阅候选集上做二次过滤的开关（见 u3-l3）。如果先 subscribe 再 exclude，在两者之间到达的主题消息不会被排除，调用方会收到本不想要的消息。先 exclude 让过滤规则就位，再订阅就安全了。

---

### 4.3 OpConfirm：oneshot 确认通道与 QoS 的协同

#### 4.3.1 概念说明

`OpConfirm` 是理解 BUS/RT 可靠性模型的核心。它的定义是：

```rust
pub type OpConfirm = Option<tokio::sync::oneshot::Receiver<Result<(), Error>>>;
```

读法：「一次发送操作，可能附带、也可能不附带一个『确认接收端』」。

- 当它是 `None`：表示这次发送**不需要**代理确认（`QoS::No` 或 `QoS::Realtime`）。调用方拿到 `Ok(None)` 后直接丢弃即可，相当于 fire-and-forget。
- 当它是 `Some(rx)`：表示这次发送**需要**代理确认（`QoS::Processed` 或 `QoS::RealtimeProcessed`）。调用方可以 `rx.await` 拿到 `Result<(), Error>`——`Ok(())` 表示代理已处理，`Err(e)` 表示代理返回了错误（比如 `not_registered`、`access` 等）。

`lib.rs` 里有一段官方注释把这套用法讲得很清楚，见 [src/lib.rs:51-68](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L51-L68)，核心三步：

```rust,ignore
let result = client.send("target", payload, QoS::Processed).await.unwrap(); // 拿到 Result
let confirm = result.unwrap();                                  // 拿到 OpConfirm
let op_result = confirm.await.unwrap();                         // 收到操作结果
```

#### 4.3.2 核心流程

`OpConfirm` 是不是 `Some`，完全由这次调用的 `QoS` 决定——确切地说，由 `QoS::needs_ack()`（低位）决定：

```text
发送方法被调用，传入 qos
        │
        ▼
qos.needs_ack()?──── true ──► 创建 oneshot (tx, rx)
        │                     把 rx 作为 OpConfirm 返回 (Some(rx))
        │ no                  (内部客户端：立刻 tx.send(Ok(()))，rx 即时兑现)
        ▼                     (外部客户端：把 tx 按 frame_id 登记进 responses 表，
        │                      等代理回 OP_ACK 帧时再兑现)
       None ──────────────► Ok(None)  (无需确认，直接返回)
```

这里出现两种客户端的关键分歧（下一节详讲）：

- **内部客户端**：确认是**假的**——它在返回 `Some(rx)` 之前就已经 `tx.send(Ok(()))`，所以 `rx.await` 立刻拿到成功，根本没有真正的往返。
- **外部客户端**：确认是**真的**——它把 `tx` 登记进一个 `responses` 映射，等代理通过网络回送 `OP_ACK` 帧时，用帧里的 `op_id` 找到对应的 `tx` 并兑现。这是一个真实的网络往返。

#### 4.3.3 源码精读

内部客户端的确认由 `make_confirm_channel!` 宏生成，见 [src/broker.rs:71-81](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L71-L81)：

```rust
macro_rules! make_confirm_channel {
    ($qos: expr) => {
        if $qos.needs_ack() {
            let (tx, rx) = tokio::sync::oneshot::channel();
            let _r = tx.send(Ok(()));          // ← 立刻兑现，不等任何东西
            Ok(Some(rx))
        } else {
            Ok(None)
        }
    };
}
```

`broker::Client` 的每个投递/订阅方法末尾都会调用这个宏，例如 `send` 见 [src/broker.rs:348-368](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L348-L368)：先执行真正的分发 `send!(...)`，再 `make_confirm_channel!(qos)`。对内部客户端而言，分发是同步入队（成功即成功），所以「立刻发 `Ok(())`」是诚实的——消息确实已经进了对端的入站通道。

外部客户端的确认由 `send_frame_and_confirm!` 宏生成，见 [src/ipc.rs:165-180](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L165-L180)：

```rust
macro_rules! send_frame_and_confirm {
    ($self: expr, $buf: expr, $payload: expr, $qos: expr) => {{
        let rx = if $qos.needs_ack() {
            let (tx, rx) = oneshot::channel();
            { $self.responses.lock().insert($self.frame_id, tx); } // ← 按 frame_id 登记 tx
            Some(rx)
        } else {
            None
        };
        send_data_or_mark_disconnected!($self, $buf, Flush::No);   // 先发帧头
        send_data_or_mark_disconnected!($self, $payload, $qos.is_realtime().into()); // 再发载荷
        Ok(rx)
    }};
}
```

对照可以看到：外部客户端**不立刻兑现** `tx`，而是把它塞进 `responses` 映射（以 `frame_id` 为键），然后才把帧真正写到 socket。这个 `tx` 何时被兑现？要等代理回送 `OP_ACK` 帧（含 `op_id`），由 `handle_read` 在 `responses` 里查到对应 `tx` 并 `send` 结果——这是下一讲（u4-l2）和协议讲（u2-l3）的内容。本讲只需记住：**外部客户端的确认是一个真实的、跨网络的、会被代理 ACK 兑现的 oneshot**。

#### 4.3.4 代码实践

**实践目标**：亲手验证 `QoS` 如何决定 `OpConfirm` 是 `Some` 还是 `None`。

**操作步骤**（源码阅读型）：

1. 读 [src/broker.rs:71-81](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L71-L81)（`make_confirm_channel!`）。
2. 回顾 u2-l1 中 `QoS::needs_ack()` 的实现：`self as u8 & 0b1 != 0`，即 `Processed(1)` 和 `RealtimeProcessed(3)` 返回 `true`，`No(0)` 和 `Realtime(2)` 返回 `false`。
3. 列一张表：对四种 QoS，`make_confirm_channel!` 返回的是 `Ok(Some(rx))` 还是 `Ok(None)`？

**需要观察的现象**：`needs_ack` 完全由 QoS 的低位决定，与「是否实时」无关。

**预期结果**：

| QoS | `needs_ack()` | `OpConfirm` |
| --- | --- | --- |
| `No` (0) | false | `Ok(None)` |
| `Processed` (1) | true | `Ok(Some(rx))`，且内部客户端已即时兑现 `Ok(())` |
| `Realtime` (2) | false | `Ok(None)` |
| `RealtimeProcessed` (3) | true | `Ok(Some(rx))` |

如果你想在本地真正跑一遍，可以参照 `examples/inter_thread.rs` 嵌入一个 Broker，分别用 `QoS::No` 和 `QoS::Processed` 调用 `send`，对返回值做模式匹配打印（`Some` 时再 `.await`）。运行结果：**待本地验证**（取决于你是否配置了 Rust 开发环境）。

#### 4.3.5 小练习与答案

**练习 1**：为什么内部客户端可以在返回 `Some(rx)` 之前就 `tx.send(Ok(()))`，而外部客户端不能？
**答案**：内部客户端的「发送」是把帧直接放进对端的进程内通道，这个动作在 `send!` 宏里已经完成（成功即入队），所以「已处理」是既成事实，可以立刻兑现。外部客户端的「发送」只是把字节写进 socket，代理是否真的处理了要等它回 ACK 才知道，所以 `tx` 必须留到 ACK 到达时才能兑现。

**练习 2**：如果调用方拿到 `OpConfirm = Some(rx)` 后**从不** `.await` 它，会发生什么？
**答案**：不会有功能问题——`rx` 被 drop 时 oneshot 的 `tx` 端发送会失败（`send` 返回 `Err`，但 BUS/RT 用 `let _r = tx.send(...)` 忽略了），代理那边该回的 ACK 照回、`responses` 表里登记的 `tx` 在 `handle_read` 兑现时发现 `rx` 已 drop 也只是无副作用地丢弃。代价仅仅是这一次确认被浪费，不影响后续帧。

---

### 4.4 两个实现对照：broker::Client 与 ipc::Client

#### 4.4.1 概念说明

trait 是契约，不同实现可以有截然不同的「身体」。本节对照两个实现，重点是**连接相关的那几个方法为何取值不同**——这也正是本讲代码实践任务的核心。

先看两个实现各自持有的状态（决定了它们能如何回答连接问题）：

- **`broker::Client`**（内部）[src/broker.rs:250-256](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L250-L256)：持有 `name`、`bus: Arc<BusRtClient>`、`db: Arc<BrokerDb>`、`rx: Option<EventChannel>`。**没有任何代表「连接」的字段**——因为它根本不在网络上，它和 broker 同处一个进程，注册成功后就「永远连着」。
- **`ipc::Client`**（外部）[src/ipc.rs:123-133](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L123-L133)：持有 `writer`、`reader_fut: JoinHandle<()>`、`frame_id`、`responses`、`rx`、**`connected: Arc<AtomicBool>`**、`timeout: Duration`、`config`。其中 `connected` 是一个共享原子布尔，连接断开时会被翻转。

#### 4.4.2 核心流程

| trait 方法 | `broker::Client`（内部） | `ipc::Client`（外部） | 差异原因 |
| --- | --- | --- | --- |
| `is_connected()` | 恒返回 `true` | 读 `self.connected` 原子布尔 | 内部客户端无网络，天然「永连」；外部客户端会断线 |
| `get_timeout()` | 返回 `None` | 返回 `Some(self.timeout)` | 内部客户端无超时概念（入队要么成功要么阻塞）；外部客户端每次 socket 写都有超时 |
| `get_connected_beacon()` | 返回 `None` | 返回 `Some(self.connected.clone())` | 内部客户端没有可分享的连接状态；外部客户端把那个共享原子布尔作为「信标」交出 |
| `ping()` | 直接 `Ok(())`（空操作） | 真正发 `PING_FRAME` 到代理 | 内部客户端无需探活；外部客户端用 ping 检测链路存活 |
| `get_name()` | `self.name.as_str()` | `self.name.as_str()` | 一致：都返回注册名 |
| 投递方法实现 | 直接调 broker 的 `send!`/`publish!` 等宏（进程内分发） | 调 `send_frame!` 把帧编码成字节写 socket | 一个是内存路由，一个是网络序列化 |
| `OpConfirm` 来源 | `make_confirm_channel!`（即时兑现） | `send_frame_and_confirm!`（登记 tx，等 ACK） | 见 4.3 节 |

#### 4.4.3 源码精读

`broker::Client` 的连接相关实现见 [src/broker.rs:456-479](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L456-L479)：

```rust
fn take_event_channel(&mut self) -> Option<EventChannel> { self.rx.take() }
async fn ping(&mut self) -> Result<(), Error> { Ok(()) }              // ← 空操作
fn is_connected(&self) -> bool { true }                               // ← 恒真
fn get_timeout(&self) -> Option<Duration> { None }                    // ← 无超时
fn get_connected_beacon(&self) -> Option<Arc<atomic::AtomicBool>> { None } // ← 无信标
fn get_name(&self) -> &str { self.name.as_str() }
```

`ipc::Client` 的对应实现见 [src/ipc.rs:530-546](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L530-L546)：

```rust
async fn ping(&mut self) -> Result<(), Error> {
    send_data_or_mark_disconnected!(self, PING_FRAME, Flush::Instant); // ← 真发 PING
    Ok(())
}
fn is_connected(&self) -> bool {
    self.connected.load(atomic::Ordering::Relaxed)                     // ← 读原子布尔
}
fn get_timeout(&self) -> Option<Duration> {
    Some(self.timeout)                                                 // ← 有超时
}
fn get_name(&self) -> &str { self.name.as_str() }
```

`get_connected_beacon` 在外部客户端里返回那个共享原子布尔，见 [src/ipc.rs:418-421](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L418-L421)：

```rust
fn get_connected_beacon(&self) -> Option<Arc<atomic::AtomicBool>> {
    Some(self.connected.clone())
}
```

这个 `connected` 在哪被翻转？在 `connect_broker!` 宏里，reader 任务出错或结束时会把 `connected` 置为 `false`，见 [src/ipc.rs:242-247](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L242-L247)：

```rust
let reader_fut = tokio::spawn(async move {
    if let Err(e) = handle_read($reader, tx, timeout, reader_responses).await {
        error!("busrt client reader error: {}", e);
    }
    rconn.store(false, atomic::Ordering::Relaxed);   // ← 读循环结束 = 断线
});
```

而发送失败时，`send_data_or_mark_disconnected!` 宏 [src/ipc.rs:148-163](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L148-L163) 也会把它置 `false` 并中止 reader。所以这个 `Arc<AtomicBool>` 是外部客户端「连接健康度」的唯一真相来源——这就是 `get_connected_beacon` 存在的意义：让上层（比如 RPC 层的断线重连逻辑）能拿到同一个原子布尔去观测，而不必反复轮询 `is_connected`。

#### 4.4.4 代码实践

**实践目标**（本讲指定任务）：对比 `broker::Client` 与 `ipc::Client` 对 `AsyncClient` 的实现，列出它们在 `is_connected`、`get_timeout`、`ping` 上的差异并解释原因。

**操作步骤**：

1. 打开 `broker::Client` 的实现：[src/broker.rs:456-479](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L456-L479)。
2. 打开 `ipc::Client` 的实现：[src/ipc.rs:530-546](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L530-L546) 以及 `get_connected_beacon` [src/ipc.rs:418-421](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L418-L421)。
3. 针对下表三个方法，逐行填入「内部客户端返回什么 / 外部客户端返回什么 / 为什么不同」。

**需要观察的现象**：内部客户端这三个方法都是「无操作」或「常量」，外部客户端则真正读写状态或网络。

**预期结果**（应填出的对照表）：

| 方法 | `broker::Client`（内部） | `ipc::Client`（外部） | 差异原因 |
| --- | --- | --- | --- |
| `is_connected` | `true`（常量） | `self.connected.load(...)`（读原子布尔） | 内部客户端与 broker 同进程，注册后永不断；外部客户端在 socket 上，reader 结束或发送失败时 `connected` 被置 `false` |
| `get_timeout` | `None` | `Some(self.timeout)` | 内部客户端的「发送」是进程内入队，没有「等待对端」的语义，故无超时；外部客户端每次 socket 写都可能阻塞，需要一个超时上限（默认 1 秒，见 `DEFAULT_TIMEOUT`） |
| `ping` | `Ok(())`（空操作） | 发 `PING_FRAME` 到代理后返回 `Ok(())` | 内部客户端探活无意义（永连）；外部客户端用 ping 主动探测链路是否健康，发送失败时还会标记断线 |

把这张表用自己的话写成一段说明，你就完成了本讲的代码实践。

**进阶（可选）**：再对比 `get_connected_beacon`——内部返回 `None`（没有可分享的连接状态），外部返回 `Some(self.connected.clone())`（把那个共享原子布尔作为「信标」交出，供上层观测/重连）。思考题：为什么不用 `is_connected()` 轮询，而要单独提供 beacon？答案：beacon 是 `Arc` 共享的同一个原子变量，多个观测者拿到的都是同一份真相，且可以在任意线程无锁读取，比反复调方法更高效、更一致。

#### 4.4.5 小练习与答案

**练习 1**：假设你写了一个泛型函数 `fn echo<C: AsyncClient>(c: &C)`，它调用 `c.is_connected()`。在内部客户端和外部客户端上分别会发生什么？
**答案**：内部客户端上恒返回 `true`（这个函数永远认为它「连着」）；外部客户端上返回当前 `connected` 原子布尔的值，断线时为 `false`。这正是 trait 抽象的价值——同一份调用代码，行为各自正确。

**练习 2**：为什么 `broker::Client` 的 `ping` 是 `Ok(())` 空操作，而不是像外部客户端那样返回 `Err` 或 panic？
**答案**：`ping` 的契约是「探活，活着就 Ok」。内部客户端按定义永远活着（与 broker 同进程），所以诚实地返回 `Ok(())` 即可，既不夸大也不报错。返回 `Err` 会误导调用方以为连不上，panic 则破坏了「ping 不致命」的契约。

---

## 5. 综合实践

把本讲的三件事——trait 全景、OpConfirm、两个实现对照——串成一个综合任务：

**任务**：为 `AsyncClient` 写一份「实现者检查清单（checklist）」。

1. 列出 trait 的全部方法，并标注每个方法「内部客户端怎么实现、外部客户端怎么实现」。投递组和订阅控制组可以只各举一个代表（如 `send` 和 `subscribe`），但 `is_connected`/`get_timeout`/`get_connected_beacon`/`ping`/`take_event_channel`/`get_name` 必须全部列出。
2. 对每个方法，在源码里找到内部实现 [src/broker.rs:258-480](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L258-L480) 和外部实现 [src/ipc.rs:412-547](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs#L412-L547) 的行号。
3. 写一段话回答：**如果将来新增第三种客户端实现（比如一个纯内存的 mock 客户端，用于单元测试），它必须至少实现哪些方法？哪些可以用 trait 的默认实现？**
   - 提示：只有 `publish_for` 有默认实现；其余 17 个方法必须自己实现。mock 客户端的 `is_connected` 通常返回 `true`、`ping` 返回 `Ok(())`、`get_timeout` 返回 `None`、投递方法可以把帧塞进一个 `Vec` 供测试断言、`OpConfirm` 可以复用 `make_confirm_channel!` 的思路即时兑现。

完成这份清单后，你不仅能默写 `AsyncClient` 的接口，还能解释为什么 RPC 层（下一单元 u5）可以放心地接受任何 `AsyncClient` 实现而不关心它是进程内的还是跨网络的。

## 6. 本讲小结

- `AsyncClient` 是 BUS/RT 客户端的统一异步契约，定义在 [src/client.rs:10-66](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/client.rs#L10-L66)，让内部客户端（`broker::Client`）和外部客户端（`ipc::Client`）可互换，是 RPC 层与业务代码「与传输无关」的关键。
- trait 的 18 个方法可分三类：消息投递（`send`/`zc_send`/`send_broadcast`/`publish`/`publish_for`）、订阅控制（`subscribe`/`unsubscribe`/`exclude` 及其 bulk 版）、连接与生命周期（`take_event_channel`/`ping`/`is_connected`/`get_connected_beacon`/`get_timeout`/`get_name`）；其中只有 `publish_for` 有默认实现。
- 所有投递与订阅方法返回 `Result<OpConfirm, Error>`，`OpConfirm = Option<oneshot::Receiver<Result<(), Error>>>`（见 [src/lib.rs:69-72](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L69-L72)）：`QoS::needs_ack()` 为真时返回 `Some(rx)`，否则 `None`，体现了「发送不等结果、确认可选」的设计。
- 内部客户端的确认是**即时兑现**的假确认（`make_confirm_channel!` 在返回前就 `tx.send(Ok(()))`），外部客户端的确认是**真实往返**（`send_frame_and_confirm!` 把 `tx` 按 `frame_id` 登记进 `responses` 表，等代理 ACK 兑现）。
- 连接状态接口在两种实现里取值不同：`broker::Client` 的 `is_connected` 恒真、`get_timeout` 为 `None`、`ping` 空操作、`get_connected_beacon` 为 `None`；`ipc::Client` 则读 `connected` 原子布尔、返回 `Some(timeout)`、真发 `PING_FRAME`、并把那个共享原子布尔作为 beacon 交出。

## 7. 下一步学习建议

本讲把「客户端接口长什么样」讲清楚了，但外部客户端 `ipc::Client` 的**内部实现**（如何连接、握手、编码帧、解析入站帧、管理 `responses` 表以兑现 ACK）我们只读了几行。下一讲 **u4-l2（ipc::Client：连接代理与帧收发）** 会深入 [src/ipc.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/ipc.rs) 的 `Config`、`connect`/`connect_broker` 启动流程、`chat` 握手与 `handle_read` 帧解析循环，把本讲提到的 `send_frame_and_confirm!`、`responses` 登记、`connected` 翻转等细节全部接上。

之后 **u4-l3（TtlBufWriter：TTL 缓冲与刷新策略）** 会解释 `send_data_or_mark_disconnected!` 里那个 `Flush::No` / `Flush::Instant` 参数的来历——即外部客户端如何在吞吐与实时性之间取舍。

建议继续精读的源码：`src/client.rs`（本讲主角）、`src/broker.rs` 第 71-480 行（宏与内部客户端实现）、`src/ipc.rs` 第 148-232 行（发送相关宏）与第 408-547 行（外部客户端的 `AsyncClient` 实现）。
