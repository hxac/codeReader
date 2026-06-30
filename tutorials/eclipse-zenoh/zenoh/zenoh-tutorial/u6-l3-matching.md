# Matching：对端匹配感知

## 1. 本讲目标

学完本讲后，你应该能够：

- 说清「matching（匹配感知）」解决什么问题、为什么它能省带宽与 CPU。
- 区分两种 API：一次性查询 `matching_status()` 与长期监听 `matching_listener()`。
- 掌握 `MatchingStatus`、`MatchingListener` 的结构与生命周期（Drop 自动反声明、`Deref` 到 handler）。
- 会写「有订阅者才发布、无订阅者就休眠」的节流发布程序，并用一个订阅者进程的启停来观察匹配状态的变化。

## 2. 前置知识

本讲承接《u3-l1 Pub/Sub 基础》。在继续之前，请确认你已理解：

- **Publisher / Subscriber / Sample**：发布者用 `Session::declare_publisher` 声明，订阅者用 `Session::declare_subscriber` 声明，数据以 `Sample` 为单位传递。
- **Key Expression 的集合语义**：一条 key expression 表示「一批 key 的集合」。消息是否投递，取决于两端 key expression 是否**相交（intersects）**，而不是 IP 地址。记 \(K_p\) 为发布者的 key expression、\(K_s\) 为订阅者的 key expression，则二者匹配当且仅当：

\[ K_p \cap K_s \neq \emptyset \quad\Longleftrightarrow\quad \texttt{intersects}(K_p, K_s) \]

- **Builder 模式与 `.await`/`.wait()`**：`declare_*` 返回的是 builder，必须 resolve 才真正执行（详见《u1-l4》）。
- **Locality（数据局部）**：Zenoh 把「对端」分为三类——本会话内（`SessionLocal`）、远端（`Remote`）、两者皆可（`Any`，默认）。本讲的匹配判断也按这个维度区分。

一个关键直觉：**默认情况下，Publisher 是「盲目发送」的**——它不知道此刻有没有人订阅，只管把数据丢进 Zenoh 网络。这在「没有人听」时会白白消耗 CPU、带宽甚至电池。Matching 提供的，正是让发布/查询方**主动感知对端是否存在**的能力。

## 3. 本讲源码地图

本讲涉及的关键文件：

| 文件 | 作用 |
| --- | --- |
| `zenoh/src/api/matching.rs` | 定义 `MatchingStatus`（匹配状态）、`MatchingStatusType`（匹配目标类型）、`MatchingListener`（监听器）及其生命周期、`MatchingListenerState::is_matching`（本地匹配判定）。 |
| `zenoh/src/api/publisher.rs` | `Publisher::matching_status()` 与 `Publisher::matching_listener()` 两个入口，把 publisher 接入匹配感知。 |
| `zenoh/src/api/querier.rs` | `Querier::matching_status()` 与 `Querier::matching_listener()`，让查询方感知是否存在匹配的 Queryable。 |
| `zenoh/src/api/builders/matching_listener.rs` | `MatchingListenerBuilder`：`.callback()` / `.callback_mut()` / `.with()` / `.background()` 等构造器方法与 resolve 实现。 |
| `zenoh/src/api/session.rs` | 匹配感知的「引擎」：`declare_matches_listener_inner`（注册并立即探测）、`matching_status`（按 Locality 计算）、`update_matching_status`（变化时通知）。 |
| `zenoh/src/api/sample.rs` | `Locality` 枚举定义。 |
| `examples/examples/z_pub.rs` | 官方示例，演示了 `--add-matching_listener` 开关如何用 `.callback(...).background()` 接入匹配监听。 |

## 4. 核心概念与源码讲解

本讲按三个最小模块展开：**MatchingStatus**（匹配状态）、**MatchingListener**（监听匹配变化）、**节流发布**（把感知用在发送决策上）。

### 4.1 MatchingStatus：匹配状态

#### 4.1.1 概念说明

`MatchingStatus` 回答一个最朴素的二元问题：**此刻，网络里有没有与我的 key expression 匹配的对端实体？**

- 对 **Publisher** 而言，「对端实体」是 **Subscriber**。
- 对 **Querier** 而言，「对端实体」是 **Queryable**。

