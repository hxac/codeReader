# u7-l6 前台工作日志与卡顿检测：ForegroundJournal 与 HangDetector

## 1. 本讲目标

学完本讲，你应该能够：

1. 说清 `ForegroundJournal` 记录了哪些前台工作（任务轮询、动作处理、输入派发、窗口绘制、帧呈现），它们如何成为 `ForegroundEvent` / `ForegroundJournalEntry`，以及「小轮询折叠」「按完成顺序记录」这两条特殊规则。
2. 解释边界条目（`Presented` / `Idle`）如何把事件流切成一个个活动区间，`IntervalSealer` 这个纯状态机如何把区间封口成 `FrameSnapshot`，以及 `occupancy` 与 `busy_fraction` 是怎么算出来的。
3. 理解「便宜到可以默认开启」的工程取舍：低于 100µs 的轮询折叠进 `ForegroundRunnableCounter` 旁边的 `PollSummary`、固定 4MiB 的无锁环形缓冲、写线程永不为读者等待、丢失以 `Discontinuity` 显式上报。
4. 会用 `HangDetector::poll` 捕获 `HangIncident`，并说清两类触发条件：单个事件 ≥ 卡顿阈值（Zed release 取 100ms），或区间累计前台开销 ≥ 帧预算（Zed release 取 8ms）。
5. 沿着 `App::foreground_journal`、`PlatformScheduler::schedule_local` 的 `queued()`、平台调度器的 `update_running_task` / `save_task_timing`、`WindowInvalidator::record_frame_pending`、`WindowProfiler` 的输入/动作/绘制/呈现括号、`PlatformInput::kind_name`，完整说出一次前台 turn 的记录链路。

本讲是「专家层」的可观测性专题。前置依赖：u2-l5（前台/后台双执行器与平台调度器）、u4-l3（窗口绘制管线与按需排帧）、u7-l5（profiler 的三层采集体系）。u7-l5 讲的是「统计聚合」（直方图、top-5），本讲讲的是「逐事件日志」——同一批埋点的另一条出口。

## 2. 前置知识

