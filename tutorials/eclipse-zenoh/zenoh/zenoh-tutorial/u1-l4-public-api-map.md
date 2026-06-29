# 公开 API 地图：lib.rs 模块导览

## 1. 本讲目标

本讲是 Zenoh 入门层（第 1 单元）的最后一篇，目标是帮你把「公开 API 的全景图」装进脑子。读完本讲，你应该能够：

- 说清楚 `zenoh/src/lib.rs` 在整个 crate 里的角色——它是公开 API 的「门面（facade）」，所有面向用户的类型、函数都从这里 re-export。
- 凭记忆画出 `session` / `key_expr` / `pubsub` / `query` / `bytes` / `handlers` / `qos` / `scouting` / `liveliness` / `matching` / `time` / `sample` 这些公开模块与各自代表类型的对应关系。
- 理解 Zenoh 几乎处处在用的 **builder 模式**，以及支撑它的三大基础 trait：`Resolvable` / `Resolve` / `Wait`——为什么一个 builder 既能 `.await` 又能 `.wait()`。
- 知道哪些 API 被 `unstable` / `internal` 这两个 feature 门控（gate），以及两者在「是否可能稳定」上的根本差别。

本讲只读两个文件，**不写应用代码**：[zenoh/src/lib.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs) 和 [zenoh/src/api/mod.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/mod.rs)。它是后续每一篇模块讲义（u2 Session、u3 Pub/Sub、u4 Query……）的目录页。

## 2. 前置知识

本讲假定你已经读过：

- **u1-l1**：知道 Zenoh 是统一「data in motion / at rest / compute」的协议栈，仓库分 `zenoh` / `zenoh-ext` / `zenohd` / `plugins` / `commons` / `io` 六大块，且只有 `zenoh` 和 `zenoh-ext` 提供稳定公开 API。
- **u1-l3**：知道 crate 的分层与「feature 层层转发」（`transport_tcp`、`shared-memory`、`unstable` 等从 `zenoh` 一路下发到内部 crate）。

下面用到的几个 Rust 概念，先用一句话解释：

- **re-export（再导出）**：在一个模块里用 `pub use` 把别处定义的项「搬到」当前路径下。Zenoh 的 `lib.rs` 大量用 `pub use crate::api::...`，把内部 `api` 目录里的类型挑选后搬到公开命名空间。
- **门面模块（facade module）**：一个 `pub mod xxx { ... }` 的块里，几乎只有 `pub use`、没有真正的实现，作用是给用户呈现一个干净、经过挑选的接口。
- **feature 门控（feature gating）**：用 `#[cfg(feature = "xxx")]` 让某段代码只在启用某 feature 时才编译。
- **proc-macro attribute（过程宏属性）**：形如 `#[zenoh_macros::unstable]`，编译期把自身展开成一串普通属性（本讲会看到它展开成什么）。

## 3. 本讲源码地图

| 文件 | 角色 |
|---|---|
| `zenoh/src/lib.rs` | crate 根。包含模块级文档、顶层 re-export、各 `pub mod` 门面模块、版本/特性常量。**本讲的主角。** |
| `zenoh/src/api/mod.rs` | 内部实现根。用 `pub(crate)` 列出全部内部子模块（session、publisher、subscriber……），是 `lib.rs` 的「挑选来源」。 |
| `commons/zenoh-core/src/lib.rs` | 定义 `Resolvable` / `Wait` / `Resolve` 三大基础 trait。被 `lib.rs` 顶层 re-export。 |
| `zenoh/Cargo.toml` | 定义 `zenoh` crate 的全部 feature，包括 `unstable` / `internal` 以及各 `transport_*`。 |
| `commons/zenoh-macros/src/lib.rs` | 定义 `#[zenoh_macros::unstable]` 与 `#[zenoh_macros::internal]` 两个属性宏，本讲用来解释门控的展开。 |

---

## 4. 核心概念与源码讲解

### 4.1 lib.rs：公开 API 的门面与模块文档

#### 4.1.1 概念说明

当你写 `use zenoh::Session;` 时，这个 `Session` 是从哪里冒出来的？答案就在 `zenoh/src/lib.rs`。

