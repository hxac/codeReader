# Get 与 Querier：请求数据

## 1. 本讲目标

《u4-l1》讲的是查询/应答（Query/Reply）的**供给侧**——如何用 `Queryable` 接收查询并 `reply`。本讲翻到另一面，讲**请求侧**：作为发起方，如何「问一次」、如何「反复问」、以及如何把网络上多个 `Queryable` 返回的多条 `Reply` 收齐并合并。

学完本讲你应当能够：

- 用 `Session::get` 发起一次性查询，并通过 `Reply::result()` 区分成功应答与错误应答。
- 理解 `Querier` 与 `get` 的差异：声明式、可复用、能在网络上声明持久「兴趣」以获得优化。
- 掌握 `QueryTarget`（问谁）与 `QueryConsolidation`（结果怎么合并）两个正交维度，特别是默认合并策略为何会让「同一 key 上的多个 Queryable」只露出一条应答，以及如何关掉它。

## 2. 前置知识

本讲默认你已掌握：

- **Session 与 Key Expression**（《u2-l1》《u2-l2》）：`zenoh::open` 得到 `Session`，匹配由 key expression 的集合关系决定。
- **Pub/Sub 与 Sample**（《u3-l1》）：`Sample` 是 Zenoh 的数据单元，含 `key_expr`、`payload`、`kind`、`encoding`、`timestamp`。
- **builder 模式**（《u1-l4》《u3-l1》）：所有「创建实体」都返回 builder，必须 `.await` 或 `.wait()` 才真正执行（`Resolvable`/`Wait`/`Resolve` 三 trait）。
- **Query/Reply 供给侧**（《u4-l1`）：`Queryable`、`Query`、`reply`/`reply_del`/`reply_err`、`Reply = Result<Sample, ReplyError>`、`ReplyKeyExpr`、`complete`、`Locality`。

再补一个本讲要反复用到的小概念：**pull 与 push**。Pub/Sub 是 push（发布者主动推给订阅者），而 Query/Reply 是 pull（请求方主动拉一次、应答方答一次）。所以查询天然适合「读当前状态」「读历史」「读持久化数据」——Zenoh 的存储后端就是用 Queryable 形态暴露数据的。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `zenoh/src/api/session.rs` | `Session` 的核心实现，包含 `get`、`declare_querier`、`declare_querier_inner`，以及真正干活的 `query` 与收应答的 `send_response`/`send_response_final`。 |
| `zenoh/src/api/querier.rs` | `Querier` 类型本身：`get`、`undeclare`、`matching_status`、`Drop` 自动反声明。 |
| `zenoh/src/api/builders/query.rs` | `SessionGetBuilder`——`session.get(...)` 返回的 builder。 |
| `zenoh/src/api/builders/querier.rs` | `QuerierBuilder`（声明 Querier）与 `QuerierGetBuilder`（Querier 上发一次 get）。 |
| `zenoh/src/api/query.rs` | `Reply`、`ReplyError`、`QueryConsolidation`、`ReplyKeyExpr`，并 re-export `QueryTarget` 与 `ConsolidationMode`。 |
| `commons/zenoh-protocol/src/network/request.rs` | 协议层 `QueryTarget` 枚举定义。 |
| `commons/zenoh-protocol/src/zenoh/query.rs` | 协议层 `ConsolidationMode` 枚举定义。 |
| `examples/examples/z_get.rs` | 一次性查询示例。 |
| `examples/examples/z_querier.rs` | 声明式 Querier 周期查询示例。 |

## 4. 核心概念与源码讲解

本讲三个最小模块：**Session::get**（一次性请求）、**Querier**（可复用查询器）、**Reply 与结果合并**（应答处理与 consolidation）。

### 4.1 Session::get：一次性查询

#### 4.1.1 概念说明

`Session::get(selector)` 是最直接的「问一次」入口：你给它一个 `Selector`（key expression + 可选查询参数，详见《u4-l3》），它就把查询发到网络上所有匹配的 `Queryable`，把应答汇聚成一个 handler 返回给你。

它适合**临时性、低频**的查询——查完就完，不保留任何状态。如果你只是偶尔读一次状态，用 `get` 最简单。

#### 4.1.2 核心流程

```text
session.get(selector)          // 返回 SessionGetBuilder（默认 handler = FifoChannel）
       .target(...)            // 可选：问谁（BestMatching / All / AllComplete）
       .consolidation(...)     // 可选：结果怎么合并（默认 AUTO → Latest）
       .timeout(...)           // 可选：超时
       .await                  // resolve 成一个 handler（可 recv_async 取 Reply）
       ↓
