# 接线与路由：scheduler_pair、Engine 与 legacy EngineHandle

## 1. 本讲目标

[u3-l5](u3-l5-scheduler-driver.md) 讲了 `Scheduler` 三方法与 `drive` 轮询循环，结尾留了一个悬而未决的角色：`scheduler_pair`——那个「一次铸出双端」的接线函数。本讲把它彻底拆开，并沿着它向上走到交接包 `Engine`/`EngineInfo`，向旁走到仍在服役的 legacy `EngineHandle`。读完本讲你应当能够：

1. **解释 `scheduler_pair` 两侧的通道选型**：提交用 crossbeam（同步消费者在调度器线程上）、步流用 tokio mpsc（异步消费者在协议栈里）、metrics 用 `Arc<Mutex>` 共享单元（读者只要最新值，变化经步流上的空消息信号）——三个方向三种通道，各有各的理由。
2. **说出 `Engine`/`EngineInfo`/`LaunchedEngine` 的装配关系**：`launch` 交回的交接包长什么样、协议栈在哪个点把它溶解、`EngineInfo` 两个 `Option` 字段（`kv_capacity`、`servable_len`）的**显式 None 语义**各是什么。
3. **推导 legacy `EngineHandle` 的路由决策树**：绑定 rank 优先（已解析前缀的 rank 压倒一切，不一致即调用方 bug）、无绑定走 \( \text{running} + 4 \times \text{waiting} \) 最低分、越界 rank 以 `Scheduled → Rejected` 事件对应答而非丢请求或报错。
4. **亲手复现** handle.rs 里的 least-loaded 多分区路由测试（三分区版本），并用 `KvPrefix` 的 rank 语义解释 `submit_resolved` 里那条 `debug_assert` 为什么把「前缀 rank 与调用方绑定不一致」定性为调用方 bug。

本讲承接 [u3-l5](u3-l5-scheduler-driver.md)（driver 循环）与 [u3-l2](u3-l2-legacy-wire.md)（legacy 线类型），是单元四 [u4-l1](u4-l1-serve-paths.md)（serve 装配层如何溶解 `Engine`）的直接前置。

## 2. 前置知识

### 2.1 同步通道与异步通道：消费者的线程类型决定选型

Rust 生态里两大通道家族的差别不在「能不能传数据」，而在**接收端以什么姿态等数据**：

- **crossbeam channel**：为普通 OS 线程设计的同步通道。`try_recv()` 非阻塞试探、`recv()` 阻塞等待，都不需要任何异步运行时。PegaInfer 的调度器线程是一个**裸的 `std::thread`**（[u3-l5](u3-l5-scheduler-driver.md) 的 `spawn_scheduler`），上面跑的是无停顿的轮询循环——它天然适合 crossbeam。
- **tokio mpsc**：为异步任务设计的通道。`recv().await` 会挂起当前异步任务而不是堵死线程，因此**消费者必须活在 tokio 运行时里**。协议栈的翻译循环（单元四的引擎桥）跑在 tokio 里，所以步流用它。

两个家族的**无界**变体还有一个共同点：`send()` 永不阻塞（不等待容量），代价是没有背压——本讲涉及的通道全部选无界，因为「提交永不被反压挡住」正是引擎前门想要的性质。

### 2.2 共享单元 + 变化信号：metrics 为什么不用通道

metrics 的读者（路由决策、`/metrics` 抓取）不想要一条历史流，只想**最新快照**。用通道传 metrics 会积压过时值、还得排空；用一个 `Arc<Mutex<T>>` 共享单元，写者覆盖、读者直读，天然「只要最新」。剩下的问题是「读者怎么知道该重读了」——答案是复用步流：快照变化时随 `commit_step` 发一条 `updates` 为空的 `StepOutputs` 唤醒消息（[u3-l5](u3-l5-scheduler-driver.md) 4.2 已验证过它的存在）。

legacy 引擎的对应物是 **tokio watch** 通道：watch 也只保留最新值，`changed().await` 只在值真实变化时唤醒（「under load 时每步醒一次、空闲时安静」）。本讲会在 4.3 看到 legacy 引擎如何把一组 watch 接到 `EngineHandle` 上。

### 2.3 RAII hold 与 debug_assert：两个本讲要用到的 Rust 习惯

- **RAII hold**：一个只在被 drop 时才释放资源的值。`KvPrefix` 里的 `hold: Option<Box<dyn Any + Send>>` 就是一个不透明 hold——它钉住（pin）前缀缓存里的块，防止被驱逐；谁最后持有 `KvPrefix`，drop 它时钉子就拔掉。「不透明」指 frontend 这个 crate 不 `downcast` 它、不知道里面是什么（本 crate 无 CUDA），只有铸造它的 KV 存储认识它。
- **`debug_assert!`**：只在 debug 构建启用的断言，release 里编译为空。用它标记「这条不变量由调用方保证，违反它是 bug 而非运行时故障」——4.3 会看到 `submit_resolved` 用它检查「前缀 rank 与调用方绑定一致」。

### 2.4 回顾：本讲要接上的三块拼图

| 来自 | 结论 | 本讲怎么用 |
|---|---|---|
| [u3-l1](u3-l1-contract-overview.md) | `LaunchedEngine` 是迁移期二选一枚举：`Handle`（legacy）/ `Stepped`（新） | 4.2 精读它的定义与两个 `From` 实现 |
| [u3-l2](u3-l2-legacy-wire.md) | legacy 线类型：`GenerateRequest`（含 `data_parallel_rank` 路由指令、`token_tx` 事件出口）、`TokenEvent`（`Scheduled`/`Rejected` 等七变体）、`TokenSink::standalone` | 4.3 的路由与拒绝应答全部落在这些类型上 |
| [u3-l5](u3-l5-scheduler-driver.md) | `drive` 每迭代四拍；`commit_step` 至多发一条 `StepOutputs`；信封 drop 炸弹 | 4.1 解释三通道各自被谁碰、什么时候 |

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用途 |
|---|---|---|
| `pegainfer-frontend/src/engine/wiring.rs` | **主角** | `scheduler_pair`、`SchedulerHandle`/`SchedulerBackend`、`MetricsPublisher`、`Engine`/`LiveScheduler`/`EngineInfo`、`LaunchedEngine` |
| `pegainfer-frontend/src/engine/handle.rs` | **主角** | legacy `EngineHandle`：构造器族、`submit`/`submit_resolved` 路由、`least_loaded_partition`、`reject_unroutable`、`Drop` join，以及五个多分区测试 |
| `pegainfer-frontend/src/engine/kv.rs` | **主角** | `KvPrefix` 的 rank/hold 语义、`SubmittedRequest`、`KvCapacity` |
| `pegainfer-frontend/src/engine/driver.rs` | 辅：装配 | `spawn_scheduler` 调 `scheduler_pair`（[u3-l5](u3-l5-scheduler-driver.md) 已精读循环本体） |
| `pegainfer-frontend/src/engine/request_lifecycle.rs` | 辅：线类型 | `StepSender`/`StepReceiver`、`RequestEnvelope`（信封为何需要活的发送端） |
| `pegainfer-frontend/src/engine/mod.rs` | 辅：地图 | 模块分组与扁平 re-export 约定（[u3-l1](u3-l1-contract-overview.md) 已讲，这里只取 handle/wiring 的分组定位） |
| `pegainfer-frontend/src/vllm/mod.rs` | 辅：消费点 | serve 装配层溶解 `LaunchedEngine` 的两个分支（单元四主讲，本讲只取「交接包被谁拆开」） |
| `pegainfer-qwen3/src/frontend_adapter.rs` | 参照 | step 引擎真值版 `Engine`/`EngineInfo { Some, Some }` 的构造 |
| `pegainfer-glm52/src/lib.rs` | 参照 | legacy 引擎的 builder 链：`new_with_join_handles(...).with_servable_len(...).with_kv_capacity(...).with_metrics_watches(...)` |
| `pegainfer-frontend/examples/echo-server.rs` | 参照 | 最小 step 引擎：`EngineInfo { None, None }`（[u3-l5](u3-l5-scheduler-driver.md) 已引用） |

