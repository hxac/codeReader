# 在线引用分发与流式队列

## 1. 本讲目标

本讲聚焦 SpecForge **在线 disaggregated** 训练里最精巧的一环：当 producer（外部 SGLang）在 A 节点源源不断地捕获特征、consumer（trainer）在 B 节点一边收一边训练时，两者如何用**纯元数据**协同，既不丢样本、不重复训练，又能让数据并行（DP）的每个 rank 严格对齐到 optimizer 边界。

学完后你应该能够：

- 说清 `RefDistributor` 为什么是「整个 run 唯一的记账权威」，以及它如何把 producer 的引用流去重后按 **quantum 窗口**轮询分发到每个 rank 的私有 inbox。
- 解释 **quantum 握手**（quantum = `dp_size × batch_size × accumulation_steps`）如何同时约束 producer 的 in-flight 水位与 consumer 的分发粒度，杜绝死锁与「半步」optimizer。
- 读懂 `StreamingRefChannel`（跨进程追加写文件 + sidecar 计数器）→ `InboxChannel`（毒丸失败）→ `StreamingRefQueue`（阻塞式 `get`/前缀 `ack`）这条适配链。
- 掌握 `DPAckController` 如何把「每个 rank 在 optimizer 边界 ack 一次」变成一次 DP **collective**，由 rank 0 单一权威写一笔 SQLite durable 事务，再做第二轮 collective 汇集清理错误。
- 推算给定 `dp/batch/accum` 下的 quantum、每 rank 每步分到的 ref 数，并判断 EOF 残留不足一个窗口时会怎样收尾。

本讲承接 u7-l1（运行时四条路径与四平面分工）、u7-l2（控制面 `DataFlowController` 与元数据账本）、u7-l3（数据面 `FeatureStore` 与 `FeatureDataLoader` 桥梁），把这三者拼成完整的「在线 consumer 数据通路」。

## 2. 前置知识

阅读本讲前，建议你已经建立以下直觉（否则先看对应讲义）：

- **控制面只传元数据、数据面才传张量**（u5-l4、u7-l1）。`SampleRef` 是一个纯元数据指针（`feature_store_uri` + `feature_keys`），本身不含任何张量；真正的特征张量始终待在 `FeatureStore`（在线路径是 Mooncake）里。
- **quantum 的定义**（u7-l1）：一次完整 optimizer 步所需的全部 ref 数，等于 `dp_size × batch_size × accumulation_steps`。它是 producer/consumer 窗口握手的计量单位。
- **optimizer 边界是单一权威信号**（u6-l3、u6-l4）：梯度累积下，只有落在累积边界的那一步 `optimizer_stepped=True` 才真正 `optimizer.step()`；durable ack、检查点、评测全部钉在这个边界上。
- **DP（数据并行）**：每个 rank 训练互不相交的数据分片，但 FSDP 的 backward/step 是一次跨卡 collective，要求所有 rank 同步进、同步出——这就是「lockstep（齐步走）」约束的来源。
- **跨进程文件 sidecar**：在一个共享目录上用「主文件 + 若干 `.后缀` 小文件」传递状态（EOF、失败、计数器），靠 `os.replace` 做原子发布。这是 `StreamingRefChannel` 的实现基础。

一个贯穿全讲的比喻：在线 disaggregated 训练像一条**传送带 + 流水线**。producer 把每个样本的「提货单」（`SampleRef`）一张张追加写到一块共享白板上；consumer 端 rank 0 是唯一的**调度员**，从白板上收提货单、盖章去重、攒够「一整批齐步走」的量（一个 quantum）后，才把它们按劳分配到每个工位（rank 的 inbox）；每个工位干完一批并真正提交了梯度（optimizer 边界）后，才回头在账本上勾销这批提货单，调度员再把「已勾销」的总量回写给白板，producer 据此决定要不要继续往传送带上放货（背压）。

## 3. 本讲源码地图

本讲涉及的关键文件及其职责：

| 文件 | 职责 | 本讲角色 |
| --- | --- | --- |
| `specforge/runtime/data_plane/ref_distributor.py` | `RefDistributor`（中心化分发器）与 `InboxChannel`（带毒丸的 inbox 读视图） | **核心**：去重、quantum 窗口分发、背压回写、EOF 收尾 |
| `specforge/runtime/data_plane/streaming_ref_channel.py` | `StreamingRefChannel`（跨进程追加写引用流）与 `StreamingRefQueue`（适配 loader 的阻塞队列） | **核心**：通道原语、quantum sidecar、背压计数器、前缀 ack |
| `specforge/runtime/data_plane/sample_ref_queue.py` | `SampleRefQueue`（进程内元数据暂存队列） | 辅助：distributor 内部暂存已 commit 的 ref |
| `specforge/runtime/control_plane/dp_ack.py` | `DPAckController`（DP 感知的 durable ack collective） | **核心**：optimizer 边界的两段式分布式 ack |
| `specforge/runtime/control_plane/controller.py` | `DataFlowController`（元数据调度器） | 辅助：`commit_samples` 去重、`ack_train_refs` 基类 |
| `specforge/runtime/control_plane/flow_control.py` | `ProducerFlowControl`（producer 迟滞背压） | 辅助：producer 侧水位判定 |
| `specforge/runtime/ARCHITECTURE.md` | 运行时架构与在线流图 | 参考与流程图 |
| `specforge/training/disaggregated.py` / `specforge/launch.py` | 在线 producer/consumer 装配 | 辅助：quantum 发布、水位校验、装配接线 |

调用关系一览（在线 consumer 标准通路）：

```text
producer 进程:  RolloutWorker → StreamingRefChannel.publish ──(共享控制文件)──┐
                                                                            │
consumer rank0: RefDistributor.poll ←────────────────────────────────────────┘
                  │ commit_samples(去重→SQLite)
                  ↓
              SampleRefQueue (进程内暂存)
                  │ 攒满一个 quantum 窗口 → 轮询分发
                  ↓
        InboxChannel(rank0) … InboxChannel(rankN)   ← 每个 rank 一个私有 inbox 文件
                  │ StreamingRefQueue.get(阻塞)
                  ↓
        FeatureDataLoader → TrainBatch → TrainerController(每个 rank)
                  │ optimizer 边界: ack_fn
                  ↓
        DPAckController.ack_train_refs  (DP collective: gather→rank0写SQLite→广播→清理)
                  │ defer_queue_ack: StreamingRefQueue.ack_ids(前缀)
                  ↓
        RefDistributor._forward_consumed → source.mark_consumed  (回写 producer 背压)
```