答案只有「有 / 没有」两种，所以 `MatchingStatus` 内部就一个布尔字段。它是最轻量的匹配感知形式——一次查询、一个布尔、不需要维护任何监听状态。

注意区分两种「目标实体类型」，由内部枚举 `MatchingStatusType` 表达：

```rust
#[derive(Debug, Copy, Clone, PartialEq)]
pub(crate) enum MatchingStatusType {
    Subscribers,
    Queryables(bool),   // bool = complete，与 Querier 的 QueryTarget 相关
}
```

- `Subscribers`：Publisher 关心的是「有没有订阅我的人」。
- `Queryables(bool)`：Querier 关心的是「有没有能回答我的 Queryable」；其中的 `bool`（complete）会影响判定方式（见 4.1.3）。

> 说明：`MatchingStatusType` 是 `pub(crate)` 内部类型，用户代码不会直接接触它，但理解它有助于看懂 publisher 与 querier 两条匹配链路的差异。

#### 4.1.2 核心流程

一次性查询 `matching_status()` 的流程：

1. 调用 `Publisher::matching_status()`（或 `Querier::matching_status()`），返回一个 `impl Resolve<ZResult<MatchingStatus>>`。
2. `.await`（或 `.wait()`）resolve 后得到 `ZResult<MatchingStatus>`。
3. 用 `.matching()` 取出布尔结果，决定后续动作。

它本质是「**拉（pull）**」模型：你想知道状态时，主动问一次。

#### 4.1.3 源码精读

**MatchingStatus 本体**——只有一个 `pub(crate)` 的布尔字段，对外只暴露 `matching()` 取值器，保证用户无法构造出语义错乱的状态：

