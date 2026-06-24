# TopicBroker：阻塞式主题分发工具

## 1. 本讲目标

本讲讲解 BUS/RT 工具箱里的一个「胶水」辅助类 `TopicBroker`（定义在 `src/tools/pubsub.rs`）。学完后你应该能够：

- 说清 `TopicBroker` 解决的问题：在一个 RPC 处理器里，如何把收到的发布帧（`FrameKind::Publish`）按主题**隔离**地、**有顺序**地交给不同的处理逻辑。
- 掌握 `register_topic`（精确主题）与 `register_prefix`（主题前缀）两种注册方式，以及它们与 `_with_handler_id` / `_tx` 变体的关系。
- 理解 `process()` 的「先精确、后前缀」匹配规则，以及它与代理内部 `SubMap` 的 `#`/`+` 通配匹配的根本区别。
- 理解 `Publication` 这个分发对象，以及 `subtopic`、`handler_id` 在多处理器场景里各自的用途。
- 能在一个真实的 `RpcHandlers::handle_frame` 里挂上 `TopicBroker`，写出可运行（或可本地验证）的分流程序。

本讲是「高级扩展」单元的一环，和 u7-l2 的游标（cursors）形成对照：游标用 **UUID** 隔离有状态的数据流，而 `TopicBroker` 用 **主题字符串** 隔离无状态的发布帧。

---

## 2. 前置知识

本讲默认你已经理解以下概念（它们在前置讲义里讲过，这里只做一句话回顾）：

- **发布订阅模式与主题树**（u3-l3）：代理用 `SubMap` 把主题按 `/` 分层，`+` 匹配单层、`#` 匹配多层；`publish` 出去的帧带 topic，订阅了匹配主题的客户端才会收到，收到时帧的 `kind` 是 `FrameKind::Publish`。
- **统一异步客户端接口 `AsyncClient`**（u4-l1）：`subscribe`、`send`、`publish` 等方法；`take_event_channel()` 取走入站帧通道 `EventChannel`。
- **RPC 处理器 `RpcHandlers` 与 `processor` 事件循环**（u5-l2）：`RpcClient` 启动时会 spawn 一个 `processor`，从 `EventChannel` 逐帧读取：`kind == FrameKind::Message` 的帧解析成 `RpcEvent` 交给 `handle_call`/`handle_notification`，**其余帧**（包括 broadcast 和 publish）交给 `handle_frame`。
- **阻塞模式选项 `blocking_frames`**（u5-l2）：开启后 `handle_frame` 在 `processor` 任务里**串行内联**执行（不再为每帧 spawn），保证收帧顺序，但要求处理器「尽快返回」。

如果上述任何一点对你陌生，建议先回到对应讲义。本讲只聚焦 `TopicBroker` 本身。

---

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/tools/pubsub.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/tools/pubsub.rs) | 本讲主角：`TopicBroker` 与 `Publication`，全文仅 205 行。 |
| [src/rpc/async_client.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs) | `TopicBroker` 的「宿主」：`RpcHandlers` trait、`Options::blocking_frames`、`processor` 事件循环决定了 `handle_frame` 如何被调用。 |
| [src/lib.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs) | feature 门控（`tools::pubsub` 模块声明）、`Error::busy`、`FrameData` 的 `topic()`/`primary_sender()` 等访问器、`FrameKind` 枚举。 |
| examples/client_rpc_handler.rs | `RpcHandlers` 的最小范例，本讲实践将以它为蓝本改造。 |

先记住一句话定位：`TopicBroker` 是一个**纯 Rust 的、传输无关的本地路由器**，它不碰网络、不碰代理，只在你进程内部把「一个收帧入口」拆成「多个按主题隔离的通道」。

---

## 4. 核心概念与源码讲解

### 4.1 为什么需要 TopicBroker：把处理器变成「漏斗」

#### 4.1.1 概念说明

假设你写了一个订阅了 `#`（所有主题）的 RPC 客户端，想对不同主题做不同处理：`a/xxx` 走日志通道、`b/特殊...` 走告警通道、其余主题丢弃。最朴素的写法是在 `handle_frame` 里写一长串 `if topic.starts_with(...) { ... }`。

但这里有两个工程难点：

1. **乱序与并发**。`processor` 默认（非阻塞模式）会为**每一帧** spawn 一个独立的 `handle_frame` 任务（见 4.1.3）。于是同一个主题的多条帧可能**并发**处理、**乱序**完成。如果你的处理逻辑对顺序敏感（比如同一条流水线上「先状态、后测量」），这会出问题。
2. **背压与解耦**。处理逻辑可能很慢（写库、调外部接口）。如果你在 `handle_frame` 里直接做重活，会拖住 `processor` 的收帧循环，进而拖住代理到本客户端的入站通道。