## 4. 核心概念与源码讲解

本讲拆成三个最小模块：**4.1 RefDistributor**（分发与窗口）、**4.2 StreamingRefChannel / InboxChannel / StreamingRefQueue**（通道与队列）、**4.3 DPAckController**（分布式 durable ack）。量子握手与 EOF 收尾作为贯穿前两个模块的重点单独点出。

### 4.1 RefDistributor：单一权威的中心化分发器

#### 4.1.1 概念说明

在线 disaggregated 训练里，producer 在远端节点持续捕获特征并发布 `SampleRef`。如果让 N 个 DP rank 各自去读 producer 的引用流，就会立刻冒出三个难题：

1. **谁去重？** producer 可能因重试而「至少一次」地重复发布同一个 sample；N 个 rank 各读各的，没法判断某条 ref 是不是已经被别的 rank 处理过。
2. **谁记账？** durable ack（哪些 sample 已被训练提交）必须只有**一个写者**，否则 N 个 rank 会把各自的 ack 集合交错写进同一份账本。
3. **怎么保证 lockstep？** DP 的 optimizer step 是一次跨卡 collective，要求每个 rank 在同一步拿到**数量完全相同**的 ref，否则有的 rank 会 hang 在 `all_reduce` 上等一个永远不会到的对端。

`RefDistributor` 的设计答案是：**整个 run 只有一个分发器，它住在 trainer 的 DP rank 0 上**。它是 producer 引用通道的**唯一读者**，也是消费记账的**唯一持有者**。每个 rank 只读自己私有的 inbox（一个普通的 consume-once `StreamingRefQueue`），不持有任何 channel 偏移、不做任何分片数学、不碰账本。源码头部的模块文档把这条设计意图说得非常直白：

> source channel -> commit (ONE ledger, dedup) -> optimizer-step windows -> per-rank inbox

