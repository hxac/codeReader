# 三种通信模式：send、broadcast 与 publish

## 1. 本讲目标

学完本讲后，你应当能够：

- 说出 BUS/RT 三种通信模式（点对点 `send` / 广播 `send_broadcast` / 发布订阅 `publish`）在代理内分别查哪张路由表、生成哪种 `FrameKind`。
- 读懂 `broker.rs` 中的 `send!` / `send_broadcast!` / `publish!` 三个核心宏，并解释它们的「查找 → 统计 → 扇出」三段式结构。
- 区分主题掩码（`+` / `#`，按 `/` 分层）与广播掩码（`?` / `*`，按 `.` 分层）的语法。
- 理解 `exclude` / `unexclude` 排除机制：为什么订阅了 `#` 之后还能「屏蔽」特定主题，以及 `has_exclusions` 这个原子布尔快路径的作用。
- 解释两个底层宏 `safe_send_frame!`（背压处理）与 `make_confirm_channel!`（QoS 确认）如何被三种模式共用。

本讲承接 u3-l2《BrokerDb：客户端注册表与订阅映射》。上一讲告诉你 `BrokerDb` 里有三张表（`clients` / `broadcasts` / `subscriptions`），本讲就回答最关键的一个问题：**这三张表分别被谁用、怎么用**——也就是三种通信模式的分发逻辑。

## 2. 前置知识

- **三张路由表**（来自 u3-l2）：`clients`（精确全名 → 客户端，点对点用）、`broadcasts`（`BroadcastMap`，对端名掩码 → 一组客户端，广播用）、`subscriptions`（`SubMap`，主题 → 订阅者，发布订阅用）。本讲就是把这三张表「点亮」。
- **`Frame = Arc<FrameData>`**（来自 u2-l1）：代理把一条消息扇出给多个订阅者时，并不复制字节缓冲，而是 `Arc::clone` 只增加引用计数。所以「广播给 N 个客户端」几乎不比「发给 1 个」贵多少。
- **`FrameKind` 与 `FrameOp`**（来自 u2-l1）：`FrameOp` 是「客户端请求代理执行的操作」（发送方视角），`FrameKind` 是「帧本身的类型标签」（接收方视角）。本讲三种模式分别对应 `FrameKind::Message` / `Broadcast` / `Publish`。
- **`QoS`**（来自 u2-l1）：低位 `needs_ack()` 对应 `Processed`（等代理回 ACK），高位 `is_realtime()` 对应立即刷新出站。本讲的 `make_confirm_channel!` 就是消费 `needs_ack()` 的地方。
- **有界通道与背压**（来自 u3-l1）：每个客户端有一个容量默认 8192 的入站有界通道 `tx`。队列满时如何处理，是 `safe_send_frame!` 的核心职责。
- **Rust 宏 `macro_rules!`**：本讲的 `send!` 等都是声明式宏，用 `$name: expr` 接收参数并在调用处展开。读它们时把 `$db` / `$client` 当成普通变量即可。

## 3. 本讲源码地图

本讲内容高度集中，主要在两个文件里：

| 文件 | 作用 |
| --- | --- |
| [src/broker.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs) | 三个分发宏、底层宏、`Client` 的 `AsyncClient` 实现、`handle_reader` 里对入站帧的路由全部在此 |
| [examples/inter_thread.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/inter_thread.rs) | 嵌入式 Broker 最小蓝本，演示 `send` 与 `send_broadcast`，综合实践以它为基础 |

补充参考（不属于本讲精读，但为完整性列出）：

- [src/client.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/client.rs) 定义了 `AsyncClient` trait 的方法签名（`send` / `send_broadcast` / `publish` / `publish_for` / `exclude` 等）。
- [src/lib.rs](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs) 提供 `FrameKind` 枚举与 `OpConfirm` 类型别名。

## 4. 核心概念与源码讲解

在拆分每个模式之前，先用一张总表建立直觉。三种模式的根本差异在于「代理用什么键去三张表里查收件人」：

