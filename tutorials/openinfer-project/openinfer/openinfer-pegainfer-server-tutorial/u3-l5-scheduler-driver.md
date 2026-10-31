# Scheduler trait 与 drive 轮询循环

## 1. 本讲目标

[u3-l4](u3-l4-request-ledger.md) 结尾留下了一个关键结论：`register`、`commit_step`、`fail_all` 是 `pub(crate)`，**节奏（cadence）由 driver 拥有**。本讲就把这个「driver」完整拆开——它是 [driver.rs](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/driver.rs) 里那个不到 50 行的轮询循环 `drive`，加上铸造接线的 `spawn_scheduler`。读完本讲你应当能够：

1. **说出 `Scheduler` 三个方法的分工**：`submit` 只做所有权转移、`step` 是唯一裁决点（也是唯一写账本的地方）、`metrics` 供驱动循环每迭代发布一次快照——模型线的全部运行时义务就这三个方法，多一点也不给。
2. **走查 `drive` 循环的顺序保证**：先排空提交通道（每条先 `register` 再 `submit`）、再 `step`、再快照并发布 metrics、再 `commit_step` 恰好一次；然后是致命错误路径与空闲自旋。
3. **解释致命错误路径**：`step` 返回 `Err` → 账本 `fail_all` 把错误原文写销**每一个**未答复请求 → 再 commit 一次让写销落地 → 线程退出。
4. **用 `spawn_scheduler` + `SchedulerHandle` 跑通一次完整回路**：提交一个请求，从 `take_steps()` 取出的接收端读到 `Scheduled` 与 terminal。

本讲承接 [u3-l4](u3-l4-request-ledger.md)（账本）与 [u3-l3](u3-l3-step-wire.md)（线类型），是 [u3-l6](u3-l6-wiring-and-handle.md)（scheduler_pair 与多分区路由）的直接前置。

## 2. 前置知识

### 2.1 轮询循环：一份代码代替八份纪律

PegaInfer 有八条模型线（qwen3、qwen35、deepseek-v2-lite、gemma4、kimi-k2、glm52、k3、qwen4），每条线都有自己的调度器。如果「每步先排空提交、再执行、再发 metrics、再统一提交、出错时写销全部请求、空闲时怎么自旋」这些**循环骨架**的约定散落在各模型 crate 里，那就是八份需要人肉保持一致的纪律——迟早漂移。driver.rs 的模块文档把这条设计决策说得很直白：

> The loop lives here — once, for every model line — so its conventions (drain order, one metrics publish and one commit per iteration, shutdown and fatal handling) are code, not per-crate discipline.
> （循环活在这里——每条模型线只此一份——于是它的约定（排空顺序、每迭代一次 metrics 发布与一次 commit、关停与致命处理）是代码，而不是各 crate 的纪律。）