在 Rust 里，crate 根文件（`lib.rs`）决定了「外部用户能看见什么」。Zenoh 采取了一个清晰的分层：

- **实现层**：真正的代码住在 `api/`（公开 API 的实现）和 `net/`（内部网络层）两个**私有**目录里。
- **公开层**：`lib.rs` 像一个「前台」，从实现层里**挑选**出稳定的类型，按主题打包成一个个 `pub mod` 门面模块，呈现给用户。

这种「内部随便长、外部精心摆」的做法，让 Zenoh 可以在不破坏用户代码的前提下重构内部——这正是 u1-l1 强调的「稳定边界」在源码层面的体现。

#### 4.1.2 核心流程

`lib.rs` 自上而下大致是六段结构：

1. **模块级文档（`//!`）**：crate 的使用说明，含「Components and concepts」与「Features」两节，相当于一份内嵌的 README。
2. **私有模块声明**：`mod api;` 和 `mod net;`，把实现层挂进来但不公开。
3. **版本/特性常量**：`GIT_VERSION`、`FEATURES` 等，供运行时自描述。
4. **顶层基础 re-export**：`Resolvable/Resolve/Wait`、`Error`、`Result`、`Config`、`open`、`Session`、`scout`——这些是「无命名空间」的根级符号。
5. **各 `pub mod` 门面模块**：`session` / `key_expr` / `pubsub` / `query` / …… 每个都是一段文档 + 一组 `pub use`。
6. **门控模块**：`internal`、`shm`、`cancellation` 等需要 feature 才编译的模块。

#### 4.1.3 源码精读

首先是两行决定性的私有声明——`api` 和 `net` 都是**私有** `mod`，外部无法直接访问：

[zenoh/src/lib.rs:217-218](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L217-L218) —— 把实现层 `api/` 与 `net/` 作为私有模块挂进 crate 根。

