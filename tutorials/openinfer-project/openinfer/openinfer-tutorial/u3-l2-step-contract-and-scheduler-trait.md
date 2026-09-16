# 新契约：step 驱动的 Scheduler trait 与 RequestLedger

## 1. 本讲目标

上一讲（u3-l1）我们剖析了旧一代引擎契约：`EngineHandle` + 逐 token 的 `TokenEvent` 流。本讲进入新一代——**step 驱动契约**。学完本讲，你应该能够：

1. 说出 step 契约的词汇表：`Request` / `QueuedRequest` / `RequestId` / `StepOutputs` / `RequestUpdate` / `Terminal` 各自的角色与约束。
2. 理解「一次调度步、一条消息」的设计：为什么把事件序列压平成一条扁平记录，能把旧协议里靠约定保证的顺序规则变成**结构上不可能违反**的规则。
3. 掌握 `RequestLedger` 账本的状态机（`Queued → Active → 终态`），以及它如何用「触碰已关闭账户即 panic」在调用点强制 **terminal 恰好一次（terminal-exactly-once）**。
4. 读懂 `Scheduler` trait 的三个方法与 `spawn_scheduler` / `drive` 专用 OS 线程驱动循环的完整迭代节奏。
5. 理解接线层：`scheduler_pair` 如何铸造前端端（`SchedulerHandle`）与调度器端（`SchedulerBackend`），`RequestEnvelope` / `DeferredFinish` 的 drop bomb 如何保证「绝不让客户端挂死」，以及 `LaunchedEngine::Stepped` 如何把引擎交还给协议栈。

## 2. 前置知识

本讲默认你已读过 u3-l1（旧契约）。先把几个关键背景补齐：

- **旧契约的三个痛点（新契约的动机）**：
  1. `TokenEvent` 是事件**序列**，「`Scheduled` 在 token 之前、token 在唯一终态之前」这些顺序规则靠文档约定，生产者写错了编译器不管；
  2. 每个事件（甚至每个 token）都是一条独立消息，前端要在共享频道上按标签 demux；
  3. `EngineHandle` 自己做多分区路由与负载估计（运行数 + 4×等待数），路由策略和引擎契约耦合在一起。
- **账户 / 账本（ledger）比喻**：把每个未应答的请求看作一个「账户」。提交请求 = 开户；调度器每一步的产出（准入、token、终结）= 对账户的**记账**；终态（Finished / Rejected / Failed）= **销户**。账户一旦销户，任何再写入都是 bug——这个比喻贯穿 `RequestLedger` 的全部 API。
- **恰好一次（exactly-once）语义**：分布式与流式系统里的经典概念。这里指：一个请求的生命周期**必须**以恰好一个终态结束——零个终态意味着客户端永远等待（挂死），两个终态意味着前端对同一条流做两次收尾（状态错乱）。
- **drop bomb（析构炸弹）**：Rust 的 `Drop` trait 在值离开作用域时必然被调用。把「未送达的应答」放进 `Option<inner>`，`Drop` 时若 `inner` 还在，就补发一条 `Failed` 终态——用类型系统保证「请求绝不无声消失」。本讲的 `RequestEnvelope` 和 `DeferredFinish` 都是 drop bomb。
- **无界通道（unbounded channel）**：发送方永不阻塞的队列。step 契约的提交通道与 step 流都是无界的：慢消费者不会把 GPU 调度线程卡住（代价是内存上界由生产速率约束——推理引擎里这是正确的取舍，见 u3-l1 的同类讨论）。
- **原子中止标志**：`Arc<AtomicBool>`。前端调用 `RequestControl::abort()` 置位，调度器在 `step` 里用 `is_aborted` 轮询后响应式退役（reactive retire）——取消不是「关频道」或「打断线程」，而是一个异步可见的标志位。

## 3. 本讲源码地图

本讲全部位于 `pegainfer-frontend` crate 的 `engine` 模块内（该 crate 的契约半不含任何 CUDA 类型，可在纯 CPU 环境阅读与测试）：