| 模式 | 调用方法 | 查的路由表 | 查找键 | 生成 `FrameKind` | 是否带 `topic` | 对端名/主题通配符 |
| --- | --- | --- | --- | --- | --- | --- |
| 点对点 | `send(target, ...)` | `clients` | 精确全名（如 `worker.2`） | `Message` | 否（`None`） | 不用掩码 |
| 广播 | `send_broadcast(mask, ...)` | `broadcasts` | 对端名掩码（如 `worker.*`） | `Broadcast` | 否（`None`） | `.` 分层，`?`/`*` |
| 发布订阅 | `publish(topic, ...)` | `subscriptions` | 主题（如 `news/tech`） | `Publish` | 是（`Some`） | `/` 分层，`+`/`#` |

> 记住一句口诀：**广播看名字（`.`），订阅看主题（`/`）**。这条差异贯穿整个库，也决定了两种掩码语法的不同。

下面四个小节分别精讲三种模式（4.1～4.3）和它们共用的底层宏（4.4）。

### 4.1 点对点 send：精确名查找与 FrameKind::Message

#### 4.1.1 概念说明

点对点（point-to-point）是最朴素的通信方式：发送方指定一个**确定的收件人名字**，代理把消息只投递给这个客户端。如果这个名字不存在，`send` 直接返回 `Error::not_registered()`（而不是静默丢弃）。这种模式适合「请求某一项服务」「向某个具体 worker 下达指令」之类的场景。

它的语义是「一对一」，因此**不涉及任何掩码匹配**——它查的是 `clients` 这张 `HashMap<String, Arc<BusRtClient>>`，键就是客户端全名，一次哈希查找即得。

#### 4.1.2 核心流程

`send!` 宏展开后做的事情可以拆成三段：

1. **统计累加**：发送方与代理各记一笔「收」计数（`r_frames` / `r_bytes`）。
2. **查表**：在 `clients` 映射里按精确全名查找目标；命中则再记一笔代理与目标的「发」计数（`w_frames` / `w_bytes`），并克隆出目标的 `Arc<BusRtClient>`；未命中则直接返回 `not_registered`。
3. **构造帧并投递**：把载荷包成 `FrameData { kind: Message, sender: Some(...), topic: None, ... }`，交给 `safe_send_frame!`（见 4.4）真正塞进目标的入站通道。

伪代码：

```text
fn send(client, target, payload):
    client.stats.add_read(len); broker.stats.add_read(len)
    target_client = broker.clients.get(target)   // 精确匹配
    if target_client is None:
        return Err(NotRegistered)
    target_client.stats.add_write(len); broker.stats.add_write(len)
    frame = FrameData(kind=Message, sender=client.name, topic=None, ...)
    return safe_send_frame(target_client, frame)
```

#### 4.1.3 源码精读

点对点分发宏，核心是 `db.clients.lock().get(target)` 的一次精确查找：

[src/broker.rs:111-143](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L111-L143) —— `send!` 宏。注意 `topic: None`（点对点帧不带主题），以及未命中时返回 `Error::not_registered()`。

`Client::send` 方法只是把高层参数（`payload.to_vec()`、`qos.is_realtime()`）填进宏并随 `make_confirm_channel!(qos)` 返回确认：

[src/broker.rs:348-368](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L348-L368) —— 内部客户端的 `send` 实现。`payload.to_vec()` 把 `borrow::Cow` 收成完整块装入 `FrameData.buf`（线程内路径，见 u2-l2）；`self.get_timeout()` 对内部客户端恒为 `None`。

`examples/inter_thread.rs` 用最直白的方式演示了点对点——`worker.1` 每秒给 `worker.2` 发一句 `hello`：

