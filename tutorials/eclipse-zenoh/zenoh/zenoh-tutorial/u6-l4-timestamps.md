# Timestamp：时间戳与 HLC

## 1. 本讲目标

学完本讲，你应当能够：

- 说清 Zenoh 里 `Timestamp` 的内部结构——它为什么不只是「一个时间值」，而是「时间值 + 时钟标识」两部分。
- 会用 `session.new_timestamp()` 生成时间戳，并用 `Publisher::put(...).timestamp(ts)` 把它附带在发布的 `Sample` 上；理解「不设置时，时间戳到底有没有」。
- 理解 Zenoh 的混合逻辑时钟（HLC，由外部 `uhlc` crate 提供）为什么能保证跨节点的时间戳单调有序，以及它「吸收」对端时间戳、拒绝「未来时间戳」的行为。
- 知道时间戳在路由层与查询合并（Latest 合并）里的去重作用。

本讲承接《u3-l1 Pub/Sub 基础》和《u6-l3 Matching》，是支撑特性单元的收尾篇。它把 `Sample` 里的 `timestamp` 字段彻底讲透，并为后续《u8 路由》和《u11 存储后端》埋下「按时间戳合并/去重」的伏笔。

## 2. 前置知识

在进入源码前，先用三段话建立直觉。

**为什么需要时间戳？** Pub/Sub 是「推」模型，消息可能经多条路径、多个路由器转发，到达顺序不保证。如果两个 publisher 都对同一个 key 写过值，订阅端或存储端要判断「哪个更新」，就不能靠到达先后，而要靠一个双方都认可的、单调递增的逻辑时间——这就是 `Timestamp`。

**为什么不用系统墙钟（wall clock）？** 不同机器的物理时钟会有偏差（clock skew），甚至可能回退。直接比较两台机器的 `SystemTime::now()` 会得到错误的「谁更新」结论。Zenoh 采用 **HLC（Hybrid Logical Clock，混合逻辑时钟）**：它把物理时间与一个逻辑计数器结合，既贴近真实时间，又能保证「happens-before」因果关系——只要 A 在因果关系上先于 B，A 的时间戳一定小于 B。

**时间戳由谁来盖？** 每条 `Sample` 可带一个可选时间戳。它由生成该 Sample 的节点用本节点的 HLC 盖章。`Timestamp` 里除了时间值，还带一个「是谁盖的章」的时钟标识（用本节点的 `ZenohId`），这样两个不同节点即使在同一物理时刻盖章，时间戳也不会冲突。

> 关键术语快查：
> - **HLC**：混合逻辑时钟，外部 crate `uhlc` 实现，保证单调与因果。
> - **Timestamp**：Zenoh 的时间戳类型 = 时间值（`NTP64`）+ 时钟标识（`TimestampId`）。
> - **ZenohId**：节点唯一标识，被用作 HLC 的时钟标识。

## 3. 本讲源码地图

本讲涉及的关键文件及其作用：

| 文件 | 作用 |
| --- | --- |
| [commons/zenoh-protocol/src/core/mod.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/mod.rs) | 协议层基础类型，从 `uhlc` re-export `Timestamp`/`NTP64`，定义 `TimestampId` |
| [zenoh/src/lib.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs) | 公开 API 门面，`pub mod time` 模块与时间戳模块级文档 |
| [zenoh/src/api/sample.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/sample.rs) | `Sample` 结构，`timestamp` 字段与 `timestamp()` 访问器 |
| [zenoh/src/api/session.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs) | `Session::new_timestamp()` 与 `resolve_put` 中自动附带时间戳 |
| [zenoh/src/api/builders/publisher.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/publisher.rs) | `PublicationBuilder.timestamp(...)` builder 方法 |
| [zenoh/src/net/runtime/mod.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/mod.rs) | Runtime 持有可选的 HLC，按配置条件创建 |
| [commons/zenoh-config/src/defaults.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-config/src/defaults.rs) | `timestamping` 默认值（router/peer/client 是否启用） |
| [zenoh/src/net/routing/dispatcher/pubsub.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/pubsub.rs) | 路由层 `treat_timestamp!` 宏：吸收/拒绝/补盖时间戳 |

---

## 4. 核心概念与源码讲解

### 4.1 Timestamp：时间值 + 时钟标识

#### 4.1.1 概念说明

`Timestamp` 在 Zenoh 里不是一个单纯的整数时间，而是两部分复合体：