`TopicBroker` 的角色就是一个**漏斗 + 分流器**：它在「快」的收帧路径上只做一件轻量的事——查表、把帧丢进对应主题的 `async_channel`；真正干活的逻辑跑到**独立的消费者任务**里，按主题各自串行、互不阻塞。文档原话称它是「The helper class to process topics in **blocking mode**」。

#### 4.1.2 核心流程

`TopicBroker` 的整体数据流可以画成：

```
代理 ──publish(a/foo)──▶ ipc::Client ──Frame(Publish)──▶ EventChannel
                                                              │
                                                processor 读帧 │ kind != Message
                                                              ▼
                                                   handle_frame(frame)
                                                              │
                                                  topic_broker.process(frame)
                                                              │
                              ┌──────────── 先查精确 topics 表 ───────────┐
                              │ 命中 → 发 Publication 到该主题通道，return  │
                              └──────────── 否则查 prefixes 表 ───────────┘
                                              命中前缀 → 发 Publication，return
                                                              │
                                                  未命中 → return Ok(Some(frame))
                                                              │
              ┌───────────────────┬──────────────────────────┴┬───────────────────┐
              ▼                   ▼                            ▼                   ▼
        消费者 A (a/)       消费者 B (b/特殊)           未匹配处理          （丢弃/默认）
```

关键点：`process` 本身**不做业务**，只做「查表 + 投递」，因此它能在收帧热路径上保持极快；所有耗时逻辑都落在右侧的消费者任务里。

#### 4.1.3 源码精读

`TopicBroker` 与 `Publication` 所在模块只在启用 `rpc`/`broker`/`ipc` 任一 feature 时才编译：

[src/lib.rs:504-507](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L504-L507) — `tools::pubsub` 模块的条件编译声明：只要有客户端或代理能力之一就可用。

要理解 `TopicBroker` 为何要配合 `handle_frame`，先看 `processor` 如何把非 RPC 帧派发给处理器。下面这段是事件循环的收帧主循环与「其余帧」分支：

[src/rpc/async_client.rs:159-262](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L159-L262) — `processor`：`while let Ok(frame) = rx.recv().await` 循环。`kind == FrameKind::Message` 的帧解析为 `RpcEvent` 走 RPC 分支；**publish/broadcast 帧落到末尾的 `else` 分支**交给 `handle_frame`。

末尾这个 `else` 分支正是 `TopicBroker` 的入口：

[src/rpc/async_client.rs:254-261](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L254-L261) — 非 `Message` 帧的处理：若 `blocking_frames` 为真则**内联 await**（串行、保序）；否则 `tokio::spawn`（并发、可能乱序）。

再看「串行」开关本身：

[src/rpc/async_client.rs:52-56](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L52-L56) — `Options::blocking_frames()` 建造者方法，把 `handle_frame` 切到内联串行模式。

> 注意 `processor` 头部那段醒目警告（[src/rpc/async_client.rs:24-34](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L24-L34)）：阻塞模式下「禁止直接使用当前 RPC client，或使用任何会阻塞的有界通道，否则 RPC client 可能卡死」。`TopicBroker` 恰恰用的是有界通道——它之所以**安全**，是因为它的消费者是**独立任务**，且约定**消费者不得同步地回调同一个 `RpcClient`**（否则处理器在 `process().await` 上阻塞、又等不到自己发出的请求回复，形成死锁）。这正是 4.5 节要强调的纪律。

#### 4.1.4 代码实践

源码阅读型实践——跟踪一条 publish 帧的「落地路径」：

1. **实践目标**：确认「publish 帧 → `handle_frame`」这条路径真实存在，而不是 RPC 分支。
2. **操作步骤**：打开 `src/rpc/async_client.rs`，从 `processor` 的 `while let Ok(frame) = rx.recv().await`（[L159](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L159)）开始读，跟随 `if frame.kind() == FrameKind::Message` 判断，确认 `FrameKind::Publish` 会走 `else` 分支（[L254-L261](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L254-L261)）。
3. **需要观察的现象**：你应能画出「publish 帧不进 `RpcEvent::try_from`，而是直接进 `handle_frame`」这一结论。
4. **预期结果**：与 `examples/client_rpc_handler.rs` 的注释「handle broadcast notifications and topic publications」一致。
5. 本步为静态阅读，无需运行。

#### 4.1.5 小练习与答案

**练习**：为什么 `TopicBroker` 文档说它是「为阻塞模式设计」的，而不推荐在默认（spawn）模式下使用？

