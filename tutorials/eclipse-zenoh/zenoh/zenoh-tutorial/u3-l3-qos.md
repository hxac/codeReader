# 服务质量（QoS）：可靠性、拥塞控制与优先级

## 1. 本讲目标

本讲承接《u3-l1 Pub/Sub 基础》，在已经会用 `Publisher` / `put` 的基础上，回答一个关键问题：**当网络或对端忙不过来时，Zenoh 该丢谁、该等谁、先发谁？** 这正是「服务质量（Quality of Service, QoS）」要解决的问题。

学完本讲你应当能够：

- 说清 **可靠性（Reliability）** 与 **拥塞控制（CongestionControl）** 这两个常被混淆的概念到底有什么区别。
- 在 `PublisherBuilder` 上正确设置 `reliability` / `congestion_control` / `priority` / `express`，并知道哪些需要 `unstable` feature。
- 理解 Zenoh 传输层如何把消息按 **优先级** 分流到不同的发送队列，以及在拥塞时如何根据「是否可丢弃（droppable）」决定丢弃还是阻塞。
- 读懂 `commons/zenoh-config/src/qos.rs` 中的 QoS 配置与「按 key expression 覆盖（overwrite）」机制。

## 2. 前置知识

在进入源码前，先用三个生活类比建立直觉：

- **可靠性（Reliability）** ——「我发的东西要不要保证送到」。像挂号信（Reliable）与平信（BestEffort）的区别。Zenoh 里它主要是一个**线路上的标记**，用来选择更合适的链路（例如 Reliable 走 TCP、BestEffort 走 UDP），而**不是**Zenoh 自己做重传。
- **拥塞控制（CongestionControl）** ——「队列满了怎么办」。像排队买奶茶：Drop 是「满了我就不排了直接走」，Block 是「我就在这等，等到有位置」。这是**发送端本地队列**的行为。
- **优先级（Priority）** ——「谁先上车」。Zenoh 给每条消息打一个优先级标签，传输层为每个优先级各维护一条发送队列，并按优先级从高到低依次服务。

一个关键概念是 **droppable（可丢弃）**：一条消息是否会在发送端队列拥塞时被丢掉，由可靠性和拥塞控制**共同**决定（见 4.1 节的公式）。本讲围绕它展开。

> 术语约定：本讲提到的「队列」「拥塞」都指 **发送端（publisher 侧）传输管道（pipeline）** 的本地缓冲队列，不是路由器里那种全网路由队列。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [zenoh/src/lib.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L837-L843) | 公开 API 门面，把 `CongestionControl` / `Reliability` / `Priority` 统一 re-export 到 `zenoh::qos` 模块。 |
| [zenoh/src/api/publisher.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/publisher.rs) | `Publisher` 结构（持有 QoS 字段）与公开 `Priority` 枚举的定义。 |
| [zenoh/src/api/builders/publisher.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/publisher.rs) | `PublisherBuilder` 上的 `congestion_control` / `priority` / `express` / `reliability` setter。 |
| [commons/zenoh-protocol/src/core/mod.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/mod.rs) | 协议层 `Reliability` / `CongestionControl` / `Priority`（含内部 `Control` 档）的权威定义。 |
| [commons/zenoh-config/src/qos.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-config/src/qos.rs) | 配置层的 QoS 类型，以及「按 key expression 覆盖 QoS」的数据结构。 |
| [commons/zenoh-protocol/src/network/mod.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/network/mod.rs) | `is_droppable` / `is_reliable` / `priority()` 的判定逻辑——把上述概念串起来的关键。 |
| [io/zenoh-transport/src/common/pipeline.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/common/pipeline.rs) | 传输管道：按优先级分队列入队、拥塞时丢/等的真正实现。 |
| [io/zenoh-transport/src/common/priority.rs](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/common/priority.rs) | 每个优先级下的 reliable / best_effort 收发通道与序列号。 |

---

## 4. 核心概念与源码讲解

本讲拆为三个最小模块：**4.1 可靠性与拥塞控制**、**4.2 优先级**、**4.3 QoS 配置与传输层落地**。