| 文件 | 作用 |
|------|------|
| [pegainfer-frontend/src/engine/mod.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/mod.rs#L1-L29) | engine 模块总纲：新旧两套契约的目录与迁移状态说明 |
| [pegainfer-frontend/src/engine/step.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/step.rs) | **线上词汇表**：`RequestId`、`Request`、`QueuedRequest`、`StepOutputs`、`RequestUpdate`、`RejectReason`、`Terminal` |
| [pegainfer-frontend/src/engine/ledger.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/ledger.rs) | **账本**：`RequestLedger`，活请求账户簿 + 每步对账单，强制 terminal 恰好一次 |
| [pegainfer-frontend/src/engine/driver.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/driver.rs) | **驱动**：`Scheduler` trait（三方法）、`spawn_scheduler`、`drive` 轮询循环 |
| [pegainfer-frontend/src/engine/wiring.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/wiring.rs) | **接线**：`scheduler_pair`、`SchedulerHandle`、`MetricsPublisher`、`Engine`/`EngineInfo`/`LaunchedEngine` |
| [pegainfer-frontend/src/engine/request_lifecycle.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/request_lifecycle.rs) | **信封与晚投递**：`RequestEnvelope`（提交信封 drop bomb）、`DeferredFinish`、`RequestControl` |
| [pegainfer-frontend/src/engine/event.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/event.rs) | 旧契约的事件类型；本讲只借用其中的 `FinishReason` 与 `TokenLogprob`（新契约复用） |

## 4. 核心概念与源码讲解

### 4.1 engine::step —— 一步一条消息的词汇表

#### 4.1.1 概念说明

step 契约的核心想法可以用一句话概括：**一次调度步（scheduler step）对前端产出恰好一条消息 `StepOutputs`；这条消息里，每个被触碰的请求至多占一条扁平记录 `RequestUpdate`**。

旧契约里「`Scheduled` 必须在 token 之前、token 之后才能有唯一终态」是事件序列上的**约定**；新契约把整步发生的一切压平成一条记录的字段——`scheduled`、`tokens`、`terminal` 是同一结构体的三个字段，消费者按字段顺序折叠（fold）。一个扁平结构**在类型上就无法表达「终态之后再出现 token」**：顺序规则从约定变成了结构。step.rs 的模块注释把这一点说得很直白：

> One scheduler step produces one `StepOutputs` message. Per request the step carries at most one `RequestUpdate` — a flat record rather than an event sequence, so the intra-step ordering rules of the old `TokenEvent` protocol ... are structure, not convention.

而**跨步**的顺序（「终态之后任何一步不得再提及该 id」）由生产侧的 `RequestLedger` 强制（见 4.2）。

#### 4.1.2 核心流程

一次典型请求在新契约词汇表下的生命周期：

```text
SchedulerHandle::submit(Request)
    │  铸造 RequestId（每调度器计数器）+ abort 标志 + queued_at
    ▼
RequestEnvelope ──(crossbeam 提交通道)──▶ drive 循环 drain
    │
    ▼ ledger.register(envelope)   → 开户，产出
QueuedRequest { id, request } ──▶ Scheduler::submit()   ← 模型代码第一次介入
    │
    ▼ Scheduler::step() 期间写账本（admit / push_tokens / ... / finish）
ledger.commit_step() ──▶ StepOutputs { updates: Vec<RequestUpdate> }
    │                     （空闲步不发送任何消息）
    ▼ tokio step 流 ──▶ 协议栈翻译循环（下一讲 u3-l3）
```

`RequestUpdate` 各字段的出现时机：

| 字段 | 出现次数 | 时机 |
|------|---------|------|
| `scheduled` | 每请求生命周期恰好一次 | 准入它的那一步 |
| `tokens` + `logprobs` | 多次（可跨步） | 本步新提交的 token；纯 prefill 步为空；投机/MTP 多 token 一次到达 |
| `cached_tokens` | 至多一次 | 调度器得知前缀命中值的那一步（首个 prefill 分块），而非准入时——准入时还不知道 |
| `prompt_echo` | 至多一次 | echo 模式下 prefill 完成时 |
| `kv_transfer` | 视情况 | P/D 交接元数据透传 |
| `terminal` | 恰好一次（与其它字段同批或后续某步） | 生命周期终结；此后任何一步不得再提及该 id |

#### 4.1.3 源码精读

**身份：`RequestId`**。[pegainfer-frontend/src/engine/step.rs:22-44](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/step.rs#L22-L44) 定义了一个 `u64` 包装类型：故意做成 `Copy` + 整数键，外部协议的字符串请求 id 留在协议栈，在协议栈自己的边界上映射成它。这与旧契约「共享频道上按 `Arc<str>` 标签 demux」形成对照——整数键让账本的 `HashMap<RequestId, Account>` 查找零字符串开销。

**负载：`Request` 与 `QueuedRequest`**。[step.rs:49-68](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/step.rs#L49-L68) 的 `Request` 是纯数据负载：`prompt_tokens`、`SamplingParams`、`max_tokens`、`lora_adapter`、`kv_transfer_params`、`logprobs`/`prompt_logprobs`、`trace_parent`、`client_label`。注意注释强调：**身份、排队时间戳、abort 标志都不在这里**——它们在 submit 时铸造，活在请求的账本账户上。调度器收到的形态是 [step.rs:73-76](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/step.rs#L73-L76) 的 `QueuedRequest`：账户 id + 负载，id 由 `SchedulerHandle::submit` 铸造，负载自己永远不携带 id。

**消息与记录：`StepOutputs` / `RequestUpdate`**。[step.rs:81-84](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/step.rs#L81-L84)：一步产出的一切装在一条 `StepOutputs` 里。核心是 [step.rs:89-112](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/step.rs#L89-L112) 的扁平记录——字段按「消费者折叠顺序」排列：先准入事实，再新 token，最后终态。其中 [step.rs:114-138](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/step.rs#L114-L138) 的 `is_vacant()` 判定「本步对该请求只有内部簿记、无可观察事实」的空记录，账本会把这种记录直接丢弃而不是发出去。

**准入事实：`ScheduledInfo`**。[step.rs:146-151](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/step.rs#L146-L151)：`queued_at` 与 `scheduled_at` 是**契约层盖的章，绝不许模型代码盖**——前者在 `SchedulerHandle::submit`，后者在 `RequestLedger::admit`。用单调 `Instant`，vLLM 协议需要的 unix 浮点秒由协议栈对着自己的锚点换算（旧契约的 `unix_now_s()` 就是那个时代的产物）。

**拒绝与终结：`RejectReason` / `Terminal`**。[step.rs:166-189](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/step.rs#L166-L189) 的 `RejectReason` 是**类型化**的拒绝原因（`ContextLength` / `EchoPrefillTokens` / `KvBudget` / `UnknownLoraAdapter` / `Unsupported`），标了 `#[non_exhaustive]`——模型线会长出新拒绝种类，前端保留通配分支；前端据此映射自己的错误面（HTTP 状态码、重试策略），而不是去解析渲染后的文本。[step.rs:232-252](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/step.rs#L232-L252) 的 `Terminal` 有三个变体，注释点明关键原则：**token 计数由账本根据实际发出的内容统计，绝不靠模型代码手工维护**——这消除了「模型说发了 5 个 token、实际流里只有 4 个」这类对不上的账。

#### 4.1.4 代码实践（源码阅读型：新旧契约字段对照）

1. **实践目标**：亲手验证「`TokenEvent` 七个事件变体被压平成 `RequestUpdate` 的字段」这一映射，建立新旧契约的对照直觉。
2. **操作步骤**：
   - 阅读旧契约事件表 [pegainfer-frontend/src/engine/event.rs:18-54](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/event.rs#L18-L54)（`Scheduled` / `Token` / `PromptTokens` / `KvTransfer` / `Finished` / `Error` / `Rejected` 共七个变体）；
   - 再阅读 [step.rs:89-112](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/step.rs#L89-L112) 的 `RequestUpdate` 字段；
   - 画一张两列对照表。
3. **需要观察的现象**：七个事件并非一一对应七个字段——有两个「合并」与一个「搬迁」。
4. **预期结果**（参考对照表）：

   | 旧 `TokenEvent` 变体 | 新 `RequestUpdate` 落点 |
   |---|---|
   | `Scheduled{cached_tokens}` | `scheduled: Option<ScheduledInfo>`；`cached_tokens` **搬迁**为独立字段（因为准入时还不知道前缀命中量，见字段注释） |
   | `Token{id, logprob}`（N 次） | `tokens` + `logprobs` 两个平行数组（N 个 token 合并为一批） |
   | `PromptTokens{ids, logprobs}` | `prompt_echo: Option<PromptEcho>` |
   | `KvTransfer{params}` | `kv_transfer` |
   | `Finished` / `Error` / `Rejected`（三个终态事件） | `terminal: Option<Terminal>` 的三个变体（**合并**为一个枚举字段） |

5. 无需运行命令，纯源码对照即可完成。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `RequestId` 要做成 `Copy` 的整数包装，而外部协议的字符串请求 id 不进入 engine 契约？

**答案**：整数键让账本 `HashMap<RequestId, Account>` 的查找、消息里的引用都零分配零字符串比较；`Copy` 让它可以在日志、账本、调度器之间自由按值传递。字符串 id 是协议栈（vLLM）的概念，映射放在协议栈自己的边界上，engine 契约保持纯净——每个 `SchedulerHandle` 用自己的计数器铸 id，天然无冲突。

**练习 2**：`RequestUpdate::cached_tokens` 为什么不在 `scheduled`（准入事实）里一起报，而要等后续某一步？

**答案**：见 [step.rs:101-104](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/step.rs#L101-L104) 的注释：前缀命中量是调度器在**首个 prefill 分块**执行时才得知的事实，准入那一刻（`admit`）还不知道，所以字段注释明确「reported in the step where the scheduler learns it ... not at admission — admission-time `Scheduled` cannot know it yet」。旧契约把它塞在 `Scheduled` 事件里，只能报 0 或事后补发，是设计缺陷的修正。

**练习 3**：`Request` 结构体里为什么没有 `id`、排队时间戳和 abort 标志？

**答案**：这三个是「账户属性」而非「负载属性」：id 与时间戳由 `SchedulerHandle::submit` 在提交瞬间铸造（时间戳必须反映提交时刻，不能被负载构造者伪造）；abort 标志是前端持有的 `RequestControl` 与账本账户共享的 `Arc<AtomicBool>`。负载在通道里流转、被调度器消费，身份信息只存在于账本一侧——`QueuedRequest` 把两者临时拼在一起交给 `Scheduler::submit`。

### 4.2 engine::ledger —— RequestLedger 账本与恰好一次终结

#### 4.2.1 概念说明

`RequestLedger` 是 step 契约的**执法机构**。模块注释（[ledger.rs:1-20](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/ledger.rs#L1-L20)）给出了它的完整宪法：

- **每个未应答请求一个账户**。开户只有一条路（`register`，且是 `pub(crate)`——只有 drive 循环能开）；销户只有五个终结转换（`finish` / `fail` / `reject` / `retire` / `defer_finish`）。
- 因为「每次开户都是一次 submit、每次销户都是一个终态」，**「终态恰好一次」与「终态之后无写入」两条不变量就在这里强制**：触碰已关闭或不存在的账户，直接 panic 在肇事调度器调用点上——bug 在开发与测试期就爆炸，而不是在生产中表现为客户端挂死或重复收尾。
- 账户在账本析构时仍开着的（引擎 bug 或拆除竞态），由 `Drop` 统一写成 `Failed` 终态核销——**客户端永远等到一个终态，绝不挂死**。

权限分层也在此划定：调度器面向的方法（`admit` / `push_tokens` / `finish`……）是 `pub`；开账户与发对账单（`register` / `commit_step` / `fail_all`）是 `pub(crate)`——**驱动循环拥有节奏，模型 crate 够不到**。

#### 4.2.2 核心流程

`RequestLedger` 的完整状态机（本讲综合实践会让你亲手画这张图）：

```text
                 SchedulerHandle::submit
                        │  铸 id / abort / queued_at，装入 RequestEnvelope
                        ▼
        ┌─ RequestEnvelope（在提交通道中，自带 drop bomb）─┐
        │  未被 register 消费就 Drop ⇒ 直接补发 Failed 终态  │
        ▼                                                  │
   ledger.register（消费信封＝拆除炸弹，开户）                 │
                        │                                  │
                        ▼                                  │
                 【Queued 账户】                            │
                  │           │                            │
         admit    │           │  reject(reason)            │
                  ▼           ▼                            │
              【Active 账户】  Terminal::Rejected ✗终态      │
                  │           （账户关闭）                   │
                  │                                        │
   持续记账（可多次、每步合并为一条 RequestUpdate）：          │
     push_tokens / set_cached_tokens / echo_prompt /       │
     kv_transfer                                          │
                  │                                        │
    ┌──────────┬──┴───────┬────────────┐                  │
 finish   defer_finish   fail        retire（已 abort）    │
    │          │           │            │                  │
    ▼          ▼           ▼            ▼                  │
Terminal::  Deferred-   Terminal::    静默销账：抽出本步    │
Finished ✗  Finish      Failed ✗      已缓冲的记录并丢弃    │
（终态）    （线程外晚    （终态）      （不发送任何消息）    │
            投递，见下）                                  │
                                                         │
账户关闭后任何再触碰 ⇒ panic "no open account" ◀────────────┘
账本 Drop 时仍开着的账户 ⇒ 每个补一条 Terminal::Failed（核销消息）
fatal step 错误 ⇒ 驱动调用 fail_all 把所有开账户写成 Failed
```

每步的**对账单（StepStatement）**机制：当前步的所有写入先进缓冲，同一请求的多次写合并到同一条 `RequestUpdate`（一步至多一条记录每 id）；`commit_step` 时滤掉空记录，一次性作为一条 `StepOutputs` 发出；没触碰任何请求的步**什么都不发**。

#### 4.2.3 源码精读

**账户与状态**。[ledger.rs:44-66](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/ledger.rs#L44-L66)：`Account` 持有 abort 标志共享句柄、prompt 长度、`queued_at` 与两态状态机 `AccountState::Queued | Active { completion_tokens }`。注释点明负载不在这里——它已在 `submit` 时交给调度器，账户里装的是**引擎还欠客户端什么**。

**对账单缓冲**。[ledger.rs:68-101](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/ledger.rs#L68-L101)：`StepStatement` 用 `Vec<Option<RequestUpdate>>` + `HashMap<RequestId, usize>` 索引实现「每 id 一步一条」的合并；`extract` 把某请求的缓冲记录整条抽走（`defer_finish` 与 `retire` 用），`take_updates` 滤空后交卷。

**查询方法**。[ledger.rs:135-155](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/ledger.rs#L135-L155)：`is_aborted`（abort 任何时刻可能落地，所以每个 finish 路径上也要探测）、`is_active`（防御同一步内早些时候已被应答的请求）、`completion_tokens`（取账本自己的 tally）。

**准入转换**。[ledger.rs:159-177](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/ledger.rs#L159-L177) `admit`：断言当前是 `Queued`（对已准入请求 admit 直接 panic），置 `Active`、盖 `scheduled_at` 章、把 `ScheduledInfo` 缓冲进本步对账单。[ledger.rs:180-190](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/ledger.rs#L180-L190) `reject`：断言仍在 `Queued` 态（「已准入的请求只能 finish 或 fail，不能 reject」），销户并写 `Terminal::Rejected`。

**流式记账**。[ledger.rs:196-215](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/ledger.rs#L196-L215) `push_tokens`：先断言 logprobs 与 ids 平行（缺席或等长），再断言账户处于 `Active`（未准入就 push 直接 panic「push_tokens before admission」），tally 自增后追加进对账单。`set_cached_tokens` / `echo_prompt` / `kv_transfer`（[ledger.rs:218-236](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/ledger.rs#L218-L236)）同样要求 `Active`。

**终结转换**。[ledger.rs:241-251](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/ledger.rs#L241-L251) `finish`：销户，token 计数取自账本 tally（`let AccountState::Active { completion_tokens } = account.state else { panic!(...) }`——未准入就 finish 也是 panic）。[ledger.rs:255-262](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/ledger.rs#L255-L262) `fail`：唯一**两态皆可**的终结——排队中的请求也可能死于引擎错误。[ledger.rs:267-274](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/ledger.rs#L267-L274) `retire`：应答已 abort 的请求，**静默**销账并丢弃本步已缓冲的记录——前端对这个 id 的状态早已删除，没有收件人。

**`defer_finish` —— 晚投递的终态**。[ledger.rs:281-296](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/ledger.rs#L281-L296)：销户，把该请求**本步已缓冲的整条记录（含 token）抽取出来**，装上 `Finished` 终态，打包成 `DeferredFinish` 返回。关键在注释里的那句设计理由：P/D（prefill/decode 分离）的 prefill 角色要把 `Finished` **扣到本步的 KV 保存对 peer 可见之后**才能放行，所以这个终态必须能**从任意线程、在更晚的时刻**投递；而把整条 per-request 记录（tokens 在内）折叠进这一条晚投递消息，保证了它**不可能与 step 流乱序**——token 不会先随 step 流发一遍、终态又晚到一遍造成重复。

**驱动面（`pub(crate)`）**。[ledger.rs:303-317](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/ledger.rs#L303-L317) `register`：消费（并拆除）信封、开户、断言 id 不重复、交回 `QueuedRequest`。[ledger.rs:322-330](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/ledger.rs#L322-L330) `commit_step`：一次迭代恰好调用一次，空对账单不发消息；接收端已关闭（前端走了）则忽略发送错误——驱动会通过提交通道的断连察觉并收尾。[ledger.rs:335-340](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/ledger.rs#L335-L340) `fail_all`：致命 `step` 错误后的核销扫除，携带真实错误（而不是 Drop 扫除的通用文案）。

**析构核销**。[ledger.rs:346-370](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/ledger.rs#L346-L370)：`Drop` 时仍开着的账户，按 `Queued`/`Active` 分别给「request dropped by the engine before it was answered」/「... mid-stream」两种文案的 `Failed`，拼成一条核销消息发出。

**单测即规格**。[ledger.rs:395-427](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/ledger.rs#L395-L427) 验证「admit + tokens + cached + finish 折叠成一条记录」；[ledger.rs:475-505](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/ledger.rs#L475-L505) 验证 defer_finish「整条记录搭车晚投递、当步本身不发货」；[ledger.rs:508-533](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/ledger.rs#L508-L533) 验证 Drop 核销；[ledger.rs:566-575](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/ledger.rs#L566-L575) 用 `#[should_panic]` 把「触碰已关闭账户必须爆炸」写进测试。

#### 4.2.4 代码实践（运行型：账本单测即规格）

1. **实践目标**：运行 `ledger.rs` 内嵌的 8 个单元测试，用测试断言逐条核对 4.2.2 的状态机。
2. **操作步骤**：
   ```bash
   cargo test -p pegainfer-frontend --lib engine::ledger
   ```
   （`pegainfer-frontend` 是纯 Rust crate，无需 GPU；首次编译依赖需要几分钟。）
3. **需要观察的现象**：8 个测试全部通过，其中 `touching_a_closed_account_panics` 显示为 **passed**（`#[should_panic]` 测试：panic 被期望捕获）。
4. **预期结果**：通过的测试名对应状态机的每条边——`admission_tokens_and_finish_fold_into_one_entry`（Queued→Active→Finished 的折叠）、`reject_carries_prompt_len_and_no_tokens`（Queued→Rejected）、`retire_discards_buffered_output`（abort 的静默销账）、`defer_finish_folds_step_output_and_delivers_late`（晚投递）、`dropped_ledger_writes_off_open_accounts`（Drop 核销）、`abort_flag_is_visible_through_both_states`（abort 双态可见）、`submit_to_a_dead_scheduler_fails_the_request`（信封 drop bomb）、`touching_a_closed_account_panics`（恰好一次执法）。具体输出格式**待本地验证**。
5. 可选加深：`cargo test -p pegainfer-frontend --lib engine::ledger -- --nocapture` 观察 `retire` 路径的 `log::debug!` 输出（需启用日志）。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `reject` 只允许从 `Queued` 态调用，而 `fail` 两种状态都允许？

**答案**：语义不同。`reject` 是**准入决策**——「这个请求我永远不会接」（超上下文、KV 预算不够、未知 LoRA 适配器），它只能发生在尚未准入时；一旦已经准入并可能发过 token，就没有「拒绝」可言，只能 `finish`（正常终结）或 `fail`（引擎侧错误）。而 `fail` 表达的是**引擎自身出了问题**（执行失败、调度器 bug、引擎拆除），排队中的请求同样可能死于引擎错误，所以 [ledger.rs:255-258](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/ledger.rs#L255-L258) 的注释明确「Valid in both states — a queued request can die to an engine error before admission」。

**练习 2**：`Terminal::Finished` 里的 `completion_tokens` 为什么由账本统计而不是调度器自己报一个数？

**答案**：账本是 token 的**唯一记账处**：每次 `push_tokens` 都同步自增 tally（[ledger.rs:207](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/ledger.rs#L207)），`finish` 时直接取 tally（[ledger.rs:246-250](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/ledger.rs#L246-L250)）。step.rs 的 `Terminal` 文档注释写明「Token counts are tallied by the ledger from what actually shipped, not hand-maintained by model code」——单一事实来源消除了「模型自报的数量与实际流出的 token 数不一致」这类对不上的账，投机解码等多 token 提交路径尤其容易出这种错。

**练习 3**：如果某调度器实现先 `finish(id)` 又对同一 id `push_tokens`，会发生什么？为什么这是正确的设计？

**答案**：`push_tokens` 内部经 `self.accounts.get_mut(&id).unwrap_or_else(|| panic!("no open account for {id}..."))`（[ledger.rs:201-205](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/ledger.rs#L201-L205)）——账户已在 `finish` 里销掉，这里直接 panic 在调用的调度器线程上。这是正确设计因为：终态已经（或即将）随 step 流发给前端，前端随即删除该 id 的状态；此后任何再写入都无处投递，若静默吞掉就是把 bug 藏进生产，panic 让它在开发期就暴露在最接近肇事点的位置（模块注释：「touching a closed or unknown account panics at the offending scheduler call site」）。

### 4.3 engine::driver —— Scheduler trait 与专用线程 drive 循环

#### 4.3.1 概念说明

`Scheduler` trait 是**模型侧的运行时义务清单，且只有三条**：

```rust
pub trait Scheduler: Send {
    fn submit(&mut self, request: QueuedRequest);
    fn step(&mut self, ledger: &mut RequestLedger) -> anyhow::Result<()>;
    fn metrics(&self) -> SchedulerMetrics;
}
```

它运行在 `spawn_scheduler` 起的**专用 OS 线程**上（`Send` 即可，无需 `Sync`——单线程独占）。最重要的架构决定写在 [driver.rs:1-9](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/driver.rs#L1-L9) 的模块注释里：**驱动循环只写一份，放在前端 crate，供所有模型线共用**——排空顺序、每次迭代恰好一次 metrics 发布与一次 commit、关停与致命错误的处理，这些约定从此是代码而不是「每个 crate 自觉遵守的纪律」。超过三条方法之外的能力（如 LoRA 控制）不进契约：需要它的调度器在 spawn 之前自己捕获通道、在 `step` 内部排空（这是 u9-l3 LoRA 讲义的伏笔）。

三个方法的语义要点：

- **`submit`：只做所有权转移**。一切裁决（admit/reject/retire）都在 `step` 里写账本——因为驱动每迭代 commit 一次，`step` 里记录的裁决与 submit 时立刻记录落在**同一次** commit 里，延迟裁决不损失任何东西（[driver.rs:22-27](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/driver.rs#L22-L27) 注释）。
- **`step`：推进一个调度步**——准入、GPU 工作、逐请求写账本。可恢复的执行失败由调度器自己吸收（fail 掉被触碰的请求、继续服务）；返回 `Err` 意味着**引擎整体不可用**，驱动会核销全部开账户并收线。
- **`metrics`：指标快照**，驱动每迭代发布一次。

#### 4.3.2 核心流程

`drive` 循环（[driver.rs:59-104](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/driver.rs#L59-L104)）每次迭代的固定节奏：

```text
loop {
    ① 排空提交通道：try_recv 直到 Empty
         每收到一个信封：ledger.register(envelope) → scheduler.submit(queued)
         （通道 Disconnected ⇒ submissions_open = false，但先继续把手头的活干完）
    ② step = scheduler.step(&mut ledger)        ← 可能写账本
    ③ snapshot = scheduler.metrics(); metrics.publish(&snapshot)
    ④ ledger.commit_step()                       ← 本步对账单一次发出
    ⑤ 若 step 返回 Err（致命）：
         log::error → ledger.fail_all(真实错误) → 再 commit_step 一次 → return
    ⑥ 若 num_running_reqs == 0 且 num_waiting_reqs == 0（空闲迭代）：
         若提交通道已断 ⇒ 引擎已排空，退出线程
         否则 std::hint::spin_loop()（让出发射槽，下次再探测；忙迭代永远不会走到这）
}
```

两条退出路径：**优雅关停**（前端 handle 全部 drop → 提交通道断连 → 调度器报告已排空）与**致命错误**（step 返回 Err → fail_all 核销 → 退出）。注意循环**从不睡眠、从不阻塞等待**——空闲时只是 `spin_loop` 提示后立刻再探测；调度器自己的工作线程在飞行中完成的产出，会在下一步的 `step` 里被捞起来（注释：「an idle scheduler returns quickly and gets polled again, so in-flight completions from the scheduler's own worker threads are picked up within a step」）。这与 CLAUDE.md 中 EP free-running 的「不停等」纪律一脉相承。

#### 4.3.3 源码精读

**trait 定义**。[driver.rs:20-41](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/driver.rs#L20-L41)：三个方法的完整文档注释就是契约文本，值得逐句读——尤其是 `step` 注释里对「可恢复失败 vs 致命错误」的二分：前者调度器自己 fail 掉被触碰的请求继续服务，后者驱动核销**每一个**开账户（包括调度器自己弄丢track的——因为账本为每个未应答请求留着账户，`fail_all` 必然覆盖到它们）。

**spawn_scheduler**。[driver.rs:45-52](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/driver.rs#L45-L52)：铸造 `scheduler_pair()` 接线 → 用指定名字起 OS 线程跑 `drive`（线程名会出现在 `top`/gdb 里，便于运维定位）→ 返回 `LiveScheduler { handle, join }`。

**提交排空**。[driver.rs:66-79](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/driver.rs#L66-L79)：内层循环 `try_recv` 直到 `Empty`（先把手头全部收下再进步）或 `Disconnected`（记下前端已走，但**不当即退出**——先把已收请求服务完）。

**迭代主干与致命处理**。[driver.rs:80-92](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/driver.rs#L80-L92)：注意顺序——`step` 先跑，`metrics` 与 `commit_step` 随后各一次；`Err` 分支用 `fail_all` 携带真实错误核销（「with the real error attached」，对应测试 `fatal_step_fails_in_flight_requests_with_the_error` 断言消息里含 `injected fatal`）。

**空闲与退出**。[driver.rs:93-103](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/driver.rs#L93-L103)：以 `SchedulerMetrics` 的两个计数判断空闲；`std::hint::spin_loop` 的注释解释了为什么不用 sleep——忙迭代永远到不了这个提示，空闲迭代用它放松核心的发射槽，延迟不受影响。

**EchoScheduler：最小合规实现**。[driver.rs:115-160](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/driver.rs#L115-L160) 是理解 trait 的最佳样本：`submit` 只入队；`step` 里先 admit 全部 `queued`，再对每个 `running` 检查 `is_aborted`（retire）→ `push_tokens`（用 `ledger.completion_tokens(id)` 当下一个 token，**token 计数直接问账本**）→ 达到 `max_tokens` 就 `finish`；`metrics` 报告两个队列长度。约 40 行就是一个语义完整的调度器。

#### 4.3.4 代码实践（运行型：驱动循环端到端）

1. **实践目标**：运行 driver 的两个集成测试，观察「专用线程上的完整生命周期」——从 submit 到 step 流收齐 token 与终态，再到关停排空。
2. **操作步骤**：
   ```bash
   cargo test -p pegainfer-frontend --lib engine::driver -- --nocapture
   ```
3. **需要观察的现象**：两个测试通过：
   - `driven_engine_streams_and_drains_on_shutdown`：断言收到 token 序列 `[0, 1, 2]` 且终态为 `Finished { reason: Length, prompt_tokens: 2, completion_tokens: 3 }`；随后 drop handle、join 驱动线程成功（优雅关停路径）；
   - `fatal_step_fails_in_flight_requests_with_the_error`：注入致命错误后，在飞请求收到 `Failed` 终态且消息含 `injected fatal`（致命路径）。
4. **预期结果**：2 passed。测试体内 `spawn_scheduler("test-echo", ...)` 起了真实 OS 线程，你可以用 `--nocapture` 加日志观察驱动线程名。**待本地验证**（输出格式因编译器版本略有差异）。
5. 加深：把 `EchoScheduler` 抄到一个临时 bin/target 里（或直接改测试本地副本），故意在 `finish` 之后再 `push_tokens` 一次，重新运行——预期 panic 消息 `no open account for req-N`，亲手验证账本执法。

#### 4.3.5 小练习与答案

**练习 1**：`Scheduler::submit` 为什么可以「只转移所有权、把裁决推迟到 step」而不丢失任何信息或时效？

**答案**：见 [driver.rs:22-27](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/driver.rs#L22-L27) 注释：驱动每迭代 `commit_step` 一次，而同一次迭代内「submit 阶段记的裁决」与「step 阶段记的裁决」折叠进**同一条** step 消息——对前端不可区分。推迟裁决换来的是：trait 方法里无需携带账本参数、裁决逻辑集中在唯一的发出点 `step`。

**练习 2**：驱动循环空闲时为什么用 `std::hint::spin_loop()` 而不是 `std::thread::sleep` 或阻塞式 `recv`？

**答案**：三个理由（[driver.rs:93-102](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/driver.rs#L93-L102)）：① sleep 引入最小延迟下限，而 spin_loop 只是放松发射槽的提示、延迟不受影响；② 阻塞 recv 会在「调度器自身工作线程飞行中完成产出」的场景下错过及时唤醒——轮询保证任何飞行完成物在一个 step 内被捞起；③ 忙迭代（有活干）永远走不到这个分支，所以空转功耗只存在于真正空闲的时刻。这也与项目级「EP free-running：绝不停等」的纪律一致（见 docs/models/glm52/free-running-dp.md）。

**练习 3**：`step` 返回 `Err` 后，驱动做了哪四件事？为什么 `fail_all` 能覆盖「调度器自己已经弄丢 track 的请求」？

**答案**：① `log::error!` 记录；② `ledger.fail_all(真实错误)`；③ 再 `commit_step()` 把核销消息发出去；④ `return` 结束驱动线程。`fail_all` 能全覆盖是因为账本为**每个未应答请求**独立持有账户——无论调度器的内部数据结构是否还记得它（[driver.rs:84-89](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/driver.rs#L84-L89) 注释原文「the write-off reaches them all — including any the scheduler lost track of」）。这是「账本是唯一事实来源」设计的直接红利。

### 4.4 engine::wiring + request_lifecycle —— 接线、drop bomb 与 LaunchedEngine

#### 4.4.1 概念说明

接线层回答三个问题：

1. **通道从哪来？** `scheduler_pair()` 一次性铸造一个调度器的两端——调度器端是 `SchedulerBackend`（提交通道接收端 + 账本 + 指标发布器），前端端是 `SchedulerHandle`。两端一起出厂，模型 crate **无法交叉接线或漏接一根线**。每个方向的通道选型都有明确理由（[wiring.rs:1-12](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/wiring.rs#L1-L12) 模块注释）：提交通道用 crossbeam（调度器线程是同步消费者；无界通道上发送方永不阻塞）；step 流用 tokio mpsc（协议栈是异步消费者；同步生产者的 send 同样永不阻塞）；指标用共享 cell（只读拉取，刻意不可订阅——驱动忙轮询下 `watch` 的变更通知会变成消息洪灾）。
2. **请求在账本管不到的地方谁来兜底？** 这是 `request_lifecycle.rs` 的主题：信封在提交通道里飞、或被发给一个已退出的调度器线程时，还没有账本账户；`DeferredFinish` 被扣在线程外时，已离开账本管辖。这两处各配一枚 drop bomb。
3. **引擎怎么交还给协议栈？** `ModelLine::launch` 返回 `LaunchedEngine::Stepped(Engine)`——`Engine` 捆绑运行中的调度器列表、`EngineInfo`（KV 容量 / 可服务长度，显式 `None` 表示不报，不能「忘了说」）与可选的 LoRA 控制端。一个引擎跑几个调度器、每个是什么含义（DP 副本？别的？）**是模型线自己的决定**，契约不附加 rank 语义；跨调度器的放置策略是前端策略——对照旧契约里 `EngineHandle` 自己做路由，这是职责的重新划界。

#### 4.4.2 核心流程

一次 submit 的完整防护链：

```text
SchedulerHandle::submit(request)
  ① next_id.fetch_add ⇒ RequestId
  ② Arc<AtomicBool> 新建 abort 标志 ⇒ RequestControl 交给调用方
  ③ RequestEnvelope::new(id, abort, step_tx 克隆, request, Instant::now())
  ④ submit_tx.send(envelope)
       ├─ Ok：信封被驱动 drain → register 消费（拆除炸弹）→ 开户
       └─ Err(调度器线程已死)：返回的信封被 drop(returned.into_inner())
              ⇒ RequestEnvelope::drop 炸弹引爆
              ⇒ 直接向 step 流补发 Terminal::Failed
                 "request dropped by the engine before it was answered"
  ⇒ submit 永不失败，调用方拿 RequestControl 观察终态即可
```

三枚「答案守护」机制的责任区划分：

| 机制 | 覆盖窗口 | 兜底动作 |
|------|---------|---------|
| `RequestEnvelope` 的 Drop | 请求已存在但**尚无账本账户**（躺在通道里 / 发给已死的调度器线程） | 补发 `Failed`（[request_lifecycle.rs:84-99](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/request_lifecycle.rs#L84-L99)） |
| `RequestLedger` 的 Drop | 账户已开但账本整体析构（引擎 bug / 拆除竞态） | 每账户一条 `Failed` 核销（4.2 已讲） |
| `DeferredFinish` 的 Drop | 终态已被抽出账本、等待晚投递，但持有者没投就扔了 | token 照发，但终态**改写**为 `Failed`——扣着的 `Finished` 兼作 P/D 的 KV-ready 屏障信号，绝不能伪造成功（[request_lifecycle.rs:149-171](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/request_lifecycle.rs#L149-L171)） |

`Option<inner>` 的惯用法：Rust 的 `Drop` 不能移出字段，所以消费型转换（`consume` / `send`）先 `take` 走 inner，drop bomb 只在 `Some` 上引爆（[request_lifecycle.rs:19-22](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/request_lifecycle.rs#L19-L22) 模块注释专门解释这一点）。

#### 4.4.3 源码精读

**两端铸造**。[wiring.rs:108-126](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/wiring.rs#L108-L126) `scheduler_pair`：一条 crossbeam 无界提交通道 + 一条 tokio 无界 step 流 + 一个 `Arc<Mutex<SchedulerMetrics>>` cell；注意 `SchedulerHandle` 里额外克隆了一份 `step_tx`（[wiring.rs:70-72](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/wiring.rs#L70-L72)）——**调度器线程退出后**新铸造的请求其信封炸弹仍需要一个活的发送端才能把终态送出去。

**`SchedulerHandle::submit`：永不失败**。[wiring.rs:80-90](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/wiring.rs#L80-L90)：铸造三件套（id、abort、信封）后发送；发送失败时 `drop(returned.into_inner())` **主动引爆**信封炸弹。对照旧契约 `EngineHandle::submit` 的越界 rank 补发 `Scheduled→Rejected` 行为（u3-l1）：新契约的「失败」统一收敛为 step 流上的 `Failed` 终态，调用方用同一套折叠逻辑处理，无需特殊分支。`take_steps`（[wiring.rs:94-96](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/wiring.rs#L94-L96)）只交一次——step 流只有一个消费者（协议栈的翻译循环）；`metrics()`（[wiring.rs:101-103](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/wiring.rs#L101-L103)）是拉取式快照。

**指标为什么不是 watch 通道**。[wiring.rs:45-61](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/wiring.rs#L45-L61) `MetricsPublisher` 的文档注释是一堂小型并发设计课：驱动忙轮询 ⇒ `watch::changed()` 每次自旋都触发 ⇒ 订阅者被消息洪灾淹没；所以刻意做成普通 cell——「metrics 变了通知我」这个需求**不可表达**，消费者在需要的时刻拉快照；用 `Mutex` 而非逐字段原子是为保证读者绝不看到跨两步撕裂的字段组合。

**信封与炸弹**。[request_lifecycle.rs:44-99](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/request_lifecycle.rs#L44-L99)：`RequestEnvelope` 包着 `Option<EnvelopeInner>`；`consume` 拆弹；`Drop` 在 `Some` 上补发 `Failed`。

**`DeferredFinish`：能扣、能发、不能扔**。[request_lifecycle.rs:108-171](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/request_lifecycle.rs#L108-L171)：`send()` 消费式投递整条记录；`Drop` 未发送则把终态改写为 `Failed`（文档注释：withheld 的 `Finished` 兼作 P/D KV-ready 屏障信号，扔掉的句柄**不得伪造成功**）。

**`RequestControl`：唯一的取消机制**。[request_lifecycle.rs:178-198](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/request_lifecycle.rs#L178-L198)：`abort()` 以 `Release` 序存储（排在调用方自己拆完 per-request 状态之后），镜像调度器侧 `is_aborted` 的 `Acquire` 载入——内存序 here 是有语义的，不是装饰。与旧契约一致：取消不关频道（对照 u3-l1 TokenSink 的共享频道设计），调度器响应式退役。

**`Engine` 捆绑包与 `LaunchedEngine`**。[wiring.rs:132-147](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/wiring.rs#L132-L147)：`Engine { schedulers: Vec<LiveScheduler>, info: EngineInfo, lora: Option<LoraClient> }`——注释称必填字段是「onboarding checklist」：不报容量或可服务长度的引擎必须显式用 `None` 声明，不能靠遗忘蒙混；`lora` 的 `Option` 本身就是能力位，不存在与之矛盾的独立开关。`LiveScheduler` 持 handle 与驱动线程 JoinHandle（服务器在关停时、drop 掉全部 handle 之后 join）。[wiring.rs:152-155](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/wiring.rs#L152-L155) 的 `LaunchedEngine` 是迁移期的双臂枚举：`Handle`（旧）或 `Stepped`（新），注释明确「全部模型线迁移完成后删除（只留 `Engine`）」；消费点在 [pegainfer-frontend/src/vllm/mod.rs:277](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/mod.rs#L277)，按臂分发到对应协议栈入口——这正是下一讲（u3-l3）的起点。

#### 4.4.4 代码实践（源码跟踪型：submit 到死调度器的防护链）

1. **实践目标**：完整跟踪「向已退出的调度器提交请求」的每一步防护，验证 4.4.2 的流程图。
2. **操作步骤**：
   - 阅读 [wiring.rs:80-90](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/wiring.rs#L80-L90) 的 `submit`，找到 `Err` 分支里 `drop(returned.into_inner())` 这一行；
   - 进入 [request_lifecycle.rs:84-99](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/request_lifecycle.rs#L84-L99) 的 `Drop for RequestEnvelope`，确认补发的 `StepOutputs` 从哪个发送端（`inner.tx`，即 handle 里克隆的 `step_tx`）走哪条流；
   - 最后对照测试 [ledger.rs:550-563](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/ledger.rs#L550-L563) `submit_to_a_dead_scheduler_fails_the_request`：`drop(backend)` 杀死调度器端 → `handle.submit(...)` → 从 step 流收到 `Failed { prompt_tokens: 4, .. }`。
3. **需要观察的现象**：三处代码首尾相接——submit 的 Err 分支、信封的 Drop、测试断言——构成一条不经过账本的独立应答通路。
4. **预期结果**：能回答「为什么 `SchedulerHandle` 要额外持有一份 `step_tx` 克隆」——调度器线程死后其 `SchedulerBackend`（含账本持有的发送端）已随线程 drop，没有这份克隆，炸弹的终态将无处可发。测试可通过 `cargo test -p pegainfer-frontend --lib submit_to_a_dead_scheduler` 验证（**待本地验证**）。
5. 延伸：同法跟踪 `DeferredFinish` 的 Drop 改写路径（[request_lifecycle.rs:149-171](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/request_lifecycle.rs#L149-L171)），并解释为什么它不像信封那样发空记录，而是**保留已缓冲 token、只改写终态**。

#### 4.4.5 小练习与答案

**练习 1**：提交通道选 crossbeam、step 流选 tokio mpsc、指标选共享 cell——三个选择的共同约束是什么？

**答案**：共同约束是**生产者（调度器线程）绝不被阻塞**：crossbeam 无界通道上同步 send 永不阻塞（慢的前端不卡 GPU 调度）；tokio 无界 mpsc 的同步 `send` 也不阻塞（协议栈异步消费，背压由无界队列的内存上界承担）；指标做拉取式 cell 则避免了「订阅变更通知」在忙轮询驱动下退化为消息洪灾（[wiring.rs:5-12](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/wiring.rs#L5-L12) 与 [wiring.rs:48-54](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/wiring.rs#L48-L54) 的注释给出了全部三个理由）。

**练习 2**：`LaunchedEngine` 为什么是一个 enum 而不是让所有模型线直接返回新契约的 `Engine`？

**答案**：因为迁移还没完成（[wiring.rs:149-152](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/wiring.rs#L149-L152) 注释：「Deleted (in favor of `Engine` alone) once every model line is migrated」；engine/mod.rs 也写明当前只有 qwen3 已迁移，其余线跟进中）。enum 让已迁移的线返回 `Stepped`、未迁移的继续返回 `Handle`，服务器与协议栈在分发点按臂选择入口（vllm/mod.rs:277），新旧契约在同一个代码库里共存到迁移收尾——这与 u2-l1 讲过的 `ServePlan` 分发是同一个「迁移期用类型系统显式表达两态」的手法。

**练习 3**：`RequestControl::abort()` 用 `Ordering::Release`、`is_aborted` 用 `Ordering::Acquire`，这对内存序各自承诺什么？

**答案**：`Release` 存储承诺：调用方在 `abort()` 之前对自己那份 per-request 状态（取消清理等）的全部写入，对随后 `Acquire` 载载到 `true` 的调度器线程可见（[request_lifecycle.rs:192-196](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/request_lifecycle.rs#L192-L196) 注释：「orders the store after the caller's own teardown of per-request state, mirroring the scheduler's `Acquire` load」）。即：调度器看到 abort 位时，前端承诺的清理副作用已一并可见；反之若两侧都用 `Relaxed`，调度器可能观察到 abort 却看不到相关清理，产生撕裂视图。

## 5. 综合实践

**任务：亲手梳理 RequestLedger 的状态机，并回答两个「为什么」。** 这是本讲规格指定的核心实践。

### 步骤

1. **画出状态机图**。以 `RequestEnvelope`（提交前）→ `Queued` → `Active` → 各终态为骨架，把以下每个方法的**出发态、到达态、对 step 流的可见效果、非法调用时的行为**标注在边上：
   - `register`（信封 → Queued；驱动专属）
   - `admit`（Queued → Active；缓冲 `ScheduledInfo`）
   - `reject`（Queued → 终态 `Rejected`；非法于 Active）
   - `push_tokens` / `set_cached_tokens` / `echo_prompt` / `kv_transfer`（Active 上的自环记账）
   - `finish`（Active → 终态 `Finished`；计数取 tally）
   - `fail`（任意态 → 终态 `Failed`）
   - `retire`（任意态 → 静默销账，丢弃本步缓冲）
   - `defer_finish`（Active → 账户关闭 + `DeferredFinish` 晚投递）
   - 补充两条全局边：`Drop`（任意开账户态 → 核销 `Failed`）、驱动 `fail_all`（fatal 后批量 `fail`）。
2. **验证图**：把你画的图与 ledger.rs 的 8 个单测逐一对照——每条边至少被一个测试覆盖（如 `admit→push_tokens→finish` 折叠边对应 `admission_tokens_and_finish_fold_into_one_entry`；`retire` 边对应 `retire_discards_buffered_output`……）。缺了哪条边就回 [ledger.rs](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/ledger.rs) 找答案。
3. **回答问题 A：为什么 `finish` 必须恰好一次？** 从前端折叠侧论证：终端消费者（协议栈 → SSE）对每个请求维护一份流状态，收到 `terminal` 即完成收尾并删除状态；第二个终态将找不到可写之处——若被静默忽略则掩盖调度器 bug，若被报错则污染正常流。零个终态同样不可接受：客户端永远等待（挂死）。账本把「销户 = 终态」绑在一起，让违反在调用点 panic（参考 [ledger.rs:566-575](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/ledger.rs#L566-L575) 的 `#[should_panic]` 测试）。
4. **回答问题 B：`defer_finish` 用于什么场景？为什么必须把整条缓冲记录（含 token）一起抽走？** 场景见 [ledger.rs:277-280](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/engine/ledger.rs#L277-L280) 注释：P/D 分离部署的 prefill 角色要把 `Finished` 扣到「本步的 KV 保存已对 peer 可见」才放行（decode 侧要能无缝续跑），所以终态需要**从任意线程、更晚时刻**投递。整条抽取的原因是防乱序/防重复：若 token 仍随当步 `commit_step` 发出、终态单独晚发，晚发消息与 step 流之间没有顺序保证；把 per-request 的全部事实折叠进一条 `DeferredFinish` 消息，则该请求的完整记录要么整体早到、要么整体晚到，且只出现一次（对照测试 `defer_finish_folds_step_output_and_delivers_late`：当步不发任何该请求的消息）。
5. **交付物**：一张状态机图 + 两段论证文字。若你所在环境可编译，用 `cargo test -p pegainfer-frontend --lib engine::ledger` 的 8 个绿灯作为图完整性的最终校验（**待本地验证**）。

## 6. 本讲小结

- **一步一条消息**：`Scheduler` 每个调度步对前端至多发一条 `StepOutputs`；每个被触碰的请求在其中至多占一条扁平 `RequestUpdate`——旧 `TokenEvent` 协议靠约定维持的步内顺序（准入→token→唯一终态）被压平成结构，类型上无法违反。
- **`RequestLedger` 是执法者**：每个未应答请求一个账户（`Queued → Active → 终态`），销户即终态；触碰已关闭账户在调用点 panic，从而强制 terminal 恰好一次；token 计数由账本 tally，单一事实来源；开账户（`register`）与发对账单（`commit_step`）是 `pub(crate)`，节奏归驱动。
- **五个终结转换各司其职**：`finish`（正常，仅 Active）、`reject`（准入拒绝，仅 Queued）、`fail`（引擎错误，两态皆可）、`retire`（abort 后静默销账）、`defer_finish`（P/D 场景的线程外晚投递，整条记录折叠防乱序）。
- **`Scheduler` trait 只有三个方法**，跑在 `spawn_scheduler` 起的专用 OS 线程上；`drive` 循环只写一份、所有模型线共用：排空提交 → `step` → 发布指标 → `commit_step`；致命错误 `fail_all` 核销全部开账户后收线；空闲只有 `spin_loop` 提示、绝不停等。
- **接线由 `scheduler_pair` 一次性铸造**：crossbeam 提交通道 + tokio step 流 + 共享指标 cell，三个选型共同保证调度器线程永不阻塞；`RequestEnvelope` / `DeferredFinish` / ledger `Drop` 三枚 drop bomb 覆盖请求生命周期中账本管辖之外的每个窗口——「未应答的请求绝不无声消失」。
- **迁移期形态**：`LaunchedEngine::Handle | Stepped(Engine)` 双臂共存，qwen3 已迁到新契约，其余模型线跟进；`Engine` 捆绑调度器列表 + `EngineInfo`（显式 `None` 声明不报）+ 可选 LoRA 能力位。

## 7. 下一步学习建议

本讲结束于 `LaunchedEngine::Stepped(engine)` 被交还——**下一讲 u3-l3（vLLM 协议栈与 ZeroMQ 桥）** 正从这一点出发：`SteppedEngineBridge` 如何消费 step 流、把 `RequestUpdate` 翻译回 vLLM 的 `EngineCoreOutputs` wire 格式并流成 SSE。阅读建议：

1. 先读 [pegainfer-frontend/src/vllm/mod.rs:277](https://github.com/openinfer-project/openinfer/blob/139d925e8693dcff412b8501f8db09eb639d0ffd/pegainfer-frontend/src/vllm/mod.rs#L277) 附近的双臂分发，体会新旧桥如何并存。
2. 若想立刻看一个真实（而非测试）的 `Scheduler` 实现，可提前跳读 `pegainfer-qwen3/src/frontend_adapter.rs`（u6-l2 的主题）——qwen3 是新契约的第一个生产迁移者。
3. 指标面：`SchedulerMetrics` 与 `SpecDecodeCounters` 的完整定义在 `pegainfer-frontend/src/engine/metrics.rs`，其到 Prometheus 的通路在 u10-l4 展开。