Session::query(...)            // 真正干活：分配 qid、注册 QueryState、发 Request、起超时任务
```

关键点：`get` 本身**不发查询**，它只是构造 builder；查询是在 builder `.await`/`.wait()` 时，由 `SessionGetBuilder::wait` 调用 `Session::query` 才发出的（见 4.1.3）。这和《u3-l1》里「`declare_*`/`put` 都是 builder，必须 resolve」完全一致。

#### 4.1.3 源码精读

`Session::get` 只负责构造 builder，并填上一组默认值：

[zenoh/src/api/session.rs:1400-1426](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L1400-L1426) —— 注意默认 `target = QueryTarget::DEFAULT`（即 `BestMatching`）、`consolidation = QueryConsolidation::DEFAULT`（即 `AUTO`）、`timeout` 取自 `queries_default_timeout()`。

`SessionGetBuilder::wait` 才是请求的真正起点，它把 builder 字段拆开，调用 `Session::query`：

[zenoh/src/api/builders/query.rs:380-411](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/query.rs#L380-L411) —— 注意它给 `querier_id` 传的是 `None`（一次性查询不归属任何 Querier），最后返回 `receiver`（即 handler 的取数端）。

`Session::query` 做了请求侧最核心的三件事：

[zenoh/src/api/session.rs:2646-2651](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L2646-L2651) —— 把 `AUTO` 合并策略解析成具体的 `Latest`（除非查询参数里带 `time_range`，此时为 `None`）。

[zenoh/src/api/session.rs:2669-2700](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L2669-L2700) —— 计算期望收到的「结束标记」数 `nb_final`（`Locality::Any` 时为 2：本地路径 + 远程路径各一个），并 spawn 一个超时任务：到点后把已缓存的应答冲刷出去，再补一条 `ReplyError("Timeout")`。

[zenoh/src/api/session.rs:2717-2746](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L2717-L2746) —— 把查询编码成协议层的 `Request{ .. RequestBody::Query(..) }` 经 `primitives.send_request` 发往网络。

`z_get.rs` 示例展示了完整的取数姿势——拿到 builder 后 `.target().timeout()`（可选 `.payload()`），再 `while let Ok(reply) = replies.recv_async().await` 循环取应答：

[examples/examples/z_get.rs:34-69](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_get.rs#L34-L69) —— 注意 `recv_async` 返回 `Err` 时循环自然退出（handler 的 sender 在查询结束时被丢弃，见 4.3.2）。

#### 4.1.4 代码实践

**目标**：用 `z_get` 配合《u4-l1》的 `z_queryable` 跑通一次「问—答」，并观察默认 target 的行为。

**操作步骤**：

1. 终端 A（应答方）：
   ```bash
   cargo run --example z_queryable -- -k demo/example/zenoh-rs-queryable -p "Hello-from-Queryable"
   ```
2. 终端 B（请求方）：
   ```bash
   cargo run --example z_get -- -s "demo/example/zenoh-rs-queryable"
   ```

**需要观察的现象**：终端 B 打印一行 `>> Received ('demo/example/zenoh-rs-queryable': 'Hello-from-Queryable')`，随后 `recv_async` 循环退出、程序结束。

**预期结果**：默认 `target = BestMatching`，Zenoh 会挑一个最佳匹配的 Queryable 来回答；只有一个 Queryable 时必然只回一条。若关掉终端 A 再查询，应观察超时后打印 `>> Received (ERROR: 'Timeout')`（默认超时 10 秒，可用 `-o` 改短）。

> 待本地验证：超时错误的具体文案与是否随版本变化。

#### 4.1.5 小练习与答案

**练习 1**：`session.get(selector)` 调用后、还没有 `.await` 时，查询发出去了吗？

> **答**：没有。`get` 只返回 `SessionGetBuilder`，查询是在 builder 被 `.await`/`.wait()` resolve 时、由 `SessionGetBuilder::wait` 调用 `Session::query` 才发出的。忘记 resolve 只会触发 `#[must_use]` 警告。