[examples/inter_thread.rs:29-37](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/inter_thread.rs#L29-L37) —— 点对点 `send` 的最小用法。

#### 4.1.4 代码实践

这是一个「源码阅读型」实践，帮助你把 `send` 与查表对应起来。

1. **目标**：确认点对点 `send` 只查 `clients` 表，且未命中目标返回 `not_registered`。
2. **步骤**：
   - 打开 `src/broker.rs`，定位 4.1.3 引用的 `send!` 宏，确认其中查找语句是 `db.clients.lock().get($target)`，没有任何掩码匹配。
   - 再读 6.1 讲会详讲的 `handle_reader`（本讲先看路由分支），确认外部客户端发来的 `OP_MESSAGE` 最终也走到同一个 `send!` 宏：
     [src/broker.rs:2088-2118](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L2088-L2118)。
3. **需要观察的现象**：`FrameOp::Message` 分支里，若 `qos.needs_ack()` 且 `send!` 失败，会用 `send_ack!(e.kind as u8, ...)` 回送错误码；成功则回送 `RESPONSE_OK`。
4. **预期结果**：你能说出「无论内部客户端还是外部客户端，点对点发送走的是同一段 `send!` 宏，查的都是 `clients` 精确名表」。

#### 4.1.5 小练习与答案

**练习 1**：如果用 `send` 发给一个不存在的客户端名，调用方会拿到什么错误？代理会广播 announce 吗？

> **答案**：返回 `Error::not_registered()`（错误码见 u2-l1 的 `ErrorKind`）。`send!` 宏本身不做任何 announce，它只是在 `clients` 表里 `get` 失败后直接 `Err`。announce 只在客户端注册 / 注销时触发（见 u3-l2）。

**练习 2**：为什么点对点帧的 `topic` 字段是 `None`？

> **答案**：因为收件人是按精确名字确定的，不经过主题树匹配，帧里不需要携带主题供下游匹配；`topic` 只在 `publish`（发布订阅）里有意义（见 4.3）。

### 4.2 广播 send_broadcast：对端名掩码匹配与 Fan-out

#### 4.2.1 概念说明

广播（broadcast）是「按对端名掩码」找到一组客户端，把**同一条**消息投递给所有匹配者。与点对点相比，它把「精确名」换成了「掩码」，例如 `worker.*` 匹配所有以 `worker.` 开头的客户端。注意：广播匹配的是**客户端名字**，而不是主题——所以掩码语法用 `.` 分层（`?` 单层、`*` 多层），对应 `BroadcastMap`。

广播是「一对多」，但这里的「多」由名字掩码决定，与订阅无关。即使某个客户端一个主题都没订阅，只要它的名字匹配掩码，就会收到广播。

#### 4.2.2 核心流程

`send_broadcast!` 的三段结构与 `send!` 类似，关键区别在第二段：

1. **统计累加**：发送方与代理各记一笔「收」计数。
2. **掩码查表**：调用 `db.broadcasts.lock().get_clients_by_mask(target)`，返回一个 `Vec<Arc<BusRtClient>>`。若为空则什么都不做（注意：**广播不因无人接收而报错**，这是与点对点的重要差异）。
3. **扇出（fan-out）**：先把代理的「发」计数一次性加上 `len × N`（N = 匹配数），再构造一个 `Arc<FrameData>`，循环里对每个匹配客户端 `frame.clone()`（`Arc` 克隆，O(1)）后 `safe_send_frame!`。

代理统计这里有个值得注意的细节——一次广播的写字节数是载荷长度乘以匹配数：

\[ w\_bytes\_{broker} \mathrel{+}= len \times N \]

这反映了「逻辑上 broker 向 N 个客户端各写了 len 字节」，哪怕物理上只有一份 `Arc<FrameData>` 缓冲。

#### 4.2.3 源码精读

广播分发宏，核心查找是 `get_clients_by_mask`：

[src/broker.rs:145-176](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L145-L176) —— `send_broadcast!` 宏。注意 `kind: FrameKind::Broadcast`、`topic: None`，以及空 `subs` 时静默返回（无错误）。`frame.clone()` 克隆的是 `Arc`，不复制载荷。

`BroadcastMap` 的掩码语法在 `BrokerDb::default` 里被配置成「`.` 分层、`?` 单层、`*` 多层」：

[src/broker.rs:676-681](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L676-L681) —— 广播映射的 `separator('.')`、`match_any("?")`、`wildcard("*")` 配置。

`Client::send_broadcast` 与 `handle_reader` 的 `FrameOp::Broadcast` 分支：

[src/broker.rs:391-411](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L391-L411) —— `send_broadcast` 方法。

[src/broker.rs:2119-2145](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L2119-L2145) —— `handle_reader` 中外部客户端广播帧的路由（含 AAA 鉴权 `allow_broadcast_any` / `allow_broadcast_to`）。

`inter_thread.rs` 里 `worker.3` 用 `worker.*` 向所有 worker 广播：

[examples/inter_thread.rs:38-50](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/examples/inter_thread.rs#L38-L50) —— 广播掩码 `worker.*` 的最小用法。

#### 4.2.4 代码实践

「运行 + 观察」型实践。

1. **目标**：直观感受广播的「按名字掩码」语义。
2. **步骤**：
   - 运行 `cargo run --example inter_thread --features broker`（待本地验证：示例可能需要额外 feature，可先 `cargo run --example` 查看 cargo 给出的可用示例与所需 feature）。
   - 观察终端输出：`worker.2` 既会收到 `worker.1` 的点对点 `hello`（`sender` 为 `worker.1`），也会收到 `worker.3` 的广播（`sender` 为 `worker.3`）。
3. **需要观察的现象**：两条消息交替出现，分别来自不同 `sender`，但 `worker.2` 都能收到——因为它叫 `worker.2`，名字匹配 `worker.*`。
4. **预期结果**：你会在输出里同时看到 `worker.1: hello` 和 `worker.3: this is a broadcast message`。注意广播能送达，**并不依赖** `worker.2` 订阅任何主题。

#### 4.2.5 小练习与答案

**练习 1**：`send_broadcast("worker.?", ...)` 会匹配 `worker.1` 吗？会匹配 `worker.sub.deep` 吗？

> **答案**：会匹配 `worker.1`（`?` 匹配单层 `1`）；**不会**匹配 `worker.sub.deep`（`?` 只匹配一层，`sub.deep` 是两层）。若想匹配多层，要用 `worker.*`。

**练习 2**：如果没有任何客户端名字匹配掩码，`send_broadcast` 返回什么？

> **答案**：返回 `Ok(...)`（确认通道，见 4.4）——它**不会**报错。`send_broadcast!` 宏在 `subs.is_empty()` 时直接跳过扇出，方法体随后照常执行 `make_confirm_channel!(qos)` 返回成功。这与点对点未命中返回 `not_registered` 形成对照。

### 4.3 发布订阅 publish：主题树、排除机制与 publish_for

#### 4.3.1 概念说明

发布订阅（publish/subscribe）是「按主题」分发：发布者把消息发到一个**主题**（如 `news/tech`），代理把它投递给所有**订阅了匹配该主题的掩码**的客户端。这里的关键是「订阅」——客户端必须先 `subscribe("news/#")` 之类，才会进入 `subscriptions` 表。匹配走的是主题树，用 `/` 分层、`+` 单层、`#` 多层（与广播的 `.`/`?`/`*` 不同）。

发布订阅比广播多了两项能力：

1. **排除（exclude）**：一个订阅了 `#`（所有主题）的客户端，可以再 `exclude("news/tech")` 把某个主题「拉黑」——即使订阅匹配，该主题的消息也不会送达。本讲的综合实践就围绕它展开。
2. **定向发布 `publish_for(topic, receiver, ...)`**：在主题匹配的基础上，再把收件人限定为某个 `primary_name`（主客户端名），用于「只发给某组客户端中真正在工作的那一个」。

#### 4.3.2 核心流程

`publish!` 宏有两个变体。第一变体（普通 `publish`）：

1. **统计累加**：发送方与代理各记一笔「收」计数。
2. **主题查表 + 排除过滤**：`db.subscriptions.lock().get_subscribers(topic)` 拿到候选订阅者列表，然后用 `retain` 过滤掉命中排除规则的订阅者。
3. **扇出**：与广播相同——一次性累加 `len × N` 写计数，构造 `Arc<FrameData { kind: Publish, topic: Some(topic), ... }>`，循环 `frame.clone()` + `safe_send_frame!`。

排除过滤的判定逻辑是「快路径 + 慢路径」：

```text
retain(sub => !sub.has_exclusions || !sub.exclusions.matches(topic))
```

`has_exclusions` 是个 `AtomicBool`：只有当客户端确实设置过排除（为 `true`）时，才去锁 `exclusions` 这个 `AclMap` 做精确匹配。绝大多数没有排除的客户端走快路径，完全不触锁。

第二变体（`publish_for`）多了一个 `$receiver` 参数，`retain` 条件额外要求 `sub.primary_name == receiver`，把主题匹配再叠加一层「主名」筛选。

#### 4.3.3 源码精读

发布订阅分发宏（含两个变体）。注意第一变体里排除过滤的快/慢路径：

[src/broker.rs:178-212](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L178-L212) —— `publish!` 第一变体：`get_subscribers(topic)` → `retain` 排除 → 扇出。`kind: FrameKind::Publish`、`topic: Some($topic.to_owned())`。

[src/broker.rs:213-247](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L213-L247) —— `publish!` 第二变体（`publish_for` 用）：`retain` 里多一项 `sub.primary_name == $receiver`。

排除数据结构定义在 `BusRtClient` 上，是一个原子布尔（快路径标志）加一把锁保护的 `AclMap`（慢路径精确匹配）：

[src/broker.rs:536-537](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L536-L537) —— `has_exclusions: AtomicBool` 与 `exclusions: SyncMutex<AclMap>`。

[src/broker.rs:577-579](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L577-L579) —— `AclMap` 初始化为 `/` 分层、`+`/`#` 通配，与 `subscriptions` 主题树同构，因此排除规则可以用和订阅一样的主题掩码书写。

排除 / 取消排除的方法实现：

[src/broker.rs:304-321](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L304-L321) —— `exclude` 把 `has_exclusions` 置 `true` 并插入主题；`unexclude` 删除主题，当排除表为空时把 `has_exclusions` 重新置 `false`（恢复快路径）。

`publish` / `publish_for` 方法与 `handle_reader` 路由：

[src/broker.rs:412-455](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L412-L455) —— `publish` 与 `publish_for` 方法。

[src/broker.rs:2146-2205](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L2146-L2205) —— `FrameOp::PublishTopic` 与 `FrameOp::PublishTopicFor` 的路由。注意 `PublishTopicFor` 会从载荷里再 `splitn` 解出一个 `receiver` 字符串。

`SubMap` 的主题树配置（`/` 分层、`+`/`#` 通配）：

[src/broker.rs:682-684](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L682-L684) —— `subscriptions` 的 `separator('/')`、`match_any("+")`、`wildcard("#")`。

#### 4.3.4 代码实践

「源码阅读型」实践，先吃透排除逻辑再动手（动手实践放在第 5 节综合实践）。

1. **目标**：把 `subscribe`、`exclude`、`publish` 三者在源码里串成一条链。
2. **步骤**：
   - 读 [src/broker.rs:263-269](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L263-L269) 的 `subscribe`，确认它把客户端登记进 `subscriptions` 表。
   - 读 [src/broker.rs:304-310](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L304-L310) 的 `exclude`，确认它不碰 `subscriptions` 表，只往客户端自己的 `exclusions` 里加主题。
   - 回到 `publish!` 宏的 `retain`（[src/broker.rs:188-191](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L188-L191)），确认排除是在「已经订阅」的候选集上做二次过滤。
3. **需要观察的现象**：排除与订阅是两套独立的状态——`exclude` 不会让你「订阅」，`subscribe` 也不会让你「排除」。两者在 `publish!` 里才汇合。
4. **预期结果**：你能解释「一个客户端 `subscribe("#")` 后 `exclude("news/tech")`，发布 `news/tech` 时它在候选集里（订阅匹配 `#`），但被 `retain` 过滤掉，因此收不到」。这正是第 5 节综合实践要验证的现象。

#### 4.3.5 小练习与答案

**练习 1**：客户端订阅了 `news/+`，发布者发了 `news/tech`，会收到吗？发了 `news/tech/alerts` 呢？

> **答案**：`news/tech` 会收到（`+` 匹配单层 `tech`）；`news/tech/alerts` **不会**收到（`+` 只匹配一层，第三层 `alerts` 不匹配）。要匹配多层，订阅掩码应写成 `news/#`。

**练习 2**：`exclude("news/tech")` 之后，再 `publish("news/tech/alerts")`，被排除的客户端会收到吗？

> **答案**：会收到。因为 `exclusions` 的 `AclMap` 与订阅树同构，`exclude("news/tech")` 只精确屏蔽 `news/tech` 这一个主题；`news/tech/alerts` 不与之匹配，故不在排除之列。若想屏蔽整个子树，应 `exclude("news/#")`。

**练习 3**：`publish_for("news/tech", "worker", ...)` 与普通 `publish("news/tech", ...)` 的收件人集合有什么关系？

> **答案**：`publish_for` 的收件人集合是普通 `publish` 收件人集合的子集——它在主题匹配的基础上，再用 `retain` 限定 `sub.primary_name == "worker"`，即只把消息投给主名为 `worker` 的那一组客户端（通常是某组 worker 里实际在线的实例）。

### 4.4 通用底座：safe_send_frame! 与 make_confirm_channel!

#### 4.4.1 概念说明

三种模式各查各的表、各生成各自的 `FrameKind`，但**投递**和**确认**这两件事是共用的：

- `safe_send_frame!` 负责「把一个 `Arc<FrameData>` 塞进某个客户端的入站通道 `tx`」，并处理**队列满（背压）**这一唯一会出错的投递场景。
- `make_confirm_channel!` 负责按 `QoS` 决定返回值：`QoS::No` 返回 `Ok(None)`（无需确认）；`QoS::Processed` 返回一个 `oneshot` 接收端（确认通道）。

理解这两个宏，就理解了三种模式「最后一步」的共同行为，也理解了 u3-l1 提到的「内部客户端满队列阻塞发送方、外部 IPC 客户端满队列被强制断连」这条关键差异。

#### 4.4.2 核心流程

`safe_send_frame!` 的判定树：

```text
if 目标队列满:
    if 目标是 Internal 客户端:
        阻塞等待（若设置 timeout 则限时，否则无限等）→ 把背压传回发送方
    else:  # 外部 IPC 客户端
        注销该客户端、关闭通道、返回 not_delivered
else:
    正常 send
```

`make_confirm_channel!` 的逻辑：

```text
if qos.needs_ack():   # QoS::Processed
    建一个 oneshot 通道，立刻 tx.send(Ok(()))
    返回 Ok(Some(rx))   # 已兑现的确认
else:                 # QoS::No
    返回 Ok(None)
```

> 对内部客户端，确认是「立刻兑现」的——因为它没有线上 ACK 往返（见 u3-l1，内部客户端 `is_connected` 恒真、`ping` 空操作）。真正意义上的「等待代理 ACK」只发生在外部 IPC 客户端的 `QoS::Processed` 路径上（见 u4）。

#### 4.4.3 源码精读

背压处理宏——三种模式扇出时每送一帧都要过它：

[src/broker.rs:83-109](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L83-L109) —— `safe_send_frame!`。`Internal` 走 `time::timeout(...).await` 或裸 `.await` 阻塞；非 `Internal` 调 `db.unregister_client` + `tx.close()` 并返回 `Error::not_delivered()`。

确认通道宏——三种模式方法体的最后一行几乎都是它：

[src/broker.rs:71-81](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L71-L81) —— `make_confirm_channel!`。

`OpConfirm` 的类型定义（`Option<oneshot::Receiver<Result<(), Error>>>`）：

[src/lib.rs:70-72](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L70-L72) —— `OpConfirm` 与 `Frame`、`EventChannel` 三个核心别名。

三种模式的 `FrameKind` 枚举：

[src/lib.rs:387-403](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/lib.rs#L387-L403) —— `FrameKind::Message` / `Broadcast` / `Publish` 分别复用 `OP_MESSAGE` / `OP_BROADCAST` / `OP_PUBLISH` 字节。

#### 4.4.4 代码实践

「推理 + 改参数观察」型实践。

1. **目标**：理解 `QoS` 如何影响返回值，以及背压如何把两种客户端区分对待。
2. **步骤**：
   - 在 `examples/inter_thread.rs` 基础上，把 `client1.send(...)` 的 `QoS::No` 改成 `QoS::Processed`，重新编译运行。
3. **需要观察的现象**：
   - 用 `QoS::No` 时，`send` 返回 `Ok(None)`，方法体内的 `make_confirm_channel!(qos)` 走 `else` 分支。
   - 用 `QoS::Processed` 时，返回 `Ok(Some(rx))`，且这个 `rx` 已经被预先 `send(Ok(()))` 兑现——`rx.await` 立刻得到 `Ok(())`，不会有任何等待。
4. **预期结果**：你确认了对内部客户端，「Processed 确认」并不带来额外的网络往返开销，它只是把一个已结算的 oneshot 返回给你。真正的 ACK 往返是外部 IPC 客户端的行为（后续 u4 详讲）。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `safe_send_frame!` 对内部客户端「阻塞发送方」，对外部客户端却「直接踢掉」？

> **答案**：内部客户端与代理同处一个进程，发送方就是同一个 Tokio 运行时里的任务，阻塞它等队列腾空是安全的、甚至是期望的（天然背压）。外部 IPC 客户端在另一端进程，阻塞代理任务去等一个可能已经卡死的远端没有意义，反而会拖垮整个代理；所以策略是「宁可断开这个慢客户端，也要保护代理与其他客户端」。

**练习 2**：`make_confirm_channel!` 在 `needs_ack()` 为真时为什么先 `tx.send(Ok(()))` 再返回 `rx`？这样 `rx` 还有用吗？

> **答案**：先 `send` 是为了让确认「立即兑现」——调用方拿到 `rx` 后 `await` 会立刻得到 `Ok(())`，表达「代理已接收」的语义。对内部客户端而言确认即到此为止；`rx` 的价值在于提供与外部客户端统一的 `OpConfirm` 接口形态，让上层代码（如 RPC 层）可以用同一套等待逻辑处理两种客户端。

## 5. 综合实践

把第 4 节的三个模式串起来：用发布订阅 + 排除机制做一个可观察的小实验。本实践以 `examples/inter_thread.rs` 为蓝本改写。

### 实践目标

验证排除机制：一个订阅了 `#`（全部主题）的客户端，在 `exclude("news/tech")` 之后，应**收不到** `news/tech` 的消息，但仍能收到其他主题（如 `news/sports`）的消息。

### 示例代码（基于 inter_thread.rs 改写，示例代码）

```rust
// Cargo.toml 需启用 broker feature，例如：
//   cargo run --release --example u3l3_exclude --features broker
use busrt::broker::Broker;
use busrt::client::AsyncClient;
use busrt::QoS;
use tokio::time::sleep;
use std::time::Duration;

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let mut broker = Broker::new();
    // 订阅者：worker.sub
    let mut sub = broker.register_client("worker.sub").await?;
    // 发布者：worker.pub
    let mut pub_c = broker.register_client("worker.pub").await?;

    let rx = sub.take_event_channel().unwrap();

    // 1. 订阅全部主题
    sub.subscribe("#", QoS::No).await?;
    // 2. 排除 news/tech
    sub.exclude("news/tech", QoS::No).await?;

    // 发布两条消息：一条被排除，一条不被排除
    pub_c.publish("news/tech", b"you should NOT see this".into(), QoS::No).await?;
    pub_c.publish("news/sports", b"you SHOULD see this".into(), QoS::No).await?;

    // 让消费循环跑一会儿
    let handle = tokio::spawn(async move {
        let mut got_tech = false;
        while let Ok(frame) = rx.recv().await {
            let topic = frame.topic().unwrap_or("");
            println!("received topic={} payload={:?}",
                topic,
                std::str::from_utf8(frame.payload()).unwrap_or("?"));
            if topic == "news/tech" { got_tech = true; }
        }
        got_tech
    });

    sleep(Duration::from_millis(200)).await;
    // 结束时打印结论
    // （实际运行时：应只看到 news/sports 那一条，got_tech 为 false）
    Ok(())
}
```

> 说明：上面是教学示例代码，未带超时收尾与断言；正式跑时建议给 `rx.recv()` 加 `tokio::time::timeout`，并在收到 `news/sports` 后主动结束，便于稳定观察。`frame.topic()` 返回 `Option<&str>`，`publish` 帧会带主题，便于区分。

### 操作步骤

1. 在 `examples/` 目录新建文件（如 `u3l3_exclude.rs`），贴入上面的示例代码。
2. 用 `cargo run --release --example u3l3_exclude --features broker` 运行（待本地验证：确切的 feature 组合以本机 `Cargo.toml` 的 `[features]` 为准）。
3. 观察打印的每一行 `received topic=...`。

### 需要观察的现象

- 应该只看到 `received topic=news/sports payload="you SHOULD see this"`。
- **不应该**看到 `topic=news/tech` 的那一行。

### 预期结果

排除生效：尽管 `worker.sub` 订阅了 `#`（`news/tech` 本来匹配），但因为 `exclude("news/tech")` 让它在 `publish!` 宏的 `retain` 步骤里被过滤掉（见 [src/broker.rs:188-191](https://github.com/alttch/busrt/blob/0e6fac83894a7798a8a042a449298e3fb4b9d635/src/broker.rs#L188-L191)），所以 `news/tech` 不送达，而 `news/sports` 仍正常送达。

### 进阶（可选）

- 把 `sub.exclude("news/tech", ...)` 改成 `sub.exclude("news/#", ...)`，重新运行，观察这次 `news/sports` 是否也被屏蔽（预期：被屏蔽，因为 `news/#` 覆盖整个子树）。
- 把 `exclude` 换成 `unexclude("news/tech")`（先 exclude 再 unexclude），观察 `news/tech` 是否重新可达。

## 6. 本讲小结

- 三种通信模式分别查三张表：点对点 `send` 查 `clients`（精确全名）、广播 `send_broadcast` 查 `broadcasts`（`.`/`?`/`*` 掩码）、发布订阅 `publish` 查 `subscriptions`（`/`/`+`/`#` 主题树）。
- 三种模式分别生成 `FrameKind::Message` / `Broadcast` / `Publish`；只有 `publish` 帧会带 `topic` 字段。
- 点对点未命中目标返回 `not_registered`；广播无人匹配则**静默成功**；发布订阅在此基础上多了 `exclude` 排除与 `publish_for` 定向发布。
- 排除机制用「`has_exclusions` 原子布尔快路径 + `exclusions` AclMap 慢路径」实现：只有声明过排除的客户端才在 `publish!` 的 `retain` 里被二次过滤。
- `safe_send_frame!` 把唯一的背压场景按客户端类型区别处理：内部客户端阻塞发送方，外部 IPC 客户端强制注销，从而保护代理不被慢消费者拖垮。
- `make_confirm_channel!` 按 `QoS.needs_ack()` 决定返回 `None` 还是已兑现的 `oneshot` 确认通道——对内部客户端而言确认是立即的、零往返的。

## 7. 下一步学习建议

到这里，你已经把「代理内部如何分发消息」这条主链路读完了。接下来有两个方向：

- **向下（传输层）**：本讲的 `send!` 等宏只负责把帧塞进客户端的入站通道 `tx`。如果是**外部 IPC 客户端**，这帧最终还要经 `handle_writer` 序列化成线上字节（见 u2-l3 的线协议）再通过 socket 送出。这一段由 u4《IPC 客户端》讲解。
- **向上（RPC 层）**：三种模式只搬运「裸字节载荷」。若想让收发双方有「方法调用 / 返回值」的约定，就要在 `send`/`publish` 之上叠一层 RPC（msgpack 编码的请求 / 回复 / 通知），这正是 u5《RPC 层》的主题。
- **横切（连接生命周期与 AAA）**：外部客户端发来的 `OP_MESSAGE` / `OP_BROADCAST` / `OP_PUBLISH` 是如何在 `handle_reader` 里被解析并走到这三个宏的、以及 AAA 如何在分发前鉴权（`allow_p2p_*` / `allow_broadcast_*` / `allow_publish_*`），见 u6《Broker 内部：连接生命周期、多传输与 AAA》。

建议优先读 u4，把「内部客户端」与「外部 IPC 客户端」两条投递路径的差异补齐，形成一个完整的消息流转图。