**参考答案**：默认模式下 `handle_frame` 被逐帧 spawn，同一主题的多条帧会**并发执行、乱序完成**，进入同一通道时 `tx.send` 的先后无法保证，破坏了「按主题串行」的语义。阻塞模式下 `handle_frame` 在 `processor` 里**内联串行**调用，`process()` 按 `rx.recv()` 的顺序逐帧投递，从而保证每个主题通道内的消息严格有序。消费者任务则仍可跨主题并行，兼顾了顺序与吞吐。

---

### 4.2 Publication：分发出去的「主题快照」

#### 4.2.1 概念说明

`process()` 匹配成功后，不会把原始 `Frame` 直接丢给消费者，而是包成一个 `Publication`。可以把它理解成「带路由元数据的主题快照」——它内部持有原始帧（`Frame = Arc<FrameData>`，零拷贝引用计数），外加两个字段：

- `subtopic_pos: usize`：从主题的哪个字节位置开始算「子主题」。精确匹配时为 `0`（子主题就是完整主题）；前缀匹配时为「前缀长度」（子主题是去掉前缀后的剩余部分）。
- `handler_id: usize`：注册通道时附带的整数标签，供「一个通道服务多个主题」时区分来源（见 4.5）。

`Publication` 提供一组只读访问器（`frame()`/`sender()`/`primary_sender()`/`topic()`/`subtopic()`/`payload()`/`header()`/`is_realtime()`/`handler_id()`），让消费者无需关心帧内部的字节布局。

#### 4.2.2 核心流程

`subtopic()` 的计算是本模块最精巧的一点，用一行切片完成：

\[ \text{subtopic} = \text{topic}[\text{subtopic\_pos}..] \]

即「完整主题」砍掉前 `subtopic_pos` 个字节。例如前缀 `"a/"`（长度 2）匹配主题 `"a/foo/bar"`，则 `subtopic_pos = 2`，`subtopic() == "foo/bar"`。

#### 4.2.3 源码精读

`Publication` 的结构与方法：

