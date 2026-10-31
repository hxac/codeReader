# u4-l6 可观测性：SchedulerMetrics 从调度器到 /metrics

## 1. 本讲目标

学完本讲，你应该能够：

- 逐字段说出 `SchedulerMetrics` 的含义，特别是 `spec_decode` 与 `prefix_cache` 两组**累计计数器**（cumulative counters）的语义与不变式。
- 解释发布通道的两代差异：stepped 引擎把快照写进 `MetricsPublisher` 共享单元、用步流上的「唤醒消息」通知变化；legacy 引擎用 `watch` 通道，`changed()` 只在真实变化时唤醒。
- 走通桥侧 stats 上报：`StatsTracker` 如何把累计值差分成按批 delta，legacy 桥的 `publish_scheduler_stats` 任务与 stepped 桥的「发批时现读」两条路径分别怎么走。
- 论证「计数器为什么必须是累计值」：读者只能看到最新快照，任何错过一步的读者都会永久丢失 per-step delta，但读累计值永远正确。
- 独立完成一次 `/metrics` 观测实践，并写出 `prefix_cache.hits` 从调度器到 `/metrics` 的完整传播路径。

## 2. 前置知识

- **快照 vs 计数器**：监控领域常分两类量。「快照/仪表」（gauge）表示"此刻的状态"，如当前有多少请求在跑，读到多少就是多少；「计数器」（counter）表示"从某个起点开始的累计"，只增不减（单调），读者用两次读数相减得到区间增量。本讲的 `SchedulerMetrics` 同时携带两者。
- **通道的"推送"与"拉取"语义**：Rust 里共享数据有两种典型方式。`Arc<Mutex<T>>` 共享单元是"拉取"——读者随时锁上读最新值，但没人通知你它变了；`tokio::sync::watch` 通道是"推送"——写端替换值，读端 `await changed()` 只在值真正变化时醒来。本讲的两代引擎各用一种。
- **为什么会"错过一步"**：u3-l5 讲过 stepped 引擎的 `drive` 循环从不睡眠、空转时用 `spin_loop` 探测；而监控读者（桥、路由、抓取端）是异步任务，读的频率与调度器步频无关。两边频率不一致，就意味着读者**注定**看不到中间某些步的值——这是"必须累计"的根源。
- **前缀缓存（prefix cache）与投机解码（spec decode）**：前者把历史 prompt 的 KV 状态缓存复用（u5-l1 会深入 `KvPrefix`），后者用一个草稿模型一次 propose 多个 token、由主模型批量验证。两者都是"事件"，天然适合计数；引擎可以没有它们，所以计数器用 `Option` 携带。
- 承接 u3-l5（`Scheduler` trait 与 `drive` 循环四拍）、u3-l6（`scheduler_pair` 接线）、u4-l3（两代引擎桥与 zmq 输出通道）。

## 3. 本讲源码地图

| 文件 | 作用 |
|------|------|
| `pegainfer-frontend/src/engine/metrics.rs` | `SchedulerMetrics`、`PrefixCacheCounters`、`SpecDecodeCounters` 的定义与语义文档；"为什么是累计值"的注释依据就在这里 |
| `pegainfer-frontend/src/engine/wiring.rs` | `MetricsPublisher`（共享单元）与 `SchedulerHandle::metrics()`（读端）；`scheduler_pair` 铸出双端 |
| `pegainfer-frontend/src/engine/driver.rs` | `Scheduler::metrics()` 方法；`drive` 循环里"每迭代发布一次"的那两行 |
| `pegainfer-frontend/src/engine/ledger.rs` | `commit_step(metrics_changed)`——快照变化时发出空更新的「唤醒消息」 |
| `pegainfer-frontend/src/engine/handle.rs` | legacy 引擎的 `metrics_watches`：watch 通道的挂载、拓扑语义与路由消费 |
| `pegainfer-frontend/src/vllm/bridge.rs` | `StatsTracker` 差分、`scheduler_stats_from` 仪表映射、`publish_scheduler_stats` 任务（legacy 发布路径） |
| `pegainfer-frontend/src/vllm/bridge/stepped.rs` | stepped 桥的 stats 路径：发批时现读共享单元，把 stats 盖在既有输出批上 |
| `pegainfer-frontend/src/vllm/mod.rs` | 服务装配处两代分支如何接线 metrics；`disable_log_stats: false` 这条"生命线" |
| `pegainfer-frontend/examples/echo-server.rs` | 无 GPU 活体标本：echo 引擎的 `metrics()` 实现与运行方式 |

## 4. 核心概念与源码讲解

### 4.1 SchedulerMetrics 与两类累计计数器

#### 4.1.1 概念说明

`SchedulerMetrics` 是调度器在**每一步之后**发布的"自画像"：占用类字段回答"引擎现在多满"，计数器字段回答"引擎开工以来做了多少事"。它同时服务三类消费者：