[zenoh/src/api/matching.rs:47-50](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/matching.rs#L47-L50) 定义结构；[zenoh/src/api/matching.rs:83-86](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/matching.rs#L83-L86) 是取值方法 `matching()`。这两段代码说明：匹配状态就是一个布尔，`matching()` 把它交还给用户。

**Publisher 的一次性查询入口**——注意它传入的是 `MatchingStatusType::Subscribers`，所以 publisher 永远只关心订阅者：

[zenoh/src/api/publisher.rs:321-329](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/publisher.rs#L321-L329) 中，`matching_status()` 把 key expression、`destination`（Locality）和 `MatchingStatusType::Subscribers` 交给 `session.matching_status(...)` 计算结果。

**Querier 的对应入口**——传入的是 `MatchingStatusType::Queryables(self.target == QueryTarget::AllComplete)`，bool 由 Querier 的 `target` 决定：

[zenoh/src/api/querier.rs:226-234](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/querier.rs#L226-L234) 说明：Querier 的匹配目标类型随 `QueryTarget` 动态确定。

**真正的匹配计算**——在 `session.rs` 中按 `Locality` 分派。本会话内的匹配（`matching_status_local`）用最直白的「遍历 + intersects」实现，是理解整个机制的钥匙：

[zenoh/src/api/session.rs:2158-2179](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L2158-L2179) 这段代码做了三件事：
- `Subscribers`：遍历本地所有 Subscriber，只要有一个 `key_expr.intersects(查询的 key_expr)` 即为匹配；
- `Queryables(false)`：遍历本地 Queryable，用 `intersects` 判断；
- `Queryables(true)`（complete 模式）：要求 Queryable 标记了 `complete` **且**它的 key expression **包含（includes）** 查询的 key expression。

> **为什么 complete 用 `includes` 而非 `intersects`？** 一个声明了 `complete` 的 Queryable 承诺「对我声明的 key 范围内的**所有** key，我都能给出完整答案」。因此只有当它的范围**完全覆盖** Querier 的 key expression 时，才算是「能完整回答我」的对端。这是 `includes`（包含）而非 `intersects`（相交）的语义根源。complete 的细节属于《u4》查询链路，本讲只需记住结论。

**按 Locality 分派的总入口**：

[zenoh/src/api/session.rs:2195-2213](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L2195-L2213) 中：`SessionLocal` 只查本地；`Remote` 委托给 runtime（远端匹配要走路由表，属《u7/u8》内核内容）；`Any`（默认）先查本地，本地不匹配再查远端。

#### 4.1.4 代码实践

**实践目标**：用一次性查询，验证「没有订阅者时返回 false、有订阅者时返回 true」。

**操作步骤**（示例代码，非项目原有代码）：

```rust
// 示例代码：matching_status 一次性查询
#[tokio::main]
async fn main() {
    let session = zenoh::open(zenoh::Config::default()).await.unwrap();
    let publisher = session.declare_publisher("demo/matching/status").await.unwrap();

    // 1) 此时还没有订阅者
    let s = publisher.matching_status().await.unwrap();
    println!("注册订阅者之前：matching = {}", s.matching()); // 预期 false

    // 2) 在同一会话里声明一个订阅者（key 相交）
    let _sub = session.declare_subscriber("demo/matching/*").await.unwrap();

    // 3) 再次查询
    let s = publisher.matching_status().await.unwrap();
    println!("注册订阅者之后：matching = {}", s.matching()); // 预期 true
}
```

**需要观察的现象**：
- 第一次打印应为 `matching = false`。
- 声明订阅者后第二次打印应为 `matching = true`（因为 `demo/matching/status` 与 `demo/matching/*` 相交）。

**预期结果 / 待本地验证**：上述布尔翻转依赖 Zenoh 的声明传播时序；在单进程内通常立即可见。若跨进程，需给声明留一点传播时间，**具体时序待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：如果把上面订阅者的 key 改成 `other/topic`（与 `demo/matching/status` 不相交），第二次 `matching()` 会是什么？为什么？

> **答案**：`false`。因为匹配只看 key expression 的相交关系，`other/topic` 与 `demo/matching/status` 没有任何公共 key，故不算匹配。

**练习 2**：`matching_status()` 是「推」还是「拉」？它适合什么场景？

> **答案**：是「拉（pull）」。适合**低频、按需**地感知状态，例如启动时自检、或长间隔周期任务前判断一次。若需要实时响应匹配变化，应改用 4.2 的 `matching_listener()`。

---

### 4.2 MatchingListener：监听匹配变化

#### 4.2.1 概念说明

一次性查询有两个缺点：要么你得反复轮询（浪费），要么你会在两次轮询之间错过状态变化。`MatchingListener` 提供「**推（push）**」模型——**当匹配状态发生变化时，Zenoh 主动通知你**。

它的典型用法是：发布者注册一个监听器，每当「从无到有」或「从有到无」匹配订阅者时，回调被触发一次。于是发布者可以做到「订阅者来了我才开始发，订阅者都走了我就停下来」。

`MatchingListener<Handler>` 与 `Subscriber<Handler>` 高度对称：
- 都 `Deref` 到 handler（默认 `DefaultHandler = FifoChannel`，提供 `recv_async()`）。
- 都在 `Drop` 时自动反声明（undeclare）。
- 都支持 `.callback()` / `.with(handler)` 两种取数姿势。

#### 4.2.2 核心流程

注册与通知的完整链路：

1. `publisher.matching_listener()` 返回 `MatchingListenerBuilder`。
2. 选择取数姿势：`.callback(f)`（闭包）、`.with(handler)`（通道），或直接 resolve 用默认 `FifoChannel`。
3. `.await` resolve，得到 `MatchingListener<Handler>`；注册时 Zenoh 会**立即探测一次当前状态**——若注册时已匹配，会立刻回调一次 `MatchingStatus { matching: true }`，避免错过「早已存在」的匹配。
4. 之后，每当有 Subscriber 声明/反声明引起「该 publisher 是否有匹配订阅者」的布尔翻转，回调被触发。
5. `MatchingListener` 被 drop 时自动反声明；也可手动 `undeclare()`。

通知是**边沿触发（on transition）**的：只在布尔值真的改变时才回调。连续多个订阅者加入（布尔一直是 true）只会产生一次 true 回调，不会重复打扰你。

#### 4.2.3 源码精读

**MatchingListener 本体**——内部持有 `MatchingListenerInner`、handler 与 `SyncGroup`（用于 `wait_callbacks`）：

[zenoh/src/api/matching.rs:156-160](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/matching.rs#L156-L160) 是结构定义。它泛型于 `Handler`，与 `Subscriber` 完全同构。

**Deref 到 handler**——这就是为什么默认 handler 下可以直接 `listener.recv_async()`：

[zenoh/src/api/matching.rs:244-256](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/matching.rs#L244-L256) 把 `&Handler` / `&mut Handler` 透明暴露出来。

**Drop 自动反声明**——和 Publisher/Subscriber 一样，靠 `undeclare_on_drop` 标志：

[zenoh/src/api/matching.rs:223-231](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/matching.rs#L223-L231) 说明：监听器离开作用域即自动从 session 注销，无需手动清理。

**Publisher 的监听器入口**——返回 builder，目标类型固定为 `Subscribers`：

[zenoh/src/api/publisher.rs:353-363](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/publisher.rs#L353-L363) 中，`matching_listener()` 把 publisher 的 key expression、`destination`、`matching_listeners` 集合等塞进 `MatchingListenerBuilder`。

**构造器方法**——`.callback()` / `.callback_mut()` / `.with()`：

[zenoh/src/api/builders/matching_listener.rs:74-80](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/matching_listener.rs#L74-L80) 是 `.callback(F)`，它等价于 `.with(Callback::from(f))`；[zenoh/src/api/builders/matching_listener.rs:133-148](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/matching_listener.rs#L133-L148) 是通用的 `.with(Handler)`，把任意 `IntoHandler<MatchingStatus>` 接入。

**后台监听器 `.background()`**——不返回 listener 对象，监听器随 publisher 的生命周期存在（publisher undeclare 时一并清理）。官方示例 `z_pub.rs` 正是这种用法：

[zenoh/src/api/builders/matching_listener.rs:177-188](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/matching_listener.rs#L177-L188) 把 builder 切到 `BACKGROUND=true` 变体；[examples/examples/z_pub.rs:33-46](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_pub.rs#L33-L46) 是项目里的真实用法：`.matching_listener().callback(|matching_status| { ... }).background().await`。

**注册时的立即探测**——这是「不会错过已存在匹配」的关键：

[zenoh/src/api/session.rs:2140-2154](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L2140-L2154) 说明：注册完成后，立刻调用 `matching_status(...)` 探测一次；若已经匹配，就把 `current` 置 true 并**立即**回调一次 `MatchingStatus { matching: true }`。所以监听器一装上就能反映「现在」的状态，而非只能等下一次变化。

**变化通知引擎 `update_matching_status`**——每当有 Subscriber/Queryable 声明或反声明，session 都会调用它。它对每个相关监听器 spawn 一个任务（在 `ZRuntime::Net` 上），**只在布尔真正翻转时**才回调，并在回调前再用完整 `matching_status` 复核一次以避免假通知：

[zenoh/src/api/session.rs:2215-2259](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L2215-L2259)。注意两点工程细节：① 注释明确写道「不能持着 session 锁去调 tables（matching_status()）」，所以用 spawn 把锁释放掉；② 判定 `if *current != status_value` 实现了「边沿触发」。

**本地匹配判定 `is_matching`**——`update_matching_status` 用它快速过滤「这次声明事件跟我这个监听器有没有关系」，是 4.1.3 中 `matching_status_local` 的「事件版」镜像：

[zenoh/src/api/matching.rs:97-116](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/matching.rs#L97-L116)。它同样对 `Queryables(true)` 用 `includes`、其余用 `intersects`。

**触发点示例**——声明一个 Subscriber 时，session 会以 `MatchingStatusType::Subscribers, true` 调用 `update_matching_status`，从而把「新增订阅者」的事件广播给所有 publisher 的匹配监听器：

[zenoh/src/api/session.rs:1771](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L1771) 就是这个触发点（紧接在向网络发送 `DeclareSubscriber` 之后）。

#### 4.2.4 代码实践

**实践目标**：用 `matching_listener` 的默认 handler（`FifoChannel`）+ `recv_async`，观察「订阅者加入 → true」「订阅者退出 → false」两次翻转。

**操作步骤**（示例代码，非项目原有代码）：

```rust
// 示例代码：用 recv_async 接收匹配变化
use std::time::Duration;

#[tokio::main]
async fn main() {
    let session = zenoh::open(zenoh::Config::default()).await.unwrap();
    let publisher = session.declare_publisher("demo/matching/listen").await.unwrap();

    // 默认 handler = FifoChannel，可 recv_async
    let listener = publisher.matching_listener().await.unwrap();

    // 同会话内启停一个订阅者，制造两次状态翻转
    {
        let _sub = session.declare_subscriber("demo/matching/listen").await.unwrap();
        // 此时应有 true 事件（注册时已匹配）
        if let Ok(s) = listener.recv_async().await {
            println!("收到：matching = {}", s.matching()); // 预期 true
        }
        tokio::time::sleep(Duration::from_millis(100)).await;
        // _sub 在此 drop → 订阅者消失
    }

    if let Ok(s) = listener.recv_async().await {
        println!("收到：matching = {}", s.matching()); // 预期 false
    }
}
```

**需要观察的现象**：
- 第一条打印 `matching = true`（订阅者出现）。
- 第二条打印 `matching = false`（订阅者消失）。

**预期结果 / 待本地验证**：在单进程内两个事件都应出现；跨进程时事件传播有延迟，**具体时序待本地验证**。若多声明一个不相交的订阅者，不应产生额外回调（边沿触发）。

#### 4.2.5 小练习与答案

**练习 1**：`.background()` 与不带 `.background()` 的 `matching_listener()` 有何区别？

> **答案**：不带 `.background()` resolve 后返回 `MatchingListener` 对象，你必须持有它（否则它 drop 就自动反声明了）；`.background()` 不返回 listener 对象，监听器常驻后台，生命周期与 publisher 绑定，publisher 反声明时一并清理。适合「装上就不用管」的场景。

**练习 2**：为什么 `update_matching_status` 要 spawn 到 `ZRuntime::Net` 上，而不是直接在持有 session 锁时回调？

> **答案**：因为回调内部需要再调用 `matching_status()`，后者可能访问路由表（tables）；持着 session 锁去访问 tables 会造成锁依赖/死锁风险。spawn 一次任务可以在释放 session 锁后再做完整状态计算与回调。

---

### 4.3 节流发布

#### 4.3.1 概念说明

把 4.1、4.2 的能力用起来，就得到本讲的实际价值——**节流发布（throttling / on-demand publishing）**：只在确实有订阅者时才发送数据，没有订阅者就休眠。

为什么要这么做？典型场景：
- **省带宽 / 省 CPU**：传感器以 100Hz 采集，但当下没有任何消费者，盲目发布只是空转。
- **省电**：边缘设备用电池供电，「无人监听就停发」能显著延长续航。
- **避免无效拥塞**：在 `Reliable + Block` 的 QoS 下，无订阅者却猛发会徒增排队与丢弃压力（QoS 见《u3-l3》）。

实现上有两种姿势，对应 4.1（拉）和 4.2（推）：

| 姿势 | 实现 | 优点 | 缺点 |
| --- | --- | --- | --- |
| 轮询式 | 发布循环里每次先 `matching_status().await` 再决定发 | 简单直接 | 每次发布多一次异步查询开销 |
| 监听式 | 用 `matching_listener` 维护一个共享布尔，发布循环只读布尔 | 高效、零额外 await | 需要一个共享状态（如 `Arc<AtomicBool>`） |

生产环境通常用**监听式**：匹配状态由回调维护进一个 `Arc<AtomicBool>`，发布热循环只做一次原子读，几乎零成本。

#### 4.3.2 核心流程

监听式节流发布的流程：

1. 准备一个共享状态 `should_send = Arc::new(AtomicBool::new(false))`。
2. `publisher.matching_listener().callback(move |status| { should_send.store(status.matching(), ...) }).background().await`，把匹配状态实时写进布尔。
3. 发布热循环：`if should_send.load(...) { publisher.put(...).await } else { /* 休眠 / 跳过 */ }`。
4. 订阅者来 → 回调把布尔置 true → 发布开始；订阅者走 → 布尔置 false → 发布停止。

> 注意：第 2 步用 `.background()` 让监听器常驻；若不用 `.background()`，则必须把返回的 `MatchingListener` 一直持有到程序结束。

#### 4.3.3 源码精读

节流发布本身是「应用层模式」，项目里没有专门的内建开关；但 `z_pub.rs` 的 `--add_matching_listener` 演示了**接入监听器**这一半，是节流发布的基础：

[examples/examples/z_pub.rs:33-46](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_pub.rs#L33-L46) 这段在发布循环之前注册了一个后台匹配监听器，回调里只打印「有 / 无匹配订阅者」。

而真正的「按匹配决定是否发」要结合 `Publisher::put`：

[zenoh/src/api/publisher.rs:257-273](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/publisher.rs#L257-L273) 是 `Publisher::put`，返回 `PublisherPutBuilder`，再 `.await` 才真正发出。节流发布就是「在调用 `put` 之前先用匹配状态把守一道关」。

#### 4.3.4 代码实践

**实践目标**：写一个 publisher，**匹配到至少一个 subscriber 才周期发布，无匹配时休眠**；用一个独立的 subscriber 进程的启停来观察 publisher 的匹配状态变化日志。这是本讲规格要求的实践任务。

**操作步骤**：

1. 编写下面这个 publisher 程序（示例代码，非项目原有代码）：

```rust
// 示例代码：节流发布 publisher（文件名可存为 z_pub_throttled.rs）
use std::sync::atomic::{AtomicBool, Ordering};
use std::sync::Arc;
use std::time::Duration;
use zenoh::Config;

#[tokio::main]
async fn main() {
    zenoh::init_log_from_env_or("info");
    let session = zenoh::open(Config::default()).await.unwrap();
    let publisher = session.declare_publisher("demo/throttled/temp").await.unwrap();

    // 共享匹配状态，由监听器回调维护
    let matched = Arc::new(AtomicBool::new(false));
    let matched_cb = matched.clone();
    publisher
        .matching_listener()
        .callback(move |status| {
            let now = status.matching();
            matched_cb.store(now, Ordering::SeqCst);
            if now {
                println!("[matching] 有订阅者，开始发布");
            } else {
                println!("[matching] 无订阅者，停止发布");
            }
        })
        .background()   // 监听器常驻，随 publisher 生命周期
        .await
        .unwrap();

    println!("Publisher 已就绪，等待订阅者……按 CTRL-C 退出");
    loop {
        if matched.load(Ordering::SeqCst) {
            let v = 36.5f64; // 这里用固定值代替真实采集
            println!("  -> 发布 demo/throttled/temp = {v}");
            publisher.put(v.to_string()).await.unwrap();
        } else {
            // 无匹配时低频休眠，避免空转
        }
        tokio::time::sleep(Duration::from_secs(1)).await;
    }
}
```

2. 编写一个最小 subscriber 程序（示例代码）：

```rust
// 示例代码：配合节流发布的 subscriber
#[tokio::main]
async fn main() {
    let session = zenoh::open(zenoh::Config::default()).await.unwrap();
    let sub = session.declare_subscriber("demo/throttled/temp").await.unwrap();
    println!("Subscriber 已就绪，按 CTRL-C 退出");
    while let Ok(s) = sub.recv_async().await {
        println!("  收到：{}", String::from_utf8_lossy(s.payload.to_bytes().as_ref()));
    }
}
```

3. 两个终端分别运行（如果走单机互联，确保 scouting/连接配置能让两端发现彼此）。

**需要观察的现象**：
- publisher 启动后，先打印 `[matching] 无订阅者，停止发布`（因为还没人订阅），且**不**打印 `-> 发布` 行。
- 启动 subscriber 后，publisher 打印 `[matching] 有订阅者，开始发布`，并开始周期打印 `-> 发布 demo/throttled/temp = 36.5`；subscriber 收到对应数据。
- 关闭 subscriber（CTRL-C）后，publisher 打印 `[matching] 无订阅者，停止发布`，并停止 `-> 发布` 行。

**预期结果 / 待本地验证**：上述「来订阅者即开始发、走订阅者即停发」是节流发布的核心行为。跨进程时，订阅者的声明/反声明需要经网络传播，状态翻转的延迟取决于 scouting 与连接配置，**翻转的具体时序待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：把节流发布从「监听式」改成「轮询式」，发布循环里每次先 `matching_status().await`。这样做有什么代价？

> **答案**：每次发布都多一次异步的 `matching_status` 查询（本地匹配要读 session 状态、远端匹配要走 runtime/路由表），在高频发布时会带来明显开销，且把「发布」与「查状态」耦合在一个 await 链上。监听式用一个原子布尔把二者解耦，热循环几乎零成本。

**练习 2**：在节流发布中，若把 publisher 的 QoS 设成 `Reliable + Block`（见《u3-l3》），「无订阅者时停止发布」相比「继续发布」还有什么额外好处？

> **答案**：`Reliable + Block` 下，发送队列拥塞会阻塞发布者形成背压。无订阅者时仍猛发会让数据在路径上空排队、占用缓冲与带宽；停止发布则从源头消除这种无效拥塞，既省资源也避免发布者被背压卡住。

## 5. 综合实践

把本讲三个最小模块串起来，完成下面这个「**带匹配感知的温度网关**」小任务：

**需求**：
- 一个 publisher 进程，周期产生温度值（可用随机数模拟），但**只有在存在匹配订阅者时才真正发送**。
- 用 `matching_listener` 维护匹配状态；状态翻转时分别打印 `[GATEWAY] 上行通道：开启 / 关闭`。
- 额外要求：用一个 `Arc<AtomicU64>` 统计「因为无订阅者而被跳过的发布次数」，subscriber 出现并停止后，打印该计数，验证节流确实生效。

**验收要点**：
1. 无 subscriber 运行 10 秒，publisher 不应发出任何 `put`，跳过计数应持续增长。
2. 启动 subscriber 后，`put` 开始发生，subscriber 能收到温度。
3. 关闭 subscriber 后，`put` 再次停止，跳过计数恢复增长。

**提示**：
- 用 4.3.4 的代码骨架，把「固定值」换成随机温度、把「直接发布」包进 `if matched.load(...)` 分支，并在 `else` 分支里对跳过计数 `fetch_add(1)`。
- 想进一步：把订阅者也改成两个不同 key（如 `demo/gateway/temp` 与 `demo/gateway/humidity`），用 publisher 的 `**` 通配观察「只要有任意一个相交订阅者就发」的行为——这正好呼应《u2-l2》的 intersects 语义。

**预期结果 / 待本地验证**：节流与计数行为在单进程内可稳定复现；跨进程的翻转时序受网络传播影响，**待本地验证**。

## 6. 本讲小结

- **Matching 解决「对端是否存在」的感知问题**：让 Publisher/Querier 知道此刻有没有匹配的 Subscriber/Queryable，从而按需发送、省带宽省电。
- **两种 API**：`matching_status()` 是一次性的「拉」，`matching_listener()` 是长期「推」。监听器在注册时会立即探测当前状态，之后只在布尔翻转时回调（边沿触发）。
- **`MatchingStatus` 就是一个布尔**，由 `matching()` 取出；背后按 `MatchingStatusType`（Subscribers / Queryables）和 `Locality`（SessionLocal / Remote / Any）分派计算。
- **`MatchingListener` 与 `Subscriber` 同构**：`Deref` 到 handler（默认 `FifoChannel`）、`Drop` 自动反声明、支持 `.callback()` / `.with()` / `.background()`。
- **节流发布是本讲的落地价值**：用监听器维护一个共享布尔，发布热循环只读布尔决定是否发，可做到「有订阅者才发、无则休眠」，无订阅者时空转与无效拥塞都被消除。
- **complete Queryable 用 `includes` 而非 `intersects`** 判定匹配，源于「complete 承诺覆盖范围内所有 key」的语义。

## 7. 下一步学习建议

- **继续匹配的另一半——Querier 侧**：本讲已带出 `Querier::matching_listener`，建议结合《u4》练习「有 Queryable 才发查询」的节流查询。
- **下探匹配的远端实现**：`matching_status_remote` 委托给了 runtime / 路由表（`runtime.matching_status_remote(...)`）。要搞清「远端匹配到底怎么算出来的」，请进入《u7-l1 Session 内部与 Runtime》和《u8 路由（HAT 拓扑）》，那里会讲 Face / Tables / 兴趣（interest）如何驱动匹配。
- **与 Liveliness 对比**：《u6-l2 Liveliness》是「token 级的存在感」，本讲是「实体（subscriber/queryable）级的存在感」，二者机制不同但目标相似，值得对照阅读。
- **QoS 与节流的配合**：回顾《u3-l3 QoS》，思考「Reliable+Block + 节流发布」如何共同构成一个既不丢关键数据、又不空转的发送策略。