### 4.1 可靠性与拥塞控制（Reliability / CongestionControl）

#### 4.1.1 概念说明

这是初学者最容易混为一谈的两个维度，必须先分清：

- **Reliability（可靠性）** 回答「要不要尽量保证送达」。取值 `Reliable`（默认）或 `BestEffort`。它本质是消息在线路上携带的一个**标记**。
- **CongestionControl（拥塞控制）** 回答「发送端本地队列满了时怎么办」。取值 `Drop`（默认，丢了）或 `Block`（阻塞等待），另有 `unstable` 的 `BlockFirst`。

二者正交：你可以「可靠但允许在拥塞时丢弃」（Reliable + Drop），也可以「尽力但拥塞时死等」（BestEffort + Block）。它们如何叠加，取决于下面这个核心判定。

#### 4.1.2 核心流程

一条网络消息是否「可丢弃（droppable）」由以下逻辑决定（出自 `NetworkMessageExt`）：

\[
\text{droppable}(m) \;=\; \neg\,\text{reliable}(m)\;\;\lor\;\;\bigl(\text{congestion\_control}(m)=\text{Drop}\bigr)
\]

翻译成话：**只要「不是 Reliable」或「拥塞控制是 Drop」，这条消息就可丢弃**。反之，只有 **Reliable 且 Block** 的消息才「不可丢弃」——传输管道会为它**阻塞等待**队列腾出空间，而不是丢掉。

四种组合的结果：

| Reliability | CongestionControl | droppable | 队列满时行为 |
| --- | --- | --- | --- |
| Reliable（默认） | Block | 否 | **阻塞等待**（对发布者形成背压） |
| Reliable | Drop（默认） | 是 | 立即丢弃 |
| BestEffort | Block | 是 | 立即丢弃（BestEffort 本身就可丢） |
| BestEffort | Drop | 是 | 立即丢弃 |

> 注意一个反直觉点：因为公式里 `¬reliable` 一项，**只要 reliability 是 BestEffort，无论 congestion_control 怎么设，消息都是可丢弃的**。所以「真正会阻塞」的组合只有 `Reliable + Block` 一种。这也是为什么实践中想防止丢消息，通常直接用默认的 `Reliable + Block`。

至于 Reliable 是否会「重传」：**不会**。源码注释明确说明 reliability 当前不触发任何线路重传，它只是个标记，并可能用于选链路。

#### 4.1.3 源码精读

**协议层定义。** 三个枚举都在 `zenoh-protocol::core`：

