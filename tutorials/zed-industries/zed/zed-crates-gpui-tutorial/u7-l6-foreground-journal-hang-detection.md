# 前台工作日志与卡顿检测：ForegroundJournal 与 HangDetector

## 1. 本讲目标

GPUI 的所有 UI 工作都发生在一条前台线程上（u2-l5 讲过双执行器模型）。这条线程一旦被某段同步计算长时间占住，整个应用就会冻结——没有掉帧日志、没有堆栈、用户只看到「卡了」。本讲讲解 `profiler` feature 下的一套可观测性子系统，学完后你应当能够：

1. 解释 `ForegroundJournal` 如何在主线程上用固定大小的无锁环形缓冲记录五类前台工作（任务轮询、动作处理、输入派发、窗口绘制、帧呈现），以及为什么低于 `TASK_POLL_FLOOR`（100µs）的轮询要折叠成计数摘要。
2. 说明边界条目（新帧呈现 / 前台转入空闲）如何把事件流切成一个个「活动区间」，`IntervalSealer` 如何把区间封口成 `FrameSnapshot`，`occupancy` 与 `busy_fraction` 又是怎么算出来的。
3. 使用 `HangDetector::poll` 读取 `HangIncident`，并解释两类触发条件：单事件时长 ≥ 卡顿阈值，或区间累计前台开销 ≥ 帧预算。
4. 沿着 `App::foreground_journal`、`PlatformScheduler` 的 `queued()` 计数、`WindowInvalidator::record_frame_pending`、`WindowProfiler` 的各对 begin/end 括号和 `PlatformInput::kind_name`，说出一次完整前台 turn 的记录链路。

## 2. 前置知识

阅读本讲前，你应当已经理解（对应前面各讲）：

- **前台线程与 turn**：GPUI 的实体更新、绘制、事件派发全部发生在单一前台线程（u1-l2、u2-l1）。一次「turn」指一段不可分割的前台工作，例如轮询一个 future、派发一条输入。
- **任务与调度**：`ForegroundExecutor` 在主线程轮询 future，任务经 `PlatformDispatcher::dispatch_on_main_thread` 入队（u2-l5）。`Task` 的 `Drop` 即取消。
- **窗口绘制管线**：`cx.notify()` 标脏窗口 → 平台请求帧 → `Window::draw` 走完元素树三阶段 → `present` 把场景提交给平台（u4-l3）。本讲的 `Draw`/`Present` 事件就是在这些环节埋点得到的。
- **profiler 三层采集**：u7-l5 讲过每线程任务计时（100µs 准入的 top-5 统计）、`WindowProfiler` 的帧直方图、`set_trace_enabled` 开关的逐帧事件缓冲。本讲的 journal 是第四层：**始终开启**、语义化、面向卡顿归因，它由 1861e58f98 引入。
- **无锁数据结构直觉**：会读 `compare_exchange` 即可，本讲不要求写过无锁代码。

两个关键术语先给出定义：

- **活动区间（interval）**：从前一个边界结束到下一个边界结束之间的一段前台历史。边界只有两种：一帧新绘制的画面被提交给平台（Presented），或前台彻底空闲（Idle）。
- **卡顿（hang）**：任何一段单独的前台工作阻塞了前台线程超过阈值；或一个区间内前台总开销达到帧预算——许多小块工作也能像一次长停顿一样丢帧。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| `src/profiler/journal.rs` | 本讲主角一：`ForegroundEvent`/`ForegroundJournalEntry` 事件模型、`ForegroundJournalWriter`（主线程写入端）、`JournalRing`/`JournalPublisher`（固定大小环形缓冲）、`ForegroundJournalCollector`（独立游标读取端）、`IntervalSealer`/`FrameSnapshot`（区间封口与占用度计算） |
| `src/profiler/hang.rs` | 本讲主角二：`HangDetector`（轮询日志、产出 `HangIncident`）与 `SerializedHangIncident`/`SerializedHangContributor`（telemetry 友好的序列化形态） |
| `src/profiler.rs` | `TaskTiming`/`FrameTiming`/`PresentTiming`/`ActionTiming` 计时结构；`WindowProfiler` 的输入/动作/绘制/呈现四对括号；`update_running_task`/`save_task_timing` 两个全局钩子 |
| `src/app.rs` | `App` 持有 `foreground_journal` 句柄；构造时安装日志；`App::foreground_journal()` 对外暴露 |
| `src/executor.rs`、`src/platform_scheduler.rs` | `ForegroundRunnableCounter` 的 `queued()`/`finished()` 维护「还有任务排队」这个事实 |
| `src/window.rs` | `WindowInvalidator` 在窗口变脏时上报 `record_frame_pending`；`dispatch_event`/`dispatch_action`/`draw`/`present` 上的埋点括号 |
| `src/interactive.rs` | `PlatformInput::kind_name()`：为每种输入变体返回 `"key_down"`、`"scroll_wheel"` 等短静态名 |
| `../zed/src/reliability/hang_detection.rs` | 生产接线示范：Zed 应用如何用几十行把 `HangDetector` 挂到独立线程（拓展阅读） |

条件编译提示：以上几乎所有内容都在 `#[cfg(feature = "profiler")]` 之下，该 feature 只额外引入 `hdrhistogram` 依赖（`Cargo.toml` 中 `profiler = ["dep:hdrhistogram"]`）。不开 feature 时这些代码不存在，运行时零开销。

## 4. 核心概念与源码讲解

### 4.1 ForegroundJournal 与 ForegroundEvent：前台工作如何被记录

#### 4.1.1 概念说明

`ForegroundJournal` 是主线程的工作日志。它要回答的问题是：**「刚才前台线程到底在忙什么，每件事花了多久？」**

设计约束非常苛刻，由此产生了三个核心取舍：

1. **记录必须极廉价**。日志默认开启（不依赖 `set_trace_enabled`），热路径上每条事件只能做几次原子写。因此它住在主线程的 thread-local 里，写入永不等待读取者。
2. **流必须有界**。一个健康的应用每秒可能有几十万次低于 100µs 的任务轮询，逐条记录会瞬间写爆任何缓冲。解法是 `TASK_POLL_FLOOR`：短于 100µs 的轮询折叠进 `PollSummary`（只保留精确条数与总时长），流的大小由「慢轮询的数量」而非「轮询总数」决定。
3. **语义化边界**。日志不是无结构的计时列表，而是被两种边界条目切成区间：`Presented`（一帧新画面提交完成）与 `Idle`（前台回到平台空闲循环）。后面所有分析（帧预算、dirty-to-present、busy_fraction）都建立在这套区间语义上。

#### 4.1.2 核心流程

一次典型帧的事件流（按时序）：