1. **HTTP 前端**（vllm-server）：把 stats 记入 Prometheus 注册表，暴露 `/metrics`。
2. **DP 负载均衡 / 路由**：按各分区的负载分数挑目的地（u3-l6 讲过 `running + 4 × waiting` 最低分）。
3. **带外消费者**（如 Dynamo KV 路由器）：拿 `kv_used_blocks` 对 `kv_total_blocks` 打分。

计数器为什么必须累计：读者只看得到**最新一份快照**——stepped 引擎发进共享单元、legacy 引擎发进 watch 通道，两者都只保留最新值。如果计数器存 per-step delta，一个错过某步的读者就把那份 delta 永久丢了；存累计值则无论何时读都正确，区间增量由**有基线的读者**自己差分。

#### 4.1.2 核心流程

- 调度器（模型 crate）维护自己的状态，`metrics()` 被调用时现场拼一份快照。
- 占用类字段（`kv_used_blocks`、`num_running_reqs`、`num_waiting_reqs`）是即时值；分数式用法由读者推导：

\[ \text{kv\_cache\_usage} = \frac{\text{kv\_used\_blocks}}{\text{kv\_total\_blocks}} \]

- 两组计数器语义（均自引擎/drafter 启动起单调递增，单位是 **prompt token 数**（前缀缓存）或**次数**（投机解码））：

| 计数器 | 单位 | 携带条件 |
|--------|------|----------|
| `prefix_cache.queries` / `hits` / `host_queries` / `host_hits` / `external_unspecified_hits` | prompt token | 引擎有前缀缓存时 `Some`，否则 `None` |
| `spec_decode.num_drafts` / `num_draft_tokens` / `num_accepted_tokens` / `num_accepted_tokens_per_pos` | 次数 / token | 加载了草稿模型时 `Some`，否则 `None` |

- 前缀缓存计数器的不变式：\( \text{host\_hits} + \text{external\_unspecified\_hits} \le \text{hits} \)，其余命中按设备命中报告。
- 投机解码的接受直方图是"前缀形"的：一次验证接受 \( n \) 个 token，就给位置 \( 0..n \) 各记一次。

#### 4.1.3 源码精读

`SchedulerMetrics` 结构体与字段——模块文档点明它是"每迭代的占用仪表 + 模型线提供的更丰富计数器"：