## 4. 核心概念与源码讲解

### 4.1 scheduler_pair：一次铸出双端接线

#### 4.1.1 概念说明

一个「运行中的调度器」需要三条线路对外：请求怎么进来（提交）、结果怎么出去（步流）、状态怎么被看见（metrics），外加一条「它还活着吗」（exited）。这些线路如果由模型 crate 自己组装——拿一个通道接这里、拿一个通道接那里——就存在接错线、忘接线的可能。wiring.rs 的解法是**铸造而非组装**：

> [wiring.rs:L1-L17](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/wiring.rs#L1-L17) —— 模块文档：「Both ends of a scheduler are minted together by `scheduler_pair`, so a model crate cannot cross-wire or forget a line」（两端由 `scheduler_pair` 一体铸出，模型 crate 无法接错线或漏线）。文档同时给出三个方向的通道选型：提交 crossbeam（调度器线程上的同步消费者）、步流 tokio（协议栈里的异步消费者；同步生产者的 send 同样永不阻塞）、metrics 共享单元（变化经步流信号）。

三个方向的选型理由展开成一张表：

| 方向 | 通道类型 | 生产者 | 消费者 | 选型理由 |
|---|---|---|---|---|
| 提交 | crossbeam 无界 channel | 前端 `SchedulerHandle::submit`（任意线程） | `drive` 循环（裸 OS 线程，`try_recv` 排空） | 消费者是同步自旋循环，crossbeam 的非阻塞 `try_recv` 直接可用；无界 → 发送端永不阻塞 |
| 步流 | tokio 无界 mpsc | 账本（`commit_step`，调度器线程） | 协议栈翻译循环（tokio 异步任务） | 消费者必须能 `.await`；无界 → 同步生产者的 `send` 也永不阻塞 |
| metrics | `Arc<Mutex<SchedulerMetrics>>` | `MetricsPublisher::publish`（driver 每迭代一次） | 前端按需读（路由/抓取） | 读者只要最新快照；「该重读了」的唤醒复用步流空消息 |
| 退出 | `CancellationToken` + `DropGuard` | driver 线程返回时 drop 守卫 | `handle.exited().await` | DropGuard 把「线程没了」自动变成可等待事件 |

文档最后两句还划了一条重要的责任边界：「跑几个调度器、每个是什么含义（DP 副本？）是模型线的决定；契约只搬运这个集合、不附加 rank 语义。**跨调度器的放置是前端策略**」——这句话在 4.3 会得到镜像：legacy 侧的放置策略（least-loaded）恰恰写在 `EngineHandle` 里，因为 legacy 前端自己就是放置者。

#### 4.1.2 核心流程

`scheduler_pair()` 铸造时的资源流向：

```
scheduler_pair()
 │
 ├─ crossbeam::unbounded::<RequestEnvelope>()      → 提交双端
 │     sender → SchedulerHandle.submit_tx
 │     receiver → SchedulerBackend.submissions
 │
 ├─ tokio::mpsc::unbounded_channel::<StepOutputs>() → 步流双端
 │     sender ─┬→ SchedulerHandle.step_tx（保活的 clone：调度器死后信封炸弹还要用）
 │            └→ RequestLedger::new(step_tx)   ← 账本出生即握发送端
 │     receiver → SchedulerHandle.steps: Option<StepReceiver>（take_steps 只发一次）
 │
 ├─ Arc<Mutex<SchedulerMetrics::default()>>        → metrics 共享单元
 │     一份 clone → SchedulerHandle.metrics（读）
 │     一份       → SchedulerBackend.metrics = MetricsPublisher（写）
 │
 └─ CancellationToken + drop_guard                 → 退出信号
       token → SchedulerHandle.exited
       guard → SchedulerBackend.exit_guard（driver 线程返回时 drop → token 取消）
```

前端侧一次 `submit(request)` 的完整路径（把 [u3-l5](u3-l5-scheduler-driver.md) 的循环接上）：

```
SchedulerHandle::submit(request)
 │ ① RequestId::new(next_id.fetch_add(1))     ← 每个调度器独立的原子 id 铸币机
 │ ② Arc::new(AtomicBool) → RequestControl    ← 返回给调用方的 abort 开关
 │ ③ RequestEnvelope::new(id, abort, step_tx.clone(), request, Instant::now())
 │ ④ submit_tx.send(envelope)
 │      成功 → drive 循环 try_recv 到 → register 开户 → scheduler.submit
 │      失败（调度器已死）→ drop 退回的信封 → 信封 drop 炸弹
 │                           在步流上以 Terminal::Failed 回答该请求
 └─ 返回 RequestControl（submit 永不返回错误）
```

#### 4.1.3 源码精读

> [wiring.rs:L116-L139](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/wiring.rs#L116-L139) —— `scheduler_pair` 全部 22 行：三条线路 + 一个退出令牌一次铸出，返回 `(SchedulerHandle, SchedulerBackend)`。注意 `#[must_use]`：铸出双端却丢掉一边几乎必是 bug。`RequestLedger::new(step_tx)` 是关键一笔——**账本从出生就握着步流发送端**，`commit_step` 能发消息的原因在此；`exit_guard: exited.drop_guard()` 则把「driver 线程返回」变成「`exited` 令牌取消」。

> [wiring.rs:L43-L49](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/wiring.rs#L43-L49) —— `SchedulerBackend`：调度器线程消费与生产的全部资源（提交通道接收端、账本、metrics 发布器、退出守卫）。字段全 `pub(crate)`——模型代码唯一摸得到的部件是账本，且只经 `step` 的参数（模块文档原话）。

> [wiring.rs:L67-L77](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/wiring.rs#L67-L77) —— `SchedulerHandle` 的六个字段，三个值得停下来看：`steps: Option<StepReceiver>`（`Option` 即「步流只发一次」的类型化表达）；`next_id: AtomicU64`（每个调度器一套 id 序列，进程内路由用，[u3-l3](u3-l3-step-wire.md)）；`step_tx`（字段注释明说：留着它，「调度器线程退出后铸造的请求仍能拿到 drop 炸弹的终态——信封需要一个活的发送端」）。

> [wiring.rs:L85-L95](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/wiring.rs#L85-L95) —— `submit`：铸造 id、abort 标志与信封，然后发送。文档第一句就是契约：「Never fails: if the scheduler is gone, the `RequestEnvelope`'s drop bomb answers the request with a `Failed` terminal on the step stream」。发送失败时代码 `drop(returned.into_inner())` 主动引爆信封炸弹——错误以普通 terminal 形态流出，与正常终态同一条通路。

> [wiring.rs:L97-L113](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/wiring.rs#L97-L113) —— 前端端另外三个入口：`take_steps`（「handed out once — one stream, one consumer」，`self.steps.take()` 的 `Option` 语义）；`metrics()`（锁一下、拷一份快照出来）；`exited()`（返回 `CancellationToken::cancelled()` 的 future，driver 返回或线程 unwind 后解析）。

> [request_lifecycle.rs:L35-L38](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/request_lifecycle.rs#L35-L38) —— 步流两端的类型别名：`StepSender`/`StepReceiver` 就是 `tokio::sync::mpsc` 的无界收发端，载荷 `StepOutputs`。「每步一条消息」的约定（[u3-l5](u3-l5-scheduler-driver.md)）就压在这个通道的载荷类型上。

> [driver.rs:L43-L52](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/driver.rs#L43-L52) —— 唯一的调用点：`spawn_scheduler` 里 `let (handle, backend) = scheduler_pair();`——backend 被 move 进 driver 线程、handle 随 `LiveScheduler` 交回。模型 crate 永远不直接调 `scheduler_pair`（除非像测试那样手工驱动）。

#### 4.1.4 代码实践

**实践目标**：亲手验证两个类型层面的保证——`take_steps` 只能取一次；调度器端消失后 `submit` 依然「永不失败」，且失败路径由信封 drop 炸弹以 `Failed` terminal 作答。本实践**不需要 GPU**。

**操作步骤**：

1. 新建一个草稿 crate（`cargo new wiring-lab`），`Cargo.toml` 加：

   ```toml
   [dependencies]
   pegainfer-frontend = { path = "<仓库路径>/pegainfer-frontend" }
   tokio = { version = "1", features = ["sync"] }
   ```

2. 在 `src/lib.rs` 写如下两个测试（**示例代码**，基于 [wiring.rs:L85-L113](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/wiring.rs#L85-L113) 与 [driver.rs:L164-L179](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/driver.rs#L164-L179) 的 `request()` 帮手改写；`Request` 字段全 `pub`、`RequestId` 经 `raw()` 暴露计数器值）：

   ```rust
   use pegainfer_frontend::engine::{scheduler_pair, Request, Terminal};

   fn request(max_tokens: usize) -> Request {
       Request {
           prompt_tokens: vec![1, 2],
           params: Default::default(),
           history_tokens: None,
           history_marks: Vec::new(),
           stop_policy: Default::default(),
           max_tokens,
           lora_adapter: None,
           kv_transfer_params: None,
           logprobs: None,
           prompt_logprobs: None,
           trace_parent: None,
           client_label: None,
       }
   }

   #[test]
   fn steps_handed_out_exactly_once() {
       let (mut handle, _backend) = scheduler_pair();
       assert!(handle.take_steps().is_some()); // 第一步：取走步流接收端
       assert!(handle.take_steps().is_none()); // 第二步：Option 已空
   }

   #[test]
   fn late_submits_get_failed_terminals_after_backend_dropped() {
       let (mut handle, backend) = scheduler_pair();
       let mut steps = handle.take_steps().expect("step stream");

       // 调度器端整体消失（模拟 driver 线程已死）：提交通道接收端、账本、发布器一同掉落
       drop(backend);

       // submit 仍然正常返回 RequestControl，且每调度器一套的 id 计数器照常自增
       let first = handle.submit(request(4));
       let second = handle.submit(request(4));
       assert_eq!(first.id().raw(), 0);
       assert_eq!(second.id().raw(), 1);

       // 两个信封的 drop 炸弹各发一条 StepOutputs（一条一个 Failed terminal）
       let mut failed = 0;
       while failed < 2 {
           let step = steps.blocking_recv().expect("drop bombs must answer");
           for update in step.updates {
               assert!(matches!(update.terminal, Some(Terminal::Failed { .. })));
               failed += 1;
           }
       }
   }
   ```

3. 运行：`cargo test`。

**需要观察的现象**：第一个测试两条断言全过（`Option::take` 的语义）；第二个测试在 `blocking_recv()` 上收到两条消息、各携带一个 `Failed` terminal，消息文案为 `"request dropped by the engine before it was answered"`（见 [request_lifecycle.rs:L84-L99](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/request_lifecycle.rs#L84-L99) 的信封 Drop 实现）；两次 `submit` 铸出的 id 是 0 与 1。

**预期结果**：两个测试通过。这证明 `handle.submit` 在调度器死后确实「永不失败」——错误作为普通 terminal 流出。本环境未执行该命令，**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：提交通道为什么不用 tokio mpsc（步流的同款）？给出一条基于消费者形态的理由。

**参考答案**：提交通道的消费者是 `drive` 循环——一个跑在裸 `std::thread` 上的无停顿自旋循环（[u3-l5](u3-l5-scheduler-driver.md)），它用 `try_recv()` 非阻塞排空（[driver.rs:L68-L80](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/driver.rs#L68-L80)）。crossbeam 的 `try_recv` 在同步线程上直接可用；tokio mpsc 的阻塞形态是 `blocking_recv`（与「绝不睡眠」的循环纪律冲突）、异步形态需要 runtime（调度器线程没有）。模块文档（[wiring.rs:L7-L11](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/wiring.rs#L7-L11)）给出的正是「sync consumer on the scheduler thread」这一消费者形态理由；「无界 → 发送端永不阻塞」是两侧共同的前提。

**练习 2**：`SchedulerHandle` 里专门留着一份 `step_tx` 的 clone（[wiring.rs:L73-L75](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/wiring.rs#L73-L75)），为什么不省掉它？账本不是也有一份吗？

**参考答案**：账本那份随 `SchedulerBackend` 活在 driver 线程里——线程一死它就掉了。而 4.1.4 实践刚验证过的路径需要**在调度器死后**仍能把 `Failed` terminal 发上步流：`submit` 铸造信封时把 `step_tx.clone()` 塞进信封，信封 drop 炸弹引爆时用它发送。若 handle 不留这份 clone，`submit` 在线程死后将无从应答，客户端挂死——「应答到底」的三层保护（[u3-l4](u3-l4-request-ledger.md)）就缺了一角。

**练习 3**：如果把 metrics 也改成一条 mpsc 通道（每步发一份快照），会破坏哪两件事？

**参考答案**：其一，读者语义被破坏——路由决策与 `/metrics` 抓取要的是**最新快照**，通道里堆的是历史，读者得排空到尾才能拿到最新值，白白做功还可能读到过时值用于路由；其二，唤醒机制失去搭档——现在的设计是「共享单元存最新值 + 步流空消息当闹钟」（[wiring.rs:L10-L12](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/wiring.rs#L10-L12)），`MetricsPublisher::publish` 返回的 `changed` 布尔（[wiring.rs:L59-L64](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/wiring.rs#L59-L64)）直接喂给 `commit_step` 决定是否发空唤醒消息（[u3-l5](u3-l5-scheduler-driver.md) 练习 3）——改成通道后「变化检测」与「最新值保证」都得另造机制。

### 4.2 MetricsPublisher 与 Engine 交接包：launch 交回什么

#### 4.2.1 概念说明

**`MetricsPublisher` 是共享单元的唯一写者**。为什么用 `Mutex<SchedulerMetrics>` 而不是逐字段 `AtomicU64`？注释给的理由是**防撕裂**：读者永远不会看到「一半字段来自上一步、另一半来自这一步」的组合——整份快照在锁内一次换掉。两侧各碰纳秒级、几乎无竞争，锁的代价可以忽略。`publish(&snapshot) -> bool` 顺手回答「变了吗」，返回值直通 `commit_step`。

**`Engine` 是 step 引擎的交接包**：`ModelLine::launch`（[u2-l1](u2-l1-model-line-trait.md)）加载完引擎后，把前端需要的东西打成一个包交回来。文档里有一句设计宣言值得逐字读：

> [wiring.rs:L141-L144](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/wiring.rs#L141-L144) —— 「a handoff package the protocol stack dissolves at wiring time. Required fields are the onboarding checklist: an engine that reports no capacity or no servable length says so explicitly with `None`, it cannot just forget.」（协议栈在接线时溶解的交接包。必填字段就是上手清单：不报容量或不报可服务长度的引擎必须**显式写 `None`**，不能干脆忘了这回事。）

这是「`Option` 即语义」的又一次运用（`Engine.lora: Option<LoraClient>` 的「`Option` 即能力」在 [u3-l1](u3-l1-contract-overview.md) 讲过）：`EngineInfo` 两个字段的 `None` 各有确定含义，不是「随便空着」：

| 字段 | `Some` 的含义 | `None` 的显式语义 |
|---|---|---|
| `kv_capacity: Option<KvCapacity>` | KV 池容量（总块数 + 块大小） | 引擎不报容量 → **前端跳过 batch-fit 检查** |
| `servable_len: Option<u32>` | 最长可服务请求（token 数） | **最大长度校验留在模型上下文窗口**（config.json 的 `max_position_embeddings`） |

**`LiveScheduler { handle, join }`** 是「一个跑着的调度器分区」：handle 是前端的交互面，join 是 driver 线程的 `JoinHandle`——注释规定它的收割时序：**server 在关停时、drop 掉全部 handle（连同提交发送端）之后**才 join（先断粮、等排空、再收尸）。

**`LaunchedEngine`** 是迁移期的二选一包装（[u3-l1](u3-l1-contract-overview.md)）：`Handle(EngineHandle)` 或 `Stepped(Engine)`，配两个 `From` 实现让模型线的 `launch` 直接 `Ok(engine.into())`。

#### 4.2.2 核心流程

一个 step 引擎从 launch 到对外服务的装配链：

```
模型 crate：launch()
 │  spawn_scheduler("qwen3-scheduler", scheduler)   ← 每分区一次
 │      └→ LiveScheduler { handle, join }
 │  Engine { schedulers: Vec<LiveScheduler>, info: EngineInfo, lora }
 │  Ok(LaunchedEngine::Stepped(engine))             ← 经 From<Engine>
 ▼
协议栈（vllm/mod.rs，接线时溶解）：
 │  ① 对账：engine.schedulers.len() 必须等于 serve 声明的 engine_count，否则 bail
 │  ② info.servable_len 钳制 max_model_len（min(模型上下文, 可服务上限)）
 │  ③ 逐调度器：取走 scheduler.handle → 铸一座 SteppedEngineBridge
 │     （携带 info.kv_capacity、钳制后的 max_model_len）；join 收集到 scheduler_joins
 │  ④ 桥全部退出后收割 scheduler_joins
 ▼
每座桥 = take_steps() 的接收端 + submit 通道发送端 + kv_capacity/max_model_len
```

#### 4.2.3 源码精读

> [wiring.rs:L51-L65](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/wiring.rs#L51-L65) —— `MetricsPublisher`：`Arc<Mutex<SchedulerMetrics>>` 的新类型包装。注释解释 Mutex 选型：「A `Mutex` (not per-field atomics) so a reader never sees fields torn across two steps」。`publish` 三行：锁 → 比较 `*current != *snapshot` → 覆盖并返回 `changed`。

> [wiring.rs:L145-L153](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/wiring.rs#L145-L153) —— `Engine` 三字段：`schedulers: Vec<LiveScheduler>`（几个、什么含义是模型线的事）、`info: EngineInfo`、`lora: Option<LoraClient>`（「The `Option` is the capability — no separate flag exists to disagree with it」：没有另一个开关能和这个 `Option` 打架）。

> [wiring.rs:L155-L160](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/wiring.rs#L155-L160) —— `LiveScheduler`：注释规定 join 的时序契约——「Joined by the server at shutdown, after the handles (and with them the submit senders) are dropped」。

> [wiring.rs:L162-L180](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/wiring.rs#L162-L180) —— `LaunchedEngine` 枚举与两个 `From` 实现。枚举文档明确它是迁移期构造：「Deleted (in favor of `Engine` alone) once every model line is migrated」——全部模型线迁到 step 契约后这个枚举将删除，只剩 `Engine`。

> [wiring.rs:L182-L190](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/wiring.rs#L182-L190) —— `EngineInfo` 两字段的 `Option` 语义（4.2.1 表格的原文出处）。

> [vllm/mod.rs:L336-L362](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/mod.rs#L336-L362) —— 协议栈溶解 `Stepped` 引擎的现场：对账 `actual_schedulers != engine_count` 即 `bail`（[u2-l5](u2-l5-serve-plan.md) 讲过的「声明分区数 vs 实际分区数」宁可死不带病服务）；`info.servable_len.map(|cap| max_model_len.min(cap))` 钳制；逐调度器把 `scheduler.handle` 移进 `SteppedEngineBridge` 并携带 `info.kv_capacity`；join 句柄收集到 `scheduler_joins`，注释说明「stepped 引擎拥有自己的调度器线程，桥（连同调度器 handle）消失后再收割」。

> [qwen3-frontend_adapter.rs:L187-L216](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-qwen3/src/frontend_adapter.rs#L187-L216) —— **真值版交接包**：qwen3 的 `Engine` 构造。`servable_len` 由 executor 的上下文/块数/块大小算出；`kv_total` 取 `available_blocks()`（注释解释为何保守一个块：刚建好的 executor 唯一已占用的是泄漏的 CUDA-graph padding 槽）；最终 `EngineInfo { kv_capacity: Some(...), servable_len: Some(...) }`——一个诚实上报两项的引擎长这样。

> [echo-server.rs:L78-L92](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/examples/echo-server.rs#L78-L92) —— **最小版交接包**：echo 线的 `Engine { schedulers: vec![spawn_scheduler("echo", ...)], info: EngineInfo { kv_capacity: None, servable_len: None }, lora: None }`——echo 引擎没有 KV 池也没有长度上限，两个字段显式 `None`、一个能力 `None`，交接包照样完整。

#### 4.2.4 代码实践

**实践目标**：把 `EngineInfo` 两个字段的「显式 None 语义」落到消费点，做一张「字段 → 消费点 → 效果」映射表。源码阅读型实践，无需运行。

**操作步骤**：

1. 从 [wiring.rs:L182-L190](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/wiring.rs#L182-L190) 的两个字段出发，分别追到消费点：
   - `servable_len`：[vllm/mod.rs:L345-L347](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/mod.rs#L345-L347)（Stepped 分支的钳制）；对照 legacy 分支 [vllm/mod.rs:L319-L320](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/mod.rs#L319-L320)（`handle.servable_len()` 做同样的事——两代引擎同一语义）。
   - `kv_capacity`：[vllm/mod.rs:L350-L358](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/mod.rs#L350-L358)（作为 `kv_capacity` 字段进 `SteppedEngineBridge`）。
2. 填表（示例答案，请自行核对行号后补全）：

   | 字段 | 消费点 | `Some` 的效果 | `None` 的效果 |
   |---|---|---|---|
   | `info.servable_len` | serve 装配的 min 钳制 | API 层最大长度 = min(模型上下文, 可服务上限) | 不钳制，停留在模型上下文窗口 |
   | `info.kv_capacity` | SteppedEngineBridge 构造 | 桥可用 `blocks_for()` 做接纳前 batch-fit 检查（[kv.rs:L106-L118](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/kv.rs#L106-L118) 的整块向上取整） | 前端跳过 batch-fit 检查 |

3. 回答思考题：如果一条模型线把 `kv_capacity` **虚报得偏大**，哪一层会先暴露问题？（提示：`KvCapacity` 的文档注释（[kv.rs:L92-L97](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/kv.rs#L92-L97)）说它存在的意义是「让调用者**提前**判断批次放不放得下，免得调度器再推迟」——虚报后前端会放过本应提前拒绝的批次，压力后移到调度器侧的接纳/推迟逻辑。这正是「必填字段即上手清单」要模型线诚实的原因。）

**需要观察的现象**：表格的每一行都能落到一个具体文件的具体行；两代引擎（Handle 分支的 `servable_len()`/`metrics_watch_for` 与 Stepped 分支的 `info`/`scheduler.handle`）在溶解点做的事**逐字段对仗**。

**预期结果**：得到一张三行对照表 + 一句「诚实上报的责任在模型线」的结论。纯阅读，可立即完成；行号引用建议以本地核对为准。

#### 4.2.5 小练习与答案

**练习 1**：`MetricsPublisher` 为什么用 `Mutex` 而不是把 `SchedulerMetrics` 的每个字段都换成原子量？

**参考答案**：撕裂读。`SchedulerMetrics`（[metrics.rs:L20-L33](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/metrics.rs#L20-L33)）有七个字段；逐字段原子意味着一次更新期间读者可能读到「running 还是上一步的、waiting 已是这一步的」这类混合快照——用它做路由打分（4.3 的 running + 4×waiting）就会得到从未存在过的状态。`Mutex` 保证整份快照原子换血；且注释指出双侧各碰纳秒级、无竞争，锁没有实际的争用代价（[wiring.rs:L51-L55](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/wiring.rs#L51-L55)）。

**练习 2**：`LaunchedEngine` 的两个 `From` 实现有什么实际用处？删除枚举时它们陪葬吗？

**参考答案**：用处是让模型线的 `launch` 写 `Ok(engine.into())` 而不必点名变体——迁移期两条线（返回 `EngineHandle` 的 legacy 线与返回 `Engine` 的 step 线）的 `launch` 签名统一为 `Result<LaunchedEngine>`，新迁移的线只改返回值的构造方式。枚举删除时它们当然一并删除，但「qwen3 的 [frontend_adapter.rs:L209-L216](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-qwen3/src/frontend_adapter.rs#L209-L216) 返回 `Engine`、glm52 的 [lib.rs:L1225-L1233](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-glm52/src/lib.rs#L1225-L1233) 返回 `EngineHandle`」这一现状意味着删除的前提是全部 Handle 线迁移完成（[u3-l1](u3-l1-contract-overview.md) 的普查结论）。

**练习 3**：`LiveScheduler.join` 为什么不能在协议栈接桥**之前**就 join？

**参考答案**：join 会**阻塞等待线程结束**，而 driver 线程的退出条件是「提交通道断开（全部 handle drop）**且**账本排空」（[u3-l5](u3-l5-scheduler-driver.md)）——接线时桥还握着 handle、客户端还有在途请求，此刻 join 等于当场死等一个永不满足的条件。[wiring.rs:L157-L159](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/wiring.rs#L157-L159) 的注释与 [vllm/mod.rs:L306-L308](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/mod.rs#L306-L308) 的「reaped below after the bridges (and with them the scheduler handles) are gone」说的是同一条时序：先桥退、handle 掉、driver 排空退出，**然后**才收割 join。

### 4.3 legacy EngineHandle：提交与多分区路由

#### 4.3.1 概念说明

`EngineHandle` 是 legacy 每 token 契约（[u3-l2](u3-l2-legacy-wire.md)）的持有端：它握着每分区一条的提交通道（载荷 `SubmittedRequest = (GenerateRequest, KvPrefix)`），对外提供 `submit` 与一组 builder 方法。deepseek-v2-lite、glm52、kimi-k2 三条未迁移线今天仍返回它（[u3-l1](u3-l1-contract-overview.md) 普查）。

**多分区 = 数据并行**。一个 `EngineHandle` 可以持有 N 条提交通道——每条对应一个逻辑 DP rank（一个自治的引擎线程）。于是 `submit` 必须回答一个 step 契约不存在的问题：**这个请求去哪个分区？**放置策略就长在 handle 里（对照 4.1 的「跨调度器放置是前端策略」——legacy 的前端策略恰好实现在这个类内部）。

**metrics watches 兼任 DP 拓扑声明**。`with_metrics_watches(vec![...])` 挂上每分区一条 watch 负载馈送；文档明确「**这个向量就是引擎对前端可见的 DP 拓扑**，空向量是无效引擎而非『缺 metrics 的单分区』」——`scheduler_partition_count()` 就是 `metrics_watches.len()`，serve 装配层用它对账（[u2-l5](u2-l5-serve-plan.md)）。注意一个精妙的不对称：**路由的范围真相是 `submit_txs.len()`，拓扑声明真相是 `metrics_watches.len()`**——多分区引擎必须挂 watches 才算「申报」了分区数，否则 serve 侧对账会抓到「声明数 ≠ 实际数」。

**`KvPrefix` 的 rank 语义是路由的第三票**。[u3-l2](u3-l2-legacy-wire.md) 讲过 `hit_tokens`；本讲补上另外两个成员：`rank`（解析跑在哪个分区上）与不透明 `hold`（把命中的块钉在那个分区的缓存里）。kv.rs 的字段注释把路由含义说得极直白：

> [kv.rs:L25-L29](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/kv.rs#L25-L29) —— 「The hold pins blocks on THIS rank, so routing anywhere else silently loses the hit and wastes the pin」（hold 把块钉在**这个** rank 上，路由去别处会无声地丢掉命中、白白浪费这根钉子）——所以 `submit_resolved` 按 rank 路由。

铸造方在 crate 外：`pegainfer-kv-store`（[store.rs:L195](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-kv-store/src/store.rs#L195) 调 `KvPrefix::resolved(hit_blocks * block_size, rank, Box::new(probe))`）；而 `KvPrefix::rank()` 是 `pub(crate)`——**rank 的知识不允许出 frontend crate**，路由判断只能由 `submit_resolved` 自己做，外部调用方连「前缀绑在哪个 rank」都读不到。

#### 4.3.2 核心流程

`submit`（公开入口， unresolved）委托给 `submit_resolved`（crate 私有），后者是完整的路由决策树：

```
submit(req)                          ← 唯一公开入口：KvPrefix::none()
 └─ submit_resolved(req, kv_prefix)
     │  路由决策（优先级从上到下）：
     ├─ kv_prefix.rank() == Some(r)？
     │    → 分区 = r   （已解析前缀说了算）
     │      debug_assert!(req.data_parallel_rank.is_none_or(|b| b == r))
     │      ← 调用方绑定与解析 rank 不一致 = 调用方 bug（release 下静默按 r 路由）
     ├─ 否则 req.data_parallel_rank == Some(b)？
     │    → 分区 = b   （调用方绑定说了算）
     ├─ 否则（两者皆无）
     │    → 分区 = argmin_i ( running_i + 4 × waiting_i , i )
     │       每分区分数取自 metrics watch 快照；无 watch 的分区记 0 分；
     │       并列时取下标最小；单分区退化情形恒返回 0
     │
     ├─ 分区 ∈ 0..submit_txs.len()？
     │    是 → submit_txs[分区].send((req, kv_prefix))
     │         失败（分区线程没了）→ Err(SendError(req)) 把请求还给调用方
     └─ 否则（越界 rank）
          → reject_unroutable：在 req.token_tx 上连发
             TokenEvent::Scheduled { cached_tokens: 0, ... } →
             TokenEvent::Rejected { message: "data_parallel_rank {p} is outside 0..{N}" }
          → 返回 Ok(())   ← 调用方错误 ≠ 引擎故障，不报 submit 错误
```

关停路径（最后一个 handle clone 被 drop 时）：

```
Drop for EngineInner
 ├─ submit_txs.clear()        ← 关闭全部分区的提交通道（driver/引擎线程感知断连）
 └─ 逐个 join 线程（分区顺序），跳过「当前线程就是它自己」的句柄；
     panic 的线程只 log::warn（用 panic_message 提取载荷），不让关停路径二次爆炸
```

#### 4.3.3 源码精读

> [handle.rs:L51-L69](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/handle.rs#L51-L69) —— `EngineHandle` 的四个字段与 `EngineInner`：`Arc<EngineInner>` 里是 `submit_txs: Vec<...>`（每分区一条发送端）与 `join_handles`；外层是 `servable_len`、`kv_capacity` 与 `metrics_watches: Vec<Option<watch::Receiver<SchedulerMetrics>>>`（每分区一条、`Option` 容忍「某分区没接馈送」）。

> [handle.rs:L88-L105](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/handle.rs#L88-L105) —— `new_with_join_handles`：多分区构造器，文档先给出路由规则全文（「routes by the request's `data_parallel_rank`; unbound requests go to the least-loaded partition (waiting requests weigh 4x, the vLLM DP policy)」），再 `assert!(!submit_txs.is_empty())`——「引擎必须至少暴露一个调度器分区」。

> [handle.rs:L151-L172](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/handle.rs#L151-L172) —— `with_metrics_watches`（「The vector is also the engine's frontend-visible DP topology」+ 非空断言）与 `scheduler_partition_count`（`metrics_watches.len()`——**拓扑声明真相**）。

> [handle.rs:L191-L200](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/handle.rs#L191-L200) —— `submit`：unresolved 入口的全部三行——「no KV-prefix resolution ran for it, so the scheduler receives `KvPrefix::none` and prefills from its own GPU prefix match alone」，委托 `submit_resolved(req, KvPrefix::none())`。这是 crate 外唯一能走的提交入口（`submit_resolved` 是私有的）。

> [handle.rs:L202-L236](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/handle.rs#L202-L236) —— `submit_resolved`，本讲的密度最高 30 行。L212-L214 的注释先立规则：「A resolved prefix binds the request: its hold pins blocks on the rank it resolved against, so that rank wins the routing. **A caller-bound rank that disagrees is a caller bug.**」；L215-L225 的 `match (kv_prefix.rank(), req.data_parallel_rank)` 就是 4.3.2 决策树的直译——`debug_assert!` 用 `bound.is_none_or(|b| b == resolved)` 允许「未绑定」与「绑定一致」两种合法态；L226-L230 命中分区则发送（失败把请求本体还给调用方）；L231-L235 越界则 `reject_unroutable` 后返回 `Ok(())`。

> [handle.rs:L238-L260](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/handle.rs#L238-L260) —— `least_loaded_partition`：`min_by_key` 的键是二元组 `(score, *partition)`——**分数并列时下标小者胜**；分数计算 `snapshot.num_running_reqs + 4 * snapshot.num_waiting_reqs`，`watch.borrow()` 取最新快照；没有接 watch 的分区 `map_or(0, ...)` 记零分，所以「单分区且没接馈送」的退化情形恒返回 0。

> [handle.rs:L271-L287](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/handle.rs#L271-L287) —— `reject_unroutable`：文档点名设计意图——「answer the request ... with the standard Scheduled → Rejected pair — the same surface a model scheduler's admission rejection produces」。也就是说，越界 rank 的客户端看到的事件序列与「模型调度器接纳拒绝」**完全同形**：先 `Scheduled`（`cached_tokens: 0`）再 `Rejected`（消息 `"data_parallel_rank {partition} is outside 0..{partitions}"`）。

> [handle.rs:L289-L303](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/handle.rs#L289-L303) —— `Drop for EngineInner`：先 `submit_txs.clear()`（逐个 drop 发送端 = 关通道），再按分区顺序 join；`join_handle.thread().id() != thread::current().id()` 防止 join 自己（死锁）；panic 的线程用 `panic_message`（[handle.rs:L263-L269](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/handle.rs#L263-L269)）提取载荷后仅 `log::warn!`。

> [kv.rs:L6-L30](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/kv.rs#L6-L30) —— `KvPrefix` 的结构文档：hit/hold/rank 三成员；「Degraded resolutions (timeout, pool pressure) are not a distinct state: they surface as a smaller `hit_tokens`」——退化解析不是独立状态，只是一个更小的数（[u3-l2](u3-l2-legacy-wire.md) 讲过，这里再看一遍 rank 部分）。

> [kv.rs:L44-L62](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/kv.rs#L44-L62) —— `KvPrefix::resolved(hit_tokens, rank, hold)` 构造器与 `rank()` 访问器——注意 `rank()` 标着 `pub(crate)`：**frontend 之外无人能读 rank**，路由知识被封在 crate 内。

> [glm52-lib.rs:L1225-L1233](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-glm52/src/lib.rs#L1225-L1233) —— legacy 多分区引擎的真实构造现场：`EngineHandle::new_with_join_handles(submit_txs, join_handles).with_servable_len(...).with_kv_capacity(KvCapacity { total_blocks, block_size }).with_metrics_watches(load_rxs)`——四个 builder 一口气申报「分区拓扑 + 可服务长度 + KV 容量 + 每分区负载馈送」，与 4.2 的 `EngineInfo` 逐字段对仗。

> 既有测试三连（都在 [handle.rs:L357-L420](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/handle.rs#L357-L420)）：`multi_partition_submit_routes_by_bound_rank`（L357-L369，绑定 rank=1 的请求只出现在 rx1，且携带 `KvPrefix::none()`、`hit_tokens()==0`）；`multi_partition_submit_places_unbound_on_the_least_loaded`（L371-L401，两分区分数 2 vs 4 → 落 0；用 `load_tx0.send_replace` 把 0 号抬到 6 分后下一请求翻到 1 号——**动态再平衡**就靠 watch 的最新值）；`multi_partition_out_of_range_rank_is_rejected_not_dropped`（L403-L420，rank=7 的请求得到 `Scheduled` → `Rejected`，消息含 `"outside 0..1"`，且 `submit` 返回 `Ok`）。

#### 4.3.4 代码实践（本讲指定实践）

**实践目标**：复现 handle.rs 的 least-loaded 测试思路，但扩到**三个分区**，验证打分公式与动态迁移；随后用 `KvPrefix` 的 rank 语义解释 `submit_resolved` 的 `debug_assert`。不需要 GPU。

**操作步骤**：

1. 草稿 crate（可复用 4.1.4 的那个），`Cargo.toml` 追加 `pegainfer-frontend` 已有；测试写在 `src/lib.rs`。以下为**示例代码**，仿照 [handle.rs:L336-L355](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/handle.rs#L336-L355) 的 `routed_request` 帮手与 [handle.rs:L371-L401](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/handle.rs#L371-L401) 的既有测试：

   ```rust
   use pegainfer_frontend::engine::{
       EngineHandle, GenerateRequest, SchedulerMetrics, TokenEvent, TokenSink,
       TokenStreamReceiver,
   };
   use pegainfer_frontend::sampler::SamplingParams;
   use tokio::sync::{mpsc, watch};

   /// 与 handle.rs 测试同形的帮手：返回请求与其事件接收端
   fn routed_request(rank: Option<usize>) -> (GenerateRequest, TokenStreamReceiver) {
       let (token_tx, token_rx) = TokenSink::standalone();
       (
           GenerateRequest {
               request_id: None,
               queued_at_unix_s: None,
               trace_parent: None,
               data_parallel_rank: rank,
               prompt_tokens: vec![1],
               params: SamplingParams::default(),
               max_tokens: 1,
               lora_adapter: None,
               kv_transfer_params: None,
               token_tx,
               logprobs: None,
               prompt_logprobs: None,
           },
           token_rx,
       )
   }

   #[test]
   fn three_partitions_unbound_requests_follow_the_score() {
       // 三分区，负载故意排成：0 号 3 running（3 分）、1 号 4 waiting（16 分）、
       // 2 号 1 running（1 分）→ 最低分是 2 号。
       let loads = [(3u64, 0u64), (0u64, 4u64), (1u64, 0u64)];
       let mut txs = Vec::new();
       let mut rxs = Vec::new();
       let mut watch_txs = Vec::new();
       let mut watches = Vec::new();
       for (running, waiting) in loads {
           let (tx, rx) = mpsc::unbounded_channel();
           txs.push(tx);
           rxs.push(rx);
           let (wtx, wrx) = watch::channel(SchedulerMetrics {
               num_running_reqs: running,
               num_waiting_reqs: waiting,
               ..SchedulerMetrics::default()
           });
           watch_txs.push(wtx);
           watches.push(wrx);
       }
       let handle = EngineHandle::new_with_join_handles(txs, Vec::new())
           .with_metrics_watches(watches);

       // ① 无绑定请求 → 分数最低的 2 号分区
       let (req, _events) = routed_request(None);
       handle.submit(req).expect("submit");
       assert!(rxs[0].try_recv().is_err());
       assert!(rxs[1].try_recv().is_err());
       let (routed, kv_prefix) = rxs[2].try_recv().expect("routed to partition 2");
       assert_eq!(routed.prompt_tokens, [1]);
       assert_eq!(kv_prefix.hit_tokens(), 0);   // unresolved → KvPrefix::none()

       // ② 改写负载：0 号抬到 6 分（6 running）、2 号抬到 10 分（10 running）、
       //    1 号降到 4 分（1 waiting）→ 新的最低分是 1 号（4 < 6 < 10）。
       watch_txs[0].send_replace(SchedulerMetrics {
           num_running_reqs: 6,
           ..SchedulerMetrics::default()
       });
       watch_txs[1].send_replace(SchedulerMetrics {
           num_waiting_reqs: 1,
           ..SchedulerMetrics::default()
       });
       watch_txs[2].send_replace(SchedulerMetrics {
           num_running_reqs: 10,
           ..SchedulerMetrics::default()
       });
       let (req, _events) = routed_request(None);
       handle.submit(req).expect("submit");
       assert!(rxs[2].try_recv().is_err());
       assert!(rxs[1].try_recv().is_ok());
   }

   #[test]
   fn out_of_range_rank_is_answered_not_errored() {
       let (tx, _rx) = mpsc::unbounded_channel();
       let (_wtx, wrx) = watch::channel(SchedulerMetrics::default());
       let handle = EngineHandle::new_with_join_handles(vec![tx], Vec::new())
           .with_metrics_watches(vec![wrx]);

       let (req, mut events) = routed_request(Some(9));
       handle
           .submit(req)
           .expect("out-of-range is answered, not an error");
       // 事件面：Scheduled → Rejected，消息点名合法范围
       assert!(matches!(
           events.try_recv().map(|(_, event)| event),
           Ok(TokenEvent::Scheduled { .. })
       ));
       assert!(matches!(
           events.try_recv().map(|(_, event)| event),
           Ok(TokenEvent::Rejected { ref message, .. })
           if message.contains("outside 0..1")
       ));
   }
   ```

   （`TokenStreamReceiver` 的 `try_recv` 拿到的是 `(RequestTag, TokenEvent)` 二元组——共享打标通道的设计，[u3-l2](u3-l2-legacy-wire.md)；帮手返回元组与 [handle.rs:L336-L355](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/handle.rs#L336-L355) 同形。）

2. 运行：`cargo test`。

3. **书面作业**：解释 [handle.rs:L215-L225](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/handle.rs#L215-L225) 里这条 `debug_assert!` 为什么把「前缀 rank 与调用方绑定不一致」定性为**调用方 bug** 而非引擎故障。组织答案时引用三处源码：(a) [kv.rs:L25-L29](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/kv.rs#L25-L29)（hold 钉块在解析时的 rank 上）；(b) [handle.rs:L202-L206](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/handle.rs#L202-L206)（`submit_resolved` 文档：「the caller that resolved the prefix must have bound the request to the rank it resolved against」）；(c) [handle.rs:L212-L214](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/handle.rs#L212-L214)（注释的两句话规则）。

**需要观察的现象**：测试 ① 第一轮只有 `rxs[2]` 收到请求、且携带的 `KvPrefix` 是 `hit_tokens()==0` 的 none 版本；`send_replace` 改写 watch 后第二轮请求翻转到 `rxs[1]`——**路由读的是 watch 里的最新快照，不是构造时的值**。测试 ② 的 `submit` 返回 `Ok`，接收端依次收到 `Scheduled` 与 `Rejected`（消息含 `outside 0..1`）。

**预期结果**：两个测试通过。对照既有测试 [handle.rs:L371-L401](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/handle.rs#L371-L401)（两分区版），行为应完全同构。书面作业的参考答案要点见 4.3.5 练习 3。本环境未执行该命令，**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：越界 rank 为什么用 `Scheduled → Rejected` 事件对应答，而不是让 `submit` 返回 `Err`？对比同函数里「分区通道发送失败返回 `Err(SendError)`」的分支，两种失败的分界线在哪？

**参考答案**：分界线是**调用方错误 vs 引擎故障**。越界 rank 是调用方把请求绑到了拓扑之外的分区——请求本身合法、引擎也活着，客户端应当拿到一个**确定的终态**（`Rejected`，且消息点名合法范围），这与模型调度器拒绝接纳的客户端可见面完全一致（[handle.rs:L271-L274](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/handle.rs#L271-L274) 注释原话「the same surface a model scheduler's admission rejection produces」）。而发送失败（`SendError`）意味着那个分区的引擎线程已经没了——这是引擎侧的异常状态，把请求本体还给调用方（`Err(SendError(req))`）让它决定重试或上报，合理得多。一个是「你的请求我不接，理由如下」，一个是「我这台机器坏了」。

**练习 2**：`KvPrefix::rank()` 为什么是 `pub(crate)` 而不是 `pub`？

**参考答案**：因为 rank 的**用途只有一个**——`submit_resolved` 的路由决策；而「已解析前缀必须路由到解析时的 rank」这条规则的正确性依赖 hold 的物理约束（块钉在那个 rank 上，[kv.rs:L25-L29](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/kv.rs#L25-L29)）。若 rank 公开，crate 外的调用方就能读到它、然后自己挑分区提交——绕过 `submit_resolved` 的仲裁，把「路由到别处 → 无声丢命中、浪费钉子」的错误路径重新打开。把它封在 crate 内，等于把「前缀路由权」收归唯一的决策点；外部调用方只能通过 `data_parallel_rank` 表达**意愿**（resolved 时仅作一致性校验），不能与解析结果对抗。

**练习 3**：把 4.3.4 的书面作业答案写成三句话。

**参考答案**：① `KvPrefix` 的 hold 把命中的 KV 块**物理地钉在**解析发生的那个 rank 上，缓存命中的全部价值都绑定在那个位置。② 因此「已解析前缀」自带路由指令——去别的 rank 会无声丢掉命中、白费钉子，`submit_resolved` 让解析 rank 压倒调用方绑定，正是把物理约束翻译成路由规则。③ 调用方绑定与解析 rank 不一致，只可能出自「同一请求既被解析又被另行绑定」的调用流程错误（正常流程是解析者按解析结果绑定，[handle.rs:L202-L206](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/handle.rs#L202-L206) 文档原话），不可能是引擎运行时状态——所以它是 bug（`debug_assert` 只在 debug 构建抓它，release 下静默按解析 rank 路由，宁取正确路由也不中断服务）。

**练习 4**（附加）：`least_loaded_partition` 里 `min_by_key` 的键为什么是 `(score, *partition)` 二元组而不是只用 `score`？

**参考答案**：为了确定性的并列决胜。`running + 4×waiting` 完全可能并列（比如两个分区都空转 0 分）；只用 `score` 做 `min_by_key` 时返回哪个并列者是未指定行为，路由会不可复现。键里追加分区下标后，**分数并列时下标小者胜**——空载的 N 分区引擎总是把请求灌进 0 号，行为可预测、可测试（[handle.rs:L238-L243](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/handle.rs#L238-L243) 文档：「ties to the lowest index」）。

## 5. 综合实践

**任务**：把本讲三块内容（接线铸造、交接包、legacy 路由）拧成一个「**两代接线对照实验**」，产出一五维对照表并附行号证据。所有代码均为**示例代码**，全部可在无 GPU 的草稿 crate 里完成。

**第一部分：代码实验**（复用 4.1.4 / 4.3.4 的草稿 crate，把三个实验并排跑通）：

1. **step 侧的「应答到底」**：4.1.4 的 `late_submits_get_failed_terminals_after_backend_dropped`——drop `SchedulerBackend` 后 `handle.submit` 仍返回 `RequestControl`，且步流上收到携带 `Terminal::Failed` 的 `StepOutputs` 消息。
2. **legacy 侧的「应答到底」**：4.3.4 的 `out_of_range_rank_is_answered_not_errored`——越界 rank 的 `submit` 返回 `Ok`，`TokenSink` 接收端依次收到 `Scheduled` 与 `Rejected`。
3. **legacy 侧的放置策略**：4.3.4 的三分区 least-loaded 测试，包括 `send_replace` 改写负载后的路由翻转。

**第二部分：阅读实验**：以 [wiring.rs](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/wiring.rs) 与 [handle.rs](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/handle.rs) 为依据，填写下表（参考答案已给出大部分，请核对行号补全空格）：

| 维度 | step 契约（wiring.rs） | legacy 契约（handle.rs） |
|---|---|---|
| 提交通道 | crossbeam 无界（L119） | tokio mpsc 无界，每分区一条（L67、L96-L105） |
| 提交入口签名 | `submit(&self, Request) -> RequestControl`，**永不失败**（L85-L95） | `submit(&self, GenerateRequest) -> Result<(), SendError<...>>`（L195-L200） |
| 应答通路 | 步流：扁平 `RequestUpdate`，终态恰一次由账本强制（[u3-l4](u3-l4-request-ledger.md)） | `token_tx`：`TokenEvent` 事件流，「Scheduled 先行、terminal 恰一次」靠约定（[u3-l2](u3-l2-legacy-wire.md)） |
| metrics 载荷 | `Arc<Mutex>` 共享单元 + 步流空消息唤醒（L51-L65） | 每分区一条 `watch::Receiver`，`changed()` 变化才唤醒（L146-L189） |
| 分区拓扑真相 | `engine.schedulers.len()`，serve 侧对账（[vllm/mod.rs:L337-L344](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/mod.rs#L337-L344)） | `metrics_watches.len()`（`scheduler_partition_count`，L169-L172），路由范围另看 `submit_txs.len()` |
| 放置策略 | 前端策略（模块文档 L14-L17；由协议栈/调用方决定） | handle 内建：绑定 rank > 解析 rank > least-loaded（L215-L225、L243-L260） |
| 线程回收 | `LiveScheduler.join`，桥退后收割（L155-L160） | `EngineInner::drop` 关通道后逐个 join（L289-L303） |

**产出与自检**：

1. 三个实验全绿（`cargo test`）；
2. 对照表每格都有可点击的行号依据，其中至少一格是你自己新核对的（而非抄本讲）；
3. 用一句话回答：两代契约在「提交永不失败/可能失败」上的差异，为什么恰好与它们「应答机制的类型化程度」反向相关？（提示：step 契约把终态保证编码进类型（信封炸弹 + 账本恰一次），所以入口敢承诺永不失败；legacy 的终态只是事件约定，入口只能把「引擎没了」这种它无法用事件回答的情形交还调用方。）

**预期结果**：以上结论基于源码推演，运行结果**待本地验证**；若某实验失败，优先检查草稿 crate 的 tokio features（只需 `sync`）与 `pegainfer-frontend` 的 path 依赖。

## 6. 本讲小结

- **`scheduler_pair` 铸造而非组装**：提交（crossbeam，同步消费者在调度器线程）、步流（tokio mpsc，异步消费者在协议栈）、metrics（`Arc<Mutex>` 共享单元，只要最新值）与 exited（`DropGuard`）一次铸出双端，模型 crate 无法接错线或漏线；账本出生即握步流发送端，handle 另留一份 `step_tx` 供调度器死后的信封 drop 炸弹使用。
- **`submit` 永不失败是类型化的承诺**：发送失败时代码主动引爆信封 drop 炸弹，以 `Failed` terminal 在步流上作答——错误走与正常终态相同的通路，客户端永不挂死。
- **`Engine` 交接包的「必填字段即上手清单」**：`EngineInfo` 的 `kv_capacity` 与 `servable_len` 两个 `Option` 各有显式 `None` 语义（跳过 batch-fit 检查 / 最大长度停在模型上下文），引擎不能「忘了报」；qwen3 报 `Some/Some`（真值），echo 报 `None/None`（无池无上限），都合法——诚实是模型线的责任。
- **legacy `EngineHandle` 的路由决策树**：已解析前缀的 rank 压倒一切（hold 把块钉在那个 rank 上，路由别处即丢命中）；否则用调用方绑定 rank；否则 \( \text{running} + 4 \times \text{waiting} \) 最低分（vLLM DP 权重，并列取下标小，缺 watch 记 0 分）；越界 rank 以 `Scheduled → Rejected` 事件对应答且 `submit` 返回 `Ok`——调用方错误与引擎故障在返回类型上分家。
- **拓扑的真相有两个**：legacy 的路由范围看 `submit_txs.len()`，对 serve 声明的分区数看 `metrics_watches.len()`（`scheduler_partition_count`）——多分区引擎必须挂 watches 才算申报了拓扑；step 侧则统一为 `schedulers.len()`，由 serve 装配层对账。
- **两代接线在「谁保证应答」上反向相关**：step 契约把终态保证编码进类型（信封炸弹 + 账本 terminal 恰一次），入口敢承诺永不失败；legacy 的终态只是事件流约定，入口只能把无法用事件回答的「引擎没了」以 `SendError` 还给调用方。

## 7. 下一步学习建议

本讲把引擎契约的**内部接线**讲完了，下一讲进入单元四。[u4-l1](u4-l1-serve-paths.md)（serve 函数族与装配骨架）会看到本讲的 `LaunchedEngine` 在协议栈被溶解的完整上下文——预绑定 unix socket、引擎加载与 HTTP 前端并发、分区数对账与 shutdown 级联；[u4-l3](u4-l3-engine-bridges.md) 会把本讲的 `take_steps()` 接收端与 legacy `metrics_watch` 接进 zmq 桥，对比两代桥在「每 token 唤醒次数」上的差异。若想继续深挖契约边缘的硬骨头（`KvPrefix` 的 hold 生命周期、`RequestEnvelope` drop 炸弹与 abort 的完整语义），直接预习 [u5-l1](u5-l1-kv-and-lifecycle.md)，并重读 [request_lifecycle.rs](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/request_lifecycle.rs) 的模块文档——它是那篇讲义的地图。