参见 [specforge/runtime/data_plane/ref_distributor.py:9-42](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/ref_distributor.py#L9-L42)，这段注释同时点明了三个关键不变量（窗口必须整窗口提交、EOF 落在窗口边界、背压镜像 optimizer-durable 工作）。

#### 4.1.2 核心流程

`RefDistributor` 是一个后台线程（`run` → 周期性 `pump`），每个 `pump` 周期做三件事：

```text
pump():
  1. ingest : source.poll() 取新 ref
              → 跳过已释放的 skip 集
              → controller.commit_samples() 去重(写 SQLite 账本)
              → 重复发布: 同次幂等结算(mark_consumed)
  2. dispatch: 从 controller.sample_queue 攒 ref
              → 仅当「整窗口 quantum 已本地 commit」才开启新 optimizer 窗口
              → 按 dispatch_round_quantum 轮询分发到各 rank inbox
              → 窗口凑满 quantum 即完成本轮窗口
  3. counter : 汇总各 inbox 的 consumed_remote() 增量 → source.mark_consumed()
              （把 optimizer-durable 的 ack 镜像成 producer 背压）
  若 source 已 closed 且排空 → _finish()：处理残留、关闭 inbox
```

两个关键的「量」在构造时就算好了（见 [specforge/runtime/data_plane/ref_distributor.py:135-136](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/ref_distributor.py#L135-L136)）：

- `dispatch_quantum = dp_size * refs_per_rank_step`：一个完整 optimizer 窗口的总 ref 数。其中 `refs_per_rank_step = batch_size * accumulation_steps`（每 rank 每步需要的 ref）。
- `dispatch_round_quantum = dp_size * refs_per_rank_batch`：一轮 DP micro-batch 的总 ref 数。其中 `refs_per_rank_batch = batch_size`。

所以一个 quantum 窗口会被拆成 `accumulation_steps` 轮分发，每轮给每个 rank 恰好 `batch_size` 条 ref——既保证 lockstep（每 rank 每轮量相同），又让每 rank 一个 optimizer 步累计拿到 `batch_size × accumulation_steps` 条。

> **注意术语区分**：`SampleRefQueue`（[sample_ref_queue.py](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/sample_ref_queue.py)）是 **distributor 进程内部**用来暂存「已 commit、待分发」ref 的元数据队列；而每个 rank 读的 `StreamingRefQueue` 是**跨进程 inbox 文件**的阻塞式适配器。两者同名一半，但分属不同层级。

#### 4.1.3 源码精读

**(a) 构造：确定 quantum、重建临时 inbox、续接背压计数器**

[specforge/runtime/data_plane/ref_distributor.py:135-166](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/ref_distributor.py#L135-L166) 做了几件关键的事：

```python
self.dispatch_quantum = dp_size * refs_per_rank_step          # 一个 optimizer 窗口
self.dispatch_round_quantum = dp_size * refs_per_rank_batch   # 一轮 DP micro-batch
...
# Inboxes are EPHEMERAL (the ledger is the durable state): recreate them
# fresh so a restarted run cannot replay a previous attempt's dispatch.
os.makedirs(inbox_dir, exist_ok=True)
for rank in range(dp_size):
    base = self.inbox_path(inbox_dir, rank)
    for suffix in _INBOX_SUFFIXES:
        ...  # 删除遗留的 .closed/.failed/.consumed_count 等
self._inboxes = [StreamingRefChannel(self.inbox_path(inbox_dir, rank))
                 for rank in range(dp_size)]
# 续接 producer 可见的计数器，而不是重启后回卷它
consumed = self.source.seed_consumed()
if len(self._skip) > consumed:
    self.source.mark_consumed(len(self._skip) - consumed)
```

要点：inbox 是**临时的**（durable 状态是 SQLite 账本，不是 inbox），每次启动都清空重建，防止「重启重放上一次的分发」；而 producer 的 `consumed` 计数器要**续接**而非回卷——因为崩溃可能发生在「SQLite 已提交 / feature 已 abort，但 rank 本地 inbox 还没 ack」的窄窗口里，这里用已释放前缀把计数器修补到正确位置（见注释 L159-165）。

**(b) pump 的窗口分发逻辑：只在整窗口已 commit 时才开窗**

这是整个分发器最核心、也最容易出错的一段，见 [specforge/runtime/data_plane/ref_distributor.py:251-281](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/ref_distributor.py#L251-L281)：

```python
while True:
    if self._window_dispatched == 0:
        # 仅当「整窗口 quantum 已本地 commit」时才开启新 optimizer 窗口
        if len(self._window) + queue.depth() < self.dispatch_quantum:
            break
    need = self.dispatch_round_quantum - len(self._window)
    if need:
        self._window.extend(queue.get(need, timeout_s=0.0))
    if len(self._window) < self.dispatch_round_quantum:
        break
    rank_batches: List[List[SampleRef]] = [[] for _ in range(self.dp_size)]
    for index, ref in enumerate(self._window):
        rank_batches[index % self.dp_size].append(ref)   # 轮询分配
    for inbox, refs in zip(self._inboxes, rank_batches):
        inbox.publish_batch(refs)
    self.stats["dispatched"] += self.dispatch_round_quantum
    self._window_dispatched += self.dispatch_round_quantum
    self._window = []
    if self._window_dispatched == self.dispatch_quantum:
        self._window_dispatched = 0                       # 窗口完成
    progress = True
```

读懂这段的关键是注释里的那句「**a released round obligates every rank to a full accumulation window**」（L19-22）：一旦你把第一轮分发出去，你就**有义务**让每个 rank 走完整个累积窗口；而 durable ack 只在整窗口边界推进。所以开窗的前提条件 `_window_dispatched == 0` 时必须确认 `len(self._window) + queue.depth() >= dispatch_quantum`——也就是这一整个窗口的所有 ref 此刻都已在本地（已 commit）。这样即使下一刻 producer 立刻 EOF，这个窗口也必定能在本轮循环内凑满并完成，绝不会出现「开了一半窗、然后流结束了」的悬空状态。

**(c) 背压镜像：把 optimizer-durable 的 ack 转成 producer 背压**

[specforge/runtime/data_plane/ref_distributor.py:184-192](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/ref_distributor.py#L184-L192)：

```python
def _forward_consumed(self) -> bool:
    """Mirror optimizer-durable inbox acks onto source backpressure."""
    consumed = sum(inbox.consumed_remote() for inbox in self._inboxes)
    delta = consumed - self._inbox_consumed
    if delta <= 0:
        return False
    self._inbox_consumed = consumed
    self.source.mark_consumed(delta)
    return True
```

这是 producer 背压的真正来源。注意它读的是**各 rank inbox 的 consumed sidecar 之和**，而每个 rank 只在「durable ack 成功之后」才会推进自己的 inbox 计数器（见 4.3）。模块文档说得很清楚（L31-36）：「dispatch 本身不算消费」——分发出去的 ref 不立刻释放，必须等 optimizer 边界 durable ack 后才算消费，这正是「已 ack 样本」与「已提交梯度」严格对齐的体现。

**(d) 失败传播：毒丸而非静默挂死**

如果分发线程自己崩了，它**不会**把 inbox 正常 `close()`（那样会被 ranks 误读成正常 EOF、在残缺数据上「成功」结束），而是给每个 inbox 投一个 `.failed` 毒丸，见 [specforge/runtime/data_plane/ref_distributor.py:365-382](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/ref_distributor.py#L365-L382)。`InboxChannel` 在下次 `poll` 时撞见毒丸就立刻抛 `RuntimeError`，把「分发线程死了」从「无限挂死」变成「所有 rank 立即失败」。

#### 4.1.4 代码实践

**实践目标**：在源码里追踪一次 `pump` 的窗口判定，亲手验证「整窗口才开窗」这条不变量。

**操作步骤**：

1. 打开 [ref_distributor.py 的 pump 方法（L194-288）](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/ref_distributor.py#L194-L288)。
2. 假设 `dp_size=2, batch_size=2, accumulation_steps=2`，先在纸上算出 `dispatch_quantum` 和 `dispatch_round_quantum`。
3. 在 L264 的 `if len(self._window) + queue.depth() < self.dispatch_quantum:` 处暂停，思考：此刻若 `queue.depth()` 只有 5（小于你算出的 quantum），会发生什么？分支会 `break`，这一轮**不分发任何东西**，等下一次 `pump` 再 poll 新 ref 攒够。
4. 继续看 L272 的 `rank_batches[index % self.dp_size].append(ref)`，验证：一轮分发正好让 rank0 和 rank1 各拿到 `batch_size` 条 ref，二者数量相等（lockstep）。

**需要观察的现象**：只要 producer 还没攒够一个 quantum 的已 commit ref，consumer 这边就**一个 ref 都不会分发**——这就是冷启动注释（L26-29）提醒的：「rank 收到第一个 micro-batch 之前，必须等整个首个窗口捕获完毕」，所以 rank 侧的 idle 超时必须覆盖「整窗口捕获延迟」而不是单轮。

**预期结果**：你会清楚地看到「窗口完整性」是分发的前置条件，而不是「先分发、凑不齐再回滚」。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `RefDistributor` 只能有一个实例、且必须住在 rank 0？

**参考答案**：它是 producer 引用通道的**唯一读者**与 durable 记账的**唯一写者**。若存在多个实例，同一份 SQLite 账本会被并发交错写入，去重也会失效；而把它放在 rank 0，是因为 rank 0 同时是 DPAckController 的 authority（见 4.3），两者天然共用同一份权威账本。

**练习 2**：`_forward_consumed` 读的是「各 inbox consumed 之和」，而不是直接读「已分发的 ref 数」。这样做是为了保证什么不变量？

**参考答案**：为了保证 producer 的背压信号**只反映 optimizer-durable 的工作量**。分发（dispatch）本身不算消费——只有当某 rank 真正在 optimizer 边界 durable ack 之后，它的 inbox 计数器才会推进；分发器把这个推进量回写给 source，使 producer 的 `in_flight = published - consumed` 恰好等于「已发布但尚未被训练提交」的量，从而背压精确地卡在「未完成 optimizer 工作」上。

### 4.2 量子握手与流式通道：StreamingRefChannel / InboxChannel / StreamingRefQueue

#### 4.2.1 概念说明

producer 和 consumer 是两个独立进程，唯一共享的是一块**控制挂载点**（一个目录）。`StreamingRefChannel` 就是建立在这块共享目录上的「跨进程、追加写、纯元数据」的 `SampleRef` 流。它的设计哲学在模块文档里讲得很清楚（[streaming_ref_channel.py:9-36](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/streaming_ref_channel.py#L9-L36)）：

- **无张量**：通道里只跑 `SampleRef` 元数据（publish 时 `assert_no_tensors`），张量走 `FeatureStore`（Mooncake），所以**不需要共享数据盘**，只需要这块小控制文件。
- **追加写 JSONL**：`publish()` 每条 ref 占一行并 `fsync`；`poll()` 从上次偏移量 tail-read 完整行，缓存不完整的尾行，保证读到的绝不会是「写了一半的记录」。
- **consume-once 友好**：reader 用 `mark_consumed` 把「已消费计数」写进一个 sidecar，writer 回读它来施加背压（`in_flight_remote`），全程无共享进程内状态。
- **显式结局**：`close()` 只为成功 producer 投 EOF 哨兵；`fail()` 单独发布 producer 异常，远端 trainer 绝不会把「截断的 rollout」误当成正常输入结束。

围绕这个通道有三个角色：**producer** 调 `publish`/`close`、读 `in_flight_remote` 做背压；**distributor（rank0）** 调 `poll`、读 sidecar；**每个 rank** 把自己的 inbox（一个 `StreamingRefChannel`）用 `StreamingRefQueue` 包成 loader 能消费的阻塞队列。

#### 4.2.2 核心流程：量子握手

「quantum 握手」是 producer 与 consumer 在捕获**开始前**的一次握手，确保双方对「一个 optimizer 窗口有多大」达成一致，避免死锁。流程如下（参见 [ARCHITECTURE.md:97-121](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/ARCHITECTURE.md#L97-L121)）：

```text
consumer rank0:  channel.publish_consumer_quantum(dp*batch*accum)   # 捕获前发布 quantum
                          │ 写入 .consumer_quantum sidecar
                          ↓
producer:        等待 .consumer_quantum 出现
                 校验 in_flight_high_watermark >= quantum   (否则 ValueError)
                 校验 resolved_low_watermark   >= quantum   (否则 ValueError)
                 通过后才进入捕获循环
```

为什么 producer 必须等这个 quantum？因为 consumer 只派发**完整 quantum 窗口**（4.1）。如果 producer 的高水位 < quantum，它可能在 producer 这侧卡住、永远攒不出第一个窗口能消费的量，于是 consumer 永远收不到第一个 batch——两边互相等待，经典死锁。所以 producer 要求自己的 in-flight 容量**至少能装下一个完整窗口**。

#### 4.2.3 源码精读

**(a) 量子 sidecar 的发布与读取**

consumer rank 0 在装配 `RefDistributor` 之后立刻发布 quantum，见 [specforge/training/disaggregated.py 的 build_disagg_online_consumer 调用处（launch.py 内，L1492-1495）](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L1492-L1495)：

```python
channel.publish_consumer_quantum(
    dp_size * batch_size * accumulation_steps,
    allow_existing=resume_from is not None,
)
```

`publish_consumer_quantum` 用 `O_CREAT | O_EXCL` 保证「每次在线尝试只写一次」（[streaming_ref_channel.py:229-263](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/streaming_ref_channel.py#L229-L263)）；`allow_existing=True` 仅用于 consumer-only 续训场景，且要求值与现存完全一致，否则报「quantum 在 resume 之间被改过」的错。

producer 侧的等待与水位校验在 [launch.py 的 producer drive（L956-998）](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L956-L998)：

```python
while True:
    consumer_quantum = channel.consumer_quantum()
    if consumer_quantum is not None:
        break
    ...  # 可选 peer_wait_timeout，超时抛 TimeoutError
if in_flight_high_watermark < consumer_quantum:
    raise ValueError("producer in-flight high watermark ... is smaller than the "
                     "consumer's global optimizer-step quantum ...")
resolved_low_watermark = flow_control.limits.resolved_low_watermark_refs
if resolved_low_watermark < consumer_quantum:
    raise ValueError("producer in-flight low watermark ... is smaller than ...")
```

注释（L986-989）解释了为什么**低水位**也必须 ≥ quantum：consumer 只派发整窗口，一个被背压暂停的 producer 必须能在「consumer 还差最多一个完整窗口」时被**唤醒**；若低水位低于 quantum，producer 会停在低水位之下、而 consumer 还在等它凑满一个窗口——又死锁。

**(b) 追加写的归属语义与原子计数器**

`publish` 的关键细节是「归属权在完整记录写入内核后即转移」，见 [streaming_ref_channel.py:125-146](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/streaming_ref_channel.py#L125-L146)：

```python
self._published += 1          # 先计数(进度可观测)
os.fsync(fd)                  # 再 fsync(可能晚报失败)
```

注释（L139-143）点明：fsync 可能在记录已可见之后才报持久化失败，所以「进度」必须在 fsync 之前就可观测——这是 `RefPublishTransaction`（L58-98）能在 `publish` 抛错时精确区分「已转移前缀 / 未触及后缀」的基础。

`mark_consumed` 用「线程唯一的 tmp 名 + `os.replace`」做原子计数发布，见 [streaming_ref_channel.py:343-355](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/streaming_ref_channel.py#L343-L355)：因为 `mark_consumed` 会从训练线程（batch ack）和预取 worker（失败结算）两处并发调用，必须保证并发写者不会互相 `replace` 掉对方的半成品计数文件。producer 读回这个计数算背压：`in_flight_remote = published - consumed_remote`（L306-308）。

**(c) InboxChannel：毒丸失败**

`InboxChannel` 是 `StreamingRefChannel` 的子类，只重写 `is_closed` 与 `failure`：每次 `poll` 前先查 `.failed` 哨兵，存在就抛 `RuntimeError`（[ref_distributor.py:69-88](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/ref_distributor.py#L69-L88)）。由于 `StreamingRefQueue.get` 每个 poll 周期都会查 `is_closed()`，毒丸就把「分发线程死亡」从「无限挂死」变成「每个 rank 下次 poll 立即报错」，与干净的 `.closed` EOF 严格区分。

**(d) StreamingRefQueue：阻塞 get 与前缀 ack**

每个 rank 用 `StreamingRefQueue` 把自己的 inbox 适配成 `FeatureDataLoader` 在 queue 模式下消费的 `get/ack/fail` 协议，见 [streaming_ref_channel.py:407-568](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/streaming_ref_channel.py#L407-L568)。两个要点：

- `get(n)` **阻塞**直到攒够 n 条 ref 或通道 closed-and-drained（L452-454），所以 trainer 会**流式消费整个在线 run**，只在 producer 关闭后才看到空 batch 而退出循环。
- `ack_ids` 强制「只能 ack 已租出的**前缀**」（L533-550）：通道只暴露一个计数、而非按 id ack，所以接受任意子集会让重启时的账目歧义；训练按租约顺序消费 ref，这里把这个不变量大声校验出来。

```python
def ack_ids(self, sample_ids: List[str]) -> None:
    ...
    with self._inflight_lock:
        actual = [ref.sample_id for ref in self._inflight[: len(sample_ids)]]
        if actual != list(sample_ids):
            raise RuntimeError("stream acknowledgement is not the leased prefix: ...")
        del self._inflight[: len(sample_ids)]
    self.channel.mark_consumed(len(sample_ids))
```

注意 `ack_ids` 推进的是 **inbox 通道的 consumed 计数器**（producer 背压的间接来源），它由 trainer 的 `ack_fn` 在 durable ack 成功**之后**调用（见 4.3 的 `defer_queue_ack`）。

#### 4.2.4 代码实践

**实践目标**：理解 quantum 握手如何防止死锁。

**操作步骤**：

1. 读 [launch.py producer drive 的等待循环（L956-998）](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L956-L998)。
2. 设想一个错误配置：`dp=4, batch=2, accum=2`（quantum=16），但用户把 `DISAGG_IN_FLIGHT_HIGH_WATERMARK` 设成了 `8`。
3. 追踪：consumer rank0 发布 quantum=16 → producer 读到 → `in_flight_high_watermark(8) < consumer_quantum(16)` → 抛 `ValueError` 并 `channel.fail(...)`。

**需要观察的现象**：producer 在捕获**还没开始**时就 fail-fast 退出，并把失败写进 `.failed` 哨兵；consumer 侧的 `RefDistributor.pump` 在 `source.failure()` 非空时立刻抛 `RuntimeError`（[ref_distributor.py:202-204](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/ref_distributor.py#L202-L204)）。

**预期结果**：你会看到一个「配置错误 → 早期显式失败」的完整链路，而不是「双方各自运行几分钟后神秘挂死」。

#### 4.2.5 小练习与答案

**练习 1**：为什么 `mark_consumed` 用「线程唯一 tmp 名 + `os.replace`」而不是直接覆盖写计数文件？

**参考答案**：因为 `mark_consumed` 会从训练线程和预取 worker 并发调用。若两个线程直接覆盖写同一个文件，一个线程可能 `replace` 掉另一个线程刚写了一半的计数文件，导致计数丢失或读到脏值。线程唯一的 tmp 名保证每个写者写自己的临时文件，`os.replace` 是原子的，最终落盘的总是某个完整的计数。

**练习 2**：`InboxChannel` 为什么要区分 `.failed`（毒丸）和 `.closed`（正常 EOF）？如果把分发线程的崩溃也实现成「关闭 inbox」，会发生什么？

**参考答案**：正常 `close` 会被 ranks 解读为「流正常结束、在已派发的对齐前缀上成功完成」。若分发线程崩溃也走 `close`，ranks 就会在**残缺数据**上「成功」结束训练，错误被静默吞掉。毒丸 `.failed` 让每个 rank 在下次 poll 立即抛错，把崩溃变成一个响亮、即时的全 rank 失败。

### 4.3 DPAckController：optimizer 边界的两段式分布式 durable ack

#### 4.3.1 概念说明

u7-l2 已经讲过 `DataFlowController.ack_train_refs`：它在 optimizer 边界记录 durable ack 事务（`{acked, global_step, optimizer_durable}` 标记）并释放队列租约。但在 **DP** 在线 consumer 里，每个 rank 训练的是互不相交的分片，各自手里只有自己那批 sample_id。如果每个 rank 各自往同一份账本写自己的 ack 集合，N 个 rank 的部分集合就会交错成一份错误账本。

`DPAckController` 继承自 `DataFlowController`，把 `ack_train_refs` 改造成一次 **DP collective**，目标是「**gather 每个 rank 的 sample_id，只记录一次**」。它依赖 `RefDistributor` 强制的 lockstep 不变量（每 rank 每步拿到等量 ref）：任何一个在边界上「跳过」的 rank，都会让 `all_gather` 卡死——所以分发器的 lockstep 既是性能要求，也是这里正确性的前提（见 [dp_ack.py 模块文档 L20-24](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/control_plane/dp_ack.py#L20-L24)）。

#### 4.3.2 核心流程：两段式 collective

`ack_train_refs` 在每个 rank 的每个 optimizer 边界被 `TrainerController` 以 lockstep 调用一次，内部是**两段 collective**：

```text
阶段1 (authority 提交 + 广播):
  union = all_gather_object(各 rank 的 local_ids) → 去重并集   (collective #1)
  if is_authority (rank0):
      super().ack_train_refs(union, optimizer_durable=True)    # 唯一一次 SQLite 写
      捕获 commit_error
  commit_error = broadcast_object_list(commit_error, src=0)    # 广播结果
  if commit_error: raise                                       # 任一 rank 提交失败 → 全失败

阶段2 (本地清理 + 汇集错误):
  if optimizer_durable:
      每个 rank 在自己的 feature_store 上 abort(本地 local_ids)  # 删自己的分片特征
      收集本地 cleanup_error
  cleanup_error = all_gather_object(各 rank cleanup_error)      # collective #2
  if cleanup_error: raise                                       # 清理失败也要让所有 rank 知道
```

为什么清理是「第二次 collective」而不是顺手并进第一段？因为每个 rank 拥有**独立的 Mooncake 客户端**，一个非 authority rank 可能在 rank0 成功的同时失败（它只物化过自己的 DP 分片）；所有 rank 必须在**任何 rank 推进自己的 inbox 计数器之前**都观察到这个失败，否则背压与账本会错位（见 [dp_ack.py:74-98](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/control_plane/dp_ack.py#L74-L98) 的注释）。

#### 4.3.3 源码精读

**(a) gather 去重并集**

[gather_id_union](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/control_plane/dp_ack.py#L33-L58)（L33-58）用 `all_gather_object` 收集每个 rank 的 id 列表，按 rank 顺序去重并集（保留首次出现）。注意它在 `torch.distributed` 不可用/未初始化/`world==1` 时退化为恒等——所以单 rank 在线 consumer 也能安全复用同一条路径。

**(b) ack_train_refs 的两段式实现**

[specforge/runtime/control_plane/dp_ack.py:140-185](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/control_plane/dp_ack.py#L140-L185)：

```python
def ack_train_refs(self, trainer_id, sample_ids, *, global_step=None,
                   optimizer_durable=False) -> None:
    local_ids = list(dict.fromkeys(sample_ids))          # 本地去重
    union = self._gather(local_ids)                      # collective #1
    commit_error = None
    if self.is_authority:                                # 仅 rank0
        try:
            super().ack_train_refs(trainer_id, union,
                                   global_step=global_step,
                                   optimizer_durable=optimizer_durable)  # 唯一 SQLite 写
        except BaseException as exc:
            commit_error = f"{type(exc).__name__}: {exc}"
    commit_error = self._sync_error(commit_error)        # 广播结果
    if commit_error is not None:
        raise RuntimeError(f"durable DP acknowledgement failed: {commit_error}")

    cleanup_error = None
    if optimizer_durable and self.feature_store is not None:
        failures = []
        for sample_id in local_ids:                      # 每个 rank 只删自己的分片
            try:
                self.feature_store.abort(sample_id, reason="optimizer-boundary-durable-ack")
            except BaseException as exc:
                failures.append(...)
        if failures:
            cleanup_error = ", ".join(failures)
    cleanup_error = self._sync_cleanup_error(cleanup_error)   # collective #2
    if cleanup_error is not None:
        raise RuntimeError("durable DP acknowledgement committed, but "
                           "rank-local feature cleanup failed: ...")
```

注意几个不变量：authority 提交是**唯一**的 durable 写、物理删除必须在它成功之后（注释 L152-155）；删除只动 `local_ids`（自己的分片）而非 union，因为别的 rank 的分片自己可能从没物化过；任一阶段失败都会 `raise`，保证「要么全 rank 一致提交、要么全 rank 一致失败」。

**(c) 与 trainer 的接线：defer_queue_ack**

`ack_train_refs` 由 `TrainerController` 在 `optimizer_stepped` 边界调用（[training/controller.py:585-592](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/controller.py#L585-L592)）。它通过 `ack_fn` 注入，而 `ack_fn` 在 durable ack 成功**之后**才推进 inbox 的前缀 ack：

```python
# specforge/training/trainer.py:434-447
def ack_fn(ids, step):
    controller.ack_train_refs(           # DPAckController 两段 collective
        trainer_id, ids, global_step=step, optimizer_durable=True
    )
    if defer_queue_ack:                  # 在线 consumer 为 True
        ack_ids = getattr(ref_source["queue"], "ack_ids", None)  # inbox 的前缀 ack
        ...
        ack_ids(ids)
```

这就是「在线借 `defer_ack_until_durable` 把 durable ack 让给 `DPAckController`」的真正落点（[trainer.py:121-127](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/trainer.py#L121-L127)）：loader 自己**不**在取出 batch 时立刻 ack inbox，而是把 ack 推迟到 `ack_fn`、且排在 durable collective 之后。于是 inbox 的 consumed 计数（→ producer 背压）只反映「已 optimizer-durable」的工作量。

#### 4.3.4 代码实践

**实践目标**：确认「optimizer 边界才 ack」「rank0 单一写者」这两条不变量在源码里的位置。

**操作步骤**：

1. 在 [dp_ack.py:140-185](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/control_plane/dp_ack.py#L140-L185) 标出：`self._gather(local_ids)` 是哪一次 collective、`super().ack_train_refs` 在哪个 `if` 分支里、`self._sync_cleanup_error` 又是哪一次。
2. 在 [training/controller.py:585-592](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/training/controller.py#L585-L592) 确认：`ack_fn` 只在 `result.optimizer_stepped` 为真（即 `global_step` 自增）之后调用，非边界 micro-batch 走 `continue` 不 ack。

**需要观察的现象**：梯度累积下，前 N-1 个 micro-batch 完全不触发任何 ack 或 collective；只有第 N 个（边界）micro-batch 才一次性 ack 整个窗口、做两次 collective、删一次特征。

**预期结果**：你会看到 durable ack 的频率 = optimizer step 频率，而不是 micro-batch 频率，这正是背压与账本对齐到 optimizer 边界的体现。待本地验证：若你有双卡环境，可在 `ack_train_refs` 首行临时加一条 `print(rank, len(local_ids))`，观察每个边界两个 rank 各打印一次、且 id 集合互不相交。

#### 4.3.5 小练习与答案

**练习 1**：为什么「物理删除特征」必须在 authority 的 SQLite 提交成功**之后**，而不是之前或同时？

**参考答案**：SQLite 的 ack id + optimizer marker 是唯一 durable、authority 拥有的事实。如果在提交前就删特征，一旦提交失败，这些特征已无法恢复，而账本又没记录它们被消费——重启时会重复训练或找不到张量。先提交、提交成功才删，保证「已删除的特征」必然对应「已 durable ack 的记录」，二者不会错位。

**练习 2**：`gather_id_union` 对 SP（序列并行）replicated 分片做了什么特殊处理？为什么需要？

**参考答案**：SP peer 会在同一序列上 replicated 出相同的 sample_id。`gather_id_union` 用 `seen` 集合「保留首次出现」做去重（L51-57），让这些重复 id 坍缩成一条，避免同一 sample 在 union 里被记多次。注意它仍按 rank 顺序保留首次出现，使结果确定。

### 4.4 EOF 残留：不足一个 quantum 的终态处理

#### 4.4.1 概念说明

producer 的流总会结束。若结尾恰好留下「不足一个 quantum」的零头 ref（比如 quantum=16，但最后只剩 5 条已 commit 的 ref），分发器**绝不会**把这 5 条派发出去——因为派发就意味着某个 rank 要走半个 optimizer 窗口，而 durable ack 只在整窗口推进，这会留下「没有任何 resume 能 ack」的悬空 ref。

SpecForge 选择 drop-last 式收尾：把这些零头标记为 terminal、必要时 adopt 进生命周期跟踪、abort 掉它们的特征对象、把 source 计数器结算掉，然后正常关闭所有 inbox。这与 `resolve_online_total_steps` 的 floor 语义一致——「一个不完整的全局 optimizer step 永远不会被派发」（见 [ARCHITECTURE.md:108-121](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/ARCHITECTURE.md#L108-L121)）。

#### 4.4.2 核心流程

```text
_finish():
  1. 断言 _window_dispatched == 0   (开窗必在本轮完成 → EOF 落在窗口边界)
  2. 收集残留: 本地 _window + 从 sample_queue 再掏空一轮
  3. if leftover:
        stats["dropped"] = len(leftover)
        queue.fail(leftover, retryable=False)          # terminal，不回队列
        for ref in leftover:
            feature_store.adopt(ref)   (若支持)
            feature_store.abort(ref.sample_id, reason=...)
        source.mark_consumed(len(leftover))            # 结算背压
        若清理出错 → raise(响亮失败)；否则仅 warning
  4. 关闭所有 inbox → finished = True
```

#### 4.4.3 源码精读

关键的终态断言与零头处理在 [ref_distributor.py:290-338](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/ref_distributor.py#L290-L338)：

```python
def _finish(self) -> None:
    if self._window_dispatched:
        raise AssertionError(
            "internal invariant violated: a dispatched optimizer window "
            "must complete within the pump cycle that opened it ...")
    leftover = list(self._window)
    self._window = []
    queue = self.controller.sample_queue
    while True:
        batch = queue.get(self.dispatch_quantum, timeout_s=0.0)
        if not batch:
            break
        leftover.extend(batch)
    if leftover:
        self.stats["dropped"] = len(leftover)
        reason = (f"end-of-stream leaves {len(leftover)} refs, fewer than one "
                  f"optimizer window (required global quantum={self.dispatch_quantum}: ...)")
        queue.fail(leftover, reason=reason, retryable=False)
        ...
        for ref in leftover:
            adopt = getattr(self.feature_store, "adopt", None)
            if callable(adopt):
                adopt(ref)
            self.feature_store.abort(ref.sample_id, reason=reason)
        ...
        self.source.mark_consumed(len(leftover))
        ...
        logger.warning("ref-distributor: %s", reason)
    for inbox in self._inboxes:
        inbox.close()
    self.finished = True
```

注意 `_finish` 开头的 `AssertionError`（L297-302）：它把「开窗必在开窗的同一 pump 内完成」这条不变量焊成硬校验——一旦违反（理论上不应发生），宁可大声崩掉也不让账目悄悄错乱。

还有一条重要副作用（见 [ARCHITECTURE.md:117-121](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/ARCHITECTURE.md#L117-L121)）：被 drop 的零头虽然在物理上删了特征，但在元数据账本里仍是「committed-but-unacknowledged」。所以一次「带零头且成功结束」的 attempt，其账本不能被后续 run 直接 resume——必须用全新账本，直到控制平面记录显式的 terminal-drop 状态。这也是为什么每次在线 attempt 都要求全新的 channel / store-id / run-id。

#### 4.4.4 代码实践

**实践目标**：验证 EOF 零头不会被派发，且会触发 `dropped` 统计。

**操作步骤**：

1. 读 [ref_distributor.py:290-338](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/ref_distributor.py#L290-L338)。
2. 构造一个心智模型：quantum=16，producer 总共只发布了 21 条已 commit 的 ref。前 16 条凑成 1 个完整窗口被派发；剩 5 条 < 16。
3. 追踪 `_finish`：5 条进 `leftover` → `queue.fail(retryable=False)` → 每条 `feature_store.abort` → `source.mark_consumed(5)` → `stats["dropped"]=5` → 关闭所有 inbox。

**需要观察的现象**：trainer 每个 rank 只会看到属于那 1 个完整窗口的 ref，正常结束；不会有任何 rank 收到「凑不齐一个 optimizer 步」的残缺 batch。

**预期结果**：日志里出现一条 `ref-distributor: end-of-stream leaves 5 refs, fewer than one optimizer window ...` 的 warning，且 `stats` 显示 `dropped=5`。待本地验证：可参考 `tests/test_runtime/test_ref_distributor.py` 中关于 sub-window leftover 的断言用例来对照行为。

#### 4.4.5 小练习与答案

**练习 1**：为什么不在 EOF 时「把零头也派发出去、让 rank 走半个 optimizer 步」？

**参考答案**：durable ack 只在整窗口边界推进。若派发了一个凑不满 quantum 的窗口，rank 会卡在「反向已完成、但累积没到边界、optimizer 没 step」的状态；流已结束、再没有更多 ref 来补齐，于是这批 ref 永远无法被 durable ack——没有任何 resume 能结算它们。drop-last 式收尾保证「每个被派发的 ref 都属于一个可被 durable ack 的完整窗口」。

**练习 2**：零头被 abort 之后，为什么元数据账本里它仍是「committed-but-unacknowledged」？这带来什么限制？

**参考答案**：`commit_samples` 在分发前就已把它写进 SQLite（它是「已 commit」），而 `_finish` 只做了物理特征删除（abort），并没有写一条 durable ack（因为它没被训练）。所以账本里它停留在「已提交、未 ack」状态。限制是：带这种零头的成功 attempt 不能被后续 run 直接 resume（否则 resume 会以为还有未 ack 的训练工作要做），必须用全新账本重启。

## 5. 综合实践

把本讲三个模块串起来，完成下面这个「纸上推演 + 源码核对」的综合任务。

**场景**：一个在线 disaggregated EAGLE3 训练，配置为 `dp_size=4, batch_size=2, accumulation_steps=2`，producer 用 2 个 SGLang server 并发捕获，默认 `DISAGG_IN_FLIGHT_HIGH_WATERMARK=256`。

**任务**：

1. **推算 quantum 与分发结构**。算出：
   - `refs_per_rank_step`、`refs_per_rank_batch`；
   - `dispatch_quantum`（一个 optimizer 窗口总数）与 `dispatch_round_quantum`（一轮 DP micro-batch 数）；
   - 一个 optimizer 窗口分几轮派发、每轮每个 rank 拿到几条 ref、一个 optimizer 步每个 rank 累计拿到几条 ref。
2. **判断水位是否合法**。说明 producer 的 in-flight 高水位与低水位各自必须满足什么下界，默认 256 是否合法，以及若不合法会怎样。
3. **推演 EOF 零头**。若 producer 总共产出了 100 条已 commit 的 ref，算出能凑成几个完整 optimizer 窗口、剩余几条零头，并描述这些零头在 `_finish` 里的终态（被 fail / abort / mark_consumed / dropped 统计 / 账本状态）。
4. **源码核对**。把你的推演结果分别对照 [ref_distributor.py:135-136](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/ref_distributor.py#L135-L136)（quantum 定义）、[launch.py:977-995](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/launch.py#L977-L995)（水位校验）、[ref_distributor.py:303-334](https://github.com/sgl-project/SpecForge/blob/a4fca140bc5fd12d6db40bc694c1a7dd790da57d/specforge/runtime/data_plane/ref_distributor.py#L303-L334)（零头收尾），确认每一项都站得住脚。

**参考答案要点**：

1. `refs_per_rank_step=4, refs_per_rank_batch=2`；`dispatch_quantum=16, dispatch_round_quantum=8`；一个窗口分 `accumulation_steps=2` 轮派发，每轮每个 rank 拿 `batch_size=2` 条，一个 optimizer 步每个 rank 累计 `2×2=4` 条。
2. 高水位与（解析后的）低水位都必须 ≥ quantum=16；默认 256 合法。若高水位 < 16，producer 在捕获前直接 `ValueError` 并 `channel.fail`，consumer 侧 `RefDistributor.pump` 因 `source.failure()` 非空而立即抛错。
3. `100 = 16×6 + 4`，即 6 个完整窗口（96 条）被正常派发并训练；剩 4 条零头 < 16，在 `_finish` 里被 `queue.fail(retryable=False)`、每条 `feature_store.abort`、`source.mark_consumed(4)`、`stats["dropped"]=4`，账本里仍是 committed-but-unacknowledged（该 attempt 不可被后续 run resume，需全新账本）。

## 6. 本讲小结

- `RefDistributor` 是整个 run **唯一的记账权威**，住在 rank 0：它是 producer 引用通道的唯一读者、durable 账本的唯一写者；每个 rank 只读私有 inbox，不做分片数学、不碰账本。
- 分发以 **quantum 窗口**为单位：`dispatch_quantum = dp_size × batch_size × accumulation_steps`，拆成 `accumulation_steps` 轮、每轮每 rank `batch_size` 条，保证 DP lockstep。
- 「**只在整窗口已本地 commit 时才开窗**」是核心不变量——它保证 EOF 永远落在窗口边界、每个被派发的 ref 都能被 durable ack；`_finish` 用 `AssertionError` 把这条不变量焊成硬校验。
- **quantum 握手**：consumer rank0 捕获前发布 quantum sidecar，producer 等到它并要求高/低水位都 ≥ quantum，否则 fail-fast，杜绝「双方互等」的死锁。
- producer 背压 = `published - consumed`，而 `consumed` 由 `RefDistributor._forward_consumed` 汇总各 rank inbox 计数器回写——**dispatch 不算消费**，只有 optimizer-durable ack 之后才推进。
- `DPAckController` 把 optimizer 边界的 ack 变成**两段 collective**：先 `all_gather` 去重并集、rank0 单一权威写一笔 SQLite 并广播结果，再各 rank 删自己分片特征并 `all_gather` 汇集清理错误；任一阶段失败则全 rank 一致失败。
- EOF 零头（不足一个 quantum）走 drop-last 式收尾：terminal fail + abort + 结算背压 + `dropped` 统计，绝不派发半个 optimizer 步；零头在账本里留 committed-but-unacknowledged，故带零头的成功 attempt 不可 resume。

## 7. 下一步学习建议

- **推理面**：本讲的 producer 端把 `SampleRef` 写进通道、把张量写进 Mooncake，但「模型执行 / 张量写入 / 元数据返回」具体发生在 `SGLangServerCaptureAdapter` 的哪一层？请接着读 **u7-l5 推理平面与 SGLang 捕获**，看 `RolloutWorker` 的 lease/produce_refs/commit 如何与本讲的通道对接。
- **恢复语义**：本讲多次提到「consumer-only 恢复」「fresh 账本」「durable marker 必须匹配 checkpoint」。这些约束的完整拼图在 **u9-l1 检查点与恢复**，建议结合 `reconcile_on_restart` 与 `_finish` 的零头行为一起读。
- **DP 与 SP 的交互**：`gather_id_union` 为什么要对 SP replicated 分片去重？这与并行拓扑强相关，建议读 **u8-l1 分布式初始化与设备网格** 和 **u8-l2 并行拓扑 USP 与 ring attention**，理解 TP/DP/SP 如何共同决定 device mesh 与每 rank 的样本归属。
- **想动手验证**：`tests/test_runtime/test_ref_distributor.py`、`test_disagg_online_dp_protocol.py`、`test_streaming_ref_channel.py` 是理解本讲行为的最佳「可执行规约」，建议逐个阅读其中的断言，它们精确刻画了 quantum、背压、前缀 ack、EOF 零头、毒丸失败等边界行为。