**练习 2**：`Session::get` 与 `Session::put`（《u3-l1》）在「是否声明实体」上有何相似处？

> **答**：两者都是「一次性快捷方法」，内部都不保留长生命周期实体；`put` 相当于临时声明 Publisher 发一次，`get` 相当于发起一次不归属任何 Querier 的查询（`querier_id = None`）。

### 4.2 Querier：声明式、可复用的查询器

#### 4.2.1 概念说明

如果你要**对同一个 key expression 反复发查询**（比如每秒轮询一次传感器值），每次都调 `session.get` 会重复做「解析 selector、构造 builder、把 key 编码上线」的工作。`Querier` 就是为此而生：先 `session.declare_querier(key_expr)` 声明一次，把 key expression 固定下来并通知网络「我对这个 key 持续感兴趣」，之后每次 `querier.get()` 只需带上可变的查询参数（payload/parameters）即可。

`Querier` 的额外好处：

- **网络优化**：声明时会把 key expression 注册为持久「兴趣」（interest），路由层可据此优化转发，并在有新 Queryable 上线时及时感知。
- **匹配感知**：可直接 `querier.matching_status()` / `querier.matching_listener()`，知道当前有没有匹配的 Queryable，从而决定要不要真去查（节流，详见《u6-l3》）。
- **自动反声明**：和 `Publisher`/`Subscriber` 一样，`Querier` 在 `Drop` 时自动 undeclare。

#### 4.2.2 核心流程

```text
声明阶段：
session.declare_querier(key_expr)        // 返回 QuerierBuilder
        .target(...).consolidation(...).timeout(...)
        .await                            // resolve 成 Querier
        ↓ QuerierBuilder::wait
        declare_keyexpr(...)              // 把 key 声明进 Session（可能拿到优化形式）
        declare_querier_inner(...)        // 注册本地状态 + 向网络发 Interest（CurrentFuture）
        → Querier

查询阶段（可反复）：
querier.get()                             // 返回 QuerierGetBuilder（无参，因为 key 已固定）
       .payload(...).parameters(...)      // 只需带本次可变内容
       .await                             // resolve 成 handler
```

#### 4.2.3 源码精读

`Session::declare_querier` 构造 `QuerierBuilder`，默认值与 `get` 类似：

[zenoh/src/api/session.rs:1224-1243](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L1224-L1243)。

`QuerierBuilder::wait` 揭示了 Querier 与一次性 `get` 的本质差异——它会先声明 key、再调用 `declare_querier_inner`：