- **前台线程（foreground thread）**：GPUI 的所有实体更新、绘制、输入派发都发生在单一主线程上（u2-l1、u2-l5）。任何一段同步计算（一次 `thread::sleep`、一个死循环、一个超长的动作处理器）都会让整个界面冻结——这就是「卡顿」（hang）。检测卡顿的前提是先记账：主线程每一刻在干什么。
- **turn（前台回合）**：一次对主线程的「进入—做完—返回空闲」过程。平台派发一个 runnable、派发一个输入事件、请求一帧，都会开启一个 turn。turn 可以嵌套（输入派发里同步触发了一帧绘制）。
- **环形缓冲（ring buffer）**：固定大小的数组 + 游标循环复用。写快读慢时旧数据被覆盖——内存有界，但需要显式告知读者「有数据丢了」。journal 用的是单写多读、带序列号的无锁变体。
- **`scheduler::Instant`**：scheduler crate 提供的时刻类型。测试平台里它走假时钟（u7-l4），生产平台里就是真实单调时钟。journal 里全部时间戳都是它。
- **`profiler` feature**：本讲全部代码都在 `#[cfg(feature = "profiler")]` 之后。该 feature 只额外引入 `hdrhistogram`（[Cargo.toml:L40](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/Cargo.toml#L40)），编译产物里常驻、默认开启（对 Zed 而言），不是「调试时才打开」的开关。
- **帧预算（frame budget）**：一帧的时间配额。120Hz 下约 8.3ms。一个区间里许多个 1ms 的小工作加起来超过了它，同样会掉帧——即使没有任何单项工作「看起来很慢」。

## 3. 本讲源码地图

| 文件 | 作用 |
| --- | --- |
| [src/profiler/journal.rs](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler/journal.rs) | 本讲主角之一：`ForegroundJournal`（固定大小环形日志）、`ForegroundEvent` / `ForegroundJournalEntry`、`IntervalSealer`（区间封口状态机）、`FrameSnapshot`（区间快照与占用率计算），以及主线程侧的写入器与全部 `record_*` 入口 |
| [src/profiler/hang.rs](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler/hang.rs) | 本讲主角之二：`HangDetector`（轮询日志、产出 `HangIncident`）与 `SerializedHangIncident` / `SerializedHangContributor`（遥测形态、嵌套深度） |
| [src/profiler.rs](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler.rs) | 采集核心：`TaskTiming` / `FrameTiming` / `PresentTiming` 计时结构、任务轮询的 turn 括号（`update_running_task` / `save_task_timing`）、`WindowProfiler` 把输入/动作/绘制/呈现逐一写入 journal |
| [src/app.rs](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/app.rs) | `App` 构造时安装日志（`install_foreground_journal`）并提供 `App::foreground_journal()` 访问器 |
| [src/executor.rs](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/executor.rs) | `ForegroundExecutor` 持有 runnable 计数器，`spawn_when_idle` 入队时 `queued()` |
| [src/platform_scheduler.rs](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/platform_scheduler.rs) | `PlatformScheduler::schedule_local` 入队时 `queued()`——runnable 计数的另一个入口 |
| [src/window.rs](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/window.rs) | 全部窗口侧接线：`WindowInvalidator` 标脏时上报 `record_frame_pending`、帧回调与 `present` 的 turn 括号、`dispatch_event` 与动作派发的 begin/end 埋点 |
| [src/interactive.rs](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/interactive.rs) | `PlatformInput::kind_name`：给每种输入变体一个短静态名（`"key_down"` 等），供日志与遥测使用 |
| [crates/zed/src/reliability/hang_detection.rs](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/zed/src/reliability/hang_detection.rs) | Zed 应用侧的真实消费者：在独立线程上每秒 `HangDetector::poll` 一次并上报遥测，阈值 100ms / 帧预算 8ms（release） |
| [crates/gpui_linux/src/linux/dispatcher.rs](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_linux/src/linux/dispatcher.rs) | 平台调度器三段式范本：`update_running_task` → `runnable.run()` → `save_task_timing`（macOS 的 dispatcher 同形） |

## 4. 核心概念与源码讲解

### 4.1 ForegroundJournal 与 ForegroundEvent：主线程在忙什么

#### 4.1.1 概念说明

`ForegroundJournal` 是 profiler feature 下的**主线程工作日志**：一段按时间排列的、有界的、可被多个消费者独立读取的事件流。它回答的问题是「刚才界面卡住的那 200ms 里，主线程到底在执行什么」。

u7-l5 的直方图只能回答「p99 的 draw 耗时是多少」，无法回答「是哪个任务的哪一次轮询把帧拖死的」。journal 补上这块：**逐事件、带源码位置、带因果边界**。

它的三条设计约束（都写在模块文档里）：

1. **只记主线程**。写入器是 thread-local 的，别的线程调用记录函数是无操作（no-op）。
2. **事件流必须有界**。用「小轮询折叠」把事件量约束在「慢轮询的数量级」上。
3. **边界必须是语义事件**。区间怎么切分由显式的边界条目决定，「绝不从流逝时间或 draw 这类顺带事件里推断边界」。

#### 4.1.2 核心流程

一次典型的「输入→重绘→呈现」会在日志里留下这样一串条目（按记录顺序）：

```text
FrameState(Pending { window_id, dirty_at })     ← 窗口第一次变脏（元数据，不是工作）
Event(Input(InputTiming { kind: "key_down", .. }))  ← 一次键盘派发
Event(TaskPoll(TaskTiming { location, .. }))    ← 一次 ≥100µs 的任务轮询
Event(SmallPolls { summary: { count: 37, total: 1.2ms }, .. })  ← 37 次低于 100µs 的轮询的折叠摘要
Event(Draw(FrameTiming { .. }))                 ← 一次窗口绘制
Boundary(Presented(PresentedFrame { .. }))      ← 帧提交平台：区间到此封口
```

要点：

- **按完成顺序记录**。一个跨帧边界仍在进行的事件（例如包住了 draw 的那次任务轮询）在它**结束**时才被记录，其开始时间可能早于此前已记录的事件。这让「区间归因」以结束时刻为准。
- **折叠规则**：轮询时长 < `TASK_POLL_FLOOR`（100µs）不单独记录，而是累进一个待冲刷的 `SmallPollFlush`（保留精确次数与总时长），在下一条被保留的事件或边界之前一次性冲出去。
- **两类非工作条目**：`FrameState` 是待决帧状态的控制面变化（纯元数据）；`Discontinuity` 显式声明「这里丢了 N 条逻辑条目」，禁止消费者跨缺口推断边界。

#### 4.1.3 源码精读

模块文档把整个设计一页说尽：[journal.rs:L1-L13](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler/journal.rs#L1-L13) —— 前台线程把任务轮询、动作处理器、输入派发、绘制、呈现记进有界环形日志；短于阈值的轮询折叠成摘要；呈现与转空闲是显式边界。

折叠的下限常量：[journal.rs:L29-L40](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler/journal.rs#L29-L40) 定义了 `TASK_POLL_FLOOR = 100µs`（事件量级被「慢轮询数」约束，同时保留精确计数与总时长）与 `FRAME_DEADLINE = 1s`（一帧脏了 1 秒还没呈现就不再阻止「转空闲」边界——隐藏或被遮挡的窗口收不到帧回调，不能让它永久压制边界）。

五种工作事件与折叠摘要构成 `ForegroundEvent`：[journal.rs:L64-L79](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler/journal.rs#L64-L79)。其中输入事件携带的 `kind` 是 `&'static str`：[journal.rs:L115-L127](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler/journal.rs#L115-L127) 明确指向 `PlatformInput::kind_name`（4.5 节精读），并记录该输入是否引发了对某窗口的失效。

折叠摘要的结构：[journal.rs:L129-L154](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler/journal.rs#L129-L154)。`PollSummary` 只有 `count` 与 `total` 两个字段；`SmallPollFlush` 额外携带最紧的包含区间（`since`/`until`），为的是占用率计算能把折叠时间按比例分摊到更窄的报告窗口（4.2 节）。

日志里实际存放的四类条目：[journal.rs:L226-L240](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler/journal.rs#L226-L240) —— `Event`（已完成的工作）、`Boundary`（语义边界）、`FrameState`（待决帧元数据）、`Discontinuity`（缺口，禁止跨缺口推断边界）。

写入器本体挂在线程局部变量上：[journal.rs:L524-L527](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler/journal.rs#L524-L527)。`FOREGROUND_JOURNAL` 是 `RefCell<Option<ForegroundJournalWriter>>`——未安装的线程上所有记录调用经 `with_journal`（[journal.rs:L593-L599](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler/journal.rs#L593-L599)）静默跳过。安装动作由 `App` 构造触发，且幂等：[journal.rs:L533-L554](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler/journal.rs#L533-L554)，同一线程上的多个 `App`（测试场景）共享一条日志流。

任务轮询的记录入口最能体现「折叠 + turn 收尾」的耦合：[journal.rs:L629-L639](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler/journal.rs#L629-L639)。它先调用 runnable 计数器的 `finished()`（每个曾 `queued()` 的 runnable 最终都有一次配对的轮询），再按 `TASK_POLL_FLOOR` 二选一：保留为 `TaskPoll` 事件或折叠；最后以该轮询的结束时刻收掉一个 turn。

#### 4.1.4 代码实践

**实践目标**：亲眼看到一条真实的日志流。

**操作步骤**：

1. 复制 `examples/hello_world.rs` 为 `examples/journal_probe.rs`（示例代码，非项目原有文件）。
2. 在 `run_example` 的 `application().run` 回调里、`open_window` 之前加入：

   ```rust
   // 示例代码：每 500ms 冲刷一次前台日志并打印
   let mut collector = cx.foreground_journal().collector();
   cx.spawn(async move |cx| {
       loop {
           cx.background_executor().timer(Duration::from_millis(500)).await;
           let drained = collector.collect_unseen();
           for entry in drained.entries {
               println!("[journal] {entry:?}");
           }
       }
   })
   .detach();
   ```

3. 顶部补 `use std::time::Duration;`。
4. 运行：

   ```bash
   cargo run -p gpui --example journal_probe --features profiler
   ```

**需要观察的现象**：窗口打开后每 500ms 打一批条目。初始阶段应能看到 `FrameState(Pending …)`、`Event(Draw …)`、`Boundary(Presented …)`；鼠标划过窗口（hover 样式重绘）会再次触发这一组；完全静止一段时间后日志安静下来（不再有 Idle 边界反复出现——空闲区间之间没有工作就不封口）。

**预期结果**：条目种类与 4.1.2 的序列吻合；`Draw` 条目里的 `invalidations` 会随你在同一帧内触发的失效次数增长。实际输出内容与机器、平台相关，具体数值**待本地验证**。

#### 4.1.5 小练习与答案

**练习 1**：为什么 `ForegroundEvent` 要按「完成顺序」而不是「开始顺序」记录？

**答案**：写入发生在一件事结束的时刻——这是唯一无需缓冲、无需预测的点（事件开始时不知道自己会不会跨帧、会跨多远）。代价是流内时间戳可能「倒序」：一个包住 draw 的长轮询在 draw 之后才记录，但开始时间更早。因为区间的切分与封口都发生在边界条目上（也是完成时刻），以完成顺序消费日志的 `IntervalSealer` 天然保证「区间里包含所有在边界之前结束的工作」。

**练习 2**：`FrameState(Pending)` 明明不是「工作」，为什么要进日志流？

**答案**：它是给消费者看的**控制面元数据**：记录某窗口从何时起有一帧等待呈现。写侧用它给 Idle 边界把关（还有未呈现帧就不算真空闲，见 4.2.3），读侧（如要计算 dirty-to-present 延迟的应用层）可以直接从流里取 `dirty_at`，而不需要另一套通道。sealer 本身对它无感（[journal.rs:L1033-L1036](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler/journal.rs#L1033-L1036)）。

**练习 3**：折叠为什么保 `since`/`until` 而不是只存 `count`/`total`？

**答案**：`FrameSnapshot::occupancy_within` 支持任意报告窗口（例如锚定在某帧首次失效时刻的窗口）。折叠后的轮询没有单独时间戳，只能按区间比例分摊：分摊量 \( = total \times \frac{overlap}{span} \)，其中 span 就是 `until - since`。没有这两个字段就无法做窗口对齐的占用率。

### 4.2 JournalRing 与 IntervalSealer：有界缓冲与区间封口

#### 4.2.1 概念说明

日志流的两侧是一对完全解耦的组件：

- **`JournalRing`（写侧基础设施）**：固定大小的槽位数组（约 4MiB），单写多读、无锁、永不阻塞写线程。写线程（主线程）宁可丢数据也不等读者。
- **`IntervalSealer`（读侧纯状态机）**：吃进 drained 条目，把「上一个边界之后到下一个边界之前」的所有工作打包成一个不可变的 `FrameSnapshot`。它没有任何时钟依赖——不从流逝时间推断任何事。

这一对组件合起来实现了本讲的核心抽象：**活动区间（activity interval）**。边界只有两种：

- `Presented`：一帧新绘的画面提交给了平台（携带 `PresentedFrame` = 这次绘制的 `FrameTiming` + 这次提交的 `PresentTiming`）。
- `Idle`：前台回到了平台空闲循环，且没有未过期（1 秒内）的待呈现帧。

#### 4.2.2 核心流程

**写入与读取的协作**（单写多读）：

```text
主线程（唯一写者）                        任意读者线程（多个，独立游标）
record_*() ──► JournalPublisher
                 │  try_publish(seq, entry) 到 ring 槽位
                 │  失败（槽位正被读者钉住）─► pending 队列（≤64）下轮重试
                 ▼
           JournalRing[seq % capacity]
                 │  每槽: users 原子计数 + sequence 原子序号
                 ▼
           collector.collect_unseen()
                 游标从 cursor 推进到 finalized；
                 槽位序号不匹配或被覆盖 → Discontinuity { lost }
```

**区间的封口条件**（写侧把关 Idle 边界）：最外层 turn 结束、且 runnable 计数为零、且没有 1 秒内变脏还未呈现的帧、且自上一边界以来至少保留过一条事件，才写入 `Idle` 边界。

**FrameSnapshot 的占用率**：区间内所有事件时间区间的**并集**（裁剪到区间内，嵌套工作不重复计数）加上折叠轮询的按比例分摊：

\[ \text{busy\_fraction} = \min\!\left(1,\ \frac{\text{occupancy}}{\text{interval\_end} - \text{interval\_start}}\right) \]

#### 4.2.3 源码精读

体量约束都在文件头部常量里：[journal.rs:L42-L56](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler/journal.rs#L42-L56) —— 单区间事件上限 16K（100µs 地板上「完全卡死的一秒」最多产生约 1 万次可记录轮询，达到这个上限说明系统已经病得很重）；环形缓冲按 4MiB 预算换算槽位数；pending 队列 64 条用来吸收与读者在「正被回绕的槽位」上的短暂碰撞，写线程永不为读者等待。

写侧对 Idle 边界的把关是理解「区间」语义的关键：[journal.rs:L403-L443](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler/journal.rs#L403-L443)。`end_turn` 里那行注释解释了一个微妙取舍：偶发唤醒（定时器、文件监听）每次只留下一个微小轮询，若每次转空闲都封口，被折叠挡在环外的轮询会以边界的形式重新涌入、几秒内写穿环。因此**只有折叠轮询、没有任何保留事件的「空闲」会在真正空闲时把折叠摘要直接丢弃**——不相干的唤醒不能把开销累积给以后某个帧预算。`has_unexpired_pending_frame` 同时实现 `FRAME_DEADLINE`：超过 1 秒还没呈现的脏帧不再阻止 Idle 边界，但只解除阻塞，区间仍会在真正的呈现或空闲边界封口——「饿死帧的卡顿」与「被饿死的帧」留在同一个区间里。

折叠的累积与冲刷：[journal.rs:L445-L476](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler/journal.rs#L445-L476)。`record_entry` 发布任何条目前先取走待冲刷的小轮询，保证顺序正确；`retained_since_boundary` 标志在边界条目后复位。

呈现的记录分两态：[journal.rs:L505-L521](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler/journal.rs#L505-L521)。**有新绘帧**的提交产出 `Boundary(Presented)`（封口区间并清掉该窗口的待决记录）；**没有新帧**的提交（比如重复呈现同一画面）只是普通 `Event(Present)`，不封口。

槽位与环的实现：[journal.rs:L666-L745](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler/journal.rs#L666-L745)。每个槽有 `users`（最高位是写者标志、其余是读者计数）与 `sequence`（逻辑序号，`u64::MAX` 表示空）；写者用一次 CAS 从 0 换到 `SLOT_WRITER` 独占槽位、写入后先存序号再放锁；读者先加读者计数、校验序号匹配再拷贝——序号不匹配说明该槽已被更新的条目复用。环本身只是 `sequence % capacity` 的定位器：[journal.rs:L767-L796](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler/journal.rs#L767-L796)。

发布器的 pending 重试：[journal.rs:L803-L874](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler/journal.rs#L803-L874)。发布失败不丢逻辑序号——条目进 pending 队列按序重试；队列满后的丢弃累计到 `dropped_after_pending`，以 `finalized` 游标的跳跃反映为读者的 `Discontinuity`。

读取端：`ForegroundJournal::collector`（[journal.rs:L898-L906](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler/journal.rs#L898-L906)）把游标初始化在当前 `offered`——**只观察创建之后的事件**；`collect_unseen`（[journal.rs:L933-L960](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler/journal.rs#L933-L960)）把游标钳制到「仍被保留的窗口」，被覆盖的部分先合成一条 `Discontinuity { lost }`，逐槽读取失败的再就地合并进相邻的间断条目。

封口状态机：[journal.rs:L963-L1086](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler/journal.rs#L963-L1086)。`push_entries` 遇到 `Presented` 边界时先把呈现工作本身入列（呈现也是区间的一部分），空区间则只推进 `interval_start`（排除区间首事件之前的空闲时间，不靠时间启发式）；`seal` 取走全部累积状态产出快照。事件数触顶时的优雅降级在 `push_small_polls`：不是丢弃轮询时间，而是把最后一个 flush 的区间拉宽（代价是分摊变粗）。

占用率与忙碌分数：[journal.rs:L282-L355](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler/journal.rs#L282-L355)。`occupancy_within` 先把每个事件的区间裁剪到报告窗口、排序后做区间并集（嵌套工作不双计），再对每个折叠 flush 按重叠比例分摊（折叠时间假设在 span 上均匀分布）。

#### 4.2.4 代码实践

**实践目标**：用 journal 自己的单元测试验证区间语义，特别是「折叠轮询在真空闲时被丢弃」。

**操作步骤**：

1. 运行 journal 模块的测试：

   ```bash
   cargo test -p gpui --features profiler profiler::journal::
   ```

2. 阅读两个测试：
   - `small_polls_are_flushed_immediately_before_a_retained_event`（[journal.rs:L1453-L1499](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler/journal.rs#L1453-L1499)）：小轮询必须在下一条被保留的事件**之前**冲出去（顺序保证）。
   - `sparse_small_polls_are_discarded_when_the_foreground_returns_to_idle`（[journal.rs:L1534-L1567](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler/journal.rs#L1534-L1567)）：只有零星小轮询、无保留事件的空闲，折叠摘要被整体丢弃。
3. 再看 `a_pending_frame_prevents_idle_until_presentation`（[journal.rs:L1267-L1299](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler/journal.rs#L1267-L1299)）与 `an_expired_frame_no_longer_blocks_idle_boundaries`（[journal.rs:L1352-L1390](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler/journal.rs#L1352-L1390)）这对测试，对照 `FRAME_DEADLINE` 的语义。

**需要观察的现象**：全部测试通过；断言里能直接看到快照的 `small_polls`、`dropped_events`、边界种类。

**预期结果**：`cargo test` 绿色。若失败请先确认带了 `--features profiler`（无 feature 时该模块根本不编译）。

#### 4.2.5 小练习与答案

**练习 1**：写线程为什么「宁可丢数据也不等读者」？丢的数据去哪了？

**答案**：写线程是主线程本身——为遥测等待读者等于制造要检测的卡顿，本末倒置。丢失有两条路：槽位被回绕覆盖（读者游标落后超过容量），或 pending 队列（64 条）满后继续丢弃。两条路都汇聚成读者可见的 `Discontinuity { lost }` 条目与 `DrainedEntries::lost` 计数，`FrameSnapshot::journal_discontinuous` 据此标记「该区间不完整」——下游（4.3）对不完整区间会降级处理。

**练习 2**：`IntervalSealer` 为什么不做任何时钟推断？

**答案**：凭时间切区间会把「一段安静的等待」（比如等 vsync、等平台帧回调）误判为边界或漏掉真正的边界；而语义边界（呈现完成、转空闲）本身就是「一段前台工作的因果终点」。测试 `a_slow_frame_with_little_foreground_spend_is_not_an_incident`（hang.rs）反过来验证了这一点：帧很慢但前台没花时间（被限流/调度延迟），不是卡顿。

**练习 3**：两个 `App` 在同一线程上（gpui 测试常见）共享一条日志流，会不会互相污染？

**答案**：不会破坏正确性：安装幂等（[journal.rs:L536-L554](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler/journal.rs#L536-L554)），两个 App 的事件进同一条流，各自创建的 collector 各有独立游标、只观察创建之后的事件，因此一个 App 的探测器天然看不到另一个 App 早先的历史。

### 4.3 HangDetector 与 HangIncident：两类卡顿判定

#### 4.3.1 概念说明

`HangDetector` 是日志的**第一个内部消费者**：定期把新条目喂给自带的 `IntervalSealer`，每得到一个 `FrameSnapshot` 就判定一次——这个区间里发生卡顿了吗？

判定有两条彼此独立的路径（[hang.rs:L1-L12](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler/hang.rs#L1-L12) 的模块文档）：

1. **阈值路径**：区间里存在至少一个事件，时长 ≥ `threshold`。一次 120ms 的同步 sleep 就是这种。
2. **预算路径**：没有任何单事件过阈值，但区间累计前台开销（occupancy）≥ `frame_budget`。16 个各 1ms 的小轮询加起来 16ms > 8ms 预算，同样掉帧——「许多小块工作能把一帧拖死，和一次长停顿一样彻底」。

检测是**事后**的：卡顿要等到封口它的边界（呈现或转空闲）到达才被报告。一个永不让出前台线程的死循环在它让出之前观测不到。

#### 4.3.2 核心流程

```text
HangDetector::poll()
  ├─ collector.collect_unseen()            取新条目
  ├─ 首个 Presented 边界 → 记住 first_present_at（供 4.4 的 phase 判定）
  ├─ sealer.push_entries(entries)          封口出若干 FrameSnapshot
  └─ 对每个 snapshot: HangIncident::detect(snapshot, threshold, frame_budget)
        ├─ 收集 duration ≥ threshold 的事件 → 非空 ⇒ 命中阈值路径
        ├─ 否则：若 journal_discontinuous ⇒ 放弃（缺口期间不可信累计结论）
        │        若 occupancy < frame_budget ⇒ 放弃
        │        否则 ⇒ 命中预算路径，贡献者 = 区间内全部事件
        └─ 贡献者按时长降序排列（最长者即 stall 的最佳估计）
```

Zed 应用的真实取值（[hang_detection.rs:L31-L53](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/zed/src/reliability/hang_detection.rs#L31-L53)）：阈值 release 100ms（debug 构建放宽到 5s，Windows debug 30s）；帧预算 release 8ms（「一个 120Hz 刷新周期」，debug 构建放宽到 100ms 防止未优化构建天天报警）。

#### 4.3.3 源码精读

判定函数本体：[hang.rs:L393-L425](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler/hang.rs#L393-L425)。注意三个细节：贡献者先按阈值过滤；预算路径在 `journal_discontinuous` 时直接返回 `None`（跨缺口的累计结论不可信——阈值路径留下的贡献者仍然有效，因为单个事件自带完整时间戳）；预算路径触发时贡献者是**区间内全部事件**（没有哪个单项能解释这个忙碌区间）。

探测器本体：[hang.rs:L24-L48](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler/hang.rs#L24-L48) 与 [hang.rs:L50-L89](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler/hang.rs#L50-L89)。`new` 同时建 collector 与 sealer——**只观察创建之后的事件**；`poll` 每次顺带锁存首个呈现边界（`first_present_at`）。

报告窗口不等于区间本身：[hang.rs:L370-L391](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler/hang.rs#L370-L391)。`active_window` 从「原因」（封口帧的首次失效时刻，或最早贡献者的开始时刻，取更早者）到封口——剪掉上一帧与原因之间的前台空闲，让 `busy_fraction` 分母里不含无关等待。

Zed 的消费方式是范本：[hang_detection.rs:L94-L135](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/zed/src/reliability/hang_detection.rs#L94-L135) 在 `App` 启动时创建探测器，随后 [hang_detection.rs:L139-L177](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/zed/src/reliability/hang_detection.rs#L139-L177) 把它移进一条独立的 `HangDetection` 线程，每秒 poll 一次——检测与上报和前台/后台的卡顿隔离（探测器里的 `spin::Mutex` 与环的 `Sync` 实现支持跨线程使用；在 gpui 示例里也可以简单地放进前台定时任务，见综合实践）。应用退出回调里再 poll 最后一次，把尾部的 incident 一并上报。

#### 4.3.4 代码实践

**实践目标**：亲手制造一次阈值路径的卡顿并检出它。

**操作步骤**：

1. 在 4.1 的 `journal_probe.rs 里加一个可点击区域（给根 `div` 加 `.id()` 与 `.on_click(...)`），点击处理器里 `std::thread::sleep(Duration::from_millis(120));`（示例代码——在真实应用里这是绝对禁止的同步阻塞，此处正是为了制造待检测的病灶）。
2. 把 collector 换成探测器（示例代码）：

   ```rust
   let startup = std::time::Instant::now();  // 注意: scheduler::Instant 未公开构造入口，
                                             // 示例用 std Instant 仅作演示；真实消费者
                                             // （zed）用的是 scheduler::Instant
   let mut detector = gpui::profiler::hang::HangDetector::new(
       cx.foreground_journal(),
       Duration::from_millis(100),
       Duration::from_millis(8),
   );
   cx.spawn(async move |cx| {
       loop {
           cx.background_executor().timer(Duration::from_millis(500)).await;
           for incident in detector.poll() {
               println!("[hang] {:#?}", incident);
           }
       }
   })
   .detach();
   ```

3. 用 release 模式运行（debug 构建的常规帧就能吃满 8ms 预算，报告会淹没在噪声里）：

   ```bash
   cargo run --release -p gpui --example journal_probe --features profiler
   ```

4. 点击那个阻塞区域，等最多 1 秒。

**需要观察的现象**：点击后界面冻结约 120ms；下一次 poll 打出 incident，`contributors` 首元素是一个 `Input` 事件、时长约 120ms（点击发生在一次 `mouse_down`/`mouse_up` 派发里——见 4.5.3 的 begin/end_input 括号）。

**预期结果**：incident 的 `snapshot.events` 里同时能看到这次输入引发的后续 `Draw`；`boundary` 是 `Presented`。具体数值**待本地验证**。

#### 4.3.5 小练习与答案

**练习 1**：为什么预算路径在 `journal_discontinuous` 时放弃，阈值路径不放弃？

**答案**：预算是**累计**结论——缺口里丢失的事件可能才是开销大头，跨缺口累计会严重低估或错归因，所以不可信。阈值路径的每个贡献者是**自包含**的（自带完整起止时间戳，单事件即结论），缺口之外的单事件结论不受影响。

**练习 2**：`an_idle_sealed_interval_over_budget_is_an_incident`（[hang.rs:L843-L871](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler/hang.rs#L843-L871) 测的是什么场景？

**答案**：无头（headless）应用或长时间无呈现的窗口：区间以 Idle 边界封口而非呈现。预算路径照样适用——模块文档点明「许多小块工作能把一帧拖死，或者把一个无头应用饿死，与一次长停顿同样彻底」。这说明判定与「有没有帧」无关，只与「前台花了多少时间」有关。

**练习 3**：检测为什么必须事后（post-hoc）？

**答案**：判定输入是**封口后的区间**：阈值要等事件结束才知道时长；预算要等边界才知道累计窗口；而且写侧 Idle 边界本身要求「turn 结束、无 runnable、无待呈现帧」——都只有事后才能成立。代价是永不返回前台的工作（死循环）检测不到，这是文档明示的边界条件。

### 4.4 SerializedHangIncident 与贡献者：遥测形态

#### 4.4.1 概念说明

`HangIncident` 里的 `FrameSnapshot` 带着 `scheduler::Instant`、`&'static Location` 这些不适合直接出门的字段。`SerializedHangIncident` 把它转成**遥测友好**的纯数据：时间戳与时长统一为「自应用启动以来的毫秒数」（微秒精度），位置转成 `SerializedLocation`，贡献者数量有上限。

两个最有信息量的字段：

- **`phase`**：`"startup"` 或 `"steady"`。以探测器观察到的**第一个新绘帧完成平台提交**的时刻（`first_present_at`）为界——启动期的卡顿用户尚未看到画面，严重性完全不同，遥测里要分开统计。
- **`stall_ms` 与 `active_ms` 的差**：`active_ms` 是报告窗口全长，`stall_ms` 是最长单块工作。多个停顿堆在一帧上时 `active_ms` 远大于 `stall_ms`——这正是「预算路径」的形态。

贡献者的 **`depth`（嵌套深度）**：前台工作是嵌套的——一次输入派发里可能同步跑一个动作处理器，一次绘制里可能轮询一个任务。depth 表示「有多少别的事件的区间完整包含它」。跨深度把时长**相加**会重复计数，depth 让消费端能正确聚合。

#### 4.4.2 核心流程

```text
HangIncident（内部形态）                SerializedHangIncident（遥测形态）
────────────────────────                ─────────────────────────────────
snapshot.interval_*          ──►        start_ms / active_ms   (自启动起, µs 精度 ms)
contributors[0].duration()   ─►         stall_ms
boundary: Presented|Idle     ─►         sealed_by = "present"|"idle", dirty_to_present_ms?
occupancy_within(active)     ─►         busy_fraction (四舍五入到 3 位小数)
contributors（按时长降序）     ─►        contributors（按开始时刻升序, 各带 depth, ≤ max）
区间事件数 / 折叠计数          ─►        event_count / small_poll_count / small_poll_total_ms
缺口信息                     ─►         dropped_events / journal_discontinuous
```

深度计算：对每个贡献者统计「区间内严格包含它的事件数」——相同区间互不包含，自身不计入。

#### 4.4.3 源码精读

序列化结构：[hang.rs:L92-L142](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler/hang.rs#L92-L142)。字段文档本身就是一份判读手册：`busy_fraction` 低而 `dirty_to_present_ms` 高说明是限流或调度延迟而非应用工作；`sealed_by` 标注的是边界不是原因，「原因」是第一个贡献者。

贡献者枚举：[hang.rs:L144-L216](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler/hang.rs#L144-L216)。五种变体各带最相关的字段：任务轮询带 spawn 位置（`location`）、动作带名称（`name`）、输入带 `input_kind` 与 `caused_invalidation`、绘制带 `dirty_to_draw_ms` 与合并的失效次数、呈现带窗口号。serde 以 `kind` 为标签、snake_case 命名。

毫秒的微秒精度化：[hang.rs:L218-L222](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler/hang.rs#L218-L222) —— `as_micros() as f64 / 1000.0`，让 JSON 里是 `115.954` 而不是一长串浮点尾差或整数微秒。

转换主函数：[hang.rs:L224-L296](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler/hang.rs#L224-L296)。注意 `contributors` 的排序变化：内部按时长降序（最重要的在前、`stall_ms` 必居首），序列化时改按开始时刻升序（时间线顺序，方便人读与后端按时间聚合），被上限裁掉的条目数记进 `contributors_elided`。

嵌套深度：[hang.rs:L298-L310](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler/hang.rs#L298-L310) 的 `nesting_depth` 是 O(n²) 的朴素计数——n 被上限约束（Zed 取 8，[hang_detection.rs:L29](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/zed/src/reliability/hang_detection.rs#L29)），朴素即正确即够用。

`small_poll_spend_alone_can_reach_the_budget`（[hang.rs:L923-L966](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler/hang.rs#L923-L966)）是预算路径的极端用例：区间没有任何保留事件，40 次折叠轮询共 12ms 单独达到预算——incident 的 `stall_ms` 为 0、`event_count` 为 0，故事完全由 `small_poll_count`/`small_poll_total_ms` 讲述。

#### 4.4.4 代码实践

**实践目标**：把 4.3 检出的 incident 转成序列化形态并判读字段。

**操作步骤**：

1. 在 4.3 的打印处改为（示例代码）：

   ```rust
   let first_present = detector.first_present_at();
   for incident in detector.poll() {
       let s = gpui::profiler::hang::SerializedHangIncident::convert(
           startup_instant, &incident, 8, first_present,
       );
       println!("[hang] {}", serde_json::to_string_pretty(&s).unwrap());
   }
   ```

   （`startup_instant` 用你记录的启动时刻；serde_json 是 gpui 的现成依赖，示例里可直接 `use`。）
2. 触发一次 120ms 阻塞点击，读输出。
3. 进阶（可选）：参照 `examples/input.rs` 的 `bind_keys` + `key_context` + `on_action` 三件套，给一个按键绑一个会 `sleep(150ms)` 的动作，按键触发后再看 incident。

**需要观察的现象**：点击场景的 `contributors` 里 `kind: "input"`、`input_kind: "mouse_down"`（或 `mouse_up`）、`duration_ms ≈ 120`；`sealed_by: "present"`；`phase` 取决于该 incident 是否发生在第一次呈现之前。按键场景里会出现 `input_kind: "key_down"` 的 depth 0 贡献者和 `kind: "action"`、`depth: 1` 的贡献者——动作处理器嵌套在输入派发里。

**预期结果**：两次实验的 `stall_ms`（点击≈120 / 按键≈150）与 `contributors` 嵌套关系明确；`dirty_to_present_ms` 应明显大于 `stall_ms`（帧要等下一次呈现时机）。数值**待本地验证**。

#### 4.4.5 小练习与答案

**练习 1**：为什么 `busy_fraction` 要在序列化时四舍五入到 3 位小数？

**答案**：纯体积考量：毫秒精度的时间除出来的比值带一堆浮点尾差（`0.08000000000000002`），对遥测毫无增量信息，3 位小数（千分之一）足够判读，还能提高下游列式存储的基数压缩率。`as_millis` 的微秒精度毫秒是同一个理由。

**练习 2**：`phase` 为什么用「第一个**新绘帧完成提交**」而不是「进程启动 N 秒内」划界？

**答案**：启动时长因机器、窗口大小、工作区而异，固定秒数既误伤慢机也放过快机的晚期启动卡顿。第一个新绘帧提交完成是语义事件：从这一刻起用户开始看到画面、卡顿开始有用户可感知的后果。`first_present_at` 在 `poll` 里锁存（[hang.rs:L74-L81](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler/hang.rs#L74-L81)），探测器创建之前的帧不算。

**练习 3**：贡献者上限裁剪保留的是「最长的」还是「最早的」？为什么 `stall_ms` 一定还在？

**答案**：保留最长的（`take(max_contributors)` 作用在按时长降序的 `contributors` 上，[hang.rs:L276-L282](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler/hang.rs#L276-L282)），再按开始时刻重排输出。`stall_ms` 取自 `contributors.first()` 且降序保证首元素就是最长者，而最长者必然在前 max 名内，所以 `stall_ms` 永远有实物支撑。

### 4.5 埋点接入：一次完整前台 turn 的记录链路

#### 4.5.1 概念说明

前面四节讲了「日志长什么样、怎么消费」；本节回答「数据从哪来」。journal 没有自己的采样线程——**所有条目都由被测代码路径上的括号埋点产生**。这些括号同时服务 u7-l5 的统计聚合与本讲的日志（双写）。

埋点分四组：

| 组 | 埋点 | 产出条目 |
| --- | --- | --- |
| 调度入队 | `schedule_local` / `spawn_when_idle` 的 `queued()` | runnable 计数（决定 Idle 边界的「无 runnable」条件） |
| 任务轮询 | 平台调度器的 `update_running_task` / `save_task_timing` | `TaskPoll` / `SmallPolls` + turn 括号 |
| 窗口生命周期 | `WindowInvalidator` 标脏、`WindowProfiler::new` / `Drop` | `FrameState(Pending/Closed)` |
| 交互与绘制 | `begin_input`/`end_input`、`begin_action_handler`/`end_action_handler`、`begin_draw`/`end_draw`、`present` 的括号 | `Input` / `Action` / `Draw` / `Present` 或 `Boundary(Presented)` |

#### 4.5.2 核心流程

以「用户按下一个键、界面重绘、帧提交」为例的完整记录链路：

```text
平台键盘事件到达
  └─ Window::dispatch_event (window.rs:5009)
       ├─ begin_input(event.kind_name())          → begin_foreground_turn + push 活动
       ├─ （监听器执行；若有动作命中）
       │    begin_action_handler / end_action_handler  → Action 事件（嵌套在输入 turn 内）
       └─ end_input(caused_invalidation)          → Input 事件（kind、是否引发失效）+ end_turn

视图 cx.notify() → WindowInvalidator::invalidate_view (window.rs:165)
  └─ 窗口首次变脏 → record_frame_pending(window_id, dirty_at)   → FrameState(Pending)

平台帧回调 on_request_frame (window.rs:1533)
  └─ foreground_turn() 括号（RAII guard，drop 时 end_turn）

Window::draw (window.rs:2854)
  ├─ take_frame_dirty() 取走本帧累计的 dirty_at/invalidations
  ├─ begin_draw()                                 → begin_turn + record_frame_pending
  ├─ （元素树三阶段；期间平台调度器可能轮询任务：
  │    update_running_task → begin_turn；save_task_timing → record_task_poll + end_turn）
  └─ end_draw(dirty_at, invalidations)            → Draw 事件 + end_turn

Window::present (window.rs:3016)
  ├─ foreground_turn() 括号 + present_start 计时
  ├─ platform_window.draw(&scene)                 → 提交平台
  └─ record_present(present_start, now, …)
       └─ journal::record_present                 → 有 pending_frame ⇒ Boundary(Presented)（封口！）

（此后主线程无 runnable、无待呈现帧、最外层 turn 结束）
  └─ end_turn 写下 Idle 边界 —— 下一个区间开始
```

#### 4.5.3 源码精读

**安装与访问**：`App` 构造时安装（[app.rs:L790-L791](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/app.rs#L790-L791)，字段声明在 [app.rs:L693](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/app.rs#L693)），公开访问器 `App::foreground_journal`（[app.rs:L1938-L1943](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/app.rs#L1938-L1943)）返回可 `Clone` 的句柄——同线程多个 App 共享流。

**runnable 计数**：`PlatformScheduler::schedule_local` 在把 runnable 交给平台主线程派发前 `queued()`（[platform_scheduler.rs:L118-L123](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/platform_scheduler.rs#L118-L123)）；`ForegroundExecutor::spawn_when_idle` 的派发闭包里同样计数（[executor.rs:L380-L399](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/executor.rs#L380-L399)）。计数器本体是 `Arc<AtomicUsize>`（[journal.rs:L357-L380](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler/journal.rs#L357-L380)），`finished()` 用 `fetch_update` 防下溢。注意一个诚实的例外：确定性测试调度器不走这些钩子，`ForegroundExecutor::new` 在 test-support 下把计数器置为 `None`（[executor.rs:L323-L336](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/executor.rs#L323-L336)），否则增量没有配对的减量。

**任务轮询括号**：`update_running_task` 开 turn（[profiler.rs:L668-L675](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler.rs#L668-L675)），`save_task_timing` 产出 `TaskTiming` 并交给 `record_task_poll`（[profiler.rs:L677-L689](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler.rs#L677-L689)）。这对括号由各平台调度器负责调用——Linux 的 worker 线程是标准三段式（[gpui_linux/src/linux/dispatcher.rs:L42-L48](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui_linux/src/linux/dispatcher.rs#L42-L48)：登记→执行→保存），macOS 的 dispatcher 同形。`TaskTiming` 的 `location` 是任务的 spawn 位置（`#[track_caller]` 捕获），这就是 4.4 里任务轮询贡献者能带源码位置的来源（[profiler.rs:L77-L83](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler.rs#L77-L83)）。

**窗口标脏**：`WindowInvalidator::invalidate_view` 在窗口从干净变脏的那一刻上报 `record_frame_pending`（[window.rs:L165-L190](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/window.rs#L165-L190)），`set_dirty` 同理（[window.rs:L196-L216](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/window.rs#L196-L216)）。首次失效时刻与失效次数由 `FrameDirtyAccumulator` 累积（[window.rs:L131-L141](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/window.rs#L131-L141)、[window.rs:L246-L256](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/window.rs#L246-L256)），draw 时一次性取走（[window.rs:L2854-L2860](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/window.rs#L2854-L2860)）——「一帧合并了多少次失效」由此而来。

**输入括号**：`dispatch_event` 以 `begin_input(event.kind_name())` 起手（[window.rs:L5007-L5012](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/window.rs#L5007-L5012)），用 `update_count` 的差值判断该输入是否引发失效，`end_input(caused_invalidation)` 收尾（[window.rs:L5138-L5143](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/window.rs#L5138-L5143)）。`kind_name` 的十二个短静态名在 [interactive.rs:L824-L841](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/interactive.rs#L824-L841)。窗口侧的 `WindowProfiler::begin_input`/`end_input`（[profiler.rs:L940-L980](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler.rs#L940-L980)）同时维护输入延迟直方图（u7-l5）与 journal 双写。

**动作括号**：动作派发的四个阶段（全局捕获、路径捕获、路径冒泡、全局冒泡，u5-l3）每处监听器调用都以 `begin_action_handler` / `end_action_handler` 包裹（如 [window.rs:L5581-L5596](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/window.rs#L5581-L5596)，四组括号分布在 [window.rs:L5588-L5671](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/window.rs#L5588-L5671)）。窗口侧实现（[profiler.rs:L982-L1006](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler.rs#L982-L1006)）里那条注释解释了为什么 journal 条目挂在窗口上：旧的单一全局 running 槽在测试并发跑动作时会错乱。

**绘制与呈现括号**：`begin_draw` / `end_draw`（[profiler.rs:L1008-L1040](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler.rs#L1008-L1040)）产出 `FrameTiming`（结构定义 [profiler.rs:L785-L815](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler.rs#L785-L815)，`end_draw` 的调用点在 [window.rs:L2980-L2986](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/window.rs#L2980-L2986)）。`Window::present` 以 RAII turn 括号 + `present_start` 计时开始，提交后调用 `record_present`（[window.rs:L3015-L3031](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/window.rs#L3015-L3031)）；窗口侧 `record_present_at`（[profiler.rs:L1078-L1125](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler.rs#L1078-L1125)）取出 `pending_frame`（上一次 `end_draw` 存入，[profiler.rs:L1127-L1132](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler.rs#L1127-L1132)）：**有**则交给 journal 产出 `Presented` 边界（封口），**无**则只是普通 `Present` 事件。`PresentTiming` 定义在 [profiler.rs:L817-L838](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler.rs#L817-L838)。帧回调 `on_request_frame` 整体套一层 `foreground_turn`（[window.rs:L1533-L1543](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/window.rs#L1533-L1543)）。窗口销毁时 `WindowProfiler::drop` 上报 `FrameState(Closed)`（[profiler.rs:L1141-L1146](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler.rs#L1141-L1146)）——已关窗口的待决帧不再阻塞 Idle 边界。

代码里有一条诚实的欠账值得知道：[journal.rs:L601-L607](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler/journal.rs#L601-L607) 的 TODO 指出调度器与 `WindowProfiler` 里的 turn 括号目前是裸的 begin/end 对而非 RAII guard，panic 被 catch 时 `turn_depth` 会永久失衡（Zed 对 panic 直接 abort 所以是潜在问题，作为库使用时才暴露）。

#### 4.5.4 代码实践

**实践目标**：不经运行、纯靠 grep 验证 4.5.2 的链路图。

**操作步骤**：

1. 在 `crates/gpui/src` 下执行：

   ```bash
   grep -rn "begin_action_handler\|end_action_handler" src/window.rs | wc -l
   grep -rn "record_frame_pending" src/ | grep -v "profiler/journal"
   grep -rn "update_running_task\|save_task_timing" ../gpui_linux/src ../gpui_macos/src
   ```

2. 对照 4.5.2 的链路图，给每个箭头标注你找到的 `文件:行号`。
3. 回答：为什么 `record_frame_pending` 在窗口侧有两处调用点（`invalidate_view` 与 `set_dirty`）之外，`WindowProfiler` 内部还有两处（`new` 与 `begin_draw`）？

**需要观察的现象**：`begin_action_handler` 恰好四组括号（对应动作派发四阶段）；`record_frame_pending` 共四处窗口侧/剖析器侧调用；Linux 与 macOS 的调度器都有 `update_running_task`/`save_task_timing` 三段式。

**预期结果**：链路图上每个箭头都有真实行号支撑。`WindowProfiler::new` 的上报是因为新窗口创建即待绘首帧；`begin_draw` 的上报是因为上一帧呈现后窗口回到「干净」，开始画新帧时又变脏——这两处保证「待决帧」状态机不漏窗。

#### 4.5.5 小练习与答案

**练习 1**：为什么 `ForegroundExecutor` 在 test-support 构建里把 runnable 计数器置为 `None`？

**答案**：计数靠两条路径配对：入队路径（`schedule_local`/`spawn_when_idle`）`queued()`，执行路径（`save_task_timing` → `record_task_poll`）`finished()`。确定性测试调度器（u7-l4）不经过平台派发、不调用 GPUI 的剖析钩子，若只有增量没有减量，计数只增不减，`has_runnables()` 永真，Idle 边界永不产生——日志退化。置 `None` 是把这条不配对的路径显式关掉（[executor.rs:L333-L336](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/executor.rs#L333-L336)）。

**练习 2**：`InputTiming::caused_invalidation` 是怎么算出来的？它对下游有什么用？

**答案**：`dispatch_event` 记录派发前后 `WindowInvalidator::update_count()` 的差值（[window.rs:L5012](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/window.rs#L5012) 与 [window.rs:L5138-L5143](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/window.rs#L5138-L5143)），非零即引发失效。下游可区分「贵而无害的输入」（如纯 hover 移动，不触发重绘）与「引发重绘的输入」；同时窗口侧用它维护输入延迟直方图（无效输入不进延迟样本）。

**练习 3**：同样叫 `record_present`，什么时候产边界、什么时候产普通事件？

**答案**：看 `WindowProfiler` 里是否有 `pending_frame`（上一次 `end_draw` 存下的 `FrameTiming`）。有新绘帧的提交 = 一个帧周期的因果终点，产 `Boundary(Presented)` 封口区间；没有新绘帧的提交（重复呈现同一画面、纯装饰性的提交）只是又一次前台工作，产 `Event(Present)`，区间继续等真正的边界（[profiler.rs:L1096-L1110](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler.rs#L1096-L1110) 与 [journal.rs:L505-L521](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/gpui/src/profiler/journal.rs#L505-L521)）。

## 5. 综合实践

把 4.1–4.4 的碎片拼成一个完整的「卡顿实验台」示例（示例代码，建议在 `examples/journal_probe.rs` 中完成）：

1. **界面**：根视图放两块可点击区域——
   - 「长停顿」：点击处理器里 `std::thread::sleep(120ms)`（制造**阈值路径**的病灶：单事件 ≥ 100ms）；
   - 「碎片风暴」：点击后用 `cx.spawn` 派发 16 个前台小任务、每个 `sleep(1ms)` 后结束（制造**预算路径**的病灶：没有任何单事件过阈值，累计 ≥ 8ms）。
2. **探测器**：`HangDetector::new(cx.foreground_journal(), 100ms, 8ms)`，放进每 500ms 触发一次的前台定时任务里 `poll()`，对每个 incident 用 `SerializedHangIncident::convert(startup, &incident, 8, detector.first_present_at())` 序列化打印。
3. **对照阅读**：分别触发两种病灶，把两次 incident 的 `stall_ms`、`active_ms`、`busy_fraction`、`contributors`（长度、种类、depth）填进一张对比表。
4. **预期结论**（待本地验证）：
   - 长停顿：`stall_ms ≈ 120`，贡献者极少（一次 Input，可能带后续 Draw），`busy_fraction` 高；
   - 碎片风暴：`stall_ms ≈ 1`（最长单个轮询），`contributors` 是一串 1ms 的 `TaskPoll`（各带 spawn 位置）加上引发它们的 Input，`busy_fraction` 与 `active_ms` 共同讲述「许多小块堆满一帧」；
   - 两种形态都产出了 incident——验证两类判定条件各自生效。
5. **延伸**：把帧预算调到 1ms 再跑「碎片风暴」，观察报告数量与 `journal_discontinuous` 的变化；把长停顿放进动作处理器（绑定按键）而不是点击，观察 `depth` 如何从 0（Input）变 1（Action）。

## 6. 本讲小结

- `ForegroundJournal` 是 profiler feature 下常驻的主线程工作日志：任务轮询、动作、输入、绘制、呈现五类工作按**完成顺序**记入固定 4MiB 的单写多读无锁环形缓冲，写线程永不为读者等待，丢失以 `Discontinuity` 显式上报。
- 「便宜到可以默认开启」靠两条折叠：< `TASK_POLL_FLOOR`（100µs）的轮询折叠成带精确计数与总时长的 `PollSummary`（真空闲时整体丢弃，防止偶发唤醒写穿环）；边界只由**语义事件**（`Presented` / `Idle`）产生，Idle 还需无 runnable、无 1 秒内待呈现帧。
- `IntervalSealer` 是零时钟依赖的纯状态机，把边界之间的工作封成 `FrameSnapshot`；`occupancy` 是事件区间并集加折叠轮询按比例分摊，`busy_fraction` 以此对区间时长归一。
- `HangDetector` 事后轮询日志，两条独立判定：单事件 ≥ 阈值（Zed release 100ms），或区间累计前台开销 ≥ 帧预算（release 8ms，即一个 120Hz 周期）；缺口期间放弃累计结论但保留单事件结论。
- `SerializedHangIncident` 把 incident 转成遥测形态：`startup`/`steady` 相位以首个新绘帧提交为界、微秒精度的毫秒时间戳、按时长保留但按时间排序的带 `depth` 嵌套深度的贡献者列表（任务轮询带 spawn 位置、动作带名称、输入带 `kind_name`）。
- 全部数据来自被测路径上的括号埋点：`schedule_local`/`spawn_when_idle` 的 `queued()`、平台调度器三段式（`update_running_task` → run → `save_task_timing`）、`WindowInvalidator` 标脏上报、`WindowProfiler` 的输入/动作/绘制/呈现括号与 `present` 的边界产出——同一批括号同时喂 u7-l5 的直方图与本讲的日志。

## 7. 下一步学习建议

- **u7-l7（综合实战）**：把本讲的探测器接到你的毕业项目里，作为「性能验收」环节——任何交互路径都不该产出 incident。
- **对照阅读 Zed 应用侧消费者**：[crates/zed/src/reliability/hang_detection.rs](https://github.com/zed-industries/zed/blob/10b2925e7c44439b99aeb39d5402133e0ad49192/crates/zed/src/reliability/hang_detection.rs) 完整展示「独立检测线程 + 每秒 poll + 退出时最后一次 poll + 遥测上报」的生产级接法，以及 `dev::HangAction` 等自测动作。
- **回看 u7-l5 的双写关系**：`WindowProfiler` 的直方图（统计聚合、无界时间跨度）与本讲 journal（逐事件、有界环形）共用同一批括号；理解「聚合看趋势、日志看因果」的分工。
- **顺带精读调度 crate**：`scheduler` 的 `LocalExecutor` 如何携带 `RunnableMeta`（spawn 位置与时刻）穿过平台派发，是本讲 `TaskTiming::location` 的上游。