对照 [zenoh/src/api/mod.rs:15-41](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/mod.rs#L15-L41) 可以看到，`api` 里所有子模块（session、publisher、subscriber、querier、queryable、bytes、handlers……）都是 `pub(crate)`，**只在 crate 内可见**。用户拿不到 `zenoh::api::session::Session`，因为 `api` 本身就不公开。这就强制所有人只能走 `lib.rs` 挑选出来的那条公开路径。

然后是顶层「根级」re-export，这里集中了最常用的几个无命名空间符号：

[zenoh/src/lib.rs:275-288](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L275-L288) —— re-export 三大基础 trait、`Error`/`Result`、日志辅助函数，以及最常用的 `Config` / `scout` / `open` / `Session`。

注意第 279 行：`pub use zenoh_result::ZResult as Result;`——所以 `zenoh::Result` 其实就是内部 `ZResult` 的别名。这也是为什么很多示例里能看到 `zenoh::open(...).await.unwrap()`，错误类型是 `zenoh::Error`。

模块级文档开头的「Components and concepts」是 Zenoh 官方对自身架构的一句话总结，值得逐字读：

[zenoh/src/lib.rs:22-72](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L22-L72) —— 依次定义 Session、Pub/Sub、Query/Reply、Key Expressions、Data representation、scouting/liveliness/matching 等概念，每个都指向对应的公开模块。

这段文档其实就是本讲后面「模块全景图」的官方依据。

#### 4.1.4 代码实践

这是一个**源码阅读型**实践（不需要运行）。

1. **实践目标**：亲手验证「公开 API 全部经 lib.rs re-export」这一结论。
2. **操作步骤**：
   - 打开 [zenoh/src/lib.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs)。
   - 数一下从第 290 行（`pub mod key_expr`）到第 1017 行（`pub mod config` 结束）一共有多少个顶层 `pub mod`。
   - 再看 [zenoh/src/api/mod.rs:15-41](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/mod.rs#L15-L41)，对比 `api/` 内部的子模块数量。
3. **需要观察的现象**：`api/` 里的子模块比 `lib.rs` 的公开 `pub mod` 多（例如 `api/mod.rs` 有 `cancellation`、`connectivity`、`info`、`loader`、`plugins` 等内部模块），它们要么不直接对外，要么被合并进某个公开模块的 `pub use`。
4. **预期结果**：你会确认——**用户可见的公开模块数 < 内部实现模块数**，`lib.rs` 是一道「挑选 + 重新组织」的关卡。
5. 不涉及运行，无需「待本地验证」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `lib.rs` 要把 `api` 声明成私有 `mod api;` 而不是 `pub mod api;`？

> **参考答案**：若 `api` 公开，用户就能直接 `use zenoh::api::session::Session` 绕过门面，导致内部重构（重命名、移动文件）直接破坏用户代码。私有化 `api` 后，所有公开符号都只能从 `lib.rs` 的 `pub use` 进入，Zenoh 团队只需保证这些被挑选出的符号稳定即可。

**练习 2**：`zenoh::Result` 和 `zenoh::Error` 分别是从哪个内部 crate re-export 来的？

> **参考答案**：都来自 `zenoh-result` crate。`Result` 是 `zenoh_result::ZResult` 的别名（lib.rs:279），`Error` 直接是 `zenoh_result::Error`（lib.rs:277）。

---

### 4.2 公开 API 的模块全景图

#### 4.2.1 概念说明

`lib.rs` 中段的每一个 `pub mod xxx { ... }` 都是一个门面模块，模块上方有一段 `///` 文档说明它的职责，模块体内部是一组 `pub use crate::api::...`。理解这些模块的**分工**，等于拿到了整本学习手册的目录——后面的单元几乎每个都对应这里的一两个模块。

#### 4.2.2 核心流程

可以把 13 个公开模块按职责分成四组：

| 分组 | 模块 | 职责 |
|---|---|---|
| 会话与配置 | `session`、`config` | 打开会话、读取/修改配置 |
| 两种通信范式 | `pubsub`、`query` | 发布/订阅、查询/应答 |
| 数据与地址 | `key_expr`、`sample`、`bytes`、`time` | 地址空间、数据单元、负载字节、时间戳 |
| 机制与支撑 | `handlers`、`qos`、`scouting`、`liveliness`、`matching` | 取数方式、服务质量、发现、存活、匹配感知 |

#### 4.2.3 源码精读

下面这张「模块 → 代表类型」对照表，是本讲最重要的产出，每个类型都来自对应 `pub mod` 内部的 `pub use`：

| 公开模块 | 一句话职责 | 代表类型（来自该模块的 re-export） |
|---|---|---|
| `session` | 会话根，打开/关闭、声明一切实体 | `Session`、`open` |
| `config` | 传给 `open`/`scout` 的配置 | `Config`、`WhatAmI` |
| `key_expr` | Zenoh 的地址空间（含通配） | `KeyExpr`、`OwnedKeyExpr` |
| `pubsub` | 发布/订阅范式 | `Publisher`、`Subscriber` |
| `query` | 查询/应答范式 | `Queryable`、`Querier`、`Query`、`Reply`、`Selector` |
| `sample` | 数据单元（负载 + 全部元数据） | `Sample`、`SampleKind` |
| `bytes` | 原始字节负载与编码 | `ZBytes`、`Encoding` |
| `handlers` | 取数方式：回调 / 通道 | `IntoHandler`、`FifoChannel`、`RingChannel` |
| `qos` | 服务质量：可靠性/拥塞/优先级 | `Reliability`、`CongestionControl`、`Priority` |
| `scouting` | 发现网络中的 Zenoh 节点 | `scout`、`Scout`、`Hello` |
| `liveliness` | 节点存活检测 | `Liveliness`、`LivelinessToken` |
| `matching` | 对端匹配感知 | `MatchingListener`、`MatchingStatus` |
| `time` | 时间戳（HLC） | `Timestamp`、`NTP64` |

我们挑两个代表性模块看其「门面」写法。

`pubsub` 模块——文档先讲 Put/Delete 两种语义，再用 `pub use` 把发布侧与订阅侧的类型一次性摆出：

[zenoh/src/lib.rs:550-562](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L550-L562) —— `pub mod pubsub`：re-export `Publisher`、`Subscriber` 及其 builder 和「undeclaration」类型。

注意它还导出了 `PublisherUndeclaration`、`SubscriberUndeclaration`——Zenoh 里声明出来的实体大多可以「反声明（undeclare）」以释放资源，这是后续讲义会反复见到的模式。

`bytes` 模块——把负载容器与编码器放在一起：

[zenoh/src/lib.rs:490-495](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L490-L495) —— `pub mod bytes`：re-export `ZBytes`、`ZBytesReader`、`ZBytesWriter`、`Encoding` 等。

它的模块文档（lib.rs:447-489）还明确指出：基本类型的序列化/反序列化在 `zenoh-ext` crate 的 `z_serialize` / `z_deserialize`，不在本 crate——这是 u5 单元会展开的内容。

`session` 模块是最重要的根，导出了 `open`、`Session`，并附带一批 builder：

[zenoh/src/lib.rs:405-415](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L405-L415) —— `pub mod session`：re-export `open`、`Session`、`SessionClosedError`、`OpenBuilder`、`SessionPutBuilder`、`SessionGetBuilder` 等。

#### 4.2.4 代码实践

这是本讲指定的主实践任务。

1. **实践目标**：亲手从 `lib.rs` 中整理出一张「模块 → 类型」对照表（针对 `session`、`pubsub`、`query`、`bytes`、`handlers` 五个模块，各找 2 个核心类型）。
2. **操作步骤**：
   - 打开 [zenoh/src/lib.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs)，定位到 4.2.3 给出的每个 `pub mod` 块。
   - 阅读块内的 `pub use crate::api::{...}`，挑出你认为「最核心」的 2 个类型（提示：通常名字最短、不带 `Builder`/`Undeclaration` 后缀的实体类型就是核心，例如 `Session`、`Publisher`、`Queryable`、`ZBytes`、`IntoHandler`）。
3. **需要观察的现象**：每个门面模块都同时导出「实体类型 + builder + undeclaration」三件套，形成「声明→使用→反声明」的完整生命周期。
4. **预期结果**：你应能得到类似下面这样的表（参考答案见 4.2.5）。
5. 不涉及运行，无需「待本地验证」。

#### 4.2.5 小练习与答案

**练习 1（即主实践任务的参考答案）**：给出 `session`、`pubsub`、`query`、`bytes`、`handlers` 五个模块各 2 个核心类型。

> **参考答案**：
> | 模块 | 核心类型 1 | 核心类型 2 |
> |---|---|---|
> | `session` | `Session` | `open`（函数） |
> | `pubsub` | `Publisher` | `Subscriber` |
> | `query` | `Queryable` | `Querier`（也可选 `Query`/`Reply`/`Selector`） |
> | `bytes` | `ZBytes` | `Encoding` |
> | `handlers` | `IntoHandler` | `FifoChannel`（也可选 `RingChannel`） |
> 依据：[session lib.rs:405-415](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L405-L415)、[pubsub lib.rs:551-561](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L551-L561)、[query lib.rs:654-666](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L654-L666)、[bytes lib.rs:491-494](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L491-L494)、[handlers lib.rs:803-806](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L803-L806)。

**练习 2**：`query` 模块里 `Selector` 和 `Reply` 分别对应查询/应答范式的哪一侧？

> **参考答案**：`Selector` 是请求侧用来描述「查什么」（key expression + 参数，形如 `key?name=value;...`）；`Reply` 是请求侧收到的每一条应答，里面要么是 `Sample` 要么是 `ReplyError`。提供数据的一侧用 `Queryable` 接收 `Query` 并 `reply`。详见 [query 模块文档 lib.rs:564-644](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L564-L644)。

---

### 4.3 Builder 模式与 Resolvable / Resolve / Wait 三大基础 trait

#### 4.3.1 概念说明

Zenoh 几乎所有「创建实体」的操作都不是直接 `new` 出来，而是走 builder：先拿到一个 builder，配置一番，最后「兑现（resolve）」。例如：

```text
session.declare_publisher("key")      // → PublisherBuilder
        .reliability(Reliable)         // 仍返回 builder，链式配置
        .await                         // 兑现成 Publisher
```

问题来了：为什么这个 builder **既能 `.await`（异步）又能 `.wait()`（同步）**？答案就是被 `lib.rs` 顶层 re-export 的三个 trait：`Resolvable`、`Wait`、`Resolve`。

#### 4.3.2 核心流程

三者关系可以用一段伪代码刻画：

```text
trait Resolvable {            // 标记「我能被兑现」
    type To;                  // 兑现后的目标类型，如 Publisher
}

trait Wait: Resolvable {      // 同步兑现
    fn wait(self) -> Self::To;
}

trait Resolve<Output>:        // 统一 trait：综合了下面所有能力
    Resolvable<To = Output>
    + Wait                    // 可同步 wait
    + IntoFuture<Output = Output>  // 可异步 await
    + Send
{}
```

也就是说，一个 builder 通常同时实现：

- `Resolvable`，声明「我兑现后会变成 `To`」；
- `IntoFuture`，于是可以 `.await`；
- `Wait`，于是可以 `.wait()`。

而 `Resolve<Output>` 只是把这几条**收拢成一个上界（super-trait 集合）**，方便在泛型里写 `T: Resolve<Publisher>`。

此外，`Resolvable` 带有 `#[must_use]` 警告：如果你创建了一个 builder 却忘了 `.await` 或 `.wait()`，编译器会警告「这东西什么都不会做」——这能在编译期拦住大量「声明了却没生效」的 bug。

#### 4.3.3 源码精读

`lib.rs` 顶层把它们作为根基 re-export：

[zenoh/src/lib.rs:275](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L275) —— `pub use zenoh_core::{Resolvable, Resolve, Wait};`，三大 trait 从 `zenoh-core` 搬到 `zenoh` 根。

真正的定义在内部 crate：

[commons/zenoh-core/src/lib.rs:33-35](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-core/src/lib.rs#L33-L35) —— `Resolvable`：只有一个关联类型 `To`，纯标记「我能被兑现成什么」。

[commons/zenoh-core/src/lib.rs:52-55](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-core/src/lib.rs#L52-L55) —— `Wait`：提供同步的 `fn wait(self) -> Self::To`。

[commons/zenoh-core/src/lib.rs:62-70](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-core/src/lib.rs#L62-L70) —— `Resolve<Output>`：带 `#[must_use]` 的总集 trait，要求实现者同时满足 `Resolvable + Wait + IntoFuture + Send`。注意第 62 行的 `#[must_use]` 文案：「Resolvables do nothing unless you resolve them using `.await` or `zenoh::Wait::wait`」。

注意第 72-79 行还有一个**全量 blanket impl**：任何满足条件的类型自动实现 `Resolve<Output>`，所以 Zenoh 的 builder 作者只需各自实现 `Resolvable`/`Wait`/`IntoFuture`，`Resolve` 自动获得。

回到 `lib.rs` 的模块文档，官方明确推荐「优先 await，慎用 wait」：

[zenoh/src/lib.rs:74-80](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L74-L80) —— 「Builders」小节：builder 在异步上下文用 `.await` 兑现，在同步上下文用 `Wait::wait` 兑现。

#### 4.3.4 代码实践

这是一个**源码阅读 + 行为预测型**实践（实际编译可选）。

1. **实践目标**：体会「不兑现就什么都不做」的 `#[must_use]` 警告，并理解 await/wait 的等价性。
2. **操作步骤**：
   - 阅读 [commons/zenoh-core/src/lib.rs:62-70](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-core/src/lib.rs#L62-L70) 上的 `#[must_use]` 文案。
   - （可选）在一个依赖 `zenoh` 的小程序里写一行 `session.declare_publisher("k");` 但**不** `.await` 也不绑定变量，用 `cargo build` 观察编译器是否报警告。
3. **需要观察的现象**：编译器应给出形如 `unused builder ... Resolvables do nothing unless you resolve them using .await or zenoh::Wait::wait` 的警告。
4. **预期结果**：确认 Zenoh 用类型系统在编译期提示「声明必须兑现」。若你环境里不便编译，可标注「待本地验证」。
5. 进阶：在同一程序里对比 `let p = session.declare_publisher("k").await?;`（异步）与 `let p = zenoh::Wait::wait(session.declare_publisher("k"));`（同步），体会二者都产出 `Publisher`，只是阻塞语义不同。

#### 4.3.5 小练习与答案

**练习 1**：`Resolvable`、`Wait`、`Resolve` 三者，哪个是「定义了实际方法」的，哪个只是「标记 + 约束集合」？

> **参考答案**：`Wait` 定义了实际方法 `fn wait(self) -> Self::To`；`Resolvable` 只有关联类型 `To`、是标记 trait；`Resolve<Output>` 没有自己新的方法，它只是一个把 `Resolvable + Wait + IntoFuture + Send` 收拢到一起的上界集合（见 [zenoh-core/src/lib.rs:63-70](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-core/src/lib.rs#L63-L70)）。

**练习 2**：为什么官方建议优先 `.await` 而慎用 `.wait()`？

> **参考答案**：`wait()` 是同步阻塞当前线程直到兑现完成，在异步运行时里阻塞线程容易拖垮调度、甚至死锁；`.await` 则是异步挂起、让出执行权。Zenoh 是异步架构，故推荐 await（见 [lib.rs:60-61](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-core/src/lib.rs#L60-L61) 的注释）。

---

### 4.4 feature 门控：unstable / internal 与传输特性

#### 4.4.1 概念说明

并不是所有写在 `lib.rs` 里的 API 都默认可见。Zenoh 用 Cargo feature 给 API 分了三个可见等级：

- **默认稳定**：不加任何 feature 就能用，例如 `Session`、`Publisher`、`ZBytes`。
- **`unstable`**：API 已公开但「可能在未来版本改动或消失」，**有可能**稳定下来。
- **`internal`**：暴露实现细节的 API，主要给其他语言 binding（Python/C/Java）和 `zenohd` 插件用，**本质上是实现的一部分**，不会稳定。

此外还有一组与可见性无关的「能力 feature」：`transport_tcp` / `transport_quic` / `shared-memory` / `stats` / `plugins` 等，决定要不要编译某块传输或能力。本节聚焦 `unstable` 与 `internal` 这两个**可见性**门控。

#### 4.4.2 核心流程

Zenoh 不直接手写 `#[cfg(feature = "unstable")]`，而是用两个**属性宏**：

```text
#[zenoh_macros::unstable]   // 编译期展开为 → #[cfg(feature = "unstable")] + 文档警告
pub fn foo() {}

#[zenoh_macros::internal]   // 编译期展开为 → #[cfg(feature = "internal")] + #[doc(hidden)]
pub fn bar() {}
```

于是：

- 不开 `unstable` feature → 带 `#[zenoh_macros::unstable]` 的项**根本不编译**，用户看不见。
- 不开 `internal` feature → 带 `#[zenoh_macros::internal]` 的项同样不编译，且即便编译了也 `#[doc(hidden)]`（文档里不显示）。

而 `zenoh` crate 的 `Cargo.toml` 里，`unstable` 和 `internal` 这两个 feature 又会**转发**给底层 crate（`zenoh-config`、`zenoh-keyexpr`、`zenoh-protocol`、`zenoh-transport`），这就是 u1-l3 讲过的「feature 层层下发」。

#### 4.4.3 源码精读

先看 `lib.rs` 模块文档里的 Features 一节，它列出了全部 feature 并明确区分了 `unstable` 与 `internal` 的语义：

[zenoh/src/lib.rs:201-204](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L201-L204) —— 原文要点：`unstable` 的 API **可能**稳定；`internal` 因暴露实现细节而**本质不稳定**。

具体到代码，门控发生在两个层面。

**第一层：`Cargo.toml` 定义 feature 并转发。**

[zenoh/Cargo.toml:47-51](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/Cargo.toml#L47-L51) —— `internal` feature 下发给 `zenoh-config/internal`、`zenoh-keyexpr/internal`、`zenoh-protocol/internal`。

[zenoh/Cargo.toml:79-84](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/Cargo.toml#L79-L84) —— `unstable` feature 下发给 `zenoh-config`、`zenoh-keyexpr`、`zenoh-protocol`、`zenoh-transport` 的 `unstable`。

**第二层：属性宏把标注展开成 `#[cfg]`。**

[commons/zenoh-macros/src/lib.rs:204-223](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-macros/src/lib.rs#L204-L223) —— `unstable` 宏：在第 219 行 `parse_quote!(#[cfg(feature = "unstable")])` 加上门控属性。

[commons/zenoh-macros/src/lib.rs:227-244](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-macros/src/lib.rs#L227-L244) —— `internal` 宏：在第 238 行加 `#[cfg(feature = "internal")]`，第 239 行再加 `#[doc(hidden)]`。

**在 `lib.rs` 里的真实用例：**

[zenoh/src/lib.rs:220-233](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L220-L233) —— 顶层的 `KE_ADV_PREFIX` / `KE_AT` / `KE_EMPTY` 等管理空间 key 常量被 `#[cfg(feature = "internal")]` 门控（`internal` 宏展开后即此形态），普通用户看不到。

[zenoh/src/lib.rs:1027-1078](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L1027-L1078) —— 整个 `pub mod internal { ... }` 被 `#[zenoh_macros::internal]` 标注，里面集中了 `ZRuntime`、`TaskController`、`Condition`、`ZBuf` 等给 binding/插件用的内部工具。

`unstable` 的例子：`cancellation` 模块整体被 `#[zenoh_macros::unstable]` 标注：

[zenoh/src/lib.rs:1124-1130](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L1124-L1130) —— `pub mod cancellation`：导出 `CancellationToken`，需开 `unstable` 才可见。

还有一处「双重门控」的设计值得注意——`plugins` feature 强制要求同时开 `unstable` 和 `internal`，否则编译失败：

[zenoh/src/lib.rs:1019-1025](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L1019-L1025) —— `compile_error!`：启用 `plugins` 但没开 `unstable+internal` 时直接报编译错误。

#### 4.4.4 代码实践

1. **实践目标**：直观看到「门控项默认不可见」。
2. **操作步骤**：
   - 在 [zenoh/src/lib.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs) 中搜索 `#[zenoh_macros::unstable]` 与 `#[zenoh_macros::internal]` 两个标注，统计各有几个，分别落在哪些模块。
   - 选一个被 `#[zenoh_macros::unstable]` 标注的类型（例如 [zenoh/src/lib.rs:1126](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L1126) 的 `cancellation` 模块），思考：默认 `cargo build` 时它会不会被编译？
3. **需要观察的现象**：你会看到门控标注集中在 `internal` 模块、`shm` 模块、`cancellation` 模块，以及 `session`/`query`/`config` 等模块里少数带标注的项（如 `WeakSession`、`ReplySample`、`Notifier`）。
4. **预期结果**：默认编译时这些项**不存在**；用户必须 `features = ["unstable"]` 或 `["internal"]` 才能访问。这正是「稳定边界」的强制执行。
5. 若要在本地验证可见性变化：在一个依赖 `zenoh` 的 crate 里，先默认依赖，尝试 `use zenoh::cancellation::CancellationToken;` 应编译失败；再把依赖改成 `zenoh = { version = "...", features = ["unstable"] }`，应能编译通过（待本地验证）。

#### 4.4.5 小练习与答案

**练习 1**：`unstable` 和 `internal` 最本质的区别是什么？

> **参考答案**：`unstable` API 是「公开但可能变动」，**未来有可能稳定**；`internal` API 是「暴露实现细节」，主要服务于其他语言 binding 和 `zenohd` 插件，**本质就不会稳定**。源码依据见 [lib.rs:201-204](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L201-L204)；宏展开区别见 `internal` 额外加了 `#[doc(hidden)]`（[zenoh-macros/src/lib.rs:239](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-macros/src/lib.rs#L239)）。

**练习 2**：为什么 `plugins` feature 要用 `compile_error!` 强制 `unstable + internal` 同时打开？

> **参考答案**：插件 API 既不稳定（`unstable`）又紧贴实现（`internal`），仅靠 `#[cfg]` 让它「默默不可见」容易让用户误以为是普通 feature；用 `compile_error!` 给出明确报错，能避免用户在缺 feature 时踩坑（见 [lib.rs:1019-1025](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L1019-L1025)）。

---

## 5. 综合实践

把本讲四节串起来，完成一份「**Zenoh 公开 API 导览速查表**」：

1. **门面定位**：用一句话说明「为什么 `zenoh::Session` 能被 `use` 到」（因为 [lib.rs:283-288](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L283-L288) 的顶层 re-export），并指出 `Session` 的真正实现藏在哪个**私有**模块（提示：`api` 模块私有，见 [lib.rs:217](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L217) 与 [api/mod.rs:40](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/mod.rs#L40)）。
2. **模块全景**：把 4.2 的 13 个公开模块按「会话/范式/数据/机制」四组整理成一张表，标注每个模块对应后续哪一讲（例如 `session`→u2、`pubsub`→u3、`query`→u4、`bytes`→u5、`scouting`→u6-l1……）。
3. **builder 三连**：写出一个完整的 builder 链（伪代码即可），标注其中哪一步返回 builder、哪一步「兑现」，并指出兑现用到的三个 trait 中各自扮演的角色。
4. **门控自检**：列出 3 个默认不可见、需要 `unstable` 或 `internal` 才能用的 API（例如 `cancellation::CancellationToken`、`internal` 模块、`WeakSession`），并说明它们被门控的原因。

预期产出：一张可贴在墙上的速查表 + 一段不超过 6 行的中文总结，说明「公开 API 如何从私有实现层经 lib.rs 门面呈现，又如何被 feature 分级保护」。

## 6. 本讲小结

- `zenoh/src/lib.rs` 是公开 API 的**门面**：它把私有实现层 `api/` 与 `net/` 里的类型**挑选**后，用 `pub use` 重新组织成一个个 `pub mod` 门面模块呈现给用户。
- 公开模块共 13 个，分四组：会话配置（`session`/`config`）、通信范式（`pubsub`/`query`）、数据地址（`key_expr`/`sample`/`bytes`/`time`）、机制支撑（`handlers`/`qos`/`scouting`/`liveliness`/`matching`）——这张表就是整本学习手册的目录。
- 几乎所有「创建实体」都走 builder，统一由三大基础 trait 支撑：`Resolvable`（标记 + 目标类型）、`Wait`（同步兑现）、`Resolve`（综合上界，带 `#[must_use]` 提醒「不兑现就什么都不做」）；builder 既能 `.await` 又能 `.wait()`。
- 可见性由 feature 分级：默认稳定、`unstable`（可能稳定）、`internal`（本质不稳定，给 binding/插件用）。两者分别由 `#[zenoh_macros::unstable]` / `#[zenoh_macros::internal]` 展开成 `#[cfg(feature = ...)]`，`internal` 还额外 `#[doc(hidden)]`。
- 顶层根级符号（`open`、`Session`、`Config`、`scout`、`Result`、`Error`、`Resolvable/Resolve/Wait`）都集中在 [lib.rs:275-288](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L275-L288)，是日常使用频率最高的入口。
- `plugins` feature 用 `compile_error!` 强制 `unstable + internal` 同时开启，体现了 Zenoh 对「稳定边界」的强制执行。

## 7. 下一步学习建议

本讲建立了「公开 API 地图」，接下来建议按模块逐个深入：

1. **首选 u2-l1《打开一个 Session》**：从最核心的 `session` 模块切入，动手 `zenoh::open(config)`，理解 `Session` 的克隆语义与生命周期——这是后续所有讲义的运行基础。
2. **然后 u2-l2《Key Expression》**：进入 `key_expr` 模块，掌握 Zenoh 地址空间的通配符与集合关系（`includes`/`intersects`）。
3. 读源码时，养成习惯：看到任何 `zenoh::Xxx`，先回 [lib.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs) 找它属于哪个 `pub mod`、再顺着 `pub use crate::api::...` 跳进实现层——这条路就是本讲教给你的「API → 实现」导航法。
4. 如果你对内部实现好奇，可以提前扫一眼 [zenoh/src/api/mod.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/mod.rs) 的子模块列表，但**不要**在生产代码里依赖 `internal`/`unstable` API——等第 7 单元以后再系统学习内部架构。