- [commons/zenoh-protocol/src/core/mod.rs:508-514](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/mod.rs#L508-L514) 定义 `Reliability`：`BestEffort = 0`、`Reliable = 1`（默认），注释「Messages may be lost.」与「Messages are guaranteed to be delivered.」。
- [commons/zenoh-protocol/src/core/mod.rs:617-629](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/mod.rs#L617-L629) 定义 `CongestionControl`：`Drop = 0`（默认）、`Block = 1`，以及 `unstable` 门控的 `BlockFirst = 2`（只阻塞第一条，其余丢弃）。默认值见 [commons/zenoh-protocol/src/core/mod.rs:631-635](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/mod.rs#L631-L635)：`DEFAULT = Drop`。

**串联判定的关键。** 在 `network/mod.rs` 中，`NetworkMessageExt` 把消息体的 `ext_qos` 扩展字段解析成 reliability / congestion_control / priority：

- [commons/zenoh-protocol/src/network/mod.rs:134-136](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/network/mod.rs#L134-L136)：`is_reliable` 等价于 `reliability() == Reliability::Reliable`。
- [commons/zenoh-protocol/src/network/mod.rs:190-192](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/network/mod.rs#L190-L192)：`is_droppable` 就是上面那个公式——`!is_reliable() || congestion_control() == Drop`。这是整讲最重要的两行。

**公开门面。** [zenoh/src/lib.rs:837-843](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/lib.rs#L837-L843) 把它们 re-export 为 `zenoh::qos::{CongestionControl, Reliability, Priority}`，其中 `Reliability` 带 `#[zenoh_macros::unstable]`（见 4.1.4）。

**传输层落地。** 拥塞时的丢/等逻辑在传输管道里。`push_network_message` 先判定是否 droppable：

- [io/zenoh-transport/src/common/pipeline.rs:883-902](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/common/pipeline.rs#L883-L902)：若 `msg.is_droppable()` 且该优先级队列已被标记为 `congested`，直接 `return Ok(false)` 丢弃；否则按 `wait_before_drop` 设定一个截止时间，超时仍无法入队则丢弃并标记拥塞。而不可丢弃的消息用 `wait_before_close` 作为等待时长——它会更久地阻塞调用者（即 `put` 的发布者），这就是「背压」的来源。

#### 4.1.4 代码实践

**实践目标：** 区分 `CongestionControl::Block` 与 `Drop` 在拥塞下的不同表现。

**操作步骤：**

1. 新建一个二进制（示例代码，非项目自带），订阅端用一个容量很小的 `RingChannel` 故意慢消费，制造本地拥塞；发布端分别用 `Block` 与 `Drop` 高频发布，统计订阅端 1 秒内收到的条数。

```rust
// 示例代码：qos_block_vs_drop.rs（CongestionControl 不需要 unstable）
use std::time::{Duration, Instant};
use zenoh::{qos::CongestionControl, handlers::RingChannel, Config};

#[tokio::main]
async fn main() {
    let session = zenoh::open(Config::default()).await.unwrap();

    // Block：拥塞时阻塞等待（默认 reliability=Reliable，即不可丢弃）
    let p_block = session.declare_publisher("demo/qos/block")
        .congestion_control(CongestionControl::Block).await.unwrap();
    // Drop：拥塞时立即丢弃
    let p_drop = session.declare_publisher("demo/qos/drop")
        .congestion_control(CongestionControl::Drop).await.unwrap();

    // 订阅 block，用极小 RingChannel 制造积压
    let mut s_block = session.declare_subscriber("demo/qos/block")
        .handler(RingChannel::new(1)).await.unwrap();

    let end = Instant::now() + Duration::from_secs(1);
    while Instant::now() < end {
        let _ = p_block.put("x").await;   // 观察这一行的耗时
        let _ = p_drop.put("x").await;
    }
    // 计数：慢消费下 Block 端会因为背压而变慢/不丢，Drop 端会丢
    let mut n = 0;
    while s_block.try_recv().is_ok() { n += 1; }
    println!("block 端 1 秒内实际收到/积压: {n}");
}
```

2. 如需按任务要求对比 **Reliable+Block** 与 **BestEffort+Drop**，则要给 crate 开启 `unstable` feature，并在 builder 上追加 `.reliability(Reliability::Reliable)` / `.reliability(Reliability::BestEffort)`。

**需要观察的现象：**

- `Drop` 端的 `put` 几乎不阻塞、立即返回；订阅端统计数明显小于发送数（被丢）。
- `Block` 端的 `put` 在队列满时会变慢（背压），订阅端不丢但吞吐受限。

**预期结果 / 待本地验证：** 在本机回环、订阅端消费足够快时，两条队列都不拥塞、几乎都**看不到差异**——这是正常的，因为 droppable 的丢弃只在「队列满」时才触发。要稳定观察到差异，必须人为制造拥塞（小 RingChannel、慢消费、或限速/高延迟链路）。具体丢多少条**待本地验证**，取决于机器与负载。

> 说明：本实践没有假装已运行；在没有真实拥塞时不会有丢弃，请按上述方式构造拥塞后再观察。

#### 4.1.5 小练习与答案

**Q1：** 某消息设为 `Reliability::Reliable` + `CongestionControl::Drop`，它是否可丢弃？
**答：** 可丢弃。因为 droppable = `!reliable || (cc==Drop)` = `false || true` = `true`。即「可靠」并不保证发送端不丢，它只在拥塞控制为 Block 时才与「不可丢弃」挂钩。

**Q2：** 为什么说 `Reliability` 在当前 Zenoh 里「不重传」？
**答：** 因为源码注释（[builders/publisher.rs:440-442](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/publisher.rs#L440-L442)）明确说明 reliability 只是线路上的标记、可能用于选链路（如 Reliable 走 TCP、BestEffort 走 UDP），不触发 Zenoh 自身的数据重传。真正的可靠性来自底层可靠链路（TCP/TLS/QUIC）。

---

### 4.2 优先级（Priority）

#### 4.2.1 概念说明

**Priority（优先级）** 决定「谁先发」。Zenoh 维护**多个**发送队列，每个优先级一个；队列按优先级从高到低依次服务。这样高优先级消息（如控制指令）可以「插队」到低优先级数据（如遥测流）前面。

公开 API 暴露 7 档优先级，数值越小优先级越高：`RealTime(1)` > `InteractiveHigh(2)` > `InteractiveLow(3)` > `DataHigh(4)` > `Data(5, 默认)` > `DataLow(6)` > `Background(7)`。

> 还有一档 `Control(0)` 是 **Zenoh 内部专用**（用于协议控制消息），不通过公开 API 暴露，初学者知道有这一档即可，不要去用它。

#### 4.2.2 核心流程

发布一条消息时，QoS（congestion_control + priority + express + reliability）被打包进网络消息的 `ext_qos` 扩展字段随消息一起上线。传输管道的入队逻辑大致为：

```
取 msg.priority()
  → 选第 priority 条发送队列 stage_in[priority]
    → 若该队列拥塞且 msg 可丢弃：丢弃（Ok(false)）
    → 否则：入队，等待 consumer 按优先级从高到低消费
```

consumer 端按优先级**从高到低**轮询各队列（`RealTime` 先于 `Background`），从而高优先级消息总能优先拿到发送机会。

#### 4.2.3 源码精读

**公开 Priority 枚举。** [zenoh/src/api/publisher.rs:523-541](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/publisher.rs#L523-L541)：文档注释明确「Zenoh keeps one transmission queue per Priority… serviced in the order of their assigned Priority (i.e. from RealTime to Background)」；枚举体给出 7 档与 `#[default] Data`。常量 `DEFAULT`/`MIN`/`MAX`/`NUM` 见 [zenoh/src/api/publisher.rs:543-557](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/publisher.rs#L543-L557)。`TryFrom<u8>` 把 1–7 的数字映射到枚举，[zenoh/src/api/publisher.rs:559-587](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/publisher.rs#L559-L587)。

**协议层 Priority（多一档 Control）。** [commons/zenoh-protocol/src/core/mod.rs:330-342](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/core/mod.rs#L330-L342) 多了一个 `Control = 0`。公开枚举到协议枚举的转换用 `unsafe transmute`，因为二者除 Control 外数值一致；正确性由 [zenoh/src/api/publisher.rs:618-630](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/publisher.rs#L618-L630) 的注释与单元测试保证。

**Publisher 持有 QoS 字段。** [zenoh/src/api/publisher.rs:110-125](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/publisher.rs#L110-L125) 的 `Publisher` 结构里有 `congestion_control`、`priority`、`is_express`，以及 `#[cfg(feature="unstable")] reliability`。这就是为什么在 builder 上设一次 QoS，之后该 publisher 每次 `put` 都沿用同一套 QoS（参见《u3-l1》「长生命周期 Publisher」）。

**入队按优先级分道。** [io/zenoh-transport/src/common/pipeline.rs:869-926](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/common/pipeline.rs#L869-L926)：`push_network_message` 中 `if self.stage_in.len() > 1 { 用 msg.priority() 选队列 } else { 单队列，用 DEFAULT }`——也就是说「是否启用优先级分道」取决于传输是否开启了 QoS（见 4.3 节）。

**每个优先级再分 reliable / best_effort。** [io/zenoh-transport/src/common/priority.rs:77-98](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/common/priority.rs#L77-L98) 的 `TransportPriorityTx` 同时持有 `reliable` 和 `best_effort` 两个 `TransportChannelTx`（各自独立序列号）；接收端 [io/zenoh-transport/src/common/priority.rs:100-121](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/common/priority.rs#L100-L121) 同理。所以完整矩阵是「优先级 × 可靠性」两组通道。

#### 4.2.4 代码实践

**实践目标：** 体验优先级插队——同一 publisher 集合里，高优先级消息先到。

**操作步骤：** 写一个示例（示例代码），声明两个 publisher 到不同 key、用不同 `priority`，在一个 loop 里**先发低优先级再发高优先级**，订阅端给两者各起一个计数器，观察谁先被消费完。

```rust
// 示例代码：qos_priority.rs
use zenoh::{qos::Priority, Config};

#[tokio::main]
async fn main() {
    let session = zenoh::open(Config::default()).await.unwrap();
    let p_rt = session.declare_publisher("demo/qos/rt")
        .priority(Priority::RealTime).await.unwrap();      // 高
    let p_bg = session.declare_publisher("demo/qos/bg")
        .priority(Priority::Background).await.unwrap();    // 低

    // 先塞一批低优先级，再插一条高优先级
    for _ in 0..1000 { let _ = p_bg.put("b").await; }
    let _ = p_rt.put("r").await;
}
```

**需要观察的现象：** 在有积压（上一句刚灌了 1000 条背景消息）时，高优先级的 `r` 通常会比队尾的背景消息更早被发出。

**预期结果 / 待本地验证：** 本机回环、无积压时看不出差异（消息瞬间就发完了）；要观察到插队，需要制造队列积压。具体先后顺序**待本地验证**。

#### 4.2.5 小练习与答案

**Q1：** `Priority::RealTime` 与 `Priority::Background` 哪个数值大？哪个优先级高？
**答：** `RealTime=1`、`Background=7`。**数值越小优先级越高**，所以 `RealTime` 优先级最高、`Background` 最低（参见 [publisher.rs:570-575](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/publisher.rs#L570-L575) 的注释）。

**Q2：** 为什么公开 `Priority` 比 `zenoh_protocol::core::Priority` 少一档？
**答：** 协议层多一档 `Control=0`，仅供 Zenoh 内部协议控制消息使用，不对外暴露。公开枚举到协议枚举用 `unsafe transmute` 完成，因两者其余各档数值完全一致（[publisher.rs:618-630](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/publisher.rs#L618-L630)）。

---

### 4.3 QoS 配置与传输层落地

#### 4.3.1 概念说明

除了在每个 builder 上「逐个 publisher 设 QoS」，Zenoh 还提供两种全局层面的机制：

1. **传输层 QoS 开关**：配置 `transport/<unicast|multicast>/qos/enabled`。关掉后传输层**不分优先级队列**，所有消息共用一条 `Data` 队列——省内存、但失去优先级分流。
2. **按 key expression 覆盖（QoS overwrite）**：在配置里针对某些 key expression 强制改写其 congestion_control / priority / express（甚至按对端 zid、网卡、链路协议限定）。适合「应用代码没设、但运维要统一管控」的场景。

#### 4.3.2 核心流程

- **传输层开关**：构建传输管道时，若 `qos.enabled == true`，则按 7 档优先级各建一条入队队列；否则只建一条（index 0），`push_network_message` 走 `else` 分支用 `Priority::DEFAULT`。
- **覆盖机制**：`PublisherBuilder` 在 `wait()` 真正解析前，会调用 `apply_qos_overwrites()`，用配置树里匹配当前 key expression 的覆盖项替换 builder 上的 QoS 字段。

```
declare_publisher(key)
  → PublisherBuilder.wait()
    → apply_qos_overwrites()      // 查配置树，命中则覆盖 cc/priority/express/reliability
      → 构造 Publisher（持有最终 QoS）
```

#### 4.3.3 源码精读

**配置层类型。** [commons/zenoh-config/src/qos.rs:42-51](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-config/src/qos.rs#L42-L51) 的 `PublisherQoSConfig` 把 congestion_control / priority / express / reliability（后两者 `unstable`）都设为 `Option`。`CongestionControlConf`（[53-60](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-config/src/qos.rs#L53-L60)）与 `ReliabilityConf`（[171-176](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-config/src/qos.rs#L171-L176)）是配置用的字符串枚举（serde 为 `snake_case`，如 `"drop"`、`"reliable"`），再 `From` 转成协议层枚举。

**覆盖用的 key 树。** [commons/zenoh-config/src/qos.rs:20-34](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-config/src/qos.rs#L20-L34)：`PublisherQoSConfList` 转成 `KeBoxTree<PublisherQoSConfig>`——一棵以 key expression 为索引的树，运行时按 key 的包含/相交关系查找最匹配的覆盖项（与《u2-l2》Key Expression 的集合语义一脉相承）。覆盖结构体 `QosOverwrites` 见 [220-228](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-config/src/qos.rs#L220-L228)（注意 reliability 当前**不能**被覆盖，源码 TODO 注明它不在 RoutingContext/NetworkMessage 里）。

**builder 应用覆盖。** [zenoh/src/api/builders/publisher.rs:397-426](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/publisher.rs#L397-L426) 的 `apply_qos_overwrites`：取出命中的 `QosOverwrites`，逐字段 `unwrap_or(self.原值)`，再交给 `wait()` 构造 Publisher（[457-481](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/publisher.rs#L457-L481)）。

**传输层 QoS 开关。** [DEFAULT_CONFIG.json5:566-569](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/DEFAULT_CONFIG.json5#L566-L569)：unicast 默认 `qos.enabled: true`；multicast 默认 `false`（注释说为与 Zenoh-Pico 开箱兼容）。队列大小配置注释 [DEFAULT_CONFIG.json5:634](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/DEFAULT_CONFIG.json5#L634) 明确「If qos is false, then only the DATA priority will be allocated.」——这正是 pipeline 里 `stage_in.len() > 1` 判断的依据。

> 顺带提一句 **express**：[builders/publisher.rs:387-394](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/publisher.rs#L387-L394) 注释指出 `express=true` 时消息**不进入批处理（batching）**，立即发出——降低延迟但牺牲吞吐。它和 priority 是正交的另一种 QoS 维度。

#### 4.3.4 代码实践

**实践目标：** 用配置文件关闭传输层 QoS，验证「优先级分流消失」。

**操作步骤：**

1. 准备一个最小配置（示例配置）`qos_off.json5`：

```json5
{
  transport: {
    unicast: { qos: { enabled: false } }
  }
}
```

2. 用此配置打开 session，再运行 4.2 节的优先级示例。
3. 阅读 [DEFAULT_CONFIG.json5:566-569](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/DEFAULT_CONFIG.json5#L566-L569) 与 [pipeline.rs:875-881](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/common/pipeline.rs#L875-L881)，对照源码确认关闭后只剩一条 `Data` 队列。

**需要观察的现象：** 关闭 QoS 后，即便你给 publisher 设了不同 `priority`，传输层也不再为高优先级插队（全部挤在一条队列里，按 FIFO 发送）。

**预期结果 / 待本地验证：** 优先级插队效果消失。是否有可观察的时序差异**待本地验证**（取决于是否构造了积压）。

> 也可尝试 `lowlatency: true`：[DEFAULT_CONFIG.json5:560-565](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/DEFAULT_CONFIG.json5#L560-L565) 注释明确「LowLatency 不保留 QoS 优先级」，且与 `qos` 互斥——启用 lowlatency 必须显式关 qos。

#### 4.3.5 小练习与答案

**Q1：** 把 `transport/unicast/qos/enabled` 设为 `false` 后，给 publisher 设 `Priority::RealTime` 还有用吗？
**答：** 传输层不再按优先级分队列（只分配 `Data` 一档，见 [DEFAULT_CONFIG.json5:634](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/DEFAULT_CONFIG.json5#L634)），所以 `push_network_message` 走单队列分支，`RealTime` 不会带来插队。QoS 字段仍随消息上线，但本地不再据此分流。

**Q2：** 用配置「QoS overwrite」把某 key 的 congestion_control 强制改成 `block`，会覆盖应用代码里设的 `Drop` 吗？
**答：** 会。`PublisherBuilder::wait()` 在构造 Publisher 前先执行 `apply_qos_overwrites()`，命中配置的覆盖项会以 `unwrap_or` 替换原值（[builders/publisher.rs:404-413](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/publisher.rs#L404-L413)）。这是「运维统一管控」的入口。但 reliability 目前不支持被覆盖。

---

## 5. 综合实践

把三个模块串起来：实现一个「双通道遥测」小应用。

- 通道 A（关键告警）：key `plant/alarm`，QoS = `Reliable + Block + Priority::RealTime`，低频但绝不能丢、要抢占发送。
- 通道 B（普通遥测）：key `plant/telemetry`，QoS = `Reliable + Drop + Priority::Data`（默认），高频、允许在拥塞时丢。

要求：

1. 用 `declare_publisher(...).congestion_control(...).priority(...)` 配置两个通道（若要用 `.reliability(...)` 需开 `unstable`）。
2. 订阅端分别订阅 `plant/alarm` 与 `plant/telemetry`，各用一个计数器。
3. 在制造本地拥塞（如小 `RingChannel` 慢消费）的前提下，验证：告警通道不丢、且能插到遥测前面；遥测通道被丢一部分但 `put` 不阻塞。
4. 再用一份配置把 `transport/unicast/qos/enabled` 关掉，重复实验，观察优先级插队消失。
5. 用一句话记录：哪种组合对发布者形成了背压（`put` 变慢）。

**自检要点：** 能说清「为什么 Reliable+Block 才会背压」「为什么关掉 qos 后 priority 失效」，就说明本讲真正掌握了。

## 6. 本讲小结

- Zenoh 的 QoS 由三个正交维度组成：**Reliability**（是否尽量送达，仅线路标记、不重传）、**CongestionControl**（队列满时丢还是等）、**Priority**（谁先发）。
- 是否「可丢弃」由公式 `droppable = !reliable || (cc==Drop)` 决定（[network/mod.rs:190-192](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/network/mod.rs#L190-L192)）；只有 `Reliable + Block` 才不可丢弃、会在拥塞时阻塞发布者形成背压。
- QoS 在 `PublisherBuilder` 上设置（`congestion_control` / `priority` / `express` 默认可用，`reliability` 需 `unstable`），之后该 publisher 每次 `put` 沿用同一套 QoS。
- 传输层为每个 Priority 维护独立发送队列、按高到低服务；每个优先级再分 reliable / best_effort 两套通道与序列号（[priority.rs:77-98](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/io/zenoh-transport/src/common/priority.rs#L77-L98)）。
- `transport/<...>/qos/enabled` 是总开关：关闭后只剩一条 `Data` 队列，priority 分流失效；`lowlatency` 与 `qos` 互斥。
- 配置层的「QoS overwrite」可在 `wait()` 前按 key expression 覆盖 congestion_control / priority / express（reliability 暂不支持），实现运维统一管控。

## 7. 下一步学习建议

- **进入内核**：本讲的丢/等与分道只讲了「发送端 pipeline」。要继续深挖，请读《u9-l2 Transport 层》《u9-l4 批处理、分片与优先级管道》，看 `TransmissionPipelineProducer/Consumer` 如何按优先级轮询、批处理如何与 `is_express` 配合。
- **QoS 在网络消息上的编码**：`ext_qos` 扩展如何被打包进帧，留到《u10-l1 协议消息模型》《u10-l2 Zenoh080 线编码》。
- **应用层对应的「背压」机制**：可对比《u3-l2 Handlers/Channels》里的 `FifoChannel`（阻塞背压）与 `RingChannel`（丢最旧）——那是订阅端取数的取舍，与本讲发送端的 Drop/Block 是镜像关系。
- **更高阶的可靠性**：若需要真正的「不丢+历史回补」，参见《u12-l2 高级 pub/sub（zenoh-ext）》的 `AdvancedPublisher` / `PublicationCache` / 心跳丢包检测与恢复。