见 [driver.rs:L1-L9](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/driver.rs#L1-L9)。

这也是整个项目「EP free-running 纪律」（见仓库 CLAUDE.md：任何引擎循环都不停不等）在单个调度器上的本地版本：`drive` **从不睡眠、从不阻塞等待事件**，它全速转圈，靠 `try_recv` 非阻塞地试探提交通道。为什么敢这样烧 CPU？因为 `step` 的契约要求「空闲的调度器快速返回」——`step` 里没有 GPU 工作可做时立刻 `Ok(())`，循环马上进入下一轮探测。调度器自己 spawn 的 worker 线程（比如 CUDA 流上的完成）会在**一个 step 的粒度内**被拾起。

### 2.2 通道三件套：回顾与预告

[u3-l1](u3-l1-contract-overview.md) 已经画过 step 契约的接线图，这里只复习 `drive` 会碰到的三件套（全部由 `scheduler_pair` 一次铸出，详见 [u3-l6](u3-l6-wiring-and-handle.md)）：

| 方向 | 通道 | 生产者 | 消费者 | `drive` 侧的操作 |
|---|---|---|---|---|
| 提交 | crossbeam 无界 channel | 前端 `SchedulerHandle::submit` | 调度器线程 | `try_recv` 排空（不阻塞） |
| 步流 | tokio 无界 mpsc | 账本（经 `commit_step`） | 协议栈翻译循环 | 间接：`commit_step` 内 `send` |
| metrics | `Arc<Mutex<SchedulerMetrics>>` 共享单元 | 驱动循环每迭代写入 | 前端按需读 | `metrics.publish(&snapshot)` |

注意一个精妙的分工：**步流上「metrics 变了」这件事本身也用一条步流消息来信号**——发布前后快照不同时，`commit_step` 会发出一条 `updates` 为空的 `StepOutputs`，专门用来唤醒读者去重读共享单元（4.2.3 详解）。

### 2.3 两档错误：panic 与 `Err` 的边界

[u3-l4](u3-l4-request-ledger.md) 讲过账本对**记账违规**（对已关账户 `finish` 等）一律 panic——那是「生产者写错了代码」的档位。本讲处理另一档：`Scheduler::step` 返回的 `anyhow::Result<()>` 里的 `Err`，语义是**引擎本身不可再用**（GPU 故障、CUDA 上下文损坏这类）。两档错误在源码里泾渭分明：

- 记账违规 → panic 在调度器调用点（bug，修代码）；
- **可恢复的执行失败**（单个请求算错了）→ 调度器自己吸收：`fail` 掉被碰到的请求，继续服务（trait 文档原话）；
- `Err` → 引擎报废 → driver 写销全部在途请求然后收摊。

### 2.4 需要的 Rust 背景

- **`try_recv` 与 `TryRecvError`**：crossbeam 通道的非阻塞接收。`Empty` = 现在没消息（不代表永远没有）；`Disconnected` = 所有发送端都掉了（**永远**不会有消息了）。`drive` 用这两者的区别实现优雅关停判定。
- **`std::hint::spin_loop`**：一条 CPU 提示指令（x86 上是 `PAUSE`），告诉核心「我在短等待，让出发射端口」，但不放弃线程、不进内核。延迟近乎为零，代价是这一核持续轻负载空转——对「随时可能有请求进来」的引擎主循环，这正是想要的属性。
- **`std::thread::Builder::name(...).spawn(...)`**：起命名 OS 线程；线程名会出现在 `top -H`、gdb、panic 回溯里，便于运维定位「哪个调度器的线程在烧 CPU」。
- **`DropGuard`（tokio-util）**：持有时持有令牌，drop 时取消 `CancellationToken`。`SchedulerBackend.exit_guard` 就是它——driver 线程一旦返回（无论正常排空还是致命退出），前端的 `handle.exited()` 立刻可等待。
- **`anyhow::Result` 与 `{error:#}`**：anyhow 的错误链交替打印格式，`fail_all` 的写销消息里用它把深层上下文链完整带给客户端。

## 3. 本讲源码地图

| 文件 | 角色 | 本讲用途 |
|---|---|---|
| `pegainfer-frontend/src/engine/driver.rs` | **主角** | `Scheduler` trait、`spawn_scheduler`、`drive` 循环，以及三个可直接运行的测试（EchoScheduler、暂停调度器、致命注入） |
| `pegainfer-frontend/src/engine/metrics.rs` | **主角** | `SchedulerMetrics` 快照的字段语义、「计数器必须是累计值」的设计论证 |
| `pegainfer-frontend/src/engine/wiring.rs` | 辅：接线 | `SchedulerBackend`/`MetricsPublisher`/`SchedulerHandle`/`scheduler_pair`/`LiveScheduler`（u3-l6 主讲，本讲只取 `drive` 触到的面） |
| `pegainfer-frontend/src/engine/ledger.rs` | 辅：账本 | 只看「Driver face」三方法：`register`、`commit_step`、`fail_all`（u3-l4 已精读其余） |
| `pegainfer-frontend/examples/echo-server.rs` | 辅：参考实现 | 生产语境里的 `EchoScheduler`：`launch` 里 `spawn_scheduler`、三方法的完整写法 |
| `pegainfer-frontend/src/engine/step.rs` | 辅：线类型 | `StepOutputs { updates }`——步流上每迭代至多一条消息（u3-l3 已精读） |

## 4. 核心概念与源码讲解

### 4.1 Scheduler trait：模型线的运行时义务被压缩成三个方法

#### 4.1.1 概念说明

[u2-l1](u2-l1-model-line-trait.md) 讲过 `ModelLine` 是 server 与模型 crate 之间的接缝；本讲的 `Scheduler` 是**接缝的运行时半边**：模型线的引擎启动之后，留给前端的全部交互面就是这三个方法。三个方法各自被压缩到只剩一个职责：

- **`submit`：只做所有权转移**。请求负载（`QueuedRequest`）从驱动循环移进调度器自己的队列，**没有任何裁决**——接纳与否、拒绝与否，一概不在这里决定。为什么可以推迟？因为 driver 每迭代 commit 一次：`step` 里补记的裁决，与「submit 时立刻裁决」落在**同一条** `StepOutputs` 里，对外不可区分。晚裁决零代价，却让 `submit` 的签名干净到不能更干净：吃一个值，返回 `()`，不可能失败。
- **`step`：唯一裁决点**。接纳、GPU 工作、逐请求记账，全部发生在这一步里；账本上的每一笔业务写入（`admit`/`push_tokens`/`finish`/`retire`/…）都出自某个 `step` 的调用栈。trait 文档称之为「the single emission site」。
- **`metrics`：自画像快照**。返回一份 `SchedulerMetrics`，driver 每迭代取一次发布。注意它拿的是 `&self`——只读，不产生任何副作用。

一个容易忽略的边界声明也写在模块文档里：**超出三方法的能力（比如 LoRA 控制通道）不关契约的事**——需要额外通道的调度器在 spawn 之前自己把通道句柄捕获进结构体，在自己的 `step` 里排空它。契约拒绝为每种能力开一个方法。

#### 4.1.2 核心流程

```
驱动循环的一次迭代（概念版，4.2 精读真代码）

    ① 排空提交通道：
         for envelope in submissions.drain():
             queued = ledger.register(envelope)   ← 开户（driver 专属）
             scheduler.submit(queued)             ← 纯所有权转移，无裁决
    ② step = scheduler.step(&mut ledger)          ← 唯一裁决点 + GPU 工作 + 记账
    ③ snapshot = scheduler.metrics()              ← 自画像
       metrics_changed = metrics.publish(snapshot) ← 写共享单元，返回是否变化
    ④ ledger.commit_step(metrics_changed)         ← 本迭代全部更新 → 至多一条步流消息
    ⑤ step 为 Err？→ fail_all(错误原文) → commit_step(false) → 线程退出
    ⑥ 快照显示空转？→ 通道也断了就退出，否则 spin_loop 后回到 ①
```

trait 的三方法与这条循环的对应关系：

| 方法 | 在循环里的位置 | 契约限制 |
|---|---|---|
| `submit` | ① 内，每条提交一次 | 只转移；裁决延到 `step` 不丢失任何信息 |
| `step` | ②，每迭代恰好一次 | 可恢复失败自己吸收；`Err` = 引擎报废 |
| `metrics` | ③，每迭代恰好一次 | `&self` 只读快照 |

#### 4.1.3 源码精读

trait 定义本体（含全部三方法的契约注释）：

> [driver.rs:L18-L41](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/driver.rs#L18-L41) —— `Scheduler` trait：`Send` 即可（跑在专属 OS 线程上，单所有权，不需要 `Sync`）。三个方法的文档注释就是契约正文。

拆开看三个签名：

```rust
fn submit(&mut self, request: QueuedRequest);                    // L27
fn step(&mut self, ledger: &mut RequestLedger) -> anyhow::Result<()>;  // L37
fn metrics(&self) -> SchedulerMetrics;                           // L40
```

三条值得逐字读的注释：

- [driver.rs:L21-L26](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/driver.rs#L21-L26)：`submit` 的「Ownership transfer only — every verdict (admit/reject/retire) is written to the ledger from `step`, the single emission site. Nothing is lost by deferring」——晚裁决零代价的完整论证。
- [driver.rs:L29-L36](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/driver.rs#L29-L36)：`step` 的两档失败语义——「Recoverable execution failures are the scheduler's to absorb (fail the touched requests, keep serving); `Err` means the engine is beyond use」，以及「driver 无停顿循环、空闲 step 快速返回、worker 线程的完成在一个 step 内被拾起」的轮询依据。
- [driver.rs:L39](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/driver.rs#L39)：`metrics` 的「the driver publishes it once per iteration」。

再看两份参照实现（都是真实代码，读者可对照体会三方法的最小实现）：

> [driver.rs:L116-L162](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/driver.rs#L116-L162) —— 测试用 `EchoScheduler`：每步每请求发一个 token，`max_tokens` 到量即 `finish(Length)`。`submit` 把 `(id, max_tokens)` 压进 `queued`；`step` 先把 `queued` 全部 `admit` 转 `running`，再对每个 running 请求查 abort → 读账本流水 `completion_tokens` → `push_tokens` → 到量 `finish`；`metrics` 现算 running/waiting 两个 gauge。

> [echo-server.rs:L116-L167](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/examples/echo-server.rs#L116-L167) —— 生产语境的 `EchoScheduler`（无 GPU 的完整模型线）：同样的三方法骨架，但 `step` 里多了真实的接纳决策——排队请求若已 abort 直接 `retire`，否则按 `prompt_tokens` 与 `max_tokens` 的较小值建逆序游标；每步弹一个 token，弹完按是否被截断选 `Stop` 或 `Length`。注意所有裁决（admit/retire/push/finish）**全部**写在 `step` 里，`submit` 只有一行 `push`。

#### 4.1.4 代码实践

**实践目标**：建立「三方法分工」的手感——亲眼确认所有账本写入都发生在 `step` 的调用栈里，`submit` 与 `metrics` 一行账都不碰。

**操作步骤**：

1. 打开 [driver.rs:L124-L162](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/driver.rs#L124-L162) 与 [echo-server.rs:L116-L167](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/examples/echo-server.rs#L116-L167)，做一张三列对照表：方法 ×（两份 EchoScheduler 各做了什么 × 是否触碰 `ledger`）。
2. 统计两份实现里 `ledger.` 前缀调用的出现方法：`admit`、`is_aborted`、`retire`、`completion_tokens`、`push_tokens`、`finish`——逐个标记它出现在 `submit`/`step`/`metrics` 中的哪一个。预期：**全部且仅在 `step`**。
3. 回答：两份实现的 `metrics()` 为什么每次现算（`self.running.len()`）而不是缓存一个字段？提示：`metrics` 拿 `&self`，而队列长度在 `&mut self` 的 `step` 里变化；现算让「每迭代发布」天然正确，缓存则需要两处同步。

**需要观察的现象**：对照表里 `submit` 行与 `metrics` 行的「触碰 ledger」列全为否。

**预期结果**：三方法分工在这两份实现里零例外。此实践为纯阅读型，无需运行；结论可立即核验。

#### 4.1.5 小练习与答案

**练习 1**：如果把 `submit` 改成 `fn submit(&mut self, request: QueuedRequest) -> bool`（返回「是否接纳」，比如队列满时拒绝），会破坏契约的哪条性质？

**参考答案**：破坏「晚裁决零代价」。现在的论证是：`step` 里补记的裁决与 submit 时立即裁决落在同一条 `StepOutputs` 里（因为 driver 每迭代只 commit 一次）。一旦 `submit` 能当场拒绝，调度器就必须**在 submit 里写账本**（`reject` 需要记账才能让客户端收到 `Rejected` terminal），裁决写入点从「单一发射点 `step`」分裂成两处，u3-l4 讲的「节奏由 driver 拥有」随之瓦解；而且 `submit` 签名里根本没有 ledger 参数，这也是类型层面的明确拒绝。

**练习 2**：qwen3 线的 LoRA 适配器装卸是一个控制通道，为什么它不能成为 `Scheduler` 的第四个方法（比如 `fn lora_control(&mut self, ...)`）？

**参考答案**：模块文档（[driver.rs:L6-L9](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/driver.rs#L6-L9)）明说：「Anything beyond them (e.g. LoRA control) is not the contract's business: a scheduler that serves an extra capability captures its own channel before spawn and drains it inside `step`」。契约面每加一个方法，八条模型线全都得面对它（哪怕多数线返回「不支持」）；而「捕获自己的通道、在自己的 `step` 里排空」把额外能力完全留在实现内部——不动 trait，driver 循环也不用为它加一轮排空。实际代码里 LoRA 控制走的是独立的 `LoraClient`（[u3-l1](u3-l1-contract-overview.md) 讲过 `Engine.lora: Option<LoraClient>`，`Option` 即能力）。

**练习 3**：`Scheduler` 的 trait 约束为什么只有 `Send`，不需要 `Sync`？

**参考答案**：调度器被 `spawn_scheduler` 移动进一个专属 OS 线程（[driver.rs:L45-L52](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/driver.rs#L45-L52)），从此单线程独占所有权——任何时刻只有一个线程能碰它，不存在共享引用，自然不需要 `Sync`。这也让模型 crate 的实现自由度最大：内部可以用任何非 `Sync` 的类型（比如裸 CUDA 句柄的包装）。

### 4.2 drive 轮询循环：一份代码代替八份纪律

#### 4.2.1 概念说明

`drive` 是整个 step 契约的**心脏起搏器**：它不认识任何模型，却规定了两件事——**每迭代发生什么**（顺序）与**每迭代至多对外说什么**（一条 `StepOutputs`）。把 u3-l3/u3-l4 的两块拼图放进来就完整了：

- u3-l3 说「一个调度器步产出一条 `StepOutputs`」——**谁把『步』定义成迭代、谁保证恰一条**？`drive`。
- u3-l4 说「`commit_step`/`fail_all` 只有 driver 能碰」——**driver 在哪调用它们**？`drive`。

「约定变代码」清单：

| 若散落到各模型 crate 的约定 | 写错的风险 | `drive` 里的落点 |
|---|---|---|
| 每迭代先收请求再干活 | 先干活后收请求 → 新请求多等一整步 | 内层排空循环（先于 `step`） |
| 提交先开户再交给调度器 | 调度器拿到无账户请求，记账必 panic | `register` → `submit` 的固定顺序 |
| 每迭代恰好一次 commit | 多次 commit → 一步拆成多条消息，压扁保证失效；漏 commit → 更新滞留 | L84 恰一次 `commit_step` |
| metrics 每迭代发布一次 | 读者读到陈旧 gauge，路由决策失真 | L82-L83 快照 + 发布 |
| 致命错误要写销**全部**在途请求 | 客户端挂死等待 | `fail_all` + 再 commit 一次 |
| 空闲时如何自旋 | `sleep` 引入毫秒级尾延迟 | `std::hint::spin_loop` |
| 前端关闭后何时退出 | 过早退出丢请求；过晚退出线程泄漏 | 断连 **且** 快照排空，二者兼备 |

#### 4.2.2 核心流程

`drive` 的一次迭代（与真实代码逐行对应，见 4.2.3）：

```
外层 loop
 ├─ ① 内层 loop：排空 submissions（crossbeam try_recv）
 │     Ok(envelope)   → ledger.register(envelope) → scheduler.submit(queued)
 │     Err(Empty)     → break（现在没有，继续本迭代）
 │     Err(Disconn.)  → submissions_open = false; break（永远没有，进入关停判定）
 ├─ ② step = scheduler.step(&mut ledger)          ← 本迭代的全部工作与记账
 ├─ ③ snapshot = scheduler.metrics()
 │    metrics_changed = metrics.publish(&snapshot) ← 写共享单元
 ├─ ④ ledger.commit_step(metrics_changed)          ← 至多发出一条 StepOutputs
 ├─ ⑤ step == Err？
 │     是 → log::error → ledger.fail_all("engine fatal: {错误原文}")
 │          → ledger.commit_step(false) 把写销冲出去 → return（线程结束）
 │     否 ↓
 └─ ⑥ snapshot 空转（running == 0 且 waiting == 0）？
        submissions_open == false → log::info → return（排空退出）
        否则 → std::hint::spin_loop() → 回到 ①
```

三个容易错过的细节：

1. **致命迭代的「遗言」也会被发出**：`step` 返回 `Err` 前可能已经记了半步账（比如 `admit` 之后才崩）——L84 的 `commit_step` 在错误检查**之前**，所以这些部分更新先落地；随后 `fail_all` 只写销**仍然开着**的账户（已 finish 的不受影响），再由第二次 `commit_step(false)` 冲出去。线上顺序：部分更新 → `Failed` terminals。
2. **空转判定用的是本迭代的快照**：③ 拍的 `snapshot` 在 ⑥ 复用，不再重拍——`step` 之后到下一次 `step` 之间调度器状态不变（单线程独占），所以快照永远新鲜。
3. **「一条 `StepOutputs`」不等于「每迭代必有消息」**：`commit_step` 在「更新为空 **且** metrics 未变」时**不发**（见 4.2.3 的账本侧代码）。空转的迭代对外完全静默；而「metrics 变了但更新为空」会发一条 `updates` 为空的**唤醒消息**——这正是 wiring.rs 文档说的「metrics are a shared cell, with changes signaled on the step stream」。

#### 4.2.3 源码精读

`drive` 本体：

> [driver.rs:L59-L80](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/driver.rs#L59-L80) —— 解构 `SchedulerBackend`（拿走提交通道、账本、metrics 发布器与退出守卫），然后内层循环排空提交：每条信封先 `ledger.register(envelope)`（开户，解除信封的 drop 炸弹、把应答责任移入账户），再 `scheduler.submit(queued)`。`Disconnected` 只置 `submissions_open = false`，**不当场退出**——还要等 ⑥ 的排空判定。

> [driver.rs:L81-L84](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/driver.rs#L81-L84) —— 每迭代的固定四拍：`step` → `metrics()` 快照 → `publish`（返回是否变化）→ `commit_step(metrics_changed)`。注意 commit 在错误检查之前——致命迭代的半步账也要落地。

> [driver.rs:L85-L93](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/driver.rs#L85-L93) —— 致命路径：`log::error!` 记下错误链，`ledger.fail_all(&format!("engine fatal: {error:#}"))` 把**真实错误原文**写进每一个未答复请求的 `Failed` terminal，再 `commit_step(false)` 冲出发送，`return` 结束线程。注释点明设计意图：「账本里每个未答复请求都有账户，所以写销必然触达全部——包括调度器自己跟丢的那些」。

> [driver.rs:L94-L103](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/driver.rs#L94-L103) —— 空转分支：最新快照 `num_running_reqs == 0 && num_waiting_reqs == 0` 时，若提交通道已断则排空退出（`log::info!("scheduler drained after frontend shutdown, exiting")`）；否则 `std::hint::spin_loop()` 放松核心的发射端口再探一轮。注释强调「busy iterations never reach this hint」——有活干的迭代从不空转提示，延迟不受影响。

commit 与写销的账本侧（u3-l4 的「Driver face」三方法中的两个）：

> [ledger.rs:L342-L351](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L342-L351) —— `commit_step(metrics_changed)`：取走本迭代的合并缓冲，`updates` 为空且 metrics 未变就直接返回（**空转迭代对外静默**）；否则发出一条 `StepOutputs { updates }`——这同时是「步」的边界与「metrics 变了」的信号。接收端已关闭（前端走了）也只是忽略发送错误——driver 会经提交通道感知并收摊。

> [ledger.rs:L353-L361](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L353-L361) —— `fail_all(message)`：把账本里**仍然开着**的全部账户逐个 `fail`，消息用 driver 拼好的错误原文（而非账本 Drop 扫尾的通用文案）——注释称之为「the driver's teardown sweep after a fatal `step` error, carrying the real error」。

metrics 的快照语义（`metrics()` 该返回什么）：

> [metrics.rs:L10-L33](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/metrics.rs#L10-L33) —— `SchedulerMetrics`：两个 KV 块 gauge（`kv_used_blocks` 是带外消费者打分的负载信号，`kv_total_blocks` 是整池容量，两者相除即分数占用）、两个请求计数 gauge（running/waiting）、两组 `Option` 包裹的累计计数器（`spec_decode`、`prefix_cache`——`None` 即「无此能力」，不冒充 0）。文档强调读者只看到最新快照，所以**计数器必须是累计值**。

> [metrics.rs:L80-L99](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/metrics.rs#L80-L99) —— `SpecDecodeCounters` 的累计值论证原话：「Use totals rather than per-step deltas, because a `SchedulerMetrics` reader only keeps the newest value: a reader that misses a step would lose that delta for good, but it can still read totals correctly」。这是 4.2.2 细节 3 的镜像论证：发布端每迭代覆盖共享单元 → 读取端可能错过中间值 → 增量会永久丢失，累计不会。

#### 4.2.4 代码实践

**实践目标**：用三个现成的 driver 测试亲眼验证「每迭代至多一条消息」「空 updates 唤醒消息」「致命写销携带错误原文」三件事。`pegainfer-frontend` 没有 CUDA 依赖（见其 `Cargo.toml` 的依赖表），本实践**不需要 GPU**。

**操作步骤**：

1. 在仓库工作区根目录运行：
   ```bash
   cargo test -p pegainfer-frontend --lib engine::driver -- --nocapture
   ```
2. 精读三个测试，重点逐行读 [driver.rs:L218-L305](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/driver.rs#L218-L305) 的 `cancellation_notifies_metrics_once_without_request_output`：它用一个 `PausedScheduler`（`step` 一进来就通过 std mpsc 卡住，由测试遥控放行）把 drive 循环**冻结在任意迭代边界上**，从而逐迭代断言步流内容。
3. 对照该测试的三段断言：
   - L282：放行第一步后 `steps.try_recv()` 拿到「admission step」（有 `scheduled` 的那条消息）；
   - L284-L293：`control.abort()` 后放行下一步，拿到一条 `updates` **为空**的消息——这就是「metrics 变了（running 1→0）但无请求输出」的唤醒消息；同时 `handle.metrics().num_running_reqs == 0` 证明共享单元已更新；
   - L296-L301：再放行一步，`steps.try_recv()` 断言 `Empty`——空转迭代（无更新、metrics 未变）**完全静默**。

**需要观察的现象**：测试全部通过；`--nocapture` 下能看到 `log::info!`/`log::error!` 的输出与否取决于 logger 初始化（frontend 测试未装 logger，可能无输出，这不影响断言）。

**预期结果**：`engine::driver` 模块的 3 个测试通过（`driven_engine_streams_and_drains_on_shutdown`、`cancellation_notifies_metrics_once_without_request_output`、`fatal_step_fails_in_flight_requests_with_the_error`）。本环境未执行该命令，**待本地验证**。

#### 4.2.5 小练习与答案

**练习 1**：如果把 L84 的 `commit_step` 挪到 L81 的 `step` **之前**（先 commit 再干活），会破坏 u3-l3 的哪条保证？

**参考答案**：会拆碎「一步一条 `StepOutputs`」。commit 提前意味着上一步 `step` 记的账要等到**下一迭代开头**才发出——对外看，同一个调度器步的更新可能被拆进两条消息、或者本迭代 `step` 的新更新与上一迭代的残留混在同一条里；压扁结构 `RequestUpdate` 的「步内顺序」保证（u3-l3）建立在「一步的全部更新恰好在一步的末尾一次性发出」之上。此外致命路径也随之变形：`fail_all` 的写销将不得不与下一迭代纠缠。现在的顺序（step → commit）让「步」与「消息」严格一一对应。

**练习 2**：空转迭代为什么选择 `spin_loop()` 而不是 `thread::sleep(Duration::from_millis(1))`？代价是什么？

**参考答案**：`sleep` 会让线程离开 CPU 至少一个调度量子（毫秒级），新到的提交要等睡醒才被排空——每个空闲后的首个请求平白多一毫秒尾延迟，这与「busy iterations never pause」「循环无停顿」的设计（2.1）冲突；`spin_loop` 是用户态提示指令，线程保持运行，下一轮 `try_recv` 几乎零延迟。代价是这一核在空闲期持续轻负载空转（不进低功耗状态）——对一个「随时可能有请求进来」的服务线程，工程上选择延迟优先。注释（[driver.rs:L99-L102](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/driver.rs#L99-L102)）原话：「relax the core's issue slots before probing again. Latency is untouched — busy iterations never reach this hint」。

**练习 3**：为什么退出条件要求「提交通道 `Disconnected` **且** 最新快照 running/waiting 均为 0」，而不是通道一断就退出？

**参考答案**：`Disconnected` 只说明**前端不会再提交新请求**（所有 `SchedulerHandle` 已 drop），不说明手头的请求做完了。若通道一断就退出，仍在 running 的请求会瞬间失去调度器——它们的账户只能靠账本 Drop 扫尾拿到一个笼统的 `Failed`（u3-l4 的三层保护兜底），本可以正常完成的流被腰斩。「断连 + 排空」两个条件同时满足才退出，正是优雅关停的定义；[driver.rs:L211-L214](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/driver.rs#L211-L214) 的测试（drop handle 后 `join` 成功）验证的正是这条路径。

### 4.3 spawn_scheduler：铸接线、起线程、交回 LiveScheduler

#### 4.3.1 概念说明

`Scheduler` trait 与 `drive` 循环之间还差一层**装配**：谁来把「一个裸的调度器值」变成「一个跑着的服务分区」？答案是一次调用：

```rust
let partition: LiveScheduler = spawn_scheduler("echo", EchoScheduler::default());
```

`spawn_scheduler` 做三件事：

1. **铸接线**：`scheduler_pair()` 一次铸出双端——前端端 `SchedulerHandle`、调度器端 `SchedulerBackend`（内含提交通道接收端、**崭新的账本**、metrics 发布器、退出守卫）。因为两端一体铸出，模型 crate 既不能接错线也不能忘接线（u3-l6 详讲）。
2. **起线程**：用 `std::thread::Builder` 起一个**命名** OS 线程跑 `drive(scheduler, backend)`——调度器值与 backend 一起被 move 进线程，从此单所有权。
3. **交回 `LiveScheduler { handle, join }`**：`handle` 是前端的交互面（提交/步流/metrics/`exited()`），`join` 是线程的 `JoinHandle`——按 [wiring.rs:L155-L160](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/wiring.rs#L155-L160) 的注释，它由 server 在关停时、**drop 掉全部 handle 之后** join。

「跑几个调度器、每个是什么含义（DP 副本？）」完全是模型线的决定，契约只搬运 `Vec<LiveScheduler>` 不附加 rank 语义——echo 线恰好一个（[echo-server.rs:L78-L92](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/examples/echo-server.rs#L78-L92)），qwen3 线可能按 DP 分区数 spawn 多个。

#### 4.3.2 核心流程

一次提交在接线路上的完整旅程（把 4.1/4.2 的循环串起来）：

```
前端侧                                   调度器线程（drive 循环）
──────                                   ──────────────────────
SchedulerHandle::submit(request)
 │ 铸 RequestId（原子自增）
 │ 铸 abort 标志 → RequestControl 返回给调用方
 │ 打包 RequestEnvelope（含 drop 炸弹 + queued_at）
 │ crossbeam send ──────────────────────→ ① try_recv 收到
 │                                          register(envelope) 开户
 │                                          scheduler.submit(queued)
 │                                       ② scheduler.step(&mut ledger)
 │                                          admit/push_tokens/finish…
 │                                       ④ commit_step → tokio mpsc send
 take_steps() 的接收端 ←────────────────── StepOutputs { updates }
 （协议栈翻译循环消费）
```

#### 4.3.3 源码精读

> [driver.rs:L43-L52](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/driver.rs#L43-L52) —— `spawn_scheduler` 全部 9 行：`scheduler_pair()` 铸双端 → 命名线程 `spawn` 跑 `drive(scheduler, backend)`（`.expect("failed to spawn scheduler thread")`——线程创建失败是启动期灾难，panic 合理）→ 组装 `LiveScheduler { handle, join }`。泛型约束 `S: Scheduler + 'static`：`'static` 是 move 进线程的必然要求。

> [wiring.rs:L43-L49](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/wiring.rs#L43-L49) —— `SchedulerBackend`：调度器线程消费与生产的全部资源。字段全 `pub(crate)`；模块文档点明「model code only ever sees the ledger, through the `step` argument」——模型代码唯一摸得到的 backend 部件就是账本，且只在 `step` 的参数里。

> [wiring.rs:L51-L65](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/wiring.rs#L51-L65) —— `MetricsPublisher`：共享单元的唯一写者。用 `Mutex`（而非逐字段原子）的理由写在注释里——读者永远不会看到「撕裂在两个 step 之间」的字段组合；两侧各碰纳秒级、无竞争。`publish` 返回 `changed` 布尔，直接喂给 `commit_step` 决定是否发空唤醒消息。

> [wiring.rs:L85-L113](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/wiring.rs#L85-L113) —— 前端端三入口：`submit`（L85-L95，铸造 id/abort/信封，发送失败也**永不返回错误**——信封 drop 炸弹会替调度器回答 `Failed`）；`take_steps`（L99-L101，步流**只发一次**，`Option` 即「已被取走」）；`exited`（L111-L113，等待 driver 返回或线程 unwind）。

> [wiring.rs:L116-L139](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/wiring.rs#L116-L139) —— `scheduler_pair`：三个通道（crossbeam 提交、tokio 步流、`Arc<Mutex>` metrics 单元）+ `CancellationToken`/`DropGuard`，一次铸出 `SchedulerHandle` 与 `SchedulerBackend`。注意 `RequestLedger::new(step_tx)`——**账本从出生就握着步流发送端**，这是 `commit_step` 能发消息的原因。

> [wiring.rs:L155-L160](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/wiring.rs#L155-L160) —— `LiveScheduler { handle, join }`：注释明确 join 的时序契约——server 在关停时先 drop 全部 handle（提交通道随之断开），driver 排空后退出，**然后**才 join。

> [echo-server.rs:L78-L92](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/examples/echo-server.rs#L78-L92) —— 模型线 `launch` 里的标准用法：`Engine { schedulers: vec![spawn_scheduler("echo", EchoScheduler::default())], info: EngineInfo { kv_capacity: None, servable_len: None }, lora: None }`——一个 `LiveScheduler` 加两个显式的 `None`，就是一条最小模型线的全部交接物。

#### 4.3.4 代码实践

**实践目标**：把「spawn 一个调度器」到「前端 handle」的对应关系在真实代码里走一遍，为综合实践的第二半（完整回路）做好铺垫。

**操作步骤**：

1. 从 [echo-server.rs:L83](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/examples/echo-server.rs#L83) 的 `spawn_scheduler("echo", ...)` 出发，向两个方向追：
   - 向内：`spawn_scheduler` → `scheduler_pair()` → 谁持有 `step_tx`？（账本）谁持有 `exit_guard`？（backend，driver 线程返回时 drop 它 → `exited` 令牌被取消）。
   - 向外：`LiveScheduler` 如何进入 `Engine::schedulers` → `vllm::serve_with_engine_count`（[echo-server.rs:L222-L231](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/examples/echo-server.rs#L222-L231)）→ 协议栈在接线时溶解 `Engine`、取走 handle 与 join。
2. 在工作区里检索 `\.join\.join\(\)` 或 `LiveScheduler` 的使用点，确认「server 关停时先 drop handle 再 join」的注释与真实调用一致。
3. 画一张你自己的时序图：请求 → `handle.submit` → 通道 → `register` → `submit` → `step` → `commit_step` → `take_steps()` 接收端。对照 4.3.2 的版本查漏。

**需要观察的现象**：检索结果显示 join 的调用点集中在 serve 装配/关停路径（frontend 的 vllm 模块与测试），模型 crate 里只 spawn 不 join。

**预期结果**：时序图与 4.3.2 一致；`exited` 的触发源（driver 正常返回 / 致命返回 / 线程 unwind）都能在图上标注。检索结果**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：调度器线程已经因致命错误退出后，前端再调 `handle.submit(request)` 会发生什么？请求的客户端会看到什么？

**参考答案**：`submit` 照常铸造 id、abort 标志与信封，然后 `submit_tx.send(envelope)` 返回 `Err`（接收端已随线程销毁）——代码（[wiring.rs:L91-L93](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/wiring.rs#L91-L93)）把退回的信封直接 `drop`，触发它的 **drop 炸弹**：在步流上以一条 `Failed` terminal 回答该请求。所以 `submit` 永不失败、客户端永不挂死——错误以普通 terminal 的形态流出，与正常终态同一条通路。`handle` 里专门留着 `step_tx` 的 clone（[wiring.rs:L73-L75](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/wiring.rs#L73-L75)）就是为这条路径服务的。

**练习 2**：`handle.exited()` 在哪三种情况下会解析（resolve）？

**参考答案**：见 [wiring.rs:L109-L113](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/wiring.rs#L109-L113) 的文档——「after a fatal step or a drained shutdown, or its thread has unwound」：① `step` 返回致命 `Err`，driver 写销后 `return`；② 前端 drop 全部 handle、调度器排空后的优雅退出；③ 线程 panic unwind（比如 u3-l4 的记账违规 panic）。三者最终都 drop `exit_guard`（`DropGuard`），取消 `exited` 令牌。

**练习 3**：`MetricsPublisher::publish` 为什么要返回「是否变化」这个布尔值，而不是让读者自己去比较？

**参考答案**：因为读者（协议栈、路由器）只在**被唤醒时**才去读共享单元，而「该醒了」的信号恰恰是步流上的空 `StepOutputs` 消息——是否发这条消息由 `commit_step(metrics_changed)` 决定。若 publish 不返回变化标志，driver 就得自己持有上一份快照做比较（多一份状态、多一处出错面）。`publish` 内部一行 `*current != *snapshot` 顺手算出（[wiring.rs:L59-L64](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/wiring.rs#L59-L64)），`SchedulerMetrics` 派生 `PartialEq` 正是为此。这是「发布者知道变化、读者被变化唤醒」的完整闭环。

## 5. 综合实践

**任务**：亲手改一个调度器的「每步产量」，并独立跑通一次「提交 → Scheduled → terminal」的完整回路。这是把 4.1（三方法）、4.2（一步一条消息）、4.3（spawn 接线）串起来的一个任务。以下代码均为**示例代码**（非项目原有代码），基于 [driver.rs:L116-L215](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/driver.rs#L116-L215) 的既有测试改写。

**准备**（二选一）：

- **选项 A（推荐，最省事）**：在你自己的克隆里，把下面的两个测试**临时**追加到 `pegainfer-frontend/src/engine/driver.rs` 的 `mod tests` 末尾（复用既有的 `request()` 帮手与 `use` 语句），跑完后还原，不要提交。
- **选项 B（不动源码树）**：新建一个草稿 crate，`Cargo.toml` 里加 `pegainfer-frontend = { path = "<仓库>/pegainfer-frontend" }`，并自己抄一份 `request()` 帮手（`Request` 与 `QueuedRequest` 的字段全是 `pub`，`Scheduler`/`spawn_scheduler`/`RequestLedger` 等经 `pegainfer_frontend::engine::*` 扁平 re-export，crate 外可用；参照 echo-server.rs 的导入清单）。

**第一半：每步两 token 的变体。** 改动点只有一个——`step` 里一次 `push_tokens` 推两个 token：

```rust
// 示例代码：EchoScheduler 的「每步两 token」变体
#[derive(Default)]
struct TwoTokenScheduler {
    queued: Vec<(RequestId, usize)>,
    running: Vec<(RequestId, usize)>,
}

impl Scheduler for TwoTokenScheduler {
    fn submit(&mut self, request: QueuedRequest) {
        self.queued.push((request.id, request.request.max_tokens)); // 只转移
    }

    fn step(&mut self, ledger: &mut RequestLedger) -> anyhow::Result<()> {
        for (id, max_tokens) in self.queued.drain(..) {
            ledger.admit(id);                       // 裁决全在 step 里
            self.running.push((id, max_tokens));
        }
        let mut still_running = Vec::new();
        for (id, max_tokens) in self.running.drain(..) {
            if ledger.is_aborted(id) {
                ledger.retire(id);
                continue;
            }
            let next = ledger.completion_tokens(id) as u32;   // 读账本流水
            let batch: Vec<u32> = (next..(next + 2).min(max_tokens as u32)).collect();
            if batch.is_empty() {
                ledger.finish(id, FinishReason::Length);      // 上一轮恰好做满
                continue;
            }
            ledger.push_tokens(id, &batch, &[]);
            if ledger.completion_tokens(id) >= max_tokens {
                ledger.finish(id, FinishReason::Length);
            } else {
                still_running.push((id, max_tokens));
            }
        }
        self.running = still_running;
        Ok(())
    }

    fn metrics(&self) -> SchedulerMetrics {
        SchedulerMetrics {
            num_running_reqs: self.running.len() as u64,
            num_waiting_reqs: self.queued.len() as u64,
            ..SchedulerMetrics::default()
        }
    }
}

// 示例代码：统计「几步到达 terminal」
#[test]
fn two_token_variant_finishes_in_half_the_steps() {
    let partition = spawn_scheduler("test-two-token", TwoTokenScheduler::default());
    let mut handle = partition.handle;
    let mut steps = handle.take_steps().expect("step stream");

    let _control = handle.submit(request(4)); // max_tokens = 4
    let (mut tokens, mut terminal, mut steps_seen) = (Vec::new(), None, 0usize);
    while terminal.is_none() {
        let step = steps.blocking_recv().expect("step message");
        steps_seen += 1;                       // 每条消息 = 一次 drive 迭代
        for update in step.updates {
            tokens.extend(update.tokens);
            if let Some(t) = update.terminal { terminal = Some(t); }
        }
    }
    assert_eq!(tokens, vec![0, 1, 2, 3]);      // token 流不变
    assert_eq!(steps_seen, 2);                 // 原版 EchoScheduler 需要 4 步
    drop(handle);                              // 断开提交通道
    partition.join.join().expect("driver drains and exits");
}
```

**第二半：完整回路——`Scheduled` 与 terminal。** 用原版 `EchoScheduler` 再跑一个只关心事件形状的测试：

```rust
// 示例代码：完整回路的形状断言
#[test]
fn round_trip_reads_scheduled_then_terminal() {
    let partition = spawn_scheduler("test-roundtrip", EchoScheduler::default());
    let mut handle = partition.handle;
    let mut steps = handle.take_steps().expect("step stream");

    let _control = handle.submit(request(2));
    let (mut saw_scheduled, mut terminal) = (false, None);
    while terminal.is_none() {
        for update in steps.blocking_recv().expect("step message").updates {
            if update.scheduled.is_some() { saw_scheduled = true; } // 接纳事实
            if let Some(t) = update.terminal { terminal = Some(t); } // 终态
        }
    }
    assert!(saw_scheduled, "第一步消息必须携带 scheduled");
    assert!(matches!(terminal, Some(Terminal::Finished { .. })));
    drop(handle);
    partition.join.join().unwrap();
}
```

**运行**（工作区根目录；无需 GPU）：

```bash
cargo test -p pegainfer-frontend --lib two_token_variant round_trip_reads -- --nocapture
```

**需要观察的现象与预期结果**：

1. `two_token_variant`：`tokens == [0,1,2,3]` 与原版完全一致（token 流只由「读了多少账本流水」决定，与每步推几个无关）；`steps_seen == 2`——同样的 `max_tokens = 4`，原版每步一个 token 要 4 条消息才到 terminal，变体 2 条，**提前一半**。这正是 4.2 的「一步一条 `StepOutputs`」的直接推论：步的粒度由 `step` 的实现决定，driver 只是忠实转发。
2. `round_trip`：第一条消息的 update 里 `scheduled` 为 `Some`（接纳事实与首批 token 同步到达——`RequestUpdate` 的区段结构），terminal 是 `Finished`；`drop(handle)` 后 `join` 成功返回（断连 + 排空的优雅退出路径，4.2 练习 3）。
3. 两个测试跑完线程干净退出，无悬挂。

以上运行结果为基于源码推演的预期，**待本地验证**。若选项 B 的草稿 crate 编译报错，优先检查导入清单（`Scheduler`、`SchedulerMetrics`、`RequestLedger`、`QueuedRequest`、`RequestId`、`Terminal`、`FinishReason` 均来自 `pegainfer_frontend::engine`）。

## 6. 本讲小结

- **`Scheduler` 三方法就是模型线的全部运行时义务**：`submit` 只做所有权转移（裁决延迟到 `step` 零代价，因为 driver 每迭代 commit 一次）、`step` 是唯一裁决点与唯一记账点、`metrics` 是 `&self` 只读快照；超出三方法的能力（如 LoRA 控制通道）由调度器自己捕获、在自己的 `step` 里排空。
- **`drive` 把循环纪律写成了代码**：每迭代固定四拍——排空提交（每条先 `register` 开户再 `submit` 转移）→ `step` → 快照并 `publish` metrics → `commit_step` 恰好一次；八条模型线共享同一份骨架，不存在八份人肉纪律。
- **「一条 `StepOutputs`」有三档**：有更新则带更新发出；无更新但 metrics 变了则发一条 `updates` 为空的**唤醒消息**（读者该去重读共享单元）；两者皆无则**完全静默**——空转迭代对外不可见。
- **致命路径**：`step` 返回 `Err` → 本迭代的半步账先照常 commit → `fail_all("engine fatal: {错误原文}")` 写销**每一个**未答复请求（含调度器跟丢的）→ 再 commit 一次冲出发送 → 线程返回，`exit_guard` drop 使 `handle.exited()` 解析。
- **退出与空闲**：优雅退出要求「提交通道断开 **且** 最新快照 running/waiting 均为 0」；空转迭代以 `std::hint::spin_loop` 放松发射端口但绝不睡眠——有活干的迭代永远到不了这条 hint。
- **`spawn_scheduler` 是装配点**：`scheduler_pair` 铸双端（前端 `SchedulerHandle` / 调度器 `SchedulerBackend`，账本出生即握步流发送端）→ 命名线程跑 `drive` → 交回 `LiveScheduler { handle, join }`；`handle.submit` 永不失败——调度器已死时由信封 drop 炸弹以 `Failed` terminal 作答。

## 7. 下一步学习建议

下一讲 [u3-l6](u3-l6-wiring-and-handle.md) 把本讲的 `scheduler_pair` 接线彻底拆开：三个通道各自的选型理由（提交 crossbeam / 步流 tokio mpsc / metrics 共享单元）、`Engine`/`EngineInfo` 交接包的显式 `None` 语义，以及 legacy `EngineHandle` 在多分区下的最低负载路由（`running + 4×waiting`）。之后进入单元四：[u4-l1](u4-l1-serve-paths.md) 会看到 serve 装配层如何溶解 `Engine`、取走 `take_steps()` 的接收端，把本讲的步流接进 vLLM EngineCore 协议。若想先巩固本讲，建议重读 [driver.rs](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/driver.rs) 的三个测试——它们分别对应本讲的三条主线：正常流与排空、逐迭代消息形状、致命写销。