[zenoh/src/api/builders/querier.rs:176-198](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/querier.rs#L176-L198) —— `declare_keyexpr` 把 key 注册进 Session（可能换得一个更省传输的内部形式），`declare_querier_inner` 拿到 `id`，两者都是一次性 `get` 不会做的事。

`declare_querier_inner` 是「网络优化」的落点——它向网络发送一条持久的 `Interest`：

[zenoh/src/api/session.rs:1658-1681](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L1658-L1681) —— `InterestMode::CurrentFuture` + `KEYEXPRS + QUERYABLES` 表示「告诉我现在和将来这个 key 上的 Queryable 情况」。一次性 `get` 不发这条 Interest，因而拿不到这种持续优化。

声明成功后，`Querier` 把 target / consolidation / timeout / QoS 等都固化在自身字段里，之后每次 `get()` 只需构造 `QuerierGetBuilder`：

[zenoh/src/api/querier.rs:166-179](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/querier.rs#L166-L179) —— 注意它没有任何 key 参数，key 来自 `self.key_expr`。

`QuerierGetBuilder::wait` 复用 Querier 上固化的全部配置，最终也调用同一个 `Session::query`，区别只是带上 `querier_id = Some(self.querier.id)`：

[zenoh/src/api/builders/querier.rs:481-508](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/builders/querier.rs#L481-L508)。

`Querier` 的反声明与 `Drop` 语义和其它实体一致：

[zenoh/src/api/querier.rs:193-205](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/querier.rs#L193-L205) —— `undeclare()` 先清空匹配监听器，再调 `undeclare_querier_inner`。

[zenoh/src/api/querier.rs:348-356](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/querier.rs#L348-L356) —— `Drop` 中若 `undeclare_on_drop` 仍为真则自动反声明。

`z_querier.rs` 示例展示了「声明 Querier → 周期 `get()`」的标准写法，并演示了 `matching_listener`：

[examples/examples/z_querier.rs:33-53](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_querier.rs#L33-L53) —— 声明 Querier 并可选挂一个匹配监听器。

[examples/examples/z_querier.rs:62-95](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/examples/examples/z_querier.rs#L62-L95) —— 每秒一次 `querier.get().payload(buf).parameters(params)`，应答循环与 `z_get` 一致。

#### 4.2.4 代码实践

**目标**：用 `z_querier` 周期查询 4.1.4 启动的 `z_queryable`，并观察匹配监听器。

**操作步骤**：

1. 终端 A 保持 `z_queryable` 运行（同 4.1.4）。
2. 终端 B：
   ```bash
   cargo run --example z_querier -- -s "demo/example/zenoh-rs-queryable" -p "ping" --add-matching-listener
   ```

**需要观察的现象**：终端 B 启动时打印 `Querier has matching queryables.`，之后每秒打印一次查询与一条 `>> Received (...)`；关掉终端 A 后打印 `Querier has NO MORE matching queryables.`，重新启动终端 A 又变回有匹配。

**预期结果**：Querier 因声明了持久 Interest，能感知 Queryable 的上下线；周期 `get` 每次都能拿到一条应答。

> 待本地验证：匹配状态切换的确切时序与日志级别。

#### 4.2.5 小练习与答案

**练习 1**：既然 `Querier` 和 `get` 最终都调用 `Session::query`，为什么用 Querier「更省」？

> **答**：声明阶段 `declare_querier_inner` 把 key 注册进 Session 并向网络发了持久 `Interest`，key expression 可能被替换成更紧凑的内部形式（`declare_keyexpr`），后续每次 `get()` 不必重复解析/编码 key、也不必重复声明兴趣。一次性 `get` 每次都从零开始。

**练习 2**：`Querier` 的 `target` / `consolidation` / `timeout` 是在声明时设，还是每次 `get()` 时设？

> **答**：在**声明时**（`QuerierBuilder`）设定，固化进 `Querier` 字段；之后每次 `querier.get()` 只能改本次的 `payload`/`parameters`（见 `QuerierGetBuilder`）。这点和《u3-l3》QoS 在 `PublisherBuilder` 上设一次、后续 `put` 沿用是一致的思路。

### 4.3 Reply、应答处理与结果合并（Consolidation）

#### 4.3.1 概念说明

一次查询可能命中**多个** Queryable（网络上同一 key 可能有多处能回答），于是请求侧要面对两类问题：

1. **问谁**（`QueryTarget`）：是让 Zenoh 挑一个最佳回答者（`BestMatching`），还是问遍所有匹配的（`All`），或只问那些标记为 `complete` 的（`AllComplete`）。
2. **结果怎么合并**（`QueryConsolidation` / `ConsolidationMode`）：多个 Queryable 可能对同一 key 返回不同版本（不同时间戳）的数据，请求侧要不要去重、怎么去重。

`Reply` 是应答在请求侧的封装，本质是 `Result<Sample, ReplyError>`：成功的应答带一个 `Sample`，失败的带一个 `ReplyError`（对应供给端的 `reply_err`，见《u4-l1》）。

合并是本讲最微妙、也最容易踩坑的点：**默认策略 `AUTO` 会解析成 `Latest`**，它按「应答 key + 时间戳」去重——同一 key 只保留时间戳最新的那一条，且会**攒着**直到所有应答方都宣告结束才一次性吐给你。这意味着「同一 key 上挂两个 Queryable、各回各的值」时，默认你只能看到一条。

#### 4.3.2 核心流程

`QueryTarget` 三档（来自协议层）：

| 取值 | 含义 |
| --- | --- |
| `BestMatching`（默认） | Zenoh 自行挑选能最快、最完整回答的 Queryable，通常只问一个。 |
| `All` | 问遍所有 key 匹配的 Queryable。 |
| `AllComplete` | 只问那些用 `QueryableBuilder::complete(true)` 标记为「完整」的 Queryable。 |

`ConsolidationMode` 四档：

| 取值 | 行为 |
| --- | --- |
| `Auto`（默认） | 由实现决定；当前解析规则见下方公式。 |
| `None` | 不合并：每来一条应答就立即交给用户（同 key 也照收）。 |
| `Monotonic` | 同 key 收到更新（时间戳 ≥ 已见）就立即转发并更新缓存；更旧的丢弃。 |
| `Latest` | 同 key 只保留时间戳最新的；**攒到最后**（所有应答方结束 / 超时）才一起吐给用户。 |

`AUTO` 的解析规则：

\[
\text{consolidation}(\text{Auto}) =
\begin{cases}
\text{None} & \text{若查询参数含 time\_range（历史查询）} \\
\text{Latest} & \text{否则（默认）}
\end{cases}
\]

应答在请求侧的完整流转：

```text
对端 Queryable 应答 → 网络回 Response/ResponseFinal
   ↓
Session::send_response(ResponseBody::Reply)      // 数据应答
   1. 校验应答 key 与查询 key 是否相交（除非 _anyke 放行）
   2. 按 reception_mode 决定是否立即转发 / 攒进 HashMap<key, Reply>
Session::send_response_final                     // 某个应答方宣告“我答完了”
   nb_final -= 1；减到 0 表示所有来源都答完
   若是 Latest：把攒着的 HashMap 一次性冲刷给用户
   → 从 state.queries 移除该查询（handler sender 丢弃 → recv_async 返回 Err，循环退出）
```

关键直觉：`nb_final` 是「结束信号计数器」。`destination = Locality::Any` 时本地路径与远程路径各贡献一个，故初值为 2；只有本地或只有远程时为 1。计数归零 = 查询关闭 = 你的 `while let Ok(...) = recv_async()` 循环自然退出。这也是为什么 `z_get`/`z_querier` 里那个取数循环不需要自己写退出条件。

#### 4.3.3 源码精读

`Reply` 与 `ReplyError` 的定义：

[zenoh/src/api/query.rs:163-184](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/query.rs#L163-L184) —— `Reply { result: Result<Sample, ReplyError> }`；`result()` 借用访问，`into_result()` 拿走所有权。`replier_id()` 需 `unstable` feature。

`QueryTarget` 与 `ConsolidationMode` 都是从协议层 re-export 进公开 API 的：

[zenoh/src/api/query.rs:38-40](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/query.rs#L38-L40)。

协议层 `QueryTarget` 枚举：

[commons/zenoh-protocol/src/network/request.rs:96-104](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/network/request.rs#L96-L104)。

协议层 `ConsolidationMode` 枚举（注意 `Auto` 是 `#[default]`）：

[commons/zenoh-protocol/src/zenoh/query.rs:23-41](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/commons/zenoh-protocol/src/zenoh/query.rs#L23-L41)。

`QueryConsolidation` 是包了一层的 newtype，默认 `AUTO`：

[zenoh/src/api/query.rs:62-81](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/query.rs#L62-L81)。

收应答的核心在 `Session::send_response`，数据应答按 `reception_mode` 分三支处理：

[zenoh/src/api/session.rs:3302-3343](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L3302-L3343) —— 先做 key 相交校验（与《u4-l1》`ReplyKeyExpr`/`_anyke` 对应），再构造 `Reply { Ok(Sample) }`。

[zenoh/src/api/session.rs:3346-3423](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L3346-L3423) —— `None` 立即转发；`Monotonic` 比时间戳、新则转发并更新；`Latest`/`Auto` 只更新缓存（保留最新时间戳）但**不立即转发**（返回 `None`）。

`send_response_final` 在所有来源答完时，对 `Latest` 把缓存冲刷出去并关闭查询：

[zenoh/src/api/session.rs:3437-3461](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L3437-L3461) —— `nb_final` 减到 0 即移除查询。

错误应答（`ResponseBody::Err`）**绕过合并**，一律立即转发：

[zenoh/src/api/session.rs:3272-3301](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/session.rs#L3272-L3301) —— 构造 `Reply { Err(ReplyError) }` 直接 `callback.call`。

`QueryState` 是请求侧保存一次查询全部状态的结构（合并模式、缓存表、回调、归属 Querier）：

[zenoh/src/api/query.rs:221-229](https://github.com/eclipse-zenoh/zenoh/blob/55263c9da5841cc620ba8d9e41f8a8965a35978a/zenoh/src/api/query.rs#L221-L229)。

#### 4.3.4 代码实践

**目标**：亲手验证「同一 key 上两个 Queryable」时，`QueryTarget` 与 `ConsolidationMode` 如何决定你能看到几条应答。

`z_get` 的命令行只暴露了 `-t/--target`，没有暴露 consolidation，所以默认永远是 `Latest`。要看到「两条都送达」，需要写一个最小 get 客户端，把 `target` 设为 `All`、`consolidation` 设为 `None`。

**示例代码**（非项目原有示例，保存为 `examples/examples/z_get_all.rs`）：

```rust
// 示例代码：z_get_all.rs —— 演示 target=All + consolidation=None，收齐所有应答
use std::time::Duration;
use zenoh::query::{ConsolidationMode, QueryTarget};

#[tokio::main]
async fn main() {
    let session = zenoh::open(zenoh::Config::default()).await.unwrap();
    let replies = session
        .get("demo/example/zenoh-rs-queryable")
        .target(QueryTarget::All)                // 问遍所有匹配的 Queryable
        .consolidation(ConsolidationMode::None)  // 不合并，每条应答都立即送达
        .timeout(Duration::from_secs(5))
        .await
        .unwrap();
    let mut count = 0;
    while let Ok(reply) = replies.recv_async().await {
        count += 1;
        match reply.result() {
            Ok(sample) => {
                let payload = sample
                    .payload()
                    .try_to_string()
                    .unwrap_or_else(|e| e.to_string().into());
                println!("reply {count}: '{}' = '{}'", sample.key_expr(), payload);
            }
            Err(e) => println!("reply {count}: ERROR"),
        }
    }
    println!("total replies: {count}");
}
```

注册为新示例：在 `examples/Cargo.toml` 里加一段 `[[example]]`（参考已有 `z_get` 的写法），随后：

1. 终端 A：`cargo run --example z_queryable -- -k demo/example/zenoh-rs-queryable -p "Reply-A"`
2. 终端 B：`cargo run --example z_queryable -- -k demo/example/zenoh-rs-queryable -p "Reply-B"`
3. 终端 C 分别试三种姿势：
   - 默认 `z_get`：`cargo run --example z_get -- -s "demo/example/zenoh-rs-queryable"`
   - 全量 target 但默认合并：`cargo run --example z_get -- -s "demo/example/zenoh-rs-queryable" -t ALL`
   - 自定义客户端：`cargo run --example z_get_all`

**需要观察的现象与预期结果**：

| 姿势 | target | consolidation | 预期收到的应答数 | 原因 |
| --- | --- | --- | --- | --- |
| 默认 `z_get` | BestMatching | Latest | **1** | Zenoh 只挑一个最佳回答者。 |
| `z_get -t ALL` | All | Latest | **1** | 两个 Queryable 都被问了，但它们用**同一个 key** 回答，`Latest` 按键去重只留时间戳最新的一条。 |
| `z_get_all` | All | None | **2** | 既问遍所有人，又不去重，两条应答都立即送达。 |

这张表就是 consolidation 行为的「证据」：**target 决定问几个人，consolidation 决定给你看几条**。要让多个 Queryable 的不同回答全部可见，二者缺一不可。

> 待本地验证：上表应答数为源码推导的预期；`Latest` 下两条同 key 应答谁胜出取决于时间戳（Zenoh 自动附 HLC 时间戳，见《u6-l4》），胜负可能随机。

#### 4.3.5 小练习与答案

**练习 1**：为什么 `z_get -t ALL` 明明问了两个 Queryable，却只收到一条应答？

> **答**：因为默认合并是 `Latest`。`Latest` 把应答放进一个以**应答 key** 为键的 `HashMap`，同 key 只保留时间戳最新的；两个 Queryable 用同一个 key 回答，所以只有一条存活，且要等所有来源 `ResponseFinal` 后才冲刷出来（`send_response_final`）。

**练习 2**：`Reply::result()` 返回什么类型？错误应答会走合并逻辑吗？

> **答**：返回 `Result<&Sample, &ReplyError>`——成功带 `Sample`，失败带 `ReplyError`。错误应答（`ResponseBody::Err`）**绕过合并**，构造后立即 `callback.call`，所以 `reply_err` 不会被 `Latest` 去重或攒住。

**练习 3**：取数循环 `while let Ok(reply) = replies.recv_async().await { ... }` 没有显式 `break`，它靠什么退出？

> **答**：靠 `nb_final` 归零。所有应答来源（本地 + 远程）都发完 `ResponseFinal` 后，查询从 `state.queries` 移除，handler 的 sender 随之丢弃，`recv_async` 返回 `Err`，循环自然结束；若超时则超时任务会补发一条 `Timeout` 错误应答后再关闭。

## 5. 综合实践

把三个模块串起来：搭一个「多 Queryable + 周期 Querier」的小系统，观察 target、consolidation、匹配感知如何协同。

**任务**：

1. 起两个 Queryable 服务同一 key，返回不同值（用 `-p` 区分），其中一个加 `--complete`：
   ```bash
   cargo run --example z_queryable -- -k demo/sensor/temp -p "A" --complete
   cargo run --example z_queryable -- -k demo/sensor/temp -p "B"
   ```
2. 用 4.3.4 的 `z_get_all`（`target=All, consolidation=None`）查询 `demo/sensor/temp`，确认能看到 2 条应答。
3. 把客户端的 `consolidation` 改回默认（删掉那一行，即 `AUTO → Latest`），重新查询，确认只剩 1 条——亲手验证「合并」发生了。
4. 把 `target` 改成 `AllComplete`（`QueryTarget::AllComplete`），重新查询，确认只剩「A」那一条——因为只有它声明了 `complete`。
5. 改用 `z_querier` 周期查询，并加 `--add-matching-listener`：
   ```bash
   cargo run --example z_querier -- -s "demo/sensor/temp" -t ALL --add-matching-listener
   ```
   观察每秒输出，以及启停两个 Queryable 时 `Querier has matching queryables.` / `NO MORE matching queryables.` 的切换。
6. 用一段话总结：在「想读到所有副本」「只想读权威副本」「只想知道有没有人能答」三种需求下，分别该用哪种 `target` + `consolidation` 组合。

**预期**：步骤 2 收 2 条、步骤 3 收 1 条、步骤 4 收 1 条（仅 complete 的）、步骤 5 周期输出且匹配状态随 Queryable 上下线变化。

> 待本地验证：`AllComplete` 在仅一个 Queryable 标 `complete` 时的应答来源；匹配监听器的具体日志时序。

## 6. 本讲小结

- `Session::get(selector)` 是一次性查询入口，返回 `SessionGetBuilder`；查询在 builder resolve 时由 `Session::query` 真正发出，`querier_id = None`。
- `Querier` 是声明式、可复用的查询器：声明时 `declare_querier_inner` 把 key 注册进 Session并向网络发持久 `Interest`（`CurrentFuture`），后续每次 `get()` 复用固化的 target/consolidation/timeout，只带本次可变的 payload/parameters。
- `Reply = Result<Sample, ReplyError>`，用 `result()` 区分成功与错误；错误应答绕过合并直接送达。
- `QueryTarget`（问谁）与 `QueryConsolidation`（怎么合并）是两个正交维度；默认 `BestMatching + AUTO(Latest)` 会让同一 key 上的多个 Queryable 只露出一条应答。
- `Latest` 按「应答 key + 时间戳」去重并攒到所有来源答完（`nb_final` 归零）才冲刷；`None` 则每条立即送达。要让多个副本的不同回答全部可见，需 `target=All + consolidation=None`。
- 取数循环 `recv_async` 靠 `nb_final` 归零（或超时）自然退出，无需手写 `break`。

## 7. 下一步学习建议

- **Selector 与查询参数**（《u4-l3》）：本讲把 selector 当作「key + 可选参数」黑盒用了，下一讲拆开 `Selector` 的 URL 式语法与 `Parameters`，讲清 `time_range`（它会让 `AUTO` 解析成 `None`）等参数如何影响合并与历史查询。
- **数据表示**（《u5-l1》）：本讲反复用 `payload().try_to_string()`，下一讲深入 `ZBytes` 与 `Encoding`，理解应答负载的零拷贝读写。
- **匹配感知**（《u6-l3》）：本讲用到 `Querier::matching_listener`，后续会系统讲解 `MatchingListener` 与节流发送。
- **内部架构**（《u7》《u8》）：想了解 `primitives.send_request` / `send_interest` 之后查询如何在路由层（Gateway/Face/Tables）被分发到匹配的 Queryable，可进入内核单元。