1. **时间值（time value）**：用一个 NTP 64 位表示（`NTP64`）记录「什么时候」。
2. **时钟标识（clock id）**：记录「是谁盖的章」，类型是 `TimestampId`。

为什么必须带时钟标识？因为 HLC 在「同一节点内」是严格单调的，但两个不同节点可能算出相同的时间值（例如同一物理时刻、逻辑计数器都为 0）。带上时钟标识后，即便时间值相同，两个时间戳也能区分，从而可定义全序（total order）。Zenoh 直接用本节点的 `ZenohId` 作为时钟标识，所以时间戳天然带有「来源节点」信息。

#### 4.1.2 核心流程

一个 `Timestamp` 的语义可以写成：

\[
\text{Timestamp} = (\text{时间值 } t,\ \text{逻辑计数 } c,\ \text{时钟标识 } id)
\]

其中 `(t, c)` 来自 HLC（见 4.3），`id` 来自节点 `ZenohId`。两个时间戳的比较规则是先比 `(t, c)`，相同再比 `id`，因此 `Timestamp` 实现了全序，这正是查询合并能做「保留最新」的前提。

它在 `Sample` 中是**可选**的：

```text
Sample { ..., timestamp: Option<Timestamp>, ... }
```

订阅端用 `sample.timestamp()` 拿到 `Option<&Timestamp>`——可能为 `None`（见 4.2 关于何时为 `None` 的讨论）。

#### 4.1.3 源码精读

`Timestamp` 与 `NTP64` 实际上来自外部 crate `uhlc`，由协议基础层 re-export 出来：

```rust
// commons/zenoh-protocol/src/core/mod.rs
pub use uhlc::{Timestamp, NTP64};

/// The unique Id of the HLC that generated the concerned Timestamp.
pub type TimestampId = uhlc::ID;
```