```text
窗口变脏        → FrameState::Pending { window_id, dirty_at }   （元数据，不算工作）
输入到达        → Event::Input(InputTiming { kind: "key_down", ... })
动作处理        → Event::Action(ActionTiming { name: "editor::Save", ... })
开始绘制        → （begin_draw 括号，同时再报一次 frame_pending）
绘制完成        → Event::Draw(FrameTiming { dirty_at, invalidations, draw_start, draw_end })
提交平台完成    → Boundary::Presented(PresentedFrame { frame, presentation })
                  ↑ 边界：此前所有工作被封口成一个区间
前台空闲        → Boundary::Idle { ended_at }
```

写入端的守门逻辑（何时产生 Idle 边界）：

```text
end_turn(ended_at):
    turn_depth 还有外层?           → 不封口（嵌套 turn 只在最外层结束时考虑）
    runnable 计数 > 0?             → 不封口（还有任务马上要跑，不算空闲）
    存在未过期(1s 内)的 pending 帧? → 不封口（帧还没呈现，区间必须等它）
    本区间没有保留过任何事件?       → 丢弃折叠的小轮询，不封口
    否则                           → 写入 Boundary::Idle
```

事件按**完成顺序**记录：一个横跨帧边界的轮询（例如包住了一次 draw 的任务轮询）在它结束时才入环，其时间戳可能早于之前已记录的事件——读取端不假设流内时间单调。

#### 4.1.3 源码精读

五类事件与折叠摘要的定义：