[src/tools/pubsub.rs:8-12](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/tools/pubsub.rs#L8-L12) — 三个私有字段 `subtopic_pos` / `frame` / `handler_id`，其中 `frame` 是 `Arc<FrameData>`，克隆廉价。

子主题切片的实现，以及文档里那句「all processed frames always have topics」的保证：

[src/tools/pubsub.rs:38-40](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/tools/pubsub.rs#L38-L40) — `subtopic()` 直接对 `topic().unwrap()` 做字节切片；注释声明 `process` 只会针对「带 topic 的帧」构造 `Publication`，故 `unwrap` 安全。

`topic()` 与 `sender()` 其实是对底层 `FrameData` 同名方法的转发：

[src/tools/pubsub.rs:31-33](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/tools/pubsub.rs#L31-L33) — `topic()` 转发 `self.frame.topic().unwrap()`（`process` 已保证非空）。

底层访问器的定义在 `lib.rs`，便于你对照「topic 从哪来」：

[src/lib.rs:477-481](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L477-L481) — `FrameData::topic()` 返回 `Option<&str>`，发布帧有值、点对点帧为 `None`。这正是 `process` 里用 `if let Some(topic) = frame.topic()` 判别的依据。

#### 4.2.4 代码实践

源码阅读 + 推理型实践：

1. **实践目标**：在不运行的前提下，推断出几个典型主题的 `subtopic()` 值。
2. **操作步骤**：阅读 `process()`（[L179-L203](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/tools/pubsub.rs#L179-L203)）里 `subtopic_pos` 的取值：精确匹配分支为 `0`，前缀分支为 `pfx.len()`。
3. **需要观察的现象**：自行填表。

| 注册方式 | 入站主题 | `subtopic_pos` | `subtopic()` 结果 |
| --- | --- | --- | --- |
| 精确 `register_topic("a/foo", ..)` | `a/foo` | 0 | `a/foo` |
| 前缀 `register_prefix("a/", ..)` | `a/foo/bar` | 2 | `foo/bar` |
| 前缀 `register_prefix("b/特殊", ..)` | `b/特殊` | `b/特殊`.len() | ``（空串） |
| 前缀 `register_prefix("b/特殊", ..)` | `b/特殊/x` | `b/特殊`.len() | `/x` |

4. **预期结果**：上表第三、四行的「子主题」对你理解前缀匹配的边界很关键——前缀恰好等于主题时子主题为空串；多字节 UTF-8（如「特殊」二字）`pfx.len()` 按字节而非字符计，但因为 `starts_with` 同样按字节，切片边界总落在 UTF-8 字符边界上，不会切出半个字符。
5. 本步为静态推理，待本地用程序验证（见第 5 节综合实践）。

#### 4.2.5 小练习与答案

**练习**：`Publication::topic()` 的注释为什么敢写「all processed frames always have topics」而用 `unwrap()`？点对点 `send` 出来的帧会进 `process` 吗？

**参考答案**：因为 `process()` 在最外层就有 `if let Some(topic) = frame.topic()` 守卫——**没有 topic 的帧直接走到末尾返回 `Ok(Some(frame))`，根本不会构造 `Publication`**。点对点 `send` 产生的帧是 `FrameKind::Message` 且 `topic == None`，它在 `processor` 里其实会被当作 RPC 帧解析；即便流到 `handle_frame`，也会被 `process` 的 topic 守卫挡下、原样回退给调用者。所以 `Publication` 内部可以安全 `unwrap`。

---

### 4.3 注册处理通道：register_topic 与 register_prefix

#### 4.3.1 概念说明

`TopicBroker` 内部是**两张 `BTreeMap`**：

- `topics: BTreeMap<String, (PublicationSender, usize)>` —— **精确主题**表，键是完整主题字符串，全等匹配。
- `prefixes: BTreeMap<String, (PublicationSender, usize)>` —— **主题前缀**表，键是前缀字符串，用 `starts_with` 匹配。

每个值是一个二元组 `(发送端 tx, handler_id)`。注册时有两个层次的选择：

1. **要不要自己建通道**：`register_topic` / `register_prefix` 会顺手用 `async_channel::bounded(channel_size)` 建一对 `(tx, rx)` 并返回给你（同时把 `tx` 存进表）；而 `register_topic_tx` / `register_prefix_tx` 则要求你**传入一个已存在的 `tx`**，适合「多个主题共用同一个通道」。
2. **要不要自定义 handler_id**：带 `_with_handler_id` 的版本可指定一个 `usize` 标签；不带则默认 `0`。

重复注册同一个主题/前缀会返回 `Error::busy("... already registered")`。

#### 4.3.2 核心流程

注册家族可以用一张表概括（以 topic 为例，prefix 完全对称）：

| 方法 | 是否自建通道 | handler_id | 典型用途 |
| --- | --- | --- | --- |
| `register_topic(topic, size)` | 是 | 默认 0 | 一个主题一个独立通道 |
| `register_topic_with_handler_id(topic, hid, size)` | 是 | 自定义 | 一个通道收多主题，靠 hid 区分 |
| `register_topic_tx(topic, tx)` | 否（传入 tx） | 默认 0 | 复用已有通道 |
| `register_topic_tx_with_handler_id(topic, hid, tx)` | 否（传入 tx） | 自定义 | 多主题共用通道且需区分 |

#### 4.3.3 源码精读

`TopicBroker` 的两表结构：

[src/tools/pubsub.rs:64-68](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/tools/pubsub.rs#L64-L68) — `prefixes` 与 `topics` 两张 `BTreeMap`，`#[derive(Default)]` 让 `new()` 直接拿到两张空表。

「自建通道 + 默认 id」的精确注册：

[src/tools/pubsub.rs:77-85](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/tools/pubsub.rs#L77-L85) — `register_topic`：建 `bounded(channel_size)`，把 `tx` 经 `register_topic_tx` 存表，返回 `(tx, rx)`。

「前缀 + 自定义 id + 复用通道」的注册，体现了全部两个旋钮：

[src/tools/pubsub.rs:164-176](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/tools/pubsub.rs#L164-L176) — `register_prefix_tx_with_handler_id`：用 `Entry::Vacant` 原子判重，命中空位才插入 `(tx, handler_id)`，否则报 busy。

冲突时报错用的就是 `Error::busy`：

[src/lib.rs:217-223](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L217-L223) — `Error::busy` 构造一个 `ErrorKind::Busy` 错误。

> 对照点（承接 u3-l3）：代理的 `SubMap` 用 `#`/`+` **通配符**做主题匹配；而 `TopicBroker` 的 `prefixes` 表用**纯字符串 `starts_with`**，**不支持任何通配符**。所以注册前缀 `"a/"` 能匹配 `a/foo`，但注册 `"a/*"` 只会匹配字面以 `a/*` 开头的主题（如 `a/*/x`）——这是个非常容易踩的坑，务必区分「代理侧通配订阅」与「本地前缀路由」。

#### 4.3.4 代码实践

最小调用示例（**示例代码**，非项目原有）：

```rust
// 示例代码：注册一个精确主题和一个前缀
use busrt::tools::pubsub::TopicBroker;

let mut tb = TopicBroker::new();
// 精确：只有主题恰好是 "health/ping" 才命中
let (tx_ping, rx_ping) = tb.register_topic("health/ping", 16)?;
// 前缀：所有以 "a/" 开头的主题都命中，子主题 = 去掉 "a/" 的部分
let (tx_a, rx_a) = tb.register_prefix("a/", 16)?;
// 重复注册会失败
assert!(tb.register_topic("health/ping", 16).is_err());
# Ok::<(), busrt::Error>(())
```

1. **实践目标**：验证「精确 vs 前缀」两种注册的匹配差异，以及重复注册报错。
2. **操作步骤**：把上面片段放进一个 `#[tokio::test]` 或 `main`，断言重复注册返回 `Err`。
3. **需要观察的现象**：第三次 `register_topic("health/ping", ..)` 返回 `Error`，其 `kind()` 为 `ErrorKind::Busy`。
4. **预期结果**：`is_err()` 为真。**待本地验证**（需启用 `ipc`/`rpc`/`broker` 任一 feature，否则 `tools::pubsub` 不编译）。

#### 4.3.5 小练习与答案

**练习**：如果你想让 `a/` 和 `b/` 两个前缀的消息**汇聚到同一个消费者**，但仍知道每条来自哪个前缀，该用哪两个注册方法？

**参考答案**：先 `let (tx, rx) = async_channel::bounded(size);` 建一对通道，再用 `register_prefix_tx_with_handler_id("a/", 1, tx.clone())` 和 `register_prefix_tx_with_handler_id("b/", 2, tx.clone())` 把**同一个 `tx`** 以**不同 handler_id** 注册两次。消费者在 `rx.recv()` 后读 `publication.handler_id()` 即可区分来源（1=`a/`、2=`b/`）。注意 `async_channel::Sender` 是可 `clone` 的（参考计数），这正是 `_tx` 系列方法存在的理由。

---

### 4.4 process：先精确、后前缀的匹配分发

#### 4.4.1 概念说明

`process` 是 `TopicBroker` 唯一的运行时方法，签名是：

```rust
pub async fn process(&self, frame: Frame) -> Result<Option<Frame>, Error>
```

它的语义是「**尝试分发；分不出去就把帧还给你**」：

1. 帧没有 topic（`frame.topic() == None`）→ 直接返回 `Ok(Some(frame))`。
2. 帧有 topic：
   - 先在 `topics` 表里**精确查找**；命中则把 `Publication{ subtopic_pos: 0, .. }` 发到对应通道，返回 `Ok(None)`。
   - 否则在 `prefixes` 表里**按 `BTreeMap` 顺序**遍历，找第一个 `topic.starts_with(pfx)` 的前缀；命中则发 `Publication{ subtopic_pos: pfx.len(), .. }`，返回 `Ok(None)`。
   - 都没命中 → 返回 `Ok(Some(frame))`，把帧原样交还调用者自行处理。

`Ok(None)` 表示「已被分发，无需再处理」；`Ok(Some(frame))` 表示「没人要，你自己看着办」——这是 `handle_frame` 里做兜底逻辑（比如记日志、丢弃）的钩子。

#### 4.4.2 核心流程

匹配优先级伪代码：

```
fn process(frame):
    topic = frame.topic()?            # 无 topic → 返回原帧
    if topic in topics:               # ① 精确优先
        send Publication{pos=0}; return None
    for (pfx, ch) in prefixes:        # ② 前缀次之，BTreeMap 字典序
        if topic.starts_with(pfx):
            send Publication{pos=len(pfx)}; return None
    return Some(frame)                # ③ 都不中 → 退还原帧
```

两个细节值得记住：

- **精确优先于前缀**：即使你同时注册了精确 `"a/foo"` 和前缀 `"a/"`，主题 `"a/foo"` 永远走精确通道（子主题为完整 `"a/foo"`），不会落到前缀通道。
- **前缀遍历顺序 = `BTreeMap` 字典序**：若多个前缀都能 `starts_with` 命中（如 `"a/"` 和 `"a/f/"` 对主题 `"a/foo"`），**字典序最小的那个胜出**（`"a/"` < `"a/f/"`，故 `"a/"` 先命中）。如果你的业务依赖「最长前缀优先」，需要自己额外处理，`TopicBroker` 不做最长匹配。

#### 4.4.3 源码精读

`process` 全文只有 25 行，但浓缩了上述全部规则：

[src/tools/pubsub.rs:179-203](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/tools/pubsub.rs#L179-L203) — 先 `topics.get(topic)` 精确查（[L181-L189](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/tools/pubsub.rs#L181-L189)），再 `for (pfx, ..) in &self.prefixes` 用 `topic.starts_with(pfx)` 前缀查（[L190-L200](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/tools/pubsub.rs#L190-L200)），都不中则 `Ok(Some(frame))`（[L202](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/tools/pubsub.rs#L202)）。

注意 `tx.send(...).await?` 的 `?`：当**消费者端全部被 drop**（通道关闭）时，`send` 返回错误并经 `?` 上抛为 `Err(Error)`。也就是说，如果你提前 drop 了某个主题的 `rx`，后续发往该主题的帧会让 `process` 直接报错——调用方应决定是忽略还是重连。

#### 4.4.4 代码实践

单元式推理实践：

1. **实践目标**：验证「精确优先 + 前缀字典序」两条规则。
2. **操作步骤**：注册 `register_topic("a/foo", 8)`、`register_prefix("a/", 8)`、`register_prefix("a/f/", 8)`，然后脑内分别对主题 `a/foo`、`a/food`、`a/f/x`、`a/bar` 调用 `process`，判断每条进哪个通道、`subtopic()` 是什么。
3. **需要观察的现象**：

| 入站主题 | 命中通道 | `subtopic()` |
| --- | --- | --- |
| `a/foo` | 精确 `a/foo` | `a/foo` |
| `a/food` | 前缀 `a/`（字典序最小，先于 `a/f/`） | `food` |
| `a/f/x` | 前缀 `a/`（同样先于 `a/f/`） | `f/x` |
| `a/bar` | 前缀 `a/` | `bar` |

4. **预期结果**：上表说明「只要 `a/` 存在，`a/f/` 几乎永远不会被命中」（除非主题以 `a/` 开头但……不，`a/` 总是先命中）。因此**长前缀会被短前缀遮蔽**——这是设计取舍，需要你规划前缀集合时避免重叠。**待本地验证**。
5. 进一步思考：若确实需要「最长前缀优先」，可改成只注册最短公共前缀，在消费者内再二次解析 `subtopic()`。

#### 4.4.5 小练习与答案

**练习 1**：`process` 返回 `Ok(Some(frame))` 有哪两种触发条件？

**参考答案**：① 帧本身没有 topic（`frame.topic() == None`）；② 帧有 topic，但既不在 `topics` 表、也不被任何 `prefixes` 表项 `starts_with` 命中。

**练习 2**：为什么 `process` 要把「未匹配的帧」还回来，而不是直接丢弃？

**参考答案**：为了给调用者一个**兜底钩子**。`TopicBroker` 是「尽力分发」的辅助类，它不知道你的业务对未覆盖主题有什么诉求——可能是记日志、转存、计数，也可能是再交给另一层路由器。返回 `Some(frame)` 让 `handle_frame` 可以 `if let Ok(Some(f)) = tb.process(f).await { … }` 自行决定，职责更清晰，也避免默默吞帧导致排障困难。

---

### 4.5 多处理器场景：subtopic 与 handler_id 的用途

#### 4.5.1 概念说明

`subtopic` 与 `handler_id` 是为「**一个通道、多种来源**」而生的两个正交信息：

- **`subtopic()`** 解决「前缀通道里，这条消息属于哪个具体子主题」。前缀 `a/` 的消费者可能收到 `a/foo`、`a/bar/baz`，靠 `subtopic()` 区分（分别得 `foo`、`bar/baz`）。
- **`handler_id()`** 解决「同一个 `rx` 收到的消息，来自哪条注册规则」。当你用 `_tx_with_handler_id` 把多个主题/前缀绑到**同一个 `tx`** 时，消费者需要一个非字符串的整数标签来快速分流（比如 match 一个枚举）。

两者结合，让你可以用**一个消费者任务**同时处理一族主题，而不必为每个主题各开一个任务。

#### 4.5.2 核心流程

典型多处理器拓扑：

```
TopicBroker
├─ register_prefix_tx_with_handler_id("a/", 1, tx.clone())  ─┐
├─ register_prefix_tx_with_handler_id("b/", 2, tx.clone())  ─┤── 同一个 rx
└─ register_topic_with_handler_id  ("health/ping", 3, ..)   ─┘
                              │
                 单一消费者：match publication.handler_id() { 1=>…, 2=>…, 3=>… }
```

#### 4.5.3 源码精读

`handler_id` 的存取：

[src/tools/pubsub.rs:54-56](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/tools/pubsub.rs#L54-L56) — `Publication::handler_id()` 返回注册时绑定的整数。

构造 `Publication` 时把表里存的 `handler_id` 透传进去：

[src/tools/pubsub.rs:181-189](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/tools/pubsub.rs#L181-L189) — 精确命中分支：`handler_id: *handler_id`（解引用表里的值）。

#### 4.5.4 代码实践

源码阅读型实践：

1. **实践目标**：确认 `handler_id` 是「注册时静态绑定、分发时原样透传」，不会动态变化。
2. **操作步骤**：对比 `register_topic`（[L77-L85](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/tools/pubsub.rs#L77-L85)，写入 `0`）与 `register_topic_with_handler_id`（[L90-L99](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/tools/pubsub.rs#L90-L99)，写入自定义值），再追踪到 `process` 里构造 `Publication` 的 `handler_id: *handler_id`。
3. **需要观察的现象**：`handler_id` 全程来自 `BTreeMap` 的 value，消费者拿到的就是注册时的那个数。
4. **预期结果**：你可以安全地用它做 `match` 分流的判别值。
5. 本步为静态阅读。

#### 4.5.5 小练习与答案

**练习**：在「长前缀被短前缀遮蔽」的情况下（4.4 节），`handler_id` 能否帮你区分原本想由 `a/f/` 处理的消息？

**参考答案**：不能直接区分。因为 `a/` 字典序在前，主题 `a/f/x` 会被 `a/` 通道吃掉，`a/f/` 通道根本收不到，`handler_id` 也无从发挥。正确做法是**不要注册会被遮蔽的短前缀**，或在 `a/` 的消费者里用 `subtopic()`（此处为 `"f/x"`）做二次解析——`handler_id` 只在「多规则共享一通道」时才有用，无法挽救「根本没投递到」的情况。

---

## 5. 综合实践

> **任务**：在一个 RPC 处理器里挂上 `TopicBroker`，为前缀 `a/`（即概念上的 `a/*`）和 `b/特殊` 各注册一个处理通道；从另一个客户端 publish 若干主题，让 `process()` 把帧分流到对应通道，分别打印 `subtopic`；并验证未被任何前缀覆盖的主题会落到兜底分支。

下面给出**完整示例代码**（非项目原有文件），改造自 `examples/client_rpc_handler.rs`。把它保存为 `examples/topic_broker_demo.rs` 即可作为 example 运行。

```rust
// 示例代码：TopicBroker 分流演示
// 运行前置：先启动一个监听 /tmp/busrt.sock 的独立代理（见 u1-l2 的 busrtd）。
use busrt::async_trait;
use busrt::ipc::{Client, Config};
use busrt::rpc::{Options, RpcClient, RpcHandlers};
use busrt::tools::pubsub::TopicBroker;
use busrt::{Frame, QoS};
use std::sync::Arc;
use std::time::Duration;
use tokio::time::sleep;

struct MyHandlers {
    broker: Arc<TopicBroker>,
}

#[async_trait]
impl RpcHandlers for MyHandlers {
    // 非 RPC 帧（broadcast/publish）走这里
    async fn handle_frame(&self, frame: Frame) {
        // process 返回 Ok(None)=已分发；Ok(Some(f))=无人认领
        match self.broker.process(frame).await {
            Ok(None) => {} // 已路由到某主题通道
            Ok(Some(unmatched)) => {
                // 兜底：未被 a/ 或 b/特殊 覆盖的主题
                eprintln!(
                    "[fallback] 未匹配主题: {:?} 来自 {}",
                    unmatched.topic(),
                    unmatched.sender()
                );
            }
            Err(e) => eprintln!("[error] process 失败: {e}"),
        }
    }
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let name = "test.topic.server";

    // 1) 注册两个前缀通道
    let mut tb = TopicBroker::new();
    let (_tx_a, rx_a) = tb.register_prefix("a/", 16)?;
    let (_tx_b, rx_b) = tb.register_prefix("b/特殊", 16)?;
    let tb = Arc::new(tb);

    // 2) 两个消费者任务：分别处理 a/ 与 b/特殊 的子主题
    let ha = tokio::spawn(async move {
        while let Some(p) = rx_a.recv().await.ok() {
            println!(
                "[a/] subtopic={:?} payload={:?}",
                p.subtopic(),
                std::str::from_utf8(p.payload()).unwrap_or("<bin>")
            );
        }
    });
    let hb = tokio::spawn(async move {
        while let Some(p) = rx_b.recv().await.ok() {
            println!(
                "[b/特殊] subtopic={:?} payload={:?}",
                p.subtopic(),
                std::str::from_utf8(p.payload()).unwrap_or("<bin>")
            );
        }
    });

    // 3) 建客户端、订阅所有主题、起 RPC（阻塞帧模式，保证按序投递）
    let config = Config::new("/tmp/busrt.sock", name);
    let mut client = Client::connect(&config).await?;
    client
        .subscribe("#", QoS::Processed)
        .await?
        .expect("no op")
        .await??;
    let rpc = RpcClient::create(
        client,
        MyHandlers { broker: tb },
        Options::new().blocking_frames(), // 关键：串行 handle_frame，保证每主题通道内有序
    );

    println!("等待主题发布到 {name} …");
    while rpc.is_connected() {
        sleep(Duration::from_secs(1)).await;
    }
    let _ = (ha, hb);
    Ok(())
}
```

**操作步骤**（生产端用 `busrt` CLI，命令格式见 [src/cli.rs:114-147](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/cli.rs#L114-L147)，即 `busrt <path> [-n name] publish <topic> [payload]`）：

```sh
# 1. 启动代理（另开终端）
./test.sh server            # 或：busrtd -B /tmp/busrt.sock

# 2. 启动上面的消费者 example
cargo run --example topic_broker_demo --features ipc,rpc

# 3. 在第三个终端发布不同主题
busrt /tmp/busrt.sock -n producer publish a/foo     hello-a-foo
busrt /tmp/busrt.sock -n producer publish a/bar/x   hello-a-bar
busrt /tmp/busrt.sock -n producer publish b/特殊     hello-b
busrt /tmp/busrt.sock -n producer publish b/特殊/x   hello-b-x
busrt /tmp/busrt.sock -n producer publish c/none    hello-c    # 走兜底
```

**需要观察的现象与预期结果**（**待本地验证**）：

| 发布的主题 | 预期落到的通道 | 预期打印的 `subtopic` |
| --- | --- | --- |
| `a/foo` | `a/` 消费者 | `foo` |
| `a/bar/x` | `a/` 消费者 | `bar/x` |
| `b/特殊` | `b/特殊` 消费者 | ``（空串） |
| `b/特殊/x` | `b/特殊` 消费者 | `/x` |
| `c/none` | 兜底 `[fallback]` | （topic=`c/none`） |

**纪律提醒**（承接 4.1.3 的警告）：本例的两个消费者**只做打印**，没有回调同一个 `RpcClient`，所以在 `blocking_frames` 模式下是安全的。若你把消费者改成「在收到消息后用同一个 `rpc` 发请求」，就会触发 `processor` 头部警告描述的死锁——需要时请为消费者**另开一个客户端**。

---

## 6. 本讲小结

- `TopicBroker`（`src/tools/pubsub.rs`）是一个**传输无关的本地路由器**，把「一个 `handle_frame` 入口」按主题拆成「多个 `async_channel` 隔离的处理通道」，让耗时业务离开收帧热路径。
- 它只应在 RPC 处理器的 `handle_frame` 里使用，因为 publish/broadcast 帧正是经 [processor 的 else 分支](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/rpc/async_client.rs#L254-L261)流入这里。
- 两张表：`topics`（精确全等）与 `prefixes`（`starts_with`）。注册家族分「自建通道 / 传入 tx」×「默认 id / 自定义 id」四个变体；重复注册返回 `Error::busy`。
- `process()` 的匹配规则是**先精确、后前缀（`BTreeMap` 字典序）**，未命中返回 `Ok(Some(frame))` 作兜底；它用的是**纯字符串前缀**，**不是**代理 `SubMap` 的 `#`/`+` 通配符。
- `Publication` 暴露 `subtopic()`（= `topic[subtopic_pos..]`）与 `handler_id()`，分别支撑「前缀通道区分子主题」和「多规则共用一通道」两种多处理器拓扑。
- 配合 `Options::blocking_frames()` 可保证每主题通道内严格有序；但消费者**不得同步回调同一 `RpcClient`**，否则触发 `processor` 警告中的死锁。

---

## 7. 下一步学习建议

- **横向对照 u7-l2（游标 cursors）**：游标用 UUID 隔离**有状态**的服务端数据流，`TopicBroker` 用主题隔离**无状态**的发布帧；两者都是构建在 RPC 之上的「应用层分流」工具，可对比它们的状态模型与生命周期管理。
- **继续本单元 u7-l4（同步客户端 sync 模块）**：`TopicBroker` 本身是异步的；如果你在不能使用 Tokio 的环境（如某些实时线程）里需要类似的主题分流，可以参考 `sync` 模块如何用 `rtsc` 通道与标准线程重组同样的模式。
- **运维侧 u8-l2（busrt CLI）**：综合实践里用到的 `busrt ... publish` 命令属于 CLI 工具集，下一阶段可在 u8-l2 系统学习 `listen`/`send`/`publish`/`rpc` 等子命令，把 CLI 当成调试 `TopicBroker` 路由效果的标准「产源」。
- **源码延伸阅读**：若你想做「最长前缀优先」或「通配前缀」，可在 `TopicBroker` 之外再包一层，或直接阅读代理侧 `SubMap`（`src/broker.rs`）的实现，理解它如何用分层结构实现 `+`/`#` 匹配，从而判断是否需要把那套机制搬到本地路由里。