[pegainfer-frontend/src/engine/metrics.rs:20-33](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/metrics.rs#L20-L33)
这段定义了 6 个字段：`kv_used_blocks` 与 `kv_total_blocks`（后者即对外广告的可服务上限，读者可一次读出分数占用而无需二次查询）、`num_running_reqs` / `num_waiting_reqs` 两个占用仪表、`spec_decode` 与 `prefix_cache` 两个 `Option` 计数器（`None` 即"引擎没有这个能力"）。

「必须累计」的第一处注释依据，就在结构体文档里：

[pegainfer-frontend/src/engine/metrics.rs:10-19](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/metrics.rs#L10-L19)
文档直说：读者永远只看到最新快照——stepped 引擎发共享单元、legacy 引擎走 watch——**所以这里携带的计数器必须是累计总额，不是每步 delta**。

`SpecDecodeCounters` 的文档把论证补完整：

[pegainfer-frontend/src/engine/metrics.rs:80-84](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/metrics.rs#L80-L84)
"用总额而非每步 delta，因为读者只保留最新值：错过一步的读者会永久丢掉那份 delta，但读总额仍然正确。"

前缀缓存计数器与它的不变式：

[pegainfer-frontend/src/engine/metrics.rs:35-56](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/metrics.rs#L35-L56)
`PrefixCacheCounters` 以 prompt token 计数、自引擎启动单调；`external_unspecified_hits` 的注释解释了为什么需要它——connector 从 host/对等体/SSD 一次调用里只报块数，引擎无法归因，硬标成 `host` 会把对等体和磁盘的命中算错账。

`observe_draft` 展示"前缀形"记账：

[pegainfer-frontend/src/engine/metrics.rs:112-121](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/metrics.rs#L112-L121)
每次验证：`num_drafts` 加一、proposal 数与接受数分别累计，再给前 \( n \) 个位置各加一——直方图因此单调不降（配套单测 `observe_draft_tallies_a_prefix_per_position` 在 [metrics.rs:127-139](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/metrics.rs#L127-L139) 断言了 `[2, 1, 0]` 这个形状）。宽度上限 `MAX_SPEC_TOKENS = 32` 固定了直方图数组宽度，超出直接拒绝而非截断（[metrics.rs:101-110](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/metrics.rs#L101-L110)）。

背景注脚：这两组"真计数器"是 `feat(observability): report real prefix-cache query/hit counters in /metrics (#953)`（提交 0a26c394）引入的——此前 `/metrics` 里的前缀缓存数字并不能反映真实查询/命中。

#### 4.1.4 代码实践

**实践目标**：用现有单测验证计数器的记账形状与宽度拒绝行为。

**操作步骤**：

1. 在工作区根目录运行：
   ```bash
   cargo test --release -p pegainfer-frontend --lib engine::metrics -- --nocapture
   ```
2. 阅读两个测试：`observe_draft_tallies_a_prefix_per_position` 与 `oversized_k_is_rejected_not_truncated`。
3. 手工推演：先 `observe_draft(2, 2)` 再 `observe_draft(3, 1)`，对照断言算出 `num_draft_tokens = 5`、`num_accepted_tokens = 3`、直方图 `[..., 2, 1, 0, ...]`。

**需要观察的现象**：两个测试通过；`--nocapture` 下无额外输出（测试只做断言）。

**预期结果**：`engine::metrics` 模块两个测试全部通过，与你手工推演的数字一致。（具体运行输出待本地验证。）

#### 4.1.5 小练习与答案

**练习 1**：为什么 `kv_used_blocks` 可以是即时值，而 `prefix_cache.hits` 必须是累计值？

**答案**：`kv_used_blocks` 是 gauge——它描述"此刻"的占用，下一份快照会覆盖它，丢一步不影响"现在多满"这个问题的答案。`hits` 是 counter——它回答"开工以来一共命中多少"，任何一次读数都应该能参与差分；如果存 per-step delta，错过一步的读者就永久丢掉那一步的命中数，而读者（桥、抓取端）与调度器步频天然不同步。

**练习 2**：`host_hits + external_unspecified_hits ≤ hits` 这个不变式在什么场景下取到严格小于？

**答案**：命中发生在设备本地 KV 池（无需 connector）时。`hits` 是全层级命中总和；`host_hits` 与 `external_unspecified_hits` 只刻画经 connector 恢复的部分，剩下的差额就是设备命中，桥侧会把它记入 `source="device"`（见 4.3.3）。

**练习 3**：一个 draft checkpoint 声明 K=33 会发生什么？

**答案**：`SpecDecodeCounters::new(33)` 返回 `Err(SpecWidthUnsupported { num_spec_tokens: 33 })`，错误文本指出 K 超过 `MAX_SPEC_TOKENS=32`——拒绝而非截断，因为截断会静默丢失第 33 个位置的接受统计。

### 4.2 发布通道两代差异：MetricsPublisher 共享单元与 legacy watch

#### 4.2.1 概念说明

同一个 `SchedulerMetrics`，两代引擎用两种通道发布：

- **stepped 引擎（新契约）**：`scheduler_pair` 铸出 `Arc<Mutex<SchedulerMetrics>>` 共享单元。`drive` 循环每迭代调 `scheduler.metrics()`，经 `MetricsPublisher::publish` 写入单元格——只在值**真正变化**时返回 `true`。共享单元没有推送机制，所以变化信号"搭便车"走步流：`commit_step(metrics_changed)` 在没有请求更新但 metrics 变了时，发出一条**空更新的 `StepOutputs`**（u3-l5 称之为"唤醒消息"）。
- **legacy 引擎（旧契约）**：模型 crate 自己 `watch::channel(SchedulerMetrics, ...)`，引擎调度循环每步 `send_replace` 最新快照；`EngineHandle` 用 `with_metrics_watches` 挂上接收端。`watch::Receiver::changed()` 在值变化时唤醒一次、空闲时安静——是天然的推送语义。

关键差异一句话：**stepped 的共享单元是"最新值 + 步流搭便车通知"，legacy 的 watch 是"最新值 + 通道自带通知"**。`changed()` 的"只在真实变化时唤醒"由 watch 语义保证（写端相同值不触发）；stepped 侧则由 `publish` 的 `changed` 比较显式复刻。

#### 4.2.2 核心流程

stepped 引擎每个 drive 迭代：

```
排空提交 → step → snapshot = scheduler.metrics()
        → metrics_changed = publisher.publish(&snapshot)   # 比较并写入单元格
        → ledger.commit_step(metrics_changed)              # 有更新或有变化 → 发 StepOutputs
```

- `commit_step(false)` 且无更新 → 什么都不发（空转迭代安静）。
- `commit_step(true)` 且无更新 → 发一条空 `StepOutputs`，桥收到后产出一条 stats-only 输出批。

legacy 引擎：

```
引擎调度循环每步 → watch 发送端 send_replace(snapshot)
EngineHandle.metrics_watches[i] ← with_metrics_watches 挂载
vllm 装配 → 每个分区克隆一个 watch 接收端交给桥
桥 spawn publish_scheduler_stats 任务 → await changed() → 现读 borrow_and_update → 差分 → 发 stats-only 批
```

注意 watch 通道在 legacy 侧身兼两职：**metrics 载体**与 **DP 拓扑声明**（`scheduler_partition_count() = metrics_watches.len()`，u3-l6 讲过"两个真相"）。

#### 4.2.3 源码精读

`MetricsPublisher`——单元格与"变化"检测：

[pegainfer-frontend/src/engine/wiring.rs:51-65](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/wiring.rs#L51-L65)
`publish` 锁住单元格，比较新旧快照（`SchedulerMetrics` 是 `PartialEq`），写入并返回是否变化。注释解释为什么用 `Mutex` 而非逐字段原子：读者永远不会看到"撕裂"在两步之间的字段组合；两侧各碰它纳秒级、无竞争。

`drive` 循环里"每迭代发布一次"的两行：

[pegainfer-frontend/src/engine/driver.rs:81-84](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/driver.rs#L81-L84)
step 之后立刻取快照、发布、把变化标志交给 `commit_step`——这是 u3-l5 "四拍"里的后两拍。`Scheduler::metrics` 只是只读快照方法（[driver.rs:39-40](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/driver.rs#L39-L40)）。

`commit_step` 的唤醒消息：

[pegainfer-frontend/src/engine/ledger.rs:343-351](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L343-L351)
更新为空**且** metrics 未变 → 直接返回不发消息；否则发一条 `StepOutputs`（更新可能为空）。这条空消息就是共享单元格的"变化通知"。

读端——`SchedulerHandle::metrics()`：

[pegainfer-frontend/src/engine/wiring.rs:103-107](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/wiring.rs#L103-L107)
文档点名三个读取时机："路由一个请求时、收到一步时、服务一次抓取时"——stepped 桥用的正是第二个（见 4.3.3）。

legacy 侧的 watch 挂载与拓扑语义：

[pegainfer-frontend/src/engine/handle.rs:151-167](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/handle.rs#L151-L167)
`with_metrics_watches` 断言向量非空——"空向量是无效引擎而非缺 metrics 的单分区"，因为它同时是前端可见的 DP 拓扑（[handle.rs:169-172](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/handle.rs#L169-L172)）。

`metrics_watch_for` 的文档——`changed()` 的唤醒语义：

[pegainfer-frontend/src/engine/handle.rs:174-184](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/handle.rs#L174-L184)
"负载下每调度步唤醒一次，空闲时保持安静，所以消费者按真实变化重发布而不是轮询"——这正是 legacy 桥 stats 任务敢于 `await changed()` 的依据。

watch 的第二个消费者——legacy 路由的最低负载评分：

[pegainfer-frontend/src/engine/handle.rs:238-260](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/handle.rs#L238-L260)
`least_loaded_partition` 用 `watch.borrow()` 现读快照算 `running + 4 × waiting`（与 vLLM DP 负载均衡同权重）；没有 feed 的分区计 0 分。配套测试 `metrics_watches_define_scheduler_partitions`（[handle.rs:445-474](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/handle.rs#L445-L474)）演示了模型侧如何用 `watch::channel` + `send_replace` 喂这条通道（[handle.rs:375-396](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/handle.rs#L375-L396)）。

#### 4.2.4 代码实践

**实践目标**：亲眼看到"空更新 + metrics 变化"会发出一条消息、而完全空闲的迭代不发。

**操作步骤**：

1. 阅读 [driver.rs:107](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/driver.rs#L107) 起的 `mod tests`（`EchoScheduler`），注意测试里对 `commit_step(false)` 的调用出现在哪些断言路径。
2. 运行既有驱动测试：
   ```bash
   cargo test --release -p pegainfer-frontend --lib engine::driver -- --nocapture
   ```
3. 对照 [ledger.rs:433](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L433)、[ledger.rs:471](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L471) 等测试里的 `commit_step(false)`：账本单测直接以 `false` 驱动，等价于"metrics 未变"的迭代。

**需要观察的现象**：driver 与 ledger 的单测全部通过；测试从不在"无更新 + metrics 未变"的路径上期待收到消息。

**预期结果**：测试通过；你能在代码里指出 `commit_step` 唯一的发送条件是"更新非空或 metrics_changed"。（运行输出待本地验证。）

#### 4.2.5 小练习与答案

**练习 1**：stepped 引擎的共享单元格没有通知机制，桥怎么知道该重发 stats？

**答案**：变化信号搭步流的便车——`publish` 返回的 `changed` 交给 `commit_step`；当本步没有请求更新但 metrics 变了，账本发出一条空更新的 `StepOutputs`，桥把它当作"该发一条 stats-only 批了"的信号（stepped.rs 的测试 `scheduled_and_metrics_only_steps_publish_load_without_request_outputs` 正是验证这条路径）。

**练习 2**：如果 `MetricsPublisher` 改成逐字段 `AtomicU64`，会损失什么保证？

**答案**：原子字段各自正确，但读者在锁外逐字段读，可能读到"running 来自第 N 步、waiting 来自第 N+1 步"的撕裂快照；`Mutex` 整体替换保证读者看到的永远是一步之内的自洽快照（wiring.rs 的注释原文："a reader never sees fields torn across two steps"）。

**练习 3**：为什么 legacy 引擎可以让桥 spawn 一个常驻的 `publish_scheduler_stats` 任务，stepped 引擎却不行？

**答案**：legacy 调度循环空闲时会停泊（park），watch 的 `changed()` 空闲时安静，任务唤醒频率有界；stepped 的 `drive` 循环忙轮询、每步都在跑，若同样"每次变化发一条"，高频自旋会变成消息风暴——所以 stepped 桥不发独立任务，而是在**发批时现读**单元格、把 stats 盖在本来就要发的批上（bridge.rs L876-L880 的注释原话）。

### 4.3 桥侧 stats 上报：StatsTracker 差分与两条发布路径

#### 4.3.1 概念说明

桥（u4-l3）把引擎伪装成 vLLM EngineCore，stats 也必须翻译成 vLLM 的 `SchedulerStats` 线上形状。翻译分两层：

1. **仪表直译**：`scheduler_stats_from` 把占用字段填进 `SchedulerStats`（`num_running_reqs`、`num_waiting_reqs`、`kv_cache_usage` 分数）。
2. **计数器差分**：`StatsTracker` 持有上次快照的基线，把累计值相减成"本区间 delta"，填进 `spec_decoding_stats` 与 `prefix_cache_stats` / `connector_prefix_cache_stats`。**传输层携带累计值、线上携带 delta**——差分这一步恰好放在拥有连续基线的地方（桥），错过快照也不会错账。

发布到 zmq 输出通道有两条路径：

- **legacy**：常驻任务 `publish_scheduler_stats`——先现发一份当前快照（让仪表在第一步之前就初始化），之后每次 `changed()` 发一条 stats-only 批。
- **stepped**：无独立任务。桥在连接成功后先发一条 stats-only 批，之后在每条本就要发的输出批上盖 `scheduler_stats`（发批时现读单元格）。

上游 vllm-server 收到 `EngineCoreOutputs` 里的 stats，记入共享 Prometheus 注册表——`/metrics` 的最终导出名由**上游 recorder** 决定（代码注释点名的仪表是 `scheduler_running`、`scheduler_waiting`、`kv_cache_usage`；spec-decode 计数器族是 `vllm:spec_decode_*_total`，前缀缓存是 `vllm:prefix_cache_*` / `vllm:external_prefix_cache_*`）。

#### 4.3.2 核心流程

一条 stats 的产线（stepped 引擎为例）：

```
模型 crate 维护累计计数
  → Scheduler::metrics()（drive 每迭代调用）
  → MetricsPublisher.publish → 共享单元格（+步流唤醒消息）
  → 桥 dispatch_step / 连接期首发
      → StatsTracker.stats(&scheduler.metrics())
          ├─ scheduler_stats_from：仪表直译（含 kv_cache_usage 分数）
          ├─ spec_decode_interval：累计 − 基线 = 本批 delta（零草稿区间丢弃）
          └─ prefix_cache_delta：累计 − 基线 = 本批 delta（本地/connector 双侧，重启检测）
      → SchedulerStats 盖进 RequestBatchOutputs.scheduler_stats
  → zmq Push socket → 上游 vllm-server recorder → Prometheus 注册表 → /metrics
```

`prefix_cache_delta` 的两个细节：

- **重启检测**：任一累计值比基线小 ⇒ 引擎重启过，本区间从 0 差分，而不是产出负数或丢新计数。
- **层级拆分**：本地侧 hits = 总 hits − connector 命中，全部记 `source="device"`；connector 侧带 `hits_by_source`（`host` / `external_unspecified`），模仿 vLLM 自己的 offload connector 上报方式。

#### 4.3.3 源码精读

legacy 桥结构体上的 watch 字段：

[pegainfer-frontend/src/vllm/bridge.rs:74-82](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/bridge.rs#L74-L82)
`LocalEngineBridge` 携带 `metrics_watch: Option<watch::Receiver<SchedulerMetrics>>`——legacy 引擎每分区一条负载通道；`None` 表示该分区没接 feed。

仪表直译：

[pegainfer-frontend/src/vllm/bridge.rs:598-612](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/bridge.rs#L598-L612)
`scheduler_stats_from` 填三个仪表，`kv_cache_usage` 在总量为 0 时取 0.0 防除零。文档点名这正是前端 Prometheus 仪表（`scheduler_running` / `scheduler_waiting` / `kv_cache_usage`）与 DP 负载均衡消费的形状。

`StatsTracker`——差分的心脏：

[pegainfer-frontend/src/vllm/bridge.rs:692-732](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/bridge.rs#L692-L732)
`stats()` 每次调用推进基线（"调用后决定不发的调用者丢掉的只是本区间的计数 delta"）。spec-decode 的零草稿区间直接丢弃——所有计数只在 `observe_draft` 里移动，丢掉不丢信息，还避免前端接受率日志除零。计数器中途"从无到有"时基线重置为 0，晚加载的 drafter / 前缀缓存从零差分。

前缀缓存差分与重启检测：

[pegainfer-frontend/src/vllm/bridge.rs:637-690](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/bridge.rs#L637-L690)
`prefix_cache_delta` 先判 `restarted`（任一累计值低于基线 ⇒ 引擎重启），从 0 或旧基线差分；随后拆本地/connector 两侧——本地侧 hits 扣掉 connector 命中后全部落 `source="device"`，connector 侧仅在 host 有活动时出现（`None` 表示本区间无 host 活动）。文档把两侧分别对应到 `vllm:prefix_cache_*` 与 `vllm:external_prefix_cache_*` 计数器族。

spec-decode 差分：

[pegainfer-frontend/src/vllm/bridge.rs:614-635](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/bridge.rs#L614-L635)
每字段 `saturating_sub` 基线，逐位置直方图 zip 相减后截到 `num_spec_tokens` 宽——文档原话："transport carries totals and the wire carries deltas"（传输层带累计、线上带 delta）。

legacy 发布任务：

[pegainfer-frontend/src/vllm/bridge.rs:734-766](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/bridge.rs#L734-L766)
`publish_scheduler_stats` 循环：先 `borrow_and_update` 现发一份（仪表在第一步前初始化），然后在 `shutdown` 与 `changed()` 之间 select——只在真实变化时醒来。丢掉输出通道或负载通道都是致命错误：留着这台引擎注册着，就会留下一堆过期指标和一个请求路由黑洞。

它的 spawn 点与两代差异的"官方解释"：

[pegainfer-frontend/src/vllm/bridge.rs:876-891](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/bridge.rs#L876-L891)
注释原文：legacy 引擎把负载快照重发成 stats-only 批（其调度循环空闲时停泊，watch 节奏有界）；**stepped 桥在这里不传 watch——它的 driver 忙轮询（每次自旋都会变成一条消息），所以它发批时现读，把 stats 盖在本来就要发的批上**。

stepped 桥的两处盖章：

[pegainfer-frontend/src/vllm/bridge/stepped.rs:110-119](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/bridge/stepped.rs#L110-L119)
连接握手成功后立即发一条 stats-only 批（`scheduler_stats` 有值、`outputs` 为空）——与 legacy 任务的"先现发一份"对齐，仪表不等第一步。

[pegainfer-frontend/src/vllm/bridge/stepped.rs:216-220](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/bridge/stepped.rs#L216-L220) 与 [stepped.rs:255-266](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/bridge/stepped.rs#L255-L266)
`stats()` 的注释是关键："counter deltas run from the last **batch** stamped, not the last step run"——它现读 `scheduler.metrics()` 单元格（对应 `SchedulerHandle::metrics` 文档里的"收到一步时读"），`dispatch_step` 把结果盖进每条输出批的 `scheduler_stats` 字段。这样 4.2 的唤醒消息（空更新步）到达时也走同一条 `dispatch_step`，产出一条无输出但有 stats 的批——测试 `scheduled_and_metrics_only_steps_publish_load_without_request_outputs`（[stepped.rs:753-794](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/bridge/stepped.rs#L753-L794)）逐字验证：`backend.metrics.publish(...)` 两次改 `num_running_reqs`，两次 dispatch 都收到 stats-only 批且数值正确。

装配处两代分支的接线：

[pegainfer-frontend/src/vllm/mod.rs:310-334](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/mod.rs#L310-L334)
`LaunchedEngine::Handle` 分支为每个分区造 `LocalEngineBridge`，`metrics_watch: handle.metrics_watch_for(engine_index)`——legacy 引擎的负载通道在这里交给桥。

[pegainfer-frontend/src/vllm/mod.rs:336-362](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/mod.rs#L336-L362)
`Stepped` 分支构造 `SteppedEngineBridge` 只带 `scheduler.handle`——不需要 watch，单元格就在 handle 里。

`/metrics` 的生命线：

[pegainfer-frontend/src/vllm/mod.rs:480-483](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/mod.rs#L480-L483)
`disable_log_stats: false`——注释直说：上游把 scheduler-stats 与请求事件两个 recorder 绑在这个开关上，**桥的 stats 就是 `/metrics` 导出的东西**；周期性 stats 日志行也随它而来。若这里是 `true`，整条本讲的可观测链路在上游处熄灯。

echo 引擎的最小 `metrics()`：

[pegainfer-frontend/examples/echo-server.rs:160-166](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/examples/echo-server.rs#L160-L166)
echo 引擎只报 `num_running_reqs` / `num_waiting_reqs`，KV 与两组计数器全是默认值（`kv_total_blocks = 0`、两个 `Option` 为 `None`）——所以综合实践里 echo 的 `/metrics` **不会有**前缀缓存数字，这是 `Option 即能力`在可观测面上的直接体现。

#### 4.3.4 代码实践

**实践目标**：用 stepped 桥的单测复现"metrics 变化 → stats-only 批"这条路径。

**操作步骤**：

1. 运行：
   ```bash
   cargo test --release -p pegainfer-frontend --lib scheduled_and_metrics_only_steps -- --nocapture
   ```
2. 对照 [stepped.rs:753-794](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/bridge/stepped.rs#L753-L794) 读测试体：先 `handle.submit` 造一个流，再循环两轮——每轮 `backend.metrics.publish(&SchedulerMetrics { num_running_reqs: running, .. })` 后 `dispatch_step`（第二轮 `updates` 为空），断言每轮各收到一条 `outputs` 为空、`scheduler_stats.num_running_reqs` 等于刚发布值的批。
3. 注意最后一行 `assert!(rx.try_recv().is_err())`：没有再 publish、没有再 dispatch，通道上就没有多余消息。

**需要观察的现象**：测试通过；两轮循环各恰好一条 stats-only 批，第二轮的空更新步（模拟唤醒消息）也产出了批。

**预期结果**：单测通过，验证"发批时现读单元格 + 空步也盖章"两件事。（运行输出待本地验证。）

#### 4.3.5 小练习与答案

**练习 1**：`StatsTracker::stats` 为什么每次调用都推进基线，而不是等消息真正发出后再推进？

**答案**：基线跟着"已折算进某条 `SchedulerStats` 的最后一份快照"走。若等发送成功再推进，发送失败后下一次差分会把同一区间再算一遍（双计）；调用即推进的语义下，调用者放弃发送"丢掉的只是本区间的 delta"（桥侧注释原文），下一批从新基线差分，账目仍单调一致。

**练习 2**：引擎重启后（计数器清零），`prefix_cache_delta` 如何避免把 `/metrics` 上的计数器拉回负数？

**答案**：[bridge.rs:653-662](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/bridge.rs#L653-L662) 检查五个计数器中任何一个低于基线即判 `restarted`，把基线重置为 `PrefixCacheCounters::default()`，本区间从 0 差分——delta 永不为负，新引擎的新计数也不会丢。上游 Prometheus 计数器本来就只增，负 delta 既表达不了也只会被丢弃。

**练习 3**：`kv_total_blocks == 0` 时 `kv_cache_usage` 报 0.0，会不会误导"缓存空载"？

**答案**：会有歧义，但这是有意的保守选择：0 总量只出现在"引擎不报 KV 容量"或"尚未初始化"时，此时任何分数都没有意义；报 0.0 至少让依赖该分数的消费者（DP 均衡、KV 路由）不把它当满载。真正区分两种含义是上游仪表读者结合其他信号的事。

## 5. 综合实践

把本讲链路在无 GPU 环境下整条跑一遍，并写出一份数据流追踪笔记。

**任务 A：起 echo-server 并抓 `/metrics`**

1. 从工作区任意目录运行（模型是仓库内固定 fixture，无需权重）：
   ```bash
   cargo run -p pegainfer-frontend --example echo-server
   ```
2. 另开终端发一个生成请求（[echo-server.rs:18-20](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/examples/echo-server.rs#L18-L20) 的 curl，默认端口 8000）：
   ```bash
   curl http://127.0.0.1:8000/v1/completions \
     -H 'content-type: application/json' \
     -d '{"model": "echo", "prompt": "hello, pegainfer", "max_tokens": 64}'
   ```
3. 抓取指标端点：
   ```bash
   curl http://127.0.0.1:8000/metrics
   ```
4. 在输出里定位调度器相关条目——代码注释点名的名字是 `scheduler_running`、`scheduler_waiting`、`kv_cache_usage`（上游 recorder 的导出名）。**预期观察**：请求前抓一次、请求中/后各抓一次，`scheduler_*` 仪表应有差异；而**前缀缓存与 spec-decode 的计数器不会出现**——echo 引擎的 `metrics()` 两个 `Option` 都是 `None`（[echo-server.rs:160-166](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/examples/echo-server.rs#L160-L166)），桥侧 `Snapshot.prefix_cache` 为 `None` 时 `StatsTracker` 既不差分也不上报，还会把基线重置（[bridge.rs:711-714](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/bridge.rs#L711-L714)）。若在 `/metrics` 里找不到任何 `scheduler` 字样，依据本讲代码说明原因：stepped 桥的 stats 从 `SchedulerHandle::metrics` 现读、经 zmq 进上游 recorder，导出名与是否登记由上游 vllm-server 决定，`disable_log_stats: false`（[mod.rs:480-483](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/mod.rs#L480-L483)）是这条链的开关。（具体导出名与输出形态待本地验证。）

**任务 B：写出 `prefix_cache.hits` 的完整传播路径**

以带前缀缓存的 qwen3 引擎（stepped）为例，把下面每一站抄进笔记并给出对应源码链接：

| 站 | 代码位置 | 发生什么 |
|----|----------|----------|
| 1 | 模型 crate 调度器 | 每次 prefix 查询/命中累计进 `PrefixCacheCounters`（token 数） |
| 2 | `Scheduler::metrics` → [driver.rs:81-84](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/driver.rs#L81-L84) | 每迭代取快照并 `publish` |
| 3 | [wiring.rs:51-65](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/wiring.rs#L51-L65) | 写入共享单元格，值变则 `changed = true` |
| 4 | [ledger.rs:343-351](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/ledger.rs#L343-L351) | 空更新 + 变化 → 发空 `StepOutputs` 唤醒消息 |
| 5 | [stepped.rs:216-220](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/bridge/stepped.rs#L216-L220)、[stepped.rs:261](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/bridge/stepped.rs#L261) | 发批时现读单元格，`StatsTracker.stats` 差分后盖进批的 `scheduler_stats` |
| 6 | [bridge.rs:649-690](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/vllm/bridge.rs#L649-L690) | `hits − 基线` 拆成本地（`vllm:prefix_cache_*`，`source="device"`）与 connector（`vllm:external_prefix_cache_*`，`host`/`external_unspecified`）两侧 |
| 7 | zmq Push socket → 上游 vllm-server | recorder 把 delta 累加进 Prometheus 注册表 → `/metrics` |

**任务 C：摘录"必须累计"的注释依据**

从 [metrics.rs:10-19](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/metrics.rs#L10-L19) 与 [metrics.rs:80-84](https://github.com/openinfer-project/openinfer/blob/a474b264537268022a9c06766f65c90ffd0be6c1/pegainfer-frontend/src/engine/metrics.rs#L80-L84) 抄下两段原文，用一句话复述论证（"读者只保留最新值 ⇒ 错过一步即永久丢 delta ⇒ 只能累计、由有基线者差分"），并指出差分发生在哪一层（桥的 `StatsTracker`）。

## 6. 本讲小结

- `SchedulerMetrics` 是调度器每迭代发布的自画像：4 个占用仪表（`kv_used/total_blocks`、`running/waiting`）加两组 `Option` 累计计数器（spec-decode、prefix-cache），`None` 即"引擎无此能力"，可观测面上同样成立。
- 计数器必须累计：读者只看得到最新快照（共享单元或 watch 都只留最新值），错过一步的读者会永久丢 per-step delta，读累计值则永远正确；差分由持有连续基线的桥侧 `StatsTracker` 完成——"transport carries totals, the wire carries deltas"。
- 两代发布通道：stepped 写 `Arc<Mutex<_>>` 共享单元（`Mutex` 防撕裂快照），变化信号经 `commit_step` 的空更新 `StepOutputs` 搭步流便车；legacy 走 `watch` 通道，`changed()` 负载下每步醒一次、空闲安静，且 watch 向量兼任 DP 拓扑声明。
- 桥侧两条发布路径：legacy 常驻 `publish_scheduler_stats` 任务（先现发一份再按变化发 stats-only 批）；stepped 不设任务——driver 忙轮询会让每次自旋变消息——而是连接后首发一条、之后把 stats 盖在既有输出批上，delta 基线跟"最后盖章的批"走。
- `prefix_cache_delta` 带重启检测（累计值倒退即从零差分，永不出负 delta），并把命中拆成本地 `source="device"` 与 connector `host`/`external_unspecified` 两族计数器；spec-decode 零草稿区间整体丢弃防除零。
- `/metrics` 的最终形状由上游 vllm-server recorder 决定，pegainfer 侧的生命线是 `disable_log_stats: false`——注释明言"桥的 stats 就是 `/metrics` 导出的东西"。

## 7. 下一步学习建议

- 下一讲进入单元五：先读 u5-l1（`KvPrefix` 的 RAII hold、`RequestEnvelope` drop 炸弹与 `DeferredFinish` 迟到终态），本讲的 `host_hits`/`external_unspecified_hits` 层级拆分正好在那一讲找到 KV 侧的来源。
- 若想看累计计数器的真实生产者，建议 grep 模型 crate（如 `pegainfer-qwen3`）里对 `PrefixCacheCounters`/`SpecDecodeCounters` 字段的累加点，对照本讲第 5 节的传播路径表补全第 1 站的源码链接。
- 若对"stats-only 批在 zmq 线上如何被上游消费"感兴趣，可顺着 `vllm_engine_core_client::protocol::stats` 的类型定义（bridge.rs L38-L43 的导入）进入上游 crate 阅读 recorder 的登记逻辑。
- 复习建议：把 u3-l5 的 drive 四拍、u3-l6 的 `scheduler_pair` 通道选型与本讲 4.2 对照重读一次，"metrics 是共享单元、变化搭步流便车"这个设计是三讲的交汇点。