- [src/profiler/journal.rs:L64-L79](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler/journal.rs#L64-L79) — `ForegroundEvent` 枚举：`TaskPoll`/`Action`/`Input`/`Draw`/`Present` 五类实际工作，加上 `SmallPolls`（折叠摘要）。每类携带自己的计时结构。
- [src/profiler/journal.rs:L29-L32](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler/journal.rs#L29-L32) — `TASK_POLL_FLOOR = 100µs`：折叠门槛，让流的大小由慢轮询数量决定。
- [src/profiler/journal.rs:L115-L127](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler/journal.rs#L115-L127) — `InputTiming`：`kind` 字段是 `&'static str`，来源就是 `PlatformInput::kind_name()`（见 4.5），另带 `caused_invalidation` 标记该输入是否弄脏了窗口。
- [src/profiler/journal.rs:L129-L154](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler/journal.rs#L129-L154) — `PollSummary`（count + total）与 `SmallPollFlush`（摘要 + 最紧包围区间 `since`/`until`）。保留 span 是为了后续能把折叠时间按比例摊到更窄的报告窗口。

写入端的折叠与记录：

- [src/profiler/journal.rs:L629-L639](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler/journal.rs#L629-L639) — `record_task_poll`：先 `finished()` 递减 runnable 计数，再按 `poll_duration() >= TASK_POLL_FLOOR` 决定逐条保留还是折叠，最后 `end_turn` 尝试封口。
- [src/profiler/journal.rs:L445-L476](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler/journal.rs#L445-L476) — `fold_small_poll` 把短轮询并入当前摘要并扩展 span；`record_entry` 保证已积累的 `SmallPolls` 摘要**紧贴在**下一条保留事件或边界之前被冲刷入环，维持流的时序语义。
- [src/profiler/journal.rs:L407-L434](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler/journal.rs#L407-L434) — `end_turn` 的四重守门。注释解释了「为什么只有折叠轮询的唤醒不封口」：定时器、文件监视器会造成无数孤立微唤醒，若每次都封口，折叠机制挡掉的轮询又会以边界条目的形式回到环里，几秒内写爆环形缓冲。

日志条目的完整类型与安装：

- [src/profiler/journal.rs:L225-L240](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler/journal.rs#L225-L240) — `ForegroundJournalEntry`：`Event`/`Boundary`/`FrameState`（元数据）/`Discontinuity`（丢失标记，消费者不得跨过它推断边界）。
- [src/profiler/journal.rs:L533-L554](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler/journal.rs#L533-L554) — `install_foreground_journal`：由 `App` 构造时调用一次；幂等，同线程多个 `App`（测试场景）共享一条日志流。
- [src/app.rs:L790-L791](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/app.rs#L790-L791) 与 [src/app.rs:L1938-L1943](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/app.rs#L1938-L1943) — `App` 构造时安装日志；`App::foreground_journal()` 返回可克隆的句柄给应用侧（如 Zed 的 reliability 模块）。

#### 4.1.4 代码实践

**实践目标**：不动手运行，通过阅读一个高质量集成测试，建立「四类前台工作 → 四类事件」的映射。

**操作步骤**：

1. 打开 [src/profiler/hang.rs:L1100-L1182](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler/hang.rs#L1100-L1182)，通读测试 `detects_randomly_placed_foreground_hangs`。
2. 该测试在一个真实 `VisualTestContext` 窗口中随机注入 1~4 次卡顿，注入点有四类：`cx.notify()` 后的 `render`（睡眠）、`simulate_mouse_down` 触发的输入派发、`dispatch_action` 触发的动作处理、`simulate_blocked_foreground_poll` 模拟的任务轮询。
3. 对照测试末尾的 `matches_kind`（[src/profiler/hang.rs:L1209-L1221](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler/hang.rs#L1209-L1221)），确认四类注入分别被断言为 `Draw`、`Input`、`Action`（按名字 `HangyAction` 匹配）、`TaskPoll`（按 spawn 位置的文件名匹配）。

**需要观察的现象**：测试如何区分「同类事件的多次注入」——它按注入时长降序逐一配对观察到的贡献者，睡眠不会提前醒来，因此观察时长必须 ≥ 注入时长。

**预期结果**：能在笔记里画出「注入手段 → 事件变体 → 匹配判据」三列对照表。

**待本地验证**：无需运行即成立（这是阅读型实践）；若想运行：`cargo test -p gpui --features profiler detects_randomly_placed`。

#### 4.1.5 小练习与答案

**练习 1**：一个任务的单次轮询耗时 60µs，它会被记录吗？

**答案**：不会逐条记录。60µs < `TASK_POLL_FLOOR`（100µs），它被折叠进 `PollSummary`：条数加一、总时长累加，随后以 `SmallPolls(SmallPollFlush)` 的形式在下一条保留事件前冲刷入环。它的精确计数与总时长没有丢失。

**练习 2**：为什么 `Discontinuity` 条目要求「消费者不得跨过它推断区间边界」？

**答案**：丢失的条目里可能恰好有 `Boundary`。若消费者假装丢失段是连续的，就会把两个区间错合并成一个，或把边界归错位置——帧预算类卡顿判定（依赖区间完整性）会基于错误前提。后面 4.3 会看到 `HangIncident::detect` 在日志不连续时直接抑制预算判定，只保留逐条观察到的超阈值事件。

**练习 3**：`FrameState::Pending` 是前台「工作」吗？

**答案**：不是。它只是控制面元数据（哪个窗口有一帧待呈现、何时变脏），用于门控 Idle 边界（`FRAME_DEADLINE`）和计算 `dirty_to_present`。`IntervalSealer` 对它直接跳过（见 [src/profiler/journal.rs:L1033-L1036](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler/journal.rs#L1033-L1036)），它不参与 occupancy 计算。

### 4.2 JournalRing 与 IntervalSealer：环形缓冲与区间封口

#### 4.2.1 概念说明

有了事件流，还需要两块基础设施：

- **JournalRing**：固定大小（约 4MiB 预算）的无锁环形缓冲，是「主线程永不等待读取者」这一承诺的落点。多个 `ForegroundJournalCollector` 各持独立游标并发读取，读不到的条目以 `Discontinuity` 如实报告，绝不阻塞写入。
- **IntervalSealer**：一个**纯状态机**，把 drained 出来的条目按边界分组为 `FrameSnapshot`。强调纯：它不看流逝时间、不猜测边界（不把「碰巧是一次 draw」当边界），只认显式的 `Boundary` 条目。这使它可以被任意分批喂入（生产中每秒喂一批、测试中一次喂完），结果完全一致——journal.rs 里甚至有一组 proptest 对照参考模型验证这一点。

`FrameSnapshot` 上最重要的两个派生量：

- **occupancy**：区间内前台真正在工作的时间。事件可能嵌套（输入派发内部同步画了一帧），所以取所有事件 span 裁剪到窗口后的**并集**，避免重复计数；折叠的小轮询没有个体时间戳，按 span 与窗口的重叠比例摊入（假设折叠时间在 span 上均匀分布）。
- **busy_fraction**：\( \text{busy\_fraction} = \min\!\left(1.0,\ \dfrac{\text{occupancy}}{\text{interval\_end} - \text{interval\_start}}\right) \)。低 busy_fraction 配上高 dirty_to_present 说明是调度/限流延迟而非应用在干活。

#### 4.2.2 核心流程

无锁槽位的协作协议（单写入者 + 多读取者）：

```text
槽结构: users(AtomicUsize) + sequence(AtomicU64) + entry(UnsafeCell)
写入 try_publish(seq, e):
    users 从 0 CAS 到 SLOT_WRITER（最高位）→ 独占槽
    写入 entry → 存 sequence → users 归零（Release）
    若槽被读者占着 → CAS 失败 → 进入 publisher 的 pending 队列（容量 64）下轮重试
读取 try_read(expected_seq):
    试加一位读者（users 计数 +1，且无写者）→ 失败则返回 None（记丢失）
    sequence 匹配 expected_seq? → Copy 出 entry（Copy 类型，读取即复制）
```

`collect_unseen` 的游标推进：

```text
end = finalized（已成功落槽的最大序列）
cursor < end - capacity? → 落后部分合并为一条 Discontinuity { lost }
逐槽读取 [cursor, end)，读不到的槽就地累计进 Discontinuity
```

Sealer 的封口规则：

```text
Event     → 区间为空时把 interval_start 提到 max(interval_start, event.start)（剔除前置空闲）
            SmallPolls → 存入 small_polls；其余存入 events（有 16k 上限，溢出只计数）
Presented 边界 → 先把 present 工作本身作为 Event 压入，再封口
任意边界  → 区间为空？推进 interval_start（不产出快照）
            否则 seal：生成 FrameSnapshot，interval_start 重置为边界时刻
FrameState → 跳过
Discontinuity → 计入 dropped_events，标记 journal_discontinuous
```

#### 4.2.3 源码精读

- [src/profiler/journal.rs:L666-L745](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler/journal.rs#L666-L745) — `JournalSlot`：`users` 的高位标记写者、低位计数读者，保证「写时无读者、读时无写者」；`try_read` 要求 `sequence` 精确匹配（`EMPTY_SEQUENCE = u64::MAX` 表示从未写入）。
- [src/profiler/journal.rs:L767-L796](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler/journal.rs#L767-L796) — `JournalRing`：`sequence % capacity` 定位槽；`offered`（已发出）与 `finalized`（已落槽）两个原子游标是读取端可见性的边界。
- [src/profiler/journal.rs:L803-L874](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler/journal.rs#L803-L874) — `JournalPublisher`：撞槽时进 `pending`（`VecDeque`，容量 64）按序重试；pending 也满则丢弃并计数 `dropped_after_pending`，让 `finalized` 追上 `offered`，把丢失如实暴露给读取端。
- [src/profiler/journal.rs:L45-L56](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler/journal.rs#L45-L56) — 容量预算：环 4MiB / 单槽大小（最坏每秒约 1 万条事件，可容纳数秒流量等消费者排空）；pending 64 只为吸收「读者恰好压住正被回绕的槽」这类瞬时碰撞；单区间事件上限 16k 是对病态事件风暴的兜底。
- [src/profiler/journal.rs:L876-L905](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler/journal.rs#L876-L905) — `ForegroundJournal::collector()`：每个收集器从当前 `offered` 开始观察，互相独立。
- [src/profiler/journal.rs:L929-L961](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler/journal.rs#L929-L961) — `collect_unseen`：先按 `finalized - capacity` 钳制游标并合成丢失标记，再逐槽读取；相邻丢失合并进同一条 `Discontinuity`。
- [src/profiler/journal.rs:L287-L354](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler/journal.rs#L287-L354) — `occupancy_within`：事件 span 裁剪到窗口、排序、合并重叠（嵌套不重复计）；小轮询按 `overlap / span` 比例摊入 `summary.total`；`busy_fraction` 即上面的公式并钳到 1.0。
- [src/profiler/journal.rs:L963-L1086](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler/journal.rs#L963-L1086) — `IntervalSealer`：`push_entries` 返回本次批内完成的快照，未封口的工作跨调用保留；`seal` 用 `mem::take` 移交状态并把 `interval_start` 重置为边界时刻。

#### 4.2.4 代码实践

**实践目标**：用属性测试验证「sealer 的结果与喂入批次划分无关」，直观感受纯状态机的可测性。

**操作步骤**：

1. 运行：`cargo test -p gpui --features profiler profiler::journal`。
2. 重点看两个测试：`completion_order_sealer_matches_reference_under_arbitrary_batching`（[src/profiler/journal.rs:L2469-L2505](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler/journal.rs#L2469-L2505)，同一批条目整批喂与随机分批喂必须得到相同快照）与 `ring_matches_reference_model_under_wrap_collisions_and_independent_cursors`（[src/profiler/journal.rs:L2341-L2441](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler/journal.rs#L2341-L2441)，随机「发布/收集/新建收集器/钉住槽/放开槽」操作序列下，真实环与参考模型逐步对账）。

**需要观察的现象**：proptest 会生成上百组随机操作序列，任何一组不一致都会以最小化用例的形式报告。

**预期结果**：全部通过；如果本地机器慢，完整 journal 测试套件（含并发 2 万条发布的 `concurrent_collection_preserves_the_complete_logical_sequence`）应在数秒内完成。

**待本地验证**：具体耗时依机器而定。

#### 4.2.5 小练习与答案

**练习 1**：为什么读取者「钉住」一个槽不会拖慢写入者？

**答案**：写入者对被占槽的 `try_publish` CAS 会失败，但失败不等待——条目进入 publisher 的 `pending` 队列（至多 64 条），在下次发布时按序重试。前台线程永远只做几次原子操作；若 pending 也满了才丢弃并让 `finalized` 追上 `offered`，把损失报告为 `Discontinuity`。代价转移给了读取端：漏看的条目再也看不到。

**练习 2**：`Presented` 边界到来时，sealer 为什么要先把 `Present` 作为事件压入再封口？

**答案**：平台提交（`platform_window.draw(&scene)`）本身消耗前台时间，是区间工作的一部分。若不压入，这段时间既不出现在 `events` 里也不计入 occupancy，帧预算判定会系统性低估。见 [src/profiler/journal.rs:L1018-L1031](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler/journal.rs#L1018-L1031)。

**练习 3**：一个只包含折叠小轮询、没有任何保留事件的「区间」会发生什么？

**答案**：不会形成区间。写入端在真正空闲时直接丢弃这批折叠摘要（`retained_since_boundary` 为假则 `small_polls = None`），sealer 端遇到「空区间 + 边界」也只推进 `interval_start` 不产出快照。两道防线共同保证：孤立微唤醒既不写环，也不会累积到后续帧预算里。

### 4.3 HangDetector 与 HangIncident：两类卡顿判定

#### 4.3.1 概念说明

`HangDetector` 是日志的消费者。它组合了一个收集器和一个 sealer，`poll()` 时排空新条目、封口完成的区间，然后对每个 `FrameSnapshot` 应用两条判定：

1. **单事件阈值**：区间内存在 `duration() >= threshold` 的事件——一次任务轮询、动作处理、输入派发、绘制或平台提交中的任何一个把前台堵了这么久。这是用户感知到的「冻一下」。
2. **帧预算**：没有任何单事件越线，但区间总前台开销（occupancy，含折叠轮询摊入）≥ `frame_budget`。许多 1ms 的小工作同样能丢帧——甚至能让无窗口的无头应用挨饿。此时**区间内全部事件**都成为贡献者，因为没有哪一次单独的停顿能解释这个忙碌区间。

两条判定的不对等地位值得注意：阈值判定基于逐条观察，即使日志有缺口仍然可信；预算判定依赖区间完整，`journal_discontinuous` 时直接放弃（返回 `None`），宁可漏报不可误报。

检测是**事后**的：一段永远不让出前台的工作，在它让出（或进程死亡）之前不会被观察到。Zed 选用 100ms（release）作为阈值，正对应人手指能感知的卡顿下限。

#### 4.3.2 核心流程

```text
poll():
    drained = collector.collect_unseen()
    首次观察到 Presented 边界? → 记住 first_present_at（只锁存一次，用于 startup/steady 分相）
    snapshots = sealer.push_entries(drained.entries)
    对每个 snapshot:
        contributors = events 中 duration >= threshold 者
        if contributors 为空:
            if journal_discontinuous → 放弃（区间不完整，预算不可信）
            if occupancy(interval) < frame_budget → 放弃
            contributors = 全部 events（预算触发，人人有责）
        contributors 按时长降序排序（首位即最大停顿 stall）
        产出 HangIncident { snapshot, contributors }
```

`HangIncident` 上还有一个派生量 `active_window`：报告窗口从「起因」开始——封口帧的首次变脏时刻 `dirty_at`，无帧则取最早贡献者的开始——到封口为止。它把上一个帧与起因之间的空闲时间裁掉，让 `active_ms` 度量的是「这一卡到底持续多忙」，而不是把安静时间也算进去。

#### 4.3.3 源码精读

- [src/profiler/hang.rs:L1-L11](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler/hang.rs#L1-L11) — 模块文档，一段话讲清两类判定与事后性。
- [src/profiler/hang.rs:L24-L48](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler/hang.rs#L24-L48) — `HangDetector`（收集器 + sealer + 两个参数 + `first_present_at`）与 `HangIncident`（快照 + 贡献者，注意文档：预算触发时 contributors 是全部事件）。
- [src/profiler/hang.rs:L50-L89](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler/hang.rs#L50-L89) — 构造（只观察创建之后的事件）与 `poll`：注意 `first_present_at` 只在 `None` 时赋值，之后永久锁存。
- [src/profiler/hang.rs:L393-L425](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler/hang.rs#L393-L425) — `HangIncident::detect`：两条判定的完整实现，顺序就是「先逐条、后预算；不连续只信逐条」。
- [src/profiler/hang.rs:L370-L391](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler/hang.rs#L370-L391) — `active_window`：`min(dirty_at, 最早贡献者开始)` 到封口；贡献者可能在上次封口时已在运行，因此窗口允许早于快照的 `interval_start`。
- [src/profiler/journal.rs:L156-L172](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler/journal.rs#L156-L172) — `PresentedFrame::dirty_to_present_duration`：从帧首次变脏到提交完成的端到端延迟，`Option` 是因为变脏时刻可能未被观察到。
- 生产参数（拓展阅读）：[../zed/src/reliability/hang_detection.rs:L31-L59](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/zed/src/reliability/hang_detection.rs#L31-L59) — Zed 的接线：release 下 `hang_time = 100ms`、`frame_budget = 8ms`（一个 120Hz 刷新周期）；debug 构建（Windows 上 30s，其余 5s；预算同为 100ms）避免未优化代码天天误报。同文件还注册了 `dev: HangAction` 等三个「自卡」动作用于端到端验证。

#### 4.3.4 代码实践

**实践目标**：通过阅读边界测试，精确掌握两条判定的触发与不触发条件。

**操作步骤**：

1. 阅读 [src/profiler/hang.rs:L724-L767](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler/hang.rs#L724-L767) `an_interval_of_small_work_over_budget_is_an_incident`：三个 5/8/5ms 的轮询都低于 10ms 阈值，但总开销达预算 → 事件，且**全部三个**都是贡献者、`stall_ms` 取最长的 8ms。
2. 对照 [src/profiler/hang.rs:L808-L837](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler/hang.rs#L808-L837) `a_slow_frame_with_little_foreground_spend_is_not_an_incident`：dirty-to-present 长达 150ms 但前台只干了 5ms——**不是**卡顿，预算度量的是前台开销不是迟到。
3. 再看 [src/profiler/hang.rs:L922-L966](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler/hang.rs#L922-L966) `small_poll_spend_alone_can_reach_the_budget`：没有任何保留事件、只有 40 次共 12ms 的折叠轮询，照样触发预算事件——此时 `contributors` 为空、`stall_ms` 为 0，`small_poll_count/total_ms` 承担全部叙事。

**需要观察的现象**：三种「忙碌形态」——单次长停顿、多次中停顿、海量微停顿——分别由哪条判定捕获、贡献者列表各是什么样子。

**预期结果**：能口头复述：形态一由阈值捕获（贡献者只有越线事件）；形态二、三由预算捕获（贡献者是全部事件或空列表）。

**待本地验证**：可运行 `cargo test -p gpui --features profiler profiler::hang` 复核。

#### 4.3.5 小练习与答案

**练习 1**：为什么检测是「事后」的？一段 `loop {}` 死循环会被检测到吗？

**答案**：不会。写入发生在工作**结束**的括号处（`save_task_timing`、`end_input` 等），一段永不返回的同步代码永远走不到结束括号，日志里只有它之前的历史。这正是模块文档「Work that never yields back to the foreground is not observed until it does」的含义。要抓现行死循环需要看门狗线程采样栈，那是另一类工具。

**练习 2**：`FRAME_DEADLINE`（1 秒）和卡顿阈值（release 100ms）是什么关系？

**答案**：二者无关。`FRAME_DEADLINE` 是**写入端**的门控参数：一帧变脏超过 1 秒还没呈现（隐藏/被遮挡的窗口收不到帧回调）就不再阻止 Idle 边界，防止一个永不呈现的窗口把区间无限拉长；但它只是「解锁」，区间仍会在真正的呈现或空闲处封口——一次饿死帧的卡顿和它饿死的那一帧留在同一个 incident 里（见 [src/profiler/journal.rs:L34-L40](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler/journal.rs#L34-L40) 的注释与测试 `a_hang_outliving_the_frame_deadline_keeps_its_frame_association`）。阈值则是**消费端**判定单事件是否算卡顿的参数。

**练习 3**：`HangDetector` 必须跑在前台线程上吗？

**答案**：不必。它只持有 `ForegroundJournalCollector`（内部是 `Arc<JournalRing>`，槽是 `Sync` 的），可以在任意线程轮询。Zed 的生产实现正是把它放进独立线程、以 1 秒间隔 `poll`（[../zed/src/reliability/hang_detection.rs:L94-L130](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/zed/src/reliability/hang_detection.rs#L94-L130)），退出前在 `on_app_quit` 里做最后一次排空并上报遥测。

### 4.4 SerializedHangIncident 与贡献者：面向遥测的报告形态

#### 4.4.1 概念说明

`HangIncident` 内部用的是 `Instant` 与枚举，直接序列化会得到平台相关的时钟与冗余结构。`SerializedHangIncident` 把它转成遥测友好的纯数据：

- 时间统一为「自启动以来的毫秒」（微秒精度，输出形如 `115.954`）。
- 补充归因字段：`phase`（startup/steady，以首个新帧呈现完成为界——启动期卡顿常见且预期，稳态卡顿更值得警惕）、`sealed_by`（present/idle，标注边界而非原因）、`stall_ms`（最长单段，用户感知冻结的最佳估计）、`dirty_to_present_ms`、`busy_fraction`。
- 贡献者列表限量（调用方传 `max_contributors`，保最长若干个，其余计入 `contributors_elided`），按**开始顺序**排列，并给每个贡献者标注**嵌套深度**。

嵌套深度是这份报告最精妙的设计：前台工作是嵌套的——一次输入派发可能同步画了一帧，一次绘制内部可能轮询了任务。`depth` 表示「区间内有几个事件严格包含本事件」。depth-1 事件的时间已经躺在某个 depth-0 事件的时长里，跨深度把时长相加会重复计数；有了 depth，读报告的人能把「40ms 的 mouse_move 里套着 38ms 的 draw」正确读成一件事而不是两件。

#### 4.4.2 核心流程

```text
convert(startup, incident, max_contributors, first_present_at):
    active = active_window()
    busy_fraction = occupancy_within(active) / active 时长
    phase  = active 开始于 first_present_at 之后? "steady" : "startup"
    stall  = contributors[0].duration()   （已按最长排序）
    保留前 max_contributors 个 → 按开始时间排序 → 逐个标注 nesting_depth
    每个 ForegroundEvent 映射为对应 SerializedHangContributor 变体:
        TaskPoll{location: 序列化的 spawn 位置, ...}
        Action{name, ...}
        Input{input_kind: "key_down" 等, caused_invalidation, ...}
        Draw{window_id, dirty_to_draw_ms, invalidations, ...}
        Present{window_id, ...}
```

深度计算：对事件 e，统计区间内满足 `other.start <= e.start && e.end <= other.end && (other.start < e.start || e.end < other.end)` 的事件数——严格包含才计数，span 完全相同的事件互不包含，e 自己也不会把自己算进去。

#### 4.4.3 源码精读

- [src/profiler/hang.rs:L92-L142](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler/hang.rs#L92-L142) — `SerializedHangIncident` 全字段文档：每个字段都写明了语义与边界情形（如 `busy_fraction` 低而 `dirty_to_present_ms` 高 → 限流/调度延迟而非应用工作）。
- [src/profiler/hang.rs:L144-L216](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler/hang.rs#L144-L216) — `SerializedHangContributor`：`#[serde(tag = "kind", rename_all = "snake_case")]` 让 JSON 自描述变体；`Input` 的字段名用 `input_kind` 是因为 `kind` 被 serde 标签占用。
- [src/profiler/hang.rs:L224-L296](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler/hang.rs#L224-L296) — `convert`：注意 `stall_ms` 直接取排序后的首个贡献者；`busy_fraction` 四舍五入到三位小数；贡献者截断后重排为开始序。
- [src/profiler/hang.rs:L298-L310](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler/hang.rs#L298-L310) — `nesting_depth` 的严格包含判定。
- [src/profiler/hang.rs:L871-L917](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler/hang.rs#L871-L917) — 测试 `serialized_contributors_are_chronological_with_nesting_depths`：一次 40ms 的 `mouse_move` 内套着 38ms 的 draw，序列化为 depth 0 的 Input 跟随 depth 1 的 Draw——两段墙钟时间几乎相同的事件被正确表达为包含关系。

#### 4.4.4 代码实践

**实践目标**：手算一次 `busy_fraction`，验证你对 occupancy 并集计算的理解。

**操作步骤**：

1. 阅读 [src/profiler/hang.rs:L513-L596](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler/hang.rs#L513-L596) `serialized_incident_reports_presented_seal_fields`。
2. 区间 `[150, 400]`ms，帧在 100ms 首次变脏 → `start_ms = 100`、`active_ms = 300`；事件为 poll 150→300、action 300→340、input 340→345、present 380→400，另有折叠轮询 2 次共 1ms（span 105→145）。
3. 手算 occupancy：四个事件 span 首尾相接共 \(150+40+5+20=215\)ms，加上折叠的 1ms = 216ms；\(216/300 = 0.72\)。

**需要观察的现象**：测试断言 `busy_fraction == 0.72`、`stall_ms == 150.0`、`dirty_to_present_ms == Some(300.0)`、`contributors_elided == 2`（上限 1，合格贡献者 3 个）。

**预期结果**：手算与断言一致；若不一致，回到 4.2 的 `occupancy_within` 重读 span 合并逻辑。

**待本地验证**：无需运行，笔算即验证。

#### 4.4.5 小练习与答案

**练习 1**：`phase` 为什么以「第一个**新绘制**帧呈现完成」为界，而不是应用启动时刻？

**答案**：启动阶段（字体加载、首帧构建）卡顿是预期且通常一次性的；用户对「刚打开时顿一下」和「用着用着冻住」的耐受完全不同。`first_present_at` 锚定的是「界面真正活起来」的时刻，活动窗口始于其后的 incident 标为 steady，优先归因。见测试 `phase_is_startup_until_the_first_present`（[src/profiler/hang.rs:L624-L654](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler/hang.rs#L624-L654)）。

**练习 2**：贡献者上限截断保留的是「最长」还是「最早」的一批？截断后顺序如何？

**答案**：`convert` 先对已按最长排序的 `incident.contributors` 取前 `max_contributors` 个（保最长，`stall_ms` 必在其中），再把这些保留者**按开始时间排序**输出，便于按时间线阅读；被裁掉的数量记入 `contributors_elided`。

**练习 3**：`SmallPolls` 摘要可能成为贡献者吗？

**答案**：正常不可能——sealer 把折叠摘要收进 `small_polls` 而非 `events`，贡献者只从 `events` 里选。序列化代码仍防御性地把它编成一个 `<small poll summary>` 位置的 TaskPoll 而非 panic（[src/profiler/hang.rs:L351-L365](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler/hang.rs#L351-L365)）。

### 4.5 埋点接入：app / executor / window 的完整记录链路

#### 4.5.1 概念说明

前面四节是「日志系统本身」，本节回答：**事件是从哪里、被谁写进来的？** 整条链路可以概括为一句话：**每个子系统在自己的工作括号前后各调一次 journal 的全局函数，写入端据此维护 turn 深度与 pending 帧状态，App 只负责安装与分发句柄。**

五类埋点来源：

| 埋点 | 位置 | 写入的 journal 内容 |
| --- | --- | --- |
| runnable 执行括号 | 各平台 dispatcher（如 macOS trampoline、Linux worker） | `begin_turn` + `TaskPoll`/折叠 + `end_turn` |
| runnable 入队 | `PlatformScheduler::schedule_local`、`ForegroundExecutor::spawn_when_idle` | runnable 计数 +1（不写字节进环） |
| 输入/动作括号 | `WindowProfiler::begin_input`/`end_input`、`begin_action_handler`/`end_action_handler` | `Input`（带 kind_name）/`Action` 事件 + turn 括号 |
| 帧括号 | `WindowProfiler::begin_draw`/`end_draw`、`Window::present` | `FrameState::Pending`、`Draw`、`Present`/`Presented` 边界 |
| 窗口变脏/关闭 | `WindowInvalidator`、`WindowProfiler` 的 Drop | `FrameState::Pending`（去重后）/`Closed` |

`ForegroundRunnableCounter` 是其中最「看不见」的一环：它只是一个 `Arc<AtomicUsize>` 计数，但 `end_turn` 靠它判断「还有任务马上要跑」，避免在连续轮询之间错误地插入 Idle 边界。

#### 4.5.2 核心流程

一次完整的用户按键 turn（从空白到再次空白）：

```text
平台键盘事件到达
  └ window.dispatch_event
      ├ window_profiler.begin_input("key_down")   → begin_turn + 记录开始时刻
      ├ （监听器可能派发动作）
      │   └ dispatch_action
      │       ├ begin_action_handler(SaveAction)  → begin_turn + 记录动作名
      │       └ end_action_handler                → Event::Action + end_turn
      ├ caused_invalidation = update_count 变化?
      └ window_profiler.end_input(bool)           → Event::Input{kind:"key_down",...} + end_turn

cx.notify() 使窗口变脏
  └ WindowInvalidator::invalidate_view
      └ journal::record_frame_pending(window_id, dirty_at)   → FrameState::Pending

平台帧回调 → window.draw()
  ├ window_profiler.begin_draw                    → begin_turn + 再次 frame_pending
  └ window_profiler.end_draw(dirty_at, invalidations) → Event::Draw + end_turn

present()
  ├ foreground_turn() 守卫                        → begin_turn
  ├ platform_window.draw(&scene)                  （真正提交）
  └ window_profiler.record_present(start, end, …)
      └ journal::record_present(timing, Some(frame)) → Boundary::Presented + end_turn

最后一个 turn 结束且无排队任务、无未过期 pending 帧
  └ end_turn                                      → Boundary::Idle
```

任务轮询这条线则是：调度器把 runnable 投递到主线程时 `queued()` +1（[src/platform_scheduler.rs:L118-L123](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/platform_scheduler.rs#L118-L123)）；执行体外部包着 `update_running_task`（→ `begin_foreground_turn`）与 `save_task_timing`（→ `record_task_poll`，内部 `finished()` −1 并决定保留或折叠）。

#### 4.5.3 源码精读

**App 侧**：

- [src/app.rs:L692-L693](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/app.rs#L692-L693) 与 [src/app.rs:L790-L791](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/app.rs#L790-L791) — `App` 的 `foreground_journal` 字段与构造期安装。
- [src/app.rs:L1938-L1943](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/app.rs#L1938-L1943) — 应用侧唯一入口 `cx.foreground_journal()`。

**executor / 调度器侧（runnable 计数）**：

- [src/profiler/journal.rs:L357-L380](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler/journal.rs#L357-L380) — `ForegroundRunnableCounter`：`queued`/`finished`/`has_runnables`，一次原子加、一次 CAS 减。
- [src/platform_scheduler.rs:L118-L123](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/platform_scheduler.rs#L118-L123) — `schedule_local`：每个投往主线程的 runnable 计数 +1，紧随其后 `dispatch_on_main_thread`。
- [src/executor.rs:L380-L399](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/executor.rs#L380-L399) — `spawn_when_idle` 的自定义派发闭包同样计数；测试调度器不经过这些钩子，所以 [src/executor.rs:L333-L336](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/executor.rs#L333-L336) 在 test-support 下把计数器置为 `None`，避免只加不减。

**平台 dispatcher 的执行括号（任务轮询来源）**：

- [../gpui_macos/src/dispatcher.rs:L166-L175](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_macos/src/dispatcher.rs#L166-L175) — macOS GCD trampoline：`update_running_task` → `runnable.run()` → `save_task_timing`，固定三段式。
- [../gpui_linux/src/linux/dispatcher.rs:L42-L48](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui_linux/src/linux/dispatcher.rs#L42-L48) — Linux worker 线程同样的三段式（wayland/x11 主循环与定时器路径各有同构括号）。
- [src/profiler.rs:L668-L689](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler.rs#L668-L689) — 这两个钩子的实现：`update_running_task` 先 `journal::begin_foreground_turn`；`save_task_timing` 产出 `TaskTiming` 后 `journal::record_task_poll`。

**window 侧（输入/动作/帧括号）**：

- [src/window.rs:L5007-L5011](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/window.rs#L5007-L5011) — `dispatch_event` 开头：`begin_input(event.kind_name())`；[src/window.rs:L5138-L5143](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/window.rs#L5138-L5143) — 结尾以 `update_count` 差值判定 `caused_invalidation` 后 `end_input`。
- [src/interactive.rs:L824-L841](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/interactive.rs#L824-L841) — `PlatformInput::kind_name()`：12 个变体到短静态名的映射，正是 `InputTiming.kind` 的来源。
- [src/profiler.rs:L983-L1006](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler.rs#L983-L1006) — 动作括号：`begin_action_handler` 经 `update_running_action` 解析出 `&'static str` 动作名（未注册的动作得到 `"un-named"`）；`end_action_handler` 同时双写旧聚合存储与 journal（注释解释了为何 journal 条目按窗口跟踪）。
- [src/window.rs:L5588-L5592](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/window.rs#L5588-L5592) — 全局动作监听器的调用点被这对括号包住（捕获/冒泡各路径同样处理）。
- [src/window.rs:L2855-L2860](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/window.rs#L2855-L2860) — `Window::draw` 开头取走 `FrameDirtyAccumulator` 并 `begin_draw`；[src/window.rs:L2980-L2986](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/window.rs#L2980-L2986) — 结尾 `end_draw(dirty_at, invalidations)`。
- [src/profiler.rs:L1008-L1040](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler.rs#L1008-L1040) — 绘制括号实现：`begin_draw` 同时上报 `record_frame_pending`；`end_draw` 组装 `FrameTiming` 并写直方图 + journal 双路。
- [src/window.rs:L3015-L3031](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/window.rs#L3015-L3031) — `present`：用 RAII 守卫 `foreground_turn()` 包住整段提交，记录 `present_start`/`present_end` 后交给 `window_profiler.record_present`。
- [src/profiler.rs:L1078-L1125](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler.rs#L1078-L1125) — `record_present_at`：有 `pending_frame`（本窗口这次确实画过）→ `journal::record_present(timing, Some(frame))` 产生 **Presented 边界**；没有 → 只产生普通 `Present` 事件（重复提交同一帧不封口）。
- [src/window.rs:L165-L190](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/window.rs#L165-L190) 与 [src/window.rs:L246-L251](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/window.rs#L246-L251) — `WindowInvalidator::invalidate_view`：窗口**由干净变脏的那一刻**上报 `record_frame_pending`，`FrameDirtyAccumulator` 记住首脏时刻与合并的失效次数——这正是 `FrameTiming.dirty_at`/`invalidations` 与 `dirty_to_present` 的数据源头。
- [src/profiler/journal.rs:L482-L503](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler/journal.rs#L482-L503) — 写入端对 `record_frame_pending` 去重：同窗口已有未过期 pending 帧时，只有 `dirty_at` 前进了至少 `FRAME_DEADLINE` 才再记一条，避免每帧一条的噪音。
- [src/profiler.rs:L910-L938](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler.rs#L910-L938) 与 [src/profiler.rs:L1141-L1146](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler.rs#L1141-L1146) — `WindowProfiler::new` 建窗即报一次 pending；`Drop` 报 `Closed`，让窗口关闭后不再阻塞 Idle 边界。

已知债务（写代码时要避免踩坑）：[src/profiler/journal.rs:L601-L627](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler/journal.rs#L601-L627) 的 TODO 指出，多数括号是裸的 begin/end 调用对而非 RAII 守卫；若 panic 被 unwind 捕获，`turn_depth` 与 runnable 计数会永久失衡，静默禁用后续 Idle 边界。Zed 进程遇 panic 即 abort 所以目前是潜伏问题，`present` 已经改用守卫，新代码应效仿。

#### 4.5.4 代码实践

**实践目标**：亲手验证「四平台对称的任务括号」与「turn 守卫」两件事。

**操作步骤**：

1. 在仓库根目录执行只读搜索：`grep -rn "update_running_task" crates/gpui_macos crates/gpui_linux crates/gpui_windows --include=*.rs`（Windows 平台的对应实现留给读者按相同模式定位）。
2. 对每个命中检查它是否与 `save_task_timing` 成对出现、是否包住 `runnable.run()`。
3. 再看 [src/profiler/journal.rs:L608-L627](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler/journal.rs#L608-L627)：`ForegroundTurnGuard` 只在 Drop 时调 `end_foreground_turn`；对比 `present`（守卫）与 `begin_input`/`end_input`（裸对）两种写法。

**需要观察的现象**：每条执行路径上「计数 +1 → begin_turn → run → 记录事件 → end_turn（可能封口）」的完整骨架。

**预期结果**：macOS trampoline 与 Linux worker/主循环/定时器路径全部符合三段式；能说出把裸对改成守卫可以解决什么问题（unwind 安全）。

**待本地验证**：Windows 实现（`crates/gpui_windows`）的对应行为。

#### 4.5.5 小练习与答案

**练习 1**：`cx.notify()` 之后、`draw` 之前，journal 里多了什么条目？

**答案**：一条 `FrameState::Pending { window_id, dirty_at }`（窗口由干净变脏时上报，带去重），外加 `Effect::Notify` 引发的观察者工作可能产生的 `TaskPoll`/`Action` 等事件。`Pending` 是元数据，不计入 occupancy，但会让 `end_turn` 推迟产生 Idle 边界直到该帧呈现或超过 1 秒。

**练习 2**：为什么 `record_present` 需要 `frame: Option<FrameTiming>` 参数？`None` 意味着什么？

**答案**：平台可能重复提交同一帧（如窗口尺寸变化触发重绘提交但内容未重建）。`Some(frame)` 表示这是**新绘制**帧的提交 → 产生 `Presented` 边界并移除该窗口的 pending 记录；`None` 表示重提交 → 只产生普通 `Present` 事件。区分二者保证边界语义严格等于「一帧新画面的完成」，也让 present 间隔直方图不被重提交污染。

**练习 3**：测试平台（`TestDispatcher`）下 journal 还工作吗？

**答案**：部分工作。测试调度器不经过 `update_running_task`/`save_task_timing` 括号（所以 `ForegroundExecutor::new` 在 test-support 下把 runnable 计数器置 `None`），任务轮询事件因此缺失；但窗口的输入/动作/绘制/呈现括号照常生效——hang.rs 的集成测试正是靠这些括号检测注入的 render/input/action 卡顿，并手工调用公共钩子模拟被阻塞的任务轮询（[src/profiler/hang.rs:L1231-L1239](https://github.com/zed-industries/zed/blob/ec18126b1dbd32b089e51d7edee1e20b3bd53637/crates/gpui/src/profiler/hang.rs#L1231-L1239)）。

## 5. 综合实践

把整讲串起来：写一个能看到自己日志与卡顿报告的示例。以下为**示例代码**（非仓库原有），基于 `examples/hello_world.rs` 改造，建议直接在本地工作副本中临时修改并用 `git restore` 还原。

**步骤 1：改造示例**。在 `crates/gpui/examples/hello_world.rs` 的 `run_example` 中加入监控线程（`ForegroundJournal` 是 `Clone` 且其收集器可跨线程——生产中 Zed 正是把检测器放在独立线程）：

```rust
// 示例代码：加入 run_example 开头（cx: &mut App 之后）
use gpui::profiler::hang::HangDetector;
use gpui::profiler::journal::ForegroundJournalEntry;
use std::time::Duration;

let journal = cx.foreground_journal();
std::thread::spawn(move || {
    let mut raw = journal.collector(); // 独立游标：只看创建后的条目
    let mut detector =
        HangDetector::new(journal.clone(), Duration::from_millis(50), Duration::from_millis(8));
    loop {
        std::thread::sleep(Duration::from_millis(200));
        for entry in raw.collect_unseen().entries {
            println!("[journal] {entry:?}");   // 所有条目都实现了 Debug
        }
        for incident in detector.poll() {
            let report = gpui::profiler::hang::SerializedHangIncident::convert(
                scheduler::Instant::now() - Duration::from_secs(1), // 近似启动锚点，示例从简
                &incident, 8, detector.first_present_at(),
            );
            println!("[hang] {}", serde_json::to_string(&report).unwrap());
        }
    }
});
```

**步骤 2：打印一帧的完整事件序列**。以 `cargo run -p gpui --example hello_world --features profiler` 启动，观察开窗后前几百毫秒的输出，应能找到类似这样的序列：`FrameState::Pending` → `Event(Draw(...))` → `Boundary::Presented(...)`，中间可能夹着输入与折叠摘要。把看到的序列与本讲 4.1.2 的流程图逐条对上。

**步骤 3：制造两种掉帧形态**。给视图加两个触发途径（示例代码）：

```rust
// 形态 A：单次长停顿——输入派发期间睡 120ms（> 阈值 50ms）
//   在 render 里给某个色块加 .on_mouse_down(MouseButton::Left, |_, _, _| {
//       std::thread::sleep(Duration::from_millis(120)); })
// 形态 B：大量小工作——16 个前台任务各睡 1ms（每个 > 100µs 折叠门槛，
//   单个 < 50ms 阈值，合计 ≥ 8ms 预算）
//   cx.spawn(async move |_| {
//       for _ in 0..16 {
//           std::thread::sleep(Duration::from_millis(1));
//       }
//   }).detach();
```

**步骤 4：对比两份报告**。预期差异（**待本地验证**，具体数值随机器浮动）：

| 维度 | 形态 A（长停顿） | 形态 B（预算超支） |
| --- | --- | --- |
| 触发判定 | 阈值（单事件 ≥ 50ms） | 预算（occupancy ≥ 8ms，无单事件越线） |
| `stall_ms` | ≈120（那一次停顿） | ≈1（最长的一次 1ms 睡眠） |
| `active_ms` | 与停顿相当 | 覆盖整串小任务的时间窗 |
| `busy_fraction` | 接近 1.0 | 取决于任务间空隙，通常明显小于 1 |
| contributors | 1 条 `input`（`input_kind: "mouse_down"`，depth 0） | 16 条 `task_poll`（各自带 spawn 位置） |
| `sealed_by` | present 或 idle | 多半 present（随后必有一帧） |

**验收标准**：两种形态都能产出 `HangIncident`；形态 B 的报告里能看到 `contributors_elided`（16 > 上限 8）；把阈值改成 200ms 后形态 A 不再触发（逐条判定失效且 occupancy 未到预算），而形态 B 的报告不受影响——这直接验证了两条判定的独立性。

## 6. 本讲小结

- `ForegroundJournal` 在主线程用约 4MiB 的固定无锁环形缓冲记录五类前台工作；低于 100µs 的任务轮询折叠成「条数 + 总时长」摘要，写入者永不等待读取者，损失以 `Discontinuity` 如实上报。
- 事件流被两种显式边界切分：`Presented`（一帧新画面提交完成）与 `Idle`（前台空闲且无未过期的 pending 帧）。`IntervalSealer` 是不依赖时间启发式的纯状态机，把区间封口为 `FrameSnapshot`；`occupancy` 取事件 span 并集、小轮询按重叠比例摊入，`busy_fraction` = occupancy / 区间时长。
- `HangDetector::poll` 对每个封口区间应用两条判定：单事件时长 ≥ 阈值（release 下 Zed 用 100ms），或区间总前台开销 ≥ 帧预算（8ms，一个 120Hz 周期）；预算触发时全部事件都算贡献者，日志不连续时预算判定被抑制。
- `SerializedHangIncident` 输出毫秒时间戳、startup/steady 分相、stall/active/busy_fraction/dirty_to_present 与限量、按时序排列、带嵌套深度的贡献者列表——深度让「输入里套着绘制」不会被读成两次独立的墙钟消耗。
- 埋点全景：平台 dispatcher 的 runnable 三段式括号产生任务事件，`ForegroundRunnableCounter` 的 `queued()/finished()` 维持「还有任务排队」事实，`WindowProfiler` 的四对括号产生输入（带 `PlatformInput::kind_name`）、动作、绘制、呈现事件，`WindowInvalidator` 在窗口变脏时上报 pending 帧，`App` 负责安装并经 `cx.foreground_journal()` 分发句柄。
- 已知债务：多数括号是裸 begin/end 对而非 RAII 守卫，unwind 被捕获时会静默破坏 turn 深度；新代码应像 `present` 一样使用 `foreground_turn()` 守卫。

## 7. 下一步学习建议

- **u7-l7（综合实战）**：把本讲的检测思路带进毕业项目——为迷你文件浏览器开启 `profiler` feature，在大量树节点展开时观察 `Draw` 事件与预算触发形态，验证虚拟化列表（u6-l1/u6-l2）对帧预算的实际改善。
- **对照阅读 u7-l5 的旧三层**：profiler.rs 的任务统计、`WindowProfiler` 直方图、`FrameTimingCollector` 与本讲 journal 的关系是「聚合 vs 明细」「开关开启 vs 默认开启」；理解双写点（`end_action_handler`、`record_draw_timing`、`record_present_at`）有助于给新埋点选址。
- **拓展阅读生产接线**：`crates/zed/src/reliability/hang_detection.rs`（含 `logging`/`telemetry` 子模块）展示独立线程轮询、`on_app_quit` 终态排空、以及 `dev: HangForeground` 等自卡动作的端到端用法。
- **无锁与属性测试范式**：若对 `JournalSlot` 的单写多读协议感兴趣，精读 journal.rs 测试模块中的 `ModelRing` 参考模型与 proptest 对账（`ring_matches_reference_model_under_wrap_collisions_and_independent_cursors`），这是学习「用模型检验并发结构」的极好范本。