这段把 `uhlc::Timestamp` 直接暴露为 `zenoh_protocol::core::Timestamp`，并定义别名 `TimestampId = uhlc::ID`。注意 `ZenohIdProto` 内部也是基于 `uhlc::ID` 构造的（`pub struct ZenohIdProto(uhlc::ID)`），所以「节点 id ↔ 时钟 id」是同一套表示。详见 [commons/zenoh-protocol/src/core/mod.rs:29-34](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/mod.rs#L29-L34)。

公开 API 把这三个类型收拢进 `time` 模块：

```rust
// zenoh/src/lib.rs
pub mod time {
    pub use zenoh_protocol::core::{Timestamp, TimestampId, NTP64};
}
```

同时模块级文档明确说明了「时间戳 = 时间值 + 时钟标识」的结构，以及「每个 Session 有自己的时钟」。详见 [zenoh/src/lib.rs:945-985](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L945-L985)。

`Sample` 里 `timestamp` 字段是私有的，只能通过访问器读取：

```rust
// zenoh/src/api/sample.rs
pub struct Sample {
    ...
    pub(crate) timestamp: Option<Timestamp>,
    ...
}

impl Sample {
    /// Gets the timestamp of this Sample.
    pub fn timestamp(&self) -> Option<&Timestamp> {
        self.timestamp.as_ref()
    }
}
```

字段定义见 [zenoh/src/api/sample.rs:246](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/sample.rs#L246)，访问器见 [zenoh/src/api/sample.rs:286-290](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/sample.rs#L286-L290)。同样的可选字段也出现在解构用的 `SampleFields` 里（[sample.rs:204](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/sample.rs#L204)）。

#### 4.1.4 代码实践

**实践目标**：确认 `Sample::timestamp()` 的返回类型与「可能为 None」这一事实。

**操作步骤（源码阅读型）**：

1. 打开 [zenoh/src/lib.rs:945-985](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L945-L985)，阅读 `time` 模块文档中的两个代码示例（发送带时间戳的值 / 接收带时间戳的值）。
2. 注意接收示例里写的是 `if let Some(timestamp) = sample.timestamp()`，说明调用方必须处理 `None` 分支。

**需要观察的现象**：文档示例本身就用 `Option` 模式匹配，佐证了「时间戳可空」。

**预期结果**：你能复述「`timestamp()` 返回 `Option<&Timestamp>`，订阅端必须判空」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `Timestamp` 除了时间值还要带时钟标识？只用时间值不行吗？

> **参考答案**：不同节点的 HLC 是各自独立的，两个节点可能在同一物理时刻、逻辑计数器都为 0，产生相同的时间值 `(t, 0)`。带时钟标识后，两个时间戳仍可区分并定义全序，避免「相同时间值」导致的歧义。Zenoh 用 `ZenohId` 作时钟标识，附带还携带了来源信息。

**练习 2**：`Sample::timestamp()` 为什么返回 `Option<&Timestamp>` 而不是 `&Timestamp`？

> **参考答案**：因为时间戳是可选的——当发布端既未显式设置、本节点也没有启用 HLC 时，`Sample` 不会有时间戳（见 4.2）。用 `Option` 在类型层面表达「可能没有」，强迫调用方处理空值。

---

### 4.2 new_timestamp 与发布时附带时间戳

#### 4.2.1 概念说明

光知道 `Timestamp` 的结构还不够，关键问题是「时间戳从哪来、怎么挂上去」。Zenoh 给出两条路径：

1. **主动生成**：调用 `session.new_timestamp()` 拿到一个新的 `Timestamp`，再用 `Publisher::put(payload).timestamp(ts)` 附带上去。
2. **自动附带**：如果你什么都不做，`put` 在底层会尝试用 Runtime 的 HLC 自动盖一个时间戳——但**前提是 Runtime 真的拥有 HLC**。

第二点是初学者最容易踩坑的地方：**「不设置时间戳」并不等于「一定没有时间戳」**，它取决于配置里 `timestamping` 是否启用，而启用与否又和节点角色（router/peer/client）有关。

#### 4.2.2 核心流程

发布一条带时间戳的 `Put`，流程如下：

```text
session.new_timestamp()            # ① 用本节点 HLC 盖章（无 HLC 则回退墙钟+zid）
  └─> Timestamp
publisher.put(payload)
        .timestamp(ts)             # ② builder 把 ts 存进 PublicationBuilder.timestamp
        .await                     # ③ resolve_put 真正发出 Put 消息
  └─> Put { timestamp, ... }       # 时间戳随协议消息送达对端
```

而 `resolve_put` 内部决定「最终带不带时间戳」的关键一行是：

```text
let timestamp = timestamp.or_else(|| self.0.runtime.new_timestamp());
```

含义：优先用用户显式设置的；用户没设，就问 Runtime 要一个；Runtime 没有 HLC 时返回 `None`，于是这条消息就不带时间戳。

`runtime.new_timestamp()` 返回 `Option`，因为 Runtime 的 HLC 本身是可选的——见 4.3。

#### 4.2.3 源码精读

`Session::new_timestamp()` 始终返回一个 `Timestamp`（不是 `Option`），它在没有 HLC 时会回退到「系统墙钟 + 本节点 zid」：

```rust
// zenoh/src/api/session.rs
pub fn new_timestamp(&self) -> Timestamp {
    match self.0.runtime.hlc() {
        Some(hlc) => hlc.new_timestamp(),
        None => {
            // runtime 没有初始化 HLC 时：用系统时间 + zid 构造
            let now = SystemTime::now().duration_since(UNIX_EPOCH).unwrap().into();
            Timestamp::new(now, self.zid().into())
        }
    }
}
```

注意这个回退路径用的是 `SystemTime::now()`（普通墙钟），**不具备 HLC 的单调/因果保证**——它只是保证你能拿到「一个」时间戳。源码见 [zenoh/src/api/session.rs:1024-1034](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L1024-L1034)。

`PublicationBuilder.timestamp(...)` 则是 builder 上的设置方法，接受 `Into<Option<uhlc::Timestamp>>`，因此可以传时间戳，也可以传 `None` 清除：

```rust
// zenoh/src/api/builders/publisher.rs
impl<P, T> TimestampBuilderTrait for PublicationBuilder<P, T> {
    /// Sets an optional timestamp to be sent along with the publication.
    fn timestamp<TS: Into<Option<uhlc::Timestamp>>>(self, timestamp: TS) -> Self {
        Self { timestamp: timestamp.into(), ..self }
    }
}
```

字段定义见 [builders/publisher.rs:99-106](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/publisher.rs#L99-L106)，setter 见 [builders/publisher.rs:214-221](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/publisher.rs#L214-L221)。

真正决定「带不带时间戳」的地方在 `resolve_put`：

```rust
// zenoh/src/api/session.rs
let timestamp = timestamp.or_else(|| self.0.runtime.new_timestamp());
...
PushBody::Put(Put {
    timestamp,
    encoding: encoding.into(),
    ...
})
```

`self.0.runtime.new_timestamp()` 返回 `Option<uhlc::Timestamp>`（Runtime 没有 HLC 时为 `None`）。所以**默认的 peer/client 会话，若不显式设置时间戳，发布出去的 `Put` 就没有时间戳**。源码见 [zenoh/src/api/session.rs:2483-2504](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L2483-L2504)。

为什么 peer/client 默认拿不到时间戳？因为 Runtime 只在「配置启用 timestamping」时才创建 HLC，而默认值是 `router=true / peer=false / client=false`：

```rust
// commons/zenoh-config/src/defaults.rs
pub mod timestamping {
    pub mod enabled {
        pub const router: &bool = &true;
        pub const peer: &bool = &false;
        pub const client: &bool = &false;
        mode_accessor!(bool);
    }
    pub const drop_future_timestamp: bool = false;
}
```

详见 [commons/zenoh-config/src/defaults.rs:139-147](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-config/src/defaults.rs#L139-L147)。`Config::default()` 的 `mode` 是 `peer`，因此默认会话**没有 HLC**。

#### 4.2.4 代码实践

**实践目标**：亲手验证「设置时间戳 → 订阅端能读到；不设置 → 默认 peer 会话下读到 `None`」。

**操作步骤**：本仓库没有专用时间戳示例，下面是参照 `z_pub`/`z_sub` 写的最小示例（**示例代码**，需放在 `examples` 之外或自行组织）。

发布端（带时间戳）：

```rust
// 示例代码：ts_pub.rs
#[tokio::main]
async fn main() {
    let session = zenoh::open(zenoh::Config::default()).await.unwrap();
    let publisher = session.declare_publisher("demo/ts").await.unwrap();

    // ① 用本会话时钟生成时间戳（peer 默认无 HLC，回退到墙钟+zid）
    for _ in 0..5 {
        let ts = session.new_timestamp();
        publisher
            .put("hello")
            .timestamp(ts)          // ② 显式附带
            .await
            .unwrap();
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    }
    // 再发一条【不带】时间戳的，用于对比
    publisher.put("no-ts").await.unwrap();
    tokio::time::sleep(std::time::Duration::from_secs(1)).await;
}
```

订阅端：

```rust
// 示例代码：ts_sub.rs
#[tokio::main]
async fn main() {
    let session = zenoh::open(zenoh::Config::default()).await.unwrap();
    let subscriber = session.declare_subscriber("demo/ts").await.unwrap();
    while let Ok(sample) = subscriber.recv_async().await {
        match sample.timestamp() {
            Some(ts) => println!(
                "[有 ts] key={} value={} ts={}",
                sample.key_expr(),
                sample.payload().try_to_string().unwrap(),
                ts.to_string_rfc3339_lossy(),
            ),
            None => println!(
                "[无 ts] key={} value={}",
                sample.key_expr(),
                sample.payload().try_to_string().unwrap(),
            ),
        }
    }
}
```

**需要观察的现象**：前 5 条带时间戳的样本，订阅端打印 `[有 ts]` 且时间戳递增；最后一条 `no-ts` 打印 `[无 ts]`。

**预期结果**：在「两个 peer 直连、中间没有 router」的情况下，未显式设置时间戳的消息在订阅端 `timestamp()` 为 `None`。

> ⚠️ **重要前提（待本地验证）**：若订阅端与发布端之间经过了一个 **router**（router 默认 `timestamping.enabled=true`），router 的路由层会给无时间戳的消息**补盖**一个时间戳（见 4.4），于是 `no-ts` 在订阅端可能反而**有**时间戳。要稳定观察到 `None`，请让两端以 peer 模式直连（或都连到 peer/客户端网关而非 router）。这一点正好印证了 4.4 的「补盖」机制。

#### 4.2.5 小练习与答案

**练习 1**：`Session::new_timestamp()` 和 `runtime.new_timestamp()` 的返回类型有何不同？为什么？

> **参考答案**：`Session::new_timestamp()` 返回 `Timestamp`（一定有值，无 HLC 时回退到墙钟+zid）；`RuntimeState::new_timestamp()` 返回 `Option<uhlc::Timestamp>`（没有 HLC 时为 `None`）。前者是面向用户的便利 API，保证总能拿到「一个」时间戳；后者如实反映「本 Runtime 到底有没有 HLC」，用于 `resolve_put` 决定是否自动附带。

**练习 2**：用默认 `Config::default()` 打开的会话，连续两次 `session.new_timestamp()` 拿到的时间戳，第二次一定大于第一次吗？

> **参考答案**：**不一定**。默认会话是 peer 模式、没有 HLC，`new_timestamp()` 走的是墙钟回退分支（`SystemTime::now()`）。墙钟可能因系统调整而回退，不保证单调。只有启用了 HLC（如 router 默认启用）时，`new_timestamp()` 才保证单调递增。

---

### 4.3 HLC（uhlc）：混合逻辑时钟

#### 4.3.1 概念说明

`Timestamp` 里的 `(t, c)` 来自 **HLC（Hybrid Logical Clock）**。Zenoh 不自己实现它，而是依赖外部 crate [`uhlc`](https://docs.rs/uhlc/)（ultra-fast HLC）。每个 Session 的 Runtime 内部持有一个**可选的** HLC 实例，作为本节点的「时钟」。

HLC 的核心思想：维护两个量——本地观察到的物理时间 \( p \) 和逻辑计数器 \( l \)。它在两个时刻更新：

- **本地盖章**（`new_timestamp`）：要产生一个新的时间戳时。
- **吸收对端时间戳**（`update_with_timestamp`）：收到带时间戳的消息时，让自己的时钟「追上」对端，保证之后的本地时间戳不会小于刚看到的时间戳（维持因果序）。

经典 HLC 的更新规则（uhlc 遵循这一算法族；具体实现细节以 `uhlc` 为准）可形式化为：

本地盖章，设当前墙钟为 \( p_{now} \)：

\[
\begin{aligned}
&\text{若 } p_{now} > p:\quad p \leftarrow p_{now},\ l \leftarrow 0 \\
&\text{否则}:\quad l \leftarrow l + 1
\end{aligned}
\]

吸收对端时间戳 \((t_m, c_m)\)：

\[
p \leftarrow \max(p_{now},\ p,\ t_m),\quad
l \leftarrow
\begin{cases}
\max(l, c_m) + 1 & \text{若 } p = t_m \\
0 & \text{否则}
\end{cases}
\]

直觉是：物理时钟能往前跳就跳；跳不动（说明时钟没推进或对端更靠前）就用逻辑计数器 +1 来保证「新时间戳一定更大」。这样得到的时间戳既贴近真实时间，又严格服从因果关系。

#### 4.3.2 核心流程

HLC 在 Zenoh 里的生命周期：

```text
Config.timestamping.enabled(whatami) == true ?
  ├── 是 → HLCBuilder::new().with_id(zid).build() → Runtime.hlc = Some(HLC)
  └── 否 → Runtime.hlc = None

发布/盖章：
  runtime.new_timestamp() ──> Some(ts) 或 None
  session.new_timestamp()  ──> 一定有 ts（None 时回退墙钟）

转发（路由层，见 4.4）：
  收到带 ts 的 Put ──> hlc.update_with_timestamp(ts)
                       ├── Ok  → 继续
                       └── Err → ts 离本地时钟太远（「未来时间戳」），按策略丢弃/替换
```

HLC 的时钟标识用本节点 `ZenohId` 构造，所以时间戳里的「谁盖的章」就是节点身份。

#### 4.3.3 源码精读

Runtime 在初始化时**按配置条件**创建 HLC，并以 `zid` 作为时钟标识：

```rust
// zenoh/src/net/runtime/mod.rs
let hlc = (*unwrap_or_default!(config.timestamping().enabled().get(whatami)))
    .then(|| Arc::new(HLCBuilder::new().with_id(uhlc::ID::from(&zid)).build()));
```

即：只有当 `timestamping.enabled` 对当前角色为 `true` 时，才建 HLC；否则 `hlc = None`。源码见 [zenoh/src/net/runtime/mod.rs:697-698](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/mod.rs#L697-L698)。

`RuntimeState` 暴露的两个方法直接对应 HLC 的两类操作（`new_timestamp` 对应本地盖章；`update_with_timestamp` 在路由层调用）：

```rust
// zenoh/src/net/runtime/mod.rs
fn new_timestamp(&self) -> Option<uhlc::Timestamp> {
    self.hlc.as_ref().map(|hlc| hlc.new_timestamp())
}

fn hlc(&self) -> Option<&HLC> {
    self.hlc.as_ref().map(Arc::as_ref)
}
```

注意 `new_timestamp` 返回 `Option`，直接说明「没有 HLC 就没有时间戳」。源码见 [zenoh/src/net/runtime/mod.rs:257-267](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/runtime/mod.rs#L257-L267)。

> 说明：`uhlc` crate 的源码不在本仓库内（它是外部依赖），本讲不展开其内部字段布局，仅依据 Zenoh 对它的**可验证用法**（`HLCBuilder::new().with_id(...).build()`、`hlc.new_timestamp()`、`hlc.update_with_timestamp(ts) -> Result`、`Timestamp::new(time, id)`）来讲解。要查阅 HLC 的精确算法实现，请参考 [uhlc 文档](https://docs.rs/uhlc/)。

#### 4.3.4 代码实践

**实践目标**：通过配置开关，观察「有没有 HLC」对自动时间戳的影响。

**操作步骤**：

1. 默认 peer 会话下，发布若干条**不带** `.timestamp()` 的消息（沿用 4.2 的 `ts_pub`，去掉显式 timestamp 那段），订阅端确认 `timestamp()` 为 `None`。
2. 用配置强制启用时间戳（关键配置键为 `timestamping/enabled`，**待确认**该键在当前版本 JSON5 路径下的确切写法），重建会话：

   ```rust
   // 示例代码
   let mut config = zenoh::Config::default();
   config.insert_json5("timestamping/enabled", "true").unwrap(); // 待确认键名
   let session = zenoh::open(config).await.unwrap();
   ```

3. 再次发布不带 `.timestamp()` 的消息，订阅端观察。

**需要观察的现象**：第 1 步 `None`；第 3 步若配置生效，则 `timestamp()` 变为 `Some`（由 Runtime HLC 自动盖）。

**预期结果**：这验证了 `resolve_put` 里 `timestamp.or_else(|| self.0.runtime.new_timestamp())` 的行为——HLC 存在时自动盖，不存在时为 `None`。配置键名请以本地 `DEFAULT_CONFIG.json5` 与 `Config::get_json` 实测为准（待本地验证）。

#### 4.3.5 小练习与答案

**练习 1**：为什么 router 默认启用 HLC，而 peer/client 默认不启用？

> **参考答案**：router 是网络中的中转/汇聚节点，常承担存储合并、跨子网转发、为多副本数据去重的职责，需要权威的、单调的时间戳来判定「谁更新」，因此默认 `timestamping.enabled=true`。peer/client 多为数据生产/消费端点，给每条消息盖章会增加开销，且它们通常不需要做权威合并，故默认关闭以省带宽和 CPU。需要时间戳语义的应用可显式开启或用 `.timestamp()` 手动盖。

**练习 2**：HLC 的逻辑计数器 \( l \) 什么时候才会非零？

> **参考答案**：当本地物理时钟在两次盖章之间没有推进（\( p_{now} \le p \)），或收到了时间值领先于本地的对端时间戳时，物理时间无法区分先后，HLC 就靠 \( l \) 自增来保证单调。时钟走得越慢、消息越密集，\( l \) 越容易非零。

---

### 4.4 时间戳在路由与合并中的作用

#### 4.4.1 概念说明

时间戳不是「盖了就完事」，它在网络里有两个实际用途：

1. **路由层补盖与安全检查**：当数据流经一个启用了 HLC 的节点（典型是 router）时，该节点会检查每条 `Put` 的时间戳——没有就补一个；有就用本地 HLC「吸收」它。如果对端时间戳远超本地时钟（疑似「来自未来」），吸收会失败，节点按策略丢弃或替换该时间戳。这就是配置项 `drop_future_timestamp` 的由来。
2. **查询合并（Latest 去重）**：当一次 `get` 命中多个 Queryable（例如多个存储副本），默认的 `Latest` 合并会**按应答 key 分组、保留时间戳最大的那条**。没有时间戳，就无法判断「哪个副本更新」，合并也就无从谈起。

这两点共同回答了本讲主题里的「时间戳在乱序合并中的作用」。

#### 4.4.2 核心流程

路由转发一条 `Put` 时，时间戳的处理由 `treat_timestamp!` 宏统一完成：

```text
if 本节点有 HLC:
    if Put 带时间戳 ts:
        hlc.update_with_timestamp(ts):
            Ok  → 接受（HLC 已吸收 ts，未来本地时间戳必 ≥ ts）
            Err → ts 离本地时钟太远：
                    drop_future_timestamp == true  → 丢弃整条消息
                    drop_future_timestamp == false → 用本地新时间戳替换 ts
    else (Put 无时间戳):
        用 hlc.new_timestamp() 补盖一个
else:
    原样转发（不碰时间戳）
```

这个流程解释了 4.2 实践里的前提警告：**经过 router 的无时间戳消息会被补盖**，所以只有绕开 router（peer 直连）才能稳定观察到 `None`。

查询侧的 Latest 合并则用 `Timestamp` 的全序：同一 key 的多条应答里，时间戳大者胜出。

#### 4.4.3 源码精读

`treat_timestamp!` 宏集中体现了路由层的时间戳策略：

```rust
// zenoh/src/net/routing/dispatcher/pubsub.rs
macro_rules! treat_timestamp {
    ($hlc:expr, $payload:expr, $drop:expr) => {
        if let Some(hlc) = $hlc {
            if let zenoh_protocol::zenoh::PushBody::Put(data) = &mut $payload {
                if let Some(ref ts) = data.timestamp {
                    // 有时间戳：用本地 HLC 吸收它
                    match hlc.update_with_timestamp(ts) {
                        Ok(()) => (),
                        Err(e) => {
                            if $drop {
                                // 策略：丢弃整条「未来」消息
                                return;
                            } else {
                                // 策略：用本地新时间戳替换
                                data.timestamp = Some(hlc.new_timestamp());
                            }
                        }
                    }
                } else {
                    // 无时间戳：补盖一个
                    data.timestamp = Some(hlc.new_timestamp());
                }
            }
        }
    }
}
```

注意三个分支：吸收成功、`Err` 时按 `drop_future_timestamp` 丢弃或替换、无时间戳时补盖。完整源码见 [zenoh/src/net/routing/dispatcher/pubsub.rs:131-164](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/pubsub.rs#L131-L164)。

该宏在每条数据转发前被调用，`$drop` 取自 `rtables.data.drop_future_timestamp`（即配置 `timestamping/drop_future_timestamp`，默认 `false`）：

```rust
// zenoh/src/net/routing/dispatcher/pubsub.rs
if !route.is_empty() {
    treat_timestamp!(
        &rtables.data.hlc,
        msg.payload,
        rtables.data.drop_future_timestamp
    );
    ...
}
```

调用点见 [zenoh/src/net/routing/dispatcher/pubsub.rs:284-288](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/pubsub.rs#L284-L288)；`drop_future_timestamp` 的来源（从配置读取）见 [tables.rs:181-199](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/net/routing/dispatcher/tables.rs#L181-L199)。

> 关于查询 Latest 合并：默认的查询合并策略是「按 key 保留最大时间戳」（详见《u4-l2 Get 与 Querier》关于 `QueryConsolidation` / `ConsolidationMode::Latest` 的讨论）。它依赖 `Timestamp` 的全序——这正是 4.1 强调「时间戳必须可比较」的原因。具体的合并实现位于 net 层的应答收集逻辑中，本讲不展开，留作综合实践的延伸阅读。

#### 4.4.4 代码实践

**实践目标**：通过对比「直连」与「经 router」，亲眼看路由层的「补盖」行为。

**操作步骤**：

1. **直连场景**：两个终端分别跑 4.2 的 `ts_pub`（仅发 `no-ts` 那条）和 `ts_sub`，peer 模式直连。订阅端应打印 `[无 ts]`。
2. **经 router 场景**：先启动一个 router（router 默认 `timestamping.enabled=true`）：

   ```bash
   cargo run -p zenohd -- --listen tcp/127.0.0.1:7447
   ```

   再让 pub/sub 以 client 模式连到它（修改示例的 `Config`，或参考 `z_pub` 的 `-m client -e tcp/127.0.0.1:7447`）。订阅端再观察同一条 `no-ts` 消息。

**需要观察的现象**：直连时 `timestamp()` 为 `None`；经 router 时同一条消息变成了 `Some(...)`——这正是 `treat_timestamp!` 里「无时间戳就补盖」的结果。

**预期结果**：两次对比清楚展示「时间戳是否被自动盖」完全取决于转发路径上有没有启用 HLC 的节点。该实验需本地实际运行（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：`drop_future_timestamp=false`（默认）时，路由器收到一个「来自未来」的时间戳会怎样？设为 `true` 又会怎样？

> **参考答案**：`false` 时，`update_with_timestamp` 报错后，路由器会用本地 HLC 生成的新时间戳**替换**那个异常时间戳（消息继续转发，但时间戳被改写）。`true` 时，路由器直接**丢弃**整条消息（`return`），阻止一个明显异常的时间戳污染下游。默认 `false` 偏向「保数据到达」，`true` 偏向「保时间戳可信」。

**练习 2**：为什么查询的 Latest 合并离不开时间戳？如果应答都没有时间戳会怎样？

> **参考答案**：Latest 合并要「同一 key 只保留最新的一条」，而「最新」必须由 `Timestamp` 的全序来判定。若所有应答都没有时间戳，就无法比较先后，合并只能退化为「到达顺序」或直接不合并（取决于 `ConsolidationMode`），可能保留到非最新的副本。这也是 router 默认启用 HLC、保证流经数据带时间戳的重要原因。

---

## 5. 综合实践

把本讲三个最小模块串起来，完成一个「带因果序的有序日志收集器」：

1. **设计**：多个 publisher（模拟分布式节点）向 `logs/**` 发布带时间戳的事件（每条都调 `session.new_timestamp()` 附带）。一个 subscriber 收集并打印。
2. **实现要点**：
   - 发布端用 `publisher.put(payload).timestamp(session.new_timestamp())`，payload 里再额外塞一个「本地序号」字段，方便和「时间戳序」做对比。
   - 订阅端把收到的样本按 `sample.timestamp()` 排序后输出（用 `Timestamp` 的全序，注意处理 `None`）。
3. **验证**：
   - 故意让其中一个 publisher 在发布之间 `sleep`，制造「物理时间靠后但本地序号靠前」的情况，观察按时间戳排序是否仍正确反映因果。
   - 把某一端改成不附带时间戳，观察它在排序时如何被处理（落到 `None` 桶，无法参与因果排序）。
4. **延伸**：把订阅端换成经 router 中转，验证 router 是否为无时间戳的消息补盖；再查阅 `QueryConsolidation::Latest`（见《u4-l2》）理解时间戳在合并里的角色。

> 这个任务同时用到 4.1（Timestamp 结构与比较）、4.2（生成与附带）、4.3（HLC 单调性）、4.4（路由补盖），是检验你是否真正理解「时间戳从哪来、怎么用」的综合练习。

## 6. 本讲小结

- `Timestamp = 时间值(NTP64) + 逻辑计数器 + 时钟标识(TimestampId)`，由外部 `uhlc` crate 提供，经 `zenoh-protocol::core` 与 `zenoh::time` re-export。
- 时钟标识用本节点 `ZenohId`，使两个节点即使在同一物理时刻盖章也能区分，从而定义全序——这是查询合并「保最新」的前提。
- `Sample.timestamp()` 返回 `Option<&Timestamp>`，时间戳是**可选**的。
- `Session::new_timestamp()` 一定返回时间戳（无 HLC 时回退墙钟+zid）；而 `resolve_put` 仅在「用户显式设置」或「Runtime 有 HLC」时才附带时间戳。
- Runtime 是否拥有 HLC 取决于 `timestamping.enabled`，默认 `router=true / peer=false / client=false`，故默认 peer/client 会话不自动盖时间戳。
- 路由层 `treat_timestamp!` 会补盖无时间戳的消息，并用 `update_with_timestamp` 吸收/拒绝「未来时间戳」（受 `drop_future_timestamp` 控制）；这也是「绕开 router 才能稳定看到 `None`」的原因。

## 7. 下一步学习建议

- **进入内核**：若想看清 HLC 如何随 Runtime 一起初始化，建议读《u7-l1 Session 内部与 Runtime》和《u7-l3 Runtime 编排器》。
- **路由与合并**：本讲提到的 `treat_timestamp!` 和查询 Latest 合并属于路由子系统，可继续读《u8-l1 路由骨架》《u8-3 Dispatcher》。
- **存储后端**：时间戳是存储去重的依据，学习《u11-l3 存储与后端》时会再次遇到它——届时你会更理解「为什么存储实现需要按时间戳保留最新版本」。
- **协议层**：想看时间戳在协议消息里如何编码，可衔接《u10-l1 协议消息模型》中 `Put`/`Reply` 的 `timestamp` 字段。
